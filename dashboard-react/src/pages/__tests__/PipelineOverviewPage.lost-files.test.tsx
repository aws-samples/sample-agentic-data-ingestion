// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Overview page's Lost Files region — the durable statement of a dead-lettered
 * upload.
 *
 * **Validates: Requirements 10.3**
 *
 * The region, the `lostFiles` derivation and both helpers behind it shipped with **no
 * tests of any kind**. A grep for `lost_files`, `lostFiles` or `Lost Files` across
 * `dashboard-react/src` matched only the four source files, and codegraph reports "no
 * covering tests found" for `lostFilesNotAlreadyQueued` and `distinctLostReasons`. So
 * before this file nothing asserted that a `lost_files` entry reaches the screen at all.
 *
 * Shaped after `PipelineOverviewPage.queued-files.test.tsx`, which covers the sibling
 * region. The pure halves are asserted directly in
 * `utils/__tests__/queued-file-state.lost-files.test.ts` and its property sibling; what
 * lives here is what only a render can show.
 *
 * ## The durability claim is the reason the region exists
 *
 * p54 **R10.3** — *"WHEN a file is recorded as lost, THE record SHALL name the file and
 * the reason, and SHALL be readable for longer than the window in which the loss
 * occurred."*
 *
 * `queued_files` cannot carry that. It is page 1 of a newest-first index and a loss row's
 * `started_at` is the instant the loss was *recorded*, so on an active pipeline the row
 * enters at the top and is pushed off page 1 within about an hour — while the record
 * itself lives 30 days. The condition that creates a lost file, a busy pipeline holding
 * an approval lock, is the same condition that then buries the evidence.
 *
 * `still shows a lost file once page 1 is full of newer runs` below is that defect as a
 * test. It is the one assertion here whose absence would let the bug come back silently:
 * every other test in this file would still pass against the pre-fix page, because the
 * pre-fix page rendered a loss correctly for the first hour.
 *
 * ## The absences are the substantive half
 *
 * **A lost file is never merged into the runs table** — do-NOT 3, do-NOT 15, p48 O2.
 * `latestExecs` is `execData?.executions ?? []` (`PipelineOverviewPage.tsx:107`) reaching
 * the table at `:500` as `latestExecs.slice(0, 5)`, and `lost_files` is read into a
 * separate `lostFiles` binding that the table never sees. `lost_files` publishes **no
 * `run_id` at all** — the row has one, but `get_run_details` 404s on it because a
 * placeholder carries no state rows — so a merged row would have to fabricate a link that
 * leads to a 404. This file asserts the runs table does not grow and that a lost row
 * carries no interactive affordance whatsoever.
 *
 * **The `'View Run'` name is fenced.** Two suites read a row's `run_id` off that link's
 * `href` and nowhere else (`recent-runs-order.test.tsx:305-307`,
 * `running-count.test.tsx:414-417`); `LGR-e5e9edd-5` (**template / ratified**) kept the
 * affordance for exactly that reason, and do-NOT 5 fences it. A lost row carrying a
 * `View Run` link would join those name-scoped queries and break their length assertions.
 *
 * ## Expectations come from the producer, never from the rendered output
 *
 * `WIRE_STATUS_LOST` and `WIRE_LOST_REASON` are literals from the two producers
 * (`_split_derive_and_filter`, `queue_loss_recorder.LOST_REASON`) rather than imports
 * from the reader, per `LGR-28b4d75-5`. The rendered *labels* are asserted as authored
 * text, which is the only channel available: every distinction is carried in **words**,
 * never colour alone, because `LGR-99050e6-1` (**template / ratified**) records that
 * colour alone fails WCAG 1.4.1 and `LGR-1f7892b-2` re-states it for a `StatusIndicator`
 * type specifically. Nothing below reads an indicator `type` or a CSS class.
 *
 * `api-client` is mocked **without** `importOriginal`, following the sibling Overview
 * suites: `ApiError` and `apiClient` are the two names the page's subtree imports from it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

