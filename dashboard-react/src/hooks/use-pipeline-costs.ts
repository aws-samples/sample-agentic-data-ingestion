// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { API_ENDPOINTS } from '../services/api-endpoints';
import { costKeys } from './query-keys';

/**
 * One run's Glue cost, as `handlers/costs.py` emits it.
 *
 * `actual_cost` is the value `job_completion.collect_job_metrics` wrote into the
 * run's `output_data` — DPU-hours × the published $0.44/DPU-hour rate, with
 * Glue's 10-minute minimum billing applied, rounded to 3 decimals at write time.
 * `timestamp` is the state row's own timestamp and CAN be the empty string: the
 * handler defaults it (`item.get("timestamp", "")`) rather than dropping the run.
 */
export interface PipelineCostRun {
  run_id: string;
  actual_cost: number;
  timestamp: string;
}

/**
 * Response of `GET /api/pipelines/{id}/costs`.
 *
 * Derived by reading the handler end to end
 * (`lambda/agentic_data_ingestion_dashboard_api/handlers/costs.py:59-68`), not
 * from a sample response. Notes that matter to consumers:
 *
 *  - `total_cost` and `average_cost_per_run` are rounded to 3 decimals server
 *    side; `cost_trend[].actual_cost` is NOT re-rounded by the handler.
 *  - `cost_trend` is the 7 most recent runs, **newest first**.
 *  - `most_expensive_run` is `null` when no run carries a cost — the key is
 *    always present.
 *  - `run_count` counts runs that carried an `actual_cost`, not runs that
 *    exist. A pipeline with three in-progress runs reports 0.
 *
 * This interface is the oracle the `/costs` contract test parses (the ratified
 * pattern from `LGR-28b4d75-5`): a contract test written from the producer's own
 * output cannot detect the producer drifting. Keep the fields one per line in
 * `name[?]: type;` form so that parser keeps working.
 */
export interface PipelineCosts {
  pipeline_id: string;
  total_cost: number;
  average_cost_per_run: number;
  cost_trend: PipelineCostRun[];
  most_expensive_run: PipelineCostRun | null;
  run_count: number;
}

/** Fetch aggregated Glue cost for one pipeline (P30 R3.1). */
export function usePipelineCosts(pipelineId: string | undefined) {
  return useQuery({
    queryKey: costKeys.pipeline(pipelineId!),
    queryFn: () => apiClient.get<PipelineCosts>(API_ENDPOINTS.costs.pipeline(pipelineId!)),
    enabled: !!pipelineId,
    staleTime: 60_000,
    // Cost only changes when a run finishes, so a failure here is not worth a
    // retry storm — the panel offers an explicit Retry instead (R3.4).
    retry: false,
    meta: { suppressToast: true },
  });
}
