// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The run-history status filter — p48 task 5.5 (C5, R4.1, R4.3, R4.4).
 *
 * WHAT THIS FILE CAN AND CANNOT PROVE
 * -----------------------------------
 * Like the other seven `ExecutionsPage.*.test.tsx` files it mocks `../../hooks`
 * wholesale — task 1.2 established that there is no way to render this page
 * without doing so, and that consequently **no page test has ever executed the
 * real request**. So everything here is a statement about the page's contract
 * *with the hook* and about what it renders: which `(page, cursor, status)` triple
 * it asks for, and what words appear for each outcome. That the triple becomes
 * `?status=SKIPPED` on the wire, and that an unfiltered read sends no `status`
 * parameter at all, is asserted one layer down in
 * `hooks/__tests__/use-executions.status-filter.test.ts` against the real hook and
 * a mocked api-client. Neither file can substitute for the other and the split is
 * deliberate rather than incidental.
 *
 * THE CORRECTNESS REQUIREMENT THIS FILE EXISTS FOR
 * ------------------------------------------------
 * `cursorStack`'s docstring in `ExecutionsPage.tsx` records the hazard task 5.1
 * left for 5.5: a cursor stays *redeemable* across a filter change, because
 * `run_cursor.decode_cursor` binds it to the pipeline and not to the filter. So a
 * retained cursor produces a **successful request for the wrong rows** — page 2 of
 * `status=SKIPPED` resuming from wherever the unfiltered walk had reached. Wrong
 * rows, no error, no complaint from any type. `resets the cursor stack …` below is
 * the whole guard for that, and it is the one test in this file whose deletion
 * would be silently expensive.
 *
 * THREE EMPTIES, THREE READINGS
 * -----------------------------
 * `ExecutionsPage` can render three different nothings and conflating any two is a
 * defect. 5.1 built the first two; this task adds the third and had to keep it out
 * of the first:
 *   1. the pipeline has no runs at all      → `AsyncContent`'s "No executions yet"
 *   2. a later page is empty, unfiltered    → the table's "No runs on this page…"
 *   3. the filter matched nothing           → the table's "No runs match …"
 * R4.1 asks for (3) in those words. `is not the empty-history copy …` asserts the
 * separation in both directions, because the mechanism that keeps them apart is
 * one clause (`!statusFilter`) in `AsyncContent`'s `isEmpty`, and dropping it
 * silently turns (3) into (1) — a claim about the *pipeline* made on the strength
 * of a filter.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Pipeline } from '../../types';

