// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient, ApiError } from '../services/api-client';
import { API_ENDPOINTS } from '../services/api-endpoints';
import { previewKeys } from './query-keys';
import type { DataPreviewResponse } from '../types';

export function useDataPreview(
  pipelineId?: string,
  runId?: string,
  limit?: number,
  options?: { enabled?: boolean },
) {
  return useQuery<DataPreviewResponse>({
    queryKey: runId
      ? previewKeys.run(pipelineId!, runId)
      : previewKeys.pipeline(pipelineId!),
    queryFn: () => {
      const basePath = runId
        ? API_ENDPOINTS.preview.specificRun(pipelineId!, runId)
        : API_ENDPOINTS.preview.latestRun(pipelineId!);
      const url = limit != null ? `${basePath}?limit=${limit}` : basePath;
      return apiClient.get<DataPreviewResponse>(url);
    },
    enabled: !!pipelineId && (options?.enabled ?? true),
    staleTime: 60_000,
    retry: (failureCount, error) => {
      // Don't retry 404s — no data to preview yet
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 3;
    },
    meta: { suppressToast: true },
  });
}
