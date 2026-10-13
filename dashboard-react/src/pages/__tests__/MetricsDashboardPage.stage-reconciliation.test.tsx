// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Stage Metrics panel says what it cannot account for (p59).
 *
 * WHY THIS FILE EXISTS. The page reported **26 Failed Executions** and a **50.7%
 * success rate** directly above a Stage Metrics panel showing roughly **one**
 * error across all six stages, and left the reader to notice the contradiction.
 * The backend now attributes the failures no stage row recorded — see
 * `handlers/metrics.py` `_attribute_failure_records` and
 * `LGR-9e76249-6` (**template / ratified**), which records the RecordFailure
 * sentinel as the only carrier of a run's terminal FAILED status when the failing
 * step never wrote a row. What it cannot attribute to one of the six rendered
 * stages arrives in `performance.stage_failure_attribution`, and this file pins
 * that the page SAYS it rather than dropping it.
 *
 * WHAT IS PINNED, AND WHAT IS NOT. The assertions read numbers and testIds, not
 * sentences. Copy is copy — `LGR-99050e6-1` is explicit that a label "can be
 * reworded without changing what the number means" — so what is pinned is that
 * the figure appears and that the note is present at all, never the exact
 * phrasing. The one thing asserted about wording is that the affected step is
 * NAMED, because a bare total ("8 failures elsewhere") is the version of this
 * note that tells an operator nothing.
 *
 * The three-state arm is the load-bearing case: a failure-record read that could
 * not be made arrives as `null`, and silence there would put the panel straight
 * back to implying its counts are complete. `LGR-4854b3d-1` (**template /
 * ratified**) is the fence — a three-state value is never collapsed to one of its
 * ends.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ComprehensiveMetrics, StageFailureAttribution } from '../../types';

const EMPTY_STAGE = { success_count: 0, error_count: 0, avg_time_ms: null };

/** A complete attribution with nothing to report — every count reconciled. */
const RECONCILED: StageFailureAttribution = {
  failure_records: 3,
  attributed_to_stage: 3,
  counted_into_stage_errors: 0,
  already_counted_by_stage_row: 3,
  unattributed: 0,
  unattributed_by_step: {},
  rejected_at_approval: 0,
  rejected_by_step: {},
  failed_executions: 3,
  failed_executions_without_a_record: 0,
};

function metricsWith(
  attribution: StageFailureAttribution | null,
  determinedFrom: Record<string, string> = {},
): ComprehensiveMetrics {
  return {
    performance: {
      lines_ingested_24h: null,
      processing_rate: null,
      overall_success_rate: 50.7,
      failed_pipelines: 26,
      rejected_at_approval: attribution === null ? null : attribution.rejected_at_approval,
      agent_latency_ms: null,
      monthly_cost: null,
      monthly_cost_basis: null,
      terminal_counts: null,
      stage_failure_attribution: attribution,
      measurement: {},
      determined_from: determinedFrom,
      missing: attribution === null ? ['stage_failure_attribution'] : [],
      trends: { ingestion: 'stable', success: 'stable', agent_latency: 'stable', cost: 'stable' },
    },
    stages: {
      schema_extract: { success_count: 52, error_count: 0, avg_time_ms: null },
      schema_mapping: { success_count: 42, error_count: 1, avg_time_ms: null },
      code_generation: EMPTY_STAGE,
      infrastructure: EMPTY_STAGE,
      quality_checks: EMPTY_STAGE,
      pipeline_run: { success_count: 31, error_count: 0, avg_time_ms: null },
    },
    infrastructure: {
      agentcore: { availability: null, response_time_ms: null, status: 'unknown' },
      dynamodb: { read_capacity_per_hour: 0, write_capacity_per_hour: 0, status: 'healthy' },
      s3: { storage_used_bytes: null, requests_per_minute: null, status: 'unknown' },
      lambda: { invocations_per_minute: 0, error_rate: 0, status: 'healthy' },
    },
  } as unknown as ComprehensiveMetrics;
}

let currentMetrics: ComprehensiveMetrics = metricsWith(RECONCILED);

vi.mock('../../hooks', () => ({
  useMetrics: () => ({ data: currentMetrics, isLoading: false, isError: false, refetch: vi.fn() }),
  useTokenUsageMetrics: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useCostRecommendations: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useApplyRecommendation: () => ({ mutate: vi.fn(), isPending: false }),
}));

import MetricsDashboardPage from '../MetricsDashboardPage';

