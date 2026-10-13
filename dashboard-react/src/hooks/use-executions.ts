// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { executionKeys } from './query-keys';
import { useWebSocket } from './useWebSocket';
import { isTerminalStatus } from '../utils/formatting';
import { isDeferredQueuedFile } from '../utils/queued-file-state';
import type { LoadOutcome } from '../types';

interface PipelineExecution {
  /**
   * The Step Functions execution *name*, not a run id (measured:
   * `file-customer_data_4_csv-20260818T174815-40c55e5cd0c7`). Optional because
   * the producer omits it on a large minority of rows (p51 R4.1) — measured live
   * 2026-08-25: 44 of 61 rows carry it, 43 of 50 `run-` rows, and none of the
   * `skipped-`, `file-`, `e2e-` or `retry-` rows.
   *
   * This corrects `LGR-8a4fefa-2`, which recorded it as never emitted.
   *
   * **No app code reads this field.** It was previously a fallback behind
   * `run_id` at three `ExecutionsPage` sites; p51 C5 deleted all three, because
   * `run_id` is the executions table's sort key so the fallback could not fire,
   * and because navigating to an execution *name* would route to a run that does
   * not exist. Do not reintroduce it as a run identifier.
   */
  execution_id?: string;
  run_id?: string;
  pipeline_key?: string;
  pipeline_id?: string;
  file_name?: string;
  file_key?: string;
  status: string;
  started_at: string;
  completed_at?: string;
  current_step?: string;
  ai_model?: string;
  ai_model_used?: string;
  schema_version?: number;
  steps?: Array<{ step_name: string; status: string }>;
  steps_status?: Record<string, string>;
  pending_approval_id?: string;
  pending_approval_type?: string;
  schema_hash?: string;
  trigger?: string;
  retry_source_run_id?: string;
  /**
   * The run this one re-runs, for any rerun trigger. Propagated from the
   * run's `file_validation` identity row by `execution_updater` (p30 R14.3).
   * `retry_source_run_id` is the dashboard retry path's own spelling of the
   * same idea — it writes its execution row directly, so both exist.
   */
  original_run_id?: string;
  /**
   * Self-healing outcome for THIS run, denormalized onto the execution row by
   * `execution_updater` from the agent's outcome record (p30 R14.3). Carries
   * the agent's `rerun_status` verbatim — see `utils/self-healing.ts` for why
   * it is not reduced to a boolean.
   */
  self_healing_status?: string;
  self_healing_remediation?: string;
  self_healing_error_class?: string;
  /**
   * The heal re-run's real run_id, known only once the re-run terminates. The
   * agent's `rerun_id` (`{original}-heal`) is lineage only and is not
   * navigable — `file_validation` mints a fresh run_id for the re-run.
   */
  self_healing_linked_run_id?: string;
  self_healing_skip_reason?: string;
  /**
   * Skipped_Run_Record fields (p30 R10.1/R10.2), present only on rows with
   * `status === 'SKIPPED'`. `skip_reason` is the discriminator across all skip
   * kinds — duplicate content, global concurrency, pipeline-lock contention —
   * written by `pipeline_trigger`; `utils/skip-reason.ts` maps it to a badge.
   *
   * `skip_count` is how many scheduled fires the row stands for: contention
   * skips of the same pipeline for the same reason collapse into one record per
   * window, so a row can represent twelve misses rather than one. Absent on
   * duplicate-content skips and on rows written before the counter existed.
   */
  skip_reason?: string;
  skip_detail?: string;
  skip_count?: number;
  /**
   * What reached the target on this run (p37 R1.7, R1.9). Derived on the read
   * path from the run's `(run_id, "load_metrics")` state row and attached to
   * **every** row by `get_pipeline_executions` — never omitted, and
   * `not_established` rather than absent when no counts row exists
   * (`pipelines_runs.py:271`, `:281`).
   *
   * Declared optional anyway: a response cached before the field shipped, and
   * every test fixture that predates it, carries no `load_outcome`.
   * `LoadOutcomeIndicator` renders nothing for both, which is the same thing it
   * renders for `not_established` — see that component for why silence is the
   * correct reading of "we do not know".
   */
  load_outcome?: LoadOutcome;
}

interface QueuedFile {
  file_name: string;
  source_key: string;
  queued_at: string;
  status: string;
}

