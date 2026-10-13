// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for MetricsDashboardPage
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 20: Metrics dashboard completeness
 *
 * **Validates: Requirements 20.1, 20.2, 20.3, 20.4**
 */

import { describe, it, expect, vi } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ComprehensiveMetrics, HealthStatus, StageMetrics } from '../../types';
import { humanizeDuration } from '../../components/common/formatters';

// ---------------------------------------------------------------------------
// Mock hooks before importing the component
// ---------------------------------------------------------------------------

let mockMetrics: ComprehensiveMetrics | undefined;

vi.mock('../../hooks', () => ({
  useMetrics: () => ({
    data: mockMetrics,
    isLoading: false,
    isError: false,
  }),
  useTokenUsageMetrics: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
  }),
  useCostRecommendations: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
  }),
  useApplyRecommendation: () => ({
    mutate: vi.fn(),
    isPending: false,
  }),
}));

// Must import after mocks
import MetricsDashboardPage from '../MetricsDashboardPage';

// ---------------------------------------------------------------------------
// Arbitraries (smart generators)
// ---------------------------------------------------------------------------

const trendArb = fc.constantFrom('up' as const, 'down' as const, 'stable' as const);

const healthStatusArb: fc.Arbitrary<HealthStatus> = fc.constantFrom('healthy', 'warning', 'critical', 'unknown');

const stageMetricsArb: fc.Arbitrary<StageMetrics> = fc.record({
  success_count: fc.integer({ min: 0, max: 10_000 }),
  error_count: fc.integer({ min: 0, max: 1_000 }),
  // null when the stage records no duration (rendered "N/A")
  avg_time_ms: fc.option(fc.integer({ min: 100, max: 120_000 }), { nil: null }),
});

const metricsArb: fc.Arbitrary<ComprehensiveMetrics> = fc.record({
  performance: fc.record({
    lines_ingested_24h: fc.integer({ min: 0, max: 10_000_000 }),
    processing_rate: fc.integer({ min: 0, max: 100_000 }),
    overall_success_rate: fc.float({ min: 0, max: 100, noNaN: true }),
    failed_pipelines: fc.integer({ min: 0, max: 100 }),
    agent_latency_ms: fc.integer({ min: 0, max: 600_000 }),
    monthly_cost: fc.integer({ min: 0, max: 100_000 }),
    trends: fc.record({
      ingestion: trendArb,
      success: trendArb,
      agent_latency: trendArb,
      cost: trendArb,
    }),
  }),
  stages: fc.record({
    schema_extract: stageMetricsArb,
    schema_mapping: stageMetricsArb,
    code_generation: stageMetricsArb,
    infrastructure: stageMetricsArb,
    quality_checks: stageMetricsArb,
    pipeline_run: stageMetricsArb,
  }),
  infrastructure: fc.record({
    agentcore: fc.record({
      availability: fc.float({ min: 0, max: 100, noNaN: true }),
      response_time_ms: fc.integer({ min: 1, max: 5_000 }),
      status: healthStatusArb,
    }),
    dynamodb: fc.record({
      read_capacity_per_hour: fc.float({ min: 0, max: 100000, noNaN: true }),
      write_capacity_per_hour: fc.float({ min: 0, max: 100000, noNaN: true }),
      status: healthStatusArb,
    }),
    s3: fc.record({
      storage_used_bytes: fc.integer({ min: 0, max: 10_737_418_240 }),
      requests_per_minute: fc.integer({ min: 0, max: 10_000 }),
      status: healthStatusArb,
    }),
    lambda: fc.record({
      invocations_per_minute: fc.integer({ min: 0, max: 10_000 }),
      error_rate: fc.float({ min: 0, max: 100, noNaN: true }),
      status: healthStatusArb,
    }),
  }),
});

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderPage() {
  return render(
    <MemoryRouter>
      <MetricsDashboardPage />
    </MemoryRouter>,
  );
}

/** Mirror of MetricsDashboardPage's formatAvgTime for assertions (P29 R4.2:
 * sub-second keeps ms fidelity; >= 1s is humanized via the shared formatter). */
function fmtAvg(ms: number | null): string {
  if (ms == null) return 'N/A';
  if (ms < 1000) return `${ms}ms`;
  return humanizeDuration(ms);
}

