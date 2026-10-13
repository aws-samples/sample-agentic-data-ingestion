// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

vi.mock('../services/api-client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import { apiClient } from '../services/api-client';
import {
  useJdbcDetectSchemaStart,
  useJdbcDetectSchemaStatus,
  type JdbcDetectSchemaStartResponse,
  type JdbcDetectSchemaStatusResponse,
} from '../hooks/use-resources';

const mockedGet = vi.mocked(apiClient.get);
const mockedPost = vi.mocked(apiClient.post);

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { Wrapper, queryClient };
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ─── useJdbcDetectSchemaStart ───────────────────────────────────

describe('useJdbcDetectSchemaStart', () => {
  it('POSTs the source_config + mode body to /resources/jdbc/detect-schema and returns the typed response', async () => {
    const fakeResponse: JdbcDetectSchemaStartResponse = {
      crawler_name: 'agentic-data-ingestion-discover-abc123def456',
      glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
      table_prefix: 'agentic_data_ingestion_abc123def456_',
      status: 'RUNNING',
    };
    mockedPost.mockResolvedValue(fakeResponse);

    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useJdbcDetectSchemaStart(), { wrapper: Wrapper });

    const payload = {
      source_config: {
        jdbc_url: 'jdbc:postgresql://example:5432/postgres',
        jdbc_driver: 'org.postgresql.Driver',
        secret_arn: 'arn:aws:secretsmanager:us-west-2:123:secret:s-1',
        jdbc_schema: 'public',
        jdbc_table: 'users',
      },
      mode: 'select_rds' as const,
    };

    let returned: JdbcDetectSchemaStartResponse | undefined;
    await act(async () => {
      returned = await result.current.mutateAsync(payload);
    });

    expect(mockedPost).toHaveBeenCalledTimes(1);
    expect(mockedPost).toHaveBeenCalledWith('/resources/jdbc/detect-schema', payload);
    expect(returned).toEqual(fakeResponse);
    await waitFor(() => expect(result.current.data).toEqual(fakeResponse));
  });

  it('supports the glue_connection mode', async () => {
    const fakeResponse: JdbcDetectSchemaStartResponse = {
      crawler_name: 'agentic-data-ingestion-discover-deadbeef',
      glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
      table_prefix: 'agentic_data_ingestion_deadbeef_',
      status: 'RUNNING',
    };
    mockedPost.mockResolvedValue(fakeResponse);

    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useJdbcDetectSchemaStart(), { wrapper: Wrapper });

    const payload = {
      source_config: {
        glue_connection_name: 'my-existing-conn',
        jdbc_schema: 'dbo',
        jdbc_table: 'orders',
      },
      mode: 'glue_connection' as const,
    };

    await act(async () => {
      await result.current.mutateAsync(payload);
    });

    expect(mockedPost).toHaveBeenCalledWith('/resources/jdbc/detect-schema', payload);
  });

  it('surfaces API failures via mutateAsync rejection', async () => {
    mockedPost.mockRejectedValue(new Error('S3 endpoint missing'));

    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useJdbcDetectSchemaStart(), { wrapper: Wrapper });

    await expect(
      act(async () => {
        await result.current.mutateAsync({
          source_config: {},
          mode: 'select_rds',
        });
      }),
    ).rejects.toThrow('S3 endpoint missing');
  });
});

// ─── useJdbcDetectSchemaStatus ──────────────────────────────────

