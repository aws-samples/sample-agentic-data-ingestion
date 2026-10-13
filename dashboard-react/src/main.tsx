// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider, QueryCache } from '@tanstack/react-query';
import '@cloudscape-design/global-styles/index.css';
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import App from './App';
import { AuthProvider } from './auth';
import { WebSocketProvider } from './providers/WebSocketProvider';
import { loadConfig } from './config';
import { ApiError } from './services/api-client';
import { showGlobalToast } from './services/global-toast';

// Apply saved theme preference
const savedMode = localStorage.getItem('agentic-data-ingestion-theme') as 'light' | 'dark' | null;
applyMode(savedMode === 'dark' ? Mode.Dark : Mode.Light);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
        return failureCount < 2;
      },
    },
  },
  queryCache: new QueryCache({
    onError: (error, query) => {
      // Skip toast for queries that opt out (e.g., data preview 404s)
      if (query.meta?.suppressToast) return;
      // Skip 404s globally — they're typically "not found yet" states, not real errors
      if (error instanceof ApiError && error.status === 404) return;
      // Skip 401s — the ApiClient triggers a single signinRedirect on session
      // expiry, so we must not surface a toast per failed request (toast storm).
      if (error instanceof ApiError && error.status === 401) return;
      if (error instanceof ApiError) {
        showGlobalToast(error.message, 'error');
      }
    },
  }),
});

async function bootstrap() {
  await loadConfig();

  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AuthProvider>
            <WebSocketProvider>
              <App />
            </WebSocketProvider>
          </AuthProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </React.StrictMode>,
  );
}

bootstrap().catch((err) => {
  // If config fails to load, show a minimal error page
  const root = document.getElementById('root');
  if (root) {
    root.innerHTML = `<div style="padding:2rem;font-family:sans-serif">
      <h1>Failed to load application configuration</h1>
      <p>${err instanceof Error ? err.message : String(err)}</p>
      <p>Check that <code>/config.json</code> is available or Vite env vars are set.</p>
    </div>`;
  }
});
