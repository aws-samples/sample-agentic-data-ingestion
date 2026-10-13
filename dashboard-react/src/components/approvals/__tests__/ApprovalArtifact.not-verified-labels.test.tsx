// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Every `not_verified_reason` a producer writes has a Status-column label — on
 * BOTH surfaces that render a check list.
 *
 * Validates: Requirements 2.20, 2.23 (p32), 3.6 (p34), R4 (p40), 2.1 (p44)
 *
 * ---------------------------------------------------------------------------
 * WHY THIS FILE EXISTS
 * ---------------------------------------------------------------------------
 *
 * A Not_Verified_Check reports `passed: false` because a boolean cannot express
 * three states (`is_not_verified_check`, `test_code/lambda_function.py:215-223`).
 * The Status column is therefore the only place a reviewer learns the check
 * NEVER RAN rather than FAILED, and per `LGR-99050e6-1` (template / ratified) it
 * has to carry that in TEXT — the indicator colour alone fails WCAG 1.4.1, and
 * `stopped` renders as an unremarkable neutral row.
 *
 * `NOT_VERIFIED_LABELS` covered three of the five reasons the producers emit.
 * The two p33 harness reasons — `test_frame_truncated` and `no_source_schema`,
 * written at `code_interpreter_runner.py:77`/`:81` and carried onto the check at
 * `test_code/lambda_function.py:484` — fell to `NOT_VERIFIED_LABEL_FALLBACK` and
 * reached a reviewer as a bare "not verified". Both are reachable on the
 * `code_generation` path that p34 task 8.1 made the default outcome of every
 * clean run, and both were already reachable on `test_failure`.
 *
 * p40 R4 adds a SIXTH reason, `scalar_literal_receiver` — the converter's
 * receiver-shape fence declining to apply a Series-only pandas member to a
 * constant (`pyspark_to_pandas._refuse_scalar`, carried onto the check at
 * `test_code/lambda_function.py:541-552`). It is a THIRD PROXY reason, so the one
 * thing that could go wrong here is it reading as either of its two neighbours:
 * `permanently_inexpressible` says pandas can never express the construct, and
 * `not_yet_expressible` says this converter has a gap and that is a defect.
 * Neither is true of it — pandas expresses this fine on a column, and the
 * converter is working as designed — so its label is asserted DISTINCT from both,
 * not merely present.
 *
 * p44 R2.1 adds `source_read_failed` — the FIFTH harness reason, and the first
 * about the run's own DATA rather than its schema, its declared types or the
 * account. A Verification_Session was created and billed and the transform's
 * input could not be read from the pipeline's own source object, so the harness
 * proved nothing (`executed` false, R2.2). All four of its harness neighbours
 * point somewhere else: the two width reasons say the schema was too wide, which
 * is false here; `session_capacity_exhausted` says retry later, which is the wrong
 * action; and `test_frame_unbuildable` says a declared TYPE could not be
 * represented. So its label is asserted DISTINCT, not merely present — design
 * §4.4 spells out why reusing `test_frame_unbuildable` was rejected.
 *
 * The assertions below are on the SPECIFIC label text, never on `/not verified/i`
 * — that loose pattern passes on the fallback and would prove nothing, which is
 * the mistake `ApprovalArtifact.code-generation.test.tsx` explicitly called out.
 * Every label transcribed below begins "not verified", so a loose match is
 * worthless here by construction, not merely weak.
 *
 * ---------------------------------------------------------------------------
 * WHY BOTH PATHS
 * ---------------------------------------------------------------------------
 *
 * Task 6.4 extracted `CHECK_COLUMN_DEFINITIONS` to module scope, so
 * `TestFailureArtifact` and `CodeGenerationArtifact` share one Status column. A
 * label added to the map therefore lands on both, which is the point — a passing
 * run's rendering of a Not_Verified_Check must not drift from a failing run's.
 * Both are asserted here rather than assumed from the shared reference, and the
 * `code_generation` case re-asserts task 6.3's forward guard: none of
 * `TestFailureArtifact`'s copy may follow the shared columns onto that path.
 *
 * Fixtures are transcribed from the PRODUCER (`aggregate_check_results`,
 * `test_code/lambda_function.py:447-486` for the harness branch), not from the
 * reader — `LGR-28b4d75-5` (template / ratified): a fixture taken from the reader
 * has the reader as its own oracle.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ApprovalArtifact from '../ApprovalArtifact';