// ---------------------------------------------------------------------------
// Property 20: Metrics dashboard completeness
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 20: Metrics dashboard completeness', () => {
  /**
   * **Validates: Requirements 20.1**
   *
   * For any ComprehensiveMetrics object, the MetricsDashboard SHALL render
   * all 6 performance metrics.
   */
  it('renders all performance metrics', () => {
    fc.assert(
      fc.property(metricsArb, (metrics) => {
        cleanup();
        mockMetrics = metrics;
        const { container } = renderPage();

        const perfSection = within(container).getByTestId('performance-metrics');

        // All 6 performance metric boxes should be present
        expect(within(perfSection).getByTestId('metric-lines-ingested')).toBeTruthy();
        expect(within(perfSection).getByTestId('metric-processing-rate')).toBeTruthy();
        expect(within(perfSection).getByTestId('metric-success-rate')).toBeTruthy();
        expect(within(perfSection).getByTestId('metric-failed-pipelines')).toBeTruthy();
        expect(within(perfSection).getByTestId('metric-agent-latency')).toBeTruthy();
        expect(within(perfSection).getByTestId('metric-monthly-cost')).toBeTruthy();

        // Verify values are rendered (toLocaleString adds commas, so check formatted value)
        expect(within(perfSection).getByTestId('metric-lines-ingested').textContent).toContain(
          metrics.performance.lines_ingested_24h.toLocaleString(),
        );
        expect(within(perfSection).getByTestId('metric-failed-pipelines').textContent).toContain(
          String(metrics.performance.failed_pipelines),
        );

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  /**
   * **Validates: Requirements 20.2**
   *
   * For any ComprehensiveMetrics object, the MetricsDashboard SHALL render
   * all 6 stage entries with success count, error count, and avg time.
   */
  it('renders all 6 stage entries with success/error/avg time', () => {
    const stageKeys = [
      'schema_extract',
      'schema_mapping',
      'code_generation',
      'infrastructure',
      'quality_checks',
      'pipeline_run',
    ];

    fc.assert(
      fc.property(metricsArb, (metrics) => {
        cleanup();
        mockMetrics = metrics;
        const { container } = renderPage();

        const stageSection = within(container).getByTestId('stage-metrics');

        for (const key of stageKeys) {
          const stageEl = within(stageSection).getByTestId(`stage-${key}`);
          expect(stageEl).toBeTruthy();

          const stage = metrics.stages[key];
          expect(within(stageEl).getByTestId(`stage-${key}-success`).textContent).toContain(
            String(stage.success_count),
          );
          expect(within(stageEl).getByTestId(`stage-${key}-error`).textContent).toContain(
            String(stage.error_count),
          );
          expect(within(stageEl).getByTestId(`stage-${key}-avg-time`).textContent).toContain(
            fmtAvg(stage.avg_time_ms),
          );
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  /**
   * **Validates: Requirements 20.3**
   *
   * For any ComprehensiveMetrics object, the MetricsDashboard SHALL render
   * all 4 infrastructure indicators with their specific sub-metrics.
   */
  it('renders all 4 infrastructure indicators with sub-metrics', () => {
    fc.assert(
      fc.property(metricsArb, (metrics) => {
        cleanup();
        mockMetrics = metrics;
        const { container } = renderPage();

        const infraSection = within(container).getByTestId('infrastructure-status');

        // AgentCore
        expect(within(infraSection).getByTestId('infra-agentcore')).toBeTruthy();
        expect(within(infraSection).getByTestId('agentcore-availability')).toBeTruthy();
        expect(within(infraSection).getByTestId('agentcore-response-time')).toBeTruthy();

        // DynamoDB
        expect(within(infraSection).getByTestId('infra-dynamodb')).toBeTruthy();
        expect(within(infraSection).getByTestId('dynamodb-read-capacity')).toBeTruthy();
        expect(within(infraSection).getByTestId('dynamodb-write-capacity')).toBeTruthy();

        // S3
        expect(within(infraSection).getByTestId('infra-s3')).toBeTruthy();
        expect(within(infraSection).getByTestId('s3-storage-used')).toBeTruthy();
        expect(within(infraSection).getByTestId('s3-requests-per-min')).toBeTruthy();

        // Lambda
        expect(within(infraSection).getByTestId('infra-lambda')).toBeTruthy();
        expect(within(infraSection).getByTestId('lambda-invocations')).toBeTruthy();
        expect(within(infraSection).getByTestId('lambda-error-rate')).toBeTruthy();

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  /**
   * **Validates: Requirements 20.4**
   *
   * For any ComprehensiveMetrics object, the MetricsDashboard SHALL render
   * all 4 trend indicators (ingestion, success, agent_latency, cost).
   */
  it('renders all 4 trend indicators with correct arrows', () => {
    const arrowMap: Record<string, string> = { up: '↑', down: '↓', stable: '→' };

    // p30 R13.1 — Semantic_Polarity. This assertion previously read only
    // `textContent`, so nothing here pinned the *colour* the arrow is painted
    // in: a `trendColor` that returned "error" for every non-stable direction
    // passed all 100 runs. Colour is now checked per tile against the metric's
    // polarity — Ingestion and Success Rate are higher-is-better, Agent Latency
    // and Cost are lower-is-better, `stable` is neutral under either.
    const polarity: Record<string, 'higher-is-better' | 'lower-is-better'> = {
      ingestion: 'higher-is-better',
      success: 'higher-is-better',
      agent_latency: 'lower-is-better',
      cost: 'lower-is-better',
    };
    function expectedColor(metric: string, direction: string): string {
      if (direction === 'stable') return 'color-text-status-info';
      const positive =
        polarity[metric] === 'higher-is-better' ? direction === 'up' : direction === 'down';
      return positive ? 'color-text-status-success' : 'color-text-status-error';
    }

    fc.assert(
      fc.property(metricsArb, (metrics) => {
        cleanup();
        mockMetrics = metrics;
        const { container } = renderPage();

        const trendSection = within(container).getByTestId('trend-indicators');

        // All 4 trend indicators present
        const ingestion = within(trendSection).getByTestId('trend-ingestion');
        const success = within(trendSection).getByTestId('trend-success');
        const agentLatency = within(trendSection).getByTestId('trend-agent-latency');
        const cost = within(trendSection).getByTestId('trend-cost');

        expect(ingestion).toBeTruthy();
        expect(success).toBeTruthy();
        expect(agentLatency).toBeTruthy();
        expect(cost).toBeTruthy();

        // Verify arrows match trend direction, and their colour matches the
        // metric's polarity (R13.1).
        const tiles: [HTMLElement, string, string][] = [
          [ingestion, 'trend-ingestion', 'ingestion'],
          [success, 'trend-success', 'success'],
          [agentLatency, 'trend-agent-latency', 'agent_latency'],
          [cost, 'trend-cost', 'cost'],
        ];
        for (const [tile, testId, metric] of tiles) {
          const direction = metrics.performance.trends[metric as keyof typeof metrics.performance.trends];
          const arrowEl = within(tile).getByTestId(`${testId}-arrow`);
          expect(arrowEl.textContent).toContain(arrowMap[direction]);
          expect(arrowEl.className).toContain(expectedColor(metric, direction));
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
