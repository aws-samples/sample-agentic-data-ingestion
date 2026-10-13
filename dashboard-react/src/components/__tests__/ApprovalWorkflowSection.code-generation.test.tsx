// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * CHARACTERIZATION tests for `ApprovalWorkflowSection`'s `code_generation`
 * render path — the run-detail surface.
 *
 * Feature: p34-code-approval-gate-honors-config, task 6.3 (design B3).
 * Validates: Requirements 3.6, 8.1
 *
 * Two things are pinned here, both on `ApprovalWorkflowSection.tsx`:
 *
 *   1. `renderTypeSpecificContent`'s `code_generation` case (`:147-156`) — the
 *      only thing this surface renders for a code approval today.
 *   2. The `isTestFailure` gate at `:336` (pending card) and its twin at `:559`
 *      (completed card), which is why `ApprovalArtifact` never runs for a
 *      `code_generation` approval on this page at all.
 *
 * ---------------------------------------------------------------------------
 * WHY, AND WHAT THIS FILE IS NOT
 * ---------------------------------------------------------------------------
 *
 * Task 8.1 inverts `CheckCodeApproval`, making a code-generation approval the
 * default outcome of every clean run of every pipeline. Design §3 records the one
 * such approval that has ever existed — approved, with seven `null`s in its
 * `approval_data`. Task 6.4 fixes the screen and gates 8.1 for that reason.
 *
 * This file changes no component behaviour; 6.4 owns the render change. It exists
 * so 6.4's change reads as a deliberate flip.
 *
 * ---------------------------------------------------------------------------
 * STATUS: TASK 6.4 HAS LANDED — THE [DEFECT RECORD]s ARE FLIPPED
 * ---------------------------------------------------------------------------
 *
 * Every `[DEFECT RECORD]` assertion below was flipped in place, with each test
 * name kept verbatim and the reversal recorded in its docstring (`LGR-5906ec6-2`,
 * `LGR-60c4985-6`, both template / ratified). Titles that now describe the old
 * behaviour are left alone on purpose: renaming them would hide the reversal.
 *
 * Two changes worth naming here, because they are not simple `not` removals:
 *
 *   - `test-failure-consequence` staying absent moved from a defect record to a
 *     FORWARD GUARD. A code approval gets its own `code-generation-consequence`
 *     block; reusing the failure block's copy would be the misattribution design
 *     §4.5 forbids.
 *   - `renderTypeSpecificContent`'s singular/plural key mismatch is NOT fixed by
 *     renaming the key at `:152` — that would render `[object Object]`. The plural
 *     assertions therefore stand, and what flipped is that the producer's singular
 *     `test_result` now reaches the card through `ApprovalArtifact`.
 *
 * **Expectations come from requirement 3.6 and requirement 5's user story, not
 * from current output** (design §6, requirements risk 7) — current output is
 * design §3's seven `null`s. Assertions are therefore labelled:
 *
 *   [DEFECT RECORD]  — today's wrong behaviour. Task 6.4 MUST flip it. All are
 *                      absence assertions, so none can silently survive 6.4.
 *   [INVARIANT]      — must hold before and after 6.4.
 *   [FORWARD GUARD]  — holds today and must still hold after 6.4; called out so
 *                      it is not read as a defect record.
 *
 * The convention is this repo's own: `LGR-5906ec6-2` (template / ratified)
 * establishes that a deliberate hold is encoded as a test assertion, and
 * `LGR-60c4985-6` (template / ratified) requires the later amendment stay legible
 * as a reversal rather than read as a regression. The precedent is in the
 * neighbouring `ApprovalWorkflowSection.test.tsx`, whose stage-A `not.toMatch`
 * assertions were flipped to `toMatch` by name once the route they forbade
 * existed.
 *
 * ---------------------------------------------------------------------------
 * THE COVERAGE FINDING, CONFIRMED BY SEARCH
 * ---------------------------------------------------------------------------
 *
 * Design §3 flags `renderTypeSpecificContent` and `PendingApprovalCard` as
 * "⚠️ none at symbol level", and design §5 risk 11 asks that this be confirmed by
 * search before being treated as fact.
 *
 * Confirmed, with a distinction the flag does not draw:
 *
 *   - `renderTypeSpecificContent` — **genuinely unasserted.** A search for
 *     `type-content-` across `dashboard-react/src` returns exactly one hit, the
 *     `data-testid` definition at `ApprovalWorkflowSection.tsx:149` itself. No
 *     test in the repo asserts on any of its three branches. It is *executed* by
 *     every render test in the sibling suite, so a crash would surface, but
 *     nothing pins its output. This file is that function's first assertion.
 *
 *   - `PendingApprovalCard` (`:302`) — **false negative.** It is not exported, so
 *     no test imports it, but its rendered output is asserted repeatedly through
 *     the `pending-approval` testid (`ApprovalWorkflowSection.test.tsx:265`,
 *     `:333`, `:392`, `:452`, `:532`). Not uncovered; unreachable by name.
 *
 *   - `ApprovalWorkflowSection.test.tsx:171` does build a `code_generation`
 *     approval, but only to assert that schema-mapping evidence and questions do
 *     NOT render for it. It asserts nothing about the code branch.
 *
 * Frontend imports here are ordinary ES modules, so the `_mod.` aliasing that
 * `requirements.md` §CodeGraph blamed for the Python false negatives does not
 * apply — yet CodeGraph still reports "no covering tests found" for
 * `ApprovalArtifact`, which has three test files. Checking by search rather than
 * trusting the flag was the right instruction.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, within } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail } from '../../types';

