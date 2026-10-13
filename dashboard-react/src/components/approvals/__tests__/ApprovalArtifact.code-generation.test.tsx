// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * CHARACTERIZATION tests for `ApprovalArtifact`'s `code_generation` branch
 * (`ApprovalArtifact.tsx:443-458`) — the ApprovalsPage / review-modal surface.
 *
 * Line anchors below are re-verified against the working tree, and several differ
 * from design §3/§4.5 by a few lines — the same drift design §4.4 recorded for the
 * ASL anchors. The corrected ones: the `code_generation` branch is `:443-458` (not
 * `:443-459`); the `test_failure` branch is `:479-485` (not `:465-471`);
 * `TestFailureArtifact` is `:307-383` (not `:307-405`); its banner Alert is
 * `:322-330` (not `:318-329`, though the `notVerifiedOnly` condition it depends on
 * is `:318` as cited); `UnknownArtifact` is `:408-438`. The file is 496 lines, as
 * design §4.5 states.
 *
 * Feature: p34-code-approval-gate-honors-config, task 6.3 (design B3).
 * Validates: Requirements 3.6, 8.1
 *
 * ---------------------------------------------------------------------------
 * WHY THIS FILE EXISTS, AND WHAT IT IS NOT
 * ---------------------------------------------------------------------------
 *
 * Task 8.1 inverts `CheckCodeApproval` so every clean run of every pipeline
 * pauses for a code-generation approval. Today that screen shows a reviewer
 * almost nothing. Design §3 records the one `code_generation` approval that has
 * ever existed in the account — `newtest-42e43d82_code_generation_1785849245`,
 * status `approved`, `approval_data` carrying seven keys and seven `null`s. A
 * human approved generated code having been shown nothing about it.
 *
 * This file pins what that branch renders **today**, so task 6.4's change is a
 * visible, deliberate flip rather than an inferred improvement. It changes no
 * component behaviour — 6.4 owns the render change.
 *
 * ---------------------------------------------------------------------------
 * STATUS: TASK 6.4 HAS LANDED — THE [DEFECT RECORD]s ARE FLIPPED
 * ---------------------------------------------------------------------------
 *
 * Every `[DEFECT RECORD]` assertion below was flipped by task 6.4, in place, with
 * each test name kept verbatim and the reversal recorded in its docstring — this
 * repo's convention for a deliberate reversal (`LGR-5906ec6-2`, template /
 * ratified; applied to this component's sibling suite by `LGR-60c4985-6`, also
 * template / ratified). A test whose title now describes the old behaviour is
 * deliberate: renaming it would hide that the reversal happened.
 *
 * Three assertions did **not** flip, and are annotated where they sit: the two
 * fixtures with no `test_result` carry no check evidence, so no table can honestly
 * render for them. What flipped there is that the absence is now stated on screen
 * (`approval-artifact-no-checks`) instead of being a silent blank.
 *
 * The `[INVARIANT]`s hold. The `[FORWARD GUARD]` — none of
 * `TestFailureArtifact`'s strings on a `code_generation` approval — holds, and it
 * is now load-bearing rather than incidental: 6.4 renders a check table on this
 * path using the same shared columns, so the guard is what keeps the failure
 * copy from coming with them.
 *
 * The component grew with 6.4, so the line anchors below are the pre-6.4 ones.
 * The `code_generation` branch now delegates to `CodeGenerationArtifact`; read
 * that component's docstring for the current shape.
 *
 * **Expectations are derived from requirement 3.6 and requirement 5's user
 * story, not from current output** (design §6, requirements risk 7). Current
 * output is the defect. So each test below labels its assertions as either:
 *
 *   [DEFECT RECORD] — records today's wrong behaviour. Task 6.4 MUST flip this
 *                     assertion. If 6.4 lands and this still passes, 6.4 is
 *                     incomplete. Every such assertion here is an absence
 *                     assertion, so it breaks the moment 6.4 renders anything.
 *
 *   [INVARIANT]     — must hold before AND after 6.4. Called out explicitly so
 *                     it is not mistaken for something 6.4 should change.
 *
 * That split follows this repo's own convention for a deliberate hold encoded as
 * an assertion (`LGR-5906ec6-2`, template / ratified; applied to this component's
 * sibling suite by `LGR-60c4985-6`, template / ratified). The precedent is
 * literally in the neighbouring file: `ApprovalWorkflowSection.test.tsx`'s
 * stage-A `not.toMatch` assertions were flipped to `toMatch` by name when the
 * route they forbade was built.
 *
 * ---------------------------------------------------------------------------
 * THE COVERAGE FINDING, CONFIRMED BY SEARCH — AND CORRECTED
 * ---------------------------------------------------------------------------
 *
 * Design §3 lists `ApprovalArtifact` as "⚠️ none at symbol level … the
 * `code_generation` branch has no covering test", and design §5 risk 11 asks that
 * this be confirmed by search rather than assumed, because the corrected
 * `requirements.md` §CodeGraph found the same flag to be a false negative for two
 * of three Python symbols.
 *
 * Confirmed by search, and **the flag is a false negative here too**:
 * `ApprovalArtifact.test.tsx:60` already carries
 * `describe('ApprovalArtifact — code_generation (R2.1)')` with three tests —
 * `:61` (CodeViewer renders), `:74` (diff renders), `:89` (missing
 * `code_version_id` is safe). CodeGraph reports "no covering tests found" for
 * `ApprovalArtifact` despite an ordinary ES-module `import ApprovalArtifact from
 * '../ApprovalArtifact'`, so the false negative is not the `_mod.` aliasing
 * `requirements.md` §CodeGraph blamed for the Python cases.
 *
 * What those three tests do NOT cover, and what this file adds:
 *   - `details` absent entirely (the real ApprovalsPage shape — `:89` passes
 *     `details: {}`, which is not the same object).
 *   - `details` present but null.
 *   - The R3.6 gap: the producer's `test_result.checks` reaching the payload and
 *     NOT reaching the screen. `:61` asserts the code renders; nothing asserts
 *     the reviewer can see what the tests said about it.
 *
 * ---------------------------------------------------------------------------
 * WHY `details` IS ABSENT ON THIS SURFACE
 * ---------------------------------------------------------------------------
 *
 * On the ApprovalsPage path `approval.details` is `undefined`. Neither
 * `get_approvals` (`dashboard_api/handlers/approvals.py:155-176`) nor
 * `get_pending_approvals` (`:184-200`) parses `approval_data` — both only
 * normalise `pipeline_key` and sort — so the record reaches the client with
 * `approval_data` unparsed and no `details` key at all. Only the run-detail
 * endpoint parses it (`pipelines_runs.py:616-641`). `approval_data` occurs
 * nowhere under `dashboard-react/src`. (Design §4.5, read there; not re-read in
 * this frontend-only task.)
 *
 * Task 6.2 (wave 1, complete) fixed `approval_handler.get_approval_data`'s
 * `code_generation` branch, so the stored payload now carries a real
 * `code_version_id`, the real code fields and `test_result`. That moved the
 * boundary: on the run-detail path the code now renders. It did NOT make the
 * checks visible on either path, and it did not give the ApprovalsPage path a
 * `details` key. Both remain 6.4's work.
 *
 * `Approval.details` is deliberately NOT narrowed here — `LGR-99050e6-5`
 * (template / ratified) rejected narrowing a frontend type to satisfy a reader,
 * and `types.ts:138` keeps `details?: Record<string, unknown>` on purpose. The
 * null fixture casts at the fixture site instead, which is also how the
 * component itself handles it (`detailStr`, `ApprovalArtifact.tsx:52-60`).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ApprovalArtifact from '../ApprovalArtifact';
