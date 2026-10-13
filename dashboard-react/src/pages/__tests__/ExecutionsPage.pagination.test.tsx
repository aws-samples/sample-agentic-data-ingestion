// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The run-history pager — p48 task 5.1 (C9, R2.6, R10.1), CP7.
 *
 * WHAT WAS BROKEN, AND WHY NO EXISTING TEST SAW IT
 * ------------------------------------------------
 * `ExecutionsPage.tsx:252` gated the pager on
 * `Math.ceil((data.total_count ?? 0) / 10) > 1`, and `total_count` was
 * `undefined` because the producer nests it under `pagination` — so `0 > 1` hid
 * the control unconditionally. The other five `ExecutionsPage.*.test.tsx` files
 * could not catch that: task 1.2 established that every one of them mocks
 * `../../hooks` wholesale, and each mock carried the *flat* shape the frontend
 * type wrongly declared. The mock agreed with the bug, so the bug passed.
 *
 * This file mocks the hook too — there is no way to render this page without
 * doing so — but its fixtures are the producer's envelope
 * (`pipelines_runs.py:1148-1176`), so a shape drift shows up here as a failure
 * rather than as a page that renders nothing.
 *
 * WHY THE GATE IS NOT A COUNT
 * ---------------------------
 * CP7 is phrased over the count — *"for any exact `total_count` greater than
 * `page_size`, the control is rendered"* — and under a cursor model the count is
 * the wrong observable to gate on, which is what task 4.3 reported: under a
 * status filter `total_count` is a **floor** (`pipelines_runs.py:1107-1108` sets
 * it to `len(executions)`), so `ceil(total_count / page_size) > 1` is false on a
 * filtered page even while `has_next` is true. `has_next` is minted from the
 * page query's own `LastEvaluatedKey` and is exact regardless of the count walk.
 *
 * So CP7 is asserted through `has_next || has_prev`, in both directions, and the
 * floor case is asserted separately as the thing the count-shaped gate gets
 * wrong. `pagerRenders` below is the CP7 predicate written once.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as fc from 'fast-check';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
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

/**
 * Pages keyed by the cursor that reaches them; `PAGE_ONE` is the no-cursor read.
 * Mutated per test before rendering.
 */
const PAGE_ONE = '@no-cursor';
let pages: Record<string, Envelope> = {};
let isFetching = false;

/** Every `(pipelineKey, page, cursor)` the page asked the hook for, in order. */
const hookCalls: Array<{ key: unknown; page: unknown; cursor: unknown }> = [];

