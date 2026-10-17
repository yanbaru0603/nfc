import { Alert, Button, Dialog, DialogActions, DialogContent, DialogContentText, DialogTitle, Link, Stack, Typography } from '@mui/material';
import { useEffect, useState } from 'react';
import { Link as RouterLink, useNavigate, useParams } from 'react-router';
import { apiGet, apiPost, isApiError } from '../api/client';
import { isBenefit, isBenefitFiles, isRecord, isWork } from '../api/guards';
import type { BenefitFile, LibraryBenefitResponse } from '../api/types';
import { PlaybackList } from '../components/PlaybackList';
import { ErrorStatus, LoadingStatus } from '../components/Status';
import { LoginRequired } from './LibraryPage';

type BenefitState =
  | { status: 'loading' }
  | { status: 'ready'; data: LibraryBenefitResponse }
  | { status: 'login_required' }
  | { status: 'not_found' }
  | { status: 'error' };

function isLibraryBenefitResponse(value: unknown): value is LibraryBenefitResponse {
  return isRecord(value) && isWork(value.work) && isBenefit(value.benefit) && isBenefitFiles(value.files) &&
    typeof value.demo_reset_enabled === 'boolean';
}

function fileUrl(file: BenefitFile) {
  return '/api/library/files/' + encodeURIComponent(file.id);
}

export function LibraryBenefitPage() {
  const { benefitId = '' } = useParams();
  // 別の特典へ移ったら、取得結果と再生中の曲をリセットする
  return <LibraryBenefitContent key={benefitId} benefitId={benefitId} />;
}

function LibraryBenefitContent({ benefitId }: { benefitId: string }) {
  const navigate = useNavigate();
  const [state, setState] = useState<BenefitState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const [confirmReset, setConfirmReset] = useState(false);
  const [resetPending, setResetPending] = useState(false);
  const [resetError, setResetError] = useState(false);
  const basePath = '/api/library/' + encodeURIComponent(benefitId);

  const reset = async () => {
    setResetPending(true);
    setResetError(false);
    try {
      await apiPost<void>(basePath + '/reset');
      void navigate('/library', { replace: true });
    } catch (error: unknown) {
      if (isApiError(error, 401)) setState({ status: 'login_required' });
      else if (isApiError(error, 404)) setState({ status: 'not_found' });
      else setResetError(true);
    } finally {
      setConfirmReset(false);
      setResetPending(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setState({ status: 'loading' });

    void apiGet<unknown>(basePath, { signal }).then((data) => {
      if (!signal.aborted) {
        setState(isLibraryBenefitResponse(data) ? { status: 'ready', data } : { status: 'error' });
      }
    }).catch((error: unknown) => {
      if (signal.aborted) return;
      if (isApiError(error, 401)) setState({ status: 'login_required' });
      else if (isApiError(error, 404)) setState({ status: 'not_found' });
      else setState({ status: 'error' });
    });

    return () => { controller.abort(); };
  }, [basePath, attempt]);

  return (
    <Stack spacing={3} sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <Link component={RouterLink} to="/library" sx={{ alignSelf: 'flex-start' }}>ライブラリに戻る</Link>
      <Typography variant="h1" component="h1">特典の再生一覧</Typography>
      {state.status === 'loading' && <LoadingStatus message="特典を読み込み中です" />}
      {state.status === 'login_required' && <LoginRequired />}
      {state.status === 'not_found' && <Typography>この特典は見つかりません</Typography>}
      {state.status === 'error' && (
        <ErrorStatus
          title="通信に失敗しました"
          message="特典を取得できませんでした。接続を確認して、再試行してください。"
          onRetry={() => { setAttempt((previous) => previous + 1); }}
        />
      )}
      {state.status === 'ready' && (
        <>
          <PlaybackList
            work={state.data.work}
            benefit={state.data.benefit}
            files={state.data.files}
            fileUrl={fileUrl}
            archiveUrl={basePath + '/archive'}
          />
          {state.data.demo_reset_enabled && (
            <Button color="warning" variant="outlined" onClick={() => { setConfirmReset(true); }} disabled={resetPending}>
              特典を削除して受け取り直す（デモ用）
            </Button>
          )}
          {resetError && (
            <Alert severity="error">削除結果を確認できませんでした。ライブラリに戻って特典が残っているか確認してください。</Alert>
          )}
          <Dialog open={confirmReset} onClose={() => { if (!resetPending) setConfirmReset(false); }} aria-labelledby="reset-title">
            <DialogTitle id="reset-title">特典を削除して受け取り直しますか？</DialogTitle>
            <DialogContent>
              <DialogContentText>
                このアカウントのライブラリから特典を削除します。同じタグをもう一度かざすと追加できます。
                再発行前の古いタグは使えません。
              </DialogContentText>
            </DialogContent>
            <DialogActions>
              <Button onClick={() => { setConfirmReset(false); }} disabled={resetPending}>キャンセル</Button>
              <Button color="warning" onClick={() => { void reset(); }} disabled={resetPending}>削除する</Button>
            </DialogActions>
          </Dialog>
        </>
      )}
    </Stack>
  );
}
