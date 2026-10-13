// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import { Link } from 'react-router-dom';
import Header from '@cloudscape-design/components/header';
import Table from '@cloudscape-design/components/table';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Modal from '@cloudscape-design/components/modal';
import Textarea from '@cloudscape-design/components/textarea';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Container from '@cloudscape-design/components/container';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import {
  useApprovals,
  useApprovalAction,
  useApprovalDetail,
  APPROVAL_DETAILS_ABSENT_STATEMENT,
  APPROVAL_DETAILS_UNREADABLE_STATEMENT,
} from '../hooks';
import { calculateWaitingTime, statusType } from '../utils/formatting';
import { useToast } from '../contexts/ToastContext';
import ApprovalArtifact from '../components/approvals/ApprovalArtifact';
import ListPagination from '../components/common/ListPagination';
import { mappingCountText } from '../utils/approval-mapping-count';
import {
  approvalUnsourcedTargetFields,
  unsourcedTargetFieldLine,
  UNSOURCED_NONE,
  UNSOURCED_NOT_ESTABLISHED,
} from '../utils/approval-unsourced-target-fields';
import {
  reusedMappingDisplay,
  reusedMappingIdText,
  REUSED_MAPPING_ID_LABEL,
  REUSED_MAPPING_STATEMENT,
  REUSED_MAPPING_UNREADABLE_STATEMENT,
} from '../utils/approval-reused-mapping';
import {
  approvalProposedRenames,
  proposedRenameLine,
  RENAMES_NONE,
  RENAMES_NOT_ESTABLISHED,
} from '../utils/rename-manifest';
import type { Approval } from '../types';

