# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: test_code honest per-check aggregation (P28 R4.3, R4.4).

The test step must report an honest status derived from every executed
check — ANY failed check → "failed". The previous behaviour is removed:

  * hardcoded ``"status": "success"`` state item (written even on failure),
  * the majority-fail approval heuristic (only escalate when
    tests_failed > tests_passed),
  * the execution-success status override (forced ``test_status="passed"``),
  * the dead ``next_step: "code_regeneration"`` reference (no SFN state
    backed it at the time).

p32 2.17 REVERSES that last item, deliberately (V6). P28 R4.4 removed a branch
that pointed at nothing; P32 D2 builds the destination first — an in-execution
``TestResultChoice`` → ``CreateCode`` edge carrying an incremented
``regeneration_attempt`` — so the name now resolves to a real state. Two P28-era
assertions changed with it, and the change is on purpose rather than a
regression: ``test_handler_next_step_never_references_code_regeneration`` is
AMENDED (its name kept, its docstring rewritten), and the grep assertion
``test_no_code_regeneration_string_remains_in_lambda_source`` is REPLACED by
``test_failing_run_reaches_approval_once_max_attempts_reached``, which pins the
bound instead of the absence of a string. Everything else here — including the
P28 R4.3 honesty guards and the LGR-334667e-1 verdict matrix — is untouched.

The structured result ``{status, checks: [{name, passed, detail}],
report_s3_uri}`` is carried in the state item and the returned payload —
it is the stable contract the SFN TestResultChoice (task 5.2) branches on
via ``$.test_code_result.test_result.status``.

Validates: P28 Requirements 4.3, 4.4
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Region/account before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# p32 Defect 5: the converter and the runner are the REAL modules, so the
# refusal shapes these tests aggregate are genuinely computed rather than
# hand-written — a drift between the runner and the aggregation shows up here
# instead of in production. Neither import touches AWS (the bedrock-agentcore
# client is created in CodeInterpreterTestRunner.__init__, not at import).
from code_interpreter_runner import (
    NOT_VERIFIED_REASON_NOT_YET,
    NOT_VERIFIED_REASON_PERMANENT,
    CodeInterpreterTestRunner,
)
from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

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


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _parsed(categories: dict[str, bool]) -> dict:
    """Build a parse_test_report-shaped dict from {category: passed}.

    P28 R10.6 (task 9.4): parse_test_report returns only structural data —
    per-category verdicts and the reviewer recommendation. The LLM-invented
    numeric fields (total_tests/tests_passed/tests_failed/coverage_percent/
    overall_status) no longer exist.
    """
    cats = {name: {"status": "PASSED" if ok else "FAILED", "passed": ok} for name, ok in categories.items()}
    return {
        "test_categories": cats,
        "recommendation": "APPROVE",
    }


_EXEC_OK = {"execution_successful": True, "input_records": 10, "output_records": 10}
_EXEC_FAIL = {"execution_successful": False, "error_details": "IndentationError: unexpected indent"}


# ---------------------------------------------------------------------------
# aggregate_check_results — the honest aggregation
# ---------------------------------------------------------------------------


def test_all_checks_pass_yields_passed():
    parsed = _parsed({"Correctness": True, "Security": True, "Performance": True})
    status, checks = _mod.aggregate_check_results(parsed, _EXEC_OK)

    assert status == "passed"
    assert len(checks) == 4  # 3 static categories + execution
    assert all(c["passed"] for c in checks)
    for check in checks:
        assert set(check) == {"name", "passed", "detail"}


def test_one_of_five_checks_failing_yields_failed_anti_majority_case():
    """The anti-majority regression: 1 failed out of 5 → failed.

    The old heuristic only escalated when a MAJORITY of checks failed
    (tests_failed > tests_passed); a single failing check sailed through.
    """
    parsed = _parsed(
        {
            "Correctness": True,
            "Schema Compliance": True,
            "Error Handling": True,
            "Security": False,  # 1 of 4 static checks fails
        }
    )
    status, checks = _mod.aggregate_check_results(parsed, _EXEC_OK)

    assert status == "failed"
    assert len(checks) == 5
    failed = [c for c in checks if not c["passed"]]
    assert [c["name"] for c in failed] == ["static_analysis:Security"]


