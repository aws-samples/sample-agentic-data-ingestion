// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { memo } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import type { LoadOutcome } from '../types';
import type { QualityReport } from '../hooks/use-quality-report';
import { INLINE_DQ_SOURCE } from './QualityVerdictLabel';

/**
 * What reached the target on this run, stated on the run detail page (p37 R1.8).
 *
 * ## This is a SIBLING of `QualityVerdictLabel`, not a fifth branch of it
 *
 * The two answer different questions and the separation is deliberate:
 *
 * - `QualityVerdictLabel` answers **did anything gate the write** — were rules
 *   evaluated before the target write, and did they withhold anything.
 * - This answers **what reached the target** — how many rows were read, how many
 *   were written, and where the difference went.
 *
 * That is the same separation `shared/quality_verdict.py`'s module docstring
 * draws ("This record describes the generated code, not the run") and
 * `LGR-eee8ac8-6` (**template / ratified**) ratifies. `requirements.md` §Current
 * state lists "the dashboard already distinguishes gated from ungated quality
 * reports" under **do NOT reimplement**, and forbids building a second
 * **quality** label. A **load** label is not that: a run can lose every row to a
 * transform filter with no quality report in existence, and four of the five
 * PySpark templates have no `EvaluateDataQuality` block at all. So
 * `QualityVerdictLabel` is untouched — no branch was added to it, nothing was
 * merged into it — and the reason is written down here so a reviewer does not
 * have to reconstruct it.
 *
 * ## R1.8's second half: do not point at nothing
 *
 * Stating that rows were withheld and stopping there is the failure this
 * component exists to avoid, and so is rendering an empty location. There are
 * exactly **two** location arms:
 *
 * 1. `quarantine_prefix` — the run's DQ report carries `quarantine_path`
 *    (`glue_job_s3_tables.py.j2:309`, already rendered as evidence at
 *    `QualityVerdictLabel.tsx:92`). Name the S3 prefix.
 * 2. `no_location_recorded` — it does not. Say so **explicitly**: the rows were
 *    dropped by the transform rather than quarantined, and no location was
 *    recorded, so there is nothing to inspect.
 *
 * **Arm 2 is the common case, not the edge case.** `quarantine_path` exists only
 * for the inline-DQ case: `glue_job_s3_tables.py.j2` is the only DQ-bearing
 * template, and only when the generated DQDL was non-empty. Every other
 * template can drop every row in its transform with no quality report written at
 * all. So arm 2 is built as a first-class arm with its own wording rather than as
 * a fallback.
 *
 * Arm 2 has two wordings and the difference is honesty, not style. When there is
 * no quarantine evidence at all, the rows were dropped by the transform and the
 * label says so. When the DQ report reports rows withheld by the quality check
 * but carries no path, claiming the transform dropped them would be false —
 * `LGR-99050e6-2` (**template / ratified**) settled that an absent quarantine
 * counter is reported as **unknown rather than as zero**, and the same restraint
 * applies to an absent path. Both wordings state that no location was recorded,
 * which is what R1.8 requires.
 *
 * ## Which outcomes render, and why that is not the same set as the run list
 *
 * | outcome | rendered | severity |
 * |---|---|---|
 * | `no_rows_loaded` | counts + location arm | `warning` |
 * | `partial_load` | counts + withheld count + location arm | `info` |
 * | `empty_source` | counts, naming the source | `info` |
 * | `complete_load` | counts | `info` |
 * | `not_established` | **nothing** | — |
 *
 * `LoadOutcomeIndicator` renders nothing for `complete_load`, because a run
 * list's status cell already carries a success indicator inches away. A run
 * detail page has no such neighbour and stating the counts is the whole point of
 * the surface, so `complete_load` is stated here. That is a deliberate
 * divergence in *what is shown*, never in **severity**: every outcome carries the
 * same severity the indicator gives it, so the two surfaces cannot disagree
 * about how alarming a run is.
 *
 * `not_established` renders nothing, exactly as the indicator does. The Glue
 * job's counts write is best-effort by necessity — a telemetry failure must not
 * fail a job that has already written to the target — so it is a reachable steady
 * state rather than a migration artefact (design §5 risk 3), and it must read as
 * absence rather than as an error.
 */

/** Cloudscape `Alert` types this label uses. Deliberately no `error`. */
export type LoadOutcomeLabelSeverity = 'info' | 'warning';

/** The two location arms R1.8's second half requires. */
export type LoadOutcomeWithheldLocation = 'quarantine_prefix' | 'no_location_recorded';

