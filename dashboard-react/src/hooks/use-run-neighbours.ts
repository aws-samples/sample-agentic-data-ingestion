// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { executionKeys } from './query-keys';
import { EXECUTIONS_BACKGROUND_POLL_MS } from './use-executions';

/**
 * One neighbouring run, as `GET /pipeline/{key}/run/{run_id}/neighbours` returns it.
 *
 * `started_at` and `status` ride along so a caller can label the destination
 * without a second request. Nothing reads them today; they are here because the
 * producer sends them and a type that omits a field the wire carries is the
 * mismatch `LGR-28b4d75-5` warns about, in miniature.
 */
export interface RunNeighbour {
  run_id: string;
  started_at: string;
  status: string;
}

/**
 * `GET /pipeline/{key}/run/{run_id}/neighbours` (p48 C10, task 5.2).
 *
 * **Direction convention — the whole point of this shape, so it is named rather
 * than positional.** `older` / `newer` are anchored to *time*, so neither can be
 * read as "up" or "down" some list:
 *
 * - `older` — started immediately **before** this run. Drives **Previous Run**.
 * - `newer` — started immediately **after** this run. Drives **Next Run**.
 *
 * A `prev` / `next` pair was rejected for exactly that reason: it names a
 * direction of travel through a list rather than a direction in time, and the two
 * agree only for as long as the list is newest-first. This is what the old array
 * arithmetic meant (`runs[idx + 1]` over a newest-first page was the *older* run),
 * so the mapping below preserves the buttons' existing behaviour rather than
 * changing it.
 *
 * Both keys are always present, `null` where there is no such run: `newer` is
 * `null` on the newest run, `older` is `null` on the oldest, and a pipeline's only
 * run has both `null`.
 */
export interface RunNeighbours {
  pipeline_id: string;
  run_id: string;
  older: RunNeighbour | null;
  newer: RunNeighbour | null;
}

/**
 * The run either side of `runId`, resolved **on the server from the index** (p48
 * R10.2, R10.4).
 *
 * This replaces `usePipelineExecutions(pipelineId)` + `findIndex` in
 * `RunDetailPage`, which fetched a whole page of run history — plus that page's
 * `Select="COUNT"` walk and its `load_metrics` batch-gets — in order to read two
 * ids out of it, and which went dead at the page edge because the neighbour of the
 * tenth run was on page 2 and nothing on page 1 could name it.
 *
 * The server answers with three keyed reads at `Limit=1` and is therefore correct
 * at any depth — run 500 costs the same as run 5. It also keeps the ordering
 * authority in one place: R10.4 forbids a second one, and an index seek delegates
 * the question entirely to `RunsByTimeIndex`. **Nothing here re-sorts, re-orders or
 * re-derives position.**
 *
 * **Two transports, deliberately, and the poll is the one that is always on.**
 * `LGR-8b6e6a1-1`'s WebSocket flush invalidates the `['executions', id]` prefix this
 * key sits under, so a frame refreshes it — but `LGR-e800696-1` (**template /
 * ratified**) is explicit that this socket must never be the *sole* transport for a
 * fact that changes while work is in flight: it "has no ack, no sequence number and
 * no replay", and `connected` flips false only on `onclose`, so a half-open socket
 * reads as healthy while delivering nothing.
 *
 * A neighbour relationship changes for exactly one reason — a new run appears, which
 * turns the newest run's `newer` from `null` into a run — and that is precisely a
 * thing that happens while work is in flight. Under socket-only refresh, an operator
 * watching the newest run would sit on a disabled "Next Run" until they refocused
 * the tab. So there is a flat background poll as the bounded backstop that record
 * prescribes.
 *
 * `EXECUTIONS_BACKGROUND_POLL_MS` is **imported, not re-declared**: it is the same
 * 30 s cadence the run-history list uses when polling is a backstop rather than the
 * primary transport, and a second copy of the number would be a second cadence
 * authority. Importing it is not an edit to `LGR-e800696-1`'s scope — that record
 * fences `EXECUTIONS_POLL_MATRIX`, `computeExecutionsRefetchInterval`'s signature and
 * `usePipelineExecutions`' `refetchInterval` callback, none of which is touched here.
 *
 * A **flat** interval rather than the matrix, and that is a narrowing rather than a
 * disagreement. The matrix's four cells exist because the run-history list has a
 * work-in-flight axis: rows change status continuously while a run executes. This
 * query has no such axis — three `Limit=1` reads, answering a question that changes
 * only on run creation — so the matrix's `connected + idle ⇒ false` cell would be
 * the one thing that record rejects: the socket as sole transport. 30 s always is
 * the cheaper and stricter reading.
 */
export function useRunNeighbours(pipelineKey: string | undefined, runId: string | undefined) {
  return useQuery({
    queryKey: executionKeys.neighbours(pipelineKey!, runId!),
    queryFn: () =>
      apiClient.get<RunNeighbours>(
        `/pipeline/${encodeURIComponent(pipelineKey!)}/run/${encodeURIComponent(runId!)}/neighbours`,
      ),
    enabled: !!pipelineKey && !!runId,
    staleTime: EXECUTIONS_BACKGROUND_POLL_MS,
    refetchInterval: EXECUTIONS_BACKGROUND_POLL_MS,
    refetchIntervalInBackground: false,
  });
}
