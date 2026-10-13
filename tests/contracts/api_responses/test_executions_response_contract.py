# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /pipeline/{id}/executions response shape, **producer-derived**.

Validates that the response includes pending_approval_id, schema_version and
steps_status, plus the enriched ``load_outcome`` values.

**This file does NOT read the frontend interface, and the claim that it did was
false.** Its docstring used to say these fields were checked "as expected by the
PaginatedExecutions TypeScript interface". Nothing here parses
``dashboard-react/src/hooks/use-executions.ts``; every expectation below is
derived from the handler's own output, which is the mechanism ``LGR-28b4d75-5``
(**template / ratified**) rejects by name — "a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle". The cost was measured, not hypothetical: this file asserted a six-key
``pagination`` block that ``PaginatedExecutions`` did not declare at all, while
all four fields the interface *did* declare (``total_count``, ``page``,
``page_size``, ``has_more``) read ``undefined`` in the browser. A docstring that
misnames its own oracle is how that survived, which is why the correction is
here rather than only in the new file.

**The interface-derived authority for this envelope is
``test_executions_envelope_contract.py``** (p48 task 4.2), which parses
``PaginatedExecutions`` / ``ExecutionsPagination`` / ``QueuedFile`` at test time
through the shared reader in ``helpers.py`` (``LGR-99050e6-5``,
``LGR-cd58706-3``). Envelope and ``pagination`` questions belong there. What this
file still pins usefully is the **row enrichment** — ``pending_approval_id``,
``schema_version``, ``steps_status``, ``load_outcome`` — and the ``QUEUED``
split, none of which the frontend interface fully describes. ``TestPaginationFields``
below is retained rather than deleted: it is a weaker, producer-derived statement
of a subset of the same keys, and it exercises the count-degrade path, because
its canned ``query.return_value`` carries no ``Count``.

Task 7.2 — Requirements 4.2, 4.8

p37 task 6.3 extends this file with the ``load_outcome`` record's **values**
(R1.7, R1.9) — which outcome and which provenance reach the response for a given
counts row. The record's *key set* is pinned in test_executions_contract.py; the
handler wiring in tests/unit/lambdas/dashboard_api/test_executions_load_outcome.py.
The field is additive: nothing that was asserted here before has been weakened.
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

os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

sys.modules.pop("lambda_function", None)

from handlers import common as _common
from handlers import pipelines_runs as _pipelines  # P21 Task 7.2 — runs handlers live here
from handlers.pipelines import get_pipeline_executions

PIPELINE_ID = "pipe-contract-exec-001"


def _make_execution(run_id, status="RUNNING", with_approval=False, with_steps_status=True):
    """Build a realistic execution DynamoDB item."""
    item = {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": status,
        "started_at": "2025-01-15T12:00:00Z",
        "updated_at": "2025-01-15T12:05:00Z",
        "current_step": "schema_extract",
        "file_name": "data.csv",
    }
    if with_steps_status:
        item["steps_status"] = {
            "file_validation": "completed",
            "schema_extract": "running",
        }
    if with_approval:
        item["pending_approval_id"] = "appr-001"
    return item


