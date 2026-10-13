// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { approvalKeys, executionKeys } from './query-keys';
import type { Approval, ApprovalDetail } from '../types';

/** Requested per-page size. The server controls the true page size; this is
 *  the hint sent so token-paginated backends return ~50 rows per page (R3.1). */
const PAGE_SIZE = 50;

/**
 * One page of approvals, normalized to a stable shape regardless of what the
 * approvals endpoint returns today vs. after p28 adds server pagination.
 */
export interface ApprovalsPageResult {
  approvals: Approval[];
  /** Opaque token for the NEXT page, when the API supplies one. */
  nextToken?: string;
  /** Whether the server indicates more pages exist. */
  hasMore: boolean;
}

/**
 * Fetch a single page of pending approvals.
 *
 * Coordination note (p28 / R12.1): the approvals endpoint currently returns a
 * PLAIN `Approval[]` with no pagination tokens (see
 * `lambda/.../handlers/approvals.py::get_approvals`, which ignores `limit` and
 * returns every matching row). Backend pagination is out of scope here, so this
 * client is written to CONSUME tokens when present and DEGRADE GRACEFULLY to a
 * single page when absent:
 *   - array body            → one page, `hasMore: false`
 *   - `{ approvals, next_token, has_more }` → server-driven token pagination
 * When p28 adds `next_token`/`has_more`, pagination past the first 50 rows works
 * with no further client change.
 */
async function fetchApprovalsPage(token?: string): Promise<ApprovalsPageResult> {
  const params = new URLSearchParams({ status: 'pending', limit: String(PAGE_SIZE) });
  if (token) params.set('next_token', token);

  const body = await apiClient.get<Record<string, unknown> | Approval[]>(
    `/approvals?${params.toString()}`,
  );

  if (Array.isArray(body)) {
    return { approvals: body, nextToken: undefined, hasMore: false };
  }

  const approvals = Array.isArray(body?.approvals) ? (body.approvals as Approval[]) : [];
  const nextToken = typeof body?.next_token === 'string' ? (body.next_token as string) : undefined;
  const hasMore =
    typeof body?.has_more === 'boolean' ? (body.has_more as boolean) : Boolean(nextToken);

  return { approvals, nextToken, hasMore };
}

/**
 * Pending approvals with server-driven, token-based pagination (R3.1).
 *
 * Returns the raw `useInfiniteQuery` result: `data.pages[]` each hold up to one
 * server page of approvals. Callers flatten pages for display and call
 * `fetchNextPage()` to pull the next token page. `hasNextPage` reflects whether
 * a further page can be fetched.
 */
