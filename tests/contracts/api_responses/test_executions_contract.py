# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Contract test for the executions API response shape, **producer-derived**.

Verifies that the response from get_pipeline_executions carries one stable key
set per row in every arm — including the ``load_outcome`` record — with
``blocks_run`` always ``False``.

**This file does NOT read the frontend interface.** It used to say it verified
the response "matches the PaginatedExecutions TypeScript interface expected by
the React dashboard"; nothing here parses
``dashboard-react/src/hooks/use-executions.ts``, and the field list below is
hand-transcribed from the handler's output. Two things follow. It cannot detect
the producer drifting (``LGR-28b4d75-5``, **template / ratified** — the output is
its own oracle), and the list is not in fact ``PaginatedExecutions``' at all:
that interface declares ``executions`` / ``queued_files`` / ``pagination``, while
these are *row* fields, which ``PipelineExecution`` declares — and it requires
``execution_id``, which is absent here and which the handler does not emit.

The interface-derived authority for the envelope is
``test_executions_envelope_contract.py`` (p48 task 4.2). It parses
``PaginatedExecutions`` / ``ExecutionsPagination`` / ``QueuedFile`` at test time
through the shared reader in ``helpers.py`` (``LGR-99050e6-5``,
``LGR-cd58706-3``). This file is retained for the row-level key set it pins,
which no frontend interface fully describes.

p37 task 6.3 extends this file with the ``load_outcome`` record (R1.7, R1.9).
The field is **additive** — every assertion that was here before is still here,
unweakened; ``load_outcome`` joins EXECUTION_REQUIRED_FIELDS rather than
replacing anything. What this file pins is the *shape*: one stable key set in
every arm, on every row, with ``blocks_run`` always ``False``. The outcome
*values* per input are pinned in test_executions_response_contract.py, and the
handler-level wiring in
tests/unit/lambdas/dashboard_api/test_executions_load_outcome.py.
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
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

PIPELINE_ID = "pipe-contract-001"

# Row fields this file pins, transcribed from the handler's output rather than
# read off a frontend interface — see the module docstring. NOT
# ``PaginatedExecutions``' field set, which is the envelope's three keys; the
# nearest interface is ``PipelineExecution``, and it requires ``execution_id``,
# which is deliberately absent here because the handler emits ``run_id`` instead.
EXECUTION_REQUIRED_FIELDS = {
    "pipeline_id",
    "run_id",
    "status",
    "current_step",
    "steps_status",
    "updated_at",
    # p37 R1.7 / R1.9 — the Load_Outcome record. Never omitted: a run with no
    # counts row carries ``not_established`` rather than nothing, so the
    # frontend never has to distinguish "absent field" from "absent counts".
    "load_outcome",
}

# Keys ``derive_load_outcome`` returns in EVERY arm (shared/load_outcome.py).
# One stable key set is the contract: a reader must not have to branch on the
# outcome to know which keys exist.
LOAD_OUTCOME_REQUIRED_FIELDS = {
    "outcome",
    "rows_read",
    "rows_written",
    "rows_not_written",
    "blocks_run",
    "missing",
    "determined_from",
    "reason",
}

LOAD_OUTCOME_VALUES = {
    "complete_load",
    "partial_load",
    "no_rows_loaded",
    "empty_source",
    "not_established",
}

PAGINATION_REQUIRED_FIELDS = {
    "page",
    "page_size",
    "total_count",
    "total_pages",
    "has_next",
    "has_prev",
}


def _parse_response(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


def _make_full_execution(run_id: str) -> dict:
    """Build a complete execution item with all required fields."""
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": "running",
        "current_step": "schema_extract",
        "steps_status": {
            "file_validation": "completed",
            "schema_extract": "running",
        },
        "updated_at": "2025-01-15T12:05:30+00:00",
        "started_at": "2025-01-15T12:00:00+00:00",
        "file_name": "data.csv",
    }


