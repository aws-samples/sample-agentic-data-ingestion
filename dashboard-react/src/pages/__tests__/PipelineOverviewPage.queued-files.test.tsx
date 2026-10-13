// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Overview page reads the array it already receives — p54 task 7.4
 * (_Requirements: 9.1, 9.3, 9.4, 9.6_).
 *
 * **Validates: Requirements 9.1, 9.3, 9.4, 9.6**
 *
 * `PipelineOverviewPage` calls `usePipelineExecutions` and, before 7.4, discarded
 * `queued_files` entirely — the key appeared nowhere in the file. Recent Runs renders
 * `executions` only, so a Deferred_File was invisible on the page an operator lands
 * on. R9.1 is that gap and this file is its positive assertion.
 *
 * ## The absences are the substantive half
 *
 * **The region is beside the Recent Runs table, never merged into it** (do-NOT 3,
 * do-NOT 15, p48 O2). A queued entry has no `run_id` and no `started_at` in its
 * published shape, so a merge means inventing both; the invented `run_id` would link
 * to a `get_run_details` that 404s, which is **R6's defect reintroduced by R9's
 * fix**. It would also consume page slots ahead of real runs and inflate the running
 * count (do-NOT 11). So this file asserts that the queued entry does **not** join the
 * Recent Runs table and carries **no** `View Run` link.
 *
 * **R9.6 — Recent Runs still shows exactly five.** `.slice(0, 5)` is untouched, and
 * the four existing assertions that are the real regression guard live elsewhere and
 * pass unmodified: `PipelineOverviewPage.recent-runs-order.test.tsx:328` / `:403` and
 * `PipelineOverviewPage.running-count.test.tsx:424` / `:429`. What this file adds is
 * the assertion those four cannot make — that adding a queued region did not change
 * the runs table's length. `hasRuns` is untouched (R8.4) and the count block above
 * the table is untouched (`LGR-1f7892b-2`, do-NOT 11).
 *
 * **The `'View Run'` name is load-bearing and this region must stay clear of it.**
 * Two suites read a row's `run_id` off that link's `href` (`recent-runs-order.test
 * .tsx:306`, `running-count.test.tsx:417`) and `LGR-1f7892b-1` / do-NOT 5 fence it. A
 * queued row with a `View Run` link would have joined those name-scoped queries and
 * broken their length assertions — which is exactly what `recent-runs-footer.test
 * .tsx` records happening to the footer link.
 *
 * `api-client` is mocked **without** `importOriginal`, following
 * `PipelineOverviewPage.recent-runs-footer.test.tsx`: `ApiError` and `apiClient` are
 * the two names the page's subtree imports from it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
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
    // count's; it never carries `queued_files`, because `status=QUEUED` is a 400 and
    // a queued entry is not a run (R9.7, `LGR-67bf5d3-3`).
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

/** Nine runs — more than the five the preview shows, so a slice change shows up. */
const NINE_RUNS: Row[] = Array.from({ length: 9 }, (_, i) => ({
  run_id: `run-2026-08-26-00-00-0${i}-abcdef${i}`,
  file_name: `customer_${i}.csv`,
  status: 'SUCCEEDED',
  started_at: `2026-08-26T0${i}:00:00Z`,
}));

function queued(file_name: string, status: string): QueuedRow {
  return {
    file_name,
    source_key: `mynewtesting/${file_name}`,
    queued_at: '2026-08-27T09:00:00.000000+00:00',
    status,
  };
}

function renderWith(executions: Row[], queuedFiles: QueuedRow[]) {
  rows = executions;
  queuedRows = queuedFiles;
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

beforeEach(() => {
  cleanup();
  rows = [];
  queuedRows = [];
});

describe('PipelineOverviewPage — a deferred file is visible while it waits (p54 R9.1)', () => {
  it('displays a queued file that the Recent Runs table cannot show', () => {
    renderWith(NINE_RUNS, [queued('customer_data_5.csv', 'queued')]);

    expect(screen.getByText('customer_data_5.csv')).toBeInTheDocument();
    expect(screen.getByText('Queued')).toBeInTheDocument();
    // The holding sentence is behind the header's question-mark button.
    expect(screen.queryByText(/a run is currently holding this pipeline/i)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'About queued files' }));
    expect(
      screen.getByText(/a run is currently holding this pipeline/i),
    ).toBeInTheDocument();
  });

  it('adds nothing when the array is empty', () => {
    renderWith(NINE_RUNS, []);

    expect(screen.queryByText(/queued files/i)).toBeNull();
    expect(screen.queryByRole('button', { name: 'About queued files' })).toBeNull();
    expect(screen.queryByText(/a run is currently holding this pipeline/i)).toBeNull();
  });
});

describe('PipelineOverviewPage — the queued region is beside Recent Runs, not in it', () => {
  it('leaves Recent Runs at exactly five rows (R9.6)', () => {
    renderWith(NINE_RUNS, [queued('customer_data_5.csv', 'queued')]);

    // The runs table is the only table whose rows carry a `View Run` link, so the
    // link count is the runs table's row count. Five, not six — a merged queued
    // entry would have made it six and would have needed an invented `run_id` to
    // link with.
    expect(screen.getAllByRole('link', { name: 'View Run' })).toHaveLength(5);
  });

  it('gives the queued entry no View Run link, because there is no run to view', () => {
    renderWith([], [queued('customer_data_5.csv', 'queued')]);

    expect(screen.getByText('customer_data_5.csv')).toBeInTheDocument();
    expect(screen.queryAllByRole('link', { name: 'View Run' })).toHaveLength(0);
  });
});

describe('PipelineOverviewPage — a lost file is visible after (p54 R9.3, R9.4)', () => {
  it('renders a lost file as Failed (DLQ) with the reason, beside a deferred one', () => {
    renderWith(NINE_RUNS, [
      queued('customer_data_5.csv', 'queued'),
      queued('customer_data_7.csv', 'failed_dlq'),
    ]);

    // Both directions on one render, so neither passes for a surface that labels
    // every entry the same way.
    expect(screen.getByText('Queued')).toBeInTheDocument();
    expect(screen.getByText('Failed (DLQ)')).toBeInTheDocument();
    expect(screen.getByText('customer_data_7.csv')).toBeInTheDocument();
    expect(
      screen.getByText(/waited longer than its redelivery attempts allowed/i),
    ).toBeInTheDocument();
  });

  it('says nothing about loss when every queued file is still deferred', () => {
    renderWith(NINE_RUNS, [queued('customer_data_5.csv', 'queued')]);

    expect(screen.queryByText(/redelivery attempts/i)).toBeNull();
    expect(screen.queryByText('Failed (DLQ)')).toBeNull();
  });
});
