# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: RecordFailure Lambda Cause parsing + Failure_Record write (P28 R1.3/R1.4).

The RecordFailure Lambda is the Catch target for every task state. It must:
- parse the StepFailure JSON out of the Lambda-runtime-wrapped Cause,
- fall back gracefully for non-StepFailure errors (States.Timeout,
  Lambda.ServiceException, GlueJobFailed, garbled causes),
- extract run/pipeline identifiers defensively from wherever they exist,
- write the pipeline_failure state item with the table's run_id + step_name
  key schema, and
- never let its own write errors mask the original failure.

Validates: P28 Requirements 1.3, 1.4
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir to path
_record_failure_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_record_failure")
)
sys.path.insert(0, _record_failure_dir)

sys.modules.pop("lambda_function", None)
import lambda_function as _mod
import pytest


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _step_failure_cause(step_name: str, error_type: str, message: str) -> str:
    """Build the SFN Cause exactly as the Lambda runtime wraps a StepFailure."""
    step_failure_json = json.dumps(
        {"step_name": step_name, "error_type": error_type, "message": message, "details": {}}
    )
    return json.dumps(
        {
            "errorMessage": step_failure_json,
            "errorType": "StepFailure" if error_type == "permanent" else "TransientStepFailure",
            "requestId": "req-1",
            "stackTrace": ["  ..."],
        }
    )


def _state_with_error(error: dict, **extra) -> dict:
    state = {
        "pipeline_id": "uploads/customer-a",
        "source_type": "S3_UPLOAD",
        "file_validation_result": {"run_id": "run-123", "original_pipeline_id": "pipe-9"},
        "error": error,
    }
    state.update(extra)
    return state


def _event(state: dict) -> dict:
    return {"state": state, "execution_name": "exec-abc"}


# ── parse_error: StepFailure contract ────────────────────────────────


def test_step_failure_cause_yields_authoritative_attribution():
    """The StepFailure JSON in Cause.errorMessage drives step/type/message."""
    cause = _step_failure_cause("schema_mapping", "permanent", "Bedrock returned unparseable JSON")
    parsed = _mod.parse_error({"Error": "StepFailure", "Cause": cause}, {})

    assert parsed["failed_step"] == "schema_mapping"
    assert parsed["failed_step_source"] == "step_failure"
    assert parsed["error_type"] == "permanent"
    assert parsed["error_message"] == "Bedrock returned unparseable JSON"
    assert parsed["error_name"] == "StepFailure"


def test_transient_step_failure_preserves_transient_error_type():
    cause = _step_failure_cause("run_pipeline", "transient", "Glue throttled")
    parsed = _mod.parse_error({"Error": "TransientStepFailure", "Cause": cause}, {})

    assert parsed["failed_step"] == "run_pipeline"
    assert parsed["error_type"] == "transient"


# ── parse_error: fallbacks ───────────────────────────────────────────


def test_plain_lambda_exception_falls_back_to_inference():
    """A non-StepFailure exception (plain errorMessage) keeps the message and infers the step."""
    cause = json.dumps({"errorMessage": "division by zero", "errorType": "ZeroDivisionError"})
    state = {"file_validation_result": {}, "schema_extract_result": {}}
    parsed = _mod.parse_error({"Error": "ZeroDivisionError", "Cause": cause}, state)

    assert parsed["failed_step"] == "schema_compare"  # next after last completed
    assert parsed["failed_step_source"] == "inferred"
    assert parsed["error_type"] == "unknown"
    assert parsed["error_message"] == "division by zero"
    assert parsed["error_name"] == "ZeroDivisionError"


def test_non_json_cause_uses_error_name_and_raw_cause():
    """States.Timeout style errors carry no JSON — message falls back sanely."""
    parsed = _mod.parse_error({"Error": "States.Timeout", "Cause": ""}, {})

    assert parsed["error_name"] == "States.Timeout"
    assert parsed["error_message"] == "States.Timeout"  # empty cause → error name
    assert parsed["error_type"] == "unknown"
    assert parsed["failed_step"] == "file_validation"  # nothing completed yet


def test_glue_job_failed_maps_to_run_pipeline_step():
    """job_completion sends error=GlueJobFailed — unambiguous step attribution."""
    parsed = _mod.parse_error(
        {"Error": "GlueJobFailed", "Cause": "Glue job agentic-data-ingestion-job-x FAILED: OOM"},
        {},
    )

    assert parsed["failed_step"] == "run_pipeline"
    assert parsed["failed_step_source"] == "error_name"
    assert "OOM" in parsed["error_message"]


def test_missing_error_fields_do_not_crash():
    parsed = _mod.parse_error({}, {})
    assert parsed["error_name"] == "Unknown"
    assert parsed["failed_step"] == "file_validation"


# ── progress inference ───────────────────────────────────────────────


