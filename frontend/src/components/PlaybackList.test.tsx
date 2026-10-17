import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BenefitFile } from '../api/types';
import { PlaybackList } from './PlaybackList';

const work = { id: 'work-001', title: '星の手紙', cover_path: '/covers/work-001.svg' };
const benefit = { id: 'benefit-001', title: '特典音源', description: '説明' };
const files: BenefitFile[] = [
  { id: 'file-2', position: 2, title: '星を見上げて', mime_type: 'audio/mpeg' },
  { id: 'file-1', position: 1, title: '夜の手紙', mime_type: 'audio/mpeg' },
  { id: 'file-3', position: 3, title: '朝の歌', mime_type: 'audio/mpeg' },
];
const fileUrl = (file: BenefitFile) => '/api/library/files/' + file.id;

let playMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  playMock = vi.fn(() => Promise.resolve());
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(playMock as () => Promise<void>);
});

function renderList(props: Partial<Parameters<typeof PlaybackList>[0]> = {}) {
  return render(
    <PlaybackList work={work} benefit={benefit} files={files} fileUrl={fileUrl} archiveUrl="/api/library/benefit-001/archive" {...props}>
      <p>差し込み</p>
    </PlaybackList>,
  );
}

function audio() {
  return screen.getByLabelText('プレーヤー') as HTMLAudioElement;
}

describe('再生一覧', () => {
  it('表紙・作品名・特典名・ZIPの通常リンク・曲一覧を表示する', () => {
    renderList();

    expect(screen.getByRole('img', { name: work.title })).toHaveAttribute('src', work.cover_path);
    expect(screen.getByRole('heading', { name: work.title })).toBeInTheDocument();
    expect(screen.getByText(benefit.title)).toBeInTheDocument();
    expect(screen.getByText('差し込み')).toBeInTheDocument();
    const zip = screen.getByRole('link', { name: 'まとめてダウンロード (ZIP)' });
    expect(zip).toHaveAttribute('href', '/api/library/benefit-001/archive');
    expect(zip).not.toHaveAttribute('download');
    expect(screen.getByText(/iPhoneでは「ファイル」アプリに保存されます/)).toBeInTheDocument();
    const items = within(screen.getByRole('list', { name: '曲の一覧' })).getAllByRole('listitem');
    expect(items.map((item) => item.textContent)).toEqual(['01夜の手紙▶ 再生', '02星を見上げて▶ 再生', '03朝の歌▶ 再生']);
    expect(audio()).not.toHaveAttribute('src');
  });

  it('再生ボタンでsrcを切り替えて再生し、再描画でaudio要素を作り直さない', async () => {
    const user = userEvent.setup();
    const { rerender } = renderList();
    const player = audio();

    await user.click(screen.getByRole('button', { name: '星を見上げてを再生' }));
    expect(player).toHaveAttribute('src', '/api/library/files/file-2');
    expect(playMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText('再生中：02. 星を見上げて')).toBeInTheDocument();
    expect(screen.getByRole('listitem', { current: true })).toHaveTextContent('星を見上げて');

    await user.click(screen.getByRole('button', { name: '夜の手紙を再生' }));
    expect(player).toHaveAttribute('src', '/api/library/files/file-1');
    expect(playMock).toHaveBeenCalledTimes(2);

    rerender(
      <PlaybackList work={work} benefit={benefit} files={[...files]} fileUrl={fileUrl} archiveUrl="/api/library/benefit-001/archive" />,
    );
    expect(audio()).toBe(player);
    expect(player).toHaveAttribute('src', '/api/library/files/file-1');
    expect(screen.getByRole('listitem', { current: true })).toHaveTextContent('夜の手紙');
  });

  it('endedで次の曲へ進み、最後の曲で止まる', async () => {
    const user = userEvent.setup();
    renderList();
    const player = audio();

    await user.click(screen.getByRole('button', { name: '星を見上げてを再生' }));
    fireEvent.ended(player);
    expect(player).toHaveAttribute('src', '/api/library/files/file-3');
    expect(screen.getByText('再生中：03. 朝の歌')).toBeInTheDocument();
    expect(playMock).toHaveBeenCalledTimes(2);

    fireEvent.ended(player);
    expect(player).toHaveAttribute('src', '/api/library/files/file-3');
    expect(playMock).toHaveBeenCalledTimes(2);
  });

  it('再生に失敗したら「再生できませんでした」を表示して親に知らせ、別の曲を選ぶと消す', async () => {
    const onPlaybackError = vi.fn();
    const user = userEvent.setup();
    renderList({ onPlaybackError });

    await user.click(screen.getByRole('button', { name: '夜の手紙を再生' }));
    fireEvent.error(audio());
    expect(screen.getByRole('alert')).toHaveTextContent('再生できませんでした');
    expect(onPlaybackError).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: '朝の歌を再生' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('play()が拒否されたら案内を出して「再生中」を外し、プレーヤーで再生が始まれば案内を消す', async () => {
    playMock.mockRejectedValueOnce(new DOMException('自動再生の制限', 'NotAllowedError'));
    const user = userEvent.setup();
    renderList();

    await user.click(screen.getByRole('button', { name: '夜の手紙を再生' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('再生を開始できませんでした。プレーヤーの再生ボタンを押してください');
    expect(screen.queryByText('再生中')).not.toBeInTheDocument();
    expect(screen.queryByRole('listitem', { current: true })).not.toBeInTheDocument();
    expect(screen.getByText('選択中：01. 夜の手紙')).toBeInTheDocument();
    expect(audio()).toHaveAttribute('src', '/api/library/files/file-1');

    fireEvent.play(audio());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('listitem', { current: true })).toHaveTextContent('夜の手紙');
  });

  it('曲の切り替えによるAbortErrorや、切り替え前の曲の拒否は案内しない', async () => {
    let rejectOld!: (error: unknown) => void;
    playMock
      .mockRejectedValueOnce(new DOMException('中断', 'AbortError'))
      .mockReturnValueOnce(new Promise((_, reject) => { rejectOld = reject; }));
    const user = userEvent.setup();
    renderList();

    await user.click(screen.getByRole('button', { name: '夜の手紙を再生' }));
    await user.click(screen.getByRole('button', { name: '星を見上げてを再生' }));
    await user.click(screen.getByRole('button', { name: '朝の歌を再生' }));
    await act(async () => { rejectOld(new DOMException('自動再生の制限', 'NotAllowedError')); });

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('listitem', { current: true })).toHaveTextContent('朝の歌');
  });

  it('曲がなければZIPのリンクを出さずに案内する', () => {
    renderList({ files: [] });

    expect(screen.getByText('曲はありません')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'まとめてダウンロード (ZIP)' })).not.toBeInTheDocument();
  });
});
