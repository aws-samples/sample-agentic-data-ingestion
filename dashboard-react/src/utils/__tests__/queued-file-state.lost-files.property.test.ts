// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property: **A recorded loss is stated exactly once, and every reason shown is one the
 * producer sent.**
 *
 * **Validates: Requirements 10.3**
 *
 * > *For any* `lost_files` array and *any* `queued_files` array over the three published
 * > status cases — including the arrays overlapping fully, partially or not at all,
 * > reasons present, blank, absent, duplicated or divergent, and either array
 * > `undefined` — every lost file is claimed by **exactly one** of the two regions, the
 * > Lost Files region's own set is an order-preserving subsequence of what the producer
 * > sent, and the reason list is non-empty, duplicate-free, and made only of sentences
 * > the producer sent or the single fallback.
 *
 * Shaped after `pages/__tests__/PipelineOverviewPage.queued-file-facts.property.test.tsx`
 * — same fast-check version, same `{ numRuns: 100 }`, same discipline of sourcing
 * expectations from the producer's literals rather than from the module under test. It
 * differs in one deliberate way: that file drives the property through a render, this one
 * calls the two functions. Nothing here needs a DOM, and keeping it out of one makes 100
 * runs cost milliseconds instead of seconds, which is what let the generator be this wide.
 *
 * ## What this catches that the three example files cannot
 *
 * `queued-file-state.lost-files.test.ts` beside this one asserts named examples: the full
 * overlap, the deferred non-overlap, the `file_name` collision, the two divergent reasons.
 * Every one is a fixed shape. What no example among them reaches:
 *
 * - **Cardinality across both regions at once.** The interesting failure mode of
 *   `lostFilesNotAlreadyQueued` is not "wrong entry dropped" but *double-counting* — a
 *   file listed in both regions, or in neither. That is a statement about the sum over two
 *   sets, and it is only falsifiable over generated overlaps.
 * - **Partial overlap at arbitrary positions.** The examples overlap at index 0 or not at
 *   all. A filter that dropped by index rather than by key, or that stopped at the first
 *   match, passes all of them.
 * - **Order preservation under filtering.** Nothing asserts the surviving entries keep the
 *   producer's order once some have been removed from the middle.
 * - **Reason list well-formedness over arbitrary multisets** — no duplicates, never empty,
 *   nothing invented, first-seen order — rather than at the four hand-picked shapes.
 *
 * ## The two claims that make "exactly once" meaningful
 *
 * The docstring on `lostFilesNotAlreadyQueued` says which region shows a given loss *"is
 * cosmetic and depends on the page; **that it is shown at all is not**"*. Those are two
 * separate properties and both are asserted below:
 *
 * - **at-most-once** — the filter removes an entry the Queued Files table is already
 *   showing, so nothing is listed twice on one screen;
 * - **at-least-once** — the filter removes an entry *only* when that table is showing it,
 *   so no loss falls between the two regions.
 *
 * Asserted as a sum over the two sets rather than as two separate existence checks,
 * because a mutation that both over-filters and under-filters would satisfy each check
 * separately on different inputs and fail the sum.
 *
 * ## Expectations come from the producer, never from the reader
 *
 * `WIRE_STATUS_LOST` and `WIRE_LOST_REASON` are literals, not imports from
 * `queued-file-state.ts`. A property whose oracle is the module under test is
 * self-consistent and passes under any mutation of it — `LGR-28b4d75-5` (**template /
 * ratified**) states the same for a contract test: *"a test written from the producer's
 * output cannot detect the producer drifting, because the output is its own oracle"*.
 * `'failed_dlq'` is `_split_derive_and_filter`'s published literal and `WIRE_LOST_REASON`
 * is `queue_loss_recorder.LOST_REASON`.
 *
 * `LOST_FILE_REASON_FALLBACK` is imported, and the property's use of it is confined to
 * *"the fallback, whatever it says, appears if and only if no row carried a reason"* — a
 * claim that holds for any wording.
 *
 * ## Two shapes deliberately NOT generated, and why
 *
 * 1. **Duplicate `source_key` within `lost_files`.** The producer keys the row on a hash
 *    of `source_key` (`queue_loss_recorder.queued_row_key`), so one file has one row and
 *    two entries with one key cannot be published. Generating it would be testing a shape
 *    no producer can emit, and it has no defined right answer.
 * 2. **`lost_reason` as a non-string.** `LostFile.lost_reason` is typed `string`; the
 *    interesting absence case is a *record written before the field existed*, which
 *    reaches the reader as `undefined`, and that is generated.
 */
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  distinctLostReasons,
  LOST_FILE_REASON_FALLBACK,
  lostFilesNotAlreadyQueued,
} from '../queued-file-state';

