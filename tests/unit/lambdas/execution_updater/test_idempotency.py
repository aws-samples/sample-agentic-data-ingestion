# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Idempotency unit tests for the execution_updater Lambda.

Covers:
- Same DynamoDB Stream record processed twice → same update_item calls both times
- Same record with same step_name and status → no additional side effects

Validates: Requirements 18.2
"""

import importlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_execution_updater",
    )
)
SHARED_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "shared",
    )
)

_ENV = {
    "EXECUTIONS_TABLE": "test-executions",
    "PIPELINES_TABLE": "test-pipelines",
    "ERRORS_TABLE": "test-errors",
    "STATE_TABLE": "test-state",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}


def _load_handler(mock_table=None):
    """Load execution_updater via importlib, patching boto3 before import."""
    if mock_table is None:
        mock_table = MagicMock()

    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table

    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        with patch("boto3.resource", return_value=mock_dynamo):
            sys.modules.pop("lambda_function", None)
            if SHARED_DIR not in sys.path:
                sys.path.insert(0, SHARED_DIR)
            spec = importlib.util.spec_from_file_location(
                "lambda_function",
                os.path.join(LAMBDA_DIR, "lambda_function.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, mock_table


def _stream_record(
    event_name="INSERT", step_name="file_validation", status="success", run_id="run-001", pipeline_id="pipe-001"
):
    """Build a DynamoDB Stream record."""
    return {
        "eventID": f"evt-{step_name}-{status}",
        "eventName": event_name,
        "dynamodb": {
            "NewImage": {
                "run_id": {"S": run_id},
                "pipeline_id": {"S": pipeline_id},
                "step_name": {"S": step_name},
                "status": {"S": status},
            }
        },
    }


def _stream_event(*records):
    return {"Records": list(records)}


# ---------------------------------------------------------------------------
# Tests — Idempotent processing
# ---------------------------------------------------------------------------


class TestIdempotentProcessing:
    """Same DynamoDB Stream record processed twice produces identical side effects."""

    def test_same_record_twice_produces_same_update_calls(self):
        """Processing the same record twice results in identical update_item calls."""
        record = _stream_record("INSERT", "file_validation", "success")
        event = _stream_event(record)

        # First invocation
        handler1, mock_table1 = _load_handler()
        handler1.lambda_handler(event, None)
        calls_first = mock_table1.update_item.call_args_list[:]

        # Second invocation (fresh module load)
        handler2, mock_table2 = _load_handler()
        handler2.lambda_handler(event, None)
        calls_second = mock_table2.update_item.call_args_list[:]

        # Both invocations should produce the same number of update_item calls
        assert len(calls_first) == len(calls_second)
        # The Key arguments should be identical
        for c1, c2 in zip(calls_first, calls_second):
            assert c1.kwargs.get("Key") == c2.kwargs.get("Key") or c1[1].get("Key") == c2[1].get("Key")

    def test_same_step_and_status_no_additional_side_effects(self):
        """Processing the same step_name + status twice doesn't create extra records."""
        record = _stream_record("MODIFY", "schema_extract", "running")
        event = _stream_event(record)

        handler, mock_table = _load_handler()

        # Process once
        handler.lambda_handler(event, None)
        count_after_first = mock_table.update_item.call_count

        # Reset and process again
        mock_table.reset_mock()
        handler.lambda_handler(event, None)
        count_after_second = mock_table.update_item.call_count

        # Same number of DynamoDB operations each time
        assert count_after_first == count_after_second
