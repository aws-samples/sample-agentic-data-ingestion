// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Component tests for `PipelineOverviewPage` (P17 Task 21.3).
 *
 * Scope (per Task 21.3 — _Requirements: 22.1_):
 *   1. **Run Now button state** — rendered alongside the other action
 *      buttons, wired up to `apiClient.post('/pipelines/{id}/run', {})`,
 *      surfaces success / 429 / 504 toast feedback. The dedicated
 *      `RunNowButton` unit-test file covers all toast paths in
 *      isolation; here we only assert the **integration** — that the
 *      page renders the button and clicking it dispatches the POST.
 *   2. **Next-run-time computation** — the `pipeline-schedule-block`
 *      only renders when `pipeline.schedule` is set, shows the raw
 *      expression plus a parsed "Next run: ..." line for valid cron /
 *      rate expressions, and falls back to a warning message for an
 *      unparseable expression.
 *
 * The cron utility itself is covered exhaustively in
 * `src/utils/__tests__/cron-utils.test.ts`; we stay at the component
 * boundary and use a fixed system clock so the rendered time is stable.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

vi.mock('../../hooks', () => ({
  useArchivePipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  usePipelineExecutions: () => ({ data: { executions: [] } }),
  // p51 task 4.1 — the awaiting-approval set now comes from the keyed approvals
  // read. Empty here; its own behaviour is covered in
  // PipelineOverviewPage.approvals-scope.test.tsx.
  usePipelinePendingApprovals: () => ({ data: [] }),
  useDataPreview: () => ({ data: undefined, isLoading: false, isError: false, error: null }),
  // P30 R3.2 — the overview now renders PipelineCostPanel, which reads this
  // hook. Its own states are covered in PipelineCostPanel.async.test.tsx.
  usePipelineCosts: () => ({ data: undefined, isLoading: true, isError: false, refetch: vi.fn() }),
  useBedrockModels: () => ({ data: [] }),
  useCreatePipeline: () => ({ mutateAsync: vi.fn() }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useSchemas: () => ({ data: [], isLoading: false }),
  useGlueDatabases: () => ({ data: [], isLoading: false }),
  useTablesInDatabase: () => ({ data: [], isLoading: false }),
  useS3TablesBuckets: () => ({ data: [], isLoading: false }),
  useTablesInBucket: () => ({ data: [], isLoading: false }),
}));

const showToastMock = vi.fn();
vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: showToastMock }),
}));

vi.mock('../../services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api-client')>();
  return {
    ...actual,
    apiClient: {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
    },
  };
});

import { apiClient } from '../../services/api-client';
import PipelineOverviewPage from '../PipelineOverviewPage';

const mockedPost = vi.mocked(apiClient.post);

const mockPipeline: Pipeline = {
  pipeline_key: 'test-pipe',
  pipeline_name: 'Test Pipeline',
  status: 'active',
  source_schema: 'orders',
  target_schema: 'orders_target',
  created_at: '2024-01-15T10:00:00Z',
  updated_at: '2024-06-01T00:00:00Z',
  ai_model: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
  schema_history: [],
  all_files: [],
};

