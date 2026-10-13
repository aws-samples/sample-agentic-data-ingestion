// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient, ApiError } from '../services/api-client';
import { mappingKeys, pipelineKeys } from './query-keys';
import type { MappingData } from '../types';

async function fetchMappings(
  pipelineKey: string,
  schemaHash: string,
  versionIndex: number,
): Promise<MappingData | null> {
  const params = new URLSearchParams({
    schema_hash: schemaHash,
    version_index: String(versionIndex),
  });
  try {
    return await apiClient.get<MappingData>(
      `/pipelines/${encodeURIComponent(pipelineKey)}/mappings?${params}`,
    );
  } catch (err) {
    // Return null for 404 — mapping may not exist yet
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

async function saveMappingHints(params: {
  pipelineKey: string;
  hints: Record<string, string>;
}): Promise<void> {
  await apiClient.post<unknown>(
    `/pipelines/${encodeURIComponent(params.pipelineKey)}/mapping-hints`,
    { hints: params.hints },
  );
}

async function regenerateMappings(params: {
  pipelineKey: string;
  context?: string;
  model_id?: string;
}): Promise<void> {
  const body: Record<string, string> = {
    pipeline_key: params.pipelineKey,
    context: params.context ?? '',
  };
  if (params.model_id) {
    body.model_id = params.model_id;
  }
  await apiClient.post<unknown>('/rerun-mapping', body);
}

export function useMappings(
  pipelineKey: string | undefined,
  schemaHash: string | undefined,
  versionIndex: number,
) {
  return useQuery({
    queryKey: mappingKeys.detail(pipelineKey!, schemaHash!, versionIndex),
    queryFn: () => fetchMappings(pipelineKey!, schemaHash!, versionIndex),
    enabled: !!pipelineKey && !!schemaHash,
    staleTime: 30_000,
    retry: false,
  });
}

export function useSaveMappingHints() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: saveMappingHints,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: mappingKeys.all(variables.pipelineKey),
      });
    },
  });
}

export function useRegenerateMappings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: regenerateMappings,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: mappingKeys.all(variables.pipelineKey),
      });
      queryClient.invalidateQueries({
        queryKey: pipelineKeys.detail(variables.pipelineKey),
      });
    },
  });
}

export function useUpdateMappings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (params: {
      pipelineKey: string;
      mappings: Array<{ source_field: string; target_field: string; confidence?: number; hint?: string }>;
      mappingId?: string;
    }) => {
      await apiClient.put<unknown>(
        `/pipelines/${encodeURIComponent(params.pipelineKey)}/mappings`,
        { mappings: params.mappings, mapping_id: params.mappingId ?? '' },
      );
    },
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: mappingKeys.all(variables.pipelineKey),
      });
      queryClient.invalidateQueries({
        queryKey: pipelineKeys.detail(variables.pipelineKey),
      });
    },
  });
}
