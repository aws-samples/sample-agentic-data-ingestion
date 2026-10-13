// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Executions page's Lost Files region — the second render surface for a
 * dead-lettered upload, and the one where the original defect was most visible.
 *
 * **Validates: Requirements 10.3**
 *
 * The sibling file `PipelineOverviewPage.lost-files.test.tsx` covers the Overview
 * region and the pure helpers are asserted directly in
 * `utils/__tests__/queued-file-state.lost-files.test.ts` and its property sibling.
 * What was still uncovered is **this** page: the derivation at
 * `ExecutionsPage.tsx:176` and the region at `:667-695` had nothing asserting a
 * `lost_files` entry reaches the screen here at all.
 *
 * ## Why this surface is not a copy of the Overview's
 *
 * The Overview shows a fixed five-row preview. This page **paginates and filters**,
 * and `ExecutionsPage.tsx:176`'s own comment names the consequence: *"Worse on this
 * surface than on the Overview: the pager can be on page 3, where `queued_files` has
 * never carried anything at all."* So the durability claim is asserted here in the
 * form only this page can express — the operator walks to page 3 and the loss is
 * still on screen — as well as in the Overview's form, a full page 1 of newer runs.
 *
 * That the fixtures carry `lost_files` on **every** page and under **every** filter
 * is not fixture convenience; it is the producer. `_lost_files`
 * (`pipelines_runs.py:1021`) is a keyed `begins_with` Query on the base table's own
 * `(pipeline_id, run_id)` schema, deliberately *not* a read of `RUNS_BY_TIME_INDEX`,
 * so its answer does not depend on paging position or on `?status=`. A fixture that
 * withheld the array from page 2 would be describing a producer that does not exist.
 * The Overview suite's mock makes the same point one filter over.
 *
 * ## The absences are the substantive half
 *
 * **A lost file is never merged into the executions table** — p54 do-NOT 3,
 * do-NOT 15, p48 O2. `lost_files` publishes **no `run_id`**
 * (`use-executions.ts:117-131`): the row has one, but `get_run_details` 404s on it
 * because a placeholder carries no state rows, so a merged row would have to
 * fabricate a link to a 404. Every run-scoped affordance on this page is gated on
 * `const runId = item.run_id` (`ExecutionsPage.tsx`'s Actions cell), which a lost
 * entry has nothing to satisfy — asserted structurally below rather than by naming
 * three labels, so a fourth affordance added later cannot slip in.
 *
 * Note the affordance is a **button** on this page, not a link: the Actions cell
 * uses `Button variant="link"` with an `onClick` that calls `navigate`, whereas the
 * Overview's Recent Runs table uses a real `Link` with an `href`. That is why the
 * row-count assertions here query `role: 'button'` where the Overview's query
 * `role: 'link'` — the same fact read through the control each page actually renders
 * (cf. `ExecutionsPage.retry.test.tsx:105`, which reads it as text for the same
 * reason).
 *
 * ## Expectations come from the producer, never from the rendered output
 *
 * `WIRE_STATUS_LOST` and `WIRE_LOST_REASON` are literals read from the two producers
 * (`_split_derive_and_filter`'s step-1 comprehension, and
 * `queue_loss_recorder.LOST_REASON` at `lambda_function.py:137-141`) rather than
 * imported from the reader under test, per `LGR-28b4d75-5`. The rendered *labels*
 * are asserted as authored text, which is the only channel available: every
 * distinction is carried in **words**, never colour alone, because `LGR-99050e6-1`
 * (**template / ratified**) records that colour alone fails WCAG 1.4.1 and
 * `LGR-1f7892b-2` re-states it for a `StatusIndicator` type specifically. Nothing
 * below reads an indicator `type` or a CSS class.
 *
 * Harness shaped after `ExecutionsPage.status-filter.test.tsx` — the richer of the
 * two idioms on this page, because it keys fixtures by `(cursor, status)` and drives
 * the Cloudscape `Select` through that library's own DOM test utils. Like every
 * other `ExecutionsPage.*.test.tsx` it mocks `../../hooks` wholesale; there is no
 * way to render this page without doing so, so these are statements about what the
 * page renders for a given response, not about the request.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Pipeline } from '../../types';

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

interface Envelope {
  executions: Array<Record<string, unknown>>;
  queued_files: QueuedRow[];
  lost_files: LostRow[];
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
}

