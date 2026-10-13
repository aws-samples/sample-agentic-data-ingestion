// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Self-healing presentation for the pipeline run list (p30 R14.3).
 *
 * R14.3 asks for a heal to be *indicated* in the run list. The indicator is a
 * pure function of the `self_healing_status` denormalized onto the execution
 * row by `execution_updater`, which copies the agent's `rerun_status` through
 * verbatim.
 *
 * **The point of this table is that it does not collapse.** A single "Healed"
 * badge would be correct for exactly one of the states below and would
 * misreport the rest — the agent's whole honesty contract is that
 * `rerun_started` is True only when `start_execution` actually succeeded, and
 * that `rerun_status` moves to `success`/`failed` only when the re-run's own
 * `job_completion` links the terminal result back. An indicator that showed
 * "Healed" for a re-run still in flight, or for a re-run that never started,
 * would reintroduce precisely the dishonesty the record was built to remove.
 */

/** Cloudscape `Badge` colors used here. */
export type SelfHealingBadgeColor = 'blue' | 'green' | 'red' | 'grey';

export interface SelfHealingIndicator {
  /** Badge text — short enough for a table cell. */
  label: string;
  color: SelfHealingBadgeColor;
  /**
   * Hover text: what the state means, so the badge is not a bare token. The
   * remediation itself is appended by {@link describeSelfHealing} when the
   * execution row carries one.
   */
  detail: string;
}

/**
 * Every `rerun_status` the self-healing agent can write, and how each reads.
 *
 * Sources (agentcore/self_healing_agent/main.py):
 *  - `running`                     — `start_execution` succeeded (`rerun_started=True`)
 *  - `success` / `failed`          — linked back by the re-run's own job_completion
 *  - `start_failed`                — `start_execution` raised
 *  - `skipped_partial_write_guard` — Partial_Write_Guard declined (p30 R15.2)
 *  - `skipped`                     — diagnosed, but no fix applies (flag_for_review /
 *                                    no_upgrade_available), so no re-run was attempted
 *  - `skipped_heal_guard`          — cannot reach the run list (the skip record carries
 *                                    no pipeline id, so execution_updater drops it), but
 *                                    mapped anyway so it can never fall through to the
 *                                    unrecognised branch
 */
const INDICATORS: Record<string, SelfHealingIndicator> = {
  running: {
    label: 'Self-heal re-running',
    color: 'blue',
    detail:
      'Self-healing started a re-run of this run. The re-run is still in flight — its outcome is not known yet.',
  },
  success: {
    label: 'Self-healed',
    color: 'green',
    detail: 'Self-healing re-ran this run and the re-run succeeded.',
  },
  failed: {
    label: 'Self-heal re-run failed',
    color: 'red',
    detail: 'Self-healing re-ran this run and the re-run failed too.',
  },
  start_failed: {
    label: 'Self-heal re-run never started',
    color: 'red',
    detail:
      'Self-healing diagnosed the failure but could not start the re-run. No re-run exists. Any fix already applied to the Glue job was not rolled back.',
  },
  skipped_partial_write_guard: {
    label: 'Self-heal declined',
    color: 'red',
    detail:
      'Self-healing declined to re-run: this target duplicates or corrupts data on re-run and the failed run may have written rows. The run is flagged for human review.',
  },
  skipped: {
    label: 'Self-heal: review needed',
    color: 'grey',
    detail:
      'Self-healing diagnosed the failure but no automatic remediation applies. Nothing was re-run.',
  },
  skipped_heal_guard: {
    label: 'Self-heal skipped',
    color: 'grey',
    detail: 'This run was itself a self-healing re-run — at most one heal is attempted per run.',
  },
};

/**
 * Resolve the run-list indicator for an execution row's self-healing state.
 *
 * Returns `null` when no heal touched the run, so callers can render nothing.
 * An unrecognised status is reported AS the unrecognised status rather than
 * guessed at — a future `rerun_status` must not silently render as "Self-healed".
 */
export function describeSelfHealing(
  status: string | undefined | null,
  remediation?: string | null,
): SelfHealingIndicator | null {
  const key = (status ?? '').trim();
  if (!key) return null;

  const known = INDICATORS[key];
  const indicator: SelfHealingIndicator = known ?? {
    label: `Self-heal: ${key}`,
    color: 'grey',
    detail: `Self-healing recorded an outcome this dashboard does not recognise (${key}). Open the run for the full record.`,
  };

  const trimmedRemediation = (remediation ?? '').trim();
  if (!trimmedRemediation) return indicator;
  return { ...indicator, detail: `${indicator.detail} Remediation: ${trimmedRemediation}` };
}
