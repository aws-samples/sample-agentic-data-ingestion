// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Recent Runs' footer link — p54 task 1.2 (_Requirements: 5.1, 5.2, 5.3_).
 *
 * **What R5 actually asks for, and why it is small.** `PipelineOverviewPage.tsx`
 * takes `.slice(0, 5)` over a ten-row page, so half the fetched page is
 * discarded with no route to the rest. The destination already exists with
 * paging and a status filter — `/pipelines/:id/executions`, a sibling route
 * under `PipelineLayout` (`App.tsx:153`) — so R5 is one link and zero backend
 * work.
 *
 * **R5.2 and R5.3 are satisfied by nothing appearing**, which is why this file
 * asserts absences alongside the link. The footer holds a *link*, never a
 * control that changes `items`: `.slice(0, 5)` is untouched, so the surface
 * still shows exactly the five newest rows with no pager. That is pinned four
 * ways (p48 R10.3, p51 R3.4, the code comment at `PipelineOverviewPage.tsx:93-98`,
 * and `LGR-1f7892b-1`, **template / ratified**, which re-ratifies the
 * arrangement in as many words).
 *
 * **The four assertions that are the real regression guard are NOT here.** They
 * already exist and must keep passing unmodified:
 * `PipelineOverviewPage.recent-runs-order.test.tsx:328` / `:403` and
 * `PipelineOverviewPage.running-count.test.tsx:426` / `:431`. This file adds the
 * positive R5.1 assertion those four cannot make, plus one they do not: that the
 * footer link does **not** join the name-scoped `'View Run'` queries those two
 * suites read a row's `run_id` off (`recent-runs-order.test.tsx:306`,
 * `running-count.test.tsx:417`). A footer link named "View Run" would have
 * silently turned their length-5 assertions into length-6 failures.
 *
 * `api-client` is mocked **without** `importOriginal`, unlike the sibling
 * overview suites: this file must not execute the real module, which a
 * concurrent task is editing. `ApiError` and `apiClient` are the two names the
 * page's subtree imports from it (`RunNowButton.tsx:18`,
 * `DataPreviewTable.tsx:7`, `FileUploadModal.tsx:10`).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

type Row = Record<string, unknown>;

/** The producer's envelope (`pipelines_runs.py:1148-1176`), nested per `LGR-2bcdd0d-1`. */
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

vi.mock('../../hooks', () => ({
  usePipelineExecutions: (
    _key: unknown,
    _page: unknown,
    _cursor: unknown,
    status?: string | null,
  ) => {
    // Status-aware, matching the server (p51 C3): the filtered call must not be
    // answered with the unfiltered partition.
    if (status) return { data: envelope(rows.filter((r) => r.status === status)) };
    return { data: envelope(rows) };
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

/** Nine rows — more than the five the preview shows, so a slice change shows up. */
const NINE_ROWS: Row[] = Array.from({ length: 9 }, (_, i) => ({
  run_id: `run-2026-08-26-00-00-0${i}-abcdef${i}`,
  file_name: `customer_${i}.csv`,
  started_at: `2026-08-2${6 - i}T12:00:00Z`,
  status: 'SUCCEEDED',
}));

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

beforeEach(() => {
  cleanup();
});

describe('PipelineOverviewPage — Recent Runs footer link (p54 task 1.2)', () => {
  it('offers a route to the pipeline\u2019s full run history (R5.1)', () => {
    renderWith(NINE_ROWS);

    const link = screen.getByRole('link', { name: 'View all runs' });
    expect(link).toHaveAttribute('href', `/pipelines/${PIPELINE_KEY}/executions`);
  });

  it('offers the route even when the pipeline has no runs yet (R5.1)', () => {
    // The footer is not gated on `items`, so the affordance does not disappear
    // on the surface where an operator is most likely to go looking.
    renderWith([]);

    expect(screen.getByRole('link', { name: 'View all runs' })).toHaveAttribute(
      'href',
      `/pipelines/${PIPELINE_KEY}/executions`,
    );
    expect(screen.getByText('No runs yet')).toBeInTheDocument();
  });

  it('still shows exactly the five newest rows (R5.2)', () => {
    renderWith(NINE_ROWS);

    // Read off the per-row "View Run" link, the only place a row's `run_id`
    // reaches the DOM. `.slice(0, 5)` is untouched by this task.
    const rowLinks = screen.getAllByRole('link', { name: 'View Run' });
    expect(rowLinks).toHaveLength(5);
    expect(rowLinks.map((a) => a.getAttribute('href'))).toEqual(
      NINE_ROWS.slice(0, 5).map((r) => `/pipelines/${PIPELINE_KEY}/runs/${r.run_id as string}`),
    );
  });

  it('adds no pagination control \u2014 the footer holds a link, not a pager (R5.3)', () => {
    renderWith(NINE_ROWS);

    expect(screen.queryByRole('button', { name: /Next page/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Previous page/i })).not.toBeInTheDocument();
  });

  it('does not join the name-scoped \u2018View Run\u2019 queries two other suites rely on', () => {
    renderWith(NINE_ROWS);

    // The footer link is a sixth link in the section but must never be a sixth
    // *"View Run"* link: `recent-runs-order.test.tsx:328` and
    // `running-count.test.tsx:426` both assert length 5 through that name.
    expect(screen.getAllByRole('link', { name: 'View Run' })).toHaveLength(5);
    expect(screen.getAllByRole('link', { name: 'View all runs' })).toHaveLength(1);
  });
});
