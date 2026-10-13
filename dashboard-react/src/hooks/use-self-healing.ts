// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient, ApiError } from '../services/api-client';

export interface SelfHealingActivity {
  agent_type: string;
  error_classification: string;
  remediation_applied: string;
  original_run_id: string;
  rerun_id: string;
  rerun_status: string;
  diagnosis_duration_ms: number;
  total_duration_ms: number;
  timestamp: number;
}

interface SelfHealingResponse {
  activity: SelfHealingActivity | null;
}

export const selfHealingKeys = {
  all: ['self-healing'] as const,
  run: (runId: string) => ['self-healing', runId] as const,
};

async function fetchSelfHealingActivity(runId: string): Promise<SelfHealingActivity | null> {
  try {
    const data = await apiClient.get<SelfHealingResponse>(`/pipelines/_/runs/${runId}/self-healing`);
    return data.activity ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

/** Fetch self-healing activity for a specific run. */
export function useSelfHealingActivity(runId: string | undefined) {
  return useQuery({
    queryKey: selfHealingKeys.run(runId!),
    queryFn: () => fetchSelfHealingActivity(runId!),
    enabled: !!runId,
    staleTime: 30_000,
    retry: false,
  });
}
