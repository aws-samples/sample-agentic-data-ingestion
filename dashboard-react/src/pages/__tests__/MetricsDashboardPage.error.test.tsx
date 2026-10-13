// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * MetricsDashboardPage — metrics error state + humanized durations (P29 task 3.4).
 *
 * R4.1: when the comprehensive-metrics query fails, the page renders an honest
 * error + Retry — never fabricated numbers. MOCK_METRICS (including the fake
 * $1,850/mo cost figure) was deleted from use-metrics, so no mock data can render.
 *
 * R4.2: elapsed-time values are humanized via the shared `humanizeDuration`
 * formatter — no raw milliseconds ("2340ms"). Sub-second latency keeps ms
 * fidelity ("120ms") by design.
 *
 * Requirements: 1.5, 4.1, 4.2
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ComprehensiveMetrics } from '../../types';

let metricsIsError = false;
const mockRefetchMetrics = vi.fn();

// Metrics carrying minute-scale elapsed durations to exercise humanizeDuration,
// plus a sub-second infra response time to prove ms fidelity is preserved.
const HUMANIZED_METRICS = {
  performance: {
    lines_ingested_24h: 0,
    processing_rate: 0,
    overall_success_rate: 0,
    failed_pipelines: 0,
    agent_latency_ms: 128_000, // → "2m"
    monthly_cost: 0,
    trends: { ingestion: 'stable', success: 'stable', agent_latency: 'stable', cost: 'stable' },
  },
  stages: {
    schema_extract: { success_count: 0, error_count: 0, avg_time_ms: 2_300 }, // → "2s"
    schema_mapping: { success_count: 0, error_count: 0, avg_time_ms: 45_000 }, // → "45s"
    code_generation: { success_count: 0, error_count: 0, avg_time_ms: 90_000 }, // → "1m"
    infrastructure: { success_count: 0, error_count: 0, avg_time_ms: null },
    quality_checks: { success_count: 0, error_count: 0, avg_time_ms: null },
    pipeline_run: { success_count: 0, error_count: 0, avg_time_ms: null },
  },
  infrastructure: {
    agentcore: { availability: 0, response_time_ms: 120, status: 'healthy' }, // → "120ms"
    dynamodb: { read_capacity_per_hour: 0, write_capacity_per_hour: 0, status: 'healthy' },
    s3: { storage_used_bytes: 0, requests_per_minute: 0, status: 'healthy' },
    lambda: { invocations_per_minute: 0, error_rate: 0, status: 'healthy' },
  },
} as unknown as ComprehensiveMetrics;

// Token metrics with a model row whose latency previously rendered as raw
// "2,340ms"; it must now be humanized.
const TOKEN_METRICS_WITH_LATENCY = {
  daily_usage: [],
  total_input_tokens: 0,
  total_output_tokens: 0,
  total_cache_read_tokens: 0,
  total_cost_usd: 0,
  model_comparison: [
    {
      model_id: 'us.anthropic.claude-sonnet-4-6',
      avg_input_tokens: 1250,
      avg_output_tokens: 340,
      avg_cost_usd: 0.0089,
      avg_latency_ms: 2_340, // → "2s"
      invocation_count: 65,
    },
  ],
  period: '7d',
};

vi.mock('../../hooks', () => ({
  useMetrics: () => ({
    data: metricsIsError ? undefined : HUMANIZED_METRICS,
    isLoading: false,
    isError: metricsIsError,
    refetch: mockRefetchMetrics,
  }),
  useTokenUsageMetrics: () => ({
    data: TOKEN_METRICS_WITH_LATENCY,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
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

beforeEach(() => {
  metricsIsError = false;
  mockRefetchMetrics.mockReset();
});

describe('MetricsDashboardPage — metrics error state (R4.1)', () => {
  it('renders an honest error + Retry when the metrics query fails', () => {
    metricsIsError = true;
    renderPage();

    expect(screen.getByTestId('metrics-error')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('metrics-retry'));
    expect(mockRefetchMetrics).toHaveBeenCalledTimes(1);
  });

  it('does not fabricate metrics on error (no $1,850 figure, no performance grid)', () => {
    metricsIsError = true;
    const { container } = renderPage();

    // The deleted MOCK_METRICS cost figure must never appear.
    expect(container.textContent).not.toContain('1,850');
    expect(container.textContent).not.toContain('1850');
    // No real metric widgets render in the error state.
    expect(screen.queryByTestId('performance-metrics')).not.toBeInTheDocument();
    expect(screen.queryByTestId('metric-monthly-cost')).not.toBeInTheDocument();
  });
});

describe('MetricsDashboardPage — humanized durations (R4.2)', () => {
  it('humanizes elapsed durations and never emits raw milliseconds', () => {
    renderPage();

    // Avg agent latency: 128000ms → "2m", not "128000ms".
    const agentLatency = screen.getByTestId('metric-agent-latency');
    expect(agentLatency.textContent).toContain('2m');
    expect(agentLatency.textContent).not.toContain('128000ms');

    // Stage avg times humanized.
    expect(screen.getByTestId('stage-schema_extract-avg-time').textContent).toContain('2s');
    expect(screen.getByTestId('stage-schema_mapping-avg-time').textContent).toContain('45s');
    expect(screen.getByTestId('stage-code_generation-avg-time').textContent).toContain('1m');

    // Model comparison latency was a raw-ms leak ("2,340ms"); now humanized.
    const modelTable = screen.getByTestId('model-comparison-table');
    expect(modelTable.textContent).toContain('2s');
    expect(modelTable.textContent).not.toContain('2,340ms');
    expect(modelTable.textContent).not.toContain('2340ms');
  });

  it('keeps sub-second latency fidelity (infra response time in ms)', () => {
    renderPage();
    // 120ms would floor to "0s" under humanizeDuration; the ms form is retained.
    expect(screen.getByTestId('agentcore-response-time').textContent).toContain('120ms');
  });
});