import type { Approval } from '../../../types';

const GENERATED_CODE = 'def transform(df):\n    return df.withColumn("new", lit(1))';

// `ApprovalArtifact` imports `useCodeVersion` (via CodeViewer) and
// `useBedrockModels` (via MappingViewer) at module scope, so both must exist on
// the mock even for a branch that calls neither. Same convention as
// `ApprovalArtifact.test.tsx:27-48`.
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
// The labels under test, verbatim
// ---------------------------------------------------------------------------
//
// Transcribed, deliberately — NOT imported from `ApprovalArtifact`'s
// `NOT_VERIFIED_LABELS`. The label map is the SUBJECT here, and `LGR-28b4d75-5`
// (template / ratified) is exactly about that: a test whose expectation is read
// from the thing under test has the subject as its own oracle, so an edit to the
// map could never fail it. The same record is why the FIXTURES below are taken
// from the producer instead. Deriving is right when the other side of a contract
// owns the string (p40 task 11.1 parses this map from Python for that reason);
// transcribing is right when this file is asserting the map's own content.

const LABEL_FRAME_TRUNCATED =
  'not verified — source schema wider than the test harness frame (the transform never ran)';
const LABEL_NO_SOURCE_SCHEMA =
  'not verified — no source schema to build a test frame from (the transform never ran)';
const LABEL_NO_TARGET_SCHEMA =
  'not verified — no target schema to compare against (the check did not run)';
const LABEL_PERMANENT = 'not verified — no pandas equivalent (nothing to fix)';
const LABEL_NOT_YET = 'not verified — converter gap (defect in the checker)';
// p40 R4 — the third proxy reason's label (`ApprovalArtifact.tsx:286-287`).
const LABEL_SCALAR_LITERAL =
  'not verified — a column operation applied to a constant; the proxy declined (nothing to fix in the code)';
// p42 R8.1 — the third HARNESS reason's label. The only not-verified label whose
// copy names an ACTION (retry later), because it is the only one about account
// capacity rather than about the code or the harness's own limits.
const LABEL_SESSION_CAPACITY =
  'not verified — no Spark session available (account at its limit; nothing to fix, retry later)';
// p44 R2.1 — the FIFTH harness reason's label (`ApprovalArtifact.tsx:313-315`).
// Written with escaped apostrophes rather than in double quotes so this line is
// character-for-character the map entry it transcribes, which is the only way a
// reviewer can check a transcription by eye.
const LABEL_SOURCE_READ_FAILED =
  'not verified — the run\'s source object could not be read (nothing to fix in the code; check the object and the session\'s read access)';
const LABEL_FALLBACK = 'not verified';

// ---------------------------------------------------------------------------
// Producer fixtures
// ---------------------------------------------------------------------------

/**
 * A harness refusal as `aggregate_check_results` writes it
 * (`test_code/lambda_function.py:474-486`): the `not_verified_by_harness` name,
 * `passed: false`, the `verified: false` discriminator, the reason, and the
 * `detail` that states the cause in full.
 */
function harnessCheck(reason: string, detail: string, name = 'not_verified_by_harness') {
  return { name, passed: false, detail, verified: false, not_verified_reason: reason };
}

/**
 * A PROXY refusal as `aggregate_check_results` writes it — same five keys, but
 * the `not_verified_by_proxy` name (`test_code/lambda_function.py:541-552`).
 * Separate helper from {@link harnessCheck} so the check name a fixture claims
 * is the one its producer actually writes; the row-addressing suffix is passed
 * explicitly where several rows are rendered at once.
 */
