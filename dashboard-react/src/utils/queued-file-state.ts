// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The two facts a `queued_files` entry can carry, and the two predicates that read
 * them — derived once here so three consumers cannot drift (p54 R9.3, R9.5).
 *
 * The producer is `_split_derive_and_filter`'s step-1 comprehension
 * (`handlers/pipelines_runs.py:1181-1192`, and the predicate it calls,
 * `_is_lost_queued_row`), which publishes exactly two literals: `'queued'` for a
 * Deferred_File and `'failed_dlq'` for a Lost_File. Before p54 it published `'queued'`
 * unconditionally, so `ExecutionsPage.tsx`'s `failed_dlq` branch was a reader with no
 * producer — the inverse of `LGR-5d68eeb-3` / `LGR-eee8ac8-6`'s shape, and R9's second
 * gap. (An earlier revision of this comment cited `:763-771`, which was the address of
 * the `_QUEUE_OUTCOME_ATTRIBUTE` constant block rather than of the comprehension.)
 *
 * ## `queued_files` is page-scoped; `lost_files` is not
 *
 * That comprehension only sees the first page of a newest-first index, so it publishes
 * a loss for about an hour and then stops, while the record itself lives 30 days. The
 * durable statement is the response's separate `lost_files` array
 * (`pipelines_runs._lost_files`), which is why {@link lostFilesNotAlreadyQueued}
 * exists: the two arrays overlap for exactly as long as a just-recorded loss remains
 * on page 1.
 *
 * `QueuedFile.status` is typed `string`, not a union (`use-executions.ts:97-102`), and
 * that stays true: the wire can carry a value neither literal matches, so both
 * predicates below take `string | undefined` and answer for any input.
 *
 * ## The two predicates are NOT negations of each other, and that is deliberate
 *
 * They answer different questions and each fails safe in its own direction. An
 * unrecognised status is a third case, and collapsing it into either fact would be
 * the invented claim `LGR-99050e6-2`'s three-state discipline exists to prevent —
 * "an absent quarantine counter is reported as unknown rather than as zero".
 *
 * - {@link isLostQueuedFile} — *do we know this file is lost?* Exact match on
 *   `'failed_dlq'`. Anything else renders as "Queued", which is the reader's existing
 *   default and makes no claim of loss. This is the render question.
 * - {@link isDeferredQueuedFile} — *do we know this file is still work in flight?*
 *   Exact match on `'queued'`. Anything else does **not** hold the fast poll cadence,
 *   because a file that is not confirmed to be coming back must not keep the UI
 *   polling forever over work that will never move. This is the cadence question, and
 *   it is why `computeExecutionsRefetchInterval`'s `hasQueued` derivation counts
 *   deferred entries rather than array length (p54, do-NOT 17 / R8.14 — the input
 *   changes, `EXECUTIONS_POLL_MATRIX` and the signature do not).
 *
 * Every distinction here is carried in **words** at the render sites, never in colour
 * alone — `LGR-99050e6-1` records that colour alone fails WCAG 1.4.1, and
 * `LGR-1f7892b-2` re-states it for a `StatusIndicator` type specifically ("a
 * `warning` type *is* colour, so the sentence is the carrier and the type is not").
 */

/** The published `status` of a Lost_File — the file's redelivery budget ran out. */
export const QUEUED_FILE_STATUS_LOST = 'failed_dlq';

/** The published `status` of a Deferred_File — waiting for the pipeline to free up. */
export const QUEUED_FILE_STATUS_DEFERRED = 'queued';

/**
 * Is this entry a Lost_File? Exact match; see the module note on why an unrecognised
 * value answers `false` rather than being treated as a loss.
 */
export function isLostQueuedFile(status: string | undefined): boolean {
  return status === QUEUED_FILE_STATUS_LOST;
}

/**
 * Is this entry a Deferred_File — a file we know is still coming back? Exact match;
 * see the module note on why an unrecognised value answers `false` rather than
 * holding the fast poll cadence.
 */
export function isDeferredQueuedFile(status: string | undefined): boolean {
  return status === QUEUED_FILE_STATUS_DEFERRED;
}

/**
 * The `lost_files` entries a surface should render **in its own Lost Files region** —
 * every one the producer sent, minus any the page-scoped Queued Files table above is
 * already showing (p54 R10.3).
 *
 * ## Why a filter rather than rendering the array straight
 *
 * The two arrays are not alternatives; they overlap. A loss lands at the top of the
 * newest-first index, so for roughly the first hour it appears in **both**
 * `queued_files` (as `'failed_dlq'`) and `lost_files`. Rendering both arrays unfiltered
 * lists the same file twice on one screen.
 *
 * ## Why the Queued Files table keeps the loss instead of this taking it over
 *
 * The other arm — filter the queued table down to deferred entries and let this region
 * own every loss — was rejected. p54's `'failed_dlq'` branch in that table is ratified
 * and asserted on both surfaces, and taking rows away from it would make that branch a
 * reader with no producer again: the exact defect p54 R9.3 closed. Additive is also the
 * only shape that leaves `_split_derive_and_filter` byte-identical, which its own
 * four-key set-equality fence requires (do-NOT 3).
 *
 * So which region shows a given loss is cosmetic and depends on the page; **that it is
 * shown at all is not**, and `lost_files` is what guarantees it. `source_key` is the
 * identity used to match, because it is what both rows are keyed on at the producer —
 * not `file_name`, which two different prefixes can share.
 */
export function lostFilesNotAlreadyQueued<L extends { source_key: string }>(
  lostFiles: readonly L[] | undefined,
  queuedFiles: readonly { source_key: string; status: string }[] | undefined,
): L[] {
  const alreadyShown = new Set(
    (queuedFiles ?? []).filter((q) => isLostQueuedFile(q.status)).map((q) => q.source_key),
  );
  return (lostFiles ?? []).filter((lost) => !alreadyShown.has(lost.source_key));
}

/**
 * The fallback sentence for a Lost_File record carrying no `lost_reason`.
 *
 * Deliberately generic where the producer's own `LOST_REASON` is specific: this is
 * what is true of *any* dead-lettered upload, and it makes no claim about receive
 * counts or timings that this surface cannot know. Never rendered beside a reason the
 * producer did send.
 */
export const LOST_FILE_REASON_FALLBACK =
  'These uploads exhausted their redelivery attempts while the pipeline was busy and ' +
  'were never processed. They will not be retried automatically.';

/**
 * The distinct `lost_reason` sentences carried by a set of lost files, in first-seen
 * order, or the single {@link LOST_FILE_REASON_FALLBACK} when none carried one.
 *
 * **Distinct rather than "the first row's".** The producer writes one constant today
 * (`queue_loss_recorder.LOST_REASON`), so this collapses to one sentence — but reading
 * one row's reason and presenting it as the reason for all of them would be an
 * assumption about another Lambda package, and it would silently mislabel every row
 * the day a second reason appears. Deduplicating costs one pass and cannot be wrong in
 * either direction.
 *
 * Empty strings are dropped rather than rendered, so a record with no reason falls to
 * the fallback instead of contributing a blank line — R9.5's three states, where "no
 * reason recorded" is not a reason of `''`.
 */
export function distinctLostReasons(
  lostFiles: readonly { lost_reason?: string }[] | undefined,
): string[] {
  const reasons = Array.from(
    new Set((lostFiles ?? []).map((lost) => lost.lost_reason ?? '').filter((reason) => reason !== '')),
  );
  return reasons.length > 0 ? reasons : [LOST_FILE_REASON_FALLBACK];
}