interface Envelope {
  executions: Array<Record<string, unknown>>;
  queued_files: Array<Record<string, unknown>>;
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

/**
 * Responses keyed by `${cursor}|${status}`, because a filtered read and an
 * unfiltered read of the same page position are different responses — which is
 * also why `status` is part of the query key. Keying the fixture the same way is
 * what lets the cursor-reset test observe a *wrong* page rather than merely a
 * different argument list.
 */
let pages: Record<string, Envelope> = {};
let isFetching = false;

/** Every `(pipelineKey, page, cursor, status)` the page asked the hook for. */
const hookCalls: Array<{ key: unknown; page: unknown; cursor: unknown; status: unknown }> = [];

function fixtureKey(cursor: unknown, status: unknown): string {
  return `${(cursor as string) ?? PAGE_ONE}|${(status as string) ?? UNFILTERED}`;
}

vi.mock('../../hooks', () => ({
  usePipelineExecutions: (key: unknown, page: unknown, cursor: unknown, status: unknown) => {
    hookCalls.push({ key, page, cursor, status });
    return {
      data: pages[fixtureKey(cursor, status)],
      isLoading: false,
      isError: false,
      isFetching,
      error: null,
      refetch: vi.fn(),
    };
  },
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

function row(id: string, file: string, startedAt: string, status = 'SUCCEEDED') {
  return { execution_id: id, run_id: id, status, started_at: startedAt, file_name: file };
}

function envelope(
  overrides: Partial<Envelope['pagination']> & { executions?: Envelope['executions'] } = {},
): Envelope {
  const { executions, ...pagination } = overrides;
  return {
    executions: executions ?? [row('run-1', 'customer_data_1.csv', '2026-08-24T13:04:01Z')],
    queued_files: [],
    pagination: {
      page: 1,
      page_size: 10,
      total_count: 1,
      total_pages: 1,
      has_next: false,
      has_prev: false,
      next_cursor: null,
      partial_counts: [],
      ...pagination,
    },
  };
}

/** The filter control, through Cloudscape's own DOM test utils. */
function statusFilter() {
  const select = createWrapper().findSelect('[data-testid="executions-status-filter"]');
  if (!select) throw new Error('no status filter rendered');
  return select;
}

/** Pick an option by its wire value, the way an operator would. */
function selectStatus(value: string) {
  const select = statusFilter();
  select.openDropdown();
  select.selectOptionByValue(value);
}

/** The labels the dropdown offers, in order. */
function offeredLabels(): string[] {
  const select = statusFilter();
  select.openDropdown();
  const labels = select
    .findDropdown()
    .findOptions()
    .map((o) => (o.getElement().textContent ?? '').trim());
  select.closeDropdown();
  return labels;
}

/** The last triple the page asked for. */
function lastCall() {
  return hookCalls[hookCalls.length - 1];
}

beforeEach(() => {
  cleanup();
  hookCalls.length = 0;
  isFetching = false;
  pages = {};
});

describe('p48 task 5.5 — the status control', () => {
  /**
   * **Validates: Requirements 4.1, 4.4**
   *
   * The control exists, it is labelled, and it offers exactly the set task 4.3
   * determined — written out here from `_ACCEPTED_STATUS_FILTERS` and the two
   * documented omissions rather than read back off the component, which would make
   * the component its own oracle.
   *
   * `SKIPPED` first (R4.4): it is the case that opened this spec. `IN_PROGRESS` is
   * absent by decision — the server accepts it for old-row tolerance and 4.3 said
   * 5.5 "may leave it out of the dropdown without any server change"; it has no
   * writer, so offering it advertises a question whose answer is empty for every
   * pipeline written by a current producer. `QUEUED` is absent because it is a
   * **400** — the split runs before the filter, so `executions` provably cannot
   * contain a match — and a control must not offer a value that errors.
   */
  it('renders a labelled control offering the six filterable statuses', () => {
    pages = { [`${PAGE_ONE}|${UNFILTERED}`]: envelope() };
    renderPage();

    // The accessible name carries BOTH halves, which is the accessibility
    // property in full: the `ariaLabel` says what the control is for, and the
    // selected option's label says what it is currently doing. So "unfiltered"
    // and "filtered to SKIPPED" are distinguishable without seeing the screen —
    // nothing is left to colour or position (`LGR-99050e6-1`).
    expect(statusFilter().findTrigger().getElement()).toHaveAccessibleName(
      'Filter runs by status All statuses',
    );
    expect(offeredLabels()).toEqual([
      'All statuses',
      'SKIPPED',
      'SUCCEEDED',
      'FAILED',
      'RUNNING',
      'Awaiting Approval',
      'CANCELLED',
    ]);
  });

  /**
   * **Validates: Requirements 4.1, 4.4**
   *
   * Choosing `SKIPPED` asks the hook for `status: 'SKIPPED'` and the matches
   * render. This is the page → hook half; the `?status=SKIPPED` half is in the
   * hook's own test file (see this file's header).
   *
   * The fixture is the operator's actual report: `customer_data_3.csv`, skipped as
   * duplicate content, the row whose position opened the spec.
   */
  it('sends the selected status and renders the matches', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: [
          row('run-1', 'customer_data_1.csv', '2026-08-24T13:04:01Z', 'RUNNING'),
          row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED'),
        ],
        total_count: 2,
      }),
      [`${PAGE_ONE}|SKIPPED`]: envelope({
        executions: [
          row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED'),
        ],
        total_count: 1,
      }),
    };
    renderPage();
    expect(lastCall()).toMatchObject({ status: undefined });

    selectStatus('SKIPPED');

    expect(lastCall()).toMatchObject({ status: 'SKIPPED' });
    expect(screen.getByText('customer_data_3.csv')).toBeInTheDocument();
    expect(screen.queryByText('customer_data_1.csv')).toBeNull();
    // The filter's own state, announced as well as displayed — the second half of
    // the accessible name above, now carrying the active status.
    expect(statusFilter().findTrigger().getElement()).toHaveAccessibleName(
      'Filter runs by status SKIPPED',
    );
  });

  /**
   * **Validates: Requirements 4.1, 4.2**
   *
   * `WAITING_APPROVAL` is selectable, and that is not a formality: **no writer
   * stores that string**. It is derived on the read path from
   * `pending_approval_id`, and it is reachable only because task 4.3 placed the
   * filter *after* that derivation. A filter on the stored status would return
   * nothing for the one status an operator most wants to find (R4.2), so a test
   * that only ever exercised `SKIPPED` would pass over that mistake.
   */
  it('offers WAITING_APPROVAL, the status no writer stores', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope(),
      [`${PAGE_ONE}|WAITING_APPROVAL`]: envelope({
        executions: [
          {
            ...row('run-waiting', 'customer_data_9.csv', '2026-08-24T14:00:00Z', 'WAITING_APPROVAL'),
            pending_approval_id: 'appr-1',
          },
        ],
      }),
    };
    renderPage();

    selectStatus('WAITING_APPROVAL');

    expect(lastCall()).toMatchObject({ status: 'WAITING_APPROVAL' });
    expect(screen.getByText('customer_data_9.csv')).toBeInTheDocument();
  });

  /**
   * **Validates: Requirements 4.1**
   *
   * Clearing returns to the unfiltered list — `undefined` on the wire, not the
   * empty string, so the request omits the parameter rather than sending
   * `?status=`. The rows come back too, which is what makes this a round trip
   * rather than a state reset.
   */
  it('returns to the unfiltered list when the filter is cleared', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: [
          row('run-1', 'customer_data_1.csv', '2026-08-24T13:04:01Z', 'RUNNING'),
          row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED'),
        ],
        total_count: 2,
      }),
      [`${PAGE_ONE}|SKIPPED`]: envelope({
        executions: [
          row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED'),
        ],
      }),
    };
    renderPage();
    selectStatus('SKIPPED');
    expect(screen.queryByText('customer_data_1.csv')).toBeNull();

    selectStatus('');

    expect(lastCall()).toMatchObject({ status: undefined });
    expect(screen.getByText('customer_data_1.csv')).toBeInTheDocument();
    expect(screen.getByText('customer_data_3.csv')).toBeInTheDocument();
  });
});

