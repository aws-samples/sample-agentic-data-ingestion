// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Skip presentation for the pipeline run list (p30 R10.2).
 *
 * **What R10.2 still needed, and what it did not.** A `SKIPPED` row already
 * rendered visibly and already rendered differently from a failure before this
 * file existed: `statusType` (`utils/formatting.ts`) has no `skipped` arm, so a
 * skip falls to `default: 'info'` and shows as a blue info indicator, while
 * `FAILED` matches the `'error'` arm and shows red. The literal clause —
 * "display Skipped_Run_Records in the pipeline's run history, distinguishable
 * from failures" — was therefore already met by accident.
 *
 * What was missing is the *reason*. `pipeline_trigger` writes `skip_reason` and
 * `skip_detail` on every skip row and nothing read them, so all three skips
 * rendered as one indistinguishable "SKIPPED": a duplicate re-upload (nothing
 * is wrong), a fleet at its concurrency ceiling (the schedule is outrunning the
 * fleet), and a pipeline still running its previous load (the schedule is
 * outrunning itself). Those need three different responses from an operator,
 * and a bare "SKIPPED" tells them to investigate from scratch every time. This
 * file surfaces the reason as a badge with the written detail on hover.
 *
 * Shape follows `utils/self-healing.ts` (task 2.5) deliberately, including its
 * refusal to collapse: an explicit table, and an unrecognised reason reported
 * AS unrecognised rather than guessed at.
 */

/** Cloudscape `Badge` colors used here. */
export type SkipBadgeColor = 'blue' | 'grey' | 'severity-medium';

export interface SkipIndicator {
  /** Badge text — short enough for a table cell. */
  label: string;
  color: SkipBadgeColor;
  /**
   * Hover text: what this reason means and what it implies. The row's own
   * `skip_detail`, written by the Lambda that made the decision, is appended by
   * {@link describeSkip} when present — it carries the specifics (which run
   * already processed the content and from which file, both sides' size and
   * ETag, how many executions were running).
   */
  detail: string;
}

/**
 * Every `skip_reason` a skip row can carry, and how each reads.
 *
 * Sources:
 *  - `duplicate_content`         — `shared/processed_file_ledger.py`
 *                                  (`SKIP_REASON_DUPLICATE`), written by
 *                                  `pipeline_trigger._record_duplicate_skip`,
 *                                  detail composed by
 *                                  `pipeline_trigger._duplicate_skip_detail`
 *  - `global_concurrency`        — `pipeline_trigger.SKIP_REASON_CONCURRENCY`
 *  - `pipeline_lock_contention`  — `pipeline_trigger.SKIP_REASON_LOCK_CONTENTION`
 *
 * The two contention reasons are NOT one "Contention" badge. They clear for
 * different causes and need different action: a global-concurrency skip is a
 * fleet-wide condition an individual pipeline owner cannot fix, while a
 * lock-contention skip means this pipeline's own schedule fires faster than its
 * runs finish — which its owner can fix by lengthening the schedule. Collapsing
 * them would send the owner of a lock-contention skip to the wrong place.
 *
 * **These entries are fallback copy, not a mirror of `skip_detail` (p64 R5.6).**
 * R5.6 asks this entry to "reflect whatever R5.1 and R5.2 add". What they added is
 * a *server-composed sentence* — `_duplicate_skip_detail` now names the matched
 * run, the file that content came from, and both sides' size and ETag — plus the
 * row attributes behind it (`previous_source_key`, `previous_content_size`,
 * `previous_content_etag`, `content_size`, `content_etag`). Those are per-row
 * values, so this table cannot restate them; `describeSkip` already appends the
 * sentence itself verbatim. Reflecting them therefore means **naming what the
 * record carries and not contradicting the sentence**, never re-deriving it.
 *
 * R5.6's stated mechanism — "an unmirrored reason renders as an
 * unrecognised-reason badge" — does not apply: R5.1/R5.2 introduced no new
 * `skip_reason` value, so `duplicate_content` cannot fall through to the
 * unrecognised arm below. Only R5.6's "MUST be updated" half is live.
 *
 * Corollary, and the reason the remediation sentence is NOT repeated here:
 * `_record_duplicate_skip` sets `skip_detail` unconditionally on every row it
 * writes, and every wording it has ever written — including the one-sentence form
 * that predates p64 — ends with the Run Now instruction. Stating it here as well
 * put it in the rendered tooltip twice. One statement, in the producer that knows
 * the specifics.
 */
const INDICATORS: Record<string, SkipIndicator> = {
  duplicate_content: {
    label: 'Skipped: duplicate content',
    color: 'blue',
    detail:
      'A previous run already processed content with the same size and ETag successfully, so this file was not processed again. Nothing is wrong. The row records which run matched, the file that content came from, and both sides\u2019 size and ETag, so a suspected false positive can be checked without recomputing a digest.',
  },
  global_concurrency: {
    label: 'Skipped: at concurrency limit',
    color: 'severity-medium',
    detail:
      'A scheduled load did not run because the maximum number of executions was already running across all pipelines. Nothing retried it — the next attempt is the schedule\u2019s next fire, so this load is missing until then.',
  },
  pipeline_lock_contention: {
    label: 'Skipped: previous run still going',
    color: 'severity-medium',
    detail:
      'A scheduled load did not run because this pipeline\u2019s previous execution had not finished. Nothing retried it. If this keeps happening, the schedule fires faster than the pipeline can complete.',
  },
};

/**
 * Resolve the run-list indicator for a skip row.
 *
 * Returns `null` when the row is not a skip, so callers render nothing. A skip
 * row with NO reason still returns an indicator — the row exists because
 * something decided not to run, and reporting that without a reason is better
 * than rendering a bare "SKIPPED" whose cause the reader has to guess.
 */
export function describeSkip(
  status: string | undefined | null,
  skipReason?: string | null,
  skipDetail?: string | null,
): SkipIndicator | null {
  if ((status ?? '').trim().toUpperCase() !== 'SKIPPED') return null;

  const key = (skipReason ?? '').trim();
  let indicator: SkipIndicator;
  if (!key) {
    indicator = {
      label: 'Skipped',
      color: 'grey',
      detail: 'This run was skipped. No reason was recorded on the row.',
    };
  } else {
    indicator =
      INDICATORS[key] ?? {
        label: `Skipped: ${key}`,
        color: 'grey',
        detail: `This run was skipped for a reason this dashboard does not recognise (${key}). Open the run for the full record.`,
      };
  }

  const trimmedDetail = (skipDetail ?? '').trim();
  if (!trimmedDetail) return indicator;
  return { ...indicator, detail: `${indicator.detail} Recorded detail: ${trimmedDetail}` };
}