function formatDate(iso: string | undefined): string {
  if (!iso) return '\u2014';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

/** Renders type-specific detail content in the review modal. */
function ApprovalTypeDetails({ approval }: { approval: Approval }) {
  const details = approval.details ?? {};
  const type = approval.approval_type?.toLowerCase();

  if (type === 'schema_mapping') {
    // p50 task 4.2 (C2, R1.4/R1.5, do-NOT 7). This line read
    // `(details.mapping_count as number) ?? 0`, which reported an unreadable
    // payload as a confident `0` — the exact collapse `LGR-4854b3d-1`
    // (**template / ratified**) refused for `field_count`, and the exact wrong
    // answer `LGR-9a35b2d-2` measured on the sibling listing surface
    // (`mapping_count: 0` for a pipeline whose `schema_mapping` row carried 7).
    // The sibling render in `ApprovalWorkflowSection` was already correct, so the
    // two sites had diverged; the expression now lives in one module for both.
    // Zero mappings and an unreadable payload are different facts (R1.5).
    const mappingCount = mappingCountText(details);
    // p50 task 5.3. This read `(details.unmapped_fields as number) ?? 0` and
    // rendered the result raw. The producer writes a DICT
    // (`schema_mapping/lambda_function.py:1740-1744`), so once task 2.1 pointed
    // `get_approval_data` at `output_data.approval` the key arrived populated
    // and the WHOLE MODAL died: `Objects are not valid as a React child (found:
    // object with keys {source, target, target_unsourced, target_required})`,
    // measured at the render. Before 2.1 the key was `null` and `?? 0` rendered
    // a false `0` instead of crashing.
    //
    // `utils/approval-unsourced-target-fields.ts` routes the payload to
    // `unsourcedTargetFields` — the single reader (`LGR-9e76249-7`,
    // `LGR-7561acb-1`) — and applies `field-requirement.ts`'s words
    // (`LGR-7561acb-4`). No count is derived here: there is no such number on
    // the wire, and inventing one is what those records forbid.
    const unsourcedTargets = approvalUnsourcedTargetFields(details);
    // Three-state, and there is NO PRODUCER for the singular key: the corrected
    // payload carries `confidence_scores` (plural, per-field). `?? 0` therefore
    // claimed a measured `0%` on every single approval. Same expression the
    // evidence cards already use (`ApprovalWorkflowSection.tsx:329`).
    const confidence = details.confidence_score;
    const confidenceText =
      typeof confidence === 'number' && Number.isFinite(confidence)
        ? `${(confidence * 100).toFixed(0)}%`
        : '\u2014';
    // RECORDED, NOT FIXED (task 5.3). This cast is wrong too — the producer's
    // elements are `{source_field, target_field}`, so the table below now
    // renders a row with blank Source and Target cells, measured. `MappingViewer`
    // (in `ApprovalArtifact`, above this container in the same modal) already
    // renders the same list correctly through the one tolerant reader. Which of
    // the two duplicate tables survives is a display decision, not a cast
    // correction, so it is left for a task that can size it. See
    // `SchemaMappingApprovalDetails.proposed_mappings` in `types.ts`.
    const proposedMappings = (details.proposed_mappings as Array<{ source: string; target: string; confidence?: number }>) ?? [];
    // p54 task 4.4 (R1.4, R2.1, R2.2). Same reader as
    // `ApprovalWorkflowSection`'s Mapping Preview card, and the sentences come
    // from it too: `LGR-4854b3d-1` ruled the expression cannot be rewritten per
    // display site, and `LGR-e59edd7-6` applied that ruling to THESE TWO
    // SURFACES for `unmapped_fields` after they had already diverged on
    // `mapping_count` — one `?? '—'`, one `?? 0`. Two surfaces that each phrase
    // one fact are two surfaces that can disagree about it.
    //
    // AMENDED by p56 — the `details` caveat that stood here is now WRONG, and it
    // is corrected rather than deleted so the change reads as deliberate
    // (`LGR-60c4985-6`, `LGR-5906ec6-2`, this repo's convention for a stated
    // reversal). It said: "`details` IS UNDEFINED ON THIS SURFACE TODAY …
    // neither `get_approvals` (`approvals.py:339`) nor `get_pending_approvals`
    // (`:399`) parses `approval_data` … so this arm is currently unreachable from
    // live data and is exercised by a fixture supplying `details` directly."
    //
    // That was true of the LISTING endpoints and still is — neither was widened.
    // What changed is that this modal no longer reads its payload from them:
    // `GET /api/approvals/{approval_id}` (`approvals.py::get_approval`) parses
    // `approval_data` for the one row being reviewed, and `ApprovalsPage` fetches
    // it on open. So this arm is now reachable from live data, and the fixture
    // tests that exercised it were testing the real shape all along.
    const reuse = reusedMappingDisplay(details);
    // p55 task 6.2 (R10.1, R10.2). Same reader as the artifact panel above it in
    // this same modal and as `ApprovalWorkflowSection`'s card —
    // `utils/rename-manifest.ts` — for the reason `LGR-4854b3d-1` (**template /
    // ratified**) and `LGR-e59edd7-6` between them settled for the two lines
    // above: the expression cannot be rewritten per display site, because two
    // surfaces that each phrase one fact are two surfaces that can disagree about
    // it, and these same two surfaces have already done so twice
    // (`mapping_count`, then `unmapped_fields`).
    //
    // Three-state here, unlike the artifact panel, and for the reason the panel's
    // own header gives: this surface has a LABEL, and a blank beside a label reads
    // as a measured "no renames proposed". The panel has no label without its
    // container, so it renders nothing rather than an em dash.
    //
    // AMENDED by p56, same correction as the block above and stated for the same
    // reason. This said: "the list endpoints do not parse `approval_data`, so this
    // arm is fixture-exercised until they do. Widening them is not this task's."
    // The list endpoints still do not parse it and were not widened — the modal
    // now reads a per-id endpoint that does, so this arm is reachable from live
    // data. Note what that does NOT change: `rename_manifest` is the payload's
    // eleventh key, so an approval whose `approval_data` predates it still arrives
    // without it and `approvalProposedRenames` still answers *not established* —
    // correctly, and that is a producer fact rather than a read failure.
    const proposedRenames = approvalProposedRenames(details);
    return (
      <Container header={<Header variant="h3">Mapping Preview</Header>}>
        <SpaceBetween size="s">
          {/*
            p54 task 4.4. Above the `ColumnLayout` rather than as a fourth cell
            in it: a fourth column would perturb the three cells p50 task 5.3
            settled, and the reuse fact is not a measurement of the mapping — it
            is a statement about which mapping is being shown at all.

            Absence renders NOTHING. A payload that states no reuse gets no
            block, because `false` here would assert the run generated its own
            mapping — do-NOT 7's collapse. Words carry both distinctions, not
            colour (do-NOT 8, `LGR-99050e6-1` WCAG 1.4.1, `LGR-1f7892b-2`).
          */}
          {reuse.stated && (
            <div data-testid="approvals-reused-mapping">
              {reuse.reused && <Box>{REUSED_MAPPING_STATEMENT}</Box>}
              <div>
                <Box variant="awsui-key-label">{REUSED_MAPPING_ID_LABEL}</Box>
                <span data-testid="approvals-reused-mapping-id">
                  {reusedMappingIdText(details)}
                </span>
              </div>
              {/*
                R1.4's second sentence. Without it the unreadable payload renders
                as a mapping with no rows — `MappingViewer` above this container
                receives `mappings: []` from `toMappingData`, and the duplicate
                table below renders nothing at all — which is
                `LGR-b2f79ef-14`'s named reference to something absent presented
                as a thing with no fields. Fires on the producer's marker and on
                task 2.3's third route; see the reader's header.
              */}
              {reuse.unreadable && (
                <Box>
                  <span data-testid="approvals-reused-mapping-unreadable">
                    {REUSED_MAPPING_UNREADABLE_STATEMENT}
                  </span>
                </Box>
              )}
            </div>
          )}
          <ColumnLayout columns={3} variant="text-grid">
            {/*
              The test id is new. The label and the value are sibling nodes with
              no separator between them, so reading the value off rendered text
              means guessing at the ColumnLayout's order; a stable id lets the
              three-state assertions name the value directly, the way
              `data-testid="audit-actor"` does for the identity sites.
            */}
            <div><Box variant="awsui-key-label">Mapping count</Box><span data-testid="approvals-mapping-count">{mappingCount}</span></div>
            {/*
              The label says "target" because that is all this payload supports.
              `unmapped_fields.source` carries bare names of source columns
              nothing consumes — a different fact about a different schema — and
              `unsourcedTargetFields` never reads it (p45 do-NOT 19). Labelling
              the target roster "Unmapped fields" was part of what made a dict
              look like it ought to be a count.
            */}
            <div>
              <Box variant="awsui-key-label">Unsourced target fields</Box>
              {/*
                A `div`, not a `span`: the populated branch is a `ul`, which is
                flow content and is not valid inside phrasing content. The count
                cell above can stay a `span` because its child is only ever text.
              */}
              <div data-testid="approvals-unsourced-target-fields">
                {unsourcedTargets === null ? (
                  UNSOURCED_NOT_ESTABLISHED
                ) : unsourcedTargets.length === 0 ? (
                  UNSOURCED_NONE
                ) : (
                  <ul style={{ margin: 0, paddingInlineStart: '1.2em' }}>
                    {unsourcedTargets.map((entry) => (
                      <li key={entry.field}>{unsourcedTargetFieldLine(entry)}</li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
            <div><Box variant="awsui-key-label">Confidence score</Box><span data-testid="approvals-confidence-score">{confidenceText}</span></div>
          </ColumnLayout>
          {/*
            p55 R10.1 / R10.2 — both names of every Proposed_Rename.

            OUTSIDE the `ColumnLayout columns={3}` above rather than a fourth cell
            in it: a fourth child starts a second row of three, which would leave
            two empty cells beside it and put this list in a third of the width it
            needs to show two names per line. The reuse block above this container
            is the in-file precedent for a full-width labelled field.

            A `div`, not a `span`, for the same reason the unsourced cell above is
            one: the populated branch is a `ul`, which is flow content and is not
            valid inside phrasing content.

            Both names are React TEXT CONTENT, which escapes — `original_name` is
            an attacker-controlled CSV header (p55 do-NOT 17), and it goes into no
            `title`, no URL and no `dangerouslySetInnerHTML`. Length is capped and
            the truncation marked by `proposedRenameLine`'s `displayName` calls.

            The `key` is `original_name`: the ORIGINALS are what the producer
            guarantees distinct (one entry per distinct non-conforming column),
            whereas two originals can legitimately map to targets differing only by
            R10.4's suffix — so keying on the target would be keying on the derived
            value rather than the identity.
          */}
          <div>
            <Box variant="awsui-key-label">Proposed source column renames</Box>
            <div data-testid="approvals-proposed-renames">
              {proposedRenames === null ? (
                RENAMES_NOT_ESTABLISHED
              ) : proposedRenames.length === 0 ? (
                RENAMES_NONE
              ) : (
                <ul style={{ margin: 0, paddingInlineStart: '1.2em' }}>
                  {proposedRenames.map((entry) => (
                    <li key={entry.original_name}>{proposedRenameLine(entry)}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
          {proposedMappings.length > 0 && (
            <Table
              variant="embedded"
              columnDefinitions={[
                { id: 'source', header: 'Source', cell: (m) => m.source },
                { id: 'target', header: 'Target', cell: (m) => m.target },
                { id: 'conf', header: 'Confidence', cell: (m) => m.confidence != null ? `${(m.confidence * 100).toFixed(0)}%` : '\u2014' },
              ]}
              items={proposedMappings}
            />
          )}
        </SpaceBetween>
      </Container>
    );
  }

  if (type === 'code_generation') {
    const codeSummary = (details.code_summary as string) ?? 'No code summary available';
    const testResults = (details.test_results as string) ?? '';
    const qualityMetrics = (details.quality_metrics as Record<string, unknown>) ?? {};
    return (
      <Container header={<Header variant="h3">Code Summary</Header>}>
        <SpaceBetween size="s">
          <div><Box variant="awsui-key-label">Summary</Box><pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{codeSummary}</pre></div>
          {testResults && <div><Box variant="awsui-key-label">Test results</Box>{testResults}</div>}
          {Object.keys(qualityMetrics).length > 0 && (
            <div>
              <Box variant="awsui-key-label">Quality metrics</Box>
              {Object.entries(qualityMetrics).map(([k, v]) => (
                <div key={k}>{k}: {String(v)}</div>
              ))}
            </div>
          )}
        </SpaceBetween>
      </Container>
    );
  }

  if (type === 'first_run') {
    // `infrastructure_status` is `null` on every payload today — the read is one
    // of three wrong-level reads `get_approval_data`'s `first_run` branch records
    // and deliberately leaves alone (`approval_handler/lambda_function.py:
    // 453-487`), and the writer stores a DICT under `output_data`. So the cast
    // stays `string` and the fallback stays: re-pointing that read is the other
    // task's, and it would have to widen this line at the same time.
    const infraStatus = (details.infrastructure_status as string | null) ?? 'Unknown';
    // p50 task 5.3. This read `(details.quality_checks as string) ?? ''`. Task
    // 5.1 pointed the read at the producer's own
    // `pre_run_validation.quality_checks`
    // (`create_quality_checks/lambda_function.py:307-312`), which is a LIST of
    // human-readable lines — and React renders a string array concatenated with
    // no separators. Measured at the render before this change:
    //   "5 quality rules configuredData type validations enabledNull value
    //    checks configuredCompleteness validation active"
    // A non-empty array is truthy, so the guard below let it through.
    //
    // Rendered as a list because it is one. NOTE, and do not write a label
    // implying otherwise: only the FIRST line is derived from anything ("N
    // quality rules configured"); the other three are unconditional literals in
    // the producer, so a run with zero rules still says "Null value checks
    // configured". Task 5.1 recorded that as a producer defect and left it, and
    // this render must not dress the three literals up as measurements.
    const qualityChecks = Array.isArray(details.quality_checks)
      ? details.quality_checks.filter((line): line is string => typeof line === 'string')
      : [];
    const estimatedCosts = (details.estimated_costs as string | null) ?? '';
    return (
      <Container header={<Header variant="h3">Infrastructure Status</Header>}>
        <SpaceBetween size="s">
          <div><Box variant="awsui-key-label">Infrastructure status</Box>{infraStatus}</div>
          {qualityChecks.length > 0 && (
            <div>
              <Box variant="awsui-key-label">Quality checks</Box>
              <ul data-testid="approvals-quality-checks" style={{ margin: 0, paddingInlineStart: '1.2em' }}>
                {qualityChecks.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </div>
          )}
          {estimatedCosts && <div><Box variant="awsui-key-label">Estimated costs</Box>{estimatedCosts}</div>}
        </SpaceBetween>
      </Container>
    );
  }

  return null;
}

/**
 * The reviewed approval's payload, fetched on modal open.
 *
 * ## Why the fetch exists
 *
 * Neither listing endpoint parses the stored `approval_data`, so `approval.details`
 * was `undefined` for every row on this surface and every reader below correctly
 * reported "not established". `GET /api/approvals/{approval_id}` parses it for the
 * one row being reviewed. Parsing it in the listing instead was rejected: the
 * unfiltered listing enumerates four statuses and paginates every row with no cap,
 * so it would ship every approval's full `proposed_mappings` on every page load to
 * serve a modal opened for one row — the shape `LGR-14d6dd3-2` (**template /
 * ratified**) moved off the schema listing onto `GET /api/schemas/{id}`.
 *
 * ## Async_State_Triple (p29 R1)
 *
 * Loading, error-with-retry and genuinely-established-nothing are three distinct
 * states, following `TestResultsViewer` and `StatsBar`. **A failed read must not
 * render as an approval with no mappings** — that is the whole point: the readers
 * below cannot tell a fetch that never returned from a payload that stated nothing,
 * so the distinction has to be made here, before they run. The Retry is wired to
 * the query's own `refetch`, matching every other surface.
 *
 * ## Why the header fields are NOT gated on this fetch
 *
 * `ApprovalsPage` renders Pipeline / Type / Created / Status from the listing row
 * it already holds, so the modal opens populated and only the payload arrives late.
 * Gating the whole modal would make four fields we already have wait on a read
 * they do not need.
 *
 * ## What this does not do
 *
 * It re-implements no reader. `ApprovalArtifact` and {@link ApprovalTypeDetails}
 * are passed the fetched approval and read it exactly as they read a run-detail
 * approval — `mappingCountText`, `approvalUnsourcedTargetFields`,
 * `approvalProposedRenames`, `reusedMappingDisplay` and `MappingViewer` are all
 * untouched. `LGR-4854b3d-1` and `LGR-e59edd7-6` (both **template / ratified**)
 * both hold that two surfaces phrasing one fact will diverge, and these two
 * surfaces have already done so twice — `mapping_count`, then `unmapped_fields`.
 */
function ApprovalReviewBody({ approval }: { approval: Approval }) {
  const detailQuery = useApprovalDetail(approval.approval_id);

  if (detailQuery.isLoading) {
    return (
      <Box data-testid="approval-detail-loading" textAlign="center">
        <SpaceBetween size="xs" direction="horizontal">
          <Spinner />
          <span>Loading the approval payload</span>
        </SpaceBetween>
      </Box>
    );
  }

  if (detailQuery.isError || !detailQuery.data) {
    // NOT a fall-through to the readers. Without this branch a failed read renders
    // as `details === undefined`, which every reader below reports as "not
    // established" — indistinguishable from a payload the producer never wrote,
    // and an approver would sign off believing they had been shown the truth
    // about the mapping. `LGR-b2f79ef-14`'s "could not check vs checked and
    // passed" confusion, on the surface where the cost is a wrong approval.
    return (
      <Alert type="error" data-testid="approval-detail-error" header="Could not load the approval payload">
        <SpaceBetween size="s">
          <span>
            The mapping, code and quality details for this approval could not be loaded, so nothing
            is shown about them below. This is not a statement that the approval has no payload.
          </span>
          <Box>
            <Button data-testid="approval-detail-retry" onClick={() => detailQuery.refetch()}>
              Retry
            </Button>
          </Box>
        </SpaceBetween>
      </Alert>
    );
  }

  const detail = detailQuery.data;

  return (
    <SpaceBetween size="l">
      {/*
        The two non-parsed states, said in words above the readers that cannot
        tell them apart. `details` is omitted by the handler on both, so every
        reader below already answers *not established* — correctly. What it cannot
        do is say WHICH of the two happened, and those are different facts: one is
        about the producer, one is about the read. `LGR-4854b3d-1` forbids
        collapsing a three-state value onto one of its ends, and `{}` for an
        unreadable payload would read as "the producer stored nothing".

        Absence of a banner is the third state: the payload parsed. Nothing is
        rendered then, because a "payload read successfully" notice is noise beside
        the payload itself.
      */}
      {detail.details_status === 'absent' && (
        <Alert type="info" data-testid="approval-detail-absent">
          {APPROVAL_DETAILS_ABSENT_STATEMENT}
        </Alert>
      )}
      {detail.details_status === 'unreadable' && (
        <Alert type="warning" data-testid="approval-detail-unreadable">
          {APPROVAL_DETAILS_UNREADABLE_STATEMENT}
        </Alert>
      )}

      {/* The artifact being approved — rendered BEFORE the approve/reject
          buttons (in the footer) so sign-off is informed (R2.1/2.5/2.6). */}
      <ApprovalArtifact approval={detail} />

      <ApprovalTypeDetails approval={detail} />
    </SpaceBetween>
  );
}

export default function ApprovalsPage() {
  const approvalsQuery = useApprovals();
  const { isLoading, isError, refetch, hasNextPage, isFetchingNextPage, fetchNextPage } = approvalsQuery;
  const approvalAction = useApprovalAction();
  const { showToast } = useToast();
  const [selected, setSelected] = useState<Approval | null>(null);
  const [comment, setComment] = useState('');
  const [quickConfirm, setQuickConfirm] = useState<{ approval: Approval; action: 'approve' | 'reject' } | null>(null);
  const [rejectReason, setRejectReason] = useState('');
  // 1-based UI page index. Each React Query page maps to one UI page; server
  // page size (~50) is honored when the backend supports token pagination (R3.1).
  const [pageIndex, setPageIndex] = useState(1);

  // Flatten the fetched pages. `pages[i].approvals` is the i-th server page.
  const pages = approvalsQuery.data?.pages ?? [];
  const pagesCount = Math.max(pages.length, 1);
  const currentPageIndex = Math.min(pageIndex, pagesCount);
  const approvals = pages[currentPageIndex - 1]?.approvals ?? [];
  const totalFetched = pages.reduce((sum, page) => sum + page.approvals.length, 0);

  // Show the pager whenever pagination is meaningful — more than one fetched
  // page, or another page can still be fetched behind a token. The pager is NOT
  // gated on being on the last page, so it never strands the user (R3.3).
  const showPager = !isLoading && !isError && (pages.length > 1 || Boolean(hasNextPage));

  const handlePageChange = (nextIndex: number) => {
    // Advancing past the last fetched page pulls the next server page (R3.1).
    if (nextIndex > pages.length && hasNextPage && !isFetchingNextPage) {
      void fetchNextPage();
    }
    setPageIndex(nextIndex);
  };

  const handleAction = (action: 'approve' | 'reject', commentText?: string) => {
    if (!selected) return;
    approvalAction.mutate(
      { id: selected.approval_id, action, comment: commentText ?? comment, runId: selected.run_id },
      {
        onSuccess: () => {
          showToast(`Approval ${action === 'approve' ? 'approved' : 'rejected'} successfully`, 'success');
          setSelected(null);
          setComment('');
        },
        onError: () => {
          showToast(`Failed to ${action} approval`, 'error');
        },
      },
    );
  };

  const handleQuickAction = () => {
    if (!quickConfirm) return;
    const { approval, action } = quickConfirm;
    const reason = action === 'reject' ? rejectReason : '';
    approvalAction.mutate(
      { id: approval.approval_id, action, comment: reason, runId: approval.run_id },
      {
        onSuccess: () => {
          showToast(`Approval ${action === 'approve' ? 'approved' : 'rejected'} successfully`, 'success');
          setQuickConfirm(null);
          setRejectReason('');
        },
        onError: () => {
          showToast(`Failed to ${action} approval`, 'error');
          setQuickConfirm(null);
          setRejectReason('');
        },
      },
    );
  };

  return (
    <SpaceBetween size="l">
      <Table
        columnDefinitions={[
          { id: 'type', header: 'Type', cell: (a: Approval) => a.approval_type },
          {
            id: 'pipeline',
            header: 'Pipeline',
            // Row link to the pipeline detail page (R3.2).
            cell: (a: Approval) =>
              a.pipeline_key ? (
                <Link to={`/pipelines/${a.pipeline_key}`}>{a.pipeline_key}</Link>
              ) : (
                '\u2014'
              ),
          },
          {
            id: 'run',
            header: 'Run',
            // Row link to the run detail page (R3.2); route shape mirrors
            // App.tsx: /pipelines/:id/runs/:runId.
            cell: (a: Approval) =>
              a.run_id && a.pipeline_key ? (
                <Link to={`/pipelines/${a.pipeline_key}/runs/${a.run_id}`}>{a.run_id}</Link>
              ) : (
                '\u2014'
              ),
          },
          {
            id: 'status',
            header: 'Status',
            cell: (a: Approval) => (
              <StatusIndicator type={statusType(a.status)}>{a.status}</StatusIndicator>
            ),
          },
          { id: 'created', header: 'Created', cell: (a: Approval) => formatDate(a.created_at) },
          {
            id: 'waiting',
            header: 'Waiting',
            cell: (a: Approval) =>
              a.status === 'pending'
                ? `Waiting: ${calculateWaitingTime(a.created_at)}`
                : '\u2014',
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (a: Approval) =>
              a.status === 'pending' ? (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="primary" onClick={() => setQuickConfirm({ approval: a, action: 'approve' })}>
                    Quick Approve
                  </Button>
                  <Button onClick={() => setQuickConfirm({ approval: a, action: 'reject' })}>
                    Quick Reject
                  </Button>
                  <Button variant="link" onClick={() => setSelected(a)}>Review</Button>
                </SpaceBetween>
              ) : '\u2014',
          },
        ]}
        items={approvals}
        loading={isLoading}
        loadingText="Loading approvals"
        pagination={
          showPager ? (
            <ListPagination
              currentPageIndex={currentPageIndex}
              pagesCount={pagesCount}
              openEnd={Boolean(hasNextPage)}
              disabled={isFetchingNextPage}
              onChange={handlePageChange}
            />
          ) : undefined
        }
        header={
          <Header variant="h1" counter={`(${totalFetched}${hasNextPage ? '+' : ''})`}>
            Pending Approvals
          </Header>
        }
        empty={
          isError ? (
            <Box textAlign="center" color="inherit">
              <SpaceBetween size="s">
                <span>Failed to load approvals.</span>
                <Box>
                  <Button data-testid="approvals-retry" onClick={() => refetch()}>Retry</Button>
                </Box>
              </SpaceBetween>
            </Box>
          ) : (
            <Box textAlign="center" color="inherit">No pending approvals.</Box>
          )
        }
      />

      {/* Quick approve confirmation dialog */}
      {quickConfirm && quickConfirm.action === 'approve' && (
        <Modal
          visible
          onDismiss={() => setQuickConfirm(null)}
          header="Confirm Quick Approve"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setQuickConfirm(null)}>Cancel</Button>
                <Button variant="primary" onClick={handleQuickAction} loading={approvalAction.isPending}>
                  Approve
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="s">
            <Box>Are you sure you want to approve this {quickConfirm.approval.approval_type} for pipeline <strong>{quickConfirm.approval.pipeline_key}</strong>?</Box>
          </SpaceBetween>
        </Modal>
      )}

      {/* Quick reject dialog with reason prompt */}
      {quickConfirm && quickConfirm.action === 'reject' && (
        <Modal
          visible
          onDismiss={() => { setQuickConfirm(null); setRejectReason(''); }}
          header="Quick Reject"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => { setQuickConfirm(null); setRejectReason(''); }}>Cancel</Button>
                <Button variant="primary" onClick={handleQuickAction} loading={approvalAction.isPending}>
                  Reject
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="s">
            <Box>Rejecting {quickConfirm.approval.approval_type} for pipeline <strong>{quickConfirm.approval.pipeline_key}</strong>.</Box>
            <Textarea
              value={rejectReason}
              onChange={({ detail }) => setRejectReason(detail.value)}
              placeholder="Enter rejection reason"
              rows={3}
            />
          </SpaceBetween>
        </Modal>
      )}

      {/* Full review modal with type-specific details */}
      {selected && (
        <Modal
          visible
          onDismiss={() => { setSelected(null); setComment(''); }}
          size="large"
          header={`Review: ${selected.approval_type}`}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => { setSelected(null); setComment(''); }}>Cancel</Button>
                <Button
                  variant="normal"
                  onClick={() => handleAction('reject')}
                  loading={approvalAction.isPending}
                >
                  Reject
                </Button>
                <Button
                  variant="primary"
                  onClick={() => handleAction('approve')}
                  loading={approvalAction.isPending}
                >
                  Approve
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="l">
            <ColumnLayout columns={2} variant="text-grid">
              <div><Box variant="awsui-key-label">Pipeline</Box>{selected.pipeline_key}</div>
              <div><Box variant="awsui-key-label">Type</Box>{selected.approval_type}</div>
              <div><Box variant="awsui-key-label">Created</Box>{formatDate(selected.created_at)}</div>
              <div><Box variant="awsui-key-label">Status</Box><StatusIndicator type={statusType(selected.status)}>{selected.status}</StatusIndicator></div>
            </ColumnLayout>

            {/*
              The artifact and the type-specific details both read
              `approval.details`, which the LISTING row does not carry — so they
              are rendered by `ApprovalReviewBody`, which fetches the payload for
              this one approval and holds the Async_State_Triple around it. Still
              BEFORE the approve/reject buttons (in the footer) so sign-off is
              informed (R2.1/2.5/2.6), and still after the four header fields
              above, which come from the row already in hand and need no fetch.
            */}
            <ApprovalReviewBody approval={selected} />

            <Textarea
              value={comment}
              onChange={({ detail }) => setComment(detail.value)}
              placeholder="Add a comment (optional)"
              rows={3}
            />
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
}
