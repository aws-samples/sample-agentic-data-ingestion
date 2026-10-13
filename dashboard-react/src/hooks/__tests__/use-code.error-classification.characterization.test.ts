// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 0.2 — characterization of `fetchTestResults`' error classification.
 *
 * **Validates: Requirements 3.1, 3.2, 3.3**
 *
 * `fetchTestResults` (`hooks/use-code.ts:22`, the classifying `catch` at `:54-65`)
 * has **no covering test at all**, and codegraph's flag is a verified true
 * positive rather than an artifact:
 *
 * - No `.ts`/`.tsx` file anywhere imports `use-code.ts`. The only importer is the
 *   barrel re-export at `hooks/index.ts:17`.
 * - All four test files that reference `useTestResults`
 *   (`TestResultsViewer.async.test.tsx:22`, `CodePage.test.tsx:7`,
 *   `SchemaHistoryPage.test.tsx:7`, `PipelineDetailPage.property.test.tsx:23`)
 *   `vi.mock` the `hooks` barrel and inject `data` / `isError` directly.
 * - The two `TestResultsViewer.verdict*` suites never reach the hook either —
 *   they exercise the presentational `TestResultsDisplay` and the pure
 *   `formatStaticAnalysisVerdict` export.
 *
 * So the code deciding what becomes an error and what becomes a swallowed 404 —
 * the code `TestResultsViewer`'s *"Failed to load test results."* alert hangs
 * off — runs in no test today. This file is what task 1.1 changes `api-client.ts`
 * against.
 *
 * **`fetchTestResults` is module-private**, so it is reached the only way its one
 * caller reaches it: through `useTestResults`. `use-code.ts` is not modified by
 * this task or by 1.1 and must end byte-identical.
 *
 * ## Where the expectations come from
 *
 * Not from current output. From R3.1–R3.3 plus the classification `use-code.ts:56`
 * performs, read together:
 *
 * - R3.2 requires a status of 400 or above to be classified **by that status**.
 *   `use-code.ts:56` swallows exactly one of those statuses, `404`. The derived
 *   consequence is therefore: `404` resolves to a pending result, and **every
 *   other** `>= 400` status must reach the caller as a rejection so the panel can
 *   say the read did not complete.
 * - R3.3 requires a sub-400 status with an unparseable body to raise an error
 *   *naming the body as unparseable*. That produces an `ApiError` whose status is
 *   **below** 400 — a shape that does not exist until task 1.1 lands. R3 does not
 *   ask `fetchTestResults` to treat it as pending, so the derived expectation is
 *   that it rejects like any other non-404. Asserted below, on an injected error,
 *   which is why the assertion is valid before 1.1 as well as after.
 * - The two outcomes must stay **distinguishable**, which is R3's whole subject:
 *   a swallowed 404 is a `pending` result carrying *"No test results available
 *   yet."*, and a classified failure is `isError`. Collapsing them is the defect.
 *
 * `LGR-2bcdd0d-3` (**template / ratified**) is the in-tree ruling behind that last
 * point, on the neighbouring 404: *"a 404 … means 'there is nothing to preview
 * here', not 'the read failed'"*, and the distinction is carried in **words**
 * rather than by an absence or a colour (it cites `LGR-99050e6-1`, WCAG 1.4.1).
 * That is why the pending copy is asserted below and not only the status field.
 * The same family governs the shape more generally: `LGR-99050e6-2` and
 * `LGR-8e34f37-3` both keep "not established" apart from a value.
 *
 * **Ledger status of the code under test, stated as a gap rather than an
 * all-clear:** `why` on `hooks/use-code.ts` returns four commits — the broad
 * publication-rename snapshot `7ad6ca1ad9` plus `2600abbc53`, `44856793fa` and
 * `f06bb5560f` — and every one carries `records: []`. `decisions` scoped to
 * `dashboard-react/src/hooks` and `.../src/services` returns nothing about the API
 * client's error classification either. So nothing fenced this behaviour before
 * p54; the expectations here rest on R3 and on the records cited above.
 *
 * The backend half of this contract is already asserted — `tests/contracts/
 * api_responses/test_tests_by_version_contract.py:218` and
 * `tests/unit/lambdas/dashboard_api/test_tests_by_version_error_details.py:189`
 * both pin that the endpoint answers `404` and both cite `use-code.ts:56-63` as
 * the reason. Nothing asserted the consumer side. That is the gap this closes.
 *
 * ## Deliberately not asserted: what an unparseable body produces
 *
 * Today `api-client.ts` reads the body before checking the status, so a
 * Non_JSON_Body propagates a `SyntaxError`; after task 1.1 the same response
 * yields an `ApiError` carrying the status. Pinning that transition here would
 * make this file fail after the change it exists to protect. So every error below
 * is **injected at `apiClient.get`**, and nothing here claims which response
 * shapes produce which error class — that is task 1.4's Property 3, with the
 * `_unwrap` side pinned by task 0.1's
 * `api-client.unwrap-outcomes.characterization.test.ts`.
 *
 * One consequence of 1.1 worth naming rather than testing: under R3.1 + R3.2 a
 * `404` carrying an unparseable body becomes `ApiError(404)` and will therefore
 * be **swallowed to pending**, where today it propagates as a `SyntaxError`. That
 * is a deliberate outcome of 1.1, not a regression, and it is why no assertion
 * below mentions a body's parseability.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

