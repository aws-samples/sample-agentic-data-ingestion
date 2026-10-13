# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression: the handler's state-row write must not clobber the feedback write.

Two writers hit the SAME DynamoDB item — ``{run_id, step_name=test_code}`` on
the pipeline-runs table — inside ONE ``lambda_handler`` invocation:

1. ``store_regeneration_feedback`` SETs ``regeneration_feedback`` /
   ``feedback_timestamp``.
2. the handler's own state-row write records the step's result.

Write 2 used to be a full-row ``put_item``, and ``PutItem`` replaces the entire
item — so it deleted write 1 roughly 130ms later, in the same invocation.
Observed live on execution
``arn:aws:states:us-west-2:777788889999:execution:agentic-data-ingestion-pipeline:file-p32obs_renB_csv-20260805T132039-3393c913f593``
(run ``run-2026-08-05-13-20-43-433f80b2``):

    13:21:19.150  store_regeneration_feedback:550  "Regeneration feedback stored"
    13:21:19.271  lambda_handler:1356             "Pipeline state with regeneration analysis stored"
    13:21:24.085  create_code get_regeneration_feedback:114  "No regeneration feedback found"

and all three generated scripts came out byte-identical (sha256
``e110e570ceafcafd…``) — the byte-identical regeneration prompt 2.16 forbids.

**Why this file drives the whole handler.** The existing coverage of the write
half calls ``store_regeneration_feedback`` in isolation
(``tests/unit/lambdas/test_code/test_store_regeneration_feedback.py``, and
``tests/integration/pipeline_flow/test_regeneration_loop_prop.py:429``). Both
pass on the broken code, because in isolation nothing else writes the item —
the defect only exists in the *sequence* the handler performs. So the assertion
here is made after a real ``lambda_handler`` invocation, against a real
(moto-backed) table from the shared fixtures, reading the item back with
``get_item``. Anything narrower can drift from the handler again.

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
_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
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


# The downstream CONSUMER of the feedback write — the reader whose "No
# regeneration feedback found" log line is the live symptom.
_reader = _load_module("p32_clobber_mapping_resolution", os.path.join(_CREATE_CODE_DIR, "mapping_resolution.py"))
_step_names = _load_module("p32_clobber_step_names", os.path.join(_SHARED_DIR, "step_names.py"))

RUN_ID = "run-2026-08-05-13-20-43-433f80b2"
PIPELINE_ID = "pipe-p32obs-renB"

# A fail verdict: this is what makes the handler decide regeneration is needed,
# which is what makes it call store_regeneration_feedback before writing its
# own row. Both writes therefore happen in the one invocation below.
_REVIEW_FAIL = json.dumps(
    {
        "verdict": "fail",
        "issues": [{"severity": "critical", "category": "Correctness", "description": "to_date format is wrong."}],
        "categories": {"Correctness": "fail", "Security": "pass"},
    }
)

_EXEC_OK = {"execution_successful": True, "executed": True, "input_records": 10, "output_records": 10}

_CODE_GEN_ROW = {
    "run_id": RUN_ID,
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
}

# Every attribute the handler's state-row write owns. Pinned as a list rather
# than sampled, so the put_item → update_item conversion is verified COMPLETE:
# an attribute silently dropped from the SET expression fails here, not in
# production three weeks later.
ROW_WRITE_ATTRIBUTES = (
    "status",
    "step_sequence",
    "timestamp",
    "pipeline_id",
    "original_pipeline_id",
    "regeneration_attempt",
    "regeneration_max_attempts",
    "approval_required",
    "regeneration_analysis",
    "output_data",
    "test_execution_id",
    "test_status",
    "test_result",
    "validation_status",
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

    p33 task 4.3 plumbing, not an assertion change: the handler's target-schema
    read moved from the schema-mappings item to the pipeline record, so the
    pipelines table is now on the handler's path. A bare ``MagicMock()`` answers
    ``False`` to ``"Item" in response``, which now reads as a *missing* pipeline
    record and raises StepFailure (p33 requirement 2.8) before the state-row
    write this file is about ever happens. The record below exists and carries no
    target schema — requirement 2.6's legitimate state.

    The ``schema_extract`` row the handler now also reads is deliberately NOT
    seeded on the real state table: its absence is the ``no source schema``
    path, which logs a warning and leaves the frame empty. Nothing in this file
    depends on the frame.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": PIPELINE_ID}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


@pytest.fixture
def invocation(state_table):
    """One real ``lambda_handler`` invocation against the real state table.

    Returns ``(response, item, put_item_calls)`` where ``item`` is the
    ``test_code`` row read back with ``get_item`` AFTER the handler returned —
    i.e. the durable end state of the row, which is the only place the clobber
    is observable.
    """
    state_table.put_item(Item=_CODE_GEN_ROW)

    original_put_item = state_table.put_item
    put_item_calls: list[dict] = []

    def _recording_put_item(**kwargs):
        put_item_calls.append(kwargs)
        return original_put_item(**kwargs)

    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value=_REVIEW_FAIL)
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
        response = _mod.lambda_handler({"run_id": RUN_ID, "pipeline_id": PIPELINE_ID}, None)

    item = state_table.get_item(Key={"run_id": RUN_ID, "step_name": _step_names.StepName.TEST_CODE})["Item"]
    return response, item, put_item_calls


