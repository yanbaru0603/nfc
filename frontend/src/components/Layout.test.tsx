import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, getSession } from '../api/client';
import { useSession } from '../session';
import { Layout } from './Layout';

vi.mock('../session', () => ({ useSession: vi.fn() }));

const fetchMock = vi.fn<typeof fetch>();

beforeEach(async () => {
  vi.mocked(useSession).mockReset();
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockResolvedValueOnce(Response.json({ account: null, csrf_token: 'csrf' }));
  await getSession({ force: true });
  fetchMock.mockClear();
});

function Location() {
  return <output aria-label="現在のパス">{useLocation().pathname}</output>;
}

function renderLayout(path = '/r/demo-token') {
  return render(<MemoryRouter initialEntries={[path]}><Layout>本文<Location /></Layout></MemoryRouter>);
}

describe('セッションの再試行ボタン', () => {
  it.each(['成功', '失敗'])('再試行中は無効になり、%s後に再び有効になる', async (outcome) => {
    let resolveRefresh!: (account: null) => void;
    let rejectRefresh!: (error: ApiError) => void;
    const error = new ApiError(0, 'network_error', '通信に失敗しました');
    const refresh = vi.fn(() => new Promise<null>((resolve, reject) => {
      resolveRefresh = resolve;
      rejectRefresh = reject;
    }));
    vi.mocked(useSession).mockReturnValue({ account: null, loading: false, error, refresh });
    const user = userEvent.setup();
    renderLayout();
    const button = screen.getByRole('button', { name: '再試行' });

    await user.click(button);
    expect(button).toBeDisabled();
    button.click();
    expect(refresh).toHaveBeenCalledTimes(1);

    if (outcome === '成功') {
      resolveRefresh(null);
    } else {
      rejectRefresh(error);
    }
    await waitFor(() => expect(button).toBeEnabled());
  });
});

describe('ログインとログアウトの操作', () => {
  it('未ログインならログインへのリンクを表示する', () => {
    vi.mocked(useSession).mockReturnValue({ account: null, loading: false, error: null, refresh: vi.fn() });
    renderLayout();
    expect(screen.getByRole('link', { name: 'ログイン' })).toHaveAttribute('href', '/login');
    expect(screen.queryByRole('button', { name: 'ログアウト' })).not.toBeInTheDocument();
  });

  it.each(['/r/demo-token', '/library', '/library/benefit-001'])(
    'ログアウト後にrefreshし、ライブラリ系の画面だけログインへ移動する（%s）', async (path) => {
      let resolveLogout!: (response: Response) => void;
      let resolveRefresh!: (value: null) => void;
      fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveLogout = resolve; }));
      const refresh = vi.fn(() => new Promise<null>((resolve) => { resolveRefresh = resolve; }));
      vi.mocked(useSession).mockReturnValue({ account: { id: 'demo-1', name: 'デモ太郎' }, loading: false, error: null, refresh });
      const user = userEvent.setup();
      renderLayout(path);
      expect(screen.getByText('デモ太郎')).toBeInTheDocument();
      const button = screen.getByRole('button', { name: 'ログアウト' });
      await user.click(button);
      expect(button).toBeDisabled();
      button.click();
      expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/auth/logout', expect.objectContaining({ method: 'POST' }));
      expect(refresh).not.toHaveBeenCalled();
      resolveLogout(Response.json({ csrf_token: 'logout-csrf' }));
      await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
      expect(screen.getByLabelText('現在のパス')).toHaveTextContent(path);
      resolveRefresh(null);
      await waitFor(() => expect(button).toBeEnabled());
      expect(screen.getByLabelText('現在のパス')).toHaveTextContent(path.startsWith('/library') ? '/login' : path);
    },
  );

  it('ログアウトの401ではセッションを更新し、エラーを出さず未ログイン表示に切り替える', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ error: { code: 'login_required', message: 'ログインが必要です' } }, { status: 401 }));
    const refresh = vi.fn(async () => {
      vi.mocked(useSession).mockReturnValue({ account: null, loading: false, error: null, refresh });
      return null;
    });
    vi.mocked(useSession).mockReturnValue({ account: { id: 'demo-1', name: 'デモ太郎' }, loading: false, error: null, refresh });
    const user = userEvent.setup();
    renderLayout('/library');
    await user.click(screen.getByRole('button', { name: 'ログアウト' }));
    expect(await screen.findByRole('link', { name: 'ログイン' })).toHaveAttribute('href', '/login');
    expect(screen.queryByText('デモ太郎')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'ログアウト' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByLabelText('現在のパス')).toHaveTextContent('/login');
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/auth/logout', expect.objectContaining({ method: 'POST' }));
  });

  it.each(['POST失敗', '不正な成功応答', '401の不正な応答', 'refresh失敗', '401後のrefresh失敗'])('ログアウトの%sは表示し、自動再送しない', async (failure) => {
    const refresh = vi.fn().mockRejectedValue(new Error('通信失敗'));
    if (failure === 'POST失敗') fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    else if (failure === '不正な成功応答') fetchMock.mockResolvedValueOnce(Response.json(null));
    else if (failure === '401の不正な応答') fetchMock.mockResolvedValueOnce(new Response('<html>エラー</html>', { status: 401 }));
    else if (failure === '401後のrefresh失敗') fetchMock.mockResolvedValueOnce(Response.json({ error: { code: 'login_required', message: 'ログインが必要です' } }, { status: 401 }));
    else fetchMock.mockResolvedValueOnce(Response.json({ csrf_token: 'logout-csrf' }));
    vi.mocked(useSession).mockReturnValue({ account: { id: 'demo-1', name: 'デモ太郎' }, loading: false, error: null, refresh });
    const user = userEvent.setup();
    renderLayout('/library');
    await user.click(screen.getByRole('button', { name: 'ログアウト' }));
    const refreshed = failure === 'refresh失敗' || failure === '401後のrefresh失敗';
    expect(await screen.findByRole('alert')).toHaveTextContent(refreshed
      ? 'ログアウトしました。表示を更新できませんでした'
      : 'ログアウトを完了できませんでした');
    expect(refresh).toHaveBeenCalledTimes(refreshed ? 1 : 0);
    expect(screen.getByRole('button', { name: 'ログアウト' })).toBeEnabled();
    expect(screen.getByLabelText('現在のパス')).toHaveTextContent('/library');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