export interface LoadOutcomeStatement {
  severity: LoadOutcomeLabelSeverity;
  /** Alert header — the label proper. */
  heading: string;
  /**
   * Rows_Read and Rows_Written, in text. Never conveyed by colour or icon alone
   * (WCAG 1.4.1) — the rule `LGR-99050e6-1` (**template / ratified**) settled for
   * the metrics tiles.
   *
   * Provenance-aware: under `dq_report_row_lower_bound` Rows_Read is stated as a
   * lower bound rather than as a count, because that is what it is. See
   * `describeCounts`.
   */
  counts: string;
  /**
   * Which location arm applies, or `null` when no rows were withheld
   * (`complete_load`, `empty_source`) and there is therefore no location to
   * state.
   */
  location: LoadOutcomeWithheldLocation | null;
  /** Where the unwritten rows went. Always non-empty. */
  withheld: string;
  /**
   * Corroborating detail from the run's DQ report, or `''` when it carries none.
   * Never the source of a count — see `describeLoadOutcomeLabel`. Also `''`
   * under `dq_report_row_lower_bound`, where `rows_passed` is not an independent
   * witness but the origin of `rows_written` — see `describeCorroboration`.
   */
  corroboration: string;
  testId: string;
}

/** `n` rows, or "not recorded" for a count the wire did not carry. */
function formatRows(count: number | null | undefined): string {
  if (typeof count !== 'number' || !Number.isFinite(count)) return 'an unrecorded number of';
  return count.toLocaleString();
}

/** "1 row" / "2 rows", with the count already localised. */
function pluralRows(count: number): string {
  return `${count.toLocaleString()} row${count === 1 ? '' : 's'}`;
}

/**
 * Where the rows that were not written went — R1.8's second half, both arms.
 *
 * `notWritten` comes from the deriver's `rows_not_written`, which is read and
 * never recomputed from the two counts: one deriver owns that arithmetic
 * (R1.10).
 */
function describeWithheld(
  notWritten: number,
  report: QualityReport | null | undefined,
): { location: LoadOutcomeWithheldLocation; withheld: string } {
  const quarantinePath =
    typeof report?.quarantine_path === 'string' && report.quarantine_path.trim() !== ''
      ? report.quarantine_path
      : null;

  // Arm 1 — a concrete prefix to point at.
  if (quarantinePath) {
    return {
      location: 'quarantine_prefix',
      withheld:
        `The ${pluralRows(notWritten)} that ${notWritten === 1 ? 'was' : 'were'} not written ` +
        `${notWritten === 1 ? 'was' : 'were'} quarantined by the data quality check before the ` +
        `target write. Quarantined rows: ${quarantinePath}`,
    };
  }

  // Arm 2 — no prefix. The report says rows were withheld by the quality check,
  // or carries no counter at all on an inline row, so the transform cannot be
  // named as the cause. Absent is reported as unknown, not as zero
  // (`LGR-99050e6-2`, template / ratified).
  const quarantineCounter = report?.source === INLINE_DQ_SOURCE ? report.rows_quarantined : 0;
  const quarantineEvidence =
    quarantineCounter == null || (typeof quarantineCounter === 'number' && quarantineCounter > 0);

  if (quarantineEvidence) {
    return {
      location: 'no_location_recorded',
      withheld:
        'No quarantine location was recorded for this run. This run’s data quality record reports ' +
        `rows withheld by the quality check but carries no location for them, so there is no ` +
        `prefix to inspect and the ${pluralRows(notWritten)} cannot be attributed to the ` +
        'transform either.',
    };
  }

  // Arm 2, the common wording: nothing quarantined anything, so the transform
  // dropped them.
  return {
    location: 'no_location_recorded',
    withheld:
      `The ${pluralRows(notWritten)} that ${notWritten === 1 ? 'was' : 'were'} not written ` +
      `${notWritten === 1 ? 'was' : 'were'} dropped by the transform rather than quarantined, ` +
      'and no location was recorded for ' +
      `${notWritten === 1 ? 'it' : 'them'}. There is no prefix to inspect.`,
  };
}

/**
 * Rows_Read and Rows_Written in text — hedged when Rows_Read is a lower bound.
 *
 * Under `dq_report_row_lower_bound` (p37 C5) the run has no `load_metrics` row
 * and `rows_read` is the **quarantine count** off its DQ report, which is a
 * lower bound on Rows_Read rather than Rows_Read: the transform filter and the
 * quarantine both reduce the frame and neither pre-transform count was ever
 * written, so the true figure can only be higher (`shared/load_outcome.py`'s
 * `FROM_DQ_REPORT_ROW_LOWER_BOUND`; `LGR-9e76249-8`, **template / ratified**).
 * Stating it as `This run read N rows from the source` would present a floor as
 * a measurement — the opposite of the restraint `LGR-99050e6-2` (**template /
 * ratified**) settled for an absent quarantine counter, which is reported as
 * unknown rather than as zero.
 *
 * The hedged wording is deliberately parallel to the deriver's own `reason`
 * string for the same case, so the run list's hover text and this body cannot
 * contradict each other about the same run. That is the `LGR-99050e6-1`
 * (**template / ratified**) rule applied to provenance rather than to severity:
 * the caveat is carried by the words, not by an icon.
 *
 * Every other provenance gets the exact original sentence. So does the
 * lower-bound case when `rows_read` is not a usable number — there is then no
 * figure to bound, and `formatRows` already says the count was not recorded.
 */
