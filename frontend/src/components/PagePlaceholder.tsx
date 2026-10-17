import { Stack, Typography } from '@mui/material';
import type { ReactNode } from 'react';

export function PagePlaceholder({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <Stack spacing={2}>
      <Typography variant="h1" component="h1">{title}</Typography>
      {children}
      <Typography>準備中</Typography>
    </Stack>
  );
}
