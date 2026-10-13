// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * MetricsDashboardPage — Semantic_Polarity on trend indicators (p30 task 5.1 / 5.7).
 *
 * R13.1: for Cost and Agent Latency an upward direction renders as a *negative*
 * signal; for Ingestion and Success Rate upward renders positive. `stable` is
 * neutral under either polarity.
 *
 * These tests assert the **colour token**, which nothing pinned before. The
 * existing property test (`MetricsDashboardPage.property.test.tsx`, Property
 * 20.4) asserts the arrow glyph via the `-arrow` testids but only ever read
 * `textContent`, so a `trendColor` that painted every non-stable trend red
 * would have passed it. Colour is read off the Cloudscape `Box color` prop,
 * which lands in the DOM as an `awsui_color-<token>_<hash>` class.
 *
 * Both directions are asserted for both polarities on purpose. Cost-up alone
 * proves nothing: a function returning "error" unconditionally satisfies it.
 * Cost-**down** being success while Ingestion-**down** is error is what
 * distinguishes polarity from a coin flip.
 *
 * Requirements: 13.1, 13.7
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ComprehensiveMetrics, TrendIndicators } from '../../types';

type Direction = 'up' | 'down' | 'stable';

let trends: TrendIndicators = {
  ingestion: 'stable',
  success: 'stable',
  agent_latency: 'stable',
  cost: 'stable',
};

/** Only `trends` varies across these cases; every other value is inert. */
function metricsWithTrends(t: TrendIndicators): ComprehensiveMetrics {
  return {
    performance: {
      lines_ingested_24h: 0,
      processing_rate: 0,
      overall_success_rate: 0,
      failed_pipelines: 0,
      agent_latency_ms: 0,
      monthly_cost: 0,
      trends: t,
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
      agentcore: { availability: 0, response_time_ms: 1, status: 'healthy' },
      dynamodb: { read_capacity_per_hour: 0, write_capacity_per_hour: 0, status: 'healthy' },
      s3: { storage_used_bytes: 0, requests_per_minute: 0, status: 'healthy' },
      lambda: { invocations_per_minute: 0, error_rate: 0, status: 'healthy' },
    },
  } as unknown as ComprehensiveMetrics;
}

