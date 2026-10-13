// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * WebSocketProvider — the Socket_Singleton (R9).
 *
 * One module-level WebSocket connection, shared app-wide via React context,
 * multiplexing every subscription instead of opening a socket per consumer
 * hook instance. Replaces the previous per-hook socket that opened one
 * connection per `useWebSocket` call (RunDetailPage opened two).
 *
 * Responsibilities:
 *  - Subscription registry keyed by topic ({type, id}). `subscribe()` returns
 *    an `unsubscribe()`.
 *  - Sends subscribe frames on: (a) registration while the socket is open,
 *    (b) socket (re)open — re-sending ALL registered topics (fixes the old
 *    onopen-only bug where re-subscription was lost after a reconnect), and
 *    (c) when a subscribed id changes (the hook re-registers via effect deps).
 *  - Reconnection: bounded exponential backoff (1s → 30s cap), unlimited
 *    attempts, with immediate retries on `window` `online` and `document`
 *    `visibilitychange` (when visible). While disconnected the provider
 *    exposes `connected: false` so consumers' polling fallback stays active —
 *    now a *temporary* fallback rather than the old permanent fall-to-polling
 *    after MAX_RETRIES.
 *  - Token: the connection URL carries the current Cognito ID token via the
 *    existing `?token=` scheme; on token refresh the provider reconnects with
 *    the fresh token.
 *  - Cache refresh (p47 R15): a frame INVALIDATES the affected query keys and
 *    never patches cached rows. Invalidations are coalesced on a trailing
 *    debounce so a burst of frames costs one refetch per key. See
 *    `updateCache` for why patching is not an available strategy here.
 *
 * `useWebSocket` (see hooks/useWebSocket.ts) is a thin wrapper that registers
 * topics with this provider and reads `connected`.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useAuth } from 'react-oidc-context';
import { executionKeys, runKeys } from '../hooks/query-keys';
import { getConfig } from '../config';
import type { ExecutionUpdate, WebSocketMessage } from '../types';

/** A subscription topic. `type` is a category ('pipeline' today); `id` is the
 *  concrete subscribed entity (a pipeline id). */
export interface WebSocketTopic {
  type: 'pipeline' | string;
  id: string;
}

/** Optional per-topic message handler. The provider always refreshes the React
 *  Query cache centrally (by coalesced invalidation — it never patches cached
 *  rows, see `updateCache`); handlers are an extra hook for consumers that want
 *  raw frames. */
export type WebSocketMessageHandler = (message: ExecutionUpdate) => void;

export interface WebSocketContextValue {
  /** True while the shared socket is open. */
  connected: boolean;
  /** Current reconnect attempt count (0 when connected or idle). */
  retryCount: number;
  /** Register interest in a topic; returns an idempotent unsubscribe fn. */
  subscribe: (
    topic: WebSocketTopic,
    handler?: WebSocketMessageHandler,
  ) => () => void;
}

const BASE_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;

/**
 * Trailing-debounce window (ms) for cache invalidation (p47 R15.4).
 *
 * Sized from the burst the writer actually produces, not from taste. One step
 * report costs **two or three** writes to the same executions row —
 * `execution_updater/lambda_function.py:340` (status / current_step /
 * completed_at), `:350` (the nested `steps_status.<step>` entry) and `:371`
 * (the lineage append on a successful step) — issued as consecutive `update_item`
 * calls in one invocation. Each is a separate MODIFY on the table's stream, so
 * `ws_broadcast` posts a separate frame for each (`ws_broadcast:55-84`, one
 * `post_to_connection` per record, `batchSize: 10` at `compute-stack.ts:1031`).
 * Frames 2 and 3 are identical to frame 1 apart from `updated_at`, because the
 * frame carries neither `steps_status` nor `lineage`.
 *
 * 250 ms comfortably exceeds the intra-burst spacing of those writes while
 * staying inside the band where a UI still reads as immediate, and it is an
 * order of magnitude below the fastest poll (`EXECUTIONS_ACTIVE_POLL_MS`, 8 s)
 * and two below the connected backstop (`EXECUTIONS_BACKGROUND_POLL_MS`, 30 s).
 * `useWizardDraft`'s 400 ms was NOT reused: that window is sized for human
 * typing cadence, and 400 ms is long enough to be perceptible on the common
 * case of a single isolated frame.
 */
export const CACHE_INVALIDATION_DEBOUNCE_MS = 250;

