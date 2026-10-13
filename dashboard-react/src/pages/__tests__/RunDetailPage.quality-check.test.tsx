// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `RunDetailPage` offers no on-demand quality check.
 *
 * The "Run Quality Check" button dispatched the data quality agent through
 * `POST /pipelines/{id}/runs/{runId}/quality-report`. The agent received no
 * target table and its runtime role had no Athena grant, so every check failed,
 * and its report replaced the inline Glue report on the same state-row key. The
 * button only appeared when a report already existed, so its one reachable
 * effect was to overwrite a real report with a failing one. The action, its
 * hook and the route were removed on 2026-10-11 (docs/AGENTS.md § 3). This file
 * used to test what the click claimed; it now pins that there is nothing to
 * click while the inline report still renders.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { RunDetail } from '../../types';
import type { QualityReport } from '../../hooks/use-quality-report';

const succeededRun: RunDetail = {
  run_id: 'run-1',
  pipeline_key: 'pipe-1',
  status: 'SUCCEEDED',
  started_at: '2026-06-01T10:00:00Z',
  completed_at: '2026-06-01T10:05:00Z',
  steps: [{ step_name: 'file_validation', status: 'completed' }],
};

const inlineReport: QualityReport = {
  pipeline_id: 'pipe-1',
  run_id: 'run-1',
  overall_status: 'pass',
  checks: [{ name: 'RowCount > 0', status: 'pass', actual: 1200 }],
  root_cause: null,
  recommendations: [],
  source: 'glue_inline_dq',
};

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

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
  useRunTraces: () => ({ data: null, isLoading: false }),
  useSelfHealingActivity: () => ({ data: undefined }),
  useQualityReport: () => ({ data: inlineReport, isError: false, refetch: vi.fn() }),
  useRunNeighbours: () => ({ data: undefined }),
  usePipelineDetail: () => ({ data: undefined, refetch: vi.fn() }),
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useMappingRegenPolling: () => {},
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  qualityReportKeys: { run: (runId: string) => ['quality-report', runId] },
}));

import RunDetailPage from '../RunDetailPage';
import * as hooks from '../../hooks/use-quality-report';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/pipelines/pipe-1/runs/run-1']}>
        <Routes>
          <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('RunDetailPage — no on-demand quality check', () => {
  it('renders the inline report on the Data Quality tab with no Run Quality Check button', () => {
    renderPage();
    // Cloudscape Tabs mounts only the active tab, so the tab must be opened.
    fireEvent.click(screen.getByText('Data Quality'));

    expect(screen.getByTestId('quality-report-card')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /run quality check/i })).not.toBeInTheDocument();
  });

  it('exports no trigger hook for a caller to wire back in', () => {
    expect('useTriggerQualityCheck' in hooks).toBe(false);
  });
});
