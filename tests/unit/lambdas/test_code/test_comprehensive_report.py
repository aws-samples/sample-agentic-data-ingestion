# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: generate_comprehensive_test_report combined verdict.

Regression guard: execution success must NOT override blocking static-analysis
failures. A transform can run cleanly on sample data while the code review
flags a Security or Correctness defect — in that case the report must NOT
recommend "PRODUCTION READY / APPROVE / Deploy", it must require review.
"""

import os
import sys
from unittest.mock import MagicMock, patch

# Region before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_test_code",
    )
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub the heavy / AWS-touching imports so the module loads in isolation.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "artifact_schema": MagicMock(),
    "dynamo_typing": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="m")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod

generate_comprehensive_test_report = _mod.generate_comprehensive_test_report
PANDAS_PROXY_CAVEAT = _mod.PANDAS_PROXY_CAVEAT


_EXEC_OK = {
    "execution_successful": True,
    "input_records": 100,
    "output_records": 100,
    "data_quality_results": {"overall_quality_score": 98},
}


def _parsed(categories: dict[str, bool], recommendation: str = "NEEDS_IMPROVEMENT") -> dict:
    # P28 R10.6 (task 9.4): parse_test_report returns only structural data —
    # per-category verdicts + recommendation, no LLM-invented numerics.
    cats = {name: {"status": "PASSED" if ok else "FAILED", "passed": ok} for name, ok in categories.items()}
    return {
        "test_categories": cats,
        "recommendation": recommendation,
    }


def test_execution_ok_but_blocking_static_failure_is_not_production_ready():
    """The reported scenario: exec SUCCESS, but Security + Correctness FAILED."""
    parsed = _parsed(
        {
            "Correctness": False,
            "Schema Compliance": False,
            "Error Handling": True,
            "AWS Best Practices": True,
            "Security": False,
            "Performance": True,
        }
    )
    report = generate_comprehensive_test_report("static", _EXEC_OK, parsed)

    assert "REVIEW REQUIRED" in report
    assert "PRODUCTION READY" not in report  # must not falsely approve
    assert "DO NOT AUTO-DEPLOY" in report
    assert "Deploy to production environment" not in report
    # Names the blocking categories it found.
    assert "Correctness" in report
    assert "Security" in report


def test_execution_ok_only_advisory_failures_is_approve_with_fixes():
    parsed = _parsed(
        {
            "Correctness": True,
            "Schema Compliance": True,
            "Error Handling": True,
            "AWS Best Practices": True,
            "Security": True,
            "Performance": False,  # advisory only
        }
    )
    report = generate_comprehensive_test_report("static", _EXEC_OK, parsed)

    assert "APPROVE WITH FIXES" in report
    assert "REVIEW REQUIRED" not in report
    assert "❌ **NOT PRODUCTION READY**" not in report
    assert "Performance" in report


def test_execution_ok_and_all_static_passed_is_production_ready():
    parsed = _parsed(
        {
            "Correctness": True,
            "Schema Compliance": True,
            "Error Handling": True,
            "AWS Best Practices": True,
            "Security": True,
            "Performance": True,
        },
        recommendation="APPROVE",
    )
    report = generate_comprehensive_test_report("static", _EXEC_OK, parsed)

    assert "✅ **PRODUCTION READY**" in report
    assert "Deploy to production environment" in report


def test_execution_failure_is_not_production_ready_regardless_of_static():
    parsed = _parsed(
        {"Correctness": True, "Security": True},
        recommendation="APPROVE",
    )
    exec_failed = {"execution_successful": False, "error_details": "boom"}
    report = generate_comprehensive_test_report("static", exec_failed, parsed)

    assert "❌ **NOT PRODUCTION READY**" in report
    assert "REJECT" in report


def test_reject_recommendation_is_blocking_even_without_category_breakdown():
    parsed = {
        "test_categories": {},
        "recommendation": "REJECT",
    }
    report = generate_comprehensive_test_report("static", _EXEC_OK, parsed)

    assert "REVIEW REQUIRED" in report
    assert "PRODUCTION READY" not in report


def test_legacy_no_parsed_results_falls_back_to_execution_only():
    """Back-compat: without parsed_results, verdict is execution-only."""
    report = generate_comprehensive_test_report("static", _EXEC_OK)
    assert "✅ **PRODUCTION READY**" in report


# ── p32 2.22 — every execution claim names the pandas approximation ──────────
#
# The report is generated from a pandas approximation of the transform running
# over generated sample data, never from the Glue/Spark job that runs in
# production (V5 defers a real Spark run). 1.22: the PRODUCTION READY branch
# claimed "Code validated through execution and static analysis" on that basis,
# which is the strongest claim the report makes and the least supported.
#
# The verdict matrix itself is NOT changed by these tests (`LGR-334667e-1`):
# the four tests above still pin which verdict each input reaches.

_ALL_STATIC_PASS = {
    "Correctness": True,
    "Schema Compliance": True,
    "Error Handling": True,
    "AWS Best Practices": True,
    "Security": True,
    "Performance": True,
}


def _all_verdict_branches() -> dict[str, str]:
    """One report per branch of the verdict matrix, keyed by branch name."""
    return {
        "production_ready": generate_comprehensive_test_report(
            "static", _EXEC_OK, _parsed(_ALL_STATIC_PASS, recommendation="APPROVE")
        ),
        "approve_with_fixes": generate_comprehensive_test_report(
            "static", _EXEC_OK, _parsed({**_ALL_STATIC_PASS, "Performance": False})
        ),
        "review_required": generate_comprehensive_test_report(
            "static", _EXEC_OK, _parsed({**_ALL_STATIC_PASS, "Security": False})
        ),
        "not_production_ready": generate_comprehensive_test_report(
            "static", {"execution_successful": False, "error_details": "boom"}, _parsed(_ALL_STATIC_PASS)
        ),
        "legacy_no_parsed_results": generate_comprehensive_test_report("static", _EXEC_OK),
    }


def test_every_verdict_branch_names_the_pandas_approximation():
    """2.22: wherever an execution result is reported, name what executed it."""
    for branch, report in _all_verdict_branches().items():
        assert PANDAS_PROXY_CAVEAT in report, f"{branch}: report reports an execution result without naming the proxy"
        # The caveat has to be readable as text, not just present as a constant:
        # it names pandas, the generated sample data, and what it is NOT.
        assert "pandas approximation" in report, f"{branch}: does not say the execution was a pandas approximation"
        assert "generated sample data" in report, f"{branch}: does not say the data was generated sample data"
        assert "not the Glue/Spark job" in report, f"{branch}: does not say this was not the Glue/Spark job"


def test_execution_results_section_carries_the_caveat_before_the_numbers():
    """The caveat qualifies the status/record lines, so it precedes them."""
    report = generate_comprehensive_test_report("static", _EXEC_OK, _parsed(_ALL_STATIC_PASS, recommendation="APPROVE"))

    caveat_at = report.index(PANDAS_PROXY_CAVEAT)
    records_at = report.index("**Records Processed:**")
    assert caveat_at < records_at, "record counts are reported before the report says what produced them"


def test_production_ready_no_longer_claims_validation_through_execution():
    """1.22/2.22: the PRODUCTION READY verdict may not claim Spark validation.

    The verdict reached is unchanged (`LGR-334667e-1`) — only what it says
    about its own evidence.
    """
    report = generate_comprehensive_test_report("static", _EXEC_OK, _parsed(_ALL_STATIC_PASS, recommendation="APPROVE"))

    # Unchanged: which verdict is reached.
    assert "✅ **PRODUCTION READY**" in report
    assert "Deploy to production environment" in report

    # Changed: the unqualified validation claim is gone...
    assert "Code validated through execution and static analysis" not in report

    # ...and the verdict's own decision line names the approximation and its
    # limit, so the claim stays qualified even read on its own.
    decision = next(line for line in report.splitlines() if "**Deployment Decision:**" in line)
    assert "pandas approximation" in decision
    assert "generated sample data" in decision
    assert "NOT a Glue/Spark run" in decision
    assert "Spark semantics are unverified" in decision


def test_production_ready_overall_status_does_not_assert_execution_validation():
    """The top-line status is an execution claim too (2.22)."""
    report = generate_comprehensive_test_report("static", _EXEC_OK, _parsed(_ALL_STATIC_PASS, recommendation="APPROVE"))

    assert "**Overall Status:** PASSED - STATIC ANALYSIS + PANDAS-PROXY EXECUTION" in report
    assert "Spark semantics unverified" in report


def test_caveat_does_not_alter_which_verdict_is_reached():
    """Preservation 3.23: execution success still never clears a blocker."""
    reports = _all_verdict_branches()

    assert "✅ **PRODUCTION READY**" in reports["production_ready"]
    assert "🟡 **APPROVE WITH FIXES**" in reports["approve_with_fixes"]
    assert "⚠️ **REVIEW REQUIRED**" in reports["review_required"]
    assert "❌ **NOT PRODUCTION READY**" in reports["not_production_ready"]
    assert "✅ **PRODUCTION READY**" in reports["legacy_no_parsed_results"]

    # A blocking static failure is not softened into approval by the caveat.
    assert "PRODUCTION READY" not in reports["review_required"]
    assert "Deploy to production environment" not in reports["review_required"]


# ── p32 2.20/2.21 — say nothing about an execution that did not happen ───────
#
# 1.21 observed all four of these verbatim on the live run: "**Execution
# Status:** ❌ FAILED", "**Records Processed:** 0 → 0 records", the
# "### ❌ Execution Error Details" block, and the "❌ **NOT PRODUCTION READY**"
# verdict — for a transform the converter REFUSED, which therefore never ran.
#
# The fixtures below are built by calling the PRODUCER's own failure-shape
# constructors rather than by hand-writing what the reader expects: a fixture
# transcribed from the reader's side cannot detect the two sides drifting,
# because the output is its own oracle (`LGR-28b4d75-5`, template provenance).
# That is the defect this spec's Defect 1 was.

from pyspark_to_pandas import REFUSAL_REASON_SCALAR_LITERAL, ConversionError

with patch("boto3.client", MagicMock()):
    from code_interpreter_runner import CodeInterpreterTestRunner as _Runner


def _refusal_results(construct: str, line: str, line_number: int, reason: str | None = None) -> dict:
    """The real ``_conversion_failure_results`` shape for a refusal.

    p40 task 8.3 amended the signature in place with an OPTIONAL ``reason``,
    rather than adding a second helper: every existing caller keeps its exact
    behaviour (``reason=None`` is what a pre-p40 refusal carries, and
    ``_conversion_failure_results`` then falls back to
    ``classify_conversion_refusal``), and the new third reason is reached by the
    same producer call the other two use. A second helper would be a second
    place for the fixture shape to drift (`LGR-b2f79ef-6`, template / ratified).
    """
    return _Runner._conversion_failure_results(ConversionError(construct, line, line_number, reason=reason))


# 2.23 reason (b) — not yet expressible: a gap in this system (1.7).
_NOT_YET = _refusal_results(
    "withColumn",
    'df_transformed = df.withColumn("state", trim(col("state"))).withColumn("name", lower(col("name")))',
    1,
)
# 2.23 reason (a) — permanently inexpressible in a single-frame pandas proxy.
_PERMANENT = _refusal_results("groupBy", 'df_transformed = df.groupBy("state").agg(count("name"))', 3)
# p40 R4 reason (c) — the receiver-shape fence: a Series-only pandas member
# applied to a scalar-literal receiver. Built with the CONVERTER's own constant
# and asserted against what the REPORT renders, so the two spellings of the
# reason are compared across the seam rather than transcribed on one side
# (`LGR-28b4d75-5`). `trim` is deliberate: it is a name
# `classify_conversion_refusal` maps to `not_yet_expressible`, so if the carried
# reason were ignored the fixture would silently become a second `_NOT_YET`.
_SCALAR_LITERAL = _refusal_results(
    "trim",
    'df_transformed = df.withColumn("mobile_phone", trim(lit("x")))',
    2,
    reason=REFUSAL_REASON_SCALAR_LITERAL,
)

# 3.22's two shapes that must KEEP the failure wording: a transform that ran
# and failed, and a Code Interpreter run that never happened at all.
_RAN_AND_FAILED = _Runner._failed_execution_results(
    "transform execution", {"failure_reason": "KeyError: 'state'", "exit_code": 1, "stderr": "Traceback ..."}
)
_NEVER_RAN = _Runner._empty_results("Code Interpreter unavailable")


def test_a_refusal_states_no_execution_status_no_record_count_and_no_error_block():
    """2.21 — none of 1.21's execution statements may be made about a refusal.

    p40 task 8.3 added the third fixture to this loop in place, rather than
    writing a parallel test: the clause is identical for every reason, and p40's
    second Fix-Checking block asks for exactly this — "the report states neither
    execution_failed nor 0 → 0 records" — over the new refusal too.
    """
    for name, results in (("not_yet", _NOT_YET), ("permanent", _PERMANENT), ("scalar_literal", _SCALAR_LITERAL)):
        report = generate_comprehensive_test_report("static", results, _parsed(_ALL_STATIC_PASS))

        assert "❌ FAILED" not in report, f"{name}: reports execution FAILED for a transform that never ran"
        assert "Execution Status:" not in report, f"{name}: reports an execution status for a run with no execution"
        assert "0 → 0 records" not in report, f"{name}: reports 0 → 0 records for a transform that never ran"
        assert "Records Processed:" not in report, f"{name}: reports a record count for a transform that never ran"
        assert "Execution Error Details" not in report, f"{name}: renders an execution error block for a refusal"


def test_a_refusal_is_still_reported__not_verified_is_not_not_reported():
    """2.20 — the non-verification stays visible, naming the construct and line."""
    report = generate_comprehensive_test_report("static", _NOT_YET, _parsed(_ALL_STATIC_PASS))

    assert "Not Verified by the Pandas Proxy" in report
    assert "never executed" in report
    assert "withColumn" in report, "the refused construct is not named"
    assert "**At line:** 1" in report, "the refusal's line number is not named"
    # The source line the converter choked on, so a reviewer can see the shape.
    assert 'trim(col("state"))' in report


def test_a_refusal_keeps_the_two_reasons_distinguishable_in_the_report():
    """2.23 — permanent (nothing to fix) vs not-yet (a defect in the checker)."""
    not_yet = generate_comprehensive_test_report("static", _NOT_YET, _parsed(_ALL_STATIC_PASS))
    permanent = generate_comprehensive_test_report("static", _PERMANENT, _parsed(_ALL_STATIC_PASS))

    assert "not_yet_expressible" in not_yet
    assert "cannot **yet** express" in not_yet
    assert "extend the converter" in not_yet, "a converter gap does not tell the reader it is fixable"

    assert "permanently_inexpressible" in permanent
    assert "nothing here to fix" in permanent
    assert "nothing to fix" in permanent.split("**Next Steps:**")[1]

    # Neither reason is rendered in the other's words.
    assert "permanently_inexpressible" not in not_yet
    assert "not_yet_expressible" not in permanent


def test_production_ready_is_unreachable_from_a_not_verified_run():
    """2.20/2.22 — neither PRODUCTION READY verdict may follow a refusal.

    "✅ PRODUCTION READY" is a claim an unverified run cannot support (1.22);
    "❌ NOT PRODUCTION READY" is a verdict on an execution that did not happen
    (1.21). The refusal needs its own wording, which is neither.

    p40 task 8.3: the third fixture joins the loop in place — a reason added to
    the producer must inherit this, not re-earn it.
    """
    for name, results in (("not_yet", _NOT_YET), ("permanent", _PERMANENT), ("scalar_literal", _SCALAR_LITERAL)):
        for label, parsed in (
            ("all_pass", _parsed(_ALL_STATIC_PASS, recommendation="APPROVE")),
            ("advisory", _parsed({**_ALL_STATIC_PASS, "Performance": False})),
            ("legacy_no_parsed_results", None),
        ):
            report = generate_comprehensive_test_report("static", results, parsed)
            assert "PRODUCTION READY" not in report, f"{name}/{label}: a run that never executed reached a verdict"
            assert "Deploy to production environment" not in report, f"{name}/{label}: recommends deploying"
            assert "NOT VERIFIED BY THE PANDAS PROXY" in report, f"{name}/{label}: no honest verdict in its place"


def test_a_refusal_does_not_rescue_a_blocking_static_failure():
    """3.23 / `LGR-334667e-1` — no fourth outcome lets a blocker through.

    A refusal adds one correctly-classified input to the gate; it must not
    weaken it. "Execution was merely unverified" is not grounds for clearing a
    blocking static failure.
    """
    report = generate_comprehensive_test_report("static", _NOT_YET, _parsed({**_ALL_STATIC_PASS, "Security": False}))

    assert "⚠️ **REVIEW REQUIRED**" in report
    assert "DO NOT AUTO-DEPLOY" in report
    assert "Security" in report
    assert "NOT VERIFIED BY THE PANDAS PROXY" in report, "the non-verification is dropped once a blocker exists"
    # Still no statement about an execution that did not happen.
    assert "❌ FAILED" not in report
    assert "0 → 0 records" not in report
    assert "Execution Error Details" not in report
    assert "PRODUCTION READY" not in report


def test_reclassifying_a_refusal_does_not_reclassify_a_real_failure():
    """3.22 — a transform that RAN and failed keeps the failure wording."""
    report = generate_comprehensive_test_report("static", _RAN_AND_FAILED, _parsed(_ALL_STATIC_PASS))

    assert "**Execution Status:** ❌ FAILED" in report
    assert "0 → 0 records" in report
    assert "Execution Error Details" in report
    assert "KeyError: 'state'" in report
    assert "❌ **NOT PRODUCTION READY**" in report
    assert "NOT VERIFIED" not in report, "a genuine failure is reported as a non-verification"


def test_a_missing_code_interpreter_run_still_reports_failure():
    """3.22 — a run that never happened proved nothing and named no reason."""
    report = generate_comprehensive_test_report("static", _NEVER_RAN, _parsed(_ALL_STATIC_PASS))

    assert "**Execution Status:** ❌ FAILED" in report
    assert "Execution Error Details" in report
    assert "Code Interpreter unavailable" in report
    assert "❌ **NOT PRODUCTION READY**" in report
    assert "NOT VERIFIED BY THE PANDAS PROXY" not in report


def test_a_refusal_still_names_the_pandas_approximation():
    """2.22 (7a) holds on the refusal path too — the caveat is about the checker.

    p40 task 8.3: third fixture added in place.
    """
    for name, results in (("not_yet", _NOT_YET), ("permanent", _PERMANENT), ("scalar_literal", _SCALAR_LITERAL)):
        report = generate_comprehensive_test_report("static", results, _parsed(_ALL_STATIC_PASS))
        assert PANDAS_PROXY_CAVEAT in report, f"{name}: refusal report drops the pandas-approximation caveat"
        assert "not the Glue/Spark job" in report, f"{name}"


# ── p40 R4 (task 8.3) — the THIRD reason on the REPORT surface ───────────────
#
# The sibling clauses of p40's Fix-Checking property are asserted through
# `aggregate_check_results` in `test_not_verified_classification_prop.py`. The
# report is a different surface with different copy: `nv_reason_text` and
# `next_steps` are function-LOCAL assignments inside
# `generate_comprehensive_test_report`, so they are not importable and not
# indexed symbols — the only way to assert them is through a rendered report,
# which is what these two tests do.
#
# `test_a_refusal_keeps_the_two_reasons_distinguishable_in_the_report` above is
# left exactly as it was: it is p32's assertion about p32's two reasons, and it
# still holds. This is the third reason's own, stated against BOTH of them.


def test_the_third_reason_is_distinguishable_from_both_others_in_the_report():
    """p40 3.7 — a third reason must not be rendered in either other's words.

    The two phrases that distinguish the existing reasons, verified as owned
    exclusively (and their exclusivity is what the assertions turn on):
    `not_yet_expressible` owns "extend the converter"; `permanently_inexpressible`
    owns "nothing to fix here". The third reason uses NEITHER — it is not a gap
    someone should close and not a construct the proxy can never reach, it is a
    receiver shape the proxy declines on purpose.

    Validates: p40 Requirements R4, R9
    """
    scalar = generate_comprehensive_test_report("static", _SCALAR_LITERAL, _parsed(_ALL_STATIC_PASS))
    not_yet = generate_comprehensive_test_report("static", _NOT_YET, _parsed(_ALL_STATIC_PASS))
    permanent = generate_comprehensive_test_report("static", _PERMANENT, _parsed(_ALL_STATIC_PASS))

    # The reason the CONVERTER named is the reason the REPORT renders — the two
    # spellings of the constant compared across the seam, not transcribed.
    assert "scalar_literal_receiver" in scalar
    assert REFUSAL_REASON_SCALAR_LITERAL in scalar

    # ...and it is not rendered in either other reason's words.
    assert "not_yet_expressible" not in scalar
    assert "permanently_inexpressible" not in scalar
    assert "cannot **yet** express" not in scalar, "the third reason claims a converter gap — that is not_yet's copy"
    assert "extend the converter" not in scalar, "the third reason schedules converter work — that is not_yet's"
    assert "nothing here to fix" not in scalar, "the third reason claims nothing-to-fix-ever — that is permanent's"

    # Reciprocally: neither existing reason has picked up the new one's copy.
    assert "scalar_literal_receiver" not in not_yet
    assert "scalar_literal_receiver" not in permanent
    for other in (not_yet, permanent):
        assert "declines this receiver shape" not in other

    # The third reason says what the fence actually refused, in the same register
    # as the other two — what did not happen, and whose fault it is.
    assert "only exists on a column to a constant value" in scalar
    assert "never executed" in scalar
    assert "⏸️ **NOT VERIFIED BY THE PANDAS PROXY**" in scalar


def test_the_third_reasons_next_step_is_neither_of_the_other_twos():
    """p40 R4 — three reasons, three next steps, no two the same.

    The next step is what a reviewer acts on, so this is the clause that decides
    whether the third reason is useful or just present. Compared to the other two
    by extracting each report's own `**Next Steps:**` line, so the assertion is
    about the three next steps relative to each other rather than about a
    transcribed string.

    Validates: p40 Requirements R4, R9
    """

    def _next_step(results: dict) -> str:
        # Annotated because `_mod` is a dynamically imported module, so the
        # report is `Any` to mypy and `next(...)` over it would be too.
        report: str = generate_comprehensive_test_report("static", results, _parsed(_ALL_STATIC_PASS))
        return next(line for line in report.splitlines() if "**Next Steps:**" in line)

    scalar = _next_step(_SCALAR_LITERAL)
    not_yet = _next_step(_NOT_YET)
    permanent = _next_step(_PERMANENT)

    assert scalar != not_yet
    assert scalar != permanent
    # The two existing next steps are already distinct from each other (pinned
    # above by p32); this is the pair-wise check that the third did not collapse
    # onto either. Substring in both directions, because a next step that merely
    # CONTAINS another's advice has still given that advice.
    assert "extend the converter" not in scalar, "the third reason tells the reader to close a gap that is not one"
    assert "nothing to fix here" not in scalar, "the third reason tells the reader nothing is ever fixable here"

    # What it says instead: judge on static analysis, and know the silence is a
    # decision rather than a shortfall.
    assert "static analysis" in scalar
    assert "no converter work to schedule" in scalar
    assert "no defect to send back to code generation" in scalar


# ── p42 defect 2 — the report names the executor that actually ran ───────────
#
# `VERIFICATION_EXECUTOR=glue` was deployed and the report still stated the
# pandas caveat: a run verified on real Spark told the operator it was a pandas
# approximation. Observed on run `run-2026-08-18-17-48-19-bd6f4deb`, where the
# stored `output_data.summary.test_report_markdown` carries both the pandas
# caveat and a Glue `IndentationError` from `<stdin>` in the same report.
#
# THE AUTHORITY FOR CHANGING THE CAVEAT IS ITS OWN REVISIT CONDITION.
# `PANDAS_PROXY_CAVEAT`'s comment says: "Do NOT delete this caveat without
# landing V5", where V5 is "running the generated code against genuine Spark,
# e.g. a Glue interactive session". V5 has landed — it is the
# `real-spark-verification` spec — so the condition is met. Nothing here deletes
# the caveat: it is a BRANCH, and the pandas arm is asserted unchanged below,
# because that arm is what the fence protects (p32 2.22).
#
# WHAT IS NOT ALLOWED TO MOVE, and is asserted rather than assumed: the verdict.
# `LGR-334667e-1`'s matrix decides which verdict each input reaches, and the
# executor is not one of its inputs. `test_the_verdict_is_identical_under_both_executors`
# is the guard.
#
# Not duplicated here:
# * `test_no_proxy_non_verification_prop.py` (Property 4) — no Glue result can
#   carry `not_verified_by_proxy`, which is why the refusal arms below keep
#   their pandas wording with no branch and no test of their own on `glue`.
# * `test_executor_selection_totality_prop.py` (Property 1) — how the executor
#   VALUE is resolved. This file takes the resolved value as given.
#
# ── p44 R5.3 / R9.6 — the six caveat assertions below pin the SYNTHETIC frame ──
#
# `GLUE_SPARK_CAVEAT` stopped being "the Glue report's wording" and became the
# SYNTHETIC-FRAME arm of `_GLUE_FRAME_CAVEATS` (`lambda_function.py:2240`), beside
# `GLUE_SPARK_SAMPLE_CAVEAT` and `GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT`. The same
# split applies to the `overall_status` parenthetical (`_GLUE_FRAME_STATUS_NOTES`,
# `:2258`) and to the PRODUCTION READY deployment clause
# (`_GLUE_FRAME_DECISION_CLAUSES`, `:2274`).
#
# NO ASSERTION IN THIS SECTION IS AMENDED OR DELETED, and that is the point.
# The constant's bytes are unchanged, the synthetic status note and decision clause
# reproduce p42's strings byte for byte, and all three are what a `frame_provenance`
# of `None` — the new keyword-only parameter's default — selects. Every call in
# `_all_verdict_branches_on` omits it, so every report rendered below is a
# synthetic-frame report. Verified rather than assumed: `frame_provenance=None` and
# an explicit `{"frame": "synthetic", …}` render byte-identical reports.
#
# Nothing narrowed. Before p44 wired the Frame_Mode caller, synthetic was the only
# reachable frame, so the synthetic-frame report is what these six always
# described; what changed is that the frame is now a dimension a reader has to
# name. `LGR-60c4985-6` (**template / ratified**) is the practice — record the
# reversal at the assertion, so a later reader finds the reason with the
# requirement — and `LGR-5108dd4-2` and `LGR-0fe3e5d-3` are the two most recent
# applications of it in this file. Rewriting `GLUE_SPARK_CAVEAT` instead would have
# forced ten assertion edits across this file and
# `test_report_names_the_running_executor.py` to express identical behaviour, each
# one a chance to encode a guess as a pin.
#
# Where the OTHER two frame arms are asserted: the frame dimension's own property
# (Property 6) over frame x verdict, and the named per-arm report examples that sit
# beside it. This section stays the synthetic baseline they are measured against.

GLUE_SPARK_CAVEAT = _mod.GLUE_SPARK_CAVEAT
PANDAS = _mod.VERIFICATION_EXECUTOR_PANDAS
GLUE = _mod.VERIFICATION_EXECUTOR_GLUE
SHADOW = _mod.VERIFICATION_EXECUTOR_SHADOW


def _all_verdict_branches_on(executor: str) -> dict[str, str]:
    """``_all_verdict_branches`` with an explicit executor.

    Deliberately a second function rather than a parameter on the original: the
    original's call sites are the *pandas contract*, and they must keep calling
    with no executor argument so that the default is what they exercise.
    """
    return {
        "production_ready": generate_comprehensive_test_report(
            "static", _EXEC_OK, _parsed(_ALL_STATIC_PASS, recommendation="APPROVE"), executor=executor
        ),
        "approve_with_fixes": generate_comprehensive_test_report(
            "static", _EXEC_OK, _parsed({**_ALL_STATIC_PASS, "Performance": False}), executor=executor
        ),
        "review_required": generate_comprehensive_test_report(
            "static", _EXEC_OK, _parsed({**_ALL_STATIC_PASS, "Security": False}), executor=executor
        ),
        "not_production_ready": generate_comprehensive_test_report(
            "static",
            {"execution_successful": False, "error_details": "boom"},
            _parsed(_ALL_STATIC_PASS),
            executor=executor,
        ),
        "legacy_no_parsed_results": generate_comprehensive_test_report("static", _EXEC_OK, executor=executor),
    }


def test_every_verdict_branch_on_glue_names_the_spark_run():
    """The mirror of ``test_every_verdict_branch_names_the_pandas_approximation``.

    Both halves in one test, because "names Spark" is worth little without "and
    no longer names pandas": the defect was an unconditional pandas claim, so a
    report that added the Spark sentence beside it would still be lying.

    **p44 R5.3 — assertions unchanged; every branch here is a SYNTHETIC-frame Glue
    report.** ``_all_verdict_branches_on`` passes no ``frame_provenance``, so each
    call takes the parameter's ``None`` default and the synthetic arm. Read the
    first assertion as "a Glue run over a synthetic frame names Spark", not as
    "every Glue run carries this string" — a sample run carries
    ``GLUE_SPARK_SAMPLE_CAVEAT`` and would fail it, correctly. The other two
    assertions are frame-independent: no arm of ``_GLUE_FRAME_CAVEATS`` may say
    "pandas approximation" or "generated sample data" (C13's fifth forbidden pair),
    so they hold across all three arms and not only this one.
    """
    for branch, report in _all_verdict_branches_on(GLUE).items():
        assert GLUE_SPARK_CAVEAT in report, f"{branch}: a Spark run is reported without naming Spark"
        assert PANDAS_PROXY_CAVEAT not in report, f"{branch}: a Spark run still claims to be a pandas approximation"
        assert "pandas approximation" not in report, f"{branch}: still calls the execution a pandas approximation"
        assert "generated sample data" not in report, f"{branch}: still calls the input generated sample data"


def test_the_glue_caveat_claims_spark_semantics_and_denies_real_source_data():
    """Both halves of the honest claim, as text a reviewer reads.

    Overclaiming is the same defect in the other direction: a report that said
    "verified against your data" would be as wrong as the pandas claim it
    replaced.

    **p44 R5.3 — docstring amended; the assertions are deliberately UNCHANGED.**
    The old justification was "``frame_mode`` has no caller, so
    ``resolve_frame_source`` returns on its first branch every run and the frame is
    synthesised from the DECLARED schema (R9.3, design C8 — the runner reads no
    JDBC source at all)". **That premise is now false** — p44 wires the caller, and
    ``LGR-5c30733-3``, which rested on it, is superseded — so it is replaced here
    rather than left to mislead a reader into thinking a synthetic frame is the
    only reachable one.

    What the assertions pin stands on its own reason, and it is a narrower claim
    than it used to be: this is the **synthetic-frame** arm's text, reached because
    ``_all_verdict_branches_on`` passes no ``frame_provenance``. The four strings
    below are still the honest wording for that frame, and the two negative ones —
    "not the pipeline's own source data", "not the full production Glue job" — are
    what R5.5 requires a non-sample run to keep saying. A sample run's caveat
    inverts the first of them, which is the whole point of the frame dimension and
    is asserted in the sample arm's own example, not here.
    """
    report = _all_verdict_branches_on(GLUE)["production_ready"]

    assert "real Spark" in report
    assert "Glue interactive session" in report
    assert "synthetic frame built from the declared source schema" in report
    assert "not the pipeline's own source data" in report
    assert "not the full production Glue job" in report


def test_the_section_heading_names_the_executor():
    """The heading is a claim too, and it was one of the four wrong ones."""
    glue_report = _all_verdict_branches_on(GLUE)["production_ready"]
    pandas_report = _all_verdict_branches_on(PANDAS)["production_ready"]

    assert "## 🚀 Real Spark Execution Results (Glue interactive session)" in glue_report
    assert "Code Interpreter Execution Results" not in glue_report
    # The pandas heading is untouched.
    assert "## 🚀 Code Interpreter Execution Results" in pandas_report
    assert "Real Spark Execution Results" not in pandas_report


def test_the_overall_status_line_names_the_executor():
    """``overall_status`` is the row the dashboard shows first.

    It is also the string p32 wrote to say "Spark semantics unverified" — which
    is false on this executor, and stating it there would contradict the caveat
    two lines above it.

    **p44 R5.3 / R5.7 — assertions unchanged; ``(synthetic input data)`` is now one
    of three parentheticals.** It is ``_GLUE_FRAME_STATUS_NOTES[FRAME_SYNTHETIC]``
    (``lambda_function.py:2258``), byte-identical to what p42 shipped, selected by
    the ``frame_provenance=None`` default. A sample run reads ``(real source
    sample, 100 rows)`` here and would fail the first assertion below, correctly.

    This line is also one of the two carriers R5.7 depends on, so its position is
    load-bearing and not incidental: measured on the report this test renders,
    ``synthetic input data`` occupies offsets 135-155 and the executor caveat
    occupies 253-486, inside the 500 characters ``review_summary`` slices
    (``lambda_function.py:3143``). That is why the frame claim is branched into
    strings already inside the excerpt rather than appended as a sentence — 14
    characters of headroom is not a budget an appended sentence fits in. The
    excerpt bound has its own property; this test does not assert offsets.
    """
    glue_report = _all_verdict_branches_on(GLUE)["production_ready"]
    pandas_report = _all_verdict_branches_on(PANDAS)["production_ready"]

    assert "PASSED - STATIC ANALYSIS + REAL SPARK EXECUTION (synthetic input data)" in glue_report
    assert "PANDAS-PROXY EXECUTION" not in glue_report
    assert "Spark semantics unverified" not in glue_report
    # And p32's own string is still what the pandas path reports.
    assert "PASSED - STATIC ANALYSIS + PANDAS-PROXY EXECUTION (Spark semantics unverified)" in pandas_report


def test_the_production_ready_deployment_decision_names_the_executor():
    """The fourth site: the strongest claim in the report (1.22).

    **p44 R5.3 / R5.5 — assertions unchanged; this is the SYNTHETIC-frame decision
    clause.** The sentence is now a fixed prefix plus
    ``_GLUE_FRAME_DECISION_CLAUSES[FRAME_SYNTHETIC]``
    (``lambda_function.py:2274``), which together reproduce p42's sentence byte for
    byte on the ``frame_provenance=None`` default. So ``behaviour on real source
    data is not`` below is a **frame** claim, not an executor claim: it is R5.5's
    withheld own-source claim, and the sample arm replaces it with a claim about
    the sampled rows specifically. This clause sits past character 500, so it is
    not what satisfies R5.7 — it is here because R5.5 forbids an own-source claim
    on a non-sample run and this is one of the places that claim is made or
    withheld.

    ``"pandas" not in decision`` is the assertion with the widest reach: it
    constrains every arm of ``_GLUE_FRAME_DECISION_CLAUSES``, not just this one, so
    no frame arm added later may name pandas.
    """
    glue_report = _all_verdict_branches_on(GLUE)["production_ready"]

    decision = next(line for line in glue_report.splitlines() if "**Deployment Decision:**" in line)
    assert "real Spark" in decision
    assert "Spark semantics are verified" in decision
    assert "behaviour on real source data is not" in decision
    assert "pandas" not in decision


def test_the_verdict_is_identical_under_both_executors():
    """`LGR-334667e-1`'s matrix does not take the executor as an input.

    The executor changes what the report says about its *evidence*; it may not
    change which verdict that evidence reaches. Asserted on every branch, and on
    the verdict banner plus the next step, which are the two strings the gate's
    readers act on.
    """
    on_pandas = _all_verdict_branches_on(PANDAS)
    on_glue = _all_verdict_branches_on(GLUE)

    banners = (
        "✅ **PRODUCTION READY**",
        "🟡 **APPROVE WITH FIXES**",
        "⚠️ **REVIEW REQUIRED**",
        "❌ **NOT PRODUCTION READY**",
    )
    for branch in on_pandas:
        pandas_banner = [b for b in banners if b in on_pandas[branch]]
        glue_banner = [b for b in banners if b in on_glue[branch]]
        assert pandas_banner == glue_banner, f"{branch}: the executor changed the verdict banner"

        def _next_step(report: str) -> str:
            return next(line for line in report.splitlines() if "**Next Steps:**" in line)

        assert _next_step(on_pandas[branch]) == _next_step(on_glue[branch]), (
            f"{branch}: the executor changed the next step"
        )


def test_shadow_reports_the_pandas_caveat():
    """Correct, not a fallback — and the reason is R4.1.

    Under ``shadow`` the pandas proxy is the AUTHORITATIVE executor: the verdict
    and the check list derive from it alone, the Glue half is non-authoritative
    and its result reaches only the ``shadow_verification`` row
    (``LGR-ad78946-3``). ``execution_results`` — the dict this report describes —
    is the pandas one, so naming Spark here would attribute the verdict to the
    run that did not produce it.

    **p44 R5.3 / R9.6 — assertions unchanged, and this is one of
    ``LGR-5c30733-3``'s three surviving claims.** p44 supersedes that record — its
    stated basis was "``frame_mode`` has no caller" — but "``shadow`` maps to the
    pandas caveat" is carried forward untouched, and this test is where it is
    pinned at the builder. It is also the reason the frame dimension is a **second
    mapping composed onto the ``glue`` arm** rather than a re-keying of
    ``_EXECUTOR_CAVEATS`` on ``(executor, frame)``: under ``shadow`` the
    authoritative result is the pandas one, which has no frame source at all, so
    those cells cannot exist. The second assertion below therefore excludes all
    three frame arms at once, not only the synthetic one.
    """
    for branch, report in _all_verdict_branches_on(SHADOW).items():
        assert PANDAS_PROXY_CAVEAT in report, f"{branch}: shadow's authoritative pandas run is not named"
        assert GLUE_SPARK_CAVEAT not in report, f"{branch}: shadow claims the verdict came from Spark"


def test_an_unrecognised_executor_reports_the_pandas_caveat():
    """The report's own conservative fall-through, which no longer mirrors the resolver's.

    **Docstring amended; the assertions are deliberately UNCHANGED.** This used to
    justify itself by deferring to the resolver: "A typo in the deployed flag
    already resolves to ``pandas`` (``resolve_effective_executor``), so a report
    that claimed Spark on an unrecognised value would describe an executor that
    did not run." That premise is now false — a typo resolves to ``glue`` under
    the glue-first reversal — so the justification is replaced rather than left to
    mislead.

    The behaviour stands on its own reason, which is stronger than the borrowed
    one: this argument governs what the report *claims*, and an unrecognised label
    must understate what ran rather than overclaim. Naming Spark on a label nobody
    recognises would be the overclaim direction p42 and p43 both exist to remove.
    Raising is still rejected — a report is not the place to fail a run over a flag
    (``LGR-ad78946-5``).

    **p44 R5.3 / R9.6 — assertions still unchanged, and the posture now repeats one
    level down.** An unrecognised *executor* reaches no frame arm at all, so the
    second assertion below excludes all three of them rather than just the
    synthetic one. The frame dimension answers its own unrecognised case the same
    conservative way: ``_GLUE_FRAME_CAVEATS.get(frame, GLUE_SPARK_CAVEAT)``
    (``lambda_function.py:2514``) falls to the synthetic arm on any value outside
    the recognised three, and ``.get`` rather than ``[...]`` for the same
    ``LGR-ad78946-5`` reason cited above — the dict crosses a module boundary and a
    report is not the place to raise. Understate what ran; never claim the
    pipeline's own source data on a value nobody recognises (R5.5).
    """
    report = generate_comprehensive_test_report(
        "static", _EXEC_OK, _parsed(_ALL_STATIC_PASS, recommendation="APPROVE"), executor="Glue"
    )

    assert PANDAS_PROXY_CAVEAT in report
    assert GLUE_SPARK_CAVEAT not in report


def test_the_default_executor_is_pandas():
    """Omitting the argument means pandas, byte for byte.

    **Docstring amended; the assertion is deliberately UNCHANGED.** The old
    reasoning was "the handler's own default resolution is ``pandas``, so the
    default has to be the honest one" — and that premise is now false: the
    resolver's fall-through is ``glue`` after the glue-first reversal. The
    parameter default stays ``pandas`` anyway, and the divergence is the point.

    This argument governs what the report *claims*, not what runs, and the handler
    always passes the resolved value explicitly. The default is therefore only ever
    reached by a caller that forgot — 30-odd of them in this suite. ``pandas``
    makes such a caller UNDERSTATE what ran, which is conservative and visible;
    ``glue`` would make the report claim real Spark on a run that may not have had
    it, the overclaim direction p42 and p43 both exist to remove. Byte equality
    against the explicit call, so the default cannot drift into a third behaviour.
    """
    args = ("static", _EXEC_OK, _parsed(_ALL_STATIC_PASS, recommendation="APPROVE"))

    assert generate_comprehensive_test_report(*args) == generate_comprehensive_test_report(*args, executor=PANDAS)


def test_the_refusal_arms_keep_their_pandas_wording():
    """p32 2.22's fence, still standing where it applies.

    A proxy refusal is a pandas-path outcome by construction — Property 4 —
    so these arms are asserted UNCHANGED rather than branched. Their copy is
    load-bearing: it is what tells a reviewer the generated PySpark is not at
    fault.
    """
    for name, results in (("not_yet", _NOT_YET), ("permanent", _PERMANENT), ("scalar_literal", _SCALAR_LITERAL)):
        report = generate_comprehensive_test_report("static", results, _parsed(_ALL_STATIC_PASS))

        assert PANDAS_PROXY_CAVEAT in report, f"{name}"
        assert "NOT VERIFIED BY THE PANDAS PROXY" in report, f"{name}"


# ── p43 R4.1/R4.2 (task 11.3) — the retention wording, branch by branch ──────
#
# A Retained_Finding is a reviewer transform-block finding at a
# Non_Blocking_Severity on a run whose Effective_Verdict is `pass`: reported,
# displayed, and deliberately not blocking. `generate_comprehensive_test_report`
# states that twice — a `- **Reported, not blocking:**` line in the
# `### Static Analysis Summary` block, and a clause appended to the
# `**Deployment Decision:**` sentence after the branch chain has chosen its
# wording.
#
# WHAT IS ASSERTED HERE, AND WHAT IS NOT.
# `test_review_verdict_detail_retention_prop.py` (task 10.2) owns the NUMBER:
# Property 7 pins that the count in the check detail, the count in the summary
# block and the count in the decision line all equal
# `len(retained_nonblocking_findings(...))`, and that both report renderings are
# absent at zero. Its own docstring on the blocking branch says the wording is
# task 11.3's. This section is the wording, so it asserts no counts beyond the
# one digit it needs to identify a rendering.
# `test_comprehensive_report_severity_blind_prop.py` (task 11.2) owns Property 9,
# that the Blocking and Advisory tallies are severity-blind. Not repeated here.
#
# C13's five forbidden strings are `"PRODUCTION READY"`, `"REVIEW REQUIRED"`,
# `"NOT VERIFIED"`, `"Deploy to production environment"` and the pair
# `"pandas approximation"` / `"generated sample data"`. Every one of them is
# ALREADY asserted absent above, in the branches where it would be a lie —
# `test_execution_ok_but_blocking_static_failure_is_not_production_ready`
# (the first and the fourth), `test_execution_ok_only_advisory_failures_is_
# approve_with_fixes` (the second), `test_reclassifying_a_refusal_does_not_
# reclassify_a_real_failure` (the third) and
# `test_every_verdict_branch_on_glue_names_the_spark_run` (the fifth pair).
# Those assertions are not duplicated. The subject here is the narrower and
# different claim that the NEW wording introduces none of them — which cannot be
# asserted against a whole report, because four of the six legitimately appear in
# one branch or another. It is asserted against the text the retention adds,
# isolated by rendering the same report twice.

import json
import re

from review_verdict import (
    NON_BLOCKING_SEVERITIES,
    parse_review_verdict,
    retained_nonblocking_findings,
    verdict_to_parsed_results,
)

#: The Non_Blocking_Severity token, read off the real frozenset rather than
#: spelled out, so nothing in this section drifts if the set grows a second tier.
_NON_BLOCKING = sorted(NON_BLOCKING_SEVERITIES)[0]

#: C13's five forbidden strings (six spellings — the last is a pair). Named as
#: data so the failure message says which one appeared.
_C13_FORBIDDEN_STRINGS = (
    "PRODUCTION READY",
    "REVIEW REQUIRED",
    "NOT VERIFIED",
    "Deploy to production environment",
    "pandas approximation",
    "generated sample data",
)

_RETENTION_SUMMARY_PREFIX = "- **Reported, not blocking:**"
#: The claim R4.1 requires of both renderings. It names **the review gate**
#: rather than static analysis, which is the whole of R4.2's reading — see
#: `test_a_retained_finding_under_a_blocking_status_line_names_only_the_review_gate`.
_RETENTION_MARKER = "did not block the review gate"
_STATIC_SUMMARY_HEADING = "### Static Analysis Summary"


def _retained_parsed(categories: dict[str, bool], issue_count: int = 1) -> dict:
    """``parsed_results`` carrying ``issue_count`` Retained_Findings.

    Built by running a reviewer JSON document through the REAL
    `parse_review_verdict` and then the REAL `verdict_to_parsed_results`, rather
    than by hand-writing the canonical shape: `attribution_source` is whatever
    the parser decides it is, and `recommendation` is whatever the gate derives,
    so the fixture cannot agree with the producer by construction
    (`LGR-28b4d75-5`, **template**). It is also the shape the handler actually
    passes — `lambda_function.py:2640` adapts the parsed verdict this way.

    The raw verdict is `pass` because that is the only raw verdict a retained
    finding can occur on: on a raw `fail`, a transform-block issue is not
    explicit scaffolding, so rules 2/3 fail the gate and
    `retained_nonblocking_findings` returns `[]` by its own guard.
    """
    return _parsed_from_docs(
        categories,
        [
            {
                "severity": _NON_BLOCKING,
                "category": "Correctness",
                "description": f"A naming nit, number {n}.",
                "attribution": "transform_block",
            }
            for n in range(issue_count)
        ],
    )


def _parsed_from_docs(categories: dict[str, bool], issue_docs: list[dict]) -> dict:
    """The same adapter chain over an arbitrary issue list, including empty."""
    document = {
        "verdict": "pass",
        "issues": issue_docs,
        "categories": {name: ("pass" if ok else "fail") for name, ok in categories.items()},
    }
    return verdict_to_parsed_results(parse_review_verdict(json.dumps(document)))


def _summary_block(report: str) -> str:
    """The ``### Static Analysis Summary`` block, up to the next heading.

    Sliced rather than scanned over the whole report so the two renderings are
    read from the two places they are built, independently: a claim that landed
    in the decision line but not the summary block is a real divergence between
    two surfaces, and reading the document as one string would hide it.
    """
    start = report.find(_STATIC_SUMMARY_HEADING)
    if start == -1:
        return ""
    end = report.find("### Recommendations", start)
    return report[start:end] if end != -1 else report[start:]


def _decision_line(report: str) -> str:
    """The ``1. **Deployment Decision:** …`` line, whichever branch built it."""
    for line in report.splitlines():
        if line.startswith("1. **Deployment Decision:**"):
            return line
    return ""


def _retention_summary_line(report: str) -> str:
    """The single retention line in the summary block, or ``""`` when absent."""
    matches = [line for line in _summary_block(report).splitlines() if line.startswith(_RETENTION_SUMMARY_PREFIX)]
    assert len(matches) <= 1, f"expected at most one retention line, got {matches!r}"
    return matches[0] if matches else ""


def _retention_branches() -> dict[str, tuple[dict | None, dict]]:
    """``{branch: (parsed_results_with_a_retained_finding, execution_results)}``.

    Every branch of the verdict chain in `generate_comprehensive_test_report`,
    not a subset of it: the four rows of `LGR-334667e-1`'s matrix, both arms of
    the p32 refusal split (which the matrix does not cover and which build their
    own `deployment_decision` wording), and the legacy no-`parsed_results` call
    as the negative control. Deliberately a separate helper from
    `_all_verdict_branches`, for the reason that one states about
    `_all_verdict_branches_on`: the original's call sites are a contract and must
    keep exercising the fixtures they were written against.
    """
    return {
        "production_ready": (_retained_parsed(_ALL_STATIC_PASS), _EXEC_OK),
        "approve_with_fixes": (_retained_parsed({**_ALL_STATIC_PASS, "Performance": False}), _EXEC_OK),
        "review_required": (_retained_parsed({**_ALL_STATIC_PASS, "Security": False}), _EXEC_OK),
        "not_production_ready": (
            _retained_parsed(_ALL_STATIC_PASS),
            {"execution_successful": False, "error_details": "boom"},
        ),
        "not_verified_blocking": (_retained_parsed({**_ALL_STATIC_PASS, "Security": False}), _NOT_YET),
        "not_verified_clean": (_retained_parsed(_ALL_STATIC_PASS), _NOT_YET),
        "legacy_no_parsed_results": (None, _EXEC_OK),
    }


def test_every_verdict_branch_states_the_retention_and_introduces_no_forbidden_string():
    """4.1 — R4.1/R4.2/R4.4/R4.5: the wording, on every branch, adding nothing else.

    One example per branch of the verdict chain. Each branch is rendered TWICE
    from inputs that differ in exactly one respect — whether the reviewer's issue
    list carries the Retained_Finding — so the text the retention adds can be
    isolated and read on its own. Nothing else about the two calls differs: the
    report reads `recommendation` and `test_categories` off `parsed_results`, and
    both are unchanged here because the Effective_Verdict is `pass` either way,
    so `verdict_to_parsed_results` derives APPROVE for both.

    That isolation is what makes three separate claims assertable at once:

    * **R4.1** — both renderings appear, and both state the retention.
    * **C13** — none of the five forbidden strings is INTRODUCED. Four of the six
      spellings legitimately appear in one branch or another (`PRODUCTION READY`
      and `Deploy to production environment` on the first branch, `REVIEW
      REQUIRED` on the third, `NOT VERIFIED` on the refusal arms, the pandas pair
      on every pandas run), so a whole-report absence assertion would be either
      false or vacuous. The added text is where the claim is true and meaningful.
      No all-caps phrase either, per task 11.1's constraint.
    * **R4.4/R4.5** — the rest of the report is BYTE-IDENTICAL, reconstructed by
      removing the two additions. So the recommendation-driven blocking
      assessment (R4.4) and the per-category assessment (R4.5) are reached
      unchanged, and `LGR-5c30733-3`'s executor-branched caveat is untouched:
      the decision clause is proved to be a pure suffix rather than an edit to
      either the Glue or the pandas arm.
    """
    for branch, (parsed, execution) in _retention_branches().items():
        with_retained = generate_comprehensive_test_report("static", execution, parsed)

        if parsed is None:
            # The negative control. Without `parsed_results` there is no verdict
            # to retain anything from, and both renderings are conditional on a
            # non-empty retained list, so neither may appear.
            assert _retention_summary_line(with_retained) == "", f"{branch}: retention line with no parsed_results"
            assert _RETENTION_MARKER not in with_retained, f"{branch}: retention claim with no parsed_results"
            continue

        assert len(retained_nonblocking_findings(parsed)) == 1, f"{branch}: fixture retains nothing"

        # The same report from the same inputs, minus the retained finding.
        categories = {name: info["passed"] for name, info in parsed["test_categories"].items()}
        without_retained = generate_comprehensive_test_report("static", execution, _parsed_from_docs(categories, []))
        assert retained_nonblocking_findings(_parsed_from_docs(categories, [])) == [], f"{branch}: control retains"

        # R4.1, first rendering: the summary line, naming the count, the
        # severity and the fact that it did not block.
        summary_line = _retention_summary_line(with_retained)
        assert summary_line, f"{branch}: no retention line in the Static Analysis Summary block"
        assert "1 reviewer finding(s)" in summary_line, f"{branch}: {summary_line!r} does not name the count"
        assert _NON_BLOCKING in summary_line, f"{branch}: {summary_line!r} does not name the severity"
        assert _RETENTION_MARKER in summary_line, f"{branch}: {summary_line!r} does not state what did not block"
        assert _retention_summary_line(without_retained) == "", f"{branch}: control renders a retention line"

        # R4.1, second rendering: a clause on the Deployment Decision sentence.
        # Asserted as a pure SUFFIX, which is the `LGR-5c30733-3` half: the
        # branch's own wording is byte-identical up to the append.
        with_decision, without_decision = _decision_line(with_retained), _decision_line(without_retained)
        assert with_decision.startswith(without_decision), (
            f"{branch}: the retention clause rewrote the branch's own decision wording rather than "
            f"appending to it:\n  with:    {with_decision!r}\n  without: {without_decision!r}"
        )
        added_clause = with_decision[len(without_decision) :]
        assert "1 reviewer finding(s)" in added_clause, f"{branch}: {added_clause!r} does not name the count"
        assert _RETENTION_MARKER in added_clause, f"{branch}: {added_clause!r} does not state what did not block"

        # C13, over exactly the text the retention introduced.
        added_text = f"{summary_line}\n{added_clause}"
        for forbidden in _C13_FORBIDDEN_STRINGS:
            assert forbidden not in added_text, f"{branch}: the retention wording introduces {forbidden!r}"
        assert not re.search(r"[A-Z]{4,}", added_text), f"{branch}: the retention wording shouts: {added_text!r}"

        # R4.4/R4.5: nothing else moved. Remove the two additions and the report
        # must be what it was — so no verdict, tally, banner, next step, caveat
        # or status line was disturbed by the retention on this branch.
        rebuilt = with_retained.replace(f"{summary_line}\n", "").replace(added_clause, "")
        assert rebuilt == without_retained, (
            f"{branch}: the retained finding changed the report somewhere other than its two "
            "renderings — the verdict matrix, a tally or a caveat moved"
        )


def test_a_retained_finding_under_a_blocking_status_line_names_only_the_review_gate():
    """4.2 — design §5 risk 2: the one confusable pairing, and R4.2's reading of it.

    Effective_Verdict `pass`, so the `minor` reviewer transform-block finding is
    retained; `categories` carry a failing **Correctness**, which is a
    Blocking_Category, so `has_blocking` is True (`lambda_function.py:2178`) and
    the run reaches `⚠️ **REVIEW REQUIRED**` with an `overall_status` asserting
    that static analysis blocked (`:2255-2256`). A retained finding therefore
    appears in a report whose status line says static analysis blocked — which is
    the shape R4.2 forbids reading as "this finding blocked".

    **The design flagged this as a copy judgement rather than resolving it.** §5
    risk 2 records that a reader could reasonably want the retention line
    suppressed on this branch instead, and that R4.2 is read as satisfied only
    because both new sentences name **the review gate** specifically while the
    banner is about a failed category. This test is that reading, made falsifiable
    — the four things the wording has to do to carry the distinction:

    1. Both renderings name the review gate, and neither names static analysis,
       so neither can be read as a claim about the thing the banner is about.
    2. Neither claims these findings blocked anything, and neither names the
       category that did.
    3. The retention line is a SIBLING of the `Blocking Failures` line and comes
       after it, so the reader is told what blocked before what did not.
    4. The heading both sit under is an inventory heading — `Static Analysis
       Summary` — and not itself an assertion that static analysis blocked, which
       is the literal thing R4.2 forbids presenting a retained finding beneath.

    `test_review_verdict_detail_retention_prop.py::test_a_retained_finding_
    coexists_with_a_blocking_static_failure` (task 10.2) already pins that both
    renderings appear on this branch with the right count. This is the wording it
    defers to 11.3.
    """
    parsed = _retained_parsed({**_ALL_STATIC_PASS, "Correctness": False})
    assert len(retained_nonblocking_findings(parsed)) == 1
    # An effective pass is the premise: this is a retained finding, not a
    # blocking one, and the reviewer's own recommendation reflects that.
    assert parsed["recommendation"] == "APPROVE"

    report = generate_comprehensive_test_report("static", _EXEC_OK, parsed)

    # The premise, asserted rather than assumed: this really is the branch whose
    # status line says static analysis blocked.
    assert "⚠️ **REVIEW REQUIRED**" in report
    assert "**Overall Status:** EXECUTION PASSED — STATIC ANALYSIS BLOCKING ISSUES" in report
    blocking_line = next(line for line in report.splitlines() if line.startswith("- **Blocking Failures:**"))
    assert "Correctness" in blocking_line

    summary_line = _retention_summary_line(report)
    decision_line = _decision_line(report)
    assert summary_line, "the retention line is absent on the blocking branch"
    assert "DO NOT AUTO-DEPLOY" in decision_line, "the blocking branch's own decision wording is gone"

    # The decision clause, isolated the same way test 4.1 isolates it — as the
    # suffix over the same report with the finding removed — rather than by
    # splitting on punctuation. It matters here specifically: this branch's OWN
    # wording says "static analysis flagged blocking issues", so a sloppy slice
    # would hand assertion (1) that phrase and pass for the wrong reason.
    without = generate_comprehensive_test_report(
        "static", _EXEC_OK, _parsed_from_docs({**_ALL_STATIC_PASS, "Correctness": False}, [])
    )
    without_decision = _decision_line(without)
    assert decision_line.startswith(without_decision)
    added_clause = decision_line[len(without_decision) :]

    for where, retention_text in (("the summary line", summary_line), ("the decision clause", added_clause)):
        # (1) Names the review gate; says nothing about static analysis. This is
        # the distinction the whole reading rests on: the retention sentences and
        # the banner have different subjects, so one cannot contradict the other.
        assert _RETENTION_MARKER in retention_text, f"{where}: does not name the review gate: {retention_text!r}"
        assert "static analysis" not in retention_text.lower(), (
            f"{where}: the retention wording talks about static analysis, which is what the blocking "
            f"status line is about — the two become confusable: {retention_text!r}"
        )

        # (2) Claims no block, and names no category. "blocking" appears only as
        # "non-blocking", and "block" only as "did not block".
        assert "blocked" not in retention_text, f"{where}: claims these findings blocked: {retention_text!r}"
        assert "Correctness" not in retention_text, (
            f"{where}: names the failed category inside the retention wording, so the retained finding "
            f"reads as the thing that failed it: {retention_text!r}"
        )
        # ...and it does not overclaim in the other direction either: it says
        # these findings did not block the gate, never that nothing did.
        assert "passed" not in retention_text.lower(), (
            f"{where}: the retention wording claims something passed, in a report whose status line "
            f"says static analysis blocked: {retention_text!r}"
        )

    # (3) A sibling bullet in the same block, after the line that names what did
    # block — not nested beneath it and not ahead of it.
    block = _summary_block(report)
    assert block.index(blocking_line) < block.index(summary_line), (
        "the retention line precedes the Blocking Failures line, so a reader meets what did NOT block "
        "before being told what did"
    )
    assert summary_line.startswith("- **"), f"the retention line is not a top-level bullet: {summary_line!r}"

    # (4) R4.2 literally: the heading the retained finding sits beneath asserts
    # nothing about blocking. The report's blocking assertion is the Overall
    # Status line, which is above this block and is not its heading.
    heading = block.splitlines()[0]
    assert heading == _STATIC_SUMMARY_HEADING
    assert "BLOCKING" not in heading and "blocked" not in heading, (
        f"the retained finding is presented beneath a heading that asserts static analysis blocked: {heading!r}"
    )


# ── p44 R4.5/R5.2/R5.4/R9.6 (task 12.4) — the two new frame arms, as examples ──
#
# The synthetic arm has had a named example in this file since p42
# (`test_the_glue_caveat_claims_spark_semantics_and_denies_real_source_data`): one
# report, rendered, with the text a reviewer actually reads asserted as text.
# These are its counterparts for the arms p44 added — `sample`,
# `sample_unavailable`, and the sample run whose read failed — plus R4.5's second
# half, which has no property at all.
#
# WHY THESE ARE EXAMPLES AND NOT MORE PROPERTY CELLS.
# `test_frame_provenance_report_prop.py` (Property 6) already quantifies the frame
# dimension over frame x verdict, and it derives every expectation from a shipped
# constant — which is right for a universal and is why a failure there reads
# "outcome X x branch Y: the report carries GLUE_SPARK_SAMPLE_CAVEAT as well as
# GLUE_SPARK_CAVEAT". What it cannot do is show a reader the sentence. These four
# assert the rendered phrases, so a reword that Property 6 reports as a
# constant-mismatch is diagnosable here as "the caveat no longer says it read the
# pipeline's own object". Nothing here re-derives Property 6's universals:
# position is Property 7's (`test_frame_provenance_excerpt_prop.py`), and no
# verdict is asserted except where the verdict is itself the observation (see
# `test_the_sample_read_failure_report_states_nothing_about_an_execution`).
#
# Fixtures come from the PRODUCER — `FrameSource.provenance()` and the shipped
# `sample_read_failure_results` — for the reason `_refusal_results` above gives:
# a fixture transcribed on the reader's side cannot detect the two sides drifting,
# because the output is its own oracle (`LGR-28b4d75-5`, **template / ratified**).
# The row bound is read off the provenance rather than spelled `100`, so it cannot
# disagree with `glue_session_runner._SAMPLE_FRAME_ROWS`; that constant's own value
# is pinned by task 5.2's `test_frame_provenance.py`, not re-pinned here.
#
# NO SESSION IS CREATED, MOCKED OR OTHERWISE (do-NOT 1). The report builder and
# both producers are pure functions of their arguments.

from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    STAGE_FRAME_BUILD,
    FrameSource,
    StatementOutcome,
    sample_read_failure_results,
    sample_read_format,
)

#: A `sourceBucket` object URI of the shape `file_validation` writes
#: (`file_validation/lambda_function.py:632`), which is the only source shape whose
#: real read is REQUIRED rather than best-effort (R4.1).
_SOURCE_URI = "s3://adi-source-files-123456789012-us-west-2/uploads/orders.csv"

#: The URI a JDBC-sourced pipeline carries — `schema_extract`'s JDBC branch writes
#: `jdbc://{engine}/{schema}/{table}` (`:746-747`). Not an edge case: with the
#: Frame_Mode variable set globally this is the ORDINARY path for every
#: JDBC-sourced pipeline (design §4.10).
_JDBC_URI = "jdbc://postgres/public/orders"

#: The words a report would have to use to claim anything about a watermark-based
#: incremental read. R4.5's second half is an absence, so it needs a vocabulary to
#: be an absence *of* — see
#: `test_a_watermark_sourced_jdbc_run_claims_no_verification_of_the_watermark_predicate`.
_WATERMARK_VOCABULARY = ("watermark", "high-water", "incremental", "predicate")

#: Every phrase in the report that CLAIMS verification against the pipeline's own
#: source data (R5.5). Two live in the caveat and the scope tail, two in the
#: `overall_status` parenthetical and the PRODUCTION READY decision clause. Only a
#: `sample` run that verified something may carry any of them, so each example
#: below that is not one asserts all four absent.
_OWN_SOURCE_CLAIMS = (
    "real Spark semantics over real source data",
    "and behaviour on those sampled rows of the pipeline's own source object",
    "(real source sample,",
    "so is behaviour on those sampled rows",
)


def _sample_provenance(uri: str = _SOURCE_URI) -> dict:
    """A Sample_Frame's provenance, built by the shipped producer."""
    read_format = sample_read_format(uri)
    assert read_format is not None, f"{uri} resolves no reader, so this fixture is not a sample"
    frame_source = FrameSource(
        mode=FRAME_MODE_SAMPLE, requested_mode=FRAME_MODE_SAMPLE, source_uri=uri, read_format=read_format
    )
    return frame_source.provenance()


def _sample_unavailable_provenance(uri: str = _JDBC_URI, reason: str = SAMPLE_UNAVAILABLE_NOT_S3_SOURCE) -> dict:
    """A requested-and-refused sample's provenance.

    `mode` is synthetic while `requested_mode` is sample, which is exactly what
    `resolve_frame_source`'s non-`s3://` branch returns. `FrameSource.__post_init__`
    refuses the inconsistent shapes, so this fixture cannot describe a run the
    runner could not produce.
    """
    frame_source = FrameSource(
        mode=FRAME_MODE_SYNTHETIC, requested_mode=FRAME_MODE_SAMPLE, source_uri=uri, unavailable_reason=reason
    )
    return frame_source.provenance()


def _read_failure_results(uri: str = _SOURCE_URI) -> dict:
    """A real harness non-verification for a failed source read (R2.1).

    Built by `sample_read_failure_results`, so the marker, the `executed` flag and
    the `error_details` sentence are whatever the module actually emits.
    Hand-building this dict would let R2.5's suppression be asserted against a
    shape no producer creates — which is how the defect task 12.5 fixed survived
    p42, and this file's total absence of `not_verified_by_harness` coverage before
    now is the other half of that story.

    The statement is an `AccessDenied` on the object, which is the failure C11
    predicts for the whole deploy window in which Compute lands before Foundation.
    """
    outcome = StatementOutcome(
        stage=STAGE_FRAME_BUILD,
        state="ERROR",
        output_status="error",
        stdout="",
        statement={
            "Output": {
                "Status": "error",
                "ErrorName": "AccessDeniedException",
                "ErrorValue": "An error occurred (403) when calling the HeadObject operation: Forbidden",
                "Traceback": [],
            }
        },
    )
    read_format = sample_read_format(uri)
    frame_source = FrameSource(
        mode=FRAME_MODE_SAMPLE, requested_mode=FRAME_MODE_SAMPLE, source_uri=uri, read_format=read_format
    )
    return sample_read_failure_results(outcome, frame_source)


def _glue_report(execution: dict, provenance: dict | None, *, recommendation: str = "APPROVE") -> str:
    """One Glue report for the given execution result and frame provenance."""
    return generate_comprehensive_test_report(
        "static",
        execution,
        _parsed(_ALL_STATIC_PASS, recommendation=recommendation),
        executor=GLUE,
        frame_provenance=provenance,
    )


def _scope_text(report: str) -> str:
    """The `> **Scope of this check:**` blockquote, whole.

    Sliced out so a failure message shows the sentence a reviewer reads rather than
    a 3000-character report. The block is three lines on the Glue arm — the caveat,
    then the two-line tail — and ends at the first line that is not a blockquote.
    """
    lines = report.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("> **Scope of this check:**")), None)
    assert start is not None, f"no scope block in the report: {report[:300]!r}"
    end = start
    while end < len(lines) and lines[end].startswith(">"):
        end += 1
    return "\n".join(lines[start:end])


