# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for store_task_token in the approval handler Lambda.

Validates AC-7.1 and AC-7.2: task tokens are written to the dedicated
agentic_data_ingestion_task_tokens table with correct attributes and 7-day TTL.
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("STATE_TABLE", "test-state")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

# ---------------------------------------------------------------------------
# Mock setup — patch boto3.resource before importing the handler module
# ---------------------------------------------------------------------------
mock_task_tokens_table = MagicMock()
mock_approvals_table = MagicMock()
mock_state_table = MagicMock()

_table_map = {
    "test-task-tokens": mock_task_tokens_table,
    "test-approvals": mock_approvals_table,
    "test-state": mock_state_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client"):
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    # Use importlib to force loading from the correct file regardless of sys.modules
    import importlib.util

    sys.modules.pop("lambda_function", None)
    _spec = importlib.util.spec_from_file_location(
        "approval_handler_lf",
        os.path.join(LAMBDA_DIR, "lambda_function.py"),
    )
    handler = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(handler)


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_task_tokens_table.reset_mock()
    # Re-patch the handler's table reference in case another test file
    # imported a different lambda_function and polluted the module
    handler.task_tokens_table = mock_task_tokens_table
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

APPROVAL_ID = "approval-abc-123"
TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAAX9p" + "x" * 500  # realistic long token


class TestStoreTaskTokenWritesCorrectItem:
    """store_task_token writes item with correct approval_id, task_token, ttl, created_at."""

    def test_puts_item_with_correct_fields(self):
        before = int(time.time())
        handler.store_task_token(APPROVAL_ID, TASK_TOKEN)

        mock_task_tokens_table.put_item.assert_called_once()
        item = mock_task_tokens_table.put_item.call_args[1]["Item"]

        assert item["approval_id"] == APPROVAL_ID
        assert item["task_token"] == TASK_TOKEN
        assert "ttl" in item
        assert "created_at" in item


class TestStoreTaskTokenTTL:
    """TTL is approximately 7 days in the future (within 10 seconds tolerance)."""

    def test_ttl_is_seven_days_from_now(self):
        before = int(time.time())
        handler.store_task_token(APPROVAL_ID, TASK_TOKEN)
        after = int(time.time())

        item = mock_task_tokens_table.put_item.call_args[1]["Item"]
        ttl = item["ttl"]

        seven_days = 7 * 24 * 60 * 60
        assert ttl >= before + seven_days
        assert ttl <= after + seven_days + 10  # 10s tolerance


class TestStoreTaskTokenUntruncated:
    """Token string is stored untruncated — full length preserved."""

    def test_long_token_stored_in_full(self):
        long_token = "T" * 2048  # very long token
        handler.store_task_token(APPROVAL_ID, long_token)

        item = mock_task_tokens_table.put_item.call_args[1]["Item"]
        assert item["task_token"] == long_token
        assert len(item["task_token"]) == 2048
