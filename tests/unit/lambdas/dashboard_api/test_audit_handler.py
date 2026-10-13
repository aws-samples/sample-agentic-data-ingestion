# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the audit log query handler.

Covers:
- Query with audit entries returned
- Empty result (no entries for pipeline)
- Limit parameter restricts result count
- Action filter narrows entries

Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5
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
os.environ.setdefault("AUDIT_TABLE", "test-audit-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_ID = "pipe-audit-001"


def _parse(response: dict) -> tuple:
    """Return (status_code, parsed_body) from a create_response dict."""
    return response["statusCode"], json.loads(response["body"])


SAMPLE_ENTRIES = [
    {
        "resource_id": PIPELINE_ID,
        "timestamp": "2025-01-15T12:00:00+00:00",
        "actor": "user@example.com",
        "action": "pipeline_created",
        "resource_type": "pipeline",
        "details": {"name": "My Pipeline"},
        "ip_address": "10.0.0.1",
    },
    {
        "resource_id": PIPELINE_ID,
        "timestamp": "2025-01-15T11:00:00+00:00",
        "actor": "user@example.com",
        "action": "pipeline_edited",
        "resource_type": "pipeline",
        "details": {"field": "description"},
        "ip_address": "10.0.0.1",
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_audit_table(monkeypatch):
    """Patch the audit_table used by the audit handler."""
    from handlers import common as _common

    mock_table = MagicMock()
    monkeypatch.setattr(_common, "audit_table", mock_table)

    from handlers import audit as _audit

    monkeypatch.setattr(_audit, "audit_table", mock_table)
    _patch_audit_table.table = mock_table
    yield


# ---------------------------------------------------------------------------
# Import handler under test (after env + path setup)
# ---------------------------------------------------------------------------
from handlers.audit import get_pipeline_audit

# ===========================================================================
# Tests
# ===========================================================================


class TestQueryWithEntries:
    """Validates Req 7.1, 7.4: returns audit entries sorted by timestamp desc."""

    def test_returns_entries_for_pipeline(self):
        _patch_audit_table.table.query.return_value = {"Items": SAMPLE_ENTRIES}

        status, body = _parse(get_pipeline_audit(PIPELINE_ID, {}))

        assert status == 200
        assert len(body["audit_entries"]) == 2
        assert body["count"] == 2
        assert body["audit_entries"][0]["action"] == "pipeline_created"
        assert body["audit_entries"][1]["action"] == "pipeline_edited"

    def test_queries_with_correct_key_and_descending_order(self):
        _patch_audit_table.table.query.return_value = {"Items": []}

        get_pipeline_audit(PIPELINE_ID, {})

        call_kwargs = _patch_audit_table.table.query.call_args[1]
        assert call_kwargs["ScanIndexForward"] is False
        assert call_kwargs["Limit"] == 50


class TestEmptyResult:
    """Validates Req 7.5: empty result returns empty array with count 0."""

    def test_returns_empty_array_when_no_entries(self):
        _patch_audit_table.table.query.return_value = {"Items": []}

        status, body = _parse(get_pipeline_audit(PIPELINE_ID, {}))

        assert status == 200
        assert body["audit_entries"] == []
        assert body["count"] == 0


class TestLimitParameter:
    """Validates Req 7.2: limit query param controls max entries."""

    def test_passes_limit_to_query(self):
        _patch_audit_table.table.query.return_value = {"Items": []}

        get_pipeline_audit(PIPELINE_ID, {"limit": "10"})

        call_kwargs = _patch_audit_table.table.query.call_args[1]
        assert call_kwargs["Limit"] == 10

    def test_defaults_to_50_when_limit_missing(self):
        _patch_audit_table.table.query.return_value = {"Items": []}

        get_pipeline_audit(PIPELINE_ID, {})

        call_kwargs = _patch_audit_table.table.query.call_args[1]
        assert call_kwargs["Limit"] == 50

    def test_defaults_to_50_when_limit_invalid(self):
        _patch_audit_table.table.query.return_value = {"Items": []}

        get_pipeline_audit(PIPELINE_ID, {"limit": "not-a-number"})

        call_kwargs = _patch_audit_table.table.query.call_args[1]
        assert call_kwargs["Limit"] == 50


class TestActionFilter:
    """Validates Req 7.3: action query param filters entries."""

    def test_applies_action_filter_expression(self):
        _patch_audit_table.table.query.return_value = {"Items": [SAMPLE_ENTRIES[0]]}

        status, body = _parse(get_pipeline_audit(PIPELINE_ID, {"action": "pipeline_created"}))

        assert status == 200
        call_kwargs = _patch_audit_table.table.query.call_args[1]
        assert "FilterExpression" in call_kwargs

    def test_no_filter_when_action_not_provided(self):
        _patch_audit_table.table.query.return_value = {"Items": []}

        get_pipeline_audit(PIPELINE_ID, {})

        call_kwargs = _patch_audit_table.table.query.call_args[1]
        assert "FilterExpression" not in call_kwargs
