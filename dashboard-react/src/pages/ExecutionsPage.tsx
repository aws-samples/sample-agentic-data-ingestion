// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Table from '@cloudscape-design/components/table';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Button from '@cloudscape-design/components/button';
import Modal from '@cloudscape-design/components/modal';
import Textarea from '@cloudscape-design/components/textarea';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Link from '@cloudscape-design/components/link';
import Select from '@cloudscape-design/components/select';
import { usePipelineExecutions, useCancelRun, useApprovalAction, useRetryRun } from '../hooks';
import { cancelFailureMessage, cancelOutcomeToast } from '../hooks/use-cancel-run';
import { getModelDisplayName, formatRelativeTime, getStatusIcon, resolveStepStatus, statusType } from '../utils/formatting';
import { orderStepKeys } from '../utils/step-names';
import { describeSelfHealing } from '../utils/self-healing';
import { describeSkip } from '../utils/skip-reason';
import { buildSkippedRunNavState } from '../utils/skipped-run-descriptor';
import {
  distinctLostReasons,
  isLostQueuedFile,
  lostFilesNotAlreadyQueued,
} from '../utils/queued-file-state';
import { usePipelineContext } from '../contexts/PipelineContext';
import { useToast } from '../contexts/ToastContext';
import AsyncContent from '../components/common/AsyncContent';
import CursorPagination from '../components/common/CursorPagination';
import LoadOutcomeIndicator from '../components/LoadOutcomeIndicator';
import QueuedFilesAbout from '../components/QueuedFilesAbout';

/** The "no filter" option's value. Never sent; see `ExecutionsPage`'s Select. */
const ANY_STATUS = '';

/**
 * The statuses the run-history filter offers (p48 C5, R4.1, R4.4, task 5.5).
 *
 * NOT a guess and not the union of every string the codebase mentions — this is
 * the accepted set task 4.3 enumerated at
 * `pipelines_runs._ACCEPTED_STATUS_FILTERS`, each entry there carrying a named
 * producer, minus the two exclusions below. Sending anything outside the accepted
 * set is a 400, so the control is the reason a 400 here would be a bug rather than
 * user error.
 *
 * `SKIPPED` is **first-class and listed first** (R4.4): a duplicate skip pinned to
 * the top of the list by `run_id` ordering is the report this whole spec opened on,
 * and "show me only the skips" is the question that report implies.
 *
 * `WAITING_APPROVAL` has **no writer at all** — it is derived on the read path from
 * `pending_approval_id` (`pipelines_runs.py:632-637`). It is selectable only
 * because task 4.3 placed the filter *after* that derivation; a
 * `FilterExpression` on the stored status would return nothing for it. Its label
 * matches the Status column's own rendering of that value below, so the filter's
 * vocabulary is the vocabulary the table displays.
 *
 * THREE DELIBERATE OMISSIONS, all accepted by the server (or refused on purpose)
 * and none offered:
 *
 *  - `CANCELLING` — a run whose Glue job is being stopped. It lasts only until
 *    Glue reports the job stopped, usually minutes, so a filter on it would almost
 *    always be empty. The server accepts it, so adding the option later is one line.
 *  - `IN_PROGRESS` — accepted for old-row tolerance ("a live table can hold shapes
 *    no current producer writes") and it has **no writer either**. Task 4.3 stated
 *    that 5.5 "may leave it out of the dropdown without any server change; the
 *    accepted set and the offered set are not required to match", and this is that
 *    choice taken. A dropdown is a menu of questions worth asking, and every
 *    `IN_PROGRESS` row in a live table is a legacy artefact; offering it invites an
 *    operator to ask a question whose answer is empty for every pipeline written by
 *    a current producer. It stays *findable* — the server still accepts
 *    `?status=IN_PROGRESS` — so nothing is closed off, and adding the option later
 *    is one line with no server change.
 *  - `QUEUED` — a **400** by design, so offering it would ship a control that
 *    errors. The reason is structural, not incidental: the QUEUED split runs
 *    *before* the filter (`_split_derive_and_filter`), so by the time the filter
 *    runs every such row is already in `queued_files` and the `executions` array
 *    provably cannot contain a match. The information is not lost — this page
 *    renders its own Queued Files section from that array, below, and that section
 *    is unaffected by this filter for the same reason.
 */
