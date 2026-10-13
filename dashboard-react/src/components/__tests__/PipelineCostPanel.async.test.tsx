// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

/* ------------------------------------------------------------------ */
/*  P30 R3.2 / R3.3 / R3.4 — per-pipeline cost view.                   */
/*                                                                    */
/*  The figures must render in currency, cost_trend must render as a   */
/*  per-run series, the Glue-only scope must be stated, and the        */
/*  Async_State_Triple must hold: a failed load shows an error plus a  */
/*  Retry wired to the query's real refetch, and a pipeline with no    */
/*  recorded cost shows empty-state copy rather than $0.00.            */
/*                                                                    */
/*  Task 2.2 owns the wire-shape contract test; this file covers only  */
/*  rendering.                                                        */
/* ------------------------------------------------------------------ */

import type { PipelineCosts } from '../../hooks/use-pipeline-costs';

let mockCostQuery: {
  data?: PipelineCosts;
  isLoading: boolean;
  isError: boolean;
  refetch: () => unknown;
};
const mockRefetch = vi.fn();

vi.mock('../../hooks', () => ({
  usePipelineCosts: () => mockCostQuery,
}));

import PipelineCostPanel from '../PipelineCostPanel';

const THREE_RUNS: PipelineCosts = {
  pipeline_id: 'pipe-1',
  total_cost: 7.0,
  average_cost_per_run: 2.333,
  cost_trend: [
    { run_id: 'run-3', actual_cost: 3.0, timestamp: '2026-01-12T10:00:00Z' },
    { run_id: 'run-2', actual_cost: 2.5, timestamp: '2026-01-11T10:00:00Z' },
    { run_id: 'run-1', actual_cost: 1.5, timestamp: '2026-01-10T10:00:00Z' },
  ],
  most_expensive_run: { run_id: 'run-3', actual_cost: 3.0, timestamp: '2026-01-12T10:00:00Z' },
  run_count: 3,
};

const NO_COST: PipelineCosts = {
  pipeline_id: 'pipe-1',
  total_cost: 0,
  average_cost_per_run: 0,
  cost_trend: [],
  most_expensive_run: null,
  run_count: 0,
};

beforeEach(() => {
  mockRefetch.mockReset();
});

describe('PipelineCostPanel — Async_State_Triple (R3.4)', () => {
  it('renders a spinner while loading, and no figures', () => {
    mockCostQuery = { data: undefined, isLoading: true, isError: false, refetch: mockRefetch };
    render(<PipelineCostPanel pipelineId="pipe-1" />);
    expect(screen.getByTestId('cost-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('cost-total')).not.toBeInTheDocument();
  });

  it('renders an error with Retry on failure — never $0.00 figures', () => {
    mockCostQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    render(<PipelineCostPanel pipelineId="pipe-1" />);
    expect(screen.getByTestId('cost-error')).toBeInTheDocument();
    expect(screen.getByTestId('cost-retry')).toBeInTheDocument();
    expect(screen.queryByTestId('cost-total')).not.toBeInTheDocument();
    expect(screen.queryByTestId('cost-trend-table')).not.toBeInTheDocument();
  });

  it('wires Retry to the query refetch', () => {
    mockCostQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    render(<PipelineCostPanel pipelineId="pipe-1" />);
    fireEvent.click(screen.getByTestId('cost-retry'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('renders empty-state copy, not a zero, when no run has a recorded cost', () => {
    mockCostQuery = { data: NO_COST, isLoading: false, isError: false, refetch: mockRefetch };
    render(<PipelineCostPanel pipelineId="pipe-1" />);
    expect(screen.getByTestId('cost-empty')).toHaveTextContent(/No cost recorded yet/);
    expect(screen.queryByTestId('cost-total')).not.toBeInTheDocument();
    expect(screen.queryByTestId('cost-error')).not.toBeInTheDocument();
  });

  it('says the pipeline has not run when the caller knows there are no runs', () => {
    mockCostQuery = { data: NO_COST, isLoading: false, isError: false, refetch: mockRefetch };
    render(<PipelineCostPanel pipelineId="pipe-1" hasRuns={false} />);
    expect(screen.getByTestId('cost-empty')).toHaveTextContent('has not run');
  });

  it('explains unattributed cost when runs exist but carry no cost', () => {
    mockCostQuery = { data: NO_COST, isLoading: false, isError: false, refetch: mockRefetch };
    render(<PipelineCostPanel pipelineId="pipe-1" hasRuns />);
    expect(screen.getByTestId('cost-empty')).toHaveTextContent(/Glue job finishes/);
    expect(screen.getByTestId('cost-empty')).not.toHaveTextContent('has not run');
  });
});

describe('PipelineCostPanel — figures and series (R3.2, R3.3)', () => {
  beforeEach(() => {
    mockCostQuery = { data: THREE_RUNS, isLoading: false, isError: false, refetch: mockRefetch };
  });

  it('renders total, average and most-expensive in currency', () => {
    render(<PipelineCostPanel pipelineId="pipe-1" />);
    expect(screen.getByTestId('cost-total')).toHaveTextContent('$7.00');
    // 2.333 → two decimals for a money-scale figure.
    expect(screen.getByTestId('cost-average')).toHaveTextContent('$2.33');
    expect(screen.getByTestId('cost-most-expensive')).toHaveTextContent('$3.00');
    expect(screen.getByTestId('cost-most-expensive-run')).toHaveTextContent('run-3');
  });

  it('renders cost_trend as a per-run series in the API\u2019s newest-first order', () => {
    render(<PipelineCostPanel pipelineId="pipe-1" />);
    const rows = screen.getByTestId('cost-trend-table').querySelectorAll('tbody tr');
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain('run-3');
    expect(rows[0].textContent).toContain('$3.00');
    expect(rows[2].textContent).toContain('run-1');
    expect(rows[2].textContent).toContain('$1.50');
  });

  it('states that the figures are Glue cost only and name the exclusions', () => {
    render(<PipelineCostPanel pipelineId="pipe-1" />);
    const note = screen.getByTestId('cost-scope-note');
    expect(note).toHaveTextContent('Glue cost only');
    expect(note).toHaveTextContent('DPU-hour');
    expect(note).toHaveTextContent('10-minute minimum');
    expect(note).toHaveTextContent('Lambda, DynamoDB, S3 and Bedrock');
  });

  it('renders a dash rather than a fabricated zero for a run with no timestamp', () => {
    mockCostQuery = {
      data: {
        ...THREE_RUNS,
        cost_trend: [{ run_id: 'run-x', actual_cost: 0.003, timestamp: '' }],
        run_count: 1,
      },
      isLoading: false,
      isError: false,
      refetch: mockRefetch,
    };
    render(<PipelineCostPanel pipelineId="pipe-1" />);
    const rows = screen.getByTestId('cost-trend-table').querySelectorAll('tbody tr');
    expect(rows[0].textContent).toContain('—');
    // Sub-cent cost must not collapse to $0.00.
    expect(rows[0].textContent).toContain('$0.003');
  });
});