function describeCounts(outcome: LoadOutcome): string {
  const rowsRead = outcome.rows_read;
  const rowsWritten = outcome.rows_written;

  const exact =
    `This run read ${formatRows(rowsRead)} row${rowsRead === 1 ? '' : 's'} from the source and ` +
    `wrote ${formatRows(rowsWritten)} row${rowsWritten === 1 ? '' : 's'} to the target.`;

  if (
    outcome.determined_from !== 'dq_report_row_lower_bound' ||
    typeof rowsRead !== 'number' ||
    !Number.isFinite(rowsRead)
  ) {
    return exact;
  }

  return (
    `This run wrote ${formatRows(rowsWritten)} row${rowsWritten === 1 ? '' : 's'} to the target. ` +
    `At least ${pluralRows(rowsRead)} ${rowsRead === 1 ? 'was' : 'were'} read from the source — a ` +
    `lower bound taken from the ${pluralRows(rowsRead)} this run’s data quality record ` +
    'quarantined, not a recorded count, so the true number read may be higher.'
  );
}

/**
 * Corroborating detail from the DQ report's `rows_passed`, or `''`.
 *
 * **`rows_passed` is NOT a general Rows_Written source and is never used as one
 * here.** It *is* Rows_Written for the inline-DQ case — the template writes
 * `"rows_passed": transformed_count` (`glue_job_s3_tables.py.j2:307`) and
 * `requirements.md` §Current state states the identity, `df_transformed` being
 * the Passed split — but that is one of five template/target combinations, which
 * is exactly the mistake R1.11 exists to prevent. The Load_Outcome's own counts
 * are the source of every number this label states; this figure only corroborates
 * them, and when the two disagree the label says so rather than picking one.
 *
 * Gated on the inline producer's own stamp, because the on-demand agent writes
 * the same `(run_id, "data_quality")` key by `put_item` and does not write
 * `rows_passed` at all.
 */
function describeCorroboration(
  outcome: LoadOutcome,
  report: QualityReport | null | undefined,
): string {
  // p37 C5: on this path `rows_passed` is not an independent witness — it **is**
  // the origin of `rows_written`. `_load_outcome_lower_bound_from_dq_row` reads
  // the DQ row's `rows_passed` into the count it passes as `rows_written`
  // (`handlers/pipelines_runs.py:190`, `:196-198`), so re-reading
  // `report.rows_passed` here and calling the agreement *independent* would
  // present one figure off one row as two concurring sources. The established
  // lower-bound case additionally requires `rows_passed == 0`, so the two are
  // equal by construction and the claim would fire on every such run. There is
  // nothing to corroborate, and the counts sentence already names the DQ record
  // as the origin — so say nothing rather than restate it more weakly.
  if (outcome.determined_from === 'dq_report_row_lower_bound') return '';

  const rowsWritten = outcome.rows_written;
  if (!report || report.source !== INLINE_DQ_SOURCE) return '';
  const rowsPassed = report.rows_passed;
  if (typeof rowsPassed !== 'number' || !Number.isFinite(rowsPassed)) return '';

  if (rowsPassed === rowsWritten) {
    return (
      `This run’s data quality record independently reports ${pluralRows(rowsPassed)} passing to ` +
      'the target write, which matches the count above.'
    );
  }

  return (
    `This run’s data quality record reports ${pluralRows(rowsPassed)} passing its quality check, ` +
    `which does not match the ${formatRows(rowsWritten)} recorded as written. The counts above ` +
    'are this run’s own load record; the quality figure is corroborating detail only.'
  );
}

/**
 * Resolve the run-detail statement for one run's Load_Outcome.
 *
 * Pure, and exported beside the component so the mapping is unit-testable
 * without rendering — the same shape `deriveQualityVerdict` and
 * `describeLoadOutcome` use. Returns `null` whenever nothing should be rendered,
 * so a caller can mount the component unconditionally and let it decide.
 *
 * `report` is optional: the run's DQ report supplies the quarantine locator and
 * the corroborating count when it exists, and its absence is a first-class input
 * rather than a missing prerequisite.
 */
