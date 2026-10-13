// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient, ApiError } from '../services/api-client';
import { agentTraceKeys } from './query-keys';
import type { AgentTrace, AgentTraceListResponse } from '../types';

async function fetchRunTraces(runId: string): Promise<AgentTraceListResponse | null> {
  try {
    return await apiClient.get<AgentTraceListResponse>(`/runs/${runId}/agent-trace`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

async function fetchAgentTrace(runId: string, stepName: string): Promise<AgentTrace | null> {
  try {
    return await apiClient.get<AgentTrace>(`/runs/${runId}/agent-trace/${stepName}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

/** Fetch all agent traces for a run. */
export function useRunTraces(runId: string | undefined, isActive = false) {
  return useQuery({
    queryKey: agentTraceKeys.run(runId!),
    queryFn: () => fetchRunTraces(runId!),
    enabled: !!runId,
    staleTime: 10_000,
    refetchInterval: isActive ? 10_000 : false,
    retry: false,
  });
}

/** Fetch a single agent trace for a specific step within a run. */
export function useAgentTrace(runId: string | undefined, stepName: string | undefined) {
  return useQuery({
    queryKey: agentTraceKeys.step(runId!, stepName!),
    queryFn: () => fetchAgentTrace(runId!, stepName!),
    enabled: !!runId && !!stepName,
    staleTime: 30_000,
    retry: false,
  });
}
