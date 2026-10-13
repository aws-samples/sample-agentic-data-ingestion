// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The run list shows what reached the target (p37 task 6.6, R1.7/R1.9).
 *
 * **Validates: Requirements 1.7, 1.9**
 *
 * `LoadOutcomeIndicator`'s own mapping is unit-tested in
 * `components/__tests__/LoadOutcomeIndicator.test.tsx`. What lives here is the
 * half a component test cannot show: that the indicator sits **in the same
 * status cell as the run's own status indicator**, beside it and not instead of
 * it.
 *
 * That is design D1, and it is the whole point of R1.9. A `partial_load` run
 * keeps its `SUCCEEDED` status — making it a non-success status is the cry-wolf
 * failure — and the withheld count is always visible, at any N, with no
 * threshold. Asserting the count exists somewhere on the page would not
 * establish either half; asserting both are in one cell does.
 *
 * R1.7's distinctness is asserted in **both directions** at page level:
 * `no_rows_loaded` puts a warning in the cell, and `complete_load` puts nothing
 * there while the run still reads `SUCCEEDED`. One direction alone would pass
 * for an indicator that warned about every run.
 *
 * `not_established` and an absent `load_outcome` add nothing to the cell at all.
 * It is a reachable steady state, not a migration artefact (design §5 risk 3):
 * the Glue job's counts write is best-effort by necessity, so any run whose
 * telemetry write failed lands here, as does every run that completed before the
 * counts shipped.
 *
 * Shaped after `ExecutionsPage.self-healing.test.tsx`, which covers the
 * neighbouring badges in the same cell.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { LoadOutcome, LoadOutcomeKind, Pipeline } from '../../types';

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
  load_outcome?: LoadOutcome;
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

function outcome(over: Partial<LoadOutcome> & { outcome: LoadOutcomeKind }): LoadOutcome {
  return {
    rows_read: 0,
    rows_written: 0,
    rows_not_written: 0,
    blocks_run: false,
    missing: [],
    determined_from: 'load_metrics_row',
    reason: '',
    ...over,
  };
}

/**
 * A run reported SUCCEEDED, which is the whole defect: this is the live run
 * `run-2026-08-09-15-38-19-5c5f5f8a`'s shape, whose Step Functions history
 * carries no `TaskFailed` or `ExecutionFailed` event anywhere.
 */
function succeededRun(load_outcome?: LoadOutcome): ExecRow {
  return {
    execution_id: 'e1',
    run_id: 'run-1',
    status: 'SUCCEEDED',
    started_at: '2026-08-09T15:38:15Z',
    file_name: 'customer_data_5.csv',
    load_outcome,
  };
}

/**
 * The cell the indicator was mounted into. Cloudscape renders a real table, so
 * the enclosing `<td>` is the cell; the `SpaceBetween` wrapper is the fallback
 * for the same claim if that ever stops being true.
 */
function cellContaining(testId: string): HTMLElement {
  const indicator = screen.getByTestId(testId);
  const cell = indicator.closest('td') ?? indicator.parentElement;
  expect(cell).not.toBeNull();
  return cell as HTMLElement;
}

describe('ExecutionsPage — partial_load shows the success indicator AND the count (R1.9, D1)', () => {
  it('keeps the run reading SUCCEEDED', () => {
    renderWith([
      succeededRun(
        outcome({
          outcome: 'partial_load',
          rows_read: 1200,
          rows_written: 1197,
          rows_not_written: 3,
        }),
      ),
    ]);

    expect(screen.getByText('SUCCEEDED')).toBeInTheDocument();
  });

  it('shows the withheld count in the same cell as that success indicator', () => {
    renderWith([
      succeededRun(
        outcome({
          outcome: 'partial_load',
          rows_read: 1200,
          rows_written: 1197,
          rows_not_written: 3,
        }),
      ),
    ]);

    const cell = cellContaining('load-outcome-partial-load');
    // Both facts, one cell: the status stays a success and the count is visible
    // beside it. That is what a component-level test cannot establish.
    expect(cell).toContainElement(screen.getByText('SUCCEEDED'));
    expect(cell.textContent).toContain('3 rows not written');
  });

  it('shows the count at N = 1 — there is no threshold anywhere in this spec', () => {
    renderWith([
      succeededRun(
        outcome({ outcome: 'partial_load', rows_read: 1000, rows_written: 999, rows_not_written: 1 }),
      ),
    ]);

    expect(cellContaining('load-outcome-partial-load').textContent).toContain('1 row not written');
  });

  it('does not replace the status indicator with the load outcome', () => {
    renderWith([
      succeededRun(
        outcome({ outcome: 'partial_load', rows_read: 10, rows_written: 8, rows_not_written: 2 }),
      ),
    ]);

    const cell = cellContaining('load-outcome-partial-load');
    expect(cell.textContent).toContain('SUCCEEDED');
    expect(cell.textContent).toContain('2 rows not written');
  });
});

