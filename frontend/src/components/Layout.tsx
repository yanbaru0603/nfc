import { AppBar, Button, Container, Stack, Toolbar, Typography } from '@mui/material';
import type { ReactNode } from 'react';
import { useRef, useState } from 'react';
import { Link as RouterLink, useLocation, useNavigate } from 'react-router';
import { apiPost, isApiError } from '../api/client';
import type { LogoutResponse } from '../api/types';
import { useSession } from '../session';
import { ErrorStatus, LoadingStatus } from './Status';

export function Layout({ children }: { children: ReactNode }) {
  const { account, loading, error, refresh } = useSession();
  const [retrying, setRetrying] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const logoutPending = useRef(false);
  const navigate = useNavigate();
  const { pathname } = useLocation();

  const retry = async () => {
    setRetrying(true);
    try {
      await refresh();
    } catch {
      // 取得失敗はSessionProviderのerrorに反映される
    } finally {
      setRetrying(false);
    }
  };

  const logout = async () => {
    if (logoutPending.current) return;
    logoutPending.current = true;
    setLoggingOut(true);
    setLogoutError(null);
    let loggedOut = false;
    try {
      try {
        const data = await apiPost<LogoutResponse>('/api/auth/logout');
        if (!data || typeof data.csrf_token !== 'string') throw new Error('invalid_response');
      } catch (error: unknown) {
        if (!isApiError(error, 401)) throw error;
      }
      loggedOut = true;
      await refresh();
      if (pathname === '/library' || pathname.startsWith('/library/')) {
        await navigate('/login', { replace: true });
      }
    } catch {
      setLogoutError(loggedOut
        ? 'ログアウトしました。表示を更新できませんでした。'
        : 'ログアウトを完了できませんでした。ログイン状態を確認して、もう一度お試しください。');
    } finally {
      logoutPending.current = false;
      setLoggingOut(false);
    }
  };

  return (
    <>
      <AppBar position="static">
        <Toolbar sx={{ flexWrap: 'wrap', gap: 1, py: 1 }}>
          <Typography variant="h6" component="div" sx={{ flexGrow: 1 }}>
            NFCグッズ特典受け取り
          </Typography>
          {account ? (
            <>
              <Typography sx={{ overflowWrap: 'anywhere' }}>{account.name}</Typography>
              <Button color="inherit" disabled={loggingOut || retrying} onClick={() => { void logout(); }}>ログアウト</Button>
            </>
          ) : !loading && !error && <Button component={RouterLink} to="/login" color="inherit">ログイン</Button>}
        </Toolbar>
      </AppBar>
      <Container component="main" maxWidth="sm" sx={{ py: 3 }}>
        <Stack spacing={3}>
          {loading && <LoadingStatus message="ログイン状態を確認中です" />}
          {error && (
            <ErrorStatus
              title="ログイン状態を確認できませんでした"
              message={error.message}
              onRetry={() => { void retry(); }}
              retryDisabled={retrying || loggingOut}
            />
          )}
          {logoutError && <ErrorStatus message={logoutError} />}
          {children}
        </Stack>
      </Container>
    </>
  );
}
