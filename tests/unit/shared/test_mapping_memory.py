# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for MappingMemoryClient (Task 10.7).

Tests store/query/format flow and disabled mode with mocked bedrock-agentcore Memory API.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add shared directory to import path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from mapping_memory import MappingMemoryClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    """Create a MappingMemoryClient with a mocked boto3 client."""
    with patch.dict(os.environ, {"MEMORY_ENABLED": "true", "MAPPING_MEMORY_ID": "test-memory-abc1234567"}):
        client = MappingMemoryClient(memory_id="test-memory-abc1234567")
        client.client = MagicMock()
        return client


@pytest.fixture
def disabled_client():
    """Create a MappingMemoryClient with Memory disabled."""
    with patch.dict(os.environ, {"MEMORY_ENABLED": "false"}):
        return MappingMemoryClient()


# ---------------------------------------------------------------------------
# Task 10.1 — Constructor
# ---------------------------------------------------------------------------


class TestMappingMemoryClientInit:
    def test_enabled_with_memory_id(self):
        with patch.dict(os.environ, {"MEMORY_ENABLED": "true", "MAPPING_MEMORY_ID": "mem-abc1234567"}):
            with patch("mapping_memory.boto3") as mock_boto:
                client = MappingMemoryClient()
                assert client.enabled is True
                assert client.memory_id == "mem-abc1234567"
                mock_boto.client.assert_called_once()

    def test_disabled_via_env_var(self):
        with patch.dict(os.environ, {"MEMORY_ENABLED": "false"}, clear=False):
            client = MappingMemoryClient()
            assert client.enabled is False
            assert client.client is None

    def test_disabled_when_no_memory_id(self):
        env = {"MEMORY_ENABLED": "true"}
        with patch.dict(os.environ, env, clear=True):
            client = MappingMemoryClient()
            assert client.enabled is False


# ---------------------------------------------------------------------------
# Task 10.2 — store_mapping_decision
# ---------------------------------------------------------------------------


class TestStoreMappingDecision:
    def test_stores_mapping_via_batch_create(self, mock_client):
        result = mock_client.store_mapping_decision(
            source_field="cust_id",
            target_field="customer_id",
            confidence=95.0,
            pipeline_id="test-pipeline",
            approved=True,
        )

        assert result is True
        # Fallback path now uses batch_create_memory_records only (no create_event)
        mock_client.client.batch_create_memory_records.assert_called_once()
        mock_client.client.create_event.assert_not_called()

        # Verify memory record payload
        call = mock_client.client.batch_create_memory_records.call_args
        assert call.kwargs["memoryId"] == "test-memory-abc1234567"
        record = call.kwargs["records"][0]
        assert "cust_id" in record["content"]["text"]
        assert "customer_id" in record["content"]["text"]
        assert "95%" in record["content"]["text"]
        assert record["namespaces"] == ["schema-mappings/test-pipeline"]
        assert "requestIdentifier" in record
        assert "timestamp" in record

    def test_returns_false_when_disabled(self, disabled_client):
        result = disabled_client.store_mapping_decision(source_field="x", target_field="y", confidence=50.0)
        assert result is False

    def test_returns_false_on_api_error(self, mock_client):
        mock_client.client.batch_create_memory_records.side_effect = Exception("API error")
        result = mock_client.store_mapping_decision(source_field="x", target_field="y", confidence=50.0)
        assert result is False


# ---------------------------------------------------------------------------
# get_similar_mappings — batched retrieval (R15.4, p28 Task 12.3)
# ---------------------------------------------------------------------------


def _record(source_field, target_field="target_x", score=0.5, record_id="rec-x"):
    """Build a memory record in the stored-text shape from store_mapping_decision."""
    return {
        "content": {
            "text": (
                f"Field '{source_field}' maps to '{target_field}' with 90% confidence. Status: approved. Pipeline: p1."
            )
        },
        "score": score,
        "memoryRecordId": record_id,
    }


