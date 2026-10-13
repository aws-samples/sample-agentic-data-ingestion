// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { Pipeline } from '../../types';

/**
 * p30 task 2.5 — a heal is indicated in the pipeline run list (R14.3).
 *
 * The run list is `ExecutionsPage`. Before this task a heal reached it only as
 * corruption: the self-healing outcome record carries no `status`, so
 * `execution_updater` derived RUNNING from it and a healed FAILED run showed as
 * "RUNNING → self_healing". The row now keeps its own status and gains a heal
 * badge whose state is not collapsed.
 *
 * Both sides of the heal are covered: the ORIGINAL run's row (the heal
 * indicator) and the heal RE-RUN's own row (its provenance badge and link back
 * to the run it re-runs).
 *
 * The badge label/colour mapping is unit-tested in
 * `src/utils/__tests__/self-healing.test.ts`; this file covers what the table
 * renders. The pre-existing retry badge and source link are asserted here only
 * to pin that they still work — their own tests are in
 * `ExecutionsPage.retry.test.tsx`.
 *
 * Validates: Requirements 14.3
 */

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

interface ExecRow {
  execution_id: string;
  run_id: string;
  status: string;
  started_at: string;
  file_name?: string;
  trigger?: string;
  original_run_id?: string;
  retry_source_run_id?: string;
  self_healing_status?: string;
  self_healing_remediation?: string;
  self_healing_linked_run_id?: string;
}

let mockExecutions: ExecRow[] = [];

