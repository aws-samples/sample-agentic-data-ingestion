// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import RadioGroup from '@cloudscape-design/components/radio-group';
import { colorBorderDividerDefault } from '@cloudscape-design/design-tokens';
import { computeApprovalMetrics, formatRelativeTime, calculateWaitingTime } from '../utils/formatting';
import { mappingCountText } from '../utils/approval-mapping-count';
import {
  reusedMappingDisplay,
  reusedMappingIdText,
  REUSED_MAPPING_ID_LABEL,
  REUSED_MAPPING_STATEMENT,
  REUSED_MAPPING_UNREADABLE_STATEMENT,
} from '../utils/approval-reused-mapping';
import { unsourcedTargetFieldsText } from '../utils/approval-unsourced-target-fields';
import { proposedRenamesText } from '../utils/rename-manifest';
import { humanizeDuration } from './common/formatters';
import ApprovalArtifact from './approvals/ApprovalArtifact';
import type { Approval, EvidenceQuestion, RunDetail } from '../types';

export interface ApprovalWorkflowSectionProps {
  approvals: Approval[];
  runDetails: RunDetail;
  /**
   * Invoked when the approver clicks Approve on a pending approval. Carries the
   * approval id and — when the approval raised disambiguation questions — the
   * collected answers keyed by question field (R2.3). The consumer forwards
   * `questionAnswers` to the shared `useApprovalAction` hook so they land in the
   * approval payload as `question_answers`.
   */
  onApprove?: (approvalId: string, questionAnswers?: Record<string, string>) => void;
  onReject?: (approvalId: string) => void;
}

/** Reads the typed question list off an approval's details. */
function getQuestions(approval: Approval): EvidenceQuestion[] {
  return (approval.details?.questions ?? []) as EvidenceQuestion[];
}

/**
 * The route the payload's `remediation_hint` names, in the terms this dashboard
 * actually offers (P32 2.5).
 *
 * `details.remediation_hint` is emitted by every `test_failure` gate
 * (`orchestration-stack.ts:585`, defaulted at
 * `approval_handler/lambda_function.py:276`) as the literal
 * `retry-from-step:create_code`. Before this change `remediation_hint` occurred
 * nowhere under `dashboard-react/src`: the payload carried a route no surface
 * showed (`bugfix.md` 1.5).
 *
 * `Approval.details` stays `Record<string, unknown>` (`types.ts:138`) — that
 * looseness is the decision in `LGR-99050e6-5`, which rejected narrowing a
 * frontend type to tidy a reader, so the value is read and narrowed here rather
 * than typed at the boundary. The read mirrors `detailStr` in
 * `ApprovalArtifact.tsx`.
 */
function remediationHint(approval: Approval): string | undefined {
  const value = (approval.details ?? {})['remediation_hint'];
  return typeof value === 'string' && value ? value : undefined;
}

/**
 * The `Retry from Step` option label for an SFN step id, matching
 * `getRetryStepOptions` in `constants.ts` (`name.replace(/([a-z])([A-Z])/g, '$1 $2')`).
 * `create_code` and `CreateCode` are both accepted because the hint is written
 * snake_case while the selector's option values are the PascalCase state ids.
 */
function retryStepLabel(step: string): string {
  const pascal = step
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join('');
  return pascal.replace(/([a-z])([A-Z])/g, '$1 $2');
}

/**
 * The step a `retry-from-step:<step>` hint names, or undefined for any other
 * hint value. Deliberately narrow: a hint this screen cannot map to a control
 * must NOT be paraphrased into one (2.4b — the sentence has to be true), it is
 * shown verbatim instead.
 */
function retryFromStepTarget(hint: string): string | undefined {
  const match = /^retry-from-step:(.+)$/.exec(hint.trim());
  return match ? match[1].trim() || undefined : undefined;
}

/**
 * The route sentence for a `test_failure` gate (2.4b, 2.5, 2.6) — see the long
 * comment at the call site for the evidence behind each clause.
 *
 * Three branches, because a route this screen cannot name is a different
 * statement from one it can:
 *
 *  1. `retry-from-step:<step>` — name the control, the step, and what executing
 *     it does, and quote the hint so the payload's own identifier is on screen.
 *  2. any other non-empty hint — say the payload names a route this screen
 *     cannot map to a control, show it verbatim, and name what the dashboard
 *     does offer. Paraphrasing an unknown route would be a guess presented as
 *     an instruction.
 *  3. no hint — name the same control without attributing it to a hint the
 *     approval does not carry. `Retry from Step` is a property of any failed
 *     run, not of the payload, so it is still true; sourcing it to a hint would
 *     not be.
 */
