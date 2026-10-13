// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * RunDetailPage approval tests — the single Approval_Contract (R6).
 *
 * Verifies RunDetailPage no longer owns an inline approval mutation: the
 * pending-approval Alert routes through the shared `useApprovalAction` hook
 * (with the acting `runId`), and post-approval refresh is event-driven — there
 * is NO 1s/3s/6s/10s `setTimeout` refetch chain.
 *
 * Requirements: 6.1, 6.3
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

const waitingRun: RunDetail = {
  run_id: 'run-wait-1',
  pipeline_key: 'pipe-1',
  status: 'WAITING_APPROVAL',
  started_at: '2024-06-01T10:00:00Z',
  approvals: [
    {
      approval_id: 'appr-1',
      pipeline_key: 'pipe-1',
      run_id: 'run-wait-1',
      approval_type: 'schema_mapping',
      status: 'pending',
      created_at: '2024-06-01T10:01:00Z',
    },
  ],
  steps: [{ step_name: 'schema_mapping', status: 'running' }],
};

const mockRefetch = vi.fn();
const mockApprovalMutate = vi.fn();

vi.mock('../../hooks', () => ({
  useRunDetail: () => ({
    data: waitingRun,
    isLoading: false,
    isError: false,
    refetch: mockRefetch,
    isWebSocketConnected: false,
  }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRunTraces: () => ({ data: null, isLoading: false }),
  useSelfHealingActivity: () => ({ data: undefined }),
  useQualityReport: () => ({ data: undefined }),
  useRunNeighbours: () => ({ data: undefined }),
  usePipelineDetail: () => ({ data: undefined, refetch: vi.fn() }),
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useMappingRegenPolling: () => {},
  useApprovalAction: () => ({ mutate: mockApprovalMutate, isPending: false }),
  qualityReportKeys: { run: (runId: string) => ['quality-report', runId] },
}));

import RunDetailPage from '../RunDetailPage';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/pipe-1/runs/run-wait-1']}>
          <Routes>
            <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockRefetch.mockReset();
  mockApprovalMutate.mockReset();
});

describe('RunDetailPage — approval via shared contract (R6)', () => {
  it('routes Approve through the shared useApprovalAction hook with the run id', () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    expect(mockApprovalMutate).toHaveBeenCalledTimes(1);
    expect(mockApprovalMutate.mock.calls[0][0]).toMatchObject({
      id: 'appr-1',
      action: 'approve',
      runId: 'run-wait-1',
    });
  });

  it('routes Reject through the shared hook', () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));

    expect(mockApprovalMutate).toHaveBeenCalledTimes(1);
    expect(mockApprovalMutate.mock.calls[0][0]).toMatchObject({
      id: 'appr-1',
      action: 'reject',
      runId: 'run-wait-1',
    });
  });

  it('schedules NO setTimeout refetch chain after approval (event-driven refresh)', () => {
    renderPage();

    // Switch to fake timers after render so we only observe timers scheduled by
    // the approval click, not render-time ones.
    vi.useFakeTimers();
    const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout');
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

      // Advance past the old 1s/3s/6s/10s chain window.
      vi.advanceTimersByTime(11_000);

      // The old code called refetch() from setTimeout at 1s/3s/6s — that chain
      // is gone; refresh is driven by mutation invalidation + WebSocket.
      expect(mockRefetch).not.toHaveBeenCalled();

      // None of the old chain delays were scheduled.
      const delays = setTimeoutSpy.mock.calls.map((c) => c[1]);
      expect(delays).not.toContain(1_000);
      expect(delays).not.toContain(3_000);
      expect(delays).not.toContain(6_000);
      expect(delays).not.toContain(10_000);
    } finally {
      vi.useRealTimers();
    }
  });
});
