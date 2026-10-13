# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: consumption rewiring of the calibrated review gate (P28b R1b, task 2.2).

Every verdict-derived decision in the test_code Lambda now consumes
``effective_verdict`` (the Issue_Attribution-calibrated gate) instead of the
raw verdict:

  * ``aggregate_check_results`` — the ``ai_review_verdict`` check passes on
    an effective pass; its detail surfaces the raw verdict, the effective
    verdict, per-attribution issue counts, and the issue summaries, so a
    scaffolding-only pass shows its RETAINED issues in the checks list, the
    comprehensive report, and the approval payload (R1.3).
  * ``analyze_test_results_for_regeneration`` — the trigger is an effective
    fail; bounds and feedback context are unchanged (R1.6).
  * ``determine_code_approval_requirement`` — same substitution.
  * ``verdict_to_parsed_results`` — ``recommendation`` derives from the
    effective verdict.

No token-presence or keyword-count logic exists anywhere in the gate (R1.7).

**Validates: Requirements 1.3, 1.4, 1.5, 1.6, 1.7, 1.8**

EXTENDED, not replaced, by spec ``real-spark-verification`` (p42) task 11.1
-------------------------------------------------------------------------

The final section of this file adds **Property 17** — *a successful execution
never excuses a blocking static finding* — validating **p42 Requirement 12.1**.
Everything above it is untouched: p42 changes the *executor*, and R12 exists
because an executor change is the most plausible way to weaken this gate by
accident, so the invariance is stated as a property rather than left implicit.

This file is named in ``LGR-60c4985-6``'s scope (**template / ratified**), whose
discipline is that an amendment here "must stay legible as a reversal, not read
as a regression". Nothing is reversed by task 11.1 — the addition is purely
additive, and the record's third hold assertion
(``test_handler_transform_block_fail_still_fails_the_gate``) is left exactly as
it stands.

RECALIBRATED by spec ``p43-review-gate-severity-floor``
-------------------------------------------------------

