// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for useApprovals — server-driven, token-based pagination (P29 R3.1).
 *
 * Covers:
 *  - R3.1: pagination past the first page — the SECOND page is fetched using the
 *    `next_token` returned by the first page.
 *  - Graceful degradation: when the approvals endpoint returns a plain
 *    `Approval[]` (today's backend, no tokens — see handlers/approvals.py), the
 *    hook yields a single page and reports no further pages.
 *
 * Requirements: 3.1
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

// Spy on the API client's GET — the hook's only side effect.
const getMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: { get: getMock },
  ApiError: class ApiError extends Error {},
}));

import { useApprovals } from '../use-approvals';

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

function approval(id: string, pipeline = 'p1') {
  return {
    approval_id: id,
    pipeline_key: pipeline,
    approval_type: 'code_generation',
    status: 'pending',
    created_at: '2024-01-01T00:00:00Z',
  };
}

beforeEach(() => {
  getMock.mockReset();
});

describe('useApprovals — token-based server pagination (R3.1)', () => {
  it('fetches the first page without a token, then the second page WITH the next_token', async () => {
    getMock
      .mockResolvedValueOnce({ approvals: [approval('a1')], next_token: 'TOKEN_2', has_more: true })
      .mockResolvedValueOnce({ approvals: [approval('a2', 'p2')], has_more: false });

    const { result } = renderHook(() => useApprovals(), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Page 1: requested with status=pending and NO token.
    expect(getMock).toHaveBeenCalledTimes(1);
    expect(getMock.mock.calls[0][0]).toContain('status=pending');
    expect(getMock.mock.calls[0][0]).not.toContain('next_token');
    expect(result.current.hasNextPage).toBe(true);

    // Turning the page pulls page 2 using the token from page 1.
    await act(async () => {
      await result.current.fetchNextPage();
    });
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2));

    expect(getMock.mock.calls[1][0]).toContain('next_token=TOKEN_2');
    expect(result.current.data?.pages).toHaveLength(2);
    expect(result.current.data?.pages[1].approvals[0].approval_id).toBe('a2');
    // Page 2 said has_more:false → no further pages.
    expect(result.current.hasNextPage).toBe(false);
  });

  it('degrades gracefully when the endpoint returns a plain array (no tokens)', async () => {
    // Today's backend shape: a bare Approval[] with no pagination envelope.
    getMock.mockResolvedValueOnce([approval('a1'), approval('a2', 'p2')]);

    const { result } = renderHook(() => useApprovals(), { wrapper: makeWrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.pages).toHaveLength(1);
    expect(result.current.data?.pages[0].approvals).toHaveLength(2);
    // No token / has_more → the hook reports a single, final page.
    expect(result.current.hasNextPage).toBe(false);
  });
});