function remediationSentence(approval: Approval): string {
  const route = 'Retry from Step on this run';
  const effect = 'that starts a new run which generates the code again';
  const preamble =
    'To regenerate the code, reject first — a run has to be failed before it can be retried — then use';
  const hint = remediationHint(approval);

  if (!hint) {
    return `${preamble} ${route} and select Create Code; ${effect}.`;
  }

  const step = retryFromStepTarget(hint);
  if (step) {
    return `${preamble} ${route} and select ${retryStepLabel(step)}; ${effect}. This approval names that route as ${hint}.`;
  }

  return `This approval names a remediation route this screen cannot map to a control: ${hint}. What the dashboard offers is ${route}: reject first, then select the step to run again; ${effect}.`;
}

/**
 * True for the approval types this section renders an {@link ApprovalArtifact}
 * for. Used at BOTH call sites — the pending card and the completed card — so
 * they cannot drift.
 *
 * `test_failure` has been here since `LGR-78f241b-1` (**backfilled** — the
 * author's words: "render a failure artifact for test_failure approvals with an
 * 'Approve anyway' action and a consequence hint", not a ratified decision). That
 * scoped the artifact to failures because a `code_generation` approval was
 * unreachable in practice: exactly one had ever been created in the account, and
 * it sat downstream of a `test_failure` (p34 design §3).
 *
 * p34 removes the premise. Task 8.1 inverts `CheckCodeApproval` so a code
 * approval becomes the default outcome of every clean run, so this list gains
 * `code_generation` (p34 R3.6, R5's user story, task 6.4). It deliberately gains
 * nothing else: `first_run` and `infrastructure` are live backend types with no
 * artifact renderer, and widening to them would newly show `UnknownArtifact`'s
 * warning on surfaces this change was not asked to touch.
 */
function rendersArtifact(approvalType: string | undefined): boolean {
  return approvalType === 'test_failure' || approvalType === 'code_generation';
}

/**
 * The literal the timeout handler writes as the deciding actor —
 * `approval_timeout_handler/lambda_function.py:327` (auto-approve) and `:383`
 * (auto-reject). Not a user id, so it must not be rendered as one (design O6).
 */
const SYSTEM_TIMEOUT_ACTOR = 'system_timeout';

/**
 * What the two identity sites render when the record carries no actor at all.
 * One value for both, replacing the mismatched `'—'` / `'System'` pair — the
 * audit trail's old `'System'` was worse than terse, it *asserted* a system
 * actor for any row that simply had no identity, including every pending one.
 */
const NO_ACTOR_RECORDED = 'Not recorded';

/**
 * The deciding identity to display, for both sites `R2.4` names — `:625`
 * ("Decided by", completed card) and `:665` (`data-testid="audit-actor"`, audit
 * trail). One function so the two cannot drift; `LGR-c58f276-2` names both and
 * fixing one leaves the other showing a UUID.
 *
 * **This closes `LGR-c58f276-2`'s `revisit_when`** (p50 C4, R2.1/R2.3/R2.4).
 * That record (**template / ratified**) chose the stable opaque `sub` as the
 * attributed actor and explicitly rejected "update the frontend projection … so
 * the UI renders `actor_email`" as out of scope, recording the consequence up
 * front: "the dashboard will show an opaque UUID where it used to show an
 * email, until that follow-up happens". This is that follow-up. Its stated
 * consequence applies from here on: `decided_by_email` is now **load-bearing
 * rather than diagnostic**, under the same wire-compatibility constraints
 * `approved_by` / `decided_by` already carry.
 *
 * Preference order, from R2.3 — email, else the `sub`, **never blank**:
 *
 *  1. `decided_by_email` — projected by `pipelines_runs.py`'s run-detail read as
 *     `approved_by_email or rejected_by_email` (p50 task 3.2). The key is always
 *     present and is `None` for every row written before task 3.1, because 3.1
 *     omits the stored attribute when no email resolved rather than writing it
 *     blank (`LGR-c58f276-2`: "omitted when empty … so an audit row never
 *     asserts an identity it does not have"). So the fall-through below is
 *     load-bearing, not defensive.
 *  2. `decided_by` — the Cognito `sub`. Opaque, and deliberately so: R3.1 /
 *     do-NOT 1 keep `sub` first in the resolver and do-NOT 3 keeps
 *     `approved_by` carrying it, because an audit row that is never rewritten
 *     wants a stable identifier over a mutable one. "An opaque identity is
 *     worse than an email and better than nothing."
 *  3. {@link NO_ACTOR_RECORDED}.
 *
 * `.trim()` rather than a bare `||` closes the CP5 gap the task 1.1
 * characterization tests found and handed over explicitly: both sites guarded
 * with a falsy check, so a whitespace-only identity was truthy and rendered as
 * whitespace — blank to a reader, which is what R2.3 forbids. Not reachable
 * from today's producers, which is why 1.1 left it rather than pinning it.
 */
