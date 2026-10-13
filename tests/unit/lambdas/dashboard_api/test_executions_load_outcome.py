# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for per-run ``load_outcome`` enrichment in get_pipeline_executions.

p37 C3. The interesting failure this file exists to catch is silent: DynamoDB
returns the counts row's ints as ``Decimal``, and ``load_outcome._usable_count``
rejects ``Decimal`` outright, so a wiring that forgets to convert produces
``not_established`` for *every* run while looking perfectly healthy. Every seeded
row below therefore carries ``Decimal`` counts, exactly as ``batch_get_item``
hands them over, and asserts a real outcome comes back.

Also pinned here: that the counts row rides the *existing* ``batch_get_item``
rather than a new operation (``LGR-7c18e81-1``, **backfilled**), and that a read
failure degrades to ``not_established`` rather than 500ing.

Validates: Requirements 1.7, 1.9, 1.10
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — same shape as test_executions_cost.py, which covers the sibling
# enrichment in the same function.
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
os.environ.setdefault("AWS_REGION", "us-west-2")

PIPELINE_ID = "pipe-load-outcome-001"
STATE_TABLE_NAME = "agentic_data_ingestion_state"


def _parse(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


def _make_execution(run_id: str, status: str = "SUCCEEDED") -> dict:
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": status,
        "current_step": "RunPipeline",
        "steps_status": {"file_validation": "completed"},
        "updated_at": "2025-01-15T12:05:30+00:00",
        "started_at": "2025-01-15T12:00:00+00:00",
    }


def _load_metrics_row(run_id: str, **counts) -> dict:
    """A ``load_metrics`` row as ``batch_get_item`` returns it.

    Counts are ``Decimal`` because that is what DynamoDB gives back for a
    number attribute — the whole point of the conversion under test. Omit a
    keyword to model the row not carrying that count.
    """
    row = {"run_id": run_id, "step_name": "load_metrics"}
    row.update({key: Decimal(str(value)) for key, value in counts.items()})
    return row


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    import contextlib
    import importlib

    # Guard against test-ordering pollution, same shape as test_executions_cost.py:
    # other files replace handlers.* in sys.modules with mock-tainted copies, so
    # force a fresh import. Each reload is guarded because a missing namespace
    # parent (a test-ordering artefact) must not error the fixture.
    #
    # The reload re-reads STATE_TABLE, and the responses below are keyed by
    # STATE_TABLE_NAME. The module-scope setdefault above is a no-op once an
    # earlier file has set STATE_TABLE to its own name, so pin it for the reload.
    # This used to pass only because a leaked env_utils MagicMock ignored the
    # env var and returned the default (LGR-c58f276-4).
    monkeypatch.setenv("STATE_TABLE", STATE_TABLE_NAME)
    for _name in ("handlers.common", "handlers.pipelines_runs", "handlers.pipelines"):
        _mod = sys.modules.get(_name)
        if _mod is not None and "handlers" in sys.modules:
            with contextlib.suppress(ImportError):
                importlib.reload(_mod)

    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    mock_exec = MagicMock()
    mock_state = MagicMock()
    mock_dynamodb = MagicMock()

    monkeypatch.setattr(_common, "executions_table", mock_exec)
    monkeypatch.setattr(_common, "state_table", mock_state)
    monkeypatch.setattr(_common, "dynamodb", mock_dynamodb)
    monkeypatch.setattr(_runs, "executions_table", mock_exec)
    monkeypatch.setattr(_runs, "state_table", mock_state)
    monkeypatch.setattr(_runs, "dynamodb", mock_dynamodb)
    mock_state.query.return_value = {"Items": []}
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    _patch_tables.exec_table = mock_exec
    _patch_tables.state_table = mock_state
    _patch_tables.dynamodb = mock_dynamodb
    _patch_tables.get_pipeline_executions = _runs.get_pipeline_executions


def _run_with(rows: list[dict], executions: list[dict] | None = None) -> tuple:
    _patch_tables.exec_table.query.return_value = {"Items": executions or [_make_execution("run-001")]}
    _patch_tables.dynamodb.batch_get_item.return_value = {
        "Responses": {STATE_TABLE_NAME: rows},
        "UnprocessedKeys": {},
    }
    return _parse(_patch_tables.get_pipeline_executions(PIPELINE_ID))