class TestGetSimilarMappings:
    """One billable retrieval per mapping run + client-side field filtering (R15.4)."""

    def test_single_retrieve_call_for_n_fields_low_level(self, mock_client):
        """N fields produce exactly ONE retrieve_memory_records call, not N."""
        fields = [f"field_{i}" for i in range(12)]
        mock_client._session_manager = None
        mock_client.client.retrieve_memory_records.return_value = {
            "memoryRecords": [_record("field_0", score=0.9, record_id="rec-1")]
        }

        results = mock_client.get_similar_mappings(fields)

        assert mock_client.client.retrieve_memory_records.call_count == 1
        call = mock_client.client.retrieve_memory_records.call_args
        assert call.kwargs["memoryId"] == "test-memory-abc1234567"
        assert call.kwargs["namespace"] == "schema-mappings"
        # top_k sized to field count: max(limit=10, 12 fields * 3) = 36
        assert call.kwargs["searchCriteria"]["topK"] == 36
        # Single batched query names the requested fields
        search_query = call.kwargs["searchCriteria"]["searchQuery"]
        assert "'field_0'" in search_query
        assert "'field_11'" in search_query
        assert len(results) == 1

    def test_single_search_call_for_n_fields_session_path(self, mock_client):
        """Session path makes exactly ONE search_long_term_memories call for N fields."""
        fields = [f"field_{i}" for i in range(12)]
        mock_session = MagicMock()
        mock_session.search_long_term_memories.return_value = [
            _record("field_3", score=0.9, record_id="rec-1"),
            _record("unrequested_field", score=0.99, record_id="rec-2"),
        ]
        mock_client._session_manager = MagicMock()

        with patch.object(mock_client, "_get_session", return_value=mock_session):
            results = mock_client.get_similar_mappings(fields)

        assert mock_session.search_long_term_memories.call_count == 1
        call = mock_session.search_long_term_memories.call_args
        assert call.kwargs["top_k"] == 36
        # Client-side filter: requested field kept, unrequested excluded
        assert len(results) == 1
        assert "'field_3'" in results[0]["text"]
        # Low-level API not touched when the session path yields results
        mock_client.client.retrieve_memory_records.assert_not_called()

    def test_top_k_capped_at_api_maximum(self, mock_client):
        """top_k never exceeds the API maximum (100), even for very wide schemas."""
        fields = [f"f_{i}" for i in range(50)]
        mock_client._session_manager = None
        mock_client.client.retrieve_memory_records.return_value = {"memoryRecords": []}

        mock_client.get_similar_mappings(fields)

        call = mock_client.client.retrieve_memory_records.call_args
        assert call.kwargs["searchCriteria"]["topK"] == 100

    def test_client_side_filtering_excludes_unrequested_fields(self, mock_client):
        """Records for fields not in the request are filtered out client-side."""
        mock_client._session_manager = None
        mock_client.client.retrieve_memory_records.return_value = {
            "memoryRecords": [
                _record("cust_id", "customer_id", score=0.95, record_id="rec-1"),
                _record("zzz_unrelated", "other", score=0.99, record_id="rec-2"),
                _record("email", "email_address", score=0.80, record_id="rec-3"),
            ]
        }

        results = mock_client.get_similar_mappings(["cust_id", "email"])

        texts = [r["text"] for r in results]
        assert len(results) == 2
        assert any("'cust_id'" in t for t in texts)
        assert any("'email'" in t for t in texts)
        assert not any("zzz_unrelated" in t for t in texts)

    def test_deduplicates_and_sorts_by_score(self, mock_client):
        """Duplicate texts collapse to one result; results sorted by score descending."""
        dup_text = "Field 'cust_id' maps to 'customer_id' with 95% confidence. Status: approved. Pipeline: p1."
        mock_client._session_manager = None
        mock_client.client.retrieve_memory_records.return_value = {
            "memoryRecords": [
                {"content": {"text": dup_text}, "score": 0.95, "memoryRecordId": "rec-1"},
                {
                    "content": {
                        "text": "Field 'email' maps to 'email_address' with 90% confidence. Status: approved. Pipeline: p1."
                    },
                    "score": 0.80,
                    "memoryRecordId": "rec-2",
                },
                {"content": {"text": dup_text}, "score": 0.90, "memoryRecordId": "rec-1"},
            ]
        }

        results = mock_client.get_similar_mappings(["cust_id", "email"])

        assert len(results) == 2  # Deduplicated
        assert results[0]["score"] == 0.95  # Highest score first
        assert mock_client.client.retrieve_memory_records.call_count == 1

    def test_returns_empty_when_disabled(self, disabled_client):
        assert disabled_client.get_similar_mappings(["field1"]) == []

    def test_returns_empty_for_empty_fields(self, mock_client):
        assert mock_client.get_similar_mappings([]) == []

    def test_batched_call_failure_returns_empty(self, mock_client):
        """The one batched low-level call failing → warning + empty result (no per-field retries)."""
        mock_client._session_manager = None
        mock_client.client.retrieve_memory_records.side_effect = Exception("timeout")

        results = mock_client.get_similar_mappings(["bad_field", "name"])

        assert results == []
        assert mock_client.client.retrieve_memory_records.call_count == 1

    def test_session_failure_falls_back_to_single_low_level_call(self, mock_client):
        """Session search failing falls back to the low-level path — still one call per path."""
        mock_session = MagicMock()
        mock_session.search_long_term_memories.side_effect = Exception("session error")
        mock_client._session_manager = MagicMock()
        mock_client.client.retrieve_memory_records.return_value = {
            "memoryRecords": [_record("cust_id", "customer_id", score=0.9, record_id="rec-1")]
        }

        with patch.object(mock_client, "_get_session", return_value=mock_session):
            results = mock_client.get_similar_mappings(["cust_id", "email"])

        assert mock_session.search_long_term_memories.call_count == 1
        assert mock_client.client.retrieve_memory_records.call_count == 1
        assert len(results) == 1

    def test_session_path_reads_sdk_memory_records(self, mock_client):
        """SDK search results are MemoryRecord wrappers, not dicts, and must parse as records.

        Each result keeps the stored mapping text, its relevance score and its
        record id, so the Historical Mapping Patterns prompt names the real
        source field.
        """
        # MemoryRecordSummary in botocore key order: memoryRecordId comes first
        stored = {
            "memoryRecordId": "rec-1",
            "content": {
                "text": "Field 'cust_id' maps to 'customer_id' with 90% confidence. Status: approved. Pipeline: p1."
            },
            "namespaces": ["schema-mappings/p1"],
            "score": 0.9,
        }
        mock_session = MagicMock()
        mock_session.search_long_term_memories.return_value = [_SdkMemoryRecord(stored)]
        mock_client._session_manager = MagicMock()

        with patch.object(mock_client, "_get_session", return_value=mock_session):
            results = mock_client.get_similar_mappings(["cust_id"])

        assert results == [{"text": stored["content"]["text"], "score": 0.9, "record_id": "rec-1"}]
        prompt = mock_client.format_historical_context(results)
        assert "| cust_id | customer_id | 90% | 1 | 0 |" in prompt
        assert "memoryRecordId" not in prompt


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


