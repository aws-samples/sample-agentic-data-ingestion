// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

// Mock apiClient
vi.mock('../services/api-client', () => ({
  apiClient: {
    get: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue(undefined),
  },
}));

import { apiClient } from '../services/api-client';
import { useDataPreview } from '../hooks/use-data-preview';
import { useNotifications, useMarkNotificationRead } from '../hooks/use-notifications';
import { useUpdates } from '../hooks/use-updates';

const mockedGet = vi.mocked(apiClient.get);
const mockedPut = vi.mocked(apiClient.put);

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedGet.mockResolvedValue({});
  mockedPut.mockResolvedValue(undefined);
});

// ─── useDataPreview ──────────────────────────────────────────────

describe('useDataPreview', () => {
  it('is disabled when pipelineId is undefined', () => {
    const { result } = renderHook(() => useDataPreview(undefined), {
      wrapper: createWrapper(),
    });
    // Query should not fetch — stays in idle/pending state
    expect(result.current.isFetching).toBe(false);
    expect(mockedGet).not.toHaveBeenCalled();
  });

  it('is disabled when pipelineId is empty string', () => {
    const { result } = renderHook(() => useDataPreview(''), {
      wrapper: createWrapper(),
    });
    expect(result.current.isFetching).toBe(false);
    expect(mockedGet).not.toHaveBeenCalled();
  });

  it('calls latestRun endpoint when runId is not provided', async () => {
    mockedGet.mockResolvedValue({
      columns: ['a'],
      rows: [],
      total_rows_returned: 0,
      source_file: 'test.csv',
    });

    const { result } = renderHook(() => useDataPreview('pipe-1'), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith(
      '/pipelines/pipe-1/preview',
    );
  });

  it('calls specificRun endpoint when runId is provided', async () => {
    mockedGet.mockResolvedValue({
      columns: ['a'],
      rows: [],
      total_rows_returned: 0,
      source_file: 'test.csv',
    });

    const { result } = renderHook(() => useDataPreview('pipe-1', 'run-1'), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith(
      '/pipelines/pipe-1/run/run-1/preview',
    );
  });

  it('appends limit query param when limit is provided', async () => {
    mockedGet.mockResolvedValue({
      columns: [],
      rows: [],
      total_rows_returned: 0,
      source_file: '',
    });

    const { result } = renderHook(() => useDataPreview('pipe-1', undefined, 50), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith(
      '/pipelines/pipe-1/preview?limit=50',
    );
  });

  it('encodes special characters in pipelineId', async () => {
    mockedGet.mockResolvedValue({
      columns: [],
      rows: [],
      total_rows_returned: 0,
      source_file: '',
    });

    const { result } = renderHook(() => useDataPreview('pipe/1&x=y'), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith(
      `/pipelines/${encodeURIComponent('pipe/1&x=y')}/preview`,
    );
  });

  it('has staleTime of 60 seconds', async () => {
    mockedGet.mockResolvedValue({
      columns: [],
      rows: [],
      total_rows_returned: 0,
      source_file: '',
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useDataPreview('pipe-1'), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const queries = queryClient.getQueryCache().getAll();
    const previewQuery = queries.find((q) => q.queryKey[0] === 'preview');
    const opts = previewQuery?.options as Record<string, unknown>;
    expect(opts?.staleTime).toBe(60_000);
  });
});

// ─── useNotifications ────────────────────────────────────────────

describe('useNotifications', () => {
  it('calls notifications list endpoint', async () => {
    mockedGet.mockResolvedValue({ notifications: [], count: 0 });

    const { result } = renderHook(() => useNotifications(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith('/notifications');
  });

  it('passes limit and unread_only query params', async () => {
    mockedGet.mockResolvedValue({ notifications: [], count: 0 });

    const { result } = renderHook(() => useNotifications(10, true), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const calledUrl = mockedGet.mock.calls[0][0];
    expect(calledUrl).toContain('limit=10');
    expect(calledUrl).toContain('unread_only=true');
  });

  it('has refetchInterval of 30 seconds', async () => {
    mockedGet.mockResolvedValue({ notifications: [], count: 0 });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useNotifications(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const queries = queryClient.getQueryCache().getAll();
    const notifQuery = queries.find((q) => q.queryKey[0] === 'notifications');
    const opts = notifQuery?.options as Record<string, unknown>;
    expect(opts?.refetchInterval).toBe(30_000);
  });
});

// ─── useMarkNotificationRead ─────────────────────────────────────

describe('useMarkNotificationRead', () => {
  it('calls PUT on the mark-read endpoint', async () => {
    mockedPut.mockResolvedValue(undefined);

    const { result } = renderHook(() => useMarkNotificationRead(), {
      wrapper: createWrapper(),
    });

    result.current.mutate('notif-123');

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedPut).toHaveBeenCalledWith(
      `/notifications/${encodeURIComponent('notif-123')}/read`,
      {},
    );
  });
});

// ─── useUpdates ──────────────────────────────────────────────────

describe('useUpdates', () => {
  it('calls updates list endpoint', async () => {
    mockedGet.mockResolvedValue({ updates: [], count: 0 });

    const { result } = renderHook(() => useUpdates(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith('/updates');
  });

  it('appends limit query param', async () => {
    mockedGet.mockResolvedValue({ updates: [], count: 0 });

    const { result } = renderHook(() => useUpdates(5), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith('/updates?limit=5');
  });

  it('has refetchInterval of 30s and staleTime of 15s', async () => {
    mockedGet.mockResolvedValue({ updates: [], count: 0 });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useUpdates(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const queries = queryClient.getQueryCache().getAll();
    const updatesQuery = queries.find((q) => q.queryKey[0] === 'updates');
    const opts = updatesQuery?.options as Record<string, unknown>;
    expect(opts?.refetchInterval).toBe(30_000);
    expect(opts?.staleTime).toBe(15_000);
  });
});