def test_execution_failure_alone_yields_failed():
    """Execution failure counts — no more 'don't penalize' exemption."""
    parsed = _parsed({"Correctness": True, "Security": True})
    status, checks = _mod.aggregate_check_results(parsed, _EXEC_FAIL)

    assert status == "failed"
    exec_check = next(c for c in checks if c["name"] == "code_interpreter_execution")
    assert exec_check["passed"] is False
    assert "IndentationError" in exec_check["detail"]


def test_execution_success_does_not_override_static_failures():
    """Old behaviour forced test_status='passed' on execution success."""
    parsed = _parsed({"Correctness": False, "Security": True})
    status, _ = _mod.aggregate_check_results(parsed, _EXEC_OK)

    assert status == "failed"


def test_missing_execution_results_is_a_failed_check():
    parsed = _parsed({"Correctness": True})
    status, checks = _mod.aggregate_check_results(parsed, None)

    assert status == "failed"
    exec_check = next(c for c in checks if c["name"] == "code_interpreter_execution")
    assert exec_check["passed"] is False


# ---------------------------------------------------------------------------
# determine_code_approval_requirement — majority heuristic deleted
# ---------------------------------------------------------------------------


def test_minority_failure_still_requires_approval():
    """1 failed / 4 passed with status 'failed' → approval required.

    Under the deleted majority heuristic (tests_failed > tests_passed) this
    returned False. P28 R10.6 (task 9.4): inputs are executed-check counts.
    """
    required = _mod.determine_code_approval_requirement(
        test_status="failed",
        checks_run=5,
        checks_passed=4,
        review_verdict={"verdict": "pass", "issues": [], "categories": []},
        regeneration_analysis={"regeneration_needed": False, "max_attempts_reached": False},
    )
    assert required is True


def test_all_passed_requires_no_approval():
    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=5,
        checks_passed=5,
        review_verdict={"verdict": "pass", "issues": [], "categories": []},
        regeneration_analysis={"regeneration_needed": False, "max_attempts_reached": False},
    )
    assert required is False


# ---------------------------------------------------------------------------
# Full handler — structured result carried in state item + response
# ---------------------------------------------------------------------------

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {
        "code_version_id": "cv-1",
        "code_s3_uri": "s3://code-bucket/code.py",
    },
}


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


#: A pipeline record that exists and carries NO target schema — requirement
#: 2.6's legitimate state. Needed since p33 task 4.3: the handler now reads the
#: pipeline record before anything else and raises StepFailure when it is
#: absent, so a bare ``MagicMock()`` for ``dynamodb`` (whose ``get_item``
#: response answers ``False`` to ``"Item" in ...``) reads as a missing record
#: and fails every handler test here. This keeps the check lists in this file
#: exactly as they were: no target schema means no computed schema-compliance
#: entry, so nothing about honest aggregation moves.
_PIPELINE_RECORD_NO_TARGET_SCHEMA = {"pipeline_id": "pipe-1", "pipeline_name": "honest-aggregation"}

#: The same record with the Code_Approval_Setting stated explicitly as the JSON
#: boolean ``false``. Needed since p34 task 2.4: the pipeline record now also
#: carries the setting the Approval_Policy reads
#: (``resolve_code_approval_setting``, ``test_code/lambda_function.py:702``), and
#: the record above carries no ``approval_config`` — which p34 requirement 1.2
#: resolves to ``True``, so a run seeded with it reports a merged
#: ``approval_required`` of ``True`` however clean it was. A test whose subject is
#: where a CLEAN run goes therefore has to say that routine approvals are off.
_PIPELINE_RECORD_APPROVAL_DISABLED = {
    **_PIPELINE_RECORD_NO_TARGET_SCHEMA,
    "approval_config": {"code_generation_enabled": False},
}