describe('p48 task 5.5 — changing the filter resets the cursor position', () => {
  /**
   * **Validates: Requirements 4.1, 4.3**
   *
   * THE MUTATION TARGET, and the reason this file exists. Task 5.1's
   * `cursorStack` docstring states the hazard in full: a cursor remains
   * *redeemable* after the filter changes — it is a key position and
   * `run_cursor.decode_cursor` binds it to the pipeline, not to the filter — so
   * retaining it yields a **successful request for the wrong rows**: page 2 of
   * `status=SKIPPED` resuming from wherever the unfiltered read had got to.
   *
   * Asserted in two independent ways, because the argument-level assertion alone
   * would be satisfiable by an implementation that reset the label and kept the
   * cursor:
   *  - the triple asked for is `(page 1, no cursor)`, and
   *  - the rows rendered are the filtered *first* page, not the row the
   *    unfiltered page-2 cursor addresses.
   *
   * The fixture makes the failure visible rather than merely detectable: a
   * retained cursor resolves to `${cursor-page-2}|SKIPPED`, which is seeded with a
   * `SUCCEEDED` row from the middle of the unfiltered history — exactly the
   * plausible-but-wrong page the hazard produces. Deleting
   * `setCursorStack([])` from `changeStatusFilter` turns this red on both
   * assertions and leaves the rest of the suite green.
   */
  it('resets the cursor stack, so a filtered read starts at page 1', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: [row('run-newest', 'page1.csv', '2026-08-24T13:04:01Z')],
        total_count: 23,
        total_pages: 3,
        has_next: true,
        next_cursor: 'cursor-page-2',
      }),
      [`cursor-page-2|${UNFILTERED}`]: envelope({
        executions: [row('run-older', 'page2.csv', '2026-08-22T12:43:57Z')],
        page: 2,
        total_count: 23,
        total_pages: 3,
        has_next: true,
        has_prev: true,
        next_cursor: 'cursor-page-3',
      }),
      // What a retained cursor would resolve to: a successful response for the
      // wrong rows. Never reached by a correct implementation.
      'cursor-page-2|SKIPPED': envelope({
        executions: [row('run-mid-history', 'wrong-rows.csv', '2026-08-21T09:00:00Z')],
        page: 2,
        has_prev: true,
      }),
      [`${PAGE_ONE}|SKIPPED`]: envelope({
        executions: [
          row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED'),
        ],
        total_count: 1,
      }),
    };
    renderPage();

    fireEvent.click(screen.getByTestId('cursor-pagination-next'));
    expect(screen.getByText('page2.csv')).toBeInTheDocument();
    expect(lastCall()).toMatchObject({ page: 2, cursor: 'cursor-page-2' });

    selectStatus('SKIPPED');

    expect(lastCall()).toMatchObject({ page: 1, cursor: undefined, status: 'SKIPPED' });
    expect(screen.getByText('customer_data_3.csv')).toBeInTheDocument();
    expect(screen.queryByText('wrong-rows.csv')).toBeNull();
  });

  /**
   * **Validates: Requirements 4.1**
   *
   * The same reset on the way back out. Clearing the filter from a filtered page 2
   * must not redeem the *filtered* cursor against the unfiltered walk, which is
   * the same hazard with the two reads swapped.
   */
  it('resets the cursor stack when the filter is cleared as well', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: [row('run-newest', 'page1.csv', '2026-08-24T13:04:01Z')],
        total_count: 23,
        total_pages: 3,
        has_next: true,
        next_cursor: 'cursor-unfiltered-2',
      }),
      [`${PAGE_ONE}|SKIPPED`]: envelope({
        executions: [row('skipped-a', 'skip-a.csv', '2026-08-24T13:04:04Z', 'SKIPPED')],
        total_count: 4,
        total_pages: 1,
        has_next: true,
        next_cursor: 'cursor-skipped-2',
      }),
      'cursor-skipped-2|SKIPPED': envelope({
        executions: [row('skipped-b', 'skip-b.csv', '2026-08-20T20:51:00Z', 'SKIPPED')],
        page: 2,
        has_prev: true,
      }),
      // The wrong-rows fixture for this direction.
      [`cursor-skipped-2|${UNFILTERED}`]: envelope({
        executions: [row('run-wrong', 'wrong-rows.csv', '2026-08-21T09:00:00Z')],
        page: 2,
        has_prev: true,
      }),
    };
    renderPage();
    selectStatus('SKIPPED');
    fireEvent.click(screen.getByTestId('cursor-pagination-next'));
    expect(screen.getByText('skip-b.csv')).toBeInTheDocument();

    selectStatus('');

    expect(lastCall()).toMatchObject({ page: 1, cursor: undefined, status: undefined });
    expect(screen.getByText('page1.csv')).toBeInTheDocument();
    expect(screen.queryByText('wrong-rows.csv')).toBeNull();
  });
});

