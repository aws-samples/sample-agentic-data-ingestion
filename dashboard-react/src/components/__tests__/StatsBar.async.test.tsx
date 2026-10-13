// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

/* ------------------------------------------------------------------ */
/*  P29 R1.4 — Async_State_Triple on StatsBar.                        */
/*                                                                    */
/*  A stats load failure MUST render an error + Retry, NOT render     */
/*  misleading zeros for every stat.                                  */
/* ------------------------------------------------------------------ */

let mockStatsQuery: {
  data?: unknown;
  isLoading: boolean;
  isError: boolean;
  refetch: () => unknown;
};
const mockRefetch = vi.fn();

vi.mock('../../hooks', () => ({
  useStats: () => mockStatsQuery,
  useUpdates: () => ({ data: { updates: [], count: 0 }, isLoading: false, isError: false }),
}));

import StatsBar from '../StatsBar';

function renderBar() {
  return render(
    <MemoryRouter>
      <StatsBar />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockRefetch.mockReset();
});

describe('StatsBar — Async_State_Triple (R1.4)', () => {
  it('renders an error with a Retry button on failure (not zeros)', () => {
    mockStatsQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    renderBar();
    expect(screen.getByTestId('stats-bar-error')).toBeInTheDocument();
    expect(screen.getByTestId('stats-bar-retry')).toBeInTheDocument();
    // The misleading zero-value stat boxes must NOT be rendered on error.
    expect(screen.queryByTestId('stat-total-pipelines')).not.toBeInTheDocument();
  });

  it('invokes refetch when Retry is clicked', () => {
    mockStatsQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    renderBar();
    fireEvent.click(screen.getByTestId('stats-bar-retry'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('renders the stat boxes when data loads successfully', () => {
    mockStatsQuery = {
      data: { total_pipelines: 3, total_files: 10, schema_versions: 2, last_activity: '' },
      isLoading: false,
      isError: false,
      refetch: mockRefetch,
    };
    renderBar();
    expect(screen.getByTestId('stat-total-pipelines')).toBeInTheDocument();
    expect(screen.queryByTestId('stats-bar-error')).not.toBeInTheDocument();
  });
});
