import type { ApiErrorBody, SessionResponse } from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

export function isApiError<S extends number>(error: unknown, status: S, code?: string): error is ApiError & { status: S } {
  return error instanceof ApiError &&
    error.code !== 'invalid_response' &&
    error.status === status &&
    (code === undefined || error.code === code);
}

type ApiRequestInit = { signal?: AbortSignal };

let csrfToken: string | null = null;
let sessionRequest: Promise<SessionResponse> | null = null;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  return (
    isRecord(value) &&
    isRecord(value.error) &&
    typeof value.error.code === 'string' &&
    typeof value.error.message === 'string'
  );
}

function isSessionResponse(value: unknown): value is SessionResponse {
  return (
    isRecord(value) &&
    typeof value.csrf_token === 'string' &&
    value.csrf_token.length > 0 &&
    (value.account === null ||
      (isRecord(value.account) &&
        typeof value.account.id === 'string' &&
        typeof value.account.name === 'string'))
  );
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      cache: 'no-store',
      referrerPolicy: 'no-referrer',
    });
  } catch (error) {
    if (init.signal?.aborted) {
      throw error;
    }
    throw new ApiError(0, 'network_error', '通信に失敗しました。接続を確認してください。');
  }

  if (response.ok && response.status === 204) {
    return undefined as T;
  }

  let text: string;
  try {
    text = await response.text();
  } catch (error) {
    if (init.signal?.aborted) {
      throw error;
    }
    throw new ApiError(0, 'network_error', '応答の受信中に通信が途切れました。接続を確認してください。');
  }

  if (response.ok && text === '') {
    return undefined as T;
  }

  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    throw new ApiError(response.status, 'invalid_response', 'サーバーから正しいJSON応答を取得できませんでした。');
  }

  if (!response.ok) {
    if (isApiErrorBody(data)) {
      throw new ApiError(response.status, data.error.code, data.error.message);
    }
    throw new ApiError(response.status, 'invalid_response', 'サーバーのエラー応答の形式が正しくありません。');
  }

  return data as T;
}

// pathには /api/session など、/api から始まるパスを渡す
export function apiGet<T>(path: string, init?: ApiRequestInit): Promise<T> {
  return request<T>(path, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    signal: init?.signal,
  });
}

export function getSession({ force = false }: { force?: boolean } = {}): Promise<SessionResponse> {
  // 同時取得をまとめ、初回のCookie発行が競合するのを防ぐ
  if (force || !sessionRequest) {
    const pending = apiGet<unknown>('/api/session').then((data) => {
      if (!isSessionResponse(data)) {
        throw new ApiError(200, 'invalid_response', 'セッション応答の形式が正しくありません。');
      }
      // 強制再取得より前の応答で、新しいトークンを上書きしない
      if (sessionRequest === pending) {
        csrfToken = data.csrf_token;
      }
      return data;
    }).finally(() => {
      if (sessionRequest === pending) {
        sessionRequest = null;
      }
    });
    sessionRequest = pending;
  }

  return sessionRequest;
}

export async function apiPost<T>(path: string, body?: unknown, init?: ApiRequestInit): Promise<T> {
  init?.signal?.throwIfAborted();
  const token = csrfToken ?? (await getSession()).csrf_token;

  const serializedBody = body === undefined ? undefined : JSON.stringify(body);
  const send = async (csrf: string) => {
    init?.signal?.throwIfAborted();
    const data = await request<T>(path, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf,
      },
      body: serializedBody,
      signal: init?.signal,
    });
    if (isRecord(data) && !Array.isArray(data) && typeof data.csrf_token === 'string') {
      csrfToken = data.csrf_token;
    }
    return data;
  };

  try {
    return await send(token);
  } catch (error) {
    if (!isApiError(error, 403, 'csrf_invalid')) {
      throw error;
    }
  }

  // CSRF不正で拒否された場合だけ更新して一度再送する。通信失敗では再送しない
  csrfToken = null;
  const session = await getSession({ force: true });
  return send(session.csrf_token);
}