describe('p48 task 5.5 — an empty match is its own state', () => {
  /**
   * **Validates: Requirements 4.1**
   *
   * R4.1's "no runs match", and it must read as neither of the other two empties.
   * An empty match "is 0 rows with an accurate count, not an error" (task 4.3), so
   * it is asserted to be neither an alert nor the error branch.
   *
   * The `!statusFilter` clause in `AsyncContent`'s `isEmpty` is the whole
   * mechanism: without it a filtered page 1 with no matches renders "No executions
   * yet", which claims the *pipeline* has no runs on the strength of a filter.
   * Removing that clause turns this red.
   */
  it('reads as "no runs match", not as an empty history and not as an error', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: [row('run-1', 'customer_data_1.csv', '2026-08-24T13:04:01Z')],
      }),
      [`${PAGE_ONE}|CANCELLED`]: envelope({
        executions: [],
        total_count: 0,
        total_pages: 0,
      }),
    };
    renderPage();

    selectStatus('CANCELLED');

    expect(screen.getByTestId('executions-empty-page')).toHaveTextContent(
      'No runs match CANCELLED. Clear the filter to see all runs.',
    );
    // NOT empty #1 — that is a claim about the pipeline, and this pipeline has a run.
    expect(screen.queryByText('No executions yet')).toBeNull();
    // NOT empty #2 — "use Previous" is nonsense on a filtered page 1.
    expect(screen.getByTestId('executions-empty-page')).not.toHaveTextContent('Use Previous');
    // NOT an error.
    expect(screen.queryByTestId('async-content-error')).toBeNull();
    // And the way out is still mounted.
    expect(statusFilter().findTrigger().getElement()).toBeInTheDocument();
  });

  /**
   * **Validates: Requirements 4.1, 4.3**
   *
   * An empty match with `has_next` true says something different, and the
   * difference is R4.3's: under a filter `has_next` means "more raw rows remain to
   * walk", not "more matches exist". So matches may sit further on, and "no runs
   * match" full stop would be a claim the response does not support.
   */
  it('points at later pages when the walk has not finished', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope(),
      [`${PAGE_ONE}|SKIPPED`]: envelope({
        executions: [],
        total_count: 0,
        total_pages: 0,
        has_next: true,
        next_cursor: 'cursor-page-2',
        partial_counts: ['executions'],
      }),
    };
    renderPage();

    selectStatus('SKIPPED');

    expect(screen.getByTestId('executions-empty-page')).toHaveTextContent(
      'No runs match SKIPPED on this page. Try Next for later pages, or clear the filter.',
    );
  });

  /**
   * **Validates: Requirements 4.1**
   *
   * The genuinely empty history still reads as one — 5.1's copy must not have been
   * swallowed by the filtered wording. This is the other direction of the same
   * separation, and it is what fails if `!statusFilter` is widened into
   * "always false".
   */
  it('still reports an unfiltered empty first page as an empty history', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope({
        executions: [],
        total_count: 0,
        total_pages: 0,
      }),
    };
    renderPage();

    expect(screen.getByText('No executions yet')).toBeInTheDocument();
    expect(screen.queryByTestId('executions-empty-page')).toBeNull();
  });
});

