// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p51 task 4.2 — *"N runs in progress"* comes from a server-filtered read of the
 * pipeline's partition, not from the ten rows page 1 happened to hold.
 *
 * WHAT WAS BROKEN
 * ---------------
 * `runningCount` was `latestExecs.filter(e => e.status === 'RUNNING').length`
 * over `usePipelineExecutions(key, 1)` — page 1 at `EXECUTIONS_PAGE_SIZE` = 10 —
 * and was rendered under the **Status** key label as a statement about the
 * pipeline. `LGR-8a4fefa-2` (**template / ratified**) recorded exactly that and
 * named this edit as its `revisit_when`: *"`runningCount` when the overview card
 * is next edited."*
 *
 * WHAT A TEST HERE CAN AND CANNOT PROVE
 * -------------------------------------
 * Every page test mocks `../../hooks` wholesale and there is no way to render
 * this page without doing so, so nothing here exercises the server's walk. What
 * it *can* prove — and what R2.1 actually asks for — is that the component reads
 * the **filtered response's `pagination.total_count`** and adds no second
 * counting authority of its own. The fixtures are built so a page-derived count
 * cannot satisfy them:
 *
 * - the filtered response's `executions` array is deliberately **shorter** than
 *   its own `total_count`, which is the real shape (one page of ten rows out of
 *   a walk that found more), so counting rows gives a different answer to
 *   reading the number;
 * - the **unfiltered** page carries `RUNNING` rows in a quantity that matches
 *   neither, so the old source is a distinguishable wrong answer rather than a
 *   coincidental right one.
 *
 * WHERE THE COPY IS ASSERTED
 * --------------------------
 * **Amended by task 4.3.** This block used to read "the floor and unavailable
 * wordings belong to task 4.3", which was true when 4.2 landed and is not any
 * more: 4.3 shipped the three rendered states, and they are asserted in
 * `PipelineOverviewPage.count-states.test.tsx` (P7, P8). What stays here is the
 * narrower property 4.2 owns — **where the number comes from** — because that is
 * the assertion the fixtures in this file are built to make: the filtered page is
 * shorter than its own `total_count`, and the unfiltered page carries a
 * distinguishably wrong number of `RUNNING` rows.
 *
 * The `> 0` gate this file pinned as characterization is **gone**, replaced by
 * `classifyCount`'s four-member union. The two "renders nothing" tests below
 * survive that change and are now amended to say which of the union's silent
 * members each one exercises — they were written when `undefined` meant both
 * "in flight" and "failed", and it no longer does.
 *
 * **The `partial_counts` distinction 4.2 recorded for 4.3, now discharged.**
 * `partial_counts` carries `"total_count"` when the *count* is a floor, and
 * separately carries `"executions"` when the *page* came back short — "a filtered
 * page shorter than `page_size` with `has_next: true`" (`pipelines_runs.py:878-880`).
 * They are different conditions. 4.2 keys `isFloor` on the `"total_count"` member
 * for that reason; a `partial_counts.length > 0` test would render an exact count
 * as a floor every time the filtered page was short. P8 now asserts exactly that,
 * with a generator that emits `['executions']` on purpose.
 *
 * Copy is asserted on **text**, per `LGR-99050e6-1` (colour alone fails WCAG
 * 1.4.1).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import * as fc from 'fast-check';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

type ExecRow = Record<string, unknown>;

/** The producer's envelope (`pipelines_runs.py:1148-1176`), nested per `LGR-2bcdd0d-1`. */
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
  executions: ExecRow[],
  over: Partial<Envelope['pagination']> = {},
): Envelope {
  return {
    executions,
    queued_files: [],
    pagination: {
      page: 1,
      page_size: 10,
      total_count: executions.length,
      total_pages: 1,
      has_next: false,
      has_prev: false,
      next_cursor: null,
      partial_counts: [],
      ...over,
    },
  };
}

