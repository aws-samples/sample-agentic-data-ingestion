// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p51 task 4.3 — every count on the pipeline overview card renders as exactly
 * one of three things, and each of the three is legible **as words**.
 *
 * WHAT WAS BROKEN
 * ---------------
 * Both counts were gated on `> 0` (`PipelineOverviewPage.tsx:99`, `:143` before
 * this change), so a failed read and a genuinely empty pipeline produced the same
 * output: nothing. An operator could not tell "no runs are waiting for you" from
 * "this card could not find out". `LGR-99050e6-2` (**template / ratified**)
 * settles the shape one surface over — *"an absent quarantine counter is reported
 * as unknown rather than as zero"* — and R1.4 / R2.4 apply it to these two
 * numbers.
 *
 * THE THREE RENDERED STATES
 * -------------------------
 * | state | approvals | running |
 * |---|---|---|
 * | a number | *"2 approvals pending"* | *"2 runs in progress"* |
 * | a floor | **unreachable** — see below | *"At least 40 runs in progress"* + scope clause |
 * | unavailable | *"Awaiting approval: unavailable"* | *"Runs in progress: unavailable"* |
 *
 * plus one silent case that is not a claim at all: a read that succeeded and
 * found none renders nothing, exactly as before this spec, and a read still in
 * flight renders nothing for a *different reason* — it has not answered yet.
 *
 * **The floor is unreachable for approvals by construction, not by omission.**
 * That read is a single keyed Query on `pipeline-status-index`, so the key
 * condition *is* the question: no walk, no bound, nothing to truncate. The
 * running count can only ever be bounded, because the executions table has no
 * `(pipeline_id, status)` index and is not getting one (`LGR-7d19eca-1`). The
 * asymmetry is asserted here rather than left as a comment.
 *
 * **P7 — zero and unavailable are different text.** For each count, a genuine `0`
 * and a failed read produce different rendered text. Asserted on `textContent`
 * only: no assertion in this file reads a class, a colour, or a
 * `StatusIndicator` type, because `LGR-99050e6-1` (**template / ratified**)
 * records that colour alone fails WCAG 1.4.1 — so a test that passed on the
 * indicator's type would pass on a version of this card that no screen reader
 * could use.
 * **Validates: Requirements 1.4, 2.4**
 *
 * **P8 — a floor renders as a floor.** `partial_counts` naming `total_count`
 * implies a floor qualifier in the text (`LGR-2bcdd0d-2`: `total_pages` "is a
 * floor when the count was bounded, so the UI states it as such rather than
 * rendering a floor as a total"). The generator deliberately also emits
 * `partial_counts: ['executions']`, which says the *page* was short and **not**
 * that the count is a floor (`pipelines_runs.py:878-880`) — task 4.2 recorded
 * that distinction and this is where it first becomes assertable.
 * **Validates: Requirements 2.2, 2.3**
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import * as fc from 'fast-check';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

type ExecRow = Record<string, unknown>;

/** One arm of a React Query result, as this card consumes it. */
type ReadResult<T> = { data: T | undefined; isError: boolean };

/** In flight: no data, no error. Renders nothing, and not because it found none. */
function inFlight<T>(): ReadResult<T> {
  return { data: undefined, isError: false };
}

/** Failed: the state that must never read as zero. */
function failed<T>(): ReadResult<T> {
  return { data: undefined, isError: true };
}

function answered<T>(data: T): ReadResult<T> {
  return { data, isError: false };
}

/** The producer's executions envelope (`pipelines_runs.py:1148-1176`). */
type Envelope = {
  executions: ExecRow[];
  queued_files: unknown[];
  pagination: {
    page: number;
    page_size: number;
    total_count: number;
    total_pages: number;
    has_next: boolean;
    has_prev: boolean;
    next_cursor: string | null;
    partial_counts: string[];
  };
};

function envelope(
  totalCount: number,
  partialCounts: string[] = [],
  rows = Math.min(totalCount, 10),
): Envelope {
  return {
    executions: Array.from({ length: Math.max(rows, 0) }, (_, i) => ({
      run_id: `run-2026-08-26-00-00-0${i % 10}-running${i}`,
      file_name: `running_${i}.csv`,
      started_at: `2026-08-26T00:00:0${i % 10}Z`,
      status: 'RUNNING',
    })),
    queued_files: [],
    pagination: {
      page: 1,
      page_size: 10,
      total_count: totalCount,
      total_pages: Math.max(Math.ceil(totalCount / 10), 0),
      has_next: partialCounts.length > 0,
      has_prev: false,
      next_cursor: partialCounts.length > 0 ? 'cursor' : null,
      partial_counts: partialCounts,
      ...{},
    },
  };
}

type ApprovalRow = {
  approval_id: string;
  pipeline_key: string;
  run_id?: string;
  approval_type: string;
  status: string;
  created_at: string;
};

function approvalRows(n: number): ApprovalRow[] {
  return Array.from({ length: n }, (_, i) => ({
    approval_id: `appr-${i}`,
    pipeline_key: 'helloworld-f9a76ce9',
    run_id: `run-2026-08-22-12-43-5${i % 10}-c3e56b8f`,
    approval_type: 'schema_mapping',
    status: 'pending',
    created_at: `2026-08-22T12:44:3${i % 10}.411868+00:00`,
  }));
}

const state = vi.hoisted(() => ({
  /** The unfiltered page-1 read — Recent Runs and `hasRuns`, never a count. */
  unfiltered: { data: undefined, isError: false } as ReadResult<unknown>,
  /** The `?status=RUNNING` read the running count is derived from. */
  running: { data: undefined, isError: false } as ReadResult<unknown>,
  /** The keyed approvals read. */
  approvals: { data: undefined, isError: false } as ReadResult<unknown>,
}));

vi.mock('../../hooks', () => ({
  usePipelineExecutions: (
    _key: unknown,
    _page: unknown,
    _cursor?: string | null,
    status?: string | null,
  ) => (status ? state.running : state.unfiltered),
  usePipelinePendingApprovals: () => state.approvals,
  useArchivePipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useDataPreview: () => ({ data: undefined, isLoading: false, isError: false, error: null }),
  usePipelineCosts: () => ({ data: undefined, isLoading: true, isError: false, refetch: vi.fn() }),
  useBedrockModels: () => ({ data: [] }),
  useCreatePipeline: () => ({ mutateAsync: vi.fn() }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useSchemas: () => ({ data: [], isLoading: false }),
  useGlueDatabases: () => ({ data: [], isLoading: false }),
  useTablesInDatabase: () => ({ data: [], isLoading: false }),
  useS3TablesBuckets: () => ({ data: [], isLoading: false }),
  useTablesInBucket: () => ({ data: [], isLoading: false }),
}));

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api-client')>();
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  };
});

