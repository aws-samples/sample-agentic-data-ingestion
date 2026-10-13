// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { glueJobLogKeys } from './query-keys';
import type { GlueJobLogsResponse, GlueLogEvent } from '../types';

async function fetchGlueJobLogs(
  pipelineId: string,
  runId: string,
  logType: string,
): Promise<GlueJobLogsResponse> {
  const raw = await apiClient.get<GlueJobLogsResponse>(
    `/pipelines/${encodeURIComponent(pipelineId)}/glue-job/runs/${encodeURIComponent(runId)}/logs?log_type=${encodeURIComponent(logType)}`,
  );
  // Normalize backend field names to what the UI components expect
  if (raw.logs) {
    raw.logs = raw.logs.map((e: GlueLogEvent) => ({
      ...e,
      level: e.log_level ?? e.level ?? 'INFO',
      timestamp_iso: e.timestamp_iso ?? (typeof e.timestamp === 'number' ? new Date(e.timestamp).toISOString() : String(e.timestamp)),
    }));
  }
  return raw;
}

export function useGlueJobLogs(
  pipelineId: string,
  runId: string | undefined,
  logType: string,
  isRunning: boolean,
) {
  return useQuery({
    queryKey: glueJobLogKeys.detail(pipelineId, runId ?? '', logType),
    queryFn: () => fetchGlueJobLogs(pipelineId, runId!, logType),
    enabled: !!pipelineId && !!runId,
    refetchInterval: isRunning ? 5000 : false,
    staleTime: isRunning ? 0 : 30_000,
  });
}