function proxyCheck(reason: string, detail: string, name = 'not_verified_by_proxy') {
  return { name, passed: false, detail, verified: false, not_verified_reason: reason };
}

const FRAME_TRUNCATED_DETAIL =
  'NOT VERIFIED by the test harness: the source schema declares 900 field(s) against a test-frame ' +
  'width ceiling of 512, so the transform was never executed. Reason (test_frame_truncated): the ' +
  'source schema is wider than the test frame can carry, so a frame holding every column the ' +
  'transform references could not be built — a limitation of the test harness, not a defect in the ' +
  'generated PySpark. No schema-compliance score is reported for this run.';

const NO_SOURCE_SCHEMA_DETAIL =
  'NOT VERIFIED by the test harness: the source schema declares 0 field(s) against a test-frame ' +
  'width ceiling of 512, so the transform was never executed. Reason (no_source_schema): no source ' +
  'schema was available for this run, so the only frame the harness could build would not carry the ' +
  'columns the transform references — a limitation of the test harness, not a defect in the ' +
  'generated PySpark. No schema-compliance score is reported for this run.';

/**
 * p42 R8.1's refusal, transcribed from the harness branch's capacity arm. Note
 * what is NOT in it: no field count and no width ceiling. A capacity refusal has
 * neither, and the producer omits `not_verified_field_count` /
 * `not_verified_frame_ceiling` rather than sending zeroes, so the branch chooses
 * this opening clause instead of the width one.
 */
const SESSION_CAPACITY_DETAIL =
  'NOT VERIFIED by the test harness: no Spark session could be obtained to run the transform in, ' +
  'so the transform was never executed. Reason (session_capacity_exhausted): the account was ' +
  'already at its limit of concurrent Glue interactive sessions, so the session this verification ' +
  'needed could not be created — a capacity limit on the account, not a defect in the generated ' +
  'PySpark. Nothing about the transform was observed; re-running the verification later is ' +
  'expected to succeed. No schema-compliance score is reported for this run.';

/**
 * p44 R2.1's refusal, transcribed from the harness branch's fifth arm
 * (`test_code/lambda_function.py:1505-1570`, wrapped by the shared `detail` at
 * `:1591-1600`). Like the capacity refusal above it carries NO field count and no
 * width ceiling — the schema is present and inside the ceiling here, so the
 * producer omits `not_verified_field_count` / `not_verified_frame_ceiling` and
 * the branch chooses its own opening clause rather than the width one.
 *
 * What it names is the OBJECT and the READ. Not the schema, and not the stage
 * label: the producer's stage for this statement is
 * `STAGE_FRAME_BUILD = "test data generation"`, so a borrowed statement-failure
 * sentence would say "test data generation failed" about a run that was reading a
 * real object — the misattribution R2.1 exists to remove.
 */
const SOURCE_READ_FAILED_DETAIL =
  "NOT VERIFIED by the test harness: the run's own source object could not be read, so the transform " +
  "was never executed. Reason (source_read_failed): the transform's input was to be read from the " +
  "pipeline's own source object and that read did not succeed, so nothing about the transform was " +
  'observed — there is nothing in the generated PySpark to fix. Check the object and the verification ' +
  "session's read access to it; the object and the reported reason are named in the error detail " +
  'above. No schema-compliance score is reported for this run.';

/**
 * p40 R4's refusal, transcribed from the proxy branch's third arm
 * (`test_code/lambda_function.py:541-552`, wrapped by the shared `detail` at
 * `:544-548`): the construct that owns the Series-only member, the line, and the
 * reason text that says the generated PySpark is valid.
 */
const SCALAR_LITERAL_DETAIL =
  "NOT VERIFIED by the pandas proxy: construct 'cast' on line 12 was not converted, so the " +
  'transform never ran. Reason (scalar_literal_receiver): the transform applies a pandas ' +
  'operation that only exists on a column to a constant value. The proxy refuses rather than ' +
  "emit pandas that raises, or pandas that silently disagrees with Spark about the column's " +
  'values — the generated PySpark is valid and there is nothing in it to fix.';

