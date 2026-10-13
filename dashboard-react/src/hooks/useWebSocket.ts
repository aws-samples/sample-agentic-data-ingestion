// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * useWebSocket — thin subscription hook over the WebSocketProvider (R9).
 *
 * Registers the given pipeline ids as topics on the app-wide Socket_Singleton
 * and reflects the shared connection state. All consumers share one socket;
 * the provider re-sends subscriptions on (re)open and when ids change, and
 * reconnects with bounded exponential backoff (plus online/visibility retries).
 *
 * While the shared socket is disconnected, `isConnected` is false and
 * `isFallbackPolling` is true so consumers keep polling — a *temporary*
 * fallback (the provider keeps reconnecting), not the old permanent
 * fall-to-polling after MAX_RETRIES.
 */
import { useEffect } from 'react';
import { useWebSocketContext } from '../providers/WebSocketProvider';

export interface UseWebSocketOptions {
  pipelineIds?: string[];
  enabled?: boolean;
}

export interface WebSocketState {
  isConnected: boolean;
  isFallbackPolling: boolean;
  retryCount: number;
}

export function useWebSocket({
  pipelineIds,
  enabled = true,
}: UseWebSocketOptions = {}): WebSocketState {
  const { connected, retryCount, subscribe } = useWebSocketContext();

  // Stable dependency string so the effect only re-registers when the set of
  // ids (order-independent) or `enabled` actually changes — not on every render
  // when the caller passes a fresh array literal.
  const idsKey =
    pipelineIds && pipelineIds.length
      ? JSON.stringify([...pipelineIds].sort())
      : '';

  useEffect(() => {
    if (!enabled || !idsKey) return;
    const ids = JSON.parse(idsKey) as string[];
    const unsubscribes = ids.map((id) => subscribe({ type: 'pipeline', id }));
    return () => unsubscribes.forEach((unsub) => unsub());
  }, [idsKey, enabled, subscribe]);

  return {
    isConnected: connected,
    isFallbackPolling: !connected,
    retryCount,
  };
}