/**
 * One entry of `lost_files` — a file whose upload was dead-lettered and will never
 * be processed (p54 R10.3, `pipelines_runs._lost_files`).
 *
 * **Not a `QueuedFile` and not an execution.** It is the *durable* statement of a
 * loss, from a keyed read of the placeholder namespace, so it is present for as long
 * as the record lives (30 days) rather than only while the row sits on page 1 of a
 * newest-first index — about an hour on an active pipeline, which is what
 * `queued_files` alone could promise.
 *
 * **There is deliberately no `run_id`.** The row has a real one, but
 * `get_run_details` 404s on it (a placeholder carries no state rows at all), so
 * publishing it would hand this surface something that looks navigable and is not —
 * p48 O2 and p54 do-NOT 3 / do-NOT 15. `source_key` is the stable identity: it is
 * unique per file within a pipeline, because the row's key is derived from it. Use it
 * as a list key; do not build a link out of it.
 */
interface LostFile {
  file_name: string;
  source_key: string;
  /**
   * When the loss was **recorded**, not when the file was uploaded. The original
   * queue instant is not recoverable from a dead-lettered message, and the recorder
   * declines to invent one — see `queue_loss_recorder.record_lost_file`.
   */
  lost_at: string;
  /** The reason, in the operator's terms, from the producer. Rendered as words. */
  lost_reason: string;
}

/**
 * The `pagination` block of `GET /pipeline/{key}/executions`, exactly as
 * `get_pipeline_executions` builds it (`pipelines_runs.py:778-801` on the success
 * arm, `:811-843` on the degrade arm — the two arms emit the same eight keys, so
 * a client never branches on which one answered).
 *
 * Declared as a NAMED sibling interface rather than an inline object literal, and
 * that is a constraint rather than a style choice.
 * `tests/contracts/api_responses/helpers.py` is the shared frontend-type oracle;
 * `LGR-99050e6-5` extracted it precisely so endpoints would not each grow their
 * own parser, and `LGR-cd58706-3` records that reusing it is *mandatory, not
 * stylistic* (a local copy there would have been the eighth parser and the first
 * divergence). Its `read_interface` requires every line inside an interface to
 * match `field[?]: type;` and **asserts** on anything else — so a multi-line
 * inline `pagination: { … }` literal would make the contract test in task 4.2
 * unwritable. This spelling parses, and 4.2 hands the name to `object_types`.
 */
interface ExecutionsPagination {
  /**
   * A page **label**, not an offset. DynamoDB has no offset addressing, so under
   * p48 C2 Arm A this rides inside the cursor and is forced to `1` when the
   * request carried none. Do not compute a jump target from it.
   */
  page: number;
  /** Echoed back from the request. */
  page_size: number;
  /**
   * Rows in the pipeline's partition — the size of **the set being paged**, not
   * this page's length after filtering (R2.2). Exact, unless `partial_counts`
   * names it, in which case it is a floor.
   */
  total_count: number;
  /**
   * `ceil(total_count / page_size)` (R2.3), and `0` for an empty set on both
   * arms. Marked in `partial_counts` together with `total_count`, because a
   * derived value is a floor whenever its input is.
   */
  total_pages: number;
  /**
   * "a cursor was returned" — the same fact as `next_cursor !== null`, derived
   * from it at the producer so the two cannot drift. NOT the frontend's old
   * `has_more`; see {@link PaginatedExecutions}.
   */
  has_next: boolean;
  /** `page > 1`. The backend carries this; the old flat shape had no spelling for it. */
  has_prev: boolean;
  /**
   * Opaque token for the page after this one — ALWAYS present, `null` on the last
   * page rather than omitted, so the envelope's key set stays assertable. Roughly
   * 199 characters of base64url; treat it as opaque and do not parse it.
   */
  next_cursor: string | null;
  /**
   * Which of the numbers above a bound truncated: `[]` when both are totals,
   * otherwise `["total_count", "total_pages"]`. Always present rather than
   * omitted when empty (`LGR-9a35b2d-3`, `LGR-b2f79ef-14`) — an unmarked
   * "could-not-check" reported as "checked and found none" is the defect those
   * records exist to prevent.
   */
  partial_counts: string[];
}