import PipelineOverviewPage from '../PipelineOverviewPage';

const PIPELINE_KEY = 'helloworld-f9a76ce9';

const mockPipeline: Pipeline = {
  pipeline_key: PIPELINE_KEY,
  pipeline_name: 'helloworld',
  status: 'active',
  source_schema: 'customer',
  target_schema: 'customer_target',
  created_at: '2026-08-20T17:00:00Z',
  updated_at: '2026-08-26T12:44:37Z',
  ai_model: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
  schema_history: [],
  all_files: [],
};

function renderWith(over: {
  running?: ReadResult<unknown>;
  approvals?: ReadResult<unknown>;
  unfiltered?: ReadResult<unknown>;
}) {
  // The unfiltered read is never the source of a count, so it defaults to a
  // healthy answer throughout: nothing in this file may pass or fail because of
  // it. Recent Runs and `hasRuns` are task 4.2's subject and are pinned there.
  state.unfiltered = over.unfiltered ?? answered(envelope(3));
  state.running = over.running ?? inFlight();
  state.approvals = over.approvals ?? inFlight();
  return render(
    <MemoryRouter>
      <PipelineProvider
        value={{ pipeline: mockPipeline, isLoading: false, isError: false, refetch: vi.fn() }}
      >
        <PipelineOverviewPage />
      </PipelineProvider>
    </MemoryRouter>,
  );
}

