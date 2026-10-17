import { Alert, Box, Button, Stack, Typography } from '@mui/material';
import { useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { Benefit, BenefitFile, Work } from '../api/types';

type PlaybackListProps = {
  work: Work;
  benefit: Benefit;
  files: BenefitFile[];
  fileUrl: (file: BenefitFile) => string;
  archiveUrl: string;
  // ヘッダーの下に差し込む表示（残り時間など）
  children?: ReactNode;
  // 再生に失敗したとき（audioのerrorイベント）に呼ぶ
  onPlaybackError?: () => void;
};

function trackNumber(file: BenefitFile) {
  return String(file.position).padStart(2, '0');
}

export function PlaybackList({ work, benefit, files, fileUrl, archiveUrl, children, onPlaybackError }: PlaybackListProps) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [playbackError, setPlaybackError] = useState(false);
  // play()が拒否され、音が出ていない状態（自動再生の制限など）
  const [startFailed, setStartFailed] = useState(false);
  const playRequest = useRef(0);
  const sortedFiles = useMemo(
    () => [...files].sort((first, second) => first.position - second.position),
    [files],
  );
  const current = sortedFiles.find((file) => file.id === currentId);

  const play = (file: BenefitFile) => {
    const audio = audioRef.current;
    if (!audio) return;
    setCurrentId(file.id);
    setPlaybackError(false);
    setStartFailed(false);
    const request = ++playRequest.current;
    // srcはReactで管理せず、audio要素を作り直さずに切り替える
    audio.src = fileUrl(file);
    void audio.play()?.catch((error: unknown) => {
      // 曲の切り替えによる中断（AbortError）と、errorイベントで表示する読み込みの失敗（NotSupportedError）は除く
      const name = error instanceof DOMException ? error.name : undefined;
      if (name === 'AbortError' || name === 'NotSupportedError') return;
      // 別の曲に切り替えた後の、古い要求の拒否は無視する
      if (request !== playRequest.current) return;
      setStartFailed(true);
    });
  };

  const playNext = () => {
    const index = sortedFiles.findIndex((file) => file.id === currentId);
    const next = index >= 0 ? sortedFiles[index + 1] : undefined;
    // 最後の曲ではそのまま止める
    if (next) play(next);
  };

  return (
    <Stack spacing={3} sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <Stack direction="row" spacing={2} sx={{ alignItems: 'flex-start' }}>
        <Box
          component="img"
          src={work.cover_path}
          alt={work.title}
          sx={{ display: 'block', width: { xs: 96, sm: 160 }, height: 'auto', flexShrink: 0, borderRadius: 1 }}
        />
        <Stack spacing={1} sx={{ minWidth: 0 }}>
          <Typography variant="h6" component="h2">{work.title}</Typography>
          <Typography>{benefit.title}</Typography>
          {sortedFiles.length > 0 && (
            <>
              {/* iPhoneのSafariで保存できるよう、fetchやdownload属性を使わず通常のリンクにする */}
              <Button component="a" href={archiveUrl} variant="contained" sx={{ alignSelf: 'flex-start' }}>
                まとめてダウンロード (ZIP)
              </Button>
              <Typography variant="body2" color="text.secondary">
                iPhoneでは「ファイル」アプリに保存されます。タップすると展開できます。
              </Typography>
            </>
          )}
        </Stack>
      </Stack>
      {children}
      {sortedFiles.length === 0 ? <Typography>曲はありません</Typography> : (
        <Box component="ol" aria-label="曲の一覧" sx={{ listStyle: 'none', m: 0, p: 0, borderTop: 1, borderColor: 'divider' }}>
          {sortedFiles.map((file) => {
            const isCurrent = file.id === currentId;
            const isPlaying = isCurrent && !startFailed;
            return (
              <Box
                component="li"
                key={file.id}
                aria-current={isPlaying ? 'true' : undefined}
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 2,
                  py: 1,
                  px: 1,
                  borderBottom: 1,
                  borderColor: 'divider',
                  bgcolor: isPlaying ? 'action.selected' : undefined,
                }}
              >
                <Typography color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>{trackNumber(file)}</Typography>
                <Typography sx={{ flexGrow: 1, minWidth: 0, fontWeight: isPlaying ? 'bold' : undefined }}>
                  {file.title}
                </Typography>
                {isPlaying && <Typography variant="body2" color="primary">再生中</Typography>}
                <Button
                  size="small"
                  variant={isPlaying ? 'contained' : 'outlined'}
                  aria-label={`${file.title}を再生`}
                  onClick={() => { play(file); }}
                  sx={{ flexShrink: 0 }}
                >
                  ▶ 再生
                </Button>
              </Box>
            );
          })}
        </Box>
      )}
      {/* audio要素は常に同じ位置に置き、一覧の再描画で作り直さない */}
      <Box
        sx={{
          display: sortedFiles.length === 0 ? 'none' : 'block',
          position: 'sticky',
          bottom: 0,
          zIndex: 1,
          py: 1,
          bgcolor: 'background.paper',
          borderTop: 1,
          borderColor: 'divider',
        }}
      >
        <Typography variant="body2" aria-live="polite" sx={{ mb: 1 }}>
          {current
            ? `${startFailed ? '選択中' : '再生中'}：${trackNumber(current)}. ${current.title}`
            : '曲を選ぶと再生します'}
        </Typography>
        {playbackError && <Alert severity="error" sx={{ mb: 1 }}>再生できませんでした</Alert>}
        {startFailed && !playbackError && (
          <Alert severity="warning" sx={{ mb: 1 }}>再生を開始できませんでした。プレーヤーの再生ボタンを押してください</Alert>
        )}
        <audio
          ref={audioRef}
          controls
          preload="none"
          aria-label="プレーヤー"
          style={{ display: 'block', width: '100%' }}
          // プレーヤーの再生ボタンで再生が始まったら、案内を消す
          onPlay={() => { setStartFailed(false); }}
          onEnded={playNext}
          onError={() => {
            // srcを設定する前のerrorは無視する
            if (!audioRef.current?.getAttribute('src')) return;
            setPlaybackError(true);
            onPlaybackError?.();
          }}
        />
      </Box>
    </Stack>
  );
}
