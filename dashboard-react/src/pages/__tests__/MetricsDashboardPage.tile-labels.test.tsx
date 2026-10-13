// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The two row tiles: what their labels must say, and what they may NOT.
 *
 * HISTORY. The first tile was "Records Ingested (estimated)", then "Records
 * Accepted (estimated)": a sum of file-size estimates over `file_validation`
 * events. On 2026-10-10 it was replaced with "Rows Loaded", the rows the Glue
 * job reports writing (`_get_rows_loaded` in `handlers/metrics.py`), because the
 * estimate could say nothing about a JDBC source and re-counted re-runs.
 *
 * WHAT IS PINNED, AND WHAT IS NOT. Copy may be reworded — `LGR-99050e6-1`: "a
 * label is copy and can be reworded without changing what the number means" — so
 * the assertions pin the claim, not the sentence: the label names loaded rows,
 * it does not call a count an estimate, and the rate tile names the same
 * quantity per hour without calling itself a throughput.
 *
 * The testIds are untouched on purpose: they are the contract other suites key
 * on, and `metric-lines-ingested` keeping its name is why this file reads label
 * TEXT rather than the attribute.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ComprehensiveMetrics } from '../../types';

/**
 * A measured payload, so the tiles render figures rather than Not_Established —
 * the labels must be honest about a number that IS there, which is the case the
 * old wording got wrong. `measurement` is supplied because the page quotes it
 * into the description slot; the strings here are this fixture's, not the
 * backend's (see the note in the description test below).
 */
const MEASURED_METRICS = {
  performance: {
    lines_ingested_24h: 1_101_306,
    processing_rate: 1_529.59,
    overall_success_rate: 33.3,
    failed_pipelines: 14,
    rejected_at_approval: 3,
    agent_latency_ms: 4_956.28,
    monthly_cost: 12.34,
    monthly_cost_basis: null,
    terminal_counts: null,
    measurement: {
      lines_ingested_24h: 'rows_written on the Glue job load_metrics rows, where the write was confirmed',
      processing_rate: 'that same rows-loaded count over wall-clock hours in the window',
      overall_success_rate: 'succeeded over all terminal executions',
      failed_pipelines: 'ExecutionsFailed sum',
      rejected_at_approval: 'ApprovalRejected failure records',
      agent_latency_ms: 'AgentCore stage duration average',
      monthly_cost: 'Glue DPU-minutes estimate',
    },
    determined_from: {},
    missing: [],
    trends: { ingestion: 'stable', success: 'stable', agent_latency: 'stable', cost: 'stable' },
  },
  stages: {
    schema_extract: { success_count: 0, error_count: 0, avg_time_ms: null },
    schema_mapping: { success_count: 0, error_count: 0, avg_time_ms: null },
    code_generation: { success_count: 0, error_count: 0, avg_time_ms: null },
    infrastructure: { success_count: 0, error_count: 0, avg_time_ms: null },
    quality_checks: { success_count: 0, error_count: 0, avg_time_ms: null },
    pipeline_run: { success_count: 0, error_count: 0, avg_time_ms: null },
  },
  infrastructure: {
    agentcore: { availability: null, response_time_ms: null, status: 'unknown' },
    dynamodb: { read_capacity_per_hour: 0, write_capacity_per_hour: 0, status: 'healthy' },
    s3: { storage_used_bytes: null, requests_per_minute: null, status: 'unknown' },
    lambda: { invocations_per_minute: 0, error_rate: 0, status: 'healthy' },
  },
} as unknown as ComprehensiveMetrics;

vi.mock('../../hooks', () => ({
  useMetrics: () => ({ data: MEASURED_METRICS, isLoading: false, isError: false, refetch: vi.fn() }),
  useTokenUsageMetrics: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useCostRecommendations: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useApplyRecommendation: () => ({ mutate: vi.fn(), isPending: false }),
}));

import MetricsDashboardPage from '../MetricsDashboardPage';

function renderPage() {
  return render(
    <MemoryRouter>
      <MetricsDashboardPage />
    </MemoryRouter>,
  );
}

/**
 * A `MetricBox`'s label: its FIRST child. The figure is the second. The
 * description sits behind the label's question-mark button, in a popover
 * rendered outside the tile, so reading the label never picks up a word from
 * the description — which is exactly the confusion this file is meant to catch,
 * since the description is allowed (and required) to use the word "ingestion"
 * when explaining what the tile is not.
 */
function boxLabel(testId: string): string {
  const kids = Array.from(screen.getByTestId(testId).children);
  expect(kids.length, `unexpected MetricBox shape for ${testId}`).toBeGreaterThanOrEqual(2);
  return (kids[0].textContent ?? '').trim();
}

/** Open a tile's question-mark popover and return the description it shows. */
function openedDescription(testId: string): string {
  fireEvent.click(within(screen.getByTestId(testId)).getByRole('button', { name: /^About / }));
  return screen.getByTestId(`${testId}-description`).textContent ?? '';
}