/**
 * Hard cap (ms) on how long a flush may be deferred by arriving frames (p47
 * R15.4).
 *
 * A trailing debounce alone can be starved by a frame stream whose gaps never
 * exceed the window, and that stream is reachable here rather than
 * hypothetical: `$connect` stores every new connection with a default
 * `pipeline_id: "__all__"` subscription (`ws_connect:49-57`) which is only
 * removed when the client's first `subscribe` frame is processed (`:69-75`), and
 * `_get_subscribers` queries both that pipeline and `__all__`
 * (`ws_broadcast:104-114`), so between open and that frame a tab receives an
 * execution update for **every** pipeline in the account.
 *
 * Starvation was confirmed by mutation, not assumed: with this cap removed and a
 * frame every 100 ms for 3 s, the flush never fired at all. So the cap is what
 * makes the deferral bound hold by construction rather than by an assumption
 * about load. That is the same *shape* of argument `LGR-14d6dd3-2` makes for its
 * fan-out deadline — cited as precedent for the reasoning only; that record's
 * scope is a synchronous dashboard listing and does not reach this file.
 */
export const CACHE_INVALIDATION_MAX_WAIT_MS = 1_000;

function topicKey(topic: WebSocketTopic): string {
  return `${topic.type}:${topic.id}`;
}

/**
 * Default context value so `useWebSocket` used outside a provider degrades to
 * "disconnected" (consumers keep polling) instead of throwing. In the wired
 * app the real provider always supplies a value.
 */
const WebSocketContext = createContext<WebSocketContextValue>({
  connected: false,
  retryCount: 0,
  subscribe: () => () => {},
});

interface Registration {
  topic: WebSocketTopic;
  handlers: Set<WebSocketMessageHandler>;
  refCount: number;
}

interface ControllerDeps {
  getQueryClient: () => QueryClient;
  onConnectedChange: (connected: boolean) => void;
  onRetryChange: (retry: number) => void;
}

/**
 * Framework-agnostic connection controller. Instantiated once per provider and
 * held in a ref, so its identity (and the `subscribe` it exposes) is stable
 * across renders.
 */
class WebSocketController {
  private ws: WebSocket | null = null;
  private readonly registry = new Map<string, Registration>();
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private intentionalClose = false;
  /** Pipeline ids whose execution list needs a refetch on the next flush. */
  private readonly pendingPipelineIds = new Set<string>();
  /** Run ids whose detail needs a refetch on the next flush. */
  private readonly pendingRunIds = new Set<string>();
  private flushTimer: ReturnType<typeof setTimeout> | undefined;
  /** Wall-clock ceiling for the current burst's flush; unset between bursts. */
  private flushDeadline: number | undefined;
  /** True once at least one topic has been registered — keeps the shared
   *  socket alive (and auto-reconnecting) for the provider's lifetime. */
  private shouldConnect = false;
  private token: string | null = null;
  /** The token the currently-open socket was created with (for refresh diff). */
  private socketToken: string | null = null;
  private disposed = false;

  constructor(private readonly deps: ControllerDeps) {}

  /** Register interest in a topic. Bound arrow so it can be passed by identity. */
  subscribe = (
    topic: WebSocketTopic,
    handler?: WebSocketMessageHandler,
  ): (() => void) => {
    const key = topicKey(topic);
    let reg = this.registry.get(key);
    if (!reg) {
      reg = { topic, handlers: new Set(), refCount: 0 };
      this.registry.set(key, reg);
    }
    reg.refCount += 1;
    if (handler) reg.handlers.add(handler);

    this.shouldConnect = true;
    this.ensureConnected();
    // If the socket is already open, register the new topic immediately by
    // re-sending the full subscription set (idempotent server-side).
    this.sendSubscriptions();

    let active = true;
    return () => {
      if (!active) return;
      active = false;
      const r = this.registry.get(key);
      if (!r) return;
      if (handler) r.handlers.delete(handler);
      r.refCount -= 1;
      if (r.refCount <= 0) {
        this.registry.delete(key);
        this.sendUnsubscribe(topic);
      }
    };
  };

  /** Update the desired token (used by the next connect). */
  setToken(token: string | null): void {
    this.token = token;
  }

  /** Reconnect with the freshest token if the live socket used a stale one. */
  syncToken(): void {
    if (this.disposed) return;
    if (this.token === this.socketToken) return;
    if (!this.ws) return; // no live socket → next connect picks up the token
    this.reconnectNow(true);
  }

  /** Attach global reconnect listeners; recover a connection after remount. */
  mount(): void {
    this.disposed = false;
    window.addEventListener('online', this.handleOnline);
    document.addEventListener('visibilitychange', this.handleVisibility);
    if (this.shouldConnect) this.ensureConnected();
  }

