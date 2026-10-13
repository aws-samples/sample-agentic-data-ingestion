// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `QualityReportCard` renders p64 R8.1's non-printable character check honestly
 * (task 2d.3).
 *
 * **Validates: Requirements 8.1, 8.4**
 *
 * R8.4 makes a new `checks[]` entry a change to BOTH the response contract and the
 * dashboard's DQ tab. The contract half is
 * `tests/contracts/api_responses/test_quality_report_checks_contract.py`; this is the
 * tab half. The tab (`run-detail/DataQualityTab`) delegates the checks to this card,
 * so the card is where the assertions belong.
 *
 * Two defects this pins, both of which the card had **before** the entry existed:
 *
 * 1. `formatActualValue` treated any name not matching `/rowcount/i` as a 0-1 ratio
 *    and multiplied it by 100. The new entry's `actual` is a ROW COUNT, so three
 *    flagged rows rendered as **"300%"** — a percentage of nothing, presented as a
 *    measurement.
 * 2. `failure_reason` has been on the wire for every failing DQDL rule all along
 *    (Glue's own `FailureReason`) and nothing displayed it. On this entry it carries
 *    the only sentence in the response that says the flagged rows *reached the
 *    target* — "flagged, not quarantined" — so leaving it undisplayed would mean the
 *    honest half of the finding exists and is invisible.
 *
 * The fixture is a literal transcribed from the template's report stage
 * (`_partials/_data_quality.py.j2`) and the `QualityCheck` interface, not something
 * produced by calling the hook: a fixture derived from the producer cannot detect the
 * producer drifting (`LGR-28b4d75-5`).
 *
 * `ExpandableSection` renders its content lazily behind the header button, so each
 * drill-down assertion clicks the header first. That is also why the scorecard
 * assertions (status indicators) and the drill-down assertions are separate tests.
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import QualityReportCard from '../QualityReportCard';
import type { QualityReport } from '../../hooks/use-quality-report';

/** A clean DQDL ruleset with the R8 check reporting three flagged rows. */
const FLAGGED_REPORT: QualityReport = {
  pipeline_id: 'pipe-np',
  run_id: 'run-np',
  // `pass`, deliberately: the verdict is the DQDL ruleset's outcome and a flag is
  // not a rule outcome (2d.1). The card must show a warning row inside a passing
  // report without contradiction.
  overall_status: 'pass',
  checks: [
    { name: 'RowCount > 0', status: 'pass', actual: 1000 },
    { name: 'Completeness "patient_id" >= 0.95', status: 'pass', actual: 0.99, threshold: 0.95 },
    {
      name: 'NonPrintableCharacters',
      status: 'warning',
      actual: 3,
      failure_reason:
        '3 row(s) written to the target contain control characters (flagged, not quarantined)',
    },
  ],
  root_cause: null,
  recommendations: [],
  source: 'glue_inline_dq',
  rules_passed: 2,
  rules_failed: 0,
  rules_total: 2,
  rows_quarantined: 0,
};

function expand(name: string) {
  // The section header is a button carrying the formatted name; clicking it is the
  // only way to reach the content.
  const header = screen.getByRole('button', { name: new RegExp(name, 'i') });
  fireEvent.click(header);
}

describe('QualityReportCard — non-printable character check', () => {
  it('renders the check as a warning, not a failure', () => {
    render(<QualityReportCard report={FLAGGED_REPORT} />);

    const scorecard = screen.getByTestId('quality-check-NonPrintableCharacters');
    expect(scorecard).toBeInTheDocument();
    // Cloudscape's StatusIndicator renders the LABEL as text and encodes the type
    // only in its class (`awsui_status-warning_…`) and its icon, so the type has to
    // be read off the class. Asserted as a substring of the hashed class name —
    // the `status-<type>` segment is the stable part. Both polarities, because
    // `checkStatusType`'s fallthrough branch is `error`: a status value it stopped
    // recognising would render a flag as a failure, which is precisely 2d.1's
    // decision inverted.
    const indicator = scorecard.querySelector('span[class*="status-"]');
    expect(indicator?.className).toContain('status-warning');
    expect(indicator?.className).not.toContain('status-error');
    expect(scorecard.textContent).toContain('Non printable characters');
  });

  it('states the count in rows, never as a percentage', () => {
    render(<QualityReportCard report={FLAGGED_REPORT} />);
    expand('non printable characters');

    expect(screen.getByText(/Actual: 3 rows/)).toBeInTheDocument();
    // The defect this replaced: 3 × 100 rendered as a percentage.
    expect(screen.queryByText(/300%/)).not.toBeInTheDocument();
  });

  it('shows the producer reason, including that the rows were not quarantined', () => {
    render(<QualityReportCard report={FLAGGED_REPORT} />);
    expand('non printable characters');

    const reason = screen.getByTestId('quality-reason-NonPrintableCharacters');
    expect(reason.textContent).toContain('flagged, not quarantined');
    expect(reason.textContent).toContain('3 row(s)');
  });

  it('leaves the overall verdict passing', () => {
    render(<QualityReportCard report={FLAGGED_REPORT} />);

    expect(screen.getByText('Overall: PASS')).toBeInTheDocument();
  });

  it('renders no reason line for a check that did not supply one', () => {
    render(<QualityReportCard report={FLAGGED_REPORT} />);
    expand('row count > 0');

    // The paired negative: the reason line is conditional, so a passing DQDL rule
    // must not acquire an empty "Reason:" row.
    expect(screen.queryByTestId('quality-reason-RowCount > 0')).not.toBeInTheDocument();
  });

  it('still renders a ratio metric as a percentage', () => {
    render(<QualityReportCard report={FLAGGED_REPORT} />);
    expand('patient_id completeness');

    // The row-count branch is narrow on purpose: Completeness/Uniqueness are 0-1
    // ratios and must keep their percentage rendering.
    expect(screen.getByText(/Actual: 99%/)).toBeInTheDocument();
  });

  it('renders a clean check with no count fabrication and no reason', () => {
    const clean: QualityReport = {
      ...FLAGGED_REPORT,
      checks: [{ name: 'NonPrintableCharacters', status: 'pass', actual: 0 }],
    };
    render(<QualityReportCard report={clean} />);
    expand('non printable characters');

    expect(screen.getByText(/Actual: 0 rows/)).toBeInTheDocument();
    expect(screen.queryByTestId('quality-reason-NonPrintableCharacters')).not.toBeInTheDocument();
  });
});

/**
 * The same defect, on the check added two tasks later.
 *
 * **Validates: Requirements 8.4, 9.1**
 *
 * Task 2e.1 added a THIRD count-valued check, `TypeCastFailures`, to
 * `_partials/_data_quality.py.j2` — `actual` is `float(_adi_cast_null_cells)` —
 * without adding it to the card's classification. So 3 cast-failed cells
 * rendered as **"300%"**: precisely the defect the block above pins for
 * `NonPrintableCharacters`, reintroduced, because 2e.1 touched no frontend file
 * and therefore never ran this suite.
 *
 * Two things this asserts that the block above does not:
 *
 * 1. The unit is the producer's noun, **`values`, not `rows`**. The count is of
 *    CELLS (`_adi_cast_null_cells`); every row continues to the target and one
 *    row may contribute several, so "3 rows" would be a different wrong number
 *    rather than a fix.
 * 2. A name that merely STARTS WITH a classified one still falls through to the
 *    ratio branch. That was a property of the anchored regex this replaced, and
 *    it is the property most easily lost by "just make the match looser".
 *
 * The fixture is transcribed from the template's report stage, not produced by
 * calling the hook, for the reason the block above gives (`LGR-28b4d75-5`).
 */
const CAST_REPORT: QualityReport = {
  pipeline_id: 'pipe-cast',
  run_id: 'run-cast',
  overall_status: 'pass',
  checks: [
    {
      name: 'TypeCastFailures',
      status: 'warning',
      actual: 3,
      failure_reason:
        '3 value(s) became NULL converting to the target type (age=2, score=1) — these had a ' +
        'source value and are not empty sources, which the completeness rule cannot distinguish.',
    },
  ],
  root_cause: null,
  recommendations: [],
  source: 'glue_inline_dq',
  rules_passed: 0,
  rules_failed: 0,
  rules_total: 0,
  rows_quarantined: 0,
};

describe('QualityReportCard — type-cast failure count', () => {
  it('states the count in the unit the producer counts, never as a percentage', () => {
    render(<QualityReportCard report={CAST_REPORT} />);
    expand('type cast failures');

    // Cells, not rows: `_adi_cast_null_cells` counts values, and the producer's
    // own reason says "value(s)".
    expect(screen.getByText(/Actual: 3 values/)).toBeInTheDocument();
    expect(screen.queryByText(/300%/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Actual: 3 rows/)).not.toBeInTheDocument();
  });

  it('renders as a warning inside a passing report', () => {
    render(<QualityReportCard report={CAST_REPORT} />);

    const scorecard = screen.getByTestId('quality-check-TypeCastFailures');
    const indicator = scorecard.querySelector('span[class*="status-"]');
    expect(indicator?.className).toContain('status-warning');
    expect(screen.getByText('Overall: PASS')).toBeInTheDocument();
  });

  it('shows the producer reason distinguishing a cast null from an empty source', () => {
    render(<QualityReportCard report={CAST_REPORT} />);
    expand('type cast failures');

    const reason = screen.getByTestId('quality-reason-TypeCastFailures');
    expect(reason.textContent).toContain('became NULL converting to the target type');
    expect(reason.textContent).toContain('are not empty sources');
  });

  it('renders a clean cast count as zero values, not 0%', () => {
    const clean: QualityReport = {
      ...CAST_REPORT,
      checks: [{ name: 'TypeCastFailures', status: 'pass', actual: 0 }],
    };
    render(<QualityReportCard report={clean} />);
    expand('type cast failures');

    expect(screen.getByText(/Actual: 0 values/)).toBeInTheDocument();
    expect(screen.queryByTestId('quality-reason-TypeCastFailures')).not.toBeInTheDocument();
  });

  it('leaves a name that only PREFIXES a classified one on the ratio branch', () => {
    // The property the anchored regex had and the map must keep. A
    // `…Rate` metric is a genuine 0-1 ratio; matching loosely would give it the
    // count branch and print "0.42 values".
    const ratio: QualityReport = {
      ...CAST_REPORT,
      checks: [
        { name: 'TypeCastFailuresRate', status: 'pass', actual: 0.42 },
        { name: 'NonPrintableCharactersRate', status: 'pass', actual: 0.05 },
      ],
    };
    render(<QualityReportCard report={ratio} />);

    expand('type cast failures rate');
    expect(screen.getByText(/Actual: 42%/)).toBeInTheDocument();
    expand('non printable characters rate');
    expect(screen.getByText(/Actual: 5%/)).toBeInTheDocument();
  });
});
