// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * ErrorsPage — Async_State_Triple (p30 task 5.4 / 5.7).
 *
 * R13.4: a fetch failure MUST NOT render "No errors found". That is the most
 * misleading output this page can produce: the reader concludes the fleet is
 * healthy exactly when the dashboard cannot tell.
 *
 * All three legs of the triple are asserted, because pinning only the error leg
 * would pass against a page that showed the error copy unconditionally:
 *   - failure  → error copy + Retry, and NOT "No errors found"
 *   - empty    → "No errors found", and NO Retry
 *   - loading  → the loading text, neither of the other two
 *
 * The error state is reachable only through the Table's `empty` slot (Cloudscape
 * renders it when `items.length === 0 && !loading`), so the query must already
 * be settled into `isError` when the assertion runs. Mocking the hook module
 * with a synchronous fake — the pattern `PipelineListPage.async.test.tsx` and
 * `ApprovalsPage.async.test.tsx` use — removes the async entirely: there is no
 * `isLoading: true` phase to race.
 *
 * Requirements: 13.4, 13.7
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import type { ErrorEntry } from '../../hooks/use-errors';

let mockErrorsQuery: {
  data?: { errors: ErrorEntry[]; count: number; start_date: string; end_date: string };
  isLoading: boolean;
  isError: boolean;
  refetch: () => unknown;
};
const mockRefetch = vi.fn();

vi.mock('../../hooks/use-errors', () => ({
  useErrors: () => mockErrorsQuery,
}));

import ErrorsPage from '../ErrorsPage';

const ERROR_ROW: ErrorEntry = {
  error_date: '2024-06-01',
  timestamp: '2024-06-01T10:30:00+00:00#run-1',
  run_id: 'run-1',
  pipeline_id: 'orders-pipeline',
  step_name: 'RunPipeline',
  error_type: 'timeout',
  error_message: 'Glue job exceeded the configured timeout',
  ttl: 0,
};

const FAILURE_COPY = 'Failed to load errors. Check your API connection.';
const EMPTY_COPY = 'No errors found';

beforeEach(() => {
  mockRefetch.mockReset();
});

describe('ErrorsPage — Async_State_Triple (R13.4)', () => {
  it('renders an error with a Retry button on API failure (not the empty copy)', () => {
    mockErrorsQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    render(<ErrorsPage />);

    expect(screen.getByText(FAILURE_COPY)).toBeInTheDocument();
    expect(screen.getByTestId('errors-retry')).toBeInTheDocument();
    // The defect R13.4 exists to remove: a failure reading as a healthy fleet.
    expect(screen.queryByText(EMPTY_COPY)).not.toBeInTheDocument();
  });

  it('invokes refetch when Retry is clicked on the error state', () => {
    mockErrorsQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    render(<ErrorsPage />);

    fireEvent.click(screen.getByTestId('errors-retry'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('suppresses the header count on failure rather than reporting zero errors', () => {
    mockErrorsQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    const { container } = render(<ErrorsPage />);

    // "(0)" next to the Errors heading would be a fabricated measurement.
    expect(container.textContent).not.toContain('(0)');
  });

  it('renders the "No errors found" empty copy only when genuinely empty', () => {
    mockErrorsQuery = {
      data: { errors: [], count: 0, start_date: '2024-05-25', end_date: '2024-06-01' },
      isLoading: false,
      isError: false,
      refetch: mockRefetch,
    };
    render(<ErrorsPage />);

    expect(screen.getByText(EMPTY_COPY)).toBeInTheDocument();
    expect(screen.queryByTestId('errors-retry')).not.toBeInTheDocument();
    expect(screen.queryByText(FAILURE_COPY)).not.toBeInTheDocument();
  });

  it('renders neither the error nor the empty copy while loading', () => {
    mockErrorsQuery = { data: undefined, isLoading: true, isError: false, refetch: mockRefetch };
    render(<ErrorsPage />);

    expect(screen.getByText('Loading errors...')).toBeInTheDocument();
    expect(screen.queryByText(EMPTY_COPY)).not.toBeInTheDocument();
    expect(screen.queryByTestId('errors-retry')).not.toBeInTheDocument();
  });

  it('renders error rows on success, with no empty or failure copy', () => {
    mockErrorsQuery = {
      data: { errors: [ERROR_ROW], count: 1, start_date: '2024-05-25', end_date: '2024-06-01' },
      isLoading: false,
      isError: false,
      refetch: mockRefetch,
    };
    render(<ErrorsPage />);

    expect(screen.getByText('orders-pipeline')).toBeInTheDocument();
    expect(screen.getByText('Glue job exceeded the configured timeout')).toBeInTheDocument();
    expect(screen.queryByText(EMPTY_COPY)).not.toBeInTheDocument();
    expect(screen.queryByTestId('errors-retry')).not.toBeInTheDocument();
  });
});
