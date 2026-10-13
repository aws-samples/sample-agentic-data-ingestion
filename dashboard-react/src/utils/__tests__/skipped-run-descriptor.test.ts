// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { buildSkippedRunNavState } from '../skipped-run-descriptor';

/**
 * The skip descriptor both Run_List_Surfaces carry into the run-detail
 * navigation (p54 task 5.1, R6.1 / R6.2).
 *
 * The builder is small, but a defect in it disables task 5.2's whole honest-skip
 * branch **silently** — the page would fall through to the existing "No run with
 * id … exists." copy, which is the original defect rather than an error. So the
 * two things it must refuse to do are asserted directly:
 *
 * - decide skip-ness from the run id (do-NOT 4: two id shapes exist, one of them
 *   `run-`-prefixed on purpose, and `status` + `skip_reason` is the
 *   discriminator);
 * - emit a blank where the row carries nothing, which would hand 5.2 a value to
 *   render for a fact the row never recorded (`LGR-44d601e-1`, do-NOT 7).
 *
 * What the two surfaces do with it is covered by 5.2's Property 5, which reaches
 * the run-detail page from a list.
 *
 * **Validates: Requirements 6.1, 6.2**
 */
describe('buildSkippedRunNavState — only skips, and never by run id', () => {
  it('carries the duplicate skip row\u2019s own five fields', () => {
    expect(
      buildSkippedRunNavState({
        status: 'SKIPPED',
        skip_reason: 'duplicate_content',
        skip_detail: "Identical content was already processed successfully by run 'run-abc'.",
        previous_run_id: 'run-abc',
        file_name: 'customer_data_4.csv',
      }),
    ).toEqual({
      skippedRun: {
        status: 'SKIPPED',
        skip_reason: 'duplicate_content',
        skip_detail: "Identical content was already processed successfully by run 'run-abc'.",
        previous_run_id: 'run-abc',
        file_name: 'customer_data_4.csv',
      },
    });
  });

  it('omits what a contention skip does not record, rather than emitting blanks', () => {
    // `_record_contention_skip` (`pipeline_trigger:488-505`) writes no
    // `previous_run_id` — nothing was duplicated — and `file_name` only where the
    // trigger had a `source_key`.
    const state = buildSkippedRunNavState({
      status: 'SKIPPED',
      skip_reason: 'pipeline_lock_contention',
      skip_detail: 'A scheduled load did not run.',
    });

    expect(state?.skippedRun).toEqual({
      status: 'SKIPPED',
      skip_reason: 'pipeline_lock_contention',
      skip_detail: 'A scheduled load did not run.',
    });
    expect(state?.skippedRun).not.toHaveProperty('previous_run_id');
    expect(state?.skippedRun).not.toHaveProperty('file_name');
  });

  it('treats a blank field as absent, so no key is carried for it', () => {
    const state = buildSkippedRunNavState({
      status: 'SKIPPED',
      skip_reason: '   ',
      skip_detail: '',
      previous_run_id: '',
      file_name: '  ',
    });

    expect(state?.skippedRun).toEqual({ status: 'SKIPPED' });
  });

  it('still describes a skip that recorded no reason', () => {
    // `describeSkip` reports a reasonless skip AS reasonless rather than
    // returning null, so the descriptor exists and 5.2 has something to render.
    expect(buildSkippedRunNavState({ status: 'SKIPPED' })).toEqual({
      skippedRun: { status: 'SKIPPED' },
    });
  });

  it('agrees with describeSkip on a differently-cased status', () => {
    // One discriminator, shared with the badge R7 put on both surfaces: a row
    // cannot be badged as a skip on the list and refused as one on the detail
    // page.
    expect(buildSkippedRunNavState({ status: ' skipped ' })?.skippedRun.status).toBe('skipped');
  });

  it('carries nothing for a non-skip row', () => {
    expect(buildSkippedRunNavState({ status: 'SUCCEEDED', file_name: 'x.csv' })).toBeUndefined();
    expect(buildSkippedRunNavState({ status: 'FAILED' })).toBeUndefined();
    expect(buildSkippedRunNavState({})).toBeUndefined();
  });

  it('never reads the run id \u2014 either id shape, either direction (do-NOT 4)', () => {
    // A `skipped-`-prefixed id on a row that is not a skip must carry nothing…
    expect(
      buildSkippedRunNavState({
        run_id: 'skipped-7412636fccb938719673179658c05773',
        status: 'SUCCEEDED',
      } as Record<string, unknown>),
    ).toBeUndefined();

    // …and a `run-`-prefixed id on a row that IS a skip must carry the
    // descriptor. `_contention_skip_record_id` mints `run-{window}-skip-{reason}`
    // deliberately (`LGR-21a1b5b-4`), so a prefix test would miss every
    // contention skip.
    expect(
      buildSkippedRunNavState({
        run_id: 'run-1756400000-skip-pipeline_lock_contention',
        status: 'SKIPPED',
        skip_reason: 'pipeline_lock_contention',
      } as Record<string, unknown>),
    ).toEqual({
      skippedRun: { status: 'SKIPPED', skip_reason: 'pipeline_lock_contention' },
    });
  });
});