const STATUS_FILTER_OPTIONS = [
  { value: ANY_STATUS, label: 'All statuses' },
  { value: 'SKIPPED', label: 'SKIPPED' },
  { value: 'SUCCEEDED', label: 'SUCCEEDED' },
  { value: 'FAILED', label: 'FAILED' },
  { value: 'RUNNING', label: 'RUNNING' },
  { value: 'WAITING_APPROVAL', label: 'Awaiting Approval' },
  { value: 'CANCELLED', label: 'CANCELLED' },
] as const;

/** The offered label for a wire value, for use in prose about the filter. */
function statusFilterLabel(value: string): string {
  return STATUS_FILTER_OPTIONS.find((o) => o.value === value)?.label ?? value;
}

export default function ExecutionsPage() {
  const { pipeline } = usePipelineContext();
  const navigate = useNavigate();
  /**
   * The cursors used to reach pages 2..N, in order (p48 C2 Arm A, task 5.1).
   *
   * DynamoDB hands out a forward cursor and nothing else — there is no backward
   * key — so "previous" cannot be a server call. It is this stack: pushing
   * `pagination.next_cursor` moves forward, popping returns to the page whose
   * cursor is now on top. Depth is therefore the page number minus one, and
   * "can I go back" is `depth > 0`, which agrees with the wire's `has_prev`
   * (`page > 1`) by construction because the producer mints each cursor with the
   * page label it addresses.
   *
   * Popping re-reads the previous page rather than replaying stored rows, which
   * is what makes Previous show the *same* rows page 1 showed: the cursor is
   * stable, `staleTime` is 3 s and React Query still holds that page's cache
   * entry under its own key, so the common case is a cache hit and the
   * uncommon case is a correct re-read.
   *
   * **For task 5.5:** changing the status filter MUST reset this to `[]`. A
   * cursor stays *redeemable* across a filter change — it is a key position, and
   * `run_cursor.decode_cursor` binds it to the pipeline, not to the filter — which
   * is precisely the hazard: the request would succeed while resuming a filter
   * walk from a position the new filter never walked to, so page 2 of
   * `status=SKIPPED` would start wherever the unfiltered read had got to. Wrong
   * rows, no error. Resetting is one `setCursorStack([])` beside the filter's own
   * setter.
   */
  const [cursorStack, setCursorStack] = useState<string[]>([]);
  /**
   * The derived status to filter on, or `ANY_STATUS` for unfiltered (p48 C5,
   * task 5.5). Sent as `?status=`; see {@link STATUS_FILTER_OPTIONS} for the set
   * and for the two values the server accepts but this control does not offer.
   */
  const [statusFilter, setStatusFilter] = useState<string>(ANY_STATUS);
  const cursor = cursorStack.length > 0 ? cursorStack[cursorStack.length - 1] : undefined;
  const execQuery = usePipelineExecutions(
    pipeline.pipeline_key,
    cursorStack.length + 1,
    cursor,
    statusFilter || undefined,
  );
  const { data: execData } = execQuery;
  /**
   * Change the filter and return to page 1 — the two halves are one action and
   * MUST NOT be separated (the hazard `cursorStack`'s docstring above records).
   *
   * A cursor is still *redeemable* after the filter changes: it is a key position
   * and `run_cursor.decode_cursor` binds it to the pipeline, not to the filter. So
   * keeping the stack would produce a **successful request for the wrong rows** —
   * page 2 of `status=SKIPPED` resuming from wherever the unfiltered walk had got
   * to. No error, plausible output, which is the failure class this spec exists to
   * remove. Written as one function rather than two setters at the call site so
   * there is one place to keep them together.
   */
  const changeStatusFilter = (next: string) => {
    setStatusFilter(next);
    setCursorStack([]);
  };
  const filterScopeTruncated =
    !!statusFilter && (execData?.pagination?.partial_counts?.includes('executions') ?? false);
  const cancelRun = useCancelRun();
  const approvalAction = useApprovalAction();
  const retryMutation = useRetryRun();
  const { showToast } = useToast();
  const [rejectModal, setRejectModal] = useState<{ approvalId: string } | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  const [pendingApprovalId, setPendingApprovalId] = useState<string | null>(null);
  const queuedFiles = execData?.queued_files ?? [];
  const queuedCount = queuedFiles.length;
  /**
   * p54 R10.3 — the **durable** lost-file record, from the response's own `lost_files`
   * array rather than from `queuedFiles`.
   *
   * `queuedFiles` is page 1 of a newest-first index, and a loss row's `started_at` is
   * the instant the loss was *recorded*, so it drops off page 1 within about an hour
   * while the record lives 30 days. Worse on this surface than on the Overview: the
   * pager can be on page 3, where `queued_files` has never carried anything at all.
   * `lost_files` is keyed and page-independent, so this region is the same on every
   * page and under every status filter.
   *
   * Filtered so a loss still visible in the Queued Files table above is not listed
   * twice; see `lostFilesNotAlreadyQueued`.
   */
  const lostFiles = lostFilesNotAlreadyQueued(execData?.lost_files, queuedFiles);

  return (
    <SpaceBetween size="s">
      {/* The status control (R4.1), and it sits OUTSIDE `AsyncContent` on
          purpose — this is the one place task 5.5 departs from 5.1's "beside
          `CursorPagination` in the same trailing block", and it departs for 5.1's
          own reason. `AsyncContent` *replaces* its children, so a control inside
          it unmounts on the loading, error and empty branches. Selecting a filter
          is a transition into exactly those states: a filter that matches nothing
          renders an empty result, and changing the filter changes the query key so
          `isLoading` is true again for the first fetch. A control that disappears
          in the state it just produced leaves the user with a filtered view and no
          way to clear it — the `ListPagination.tsx:11-16` stranding bug by a third
          route, after the two 5.1 already had to route around. Mounted here it is
          reachable in every state, so clearing the filter is always available.

          The pager keeps its place in the trailing block; it is navigation
          *within* a result and has nothing to offer when there is no result. */}
      <SpaceBetween size="xxs">
        <Select
          selectedOption={{
            value: statusFilter,
            label: statusFilterLabel(statusFilter),
          }}
          onChange={({ detail }) => changeStatusFilter(detail.selectedOption.value ?? ANY_STATUS)}
          options={STATUS_FILTER_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
          /* The control's NAME, for assistive technology. `ariaLabel` rather
             than visible text is the right instrument here and 5.2 drew the
             same distinction deliberately: this is a control, and what a
             screen reader is missing is what the widget is *for*. The
             control's *state* is already in words without any help — Cloudscape
             renders the selected option's label as the trigger's visible text,
             so "filtered to SKIPPED" and "All statuses" are both legible and
             announced. Nothing here is carried by colour, which is what
             `LGR-99050e6-1` (template / ratified) forbids: colour alone fails
             WCAG 1.4.1. */
          ariaLabel="Filter runs by status"
          data-testid="executions-status-filter"
        />
        {/* R4.3 — the honest statement about scope, and the ONLY `partial_counts`
            entry this control owns. `executions` means task 4.3's filter walk
            stopped with matches possibly still unread, so a page shorter than
            `page_size` no longer means "the filtered list ended". Presenting that
            as a complete filtered view is what R4.3 forbids. Next is always
            available when this fires: a truncated walk always leaves a
            `LastEvaluatedKey`, so `has_next` is true (`pipelines_runs.py:1141-1145`).

            `total_count` and `total_pages` are NOT this component's business —
            5.1's pager words those as a floor via `totalPagesIsFloor`, and saying
            it twice in two wordings would be two authorities on one fact.

            In words, not by styling — `LGR-99050e6-1` again, and the in-repo
            precedent for a written state statement is `RunDetailPage.tsx:391-397`. */}
        {filterScopeTruncated && (
          <Box variant="small" color="text-body-secondary" data-testid="executions-filter-scope">
            Showing {statusFilterLabel(statusFilter)} runs found so far. The search stopped
            before the end of this pipeline's history, so this page may be short and more
            matching runs may exist — use Next to keep looking.
          </Box>
        )}
      </SpaceBetween>
      <AsyncContent
        query={execQuery}
        errorTitle="Couldn't load executions"
        /* "This pipeline has no runs" is only true on the FIRST page, and the
           distinction is load-bearing rather than pedantic. `AsyncContent`
           replaces its children with this node, pager included — so treating an
           empty page N as empty would unmount the pager on exactly the page the
           user needs Previous on. That is the `ListPagination.tsx:11-16` bug
           reproduced by a different mechanism.

           An empty page N is reachable without anything being wrong: task 4.3
           records that under a status filter `has_next` is an UPPER bound — it
           means "more raw rows remain to walk", not "more matches exist" — so
           following the cursor can legitimately land on a page where every raw
           row was filtered out. Beyond page 1 the children render with an empty
           table, whose own `empty` slot below says so in words.

           `!statusFilter` is the same argument for a second mechanism (task 5.5).
           "This pipeline has no runs" is a claim about the PIPELINE, and a filter
           matching nothing says nothing about the pipeline — it says the filter
           found none of that status on the pages it walked. Without this clause a
           filtered page 1 with no matches would render the empty-history copy, and
           an empty match "is 0 rows with an accurate count, not an error" (task
           4.3), so the honest reading belongs in the table's `empty` slot where the
           filter is nameable. Dropping this clause is the mutation that collapses
           empty #3 into empty #1. */
        isEmpty={(d) =>
          cursorStack.length === 0 && !statusFilter && (d.executions?.length ?? 0) === 0
        }
        empty={<Box textAlign="center" color="text-body-secondary">No executions yet</Box>}
      >
        {(data) => (
          <SpaceBetween size="s">
            <Table
              items={data.executions ?? []}
              /* THREE empty states, three readings, and conflating any two is a
                 defect. This slot carries two of them; the third — the pipeline
                 has no runs at all — is `AsyncContent`'s `empty` above.

                 Deliberately NOT an error or an alert in either case. An empty
                 page is a normal outcome of following a cursor whose `has_next`
                 was an upper bound, and an empty match "is 0 rows with an
                 accurate count, not an error" (task 4.3). In words rather than by
                 styling, per `LGR-99050e6-1`.

                 #3 — the FILTER matched nothing (R4.1's "no runs match"). Named
                 with the status so it cannot be mistaken for #1's claim about the
                 pipeline, and it points at clearing the filter rather than at
                 Previous. The `has_next` arm matters here for a reason specific to
                 filtering: under a filter `has_next` means "more raw rows remain
                 to walk", so matches may sit further on and "none exist" would be
                 the wrong thing to say.

                 #2 — a later PAGE is empty, unfiltered. 5.1's copy, unchanged. */
              empty={
                <Box textAlign="center" color="text-body-secondary" data-testid="executions-empty-page">
                  {statusFilter
                    ? data.pagination.has_next
                      ? `No runs match ${statusFilterLabel(statusFilter)} on this page. Try Next for later pages, or clear the filter.`
                      : `No runs match ${statusFilterLabel(statusFilter)}. Clear the filter to see all runs.`
                    : data.pagination.has_next
                      ? 'No runs on this page. Try Next for later pages, or Previous to go back.'
                      : 'No runs on this page. Use Previous to go back.'}
                </Box>
              }
              columnDefinitions={[
          { id: 'file_name', header: 'File', cell: (item) => item.file_name || item.file_key || '—' },
          { id: 'started_at', header: 'Started', cell: (item) => formatRelativeTime(item.started_at) },
          {
            id: 'status', header: 'Status',
            cell: (item) => {
              // p30 R14.3 — a heal is indicated here, not only on the failed
              // run's detail page. `describeSelfHealing` keeps the four
              // outcomes distinct; see utils/self-healing.ts for why they must
              // not collapse into one "Healed" badge.
              const heal = describeSelfHealing(item.self_healing_status, item.self_healing_remediation);
              // p30 R10.2 — a Skipped_Run_Record must be distinguishable from a
              // failure. The STATUS already was (`statusType` has no 'skipped'
              // arm, so a skip renders 'info' where FAILED renders 'error');
              // what was missing is the REASON, which decides what an operator
              // should do about it. See utils/skip-reason.ts.
              const skip = describeSkip(item.status, item.skip_reason, item.skip_detail);
              // Any rerun trigger's source run. The dashboard retry path
              // writes `retry_source_run_id` on its own execution row; every
              // other rerun (self-healing) gets `original_run_id` propagated
              // from its file_validation identity row.
              const sourceRunId = item.retry_source_run_id || item.original_run_id;
              return (
                <SpaceBetween direction="horizontal" size="xxs">
                  <StatusIndicator type={statusType(item.status)}>
                    {item.status === 'WAITING_APPROVAL' ? 'Awaiting Approval' : item.status}
                  </StatusIndicator>
                  {/* p37 R1.7 / R1.9 — what reached the target, BESIDE the run's
                      status and never replacing it. A `partial_load` therefore
                      reads as "SUCCEEDED · N rows not written", which is D1: the
                      status stays a success and the withheld count is always
                      visible. Mounted unconditionally — the component itself
                      decides, and returns null for `complete_load` and
                      `not_established`, so a conditionally-absent child never
                      shifts the positions of this SpaceBetween's list. */}
                  <LoadOutcomeIndicator outcome={item.load_outcome} />
                  {item.trigger === 'retry' && (
                    <Badge color="blue" data-testid="retry-badge">Retry</Badge>
                  )}
                  {item.trigger === 'self_healing' && (
                    <span title="This run was started automatically by the self-healing agent to re-run the source run.">
                      <Badge color="blue" data-testid="self-healing-rerun-badge">Self-heal re-run</Badge>
                    </span>
                  )}
                  {heal && (
                    <span title={heal.detail}>
                      <Badge color={heal.color} data-testid="self-healing-badge">{heal.label}</Badge>
                    </span>
                  )}
                  {skip && (
                    <span title={skip.detail}>
                      <Badge color={skip.color} data-testid="skip-reason-badge">{skip.label}</Badge>
                    </span>
                  )}
                  {/* How many scheduled fires this one row stands for. Contention
                      skips collapse into one row per window (see
                      SKIPPED_RUN_NOTIFY_WINDOW_SECONDS), so without this the row
                      reads as a single miss when it may be twelve. */}
                  {skip && Number(item.skip_count ?? 0) > 1 && (
                    <Badge color="grey" data-testid="skip-count-badge">
                      &times;{Number(item.skip_count)}
                    </Badge>
                  )}
                  {item.current_step && item.status !== 'WAITING_APPROVAL' && (
                    <Box variant="small" color="text-body-secondary">
                      {item.status === 'RUNNING' ? `→ ${item.current_step}` : `(${item.current_step})`}
                    </Box>
                  )}
                  {item.status === 'WAITING_APPROVAL' && item.current_step && (
                    <Box variant="small" color="text-body-secondary">({item.current_step})</Box>
                  )}
                  {sourceRunId && (
                    <Link
                      href={`/pipelines/${pipeline.pipeline_key}/runs/${sourceRunId}`}
                      data-testid="source-run-link"
                      fontSize="body-s"
                    >
                      Source: {String(sourceRunId)}
                    </Link>
                  )}
                  {/* The heal re-run's REAL run_id, which only exists once the
                      re-run terminates. The agent's `rerun_id`
                      (`{original}-heal`) is lineage only — file_validation
                      mints a fresh run_id — so linking to it would be a dead
                      link. */}
                  {item.self_healing_linked_run_id && (
                    <Link
                      href={`/pipelines/${pipeline.pipeline_key}/runs/${item.self_healing_linked_run_id}`}
                      data-testid="self-healing-rerun-link"
                      fontSize="body-s"
                    >
                      Heal re-run: {String(item.self_healing_linked_run_id)}
                    </Link>
                  )}
                </SpaceBetween>
              );
            },
          },
          { id: 'ai_model', header: 'Model', cell: (item) => getModelDisplayName(item.ai_model || item.ai_model_used) },
          {
            id: 'schema_version', header: 'Schema Version',
            cell: (item) => item.schema_version != null ? <Badge>v{item.schema_version}</Badge> : '—',
          },
          {
            id: 'steps', header: 'Steps',
            cell: (item) => {
              const stepsObj = item.steps_status || item.steps;
              if (stepsObj && typeof stepsObj === 'object' && !Array.isArray(stepsObj)) {
                // steps_status keys are canonical snake_case on new runs and
                // legacy PascalCase on pre-rename runs (P28 task 14.2) —
                // orderStepKeys sorts either generation into execution order.
                return orderStepKeys(Object.keys(stepsObj)).map((name) => (
                  <span key={name} title={name}>{getStatusIcon(resolveStepStatus((stepsObj as Record<string, string>)[name], item.status))}</span>
                ));
              }
              if (Array.isArray(stepsObj)) {
                return stepsObj.map((s: { step_name: string; status: string }) => (
                  <span key={s.step_name} title={s.step_name}>{getStatusIcon(resolveStepStatus(s.status, item.status))}</span>
                ));
              }
              return '—';
            },
          },
          {
            id: 'actions', header: 'Actions',
            cell: (item) => {
              const isActive = item.status === 'RUNNING' || item.status === 'WAITING_APPROVAL' || item.status === 'IN_PROGRESS';
              const isWaiting = item.status === 'WAITING_APPROVAL' && item.pending_approval_id;
              const isFailed = item.status === 'FAILED';
              /**
               * Every run-scoped action addresses the run by `run_id` and by
               * nothing else (p51 C5, R4.2).
               *
               * All three used to read `item.run_id || item.execution_id`. That
               * fallback could not fire — `run_id` is the executions table's sort
               * key — and it was wrong if it ever had: `execution_id` holds the
               * Step Functions execution *name*, so it would have addressed a run
               * that does not exist. Deleted rather than left, per R4.3.
               *
               * Hoisted to a `const` because `run_id` is declared optional and
               * `retryMutation`/`cancelRun` both take a required `string`: this is
               * the narrowing the gates below rely on, and it survives into the
               * `onClick` closures where a narrowed property access would not.
               * Gating on it rather than asserting `item.run_id!` keeps the
               * unreachable case failing closed — no action offered — instead of
               * putting `undefined` into a request path.
               */
              const runId = item.run_id;
              return (
                <SpaceBetween direction="horizontal" size="xxs">
                  {isFailed && runId && (
                    <Button
                      variant="link"
                      onClick={() => {
                        retryMutation.mutate({ runId });
                      }}
                      loading={retryMutation.isPending}
                      data-testid="retry-action-button"
                    >
                      Retry
                    </Button>
                  )}
                  {isWaiting && (
                    <>
                      <Button
                        variant="primary"
                        onClick={() => {
                          setPendingApprovalId(item.pending_approval_id!);
                          approvalAction.mutate(
                            { id: item.pending_approval_id!, action: 'approve', comment: '' },
                            {
                              onSuccess: () => { setPendingApprovalId(null); showToast('Approved successfully', 'success'); },
                              onError: () => { setPendingApprovalId(null); showToast('Failed to approve', 'error'); },
                            },
                          );
                        }}
                        loading={approvalAction.isPending && pendingApprovalId === item.pending_approval_id}
                      >
                        Approve
                      </Button>
                      <Button
                        onClick={() => setRejectModal({ approvalId: item.pending_approval_id! })}
                      >
                        Reject
                      </Button>
                    </>
                  )}
                  {/* R6.1 — a skip row's navigation carries the row's own skip
                      fields, so the run-detail page can state what happened to
                      the run instead of denying it exists. A skip has no state
                      rows, so `get_run_details` correctly 404s (R6.3, R8.2 — p38
                      do-NOT 6 forbids relaxing that), and arm B2 has the frontend
                      recognise the 404 beside the row it already holds.
                      `buildSkippedRunNavState` returns `undefined` for every
                      non-skip row, which is byte-equivalent to today's stateless
                      navigate. Skip-ness comes from `status` + `skip_reason` and
                      never from the run id — do-NOT 4, two id shapes.
                      The `runId` gate above is untouched (do-NOT 12,
                      `LGR-958c751-1`): it is the narrowing this closure relies on,
                      and `execution_id` is not reintroduced as a run identifier. */}
                  {runId && (
                    <Button
                      variant="link"
                      onClick={() =>
                        navigate(`/pipelines/${pipeline.pipeline_key}/runs/${runId}`, {
                          state: buildSkippedRunNavState(item),
                        })
                      }
                    >
                      View Run
                    </Button>
                  )}
                  {isActive && !isWaiting && runId && (
                    <Button
                      variant="link"
                      onClick={() => {
                        cancelRun.mutate(runId, {
                          onSuccess: (response) => {
                            const toast = cancelOutcomeToast(response);
                            showToast(toast.message, toast.type);
                          },
                          onError: (error) => showToast(cancelFailureMessage(error), 'error'),
                        });
                      }}
                      loading={cancelRun.isPending}
                    >
                      Cancel
                    </Button>
                  )}
                </SpaceBetween>
              );
            },
          },
        ]}
            />
            {/* The pager (R2.6, C9). Gated on `has_next || has_prev`, which task
                4.3 asked for and which is the only gate that is correct under a
                cursor model.

                WHY NOT A COUNT. The old gate was
                `Math.ceil((total_count ?? 0) / 10) > 1`, and it was wrong twice
                over: `total_count` read `undefined` before task 4.1 repointed it
                at `pagination`, and even repointed it was `len(executions)` until
                task 3.3 made it exact. Both had to be true at once for a pager to
                appear, which is why R2.6 exists as its own requirement. It is
                *still* the wrong gate now that both are fixed: under a status
                filter `total_count` is a floor (`pipelines_runs.py:1107-1108`
                sets it to `len(executions)`), so a filtered page of three rows
                would compute one page and hide the pager while `has_next` is
                true. `has_next` is minted from the page query's own
                `LastEvaluatedKey`, independent of the count walk — task 3.3 kept
                it out of `partial_counts` for exactly this reason — so it stays
                exact when the count is a floor.

                WHY THIS DOES NOT REPRODUCE THE `ListPagination` BUG. That bug
                (`ListPagination.tsx:11-16`) was gating **visibility** on a
                per-page `has_more`, so the control vanished on the last page and
                stranded the user with no route back. `has_next || has_prev` is a
                different predicate: on the last page `has_prev` is true, so the
                control stays mounted and Previous stays reachable. Within the
                control, "no further page" is a `disabled` button rather than an
                absent one — the same fix, one level down.

                THE SINGLE-PAGE CASE. Both flags false, so no control renders. That
                is correct and not the stranding case: with one page there is
                nowhere to navigate to and nowhere to have come from, so there is
                no state the user can be stuck in. It is also the behaviour this
                page already had (`> 1`), so it is not a regression. The
                stranding bug needed a *transition* into a state with no control;
                here the state is reachable only as the initial one. */}
            {(data.pagination.has_next || data.pagination.has_prev) && (
              <CursorPagination
                /* Read off the wire, not counted here. `page` is the producer's
                   label (derived from the cursor) and `total_pages` is
                   `ceil(total_count / page_size)` computed once at the producer —
                   recomputing either would make this file a second authority on
                   the page count. `partial_counts` naming `total_pages` is how the
                   producer says that number is a floor; the control words it as
                   such (R4.3). The sibling `executions` entry is the status
                   filter's own truncation signal and belongs to the status control
                   (task 5.5), not to the pager. */
                page={data.pagination.page}
                totalPages={data.pagination.total_pages}
                totalPagesIsFloor={data.pagination.partial_counts.includes('total_pages')}
                hasNext={data.pagination.has_next}
                hasPrev={data.pagination.has_prev}
                onNext={() => {
                  const next = data.pagination.next_cursor;
                  // `has_next` is derived from `next_cursor !== null` at the
                  // producer, so a null here with Next enabled is impossible —
                  // guarded anyway, because pushing `null` would corrupt the stack
                  // and the failure would show up as a wrong page rather than as
                  // an error.
                  if (next) setCursorStack((stack) => [...stack, next]);
                }}
                onPrevious={() => setCursorStack((stack) => stack.slice(0, -1))}
                /* Not `isLoading`: React Query reports that only on the first
                   fetch for a key, and a revisited page is a cache hit. This is
                   the flag that is true while any page request is in flight, so
                   the control cannot be double-advanced past a cursor that has
                   not arrived. */
                disabled={execQuery.isFetching}
              />
            )}
          </SpaceBetween>
        )}
      </AsyncContent>

      {queuedCount > 0 && (
        <Container
          header={
            <Header variant="h3" counter={`(${queuedCount})`} info={<QueuedFilesAbout />}>
              Queued Files
            </Header>
          }
        >
          <Table
            variant="embedded"
            columnDefinitions={[
              { id: 'file_name', header: 'File', cell: (item) => item.file_name },
              { id: 'source_key', header: 'S3 Key', cell: (item) => <Box variant="small" color="text-body-secondary">{item.source_key}</Box> },
              { id: 'queued_at', header: 'Queued At', cell: (item) => item.queued_at ? formatRelativeTime(item.queued_at) : '—' },
              /* p54 R9.3 — this branch has existed since before p54 and had **zero
                 producers**: `_split_derive_and_filter` published `'queued'`
                 unconditionally, so "Failed (DLQ)" could never render. It has one
                 now. The literal moved into `utils/queued-file-state.ts` so this
                 surface and the Overview region read one predicate rather than two
                 spellings — the drift `LGR-9e76249-9` records as the cost of a
                 mirrored definition. Both arms carry words, so do-NOT 8 /
                 `LGR-99050e6-1` (colour alone fails WCAG 1.4.1) stays satisfied. */
              { id: 'status', header: 'Status', cell: (item) =>
                isLostQueuedFile(item.status)
                  ? <StatusIndicator type="error">Failed (DLQ)</StatusIndicator>
                  : <StatusIndicator type="pending">Queued</StatusIndicator>
              },
            ]}
            items={queuedFiles}
            empty={<Box textAlign="center" color="text-body-secondary">No files queued</Box>}
          />
          {/* p54 R9.2 — this read "Files queued — will process after current
              execution completes". That is an **unconditional promise**, and it is
              false from the moment a gate holds the pipeline lock: a run parked at a
              human approval gate can outlast the file's ~25-minute Redelivery_Budget,
              after which the file goes to the DLQ and is never processed. It was also
              the sentence an operator read while that was happening.

              It now states what is **known** — a run is holding the pipeline — with
              no claim about when, which is honest for a short lock hold and for a
              long one alike. That sentence is an explanation, so it sits behind the
              header's question-mark button (`QueuedFilesAbout`). The sentence below
              stays on the page: it is conditional on there actually being a
              Lost_File, so a page with only deferred files makes no mention of loss,
              and it reports that something went wrong — the reason R9.4 asks for, in
              words rather than in the Status cell's colour (do-NOT 8,
              `LGR-99050e6-1`). */}
          {queuedFiles.some((f) => isLostQueuedFile(f.status)) && (
            <Box variant="small" color="text-status-error">
              Failed (DLQ) means that file waited longer than its redelivery attempts
              allowed and was never processed. It will not be retried automatically.
            </Box>
          )}
        </Container>
      )}

      {/* Lost Files — p54 R10.3. Outside the `AsyncContent` above on purpose: it is a
          statement about the pipeline, not about the page being viewed, and it must not
          disappear when the pager moves or a status filter is applied. Its own region
          rather than rows in the Queued Files table, because these files are not queued
          for anything — see the `lostFiles` derivation, and `lostFilesNotAlreadyQueued`
          for why the table above keeps its own lost arm. Same vocabulary as that arm so
          one outcome cannot read two ways, and every distinction is in words rather
          than colour alone (`LGR-99050e6-1`, `LGR-1f7892b-2`). */}
      {lostFiles.length > 0 && (
        <Container header={<Header variant="h3" counter={`(${lostFiles.length})`}>Lost Files</Header>}>
          <Table
            variant="embedded"
            trackBy="source_key"
            columnDefinitions={[
              { id: 'file_name', header: 'File', cell: (item) => item.file_name || '—' },
              { id: 'source_key', header: 'S3 Key', cell: (item) => <Box variant="small" color="text-body-secondary">{item.source_key}</Box> },
              /* "Recorded", not "Lost At": the timestamp is when the loss was
                 *recorded*, hours after the upload — the original queue instant is not
                 recoverable from a dead-lettered message and the producer declines to
                 invent one. */
              { id: 'lost_at', header: 'Recorded', cell: (item) => item.lost_at ? formatRelativeTime(item.lost_at) : '—' },
              { id: 'status', header: 'Status', cell: () => <StatusIndicator type="error">Failed (DLQ)</StatusIndicator> },
            ]}
            items={lostFiles}
            /* No retry, cancel or View Run action on these rows. `lost_files` carries no
               `run_id` — nothing was ever started for these files — so there is nothing
               to navigate to and nothing to cancel. */
            empty={<Box textAlign="center" color="text-body-secondary">No files lost</Box>}
          />
          {/* The producer's own account of what happened, deduplicated rather than taken
              from the first row, with a generic fallback for a record carrying no
              reason — see `distinctLostReasons`. */}
          {distinctLostReasons(lostFiles).map((reason) => (
            <Box key={reason} variant="small" color="text-status-error" padding={{ top: 'xs' }}>
              {reason}
            </Box>
          ))}
        </Container>
      )}

      {rejectModal && (
        <Modal
          visible
          onDismiss={() => { setRejectModal(null); setRejectReason(''); }}
          header="Reject Approval"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => { setRejectModal(null); setRejectReason(''); }}>Cancel</Button>
                <Button
                  variant="primary"
                  onClick={() => {
                    setPendingApprovalId(rejectModal.approvalId);
                    approvalAction.mutate(
                      { id: rejectModal.approvalId, action: 'reject', comment: rejectReason },
                      {
                        onSuccess: () => {
                          setPendingApprovalId(null);
                          showToast('Rejected successfully', 'success');
                          setRejectModal(null);
                          setRejectReason('');
                        },
                        onError: () => { setPendingApprovalId(null); showToast('Failed to reject', 'error'); },
                      },
                    );
                  }}
                  loading={approvalAction.isPending && pendingApprovalId === rejectModal.approvalId}
                >
                  Reject
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Textarea
            value={rejectReason}
            onChange={({ detail }) => setRejectReason(detail.value)}
            placeholder="Enter rejection reason (optional)"
            rows={3}
          />
        </Modal>
      )}
    </SpaceBetween>
  );
}
