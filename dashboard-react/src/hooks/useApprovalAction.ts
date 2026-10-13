// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * useApprovalAction — THE single approval mutation (Approval_Contract, R6).
 *
 * Every approval surface (ApprovalsPage, RunDetailPage, ApprovalWorkflowSection)
 * routes through this one hook so the audit trail means something. It replaces
 * the previous divergent shapes: the old `useApprovals` mutation that posted
 * `{ comment }` and RunDetailPage's inline `apiClient.post(..., { decision, user })`.
 *
 * Contract:
 *  - Endpoint: `POST /approvals/{id}/{action}` (action = approve | reject — the
 *    backend routes on the path segment; see handlers/approvals.py).
 *  - Payload: `{ decision, comment, user, question_answers? }`.
 *      · `decision` — 'approved' | 'rejected' (recorded outcome).
 *      · `user` — Identity_From_Token (R6.2): the real Cognito identity from
 *        `useAuth()`, `profile.email` preferred with `profile.sub` fallback.
 *        Replaces the hardcoded `'dashboard-user'` that used to be sent from the
 *        client. (The backend still defaults to 'dashboard-user' only when no
 *        identity is supplied.)
 *      · `question_answers` — optional answers to agent disambiguation
 *        questions, included only when present.
 *  - On success (R6.3): event-driven refresh via query invalidation
 *    (`['approvals']`, and the acting run's `['runs', runId]` when known). No
 *    blind `setTimeout` refetch chains — WebSocket `run_update` messages also
 *    invalidate through the WebSocketProvider, so the UI converges even when the
 *    backend resume is slow.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from 'react-oidc-context';
import { apiClient } from '../services/api-client';
import { approvalKeys, executionKeys, pipelineKeys, runKeys } from './query-keys';

/** approve | reject — selects the endpoint and the recorded decision. */
export type ApprovalDecision = 'approve' | 'reject';

export interface ApprovalActionVars {
  /** Approval id to act on. */
  id: string;
  /** approve | reject. */
  action: ApprovalDecision;
  /** Optional reviewer comment. */
  comment?: string;
  /** Optional answers to agent disambiguation questions, keyed by question/field id. */
  questionAnswers?: Record<string, string>;
  /** When acting from a run context, the run id whose detail should be invalidated. */
  runId?: string;
}

export function useApprovalAction() {
  const queryClient = useQueryClient();
  const auth = useAuth();

  // Identity_From_Token (R6.2). email preferred, sub as fallback. Never the
  // hardcoded 'dashboard-user' — that string no longer originates in the client.
  const identity = auth.user?.profile?.email ?? auth.user?.profile?.sub;

  return useMutation({
    mutationFn: ({ id, action, comment, questionAnswers }: ApprovalActionVars) => {
      const decision = action === 'approve' ? 'approved' : 'rejected';
      const payload: Record<string, unknown> = {
        decision,
        comment: comment ?? '',
        user: identity,
      };
      if (questionAnswers && Object.keys(questionAnswers).length > 0) {
        payload.question_answers = questionAnswers;
      }
      return apiClient.post<unknown>(`/approvals/${id}/${action}`, payload);
    },
    onSuccess: (_data, variables) => {
      // Event-driven refresh (R6.3): invalidate the approvals list, the run
      // lists, and — when acting from a run — that run's detail. No setTimeout
      // refetch chains.
      queryClient.invalidateQueries({ queryKey: approvalKeys.all });
      queryClient.invalidateQueries({ queryKey: executionKeys.all });
      queryClient.invalidateQueries({ queryKey: pipelineKeys.all });
      if (variables.runId) {
        queryClient.invalidateQueries({ queryKey: runKeys.detail(variables.runId) });
      }
    },
  });
}
