import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { ReactNode } from 'react';
import { ApiError, getSession } from './api/client';
import type { Account } from './api/types';

type SessionState = {
  account: Account | null;
  loading: boolean;
  error: ApiError | null;
};

type SessionContextValue = SessionState & {
  refresh: () => Promise<Account | null>;
};

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({
    account: null,
    loading: true,
    error: null,
  });
  const requestId = useRef(0);
  const hasLoaded = useRef(false);

  const loadSession = useCallback(async (force: boolean): Promise<Account | null> => {
    const id = ++requestId.current;
    const loading = !hasLoaded.current;
    setState((previous) => ({ ...previous, loading, error: null }));
    try {
      const session = await getSession({ force });
      if (requestId.current === id) {
        hasLoaded.current = true;
        setState({ account: session.account, loading: false, error: null });
      }
      return session.account;
    } catch (error) {
      const sessionError = error instanceof ApiError
        ? error
        : new ApiError(0, 'network_error', 'セッションを取得できませんでした。');
      if (requestId.current === id) {
        setState((previous) => ({
          ...previous,
          loading: false,
          error: sessionError,
        }));
      }
      throw sessionError;
    }
  }, []);

  const refresh = useCallback(() => loadSession(true), [loadSession]);

  useEffect(() => {
    void loadSession(false).catch(() => {});
    return () => {
      // 画面を離れた後の応答とStrictModeの古い応答を反映しない
      requestId.current += 1;
    };
  }, [loadSession]);

  const value = useMemo(() => ({ ...state, refresh }), [state, refresh]);
  return <SessionContext value={value}>{children}</SessionContext>;
}

export function useSession(): SessionContextValue {
  const session = useContext(SessionContext);
  if (!session) {
    throw new Error('useSessionはSessionProvider内で使用してください。');
  }
  return session;
}
