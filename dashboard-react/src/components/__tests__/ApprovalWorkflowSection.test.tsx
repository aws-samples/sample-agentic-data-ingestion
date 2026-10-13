// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for ApprovalWorkflowSection evidence cards and questions rendering.
 *
 * Validates that schema_mapping approvals with populated `details.evidence` and
 * `details.questions` render the expected evidence cards and question UI.
 *
 * Requirements: 9.1, 9.2, 9.3
 */

import { describe, it, expect } from 'vitest';
import { render, within } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail, SchemaMappingApprovalDetails } from '../../types';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const baseRunDetail: RunDetail = {
  run_id: 'run-001',
  pipeline_key: 'test-pipeline',
  status: 'RUNNING',
  started_at: '2024-06-01T10:00:00Z',
};

function makeSchemaMappingApproval(
  overrides: Partial<Approval> = {},
  details: SchemaMappingApprovalDetails = {},
): Approval {
  return {
    approval_id: 'approval-sm-001',
    pipeline_key: 'test-pipeline',
    run_id: 'run-001',
    approval_type: 'schema_mapping',
    status: 'pending',
    created_at: '2024-06-01T10:05:00Z',
    gate_name: 'Schema Review',
    details,
    ...overrides,
  };
}

const populatedEvidence: SchemaMappingApprovalDetails = {
  mapping_count: 8,
  confidence_score: 0.62,
  evidence: {
    customer_id: {
      sample_values: ['C-1001', 'C-2002', 'C-3003'],
      reasoning: 'Field matches by name and value pattern to target customer_identifier',
      memory_matches: 3,
      confidence_before: 0.55,
      confidence_after: 0.88,
    },
    order_date: {
      sample_values: ['2024-01-15', '2024-02-20'],
      reasoning: 'Date format matches target purchase_date; values align temporally',
      memory_matches: 1,
      confidence_before: 0.42,
      confidence_after: 0.79,
    },
  },
  questions: [
    {
      field: 'status_code',
      question: 'Which target field should status_code map to?',
      options: ['order_status', 'fulfillment_state', 'skip'],
      sample_values: ['active', 'pending', 'closed'],
      current_confidence: 0.35,
    },
    {
      field: 'amount',
      question: 'Is amount in cents or dollars?',
      options: ['cents (divide by 100)', 'dollars (no transform)', 'unknown'],
      sample_values: ['1500', '2999', '450'],
    },
  ],
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ApprovalWorkflowSection – evidence and questions rendering', () => {
  it('renders evidence cards for a pending schema_mapping approval with populated details', () => {
    const approval = makeSchemaMappingApproval({}, populatedEvidence);
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const section = within(container).getByTestId('approval-workflow-section');

    // Evidence section should be present
    const evidenceSection = within(section).getByTestId('evidence-section');
    expect(evidenceSection).toBeTruthy();

    // Should render one card per evidence field
    const customerCard = within(section).getByTestId('evidence-card-customer_id');
    expect(customerCard).toBeTruthy();
    expect(customerCard.textContent).toContain('customer_id');
    expect(customerCard.textContent).toContain('C-1001');
    expect(customerCard.textContent).toContain('matches by name and value pattern');
    expect(customerCard.textContent).toContain('55%'); // confidence_before
    expect(customerCard.textContent).toContain('88%'); // confidence_after
    expect(customerCard.textContent).toContain('3');   // memory_matches

    const dateCard = within(section).getByTestId('evidence-card-order_date');
    expect(dateCard).toBeTruthy();
    expect(dateCard.textContent).toContain('order_date');
    expect(dateCard.textContent).toContain('2024-01-15');
  });

  it('renders question cards for a pending schema_mapping approval with populated details', () => {
    const approval = makeSchemaMappingApproval({}, populatedEvidence);
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const section = within(container).getByTestId('approval-workflow-section');

    // Questions section should be present
    const questionsSection = within(section).getByTestId('questions-section');
    expect(questionsSection).toBeTruthy();

    // Should render one card per question
    const statusQ = within(section).getByTestId('question-card-status_code');
    expect(statusQ).toBeTruthy();
    expect(statusQ.textContent).toContain('status_code');
    expect(statusQ.textContent).toContain('Which target field should status_code map to?');
    expect(statusQ.textContent).toContain('active');

    const amountQ = within(section).getByTestId('question-card-amount');
    expect(amountQ).toBeTruthy();
    expect(amountQ.textContent).toContain('Is amount in cents or dollars?');
    expect(amountQ.textContent).toContain('1500');
  });

  it('renders evidence and questions for a completed schema_mapping approval', () => {
    const approval = makeSchemaMappingApproval(
      { status: 'approved', decided_by: 'admin', decided_at: '2024-06-01T10:15:00Z' },
      populatedEvidence,
    );
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const section = within(container).getByTestId('approval-workflow-section');

    // Completed approvals also render evidence and questions
    expect(within(section).getByTestId('evidence-section')).toBeTruthy();
    expect(within(section).getByTestId('questions-section')).toBeTruthy();
    expect(within(section).getByTestId('evidence-card-customer_id')).toBeTruthy();
    expect(within(section).getByTestId('question-card-status_code')).toBeTruthy();
  });

  it('does not render evidence or questions sections when details is empty', () => {
    const approval = makeSchemaMappingApproval({}, {});
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const section = within(container).getByTestId('approval-workflow-section');

    expect(within(section).queryByTestId('evidence-section')).toBeNull();
    expect(within(section).queryByTestId('questions-section')).toBeNull();
  });

  it('does not render evidence/questions for non-schema_mapping approvals even with details', () => {
    const approval: Approval = {
      approval_id: 'approval-cg-001',
      pipeline_key: 'test-pipeline',
      approval_type: 'code_generation',
      status: 'pending',
      created_at: '2024-06-01T10:05:00Z',
      details: {
        evidence: { some_field: { reasoning: 'Should not render' } },
        questions: [{ field: 'x', question: 'y', options: ['a'] }],
      },
    };
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const section = within(container).getByTestId('approval-workflow-section');

    // Evidence/questions are only rendered for schema_mapping type
    expect(within(section).queryByTestId('evidence-section')).toBeNull();
    expect(within(section).queryByTestId('questions-section')).toBeNull();
  });

  it('renders evidence without questions when only evidence is populated', () => {
    const detailsOnlyEvidence: SchemaMappingApprovalDetails = {
      evidence: {
        field_a: {
          sample_values: ['val1'],
          reasoning: 'Direct name match',
          confidence_before: 0.6,
          confidence_after: 0.9,
        },
      },
    };
    const approval = makeSchemaMappingApproval({}, detailsOnlyEvidence);
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const section = within(container).getByTestId('approval-workflow-section');
    expect(within(section).getByTestId('evidence-section')).toBeTruthy();
    expect(within(section).queryByTestId('questions-section')).toBeNull();
  });

  it('renders questions without evidence when only questions are populated', () => {
    const detailsOnlyQuestions: SchemaMappingApprovalDetails = {
      questions: [
        {
          field: 'ambiguous_field',
          question: 'What does this map to?',
          options: ['optionA', 'optionB'],
        },
      ],
    };
    const approval = makeSchemaMappingApproval({}, detailsOnlyQuestions);
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const section = within(container).getByTestId('approval-workflow-section');
    expect(within(section).queryByTestId('evidence-section')).toBeNull();
    expect(within(section).getByTestId('questions-section')).toBeTruthy();
    expect(within(section).getByTestId('question-card-ambiguous_field')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// test_failure gate — explicit failure surfacing + consequence-labeled actions
// ---------------------------------------------------------------------------

function makeTestFailureApproval(overrides: Partial<Approval> = {}): Approval {
  return {
    approval_id: 'approval-tf-001',
    pipeline_key: 'test-pipeline',
    run_id: 'run-001',
    approval_type: 'test_failure',
    status: 'pending',
    created_at: '2024-06-01T10:05:00Z',
    details: {
      report: {
        summary: 'Data quality checks failed',
        checks: [
          { name: 'row_count', status: 'passed', message: 'rows match' },
          { name: 'null_check', status: 'failed', message: 'nulls found in id' },
        ],
      },
    },
    ...overrides,
  };
}

describe('ApprovalWorkflowSection – test_failure gate', () => {
  it('renders the failure artifact, "Approve anyway" label, and the consequence hint on a pending test_failure', () => {
    const approval = makeTestFailureApproval();
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const pending = within(container).getByTestId('pending-approval');

    // The failure artifact (banner + per-check table) is surfaced in the card.
    expect(within(pending).getByTestId('approval-artifact-test-failure')).toBeTruthy();
    expect(
      within(pending).getByTestId('approval-artifact-test-failure-alert').textContent,
    ).toContain('Code testing failed');
    expect(within(pending).getByTestId('approval-artifact-test-report').textContent).toContain(
      'null_check',
    );

    // Approve button carries the consequence-aware label.
    const approveButton = within(pending).getByTestId('approve-button');
    expect(approveButton.textContent).toContain('Approve anyway');

    // Consequence hint explains what each choice means, and the button stays enabled
    // (test_failure has no required questions).
    const consequence = within(pending).getByTestId('test-failure-consequence');
    expect(consequence.textContent).toContain('Approve anyway proceeds with code that failed testing');
    expect((approveButton as HTMLButtonElement).disabled).toBe(false);
  });

  /**
   * P32 T18 / C9 — Property 2 (stage B): the Reject statement is true, and the
   * route it names works.
   *
   * This is task 1's assertion EXTENDED, not replaced. Expectations are derived
   * from bugfix.md 2.4a/2.4b and from the state machine, NOT from the copy on
   * screen — `LGR-28b4d75-5`, a test transcribed from the thing it is checking
   * has that thing as its own oracle.
   *
   * What Reject actually does, unchanged and still asserted below:
   * `send_task_failure(error="ApprovalRejected")`
   * (`approval_handler/lambda_function.py:565-567`) → `Catch`
   * (`orchestration-stack.ts:505`) → `RecordFailure` → `releaseLockFailure`
   * (`:736`) → `PipelineFailed`, a terminal `sfn.Fail` (`:266`). The backend
   * guard for the behaviour this copy describes is
   * `tests/integration/pipeline_flow/test_test_failure_gating.py::test_rejection_writes_failure_record_and_no_downstream`
   * (bugfix.md 3.16).
   *
   * **Why the absence assertions became presence assertions.** Stage A asserted
   * that the hint named NO route, because at that point no route regenerated
   * anything (bugfix.md 1.6) and naming one would have restated the same false
   * promise in different words. That was a deliberate hold encoded as a test —
   * `LGR-5906ec6-2`'s pattern — so lifting it is a decision, not a cleanup.
   * What changed: tasks 14-16 built the destination (`regeneration_attempt` in
   * the trigger contract, the D2 loop-back edge, the counter and routing), so a
   * route now exists, and 2.4b requires the hint to name it. The three clauses
   * flipped from `not.toMatch` to `toMatch` are exactly the three the stage-A
   * test forbade.
   *
   * **What the sentence may NOT claim.** `Retry from Step` starts a new
   * execution (`pipelines_runs.py:1327-1340`); it does not today resume at the
   * chosen step — a retry re-enters `FileValidation` and the pre-seeded cached
   * results are overwritten (design.md "V1 live finding"; `LGR-2600abb-1`
   * records that P16's CDK skip states were reverted). Task 17 is building the
   * resume. So the hint asserts only that the code is generated again, which
   * holds either way, and this test asserts the absence of a resume claim so a
   * future edit cannot quietly add one.
   *
   * Validates: Requirements 2.4b, 2.5, 2.6; 3.16
   */
  it('states that Reject fails the run and names the route that regenerates the code', () => {
    const approval = makeTestFailureApproval();
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const pending = within(container).getByTestId('pending-approval');
    const hint = within(pending).getByTestId('test-failure-consequence').textContent ?? '';

    // 1. The hint still describes terminal failure of the run (2.4a, kept).
    expect(hint).toMatch(/\bReject\b/);
    expect(hint).toMatch(/fails the run/i);

    // 2. It names route R — the dashboard control, the step, and the fact that
    //    a rejection has to come first because the control only exists on a
    //    FAILED run (`RunDetailHeader.tsx:85-114`;
    //    `pipelines_runs.py:1204-1213` answers 409 otherwise).
    expect(hint).toMatch(/Retry from Step/i);
    expect(hint).toMatch(/Create Code/i);
    expect(hint).toMatch(/reject first/i);

    // 3. Executing R regenerates the code, and the hint says so (2.6).
    expect(hint).toMatch(/regenerat/i);
    expect(hint).toMatch(/generates the code again/i);

    // 4. It does NOT claim the retry resumes at the named step, because it does
    //    not (V1). A claim about where the new run begins would be the next
    //    false promise on this screen.
    expect(hint).not.toMatch(/resume/i);
    expect(hint).not.toMatch(/skip/i);
    expect(hint).not.toMatch(/picks? up (from|at|where)/i);

    // 5. Automatic regeneration is not offered here, and the hint does not imply
    //    it is. ForceTestReviewApproval is only reachable from TestResultChoice's
    //    Default arm (`orchestration-stack.ts:782-811`) and no edge leaves it for
    //    CreateCode.
    expect(hint).toMatch(/no automatic attempt left/i);

    // Preservation — `LGR-78f241b-1`'s warning-header-plus-explicit-consequence
    // pattern stays: the hint still spells out what Approve anyway means.
    expect(hint).toContain('Approve anyway proceeds with code that failed testing');
  });

  /**
   * P32 T18 / C9 — the reader for `details.remediation_hint` (2.5).
   *
   * Before this change `remediation_hint` occurred nowhere under
   * `dashboard-react/src`: every `test_failure` gate carried the route
   * (`orchestration-stack.ts:585`, defaulted at
   * `approval_handler/lambda_function.py:276`) and no surface showed it
   * (bugfix.md 1.5). These cases pin that the screen names the route the PAYLOAD
   * names rather than a hardcoded one, and that a hint it cannot map to a
   * control is shown verbatim instead of paraphrased into an instruction.
   *
   * `Approval.details` stays `Record<string, unknown>` — the fixtures below feed
   * it as such, per `LGR-99050e6-5`.
   *
   * Validates: Requirements 2.5
   */
  describe('reads details.remediation_hint', () => {
    const renderHint = (details: Record<string, unknown>) => {
      const approval = makeTestFailureApproval({ details });
      const { container } = render(
        <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
      );
      const pending = within(container).getByTestId('pending-approval');
      return within(pending).getByTestId('test-failure-remediation').textContent ?? '';
    };

    it('surfaces the route the payload carries, quoting the hint value', () => {
      const text = renderHint({ remediation_hint: 'retry-from-step:create_code' });
      expect(text).toMatch(/Retry from Step/);
      expect(text).toMatch(/Create Code/);
      expect(text).toContain('retry-from-step:create_code');
    });

    it('names the step the hint names, not a hardcoded one', () => {
      // A different step in the same route family must change the sentence —
      // otherwise the reader is decoration over a hardcoded string.
      const text = renderHint({ remediation_hint: 'retry-from-step:schema_mapping' });
      expect(text).toMatch(/Schema Mapping/);
      expect(text).not.toMatch(/Create Code/);
      expect(text).toContain('retry-from-step:schema_mapping');
    });

    it('shows an unrecognised hint verbatim rather than paraphrasing it', () => {
      const text = renderHint({ remediation_hint: 'open-a-ticket:PLATFORM-42' });
      expect(text).toContain('open-a-ticket:PLATFORM-42');
      expect(text).toMatch(/cannot map to a control/i);
      // It still tells the approver what this dashboard does offer.
      expect(text).toMatch(/Retry from Step/);
    });

    it('names the route without attributing it to a hint when none is carried', () => {
      const text = renderHint({ test_result: { status: 'failed', checks: [] } });
      expect(text).toMatch(/Retry from Step/);
      expect(text).toMatch(/Create Code/);
      expect(text).not.toMatch(/This approval names/);
      expect(text).not.toMatch(/retry-from-step:/);
    });
  });

  it('renders the decided failure context for a completed test_failure approval', () => {
    const approval = makeTestFailureApproval({
      status: 'approved',
      decided_by: 'admin',
      decided_at: '2024-06-01T10:15:00Z',
    });
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const completed = within(container).getByTestId('completed-approval');
    expect(within(completed).getByTestId('approval-artifact-test-failure')).toBeTruthy();
    expect(
      within(completed).getByTestId('approval-artifact-test-failure-alert').textContent,
    ).toContain('Code testing failed');
  });

  it('keeps the plain "Approve" label for schema_mapping approvals (no regression)', () => {
    const approval = makeSchemaMappingApproval({}, populatedEvidence);
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const pending = within(container).getByTestId('pending-approval');
    const approveButton = within(pending).getByTestId('approve-button');
    expect(approveButton.textContent?.trim()).toBe('Approve');
    // No test_failure-only affordances leak into schema_mapping cards.
    expect(within(pending).queryByTestId('test-failure-consequence')).toBeNull();
    expect(within(pending).queryByTestId('approval-artifact-test-failure')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// P32 task 3.2 / bugfix.md 3.14 — the Defect 1 reader fix at each call site
// ---------------------------------------------------------------------------

/**
 * `ApprovalArtifact` has three call sites across two files:
 * `ApprovalWorkflowSection.tsx:251` (pending card), `:437` (completed card) and
 * `ApprovalsPage.tsx:357` (review modal). 3.14 makes each one an obligation,
 * because the Defect 1 fix in `TestFailureArtifact` has blast radius beyond the
 * run-detail page.
 *
 * The two assertions below cover the first two. The third lives in
 * `src/pages/__tests__/ApprovalsPage.artifact.test.tsx`.
 *
 * Why they are not covered by `makeTestFailureApproval` above: that fixture
 * carries `details.report` with per-check `status`/`message` — the *reader*
 * shape, which the `??` chains still accept per 2.2. Exercising it proves the
 * fallback leg, not the leg the producer actually writes. This fixture is
 * transcribed from the producer instead — `details.test_result.checks` from
 * `approval_handler/lambda_function.py:248-267`, per-check `detail` from
 * `test_code/lambda_function.py:234`, `:244`, `:253`, `:261`, `:273`.
 *
 * Validates: Requirements 3.14
 */
const PRODUCER_EXECUTION_DETAIL =
  "Execution failed: PySpark-to-pandas conversion failed: unconvertible PySpark construct 'withColumn' on line 5";
const PRODUCER_STATIC_DETAIL = "Static analysis category 'correctness': PASS";

function makeProducerShapedTestFailureApproval(overrides: Partial<Approval> = {}): Approval {
  return {
    approval_id: 'approval-tf-producer-001',
    pipeline_key: 'test-pipeline',
    run_id: 'run-001',
    approval_type: 'test_failure',
    status: 'pending',
    created_at: '2024-06-01T10:05:00Z',
    details: {
      test_result: {
        status: 'failed',
        checks: [
          { name: 'static_analysis:correctness', passed: true, detail: PRODUCER_STATIC_DETAIL },
          { name: 'code_interpreter_execution', passed: false, detail: PRODUCER_EXECUTION_DETAIL },
        ],
        report_s3_uri: 's3://test-bucket/tests/exec-1/test_report.md',
      },
      test_status: 'failed',
      failed_checks: ['code_interpreter_execution'],
    },
    ...overrides,
  };
}

/** One row per check, with the Details cell carrying that check's `detail`. */
function expectProducerChecksRendered(scope: HTMLElement) {
  const table = within(scope).getByTestId('approval-artifact-test-report');
  const rows = table.querySelectorAll('tbody tr');
  expect(rows).toHaveLength(2);
  const detailsCell = (row: Element) => row.querySelectorAll('td')[2]?.textContent ?? '';
  expect(detailsCell(rows[0])).toContain(PRODUCER_STATIC_DETAIL);
  expect(detailsCell(rows[1])).toContain(PRODUCER_EXECUTION_DETAIL);
  // The neutral no-checks note is the Defect 1 symptom — it must not appear.
  expect(within(scope).queryByTestId('approval-artifact-empty')).toBeNull();
}

describe('ApprovalWorkflowSection – producer-shaped test_failure at both call sites (3.14)', () => {
  it('renders the checks from details.test_result on the PENDING card (ApprovalWorkflowSection.tsx:251)', () => {
    const approval = makeProducerShapedTestFailureApproval();
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const pending = within(container).getByTestId('pending-approval');
    expect(within(pending).getByTestId('approval-artifact-test-failure')).toBeTruthy();
    expectProducerChecksRendered(pending);
  });

  it('renders the checks from details.test_result on the COMPLETED card (ApprovalWorkflowSection.tsx:437)', () => {
    const approval = makeProducerShapedTestFailureApproval({
      status: 'approved',
      decided_by: 'admin',
      decided_at: '2024-06-01T10:15:00Z',
    });
    const { container } = render(
      <ApprovalWorkflowSection approvals={[approval]} runDetails={baseRunDetail} />,
    );

    const completed = within(container).getByTestId('completed-approval');
    expect(within(completed).getByTestId('approval-artifact-test-failure')).toBeTruthy();
    expectProducerChecksRendered(completed);
  });
});
