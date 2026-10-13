// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * MetricsDashboardPage — how Worker Type / Number of Workers can be changed (p30 task 5.3).
 *
 * R13.3: WHERE Worker Type and Number of Workers are read-only but named by a cost
 * recommendation, THE UI SHALL state how they can be changed.
 *
 * The statement has to describe the route that actually exists, which is why these
 * assertions pin content and not just presence:
 *
 * - the dashboard has no editor — `router.py` exposes no PUT/PATCH on `glue-job`,
 *   which is also why task 5.2 made the job's Configuration panel read-only;
 * - the recommendation's own Apply button is gated twice over. It renders only for
 *   `risk === 'low' && auto_apply_eligible` (so `upsize` recommendations never get one),
 *   and `apply_cost_recommendation` returns `applied: false, dispatched: false` unless
 *   `AUTO_APPLY_ENABLED` is `"true"` — set by no CDK stack, so off in every deployment
 *   as shipped (`LGR-28b4d75-1`);
 * - the only unconditional route is AWS Glue itself, and it is not durable: the
 *   infrastructure stage re-derives worker sizing from measured data volume on every run
 *   and calls UpdateJob (`job_builders.py::create_glue_job`, whose only carve-out is the
 *   heal rerun's own `job_config_overrides`).
 *
 * A statement that omitted the gating or the overwrite would satisfy the letter of R13.3
 * while reproducing the defect it exists to remove, so the copy is asserted, not just
 * counted.
 *
 * Requirements: 13.3
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ComprehensiveMetrics } from '../../types';
import type { CostRecommendationsResponse } from '../../hooks/use-cost-recommendations';

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

// Shapes transcribed from the agent's own detectors rather than invented:
// detect_over_provisioned (cost_optimization_agent/main.py) emits type "downsize",
// risk "low", auto_apply_eligible true; detect_idle_pipelines emits type "idle" with
// no current/recommended worker fields at all.
const DOWNSIZE_REC = {
  id: 'rec-downsize-1',
  pipeline_id: 'pipeline-a',
  type: 'downsize' as const,
  current: { worker_type: 'G.2X', workers: 10 },
  recommended: { worker_type: 'G.1X', workers: 5 },
  avg_dpu_utilization: 0.2,
  projected_savings_monthly: 120.5,
  risk: 'low' as const,
  auto_apply_eligible: true,
};

// detect_under_provisioned emits risk "medium" / auto_apply_eligible false, so this
// recommendation names Worker Type and Number of Workers and offers no Apply button.
// It is the case where R13.3 matters most.
const UPSIZE_REC = {
  id: 'rec-upsize-1',
  pipeline_id: 'pipeline-b',
  type: 'upgrade' as const,
  current: { worker_type: 'G.1X', workers: 2 },
  recommended: { worker_type: 'G.2X', workers: 4 },
  risk: 'medium' as const,
  auto_apply_eligible: false,
};

const IDLE_REC = {
  id: 'rec-idle-1',
  pipeline_id: 'pipeline-c',
  type: 'idle' as const,
  last_run: '2026-06-01T00:00:00Z',
  recommendation: 'Archive pipeline — no runs in 45 days',
  risk: 'low' as const,
  auto_apply_eligible: false,
};

let costData: CostRecommendationsResponse | undefined;

vi.mock('../../hooks', () => ({
  useMetrics: () => ({ data: MINIMAL_METRICS, isLoading: false, isError: false, refetch: vi.fn() }),
  useTokenUsageMetrics: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useCostRecommendations: () => ({ data: costData, isLoading: false, isError: false, refetch: vi.fn() }),
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

function response(recommendations: CostRecommendationsResponse['recommendations']): CostRecommendationsResponse {
  return {
    generated_at: '2026-08-02T00:00:00Z',
    recommendations,
    total_projected_savings_monthly: 120.5,
  };
}

beforeEach(() => {
  costData = undefined;
});

describe('MetricsDashboardPage — worker config change route (R13.3)', () => {
  it('states the route when a recommendation names Worker Type and Number of Workers', () => {
    costData = response([DOWNSIZE_REC]);
    renderPage();

    const note = screen.getByTestId('worker-config-change-route');
    expect(note).toBeInTheDocument();
    // The recommendation does name the fields, so the precondition for R13.3 holds.
    expect(screen.getByTestId('cost-rec-rec-downsize-1')).toBeInTheDocument();
  });

  it('names the AWS Glue route rather than implying the dashboard can edit the values', () => {
    costData = response([DOWNSIZE_REC]);
    renderPage();

    const text = screen.getByTestId('worker-config-change-route').textContent ?? '';
    expect(text).toContain('aws glue update-job');
    expect(text).toMatch(/glue console/i);
    expect(text).toMatch(/cannot change them/i);
  });

  it('states the auto-apply gate so Apply is not read as an unconditional action', () => {
    costData = response([DOWNSIZE_REC]);
    renderPage();

    const text = screen.getByTestId('worker-config-change-route').textContent ?? '';
    expect(text).toContain('AUTO_APPLY_ENABLED=true');
    expect(text).toMatch(/off by default/i);
    // Apply IS rendered for this recommendation — the statement has to co-exist with it.
    expect(screen.getByTestId('apply-rec-rec-downsize-1')).toBeInTheDocument();
  });

  it('states that a change made in AWS Glue can be overwritten by a later run', () => {
    costData = response([DOWNSIZE_REC]);
    renderPage();

    const text = screen.getByTestId('worker-config-change-route').textContent ?? '';
    expect(text).toMatch(/not\s+durable/i);
    expect(text).toMatch(/overwrite/i);
  });

  it('states the route for an upsize recommendation, which offers no Apply button at all', () => {
    costData = response([UPSIZE_REC]);
    renderPage();

    expect(screen.getByTestId('worker-config-change-route')).toBeInTheDocument();
    expect(screen.queryByTestId('apply-rec-rec-upsize-1')).not.toBeInTheDocument();
  });

  it('omits the statement when only idle recommendations render (they name neither field)', () => {
    costData = response([IDLE_REC]);
    renderPage();

    expect(screen.getByTestId('cost-recommendations')).toBeInTheDocument();
    expect(screen.queryByTestId('worker-config-change-route')).not.toBeInTheDocument();
  });

  it('omits the statement when no recommendations render at all', () => {
    costData = response([]);
    renderPage();

    expect(screen.queryByTestId('worker-config-change-route')).not.toBeInTheDocument();
  });
});
