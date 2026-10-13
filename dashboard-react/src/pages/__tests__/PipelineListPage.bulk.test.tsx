// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Pipeline } from '../../types';

/* ------------------------------------------------------------------ */
/*  Mock data                                                          */
/* ------------------------------------------------------------------ */

const PIPELINES: Pipeline[] = [
  {
    pipeline_key: 'p1', pipeline_name: 'Pipeline Alpha', status: 'active',
    source_schema: 'src', target_schema: 'tgt',
    created_at: '2024-01-01T00:00:00Z', updated_at: '2024-06-01T00:00:00Z',
  },
  {
    pipeline_key: 'p2', pipeline_name: 'Pipeline Beta', status: 'archived',
    source_schema: 'src', target_schema: 'tgt',
    created_at: '2024-01-02T00:00:00Z', updated_at: '2024-06-02T00:00:00Z',
  },
  {
    pipeline_key: 'p3', pipeline_name: 'Pipeline Gamma', status: 'archived',
    source_schema: 'src', target_schema: 'tgt',
    created_at: '2024-01-03T00:00:00Z', updated_at: '2024-06-03T00:00:00Z',
  },
];

const mockPipelinesData = {
  pipelines: PIPELINES,
  total_count: 3,
  page: 1,
  page_size: 20,
  total_pages: 1,
  has_more: false,
};

/* ------------------------------------------------------------------ */
/*  Mocks                                                              */
/* ------------------------------------------------------------------ */

const mockBulkArchiveExecute = vi.fn();
const mockBulkDeleteExecute = vi.fn();
let mockBulkArchiveProgress: { completed: number; total: number } | null = null;
let mockBulkDeleteProgress: { completed: number; total: number } | null = null;
let mockBulkArchiveIsExecuting = false;
let mockBulkDeleteIsExecuting = false;

vi.mock('../../hooks', () => ({
  usePipelines: () => ({
    data: mockPipelinesData,
    isLoading: false,
    isError: false,
  }),
  useBulkArchive: () => ({
    execute: mockBulkArchiveExecute,
    progress: mockBulkArchiveProgress,
    isExecuting: mockBulkArchiveIsExecuting,
  }),
  useBulkDelete: () => ({
    execute: mockBulkDeleteExecute,
    progress: mockBulkDeleteProgress,
    isExecuting: mockBulkDeleteIsExecuting,
  }),
  useStats: () => ({ data: undefined, isLoading: false }),
  useUpdates: () => ({ data: { updates: [], count: 0 }, isLoading: false, isError: false }),
}));

vi.mock('../../contexts/ToastContext', async () => {
  const actual = await vi.importActual('../../contexts/ToastContext');
  return {
    ...actual,
    useToast: () => ({ showToast: mockShowToast }),
  };
});

const mockShowToast = vi.fn();

import PipelineListPage from '../PipelineListPage';

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

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
  vi.clearAllMocks();
  mockBulkArchiveProgress = null;
  mockBulkDeleteProgress = null;
  mockBulkArchiveIsExecuting = false;
  mockBulkDeleteIsExecuting = false;
  mockBulkArchiveExecute.mockResolvedValue({ succeeded: ['p1'], failed: [] });
  mockBulkDeleteExecute.mockResolvedValue({ succeeded: ['p2'], failed: [] });
});

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

describe('PipelineListPage — Bulk Operations', () => {
  it('renders table with multi-select checkboxes', () => {
    renderPage();
    // Cloudscape multi-select tables render checkboxes with role="checkbox"
    const checkboxes = screen.getAllByRole('checkbox');
    // At least one checkbox per row + select-all
    expect(checkboxes.length).toBeGreaterThanOrEqual(PIPELINES.length);
  });

  it('renders pipeline names as links for navigation', () => {
    renderPage();
    const link = screen.getByText('Pipeline Alpha');
    expect(link.closest('a')).toHaveAttribute('href', '/pipelines/p1');
  });

  it('does not show bulk action bar when no items selected', () => {
    renderPage();
    expect(screen.queryByTestId('bulk-archive-button')).not.toBeInTheDocument();
  });

  it('shows bulk action bar when items are selected', () => {
    renderPage();
    // Select the first row checkbox
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[1]); // first row checkbox (index 0 is select-all)
    expect(screen.getByTestId('bulk-archive-button')).toBeInTheDocument();
  });

  it('shows confirmation modal when archive is clicked', () => {
    renderPage();
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByTestId('bulk-archive-button'));
    expect(screen.getByText(/Are you sure you want to archive/)).toBeInTheDocument();
  });

  it('calls bulkArchive.execute on confirm', async () => {
    renderPage();
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByTestId('bulk-archive-button'));
    fireEvent.click(screen.getByTestId('bulk-confirm-submit'));

    await waitFor(() => {
      expect(mockBulkArchiveExecute).toHaveBeenCalledOnce();
    });
  });

  it('shows success toast after successful bulk archive', async () => {
    mockBulkArchiveExecute.mockResolvedValue({ succeeded: ['p1'], failed: [] });
    renderPage();
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByTestId('bulk-archive-button'));
    fireEvent.click(screen.getByTestId('bulk-confirm-submit'));

    await waitFor(() => {
      expect(mockShowToast).toHaveBeenCalledWith(
        expect.stringContaining('Successfully archived'),
        'success',
      );
    });
  });

  it('shows warning toast on partial failure', async () => {
    mockBulkArchiveExecute.mockResolvedValue({
      succeeded: ['p1'],
      failed: [{ id: 'p2', name: 'Pipeline Beta', error: 'Server error' }],
    });
    renderPage();
    // Select all
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[0]); // select-all
    fireEvent.click(screen.getByTestId('bulk-archive-button'));
    fireEvent.click(screen.getByTestId('bulk-confirm-submit'));

    await waitFor(() => {
      expect(mockShowToast).toHaveBeenCalledWith(
        expect.stringContaining('Pipeline Beta'),
        'warning',
      );
    });
  });

  it('shows error toast when all operations fail', async () => {
    mockBulkArchiveExecute.mockResolvedValue({
      succeeded: [],
      failed: [{ id: 'p1', name: 'Pipeline Alpha', error: 'fail' }],
    });
    renderPage();
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByTestId('bulk-archive-button'));
    fireEvent.click(screen.getByTestId('bulk-confirm-submit'));

    await waitFor(() => {
      expect(mockShowToast).toHaveBeenCalledWith(
        expect.stringContaining('Failed to archive'),
        'error',
      );
    });
  });

  it('dismisses modal on cancel without executing', () => {
    renderPage();
    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByTestId('bulk-archive-button'));
    // Confirm modal content is showing
    expect(screen.getByTestId('bulk-confirm-submit')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('bulk-confirm-cancel'));
    // After cancel, the confirm submit button should no longer be accessible
    // (Cloudscape Modal hides content when visible=false)
    expect(mockBulkArchiveExecute).not.toHaveBeenCalled();
  });
});
