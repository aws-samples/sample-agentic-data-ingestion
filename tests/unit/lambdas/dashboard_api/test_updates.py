# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for updates endpoint — default response, limit param,
sorting order, mixed statuses, empty result.
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
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(response: dict) -> tuple[int, dict]:
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Sample execution data
# ---------------------------------------------------------------------------

# Five executions rows shaped the way ``execution_updater`` writes one.
#
# ``current_step`` and ``steps_status`` are load-bearing, not decoration. p38
# task 6.2 filters ``get_updates`` on ``is_phantom_execution_row``
# (``handlers/common.py:59``), which excludes rows carrying neither field —
# ``execution_updater`` sets BOTH on its first write for a run
# (``execution_updater/lambda_function.py:236-253``, issued at ``:339-344``,
# nested entry at ``:350-358``). So a ``RUNNING`` or ``SUCCEEDED`` row carrying
# neither has never been touched by the writer that runs when a step reports,
# which is a shape no writer produces.
#
# Without the two fields every row here is phantom-shaped: the filter correctly
# drops all five, the fixture becomes unreachable from the reader, and the cases
# below stop testing the default response, the limit clamp, the sort order and
# the status coverage they name. That is the fixture-side form of
# ``LGR-28b4d75-5`` (**template / ratified**) — a test whose oracle is a
# hand-made shape cannot detect that the producer emits a different one. Do not
# strip these two fields as noise. The subject of the cases below is unchanged;
# none of them reads either field.
SAMPLE_EXECUTIONS = [
    {
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "status": "RUNNING",
        "started_at": "2025-01-15T10:00:00Z",
        "pipeline_name": "Sales ETL",
        "current_step": "create_code",
        "steps_status": {"file_validation": "SUCCEEDED", "create_code": "RUNNING"},
    },
    {
        "pipeline_id": "pipe-002",
        "run_id": "run-002",
        "status": "SUCCEEDED",
        "started_at": "2025-01-15T09:00:00Z",
        "pipeline_name": "Inventory Sync",
        "current_step": "run_pipeline",
        "steps_status": {"file_validation": "SUCCEEDED", "create_code": "SUCCEEDED", "run_pipeline": "SUCCEEDED"},
    },
    {
        "pipeline_id": "pipe-003",
        "run_id": "run-003",
        "status": "FAILED",
        "started_at": "2025-01-15T08:00:00Z",
        "pipeline_name": "Customer Import",
        "current_step": "test_code",
        "steps_status": {"file_validation": "SUCCEEDED", "create_code": "SUCCEEDED", "test_code": "FAILED"},
    },
    {
        "pipeline_id": "pipe-004",
        "run_id": "run-004",
        "status": "WAITING_APPROVAL",
        "started_at": "2025-01-15T11:00:00Z",
        "pipeline_name": "Finance Report",
        "current_step": "create_code",
        "steps_status": {"file_validation": "SUCCEEDED", "create_code": "WAITING_APPROVAL"},
    },
    {
        "pipeline_id": "pipe-005",
        "run_id": "run-005",
        "status": "IN_PROGRESS",
        "started_at": "2025-01-15T07:00:00Z",
        "pipeline_name": "Log Aggregation",
        "current_step": "create_infrastructure",
        "steps_status": {
            "file_validation": "SUCCEEDED",
            "create_code": "SUCCEEDED",
            "create_infrastructure": "RUNNING",
        },
    },
]

# Group sample data by status for mock query responses
_ITEMS_BY_STATUS = {}
for _item in SAMPLE_EXECUTIONS:
    _ITEMS_BY_STATUS.setdefault(_item["status"], []).append(_item)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_executions_table(monkeypatch):
    """Patch executions_table in handlers.common so no real AWS calls are made."""
    from handlers import common as _common

    mock_table = MagicMock()
    monkeypatch.setattr(_common, "executions_table", mock_table)
    _patch_executions_table.mock_table = mock_table
    yield


def _set_query_responses(items_by_status: dict[str, list]):
    """Configure mock to return items grouped by status for GSI queries."""

    def query_side_effect(**kwargs):
        # Key("status").eq(val) stores val at get_expression()["values"][1]
        kce = kwargs.get("KeyConditionExpression")
        status_val = kce.get_expression()["values"][1] if kce else None
        items = items_by_status.get(status_val, [])
        limit = kwargs.get("Limit", 100)
        return {"Items": items[:limit]}

    _patch_executions_table.mock_table.query.side_effect = query_side_effect


def _set_all_statuses_populated():
    """Set up mock to return all sample executions grouped by status."""
    _set_query_responses(_ITEMS_BY_STATUS)