def test_handler_stored_feedback_survives_its_own_state_row_write(invocation):
    """The live defect: feedback written, then deleted by the row write.

    The handler decides regeneration is needed, stores the feedback, and then
    writes its state row — in that order, in one invocation. After it returns,
    the feedback must still be on the item.

    On the unfixed code the row write is a ``put_item``, so
    ``regeneration_feedback`` is gone here and this fails.

    **Validates: Requirements 2.16**
    """
    response, item, _ = invocation

    assert response["regeneration_needed"] is True, "fixture must exercise the feedback-writing path"
    stored_feedback = response["regeneration_feedback"]
    assert stored_feedback, "the handler must have had feedback to store"

    assert "regeneration_feedback" in item, (
        "the state-row write deleted the regeneration feedback written earlier in the same invocation"
    )
    assert item["regeneration_feedback"] == stored_feedback
    assert "feedback_timestamp" in item


def test_create_codes_reader_finds_the_feedback_after_the_handler_returns(invocation, state_table):
    """The symptom, from the consumer's side.

    ``create_code``'s reader is what logged "No regeneration feedback found" on
    the live run. It is the reason the clobber matters at all: with nothing to
    read, the regeneration prompt is byte-identical to the one that failed.

    **Validates: Requirements 2.16**
    """
    response, _, _ = invocation

    assert _reader.get_regeneration_feedback(RUN_ID, state_table) == response["regeneration_feedback"]


def test_state_row_write_carries_every_attribute_it_owns(invocation):
    """The conversion is complete, not merely non-clobbering.

    A SET expression that quietly dropped an attribute would satisfy the test
    above while losing part of the step's recorded result. Every attribute the
    write owns is asserted present, and the load-bearing ones are checked
    against the response the handler returned.

    **Validates: Requirements 2.16**
    """
    response, item, _ = invocation

    missing = [name for name in ROW_WRITE_ATTRIBUTES if name not in item]
    assert not missing, f"state-row write dropped attribute(s): {missing}"

    # Key attributes stay the key (they cannot be in a SET expression).
    assert item["run_id"] == RUN_ID
    assert item["step_name"] == _step_names.StepName.TEST_CODE

    # Values carried through unchanged by the conversion.
    assert item["status"] == "failed"
    assert item["test_status"] == response["test_status"] == "failed"
    assert item["pipeline_id"] == PIPELINE_ID
    assert item["original_pipeline_id"] == PIPELINE_ID
    assert int(item["regeneration_attempt"]) == 0
    assert int(item["regeneration_max_attempts"]) == response["regeneration_max_attempts"]
    assert item["approval_required"] == response["approval_required"]
    assert item["validation_status"] == "failed"
    assert json.loads(item["regeneration_analysis"])["regeneration_needed"] is True
    assert item["test_result"]["status"] == "failed"


def test_handler_never_full_row_puts_its_own_state_row(invocation):
    """The mechanism guard: no ``put_item`` on the row two writers share.

    ``PutItem`` is the defect, not a symptom of it — it replaces the whole item,
    so any full-row write here deletes whatever another writer put there. The
    only ``put_item`` in this fixture is the test's own ``create_code`` seed.

    **Validates: Requirements 2.16**
    """
    _, _, put_item_calls = invocation

    test_code_puts = [
        call for call in put_item_calls if call.get("Item", {}).get("step_name") == _step_names.StepName.TEST_CODE
    ]
    assert not test_code_puts, (
        "the handler full-row put_item'd the test_code state row; that deletes "
        "store_regeneration_feedback's attributes on the same item"
    )
