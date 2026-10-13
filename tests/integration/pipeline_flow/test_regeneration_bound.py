# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration: the regeneration loop terminates at the bound (p32 2.17).

Task 16.1's second half. ``test_regeneration_loop_prop.py`` (task 13) proves a
second attempt is genuinely a second attempt; this file proves the loop *stops*
— a failing run regenerates, then reaches the human decision at
``DEFAULT_REGENERATION_MAX_ATTEMPTS``, and never loops indefinitely.

**Why this is an integration test and not a unit test.** The bound is enforced
in two places that must agree:

  * ``test_code``'s ``analyze_test_results_for_regeneration`` computes
    ``regeneration_needed`` and ``max_attempts_reached``, and the handler turns
    them into ``next_step`` plus the two numbers it returns; and
  * the Step Functions ``TestResultChoice`` regeneration branch re-evaluates
    the bound **on the edge itself**, from
    ``$.test_code_result.regeneration_attempt`` vs
    ``$.test_code_result.regeneration_max_attempts`` (P32 D2 — deliberately not
    trusting the Lambda's own verdict, so a regression in ``test_code`` cannot
    produce an unbounded loop).

So the loop is driven here by the REAL edge condition, read out of
``deploy-cdk/lib/orchestration-stack.ts`` rather than restated — the same
source-of-truth move ``test_test_failure_gating.py::_choice_passed_literal``
and ``tests/cdk/stacks/test_asl_state_names_pinned.py`` make. A hand-written
mirror of the condition would only prove the mirror terminates.

The counter advance between iterations is the state machine's
``ComputeNextRegenerationAttempt`` → ``IncrementRegenerationAttempt`` pair
(``States.MathAdd(attempt, 1)``), which is why the loop body below increments
by exactly one.

