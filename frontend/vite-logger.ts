import { createLogger } from 'vite';
import type { Logger } from 'vite';

export function redactReceiveTokens(message: string): string {
  return message.replace(/\/r\/[A-Za-z0-9_-]+/g, '/r/***');
}

export function createRedactedLogger(): Logger {
  const logger = createLogger();
  const { info, warn, warnOnce, error } = logger;
  logger.info = (message, options) => info(redactReceiveTokens(message), options);
  logger.warn = (message, options) => warn(redactReceiveTokens(message), options);
  logger.warnOnce = (message, options) => warnOnce(redactReceiveTokens(message), options);
  logger.error = (message, options) => error(redactReceiveTokens(message), options);
  return logger;
}
