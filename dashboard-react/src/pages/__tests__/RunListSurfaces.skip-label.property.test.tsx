// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 1.5 — Property 6, generatively over the whole skip reason space.
 *
 * **Validates: Requirements 7.1, 7.2**
 *
 * > **Property 6: The two Run_List_Surfaces describe one skip with one label.**
 * > *For any* skip row — the three known reasons, an unrecognised one, and none —
 * > the label the Overview card renders equals the label the Executions tab
 * > renders for the same row, and neither surface renders the row's internal step
 * > name as the skip reason.
 *
 * **What this adds over task 1.3's example suite.**
 * `PipelineOverviewPage.skip-reason.test.tsx` asserts that the Overview card
 * renders `describeSkip`'s answer — one surface against the derivation. This file
 * asserts something the example suite structurally cannot: that the two surfaces'
 * **rendered** labels are the *same string*, for every reason shape. Both
 * assertions are needed and neither implies the other. A surface could read the
 * right derivation and still render it differently — truncated, re-cased, wrapped
 * in its own prefix — and 1.3 would pass. Equally, both surfaces could render one
 * agreed label that no longer comes from `describeSkip`, and this file would pass
 * while 1.3 failed. R7.1 is the conjunction.
 *
 * **Rendered-to-rendered, never label-to-literal.** No skip copy is transcribed
 * here. The assertion compares what the Overview card put in the DOM to what the
 * Executions tab put in the DOM, so a test carrying its own copy of the copy
 * cannot let the two surfaces drift while still passing — the failure mode
 * `utils/skip-reason.ts`'s p30 R10.2 docstring is written against ("an explicit
 * table" in exactly one place). The hover detail is compared the same way, and for
 * the same reason: it comes from the same single derivation, so a drift in it is
 * the same defect as a drift in the label.
 *
 * **Never keyed on the run id — do-NOT 4.** Two id shapes exist and one of them is
 * `run-`-prefixed on purpose (`LGR-21a1b5b-4`): `skipped-{fingerprint}` for a
 * duplicate, `run-{window}-skip-{reason}` for contention. The generator draws from
 * both and nothing below reads a prefix. `status` + `skip_reason` is the
 * discriminator, as `pipeline_trigger:373` states on the writer side and
 * `use-executions.ts:69` on the reader side.
 *
 * **R7.2's half is asserted against the badge, not against the page.** The
 * requirement is that the internal step name is not the *skip reason* — and the
 * two surfaces legitimately differ in what else they show: the Executions tab
 * still renders `({current_step})` beside the badge for a skip row
 * (`ExecutionsPage.tsx:345-349`), where the Overview card suppresses it
 * (`PipelineOverviewPage.tsx:532`). So "absent as a skip reason" means absent from
 * the skip description — the badge label and its hover text — on **both**
 * surfaces, which is the claim R7.2 actually makes and the one that is true of
 * both. The asymmetry in the parenthetical is R7.2 being one-sided by design and
 * is pinned by 1.3; this file pins the other half of it, that a **non-skip** row's
 * step name survives on both surfaces.
 *
 * **No ledger record governs `utils/skip-reason.ts`.** Confirmed rather than
 * re-derived: `why` on it returns only the BROAD `99050e6225` commit (59 files,
 * carrying its own `NOTE: BROAD`), whose five records rule on
 * `MetricsDashboardPage`, `QualityVerdictLabel`, `SchemaTab`, `GlueJobDetailPage`
 * and the contract-test type reader — none on the skip vocabulary.
 * `decisions(concept="testing")` with `include_backfilled=True` returns nothing
 * about `describeSkip` either. R7's authority for "one derivation, never a second
 * table of skip copy" is that file's own p30 R10.2 docstring, not the ledger.
 * `LGR-1f7892b-1` governs `PipelineOverviewPage.tsx` (the unfiltered page-1 read
 * feeding five rows with no pager) and `LGR-1f7892b-2` fences
 * `classifyCount` / `CountState`; this file touches neither — it renders the page
 * and reads the DOM.
 *
 * **Test-only.** `PipelineOverviewPage.tsx`, `ExecutionsPage.tsx` and
 * `utils/skip-reason.ts` are unchanged by this task.
 *
 * `api-client` is mocked **without** `importOriginal`, matching the sibling
 * Overview suites: this file must not execute the real module.
 *
 * fast-check `^3.15.0` (an existing dependency), `{ numRuns: 100 }` per the
 * in-tree convention.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

type Row = Record<string, unknown>;

