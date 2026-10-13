// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p51 task 3.1 — `executionKeys.pendingApprovals` and `usePipelinePendingApprovals`.
 *
 * Two correctness properties from the design, plus the request shape the hook is
 * for. Both properties are about **cache keying**, which is why they are asserted
 * against a real `QueryClient` rather than a spy: what is under test is React
 * Query's own prefix matching, and a mock would only re-assert this test's own
 * beliefs about it.
 *
 * **Validates: Requirements 3.1, 3.2**
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

// Spy on the API client's GET — the hook's only side effect.
const getMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: { get: getMock },
  ApiError: class ApiError extends Error {},
}));

import { usePipelinePendingApprovals } from '../use-approvals';
import { approvalKeys, executionKeys, pipelineKeys } from '../query-keys';

const PIPELINE_KEY = 'pipe-1';

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function makeWrapper(queryClient: QueryClient) {
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

/** Seed the key with data so it becomes a real, invalidatable cache entry. */
function seed(qc: QueryClient) {
  qc.setQueryData(executionKeys.pendingApprovals(PIPELINE_KEY), []);
  expect(
    qc.getQueryState(executionKeys.pendingApprovals(PIPELINE_KEY))?.isInvalidated,
  ).toBe(false);
}

beforeEach(() => {
  getMock.mockReset();
  getMock.mockResolvedValue([]);
});

/**
 * Property 5: the pipeline id stays in second position.
 *
 * `LGR-8b6e6a1-1` (**template / ratified**) has a WebSocket frame invalidate the
 * **prefix** `['executions', id]`, so any key member placed ahead of the id stops
 * the flush matching and a live run stops refreshing. Asserted on the literal key
 * array so the constraint is enforced by a test rather than by a comment.
 *
 * **Validates: Requirements 3.2**
 */
describe('P5 — executionKeys.pendingApprovals key shape', () => {
  it('is exactly [executions, pipelineKey, approvals]', () => {
    expect(executionKeys.pendingApprovals(PIPELINE_KEY)).toEqual([
      'executions',
      PIPELINE_KEY,
      'approvals',
    ]);
  });

  it('keeps the pipeline id in second position, so the flush prefix reaches it', () => {
    const key = executionKeys.pendingApprovals(PIPELINE_KEY);
    expect(key[1]).toBe(PIPELINE_KEY);
    // The exact prefix the WebSocket flush invalidates (`WebSocketProvider.tsx:556`).
    expect(key.slice(0, 2)).toEqual([...executionKeys.all, PIPELINE_KEY]);
  });

  it('cannot collide with the executionKeys.list family', () => {
    const approvals = executionKeys.pendingApprovals(PIPELINE_KEY);
    // `'approvals'` sits where `list` puts a numeric `page`, so no page position
    // can ever produce this key — checked over a range rather than one page.
    for (const page of [1, 2, 3, 10, 99]) {
      const list = executionKeys.list(PIPELINE_KEY, page, null, null);
      expect(list).not.toEqual(approvals);
      expect(list[2]).not.toBe(approvals[2]);
      // Neither array is a prefix of the other, so neither invalidation can
      // reach the other family by prefix match.
      expect(list.slice(0, approvals.length)).not.toEqual([...approvals]);
      expect(approvals.slice(0, list.length)).not.toEqual([...list]);
    }
    // Nor with the other non-list member filed under the same prefix.
    expect(approvals).not.toEqual(executionKeys.neighbours(PIPELINE_KEY, 'run-1'));
  });

  it('does not disturb the list family in the cache', () => {
    const qc = makeClient();
    qc.setQueryData(executionKeys.list(PIPELINE_KEY, 1, null, null), { executions: [] });
    seed(qc);

    qc.invalidateQueries({ queryKey: executionKeys.pendingApprovals(PIPELINE_KEY) });

    expect(
      qc.getQueryState(executionKeys.list(PIPELINE_KEY, 1, null, null))?.isInvalidated,
    ).toBe(false);
  });
});

/**
 * Property 6: both refresh paths reach the new key.
 *
 * Driven against a real `QueryClient`, replaying each path's invalidation set as
 * the source issues it:
 *  - the WebSocket flush — `[...executionKeys.all, pipelineId]` (`WebSocketProvider.tsx:556`)
 *  - approve / reject — `approvalKeys.all`, `executionKeys.all`, `pipelineKeys.all`
 *    (`useApprovalAction.ts:73-75`)
 *
 * The third case is the measured correction to the design's P6 wording, and the
 * reason the placement is the mechanism: `invalidateQueries` matches by key prefix
 * from index 0, so `['approvals']` does **not** match `['executions', id, 'approvals']`.
 * The approve/reject path reaches this key through `executionKeys.all` (`:74`), not
 * through `approvalKeys.all` (`:73`). Both paths therefore arrive via the
 * `executions` prefix — which is why no change to `WebSocketProvider` is needed.
 *
 * **Validates: Requirements 3.2**
 */
describe('P6 — both refresh paths reach executionKeys.pendingApprovals', () => {
  it('is marked stale by the WebSocket flush prefix [executions, id]', () => {
    const qc = makeClient();
    seed(qc);

    qc.invalidateQueries({ queryKey: [...executionKeys.all, PIPELINE_KEY] });

    expect(
      qc.getQueryState(executionKeys.pendingApprovals(PIPELINE_KEY))?.isInvalidated,
    ).toBe(true);
  });

  it('is marked stale by the approve/reject invalidation set', () => {
    const qc = makeClient();
    seed(qc);

    // Exactly what `useApprovalAction`'s onSuccess issues, in order.
    qc.invalidateQueries({ queryKey: approvalKeys.all });
    qc.invalidateQueries({ queryKey: executionKeys.all });
    qc.invalidateQueries({ queryKey: pipelineKeys.all });

    expect(
      qc.getQueryState(executionKeys.pendingApprovals(PIPELINE_KEY))?.isInvalidated,
    ).toBe(true);
  });

  it('is reached by executionKeys.all and NOT by approvalKeys.all alone', () => {
    // The negative half, asserted rather than assumed. This is what makes an
    // `approvalKeys` placement reachable by one path and this placement by both.
    const viaApprovals = makeClient();
    seed(viaApprovals);
    viaApprovals.invalidateQueries({ queryKey: approvalKeys.all });
    expect(
      viaApprovals.getQueryState(executionKeys.pendingApprovals(PIPELINE_KEY))
        ?.isInvalidated,
    ).toBe(false);

    const viaExecutions = makeClient();
    seed(viaExecutions);
    viaExecutions.invalidateQueries({ queryKey: executionKeys.all });
    expect(
      viaExecutions.getQueryState(executionKeys.pendingApprovals(PIPELINE_KEY))
        ?.isInvalidated,
    ).toBe(true);
  });

  it('is not collateral damage from another pipeline\u2019s flush', () => {
    const qc = makeClient();
    seed(qc);

    qc.invalidateQueries({ queryKey: [...executionKeys.all, 'pipe-2'] });

    expect(
      qc.getQueryState(executionKeys.pendingApprovals(PIPELINE_KEY))?.isInvalidated,
    ).toBe(false);
  });
});

describe('usePipelinePendingApprovals — request shape', () => {
  it('asks the pipeline-keyed pending endpoint and passes the bare array through', async () => {
    const rows = [
      {
        approval_id: 'appr-1',
        pipeline_key: PIPELINE_KEY,
        run_id: 'run-9',
        approval_type: 'code_generation',
        status: 'pending',
        created_at: '2026-08-25T00:00:00Z',
      },
    ];
    getMock.mockResolvedValueOnce(rows);

    const qc = makeClient();
    const { result } = renderHook(() => usePipelinePendingApprovals(PIPELINE_KEY), {
      wrapper: makeWrapper(qc),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(getMock).toHaveBeenCalledTimes(1);
    expect(getMock.mock.calls[0][0]).toBe(
      `/approvals?pipeline_id=${PIPELINE_KEY}&status=pending`,
    );
    // No envelope, no count field, no reshaping (`LGR-2bcdd0d-1`).
    expect(result.current.data).toEqual(rows);
  });

  it('an empty result is an empty array, never an error or a 404', async () => {
    getMock.mockResolvedValueOnce([]);

    const qc = makeClient();
    const { result } = renderHook(() => usePipelinePendingApprovals(PIPELINE_KEY), {
      wrapper: makeWrapper(qc),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
    expect(result.current.isError).toBe(false);
  });

  it('issues no request until a pipeline key exists', () => {
    const qc = makeClient();
    const { result } = renderHook(() => usePipelinePendingApprovals(undefined), {
      wrapper: makeWrapper(qc),
    });

    expect(getMock).not.toHaveBeenCalled();
    expect(result.current.isPending).toBe(true);
  });

  it('percent-encodes a pipeline key that needs it', async () => {
    getMock.mockResolvedValueOnce([]);
    const qc = makeClient();
    const { result } = renderHook(() => usePipelinePendingApprovals('a/b c'), {
      wrapper: makeWrapper(qc),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock.mock.calls[0][0]).toBe('/approvals?pipeline_id=a%2Fb%20c&status=pending');
  });

  it('does not add a bespoke refetchInterval (LGR-e800696-1)', () => {
    const qc = makeClient();
    renderHook(() => usePipelinePendingApprovals(PIPELINE_KEY), {
      wrapper: makeWrapper(qc),
    });

    const observed = qc
      .getQueryCache()
      .find({ queryKey: executionKeys.pendingApprovals(PIPELINE_KEY) });
    expect(observed?.options.refetchInterval).toBeUndefined();
  });
});
