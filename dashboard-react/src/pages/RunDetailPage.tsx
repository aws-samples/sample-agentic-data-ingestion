// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useMemo, useState, useEffect, useRef } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import Tabs from '@cloudscape-design/components/tabs';
import { useRunDetail, useCancelRun, useRetryRun, useRunTraces, useSelfHealingActivity, useQualityReport, useRunNeighbours, usePipelineDetail, useRegenerateMappings, useEditPipeline, useApprovalAction, useMappingRegenPolling, qualityReportKeys } from '../hooks';
import { cancelFailureMessage, cancelOutcomeToast } from '../hooks/use-cancel-run';
import { useQueryClient } from '@tanstack/react-query';
import type { AgentTrace, MappingData, RunAttempt, RunDetail, UnmappedFieldsPayload } from '../types';
import { useToast } from '../contexts/ToastContext';
import { getRetryStepOptions, PIPELINE_STEP_ORDER } from '../constants';
import { normalizeStepName, sameStep } from '../utils/step-names';
import { describeSkip, type SkipIndicator } from '../utils/skip-reason';
import type { SkippedRunDescriptor, SkippedRunNavState } from '../utils/skipped-run-descriptor';
import { unsourcedTargetFields } from '../utils/unsourced-target-fields';
import { approvalMappingModel } from '../utils/approval-mapping-model';
import { formatStepName, formatTimestamp, isActiveRunStatus } from '../utils/formatting';
import StepFlowTab from '../components/run-detail/StepFlowTab';
import AiTracesTab from '../components/run-detail/AiTracesTab';
import ApprovalsTab from '../components/run-detail/ApprovalsTab';
import SchemaTab from '../components/run-detail/SchemaTab';
import CodeTab from '../components/run-detail/CodeTab';
import DataQualityTab from '../components/run-detail/DataQualityTab';
import RunDetailHeader from '../components/run-detail/RunDetailHeader';
import RunMetadataPanel from '../components/run-detail/RunMetadataPanel';
import LoadOutcomeLabel from '../components/LoadOutcomeLabel';

/* ────────────────────────────────────────────────────────────────────────
   The retried banner (p38 R2.2, R2.3)

   Under the continuation model a retry executes and reports under the
   ORIGINAL run's id, so there is no second run id and no second page. The
   price of that is the one this banner pays down: a bookmarked original-run
   URL now resolves to a page describing the retry. The banner is the
   explanation of that shared identity — which is why its load-bearing line is
   the last one, stating that the steps and status below describe the current
   attempt.

   Deliberately NO link anywhere in it. Under one shared id the only candidate
   related-run id is the run's own, so a link would resolve to the page you are
   already on — do-NOT 5 / R5.3, governed by `LGR-453f12d-5` (**template /
   ratified**): a forward link renders only from an id that resolves elsewhere.
   ──────────────────────────────────────────────────────────────────────── */

/** A run (or attempt) status that contradicts one of its own step statuses. */
interface StatusDisagreement {
  step: string;
  ownStatus: string;
  stepStatus: string;
}

function isSuccessRunStatus(status: string | undefined): boolean {
  const v = (status ?? '').toLowerCase();
  return v === 'succeeded' || v === 'completed' || v === 'success';
}

function isFailedStepStatus(status: string | undefined): boolean {
  const v = (status ?? '').toLowerCase();
  return v === 'failed' || v === 'error';
}

/**
 * Every step whose recorded status contradicts the run status it sits under
 * (R2.3). The observed live instance: a run reading `SUCCEEDED` whose own
 * `steps_status.test_code` still reads `FAILED`, because the retry overwrote
 * the row's status in place and nothing overwrote the step entry.
 *
 * **Narrowed to the success-over-failure direction on purpose.** The opposite
 * shape — a `FAILED` run whose steps all read success — is a legitimate state
 * rather than a contradiction: `LGR-9e76249-6` (**template / ratified**)
 * records that the RecordFailure sentinel is the only carrier of a run's
 * terminal `FAILED` status whenever the failing step never wrote a row of its
 * own (a Lambda timeout, `States.Timeout` at an approval gate, a Glue
 * failure). Reporting that as a disagreement would claim something the data
 * does not support.
 */
function findDisagreements(
  ownStatus: string | undefined,
  stepStatuses: ReadonlyArray<readonly [string, string | undefined]>,
): StatusDisagreement[] {
  const own = ownStatus ?? '';
  if (!isSuccessRunStatus(own)) return [];
  const out: StatusDisagreement[] = [];
  for (const [step, stepStatus] of stepStatuses) {
    if (step && isFailedStepStatus(stepStatus)) {
      out.push({ step, ownStatus: own, stepStatus: stepStatus as string });
    }
  }
  return out;
}

interface RetryBannerModel {
  /** Total attempts including the current one, or `null` when not recorded. */
  attemptCount: number | null;
  /** The displaced attempts, oldest first. Empty when no attribution exists. */
  priorAttempts: RunAttempt[];
  /** True when the run was retried but no attempt record was ever kept. */
  attributionMissing: boolean;
  /** Contradictions on the run's own row — the pre-p38 case's only evidence. */
  runDisagreements: StatusDisagreement[];
}

/**
 * The banner's view model, or `null` when nothing says this run was retried.
 *
 * Four independent tells, because the fields available differ by era:
 *   - a populated `attempts` list (a p38-era retry),
 *   - an `attempt_count` above 1 (same, and it survives the five-node cap),
 *   - a non-empty `retry_source_run_id` (the row was touched by a retry),
 *   - a status/steps contradiction on the row itself.
 *
 * **The last one is the common case, not a fallback.** Every retried run that
 * predates p38 carries no attempt attribution at all (§5 delta D2), so the
 * overwritten row is the only evidence that a retry happened. A banner built
 * only on `attempts` would be correct for the rare case and absent for the
 * frequent one.
 *
 * An absent `attempts` and an empty one are both "no attribution" here, and
 * neither renders as "0 attempts": `attemptCount` stays `null` rather than
 * falling to zero (`LGR-99050e6-2`, **template / ratified**).
 */
