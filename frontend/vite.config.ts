import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';
import { createRedactedLogger } from './vite-logger.ts';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  const host = env.VITE_HOST;

  return {
    customLogger: createRedactedLogger(),
    plugins: [react()],
    server: {
      host: host === 'true' ? true : host === 'false' ? false : host || '127.0.0.1',
      proxy: {
        '/api': {
          target: env.API_ORIGIN || 'http://127.0.0.1:8000',
          changeOrigin: false,
        },
      },
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      unstubGlobals: true,
    },
  };
});
