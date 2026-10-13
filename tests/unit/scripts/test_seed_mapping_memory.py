# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for scripts/seed_mapping_memory.py.

Tests the seed script with mocked DynamoDB and Memory API.
"""

import os
import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(SCRIPTS_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("MEMORY_ENABLED", "false")
os.environ.setdefault("MAPPING_MEMORY_ID", "")

from seed_mapping_memory import (
    get_approved_approval_ids,
    get_mappings_for_approvals,
    seed_memory,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_approval(approval_id, pipeline_id, proposed_mappings):
    return {
        "approval_id": approval_id,
        "status": "approved",
        "approval_type": "schema_mapping",
        "pipeline_id": pipeline_id,
        "proposed_mappings": proposed_mappings,
    }


# ---------------------------------------------------------------------------
# Tests: get_approved_approval_ids
# ---------------------------------------------------------------------------


class TestGetApprovedApprovalIds:
    """Scans approvals table for approved schema_mapping records."""

    def test_returns_approved_schema_mapping_records(self):
        mock_table = MagicMock()
        mock_table.scan.return_value = {
            "Items": [
                _make_approval("a1", "pipe-1", [{"source_field": "id", "target_field": "record_id", "confidence": 90}]),
                _make_approval(
                    "a2", "pipe-2", [{"source_field": "name", "target_field": "full_name", "confidence": 85}]
                ),
            ]
        }

        result = get_approved_approval_ids(mock_table)

        assert len(result) == 2
        assert "a1" in result
        assert "a2" in result

    def test_handles_pagination(self):
        mock_table = MagicMock()
        mock_table.scan.side_effect = [
            {
                "Items": [_make_approval("a1", "pipe-1", [])],
                "LastEvaluatedKey": {"approval_id": "a1"},
            },
            {
                "Items": [_make_approval("a2", "pipe-2", [])],
            },
        ]

        result = get_approved_approval_ids(mock_table)

        assert len(result) == 2
        assert mock_table.scan.call_count == 2

    def test_returns_empty_when_no_approvals(self):
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": []}

        result = get_approved_approval_ids(mock_table)

        assert result == {}


# ---------------------------------------------------------------------------
# Tests: get_mappings_for_approvals
# ---------------------------------------------------------------------------


class TestGetMappingsForApprovals:
    """Collects unique field mappings from approval records and mappings table."""

    def test_extracts_mappings_from_approval_proposed_mappings(self):
        approved = {
            "a1": _make_approval(
                "a1",
                "pipe-1",
                [
                    {"source_field": "cust_id", "target_field": "customer_id", "confidence": 95},
                    {"source_field": "fname", "target_field": "first_name", "confidence": 88},
                ],
            ),
        }
        mock_mappings_table = MagicMock()
        mock_mappings_table.scan.return_value = {"Items": []}

        result = get_mappings_for_approvals(mock_mappings_table, approved)

        assert len(result) == 2
        fields = {(m["source_field"], m["target_field"]) for m in result}
        assert ("cust_id", "customer_id") in fields
        assert ("fname", "first_name") in fields

    def test_deduplicates_across_sources(self):
        """Same field pair from approval and mappings table is counted once."""
        approved = {
            "a1": _make_approval(
                "a1",
                "pipe-1",
                [
                    {"source_field": "id", "target_field": "record_id", "confidence": 90},
                ],
            ),
        }
        mock_mappings_table = MagicMock()
        mock_mappings_table.scan.return_value = {
            "Items": [
                {
                    "status": "approved",
                    "pipeline_id": "pipe-2",
                    "mappings": [
                        {"source_field": "id", "target_field": "record_id", "confidence": 92},
                    ],
                }
            ]
        }

        result = get_mappings_for_approvals(mock_mappings_table, approved)

        assert len(result) == 1  # deduplicated

    def test_skips_non_approved_mappings_table_records(self):
        mock_mappings_table = MagicMock()
        mock_mappings_table.scan.return_value = {
            "Items": [
                {
                    "status": "pending",
                    "pipeline_id": "pipe-1",
                    "mappings": [{"source_field": "a", "target_field": "b", "confidence": 50}],
                },
                {
                    "status": "approved",
                    "pipeline_id": "pipe-2",
                    "mappings": [{"source_field": "x", "target_field": "y", "confidence": 80}],
                },
            ]
        }

        result = get_mappings_for_approvals(mock_mappings_table, {})

        assert len(result) == 1
        assert result[0]["source_field"] == "x"


# ---------------------------------------------------------------------------
# Tests: seed_memory
# ---------------------------------------------------------------------------


class TestSeedMemory:
    """Stores mappings in Memory via MappingMemoryClient."""

    def test_stores_each_mapping(self):
        mock_client = MagicMock()
        mock_client.store_mapping_decision.return_value = True

        mappings = [
            {"source_field": "a", "target_field": "b", "confidence": 90, "pipeline_id": "p1"},
            {"source_field": "c", "target_field": "d", "confidence": 85, "pipeline_id": "p2"},
        ]

        result = seed_memory(mappings, mock_client)

        assert result == {"stored": 2, "failed": 0, "skipped": 0}
        assert mock_client.store_mapping_decision.call_count == 2
        mock_client.store_mapping_decision.assert_any_call(
            source_field="a",
            target_field="b",
            confidence=90,
            pipeline_id="p1",
            approved=True,
        )

    def test_dry_run_does_not_write(self):
        mock_client = MagicMock()

        mappings = [
            {"source_field": "a", "target_field": "b", "confidence": 90, "pipeline_id": "p1"},
        ]

        result = seed_memory(mappings, mock_client, dry_run=True)

        assert result == {"stored": 0, "failed": 0, "skipped": 1}
        mock_client.store_mapping_decision.assert_not_called()

    def test_counts_failures(self):
        mock_client = MagicMock()
        mock_client.store_mapping_decision.side_effect = [True, False]

        mappings = [
            {"source_field": "a", "target_field": "b", "confidence": 90, "pipeline_id": "p1"},
            {"source_field": "c", "target_field": "d", "confidence": 85, "pipeline_id": "p2"},
        ]

        result = seed_memory(mappings, mock_client)

        assert result == {"stored": 1, "failed": 1, "skipped": 0}

    def test_empty_mappings(self):
        mock_client = MagicMock()

        result = seed_memory([], mock_client)

        assert result == {"stored": 0, "failed": 0, "skipped": 0}
        mock_client.store_mapping_decision.assert_not_called()