export function useApprovals() {
  return useInfiniteQuery({
    queryKey: approvalKeys.pending(),
    queryFn: ({ pageParam }) => fetchApprovalsPage(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => (lastPage.hasMore ? lastPage.nextToken : undefined),
    staleTime: 10_000,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
}

/**
 * Every pending approval on **one pipeline**, exactly (p51 C2/C3, R1.1–R1.3).
 *
 * `GET /approvals?pipeline_id=<key>&status=pending`, served by a keyed Query on
 * the `pipeline-status-index` GSI — `(pipeline_id, status)`, `ProjectionType.ALL`.
 * This is a different question from {@link useApprovals}, which is the global
 * pending list and is **untouched** by this hook's existence.
 *
 * **Why this exists.** `PipelineOverviewPage` used to derive its awaiting-approval
 * set from one page of ten execution rows, so a run awaiting approval outside the
 * ten newest lost its Approve/Reject buttons entirely — a missing action, not just
 * a wrong number. The approvals row also carries strictly more than the execution
 * row's lone `pending_approval_id`: `approval_id`, `run_id`, `approval_type` and
 * `created_at` all project under `ALL`, so no follow-up read per approval is needed.
 *
 * **Bare array, passed through.** The endpoint returns `Approval[]`
 * `created_at`-descending — no envelope, no count field, and no matches is a **200
 * with `[]`** rather than a 404. `LGR-2bcdd0d-1` (**template / ratified**) governs:
 * do not re-introduce a flat count or a new envelope for this read, so nothing here
 * reshapes the body. A caller reads `data.length` for the count and `data` for the
 * controls, which is what makes the count and the controls describe the same set
 * (R1.2) rather than two sets that can disagree.
 *
 * **No `refetchInterval`, deliberately.** The key placement is the refresh
 * mechanism: `executionKeys.pendingApprovals` sits under the `executions` prefix,
 * so a WebSocket frame (`WebSocketProvider.tsx:556`) and an approve/reject
 * (`useApprovalAction.ts:74`) each invalidate it with no new wiring — see that key's
 * own doc comment for the measured argument. Adding a bespoke interval here would
 * put a second cadence authority beside `EXECUTIONS_POLL_MATRIX`, which
 * `LGR-e800696-1` (**template / ratified**) fences along with
 * `computeExecutionsRefetchInterval`'s signature and `usePipelineExecutions`'
 * `refetchInterval` callback — none of which is touched. `staleTime` is the app-wide
 * 30 s default from `main.tsx:21`, for the same reason: one authority, not two.
 *
 * `enabled` guards the undefined-key render, so the overview page can call this
 * before its pipeline has loaded without firing a request for `undefined`.
 */
export function usePipelinePendingApprovals(pipelineKey: string | undefined) {
  return useQuery({
    queryKey: executionKeys.pendingApprovals(pipelineKey!),
    queryFn: () =>
      apiClient.get<Approval[]>(
        `/approvals?pipeline_id=${encodeURIComponent(pipelineKey!)}&status=pending`,
      ),
    enabled: !!pipelineKey,
  });
}

/**
 * What the surface SAYS when the stored payload was not established.
 *
 * Two sentences, because `'absent'` and `'unreadable'` are two facts. The field
 * readers already render an em dash for both — they cannot tell them apart, and
 * they should not have to — so these state the difference once, above them,
 * rather than being pushed down into every reader.
 *
 * Words, not colour: `LGR-99050e6-1` (**template / ratified**) requires the state
 * to be carried in text, and colour alone fails WCAG 1.4.1. Phrased about the
 * READ and about the PRODUCER respectively, so neither can be mistaken for "this
 * approval has no mappings" — `LGR-b2f79ef-14`'s distinction, and the same shape
 * p54 gave `REUSED_MAPPING_UNREADABLE_STATEMENT` for a single field.
 *
 * **Why they live here rather than in `src/utils/`.** The three field readers are
 * in `utils/` because TWO surfaces share each of them, which is what
 * `LGR-4854b3d-1` and `LGR-e59edd7-6` require. This status is produced by exactly
 * one endpoint and read by exactly one surface, so it sits beside the hook that
 * owns that endpoint's response contract. Promote it to `utils/` the moment
 * `ApprovalWorkflowSection` needs it — that is the condition, not a preference.
 */
export const APPROVAL_DETAILS_ABSENT_STATEMENT =
  'No approval payload was recorded for this approval, so there is nothing to review below.';

export const APPROVAL_DETAILS_UNREADABLE_STATEMENT =
  'The recorded approval payload could not be read, so the fields below state nothing about it. ' +
  'This is not the same as an approval with no mappings.';

/**
 * One approval WITH its payload — `GET /api/approvals/{approval_id}` (the p56
 * per-id endpoint).
 *
 * **Why a second read rather than a richer listing.** Neither listing handler
 * parses the stored `approval_data`, so `approval.details` was `undefined` on this
 * whole surface and every reader in the review modal correctly reported "not
 * established": the mapping count, the unsourced target fields, the confidence,
 * the proposed renames, and `MappingViewer`'s entire panel. Parsing it in the
 * listing instead was rejected — `get_approvals` unfiltered enumerates all four
 * statuses and paginates every row with no cap, so it would ship every approval's
 * full `proposed_mappings` on every page load to serve a modal opened for one row.
 * `LGR-14d6dd3-2` (**template / ratified**) settled that shape for this repo when
 * it moved per-row schema field resolution off the listing onto
 * `GET /api/schemas/{id}`.
 *
 * `enabled` guards the closed-modal render, so the page can call this
 * unconditionally and fire no request until a row is actually being reviewed.
 * `approvalKeys.detail` already existed and is unused elsewhere; it is
 * `['approvals', id]`, which an `approvalKeys.all` invalidation reaches by prefix
 * and `approvalKeys.pending()` cannot collide with (no approval_id is `'pending'`
 * — the producer writes `{pipeline_id}_{approval_type}_{epoch}`).
 *
 * No `refetchInterval`: a payload is written once, at approval-creation time, and
 * never rewritten. Polling it would be a second cadence authority beside
 * `EXECUTIONS_POLL_MATRIX` for no new information — the reason
 * `usePipelinePendingApprovals` above declines one too (`LGR-e800696-1`).
 */
export function useApprovalDetail(approvalId: string | undefined) {
  return useQuery({
    queryKey: approvalKeys.detail(approvalId!),
    queryFn: () => apiClient.get<ApprovalDetail>(`/approvals/${encodeURIComponent(approvalId!)}`),
    enabled: !!approvalId,
  });
}

// The approval mutation (Approval_Contract) lives in `useApprovalAction.ts`
// as the single shared hook used by every approval surface (R6).
