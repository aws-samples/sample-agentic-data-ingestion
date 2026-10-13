// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Component tests for the thin useWebSocket hook over the WebSocketProvider (R9).
 *
 * useWebSocket is now a thin subscription wrapper: it registers pipeline topics
 * with the app-wide Socket_Singleton and reflects the shared connection state.
 * These tests render it inside a real WebSocketProvider and verify the
 * behaviour consumers rely on (connect on mount, subscribe frame, connected
 * state, cache refresh by invalidation, empty-ids guard, fallback-polling flag).
 *
 * Provider-level concerns (single shared socket, re-subscribe on id change,
 * reconnect on `online`, backoff) live in WebSocketProvider.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { useWebSocket } from '../useWebSocket';
import {
  CACHE_INVALIDATION_DEBOUNCE_MS,
  WebSocketProvider,
} from '../../providers/WebSocketProvider';

// Mock react-oidc-context so useAuth() returns a minimal auth state.
// The provider sends the ID token (the WS $connect authorizer accepts
// id/access token_use); give access_token a distinct value to prove id_token
// is used.
vi.mock('react-oidc-context', () => ({
  useAuth: () => ({
    user: { id_token: 'test-token-123', access_token: 'access-token-456' },
    isAuthenticated: true,
  }),
}));

// Mock runtime config (config.json) — the provider reads the WS URL from
// getConfig().wsUrl, not a build-time env var.
const mocks = vi.hoisted(() => ({
  wsUrl: 'wss://test.example.com/prod',
}));

vi.mock('../../config', () => ({
  getConfig: () => ({ wsUrl: mocks.wsUrl }),
}));

// ---------------------------------------------------------------------------
// Mock WebSocket
// ---------------------------------------------------------------------------

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  url: string;
  readyState = MockWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn(() => {
    this.readyState = MockWebSocket.CLOSED;
  });

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  simulateMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }

  simulateClose() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(
      QueryClientProvider,
      { client: queryClient },
      React.createElement(WebSocketProvider, null, children),
    );
  return { wrapper, queryClient };
}