import type { Approval } from '../../../types';

const GENERATED_CODE = 'def transform(df):\n    return df.withColumn("total", col("qty") * col("price"))';

// `ApprovalArtifact` imports `useCodeVersion` (via CodeViewer) and
// `useBedrockModels` (via MappingViewer) at module scope, so both must exist on
// the mock even when the branch under test calls neither. Same convention as
// `ApprovalArtifact.test.tsx:27-48` and `ApprovalArtifact.property.test.tsx:36-39`.
vi.mock('../../../hooks', () => ({
  useCodeVersion: (id: string | undefined) => {
    if (id === 'code-v7') {
      return {
        data: {
          code_version_id: id,
          code: GENERATED_CODE,
          code_content: GENERATED_CODE,
          language: 'python',
          created_at: '',
        },
        isLoading: false,
        isError: false,
      };
    }
    return { data: undefined, isLoading: false, isError: false };
  },
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
}));

// ---------------------------------------------------------------------------
// Fixtures — transcribed from the PRODUCERS, not from the reader
// ---------------------------------------------------------------------------

/**
 * Per-check rows as `aggregate_check_results` emits them: `{name, passed,
 * detail}` (`test_code/lambda_function.py:234`, `:244`, `:253`, `:261`, `:273`),
 * wrapped in the `{status, checks, report_s3_uri}` object built at `:940-944`
 * and published as `test_result`.
 *
 * A PASSING run, deliberately — this is the shape requirement 1.1's clean run
 * produces and the shape task 8.1 makes routine. `LGR-28b4d75-5` (template):
 * a fixture transcribed from the reader has the reader as its own oracle, so
 * these come from the writer.
 */
