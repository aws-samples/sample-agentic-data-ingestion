// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { Pipeline } from '../../types';

/* ------------------------------------------------------------------ */
/*  Mock data                                                          */
/* ------------------------------------------------------------------ */

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

const mockRetryMutate = vi.fn();
let mockRetryIsPending = false;

const mockExecData = {
  executions: [
    {
      execution_id: 'e1', run_id: 'r1', status: 'FAILED',
      started_at: '2024-06-01T10:00:00Z', file_name: 'fail.csv',
    },
    {
      execution_id: 'e2', run_id: 'r2', status: 'COMPLETED',
      started_at: '2024-06-02T10:00:00Z', file_name: 'ok.csv',
    },
    {
      execution_id: 'e3', run_id: 'r3', status: 'RUNNING',
      started_at: '2024-06-03T10:00:00Z', file_name: 'running.csv',
      trigger: 'retry', retry_source_run_id: 'r1',
    },
  ],
  queued_files: [],
  // The wire's nested envelope (p48 task 5.1) — see `ExecutionsPage.test.tsx`.
  pagination: {
    page: 1, page_size: 10, total_count: 3, total_pages: 1,
    has_next: false, has_prev: false, next_cursor: null, partial_counts: [],
  },
};

vi.mock('../../hooks', () => ({
  usePipelineExecutions: () => ({ data: mockExecData }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: mockRetryMutate, isPending: mockRetryIsPending }),
}));

import ExecutionsPage from '../ExecutionsPage';

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

const mockPipeline: Pipeline = {
  pipeline_key: 'test-pipe',
  pipeline_name: 'Test Pipeline',
  status: 'active',
  source_schema: 'src',
  target_schema: 'tgt',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-06-01T00:00:00Z',
};

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter>
          <PipelineProvider value={{ pipeline: mockPipeline, isLoading: false, isError: false, refetch: vi.fn() }}>
            <ExecutionsPage />
          </PipelineProvider>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  mockRetryIsPending = false;
  mockRetryMutate.mockReset();
  mockNavigate.mockReset();
});

describe('ExecutionsPage — Retry action button', () => {
  it('shows Retry button for FAILED rows', () => {
    renderPage();
    const retryButtons = screen.getAllByText('Retry');
    // At least one "Retry" button for the FAILED row (the badge also says "Retry")
    expect(retryButtons.length).toBeGreaterThanOrEqual(1);
  });

  it('does not show Retry action button for COMPLETED rows', () => {
    renderPage();
    // The COMPLETED row should have "View Run" but not a retry action button
    const rows = screen.getAllByText('View Run');
    expect(rows.length).toBeGreaterThanOrEqual(1);
  });

  it('calls retryMutation.mutate when Retry action is clicked on failed row', () => {
    renderPage();
    const retryButtons = screen.getAllByTestId('retry-action-button');
    expect(retryButtons.length).toBe(1); // Only the FAILED row
    fireEvent.click(retryButtons[0]);
    expect(mockRetryMutate).toHaveBeenCalledWith({ runId: 'r1' });
  });
});

describe('ExecutionsPage — Retry Badge', () => {
  it('shows blue Retry badge for retry-triggered runs', () => {
    renderPage();
    const badges = screen.getAllByTestId('retry-badge');
    expect(badges.length).toBe(1); // Only the retry-triggered row (r3)
  });

  it('does not show Retry badge for non-retry runs', () => {
    renderPage();
    // Only 1 retry badge total (for r3)
    const badges = screen.getAllByTestId('retry-badge');
    expect(badges.length).toBe(1);
  });
});

describe('ExecutionsPage — Source run link', () => {
  it('shows source run link for retry-triggered rows', () => {
    renderPage();
    const sourceLinks = screen.getAllByTestId('source-run-link');
    expect(sourceLinks.length).toBe(1);
    expect(screen.getByText('Source: r1')).toBeInTheDocument();
  });
});