describe('ExecutionsPage — no_rows_loaded is distinct from complete_load (R1.7)', () => {
  it('puts a warning in the status cell of a run that loaded nothing', () => {
    renderWith([
      succeededRun(
        outcome({
          outcome: 'no_rows_loaded',
          rows_read: 5,
          rows_written: 0,
          rows_not_written: 5,
          reason:
            'This run read 5 row(s) and wrote none to the target. Every row was withheld — ' +
            'quarantined by a data quality rule, or dropped by the transform.',
        }),
      ),
    ]);

    const cell = cellContaining('load-outcome-no-rows-loaded');
    expect(cell.textContent).toContain('0 rows loaded');
    // The run still reports SUCCEEDED — this spec does not re-decide the run
    // (R1.12); it makes the zero visible next to it.
    expect(cell).toContainElement(screen.getByText('SUCCEEDED'));
  });

  it('puts nothing in the status cell of a run that loaded everything', () => {
    renderWith([
      succeededRun(outcome({ outcome: 'complete_load', rows_read: 1200, rows_written: 1200 })),
    ]);

    expect(screen.queryByTestId('load-outcome-no-rows-loaded')).not.toBeInTheDocument();
    expect(screen.queryByTestId('load-outcome-partial-load')).not.toBeInTheDocument();
    expect(screen.queryByTestId('load-outcome-empty-source')).not.toBeInTheDocument();
    // The run's own success indicator is untouched and still says it.
    expect(screen.getByText('SUCCEEDED')).toBeInTheDocument();
  });

  it('shows both runs distinctly in one list', () => {
    renderWith([
      succeededRun(
        outcome({ outcome: 'no_rows_loaded', rows_read: 5, rows_written: 0, rows_not_written: 5 }),
      ),
      {
        ...succeededRun(outcome({ outcome: 'complete_load', rows_read: 9, rows_written: 9 })),
        execution_id: 'e2',
        run_id: 'run-2',
      },
    ]);

    expect(screen.getAllByTestId('load-outcome-no-rows-loaded')).toHaveLength(1);
    expect(screen.getAllByText('SUCCEEDED')).toHaveLength(2);
  });
});

describe('ExecutionsPage — not_established adds nothing to the cell', () => {
  it('renders no load-outcome indicator for a run with no counts row', () => {
    renderWith([
      succeededRun({
        outcome: 'not_established',
        rows_read: null,
        rows_written: null,
        rows_not_written: 0,
        blocks_run: false,
        missing: ['rows_read', 'rows_written'],
        determined_from: 'load_metrics_row_absent',
        reason:
          'How many rows this run read and wrote was not established ' +
          '(load_metrics_row_absent), so what reached the target cannot be stated. ' +
          'This is not a claim that nothing loaded.',
      }),
    ]);

    for (const testId of [
      'load-outcome-no-rows-loaded',
      'load-outcome-partial-load',
      'load-outcome-empty-source',
    ]) {
      expect(screen.queryByTestId(testId)).not.toBeInTheDocument();
    }
    expect(screen.getByText('SUCCEEDED')).toBeInTheDocument();
  });

  it('renders nothing for a row carrying no load_outcome at all', () => {
    // Every fixture and cached response that predates p37. Identical treatment
    // to `not_established` — both mean "we do not know".
    renderWith([succeededRun(undefined)]);

    for (const testId of [
      'load-outcome-no-rows-loaded',
      'load-outcome-partial-load',
      'load-outcome-empty-source',
    ]) {
      expect(screen.queryByTestId(testId)).not.toBeInTheDocument();
    }
    expect(screen.getByText('SUCCEEDED')).toBeInTheDocument();
  });
});

describe('ExecutionsPage — empty_source is stated without reading as loss', () => {
  it('states it in the cell beside the success indicator', () => {
    renderWith([succeededRun(outcome({ outcome: 'empty_source' }))]);

    const cell = cellContaining('load-outcome-empty-source');
    expect(cell.textContent).toContain('Empty source');
    expect(cell.textContent).not.toContain('0 rows loaded');
    expect(cell).toContainElement(screen.getByText('SUCCEEDED'));
  });
});
