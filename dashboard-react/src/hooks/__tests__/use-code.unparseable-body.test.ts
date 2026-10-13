// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 1.1 — the operator-visible payoff, asserted through the real `_unwrap`.
 *
 * **Validates: Requirements 3.1, 3.2**
 *
 * Task 0.2 injects every error at `apiClient.get`, so `_unwrap` never runs there;
 * task 1.1's own suite stops at the client and asserts the class and status. Neither
 * shows the composition, and the composition is the defect R3 describes:
 *
 * > A Non_JSON_Body throws `SyntaxError`, which is not an `ApiError`, so
 * > `use-code.ts:56`'s `err instanceof ApiError && err.status === 404` cannot catch
 * > it. It propagates, `isError` becomes true, and `TestResultsViewer.tsx:257-271`
 * > replaces the whole panel with *"Failed to load test results."*
 *
 * So this file stubs **`fetch`**, not `apiClient`, and lets the real client classify.
 * `use-code.ts` is not modified by task 1.1 and is byte-identical; the behaviour
 * change is entirely in `api-client.ts`.
 *
 * ## AMENDMENT — the edge cause was removed; this file is unchanged and still valid
 *
 * The scenario this suite characterizes has since been fixed at its source. The
 * Hosting distribution carried `errorResponses` mapping 403/404 to HTTP 200 with
 * the body of `/index.html`; because CloudFront custom error responses are
 * distribution-wide and cannot be scoped to a behaviour, they also rewrote every
 * `/api/*` error. So the `404` this suite's first test stubs was, on the deployed
 * path, *never actually delivered as a 404* — it arrived as a 200 carrying HTML,
 * which is why the panel reported failure despite p54's fix. That mapping is gone:
 * the SPA fallback is now a viewer-request CloudFront Function attached to the
 * default (S3) behaviour only (`deploy-cdk/lib/spa-router-function.ts`, pinned by
 * `deploy-cdk/test/p56-spa-router-function.test.ts`).
 *
 * **Nothing here needed amending beyond this note.** Both tests stub `fetch`
 * directly, so neither ever traversed CloudFront, and both assertions describe
 * `_unwrap` + `fetchTestResults` composition rather than edge behaviour:
 *
 * - The `404`-with-HTML case is now reachable for other reasons (a proxy, a
 *   gateway error page from something other than this distribution) and its
 *   handling is still what R3 requires.
 * - The `502`-with-HTML case is untouched and is the arm that the edge fix makes
 *   *more* important, because a genuine 502 now reaches the client as a 502
 *   instead of being laundered into a 200.
 *
 * The error state that these classifications feed can now also heal itself rather
 * than waiting on a Retry click — see `use-code.error-recovery.test.ts`.
 *
 * 0.2's docblock names this outcome in advance and deliberately does not assert it:
 * *"a `404` carrying an unparseable body becomes `ApiError(404)` and will therefore
 * be swallowed to pending, where today it propagates as a `SyntaxError`. That is a
 * deliberate outcome of 1.1, not a regression."* This is the assertion of it.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

import { useTestResults } from '../use-code';
import { ApiError } from '../../services/api-client';

const CODE_VERSION_ID = 'cv-2026-08-28-0002';

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

/** Stub `fetch` so the real `_unwrap` does the classifying. */
function stubFetch(status: number, body: string | null) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(body, { status, headers: { 'Content-Type': 'text/html' } }),
      ),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('fetchTestResults over a body the client could not read (p54 1.1)', () => {
  it('swallows a 404 whose body is not JSON to a pending result', async () => {
    // Before 1.1 this produced `SyntaxError` -> `isError` -> "Failed to load test
    // results." After it, the 404 is classified by its status and the existing
    // 404 arm at `use-code.ts:56` catches it, exactly as it does for a JSON 404.
    stubFetch(404, '<html><body>Not Found</body></html>');

    const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.isError).toBe(false);
    expect(result.current.data?.test_status).toBe('pending');
    expect(result.current.data?.test_output).toBe('No test results available yet.');
  });

  it('still reports a 502 whose body is not JSON as a failure, carrying 502', async () => {
    // The other half of R3.2: a genuine edge failure must stay visible rather
    // than being swallowed alongside the 404. The status is what separates them,
    // and it is now available because the error is an `ApiError`.
    stubFetch(502, '<html><body><h1>502 Bad Gateway</h1></body></html>');

    const { result } = renderHook(() => useTestResults(CODE_VERSION_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect((result.current.error as ApiError).status).toBe(502);
    expect(result.current.data).toBeUndefined();
  });
});