def _load_metrics_row(run_id, **counts):
    """A ``(run_id, "load_metrics")`` counts row as ``batch_get_item`` returns it.

    Counts are ``Decimal`` deliberately: that is what DynamoDB gives back for a
    number attribute, and ``load_outcome._usable_count`` rejects ``Decimal``. A
    reader that drops the ``item_int`` conversion answers ``not_established`` for
    every run and looks entirely healthy doing it, so seeding ``int`` here would
    let that regression through the contract.
    """
    row = {"run_id": run_id, "step_name": "load_metrics"}
    row.update({key: Decimal(str(value)) for key, value in counts.items()})
    return row


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    mock_exec = MagicMock()
    mock_state = MagicMock()
    mock_approvals = MagicMock()
    mock_dynamodb = MagicMock()

    monkeypatch.setattr(_common, "executions_table", mock_exec)
    monkeypatch.setattr(_common, "state_table", mock_state)
    monkeypatch.setattr(_common, "approvals_table", mock_approvals)
    monkeypatch.setattr(_common, "dynamodb", mock_dynamodb)
    monkeypatch.setattr(_pipelines, "executions_table", mock_exec)
    monkeypatch.setattr(_pipelines, "state_table", mock_state)
    monkeypatch.setattr(_pipelines, "approvals_table", mock_approvals)
    if hasattr(_pipelines, "dynamodb"):
        monkeypatch.setattr(_pipelines, "dynamodb", mock_dynamodb)

    # P21 Task 7.2: get_pipeline_executions / get_queued_files now live in
    # handlers.pipelines_runs; mirror the table mocks onto the owning module
    # (module references only — behaviour-preserving).
    from handlers import pipelines_runs as _runs

    monkeypatch.setattr(_runs, "executions_table", mock_exec)
    monkeypatch.setattr(_runs, "state_table", mock_state)
    monkeypatch.setattr(_runs, "approvals_table", mock_approvals)
    monkeypatch.setattr(_runs, "dynamodb", mock_dynamodb)

    _patch_tables.exec_table = mock_exec
    _patch_tables.state_table = mock_state
    _patch_tables.approvals_table = mock_approvals
    _patch_tables.dynamodb = mock_dynamodb
    # Additive (p37 task 6.3): the load_outcome enrichment reads the state table
    # through the same batch_get_item. The table name is read off the handler
    # module rather than hardcoded — it is resolved by
    # require_env("STATE_TABLE", …) at import time, so whichever contract file
    # imported handlers.common first decides the value.
    _patch_tables.state_table_name = _runs.STATE_TABLE_NAME


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExecutionsResponseShape:
    """Response has 'executions' array, 'queued_files' array, and 'pagination' object."""

    def test_top_level_shape(self):
        _patch_tables.exec_table.query.return_value = {"Items": [_make_execution("run-001")]}
        _patch_tables.dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}
        _patch_tables.approvals_table.query.return_value = {"Items": []}

        status, body = _parse(get_pipeline_executions(PIPELINE_ID))
        assert status == 200
        assert "executions" in body
        assert "queued_files" in body
        assert "pagination" in body
        assert isinstance(body["executions"], list)
        assert isinstance(body["queued_files"], list)
        assert isinstance(body["pagination"], dict)

    def test_empty_response(self):
        _patch_tables.exec_table.query.return_value = {"Items": []}
        status, body = _parse(get_pipeline_executions(PIPELINE_ID))
        assert status == 200
        assert body["executions"] == []
        assert body["queued_files"] == []

    def test_queued_items_appear_in_queued_files_not_executions(self):
        """QUEUED items should be in queued_files array, not in executions."""
        queued_item = {
            "pipeline_id": PIPELINE_ID,
            "run_id": "run-queued-001",
            "status": "QUEUED",
            "file_name": "queued.csv",
            "source_key": "s3://bucket/queued.csv",
            "started_at": "2025-01-15T12:00:00Z",
        }
        _patch_tables.exec_table.query.return_value = {"Items": [_make_execution("run-001"), queued_item]}
        _patch_tables.dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}
        _patch_tables.approvals_table.scan.return_value = {"Items": []}

        _, body = _parse(get_pipeline_executions(PIPELINE_ID))
        assert len(body["executions"]) == 1
        assert body["executions"][0]["run_id"] == "run-001"
        assert len(body["queued_files"]) == 1
        assert body["queued_files"][0]["file_name"] == "queued.csv"
        assert body["queued_files"][0]["status"] == "queued"


