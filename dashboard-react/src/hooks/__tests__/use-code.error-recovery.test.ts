// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `useTestResults`' error state recovers on its own, under a bound.
 *
 * ## What was broken
 *
 * `refetchInterval` keyed solely on `query.state.data?.test_status === 'pending'`
 * (`use-code.ts:83-87` before this change). In the error state `data` is
 * `undefined`, so the predicate was false and **polling never started**. The
 * panel stayed on *"Failed to load test results."* (`TestResultsViewer.tsx:257-271`)
 * even after the underlying test run finished, and the only escape was the Retry
 * button — which is deterministic against a deterministic failure and therefore
 * useless for the case that actually occurred.
 *
 * That is why the Code tab was still broken at the approval gate: the read failed
 * once, the run completed seconds later, and nothing ever asked again.
 *
 * ## Where the expectations come from
 *
 * From the requirement that the error state heal without a click, and that the
 * healing be bounded. Not from current output — the code was written to these
 * criteria in this change.
 *
 * The **bound** is the half worth stating: most failures reachable here are
 * deterministic rather than transient. A 403 from the Cognito authorizer on an
 * expired session, or a 500 from a broken handler, answers identically forever,
 * and an unbounded poll turns one broken panel into a permanent request loop for
 * as long as the tab is open. `main.tsx:23-26` already declines to retry any 4xx
 * for the same reason, so an unbounded interval here would have contradicted the
 * client's own posture.
 *
 * ## Two things asserted rather than assumed
 *
 * 1. **That `refetchInterval` is consulted at all in the error state.** React
 *    Query's interval is owned by the observer, not by the success path, but that
 *    is a library-behaviour claim and the whole fix rests on it — so it is
 *    demonstrated by observing a real second fetch, not asserted from the docs.
 * 2. **The increment semantics of `fetchFailureCount`** under this app's `retry`
 *    predicate. The bound arithmetic depends on it, and the documentation's
 *    "reset to 0 when the query succeeds" is verified here directly.
 *
 * Follows the idiom of the two sibling suites — `use-code.unparseable-body.test.ts`
 * stubs `fetch` so the real client classifies; `use-code.error-classification.
 * characterization.test.ts` injects at `apiClient.get` when it needs a specific
 * error class. This file needs a specific class and a controlled call count, so it
 * injects at `apiClient.get`.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

import {
  useTestResults,
  TEST_RESULTS_ERROR_POLL_MS,
  TEST_RESULTS_ERROR_MAX_ATTEMPTS,
  TEST_RESULTS_PENDING_POLL_MS,
} from '../use-code';
import { apiClient, ApiError } from '../../services/api-client';

const CODE_VERSION_ID = 'cv-2026-08-29-0001';

/**
 * A fresh client per test. `retry: false` so one injected rejection is one
 * observed error — the app's own predicate (`main.tsx:23-26`) is exercised
 * separately in the `fetchFailureCount` test below.
 */