function renderWith(
  attribution: StageFailureAttribution | null,
  determinedFrom: Record<string, string> = {},
) {
  currentMetrics = metricsWith(attribution, determinedFrom);
  return render(
    <MemoryRouter>
      <MetricsDashboardPage />
    </MemoryRouter>,
  );
}

/** The reconciliation note's text, or `null` when the page renders none. */
function noteText(): string | null {
  const note = screen.queryByTestId('stage-failure-reconciliation');
  return note ? (note.textContent ?? '') : null;
}

beforeEach(() => {
  currentMetrics = metricsWith(RECONCILED);
});

describe('MetricsDashboardPage — the stage panel reconciles with Failed Executions', () => {
  it('says nothing when every failure is already inside a stage count', () => {
    renderWith(RECONCILED);
    expect(noteText()).toBeNull();
  });

  it('names the failures that belong to no rendered stage, and the step they hit', () => {
    // The screenshot's shape: 21 runs reached mapping and never reached the Glue
    // run, with almost no recorded stage errors. Those runs died at the code
    // approval gate and in test_code — neither is one of the six stages.
    renderWith({
      failure_records: 21,
      attributed_to_stage: 0,
      counted_into_stage_errors: 0,
      already_counted_by_stage_row: 0,
      unattributed: 21,
      unattributed_by_step: { check_code_approval: 14, test_code: 7 },
      rejected_at_approval: 0,
      rejected_by_step: {},
      failed_executions: 26,
      failed_executions_without_a_record: 5,
    });

    const text = noteText();
    expect(text, 'a panel with 21 unattributable failures must say so').not.toBeNull();
    expect(text).toContain('21');
    // The breakdown, not just a total: which step, and how many. A bare total
    // tells an operator nothing about where to look.
    expect(text).toMatch(/approval/i);
    expect(text).toContain('14');
    expect(text).toContain('7');
  });

  it('reports failed executions that left no failure record at all', () => {
    renderWith({
      failure_records: 4,
      attributed_to_stage: 4,
      counted_into_stage_errors: 4,
      already_counted_by_stage_row: 0,
      unattributed: 0,
      unattributed_by_step: {},
      rejected_at_approval: 0,
      rejected_by_step: {},
      failed_executions: 26,
      failed_executions_without_a_record: 22,
    });

    const text = noteText();
    expect(text).not.toBeNull();
    expect(text).toContain('22');
  });

  it('reports the residual in the other direction rather than hiding it', () => {
    // More failure records than failed executions. Real, because the two are read
    // from different systems over the same window. Clamping it to zero would be
    // the collapse `LGR-4854b3d-1` forbids, in the field whose job is to admit
    // the arithmetic does not close.
    renderWith({
      failure_records: 8,
      attributed_to_stage: 8,
      counted_into_stage_errors: 8,
      already_counted_by_stage_row: 0,
      unattributed: 0,
      unattributed_by_step: {},
      rejected_at_approval: 0,
      rejected_by_step: {},
      failed_executions: 5,
      failed_executions_without_a_record: -3,
    });

    const text = noteText();
    expect(text).not.toBeNull();
    // The magnitude is shown; the minus sign is not printed at the reader.
    expect(text).toContain('3');
    expect(text).not.toContain('-3');
  });

  it('says the counts came partly from failure records when they did', () => {
    renderWith({
      failure_records: 6,
      attributed_to_stage: 6,
      counted_into_stage_errors: 6,
      already_counted_by_stage_row: 0,
      unattributed: 0,
      unattributed_by_step: {},
      rejected_at_approval: 0,
      rejected_by_step: {},
      failed_executions: 6,
      failed_executions_without_a_record: 0,
    });

    const text = noteText();
    expect(text).not.toBeNull();
    expect(text).toContain('6');
  });

  it('says the counts are a floor when the failure-record read could not be made', () => {
    // The three-state arm. `null` is "we could not look", which is NOT "no run
    // failed outside a stage row" — and silence here would restore the very
    // implication this change removes.
    renderWith(null, { stage_failure_attribution: 'pipeline_failure_records_incomplete' });

    const text = noteText();
    expect(text, 'an unreadable failure partition must be stated, not passed over').not.toBeNull();
    // The reason travels with it, so the note says what was looked at.
    expect(text).toContain('pipeline_failure_records_incomplete');
  });

  it('still states the incompleteness when the payload carries no reason for it', () => {
    renderWith(null, {});
    expect(noteText()).not.toBeNull();
  });

  it('says the counts cannot be checked when the execution totals were not read', () => {
    renderWith({
      failure_records: 2,
      attributed_to_stage: 2,
      counted_into_stage_errors: 2,
      already_counted_by_stage_row: 0,
      unattributed: 0,
      unattributed_by_step: {},
      rejected_at_approval: 0,
      rejected_by_step: {},
      failed_executions: null,
      failed_executions_without_a_record: null,
    });

    const text = noteText();
    expect(text).not.toBeNull();
    expect(text).toMatch(/not read|cannot be checked/i);
  });

  it('leaves the six stage tiles and their figures untouched', () => {
    // The note is additive. The panel still renders exactly the six stages with
    // the counts the payload carries — no seventh tile for the unattributable
    // failures, which would look like a pipeline stage and be a bucket.
    renderWith({
      failure_records: 21,
      attributed_to_stage: 0,
      counted_into_stage_errors: 0,
      already_counted_by_stage_row: 0,
      unattributed: 21,
      unattributed_by_step: { check_code_approval: 21 },
      rejected_at_approval: 0,
      rejected_by_step: {},
      failed_executions: 26,
      failed_executions_without_a_record: 5,
    });

    for (const key of [
      'schema_extract',
      'schema_mapping',
      'code_generation',
      'infrastructure',
      'quality_checks',
      'pipeline_run',
    ]) {
      expect(screen.getByTestId(`stage-${key}`)).toBeTruthy();
    }
    expect(screen.getByTestId('stage-schema_extract-success').textContent).toContain('52');
    expect(screen.getByTestId('stage-schema_mapping-error').textContent).toContain('1');
    expect(screen.getByTestId('stage-pipeline_run-success').textContent).toContain('31');
  });

  it('carries the note in text, not in a colour or an icon alone', () => {
    // WCAG 1.4.1 applied one layer down (`LGR-99050e6-1`'s ground): every
    // statement is a full sentence carrying its own number, so the note survives
    // the Alert's hue and its status icon being unavailable to the reader.
    renderWith(null, { stage_failure_attribution: 'pipeline_failure_records_incomplete' });
    const text = noteText() ?? '';
    expect(text.trim().length, 'the note must carry its meaning in words').toBeGreaterThan(40);
  });
});

