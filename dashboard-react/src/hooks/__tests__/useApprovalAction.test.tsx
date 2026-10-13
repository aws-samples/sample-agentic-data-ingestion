// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for useApprovalAction — THE single Approval_Contract hook (R6).
 *
 * Covers:
 *  - R6.2: the payload carries Identity_From_Token (real Cognito email/sub),
 *    never the hardcoded 'dashboard-user'.
 *  - R6.1: one contract module posts `{ decision, comment, user, question_answers? }`
 *    to `/approvals/{id}/{action}`.
 *  - R6.3/R6.4: success invalidates the approvals list (and the acting run's
 *    detail) — event-driven refresh, no setTimeout refetch chain.
 *
 * Requirements: 6.1, 6.2, 6.3, 6.4
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

// Mutable auth mock — the provider identity varies per test.
const authMock = vi.hoisted(() => ({
  user: { profile: { email: 'jane@example.com', sub: 'sub-123' } } as
    | { profile: { email?: string; sub?: string } }
    | null,
}));
vi.mock('react-oidc-context', () => ({
  useAuth: () => ({ user: authMock.user }),
}));

// Spy on the API client's POST — the hook's only side effect.
const postMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: { post: postMock },
  ApiError: class ApiError extends Error {},
}));

import { useApprovalAction } from '../useApprovalAction';
import { approvalKeys, runKeys } from '../query-keys';

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
  return { queryClient, wrapper };
}

beforeEach(() => {
  postMock.mockReset();
  postMock.mockResolvedValue({ success: true });
  authMock.user = { profile: { email: 'jane@example.com', sub: 'sub-123' } };
});

describe('useApprovalAction — Approval_Contract', () => {
  it('sends the real Cognito email as `user` (Identity_From_Token, not dashboard-user)', async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useApprovalAction(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ id: 'appr-1', action: 'approve' });
    });

    expect(postMock).toHaveBeenCalledTimes(1);
    const [url, body] = postMock.mock.calls[0];
    expect(url).toBe('/approvals/appr-1/approve');
    expect(body).toMatchObject({ decision: 'approved', user: 'jane@example.com', comment: '' });
    expect(body.user).not.toBe('dashboard-user');
  });

  it('falls back to the token `sub` when no email is present', async () => {
    authMock.user = { profile: { sub: 'sub-123' } };
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useApprovalAction(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ id: 'appr-1', action: 'reject', comment: 'nope' });
    });

    const [url, body] = postMock.mock.calls[0];
    expect(url).toBe('/approvals/appr-1/reject');
    expect(body).toMatchObject({ decision: 'rejected', user: 'sub-123', comment: 'nope' });
  });

  it('includes question_answers only when provided', async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useApprovalAction(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({
        id: 'appr-1',
        action: 'approve',
        questionAnswers: { status_code: 'order_status' },
      });
    });
    expect(postMock.mock.calls[0][1]).toMatchObject({
      question_answers: { status_code: 'order_status' },
    });

    postMock.mockClear();
    await act(async () => {
      await result.current.mutateAsync({ id: 'appr-2', action: 'approve' });
    });
    expect(postMock.mock.calls[0][1]).not.toHaveProperty('question_answers');
  });

  it('invalidates the approvals list and the acting run on success (event-driven refresh)', async () => {
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    const { result } = renderHook(() => useApprovalAction(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ id: 'appr-1', action: 'approve', runId: 'run-1' });
    });

    const invalidatedKeys = invalidateSpy.mock.calls.map((c) =>
      JSON.stringify((c[0] as { queryKey?: unknown })?.queryKey),
    );
    expect(invalidatedKeys).toContain(JSON.stringify(approvalKeys.all));
    expect(invalidatedKeys).toContain(JSON.stringify(runKeys.detail('run-1')));
  });

  it('does not invalidate a run when no runId is supplied', async () => {
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');
    const { result } = renderHook(() => useApprovalAction(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ id: 'appr-1', action: 'approve' });
    });

    const invalidatedKeys = invalidateSpy.mock.calls.map((c) =>
      JSON.stringify((c[0] as { queryKey?: unknown })?.queryKey),
    );
    expect(invalidatedKeys).toContain(JSON.stringify(approvalKeys.all));
    // No run-detail key present since runId was omitted.
    expect(invalidatedKeys.some((k) => k.includes('"runs"') && k !== JSON.stringify(runKeys.all))).toBe(false);
  });
});
