// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
import Link from '@cloudscape-design/components/link';
import Badge from '@cloudscape-design/components/badge';
import {
  useArchivePipeline,
  usePipelineExecutions,
  useApprovalAction,
  usePipelinePendingApprovals,
} from '../hooks';
import { getModelDisplayName, FALLBACK_MODEL_DISPLAY, formatRelativeTime, getStatusIcon, resolveStepStatus, statusType } from '../utils/formatting';
import { orderStepKeys } from '../utils/step-names';
import { nextScheduledRun, formatNextRun } from '../utils/cron-utils';
import { describeSkip } from '../utils/skip-reason';
import { buildSkippedRunNavState } from '../utils/skipped-run-descriptor';
import {
  distinctLostReasons,
  isLostQueuedFile,
  lostFilesNotAlreadyQueued,
} from '../utils/queued-file-state';
import { useToast } from '../contexts/ToastContext';
import { usePipelineContext } from '../contexts/PipelineContext';
import PipelineFormModal from '../components/PipelineFormModal';
import FileUploadModal from '../components/FileUploadModal';
import TargetConfigDisplay from '../components/TargetConfigDisplay';
import SourceConfigDisplay from '../components/SourceConfigDisplay';
import DataPreviewTable from '../components/DataPreviewTable';
import RunNowButton from '../components/RunNowButton';
import PipelineCostPanel from '../components/PipelineCostPanel';
import LoadOutcomeIndicator from '../components/LoadOutcomeIndicator';
import QueuedFilesAbout from '../components/QueuedFilesAbout';
import type { LoadOutcome } from '../types';

/**
 * What a count on this card can be (p51 C4). A discriminated union rather than
 * three sibling conditionals, so "exactly one of" is **structural**: two states
 * cannot both render, and none-of-them cannot happen. The same move C1 made one
 * layer down, where the count and the controls read one array so they cannot
 * describe different sets.
 *
 * - `count` — a number the read established. `isFloor` marks it as a lower
 *   bound rather than a total, which is what R2.3 forbids rendering as a total.
 * - `unavailable` — the read **failed**. Rendered in words, never as `0`
 *   (`LGR-99050e6-2`, **template / ratified**: "an absent quarantine counter is
 *   reported as unknown rather than as zero").
 * - `unknown` — the read has not answered *yet*. Renders nothing, and is a
 *   separate member from `none` on purpose: they render the same thing for
 *   different reasons, and collapsing them would make the model claim the
 *   pipeline has none while the request is still open.
 * - `none` — the read succeeded and the answer is zero. Renders nothing, as
 *   before this spec.
 */
type CountState =
  | { kind: 'count'; value: number; isFloor: boolean }
  | { kind: 'unavailable' }
  | { kind: 'unknown' }
  | { kind: 'none' };

/**
 * The one place either count is classified, so the two lines cannot drift in
 * *when* they say something — only in *what* they say.
 *
 * **`readFailed` is checked first, and that ordering is load-bearing.** React
 * Query leaves `data` at its last value or `undefined` on error; if `value`
 * were consulted first, a failed read holding no data would classify as
 * `unknown` and go silent — which is the indistinguishable-from-empty outcome
 * C4 exists to remove.
 */
function classifyCount(
  value: number | undefined,
  readFailed: boolean,
  isFloor: boolean,
): CountState {
  if (readFailed) return { kind: 'unavailable' };
  if (value === undefined) return { kind: 'unknown' };
  if (value <= 0) return { kind: 'none' };
  return { kind: 'count', value, isFloor };
}

