// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Tests for the quality report's verdict label (p30 task 4.3, R7.3).
 *
 * **Validates: Requirements 7.3**
 *
 * R7.3 asks for one advisory label. Phase 0.2 established that it is wrong for
 * one of the five source→target combinations — `S3_UPLOAD` → S3 Tables
 * evaluates inside the Glue job BEFORE the target write and quarantines failing
 * rows, so they never land, while the run still reports success. Every negative
 * assertion below exists because the label could satisfy R7.3's letter and
 * still say something false: "written regardless" about quarantined rows,
 * "quarantined" about an agent report produced by an Athena query after the
 * load, or a count of withheld rows for a row that records none.
 *
 * Fixtures are literal state rows transcribed from the two producers —
 * `pyspark_templates/glue_job_s3_tables.py.j2:279`–`:290` and
 * `agentcore/data_quality_agent/main.py:912`–`:918` — rather than produced by
 * calling either one. Shape drift between producer and consumer fails
 * silently, and a fixture derived from the producer cannot detect the producer
 * drifting (`LGR-28b4d75-5`).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import QualityVerdictLabel, { deriveQualityVerdict } from '../QualityVerdictLabel';
import DataQualityTab from '../run-detail/DataQualityTab';
import type { QualityReport } from '../../hooks/use-quality-report';

/**
 * `get_quality_report` returns the `(run_id, "data_quality")` state row
 * verbatim with no projection, so a faithful fixture carries keys the frontend
 * type does not declare (`step_name`, `timestamp`, `rules_passed`,
 * `rows_passed`, `total_duration_ms`). The index signature keeps them in the
 * fixture instead of trimming the row down to what the type happens to name.
 */
type StateRow = QualityReport & Record<string, unknown>;

/** Inline producer: rules failed and failing rows were withheld from the load. */
const INLINE_QUARANTINED: StateRow = {
  run_id: 'run-1',
  step_name: 'data_quality',
  pipeline_id: 'pipe-1',
  overall_status: 'fail',
  checks: [
    { name: 'Completeness "customer_id" >= 0.95', status: 'fail', actual: 0.82 },
    { name: 'RowCount > 0', status: 'pass', actual: 1200 },
  ],
  root_cause: 'data_issue',
  recommendations: ['Review failed data quality rules'],
  source: 'glue_inline_dq',
  timestamp: '2026-08-02T10:00:00+00:00',
  rules_passed: 1,
  rules_failed: 1,
  rules_total: 2,
  rows_passed: 1150,
  rows_quarantined: 50,
  quarantine_path: 's3://adi-quarantine/quarantine/run-1/',
};

/** Inline producer, all rules passed — nothing to withhold. */
const INLINE_ALL_PASSED: StateRow = {
  ...INLINE_QUARANTINED,
  overall_status: 'pass',
  checks: [{ name: 'RowCount > 0', status: 'pass', actual: 1200 }],
  root_cause: null,
  recommendations: [],
  rules_passed: 1,
  rules_failed: 0,
  rules_total: 1,
  rows_passed: 1200,
  rows_quarantined: 0,
  quarantine_path: null,
};

/**
 * Inline producer, dataset-level failure: the template only quarantines when
 * failing ROWS exist, so a `RowCount` rule can fail with nothing withheld
 * (`glue_job_s3_tables.py.j2:200`–`:214`).
 */
const INLINE_DATASET_RULE_FAILED: StateRow = {
  ...INLINE_ALL_PASSED,
  overall_status: 'fail',
  checks: [{ name: 'RowCount > 0', status: 'fail', actual: 0 }],
  root_cause: 'data_issue',
  rules_passed: 0,
  rules_failed: 1,
  rows_passed: 0,
  rows_quarantined: 0,
};

/** On-demand agent: Athena queries against the target, i.e. after the load. */
const AGENT_REPORT: StateRow = {
  run_id: 'run-1',
  step_name: 'data_quality',
  agent_type: 'data_quality',
  pipeline_id: 'pipe-1',
  overall_status: 'pass',
  checks: [
    { name: 'row_count', status: 'pass', actual: 1200 },
    { name: 'duplicate_keys', status: 'pass', duplicates: 0 },
  ],
  root_cause: null,
  recommendations: [],
  timestamp: '2026-08-02T11:00:00+00:00',
  total_duration_ms: 4210,
};