  /** Detach listeners, cancel timers, and close the socket. */
  dispose(): void {
    this.disposed = true;
    window.removeEventListener('online', this.handleOnline);
    document.removeEventListener('visibilitychange', this.handleVisibility);
    clearTimeout(this.timer);
    // A pending invalidation flush is DROPPED, not flushed (p47 R15.4). The
    // provider is going away, so the QueryClient it would invalidate may be
    // torn down with it, and any remount refetches on mount anyway. Flushing
    // here would be a write from a disposed controller for no observable gain.
    //
    // Both halves are load-bearing and were verified by mutation: removing the
    // `clearTimeout` leaves a live timer holding this controller (a leak even
    // though the `disposed` guard in `flushInvalidations` would make it a
    // no-op), and removing that guard lets a timer scheduled a moment before
    // unmount invalidate a client that is being torn down.
    clearTimeout(this.flushTimer);
    this.flushTimer = undefined;
    this.flushDeadline = undefined;
    this.pendingPipelineIds.clear();
    this.pendingRunIds.clear();
    this.intentionalClose = true;
    const ws = this.ws;
    if (ws) {
      ws.onopen = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
  }

  private readonly handleOnline = (): void => this.reconnectNow(false);

  private readonly handleVisibility = (): void => {
    if (document.visibilityState === 'visible') this.reconnectNow(false);
  };

  private pipelineIds(): string[] {
    const ids: string[] = [];
    for (const reg of this.registry.values()) {
      if (reg.topic.type === 'pipeline') ids.push(reg.topic.id);
    }
    return ids;
  }

  private sendSubscriptions(): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const ids = this.pipelineIds();
    if (ids.length === 0) return;
    const msg: WebSocketMessage = { action: 'subscribe', pipeline_ids: ids };
    ws.send(JSON.stringify(msg));
  }

