import { Link, Typography } from '@mui/material';
import { Link as RouterLink } from 'react-router';
import { PagePlaceholder } from '../components/PagePlaceholder';
import { useSession } from '../session';

export function LibraryPage() {
  const { account, loading, error } = useSession();

  return (
    <PagePlaceholder title="ライブラリ">
      {account && <Typography>{account.name}さんのライブラリ</Typography>}
      {!account && !loading && !error && (
        <Typography>
          ライブラリを確認するには<Link component={RouterLink} to="/login">ログイン</Link>してください
        </Typography>
      )}
    </PagePlaceholder>
  );
}
