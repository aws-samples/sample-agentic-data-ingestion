# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 5 — a refusal is reported as a non-verification (p32, Defect 5).

For any test run whose transform block raises ``ConversionError``, ``test_code``
SHALL emit a not-verified check naming the construct and line, SHALL NOT emit a
``code_interpreter_execution`` failure for it, SHALL produce a report stating
neither execution failure nor ``0 → 0 records`` nor ``PRODUCTION READY``, and —
where no other check fails — SHALL yield ``test_status != "failed"`` and
``regeneration_needed = False``.

Bug condition: ``isBugCondition_D5(X)`` where ``convert(X.transform_block)``
raises ``ConversionError``.

The assertion is ``!= "failed"`` rather than ``== "passed"`` on purpose:
bugfix.md 2.21 leaves the choice of a third status open, and the requirement is
only that a refusal must not be the thing that fails the gate.

Expectations here derive from bugfix.md 2.20/2.21/2.23, NOT from current output
— current output is the defect (``LGR-28b4d75-5``, template provenance: a test
transcribed from the producer's own output cannot detect drift, because the
output is its own oracle).

**RED ON UNFIXED CODE — the failure is task 5's deliverable. Task 6 fixes it.**

Validates: Requirements 2.20, 2.21, 2.23; V4b

p40 task 8.3 — the THIRD reason joins the input space
-----------------------------------------------------
`p40-pandas-proxy-literal-cast`'s second Fix-Checking property ("a checker
fault is not a code defect") is the same shape as this one, over a wider bug
condition: the converter's receiver-shape fence (`pyspark_to_pandas.py`'s
`_require_series`) now refuses a Series-only pandas member applied to a
scalar-literal receiver, and that refusal carries a third reason —
`scalar_literal_receiver`. **Not one assertion below was rewritten for it**;
the generator gained a third arm and the property gained three clauses the p32
version did not have (the reason travels and is distinct from both p32 reasons,
and the approval gate does not see the refusal). That is the whole of p40's
Fix-Checking property except the report-prose surface, which lives in
`test_comprehensive_report.py` because `nv_reason_text` and `next_steps` are
function-local and only observable through a rendered report.

Expected GREEN on the first run: p40 tasks 8.1 and 8.2 have landed, so a red
here is a finding about one of them, not a test to adjust.

