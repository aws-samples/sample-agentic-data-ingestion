// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property test: useSubnets / useSecurityGroups skip the network fetch
 * when vpcId is falsy.
 *
 * Validates: P20 Requirements 10.2, 10.3
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as fc from 'fast-check';
import type { ReactNode } from 'react';

// Mock the API client so we can count fetch invocations.
// Vitest hoists vi.mock; use vi.hoisted to share state with the factory.
const mocks = vi.hoisted(() => ({
  apiClientGetMock: vi.fn(async () => ({
    subnets: [],
    security_groups: [],
    count: 0,
  })),
}));

vi.mock('../../services/api-client', () => ({
  apiClient: { get: mocks.apiClientGetMock },
}));

import { useSubnets, useSecurityGroups } from '../use-resources';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

// Feature: p20-jdbc-manual-mode, Property 17: useSubnets / useSecurityGroups skip the network fetch when vpcId is falsy
describe('Property 17 — fetch skip when vpcId falsy', () => {
  beforeEach(() => {
    mocks.apiClientGetMock.mockClear();
  });

  it('useSubnets does NOT fetch when vpcId is undefined / null / empty', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(undefined, null, ''),
        (vpcId) => {
          mocks.apiClientGetMock.mockClear();
          renderHook(() => useSubnets(vpcId), { wrapper: createWrapper() });
          // Falsy vpcId → React Query's `enabled: !!vpcId` returns false
          // → no fetch.
          expect(mocks.apiClientGetMock).not.toHaveBeenCalled();
        },
      ),
      { numRuns: 10 },
    );
  });

  it('useSecurityGroups does NOT fetch when vpcId is undefined / null / empty', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(undefined, null, ''),
        (vpcId) => {
          mocks.apiClientGetMock.mockClear();
          renderHook(() => useSecurityGroups(vpcId), {
            wrapper: createWrapper(),
          });
          expect(mocks.apiClientGetMock).not.toHaveBeenCalled();
        },
      ),
      { numRuns: 10 },
    );
  });

  it('useSubnets DOES fetch when vpcId is a non-empty string', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.stringMatching(/^vpc-[a-f0-9]{8,16}$/),
        async (vpcId) => {
          mocks.apiClientGetMock.mockClear();
          renderHook(() => useSubnets(vpcId), { wrapper: createWrapper() });
          // React Query schedules the fetch via a microtask; flush it
          // and one tick of the event loop so the queryFn runs.
          await new Promise((resolve) => setTimeout(resolve, 0));
          expect(mocks.apiClientGetMock).toHaveBeenCalled();
        },
      ),
      { numRuns: 5 },
    );
  });
});
