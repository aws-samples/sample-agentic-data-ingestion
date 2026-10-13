# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the lineage query handler.

Covers:
- Successful lineage retrieval (run exists with lineage nodes)
- 404 when the pipeline_id/run_id combination does not exist
- Empty lineage array when the row exists but has no lineage attribute

Validates: Requirements 2.1, 2.2, 2.3, 2.4
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

# Environment variables required by handlers/common.py at import time
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("STATE_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_ID = "pipe-lineage-001"
RUN_ID = "run-abc-123"


def _parse(response: dict) -> tuple:
    """Return (status_code, parsed_body) from a create_response dict."""
    return response["statusCode"], json.loads(response["body"])


SAMPLE_LINEAGE = [
    {
        "step_name": "file_validation",
        "artifact_id": "data.csv",
        "artifact_type": "source_file",
        "s3_uri": "s3://bucket/data.csv",
        "timestamp": "2025-01-15T10:00:00+00:00",
    },
    {
        "step_name": "schema_extract",
        "artifact_id": "abc123hash",
        "artifact_type": "schema",
        "s3_uri": "",
        "timestamp": "2025-01-15T10:01:00+00:00",
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_executions_table(monkeypatch):
    """Patch the executions_table used by the lineage handler."""
    from handlers import common as _common

    mock_table = MagicMock()
    monkeypatch.setattr(_common, "executions_table", mock_table)

    # Also patch the module-level reference in lineage.py (it imports from common)
    from handlers import lineage as _lineage

    monkeypatch.setattr(_lineage, "executions_table", mock_table)
    _patch_executions_table.table = mock_table
    yield


# ---------------------------------------------------------------------------
# Import handler under test (after env + path setup)
# ---------------------------------------------------------------------------
from handlers.lineage import get_run_lineage

# ===========================================================================
# Tests
# ===========================================================================


class TestSuccessfulLineageRetrieval:
    """Validates Req 2.1, 2.2: GET lineage returns the lineage chain."""

    def test_returns_lineage_for_existing_run(self):
        _patch_executions_table.table.get_item.return_value = {
            "Item": {
                "pipeline_id": PIPELINE_ID,
                "run_id": RUN_ID,
                "status": "completed",
                "lineage": SAMPLE_LINEAGE,
            }
        }

        status, body = _parse(get_run_lineage(PIPELINE_ID, RUN_ID))

        assert status == 200
        assert body["run_id"] == RUN_ID
        assert body["pipeline_id"] == PIPELINE_ID
        assert len(body["lineage"]) == 2
        assert body["lineage"][0]["artifact_type"] == "source_file"
        assert body["lineage"][1]["artifact_type"] == "schema"

    def test_queries_with_correct_key(self):
        _patch_executions_table.table.get_item.return_value = {
            "Item": {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID, "lineage": []}
        }

        get_run_lineage(PIPELINE_ID, RUN_ID)

        _patch_executions_table.table.get_item.assert_called_once_with(
            Key={"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}
        )


class TestMissingRun:
    """Validates Req 2.3: 404 when run does not exist."""

    def test_returns_404_when_run_not_found(self):
        _patch_executions_table.table.get_item.return_value = {}

        status, body = _parse(get_run_lineage(PIPELINE_ID, "nonexistent-run"))

        assert status == 404
        assert "not found" in body["error"].lower()
        assert "nonexistent-run" in body["error"]
        assert PIPELINE_ID in body["error"]


class TestEmptyLineage:
    """Validates Req 2.4: empty lineage when attribute absent."""

    def test_returns_empty_lineage_when_attribute_missing(self):
        _patch_executions_table.table.get_item.return_value = {
            "Item": {
                "pipeline_id": PIPELINE_ID,
                "run_id": RUN_ID,
                "status": "running",
                # No "lineage" key at all
            }
        }

        status, body = _parse(get_run_lineage(PIPELINE_ID, RUN_ID))

        assert status == 200
        assert body["lineage"] == []
        assert body["run_id"] == RUN_ID
        assert body["pipeline_id"] == PIPELINE_ID
