// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Tests for WebSocketProvider — the Socket_Singleton (R9).
 *
 * Validates: Requirements 9.1, 9.2, 9.3, 9.4
 *  - 9.1 One shared socket multiplexes subscriptions (two consumers → 1 socket).
 *  - 9.2 Subscriptions re-sent on (re)open and when the subscribed id changes.
 *  - 9.3 Reconnection with bounded exponential backoff + immediate retry on
 *        `navigator.onLine` restoration (`online` event) and visibilitychange.
 *  - 9.4 Covered by the single-socket, re-subscribe-on-id-change, and
 *        reconnect-on-online tests below.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { useWebSocket } from '../../hooks/useWebSocket';
import {
  CACHE_INVALIDATION_DEBOUNCE_MS,
  CACHE_INVALIDATION_MAX_WAIT_MS,
  WebSocketProvider,
} from '../WebSocketProvider';
import { executionKeys, runKeys } from '../../hooks/query-keys';

const mocks = vi.hoisted(() => ({
  wsUrl: 'wss://test.example.com/prod',
  // Mutable so the token-refresh case can hand out a fresh token on re-render;
  // every other test keeps the fixed default.
  idToken: 'tok-1' as string | null,
}));

// useAuth() → a token from `mocks` so URL/token behaviour is deterministic.
vi.mock('react-oidc-context', () => ({
  useAuth: () => ({
    user: mocks.idToken === null ? undefined : { id_token: mocks.idToken },
    isAuthenticated: mocks.idToken !== null,
  }),
}));

vi.mock('../../config', () => ({
  getConfig: () => ({ wsUrl: mocks.wsUrl }),
}));

// ---------------------------------------------------------------------------
// Mock WebSocket with readyState + static constants (the provider reads
// WebSocket.OPEN / CONNECTING / CLOSED off the global).
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

function openInstances(): MockWebSocket[] {
  return MockWebSocket.instances;
}

/** The query keys an `invalidateQueries` spy was called with, JSON-comparable. */
function invalidatedKeys(spy: { mock: { calls: unknown[][] } }): string[] {
  return spy.mock.calls.map((c) =>
    JSON.stringify((c[0] as { queryKey?: unknown })?.queryKey),
  );
}

