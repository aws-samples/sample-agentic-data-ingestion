// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery, useMutation } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';

export interface CostRecommendation {
  id: string;
  pipeline_id: string;
  type: 'downsize' | 'idle' | 'upgrade';
  current?: { worker_type: string; workers: number };
  recommended?: { worker_type: string; workers: number };
  avg_dpu_utilization?: number;
  projected_savings_monthly?: number;
  risk: 'low' | 'medium' | 'high';
  auto_apply_eligible?: boolean;
  last_run?: string;
  recommendation?: string;
}

export interface CostRecommendationsResponse {
  generated_at: string;
  recommendations: CostRecommendation[];
  total_projected_savings_monthly: number;
}

export const costRecommendationKeys = {
  all: ['cost-recommendations'] as const,
};

/** Fetch latest cost optimization recommendations. */
export function useCostRecommendations() {
  return useQuery({
    queryKey: costRecommendationKeys.all,
    queryFn: () => apiClient.get<CostRecommendationsResponse>('/cost-recommendations'),
    staleTime: 60_000,
    retry: false,
  });
}

/** Apply a cost recommendation by id. */
export function useApplyRecommendation() {
  return useMutation({
    mutationFn: async (recommendationId: string) => {
      return apiClient.post<{ status: string }>(`/cost-recommendations/${recommendationId}/apply`, {});
    },
  });
}