Rule 1 now carries a severity floor: a Reviewer_Transform_Block_Finding blocks
the gate at ``critical``, ``major`` or any unrecognised severity, and is
**retained but not blocking** at a severity in
``review_verdict.NON_BLOCKING_SEVERITIES`` (p43 R1.1/R1.2/R1.3/R1.6, calibrating
P28b R1.4 rather than removing it). Assertions in this file that pinned rule 1's
severity-blindness are **amended, never deleted**, each stating what reversed at
its own site, under ``LGR-60c4985-6``'s discipline and ``LGR-5906ec6-2``'s
convention that a deliberate hold is encoded as a test assertion. The hold being
reversed is the one stated in the section header before Property 1 (the paragraph
that pinned the severity axis as current behaviour belonging to a different
spec — this is that spec). Property 17 survives as **p43 Property 1** with a
narrowed antecedent, joined by **Property 2** over the released half of the space
(p43 R7.2).
"""

import inspect
import json
import os
import string
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Region/account before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy / AWS-touching imports so the module loads in isolation.
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

import review_verdict as _rv
from review_verdict import effective_verdict, parse_review_verdict, verdict_to_parsed_results

# p42 task 11.1 — the declared Test_Result_Contract, used as the shape oracle for
# the generated execution results in the Property 17 section at the end of this
# file. Imported from the fixture rather than restated, per `LGR-28b4d75-5`
# (template): "a test written from the producer's output cannot detect the
# producer drifting, because the output is its own oracle."
from tests.fixtures.run_test_result_shapes import BASE_CONTRACT_KEYS, SUCCESS, shape_violations


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _parsed_from(verdict: str, issues: list[dict], categories: dict[str, str] | None = None) -> dict:
    """Canonical parsed_results via the REAL parser + adapter (no hand-built shapes)."""
    doc = {"verdict": verdict, "issues": issues, "categories": categories or {}}
    return verdict_to_parsed_results(parse_review_verdict(json.dumps(doc)))


def _issue(description="an issue", severity="minor", **fields) -> dict:
    return {"severity": severity, "description": description, **fields}


_EXEC_OK = {"execution_successful": True, "input_records": 10, "output_records": 10}
_NO_REGEN = {"regeneration_needed": False, "max_attempts_reached": False}

_SCAFFOLDING_ONLY_FAIL_ISSUES = [
    _issue("Glue boilerplate re-creates the SparkSession.", "major", attribution="template_scaffolding"),
    _issue("getResolvedOptions lacks a default for JOB_NAME.", "minor", attribution="template_scaffolding"),
]


# ---------------------------------------------------------------------------
# aggregate_check_results — the ai_review_verdict check is effective-verdict
# driven; scaffolding issues stay visible in the check detail (R1.3)
# ---------------------------------------------------------------------------


def test_scaffolding_only_fail_verdict_passes_aggregation_with_issues_retained():
    """**Validates: Requirements 1.3** — a raw fail whose every issue is
    explicitly scaffolding-attributed passes the gate, and the retained
    issues stay visible in the check detail."""
    parsed = _parsed_from("fail", _SCAFFOLDING_ONLY_FAIL_ISSUES, {"Correctness": "pass", "Security": "pass"})
    status, checks = _mod.aggregate_check_results(parsed, _EXEC_OK)

    assert status == "passed"
    verdict_check = next(c for c in checks if c["name"] == "ai_review_verdict")
    assert verdict_check["passed"] is True
    # Detail surfaces raw verdict, effective verdict, per-attribution counts,
    # AND the retained issue text — visible, not discarded.
    assert "Reviewer verdict: fail" in verdict_check["detail"]
    assert "effective verdict: pass" in verdict_check["detail"]
    assert "0 transform_block, 2 template_scaffolding" in verdict_check["detail"]
    assert "Glue boilerplate re-creates the SparkSession." in verdict_check["detail"]
    assert "getResolvedOptions lacks a default for JOB_NAME." in verdict_check["detail"]


@pytest.mark.parametrize("severity", ["critical", "major"])
def test_explicit_transform_block_issue_fails_aggregation_any_severity(severity):
    """**Validates: Requirements 1.4** — an explicit reviewer transform-block
    finding fails the check regardless of severity, even on a raw pass.

    **What reversed, and on whose authority.** The parametrization was
    ``["critical", "major", "minor"]``, and "any severity" in the name meant
    literally any: rule 1 read no severity at all. Under p43 R1.1/R1.2 it means
    **any Blocking_Severity** — any token not in
    ``review_verdict.NON_BLOCKING_SEVERITIES`` — so the ``minor`` arm is not
    deleted, it moves to
    :func:`test_retained_transform_block_issue_passes_aggregation_at_non_blocking_severity`
    immediately below and asserts the opposite outcome (p43 R3.1). Every
    assertion here stands verbatim on the narrower draw. Authority:
    ``LGR-60c4985-6`` (**template / ratified**) — amend the assertions that
    encoded the hold, never delete them, and keep the reversal legible as a
    reversal rather than read as a regression; citing ``LGR-5906ec6-2``
    (**template / ratified**) for this repo's convention that a deliberate hold
    is encoded as a test assertion. The hold this reverses is stated in the
    section header "Why the property is about the ATTRIBUTION and the severity
    FLOOR", below.
    """
    parsed = _parsed_from("pass", [_issue("Transform drops null rows.", severity, attribution="transform_block")])
    status, checks = _mod.aggregate_check_results(parsed, _EXEC_OK)

    assert status == "failed"
    verdict_check = next(c for c in checks if c["name"] == "ai_review_verdict")
    assert verdict_check["passed"] is False
    assert "effective verdict: fail" in verdict_check["detail"]
    assert "1 transform_block, 0 template_scaffolding" in verdict_check["detail"]


@pytest.mark.parametrize("severity", sorted(_rv.NON_BLOCKING_SEVERITIES))
def test_retained_transform_block_issue_passes_aggregation_at_non_blocking_severity(severity):
    """**Validates: Requirements 3.1** — the released half of the arm above: a
    reviewer transform-block finding at a Non_Blocking_Severity does NOT fail
    the check on a raw pass, and stays visible in the detail.

    The severities are read off ``review_verdict.NON_BLOCKING_SEVERITIES`` rather
    than restated, so this cannot drift from the gate's own vocabulary if the set
    ever grows a second tier — the same discipline
    :data:`_NON_BLOCKING_SEVERITY` follows for Property 2's generator.

    **What reversed, and on whose authority.** This is the ``minor`` arm of
    ``test_explicit_transform_block_issue_fails_aggregation_any_severity``,
    amended rather than deleted under ``LGR-60c4985-6`` (**template /
    ratified**), whose discipline is that the reversal must stay legible as a
    reversal; ``LGR-5906ec6-2`` (**template / ratified**) is why the hold lived
    in an assertion in the first place. The hold being reversed is stated in the
    section header "Why the property is about the ATTRIBUTION and the severity
    FLOOR", below. p43 R1.1/R1.2 put the severity floor inside rule 1's
    condition; R1.3/R1.6 make this finding retained-but-not-blocking.

    Non-vacuity: the attribution antecedent is asserted, not assumed. A ``pass``
    here is also what a *defaulted* attribution produces on a raw pass
    (``review_verdict.py:220-222`` → rule 4), so without the
    ``attribution_source == "reviewer"`` assertion a misspelled ``attribution``
    key in the fixture would pass this test for entirely the wrong reason — the
    same trap ``test_the_blocking_finding_generator_supplies_reviewer_attribution``
    exists to catch for Property 1.
    """
    parsed = _parsed_from("pass", [_issue("Transform drops null rows.", severity, attribution="transform_block")])
    assert [i["attribution_source"] for i in parsed["issues"]] == [_rv.ATTRIBUTION_SOURCE_REVIEWER]

    status, checks = _mod.aggregate_check_results(parsed, _EXEC_OK)

    assert status == "passed"
    verdict_check = next(c for c in checks if c["name"] == "ai_review_verdict")
    assert verdict_check["passed"] is True
    assert "effective verdict: pass" in verdict_check["detail"]
    # Retained, not discarded (R3.5): still counted under its own attribution
    # and still displayed, exactly as the scaffolding-only pass above.
    assert "1 transform_block, 0 template_scaffolding" in verdict_check["detail"]
    assert "Transform drops null rows." in verdict_check["detail"]
    # The retained COUNT, stated as text distinct from the per-attribution counts
    # (p43 R3.6). Landed by task 10.1; this assertion was deferred out of 6.1
    # because the producer had not been written yet. Property 7 (task 10.2) is
    # what pins the same number across all three consumers — this arm pins only
    # that the check detail carries it.
    assert "1 retained as non-blocking" in verdict_check["detail"]


def test_unattributed_issues_on_fail_verdict_keep_aggregation_failing():
    """**Validates: Requirements 1.5** — the conservative default: a raw fail
    with any issue not explicitly scaffolding-attributed stays a fail."""
    parsed = _parsed_from("fail", [_issue("Unattributed finding.", "major")])
    status, checks = _mod.aggregate_check_results(parsed, _EXEC_OK)

    assert status == "failed"
    verdict_check = next(c for c in checks if c["name"] == "ai_review_verdict")
    assert verdict_check["passed"] is False
    assert "Reviewer verdict: fail" in verdict_check["detail"]
    assert "effective verdict: fail" in verdict_check["detail"]


def test_mixed_scaffolding_and_unattributed_issues_on_fail_stay_failing():
    """**Validates: Requirements 1.5** — exemption requires explicit
    scaffolding attribution on EVERY issue."""
    parsed = _parsed_from(
        "fail",
        [
            _issue("Scaffolding nit.", "minor", attribution="template_scaffolding"),
            _issue("Unattributed finding.", "minor"),
        ],
    )
    status, checks = _mod.aggregate_check_results(parsed, _EXEC_OK)

    assert status == "failed"
    assert next(c for c in checks if c["name"] == "ai_review_verdict")["passed"] is False


# ---------------------------------------------------------------------------
# analyze_test_results_for_regeneration — trigger is the effective verdict;
# bounds and feedback context unchanged (R1.6)
# ---------------------------------------------------------------------------


def test_scaffolding_only_fail_does_not_trigger_regeneration():
    """**Validates: Requirements 1.3, 1.6**"""
    verdict = parse_review_verdict(
        json.dumps({"verdict": "fail", "issues": _SCAFFOLDING_ONLY_FAIL_ISSUES, "categories": {}})
    )
    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="passed", checks_run=4, checks_passed=4, review_verdict=verdict, regeneration_attempt=0
    )
    assert analysis["regeneration_needed"] is False
    assert analysis["max_attempts_reached"] is False
    # The bound machinery is untouched (R1.6): config default 2, recorded.
    assert analysis["max_attempts"] == 2
    assert analysis["regeneration_attempt"] == 0


@pytest.mark.parametrize("severity", ["critical", "major"])
def test_explicit_transform_block_issue_triggers_regeneration_any_severity(severity):
    """**Validates: Requirements 1.4, 1.6** — an effective fail (explicit
    transform-block finding, even on a raw pass) triggers regeneration, and
    the feedback context carries the reviewer's issues unchanged.

    **What reversed, and on whose authority.** As for
    ``test_explicit_transform_block_issue_fails_aggregation_any_severity`` above:
    the parametrization was ``["critical", "major", "minor"]`` when rule 1 read no
    severity, and "any severity" now means any Blocking_Severity (p43 R1.1/R1.2).
    The ``minor`` arm is not deleted — it moves to
    :func:`test_retained_transform_block_issue_triggers_no_regeneration_at_non_blocking_severity`
    below, which asserts the opposite trigger outcome *and* that the finding is
    still carried in the feedback context (p43 R3.2/R3.5). Authority:
    ``LGR-60c4985-6`` (**template / ratified**) — amend, never delete, and keep
    the reversal legible as a reversal; citing ``LGR-5906ec6-2`` (**template /
    ratified**) for the convention that a deliberate hold is a test assertion.
    The hold this reverses is stated in the section header "Why the property is
    about the ATTRIBUTION and the severity FLOOR", below.
    """
    verdict = parse_review_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "issues": [_issue("Transform drops null rows.", severity, attribution="transform_block")],
                "categories": {},
            }
        )
    )
    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="passed", checks_run=4, checks_passed=4, review_verdict=verdict, regeneration_attempt=0
    )
    assert analysis["regeneration_needed"] is True
    feedback = analysis["feedback_context"]
    assert feedback["reviewer_issues"] == verdict["issues"]
    assert "review verdict: fail" in "\n".join(feedback["issues_found"])
    assert any("Transform drops null rows." in line for line in feedback["issues_found"])


@pytest.mark.parametrize("severity", sorted(_rv.NON_BLOCKING_SEVERITIES))
def test_retained_transform_block_issue_triggers_no_regeneration_at_non_blocking_severity(severity):
    """**Validates: Requirements 3.2, 3.5** — the released half of the arm above:
    a reviewer transform-block finding at a Non_Blocking_Severity on a raw pass
    adds no verdict trigger, and the finding is RETAINED rather than discarded.

    Both halves matter, and the second is the point. "No regeneration" on its own
    would be satisfied by a gate that dropped the finding, which is not what p43
    R3.5 asks for: the reviewer said something, it is not blocking, and it is
    still reported. So ``feedback_context["reviewer_issues"]`` is asserted to
    carry it — that key is populated ahead of every branch
    (``lambda_function.py:1497``), so its content is a claim about retention and
    not about the trigger.

    **What reversed, and on whose authority.** This is the ``minor`` arm of
    ``test_explicit_transform_block_issue_triggers_regeneration_any_severity``,
    amended rather than deleted under ``LGR-60c4985-6`` (**template /
    ratified**); ``LGR-5906ec6-2`` (**template / ratified**) is why the hold was
    an assertion. The ``"review verdict: fail"`` trigger assertion is inverted
    here rather than dropped, so the reversal is visible at the same line it
    reverses. The hold is stated in the section header "Why the property is about
    the ATTRIBUTION and the severity FLOOR", below.

    Non-vacuity, as for the aggregation sibling: a defaulted attribution would
    also produce no trigger on a raw pass (rule 4), so the parsed issue's
    ``attribution_source`` is asserted to be ``reviewer``.
    """
    verdict = parse_review_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "issues": [_issue("Transform drops null rows.", severity, attribution="transform_block")],
                "categories": {},
            }
        )
    )
    assert [i["attribution_source"] for i in verdict["issues"]] == [_rv.ATTRIBUTION_SOURCE_REVIEWER]

    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="passed", checks_run=4, checks_passed=4, review_verdict=verdict, regeneration_attempt=0
    )

    assert analysis["regeneration_needed"] is False
    feedback = analysis["feedback_context"]
    # R3.5 — retained, not discarded: the reviewer's issue is still on the
    # feedback context even though it did not trigger anything.
    assert feedback["reviewer_issues"] == verdict["issues"]
    # The inverse of the blocking arm's trigger assertion, at the same site.
    assert "review verdict: fail" not in "\n".join(feedback["issues_found"])
    # Non-vacuity on the line above: `regeneration_needed is False` is also what
    # an out-of-bounds run returns, so the in-bounds fact is asserted too — this
    # run did not regenerate because nothing triggered, not because it ran out of
    # attempts.
    assert analysis["max_attempts_reached"] is False


def test_unattributed_issues_on_fail_verdict_still_trigger_regeneration():
    """**Validates: Requirements 1.5, 1.6**"""
    verdict = parse_review_verdict(
        json.dumps({"verdict": "fail", "issues": [_issue("Unattributed finding.", "major")], "categories": {}})
    )
    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="passed", checks_run=4, checks_passed=4, review_verdict=verdict, regeneration_attempt=0
    )
    assert analysis["regeneration_needed"] is True


def test_scaffolding_only_fail_respects_existing_attempt_bounds():
    """**Validates: Requirements 1.6** — the bound applies to effective fails
    exactly as before (attempt at bound → no regeneration, bound recorded)."""
    verdict = parse_review_verdict(
        json.dumps(
            {
                "verdict": "fail",
                "issues": [_issue("Transform bug.", "major", attribution="transform_block")],
                "categories": {},
            }
        )
    )
    at_bound = _mod.analyze_test_results_for_regeneration(
        test_status="passed", checks_run=4, checks_passed=4, review_verdict=verdict, regeneration_attempt=2
    )
    assert at_bound["regeneration_needed"] is False
    assert at_bound["max_attempts_reached"] is True
    assert at_bound["max_attempts"] == 2


# ---------------------------------------------------------------------------
# determine_code_approval_requirement — effective-verdict substitution
# ---------------------------------------------------------------------------


def test_scaffolding_only_fail_requires_no_approval_when_checks_pass():
    """**Validates: Requirements 1.3**"""
    verdict = parse_review_verdict(
        json.dumps({"verdict": "fail", "issues": _SCAFFOLDING_ONLY_FAIL_ISSUES, "categories": {}})
    )
    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=verdict,
        regeneration_analysis=_NO_REGEN,
    )
    assert required is False


@pytest.mark.parametrize("severity", ["critical", "major"])
def test_explicit_transform_block_issue_requires_approval_any_severity(severity):
    """**Validates: Requirements 1.4**

    **What reversed, and on whose authority.** Third of the three
    ``any_severity`` parametrizations amended together (with the aggregation and
    regeneration arms above): the list was ``["critical", "major", "minor"]``
    when rule 1 read no severity, and "any severity" now means any
    Blocking_Severity (p43 R1.1/R1.2). The ``minor`` arm moves to
    :func:`test_retained_transform_block_issue_requires_no_approval_at_non_blocking_severity`
    below and asserts the opposite outcome (p43 R3.3). Authority:
    ``LGR-60c4985-6`` (**template / ratified**) — amend, never delete, and keep
    the reversal legible as a reversal; citing ``LGR-5906ec6-2`` (**template /
    ratified**). The hold this reverses is stated in the section header "Why the
    property is about the ATTRIBUTION and the severity FLOOR", below.
    """
    verdict = parse_review_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "issues": [_issue("Transform bug.", severity, attribution="transform_block")],
                "categories": {},
            }
        )
    )
    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=verdict,
        regeneration_analysis=_NO_REGEN,
    )
    assert required is True


@pytest.mark.parametrize("severity", sorted(_rv.NON_BLOCKING_SEVERITIES))
def test_retained_transform_block_issue_requires_no_approval_at_non_blocking_severity(severity):
    """**Validates: Requirements 3.3** — the released half of the arm above: a
    reviewer transform-block finding at a Non_Blocking_Severity on a raw pass,
    with the checks passing, requires no approval.

    **What reversed, and on whose authority.** This is the ``minor`` arm of
    ``test_explicit_transform_block_issue_requires_approval_any_severity``,
    amended rather than deleted under ``LGR-60c4985-6`` (**template /
    ratified**); ``LGR-5906ec6-2`` (**template / ratified**) is why the hold was
    encoded as an assertion. The hold is stated in the section header "Why the
    property is about the ATTRIBUTION and the severity FLOOR", below.

    Nothing here says the pipeline's own Code_Approval_Setting is ignored — this
    is the QUALITY GATE's answer, the first of the two inputs
    ``determine_merged_approval_requirement`` merges, and p43 reads neither the
    configuration nor the merge (p43 R3.7). ``test_status`` is held at ``passed``
    so the verdict branch is the only one that could fire; a failing status
    requires approval by its own branch
    (``lambda_function.py:1670-1672``), regardless of any verdict.

    Non-vacuity, as for the two siblings above: a defaulted attribution would
    also require no approval on a raw pass (rule 4), so the parsed issue's
    ``attribution_source`` is asserted to be ``reviewer``.
    """
    verdict = parse_review_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "issues": [_issue("Transform bug.", severity, attribution="transform_block")],
                "categories": {},
            }
        )
    )
    assert [i["attribution_source"] for i in verdict["issues"]] == [_rv.ATTRIBUTION_SOURCE_REVIEWER]

    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=verdict,
        regeneration_analysis=_NO_REGEN,
    )
    assert required is False


def test_unattributed_issues_on_fail_verdict_still_require_approval():
    """**Validates: Requirements 1.5**"""
    verdict = parse_review_verdict(
        json.dumps({"verdict": "fail", "issues": [_issue("Unattributed finding.", "minor")], "categories": {}})
    )
    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=verdict,
        regeneration_analysis=_NO_REGEN,
    )
    assert required is True


# ---------------------------------------------------------------------------
# R1.7 — no token-presence or keyword-count logic anywhere in the gate
# ---------------------------------------------------------------------------


def test_gate_functions_contain_no_token_presence_or_keyword_count_logic():
    """**Validates: Requirements 1.7** — the calibration is confined to the
    structured-verdict machinery: no gate function scans code or review text
    for the transform-block marker or counts keywords."""
    gate_functions = (
        _mod.aggregate_check_results,
        _mod.analyze_test_results_for_regeneration,
        _mod.determine_code_approval_requirement,
        effective_verdict,
        verdict_to_parsed_results,
    )
    for func in gate_functions:
        src = inspect.getsource(func)
        assert "=== AI-Generated" not in src, f"{func.__name__} consults the transform-block marker"
        assert ".count(" not in src, f"{func.__name__} counts tokens/keywords"
        assert "review_lower" not in src, f"{func.__name__} resurrects the substring scan"


def test_effective_verdict_is_pure_over_the_verdict_dict():
    """**Validates: Requirements 1.7** — the gate decision takes ONLY the
    structured verdict; there is no code/text input it could scan."""
    params = list(inspect.signature(effective_verdict).parameters)
    assert params == ["verdict"]


def test_attribution_values_come_from_the_verdict_not_from_code_inspection():
    """**Validates: Requirements 1.5, 1.7** — the same verdict yields the
    same gate decision regardless of any code artifact: nothing but the
    structured issues determines attribution."""
    scaffolding_only = parse_review_verdict(
        json.dumps({"verdict": "fail", "issues": _SCAFFOLDING_ONLY_FAIL_ISSUES, "categories": {}})
    )
    assert effective_verdict(scaffolding_only) == "pass"
    assert _rv.ATTRIBUTION_TRANSFORM_BLOCK == "transform_block"
    assert _rv.ATTRIBUTION_TEMPLATE_SCAFFOLDING == "template_scaffolding"


# ---------------------------------------------------------------------------
# verdict_to_parsed_results — recommendation derives from the effective verdict
# ---------------------------------------------------------------------------


def test_scaffolding_only_fail_recommends_approve_with_issues_passed_through():
    """**Validates: Requirements 1.3** — the adapter recommendation follows
    the calibrated gate; the attributed issues pass through unchanged."""
    parsed = _parsed_from("fail", _SCAFFOLDING_ONLY_FAIL_ISSUES)
    assert parsed["verdict"] == "fail"  # the raw verdict is NOT rewritten
    assert parsed["recommendation"] == "APPROVE"
    assert [i["attribution"] for i in parsed["issues"]] == ["template_scaffolding", "template_scaffolding"]
    assert all(i["attribution_source"] == "reviewer" for i in parsed["issues"])


@pytest.mark.parametrize("severity", sorted(_rv.NON_BLOCKING_SEVERITIES))
def test_transform_block_issue_on_raw_pass_recommends_approve(severity):
    """**Validates: Requirements 3.4, 8.1** — a reviewer transform-block finding
    at a Non_Blocking_Severity on a raw ``pass`` is an effective pass, so the
    adapter derives ``APPROVE``; the Raw_Verdict is passed through untouched.

    The severities are read off ``review_verdict.NON_BLOCKING_SEVERITIES`` rather
    than restated, following the three consumer arms above, so this cannot drift
    from the gate's own vocabulary if the set ever grows a second tier.

    **What reversed, and on whose authority.** This test was
    ``test_transform_block_issue_on_raw_pass_recommends_reject`` and asserted
    ``recommendation == "REJECT"`` on exactly this fixture: rule 1 read no
    severity at all, so an explicit reviewer transform-block finding recommended
    REJECT even on a raw pass. Under p43 R1.1/R1.2 the severity floor puts a
    Non_Blocking_Severity finding outside rule 1's antecedent, so the same
    fixture is an effective pass and R3.4 derives ``APPROVE``. The REJECT case
    the old name stood for is **not lost** — it moves to
    :func:`test_transform_block_issue_on_raw_pass_recommends_reject_at_blocking_severity`
    immediately below, at ``critical``. Authority: ``LGR-60c4985-6``
    (**template / ratified**) — amend the assertions that encoded the hold, never
    delete them, and keep the reversal legible as a reversal rather than read as
    a regression; citing ``LGR-5906ec6-2`` (**template / ratified**) for this
    repo's convention that a deliberate hold is encoded as a test assertion. The
    hold this reverses is stated in the section header "Why the property is about
    the ATTRIBUTION and the severity FLOOR", below.

    ``parsed["verdict"] == "pass"`` is retained deliberately (p43 R8.1,
    do-NOT 6): the Recommendation_Deriver recalibrates the *recommendation* and
    never rewrites the Raw_Verdict — the same pin
    :func:`test_scaffolding_only_fail_recommends_approve_with_issues_passed_through`
    carries for the scaffolding route.

    Non-vacuity: the attribution antecedent is asserted, not assumed.
    ``APPROVE`` is equally what a *defaulted* attribution produces on a raw pass
    (``review_verdict.py:220-222`` → rule 4), so without the
    ``attribution_source == "reviewer"`` assertion a misspelled ``attribution``
    key in the fixture would pass this test for entirely the wrong reason — the
    same trap the three consumer arms above assert against.
    """
    parsed = _parsed_from("pass", [_issue("Transform bug.", severity, attribution="transform_block")])
    assert [i["attribution"] for i in parsed["issues"]] == [_rv.ATTRIBUTION_TRANSFORM_BLOCK]
    assert [i["attribution_source"] for i in parsed["issues"]] == [_rv.ATTRIBUTION_SOURCE_REVIEWER]

    assert parsed["verdict"] == "pass"  # the raw verdict is NOT rewritten (R8.1)
    assert parsed["recommendation"] == "APPROVE"


def test_transform_block_issue_on_raw_pass_recommends_reject_at_blocking_severity():
    """**Validates: Requirements 1.4, 8.1** — the case the old
    ``…_recommends_reject`` name stood for, kept at a Blocking_Severity: an
    explicit reviewer transform-block finding at ``critical`` on a raw ``pass``
    is an effective fail, so the adapter still derives ``REJECT``.

    Added under ``LGR-60c4985-6`` (**template / ratified**) so that no case is
    lost to the inversion above: the amendment narrows rule 1's antecedent, and
    this is the half of the old assertion that survives it unchanged. Without it
    nothing in this section would assert that rule 1 still reaches the
    recommendation at all — the same reason design §4.7 row 7 moved
    ``test_the_blocking_finding_generator_supplies_reviewer_attribution``'s
    fixture to ``critical`` rather than inverting it.

    ``parsed["verdict"] == "pass"`` holds here too (R8.1, do-NOT 6): the raw
    verdict is passed through even where the gate overrides it in the other
    direction.

    The ``attribution_source`` assertion is documentation rather than a
    non-vacuity guard on this arm — ``REJECT`` on a raw ``pass`` is reachable
    only through rule 1, so a defaulted attribution could not green it.
    """
    parsed = _parsed_from("pass", [_issue("Transform bug.", "critical", attribution="transform_block")])
    assert [i["attribution_source"] for i in parsed["issues"]] == [_rv.ATTRIBUTION_SOURCE_REVIEWER]

    assert parsed["verdict"] == "pass"  # the raw verdict is NOT rewritten (R8.1)
    assert parsed["recommendation"] == "REJECT"


# ---------------------------------------------------------------------------
# Full handler — scaffolding-only fail passes the step end-to-end with the
# issues visible in the checks list, the report, and the approval payload
# ---------------------------------------------------------------------------

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
}

_SCAFFOLDING_ONLY_FAIL_JSON = json.dumps(
    {
        "verdict": "fail",
        "issues": [
            {
                "severity": "major",
                "category": "AWS Best Practices",
                "description": "Glue boilerplate re-creates the SparkSession.",
                "attribution": "template_scaffolding",
            }
        ],
        "categories": {"Correctness": "pass", "Security": "pass"},
    }
)

_TRANSFORM_BLOCK_FAIL_JSON = json.dumps(
    {
        "verdict": "fail",
        "issues": [
            {
                "severity": "minor",
                "category": "Correctness",
                "description": "Transform drops null rows.",
                "attribution": "transform_block",
            }
        ],
        "categories": {"Correctness": "pass", "Security": "pass"},
    }
)


def _state_rows(runs_table) -> list[dict]:
    """Reconstruct the state rows the handler wrote to the runs table.

    The handler's row write is an ``update_item`` SET over exactly the
    attributes the step owns, not a full-row ``put_item``: a PutItem there
    deleted ``store_regeneration_feedback``'s write on the same item earlier in
    the same invocation (see ``build_state_row_update``). The row is therefore
    the ``Key`` plus the SET values, identified by ``step_sequence`` — an
    attribute only the row write sets.
    """
    rows = []
    for call in runs_table.update_item.call_args_list:
        values = {name.lstrip(":"): value for name, value in call.kwargs["ExpressionAttributeValues"].items()}
        if "step_sequence" not in values:
            continue
        rows.append({**call.kwargs["Key"], **values})
    return rows


#: The pre-p34 record shape this file's handler tests were written against: a
#: pipeline record that exists, carries no target schema (p33 requirement 2.6's
#: legitimate state) and **no** ``approval_config``. Under p34 requirement 1.2 an
#: absent Code_Approval_Setting resolves to ``True``, so the Approval_Policy's
#: merged ``approval_required`` is ``True`` on any run seeded with this record,
#: whatever the Code_Quality_Gate concluded.
_PIPELINE_RECORD_NO_APPROVAL_CONFIG = {"pipeline_id": "pipe-1"}

#: The same record with the Code_Approval_Setting stated explicitly as the JSON
#: boolean ``false``, so the ONLY thing that can force the gate is the verdict.
_PIPELINE_RECORD_APPROVAL_DISABLED = {
    "pipeline_id": "pipe-1",
    "approval_config": {"code_generation_enabled": False},
}


def _fake_dynamodb(pipeline_record: dict = _PIPELINE_RECORD_NO_APPROVAL_CONFIG) -> MagicMock:
    """A ``dynamodb`` stand-in whose pipelines table returns a keyless record.

    p33 task 4.3 plumbing, not an assertion change: the handler's target-schema
    read moved from the schema-mappings item to the pipeline record, so the
    pipelines table is now on the handler's path. A bare ``MagicMock()`` answers
    ``False`` to ``"Item" in response``, which now reads as a *missing* pipeline
    record and raises StepFailure (p33 requirement 2.8). The default record
    exists and carries no target schema — requirement 2.6's legitimate state —
    so the check lists this file asserts on are unchanged: no target schema means
    no computed schema-compliance entry.

    p34 task 4.1: the record is now a parameter, because the same record also
    carries the Code_Approval_Setting the Approval_Policy reads
    (``resolve_code_approval_setting``, ``test_code/lambda_function.py:702``).
    A test that wants the verdict to be the only thing able to force the gate
    passes :data:`_PIPELINE_RECORD_APPROVAL_DISABLED`.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": dict(pipeline_record)}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def _run_handler(
    review_text: str,
    execution_results: dict,
    pipeline_record: dict = _PIPELINE_RECORD_NO_APPROVAL_CONFIG,
):
    """Run lambda_handler with all AWS/agent dependencies mocked.

    Returns (response, state_item, s3_mock).
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value=review_text)
    agent.add_hook = MagicMock()

    runner = MagicMock()
    runner.run_test.return_value = execution_results

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb(pipeline_record)),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        response = _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1"}, None)

    testing_items = [i for i in _state_rows(runs_table) if i.get("step_name") == "test_code"]
    assert len(testing_items) == 1, "expected exactly one test_code state item"
    return response, testing_items[0], s3


def test_handler_scaffolding_only_fail_passes_the_gate_with_issues_visible():
    """**Validates: Requirements 1.3, 1.8** — a scaffolding-only fail verdict
    passes the test step; the retained issues are visible in the checks list,
    the comprehensive report, and the approval payload.

    AMENDED for p34 task 4.1 — this test's fixture REVERSES, deliberately, and
    the test name is kept verbatim because P28b R1b's calibration is pinned at
    exactly this identifier (`LGR-5906ec6-2`, **template / ratified**: this repo
    encodes a deliberate hold as a test assertion, so enabling held behaviour
    means changing a test on purpose; `LGR-60c4985-6`, **template / ratified**,
    names this file and requires the amendment stay legible as a reversal rather
    than read as a regression).

    **The surviving subject is P28b R1b**: a scaffolding-only fail does not force
    the gate *on verdict grounds*. That subject is untouched by p34.

    What reversed is the fixture, not the claim. Before p34 the pipeline record
    seeded here carried no ``approval_config`` and nothing read it, so
    ``approval_required`` was the Code_Quality_Gate's return. p34 makes
    ``approval_required`` the MERGED Approval_Policy decision — the disjunction
    of the pipeline's Code_Approval_Setting, the quality gate, and the
    regeneration-attempt count (requirement 3.2) — and requirement 1.2 resolves
    an absent setting to ``True``. On the old fixture the merged field would now
    be ``True`` for a reason that has nothing to do with the verdict, which would
    stop this test from saying anything about the verdict at all.

    So the fixture states the configuration EXPLICITLY as ``false``
    (:data:`_PIPELINE_RECORD_APPROVAL_DISABLED`), which makes the verdict the only
    thing that could force the gate. The two original assertions then hold for a
    **stronger** reason than before: ``approval_required`` is ``False`` BECAUSE
    the verdict did not force it, with the configuration ruled out by
    construction rather than by absence.

    The added assertion is the actual subject: ``quality_gate_requires_approval``
    is requirement 3.1's Quality_Verdict_Field, the quality gate's own answer with
    no configuration input, so P28b R1b stays observable on its own field no
    matter what any pipeline's configuration says. Its config-absent sibling is
    ``test_handler_scaffolding_only_fail_still_gates_when_the_configuration_is_absent``.
    """
    response, state_item, s3 = _run_handler(
        _SCAFFOLDING_ONLY_FAIL_JSON, _EXEC_OK, pipeline_record=_PIPELINE_RECORD_APPROVAL_DISABLED
    )

    # The step passes: no forced approval, straight to pipeline creation.
    assert response["test_status"] == "passed"
    assert response["test_result"]["status"] == "passed"
    assert response["approval_required"] is False
    assert response["next_step"] == "create_glue_pipeline"
    assert state_item["status"] == "success"
    assert response["test_summary"]["recommendation"] == "APPROVE"

    # AMENDED per p34 requirement 3.1 — the unmerged quality answer is the actual
    # P28b R1b subject, now observable on its own field regardless of config.
    assert response["quality_gate_requires_approval"] is False
    assert state_item["quality_gate_requires_approval"] is False

    # Visibility (checks list): the retained scaffolding issue is in the
    # ai_review_verdict check detail of BOTH the response and the state item.
    for container in (response["test_result"], state_item["test_result"]):
        verdict_check = next(c for c in container["checks"] if c["name"] == "ai_review_verdict")
        assert verdict_check["passed"] is True
        assert "Reviewer verdict: fail" in verdict_check["detail"]
        assert "effective verdict: pass" in verdict_check["detail"]
        assert "0 transform_block, 1 template_scaffolding" in verdict_check["detail"]
        assert "Glue boilerplate re-creates the SparkSession." in verdict_check["detail"]

    # Visibility (approval payload): the state item's approval payload carries
    # the same structured test_result with the retained issue detail.
    approval_checks = state_item["output_data"]["approval"]["test_result"]["checks"]
    approval_verdict_check = next(c for c in approval_checks if c["name"] == "ai_review_verdict")
    assert "Glue boilerplate re-creates the SparkSession." in approval_verdict_check["detail"]

    # Visibility (comprehensive report): the markdown report stored to S3
    # embeds the reviewer output, so the scaffolding issue text is retained.
    report_bodies = [
        call.kwargs["Body"] for call in s3.put_object.call_args_list if call.kwargs["Key"].endswith("test_report.md")
    ]
    assert len(report_bodies) == 1
    assert "Glue boilerplate re-creates the SparkSession." in report_bodies[0].decode("utf-8")


def test_handler_scaffolding_only_fail_still_gates_when_the_configuration_is_absent():
    """**Validates: p34 Requirements 3.1, 3.2** — the sibling that makes the
    reversal legible at the assertion site (`LGR-60c4985-6`, template / ratified).

    The run is the one above, verdict for verdict: the same scaffolding-only fail,
    the same clean checks, the same ``test_status == "passed"``. Only the pipeline
    record differs — it carries no ``approval_config``, which is the shape of every
    Pipeline_Record written before p34 (requirement 7.2).

    Two assertions, and they are the whole of this spec:

    * ``quality_gate_requires_approval is False`` — the Code_Quality_Gate did not
      ask for review. P28b R1b's calibration, unchanged and unchangeable by any
      configuration (requirement 3.1).
    * ``approval_required is True`` — the Approval_Policy asks for review anyway,
      because requirement 1.2 resolves the absent Code_Approval_Setting to ``True``
      and requirement 2.2 makes an enabled setting force the gate irrespective of
      the quality gate's outcome.

    Before p34 those two values were one field and could not disagree, which is
    exactly why the gate could not honour the configuration: the field the
    ``CheckCodeApproval`` Choice reads structurally could not see it. They can
    disagree now, and here they do.
    """
    response, state_item, _ = _run_handler(
        _SCAFFOLDING_ONLY_FAIL_JSON, _EXEC_OK, pipeline_record=_PIPELINE_RECORD_NO_APPROVAL_CONFIG
    )

    # Same run as the test above — the verdict outcome is identical.
    assert response["test_status"] == "passed"
    assert response["test_result"]["status"] == "passed"
    assert state_item["status"] == "success"

    # R3.1 — the quality gate's own answer, with no configuration input.
    assert response["quality_gate_requires_approval"] is False
    assert state_item["quality_gate_requires_approval"] is False

    # R3.2 — the merged Approval_Policy decision, which the configuration reaches.
    assert response["approval_required"] is True
    assert state_item["approval_required"] is True
    assert response["next_step"] == "approval_check"

    # R3.5 — the state row records WHICH input required approval, so the audit
    # trail names the configuration rather than implying a quality concern.
    assert state_item["approval_decision_inputs"] == {
        "pipeline_config": True,
        "quality_gate": False,
        "regeneration": False,
    }


def test_handler_transform_block_fail_still_fails_the_gate():
    """**Validates: Requirements 1.4, 1.8** — an explicit transform-block
    finding (minor severity) fails the step end-to-end.

    p32 2.17 amended ONE line here: ``next_step``. This is a third assertion
    pinning P28 R4.4's hold, alongside the two the p32 design enumerated in
    ``test_honest_aggregation.py`` — the reversal is recorded there and in the
    MR's Decision block. The subject of THIS test is that a transform-block
    finding fails the gate, and that is unchanged: the status is still failed,
    approval is still required, and the verdict check still fails. Only the
    destination the step names moved, because this run is in bounds
    (``regeneration_attempt`` defaults to 0, bound 2) and the regeneration
    destination now exists.

    **Docstring-only amendment under p43: the outcome is unchanged, the ROUTE
    changed.** ``_TRANSFORM_BLOCK_FAIL_JSON`` carries ``"severity": "minor"`` — a
    member of ``review_verdict.NON_BLOCKING_SEVERITIES`` — so under p43 R1.1/R1.2
    the severity conjunct exempts this finding and **rule 1 no longer fires
    here**. The step still fails, through rules 2/3
    (``review_verdict.py:336-343``): the raw verdict is ``fail`` and the sole
    issue is not explicitly scaffolding-attributed, so the conservative default
    keeps it failing. That is p43 R1.4 — the floor may not weaken a raw ``fail``
    — and it is why every assertion below stands verbatim: status failed,
    approval required, ``next_step == "code_regeneration"``, the state row
    failed, ``regeneration_needed``, and the ``ai_review_verdict`` check failed.
    **No assertion is touched**, because none of them was wrong.

    ``LGR-60c4985-6`` (**template / ratified**) names this test as its *third
    hold assertion*, so leaving it unamended in substance is that record's own
    expectation: what is owed here is the reason, not a changed outcome. It is
    recorded beside p32's one-line amendment above rather than in place of it —
    successive amendments at one site have to stay legible as a sequence, which
    is the point of the discipline. The hold p43 reverses is stated in the
    section header "Why the property is about the ATTRIBUTION and the severity
    FLOOR" further down this file; ``LGR-5906ec6-2`` (**template / ratified**) is
    why that hold lived in an assertion in the first place. The two guards the
    record enumerates as untouched — ``test_honest_aggregation.py:161``
    ``test_execution_success_does_not_override_static_failures`` and ``:169``
    ``test_missing_execution_results_is_a_failed_check`` — stay untouched by p43
    as well, and the floor cannot reach them: their ``_parsed`` fixture (``:93``)
    emits neither ``verdict`` nor ``issues``, so the gate is never called there.
    """
    response, state_item, _ = _run_handler(_TRANSFORM_BLOCK_FAIL_JSON, _EXEC_OK)

    assert response["test_status"] == "failed"
    assert response["test_result"]["status"] == "failed"
    assert response["approval_required"] is True
    assert response["next_step"] == "code_regeneration"
    assert state_item["status"] == "failed"
    assert response["regeneration_needed"] is True
    verdict_check = next(c for c in response["test_result"]["checks"] if c["name"] == "ai_review_verdict")
    assert verdict_check["passed"] is False


@pytest.mark.parametrize("severity", sorted(_rv.NON_BLOCKING_SEVERITIES))
@pytest.mark.parametrize(
    ("pipeline_record", "pipeline_config_input", "approval_required"),
    [
        (_PIPELINE_RECORD_NO_APPROVAL_CONFIG, True, True),
        (_PIPELINE_RECORD_APPROVAL_DISABLED, False, False),
    ],
    ids=["setting_absent_resolves_true", "setting_explicitly_false"],
)
def test_handler_retained_finding_leaves_the_approval_inputs_unchanged(
    pipeline_record, pipeline_config_input, approval_required, severity
):
    """**Validates: Requirements 3.7** — on a handler run whose gate passes
    *because of the severity floor*, the merged-approval input map and the
    ``test_status`` inputs to the approval decision are what the configuration
    and the attempt count say they are, not altered by the retention.

    A no-change claim over fixed inputs, so an example rather than a property:
    there is no quantifier here, only three inputs whose values are decided
    elsewhere and must arrive unchanged.

    **What the merged-approval input map is.** The Approval_Policy is
    ``determine_merged_approval_requirement``
    (``test_code/lambda_function.py:1685``), which returns
    ``(decision, inputs)`` from one dict (``:1748-1753``) so the audit row and
    the decision cannot drift. The handler calls it at ``:2867-2872`` and writes
    the map onto the state row as ``approval_decision_inputs`` (``:3088``, state
    row only — p34 design D3 keeps it off the handler response). Its three keys:

    * ``pipeline_config`` — the Code_Approval_Setting resolved by
      ``resolve_code_approval_setting`` (``:1568``) from the pipeline record the
      handler already read, fail-closed to ``True`` when absent or non-boolean
      (p34 R1.2/R1.4);
    * ``quality_gate`` — ``determine_code_approval_requirement``'s unmerged
      return (``:2849-2852``), the only one of the three p43 moves;
    * ``regeneration`` — ``regeneration_attempt > 0``, ``0`` on a first attempt.

    **Which Code_Approval_Setting this exercises, and why both.** Parametrized
    over the two records because "approval not required" is *not* the only
    correct outcome on a clean run: p34 R1.1 lets the pipeline's own setting
    force the gate whatever the quality gate concluded, and R1.2 resolves an
    absent setting to ``True``. So:

    * ``setting_absent_resolves_true`` is the load-bearing arm. It is the only
      one where a bug could hide: ``pipeline_config`` is ``True`` while
      ``quality_gate`` is ``False``, so a retention that wrongly reached the
      configuration input would show up as ``approval_required`` dropping to
      ``False`` on a pipeline whose owner asked for review — the permissive
      direction. With the setting explicitly ``false`` all three inputs are
      ``False`` and "untouched" is indistinguishable from "zeroed".
    * ``setting_explicitly_false`` is the arm where the floor is visible in the
      merged decision at all, and it is the run the spec exists for: nothing
      forces the gate, so a ``minor`` reviewer transform-block finding on a raw
      ``pass`` no longer consumes a human decision.

    Either way ``quality_gate is False`` and the map's other two entries track
    the configuration and the attempt count alone, which is the whole of R3.7.

    The ``test_status`` half is the other input to the quality gate
    (``determine_code_approval_requirement(test_status, checks_run,
    checks_passed, …)``, ``:1614-1620``): a retained finding leaves the honest
    aggregation at ``passed`` with no failed check, so the status branch at
    ``:1669-1671`` is not what decided this run either.

    This is an ADDITION, not an amendment — no assertion here encoded the old
    rule 1 — so ``LGR-60c4985-6`` (**template / ratified**) owes it no reversal
    paragraph. What governs the claim is p34: ``LGR-f077f1b-1`` (**template /
    ratified**) put the code-generation Approval_Policy in ``test_code`` and
    deleted ``create_code``'s copy rather than renaming it, which is why this
    map is asserted on the state row of this step and nowhere else;
    ``LGR-69ca78b-1`` (**invariant**) is the posture the ``pipeline_config``
    fail-closed default mirrors — the governance Choice states fail closed, so
    an absent setting is a human, not a permissive default. Its assertion
    precedent is
    :func:`test_handler_scaffolding_only_fail_still_gates_when_the_configuration_is_absent`
    above, which pins the same map on the scaffolding route.
    """
    # Non-vacuity, as for the three consumer arms above: the fixture really is a
    # Reviewer_Transform_Block_Finding, so this run exercises the floor rather
    # than the defaulted-attribution route into rule 4
    # (``review_verdict.py:220-222``), which would reach an effective pass for a
    # reason that has nothing to do with severity.
    parsed = _parsed_from("pass", [_issue("Transform drops null rows.", severity, attribution="transform_block")])
    assert [i["attribution_source"] for i in parsed["issues"]] == [_rv.ATTRIBUTION_SOURCE_REVIEWER]

    review_json = json.dumps(
        {
            "verdict": "pass",
            "issues": [
                {
                    "severity": severity,
                    "category": "Correctness",
                    "description": "Transform drops null rows.",
                    "attribution": "transform_block",
                }
            ],
            "categories": {"Correctness": "pass", "Security": "pass"},
        }
    )
    response, state_item, _ = _run_handler(review_json, _EXEC_OK, pipeline_record=pipeline_record)

    # The premise: the gate passes because of the floor, and the finding is
    # retained rather than discarded (R3.1, R3.5).
    verdict_check = next(c for c in response["test_result"]["checks"] if c["name"] == "ai_review_verdict")
    assert verdict_check["passed"] is True
    assert "effective verdict: pass" in verdict_check["detail"]
    assert "1 transform_block, 0 template_scaffolding" in verdict_check["detail"]
    assert "Transform drops null rows." in verdict_check["detail"]

    # R3.7 — the `test_status` inputs to the approval decision, unchanged: the
    # honest aggregation is `passed` with no failed check, so neither the status
    # branch nor the counts are what decided this run.
    assert response["test_status"] == "passed"
    assert state_item["test_status"] == "passed"
    assert state_item["status"] == "success"
    summary = response["test_summary"]
    assert summary["checks_failed"] == 0
    assert summary["checks_run"] > 0
    assert summary["checks_passed"] == summary["checks_run"]

    # R3.7 — the merged-approval input map, unchanged: `quality_gate` is the
    # only entry the floor moves, and it moved to False. The other two are the
    # configuration and the attempt count, verbatim.
    assert state_item["approval_decision_inputs"] == {
        "pipeline_config": pipeline_config_input,
        "quality_gate": False,
        "regeneration": False,
    }
    assert response["quality_gate_requires_approval"] is False
    assert state_item["quality_gate_requires_approval"] is False
    assert response["approval_required"] is approval_required
    assert state_item["approval_required"] is approval_required


# ===========================================================================
# Property 17 — a successful execution never excuses a blocking static finding
# ===========================================================================
#
# Spec ``real-spark-verification`` (p42) task 11.1.
#
#   *For any* successful execution result from either executor and *any*
#   reviewer verdict carrying an issue attributed to the transform block, the
#   derived ``test_status`` is failed.
#
# **Validates: Requirement 12.1**
#
# What this generalises
# ---------------------
#
# The existing example is ``test_honest_aggregation.py:161``,
# ``test_execution_success_does_not_override_static_failures``: one hand-built
# pandas-shaped success against one failing ``static_analysis:`` category.
# ``LGR-60c4985-6`` (**template / ratified**) names it as one of two "untouched
# guards, verified still passing untouched". It stays — a named case reads better
# than a counterexample. What this section adds is the **quantifier**, over the
# two axes an executor change moves:
#
# * the *shape* of a successful result — pandas-shaped and Glue-shaped, since
#   p42 introduces a second producer of the same contract; and
# * the *content* of a blocking finding — any severity, any position in the
#   issue list, on a raw ``pass`` verdict as well as a raw ``fail``.
#
# Why the property is about the ATTRIBUTION and the severity FLOOR
# ----------------------------------------------------------------
#
# ``effective_verdict`` rule 1 (``review_verdict.py:294``) returns ``fail`` on any
# issue with ``attribution == "transform_block"`` AND ``attribution_source ==
# "reviewer"`` **whose severity is not in
# ``review_verdict.NON_BLOCKING_SEVERITIES``**. R12.1's antecedent is "the
# reviewer attributed an issue to the transform block", so the generator supplies
# an explicit ``attribution`` value, which is what makes ``attribution_source``
# come out as ``reviewer`` from the real parser (``review_verdict.py:215-222``).
#
# **THE SEVERITY AXIS IS NOW RECALIBRATED. This paragraph is the reversal of the
# hold that stood here, kept in amended form rather than deleted.** What stood
# here said the severity over-blocking was real, was pinned as current behaviour,
# and was "a **different spec**" — design §3 do-NOT 2 of p42, quoted then as
# "``effective_verdict`` rule 1 failing the gate on any transform-block finding
# regardless of severity … are a different subsystem with an independent Decision
# and their own spec. C6 requires the *principle* to survive; the *calibration*
# is not this spec's business."
#
# **``p43-review-gate-severity-floor`` is that different spec**, and it has
# landed: p43 R1.1/R1.2 put a severity floor inside rule 1's condition, R1.3/R1.6
# make a ``minor`` reviewer transform-block finding retained-but-not-blocking, and
# R7.2 requires this property to be *preserved by narrowing its antecedent* — not
# deleted and not weakened. So:
#
# * the antecedent is now **Blocking_Severity** — :data:`_BLOCKING_SEVERITY`
#   feeds the reviewer arm of :func:`_verdicts_with_a_blocking_finding`, and
#   every assertion of Property 1 below holds verbatim on that narrower draw;
# * the ``minor`` half of the old space did not stop being tested, it moved:
#   **Property 2** asserts the opposite outcome over it, with the same
#   companions, the same permutation and the same non-vacuity discipline.
#
# The principle C6 required to survive is untouched: a successful execution still
# never excuses a *blocking* static finding. What changed is which findings block,
# and that is a calibration this spec's requirement ids own. Authority:
# ``LGR-60c4985-6`` (**template / ratified**) — amend the assertions that encoded
# the hold, never delete them, and keep the reversal legible as a reversal rather
# than read as a regression; citing ``LGR-5906ec6-2`` (**template / ratified**)
# for this repo's convention that a deliberate hold is encoded as a test
# assertion, which makes changing one a code change with a reason attached.
#
# What is NOT claimed
# -------------------
#
# A *defaulted* attribution does not satisfy this property's antecedent and is
# not generated here. An issue with no reviewer attribution is parsed to
# ``transform_block`` with ``attribution_source == "default"``
# (``review_verdict.py:220-222``), which reaches rule 2 rather than rule 1 — so
# it blocks a raw ``fail`` and does not block a raw ``pass``. That asymmetry is
# R1.5's conservative default, already covered by
# ``test_unattributed_issues_on_fail_verdict_keep_aggregation_failing`` above and
# by ``test_review_verdict_attribution.py:165``,
# ``test_rule2_defaulted_scaffolding_value_does_not_exempt``. Widening this
# property to defaulted attributions would assert something rule 4 deliberately
# does not do.
#
# Non-vacuity, by construction
# ----------------------------
#
# "Status is failed" is satisfied for free by any run that had some *other*
# reason to fail, so every draw carries its own control: the same execution
# result and the same issues, with every ``attribution`` flipped to
# ``template_scaffolding``, must derive ``passed``. The attribution is then the
# only difference between a passing and a failing run, which is exactly the
# claim. Concretely this rules out three ways the property could pass while
# testing nothing: a failing ``static_analysis:`` category (the generator emits
# only passing ones), a schema-compliance shortfall (``missing_columns`` is
# always empty), and a not-verified partition that leaves no verified check at
# all (a success always emits a passing ``code_interpreter_execution``).
#
# Falsifiability was checked rather than assumed. Each half of the assertion was
# inverted in turn (in the working tree, then reverted) and confirmed to fail:
# ``status == "passed"`` shrinks to a single ``critical`` transform-block issue on
# a raw ``pass`` verdict, and ``control_status == "failed"`` shrinks likewise. So
# neither half is passing by accident.
#
# Ledger notes
# ------------
#
# * ``LGR-60c4985-6`` (**template / ratified**) — names this file, and names
#   ``test_execution_success_does_not_override_static_failures`` as an untouched
#   guard. Both honoured: nothing above is edited.
# * ``LGR-5906ec6-2`` (**template / ratified**, cited through the record above) —
#   this repo encodes a deliberate hold as a test assertion. The severity
#   over-blocking WAS held here on purpose; p43 released it on purpose, through
#   the same mechanism. Which is the point of the convention: the hold and its
#   release are both a code change with a reason attached, and both are recorded
#   at the assertion. See the section above.
# * ``LGR-28b4d75-5`` (**template**) — the shape oracle is the declared contract
#   in ``tests/fixtures/run_test_result_shapes.py``, never what a runner
#   currently returns.
# * ``LGR-b2f79ef-1`` (**template / ratified**) — ``schema_compliance`` is "a real
#   emitted check that participates in the pass/fail derivation and **can fail a
#   run**". That is why the generator holds ``missing_columns`` empty: a shortfall
#   would fail every draw on its own and the property would assert nothing. The
#   same record notes the check is emitted only where validation output exists,
#   which is why a successful result always carries the seven DQ keys.
# * ``LGR-b2f79ef-2`` (**template / ratified**) — the ``passed`` + optional
#   ``verified`` pair already expresses three outcomes, and a third check *field*
#   was refused once. Its caveat matters to the control here: a ``verified: False``
#   entry must never be the only check, or "could not check" derives ``failed``.
#   The ``target_schema=None`` draw produces exactly such an entry, and the
#   control still reaches ``passed`` because ``ai_review_verdict`` and
#   ``code_interpreter_execution`` are both emitted alongside it.
# * ``LGR-b2f79ef-6`` (**template / ratified**, scope
#   ``tests/unit/lambdas/test_code/**``) — the convention followed here: amend
#   assertions that encoded an old contract and record the reason at the
#   assertion, never delete them. Task 11.1 amends none; it appends.
# * ``LGR-69ca78b-1`` (**invariant**) — the governance Choice states fail closed.
#   Relevant because this property is the Python half of the same posture: a key
#   nothing reads cannot make a gate permissive, and a blocking finding is not
#   read out of the execution result at all.
# * ``why`` on this file returns two commits' records — ``LGR-69ca78b-1..5`` (p34,
#   59 files) and ``LGR-60c4985-1..7`` (p32, 64 files) — of which exactly one,
#   ``LGR-60c4985-6``, names this file in its body. Both commits are broad enough
#   to match nearly any query, which is the documented snapshot trap; the other
#   eleven records are background, not fences on this file.
#   ``decisions(concept="testing", include_backfilled=True)`` returns five
#   records, none about choosing an executor, and
#   ``decisions(scope="agentic_data_ingestion/tests/unit/lambdas/test_code")``
#   returns the p33 set cited above. Stated as a gap in the ledger where it is one,
#   not as an absence of decision.


#: The two shapes of successful result the gate must be indifferent to. p42's
#: whole change is that a second executor produces the same contract, so the
#: property quantifies over both. Used by the strategy AND by the parametrized
#: conformance test below, so the two cannot disagree about which shapes exist.
_EXECUTOR_SHAPES = ("pandas", "glue")

#: The seven keys ``_parse_results`` writes into ``data_quality_results`` for a
#: parsed ``VALIDATION:`` payload (``code_interpreter_runner.py:626-634``). A
#: successful result always carries them: ``execution_successful`` is set to True
#: only inside that branch, so "success with an empty ``data_quality_results``"
#: is not a reachable shape and is not generated.
#:
#: ``dtypes`` is deliberately absent — design C7 predicts every ``dtypes`` value
#: moves between executors, but ``dtypes`` is not a ``data_quality_results`` key,
#: so that divergence is not representable here. ``null_counts`` is, and is
#: varied per executor below.
_DQ_KEYS = (
    "completeness_score",
    "uniqueness_score",
    "schema_compliance",
    "overall_quality_score",
    "null_counts",
    "duplicates",
    "missing_columns",
)

#: pandas ``isna()`` is True for ``None`` **and** ``NaN``; Spark ``isNull()`` is
#: False for ``NaN``, so a float column carrying ``NaN`` reports fewer nulls
#: under Glue (design C7). Both vocabularies are generated so the property is
#: visibly insensitive to the re-baseline.
_NULL_COUNTS_BY_EXECUTOR = {
    "pandas": {"customer_id": 0, "amount": 5},
    "glue": {"customer_id": 0, "amount": 0},
}


def _success_result(executor: str, *, completeness: float, duplicates: int, stderr: str) -> dict:
    """A successful ``run_test`` result in *executor*'s shape.

    Faithful to both producers, and asserted against the declared contract by
    :func:`test_generated_success_results_conform_to_the_declared_contract`:

    * pandas — ``run_test`` overwrites ``conversion_report`` with the real report
      on every path that reached a successful conversion
      (``code_interpreter_runner.py:304``, ``:316``), and ``_run_lifecycle``
      joins the three steps' non-fatal stderr onto a successful result
      (``:372-374``), so a benign ``stderr`` on a success is a real shape rather
      than an invented one.
    * Glue — ``conversion_report`` is ``None`` on **every** path, which task 8.1
      settled as final rather than provisional: that runner converts nothing, so
      its ``run_test`` overwrites nothing
      (``glue_session_runner.py:2456-2467``). Its ``success_results`` also leaves
      ``stderr`` at ``""``, because a Glue statement's ``Output`` has no stderr
      stream to join (``glue_session_runner.py:2657-2692``).

    Being *broader* than either producer would be safe for this property — it
    quantifies over any successful result, so an over-approximation still covers
    the real ones — but the shapes are kept faithful so that a reader can take
    "Glue-shaped" literally.
    """
    return {
        "execution_successful": True,
        "executed": True,
        "exit_code": 0,
        "stderr": "" if executor == "glue" else stderr,
        "input_records": 100,
        "output_records": 100,
        "data_quality_results": {
            "completeness_score": completeness,
            "uniqueness_score": 100,
            # Always a full score with nothing missing: a schema-compliance
            # shortfall would fail the run on its own and make the property
            # vacuous. See the non-vacuity note in the section header.
            "schema_compliance": 100,
            "overall_quality_score": completeness,
            "null_counts": dict(_NULL_COUNTS_BY_EXECUTOR[executor]),
            "duplicates": duplicates,
            "missing_columns": [],
        },
        "error_details": None,
        "conversion_report": None if executor == "glue" else {"converted": True, "rules_applied": ["withColumn"]},
    }


#: Issue text: non-empty after the parser's ``strip()``, which is what
#: ``_validate_verdict_document`` requires of a description
#: (``review_verdict.py:200-201``).
_ISSUE_TEXT = (
    st.text(alphabet=string.ascii_letters + string.digits + " .,", min_size=1, max_size=40).map(str.strip).filter(bool)
)

#: Any non-empty token is accepted as a severity — the parser documents three as
#: *recommended* and enforces none (``review_verdict.py:70-72``), so a novel
#: severity cannot fail the step. Two unrecommended values are generated
#: alongside the three recommended ones.
#:
#: **p43 (this spec, R1.1/R1.2) narrows where this is used, and does not narrow
#: the strategy itself.** It kept all five tokens when rule 1 read no severity at
#: all; it keeps all five now because it feeds the *scaffolding companions*,
#: where severity is still irrelevant — rules 2/3 test attribution only
#: (``review_verdict.py:281-290``). The reviewer transform-block arm draws from
#: :data:`_BLOCKING_SEVERITY` instead. Amended, not replaced, per
#: ``LGR-60c4985-6`` (**template / ratified**).
_SEVERITY = st.sampled_from(("critical", "major", "minor", "blocker", "nit"))

#: The Blocking_Severity tokens: exactly :data:`_SEVERITY` minus ``minor``.
#:
#: p43 R1.1/R1.2 put a severity floor inside rule 1's condition
#: (``review_verdict.py:294``), so the antecedent of Property 1 below is now a
#: reviewer transform-block finding at a severity NOT in
#: ``review_verdict.NON_BLOCKING_SEVERITIES``. ``blocker`` and ``nit`` stay:
#: they are Unrecognised_Severity, and the floor is encoded as the *complement*
#: of the non-blocking set, so an unrecognised token blocks (p43 R1.5). The
#: narrowing removes exactly ``minor``, which is what
#: :data:`_NON_BLOCKING_SEVERITY` then covers as its own property.
_BLOCKING_SEVERITY = st.sampled_from(("critical", "major", "blocker", "nit"))

#: The Non_Blocking_Severity tokens, read off the real frozenset rather than
#: restated, so this cannot drift from the gate's own vocabulary if the set ever
#: grows a second tier. Feeds Property 2's retained-finding arm.
_NON_BLOCKING_SEVERITY = st.sampled_from(sorted(_rv.NON_BLOCKING_SEVERITIES))

#: The three states the caller can pass for ``target_schema``: a resolved schema,
#: requirement 2.6's schema-less pipeline, and "the caller said nothing". All
#: three are safe for the control, because ``missing_columns`` is always empty —
#: a dict scores a passing check, ``None`` yields a ``verified: False`` entry
#: partitioned out of the derivation, and the sentinel takes the same branch as a
#: dict (``lambda_function.py:1343-1392``).
_TARGET_SCHEMA_STATES = (
    _mod.TARGET_SCHEMA_UNSPECIFIED,
    None,
    {"columns": [{"name": "customer_id"}, {"name": "amount"}]},
)


@st.composite
def _successful_execution_results(draw) -> tuple[str, dict]:
    """Draw ``(executor, result)`` for a successful run in either shape."""
    executor = draw(st.sampled_from(_EXECUTOR_SHAPES))
    return executor, _success_result(
        executor,
        completeness=draw(st.sampled_from((100, 97.5, 91.4, 0))),
        duplicates=draw(st.integers(min_value=0, max_value=5)),
        stderr=draw(st.sampled_from(("", "UserWarning: benign\n"))),
    )


@st.composite
def _verdicts_with_a_blocking_finding(draw) -> list[dict]:
    """Draw an issue list carrying at least one BLOCKING reviewer transform-block
    finding.

    Scaffolding-attributed companions are mixed in and the order is permuted, so
    the blocking issue is not always first — rule 1 scans the whole list
    (``review_verdict.py:294``) and a scan that stopped early would pass a
    first-position-only generator.

    **p43 amendment (R1.1/R1.2), under ``LGR-60c4985-6``.** The reviewer arm
    draws from :data:`_BLOCKING_SEVERITY` rather than :data:`_SEVERITY`, so
    "blocking" is now true of every draw by construction rather than by rule 1
    ignoring severity. The scaffolding companions keep the full :data:`_SEVERITY`
    vocabulary, because rules 2/3 read attribution and not severity. The
    non-blocking half of the space moved to
    :func:`_verdicts_with_only_retained_findings`, which is Property 2's
    generator — the sibling R7.2 requires, so no shape this generator used to
    reach is now unreachable.
    """
    blocking = draw(st.lists(st.tuples(_ISSUE_TEXT, _BLOCKING_SEVERITY), min_size=1, max_size=3))
    scaffolding = draw(st.lists(st.tuples(_ISSUE_TEXT, _SEVERITY), max_size=3))
    issues = [_issue(text, severity, attribution="transform_block") for text, severity in blocking]
    issues += [_issue(text, severity, attribution="template_scaffolding") for text, severity in scaffolding]
    return draw(st.permutations(issues))


@st.composite
def _verdicts_with_only_retained_findings(draw) -> list[dict]:
    """Draw an issue list whose reviewer transform-block findings are ALL
    non-blocking.

    The mirror of :func:`_verdicts_with_a_blocking_finding`, sharing its
    vocabulary (:data:`_ISSUE_TEXT`, :data:`_SEVERITY` for the companions) and
    its permutation, and differing in exactly one strategy: the reviewer arm
    draws :data:`_NON_BLOCKING_SEVERITY`. Kept beside its sibling so the one
    axis that separates a blocking finding from a retained one is visible in
    four lines of parallel structure rather than inferred.

    Both arms are still *reviewer*-attributed, so ``attribution_source`` comes
    out of the real parser as ``reviewer`` and rule 1's attribution antecedent is
    satisfied — only the severity conjunct is not. That is the whole of p43
    R1.3/R1.6: the finding is retained and displayed, and does not block.
    """
    retained = draw(st.lists(st.tuples(_ISSUE_TEXT, _NON_BLOCKING_SEVERITY), min_size=1, max_size=3))
    scaffolding = draw(st.lists(st.tuples(_ISSUE_TEXT, _SEVERITY), max_size=3))
    issues = [_issue(text, severity, attribution="transform_block") for text, severity in retained]
    issues += [_issue(text, severity, attribution="template_scaffolding") for text, severity in scaffolding]
    return draw(st.permutations(issues))


#: Every review category the prompt asks for, all reported as passing. The
#: generator emits ONLY passing categories: a failing one would fail the run by
#: itself, which is the other way this property could pass while testing nothing.
_ALL_PASS_CATEGORIES = st.lists(
    st.sampled_from(
        ("Correctness", "Schema Compliance", "Error Handling", "AWS Best Practices", "Security", "Performance")
    ),
    unique=True,
    max_size=6,
).map(lambda names: dict.fromkeys(names, "pass"))


# Feature: p43-review-gate-severity-floor, Property 1: A blocking finding always
# fails the gate.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    execution=_successful_execution_results(),
    issues=_verdicts_with_a_blocking_finding(),
    raw_verdict=st.sampled_from(("pass", "fail")),
    categories=_ALL_PASS_CATEGORIES,
    target_schema=st.sampled_from(_TARGET_SCHEMA_STATES),
)
def test_success_never_excuses_a_blocking_static_finding(execution, issues, raw_verdict, categories, target_schema):
    """**Validates: Requirements 1.1, 1.2, 2.3** — p43 Property 1. Preserves p42
    Requirement 12.1 (Property 17) by narrowing its antecedent (p43 R7.2).

    For any successful execution result from either executor, and any reviewer
    verdict carrying a transform-block-attributed issue **at a Blocking_Severity**
    — any severity not in ``review_verdict.NON_BLOCKING_SEVERITIES``, at any
    position, with any scaffolding companions, on either raw verdict —
    ``test_status`` is ``failed``, and the same run passes once that attribution
    is flipped, so the finding is the only thing failing it.

    **What reversed, and on whose authority.** The antecedent used to be any
    severity: :func:`_verdicts_with_a_blocking_finding` drew :data:`_SEVERITY`,
    including ``minor``, and the section header above pinned that over-blocking as
    current behaviour belonging to a different spec. This spec is that different
    spec — ``p43-review-gate-severity-floor``, calibrating P28b R1.4 under p43
    R1.1/R1.2 — so the generator now draws :data:`_BLOCKING_SEVERITY` and every
    assertion below stands verbatim on the narrower draw. Nothing here was
    deleted or weakened, per ``LGR-60c4985-6`` (**template / ratified**): amend
    the assertions that encoded the hold, never delete them, and keep the
    reversal legible as a reversal. The hold this reverses is stated at the
    section header above (``:726-733`` as it stood). The ``minor`` half of the old
    input space is asserted by **Property 2** immediately below, with the opposite
    outcome.
    """
    _executor, execution_results = execution
    parsed = _parsed_from(raw_verdict, issues, categories)

    status, checks = _mod.aggregate_check_results(parsed, execution_results, target_schema)

    assert status == "failed"

    verdict_check = next(c for c in checks if c["name"] == "ai_review_verdict")
    assert verdict_check["passed"] is False
    assert "effective verdict: fail" in verdict_check["detail"]

    # The execution DID succeed and is recorded as a passing check — the gate is
    # not failing because it disbelieved the run.
    execution_check = next(c for c in checks if c["name"] == "code_interpreter_execution")
    assert execution_check["passed"] is True

    # Non-vacuity control: the same execution result, the same issues, the same
    # categories and the same target-schema state — with every attribution
    # flipped to scaffolding — derive `passed`. So nothing else in this draw
    # was failing the run.
    control_issues = [{**issue, "attribution": "template_scaffolding"} for issue in issues]
    control_parsed = _parsed_from(raw_verdict, control_issues, categories)
    control_status, control_checks = _mod.aggregate_check_results(control_parsed, execution_results, target_schema)

    assert control_status == "passed"
    assert next(c for c in control_checks if c["name"] == "ai_review_verdict")["passed"] is True


# Feature: p43-review-gate-severity-floor, Property 2: A retained finding never
# fails a passing review.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    execution=_successful_execution_results(),
    issues=_verdicts_with_only_retained_findings(),
    categories=_ALL_PASS_CATEGORIES,
    target_schema=st.sampled_from(_TARGET_SCHEMA_STATES),
    flip_offset=st.integers(min_value=0, max_value=2),
    blocking_severity=_BLOCKING_SEVERITY,
)
def test_a_retained_finding_never_fails_a_passing_review(
    execution, issues, categories, target_schema, flip_offset, blocking_severity
):
    """**Validates: Requirements 1.3, 1.6, 3.1** — p43 Property 2, the sibling
    R7.2 requires beside Property 1.

    For any raw ``pass`` verdict whose reviewer transform-block findings ALL carry
    a Non_Blocking_Severity, with any scaffolding companions in any order, the
    gate returns ``pass`` and the derived ``ai_review_verdict`` check has
    ``passed = True``. Flipping any ONE of those severities to a
    Blocking_Severity returns ``fail`` — the non-vacuity control, mirroring
    Property 1's attribution flip: the severity is then the only difference
    between a passing and a failing run, which is exactly the claim.

    This is the half of the old Property 17 input space that p43 R1.1/R1.2
    reversed. Under ``LGR-60c4985-6`` (**template / ratified**) the ``minor``
    draws were not deleted when :data:`_BLOCKING_SEVERITY` narrowed Property 1's
    antecedent; they moved here and are asserted to do the opposite thing. The
    hold that stood over this space is stated at the section header above.

    Note what is NOT claimed: nothing here says a retained finding is discarded.
    It stays on the verdict and reaches the check detail (R3.1), which is why the
    assertion is about the *verdict* and the check's ``passed``, not about the
    issue list shrinking.

    Falsifiability was checked rather than assumed, following the same practice as
    Property 1 above. Each half was inverted in turn (in the working tree, then
    reverted) and confirmed to fail: ``effective_verdict(parsed) == "fail"``
    shrinks to a single ``minor`` reviewer transform-block issue on a raw ``pass``,
    and ``effective_verdict(flipped_parsed) == "pass"`` shrinks likewise with
    ``blocking_severity='critical'``. The flip is additionally self-guarding — had
    ``positions`` selected a scaffolding companion, or had the parser defaulted
    ``attribution_source``, the flipped draw would return ``pass`` and this
    assertion would fail rather than pass quietly.
    """
    _executor, execution_results = execution
    parsed = _parsed_from("pass", issues, categories)

    # The antecedent is real: these are reviewer-attributed findings that satisfy
    # rule 1's ATTRIBUTION half and are exempted only by the severity conjunct.
    retained = [i for i in parsed["issues"] if i["attribution"] == _rv.ATTRIBUTION_TRANSFORM_BLOCK]
    assert retained
    assert all(i["attribution_source"] == _rv.ATTRIBUTION_SOURCE_REVIEWER for i in retained)
    assert all(i["severity"] in _rv.NON_BLOCKING_SEVERITIES for i in retained)

    assert effective_verdict(parsed) == "pass"

    _status, checks = _mod.aggregate_check_results(parsed, execution_results, target_schema)
    verdict_check = next(c for c in checks if c["name"] == "ai_review_verdict")
    assert verdict_check["passed"] is True
    assert "effective verdict: pass" in verdict_check["detail"]

    # Non-vacuity control: flip exactly ONE retained finding's severity to a
    # Blocking_Severity and the same draw fails. Positions are computed over the
    # RAW issue dicts so the flip goes through the real parser, keeping
    # `attribution_source == "reviewer"` — a hand-built dict would not.
    positions = [n for n, issue in enumerate(issues) if issue.get("attribution") == "transform_block"]
    flipped_issues = [dict(issue) for issue in issues]
    flipped_issues[positions[flip_offset % len(positions)]]["severity"] = blocking_severity
    flipped_parsed = _parsed_from("pass", flipped_issues, categories)

    assert effective_verdict(flipped_parsed) == "fail"
    _flipped_status, flipped_checks = _mod.aggregate_check_results(flipped_parsed, execution_results, target_schema)
    assert next(c for c in flipped_checks if c["name"] == "ai_review_verdict")["passed"] is False


@pytest.mark.parametrize("executor", _EXECUTOR_SHAPES)
def test_generated_success_results_conform_to_the_declared_contract(executor):
    """The generated results are the contract's ``success`` shape, one documented
    asymmetry aside.

    Checks the strategy against ``tests/fixtures/run_test_result_shapes.py``
    rather than against either runner, so a hand-written Glue shape cannot drift
    from the declared Test_Result_Contract unnoticed. Task 8.2
    (``test_result_shape_equivalence_prop.py``) is what holds the *real*
    ``GlueSessionTestRunner`` to the same table; this only keeps the generator
    honest.

    The asymmetry: ``SUCCESS`` narrows ``conversion_report`` to ``dict``, because
    the pandas ``run_test`` always overwrites it on a path that converted. The
    Glue runner converts nothing and returns ``None`` on every path — settled by
    task 8.1, not provisional. The base contract already allows
    ``dict | None`` for that key, so the Glue shape is contract-valid; it is only
    the *narrowed* success shape it departs from, in exactly one place, asserted
    here so the departure is stated rather than tolerated.
    """
    result = _success_result(executor, completeness=100, duplicates=0, stderr="")

    assert set(result) == set(BASE_CONTRACT_KEYS)
    assert set(result["data_quality_results"]) == set(_DQ_KEYS)

    violations = shape_violations(result, SUCCESS)
    if executor == "pandas":
        assert violations == []
    else:
        assert len(violations) == 1, violations
        assert "conversion_report" in violations[0]


def test_the_blocking_finding_generator_supplies_reviewer_attribution():
    """The antecedent is real: a generated issue reaches rule 1, not rule 2.

    Rule 1 requires ``attribution_source == "reviewer"``, which the parser sets
    only for an explicit, recognised ``attribution`` value
    (``review_verdict.py:215-222``). A generator that emitted the field
    misspelled — or omitted it — would produce issues that default to
    ``transform_block`` with ``attribution_source == "default"``, which blocks a
    raw ``fail`` and NOT a raw ``pass``. Property 1 would then hold on every
    raw-``fail`` draw for the wrong reason and be a coin flip on the rest.

    **What changed here, and why it is the FIXTURE and not the assertion.** The
    fixture was ``severity="minor"``. Under p43 R1.1/R1.2 a ``minor`` reviewer
    transform-block finding no longer fires rule 1, so the final assertion would
    have inverted to ``"pass"`` — and a ``"pass"`` here is exactly what a
    *defaulted* attribution also produces on a raw pass, so the inverted test
    would no longer distinguish rule 1 from rule 2 and would stop testing its own
    subject. Nothing would then assert that rule 1 is reachable at all.
    ``requirements.md`` classified this site as "inverts"; **this is a deliberate
    departure from that classification**, taken so the subject survives: the
    fixture moves to ``critical`` and the assertion stands.

    The ``minor`` case is not lost — it is pinned two lines below, at the same
    site, as the new fact: still ``attribution_source == "reviewer"``, and yet
    ``"pass"``. Authority: ``LGR-60c4985-6`` (**template / ratified**) — amend,
    never delete, and keep the reversal legible as a reversal; the hold this
    reverses is stated at the section header above.
    """
    issues = [_issue("Transform drops null rows.", "critical", attribution="transform_block")]
    parsed = _parsed_from("pass", issues)

    assert [i["attribution"] for i in parsed["issues"]] == [_rv.ATTRIBUTION_TRANSFORM_BLOCK]
    assert [i["attribution_source"] for i in parsed["issues"]] == [_rv.ATTRIBUTION_SOURCE_REVIEWER]
    # Rule 1, on a raw PASS — the case a defaulted attribution would not reach.
    assert effective_verdict(parsed) == "fail"

    # p43 R1.3/R1.6: the same generated shape at a Non_Blocking_Severity still
    # reaches rule 1's ATTRIBUTION antecedent — the reviewer said it — and is
    # exempted by the severity conjunct alone.
    retained = _parsed_from("pass", [_issue("Transform drops null rows.", "minor", attribution="transform_block")])
    assert [i["attribution_source"] for i in retained["issues"]] == [_rv.ATTRIBUTION_SOURCE_REVIEWER]
    assert effective_verdict(retained) == "pass"
