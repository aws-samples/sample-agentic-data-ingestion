# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression: the handler's FAILURE-path state-row write must not clobber the row.

Sibling of ``test_state_row_write_preserves_feedback.py``, which covers the same
defect on the success path. Both writers land on ONE DynamoDB item —
``{run_id, step_name=test_code}`` on the pipeline-runs table — and the failure
path was still a full-row ``put_item`` carrying nine attributes:

    run_id, step_name, status, step_sequence, timestamp, pipeline_id,
    original_pipeline_id, error_message, error_type

``PutItem`` replaces the WHOLE item, so a failing invocation reduced the row to
those nine and deleted ``output_data``, ``test_result``, ``test_output_s3_uri``,
``regeneration_feedback``, the approval fields and everything else a previous
invocation had written. That is the rule ``build_state_row_update``'s docstring
exists to state — **no writer clobbers attributes it does not own** — reached by
the other door. The success path was converted for exactly this reason; the
error path was not.

**Why the SEQUENCE is the test.** A test that drives only the error path passes
against the broken code: with nothing on the row beforehand, a full-row PutItem
and a SET update are indistinguishable. The defect exists only in the ordering,
so every assertion below is made after TWO invocations against the same
``(run_id, test_code)`` item on a real (moto-backed) table, reading the item back
with ``get_item``. Regeneration makes that ordering routine rather than exotic:
``DEFAULT_REGENERATION_MAX_ATTEMPTS`` is 2, so the state machine re-enters
``TestCode`` in the same execution, on the same ``run_id``.

The failure path is ALSO frequently the first writer for the row (a failure
before the success write ever runs), so ``test_error_path_creates_the_row_when_it_is_the_first_writer``
pins the upsert empirically rather than trusting that ``UpdateItem`` creates
missing items.

**Validates: Requirements 2.16**
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

# Pre-import the condition classes before the patch.dict(sys.modules) block
# below (see test_store_regeneration_feedback.py:70-74): a re-imported
# boto3.dynamodb.conditions carries Key/Attr classes moto's isinstance checks
# do not recognize.
import boto3.dynamodb.conditions  # noqa: F401  (import order is the point)

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, _SHARED_DIR)

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


def _load_module(alias: str, path: str):
    """Load a module by file path under a unique alias (no sys.modules clash)."""
    import importlib.util

    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


_step_names = _load_module("p42_errpath_step_names", os.path.join(_SHARED_DIR, "step_names.py"))

RUN_ID = "run-2026-08-18-19-45-13-a158b7e5"
PIPELINE_ID = "pipe-p42-errpath"

#: A pass verdict, so the successful invocation leaves a row whose test verdict
#: is ``passed``. That is what makes the staleness question concrete: after the
#: failing invocation the row must not still claim a passing test.
_REVIEW_PASS = json.dumps(
    {
        "verdict": "pass",
        "issues": [],
        "categories": {"Correctness": "pass", "Security": "pass"},
    }
)

_EXEC_OK = {"execution_successful": True, "executed": True, "input_records": 10, "output_records": 10}

_CODE_GEN_ROW = {
    "run_id": RUN_ID,
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
}

#: The message the second invocation fails with. Asserted verbatim on the row so
#: the failure recorded is the one this test caused, not an incidental one.
_BOOM = "review agent unavailable"

#: Attributes the SUCCESSFUL invocation writes that the failure path does not own
#: and must therefore leave alone. Pinned as a list rather than sampled: this is
#: the exact set a full-row PutItem deleted, so a partial regression fails here.
PRESERVED_ATTRIBUTES = (
    "regeneration_attempt",
    "regeneration_max_attempts",
    "approval_required",
    "quality_gate_requires_approval",
    "approval_decision_inputs",
    "regeneration_analysis",
    "output_data",
    "test_execution_id",
    "test_output_s3_uri",
    "test_report_s3_uri",
    "agent_session_id",
)


@pytest.fixture
def state_table(mock_dynamodb_tables):
    """The real (moto) pipeline-runs/state table: PK run_id, SK step_name."""
    return mock_dynamodb_tables["agentic_data_ingestion_state"]


