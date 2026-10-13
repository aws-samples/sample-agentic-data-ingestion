// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 5.2 — Property 5, generatively over the whole skip input space.
 *
 * **Validates: Requirements 6.1, 6.2**
 *
 * > **Property 5: No skip, by any reason or id shape, is told it does not
 * > exist.** *For any* skip reason — the three known ones, an unrecognised one,
 * > and none — and **either** id shape, the page reached from a run list renders a
 * > statement of why the run was skipped, and does **not** render the sentence
 * > claiming no run with that id exists.
 *
 * **Why this property is not optional, in the plan's own words.** It "is the only
 * check that no skip, by any reason or either id shape, is told it does not
 * exist, and the shape of that bug is a false statement to an operator rather
 * than a stack trace, so nothing else in the suite would notice." A page that
 * denies a run's existence throws nothing, logs nothing and fails no type check.
 *
 * **What this adds over the example suite.**
 * `RunDetailPage.skipped-run.test.tsx` walks four reasons and two ids by hand.
 * This file sweeps the cross product — reason × id shape × `previous_run_id`
 * present/absent × `file_name` present/absent — so a branch condition that
 * happens to hold for the enumerated fixtures and not in general fails here.
 * The unrecognised-reason arm is the one an example suite structurally cannot
 * cover: there is no list of unrecognised reasons to enumerate.
 *
 * **Rendered, not derived.** The assertion reads the DOM the page produced and
 * compares it to `describeSkip`'s answer for the same row. No skip copy is
 * transcribed anywhere in this file, so the two cannot drift while it still
 * passes — the failure mode `utils/skip-reason.ts`'s p30 R10.2 docstring is
 * written against, and the same drift class R7 fixes between the two run lists.
 *
 * **The state comes from the production writer.** `buildSkippedRunNavState` is
 * what both run lists call (`ExecutionsPage.tsx:488`,
 * `PipelineOverviewPage.tsx:615`), so "the page reached from a list" is a claim
 * about the real path: a renamed router-state key or a dropped field fails here
 * rather than being papered over by a hand-built fixture.
 *
 * **Never keyed on the run id — do-NOT 4.** The generator draws from **both**
 * shapes: `skipped-{fingerprint}` for a duplicate
 * (`pipeline_trigger._duplicate_skip_record_id`) and
 * `run-{window}-skip-{reason}` for contention (`_contention_skip_record_id`),
 * the latter `run-`-prefixed deliberately (`LGR-21a1b5b-4`). Nothing below reads
 * a prefix, and neither does the page: `status` + `skip_reason` is the
 * discriminator, as `pipeline_trigger:373` states on the writer side and
 * `use-executions.ts:69` on the reader side.
 *
 * **Mutation-tested — see the last describe block.** The gate is nulled through
 * a wrapped `describeSkip`, which makes branch 2 unreachable, and the property's
 * assertions are then asserted to *fail*. That single mutation covers both of the
 * task's named mutations, and the reason is structural rather than a shortcut:
 * branch 3's condition (`!isError && runDetail === null`) is a strict superset of
 * branch 2's, with no guard branch 2 lacks. So "branch 2 removed" and "branch 2
 * placed after branch 3" produce the *same* render — branch 3 wins either way —
 * and the check below proves the suite notices that render. The ordering half was
 * additionally confirmed by moving the branch in the source, running this file,
 * and reverting.
 *
 * **DOM assertions under jsdom, not observations of what a user sees** — the same
 * limitation `RunDetailPage.error-states.test.tsx` records.
 *
 * fast-check `^3.15.0` (an existing dependency), `{ numRuns: 100 }` per the
 * in-tree convention.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { buildSkippedRunNavState } from '../../utils/skipped-run-descriptor';
import {
  SKIPPED_RUN_FILE_LABEL,
  SKIPPED_RUN_PREVIOUS_RUN_LABEL,
} from '../RunDetailPage';

/**
 * The mutation lever. `describeSkip` is the gate `readCarriedSkip` asks — the
 * same authority `buildSkippedRunNavState` asks on the list side — so forcing it
 * to `null` makes branch 2 unreachable without editing the page. Off for every
 * test but the last block.
 */
let branchTwoDisabled = false;
vi.mock('../../utils/skip-reason', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/skip-reason')>();
  return {
    ...actual,
    describeSkip: (...args: Parameters<typeof actual.describeSkip>) =>
      branchTwoDisabled ? null : actual.describeSkip(...args),
  };
});
import { describeSkip } from '../../utils/skip-reason';

const PIPELINE_ID = 'newtest-ab2a08a0';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