// ---------------------------------------------------------------------------
// The producer's literals
// ---------------------------------------------------------------------------

/** `_split_derive_and_filter`'s published Lost_File status. */
const WIRE_STATUS_LOST = 'failed_dlq';
/** Its published Deferred_File status. */
const WIRE_STATUS_DEFERRED = 'queued';

/** `queue_loss_recorder.LOST_REASON` — the one reason constant that exists today. */
const WIRE_LOST_REASON =
  "The upload's SQS message exhausted its redelivery budget before the " +
  'pipeline lock became free, so SQS moved it to the dead-letter queue and no ' +
  'run was started for this file.';

/** A second reason no producer emits yet. See the sibling example file on why it exists. */
const SECOND_REASON = 'The pipeline was archived before this upload could be processed.';

// ---------------------------------------------------------------------------
// The input space
// ---------------------------------------------------------------------------

interface LostRow {
  file_name: string;
  source_key: string;
  lost_at: string;
  lost_reason?: string;
}

interface QueuedRow {
  file_name: string;
  source_key: string;
  queued_at: string;
  status: string;
}

/**
 * A shared key pool, so the two arrays can be generated independently and still overlap
 * at arbitrary positions. Six keys against arrays of up to five entries gives full
 * overlap, no overlap and everything between at usable frequencies.
 *
 * Two of them share a `file_name` across different prefixes — the collision that makes
 * `source_key` rather than `file_name` the identity. Nothing below reads `file_name` as
 * an identity, so a filter that did would fail on the runs that draw both.
 */
const KEY_POOL = [
  'mynewtesting/customer_1.csv',
  'mynewtesting/customer_2.csv',
  'mynewtesting/customer_3.csv',
  'other_pipeline/customer_1.csv',
  'other_pipeline/customer_2.csv',
  'archive/2026/customer_1.csv',
] as const;

/** A status the reader recognises as neither published state. */
const unrecognisedStatusArb = fc
  .oneof(
    fc.constantFrom('QUEUED', 'Queued', 'FAILED_DLQ', 'Failed_DLQ', 'failed', 'dlq', ''),
    fc.stringMatching(/^[a-z]{4,12}$/),
  )
  .filter((s) => s !== WIRE_STATUS_LOST && s !== WIRE_STATUS_DEFERRED);

type ReasonKind = 'producer' | 'second' | 'blank' | 'absent';

const reasonKindArb = fc.constantFrom<ReasonKind>('producer', 'second', 'blank', 'absent');

function reasonFor(kind: ReasonKind): string | undefined {
  switch (kind) {
    case 'producer':
      return WIRE_LOST_REASON;
    case 'second':
      return SECOND_REASON;
    case 'blank':
      return '';
    case 'absent':
      return undefined;
  }
}

function buildLost(keys: readonly string[], kinds: readonly ReasonKind[]): LostRow[] {
  return keys.map((source_key, i) => {
    const reason = reasonFor(kinds[i % Math.max(kinds.length, 1)] ?? 'producer');
    return {
      file_name: source_key.slice(source_key.lastIndexOf('/') + 1),
      source_key,
      lost_at: '2026-08-27T09:00:00.000000+00:00',
      ...(reason === undefined ? {} : { lost_reason: reason }),
    };
  });
}

/**
 * `lost_files` — a set of distinct keys (see the docstring on why duplicates are not
 * generated) with a reason kind per entry. Zero entries is the ordinary case, not an
 * edge: most pipelines have lost nothing.
 */
const lostArb = fc
  .tuple(
    fc.uniqueArray(fc.constantFrom(...KEY_POOL), { minLength: 0, maxLength: 5 }),
    fc.array(reasonKindArb, { minLength: 1, maxLength: 5 }),
  )
  .map(([keys, kinds]) => buildLost(keys, kinds));

/**
 * `queued_files` — page 1 of a newest-first index, so its keys may or may not be among
 * the lost ones and its statuses span all three cases. Duplicates are allowed here: the
 * index can legitimately hold two rows for two different files that hash to keys drawn
 * from the same pool in this generator, and the filter must be indifferent to that.
 */
