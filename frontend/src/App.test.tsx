import { StrictMode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async () => Response.json({
    account: { id: 'demo-1', name: 'デモ太郎' },
    csrf_token: 'csrf-demo',
  }));
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
    ['/r/demo-token', '特典ページ'],
    ['/r/demo-token/download', 'ダウンロード'],
    ['/login', 'ログイン'],
    ['/library', 'ライブラリ'],
    ['/library/benefit-1', '特典の再生一覧'],
  ])('各ルートの仮画面を表示する（%s）', async (path, title) => {
    renderAt(path);

    expect(screen.getByRole('heading', { name: title })).toBeInTheDocument();
    expect(screen.getByText('準備中')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/session', expect.objectContaining({
      method: 'GET', credentials: 'same-origin',
    }));
  });
});

describe('セッション共有', () => {
  it('取得したアカウント名をAppBarとライブラリに表示する', async () => {
    renderAt('/library');

    expect(await screen.findByText('デモ太郎さんのライブラリ')).toBeInTheDocument();
    expect(within(screen.getByRole('banner')).getByText('デモ太郎')).toBeInTheDocument();
  });

  it('読み込み中はログイン案内を先に表示しない', async () => {
    let resolveResponse!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveResponse = resolve; }));
    renderAt('/library');

    expect(screen.getByRole('status')).toHaveTextContent('ログイン状態を確認中です');
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'ログイン' })).not.toBeInTheDocument();

    resolveResponse(Response.json({ account: null, csrf_token: 'csrf-demo' }));
    expect(await screen.findByRole('link', { name: 'ログイン' })).toHaveAttribute('href', '/login');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('未ログインならライブラリにログイン案内を表示する', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ account: null, csrf_token: 'csrf-demo' }));
    renderAt('/library');

    expect(await screen.findByRole('link', { name: 'ログイン' })).toBeInTheDocument();
    expect(within(screen.getByRole('banner')).queryByText('デモ太郎')).not.toBeInTheDocument();
  });

  it('セッション取得失敗を表示し、再試行でアカウントを取得できる', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    renderAt('/library');

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('ログイン状態を確認できませんでした');
    expect(alert).toHaveTextContent('通信に失敗しました');
    expect(screen.queryByRole('link', { name: 'ログイン' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByText('デモ太郎さんのライブラリ')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('初回取得失敗後の再試行中もログイン案内を表示しない', async () => {
    let resolveResponse!: (response: Response) => void;
    fetchMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockReturnValueOnce(new Promise((resolve) => { resolveResponse = resolve; }));
    const user = userEvent.setup();
    renderAt('/library');
    await screen.findByRole('alert');

    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(screen.getByRole('status')).toHaveTextContent('ログイン状態を確認中です');
    expect(screen.queryByRole('link', { name: 'ログイン' })).not.toBeInTheDocument();

    resolveResponse(Response.json({ account: { id: 'demo-1', name: 'デモ太郎' }, csrf_token: 'csrf-demo' }));
    expect(await screen.findByText('デモ太郎さんのライブラリ')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('StrictModeでもセッションの初回取得は1回だけで、アカウントを表示する', async () => {
    render(<StrictMode><MemoryRouter initialEntries={['/library']}><App /></MemoryRouter></StrictMode>);

    expect(await screen.findByText('デモ太郎さんのライブラリ')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('画面からの再試行が失敗しても、エラーを表示して再び試せる', async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    renderAt('/library');
    await screen.findByRole('alert');

    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByText('デモ太郎さんのライブラリ')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
