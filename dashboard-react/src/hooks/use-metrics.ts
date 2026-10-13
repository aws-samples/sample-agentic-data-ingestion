// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { metricsKeys } from './query-keys';
import type { ComprehensiveMetrics } from '../types';

// P29 R4.1: no MOCK_METRICS fallback. A failed/404 metrics fetch surfaces as a
// React Query error so the page can render an honest error state — never
// fabricated numbers (the old mock included a fake $1,850/mo cost figure).
async function fetchMetrics(period: string): Promise<ComprehensiveMetrics> {
  return apiClient.get<ComprehensiveMetrics>(`/metrics/comprehensive?period=${period}`);
}

export function useMetrics(period: string = '24h') {
  return useQuery({
    queryKey: [...metricsKeys.comprehensive(), period],
    queryFn: () => fetchMetrics(period),
    staleTime: 60_000,
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
}