// `ApprovalWorkflowSection` → `ApprovalArtifact` → `useCodeVersion` (via
// CodeViewer) and `useBedrockModels` (via MappingViewer) at module scope. The
// gate at `:336` means neither is called today; the mock is here so this file
// keeps working when task 6.4 opens that gate and the CodeViewer starts
// rendering. Same convention as `ApprovalsPage.artifact.test.tsx:44-49`.
vi.mock('../../hooks', () => ({
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
}));

const baseRunDetail: RunDetail = {
  run_id: 'run-2026-08-06-13-36-42-040fb0eb',
  pipeline_key: 'hello123-c4827272',
  status: 'RUNNING',
  started_at: '2026-08-06T13:36:42Z',
};

// ---------------------------------------------------------------------------
// Fixtures — transcribed from the PRODUCERS, not from the reader
// ---------------------------------------------------------------------------

/**
 * `{name, passed, detail}` per check, as `aggregate_check_results` emits
 * (`test_code/lambda_function.py:234`, `:244`, `:253`, `:261`, `:273`), inside the
 * `{status, checks, report_s3_uri}` object built at `:940-944` and published as
 * `test_result` — **singular**. That key matters; see the mismatch test below.
 *
 * A PASSING run: requirement 1.1's clean run, the case task 8.1 makes routine.
 * `LGR-28b4d75-5` (template): a fixture built from the reader's shape has the
 * reader as its own oracle, so these come from the writer.
 */
const PASSING_CHECK_STATIC = "Static analysis category 'correctness': PASS";
const PASSING_CHECK_EXECUTION = 'Transform executed against 100 sampled rows; output row count matched';

const PRODUCER_TEST_RESULT = {
  status: 'passed',
  checks: [
    { name: 'static_analysis:correctness', passed: true, detail: PASSING_CHECK_STATIC },
    { name: 'code_interpreter_execution', passed: true, detail: PASSING_CHECK_EXECUTION },
  ],
  report_s3_uri: 's3://test-bucket/tests/exec-1/test_report.md',
};