def _fake_dynamodb(pipeline_record: dict | None = _PIPELINE_RECORD_NO_TARGET_SCHEMA) -> MagicMock:
    """A ``dynamodb`` resource stand-in whose pipelines table answers.

    p33 task 4.3 plumbing, not an assertion change: the handler's target-schema
    read moved from the schema-mappings item to the pipeline record, so the
    pipelines table is now on the handler's path and has to be stubbed.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": dict(pipeline_record)} if pipeline_record else {}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def _run_handler(
    review_text: str,
    execution_results: dict,
    regeneration_attempt: int | None = None,
    pipeline_record: dict = _PIPELINE_RECORD_NO_TARGET_SCHEMA,
):
    """Run lambda_handler with all AWS/agent dependencies mocked.

    ``regeneration_attempt`` is left OUT of the event when None so the existing
    callers keep exercising the absent-key path (p32 3.18: first-attempt
    behaviour is identical whether or not the key is present). Pass an int to
    drive the attempt bound.

    ``pipeline_record`` defaults to the config-absent record every caller here was
    written against; pass :data:`_PIPELINE_RECORD_APPROVAL_DISABLED` to switch the
    pipeline's routine code-generation approvals off (p34 task 4.1).
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    results_table = MagicMock()
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value=review_text)
    agent.add_hook = MagicMock()

    runner = MagicMock()
    runner.run_test.return_value = execution_results

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", results_table),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb(pipeline_record)),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        event: dict = {"run_id": "run-1", "pipeline_id": "pipe-1"}
        if regeneration_attempt is not None:
            event["regeneration_attempt"] = regeneration_attempt
        response = _mod.lambda_handler(event, None)

    testing_items = [i for i in _state_rows(runs_table) if i.get("step_name") == "test_code"]
    assert len(testing_items) == 1, "expected exactly one test_code state item"
    return response, testing_items[0]


# P28 R13.1 (task 11.2): the review model returns a structured JSON verdict —
# these fixtures are what the (mocked) agent emits.
_REVIEW_ALL_PASS = json.dumps(
    {
        "verdict": "pass",
        "issues": [],
        "categories": {"Correctness": "pass", "Security": "pass", "Performance": "pass"},
    }
)

_REVIEW_ONE_FAIL = json.dumps(
    {
        "verdict": "fail",
        "issues": [
            {"severity": "major", "category": "Security", "description": "Credentials are logged in plaintext."}
        ],
        "categories": {
            "Correctness": "pass",
            "Schema Compliance": "pass",
            "Error Handling": "pass",
            "Security": "fail",
        },
    }
)


def test_handler_all_pass_reports_passed_everywhere():
    response, state_item = _run_handler(_REVIEW_ALL_PASS, _EXEC_OK)

    assert response["test_status"] == "passed"
    assert response["test_result"]["status"] == "passed"
    assert state_item["status"] == "success"
    assert state_item["test_status"] == "passed"
    assert state_item["test_result"]["status"] == "passed"


def test_handler_one_failed_check_reports_failed_no_hardcoded_success():
    """1 of 5 checks failing → failed status in state item AND response."""
    response, state_item = _run_handler(_REVIEW_ONE_FAIL, _EXEC_OK)

    # Response payload — the SFN Choice contract
    assert response["test_status"] == "failed"
    assert response["test_result"]["status"] == "failed"

    # State item — hardcoded "success" removed, status reflects aggregation
    assert state_item["status"] == "failed"
    assert state_item["test_status"] == "failed"
    assert state_item["test_result"]["status"] == "failed"

    # Failed run still requires approval (minority failure: 1 of 5)
    assert response["approval_required"] is True
    assert state_item["approval_required"] is True


def test_handler_carries_checks_list_in_state_item_and_response():
    response, state_item = _run_handler(_REVIEW_ONE_FAIL, _EXEC_OK)

    for container in (response["test_result"], state_item["test_result"]):
        checks = container["checks"]
        # P28 R13.1: the structured review verdict participates as its own
        # check — 1 verdict + 4 static categories + execution.
        assert len(checks) == 6
        assert {c["name"] for c in checks} == {
            "ai_review_verdict",
            "static_analysis:Correctness",
            "static_analysis:Schema Compliance",
            "static_analysis:Error Handling",
            "static_analysis:Security",
            "code_interpreter_execution",
        }
        for check in checks:
            assert isinstance(check["passed"], bool)
            assert check["detail"]
        assert {c["name"] for c in checks if not c["passed"]} == {
            "ai_review_verdict",
            "static_analysis:Security",
        }