function renderPage(pipeline: Pipeline = mockPipeline) {
  return render(
    <MemoryRouter>
      <PipelineProvider value={{ pipeline, isLoading: false, isError: false, refetch: vi.fn() }}>
        <PipelineOverviewPage />
      </PipelineProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('PipelineOverviewPage', () => {
  it('renders pipeline status', () => {
    renderPage();
    expect(screen.getByTestId('pipeline-status')).toHaveTextContent('active');
  });

  it('renders source and target schema', () => {
    renderPage();
    expect(screen.getByText('orders')).toBeInTheDocument();
    expect(screen.getByText('orders_target')).toBeInTheDocument();
  });

  it('renders action buttons', () => {
    renderPage();
    expect(screen.getByRole('button', { name: /Upload File/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Edit Pipeline/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Archive Pipeline/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Refresh/ })).toBeInTheDocument();
  });

  it('renders AI model display name', () => {
    renderPage();
    expect(screen.getByTestId('pipeline-model')).toBeInTheDocument();
  });
});

/* -------------------------------------------------------------------- */
/* P17 Task 21.3 — Run Now button integration                           */
/* -------------------------------------------------------------------- */

describe('PipelineOverviewPage — Run Now button (P17 Task 21.3)', () => {
  it('renders the Run Now button enabled by default', () => {
    renderPage();
    const button = screen.getByTestId('run-now-button');
    expect(button).toBeInTheDocument();
    // Cloudscape Buttons render as <button>; assert it's not disabled.
    expect(button).not.toBeDisabled();
  });

  it('clicking Run Now POSTs to /pipelines/{id}/run with an empty body and shows the success toast', async () => {
    mockedPost.mockResolvedValue({ execution_arn: 'arn:aws:states:...' });

    renderPage();
    fireEvent.click(screen.getByTestId('run-now-button'));

    await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1));
    expect(mockedPost).toHaveBeenCalledWith('/pipelines/test-pipe/run', {});
    await waitFor(() =>
      expect(showToastMock).toHaveBeenCalledWith('Pipeline run triggered', 'success'),
    );
  });

  it('shows the rate-limit toast when the trigger endpoint returns HTTP 429', async () => {
    const { ApiError } = await import('../../services/api-client');
    mockedPost.mockRejectedValue(new ApiError(429, { message: 'too many concurrent runs' }));

    renderPage();
    fireEvent.click(screen.getByTestId('run-now-button'));

    await waitFor(() =>
      expect(showToastMock).toHaveBeenCalledWith('Concurrency limit reached', 'warning'),
    );
  });

  it('shows the timeout toast when the trigger endpoint returns HTTP 504', async () => {
    const { ApiError } = await import('../../services/api-client');
    mockedPost.mockRejectedValue(new ApiError(504, { message: 'gateway timeout' }));

    renderPage();
    fireEvent.click(screen.getByTestId('run-now-button'));

    await waitFor(() =>
      expect(showToastMock).toHaveBeenCalledWith('Trigger Lambda timed out', 'error'),
    );
  });
});

/* -------------------------------------------------------------------- */
/* P17 Task 21.3 — schedule / next-run computation                      */
/* -------------------------------------------------------------------- */

describe('PipelineOverviewPage — schedule block (P17 Task 21.3)', () => {
  // Pin the wall clock so the formatted next-run time is stable across
  // machines. EventBridge cron expressions are evaluated in UTC, so we
  // anchor `now` to a UTC instant; the rendered string still depends on
  // the test runner's locale/TZ, hence we assert with regex matchers
  // rather than exact text.
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2024-06-15T10:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('does NOT render the schedule block when pipeline.schedule is undefined', () => {
    renderPage();
    expect(screen.queryByTestId('pipeline-schedule-block')).not.toBeInTheDocument();
    expect(screen.queryByTestId('pipeline-next-run')).not.toBeInTheDocument();
  });

  it('does NOT render the schedule block when pipeline.schedule is an empty string', () => {
    renderPage({ ...mockPipeline, schedule: '' });
    expect(screen.queryByTestId('pipeline-schedule-block')).not.toBeInTheDocument();
  });

  it('renders the schedule expression and a "Next run" line for a valid rate(...) expression', () => {
    renderPage({ ...mockPipeline, schedule: 'rate(1 hour)' });

    const block = screen.getByTestId('pipeline-schedule-block');
    expect(block).toBeInTheDocument();
    expect(block).toHaveTextContent('rate(1 hour)');

    const nextRun = screen.getByTestId('pipeline-next-run');
    // `formatNextRun` includes "Next run: " prefix and a 4-digit year.
    expect(nextRun).toHaveTextContent(/Next run:/);
    expect(nextRun).toHaveTextContent(/2024/);
    expect(nextRun).not.toHaveTextContent(/Unable to parse/);
  });

  it('renders the schedule expression and a "Next run" line for a valid cron(...) expression', () => {
    renderPage({ ...mockPipeline, schedule: 'cron(0 2 * * ? *)' });

    const block = screen.getByTestId('pipeline-schedule-block');
    expect(block).toBeInTheDocument();
    expect(block).toHaveTextContent('cron(0 2 * * ? *)');

    const nextRun = screen.getByTestId('pipeline-next-run');
    expect(nextRun).toHaveTextContent(/Next run:/);
    expect(nextRun).toHaveTextContent(/2024/);
  });

  it('renders the warning fallback when the schedule expression is unparseable', () => {
    renderPage({ ...mockPipeline, schedule: 'every hour or so' });

    const block = screen.getByTestId('pipeline-schedule-block');
    expect(block).toBeInTheDocument();
    // The raw (unparseable) expression is still surfaced so operators
    // can see the source-of-truth value EventBridge was given.
    expect(block).toHaveTextContent('every hour or so');

    const nextRun = screen.getByTestId('pipeline-next-run');
    expect(nextRun).toHaveTextContent('Unable to parse schedule expression');
    expect(nextRun).not.toHaveTextContent(/Next run:/);
  });
});
