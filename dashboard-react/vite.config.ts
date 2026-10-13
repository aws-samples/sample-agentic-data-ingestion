// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { defineConfig, loadEnv, searchForWorkspaceRoot } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');

  // The proxy target must be the CloudFront distribution URL (not the raw
  // execute-api URL) because the regional WAF blocks requests missing the
  // X-Origin-Verify header that only CloudFront injects.
  //
  // Set VITE_API_PROXY_TARGET in .env.local, e.g.:
  //   VITE_API_PROXY_TARGET=https://d1234abcdef8.cloudfront.net
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || 'https://CLOUDFRONT_DOMAIN.cloudfront.net';

  // Test runs only: MetricsDashboardPage.seam.test.tsx reads the shared API
  // fixtures in ../tests/fixtures/api_responses via `new URL(..., import.meta.url)`,
  // which vite's server.fs.allow check denies under vitest 4. Setting `allow`
  // replaces the default, so the workspace root is re-listed. `vite dev` is unchanged.
  const isTest = mode === 'test' || !!process.env.VITEST;

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      proxy: {
        '/api': {
          target: apiProxyTarget,
          changeOrigin: true,
          secure: true,
        },
      },
      ...(isTest
        ? {
            fs: {
              allow: [
                searchForWorkspaceRoot(process.cwd()),
                path.resolve(__dirname, '../tests/fixtures/api_responses'),
              ],
            },
          }
        : {}),
    },
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: ['./src/test-setup.ts'],
      // vitest 4 enforces this on long synchronous renders; vitest 2 did not (slowest passing test ~22 s).
      testTimeout: 60000,
    },
  };
});