def test_the_sample_frame_report_states_the_bounded_real_source_read():
    """R5.2 — the one arm permitted to claim the pipeline's own data, as text.

    The mirror of `test_the_glue_caveat_claims_spark_semantics_and_denies_real_
    source_data`, which asserts the SYNTHETIC arm's four phrases. This asserts the
    sample arm's, and the two are inverses on exactly one clause: the synthetic
    report says "not the pipeline's own source data", this one says the transform
    ran over a bounded sample OF that object. Both still deny the full production
    Glue job, because neither is one.

    R5.2 asks for two things and both are asserted here: that the report state the
    run was measured against a bounded sample of the pipeline's own source object,
    and that it NAME the row bound. The bound is read off the provenance rather
    than spelled, so a change to `_SAMPLE_FRAME_ROWS` moves this expectation with
    it instead of failing here for the wrong reason.
    """
    provenance = _sample_provenance()
    row_bound = provenance["row_bound"]
    report = _glue_report(_EXEC_OK, provenance)
    scope = _scope_text(report)

    # The caveat, as the sentence a reviewer reads.
    assert f"**bounded {row_bound}-row sample of this pipeline's own source object**" in scope, (
        f"the sample caveat does not state the bounded read of the pipeline's own object:\n{scope}"
    )
    assert "real Spark semantics over real source data" in scope, (
        f"the sample caveat does not claim real source data, which is the whole point of the arm:\n{scope}"
    )
    assert "not the full production Glue job" in scope, (
        f"the sample caveat dropped the limit it shares with the synthetic arm — a sample read is "
        f"still not the production job:\n{scope}"
    )

    # ...and the sentence after it agrees with it. A report whose caveat claimed
    # real data while the next sentence denied it is the p42 defect in miniature.
    assert "and behaviour on those sampled rows of the pipeline's own source object" in scope, (
        f"the scope tail still denies real-source verification on a run that had it:\n{scope}"
    )
    assert "the rest of that object is not" in scope, (
        f"the scope tail claims the whole object was verified, not the sampled rows:\n{scope}"
    )

    # The synthetic arm's wording is gone, not merely joined.
    assert "synthetic frame built from the declared source schema" not in report, (
        "a sample-frame report still calls its input a synthetic frame"
    )
    assert "and behaviour on the pipeline's real source data is\n> not verified by it" not in report

    # The other two carriers, on the branch that renders them.
    assert (
        f"**Overall Status:** PASSED - STATIC ANALYSIS + REAL SPARK EXECUTION (real source sample, {row_bound} rows)"
        in report
    )
    decision = next(line for line in report.splitlines() if "**Deployment Decision:**" in line)
    assert f"**bounded {row_bound}-row sample of this pipeline's own source object**" in decision
    assert "so is behaviour on those sampled rows" in decision
    assert "the rest of that object is not" in decision
    assert "pandas" not in decision