class TestExecutionFields:
    """Each execution has the critical fields the frontend expects."""

    def test_execution_has_pipeline_id_and_run_id(self):
        _patch_tables.exec_table.query.return_value = {"Items": [_make_execution("run-001")]}
        _patch_tables.dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}
        _patch_tables.approvals_table.query.return_value = {"Items": []}

        _, body = _parse(get_pipeline_executions(PIPELINE_ID))
        exec_item = body["executions"][0]
        assert "pipeline_id" in exec_item
        assert "run_id" in exec_item
        assert "status" in exec_item
        assert "started_at" in exec_item

    def test_steps_status_present_when_provided(self):
        _patch_tables.exec_table.query.return_value = {"Items": [_make_execution("run-001", with_steps_status=True)]}
        _patch_tables.dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}
        _patch_tables.approvals_table.query.return_value = {"Items": []}

        _, body = _parse(get_pipeline_executions(PIPELINE_ID))
        exec_item = body["executions"][0]
        assert "steps_status" in exec_item
        assert isinstance(exec_item["steps_status"], dict)

    def test_pending_approval_id_enriched_for_running_execution(self):
        """When a RUNNING execution has a denormalized pending_approval_id, the handler enriches it."""
        exec_item = _make_execution("run-001", status="RUNNING")
        exec_item["pending_approval_id"] = "appr-enriched-001"
        exec_item["pending_approval_type"] = "schema_mapping"
        _patch_tables.exec_table.query.return_value = {"Items": [exec_item]}

        _, body = _parse(get_pipeline_executions(PIPELINE_ID))
        result = body["executions"][0]
        assert result.get("pending_approval_id") == "appr-enriched-001"
        assert result["status"] == "WAITING_APPROVAL"

    def test_schema_version_enriched_from_denormalized_field(self):
        """Handler enriches executions with schema_version from denormalized schema_hash."""
        exec_item = _make_execution("run-001", status="SUCCEEDED")
        exec_item["schema_hash"] = "abc123"
        _patch_tables.exec_table.query.return_value = {"Items": [exec_item]}
        # Mock the PipelineIndex GSI query for global hash version map
        _patch_tables.state_table.query.return_value = {"Items": [{"schema_hash": "abc123"}]}

        _, body = _parse(get_pipeline_executions(PIPELINE_ID))
        exec_result = body["executions"][0]
        assert "schema_version" in exec_result
        assert isinstance(exec_result["schema_version"], int)


