// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Polling cadence for the executions query — `computeExecutionsRefetchInterval`.
//
// P29 R11.3 established the shape: the executions hook must NOT keep
// per-instance interval state that fights the shared React Query cache, so the
// cadence is derived purely from the cached data and fed to React Query's
// native `refetchInterval`.
//
// p47 R14 adds the second input. Keying on work state alone breaks in both
// directions — connected + work in flight had poll and socket both fetching,
// and **disconnected + all terminal produced no updates at all**, because
// polling had stopped and the socket meant to replace it was down.
//
// DELIBERATE AMENDMENT of two P29 assertions (the `LGR-4c4b4c9-6` /
// `LGR-60c4985-6` convention: a hold encoded as an assertion is changed on
// purpose and stays legible as a reversal rather than reading as a regression):
//
//   1. "polls at the active cadence while an execution is running" and the
//      non-terminal-status table asserted EXECUTIONS_ACTIVE_POLL_MS with no
//      transport input. They now assert it for `connected: false` — where the
//      poll really is the only transport, which is what P29 meant — and assert
//      the backed-off cadence for `connected: true`. Both are kept below.
//   2. "returns false when the only execution is terminal" asserted `false`
//      unconditionally. `false` still holds while CONNECTED; while
//      disconnected it is exactly the defect R14.2 forbids, so the
//      disconnected arm now asserts a positive interval. Both are kept below.
//
// Expectations here come from R14.1/R14.2, not from current output — current
// output contains the defect.
import { describe, it, expect } from 'vitest';
import {
  computeExecutionsRefetchInterval,
  EXECUTIONS_ACTIVE_POLL_MS,
  EXECUTIONS_BACKGROUND_POLL_MS,
} from '../use-executions';

// Minimal shape helpers — the function only reads `executions[].status` and
// `queued_files.length`.
function paginated(
  executions: Array<{ status: string }>,
  queuedCount = 0,
) {
  return {
    executions: executions.map((e, i) => ({
      execution_id: `e${i}`,
      status: e.status,
      started_at: '2024-01-01T00:00:00Z',
    })),
    queued_files: Array.from({ length: queuedCount }, (_, i) => ({
      file_name: `f${i}.csv`,
      source_key: `k${i}`,
      queued_at: '2024-01-01T00:00:00Z',
      status: 'queued',
    })),
    total_count: executions.length,
    page: 1,
    page_size: 10,
    has_more: false,
    // Cast through unknown — this mirror carries exactly the fields the
    // function inspects.
  } as unknown as Parameters<typeof computeExecutionsRefetchInterval>[0];
}

// ---------------------------------------------------------------------------
// The truth table: (connected × active × terminal), all eight cells.
// ---------------------------------------------------------------------------