const PASSING_CHECK = {
  name: 'ai_review_verdict',
  passed: true,
  detail: 'Reviewer verdict: PASS; effective verdict: PASS (no issues reported)',
};

function makeApproval(approvalType: string, checks: Array<Record<string, unknown>>): Approval {
  return {
    approval_id: 'appr-nv-1',
    pipeline_key: 'pipe-1',
    run_id: 'run-1',
    approval_type: approvalType,
    status: 'pending',
    created_at: '2026-08-06T13:38:05Z',
    details: {
      code_version_id: 'code-v7',
      test_result: {
        status: 'passed',
        checks,
        report_s3_uri: 's3://test-bucket/tests/exec-9/test_report.md',
      },
    },
  };
}

/** The Status cell of the row whose Check cell names `name`. */
function statusLabelFor(name: string): string {
  const rows = screen.getByTestId('approval-artifact-test-report').querySelectorAll('tbody tr');
  const row = Array.from(rows).find((r) => (r.querySelectorAll('td')[0]?.textContent ?? '') === name);
  expect(row, `no row rendered for check ${name}`).toBeTruthy();
  return row!.querySelectorAll('td')[1]?.textContent ?? '';
}

// ---------------------------------------------------------------------------
// The two harness reasons, on the test_failure path
// ---------------------------------------------------------------------------

