// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useRef } from 'react';

/**
 * useMappingRegenPolling — effect-scoped polling for mapping regeneration (P29 R7).
 *
 * The mapping-regeneration endpoint is fired asynchronously (InvocationType:
 * Event) so the dashboard has to poll until the new mapping lands. The previous
 * implementation lived inline in a mutation `onSuccess` callback: it called
 * `setInterval(..., 5000)` and relied on a blind `setTimeout(clearInterval,
 * 60000)` to stop. That interval was **never cleaned up on component unmount**
 * (orphaned interval) and only ever stopped by the blind timeout — never when
 * the mapping actually finished.
 *
 * This hook fixes both problems (R7.1):
 *  - **Unmount cleanup**: the interval and the safety-net timeout are created
 *    inside a `useEffect` and cleared in its cleanup, so navigating away stops
 *    the polling immediately — no orphaned timers.
 *  - **Status-based stop**: when `completionSignal` changes from the value
 *    captured at activation (e.g. the pipeline's `mapping_id` after a new
 *    mapping is generated), polling stops via `onStop`. The 60s cap is only a
 *    safety net, not the primary stop condition.
 *
 * Contract:
 *  - `active` toggling `true` starts polling; toggling `false` (typically from
 *    the caller's `onStop` handler) tears the timers down via effect cleanup.
 *  - `onPoll` runs on every tick (usually `refetch()` of the relevant queries).
 *  - `onStop` runs exactly once when polling ends — either because the
 *    `completionSignal` changed (done) or the `capMs` safety net elapsed.
 *  - `onPoll`/`onStop` are held in refs so passing fresh closures each render
 *    does not restart the interval.
 */

export const DEFAULT_POLL_INTERVAL_MS = 5_000;
export const DEFAULT_POLL_CAP_MS = 60_000;

export interface UseMappingRegenPollingOptions {
  /** Whether regeneration is in progress and polling should run. */
  active: boolean;
  /**
   * A value that changes when regeneration completes (e.g. the pipeline's
   * `mapping_id`). When it differs from the value captured at activation,
   * polling stops. May be `undefined`/`null` before a mapping exists — in that
   * case the 60s cap is the only stop condition (safety net).
   */
  completionSignal: string | number | null | undefined;
  /** Called on each poll tick — typically `refetch()` of the relevant queries. */
  onPoll: () => void;
  /** Called once when polling stops (completion detected OR cap reached). */
  onStop: () => void;
  /** Poll cadence in ms. Defaults to 5000. */
  intervalMs?: number;
  /** Safety-net cap in ms. Defaults to 60000. */
  capMs?: number;
}

export function useMappingRegenPolling({
  active,
  completionSignal,
  onPoll,
  onStop,
  intervalMs = DEFAULT_POLL_INTERVAL_MS,
  capMs = DEFAULT_POLL_CAP_MS,
}: UseMappingRegenPollingOptions): void {
  // Keep the latest callbacks in refs so a caller passing new closures each
  // render does not tear down and restart the interval.
  const onPollRef = useRef(onPoll);
  const onStopRef = useRef(onStop);
  onPollRef.current = onPoll;
  onStopRef.current = onStop;

  // The completion-signal value captured when polling last became active. A
  // later change to `completionSignal` means regeneration finished.
  const baselineRef = useRef<string | number | null | undefined>(completionSignal);

  // Interval + safety-net cap. Started when `active` becomes true; cleared on
  // unmount AND when `active` flips false (status-based stop below). The
  // `completionSignal` is intentionally NOT a dependency here so a signal
  // change does not restart the interval — completion is handled separately.
  useEffect(() => {
    if (!active) return undefined;

    // Capture the baseline at activation so the completion check below compares
    // against the value present when polling started.
    baselineRef.current = completionSignal;

    const interval = setInterval(() => {
      onPollRef.current();
    }, intervalMs);

    const cap = setTimeout(() => {
      clearInterval(interval);
      onStopRef.current();
    }, capMs);

    return () => {
      clearInterval(interval);
      clearTimeout(cap);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, intervalMs, capMs]);

  // Status-based stop: when the completion signal changes from the value
  // captured at activation, regeneration finished. Calling `onStop` flips the
  // caller's `active` state false, which triggers the effect above's cleanup
  // (clearing both the interval and the cap).
  useEffect(() => {
    if (!active) return;
    if (completionSignal !== baselineRef.current) {
      onStopRef.current();
    }
  }, [active, completionSignal]);
}