function buildRetryBanner(runDetail: RunDetail): RetryBannerModel | null {
  const priorAttempts = (runDetail.attempts ?? []).filter(
    (a): a is RunAttempt => !!a && typeof a === 'object',
  );
  const runDisagreements = findDisagreements(
    runDetail.status,
    (runDetail.steps ?? []).map((s) => [s.step_name, s.status] as const),
  );
  const countFromRow = typeof runDetail.attempt_count === 'number' ? runDetail.attempt_count : 0;
  const countFromNodes = priorAttempts.reduce(
    (max, a) => (typeof a.attempt === 'number' && a.attempt + 1 > max ? a.attempt + 1 : max),
    0,
  );

  const retried =
    priorAttempts.length > 0 ||
    countFromRow > 1 ||
    !!runDetail.retry_source_run_id ||
    runDisagreements.length > 0;
  if (!retried) return null;

  // `|| null` is what keeps an unrecorded count from rendering as zero.
  const attemptCount = Math.max(countFromRow, countFromNodes) || null;

  return {
    attemptCount,
    priorAttempts,
    attributionMissing: priorAttempts.length === 0,
    runDisagreements,
  };
}

/**
 * What one displaced attempt did, and where. Every field but `attempt` can be
 * absent from the node, so each clause is composed only when its field is
 * present — nothing is defaulted and nothing renders as `undefined`.
 */
function describeAttempt(attempt: RunAttempt): string {
  const n = typeof attempt.attempt === 'number' ? attempt.attempt : null;
  const head = n === null ? 'An earlier attempt' : `Attempt ${n}`;
  const step = attempt.failed_step ? formatStepName(attempt.failed_step) : '';

  // completed_at is preferred and genuinely optional on the node; started_at
  // is the fallback. With neither, the sentence simply carries no time.
  const rawTime = attempt.completed_at || attempt.started_at || '';
  const formatted = rawTime ? formatTimestamp(rawTime) : '';
  const when = formatted && formatted !== 'Unknown' ? ` on ${formatted}` : '';

  if (attempt.status) {
    const where = step ? ` at ${step}` : '';
    return `${head} ended ${attempt.status}${where}${when}.`;
  }
  const from = step ? ` from ${step}` : '';
  return `${head} was retried${from}${when}, and its own status was not recorded.`;
}

/**
 * One Approval_Payload → one `MappingData`, or `null` when it proposes nothing.
 *
 * ONE DERIVATION, TWO READERS (p54 R1.3). Lifted out of the step-walking
 * `useMemo` below without changing a single expression, so that the reuse path
 * can source the same shape from a DIFFERENT payload — the reused mapping's own,
 * carried on the `schema_mapping` approval record — without spelling the shape a
 * second time. `LGR-28b4d75-1`, `LGR-28b4d75-5` and `LGR-9e76249-2` (all
 * template / ratified) each reject a second derivation of one shape in a second
 * place; `LGR-7561acb-1` applies the same rule to the `unmapped_target` join in
 * particular, keeping "the count of tolerant readers at one per side". The
 * backend made exactly this move one layer down in the same spec —
 * `_schema_mapping_payload` was lifted out of `get_approval_data`'s branch body
 * so its reuse arm could project a different row.
 *
 * `approval` is the payload itself, so the CALLER decides which one is projected:
 * the run's own `output_data.approval` on the normal path, the reused mapping's
 * approval `details` on the reuse path.
 */
function mappingDataFromApprovalPayload(
  approval: Record<string, unknown>,
  schemaHash: string,
  aiModelUsed: string | undefined,
): MappingData | null {
  const mappings = (approval.proposed_mappings as Array<Record<string, unknown>>) ?? [];
  // p45 R4.4 — the nested payload is named at the point of use, exactly as
  // `approval` itself is coerced to `Record<string, unknown>` by the caller.
  // `UnmappedFieldsPayload` (`types.ts`) doc-comments
  // `schema_mapping/lambda_function.py:1391-1397` as its source of truth and
  // is guarded by a contract test that fails on a rename of either
  // Unsourced_Key (R6.4/R6.5).
  const unmappedFields = (approval.unmapped_fields as UnmappedFieldsPayload) ?? {};
  // Source stays bare names and this expression is unchanged (do-NOT 19):
  // both producers write names there, so an object shape would be invented.
  const unmappedSource = ((unmappedFields.source ?? []) as Array<string | Record<string, unknown>>).map(
    f => typeof f === 'string' ? f : (f as Record<string, unknown>).field as string
  );
  // p45 R4.4 — the target list stops discarding the Unmapped_Entry object.
  // This replaces a `.map(f => typeof f === 'string' ? f : f.field as string)`
  // that threw away `default_value` and `reason` one line before the
  // component that needs them.
  //
  // Same payload as the approval panel: `store_mapping_result`
  // (`schema_mapping/lambda_function.py:1533`) writes the approval object
  // onto the `schema_mapping` state row, so **both Unsourced_Keys are
  // available here** — required-ness comes from `target_required` only, and
  // the roster and its order come from `target_unsourced` as received
  // (R1.3, R1.5, R1.7, do-NOT 2, do-NOT 13). `unsourcedTargetFields` owns
  // the whole join and no surface re-derives it (design §4.8's four steps).
  // Where the keys are absent — the common case until p37 deploys (C15) —
  // it falls back to `target`'s own names with every entry
  // `not_established`, so nothing is presented as optional (R1.4, R2.6).
  const unmappedTarget = unsourcedTargetFields(unmappedFields);
  // NOT relaxed, deliberately. This gates the whole `MappingData`
  // construction, so a mapping in which *every* target field is unsourced
  // leaves `runMappingData` null and `run-detail/SchemaTab.tsx`'s absence arm
  // renders "No mapping data available for this run." That is the exact case p45
  // exists for and it is the one surface that shows nothing — but relaxing
  // the guard changes **which runs render a mapping panel at all**, a
  // rendering-behaviour change no p45 criterion authorises (design §4.11,
  // §5). Recorded here as a known follow-on so it is not rediscovered; it
  // should be its own small spec.
  if (mappings.length === 0) return null;
  return {
    mappings: mappings.map(m => ({
      source_field: (m.source_field as string) ?? '',
      target_field: (m.target_field as string) ?? '',
      confidence: (m.confidence as number) ?? undefined,
      transformation: (m.transformation as string) ?? undefined,
      hint: (m.hint as string) ?? undefined,
    })),
    unmapped_source: unmappedSource,
    unmapped_target: unmappedTarget,
    schema_hash: schemaHash,
    version_index: 0,
    ai_model_used: aiModelUsed,
  };
}

