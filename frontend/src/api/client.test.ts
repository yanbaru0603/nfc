import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ApiError } from './client';

const fetchMock = vi.fn<typeof fetch>();
let client: typeof import('./client');

beforeEach(async () => {
  vi.resetModules();
  fetchMock.mockReset();
  vi.stubGlobal('fetch', fetchMock);
  client = await import('./client');
});

function session(csrf_token = 'csrf-initial') {
  return Response.json({ account: null, csrf_token });
}

function apiError(status: number, code: string, message = '操作できません') {
  return Response.json({ error: { code, message } }, { status });
}

describe('API応答とエラー', () => {
  it('GETは同一オリジンのCookieとJSONのAcceptを使う', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ value: 1 }));

    await expect(client.apiGet<{ value: number }>('/api/library')).resolves.toEqual({ value: 1 });
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith('/api/library', expect.objectContaining({
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      referrerPolicy: 'no-referrer',
      headers: { Accept: 'application/json' },
    }));
  });

  it('エラー形式からHTTPステータス・コード・メッセージを取り出す', async () => {
    fetchMock.mockResolvedValueOnce(apiError(422, 'invalid_input', '入力を確認してください'));

    const result = client.apiGet('/api/library');
    await expect(result).rejects.toBeInstanceOf(client.ApiError);
    await expect(result).rejects.toMatchObject({
      status: 422,
      code: 'invalid_input',
      message: '入力を確認してください',
    });
  });

  it.each([200, 502])('非JSONの応答をApiErrorに変換する（%i）', async (status) => {
    fetchMock.mockResolvedValueOnce(new Response('<html>エラー</html>', { status }));

    const result = client.apiGet('/api/library');
    await expect(result).rejects.toBeInstanceOf(client.ApiError);
    await expect(result).rejects.toMatchObject({ status, code: 'invalid_response' });
  });

  it.each([null, {}, { error: { code: 1, message: '不正' } }])(
    'エラーJSONの形式が違う場合もApiErrorに変換する（%j）', async (body) => {
      fetchMock.mockResolvedValueOnce(Response.json(body, { status: 500 }));

      await expect(client.apiGet('/api/library')).rejects.toMatchObject({
        name: 'ApiError', status: 500, code: 'invalid_response',
      });
    },
  );

  it('通信失敗をApiErrorに変換する', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));

    const result = client.apiGet('/api/library');
    await expect(result).rejects.toBeInstanceOf(client.ApiError);
    await expect(result).rejects.toMatchObject({ status: 0, code: 'network_error' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([200, 204])('本文なしの成功応答はundefinedを返す（%i）', async (status) => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status }));

    await expect(client.apiGet<void>('/api/library')).resolves.toBeUndefined();
  });

  it('本文なしのエラー応答は成功扱いにしない', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 404 }));

    await expect(client.apiGet('/api/library')).rejects.toMatchObject({
      status: 404, code: 'invalid_response',
    });
  });

  it('APIのエラーをステータスと任意のコードで判定する', () => {
    const error = new client.ApiError(404, 'not_found', '見つかりません');

    expect(client.isApiError(error, 404)).toBe(true);
    expect(client.isApiError(error, 404, 'not_found')).toBe(true);
    expect(client.isApiError(error, 403)).toBe(false);
    expect(client.isApiError(error, 404, 'forbidden')).toBe(false);
    expect(client.isApiError(new Error('エラー'), 404)).toBe(false);
    expect(client.isApiError({ status: 404, code: 'not_found' }, 404)).toBe(false);
    expect(client.isApiError(null, 404)).toBe(false);
  });

  it('ApiError | nullの真の分岐でstatusを絞り込み、偽の分岐でもmessageを参照できる', () => {
    const describeError = (error: ApiError | null) => {
      if (client.isApiError(error, 404)) {
        const status: 404 = error.status;
        return status;
      }
      if (error) {
        return error.message;
      }
      return null;
    };

    expect(describeError(new client.ApiError(404, 'not_found', '見つかりません'))).toBe(404);
    expect(describeError(new client.ApiError(403, 'forbidden', '権限がありません'))).toBe('権限がありません');
    expect(describeError(null)).toBeNull();
  });

  it('HTMLの404はinvalid_responseになり、APIの404として判定しない', async () => {
    fetchMock.mockResolvedValueOnce(new Response('<html>SPA</html>', {
      status: 404, headers: { 'Content-Type': 'text/html' },
    }));

    const error = await client.apiGet('/api/r/token').catch((error: unknown) => error);
    expect(error).toBeInstanceOf(client.ApiError);
    expect(error).toMatchObject({ status: 404, code: 'invalid_response' });
    expect(client.isApiError(error, 404)).toBe(false);
    expect(client.isApiError(error, 404, 'invalid_response')).toBe(false);
  });
});