def test_handler_report_s3_uri_present_and_consistent():
    response, state_item = _run_handler(_REVIEW_ALL_PASS, _EXEC_OK)

    report_uri = response["test_result"]["report_s3_uri"]
    assert report_uri.startswith("s3://")
    assert report_uri.endswith("/test_report.md")
    assert response["test_report_s3_uri"] == report_uri
    assert state_item["test_result"]["report_s3_uri"] == report_uri
    assert state_item["test_report_s3_uri"] == report_uri


def test_handler_next_step_never_references_code_regeneration():
    """AMENDED for p32 2.17 — this REVERSES P28 R4.4, deliberately.

    The function name is kept verbatim because P28 R4.4 pinned the hold at
    exactly this identifier (`LGR-5906ec6-2`'s pattern: a decision encoded as a
    test assertion, so enabling the held behaviour means changing a test on
    purpose). Reading the name as the current contract is wrong — read this
    docstring instead.

    R4.4 removed `next_step: "code_regeneration"` because nothing backed it:
    the attempt counter was never set, so `regeneration_needed` was True on
    every failing run and its only consumer was the approval gate (p32 1.16).
    P32 D2 builds the destination first — the TestResultChoice → CreateCode
    edge with an incremented counter — so the failing in-bounds outcome
    changes from "approval_check" to "code_regeneration".

    Two things do NOT change, and are asserted here:
      * the all-pass branch still routes to create_glue_pipeline, and
      * a failing run still reports approval_required, so if the regeneration
        edge's guards do not match, the forced review's approve-anyway path
        still hits a gate (3.21/P28 R4.5).

    AMENDED AGAIN for p34 task 4.1 — the all-pass branch REVERSES, deliberately,
    and the name stays verbatim for the same reason it did the first time
    (`LGR-5906ec6-2` and `LGR-60c4985-6`, both **template / ratified**: a held
    assertion is amended, never deleted, and the amendment must read as a reversal
    rather than as a regression).

    **The surviving p32 subject is that regeneration is not the all-pass
    destination.** That is what the first amendment's "all-pass branch assertion
    still stands" line was protecting, and it is untouched by p34.

    What changed is that ``next_step`` now derives from the MERGED Approval_Policy
    decision (p34 requirement 3.2), and requirement 1.2 resolves an absent
    Code_Approval_Setting to ``True``. So a clean run on the config-absent record
    routes to ``approval_check`` — a human, which is the whole point of p34 — and
    the p32 subject is carried by a second case seeded with the setting explicitly
    ``false``, where the destination is still ``create_glue_pipeline``. Both cases
    are asserted below, and NEITHER is ``code_regeneration``, which is the claim
    this test's name makes.
    """
    # In bounds (attempt 0 of DEFAULT 2): the conclusion routes to
    # regeneration, not to a human.
    response, state_item = _run_handler(_REVIEW_ONE_FAIL, _EXEC_OK)
    assert response["next_step"] == "code_regeneration"
    assert response["regeneration_needed"] is True
    assert response["regeneration_attempt"] == 0
    assert response["regeneration_max_attempts"] == _mod.DEFAULT_REGENERATION_MAX_ATTEMPTS
    # The fail-safe: still True, because the SFN's regeneration edge is what
    # diverts the run, not this flag (see determine_code_approval_requirement).
    assert response["approval_required"] is True
    assert state_item["approval_required"] is True

    # AMENDED for p34 task 4.1. The all-pass branch on a record carrying NO
    # `approval_config` now routes to the human, because requirement 1.2 resolves
    # the absent Code_Approval_Setting to True and requirement 2.2 makes an
    # enabled setting force the gate whatever the quality gate concluded. The
    # quality gate itself still says the run is clean — asserted on its own field
    # (requirement 3.1), which is what makes this a routing change and not a
    # verdict change.
    response_ok, _ = _run_handler(_REVIEW_ALL_PASS, _EXEC_OK)
    assert response_ok["next_step"] == "approval_check"
    assert response_ok["regeneration_needed"] is False
    assert response_ok["quality_gate_requires_approval"] is False
    assert response_ok["approval_required"] is True

    # The p32 subject, preserved: with routine approvals explicitly off, an
    # all-pass run's destination is create_glue_pipeline — NOT code_regeneration.
    # p32 removed a branch that pointed at nothing and this test pins that the
    # branch has not come back as the clean-run destination; p34 changes which
    # input decides between the human and the pipeline, not that.
    response_no_gate, _ = _run_handler(_REVIEW_ALL_PASS, _EXEC_OK, pipeline_record=_PIPELINE_RECORD_APPROVAL_DISABLED)
    assert response_no_gate["next_step"] == "create_glue_pipeline"
    assert response_no_gate["regeneration_needed"] is False
    assert response_no_gate["quality_gate_requires_approval"] is False
    assert response_no_gate["approval_required"] is False


