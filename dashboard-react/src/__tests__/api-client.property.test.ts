// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as fc from 'fast-check';
import { ApiClient } from '../services/api-client';

/**
 * Feature: agentic-data-ingestion-codebase-optimization
 * Property 8: API request deduplication
 * Validates: Requirements 13.1
 *
 * For any URL and HTTP method, when N concurrent requests (N >= 2) are issued
 * before the first completes, exactly 1 fetch call is made and all N callers
 * receive the same response.
 */

describe('Property 8: API request deduplication', () => {
  let fetchCallCount: number;
  let resolveInflight: ((v: Response) => void) | null;

  beforeEach(() => {
    fetchCallCount = 0;
    resolveInflight = null;

    vi.stubGlobal(
      'fetch',
      vi.fn(() => {
        fetchCallCount++;
        return new Promise<Response>((resolve) => {
          resolveInflight = resolve;
        });
      }),
    );
  });

  it('concurrent identical requests produce exactly one fetch call', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.stringMatching(/^\/[a-z]{1,10}$/),
        fc.integer({ min: 2, max: 8 }),
        async (path, concurrency) => {
          // Reset per iteration
          fetchCallCount = 0;
          resolveInflight = null;

          const client = new ApiClient('');

          // Fire N concurrent requestRaw requests (dedup operates at raw level)
          const promises = Array.from({ length: concurrency }, () =>
            client.requestRaw(path),
          );

          // Exactly one fetch should have been called
          expect(fetchCallCount).toBe(1);

          // Resolve the single in-flight request
          const mockResponse = new Response('ok', { status: 200 });
          resolveInflight!(mockResponse);

          // All callers get the same response object
          const results = await Promise.all(promises);
          for (const r of results) {
            expect(r).toBe(mockResponse);
          }

          // Cache is cleaned up
          expect(client.inflightSize).toBe(0);
        },
      ),
      { numRuns: 100 },
    );
  });

  it('failed requests propagate error to all callers and clean cache', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.stringMatching(/^\/[a-z]{1,10}$/),
        fc.integer({ min: 2, max: 5 }),
        async (path, concurrency) => {
          let rejectInflight: ((e: Error) => void) | null = null;
          let localCallCount = 0;

          vi.stubGlobal(
            'fetch',
            vi.fn(() => {
              localCallCount++;
              return new Promise<Response>((_resolve, reject) => {
                rejectInflight = reject;
              });
            }),
          );

          const client = new ApiClient('');
          const promises = Array.from({ length: concurrency }, () =>
            client.requestRaw(path).catch((e: Error) => e),
          );

          expect(localCallCount).toBe(1);

          const error = new Error('network failure');
          rejectInflight!(error);

          const results = await Promise.all(promises);
          for (const r of results) {
            expect(r).toBe(error);
          }

          expect(client.inflightSize).toBe(0);
        },
      ),
      { numRuns: 100 },
    );
  });
});