/**
 * Everything the card says about one count, as text and nothing but text —
 * whichever of the three states produced it. `''` means the card said nothing.
 *
 * This is the only reader the assertions below use, which is what keeps them
 * honest per `LGR-99050e6-1`: there is no path from here to a class name, an
 * `aria-*` attribute or a `StatusIndicator` type, so a card that carried its
 * meaning in colour would fail these tests rather than pass them.
 */
function countText(which: 'approval' | 'running'): string {
  return [
    screen.queryByTestId(`${which}-count`),
    screen.queryByTestId(`${which}-count-unavailable`),
    screen.queryByTestId(`${which}-count-scope`),
  ]
    .map((el) => el?.textContent ?? '')
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
}

beforeEach(() => {
  cleanup();
});

/* ------------------------------------------------------------------ */
/* The three states, one at a time                                     */
/* ------------------------------------------------------------------ */

describe('the running count renders exactly one of three states', () => {
  it('a completed walk renders the number, with no scope clause', () => {
    renderWith({ running: answered(envelope(2)) });

    expect(countText('running')).toBe('2 runs in progress');
    // No clause, and that is the requirement rather than an omission: a
    // completed walk counted every match in the partition, so the number does
    // describe the whole history (R2.2).
    expect(screen.queryByTestId('running-count-scope')).toBeNull();
  });

  it('a truncated walk renders a floor, plus the clause naming what it means', () => {
    renderWith({ running: answered(envelope(40, ['total_count', 'total_pages'])) });

    expect(countText('running')).toMatch(/At least 40 runs in progress/);
    expect(screen.getByTestId('running-count-scope')).toHaveTextContent(
      /search stopped before the end of this pipeline's history/,
    );
  });

  it('a failed read says unavailable, in words', () => {
    renderWith({ running: failed() });

    const text = countText('running');
    expect(text).toMatch(/unavailable/i);
    // The words carry the meaning that matters — that this is not a zero.
    expect(text).toMatch(/not a count of none/i);
    expect(screen.queryByTestId('running-count')).toBeNull();
  });

  it('a genuine zero says nothing at all', () => {
    renderWith({ running: answered(envelope(0)) });

    expect(countText('running')).toBe('');
  });

  it('an in-flight read also says nothing — but for a different reason', () => {
    // Silent like the zero case, and deliberately not "unavailable": a routine
    // in-flight read is not a failure, and crying unavailable on every mount
    // would train the operator to ignore the word on the one render where it
    // means something.
    renderWith({ running: inFlight() });

    expect(countText('running')).toBe('');
  });
});

describe('the approvals count renders exactly one of three states', () => {
  it('an answered read renders the number, with no scope clause', () => {
    renderWith({ approvals: answered(approvalRows(2)) });

    expect(countText('approval')).toBe('2 approvals pending');
  });

  it('a failed read says unavailable, in words', () => {
    renderWith({ approvals: failed() });

    const text = countText('approval');
    expect(text).toMatch(/Awaiting approval: unavailable/);
    expect(text).toMatch(/not a count of none/i);
    expect(screen.queryByTestId('approval-count')).toBeNull();
    // And no controls, because there is no approval to act on — the count and
    // the controls still describe the same set (R1.2), which here is no set.
    expect(screen.queryAllByRole('button', { name: /^Approve$/ })).toHaveLength(0);
  });

  it('a genuine zero says nothing at all', () => {
    renderWith({ approvals: answered(approvalRows(0)) });

    expect(countText('approval')).toBe('');
  });

  it('an in-flight read says nothing', () => {
    renderWith({ approvals: inFlight() });

    expect(countText('approval')).toBe('');
  });

  /**
   * The keyed read is exact, so this count has no floor state to render. Pinned
   * so the asymmetry with the running count reads as a decision rather than an
   * oversight: `pipeline-status-index` answers the question with a key
   * condition, while the executions table has no `(pipeline_id, status)` index
   * and is not getting one (`LGR-7d19eca-1`).
   */
  it('never renders a floor, however many approvals come back', () => {
    for (const n of [1, 2, 11, 40]) {
      cleanup();
      renderWith({ approvals: answered(approvalRows(n)) });
      expect(countText('approval')).not.toMatch(/at least/i);
    }
  });
});