Validates: p40 Requirements R4, R5, R9
"""

# ---------------------------------------------------------------------------
# V4b — enumeration of every consumer of ``test_status``
#
# ``all(check["passed"] ...)`` at ``test_code/lambda_function.py:277`` cannot
# express three states. design.md §5 V4b requires every reader be enumerated
# BEFORE that derivation changes, "or a not-verified run reads as passed
# somewhere nobody looked". This is that enumeration. Task 6 must check each
# entry; six of them are not in V4b's own four-item list.
#
# PRODUCER
#   aggregate_check_results, lambda_function.py:277 — the only derivation site.
#
# A. IN-PROCESS READERS (lambda/agentic_data_ingestion_test_code/lambda_function.py)
#   A1 :305,:349  analyze_test_results_for_regeneration — `if test_status ==
#                 "failed" or checks_failed > 0` appends a regeneration
#                 trigger. Partitioning the all() is NOT sufficient here:
#                 `checks_failed > 0` is a second, independent path to the
#                 same trigger, so a not-verified entry left in the failed
#                 count still sets regeneration_needed. (2.21)
#   A2 :385,:417  determine_code_approval_requirement — `if test_status ==
#                 "failed": return True` forces the gate. V4b's named reader.
#                 Note :410-412 fires FIRST on regeneration_needed, so A1
#                 also reaches the gate independently of A2.
#   A3 :894       write_agent_audit_entry(details={"test_status": ...}) —
#                 the audit trail. A third value lands in audit records.
#   A4 :941       test_result = {"status": test_status, checks, report_s3_uri}
#                 — the structured SFN contract object; copied into the state
#                 row (:1079) and the approval payload (:1027).
#   A5 :957       test_output JSON → s3://…/test_results.json. Read back by
#                 the dashboard as `s3Report.test_status` (B7).
#   A6 :996       test-results DynamoDB item `test_status`; :1013
#                 error_details (None iff "passed" — a third value produces a
#                 "Failed checks: …" string); :1016 validation_status.
#   A7 :1027      test_approval_payload → output_data["approval"], consumed by
#                 approval_handler (B2).
#   A8 :1044      create_output_data(status="success" if passed else "failed")
#                 — the artifact envelope; pinned by
#                 tests/test_output_data_schema.py::test_status_is_string
#                 [test_results] and the artifacts contract test.
#   A9 :1066      pipeline-runs STATE ROW `status` = "success"/"failed" —
#                 V4b's "state row". Binary by construction, so a third
#                 test_status collapses to "failed" here unless changed.
#                 :1079 test_status, :1081 validation_status on the same item.
#   A10 :1119     handler return payload test_status; :1122 test_result;
#                 :1125 validation_status. Read by the SFN Choice (B1).
#   A11 next_step — derived from approval_required, i.e. transitively A1+A2.
#
# B. OUT-OF-PROCESS READERS
#   B1 deploy-cdk/lib/orchestration-stack.ts:453-468 — TestResultChoice
#      branches on `$.test_code_result.test_result.status`: "passed" →
#      CheckCodeApproval, ANYTHING ELSE → ForceTestReviewApproval. **This is
#      the reader that actually gates the pipeline and it is not in V4b's
#      list.** It reads test_result.status (A4/A10), not the top-level key.
#      A third status value routes to the forced gate, so 2.21 is not
#      satisfied by test_status alone — the Choice must be considered too.
#   B2 approval_handler/lambda_function.py:263 — details["test_status"] =
#      test_result.get("status"); :272 defaults to "unknown" when absent.
#      Feeds the approval screen (Defect 1's payload).
#   B3 dashboard_api/handlers/pipelines_runs.py:326,:333 — step_info
#      ["test_status"] from approval_data then output_data (run-detail steps).
#   B4 dashboard_api/handlers/pipelines_artifacts.py:99 — artifacts response.
#   B5 dashboard_api/handlers/pipelines_crud.py:1385-1440 — schema-history run
#      enrichment (approval_data then output_data); :1308,:1330 default None.
#   B6 create_glue_pipeline/artifacts.py:319-320 — reads the STATE ROW `status`
#      (A9) and derives `test_status` + `test_passed = status == "success"`;
#      :316 "not_run", :369 "error". Then
#      job_builders.py:835 logs it, and **workers.py:160 puts it in the
#      deployed Glue job's DefaultArguments as `--test-status`** — the value
#      leaves this system entirely. Not in V4b's list.
#   B7 dashboard-react: types.ts:173,:264; TestResultsViewer.tsx:245,:267 and
#      :379-390 (isPassing at :32-35 is an ALLOWLIST — passed|pass|success|
#      succeeded — so any third value renders ❌ FAILED, i.e. the dashboard
#      risk is the inverse of V4b's worry, not a false green);
#      hooks/use-code.ts:49 (s3Report ?? test ?? validation_status ??
#      'unknown') and :85 (polls while === 'pending'); hooks/use-pipelines.ts
#      :36; pages/SchemaHistoryPage.tsx:348 and pages/CodePage.tsx:74 render
#      the raw string in a Badge. Task 6b owns the check-level reader.
#   B8 deploy-cdk/lib/data-stack.ts:117 — the test-results table's
#      `test_status-index` GSI partitions on test_status. A new value becomes
#      a new partition key. Not in V4b's list.
#
# C. NOT a reader of test_status (checked, so task 7a/7b are not redundant)
#   generate_comprehensive_test_report (:494-600) reads
#   execution_results["execution_successful"] and parsed_results — never
#   test_status. So partitioning the all() does NOT fix the report; the four
#   1.21 statements and the PRODUCTION READY verdict are a separate surface.
#   That is exactly why 7a/7b exist as their own tasks, and why the report
#   clause of this property stays red until task 7b.
# ---------------------------------------------------------------------------

import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, event, given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The converter and the runner are the REAL modules — the refusal, its
# construct/line, and _conversion_failure_results' shape are all genuinely
# computed. Only the bedrock-agentcore client is stubbed, and conversion
# raises at code_interpreter_runner.py:83-86 BEFORE any session is started,
# so no AWS call is reached on this path.
from pyspark_to_pandas import (
    ConversionError,
    PySpark2PandasConverter,
)

with patch("boto3.client", MagicMock()):
    from code_interpreter_runner import CodeInterpreterTestRunner

# lambda_function pulls in strands/AWS at import time; stub those only.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# Generators — the two reasons for a refusal must stay distinguishable (2.23)
# ---------------------------------------------------------------------------

_COLUMNS = ["state", "name", "amount", "city", "code"]
_LITERALS = ["California", "NY_full", "Other", "unknown"]

# 2.23 reason (a) — NOT YET expressible: a defect in this system.
# The generation prompt is allowed to emit every one of these shapes.
#
# These were p32 1.7-1.9 until task T10's AST converter made all three CONVERT,
# which is what 2.7-2.9 require. The property below is quantified over blocks
# the converter REFUSES, so those three stopped satisfying its bug condition
# (isBugCondition_D5) and had to be replaced by shapes that still refuse. Not
# one assertion changed — only which inputs are drawn. The three below are all
# converter gaps rather than 3.7 inexpressibility, so they still exercise the
# "something to fix" half of 2.23:
#   * a chain mixing frame operations — the same defect class as
#     `pyspark_templates/README.md`'s parenthesised fluent worked example (1.12)
#   * a `select` argument whose output column name cannot be resolved (3.6)
#   * `.alias()` with a non-literal argument
#
# p41 task 9.3 (2.7) MOVED the fourth entry here from `_PERMANENT_TEMPLATES`,
# rather than deleting it: `initcap` is `.str.title()` — single-frame and
# elementwise — so "no pandas equivalent exists" was false; it is unimplemented,
# not inexpressible. The shape is still worth generating, under the true reason.
# `LGR-60c4985-6` / `LGR-5906ec6-2` (both template / ratified) require a
# deliberately-held assertion to be amended in place with its reason at the
# assertion; a template moved between the two lists is that amendment, and a
# template deleted would not be. Nothing about the property's assertions changed
# — only which reason this input is drawn under. `initcap` still REFUSES; task
# 9.2 changed the reason only, and implementing `initcap` is out of scope.
_NOT_YET_TEMPLATES = [
    'df_transformed = {src}.withColumnRenamed("{a}", "{b}").withColumn("{a}", trim(col("{b}"))).drop("{b}")',
    'df_transformed = {src}.select(trim(col("{a}")))',
    'df_transformed = {src}.select(col("{a}").alias(alias_var))',
    'df_transformed = {src}.withColumn("{a}", initcap(col("{a}")))',
]

# 2.23 reason (b) — PERMANENTLY inexpressible in a single-frame pandas proxy:
# 3.7's list plus joins. These keep refusing after the AST rewrite and are
# nothing to fix, but they are still "not verified" rather than "failed".
#
# p41 task 9.3 (2.7): the `initcap` template that sat between `spark.sql` and
# `join` was MOVED to `_NOT_YET_TEMPLATES`, with the reason recorded there.
# `initcap` is `.str.title()` — single-frame and elementwise — so it never met
# this list's membership criterion (`code_interpreter_runner.py:136-163`, added
# by task 9.2). The three that remain do: `groupBy`/`agg` change the frame's
# shape, `spark.sql` is a Spark entry point, `join` needs a second frame.
_PERMANENT_TEMPLATES = [
    'df_transformed = {src}.groupBy("{a}").agg(count("{b}"))',
    'df_transformed = spark.sql("SELECT {a} FROM t")',
    'df_transformed = {src}.join(other_df, "{a}")',
]

# p40 R4 reason (c) — SCALAR_LITERAL_RECEIVER: the receiver-shape fence. The
# converter would otherwise have LIED rather than refused, which is why this is
# a third reason and not a re-spelling of either above (p40 bugfix.md 2.13, 3.7).
#
# Every shape here was MEASURED to refuse with `reason == scalar_literal_receiver`
# against the working tree, not inferred from the fence's rule — the four shapes
# p40's arm A converts (`lit(<const>).cast("string"|StringType())`, plus `.alias`
# and a nested string function over one) deliberately do NOT appear, because they
# succeed and so are not bug conditions for THIS property. `to_date(lit(...))`
# does appear: `pd.to_datetime` over a scalar is itself a scalar emission, so the
# `.dt` accessor lands on one and the fence fires.
_SCALAR_LITERAL_TEMPLATES = [
    'df_transformed = {src}.withColumn("{a}", lit(None).cast("boolean"))',
    'df_transformed = {src}.withColumn("{a}", lit(0).cast("int"))',
    'df_transformed = {src}.withColumn("{a}", trim(lit("{lit}")))',
    'df_transformed = {src}.withColumn("{a}", lower(lit("{lit}")))',
    'df_transformed = {src}.withColumn("{a}", upper(lit("{lit}")))',
    'df_transformed = {src}.withColumn("{a}", regexp_replace(lit("{lit}"), "x", "y"))',
    'df_transformed = {src}.withColumn("{a}", concat_ws("-", lit("{lit}"), col("{b}")))',
    'df_transformed = {src}.withColumn("{a}", to_date(lit("2020-01-01"), "yyyy-MM-dd"))',
]

#: The reason → templates map the generator samples over. Keyed on the
#: PRODUCTION constant for the third reason rather than a fourth transcription
#: of the string (`LGR-28b4d75-5`, template provenance: an expectation
#: transcribed from the producer cannot detect the two sides drifting). The two
#: p32 reasons keep their literals, which is what they were before p40.
_TEMPLATES_BY_REASON = {
    "not_yet_expressible": _NOT_YET_TEMPLATES,
    "permanently_inexpressible": _PERMANENT_TEMPLATES,
    _mod.SCALAR_LITERAL_RECEIVER_REASON: _SCALAR_LITERAL_TEMPLATES,
}

# A convertible prefix, so line_number varies and the "names the line" clause
# is tested against something other than a constant 1.
_CONVERTIBLE_PREFIX = 'df_transformed = df.withColumn("{a}", trim(col("{a}")))'


@st.composite
def refusing_transform_blocks(draw):
    """Transform blocks the pandas proxy refuses, over all THREE reasons.

    "Both 2.23 reasons" until p40 task 8.3 added `scalar_literal_receiver` —
    amended here rather than in a second generator, see the sampling comment.

    Constrained to the generation prompt's construct vocabulary rather than
    arbitrary text: the point is shapes this system really produces. The 1.10
    shape (``.withColumn`` chained with ``.select``) is deliberately EXCLUDED
    — it converts successfully today, so it is not a D5 bug condition; it is
    Property 3's business.
    """
    # p40 task 8.3: a THIRD arm. Amended in place rather than appended as a
    # second generator, because the property's assertions are identical for all
    # three reasons and a second generator would be a second place for them to
    # drift (`LGR-b2f79ef-6`, template / ratified — amend with the reason at the
    # site, never fork).
    reason = draw(st.sampled_from(sorted(_TEMPLATES_BY_REASON)))
    template = draw(st.sampled_from(_TEMPLATES_BY_REASON[reason]))
    a, b = draw(st.lists(st.sampled_from(_COLUMNS), min_size=2, max_size=2, unique=True))
    lit = draw(st.sampled_from(_LITERALS))

    n_prefix = draw(st.integers(min_value=0, max_value=2))
    lines: list[str] = []
    for _ in range(n_prefix):
        lines.append(_CONVERTIBLE_PREFIX.format(a=draw(st.sampled_from(_COLUMNS))))
        if draw(st.booleans()):
            lines.append("# mapping step")
    src = "df_transformed" if n_prefix else "df"
    lines.append(template.format(src=src, a=a, b=b, lit=lit))
    return "\n".join(lines), reason


_SOURCE_SCHEMA = {"properties": {c: {"type": "string"} for c in _COLUMNS}}
_TARGET_SCHEMA = {"properties": {c: {"type": "string"} for c in _COLUMNS}}

# Every other check passes, so a refusal is the ONLY non-passing outcome —
# that is the antecedent of 2.21's "where no other check fails".
_ALL_STATIC_PASS = {
    "verdict": "pass",
    "issues": [],
    "test_categories": {
        "Correctness": {"status": "PASSED", "passed": True},
        "Security": {"status": "PASSED", "passed": True},
        "Schema Compliance": {"status": "PASSED", "passed": True},
    },
    "recommendation": "APPROVE",
}
_REVIEW_PASS = {"verdict": "pass", "issues": [], "categories": {}}


def _refusal_execution_results(block: str) -> tuple[dict, ConversionError]:
    """Run the real refusal path and return its real structured result."""
    with pytest.raises(ConversionError) as excinfo:
        PySpark2PandasConverter.convert(block)

    with patch("boto3.client", MagicMock()):
        runner = CodeInterpreterTestRunner("us-east-1")
    assert runner.client is not None, "guard: a None client would take the _empty_results path instead"

    return runner.run_test(block, _SOURCE_SCHEMA, _TARGET_SCHEMA), excinfo.value


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(refusing_transform_blocks())
def test_a_refusal_is_reported_as_a_non_verification(case):
    """Property 5. RED on unfixed code — that failure is task 5's deliverable.

    Validates: Requirements 2.20, 2.21, 2.23
    """
    block, reason = case
    # p40 task 8.3: which arm this example drew, so `--hypothesis-show-statistics`
    # shows the third reason really is being exercised rather than assumed.
    event(f"refusal reason: {reason}")
    execution_results, exc = _refusal_execution_results(block)

    test_status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, execution_results)
    checks_run = len(checks)
    checks_passed = sum(1 for c in checks if c["passed"])
    regeneration = _mod.analyze_test_results_for_regeneration(
        test_status, checks_run, checks_passed, _REVIEW_PASS, regeneration_attempt=0
    )
    report = _mod.generate_comprehensive_test_report("static analysis report", execution_results, _ALL_STATIC_PASS)

    # 2.20 — a not-verified check exists, naming the construct AND the line.
    # Matched on a name substring rather than an exact literal: 2.21 leaves
    # the spelling to task 6, but the concept is required.
    not_verified = [c for c in checks if "not_verified" in c["name"]]
    assert not_verified, (
        f"no not-verified check emitted for a refusal ({reason}); checks were {[c['name'] for c in checks]}"
    )
    detail = " ".join(str(c.get("detail", "")) for c in not_verified)
    assert exc.construct in detail, f"not-verified check does not name the construct {exc.construct!r}"
    assert str(exc.line_number) in detail, f"not-verified check does not name line {exc.line_number}"

    # p40 R4 (task 8.3) — the reason the CONVERTER decided travels all the way
    # to the check, and the third one is distinct from BOTH p32 reasons. This is
    # the clause p40's second Fix-Checking block spells as "an entry with
    # verified = False whose reason is distinct from permanently_inexpressible
    # AND from not_yet_expressible". Asserted for all three arms, so it is a
    # statement about the reason travelling, not only about the new one existing.
    emitted_reasons = {c.get("not_verified_reason") for c in not_verified}
    assert reason in emitted_reasons, (
        f"the converter refused for {reason!r} but the check reports {emitted_reasons} — the reason "
        "did not survive _conversion_failure_results → aggregate_check_results (p40 R4)"
    )
    if reason == _mod.SCALAR_LITERAL_RECEIVER_REASON:
        assert reason not in ("permanently_inexpressible", "not_yet_expressible"), (
            "the third reason has collapsed into one of p32's two — p40 3.7 forbids blurring them"
        )

    # 2.20 — a refusal is NOT an execution failure.
    exec_failures = [c for c in checks if c["name"] == "code_interpreter_execution" and not c["passed"]]
    assert not exec_failures, f"refusal recorded as a failed code_interpreter_execution check: {exec_failures}"

    # 2.21/2.22 — the report says nothing about an execution that never happened.
    assert "❌ FAILED" not in report, "report states execution FAILED for a transform that never ran"
    assert "0 → 0 records" not in report, "report states 0 → 0 records for a transform that never ran"
    assert "Execution Error Details" not in report, "report renders an Execution Error Details block for a refusal"
    # Substring covers BOTH verdicts on purpose: 1.21's "❌ NOT PRODUCTION
    # READY" is a statement about an execution that did not happen, and
    # 1.22's "✅ PRODUCTION READY" is a claim an unverified run cannot support.
    assert "PRODUCTION READY" not in report, (
        "report reaches a (NOT) PRODUCTION READY verdict from a run that never executed"
    )

    # 2.21 — no other check fails, so a refusal alone must not fail the gate
    # or trigger regeneration. `!= "failed"`, not `== "passed"` (2.21 leaves
    # the third-status choice open).
    assert test_status != "failed", f"refusal alone set test_status to {test_status!r}"
    assert regeneration["regeneration_needed"] is False, (
        "refusal triggered regeneration — the PySpark is correct, there is nothing to regenerate"
    )

    # p40 R5 (task 8.3) — the OTHER consumer of the same derivation, which the
    # p32 property never reached. `regeneration_triggers` is a function-local
    # list, so it is only observable through the analysis above; the approval
    # gate is a real function and is called here directly.
    #
    # ASSERTED, NOT REBUILT. `determine_code_approval_requirement` returns True
    # on `test_status == "failed"` (`lambda_function.py:838`) and on a
    # max-attempts-reached analysis; a partitioned-out check (`:610-612`) reaches
    # neither, so p40 routes the new refusal onto the path that was already
    # correct rather than building a second one. The counts are derived the way
    # the HANDLER derives them — partition first, over the production
    # discriminator (`:1635-1641`) — because feeding unpartitioned counts would
    # be testing a call shape the handler never makes.
    verified_checks = [c for c in checks if not _mod.is_not_verified_check(c)]
    verified_failed = [c for c in verified_checks if not c["passed"]]
    assert verified_checks, "guard: nothing verified, so `:611`'s own guard would derive 'failed' for its own reason"
    approval_required = _mod.determine_code_approval_requirement(
        test_status=test_status,
        checks_run=len(verified_checks),
        checks_passed=len(verified_checks) - len(verified_failed),
        review_verdict=_REVIEW_PASS,
        regeneration_analysis=regeneration,
    )
    assert approval_required is False, (
        f"a refusal alone ({reason}) forced the approval gate — that is the misattribution p40 2.11/2.12 "
        "exists to remove: the reviewer would be asked to judge PySpark the checker never disputed"
    )


# ── p40 task 8.3 — the three spellings of the third reason must agree ────────
#
# WHY THIS FILE IS THE SITE. The constant exists in three places on purpose:
#
#   * `pyspark_to_pandas.REFUSAL_REASON_SCALAR_LITERAL`      — the OWNER
#   * `code_interpreter_runner.NOT_VERIFIED_REASON_SCALAR_LITERAL` — an alias
#     BOUND to the owner (so `is` holds), following that module's naming
#     convention
#   * `lambda_function.SCALAR_LITERAL_RECEIVER_REASON`       — a SECOND
#     SPELLING, deliberately, carrying a "change both or neither" comment
#
# The second spelling is not an oversight: task 8.1 measured that importing the
# producer's constant into `lambda_function` produces a DEAD ARM. 24 files under
# `tests/` stub `code_interpreter_runner` with a bare `MagicMock`, so an imported
# constant arrives as an auto-attribute mock; `reason == <MagicMock>` does not
# raise, it silently never matches, and the branch would never fire while the
# suite stayed green. A spelled string cannot fail that way.
#
# What a second spelling CAN do is drift. Nothing caught that before this test.
#
# `test_code_interpreter_runner.py` already asserts the owner ↔ alias pair
# (`TestConversionRefusalReasonPrecedence`, task 7.2), but it never loads
# `lambda_function`, so it cannot see the third spelling. THIS module already
# holds all three as REAL objects, and that was measured rather than assumed:
# `pyspark_to_pandas` is imported at the top and never stubbed;
# `code_interpreter_runner` is imported for real BEFORE the `patch.dict` above,
# and `patch.dict` restores it on exit, so `sys.modules` carries the real module
# afterwards; and `_mod.SCALAR_LITERAL_RECEIVER_REASON` is `lambda_function`'s
# own literal, which no stub can replace.
#
# The `isinstance(..., str)` guards are the anti-vacuity fence, and they are the
# point rather than decoration: a `MagicMock` is not a `str`, so if any of the
# three ever arrives mocked, this test FAILS loudly instead of comparing two
# mocks and passing — which is the exact failure mode task 8.1 measured.


def test_the_three_spellings_of_the_scalar_literal_reason_agree():
    """p40 R4 — one string, three names, asserted against the real objects.

    Validates: p40 Requirements R4
    """
    import code_interpreter_runner
    import pyspark_to_pandas

    owner = pyspark_to_pandas.REFUSAL_REASON_SCALAR_LITERAL
    alias = code_interpreter_runner.NOT_VERIFIED_REASON_SCALAR_LITERAL
    reader = _mod.SCALAR_LITERAL_RECEIVER_REASON

    # Anti-vacuity: a mock would satisfy `==` against another mock and prove
    # nothing. Nothing here may be a mock.
    for name, value in (("converter", owner), ("runner", alias), ("lambda_function", reader)):
        assert isinstance(value, str), (
            f"{name}'s reason constant is {type(value).__name__}, not str — this assertion would have "
            "compared mocks and passed vacuously, which is precisely what task 8.1 measured and "
            "avoided by spelling the reader's constant out"
        )

    # The alias is BOUND, so identity holds and a rename on the owning side
    # cannot leave a stale copy behind.
    assert alias is owner
    # The reader is a second spelling, so only equality is assertable — and this
    # is the assertion that catches the drift the "change both or neither"
    # comment asks a human to remember.
    assert reader == owner, (
        f"lambda_function.SCALAR_LITERAL_RECEIVER_REASON is {reader!r} but the converter owns "
        f"{owner!r} — the two spellings have drifted, so `aggregate_check_results`' third arm is "
        "unreachable and every scalar-literal refusal renders as not_yet_expressible"
    )
    # And it is still distinct from p32's two, which is what makes it a third
    # reason rather than a re-spelling (p40 3.7).
    assert reader not in ("permanently_inexpressible", "not_yet_expressible")


def test_the_third_reasons_check_detail_is_neither_of_the_other_twos_copy():
    """p40 3.7 — the third reason's CHECK copy blurs neither existing reason.

    The task's own words: "assert the third reason's detail text is not either
    of the other two reasons' copy". Asserted on the reason TEXT rather than on
    the whole detail, because the surrounding sentence ("NOT VERIFIED by the
    pandas proxy: construct ... was not converted ...") is deliberately shared —
    it is the register `LGR-b2f79ef-12` established, and sharing it is what
    makes this a third reason and not a third state. What must differ is the
    clause after `Reason (<name>):`.

    Derived by rendering all three through the real producer and comparing them
    to each other, so nothing here is a transcription of current output that
    could go stale in one direction only (`LGR-28b4d75-5`).

    Validates: p40 Requirements R4, R9
    """
    blocks = {
        "not_yet_expressible": 'df_transformed = df.select(trim(col("state")))',
        "permanently_inexpressible": 'df_transformed = df.groupBy("state").agg(count("name"))',
        _mod.SCALAR_LITERAL_RECEIVER_REASON: 'df_transformed = df.withColumn("state", trim(lit("x")))',
    }

    reason_texts: dict[str, str] = {}
    for expected_reason, block in blocks.items():
        execution_results, _exc = _refusal_execution_results(block)
        assert execution_results["not_verified_reason"] == expected_reason, (
            f"{block!r} refused for {execution_results['not_verified_reason']!r}, not {expected_reason!r} — "
            "this test is no longer comparing the three reasons it names"
        )
        _status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, execution_results)
        entry = next(c for c in checks if _mod.is_not_verified_check(c))
        marker = f"Reason ({expected_reason}): "
        assert marker in entry["detail"], f"{expected_reason}: the detail does not name its own reason"
        reason_texts[expected_reason] = entry["detail"].split(marker, 1)[1]

    third = reason_texts[_mod.SCALAR_LITERAL_RECEIVER_REASON]
    for other in ("not_yet_expressible", "permanently_inexpressible"):
        assert third != reason_texts[other], f"the third reason renders {other}'s copy verbatim"
        assert reason_texts[other] not in third, f"the third reason's copy CONTAINS {other}'s"
        assert third not in reason_texts[other], f"{other}'s copy CONTAINS the third reason's"

    # The two distinguishing clauses, named so a future edit that reintroduces
    # either into the third reason's copy fails here rather than in review.
    # `permanently_inexpressible` owns "there is nothing to fix"; the third
    # reason says "nothing in it to fix", about the generated PySpark rather than
    # about the proxy's reach, which is a different claim.
    assert "cannot yet express" not in third, "the third reason claims a converter gap — that is not_yet's"
    assert "there is nothing to fix" not in third, "the third reason claims nothing-to-fix-ever — that is permanent's"
    assert "only exists on a column" in third, "the third reason no longer says what the fence actually refused"