describe('ApprovalArtifact — harness not-verified labels on test_failure', () => {
  it('names the frame-width limit in the Status column instead of the bare fallback', () => {
    render(
      <ApprovalArtifact
        approval={makeApproval('test_failure', [
          PASSING_CHECK,
          harnessCheck('test_frame_truncated', FRAME_TRUNCATED_DETAIL),
        ])}
      />,
    );

    // The specific text, not `/not verified/i`: the loose pattern matches the
    // fallback and so proves nothing about the label existing.
    expect(statusLabelFor('not_verified_by_harness')).toBe(LABEL_FRAME_TRUNCATED);
    expect(statusLabelFor('not_verified_by_harness')).not.toBe(LABEL_FALLBACK);
    // A check that DID verify something is untouched by the new entry.
    expect(statusLabelFor('ai_review_verdict')).toBe('passed');
    // The producer's full statement of the cause still reaches the Details cell —
    // the label is the summary, not a replacement for it.
    expect(screen.getByTestId('approval-artifact-test-report').textContent).toContain(
      'a limitation of the test harness, not a defect in the generated PySpark',
    );
  });

  it('names the absent source schema in the Status column instead of the bare fallback', () => {
    render(
      <ApprovalArtifact
        approval={makeApproval('test_failure', [
          harnessCheck('no_source_schema', NO_SOURCE_SCHEMA_DETAIL),
        ])}
      />,
    );

    expect(statusLabelFor('not_verified_by_harness')).toBe(LABEL_NO_SOURCE_SCHEMA);
    expect(statusLabelFor('not_verified_by_harness')).not.toBe(LABEL_FALLBACK);
  });

  // p44 task 4.3 — the FIFTH harness reason. Its own case rather than only a row
  // in the enumeration below, because the enumeration's failure message is an
  // eight-key object diff, and because the two comparisons that matter for this
  // reason are worth naming: it is not either width reason, and it is not the
  // capacity one.
  it('names the unreadable source object in the Status column instead of the bare fallback', () => {
    render(
      <ApprovalArtifact
        approval={makeApproval('test_failure', [
          PASSING_CHECK,
          harnessCheck('source_read_failed', SOURCE_READ_FAILED_DETAIL),
        ])}
      />,
    );

    // The exact text. `/not verified/i` matches the fallback and all seven other
    // labels, so it cannot tell "this reason is labelled" from "this reason fell
    // through".
    expect(statusLabelFor('not_verified_by_harness')).toBe(LABEL_SOURCE_READ_FAILED);
    expect(statusLabelFor('not_verified_by_harness')).not.toBe(LABEL_FALLBACK);
    // And distinct from the three reasons written by the same harness branch. The
    // two width reasons would claim the schema was too wide, which is false here —
    // the schema is present and inside the ceiling. Capacity would say "retry
    // later", which is the wrong action: the session WAS created and billed, and
    // what to check is the object and the session's read access.
    expect(statusLabelFor('not_verified_by_harness')).not.toBe(LABEL_FRAME_TRUNCATED);
    expect(statusLabelFor('not_verified_by_harness')).not.toBe(LABEL_NO_SOURCE_SCHEMA);
    expect(statusLabelFor('not_verified_by_harness')).not.toBe(LABEL_SESSION_CAPACITY);
    // A check that DID verify something is untouched by the new entry.
    expect(statusLabelFor('ai_review_verdict')).toBe('passed');
    // The producer's full statement of the cause still reaches the Details cell —
    // the label is the summary, not a replacement for it. This clause is the one
    // that tells a reviewer where to look, so it is the one asserted.
    expect(screen.getByTestId('approval-artifact-test-report').textContent).toContain(
      "Check the object and the verification session's read access to it",
    );
  });

  // AMENDED IN PLACE by p40 task 10.2, not rewritten: five producer reasons
  // became six when R4 added `scalar_literal_receiver`, so the count in the name,
  // the enumerated rows and the `Set` size below all move together. Amending
  // rather than deleting is `LGR-60c4985-6` / `LGR-b2f79ef-6` (both template /
  // ratified) — the reason for each moved number is recorded at the number, so
  // the change reads as a widening rather than as a regression. Amended again the
  // same way by p44 task 4.3 for `source_read_failed`, 7 → 8.
  //
  // "ENUMERATED", not "all": the count is the number of rows below, and it is NOT
  // a claim that the map has that many entries. `test_frame_unbuildable` (p42
  // R7.3, `ApprovalArtifact.tsx:302-304`) has no row here and never did, so the
  // map carries nine entries against these eight. That gap is stated rather than
  // papered over, and it has one consequence worth knowing: design §4.4's central
  // argument is that `source_read_failed` must not read as
  // `test_frame_unbuildable`, and the `Set` size below cannot check that pair
  // while one half of it is absent. Adding the missing row is p42's work, not
  // this task's.
  it('keeps all eight enumerated producer reasons distinguishable on screen, and none of them the fallback', () => {
    // One row per reason the producers emit, plus a reason no producer writes.
    // The names are suffixed so each row is addressable — the two harness reasons
    // share one check name in production (`not_verified_by_harness`), as the
    // THREE proxy reasons share `not_verified_by_proxy` (p40 R4 made it three).
    // Same technique as
    // `ApprovalArtifact.test.tsx:323-352`.
    render(
      <ApprovalArtifact
        approval={makeApproval('test_failure', [
          harnessCheck('permanently_inexpressible', 'proxy refusal', 'nv:permanent'),
          harnessCheck('not_yet_expressible', 'proxy refusal', 'nv:not_yet'),
          // p40 R4's reason, the third the proxy branch writes.
          proxyCheck('scalar_literal_receiver', SCALAR_LITERAL_DETAIL, 'nv:scalar_literal'),
          harnessCheck('test_frame_truncated', FRAME_TRUNCATED_DETAIL, 'nv:truncated'),
          harnessCheck('no_source_schema', NO_SOURCE_SCHEMA_DETAIL, 'nv:no_source'),
          harnessCheck('no_target_schema', 'no target schema', 'nv:no_target'),
          // p42 R8.1's reason, the third the harness branch writes.
          harnessCheck('session_capacity_exhausted', SESSION_CAPACITY_DETAIL, 'nv:capacity'),
          // p44 R2.1's reason, the fifth the harness branch writes.
          harnessCheck('source_read_failed', SOURCE_READ_FAILED_DETAIL, 'nv:source_read'),
          harnessCheck('some_reason_added_later', 'a reason this file does not know', 'nv:unknown'),
        ])}
      />,
    );

    const labels = {
      'nv:permanent': statusLabelFor('nv:permanent'),
      'nv:not_yet': statusLabelFor('nv:not_yet'),
      'nv:scalar_literal': statusLabelFor('nv:scalar_literal'),
      'nv:truncated': statusLabelFor('nv:truncated'),
      'nv:no_source': statusLabelFor('nv:no_source'),
      'nv:no_target': statusLabelFor('nv:no_target'),
      'nv:capacity': statusLabelFor('nv:capacity'),
      'nv:source_read': statusLabelFor('nv:source_read'),
    };

    expect(labels).toEqual({
      'nv:permanent': LABEL_PERMANENT,
      'nv:not_yet': LABEL_NOT_YET,
      'nv:scalar_literal': LABEL_SCALAR_LITERAL,
      'nv:truncated': LABEL_FRAME_TRUNCATED,
      'nv:no_source': LABEL_NO_SOURCE_SCHEMA,
      'nv:no_target': LABEL_NO_TARGET_SCHEMA,
      'nv:capacity': LABEL_SESSION_CAPACITY,
      'nv:source_read': LABEL_SOURCE_READ_FAILED,
    });
    // Eight reasons, eight distinct labels — a shared label would re-create the
    // ambiguity each classification exists to remove. Was 5; moved to 6 by p40
    // task 10.2 when R4 added `scalar_literal_receiver`, which is a third PROXY
    // reason and so the one most at risk of being given a neighbour's copy; moved
    // to 7 by p42 task 3.6, whose `session_capacity_exhausted` is a third HARNESS
    // reason and carries the same risk against the two width reasons; moved to 8
    // by p44 task 4.3, whose `source_read_failed` is a FIFTH harness reason and at
    // risk against all three of those — the two width reasons make a claim about
    // the schema that is false here, and capacity names an action ("retry later")
    // that is wrong here.
    //
    // The assertion is moved, never relaxed to a subset or a `>=`
    // (`LGR-60c4985-6`, template / ratified): the exact size is the whole
    // mechanism by which a new reason reusing a neighbour's copy fails this test,
    // and a `>=` would pass on exactly the defect it is here to catch.
    expect(new Set(Object.values(labels)).size).toBe(8);
    for (const label of Object.values(labels)) {
      expect(label).not.toBe(LABEL_FALLBACK);
    }

    // The fallback is still in place for a reason this file does not recognise.
    // It is residual, not removed: an unknown reason must still render its state
    // in text rather than leaving the Status cell empty.
    expect(statusLabelFor('nv:unknown')).toBe(LABEL_FALLBACK);
  });
});

