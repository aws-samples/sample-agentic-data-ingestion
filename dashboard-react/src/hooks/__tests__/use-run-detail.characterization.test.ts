// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Characterization tests for `fetchRunDetail` (`use-run-detail.ts`).
 *
 * Feature: p38-retry-run-identity
 *
 * `fetchRunDetail` is the one genuine zero-coverage finding in the p38 sweep,
 * and it is the function Requirement 4 is entirely about. These tests import
 * the REAL function. They deliberately do not follow the pattern in the
 * sibling file `use-run-detail.property.test.ts`, which declares a local copy
 * of the polling logic and therefore covers nothing in this module.
 *
 * Expectations come from `R4.5` and do-NOT 7 — the 404-as-`null` convention is
 * deliberate and repo-wide — not from current output. Current output collapses
 * two distinct outcomes to `null`, which is the defect p38 task 8.1 fixes.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import * as fc from 'fast-check';

import { fetchRunDetail } from '../use-run-detail';
import { apiClient, ApiError } from '../../services/api-client';

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Feature: p38-retry-run-identity, Property 8: 404 resolves and every other failure throws
// ---------------------------------------------------------------------------

describe('Feature: p38-retry-run-identity, Property 8: 404 resolves and every other failure throws', () => {
  /**
   * **Validates: Requirements 4.5**
   *
   * For any HTTP status in 400–599, `fetchRunDetail` resolves to `null` when
   * the status is `404` and rejects otherwise.
   *
   * This is the do-NOT 7 guard. A 404 must stay a non-error result — it is
   * what keeps the global 404-toast suppression and retry-disable intact — and
   * every other failure status must keep reaching the caller so the page can
   * report that the request did not complete.
   */
  it('resolves to null on 404 and rejects on every other 4xx/5xx status', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.integer({ min: 400, max: 599 }),
        async (status) => {
          vi.spyOn(apiClient, 'get').mockRejectedValue(
            new ApiError(status, { error: `upstream failure ${status}` }),
          );

          if (status === 404) {
            await expect(fetchRunDetail('run-2026-08-09-15-38-19-5c5f5f8a')).resolves.toBeNull();
          } else {
            await expect(
              fetchRunDetail('run-2026-08-09-15-38-19-5c5f5f8a'),
            ).rejects.toBeInstanceOf(ApiError);
          }

          vi.restoreAllMocks();
        },
      ),
      {
        numRuns: 100,
        // 404 is the one status whose behaviour differs, and it is not a
        // boundary of the generated range, so a random 100-run sample can miss
        // it. Pinning it as an example keeps the generator exactly as
        // specified while guaranteeing the resolving arm is exercised.
        examples: [[404]],
      },
    );
  });
});
// ---------------------------------------------------------------------------
// p38 task 1.2 — the `body.error`-on-200 arm
//
// EXPECTED-RED WINDOW CLOSED by p38 task 8.1, which narrowed
// `fetchRunDetail`'s `null` return to the 404 branch only (design.md §4.7):
// the `body.error` branch now throws `new Error(String(body.error))`. These
// two tests were red from the moment they were written until that change, and
// they are green from it onwards. A red here now is a regression, not a
// window — it would mean the collapse `R4.3` forbids has come back, with the
// page unable to tell "the request did not complete" from "no such run"
// because both arrive as `null`.
//
// The assertions are unchanged from task 1.2. They match on the message
// rather than on the thrown class, so they hold whether the rejection is a
// plain `Error` (what 8.1 chose, per §4.7's snippet) or an `ApiError`.
//
// Task 1.1's Property 8 above is separate and must stay GREEN: 404 still
// resolves to `null`, which is what keeps do-NOT 7 / `R4.5` intact.
// ---------------------------------------------------------------------------

describe('Feature: p38-retry-run-identity, task 1.2: a 200 carrying `error` rejects', () => {
  /**
   * **Validates: Requirements 4.2**
   *
   * A `200` whose body carries `error` means the request completed but the
   * response is not a run. `R4.2` requires the page to state that the run
   * details could not be retrieved, which it can only do from `isError` — so
   * this outcome must reach the caller as a rejection rather than as `null`.
   */
  it('rejects instead of resolving to null when a 200 body carries `error`', async () => {
    vi.spyOn(apiClient, 'get').mockResolvedValue({
      error: 'Internal server error retrieving run details',
    });

    await expect(
      fetchRunDetail('run-2026-08-09-15-38-19-5c5f5f8a'),
    ).rejects.toThrow(/Internal server error retrieving run details/);
  });

  /**
   * **Validates: Requirements 4.3**
   *
   * The two outcomes must be distinguishable by the caller. A `404` resolves
   * to `null` (`R4.5`, do-NOT 7 — unchanged), and a `200` carrying `error`
   * must not, or the page has nothing to branch on and renders one string for
   * both states.
   */
  it('does not resolve a 200-carrying-`error` to the same value a 404 resolves to', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(
      new ApiError(404, { error: 'Run not found: run-2026-08-09-15-38-19-5c5f5f8a' }),
    );
    const notFound = await fetchRunDetail('run-2026-08-09-15-38-19-5c5f5f8a');
    expect(notFound).toBeNull();

    vi.restoreAllMocks();

    vi.spyOn(apiClient, 'get').mockResolvedValue({ error: 'Run details unavailable' });
    await expect(
      fetchRunDetail('run-2026-08-09-15-38-19-5c5f5f8a'),
    ).rejects.toBeInstanceOf(Error);
  });
});
