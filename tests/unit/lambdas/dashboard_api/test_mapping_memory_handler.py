# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the mapping memory dashboard API handlers.

Covers: GET returns entries, DELETE removes entry, error responses
for both endpoints.

Validates: Requirements 5.8, 5.9, 5.12
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

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
os.environ.setdefault("MEMORY_ENABLED", "true")
os.environ.setdefault("MAPPING_MEMORY_ID", "test-memory-id")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_ID = "pipe-mem-001"


def _parse(response: dict) -> tuple:
    """Return (status_code, parsed_body) from a create_response dict."""
    return response["statusCode"], json.loads(response["body"])


SAMPLE_ENTRIES = [
    {
        "entry_id": "rec-1",
        "source_field": "email",
        "target_field": "email_address",
        "confidence": 95,
        "approved": True,
        "timestamp": "2025-01-15T10:00:00Z",
    },
    {
        "entry_id": "rec-2",
        "source_field": "name",
        "target_field": "full_name",
        "confidence": 80,
        "approved": False,
        "timestamp": "2025-01-14T10:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetMappingMemory:
    """Test GET /api/pipelines/{id}/mapping-memory handler."""

    @patch("handlers.mapping_memory.MappingMemoryClient")
    def test_returns_entries_successfully(self, MockClient):
        mock_instance = MagicMock()
        mock_instance.get_entries.return_value = SAMPLE_ENTRIES
        MockClient.return_value = mock_instance

        from handlers.mapping_memory import get_mapping_memory

        status, body = _parse(get_mapping_memory(PIPELINE_ID))

        assert status == 200
        assert body["pipeline_id"] == PIPELINE_ID
        assert body["count"] == 2
        assert len(body["entries"]) == 2
        assert body["entries"][0]["source_field"] == "email"
        mock_instance.get_entries.assert_called_once_with(PIPELINE_ID)

    @patch("handlers.mapping_memory.MappingMemoryClient")
    def test_returns_empty_list_when_no_entries(self, MockClient):
        mock_instance = MagicMock()
        mock_instance.get_entries.return_value = []
        MockClient.return_value = mock_instance

        from handlers.mapping_memory import get_mapping_memory

        status, body = _parse(get_mapping_memory(PIPELINE_ID))

        assert status == 200
        assert body["count"] == 0
        assert body["entries"] == []

    @patch("handlers.mapping_memory.MappingMemoryClient")
    def test_returns_500_on_exception(self, MockClient):
        MockClient.side_effect = Exception("Memory service unavailable")

        from handlers.mapping_memory import get_mapping_memory

        status, body = _parse(get_mapping_memory(PIPELINE_ID))

        assert status == 500
        assert "error" in body


class TestDeleteMappingMemoryEntry:
    """Test DELETE /api/pipelines/{id}/mapping-memory/{entry_id} handler."""

    @patch("handlers.mapping_memory.MappingMemoryClient")
    def test_deletes_entry_successfully(self, MockClient):
        mock_instance = MagicMock()
        mock_instance.delete_entry.return_value = True
        MockClient.return_value = mock_instance

        from handlers.mapping_memory import delete_mapping_memory_entry

        status, body = _parse(delete_mapping_memory_entry(PIPELINE_ID, "rec-1"))

        assert status == 200
        assert body["entry_id"] == "rec-1"
        assert "deleted" in body["message"].lower()
        mock_instance.delete_entry.assert_called_once_with("rec-1")

    @patch("handlers.mapping_memory.MappingMemoryClient")
    def test_returns_500_when_delete_fails(self, MockClient):
        mock_instance = MagicMock()
        mock_instance.delete_entry.return_value = False
        MockClient.return_value = mock_instance

        from handlers.mapping_memory import delete_mapping_memory_entry

        status, body = _parse(delete_mapping_memory_entry(PIPELINE_ID, "rec-bad"))

        assert status == 500
        assert "error" in body

    @patch("handlers.mapping_memory.MappingMemoryClient")
    def test_returns_500_on_exception(self, MockClient):
        MockClient.side_effect = Exception("Connection refused")

        from handlers.mapping_memory import delete_mapping_memory_entry

        status, body = _parse(delete_mapping_memory_entry(PIPELINE_ID, "rec-1"))

        assert status == 500
        assert "error" in body