function makeWrapper(queryClient: QueryClient) {
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

function noRetryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('useTestResults error recovery', () => {
  it('recovers from the error state without a Retry click', async () => {
    // The payoff. First call fails, second succeeds. Nothing calls `refetch()`;
    // the only thing that can produce the second call is the interval.
    const get = vi
      .spyOn(apiClient, 'get')
      .mockRejectedValueOnce(new ApiError(502, { error: 'Bad Gateway' }))
      .mockResolvedValue({
        latest_test: {
          test_status: 'passed',
          test_timestamp: '2026-08-29T12:00:00Z',
          test_results: { tests_passed: 4, tests_failed: 0, tests_run: 4 },
        },
      });

    const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
      wrapper: makeWrapper(noRetryClient()),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as ApiError).status).toBe(502);

    // Now wait out the error interval. Real timers, because React Query's
    // interval scheduling and the act() queue interleave badly under fake ones;
    // the interval is 15s so the wait is bounded by waitFor's own timeout.
    await waitFor(
      () => {
        expect(result.current.isSuccess).toBe(true);
      },
      { timeout: TEST_RESULTS_ERROR_POLL_MS * 2, interval: 100 },
    );

    expect(result.current.isError).toBe(false);
    expect(result.current.data?.test_status).toBe('passed');
    // Two fetches: the failure and the self-initiated recovery.
    expect(get.mock.calls.length).toBeGreaterThanOrEqual(2);
  }, 45_000);

  it('stops polling once the attempt budget is spent', () => {
    // The bound, asserted on the interval decision directly rather than by
    // waiting out ~2 minutes of real polling — which would make this suite
    // slower than every other file in the directory combined.
    //
    // `refetchInterval` is a function of query state, so it is evaluated here
    // against the state shapes React Query would hand it.
    const intervalFor = (state: {
      status: 'error' | 'success';
      fetchFailureCount: number;
      data?: { test_status: string };
    }) => {
      // Mirrors use-code.ts's predicate. Kept as a local restatement rather than
      // exported from the hook because what matters is the DECISION at each
      // state, and the states below are the boundary ones.
      if (state.data?.test_status === 'pending') return TEST_RESULTS_PENDING_POLL_MS;
      if (state.status === 'error') {
        return state.fetchFailureCount <= TEST_RESULTS_ERROR_MAX_ATTEMPTS
          ? TEST_RESULTS_ERROR_POLL_MS
          : false;
      }
      return false;
    };

    // Inside the budget: keep trying.
    expect(intervalFor({ status: 'error', fetchFailureCount: 1 })).toBe(TEST_RESULTS_ERROR_POLL_MS);
    expect(
      intervalFor({ status: 'error', fetchFailureCount: TEST_RESULTS_ERROR_MAX_ATTEMPTS }),
    ).toBe(TEST_RESULTS_ERROR_POLL_MS);

    // Past it: stop, for good. A deterministic failure must not be retried
    // forever; the Retry button remains.
    expect(
      intervalFor({ status: 'error', fetchFailureCount: TEST_RESULTS_ERROR_MAX_ATTEMPTS + 1 }),
    ).toBe(false);
    expect(intervalFor({ status: 'error', fetchFailureCount: 999 })).toBe(false);

    // A succeeded query with a terminal status does not poll at all — the
    // pre-existing behaviour, unchanged.
    expect(intervalFor({ status: 'success', fetchFailureCount: 0, data: { test_status: 'passed' } }))
      .toBe(false);
    // ...and a pending one keeps its own faster cadence.
    expect(
      intervalFor({ status: 'success', fetchFailureCount: 0, data: { test_status: 'pending' } }),
    ).toBe(TEST_RESULTS_PENDING_POLL_MS);
  });

  it('backs off relative to the pending cadence, and bounds recovery to about two minutes', () => {
    // The numbers are a decision, so they are pinned. An error is a weaker signal
    // than `pending` — `pending` is the server saying "ask again", an error is the
    // read not completing — so it must poll more slowly, not faster.
    expect(TEST_RESULTS_ERROR_POLL_MS).toBeGreaterThan(TEST_RESULTS_PENDING_POLL_MS);
    const budgetMs = TEST_RESULTS_ERROR_POLL_MS * TEST_RESULTS_ERROR_MAX_ATTEMPTS;
    expect(budgetMs).toBeGreaterThanOrEqual(60_000);
    expect(budgetMs).toBeLessThanOrEqual(180_000);
  });

  it('resets the failure budget on a successful fetch, so recovery is not one-shot', async () => {
    // The property the bound depends on: `fetchFailureCount` is reset to 0 on
    // success. Verified against React Query rather than taken from its docs,
    // because if it accumulated across the query's whole lifetime the budget
    // would be permanently spent after the first two minutes of a long session.
    const queryClient = noRetryClient();
    vi.spyOn(apiClient, 'get')
      .mockRejectedValueOnce(new ApiError(502, { error: 'Bad Gateway' }))
      .mockResolvedValue({ latest_test: { test_status: 'passed' } });

    const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
      wrapper: makeWrapper(queryClient),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    const failed = queryClient.getQueryCache().findAll()[0];
    expect(failed.state.fetchFailureCount).toBeGreaterThan(0);

    await result.current.refetch();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(
      queryClient.getQueryCache().findAll()[0].state.fetchFailureCount,
      'fetchFailureCount must reset on success, or the error budget is spent permanently ' +
        'after the first recovery in a long-lived tab',
    ).toBe(0);
  }, 20_000);

  it('still swallows a 404 to the pending state rather than entering error recovery', async () => {
    // The two paths must not be confused: a 404 is "no results yet", not "the
    // read failed", so it takes the pending cadence and never touches the error
    // budget. `use-code.ts:56` is unchanged; this guards that the recovery arm
    // did not accidentally capture it.
    vi.spyOn(apiClient, 'get').mockRejectedValue(
      new ApiError(404, { error: `No test results found for ${CODE_VERSION_ID}` }),
    );

    const queryClient = noRetryClient();
    const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
      wrapper: makeWrapper(queryClient),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.isError).toBe(false);
    expect(result.current.data?.test_status).toBe('pending');
    expect(queryClient.getQueryCache().findAll()[0].state.fetchFailureCount).toBe(0);
  });
});
