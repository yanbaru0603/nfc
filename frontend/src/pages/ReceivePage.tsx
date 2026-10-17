import {
  Alert,
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Link,
  Stack,
  Typography,
} from '@mui/material';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Link as RouterLink, useNavigate, useParams } from 'react-router';
import { apiGet, apiPost, isApiError } from '../api/client';
import type { ClaimAccountResponse, ClaimDownloadResponse, PrepareLoginResponse, ReceiveResponse } from '../api/types';
import { ErrorStatus, LoadingStatus } from '../components/Status';
import { useSession } from '../session';

type ReceiveState =
  | { status: 'loading' }
  | { status: 'ready'; data: ReceiveResponse }
  | { status: 'not_found' }
  | { status: 'error' };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isReceiveResponse(value: unknown): value is ReceiveResponse {
  return isRecord(value) &&
    typeof value.repeatable === 'boolean' &&
    typeof value.state === 'string' &&
    ['unused', 'account_owned', 'download_retryable', 'claimed'].includes(value.state) &&
    isRecord(value.work) &&
    typeof value.work.id === 'string' &&
    typeof value.work.title === 'string' &&
    typeof value.work.cover_path === 'string' &&
    isRecord(value.benefit) &&
    typeof value.benefit.id === 'string' &&
    typeof value.benefit.title === 'string' &&
    typeof value.benefit.description === 'string' &&
    Array.isArray(value.files) &&
    value.files.every((file: unknown) => isRecord(file) &&
      typeof file.id === 'string' &&
      typeof file.position === 'number' &&
      typeof file.title === 'string' &&
      typeof file.mime_type === 'string');
}

export function ReceivePage() {
  const { token = '' } = useParams();
  const attemptedTokens = useRef(new Set<string>());
  // 別のトークンへ移ったら、取得結果と確認ダイアログをリセットする
  return <ReceiveContent key={token} token={token} attemptedTokens={attemptedTokens.current} />;
}