def _set_empty_table():
    """Set up mock to return empty results for all status queries."""
    _patch_executions_table.mock_table.query.return_value = {"Items": []}


def _group_by_status(items: list[dict]) -> dict[str, list]:
    """Group arbitrary rows by ``status`` the way the StatusIndex GSI partitions them."""
    grouped: dict[str, list] = {}
    for item in items:
        grouped.setdefault(item["status"], []).append(item)
    return grouped


# ---------------------------------------------------------------------------
# Import handler under test
# ---------------------------------------------------------------------------
from handlers.health import get_updates

# ===========================================================================
# GET /api/updates — default response
# ===========================================================================


class TestGetUpdatesDefault:
    def test_default_returns_updates_and_count(self):
        """Default call returns updates array and count field."""
        _set_all_statuses_populated()
        status, body = _resp(get_updates({}))

        assert status == 200
        assert "updates" in body
        assert "count" in body
        assert body["count"] == len(body["updates"])
        assert body["count"] == 5


# ===========================================================================
# GET /api/updates?limit=N
# ===========================================================================


class TestGetUpdatesLimit:
    def test_limit_param_restricts_results(self):
        """Passing limit=2 returns at most 2 updates."""
        _set_all_statuses_populated()
        status, body = _resp(get_updates({"limit": "2"}))

        assert status == 200
        assert body["count"] <= 2
        assert len(body["updates"]) <= 2

    def test_limit_clamped_to_100(self):
        """Limit values above 100 are clamped to 100."""
        _set_all_statuses_populated()
        status, body = _resp(get_updates({"limit": "500"}))

        assert status == 200
        # With only 5 items, count should be 5 (not 500)
        assert body["count"] == 5

    def test_default_limit_is_20(self):
        """When no limit is provided, default is 20."""
        _set_all_statuses_populated()
        status, body = _resp(get_updates({}))

        assert status == 200
        # 5 items < 20 default, so all returned
        assert body["count"] == 5


# ===========================================================================
# Sorting order — started_at descending
# ===========================================================================


class TestGetUpdatesSorting:
    def test_results_sorted_by_started_at_descending(self):
        """Updates are sorted by started_at in descending order."""
        _set_all_statuses_populated()
        status, body = _resp(get_updates({}))

        assert status == 200
        timestamps = [u["started_at"] for u in body["updates"]]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_most_recent_first(self):
        """The first update should be the most recent one."""
        _set_all_statuses_populated()
        status, body = _resp(get_updates({}))

        assert status == 200
        # WAITING_APPROVAL at 11:00 is the most recent
        assert body["updates"][0]["started_at"] == "2025-01-15T11:00:00Z"


# ===========================================================================
# Mixed statuses
# ===========================================================================


class TestGetUpdatesMixedStatuses:
    def test_returns_all_status_types(self):
        """Updates include items from all queried statuses."""
        _set_all_statuses_populated()
        status, body = _resp(get_updates({}))

        assert status == 200
        returned_statuses = {u["status"] for u in body["updates"]}
        assert returned_statuses == {"RUNNING", "IN_PROGRESS", "WAITING_APPROVAL", "SUCCEEDED", "FAILED"}


# ===========================================================================
# Empty result
# ===========================================================================


class TestGetUpdatesEmpty:
    def test_empty_table_returns_empty_updates(self):
        """When no executions exist, returns empty updates array."""
        _set_empty_table()
        status, body = _resp(get_updates({}))

        assert status == 200
        assert body["updates"] == []
        assert body["count"] == 0


# ===========================================================================
# Phantom_Execution_Row exclusion — p38 R3.3
# ===========================================================================

# A Phantom_Execution_Row is an executions row no state-recording writer has
# ever touched: it carries a ``status`` but neither ``current_step`` nor
# ``steps_status``. The rows below reproduce the shape observed live — only
# ``pipeline_id``, ``run_id``, ``status``, ``trigger``, ``retry_source_run_id``,
# ``execution_name``, ``started_at`` and ``updated_at``, with
# ``started_at == updated_at`` because nothing ever updated the row after the
# ``put_item`` that created it.
#
# ``status="RUNNING"`` is not arbitrary: the phantoms sit permanently in the
# ``RUNNING`` partition of ``StatusIndex``, which is the partition this endpoint
# queries first. ``LGR-21a1b5b-2`` (**template / ratified**) names that GSI as
# one of the three readers a non-run row reaches.
PHANTOM_EXECUTIONS = [
    {
        "pipeline_id": "pipe-001",
        "run_id": "retry-run-001-a1b2c3d4",
        "status": "RUNNING",
        "trigger": "retry",
        "retry_source_run_id": "run-001",
        "execution_name": "retry-run-001-a1b2c3d4",
        "started_at": "2025-01-15T12:00:00Z",
        "updated_at": "2025-01-15T12:00:00Z",
    },
    {
        "pipeline_id": "pipe-002",
        "run_id": "retry-run-002-e5f6a7b8",
        "status": "RUNNING",
        "trigger": "retry",
        "retry_source_run_id": "run-002",
        "execution_name": "retry-run-002-e5f6a7b8",
        "started_at": "2025-01-15T12:30:00Z",
        "updated_at": "2025-01-15T12:30:00Z",
    },
    {
        "pipeline_id": "pipe-003",
        "run_id": "retry-run-003-c9d0e1f2",
        "status": "RUNNING",
        "trigger": "retry",
        "retry_source_run_id": "run-003",
        "execution_name": "retry-run-003-c9d0e1f2",
        "started_at": "2025-01-15T13:00:00Z",
        "updated_at": "2025-01-15T13:00:00Z",
    },
]

