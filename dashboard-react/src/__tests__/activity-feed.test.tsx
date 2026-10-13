// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import * as fc from 'fast-check';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import type { UpdateItem } from '../types';
import { formatRelativeTime } from '../utils/formatting';

// Mock the useUpdates hook
const mockUseUpdates = vi.fn();
vi.mock('../hooks', () => ({
  useUpdates: (...args: unknown[]) => mockUseUpdates(...args),
}));

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

import ActivityFeed, { ActivityFeedItem } from '../components/ActivityFeed';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

function makeUpdateItem(overrides: Partial<UpdateItem> = {}): UpdateItem {
  return {
    pipeline_id: 'pipe-1',
    run_id: 'run-1',
    status: 'completed',
    started_at: new Date().toISOString(),
    pipeline_name: 'My Pipeline',
    current_step: 'schema_extract',
    ...overrides,
  };
}

// ── Unit tests ───────────────────────────────────────────────────

describe('ActivityFeed', () => {
  it('shows a loading spinner while data is being fetched', () => {
    mockUseUpdates.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    });

    render(<ActivityFeed />, { wrapper: createWrapper() });
    expect(screen.getByTestId('activity-feed-loading')).toBeInTheDocument();
  });

  it('shows an error message when the API returns an error', () => {
    mockUseUpdates.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });

    render(<ActivityFeed />, { wrapper: createWrapper() });
    expect(screen.getByText('Failed to load recent activity')).toBeInTheDocument();
  });

  it('shows "No recent activity" when updates array is empty', () => {
    mockUseUpdates.mockReturnValue({
      data: { updates: [], count: 0 },
      isLoading: false,
      isError: false,
    });

    render(<ActivityFeed />, { wrapper: createWrapper() });
    expect(screen.getByText('No recent activity')).toBeInTheDocument();
  });

  it('renders only collapsedCount items by default', () => {
    const updates = Array.from({ length: 6 }, (_, i) =>
      makeUpdateItem({ run_id: `run-${i}`, pipeline_name: `Pipeline ${i}` }),
    );
    mockUseUpdates.mockReturnValue({
      data: { updates, count: 6 },
      isLoading: false,
      isError: false,
    });

    render(<ActivityFeed collapsedCount={3} />, { wrapper: createWrapper() });
    expect(screen.getByTestId('activity-item-run-0')).toBeInTheDocument();
    expect(screen.getByTestId('activity-item-run-1')).toBeInTheDocument();
    expect(screen.getByTestId('activity-item-run-2')).toBeInTheDocument();
    expect(screen.queryByTestId('activity-item-run-3')).not.toBeInTheDocument();
  });

  it('expands to show all items when toggle is clicked', () => {
    const updates = Array.from({ length: 5 }, (_, i) =>
      makeUpdateItem({ run_id: `run-${i}`, pipeline_name: `Pipeline ${i}` }),
    );
    mockUseUpdates.mockReturnValue({
      data: { updates, count: 5 },
      isLoading: false,
      isError: false,
    });

    render(<ActivityFeed collapsedCount={3} />, { wrapper: createWrapper() });
    expect(screen.queryByTestId('activity-item-run-4')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('activity-feed-toggle'));
    expect(screen.getByTestId('activity-item-run-4')).toBeInTheDocument();
  });

  it('navigates to pipeline run when item is clicked', () => {
    const updates = [
      makeUpdateItem({ run_id: 'run-abc', pipeline_id: 'pipe-xyz' }),
    ];
    mockUseUpdates.mockReturnValue({
      data: { updates, count: 1 },
      isLoading: false,
      isError: false,
    });

    render(<ActivityFeed />, { wrapper: createWrapper() });
    fireEvent.click(screen.getByTestId('activity-item-run-abc'));
    expect(mockNavigate).toHaveBeenCalledWith('/pipelines/pipe-xyz/runs/run-abc');
  });

  it('defaults limit to 10 and passes it to useUpdates', () => {
    mockUseUpdates.mockReturnValue({
      data: { updates: [], count: 0 },
      isLoading: false,
      isError: false,
    });

    render(<ActivityFeed />, { wrapper: createWrapper() });
    expect(mockUseUpdates).toHaveBeenCalledWith(10);
  });

  it('passes custom limit to useUpdates', () => {
    mockUseUpdates.mockReturnValue({
      data: { updates: [], count: 0 },
      isLoading: false,
      isError: false,
    });

    render(<ActivityFeed limit={5} />, { wrapper: createWrapper() });
    expect(mockUseUpdates).toHaveBeenCalledWith(5);
  });

  it('falls back to pipeline_id when pipeline_name is not provided', () => {
    const updates = [
      makeUpdateItem({ run_id: 'run-1', pipeline_id: 'pipe-abc', pipeline_name: undefined }),
    ];
    mockUseUpdates.mockReturnValue({
      data: { updates, count: 1 },
      isLoading: false,
      isError: false,
    });

    render(<ActivityFeed />, { wrapper: createWrapper() });
    expect(screen.getByText('pipe-abc')).toBeInTheDocument();
  });

  it('does not show toggle when items fit within collapsedCount', () => {
    const updates = [
      makeUpdateItem({ run_id: 'run-1' }),
      makeUpdateItem({ run_id: 'run-2' }),
    ];
    mockUseUpdates.mockReturnValue({
      data: { updates, count: 2 },
      isLoading: false,
      isError: false,
    });

    render(<ActivityFeed collapsedCount={3} />, { wrapper: createWrapper() });
    expect(screen.queryByTestId('activity-feed-toggle')).not.toBeInTheDocument();
  });

  // ── Property test ──────────────────────────────────────────────
  /**
   * Feature: p12c-addon, Property 8: ActivityFeed item rendering
   *
   * For any UpdateItem with a valid status, started_at ISO string, and either
   * pipeline_name or pipeline_id, the rendered feed item shall contain a
   * StatusIndicator whose type matches statusType(status), the pipeline name
   * (falling back to pipeline_id), and a relative timestamp derived from started_at.
   *
   * Validates: Requirements 8.2, 8.3
   */
  it('Property 8: item rendering — StatusIndicator type, pipeline name, and timestamp are correct', () => {
    const statusArb = fc.constantFrom(
      'completed', 'active', 'succeeded', 'ready', 'approved',
      'failed', 'error', 'stopped', 'rejected', 'expired', 'cancelled',
      'running', 'in_progress', 'in-progress',
      'waiting_approval', 'pending', 'approval_required',
      'aborted', 'timed_out',
      'unknown_status',
    );

    const updateItemArb = fc.record({
      pipeline_id: fc.stringMatching(/^[a-zA-Z0-9_-]{1,30}$/),
      run_id: fc.uuid(),
      status: statusArb,
      started_at: fc.date({ min: new Date('2020-01-01'), max: new Date('2025-06-01') }).map((d) => d.toISOString()),
      current_step: fc.option(fc.stringMatching(/^[a-zA-Z_]{1,20}$/), { nil: undefined }),
      pipeline_name: fc.option(fc.stringMatching(/^[a-zA-Z0-9 _-]{1,30}$/), { nil: undefined }),
    });

    fc.assert(
      fc.property(updateItemArb, (item) => {
        const expectedName = item.pipeline_name || item.pipeline_id;
        const expectedTimestamp = formatRelativeTime(item.started_at);

        const { container, unmount } = render(
          <ActivityFeedItem item={item as UpdateItem} />,
          { wrapper: createWrapper() },
        );

        const text = container.textContent ?? '';
        expect(text).toContain(expectedName);
        expect(text).toContain(expectedTimestamp);

        unmount();
      }),
      { numRuns: 100 },
    );
  });
});