def test_failing_run_reaches_approval_once_max_attempts_reached():
    """p32 2.17 — REPLACES the P28 R4.4 grep assertion.

    The test this supersedes was
    ``test_no_code_regeneration_string_remains_in_lambda_source``: a grep over
    ``test_code/lambda_function.py`` asserting the literal ``code_regeneration``
    appeared nowhere. That assertion pinned the ABSENCE OF A STRING. Now that
    the route exists, the thing worth pinning is the BOUND — a failing
    pipeline must not be able to regenerate indefinitely (2.17).

    At ``regeneration_attempt == DEFAULT_REGENERATION_MAX_ATTEMPTS`` the same
    failing review that regenerated at attempt 0 must instead reach the human
    decision. This is also what stops the SFN loop: the regeneration edge's
    own condition is ``regeneration_attempt < regeneration_max_attempts`` over
    these two returned numbers.
    """
    bound = _mod.DEFAULT_REGENERATION_MAX_ATTEMPTS

    # One attempt short of the bound → still regenerates.
    in_bounds, _ = _run_handler(_REVIEW_ONE_FAIL, _EXEC_OK, regeneration_attempt=bound - 1)
    assert in_bounds["regeneration_needed"] is True
    assert in_bounds["next_step"] == "code_regeneration"
    assert in_bounds["regeneration_attempt"] < in_bounds["regeneration_max_attempts"]

    # At the bound → no further regeneration, human decision instead.
    at_bound, state_item = _run_handler(_REVIEW_ONE_FAIL, _EXEC_OK, regeneration_attempt=bound)
    assert at_bound["regeneration_needed"] is False
    assert at_bound["next_step"] == "approval_check"
    assert at_bound["approval_required"] is True
    assert not at_bound["regeneration_attempt"] < at_bound["regeneration_max_attempts"]

    # max_attempts_reached is recorded in the state row's serialised analysis.
    analysis = json.loads(state_item["regeneration_analysis"])
    assert analysis["max_attempts_reached"] is True
    assert analysis["regeneration_needed"] is False

    # Past the bound stays out of bounds — no way back into the loop.
    beyond, _ = _run_handler(_REVIEW_ONE_FAIL, _EXEC_OK, regeneration_attempt=bound + 5)
    assert beyond["regeneration_needed"] is False
    assert beyond["next_step"] == "approval_check"


def test_response_test_result_is_json_serializable():
    """The SFN payload contract must serialize (no Decimals/sets in checks)."""
    response, _ = _run_handler(_REVIEW_ONE_FAIL, _EXEC_OK)
    json.dumps(response["test_result"])