// ---------------------------------------------------------------------------
// p40 R4 — the third PROXY reason, on the test_failure path
// ---------------------------------------------------------------------------

describe('ApprovalArtifact — the scalar-literal proxy not-verified label on test_failure', () => {
  it('names the constant receiver in the Status column instead of the bare fallback', () => {
    render(
      <ApprovalArtifact
        approval={makeApproval('test_failure', [
          PASSING_CHECK,
          proxyCheck('scalar_literal_receiver', SCALAR_LITERAL_DETAIL),
        ])}
      />,
    );

    // The exact text. `/not verified/i` would pass on the fallback AND on all
    // five other labels, so it cannot tell "this reason is labelled" from "this
    // reason fell through".
    expect(statusLabelFor('not_verified_by_proxy')).toBe(LABEL_SCALAR_LITERAL);
    expect(statusLabelFor('not_verified_by_proxy')).not.toBe(LABEL_FALLBACK);
    // And distinct from the two reasons it is most likely to be mistaken for —
    // the other two refusals written by the same proxy branch.
    expect(statusLabelFor('not_verified_by_proxy')).not.toBe(LABEL_PERMANENT);
    expect(statusLabelFor('not_verified_by_proxy')).not.toBe(LABEL_NOT_YET);
    // A check that DID verify something is untouched by the new entry.
    expect(statusLabelFor('ai_review_verdict')).toBe('passed');
    // The producer's full statement of the cause still reaches the Details cell —
    // the label is the summary, not a replacement for it.
    expect(screen.getByTestId('approval-artifact-test-report').textContent).toContain(
      'the generated PySpark is valid and there is nothing in it to fix',
    );
  });
});