vi.mock('../../hooks', () => ({
  useMetrics: () => ({
    data: metricsWithTrends(trends),
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  useTokenUsageMetrics: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useCostRecommendations: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useApplyRecommendation: () => ({ mutate: vi.fn(), isPending: false }),
}));

import MetricsDashboardPage from '../MetricsDashboardPage';

function renderWith(t: Partial<TrendIndicators>) {
  trends = { ingestion: 'stable', success: 'stable', agent_latency: 'stable', cost: 'stable', ...t };
  return render(
    <MemoryRouter>
      <MetricsDashboardPage />
    </MemoryRouter>,
  );
}

/** The Cloudscape colour token carried by a tile's arrow, e.g. "text-status-error". */
function arrowColorToken(testId: string): string {
  const cls = screen.getByTestId(`${testId}-arrow`).className;
  const match = /awsui_color-(text-status-(?:success|error|info))_/.exec(cls);
  expect(match, `no colour token class on ${testId}-arrow (class="${cls}")`).not.toBeNull();
  return match![1];
}

beforeEach(() => {
  trends = { ingestion: 'stable', success: 'stable', agent_latency: 'stable', cost: 'stable' };
});

describe('MetricsDashboardPage — trend polarity colour (R13.1)', () => {
  it('renders Cost trending up as a negative signal', () => {
    renderWith({ cost: 'up' });
    expect(arrowColorToken('trend-cost')).toBe('text-status-error');
    // The direction itself is unchanged — polarity re-encodes it, it does not
    // rewrite it (constraint C6, LGR-620555b-1).
    expect(screen.getByTestId('trend-cost-arrow').textContent).toContain('↑');
    expect(screen.getByTestId('trend-cost').textContent).toContain('Up');
  });

  it('renders Cost trending down as a positive signal', () => {
    renderWith({ cost: 'down' });
    expect(arrowColorToken('trend-cost')).toBe('text-status-success');
    expect(screen.getByTestId('trend-cost-arrow').textContent).toContain('↓');
  });

  it('renders Agent Latency trending up as a negative signal', () => {
    renderWith({ agent_latency: 'up' });
    expect(arrowColorToken('trend-agent-latency')).toBe('text-status-error');
  });

  it('renders Agent Latency trending down as a positive signal', () => {
    renderWith({ agent_latency: 'down' });
    expect(arrowColorToken('trend-agent-latency')).toBe('text-status-success');
  });

  it('renders Ingestion trending up as a positive signal', () => {
    renderWith({ ingestion: 'up' });
    expect(arrowColorToken('trend-ingestion')).toBe('text-status-success');
  });

  it('renders Ingestion trending down as a negative signal', () => {
    renderWith({ ingestion: 'down' });
    expect(arrowColorToken('trend-ingestion')).toBe('text-status-error');
  });

  it('renders Success Rate trending up as a positive signal', () => {
    renderWith({ success: 'up' });
    expect(arrowColorToken('trend-success')).toBe('text-status-success');
  });

  it('renders Success Rate trending down as a negative signal', () => {
    renderWith({ success: 'down' });
    expect(arrowColorToken('trend-success')).toBe('text-status-error');
  });

  it('inverts Cost against Ingestion for the same direction, in a single render', () => {
    // The assertion that actually proves polarity: identical directions, opposite
    // colours, side by side in one tree.
    renderWith({ ingestion: 'up', success: 'up', agent_latency: 'up', cost: 'up' });
    expect(arrowColorToken('trend-ingestion')).toBe('text-status-success');
    expect(arrowColorToken('trend-success')).toBe('text-status-success');
    expect(arrowColorToken('trend-agent-latency')).toBe('text-status-error');
    expect(arrowColorToken('trend-cost')).toBe('text-status-error');
  });

  it('inverts again for the opposite direction, in a single render', () => {
    renderWith({ ingestion: 'down', success: 'down', agent_latency: 'down', cost: 'down' });
    expect(arrowColorToken('trend-ingestion')).toBe('text-status-error');
    expect(arrowColorToken('trend-success')).toBe('text-status-error');
    expect(arrowColorToken('trend-agent-latency')).toBe('text-status-success');
    expect(arrowColorToken('trend-cost')).toBe('text-status-success');
  });

  it('renders stable as neutral under either polarity', () => {
    renderWith({ ingestion: 'stable', success: 'stable', agent_latency: 'stable', cost: 'stable' });
    for (const id of ['trend-ingestion', 'trend-success', 'trend-agent-latency', 'trend-cost']) {
      expect(arrowColorToken(id)).toBe('text-status-info');
      expect(screen.getByTestId(`${id}-arrow`).textContent).toContain('→');
    }
  });

  it('states polarity in text as well as colour (colour alone is not accessible)', () => {
    renderWith({ cost: 'up', ingestion: 'up' });
    expect(screen.getByTestId('trend-cost-polarity').textContent).toContain('Lower is better');
    expect(screen.getByTestId('trend-agent-latency-polarity').textContent).toContain('Lower is better');
    expect(screen.getByTestId('trend-ingestion-polarity').textContent).toContain('Higher is better');
    expect(screen.getByTestId('trend-success-polarity').textContent).toContain('Higher is better');
  });

  it('keys polarity on the metric, not the tile label', () => {
    // Renaming a label must not move a tile's polarity. The label and the
    // polarity caption are independent renders of the same tile, so a
    // label-keyed lookup would have to read "Cost" to arrive at
    // "Lower is better" — this pins the pairing that TREND_POLARITY produces.
    renderWith({ cost: 'up', agent_latency: 'up' });
    const cost = screen.getByTestId('trend-cost');
    expect(cost.textContent).toContain('Cost');
    expect(cost.textContent).toContain('Lower is better');

    const latency = screen.getByTestId('trend-agent-latency');
    expect(latency.textContent).toContain('Agent Latency');
    expect(latency.textContent).toContain('Lower is better');
  });
});

/** Exhaustive matrix: every metric × every direction. */
describe('MetricsDashboardPage — trend polarity matrix (R13.1)', () => {
  const cases: [keyof TrendIndicators, string, Direction, string][] = [
    ['ingestion', 'trend-ingestion', 'up', 'text-status-success'],
    ['ingestion', 'trend-ingestion', 'down', 'text-status-error'],
    ['ingestion', 'trend-ingestion', 'stable', 'text-status-info'],
    ['success', 'trend-success', 'up', 'text-status-success'],
    ['success', 'trend-success', 'down', 'text-status-error'],
    ['success', 'trend-success', 'stable', 'text-status-info'],
    ['agent_latency', 'trend-agent-latency', 'up', 'text-status-error'],
    ['agent_latency', 'trend-agent-latency', 'down', 'text-status-success'],
    ['agent_latency', 'trend-agent-latency', 'stable', 'text-status-info'],
    ['cost', 'trend-cost', 'up', 'text-status-error'],
    ['cost', 'trend-cost', 'down', 'text-status-success'],
    ['cost', 'trend-cost', 'stable', 'text-status-info'],
  ];

  it.each(cases)('%s (%s) trending %s renders %s', (metric, testId, direction, expected) => {
    renderWith({ [metric]: direction } as Partial<TrendIndicators>);
    expect(arrowColorToken(testId)).toBe(expected);
  });
});