def test_the_sample_unavailable_report_names_the_reason_and_withholds_the_own_source_claim():
    """R5.4 — a sample was ASKED FOR and refused, which is not the same as never asking.

    Both halves, because either alone would be a different report. The report has
    to carry the synthetic arm's claim — the frame was generated, real source
    behaviour is unverified — AND the fact that a sample was requested plus the
    recorded reason it was not built, because only the second tells a reader to go
    and look at the source object.

    **It carries no non-verification marker, and that is C9 / do-NOT 7 rather than
    an omission.** A fallback to synthetic is a fully verified run against a
    generated frame — exactly what `frame_mode="synthetic"` produces on purpose —
    so the execution facts are still reported and no "not verified" wording
    appears. That is asserted here because it is the boundary this arm shares with
    the read-failure arm below, and the two are one keystroke apart in the builder.
    """
    provenance = _sample_unavailable_provenance()
    report = _glue_report(_EXEC_OK, provenance)
    scope = _scope_text(report)

    assert f"a real-source sample was requested and could not be read ({SAMPLE_UNAVAILABLE_NOT_S3_SOURCE})" in scope, (
        f"the report does not state that a sample was requested, or does not name the recorded "
        f"reason it was not built:\n{scope}"
    )
    assert "reason not recorded" not in report, (
        "the builder rendered its missing-reason placeholder over a provenance that carried one"
    )

    # The synthetic claim is carried, unchanged — this run got a generated frame.
    assert "**synthetic frame built from the declared source schema**" in scope, (
        f"a requested-but-unavailable sample no longer says its frame was synthetic:\n{scope}"
    )
    assert "not the pipeline's own source data" in scope
    assert "and behaviour on the pipeline's real source data is" in scope

    # R5.5: none of the four own-source claims may appear on this arm.
    present = [claim for claim in _OWN_SOURCE_CLAIMS if claim in report]
    assert not present, (
        f"a run whose sample could not be read claims verification against the pipeline's own source data: {present}"
    )

    # The status parenthetical distinguishes it from a run that never asked. The
    # brackets are load-bearing: `synthetic input data` is a PREFIX of this note,
    # so a bare substring test cannot tell the two frames apart.
    assert "(synthetic input data — a sample was requested and unavailable)" in report
    assert "(synthetic input data)" not in report, (
        "the report carries the never-asked parenthetical on a run that asked and was refused"
    )

    # C9 / do-NOT 7: a verified run. Execution facts present, no harness wording.
    assert "**Execution Status:** ✅ SUCCESS" in report
    assert "**Records Processed:** 100 → 100 records" in report
    assert "NOT VERIFIED" not in report, (
        "a fallback to synthetic carries a non-verification marker, which reports "
        "nothing-was-verified about a run that verified something (C9, do-NOT 7)"
    )
    assert "Not Verified by the Test Harness" not in report


