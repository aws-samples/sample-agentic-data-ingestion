// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { describeSkip } from '../skip-reason';

/**
 * The skip-reason table in the run list (p30 task 4.9, R10.2).
 *
 * R10.2's literal clause was already satisfied before this table existed: a
 * `SKIPPED` row falls through `statusType`'s `default: 'info'` and renders blue
 * where `FAILED` renders red. What was missing is the reason, so these tests are
 * about the reason and about the two things the table must refuse to do:
 *
 * - collapse the two contention reasons into one "Contention" badge (they clear
 *   for different causes and send the operator to different places);
 * - guess at a reason it does not recognise.
 *
 * What the table renders inside `ExecutionsPage` is covered in
 * `pages/__tests__/ExecutionsPage.skipped.test.tsx`.
 *
 * **Validates: Requirements 10.2**
 */
describe('describeSkip — only skips', () => {
  it('returns null for a successful run', () => {
    expect(describeSkip('SUCCEEDED', undefined, undefined)).toBeNull();
  });

  it('returns null for a failure, which is the state a skip must not resemble', () => {
    expect(describeSkip('FAILED', undefined, undefined)).toBeNull();
  });

  it('returns null for a missing status', () => {
    expect(describeSkip(undefined)).toBeNull();
    expect(describeSkip(null)).toBeNull();
    expect(describeSkip('')).toBeNull();
  });

  it('matches the status case-insensitively and ignores padding', () => {
    expect(describeSkip('  skipped ', 'global_concurrency')).not.toBeNull();
  });
});

describe('describeSkip — the reason table', () => {
  it('names duplicate content and says nothing is wrong', () => {
    const indicator = describeSkip('SKIPPED', 'duplicate_content');
    expect(indicator?.label).toBe('Skipped: duplicate content');
    expect(indicator?.color).toBe('blue');
    expect(indicator?.detail).toContain('Nothing is wrong');
  });

  it('names what the duplicate record carries, so a false positive is checkable', () => {
    // p64 R5.1 put both sides' pre-image on the row and R5.2 named the matched
    // FILE, not only the previous run id — because the reported false positive
    // could not be investigated from a record naming just a run. The entry cannot
    // restate per-row values, so what R5.6 asks of it is to say those facts exist
    // and where they came from. Asserted as concepts, not as a transcribed
    // sentence: the wording is free to change, the promise is not.
    const detail = describeSkip('SKIPPED', 'duplicate_content')?.detail ?? '';
    expect(detail).toMatch(/size and ETag/); // the match basis (R5.3 — unchanged)
    expect(detail).toMatch(/\bfile\b/i); // the matched file (R5.2)
    expect(detail).toMatch(/both sides/i); // both pre-images, not one (R5.1)
  });

  it('names the concurrency ceiling and says nothing retried it', () => {
    const indicator = describeSkip('SKIPPED', 'global_concurrency');
    expect(indicator?.label).toBe('Skipped: at concurrency limit');
    expect(indicator?.color).toBe('severity-medium');
    expect(indicator?.detail).toContain('Nothing retried it');
  });

  it('names lock contention and points at the schedule, not the fleet', () => {
    const indicator = describeSkip('SKIPPED', 'pipeline_lock_contention');
    expect(indicator?.label).toBe('Skipped: previous run still going');
    expect(indicator?.detail).toContain('schedule fires faster');
  });

  it('does not collapse the two contention reasons into one badge', () => {
    const concurrency = describeSkip('SKIPPED', 'global_concurrency');
    const lock = describeSkip('SKIPPED', 'pipeline_lock_contention');
    expect(concurrency?.label).not.toBe(lock?.label);
    expect(concurrency?.detail).not.toBe(lock?.detail);
  });

  it('grades the harmless reason differently from the costly ones', () => {
    // A duplicate skip cost nothing; a contention skip cost a load.
    expect(describeSkip('SKIPPED', 'duplicate_content')?.color).toBe('blue');
    expect(describeSkip('SKIPPED', 'global_concurrency')?.color).toBe('severity-medium');
  });
});

describe('describeSkip — reasons it does not recognise', () => {
  it('reports an unknown reason AS unknown rather than guessing', () => {
    const indicator = describeSkip('SKIPPED', 'some_future_reason');
    expect(indicator?.label).toBe('Skipped: some_future_reason');
    expect(indicator?.detail).toContain('does not recognise');
    expect(indicator?.color).toBe('grey');
  });

  it('still reports a skip that carries no reason at all', () => {
    // The row exists because something decided not to run. Rendering nothing
    // would leave a bare "SKIPPED" whose cause the reader has to guess.
    const indicator = describeSkip('SKIPPED', '');
    expect(indicator).not.toBeNull();
    expect(indicator?.label).toBe('Skipped');
    expect(indicator?.detail).toContain('No reason was recorded');
  });
});

describe('describeSkip — the row’s own recorded detail', () => {
  it('appends the recorded detail to the table’s explanation', () => {
    const indicator = describeSkip(
      'SKIPPED',
      'global_concurrency',
      'Global concurrency limit reached: at least 11 running of a maximum 10.',
    );
    expect(indicator?.detail).toContain('Nothing retried it');
    expect(indicator?.detail).toContain('at least 11 running of a maximum 10');
  });

  it('leaves the explanation alone when there is no recorded detail', () => {
    const withoutDetail = describeSkip('SKIPPED', 'global_concurrency');
    expect(describeSkip('SKIPPED', 'global_concurrency', '   ')?.detail).toBe(withoutDetail?.detail);
    expect(describeSkip('SKIPPED', 'global_concurrency', null)?.detail).toBe(withoutDetail?.detail);
  });

  it('carries the recorded detail through on an unrecognised reason too', () => {
    const indicator = describeSkip('SKIPPED', 'mystery', 'something specific happened');
    expect(indicator?.detail).toContain('something specific happened');
  });

  it('states the duplicate remediation once, not once per source (p64 R5.6)', () => {
    // The composed tooltip is `INDICATORS[reason].detail` + the row's own
    // `skip_detail`, and `_record_duplicate_skip` sets `skip_detail`
    // unconditionally — every wording it has written, including the one-sentence
    // form predating p64, ends with the Run Now instruction. The table used to end
    // with it too, so the operator read the same instruction twice in one hover.
    //
    // R5.6 is satisfied by the table NOT restating what the producer composes; the
    // fixture below is `_duplicate_skip_detail`'s current output shape.
    const serverDetail =
      "Identical content was already processed successfully by run 'run-2026-08-25-11-02-33-9f1c2e'" +
      " from 'customers.csv'. That content was 1024 bytes, ETag d41d8cd98f00b204e9800998ecf8427e." +
      ' This submission is 1024 bytes, ETag d41d8cd98f00b204e9800998ecf8427e.' +
      ' The match is on size and ETag only.' +
      ' Use Run Now with the duplicate acknowledgement to process it again.';

    const detail = describeSkip('SKIPPED', 'duplicate_content', serverDetail)?.detail ?? '';
    expect(detail.match(/duplicate acknowledgement/g)).toHaveLength(1);

    // And the specifics still reach the reader — the point of appending at all.
    expect(detail).toContain('customers.csv');
    expect(detail).toContain('1024 bytes');
  });
});
