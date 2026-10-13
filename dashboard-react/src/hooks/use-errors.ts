// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { API_ENDPOINTS } from '../services/api-endpoints';

export interface ErrorEntry {
  error_date: string;
  timestamp: string;
  run_id: string;
  pipeline_id: string;
  step_name: string;
  error_type: string;
  error_message: string;
  ttl: number;
}

interface ErrorsResponse {
  errors: ErrorEntry[];
  count: number;
  start_date: string;
  end_date: string;
}

interface DateRange {
  start_date?: string;
  end_date?: string;
}

interface ErrorFilters {
  pipeline_id?: string;
  error_type?: string;
}

export const errorKeys = {
  all: ['errors'] as const,
  list: (dateRange: DateRange, filters: ErrorFilters) =>
    ['errors', 'list', dateRange, filters] as const,
};

async function fetchErrors(dateRange: DateRange, filters: ErrorFilters): Promise<ErrorsResponse> {
  const params = new URLSearchParams();
  if (dateRange.start_date) params.set('start_date', dateRange.start_date);
  if (dateRange.end_date) params.set('end_date', dateRange.end_date);
  if (filters.pipeline_id) params.set('pipeline_id', filters.pipeline_id);
  if (filters.error_type) params.set('error_type', filters.error_type);

  const qs = params.toString();
  const url = `${API_ENDPOINTS.errors.list}${qs ? `?${qs}` : ''}`;
  return apiClient.get<ErrorsResponse>(url);
}

export function useErrors(dateRange: DateRange = {}, filters: ErrorFilters = {}) {
  return useQuery({
    queryKey: errorKeys.list(dateRange, filters),
    queryFn: () => fetchErrors(dateRange, filters),
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  });
}
