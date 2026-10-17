// @vitest-environment node
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRedactedLogger, redactReceiveTokens } from './vite-logger.ts';

afterEach(() => { vi.restoreAllMocks(); });

describe('Viteログの受取トークン', () => {
  it.each([
    ['/r/token-123_ABC', '/r/***'],
    ['http proxy error: /api/r/token-123/claim/account\nECONNREFUSED', 'http proxy error: /api/r/***/claim/account\nECONNREFUSED'],
    ['https://example.com/r/token/download?x=1#top', 'https://example.com/r/***/download?x=1#top'],
    ['"/r/first" と \'/api/r/second/archive\'', '"/r/***" と \'/api/r/***/archive\''],
    ['\u001b[31m/api/r/token\u001b[39m', '\u001b[31m/api/r/***\u001b[39m'],
    ['/api/library と /r/ と /r/***', '/api/library と /r/ と /r/***'],
  ])('URL中のトークンを伏せ、他の情報を残す（%s）', (message, expected) => {
    expect(redactReceiveTokens(message)).toBe(expected);
  });

  it.each(['info', 'warn', 'warnOnce', 'error'] as const)('%sの出力前にトークンを伏せる', (method) => {
    const output = vi.spyOn(console, method === 'info' ? 'log' : method === 'warnOnce' ? 'warn' : method)
      .mockImplementation(() => {});
    const logger = createRedactedLogger();

    logger[method]('proxy: /api/r/secret-token/download');

    expect(output).toHaveBeenCalledWith('proxy: /api/r/***/download');
    if (method !== 'info') {
      expect(logger.hasWarned).toBe(true);
    }
  });

  it('errorのオプションと記録済みエラーの判定を維持する', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const logger = createRedactedLogger();
    const error = new Error('ECONNREFUSED');

    logger.error('/api/r/secret-token', { error });

    expect(logger.hasErrorLogged(error)).toBe(true);
  });
});