  private sendUnsubscribe(topic: WebSocketTopic): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (topic.type !== 'pipeline') return;
    const msg: WebSocketMessage = {
      action: 'unsubscribe',
      pipeline_ids: [topic.id],
    };
    ws.send(JSON.stringify(msg));
  }

  private ensureConnected(): void {
    if (this.disposed) return;
    const ws = this.ws;
    if (
      ws &&
      (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)
    ) {
      return; // already have (or opening) a socket
    }
    clearTimeout(this.timer);
    this.retry = 0;
    this.deps.onRetryChange(0);
    this.connect();
  }

  /** Immediate reconnect from an external signal (online / visible / token). */
  private reconnectNow(force: boolean): void {
    if (this.disposed || !this.shouldConnect) return;
    const ws = this.ws;
    if (
      !force &&
      ws &&
      (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)
    ) {
      return; // already healthy
    }
    clearTimeout(this.timer);
    this.retry = 0;
    this.deps.onRetryChange(0);
    if (ws && force) {
      // Token refresh: drop the stale socket without triggering auto-reconnect.
      this.intentionalClose = true;
      ws.onclose = null;
      ws.onerror = null;
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
    this.connect();
  }

  private connect(): void {
    if (this.disposed) return;
    const existing = this.ws;
    if (
      existing &&
      (existing.readyState === WebSocket.CONNECTING ||
        existing.readyState === WebSocket.OPEN)
    ) {
      return; // never open a second socket
    }

    // The WebSocket URL comes from runtime config (config.json), not a
    // build-time env var — unset in the deployed bundle would otherwise pin the
    // app to polling forever. Guard defensively; an empty/absent URL means
    // "stay disconnected and let consumers poll".
    let wsUrl: string | undefined;
    try {
      wsUrl = getConfig().wsUrl;
    } catch {
      wsUrl = undefined;
    }
    if (!wsUrl) {
      this.deps.onConnectedChange(false);
      return;
    }

    // Append the ID token for the $connect authorizer (accepts id/access
    // token_use); the REST client sends the ID token too.
    const token = this.token;
    let url = wsUrl;
    if (token) {
      const sep = wsUrl.includes('?') ? '&' : '?';
      url = `${wsUrl}${sep}token=${encodeURIComponent(token)}`;
    }

    this.intentionalClose = false;
    this.socketToken = token;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => {
      this.retry = 0;
      this.deps.onRetryChange(0);
      this.deps.onConnectedChange(true);
      // Re-send ALL registered topics on every (re)open.
      this.sendSubscriptions();
    };

    ws.onmessage = (event: MessageEvent) => this.handleMessage(event);

    ws.onclose = () => {
      this.deps.onConnectedChange(false);
      if (this.ws === ws) this.ws = null;
      if (this.intentionalClose) return;
      if (!this.shouldConnect) return;
      this.scheduleReconnect();
    };

    ws.onerror = () => {
      // A closed socket during StrictMode cleanup must not throw.
      if (ws.readyState !== WebSocket.CLOSED) {
        try {
          ws.close();
        } catch {
          /* ignore */
        }
      }
    };
  }

  private scheduleReconnect(): void {
    if (this.disposed) return;
    const attempt = (this.retry += 1);
    this.deps.onRetryChange(attempt);
    // Exponential backoff: 1s, 2s, 4s, 8s … capped at 30s; unlimited attempts.
    const delay = Math.min(BASE_BACKOFF_MS * 2 ** (attempt - 1), MAX_BACKOFF_MS);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.connect(), delay);
  }

  private handleMessage(event: MessageEvent): void {
    // A malformed frame must never tear down the socket — log and ignore.
    let data: ExecutionUpdate;
    try {
      data = JSON.parse(event.data) as ExecutionUpdate;
    } catch (err) {
      console.warn('WebSocketProvider: ignoring malformed message frame', err);
      return;
    }
    if (data.type !== 'execution_update') return;

    this.updateCache(data);

    const reg = this.registry.get(
      topicKey({ type: 'pipeline', id: data.pipeline_id }),
    );
    reg?.handlers.forEach((handler) => {
      try {
        handler(data);
      } catch (err) {
        console.warn('WebSocketProvider: subscription handler threw', err);
      }
    });
  }

  /**
   * Record what a frame invalidates and schedule one coalesced flush.
   *
   * **This writes nothing into the cache.** The name is kept because p47 R15
   * and the record that governs this strategy cite it, but the body is now
   * "note the affected keys, debounce, refetch" — invalidation only (R15.1).
   *
   * The optimistic `setQueriesData` patch that used to run here first was
   * **deleted** (R15.3), not merely bypassed. Two reasons, and the second is
   * the one that makes patch-only unavailable rather than merely worse:
   *
   * 1. It was dead work. The `invalidateQueries` on the same prefix ran on the
   *    very next line, and `refetchQueries` defaults `cancelRefetch: true`
   *    (`query-core/queryClient.cjs:204`), so the refetch it triggered
   *    overwrote the patch every time.
   * 2. **The frame cannot carry the row.** `ExecutionUpdate` (`types.ts:1003`)
   *    has six keys, of which the patch applied two (`status`,
   *    `current_step`). `ExecutionsPage` reads **23** distinct properties off
   *    an execution row (`ExecutionsPage.tsx:45-240`), and the frame overlaps
   *    three of them (`run_id`, `status`, `current_step`). The ones that
   *    co-vary with `status` are all in the missing twenty: `steps_status`
   *    drives the step-icon strip
   *    (`ExecutionsPage.tsx:159-170`), `pending_approval_id` gates the
   *    Approve/Reject buttons (`:180`), `self_healing_status` the heal badge
   *    (`:63`), and `completed_at` is written by a different `update_item`
   *    than the one the frame came from. So a status flip to SUCCESS or
   *    WAITING_APPROVAL under patch-only would render with a stale step strip
   *    and no approval action — R15.2's "every field the UI renders" is not
   *    satisfiable from this payload.
   *
   *    Nor is it satisfiable by widening the payload. `load_outcome` (`:88`)
   *    and `actual_cost` are **read-path derivations** computed by
   *    `get_pipeline_executions` from other tables' rows
   *    (`pipelines_runs.py:412`, `:422`) and deliberately never persisted, per
   *    `LGR-9e76249-2` (template / ratified) — which rejected deriving them on
   *    the DynamoDB stream precisely because "its retry semantics make a
   *    derivation failure a stalled shard". `ws_broadcast` sits on that same
   *    stream and sees only the executions-table image, so those fields are
   *    structurally out of its reach. Note too that the frame carries the
   *    **stored** status while the UI renders a **derived** one
   *    (`pipelines_runs.py:296-299` rewrites it to WAITING_APPROVAL whenever
   *    `pending_approval_id` is present); the two agree today only because one
   *    writer sets both in a single update (`approval_handler:527-533`).
   *
   * Invalidating rather than patching is also the house convention, stated
   * where it was last decided: `use-schemas.ts:338-341` rejected a
   * `setQueryData` write-through as "a value that is right for 60 seconds and
   * wrong afterwards", and `useApprovalAction.onSuccess` refreshes four key
   * prefixes and patches nothing. Consumers that genuinely need the raw frame
   * without waiting for a refetch already have the per-topic handler seam in
   * {@link handleMessage} — no consumer uses it today.
   */
  private updateCache(data: ExecutionUpdate): void {
    this.pendingPipelineIds.add(data.pipeline_id);
    if (data.run_id) this.pendingRunIds.add(data.run_id);
    this.scheduleInvalidationFlush();
  }

  /**
   * Trailing debounce with a hard ceiling, so *n* frames in a burst cost one
   * refetch per key instead of *n* (R15.4).
   *
   * Coalescing has to be explicit because React Query does the opposite of
   * deduplicating here: `invalidateQueries` → `refetchQueries` defaults
   * `cancelRefetch: true` (`queryClient.cjs:204`), and `Query.fetch` with that
   * flag and existing data calls `this.cancel({ silent: true })` before
   * starting again (`query.cjs:203-204`). So three frames used to mean three
   * HTTP requests with the first two cancelled in flight — per key, and there
   * are two keys.
   */
  private scheduleInvalidationFlush(): void {
    if (this.disposed) return;
    const now = Date.now();
    if (this.flushDeadline === undefined) {
      this.flushDeadline = now + CACHE_INVALIDATION_MAX_WAIT_MS;
    }
    const delay = Math.max(
      0,
      Math.min(CACHE_INVALIDATION_DEBOUNCE_MS, this.flushDeadline - now),
    );
    clearTimeout(this.flushTimer);
    this.flushTimer = setTimeout(() => this.flushInvalidations(), delay);
  }

  private flushInvalidations(): void {
    this.flushTimer = undefined;
    this.flushDeadline = undefined;
    const pipelineIds = [...this.pendingPipelineIds];
    const runIds = [...this.pendingRunIds];
    this.pendingPipelineIds.clear();
    this.pendingRunIds.clear();
    if (this.disposed) return;

    const qc = this.deps.getQueryClient();
    // PREFIX TARGETING, PRESERVED (R15.5). The execution query key is
    // page-scoped — `executionKeys.list(pipelineKey, page)` is
    // `['executions', id, page]` (`query-keys.ts:97`) — so an exact-key
    // invalidation would miss a user sitting on page 2. `invalidateQueries`
    // matches by key PREFIX, so `['executions', id]` reaches every page of
    // that pipeline and no other pipeline's list. This is the same property
    // the deleted `setQueriesData` prefix match provided, carried over to the
    // surviving call.
    for (const pipelineId of pipelineIds) {
      qc.invalidateQueries({ queryKey: [...executionKeys.all, pipelineId] });
    }
    for (const runId of runIds) {
      qc.invalidateQueries({ queryKey: runKeys.detail(runId) });
    }
  }
}

