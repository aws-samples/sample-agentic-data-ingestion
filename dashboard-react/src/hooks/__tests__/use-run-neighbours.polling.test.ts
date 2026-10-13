// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `useRunNeighbours` keeps a bounded backstop poll (p48 task 5.2, `LGR-e800696-1`).
 *
 * WHY THIS EXISTS AS A TEST RATHER THAN A COMMENT
 * -----------------------------------------------
 * The tempting simplification is "the WebSocket invalidates this key, so drop the
 * poll". `LGR-e800696-1` (**template / ratified**) rejects exactly that reasoning for
 * the run-history list, on grounds that transfer verbatim: this socket has no ack, no
 * sequence number and no replay, and `connected` flips false only on `onclose`, so a
 * half-open socket reads as healthy while delivering nothing. A neighbour
 * relationship changes when a NEW RUN APPEARS — which is a thing that happens while
 * work is in flight — so socket-only refresh would leave an operator on the newest
 * run staring at a disabled "Next Run".
 *
 * WHAT THE CADENCE ASSERTION CAN AND CANNOT CATCH — stated because it would
 * otherwise overclaim. It compares the interval against
 * `EXECUTIONS_BACKGROUND_POLL_MS`, so it catches a **divergent** cadence (someone
 * writing `10_000` and creating a second, disagreeing authority). It does **not**
 * catch a **duplicated** one: a local literal `30_000` is numerically equal to the
 * imported constant, so the assertion passes. Confirmed by mutation at task 5.2 —
 * replacing the import with `30_000` left all six tests green.
 *
 * That gap is accepted rather than closed. Catching it needs an AST or source-text
 * check on this one line, which is disproportionate to the risk: the harm from a
 * duplicated literal is that the two copies later drift, and the moment they drift
 * they diverge, which this assertion *does* catch. The single-authority intent is
 * carried by the comment on `refetchInterval` in `use-run-neighbours.ts`.
 *
 * `0` and `undefined` are checked explicitly because React Query treats BOTH as
 * "stop": `queryObserver.cjs:430` coalesces `undefined` to `false` and `:435` returns
 * before `setInterval` when the interval is `0`. `LGR-e800696-1` records that a cell
 * computing to `0` would silently mean "stop" — so a positive-number assertion is the
 * guard, not a `!== false` one.
 *
 * Requirements: p48 R10.2
 */
import { describe, it, expect, vi } from 'vitest';
import { EXECUTIONS_BACKGROUND_POLL_MS } from '../use-executions';

const mockUseQuery = vi.fn(() => ({ data: undefined }));
vi.mock('@tanstack/react-query', () => ({ useQuery: (options: unknown) => mockUseQuery(options as never) }));
vi.mock('../../services/api-client', () => ({ apiClient: { get: vi.fn() } }));

const { useRunNeighbours } = await import('../use-run-neighbours');

interface CapturedOptions {
  queryKey: readonly unknown[];
  enabled: boolean;
  refetchInterval: unknown;
  refetchIntervalInBackground: boolean;
}

function optionsFor(pipelineKey: string | undefined, runId: string | undefined): CapturedOptions {
  mockUseQuery.mockClear();
  useRunNeighbours(pipelineKey, runId);
  return mockUseQuery.mock.calls[0][0] as unknown as CapturedOptions;
}

describe('useRunNeighbours — the poll is the always-on transport (LGR-e800696-1)', () => {
  it('polls on a positive interval rather than relying on the socket alone', () => {
    const options = optionsFor('pipe-1', 'run-1');
    expect(typeof options.refetchInterval).toBe('number');
    expect(options.refetchInterval).toBeGreaterThan(0);
  });

  it('does not diverge from the run-history background cadence', () => {
    // Deliberately NOT titled "uses the shared constant" — see the header: this
    // cannot distinguish the import from an equal literal, only from a different
    // number.
    expect(optionsFor('pipe-1', 'run-1').refetchInterval).toBe(EXECUTIONS_BACKGROUND_POLL_MS);
  });

  it('is neither 0 nor undefined, both of which React Query reads as "stop"', () => {
    const options = optionsFor('pipe-1', 'run-1');
    expect(options.refetchInterval).not.toBe(0);
    expect(options.refetchInterval).not.toBeUndefined();
    expect(options.refetchInterval).not.toBe(false);
  });

  it('does not poll a backgrounded tab', () => {
    expect(optionsFor('pipe-1', 'run-1').refetchIntervalInBackground).toBe(false);
  });
});

describe('useRunNeighbours — key placement and gating', () => {
  it('keys under the executions prefix so the WebSocket flush reaches it (LGR-8b6e6a1-1)', () => {
    // The flush invalidates the prefix `['executions', id]`, so the pipeline key must
    // sit second and every later member after it.
    expect(optionsFor('pipe-1', 'run-1').queryKey).toEqual([
      'executions',
      'pipe-1',
      'neighbours',
      'run-1',
    ]);
  });

  it('is disabled until both the pipeline key and the run id are known', () => {
    expect(optionsFor(undefined, 'run-1').enabled).toBe(false);
    expect(optionsFor('pipe-1', undefined).enabled).toBe(false);
    expect(optionsFor('pipe-1', 'run-1').enabled).toBe(true);
  });
});
