// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Pipeline } from '../../types';

/* ------------------------------------------------------------------ */
/*  P29 R1.1 / R1.5 — Async_State_Triple on ExecutionsPage.           */
/*                                                                    */
/*  An API failure MUST render an error + retry, NOT the             */
/*  "No executions yet" empty-state copy.                            */
/* ------------------------------------------------------------------ */

// Mutable query stub swapped per test (vi.mock factory is hoisted, so it
// must close over a mutable variable rather than a per-test value).
let mockExecQuery: {
  data?: unknown;
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
  refetch: () => unknown;
};
const mockRefetch = vi.fn();

vi.mock('../../hooks', () => ({
  usePipelineExecutions: () => mockExecQuery,
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
}));

import ExecutionsPage from '../ExecutionsPage';

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
  return render(
    <MemoryRouter>
      <ToastProvider>
        <PipelineProvider value={{ pipeline: mockPipeline, isLoading: false, isError: false, refetch: vi.fn() }}>
          <ExecutionsPage />
        </PipelineProvider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockRefetch.mockReset();
});

describe('ExecutionsPage — Async_State_Triple', () => {
  it('renders a loading spinner while the query is loading', () => {
    mockExecQuery = { data: undefined, isLoading: true, isError: false, error: null, refetch: mockRefetch };
    renderPage();
    expect(screen.getByTestId('async-content-loading')).toBeInTheDocument();
    expect(screen.queryByText('No executions yet')).not.toBeInTheDocument();
  });

  it('renders an error with a Retry button on API failure (not the empty copy)', () => {
    mockExecQuery = {
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error('Service unavailable'),
      refetch: mockRefetch,
    };
    renderPage();
    // Error surface + retry affordance present …
    expect(screen.getByTestId('async-content-error')).toBeInTheDocument();
    expect(screen.getByTestId('async-content-retry')).toBeInTheDocument();
    expect(screen.getByText('Service unavailable')).toBeInTheDocument();
    // … and the failure MUST NOT masquerade as "no data".
    expect(screen.queryByText('No executions yet')).not.toBeInTheDocument();
  });

  it('invokes refetch when Retry is clicked on the error state', () => {
    mockExecQuery = {
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error('boom'),
      refetch: mockRefetch,
    };
    renderPage();
    fireEvent.click(screen.getByTestId('async-content-retry'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('renders the "No executions yet" empty copy only when genuinely empty', () => {
    mockExecQuery = {
      // The wire's nested envelope (p48 task 5.1) — see `ExecutionsPage.test.tsx`
      // for why the previous flat `total_count`/`page`/`page_size`/`has_more`
      // spelling was a fiction no producer ever emitted.
      data: {
        executions: [],
        queued_files: [],
        pagination: {
          page: 1, page_size: 10, total_count: 0, total_pages: 0,
          has_next: false, has_prev: false, next_cursor: null, partial_counts: [],
        },
      },
      isLoading: false,
      isError: false,
      error: null,
      refetch: mockRefetch,
    };
    renderPage();
    expect(screen.getByText('No executions yet')).toBeInTheDocument();
    expect(screen.queryByTestId('async-content-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('async-content-loading')).not.toBeInTheDocument();
  });

  it('renders the executions table when data is present', () => {
    mockExecQuery = {
      data: {
        executions: [
          { execution_id: 'e1', run_id: 'r1', status: 'COMPLETED', started_at: '2024-06-01T10:00:00Z', file_name: 'data.csv', schema_version: 1 },
        ],
        queued_files: [],
        pagination: {
          page: 1, page_size: 10, total_count: 1, total_pages: 1,
          has_next: false, has_prev: false, next_cursor: null, partial_counts: [],
        },
      },
      isLoading: false,
      isError: false,
      error: null,
      refetch: mockRefetch,
    };
    renderPage();
    expect(screen.getByText('data.csv')).toBeInTheDocument();
    expect(screen.queryByText('No executions yet')).not.toBeInTheDocument();
    expect(screen.queryByTestId('async-content-error')).not.toBeInTheDocument();
  });
});
