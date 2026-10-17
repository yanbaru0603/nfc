import { StrictMode } from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useNavigationType } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, getSession } from '../api/client';
import type { ReceiveResponse } from '../api/types';
import { useSession } from '../session';
import { ReceivePage } from './ReceivePage';

vi.mock('../session', () => ({ useSession: vi.fn() }));

const fetchMock = vi.fn<typeof fetch>();
const account = { id: 'demo-1', name: 'デモ太郎' };
const receive: ReceiveResponse = {
  state: 'unused',
  repeatable: false,
  work: { id: 'work-001', title: '星の手紙', cover_path: '/covers/work-001.svg' },
  benefit: { id: 'benefit-001', title: '特典音源', description: 'グッズ限定の二曲です。' },
  files: [
    { id: 'file-2', position: 2, title: '星を見上げて', mime_type: 'audio/mpeg' },
    { id: 'file-1', position: 1, title: '夜の手紙', mime_type: 'audio/mpeg' },
  ],
};

beforeEach(async () => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockResolvedValueOnce(Response.json({ account: null, csrf_token: 'csrf' }));
  await getSession({ force: true });
  fetchMock.mockClear();
  fetchMock.mockImplementation(async (_, init) => init?.method === 'POST'
    ? new Promise<Response>(() => {}) : Response.json(receive));
  vi.stubGlobal('fetch', fetchMock);
  vi.mocked(useSession).mockReset();
  vi.mocked(useSession).mockReturnValue({ account: null, loading: false, error: null, refresh: vi.fn() });
});

function respond(data: ReceiveResponse) {
  fetchMock.mockImplementation(async () => Response.json(data));
}

function Destination({ title }: { title: string }) {
  const { pathname } = useLocation();
  const navigationType = useNavigationType();
  return <><h1>{title}</h1><span aria-label="移動先">{pathname}</span><span aria-label="遷移方法">{navigationType}</span></>;
}

function page(path = '/r/demo-token') {
  return (
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/r/:token" element={<ReceivePage />} />
        <Route path="/login" element={<Destination title="ログイン先" />} />
        <Route path="/library/:benefitId" element={<Destination title="再生一覧へ移動済み" />} />
        <Route path="/r/:token/download" element={<Destination title="ダウンロードへ移動済み" />} />
      </Routes>
    </MemoryRouter>
  );
}

async function renderReady() {
  const result = render(page());
  await screen.findByRole('img', { name: receive.work.title + 'の表紙' });
  return result;
}

