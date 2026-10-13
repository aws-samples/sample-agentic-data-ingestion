// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 7.5 — Property 8, generatively over the whole `queued_files` shape space.
 *
 * **Validates: Requirements 9.3, 9.4, 9.5, 10.5**
 *
 * > **Property 8: A deferred, a lost, a refused and a never-submitted file are four
 * > facts and never collapse.**
 * > *For any* `queued_files` array — entries in either published state, an
 * > unrecognised status string, an entry with a blank file name, an empty array
 * > beside a non-empty `executions` list, and a `SKIPPED` executions row alongside —
 * > each rendered entry carries exactly one of the two published claims, a lost entry
 * > carries its file name and the reason, a refused file renders from its
 * > `executions` row and never as a `queued_files` entry, and a file with no entry
 * > renders as neither.
 *
 * One property over one generator, deliberately: R9.4's field assertions and R10.5's
 * refusal state are absorbed here rather than split out, because three properties over
 * this generator would be three spellings of one guard.
 *
 * ---
 *
 * ## "Exactly one of {deferred, lost}" — how the unrecognised status is resolved
 *
 * The task text asks that each rendered entry be classifiable into **exactly one** of
 * {deferred, lost}. Taken literally against the render that is vacuous and against
 * R9.5 it is wrong, so it is resolved by separating the **label** from the **claim**.
 *
 * The Status cell is a ternary (`PipelineOverviewPage.tsx:721-726`,
 * `ExecutionsPage.tsx:606-609`), so it emits one of exactly two strings by
 * construction. Asserting "one label per row" alone would be a re-spelling of that
 * ternary and would pass for any predicate at all.
 *
 * What R9.5 actually governs is which **fact the surface claims**, and there are
 * three:
 *
 * | published `status` | render claim | cadence claim |
 * |---|---|---|
 * | `'failed_dlq'` | **lost** — "Failed (DLQ)" + the reason sentence | not work in flight |
 * | `'queued'` | **deferred** — "Queued" | work in flight (fast cadence) |
 * | anything else | *neither* — "Queued", the reader's pre-existing default, which makes no claim of loss | **not** work in flight |
 *
 * So an unrecognised status is a **third case and is asserted as neither**, which is
 * exactly why `isLostQueuedFile` and `isDeferredQueuedFile` are not negations of each
 * other (`utils/queued-file-state.ts:16-32`). Collapsing it into either fact would be
 * the invented claim `LGR-99050e6-2` (**template / ratified**) prevents — "an absent
 * quarantine counter is reported as unknown rather than as zero".
 *
 * The unrecognised row is therefore observable as *neither* only because two channels
 * are read, not one:
 *
 * - the **render** channel — its label, and whether the page makes the loss claim;
 * - the **cadence** channel — `computeExecutionsRefetchInterval`, which is where "do
 *   we know this file is still coming back?" is answered.
 *
 * Reading only the render would leave the unrecognised row indistinguishable from a
 * deferred one; reading only the cadence would leave it indistinguishable from a lost
 * one. The pair is what makes three states three.
 *
 * ## Expectations come from the published literals, never from the predicates
 *
 * `QUEUED_STATUS_DEFERRED` / `QUEUED_STATUS_LOST` below are written out as literals
 * rather than imported from `utils/queued-file-state.ts`. That is load-bearing and not
 * a style choice: a property whose oracle *is* the module under test is self-
 * consistent and would pass under any mutation of it. The literals are R9.3's own
 * published states, sourced from `_split_derive_and_filter`'s step-1 comprehension
 * (`handlers/pipelines_runs.py:763-771`) — the producer, not the reader. The same
 * reasoning `LGR-28b4d75-5` (**template / ratified**) gives for deriving a contract
 * test's expectations from the frontend interface rather than from producer output:
 * "a test written from the producer's output cannot detect the producer drifting,
 * because the output is its own oracle".
 *
 * ## Words, not colour
 *
 * Every assertion below reads text. Nothing reads a `StatusIndicator` `type`, a badge
 * colour or a CSS class. `LGR-99050e6-1` (**template / ratified**) records that colour
 * alone fails **WCAG 1.4.1**, and `LGR-1f7892b-2` re-states it for a
 * `StatusIndicator` specifically — "a `warning` type *is* colour, so the sentence is
 * the carrier and the type is not". A property that asserted on the indicator type
 * would pass for a surface that carried the distinction only in colour.
 *
 * ## Why a refusal is a different fact from a deferral
 *
 * R10.5, and do-NOT 18 / `LGR-21a1b5b-4` from the reader side. A `SKIPPED` row is a
 * refusal: something decided not to run, in front of the person who asked or on a
 * schedule that will fire again. A `queued_files` entry is a deferral: the file is
 * coming back. `LGR-21a1b5b-2` (**template / ratified**) is why the two can never
 * share a surface by accident — a non-run row on the executions table "would render as
 * a phantom run unless three separate readers learned to ignore it", which is why the
 * queued placeholder is split out *before* the run list is built and why a refusal
 * stays in `executions`. This file asserts both halves: the refusal renders as a run,
 * with a run link and a skip badge; and it never adds a row to the queued region, nor
 * pulls in the loss claim.
 *
 * ## What this adds over 7.4's twelve example tests
 *
 * `PipelineOverviewPage.queued-files.test.tsx` and
 * `ExecutionsPage.queued-files.test.tsx` assert named examples in both directions — a
 * deferred entry renders, a lost one renders with its reason, an empty array renders
 * nothing, a deferred-only page says nothing about loss. All four are single-shape
 * assertions on the two *recognised* statuses. What no example among them reaches:
 *
 * - the **unrecognised** status at all, on either channel;
 * - an entry with a **blank** file name;
 * - **cardinality** — that the number of claims equals the number of entries, so no
 *   entry renders twice and none is silently dropped;
 * - a **refusal beside a deferral** in one render, which is where the two facts would
 *   collapse if they were going to;
 * - the **cadence** channel keyed to the same array the render read.
 *
 * This file is not a thirteenth example: every assertion below is over a generated
 * multiset, and the shapes that falsify it are the ones no example enumerates.
 *
 * ## Test-only
 *
 * `PipelineOverviewPage.tsx`, `ExecutionsPage.tsx`, `hooks/use-executions.ts` and
 * `utils/queued-file-state.ts` are unchanged by this task, and 7.4's two example files
 * must pass unmodified.
 *
 * ## Two out-of-scope observations, recorded rather than acted on
 *
 * 1. **`SKIPPED` is not in `TERMINAL_STATUSES`** (`utils/formatting.ts:8`), so a
 *    refused run counts as work in flight for polling purposes and holds the fast
 *    cadence on its own. That is why the cadence assertion below is made against an
 *    executions-free envelope: it isolates `hasQueued`, which is the half this property
 *    is about. Whether a refusal should hold the cadence is a question for
 *    `LGR-e800696-1`'s matrix, which p54 do-NOT 17 / R8.14 forbid this spec to retune.
 * 2. `why` on `utils/queued-file-state.ts` answers `unknown_path` — the file is 7.4's
 *    and is not yet committed, so the ledger has nothing on it. Expected, and reported
 *    rather than read as "undecided": the governing records are `LGR-99050e6-2`
 *    (three-state discipline), `LGR-99050e6-1` (words not colour) and `LGR-21a1b5b-2`
 *    (a queued entry is not a run).
 *
 * `api-client` is mocked **without** `importOriginal`, matching the sibling Overview
 * suites: this file must not execute the real module.
 *
 * fast-check `^3.15.0` (an existing dependency), `{ numRuns: 100 }` per the in-tree
 * convention.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as fc from 'fast-check';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

