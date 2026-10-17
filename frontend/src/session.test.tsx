import type { ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, getSession } from './api/client';
import type { SessionResponse } from './api/types';
import { SessionProvider, useSession } from './session';

vi.mock('./api/client', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api/client')>(),
  getSession: vi.fn(),
}));

const account = { id: 'demo-1', name: 'デモ太郎' };
const getSessionMock = vi.mocked(getSession);

beforeEach(() => {
  getSessionMock.mockReset();
  getSessionMock.mockResolvedValue({ account, csrf_token: 'csrf-initial' });
});

function renderSession() {
  return renderHook(useSession, {
    wrapper: ({ children }: { children: ReactNode }) => <SessionProvider>{children}</SessionProvider>,
  });
}

describe('セッションの再取得', () => {
  it.each([account, null])('refreshは取得したaccountを返して状態に反映する（%j）', async (nextAccount) => {
    const { result } = renderSession();
    await waitFor(() => expect(result.current.loading).toBe(false));
    getSessionMock.mockResolvedValueOnce({ account: nextAccount, csrf_token: 'csrf-new' });

    await act(async () => {
      await expect(result.current.refresh()).resolves.toEqual(nextAccount);
    });

    expect(result.current.account).toEqual(nextAccount);
    expect(result.current.error).toBeNull();
    expect(getSessionMock).toHaveBeenLastCalledWith({ force: true });
  });

  it('初回取得はloadingにし、取得成功後の再取得中は前のaccountを表示する', async () => {
    let resolveSession!: (session: SessionResponse) => void;
    getSessionMock.mockReturnValueOnce(new Promise((resolve) => { resolveSession = resolve; }));
    const { result } = renderSession();
    expect(result.current.loading).toBe(true);
    expect(getSessionMock).toHaveBeenCalledWith({ force: false });
    await act(async () => { resolveSession({ account, csrf_token: 'csrf-initial' }); });

    getSessionMock.mockReturnValueOnce(new Promise((resolve) => { resolveSession = resolve; }));
    let refresh!: Promise<typeof account | null>;
    act(() => { refresh = result.current.refresh(); });
    expect(result.current.loading).toBe(false);
    expect(result.current.account).toEqual(account);

    await act(async () => {
      resolveSession({ account: null, csrf_token: 'csrf-new' });
      await refresh;
    });
    expect(result.current.account).toBeNull();
  });

  it('refreshの失敗はerrorに格納して例外を投げ、前のaccountを残す', async () => {
    const { result } = renderSession();
    await waitFor(() => expect(result.current.loading).toBe(false));
    const error = new ApiError(0, 'network_error', '通信に失敗しました');
    getSessionMock.mockRejectedValueOnce(error);

    await act(async () => {
      await expect(result.current.refresh()).rejects.toBe(error);
    });

    expect(result.current.error).toBe(error);
    expect(result.current.account).toEqual(account);
    expect(result.current.loading).toBe(false);

    await act(async () => { await result.current.refresh(); });
    expect(result.current.error).toBeNull();
    expect(result.current.account).toEqual(account);
  });

  it('初回取得の失敗後は再取得でloadingに戻し、失敗を呼び出し元に返す', async () => {
    const error = new ApiError(0, 'network_error', '通信に失敗しました');
    getSessionMock.mockRejectedValueOnce(error);
    const { result } = renderSession();
    await waitFor(() => expect(result.current.error).toBe(error));
    let rejectSession!: (error: ApiError) => void;
    getSessionMock.mockReturnValueOnce(new Promise((_, reject) => { rejectSession = reject; }));
    let refresh!: Promise<typeof account | null>;
    act(() => { refresh = result.current.refresh(); });
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBeNull();

    await act(async () => {
      const rejected = expect(refresh).rejects.toBe(error);
      rejectSession(error);
      await rejected;
    });
    expect(result.current.error).toBe(error);
    expect(result.current.loading).toBe(false);
  });

  it('未ログインの取得成功後も再取得中はloadingに戻さない', async () => {
    getSessionMock.mockResolvedValueOnce({ account: null, csrf_token: 'csrf-initial' });
    const { result } = renderSession();
    await waitFor(() => expect(result.current.loading).toBe(false));
    let resolveSession!: (session: SessionResponse) => void;
    getSessionMock.mockReturnValueOnce(new Promise((resolve) => { resolveSession = resolve; }));

    let refresh!: Promise<typeof account | null>;
    act(() => { refresh = result.current.refresh(); });
    expect(result.current.loading).toBe(false);
    expect(result.current.account).toBeNull();

    await act(async () => {
      resolveSession({ account, csrf_token: 'csrf-new' });
      await refresh;
    });
    expect(result.current.account).toEqual(account);
  });

  it('遅れて届いた初回の応答でrefreshのaccountを上書きしない', async () => {
    let resolveInitial!: (session: SessionResponse) => void;
    getSessionMock.mockReturnValueOnce(new Promise((resolve) => { resolveInitial = resolve; }));
    const { result } = renderSession();
    await act(async () => { await result.current.refresh(); });
    expect(result.current.account).toEqual(account);

    await act(async () => { resolveInitial({ account: null, csrf_token: 'csrf-old' }); });
    expect(result.current.account).toEqual(account);
    expect(result.current.loading).toBe(false);
  });
});
