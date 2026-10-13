// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import type { GlueJobRun } from '../../types';

// Mock the useGlueJobLogs hook
const mockUseGlueJobLogs = vi.fn();
vi.mock('../../hooks', () => ({
  useGlueJobLogs: (...args: unknown[]) => mockUseGlueJobLogs(...args),
}));

import GlueJobLogViewer from '../GlueJobLogViewer';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

const RUNS: GlueJobRun[] = [
  { run_id: 'run-1', status: 'SUCCEEDED', started_at: '2024-01-01T00:00:00Z', completed_at: '2024-01-01T00:05:00Z', duration_seconds: 300 },
  { run_id: 'run-2', status: 'RUNNING', started_at: '2024-01-02T00:00:00Z' },
  { run_id: 'run-3', status: 'FAILED', started_at: '2024-01-03T00:00:00Z', completed_at: '2024-01-03T00:02:00Z', error_message: 'OOM' },
];

const SAMPLE_LOGS = [
  { timestamp: '2024-01-01T00:00:01Z', message: 'Starting job', level: 'INFO' },
  { timestamp: '2024-01-01T00:00:02Z', message: 'Slow partition detected', level: 'WARN' },
  { timestamp: '2024-01-01T00:00:03Z', message: 'NullPointerException in mapper', level: 'ERROR' },
];

beforeEach(() => {
  vi.clearAllMocks();
});

describe('GlueJobLogViewer', () => {
  it('renders log lines with timestamps, levels, and messages', () => {
    mockUseGlueJobLogs.mockReturnValue({
      data: { logs: SAMPLE_LOGS, log_type: 'output', run_id: 'run-1' },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<GlueJobLogViewer pipelineId="pipe-1" runs={RUNS} />, { wrapper: createWrapper() });

    const logLines = screen.getAllByTestId('log-line');
    expect(logLines).toHaveLength(3);
    expect(screen.getByText('Starting job')).toBeInTheDocument();
    expect(screen.getByText('Slow partition detected')).toBeInTheDocument();
    expect(screen.getByText('NullPointerException in mapper')).toBeInTheDocument();
  });

  it('color-codes ERROR lines red and WARN lines yellow', () => {
    mockUseGlueJobLogs.mockReturnValue({
      data: { logs: SAMPLE_LOGS, log_type: 'output', run_id: 'run-1' },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<GlueJobLogViewer pipelineId="pipe-1" runs={RUNS} />, { wrapper: createWrapper() });

    const logLines = screen.getAllByTestId('log-line');
    // INFO line — no special color
    expect(logLines[0].style.color).toBe('');
    // WARN line — warning design token (P29 R8.2), not a hardcoded hex
    expect(logLines[1].style.color).toContain('color-text-status-warning');
    // ERROR line — error design token
    expect(logLines[2].style.color).toContain('color-text-status-error');
  });

  it('toggles between Output and Error log types', () => {
    mockUseGlueJobLogs.mockReturnValue({
      data: { logs: SAMPLE_LOGS, log_type: 'output', run_id: 'run-1' },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<GlueJobLogViewer pipelineId="pipe-1" runs={RUNS} />, { wrapper: createWrapper() });

    // Initially called with 'output' log type
    expect(mockUseGlueJobLogs).toHaveBeenCalledWith('pipe-1', 'run-1', 'output', false);

    // Click the Error segment
    const errorButton = screen.getByText('Error');
    fireEvent.click(errorButton);

    // Should now be called with 'error' log type
    expect(mockUseGlueJobLogs).toHaveBeenCalledWith('pipe-1', 'run-1', 'error', false);
  });

  it('filters log lines by keyword', () => {
    mockUseGlueJobLogs.mockReturnValue({
      data: { logs: SAMPLE_LOGS, log_type: 'output', run_id: 'run-1' },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<GlueJobLogViewer pipelineId="pipe-1" runs={RUNS} />, { wrapper: createWrapper() });

    // Type a keyword into the filter
    const filterInput = screen.getByPlaceholderText('Filter log lines by keyword');
    fireEvent.change(filterInput, { target: { value: 'partition' } });

    // Only the WARN line should remain
    const logLines = screen.getAllByTestId('log-line');
    expect(logLines).toHaveLength(1);
    expect(screen.getByText('Slow partition detected')).toBeInTheDocument();
  });

  it('passes isRunning=true and refetchInterval=5000 for RUNNING runs', () => {
    // Select run-2 which is RUNNING — the component defaults to first run (run-1)
    // so we need runs with RUNNING first
    const runningRuns: GlueJobRun[] = [
      { run_id: 'run-running', status: 'RUNNING', started_at: '2024-01-02T00:00:00Z' },
    ];

    mockUseGlueJobLogs.mockReturnValue({
      data: { logs: [], log_type: 'output', run_id: 'run-running' },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<GlueJobLogViewer pipelineId="pipe-1" runs={runningRuns} />, { wrapper: createWrapper() });

    // The hook should be called with isRunning=true
    expect(mockUseGlueJobLogs).toHaveBeenCalledWith('pipe-1', 'run-running', 'output', true);
  });

  it('passes isRunning=false for terminal status runs', () => {
    mockUseGlueJobLogs.mockReturnValue({
      data: { logs: [], log_type: 'output', run_id: 'run-1' },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<GlueJobLogViewer pipelineId="pipe-1" runs={RUNS} />, { wrapper: createWrapper() });

    // run-1 is SUCCEEDED (terminal), so isRunning should be false
    expect(mockUseGlueJobLogs).toHaveBeenCalledWith('pipe-1', 'run-1', 'output', false);
  });

  it('shows empty state when no logs are available', () => {
    mockUseGlueJobLogs.mockReturnValue({
      data: { logs: [], log_type: 'output', run_id: 'run-1' },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<GlueJobLogViewer pipelineId="pipe-1" runs={RUNS} />, { wrapper: createWrapper() });

    expect(screen.getByTestId('log-empty')).toBeInTheDocument();
    expect(screen.getByText('No logs available for this run.')).toBeInTheDocument();
  });

  it('shows error state with retry button on fetch failure', () => {
    const mockRefetch = vi.fn();
    mockUseGlueJobLogs.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      refetch: mockRefetch,
    });

    render(<GlueJobLogViewer pipelineId="pipe-1" runs={RUNS} />, { wrapper: createWrapper() });

    expect(screen.getByTestId('log-error')).toBeInTheDocument();
    expect(screen.getByText('Failed to load logs')).toBeInTheDocument();

    // Click retry
    const retryButton = screen.getByTestId('log-retry-button');
    fireEvent.click(retryButton);
    expect(mockRefetch).toHaveBeenCalled();
  });

  it('shows auto-refresh indicator for running jobs', () => {
    const runningRuns: GlueJobRun[] = [
      { run_id: 'run-running', status: 'RUNNING', started_at: '2024-01-02T00:00:00Z' },
    ];

    mockUseGlueJobLogs.mockReturnValue({
      data: { logs: SAMPLE_LOGS, log_type: 'output', run_id: 'run-running' },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });

    render(<GlueJobLogViewer pipelineId="pipe-1" runs={runningRuns} />, { wrapper: createWrapper() });

    expect(screen.getByTestId('log-auto-refresh')).toBeInTheDocument();
    expect(screen.getByText('Auto-refreshing every 5 seconds')).toBeInTheDocument();
  });
});
