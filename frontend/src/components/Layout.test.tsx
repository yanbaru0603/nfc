import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { useSession } from '../session';
import { Layout } from './Layout';

vi.mock('../session', () => ({ useSession: vi.fn() }));

beforeEach(() => { vi.mocked(useSession).mockReset(); });

describe('セッションの再試行ボタン', () => {
  it.each(['成功', '失敗'])('再試行中は無効になり、%s後に再び有効になる', async (outcome) => {
    let resolveRefresh!: (account: null) => void;
    let rejectRefresh!: (error: ApiError) => void;
    const error = new ApiError(0, 'network_error', '通信に失敗しました');
    const refresh = vi.fn(() => new Promise<null>((resolve, reject) => {
      resolveRefresh = resolve;
      rejectRefresh = reject;
    }));
    vi.mocked(useSession).mockReturnValue({ account: null, loading: false, error, refresh });
    const user = userEvent.setup();
    render(<Layout>本文</Layout>);
    const button = screen.getByRole('button', { name: '再試行' });

    await user.click(button);
    expect(button).toBeDisabled();
    button.click();
    expect(refresh).toHaveBeenCalledTimes(1);

    if (outcome === '成功') {
      resolveRefresh(null);
    } else {
      rejectRefresh(error);
    }
    await waitFor(() => expect(button).toBeEnabled());
  });
});
