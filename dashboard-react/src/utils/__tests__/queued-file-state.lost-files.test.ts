// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The two `lost_files` helpers, directly — `lostFilesNotAlreadyQueued` and
 * `distinctLostReasons`.
 *
 * **Validates: Requirements 10.3**
 *
 * Both shipped with no coverage of any kind. A grep for either name across
 * `dashboard-react/src` matched only their definitions and their four call sites, and
 * codegraph reports "no covering tests found" for both. So this file is the first
 * statement anywhere of what they are supposed to do.
 *
 * ## Why the pure half is worth its own file rather than being folded into the render tests
 *
 * The two page tests beside this one can only observe these functions through a
 * rendered region, which means they observe the *composition* of a filter, a
 * deduplication, a Cloudscape `Table` and a `.map`. When that composition is wrong the
 * failure names a missing DOM node, and every layer is a candidate. Here the failure
 * names the function.
 *
 * It also reaches inputs the render cannot. `lostFilesNotAlreadyQueued` accepts
 * `undefined` for either argument — the shape a response cached before `lost_files`
 * shipped takes — and the pages spell `?? []` at the call site, so no render can ever
 * hand it an `undefined`.
 *
 * ## Expectations come from the producer, never from the reader
 *
 * `WIRE_STATUS_LOST` and `WIRE_LOST_REASON` below are written out as literals rather
 * than imported from the modules under test. That is load-bearing rather than stylistic:
 * a test whose oracle *is* the module under test is self-consistent and passes under any
 * mutation of it. `LGR-28b4d75-5` (**template / ratified**) gives the same reasoning for
 * a contract test — *"a test written from the producer's output cannot detect the
 * producer drifting, because the output is its own oracle"*.
 *
 * - `'failed_dlq'` is `_split_derive_and_filter`'s published Lost_File status.
 * - `WIRE_LOST_REASON` is `queue_loss_recorder.LOST_REASON`
 *   (`lambda/agentic_data_ingestion_queue_loss_recorder/lambda_function.py:137-141`),
 *   read from the producer rather than transcribed from a frontend fixture. It is the
 *   **one** reason constant that exists today, which is exactly why the two-reason cases
 *   below use a second, invented one: the interesting behaviour is unreachable from
 *   production data and would otherwise go untested until the day it fires.
 *
 * `LOST_FILE_REASON_FALLBACK` **is** imported, and only for identity — "the same
 * sentence in every no-reason case". Every claim about it that could be wrong (there is
 * exactly one of it, it is non-empty, it never appears beside a real reason) is asserted
 * structurally and would hold for any wording.
 *
 * ## The requirement these derive from
 *
 * p54 **R10.3** — *"WHEN a file is recorded as lost, THE record SHALL name the file and
 * the reason, and SHALL be readable for longer than the window in which the loss
 * occurred."* `distinctLostReasons` is the "and the reason" half on the read side: its
 * job is to state a reason for every set of lost files, including the sets where the
 * producer sent none. There is no input for which returning nothing is correct, and the
 * first test below is that.
 */
import { describe, it, expect } from 'vitest';
import {
  distinctLostReasons,
  LOST_FILE_REASON_FALLBACK,
  lostFilesNotAlreadyQueued,
} from '../queued-file-state';

/** `_split_derive_and_filter`'s published Lost_File status — the producer's literal. */
const WIRE_STATUS_LOST = 'failed_dlq';
/** Its published Deferred_File status. */
const WIRE_STATUS_DEFERRED = 'queued';

/** `queue_loss_recorder.LOST_REASON`, verbatim from the producer. */
const WIRE_LOST_REASON =
  "The upload's SQS message exhausted its redelivery budget before the " +
  'pipeline lock became free, so SQS moved it to the dead-letter queue and no ' +
  'run was started for this file.';

/**
 * A second reason, which no producer emits today. Not padding: `distinctLostReasons`
 * exists *because* one row's reason must not be presented as every row's, and with one
 * producer constant that behaviour is unreachable from live data.
 */