function latestWs(): MockWebSocket {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

// ---------------------------------------------------------------------------
// Setup / Teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  MockWebSocket.instances = [];
  mocks.wsUrl = 'wss://test.example.com/prod';
  vi.stubGlobal('WebSocket', MockWebSocket);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useWebSocket (thin hook over WebSocketProvider)', () => {
  it('connects to the configured wsUrl (with token) on mount', () => {
    const { wrapper } = createWrapper();
    renderHook(() => useWebSocket({ pipelineIds: ['pipe-1'] }), { wrapper });

    expect(MockWebSocket.instances).toHaveLength(1);
    expect(latestWs().url).toBe(
      'wss://test.example.com/prod?token=test-token-123',
    );
  });

  it('sends subscribe message with pipeline IDs after connection opens', () => {
    const { wrapper } = createWrapper();
    renderHook(() => useWebSocket({ pipelineIds: ['pipe-1', 'pipe-2'] }), {
      wrapper,
    });

    act(() => latestWs().simulateOpen());

    expect(latestWs().send).toHaveBeenCalledWith(
      JSON.stringify({
        action: 'subscribe',
        pipeline_ids: ['pipe-1', 'pipe-2'],
      }),
    );
  });

  it('sets isConnected=true after open (and isFallbackPolling=false)', () => {
    const { wrapper } = createWrapper();
    const { result } = renderHook(
      () => useWebSocket({ pipelineIds: ['pipe-1'] }),
      { wrapper },
    );

    act(() => latestWs().simulateOpen());

    expect(result.current.isConnected).toBe(true);
    expect(result.current.isFallbackPolling).toBe(false);
  });

  it('reports isFallbackPolling=true before the socket opens', () => {
    const { wrapper } = createWrapper();
    const { result } = renderHook(
      () => useWebSocket({ pipelineIds: ['pipe-1'] }),
      { wrapper },
    );

    // Socket created but not yet open → consumers keep polling.
    expect(result.current.isConnected).toBe(false);
    expect(result.current.isFallbackPolling).toBe(true);
  });

  // REWRITTEN at p47 F2 — deliberate, and legible as such (`LGR-60c4985-6`).
  //
  // This test used to be called `updates React Query cache on execution_update
  // message` and asserted the two fields `updateCache`'s optimistic
  // `setQueriesData` patch wrote (`status`, `current_step`), plus that sibling
  // rows were left alone. That patch is what p47 R15.3 deleted, so the old
  // assertions could only be preserved by keeping the thing the requirement
  // removes. Unlike the sibling amendment in `WebSocketProvider.test.tsx` —
  // where the cache write was merely the observable for a test about malformed
  // frames — the patch was this test's whole subject, so the test is rewritten
  // rather than re-pointed.
  //
  // The replacement pins the contract that now holds: a frame refreshes the
  // affected queries by invalidation and writes nothing itself. Provider-level
  // coalescing, prefix targeting and unmount cleanup are covered in
  // `WebSocketProvider.test.tsx`; this file keeps the consumer-visible half.
  it('refreshes the cache by invalidation on execution_update, patching nothing', () => {
    vi.useFakeTimers();
    try {
      const { wrapper, queryClient } = createWrapper();

      // Seed the cache with existing execution data.
      queryClient.setQueryData(['executions', 'pipe-1', 1], {
        executions: [
          { run_id: 'run-1', status: 'RUNNING', current_step: 'extract' },
          { run_id: 'run-2', status: 'COMPLETED', current_step: 'done' },
        ],
      });
      queryClient.setQueryData(['executions', 'other-pipe', 1], {
        executions: [{ run_id: 'run-9', status: 'RUNNING', current_step: 'extract' }],
      });

      renderHook(() => useWebSocket({ pipelineIds: ['pipe-1'] }), { wrapper });
      act(() => latestWs().simulateOpen());

      act(() =>
        latestWs().simulateMessage({
          type: 'execution_update',
          pipeline_id: 'pipe-1',
          run_id: 'run-1',
          status: 'COMPLETED',
          current_step: 'load',
          updated_at: '2025-01-15T12:00:00Z',
        }),
      );
      act(() => vi.advanceTimersByTime(CACHE_INVALIDATION_DEBOUNCE_MS));

      // The pipeline's page is marked for refetch…
      expect(
        queryClient.getQueryState(['executions', 'pipe-1', 1])?.isInvalidated,
      ).toBe(true);
      // …and another pipeline's is not.
      expect(
        queryClient.getQueryState(['executions', 'other-pipe', 1])?.isInvalidated,
      ).toBe(false);

      // No row was patched. These entries have no queryFn, so an invalidation
      // cannot refill them — the frame's `status: 'COMPLETED'` reaching
      // `run-1` here would mean the deleted patch had come back.
      const cached = queryClient.getQueryData(['executions', 'pipe-1', 1]) as {
        executions: Array<{
          run_id: string;
          status: string;
          current_step: string;
        }>;
      };
      expect(cached.executions[0].status).toBe('RUNNING');
      expect(cached.executions[0].current_step).toBe('extract');
      expect(cached.executions[1].status).toBe('COMPLETED');
      expect(cached.executions[1].current_step).toBe('done');
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not send subscribe when pipelineIds is empty', () => {
    const { wrapper } = createWrapper();
    renderHook(() => useWebSocket({ pipelineIds: [] }), { wrapper });

    // No topics → no socket opened, nothing to subscribe.
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('does not connect when disabled', () => {
    const { wrapper } = createWrapper();
    const { result } = renderHook(
      () => useWebSocket({ pipelineIds: ['pipe-1'], enabled: false }),
      { wrapper },
    );

    expect(MockWebSocket.instances).toHaveLength(0);
    expect(result.current.isConnected).toBe(false);
    expect(result.current.isFallbackPolling).toBe(true);
  });

  it('stays disconnected (fallback polling) when wsUrl is not configured', () => {
    mocks.wsUrl = '';
    const { wrapper } = createWrapper();
    const { result } = renderHook(
      () => useWebSocket({ pipelineIds: ['pipe-1'] }),
      { wrapper },
    );

    expect(result.current.isFallbackPolling).toBe(true);
    expect(MockWebSocket.instances).toHaveLength(0);
  });
});
