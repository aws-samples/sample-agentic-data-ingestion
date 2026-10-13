// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { glueJobKeys } from './query-keys';
import type { GlueJobDetail, JobVersionHistoryResponse } from '../types';

async function fetchGlueJob(jobName: string): Promise<GlueJobDetail> {
  // The API uses pipeline key as the path, not job name
  return apiClient.get<GlueJobDetail>(`/pipelines/${encodeURIComponent(jobName)}/glue-job`);
}

async function runGlueJob(jobName: string): Promise<void> {
  await apiClient.post<unknown>(`/pipelines/${encodeURIComponent(jobName)}/glue-job/run`, {});
}

async function fetchJobVersionHistory(pipelineId: string): Promise<JobVersionHistoryResponse> {
  return apiClient.get<JobVersionHistoryResponse>(`/pipelines/${encodeURIComponent(pipelineId)}/glue-job/versions`);
}

async function rollbackGlueJob(pipelineId: string): Promise<void> {
  await apiClient.post<unknown>(`/pipelines/${encodeURIComponent(pipelineId)}/glue-job/rollback`, {});
}

export function useGlueJob(jobName: string | undefined) {
  return useQuery({
    queryKey: glueJobKeys.detail(jobName!),
    queryFn: () => fetchGlueJob(jobName!),
    enabled: !!jobName,
    staleTime: 15_000,
  });
}

export function useRunGlueJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: runGlueJob,
    onSuccess: (_data, jobName) => {
      queryClient.invalidateQueries({ queryKey: glueJobKeys.detail(jobName) });
    },
  });
}

export function useJobVersionHistory(pipelineId: string, enabled: boolean) {
  return useQuery({
    queryKey: glueJobKeys.versions(pipelineId),
    queryFn: () => fetchJobVersionHistory(pipelineId),
    enabled,
    staleTime: 30_000,
  });
}

export function useRollbackGlueJob(pipelineId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => rollbackGlueJob(pipelineId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: glueJobKeys.all });
      queryClient.invalidateQueries({ queryKey: ['glue-script', pipelineId] });
    },
  });
}