class TestLoadOutcomeValues:
    """p37 R1.7 / R1.9 — which outcome the response carries for a given row.

    Every test here drives the response through the real handler with
    ``Decimal``-seeded rows, so the contract covers the conversion as well as
    the mapping.
    """

    def _executions(self, *run_ids):
        return [_make_execution(rid, status="SUCCEEDED") for rid in run_ids]

    def _respond(self, rows, executions):
        _patch_tables.exec_table.query.return_value = {"Items": executions}
        _patch_tables.dynamodb.batch_get_item.return_value = {
            "Responses": {_patch_tables.state_table_name: rows},
            "UnprocessedKeys": {},
        }
        _patch_tables.state_table.query.return_value = {"Items": []}
        _patch_tables.approvals_table.query.return_value = {"Items": []}
        return _parse(get_pipeline_executions(PIPELINE_ID))

    def test_every_row_on_a_mixed_page_carries_its_own_outcome(self):
        """R1.7 — the enrichment is per-row from a ``run_id``-keyed map, and the
        four established outcomes plus the absent case are seeded together so a
        map lookup that ignored the key, or an enrichment that stopped after the
        first row, cannot pass. Asserted as a whole mapping rather than row by
        row: a partial answer fails on the diff, not on a missing key."""
        executions = self._executions("run-complete", "run-zero", "run-empty", "run-partial", "run-none")
        rows = [
            _load_metrics_row("run-complete", rows_read=100, rows_written=100),
            _load_metrics_row("run-zero", rows_read=5, rows_written=0),
            _load_metrics_row("run-empty", rows_read=0, rows_written=0),
            _load_metrics_row("run-partial", rows_read=1000, rows_written=998),
        ]

        status, body = self._respond(rows, executions)

        assert status == 200
        assert {e["run_id"]: e["load_outcome"]["outcome"] for e in body["executions"]} == {
            "run-complete": "complete_load",
            "run-zero": "no_rows_loaded",
            "run-empty": "empty_source",
            "run-partial": "partial_load",
            "run-none": "not_established",
        }

    def test_no_rows_loaded_carries_the_counts_the_indicator_shows(self):
        """The observed defect's shape — 5 rows read, every one withheld."""
        _, body = self._respond(
            [_load_metrics_row("run-001", rows_read=5, rows_written=0)],
            self._executions("run-001"),
        )

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "no_rows_loaded"
        assert outcome["rows_read"] == 5
        assert outcome["rows_written"] == 0
        assert outcome["rows_not_written"] == 5
        assert outcome["blocks_run"] is False

    def test_partial_load_carries_the_withheld_count(self):
        """R1.9 — the run list shows the success indicator *and* this number, so
        the number has to be in the response for it to be showable."""
        _, body = self._respond(
            [_load_metrics_row("run-001", rows_read=1000, rows_written=998)],
            self._executions("run-001"),
        )

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "partial_load"
        assert outcome["rows_not_written"] == 2

    def test_absence_and_incompleteness_are_different_provenances(self):
        """Both are ``not_established``; the provenance is the only thing that
        tells them apart, and keeping them apart is ``LGR-78ce0b7-8``
        (**template / ratified**) — a record that exists and says nothing must
        not read as no record at all. ``missing`` carries the second half: the
        incomplete row names the count it lacks, the absent one names both."""
        executions = self._executions("run-absent", "run-incomplete")
        rows = [_load_metrics_row("run-incomplete", rows_read=7)]

        _, body = self._respond(rows, executions)

        by_run = {e["run_id"]: e["load_outcome"] for e in body["executions"]}

        assert by_run["run-absent"]["outcome"] == "not_established"
        assert by_run["run-absent"]["determined_from"] == "load_metrics_row_absent"
        assert by_run["run-absent"]["missing"] == ["rows_read", "rows_written"]

        assert by_run["run-incomplete"]["outcome"] == "not_established"
        assert by_run["run-incomplete"]["determined_from"] == "load_metrics_row_incomplete"
        assert by_run["run-incomplete"]["missing"] == ["rows_written"]

        assert by_run["run-absent"]["determined_from"] != by_run["run-incomplete"]["determined_from"]

    def test_a_read_failure_degrades_to_not_established_and_still_returns_200(self):
        """R1.7 with the honesty clause: the annotation may be unavailable, but
        it must not cost the page. Mirrors how ``actual_cost`` degrades to
        ``None`` for every row in the same ``except``."""
        _patch_tables.exec_table.query.return_value = {"Items": self._executions("run-001", "run-002")}
        _patch_tables.state_table.query.return_value = {"Items": []}
        _patch_tables.dynamodb.batch_get_item.side_effect = Exception("DynamoDB unavailable")

        status, body = _parse(get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert len(body["executions"]) == 2
        for execution in body["executions"]:
            assert execution["load_outcome"]["outcome"] == "not_established"
            assert execution["load_outcome"]["determined_from"] == "load_metrics_row_absent"
            assert execution["load_outcome"]["blocks_run"] is False

    def test_queued_files_do_not_gain_a_load_outcome(self):
        """A QUEUED placeholder is not a run that read or wrote anything, and it
        is not in ``executions`` at all — pinned so a future enrichment does not
        start annotating the queue with a verdict about a load that has not
        started."""
        queued = {
            "pipeline_id": PIPELINE_ID,
            "run_id": "run-queued-001",
            "status": "QUEUED",
            "file_name": "queued.csv",
            "source_key": "s3://bucket/queued.csv",
            "started_at": "2025-01-15T12:00:00Z",
        }
        executions = self._executions("run-001")
        _, body = self._respond([_load_metrics_row("run-001", rows_read=3, rows_written=3)], [*executions, queued])

        assert body["executions"][0]["load_outcome"]["outcome"] == "complete_load"
        assert len(body["queued_files"]) == 1
        assert "load_outcome" not in body["queued_files"][0]


class TestPaginationFields:
    """Pagination object has all required fields."""

    def test_pagination_required_fields(self):
        _patch_tables.exec_table.query.return_value = {"Items": []}
        _, body = _parse(get_pipeline_executions(PIPELINE_ID))
        pagination = body["pagination"]
        required = {"page", "page_size", "total_count", "total_pages", "has_next", "has_prev"}
        missing = required - set(pagination.keys())
        assert not missing, f"Pagination missing: {missing}"

    def test_pagination_field_types(self):
        _patch_tables.exec_table.query.return_value = {"Items": []}
        _, body = _parse(get_pipeline_executions(PIPELINE_ID))
        p = body["pagination"]
        assert isinstance(p["page"], int)
        assert isinstance(p["page_size"], int)
        assert isinstance(p["has_next"], bool)
        assert isinstance(p["has_prev"], bool)