function ReceiveContent({ token, attemptedTokens }: { token: string; attemptedTokens: Set<string> }) {
  const navigate = useNavigate();
  const { account, loading: sessionLoading, error: sessionError, refresh } = useSession();
  const [state, setState] = useState<ReceiveState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState(false);
  const [accountClaimFailed, setAccountClaimFailed] = useState(attemptedTokens.has(token));
  const actionPending = useRef(false);
  const mounted = useRef(false);
  const apiPath = '/api/r/' + encodeURIComponent(token);
  const dialogTitleId = useId();
  const dialogDescriptionId = useId();

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setState({ status: 'loading' });

    void apiGet<ReceiveResponse>(apiPath, { signal }).then((data) => {
      if (!signal.aborted) {
        setState(isReceiveResponse(data) ? { status: 'ready', data } : { status: 'error' });
      }
    }).catch((error: unknown) => {
      if (!signal.aborted) {
        setState({ status: isApiError(error, 404, 'entitlement_not_found') ? 'not_found' : 'error' });
      }
    });

    return () => { controller.abort(); };
  }, [apiPath, attempt]);

  const runAction = useCallback(async (action: 'login' | 'account' | 'download') => {
    if (actionPending.current) return;
    actionPending.current = true;
    setBusy(true);
    setActionError(false);
    try {
      let destination: string;
      if (action === 'login') {
        const data = await apiPost<PrepareLoginResponse>('/api/auth/prepare', { token });
        if (!isRecord(data) || typeof data.return_to !== 'string') throw new Error('invalid_response');
        destination = '/login';
      } else if (action === 'account') {
        const data = await apiPost<ClaimAccountResponse>(apiPath + '/claim/account');
        if (!isRecord(data) || typeof data.benefit_id !== 'string' || !data.benefit_id) throw new Error('invalid_response');
        destination = '/library/' + encodeURIComponent(data.benefit_id);
      } else {
        const data = await apiPost<ClaimDownloadResponse>(apiPath + '/claim/download');
        if (!isRecord(data) || typeof data.expires_at !== 'string' || typeof data.remaining_seconds !== 'number') throw new Error('invalid_response');
        destination = '/r/' + encodeURIComponent(token) + '/download';
      }
      if (mounted.current) {
        setDialogOpen(false);
        await navigate(destination, { replace: action === 'account' });
      }
    } catch (error: unknown) {
      if (mounted.current) {
        setDialogOpen(false);
        setActionError(true);
        if (action === 'account') setAccountClaimFailed(true);
        setState({ status: 'loading' });
        setAttempt((previous) => previous + 1);
        if (action === 'account' && isApiError(error, 401, 'login_required')) {
          try {
            await refresh();
          } catch {
            // セッション取得の失敗は共通レイアウトに表示される
          }
        }
      }
    } finally {
      actionPending.current = false;
      if (mounted.current) setBusy(false);
    }
  }, [apiPath, navigate, refresh, token]);

  useEffect(() => {
    if (state.status !== 'ready' || state.data.state !== 'unused' ||
      !account || sessionLoading || sessionError || actionPending.current || attemptedTokens.has(token)) return;
    // 失敗後にunusedが返っても、StrictModeでEffectが再実行されても再送しない
    attemptedTokens.add(token);
    void runAction('account');
  }, [account, sessionLoading, sessionError, state, attemptedTokens, token, runAction]);

  return (
    <Stack spacing={3} sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <Typography variant="h1" component="h1">特典ページ</Typography>
      {state.status === 'loading' && <LoadingStatus message="特典を読み込み中です" />}
      {state.status === 'not_found' && <Alert severity="warning">この特典は見つかりません</Alert>}
      {actionError && (state.status === 'ready' || state.status === 'not_found') && (
        <Alert severity="warning">操作を完了できませんでした。最新の状態を表示しています</Alert>
      )}
      {state.status === 'error' && (
        <ErrorStatus
          title="通信に失敗しました"
          message="特典情報を取得できませんでした。接続を確認して、再試行してください。"
          onRetry={() => { setAttempt((previous) => previous + 1); }}
        />
      )}
      {state.status === 'ready' && (
        <>
          {state.data.repeatable && <Alert severity="info">このカードはデモ用です。何度でも特典を受け取れます</Alert>}
          <Typography variant="h6" component="h2">{state.data.work.title}</Typography>
          <Box
            component="img"
            src={state.data.work.cover_path}
            alt={state.data.work.title + 'の表紙'}
            sx={{ display: 'block', width: '100%', maxWidth: 320, aspectRatio: '1 / 1', objectFit: 'contain', alignSelf: 'center', borderRadius: 1 }}
          />
          <Stack spacing={1}>
            <Typography variant="h6" component="h2">{state.data.benefit.title}</Typography>
            <Typography sx={{ whiteSpace: 'pre-wrap' }}>{state.data.benefit.description}</Typography>
          </Stack>
          <Stack spacing={1}>
            <Typography variant="h6" component="h3">ファイル（曲）一覧</Typography>
            {state.data.files.length === 0 ? <Typography>ファイルはありません</Typography> : (
              <Box component="ol" aria-label="特典ファイル" sx={{ m: 0, pl: 3 }}>
                {[...state.data.files].sort((first, second) => first.position - second.position).map((file) => (
                  <Typography component="li" key={file.id} sx={{ py: 0.5 }}>{file.title}</Typography>
                ))}
              </Box>
            )}
          </Stack>
          {!sessionLoading && !sessionError && (
            <Stack spacing={2}>
              {state.data.state === 'unused' && (account ? (
                !accountClaimFailed
                  ? <LoadingStatus message="アカウントに追加しています" />
                  : <>
                    <Typography>この特典はまだ受け取られていません</Typography>
                    <Button variant="contained" fullWidth disabled={busy} onClick={() => { void runAction('account'); }}>
                      ライブラリに追加
                    </Button>
                    {busy && <LoadingStatus message="アカウントに追加しています" />}
                  </>
              ) : (
                <>
                  <Button
                    variant="contained"
                    fullWidth
                    disabled={busy}
                    onClick={() => { void runAction('login'); }}
                  >
                    ログインしてライブラリに追加
                  </Button>
                  <Button
                    variant="text"
                    fullWidth
                    disabled={busy}
                    onClick={() => { setActionError(false); setDialogOpen(true); }}
                  >
                    {state.data.repeatable ? '登録せずにダウンロード' : '登録せずに一回だけダウンロード'}
                  </Button>
                  <Dialog
                    open={dialogOpen}
                    onClose={() => { if (!actionPending.current) setDialogOpen(false); }}
                    fullWidth
                    maxWidth="xs"
                    aria-labelledby={dialogTitleId}
                    aria-describedby={dialogDescriptionId}
                  >
                    <DialogTitle id={dialogTitleId}>登録せずにダウンロードしますか？</DialogTitle>
                    <DialogContent sx={{ overflowWrap: 'anywhere' }}>
                      <Stack id={dialogDescriptionId} spacing={2}>
                        <Typography>{state.data.repeatable
                          ? '受け取った後も、同じカードから繰り返し受け取れます。'
                          : '登録せずに受け取ると、後からアカウントに追加できません。'}</Typography>
                        <Typography>再ダウンロードは、同じブラウザで短時間に限られます。</Typography>
                      </Stack>
                    </DialogContent>
                    <DialogActions disableSpacing sx={{ flexDirection: { xs: 'column', sm: 'row' }, gap: 1, p: 2 }}>
                      <Button disabled={busy} onClick={() => { setDialogOpen(false); }} sx={{ width: { xs: '100%', sm: 'auto' } }}>
                        キャンセル
                      </Button>
                      <Button
                        variant="contained"
                        disabled={busy}
                        onClick={() => { void runAction('download'); }}
                        sx={{ width: { xs: '100%', sm: 'auto' } }}
                      >
                        ダウンロードする
                      </Button>
                    </DialogActions>
                  </Dialog>
                </>
              ))}
              {state.data.state === 'account_owned' && (
                <>
                  <Typography>この特典は受け取り済みです</Typography>
                  <Button component={RouterLink} variant="contained" to={'/library/' + encodeURIComponent(state.data.benefit.id)}>特典の再生一覧へ</Button>
                </>
              )}
              {state.data.state === 'download_retryable' && (
                <Button component={RouterLink} variant="contained" to={'/r/' + encodeURIComponent(token) + '/download'}>ダウンロードページへ</Button>
              )}
            </Stack>
          )}
          {state.data.state === 'claimed' && (
            <Stack spacing={2}>
              <Typography>この特典はすでに受け取られています</Typography>
              {!account && !sessionLoading && !sessionError && (
                <>
                  <Typography>アカウントに追加した方は、ログインするとライブラリで確認できます</Typography>
                  <Link component={RouterLink} to="/login">ログイン</Link>
                </>
              )}
            </Stack>
          )}
        </>
      )}
    </Stack>
  );
}
