# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for MappingMemoryClient.get_entries() and delete_entry().

Covers: get_entries returns parsed records sorted by confidence,
delete_entry calls correct API, error handling for both methods,
and disabled client behavior.
"""

import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("MEMORY_ENABLED", "true")
os.environ.setdefault("MAPPING_MEMORY_ID", "test-memory-id")


def _make_memory_record(source: str, target: str, confidence: float, approved: bool, record_id: str, ts: str = ""):
    """Build a mock AgentCore Memory record."""
    status = "approved" if approved else "pending"
    text = (
        f"Field '{source}' maps to '{target}' with {confidence:.0f}% confidence. Status: {status}. Pipeline: test-pipe."
    )
    return {
        "memoryRecordId": record_id,
        "content": {"text": text},
        "score": confidence / 100.0,
        "timestamp": ts or "2025-01-15T10:00:00Z",
    }


class _SdkMemoryRecord:
    """Stand-in for bedrock_agentcore's MemoryRecord (a DictWrapper subclass).

    Mirrors the parts that matter: dict-style ``get``/``[]`` access, NOT a
    ``dict`` subclass, and a ``repr`` that prints the wrapped dict. The SDK is
    only in the Lambda layer, not the test environment.
    """

    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __repr__(self):
        return repr(self._data)


@pytest.fixture
def memory_client():
    """Create a MappingMemoryClient with a mocked boto3 client.

    The session manager is set to None so tests exercise the low-level fallback path.
    """
    with patch("mapping_memory.boto3") as mock_boto3:
        mock_agentcore = MagicMock()
        mock_boto3.client.return_value = mock_agentcore

        from mapping_memory import MappingMemoryClient

        client = MappingMemoryClient(memory_id="test-memory-id", region="us-east-1")
        client.client = mock_agentcore
        client.enabled = True
        # Ensure session manager is None so we test the low-level fallback
        client._session_manager = None
        yield client, mock_agentcore


class TestGetEntries:
    """Test MappingMemoryClient.get_entries() returns parsed records via fallback."""

    def test_returns_parsed_records_sorted_by_confidence(self, memory_client):
        client, mock_agentcore = memory_client

        mock_agentcore.list_memory_records.return_value = {
            "memoryRecords": [
                _make_memory_record("name", "full_name", 75, False, "rec-1"),
                _make_memory_record("email", "email_address", 95, True, "rec-2"),
                _make_memory_record("addr", "address", 60, False, "rec-3"),
            ]
        }

        entries = client.get_entries("test-pipe")

        assert len(entries) == 3
        # Sorted by confidence descending
        assert entries[0]["confidence"] == 95
        assert entries[0]["source_field"] == "email"
        assert entries[0]["target_field"] == "email_address"
        assert entries[0]["approved"] is True
        assert entries[0]["entry_id"] == "rec-2"

        assert entries[1]["confidence"] == 75
        assert entries[2]["confidence"] == 60

    def test_calls_list_memory_records(self, memory_client):
        client, mock_agentcore = memory_client

        mock_agentcore.list_memory_records.return_value = {"memoryRecords": []}

        client.get_entries("my-pipeline", limit=50)

        mock_agentcore.list_memory_records.assert_called_once_with(
            memoryId="test-memory-id",
            namespace="schema-mappings/my-pipeline",
            maxResults=50,
        )

    def test_returns_empty_list_when_no_records(self, memory_client):
        client, mock_agentcore = memory_client

        mock_agentcore.list_memory_records.return_value = {"memoryRecords": []}

        entries = client.get_entries("empty-pipe")
        assert entries == []

    def test_skips_unparseable_records(self, memory_client):
        client, mock_agentcore = memory_client

        mock_agentcore.list_memory_records.return_value = {
            "memoryRecords": [
                {"memoryRecordId": "bad-1", "content": {"text": "garbage text"}, "score": 0.5},
                _make_memory_record("name", "full_name", 90, True, "good-1"),
            ]
        }

        entries = client.get_entries("test-pipe")
        assert len(entries) == 1
        assert entries[0]["entry_id"] == "good-1"

    def test_returns_empty_when_disabled(self, memory_client):
        client, mock_agentcore = memory_client
        client.enabled = False

        entries = client.get_entries("test-pipe")
        assert entries == []
        mock_agentcore.list_memory_records.assert_not_called()

    def test_returns_empty_on_api_error(self, memory_client):
        client, mock_agentcore = memory_client

        mock_agentcore.list_memory_records.side_effect = Exception("API error")

        entries = client.get_entries("test-pipe")
        assert entries == []


class TestGetEntriesWithSessionManager:
    """get_entries when the AgentCore SDK is importable, as it is in the deployed Lambda.

    The dashboard API Lambda carries the Strands layer, so ``_session_manager``
    is set in production. Every entry must still carry the stored source
    field, the record id (the table's row key and the delete handle) and the
    record's creation time, read from the pipeline's own namespace.
    """

    def test_entries_carry_source_field_record_id_and_created_at(self, memory_client):
        client, mock_agentcore = memory_client
        created_at = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        # Real ListMemoryRecords summary shape, keys in botocore order, createdAt a datetime
        summary = {
            "memoryRecordId": "mem-abc123",
            "content": {
                "text": "Field 'cust_email' maps to 'email' with 95% confidence. Status: approved. Pipeline: pipe-1."
            },
            "memoryStrategyId": "strategy-1",
            "namespaces": ["schema-mappings/pipe-1"],
            "createdAt": created_at,
        }
        mock_agentcore.list_memory_records.return_value = {"memoryRecordSummaries": [summary]}
        # The SDK session would hand back the same record wrapped, not as a dict
        client._session_manager = MagicMock()
        session = client._session_manager.create_memory_session.return_value
        session.list_long_term_memory_records.return_value = [_SdkMemoryRecord(summary)]

        entries = client.get_entries("pipe-1", limit=100)

        assert entries == [
            {
                "source_field": "cust_email",
                "target_field": "email",
                "confidence": 95.0,
                "approved": True,
                "entry_id": "mem-abc123",
                "timestamp": created_at.isoformat(),
            }
        ]
        mock_agentcore.list_memory_records.assert_called_once_with(
            memoryId="test-memory-id",
            namespace="schema-mappings/pipe-1",
            maxResults=100,
        )
        client._session_manager.create_memory_session.assert_not_called()


class TestDeleteEntry:
    """Test MappingMemoryClient.delete_entry() calls correct API."""

    def test_deletes_entry_successfully(self, memory_client):
        client, mock_agentcore = memory_client

        mock_agentcore.batch_delete_memory_records.return_value = {}

        result = client.delete_entry("rec-123")

        assert result is True
        mock_agentcore.batch_delete_memory_records.assert_called_once_with(
            memoryId="test-memory-id",
            records=[{"memoryRecordId": "rec-123"}],
        )

    def test_returns_false_when_disabled(self, memory_client):
        client, mock_agentcore = memory_client
        client.enabled = False

        result = client.delete_entry("rec-123")
        assert result is False
        mock_agentcore.batch_delete_memory_records.assert_not_called()

    def test_returns_false_on_api_error(self, memory_client):
        client, mock_agentcore = memory_client

        mock_agentcore.batch_delete_memory_records.side_effect = Exception("Delete failed")

        result = client.delete_entry("rec-123")
        assert result is False
