// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { API_ENDPOINTS } from '../services/api-endpoints';
import { updateKeys } from './query-keys';
import type { UpdatesResponse } from '../types';

export function useUpdates(limit?: number) {
  return useQuery<UpdatesResponse>({
    queryKey: updateKeys.list(limit),
    queryFn: () => {
      const url = limit != null
        ? `${API_ENDPOINTS.updates.list}?limit=${limit}`
        : API_ENDPOINTS.updates.list;
      return apiClient.get<UpdatesResponse>(url);
    },
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}
