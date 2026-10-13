// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `QualityReportCard` renders the ON-DEMAND QUALITY AGENT's checks honestly.
 *
 * **Validates: Requirements 8.4**
 *
 * `checks[]` has TWO producers. `LGR-f80f6a2-1` classified the generated Glue
 * job's three count-valued checks and guarded them on the template's own gate,
 * and recorded this producer as explicitly still broken: "The on-demand quality
 * agent is one today and its `row_count` still renders as a percentage; add it
 * as a second parsed producer rather than loosening the match."
 *
 * The agent's name is `row_count` — snake_case (`data_quality_agent/main.py:207`)
 * — so it never matched the `rowcount` key that classifies the DQDL `RowCount`
 * rule. `formatActualValue`'s fall-through branch multiplied it by 100, so an
 * agent-reported 1000 rows rendered as **"100000%"**: the same defect as the
 * template-side one, four orders of magnitude louder.
 *
 * WHY ONLY `row_count` IS CLASSIFIED, when the agent reports four checks. Only a
 * check whose dict carries `actual` reaches `formatActualValue` at all — the card
 * guards the `Actual:` row on `check.actual != null`. Every other agent check
 * reports its measurement under a key of its own and reaches a different render
 * branch: `null_rate_<col>` carries `rate` (main.py:265), `duplicate_keys`
 * carries `duplicates` (main.py:359), `freshness` carries `latest_record`
 * (main.py:454). The last three tests below are the paired negatives for that
 * claim — they are the evidence that classifying those names would be inventing
 * a unit for a value this formatter never sees, and the reason the ratio DEFAULT
 * was kept rather than inverted.
 *
 * The fixture is a literal transcribed from the agent's own return dicts and the
 * `QualityCheck` interface, not something produced by calling the hook: a
 * fixture derived from the producer cannot detect the producer drifting
 * (`LGR-28b4d75-5`).
 *
 * `ExpandableSection` renders its content lazily behind the header button, so
 * every drill-down assertion clicks the header first.
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import QualityReportCard from '../QualityReportCard';
import type { QualityReport } from '../../hooks/use-quality-report';

/**
 * One report carrying all four agent checks, each in the shape its own function
 * returns. `agent_type` rather than `source` — that stamp is what makes the
 * verdict label call this advisory (p30 R7.3).
 */
const AGENT_REPORT: QualityReport = {
  pipeline_id: 'pipe-agent',
  run_id: 'run-agent',
  overall_status: 'fail',
  checks: [
    // main.py:210 — `actual` is `int(rows[0]["row_count"])` (:203), an absolute count.
    { name: 'row_count', status: 'fail', expected: 1200, actual: 1000 },
    // main.py:265 — reports `rate`, a 0-1 ratio (SUM(NULL)/COUNT(*), :238), and
    // no `actual`.
    { name: 'null_rate_email', status: 'warning', rate: 0.12, threshold: 0.05 },
    // main.py:359 — reports `duplicates`, a count of duplicated key
    // combinations (`GROUP BY … HAVING COUNT(*) > 1`, :352), and no `actual`.
    { name: 'duplicate_keys', status: 'fail', duplicates: 4 },
    // main.py:454 — reports `latest_record`; its age lives on
    // `actual_age_hours` (:456), which is neither a count nor a ratio and which
    // the frontend type does not declare.
    { name: 'freshness', status: 'pass', latest_record: '2026-09-14 10:00:00' },
  ],
  root_cause: 'transform_issue',
  recommendations: [],
  agent_type: 'data_quality',
};

function expand(name: string) {
  const header = screen.getByRole('button', { name: new RegExp(name, 'i') });
  fireEvent.click(header);
}

describe('QualityReportCard — on-demand agent row count', () => {
  it('states the agent row count in rows, never as a percentage', () => {
    render(<QualityReportCard report={AGENT_REPORT} />);
    expand('row_count');
    expect(screen.getByText(/Actual: 1000 rows/)).toBeInTheDocument();
    // The defect this replaced: 1000 × 100, presented as a measurement.
    expect(screen.queryByText(/100000%/)).not.toBeInTheDocument();
  });

  it('keeps the agent count distinct from the DQDL rule of the same meaning', () => {
    // Both spellings must classify. They are separate keys because the consumer
    // matches the WHOLE lowercased identifier: `row_count` cannot inherit
    // `rowcount`, and making it do so would mean relaxing the exact match that
    // keeps a future `NonPrintableCharactersRate` on the ratio branch.
    const dqdl: QualityReport = {
      ...AGENT_REPORT,
      checks: [{ name: 'RowCount > 0', status: 'pass', actual: 1000 }],
    };
    render(<QualityReportCard report={dqdl} />);
    expand('row count > 0');
    expect(screen.getByText(/Actual: 1000 rows/)).toBeInTheDocument();
  });

  it('still renders a ratio metric as a percentage', () => {
    // The paired positive for keeping the DEFAULT as ratio. The open set landing
    // in the fall-through branch is the DQDL rule names, which arrive from Glue
    // at runtime and are 0-1 metrics; inverting the default would render this
    // 0.99 as a count.
    const ratio: QualityReport = {
      ...AGENT_REPORT,
      checks: [{ name: 'Completeness "email" >= 0.95', status: 'pass', actual: 0.99, threshold: 0.95 }],
    };
    render(<QualityReportCard report={ratio} />);
    expand('email completeness');
    expect(screen.getByText(/Actual: 99%/)).toBeInTheDocument();
  });

  it('renders a null-rate check through the rate branch, with no Actual line', () => {
    render(<QualityReportCard report={AGENT_REPORT} />);
    expand('null_rate_email');
    const section = screen.getByTestId('quality-expand-null_rate_email');
    expect(section.textContent).toContain('Rate: 12.0%');
    // The evidence that this name never reaches `formatActualValue`, and so needs
    // no entry in the classification map.
    expect(section.textContent).not.toContain('Actual:');
  });

  it('renders a duplicate count through the duplicates branch, with no Actual line', () => {
    render(<QualityReportCard report={AGENT_REPORT} />);
    expand('duplicate_keys');
    const section = screen.getByTestId('quality-expand-duplicate_keys');
    expect(section.textContent).toContain('Duplicates found: 4');
    expect(section.textContent).not.toContain('Actual:');
  });

  it('renders freshness through the latest-record branch, with no Actual line', () => {
    render(<QualityReportCard report={AGENT_REPORT} />);
    expand('freshness');
    const section = screen.getByTestId('quality-expand-freshness');
    expect(section.textContent).toContain('Latest record: 2026-09-14 10:00:00');
    expect(section.textContent).not.toContain('Actual:');
  });
});