/**
 * The post-6.2 payload as the run-detail endpoint delivers it. Task 6.2 (wave 1,
 * complete) fixed `approval_handler.get_approval_data`'s `code_generation` branch
 * to read the paths the producers actually write, so `code_version_id`, the real
 * code fields and `test_result` are all present. `pipelines_runs.py:616-641`
 * parses `approval_data` into `details`, so on THIS surface `details` is
 * populated — unlike the ApprovalsPage surface, where neither
 * `approvals.py:155-176` nor `:184-200` parses it and `details` is `undefined`.
 *
 * That is what makes this surface the sharper statement of requirement 3.6's gap:
 * the evidence is right here, and the screen shows a nine-word placeholder.
 */
const POST_6_2_DETAILS: Record<string, unknown> = {
  code_version_id: 'code-v7',
  generated_code_s3_uri: 's3://test-bucket/code/exec-1/transform.py',
  code_quality_metrics: { lines: 42, complexity: 3 },
  test_result: PRODUCER_TEST_RESULT,
};

function makeCodeGenApproval(overrides: Partial<Approval> = {}): Approval {
  return {
    approval_id: 'appr-codegen-1',
    pipeline_key: 'hello123-c4827272',
    run_id: 'run-2026-08-06-13-36-42-040fb0eb',
    approval_type: 'code_generation',
    status: 'pending',
    created_at: '2026-08-06T13:38:05Z',
    ...overrides,
  };
}

const CHECK_EVIDENCE_STRINGS = [
  'static_analysis:correctness',
  'code_interpreter_execution',
  PASSING_CHECK_STATIC,
  PASSING_CHECK_EXECUTION,
];

// ---------------------------------------------------------------------------
// renderTypeSpecificContent — the code_generation case (:147-156)
// ---------------------------------------------------------------------------

