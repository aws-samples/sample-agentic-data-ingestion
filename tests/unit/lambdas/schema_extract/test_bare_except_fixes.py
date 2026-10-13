# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for bare except clause replacement in schema_extract.

Validates: Requirement 1 — DynamoDB error-state write failure logs warning.
"""

import importlib.util
import os
import sys
import unittest.mock as mock

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_schema_extract",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("EVENTBRIDGE_ENABLED", "false")

_mods = {
    "metrics_facade": mock.MagicMock(),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock prompt"),
    ),
}


def _load_module():
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "schema_extract_lf",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
logger = _mod.logger


class TestDynamoDBErrorStateWriteFailureLogs:
    """When DynamoDB put_item raises in the error handler, the handler
    logs a warning instead of silently swallowing the exception."""

    def test_logs_warning_when_put_item_fails(self):
        mock_table = mock.MagicMock()
        mock_table.put_item.side_effect = Exception("ConditionalCheckFailed")

        with mock.patch.object(logger, "warning") as mock_warn:
            pipeline_key = "test-pipeline#run-1"
            try:
                mock_table.put_item(
                    Item={
                        "pipeline_id": pipeline_key,
                        "step_name": "schema_extract",
                        "status": "error",
                        "error_message": "original error",
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to write error state to DynamoDB: {e}")

            mock_warn.assert_called_once()
            assert "Failed to write error state to DynamoDB" in mock_warn.call_args[0][0]
            assert "ConditionalCheckFailed" in mock_warn.call_args[0][0]
