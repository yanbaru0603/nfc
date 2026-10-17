import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { LibraryBenefitResponse } from '../api/types';
import { getSession } from '../api/client';
import { LibraryBenefitPage } from './LibraryBenefitPage';
import { LibraryPage } from './LibraryPage';

const fetchMock = vi.fn<typeof fetch>();
const work = { id: 'work-001', title: '星の手紙', cover_path: '/covers/work-001.svg' };
const benefit = { id: 'benefit/001', title: '特典音源', description: '説明' };
const detail: LibraryBenefitResponse = {
  work,
  benefit,
  files: [{ id: 'file 1', position: 1, title: '夜の手紙', mime_type: 'audio/mpeg' }],
  demo_reset_enabled: false,
};

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
});

function errorResponse(status: number, code: string) {
  return Response.json({ error: { code, message: 'エラー' } }, { status });
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/library" element={<LibraryPage />} />
        <Route path="/library/:benefitId" element={<LibraryBenefitPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('ライブラリ', () => {
  it('特典をカードで一覧し、再生一覧へリンクする', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ items: [{ work, benefit }] }));
    renderAt('/library');

    const list = await screen.findByRole('list', { name: '受け取った特典' });
    const card = within(list).getByRole('link');
    expect(card).toHaveAttribute('href', '/library/benefit%2F001');
    expect(card).toHaveTextContent('星の手紙特典音源');
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/library', expect.objectContaining({ method: 'GET' }));
  });

  it('0件なら「まだ特典がありません」を表示する', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ items: [] }));
    renderAt('/library');

    expect(await screen.findByText('まだ特典がありません')).toBeInTheDocument();
  });

  it('401ならログインを案内する', async () => {
    fetchMock.mockResolvedValueOnce(errorResponse(401, 'login_required'));
    renderAt('/library');

    expect(await screen.findByText('ログインが必要です')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'ログイン画面へ' })).toHaveAttribute('href', '/login');
  });

  it('通信失敗・不正な応答を表示し、再試行で取得し直す', async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(Response.json({ items: [{ work }] }))
      .mockResolvedValueOnce(Response.json({ items: [] }));
    const user = userEvent.setup();
    renderAt('/library');

    expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByText('まだ特典がありません')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

describe('特典の再生一覧', () => {
  it('通常モードではデモ用の削除を表示しない', async () => {
    fetchMock.mockResolvedValueOnce(Response.json(detail));
    renderAt('/library/benefit-001');
    await screen.findByRole('heading', { name: work.title });
    expect(screen.queryByRole('button', { name: /特典を削除/ })).not.toBeInTheDocument();
  });

  it('デモ用の削除をキャンセルするとPOSTしない', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ ...detail, demo_reset_enabled: true }));
    const user = userEvent.setup();
    renderAt('/library/benefit-001');
    await user.click(await screen.findByRole('button', { name: /特典を削除/ }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('同じタグをもう一度かざすと追加できます');
    await user.click(within(dialog).getByRole('button', { name: 'キャンセル' }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('削除の確定中は二重送信を防ぎ、成功したらライブラリへ戻る', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ account: null, csrf_token: 'reset-csrf' }));
    await getSession({ force: true });
    let finishReset!: (response: Response) => void;
    fetchMock
      .mockResolvedValueOnce(Response.json({ ...detail, demo_reset_enabled: true }))
      .mockImplementationOnce(() => new Promise((resolve) => { finishReset = resolve; }))
      .mockResolvedValueOnce(Response.json({ items: [] }));
    const user = userEvent.setup();
    renderAt('/library/benefit-001');
    await user.click(await screen.findByRole('button', { name: /特典を削除/ }));
    await user.click(screen.getByRole('button', { name: '削除する' }));
    expect(screen.getByRole('button', { name: '削除する' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'キャンセル' })).toBeDisabled();
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/library/benefit-001/reset', expect.objectContaining({
      method: 'POST', headers: expect.objectContaining({ 'X-CSRF-Token': 'reset-csrf' }),
    }));
    await act(async () => { finishReset(new Response(null, { status: 204 })); });
    expect(await screen.findByText('まだ特典がありません')).toBeInTheDocument();
  });

  it('削除の通信失敗は自動再送せず、確認方法を案内する', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ account: null, csrf_token: 'reset-csrf' }));
    await getSession({ force: true });
    fetchMock
      .mockResolvedValueOnce(Response.json({ ...detail, demo_reset_enabled: true }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    renderAt('/library/benefit-001');
    await user.click(await screen.findByRole('button', { name: /特典を削除/ }));
    await user.click(screen.getByRole('button', { name: '削除する' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('ライブラリに戻って特典が残っているか確認');
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('IDをエンコードして取得し、再生・ZIPのURLと戻るリンクを表示する', async () => {
    fetchMock.mockResolvedValueOnce(Response.json(detail));
    const user = userEvent.setup();
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue();
    renderAt('/library/benefit%2F001');

    expect(await screen.findByRole('heading', { name: work.title })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/library/benefit%2F001', expect.objectContaining({ method: 'GET' }));
    expect(screen.getByRole('link', { name: 'まとめてダウンロード (ZIP)' })).toHaveAttribute('href', '/api/library/benefit%2F001/archive');
    expect(screen.getByRole('link', { name: 'ライブラリに戻る' })).toHaveAttribute('href', '/library');
    await user.click(screen.getByRole('button', { name: '夜の手紙を再生' }));
    expect(screen.getByLabelText('プレーヤー')).toHaveAttribute('src', '/api/library/files/file%201');
  });

  it.each([
    [401, 'login_required', 'ログインが必要です'],
    [404, 'benefit_not_found', 'この特典は見つかりません'],
  ])('%iでは状態に応じて案内する', async (status, code, message) => {
    fetchMock.mockResolvedValueOnce(errorResponse(status, code));
    renderAt('/library/benefit-001');

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('HTMLの404は通信失敗として表示し、再試行で取得し直す', async () => {
    fetchMock
      .mockResolvedValueOnce(new Response('<html>SPA</html>', { status: 404 }))
      .mockResolvedValueOnce(Response.json(detail));
    const user = userEvent.setup();
    renderAt('/library/benefit-001');

    expect(await screen.findByRole('alert')).toHaveTextContent('通信に失敗しました');
    expect(screen.queryByText('この特典は見つかりません')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '再試行' }));
    expect(await screen.findByRole('heading', { name: work.title })).toBeInTheDocument();
  });

  it.each(['/library', '/library/benefit-001'])('アンマウント時に取得を中止する（%s）', async (path) => {
    let signal!: AbortSignal;
    let rejectRequest!: (error: unknown) => void;
    fetchMock.mockImplementationOnce((_, init) => {
      signal = init!.signal!;
      return new Promise((_, reject) => { rejectRequest = reject; });
    });
    const { unmount } = renderAt(path);
    expect(screen.getByRole('status')).toBeInTheDocument();

    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { rejectRequest(signal.reason); });
  });
});