const PAGE_ONE = '@no-cursor';
const UNFILTERED = '@no-status';

/** Responses keyed by `${cursor}|${status}`, as the status-filter suite keys them. */
let pages: Record<string, Envelope> = {};

/**
 * The `lost_files` array **every** response carries, page and filter alike — see the
 * module note: `_lost_files` is a keyed read, so this is the producer's behaviour and
 * not a shortcut.
 */
let lostRows: LostRow[] = [];

function fixtureKey(cursor: unknown, status: unknown): string {
  return `${(cursor as string) ?? PAGE_ONE}|${(status as string) ?? UNFILTERED}`;
}

vi.mock('../../hooks', () => ({
  usePipelineExecutions: (_key: unknown, _page: unknown, cursor: unknown, status: unknown) => ({
    data: pages[fixtureKey(cursor, status)],
    isLoading: false,
    isError: false,
    isFetching: false,
    error: null,
    refetch: vi.fn(),
  }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
}));

import ExecutionsPage from '../ExecutionsPage';

const mockPipeline: Pipeline = {
  pipeline_key: 'latesttesting-19ca826a',
  pipeline_name: 'latesttesting',
  status: 'active',
  source_schema: 'src',
  target_schema: 'tgt',
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-24T00:00:00Z',
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <PipelineProvider
          value={{ pipeline: mockPipeline, isLoading: false, isError: false, refetch: vi.fn() }}
        >
          <ExecutionsPage />
        </PipelineProvider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

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

/** The rendered claim. Words, never an indicator type — `LGR-1f7892b-2`. */
const LABEL_LOST = 'Failed (DLQ)';

/** The Queued Files table's own lost-arm sentence, which is NOT the wire's reason. */
const QUEUED_TABLE_LOSS_SENTENCE = /waited longer than its redelivery attempts allowed/i;

/** The fallback for a record carrying no reason, from `LOST_FILE_REASON_FALLBACK`. */
const FALLBACK_SENTENCE = /exhausted their redelivery attempts/i;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function row(id: string, file: string, startedAt: string, status = 'SUCCEEDED') {
  return { execution_id: id, run_id: id, status, started_at: startedAt, file_name: file };
}

function envelope(
  overrides: Partial<Envelope['pagination']> & {
    executions?: Envelope['executions'];
    queued_files?: QueuedRow[];
  } = {},
): Envelope {
  const { executions, queued_files, ...pagination } = overrides;
  return {
    executions: executions ?? THREE_RUNS,
    queued_files: queued_files ?? [],
    // Always present, on every page and under every filter — the keyed read.
    lost_files: lostRows,
    pagination: {
      page: 1,
      page_size: 10,
      total_count: 3,
      total_pages: 1,
      has_next: false,
      has_prev: false,
      next_cursor: null,
      partial_counts: [],
      ...pagination,
    },
  };
}

/** Three ordinary runs. Their file names never collide with a lost file's. */
const THREE_RUNS: Envelope['executions'] = [
  row('run-2026-08-26-00-00-01-aaaaaa', 'customer_0.csv', '2026-08-26T00:00:01Z'),
  row('run-2026-08-26-00-00-02-bbbbbb', 'customer_1.csv', '2026-08-26T00:00:02Z'),
  row('run-2026-08-26-00-00-03-cccccc', 'customer_2.csv', '2026-08-26T00:00:03Z'),
];

/**
 * Ten newer runs — a **full** page 1 at `page_size: 10`, which is the state that
 * used to make a recorded loss invisible.
 */
const TEN_NEWER_RUNS: Envelope['executions'] = Array.from({ length: 10 }, (_, i) =>
  row(`run-2026-08-28-1${i}-00-00-abcdef${i}`, `newer_run_${i}.csv`, `2026-08-28T1${i}:00:00Z`),
);

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

/**
 * The single-page, unfiltered case: `executions` and `queued_files` as given, plus
 * whatever `lost_files` was set to.
 */
function renderWith(
  executions: Envelope['executions'],
  queuedFiles: QueuedRow[],
  lostFiles: LostRow[],
) {
  lostRows = lostFiles;
  pages = {
    [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
      executions,
      queued_files: queuedFiles,
      total_count: executions.length,
    }),
  };
  return renderPage();
}

// ---------------------------------------------------------------------------
// Readers
// ---------------------------------------------------------------------------

/**
 * Every executions-table row, counted through the one affordance only that table
 * renders. A `Button variant="link"` with an `onClick`, so `role: 'button'` — not
 * the Overview's `role: 'link'`.
 */
function runRowButtons(): HTMLElement[] {
  return screen.queryAllByRole('button', { name: 'View Run' });
}

/** The table row a piece of text sits in. */
function rowContaining(text: string): HTMLElement {
  const node = screen.getByText(text);
  const row = node.closest('tr');
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

/** Pick a status by its wire value, the way an operator would. */
function selectStatus(value: string) {
  const select = createWrapper().findSelect('[data-testid="executions-status-filter"]');
  if (!select) throw new Error('no status filter rendered');
  select.openDropdown();
  select.selectOptionByValue(value);
}

beforeEach(() => {
  cleanup();
  pages = {};
  lostRows = [];
});

describe('ExecutionsPage — a lost file reaches the screen (p54 R10.3)', () => {
  it('names the file, states the loss, and carries the producer’s reason', () => {
    renderWith(THREE_RUNS, [], [lostFile('customer_data_9.csv')]);

    // R10.3's "SHALL name the file and the reason", both halves.
    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
    expect(screen.getByText(LABEL_LOST)).toBeInTheDocument();
    // The wire's own sentence, not one this surface wrote.
    expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();
    // And the S3 key, which is the row's identity at the producer.
    expect(screen.getByText('mynewtesting/customer_data_9.csv')).toBeInTheDocument();
  });

  it('does not borrow the Queued Files table’s sentence when nothing is queued', () => {
    // The two regions have separate copy. A page showing only a durable loss must not
    // also claim "a run is currently holding this pipeline" — no file is waiting.
    renderWith(THREE_RUNS, [], [lostFile('customer_data_9.csv')]);

    expect(screen.queryByText('Queued Files')).toBeNull();
    expect(screen.queryByText(/a run is currently holding this pipeline/i)).toBeNull();
    expect(screen.queryByText(QUEUED_TABLE_LOSS_SENTENCE)).toBeNull();
  });

  it('renders the region alongside the executions table, not instead of it', () => {
    renderWith(THREE_RUNS, [], [lostFile('customer_data_9.csv')]);

    // Both statements on one screen: the runs happened, and a file never ran.
    expect(screen.getByText('customer_0.csv')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
  });
});

describe('ExecutionsPage — a lost file is never merged into the executions table', () => {
  it('leaves the executions table at exactly its own rows', () => {
    renderWith(THREE_RUNS, [], [lostFile('customer_data_9.csv')]);

    // The executions table is the only table whose rows carry a `View Run` control, so
    // the control count IS that table's row count. Three, not four — a merged lost
    // entry would have made it four and would have needed a fabricated `run_id`, since
    // every affordance in the Actions cell is gated on `item.run_id`.
    expect(runRowButtons()).toHaveLength(3);
  });

  it('offers no View Run, retry or cancel affordance on a lost row', () => {
    // Structural rather than by name: the row must contain no interactive element at
    // all. `lost_files` publishes no `run_id`, so there is nothing to navigate to,
    // nothing to retry and nothing to cancel — and asserting the absence of three
    // specific labels would miss a fourth added later.
    renderWith(THREE_RUNS, [], [lostFile('customer_data_9.csv')]);

    const lostRow = rowContaining('customer_data_9.csv');

    expect(lostRow.querySelectorAll('button')).toHaveLength(0);
    expect(lostRow.querySelectorAll('a')).toHaveLength(0);
    expect(lostRow.querySelectorAll('[href]')).toHaveLength(0);
  });

  it('keeps the lost file out of every row the executions table owns', () => {
    renderWith(THREE_RUNS, [], [lostFile('customer_data_9.csv')]);

    // Every row carrying a `View Run` control is an executions-table row. None of them
    // names the lost file — so it did not join the table under a different column
    // either.
    const runRows = runRowButtons().map((button) => button.closest('tr'));

    expect(runRows).toHaveLength(3);
    for (const runRow of runRows) {
      expect(runRow?.textContent ?? '').not.toContain('customer_data_9.csv');
    }
  });

  it('renders the region even when the pipeline has no runs at all', () => {
    // The two are independent statements, and on this page they are separated by
    // `AsyncContent`: an empty page 1 replaces the table with "No executions yet",
    // while the Lost Files region sits outside that wrapper and is unaffected. A
    // pipeline whose only upload was lost is exactly this shape.
    renderWith([], [], [lostFile('customer_data_9.csv')]);

    expect(screen.getByText('No executions yet')).toBeInTheDocument();
    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
    expect(runRowButtons()).toHaveLength(0);
  });
});

describe('ExecutionsPage — the empty case adds nothing', () => {
  it('renders no region and no empty-state copy when nothing was lost', () => {
    renderWith(THREE_RUNS, [], []);

    // Absence of the region is the third state and is not a value. An empty table
    // saying "No files lost" would report "we looked and found none" — which happens
    // to be true here, but the region is gated on `lostFiles.length > 0`, so the
    // `empty` slot is unreachable and must stay so.
    expect(screen.queryByText('Lost Files')).toBeNull();
    expect(screen.queryByText('No files lost')).toBeNull();
    expect(screen.queryByText(LABEL_LOST)).toBeNull();
    expect(screen.queryByText(WIRE_LOST_REASON)).toBeNull();
    // Nor does the fallback sentence leak onto a page with nothing to explain.
    expect(screen.queryByText(FALLBACK_SENTENCE)).toBeNull();
  });

  it('leaves the executions table untouched when nothing was lost', () => {
    renderWith(THREE_RUNS, [], []);

    expect(runRowButtons()).toHaveLength(3);
    expect(screen.getByText('customer_0.csv')).toBeInTheDocument();
  });
});

describe('ExecutionsPage — the durability claim, which is the whole point (p54 R10.3)', () => {
  it('still shows a lost file once page 1 is full of newer runs', () => {
    // THE REGRESSION. Before `lost_files`, the loss was visible only as a
    // `queued_files` entry — page 1 of a newest-first index. Ten newer runs push it
    // off, and the placeholder's own TTL is an hour against a 30-day record, so the
    // operator saw nothing while the evidence was still on file.
    lostRows = [lostFile('customer_data_9.csv')];
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: TEN_NEWER_RUNS,
        total_count: 23,
        total_pages: 3,
        has_next: true,
        next_cursor: 'cursor-page-2',
      }),
    };
    renderPage();

    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
    expect(screen.getByText(LABEL_LOST)).toBeInTheDocument();
    expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();

    // And it did not buy that visibility by taking a slot from a real run.
    expect(runRowButtons()).toHaveLength(10);
  });

  it('still shows it on page 3, where queued_files has never carried anything', () => {
    // The form of the claim only THIS page can express, and the case
    // `ExecutionsPage.tsx:176` calls out as worse here than on the Overview: the
    // pager can be on a page whose `queued_files` is empty by construction, because
    // that array is the first page of a newest-first index and nothing else. If the
    // region were derived from `queued_files` it would be gone by page 2.
    lostRows = [lostFile('customer_data_9.csv')];
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: TEN_NEWER_RUNS,
        // Only page 1 carries queued entries — the producer's own scoping.
        queued_files: [queued('customer_data_5.csv', WIRE_STATUS_DEFERRED)],
        total_count: 23,
        total_pages: 3,
        has_next: true,
        next_cursor: 'cursor-page-2',
      }),
      [`cursor-page-2|${UNFILTERED}`]: envelope({
        executions: [row('run-page-2', 'older_a.csv', '2026-08-22T12:00:00Z')],
        page: 2,
        total_count: 23,
        total_pages: 3,
        has_next: true,
        has_prev: true,
        next_cursor: 'cursor-page-3',
      }),
      [`cursor-page-3|${UNFILTERED}`]: envelope({
        executions: [row('run-page-3', 'older_b.csv', '2026-08-20T12:00:00Z')],
        page: 3,
        total_count: 23,
        total_pages: 3,
        has_next: false,
        has_prev: true,
        next_cursor: null,
      }),
    };
    renderPage();

    fireEvent.click(screen.getByTestId('cursor-pagination-next'));
    fireEvent.click(screen.getByTestId('cursor-pagination-next'));

    // Page 3, and the Queued Files region is gone with page 1's array.
    expect(screen.getByTestId('cursor-pagination-position')).toHaveTextContent('Page 3 of 3');
    expect(screen.getByText('older_b.csv')).toBeInTheDocument();
    expect(screen.queryByText('Queued Files')).toBeNull();
    expect(screen.queryByText('customer_data_5.csv')).toBeNull();

    // The loss is still stated, in full.
    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
    expect(screen.getByText(LABEL_LOST)).toBeInTheDocument();
    expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();
  });

  it('shows the same loss on every page of a walk, not only where it started', () => {
    // Stated as invariance rather than as a single position: the region is identical
    // on pages 1, 2 and 3, which is what "page-independent" means and what a
    // `queued_files`-derived region could not do.
    lostRows = [lostFile('customer_data_9.csv')];
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: TEN_NEWER_RUNS,
        total_count: 23,
        total_pages: 3,
        has_next: true,
        next_cursor: 'cursor-page-2',
      }),
      [`cursor-page-2|${UNFILTERED}`]: envelope({
        executions: [row('run-page-2', 'older_a.csv', '2026-08-22T12:00:00Z')],
        page: 2,
        total_count: 23,
        total_pages: 3,
        has_next: true,
        has_prev: true,
        next_cursor: 'cursor-page-3',
      }),
    };
    renderPage();

    for (const step of [0, 1]) {
      if (step > 0) fireEvent.click(screen.getByTestId('cursor-pagination-next'));
      expect(screen.getByText('Lost Files')).toBeInTheDocument();
      expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
      expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();
    }
  });

  it('survives a status filter, which cannot narrow a keyed read', () => {
    // `_lost_files` does not consult `?status=`, so the region is the same under
    // every filter — including one that matches nothing, where the table renders its
    // own "no matches" copy and the loss is the only substantive thing on the page.
    // The region also sits outside `AsyncContent`, so a filtered empty result cannot
    // unmount it.
    lostRows = [lostFile('customer_data_9.csv')];
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({ executions: THREE_RUNS }),
      [`${PAGE_ONE}|SKIPPED`]: envelope({
        executions: [],
        total_count: 0,
        total_pages: 0,
      }),
    };
    renderPage();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();

    selectStatus('SKIPPED');

    expect(screen.getByTestId('executions-empty-page')).toHaveTextContent(
      'No runs match SKIPPED. Clear the filter to see all runs.',
    );
    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
    expect(screen.getByText(LABEL_LOST)).toBeInTheDocument();
    expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();
  });

  it('moves the claim from the Queued Files table to the Lost Files region as the pager advances', () => {
    // The two arrays overlap for as long as the placeholder sits on page 1, and
    // `lostFilesNotAlreadyQueued` drops the duplicate so one file makes one claim on
    // one screen. Which region carries it is cosmetic and page-dependent; that it is
    // carried at all is not. Both halves asserted in one walk, because it is the
    // transition — not either endpoint — that the page-scoped derivation got wrong.
    lostRows = [lostFile('customer_data_9.csv')];
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: TEN_NEWER_RUNS,
        queued_files: [queued('customer_data_9.csv', WIRE_STATUS_LOST)],
        total_count: 23,
        total_pages: 3,
        has_next: true,
        next_cursor: 'cursor-page-2',
      }),
      [`cursor-page-2|${UNFILTERED}`]: envelope({
        executions: [row('run-page-2', 'older_a.csv', '2026-08-22T12:00:00Z')],
        page: 2,
        total_count: 23,
        total_pages: 3,
        has_next: false,
        has_prev: true,
        next_cursor: null,
      }),
    };
    renderPage();

    // Page 1: once, in the Queued Files table, with that table's sentence.
    expect(screen.getAllByText('customer_data_9.csv')).toHaveLength(1);
    expect(screen.getAllByText(LABEL_LOST)).toHaveLength(1);
    expect(screen.getByText('Queued Files')).toBeInTheDocument();
    expect(screen.queryByText('Lost Files')).toBeNull();
    expect(screen.getByText(QUEUED_TABLE_LOSS_SENTENCE)).toBeInTheDocument();
    expect(screen.queryByText(WIRE_LOST_REASON)).toBeNull();

    fireEvent.click(screen.getByTestId('cursor-pagination-next'));

    // Page 2: still once, now in the Lost Files region, with the wire's own reason.
    expect(screen.getAllByText('customer_data_9.csv')).toHaveLength(1);
    expect(screen.getAllByText(LABEL_LOST)).toHaveLength(1);
    expect(screen.queryByText('Queued Files')).toBeNull();
    expect(screen.getByText('Lost Files')).toBeInTheDocument();
    expect(screen.getByText(WIRE_LOST_REASON)).toBeInTheDocument();
    expect(screen.queryByText(QUEUED_TABLE_LOSS_SENTENCE)).toBeNull();
  });
});