class TestSeededRowYieldsARealOutcome:
    """A row that is there must not read as ``not_established`` (R1.7, R1.9)."""

    def test_equal_counts_yield_complete_load(self):
        """The Decimal→int conversion: without it this returns not_established."""
        _, body = _run_with([_load_metrics_row("run-001", rows_read=100, rows_written=100)])

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "complete_load"
        assert outcome["determined_from"] == "load_metrics_row"
        assert outcome["rows_read"] == 100
        assert outcome["rows_written"] == 100
        assert outcome["missing"] == []

    def test_zero_written_from_a_non_empty_source_is_no_rows_loaded(self):
        """The observed defect's shape: 5 rows read, every one quarantined."""
        _, body = _run_with([_load_metrics_row("run-001", rows_read=5, rows_written=0)])

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "no_rows_loaded"
        assert outcome["rows_not_written"] == 5

    def test_zero_read_and_zero_written_is_empty_source(self):
        """A real 0 must survive the conversion as 0, not as absence (R1.3)."""
        _, body = _run_with([_load_metrics_row("run-001", rows_read=0, rows_written=0)])

        assert body["executions"][0]["load_outcome"]["outcome"] == "empty_source"

    def test_partial_load_carries_the_withheld_count(self):
        """R1.9 — the count the run list shows beside the success indicator."""
        _, body = _run_with([_load_metrics_row("run-001", rows_read=1000, rows_written=998)])

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "partial_load"
        assert outcome["rows_not_written"] == 2

    def test_each_run_gets_its_own_outcome(self):
        executions = [_make_execution("run-001"), _make_execution("run-002"), _make_execution("run-003")]
        _, body = _run_with(
            [
                _load_metrics_row("run-001", rows_read=10, rows_written=10),
                _load_metrics_row("run-003", rows_read=10, rows_written=0),
            ],
            executions=executions,
        )

        outcomes = {e["run_id"]: e["load_outcome"]["outcome"] for e in body["executions"]}
        assert outcomes == {
            "run-001": "complete_load",
            "run-003": "no_rows_loaded",
            "run-002": "not_established",  # no row of its own
        }


class TestNotEstablished:
    """Absence and incompleteness stay distinguishable (R1.6)."""

    def test_no_load_metrics_row_is_absent_provenance(self):
        _, body = _run_with([])

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "not_established"
        assert outcome["determined_from"] == "load_metrics_row_absent"
        assert outcome["rows_read"] is None
        assert outcome["rows_written"] is None

    def test_row_present_without_rows_written_is_incomplete_provenance(self):
        _, body = _run_with([_load_metrics_row("run-001", rows_read=7)])

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "not_established"
        assert outcome["determined_from"] == "load_metrics_row_incomplete"
        assert outcome["missing"] == ["rows_written"]

    def test_a_boolean_in_a_count_field_is_not_read_as_a_count(self):
        row = {"run_id": "run-001", "step_name": "load_metrics", "rows_read": True, "rows_written": Decimal("0")}
        _, body = _run_with([row])

        outcome = body["executions"][0]["load_outcome"]
        assert outcome["outcome"] == "not_established"
        assert outcome["missing"] == ["rows_read"]


class TestOneOperationAndNoRegression:
    """The counts ride the existing read (LGR-7c18e81-1) and cost still works."""

    def test_counts_key_is_added_to_the_existing_batch_get(self):
        executions = [_make_execution(f"run-{i:03d}") for i in range(1, 4)]
        _run_with([], executions=executions)

        assert _patch_tables.dynamodb.batch_get_item.call_count == 1
        keys = _patch_tables.dynamodb.batch_get_item.call_args.kwargs["RequestItems"][STATE_TABLE_NAME]["Keys"]
        # 3 runs × (run_pipeline, RunPipeline, load_metrics, data_quality).
        # The fourth key is p37 C5's: the DQ_Report_Row supplies the retroactive
        # lower bound for runs written before the counts row existed. Still ONE
        # batch_get_item — a key was added, not an operation (LGR-7c18e81-1).
        assert len(keys) == 12
        assert sum(1 for k in keys if k["step_name"] == "load_metrics") == 3
        assert sum(1 for k in keys if k["step_name"] == "data_quality") == 3

    def test_actual_cost_and_load_outcome_are_both_enriched(self):
        _, body = _run_with(
            [
                {
                    "run_id": "run-001",
                    "step_name": "run_pipeline",
                    "output_data": {"actual_cost": Decimal("1.234")},
                },
                _load_metrics_row("run-001", rows_read=42, rows_written=42),
            ]
        )

        execution = body["executions"][0]
        assert execution["actual_cost"] == 1.234
        assert execution["load_outcome"]["outcome"] == "complete_load"


class TestReadFailureDegrades:
    """A failed read is not_established for every row, never a 500."""

    def test_batch_get_item_failure_yields_not_established_not_a_500(self):
        _patch_tables.exec_table.query.return_value = {
            "Items": [_make_execution("run-001"), _make_execution("run-002")]
        }
        _patch_tables.dynamodb.batch_get_item.side_effect = Exception("DynamoDB error")

        status, body = _parse(_patch_tables.get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert len(body["executions"]) == 2
        for execution in body["executions"]:
            assert execution["actual_cost"] is None
            assert execution["load_outcome"]["outcome"] == "not_established"
            assert execution["load_outcome"]["determined_from"] == "load_metrics_row_absent"
