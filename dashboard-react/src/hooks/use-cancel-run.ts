// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import type { ToastType } from '../contexts/ToastContext';
import { executionKeys, runKeys } from './query-keys';

/**
 * The body of `POST /runs/{runId}/cancel` when the cancel was accepted.
 *
 * `CANCELLED` (HTTP 200): the run was stopped at a Lambda step or an approval
 * gate, or was already cancelled. `CANCELLING` (HTTP 202): the run's Glue job is
 * being stopped; the run becomes `CANCELLED` once Glue reports the job stopped,
 * which can take minutes. A finished run answers 409 and an unknown one 404;
 * both reach `onError` as an `ApiError` whose message is the server's reason.
 *
 * Pinned by `tests/contracts/api_responses/test_cancel_run_response_contract.py`.
 */
export interface CancelRunResponse {
  success: boolean;
  run_id: string;
  status: 'CANCELLED' | 'CANCELLING';
  message: string;
}

async function cancelRun(runId: string): Promise<CancelRunResponse> {
  return apiClient.post<CancelRunResponse>(`/runs/${runId}/cancel`, {});
}

/** The toast for an accepted cancel. A 202 must not claim the run is already cancelled. */
export function cancelOutcomeToast(response: CancelRunResponse): { message: string; type: ToastType } {
  if (response.status === 'CANCELLING') {
    return {
      message: 'Stopping the Glue job. The run will show CANCELLED once the job has stopped.',
      type: 'info',
    };
  }
  return { message: 'Run cancelled', type: 'success' };
}

/** The toast for a refused or failed cancel, carrying the server's reason when there is one. */
export function cancelFailureMessage(error: unknown): string {
  return error instanceof Error && error.message ? `Failed to cancel run: ${error.message}` : 'Failed to cancel run';
}

export function useCancelRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: cancelRun,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: executionKeys.all });
      queryClient.invalidateQueries({ queryKey: runKeys.all });
    },
  });
}