const state = vi.hoisted(() => ({
  /** Answer to the unfiltered page-1 read (Recent Runs, `hasRuns`). */
  unfiltered: undefined as unknown,
  /** Answer to the `?status=RUNNING` read. `undefined` = in flight or failed. */
  running: undefined as unknown,
}));

/** Every `(key, page, cursor, status)` the card asked the executions hook for. */
const hookCalls = vi.hoisted(
  () =>
    [] as Array<{
      key: unknown;
      page: unknown;
      cursor: unknown;
      status: string | null | undefined;
    }>,
);

vi.mock('../../hooks', () => ({
  usePipelineExecutions: (
    key: unknown,
    page: unknown,
    cursor?: string | null,
    status?: string | null,
  ) => {
    hookCalls.push({ key, page, cursor, status });
    return { data: status ? state.running : state.unfiltered };
  },
  useArchivePipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  // Empty throughout: this file is about the running half of the Status cell.
  usePipelinePendingApprovals: () => ({ data: [] }),
  useDataPreview: () => ({ data: undefined, isLoading: false, isError: false, error: null }),
  // `run_count: 0` puts `PipelineCostPanel` on its empty branch, which is the
  // only branch whose copy varies with `hasRuns` — so `hasRuns`' source is
  // observable from the rendered text.
  usePipelineCosts: () => ({
    data: { run_count: 0 },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
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

const PIPELINE_KEY = 'latesttesting-19ca826a';

const mockPipeline: Pipeline = {
  pipeline_key: PIPELINE_KEY,
  pipeline_name: 'latesttesting',
  status: 'active',
  source_schema: 'customer',
  target_schema: 'customer_target',
  created_at: '2026-08-20T17:00:00Z',
  updated_at: '2026-08-24T13:48:26Z',
  ai_model: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
  schema_history: [],
  all_files: [],
};

/**
 * Ten unfiltered rows, **four of them `RUNNING`**. Four is the old derivation's
 * answer, and it matches neither number any filtered fixture below reports — so
 * every assertion here fails loudly if the source reverts to `latestExecs`.
 */
function unfilteredPage(): ExecRow[] {
  return Array.from({ length: 10 }, (_, i) => ({
    run_id: `run-2026-08-2${i % 10}-00-00-00-page${i}`,
    file_name: `customer_data_${i}.csv`,
    started_at: `2026-08-2${i % 10}T00:00:00Z`,
    status: i < 4 ? 'RUNNING' : 'SUCCEEDED',
  }));
}

/** `n` rows that would be on the filtered page — capped at a page, like the wire. */
function runningRows(n: number): ExecRow[] {
  return Array.from({ length: Math.min(n, 10) }, (_, i) => ({
    run_id: `run-2026-08-25-00-00-0${i}-running${i}`,
    file_name: `running_${i}.csv`,
    started_at: `2026-08-25T00:00:0${i}Z`,
    status: 'RUNNING',
  }));
}

function renderWith(unfiltered: unknown, running: unknown) {
  state.unfiltered = unfiltered;
  state.running = running;
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

/** The number the card rendered, read off the text. `null` when absent. */
function renderedRunningCount(): number | null {
  const el = screen.queryByTestId('running-count');
  if (!el) return null;
  const match = (el.textContent ?? '').match(/(\d+)/);
  return match ? Number(match[1]) : null;
}

beforeEach(() => {
  cleanup();
  hookCalls.length = 0;
  state.unfiltered = undefined;
  state.running = undefined;
});

/* ------------------------------------------------------------------ */
/* The reads the card issues                                          */
/* ------------------------------------------------------------------ */

describe('PipelineOverviewPage — the two executions reads (p51 task 4.2)', () => {
  it('asks for the running count with the status filter the server accepts', () => {
    renderWith(envelope(unfilteredPage()), envelope(runningRows(2), { total_count: 2 }));

    const filtered = hookCalls.filter((c) => c.status);
    expect(filtered).toHaveLength(1);
    // `'RUNNING'`, upper case: execution statuses are upper and approvals
    // statuses are lower, and an unrecognised value is a 400 naming
    // `pipelines_runs._ACCEPTED_STATUS_FILTERS`. A copy-pasted `'running'`
    // would fail there rather than here, so it is pinned here.
    expect(filtered[0].status).toBe('RUNNING');
    expect(filtered[0]).toMatchObject({ key: PIPELINE_KEY, page: 1 });
    // Explicit `null`, not omitted: `executionKeys.list` spells the cursor
    // `cursor ?? null` for React Query's key hashing, and the filtered read has
    // no cursor to carry — a filter walk must never resume from a position the
    // filter never walked to.
    expect(filtered[0].cursor).toBeNull();
  });

  it('leaves the unfiltered page-1 read exactly as it was (R3.4)', () => {
    renderWith(envelope(unfilteredPage()), envelope(runningRows(2), { total_count: 2 }));

    const unfiltered = hookCalls.filter((c) => !c.status);
    expect(unfiltered).toHaveLength(1);
    expect(unfiltered[0]).toMatchObject({ key: PIPELINE_KEY, page: 1 });
    expect(unfiltered[0].cursor).toBeUndefined();
    // Two reads, not three: the count is an addition beside the preview, not a
    // replacement of it and not a second copy of it.
    expect(hookCalls).toHaveLength(2);
  });
});

/* ------------------------------------------------------------------ */
/* The number describes the pipeline, not the page                     */
/* ------------------------------------------------------------------ */

describe('the count is pagination.total_count, not a row count', () => {
  it('reports the walk total even when the filtered page holds fewer rows', () => {
    // The real shape: the walk found 23 matches and returned the first page of
    // ten. Counting rows gives 10; counting the unfiltered page's RUNNING rows
    // gives 4; the answer is 23.
    renderWith(
      envelope(unfilteredPage()),
      envelope(runningRows(10), { total_count: 23, total_pages: 3, has_next: true }),
    );

    expect(renderedRunningCount()).toBe(23);
    expect(screen.getByTestId('running-count')).toHaveTextContent('23 runs in progress');
  });

  it('ignores the unfiltered page entirely — the old source is dead', () => {
    // Four RUNNING rows on the unfiltered page, one match in the pipeline.
    // The old derivation answered 4.
    renderWith(envelope(unfilteredPage()), envelope(runningRows(1), { total_count: 1 }));

    expect(renderedRunningCount()).toBe(1);
    expect(screen.getByTestId('running-count')).toHaveTextContent('1 run in progress');
  });

  it('counts matches the unfiltered page cannot see at all', () => {
    // The running run is *off* page 1 — the mechanism §Introduction describes,
    // where recent skips sort above an older-but-still-running run. The
    // unfiltered page carries no RUNNING row, so the old derivation answered 0
    // and rendered nothing.
    const skipsOnly: ExecRow[] = Array.from({ length: 10 }, (_, i) => ({
      run_id: `skipped-${i}${'a'.repeat(31)}`,
      file_name: `dup_${i}.csv`,
      started_at: `2026-08-26T0${i % 10}:00:00Z`,
      status: 'SKIPPED',
    }));
    expect(skipsOnly.some((r) => r.status === 'RUNNING')).toBe(false);

    renderWith(envelope(skipsOnly), envelope(runningRows(1), { total_count: 1 }));

    expect(renderedRunningCount()).toBe(1);
  });

  it('pluralises on the number it reports, not on the rows it received', () => {
    renderWith(envelope(unfilteredPage()), envelope(runningRows(1), { total_count: 1 }));
    expect(screen.getByTestId('running-count')).toHaveTextContent('1 run in progress');

    cleanup();
    renderWith(envelope(unfilteredPage()), envelope(runningRows(10), { total_count: 12 }));
    expect(screen.getByTestId('running-count')).toHaveTextContent('12 runs in progress');
  });
});

/* ------------------------------------------------------------------ */
/* Zero, unknown, and truncated                                        */
/* ------------------------------------------------------------------ */

describe('a count that is not a positive total', () => {
  it('renders nothing when the walk found no match', () => {
    renderWith(envelope(unfilteredPage()), envelope([], { total_count: 0, total_pages: 0 }));

    expect(renderedRunningCount()).toBeNull();
    // **Amended by 4.3, which resolved the open question this comment carried.**
    // It used to read "task 4.3 owns whether this stays silent or gains words".
    // It stays silent: `design.md`'s Error Handling table gives "genuinely none →
    // the section does not render, as today", and the `none` member of
    // `classifyCount`'s union is what makes that explicit rather than incidental.
    // Words are reserved for the arm where they change a decision — a *failed*
    // read, asserted in `count-states.test.tsx`.
    //
    // Anchored on the digit because `PipelineCostPanel`'s empty copy also
    // contains "in progress".
    expect(screen.queryByText(/\d+ runs? in progress/i)).not.toBeInTheDocument();
  });

  it('renders nothing when the read is still in flight — and specifically not a zero', () => {
    // **Amended by 4.3.** Both the name and this comment used to say "when the
    // read has no answer", covering "still in flight" and "failed" together,
    // because `data: undefined` was the only signal the card had. It no longer
    // is: `classifyCount` reads `isError` first, so those two are now `unknown`
    // and `unavailable` and render differently. This mock returns no `isError`,
    // so this test exercises **in flight only**; the failed arm renders words and
    // is asserted in `count-states.test.tsx` (P7). That split is the whole point
    // of 4.3 — before it, a failed read and an empty pipeline were the same
    // output.
    //
    // What survives the change unaltered: neither state is a count of zero
    // (`LGR-99050e6-2`) — an absent counter is unknown, not none.
    renderWith(envelope(unfilteredPage()), undefined);

    expect(renderedRunningCount()).toBeNull();
    expect(screen.queryByText(/0 runs? in progress/i)).not.toBeInTheDocument();
    // The rest of the Status cell is unaffected — the pipeline's own status
    // still renders, so an unknown count degrades one line and not the card.
    expect(screen.getByTestId('pipeline-status')).toHaveTextContent('active');
  });

  it('renders a truncated walk as a floor', () => {
    // **Amended by 4.3**, from "still renders the number when a bound truncated
    // the walk". 4.2 could only assert that the truncation neither suppressed
    // the number nor was mistaken for an error, because the floor had no
    // rendering yet; it now has one, so this fixture asserts it rather than
    // leaving its own expectation weaker than the behaviour. The property form
    // lives in `count-states.test.tsx` (P8).
    renderWith(
      envelope(unfilteredPage()),
      envelope(runningRows(10), {
        total_count: 40,
        total_pages: 4,
        partial_counts: ['total_count', 'total_pages'],
      }),
    );

    expect(renderedRunningCount()).toBe(40);
    expect(screen.getByTestId('running-count')).toHaveTextContent('At least 40 runs in progress');
  });

  it('treats an empty partial_counts as a complete answer', () => {
    // `partial_counts` is always present and `[]` when nothing was short
    // (`LGR-9a35b2d-3`, `LGR-b2f79ef-14`), so `[]` must not read as truncated.
    renderWith(
      envelope(unfilteredPage()),
      envelope(runningRows(3), { total_count: 3, partial_counts: [] }),
    );

    expect(renderedRunningCount()).toBe(3);
  });
});

/* ------------------------------------------------------------------ */
/* R3.4 — the unfiltered read's two consumers are untouched            */
/* ------------------------------------------------------------------ */

describe('R3.4 — Recent Runs and hasRuns still read the unfiltered page', () => {
  /** Run ids the card rendered, off the per-row "View Run" link. */
  function renderedRunIds(): string[] {
    return screen
      .getAllByRole('link', { name: 'View Run' })
      .map((a) => a.getAttribute('href') ?? '')
      .map((href) => href.replace(`/pipelines/${PIPELINE_KEY}/runs/`, ''));
  }

  it('shows the five newest unfiltered rows, not the filtered ones', () => {
    const page = unfilteredPage();
    renderWith(envelope(page), envelope(runningRows(10), { total_count: 40 }));

    const ids = renderedRunIds();
    expect(ids).toHaveLength(5);
    expect(ids).toEqual(page.slice(0, 5).map((r) => r.run_id as string));
    // None of the filtered read's rows leaked into the table.
    for (const id of ids) expect(id).not.toMatch(/running\d/);
    // Still a preview, still no pager.
    expect(screen.queryByRole('button', { name: /Next page/i })).not.toBeInTheDocument();
  });

  it('tells PipelineCostPanel the pipeline HAS run even when nothing is running', () => {
    // Unfiltered page has rows; the filtered read is empty. `hasRuns` must come
    // from the former — reading the latter would claim "this pipeline has not
    // run" for a pipeline with ten runs.
    renderWith(envelope(unfilteredPage()), envelope([], { total_count: 0 }));

    const empty = screen.getByTestId('cost-empty');
    expect(empty.textContent ?? '').not.toMatch(/has not run/i);
    expect(empty).toHaveTextContent(/still in progress/i);
  });

  it('leaves hasRuns undefined while the unfiltered read is in flight', () => {
    // The third copy: neither "has not run" nor the has-run explanation. The
    // filtered read having answered must not stand in for the unfiltered one.
    renderWith(undefined, envelope(runningRows(2), { total_count: 2 }));

    const empty = screen.getByTestId('cost-empty');
    expect(empty.textContent ?? '').not.toMatch(/has not run/i);
    expect(empty.textContent ?? '').not.toMatch(/still in progress/i);
  });
});

/* ------------------------------------------------------------------ */
/* Property — the rendered number is the filtered read's total         */
/* ------------------------------------------------------------------ */

describe('property — the number is the walk total, whatever the pages hold', () => {
  /**
   * A filtered response whose row count and total are generated independently,
   * within the constraint the wire imposes (a page holds at most `page_size`
   * rows, and a page of rows implies at least that many matches).
   *
   * **Validates: Requirements 2.1**
   */
  const filteredArb = fc
    .tuple(fc.integer({ min: 1, max: 60 }), fc.boolean())
    .map(([total, truncated]) =>
      envelope(runningRows(total), {
        total_count: total,
        total_pages: Math.ceil(total / 10),
        partial_counts: truncated ? ['total_count', 'total_pages'] : [],
      }),
    );

  /** Generated independently, and free to contain any number of RUNNING rows. */
  const unfilteredArb = fc
    .array(
      fc.record({
        run_id: fc.integer({ min: 0, max: 999 }).map((n) => `run-unfiltered-${n}`),
        status: fc.constantFrom('RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'WAITING_APPROVAL'),
        started_at: fc.constant('2026-08-24T00:00:00Z'),
      }),
      { maxLength: 10 },
    )
    .map((rows) => envelope(rows as ExecRow[]));

  it('renders total_count regardless of either response’s row count', () => {
    fc.assert(
      fc.property(unfilteredArb, filteredArb, (unfiltered, filtered) => {
        cleanup();
        renderWith(unfiltered, filtered);

        const rendered = renderedRunningCount();
        expect(rendered).toBe(filtered.pagination.total_count);
        // And it is not merely equal by accident to a row count that happens to
        // match — where the two differ, the total wins.
        if (filtered.executions.length !== filtered.pagination.total_count) {
          expect(rendered).not.toBe(filtered.executions.length);
        }
      }),
      { numRuns: 40 },
    );
  });
});
