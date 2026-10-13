// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * MetricsDashboardPage widget Async_State_Triple tests (P29 R1.4).
 *
 * The AI Token Usage and Cost Recommendations widgets consume their own
 * queries; on failure they must surface an error + Retry rather than
 * silently disappearing. (The main comprehensive-metrics query + MOCK_METRICS
 * removal are covered by task 3.4.)
 *
 * Requirements: 1.4
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ComprehensiveMetrics } from '../../types';

// Minimal, valid metrics so the page renders past its own loading/error gate.
const MINIMAL_METRICS = {
  performance: {
    lines_ingested_24h: 0,
    processing_rate: 0,
    overall_success_rate: 0,
    failed_pipelines: 0,
    agent_latency_ms: 0,
    monthly_cost: 0,
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
    agentcore: { availability: 0, response_time_ms: 1, status: 'healthy' },
    dynamodb: { read_capacity_per_hour: 0, write_capacity_per_hour: 0, status: 'healthy' },
    s3: { storage_used_bytes: 0, requests_per_minute: 0, status: 'healthy' },
    lambda: { invocations_per_minute: 0, error_rate: 0, status: 'healthy' },
  },
} as unknown as ComprehensiveMetrics;

let tokenIsError = false;
let costIsError = false;
const mockRefetchToken = vi.fn();
const mockRefetchCost = vi.fn();

vi.mock('../../hooks', () => ({
  useMetrics: () => ({ data: MINIMAL_METRICS, isLoading: false, isError: false }),
  useTokenUsageMetrics: () => ({ data: undefined, isLoading: false, isError: tokenIsError, refetch: mockRefetchToken }),
  useCostRecommendations: () => ({ data: undefined, isLoading: false, isError: costIsError, refetch: mockRefetchCost }),
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
  tokenIsError = false;
  costIsError = false;
  mockRefetchToken.mockReset();
  mockRefetchCost.mockReset();
});

describe('MetricsDashboardPage — widget Async_State_Triple (R1.4)', () => {
  it('AI Token Usage widget renders an error + Retry on failure', () => {
    tokenIsError = true;
    renderPage();
    expect(screen.getByTestId('token-usage-error')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('token-usage-retry'));
    expect(mockRefetchToken).toHaveBeenCalledTimes(1);
  });

  it('Cost Recommendations widget renders an error + Retry on failure', () => {
    costIsError = true;
    renderPage();
    expect(screen.getByTestId('cost-recommendations-error')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('cost-recommendations-retry'));
    expect(mockRefetchCost).toHaveBeenCalledTimes(1);
  });

  it('does not render widget errors when the queries succeed', () => {
    renderPage();
    expect(screen.queryByTestId('token-usage-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('cost-recommendations-error')).not.toBeInTheDocument();
  });
});
