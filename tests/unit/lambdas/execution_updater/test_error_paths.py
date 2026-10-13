# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Error path unit tests for the execution_updater Lambda.

Covers:
- Malformed DynamoDB Stream record (missing NewImage, missing keys)
- ConditionalCheckFailedException on update_item
- REMOVE event is skipped

Validates: Requirements 17.1, 17.2, 17.3
"""

import importlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

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
            # Ensure shared dir is on path for env_utils import
            if SHARED_DIR not in sys.path:
                sys.path.insert(0, SHARED_DIR)
            spec = importlib.util.spec_from_file_location(
                "lambda_function",
                os.path.join(LAMBDA_DIR, "lambda_function.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, mock_table


def _stream_record(event_name, step_name="file_validation", status="success", run_id="run-001", pipeline_id="pipe-001"):
    """Build a DynamoDB Stream record."""
    new_image = {
        "run_id": {"S": run_id},
        "pipeline_id": {"S": pipeline_id},
        "step_name": {"S": step_name},
        "status": {"S": status},
    }
    rec = {
        "eventID": f"evt-{step_name}",
        "eventName": event_name,
        "dynamodb": {"NewImage": new_image},
    }
    return rec


# ---------------------------------------------------------------------------
# Tests — Malformed DynamoDB Stream records
# ---------------------------------------------------------------------------


class TestMalformedStreamRecord:
    """Error paths for malformed or incomplete DynamoDB Stream records."""

    def test_missing_new_image_skips_record(self):
        """Record with no NewImage (e.g. REMOVE event) is skipped."""
        handler, mock_table = _load_handler()
        record = {
            "eventID": "evt-1",
            "eventName": "INSERT",
            "dynamodb": {},  # No NewImage
        }
        # Should not raise — missing run_id/pipeline_id causes skip
        result = handler.lambda_handler({"Records": [record]}, None)
        assert result["statusCode"] == 200
        mock_table.update_item.assert_not_called()

    def test_missing_run_id_skips_record(self):
        """Record with NewImage but no run_id is skipped."""
        handler, mock_table = _load_handler()
        record = {
            "eventID": "evt-2",
            "eventName": "INSERT",
            "dynamodb": {
                "NewImage": {
                    "pipeline_id": {"S": "pipe-001"},
                    "step_name": {"S": "file_validation"},
                    "status": {"S": "success"},
                }
            },
        }
        result = handler.lambda_handler({"Records": [record]}, None)
        assert result["statusCode"] == 200
        mock_table.update_item.assert_not_called()

    def test_missing_pipeline_id_skips_record(self):
        """Record with NewImage but no pipeline_id is skipped."""
        handler, mock_table = _load_handler()
        record = {
            "eventID": "evt-3",
            "eventName": "INSERT",
            "dynamodb": {
                "NewImage": {
                    "run_id": {"S": "run-001"},
                    "step_name": {"S": "file_validation"},
                    "status": {"S": "success"},
                }
            },
        }
        result = handler.lambda_handler({"Records": [record]}, None)
        assert result["statusCode"] == 200
        mock_table.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — ConditionalCheckFailedException
# ---------------------------------------------------------------------------


class TestConditionalCheckFailed:
    """The upsert's only condition protects a CANCELLED run; every other DynamoDB error propagates.

    Until 2026-10-11 the upsert carried no condition, so a
    ConditionalCheckFailedException could not occur and this class asserted it
    propagated like any other error. The upsert now refuses to overwrite a
    CANCELLED row, and a refusal is that guard working: the step write is
    skipped, not retried.
    """

    def test_a_throttled_write_propagates_for_retry(self):
        mock_table = MagicMock()
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Slow down"}}
        mock_table.update_item.side_effect = ClientError(error_response, "UpdateItem")

        handler, _ = _load_handler(mock_table)
        record = _stream_record("INSERT", "file_validation", "success")
        with pytest.raises(ClientError):
            handler.lambda_handler({"Records": [record]}, None)

    def test_a_refused_write_on_a_cancelled_run_is_skipped_not_retried(self):
        mock_table = MagicMock()
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "Condition not met"}}
        mock_table.update_item.side_effect = ClientError(error_response, "UpdateItem")

        handler, _ = _load_handler(mock_table)
        record = _stream_record("MODIFY", "create_code", "failed")
        result = handler.lambda_handler({"Records": [record]}, None)

        assert result["statusCode"] == 200
        # Only the refused upsert: no steps_status write, no lineage, no error capture.
        assert mock_table.update_item.call_count == 1
        mock_table.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — REMOVE event is skipped
# ---------------------------------------------------------------------------


class TestRemoveEventSkipped:
    """REMOVE events from DynamoDB Stream are skipped (only INSERT/MODIFY processed)."""

    def test_remove_event_skipped(self):
        handler, mock_table = _load_handler()
        record = _stream_record("REMOVE", "file_validation", "success")
        result = handler.lambda_handler({"Records": [record]}, None)
        assert result["statusCode"] == 200
        mock_table.update_item.assert_not_called()

    def test_insert_event_processed(self):
        """Sanity check: INSERT events ARE processed."""
        handler, mock_table = _load_handler()
        record = _stream_record("INSERT", "file_validation", "success")
        result = handler.lambda_handler({"Records": [record]}, None)
        assert result["statusCode"] == 200
        assert mock_table.update_item.call_count >= 1