/**
 * `GET /pipeline/{key}/executions`.
 *
 * p48 C4 **Arm B**: the producer's nested envelope stays and this type is
 * corrected to read it. Arm A — flattening the backend to match the four flat
 * fields this interface used to declare — was rejected at task 1.1 on measured
 * grounds rather than on taste: three test files already assert the nested shape
 * through two independent mechanisms
 * (`test_executions_response_contract.py:153`, `:156`, `:378-388`;
 * `tests/contracts/api_responses/schemas.py:158-180`'s
 * `PAGINATED_EXECUTIONS_SCHEMA` with `required: ["executions", "pagination"]`;
 * and `test_api_contract_props.py:194` validating the same handler against that
 * schema). Flattening would have broken all three in order to match the newer,
 * never-exercised artifact — which is this type.
 *
 * **`has_more` is removed, not aliased.** It was never the backend's spelling:
 * the wire carries `has_next` *and* `has_prev`, which is strictly more
 * information. Every reader was enumerated before removing it, and there are no
 * app-code readers at all — `ExecutionsPage.tsx:249` names it only in a comment
 * explaining that the pager is deliberately *not* gated on it, and
 * `ListPagination.tsx:11-15` records the bug that gating on a per-page `has_more`
 * caused (the pager unmounted on the last page, stranding the user). Keeping it
 * as an alias would have re-introduced a second spelling of a fact the producer
 * does not send, which is the shape of the defect R3.1 opened on.
 *
 * `executions` and `queued_files` stay top level in both shapes and were read
 * correctly all along; only the pagination block diverged. R3.3 depends on that:
 * `get_queued_files` (`pipelines_runs.py:458-470`) reads `queued_files` off this
 * same body and keeps working unchanged.
 */
interface PaginatedExecutions {
  executions: PipelineExecution[];
  queued_files: QueuedFile[];
  /**
   * Every Lost_File on record for this pipeline (p54 R10.3) — always present, `[]`
   * when nothing was lost, and identical on every page and under every `?status=`
   * because the read behind it is keyed rather than page-scoped.
   *
   * Declared **required**, like `queued_files` and unlike `PipelineExecution
   * .load_outcome`: the producer emits it on both response arms, so a reader never
   * has to branch on which arm answered. Read sites still spell `?? []` for a
   * response cached before the field shipped.
   *
   * This is a fourth top-level field, which
   * `tests/contracts/api_responses/test_executions_envelope_contract.py` guards with
   * an exact set equality whose own comment says a fourth "must fail here rather than
   * pass unnoticed". That guard was amended in place, with the reason at the
   * assertion, which is this repo's convention for a deliberate reversal
   * (`LGR-e59edd7-5`, `LGR-9e76249-10`) — not deleted.
   */
  lost_files: LostFile[];
  pagination: ExecutionsPagination;
}

/**
 * Poll cadence (ms) while work is in flight AND the socket cannot report it.
 * This is the "polling is the only transport" cadence.
 */
export const EXECUTIONS_ACTIVE_POLL_MS = 8_000;

/**
 * Slow cadence (ms) for the two cells where polling is a backstop rather than
 * the primary transport (p47 R14.1, R14.2):
 *  - connected + work in flight — the socket is primary; the poll only has to
 *    catch a frame the socket dropped; and
 *  - disconnected + nothing in flight — nothing is moving, but a *new* run can
 *    still start (an S3 upload or a schedule), and with the socket down there
 *    is no other way to hear about it.
 *
 * 30 s is the cadence `use-run-detail.ts` already uses for a settled run and
 * matches the provider's `MAX_BACKOFF_MS` reconnect ceiling, so a disconnected
 * tab polls at roughly the rate at which it is retrying the socket.
 */
export const EXECUTIONS_BACKGROUND_POLL_MS = 30_000;

/**
 * The (transport × work) cadence matrix, written out so all four cells are
 * visible at once rather than implied by nested conditionals (p47 R14).
 *
 * `false` is React Query's "stop polling". So are `0` and `undefined`
 * (`queryObserver.cjs:430` coalesces `undefined` to `false`; `:435` returns
 * early on `0`) — which is why every polling cell below is a positive number
 * and the single stop cell is spelled `false` explicitly.
 */
const EXECUTIONS_POLL_MATRIX: Record<
  'connected' | 'disconnected',
  Record<'work' | 'idle', number | false>