type Row = Record<string, unknown>;

interface QueuedRow {
  file_name: string;
  source_key: string;
  queued_at: string;
  status: string;
}

interface LostRow {
  file_name: string;
  source_key: string;
  lost_at: string;
  lost_reason: string;
}

let rows: Row[] = [];
let queuedRows: QueuedRow[] = [];
let lostRows: LostRow[] = [];

/**
 * The producer's envelope, nested per `LGR-2bcdd0d-1`, now with the fourth top-level
 * array. `lost_files` is declared **required** on `PaginatedExecutions` because the
 * producer emits it on both response arms, so a fixture that omitted it would be
 * describing a response no handler returns.
 */
function envelope(executions: Row[], queued_files: QueuedRow[], lost_files: LostRow[]) {
  return {
    executions,
    queued_files,
    lost_files,
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
    // count's; it carries no `queued_files`, because `status=QUEUED` is a 400 and a
    // queued entry is not a run (R9.7, `LGR-21a1b5b-2`). It DOES carry `lost_files`,
    // because that read is keyed rather than page-scoped and is therefore identical
    // under every filter — which is the property the region depends on.
    if (status) {
      return { data: envelope(rows.filter((r) => r.status === status), [], lostRows) };
    }
    return { data: envelope(rows, queuedRows, lostRows) };
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

// ---------------------------------------------------------------------------
// The producer's literals
// ---------------------------------------------------------------------------

/** `_split_derive_and_filter`'s published Lost_File status. */
const WIRE_STATUS_LOST = 'failed_dlq';
/** Its published Deferred_File status. */
const WIRE_STATUS_DEFERRED = 'queued';

/** `queue_loss_recorder.LOST_REASON`, verbatim — the one reason constant today. */
const WIRE_LOST_REASON =
  "The upload's SQS message exhausted its redelivery budget before the " +
  'pipeline lock became free, so SQS moved it to the dead-letter queue and no ' +
  'run was started for this file.';

/** A second reason no producer emits yet. */
const SECOND_REASON = 'The pipeline was archived before this upload could be processed.';

/** The rendered claim. Words, never an indicator type — `LGR-1f7892b-2`. */
const LABEL_LOST = 'Failed (DLQ)';

/** The Queued Files table's own lost-arm sentence, which is NOT the wire's reason. */
const QUEUED_TABLE_LOSS_SENTENCE = /waited longer than its redelivery attempts allowed/i;

/**
 * Ten newer runs — a **full** page 1 at `page_size: 10`, which is the state that used to
 * make a recorded loss invisible.
 */
const TEN_NEWER_RUNS: Row[] = Array.from({ length: 10 }, (_, i) => ({
  run_id: `run-2026-08-28-1${i}-00-00-abcdef${i}`,
  file_name: `newer_run_${i}.csv`,
  status: 'SUCCEEDED',
  started_at: `2026-08-28T1${i}:00:00Z`,
}));

/** Nine runs — more than the five the preview shows, so a slice change shows up. */
const NINE_RUNS: Row[] = Array.from({ length: 9 }, (_, i) => ({
  run_id: `run-2026-08-26-00-00-0${i}-abcdef${i}`,
  file_name: `customer_${i}.csv`,
  status: 'SUCCEEDED',
  started_at: `2026-08-26T0${i}:00:00Z`,
}));

function lostFile(file_name: string, lost_reason = WIRE_LOST_REASON): LostRow {
  return {
    file_name,
    source_key: `mynewtesting/${file_name}`,
    lost_at: '2026-08-27T09:00:00.000000+00:00',
    lost_reason,
  };
}

function queued(file_name: string, status: string): QueuedRow {
  return {
    file_name,
    source_key: `mynewtesting/${file_name}`,
    queued_at: '2026-08-27T09:00:00.000000+00:00',
    status,
  };
}

function renderWith(executions: Row[], queuedFiles: QueuedRow[], lostFiles: LostRow[]) {
  rows = executions;
  queuedRows = queuedFiles;
  lostRows = lostFiles;
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

/** The table row a piece of text sits in. */
function rowContaining(text: string): HTMLElement {
  const node = screen.getByText(text);
  const row = node.closest('tr');
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

beforeEach(() => {
  cleanup();
  rows = [];
  queuedRows = [];
  lostRows = [];
});

describe('PipelineOverviewPage — a lost file reaches the screen (p54 R10.3)', () => {
  it('names the file, states the loss, and carries the producer’s reason', () => {
    renderWith(NINE_RUNS, [], [lostFile('customer_data_9.csv')]);

    // R10.3's "SHALL name the file and the reason", both halves.
    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
    expect(screen.getByText(LABEL_LOST)).toBeInTheDocument();
    // The wire's own sentence, not one this surface wrote.
    expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();
  });

  it('does not borrow the Queued Files table’s sentence when nothing is queued', () => {
    // The two regions have separate copy. A page showing only a durable loss must not
    // also claim "a run is currently holding this pipeline" — no file is waiting.
    renderWith(NINE_RUNS, [], [lostFile('customer_data_9.csv')]);

    expect(screen.queryByText('Queued Files')).toBeNull();
    expect(screen.queryByText(/a run is currently holding this pipeline/i)).toBeNull();
    expect(screen.queryByText(QUEUED_TABLE_LOSS_SENTENCE)).toBeNull();
  });
});

describe('PipelineOverviewPage — a lost file is never merged into the runs table', () => {
  it('leaves Recent Runs at exactly five rows', () => {
    renderWith(NINE_RUNS, [], [lostFile('customer_data_9.csv')]);

    // The runs table is the only table whose rows carry a `View Run` link, so the link
    // count IS the runs table's row count. Five, not six — a merged lost entry would
    // have made it six and would have needed a fabricated `run_id` to link with.
    expect(screen.getAllByRole('link', { name: 'View Run' })).toHaveLength(5);
  });

  it('offers no View Run, retry or cancel affordance on a lost row', () => {
    // Structural rather than by name: the row must contain no interactive element at
    // all. `lost_files` publishes no `run_id`, so there is nothing to navigate to,
    // nothing to retry and nothing to cancel — and asserting the absence of three
    // specific labels would miss a fourth added later.
    renderWith(NINE_RUNS, [], [lostFile('customer_data_9.csv')]);

    const row = rowContaining('customer_data_9.csv');

    expect(row.querySelectorAll('a')).toHaveLength(0);
    expect(row.querySelectorAll('button')).toHaveLength(0);
    expect(row.querySelectorAll('[href]')).toHaveLength(0);
  });

  it('keeps the lost file out of every row the runs table owns', () => {
    renderWith(NINE_RUNS, [], [lostFile('customer_data_9.csv')]);

    // Every row carrying a `View Run` link is a runs-table row. None of them names the
    // lost file — so it did not join the table under a different column either.
    const runRows = screen
      .getAllByRole('link', { name: 'View Run' })
      .map((link) => link.closest('tr'));

    expect(runRows).toHaveLength(5);
    for (const runRow of runRows) {
      expect(runRow?.textContent ?? '').not.toContain('customer_data_9.csv');
    }
  });

  it('renders the lost region even when the pipeline has no runs at all', () => {
    // The two are independent statements. A pipeline whose only upload was lost has an
    // empty runs table and a populated lost region.
    renderWith([], [], [lostFile('customer_data_9.csv')]);

    expect(screen.getByText('No runs yet')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
    expect(screen.queryAllByRole('link', { name: 'View Run' })).toHaveLength(0);
  });
});

describe('PipelineOverviewPage — the empty case adds nothing', () => {
  it('renders no region and no empty-state copy when nothing was lost', () => {
    renderWith(NINE_RUNS, [], []);

    // Absence of the region is the third state and is not a value. An empty table
    // saying "No files lost" would report "we looked and found none" — which happens
    // to be true here, but the region is gated on `lostFiles.length > 0`, so the
    // `empty` slot is unreachable and must stay so.
    expect(screen.queryByText('Lost Files')).toBeNull();
    expect(screen.queryByText('No files lost')).toBeNull();
    expect(screen.queryByText(LABEL_LOST)).toBeNull();
    expect(screen.queryByText(WIRE_LOST_REASON)).toBeNull();
    // Nor does the fallback sentence leak onto a page with nothing to explain.
    expect(screen.queryByText(/exhausted their redelivery attempts/i)).toBeNull();
  });

  it('leaves the runs table untouched when nothing was lost', () => {
    renderWith(NINE_RUNS, [], []);

    expect(screen.getAllByRole('link', { name: 'View Run' })).toHaveLength(5);
  });
});

describe('PipelineOverviewPage — the durability claim, which is the whole point (p54 R10.3)', () => {
  it('still shows a lost file once page 1 is full of newer runs', () => {
    // THE REGRESSION. Before `lost_files`, the loss was visible only as a `queued_files`
    // entry — page 1 of a newest-first index. Ten newer runs push it off, and the
    // placeholder's own TTL is an hour against a 30-day record, so the operator saw
    // nothing while the evidence was still on file. `lost_files` comes from a keyed read
    // and is therefore identical on every page.
    renderWith(TEN_NEWER_RUNS, [], [lostFile('customer_data_9.csv')]);

    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
    expect(screen.getByText(LABEL_LOST)).toBeInTheDocument();
    expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();

    // And it did not buy that visibility by taking a slot from a real run.
    expect(screen.getAllByRole('link', { name: 'View Run' })).toHaveLength(5);
  });

  it('shows it with no queued entry present at all', () => {
    // The steady state after the placeholder's 1 h TTL expires: `queued_files` is empty
    // and `lost_files` is the only remaining statement of the loss. If the region were
    // derived from `queued_files` this would render nothing.
    renderWith(TEN_NEWER_RUNS, [], [lostFile('customer_data_9.csv')]);

    expect(screen.queryByText('Queued Files')).toBeNull();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
  });
});

describe('PipelineOverviewPage — which region shows a loss is page-dependent, that it is shown is not', () => {
  it('lists a just-recorded loss once, in the Queued Files table, while it is still on page 1', () => {
    // The two arrays overlap for as long as the placeholder sits on page 1.
    // `lostFilesNotAlreadyQueued` removes the duplicate rather than the queued table's
    // lost arm, because that arm is ratified and asserted on both surfaces — taking
    // rows away from it would make it a reader with no producer again, the exact defect
    // p54 R9.3 closed. Its docstring records that rejected alternative.
    renderWith(
      NINE_RUNS,
      [queued('customer_data_9.csv', WIRE_STATUS_LOST)],
      [lostFile('customer_data_9.csv')],
    );

    // Once, not twice — one file, one claim on one screen.
    expect(screen.getAllByText(LABEL_LOST)).toHaveLength(1);
    expect(screen.getAllByText('customer_data_9.csv')).toHaveLength(1);

    // In the Queued Files region; the Lost Files region has nothing left to show.
    expect(screen.getByText('Queued Files')).toBeInTheDocument();
    expect(screen.queryByText('Lost Files')).toBeNull();

    // So the sentence an operator reads is the queued table's, and the wire's own
    // reason is not on the page. That asymmetry is the documented cost of the split.
    expect(screen.getByText(QUEUED_TABLE_LOSS_SENTENCE)).toBeInTheDocument();
    expect(screen.queryByText(WIRE_LOST_REASON)).toBeNull();
  });

  it('shows both regions when a queued loss and a durable loss are different files', () => {
    renderWith(
      NINE_RUNS,
      [queued('customer_data_5.csv', WIRE_STATUS_LOST)],
      [lostFile('customer_data_9.csv')],
    );

    expect(screen.getByText('Queued Files')).toBeInTheDocument();
    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    // One claim per file, two files.
    expect(screen.getAllByText(LABEL_LOST)).toHaveLength(2);
    expect(screen.getByText('customer_data_5.csv')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
  });

  it('keeps the durable loss visible when the queued entry for it is merely DEFERRED', () => {
    // The queued row exists but reads "Queued", so nothing on the page has yet said
    // this file is lost. Both rows render, and the loss claim comes from the Lost Files
    // region — the case where the two arrays disagree and `lost_files` is load-bearing.
    renderWith(
      NINE_RUNS,
      [queued('customer_data_9.csv', WIRE_STATUS_DEFERRED)],
      [lostFile('customer_data_9.csv')],
    );

    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    expect(screen.getByText('Queued')).toBeInTheDocument();
    expect(screen.getAllByText(LABEL_LOST)).toHaveLength(1);
    expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();
  });
});

describe('PipelineOverviewPage — the reason is one sentence per distinct reason, below the table', () => {
  it('collapses many files sharing one reason into a single sentence', () => {
    // The steady state today: `queue_loss_recorder` writes one constant, so three lost
    // files yield one sentence rather than three copies of it.
    renderWith(
      NINE_RUNS,
      [],
      [lostFile('a.csv'), lostFile('b.csv'), lostFile('c.csv')],
    );

    expect(screen.getAllByText(WIRE_LOST_REASON)).toHaveLength(1);
    expect(screen.getAllByText(LABEL_LOST)).toHaveLength(3);
  });

  it('renders one sentence per distinct reason, and neither sentence sits in a row', () => {
    // **Pinning a judgement call rather than endorsing it.** With two reasons an
    // operator sees two sentences below the table and no way to tell which file carries
    // which — the reasons are page-level, not per-row. That is invisible today because
    // one producer constant exists, and it is asserted here so that changing it is a
    // deliberate act rather than an accident.
    renderWith(NINE_RUNS, [], [lostFile('a.csv', SECOND_REASON), lostFile('b.csv')]);

    expect(screen.getAllByText(SECOND_REASON)).toHaveLength(1);
    expect(screen.getAllByText(WIRE_LOST_REASON)).toHaveLength(1);

    // Below the table, not inside it — so neither sentence is attached to a file.
    for (const sentence of [SECOND_REASON, WIRE_LOST_REASON]) {
      expect(screen.getByText(sentence).closest('tr')).toBeNull();
    }

    // Stated the other way round, because it is the operator-visible consequence:
    // neither file's row names its own reason.
    for (const file of ['a.csv', 'b.csv']) {
      const row = rowContaining(file);
      expect(row.textContent ?? '').not.toContain(SECOND_REASON);
      expect(row.textContent ?? '').not.toContain(WIRE_LOST_REASON);
    }
  });

  it('falls back to a generic sentence when the producer sent no reason', () => {
    // A record written before `lost_reason` existed. The region never renders blank and
    // never invents a specific account — the fallback claims nothing about receive
    // counts or timings this surface cannot know.
    renderWith(NINE_RUNS, [], [lostFile('a.csv', '')]);

    expect(screen.getByText(LABEL_LOST)).toBeInTheDocument();
    expect(screen.getByText(/exhausted their redelivery attempts/i)).toBeInTheDocument();
    expect(screen.queryByText(WIRE_LOST_REASON)).toBeNull();
  });

  it('never shows the generic sentence beside a reason the producer did send', () => {
    renderWith(NINE_RUNS, [], [lostFile('a.csv', ''), lostFile('b.csv')]);

    expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();
    expect(screen.queryByText(/exhausted their redelivery attempts/i)).toBeNull();
  });
});