function decidedByLabel(approval: Approval): string {
  const email = approval.decided_by_email?.trim();
  if (email) return email;

  const actor = approval.decided_by?.trim();
  if (!actor) return NO_ACTOR_RECORDED;
  // O6. A timeout decision has a real actor — it just is not a person, and it
  // never carries an email, so it must not fall to the no-identity placeholder
  // and must not read as a failed email lookup either.
  if (actor === SYSTEM_TIMEOUT_ACTOR) return 'System (approval timeout)';
  return actor;
}

function approvalStatusType(status: string): 'success' | 'error' | 'warning' | 'info' | 'pending' {
  switch (status?.toLowerCase()) {
    case 'approved': return 'success';
    case 'rejected': return 'error';
    case 'pending': return 'warning';
    default: return 'info';
  }
}

function renderTypeSpecificContent(approval: Approval) {
  const details = approval.details ?? {};
  // p54 task 4.4 (R1.4, R2.1, R2.2). Read only by the `schema_mapping` arm
  // below; declared here because a `case` label cannot hold a declaration
  // without a block, and the reader is pure so computing it for the other two
  // arms costs nothing. The expression lives in one module for this surface and
  // `ApprovalsPage`'s review modal, per `LGR-4854b3d-1` and `LGR-e59edd7-6` —
  // those two surfaces had already diverged on `mapping_count`, one `?? '—'` and
  // one `?? 0`, which is why the derivation and the words are both shared.
  const reuse = reusedMappingDisplay(details);
  switch (approval.approval_type) {
    case 'schema_mapping':
      return (
        <div data-testid="type-content-schema-mapping">
          <Box variant="awsui-key-label">Mapping Preview</Box>
          {/*
            p54 task 4.4. The reuse block sits ABOVE the count deliberately, for
            two reasons. It states the more basic fact first — whether there is a
            reused mapping at all, and which one — before quantifying it. And it
            keeps `Mapping count: 0` at a word boundary in this div's fused
            `textContent`: two assertions in
            `ApprovalWorkflowSection.actor-identity.characterization.test.tsx`
            read `/Mapping count:\s*0\b/` off it, and p50 task 5.3 already had to
            back out an added SIBLING below the count for exactly that reason
            (see the `!= null` note further down). A block above the count cannot
            reach that boundary.

            Absence renders NOTHING. A payload that states no reuse gets no
            block, because `false` here would assert the run generated its own
            mapping — the collapse do-NOT 7 forbids, and the same reason
            `get_run_details` omits the key rather than defaulting it (task 3.1).

            Words carry every distinction, not colour: no `StatusIndicator` and
            no alert type. do-NOT 8, `LGR-99050e6-1` (colour alone fails WCAG
            1.4.1), `LGR-1f7892b-2`.
          */}
          {reuse.stated && (
            <div data-testid="workflow-reused-mapping">
              {reuse.reused && <Box>{REUSED_MAPPING_STATEMENT}</Box>}
              <Box>
                {REUSED_MAPPING_ID_LABEL}:{' '}
                <span data-testid="workflow-reused-mapping-id">
                  {reusedMappingIdText(details)}
                </span>
              </Box>
              {/*
                The second of the two sentences R1.4 requires, and the reason
                this task exists at all: `toMappingData` yields `mappings: []`
                for an unreadable payload, so without this line the state
                renders as A MAPPING WITH NO ROWS — `LGR-b2f79ef-14`'s named
                reference to something absent, presented as a thing that has no
                fields. It fires on the producer's `mapping_unreadable` marker
                AND on task 2.3's third route (a payload stating reuse whose
                `proposed_mappings` is not a list); the reader's header argues
                that choice.
              */}
              {reuse.unreadable && (
                <Box>
                  <span data-testid="workflow-reused-mapping-unreadable">
                    {REUSED_MAPPING_UNREADABLE_STATEMENT}
                  </span>
                </Box>
              )}
            </div>
          )}
          {/*
            p50 task 4.2 (C2, R1.4/R1.5). This site was ALREADY correct — it read
            `?? '—'`, so a null count rendered as not-established — and the
            sibling site in `ApprovalsPage.tsx` was not: it read `?? 0` and
            reported an unreadable payload as a confident zero. The expression
            now lives in one module for both, per `LGR-4854b3d-1`'s ruling for
            `field_count` ("the expression lives once"), so the two cannot
            diverge again. What changed here in behaviour is only the non-numeric
            arm: `details` is `Record<string, unknown>`, and `String(...)` would
            have stringified a malformed value as though it were a count.
          */}
          <Box>Mapping count: {mappingCountText(details)}</Box>
          {/*
            p50 task 5.3. This read `String(details.unmapped_fields)` behind a
            `!= null` guard, and the guard is the only reason this site did not
            crash the way `ApprovalsPage`'s did: the producer writes a DICT
            (`schema_mapping/lambda_function.py:1740-1744`), and `String(...)` on
            it yields `"[object Object]"` — wrong in a quieter way. Task 2.1
            populated the key, so this fired on every corrected payload.

            The expression now lives in one module for both surfaces, the same
            way `mappingCountText` above does and for the same reason
            (`LGR-4854b3d-1`: the expression cannot be rewritten per site). It
            delegates to `unsourcedTargetFields`, the single reader of this
            payload (`LGR-9e76249-7`, `LGR-7561acb-1`), and to
            `field-requirement.ts`'s words (`LGR-7561acb-4`) — no count is
            derived, because the wire carries none.

            The `!= null` guard STAYS, and that is a correction to this task's
            first attempt rather than the original shape being left alone. The
            line was briefly made unconditional so it would report absence the
            way `Mapping count` above does — and that is a display decision
            beyond the defect, not part of it. It also broke two assertions in
            `ApprovalWorkflowSection.actor-identity.characterization.test.tsx`
            (task 1.1), which read `/Mapping count:\s*0\b/` off this div's fused
            `textContent`: an added sibling puts `U` immediately after the `0`,
            so the trailing word boundary disappears. Those assertions are
            another task's deliverable and their subject is the count, so the
            change that has to give is this one. Omitting the line when nothing
            was recorded is also this card's own existing convention — the
            `Confidence` line below does exactly that.
          */}
          {details.unmapped_fields != null && (
            <Box>
              Unsourced target fields:{' '}
              <span data-testid="workflow-unsourced-target-fields">
                {unsourcedTargetFieldsText(details)}
              </span>
            </Box>
          )}
          {/*
            p55 task 6.2 (R10.1, R10.2). Deliberately the same shape as the line
            above it: the `!= null` guard, one delegated expression, one
            `data-testid` on the value. The guard is this card's own convention —
            `Confidence` below omits itself the same way — and it means an approval
            whose payload predates p55 shows no line rather than an em dash for a
            fact that did not exist when the row was written.
            `proposedRenamesText` still answers three-state for the rows that DO
            carry the key, because a manifest present but unreadable is not the
            same as absent.

            The expression lives in `utils/rename-manifest.ts` and is shared with
            `ApprovalsPage` and `ApprovalArtifact`, per `LGR-4854b3d-1` — these two
            surfaces have already diverged on one expression twice, and both
            corrections ended with the expression in one module.

            One line rather than a list, because this is the compact card: the full
            table is in `ApprovalArtifact` directly below, on the same screen.
          */}
          {details.rename_manifest != null && (
            <Box>
              Proposed source column renames:{' '}
              <span data-testid="workflow-proposed-renames">{proposedRenamesText(details)}</span>
            </Box>
          )}
          {details.confidence_score != null && (
            <Box>Confidence: {String(details.confidence_score)}</Box>
          )}
        </div>
      );
    case 'code_generation':
      return (
        <div data-testid="type-content-code-generation">
          <Box variant="awsui-key-label">Code Summary</Box>
          {/*
            p34 task 6.4. The default was `Generated code pending review`, which
            was two things at once: the only content this surface showed for a
            code approval, and — on the completed card, which renders this same
            function (`:558`) — false, since the review had already happened. The
            code and its check list are now below, in `ApprovalArtifact`, so this
            line only has to say honestly whether a summary was recorded.

            `details.test_results` — PLURAL — is left exactly as it was. The
            producer writes `test_result`, singular, and renaming the key here
            would render `[object Object]`, because the value is the
            `{status, checks, report_s3_uri}` object and `String(...)` below
            stringifies it (p34 design §4.5). Rendering the checks list is the
            fix, and `ApprovalArtifact` now does it. This branch stays for any
            payload that really does carry a plural string.
          */}
          <Box>{String(details.code_summary ?? 'No code summary was recorded for this approval.')}</Box>
          {details.test_results != null && (
            <Box>Test results: {String(details.test_results)}</Box>
          )}
        </div>
      );
    case 'first_run':
      return (
        <div data-testid="type-content-first-run">
          <Box variant="awsui-key-label">Pipeline Configuration</Box>
          <Box>{String(details.config_summary ?? 'First run configuration')}</Box>
          {details.infrastructure_status != null && (
            <Box>Infrastructure: {String(details.infrastructure_status)}</Box>
          )}
        </div>
      );
    default:
      return null;
  }
}