# A row shaped like a Skipped_Run_Record: ``current_step`` present,
# ``steps_status`` absent (``pipeline_trigger/lambda_function.py:243`` for the
# duplicate-content skip, ``:499`` for the contention skip). It must survive the
# filter — ``LGR-21a1b5b-4`` (**template / ratified**) shaped that record to
# interleave chronologically with real runs, and a predicate keyed on
# ``steps_status`` alone would hide it and undo that decision.
#
# Its real ``status`` is ``SKIPPED``, which this endpoint never queries, so a
# genuine skip record cannot reach ``all_updates`` at all. It is placed under a
# queried status here so that the filter actually sees it: the assertion below
# is about ``get_updates``' filter admitting the two-field shape, not a claim
# that a skip record is ever ``RUNNING``.
SKIP_SHAPED_EXECUTION = {
    "pipeline_id": "pipe-006",
    "run_id": "run-006",
    "status": "RUNNING",
    "started_at": "2025-01-15T06:00:00Z",
    "pipeline_name": "Nightly Rollup",
    "current_step": "duplicate_check",
}


class TestGetUpdatesExcludesPhantomRows:
    """p38 R3.3 — phantoms are absent from ``updates`` AND from ``count``.

    ``count`` is the answer that is wrong today. Both are asserted, and the
    ``count`` assertion is on the VALUE — the number of real rows seeded — not on
    ``count == len(updates)``, which is true by construction: ``count`` is
    ``len(updates)`` (``health.py:123``) and ``updates`` is a slice of the
    filtered list (``:117``), so that comparison holds whether the filter runs
    or not.

    **Known limitation, deliberately not tested around.** The per-status
    ``Limit`` is server-side (``health.py:72``) and runs upstream of this
    client-side filter, so a phantom still consumes a query slot and the endpoint
    can under-fill. That is inherent to reader-side exclusion and identical in
    task 6.1's filter; seeding more phantoms than ``limit`` would exercise the
    under-fill rather than the exclusion. The mixes below stay well inside the
    default ``limit`` of 20.
    """

    def test_phantoms_absent_from_updates_and_from_count(self):
        """A mixed StatusIndex returns the real rows only, and counts only those."""
        real_rows = SAMPLE_EXECUTIONS
        _set_query_responses(_group_by_status([*real_rows, *PHANTOM_EXECUTIONS]))

        status, body = _resp(get_updates({}))

        assert status == 200

        returned_ids = [u["run_id"] for u in body["updates"]]
        for phantom in PHANTOM_EXECUTIONS:
            assert phantom["run_id"] not in returned_ids
        assert set(returned_ids) == {r["run_id"] for r in real_rows}

        # The value, not ``count == len(updates)``: five real rows were seeded
        # alongside three phantoms, so the honest count is five.
        assert body["count"] == len(real_rows)
        assert body["count"] == 5

    def test_only_phantoms_yields_no_updates_and_a_zero_count(self):
        """A partition holding nothing but phantoms reports nothing in progress."""
        _set_query_responses(_group_by_status(PHANTOM_EXECUTIONS))

        status, body = _resp(get_updates({}))

        assert status == 200
        assert body["updates"] == []
        assert body["count"] == 0

    def test_skip_shaped_row_survives_the_filter(self):
        """A row carrying ``current_step`` and no ``steps_status`` is not a phantom."""
        _set_query_responses(_group_by_status([SKIP_SHAPED_EXECUTION, *PHANTOM_EXECUTIONS]))

        status, body = _resp(get_updates({}))

        assert status == 200
        assert [u["run_id"] for u in body["updates"]] == [SKIP_SHAPED_EXECUTION["run_id"]]
        assert body["count"] == 1
