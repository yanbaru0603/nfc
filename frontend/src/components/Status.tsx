import { Alert, AlertTitle, Button, CircularProgress, Stack, Typography } from '@mui/material';

export function LoadingStatus({ message = '読み込み中です' }: { message?: string }) {
  return (
    <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }} role="status">
      <CircularProgress size={24} aria-hidden="true" />
      <Typography>{message}</Typography>
    </Stack>
  );
}

type ErrorStatusProps = {
  message: string;
  title?: string;
  onRetry?: () => void;
  retryDisabled?: boolean;
};

export function ErrorStatus({ message, title = 'エラーが発生しました', onRetry, retryDisabled }: ErrorStatusProps) {
  return (
    <Alert severity="error">
      <AlertTitle>{title}</AlertTitle>
      <Typography>{message}</Typography>
      {onRetry && <Button onClick={onRetry} disabled={retryDisabled}>再試行</Button>}
    </Alert>
  );
}