describe('特典情報と状態別の表示', () => {
  it('例外カードでは繰り返し受取を案内し、一回きりの注意を表示しない', async () => {
    respond({ ...receive, repeatable: true });
    const user = userEvent.setup();
    await renderReady();
    expect(screen.getByRole('alert')).toHaveTextContent('何度でも特典を受け取れます');
    await user.click(screen.getByRole('button', { name: '登録せずにダウンロード' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('同じカードから繰り返し受け取れます');
    expect(screen.queryByText('登録せずに受け取ると、後からアカウントに追加できません。')).not.toBeInTheDocument();
  });

  it.each(['unused', 'account_owned', 'download_retryable', 'claimed'] as const)(
    '%sでも作品名・表紙・特典名・説明・曲一覧を表示する', async (state) => {
      respond({ ...receive, state });
      await renderReady();

      expect(screen.getByRole('heading', { name: receive.work.title })).toBeInTheDocument();
      expect(screen.getByRole('img', { name: receive.work.title + 'の表紙' })).toHaveAttribute('src', receive.work.cover_path);
      expect(screen.getByRole('heading', { name: receive.benefit.title })).toBeInTheDocument();
      expect(screen.getByText(receive.benefit.description)).toBeInTheDocument();
      const files = within(screen.getByRole('list', { name: '特典ファイル' })).getAllByRole('listitem');
      expect(files.map((file) => file.textContent)).toEqual(['夜の手紙', '星を見上げて']);
    },
  );

  it('unused・未ログインでは主ボタンと控えめな副ボタンを表示する', async () => {
    await renderReady();

    expect(screen.getByRole('button', { name: 'ログインしてライブラリに追加' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '登録せずに一回だけダウンロード' })).toBeEnabled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('unused・ログイン済みでは追加中を表示して自動追加する', async () => {
    vi.mocked(useSession).mockReturnValue({ account, loading: false, error: null, refresh: vi.fn() });
    await renderReady();

    expect(screen.getByRole('status')).toHaveTextContent('アカウントに追加しています');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith('/api/r/demo-token/claim/account', expect.objectContaining({ method: 'POST' }));
  });

  it('account_ownedでは受け取り済みの案内と再生一覧へのリンクを表示する', async () => {
    respond({ ...receive, state: 'account_owned' });
    vi.mocked(useSession).mockReturnValue({ account, loading: false, error: null, refresh: vi.fn() });
    await renderReady();

    expect(screen.getByText('この特典は受け取り済みです')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '特典の再生一覧へ' })).toHaveAttribute('href', '/library/benefit-001');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('download_retryableではダウンロードページへのリンクを表示する', async () => {
    respond({ ...receive, state: 'download_retryable' });
    await renderReady();

    expect(screen.getByRole('link', { name: 'ダウンロードページへ' })).toHaveAttribute('href', '/r/demo-token/download');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it.each([null, account])('claimedでは使用済みを表示し、未ログインの場合だけログインを案内する（%j）', async (sessionAccount) => {
    respond({ ...receive, state: 'claimed' });
    vi.mocked(useSession).mockReturnValue({ account: sessionAccount, loading: false, error: null, refresh: vi.fn() });
    await renderReady();

    expect(screen.getByText('この特典はすでに受け取られています')).toBeInTheDocument();
    const message = 'アカウントに追加した方は、ログインするとライブラリで確認できます';
    if (sessionAccount) {
      expect(screen.queryByText(message)).not.toBeInTheDocument();
      expect(screen.queryByRole('link', { name: 'ログイン' })).not.toBeInTheDocument();
    } else {
      expect(screen.getByText(message)).toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'ログイン' })).toHaveAttribute('href', '/login');
    }
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it.each(['unused', 'account_owned', 'download_retryable', 'claimed'] as const)(
    'セッション読み込み中は%sの操作やログイン案内を出さない', async (state) => {
      respond({ ...receive, state });
      vi.mocked(useSession).mockReturnValue({ account: null, loading: true, error: null, refresh: vi.fn() });
      await renderReady();

      expect(screen.queryByRole('button')).not.toBeInTheDocument();
      expect(screen.queryByRole('link')).not.toBeInTheDocument();
      expect(screen.queryByText('この特典は受け取り済みです')).not.toBeInTheDocument();
      expect(Boolean(screen.queryByText('この特典はすでに受け取られています'))).toBe(state === 'claimed');
      expect(screen.queryByText('アカウントに追加しています')).not.toBeInTheDocument();
    },
  );

  it('ログイン状態の取得後に、未ログインの選択肢を挟まず追加中を表示する', async () => {
    vi.mocked(useSession).mockReturnValue({ account: null, loading: true, error: null, refresh: vi.fn() });
    const { rerender } = await renderReady();
    vi.mocked(useSession).mockReturnValue({ account, loading: false, error: null, refresh: vi.fn() });
    rerender(page());

    expect(screen.getByRole('status')).toHaveTextContent('アカウントに追加しています');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('セッション取得失敗時も未ログインの受け取り操作を出さない', async () => {
    vi.mocked(useSession).mockReturnValue({
      account: null, loading: false, error: new ApiError(0, 'network_error', '通信失敗'), refresh: vi.fn(),
    });
    await renderReady();

    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('セッション取得失敗でもclaimedの本文を表示する', async () => {
    respond({ ...receive, state: 'claimed' });
    vi.mocked(useSession).mockReturnValue({ account: null, loading: false, error: new ApiError(0, 'network_error', '通信失敗'), refresh: vi.fn() });
    await renderReady();
    expect(screen.getByText('この特典はすでに受け取られています')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'ログイン' })).not.toBeInTheDocument();
  });

  it('ファイルが空でも特典情報を表示する', async () => {
    respond({ ...receive, files: [] });
    await renderReady();

    expect(screen.getByText('ファイルはありません')).toBeInTheDocument();
  });

  it('トークンをエンコードしてAPI取得し、ダウンロードリンクにも使う', async () => {
    respond({ ...receive, state: 'download_retryable' });
    render(page('/r/token%2Fwith%20space%3F'));
    await screen.findByRole('img', { name: receive.work.title + 'の表紙' });

    expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/r/token%2Fwith%20space%3F', expect.objectContaining({
      method: 'GET', signal: expect.any(AbortSignal),
    }));
    expect(screen.getByRole('link', { name: 'ダウンロードページへ' })).toHaveAttribute('href', '/r/token%2Fwith%20space%3F/download');
  });
});

describe('読み込みとエラー', () => {
  it('API取得中は読み込み中を表示する', () => {
    fetchMock.mockReturnValueOnce(new Promise(() => {}));
    render(page());

    expect(screen.getByRole('status')).toHaveTextContent('特典を読み込み中です');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('404のAPIエラーだけを対象なしとして表示する', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({
      error: { code: 'entitlement_not_found', message: '存在しないか無効です' },
    }, { status: 404 }));
    render(page());

    expect(await screen.findByText('この特典は見つかりません')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '再試行' })).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it.each(['HTMLの404', 'HTMLの200', '通信失敗', 'サーバーエラー', '別の404'])(
    '%sを通信失敗として表示し、再試行で新しく取得する', async (failure) => {
      if (failure === '通信失敗') {
        fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
      } else if (failure === '別の404') {
        fetchMock.mockResolvedValueOnce(Response.json({ error: { code: 'file_not_found', message: 'エラー' } }, { status: 404 }));
      } else if (failure === 'サーバーエラー') {
        fetchMock.mockResolvedValueOnce(Response.json({ error: { code: 'server_error', message: 'エラー' } }, { status: 500 }));
      } else {
        fetchMock.mockResolvedValueOnce(new Response('<html>SPA</html>', { status: failure === 'HTMLの404' ? 404 : 200 }));
      }
      let resolveRetry!: (response: Response) => void;
      fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveRetry = resolve; }));
      const user = userEvent.setup();
      render(page());

      expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
      expect(screen.queryByText('この特典は見つかりません')).not.toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const firstSignal = fetchMock.mock.calls[0][1]?.signal;

      await user.click(screen.getByRole('button', { name: '再試行' }));
      expect(screen.getByRole('status')).toHaveTextContent('特典を読み込み中です');
      expect(screen.queryByRole('button', { name: '再試行' })).not.toBeInTheDocument();
      expect(firstSignal?.aborted).toBe(true);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(fetchMock.mock.calls[1][1]?.signal).not.toBe(firstSignal);

      resolveRetry(Response.json(receive));
      expect(await screen.findByRole('img', { name: receive.work.title + 'の表紙' })).toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    },
  );

  it.each([null, { account: null, csrf_token: 'csrf' }, { ...receive, files: [null] }])(
    '不正な成功JSONを通信失敗として扱う（%j）', async (data) => {
      fetchMock.mockResolvedValueOnce(Response.json(data));
      render(page());

      expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
      expect(screen.getByRole('button', { name: '再試行' })).toBeEnabled();
      expect(screen.queryByText('この特典は見つかりません')).not.toBeInTheDocument();
    },
  );
});

describe('ログイン準備と受取操作', () => {
  it('prepareにトークンを送り、成功後にログインへ移動する', async () => {
    const user = userEvent.setup();
    await renderReady();
    let resolvePrepare!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolvePrepare = resolve; }));
    const login = screen.getByRole('button', { name: 'ログインしてライブラリに追加' });
    await user.click(login);
    expect(login).toBeDisabled();
    expect(screen.getByRole('button', { name: '登録せずに一回だけダウンロード' })).toBeDisabled();
    login.click();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenLastCalledWith('/api/auth/prepare', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ token: 'demo-token' }),
      headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf' }),
    }));
    await act(async () => { resolvePrepare(Response.json({ return_to: '/r/demo-token' })); });
    expect(await screen.findByRole('heading', { name: 'ログイン先' })).toBeInTheDocument();
  });

  it('注意事項を表示し、キャンセルとEscapeではPOSTを送らない', async () => {
    const user = userEvent.setup();
    await renderReady();
    await user.click(screen.getByRole('button', { name: '登録せずに一回だけダウンロード' }));
    const dialog = screen.getByRole('dialog', { name: '登録せずにダウンロードしますか？' });
    expect(dialog).toHaveAccessibleDescription('登録せずに受け取ると、後からアカウントに追加できません。 再ダウンロードは、同じブラウザで短時間に限られます。');
    await user.click(within(dialog).getByRole('button', { name: 'キャンセル' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: '登録せずに一回だけダウンロード' }));
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('確定中は二重操作と閉じる操作を防ぎ、成功後にダウンロードへ移動する', async () => {
    const user = userEvent.setup();
    await renderReady();
    let resolveDownload!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveDownload = resolve; }));
    await user.click(screen.getByRole('button', { name: '登録せずに一回だけダウンロード' }));
    const confirm = screen.getByRole('button', { name: 'ダウンロードする' });
    await user.click(confirm);
    expect(confirm).toBeDisabled();
    expect(screen.getByRole('button', { name: 'キャンセル' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'ログインしてライブラリに追加', hidden: true })).toBeDisabled();
    confirm.click();
    await user.keyboard('{Escape}');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenLastCalledWith('/api/r/demo-token/claim/download', expect.objectContaining({ method: 'POST' }));
    await act(async () => { resolveDownload(Response.json({ expires_at: '2026-10-17T01:23:45Z', remaining_seconds: 600 })); });
    expect(await screen.findByRole('heading', { name: 'ダウンロードへ移動済み' })).toBeInTheDocument();
  });

  it.each(['通信失敗', '409', '不正な成功応答'])('ダウンロードの%sでダイアログを閉じ、状態を取り直す', async (failure) => {
    const user = userEvent.setup();
    await renderReady();
    if (failure === '通信失敗') fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    else if (failure === '409') fetchMock.mockResolvedValueOnce(Response.json({ error: { code: 'already_claimed', message: '受け取り済み' } }, { status: 409 }));
    else fetchMock.mockResolvedValueOnce(Response.json(null));
    let resolveReload!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveReload = resolve; }));
    await user.click(screen.getByRole('button', { name: '登録せずに一回だけダウンロード' }));
    await user.click(screen.getByRole('button', { name: 'ダウンロードする' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByRole('status')).toHaveTextContent('特典を読み込み中です');
    expect(screen.queryByText('操作を完了できませんでした。最新の状態を表示しています')).not.toBeInTheDocument();
    await act(async () => { resolveReload(Response.json({ ...receive, state: 'download_retryable' })); });
    expect(await screen.findByRole('link', { name: 'ダウンロードページへ' })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('操作を完了できませんでした。最新の状態を表示しています');
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
    expect(fetchMock).toHaveBeenLastCalledWith('/api/r/demo-token', expect.objectContaining({ method: 'GET' }));
  });

  it('ログイン準備の失敗後も状態を取り直す', async () => {
    const user = userEvent.setup();
    await renderReady();
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    fetchMock.mockResolvedValueOnce(Response.json({ ...receive, state: 'claimed' }));
    await user.click(screen.getByRole('button', { name: 'ログインしてライブラリに追加' }));
    expect(await screen.findByText('この特典はすでに受け取られています')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

describe('アカウントへの自動追加', () => {
  beforeEach(() => {
    vi.mocked(useSession).mockReturnValue({ account, loading: false, error: null, refresh: vi.fn() });
  });

  it('StrictModeでもPOSTは1回で、成功後に再生一覧へ移動する', async () => {
    let resolveClaim!: (response: Response) => void;
    fetchMock.mockImplementation(async (_, init) => init?.method === 'POST'
      ? new Promise((resolve) => { resolveClaim = resolve; }) : Response.json(receive));
    render(<StrictMode>{page()}</StrictMode>);
    await screen.findByText('アカウントに追加しています');
    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1));
    await act(async () => { resolveClaim(Response.json({ benefit_id: 'benefit-001' })); });
    expect(await screen.findByRole('heading', { name: '再生一覧へ移動済み' })).toBeInTheDocument();
    expect(screen.getByLabelText('移動先')).toHaveTextContent('/library/benefit-001');
    expect(screen.getByLabelText('遷移方法')).toHaveTextContent('REPLACE');
  });

  it.each(['成功', '失敗'])('401 login_requiredでは状態とセッションを取り直す（refresh%s）', async (outcome) => {
    let resolveRefresh!: (value: null) => void;
    let rejectRefresh!: (error: ApiError) => void;
    const refresh = vi.fn(() => new Promise<null>((resolve, reject) => {
      resolveRefresh = resolve;
      rejectRefresh = reject;
    }));
    vi.mocked(useSession).mockReturnValue({ account, loading: false, error: null, refresh });
    fetchMock.mockResolvedValueOnce(Response.json(receive))
      .mockResolvedValueOnce(Response.json({ error: { code: 'login_required', message: 'ログインが必要です' } }, { status: 401 }))
      .mockResolvedValueOnce(Response.json(receive));
    render(page());
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('button', { name: 'ライブラリに追加' })).toBeDisabled();
    await act(async () => {
      if (outcome === '成功') {
        vi.mocked(useSession).mockReturnValue({ account: null, loading: false, error: null, refresh });
        resolveRefresh(null);
      } else {
        const error = new ApiError(0, 'network_error', '通信に失敗しました');
        vi.mocked(useSession).mockReturnValue({ account, loading: false, error, refresh });
        rejectRefresh(error);
      }
    });
    if (outcome === '成功') {
      expect(await screen.findByRole('button', { name: 'ログインしてライブラリに追加' })).toBeEnabled();
      expect(screen.getByRole('button', { name: '登録せずに一回だけダウンロード' })).toBeEnabled();
    } else {
      expect(screen.queryByRole('button')).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: 'ライブラリに追加' })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock).toHaveBeenLastCalledWith('/api/r/demo-token', expect.objectContaining({ method: 'GET' }));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  });

  it.each(['通信失敗', '409'])('自動追加の%s後にunusedなら、手動操作だけで追加し送信中は無効にする', async (failure) => {
    fetchMock.mockResolvedValueOnce(Response.json(receive));
    if (failure === '通信失敗') fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    else fetchMock.mockResolvedValueOnce(Response.json({ error: { code: 'already_claimed', message: '受け取り済み' } }, { status: 409 }));
    fetchMock.mockResolvedValueOnce(Response.json(receive));
    const user = userEvent.setup();
    const { rerender } = render(page());
    const button = await screen.findByRole('button', { name: 'ライブラリに追加' });
    expect(button).toBeEnabled();
    rerender(page());
    expect(fetchMock).toHaveBeenCalledTimes(3);
    let resolveClaim!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveClaim = resolve; }));
    await user.click(button);
    expect(button).toBeDisabled();
    button.click();
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock).toHaveBeenLastCalledWith('/api/r/demo-token/claim/account', expect.objectContaining({ method: 'POST' }));
    await act(async () => { resolveClaim(Response.json({ benefit_id: 'benefit-001' })); });
    expect(await screen.findByRole('heading', { name: '再生一覧へ移動済み' })).toBeInTheDocument();
    expect(screen.getByLabelText('遷移方法')).toHaveTextContent('REPLACE');
  });

  it.each(['claimed', 'account_owned', 'unused'] as const)('409後に%sを取得して表示し、unusedでも再送しない', async (state) => {
    fetchMock.mockResolvedValueOnce(Response.json(receive))
      .mockResolvedValueOnce(Response.json({ error: { code: 'already_claimed', message: '受け取り済み' } }, { status: 409 }))
      .mockResolvedValueOnce(Response.json({ ...receive, state }));
    const { rerender } = render(page());
    const message = state === 'claimed' ? 'この特典はすでに受け取られています'
      : state === 'account_owned' ? 'この特典は受け取り済みです' : 'この特典はまだ受け取られていません';
    expect(await screen.findByText(message)).toBeInTheDocument();
    if (state === 'account_owned') expect(screen.getByRole('link', { name: '特典の再生一覧へ' })).toHaveAttribute('href', '/library/benefit-001');
    if (state === 'unused') expect(screen.getByRole('button', { name: 'ライブラリに追加' })).toBeEnabled();
    else expect(screen.queryByRole('button', { name: 'ライブラリに追加' })).not.toBeInTheDocument();
    rerender(page());
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
    expect(screen.queryByText('アカウントに追加しています')).not.toBeInTheDocument();
  });

  it('自動追加と状態再取得が通信失敗しても、GETの再試行ではPOSTを再送しない', async () => {
    fetchMock.mockResolvedValueOnce(Response.json(receive))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(Response.json(receive));
    const user = userEvent.setup();
    render(page());
    expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByText('この特典はまだ受け取られていません')).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  });

  it('別のトークンでは一度ずつ送信し、戻ってきたトークンでは再送しない', async () => {
    let navigate!: ReturnType<typeof useNavigate>;
    function Navigation() {
      navigate = useNavigate();
      return <Routes><Route path="/r/:token" element={<ReceivePage />} /></Routes>;
    }
    fetchMock.mockImplementation(async (_, init) => init?.method === 'POST'
      ? Response.json({ error: { code: 'already_claimed', message: '受け取り済み' } }, { status: 409 })
      : Response.json(receive));
    render(<MemoryRouter initialEntries={['/r/first-token']}><Navigation /></MemoryRouter>);
    await screen.findByText('この特典はまだ受け取られていません');
    await act(async () => { await navigate('/r/second-token'); });
    await screen.findByText('この特典はまだ受け取られていません');
    await act(async () => { await navigate('/r/first-token'); });
    await screen.findByRole('img', { name: receive.work.title + 'の表紙' });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST').map(([path]) => path)).toEqual([
      '/api/r/first-token/claim/account', '/api/r/second-token/claim/account',
    ]);
  });
});

