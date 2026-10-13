# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: dashboard API read paths surface infra errors as 5xx (P28 R17.3).

Two masquerades removed:

- ``get_pipeline_details``: a DynamoDB error while checking the pipelines
  table used to return 404 "Pipeline not found" — a throttle is now a 500,
  and only a genuinely-missing item is a 404.
- ``get_pipeline_schema_history``: any read error used to return [] —
  indistinguishable from a pipeline with no schema history. Errors now
  propagate to callers (the list-view enrichment degrades per-pipeline;
  endpoint-level errors surface via handle_error as 5xx).

Genuine not-found response shapes are unchanged (contract preserved).

Validates: P28 Requirements 17.3
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

# Environment variables required by handlers/common.py at import time
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

from handlers import pipelines_crud as _crud


def _client_error(code: str, operation: str = "GetItem") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, operation)


def _parse_response(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


PIPELINE_ID = "pipe-read-path"


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    """Patch pipelines_crud module tables and clear the schema-history cache."""
    state_table = MagicMock()
    pipelines_table = MagicMock()
    monkeypatch.setattr(_crud, "state_table", state_table)
    monkeypatch.setattr(_crud, "pipelines_table", pipelines_table)
    # Empty state data → get_pipeline_details takes the pipelines-table branch
    state_table.query.return_value = {"Items": []}
    _patch_tables.state_table = state_table
    _patch_tables.pipelines_table = pipelines_table
    yield


class TestGetPipelineDetailsNotFoundVsInfraError:
    def test_missing_pipeline_returns_404(self):
        _patch_tables.pipelines_table.get_item.return_value = {}

        status, body = _parse_response(_crud.get_pipeline_details(PIPELINE_ID))

        assert status == 404
        assert "error" in body
        assert PIPELINE_ID in body["error"]

    def test_throttle_returns_5xx_not_404(self):
        _patch_tables.pipelines_table.get_item.side_effect = _client_error("ThrottlingException")

        status, body = _parse_response(_crud.get_pipeline_details(PIPELINE_ID))

        assert status == 500
        assert "error" in body

    def test_access_denied_returns_5xx_not_404(self):
        _patch_tables.pipelines_table.get_item.side_effect = _client_error("AccessDeniedException")

        status, _body = _parse_response(_crud.get_pipeline_details(PIPELINE_ID))

        assert status == 500

    def test_existing_pipeline_without_runs_returns_200_basic_info(self):
        _patch_tables.pipelines_table.get_item.return_value = {
            "Item": {"pipeline_id": PIPELINE_ID, "pipeline_name": "My Pipeline"}
        }

        status, body = _parse_response(_crud.get_pipeline_details(PIPELINE_ID))

        assert status == 200
        assert body["pipeline_name"] == "My Pipeline"
        assert body["schema_history"] == []


class TestGetPipelineSchemaHistoryErrors:
    def test_throttle_propagates_instead_of_empty_history(self, monkeypatch):
        # Bypass the module-level response cache for this key
        monkeypatch.setattr(_crud, "_get_cached", lambda key: None)
        monkeypatch.setattr(_crud, "_set_cache", lambda key, value: None)
        _patch_tables.state_table.query.side_effect = _client_error("ThrottlingException", "Query")

        with pytest.raises(ClientError):
            _crud.get_pipeline_schema_history(PIPELINE_ID)

    def test_no_items_still_returns_empty_history(self, monkeypatch):
        monkeypatch.setattr(_crud, "_get_cached", lambda key: None)
        monkeypatch.setattr(_crud, "_set_cache", lambda key, value: None)
        _patch_tables.state_table.query.return_value = {"Items": []}

        assert _crud.get_pipeline_schema_history(PIPELINE_ID) == []

    def test_list_view_enrichment_degrades_per_pipeline_on_throttle(self, monkeypatch):
        """The pipelines list keeps its per-pipeline degradation catch: a
        throttled enrichment yields a minimal entry, not a failed page."""
        monkeypatch.setattr(_crud, "_get_cached", lambda key: None)
        monkeypatch.setattr(_crud, "_set_cache", lambda key, value: None)
        executions_table = MagicMock()
        executions_table.query.return_value = {"Items": []}
        monkeypatch.setattr(_crud, "executions_table", executions_table)

        _patch_tables.pipelines_table.scan.return_value = {
            "Items": [{"pipeline_id": PIPELINE_ID, "pipeline_name": "My Pipeline", "updated_at": "2026-01-01"}]
        }
        _patch_tables.state_table.query.side_effect = _client_error("ThrottlingException", "Query")

        status, body = _parse_response(_crud.get_pipelines())

        assert status == 200
        assert len(body["pipelines"]) == 1
        entry = body["pipelines"][0]
        assert entry["pipeline_id"] == PIPELINE_ID
        assert entry["schema_history"] == []
