// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { fileKeys } from './query-keys';
import type { FileDetail } from '../types';

async function fetchFileDetail(pipelineKey: string, fileKey: string): Promise<FileDetail> {
  return apiClient.get<FileDetail>(
    `/files/${encodeURIComponent(pipelineKey)}/${encodeURIComponent(fileKey)}/details`,
  );
}

export function useFileDetail(pipelineKey: string | undefined, fileKey: string | undefined) {
  return useQuery({
    queryKey: fileKeys.detail(pipelineKey!, fileKey!),
    queryFn: () => fetchFileDetail(pipelineKey!, fileKey!),
    enabled: !!pipelineKey && !!fileKey,
    staleTime: 30_000,
  });
}