/** One `execution_update` frame, as `ws_broadcast` builds it (`:55-63`). */
function execFrame(overrides: Record<string, unknown> = {}) {
  return {
    type: 'execution_update',
    pipeline_id: 'pipe-1',
    run_id: 'run-1',
    status: 'RUNNING',
    current_step: 'schema_mapping',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  MockWebSocket.instances = [];
  mocks.wsUrl = 'wss://test.example.com/prod';
  mocks.idToken = 'tok-1';
  vi.stubGlobal('WebSocket', MockWebSocket);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// 9.1 — single shared socket across two consumers
// ---------------------------------------------------------------------------

describe('WebSocketProvider — single shared socket (R9.1)', () => {
  it('opens exactly one socket for two consumers', () => {
    const { wrapper } = createWrapper();

    renderHook(
      () => {
        useWebSocket({ pipelineIds: ['pipe-1'] });
        useWebSocket({ pipelineIds: ['pipe-2'] });
      },
      { wrapper },
    );

    expect(openInstances()).toHaveLength(1);
  });

  it('multiplexes both consumers subscriptions onto the one socket', () => {
    const { wrapper } = createWrapper();

    renderHook(
      () => {
        useWebSocket({ pipelineIds: ['pipe-1'] });
        useWebSocket({ pipelineIds: ['pipe-2'] });
      },
      { wrapper },
    );

    act(() => openInstances()[0].simulateOpen());

    // On open, the provider re-sends ALL registered topics in one frame.
    expect(openInstances()[0].send).toHaveBeenCalledWith(
      JSON.stringify({
        action: 'subscribe',
        pipeline_ids: ['pipe-1', 'pipe-2'],
      }),
    );
    expect(openInstances()).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 9.2 — re-subscribe on id change (same socket)
// ---------------------------------------------------------------------------

describe('WebSocketProvider — re-subscribe on id change (R9.2)', () => {
  it('sends a new subscribe frame for the new id on the same socket', () => {
    const { wrapper } = createWrapper();

    const { rerender } = renderHook(
      ({ id }: { id: string }) => useWebSocket({ pipelineIds: [id] }),
      { wrapper, initialProps: { id: 'pipe-a' } },
    );

    act(() => openInstances()[0].simulateOpen());
    expect(openInstances()[0].send).toHaveBeenCalledWith(
      JSON.stringify({ action: 'subscribe', pipeline_ids: ['pipe-a'] }),
    );

    // Change the subscribed id → the hook re-registers via effect deps.
    act(() => rerender({ id: 'pipe-b' }));

    // Same socket (no new instance) and a subscribe frame for the new id.
    expect(openInstances()).toHaveLength(1);
    expect(openInstances()[0].send).toHaveBeenCalledWith(
      JSON.stringify({ action: 'subscribe', pipeline_ids: ['pipe-b'] }),
    );
  });

  it('re-sends all registered topics on reconnect (not only in onopen)', () => {
    const { wrapper } = createWrapper();

    renderHook(() => useWebSocket({ pipelineIds: ['pipe-1'] }), { wrapper });

    // First open → subscribe.
    act(() => openInstances()[0].simulateOpen());
    expect(openInstances()[0].send).toHaveBeenCalledWith(
      JSON.stringify({ action: 'subscribe', pipeline_ids: ['pipe-1'] }),
    );

    // Drop and reconnect via the online signal.
    act(() => openInstances()[0].simulateClose());
    act(() => window.dispatchEvent(new Event('online')));
    expect(openInstances()).toHaveLength(2);

    // The reconnected socket re-sends the subscription on open.
    act(() => openInstances()[1].simulateOpen());
    expect(openInstances()[1].send).toHaveBeenCalledWith(
      JSON.stringify({ action: 'subscribe', pipeline_ids: ['pipe-1'] }),
    );
  });
});

// ---------------------------------------------------------------------------
// 9.3 — reconnect on online / visibilitychange, bounded backoff
// ---------------------------------------------------------------------------

describe('WebSocketProvider — reconnect (R9.3)', () => {
  it('reconnects immediately on the window `online` event', () => {
    const { wrapper } = createWrapper();

    renderHook(() => useWebSocket({ pipelineIds: ['pipe-1'] }), { wrapper });
    expect(openInstances()).toHaveLength(1);

    act(() => openInstances()[0].simulateOpen());
    act(() => openInstances()[0].simulateClose());

    // Immediate retry on connectivity restoration.
    act(() => window.dispatchEvent(new Event('online')));

    expect(openInstances()).toHaveLength(2);
  });

  it('reconnects when the tab becomes visible again', () => {
    const { wrapper } = createWrapper();

    renderHook(() => useWebSocket({ pipelineIds: ['pipe-1'] }), { wrapper });
    act(() => openInstances()[0].simulateOpen());
    act(() => openInstances()[0].simulateClose());

    // Simulate tab returning to the foreground.
    const spy = vi
      .spyOn(document, 'visibilityState', 'get')
      .mockReturnValue('visible');
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    spy.mockRestore();

    expect(openInstances()).toHaveLength(2);
  });

  it('schedules exponential backoff (1s → 2s) on repeated drops', () => {
    const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout');
    const { wrapper } = createWrapper();

    renderHook(() => useWebSocket({ pipelineIds: ['pipe-1'] }), { wrapper });

    // First drop → 1s backoff scheduled.
    act(() => openInstances()[0].simulateClose());
    expect(setTimeoutSpy.mock.calls.some((c) => c[1] === 1000)).toBe(true);

    // Fire the scheduled reconnect, then drop again → 2s backoff.
    const firstReconnect = setTimeoutSpy.mock.calls.find((c) => c[1] === 1000);
    act(() => (firstReconnect![0] as () => void)());
    expect(openInstances()).toHaveLength(2);

    act(() => openInstances()[1].simulateClose());
    expect(setTimeoutSpy.mock.calls.some((c) => c[1] === 2000)).toBe(true);

    setTimeoutSpy.mockRestore();
  });

  it('does not permanently fall back after 3 retries (unlimited attempts)', () => {
    const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout');
    const { wrapper } = createWrapper();

    const { result } = renderHook(
      () => useWebSocket({ pipelineIds: ['pipe-1'] }),
      { wrapper },
    );

    // Drop and reconnect four times — the old hook stopped retrying after 3.
    for (let i = 0; i < 4; i++) {
      act(() => openInstances()[openInstances().length - 1].simulateClose());
      const lastTimer =
        setTimeoutSpy.mock.calls[setTimeoutSpy.mock.calls.length - 1];
      act(() => (lastTimer[0] as () => void)());
    }

    // A 5th socket was created (attempt 4 → reconnect), proving retries persist.
    expect(openInstances().length).toBeGreaterThanOrEqual(5);
    // While disconnected the hook reports fallback polling (temporary).
    expect(result.current.isConnected).toBe(false);
    expect(result.current.isFallbackPolling).toBe(true);

    setTimeoutSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// Token refresh reconnect, and frame resilience.
//
// **Validates: Requirements 16.2**
//
// Added by p30 task 6.6. WebSocketController was on R16.2's zero-coverage list,
// but that flag was wrong: the class is module-private and only reachable
// through WebSocketProvider, so the 20 tests in this file and
// hooks/__tests__/useWebSocket.test.ts already exercise it (including the
// `?token=` ID-token contract and the runtime-config wsUrl guard). What genuinely
// had no coverage were the two behaviours below, both documented at the class:
// "on token refresh the provider reconnects with the fresh token" and "a
// malformed frame must never tear down the socket".
//
// The $connect Lambda authorizer validates the Cognito JWT carried in
// `?token=<jwt>` (LGR-2d64f7a-1), so a socket left holding an expired token is
// rejected at the next reconnect — the refresh path is load-bearing, not
// cosmetic.
// ---------------------------------------------------------------------------

describe('WebSocketProvider — token refresh (LGR-2d64f7a-1)', () => {
  it('reconnects with the fresh token when the ID token is refreshed', () => {
    const { wrapper } = createWrapper();

    const { rerender } = renderHook(
      () => useWebSocket({ pipelineIds: ['pipe-1'] }),
      { wrapper },
    );
    act(() => openInstances()[0].simulateOpen());
    expect(openInstances()[0].url).toBe(
      'wss://test.example.com/prod?token=tok-1',
    );

    // Silent renew hands out a new ID token.
    mocks.idToken = 'tok-2';
    act(() => rerender());

    // The stale socket is dropped and replaced by one carrying the new token.
    expect(openInstances()).toHaveLength(2);
    expect(openInstances()[1].url).toBe(
      'wss://test.example.com/prod?token=tok-2',
    );
    expect(openInstances()[0].close).toHaveBeenCalled();
  });

  it('appends the token with & when the configured wsUrl already has a query', () => {
    mocks.wsUrl = 'wss://test.example.com/prod?stage=dev';
    const { wrapper } = createWrapper();

    renderHook(() => useWebSocket({ pipelineIds: ['pipe-1'] }), { wrapper });

    expect(openInstances()[0].url).toBe(
      'wss://test.example.com/prod?stage=dev&token=tok-1',
    );
  });
});

describe('WebSocketProvider — malformed frames', () => {
  // Fake timers because the "frame was processed" observable is now a DEBOUNCED
  // invalidation rather than a synchronous cache write (p47 R15.4).
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('ignores an unparseable frame and keeps the socket usable', () => {
    const { wrapper, queryClient } = createWrapper();
    queryClient.setQueryData(['executions', 'pipe-1'], {
      executions: [{ run_id: 'run-1', status: 'RUNNING', current_step: 'read' }],
    });

    renderHook(() => useWebSocket({ pipelineIds: ['pipe-1'] }), { wrapper });
    act(() => openInstances()[0].simulateOpen());

    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    act(() => openInstances()[0].onmessage?.({ data: '{ not json' }));
    warn.mockRestore();

    // No teardown, no reconnect churn.
    expect(openInstances()).toHaveLength(1);
    expect(openInstances()[0].close).not.toHaveBeenCalled();

    // AMENDED at p47 F2 — deliberate, and legible as such (`LGR-60c4985-6`).
    //
    // This test's subject is unchanged: a malformed frame must not stop the
    // NEXT well-formed frame from being processed. What changed is the
    // observable it uses to see that. It used to read the frame's `status` back
    // out of the cache — which worked only because `updateCache` applied an
    // optimistic `setQueriesData` patch, and that patch is exactly what R15.3
    // deleted (it was overwritten by the refetch on the next line, and could
    // never carry the other twenty properties the row renders). Reading cache
    // CONTENT would now assert the absence of a patch, which is a different
    // test — it is the one below, `does not patch the cached row`.
    //
    // The frame's effect is now an invalidation, so that is what is asserted.
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    act(() =>
      openInstances()[0].simulateMessage({
        type: 'execution_update',
        pipeline_id: 'pipe-1',
        run_id: 'run-1',
        status: 'COMPLETED',
        current_step: 'load',
      }),
    );
    act(() => vi.advanceTimersByTime(CACHE_INVALIDATION_DEBOUNCE_MS));

    expect(invalidatedKeys(invalidateSpy)).toContain(
      JSON.stringify(['executions', 'pipe-1']),
    );
  });
});

describe('WebSocketProvider — connection lifecycle', () => {
  it('does not open a socket until a topic is subscribed', () => {
    const { wrapper } = createWrapper();

    renderHook(() => useWebSocket({ pipelineIds: [] }), { wrapper });

    expect(openInstances()).toHaveLength(0);
  });

  it('shares one socket when a consumer changes id (no socket churn)', () => {
    const { wrapper } = createWrapper();

    const { rerender } = renderHook(
      ({ id }: { id: string }) => useWebSocket({ pipelineIds: [id] }),
      { wrapper, initialProps: { id: 'pipe-a' } },
    );
    act(() => openInstances()[0].simulateOpen());

    act(() => rerender({ id: 'pipe-b' }));
    act(() => rerender({ id: 'pipe-c' }));

    expect(openInstances()).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Cache refresh strategy — p47 F2.
//
// **Validates: Requirements 15.1, 15.2, 15.3, 15.4, 15.5**
//
// One strategy, not two: a frame invalidates and never patches (R15.1), the
// patch is gone rather than dormant (R15.3), a burst costs one refetch per key
// (R15.4), and the invalidation still matches by page-key PREFIX so it reaches
// a user sitting on page 2 (R15.5).
//
// R15.2 is discharged by the strategy choice rather than by an assertion here:
// it applies only if the patch is kept, and the frame cannot carry the row —
// `ExecutionUpdate` has six keys against the 23 properties `ExecutionsPage`
// reads, and `load_outcome` is a read-path derivation that `ws_broadcast`'s
// stream image structurally cannot see (`LGR-9e76249-2`). See `updateCache`.
// ---------------------------------------------------------------------------

describe('WebSocketProvider — cache refresh strategy (p47 R15)', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  function mountSubscribed() {
    const { wrapper, queryClient } = createWrapper();
    renderHook(() => useWebSocket({ pipelineIds: ['pipe-1'] }), { wrapper });
    act(() => openInstances()[0].simulateOpen());
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    return { queryClient, invalidateSpy, socket: openInstances()[0] };
  }

  it('invalidates the execution prefix and the run detail once per frame', () => {
    const { invalidateSpy, socket } = mountSubscribed();

    act(() => socket.simulateMessage(execFrame()));
    act(() => vi.advanceTimersByTime(CACHE_INVALIDATION_DEBOUNCE_MS));

    expect(invalidatedKeys(invalidateSpy)).toEqual([
      JSON.stringify([...executionKeys.all, 'pipe-1']),
      JSON.stringify(runKeys.detail('run-1')),
    ]);
  });

  it('does not invalidate before the debounce window elapses (R15.4)', () => {
    const { invalidateSpy, socket } = mountSubscribed();

    act(() => socket.simulateMessage(execFrame()));
    act(() => vi.advanceTimersByTime(CACHE_INVALIDATION_DEBOUNCE_MS - 1));

    expect(invalidateSpy).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(1));
    expect(invalidateSpy).toHaveBeenCalled();
  });

  it('does not patch the cached row — a frame writes nothing into the cache (R15.3)', () => {
    const { queryClient, socket } = mountSubscribed();
    // A row as the read path produced it: the derived status the UI renders,
    // plus the co-varying fields the frame does not carry.
    queryClient.setQueryData(executionKeys.list('pipe-1', 1), {
      executions: [
        {
          run_id: 'run-1',
          status: 'WAITING_APPROVAL',
          current_step: 'schema_mapping',
          pending_approval_id: 'appr-1',
          steps_status: { schema_mapping: 'waiting_approval' },
          load_outcome: { outcome: 'rows_loaded' },
        },
      ],
    });

    // The frame carries the STORED status, which is not what the UI renders.
    act(() => socket.simulateMessage(execFrame({ status: 'RUNNING' })));
    act(() => vi.advanceTimersByTime(CACHE_INVALIDATION_MAX_WAIT_MS));

    const cached = queryClient.getQueryData(
      executionKeys.list('pipe-1', 1),
    ) as { executions: Array<Record<string, unknown>> };
    // Untouched. There is no queryFn on this cache entry, so an invalidation
    // cannot refill it — anything other than the original row would be a patch.
    expect(cached.executions[0]).toEqual({
      run_id: 'run-1',
      status: 'WAITING_APPROVAL',
      current_step: 'schema_mapping',
      pending_approval_id: 'appr-1',
      steps_status: { schema_mapping: 'waiting_approval' },
      load_outcome: { outcome: 'rows_loaded' },
    });
  });

  it('coalesces the three frames one step report produces into one refetch per key (R15.4)', () => {
    const { invalidateSpy, socket } = mountSubscribed();

    // `execution_updater` issues 2–3 `update_item` calls per step report
    // (`:340`, `:350`, `:371`); each is its own stream record, so `ws_broadcast`
    // posts three frames that differ only in `updated_at`.
    act(() => {
      socket.simulateMessage(execFrame({ updated_at: 't1' }));
      socket.simulateMessage(execFrame({ updated_at: 't2' }));
      socket.simulateMessage(execFrame({ updated_at: 't3' }));
    });
    act(() => vi.advanceTimersByTime(CACHE_INVALIDATION_DEBOUNCE_MS));

    // Two calls total — one per distinct key — not six.
    expect(invalidateSpy).toHaveBeenCalledTimes(2);
    expect(invalidatedKeys(invalidateSpy)).toEqual([
      JSON.stringify([...executionKeys.all, 'pipe-1']),
      JSON.stringify(runKeys.detail('run-1')),
    ]);
  });

  it('coalesces per distinct pipeline and run, not across them (R15.4)', () => {
    const { invalidateSpy, socket } = mountSubscribed();

    act(() => {
      socket.simulateMessage(execFrame({ run_id: 'run-1' }));
      socket.simulateMessage(execFrame({ run_id: 'run-2' }));
      socket.simulateMessage(execFrame({ pipeline_id: 'pipe-2', run_id: 'run-2' }));
    });
    act(() => vi.advanceTimersByTime(CACHE_INVALIDATION_DEBOUNCE_MS));

    const keys = invalidatedKeys(invalidateSpy);
    expect(keys).toContain(JSON.stringify([...executionKeys.all, 'pipe-1']));
    expect(keys).toContain(JSON.stringify([...executionKeys.all, 'pipe-2']));
    expect(keys).toContain(JSON.stringify(runKeys.detail('run-1')));
    expect(keys).toContain(JSON.stringify(runKeys.detail('run-2')));
    // Two pipelines + two runs = four, with no duplicate for the repeated run.
    expect(keys).toHaveLength(4);
  });

  it('reaches every page of the pipeline by prefix, and no other pipeline (R15.5)', () => {
    const { queryClient, socket } = mountSubscribed();
    // The user is on page 2. The key is page-scoped, so an exact-key
    // invalidation would miss this entry entirely.
    queryClient.setQueryData(executionKeys.list('pipe-1', 2), { executions: [] });
    queryClient.setQueryData(executionKeys.list('pipe-1', 3), { executions: [] });
    queryClient.setQueryData(executionKeys.list('pipe-2', 1), { executions: [] });

    act(() => socket.simulateMessage(execFrame()));
    act(() => vi.advanceTimersByTime(CACHE_INVALIDATION_DEBOUNCE_MS));

    expect(
      queryClient.getQueryState(executionKeys.list('pipe-1', 2))?.isInvalidated,
    ).toBe(true);
    expect(
      queryClient.getQueryState(executionKeys.list('pipe-1', 3))?.isInvalidated,
    ).toBe(true);
    // A different pipeline's list is not collateral damage.
    expect(
      queryClient.getQueryState(executionKeys.list('pipe-2', 1))?.isInvalidated,
    ).toBe(false);
  });

  it('flushes within the max-wait cap when frames never stop arriving (R15.4)', () => {
    const { invalidateSpy, socket } = mountSubscribed();

    // A frame every 100ms — each one resets a pure trailing debounce, so
    // without the cap the flush would be deferred indefinitely. The `__all__`
    // default subscription (`ws_connect:49-57`) makes this stream reachable.
    for (let elapsed = 0; elapsed < CACHE_INVALIDATION_MAX_WAIT_MS * 3; elapsed += 100) {
      act(() => socket.simulateMessage(execFrame({ updated_at: `t${elapsed}` })));
      act(() => vi.advanceTimersByTime(100));
    }

    expect(invalidateSpy).toHaveBeenCalled();
    // Three cap windows of sustained traffic → a small number of flushes, not
    // one per frame (30 frames were delivered).
    const flushes = invalidateSpy.mock.calls.length / 2;
    expect(flushes).toBeLessThanOrEqual(4);
  });

  it('drops a pending flush on unmount rather than invalidating a torn-down client (R15.4)', () => {
    const { wrapper, queryClient } = createWrapper();
    const { unmount } = renderHook(
      () => useWebSocket({ pipelineIds: ['pipe-1'] }),
      { wrapper },
    );
    act(() => openInstances()[0].simulateOpen());
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    act(() => openInstances()[0].simulateMessage(execFrame()));
    expect(vi.getTimerCount()).toBeGreaterThan(0); // the flush is pending

    // Unmount inside the debounce window, with the flush still pending.
    act(() => unmount());

    // The timer is CLEARED, not merely made harmless. A surviving timer would
    // hold the controller (and through it the socket) until it fired, so the
    // `disposed` guard inside the flush is a second line of defence rather than
    // the cleanup. No query data is ever set in this test, so React Query holds
    // no gc timers of its own and this count is the provider's alone.
    expect(vi.getTimerCount()).toBe(0);

    act(() => vi.advanceTimersByTime(CACHE_INVALIDATION_MAX_WAIT_MS * 2));
    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});
