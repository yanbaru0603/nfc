import { Alert, Button, Stack, Typography } from '@mui/material';
import { useEffect, useRef, useState } from 'react';
import { Link as RouterLink, useNavigate } from 'react-router';
import { apiGet, apiPost } from '../api/client';
import type { Account, DemoAccountsResponse, LoginResponse } from '../api/types';
import { ErrorStatus, LoadingStatus } from '../components/Status';
import { useSession } from '../session';

type AccountsState =
  | { status: 'loading' }
  | { status: 'ready'; accounts: Account[] }
  | { status: 'error' };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isAccount(value: unknown): value is Account {
  return isRecord(value) && typeof value.id === 'string' && typeof value.name === 'string';
}

export function LoginPage() {
  const { account, loading, error, refresh } = useSession();
  const navigate = useNavigate();
  const [state, setState] = useState<AccountsState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState(false);
  const pending = useRef(false);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    if (account || loading || error) return;
    const controller = new AbortController();
    const { signal } = controller;
    setState({ status: 'loading' });
    void apiGet<DemoAccountsResponse>('/api/auth/accounts', { signal }).then((data) => {
      if (signal.aborted) return;
      setState(isRecord(data) && Array.isArray(data.accounts) && data.accounts.every(isAccount)
        ? { status: 'ready', accounts: data.accounts }
        : { status: 'error' });
    }).catch(() => {
      if (!signal.aborted) setState({ status: 'error' });
    });
    return () => { controller.abort(); };
  }, [account, loading, error, attempt]);

  const login = async (accountId: string) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setActionError(false);
    try {
      const data = await apiPost<LoginResponse>('/api/auth/login', { account_id: accountId });
      if (!isRecord(data) || !isAccount(data.account) || typeof data.csrf_token !== 'string') {
        throw new Error('invalid_response');
      }
      const sessionAccount = await refresh();
      if (!sessionAccount) throw new Error('login_required');
      const destination = typeof data.return_to === 'string' &&
        data.return_to.startsWith('/') && !data.return_to.startsWith('//') && !data.return_to.includes('\\')
        ? data.return_to : '/library';
      if (mounted.current) await navigate(destination, { replace: true });
    } catch {
      if (mounted.current) setActionError(true);
    } finally {
      pending.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  return (
    <Stack spacing={3} sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <Typography variant="h1" component="h1">ログイン</Typography>
      {actionError && <ErrorStatus message="ログインを完了できませんでした。ログイン状態を確認して、もう一度お試しください。" />}
      {account ? (
        <>
          <Typography>{account.name}さんとしてログイン済みです</Typography>
          <Button component={RouterLink} to="/library" variant="contained" disabled={busy}>ライブラリへ</Button>
          <Typography>別のアカウントを選ぶには、画面上部の「ログアウト」を押してください。</Typography>
        </>
      ) : !loading && !error && (
        <>
          <Typography>デモ用のアカウントを選んでください。パスワードは不要です。</Typography>
          {state.status === 'loading' && <LoadingStatus message="アカウントを読み込み中です" />}
          {state.status === 'error' && (
            <ErrorStatus
              title="通信に失敗しました"
              message="アカウント一覧を取得できませんでした。接続を確認して、再試行してください。"
              onRetry={() => { setAttempt((previous) => previous + 1); }}
            />
          )}
          {state.status === 'ready' && (state.accounts.length === 0
            ? <Alert severity="info">選択できるアカウントはありません</Alert>
            : <Stack spacing={2}>
              {state.accounts.map((item) => (
                <Button key={item.id} variant="contained" fullWidth disabled={busy} onClick={() => { void login(item.id); }}>
                  {item.name}
                </Button>
              ))}
            </Stack>)}
          {busy && <LoadingStatus message="ログインしています" />}
        </>
      )}
    </Stack>
  );
}
