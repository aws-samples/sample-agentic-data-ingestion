// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import QualityVerdictLabel from './QualityVerdictLabel';
import type { QualityReport } from '../hooks/use-quality-report';

function checkStatusType(status: string): 'success' | 'warning' | 'error' {
  if (status === 'pass') return 'success';
  if (status === 'warning') return 'warning';
  return 'error';
}

/** Format a DQDL rule name into a human-readable label. */
function formatRuleName(name: string): string {
  // "RowCount > 0" → "Row count > 0"
  // "Completeness "customer_id" >= 0.95" → "customer_id completeness ≥ 95%"
  // "Uniqueness "email" >= 0.95" → "email uniqueness ≥ 95%"
  const match = name.match(/^(Completeness|Uniqueness|IsComplete|IsPrimaryKey|IsUnique)\s+"([^"]+)"\s*(>=?)\s*(\d+\.?\d*)/i);
  if (match) {
    const [, ruleType, field, op, threshold] = match;
    const pct = (parseFloat(threshold) * 100).toFixed(0);
    const opSymbol = op === '>=' ? '≥' : '>';
    return `${field} ${ruleType.toLowerCase()} ${opSymbol} ${pct}%`;
  }
  // "RowCount > 0" → "Row count > 0"
  return name.replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase().replace(/^\w/, c => c.toUpperCase());
}

/**
 * Check names whose `actual` is an absolute COUNT, mapped to the noun being
 * counted. Anything absent from this map is treated as a 0-1 ratio and
 * multiplied by 100, so a count landing in that branch is rendered as a
 * fabricated percentage — three flagged rows as "300%", a measurement of
 * nothing.
 *
 * The entries and their units come from the producer, not from a guess. THREE
 * are the generated Glue job's, one is the on-demand quality agent's:
 *
 * - `rowcount` — the DQDL `RowCount` rule; rows.
 * - `nonprintablecharacters` — p64 R8.1, `float(_adi_np_rows)`; ROWS
 *   containing control characters (`_partials/_nonprintable_check.py.j2`).
 * - `typecastfailures` — p64 R9.1, `float(_adi_cast_null_cells)`; CELLS, not
 *   rows — every row continues to the target and one row may contribute
 *   several. The producer calls them "value(s)"
 *   (`_partials/_cast_null_check.py.j2`), so that is the noun used here.
 * - `row_count` — the SECOND producer: the on-demand data quality agent's
 *   Athena row count — `int(rows[0]["row_count"])` at
 *   `data_quality_agent/main.py:203`, assigned to `actual` at `:210`; rows.
 *   Snake_case, so it does NOT match the
 *   `rowcount` key above — an agent-reported 1000 rows rendered as "100000%"
 *   until this entry existed. `LGR-f80f6a2-1` left it open with exactly that
 *   note and said to add the agent as a second parsed producer rather than
 *   loosen the match, which is what happened.
 *
 * THE AGENT'S OTHER CHECKS ARE ABSENT ON PURPOSE, not overlooked. Only
 * `row_count` gives its measurement the key `actual`; every other agent check
 * reports under a key of its own and reaches a different render branch —
 * `null_rate_<col>` carries `rate` (`main.py:265`), `duplicate_keys` carries
 * `duplicates` (`:359`), `freshness` carries `latest_record` (`:454`) plus
 * `actual_age_hours` (`:456`), and the plural error-path `null_rates` (`:839`)
 * carries only `error`. None of them reach this function at all, because the
 * `Actual:` row is guarded on `check.actual != null`. Classifying them here
 * would be inventing a unit for a value this formatter never sees. That is also
 * why the default stays RATIO rather than being inverted: the open set landing
 * in the fall-through branch is the DQDL rule names, which arrive from Glue at
 * runtime as `_row["Rule"]` and are 0-1 metrics, so an inverted default would
 * render `Completeness` 0.95 as a count and could never be enumerated by any
 * guard. The agent's dynamic `null_rate_<column>` names, which a per-name map
 * genuinely cannot enumerate, are not an argument against the map here —
 * they carry no `actual`.
 *
 * WHY A MAP AND NOT ONE MORE ALTERNATIVE IN A REGEX. The previous form was
 * `/^(rowcount|nonprintablecharacters)$/i`, and `TypeCastFailures` was added to
 * the producer two tasks after that regex was written without being added here
 * — the same defect, reintroduced, because the producer gained a check and the
 * consumer was not touched. The map is paired with a producer-side guard for
 * EACH of the two producers, both parsing this declaration rather than holding a
 * copy of it — on the PRODUCER's own gate, because a producer-only change never
 * runs `make test-frontend`:
 *
 * - `tests/unit/templates/test_data_quality_all_targets.py` — the generated
 *   Glue job; regex over `_partials/_data_quality.py.j2`.
 * - `tests/unit/agents/data_quality_agent/test_data_quality.py` — the on-demand
 *   agent; an AST walk over `data_quality_agent/main.py` for dict literals
 *   carrying both a `name` and an `actual`, which is the precise condition for
 *   reaching this function.
 *
 * Lookup is an exact match on the whole lowercased identifier, never a
 * substring: a future `NonPrintableCharactersRate` must fall through to the
 * ratio branch rather than silently inherit this one. Do not relax that into a
 * prefix or substring test.
 */
