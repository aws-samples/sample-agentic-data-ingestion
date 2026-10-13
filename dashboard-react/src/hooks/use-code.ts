// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient, ApiError } from '../services/api-client';
import { artifactKeys, codeKeys } from './query-keys';
import type { CodeDetail, RunArtifacts, TestResult } from '../types';

async function fetchCodeVersion(codeVersionId: string): Promise<CodeDetail> {
  try {
    return await apiClient.get<CodeDetail>(`/code/${encodeURIComponent(codeVersionId)}`);
  } catch (err: unknown) {
    if (err instanceof ApiError && err.status === 404) {
      return {
        code_version_id: codeVersionId,
        code: '',
        language: 'python',
        created_at: '',
      };
    }
    throw err;
  }
}

async function fetchTestResults(codeVersionId: string): Promise<TestResult> {
  try {
    const body = await apiClient.get<Record<string, unknown>>(`/tests/${encodeURIComponent(codeVersionId)}`);
    const test = (body?.latest_test ?? body) as Record<string, unknown>;
    const results = (test?.test_results ?? {}) as Record<string, unknown>;

    // Try to fetch the full test report from S3
    let s3Report: Record<string, unknown> | null = null;
    const s3Uri = test?.test_output_s3_uri as string | undefined;
    if (s3Uri) {
      try {
        const match = s3Uri.match(/^s3:\/\/([^/]+)\/(.+)$/);
        if (match) {
          const [, bucket, key] = match;
          s3Report = await apiClient.get<Record<string, unknown>>(
            `/s3-content?bucket=${encodeURIComponent(bucket)}&key=${encodeURIComponent(key)}`,
          );
        }
      } catch { /* best-effort */ }
    }

    const reviewResult = (s3Report?.review_result as string) ?? '';
    const summary = typeof results === 'object' && results !== null
      ? `Tests: ${results.tests_passed ?? 0} passed, ${results.tests_failed ?? 0} failed, ${results.tests_run ?? 0} total`
      : '';

    return {
      test_status: (s3Report?.test_status as string) ?? test?.test_status ?? test?.validation_status ?? 'unknown',
      test_output: reviewResult || summary,
      error_details: (test?.error_details as string) ?? undefined,
      executed_at: (s3Report?.test_timestamp as string) ?? test?.test_timestamp ?? undefined,
    };
  } catch (err: unknown) {
    // 404 means no test results exist yet — return empty result instead of throwing
    if (err instanceof ApiError && err.status === 404) {
      return {
        test_status: 'pending',
        test_output: 'No test results available yet.',
        error_details: undefined,
        executed_at: undefined,
      };
    }
    throw err;
  }
}

export function useCodeVersion(codeVersionId: string | undefined) {
  return useQuery({
    queryKey: codeKeys.detail(codeVersionId!),
    queryFn: () => fetchCodeVersion(codeVersionId!),
    enabled: !!codeVersionId,
    staleTime: 60_000,
  });
}

/** Poll cadence while a test run is genuinely still in flight. */
export const TEST_RESULTS_PENDING_POLL_MS = 5_000;

/**
 * Poll cadence while the query is in its ERROR state, and the bound on it.
 *
 * ## Why the error state polls at all
 *
 * `refetchInterval` used to key solely on `query.state.data?.test_status ===
 * 'pending'`. In the error state `data` is `undefined`, so the predicate was
 * false and **no polling ever started** — the panel stayed on *"Failed to load
 * test results."* even after the tests finished, and the only way out was a
 * Retry click. That is why the Code tab was still broken at the approval gate:
 * the failure was transient at the edge, the underlying run completed, and
 * nothing ever asked again.
 *
 * The distribution-wide `errorResponses` that produced the original failure are
 * gone (`deploy-cdk/lib/spa-router-function.ts`), so the specific 200-carrying-
 * HTML case no longer occurs. This recovery is for the class the edge fix cannot
 * remove: a 502/504 while the API is cold or redeploying, or a transient WAF
 * block, landing on a panel whose real answer arrives seconds later.
 *
 * ## The bound, and why these numbers
 *
 * 15 s x 8 attempts ≈ **2 minutes**, then it stops for good and the existing
 * Retry button is the escape hatch.
 *
 * - **Slower than pending's 5 s** because an error is a weaker signal than
 *   `pending`. `pending` is the server saying "ask me again"; an error is the
 *   read not completing, and hammering it adds load to whatever is already
 *   failing.
 * - **Bounded rather than indefinite** because most failures reachable here are
 *   deterministic, not transient. A 403 from the Cognito authorizer on an
 *   expired session and a 500 from a broken handler will answer identically
 *   forever, and an unbounded poll turns one broken panel into a permanent
 *   request loop against a known-bad endpoint for as long as the tab is open.
 *   `main.tsx:23-26` already declines to retry any 4xx for the same reason.
 * - **~2 minutes** because that is the window a transient edge failure or a
 *   just-finishing test run resolves in. Beyond it, a human deciding to retry is
 *   better information than a timer.
 *
 * The counter is `fetchFailureCount`, which React Query resets to 0 on a
 * successful fetch — so a recovery genuinely clears the budget rather than
 * leaving it spent. Its exact increment semantics under the app's `retry`
 * predicate are asserted in `__tests__/use-code.error-recovery.test.ts` rather
 * than assumed from the documentation.
 */
export const TEST_RESULTS_ERROR_POLL_MS = 15_000;
export const TEST_RESULTS_ERROR_MAX_ATTEMPTS = 8;

export function useTestResults(codeVersionId: string | undefined) {
  return useQuery({
    queryKey: codeKeys.testResults(codeVersionId!),
    queryFn: () => fetchTestResults(codeVersionId!),
    enabled: !!codeVersionId,
    staleTime: 60_000,
    refetchInterval: (query) => {
      // Poll every 5s while test results are still pending.
      if (query.state.data?.test_status === 'pending') return TEST_RESULTS_PENDING_POLL_MS;

      // The error state must be able to heal without a Retry click — but not
      // forever. See the constants above for the reasoning behind both halves.
      if (query.state.status === 'error') {
        return query.state.fetchFailureCount <= TEST_RESULTS_ERROR_MAX_ATTEMPTS
          ? TEST_RESULTS_ERROR_POLL_MS
          : false;
      }

      return false;
    },
  });
}

async function fetchRunArtifacts(pipelineId: string, runId: string): Promise<RunArtifacts> {
  return apiClient.get<RunArtifacts>(
    `/pipeline/${encodeURIComponent(pipelineId)}/run/${encodeURIComponent(runId)}/artifacts`,
  );
}

export function useRunArtifacts(pipelineId: string | undefined, runId: string | undefined) {
  return useQuery({
    queryKey: artifactKeys.run(pipelineId!, runId!),
    queryFn: () => fetchRunArtifacts(pipelineId!, runId!),
    enabled: !!pipelineId && !!runId,
    staleTime: 60_000,
  });
}