export function WebSocketProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const auth = useAuth();

  // Keep a ref so the controller always reaches the latest QueryClient without
  // being re-created.
  const queryClientRef = useRef(queryClient);
  queryClientRef.current = queryClient;

  const [connected, setConnected] = useState(false);
  const [retryCount, setRetryCount] = useState(0);

  const controllerRef = useRef<WebSocketController>();
  if (!controllerRef.current) {
    controllerRef.current = new WebSocketController({
      getQueryClient: () => queryClientRef.current,
      onConnectedChange: setConnected,
      onRetryChange: setRetryCount,
    });
  }
  const controller = controllerRef.current;

  // Set the token during render so the first connect — triggered by a child's
  // subscribe effect, which runs before this component's effects — uses it.
  const idToken = auth.user?.id_token ?? null;
  controller.setToken(idToken);

  // Reconnect with the fresh token when it changes (token refresh).
  useEffect(() => {
    controller.syncToken();
  }, [idToken, controller]);

  // Attach global reconnect listeners; tear everything down on unmount.
  useEffect(() => {
    controller.mount();
    return () => controller.dispose();
  }, [controller]);

  const value = useMemo<WebSocketContextValue>(
    () => ({ connected, retryCount, subscribe: controller.subscribe }),
    [connected, retryCount, controller],
  );

  return (
    <WebSocketContext.Provider value={value}>
      {children}
    </WebSocketContext.Provider>
  );
}

/** Access the shared WebSocket context. Returns a disconnected no-op value when
 *  used outside a provider. */
export function useWebSocketContext(): WebSocketContextValue {
  return useContext(WebSocketContext);
}