/** R2.3, in words: both values, with neither chosen over the other. */
function describeDisagreement(d: StatusDisagreement, subject: string): string {
  return (
    `${subject} records status ${d.ownStatus}, while its own ${formatStepName(d.step)} ` +
    `step records ${d.stepStatus}. Both are shown because no record establishes ` +
    `which attempt each value came from.`
  );
}

/* ────────────────────────────────────────────────────────────────────────
   The honest skip page (p54 R6, design 4.6 — arm B2)

   A skip row has no state-table rows, so `get_run_details` answers 404 for its
   run id, `fetchRunDetail` maps 404 to `null` (`use-run-detail.ts:29-30`), and
   the branch below this one denied the run existed — about a row the list the
   operator clicked from had just read to them. The backend repair is fenced
   (R6.3 / R8.2, p38 do-NOT 6 / p38 R1.4: a tolerant 200 "would convert a
   diagnosable 404 into a run that appears to exist and is empty"), so arm B2
   recognises the 404 in the frontend **beside the row it already holds** — the
   descriptor the two run lists attach to the navigation
   (`utils/skipped-run-descriptor.ts`).

   This is `LGR-2bcdd0d-3`'s ratified precedent one surface over: the
   data-preview 404 on this same skip row renders its own worded statement,
   because "a 404 … means 'there is nothing to preview here', not 'the read
   failed'", and because "no words at all leaves an empty region
   indistinguishable from a panel that failed to load". Here the page made a
   FALSE claim rather than none, and the repair is the same shape — a worded
   statement of what actually happened. That record also rejected a Retry, on
   the ground this branch inherits: re-requesting an id that correctly 404s
   cannot succeed (p38 R4.4). `Back to Pipeline` is kept.
   ──────────────────────────────────────────────────────────────────────── */

/**
 * What this page has, and does not have, about a skipped run.
 *
 * The 404 is the whole evidence: no state row for this id means no steps, no
 * logs, no results — so the sentence claims exactly that and nothing about
 * *why* beyond it, which is {@link describeSkip}'s to say. Deliberately makes no
 * claim about what a load "would have" done: two of the three reasons never
 * started a run and the unrecognised-reason arm is not ours to interpret.
 */
export const SKIPPED_RUN_NO_EXECUTION_STATEMENT =
  'This run was skipped, so no execution record was created for it — there are no steps, logs or results to show.';

/**
 * Labels for the two optional descriptor fields. Both render **only when the
 * row carried the field**, because the two writers genuinely differ and that
 * difference is not a gap to paper over: a duplicate skip
 * (`pipeline_trigger:225-244`) carries `file_name` and `previous_run_id`, while
 * a contention skip (`:488-505`) carries **no `previous_run_id` at all** and
 * `file_name` only where the trigger had a `source_key`. So this page never
 * promises a previous run — absence stays absence (`LGR-44d601e-1`).
 *
 * Flat, reason-neutral wording on purpose. "The run that already processed this
 * content" is true of a duplicate and an over-claim on any other reason, and
 * the reason arm that would need it already says so in full: `skip_detail`
 * names the previous run in a sentence, and {@link describeSkip} folds that in.
 *
 * Rendered as text, never as a link. p38 do-NOT 5 / `LGR-453f12d-5` forbid
 * linking to a related run id Run_Detail_Read cannot resolve, and whether this
 * one resolves is not established here.
 */
export const SKIPPED_RUN_FILE_LABEL = 'File recorded on this run';
export const SKIPPED_RUN_PREVIOUS_RUN_LABEL = 'Previous run recorded on this row';

/** A carried skip descriptor together with the one derivation of its copy. */
interface CarriedSkip {
  descriptor: SkippedRunDescriptor;
  skip: SkipIndicator;
}

/**
 * The skip descriptor a Run_List_Surface carried into this navigation, or `null`.
 *
 * `null` is the normal answer. A pasted URL, a bookmark, a deep link, a
 * cmd/ctrl/middle-click, and every non-skip row all arrive with no state — and
 * `onFollow` fires only on a plain left click — so the branch below is narrow by
 * construction and the 404 branch beside it stays reachable and unchanged
 * (R6.4, R8.5, p38 R4.1). That asymmetry is accepted with its reason in design
 * 4.6: reading the executions row here would either re-read page 1 only — the
 * shape `LGR-2bcdd0d-2` rejected by name — or add a fourth read path, and the
 * recorded fallback is to extend the neighbours endpoint instead.
 *
 * **The gate is {@link describeSkip}'s own**, not a second `=== 'SKIPPED'` test:
 * `buildSkippedRunNavState` attaches a descriptor exactly when
 * `describeSkip(...) !== null` (`skipped-run-descriptor.ts:138`), so asking the
 * same authority here means the list cannot attach a descriptor this page
 * refuses, and the badge below reads the same derivation the two lists render
 * (R6.2 and R7 share one authority and cannot drift). **Never the run id** —
 * two id shapes exist and one is `run-`-prefixed deliberately
 * (`LGR-21a1b5b-4`), so `status` + `skip_reason` is the discriminator, do-NOT 4.
 *
 * Untyped `unknown` in, because router state is whatever the history entry
 * carries: a bookmark restores it verbatim across a reload, so every field is
 * checked rather than trusted.
 */