const SECOND_REASON = 'The pipeline was archived before this upload could be processed.';

interface Lost {
  file_name: string;
  source_key: string;
  lost_at: string;
  lost_reason: string;
}

function lost(source_key: string, lost_reason = WIRE_LOST_REASON): Lost {
  return {
    file_name: source_key.slice(source_key.lastIndexOf('/') + 1),
    source_key,
    lost_at: '2026-08-27T09:00:00.000000+00:00',
    lost_reason,
  };
}

function queued(source_key: string, status: string) {
  return {
    file_name: source_key.slice(source_key.lastIndexOf('/') + 1),
    source_key,
    queued_at: '2026-08-27T09:00:00.000000+00:00',
    status,
  };
}

describe('lostFilesNotAlreadyQueued — the overlap between two arrays that both carry a loss', () => {
  it('returns every lost file when nothing is queued', () => {
    const a = lost('mynewtesting/customer_1.csv');
    const b = lost('mynewtesting/customer_2.csv');

    expect(lostFilesNotAlreadyQueued([a, b], [])).toEqual([a, b]);
  });

  it('drops the one entry the Queued Files table above is already showing', () => {
    const shown = lost('mynewtesting/customer_1.csv');
    const notShown = lost('mynewtesting/customer_2.csv');

    // A loss lands at the top of a newest-first index, so for its first hour it is in
    // BOTH arrays. Rendering both unfiltered lists the same file twice on one screen.
    const result = lostFilesNotAlreadyQueued(
      [shown, notShown],
      [queued('mynewtesting/customer_1.csv', WIRE_STATUS_LOST)],
    );

    expect(result).toEqual([notShown]);
  });

  it('keeps a lost file whose queued entry is still DEFERRED, because that row makes no loss claim', () => {
    // The queued row exists but reads "Queued", so nothing on the page has yet said
    // this file is lost. Filtering it out here would remove the only statement of the
    // loss — the two arrays disagreeing is the case where `lost_files` is load-bearing.
    const entry = lost('mynewtesting/customer_1.csv');

    const result = lostFilesNotAlreadyQueued(
      [entry],
      [queued('mynewtesting/customer_1.csv', WIRE_STATUS_DEFERRED)],
    );

    expect(result).toEqual([entry]);
  });

  it('keeps a lost file whose queued entry carries an unrecognised status', () => {
    // An unrecognised status renders as "Queued" — the reader's default, which makes no
    // claim of loss (`LGR-99050e6-2`'s three states). So it is not "already shown"
    // either, and treating it as such would hide the loss behind a row that denies it.
    const entry = lost('mynewtesting/customer_1.csv');

    for (const status of ['QUEUED', 'FAILED_DLQ', '', 'pending']) {
      expect(lostFilesNotAlreadyQueued([entry], [queued(entry.source_key, status)])).toEqual([
        entry,
      ]);
    }
  });

  it('matches on source_key and not on file_name', () => {
    // Two prefixes can hold the same file name; `source_key` is unique per file because
    // the producer's row key is derived from it. Matching on `file_name` would hide a
    // real loss in one prefix because an unrelated file of the same name was queued in
    // another.
    const entry = lost('other_pipeline/customer_1.csv');

    const result = lostFilesNotAlreadyQueued(
      [entry],
      [queued('mynewtesting/customer_1.csv', WIRE_STATUS_LOST)],
    );

    expect(result).toEqual([entry]);
    expect(result[0].file_name).toBe('customer_1.csv');
  });

  it('preserves order and identity, adding and renaming nothing', () => {
    const a = lost('mynewtesting/a.csv');
    const b = lost('mynewtesting/b.csv');
    const c = lost('mynewtesting/c.csv');

    const result = lostFilesNotAlreadyQueued([a, b, c], []);

    // Same objects, not copies: the render reads `file_name`, `lost_at` and
    // `lost_reason` straight off these, and `trackBy="source_key"` needs a stable
    // identity.
    expect(result[0]).toBe(a);
    expect(result[1]).toBe(b);
    expect(result[2]).toBe(c);
  });

  it('answers for the two undefined shapes no render can produce', () => {
    // A response cached before `lost_files` shipped carries neither array. The pages
    // spell `?? []` at the call site, so only a direct call reaches these.
    expect(lostFilesNotAlreadyQueued(undefined, undefined)).toEqual([]);
    expect(lostFilesNotAlreadyQueued(undefined, [queued('mynewtesting/a.csv', WIRE_STATUS_LOST)])).toEqual(
      [],
    );

    const entry = lost('mynewtesting/a.csv');
    expect(lostFilesNotAlreadyQueued([entry], undefined)).toEqual([entry]);
  });

  it('does not mutate either argument', () => {
    const lostFiles = [lost('mynewtesting/a.csv'), lost('mynewtesting/b.csv')];
    const queuedFiles = [queued('mynewtesting/a.csv', WIRE_STATUS_LOST)];

    lostFilesNotAlreadyQueued(lostFiles, queuedFiles);

    expect(lostFiles).toHaveLength(2);
    expect(queuedFiles).toHaveLength(1);
  });
});

