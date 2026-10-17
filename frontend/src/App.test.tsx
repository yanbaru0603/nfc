import { StrictMode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (path) => {
    const work = { id: 'work-001', title: '星の手紙', cover_path: '/covers/work-001.svg' };
    const benefit = { id: 'benefit-1', title: '特典音源', description: 'グッズ限定の音源です。' };
    if (path === '/api/library') return Response.json({ items: [{ work, benefit }] });
    if (path === '/api/library/benefit-1') return Response.json({ work, benefit, files: [] });
    if (path === '/api/r/demo-token/download') return Response.json({
      work, benefit, files: [], expires_at: '2026-10-17T01:23:45Z', remaining_seconds: 600,
    });
    if (path === '/api/auth/accounts') return Response.json({ accounts: [{ id: 'demo-1', name: 'デモ太郎' }] });
    return Response.json({ account: { id: 'demo-1', name: 'デモ太郎' }, csrf_token: 'csrf-demo' });
  });
  vi.stubGlobal('fetch', fetchMock);
});

function renderAt(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
}

describe('画面ルーティング', () => {
  it.each(['/', '/not-found', '/r/token/unknown', '/library/benefit-1/unknown'])(
    '存在しないパスでは「ページが見つかりません」を表示する（%s）', async (path) => {
      renderAt(path);

      expect(screen.getByRole('heading', { name: 'ページが見つかりません' })).toBeInTheDocument();
      await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    },
  );

  it.each([
    ['/r/demo-token/download', 'ダウンロード'],
    ['/login', 'ログイン'],
    ['/library', 'ライブラリ'],
    ['/library/benefit-1', '特典の再生一覧'],
  ])('各ルートの画面を表示する（%s）', async (path, title) => {
    renderAt(path);

    expect(screen.getByRole('heading', { name: title })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith('/api/session', expect.objectContaining({
      method: 'GET', credentials: 'same-origin',
    }));
  });

  it('特典ページのルートではAPIから作品を取得して表示する', async () => {
    fetchMock.mockImplementation(async (path) => path === '/api/r/demo-token'
      ? Response.json({
        state: 'claimed',
        repeatable: false,
        work: { id: 'work-001', title: '星の手紙', cover_path: '/covers/work-001.svg' },
        benefit: { id: 'benefit-001', title: '特典音源', description: 'グッズ限定の音源です。' },
        files: [],
      })
      : Response.json({ account: { id: 'demo-1', name: 'デモ太郎' }, csrf_token: 'csrf-demo' }));
    renderAt('/r/demo-token');

    expect(screen.getByRole('heading', { name: '特典ページ' })).toBeInTheDocument();
    expect(await screen.findByRole('img', { name: '星の手紙の表紙' })).toHaveAttribute('src', '/covers/work-001.svg');
    expect(await screen.findByText('この特典はすでに受け取られています')).toBeInTheDocument();
    expect(screen.queryByText('準備中')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledWith('/api/r/demo-token', expect.objectContaining({ method: 'GET' }));
    expect(fetchMock).toHaveBeenCalledWith('/api/session', expect.objectContaining({ method: 'GET' }));
  });
});

describe('セッション共有', () => {
  it('取得したアカウント名をAppBarとログイン画面に表示する', async () => {
    renderAt('/login');

    expect(await screen.findByText('デモ太郎さんとしてログイン済みです')).toBeInTheDocument();
    expect(within(screen.getByRole('banner')).getByText('デモ太郎')).toBeInTheDocument();
  });

  it('読み込み中はログイン案内を先に表示しない', async () => {
    let resolveResponse!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveResponse = resolve; }));
    renderAt('/login');

    expect(screen.getByRole('status')).toHaveTextContent('ログイン状態を確認中です');
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'ログイン' })).not.toBeInTheDocument();

    resolveResponse(Response.json({ account: null, csrf_token: 'csrf-demo' }));
    expect(await screen.findByRole('link', { name: 'ログイン' })).toHaveAttribute('href', '/login');
    // LoginPageのアカウント読み込み中の表示は残り得るので、セッション確認の表示だけを確かめる
    expect(screen.queryByText('ログイン状態を確認中です')).not.toBeInTheDocument();
  });

  it('未ログインならAppBarにログインへのリンクを表示する', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ account: null, csrf_token: 'csrf-demo' }));
    renderAt('/login');

    expect(await screen.findByRole('link', { name: 'ログイン' })).toBeInTheDocument();
    expect(within(screen.getByRole('banner')).queryByText('デモ太郎')).not.toBeInTheDocument();
  });

  it('セッション取得失敗を表示し、再試行でアカウントを取得できる', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    renderAt('/login');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('ログイン状態を確認できませんでした');
    expect(alert).toHaveTextContent('通信に失敗しました');
    expect(screen.queryByRole('link', { name: 'ログイン' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByText('デモ太郎さんとしてログイン済みです')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('初回取得失敗後の再試行中もログイン案内を表示しない', async () => {
    let resolveResponse!: (response: Response) => void;
    fetchMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockReturnValueOnce(new Promise((resolve) => { resolveResponse = resolve; }));
    const user = userEvent.setup();
    renderAt('/login');
    await screen.findByRole('alert');

    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(screen.getByRole('status')).toHaveTextContent('ログイン状態を確認中です');
    expect(screen.queryByRole('link', { name: 'ログイン' })).not.toBeInTheDocument();

    resolveResponse(Response.json({ account: { id: 'demo-1', name: 'デモ太郎' }, csrf_token: 'csrf-demo' }));
    expect(await screen.findByText('デモ太郎さんとしてログイン済みです')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('StrictModeでもセッションの初回取得は1回だけで、アカウントを表示する', async () => {
    render(<StrictMode><MemoryRouter initialEntries={['/login']}><App /></MemoryRouter></StrictMode>);

    expect(await screen.findByText('デモ太郎さんとしてログイン済みです')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('画面からの再試行が失敗しても、エラーを表示して再び試せる', async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    renderAt('/login');
    await screen.findByRole('alert');

    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByText('デモ太郎さんとしてログイン済みです')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

describe('ログイン・受取とセッション更新の接続', () => {
  it('未ログインの受取からログインし、更新したセッションで一度だけ自動追加する', async () => {
    let loggedIn = false;
    const account = { id: 'demo-1', name: 'デモ太郎' };
    const work = { id: 'work-001', title: '星の手紙', cover_path: '/covers/work-001.svg' };
    const benefit = { id: 'benefit-1', title: '特典音源', description: 'グッズ限定の音源です。' };
    fetchMock.mockImplementation(async (path) => {
      if (path === '/api/session') return Response.json({
        account: loggedIn ? account : null, csrf_token: loggedIn ? 'csrf-refreshed' : 'csrf-anonymous',
      });
      if (path === '/api/r/demo-token') return Response.json({ state: 'unused', repeatable: false, work, benefit, files: [] });
      if (path === '/api/auth/prepare') return Response.json({ return_to: '/r/demo-token' });
      if (path === '/api/auth/accounts') return Response.json({ accounts: [account] });
      if (path === '/api/auth/login') {
        loggedIn = true;
        return Response.json({ account, return_to: '/r/demo-token', csrf_token: 'csrf-login' });
      }
      if (path === '/api/r/demo-token/claim/account') return Response.json({ benefit_id: benefit.id });
      return Response.json({ work, benefit, files: [] });
    });
    const user = userEvent.setup();
    renderAt('/r/demo-token');
    await user.click(await screen.findByRole('button', { name: 'ログインしてライブラリに追加' }));
    await user.click(await screen.findByRole('button', { name: 'デモ太郎' }));
    expect(await screen.findByRole('heading', { name: '特典の再生一覧' })).toBeInTheDocument();
    expect(within(screen.getByRole('banner')).getByText('デモ太郎')).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(2);
    expect(fetchMock.mock.calls.filter(([path]) => path === '/api/r/demo-token/claim/account')).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith('/api/r/demo-token/claim/account', expect.objectContaining({
      method: 'POST', headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-refreshed' }),
    }));
  });

  it('ログアウト後は共有セッションを更新し、ログインへのリンクとアカウント一覧に切り替える', async () => {
    const user = userEvent.setup();
    renderAt('/login');
    const logout = await screen.findByRole('button', { name: 'ログアウト' });
    fetchMock.mockResolvedValueOnce(Response.json({ csrf_token: 'csrf-logout' }))
      .mockResolvedValueOnce(Response.json({ account: null, csrf_token: 'csrf-anonymous' }));
    await user.click(logout);
    expect(await screen.findByRole('button', { name: 'デモ太郎' })).toBeEnabled();
    expect(within(screen.getByRole('banner')).getByRole('link', { name: 'ログイン' })).toHaveAttribute('href', '/login');
    expect(screen.queryByRole('button', { name: 'ログアウト' })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/logout', expect.objectContaining({ method: 'POST' }));
    expect(fetchMock.mock.calls.filter(([path]) => path === '/api/session')).toHaveLength(2);
  });
});