describe('ApprovalWorkflowSection — renderTypeSpecificContent code_generation (characterization)', () => {
  /**
   * [DEFECT RECORD — FLIPPED by task 6.4] Name kept verbatim, reversal recorded
   * here per `LGR-5906ec6-2` / `LGR-60c4985-6` (both template / ratified).
   *
   * The gate at `:336` now admits `code_generation` through the shared
   * `rendersArtifact` predicate, so the code and its check list both render on
   * this surface. The `??` default at `:151` changed too: `Generated code pending
   * review` was the only content a code approval had, and on the completed card —
   * which calls the same function at `:558` — it was also false, since the review
   * had already happened.
   *
   * The record of what was wrong:
   *
   * With the post-6.2 payload fully populated, the entire code-approval content
   * on this surface is the literal string `Generated code pending review` — the
   * `??` default at `:151`, because `get_approval_data` publishes no
   * `code_summary` key. A header, one sentence of placeholder text, and two live
   * buttons.
   *
   * `ApprovalArtifact` is not reached: the gate at `:336` is
   * `{isTestFailure && <ApprovalArtifact approval={approval} />}`, and
   * `isTestFailure` (`:313`) is `approval_type === 'test_failure'`. So the code
   * the reviewer is approving is not on screen either, even though
   * `details.code_version_id` is present and `ApprovalArtifact`'s own
   * `code_generation` branch would render it.
   *
   * R5's user story is the target: "see the test results alongside the code I am
   * approving". Neither is shown.
   */
  it('shows only placeholder text for a fully populated payload — no code, no checks', () => {
    const { container } = render(
      <ApprovalWorkflowSection
        approvals={[makeCodeGenApproval({ details: POST_6_2_DETAILS })]}
        runDetails={baseRunDetail}
      />,
    );

    const pending = within(container).getByTestId('pending-approval');
    const content = within(pending).getByTestId('type-content-code-generation');

    // FLIPPED — the placeholder is gone. This payload records no `code_summary`,
    // and the line now says so instead of claiming a review is pending.
    expect(content.textContent).not.toContain('Generated code pending review');
    expect(content.textContent).toContain('No code summary was recorded for this approval.');

    // FLIPPED — the gate at `:336` is open, so the artifact renders. The code
    // wrapper is present because `details.code_version_id` is; `-empty` stays null
    // for the same reason, and it is the branch that would render if the id went
    // missing.
    expect(within(pending).getByTestId('approval-artifact-code')).toBeTruthy();
    expect(within(pending).queryByTestId('approval-artifact-empty')).toBeNull();
    expect(within(pending).getByTestId('approval-artifact-test-report')).toBeTruthy();

    // FLIPPED — every check name and detail the producer wrote now reaches this
    // surface, which is requirement 3.6's consumer half and R5's user story.
    const text = pending.textContent ?? '';
    for (const evidence of CHECK_EVIDENCE_STRINGS) {
      expect(text).toContain(evidence);
    }
  });

  /**
   * [DEFECT RECORD] Task 6.4 MUST flip the singular-key assertion.
   *
   * The reader/producer key mismatch, isolated. `:152-154` renders a `Test
   * results` line from `details.test_results` — **plural**. The producer writes
   * `test_result` — **singular** (`test_code/lambda_function.py:1722`, published
   * through `get_approval_data`). Design §4.5 records the mismatch; this pins it.
   *
   * Both halves are asserted, because only together do they show it is a key
   * mismatch and not an empty payload: the singular key the producer writes is
   * ignored, and the plural key nothing writes would render if it existed.
   *
   * Note what 6.4 must NOT do about it: renaming `test_results` to `test_result`
   * at `:152` would make the line render `[object Object]`, since the producer's
   * value is the `{status, checks, report_s3_uri}` object and `:153` wraps it in
   * `String(...)`. The fix is to render the checks list, which is what task 6.4
   * specifies.
   */
  it('ignores the producer singular test_result while reading a plural test_results nothing writes', () => {
    // [DEFECT RECORD] Singular — what the producer writes. Ignored today.
    const { container: withSingular } = render(
      <ApprovalWorkflowSection
        approvals={[makeCodeGenApproval({ details: { test_result: PRODUCER_TEST_RESULT } })]}
        runDetails={baseRunDetail}
      />,
    );
    const singularContent = within(withSingular)
      .getByTestId('type-content-code-generation')
      .textContent ?? '';
    // NOT flipped, and deliberately so. `:152` still reads the plural key and
    // still ignores the singular one, because renaming it would render
    // `[object Object]` — `:153` wraps the value in `String(...)` and the
    // producer's value is the `{status, checks, report_s3_uri}` object.
    expect(singularContent).not.toContain('Test results:');
    for (const evidence of CHECK_EVIDENCE_STRINGS) {
      expect(singularContent).not.toContain(evidence);
    }

    // FLIPPED — this is where the mismatch stopped mattering. The producer's
    // singular `test_result` is no longer ignored by the card: `ApprovalArtifact`,
    // a sibling of the div above, reads it and renders the checks list. Scoped to
    // the whole card rather than to `type-content-code-generation`, because the
    // fix was to render the evidence somewhere true, not to bend this line.
    const singularCard = within(withSingular).getByTestId('pending-approval').textContent ?? '';
    for (const evidence of CHECK_EVIDENCE_STRINGS) {
      expect(singularCard).toContain(evidence);
    }

    // [INVARIANT] Plural — the key the reader looks for. Renders when supplied,
    // which is how we know the branch works and the key is the problem. No
    // producer emits this shape.
    const { container: withPlural } = render(
      <ApprovalWorkflowSection
        approvals={[makeCodeGenApproval({ details: { test_results: '2/2 checks passed' } })]}
        runDetails={baseRunDetail}
      />,
    );
    expect(
      within(withPlural).getByTestId('type-content-code-generation').textContent,
    ).toContain('Test results: 2/2 checks passed');
  });

  /**
   * [DEFECT RECORD — FLIPPED by task 6.4] Name kept verbatim, reversal recorded
   * here (`LGR-5906ec6-2`, `LGR-60c4985-6`).
   *
   * `details` absent — the ApprovalsPage-delivered shape, reachable on this
   * surface too whenever `approval_data` is unparsed or empty. The finding this
   * test recorded was that the payload made no difference to what a reviewer saw.
   * It does now: with no payload the card says what is missing — no code preview,
   * no per-check results — instead of showing a placeholder that reads like
   * everything is in hand. The two absence assertions are kept because with no
   * `code_version_id` and no `test_result` there is genuinely nothing to render;
   * they now sit beside the notes that say so.
   */
  it('renders the same placeholder when details is absent', () => {
    const { container } = render(
      <ApprovalWorkflowSection approvals={[makeCodeGenApproval()]} runDetails={baseRunDetail} />,
    );

    const pending = within(container).getByTestId('pending-approval');
    // FLIPPED — no more `Generated code pending review`.
    expect(within(pending).getByTestId('type-content-code-generation').textContent).not.toContain(
      'Generated code pending review',
    );
    expect(within(pending).queryByTestId('approval-artifact-code')).toBeNull();
    expect(within(pending).queryByTestId('approval-artifact-test-report')).toBeNull();
    // FLIPPED — the artifact renders and states both absences explicitly.
    expect(within(pending).getByTestId('approval-artifact-empty').textContent).toContain(
      'not available for preview',
    );
    expect(within(pending).getByTestId('approval-artifact-no-checks').textContent).toContain(
      'No per-check results were recorded for this approval',
    );
  });

  /**
   * [INVARIANT — not a defect record] `null` details must not throw, before or
   * after 6.4.
   *
   * `renderTypeSpecificContent` coalesces at `:132` (`approval.details ?? {}`),
   * so `null` collapses to `{}`. `Approval.details` is typed
   * `Record<string, unknown> | undefined` (`types.ts:138`) and is deliberately
   * NOT narrowed or widened here — `LGR-99050e6-5` (template / ratified) rejected
   * changing a frontend type to accommodate a reader. The cast is at the fixture
   * site, matching how `remediationHint` (`:54-57`) reads the same field.
   *
   * An invariant rather than a defect record: the guard is correct, and 6.4 must
   * keep it while adding a `details.test_result.checks` read that could easily
   * introduce an unguarded dereference.
   */
  it('does not throw when details is present but null', () => {
    const nullDetails = makeCodeGenApproval({
      details: null as unknown as Record<string, unknown>,
    });

    expect(() =>
      render(<ApprovalWorkflowSection approvals={[nullDetails]} runDetails={baseRunDetail} />),
    ).not.toThrow();

    const { container } = render(
      <ApprovalWorkflowSection approvals={[nullDetails]} runDetails={baseRunDetail} />,
    );
    // The invariant is the guard, and it holds: `null` still collapses to `{}` at
    // `:132`, and 6.4's new `details.test_result.checks` read in `ApprovalArtifact`
    // is guarded the same way, so neither throws. The asserted string moved with
    // 6.4's copy change — the subject of this test is the non-throwing read and
    // the type-content div still rendering, not the placeholder wording.
    expect(
      within(container)
        .getAllByTestId('type-content-code-generation')[0]
        .textContent,
    ).toContain('No code summary was recorded for this approval.');
  });
});

