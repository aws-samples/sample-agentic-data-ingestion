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
 * p30 task 4.9 — Skipped_Run_Records in the run list, distinct from failures (R10.2).
 *
 * Two things are asserted here that a unit test of `describeSkip` cannot reach:
 *
 * 1. **A SKIPPED row renders as SKIPPED end to end.** Task 2.5 found a non-run
 *    row deriving RUNNING because it carried no `status`, so "the row is written
 *    correctly" is not evidence that it displays correctly. `statusType` has no
 *    `skipped` arm — the row relies on its `default: 'info'` — so this is the
 *    only place that pins it.
 * 2. **A skip does not render like a failure.** They must not share a status
 *    indicator type, which is what R10.2 literally asks for.
 *
 * The reason table's labels and colours are unit-tested in
 * `src/utils/__tests__/skip-reason.test.ts`; the pre-existing retry and
 * self-healing badges are asserted only to pin that a skip row does not disturb
 * them.
 *
 * Validates: Requirements 10.2
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
  current_step?: string;
  skip_reason?: string;
  skip_detail?: string;
  skip_count?: number;
  self_healing_status?: string;
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

/** A contention Skipped_Run_Record as pipeline_trigger writes it. */
function contentionSkip(overrides: Partial<ExecRow> = {}): ExecRow {
  return {
    execution_id: 's1',
    run_id: 'run-2026-08-02-14-00-00-skip-global_concurrency',
    status: 'SKIPPED',
    started_at: '2026-08-02T14:03:00Z',
    skip_reason: 'global_concurrency',
    skip_detail:
      'Global concurrency limit reached: at least 11 running of a maximum 10, observed when the trigger was checked. Scheduled trigger skipped.',
    skip_count: 1,
    current_step: 'concurrency_gate',
    ...overrides,
  };
}

function failedRun(overrides: Partial<ExecRow> = {}): ExecRow {
  return {
    execution_id: 'f1',
    run_id: 'run-2026-08-02-13-00-00-a1b2c3d4',
    status: 'FAILED',
    started_at: '2026-08-02T13:00:00Z',
    file_name: 'orders.csv',
    ...overrides,
  };
}

describe('ExecutionsPage — a SKIPPED row reaches the run list', () => {
  it('renders the row with its own SKIPPED status, not a derived one', () => {
    renderWith([contentionSkip()]);
    expect(screen.getByText('SKIPPED')).toBeInTheDocument();
    expect(screen.queryByText('RUNNING')).not.toBeInTheDocument();
  });

  it('does not use the failure status indicator', () => {
    // Cloudscape encodes the StatusIndicator type in the class name
    // (`awsui_status-error_…` / `awsui_status-info_…`), which is the only place
    // the visual distinction R10.2 asks for is observable from the DOM.
    const { container } = renderWith([contentionSkip()]);
    expect(container.querySelectorAll('[class*="status-error"]')).toHaveLength(0);
    expect(container.querySelectorAll('[class*="status-info"]').length).toBeGreaterThan(0);
  });

  it('renders a skip and a failure with different status indicators', () => {
    const { container } = renderWith([contentionSkip(), failedRun()]);
    // Exactly one of the two rows reads as an error, and it is the failure.
    expect(container.querySelectorAll('[class*="status-error"]')).toHaveLength(1);
    expect(container.querySelectorAll('[class*="status-info"]')).toHaveLength(1);
    expect(screen.getByText('FAILED')).toBeInTheDocument();
    expect(screen.getByText('SKIPPED')).toBeInTheDocument();
  });
});

describe('ExecutionsPage — the reason, which is what R10.2 was missing', () => {
  it('shows a reason badge on a skip row', () => {
    renderWith([contentionSkip()]);
    expect(screen.getByTestId('skip-reason-badge')).toHaveTextContent('Skipped: at concurrency limit');
  });

  it('shows no reason badge on a failure', () => {
    renderWith([failedRun()]);
    expect(screen.queryByTestId('skip-reason-badge')).not.toBeInTheDocument();
  });

  it('shows no reason badge on a successful run', () => {
    renderWith([{ execution_id: 'ok', run_id: 'run-ok', status: 'SUCCEEDED', started_at: '2026-08-02T12:00:00Z' }]);
    expect(screen.queryByTestId('skip-reason-badge')).not.toBeInTheDocument();
  });

  it('distinguishes a lock-contention skip from a concurrency skip', () => {
    renderWith([
      contentionSkip(),
      contentionSkip({
        execution_id: 's2',
        run_id: 'run-2026-08-02-14-00-00-skip-pipeline_lock_contention',
        skip_reason: 'pipeline_lock_contention',
        skip_detail: "Pipeline 'orders' already has a running execution. Scheduled trigger skipped.",
      }),
    ]);
    const labels = screen.getAllByTestId('skip-reason-badge').map((n) => n.textContent);
    expect(new Set(labels).size).toBe(2);
  });

  it('distinguishes a duplicate-content skip from a contention skip', () => {
    renderWith([
      contentionSkip({
        execution_id: 's3',
        run_id: 'skipped-abc123',
        skip_reason: 'duplicate_content',
        skip_detail: "Identical content was already processed successfully by run 'run-old'.",
        current_step: 'duplicate_check',
      }),
    ]);
    expect(screen.getByTestId('skip-reason-badge')).toHaveTextContent('Skipped: duplicate content');
  });

  it('carries the recorded detail as hover text so the numbers are reachable', () => {
    renderWith([contentionSkip()]);
    const badge = screen.getByTestId('skip-reason-badge');
    expect(badge.closest('span[title]')?.getAttribute('title')).toContain(
      'at least 11 running of a maximum 10',
    );
  });
});

describe('ExecutionsPage — how many fires one row stands for', () => {
  it('shows a count badge when skips collapsed into the row', () => {
    renderWith([contentionSkip({ skip_count: 12 })]);
    expect(screen.getByTestId('skip-count-badge')).toHaveTextContent('12');
  });

  it('shows no count badge for a single skip', () => {
    renderWith([contentionSkip({ skip_count: 1 })]);
    expect(screen.queryByTestId('skip-count-badge')).not.toBeInTheDocument();
  });

  it('shows no count badge when the row predates the counter', () => {
    renderWith([contentionSkip({ skip_count: undefined })]);
    expect(screen.queryByTestId('skip-count-badge')).not.toBeInTheDocument();
  });

  it('shows no count badge on a non-skip row that happens to carry a count', () => {
    renderWith([failedRun({ skip_count: 9 })]);
    expect(screen.queryByTestId('skip-count-badge')).not.toBeInTheDocument();
  });
});
