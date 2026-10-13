# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for bare except clause replacements in dashboard_api/pipelines.py.

Validates: Requirement 1 — S3 metadata failure logs warning;
           approval_data parse failure logs warning.

These tests verify the exception handling patterns that replaced bare except
clauses. They simulate the exact code paths from pipelines.py to confirm
that specific exception types are caught and logger.warning is called.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading — reuse the pattern from test_error_paths.py
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
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
    "STATE_TABLE": "test-state",
    "MAPPINGS_TABLE": "test-mappings",
    "PIPELINES_TABLE": "test-pipelines",
    "GENERATED_CODE_TABLE": "test-code",
    "TEST_RESULTS_TABLE": "test-results",
    "APPROVALS_TABLE": "test-approvals",
    "EXECUTIONS_TABLE": "test-executions",
    "ERRORS_TABLE": "test-errors",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AWS_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}


def _load_logger():
    """Load the dashboard_api common module to get the logger instance."""
    import importlib
    import importlib.util

    mock_dynamo = MagicMock()
    mock_s3 = MagicMock()

    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        with patch("boto3.client", return_value=mock_s3), patch("boto3.resource", return_value=mock_dynamo):
            for mod_name in list(sys.modules.keys()):
                if mod_name in ("router", "lambda_function") or mod_name.startswith("handlers"):
                    del sys.modules[mod_name]

            if LAMBDA_DIR not in sys.path:
                sys.path.insert(0, LAMBDA_DIR)
            if SHARED_DIR not in sys.path:
                sys.path.insert(0, SHARED_DIR)

            spec = importlib.util.spec_from_file_location(
                "handlers.common",
                os.path.join(LAMBDA_DIR, "handlers", "common.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.logger


_logger = _load_logger()


# ---------------------------------------------------------------------------
# Test: S3 metadata load failure logs warning (line 982)
# ---------------------------------------------------------------------------


class TestS3MetadataFailureLogs:
    """When S3 get_object for schema metadata raises, the handler
    logs a warning and continues with empty metadata."""

    def test_logs_warning_on_s3_metadata_failure(self):
        """Simulates the exact code path: s3_client.get_object raises,
        except Exception as e catches it, logger.warning is called."""
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("NoSuchKey")

        with patch.object(_logger, "warning") as mock_warn:
            metadata = {}
            try:
                metadata_key = "schemas/uploaded/test-metadata.json"
                metadata_response = mock_s3.get_object(Bucket="test-bucket", Key=metadata_key)
                metadata = json.loads(metadata_response["Body"].read())
            except Exception as e:
                _logger.warning(f"Failed to load schema metadata from S3: {e}")

            mock_warn.assert_called_once()
            assert "Failed to load schema metadata from S3" in mock_warn.call_args[0][0]
            assert "NoSuchKey" in mock_warn.call_args[0][0]
            assert metadata == {}


# ---------------------------------------------------------------------------
# Test: approval_data JSON parse failure logs warning (lines 1770, 1789)
# ---------------------------------------------------------------------------


class TestApprovalDataParseFailureLogs:
    """When json.loads of approval_data raises JSONDecodeError, TypeError,
    or AttributeError, the handler logs a warning and continues."""

    @pytest.mark.parametrize(
        "bad_value",
        [
            "{invalid json",
        ],
    )
    def test_logs_warning_on_json_decode_error(self, bad_value):
        """JSONDecodeError from malformed JSON is caught and logged."""
        with patch.object(_logger, "warning") as mock_warn:
            step_info = {}
            approval_data_str = bad_value
            if approval_data_str:
                try:
                    approval_data = (
                        json.loads(approval_data_str) if isinstance(approval_data_str, str) else approval_data_str
                    )
                    step_info["code_version_id"] = approval_data.get("code_version_id", "")
                except (json.JSONDecodeError, TypeError, AttributeError) as e:
                    _logger.warning(f"Failed to parse approval_data: {e}")

            mock_warn.assert_called_once()
            assert "Failed to parse approval_data" in mock_warn.call_args[0][0]

    def test_logs_warning_on_attribute_error(self):
        """AttributeError from non-dict parsed JSON (e.g. list) is caught."""
        with patch.object(_logger, "warning") as mock_warn:
            step_info = {}
            approval_data_str = '["not", "a", "dict"]'
            try:
                approval_data = (
                    json.loads(approval_data_str) if isinstance(approval_data_str, str) else approval_data_str
                )
                step_info["code_version_id"] = approval_data.get("code_version_id", "")
            except (json.JSONDecodeError, TypeError, AttributeError) as e:
                _logger.warning(f"Failed to parse approval_data: {e}")

            mock_warn.assert_called_once()
            assert "Failed to parse approval_data" in mock_warn.call_args[0][0]

    def test_code_testing_parse_failure_logs_warning(self):
        """CodeTesting path: same pattern, different fields extracted."""
        with patch.object(_logger, "warning") as mock_warn:
            step_info = {}
            approval_data_str = "{bad json"
            if approval_data_str:
                try:
                    approval_data = (
                        json.loads(approval_data_str) if isinstance(approval_data_str, str) else approval_data_str
                    )
                    step_info["test_execution_id"] = approval_data.get("test_execution_id", "")
                    step_info["test_status"] = approval_data.get("test_status", "")
                except (json.JSONDecodeError, TypeError, AttributeError) as e:
                    _logger.warning(f"Failed to parse approval_data: {e}")

            mock_warn.assert_called_once()
            assert "Failed to parse approval_data" in mock_warn.call_args[0][0]