// ---------------------------------------------------------------------------
// The same two reasons, on the code_generation path
// ---------------------------------------------------------------------------

describe('ApprovalArtifact — harness not-verified labels on code_generation', () => {
  it('renders the same labels through the shared check columns', () => {
    render(
      <ApprovalArtifact
        approval={makeApproval('code_generation', [
          PASSING_CHECK,
          harnessCheck('test_frame_truncated', FRAME_TRUNCATED_DETAIL, 'nv:truncated'),
          harnessCheck('no_source_schema', NO_SOURCE_SCHEMA_DETAIL, 'nv:no_source'),
          // p40 R4, on this path too. Asserted rather than inferred from the
          // shared `CHECK_COLUMN_DEFINITIONS` reference: a label that landed on
          // only one of the two surfaces is the drift this file exists to catch.
          // Nothing on the `code_generation` path's OWN copy is touched here
          // (`LGR-69ca78b-4`, template / ratified) — only the shared Status column.
          proxyCheck('scalar_literal_receiver', SCALAR_LITERAL_DETAIL, 'nv:scalar_literal'),
          // p44 R2.1, on this path too, and asserted for the same reason: a label
          // that landed on only one of the two surfaces is the drift this file
          // exists to catch. Reachable here as well as on `test_failure` — a run
          // whose source could not be read has `executed` false rather than a
          // failed transform (R2.2), so it does not fail the run and reaches a
          // reviewer through the `code_generation` artifact.
          harnessCheck('source_read_failed', SOURCE_READ_FAILED_DETAIL, 'nv:source_read'),
        ])}
      />,
    );

    expect(screen.getByTestId('approval-artifact-code-generation')).toBeInTheDocument();
    expect(statusLabelFor('nv:truncated')).toBe(LABEL_FRAME_TRUNCATED);
    expect(statusLabelFor('nv:no_source')).toBe(LABEL_NO_SOURCE_SCHEMA);
    expect(statusLabelFor('nv:scalar_literal')).toBe(LABEL_SCALAR_LITERAL);
    expect(statusLabelFor('nv:scalar_literal')).not.toBe(LABEL_FALLBACK);
    expect(statusLabelFor('nv:source_read')).toBe(LABEL_SOURCE_READ_FAILED);
    expect(statusLabelFor('nv:source_read')).not.toBe(LABEL_FALLBACK);
  });

  it('counts a harness refusal as neither passed nor failed in the banner', () => {
    const { container } = render(
      <ApprovalArtifact
        approval={makeApproval('code_generation', [
          PASSING_CHECK,
          harnessCheck('no_source_schema', NO_SOURCE_SCHEMA_DETAIL),
        ])}
      />,
    );

    // Three states, not two: `passed: false` on this row does not make it a
    // failure, and the banner's header carries that in text.
    expect(container.textContent ?? '').toContain('1 of 2 checks passed, 1 not verified');
  });

  it('still shows none of TestFailureArtifact copy (task 6.3 forward guard)', () => {
    const { container } = render(
      <ApprovalArtifact
        approval={makeApproval('code_generation', [
          harnessCheck('test_frame_truncated', FRAME_TRUNCATED_DETAIL),
        ])}
      />,
    );

    // The shared columns must not bring the failure copy with them. All four
    // strings task 6.3 guards, re-asserted on a not-verified fixture.
    const text = container.textContent ?? '';
    expect(text).not.toContain('Code testing failed');
    expect(text).not.toContain('Code testing did not verify this transform');
    expect(text).not.toContain('pandas proxy');
    expect(screen.queryByTestId('approval-artifact-test-failure')).toBeNull();
    expect(screen.queryByTestId('approval-artifact-test-failure-alert')).toBeNull();
  });
});
