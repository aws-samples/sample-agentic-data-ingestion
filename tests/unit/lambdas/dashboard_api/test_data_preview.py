# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for data_preview handler — limit validation, 404 paths,
format detection, latest-run resolution, and response structure.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

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


def _resp(response: dict) -> tuple[int, dict]:
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PIPELINE_ID = "pipe-preview-001"
RUN_ID = "run-preview-abc"


@pytest.fixture(autouse=True)
def _patch_boto(monkeypatch):
    """Patch module-level boto resources so no real AWS calls are made."""
    from handlers import common as _common
    from handlers import data_preview as _dp

    mock_state = MagicMock()
    mock_exec = MagicMock()
    mock_s3 = MagicMock()

    monkeypatch.setattr(_common, "state_table", mock_state)
    monkeypatch.setattr(_common, "executions_table", mock_exec)
    monkeypatch.setattr(_common, "s3_client", mock_s3)
    monkeypatch.setattr(_dp, "state_table", mock_state)
    monkeypatch.setattr(_dp, "executions_table", mock_exec)
    monkeypatch.setattr(_dp, "s3_client", mock_s3)

    _patch_boto.state_table = mock_state
    _patch_boto.executions_table = mock_exec
    _patch_boto.s3_client = mock_s3
    yield


def _set_state_items(items: list):
    _patch_boto.state_table.query.return_value = {"Items": items}


def _set_exec_items(items: list):
    _patch_boto.executions_table.query.return_value = {"Items": items}


def _set_s3_json_body(content: str):
    body = MagicMock()
    body.read.return_value = content.encode("utf-8")
    _patch_boto.s3_client.get_object.return_value = {"Body": body}


# ---------------------------------------------------------------------------
# Import handler under test
# ---------------------------------------------------------------------------
from handlers.data_preview import (
    _parse_limit,
    _resolve_source_file,
    get_data_preview,
    get_latest_preview,
)

# ===========================================================================
# Limit validation tests
# ===========================================================================


class TestParseLimit:
    def test_default_limit(self):
        assert _parse_limit({}) == 20

    def test_explicit_limit(self):
        assert _parse_limit({"limit": "10"}) == 10

    def test_max_clamp(self):
        assert _parse_limit({"limit": "200"}) == 100

    def test_exactly_100(self):
        assert _parse_limit({"limit": "100"}) == 100

    def test_invalid_non_integer(self):
        with pytest.raises(ValueError, match="Invalid limit"):
            _parse_limit({"limit": "abc"})

    def test_invalid_zero(self):
        with pytest.raises(ValueError, match="Invalid limit"):
            _parse_limit({"limit": "0"})

    def test_invalid_negative(self):
        with pytest.raises(ValueError, match="Invalid limit"):
            _parse_limit({"limit": "-5"})


# ===========================================================================
# 404 — missing run
# ===========================================================================


class TestMissingRun:
    def test_run_not_found_returns_404(self):
        _set_state_items([])
        status, body = _resp(get_data_preview(PIPELINE_ID, "no-such-run", {}))
        assert status == 404
        assert "not found" in body["error"].lower()


# ===========================================================================
# 404 — missing file (run exists but no S3 URIs)
# ===========================================================================


class TestMissingFile:
    def test_no_source_file_returns_404(self):
        _set_state_items([{"step_name": "file_validation", "run_id": RUN_ID}])
        status, body = _resp(get_data_preview(PIPELINE_ID, RUN_ID, {}))
        assert status == 404
        assert "no source file" in body["error"].lower()


# ===========================================================================
# Format detection
# ===========================================================================


class TestFormatDetection:
    def test_csv_extension(self):
        _set_state_items(
            [{"step_name": "file_validation", "run_id": RUN_ID, "original_s3_uri": "s3://bucket/data.csv"}]
        )
        s3_uri, fmt = _resolve_source_file(RUN_ID)
        assert fmt == "csv"
        assert s3_uri == "s3://bucket/data.csv"

    def test_json_extension(self):
        _set_state_items(
            [{"step_name": "file_validation", "run_id": RUN_ID, "original_s3_uri": "s3://bucket/data.json"}]
        )
        _, fmt = _resolve_source_file(RUN_ID)
        assert fmt == "json"

    def test_parquet_extension(self):
        _set_state_items(
            [{"step_name": "file_validation", "run_id": RUN_ID, "original_s3_uri": "s3://bucket/data.parquet"}]
        )
        _, fmt = _resolve_source_file(RUN_ID)
        assert fmt == "parquet"

    def test_json_fallback_when_only_json_uri(self):
        """When only json_s3_uri is present, format is always json."""
        _set_state_items(
            [{"step_name": "schema_extract", "run_id": RUN_ID, "json_s3_uri": "s3://bucket/converted.json"}]
        )
        s3_uri, fmt = _resolve_source_file(RUN_ID)
        assert fmt == "json"
        assert s3_uri == "s3://bucket/converted.json"

    def test_original_preferred_over_json(self):
        _set_state_items(
            [
                {
                    "step_name": "file_validation",
                    "run_id": RUN_ID,
                    "original_s3_uri": "s3://bucket/data.csv",
                    "json_s3_uri": "s3://bucket/converted.json",
                }
            ]
        )
        s3_uri, fmt = _resolve_source_file(RUN_ID)
        assert s3_uri == "s3://bucket/data.csv"
        assert fmt == "csv"


# ===========================================================================
# Latest-run resolution
# ===========================================================================


class TestLatestRunResolution:
    def test_latest_run_delegates_to_get_data_preview(self):
        """get_latest_preview finds the latest run_id and delegates."""
        _set_exec_items([{"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}])
        # Set up state table to return a JSON file for the resolved run
        _set_state_items(
            [{"step_name": "file_validation", "run_id": RUN_ID, "original_s3_uri": "s3://bucket/data.json"}]
        )
        _set_s3_json_body('[{"name": "Alice"}]')

        status, body = _resp(get_latest_preview(PIPELINE_ID, {}))
        assert status == 200
        assert body["source_file"] == "s3://bucket/data.json"

    def test_latest_run_no_runs_returns_404(self):
        _set_exec_items([])
        status, body = _resp(get_latest_preview(PIPELINE_ID, {}))
        assert status == 404
        assert "no runs found" in body["error"].lower()


# ===========================================================================
# Response structure
# ===========================================================================


class TestResponseStructure:
    def test_successful_preview_has_required_fields(self):
        _set_state_items(
            [{"step_name": "file_validation", "run_id": RUN_ID, "original_s3_uri": "s3://bucket/data.json"}]
        )
        _set_s3_json_body('[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]')

        status, body = _resp(get_data_preview(PIPELINE_ID, RUN_ID, {}))
        assert status == 200
        assert "columns" in body
        assert "rows" in body
        assert "total_rows_returned" in body
        assert "source_file" in body
        assert body["total_rows_returned"] == len(body["rows"])
        assert body["source_file"] == "s3://bucket/data.json"

    def test_invalid_limit_returns_400(self):
        status, body = _resp(get_data_preview(PIPELINE_ID, RUN_ID, {"limit": "abc"}))
        assert status == 400
        assert "invalid limit" in body["error"].lower()
