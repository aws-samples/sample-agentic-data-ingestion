// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { apiClient, ApiError } from '../services/api-client';
import { runKeys, executionKeys } from './query-keys';
import { useWebSocket } from './useWebSocket';
import { useToast } from '../contexts/ToastContext';
import { isActiveRunStatus } from '../utils/formatting';
import type { RunDetail, RetryResponse } from '../types';

/**
 * Exported for characterization testing only (p38 task 1.1). Behaviour is
 * unchanged by the export; `useRunDetail` below remains the only production
 * caller. Tests must import THIS function rather than replicate its logic —
 * `use-run-detail.property.test.ts` declares a local copy of the polling
 * interval and therefore covers nothing in this module.
 */
export async function fetchRunDetail(runId: string): Promise<RunDetail | null> {
  try {
    const body = await apiClient.get<Record<string, unknown>>(`/runs/${runId}/details`);
    // The request completed, but the response is not a run (p38 R4.2). This
    // must reach the caller as a rejection: it is the only way the page can
    // report "the run details could not be retrieved" separately from "no
    // such run", which is the state `null` is now reserved for.
    if (body?.error) throw new Error(String(body.error));
    return body as unknown as RunDetail;
  } catch (err) {
    // R4.5 / do-NOT 7 — unchanged. A 404 stays a non-error result so the
    // global 404-toast suppression (`main.tsx:33-34`) and the 4xx
    // retry-disable (`main.tsx:23-26`) keep applying. `null` now means
    // exactly one thing: there is no run with this id.
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

export function useRunDetail(runId: string | undefined, pipelineId?: string) {
  const { isConnected } = useWebSocket({
    pipelineIds: pipelineId ? [pipelineId] : undefined,
    enabled: !!pipelineId,
  });

  const query = useQuery({
    queryKey: runKeys.detail(runId!),
    queryFn: () => fetchRunDetail(runId!),
    enabled: !!runId,
    staleTime: 10_000,
    retry: false,
    refetchInterval: (q) => {
      // CANCELLING counts as active so the page sees the run become CANCELLED.
      const isActive = isActiveRunStatus(q.state.data?.status);
      if (isConnected) {
        // WebSocket connected: poll as safety net
        return isActive ? 10_000 : 30_000;
      }
      // No WebSocket: poll actively
      return isActive ? 5_000 : 15_000;
    },
  });

  return { ...query, isWebSocketConnected: isConnected };
}


export function useRetryRun() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { showToast } = useToast();

  return useMutation({
    mutationFn: ({ runId, retryFromStep }: { runId: string; retryFromStep?: string }) =>
      apiClient.post<RetryResponse>(`/runs/${runId}/retry`, {
        retry_from_step: retryFromStep,
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: runKeys.all });
      queryClient.invalidateQueries({ queryKey: executionKeys.all });
      navigate(`/pipelines/${data.pipeline_id}/runs/${data.run_id}`);
      showToast(`Retry started from step: ${data.retry_from_step}`, 'success');
    },
    onError: (err) => {
      const message = err instanceof ApiError ? err.message : 'Failed to retry run';
      showToast(message, 'error');
    },
  });
}
