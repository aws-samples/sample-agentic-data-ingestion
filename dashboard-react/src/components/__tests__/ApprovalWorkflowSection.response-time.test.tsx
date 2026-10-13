// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * ApprovalWorkflowSection — humanized Avg Response Time (p30 task 5.5 / 5.7).
 *
 * R13.5 (completing p29 R4.2): an approval turnaround is a human wait, so raw
 * milliseconds ("86400000ms") were unreadable. The cell now feeds
 * `computeApprovalMetrics`' mean straight into the shared `humanizeDuration`,
 * behind a `> 0` guard that renders the em dash for "no measurement".
 *
 * Every fixture here sets `response_time_ms` explicitly, because nothing in the
 * repo persists that field onto an approval record yet. With the field absent
 * `computeApprovalMetrics` returns 0 and the cell already rendered `—` before
 * the change — the raw-ms defect was latent, not live. Without an injected
 * value the humanized branch is never entered and the test would assert
 * nothing.
 *
 * The regression guarded against is a reversion to raw ms, so the negative
 * assertions matter as much as the positive ones.
 *
 * Requirements: 13.5, 13.7
 */
import { describe, it, expect } from 'vitest';
import { render, within } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail } from '../../types';

const baseRunDetail: RunDetail = {
  run_id: 'run-rt-001',
  pipeline_key: 'test-pipeline',
  status: 'RUNNING',
  started_at: '2024-06-01T10:00:00Z',
};

/**
 * A decided approval carrying an explicit `response_time_ms` in milliseconds —
 * the unit the API declares and the only in-repo producer computes.
 */
function approvalWithResponseTime(id: string, responseTimeMs?: number): Approval {
  return {
    approval_id: id,
    pipeline_key: 'test-pipeline',
    run_id: 'run-rt-001',
    approval_type: 'schema_mapping',
    status: 'approved',
    created_at: '2024-06-01T10:00:00Z',
    decided_at: '2024-06-02T10:00:00Z',
    decided_by: 'admin',
    gate_name: 'Schema Review',
    ...(responseTimeMs === undefined ? {} : { response_time_ms: responseTimeMs }),
  };
}

function renderWith(approvals: Approval[]) {
  const { container } = render(
    <ApprovalWorkflowSection approvals={approvals} runDetails={baseRunDetail} />,
  );
  return within(container).getByTestId('approval-avg-response');
}

describe('ApprovalWorkflowSection — Avg Response Time is humanized (R13.5)', () => {
  it('renders a day-scale turnaround as "1d", never as raw milliseconds', () => {
    const cell = renderWith([approvalWithResponseTime('a-1', 86_400_000)]);

    expect(cell.textContent).toBe('1d');
    // The exact string the defect produced.
    expect(cell.textContent).not.toContain('86400000');
    expect(cell.textContent).not.toMatch(/\d\s*ms/);
  });

  it('humanizes hour-scale turnaround with two units', () => {
    const cell = renderWith([approvalWithResponseTime('a-1', 8_100_000)]); // 2h 15m
    expect(cell.textContent).toBe('2h 15m');
    expect(cell.textContent).not.toMatch(/\d\s*ms/);
  });

  it('humanizes minute-scale and second-scale turnarounds', () => {
    expect(renderWith([approvalWithResponseTime('a-1', 2_700_000)]).textContent).toBe('45m');
    expect(renderWith([approvalWithResponseTime('a-2', 45_000)]).textContent).toBe('45s');
  });

  it('humanizes the mean across several approvals, not one of them raw', () => {
    // mean(1h, 3h) = 2h
    const cell = renderWith([
      approvalWithResponseTime('a-1', 3_600_000),
      approvalWithResponseTime('a-2', 10_800_000),
    ]);
    expect(cell.textContent).toBe('2h');
    expect(cell.textContent).not.toContain('3600000');
    expect(cell.textContent).not.toContain('10800000');
  });

  it('renders an em dash when no approval records a response time', () => {
    // The live shape today: nothing persists `response_time_ms`, so the mean is
    // 0 and "0s" would read as a measured instant turnaround.
    const cell = renderWith([approvalWithResponseTime('a-1', undefined)]);
    expect(cell.textContent).toBe('—');
  });

  it('renders an em dash for an explicit zero rather than "0s"', () => {
    const cell = renderWith([approvalWithResponseTime('a-1', 0)]);
    expect(cell.textContent).toBe('—');
  });

  it('emits no raw-millisecond string anywhere in the metrics panel', () => {
    const { container } = render(
      <ApprovalWorkflowSection
        approvals={[approvalWithResponseTime('a-1', 86_400_000)]}
        runDetails={baseRunDetail}
      />,
    );
    const metrics = within(container).getByTestId('approval-avg-response').closest('div')!;
    expect(metrics.textContent).not.toMatch(/\d+ms/);
  });
});
