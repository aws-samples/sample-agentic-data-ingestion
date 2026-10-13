// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

/* ------------------------------------------------------------------ */
/*  p30 R14.4 — Retry on TestResultsViewer's error state.             */
/*                                                                    */
/*  The error state must offer a Retry wired to the query's own        */
/*  refetch, matching every other tab. Loading, error and             */
/*  genuinely-empty must stay three distinct states (p29 R1's         */
/*  Async_State_Triple): a failure must not read as empty, and an     */
/*  empty result must not read as a failure.                          */
/* ------------------------------------------------------------------ */

let mockTestResultsQuery: {
  data?: unknown;
  isLoading: boolean;
  isError: boolean;
  refetch: () => unknown;
};
const mockRefetch = vi.fn();

vi.mock('../../hooks', () => ({
  useTestResults: () => mockTestResultsQuery,
}));

import TestResultsViewer from '../TestResultsViewer';

function renderViewer() {
  return render(<TestResultsViewer codeVersionId="code-v1" />);
}

beforeEach(() => {
  cleanup();
  mockRefetch.mockReset();
});

describe('TestResultsViewer — Retry on the error state (R14.4)', () => {
  it('renders an error with a Retry button on failure', () => {
    mockTestResultsQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    renderViewer();
    expect(screen.getByTestId('test-results-error')).toBeInTheDocument();
    expect(screen.getByTestId('test-results-retry')).toBeInTheDocument();
  });

  it('invokes the query refetch when Retry is clicked', () => {
    mockTestResultsQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    renderViewer();
    fireEvent.click(screen.getByTestId('test-results-retry'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('renders the loading state without an error or Retry', () => {
    mockTestResultsQuery = { data: undefined, isLoading: true, isError: false, refetch: mockRefetch };
    renderViewer();
    expect(screen.queryByTestId('test-results-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('test-results-retry')).not.toBeInTheDocument();
    expect(screen.getByText('Loading test results...')).toBeInTheDocument();
  });

  it('renders empty copy — not an error — when there is no data and no failure', () => {
    mockTestResultsQuery = { data: undefined, isLoading: false, isError: false, refetch: mockRefetch };
    renderViewer();
    expect(screen.getByTestId('test-results-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('test-results-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('test-results-retry')).not.toBeInTheDocument();
  });

  it('renders the report — not an error — when data loads successfully', () => {
    mockTestResultsQuery = {
      data: { test_status: 'passed', test_output: 'Tests: 3 passed, 0 failed, 3 total' },
      isLoading: false,
      isError: false,
      refetch: mockRefetch,
    };
    renderViewer();
    expect(screen.getByTestId('test-status')).toHaveTextContent('PASSED');
    expect(screen.queryByTestId('test-results-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('test-results-empty')).not.toBeInTheDocument();
  });
});
