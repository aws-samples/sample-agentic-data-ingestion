// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The overview "Recent Runs" card shows the five genuinely newest runs —
 * p48 task 5.3 (C10, R10.3, R10.4).
 *
 * WHAT WAS BROKEN, AND WHERE THE FIX ACTUALLY LANDED
 * -------------------------------------------------
 * The card is `PipelineOverviewPage.tsx:201`, `.slice(0, 5)` over
 * `execData.executions` (`:44`). The slice was never wrong; the list handed to
 * it was. Before task 2.2 the producer queried the base table, whose sort key is
 * `run_id`, so "newest first" grouped by id **prefix** —
 * `queued-` < `run-` < `skipped-` — and every duplicate skip pinned above every
 * run for as long as the row lived. This card is the most visible instance of
 * the operator's original complaint: five rows, no pager, two of them skips.
 *
 * Task 2.2 moved the read to `RunsByTimeIndex` with `ScanIndexForward=False`
 * (`pipelines_runs.py:870-874`), so the ordering authority is the server's index
 * and the card is a pass-through. This file pins that pass-through property,
 * because it is the only part of the guarantee the frontend owns.
 *
 * WHAT A TEST ON THIS PAGE CAN AND CANNOT PROVE
 * ---------------------------------------------
 * Task 1.2 established that every page test mocks `../../hooks` wholesale, and
 * there is no way to render this page without doing so. So no test here can
 * exercise the real DynamoDB ordering — the mock supplies the order. What it
 * *can* prove, and what R10.4 actually asks for, is that the component adds **no
 * second ordering authority**: whatever order arrives is the order rendered, and
 * the five shown are the input's first five. That makes the server's order the
 * only order, which is the whole of R10.4.
 *
 * HOW THE TWO ORDERINGS ARE KEPT HONEST
 * -------------------------------------
 * `LIVE_PARTITION` is the nine rows of `latesttesting-19ca826a` (the validation account /
 * us-west-2), the partition `requirements.md` §Introduction tabulates. Both
 * orderings are **derived from that one fixture by sorting** rather than
 * transcribed, and `OBSERVED_*` are the orders two live queries actually
 * returned — so the fixture is checked against reality before it is used as
 * input, and neither ordering can be quietly hand-fudged into agreeing with the
 * assertion.
 *
 * The two orderings differ in their first five by exactly one swap, and that
 * swap is the defect: the **08-20** skip is in, and the **08-21** run is out.
 * `renders the five newest ...` asserts the post-2.2 five; `reproduces the
 * pre-2.2 defect ...` feeds the pre-2.2 order and asserts the *wrong* five, so
 * the first test is demonstrably order-sensitive rather than passing by
 * construction.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

interface Row {
  run_id: string;
  execution_id: string;
  file_name: string;
  started_at: string;
  status: string;
}

/**
 * `latesttesting-19ca826a`, all nine rows, verified against the validation account /
 * us-west-2 on 2026-08-24. Declared in an order that is neither of the two under
 * test (base-table insertion order would be an accident either way), so nothing
 * below can pass by inheriting the literal's order.
 */