/**
 * `data: null` with `isError: false` throughout — the 404 a skip's run id
 * correctly produces (`use-run-detail.ts:29-30`). R6.3 / R8.2 are untouched by
 * this task, so this is the real response, not a degraded one.
 */
vi.mock('../../hooks', () => ({
  useRunDetail: () => ({
    data: null,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
    isWebSocketConnected: false,
  }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRunTraces: () => ({ data: null, isLoading: false }),
  useSelfHealingActivity: () => ({ data: undefined }),
  useQualityReport: () => ({ data: null, isError: false, refetch: vi.fn() }),
  useRunNeighbours: () => ({ data: undefined }),
  usePipelineDetail: () => ({ data: undefined, refetch: vi.fn() }),
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useMappingRegenPolling: () => {},
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  qualityReportKeys: { run: (runId: string) => ['quality-report', runId] },
}));

import RunDetailPage from '../RunDetailPage';

type Row = Record<string, unknown>;

// ---------------------------------------------------------------------------
// The input space
// ---------------------------------------------------------------------------

/**
 * The three reasons a producer writes today, named at
 * `utils/skip-reason.ts:45-50`: `shared/processed_file_ledger.py`'s
 * `SKIP_REASON_DUPLICATE`, and `pipeline_trigger`'s `SKIP_REASON_CONCURRENCY`
 * and `SKIP_REASON_LOCK_CONTENTION`.
 */
const KNOWN_REASONS: readonly string[] = [
  'duplicate_content',
  'global_concurrency',
  'pipeline_lock_contention',
];

/**
 * A reason this dashboard does not recognise — the fourth of the five shapes the
 * property names. `describeSkip` echoes the key into its label for this case, and
 * reports it AS unrecognised rather than guessing (`skip-reason.ts:104-109`).
 */
const unrecognisedReasonArb = fc
  .stringMatching(/^[a-z]{3,10}_[a-z]{3,10}$/)
  .filter((r) => !KNOWN_REASONS.includes(r));

/**
 * The five reason shapes: the three known ones, an unrecognised one, and none —
 * where "none" covers absent, empty and whitespace, all three of which
 * `describeSkip` treats as unrecorded (`skip-reason.ts:95-102`) and all three of
 * which `buildSkippedRunNavState` omits from the descriptor (`LGR-44d601e-1`).
 */
const skipReasonArb: fc.Arbitrary<string | undefined> = fc.oneof(
  fc.constantFrom(...KNOWN_REASONS),
  unrecognisedReasonArb,
  fc.constantFrom<string | undefined>(undefined, '', '   '),
);

/** Both id shapes, so nothing downstream can be keyed on a prefix (do-NOT 4). */
const skipRunIdArb = fc.oneof(
  fc.hexaString({ minLength: 16, maxLength: 32 }).map((h) => `skipped-${h}`),
  fc
    .tuple(
      fc.constantFrom('2026-08-26-11-30-00', '2026-08-02-14-00-00', '2026-07-14-09-15-00'),
      fc.constantFrom(...KNOWN_REASONS),
    )
    .map(([window, reason]) => `run-${window}-skip-${reason}`),
);

/** The run that already processed the content — a duplicate carries one, a contention skip does not. */
const previousRunIdArb = fc.option(
  fc.hexaString({ minLength: 6, maxLength: 6 }).map((h) => `run-2026-08-24-09-12-00-${h}`),
  { nil: undefined },
);

/** The trigger had a `source_key`, or it did not (`pipeline_trigger:488-505`). */
const fileNameArb = fc.option(fc.stringMatching(/^[a-z]{4,10}\.csv$/), { nil: undefined });

/**
 * The row's own recorded detail, or none. Letters and spaces only so the
 * generated sentence cannot itself contain the 404 phrasing this property
 * forbids — a row whose *data* said "no run with id … exists" would be outside
 * the property's frame rather than a counterexample to it.
 */
const skipDetailArb = fc.option(fc.stringMatching(/^[A-Za-z ]{5,60}\.$/), { nil: undefined });

const skipRowArb: fc.Arbitrary<Row> = fc
  .record({
    run_id: skipRunIdArb,
    skip_reason: skipReasonArb,
    skip_detail: skipDetailArb,
    previous_run_id: previousRunIdArb,
    file_name: fileNameArb,
  })
  .map((r) => ({ ...r, status: 'SKIPPED', started_at: '2026-08-26T12:00:00Z' }));

/**
 * Render the page exactly as a Run_List_Surface reaches it: the router carries
 * what the production builder produced for this row.
 */
function renderFromList(row: Row) {
  const runId = row.run_id as string;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        initialEntries={[
          { pathname: `/pipelines/${PIPELINE_ID}/runs/${runId}`, state: buildSkippedRunNavState(row) },
        ]}
      >
        <Routes>
          <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { view, runId, text: view.container.textContent ?? '' };
}

// ---------------------------------------------------------------------------
// Property 5
// ---------------------------------------------------------------------------

describe('Feature: p54-reused-and-skipped-run-honesty, Property 5: No skip, by any reason or id shape, is told it does not exist', () => {
  beforeEach(() => {
    branchTwoDisabled = false;
    cleanup();
  });

  /**
   * **Validates: Requirements 6.1, 6.2**
   *
   * For any skip reason and either id shape, the page reached from a list states
   * why the run was skipped and never claims no run with that id exists.
   */
  it('states the reason and never denies the run', () => {
    fc.assert(
      fc.property(skipRowArb, (row) => {
        cleanup();
        const { runId, text } = renderFromList(row);

        // R6.1 — both forms of the denial: the exact sentence for this id, and the
        // phrasing generally, so a reworded denial is caught too.
        expect(text).not.toContain(`No run with id ${runId} exists.`);
        expect(text).not.toMatch(/no run with id/i);

        // R6.2 — the reason, read from the one authority both run lists render,
        // never transcribed here. Label AND full detail, because the detail is
        // what carries the row's own `skip_detail`.
        const expected = describeSkip(
          row.status as string,
          row.skip_reason as string | undefined,
          row.skip_detail as string | undefined,
        );
        expect(expected).not.toBeNull();
        expect(screen.getByTestId('skip-reason-badge').textContent).toBe(expected!.label);
        expect(screen.getByTestId('run-detail-skip-reason-detail').textContent).toBe(
          expected!.detail,
        );
      }),
      { numRuns: 100 },
    );
  });

  /**
   * The same guard's other half, over the same generator: a page that invents a
   * previous run it was never given is as false a statement as one that denies
   * the run. A contention skip carries no `previous_run_id` at all, so absence
   * must stay absence (`LGR-44d601e-1`) — never blank, never a placeholder.
   */
  it('promises only the optional fields the row actually carried', () => {
    fc.assert(
      fc.property(skipRowArb, (row) => {
        cleanup();
        const { text } = renderFromList(row);
        const carried = buildSkippedRunNavState(row)!.skippedRun;

        if (carried.previous_run_id === undefined) {
          expect(screen.queryByTestId('run-detail-skip-previous-run')).not.toBeInTheDocument();
          expect(text).not.toContain(SKIPPED_RUN_PREVIOUS_RUN_LABEL);
        } else {
          expect(screen.getByTestId('run-detail-skip-previous-run').textContent).toContain(
            carried.previous_run_id,
          );
        }

        if (carried.file_name === undefined) {
          expect(screen.queryByTestId('run-detail-skip-file')).not.toBeInTheDocument();
          expect(text).not.toContain(SKIPPED_RUN_FILE_LABEL);
        } else {
          expect(screen.getByTestId('run-detail-skip-file').textContent).toContain(
            carried.file_name,
          );
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Mutation resistance
// ---------------------------------------------------------------------------

describe('Property 5 — mutation resistance', () => {
  beforeEach(() => {
    cleanup();
  });

  /**
   * With the gate nulled, branch 2 cannot fire. The page falls through to branch
   * 3 and denies the run — which is the original defect — so the property above
   * must fail. If this block passes while the property above also passes with the
   * mutation live, the property is not testing what it claims.
   *
   * This is the "branch 2 removed" mutation, and it is also the "branch 2 after
   * branch 3" mutation: branch 3 requires only `!isError && runDetail === null`,
   * which branch 2 requires too, so putting branch 2 second makes it unreachable
   * for exactly the inputs branch 3 already claims. Both mutations render the
   * same page, and this asserts the suite notices it.
   */
  it('the property fails when branch 2 is unreachable', () => {
    branchTwoDisabled = true;
    const row: Row = {
      run_id: 'skipped-8f14e45fceea167a5a36dedd4bea2543',
      status: 'SKIPPED',
      skip_reason: 'duplicate_content',
      skip_detail: 'Content already processed by an earlier run.',
      file_name: 'customers_aug.csv',
      started_at: '2026-08-26T12:00:00Z',
    };
    const { runId, text } = renderFromList(row);

    // The defect, back: the denial renders and no reason is stated.
    expect(text).toContain(`No run with id ${runId} exists.`);
    expect(screen.queryByTestId('skip-reason-badge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('run-detail-skip-reason-detail')).not.toBeInTheDocument();

    // So the property's own assertions do not hold under the mutation.
    expect(() => expect(text).not.toMatch(/no run with id/i)).toThrow();
    expect(() => screen.getByTestId('skip-reason-badge')).toThrow();
    branchTwoDisabled = false;
  });
});