def _fake_dynamodb() -> MagicMock:
    """A ``dynamodb`` stand-in whose pipelines table returns a keyless record.

    Same shape as ``test_state_row_write_preserves_feedback.py:_fake_dynamodb``:
    the handler reads the pipeline record and raises StepFailure when it is
    absent (p33 requirement 2.8), which would fail the run BEFORE the state-row
    write this file is about. The record exists and carries no target schema —
    requirement 2.6's legitimate state.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": PIPELINE_ID}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def _invoke(state_table, *, agent_raises: Exception | None = None):
    """One real ``lambda_handler`` invocation. Returns ``(outcome, put_item_calls)``.

    ``outcome`` is the response dict when the handler returned, or the exception
    it raised. ``put_item_calls`` records every ``put_item`` on the state table
    during the invocation, which is how the mechanism guard below sees a full-row
    write.
    """
    original_put_item = state_table.put_item
    put_item_calls: list[dict] = []

    def _recording_put_item(**kwargs):
        put_item_calls.append(kwargs)
        return original_put_item(**kwargs)

    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(side_effect=agent_raises) if agent_raises else MagicMock(return_value=_REVIEW_PASS)
    agent.add_hook = MagicMock()
    runner = MagicMock()
    runner.run_test.return_value = _EXEC_OK
    # p44 task 14.1: this double stands in for ``CodeInterpreterTestRunner``,
    # which has NO ``last_frame_source`` — pinned by
    # ``test_frame_provenance.py::test_the_pandas_runner_has_no_frame_provenance``.
    # A bare ``MagicMock`` auto-creates it, so the handler's
    # ``getattr(runner, "last_frame_source", None)`` got a child mock instead of
    # ``None``, and ``.provenance()`` a second one, which then rode into
    # ``output_data.summary["frame_provenance"]`` and hit this file's REAL
    # (moto) state table: boto3's ``copy_dynamodb_params`` deepcopies the
    # params, and deepcopying a ``MagicMock`` recurses until the stack is gone.
    # ``del`` restores the real class's shape, so the pandas arm carries no key.
    del runner.last_frame_source

    with (
        patch.object(state_table, "put_item", _recording_put_item),
        patch.object(_mod, "pipeline_runs_table", state_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        event = {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID}
        try:
            return _mod.lambda_handler(event, None), put_item_calls
        except Exception as exc:
            # The failing invocation's raise IS the subject: it has to be
            # returned rather than propagated so the assertions can inspect the
            # row the error path wrote on its way out.
            return exc, put_item_calls


def _row(state_table) -> dict:
    """The durable end state of the ``test_code`` row — where a clobber shows."""
    return state_table.get_item(Key={"run_id": RUN_ID, "step_name": _step_names.StepName.TEST_CODE})["Item"]


@pytest.fixture
def sequence(state_table):
    """Success, then failure, on the SAME row. Returns ``(row, put_item_calls)``.

    The ordering is the defect: invocation 1 fills the row in, invocation 2 fails
    and records its failure on the same item. Anything invocation 1 wrote that
    invocation 2 does not own has to survive.
    """
    state_table.put_item(Item=_CODE_GEN_ROW)

    first, _ = _invoke(state_table)
    assert isinstance(first, dict), f"invocation 1 must succeed to set up the sequence, got {first!r}"
    assert first["test_status"] == "passed", "invocation 1 must leave a PASSING verdict on the row"
    before = _row(state_table)

    second, put_item_calls = _invoke(state_table, agent_raises=RuntimeError(_BOOM))
    assert isinstance(second, _mod.StepFailure), f"invocation 2 must fail through the error path, got {second!r}"

    return before, _row(state_table), put_item_calls


def test_failure_write_preserves_the_attributes_it_does_not_own(sequence):
    """The defect: a failing invocation deleted the previous one's row content.

    On the unfixed code the error path is a full-row ``put_item`` carrying nine
    attributes, so every name below is gone from the row here and this fails.

    **Validates: Requirements 2.16**
    """
    before, after, _ = sequence

    # Premise: invocation 1 actually wrote them, so their survival is a real
    # assertion rather than a vacuous one.
    missing_before = [name for name in PRESERVED_ATTRIBUTES if name not in before]
    assert not missing_before, f"fixture premise broken — invocation 1 never wrote: {missing_before}"

    deleted = [name for name in PRESERVED_ATTRIBUTES if name not in after]
    assert not deleted, f"the failure-path write deleted attribute(s) it does not own: {deleted}"

    for name in PRESERVED_ATTRIBUTES:
        assert after[name] == before[name], f"the failure-path write overwrote {name!r}"


def test_failure_write_records_the_failure(sequence):
    """The row still says what happened — the preservation is not a no-op write.

    P28 R1.1 has the handler record its own failure on purpose, so the failure
    attributes must be present and current, not merely non-destructive.

    **Validates: Requirements 2.16**
    """
    _, after, _ = sequence

    assert after["status"] == "failed"
    assert after["error_type"] == "ProcessingError"
    assert _BOOM in after["error_message"]
    assert int(after["step_sequence"]) == _mod.STEP_SEQUENCE[_step_names.StepName.TEST_CODE]
    assert after["pipeline_id"] == PIPELINE_ID
    assert after["original_pipeline_id"] == PIPELINE_ID


def test_failure_write_withdraws_the_stale_test_verdict(sequence):
    """A ``status: failed`` row must not carry the earlier attempt's PASS.

    Left alone, ``test_status``/``test_result``/``validation_status`` would still
    read ``passed``/``validated`` from invocation 1 — and
    ``pipelines_runs.py:641`` reads top-level ``test_status`` FIRST, so that is
    what the run-detail step card would show for a run that failed. They are
    withdrawn to this repo's existing no-verdict values rather than restated as
    a failure the error path never measured: ``"unknown"``
    (``approval_handler:287,313``) and ``None`` (the shape every ``test_result``
    reader already guards for).

    **Validates: Requirements 2.16**
    """
    before, after, _ = sequence

    # Premise: invocation 1 left a passing verdict, so withdrawal is observable.
    assert before["test_status"] == "passed"
    assert before["test_result"]["status"] == "passed"
    assert before["validation_status"] == "validated"

    assert after["test_status"] == "unknown"
    assert after["test_result"] is None
    assert after["validation_status"] == "failed"

    # ``output_data`` is deliberately preserved (it is the record of invocation
    # 1, and `create_output_data` fixes it to five validated keys), so the
    # top-level withdrawal above is what has to shadow its approval payload.
    assert after["output_data"]["approval"]["test_status"] == "passed"


def test_failure_path_never_full_row_puts_the_state_row(sequence):
    """The mechanism guard: no ``put_item`` on the row two writers share.

    ``PutItem`` is the defect, not a symptom — it replaces the whole item, so any
    full-row write here deletes whatever another writer put there. The only
    ``put_item`` in this fixture is the test's own ``create_code`` seed, which is
    a different ``step_name``.

    **Validates: Requirements 2.16**
    """
    _, _, put_item_calls = sequence

    test_code_puts = [
        call for call in put_item_calls if call.get("Item", {}).get("step_name") == _step_names.StepName.TEST_CODE
    ]
    assert not test_code_puts, (
        "the failure path full-row put_item'd the test_code state row; that deletes "
        "every attribute an earlier invocation wrote to the same item"
    )


def test_error_path_creates_the_row_when_it_is_the_first_writer(state_table):
    """``UpdateItem`` upserts — verified, not assumed.

    The failure path is often the FIRST writer for the row (a failure before the
    success write ever runs), and converting it to a SET update must not turn a
    recorded failure into no record at all. No ``attribute_exists`` condition is
    attached for that reason: ``LGR-60c4985-4`` uses exactly that condition to
    stop ``cache_step_result`` leaving a phantom row, whereas P28 R1.1/R8.5
    oblige this write to happen unconditionally.

    **Validates: Requirements 2.16**
    """
    state_table.put_item(Item=_CODE_GEN_ROW)
    assert "Item" not in state_table.get_item(Key={"run_id": RUN_ID, "step_name": _step_names.StepName.TEST_CODE}), (
        "premise: no test_code row exists yet"
    )

    outcome, _ = _invoke(state_table, agent_raises=RuntimeError(_BOOM))
    assert isinstance(outcome, _mod.StepFailure)

    row = _row(state_table)
    assert row["status"] == "failed"
    assert _BOOM in row["error_message"]
    assert row["error_type"] == "ProcessingError"
    assert row["step_name"] == _step_names.StepName.TEST_CODE