vi.mock('../../hooks', () => ({
  usePipelineExecutions: () => ({
    data: {
      executions: mockExecutions,
      queued_files: [],
      // The wire's nested envelope (p48 task 5.1) — see `ExecutionsPage.test.tsx`
      // for why the previous flat spelling was a fiction no producer ever emitted.
      // A single page, so no pager renders.
      pagination: {
        page: 1,
        page_size: 10,
        total_count: mockExecutions.length,
        total_pages: 1,
        has_next: false,
        has_prev: false,
        next_cursor: null,
        partial_counts: [],
      },
    },
  }),
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

function renderWith(executions: ExecRow[]) {
  mockExecutions = executions;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter>
          <PipelineProvider
            value={{ pipeline: mockPipeline, isLoading: false, isError: false, refetch: vi.fn() }}
          >
            <ExecutionsPage />
          </PipelineProvider>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

/** The original failed run, with a heal recorded against it. */
function healedRun(overrides: Partial<ExecRow> = {}): ExecRow {
  return {
    execution_id: 'e1',
    run_id: 'run-original',
    status: 'FAILED',
    started_at: '2026-08-02T10:00:00Z',
    file_name: 'orders.csv',
    self_healing_status: 'running',
    self_healing_remediation: 'Upgraded worker type from G.1X to G.2X',
    ...overrides,
  };
}

/** The heal re-run, which file_validation gave a fresh run_id. */
function healRerun(overrides: Partial<ExecRow> = {}): ExecRow {
  return {
    execution_id: 'e2',
    run_id: 'run-fresh',
    status: 'RUNNING',
    started_at: '2026-08-02T10:05:00Z',
    file_name: 'orders.csv',
    trigger: 'self_healing',
    original_run_id: 'run-original',
    ...overrides,
  };
}

describe('ExecutionsPage — heal indicator on the healed run', () => {
  it('shows no heal badge on a run no heal touched', () => {
    renderWith([{ execution_id: 'e0', run_id: 'r0', status: 'SUCCEEDED', started_at: '2026-08-02T09:00:00Z' }]);
    expect(screen.queryByTestId('self-healing-badge')).not.toBeInTheDocument();
  });

  it('shows one heal badge on the run that was healed', () => {
    renderWith([healedRun()]);
    expect(screen.getAllByTestId('self-healing-badge')).toHaveLength(1);
  });

  it('keeps the run\u2019s own status — the regression the badge replaced', () => {
    // The heal used to flip this row to RUNNING with current_step self_healing.
    renderWith([healedRun()]);
    expect(screen.getByText('FAILED')).toBeInTheDocument();
    expect(screen.queryByText(/self_healing/)).not.toBeInTheDocument();
  });

  it.each([
    ['running', 'Self-heal re-running'],
    ['success', 'Self-healed'],
    ['failed', 'Self-heal re-run failed'],
    ['start_failed', 'Self-heal re-run never started'],
    ['skipped_partial_write_guard', 'Self-heal declined'],
  ])('renders %s as its own label, not a shared one', (status, label) => {
    renderWith([healedRun({ self_healing_status: status })]);
    expect(screen.getByTestId('self-healing-badge')).toHaveTextContent(label);
  });

  it('only the succeeded re-run reads as healed', () => {
    for (const status of ['running', 'failed', 'start_failed', 'skipped_partial_write_guard']) {
      const { unmount } = renderWith([healedRun({ self_healing_status: status })]);
      expect(screen.getByTestId('self-healing-badge')).not.toHaveTextContent('Self-healed');
      unmount();
    }
  });

  it('carries the remediation in the hover text', () => {
    renderWith([healedRun()]);
    const badge = screen.getByTestId('self-healing-badge');
    expect(badge.parentElement).toHaveAttribute(
      'title',
      expect.stringContaining('Upgraded worker type from G.1X to G.2X'),
    );
  });
});

describe('ExecutionsPage — link to the heal re-run', () => {
  it('offers no link while the re-run is in flight', () => {
    // `rerun_id` is `{original}-heal`, which is lineage only and not navigable.
    renderWith([healedRun({ self_healing_status: 'running' })]);
    expect(screen.queryByTestId('self-healing-rerun-link')).not.toBeInTheDocument();
  });

  it('links to the re-run\u2019s real run id once it is known', () => {
    renderWith([
      healedRun({ self_healing_status: 'success', self_healing_linked_run_id: 'run-fresh' }),
    ]);
    const link = screen.getByTestId('self-healing-rerun-link');
    expect(link).toHaveAttribute('href', '/pipelines/test-pipe/runs/run-fresh');
    expect(screen.getByText('Heal re-run: run-fresh')).toBeInTheDocument();
  });
});

describe('ExecutionsPage — the heal re-run is identifiable as one', () => {
  it('badges a self-healing-triggered run', () => {
    renderWith([healRerun()]);
    expect(screen.getAllByTestId('self-healing-rerun-badge')).toHaveLength(1);
    expect(screen.queryByTestId('retry-badge')).not.toBeInTheDocument();
  });

  it('links back to the run it re-runs', () => {
    renderWith([healRerun()]);
    const link = screen.getByTestId('source-run-link');
    expect(link).toHaveAttribute('href', '/pipelines/test-pipe/runs/run-original');
    expect(screen.getByText('Source: run-original')).toBeInTheDocument();
  });

  it('does not badge an ordinary run as a heal re-run', () => {
    renderWith([healRerun({ trigger: 's3_upload', original_run_id: undefined })]);
    expect(screen.queryByTestId('self-healing-rerun-badge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('source-run-link')).not.toBeInTheDocument();
  });

  it('keeps the dashboard retry path\u2019s own badge and source link', () => {
    // retry_source_run_id is the retry path's spelling; it writes its own row.
    renderWith([
      healRerun({ trigger: 'retry', original_run_id: undefined, retry_source_run_id: 'run-original' }),
    ]);
    expect(screen.getAllByTestId('retry-badge')).toHaveLength(1);
    expect(screen.queryByTestId('self-healing-rerun-badge')).not.toBeInTheDocument();
    expect(screen.getByText('Source: run-original')).toBeInTheDocument();
  });

  it('shows both sides of a heal in one list', () => {
    renderWith([healedRun({ self_healing_linked_run_id: 'run-fresh' }), healRerun()]);
    expect(screen.getAllByTestId('self-healing-badge')).toHaveLength(1);
    expect(screen.getAllByTestId('self-healing-rerun-badge')).toHaveLength(1);
    expect(screen.getAllByTestId('self-healing-rerun-link')).toHaveLength(1);
    expect(screen.getAllByTestId('source-run-link')).toHaveLength(1);
  });
});
