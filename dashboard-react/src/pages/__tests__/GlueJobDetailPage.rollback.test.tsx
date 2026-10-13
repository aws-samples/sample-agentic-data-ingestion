// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import GlueJobDetailPage from '../GlueJobDetailPage';
import type { GlueJobDetail, JobVersionHistoryResponse } from '../../types';

/* ------------------------------------------------------------------ */
/*  Mock state                                                         */
/* ------------------------------------------------------------------ */

let mockGlueJob: GlueJobDetail | undefined;
const mockRollbackMutateAsync = vi.fn();
let mockRollbackIsPending = false;
let mockVersionHistory: JobVersionHistoryResponse | undefined;
let mockVersionsLoading = false;

vi.mock('../../hooks', () => ({
  useGlueJob: () => ({
    data: mockGlueJob,
    isLoading: false,
    isError: false,
  }),
  useRunGlueJob: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useRollbackGlueJob: () => ({
    mutateAsync: mockRollbackMutateAsync,
    isPending: mockRollbackIsPending,
  }),
  useJobVersionHistory: () => ({
    data: mockVersionHistory,
    isLoading: mockVersionsLoading,
  }),
}));

/* ------------------------------------------------------------------ */
/*  Fixtures                                                           */
/* ------------------------------------------------------------------ */

const JOB_MULTI_VERSIONS: GlueJobDetail = {
  job_name: 'etl-job-alpha',
  status: 'READY',
  worker_type: 'G.1X',
  number_of_workers: 5,
  timeout: 120,
  script_location: 's3://bucket/scripts/etl.py',
  created_at: '2024-06-01T00:00:00Z',
  last_modified: '2024-06-10T00:00:00Z',
  runs: [],
  configuration: {},
  script_content: 'print("hello")',
  script_versions: [
    { version: 2, timestamp: '2024-06-10T00:00:00Z' },
    { version: 1, timestamp: '2024-06-01T00:00:00Z' },
  ],
};

const JOB_SINGLE_VERSION: GlueJobDetail = {
  ...JOB_MULTI_VERSIONS,
  script_versions: [
    { version: 1, timestamp: '2024-06-01T00:00:00Z' },
  ],
};

const VERSION_HISTORY: JobVersionHistoryResponse = {
  versions: [
    { version: 2, timestamp: '2024-06-10T12:00:00Z' },
    { version: 1, timestamp: '2024-06-01T08:00:00Z' },
  ],
  current_version: 2,
};

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function renderPage(job: GlueJobDetail = JOB_MULTI_VERSIONS) {
  mockGlueJob = job;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={[`/pipelines/test-pipe/glue-job/${job.job_name}`]}>
          <Routes>
            <Route path="/pipelines/:id/glue-job/:jobName" element={<GlueJobDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function switchToCodeTab() {
  const codeTab = screen.getByRole('tab', { name: /Code/i });
  fireEvent.click(codeTab);
}

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  mockRollbackIsPending = false;
  mockRollbackMutateAsync.mockReset();
  mockVersionHistory = VERSION_HISTORY;
  mockVersionsLoading = false;
});

describe('GlueJobDetailPage — Rollback button states', () => {
  it('shows enabled Rollback button when multiple script versions exist', () => {
    renderPage(JOB_MULTI_VERSIONS);
    switchToCodeTab();

    const btn = screen.getByTestId('rollback-button');
    expect(btn).toBeInTheDocument();
    expect(btn).not.toBeDisabled();
  });

  it('shows disabled Rollback button when only one script version exists', () => {
    renderPage(JOB_SINGLE_VERSION);
    switchToCodeTab();

    const btn = screen.getByTestId('rollback-button');
    expect(btn).toBeInTheDocument();
    // The button is wrapped in a Popover for the tooltip; it should be disabled
    expect(btn).toBeDisabled();
  });

  it('shows "No previous version available" tooltip text when button is disabled', () => {
    renderPage(JOB_SINGLE_VERSION);
    switchToCodeTab();

    // Click the disabled button area to trigger the popover
    const btn = screen.getByTestId('rollback-button');
    // The popover content should be in the DOM
    fireEvent.click(btn.closest('[data-testid="rollback-button"]')!.parentElement!);
    expect(screen.getByText('No previous version available')).toBeInTheDocument();
  });
});

describe('GlueJobDetailPage — Rollback confirmation modal', () => {
  it('opens modal when Rollback button is clicked', () => {
    renderPage(JOB_MULTI_VERSIONS);
    switchToCodeTab();

    fireEvent.click(screen.getByTestId('rollback-button'));

    // Modal should be visible with confirm and cancel buttons
    expect(screen.getByTestId('rollback-confirm')).toBeInTheDocument();
    expect(screen.getByTestId('rollback-cancel')).toBeInTheDocument();
    expect(screen.getByTestId('current-version-info')).toBeInTheDocument();
    expect(screen.getByTestId('previous-version-info')).toBeInTheDocument();
  });

  it('displays current and previous version info from version history', () => {
    renderPage(JOB_MULTI_VERSIONS);
    switchToCodeTab();
    fireEvent.click(screen.getByTestId('rollback-button'));

    const currentInfo = screen.getByTestId('current-version-info');
    expect(currentInfo.textContent).toContain('Version 2');

    const previousInfo = screen.getByTestId('previous-version-info');
    expect(previousInfo.textContent).toContain('Version 1');
  });

  it('shows loading spinner when version history is loading', () => {
    mockVersionsLoading = true;
    mockVersionHistory = undefined;
    renderPage(JOB_MULTI_VERSIONS);
    switchToCodeTab();
    fireEvent.click(screen.getByTestId('rollback-button'));

    expect(screen.getByText('Loading version history...')).toBeInTheDocument();
  });

  it('closes modal when Cancel is clicked', () => {
    renderPage(JOB_MULTI_VERSIONS);
    switchToCodeTab();
    fireEvent.click(screen.getByTestId('rollback-button'));

    // Modal should be open
    expect(screen.getByTestId('rollback-confirm')).toBeVisible();

    // Click cancel — find the button by test id
    const cancelBtn = screen.getByTestId('rollback-cancel');
    fireEvent.click(cancelBtn);

    // Cloudscape Modal keeps DOM nodes but hides the overlay.
    // Verify the modal-open class is removed from body.
    expect(document.body.classList.contains('awsui_modal-open_1d2i7_1e7ip_433')).toBe(false);
  });
});

describe('GlueJobDetailPage — Rollback success/error flows', () => {
  it('calls rollback mutation and shows success toast on confirm', async () => {
    mockRollbackMutateAsync.mockResolvedValueOnce(undefined);
    renderPage(JOB_MULTI_VERSIONS);
    switchToCodeTab();
    fireEvent.click(screen.getByTestId('rollback-button'));
    fireEvent.click(screen.getByTestId('rollback-confirm'));

    expect(mockRollbackMutateAsync).toHaveBeenCalledTimes(1);

    await waitFor(() => {
      expect(screen.getByText('Rollback completed successfully')).toBeInTheDocument();
    });
  });

  it('shows error toast with API message on rollback failure', async () => {
    mockRollbackMutateAsync.mockRejectedValueOnce(new Error('Version conflict detected'));
    renderPage(JOB_MULTI_VERSIONS);
    switchToCodeTab();
    fireEvent.click(screen.getByTestId('rollback-button'));
    fireEvent.click(screen.getByTestId('rollback-confirm'));

    await waitFor(() => {
      expect(screen.getByText('Version conflict detected')).toBeInTheDocument();
    });
  });
});
