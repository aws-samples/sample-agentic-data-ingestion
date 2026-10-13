# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``load_outcome`` and the synthetic-step filter in get_run_details.

p37 C4. Two things are pinned here and they are the two halves of one change.

**The phantom step (R4.4, and the live regression this task closes).** The
generated Glue job writes a ``(run_id, "load_metrics")`` row (p37 C1). It is a
record, not a Step_Row — the same category as ``data_quality`` — so it must not
appear in the run's step tree. Before ``load_metrics`` was added to
``_synthetic_steps`` it did appear, rendering as a trailing ⚙️ with an unknown
status: the mirror image of the ``data_quality`` defect Requirement 4 exists to
remove. ``test_load_metrics_row_is_not_a_step`` fails without that one key.
``data_quality`` is asserted alongside it rather than assumed, so R4.4's existing
filter is tested rather than trusted.

**The counts survive the filter (R1.8, R1.10).** ``load_outcome`` is derived
*before* the filter runs, from the same row the filter then drops, through the
same helpers the run list uses. Every seeded row carries ``Decimal`` counts, as
DynamoDB hands them over: ``load_outcome._usable_count`` rejects ``Decimal``, so a
reader that skips the conversion returns ``not_established`` for every run while
looking healthy.

And one thing pinned as *unchanged*: a ``load_metrics`` row does not move the
run's Run_Status. R1.10 warns against a second independently-derived answer to
one question; the Load_Outcome is not allowed to join Run_Status's existing
disagreement with ``execution_updater._derive_execution_status``.

Validates: Requirements 1.8, 1.10, 4.4
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — same shape as test_run_details_approval_parsing.py, which covers
# a sibling concern in the same function.
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
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

RUN_ID = "run-load-outcome-detail-001"
PIPELINE_ID = "pipe-load-outcome-detail"


