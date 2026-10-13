// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Self-Healing Activity card must not present a decision as a failure.
 *
 * Two defects, both visible on run-2026-09-22-20-35-04-689a0a7b:
 *
 *   1. `Re-run Status: skipped` rendered with a red error cross. The mapping was
 *      `success ? 'success' : running ? 'in-progress' : 'error'`, so all three
 *      `skipped*` values fell to `error`. A skip is the agent deciding NOT to
 *      re-run — because the error class is not auto-healable, or because a guard
 *      refused — and rendering that as an error makes a correct decision
 *      indistinguishable from a re-run that genuinely broke.
 *   2. The `Re-run` column was blank. `rerun_id` is "" for every skipped
 *      outcome, and the unguarded `<Link>` rendered empty text against an href
 *      ending in `/runs/` — an invisible, clickable dead link.
 *
 * The genuine failures (`failed`, `start_failed`) must KEEP the error severity,
 * and so must an unrecognised value: a status this component does not know about
 * should look wrong rather than quietly reassuring.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import StepFlowTab, { rerunStatusType } from '../StepFlowTab';
import type { RunDetail } from '../../../types';
import type { SelfHealingActivity } from '../../../hooks/use-self-healing';

// The card is the subject; the siblings are not.
vi.mock('../../StepFlowVisualization', () => ({ default: () => <div data-testid="step-flow-viz" /> }));
vi.mock('../../DataPreviewTable', () => ({ default: () => <div data-testid="data-preview" /> }));

const RUN_DETAIL = { run_id: 'run-1', status: 'FAILED', steps: [] } as unknown as RunDetail;

function activity(overrides: Partial<SelfHealingActivity> = {}): SelfHealingActivity {
  return {
    error_classification: 'sql_syntax_error',
    rerun_status: 'skipped',
    rerun_id: '',
    remediation_applied: 'The database rejected the SQL the generated job sent.',
    ...overrides,
  } as SelfHealingActivity;
}

function renderCard(a: SelfHealingActivity) {
  return render(<StepFlowTab runDetail={RUN_DETAIL} pipelineId="pipe-1" runId="run-1" selfHealingActivity={a} />);
}

/* ---------- 1. severity mapping ---------- */

describe('rerunStatusType', () => {
  it.each(['skipped', 'skipped_heal_guard', 'skipped_partial_write_guard'])(
    'maps %s to info, not error — a skip is a decision',
    (status) => {
      expect(rerunStatusType(status)).toBe('info');
    },
  );

  it('maps success to success', () => {
    expect(rerunStatusType('success')).toBe('success');
  });

  it('maps running to in-progress', () => {
    expect(rerunStatusType('running')).toBe('in-progress');
  });

  it.each(['failed', 'start_failed'])('keeps %s as error — these are real failures', (status) => {
    expect(rerunStatusType(status)).toBe('error');
  });

  it('treats an unrecognised status as an error rather than reassuring', () => {
    expect(rerunStatusType('something_new')).toBe('error');
  });
});

/* ---------- 2. rendering ---------- */

describe('Self-Healing Activity card', () => {
  it('renders a skipped re-run without the error cross', () => {
    renderCard(activity({ rerun_status: 'skipped' }));
    const indicator = screen.getByTestId('self-healing-rerun-status');
    expect(indicator).toHaveTextContent('skipped');
    // Cloudscape encodes severity in the rendered status markup; assert the
    // error variant is absent rather than guessing the info class name.
    expect(indicator.querySelector('[class*="status-error"]')).toBeNull();
  });

  it('explains the absent re-run instead of rendering an empty dead link', () => {
    renderCard(activity({ rerun_id: '' }));
    expect(screen.getByTestId('self-healing-no-rerun')).toHaveTextContent(/no re-run was started/i);
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('links to the re-run when one exists', () => {
    renderCard(activity({ rerun_status: 'running', rerun_id: 'run-2' }));
    const link = screen.getByRole('link', { name: 'run-2' });
    expect(link).toHaveAttribute('href', '/pipelines/pipe-1/runs/run-2');
    expect(screen.queryByTestId('self-healing-no-rerun')).toBeNull();
  });

  it('never emits an href ending in /runs/ with no run id', () => {
    renderCard(activity({ rerun_id: '' }));
    for (const a of Array.from(document.querySelectorAll('a'))) {
      expect(a.getAttribute('href')).not.toMatch(/\/runs\/$/);
    }
  });

  it('still shows the classification and the remediation text', () => {
    renderCard(activity());
    expect(screen.getByText('sql_syntax_error')).toBeInTheDocument();
    expect(screen.getByText(/database rejected the SQL/)).toBeInTheDocument();
  });

  it('renders nothing when there is no self-healing activity', () => {
    render(<StepFlowTab runDetail={RUN_DETAIL} pipelineId="pipe-1" runId="run-1" selfHealingActivity={null} />);
    expect(screen.queryByText('Self-Healing Activity')).toBeNull();
  });
});