describe('computeExecutionsRefetchInterval — (connected × active × terminal) matrix (p47 R14)', () => {
  // `active` and `terminal` are not independent inputs: both are read off the
  // same status list via `isTerminalStatus`. A list can carry rows of each
  // kind, so all four (active, terminal) presence combinations are reachable.
  const lists: Record<string, Array<{ status: string }>> = {
    // active present, terminal present — a mixed page mid-run
    'active+terminal': [{ status: 'RUNNING' }, { status: 'succeeded' }],
    // active only
    'active-only': [{ status: 'RUNNING' }],
    // terminal only — everything settled
    'terminal-only': [{ status: 'succeeded' }],
    // neither — an empty page
    neither: [],
  };

  // Expectations derived from R14.1 (connected + work in flight backs off) and
  // R14.2 (disconnected never stops), NOT from current output.
  const cases: Array<{
    connected: boolean;
    list: keyof typeof lists;
    expected: number | false;
    why: string;
  }> = [
    // --- connected: the socket is primary, the poll is a safety net --------
    {
      connected: true,
      list: 'active+terminal',
      expected: EXECUTIONS_BACKGROUND_POLL_MS,
      why: 'R14.1 — work in flight, socket reporting: back off, do not stop',
    },
    {
      connected: true,
      list: 'active-only',
      expected: EXECUTIONS_BACKGROUND_POLL_MS,
      why: 'R14.1 — work in flight, socket reporting: back off, do not stop',
    },
    {
      connected: true,
      list: 'terminal-only',
      expected: false,
      why: 'nothing in flight and the socket is up to announce a new run: stop',
    },
    {
      connected: true,
      list: 'neither',
      expected: false,
      why: 'nothing in flight and the socket is up to announce a new run: stop',
    },
    // --- disconnected: polling is the ONLY transport ----------------------
    {
      connected: false,
      list: 'active+terminal',
      expected: EXECUTIONS_ACTIVE_POLL_MS,
      why: 'R14.2 — work is moving and the socket cannot report it',
    },
    {
      connected: false,
      list: 'active-only',
      expected: EXECUTIONS_ACTIVE_POLL_MS,
      why: 'R14.2 — work is moving and the socket cannot report it',
    },
    {
      connected: false,
      list: 'terminal-only',
      expected: EXECUTIONS_BACKGROUND_POLL_MS,
      why: 'R14.2 — THE DEFECT CELL: this used to return false, so polling had stopped and the socket meant to replace it was down',
    },
    {
      connected: false,
      list: 'neither',
      expected: EXECUTIONS_BACKGROUND_POLL_MS,
      why: 'R14.2 — a dropped socket degrades to polling, never to silence',
    },
  ];

  it.each(cases)(
    'connected=$connected, list=$list → $expected ($why)',
    ({ connected, list, expected }) => {
      expect(
        computeExecutionsRefetchInterval(paginated(lists[list]), connected),
      ).toBe(expected);
    },
  );

  it('covers all eight combinations of (connected × active × terminal)', () => {
    expect(cases).toHaveLength(8);
    expect(new Set(cases.map((c) => `${c.connected}:${c.list}`)).size).toBe(8);
  });

  it('never returns 0 or undefined — React Query reads both as "stop" (R14.2)', () => {
    for (const { connected, list } of cases) {
      const interval = computeExecutionsRefetchInterval(
        paginated(lists[list]),
        connected,
      );
      expect(interval === 0).toBe(false);
      expect(interval).not.toBeUndefined();
      // Every disconnected cell must be a positive number, never a stop value.
      if (!connected) {
        expect(typeof interval).toBe('number');
        expect(interval as number).toBeGreaterThan(0);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// Terminal-status detection stays delegated to `isTerminalStatus` (R14.3).
// ---------------------------------------------------------------------------

describe('computeExecutionsRefetchInterval — terminal detection (P29 R11.3, p47 R14.3)', () => {
  // AMENDED from P29: these asserted EXECUTIONS_ACTIVE_POLL_MS with no
  // transport input. The disconnected arm preserves that assertion verbatim.
  it.each(['IN_PROGRESS', 'WAITING_APPROVAL', 'PENDING', 'QUEUED', 'submitted'])(
    'treats non-terminal status %s as work in flight — active cadence while disconnected',
    (status) => {
      expect(computeExecutionsRefetchInterval(paginated([{ status }]), false)).toBe(
        EXECUTIONS_ACTIVE_POLL_MS,
      );
    },
  );

  it.each(['IN_PROGRESS', 'WAITING_APPROVAL', 'PENDING', 'QUEUED', 'submitted'])(
    'treats non-terminal status %s as work in flight — backed off while connected',
    (status) => {
      expect(computeExecutionsRefetchInterval(paginated([{ status }]), true)).toBe(
        EXECUTIONS_BACKGROUND_POLL_MS,
      );
    },
  );

  // AMENDED from P29: these asserted `false` with no transport input. The
  // connected arm preserves that assertion verbatim.
  it.each(['succeeded', 'completed', 'failed', 'error', 'stopped', 'timed_out', 'aborted'])(
    'stops while connected when the only execution is terminal (%s)',
    (status) => {
      expect(computeExecutionsRefetchInterval(paginated([{ status }]), true)).toBe(false);
    },
  );

  it.each(['succeeded', 'completed', 'failed', 'error', 'stopped', 'timed_out', 'aborted'])(
    'keeps polling while DISCONNECTED even though the only execution is terminal (%s)',
    (status) => {
      expect(computeExecutionsRefetchInterval(paginated([{ status }]), false)).toBe(
        EXECUTIONS_BACKGROUND_POLL_MS,
      );
    },
  );

  it('stops while connected when every execution is terminal (mixed terminal set)', () => {
    expect(
      computeExecutionsRefetchInterval(
        paginated([{ status: 'SUCCEEDED' }, { status: 'FAILED' }, { status: 'completed' }]),
        true,
      ),
    ).toBe(false);
  });

  it('keeps polling when at least one execution is still active among terminal ones', () => {
    expect(
      computeExecutionsRefetchInterval(
        paginated([{ status: 'succeeded' }, { status: 'RUNNING' }]),
        false,
      ),
    ).toBe(EXECUTIONS_ACTIVE_POLL_MS);
    expect(
      computeExecutionsRefetchInterval(
        paginated([{ status: 'succeeded' }, { status: 'RUNNING' }]),
        true,
      ),
    ).toBe(EXECUTIONS_BACKGROUND_POLL_MS);
  });

  it('counts queued files as work in flight even when all executions are terminal', () => {
    expect(
      computeExecutionsRefetchInterval(paginated([{ status: 'completed' }], 1), false),
    ).toBe(EXECUTIONS_ACTIVE_POLL_MS);
    expect(
      computeExecutionsRefetchInterval(paginated([{ status: 'completed' }], 1), true),
    ).toBe(EXECUTIONS_BACKGROUND_POLL_MS);
  });

  it('polls at the active cadence while data is still loading (undefined), either transport', () => {
    // Work state is unknown on the initial load, so neither R14.1 nor R14.2 has
    // a determined antecedent — get the first settled response promptly and let
    // it decide.
    expect(computeExecutionsRefetchInterval(undefined, true)).toBe(
      EXECUTIONS_ACTIVE_POLL_MS,
    );
    expect(computeExecutionsRefetchInterval(undefined, false)).toBe(
      EXECUTIONS_ACTIVE_POLL_MS,
    );
  });
});