describe('p48 task 5.5 — partial_counts scope honesty', () => {
  /**
   * **Validates: Requirements 4.3**
   *
   * `partial_counts` naming `executions` is task 4.3's filter walk saying it
   * stopped with matches possibly still unread — so a page shorter than
   * `page_size` no longer means "the filtered list ended". R4.3 forbids presenting
   * that as though all history had been searched, so the page says so in words
   * (`LGR-99050e6-1`: colour alone fails WCAG 1.4.1).
   *
   * The fixture is the shape that makes the omission dangerous: **two** rows on a
   * page of ten, which without this statement reads as "these are all the skips".
   */
  it('states the scope when the filter walk was bound-truncated', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope(),
      [`${PAGE_ONE}|SKIPPED`]: envelope({
        executions: [
          row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED'),
          row('skipped-42e54662', 'customer_data_4 copy.csv', '2026-08-20T20:51:00Z', 'SKIPPED'),
        ],
        page_size: 10,
        total_count: 2,
        total_pages: 1,
        has_next: true,
        next_cursor: 'cursor-page-2',
        partial_counts: ['executions', 'total_count', 'total_pages'],
      }),
    };
    renderPage();

    selectStatus('SKIPPED');

    expect(screen.getByTestId('executions-filter-scope')).toHaveTextContent(
      /search stopped before the end of this pipeline's history/,
    );
    expect(screen.getByTestId('executions-filter-scope')).toHaveTextContent('SKIPPED');
  });

  /**
   * **Validates: Requirements 4.3**
   *
   * No entry, no statement. A complete walk over a full page is not a truncated
   * one, and warning about scope unconditionally would make the warning
   * meaningless — the same argument `partial_counts` itself rests on
   * (`LGR-9a35b2d-3`: an unmarked "could-not-check" reported as "checked and found
   * none" is the defect, and its converse is noise).
   */
  it('says nothing about scope when the walk completed', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope(),
      [`${PAGE_ONE}|SKIPPED`]: envelope({
        executions: [
          row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED'),
        ],
        partial_counts: [],
      }),
    };
    renderPage();

    selectStatus('SKIPPED');

    expect(screen.queryByTestId('executions-filter-scope')).toBeNull();
  });

  /**
   * **Validates: Requirements 4.3**
   *
   * The division of labour 5.1 stated in code: `total_count` / `total_pages` are
   * the **pager's** to word, and it words them as a floor ("Page 1 of at least 4").
   * This component owns `executions` only. Asserted together on one response
   * carrying all three entries, so a change that moved either fact into the scope
   * statement — two authorities on one number — fails here.
   */
  it('leaves total_count and total_pages to the pager', () => {
    pages = {
      [`${PAGE_ONE}|${UNFILTERED}`]: envelope(),
      [`${PAGE_ONE}|SKIPPED`]: envelope({
        executions: [
          row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED'),
        ],
        total_count: 40,
        total_pages: 4,
        has_next: true,
        next_cursor: 'cursor-page-2',
        partial_counts: ['executions', 'total_count', 'total_pages'],
      }),
    };
    renderPage();

    selectStatus('SKIPPED');

    expect(screen.getByTestId('cursor-pagination-position')).toHaveTextContent(
      'Page 1 of at least 4',
    );
    expect(screen.getByTestId('executions-filter-scope')).not.toHaveTextContent('at least 4');
  });
});