def test_a_watermark_sourced_jdbc_run_claims_no_verification_of_the_watermark_predicate():
    """R4.5's second half — the one rendering fact in this spec with no property.

    A JDBC watermark-based incremental read reaches the sample-unavailable arm by
    the same route a full-table read does, and for the same reason: the URI
    `schema_extract` writes is a `jdbc://` one, `is_s3_source_uri` answers False,
    and `SAMPLE_UNAVAILABLE_NOT_S3_SOURCE` is recorded. The watermark predicate
    itself never reaches the runner at all — it lives in the pipeline
    configuration, and `run_test`'s three arguments are the transform block and two
    schemas (C6) — so there is nothing the harness could have evaluated even in
    principle. `LGR-819980f-1` (**standing invariant**) is why that stays true: a
    JDBC read from a verification session would need credentials, and credentials
    reach Glue only by Secrets Manager ARN, which a Verification_Session holds none
    of.

    So R4.5 asks the report to claim no verification OF the predicate, and the
    honest form of that claim is an absence plus a positive denial:

    * the report names no watermark vocabulary at all, so no sentence in it can be
      read as having checked the predicate; and
    * it states positively that the frame was synthetic and that behaviour on real
      source data is not verified — which covers the predicate a fortiori, because
      the predicate is only observable against real rows.

    The absence is not vacuous: the positive assertions below are what a report
    would have to keep while adding a false watermark claim, and this test fails
    the moment one is added.
    """
    provenance = _sample_unavailable_provenance(_JDBC_URI)
    report = _glue_report(_EXEC_OK, provenance)
    scope = _scope_text(report)

    # The premise: a JDBC URI reaches the requested-and-refused arm, with the
    # reason the runner records for a non-`s3://` source.
    assert provenance["frame"] == "sample_unavailable", (
        f"a jdbc:// URI no longer produces the requested-and-refused provenance: {provenance!r}"
    )
    assert provenance["unavailable_reason"] == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
    assert f"could not be read ({SAMPLE_UNAVAILABLE_NOT_S3_SOURCE})" in scope, (
        f"the report does not name the JDBC refusal reason:\n{scope}"
    )

    # The positive denial, which is what makes the absence below meaningful.
    assert "**synthetic frame built from the declared source schema**" in scope
    assert "not the pipeline's own source data" in scope
    decision = next(line for line in report.splitlines() if "**Deployment Decision:**" in line)
    assert "behaviour on real source data is not" in decision, (
        f"the deployment decision does not withhold the own-source claim on a JDBC run:\n{decision}"
    )
    assert not [claim for claim in _OWN_SOURCE_CLAIMS if claim in report]

    # R4.5: nothing in the report claims anything about the watermark predicate.
    named = [word for word in _WATERMARK_VOCABULARY if word in report.lower()]
    assert not named, (
        f"the report names {named} on a run in which the watermark predicate was never evaluated — "
        f"the frame was synthetic and the source was never read, so any sentence about the "
        f"predicate is a claim the run cannot support (R4.5)."
    )