# ---------------------------------------------------------------------------
# p32 Defect 5 (C4) — a pandas-proxy refusal is a NON-VERIFICATION
#
# The converter exists so the transform can be executed as a *proxy* for
# Spark. When the proxy cannot express the transform, nothing about that
# transform was proven either way — so it is neither a pass nor a failure.
# The refusal itself is untouched (P28 R10.2, p32 3.7: name the construct,
# never comment out, never drop); what changes is how it is classified
# downstream (p32 V4).
#
# The shapes below are the REAL ones: the real converter refuses, and the
# real runner's structured result carries that refusal. Nothing is
# hand-written, so a drift between the runner and the aggregation is visible
# here rather than in production.
#
# Validates: Requirements 2.20, 2.21, 2.23, 2.24; 3.22
# ---------------------------------------------------------------------------

# p32 2.23 reason (a) — a construct the converter cannot YET express, so the
# refusal is a defect in the checker rather than a statement about the transform.
#
# This was p32 1.7 (two chained `.withColumn` on one statement) until task T10's
# AST converter made that shape CONVERT, which is what 2.7 requires. Every
# assertion below is unchanged; only the input moved to a shape that still
# refuses — a chain mixing frame operations, the same defect class as
# `pyspark_templates/README.md`'s parenthesised fluent worked example (1.12).
_NOT_YET_BLOCK = 'df_transformed = df.withColumnRenamed("old", "new").withColumn("a", trim(col("x"))).drop("tmp")'

# p32 3.7 / 2.23 — no single-frame pandas equivalent. Keeps refusing after the
# AST rewrite, and is nothing to fix.
_PERMANENT_BLOCK = 'df_transformed = df.groupBy("state").agg(count("name"))'


def _refusal_results(block: str) -> tuple[dict, ConversionError]:
    """Real refusal: real converter, real ConversionError, real result shape."""
    try:
        PySpark2PandasConverter.convert(block)
    except ConversionError as exc:
        return CodeInterpreterTestRunner._conversion_failure_results(exc), exc
    raise AssertionError(f"expected {block!r} to be refused by the converter")


def _real_execution_failure() -> dict:
    """3.22's converse: the transform RAN and failed (real runner shape)."""
    return CodeInterpreterTestRunner._failed_execution_results(
        "transform execution",
        {"failure_reason": "exit code 1", "exit_code": 1, "stderr": "Traceback (most recent call last):\nKeyError"},
    )


def test_proxy_refusal_is_a_not_verified_check_not_a_failed_execution():
    """2.20 — the refusal is recorded as a check distinct from a failure."""
    execution_results, exc = _refusal_results(_NOT_YET_BLOCK)
    parsed = _parsed({"Correctness": True, "Security": True})

    status, checks = _mod.aggregate_check_results(parsed, execution_results)

    not_verified = [c for c in checks if _mod.is_not_verified_check(c)]
    assert len(not_verified) == 1
    entry = not_verified[0]
    assert entry["name"] == _mod.NOT_VERIFIED_CHECK_NAME
    assert entry["verified"] is False
    # 2.20 — the construct and the line are named, as text (LGR-99050e6-1).
    assert exc.construct in entry["detail"]
    assert str(exc.line_number) in entry["detail"]
    assert "NOT VERIFIED" in entry["detail"]

    # 2.20 — no execution check at all: nothing executed, so there is nothing
    # to report about an execution.
    assert not [c for c in checks if c["name"] == "code_interpreter_execution"]
    # 2.21 — a refusal alone does not fail the gate.
    assert status != "failed"


def test_refusal_only_run_does_not_trigger_regeneration_or_require_approval():
    """2.21 — nothing to regenerate: the PySpark is correct, the checker is not."""
    execution_results, _exc = _refusal_results(_NOT_YET_BLOCK)
    parsed = _parsed({"Correctness": True, "Security": True})
    review_verdict = {"verdict": "pass", "issues": [], "categories": {}}

    status, checks = _mod.aggregate_check_results(parsed, execution_results)
    verified = [c for c in checks if not _mod.is_not_verified_check(c)]
    checks_run = len(verified)
    checks_passed = sum(1 for c in verified if c["passed"])

    analysis = _mod.analyze_test_results_for_regeneration(
        status, checks_run, checks_passed, review_verdict, regeneration_attempt=0
    )
    assert analysis["regeneration_needed"] is False

    # determine_code_approval_requirement fires on regeneration_needed FIRST
    # (:410-412), ahead of its test-status branch — so both have to be clean.
    assert (
        _mod.determine_code_approval_requirement(status, checks_run, checks_passed, review_verdict, analysis) is False
    )