# ---------------------------------------------------------------------------
# Task 10.4 — store_approval_feedback
# ---------------------------------------------------------------------------


class TestStoreApprovalFeedback:
    def test_stores_approval_event(self, mock_client):
        result = mock_client.store_approval_feedback(mapping_id="approval-123", approved=True)

        assert result is True
        mock_client.client.create_event.assert_called_once()
        call = mock_client.client.create_event.call_args
        assert call.kwargs["actorId"] == "reviewer:approval-123"
        payload_text = call.kwargs["payload"][0]["conversational"]["content"]["text"]
        assert "mapping_approved" in payload_text
        assert "approval-123" in payload_text

    def test_stores_rejection_with_notes(self, mock_client):
        result = mock_client.store_approval_feedback(
            mapping_id="approval-456",
            approved=False,
            reviewer_notes="Wrong target field",
        )

        assert result is True
        call = mock_client.client.create_event.call_args
        payload_text = call.kwargs["payload"][0]["conversational"]["content"]["text"]
        assert "mapping_rejected" in payload_text
        assert "Wrong target field" in payload_text

    def test_returns_false_when_disabled(self, disabled_client):
        assert disabled_client.store_approval_feedback("id", True) is False

    def test_returns_false_on_api_error(self, mock_client):
        mock_client.client.create_event.side_effect = Exception("API error")
        result = mock_client.store_approval_feedback("id", True)
        assert result is False


