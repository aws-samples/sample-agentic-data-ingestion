# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for get_run_artifacts handler.

Tests that the handler correctly joins state table steps into a single
response dict with artifacts keyed by step_name, fetches code content
from S3 when available, and handles edge cases gracefully.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup — allow imports from the dashboard API Lambda and shared modules
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# ---------------------------------------------------------------------------
# Environment variables required by handlers/common.py at import time
# ---------------------------------------------------------------------------
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("AWS_REGION", "us-west-2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_response(response: dict) -> tuple:
    """Return (status_code, parsed_body) from a create_response dict."""
    return response["statusCode"], json.loads(response["body"])


def _make_step_item(step_name: str, run_id: str, output_data: dict = None) -> dict:
    """Build a DynamoDB item that looks like a state-table step row."""
    item = {"step_name": step_name, "run_id": run_id}
    if output_data is not None:
        item["output_data"] = output_data
    return item


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PIPELINE_ID = "pipe-001"
RUN_ID = "run-abc-123"


@pytest.fixture(autouse=True)
def _patch_boto(monkeypatch):
    """
    Patch the module-level boto resources used by handlers/common.py so that
    no real AWS calls are made.  Yields the mock state_table and s3_client
    for per-test customisation.
    """
    from handlers import common as _common

    mock_state_table = MagicMock()
    mock_s3 = MagicMock()

    monkeypatch.setattr(_common, "state_table", mock_state_table)
    monkeypatch.setattr(_common, "s3_client", mock_s3)
    # P21 Task 7.2: get_run_artifacts now lives in handlers.pipelines_runs and
    # reads S3 via handlers.pipelines_artifacts._read_s3_object. The thin
    # handlers.pipelines façade no longer owns these names, so patch the
    # owning modules directly (module references only — behaviour-preserving).
    from handlers import pipelines_artifacts as _artifacts
    from handlers import pipelines_runs as _runs

    monkeypatch.setattr(_runs, "state_table", mock_state_table)
    monkeypatch.setattr(_artifacts, "s3_client", mock_s3)

    # Store on the module so individual tests can configure return values
    _patch_boto.state_table = mock_state_table
    _patch_boto.s3_client = mock_s3
    yield


def _set_query_items(items: list):
    """Configure the mock state_table.query to return *items*."""
    _patch_boto.state_table.query.return_value = {"Items": items}


def _set_s3_body(content: str):
    """Configure the mock s3_client.get_object to return *content*."""
    body = MagicMock()
    body.read.return_value = content.encode("utf-8")
    _patch_boto.s3_client.get_object.return_value = {"Body": body}


def _client_error(code: str, operation: str = "GetObject") -> "ClientError":
    """Build a botocore ClientError with the given error code."""
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, operation)


def _set_s3_failure(exc: Exception = None):
    """Configure the mock s3_client.get_object to raise an exception."""
    _patch_boto.s3_client.get_object.side_effect = exc or Exception("S3 read error")


# ---------------------------------------------------------------------------
# Import the handler under test (after env + path setup)
# ---------------------------------------------------------------------------
from handlers.pipelines import get_run_artifacts

# ===========================================================================
# Test cases
# ===========================================================================