def test_the_two_refusal_reasons_stay_distinguishable():
    """2.23 — permanently inexpressible vs not yet expressible, in text."""
    not_yet_results, _ = _refusal_results(_NOT_YET_BLOCK)
    permanent_results, _ = _refusal_results(_PERMANENT_BLOCK)

    assert not_yet_results["not_verified_reason"] == NOT_VERIFIED_REASON_NOT_YET
    assert permanent_results["not_verified_reason"] == NOT_VERIFIED_REASON_PERMANENT

    parsed = _parsed({"Correctness": True})
    _, not_yet_checks = _mod.aggregate_check_results(parsed, not_yet_results)
    _, permanent_checks = _mod.aggregate_check_results(parsed, permanent_results)

    not_yet_entry = next(c for c in not_yet_checks if _mod.is_not_verified_check(c))
    permanent_entry = next(c for c in permanent_checks if _mod.is_not_verified_check(c))

    assert not_yet_entry["not_verified_reason"] != permanent_entry["not_verified_reason"]
    assert not_yet_entry["detail"] != permanent_entry["detail"]
    # Only the not-yet kind is something to fix — the text has to say so.
    assert "defect in the checker" in not_yet_entry["detail"]
    assert "nothing to fix" in permanent_entry["detail"]


def test_a_transform_that_ran_and_failed_is_still_a_failed_check():
    """3.22's converse — reclassifying a refusal must not reclassify a failure."""
    execution_results = _real_execution_failure()
    assert execution_results["executed"] is True
    assert "not_verified_by_proxy" not in execution_results

    status, checks = _mod.aggregate_check_results(_parsed({"Correctness": True}), execution_results)

    exec_check = next(c for c in checks if c["name"] == "code_interpreter_execution")
    assert exec_check["passed"] is False
    assert not _mod.is_not_verified_check(exec_check)
    assert status == "failed"

    review_verdict = {"verdict": "pass", "issues": [], "categories": {}}
    analysis = _mod.analyze_test_results_for_regeneration(status, len(checks), 1, review_verdict)
    assert _mod.determine_code_approval_requirement(status, len(checks), 1, review_verdict, analysis) is True


def test_an_unavailable_code_interpreter_run_is_still_a_failed_check():
    """3.22's converse — that path proved nothing AND named no reason."""
    execution_results = CodeInterpreterTestRunner._empty_results("bedrock-agentcore client not available")
    assert "not_verified_by_proxy" not in execution_results

    status, checks = _mod.aggregate_check_results(_parsed({"Correctness": True}), execution_results)

    exec_check = next(c for c in checks if c["name"] == "code_interpreter_execution")
    assert exec_check["passed"] is False
    assert not _mod.is_not_verified_check(exec_check)
    assert status == "failed"


def test_a_refusal_does_not_rescue_a_failing_static_check():
    """3.23 / LGR-334667e-1 — no fourth outcome lets a static failure through."""
    execution_results, _ = _refusal_results(_NOT_YET_BLOCK)

    status, checks = _mod.aggregate_check_results(_parsed({"Correctness": False, "Security": True}), execution_results)

    assert status == "failed"
    assert [c["name"] for c in checks if not c["passed"] and not _mod.is_not_verified_check(c)] == [
        "static_analysis:Correctness"
    ]


def test_a_run_with_nothing_verifiable_is_not_reported_as_passed():
    """A refusal with no other check proved nothing — same posture as 3.22."""
    execution_results, _ = _refusal_results(_NOT_YET_BLOCK)

    status, checks = _mod.aggregate_check_results({}, execution_results)

    assert [c["name"] for c in checks] == [_mod.NOT_VERIFIED_CHECK_NAME]
    assert status == "failed"