/** Evidence cards for schema_mapping approvals with sample values, reasoning, and confidence breakdown. */
function renderEvidenceCards(approval: Approval) {
  const evidence = (approval.details?.evidence ?? {}) as Record<string, {
    sample_values?: string[];
    reasoning?: string;
    memory_matches?: number;
    confidence_before?: number;
    confidence_after?: number;
  }>;
  const fields = Object.keys(evidence);
  if (fields.length === 0) return null;

  return (
    <ExpandableSection headerText="Evidence" defaultExpanded data-testid="evidence-section">
      <SpaceBetween size="m">
        {fields.map((field) => {
          const ev = evidence[field];
          return (
            <div
              key={field}
              data-testid={`evidence-card-${field}`}
              style={{ padding: '12px', borderRadius: '8px', border: `1px solid ${colorBorderDividerDefault}` }}
            >
              <SpaceBetween size="xs">
                <Box variant="h4">{field}</Box>
                {ev.sample_values && ev.sample_values.length > 0 && (
                  <div>
                    <Box variant="awsui-key-label">Sample Values</Box>
                    <Box variant="small">{ev.sample_values.join(', ')}</Box>
                  </div>
                )}
                {ev.reasoning && (
                  <div>
                    <Box variant="awsui-key-label">Reasoning</Box>
                    <Box variant="small">{ev.reasoning}</Box>
                  </div>
                )}
                <ColumnLayout columns={3} variant="text-grid">
                  <div>
                    <Box variant="awsui-key-label">Confidence Before</Box>
                    <Box variant="small">{ev.confidence_before != null ? `${(ev.confidence_before * 100).toFixed(0)}%` : '—'}</Box>
                  </div>
                  <div>
                    <Box variant="awsui-key-label">Confidence After</Box>
                    <Box variant="small">{ev.confidence_after != null ? `${(ev.confidence_after * 100).toFixed(0)}%` : '—'}</Box>
                  </div>
                  <div>
                    <Box variant="awsui-key-label">Memory Matches</Box>
                    <Box variant="small">{ev.memory_matches ?? 0}</Box>
                  </div>
                </ColumnLayout>
              </SpaceBetween>
            </div>
          );
        })}
      </SpaceBetween>
    </ExpandableSection>
  );
}

