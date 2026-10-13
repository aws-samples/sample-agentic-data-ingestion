// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import type { LoadOutcome } from '../types';

/**
 * What reached the target, shown in a run list's status cell (p37 R1.7, R1.9).
 *
 * A run reported `SUCCEEDED` — no `TaskFailed` or `ExecutionFailed` event
 * anywhere in its Step Functions history — while writing **zero rows** to its
 * Iceberg target, because a data quality rule quarantined every row. Nothing
 * downstream noticed, because a run list derives its status from Glue state and
 * Glue succeeded. This indicator is what makes a zero-row load visibly
 * different from a load that landed the data.
 *
 * **It sits BESIDE the run's status indicator and never replaces it.** The
 * status stays whatever `statusType(item.status)` says; this adds the one fact
 * that status cannot carry. That separation is p37 D1: making `partial_load` a
 * non-success status is the cry-wolf failure, and making the withheld count
 * invisible is how a slow leak becomes a quarter of the data.
 *
 * **It is not a step status, and it must never become one.** `getStatusIcon` /
 * `resolveStepStatus` (`utils/formatting.ts:185`, `:25`) map a *step's* status
 * to the ⚙️-defaulting emoji table, across four call sites
 * (`StepFlowVisualization.tsx:92`, `ExecutionsPage.tsx:155`, `:160`,
 * `PipelineOverviewPage.tsx:229`) with `utils/__tests__/formatting.test.ts`
 * pinning the ⚙️ default. A Load_Outcome is a statement about the run's data,
 * not about a step, so it renders through its own component and neither of
 * those functions is touched.
 *
 * ## What each of the five outcomes renders, and why
 *
 * | outcome | rendered | why |
 * |---|---|---|
 * | `no_rows_loaded` | `warning` — "0 rows loaded" | R1.7. The observed defect. Distinct from `complete_load` by being present at all |
 * | `partial_load` | `info` — "N rows not written" | R1.9 / D1. **Always**, at any N. No threshold anywhere in this spec |
 * | `empty_source` | `info` — "Empty source — no rows to load" | See below. Stated, but never as loss |
 * | `complete_load` | **nothing** | The run's existing success indicator already says it |
 * | `not_established` | **nothing** | See below. Absence, not error |
 *
 * ### Why `not_established` renders nothing at all
 *
 * Not a dash, not a muted "unknown", not a tooltip saying data is missing. The
 * Glue job's counts write is best-effort by necessity — a telemetry failure must
 * not fail a job that has already written to the target — so `not_established`
 * is a **reachable steady state**, not a migration artefact: every run that
 * completed before the counts shipped, plus any run whose counts write failed
 * (design §5 risk 3). An indicator on a common steady state trains operators to
 * ignore this column, which is the cry-wolf failure by another door — and the
 * one thing that would make the `no_rows_loaded` warning stop working.
 *
 * ### Why `empty_source` is stated rather than silent
 *
 * `complete_load` renders nothing because the run's success indicator *already
 * says it*: the data landed. That reasoning does not carry to `empty_source` —
 * a `SUCCEEDED` run that read nothing is otherwise indistinguishable from a
 * `SUCCEEDED` run that loaded a million rows, and that indistinguishability is
 * the exact defect p37 exists to remove. So it is stated. It is stated as
 * `info`, never `warning`, and its wording names the source rather than the
 * load: R1.3 makes `empty_source` a separate outcome precisely so an empty
 * source is not shown as data loss, and an empty scheduled JDBC poll is a
 * legitimate no-op that must not read as a problem.
 */

/** Cloudscape `StatusIndicator` types this component uses. Deliberately no `error`. */
export type LoadOutcomeIndicatorType = 'warning' | 'info';

export interface LoadOutcomeDescriptor {
  type: LoadOutcomeIndicatorType;
  /**
   * The visible text. Carries the meaning on its own — nothing here is conveyed
   * by colour alone (WCAG 1.4.1). That is the same rule `LGR-99050e6-1`
   * (**template / ratified**) settled for the metrics tiles: colour alone makes
   * the distinction invisible, so the text states it.
   */
  text: string;
  /** Hover/description text — the deriver's own one-sentence `reason`. */
  detail: string;
  testId: string;
}

/**
 * Resolve the run-list indicator for one run's Load_Outcome.
 *
 * Returns `null` whenever nothing should be rendered, so a caller can mount
 * this unconditionally and let it decide.
 */
export function describeLoadOutcome(
  outcome: LoadOutcome | null | undefined,
): LoadOutcomeDescriptor | null {
  if (!outcome) return null;

  const detail = typeof outcome.reason === 'string' ? outcome.reason : '';

  switch (outcome.outcome) {
    case 'no_rows_loaded':
      return {
        type: 'warning',
        text: '0 rows loaded',
        detail,
        testId: 'load-outcome-no-rows-loaded',
      };

    case 'partial_load': {
      // `rows_not_written` is computed by the backend and is never negative.
      // It is read, never recomputed from the two counts — one deriver owns
      // that arithmetic (R1.10).
      const withheld = outcome.rows_not_written;
      const hasCount = typeof withheld === 'number' && Number.isFinite(withheld) && withheld > 0;
      return {
        type: 'info',
        // No threshold and no collapsing: two rows withheld out of a thousand
        // is a successful run whose operator should be able to see the two (D1).
        // When the count did not arrive, say that rows were withheld rather than
        // print a number this component invented.
        text: hasCount
          ? `${withheld.toLocaleString()} row${withheld === 1 ? '' : 's'} not written`
          : 'Some rows not written',
        detail,
        testId: 'load-outcome-partial-load',
      };
    }

    case 'empty_source':
      return {
        type: 'info',
        text: 'Empty source — no rows to load',
        detail,
        testId: 'load-outcome-empty-source',
      };

    // The run's own success indicator already says this one.
    case 'complete_load':
      return null;

    // Absence, not error. See the module docstring.
    case 'not_established':
      return null;

    default: {
      // A wire value this build does not know: render nothing rather than guess
      // at a severity for it. This assignment is the fence — adding an arm to
      // `LoadOutcomeKind` without deciding its presentation here fails the
      // type-check instead of silently falling through to silence.
      const unrecognised: never = outcome.outcome;
      void unrecognised;
      return null;
    }
  }
}

export interface LoadOutcomeIndicatorProps {
  outcome: LoadOutcome | null | undefined;
}

export default function LoadOutcomeIndicator({ outcome }: LoadOutcomeIndicatorProps) {
  const descriptor = describeLoadOutcome(outcome);
  if (!descriptor) return null;

  // The `<span title>` wrapper matches how the neighbouring cells carry their
  // explanation — the self-healing and skip-reason badges in
  // `ExecutionsPage.tsx` do exactly this, because Cloudscape's
  // `StatusIndicator` takes no `title`. The visible text is self-sufficient, so
  // nothing depends on the hover: the title adds the deriver's full sentence
  // rather than supplying the meaning.
  return (
    <span title={descriptor.detail} data-testid={descriptor.testId}>
      <StatusIndicator type={descriptor.type}>{descriptor.text}</StatusIndicator>
    </span>
  );
}
