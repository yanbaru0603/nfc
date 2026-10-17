import { AppBar, Container, Stack, Toolbar, Typography } from '@mui/material';
import type { ReactNode } from 'react';
import { useState } from 'react';
import { useSession } from '../session';
import { ErrorStatus, LoadingStatus } from './Status';

export function Layout({ children }: { children: ReactNode }) {
  const { account, loading, error, refresh } = useSession();
  const [retrying, setRetrying] = useState(false);

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

  return (
    <>
      <AppBar position="static">
        <Toolbar sx={{ flexWrap: 'wrap', gap: 1, py: 1 }}>
          <Typography variant="h6" component="div" sx={{ flexGrow: 1 }}>
            NFCグッズ特典受け取り
          </Typography>
          {account && <Typography sx={{ overflowWrap: 'anywhere' }}>{account.name}</Typography>}
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
              retryDisabled={retrying}
            />
          )}
          {children}
        </Stack>
      </Container>
    </>
  );
}
