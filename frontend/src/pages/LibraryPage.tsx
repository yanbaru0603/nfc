import { Box, Card, CardActionArea, CardContent, Link, Stack, Typography } from '@mui/material';
import { useEffect, useState } from 'react';
import { Link as RouterLink } from 'react-router';
import { apiGet, isApiError } from '../api/client';
import { isBenefit, isRecord, isWork } from '../api/guards';
import type { LibraryResponse } from '../api/types';
import { ErrorStatus, LoadingStatus } from '../components/Status';

type LibraryState =
  | { status: 'loading' }
  | { status: 'ready'; data: LibraryResponse }
  | { status: 'login_required' }
  | { status: 'error' };

function isLibraryResponse(value: unknown): value is LibraryResponse {
  return isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every((item: unknown) => isRecord(item) && isWork(item.work) && isBenefit(item.benefit));
}

export function LibraryPage() {
  const [state, setState] = useState<LibraryState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setState({ status: 'loading' });

    void apiGet<unknown>('/api/library', { signal }).then((data) => {
      if (!signal.aborted) {
        setState(isLibraryResponse(data) ? { status: 'ready', data } : { status: 'error' });
      }
    }).catch((error: unknown) => {
      if (!signal.aborted) {
        setState({ status: isApiError(error, 401) ? 'login_required' : 'error' });
      }
    });

    return () => { controller.abort(); };
  }, [attempt]);

  return (
    <Stack spacing={3} sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <Typography variant="h1" component="h1">ライブラリ</Typography>
      {state.status === 'loading' && <LoadingStatus message="ライブラリを読み込み中です" />}
      {state.status === 'login_required' && <LoginRequired />}
      {state.status === 'error' && (
        <ErrorStatus
          title="通信に失敗しました"
          message="ライブラリを取得できませんでした。接続を確認して、再試行してください。"
          onRetry={() => { setAttempt((previous) => previous + 1); }}
        />
      )}
      {state.status === 'ready' && (state.data.items.length === 0 ? <Typography>まだ特典がありません</Typography> : (
        <Box component="ul" aria-label="受け取った特典" sx={{ listStyle: 'none', m: 0, p: 0, display: 'grid', gap: 2 }}>
          {state.data.items.map(({ work, benefit }) => (
            <Box component="li" key={benefit.id}>
              <Card variant="outlined">
                <CardActionArea
                  component={RouterLink}
                  to={'/library/' + encodeURIComponent(benefit.id)}
                  sx={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-start', gap: 2, p: 1 }}
                >
                  <Box
                    component="img"
                    src={work.cover_path}
                    alt=""
                    sx={{ display: 'block', width: 80, height: 'auto', flexShrink: 0, borderRadius: 1 }}
                  />
                  <CardContent sx={{ p: 0, minWidth: 0, '&:last-child': { pb: 0 } }}>
                    <Typography variant="h6" component="h2">{work.title}</Typography>
                    <Typography color="text.secondary">{benefit.title}</Typography>
                  </CardContent>
                </CardActionArea>
              </Card>
            </Box>
          ))}
        </Box>
      ))}
    </Stack>
  );
}

export function LoginRequired() {
  return (
    <Stack spacing={1}>
      <Typography>ログインが必要です</Typography>
      <Link component={RouterLink} to="/login">ログイン画面へ</Link>
    </Stack>
  );
}
