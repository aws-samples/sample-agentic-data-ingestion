// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Characterization tests for `fetchApprovalsPage` — p50 task 1.1, landing BEFORE C4.
 *
 * Why this file exists (design O3): codegraph reports **no covering tests** for
 * `fetchApprovalsPage` (`use-approvals.ts:36`). It is module-private, so it is
 * reachable only through `useApprovals`, its single caller. The existing
 * `use-approvals.test.tsx` covers the *pagination* contract (R3.1 tokens, and
 * graceful degradation to a plain array); what it does not cover is whether the
 * normalization at `:44-53` preserves the payload — and that is precisely what
 * C4's path depends on.
 *
 * C4 reaches this function: `ApprovalsPage.tsx:39` renders a mapping count off
 * `approval.details`, and every approval it renders arrives through
 * `fetchApprovalsPage`. C1 populates `details` (eight keys that are `null`
 * today) and C3 adds an identity field beside `decided_by`. Both are additive on
 * the wire, so both are silently dropped if this function ever moves from a
 * wholesale cast to field-picking.
 *
 * Expectations are derived from the function's stated contract in its own
 * docstring (`:22-35` — "CONSUME tokens when present and DEGRADE GRACEFULLY to a
 * single page when absent") and from `requirements.md` R1/R2, not from observed
 * output. Nothing here asserts that `details` is empty or that no email is
 * present, which is today's state and the defect.
 *
 * Requirements: R1.1, R2.2 — Design: O3
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

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

/**
 * The shape C1 and C3 produce, measured against the reported record. Today
 * `details` carries eight of nine keys as `null` and no email exists at all;
 * this is the payload AFTER the spec, used here to prove the client relays it
 * rather than to assert the backend already sends it.
 */
const POPULATED = {
  approval_id: 'ap-1',
  pipeline_key: 'test-pipeline',
  approval_type: 'schema_mapping',
  status: 'approved',
  created_at: '2026-08-24T13:48:26Z',
  decided_at: '2026-08-24T14:02:11Z',
  decided_by: '583123b0-60b1-7056-51a3-bd7ba804c325',
  decided_by_email: 'approver@example.com',
  details: {
    mapping_count: 1,
    proposed_mappings: [
      { source: 'signup_date', target: 'registration_date', confidence: 0.96 },
    ],
    confidence_scores: { signup_date: 0.96 },
    generated_by: 'us.anthropic.claude-sonnet-4-6',
  },
};

beforeEach(() => {
  getMock.mockReset();
});

describe('fetchApprovalsPage payload passthrough (design O3)', () => {
  it('relays every approval attribute unchanged from an envelope body', async () => {
    // C1 populates `details` and C3 adds the identity field. Both are additive on
    // the wire; a field-picking normalization would drop them without erroring,
    // which is the failure mode this pins against.
    getMock.mockResolvedValueOnce({ approvals: [POPULATED], has_more: false });

    const { result } = renderHook(() => useApprovals(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.pages[0].approvals[0]).toEqual(POPULATED);
  });

  it('relays every approval attribute unchanged from a plain-array body', async () => {
    // The endpoint's shape today (`handlers/approvals.py::get_approvals` returns a
    // plain `Approval[]`), so this is the arm that is live in production.
    getMock.mockResolvedValueOnce([POPULATED]);

    const { result } = renderHook(() => useApprovals(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.pages[0].approvals[0]).toEqual(POPULATED);
  });

  it('preserves a nested details payload by value, not by shallow copy', async () => {
    // `ApprovalsPage.tsx:39` and `ApprovalWorkflowSection.tsx:161` both read
    // through `details` into nested structures. R1.1's proposed mappings are a
    // list of objects, so a shallow normalization would be indistinguishable from
    // a correct one on a flat fixture.
    getMock.mockResolvedValueOnce({ approvals: [POPULATED], has_more: false });

    const { result } = renderHook(() => useApprovals(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const details = result.current.data?.pages[0].approvals[0].details as Record<string, unknown>;
    expect(details.proposed_mappings).toEqual([
      { source: 'signup_date', target: 'registration_date', confidence: 0.96 },
    ]);
    expect(details.confidence_scores).toEqual({ signup_date: 0.96 });
  });

  it('yields an empty page rather than throwing when the envelope carries no approvals array', async () => {
    // R1.5's spirit on the client: a shape that resolves to nothing must be
    // distinguishable from an error, and must not take the page down. Derived from
    // the `Array.isArray(body?.approvals) ? ... : []` contract at `:48`.
    getMock.mockResolvedValueOnce({ has_more: false });

    const { result } = renderHook(() => useApprovals(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.pages[0].approvals).toEqual([]);
    expect(result.current.data?.pages[0].hasMore).toBe(false);
  });

  it('infers a further page from a bare next_token when has_more is absent', async () => {
    // The documented degradation at `:50-51`: `has_more` governs when it is a
    // boolean, otherwise the presence of a token does. Pinned because C4 adds no
    // pagination change, so any drift here would be incidental.
    getMock.mockResolvedValueOnce({ approvals: [POPULATED], next_token: 'TOKEN_2' });

    const { result } = renderHook(() => useApprovals(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.pages[0].hasMore).toBe(true);
    expect(result.current.data?.pages[0].nextToken).toBe('TOKEN_2');
    expect(result.current.hasNextPage).toBe(true);
  });

  it('requests pending approvals with the page-size hint', async () => {
    getMock.mockResolvedValueOnce([]);

    const { result } = renderHook(() => useApprovals(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(getMock.mock.calls[0][0]).toContain('status=pending');
    expect(getMock.mock.calls[0][0]).toContain('limit=50');
  });
});