import { useTestResults } from '../use-code';
import { apiClient, ApiError } from '../../services/api-client';

const CODE_VERSION_ID = 'cv-2026-08-28-0001';

/**
 * A fresh `QueryClient` per test. `retry: false` so one injected rejection is one
 * observed outcome, and a per-test client so the 60 s `staleTime` cannot serve a
 * previous test's result.
 */
function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('fetchTestResults error classification (p54 0.2)', () => {
  // ── A 404 is swallowed — it is a result, not a failure ──────────────────────

  it('resolves a 404 to a pending result rather than reporting a failure', async () => {
    vi.spyOn(apiClient, 'get').mockRejectedValue(
      new ApiError(404, { error: `No test results found for ${CODE_VERSION_ID}` }),
    );

    const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.isError).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.data?.test_status).toBe('pending');
    // The copy is the observable half of R3's distinction: this sentence means
    // "there is nothing here yet", where `isError` means "the read did not
    // complete". `TestResultsViewer` renders one or the other, never both.
    expect(result.current.data?.test_output).toBe('No test results available yet.');
    // A swallowed 404 carries no error detail — there is no error to detail.
    expect(result.current.data?.error_details).toBeUndefined();
  });

  // ── Every other >= 400 status reaches the caller ────────────────────────────

  it('rejects on every other 4xx/5xx status, carrying that status', async () => {
    // R3.2's consequence for this function: 404 is the one swallowed status, so
    // each of these must stay visible as a failure. 502 and 504 are the edge
    // statuses R3 was written for — a CloudFront or WAF response that today
    // arrives as a body the client could not parse.
    for (const status of [400, 401, 403, 409, 500, 502, 504]) {
      vi.spyOn(apiClient, 'get').mockRejectedValue(
        new ApiError(status, { error: `upstream failure ${status}` }),
      );

      const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
        wrapper: makeWrapper(),
      });
      await waitFor(() => expect(result.current.isError).toBe(true));

      expect(result.current.error).toBeInstanceOf(ApiError);
      expect((result.current.error as ApiError).status).toBe(status);
      // Not silently downgraded to the pending result the 404 arm produces.
      expect(result.current.data).toBeUndefined();

      vi.restoreAllMocks();
    }
  });

  it('rejects an ApiError whose status is below 400 — the shape R3.3 introduces', async () => {
    // R3.3: a sub-400 response with an unparseable body raises an error naming
    // the body as unparseable. That `ApiError` carries a 2xx status, so the
    // `err.status === 404` guard does not match it and it must surface as a
    // failure rather than as "no results yet". Unreachable from a live response
    // until task 1.1 lands; injected here, so the assertion holds either side of
    // it, and `use-code.ts` needs no change to satisfy it.
    vi.spyOn(apiClient, 'get').mockRejectedValue(
      new ApiError(200, { error: 'Response body was not valid JSON' }),
    );

    const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect((result.current.error as ApiError).status).toBe(200);
    expect(result.current.data).toBeUndefined();
  });

  it('rejects a failure that is not an ApiError at all', async () => {
    // The guard at `:56` is class-gated *before* it is status-gated, so anything
    // that is not an `ApiError` bypasses the 404 arm whatever it means. This is
    // the mechanism R3 exists to correct — but the assertion is about
    // `fetchTestResults`, not about which responses produce this class, so it
    // holds unchanged after task 1.1 stops `api-client.ts` producing it.
    vi.spyOn(apiClient, 'get').mockRejectedValue(
      new SyntaxError('Unexpected token < in JSON at position 0'),
    );

    const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(SyntaxError);
    expect(result.current.error).not.toBeInstanceOf(ApiError);
    // Specifically NOT the pending result: a transport failure and an absent
    // resource are two facts.
    expect(result.current.data).toBeUndefined();
  });

  // ── The nested S3 report read is best-effort and classifies separately ──────

  it('does not fail the query when only the S3 report read fails', async () => {
    // `fetchTestResults` makes a second `apiClient.get` for the full report
    // (`:36`) inside its own `catch { }` (`:40`). That read goes through the same
    // `_unwrap` task 1.1 changes, so its failures are in this task's blast
    // radius: they must stay swallowed, leaving the metadata read's own result
    // intact rather than turning a readable test outcome into a failed panel.
    vi.spyOn(apiClient, 'get')
      .mockResolvedValueOnce({
        latest_test: {
          test_status: 'passed',
          test_timestamp: '2026-08-28T12:00:00Z',
          test_output_s3_uri: 's3://reports-bucket/reports/cv-2026-08-28-0001.md',
          test_results: { tests_passed: 3, tests_failed: 0, tests_run: 3 },
        },
      })
      .mockRejectedValueOnce(new ApiError(500, { error: 'S3 read failed' }));

    const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.isError).toBe(false);
    expect(result.current.data?.test_status).toBe('passed');
    expect(result.current.data?.executed_at).toBe('2026-08-28T12:00:00Z');
    // The summary is built from the metadata read, which succeeded.
    expect(result.current.data?.test_output).toBe('Tests: 3 passed, 0 failed, 3 total');
  });
});