const PASSING_CHECK_STATIC = "Static analysis category 'correctness': PASS";
const PASSING_CHECK_EXECUTION = 'Transform executed against 100 sampled rows; output row count matched';
const NOT_VERIFIED_DETAIL = 'schema_compliance not evaluated: no target schema available for this pipeline';

const PRODUCER_TEST_RESULT = {
  status: 'passed',
  checks: [
    { name: 'static_analysis:correctness', passed: true, detail: PASSING_CHECK_STATIC },
    { name: 'code_interpreter_execution', passed: true, detail: PASSING_CHECK_EXECUTION },
  ],
  report_s3_uri: 's3://test-bucket/tests/exec-1/test_report.md',
};

/**
 * The same result carrying a Not_Verified_Check, the shape `test_code:515-538`
 * emits for `schema_compliance` when no target schema is available. Its
 * `not_verified_reason` is `no_target_schema` (`test_code:235`), which
 * `NOT_VERIFIED_LABELS` (`ApprovalArtifact.tsx:209-212`) does not map — task 6.4
 * adds that entry.
 */
const PRODUCER_TEST_RESULT_WITH_NOT_VERIFIED = {
  status: 'passed',
  checks: [
    ...PRODUCER_TEST_RESULT.checks,
    {
      name: 'schema_compliance',
      passed: false,
      verified: false,
      not_verified_reason: 'no_target_schema',
      detail: NOT_VERIFIED_DETAIL,
    },
  ],
  report_s3_uri: PRODUCER_TEST_RESULT.report_s3_uri,
};

/** The post-6.2 `code_generation` payload, as `get_approval_data` now returns it. */
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

/** Every string a reviewer would need to see to satisfy R3.6 / R5's user story. */
const CHECK_EVIDENCE_STRINGS = [
  'static_analysis:correctness',
  'code_interpreter_execution',
  PASSING_CHECK_STATIC,
  PASSING_CHECK_EXECUTION,
];

// ---------------------------------------------------------------------------
// `details` absent — the real ApprovalsPage shape
// ---------------------------------------------------------------------------