def _load_metrics_row(run_id: str, **counts) -> dict:
    """A ``(run_id, "load_metrics")`` counts row as ``batch_get_item`` returns it.

    Counts are seeded as ``Decimal`` on purpose. DynamoDB hands number
    attributes back as ``Decimal`` and ``load_outcome._usable_count`` rejects
    ``Decimal`` outright, so a reader that drops the ``item_int`` conversion
    returns ``not_established`` for every run while looking perfectly healthy.
    Seeding ``int`` here would never exercise that conversion, and the contract
    would pass through the regression.
    """
    row = {"run_id": run_id, "step_name": "load_metrics"}
    row.update({key: Decimal(str(value)) for key, value in counts.items()})
    return row


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_executions_table(monkeypatch):
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
    # Additive: the load_outcome enrichment reads the state table through this
    # same batch_get_item, so the tests below need to seed its response. The
    # table name is read off the handler module rather than hardcoded — it comes
    # from require_env("STATE_TABLE", …) at import time, and whichever contract
    # file imported handlers.common first decides the value.
    _patch_executions_table.dynamodb = mock_dynamodb
    _patch_executions_table.state_table_name = _runs.STATE_TABLE_NAME
    yield


def _set_query_response(items):
    _patch_executions_table.table.query.return_value = {"Items": items}


def _set_state_rows(rows):
    """Seed the state-table rows the load_outcome enrichment batch-gets."""
    _patch_executions_table.dynamodb.batch_get_item.return_value = {
        "Responses": {_patch_executions_table.state_table_name: rows},
        "UnprocessedKeys": {},
    }


# ---------------------------------------------------------------------------
# Import handler under test
# ---------------------------------------------------------------------------
from handlers.pipelines import get_pipeline_executions

# ===========================================================================
# Contract tests
# ===========================================================================


class TestResponseTopLevelShape:
    """Response has 'executions' array and 'pagination' object."""

    def test_response_has_executions_and_pagination(self):
        _set_query_response([_make_full_execution("run-001")])

        status, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert isinstance(body["executions"], list)
        assert isinstance(body["pagination"], dict)

    def test_empty_response_still_has_both_keys(self):
        _set_query_response([])

        status, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert "executions" in body
        assert "pagination" in body


class TestExecutionRequiredFields:
    """Each execution has all required fields from the TypeScript interface."""

    def test_execution_has_all_required_fields(self):
        _set_query_response(
            [
                _make_full_execution("run-001"),
                _make_full_execution("run-002"),
            ]
        )

        status, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        for execution in body["executions"]:
            missing = EXECUTION_REQUIRED_FIELDS - set(execution.keys())
            assert not missing, f"Execution missing fields: {missing}"

    def test_steps_status_is_dict(self):
        _set_query_response([_make_full_execution("run-001")])

        _, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        assert isinstance(body["executions"][0]["steps_status"], dict)