export default function PipelineOverviewPage() {
  const { pipeline, refetch } = usePipelineContext();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const archiveMutation = useArchivePipeline();
  const approvalAction = useApprovalAction();

  const [showEditModal, setShowEditModal] = useState(false);
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [pendingApprovalId, setPendingApprovalId] = useState<string | null>(null);

  const activeVersion = pipeline.schema_history?.[pipeline.schema_history.length - 1];

  /**
   * Page 1, **unfiltered** — exactly as before. This read feeds Recent Runs
   * (the five newest rows, no pager, R3.4) and `PipelineCostPanel`'s `hasRuns`,
   * and nothing else. No cursor is passed and none ever should be: this surface
   * is a preview by design (p48 R10.3).
   */
  const { data: execData } = usePipelineExecutions(pipeline.pipeline_key, 1);
  const latestExecs = execData?.executions ?? [];

  /**
   * p54 R9.1 — `queued_files`, off the **array this page already receives**. The
   * response has carried it since p48; this page discarded it, so a Deferred_File
   * was invisible on the surface an operator lands on.
   *
   * **Read here, rendered in its own region below the Recent Runs table — never
   * merged into `latestExecs`** (do-NOT 3, do-NOT 15, p48 O2). A queued entry has
   * no `run_id` and no `started_at` in its published shape, so merging means
   * inventing both; the invented `run_id` would link to a `get_run_details` that
   * 404s, which is R6's defect reintroduced by R9's fix. It would also consume
   * page slots ahead of real runs and inflate the running count.
   *
   * `.slice(0, 5)` below is untouched and the runs table is unchanged, so Recent
   * Runs still shows exactly the five newest rows with no pager (R9.6). `hasRuns`
   * is untouched (R8.4) and the count block above is untouched (do-NOT 11).
   */
  const queuedFiles = execData?.queued_files ?? [];

  /**
   * p54 R10.3 — the **durable** record of files that will never be processed, from
   * the response's own `lost_files` array.
   *
   * `queuedFiles` above cannot carry this fact for long. It is the first page of a
   * newest-first index, and a loss row's `started_at` is the instant the loss was
   * *recorded*, so on an active pipeline it enters at the top and is pushed off page 1
   * within about an hour — while the record itself lives 30 days. The condition that
   * creates a lost file, a busy pipeline holding an approval lock, is the same
   * condition that buries the evidence. `lost_files` comes from a keyed read of the
   * placeholder namespace instead, so it is the same on every page and under every
   * filter.
   *
   * Filtered through `lostFilesNotAlreadyQueued` so a just-recorded loss is not listed
   * twice while it is still on page 1; see that helper for why the Queued Files table
   * keeps its own `failed_dlq` rendering rather than handing it over.
   *
   * **Never merged into `latestExecs`**, for the same four reasons the queued array is
   * not (do-NOT 3, do-NOT 15, p48 O2) and one more: `lost_files` publishes no `run_id`
   * at all, so there is nothing to fabricate a link from even by accident.
   */
  const lostFiles = lostFilesNotAlreadyQueued(execData?.lost_files, queuedFiles);

  /**
   * The running count comes from a **second, server-filtered read** of the same
   * hook, not from `latestExecs` (p51 C3, R2.1/R2.3).
   *
   * The old derivation was `latestExecs.filter(e => e.status === 'RUNNING')
   * .length` — a count over the ten rows page 1 happened to hold, rendered
   * under the Status key label as a statement about the pipeline. `LGR-8a4fefa-2`
   * (**template / ratified**) recorded exactly that, and named this edit as its
   * `revisit_when`: "`runningCount` when the overview card is next edited."
   *
   * Filtering the loaded rows and re-wording the label is the one shape
   * `LGR-2bcdd0d-2` (**template / ratified**) forbids — "a filter over loaded
   * rows presented as a filtered view of the pipeline" — so the filter has to
   * happen where the walk happens, on the server.
   *
   * **What `total_count` counts here, per `pipelines_runs.py:872`:** with
   * `?status=` present it counts **matches found**, not raw partition rows; the
   * partition `Select="COUNT"` walk is skipped entirely because it counts the
   * wrong set; and the number is exact only when the walk covered the whole
   * partition, otherwise it is a **floor** named in `partial_counts`. Hence the
   * two fields below rather than one — `isFloor` is what keeps a bounded walk
   * from being rendered as a total.
   *
   * Not made exact by a keyed read, deliberately: the executions table has no
   * `(pipeline_id, status)` GSI and is not getting one — it already carries
   * three and a fourth must deploy alone (`LGR-7d19eca-1`). A count on one card
   * does not clear that bar.
   *
   * Cost, stated rather than discovered later: one extra polled query per
   * overview mount, and a bounded filter walk on the server instead of a single
   * page read. It inherits `EXECUTIONS_POLL_MATRIX` untouched — no bespoke
   * `refetchInterval` here, which `LGR-e800696-1` (**template / ratified**)
   * fences. The `useWebSocket` registration the hook makes is refcounted
   * (`WebSocketProvider.subscribe`), so a second consumer of the same pipeline
   * topic adds no socket and no second subscription set.
   */
  const { data: runningData, isError: runningReadFailed } = usePipelineExecutions(
    pipeline.pipeline_key,
    1,
    null,
    'RUNNING',
  );
  /**
   * `total_count` is `undefined` while the read is in flight, and a failure is
   * reported by `isError` rather than by the absence of data — see
   * `classifyCount`, which is why the two arrive separately here.
   *
   * `isFloor` is true when a bound stopped the walk before the partition was
   * exhausted, so the number is a floor rather than a total. `partial_counts` is
   * always present and `[]` when nothing was short (`LGR-9a35b2d-3`,
   * `LGR-b2f79ef-14`) — an unmarked could-not-check reported as
   * checked-and-found-none is the defect those records exist to prevent.
   *
   * Keyed on the **`total_count` member specifically**, not on the array being
   * non-empty, and that is load-bearing: the same array also carries
   * `"executions"` for a different condition — "a filtered page shorter than
   * `page_size` with `has_next: true`" (`pipelines_runs.py:878-880`), which says
   * the *page* was short, not that the *count* is a floor. A
   * `partial_counts.length > 0` test would render an exact count as a floor
   * every time the filtered page came back short. P8 asserts that distinction
   * now that the floor reaches the DOM.
   */
  const runningRuns = classifyCount(
    runningData?.pagination?.total_count,
    runningReadFailed,
    runningData?.pagination?.partial_counts?.includes('total_count') ?? false,
  );

  /**
   * The awaiting-approval set comes from the **keyed approvals read**, not from
   * the ten execution rows above (p51 C3, R1.1–R1.3).
   *
   * Two independent reasons, both measured rather than assumed:
   *
   * 1. **Scope.** `latestExecs` is page 1 at `EXECUTIONS_PAGE_SIZE` = 10, so an
   *    approval whose run sits outside the ten newest lost its Approve/Reject
   *    buttons entirely — a missing action, not a wrong number. `LGR-8b6e6a1-1`
   *    (**template / ratified**) independently identified `pending_approval_id`
   *    as the field that "gates the Approve/Reject buttons entirely".
   * 2. **The filtered status is the wrong one.** The previous derivation filtered
   *    on the execution row's *top-level* `status === 'WAITING_APPROVAL'`. Both
   *    live pending approvals (measured 2026-08-26, the validation account) sit on rows whose
   *    top-level status is `FAILED` with `current_step: "pipeline_failure"`, while
   *    `steps_status.schema_mapping` is still `WAITING_APPROVAL` and
   *    `pending_approval_id` is still set — the designed steady state for a
   *    `timeout_behavior: "manual"` approval whose Step Functions execution has
   *    already failed. So that filter rendered **zero** controls for both of
   *    them. No filter over the execution row would have fixed it.
   *
   * The count and the controls read the same array, so they cannot describe
   * different sets (R1.2) — that is structural here, not a convention to be
   * maintained.
   *
   * **This count is never a floor, and that is structural rather than a
   * coincidence** — which is why it carries no scope clause where the running
   * count does. The read is a single keyed Query on `pipeline-status-index`
   * (`pipeline_id` = this pipeline, `status = "pending"`), so the key condition
   * *is* the question: there is no walk to truncate and no bound to name. The
   * executions table has no `(pipeline_id, status)` index and is not getting one
   * (`LGR-7d19eca-1`), so the running count can only be bounded. That asymmetry
   * is the substance of this spec, and R2.2 asks for a scope statement only
   * "wherever that set is not the whole history" — here it always is.
   */
  const { data: pendingApprovals, isError: approvalsReadFailed } =
    usePipelinePendingApprovals(pipeline.pipeline_key);
  const waitingApprovals = pendingApprovals ?? [];
  const approvals = classifyCount(
    // `undefined` until the read answers — deliberately not `waitingApprovals
    // .length`, which would be a confident `0` for a request still in flight.
    pendingApprovals?.length,
    approvalsReadFailed,
    // Never a floor, per the keyed-read argument above.
    false,
  );

  async function handleArchive() {
    try {
      await archiveMutation.mutateAsync(pipeline.pipeline_key);
      showToast('Pipeline archived successfully', 'success');
      navigate('/');
    } catch {
      showToast('Failed to archive pipeline', 'error');
    }
  }

  return (
    <SpaceBetween size="l">
      {/* Action buttons */}
      <SpaceBetween direction="horizontal" size="xs">
        {/* P17 Task 21.1 / Req 5.6 — "Run Now" triggers a full Step
            Functions execution and is available for all source types
            (S3_UPLOAD and JDBC). The existing "View Glue Job" page
            still exposes the legacy re-run-glue-job button for
            advanced users (Req 5.6 final paragraph). */}
        <RunNowButton pipelineKey={pipeline.pipeline_key} />
        <Button iconName="upload" onClick={() => setShowUploadModal(true)}>Upload File</Button>
        <Button onClick={() => setShowEditModal(true)}>Edit Pipeline</Button>
        <Button onClick={handleArchive} loading={archiveMutation.isPending}>Archive Pipeline</Button>
        {pipeline.pipeline_key && (
          <Button iconName="external" onClick={() => navigate(`/pipelines/${pipeline.pipeline_key}/glue-job/${pipeline.pipeline_key}`)}>
            View Glue Job
          </Button>
        )}
        <Button iconName="refresh" onClick={() => refetch()}>Refresh</Button>
      </SpaceBetween>

      {/* Pipeline metadata */}
      <Container header={<Header variant="h2">Pipeline Details</Header>}>
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Status</Box>
            <StatusIndicator type={statusType(pipeline.status)} data-testid="pipeline-status">
              {pipeline.status}
            </StatusIndicator>
            {/* The read failed, so there is no count to state. Said in **words**,
                because `LGR-99050e6-1` (**template / ratified**) records that
                colour alone fails WCAG 1.4.1 — and a `warning`
                `StatusIndicator` type is colour, so the type is not the carrier
                here, the sentence is.

                The second line is not decoration. The risk being guarded is not
                that the operator sees no number; it is that they read the
                absence as "none pending" and stop looking, which is the outcome
                `LGR-99050e6-2` forbids. */}
            {approvals.kind === 'unavailable' && (
              <Box padding={{ top: 'xxs' }} data-testid="approval-count-unavailable">
                <SpaceBetween size="xxs">
                  <StatusIndicator type="warning">
                    Awaiting approval: unavailable
                  </StatusIndicator>
                  <Box variant="small" color="text-body-secondary">
                    The pending-approval read failed, so this is not a count of
                    none — runs may be waiting for a decision that this card
                    cannot show.
                  </Box>
                </SpaceBetween>
              </Box>
            )}
            {approvals.kind === 'count' && (
              <Box padding={{ top: 'xxs' }}>
                <SpaceBetween size="xxs">
                  {/* Anchored on the **approval**, not the run: the approval is
                      pending, while its run may be FAILED — and on 2 of 2 live
                      rows it is. "N runs awaiting approval" was both the old
                      wording and false.

                      `design.md`'s Error Handling table read *"2 runs awaiting
                      approval"* until task 4.3 amended it to this, with the
                      measurement as the reason. The table predated task 1.2's
                      live read; the copy is the half that is true, so the design
                      moved rather than the copy. Recorded there as amendment 1
                      of 3, so the change reads as a correction and not as drift.

                      No scope clause, ever: the read is exact, per the
                      keyed-Query argument at the derivation above. */}
                  <StatusIndicator type="warning" data-testid="approval-count">
                    {approvals.value} approval{approvals.value > 1 ? 's' : ''} pending
                  </StatusIndicator>
                  {waitingApprovals.map((approval) => (
                    <SpaceBetween key={approval.approval_id} direction="horizontal" size="xxs">
                      {/* `approval_type` rather than the execution row's
                          `current_step`, which reads "pipeline_failure" on both
                          live rows. No "below" / "in the table" / "this run's
                          row" — the run need not be in Recent Runs at all, and
                          wording that survives either way costs nothing. */}
                      <Box variant="small" color="text-body-secondary" data-testid="approval-label">
                        {approval.approval_type || 'approval'} — {approval.run_id?.slice(0, 16)}
                      </Box>
                      <Button
                        variant="primary"
                        onClick={() => {
                          setPendingApprovalId(approval.approval_id);
                          approvalAction.mutate(
                            { id: approval.approval_id, action: 'approve', comment: '' },
                            {
                              onSuccess: () => { setPendingApprovalId(null); showToast('Approved', 'success'); },
                              onError: () => { setPendingApprovalId(null); showToast('Failed to approve', 'error'); },
                            },
                          );
                        }}
                        loading={approvalAction.isPending && pendingApprovalId === approval.approval_id}
                      >
                        Approve
                      </Button>
                      <Button
                        onClick={() => {
                          setPendingApprovalId(approval.approval_id);
                          approvalAction.mutate(
                            { id: approval.approval_id, action: 'reject', comment: '' },
                            {
                              onSuccess: () => { setPendingApprovalId(null); showToast('Rejected', 'success'); },
                              onError: () => { setPendingApprovalId(null); showToast('Failed to reject', 'error'); },
                            },
                          );
                        }}
                        loading={approvalAction.isPending && pendingApprovalId === approval.approval_id}
                      >
                        Reject
                      </Button>
                    </SpaceBetween>
                  ))}
                </SpaceBetween>
              </Box>
            )}
            {/* Same three states as the approvals line above, and the same
                reason for the second sentence. `0 runs in progress` is the one
                string this branch may never produce (`LGR-99050e6-2`). */}
            {runningRuns.kind === 'unavailable' && (
              <Box padding={{ top: 'xxs' }} data-testid="running-count-unavailable">
                <SpaceBetween size="xxs">
                  <StatusIndicator type="warning">
                    Runs in progress: unavailable
                  </StatusIndicator>
                  <Box variant="small" color="text-body-secondary">
                    The run-count read failed, so this is not a count of none —
                    runs may be in progress that this card cannot show.
                  </Box>
                </SpaceBetween>
              </Box>
            )}
            {runningRuns.kind === 'count' && (
              <Box padding={{ top: 'xxs' }}>
                <SpaceBetween size="xxs">
                  {/* *"At least N"* when a bound stopped the walk — a floor
                      stated as a floor (`LGR-2bcdd0d-2`, **template /
                      ratified**: `total_pages` "is a floor when the count was
                      bounded, so the UI states it as such rather than rendering
                      a floor as a total"). Pluralisation agrees with the numeral
                      rendered, not with the unknown true total. */}
                  <StatusIndicator type="in-progress" data-testid="running-count">
                    {runningRuns.isFloor ? 'At least ' : ''}
                    {runningRuns.value} run{runningRuns.value > 1 ? 's' : ''} in progress
                  </StatusIndicator>
                  {/* The scope clause, and only on the floor branch: a completed
                      walk counted every match in the partition, so that number
                      *does* describe the whole history and a clause would be
                      noise. R2.2 asks for one "wherever that set is not the
                      whole history".

                      Worded after p48's `data-testid="executions-filter-scope"`
                      line on `ExecutionsPage` (`:210-214`), minus its
                      "use Next to keep looking" — this card has no pager
                      (R3.4), so repeating that instruction here would point at
                      a control that does not exist. */}
                  {runningRuns.isFloor && (
                    <Box variant="small" color="text-body-secondary" data-testid="running-count-scope">
                      The search stopped before the end of this pipeline&apos;s
                      history, so more runs may be in progress than this.
                    </Box>
                  )}
                </SpaceBetween>
              </Box>
            )}
          </div>
          <div>
            <Box variant="awsui-key-label">Source Schema</Box>
            <Box>{pipeline.source_schema || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Target Schema</Box>
            <Box>{pipeline.target_schema || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Target Type</Box>
            <Box>{pipeline.target_type || pipeline.target_config?.type || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">AI Model</Box>
            {/* The pipeline's CONFIGURED model, which is what a field labelled
                "AI Model" in a pipeline-details panel states.

                This used to prefer `activeVersion.ai_model_used`, so a pipeline
                configured with a real model displayed "Fallback — no model used"
                whenever its latest mapping happened to come from the substring
                fallback. The value was true of that mapping and false of the
                pipeline, under a label that reads as the pipeline's. Observed on
                a pipeline whose record carries `ai_model=anthropic.claude-sonnet-5`.

                The mapping's own provenance is not lost: it is stated below when
                it differs, and in full on the mapping surfaces
                (`MappingViewer`, and the versions table further down this page). */}
            <Box data-testid="pipeline-model">
              {getModelDisplayName(pipeline.ai_model ?? pipeline.model_id)}
            </Box>
            {getModelDisplayName(activeVersion?.ai_model_used) === FALLBACK_MODEL_DISPLAY && (
              <Box variant="small" color="text-status-inactive" data-testid="pipeline-model-mapping-note">
                Latest mapping used no model (substring fallback)
              </Box>
            )}
          </div>
          <div>
            <Box variant="awsui-key-label">Created</Box>
            <Box>{pipeline.created_at ? formatRelativeTime(pipeline.created_at) : '—'}</Box>
          </div>
          {/* P17 Task 21.2 / Req 18.5 — schedule + next-scheduled-run-time
              display. Only rendered when a schedule expression is configured
              on the pipeline (the field is absent for ad-hoc S3-upload
              pipelines that have no schedule rule). */}
          {pipeline.schedule && (
            <div data-testid="pipeline-schedule-block">
              <Box variant="awsui-key-label">Schedule</Box>
              <Box>{pipeline.schedule}</Box>
              {(() => {
                const next = nextScheduledRun(pipeline.schedule);
                if (!next) {
                  return (
                    <Box color="text-status-warning" variant="small" data-testid="pipeline-next-run">
                      Unable to parse schedule expression
                    </Box>
                  );
                }
                return (
                  <Box variant="small" color="text-body-secondary" data-testid="pipeline-next-run">
                    Next run: {formatNextRun(next)}
                  </Box>
                );
              })()}
            </div>
          )}
        </ColumnLayout>
      </Container>

      {/* Recent Runs */}
      <Container header={<Header variant="h2">Recent Runs</Header>}>
        {(() => {
          return (
            <Table
              variant="embedded"
              items={(latestExecs as unknown as Array<Record<string, unknown>>).slice(0, 5)}
              columnDefinitions={[
                { id: 'file_name', header: 'File', cell: (item) => (item.file_name as string) || (item.file_key as string) || '—' },
                { id: 'started_at', header: 'Started', cell: (item) => item.started_at ? formatRelativeTime(item.started_at as string) : '—' },
                {
                  id: 'status', header: 'Status',
                  cell: (item) => {
                    /**
                     * R7.1 — the skip description comes from the same derivation
                     * the Executions tab uses (`describeSkip`, called at
                     * `ExecutionsPage.tsx:298`), not from a second table of skip
                     * copy on this surface. Until p54 this cell called nothing,
                     * so a duplicate skip read **"SKIPPED (duplicate_check)"**
                     * here against **"Skipped: duplicate content"** there — one
                     * outcome in two vocabularies, which is what R7 exists to
                     * remove.
                     *
                     * Keyed on `status` + `skip_reason`, **never on the run id**.
                     * Two id shapes exist and one of them is `run-`-prefixed on
                     * purpose (`skipped-{fingerprint}` for a duplicate,
                     * `run-{window}-skip-{reason}` for contention), so a prefix
                     * test would silently miss half the skips.
                     * `pipeline_trigger:373` says the same thing on the writer
                     * side and `use-executions.ts:69` on the reader side.
                     *
                     * The casts match `item.status as string` below — items here
                     * are untyped `Record<string, unknown>` (`:452`), while
                     * `PipelineExecution` already declares both skip fields, so
                     * nothing on the type side changes.
                     */
                    const skip = describeSkip(
                      item.status as string,
                      item.skip_reason as string,
                      item.skip_detail as string,
                    );
                    return (
                      <SpaceBetween direction="horizontal" size="xxs">
                        <StatusIndicator type={statusType(item.status as string)}>
                          {item.status === 'WAITING_APPROVAL' ? 'Awaiting Approval' : (item.status as string)}
                        </StatusIndicator>
                        {/* **What this cell shares with the Executions tab, and
                            what it does not.** The comment that stood here
                            claimed the two surfaces used "one component so the
                            two run-list surfaces cannot drift in what they say
                            about a run" — a claim that was only ever true of
                            `LoadOutcomeIndicator`, sitting directly above a skip
                            description that HAD drifted. The claim is what made
                            the drift invisible, so it is replaced with the
                            inventory rather than deleted (R7.3).
                            Shared, and therefore unable to drift:
                            `LoadOutcomeIndicator` for the load outcome — same
                            component, same position (p37 R1.7 / R1.9) — and
                            `describeSkip` for the skip label and its hover
                            detail (R7.1).
                            NOT shared, and free to differ: every other part of
                            this cell is its own markup. The Executions tab also
                            renders retry, self-healing, skip-count and
                            `WAITING_APPROVAL` step affordances that this
                            five-row preview deliberately does not. "Cannot
                            drift" is a statement about the two shared pieces
                            above, and about nothing else in this column. */}
                        <LoadOutcomeIndicator outcome={item.load_outcome as LoadOutcome | undefined} />
                        {skip && (
                          <span title={skip.detail}>
                            <Badge color={skip.color} data-testid="skip-reason-badge">{skip.label}</Badge>
                          </span>
                        )}
                        {/* The badge above REPLACES this parenthetical **for a
                            skip row only** (R7.2). On a skip, `current_step` is
                            the internal gate that made the decision —
                            `duplicate_check`, `concurrency_gate`,
                            `pipeline_lock_gate` — and showing it as the reason is
                            precisely what R7.2 forbids; showing it *beside* the
                            reason would put the internal name back on the card
                            for no reader's benefit.
                            On every other row it stays, because there it is doing
                            real work: for a run in flight it names the step in
                            flight, and that is the only place this preview says
                            where a run has got to. */}
                        {!skip && Boolean(item.current_step) && (
                          <Box variant="small" color="text-body-secondary">({item.current_step as string})</Box>
                        )}
                      </SpaceBetween>
                    );
                  },
                },
                { id: 'ai_model', header: 'Model', cell: (item) => getModelDisplayName((item.ai_model as string) || (item.ai_model_used as string)) },
                {
                  id: 'schema_version', header: 'Schema Version',
                  cell: (item) => item.schema_version != null ? <Badge>v{item.schema_version as number}</Badge> : '—',
                },
                {
                  id: 'steps', header: 'Steps',
                  cell: (item) => {
                    const stepsObj = item.steps_status || item.steps;
                    if (stepsObj && typeof stepsObj === 'object' && !Array.isArray(stepsObj)) {
                      // Canonical snake_case keys on new runs, legacy PascalCase
                      // on pre-rename runs (P28 task 14.2).
                      return orderStepKeys(Object.keys(stepsObj)).map((name) => (
                        <span key={name} title={name}>{getStatusIcon(resolveStepStatus((stepsObj as Record<string, string>)[name], item.status as string))}</span>
                      ));
                    }
                    return '—';
                  },
                },
                {
                  id: 'actions', header: 'Actions',
                  cell: (item) => {
                    const path = `/pipelines/${pipeline.pipeline_key}/runs/${item.run_id as string}`;
                    return (
                      /**
                       * R6.1 — a skip row's navigation carries the row's own skip
                       * fields, so the run-detail page can state what happened to
                       * the run rather than deny it exists. A skip has no state
                       * rows, so `get_run_details` correctly 404s (R6.3, R8.2 —
                       * p38 do-NOT 6 forbids relaxing that), and arm B2 has the
                       * frontend recognise the 404 beside the row it already
                       * holds. `buildSkippedRunNavState` returns `undefined` on
                       * every non-skip row. Skip-ness is `status` +
                       * `skip_reason`, never the run id (do-NOT 4).
                       *
                       * **The `href` STAYS, and that is a hard constraint, not a
                       * preference — do-NOT 5.** Two suites read each row's
                       * `run_id` off this attribute and nowhere else:
                       * `recent-runs-order.test.tsx:305-307`, whose own comment
                       * calls it "the only place a row's `run_id` reaches the
                       * DOM" and which then asserts a `skipped-` id at a specific
                       * index (`:336`, `:346`), and
                       * `running-count.test.tsx:414-417`. Replacing the `Link`
                       * with a `Button` — arm B1's shape — pays a two-suite cost
                       * for a display fix, which is what killed B1. So the router
                       * state arrives through `onFollow` *beside* the `href`
                       * rather than instead of it.
                       *
                       * `LinkProps.onFollow` is a `CancelableEventHandler`
                       * (`link/interfaces.d.ts:77-83`) whose own docstring
                       * directs client-side routing through this event and says
                       * to "prevent default browser navigation (by calling
                       * `preventDefault`)". Verified in the installed 3.0.1204
                       * rather than assumed: `link/internal.js:83` fires it
                       * through `fireCancelableEvent`, which propagates a
                       * cancelled custom event onto the native click
                       * (`internal/events/index.js:31-33`).
                       *
                       * Two consequences, both stated rather than discovered
                       * later. (1) Every row's View Run now routes client-side;
                       * before this it was a bare anchor, so a plain click was a
                       * full page load. Nothing depended on the reload, and it
                       * makes this cell agree with `ExecutionsPage`, whose View
                       * Run has always been `navigate`. (2) `onFollow` fires only
                       * on a plain left click (`isPlainLeftClick`,
                       * `link/internal.js:93`), so a cmd/ctrl/middle-click still
                       * follows the `href` with no state and lands on the bare
                       * 404 — the same accepted asymmetry design 4.6 records for
                       * a pasted URL, and the reason branch 3's copy stays
                       * reachable (R6.4, R8.5).
                       */
                      <Link
                        href={path}
                        onFollow={(e) => {
                          e.preventDefault();
                          navigate(path, { state: buildSkippedRunNavState(item) });
                        }}
                      >
                        View Run
                      </Link>
                    );
                  },
                },
              ]}
              /**
               * R5.1 — the route from the preview to the full history. The
               * destination already exists with paging and a status filter
               * (`App.tsx:153`), so this is zero backend work.
               *
               * **A link, never a control that changes `items`.** R5.2 and R5.3
               * are satisfied by *nothing appearing*: `.slice(0, 5)` above is
               * untouched, so this surface still shows exactly the five newest
               * rows with no pager (R8.3, p48 R10.3, p51 R3.4, and the comment
               * at `:93-98`). `LGR-1f7892b-1` (**template / ratified**)
               * re-ratifies the arrangement in as many words: the unfiltered
               * page-1 read is unchanged and still feeds "Recent Runs' five
               * newest rows with no pager … and `PipelineCostPanel`'s
               * `hasRuns`". Nothing here couples to `hasRuns` (R8.4), and the
               * count block above the table is untouched (`LGR-1f7892b-2`).
               *
               * **Rejected: a header action.** The header is a bare
               * `<Header variant="h2">Recent Runs</Header>`, so an action would
               * have broken no test — but the operator asked for it "at the
               * bottom of recent runs", and a "see the rest" affordance belongs
               * below the five rows it extends.
               *
               * The accessible name is deliberately **not** "View Run": that is
               * the per-row link's name, and two suites read a row's `run_id`
               * off it by that name (`recent-runs-order.test.tsx:306`,
               * `running-count.test.tsx:417`). A footer link sharing the name
               * would join those queries and break the length-5 assertions.
               */
              footer={
                <Link href={`/pipelines/${pipeline.pipeline_key}/executions`}>
                  View all runs
                </Link>
              }
              empty={<Box textAlign="center" color="text-body-secondary">No runs yet</Box>}
            />
          );
        })()}
      </Container>

      {/* Queued Files — p54 R9.1/R9.2/R9.3/R9.4.
          
          **Beside the Recent Runs table, not inside it.** See the `queuedFiles`
          derivation above for the four reasons a merge was rejected. Rendered only
          when the array is non-empty, so a pipeline with nothing queued shows
          nothing new here — absence of the region means "no file is waiting", which
          is the third of R9.5's three states and is not a value.

          Vocabulary is the Executions tab's, through the same `isLostQueuedFile`
          predicate, so one outcome cannot read two ways across the two surfaces
          (R7's argument, one requirement over). Every distinction is in words:
          `LGR-99050e6-1` records that colour alone fails WCAG 1.4.1 and
          `LGR-1f7892b-2` re-states it for a `StatusIndicator` type specifically. */}
      {queuedFiles.length > 0 && (
        <Container
          header={
            <Header variant="h2" counter={`(${queuedFiles.length})`} info={<QueuedFilesAbout />}>
              Queued Files
            </Header>
          }
        >
          <Table
            variant="embedded"
            items={queuedFiles}
            columnDefinitions={[
              { id: 'file_name', header: 'File', cell: (item) => item.file_name || '—' },
              {
                id: 'queued_at',
                /* "Queued At", the Executions tab's own header (`ExecutionsPage
                   .tsx:594`) — same column, same words, so one fact does not read
                   two ways across the two surfaces. It also keeps the header from
                   colliding with the Status cell's "Queued" label. */
                header: 'Queued At',
                cell: (item) => (item.queued_at ? formatRelativeTime(item.queued_at) : '—'),
              },
              {
                id: 'status',
                header: 'Status',
                cell: (item) =>
                  isLostQueuedFile(item.status) ? (
                    <StatusIndicator type="error">Failed (DLQ)</StatusIndicator>
                  ) : (
                    <StatusIndicator type="pending">Queued</StatusIndicator>
                  ),
              },
            ]}
            /* No `run_id` is published for a queued entry, so there is deliberately
               no View Run link on these rows — there is no run to view. That is also
               why this table has no `href` for the two suites that read a row's
               `run_id` off one (do-NOT 5): those queries are scoped to the Recent
               Runs table's links and this region adds none. */
            empty={<Box textAlign="center" color="text-body-secondary">No files queued</Box>}
          />
          {/* The p54 R9.2 holding sentence is behind the header's question-mark
              button (`QueuedFilesAbout`). The loss sentence stays on the page: it
              reports that something went wrong (R9.4), and only when it did. */}
          {queuedFiles.some((f) => isLostQueuedFile(f.status)) && (
            <Box variant="small" color="text-status-error">
              Failed (DLQ) means that file waited longer than its redelivery attempts
              allowed and was never processed. It will not be retried automatically.
            </Box>
          )}
        </Container>
      )}

      {/* Lost Files — p54 R10.3.

          The Queued Files region above tells the truth for about an hour. This one
          tells it for as long as the record exists, because it reads `lost_files`
          rather than page 1 of a newest-first index. See the `lostFiles` derivation
          for why the two arrays overlap and how the overlap is removed.

          Its own region rather than more rows in the table above: these files are not
          queued for anything. Presenting them under a "Queued Files" header, beside a
          footer about a run that will release the pipeline, is the promise p54 R9.2
          removed — restated by placement instead of by words.

          Rendered only when there is something to say, so absence of the region means
          "nothing has been lost". Every distinction is carried in words, never colour
          alone (`LGR-99050e6-1`, `LGR-1f7892b-2`). */}
      {lostFiles.length > 0 && (
        <Container
          header={
            <Header variant="h2" counter={`(${lostFiles.length})`}>
              Lost Files
            </Header>
          }
        >
          <Table
            variant="embedded"
            items={lostFiles}
            trackBy="source_key"
            columnDefinitions={[
              { id: 'file_name', header: 'File', cell: (item) => item.file_name || '—' },
              {
                id: 'lost_at',
                /* "Recorded", not "Lost At": the timestamp is when the loss was
                   *recorded*, which is hours after the upload, and the original queue
                   instant is not recoverable from a dead-lettered message. Naming the
                   column after the moment it actually describes is the same discipline
                   that made the producer decline to invent an upload time. */
                header: 'Recorded',
                cell: (item) => (item.lost_at ? formatRelativeTime(item.lost_at) : '—'),
              },
              {
                id: 'status',
                header: 'Status',
                /* Same vocabulary as the Queued Files table's lost arm, so one outcome
                   does not read two ways depending on which region happens to be
                   showing it. */
                cell: () => <StatusIndicator type="error">Failed (DLQ)</StatusIndicator>,
              },
            ]}
            /* No View Run link, and here that is structural rather than a choice:
               `lost_files` publishes no `run_id` at all. There is no run — nothing was
               ever started for these files. */
            empty={<Box textAlign="center" color="text-body-secondary">No files lost</Box>}
          />
          {/* The reason comes off the wire (`lost_reason`) rather than being written
              here, so the sentence an operator reads is the producer's own account of
              what happened. Deduplicated rather than taken from the first row — see
              `distinctLostReasons` — and falling back to a generic sentence for a
              record that carries no reason, so this is never blank and never a claim
              this surface invented. */}
          {distinctLostReasons(lostFiles).map((reason) => (
            <Box key={reason} variant="small" color="text-status-error" padding={{ top: 'xs' }}>
              {reason}
            </Box>
          ))}
        </Container>
      )}

      {/* Cost (P30 R3.2 — "THE pipeline overview SHALL display …"). `hasRuns`
          reuses the executions already fetched above so the empty-state copy can
          distinguish "never ran" from "ran, no cost attributed yet"; while that
          query is still in flight it is left undefined rather than guessed. */}
      <PipelineCostPanel
        pipelineId={pipeline.pipeline_key}
        hasRuns={execData ? latestExecs.length > 0 : undefined}
      />

      {/* Source Configuration */}
      <Container header={<Header variant="h2">Source Configuration</Header>}>
        <SourceConfigDisplay pipeline={pipeline} />
      </Container>

      {/* Target Configuration */}
      {pipeline.target_config && (
        <Container header={<Header variant="h2">Target Configuration</Header>}>
          <TargetConfigDisplay pipeline={pipeline} />
        </Container>
      )}

      {/* Data Preview */}
      <DataPreviewTable pipelineId={pipeline.pipeline_key} sourceType={pipeline.source_type} />

      {/* Modals */}
      <PipelineFormModal visible={showEditModal} onDismiss={() => setShowEditModal(false)} pipeline={pipeline} />
      <FileUploadModal visible={showUploadModal} onDismiss={() => setShowUploadModal(false)} pipeline={pipeline} />
    </SpaceBetween>
  );
}