/* ------------------------------------------------------------------ */
/* The two counts are independent                                      */
/* ------------------------------------------------------------------ */

describe('one failed read does not silence the other count', () => {
  it('a failed approvals read leaves the running count intact', () => {
    renderWith({ approvals: failed(), running: answered(envelope(3)) });

    expect(countText('approval')).toMatch(/unavailable/i);
    expect(countText('running')).toBe('3 runs in progress');
  });

  it('a failed running read leaves the approvals count and its controls intact', () => {
    renderWith({ running: failed(), approvals: answered(approvalRows(1)) });

    expect(countText('running')).toMatch(/unavailable/i);
    expect(countText('approval')).toBe('1 approval pending');
    expect(screen.getAllByRole('button', { name: /^Approve$/ })).toHaveLength(1);
  });

  it('both failing degrades two lines, not the card', () => {
    renderWith({ running: failed(), approvals: failed() });

    expect(countText('running')).toMatch(/unavailable/i);
    expect(countText('approval')).toMatch(/unavailable/i);
    // The pipeline's own status still renders, and so does Recent Runs.
    expect(screen.getByTestId('pipeline-status')).toHaveTextContent('active');
    expect(screen.getAllByRole('link', { name: 'View Run' }).length).toBeGreaterThan(0);
  });
});

/* ------------------------------------------------------------------ */
/* P7 — zero and unavailable are different text                        */
/* ------------------------------------------------------------------ */

describe('P7 — a genuine zero and a failed read produce different text', () => {
  /**
   * **Validates: Requirements 1.4, 2.4**
   *
   * Stated as the difference rather than as two fixed strings, so it survives a
   * rewording of either state. What it forbids is the two states converging —
   * which is what the `> 0` gate did, by rendering nothing for both.
   */
  it('holds for the running count', () => {
    renderWith({ running: answered(envelope(0)) });
    const zero = countText('running');

    cleanup();
    renderWith({ running: failed() });
    const unavailable = countText('running');

    expect(zero).not.toBe(unavailable);
    // Non-trivially different: the failure arm actually says something.
    expect(unavailable.length).toBeGreaterThan(0);
  });

  it('holds for the approvals count', () => {
    renderWith({ approvals: answered(approvalRows(0)) });
    const zero = countText('approval');

    cleanup();
    renderWith({ approvals: failed() });
    const unavailable = countText('approval');

    expect(zero).not.toBe(unavailable);
    expect(unavailable.length).toBeGreaterThan(0);
  });

  /**
   * The narrow prohibition, separate from the difference above: `0` is the one
   * numeral neither line may ever print. A card that rendered *"0 runs in
   * progress"* on a failed read would satisfy "different text" against a silent
   * zero and still be the exact defect `LGR-99050e6-2` forbids.
   *
   * **Validates: Requirements 1.4, 2.4**
   */
  it('never prints a zero count, in any state of either read', () => {
    const arms: Array<ReadResult<unknown>> = [
      inFlight(),
      failed(),
      answered(envelope(0)),
      answered(envelope(0, ['total_count'])),
    ];
    const approvalArms: Array<ReadResult<unknown>> = [
      inFlight(),
      failed(),
      answered(approvalRows(0)),
    ];

    for (const running of arms) {
      for (const approvals of approvalArms) {
        cleanup();
        renderWith({ running, approvals });
        expect(countText('running')).not.toMatch(/\b0\b/);
        expect(countText('approval')).not.toMatch(/\b0\b/);
        expect(screen.queryByText(/0 runs? in progress/i)).not.toBeInTheDocument();
        expect(screen.queryByText(/0 approvals? pending/i)).not.toBeInTheDocument();
      }
    }
  });
});