const LIVE_PARTITION: Row[] = [
  {
    run_id: 'run-2026-08-20-17-52-21-248afa7a',
    execution_id: 'exec-17-52-21',
    file_name: 'customer_data_3.csv',
    started_at: '2026-08-20T17:52:21.739239+00:00',
    status: 'SUCCEEDED',
  },
  {
    run_id: 'run-2026-08-24-13-48-26-4fccdf03',
    execution_id: 'exec-13-48-26',
    file_name: 'customer_data_6.csv',
    started_at: '2026-08-24T13:48:26.863631+00:00',
    status: 'SUCCEEDED',
  },
  {
    // The duplicate skip that opened this investigation: correct as a skip,
    // wrong only in its position. Newest-by-time on 08-24, so it legitimately
    // belongs in the five.
    run_id: 'skipped-7412636fccb938719673179658c05773',
    execution_id: 'exec-skip-7412636f',
    file_name: 'customer_data_3.csv',
    started_at: '2026-08-24T13:04:04.864784+00:00',
    status: 'SKIPPED',
  },
  {
    run_id: 'run-2026-08-20-17-59-33-8505fa5b',
    execution_id: 'exec-17-59-33',
    file_name: 'customer_data_5.csv',
    started_at: '2026-08-20T17:59:33.097863+00:00',
    status: 'SUCCEEDED',
  },
  {
    // The row the defect is measured by. Four days old, and `'s' > 'r'` put it
    // second on this card under the base-table ordering.
    run_id: 'skipped-42e546628820e9630a45c6bcfd8792e1',
    execution_id: 'exec-skip-42e54662',
    file_name: 'customer_data_4 copy.csv',
    started_at: '2026-08-20T20:51:59.783707+00:00',
    status: 'SKIPPED',
  },
  {
    run_id: 'run-2026-08-22-12-43-57-f009d524',
    execution_id: 'exec-12-43-57',
    file_name: 'customer_data.avro',
    started_at: '2026-08-22T12:43:57.671036+00:00',
    status: 'SUCCEEDED',
  },
  {
    run_id: 'run-2026-08-20-18-08-57-70909254',
    execution_id: 'exec-18-08-57',
    file_name: 'customer_data_4.csv',
    started_at: '2026-08-20T18:08:57.353328+00:00',
    status: 'SUCCEEDED',
  },
  {
    // The row the 08-20 skip displaced: present in the newest five by time,
    // absent from the newest five by `run_id`.
    run_id: 'run-2026-08-21-17-13-33-9c2bc0e6',
    execution_id: 'exec-17-13-33',
    file_name: 'customer_data_2.csv',
    started_at: '2026-08-21T17:13:33.751749+00:00',
    status: 'SUCCEEDED',
  },
  {
    run_id: 'run-2026-08-24-13-04-01-421fc544',
    execution_id: 'exec-13-04-01',
    file_name: 'customer_data_1.csv',
    started_at: '2026-08-24T13:04:01.691321+00:00',
    status: 'SUCCEEDED',
  },
];

/** What the producer sends since task 2.2 — `RunsByTimeIndex`, descending. */
const byStartedAtDesc = [...LIVE_PARTITION].sort((a, b) =>
  a.started_at < b.started_at ? 1 : a.started_at > b.started_at ? -1 : 0,
);

/** What the producer sent before task 2.2 — base table, `run_id` descending. */
const byRunIdDesc = [...LIVE_PARTITION].sort((a, b) =>
  a.run_id < b.run_id ? 1 : a.run_id > b.run_id ? -1 : 0,
);

/**
 * `query --index-name RunsByTimeIndex --no-scan-index-forward`, verbatim.
 * The oracle for `byStartedAtDesc`, so the sort above is checked against a live
 * response rather than trusted.
 */
const OBSERVED_BY_TIME = [
  'run-2026-08-24-13-48-26-4fccdf03',
  'skipped-7412636fccb938719673179658c05773',
  'run-2026-08-24-13-04-01-421fc544',
  'run-2026-08-22-12-43-57-f009d524',
  'run-2026-08-21-17-13-33-9c2bc0e6',
  'skipped-42e546628820e9630a45c6bcfd8792e1',
  'run-2026-08-20-18-08-57-70909254',
  'run-2026-08-20-17-59-33-8505fa5b',
  'run-2026-08-20-17-52-21-248afa7a',
];

/** The same query against the base table — the pre-2.2 response, verbatim. */
const OBSERVED_BY_RUN_ID = [
  'skipped-7412636fccb938719673179658c05773',
  'skipped-42e546628820e9630a45c6bcfd8792e1',
  'run-2026-08-24-13-48-26-4fccdf03',
  'run-2026-08-24-13-04-01-421fc544',
  'run-2026-08-22-12-43-57-f009d524',
  'run-2026-08-21-17-13-33-9c2bc0e6',
  'run-2026-08-20-18-08-57-70909254',
  'run-2026-08-20-17-59-33-8505fa5b',
  'run-2026-08-20-17-52-21-248afa7a',
];

