// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import type { QualityReport } from '../hooks/use-quality-report';

/**
 * Says whether a quality report gated the load or merely described it
 * (p30 R7.3).
 *
 * R7.3 asks for one blanket advisory label — "reported after load, this run's
 * data was written regardless". That is right for four of the five
 * source→target combinations and WRONG for the fifth. Phase 0.2 read all five
 * PySpark templates: exactly one (`glue_job_s3_tables.py.j2`) contains
 * `EvaluateDataQuality`, it runs BEFORE the target write, and it removes
 * failing rows from the frame and quarantines them, so those rows never land.
 * A rule failure still does not fail the job. So neither "written regardless"
 * nor "the run failed" describes that combination, and a single label would be
 * confidently wrong on it.
 *
 * **The mode is derived from the report row, not from the pipeline's target
 * type.** Both producers write the same `(run_id, "data_quality")` state row
 * and the agent's write is a `put_item`, so an on-demand check replaces an
 * inline report. Deriving from `(source_type, target_type)` would label such a
 * row "enforced" when the row on screen was produced by an Athena query after
 * the load. The row's own `source` / `agent_type` is per-run truth; the
 * pipeline config is a statement about generated code.
 *
 * That distinction also covers 0.2's silent-degradation finding: when
 * `awsgluedq` is missing from the job classpath the inline block logs a
 * warning and skips, leaving `_dq_outcomes` empty — and the state-row write is
 * gated on `if _dq_outcomes and _dq_run_id`
 * (`glue_job_s3_tables.py.j2:259`), so no row is written at all. A row
 * carrying `source: "glue_inline_dq"` is therefore evidence that the
 * evaluation actually ran, which the pipeline's configuration cannot tell us.
 */
export type QualityVerdictMode = 'enforced_row_level' | 'advisory' | 'undetermined';

export interface QualityVerdict {
  mode: QualityVerdictMode;
  /** Alert header — the label proper. */
  heading: string;
  /** What ran when, and what that means for the data now in the target. */
  detail: string;
  /** Supporting locator, e.g. the quarantine prefix. Empty when there is none. */
  evidence: string;
  alertType: 'info' | 'warning';
}

/** `source` value the generated Glue job's inline DQ block stamps on its row. */
export const INLINE_DQ_SOURCE = 'glue_inline_dq';

/** `agent_type` value the on-demand data quality agent stamps on its row. */
export const DQ_AGENT_TYPE = 'data_quality';

/**
 * Classify a report by what its own row records.
 *
 * `warning` rather than `info` wherever data that failed a check is sitting in
 * the target: an advisory report that is not passing, a dataset-level rule
 * failure that withheld nothing, and an undetermined producer. The card's own
 * status indicator says whether checks passed; this says whether anyone acted
 * on that.
 */
export function deriveQualityVerdict(report: QualityReport): QualityVerdict {
  if (report.source === INLINE_DQ_SOURCE) {
    const rulesFailed = report.rules_failed ?? 0;
    const rowsQuarantined = report.rows_quarantined;

    // Absent counters are not zero. Inline rows predating P28 R11.1's
    // row-level split carry no `rows_quarantined`, and that script had no
    // quarantine step at all — so claiming rows were withheld would be false,
    // and claiming none failed would be equally unfounded.
    if (rowsQuarantined == null) {
      return {
        mode: 'enforced_row_level',
        heading: 'Evaluated before load — row-level outcome not recorded',
        detail:
          'These rules were evaluated inside the Glue job before the target write, but this run’s record carries no row-level quarantine counters, so whether any failing rows were withheld from the load cannot be determined from it. A rule failure does not fail the run either way.',
        evidence: '',
        alertType: 'warning',
      };
    }

    if (rowsQuarantined > 0) {
      return {
        mode: 'enforced_row_level',
        heading: 'Failing rows were quarantined before load',
        detail: `${rowsQuarantined.toLocaleString()} failing row${rowsQuarantined === 1 ? '' : 's'} ${
          rowsQuarantined === 1 ? 'was' : 'were'
        } removed from this run’s data before the target write, so ${
          rowsQuarantined === 1 ? 'it' : 'they'
        } never landed in the target. The run itself was not blocked — it completed with the remaining rows.`,
        evidence: report.quarantine_path ? `Quarantined rows: ${report.quarantine_path}` : '',
        alertType: 'info',
      };
    }

    if (rulesFailed > 0) {
      return {
        mode: 'enforced_row_level',
        heading: 'Rules failed, but nothing was withheld from the load',
        detail: `${rulesFailed} rule${rulesFailed === 1 ? '' : 's'} failed without any individual row failing — a dataset-level rule such as a row count. Nothing was quarantined, so this run’s data was written as it stood. The run itself was not blocked.`,
        evidence: '',
        alertType: 'warning',
      };
    }

    return {
      mode: 'enforced_row_level',
      heading: 'Evaluated before load',
      detail:
        'These rules were evaluated inside the Glue job before the target write; failing rows would have been quarantined rather than written. No rule failed, so the full result was written. A rule failure would not have failed the run.',
      evidence: '',
      alertType: 'info',
    };
  }

  if (report.agent_type === DQ_AGENT_TYPE) {
    return {
      mode: 'advisory',
      // R7.3's own wording, kept verbatim.
      heading: 'Reported after load — this run’s data was written regardless',
      detail:
        'This report comes from the on-demand data quality agent, which queries the target table after the run has already written to it. Nothing here gated that write: every row this run produced is in the target, including any that fail a check below.',
      evidence: '',
      alertType: report.overall_status === 'pass' ? 'info' : 'warning',
    };
  }

  return {
    mode: 'undetermined',
    heading: 'Evaluation point not recorded',
    detail:
      'This run’s record does not say which producer wrote this report, so whether these rules ran before the load (failing rows withheld) or after it (reported only) cannot be determined from it. Treat the load as ungated unless you can confirm otherwise.',
    evidence: '',
    alertType: 'warning',
  };
}

export interface QualityVerdictLabelProps {
  report: QualityReport;
}

export default function QualityVerdictLabel({ report }: QualityVerdictLabelProps) {
  const verdict = deriveQualityVerdict(report);

  return (
    <Alert type={verdict.alertType} header={verdict.heading} data-testid="quality-verdict-label">
      {/* Deliberately not a SpaceBetween: it re-renders its children as a list,
          and a conditionally-absent child there produces the React
          duplicate-key warning task 2.8 found in MappingViewer. Two Boxes
          carry their own spacing. */}
      <Box variant="small">{verdict.detail}</Box>
      {verdict.evidence ? (
        <Box variant="small" data-testid="quality-verdict-evidence">
          {verdict.evidence}
        </Box>
      ) : null}
    </Alert>
  );
}