describe('distinctLostReasons — the producer’s own account, deduplicated', () => {
  it('always states a reason, for every input including none at all', () => {
    // R10.3's "and the reason" half. There is no input for which saying nothing is
    // correct, so the region can never render blank.
    for (const input of [undefined, [], [{}], [{ lost_reason: '' }]]) {
      const reasons = distinctLostReasons(input);

      expect(reasons).toHaveLength(1);
      expect(reasons[0].length).toBeGreaterThan(0);
      // The same sentence in every no-reason case, so "we have no reason on record"
      // reads one way rather than four.
      expect(reasons[0]).toBe(LOST_FILE_REASON_FALLBACK);
    }
  });

  it('returns the producer’s sentence when the wire carried one', () => {
    expect(distinctLostReasons([lost('mynewtesting/a.csv')])).toEqual([WIRE_LOST_REASON]);
  });

  it('collapses many rows carrying the same reason to one sentence', () => {
    // The steady state today: one producer constant, so three lost files yield one
    // sentence rather than three copies of it.
    const reasons = distinctLostReasons([
      lost('mynewtesting/a.csv'),
      lost('mynewtesting/b.csv'),
      lost('mynewtesting/c.csv'),
    ]);

    expect(reasons).toEqual([WIRE_LOST_REASON]);
  });

  it('states both reasons, in first-seen order, when two rows disagree', () => {
    // Unreachable from live data and the whole point of the function: taking the first
    // row's reason would mislabel the second row the day a second reason appears.
    const reasons = distinctLostReasons([
      lost('mynewtesting/a.csv', SECOND_REASON),
      lost('mynewtesting/b.csv', WIRE_LOST_REASON),
      lost('mynewtesting/c.csv', SECOND_REASON),
    ]);

    expect(reasons).toEqual([SECOND_REASON, WIRE_LOST_REASON]);
  });

  it('never renders the fallback beside a reason the producer did send', () => {
    // A blank reason on one row is "no reason recorded" for that row, not a reason of
    // `''`. It contributes nothing rather than a blank line, and it must not drag the
    // generic sentence in beside a specific one — which would read as a second,
    // contradictory account of the same loss.
    const reasons = distinctLostReasons([
      lost('mynewtesting/a.csv', ''),
      lost('mynewtesting/b.csv'),
    ]);

    expect(reasons).toEqual([WIRE_LOST_REASON]);
    expect(reasons).not.toContain(LOST_FILE_REASON_FALLBACK);
  });

  it('treats an absent lost_reason property the same as a blank one', () => {
    const reasons = distinctLostReasons([{}, { lost_reason: WIRE_LOST_REASON }]);

    expect(reasons).toEqual([WIRE_LOST_REASON]);
  });
});