function readCarriedSkip(state: unknown): CarriedSkip | null {
  if (typeof state !== 'object' || state === null) return null;
  // Read through `Partial<SkippedRunNavState>` so `tsc` binds the key name to the
  // interface the writers return: renaming `skippedRun` there fails HERE rather
  // than silently producing a page that never finds the descriptor. The value is
  // then widened back to `unknown`, because the declared field types are the
  // writer's promise and this side verifies them.
  const carried: unknown = (state as Partial<SkippedRunNavState>).skippedRun;
  if (typeof carried !== 'object' || carried === null) return null;
  const row = carried as Record<string, unknown>;

  // Each field is re-checked rather than trusted. `buildSkippedRunNavState`
  // writes strings or omits the key, but a history entry survives a reload and
  // is restored verbatim, so a non-string here must degrade to absence rather
  // than reach `describeSkip`'s `.trim()`.
  const str = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined);
  const status = str(row.status);
  if (status === undefined) return null;
  const skipReason = str(row.skip_reason);
  const skipDetail = str(row.skip_detail);
  const skip = describeSkip(status, skipReason, skipDetail);
  if (skip === null) return null;

  const descriptor: SkippedRunDescriptor = { status };
  const previousRunId = str(row.previous_run_id);
  const fileName = str(row.file_name);
  if (skipReason !== undefined) descriptor.skip_reason = skipReason;
  if (skipDetail !== undefined) descriptor.skip_detail = skipDetail;
  if (previousRunId !== undefined) descriptor.previous_run_id = previousRunId;
  if (fileName !== undefined) descriptor.file_name = fileName;
  return { descriptor, skip };
}