describe('useJdbcDetectSchemaStatus', () => {
  it('is disabled and idle when crawlerName is null', async () => {
    const { Wrapper } = createWrapper();
    const { result } = renderHook(() => useJdbcDetectSchemaStatus(null), { wrapper: Wrapper });

    // Allow any pending microtasks to flush
    await act(async () => { await Promise.resolve(); });
    expect(mockedGet).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe('idle');
  });

  it('calls GET /resources/jdbc/detect-schema/{crawler_name} on mount', async () => {
    const ready: JdbcDetectSchemaStatusResponse = {
      status: 'READY',
      glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
      glue_table: 'agentic_data_ingestion_abc_users',
      schema: [
        { name: 'id', type: 'bigint', nullable: false },
        { name: 'name', type: 'string', nullable: true },
      ],
    };
    mockedGet.mockResolvedValue(ready);

    const { Wrapper } = createWrapper();
    const { result } = renderHook(
      () => useJdbcDetectSchemaStatus('agentic-data-ingestion-discover-abc'),
      { wrapper: Wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockedGet).toHaveBeenCalledWith(
      '/resources/jdbc/detect-schema/agentic-data-ingestion-discover-abc',
    );
    expect(result.current.data).toEqual(ready);
  });

  // The polling tests use fake timers; we drive React Query's internal interval
  // by calling `vi.advanceTimersByTimeAsync(3_000)` and then waiting on the
  // mock-call count rather than using `waitFor` (which itself uses timers).
  describe('polling behavior', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    async function flushMicrotasks() {
      // Three ticks: queryFn resolution, React Query state transition, refetchInterval scheduling.
      for (let i = 0; i < 3; i++) {
        await act(async () => { await Promise.resolve(); });
      }
    }

    it('polls every 3 seconds while status is RUNNING', async () => {
      const running: JdbcDetectSchemaStatusResponse = {
        status: 'RUNNING',
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
        table_prefix: 'agentic_data_ingestion_abc_',
        crawler_name: 'agentic-data-ingestion-discover-abc',
      };
      mockedGet.mockResolvedValue(running);

      const { Wrapper } = createWrapper();
      renderHook(
        () => useJdbcDetectSchemaStatus('agentic-data-ingestion-discover-abc'),
        { wrapper: Wrapper },
      );

      // Initial fetch on mount.
      await flushMicrotasks();
      expect(mockedGet).toHaveBeenCalledTimes(1);

      // Advance one polling interval: second fetch.
      await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
      await flushMicrotasks();
      expect(mockedGet).toHaveBeenCalledTimes(2);

      // And another: third fetch.
      await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
      await flushMicrotasks();
      expect(mockedGet).toHaveBeenCalledTimes(3);
    });

    it('stops polling once status flips to READY', async () => {
      const running: JdbcDetectSchemaStatusResponse = {
        status: 'RUNNING',
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
        table_prefix: 'agentic_data_ingestion_abc_',
        crawler_name: 'agentic-data-ingestion-discover-abc',
      };
      const ready: JdbcDetectSchemaStatusResponse = {
        status: 'READY',
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
        glue_table: 'agentic_data_ingestion_abc_users',
        schema: [{ name: 'id', type: 'bigint' }],
      };
      // First call -> RUNNING (poll continues), all subsequent -> READY (poll stops).
      mockedGet.mockResolvedValueOnce(running).mockResolvedValue(ready);

      const { Wrapper } = createWrapper();
      renderHook(
        () => useJdbcDetectSchemaStatus('agentic-data-ingestion-discover-abc'),
        { wrapper: Wrapper },
      );

      // Initial fetch (RUNNING).
      await flushMicrotasks();
      expect(mockedGet).toHaveBeenCalledTimes(1);

      // After 3s the second fetch returns READY.
      await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
      await flushMicrotasks();
      expect(mockedGet).toHaveBeenCalledTimes(2);

      // Once READY is observed, refetchInterval returns false.
      // Advance many intervals — no further fetches.
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      await flushMicrotasks();
      expect(mockedGet).toHaveBeenCalledTimes(2);
    });

    it('stops polling once status flips to FAILED', async () => {
      const running: JdbcDetectSchemaStatusResponse = {
        status: 'RUNNING',
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
        table_prefix: 'agentic_data_ingestion_abc_',
        crawler_name: 'agentic-data-ingestion-discover-abc',
      };
      const failed: JdbcDetectSchemaStatusResponse = {
        status: 'FAILED',
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
        crawler_name: 'agentic-data-ingestion-discover-abc',
        error_message: 'security-group misconfiguration',
      };
      mockedGet.mockResolvedValueOnce(running).mockResolvedValue(failed);

      const { Wrapper } = createWrapper();
      renderHook(
        () => useJdbcDetectSchemaStatus('agentic-data-ingestion-discover-abc'),
        { wrapper: Wrapper },
      );

      await flushMicrotasks();
      expect(mockedGet).toHaveBeenCalledTimes(1);

      await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
      await flushMicrotasks();
      expect(mockedGet).toHaveBeenCalledTimes(2);

      // Terminal status FAILED — no further fetches even after a long delay.
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      await flushMicrotasks();
      expect(mockedGet).toHaveBeenCalledTimes(2);
    });
  });
});