class TestHappyPath:
    """Multiple steps with output_data → all artifacts joined into response."""

    def test_multiple_steps_joined(self):
        _set_query_items(
            [
                _make_step_item(
                    "file_validation",
                    RUN_ID,
                    {
                        "artifact_type": "file_validation",
                        "primary_id": "fv-1",
                        "status": "success",
                        "summary": {"file_name": "data.json"},
                        "s3_uris": {"source_file": "s3://bucket/data.json"},
                    },
                ),
                _make_step_item(
                    "schema_extraction",
                    RUN_ID,
                    {
                        "artifact_type": "schema_extraction",
                        "primary_id": "se-1",
                        "status": "success",
                        "summary": {"field_count": 5},
                        "s3_uris": {},
                    },
                ),
                _make_step_item(
                    "schema_mapping",
                    RUN_ID,
                    {
                        "artifact_type": "schema_mapping",
                        "primary_id": "sm-1",
                        "status": "success",
                        "summary": {"mapped_fields": 5},
                        "s3_uris": {},
                    },
                ),
            ]
        )

        status, body = _parse_response(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 200
        assert body["pipeline_id"] == PIPELINE_ID
        assert body["run_id"] == RUN_ID
        assert len(body["artifacts"]) == 3
        assert "file_validation" in body["artifacts"]
        assert "schema_extraction" in body["artifacts"]
        assert "schema_mapping" in body["artifacts"]
        assert body["code_content"] is None  # no code_generation step


class TestStepsWithoutOutputData:
    """Steps that have no output_data are excluded from the artifacts dict."""

    def test_steps_without_output_data_excluded(self):
        _set_query_items(
            [
                _make_step_item(
                    "file_validation",
                    RUN_ID,
                    {
                        "artifact_type": "file_validation",
                        "primary_id": "fv-1",
                        "status": "success",
                        "summary": {},
                        "s3_uris": {},
                    },
                ),
                # This step has no output_data key at all
                _make_step_item("schema_extraction", RUN_ID),
                # This step has an empty dict for output_data
                _make_step_item("schema_mapping", RUN_ID, {}),
            ]
        )

        status, body = _parse_response(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 200
        # Only file_validation has non-empty output_data
        assert len(body["artifacts"]) == 1
        assert "file_validation" in body["artifacts"]
        assert "schema_extraction" not in body["artifacts"]
        assert "schema_mapping" not in body["artifacts"]


class TestCodeContentFromS3:
    """Code content is fetched from S3 when code_generation step has code_file URI."""

    def test_code_content_fetched(self):
        code_body = "# generated PySpark\ndf_transformed = df.select('id')"
        _set_query_items(
            [
                _make_step_item(
                    "code_generation",
                    RUN_ID,
                    {
                        "artifact_type": "code_generation",
                        "primary_id": "cv-1",
                        "status": "success",
                        "summary": {"line_count": 2},
                        "s3_uris": {"code_file": "s3://code-bucket/scripts/job.py"},
                    },
                ),
            ]
        )
        _set_s3_body(code_body)

        status, body = _parse_response(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 200
        assert body["code_content"] == code_body
        _patch_boto.s3_client.get_object.assert_called_once_with(
            Bucket="code-bucket",
            Key="scripts/job.py",
        )


class TestCodeContentNullWhenNoCodeStep:
    """code_content is null when no code_generation step exists."""

    def test_no_code_generation_step(self):
        _set_query_items(
            [
                _make_step_item(
                    "file_validation",
                    RUN_ID,
                    {
                        "artifact_type": "file_validation",
                        "primary_id": "fv-1",
                        "status": "success",
                        "summary": {},
                        "s3_uris": {},
                    },
                ),
            ]
        )

        status, body = _parse_response(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 200
        assert body["code_content"] is None
        _patch_boto.s3_client.get_object.assert_not_called()


class TestEmptyResults:
    """No steps found → 404 response."""

    def test_no_steps_returns_404(self):
        _set_query_items([])

        status, body = _parse_response(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 404
        assert "error" in body
        assert RUN_ID in body["error"]


class TestS3ReadFailure:
    """P28 R17.3: genuine not-found → null code_content; infra error → 5xx.

    An S3 throttle must never masquerade as absent content — only a
    genuinely-missing object degrades to ``code_content: null``.
    """

    _CODE_STEP = [
        {
            "step_name": "code_generation",
            "run_id": RUN_ID,
            "output_data": {
                "artifact_type": "code_generation",
                "primary_id": "cv-1",
                "status": "success",
                "summary": {"line_count": 10},
                "s3_uris": {"code_file": "s3://code-bucket/scripts/job.py"},
            },
        }
    ]

    def test_s3_not_found_returns_null_code_content(self):
        _set_query_items(self._CODE_STEP)
        _set_s3_failure(_client_error("NoSuchKey"))

        status, body = _parse_response(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 200
        assert body["code_content"] is None
        assert "code_generation" in body["artifacts"]

    def test_s3_throttle_returns_5xx_not_empty_200(self):
        _set_query_items(self._CODE_STEP)
        _set_s3_failure(_client_error("ThrottlingException"))

        status, body = _parse_response(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 500
        assert "error" in body

    def test_s3_access_denied_returns_5xx_not_empty_200(self):
        _set_query_items(self._CODE_STEP)
        _set_s3_failure(_client_error("AccessDenied"))

        status, body = _parse_response(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 500
        assert "error" in body
