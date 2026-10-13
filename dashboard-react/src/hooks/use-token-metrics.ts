// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { metricsKeys } from './query-keys';

/** Token usage data point for a single day */
export interface TokenUsageDataPoint {
  date: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  estimated_cost_usd: number;
  invocation_count: number;
}

/** Model comparison row */
export interface ModelComparisonRow {
  model_id: string;
  avg_input_tokens: number;
  avg_output_tokens: number;
  avg_cost_usd: number;
  avg_latency_ms: number;
  invocation_count: number;
}

/** Full token usage metrics response */
export interface TokenUsageMetrics {
  daily_usage: TokenUsageDataPoint[];
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cost_usd: number;
  model_comparison: ModelComparisonRow[];
  period: string;
}

// P29 R4.1: no MOCK_TOKEN_METRICS fallback (analogous to the MOCK_METRICS
// deletion). A failed/404 token-usage fetch surfaces as a React Query error so
// the token-usage widget renders an honest error + retry — never fabricated data.
async function fetchTokenUsageMetrics(period: string): Promise<TokenUsageMetrics> {
  const raw = await apiClient.get<Record<string, unknown>>(`/metrics/token-usage?period=${period}`);
  return {
    daily_usage: (raw.daily as TokenUsageDataPoint[]) ?? (raw.daily_usage as TokenUsageDataPoint[]) ?? [],
    total_input_tokens: Number(raw.total_input_tokens) || 0,
    total_output_tokens: Number(raw.total_output_tokens) || 0,
    total_cache_read_tokens: Number(raw.total_cache_read_tokens) || 0,
    total_cost_usd: Number(raw.total_cost_usd ?? raw.total_estimated_cost_usd) || 0,
    model_comparison: (raw.model_comparison as ModelComparisonRow[]) ?? [],
    period: String(raw.period ?? period),
  };
}

export function useTokenUsageMetrics(period: string = '7d') {
  return useQuery({
    queryKey: [...metricsKeys.all, 'token-usage', period],
    queryFn: () => fetchTokenUsageMetrics(period),
    staleTime: 60_000,
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
}
