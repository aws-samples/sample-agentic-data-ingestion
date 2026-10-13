// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The run detail page states what reached the target (p37 task 6.6, R1.8).
 *
 * **Validates: Requirements 1.8**
 *
 * `LoadOutcomeLabel`'s wording is unit-tested in
 * `components/__tests__/LoadOutcomeLabel.test.tsx`. What lives here is the mount
 * decision task 6.5 made, which no component test can express: the label sits
 * **above the tabs**, not inside `DataQualityTab`.
 *
 * The reason is that the common `no_rows_loaded` run has **no DQ report at
 * all**. `quarantine_path` exists only for the inline-DQ case —
 * `glue_job_s3_tables.py.j2` is the only DQ-bearing template of the five, and
 * only with a non-empty ruleset — so a run that lost every row to a transform
 * filter would render this label behind a tab whose content reads "No data
 * quality results available for this run". A label an operator cannot reach does
 * not satisfy R1.8.
 *
 * So the first assertion below is the load-bearing one: `qualityReport` is
 * `null` and the label still renders on the landing tab.
 *
 * Shaped after `RunDetailPage.quality-check.test.tsx`; the hook mock list is
 * taken from it unchanged, because the page reads all of them.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { LoadOutcome, LoadOutcomeKind, RunDetail } from '../../types';
import type { QualityReport } from '../../hooks/use-quality-report';

type StateRow = QualityReport & Record<string, unknown>;

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

const NO_ROWS_LOADED = outcome({
  outcome: 'no_rows_loaded',
  rows_read: 5,
  rows_written: 0,
  rows_not_written: 5,
  reason:
    'This run read 5 row(s) and wrote none to the target. Every row was withheld — ' +
    'quarantined by a data quality rule, or dropped by the transform.',
});

const NOT_ESTABLISHED: LoadOutcome = {
  outcome: 'not_established',
  rows_read: null,
  rows_written: null,
  rows_not_written: 0,
  blocks_run: false,
  missing: ['rows_read', 'rows_written'],
  determined_from: 'load_metrics_row_absent',
  reason:
    'How many rows this run read and wrote was not established (load_metrics_row_absent), ' +
    'so what reached the target cannot be stated. This is not a claim that nothing loaded.',
};

/** Inline producer row carrying the quarantine locator. */
const INLINE_WITH_PREFIX: StateRow = {
  pipeline_id: 'pipe-1',
  run_id: 'run-1',
  step_name: 'data_quality',
  overall_status: 'fail',
  checks: [{ name: 'Completeness "phone" >= 0.95', status: 'fail', actual: 0 }],
  root_cause: 'data_issue',
  recommendations: ['Review failed data quality rules'],
  source: 'glue_inline_dq',
  rules_passed: 10,
  rules_failed: 1,
  rules_total: 11,
  rows_passed: 0,
  rows_quarantined: 5,
  quarantine_path: 's3://adi-quarantine/quarantine/run-1/',
};

function run(load_outcome?: LoadOutcome): RunDetail {
  return {
    run_id: 'run-1',
    pipeline_key: 'pipe-1',
    status: 'SUCCEEDED',
    started_at: '2026-08-09T15:38:15Z',
    completed_at: '2026-08-09T15:44:02Z',
    steps: [{ step_name: 'file_validation', status: 'completed' }],
    load_outcome,
  };
}

let mockRunDetail: RunDetail = run(NO_ROWS_LOADED);
let mockQualityReport: QualityReport | null = null;

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../hooks', () => ({
  useRunDetail: () => ({
    data: mockRunDetail,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
    isWebSocketConnected: false,
  }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRunTraces: () => ({ data: null, isLoading: false }),
  useSelfHealingActivity: () => ({ data: undefined }),
  useQualityReport: () => ({ data: mockQualityReport, isError: false, refetch: vi.fn() }),
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
      <MemoryRouter initialEntries={['/pipelines/pipe-1/runs/run-1']}>
        <Routes>
          <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockRunDetail = run(NO_ROWS_LOADED);
  mockQualityReport = null;
});

describe('RunDetailPage — the label is reachable without a DQ report (R1.8)', () => {
  it('renders on the landing tab for a run with no quality report at all', () => {
    // The common case, and the reason the label is not inside DataQualityTab.
    renderPage();

    const label = screen.getByTestId('load-outcome-label');
    expect(label.textContent).toMatch(/no rows reached the target/i);
    // Nothing was clicked, and the Data Quality tab's content is not mounted.
    expect(screen.queryByTestId('quality-report-card')).not.toBeInTheDocument();
    expect(
      screen.queryByText('No data quality results available for this run.'),
    ).not.toBeInTheDocument();
  });

  it('states both counts and says no location was recorded', () => {
    renderPage();

    expect(screen.getByTestId('load-outcome-counts').textContent).toContain('read 5 rows');
    expect(screen.getByTestId('load-outcome-counts').textContent).toContain('wrote 0 rows');
    expect(screen.getByTestId('load-outcome-withheld').textContent).toMatch(
      /dropped by the transform rather than quarantined/i,
    );
  });

  it('names the quarantine prefix when the run does have an inline DQ report', () => {
    // The report is wired through from `useQualityReport` for the locator only.
    mockQualityReport = INLINE_WITH_PREFIX;
    renderPage();

    expect(screen.getByTestId('load-outcome-withheld').textContent).toContain(
      's3://adi-quarantine/quarantine/run-1/',
    );
  });

  it('stays on the page when the Data Quality tab is opened, and does not duplicate', () => {
    mockQualityReport = INLINE_WITH_PREFIX;
    renderPage();

    fireEvent.click(screen.getByText('Data Quality'));

    // One load label, above the tabs; the quality label is the tab's own and is
    // a separate surface (R4.5).
    expect(screen.getAllByTestId('load-outcome-label')).toHaveLength(1);
    expect(screen.getByTestId('quality-report-card')).toContainElement(
      screen.getByTestId('quality-verdict-label'),
    );
    expect(screen.getByTestId('quality-report-card')).not.toContainElement(
      screen.getByTestId('load-outcome-label'),
    );
  });
});

describe('RunDetailPage — not_established renders nothing', () => {
  it('renders no label for a run with no counts row', () => {
    mockRunDetail = run(NOT_ESTABLISHED);
    renderPage();

    expect(screen.queryByTestId('load-outcome-label')).not.toBeInTheDocument();
    expect(screen.queryByTestId('load-outcome-counts')).not.toBeInTheDocument();
  });

  it('renders no label for a response carrying no load_outcome at all', () => {
    mockRunDetail = run(undefined);
    renderPage();

    expect(screen.queryByTestId('load-outcome-label')).not.toBeInTheDocument();
  });

  it('renders no label even when a DQ report exists for the run', () => {
    // The label is derived from the Load_Outcome, never from the DQ report.
    mockRunDetail = run(NOT_ESTABLISHED);
    mockQualityReport = INLINE_WITH_PREFIX;
    renderPage();

    expect(screen.queryByTestId('load-outcome-label')).not.toBeInTheDocument();
  });
});