describe('取得要求の中止', () => {
  it('アンマウント時に要求を中止する', async () => {
    let signal!: AbortSignal;
    let rejectRequest!: (error: unknown) => void;
    fetchMock.mockImplementationOnce((_, init) => {
      signal = init!.signal!;
      return new Promise((_, reject) => { rejectRequest = reject; });
    });
    const { unmount } = render(page());
    expect(signal.aborted).toBe(false);

    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { rejectRequest(signal.reason); });
  });

  it('StrictModeで中止された古い要求の失敗をエラー表示しない', async () => {
    let rejectRequest!: (error: unknown) => void;
    fetchMock.mockReturnValueOnce(new Promise((_, reject) => { rejectRequest = reject; }));
    render(<StrictMode>{page()}</StrictMode>);
    await screen.findByRole('img', { name: receive.work.title + 'の表紙' });

    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
    await act(async () => { rejectRequest('stop'); });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: receive.work.title + 'の表紙' })).toBeInTheDocument();
  });

  it('トークン変更時に古い要求を中止し、遅い応答で新しい特典を上書きしない', async () => {
    let navigate!: ReturnType<typeof useNavigate>;
    function Navigation() {
      navigate = useNavigate();
      return <Routes><Route path="/r/:token" element={<ReceivePage />} /></Routes>;
    }
    let resolveOld!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }));
    respond({ ...receive, work: { ...receive.work, title: '次の作品' } });
    render(<MemoryRouter initialEntries={['/r/old-token']}><Navigation /></MemoryRouter>);
    const oldSignal = fetchMock.mock.calls[0][1]?.signal;

    await act(async () => { await navigate('/r/new-token'); });
    expect(oldSignal?.aborted).toBe(true);
    expect(await screen.findByRole('img', { name: '次の作品の表紙' })).toBeInTheDocument();
    await act(async () => { resolveOld(Response.json(receive)); });
    expect(screen.queryByRole('img', { name: receive.work.title + 'の表紙' })).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: '次の作品の表紙' })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenLastCalledWith('/api/r/new-token', expect.anything());
  });
});