export default function RunDetailPage() {
  const { id: pipelineId, runId } = useParams<{ id: string; runId: string }>();
  const navigate = useNavigate();
  // The skip descriptor a Run_List_Surface carried in, if this navigation came
  // from one (p54 R6.1). Read unconditionally, above the early returns, because
  // it is a hook. No `useMemo`: five typeof checks and one pure call, nothing to
  // memoise — the same judgement `prevRunId` below records.
  const carriedSkip = readCarriedSkip(useLocation().state);
  const { data: runDetail, isLoading, isError, refetch, isWebSocketConnected } = useRunDetail(runId, pipelineId);
  const cancelRun = useCancelRun();
  const retryMutation = useRetryRun();
  const { showToast } = useToast();
  // Includes CANCELLING: traces keep updating while the Glue job stops.
  const runIsActive = isActiveRunStatus(runDetail?.status);
  const { data: tracesData, isLoading: tracesLoading, isError: tracesError, refetch: refetchTraces } = useRunTraces(runId, runIsActive);
  const { data: selfHealingActivity } = useSelfHealingActivity(runId);
  const { data: qualityReport, isError: qualityError, refetch: refetchQuality } = useQualityReport(runId, runDetail?.status === 'SUCCEEDED');
  const [activeTabId, setActiveTabId] = useState('step-flow');
  // Optimistic banner while an approval action is in flight / resolving. The
  // terminal 'approved'/'rejected' timed states are gone — refresh is
  // event-driven (mutation invalidation + WebSocket run_update).
  const [optimisticAction, setOptimisticAction] = useState<'approving' | 'rejecting' | null>(null);
  const approvalMutation = useApprovalAction();
  const [selectedRetryStep, setSelectedRetryStep] = useState<string | undefined>();

  // Compute retry step options and default to first failed step
  const retryStepOptions = useMemo(() => {
    if (runDetail?.status !== 'FAILED' || !runDetail.steps) return [];
    return getRetryStepOptions(runDetail.steps, PIPELINE_STEP_ORDER);
  }, [runDetail?.status, runDetail?.steps]);

  // Default selection: first failed step
  useEffect(() => {
    if (runDetail?.status === 'FAILED' && runDetail.steps) {
      const firstFailed = runDetail.steps.find((s) => {
        const st = (s.status ?? '').toLowerCase();
        return st === 'error' || st === 'failed';
      });
      if (firstFailed) {
        setSelectedRetryStep(firstFailed.step_name);
      }
    }
  }, [runDetail?.status, runDetail?.steps]);


  // Refetch quality report when run transitions to SUCCEEDED via WebSocket
  const queryClient = useQueryClient();
  const prevStatusRef = useRef(runDetail?.status);
  useEffect(() => {
    if (prevStatusRef.current !== runDetail?.status && runDetail?.status === 'SUCCEEDED' && runId) {
      queryClient.invalidateQueries({ queryKey: qualityReportKeys.run(runId) });
    }
    prevStatusRef.current = runDetail?.status;
  }, [runDetail?.status, runId, queryClient]);

  // Clear the optimistic approval banner once the run no longer has a pending
  // approval. Convergence is event-driven: the approval mutation invalidates
  // this run's query on success and WebSocket run_update messages invalidate via
  // the provider — replacing the old 1s/3s/6s/10s setTimeout refetch chains.
  useEffect(() => {
    const stillPending = (runDetail?.approvals ?? []).some((a) => a.status === 'pending');
    if (!stillPending) setOptimisticAction(null);
  }, [runDetail?.approvals]);
  // Fetch pipeline detail for ai_model and mapping_context
  const { data: pipelineDetail, refetch: refetchPipeline } = usePipelineDetail(pipelineId);
  const regenerateMapping = useRegenerateMappings();
  const editPipeline = useEditPipeline();
  const [waitingForRegen, setWaitingForRegen] = useState(false);

  // Mapping-regen polling (R7.1): effect-scoped so the interval is cleared on
  // unmount and stops when the pipeline's mapping_id changes (regeneration
  // complete), with a 60s safety-net cap — replacing the old orphaned inline
  // setInterval + blind 60s setTimeout in onRerunMapping below.
  useMappingRegenPolling({
    active: waitingForRegen,
    completionSignal: pipelineDetail?.mapping_id,
    onPoll: () => { refetch(); refetchPipeline(); },
    onStop: () => { setWaitingForRegen(false); refetch(); refetchPipeline(); },
  });

  // Prev/next navigation (p48 R10.2, task 5.2). Resolved by the SERVER from
  // `RunsByTimeIndex` — three keyed reads at `Limit=1` — rather than by fetching a
  // page of run history and doing index arithmetic on it. See
  // `use-run-neighbours.ts` for the direction convention and for why nothing here
  // may re-sort (R10.4: one ordering authority).
  const { data: runNeighbours } = useRunNeighbours(pipelineId, runId);

  // Extract step-specific data for Schema & Code tabs
  const { extractedSchema, runMappingData, runMappingId, codeVersionId } = useMemo(() => {
    const steps = runDetail?.steps ?? [];
    let _extractedSchema = '';
    let _mappingData: MappingData | null = null;
    let _mappingId = '';
    let _codeVersionId = '';

    for (const step of steps) {
      const s = step as unknown as Record<string, unknown>;
      if (step.step_name === 'schema_extract') {
        _extractedSchema = (s.extracted_schema as string) ?? '';
      }
      if (step.step_name === 'schema_mapping') {
        // The mapping record this run used (pipelines_runs.py:292). SchemaTab
        // compares it with the pipeline's current mapping_id to decide whether
        // editing the mappings here would edit what the pipeline still uses.
        _mappingId = (s.mapping_id as string) ?? '';
        // Extract mapping data from the step's output_data.approval payload.
        // The projection itself lives in `mappingDataFromApprovalPayload` above,
        // unchanged expression for expression, so the reuse path can project the
        // same shape from a different payload (p54 R1.3).
        const outputData = (s.output_data as Record<string, unknown>) ?? {};
        const approval = (outputData.approval as Record<string, unknown>) ?? {};
        // The model is the payload's own `generated_by`, so a fallback mapping
        // reads as "no model used" rather than as whatever the pipeline is set
        // to now. The pipeline's model stays the answer only for a row written
        // before payloads carried `generated_by` (P28 R3.5), as it always was.
        _mappingData = mappingDataFromApprovalPayload(
          approval,
          (s.schema_hash as string) ?? '',
          approvalMappingModel(approval) ?? pipelineDetail?.ai_model ?? undefined,
        );
      }
      // Canonical-space match (P28 task 14.2): covers new "create_code"/
      // "test_code" rows, legacy "CodeGeneration"/"CodeTesting" rows, and
      // the old "code_generation"/"code_testing" variants.
      if (sameStep(step.step_name, 'create_code')) {
        _codeVersionId = (s.code_version_id as string) ?? '';
      }
      if (!_codeVersionId && sameStep(step.step_name, 'test_code')) {
        _codeVersionId = (s.code_version_id as string) ?? '';
      }
    }
    return { extractedSchema: _extractedSchema, runMappingData: _mappingData, runMappingId: _mappingId, codeVersionId: _codeVersionId };
  }, [runDetail, pipelineDetail]);

  // The reused mapping's own fields, for a Reuse_Run's Schema tab (p54 R1.3).
  //
  // A Reuse_Run takes the `ReuseExistingMapping` Pass instead of the
  // `schema_mapping` Lambda, a Pass writes nothing to DynamoDB, so the walk above
  // finds no `schema_mapping` step and `runMappingData` is `null`. The mapping's
  // fields are nonetheless already on this page: p54 task 2.2 resolves the reused
  // mapping to its origin run and returns THAT run's `output_data.approval`
  // verbatim, and `get_run_details` parses the stored `approval_data` into
  // `approvals[].details` (`pipelines_runs.py`). So this is a projection of bytes
  // already fetched — no second request.
  //
  // GATED ON `reused_mapping === true`, not on `runMappingData === null`. That is
  // what keeps every non-reuse run byte-identical: a run whose own step proposed
  // an empty `proposed_mappings` still renders the existing absence sentence
  // rather than acquiring a mapping panel from its approval record.
  //
  // `details` is narrowed at the point of use and `Approval.details` stays
  // `Record<string, unknown>` — `LGR-99050e6-5`, restated by `LGR-69ca78b-4`.
  const reusedMappingData = useMemo<MappingData | null>(() => {
    if (runDetail?.reused_mapping !== true) return null;
    const details = (runDetail.approvals ?? []).find(
      (a) => a.approval_type === 'schema_mapping',
    )?.details;
    if (!details) return null;
    // `schema_hash` is `''` ON PURPOSE: the payload carries none. The model is
    // the payload's own `generated_by`, which the origin run's
    // `create_approval_payload` wrote, so it names the model that produced this
    // mapping. The pipeline's CURRENT `ai_model` is deliberately NOT the
    // fallback here: it is not the model that produced a mapping some earlier
    // run proposed, and passing it would attribute the reused mapping to a model
    // that may never have seen it. With no `generated_by` the model stays
    // absent — this file's spelling for not-established (`LGR-44d601e-1`,
    // `LGR-7561acb-7`).
    return mappingDataFromApprovalPayload(details, '', approvalMappingModel(details));
  }, [runDetail]);

  // Prev/next run ids, straight off the server's answer (p48 R10.2, R10.4).
  //
  // **Previous = older, Next = newer**, and the mapping is here in one place so it
  // is checkable. It is what the array arithmetic this replaces already meant: over
  // a newest-first page, `runs[idx + 1]` — the old `prevRunId` — was the next row
  // DOWN the list, which is the next-OLDER run. So the buttons keep pointing the
  // way they always did; only the resolution moved.
  //
  // `undefined` rather than the wire's `null` because that is what
  // `RunDetailHeader`'s optional props are typed as, and `disabled={!prevRunId}`
  // treats both the same. No `useMemo`: two property reads, nothing to memoise.
  const prevRunId = runNeighbours?.older?.run_id;
  const nextRunId = runNeighbours?.newer?.run_id;

  // Trace records carry their own step-name variant ("code_generation",
  // "code_testing"); state rows carry canonical or legacy names (P28 task
  // 14.2). Keying both sides by normalizeStepName makes every combination
  // meet in canonical space.
  const tracesByStep = useMemo(() => {
    const map = new Map<string, AgentTrace>();
    if (tracesData?.traces) {
      for (const t of tracesData.traces) {
        map.set(normalizeStepName(t.step_name), t);
      }
    }
    return map;
  }, [tracesData]);

  const isActive = runDetail?.status === 'RUNNING' || runDetail?.status === 'IN_PROGRESS' || runDetail?.status === 'WAITING_APPROVAL';

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" color="text-body-secondary">Loading run details...</Box>
      </Box>
    );
  }

  // A run the operator reached from a run list, which that list had read to them
  // as SKIPPED, and which correctly 404s because a skip writes no state rows
  // (p54 R6.1, R6.2 — design 4.6, arm B2). See `readCarriedSkip` above for why
  // this is a frontend recognition rather than a backend change.
  //
  // **STRICTLY NARROWER THAN THE BRANCH BELOW, AND ORDERED BEFORE IT.** Both
  // require `!isError && runDetail === null` — the 404. This one adds the
  // carried descriptor, and that single extra conjunct is the whole
  // discriminator. Moving this below the next branch, or dropping the conjunct,
  // makes one of the two unreachable: the next branch has no guard this one
  // lacks. With no descriptor the next branch fires, which is what keeps its
  // copy reachable and unchanged (R6.4, R8.5, p38 R4.1) and keeps
  // `RunDetailPage.error-states.test.tsx:137-151` passing unmodified. This
  // branch sits BESIDE that one; it does not replace it.
  //
  // No Retry, for the same reason the branch below has none: re-requesting an id
  // that correctly 404s cannot succeed (p38 R4.4, `LGR-2bcdd0d-3`, which
  // rejected a Retry in as many words). `Back to Pipeline` is kept.
  if (!isError && runDetail === null && carriedSkip) {
    const { descriptor, skip } = carriedSkip;
    return (
      <Container>
        <Box padding="l">
          <SpaceBetween size="m">
            {/* The reason, from the ONE derivation both run lists render, so the
                detail page cannot describe a skip differently from the list the
                operator clicked from (R6.2 shares R7's authority). Words carry
                the distinction, not the badge colour — `LGR-99050e6-1`
                (template / ratified): colour alone fails WCAG 1.4.1, so the
                label and its full detail are both written out. The detail is
                visible text here rather than the lists' hover title, because a
                page has room for it and a reason an operator must hover to find
                is not stated. */}
            <SpaceBetween direction="horizontal" size="xs">
              <StatusIndicator type="info">{descriptor.status}</StatusIndicator>
              <Badge color={skip.color} data-testid="skip-reason-badge">{skip.label}</Badge>
            </SpaceBetween>
            <Box variant="p" data-testid="run-detail-skip-reason-detail">{skip.detail}</Box>

            {/* What the 404 actually established, in place of the denial it used
                to produce. No steps, logs or results — and no claim beyond
                that. */}
            <Box variant="p" data-testid="run-detail-skip-absence">
              {SKIPPED_RUN_NO_EXECUTION_STATEMENT}
            </Box>

            {/* The id, named for the same reason the branch below names it: the
                operator arrived here by navigating to it. Stated as the subject
                of a skip rather than as an identity that does not exist. */}
            {runId && (
              <Box variant="small" color="text-body-secondary" data-testid="run-detail-skip-run-id">
                Run id: {runId}
              </Box>
            )}

            {/* Both optional, both omitted when the row did not carry them. A
                contention skip carries no `previous_run_id` at all, so this page
                must not promise one (`LGR-44d601e-1`). Text, not links — p38
                do-NOT 5 / `LGR-453f12d-5`. */}
            {descriptor.file_name && (
              <Box variant="small" color="text-body-secondary" data-testid="run-detail-skip-file">
                {SKIPPED_RUN_FILE_LABEL}: {descriptor.file_name}
              </Box>
            )}
            {descriptor.previous_run_id && (
              <Box variant="small" color="text-body-secondary" data-testid="run-detail-skip-previous-run">
                {SKIPPED_RUN_PREVIOUS_RUN_LABEL}: {descriptor.previous_run_id}
              </Box>
            )}

            <Box>
              <Button onClick={() => navigate(pipelineId ? `/pipelines/${pipelineId}` : '/')}>
                Back to Pipeline
              </Button>
            </Box>
          </SpaceBetween>
        </Box>
      </Container>
    );
  }

  // Two distinct outcomes, stated in two distinct texts (p38 R4.1-R4.4).
  // `fetchRunDetail` reserves `null` for a 404 and rejects on everything else
  // (use-run-detail.ts:16-33), so `!isError && runDetail === null` means one
  // thing only: there is no run with this id. The distinction is carried by
  // the WORDING, not by the StatusIndicator type or its colour —
  // LGR-99050e6-1 (template / ratified) records that colour alone fails
  // WCAG 1.4.1, so each state also carries a written statement.
  //
  // Reached whenever no skip descriptor was carried — a pasted URL, a bookmark,
  // a deep link, a cmd/ctrl/middle-click, or a run id that genuinely does not
  // exist. The copy is p38 R4.1's and is NOT replaced (R8.5).
  if (!isError && runDetail === null) {
    return (
      <Container>
        <Box textAlign="center" padding="l">
          {/* R4.1 — names the id, because a 404 on an id the dashboard just
              navigated to is a claim about identity, not availability. */}
          <StatusIndicator type="error">No run with id {runId} exists.</StatusIndicator>
          <Box padding={{ top: 's' }}>
            {/* R4.4 — a route back to the pipeline. Deliberately NO Retry:
                re-requesting an id that correctly 404s cannot succeed, so the
                button would be a false affordance (R4.4's note). */}
            <Button onClick={() => navigate(pipelineId ? `/pipelines/${pipelineId}` : '/')}>
              Back to Pipeline
            </Button>
          </Box>
        </Box>
      </Container>
    );
  }

  // The request did not complete (R4.2). Retrying it can succeed, so this is
  // the one branch that carries the retry affordance. The `!runDetail` arm also
  // catches `data === undefined` with no error — a disabled query — which
  // renders here exactly as it did before the split, and keeps the non-null
  // narrowing everything below this guard relies on.
  if (isError || !runDetail) {
    return (
      <Container>
        <Box textAlign="center" padding="l">
          <StatusIndicator type="error">Run details could not be retrieved.</StatusIndicator>
          <Box padding={{ top: 's' }}>
            <SpaceBetween direction="horizontal" size="xs">
              <Button data-testid="run-detail-retry" onClick={() => refetch()}>Retry</Button>
              <Button onClick={() => navigate(pipelineId ? `/pipelines/${pipelineId}` : '/')}>
                Back to Pipeline
              </Button>
            </SpaceBetween>
          </Box>
        </Box>
      </Container>
    );
  }

  // --- Approval action handlers (route through the shared Approval_Contract) ---

  // Tab (ApprovalWorkflowSection) approve/reject — carries question answers.
  const handleTabApprove = (approvalId: string, questionAnswers?: Record<string, string>) => {
    showToast('Approving...', 'info');
    approvalMutation.mutate(
      { id: approvalId, action: 'approve', runId, questionAnswers },
      { onError: () => showToast('Approve failed', 'error') },
    );
  };
  const handleTabReject = (approvalId: string) => {
    showToast('Rejecting...', 'info');
    approvalMutation.mutate(
      { id: approvalId, action: 'reject', runId },
      { onError: () => showToast('Reject failed', 'error') },
    );
  };

  // Metadata-card pending-approval Approve/Reject — optimistic banner UI.
  const handleMetaApprove = (approvalId: string) => {
    setOptimisticAction('approving');
    approvalMutation.mutate(
      { id: approvalId, action: 'approve', runId },
      {
        onSuccess: () => showToast('Approved', 'success'),
        onError: () => { setOptimisticAction(null); showToast('Approve failed', 'error'); },
      },
    );
  };
  const handleMetaReject = (approvalId: string) => {
    setOptimisticAction('rejecting');
    approvalMutation.mutate(
      { id: approvalId, action: 'reject', runId },
      {
        onSuccess: () => showToast('Rejected', 'success'),
        onError: () => { setOptimisticAction(null); showToast('Reject failed', 'error'); },
      },
    );
  };

  // --- Schema tab handlers (mapping context + regeneration) ---
  const handleSaveContext = async (context: string) => {
    if (pipelineId) {
      await editPipeline.mutateAsync({ id: pipelineId, mapping_context: context });
      refetchPipeline();
      showToast(context ? 'Mapping context saved' : 'Mapping context cleared', 'success');
    }
  };
  const handleRerunMapping = (context: string, modelId?: string) => {
    if (pipelineId) {
      setWaitingForRegen(true);
      regenerateMapping.mutate(
        { pipelineKey: pipelineId, context, model_id: modelId },
        {
          onSuccess: () => {
            // Polling is handled by useMappingRegenPolling (effect-scoped,
            // status-based stop on mapping_id change, unmount-safe) — no
            // orphaned inline setInterval/setTimeout here.
            showToast('Mapping regeneration started — will refresh when complete', 'info');
          },
          onError: () => { showToast('Failed to regenerate mapping', 'error'); setWaitingForRegen(false); },
        },
      );
    }
  };

  // p38 R2.2 / R2.3. Built here rather than in a useMemo above because it is
  // a pure read of `runDetail`, which only narrows to non-null past the two
  // error guards.
  const retryBanner = buildRetryBanner(runDetail);

  return (
    <SpaceBetween size="l">
      <RunDetailHeader
        runId={runDetail.run_id}
        status={runDetail.status}
        isActive={isActive}
        prevRunId={prevRunId}
        nextRunId={nextRunId}
        retryStepOptions={retryStepOptions}
        selectedRetryStep={selectedRetryStep}
        retryPending={retryMutation.isPending}
        cancelPending={cancelRun.isPending}
        onBack={() => navigate(pipelineId ? `/pipelines/${pipelineId}` : '/')}
        onPrev={() => { if (prevRunId) navigate(`/pipelines/${pipelineId}/runs/${prevRunId}`); }}
        onNext={() => { if (nextRunId) navigate(`/pipelines/${pipelineId}/runs/${nextRunId}`); }}
        onSelectRetryStep={(value) => setSelectedRetryStep(value)}
        onRetry={() => retryMutation.mutate({ runId: runDetail.run_id, retryFromStep: selectedRetryStep })}
        onCancel={() => {
          cancelRun.mutate(runDetail.run_id, {
            onSuccess: (response) => {
              const toast = cancelOutcomeToast(response);
              showToast(toast.message, toast.type);
              refetch();
            },
            onError: (error) => showToast(cancelFailureMessage(error), 'error'),
          });
        }}
        onRefresh={() => refetch()}
      />

      {/* Retried banner (p38 R2.2, R2.3) — see buildRetryBanner above for the
          four tells and why the pre-p38 case drives the design. No link: under
          one shared id the only candidate id is this run's own, and rendering
          it is what do-NOT 5 / R5.3 forbid. */}
      {retryBanner && (
        <Alert type="info" data-testid="retried-banner">
          <SpaceBetween size="xxs">
            {/* How many attempts. Never "0 attempts" — an unrecorded count is
                stated as unrecorded (LGR-99050e6-2). */}
            <Box variant="p">
              {retryBanner.attemptCount === null
                ? 'This run has been attempted more than once. How many times is not recorded.'
                : `This run has been attempted ${retryBanner.attemptCount} times.`}
            </Box>

            {/* What each earlier attempt did, and where. Flattened rather than
                nested so no <p> ends up inside another <p>: an attempt's own
                R2.3 disagreement is a sibling line, not a child. */}
            {retryBanner.priorAttempts.flatMap((attempt, i) => {
              const id = typeof attempt.attempt === 'number' ? String(attempt.attempt) : `i${i}`;
              const subject =
                typeof attempt.attempt === 'number' ? `Attempt ${attempt.attempt}` : 'That attempt';
              return [
                <Box variant="p" key={`attempt-${id}`}>{describeAttempt(attempt)}</Box>,
                ...findDisagreements(
                  attempt.status,
                  Object.entries(attempt.steps_status ?? {}),
                ).map((d) => (
                  <Box variant="p" key={`attempt-${id}-${d.step}`}>
                    {describeDisagreement(d, subject)}
                  </Box>
                )),
              ];
            })}

            {/* The pre-p38 case: retried, but no attempt record was ever
                written for the earlier attempts, so there is nothing to
                attribute. Said out loud rather than left as an empty list. */}
            {retryBanner.attributionMissing && (
              <Box variant="p">
                No per-attempt record was kept for the earlier attempts, so what
                each of them did is not recoverable.
              </Box>
            )}

            {/* R2.3 on the run's own row — both values, neither chosen. */}
            {retryBanner.runDisagreements.map((d) => (
              <Box variant="p" key={`run-disagreement-${d.step}`}>
                {describeDisagreement(d, 'This run')}
              </Box>
            ))}

            {/* THE load-bearing sentence. A bookmarked original-run URL now
                resolves here and shows the retry's outcome; this is what makes
                that legible rather than confusing. */}
            <Box variant="p">
              {retryBanner.attemptCount === null
                ? 'The steps and status below describe the most recent attempt, not the earlier ones.'
                : `The steps and status below describe attempt ${retryBanner.attemptCount}.`}
            </Box>
          </SpaceBetween>
        </Alert>
      )}

      {/* What reached the target (p37 R1.8). Above the tabs, not inside the Data
          Quality tab, and the reason is that the common case has no quality
          report at all: `quarantine_path` exists only for the inline-DQ case, so
          a run that lost every row to a transform filter would render this label
          behind a tab reading "No data quality results available for this run".
          It renders nothing for `complete_load`'s absent counts and for
          `not_established`, so an unaffected run is unchanged.

          The report is passed in for the quarantine locator only — this is a
          sibling of QualityVerdictLabel, which stays inside QualityReportCard on
          the Data Quality tab and is not modified. */}
      <LoadOutcomeLabel outcome={runDetail.load_outcome} report={qualityReport} />

      {/* Run metadata — stays above tabs */}
      <RunMetadataPanel
        runDetail={runDetail}
        isActive={isActive}
        isWebSocketConnected={isWebSocketConnected}
        optimisticAction={optimisticAction}
        onApprove={handleMetaApprove}
        onReject={handleMetaReject}
      />

      {/* Tabbed content */}
      <Tabs
        activeTabId={activeTabId}
        onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
        tabs={[
          {
            id: 'step-flow', label: 'Step Flow',
            content: (
              <StepFlowTab
                runDetail={runDetail}
                pipelineId={pipelineId}
                runId={runId}
                selfHealingActivity={selfHealingActivity}
                pipelineDetail={pipelineDetail}
              />
            ),
          },
          {
            id: 'ai-traces', label: 'AI Traces',
            content: (
              <AiTracesTab
                tracesData={tracesData}
                tracesLoading={tracesLoading}
                tracesError={tracesError}
                onRetryTraces={() => refetchTraces()}
                steps={runDetail.steps}
                tracesByStep={tracesByStep}
              />
            ),
          },
          {
            id: 'approvals', label: 'Approvals',
            content: (
              <ApprovalsTab
                approvals={runDetail.approvals}
                runDetails={runDetail}
                onApprove={handleTabApprove}
                onReject={handleTabReject}
              />
            ),
            disabled: !runDetail.approvals || runDetail.approvals.length === 0,
          },
          {
            id: 'schema', label: 'Schema & Mappings',
            content: (
              <SchemaTab
                // p54 R1.3 — the reused mapping's fields stand in only when the
                // run produced none of its own, and `reusedMappingData` is null
                // for every run that is not a Reuse_Run.
                mappingData={runMappingData ?? reusedMappingData}
                extractedSchema={extractedSchema}
                pipelineKey={pipelineId}
                // `runMappingId` stays the RUN's own id and is deliberately NOT
                // given `reused_mapping_id` — that would flip `mappingIsCurrent`
                // and offer the editor on a Reuse_Run. See
                // `REUSED_MAPPING_READ_ONLY` in `SchemaTab.tsx` for the rejected
                // route; `LGR-99050e6-3` is the rule being preserved.
                runMappingId={runMappingId}
                currentMappingId={pipelineDetail?.mapping_id}
                reusedMapping={runDetail.reused_mapping}
                reusedMappingId={runDetail.reused_mapping_id}
                initialContext={pipelineDetail?.mapping_context}
                defaultModelId={pipelineDetail?.ai_model}
                isRegenerating={regenerateMapping.isPending || waitingForRegen}
                onSaveContext={handleSaveContext}
                onRerunMapping={handleRerunMapping}
              />
            ),
          },
          {
            id: 'code', label: 'Code',
            content: <CodeTab codeVersionId={codeVersionId} />,
          },
          {
            id: 'data-quality', label: 'Data Quality',
            content: (
              <DataQualityTab
                qualityReport={qualityReport}
                qualityError={qualityError}
                onRetry={() => refetchQuality()}
              />
            ),
          },
        ]}
      />
    </SpaceBetween>
  );
}