// ---------------------------------------------------------------------------
// The isTestFailure gate at :336 and :559
// ---------------------------------------------------------------------------

describe('ApprovalWorkflowSection — the isTestFailure artifact gate (characterization)', () => {
  /**
   * [DEFECT RECORD] Task 6.4 MUST flip this — it is the line 6.4 widens.
   *
   * `:336` renders `ApprovalArtifact` only for `test_failure`. The record for
   * that line is `LGR-78f241b-1` (**backfilled** — the author's own words, not a
   * ratified decision: "render a failure artifact for test_failure approvals with
   * an 'Approve anyway' action and a consequence hint"). It scoped the artifact to
   * failures because at the time a `code_generation` approval was unreachable in
   * practice — design §3 confirms exactly one has ever been created in the
   * account, downstream of a `test_failure`. Task 8.1 removes that premise.
   *
   * Asserted on both call sites, because the sibling suite treats them as
   * separate obligations (its 3.14 block covers `:336` and `:559` in two tests)
   * and a 6.4 that widens only the pending card would leave the completed card
   * showing nothing about an approval already granted.
   */
  it('renders no ApprovalArtifact for a pending code_generation approval (:336)', () => {
    const { container } = render(
      <ApprovalWorkflowSection
        approvals={[makeCodeGenApproval({ details: POST_6_2_DETAILS })]}
        runDetails={baseRunDetail}
      />,
    );

    const pending = within(container).getByTestId('pending-approval');

    // FLIPPED — the line 6.4 widened. `code_generation` now passes
    // `rendersArtifact`, so the artifact renders on the pending card.
    expect(within(pending).getByTestId('approval-artifact-code-generation')).toBeTruthy();
    expect(within(pending).getByTestId('approval-artifact-code')).toBeTruthy();

    // Still absent, each for its own reason, and each still worth asserting:
    // `-code-diff` needs a `previous_code_version_id` this fixture does not carry;
    // `-empty` is the no-code-id branch; and the last two are the FORWARD GUARD —
    // widening the gate must not route a code approval into `TestFailureArtifact`
    // or `UnknownArtifact`.
    for (const testId of [
      'approval-artifact-code-diff',
      'approval-artifact-empty',
      'approval-artifact-test-failure',
      'approval-artifact-unknown',
    ]) {
      expect(within(pending).queryByTestId(testId)).toBeNull();
    }
  });

  it('renders no ApprovalArtifact for a completed code_generation approval (:559)', () => {
    const { container } = render(
      <ApprovalWorkflowSection
        approvals={[
          makeCodeGenApproval({
            status: 'approved',
            decided_by: 'admin',
            decided_at: '2026-08-06T13:40:45Z',
            details: POST_6_2_DETAILS,
          }),
        ]}
        runDetails={baseRunDetail}
      />,
    );

    const completed = within(container).getByTestId('completed-approval');
    expect(within(completed).getByTestId('type-content-code-generation')).toBeTruthy();

    // FLIPPED — `:559` was widened through the same predicate as `:336`, so a
    // granted code approval shows what was approved. Widening only the pending
    // card would have left the audit half of design §3's incident in place.
    expect(within(completed).getByTestId('approval-artifact-code')).toBeTruthy();
    expect(within(completed).getByTestId('approval-artifact-test-report')).toBeTruthy();

    // FORWARD GUARD, unchanged.
    for (const testId of [
      'approval-artifact-empty',
      'approval-artifact-test-failure',
      'approval-artifact-unknown',
    ]) {
      expect(within(completed).queryByTestId(testId)).toBeNull();
    }
  });

  /**
   * [DEFECT RECORD — FLIPPED by task 6.4] Name kept verbatim, reversal recorded
   * here (`LGR-5906ec6-2`, `LGR-60c4985-6`).
   *
   * The card now states what each choice does, in its own block with its own test
   * id. Both buttons stay live and the label stays the plain `Approve` — design §5
   * risk 1's fallback of disabling approve behind a banner was not reached,
   * because the artifact renders.
   *
   * The record of what was wrong:
   *
   * The buttons are live and unqualified. `test_failure` approvals get a
   * `test-failure-consequence` block (`:405-412`) naming what each choice does —
   * `LGR-78f241b-1`'s house pattern, with the state carried in text rather than
   * colour per `LGR-99050e6-1` (template / ratified, WCAG 1.4.1). A
   * `code_generation` approval gets nothing: a plain `Approve` button, a plain
   * `Reject` button, and no sentence anywhere saying what approving authorises.
   *
   * That is the exact shape of design §3's incident. One human clicked Approve on
   * generated code having been shown nothing; 8.1 makes that click the default
   * step in every clean run.
   *
   * 6.4 must state the consequence for this type too. The button labels are
   * asserted as an invariant — `Approve anyway` belongs to a failing run and would
   * be the same misattribution task 6.4 is told not to make.
   */
  it('offers live buttons with no consequence statement for a code_generation approval', () => {
    const { container } = render(
      <ApprovalWorkflowSection
        approvals={[makeCodeGenApproval({ details: POST_6_2_DETAILS })]}
        runDetails={baseRunDetail}
      />,
    );

    const pending = within(container).getByTestId('pending-approval');

    // [INVARIANT] Both buttons live and enabled. 6.4 keeps them live; design §5
    // risk 1's fallback (disabling approve behind a banner) is only reached if
    // 6.4 cannot render the artifact at all.
    const approveButton = within(pending).getByTestId('approve-button') as HTMLButtonElement;
    expect(approveButton.disabled).toBe(false);
    expect(within(pending).getByTestId('reject-button')).toBeTruthy();

    // [INVARIANT / FORWARD GUARD] The plain label, not `Approve anyway`. That
    // string belongs to a failing run; reusing it here would be the
    // misattribution task 6.4 is explicitly told not to create.
    expect(approveButton.textContent?.trim()).toBe('Approve');

    // FLIPPED — there is a consequence block, and it is `code_generation`'s OWN.
    // `test-failure-consequence` staying null is now a FORWARD GUARD rather than a
    // defect record: reusing that block would put `Approve anyway proceeds with
    // code that failed testing` in front of a passing run, which is the
    // misattribution design §4.5 forbids. The flip is the new test id.
    expect(within(pending).queryByTestId('test-failure-consequence')).toBeNull();
    expect(within(pending).getByTestId('code-generation-consequence')).toBeTruthy();

    // FLIPPED — both consequences are stated in TEXT (`LGR-99050e6-1`, template /
    // ratified, WCAG 1.4.1), and each is read off the definition:
    // `waitForCodeApproval.next(createInfrastructure)`
    // (`orchestration-stack.ts:908`) for approve, and the state's
    // `Catch[States.ALL] → RecordFailure` (`:672`) for reject.
    const text = pending.textContent ?? '';
    expect(text).toMatch(/proceeds with/i);
    expect(text).toMatch(/fails the run/i);

    // FLIPPED — the artifact's banner. `[data-analytics-alert]` is the attribute
    // Cloudscape's Alert puts on its own root; `role="alert"` appears nowhere in
    // its markup, so probing for that would have made this assertion vacuous and
    // it would have kept passing after 6.4.
    expect(pending.querySelectorAll('[data-analytics-alert]')).toHaveLength(1);
  });

  /**
   * [FORWARD GUARD for task 6.4 — deliberately not a defect record]
   *
   * Holds today and MUST STILL HOLD after 6.4.
   *
   * Design §4.5 establishes that `TestFailureArtifact`'s `notVerifiedOnly` banner
   * (condition `ApprovalArtifact.tsx:318`, Alert `:322-330`) is not reachable on a
   * `code_generation` approval, and that task 6.4 must not make it so: for a code
   * approval the
   * checks list is evidence *in favour*, so `Code testing failed` would be a
   * misattribution created by the fix rather than found by it. Widening the gate
   * at `:336` is exactly the change that could do it, which is why the guard sits
   * in this describe block.
   */
  it('never shows test-failure copy on a code_generation approval', () => {
    const { container } = render(
      <ApprovalWorkflowSection
        approvals={[makeCodeGenApproval({ details: POST_6_2_DETAILS })]}
        runDetails={baseRunDetail}
      />,
    );

    const pending = within(container).getByTestId('pending-approval');
    const text = pending.textContent ?? '';
    expect(text).not.toContain('Code testing failed');
    expect(text).not.toContain('Code testing did not verify this transform');
    expect(text).not.toContain('pandas proxy');
    expect(text).not.toContain('Approve anyway');
  });
});