describe('deriveQualityVerdict — inline (pre-write) producer', () => {
  it('reports rows withheld before the load, and that the run was not blocked', () => {
    const verdict = deriveQualityVerdict(INLINE_QUARANTINED);

    expect(verdict.mode).toBe('enforced_row_level');
    expect(verdict.heading).toMatch(/quarantined before load/i);
    expect(verdict.detail).toContain('50');
    expect(verdict.detail).toMatch(/never landed/i);
    expect(verdict.detail).toMatch(/not blocked/i);
    expect(verdict.evidence).toContain('s3://adi-quarantine/quarantine/run-1/');
    expect(verdict.alertType).toBe('info');
  });

  it('never tells the user the data was written regardless — R7.3 verbatim would be false here', () => {
    const verdict = deriveQualityVerdict(INLINE_QUARANTINED);

    expect(verdict.heading).not.toMatch(/written regardless/i);
    expect(verdict.detail).not.toMatch(/written regardless/i);
    expect(`${verdict.heading} ${verdict.detail}`).not.toMatch(/after (the )?load/i);
  });

  it('never implies the run failed, either', () => {
    const text = Object.values(deriveQualityVerdict(INLINE_QUARANTINED)).join(' ');

    expect(text).not.toMatch(/run failed|job failed|failed the run/i);
  });

  it('uses singular wording for a single quarantined row', () => {
    const verdict = deriveQualityVerdict({ ...INLINE_QUARANTINED, rows_quarantined: 1 });

    expect(verdict.detail).toContain('1 failing row was');
    expect(verdict.detail).not.toMatch(/rows were/);
  });

  it('states the evaluation point when every rule passed, without claiming anything was withheld', () => {
    const verdict = deriveQualityVerdict(INLINE_ALL_PASSED);

    expect(verdict.mode).toBe('enforced_row_level');
    expect(verdict.heading).toBe('Evaluated before load');
    expect(verdict.detail).toMatch(/would have been quarantined/i);
    expect(verdict.detail).toMatch(/no rule failed/i);
    expect(verdict.alertType).toBe('info');
    expect(verdict.evidence).toBe('');
  });

  it('says nothing was withheld when a dataset-level rule failed with no failing rows', () => {
    const verdict = deriveQualityVerdict(INLINE_DATASET_RULE_FAILED);

    expect(verdict.mode).toBe('enforced_row_level');
    expect(verdict.heading).toMatch(/nothing was withheld/i);
    expect(verdict.detail).toMatch(/dataset-level/i);
    expect(verdict.detail).toMatch(/written as it stood/i);
    // Data that failed a check is in the target, so this is not merely FYI.
    expect(verdict.alertType).toBe('warning');
  });

  it('treats an absent quarantine counter as unknown, not as zero', () => {
    // Inline rows written by a script generated before P28 R11.1's row-level
    // split carry no counters — and that script had no quarantine step, so
    // neither "rows were withheld" nor "nothing failed" is supportable.
    const { rows_quarantined: _dropped, ...withoutCounter } = INLINE_QUARANTINED;
    const verdict = deriveQualityVerdict(withoutCounter as StateRow);

    expect(verdict.mode).toBe('enforced_row_level');
    expect(verdict.heading).toMatch(/not recorded/i);
    expect(verdict.detail).toMatch(/cannot be determined/i);
    expect(verdict.detail).not.toMatch(/were removed|never landed/i);
    expect(verdict.detail).not.toMatch(/no rule failed/i);
    expect(verdict.alertType).toBe('warning');
  });
});

describe('deriveQualityVerdict — on-demand agent (post-load) producer', () => {
  it('carries R7.3’s wording and explains that nothing gated the write', () => {
    const verdict = deriveQualityVerdict(AGENT_REPORT);

    expect(verdict.mode).toBe('advisory');
    expect(verdict.heading).toMatch(/reported after load/i);
    expect(verdict.heading).toMatch(/written regardless/i);
    expect(verdict.detail).toMatch(/after the run has already written/i);
    expect(verdict.detail).toMatch(/nothing here gated that write/i);
  });

  it('never claims rows were quarantined or withheld', () => {
    const text = Object.values(deriveQualityVerdict(AGENT_REPORT)).join(' ');

    expect(text).not.toMatch(/quarantin|withheld/i);
  });

  it('is informational when passing and a warning when not', () => {
    expect(deriveQualityVerdict(AGENT_REPORT).alertType).toBe('info');
    expect(
      deriveQualityVerdict({ ...AGENT_REPORT, overall_status: 'fail' }).alertType,
    ).toBe('warning');
    expect(
      deriveQualityVerdict({ ...AGENT_REPORT, overall_status: 'warning' }).alertType,
    ).toBe('warning');
  });
});