const COUNT_VALUED_CHECKS: Record<string, string> = {
  rowcount: 'rows',
  nonprintablecharacters: 'rows',
  typecastfailures: 'values',
  row_count: 'rows',
};

/** Format an actual metric value — show as percentage if it's a 0-1 ratio. */
function formatActualValue(actual: number, ruleName: string): string {
  // A count is an absolute number, not a ratio. The DQDL rule name carries its
  // threshold ("RowCount > 0"), so compare on the leading identifier only.
  const identifier = ruleName.split(/[\s"]/)[0];
  const unit = Object.prototype.hasOwnProperty.call(COUNT_VALUED_CHECKS, identifier.toLowerCase())
    ? COUNT_VALUED_CHECKS[identifier.toLowerCase()]
    : undefined;
  if (unit !== undefined) {
    return `${actual} ${unit}`;
  }
  // Completeness/Uniqueness metrics are 0-1 ratios
  return `${(actual * 100).toFixed(0)}%`;
}

export interface QualityReportCardProps {
  report: QualityReport;
}

// No "Run Quality Check" action. The on-demand check it triggered was disabled
// on 2026-10-11: the agent received no target table and had no Athena grant,
// so it could only write a failing report over the inline Glue one.
export default function QualityReportCard({ report }: QualityReportCardProps) {
  return (
    <Container
      header={<Header variant="h2">Data Quality Report</Header>}
      data-testid="quality-report-card"
    >
      <SpaceBetween size="m">
        {/* Whether this report gated the load or merely described it (p30 R7.3).
            Above the status indicator on purpose: "Overall: FAIL" reads as
            "the load was stopped" unless the label says otherwise. Needs no
            new prop — both producer discriminators are on `report` itself, so
            nothing has to be plumbed through DataQualityTab. */}
        <QualityVerdictLabel report={report} />

        {/* Overall status */}
        <Box>
          <StatusIndicator type={checkStatusType(report.overall_status)}>
            Overall: {report.overall_status.toUpperCase()}
          </StatusIndicator>
        </Box>

        {/* Pass/fail scorecard */}
        <ColumnLayout columns={Math.min(report.checks.length, 4)} variant="text-grid">
          {report.checks.map((check) => (
            <div key={check.name} data-testid={`quality-check-${check.name}`}>
              <StatusIndicator type={checkStatusType(check.status)}>
                {formatRuleName(check.name)}
              </StatusIndicator>
            </div>
          ))}
        </ColumnLayout>

        {/* Expandable drill-down per check */}
        {report.checks.map((check) => (
          <ExpandableSection
            key={check.name}
            headerText={`${formatRuleName(check.name)} — ${check.status}`}
            data-testid={`quality-expand-${check.name}`}
          >
            <SpaceBetween size="xs">
              {check.expected != null && (
                <Box variant="small">Expected: {check.expected}</Box>
              )}
              {check.actual != null && (
                <Box variant="small">Actual: {formatActualValue(check.actual, check.name)}</Box>
              )}
              {/* The producer's own reason, rendered rather than dropped (p64
                  2d.3). It has been on the wire for every failing DQDL rule all
                  along (Glue's `FailureReason`) and nothing displayed it. R8
                  makes it load-bearing: on the non-printable check it is the
                  only place the report says the flagged rows were "flagged, not
                  quarantined", i.e. that they reached the target. A `warning`
                  row with no explanation is exactly the silent finding p64
                  exists to remove. */}
              {check.failure_reason && (
                <Box variant="small" data-testid={`quality-reason-${check.name}`}>
                  Reason: {check.failure_reason}
                </Box>
              )}
              {check.rate != null && (
                <Box variant="small">Rate: {(check.rate * 100).toFixed(1)}% (threshold: {((check.threshold ?? 0) * 100).toFixed(1)}%)</Box>
              )}
              {check.duplicates != null && (
                <Box variant="small">Duplicates found: {check.duplicates}</Box>
              )}
              {check.latest_record && (
                <Box variant="small">Latest record: {check.latest_record}</Box>
              )}
              {check.sample_nulls && check.sample_nulls.length > 0 && (
                <Box variant="small">Sample nulls: {check.sample_nulls.join(', ')}</Box>
              )}
              {check.sample_duplicates && check.sample_duplicates.length > 0 && (
                <Box variant="small">Sample duplicates: {check.sample_duplicates.join(', ')}</Box>
              )}
            </SpaceBetween>
          </ExpandableSection>
        ))}

        {/* Recommendations */}
        {report.recommendations.length > 0 && (
          <div>
            <Box variant="awsui-key-label">Recommendations</Box>
            {report.recommendations.map((rec, i) => (
              <Box key={i} variant="small" color="text-body-secondary">• {rec}</Box>
            ))}
          </div>
        )}

        {/* Root cause */}
        {report.root_cause && (
          <div>
            <Box variant="awsui-key-label">Root Cause</Box>
            <Box variant="small">{report.root_cause}</Box>
          </div>
        )}
      </SpaceBetween>
    </Container>
  );
}
