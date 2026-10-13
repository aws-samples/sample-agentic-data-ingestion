// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

// Mock apiClient
vi.mock('../services/api-client', () => ({
  apiClient: {
    get: vi.fn().mockResolvedValue([]),
  },
}));

import { apiClient } from '../services/api-client';
import { useRdsInstances, useRdsClusters, useRdsGlueConnections, useRdsSecrets } from '../hooks/use-resources';

const mockedGet = vi.mocked(apiClient.get);

function createWrapper() {
  const queryClient = new QueryClient({
    // P29 R1.2 — the hooks now set `retry: 2`, which overrides the client
    // default. Keep `retryDelay: 0` so the two retries resolve instantly and
    // the error-state assertions below don't hit waitFor's timeout.
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedGet.mockResolvedValue([]);
});

// ─── useRdsInstances ─────────────────────────────────────────────

describe('useRdsInstances', () => {
  // P29 R1.2 — fetchers no longer swallow errors into `[]`; the error must
  // propagate to React Query so consumers can surface an honest error state.
  it('propagates the error instead of swallowing it into []', async () => {
    mockedGet.mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() => useRdsInstances(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });

  it('calls the correct endpoint', async () => {
    mockedGet.mockResolvedValue([]);

    const { result } = renderHook(() => useRdsInstances(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith('/resources/rds/instances');
  });

  it('has staleTime of 60 seconds and retry 2 (P29 R1.2)', async () => {
    mockedGet.mockResolvedValue([]);

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useRdsInstances(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const queries = queryClient.getQueryCache().getAll();
    const q = queries.find((q) => JSON.stringify(q.queryKey).includes('instances'));
    const opts = q?.options as Record<string, unknown>;
    expect(opts?.staleTime).toBe(60_000);
    expect(opts?.retry).toBe(2);
  });
});

// ─── useRdsClusters ──────────────────────────────────────────────

describe('useRdsClusters', () => {
  it('propagates the error instead of swallowing it into []', async () => {
    mockedGet.mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() => useRdsClusters(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });

  it('calls the correct endpoint', async () => {
    mockedGet.mockResolvedValue([]);

    const { result } = renderHook(() => useRdsClusters(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith('/resources/rds/clusters');
  });
});

// ─── useRdsGlueConnections ───────────────────────────────────────

describe('useRdsGlueConnections', () => {
  it('propagates the error instead of swallowing it into []', async () => {
    mockedGet.mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() => useRdsGlueConnections(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });

  it('calls the correct endpoint', async () => {
    mockedGet.mockResolvedValue([]);

    const { result } = renderHook(() => useRdsGlueConnections(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith('/resources/rds/glue-connections');
  });
});

// ─── useRdsSecrets ───────────────────────────────────────────────

describe('useRdsSecrets', () => {
  it('propagates the error instead of swallowing it into []', async () => {
    mockedGet.mockRejectedValue(new Error('Network error'));

    const { result } = renderHook(() => useRdsSecrets(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });

  it('calls the correct endpoint', async () => {
    mockedGet.mockResolvedValue([]);

    const { result } = renderHook(() => useRdsSecrets(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith('/resources/rds/secrets');
  });
});