export function describeLoadOutcomeLabel(
  outcome: LoadOutcome | null | undefined,
  report?: QualityReport | null,
): LoadOutcomeStatement | null {
  if (!outcome) return null;

  // Read, never recomputed from the two counts (R1.10). Guarded only against a
  // wire value that is not a usable number, never re-derived.
  const notWritten =
    typeof outcome.rows_not_written === 'number' && Number.isFinite(outcome.rows_not_written)
      ? outcome.rows_not_written
      : 0;

  const counts = describeCounts(outcome);
  const corroboration = describeCorroboration(outcome, report);

  switch (outcome.outcome) {
    case 'no_rows_loaded': {
      const { location, withheld } = describeWithheld(notWritten, report);
      return {
        severity: 'warning',
        heading: 'No rows reached the target',
        counts,
        location,
        withheld,
        corroboration,
        testId: 'load-outcome-label-no-rows-loaded',
      };
    }

    case 'partial_load': {
      const { location, withheld } = describeWithheld(notWritten, report);
      return {
        // `info`, matching what `LoadOutcomeIndicator` shows in the run list.
        // A detail page may say more than a list cell; it must not disagree with
        // it about how alarming the run is. No threshold anywhere — the count is
        // shown at any N (D1).
        severity: 'info',
        heading: `${pluralRows(notWritten)} did not reach the target`,
        counts,
        location,
        withheld,
        corroboration,
        testId: 'load-outcome-label-partial-load',
      };
    }

    case 'empty_source':
      return {
        severity: 'info',
        // Names the source, not the load: R1.3 makes `empty_source` a separate
        // outcome precisely so an empty source is not shown as data loss, and an
        // empty scheduled JDBC poll is a legitimate no-op.
        heading: 'The source was empty — there were no rows to load',
        counts,
        location: null,
        withheld: 'No rows were withheld: the source contained none to begin with.',
        corroboration,
        testId: 'load-outcome-label-empty-source',
      };

    case 'complete_load':
      return {
        severity: 'info',
        heading: 'All rows reached the target',
        counts,
        location: null,
        withheld: 'No rows were withheld: every row read reached the target.',
        corroboration,
        testId: 'load-outcome-label-complete-load',
      };

    // Absence, not error. See the module docstring.
    case 'not_established':
      return null;

    default: {
      // A wire value this build does not know: say nothing rather than invent a
      // reading for it. This assignment is the fence — adding an arm to
      // `LoadOutcomeKind` without deciding its wording here fails the type-check
      // instead of silently falling through to silence.
      const unrecognised: never = outcome.outcome;
      void unrecognised;
      return null;
    }
  }
}

export interface LoadOutcomeLabelProps {
  outcome: LoadOutcome | null | undefined;
  /** The run's DQ report, when one exists. Supplies the quarantine locator. */
  report?: QualityReport | null;
}

function LoadOutcomeLabel({ outcome, report }: LoadOutcomeLabelProps) {
  const statement = describeLoadOutcomeLabel(outcome, report);
  if (!statement) return null;

  return (
    <Alert
      type={statement.severity}
      header={statement.heading}
      // The icon's meaning in words, so severity is not carried by colour and
      // shape alone (`LGR-99050e6-1`, template / ratified). The visible body text
      // is self-sufficient without it.
      statusIconAriaLabel={statement.severity === 'warning' ? 'Warning' : 'Info'}
      data-testid="load-outcome-label"
    >
      {/* Deliberately not a SpaceBetween, for the reason QualityVerdictLabel
          records: it re-renders its children as a list, and a
          conditionally-absent child there produces a React duplicate-key
          warning. Each Box carries its own spacing. */}
      <Box variant="small" data-testid="load-outcome-counts">
        {statement.counts}
      </Box>
      <Box variant="small" data-testid="load-outcome-withheld">
        {statement.withheld}
      </Box>
      {statement.corroboration ? (
        <Box variant="small" data-testid="load-outcome-corroboration">
          {statement.corroboration}
        </Box>
      ) : null}
    </Alert>
  );
}

/**
 * Memoized on the same measured basis as `ApprovalArtifact` (p47 R16.1), and for
 * one additional reason specific to this component's position: it sits OUTSIDE
 * `RunDetailPage`'s `Tabs` (`:654`), so it renders on every render of that page
 * rather than only while one tab is open. Both props are data — `load_outcome`
 * off the run-detail payload and the run's DQ report — and neither is a
 * callback, so the shallow comparison succeeds whenever the run's counts have
 * not moved.
 *
 * The saving is small and stated as such: **0.096–0.120 ms** per avoided render
 * (measured, jsdom), against 0.005–0.006 ms when the comparison hits. It is a
 * wrapper with no parent change, not a restructuring.
 *
 * `describeLoadOutcomeLabel` above stays a plain exported function — it is
 * unit-tested without rendering, and memoizing a component does not touch it.
 */
export default memo(LoadOutcomeLabel);