describe('セッションとCSRF', () => {
  it.each(['/api/auth/login', '/api/auth/logout'])(
    '%sの成功応答のCSRFトークンを次のPOSTに使う', async (path) => {
      const data = { account: null, csrf_token: 'csrf-rotated' };
      fetchMock
        .mockResolvedValueOnce(session())
        .mockResolvedValueOnce(Response.json(data))
        .mockResolvedValueOnce(Response.json({ ok: true }));

      await expect(client.apiPost(path)).resolves.toEqual(data);
      await client.apiPost('/api/r/token/claim/account');

      expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/r/token/claim/account', expect.objectContaining({
        headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-rotated' }),
      }));
      expect(fetchMock).toHaveBeenCalledTimes(3);
    },
  );

  it.each([{}, { csrf_token: null }, { csrf_token: 123 }, null, ['csrf-other'], 'csrf-other'])(
    '成功応答に文字列のcsrf_tokenがなければ既存のトークンを維持する（%j）', async (data) => {
      fetchMock
        .mockResolvedValueOnce(session())
        .mockResolvedValueOnce(Response.json(data))
        .mockResolvedValueOnce(Response.json({ ok: true }));

      await expect(client.apiPost('/api/auth/login')).resolves.toEqual(data);
      await client.apiPost('/api/r/token/claim/account');

      expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/r/token/claim/account', expect.objectContaining({
        headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-initial' }),
      }));
      expect(fetchMock).toHaveBeenCalledTimes(3);
    },
  );

  it('CSRF更新後の再送の成功応答からもトークンを更新する', async () => {
    fetchMock
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(apiError(403, 'csrf_invalid'))
      .mockResolvedValueOnce(session('csrf-refreshed'))
      .mockResolvedValueOnce(Response.json({ csrf_token: 'csrf-rotated' }))
      .mockResolvedValueOnce(Response.json({ ok: true }));

    await client.apiPost('/api/auth/login');
    await client.apiPost('/api/auth/logout');

    expect(fetchMock).toHaveBeenNthCalledWith(5, '/api/auth/logout', expect.objectContaining({
      headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-rotated' }),
    }));
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it('セッションのアカウントを返し、取得したトークンをPOSTに付ける', async () => {
    const data = { account: { id: 'demo-1', name: 'デモ太郎' }, csrf_token: 'csrf-session' };
    fetchMock
      .mockResolvedValueOnce(Response.json(data))
      .mockResolvedValueOnce(Response.json({ return_to: '/library' }));

    await expect(client.getSession()).resolves.toEqual(data);
    await client.apiPost('/api/auth/login', { account_id: 'demo-1' });

    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/auth/login', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'X-CSRF-Token': 'csrf-session',
      },
      body: JSON.stringify({ account_id: 'demo-1' }),
    }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('トークン未取得ならセッションを先に取得し、本文なしのPOSTも送れる', async () => {
    fetchMock
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(Response.json({ benefit_id: 'benefit-1' }));

    await expect(client.apiPost('/api/r/token/claim/account')).resolves.toEqual({ benefit_id: 'benefit-1' });
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/session', expect.objectContaining({ method: 'GET' }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/r/token/claim/account', expect.objectContaining({
      headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-initial' }),
      body: undefined,
    }));
  });

  it('同時のセッション取得は1回のGETにまとめる', async () => {
    fetchMock.mockResolvedValueOnce(session());

    const [first, second] = await Promise.all([client.getSession(), client.getSession()]);
    expect(first).toEqual(second);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(['古い応答が先', '古い応答が後'])(
    '強制再取得は別のGETを送り、古い応答で新しい要求やトークンを上書きしない（%s）', async (order) => {
      let resolveOld!: (response: Response) => void;
      let resolveNew!: (response: Response) => void;
      fetchMock
        .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }))
        .mockReturnValueOnce(new Promise((resolve) => { resolveNew = resolve; }))
        .mockResolvedValueOnce(Response.json({ ok: true }));

      const oldRequest = client.getSession();
      const newRequest = client.getSession({ force: true });
      expect(newRequest).not.toBe(oldRequest);
      expect(client.getSession()).toBe(newRequest);
      expect(fetchMock).toHaveBeenCalledTimes(2);

      if (order === '古い応答が先') {
        resolveOld(session('csrf-old'));
        await oldRequest;
        expect(client.getSession()).toBe(newRequest);
        resolveNew(session('csrf-new'));
        await newRequest;
      } else {
        resolveNew(session('csrf-new'));
        await newRequest;
        resolveOld(session('csrf-old'));
        await oldRequest;
      }

      await client.apiPost('/api/auth/logout');
      expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/auth/logout', expect.objectContaining({
        headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-new' }),
      }));
    },
  );

  it('明示的なセッション再取得でCSRFトークンを更新する', async () => {
    fetchMock
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(session('csrf-new'))
      .mockResolvedValueOnce(Response.json({ ok: true }));

    await client.getSession();
    await client.getSession();
    await client.apiPost('/api/auth/logout');

    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/auth/logout', expect.objectContaining({
      headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-new' }),
    }));
  });

  it.each([
    { account: null, csrf_token: '' },
    { account: null },
    { account: { id: 'demo-1' }, csrf_token: 'csrf' },
  ])('不正なセッション応答ではPOSTしない（%j）', async (body) => {
    fetchMock.mockResolvedValueOnce(Response.json(body));

    await expect(client.apiPost('/api/auth/login')).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('セッション取得失敗後も次の呼び出しで取得できる', async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(session());

    await expect(client.getSession()).rejects.toMatchObject({ code: 'network_error' });
    await expect(client.getSession()).resolves.toMatchObject({ csrf_token: 'csrf-initial' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('403・csrf_invalidなら更新したトークンで同じ本文を一度再送する', async () => {
    fetchMock
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(apiError(403, 'csrf_invalid'))
      .mockResolvedValueOnce(session('csrf-refreshed'))
      .mockResolvedValueOnce(Response.json({ ok: true }));

    await expect(client.apiPost('/api/auth/login', { account_id: 'demo-1' })).resolves.toEqual({ ok: true });
    const firstPost = fetchMock.mock.calls[1];
    const retryPost = fetchMock.mock.calls[3];
    expect(firstPost).toEqual(['/api/auth/login', expect.objectContaining({
      headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-initial' }),
    })]);
    expect(retryPost).toEqual(['/api/auth/login', expect.objectContaining({
      headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-refreshed' }),
      body: firstPost?.[1]?.body,
    })]);
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/session', expect.objectContaining({ method: 'GET' }));
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it('csrf_invalidの更新は進行中の古いGETに合流しない', async () => {
    let resolveOld!: (response: Response) => void;
    fetchMock
      .mockResolvedValueOnce(session())
      .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce(apiError(403, 'csrf_invalid'))
      .mockResolvedValueOnce(session('csrf-refreshed'))
      .mockResolvedValueOnce(Response.json({ ok: true }));
    await client.getSession();
    const oldRequest = client.getSession();

    await expect(client.apiPost('/api/auth/login')).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenNthCalledWith(4, '/api/session', expect.objectContaining({ method: 'GET' }));
    expect(fetchMock).toHaveBeenNthCalledWith(5, '/api/auth/login', expect.objectContaining({
      headers: expect.objectContaining({ 'X-CSRF-Token': 'csrf-refreshed' }),
    }));
    resolveOld(session('csrf-old'));
    await oldRequest;
  });

  it.each([200, 204])('POSTも本文なしの成功応答はundefinedを返す（%i）', async (status) => {
    fetchMock.mockResolvedValueOnce(session()).mockResolvedValueOnce(new Response(null, { status }));

    await expect(client.apiPost<void>('/api/auth/logout')).resolves.toBeUndefined();
  });

  it('再送もcsrf_invalidなら、さらに再送しない', async () => {
    fetchMock
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(apiError(403, 'csrf_invalid'))
      .mockResolvedValueOnce(session('csrf-refreshed'))
      .mockResolvedValueOnce(apiError(403, 'csrf_invalid'));

    await expect(client.apiPost('/api/r/token/claim/account')).rejects.toMatchObject({
      status: 403, code: 'csrf_invalid',
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it.each([
    [403, 'forbidden'],
    [401, 'login_required'],
    [409, 'already_claimed'],
    [500, 'csrf_invalid'],
  ])('それ以外のエラーは再送しない（%i・%s）', async (status, code) => {
    fetchMock
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(apiError(status, code));

    await expect(client.apiPost('/api/r/token/claim/account')).rejects.toMatchObject({ status, code });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('受取POSTの通信失敗では自動再送しない', async () => {
    fetchMock
      .mockResolvedValueOnce(session())
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));

    await expect(client.apiPost('/api/r/token/claim/download')).rejects.toMatchObject({
      status: 0, code: 'network_error',
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('受取POSTの応答受信中に通信が切れた場合も自動再送しない', async () => {
    const response = Response.json({ ok: true });
    vi.spyOn(response, 'text').mockRejectedValueOnce(new TypeError('terminated'));
    fetchMock.mockResolvedValueOnce(session()).mockResolvedValueOnce(response);

    await expect(client.apiPost('/api/r/token/claim/download')).rejects.toMatchObject({
      status: 0, code: 'network_error',
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('受取POSTの非JSON応答でも自動再送しない', async () => {
    fetchMock
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(new Response('<html>エラー</html>', { status: 502 }));

    await expect(client.apiPost('/api/r/token/claim/download')).rejects.toMatchObject({
      status: 502, code: 'invalid_response',
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('CSRF更新のセッション取得に失敗した場合はPOSTを再送しない', async () => {
    fetchMock
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(apiError(403, 'csrf_invalid'))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));

    await expect(client.apiPost('/api/r/token/claim/account')).rejects.toMatchObject({ code: 'network_error' });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

describe('リクエストの中止', () => {
  it.each(['GET', 'POST'])('%sのfetchのAbortErrorを変換せず、signalを渡す', async (method) => {
    const controller = new AbortController();
    const error = new DOMException('中止しました', 'AbortError');
    if (method === 'POST') {
      fetchMock.mockResolvedValueOnce(session());
      await client.getSession();
    }
    fetchMock.mockImplementationOnce(async () => {
      controller.abort(error);
      throw error;
    });

    const request = method === 'GET'
      ? client.apiGet('/api/library', { signal: controller.signal })
      : client.apiPost('/api/auth/logout', undefined, { signal: controller.signal });
    await expect(request).rejects.toBe(error);
    expect(fetchMock).toHaveBeenLastCalledWith(expect.any(String), expect.objectContaining({
      signal: controller.signal,
    }));
    expect(fetchMock).toHaveBeenCalledTimes(method === 'GET' ? 1 : 2);
  });

  it.each(['GET', 'POST'])('%sの本文受信中のAbortErrorも変換しない', async (method) => {
    const controller = new AbortController();
    const error = new DOMException('中止しました', 'AbortError');
    const response = Response.json({ ok: true });
    vi.spyOn(response, 'text').mockImplementationOnce(async () => {
      controller.abort(error);
      throw error;
    });
    if (method === 'POST') {
      fetchMock.mockResolvedValueOnce(session());
    }
    fetchMock.mockResolvedValueOnce(response);

    const request = method === 'GET'
      ? client.apiGet('/api/library', { signal: controller.signal })
      : client.apiPost('/api/auth/logout', undefined, { signal: controller.signal });
    await expect(request).rejects.toBe(error);
    expect(fetchMock).toHaveBeenCalledTimes(method === 'GET' ? 1 : 2);
  });

  describe.each(['GET', 'POST'])('%sの中止理由', (method) => {
    it.each(['fetch', '本文受信'])('%s中のabort("stop")の文字列を変換しない', async (phase) => {
      const controller = new AbortController();
      const fail = async () => {
        controller.abort('stop');
        throw controller.signal.reason;
      };
      if (method === 'POST') {
        fetchMock.mockResolvedValueOnce(session());
        await client.getSession();
      }
      if (phase === 'fetch') {
        fetchMock.mockImplementationOnce(fail);
      } else {
        const response = Response.json({ ok: true });
        vi.spyOn(response, 'text').mockImplementationOnce(fail);
        fetchMock.mockResolvedValueOnce(response);
      }

      const request = method === 'GET'
        ? client.apiGet('/api/library', { signal: controller.signal })
        : client.apiPost('/api/auth/logout', undefined, { signal: controller.signal });

      await expect(request).rejects.toBe('stop');
      expect(fetchMock).toHaveBeenCalledTimes(method === 'GET' ? 1 : 2);
    });

    it.each(['fetch', '本文受信'])('%s中のAbortSignal.timeout()のTimeoutErrorを変換しない', async (phase) => {
      if (method === 'POST') {
        fetchMock.mockResolvedValueOnce(session());
        await client.getSession();
      }
      const signal = AbortSignal.timeout(0);
      const fail = () => new Promise<never>((_, reject) => {
        if (signal.aborted) {
          reject(signal.reason);
        } else {
          signal.addEventListener('abort', () => { reject(signal.reason); }, { once: true });
        }
      });
      if (phase === 'fetch') {
        fetchMock.mockImplementationOnce(fail);
      } else {
        const response = Response.json({ ok: true });
        vi.spyOn(response, 'text').mockImplementationOnce(fail);
        fetchMock.mockResolvedValueOnce(response);
      }

      const request = method === 'GET'
        ? client.apiGet('/api/library', { signal })
        : client.apiPost('/api/auth/logout', undefined, { signal });

      await expect(request).rejects.toMatchObject({ name: 'TimeoutError' });
      await expect(request).rejects.toBe(signal.reason);
      expect(fetchMock).toHaveBeenCalledTimes(method === 'GET' ? 1 : 2);
    });
  });

  it.each(['fetch', '本文受信'])('signalが中止されていない%sのAbortErrorは通信失敗として扱う', async (phase) => {
    const controller = new AbortController();
    const error = new DOMException('別の理由による失敗', 'AbortError');
    if (phase === 'fetch') {
      fetchMock.mockRejectedValueOnce(error);
    } else {
      const response = Response.json({ ok: true });
      vi.spyOn(response, 'text').mockRejectedValueOnce(error);
      fetchMock.mockResolvedValueOnce(response);
    }

    await expect(client.apiGet('/api/library', { signal: controller.signal })).rejects.toMatchObject({
      status: 0, code: 'network_error',
    });
    expect(controller.signal.aborted).toBe(false);
  });

  it('POSTの中止済みsignalではセッションもPOSTも送らない', async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(client.apiPost('/api/auth/logout', undefined, { signal: controller.signal }))
      .rejects.toBe(controller.signal.reason);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('初回のセッション取得中に中止されたPOSTを送らない', async () => {
    const controller = new AbortController();
    let resolveSession!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((resolve) => { resolveSession = resolve; }));
    const request = client.apiPost('/api/auth/login', undefined, { signal: controller.signal });
    controller.abort();
    resolveSession(session());

    await expect(request).rejects.toBe(controller.signal.reason);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('CSRF更新中に中止されたPOSTを再送しない', async () => {
    const controller = new AbortController();
    const error = new DOMException('中止しました', 'AbortError');
    fetchMock
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(apiError(403, 'csrf_invalid'))
      .mockImplementationOnce(async () => {
        controller.abort(error);
        return session('csrf-refreshed');
      });

    await expect(client.apiPost('/api/auth/logout', undefined, { signal: controller.signal }))
      .rejects.toBe(error);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