def test_the_sample_read_failure_report_states_nothing_about_an_execution():
    """R2.5 — the sample frame whose read failed, which may claim nothing at all.

    **The first `not_verified_by_harness` example in this file, and its absence is
    why p42 shipped past the defect.** Task 12.5 found it by rendering a real
    `sample_read_failure_results` through the builder: the report stated
    `**Execution Status:** ❌ FAILED`, `**Records Processed:** 0 → 0 records` and an
    `### ❌ Execution Error Details` block for a transform that was never submitted.
    All three are forbidden by R2.5 and all three are asserted absent below.

    Two things this arm must get right that the frame alone cannot decide, because
    `frame_provenance["frame"]` is `"sample"` on this run just as it is on a
    successful one:

    * the caveat claims NOTHING — not Spark semantics, not real source data; and
    * "not verified" is not implemented as "not reported" (p32 2.20): the reason id,
      its label and the object whose read failed are all named.

    **Its own words, not the proxy's.** Routing this through
    `⏸️ Not Verified by the Pandas Proxy` would put a pandas phrase on a Glue run,
    which is the misattribution p42 and p44 both exist to remove, and the arm is
    reachable on the pandas executor too (p33's two width reasons). Asserted here
    because this file is where the proxy arm's own wording is pinned
    (`test_the_refusal_arms_keep_their_pandas_wording`).

    **The verdict is deliberately UNCHANGED and is asserted as current behaviour.**
    A read failure still reaches `❌ **NOT PRODUCTION READY**` and
    `FAILED - EXECUTION ISSUES DETECTED`, because `execution_successful` is False
    and `LGR-334667e-1`'s matrix takes that as its input. Task 12.5's relaxation
    covered the execution-status, record-count and error-block branch and nothing
    else, so the verdict arms keep their bytes. A reader may reasonably think a
    harness abstention should not read as a rejected transform at the top of the
    report — that is a real question, and it is p43's territory (R9.5, do-NOT 11),
    not something to change inside a test task. Pinned rather than left implicit,
    per `LGR-60c4985-6`: a deliberate hold is recorded at the assertion so a later
    reversal reads as a reversal.
    """
    results = _read_failure_results()
    provenance = _sample_provenance()
    row_bound = provenance["row_bound"]
    report = _glue_report(results, provenance)
    scope = _scope_text(report)

    # The premise, from the producer: a harness non-verification, nothing executed.
    assert results["not_verified_by_harness"] is True
    assert results["executed"] is False
    assert results["not_verified_reason"] == _mod.SOURCE_READ_FAILED_REASON

    # (a) The caveat claims nothing.
    assert f"the frame was to be a **bounded {row_bound}-row sample of this pipeline's own source object**" in scope, (
        f"the read-failure caveat does not say what the frame was going to be:\n{scope}"
    )
    assert "that read did not succeed" in scope, f"the caveat does not say the read failed:\n{scope}"
    assert "the transform **never ran**" in scope and "**nothing about it was verified**" in scope, (
        f"the caveat does not withhold every claim about the transform:\n{scope}"
    )
    present = [claim for claim in _OWN_SOURCE_CLAIMS if claim in report]
    assert not present, (
        f"a run whose source read FAILED claims verification against the pipeline's own source "
        f"data: {present}. This is the cell the frame alone gets wrong — the frame was a sample, "
        f"the read of it is what failed, and nothing was verified (R5.5, task 12.5)."
    )

    # (b) R2.5's three suppressed facts.
    for forbidden in ("**Execution Status:**", "**Records Processed:**", "### ❌ Execution Error Details"):
        assert forbidden not in report, (
            f"the report states {forbidden!r} for a transform that was never submitted (R2.5)"
        )
    assert "### Data Quality Assessment" not in report

    # (c) ...and states the non-verification instead: p32 2.20, unchanged.
    assert "### ⏸️ Not Verified by the Test Harness" in report
    assert f"**What the harness could not do (`{_mod.SOURCE_READ_FAILED_REASON}`):**" in report
    # `.get`, not `[...]`: a missing entry is the failure task 4.1's entry exists to
    # prevent, and it should read as a sentence rather than as a KeyError inside an
    # assertion. `test_not_verified_reason_labels_cross_language.py` is what makes
    # the entry mandatory; this is what says which sentence it puts in the report.
    label = _mod.NOT_VERIFIED_REASON_LABELS.get(_mod.SOURCE_READ_FAILED_REASON)
    assert label, (
        f"NOT_VERIFIED_REASON_LABELS carries no entry for {_mod.SOURCE_READ_FAILED_REASON!r}, so the "
        f"harness section degrades to a bare reason id — p33's two harness reasons shipped unlabelled "
        f"for a release, which is why that map is asserted at all"
    )
    assert label in report, (
        "the harness arm names the reason id but not what it means — the label map is what keeps a "
        "new reason from degrading this section to a bare id"
    )
    assert _SOURCE_URI in report, "the report does not name the object whose read failed"

    # (d) Neither executor's refusal vocabulary is borrowed.
    assert "NOT VERIFIED BY THE PANDAS PROXY" not in report
    assert "Not Verified by the Pandas Proxy" not in report
    assert PANDAS_PROXY_CAVEAT not in report
    assert "pandas" not in scope

    # (e) The verdict, unchanged and deliberately so. See the docstring.
    assert "❌ **NOT PRODUCTION READY**" in report
    assert "**Overall Status:** FAILED - EXECUTION ISSUES DETECTED" in report