/**
 * Questions section for ambiguous fields with suggested options (R2.2).
 *
 * The RadioGroup is a *controlled* component: its `value` comes from the
 * `answers` map (keyed by question field) and `onChange` reports the selection
 * back through `onAnswer`. This replaces the previous broken
 * `value={null as unknown as string}` + no-op onChange that silently discarded
 * the approver's answers. When `readOnly` is set (completed approvals) the
 * radios display the historical/blank selection without accepting input.
 */
function QuestionsSection({
  questions,
  answers,
  onAnswer,
  readOnly = false,
}: {
  questions: EvidenceQuestion[];
  answers?: Record<string, string>;
  onAnswer?: (field: string, value: string) => void;
  readOnly?: boolean;
}) {
  if (questions.length === 0) return null;

  return (
    <ExpandableSection headerText="Questions" defaultExpanded data-testid="questions-section">
      <SpaceBetween size="m">
        {questions.map((q, i) => (
          <div
            key={`${q.field}-${i}`}
            data-testid={`question-card-${q.field}`}
            style={{ padding: '12px', borderRadius: '8px', border: `1px solid ${colorBorderDividerDefault}` }}
          >
            <SpaceBetween size="xs">
              <Box variant="h4">
                {q.field}
                {q.required && (
                  <Box variant="span" color="text-status-error" data-testid={`question-required-${q.field}`}>
                    {' *'}
                  </Box>
                )}
              </Box>
              <Box>{q.question}</Box>
              {q.sample_values && q.sample_values.length > 0 && (
                <Box variant="small" color="text-body-secondary">
                  Sample values: {q.sample_values.join(', ')}
                </Box>
              )}
              <RadioGroup
                items={q.options.map((opt) => ({ value: opt, label: opt }))}
                value={answers?.[q.field] ?? null}
                onChange={({ detail }) => onAnswer?.(q.field, detail.value)}
                readOnly={readOnly}
                data-testid={`question-options-${q.field}`}
              />
            </SpaceBetween>
          </div>
        ))}
      </SpaceBetween>
    </ExpandableSection>
  );
}

/**
 * A single pending approval card. Owns the local `answers` state for its
 * disambiguation questions so each pending approval collects its own answers
 * independently. The Approve button is disabled while any *required* question
 * is unanswered (R2.4) with an inline hint, and on approve the collected
 * answers are handed to `onApprove` for inclusion in the approval payload
 * (R2.3).
 */