**Validates: Requirements 2.15, 2.16, 2.17**
"""

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, NonCallableMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_REPO = Path(__file__).resolve().parents[3]
_TEST_CODE_DIR = _REPO / "lambda" / "agentic_data_ingestion_test_code"
_ORCHESTRATION_STACK = _REPO / "deploy-cdk" / "lib" / "orchestration-stack.ts"

sys.path.insert(0, str(_TEST_CODE_DIR))
sys.path.insert(0, str(_REPO / "shared"))

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
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
}


def _load_test_code_module():
    """Load test_code's handler under a UNIQUE module name.

    ``import lambda_function`` is not safe at the integration layer: several
    Lambdas have a module by that name and whichever ran first wins
    ``sys.modules``, so a plain import silently binds another Lambda's handler
    (observed as ``'Mock' object does not support item assignment`` when this
    file was run inside the full suite instead of alone). Same loader shape as
    ``test_test_failure_gating.py::_load_test_code_aggregation``.
    """
    import importlib.util

    # Other unit files stub their Lambda's dependencies by writing MagicMocks
    # into sys.modules, and those survive into this module's import. This
    # handler needs the REAL ones (`artifact_schema.create_output_data`
    # returning a dict that line 1319 assigns into, for one), so strip every
    # mock entry for the duration of the load and put them back afterwards.
    # Without this the file passes alone and fails inside the full suite —
    # which is worse than failing both times.
    # NonCallableMock is the common base of Mock, MagicMock and friends —
    # `test_approval_system.py:52` installs a bare `Mock()`, which is NOT an
    # instance of MagicMock, so checking for MagicMock alone misses it.
    borrowed = {name: mod for name, mod in sys.modules.items() if isinstance(mod, NonCallableMock)}
    for name in borrowed:
        del sys.modules[name]

    try:
        with (
            patch.dict("sys.modules", _mods),
            patch("boto3.client", MagicMock()),
            patch("boto3.resource", MagicMock()),
        ):
            spec = importlib.util.spec_from_file_location(
                "itest_test_code_regeneration_bound",
                str(_TEST_CODE_DIR / "lambda_function.py"),
            )
            module = importlib.util.module_from_spec(spec)
            # Intra-module imports resolve against this name while it executes.
            sys.modules["lambda_function"] = module
            spec.loader.exec_module(module)
        sys.modules.pop("lambda_function", None)
    finally:
        sys.modules.update(borrowed)

    return module


_mod = _load_test_code_module()


# The review the reviewer keeps returning: a genuine transform-block failure,
# so every attempt is a regeneration trigger. This is the input under which an
# unbounded loop would actually be possible.
_REVIEW_KEEPS_FAILING = json.dumps(
    {
        "verdict": "fail",
        "issues": [
            {
                "severity": "major",
                "category": "Correctness",
                "description": "to_date called without an explicit format string",
                "attribution": "transform_block",
            }
        ],
        "categories": {"Correctness": "fail", "Security": "pass"},
    }
)

_EXEC_OK = {
    "execution_successful": True,
    "executed": True,
    "input_records": 10,
    "output_records": 10,
}

_CODE_GEN_ITEM = {
    "run_id": "run-bound",
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
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


def _fake_dynamodb() -> MagicMock:
    """A ``dynamodb`` stand-in whose pipelines table returns a keyless record.

    p33 task 4.3 plumbing, not an assertion change: ``test_code``'s
    target-schema read moved from the schema-mappings item to the pipeline
    record, so the pipelines table is now on the handler's path. A bare
    ``MagicMock()`` answers ``False`` to ``"Item" in response``, which now reads
    as a *missing* pipeline record and raises StepFailure (p33 requirement 2.8).
    The record below exists and carries no target schema — requirement 2.6's
    legitimate state — so the regeneration-bound behaviour this file is about is
    unchanged: with no target schema there is no computed schema-compliance
    entry, and the check list stays as it was.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-bound"}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def _invoke(regeneration_attempt: int) -> tuple[dict, dict]:
    """One TestCode invocation at the given attempt, through the real handler.

    Returns (response payload, test_code state row).
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value=_REVIEW_KEEPS_FAILING)
    agent.add_hook = MagicMock()
    runner = MagicMock()
    runner.run_test.return_value = _EXEC_OK

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        response = _mod.lambda_handler(
            {
                "run_id": "run-bound",
                "pipeline_id": "pipe-bound",
                "regeneration_attempt": regeneration_attempt,
            },
            None,
        )

    state_items = [r for r in _state_rows(runs_table) if r["step_name"] == "test_code"]
    assert len(state_items) == 1, "expected exactly one test_code state row per invocation"
    return response, state_items[0]


def _run_test_code(regeneration_attempt: int) -> dict:
    """The TestCode response payload the state machine branches on."""
    return _invoke(regeneration_attempt)[0]


# ── The real ASL edge condition, read from the CDK source ────────────────


def _regeneration_edge_fields() -> tuple[str, str, str]:
    """The three payload fields TestResultChoice's regeneration branch reads.

    Returns (needed_field, attempt_field, max_field). Extracted rather than
    restated so a change to the edge fails this test instead of passing a stale
    mirror of it.
    """
    source = _ORCHESTRATION_STACK.read_text(encoding="utf-8")

    needed = re.search(
        r"booleanEquals\(\s*'\$\.test_code_result\.(\w+)'\s*,\s*true\s*\)",
        source,
    )
    bound = re.search(
        r"numberLessThanJsonPath\(\s*'\$\.test_code_result\.(\w+)'\s*,\s*'\$\.test_code_result\.(\w+)'\s*,?\s*\)",
        source,
    )
    assert needed is not None, "TestResultChoice's regeneration_needed condition not found"
    assert bound is not None, "TestResultChoice's bound comparison not found"
    return needed.group(1), bound.group(1), bound.group(2)


def _regeneration_edge_taken(payload: dict) -> bool:
    """Evaluate the real edge condition against a TestCode result payload."""
    needed_field, attempt_field, max_field = _regeneration_edge_fields()
    for field in (needed_field, attempt_field, max_field):
        if field not in payload:
            return False  # the ASL isPresent guards
    if payload[needed_field] is not True:
        return False
    return payload[attempt_field] < payload[max_field]


def test_the_edge_reads_the_fields_test_code_actually_returns():
    """Guard: the loop below is only meaningful if the edge and the Lambda agree."""
    needed_field, attempt_field, max_field = _regeneration_edge_fields()
    assert (needed_field, attempt_field, max_field) == (
        "regeneration_needed",
        "regeneration_attempt",
        "regeneration_max_attempts",
    )
    payload = _run_test_code(0)
    for field in (needed_field, attempt_field, max_field):
        assert field in payload, f"TestResultChoice reads {field!r}, which test_code does not return"


def test_a_failing_run_regenerates_then_reaches_approval_at_the_bound():
    """2.17: bounded regeneration, then the human decision — never indefinite."""
    bound = _mod.DEFAULT_REGENERATION_MAX_ATTEMPTS
    # A cap strictly larger than the bound: if the loop is unbounded, we exit
    # through the `else` and fail rather than spin.
    hard_cap = bound + 5

    attempt = 0
    regenerated_from: list[int] = []
    payload = None

    for _ in range(hard_cap):
        payload = _run_test_code(attempt)
        assert payload["test_result"]["status"] == "failed", "fixture guard: every attempt must fail"

        if _regeneration_edge_taken(payload):
            assert payload["next_step"] == "code_regeneration"
            regenerated_from.append(attempt)
            # ComputeNextRegenerationAttempt → IncrementRegenerationAttempt
            attempt += 1
            continue
        break
    else:
        pytest.fail(f"the regeneration loop did not terminate within {hard_cap} attempts")

    # Exactly `bound` regenerations happened, from attempts 0..bound-1.
    assert regenerated_from == list(range(bound))
    assert attempt == bound

    # The terminal iteration falls through to the human decision.
    assert payload is not None
    assert payload["regeneration_needed"] is False
    assert payload["approval_required"] is True
    assert payload["next_step"] == "approval_check"
    assert not payload["regeneration_attempt"] < payload["regeneration_max_attempts"]

    # And the state row records WHY, so an operator reading run history sees the
    # bound rather than an unexplained gate.
    _response, state_item = _invoke(bound)
    analysis = json.loads(state_item["regeneration_analysis"])
    assert analysis["max_attempts_reached"] is True
    assert analysis["regeneration_needed"] is False
    assert state_item["regeneration_attempt"] == bound
    assert state_item["regeneration_max_attempts"] == bound


def test_the_bound_is_the_configured_default_not_a_literal():
    """2.17 pins the bound to DEFAULT_REGENERATION_MAX_ATTEMPTS, which
    ``get_regeneration_max_attempts`` reads from config — so the returned
    max must track the configured value, not a hardcoded number.
    """
    assert _run_test_code(0)["regeneration_max_attempts"] == _mod.DEFAULT_REGENERATION_MAX_ATTEMPTS

    with patch.dict("os.environ", {"CODE_REGENERATION_MAX_ATTEMPTS": "1"}):
        one = _run_test_code(0)
    assert one["regeneration_max_attempts"] == 1
    assert one["regeneration_needed"] is True

    with patch.dict("os.environ", {"CODE_REGENERATION_MAX_ATTEMPTS": "1"}):
        spent = _run_test_code(1)
    assert spent["regeneration_needed"] is False
    assert spent["next_step"] == "approval_check"
    assert _regeneration_edge_taken(spent) is False


def test_a_zero_bound_never_regenerates():
    """The degenerate configuration: 0 attempts means straight to the human,
    with no edge taken at all (``get_regeneration_max_attempts`` accepts 0).
    """
    with patch.dict("os.environ", {"CODE_REGENERATION_MAX_ATTEMPTS": "0"}):
        payload = _run_test_code(0)

    assert payload["regeneration_needed"] is False
    assert payload["next_step"] == "approval_check"
    assert payload["approval_required"] is True
    assert _regeneration_edge_taken(payload) is False