/**
 * The producer's envelope, as `get_pipeline_executions` builds it on its success
 * arm (`pipelines_runs.py:1353-1381`), nested per `LGR-2bcdd0d-1`. The sibling
 * suites cite `:1148-1176` for this shape; that range is the schema-version
 * assignment loop now, so the line reference is corrected here rather than copied.
 * Those files are not edited — they must pass unmodified.
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
 * The union of both pages' hooks. `usePipelineExecutions` honours the status
 * argument because the Overview card reads it twice — unfiltered for the five-row
 * preview, then `?status=RUNNING` for its count (`PipelineOverviewPage.tsx:100`,
 * `:139`) — and the Executions tab reads it once.
 */
vi.mock('../../hooks', () => ({
  usePipelineExecutions: (
    _key: unknown,
    _page: unknown,
    _cursor: unknown,
    status?: string | null,
  ) => {
    if (status) return { data: envelope(rows.filter((r) => r.status === status)) };
    return { data: envelope(rows) };
  },
  // PipelineOverviewPage
  useArchivePipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
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
  // ExecutionsPage
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
  // both
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../services/api-client', () => ({
  ApiError: class ApiError extends Error {},
  apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

import PipelineOverviewPage from '../PipelineOverviewPage';
import ExecutionsPage from '../ExecutionsPage';

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

function renderSurface(surface: 'overview' | 'executions', executions: Row[]) {
  rows = executions;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PipelineProvider
          value={{ pipeline: mockPipeline, isLoading: false, isError: false, refetch: vi.fn() }}
        >
          {surface === 'overview' ? <PipelineOverviewPage /> : <ExecutionsPage />}
        </PipelineProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// The input space
// ---------------------------------------------------------------------------

/**
 * The three reasons a producer writes today, named at
 * `utils/skip-reason.ts:45-50`: `shared/processed_file_ledger.py`'s
 * `SKIP_REASON_DUPLICATE`, and `pipeline_trigger`'s `SKIP_REASON_CONCURRENCY` and
 * `SKIP_REASON_LOCK_CONTENTION`.
 */
const KNOWN_REASONS: readonly string[] = [
  'duplicate_content',
  'global_concurrency',
  'pipeline_lock_contention',
];

/**
 * The internal gate names that reach a skip row's `current_step` — the strings
 * R7.2 forbids standing in for the reason.
 */
const GATE_STEPS: readonly string[] = [
  'duplicate_check',
  'concurrency_gate',
  'pipeline_lock_gate',
];

/**
 * A reason this dashboard does not recognise — the fourth of the five shapes the
 * property names. `describeSkip` echoes the key into its label for this case
 * (`Skipped: ${key}`), which is why the generator excludes any string containing a
 * gate name: a row whose *recorded reason* literally is the gate name is outside
 * this property's frame, since then the step name in the badge would be the row's
 * own data rather than the internal name leaking. Excluding the three known
 * reasons keeps this arm genuinely unrecognised.
 */
const unrecognisedReasonArb = fc
  .stringMatching(/^[a-z]{3,10}_[a-z]{3,10}$/)
  .filter((r) => !KNOWN_REASONS.includes(r) && !GATE_STEPS.some((s) => r.includes(s)));

/**
 * The five reason shapes: the three known ones, an unrecognised one, and none —
 * where "none" covers absent, empty and whitespace, all three of which
 * `describeSkip` treats as unrecorded (`skip-reason.ts:95-102`).
 */
const skipReasonArb: fc.Arbitrary<string | undefined> = fc.oneof(
  fc.constantFrom(...KNOWN_REASONS),
  unrecognisedReasonArb,
  fc.constantFrom<string | undefined>(undefined, '', '   '),
);

/**
 * Both id shapes, drawn deliberately so that nothing downstream can be keyed on a
 * prefix without failing here (do-NOT 4).
 */
const skipRunIdArb = fc.oneof(
  fc.hexaString({ minLength: 16, maxLength: 32 }).map((h) => `skipped-${h}`),
  fc
    .tuple(
      fc.constantFrom('2026-08-26-11-30-00', '2026-08-02-14-00-00', '2026-07-14-09-15-00'),
      fc.constantFrom(...KNOWN_REASONS),
    )
    .map(([window, reason]) => `run-${window}-skip-${reason}`),
);

/**
 * The row's own recorded detail, or none. Restricted to letters and spaces so it
 * cannot itself contain an underscore-joined gate name — the R7.2 assertion below
 * reads the hover text as well as the label.
 */
const skipDetailArb = fc.option(fc.stringMatching(/^[A-Za-z ]{5,40}\.$/), { nil: undefined });

const skipRowArb = fc
  .record({
    run_id: skipRunIdArb,
    skip_reason: skipReasonArb,
    skip_detail: skipDetailArb,
    current_step: fc.constantFrom(...GATE_STEPS),
    file_name: fc.stringMatching(/^[a-z]{4,10}\.csv$/),
  })
  .map((r) => ({
    ...r,
    execution_id: `exec-${r.run_id}`,
    status: 'SKIPPED',
    started_at: '2026-08-26T12:00:00Z',
  }));

/**
 * A row that is not a skip, carrying a real pipeline step. Its step name must
 * survive on both surfaces — R7.2 is one-sided, and the step name is the only
 * place either list says where a run has got to. The two surfaces spell it
 * differently for a run in flight (`→ step` on the Executions tab, `(step)` on the
 * Overview card), so the assertion below reads the container text rather than an
 * exact node.
 */
const nonSkipRowArb = fc
  .record({
    status: fc.constantFrom('RUNNING', 'SUCCEEDED', 'FAILED'),
    current_step: fc.constantFrom('create_code', 'test_code', 'create_glue_pipeline'),
  })
  .map((r) => ({
    ...r,
    run_id: 'run-2026-08-26-09-00-00-aabbcc',
    execution_id: 'exec-live',
    file_name: 'customer_live.csv',
    started_at: '2026-08-26T09:00:00Z',
  }));

/** The badge's text and its hover detail, as one surface actually rendered them. */
function readSkipDescription(container: HTMLElement): { label: string; detail: string } {
  const badge = within(container).getByTestId('skip-reason-badge');
  return {
    label: badge.textContent ?? '',
    detail: badge.closest('[title]')?.getAttribute('title') ?? '',
  };
}

// ---------------------------------------------------------------------------
// Property 6
// ---------------------------------------------------------------------------

describe('Feature: p54-reused-and-skipped-run-honesty, Property 6: The two Run_List_Surfaces describe one skip with one label', () => {
  beforeEach(() => {
    cleanup();
  });

  /**
   * **Validates: Requirements 7.1, 7.2**
   *
   * For any skip row — the three known reasons, an unrecognised one, and none —
   * and either id shape: the label the Overview card renders equals the label the
   * Executions tab renders, and the internal step name is absent from the skip
   * description on both surfaces while surviving on a row that is not a skip.
   */
  it('renders one label and one hover detail for the same skip row on both surfaces', () => {
    fc.assert(
      fc.property(skipRowArb, nonSkipRowArb, (skipRow, nonSkipRow) => {
        cleanup();

        // Same two rows, same order, to each surface in turn.
        const both = [skipRow as Row, nonSkipRow as Row];

        const overview = renderSurface('overview', both);
        const overviewSkip = readSkipDescription(overview.container);
        const overviewText = overview.container.textContent ?? '';
        cleanup();

        const executions = renderSurface('executions', both);
        const executionsSkip = readSkipDescription(executions.container);
        const executionsText = executions.container.textContent ?? '';
        cleanup();

        // R7.1 — one skip, one label. Rendered against rendered: no literal from
        // the reason table appears in this file, so the two surfaces cannot drift
        // apart and still pass.
        expect(overviewSkip.label).toBe(executionsSkip.label);
        expect(overviewSkip.label.length).toBeGreaterThan(0);
        // The same holds for the hover text, which comes from the same derivation
        // and carries the row's own recorded detail when it has one.
        expect(overviewSkip.detail).toBe(executionsSkip.detail);
        expect(overviewSkip.detail.length).toBeGreaterThan(0);

        // R7.2 — the internal step name is not the skip reason, on either
        // surface. Asserted against the skip description rather than the page,
        // because the Executions tab legitimately still shows the step in a
        // parenthetical beside the badge.
        const gate = skipRow.current_step;
        expect(overviewSkip.label).not.toContain(gate);
        expect(executionsSkip.label).not.toContain(gate);
        expect(overviewSkip.detail).not.toContain(gate);
        expect(executionsSkip.detail).not.toContain(gate);

        // R7.2 is one-sided: a non-skip row's step name survives on both
        // surfaces. Dropping it here would be a regression dressed up as
        // consistency.
        expect(overviewText).toContain(nonSkipRow.current_step);
        expect(executionsText).toContain(nonSkipRow.current_step);
      }),
      { numRuns: 100 },
    );
  });
});
