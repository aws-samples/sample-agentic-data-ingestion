// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { mappingMemoryKeys } from './query-keys';
import type { MappingMemoryResponse } from '../types';

const DELETED_IDS_KEY = 'agentic-data-ingestion-deleted-memory-ids';
const DELETE_TTL_MS = 30_000; // 30s — enough for eventual consistency

/** Read pending deletes from localStorage, pruning expired entries. */
function getPendingDeletes(): Record<string, number> {
  try {
    const raw = localStorage.getItem(DELETED_IDS_KEY);
    if (!raw) return {};
    const map: Record<string, number> = JSON.parse(raw);
    const now = Date.now();
    let changed = false;
    for (const [id, ts] of Object.entries(map)) {
      if (now - ts > DELETE_TTL_MS) {
        delete map[id];
        changed = true;
      }
    }
    if (changed) localStorage.setItem(DELETED_IDS_KEY, JSON.stringify(map));
    return map;
  } catch {
    return {};
  }
}

function addPendingDelete(entryId: string) {
  const map = getPendingDeletes();
  map[entryId] = Date.now();
  localStorage.setItem(DELETED_IDS_KEY, JSON.stringify(map));
}

async function fetchMappingMemory(pipelineId: string): Promise<MappingMemoryResponse> {
  const raw = await apiClient.get<MappingMemoryResponse>(
    `/pipelines/${encodeURIComponent(pipelineId)}/mapping-memory`,
  );
  // Filter out entries that are pending deletion (eventual consistency window)
  const pending = getPendingDeletes();
  if (Object.keys(pending).length > 0) {
    raw.entries = raw.entries.filter((e) => !(e.entry_id in pending));
    raw.count = raw.entries.length;
  }
  return raw;
}

async function deleteMappingMemoryEntry(pipelineId: string, entryId: string): Promise<void> {
  await apiClient.delete(
    `/pipelines/${encodeURIComponent(pipelineId)}/mapping-memory/${encodeURIComponent(entryId)}`,
  );
}

export function useMappingMemory(pipelineId: string) {
  return useQuery({
    queryKey: mappingMemoryKeys.list(pipelineId),
    queryFn: () => fetchMappingMemory(pipelineId),
    enabled: !!pipelineId,
  });
}

export function useDeleteMappingMemoryEntry(pipelineId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (entryId: string) => deleteMappingMemoryEntry(pipelineId, entryId),
    onMutate: async (entryId: string) => {
      // Track in localStorage so it persists across refreshes
      addPendingDelete(entryId);
      // Optimistic cache update
      await queryClient.cancelQueries({ queryKey: mappingMemoryKeys.list(pipelineId) });
      const previous = queryClient.getQueryData<MappingMemoryResponse>(mappingMemoryKeys.list(pipelineId));
      if (previous) {
        queryClient.setQueryData<MappingMemoryResponse>(mappingMemoryKeys.list(pipelineId), {
          ...previous,
          entries: previous.entries.filter((e) => e.entry_id !== entryId),
          count: previous.count - 1,
        });
      }
      return { previous };
    },
    onError: (_err, _entryId, context) => {
      if (context?.previous) {
        queryClient.setQueryData(mappingMemoryKeys.list(pipelineId), context.previous);
      }
    },
    onSettled: () => {
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: mappingMemoryKeys.list(pipelineId) });
      }, 15000);
    },
  });
}