class TestLoadOutcomeShapeContract:
    """p37 R1.7 / R1.9 — the Load_Outcome record's shape on the run list.

    The run list enriches per-row from a map keyed by ``run_id``, so every
    assertion here iterates **all** rows. A bug that populated only the first
    execution would satisfy a ``["executions"][0]``-only check and ship.
    """

    def test_load_outcome_present_on_every_execution_row(self):
        """Three runs, none of them seeded with counts: all three still carry
        the field. ``EXECUTION_REQUIRED_FIELDS`` covers this too; it is asserted
        here again against a multi-row page because that is the shape of the
        bug — an enrichment that reaches row 0 and stops."""
        _set_query_response([_make_full_execution(f"run-{i:03d}") for i in range(1, 4)])
        _set_state_rows([])

        status, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert len(body["executions"]) == 3
        for execution in body["executions"]:
            assert "load_outcome" in execution, f"{execution['run_id']} has no load_outcome"

    def test_load_outcome_has_one_stable_key_set_on_every_row(self):
        """Mixed page: one row with counts, one without. Both key sets are
        identical — a reader must not branch on the outcome to know its keys."""
        _set_query_response([_make_full_execution("run-001"), _make_full_execution("run-002")])
        _set_state_rows([_load_metrics_row("run-001", rows_read=10, rows_written=10)])

        _, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        outcomes = [e["load_outcome"] for e in body["executions"]]
        assert len(outcomes) == 2
        for outcome in outcomes:
            missing = LOAD_OUTCOME_REQUIRED_FIELDS - set(outcome.keys())
            assert not missing, f"load_outcome missing fields: {missing}"
        assert set(outcomes[0].keys()) == set(outcomes[1].keys())

    def test_load_outcome_field_types(self):
        _set_query_response([_make_full_execution("run-001")])
        _set_state_rows([_load_metrics_row("run-001", rows_read=1000, rows_written=998)])

        _, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] in LOAD_OUTCOME_VALUES
        assert isinstance(outcome["rows_read"], int)
        assert isinstance(outcome["rows_written"], int)
        assert isinstance(outcome["rows_not_written"], int)
        assert isinstance(outcome["missing"], list)
        assert isinstance(outcome["determined_from"], str)
        assert isinstance(outcome["reason"], str)

    def test_blocks_run_is_false_on_every_row(self):
        """p37 R1.12 and do-NOT 4, encoded as a field and pinned here so a
        future change cannot flip it quietly. ``is False`` rather than
        ``== False``: a falsy stand-in (0, "", None) would pass equality, and
        the point of the field is that it is the boolean ``False``."""
        _set_query_response([_make_full_execution("run-001"), _make_full_execution("run-002")])
        _set_state_rows([_load_metrics_row("run-001", rows_read=5, rows_written=0)])

        _, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        for execution in body["executions"]:
            assert execution["load_outcome"]["blocks_run"] is False

    def test_no_counts_row_is_not_established_naming_the_absence(self):
        """R1.7 — every pre-p37 run reads honestly rather than as a success.
        ``load_metrics_row_absent`` is the provenance, and it is what tells this
        case apart from a row that exists and says nothing."""
        _set_query_response([_make_full_execution("run-001")])
        _set_state_rows([])

        _, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "not_established"
        assert outcome["determined_from"] == "load_metrics_row_absent"
        assert outcome["rows_read"] is None
        assert outcome["rows_written"] is None
        assert outcome["rows_not_written"] == 0
        assert outcome["blocks_run"] is False

    def test_a_seeded_row_does_not_read_as_not_established(self):
        """The Decimal trap, as a contract: seeded counts must produce a real
        outcome. Without the ``item_int`` conversion in the reader this returns
        ``not_established`` and nothing else about the response looks wrong."""
        _set_query_response([_make_full_execution("run-001")])
        _set_state_rows([_load_metrics_row("run-001", rows_read=100, rows_written=100)])

        _, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "complete_load"
        assert outcome["determined_from"] == "load_metrics_row"
        assert outcome["missing"] == []

    def test_load_outcome_survives_json_serialisation_as_plain_types(self):
        """The record crosses the API as JSON, so it must contain no Decimal.
        ``_parse_response`` already round-tripped it — this asserts the counts
        came back as ints rather than as floats, which is what a stray
        ``float()`` in the reader would produce."""
        _set_query_response([_make_full_execution("run-001")])
        _set_state_rows([_load_metrics_row("run-001", rows_read=7, rows_written=3)])

        raw = get_pipeline_executions(PIPELINE_ID)
        outcome = json.loads(raw["body"])["executions"][0]["load_outcome"]

        assert outcome["rows_read"] == 7
        assert not isinstance(outcome["rows_read"], float)
        assert outcome["rows_not_written"] == 4


class TestPaginationRequiredFields:
    """Pagination object has all required fields."""

    def test_pagination_has_all_required_fields(self):
        _set_query_response([_make_full_execution("run-001")])

        _, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        pagination = body["pagination"]
        missing = PAGINATION_REQUIRED_FIELDS - set(pagination.keys())
        assert not missing, f"Pagination missing fields: {missing}"

    def test_pagination_field_types(self):
        _set_query_response([])

        _, body = _parse_response(get_pipeline_executions(PIPELINE_ID))

        p = body["pagination"]
        assert isinstance(p["page"], int)
        assert isinstance(p["page_size"], int)
        assert isinstance(p["total_count"], int)
        assert isinstance(p["total_pages"], int)
        assert isinstance(p["has_next"], bool)
        assert isinstance(p["has_prev"], bool)
