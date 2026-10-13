// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { archiveKeys, pipelineKeys } from './query-keys';
import type { Pipeline } from '../types';

async function fetchArchivedPipelines(): Promise<Pipeline[]> {
  const body = await apiClient.get<Record<string, unknown> | Pipeline[]>('/pipelines/archived');
  // Normalize: API may return Pipeline[] or { pipelines: Pipeline[] }
  let items: unknown[];
  if (Array.isArray(body)) items = body;
  else if (body?.pipelines && Array.isArray(body.pipelines)) items = body.pipelines;
  else return [];
  // API returns pipeline_id from DynamoDB; normalize to pipeline_key
  return items.map((item) => {
    const p = item as Record<string, unknown>;
    return {
      ...p,
      pipeline_key: (p.pipeline_key ?? p.pipeline_id ?? '') as string,
    };
  }) as Pipeline[];
}

async function archivePipeline(id: string): Promise<void> {
  await apiClient.put<unknown>(`/pipelines/${id}/archive`, {});
}

async function restorePipeline(id: string): Promise<void> {
  await apiClient.put<unknown>(`/pipelines/${id}/restore`, {});
}

async function deletePipeline(id: string): Promise<void> {
  await apiClient.delete<unknown>(`/pipelines/${encodeURIComponent(id)}`);
}

export function useArchivedPipelines() {
  return useQuery({
    queryKey: archiveKeys.list(),
    queryFn: fetchArchivedPipelines,
    staleTime: 30_000,
  });
}

export function useArchivePipeline() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: archivePipeline,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: archiveKeys.all });
      queryClient.invalidateQueries({ queryKey: pipelineKeys.all });
    },
  });
}

export function useRestorePipeline() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: restorePipeline,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: archiveKeys.all });
      queryClient.invalidateQueries({ queryKey: pipelineKeys.all });
    },
  });
}

export function useDeletePipeline() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deletePipeline,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: archiveKeys.all });
    },
  });
}