describe('MetricsDashboardPage — the rows tile names what was loaded', () => {
  it('says rows were loaded', () => {
    renderPage();
    expect(boxLabel('metric-lines-ingested')).toMatch(/rows/i);
    expect(boxLabel('metric-lines-ingested')).toMatch(/load/i);
  });

  it('does not call a count an estimate, or say records were accepted', () => {
    renderPage();
    const label = boxLabel('metric-lines-ingested');
    // The figure is the job's own count, not a file-size derivation, and it says
    // nothing about validation.
    expect(label).not.toMatch(/estimat/i);
    expect(label).not.toMatch(/accepted|validat/i);
  });

  it('keeps its testId, which other suites key on', () => {
    renderPage();
    expect(screen.getByTestId('metric-lines-ingested')).toBeTruthy();
  });

  it('renders the measured figure', () => {
    renderPage();
    expect(screen.getByTestId('metric-lines-ingested').textContent).toContain(
      (1_101_306).toLocaleString(),
    );
  });
});

describe('MetricsDashboardPage — the rate tile is the same count per hour', () => {
  it('does not call itself a processing rate or a throughput', () => {
    renderPage();
    const label = boxLabel('metric-processing-rate');
    // The denominator is wall clock, not pipeline running time, so neither term
    // is supportable.
    expect(label).not.toMatch(/processing/i);
    expect(label).not.toMatch(/throughput/i);
  });

  it('names the same quantity as the tile it divides, per hour', () => {
    renderPage();
    const label = boxLabel('metric-processing-rate');
    // The two tiles share one numerator, so a reader must be able to see that
    // from the labels alone. If one is relabelled and the other is not, this fails.
    expect(label).toMatch(/rows/i);
    expect(label).toMatch(/load/i);
    expect(label).toMatch(/hour|\/hr/i);
  });

  it('keeps its testId and its measured figure', () => {
    renderPage();
    expect(screen.getByTestId('metric-processing-rate').textContent).toContain(
      `${(1_529.59).toLocaleString()}/hr`,
    );
  });
});

describe('MetricsDashboardPage — the two labels do not carry the detail alone', () => {
  /**
   * The label is short by necessity, so the detail lives in the description slot
   * quoted from `measurement` (R7.6). This asserts the RENDERING of that quote,
   * not the backend's wording: the strings come from `MEASURED_METRICS` above.
   * The backend's actual strings are pinned on the other side of the wire, in
   * `tests/unit/lambdas/dashboard_api/test_metrics_acceptance_labelling.py`.
   */
  it('renders the payload measurement into each tile description', () => {
    renderPage();
    const rows = openedDescription('metric-lines-ingested');
    const rate = openedDescription('metric-processing-rate');
    expect(rows).toContain('load_metrics');
    expect(rate).toContain('wall-clock hours');
  });
});

describe('MetricsDashboardPage — tile detail is one click away, not printed under the figure', () => {
  const TILES = [
    { testId: 'metric-lines-ingested', label: 'Rows Loaded', field: 'lines_ingested_24h' },
    { testId: 'metric-processing-rate', label: 'Rows Loaded per Hour', field: 'processing_rate' },
    { testId: 'metric-success-rate', label: 'Success Rate', field: 'overall_success_rate' },
    { testId: 'metric-failed-pipelines', label: 'Failed Executions', field: 'failed_pipelines' },
    { testId: 'metric-rejected-at-approval', label: 'Rejected at Approval', field: 'rejected_at_approval' },
    { testId: 'metric-agent-latency', label: 'Avg Agent Stage Duration', field: 'agent_latency_ms' },
    { testId: 'metric-monthly-cost', label: 'Est. Glue Cost (selected period)', field: 'monthly_cost' },
  ] as const;

  it.each(TILES)('$testId keeps its description off the tile until asked', ({ testId, field }) => {
    renderPage();
    const measurement = MEASURED_METRICS.performance.measurement[field];
    expect(screen.getByTestId(testId).textContent).not.toContain(measurement);
    expect(screen.queryByTestId(`${testId}-description`)).not.toBeInTheDocument();
  });

  it.each(TILES)('$testId opens its description from a named question-mark button', ({ testId, label, field }) => {
    renderPage();
    fireEvent.click(within(screen.getByTestId(testId)).getByRole('button', { name: `About ${label}` }));
    expect(screen.getByTestId(`${testId}-description`).textContent).toContain(
      MEASURED_METRICS.performance.measurement[field],
    );
    // The open popover is outside the tile, so the label still reads as just the label.
    expect(boxLabel(testId)).toBe(label);
  });

  it('explains on request what a trend compares and what Not established means', () => {
    renderPage();
    expect(screen.queryByTestId('trend-indicators-about')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'About Trend Indicators' }));
    const about = screen.getByTestId('trend-indicators-about').textContent ?? '';
    expect(about).toMatch(/same length just before it/);
    expect(about).toContain('Not established');
    expect(about).toContain('Stable');
  });
});