# ---------------------------------------------------------------------------
# Task 10.5 — format_historical_context
# ---------------------------------------------------------------------------


class TestFormatHistoricalContext:
    def test_formats_table_with_rules(self, mock_client):
        mappings = [
            {
                "text": "Field 'cust_id' maps to 'customer_id' with 95% confidence. Status: approved. Pipeline: p1.",
                "score": 0.95,
            },
            {
                "text": "Field 'fname' maps to 'first_name' with 92% confidence. Status: approved. Pipeline: p2.",
                "score": 0.90,
            },
        ]

        result = mock_client.format_historical_context(mappings)

        assert "## Historical Mapping Patterns" in result
        assert "| Source Field | Target Field | Confidence | Approvals | Rejections |" in result
        assert "| cust_id | customer_id | 95% | 1 | 0 |" in result
        assert "| fname | first_name | 92% | 1 | 0 |" in result
        assert "99% confidence" in result  # Rule about exact matches
        assert "rejected" in result.lower()  # Rule about rejections

    def test_formats_rejected_mapping(self, mock_client):
        mappings = [
            {
                "text": "Field 'dt' maps to 'created_at' with 78% confidence. Status: rejected. Pipeline: p3.",
                "score": 0.70,
            },
        ]

        result = mock_client.format_historical_context(mappings)
        assert "| dt | created_at | 78% | 0 | 1 |" in result

    def test_returns_empty_for_no_mappings(self, mock_client):
        assert mock_client.format_historical_context([]) == ""

    def test_returns_empty_for_unparseable_mappings(self, mock_client):
        mappings = [{"text": "garbage data", "score": 0.5}]
        assert mock_client.format_historical_context(mappings) == ""


# ---------------------------------------------------------------------------
# Task 10.6 — MEMORY_ENABLED=false graceful handling
# ---------------------------------------------------------------------------


class TestDisabledMode:
    """All methods return empty/false results without calling the API."""

    def test_store_mapping_decision_noop(self, disabled_client):
        assert disabled_client.store_mapping_decision("a", "b", 50.0) is False

    def test_get_similar_mappings_noop(self, disabled_client):
        assert disabled_client.get_similar_mappings(["field1", "field2"]) == []

    def test_store_approval_feedback_noop(self, disabled_client):
        assert disabled_client.store_approval_feedback("id", True) is False

    def test_format_historical_context_still_works(self, disabled_client):
        """format_historical_context is pure formatting — works even when disabled."""
        mappings = [
            {
                "text": "Field 'x' maps to 'y' with 80% confidence. Status: approved. Pipeline: p1.",
                "score": 0.8,
            }
        ]
        result = disabled_client.format_historical_context(mappings)
        assert "## Historical Mapping Patterns" in result


# ---------------------------------------------------------------------------
# _parse_mapping_text
# ---------------------------------------------------------------------------


class TestParseMappingText:
    def test_parses_approved_mapping(self):
        text = "Field 'cust_id' maps to 'customer_id' with 95% confidence. Status: approved. Pipeline: p1."
        result = MappingMemoryClient._parse_mapping_text(text)
        assert result == {
            "source_field": "cust_id",
            "target_field": "customer_id",
            "confidence": 95.0,
            "approved": True,
        }

    def test_parses_pending_mapping(self):
        text = "Field 'dt' maps to 'created_at' with 78% confidence. Status: pending. Pipeline: p2."
        result = MappingMemoryClient._parse_mapping_text(text)
        assert result is not None
        assert result["approved"] is False

    def test_returns_none_for_empty(self):
        assert MappingMemoryClient._parse_mapping_text("") is None
        assert MappingMemoryClient._parse_mapping_text(None) is None

    def test_returns_none_for_garbage(self):
        assert MappingMemoryClient._parse_mapping_text("random text") is None