/** The 08-20 skip. Named because its presence is the defect. */
const STALE_SKIP = 'skipped-42e546628820e9630a45c6bcfd8792e1';
/** The 08-21 run the stale skip displaced. */
const DISPLACED_RUN = 'run-2026-08-21-17-13-33-9c2bc0e6';

/**
 * The producer's envelope (`pipelines_runs.py:1148-1176`), nested `pagination`
 * per C4 Arm B. The card reads only `executions`, but the fixture carries the
 * real shape so a drift shows up here rather than as a page rendering nothing —
 * the mistake task 1.2 found in the pre-p48 mocks, which carried a flat
 * pagination block no producer has ever emitted.
 */
function envelope(executions: Row[]) {
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
      partial_counts: [] as string[],
    },
  };
}

let rows: Row[] = [];
/**
 * Every `(pipelineKey, page, cursor, status)` the card asked the hook for, in
 * order. `status` was added by p51 task 4.2, which gave the card a **second**
 * call of this hook for the running count — see the amended assertion below.
 */
const hookCalls: Array<{
  key: unknown;
  page: unknown;
  cursor: unknown;
  status: string | null | undefined;
}> = [];

vi.mock('../../hooks', () => ({
  usePipelineExecutions: (
    key: unknown,
    page: unknown,
    cursor: unknown,
    status?: string | null,
  ) => {
    hookCalls.push({ key, page, cursor, status });
    // Status-aware on purpose: the server filters (p51 C3), so a `?status=`
    // read must not be answered with the unfiltered partition. Answering both
    // calls with the whole fixture would have this file assert "9 runs in
    // progress" over a fixture containing no RUNNING row at all.
    if (status) {
      return { data: envelope(rows.filter((r) => r.status === status)) };
    }
    return { data: envelope(rows) };
  },
  useArchivePipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  // p51 task 4.1 — the approvals half of the Status cell reads this instead of
  // `latestExecs`. Empty here: this file is about Recent Runs' ordering, and the
  // fixture rows carry no pending approval.
  usePipelinePendingApprovals: () => ({ data: [] }),
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

const mockPipeline: Pipeline = {
  pipeline_key: 'latesttesting-19ca826a',
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

function renderWith(executions: Row[]) {
  rows = executions;
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
 * The run ids the card rendered, in DOM order. Read off the per-row "View Run"
 * link (`PipelineOverviewPage.tsx:243-247`), which is the only place a row's
 * `run_id` reaches the DOM.
 */
function renderedRunIds(): string[] {
  return screen
    .getAllByRole('link', { name: 'View Run' })
    .map((a) => a.getAttribute('href') ?? '')
    .map((href) => href.replace(`/pipelines/${mockPipeline.pipeline_key}/runs/`, ''));
}

beforeEach(() => {
  hookCalls.length = 0;
  cleanup();
});

describe('PipelineOverviewPage — Recent Runs order (p48 task 5.3)', () => {
  it('the fixture reproduces both live orderings, so neither can be hand-fudged', () => {
    expect(byStartedAtDesc.map((r) => r.run_id)).toEqual(OBSERVED_BY_TIME);
    expect(byRunIdDesc.map((r) => r.run_id)).toEqual(OBSERVED_BY_RUN_ID);
    // The two disagree, and disagree inside the first five — otherwise this
    // whole file would be asserting nothing.
    expect(OBSERVED_BY_TIME.slice(0, 5)).not.toEqual(OBSERVED_BY_RUN_ID.slice(0, 5));
  });

  it('renders the five newest runs by started_at, and not the four-day-old skip (R10.3)', () => {
    renderWith(byStartedAtDesc);

    const ids = renderedRunIds();
    expect(ids).toHaveLength(5);
    expect(ids).toEqual(OBSERVED_BY_TIME.slice(0, 5));
    // The operator's complaint, stated as the thing that must not happen.
    expect(ids).not.toContain(STALE_SKIP);
    // ... and the run it used to displace is present.
    expect(ids).toContain(DISPLACED_RUN);
    // The 08-24 skip IS newest-by-time, so it stays — the skip was never the
    // problem, only its position.
    expect(ids[1]).toBe('skipped-7412636fccb938719673179658c05773');
  });

  it('reproduces the pre-2.2 defect when fed the pre-2.2 order, so the assertion above is order-sensitive', () => {
    renderWith(byRunIdDesc);

    const ids = renderedRunIds();
    expect(ids).toEqual(OBSERVED_BY_RUN_ID.slice(0, 5));
    // Both skips on top, the 08-20 one above three newer runs — this is the
    // screenshot the operator reported.
    expect(ids[1]).toBe(STALE_SKIP);
    // And the 08-21 run falls off the card entirely.
    expect(ids).not.toContain(DISPLACED_RUN);
  });

  it('renders the input order verbatim — no client-side re-sort (R10.4)', () => {
    // Deliberately neither ordering: if the card sorted by time it would render
    // OBSERVED_BY_TIME's five, and if it sorted by id it would render
    // OBSERVED_BY_RUN_ID's five. It renders neither, because it sorts nothing.
    const scrambled = [
      LIVE_PARTITION[0],
      LIVE_PARTITION[4],
      LIVE_PARTITION[1],
      LIVE_PARTITION[6],
      LIVE_PARTITION[2],
      LIVE_PARTITION[8],
    ];
    renderWith(scrambled);

    const ids = renderedRunIds();
    expect(ids).toEqual(scrambled.slice(0, 5).map((r) => r.run_id));
    expect(ids).not.toEqual(OBSERVED_BY_TIME.slice(0, 5));
    expect(ids).not.toEqual(OBSERVED_BY_RUN_ID.slice(0, 5));
  });

  it('asks for page 1 with no cursor — a preview, not a pager (R10.3)', () => {
    renderWith(byStartedAtDesc);

    /**
     * **Amended by p51 task 4.2, on purpose, and left legible as a reversal**
     * rather than deleted — the convention `LGR-9e76249-10` follows from
     * `LGR-4c4b4c9-6` / `LGR-60c4985-6` (**template / ratified**): a deliberate
     * hold encoded as an assertion is changed knowingly and stays readable as a
     * change of mind, not as a regression.
     *
     * This previously read `expect(hookCalls).toHaveLength(1)`. p51 C3 gives the
     * card a second call of the same hook — `(key, 1, null, 'RUNNING')` — for the
     * running count, so exactly one call is no longer true and was never the
     * subject. The subject was and is: **the read behind Recent Runs is a single
     * unfiltered page 1 with no cursor.** That is what is asserted now, by
     * isolating the unfiltered call instead of counting all of them, so the
     * assertion still fails if this surface acquires a cursor stack, a second
     * preview read, or a pager.
     */
    const unfiltered = hookCalls.filter((c) => !c.status);
    expect(unfiltered).toHaveLength(1);
    expect(unfiltered[0]).toMatchObject({ key: mockPipeline.pipeline_key, page: 1 });
    // Third argument omitted: the card has no cursor stack and must never
    // acquire one. `undefined` here, not `null`, is what the unfiltered call
    // passes.
    expect(unfiltered[0].cursor).toBeUndefined();

    // The second call is the running count's, and it is the *only* other one.
    expect(hookCalls).toHaveLength(2);
    expect(hookCalls.filter((c) => c.status === 'RUNNING')).toHaveLength(1);

    // R10.3 — a preview by design. No pagination control on this surface.
    expect(screen.queryByRole('button', { name: /Next page/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Previous page/i })).not.toBeInTheDocument();
  });

  it('shows every row when the pipeline has fewer than five', () => {
    renderWith(byStartedAtDesc.slice(0, 3));
    expect(renderedRunIds()).toEqual(OBSERVED_BY_TIME.slice(0, 3));
  });
});
