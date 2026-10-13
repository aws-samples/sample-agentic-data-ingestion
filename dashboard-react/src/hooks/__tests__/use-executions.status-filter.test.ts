// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `?status=` on the executions request, and the query key that carries it —
 * p48 task 5.5 (C5, R4.1, R4.3).
 *
 * WHY THIS FILE IS SEPARATE FROM THE PAGE TEST
 * --------------------------------------------
 * Task 1.2 established that every `ExecutionsPage.*.test.tsx` mocks
 * `../../hooks` wholesale, so **no page test executes the real request**. A page
 * test can prove which `(page, cursor, status)` triple the page asks for; only a
 * test against the real hook can prove that a selected status becomes
 * `?status=SKIPPED` on the wire and that an unfiltered read sends no `status`
 * parameter at all. That second half is what this file is for.
 *
 * WHAT IT MUST NOT BECOME
 * -----------------------
 * `fetchPipelineExecutions` is a **pass-through cast** and task 1.2's
 * `returns the producer body unchanged, adding and renaming nothing` is what holds
 * that. Nothing here asserts a reshape, and nothing here filters client-side: the
 * server filters (task 4.3, after the `WAITING_APPROVAL` derivation), and a second
 * filtering authority in the client would see one page where the server walked
 * many — R4.3's misleading-scope defect, arrived at from the other side.
 *
 * `computeExecutionsRefetchInterval`, `EXECUTIONS_POLL_MATRIX` and
 * `usePipelineExecutions`' `refetchInterval` callback are `LGR-e800696-1`'s
 * (**template / ratified**) and are untouched. Rendering without a
 * `WebSocketProvider` yields `connected: false`, so the callback returns a
 * positive cadence — 8 s or 30 s, neither of which elapses inside these tests.
 *
 * _Requirements: R4.1, R4.3 — Design: C5_
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import type { ReactNode } from 'react';

const getMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: {
    get: getMock,
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import { usePipelineExecutions } from '../use-executions';
import { executionKeys } from '../query-keys';

const PIPELINE_KEY = 'latesttesting-19ca826a';

/** Minimal producer-shaped envelope; this file asserts requests, not bodies. */
const BODY = {
  executions: [
    {
      execution_id: 'skipped-7412636fccb938719673179658c05773',
      run_id: 'skipped-7412636fccb938719673179658c05773',
      status: 'SKIPPED',
      started_at: '2026-08-24T13:04:04+00:00',
      file_name: 'customer_data_3.csv',
    },
  ],
  queued_files: [],
  pagination: {
    page: 1,
    page_size: 10,
    total_count: 1,
    total_pages: 1,
    has_next: false,
    has_prev: false,
    next_cursor: null,
    partial_counts: [],
  },
};

/**
 * Local wrapper, matching every other hook test in this directory — there is no
 * shared React Query harness in this repo to reuse.
 */
function createWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

async function requestFor(
  page?: number,
  cursor?: string | null,
  status?: string | null,
): Promise<URL> {
  const { result } = renderHook(() => usePipelineExecutions(PIPELINE_KEY, page, cursor, status), {
    wrapper: createWrapper(),
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  return new URL(String(getMock.mock.lastCall?.[0]), 'https://example.invalid');
}

beforeEach(() => {
  getMock.mockReset();
  getMock.mockImplementation(() => Promise.resolve(JSON.parse(JSON.stringify(BODY))));
});

describe('Feature: p48-run-history-order-and-status-filter, task 5.5: ?status= on the request', () => {
  /**
   * **Validates: Requirements 4.1, 4.4**
   *
   * The wire half of "selecting SKIPPED sends `?status=SKIPPED`". `SKIPPED` because
   * R4.4 makes it first-class — it is the case that opened this spec.
   */
  it('puts the selected status on the query string', async () => {
    const url = await requestFor(1, undefined, 'SKIPPED');
    expect(url.searchParams.get('status')).toBe('SKIPPED');
  });

  /**
   * **Validates: Requirements 4.1**
   *
   * An unfiltered read omits the parameter **entirely** rather than sending
   * `?status=`. `router.py:191` reads `query_params.get("status") or None`, so the
   * two are equivalent to the server today — but the handler answers an
   * unrecognised value with a 400, and not sending the parameter keeps "unfiltered"
   * structurally incapable of reaching that path. Same treatment and same reason as
   * `cursor` on page 1.
   */
  it('omits status entirely when unfiltered', async () => {
    const url = await requestFor(1, undefined, undefined);
    expect(url.searchParams.has('status')).toBe(false);
    expect(url.search).not.toContain('status=');
  });

  /**
   * **Validates: Requirements 4.1**
   *
   * The empty string is treated as unfiltered rather than sent. `ExecutionsPage`
   * spells "no filter" as `''` in its own state, so this is the boundary between
   * that spelling and the wire.
   */
  it('treats an empty status as unfiltered', async () => {
    const url = await requestFor(1, undefined, '');
    expect(url.searchParams.has('status')).toBe(false);
  });

  /**
   * **Validates: Requirements 4.1**
   *
   * `status` rides alongside `cursor` rather than replacing it — a filtered page 2
   * carries both. Worth pinning because the two arrive at the same function and a
   * mistake that dropped one while setting the other would still produce a valid
   * request.
   */
  it('sends cursor and status together on a filtered later page', async () => {
    const url = await requestFor(2, 'opaque-cursor', 'WAITING_APPROVAL');
    expect(url.searchParams.get('cursor')).toBe('opaque-cursor');
    expect(url.searchParams.get('status')).toBe('WAITING_APPROVAL');
    expect(url.searchParams.get('page')).toBe('2');
  });
});

describe('Feature: p48-run-history-order-and-status-filter, task 5.5: the query key', () => {
  /**
   * **Validates: Requirements 4.1**
   *
   * `LGR-8b6e6a1-1` (**template / ratified**): the WebSocket flush invalidates the
   * page-key **prefix** `['executions', id]`, so any new key member must sit
   * *after* the id or the flush stops matching and a live run stops refreshing.
   * `status` is appended last, which keeps that prefix intact — asserted directly
   * on the prefix rather than on the whole key, because the prefix is the fact the
   * record protects.
   */
  it('keeps the flush prefix intact with status appended last', () => {
    const filtered = executionKeys.list(PIPELINE_KEY, 1, null, 'SKIPPED');
    expect(filtered.slice(0, 2)).toEqual(['executions', PIPELINE_KEY]);
    expect(filtered[filtered.length - 1]).toBe('SKIPPED');
  });

  /**
   * **Validates: Requirements 4.1**
   *
   * A filtered read and an unfiltered read of the same page position are different
   * responses, so they must not share a cache entry — one would be served as the
   * other. `null` is spelled explicitly for the unfiltered case, for the same
   * reason `cursor` is: React Query hashes the key and a trailing `undefined` is
   * not stable across the two spellings a caller might use.
   */
  it('distinguishes filtered from unfiltered at the same page position', () => {
    const unfiltered = executionKeys.list(PIPELINE_KEY, 1, null);
    const filtered = executionKeys.list(PIPELINE_KEY, 1, null, 'SKIPPED');
    expect(unfiltered).not.toEqual(filtered);
    expect(unfiltered).toEqual(executionKeys.list(PIPELINE_KEY, 1, null, undefined));
    expect(unfiltered[unfiltered.length - 1]).toBeNull();
  });
});