/* ------------------------------------------------------------------ */
/* P8 — a floor renders as a floor                                     */
/* ------------------------------------------------------------------ */

describe('P8 — partial_counts naming total_count renders a floor qualifier', () => {
  /**
   * `total_count` is generated independently of the truncation marker, and the
   * marker set includes the two members that mean different things:
   *
   * - `'total_count'` — the **count** is a floor. The only member that may
   *   produce a floor qualifier.
   * - `'executions'` — the **page** came back short with `has_next: true`
   *   (`pipelines_runs.py:878-880`). Says nothing about the count, so a card
   *   keying on `partial_counts.length > 0` would render an exact number as a
   *   floor here. Task 4.2 recorded that trap; this generator walks into it on
   *   purpose.
   *
   * **Validates: Requirements 2.2, 2.3**
   */
  const arb = fc.tuple(
    fc.integer({ min: 1, max: 400 }),
    fc.subarray(['total_count', 'total_pages', 'executions'], { minLength: 0, maxLength: 3 }),
  );

  it('a floor qualifier appears exactly when total_count is named', () => {
    // Both branches must actually be exercised, or the property is vacuously
    // green: a generator that happened never to emit `total_count` would assert
    // only the negative arm and report a pass for the floor it never rendered.
    const seen = { floor: 0, exact: 0 };

    fc.assert(
      fc.property(arb, ([total, partialCounts]) => {
        cleanup();
        renderWith({ running: answered(envelope(total, partialCounts)) });

        const text = countText('running');
        const isFloor = partialCounts.includes('total_count');
        if (isFloor) seen.floor += 1;
        else seen.exact += 1;

        // The number itself is unaffected by truncation — a floor is still the
        // count of matches actually found (`pipelines_runs.py:872`).
        expect(text).toMatch(new RegExp(`\\b${total}\\b`));

        if (isFloor) {
          expect(text).toMatch(/at least/i);
          expect(screen.getByTestId('running-count-scope')).toBeInTheDocument();
        } else {
          expect(text).not.toMatch(/at least/i);
          expect(screen.queryByTestId('running-count-scope')).toBeNull();
        }
      }),
      { numRuns: 40 },
    );

    expect(seen.floor).toBeGreaterThan(0);
    expect(seen.exact).toBeGreaterThan(0);
  });

  it('a short page alone is not a floor', () => {
    // The single case the property above could reach only by chance, spelled
    // out: `['executions']` with an exact count.
    renderWith({ running: answered(envelope(7, ['executions'], 4)) });

    expect(countText('running')).toBe('7 runs in progress');
    expect(screen.queryByTestId('running-count-scope')).toBeNull();
  });

  it('an empty partial_counts is a complete answer', () => {
    // `partial_counts` is always present and `[]` when nothing was short
    // (`LGR-9a35b2d-3`, `LGR-b2f79ef-14`), so `[]` must not read as truncated.
    renderWith({ running: answered(envelope(5, [])) });

    expect(countText('running')).toBe('5 runs in progress');
  });

  it('pluralises the floor on the numeral it printed', () => {
    renderWith({ running: answered(envelope(1, ['total_count'])) });
    // "At least 1 run", not "At least 1 runs": the agreement is with the number
    // rendered, not with the unknown true total the floor bounds.
    expect(countText('running')).toMatch(/At least 1 run in progress/);
  });
});