def test_handler_carries_the_not_verified_check_into_the_state_row():
    """2.20 — "not verified" is not implemented as "not reported".

    AMENDED for p34 task 4.1 — the gate assertions REVERSE, deliberately, and the
    name is kept verbatim (`LGR-5906ec6-2`, `LGR-60c4985-6`, both **template /
    ratified**: a held assertion is amended rather than deleted, and the amendment
    must stay legible as a reversal).

    **The surviving subject is p32 2.21**: a pandas-proxy refusal alone neither
    fails the step nor forces the gate. That is a statement about the *quality*
    gate, so it now lives on the Quality_Verdict_Field
    ``quality_gate_requires_approval`` (p34 requirement 3.1) — the quality gate's
    own answer, with no configuration input. The amendment makes this test say what
    it always meant.

    ``approval_required`` is now the MERGED Approval_Policy decision (requirement
    3.2), so it and ``next_step`` follow the pipeline's configuration rather than
    the refusal: on the config-absent record requirement 1.2 resolves the setting
    to ``True`` and the run gates; with the setting explicitly ``false`` it does
    not. Both are asserted, which is what shows the refusal is not what moved
    either of them.

    ``test_result["status"] == "passed"`` is unchanged — the ``TestResultChoice``
    branch named in the comment below still takes the passed arm.
    """
    execution_results, exc = _refusal_results(_NOT_YET_BLOCK)
    response, state_item = _run_handler(_REVIEW_ALL_PASS, execution_results)

    for container in (response["test_result"], state_item["test_result"]):
        entry = next(c for c in container["checks"] if c["name"] == _mod.NOT_VERIFIED_CHECK_NAME)
        assert entry["verified"] is False
        assert exc.construct in entry["detail"]
        assert str(exc.line_number) in entry["detail"]
        assert entry["not_verified_reason"] == NOT_VERIFIED_REASON_NOT_YET
        assert not [c for c in container["checks"] if c["name"] == "code_interpreter_execution"]

    # 2.21 — the refusal alone neither fails the step nor forces the gate, so
    # the SFN TestResultChoice ($.test_code_result.test_result.status) takes
    # the "passed" branch instead of ForceTestReviewApproval.
    assert response["test_result"]["status"] == "passed"
    assert state_item["status"] == "success"
    # AMENDED per p34 requirement 3.1 — 2.21's "does not force the gate" is a
    # statement about the QUALITY gate, and this is where it now lives.
    assert response["quality_gate_requires_approval"] is False
    assert state_item["quality_gate_requires_approval"] is False

    # The merged field and next_step follow the CONFIGURATION, not the refusal.
    # Config absent → requirement 1.2 resolves the setting to True, so the run
    # gates; the quality gate above is unmoved by that.
    assert response["approval_required"] is True
    assert response["next_step"] == "approval_check"
    assert state_item["approval_decision_inputs"] == {
        "pipeline_config": True,
        "quality_gate": False,
        "regeneration": False,
    }

    # Same refusal, routine approvals explicitly off → no gate, and the
    # destination is create_glue_pipeline exactly as it was before p34. The
    # refusal did not move either value; the configuration did.
    off_response, off_state_item = _run_handler(
        _REVIEW_ALL_PASS, execution_results, pipeline_record=_PIPELINE_RECORD_APPROVAL_DISABLED
    )
    assert off_response["quality_gate_requires_approval"] is False
    assert off_response["approval_required"] is False
    assert off_response["next_step"] == "create_glue_pipeline"
    assert off_state_item["status"] == "success"

    # P28 R10.6 stays honest: the not-verified check is not counted as run or
    # passed (nothing executed), but it is still reported.
    assert response["test_results"]["checks_run"] == 4  # verdict + 3 categories
    assert response["test_results"]["checks_passed"] == 4
    assert response["test_results"]["checks_failed"] == 0
    assert len(response["test_result"]["checks"]) == 5


def test_not_verified_check_is_json_serializable():
    """The SFN payload contract must serialize the third kind of check too."""
    execution_results, _ = _refusal_results(_NOT_YET_BLOCK)
    response, _ = _run_handler(_REVIEW_ALL_PASS, execution_results)
    json.dumps(response["test_result"])