function PendingApprovalCard({
  approval,
  onApprove,
  onReject,
}: {
  approval: Approval;
  onApprove?: (approvalId: string, questionAnswers?: Record<string, string>) => void;
  onReject?: (approvalId: string) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const isSchemaMapping = approval.approval_type === 'schema_mapping';
  const isTestFailure = approval.approval_type === 'test_failure';
  const isCodeGeneration = approval.approval_type === 'code_generation';
  const questions = isSchemaMapping ? getQuestions(approval) : [];
  const requiredUnanswered = questions.some((q) => q.required && !answers[q.field]);

  const handleAnswer = (field: string, value: string) => {
    setAnswers((prev) => ({ ...prev, [field]: value }));
  };

  // test_failure gates rarely carry a gate_name — lead with an explicit label
  // rather than the raw type string so the card reads as a failure up front.
  const cardTitle =
    isTestFailure && !approval.gate_name ? 'Code testing failed' : approval.gate_name || approval.approval_type;

  return (
    <div data-testid="pending-approval">
      <SpaceBetween size="xs">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Box variant="h4">{cardTitle}</Box>
          <Box variant="small" color="text-body-secondary">
            Waiting: {calculateWaitingTime(approval.created_at)}
          </Box>
        </div>
        {renderTypeSpecificContent(approval)}
        {rendersArtifact(approval.approval_type) && <ApprovalArtifact approval={approval} />}
        {isSchemaMapping && renderEvidenceCards(approval)}
        {isSchemaMapping && (
          <QuestionsSection questions={questions} answers={answers} onAnswer={handleAnswer} />
        )}
        {requiredUnanswered && (
          <Box variant="small" color="text-status-error" data-testid="approve-required-hint">
            Answer all required questions before approving.
          </Box>
        )}
        {/*
          P32 C3 (2.4a) then C9 (2.4b, 2.5, 2.6): what Reject actually does, and
          the route that does regenerate.

          C3's half, unchanged in substance. The original copy promised "Reject
          stops the run so the code can be regenerated"; Reject calls
          send_task_failure(error="ApprovalRejected")
          (approval_handler/lambda_function.py:565-567) → Catch
          (orchestration-stack.ts:505) → RecordFailure → releaseLockFailure
          (:736) → PipelineFailed, a terminal sfn.Fail (:266). The backend guard
          is
          tests/integration/pipeline_flow/test_test_failure_gating.py::test_rejection_writes_failure_record_and_no_downstream
          (bugfix.md 3.16): a Failure_Record is written with
          error_name="ApprovalRejected" and no downstream step row appears.

          C9's half names a route, which 2.4a deliberately refused to do while no
          route worked. Each clause below is a claim about code read at the time
          it was written:

          • "no automatic attempt left" — ForceTestReviewApproval is only
            reachable from TestResultChoice's Default arm
            (orchestration-stack.ts:782-811): the passed arm goes to
            CheckCodeApproval and the P32 D2 regeneration arm requires
            regeneration_needed AND attempt < max_attempts. No edge leaves this
            gate for CreateCode — approve-anyway goes to CheckCodeApproval,
            reject fails the run — so a run that reached this screen has no
            automatic attempt available.
          • "reject first" — the Retry from Step control renders only on
            status === 'FAILED' (run-detail/RunDetailHeader.tsx:85-114) and
            retry_run answers 409 "Run is not in a failed state" otherwise
            (dashboard_api/handlers/pipelines_runs.py:1204-1213). While this gate
            is pending the run is RUNNING, so rejecting is a precondition of the
            route, not an alternative to it.
          • "select Create Code" — the selector's options are the run's
            completed/failed steps as SFN state ids (constants.ts
            getRetryStepOptions); CreateCode succeeded on a run that reached a
            test_failure gate, so it is offered.
          • "starts a new run which generates the code again" — retry_run always
            calls start_execution with a new run id
            (pipelines_runs.py:1327-1340), and CreateCode executes in that run.
            This is deliberately silent on WHERE the new run begins: today it
            re-enters FileValidation and the pre-seeded cached results are
            overwritten (design.md "V1 live finding", confirmed live 2026-08-04;
            LGR-2600abb-1 records that P16's CDK skip states were reverted), and
            task 17 is building the resume. Claiming either would be a sentence
            that stops being true when the other lands — and 2.4b requires truth
            at the moment of shipping. What holds in both is that the code is
            generated again, which is what 2.6 asks of the route.

          The route is read from details.remediation_hint rather than hardcoded
          (2.5), so the screen names the route the payload names. A hint this
          component cannot map to a control is shown verbatim instead of being
          paraphrased — the same choice UnknownArtifact makes with an
          unrenderable approval_type.

          LGR-78f241b-1's warning-header-plus-explicit-consequence pattern is
          preserved: every choice still carries its consequence in text, not in
          colour (the house pattern from LGR-99050e6-1).
        */}
        {isTestFailure && (
          <Box variant="small" color="text-body-secondary" data-testid="test-failure-consequence">
            Approve anyway proceeds with code that failed testing. Reject fails the run: the pipeline stops
            at this gate, records the failure, and no later steps run. Automatic regeneration is not one of
            the choices here — no path out of this gate re-enters code generation, so this run has no
            automatic attempt left.{' '}
            <span data-testid="test-failure-remediation">{remediationSentence(approval)}</span>
          </Box>
        )}
        {/*
          p34 task 6.4 (R3.6, R5's user story): the same house pattern for a code
          approval — every choice carries its consequence in TEXT, not in colour
          (`LGR-78f241b-1`, backfilled; `LGR-99050e6-1`, template / ratified, WCAG
          1.4.1).

          Its OWN block and its own copy. The `test_failure` block above is not
          reused and its test id is not shared: `Approve anyway proceeds with code
          that failed testing` is true of a failing run and false of this one, and
          reusing it would be the misattribution p34 design §4.5 forbids — created
          by this change rather than found by it.

          Both clauses are read off the definition, not inferred:
          • "continues to infrastructure creation and the remaining steps run" —
            `waitForCodeApproval.next(createInfrastructure)`
            (`orchestration-stack.ts:908`), then
            `createInfrastructure.next(createGluePipeline)` (`:909`).
          • "fails the run … no later steps run" — Reject calls
            `send_task_failure(error="ApprovalRejected")`, and this state carries
            `Catch: [{ErrorEquals: ['States.ALL'], Next: 'RecordFailure'}]`
            (`:672`) → the same RecordFailure → releaseLockFailure → PipelineFailed
            terminal path the `test_failure` gate documents above.

          Nothing here names a regeneration route. On this gate there is none: the
          only edge out of `WaitForCodeApproval` is the success edge to
          `CreateInfrastructure` (`:908`), so a sentence about regenerating would
          have to be about a different screen.
        */}
        {isCodeGeneration && (
          <Box variant="small" color="text-body-secondary" data-testid="code-generation-consequence">
            Approve proceeds with this code: the run continues to infrastructure creation and the
            remaining steps run. Reject fails the run: the pipeline stops at this gate, records the
            failure, and no later steps run.
          </Box>
        )}
        <SpaceBetween direction="horizontal" size="xs">
          <Button
            variant="primary"
            data-testid="approve-button"
            disabled={requiredUnanswered}
            onClick={() =>
              onApprove?.(
                approval.approval_id,
                Object.keys(answers).length > 0 ? answers : undefined,
              )
            }
          >
            {isTestFailure ? 'Approve anyway' : 'Approve'}
          </Button>
          <Button
            data-testid="reject-button"
            onClick={() => onReject?.(approval.approval_id)}
          >
            Reject
          </Button>
        </SpaceBetween>
      </SpaceBetween>
    </div>
  );
}

export default function ApprovalWorkflowSection({
  approvals,
  runDetails: _runDetails,
  onApprove,
  onReject,
}: ApprovalWorkflowSectionProps) {
  if (!approvals || approvals.length === 0) {
    return (
      <Box textAlign="center" color="text-body-secondary" data-testid="approval-workflow-empty">
        No approvals for this run
      </Box>
    );
  }

  const { averageResponseTime, approvalRate } = computeApprovalMetrics(approvals);
  const pendingApprovals = approvals.filter((a) => a.status?.toLowerCase() === 'pending');
  const completedApprovals = approvals.filter((a) => a.status?.toLowerCase() !== 'pending');

  return (
    <div data-testid="approval-workflow-section">
      <SpaceBetween size="l">
        {/* Performance metrics */}
        <Container header={<Header variant="h3">Approval Metrics</Header>}>
          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Total Approvals</Box>
              <Box data-testid="approval-total">{approvals.length}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Avg Response Time</Box>
              {/*
                R13.5 (completes p29 R4.2): an approval turnaround is a human
                wait, so raw milliseconds ("86400000ms") were unreadable. The
                mean is over `Approval.response_time_ms`, which the API declares
                — and the only in-repo producer computes — in milliseconds, so
                it feeds `humanizeDuration` unconverted.

                The `> 0` guard is the empty state, not a measurement: with no
                recorded response time `computeApprovalMetrics` returns 0, and
                "0s" would read as a measured instant turnaround. A non-finite
                value falls here too (NaN > 0 is false).

                Sub-second floors to "0s" — the formatter's documented
                behaviour, kept deliberately over a raw-ms branch per p29 R4.2.
                `MetricsDashboardPage`'s `formatLatency` retains ms below 1s
                because agent latency genuinely lives there.
              */}
              <Box data-testid="approval-avg-response">
                {averageResponseTime > 0 ? humanizeDuration(averageResponseTime) : '—'}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Approval Rate</Box>
              <Box data-testid="approval-rate">
                {approvals.length > 0 ? `${Math.round(approvalRate * 100)}%` : '—'}
              </Box>
            </div>
          </ColumnLayout>
        </Container>

        {/* Gate indicators */}
        <Container header={<Header variant="h3">Approval Gates</Header>}>
          <SpaceBetween size="s">
            {approvals.map((approval) => (
              <div key={approval.approval_id} data-testid="approval-gate">
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <StatusIndicator type={approvalStatusType(approval.status)}>
                    {approval.gate_name || approval.approval_type}
                  </StatusIndicator>
                  <Badge color={approval.status?.toLowerCase() === 'approved' ? 'green' : approval.status?.toLowerCase() === 'rejected' ? 'red' : 'blue'}>
                    {approval.status}
                  </Badge>
                </div>
              </div>
            ))}
          </SpaceBetween>
        </Container>

        {/* Pending approvals with action buttons */}
        {pendingApprovals.length > 0 && (
          <Container header={<Header variant="h3">Pending Approvals ({pendingApprovals.length})</Header>}>
            <SpaceBetween size="m">
              {pendingApprovals.map((approval) => (
                <PendingApprovalCard
                  key={approval.approval_id}
                  approval={approval}
                  onApprove={onApprove}
                  onReject={onReject}
                />
              ))}
            </SpaceBetween>
          </Container>
        )}

        {/* Completed approvals with decision details */}
        {completedApprovals.length > 0 && (
          <Container header={<Header variant="h3">Completed Approvals ({completedApprovals.length})</Header>}>
            <SpaceBetween size="m">
              {completedApprovals.map((approval) => (
                <div key={approval.approval_id} data-testid="completed-approval">
                  <SpaceBetween size="xs">
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <StatusIndicator type={approvalStatusType(approval.status)}>
                        {approval.gate_name || approval.approval_type}
                      </StatusIndicator>
                      <Badge color={approval.status?.toLowerCase() === 'approved' ? 'green' : 'red'}>
                        {approval.status}
                      </Badge>
                    </div>
                    <Box variant="small">
                      {/* p50 C4, site 1 of the two R2.4 names. See {@link decidedByLabel}. */}
                      Decided by: {decidedByLabel(approval)} at{' '}
                      {approval.decided_at ? formatRelativeTime(approval.decided_at) : '—'}
                    </Box>
                    {approval.comment && (
                      <Box variant="small" color="text-body-secondary">
                        Comment: {approval.comment}
                      </Box>
                    )}
                    {renderTypeSpecificContent(approval)}
                    {/*
                      p34 task 6.4 widens this with the pending gate above, through
                      the same predicate. Widening only the pending card would leave
                      an already-granted code approval showing nothing about what was
                      approved — which is the audit half of design §3's incident.
                    */}
                    {rendersArtifact(approval.approval_type) && <ApprovalArtifact approval={approval} />}
                    {approval.approval_type === 'schema_mapping' && renderEvidenceCards(approval)}
                    {approval.approval_type === 'schema_mapping' && (
                      <QuestionsSection questions={getQuestions(approval)} readOnly />
                    )}
                  </SpaceBetween>
                </div>
              ))}
            </SpaceBetween>
          </Container>
        )}

        {/* Audit trail */}
        <Container header={<Header variant="h3">Audit Trail</Header>}>
          <SpaceBetween size="xs">
            {approvals.map((approval) => (
              <div key={approval.approval_id} data-testid="audit-trail-entry">
                <Box variant="small">
                  <span data-testid="audit-timestamp">
                    {approval.decided_at
                      ? formatRelativeTime(approval.decided_at)
                      : formatRelativeTime(approval.created_at)}
                  </span>
                  {' — '}
                  {/*
                    p50 C4, site 2 of the two. R2.4 requires both: fixing only
                    "Decided by" above would leave the audit trail — the surface
                    whose whole job is attribution — still showing a UUID.
                    Through the same helper, so they cannot drift.
                  */}
                  <span data-testid="audit-actor">
                    {decidedByLabel(approval)}
                  </span>
                  {' — '}
                  <Badge color={approval.status?.toLowerCase() === 'approved' ? 'green' : approval.status?.toLowerCase() === 'rejected' ? 'red' : 'blue'}>
                    {approval.status}
                  </Badge>
                  {' '}
                  {approval.gate_name || approval.approval_type}
                </Box>
              </div>
            ))}
          </SpaceBetween>
        </Container>
      </SpaceBetween>
    </div>
  );
}