describe('deriveQualityVerdict — provenance the row does not record', () => {
  it('declines to guess when neither producer stamped the row', () => {
    const { source: _s, ...noSource } = INLINE_QUARANTINED;
    const verdict = deriveQualityVerdict(noSource as StateRow);

    expect(verdict.mode).toBe('undetermined');
    expect(verdict.heading).toMatch(/not recorded/i);
    expect(verdict.detail).toMatch(/cannot be determined/i);
    expect(verdict.alertType).toBe('warning');
  });

  it('does not assert either evaluation point for an unstamped row', () => {
    const { source: _s, ...noSource } = INLINE_QUARANTINED;
    const verdict = deriveQualityVerdict(noSource as StateRow);

    // "before the load (failing rows withheld) or after it" names both as
    // possibilities; neither may be stated as fact.
    expect(verdict.detail).not.toMatch(/were removed|never landed/i);
    expect(verdict.detail).not.toMatch(/nothing here gated/i);
  });

  it('lets the inline stamp win if a row somehow carries both', () => {
    // Only the Glue job writes `source: "glue_inline_dq"`, and it writes it on
    // its own row; the agent's put_item replaces the row rather than merging,
    // so this combination should not occur. Pinned so the precedence is a
    // decision rather than an accident of ordering.
    const verdict = deriveQualityVerdict({
      ...INLINE_QUARANTINED,
      agent_type: 'data_quality',
    });

    expect(verdict.mode).toBe('enforced_row_level');
  });
});

describe('QualityVerdictLabel rendering', () => {
  it('renders the heading, detail and quarantine locator', () => {
    render(<QualityVerdictLabel report={INLINE_QUARANTINED} />);

    const label = screen.getByTestId('quality-verdict-label');
    expect(label.textContent).toMatch(/quarantined before load/i);
    expect(label.textContent).toContain('50');
    expect(screen.getByTestId('quality-verdict-evidence').textContent).toContain(
      's3://adi-quarantine/quarantine/run-1/',
    );
  });

  it('renders no locator element when the row records no quarantine path', () => {
    render(<QualityVerdictLabel report={INLINE_ALL_PASSED} />);

    expect(screen.getByTestId('quality-verdict-label')).toBeInTheDocument();
    expect(screen.queryByTestId('quality-verdict-evidence')).not.toBeInTheDocument();
  });
});

describe('the label reaches the surface that renders the report', () => {
  it('appears inside the report card on the Data Quality tab for an agent report', () => {
    render(<DataQualityTab qualityReport={AGENT_REPORT} onRetry={vi.fn()} />);

    const card = screen.getByTestId('quality-report-card');
    const label = screen.getByTestId('quality-verdict-label');
    expect(card).toContainElement(label);
    expect(label.textContent).toMatch(/reported after load/i);
  });

  it('appears inside the report card for an inline report', () => {
    render(<DataQualityTab qualityReport={INLINE_QUARANTINED} onRetry={vi.fn()} />);

    expect(screen.getByTestId('quality-verdict-label').textContent).toMatch(
      /quarantined before load/i,
    );
  });

  it('precedes the overall status, which reads as "the load was stopped" without it', () => {
    render(<DataQualityTab qualityReport={INLINE_QUARANTINED} onRetry={vi.fn()} />);

    const label = screen.getByTestId('quality-verdict-label');
    const status = screen.getByText(/Overall: FAIL/);
    // Node.DOCUMENT_POSITION_FOLLOWING === 4
    expect(label.compareDocumentPosition(status) & 4).toBeTruthy();
  });

  it('renders no label when there is no report', () => {
    render(<DataQualityTab qualityReport={null} onRetry={vi.fn()} />);

    expect(screen.queryByTestId('quality-verdict-label')).not.toBeInTheDocument();
  });
});
