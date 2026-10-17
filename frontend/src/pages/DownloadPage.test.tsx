import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DownloadResponse } from '../api/types';
import { DownloadPage } from './DownloadPage';

const fetchMock = vi.fn<typeof fetch>();
const download: DownloadResponse = {
  expires_at: '2026-10-17T01:23:45Z',
  remaining_seconds: 65,
  work: { id: 'work-001', title: '星の手紙', cover_path: '/covers/work-001.svg' },
  benefit: { id: 'benefit-001', title: '特典音源', description: '説明' },
  files: [
    { id: 'file-1', position: 1, title: '夜の手紙', mime_type: 'audio/mpeg' },
    { id: 'file-2', position: 2, title: '星を見上げて', mime_type: 'audio/mpeg' },
  ],
};

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
});

afterEach(() => {
  vi.useRealTimers();
});

function errorResponse(status: number, code: string) {
  return Response.json({ error: { code, message: 'エラー' } }, { status });
}

function renderAt(path = '/r/demo-token/download') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes><Route path="/r/:token/download" element={<DownloadPage />} /></Routes>
    </MemoryRouter>,
  );
}

describe('ダウンロードページ', () => {
  it('トークンをエンコードして取得し、再生・ZIPのURLに使う', async () => {
    fetchMock.mockResolvedValueOnce(Response.json(download));
    const user = userEvent.setup();
    renderAt('/r/token%2Fa%20b/download');

    expect(await screen.findByRole('heading', { name: '星の手紙' })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/r/token%2Fa%20b/download', expect.objectContaining({ method: 'GET' }));
    const zip = screen.getByRole('link', { name: 'まとめてダウンロード (ZIP)' });
    expect(zip).toHaveAttribute('href', '/api/r/token%2Fa%20b/archive');
    await user.click(screen.getByRole('button', { name: '星を見上げてを再生' }));
    expect(screen.getByLabelText('プレーヤー')).toHaveAttribute('src', '/api/r/token%2Fa%20b/download/file-2');
  });

  it('残り時間を1秒ごとに数え下げ、0で期限切れに切り替える', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance', 'Date'] });
    fetchMock.mockResolvedValueOnce(Response.json({ ...download, remaining_seconds: 61 }));
    renderAt();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });

    expect(screen.getByRole('timer')).toHaveTextContent('再ダウンロードできる残り時間 1:01');
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('timer')).toHaveTextContent('1:00');
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('timer')).toHaveTextContent('0:59');
    // 端末の時計が変わっても、取得時点からの経過時間で数える
    vi.setSystemTime(Date.now() + 3_600_000);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.getByRole('timer')).toHaveTextContent('0:58');

    await act(async () => { await vi.advanceTimersByTimeAsync(58_000); });
    expect(screen.queryByRole('timer')).not.toBeInTheDocument();
    expect(screen.getByText('再ダウンロードの期限が切れました')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '特典ページへ' })).toHaveAttribute('href', '/r/demo-token');
    expect(screen.queryByLabelText('プレーヤー')).not.toBeInTheDocument();
  });

  it.each([
    [410, 'retry_expired', '再ダウンロードの期限が切れました', true],
    [403, 'retry_forbidden', 'このブラウザではダウンロードできません（受け取ったブラウザで開いてください）', true],
    [404, 'entitlement_not_found', 'この特典は見つかりません', false],
  ])('%iでは状態に応じて案内する', async (status, code, message, receiveLink) => {
    fetchMock.mockResolvedValueOnce(errorResponse(status, code));
    renderAt();

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    if (receiveLink) expect(screen.getByRole('link', { name: '特典ページへ' })).toHaveAttribute('href', '/r/demo-token');
    else expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('残り時間0の応答は期限切れとして表示する', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ ...download, remaining_seconds: 0 }));
    renderAt();

    expect(await screen.findByText('再ダウンロードの期限が切れました')).toBeInTheDocument();
  });

  it('通信失敗・HTMLの応答を表示し、再試行で取得し直す', async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(new Response('<html>SPA</html>', { status: 410 }))
      .mockResolvedValueOnce(Response.json(download));
    const user = userEvent.setup();
    renderAt();

    expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
    expect(screen.queryByText('再ダウンロードの期限が切れました')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByRole('heading', { name: '星の手紙' })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('再生エラーで一覧を取り直し、410なら期限切れに切り替える', async () => {
    fetchMock
      .mockResolvedValueOnce(Response.json(download))
      .mockResolvedValueOnce(errorResponse(410, 'retry_expired'));
    const user = userEvent.setup();
    renderAt();
    await user.click(await screen.findByRole('button', { name: '夜の手紙を再生' }));

    fireEvent.error(screen.getByLabelText('プレーヤー'));
    expect(screen.getByRole('alert')).toHaveTextContent('再生できませんでした');
    expect(await screen.findByText('再ダウンロードの期限が切れました')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenLastCalledWith('/api/r/demo-token/download', expect.objectContaining({ method: 'GET' }));
  });

  it('再生エラー後の取り直しが通信失敗なら一覧の表示を保つ', async () => {
    fetchMock
      .mockResolvedValueOnce(Response.json(download))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    renderAt();
    await user.click(await screen.findByRole('button', { name: '夜の手紙を再生' }));

    fireEvent.error(screen.getByLabelText('プレーヤー'));
    await act(async () => {});
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('alert')).toHaveTextContent('再生できませんでした');
    expect(screen.getByRole('timer')).toBeInTheDocument();
  });

  it('期限切れに切り替えたら取り直しを中止し、古い200応答で一覧を戻さない', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance'] });
    let refreshSignal!: AbortSignal;
    let resolveRefresh!: (response: Response) => void;
    fetchMock
      .mockResolvedValueOnce(Response.json({ ...download, remaining_seconds: 2 }))
      .mockImplementationOnce((_, init) => {
        refreshSignal = init!.signal!;
        return new Promise((resolve) => { resolveRefresh = resolve; });
      });
    renderAt();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.click(screen.getByRole('button', { name: '夜の手紙を再生' }));
    fireEvent.error(screen.getByLabelText('プレーヤー'));
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByText('再ダウンロードの期限が切れました')).toBeInTheDocument();
    expect(refreshSignal.aborted).toBe(true);

    await act(async () => {
      resolveRefresh(Response.json({ ...download, remaining_seconds: 30 }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText('再ダウンロードの期限が切れました')).toBeInTheDocument();
    expect(screen.queryByRole('timer')).not.toBeInTheDocument();
    expect(screen.queryByRole('list', { name: '曲の一覧' })).not.toBeInTheDocument();
  });

  it('アンマウント時に取得と取り直しを中止する', async () => {
    const signals: AbortSignal[] = [];
    fetchMock
      .mockResolvedValueOnce(Response.json(download))
      .mockImplementationOnce((_, init) => {
        signals.push(init!.signal!);
        return new Promise(() => {});
      });
    const user = userEvent.setup();
    const { unmount } = renderAt();
    await user.click(await screen.findByRole('button', { name: '夜の手紙を再生' }));
    fireEvent.error(screen.getByLabelText('プレーヤー'));

    let rejectInitial!: (error: unknown) => void;
    fetchMock.mockImplementationOnce((_, init) => {
      signals.push(init!.signal!);
      return new Promise((_, reject) => { rejectInitial = reject; });
    });
    unmount();
    const second = renderAt();
    expect(signals).toHaveLength(2);
    expect(signals[0].aborted).toBe(true);

    second.unmount();
    expect(signals[1].aborted).toBe(true);
    await act(async () => { rejectInitial(signals[1].reason); });
  });
});