vi.mock('../../hooks', () => ({
  usePipelineExecutions: (key: unknown, page: unknown, cursor: unknown) => {
    hookCalls.push({ key, page, cursor });
    return {
      data: pages[(cursor as string) ?? PAGE_ONE],
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

function envelope(overrides: Partial<Envelope['pagination']> & { executions?: Envelope['executions'] } = {}): Envelope {
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

/** CP7's predicate, written once so no test restates it. */
function pagerRenders(): boolean {
  return screen.queryByTestId('cursor-pagination-next') !== null;
}

beforeEach(() => {
  cleanup();
  hookCalls.length = 0;
  isFetching = false;
  pages = {};
});

describe('p48 CP7 — the pager renders exactly when another page is reachable', () => {
  /**
   * **Validates: Requirements 2.6, 10.1**
   *
   * CP7, over generated envelopes. Written as a property rather than as examples
   * because two independent expressions had to be wrong at the same time to
   * suppress this control (R2.6), so a single example passes with one of them
   * still broken.
   *
   * The generator deliberately decorrelates `total_count` from the direction
   * flags. That is not artificial: it is the live shape under a status filter,
   * where the count is a floor of matches while `has_next` remains an exact
   * statement about raw rows remaining. A gate computed from the count would
   * pass a correlated generator and fail this one.
   */
  it('renders iff has_next || has_prev, for any count', () => {
    fc.assert(
      fc.property(
        fc.record({
          total_count: fc.integer({ min: 0, max: 400 }),
          page_size: fc.constantFrom(2, 10, 25),
          page: fc.integer({ min: 1, max: 9 }),
          has_next: fc.boolean(),
          has_prev: fc.boolean(),
        }),
        (wire) => {
          cleanup();
          pages = {
            [PAGE_ONE]: envelope({
              ...wire,
              total_pages: Math.ceil(wire.total_count / wire.page_size),
              next_cursor: wire.has_next ? 'cursor-for-page-2' : null,
            }),
          };
          renderPage();
          expect(pagerRenders()).toBe(wire.has_next || wire.has_prev);
        },
      ),
      { numRuns: 200 },
    );
  });

  /**
   * **Validates: Requirements 2.6**
   *
   * CP7 stated in its own count-shaped terms, so the requirement's literal
   * wording has a witness: an exact count above `page_size` does render the
   * control. It renders *because* the producer sets `has_next` on such a page,
   * not because this file recomputed a ceiling.
   */
  it('renders when an exact total_count exceeds page_size', () => {
    pages = {
      [PAGE_ONE]: envelope({
        total_count: 23,
        page_size: 10,
        total_pages: 3,
        has_next: true,
        next_cursor: 'cursor-for-page-2',
      }),
    };
    renderPage();
    expect(pagerRenders()).toBe(true);
    expect(screen.getByTestId('cursor-pagination-position')).toHaveTextContent('Page 1 of 3');
  });

  /**
   * **Validates: Requirements 2.6, 4.3**
   *
   * THE MUTATION TARGET. Task 4.3's reported case: under a filter `total_count`
   * is a floor, so `ceil(total_count / page_size) > 1` is `1 > 1` — false —
   * while `has_next` is true and a further page genuinely exists. Reverting the
   * gate in `ExecutionsPage.tsx` to that count-shaped expression turns this test
   * red and leaves the rest of the file green, which is why it is called out
   * here rather than folded into the property above.
   */
  it('renders when total_count is a floor at or below page_size but has_next is true', () => {
    pages = {
      [PAGE_ONE]: envelope({
        executions: [row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED')],
        total_count: 1,
        page_size: 10,
        total_pages: 1,
        has_next: true,
        next_cursor: 'cursor-for-page-2',
        partial_counts: ['executions', 'total_count', 'total_pages'],
      }),
    };
    renderPage();
    expect(pagerRenders()).toBe(true);
  });

  /**
   * **Validates: Requirements 2.6**
   *
   * A single-page result renders **no** control, and that is the intended
   * behaviour rather than the `ListPagination.tsx:11-16` stranding bug returning.
   * That bug needed a *transition* into a state with no control — the user
   * reached the last page and lost the route back. Here both directions are
   * false only on a one-page history, which is the initial state and has nowhere
   * to have come from. It is also the behaviour this page had before (`> 1`).
   */
  it('renders no control for a single-page history, and still renders the rows', () => {
    pages = { [PAGE_ONE]: envelope({ total_count: 4, total_pages: 1 }) };
    renderPage();
    expect(pagerRenders()).toBe(false);
    expect(screen.queryByTestId('cursor-pagination-previous')).toBeNull();
    expect(screen.getByText('customer_data_1.csv')).toBeInTheDocument();
  });

  /**
   * **Validates: Requirements 2.6**
   *
   * The last page keeps the control mounted with Next disabled — the fence
   * `ListPagination.tsx:11-16` records, carried into the cursor model. Gating on
   * `has_next` alone would unmount it here and strand the user.
   */
  it('keeps the control mounted on the last page, with Next disabled', () => {
    pages = {
      [PAGE_ONE]: envelope({
        page: 3,
        total_count: 23,
        total_pages: 3,
        has_next: false,
        has_prev: true,
        next_cursor: null,
      }),
    };
    renderPage();
    expect(pagerRenders()).toBe(true);
    expect(screen.getByTestId('cursor-pagination-next')).toBeDisabled();
    expect(screen.getByTestId('cursor-pagination-previous')).not.toBeDisabled();
  });
});

describe('p48 task 5.1 — cursor navigation', () => {
  /**
   * **Validates: Requirements 2.6, 10.1**
   *
   * Next follows the cursor the wire handed over, and the second page's rows
   * replace the first's. The assertion on `hookCalls` is what pins the client
   * protocol task 3.2 stated: page 1 asks with no cursor, page 2 asks with the
   * previous response's `next_cursor`.
   */
  it('navigates to the second page by following next_cursor', () => {
    pages = {
      [PAGE_ONE]: envelope({
        executions: [row('run-newest', 'page1.csv', '2026-08-24T13:04:01Z')],
        total_count: 23,
        total_pages: 3,
        has_next: true,
        next_cursor: 'cursor-page-2',
      }),
      'cursor-page-2': envelope({
        executions: [row('run-older', 'page2.csv', '2026-08-22T12:43:57Z')],
        page: 2,
        total_count: 23,
        total_pages: 3,
        has_next: true,
        has_prev: true,
        next_cursor: 'cursor-page-3',
      }),
    };
    renderPage();
    expect(hookCalls[0]).toMatchObject({ page: 1, cursor: undefined });

    fireEvent.click(screen.getByTestId('cursor-pagination-next'));

    expect(screen.getByText('page2.csv')).toBeInTheDocument();
    expect(screen.queryByText('page1.csv')).toBeNull();
    expect(screen.getByTestId('cursor-pagination-position')).toHaveTextContent('Page 2 of 3');
    expect(hookCalls[hookCalls.length - 1]).toMatchObject({ page: 2, cursor: 'cursor-page-2' });
  });

  /**
   * **Validates: Requirements 2.6, 10.1**
   *
   * Previous returns to the rows page 1 showed. DynamoDB hands out no backward
   * cursor, so this is the client popping its own stack — the assertion that
   * matters is that popping produces a *no-cursor* read again, which is the only
   * way page 1 is addressable.
   */
  it('returns to the first page, and to the rows it showed', () => {
    pages = {
      [PAGE_ONE]: envelope({
        executions: [row('run-newest', 'page1.csv', '2026-08-24T13:04:01Z')],
        total_count: 23,
        total_pages: 3,
        has_next: true,
        next_cursor: 'cursor-page-2',
      }),
      'cursor-page-2': envelope({
        executions: [row('run-older', 'page2.csv', '2026-08-22T12:43:57Z')],
        page: 2,
        total_count: 23,
        total_pages: 3,
        has_next: true,
        has_prev: true,
        next_cursor: 'cursor-page-3',
      }),
    };
    renderPage();
    fireEvent.click(screen.getByTestId('cursor-pagination-next'));
    expect(screen.getByText('page2.csv')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('cursor-pagination-previous'));

    expect(screen.getByText('page1.csv')).toBeInTheDocument();
    expect(screen.queryByText('page2.csv')).toBeNull();
    expect(hookCalls[hookCalls.length - 1]).toMatchObject({ page: 1, cursor: undefined });
    expect(screen.getByTestId('cursor-pagination-previous')).toBeDisabled();
  });

  /**
   * **Validates: Requirements 2.6, 4.3**
   *
   * Next landing on an empty page is a normal outcome, not a failure: task 4.3
   * records that under a status filter `has_next` means "more raw rows remain to
   * walk", which is an upper bound on matches. So it must not render as an error,
   * and — the part that would actually strand a user — the pager must survive.
   *
   * Before this task the page-1 empty predicate applied to every page, so
   * `AsyncContent` replaced the children (pager included) with "No executions
   * yet". That is the `ListPagination` stranding bug by a second route: an empty
   * page with no Previous.
   */
  it('renders an empty later page as a statement about the page, keeping Previous reachable', () => {
    pages = {
      [PAGE_ONE]: envelope({
        executions: [row('skipped-7412636f', 'customer_data_3.csv', '2026-08-24T13:04:04Z', 'SKIPPED')],
        total_count: 1,
        total_pages: 1,
        has_next: true,
        next_cursor: 'cursor-page-2',
        partial_counts: ['executions', 'total_count', 'total_pages'],
      }),
      'cursor-page-2': envelope({
        executions: [],
        page: 2,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: true,
        next_cursor: null,
        partial_counts: [],
      }),
    };
    renderPage();
    fireEvent.click(screen.getByTestId('cursor-pagination-next'));

    expect(screen.getByTestId('executions-empty-page')).toHaveTextContent(
      'No runs on this page. Use Previous to go back.',
    );
    // NOT the page-1 copy, which would claim the pipeline has no runs at all.
    expect(screen.queryByText('No executions yet')).toBeNull();
    // NOT an error state.
    expect(screen.queryByTestId('async-content-error')).toBeNull();
    // And the way out is still there.
    expect(pagerRenders()).toBe(true);
    expect(screen.getByTestId('cursor-pagination-previous')).not.toBeDisabled();

    fireEvent.click(screen.getByTestId('cursor-pagination-previous'));
    expect(screen.getByText('customer_data_3.csv')).toBeInTheDocument();
  });

  /**
   * **Validates: Requirements 2.6**
   *
   * Page 1 with nothing in it still reads "No executions yet" — the empty-page
   * copy above must not have swallowed the genuine empty-history case.
   */
  it('still reports an empty first page as an empty history', () => {
    pages = {
      [PAGE_ONE]: envelope({ executions: [], total_count: 0, total_pages: 0 }),
    };
    renderPage();
    expect(screen.getByText('No executions yet')).toBeInTheDocument();
    expect(screen.queryByTestId('executions-empty-page')).toBeNull();
  });
});

describe('p48 task 5.1 — the pager reads the wire rather than recomputing it', () => {
  /**
   * **Validates: Requirements 2.3, 4.3**
   *
   * `total_pages` comes off the wire. The producer derives it as
   * `ceil(total_count / page_size)`; recomputing it here would make this page a
   * second authority on the page count. Asserted with a wire value that is
   * *inconsistent* with the count — which a local recomputation could not
   * produce, so the assertion can only pass if the number was read.
   */
  it('displays the wire total_pages, not a locally derived ceiling', () => {
    pages = {
      [PAGE_ONE]: envelope({
        total_count: 23,
        page_size: 10,
        // Deliberately not ceil(23/10): a recomputing pager would say 3.
        total_pages: 7,
        has_next: true,
        next_cursor: 'cursor-page-2',
      }),
    };
    renderPage();
    expect(screen.getByTestId('cursor-pagination-position')).toHaveTextContent('Page 1 of 7');
  });

  /**
   * **Validates: Requirements 4.3**
   *
   * A floor is not rendered as a total. `partial_counts` naming `total_pages` is
   * the producer saying the count walk hit a bound, and R4.3 forbids presenting
   * that as if all of history were searched — so the wording says so, in words
   * rather than by styling (`LGR-99050e6-1`).
   */
  it('words a bound-truncated total_pages as a floor', () => {
    pages = {
      [PAGE_ONE]: envelope({
        total_count: 40,
        total_pages: 4,
        has_next: true,
        next_cursor: 'cursor-page-2',
        partial_counts: ['total_count', 'total_pages'],
      }),
    };
    renderPage();
    expect(screen.getByTestId('cursor-pagination-position')).toHaveTextContent(
      'Page 1 of at least 4',
    );
  });

  /**
   * **Validates: Requirements 2.6**
   *
   * Both controls are disabled while a page request is in flight, so a second
   * click cannot advance the cursor stack past a cursor that has not arrived.
   * Disabled rather than unmounted, per the fence.
   */
  it('disables both directions while a page is in flight', () => {
    isFetching = true;
    pages = {
      [PAGE_ONE]: envelope({
        page: 2,
        total_count: 23,
        total_pages: 3,
        has_next: true,
        has_prev: true,
        next_cursor: 'cursor-page-3',
      }),
    };
    renderPage();
    expect(screen.getByTestId('cursor-pagination-next')).toBeDisabled();
    expect(screen.getByTestId('cursor-pagination-previous')).toBeDisabled();
  });
});
