// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for useMappingRegenPolling — effect-scoped mapping-regen polling
 * (P29 R7).
 *
 * The critical guarantee (R7.1/R7.3) is that the polling interval is torn down
 * on unmount so navigating away never leaves an orphaned interval, and that
 * polling stops on a status change (mapping_id) rather than only via a blind
 * timeout, with a 60s cap as a safety net.
 *
 * Requirements: 7.1, 7.2, 7.3
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useMappingRegenPolling } from '../use-mapping-regen-polling';

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('useMappingRegenPolling', () => {
  it('polls at the configured interval while active', () => {
    const onPoll = vi.fn();
    const onStop = vi.fn();
    renderHook(() =>
      useMappingRegenPolling({
        active: true,
        completionSignal: 'm1',
        onPoll,
        onStop,
        intervalMs: 5_000,
        capMs: 60_000,
      }),
    );

    expect(onPoll).not.toHaveBeenCalled();
    vi.advanceTimersByTime(5_000);
    expect(onPoll).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(10_000);
    expect(onPoll).toHaveBeenCalledTimes(3);
    expect(onStop).not.toHaveBeenCalled();
  });

  it('does not poll when inactive', () => {
    const onPoll = vi.fn();
    renderHook(() =>
      useMappingRegenPolling({
        active: false,
        completionSignal: 'm1',
        onPoll,
        onStop: vi.fn(),
      }),
    );
    vi.advanceTimersByTime(120_000);
    expect(onPoll).not.toHaveBeenCalled();
  });

  it('clears the polling interval on unmount — no orphaned interval (R7.3)', () => {
    const onPoll = vi.fn();
    const onStop = vi.fn();
    const clearIntervalSpy = vi.spyOn(globalThis, 'clearInterval');

    const { unmount } = renderHook(() =>
      useMappingRegenPolling({
        active: true,
        completionSignal: 'm1',
        onPoll,
        onStop,
        intervalMs: 5_000,
        capMs: 60_000,
      }),
    );

    vi.advanceTimersByTime(5_000);
    expect(onPoll).toHaveBeenCalledTimes(1);

    unmount();
    expect(clearIntervalSpy).toHaveBeenCalled();

    // No further polling after unmount, even well past the cap window.
    vi.advanceTimersByTime(120_000);
    expect(onPoll).toHaveBeenCalledTimes(1);
    // The safety-net cap must not fire after unmount either.
    expect(onStop).not.toHaveBeenCalled();
  });

  it('stops polling when the completion signal changes (status-based stop)', () => {
    const onPoll = vi.fn();
    const onStop = vi.fn();

    const { rerender } = renderHook(
      ({ signal }: { signal: string }) =>
        useMappingRegenPolling({
          active: true,
          completionSignal: signal,
          onPoll,
          onStop,
          intervalMs: 5_000,
          capMs: 60_000,
        }),
      { initialProps: { signal: 'm1' } },
    );

    vi.advanceTimersByTime(5_000);
    expect(onPoll).toHaveBeenCalledTimes(1);
    expect(onStop).not.toHaveBeenCalled();

    // A new mapping lands: the completion signal changes → stop.
    rerender({ signal: 'm2' });
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('does not stop on the activation render when the signal is already set', () => {
    const onStop = vi.fn();
    renderHook(() =>
      useMappingRegenPolling({
        active: true,
        completionSignal: 'm1',
        onPoll: vi.fn(),
        onStop,
      }),
    );
    // The signal captured at activation equals the current signal, so no
    // spurious stop before any regeneration has completed.
    expect(onStop).not.toHaveBeenCalled();
  });

  it('tears down the interval when active flips false', () => {
    const onPoll = vi.fn();
    const { rerender } = renderHook(
      ({ active }: { active: boolean }) =>
        useMappingRegenPolling({
          active,
          completionSignal: 'm1',
          onPoll,
          onStop: vi.fn(),
          intervalMs: 5_000,
          capMs: 60_000,
        }),
      { initialProps: { active: true } },
    );

    vi.advanceTimersByTime(5_000);
    expect(onPoll).toHaveBeenCalledTimes(1);

    rerender({ active: false });
    vi.advanceTimersByTime(30_000);
    expect(onPoll).toHaveBeenCalledTimes(1);
  });

  it('stops via the 60s cap when the signal never changes (safety net)', () => {
    const onPoll = vi.fn();
    const onStop = vi.fn();
    renderHook(() =>
      useMappingRegenPolling({
        active: true,
        completionSignal: undefined,
        onPoll,
        onStop,
        intervalMs: 5_000,
        capMs: 60_000,
      }),
    );

    vi.advanceTimersByTime(60_000);
    expect(onStop).toHaveBeenCalledTimes(1);

    // After the cap fires the interval is cleared — no more polls.
    const pollsAtCap = onPoll.mock.calls.length;
    vi.advanceTimersByTime(60_000);
    expect(onPoll).toHaveBeenCalledTimes(pollsAtCap);
    expect(onStop).toHaveBeenCalledTimes(1);
  });
});