> = {
  // R14.1 — the socket is up and reporting, so the poll backs off to a
  // safety net. NOT stopped: `WebSocketProvider` has no ack, no sequence
  // number and no replay, `handleMessage` drops a malformed frame with a
  // `console.warn`, and `connected` only flips false on `onclose` — so a
  // half-open socket reads as healthy while delivering nothing. Stopping here
  // would make a lossy transport the sole source of truth for a running job.
  connected: { work: EXECUTIONS_BACKGROUND_POLL_MS, idle: false },
  // R14.2 — while the socket is down, polling never stops. The bottom-right
  // cell is the defect this replaces: disconnected + everything terminal used
  // to return `false`, so the poll had stopped and the socket meant to replace
  // it was down. That is silence, not a fallback.
  disconnected: {
    work: EXECUTIONS_ACTIVE_POLL_MS,
    idle: EXECUTIONS_BACKGROUND_POLL_MS,
  },
};

/**
 * Adaptive polling as a PURE FUNCTION of query state plus transport health,
 * fed straight to React Query's native `refetchInterval`.
 *
 * P29 R11.3 — why it is a pure function at all. The pre-P29 hook kept a
 * per-instance `useState` interval plus a `useRef` response-hash counter and
 * mutated that state from inside `queryFn`. Because every consumer of the same
 * query key got its own interval state, those copies fought the single shared
 * React Query cache (one mounted consumer could hold the cache on the fast
 * cadence while another sat on the slow one).
 *
 * p47 R14 — why it also takes `connected`. Keying on work state alone breaks in
 * both directions: connected + work in flight had poll and socket both fetching,
 * and disconnected + all terminal produced **no updates at all**. The cadence
 * now comes from {@link EXECUTIONS_POLL_MATRIX} over (transport × work).
 *
 * `connected` arrives as an ARGUMENT, not by reading the WebSocket context from
 * in here (R14.4). That keeps the function directly testable without a provider
 * and keeps every call site's transport source visible at the call site. It is
 * deliberately required rather than defaulted — a default would silently
 * reinstate the transport-blind behaviour at any call site that forgot it.
 *
 * `undefined` data (initial load) polls at the active cadence regardless of
 * transport: work state is not yet known, so neither R14.1 nor R14.2 has a
 * determined antecedent, and the first settled response should arrive promptly
 * to decide.
 *
 * Terminal detection reuses {@link isTerminalStatus} so there is no second,
 * divergent terminal-status list (R14.3).
 */
export function computeExecutionsRefetchInterval(
  data: PaginatedExecutions | undefined,
  connected: boolean,
): number | false {
  if (!data) return EXECUTIONS_ACTIVE_POLL_MS;
  // p54 R9.3 — a **deferred** file is work in flight; a **lost** one is not.
  // This was `(data.queued_files?.length ?? 0) > 0`, which was correct while
  // `_split_derive_and_filter` published `'queued'` unconditionally. Now that the
  // array can carry a Lost_File, array length would hold `workInFlight` true —
  // and therefore the fast cadence — **forever**, over a file that will never
  // move.
  //
  // The fix is the predicate's INPUT, not the matrix: `EXECUTIONS_POLL_MATRIX`,
  // this function's signature and every `staleTime` are byte-identical, because
  // `LGR-e800696-1` (**template / ratified**) makes the 2x2 over
  // (transport × work in flight) deliberate and p54 do-NOT 17 / R8.14 forbid
  // retuning it. A lost file is correctly **not** work in flight, so the cell it
  // selects is right; only the classification of the row was wrong.
  const hasQueued = (data.queued_files ?? []).some((q) => isDeferredQueuedFile(q.status));
  const hasActive = (data.executions ?? []).some((e) => !isTerminalStatus(e.status));
  const workInFlight = hasActive || hasQueued;
  return EXECUTIONS_POLL_MATRIX[connected ? 'connected' : 'disconnected'][
    workInFlight ? 'work' : 'idle'
  ];
}

/**
 * Rows per run-history page, and the ONE place the number is written (p48 R10.1,
 * task 5.1).
 *
 * It used to be three copies of the literal `10` — this request's `page_size`,
 * and `ExecutionsPage.tsx`'s pager gate and `pagesCount` — which is two more
 * authorities than a page size can have. The two in `ExecutionsPage` are gone
 * rather than re-pointed here: under C2 Arm A that page reads `pagination.page`
 * and `pagination.total_pages` **off the wire** instead of dividing a count by a
 * page size, so it no longer needs to know the number at all. This is therefore
 * the request's own parameter, exported so a test can assert what was sent
 * without transcribing it.
 *
 * Note the asymmetry with the response: the *authoritative* page size for
 * anything displayed is `pagination.page_size`, which the producer echoes back.
 * Read that, not this, when interpreting a response — a server that clamps the
 * request would otherwise be invisible.
 */
