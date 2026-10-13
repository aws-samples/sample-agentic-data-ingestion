// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * MetricsDashboardPage keyboard-accessibility test (P29 R8.1 / R8.3).
 *
 * The stage cards were bare `<div onClick>` (a keyboard trap). They now render
 * through the shared ClickableArea, which gives them `role="button"`,
 * `tabIndex=0`, and Enter/Space activation. This proves the converted
 * stage-card pattern is reachable AND operable with the keyboard end to end
 * (Enter opens the stage detail modal).
 *
 * Requirements: 8.1, 8.3
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ComprehensiveMetrics } from '../../types';

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
    schema_extract: { success_count: 3, error_count: 1, avg_time_ms: 1200 },
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

vi.mock('../../hooks', () => ({
  useMetrics: () => ({ data: MINIMAL_METRICS, isLoading: false, isError: false }),
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

describe('MetricsDashboardPage — stage card keyboard access (R8.1)', () => {
  it('stage cards are focusable and expose button role', () => {
    renderPage();
    const card = screen.getByTestId('stage-schema_extract');
    expect(card).toHaveAttribute('role', 'button');
    expect(card).toHaveAttribute('tabindex', '0');
    expect(card).toHaveAttribute('aria-label', 'View Schema Extraction stage details');
  });

  it('opens the stage detail modal on Enter', () => {
    renderPage();
    expect(screen.queryByText('Success Count:')).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByTestId('stage-schema_extract'), { key: 'Enter' });
    expect(screen.getByText('Success Count:')).toBeInTheDocument();
  });

  it('opens the stage detail modal on Space', () => {
    renderPage();
    fireEvent.keyDown(screen.getByTestId('stage-schema_extract'), { key: ' ' });
    expect(screen.getByText('Success Count:')).toBeInTheDocument();
  });
});