describe('ApprovalArtifact — code_generation, details ABSENT (characterization)', () => {
  /**
   * [DEFECT RECORD — FLIPPED by task 6.4] The name is kept verbatim and the
   * reversal is recorded here rather than in a rename, per `LGR-5906ec6-2` and
   * `LGR-60c4985-6` (both template / ratified): an amendment has to read as a
   * deliberate reversal, not as a regression. The test now asserts the opposite
   * of its own title — there IS a banner, and it DOES state the consequence.
   *
   * Two assertions did not flip, and could not: this fixture carries no
   * `test_result`, so there is no check evidence to render. That pair is
   * annotated at the assertion site, and the populated fixture further down is
   * where the table flips.
   *
   * The record of what was wrong, kept because it is the reason this file exists:
   *
   * With no `details` key at all — the shape `approvals.py:155-176` actually
   * delivers — `detailStr` (`ApprovalArtifact.tsx:52-60`) finds no
   * `code_version_id` on `details` or on the approval top level, so the branch
   * returns a single quiet `Box`: nine words of placeholder text, directly above
   * two live approve/reject buttons.
   *
   * The comparison that makes this a defect rather than a limitation is inside
   * this same file. For an approval type it does not recognise at all,
   * `ApprovalArtifact` renders `UnknownArtifact` (`:408-438`) — a warning Alert
   * with a header, a statement that the buttons are live, and an instruction to
   * reject unless the approval can be confirmed elsewhere. A `code_generation`
   * approval in exactly the same evidentiary position is treated more quietly
   * than an unrecognised one.
   *
   * R5's user story ("see the test results alongside the code I am approving")
   * and `LGR-78f241b-1`'s house pattern (backfilled — the author's words for the
   * approval-banner pattern: a header plus an explicit statement of what
   * approving means) are the target. `LGR-99050e6-1` (template / ratified) adds
   * that the state must be carried in TEXT, not colour alone (WCAG 1.4.1).
   */
  it('renders only a quiet placeholder — no banner, no consequence statement, no evidence', () => {
    const { container } = render(<ApprovalArtifact approval={makeCodeGenApproval()} />);

    // FLIPPED by task 6.4. The note survives, because with no `code_version_id`
    // there is still no code to show and saying so is still the honest thing —
    // but it is no longer the whole artifact, and its copy now names what is
    // missing relative to the banner above it instead of reading like a feature
    // description. `ApprovalArtifact.test.tsx:89` asserts this test id's presence
    // for the same fixture and passes unchanged.
    const placeholder = screen.getByTestId('approval-artifact-empty');
    expect(placeholder.textContent).toBe(
      'The generated code is not available for preview, so nothing here shows the code itself.',
    );

    // FLIPPED by task 6.4 — the `code_generation` banner. `[data-analytics-alert]`
    // is the attribute Cloudscape's Alert puts on its own root; `role="alert"`
    // appears nowhere in its markup, so probing for that would have made this
    // assertion vacuous and it would have kept passing after 6.4.
    expect(container.querySelectorAll('[data-analytics-alert]')).toHaveLength(1);

    // FLIPPED by task 6.4. The banner now states what each choice does, which is
    // `LGR-78f241b-1`'s house pattern (backfilled), and it carries the check state
    // in its header TEXT rather than in the alert colour (`LGR-99050e6-1`,
    // template / ratified, WCAG 1.4.1). `\breject\b` matches the standalone word
    // deliberately: "Rejecting" would not, and the copy says "Reject to stop the
    // run at this gate".
    const text = container.textContent ?? '';
    expect(text).toMatch(/approv/i);
    expect(text).toMatch(/\breject\b/i);

    // NOT flipped, and honestly cannot be for THIS fixture: `details` is absent,
    // so the payload carries no `test_result` and there are no checks to put in a
    // table. Rendering an empty table here would assert an evidence surface that
    // has no evidence behind it. The flip for this pair is the third assertion —
    // the absence is now STATED on screen rather than being a silent blank, which
    // is the same choice `UnknownArtifact` makes (`ApprovalArtifact.tsx:408-438`).
    // The populated fixture below is where the table itself flips.
    expect(screen.queryByTestId('approval-artifact-test-report')).toBeNull();
    expect(container.querySelectorAll('tbody tr')).toHaveLength(0);
    expect(screen.getByTestId('approval-artifact-no-checks').textContent).toContain(
      'No per-check results were recorded for this approval',
    );
  });

  /**
   * [INVARIANT — not a defect record] Guarded reads must stay guarded.
   *
   * `detailStr` coalesces with `?? {}`, so an absent `details` cannot throw.
   * This must remain true after 6.4, which is why it is labelled an invariant:
   * 6.4 widens what is rendered and must not introduce an unguarded
   * `details.test_result.checks` dereference on the way.
   */
  it('does not throw when details is absent', () => {
    expect(() => render(<ApprovalArtifact approval={makeCodeGenApproval()} />)).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// `details` present but null
// ---------------------------------------------------------------------------

describe('ApprovalArtifact — code_generation, details PRESENT-BUT-NULL (characterization)', () => {
  /**
   * [INVARIANT — not a defect record] `null` behaves exactly as absent.
   *
   * `Approval.details` is typed `Record<string, unknown> | undefined`
   * (`types.ts:138`), so `null` is off-type at compile time but reachable on the
   * wire: `approval_data` is a DynamoDB attribute and a JSON `null` survives any
   * parse that does not check. The cast is at the fixture site rather than in the
   * type, because `LGR-99050e6-5` (template / ratified) rejected narrowing — or
   * widening — a frontend type to accommodate a reader.
   *
   * This is an invariant rather than a defect record because `?? {}` collapsing
   * `null` to `{}` is correct behaviour that 6.4 must preserve. The *output* it
   * collapses to is the defect, and the preceding describe block records that.
   */
  it('collapses null details to the same output as absent details, and does not throw', () => {
    const nullDetails = makeCodeGenApproval({
      details: null as unknown as Record<string, unknown>,
    });

    expect(() => render(<ApprovalArtifact approval={nullDetails} />)).not.toThrow();
    // The invariant is the `?? {}` collapse, and it holds: `null` still produces
    // exactly the absent-`details` output. Only the note's copy moved with 6.4.
    expect(screen.getByTestId('approval-artifact-empty').textContent).toBe(
      'The generated code is not available for preview, so nothing here shows the code itself.',
    );
  });

  /**
   * [DEFECT RECORD — FLIPPED by task 6.4] Name kept verbatim; the reversal is
   * recorded here (`LGR-5906ec6-2`, `LGR-60c4985-6`, both template / ratified).
   *
   * Same set as the `details`-absent case, and it stays a separate test for the
   * same reason it was written as one: a 6.4 implementation that special-cased
   * only `undefined` and left `null` falling through to the old placeholder would
   * fail here rather than shipping. The banner assertion is what proves `null`
   * reaches the new artifact, not the old branch.
   */
  it('renders no banner and no evidence for null details', () => {
    const { container } = render(
      <ApprovalArtifact
        approval={makeCodeGenApproval({ details: null as unknown as Record<string, unknown> })}
      />,
    );

    // FLIPPED — the banner renders for `null` details too.
    expect(container.querySelectorAll('[data-analytics-alert]')).toHaveLength(1);
    // Not flipped: `null` carries no `test_result`, so there is nothing to table.
    // The stated absence is asserted instead, as in the absent-`details` case.
    expect(screen.queryByTestId('approval-artifact-test-report')).toBeNull();
    expect(screen.getByTestId('approval-artifact-no-checks')).toBeInTheDocument();
    // FLIPPED — the consequence statement is on screen.
    expect(container.textContent ?? '').toMatch(/approv/i);
  });
});

// ---------------------------------------------------------------------------
// `details` populated with the post-6.2 producer payload — the R3.6 gap
// ---------------------------------------------------------------------------

describe('ApprovalArtifact — code_generation with the post-6.2 payload (characterization)', () => {
  /**
   * [DEFECT RECORD — the central one — FLIPPED by task 6.4] Name kept verbatim,
   * reversal recorded here (`LGR-5906ec6-2`, `LGR-60c4985-6`).
   *
   * Requirement 3.6's consumer half is now met: the producer's
   * `test_result.checks` reaches the screen beside the code, in a table sharing
   * `TestFailureArtifact`'s column definitions and status vocabulary
   * (`CHECK_COLUMN_DEFINITIONS`) but none of its copy. R5's user story — "see the
   * test results alongside the code I am approving" — is what this test now
   * asserts instead of forbidding.
   *
   * The record of what was wrong:
   *
   * This is the test that states requirement 3.6's gap exactly. Task 6.2 made
   * `get_approval_data` read the paths the producers actually write, so the
   * payload now carries `code_version_id` AND the structured `test_result` with
   * its `checks` list. `detailStr` finds the code version id, so the real
   * `CodeViewer` renders — that half works, and 6.2 is why.
   *
   * The checks do not render. `ApprovalArtifact.tsx:443-458` reads
   * `code_version_id` and `previous_code_version_id` and nothing else; the
   * per-check table lives only inside `TestFailureArtifact` (`:307-383`), which
   * is reachable only from the `type === 'test_failure'` branch at `:479-485`.
   *
   * So requirement 3.6's producer half is satisfied and its consumer half is
   * not: the evidence is present in the payload, one field away from the code
   * the reviewer is judging, and no surface shows it. That is precisely what
   * requirement 5's user story asks for — "see the test results alongside the
   * code I am approving" — and design §4.5 records that no acceptance criterion
   * covered it before this spec.
   */
  it('renders the code but NOT the test evidence that sits beside it in the payload', () => {
    const { container } = render(
      <ApprovalArtifact approval={makeCodeGenApproval({ details: POST_6_2_DETAILS })} />,
    );

    // [INVARIANT] The code renders — task 6.2's outcome, and 6.4 keeps it.
    expect(screen.getByTestId('approval-artifact-code')).toBeInTheDocument();
    expect(screen.getByTestId('code-content').textContent).toContain('withColumn("total"');

    // FLIPPED — every check name and every check detail the producer wrote is now
    // on screen beside the code.
    const text = container.textContent ?? '';
    for (const evidence of CHECK_EVIDENCE_STRINGS) {
      expect(text).toContain(evidence);
    }

    // FLIPPED — the table, one row per produced check, and the banner.
    expect(screen.getByTestId('approval-artifact-test-report')).toBeInTheDocument();
    expect(container.querySelectorAll('tbody tr')).toHaveLength(PRODUCER_TEST_RESULT.checks.length);
    expect(container.querySelectorAll('[data-analytics-alert]')).toHaveLength(1);

    // The banner's header carries the state in TEXT, not in the alert colour
    // (`LGR-99050e6-1`, template / ratified, WCAG 1.4.1). Both checks passed and
    // both were verified, so the state is exactly that and nothing stronger — the
    // copy does not claim the code is correct.
    expect(text).toContain('All 2 checks passed');
  });

  /**
   * [DEFECT RECORD — FLIPPED by task 6.4] Name kept verbatim, reversal recorded
   * here (`LGR-5906ec6-2`, `LGR-60c4985-6`).
   *
   * Both obligations this test named are now met: the not-verified row renders,
   * and `NOT_VERIFIED_LABELS` (`ApprovalArtifact.tsx:209-212`, now three entries)
   * has a `no_target_schema` label, so the row does not reach an approver as the
   * bare `NOT_VERIFIED_LABEL_FALLBACK` "not verified". The assertion below is on
   * the specific label text for that reason — asserting only `/not verified/i`
   * would pass on the fallback and leave the second obligation unproven.
   *
   * The record of what was wrong:
   *
   * A Not_Verified_Check is the one row on a passing run that a reviewer most
   * needs the label for: `schema_compliance` reporting `passed: false` with
   * `verified: false` means the check never ran, not that it failed
   * (`test_code:515-538`, partitioned out before the `all()` at `:571-572`).
   *
   * Today no row renders at all, so the distinction cannot be made. When 6.4
   * renders the list it must also add a `NOT_VERIFIED_LABELS` entry for
   * `no_target_schema` (`ApprovalArtifact.tsx:209-212`), or the row reads as the
   * bare `NOT_VERIFIED_LABEL_FALLBACK` "not verified" (`:215`, applied `:263`) in
   * front of a human about to approve. This test names both obligations.
   */
  it('renders no not-verified row and no no_target_schema label for a passing run', () => {
    const { container } = render(
      <ApprovalArtifact
        approval={makeCodeGenApproval({
          details: { ...POST_6_2_DETAILS, test_result: PRODUCER_TEST_RESULT_WITH_NOT_VERIFIED },
        })}
      />,
    );

    const text = container.textContent ?? '';

    // FLIPPED — the not-verified row renders, name and produced detail both.
    expect(text).toContain('schema_compliance');
    expect(text).toContain(NOT_VERIFIED_DETAIL);

    // FLIPPED — and to the SPECIFIC label, not the bare fallback. "the check did
    // not run" is the part an approver needs: `passed: false` on this entry means
    // the check never ran, so there is nothing in the code to fix and nothing to
    // be reassured by either (`test_code:515-538`, partitioned out before the
    // `all()` at `:571-572`).
    expect(text).toContain('not verified — no target schema to compare against (the check did not run)');

    // The banner counts it as neither a pass nor a failure — three states, not two.
    expect(text).toContain('2 of 3 checks passed, 1 not verified');
  });

  /**
   * [FORWARD GUARD for task 6.4 — deliberately not a defect record]
   *
   * Stated as a guard rather than a characterization so it is not mistaken for
   * something 6.4 should flip: this assertion holds today and MUST STILL HOLD
   * after 6.4.
   *
   * Design §4.5 settles that `TestFailureArtifact`'s `notVerifiedOnly` banner
   * (condition at `:318`, Alert at `:322-330`) is not reachable on a
   * `code_generation` approval — the banner is inside `TestFailureArtifact`, which
   * `ApprovalArtifact` enters only at `:479-485` under `type === 'test_failure'`.
   * Task 6.4 must not make it reachable: for a `code_generation` approval the
   * checks list is evidence *in favour*, so `Code testing failed` and the
   * pandas-proxy copy would be a genuine misattribution created by the fix rather
   * than discovered by it.
   *
   * The strings below are quoted from that Alert's header and body (`:324`,
   * `:328`), and each was verified to be genuinely reachable on the `test_failure`
   * path before being asserted absent here — an unreachable string would make this
   * guard vacuous. If 6.4 reuses the banner instead of writing its own copy, this
   * fails.
   */
  it('never shows TestFailureArtifact copy on a code_generation approval', () => {
    const { container } = render(
      <ApprovalArtifact approval={makeCodeGenApproval({ details: POST_6_2_DETAILS })} />,
    );

    const text = container.textContent ?? '';
    expect(text).not.toContain('Code testing failed');
    expect(text).not.toContain('Code testing did not verify this transform');
    expect(text).not.toContain('pandas proxy');
    expect(screen.queryByTestId('approval-artifact-test-failure')).toBeNull();
    expect(screen.queryByTestId('approval-artifact-test-failure-alert')).toBeNull();
  });
});
