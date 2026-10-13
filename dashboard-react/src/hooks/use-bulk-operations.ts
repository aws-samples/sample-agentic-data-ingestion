// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { pipelineKeys, archiveKeys } from './query-keys';
import type { Pipeline } from '../types';

export interface BulkProgress {
  completed: number;
  total: number;
}

export interface BulkResult {
  succeeded: string[];
  failed: { id: string; name: string; error: string }[];
}

async function executeBulk(
  pipelines: Pipeline[],
  action: (id: string) => Promise<unknown>,
  onProgress: (progress: BulkProgress) => void,
): Promise<BulkResult> {
  const total = pipelines.length;
  const succeeded: string[] = [];
  const failed: BulkResult['failed'] = [];

  for (let i = 0; i < pipelines.length; i++) {
    const p = pipelines[i];
    try {
      await action(p.pipeline_key);
      succeeded.push(p.pipeline_key);
    } catch (err) {
      failed.push({
        id: p.pipeline_key,
        name: p.pipeline_name || p.pipeline_key,
        error: err instanceof Error ? err.message : 'Unknown error',
      });
    }
    onProgress({ completed: i + 1, total });
  }

  return { succeeded, failed };
}

export function useBulkArchive() {
  const queryClient = useQueryClient();
  const [progress, setProgress] = useState<BulkProgress | null>(null);
  const [isExecuting, setIsExecuting] = useState(false);

  const execute = useCallback(async (pipelines: Pipeline[]): Promise<BulkResult> => {
    setIsExecuting(true);
    setProgress({ completed: 0, total: pipelines.length });
    try {
      const result = await executeBulk(
        pipelines,
        (id) => apiClient.put(`/pipelines/${id}/archive`, {}),
        setProgress,
      );
      await queryClient.invalidateQueries({ queryKey: pipelineKeys.all });
      await queryClient.invalidateQueries({ queryKey: archiveKeys.all });
      return result;
    } finally {
      setIsExecuting(false);
      setProgress(null);
    }
  }, [queryClient]);

  return { execute, progress, isExecuting };
}

export function useBulkDelete() {
  const queryClient = useQueryClient();
  const [progress, setProgress] = useState<BulkProgress | null>(null);
  const [isExecuting, setIsExecuting] = useState(false);

  const execute = useCallback(async (pipelines: Pipeline[]): Promise<BulkResult> => {
    setIsExecuting(true);
    setProgress({ completed: 0, total: pipelines.length });
    try {
      const result = await executeBulk(
        pipelines,
        (id) => apiClient.delete(`/pipelines/${id}`),
        setProgress,
      );
      await queryClient.invalidateQueries({ queryKey: pipelineKeys.all });
      await queryClient.invalidateQueries({ queryKey: archiveKeys.all });
      return result;
    } finally {
      setIsExecuting(false);
      setProgress(null);
    }
  }, [queryClient]);

  return { execute, progress, isExecuting };
}