def test_the_pandas_caveat_is_untouched_by_the_frame_dimension():
    """R9.6 / do-NOT 6 — the pandas caveat still reaches every pandas run, unchanged.

    Two claims, and the second is the one the frame dimension made possible.

    **It still reaches every pandas run.** Asserted here over the same five verdict
    branches `test_every_verdict_branch_names_the_pandas_approximation` uses, but
    with a *sample* provenance passed on every call — which is malformed input, and
    malformed input is exactly what must not perturb it. `CodeInterpreterTestRunner`
    has no frame source at all (`code_interpreter_runner.py:223`: `region` in, one
    `bedrock-agentcore` client), so the handler passes `None` there; a caller that
    passed one anyway must get a byte-identical report. Byte equality rather than
    substring presence, because "unchanged" is the requirement and a report that
    grew a frame sentence while keeping the caveat would satisfy the weaker form.
    That holds because the frame is read on the `glue` arm only — two mappings
    composed, not one re-keyed on `(executor, frame)`.

    **And no new arm introduces it.** `_GLUE_FRAME_CAVEATS` gained two arms and
    task 12.5 added a third string beside them; do-NOT 6 forbids deleting the
    pandas caveat, and the inverse obligation is that none of the new arms may
    quietly reproduce it. Asserted on the constants — so a reword is caught at the
    source rather than only where it happens to render — and then on the rendered
    reports for all three new arms.
    """
    args = ("static", _EXEC_OK, _parsed(_ALL_STATIC_PASS, recommendation="APPROVE"))
    sample = _sample_provenance()

    for executor in (PANDAS, SHADOW, "Glue"):
        for branch, execution, parsed in (
            ("production_ready", _EXEC_OK, _parsed(_ALL_STATIC_PASS, recommendation="APPROVE")),
            ("approve_with_fixes", _EXEC_OK, _parsed({**_ALL_STATIC_PASS, "Performance": False})),
            ("review_required", _EXEC_OK, _parsed({**_ALL_STATIC_PASS, "Security": False})),
            (
                "not_production_ready",
                {"execution_successful": False, "error_details": "boom"},
                _parsed(_ALL_STATIC_PASS),
            ),
            ("refusal", _NOT_YET, _parsed(_ALL_STATIC_PASS)),
        ):
            without = generate_comprehensive_test_report("static", execution, parsed, executor=executor)
            with_frame = generate_comprehensive_test_report(
                "static", execution, parsed, executor=executor, frame_provenance=sample
            )

            assert PANDAS_PROXY_CAVEAT in without, f"{executor}/{branch}: the pandas caveat no longer reaches this run"
            assert with_frame == without, (
                f"{executor}/{branch}: a frame provenance changed a report for an executor that has no "
                f"frame source. The frame dimension applies on the glue arm only (R9.6)."
            )

    # The legacy no-`parsed_results` call, which takes a different path entirely.
    assert PANDAS_PROXY_CAVEAT in generate_comprehensive_test_report("static", _EXEC_OK, executor=PANDAS)
    assert generate_comprehensive_test_report(*args, executor=PANDAS, frame_provenance=sample) == (
        generate_comprehensive_test_report(*args, executor=PANDAS)
    )

    # No new arm reproduces the pandas wording — on the constants first.
    new_arms = {
        "GLUE_SPARK_SAMPLE_CAVEAT": _mod.GLUE_SPARK_SAMPLE_CAVEAT,
        "GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT": _mod.GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT,
        "GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT": _mod.GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT,
    }
    for name, caveat in new_arms.items():
        assert PANDAS_PROXY_CAVEAT not in caveat, f"{name} contains the pandas caveat verbatim"
        for forbidden in ("pandas", "generated sample data", "Code Interpreter"):
            assert forbidden not in caveat, f"{name} says {forbidden!r} — a Glue arm may not name the proxy"

    # ...and then where they render, which is what a reviewer would see.
    for label, execution, provenance in (
        ("sample", _EXEC_OK, _sample_provenance()),
        ("sample_unavailable", _EXEC_OK, _sample_unavailable_provenance()),
        ("sample_read_failed", _read_failure_results(), _sample_provenance()),
    ):
        report = _glue_report(execution, provenance)
        assert PANDAS_PROXY_CAVEAT not in report, f"{label}: a Glue run carries the pandas caveat"
        assert "pandas approximation" not in report, f"{label}: calls the run a pandas approximation"
        assert "generated sample data" not in report, f"{label}: calls the input generated sample data"
