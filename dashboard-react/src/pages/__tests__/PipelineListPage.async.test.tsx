// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';

/* ------------------------------------------------------------------ */
/*  P29 R1.4 — Async_State_Triple on PipelineListPage (PipelinesPage). */
/*                                                                    */
/*  A load failure MUST render an error + Retry, NOT the             */
/*  "No pipelines found." empty-state copy.                          */
/* ------------------------------------------------------------------ */

let mockPipelinesQuery: {
  data?: unknown;
  isLoading: boolean;
  isError: boolean;
  refetch: () => unknown;
};
const mockRefetch = vi.fn();

vi.mock('../../hooks', () => ({
  usePipelines: () => mockPipelinesQuery,
  useBulkArchive: () => ({ execute: vi.fn(), progress: null, isExecuting: false }),
  useBulkDelete: () => ({ execute: vi.fn(), progress: null, isExecuting: false }),
  useStats: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useUpdates: () => ({ data: { updates: [], count: 0 }, isLoading: false, isError: false }),
}));

import PipelineListPage from '../PipelineListPage';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter>
          <PipelineListPage />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockRefetch.mockReset();
});

describe('PipelineListPage — Async_State_Triple (R1.4)', () => {
  it('renders an error with a Retry button on API failure (not the empty copy)', () => {
    mockPipelinesQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    renderPage();
    expect(screen.getByText('Failed to load pipelines. Check your API connection.')).toBeInTheDocument();
    expect(screen.getByTestId('pipelines-retry')).toBeInTheDocument();
    expect(screen.queryByText('No pipelines found.')).not.toBeInTheDocument();
  });

  it('invokes refetch when Retry is clicked on the error state', () => {
    mockPipelinesQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    renderPage();
    fireEvent.click(screen.getByTestId('pipelines-retry'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('renders the "No pipelines found." empty copy only when genuinely empty', () => {
    mockPipelinesQuery = {
      data: { pipelines: [], total_count: 0, page: 1, page_size: 20, total_pages: 1, has_more: false },
      isLoading: false,
      isError: false,
      refetch: mockRefetch,
    };
    renderPage();
    expect(screen.getByText('No pipelines found.')).toBeInTheDocument();
    expect(screen.queryByTestId('pipelines-retry')).not.toBeInTheDocument();
  });
});