def test_infer_failed_step_walks_progress_order():
    state = {
        "file_validation_result": {},
        "schema_extract_result": {},
        "schema_compare_result": {},
        "schema_mapping_result": {},
        "create_code_result": {},
    }
    # last completed = create_code → next = test_code
    assert _mod.infer_failed_step("States.Timeout", state) == "test_code"


def test_infer_failed_step_all_keys_present_returns_unknown():
    state = {key: {} for key, _ in _mod.PROGRESS_ORDER}
    assert _mod.infer_failed_step("SomeError", state) == "unknown"


# ── identifier extraction ────────────────────────────────────────────


def test_identifiers_prefer_file_validation_result():
    state = {
        "pipeline_id": "uploads/prefix",  # lock key, NOT the real pipeline id
        "run_id": "top-level-run",
        "file_validation_result": {"run_id": "run-42", "original_pipeline_id": "pipe-7"},
    }
    run_id, pipeline_id = _mod.extract_identifiers(state, "exec-1")
    assert (run_id, pipeline_id) == ("run-42", "pipe-7")


def test_identifiers_fall_back_to_top_level_then_execution_name():
    # Retry-run shape: run_id/pipeline_id at top level, no file_validation_result
    run_id, pipeline_id = _mod.extract_identifiers({"run_id": "r-1", "pipeline_id": "p-1"}, "exec-1")
    assert (run_id, pipeline_id) == ("r-1", "p-1")

    # FileValidation failed before producing anything: execution name keys the record
    run_id, pipeline_id = _mod.extract_identifiers({"pipeline_id": "uploads/x"}, "exec-99")
    assert run_id == "exec-99"
    assert pipeline_id == "uploads/x"

    # Nothing at all — never an empty (unkeyable) run_id
    run_id, pipeline_id = _mod.extract_identifiers({}, "")
    assert (run_id, pipeline_id) == ("unknown", "unknown")


def test_identifiers_tolerate_non_dict_file_validation_result():
    run_id, _ = _mod.extract_identifiers({"file_validation_result": "garbage", "run_id": "r-2"}, "")
    assert run_id == "r-2"


# ── lambda_handler: Failure_Record write ─────────────────────────────


def test_handler_writes_failure_record_with_table_key_schema():
    """The record is keyed run_id (PK) + step_name='pipeline_failure' (SK) and
    carries step name, error type, human-readable message, and run/pipeline ids."""
    mock_table = MagicMock()
    cause = _step_failure_cause("create_code", "permanent", "mapping missing for field x")
    event = _event(_state_with_error({"Error": "StepFailure", "Cause": cause}))

    with patch.object(_mod, "state_table", mock_table):
        result = _mod.lambda_handler(event, None)

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["run_id"] == "run-123"
    assert item["step_name"] == "pipeline_failure"
    assert item["failed_step"] == "create_code"
    assert item["error_type"] == "permanent"
    assert item["error_message"] == "mapping missing for field x"
    assert item["original_pipeline_id"] == "pipe-9"
    assert item["status"] == "error"
    # Legacy compatibility attributes (dashboard + e2e assertions)
    assert item["pipeline_id"] == "run-123.99"
    assert item["step_sequence"] == 99
    assert result["recorded"] is True
    assert result["status"] == "FAILED"


def test_handler_supports_legacy_flat_event_shape():
    """Direct invocations without the {state, execution_name} wrapper still work."""
    mock_table = MagicMock()
    flat_event = _state_with_error({"Error": "States.Timeout", "Cause": ""})

    with patch.object(_mod, "state_table", mock_table):
        result = _mod.lambda_handler(flat_event, None)

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["run_id"] == "run-123"
    assert result["recorded"] is True


def test_handler_write_error_does_not_mask_original_failure():
    """A DynamoDB write failure is logged and swallowed — the handler must
    return (not raise) so the state machine proceeds to PipelineFailed with
    the ORIGINAL error intact."""
    mock_table = MagicMock()
    mock_table.put_item.side_effect = RuntimeError("dynamo down")
    event = _event(_state_with_error({"Error": "StepFailure", "Cause": _step_failure_cause("t", "permanent", "m")}))

    with patch.object(_mod, "state_table", mock_table):
        result = _mod.lambda_handler(event, None)

    assert result["recorded"] is False
    assert result["status"] == "FAILED"


def test_handler_bounds_message_and_cause_sizes():
    mock_table = MagicMock()
    huge = "x" * 10_000
    cause = _step_failure_cause("test_code", "permanent", huge)
    event = _event(_state_with_error({"Error": "StepFailure", "Cause": cause}))

    with patch.object(_mod, "state_table", mock_table):
        _mod.lambda_handler(event, None)

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert len(item["error_message"]) <= _mod.MAX_MESSAGE_CHARS
    assert len(item["error_cause"]) <= _mod.MAX_CAUSE_CHARS
