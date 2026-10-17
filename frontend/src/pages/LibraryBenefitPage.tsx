import { Link, Stack, Typography } from '@mui/material';
import { useEffect, useState } from 'react';
import { Link as RouterLink, useParams } from 'react-router';
import { apiGet, isApiError } from '../api/client';
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
  return isRecord(value) && isWork(value.work) && isBenefit(value.benefit) && isBenefitFiles(value.files);
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
  const [state, setState] = useState<BenefitState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const basePath = '/api/library/' + encodeURIComponent(benefitId);

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
        <PlaybackList
          work={state.data.work}
          benefit={state.data.benefit}
          files={state.data.files}
          fileUrl={fileUrl}
          archiveUrl={basePath + '/archive'}
        />
      )}
    </Stack>
  );
}