type Row = Record<string, unknown>;

interface QueuedRow {
  file_name: string;
  source_key: string;
  queued_at: string;
  status: string;
}

let rows: Row[] = [];
let queuedRows: QueuedRow[] = [];

/** The producer's envelope, nested per `LGR-2bcdd0d-1`. */
function envelope(executions: Row[], queued_files: QueuedRow[]) {
  return {
    executions,
    queued_files,
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

vi.mock('../../hooks', () => ({
  usePipelineExecutions: (
    _key: unknown,
    _page: unknown,
    _cursor: unknown,
    status?: string | null,
  ) => {
    // Status-aware, matching the server (p51 C3). The filtered call is the running
    // count's; it never carries `queued_files`, because `status=QUEUED` is a 400 and a
    // queued entry is not a run (R9.7, `LGR-21a1b5b-2`).
    if (status) return { data: envelope(rows.filter((r) => r.status === status), []) };
    return { data: envelope(rows, queuedRows) };
  },
  useArchivePipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
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

vi.mock('../../services/api-client', () => ({
  ApiError: class ApiError extends Error {},
  apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

import PipelineOverviewPage from '../PipelineOverviewPage';
import {
  computeExecutionsRefetchInterval,
  EXECUTIONS_ACTIVE_POLL_MS,
  EXECUTIONS_BACKGROUND_POLL_MS,
} from '../../hooks/use-executions';

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

function renderWith(executions: Row[], queued: QueuedRow[]) {
  rows = executions;
  queuedRows = queued;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PipelineProvider
          value={{ pipeline: mockPipeline, isLoading: false, isError: false, refetch: vi.fn() }}
        >
          <PipelineOverviewPage />
        </PipelineProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// The two published states, as LITERALS — see the docstring on why these are not
// imported from the module under test.
// ---------------------------------------------------------------------------

/** R9.3's Lost_File state, as `_split_derive_and_filter` publishes it. */
const QUEUED_STATUS_LOST = 'failed_dlq';
/** R9.3's Deferred_File state, as `_split_derive_and_filter` publishes it. */
const QUEUED_STATUS_DEFERRED = 'queued';

/** The rendered claim for each of the two states, and the reader's default. */
const LABEL_LOST = 'Failed (DLQ)';
const LABEL_QUEUED = 'Queued';

/** The page's two sentences, matched loosely so wording may change without drift. */
const HOLDING_SENTENCE = /a run is currently holding this pipeline/i;
const LOSS_REASON_SENTENCE = /waited longer than its redelivery attempts allowed/i;

/**
 * A name that appears in **neither** array — R9.5's third state, and per 7.1's live
 * read the common steady state rather than an edge case: every `QUEUED` row in the
 * live table has TTL'd out, so today every uploaded file is either a run or absent.
 */
const NEVER_SUBMITTED_FILE = 'never_submitted_0.csv';

// ---------------------------------------------------------------------------
// The input space
// ---------------------------------------------------------------------------

type Fact = 'deferred' | 'lost' | 'unrecognised';

/**
 * A status value the reader recognises as neither state. The hand-picked members are
 * the realistic drifts rather than noise: `'QUEUED'` is the **stored** DynamoDB value
 * (`_track_queued_file:681-692`), so a case-insensitive predicate would wrongly read it
 * as deferred; `'FAILED_DLQ'` is the same trap on the other literal; `''` is the shape
 * an absent attribute takes once it reaches a `string`-typed field.
 */
const unrecognisedStatusArb = fc
  .oneof(
    fc.constantFrom('QUEUED', 'Queued', 'FAILED_DLQ', 'failed', 'dlq', 'pending', ''),
    fc.stringMatching(/^[a-z]{4,12}$/),
  )
  .filter((s) => s !== QUEUED_STATUS_DEFERRED && s !== QUEUED_STATUS_LOST);

const queuedSpecArb = fc.record({
  fact: fc.constantFrom<Fact>('deferred', 'lost', 'unrecognised'),
  unrecognised: unrecognisedStatusArb,
  /** R9.4's degenerate case: the producer published no usable name. */
  blankName: fc.boolean(),
});

/**
 * Zero to four entries. Zero is not padding — it is the empty array beside a
 * non-empty `executions` list that the property names, and it is the state in which
 * the whole region must be absent rather than empty.
 */
const queuedSpecsArb = fc.array(queuedSpecArb, { minLength: 0, maxLength: 4 });

const execSpecArb = fc.record({
  /** A `SKIPPED` row — a refusal, not a deferral (R10.5, do-NOT 18). */
  refused: fc.boolean(),
  skipReason: fc.constantFrom<string | undefined>(
    'duplicate_content',
    'global_concurrency',
    'pipeline_lock_contention',
    undefined,
  ),
  /**
   * Both skip id shapes, one of which is `run-`-prefixed on purpose
   * (`LGR-21a1b5b-4`). Drawn so that nothing below can be keyed on a prefix without
   * failing here — do-NOT 4.
   */
  runIdShape: fc.constantFrom<'skipped' | 'run'>('skipped', 'run'),
});

/**
 * Zero to seven rows, so the five-row preview slice is exercised on both sides of its
 * boundary (R9.6 — `.slice(0, 5)` is untouched by 7.4 and by this task).
 */
const execSpecsArb = fc.array(execSpecArb, { minLength: 0, maxLength: 7 });

/**
 * Build the published `queued_files` entries. Names are unique by index and carry the
 * intended fact, so a name found in the DOM identifies its row without the test having
 * to walk the table's structure.
 */
function buildQueued(
  specs: ReadonlyArray<{ fact: Fact; unrecognised: string; blankName: boolean }>,
): QueuedRow[] {
  return specs.map((s, i) => ({
    file_name: s.blankName ? '' : `queued_${i}_${s.fact}.csv`,
    source_key: `mynewtesting/queued_${i}.csv`,
    queued_at: '2026-08-27T09:00:00.000000+00:00',
    status:
      s.fact === 'deferred'
        ? QUEUED_STATUS_DEFERRED
        : s.fact === 'lost'
          ? QUEUED_STATUS_LOST
          : s.unrecognised,
  }));
}

/**
 * Build the `executions` rows. Non-refused rows are `SUCCEEDED` — a member of
 * `TERMINAL_STATUSES` — so the runs half of the page is settled and the only thing
 * moving is the refusal. Every row carries `run_id`, `file_name` and `started_at`, so
 * no row is missing a field the runs table would render as an em dash for its own
 * reasons.
 */
function buildExecutions(
  specs: ReadonlyArray<{
    refused: boolean;
    skipReason: string | undefined;
    runIdShape: 'skipped' | 'run';
  }>,
): Row[] {
  return specs.map((s, i) => {
    const runId =
      s.runIdShape === 'skipped'
        ? `skipped-fingerprint${i}0abcdef`
        : `run-2026-08-26-1${i}-00-00-skip-contention`;
    return {
      run_id: runId,
      file_name: `execrow_${i}.csv`,
      status: s.refused ? 'SKIPPED' : 'SUCCEEDED',
      started_at: `2026-08-26T0${i}:00:00Z`,
      ...(s.refused ? { skip_reason: s.skipReason } : {}),
    } satisfies Row;
  });
}

/** How many claims of each kind the published array entitles the page to make. */
function expectedClaims(queued: readonly QueuedRow[]) {
  const lost = queued.filter((q) => q.status === QUEUED_STATUS_LOST);
  const deferred = queued.filter((q) => q.status === QUEUED_STATUS_DEFERRED);
  return {
    lost,
    deferred,
    /** Neither claim — the third state. Rendered under the reader's default label. */
    neither: queued.filter(
      (q) => q.status !== QUEUED_STATUS_LOST && q.status !== QUEUED_STATUS_DEFERRED,
    ),
  };
}

/**
 * The text of the table row a rendered claim sits in. Used only to pair a lost claim
 * with the file name beside it (R9.4); every other assertion is a text count over the
 * whole page and reads no structure.
 */
function claimRowTexts(label: string): string[] {
  return screen.queryAllByText(label).map((node) => {
    const row = node.closest('tr');
    expect(row).not.toBeNull();
    return row?.textContent ?? '';
  });
}

// ---------------------------------------------------------------------------
// Property 8
// ---------------------------------------------------------------------------

describe('Feature: p54-reused-and-skipped-run-honesty, Property 8: A deferred, a lost, a refused and a never-submitted file are four facts and never collapse', () => {
  beforeEach(() => {
    cleanup();
    rows = [];
    queuedRows = [];
  });

  /**
   * **Validates: Requirements 9.3, 9.4, 9.5, 10.5**
   *
   * For any `queued_files` array over the three status cases, with or without a blank
   * file name, beside any number of refusals and settled runs: the four facts stay
   * four.
   */
  it('keeps a deferral, a loss, a refusal and an absence as four distinct facts', () => {
    fc.assert(
      fc.property(queuedSpecsArb, execSpecsArb, (queuedSpecs, execSpecs) => {
        cleanup();

        const queued = buildQueued(queuedSpecs);
        const executions = buildExecutions(execSpecs);
        const { lost, deferred, neither } = expectedClaims(queued);

        // ── The frame: the two claims are mutually exclusive per entry ──────────
        // Not a render assertion — a statement about the published states, so that
        // what follows is known to be asking for "exactly one" rather than "at most
        // one of two things that could both be true".
        for (const entry of queued) {
          const isLost = entry.status === QUEUED_STATUS_LOST;
          const isDeferred = entry.status === QUEUED_STATUS_DEFERRED;
          expect(Number(isLost) + Number(isDeferred)).toBeLessThanOrEqual(1);
        }
        expect(lost.length + deferred.length + neither.length).toBe(queued.length);

        renderWith(executions, queued);

        // ── Fact 1 and fact 2: exactly one claim per entry, and the right one ───
        // Cardinality is the assertion an example test cannot make. The two counts
        // being individually right AND summing to the array length is what says no
        // entry rendered twice, none was dropped, and none rendered both claims.
        //
        // The expected counts come from the published literals, so a predicate
        // rewritten as the negation of its sibling — which would make an
        // unrecognised entry render "Failed (DLQ)" — fails here.
        expect(screen.queryAllByText(LABEL_LOST)).toHaveLength(lost.length);
        expect(screen.queryAllByText(LABEL_QUEUED)).toHaveLength(
          deferred.length + neither.length,
        );
        expect(
          screen.queryAllByText(LABEL_LOST).length + screen.queryAllByText(LABEL_QUEUED).length,
        ).toBe(queued.length);

        // ── R9.4: a lost entry carries the file name and the reason ─────────────
        // The reason is one page-level sentence, conditional on there actually being
        // a loss, so a page of deferrals makes no mention of loss at all. Asserted as
        // a count rather than presence/absence, so a second copy would fail too.
        expect(screen.queryAllByText(LOSS_REASON_SENTENCE)).toHaveLength(lost.length > 0 ? 1 : 0);

        const lostRows = claimRowTexts(LABEL_LOST);
        expect(lostRows).toHaveLength(lost.length);
        for (const entry of lost) {
          // A blank name is R9.4's degenerate case: there is no name to state, so the
          // honest render is the em-dash placeholder rather than an empty cell —
          // `LGR-99050e6-2`'s discipline one field over, absence stated rather than
          // invented. `PipelineOverviewPage.tsx:708` is that `|| '—'`.
          const expectedCell = entry.file_name || '\u2014';
          expect(lostRows.some((text) => text.includes(expectedCell))).toBe(true);
        }

        // ── R9.5: the region exists only when there is something to say ────────
        // An empty array beside a non-empty `executions` list renders nothing new.
        // Absence of the region is the third state and is not a value — an empty
        // table saying "No files queued" would be the collapse, because it would
        // report "we looked and found none" on a page that never looked.
        //
        // The holding sentence sits behind the region's question-mark button, so the
        // button carries the count and is opened before the sentence is counted.
        expect(screen.queryAllByText('Queued Files')).toHaveLength(queued.length > 0 ? 1 : 0);
        const about = screen.queryAllByRole('button', { name: 'About queued files' });
        expect(about).toHaveLength(queued.length > 0 ? 1 : 0);
        expect(screen.queryAllByText(HOLDING_SENTENCE)).toHaveLength(0);
        about.forEach((button) => fireEvent.click(button));
        expect(screen.queryAllByText(HOLDING_SENTENCE)).toHaveLength(queued.length > 0 ? 1 : 0);

        // ── Fact 3, R10.5: a refusal renders from its `executions` row ──────────
        // Every visible run row carries a View Run link, refusals included — a
        // refusal IS a run and has somewhere to go. Queued entries add none, because
        // no `run_id` is published for them and an invented one would link to a
        // `get_run_details` that 404s. `LGR-21a1b5b-2` is why the two can never share
        // a surface by accident.
        const visible = execSpecs.slice(0, 5);
        expect(screen.queryAllByRole('link', { name: 'View Run' })).toHaveLength(visible.length);
        expect(screen.queryAllByTestId('skip-reason-badge')).toHaveLength(
          visible.filter((s) => s.refused).length,
        );
        for (const [i] of visible.entries()) {
          // The refused file is named on the runs table, not in the queued region —
          // and the claim counts above already equal `queued.length`, so it provably
          // did not also become a queued entry.
          expect(screen.queryAllByText(`execrow_${i}.csv`)).toHaveLength(1);
        }

        // ── Fact 4: a file with no entry and no row renders as neither ──────────
        expect(screen.queryByText(NEVER_SUBMITTED_FILE)).toBeNull();

        // ── The cadence channel: what makes three states three ─────────────────
        // "Do we know this file is still coming back?" is answered here and nowhere
        // else. Only a deferral holds the fast cadence; a loss and an unrecognised
        // status both do not, which is the distinction the render's shared "Queued"
        // label cannot carry.
        //
        // Measured against an executions-free envelope on purpose: `SKIPPED` is not in
        // `TERMINAL_STATUSES` (`utils/formatting.ts:8`), so a refusal holds the fast
        // cadence on its own and would mask this signal. Isolating `hasQueued` is what
        // this half of the property is about; the matrix itself is `LGR-e800696-1`'s
        // and p54 do-NOT 17 / R8.14 forbid retuning it.
        const cadence = computeExecutionsRefetchInterval(
          envelope([], queued) as unknown as Parameters<
            typeof computeExecutionsRefetchInterval
          >[0],
          false,
        );
        expect(cadence).toBe(
          deferred.length > 0 ? EXECUTIONS_ACTIVE_POLL_MS : EXECUTIONS_BACKGROUND_POLL_MS,
        );

        cleanup();
      }),
      { numRuns: 100 },
    );
    // 100 full renders: 7 s alone, 72.5 s in a contended full `make test-frontend`
    // run, past the 60 s default. The same per-test budget the sibling property
    // tests carry (SchemaManagementPage.filter.prop.test.tsx: 180_000).
  }, 180_000);
});