def _parse(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


def _step_row(step_name: str, *, status: str = "success", sequence: int = 1) -> dict:
    """A genuine Step_Row, the kind that belongs in the tree."""
    return {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "execution_id": "exec-001",
        "status": status,
        "timestamp": f"2025-01-15T12:{sequence:02d}:00+00:00",
        "step_sequence": sequence,
    }


def _load_metrics_row(**counts) -> dict:
    """A ``load_metrics`` row as ``state_table.query`` returns it.

    Counts are ``Decimal`` because that is what DynamoDB gives back for a number
    attribute. Omit a keyword to model the row not carrying that count. No
    ``status`` attribute, matching the real writer — it is a record, not a step.
    """
    row = {
        "run_id": RUN_ID,
        "step_name": "load_metrics",
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "source": "glue_job",
        "timestamp": "2025-01-15T12:09:00+00:00",
    }
    row.update({key: Decimal(str(value)) for key, value in counts.items()})
    return row


def _dq_report_row() -> dict:
    """A DQ_Report_Row: ``overall_status`` and no ``status`` (R4.4's subject)."""
    return {
        "run_id": RUN_ID,
        "step_name": "data_quality",
        "pipeline_id": PIPELINE_ID,
        "overall_status": "PASSED",
        "source": "glue_inline_dq",
        "timestamp": "2025-01-15T12:08:00+00:00",
    }


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    mock_state = MagicMock()
    mock_approvals = MagicMock()
    mock_executions = MagicMock()
    mock_pipelines = MagicMock()
    mock_dynamodb = MagicMock()

    mock_state.query.return_value = {"Items": []}
    mock_approvals.query.return_value = {"Items": []}
    mock_executions.query.return_value = {"Items": []}
    mock_executions.get_item.return_value = {}
    mock_pipelines.get_item.return_value = {}
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    for mod in (_common, _runs):
        monkeypatch.setattr(mod, "state_table", mock_state)
        monkeypatch.setattr(mod, "approvals_table", mock_approvals)
        monkeypatch.setattr(mod, "executions_table", mock_executions)
        monkeypatch.setattr(mod, "pipelines_table", mock_pipelines)
        monkeypatch.setattr(mod, "dynamodb", mock_dynamodb)

    # Log fetching is covered elsewhere and would reach CloudWatch.
    monkeypatch.setattr(_runs, "get_step_logs", lambda *a, **kw: [])

    _patch_tables.state_table = mock_state
    _patch_tables.get_run_details = _runs.get_run_details
    yield


def _run_with(items: list[dict]) -> tuple:
    _patch_tables.state_table.query.return_value = {"Items": items}
    return _parse(_patch_tables.get_run_details(RUN_ID))


class TestNonStepRowsAreNotSteps:
    """R4.4 — a record on the run's partition is not a step in the tree."""

    def test_load_metrics_row_is_not_a_step(self):
        """The live regression: without ``load_metrics`` in ``_synthetic_steps``
        this row renders as a trailing step with an unknown status."""
        status, body = _run_with(
            [
                _step_row("schema_extract", sequence=1),
                _step_row("run_pipeline", sequence=10),
                _load_metrics_row(rows_read=5, rows_written=0),
            ]
        )

        assert status == 200
        step_names = [s["step_name"] for s in body["steps"]]
        assert "load_metrics" not in step_names
        assert step_names == ["schema_extract", "run_pipeline"]
        assert body["total_steps"] == 2

    def test_data_quality_row_is_still_not_a_step(self):
        """R4.4's pre-existing key, asserted rather than assumed."""
        status, body = _run_with([_step_row("run_pipeline", sequence=10), _dq_report_row()])

        assert status == 200
        assert "data_quality" not in [s["step_name"] for s in body["steps"]]

    def test_both_records_are_filtered_in_one_run(self):
        """The two record types coexist on a real run's partition."""
        _, body = _run_with(
            [
                _step_row("run_pipeline", sequence=10),
                _dq_report_row(),
                _load_metrics_row(rows_read=5, rows_written=0),
            ]
        )

        assert [s["step_name"] for s in body["steps"]] == ["run_pipeline"]

    def test_a_genuine_step_row_is_unaffected(self):
        """The filter is a denylist of records, not a change to step handling."""
        _, body = _run_with(
            [_step_row(name, sequence=i + 1) for i, name in enumerate(("schema_extract", "create_code"))]
        )

        assert [s["step_name"] for s in body["steps"]] == ["schema_extract", "create_code"]


class TestLoadOutcomeIsReadBeforeTheFilter:
    """R1.8 — the counts are read off the row the filter then drops."""

    def test_the_row_is_filtered_and_its_counts_still_arrive(self):
        """Both halves at once: this is what "before the filter" means."""
        _, body = _run_with(
            [
                _step_row("run_pipeline", sequence=10),
                _load_metrics_row(rows_read=5, rows_written=0),
            ]
        )

        assert "load_metrics" not in [s["step_name"] for s in body["steps"]]
        assert body["load_outcome"]["outcome"] == "no_rows_loaded"

    def test_equal_counts_yield_complete_load(self):
        """The Decimal→int conversion: without it this reads not_established."""
        _, body = _run_with(
            [_step_row("run_pipeline", sequence=10), _load_metrics_row(rows_read=100, rows_written=100)]
        )

        outcome = body["load_outcome"]
        assert outcome["outcome"] == "complete_load"
        assert outcome["determined_from"] == "load_metrics_row"
        assert outcome["rows_read"] == 100
        assert outcome["rows_written"] == 100
        assert outcome["missing"] == []

    def test_the_observed_run_shape_states_both_counts(self):
        """R1.8 — 5 rows read, none written, and the withheld count is carried."""
        _, body = _run_with([_step_row("run_pipeline", sequence=10), _load_metrics_row(rows_read=5, rows_written=0)])

        outcome = body["load_outcome"]
        assert outcome["outcome"] == "no_rows_loaded"
        assert outcome["rows_read"] == 5
        assert outcome["rows_written"] == 0
        assert outcome["rows_not_written"] == 5
        assert outcome["blocks_run"] is False

    def test_zero_read_and_zero_written_is_empty_source(self):
        """A real 0 survives the conversion as 0, not as absence (R1.3)."""
        _, body = _run_with([_step_row("run_pipeline", sequence=10), _load_metrics_row(rows_read=0, rows_written=0)])

        assert body["load_outcome"]["outcome"] == "empty_source"

    def test_partial_load_carries_the_withheld_count(self):
        _, body = _run_with(
            [_step_row("run_pipeline", sequence=10), _load_metrics_row(rows_read=1000, rows_written=998)]
        )

        outcome = body["load_outcome"]
        assert outcome["outcome"] == "partial_load"
        assert outcome["rows_not_written"] == 2


class TestNotEstablished:
    """R1.6 — absence and incompleteness stay distinguishable."""

    def test_no_load_metrics_row_is_absent_provenance(self):
        _, body = _run_with([_step_row("run_pipeline", sequence=10)])

        outcome = body["load_outcome"]
        assert outcome["outcome"] == "not_established"
        assert outcome["determined_from"] == "load_metrics_row_absent"
        assert outcome["rows_read"] is None
        assert outcome["rows_written"] is None

    def test_row_present_without_rows_written_is_incomplete_provenance(self):
        _, body = _run_with([_step_row("run_pipeline", sequence=10), _load_metrics_row(rows_read=7)])

        outcome = body["load_outcome"]
        assert outcome["outcome"] == "not_established"
        assert outcome["determined_from"] == "load_metrics_row_incomplete"
        assert outcome["missing"] == ["rows_written"]

    def test_a_boolean_in_a_count_field_is_not_read_as_a_count(self):
        row = _load_metrics_row(rows_written=0)
        row["rows_read"] = True
        _, body = _run_with([_step_row("run_pipeline", sequence=10), row])

        outcome = body["load_outcome"]
        assert outcome["outcome"] == "not_established"
        assert outcome["missing"] == ["rows_read"]


class TestRunStatusIsUnchanged:
    """R1.10 — the Load_Outcome describes the run; it does not re-derive it."""

    def test_a_no_rows_loaded_run_still_reads_succeeded(self):
        """The eleven-branch Run_Status chain is untouched by this change: a
        successful ``run_pipeline`` row still yields SUCCEEDED. Whether that is
        the *right* status for a zero-row load is R1.7/R1.8's presentational
        question, answered by the indicator — not by editing this chain."""
        _, body = _run_with([_step_row("run_pipeline", sequence=10), _load_metrics_row(rows_read=5, rows_written=0)])

        assert body["status"] == "SUCCEEDED"
        assert body["load_outcome"]["outcome"] == "no_rows_loaded"

    def test_the_counts_row_does_not_make_a_finished_run_look_unfinished(self):
        """The row carries no ``status``, so it must not contribute one."""
        without = _run_with([_step_row("run_pipeline", sequence=10)])[1]["status"]
        with_row = _run_with([_step_row("run_pipeline", sequence=10), _load_metrics_row(rows_read=5, rows_written=5)])[
            1
        ]["status"]

        assert without == with_row == "SUCCEEDED"
