import { Alert, Link, Stack, Typography } from '@mui/material';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link as RouterLink, useParams } from 'react-router';
import { apiGet, isApiError } from '../api/client';
import { isBenefit, isBenefitFiles, isRecord, isWork } from '../api/guards';
import type { BenefitFile, DownloadResponse } from '../api/types';
import { PlaybackList } from '../components/PlaybackList';
import { ErrorStatus, LoadingStatus } from '../components/Status';

type DownloadState =
  | { status: 'loading' }
  // deadlineは performance.now() 基準の期限（端末の時計に依存しない）
  | { status: 'ready'; data: DownloadResponse; deadline: number }
  | { status: 'expired' }
  | { status: 'forbidden' }
  | { status: 'not_found' }
  | { status: 'error' };

function isDownloadResponse(value: unknown): value is DownloadResponse {
  return isRecord(value) &&
    typeof value.expires_at === 'string' &&
    typeof value.remaining_seconds === 'number' &&
    Number.isFinite(value.remaining_seconds) &&
    isWork(value.work) &&
    isBenefit(value.benefit) &&
    isBenefitFiles(value.files);
}

function toState(data: unknown): DownloadState {
  if (!isDownloadResponse(data)) return { status: 'error' };
  if (data.remaining_seconds <= 0) return { status: 'expired' };
  return { status: 'ready', data, deadline: performance.now() + data.remaining_seconds * 1000 };
}

function remainingSeconds(deadline: number) {
  return Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
}

function formatRemaining(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

export function DownloadPage() {
  const { token = '' } = useParams();
  // 別のトークンへ移ったら、取得結果と再生中の曲をリセットする
  return <DownloadContent key={token} token={token} />;
}

function DownloadContent({ token }: { token: string }) {
  const [state, setState] = useState<DownloadState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const refreshController = useRef<AbortController | null>(null);
  const basePath = '/api/r/' + encodeURIComponent(token);
  const receivePath = '/r/' + encodeURIComponent(token);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setState({ status: 'loading' });

    void apiGet<unknown>(basePath + '/download', { signal }).then((data) => {
      if (!signal.aborted) setState(toState(data));
    }).catch((error: unknown) => {
      if (signal.aborted) return;
      if (isApiError(error, 410)) setState({ status: 'expired' });
      else if (isApiError(error, 403)) setState({ status: 'forbidden' });
      else if (isApiError(error, 404)) setState({ status: 'not_found' });
      else setState({ status: 'error' });
    });

    return () => { controller.abort(); };
  }, [basePath, attempt]);

  // 再生の失敗は期限切れの可能性があるため、一覧を取り直して状態を確かめる
  const handlePlaybackError = useCallback(() => {
    refreshController.current?.abort();
    const controller = new AbortController();
    refreshController.current = controller;
    const { signal } = controller;

    void apiGet<unknown>(basePath + '/download', { signal }).then((data) => {
      if (signal.aborted) return;
      const next = toState(data);
      // 不正な応答なら今の表示を保ち、プレーヤーの「再生できませんでした」に任せる
      if (next.status !== 'error') setState(next);
    }).catch((error: unknown) => {
      if (signal.aborted) return;
      if (isApiError(error, 410)) setState({ status: 'expired' });
      else if (isApiError(error, 403)) setState({ status: 'forbidden' });
      else if (isApiError(error, 404)) setState({ status: 'not_found' });
      // 通信失敗では一覧の表示を保つ
    });
  }, [basePath]);

  useEffect(() => () => { refreshController.current?.abort(); }, []);

  const expire = useCallback(() => {
    // 期限切れの後に、取り直しの古い応答で一覧を戻さない
    refreshController.current?.abort();
    refreshController.current = null;
    setState({ status: 'expired' });
  }, []);

  return (
    <Stack spacing={3} sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <Typography variant="h1" component="h1">ダウンロード</Typography>
      {state.status === 'loading' && <LoadingStatus message="特典を読み込み中です" />}
      {state.status === 'expired' && (
        <Stack spacing={1}>
          <Typography>再ダウンロードの期限が切れました</Typography>
          <Link component={RouterLink} to={receivePath} sx={{ alignSelf: 'flex-start' }}>特典ページへ</Link>
        </Stack>
      )}
      {state.status === 'forbidden' && (
        <Stack spacing={1}>
          <Typography>このブラウザではダウンロードできません（受け取ったブラウザで開いてください）</Typography>
          <Link component={RouterLink} to={receivePath} sx={{ alignSelf: 'flex-start' }}>特典ページへ</Link>
        </Stack>
      )}
      {state.status === 'not_found' && <Typography>この特典は見つかりません</Typography>}
      {state.status === 'error' && (
        <ErrorStatus
          title="通信に失敗しました"
          message="ダウンロードできるファイルを取得できませんでした。接続を確認して、再試行してください。"
          onRetry={() => { setAttempt((previous) => previous + 1); }}
        />
      )}
      {state.status === 'ready' && (
        <PlaybackList
          work={state.data.work}
          benefit={state.data.benefit}
          files={state.data.files}
          fileUrl={(file: BenefitFile) => basePath + '/download/' + encodeURIComponent(file.id)}
          archiveUrl={basePath + '/archive'}
          onPlaybackError={handlePlaybackError}
        >
          <RemainingTime deadline={state.deadline} onExpire={expire} />
        </PlaybackList>
      )}
    </Stack>
  );
}

function RemainingTime({ deadline, onExpire }: { deadline: number; onExpire: () => void }) {
  const [seconds, setSeconds] = useState(() => remainingSeconds(deadline));

  useEffect(() => {
    const update = () => {
      const next = remainingSeconds(deadline);
      setSeconds(next);
      if (next === 0) onExpire();
    };
    update();
    // 秒の切り替わりに遅れないよう、1秒より短い間隔で確かめる
    const timer = setInterval(update, 250);
    return () => { clearInterval(timer); };
  }, [deadline, onExpire]);

  return (
    <Alert severity="info" role="timer" aria-live="off">
      再ダウンロードできる残り時間 {formatRemaining(seconds)}
    </Alert>
  );
}