describe('MetricsDashboardPage — approval rejections are not failures (2026-10-10)', () => {
  // The 7-day window the user reported: 3 runs rejected at schema-mapping
  // approval and 1 real failure at create_glue_pipeline. The backend sets the
  // rejections aside, so the failure fields carry only the one failure.
  const USER_WINDOW: StageFailureAttribution = {
    failure_records: 1,
    attributed_to_stage: 0,
    counted_into_stage_errors: 0,
    already_counted_by_stage_row: 0,
    unattributed: 1,
    unattributed_by_step: { create_glue_pipeline: 1 },
    rejected_at_approval: 3,
    rejected_by_step: { check_schema_mapping_approval: 3 },
    failed_executions: 1,
    failed_executions_without_a_record: 0,
  };

  it('says the rejections apart from the failure, naming the gate', () => {
    renderWith(USER_WINDOW);
    const text = noteText() ?? '';
    expect(text).toContain('Create Glue Pipeline (1)');
    expect(text).toContain('Check Schema Mapping Approval (3)');
    expect(text).toMatch(/3 runs were rejected/);
    expect(text).toMatch(/1 failure happened/);
  });

  it('still speaks when rejections are the only thing the panel cannot show', () => {
    renderWith({
      ...RECONCILED,
      rejected_at_approval: 2,
      rejected_by_step: { check_code_approval: 2 },
    });
    const text = noteText() ?? '';
    expect(text).toContain('Check Code Approval (2)');
    expect(text).not.toMatch(/failures? happened/);
  });

  it('shows the rejected count on its own tile', () => {
    renderWith(USER_WINDOW);
    expect(screen.getByTestId('metric-rejected-at-approval').textContent).toContain('3');
  });

  it('says Not established on the tile when the failure records could not be read, never 0', () => {
    renderWith(null, { rejected_at_approval: 'pipeline_failure_records_incomplete' });
    const tile = screen.getByTestId('metric-rejected-at-approval').textContent ?? '';
    expect(tile).toContain('Not established');
    expect(tile).not.toMatch(/\b0\b/);
  });
});