const queuedArb = fc.array(
  fc
    .tuple(
      fc.constantFrom(...KEY_POOL),
      fc.oneof(
        fc.constant(WIRE_STATUS_LOST),
        fc.constant(WIRE_STATUS_DEFERRED),
        unrecognisedStatusArb,
      ),
    )
    .map(
      ([source_key, status]): QueuedRow => ({
        file_name: source_key.slice(source_key.lastIndexOf('/') + 1),
        source_key,
        queued_at: '2026-08-27T09:00:00.000000+00:00',
        status,
      }),
    ),
  { minLength: 0, maxLength: 5 },
);

/** Either argument may be absent — the shape a response cached before the field takes. */
const maybeAbsent = <T>(arb: fc.Arbitrary<T[]>) =>
  fc.oneof({ weight: 9, arbitrary: arb }, { weight: 1, arbitrary: fc.constant(undefined) });

// ---------------------------------------------------------------------------
// The property
// ---------------------------------------------------------------------------

describe('Property: a recorded loss is stated exactly once, and every reason shown is one the producer sent', () => {
  it('claims each lost file in exactly one region, preserving the producer’s order', () => {
    fc.assert(
      fc.property(maybeAbsent(lostArb), maybeAbsent(queuedArb), (lostFiles, queuedFiles) => {
        const result = lostFilesNotAlreadyQueued(lostFiles, queuedFiles);

        const published = lostFiles ?? [];
        // The keys the Queued Files table is ALREADY claiming as lost. Exact match on
        // the producer's literal — a case-insensitive or `startsWith` predicate would
        // pull in the `'FAILED_DLQ'` and `'Failed_DLQ'` draws and fail here.
        const claimedByQueuedTable = new Set(
          (queuedFiles ?? []).filter((q) => q.status === WIRE_STATUS_LOST).map((q) => q.source_key),
        );

        // ── At-most-once AND at-least-once, as one sum ──────────────────────────
        // Every published loss is claimed by exactly one region. A filter that
        // over-removes leaves a 0 here; one that under-removes leaves a 2. Checking
        // the sum rather than each side separately is what makes a mutation doing
        // both at once falsifiable.
        for (const entry of published) {
          const claims =
            Number(result.some((r) => r.source_key === entry.source_key)) +
            Number(claimedByQueuedTable.has(entry.source_key));
          expect(claims).toBe(1);
        }

        // ── The membership rule, stated independently of the sum ────────────────
        // Spelled out because the sum above would also be satisfied by a filter that
        // dropped the WRONG entries in exactly compensating numbers.
        for (const entry of published) {
          expect(result.includes(entry)).toBe(!claimedByQueuedTable.has(entry.source_key));
        }

        // ── An order-preserving subsequence of what the producer sent ───────────
        // Identity, not equality: the render reads fields straight off these objects
        // and `trackBy="source_key"` needs stable references. Filtering by index, or
        // rebuilding entries, or sorting, all fail here.
        expect(result.length).toBeLessThanOrEqual(published.length);
        expect(published.filter((e) => result.includes(e))).toEqual(result);
        for (const entry of result) {
          expect(published.some((e) => e === entry)).toBe(true);
        }

        // ── The reason list, over whatever survived the filter ─────────────────
        const reasons = distinctLostReasons(result);
        const sentByProducer = result
          .map((e) => e.lost_reason ?? '')
          .filter((r) => r !== '');

        // Never empty: there is no input for which the region renders no sentence.
        expect(reasons.length).toBeGreaterThanOrEqual(1);
        // Duplicate-free, so N rows carrying one reason yield one sentence.
        expect(new Set(reasons).size).toBe(reasons.length);

        if (sentByProducer.length === 0) {
          // The fallback, alone — and only here. A blank or absent reason is "none
          // recorded", not a reason of `''`.
          expect(reasons).toEqual([LOST_FILE_REASON_FALLBACK]);
        } else {
          // Nothing invented: every sentence is one a row actually carried, in
          // first-seen order, and the generic fallback never appears beside a
          // specific account of the same loss.
          expect(reasons).toEqual([...new Set(sentByProducer)]);
          expect(reasons).not.toContain(LOST_FILE_REASON_FALLBACK);
        }
      }),
      { numRuns: 100 },
    );
  });
});
