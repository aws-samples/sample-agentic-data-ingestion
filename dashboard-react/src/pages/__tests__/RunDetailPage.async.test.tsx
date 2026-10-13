// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * RunDetailPage Async_State_Triple tests (P29 R1.4).
 *
 * Supplementary tab queries (AI Traces, Data Quality) must surface an
 * error + Retry on failure rather than masquerading as an empty tab
 * ("No AI traces available" / "No data quality results available").
 *
 * Requirements: 1.4
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import type { RunDetail } from '../../types';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

const succeededRun: RunDetail = {
  run_id: 'run-1',
  pipeline_key: 'pipe-1',
  status: 'SUCCEEDED',
  started_at: '2024-06-01T10:00:00Z',
  completed_at: '2024-06-01T10:05:00Z',
  approvals: [],
  steps: [{ step_name: 'schema_extract', status: 'success' }],
};

const mockRefetchTraces = vi.fn();
const mockRefetchQuality = vi.fn();

// Mutable per-test error flags for the two supplementary queries.
let tracesIsError = false;
let qualityIsError = false;

vi.mock('../../hooks', () => ({
  useRunDetail: () => ({
    data: succeededRun,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
    isWebSocketConnected: false,
  }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRunTraces: () => ({ data: null, isLoading: false, isError: tracesIsError, refetch: mockRefetchTraces }),
  useSelfHealingActivity: () => ({ data: undefined }),
  useQualityReport: () => ({ data: undefined, isError: qualityIsError, refetch: mockRefetchQuality }),
  useRunNeighbours: () => ({ data: undefined }),
  usePipelineDetail: () => ({ data: undefined, refetch: vi.fn() }),
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useMappingRegenPolling: () => {},
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  qualityReportKeys: { run: (runId: string) => ['quality-report', runId] },
}));

import RunDetailPage from '../RunDetailPage';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/pipe-1/runs/run-1']}>
          <Routes>
            <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  tracesIsError = false;
  qualityIsError = false;
  mockRefetchTraces.mockReset();
  mockRefetchQuality.mockReset();
});

describe('RunDetailPage — Async_State_Triple on supplementary tabs (R1.4)', () => {
  it('AI Traces tab renders an error + Retry (not the empty copy) on trace load failure', () => {
    tracesIsError = true;
    renderPage();
    fireEvent.click(screen.getByRole('tab', { name: 'AI Traces' }));

    expect(screen.getByTestId('traces-error')).toBeInTheDocument();
    expect(screen.getByTestId('traces-retry')).toBeInTheDocument();
    expect(screen.queryByText('No AI traces available for this run.')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('traces-retry'));
    expect(mockRefetchTraces).toHaveBeenCalledTimes(1);
  });

  it('AI Traces tab shows the empty copy only when genuinely empty (no error)', () => {
    tracesIsError = false;
    renderPage();
    fireEvent.click(screen.getByRole('tab', { name: 'AI Traces' }));
    expect(screen.getByText('No AI traces available for this run.')).toBeInTheDocument();
    expect(screen.queryByTestId('traces-error')).not.toBeInTheDocument();
  });

  it('Data Quality tab renders an error + Retry (not the empty copy) on report load failure', () => {
    qualityIsError = true;
    renderPage();
    fireEvent.click(screen.getByRole('tab', { name: 'Data Quality' }));

    expect(screen.getByTestId('quality-error')).toBeInTheDocument();
    expect(screen.getByTestId('quality-retry')).toBeInTheDocument();
    expect(screen.queryByText('No data quality results available for this run.')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('quality-retry'));
    expect(mockRefetchQuality).toHaveBeenCalledTimes(1);
  });
});