export const EXECUTIONS_PAGE_SIZE = 10;

async function fetchPipelineExecutions(
  pipelineKey: string,
  page: number,
  cursor?: string | null,
  status?: string | null,
): Promise<PaginatedExecutions> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(EXECUTIONS_PAGE_SIZE),
  });
  // Omitted entirely rather than sent empty for page 1. `router.py:162` reads
  // `query_params.get("cursor") or None`, so `?cursor=` is already equivalent to
  // absent — but the handler answers a *malformed* cursor with a 400, and not
  // sending the parameter keeps "page 1" structurally incapable of tripping that
  // path.
  if (cursor) params.set('cursor', cursor);
  // Same treatment for the same reason (p48 C5, task 5.5). `router.py:191` reads
  // `query_params.get("status") or None`, so `?status=` already means absent, and
  // an **unrecognised** value is a 400 naming
  // `pipelines_runs._ACCEPTED_STATUS_FILTERS` — so omitting the parameter keeps
  // "unfiltered" structurally incapable of reaching that path.
  //
  // No client-side filtering anywhere in this module, and that is deliberate: the
  // server filters (task 4.3, after the `WAITING_APPROVAL` derivation), and a
  // second filtering authority here would disagree about scope — it would see one
  // page where the server walked many, which is precisely the misleading-scope
  // defect R4.3 forbids. This function stays a pass-through cast; the
  // characterization test `returns the producer body unchanged, adding and
  // renaming nothing` is what holds that.
  if (status) params.set('status', status);
  return apiClient.get<PaginatedExecutions>(
    `/pipeline/${encodeURIComponent(pipelineKey)}/executions?${params}`,
  );
}

/**
 * @param page  The page **label** to send and to key the cache by. Under C2
 *   Arm A the server ignores it as a seek input and derives the label from the
 *   cursor (`pipelines_runs.py:700-714`), so a caller must keep it in step with
 *   `cursor` — `ExecutionsPage` does, by deriving both from one cursor stack.
 * @param cursor  The previous page's `pagination.next_cursor`. Omit for page 1;
 *   there is no cursor for the first page and none is needed.
 * @param status  A **derived** status to filter on (p48 C5). Omit for unfiltered.
 *   Only values in the server's accepted set may be sent — `SKIPPED`,
 *   `SUCCEEDED`, `FAILED`, `RUNNING`, `WAITING_APPROVAL`, `CANCELLED`,
 *   `CANCELLING`, `IN_PROGRESS` — anything else is a 400. `QUEUED` is refused on purpose:
 *   those rows are split into `queued_files` **before** the filter runs, so the
 *   `executions` array provably cannot contain a match
 *   (`pipelines_runs._QUEUED_STATUS_FILTER_MESSAGE`).
 *
 *   Callers must reset their cursor position when this changes. A cursor stays
 *   *redeemable* across a filter change — `run_cursor.decode_cursor` binds it to
 *   the pipeline, not to the filter — so the request would succeed while
 *   resuming a filter walk from a position the new filter never walked to.
 *   `ExecutionsPage` does this (`setCursorStack([])` beside the filter's setter).
 */
export function usePipelineExecutions(
  pipelineKey: string | undefined,
  page: number = 1,
  cursor?: string | null,
  status?: string | null,
) {
  // `isConnected` is the shared socket's state from the app-wide
  // WebSocketProvider, so it is the same value for every consumer of this query
  // key — the interval stays consistent across mounted consumers even though it
  // now depends on transport (p47 R14.4). It is accurate even when `enabled` is
  // false: the provider owns the socket, this hook only registers a topic on it.
  const { isConnected } = useWebSocket({
    pipelineIds: pipelineKey ? [pipelineKey] : undefined,
    enabled: !!pipelineKey,
  });

  return useQuery({
    queryKey: executionKeys.list(pipelineKey!, page, cursor, status),
    queryFn: () => fetchPipelineExecutions(pipelineKey!, page, cursor, status),
    enabled: !!pipelineKey,
    staleTime: 3_000,
    // Adaptive polling shared via the query cache (P29 R11.3): the cadence is a
    // pure function of the cached data plus transport health, so every consumer
    // of this query key agrees on the interval instead of each holding its own
    // useState copy.
    refetchInterval: (query) =>
      computeExecutionsRefetchInterval(query.state.data, isConnected),
    refetchIntervalInBackground: false,
  });
}
