import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getSession } from '../api/client';
import { useSession } from '../session';
import { LoginPage } from './LoginPage';

vi.mock('../session', () => ({ useSession: vi.fn() }));

const fetchMock = vi.fn<typeof fetch>();
const account = { id: 'demo-1', name: 'デモ太郎' };
const accounts = [account, { id: 'demo-2', name: 'デモ花子' }];
const refresh = vi.fn();

beforeEach(async () => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockResolvedValueOnce(Response.json({ account: null, csrf_token: 'csrf' }));
  await getSession({ force: true });
  fetchMock.mockClear();
  fetchMock.mockImplementation(async () => Response.json({ accounts }));
  refresh.mockReset().mockResolvedValue(account);
  vi.mocked(useSession).mockReset().mockReturnValue({ account: null, loading: false, error: null, refresh });
});

function Location() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  return <><div aria-label="現在のパス">{pathname}</div><button onClick={() => { void navigate(-1); }}>戻る</button></>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/before', '/login']} initialIndex={1}>
      <Location />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<h1>移動先</h1>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('デモログイン', () => {
  it('一覧取得中の表示後にアカウントの選択ボタンを表示する', async () => {
    let resolveAccounts!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveAccounts = resolve; }));
    renderPage();
    expect(screen.getByRole('status')).toHaveTextContent('アカウントを読み込み中です');
    await act(async () => { resolveAccounts(Response.json({ accounts })); });
    expect(await screen.findByRole('button', { name: 'デモ太郎' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'デモ花子' })).toBeEnabled();
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/auth/accounts', expect.objectContaining({ method: 'GET' }));
  });

  it('選択したIDを送り、refreshの完了後にreturn_toへreplaceで移動する', async () => {
    const user = userEvent.setup();
    renderPage();
    const button = await screen.findByRole('button', { name: 'デモ太郎' });
    let resolveLogin!: (response: Response) => void;
    let resolveRefresh!: (value: typeof account) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveLogin = resolve; }));
    refresh.mockReturnValueOnce(new Promise((resolve) => { resolveRefresh = resolve; }));
    await user.click(button);
    expect(button).toBeDisabled();
    expect(screen.getByRole('button', { name: 'デモ花子' })).toBeDisabled();
    button.click();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(refresh).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenLastCalledWith('/api/auth/login', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ account_id: 'demo-1' }),
    }));
    await act(async () => { resolveLogin(Response.json({ account, csrf_token: 'new-csrf', return_to: '/r/demo-token' })); });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('現在のパス')).toHaveTextContent('/login');
    expect(button).toBeDisabled();
    await act(async () => { resolveRefresh(account); });
    await waitFor(() => expect(screen.getByLabelText('現在のパス')).toHaveTextContent('/r/demo-token'));
    await user.click(screen.getByRole('button', { name: '戻る' }));
    expect(screen.getByLabelText('現在のパス')).toHaveTextContent('/before');
  });

  it.each(['https://example.com', '//example.com/path', '/\\example.com', '/library\\benefit-001', 'library', '', null])(
    '安全なパス以外のreturn_toはライブラリへ移動する（%j）', async (returnTo) => {
      const user = userEvent.setup();
      renderPage();
      const button = await screen.findByRole('button', { name: 'デモ太郎' });
      fetchMock.mockResolvedValueOnce(Response.json({ account, csrf_token: 'new-csrf', return_to: returnTo }));
      await user.click(button);
      await waitFor(() => expect(screen.getByLabelText('現在のパス')).toHaveTextContent('/library'));
      expect(refresh).toHaveBeenCalledTimes(1);
    },
  );

  it.each(['POST失敗', 'セッション再取得失敗', 'セッション未ログイン'])('%sでは遷移せず、自動で再送しない', async (failure) => {
    const user = userEvent.setup();
    renderPage();
    const button = await screen.findByRole('button', { name: 'デモ太郎' });
    if (failure === 'POST失敗') fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    else fetchMock.mockResolvedValueOnce(Response.json({ account, csrf_token: 'new-csrf', return_to: '/r/demo-token' }));
    if (failure === 'セッション再取得失敗') refresh.mockRejectedValueOnce(new Error('通信失敗'));
    if (failure === 'セッション未ログイン') refresh.mockResolvedValueOnce(null);
    await user.click(button);
    expect(await screen.findByRole('alert')).toHaveTextContent('ログインを完了できませんでした');
    expect(button).toBeEnabled();
    expect(screen.getByLabelText('現在のパス')).toHaveTextContent('/login');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('ログイン済みならライブラリへのリンクとログアウト案内を表示する', () => {
    vi.mocked(useSession).mockReturnValue({ account, loading: false, error: null, refresh });
    renderPage();
    expect(screen.getByText('デモ太郎さんとしてログイン済みです')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'ライブラリへ' })).toHaveAttribute('href', '/library');
    expect(screen.getByText(/別のアカウントを選ぶには/)).toHaveTextContent('ログアウト');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('アカウントが空なら対象なしを表示する', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ accounts: [] }));
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('選択できるアカウントはありません');
  });

  it.each(['通信失敗', '不正な成功応答'])('一覧の%sを表示し、再試行で取得する', async (failure) => {
    if (failure === '通信失敗') fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    else fetchMock.mockResolvedValueOnce(Response.json({ accounts: [null] }));
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByRole('alert')).toHaveTextContent('アカウント一覧を取得できませんでした');
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByRole('button', { name: 'デモ太郎' })).toBeEnabled();
  });
});
