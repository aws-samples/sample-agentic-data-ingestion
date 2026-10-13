# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the rewritten get_pipeline_executions handler.

Validates that the handler queries the executions table by pipeline_id,
returns paginated results with the correct shape, and handles edge cases.
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
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("AWS_REGION", "us-west-2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_ID = "pipe-test-001"


def _parse_response(response: dict) -> tuple:
    """Return (status_code, parsed_body) from a create_response dict."""
    return response["statusCode"], json.loads(response["body"])


def _make_execution_item(run_id: str, status: str = "running", current_step: str = "file_validation") -> dict:
    """Build a mock execution row."""
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": status,
        "current_step": current_step,
        "steps_status": {"file_validation": status},
        "updated_at": "2025-01-15T12:05:30+00:00",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_executions_table(monkeypatch):
    """Patch the executions_table used by the pipelines handler."""
    from handlers import common as _common
    from handlers import pipelines_runs as _pipelines  # P21 Task 7.2 — runs handlers live here

    mock_table = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}
    monkeypatch.setattr(_common, "executions_table", mock_table)
    monkeypatch.setattr(_common, "dynamodb", mock_dynamodb)
    monkeypatch.setattr(_pipelines, "executions_table", mock_table)
    # P21 Task 7.2: get_pipeline_executions now lives in handlers.pipelines_runs;
    # mirror the mocks onto the owning module (module references only).
    from handlers import pipelines_runs as _runs

    monkeypatch.setattr(_runs, "executions_table", mock_table)
    monkeypatch.setattr(_runs, "dynamodb", mock_dynamodb)
    _patch_executions_table.table = mock_table
    yield


def _set_query_response(items, last_evaluated_key=None):
    """Configure the mock executions_table.query to return items."""
    response = {"Items": items}
    if last_evaluated_key:
        response["LastEvaluatedKey"] = last_evaluated_key
    _patch_executions_table.table.query.return_value = response


def _page_read_kwargs():
    """The kwargs of the query that read a **page of rows** (p48 task 3.3).

    ``query.call_args`` is the kwargs of the LAST call, and since task 3.3 the
    handler issues two kinds of query per request: the page read, then a windowless
    ``Select="COUNT"`` walk for the exact ``total_count``. So ``call_args`` now
    names the count query, and every assertion about ``ScanIndexForward``,
    ``IndexName`` or ``Limit`` has to say which query it means.

    Scoped by ``Select`` rather than by call position, so a future reordering of the
    two reads does not silently change what these assertions cover. **This also
    closes a vacuous pass**: the count query sets ``IndexName`` too, so an
    ``IndexName`` assertion made against ``call_args`` would have been satisfied by
    the count query even if the page read had reverted to the base table.

    Note this mock returns one canned dict for BOTH queries, so the count query sees
    a response with no ``Count`` key and the handler's count degrades — deliberately
    left that way: a canned dict cannot faithfully simulate a paginated count walk,
    and the exact-count path is covered against moto in
    ``test_run_history_ordering.py``. What this file gets in exchange is coverage of
    the degrade arm, which must never become a 500 or discard the page.
    """
    page_reads = [
        call.kwargs
        for call in _patch_executions_table.table.query.call_args_list
        if call.kwargs.get("Select") != "COUNT"
    ]
    assert page_reads, "the handler must issue a page read"
    return page_reads[0]


# ---------------------------------------------------------------------------
# Import handler under test (after env + path setup)
# ---------------------------------------------------------------------------
from handlers.pipelines import get_pipeline_executions

# ===========================================================================
# Test cases
# ===========================================================================


class TestPaginatedResults:
    """Returns paginated results with correct shape."""

    def test_returns_executions_with_pagination(self):
        items = [
            _make_execution_item("run-001", "completed", "create_glue_pipeline"),
            _make_execution_item("run-002", "running", "code_generation"),
        ]
        _set_query_response(items)

        status, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert len(body["executions"]) == 2
        assert "pagination" in body
        assert body["pagination"]["page"] == 1
        assert body["pagination"]["page_size"] == 10
        assert body["pagination"]["has_next"] is False
        assert body["pagination"]["has_prev"] is False


class TestEmptyResults:
    """Returns empty list for pipeline with no executions."""

    def test_no_executions_returns_empty(self):
        _set_query_response([])

        status, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert body["executions"] == []
        assert body["pagination"]["total_count"] == 0
        assert body["pagination"]["has_next"] is False


class TestHasNextWhenLastEvaluatedKey:
    """Sets has_next=True when LastEvaluatedKey is present."""

    def test_has_next_true_with_last_evaluated_key(self):
        items = [_make_execution_item(f"run-{i:03d}") for i in range(10)]
        _set_query_response(items, last_evaluated_key={"pipeline_id": PIPELINE_ID, "run_id": "run-009"})

        status, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert body["pagination"]["has_next"] is True


class TestScanIndexForward:
    """Passes ScanIndexForward=False to get newest first."""

    def test_queries_newest_first(self):
        _set_query_response([])

        get_pipeline_executions(PIPELINE_ID)

        # Amended in task 3.3: scoped to the page read. ``call_args`` is the LAST
        # call and that is now C3's ``Select="COUNT"`` query, which carries no
        # ``ScanIndexForward`` — a count has no order. See :func:`_page_read_kwargs`.
        assert _page_read_kwargs()["ScanIndexForward"] is False

    def test_queries_the_chronological_index_not_the_base_table(self):
        """p48 R1.4/R1.5 (task 2.2): ``ScanIndexForward=False`` alone was a lie.

        The base table's sort key is ``run_id``, so descending meant "highest id
        first" and every ``skipped-`` row pinned above every ``run-`` row —
        permanently, because the duplicate-skip id is deterministic in the content
        fingerprint. ``RunsByTimeIndex`` (``(pipeline_id, started_at)``) is what
        makes "newest first" true.

        The ORDERING itself cannot be asserted here: this file's autouse fixture
        replaces the table with a ``MagicMock``, which does no sorting, so a test
        written here would assert that a list the test itself ordered came back
        ordered. It lives in ``test_run_history_ordering.py``, against moto with
        the real key schema. What IS assertable here is the call, and the pairing
        matters — ``ScanIndexForward=False`` against the wrong index is the
        original defect.

        **Amended in task 3.3** to read the page query rather than ``call_args``.
        Not merely a mechanical fix: C3's count query also sets ``IndexName``, so
        against ``call_args`` this assertion would have kept passing even if the page
        read had reverted to the base table — a vacuous pass on the very defect the
        test exists for.
        """
        _set_query_response([])

        get_pipeline_executions(PIPELINE_ID)

        assert _page_read_kwargs()["IndexName"] == "RunsByTimeIndex"
