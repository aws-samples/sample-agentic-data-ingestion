# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the DQ_Report_Row lower-bound reader (p37 C5, R1.6, R1.8).

Three things live here, and they are the three halves of C5 that
``tests/unit/shared/test_load_outcome_lower_bound.py`` cannot reach: it owns the
pure deriver, this file owns the reader that feeds it.

**1. ``_load_outcome_lower_bound_from_dq_row`` — what the row has to say before
it is allowed to answer.** It returns ``None`` when the row establishes nothing,
so the caller falls through to the absent arm rather than replacing one
``not_established`` with another that reads as if a source had been consulted.
Every seeded row here carries ``Decimal`` counts, as ``batch_get_item`` and
``query`` both hand them over: ``load_outcome._usable_count`` rejects ``Decimal``
outright, so a reader that skips the conversion reports ``not_established`` for
every historical run while looking perfectly healthy. That is the silent failure
this file exists to catch.

**2. ``_prefer_established`` — which candidate answers.** Recorded counts first,
the lower bound second, absence last. And when *neither* establishes anything,
``load_metrics_row_incomplete`` still beats ``load_metrics_row_absent``: "the row
was there and said nothing" and "there was no row" must not collapse into one
value (``LGR-78ce0b7-8``, **template / ratified**, whose prohibited outcome is "a
run reported as unverifiable when the real problem is a missing record").

**3. Both surfaces agree.** R1.10 requires one Load_Outcome per run, the same on
the run list and the run detail. A retroactive fallback wired into one surface
only would make the two disagree for exactly the historical runs C5 exists to
serve, so the agreement is asserted by calling both entry points over one set of
seeded rows and comparing the whole record.

Neither ``_load_outcome_lower_bound_from_dq_row`` nor ``_prefer_established`` was
named by any test before this file — both were reached only through the two
public readers. A CodeGraph zero-coverage finding under ``lambda/**`` usually
means "unresolved" rather than "untested", but searching ``tests/`` for both
symbol names returned zero hits, so these two survived that check.

Validates: Requirements 1.6, 1.8, 1.10
"""

import contextlib
import importlib
import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — same shape as test_executions_load_outcome.py and
# test_run_details_load_outcome.py, the two files whose surfaces this one joins.
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("DYNAMODB_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("APPROVALS_TABLE_NAME", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")

PIPELINE_ID = "pipe-c5-lower-bound"
RUN_ID = "run-c5-lower-bound-001"
STATE_TABLE_NAME = "agentic_data_ingestion_state"


# ── Row builders, in the shapes the two real producers write ──────────


def _glue_dq_row(run_id: str = RUN_ID, **counts) -> dict:
    """A DQ_Report_Row as the generated Glue job's inline DQ block writes it.

    Field names and the ``glue_inline_dq`` stamp are transcribed from
    ``glue_job_s3_tables.py.j2``'s ``_dq_report`` dict; the counts are ``Decimal``
    because that template serialises through ``parse_float=Decimal`` and DynamoDB
    returns numbers that way regardless. Omit a keyword to model the row not
    carrying that count.
    """
    row = {
        "run_id": run_id,
        "step_name": "data_quality",
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "overall_status": "fail",
        "checks": [{"name": 'Completeness "phone" >= 0.95', "status": "fail"}],
        "source": "glue_inline_dq",
        "quarantine_path": "s3://quarantine-bucket/run-c5/",
        "timestamp": "2025-01-15T12:08:00+00:00",
    }
    row.update({key: Decimal(str(value)) for key, value in counts.items()})
    return row


def _on_demand_agent_dq_row(run_id: str = RUN_ID) -> dict:
    """A DQ_Report_Row as the on-demand data quality agent writes it.

    ``agentcore/data_quality_agent/main.py::generate_report`` returns
    ``overall_status``, ``checks``, ``root_cause`` and ``recommendations`` — and
    **neither** row count. It describes a check, not a load, which is why it must
    establish nothing.
    """
    return {
        "run_id": run_id,
        "step_name": "data_quality",
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "overall_status": "warning",
        "checks": [{"name": "null_rate", "status": "warning", "actual": Decimal("0.12")}],
        "root_cause": "data_issue",
        "recommendations": ["Review failed data quality rules"],
        "agent_type": "data_quality",
        "timestamp": "2025-01-15T12:20:00+00:00",
    }


def _load_metrics_row(run_id: str = RUN_ID, **counts) -> dict:
    """A ``load_metrics`` row (p37 C1) as DynamoDB returns it."""
    row = {
        "run_id": run_id,
        "step_name": "load_metrics",
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "source": "glue_job",
        "write_confirmed": True,
        "timestamp": "2025-01-15T12:09:00+00:00",
    }
    row.update({key: Decimal(str(value)) for key, value in counts.items()})
    return row


def _step_row(step_name: str, *, status: str = "success", sequence: int = 1) -> dict:
    """A genuine Step_Row, so the run has something in its tree."""
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


def _execution(run_id: str = RUN_ID, status: str = "SUCCEEDED") -> dict:
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": status,
        "current_step": "RunPipeline",
        "steps_status": {"file_validation": "completed"},
        "updated_at": "2025-01-15T12:10:30+00:00",
        "started_at": "2025-01-15T12:00:00+00:00",
    }


def _parse(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    """Patch the handler's table objects for BOTH readers in one place.

    The surface-agreement tests call ``get_pipeline_executions`` and
    ``get_run_details`` over one set of rows, so both readers' inputs have to be
    seeded together. Reloading first guards against test-ordering pollution —
    other files in this directory replace ``handlers.*`` in ``sys.modules`` with
    mock-tainted copies — which is the same shape ``test_executions_cost.py`` and
    ``test_executions_load_outcome.py`` use.
    """
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

    mock_state = MagicMock()
    mock_executions = MagicMock()
    mock_approvals = MagicMock()
    mock_pipelines = MagicMock()
    mock_dynamodb = MagicMock()

    mock_state.query.return_value = {"Items": []}
    mock_executions.query.return_value = {"Items": []}
    mock_executions.get_item.return_value = {}
    mock_approvals.query.return_value = {"Items": []}
    mock_pipelines.get_item.return_value = {}
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    for mod in (_common, _runs):
        monkeypatch.setattr(mod, "state_table", mock_state)
        monkeypatch.setattr(mod, "executions_table", mock_executions)
        monkeypatch.setattr(mod, "approvals_table", mock_approvals)
        monkeypatch.setattr(mod, "pipelines_table", mock_pipelines)
        monkeypatch.setattr(mod, "dynamodb", mock_dynamodb)

    # Log fetching is covered elsewhere and would reach CloudWatch.
    monkeypatch.setattr(_runs, "get_step_logs", lambda *a, **kw: [])

    _patch_tables.runs = _runs
    _patch_tables.state_table = mock_state
    _patch_tables.executions_table = mock_executions
    _patch_tables.dynamodb = mock_dynamodb
    yield


def _seed_both_surfaces(rows: list[dict]) -> None:
    """Make one set of state rows visible to both readers.

    The run list reads the records through the existing ``batch_get_item``; the
    run detail already holds every row from its own ``query``. Seeding one list
    into both is what makes the agreement assertion about the *derivation* rather
    than about two different inputs.
    """
    _patch_tables.executions_table.query.return_value = {"Items": [_execution()]}
    _patch_tables.state_table.query.return_value = {"Items": rows}
    _patch_tables.dynamodb.batch_get_item.return_value = {
        "Responses": {STATE_TABLE_NAME: rows},
        "UnprocessedKeys": {},
    }


def _run_list_outcome() -> dict:
    _, body = _parse(_patch_tables.runs.get_pipeline_executions(PIPELINE_ID))
    return body["executions"][0]["load_outcome"]


def _run_detail_outcome() -> dict:
    _, body = _parse(_patch_tables.runs.get_run_details(RUN_ID))
    return body["load_outcome"]


# ═══════════════════════════════════════════════════════════════════════
# 1. The row-level reader
# ═══════════════════════════════════════════════════════════════════════


class TestTheDqRowEstablishesOneThing:
    """``rows_passed == 0`` with ``rows_quarantined > 0``, and nothing else."""

    def test_the_observed_run_shape_yields_no_rows_loaded(self):
        outcome = _patch_tables.runs._load_outcome_lower_bound_from_dq_row(
            _glue_dq_row(rows_passed=0, rows_quarantined=5)
        )

        assert outcome is not None
        assert outcome["outcome"] == "no_rows_loaded"
        assert outcome["determined_from"] == "dq_report_row_lower_bound"
        assert outcome["rows_written"] == 0
        assert outcome["rows_read"] == 5
        assert outcome["rows_not_written"] == 5
        assert outcome["blocks_run"] is False

    def test_the_decimal_conversion_is_what_makes_it_reachable(self):
        """Both counts arrive as ``Decimal`` from DynamoDB and the deriver
        rejects ``Decimal``, so without ``_optional_row_count``'s coercion this
        row establishes nothing while looking entirely healthy. The assertion is
        that a ``Decimal``-carrying row does reach the outcome."""
        row = _glue_dq_row(rows_passed=0, rows_quarantined=5)
        assert isinstance(row["rows_passed"], Decimal)
        assert isinstance(row["rows_quarantined"], Decimal)

        assert _patch_tables.runs._load_outcome_lower_bound_from_dq_row(row)["outcome"] == "no_rows_loaded"

    def test_the_reason_says_the_read_count_is_a_lower_bound(self):
        """R1.6's honesty requirement reaches the reader too: what the surface
        renders has to state that the read count was not recorded."""
        reason = _patch_tables.runs._load_outcome_lower_bound_from_dq_row(
            _glue_dq_row(rows_passed=0, rows_quarantined=5)
        )["reason"].lower()

        assert "at least" in reason
        assert "lower bound" in reason
        assert "not a recorded count" in reason


class TestTheDqRowUsuallyEstablishesNothing:
    """``None`` means "fall through to the absent arm", so each of these is a
    row that must NOT be allowed to answer."""

    @pytest.mark.parametrize(
        ("label", "counts"),
        [
            ("rows passed, so Rows_Read is unknown", {"rows_passed": 3, "rows_quarantined": 2}),
            ("nothing quarantined, so nothing forces Rows_Read > 0", {"rows_passed": 0, "rows_quarantined": 0}),
            ("more passed than quarantined", {"rows_passed": 9, "rows_quarantined": 5}),
            ("equal counts — R1.5's arm arithmetically", {"rows_passed": 5, "rows_quarantined": 5}),
            ("negative quarantine count", {"rows_passed": 0, "rows_quarantined": -1}),
            ("negative rows_passed", {"rows_passed": -1, "rows_quarantined": 5}),
            ("no rows_quarantined attribute", {"rows_passed": 0}),
            ("no rows_passed attribute", {"rows_quarantined": 5}),
            ("neither count", {}),
        ],
    )
    def test_a_row_that_settles_nothing_returns_none(self, label, counts):
        assert _patch_tables.runs._load_outcome_lower_bound_from_dq_row(_glue_dq_row(**counts)) is None

    def test_an_on_demand_agent_row_establishes_nothing(self):
        """It carries ``overall_status`` and ``checks`` and neither count,
        because it describes a check rather than a load. A row that overwrote a
        Glue DQ report — the agent uses ``put_item`` — must not start answering a
        question about what was written."""
        row = _on_demand_agent_dq_row()
        assert "rows_passed" not in row
        assert "rows_quarantined" not in row

        assert _patch_tables.runs._load_outcome_lower_bound_from_dq_row(row) is None

    def test_a_boolean_in_a_count_field_is_not_read_as_a_count(self):
        row = _glue_dq_row(rows_quarantined=5)
        row["rows_passed"] = True

        assert _patch_tables.runs._load_outcome_lower_bound_from_dq_row(row) is None

    def test_it_never_returns_complete_load(self):
        """The reader's own pre-filter and the deriver's structural guard both
        forbid it; this asserts the pair, over every small-integer combination
        plus the absent shapes."""
        values = [*range(-3, 12), None]
        for passed in values:
            for quarantined in values:
                counts = {}
                if passed is not None:
                    counts["rows_passed"] = passed
                if quarantined is not None:
                    counts["rows_quarantined"] = quarantined
                outcome = _patch_tables.runs._load_outcome_lower_bound_from_dq_row(_glue_dq_row(**counts))
                assert outcome is None or outcome["outcome"] == "no_rows_loaded", (
                    f"rows_passed={passed!r}, rows_quarantined={quarantined!r} produced {outcome}"
                )


# ═══════════════════════════════════════════════════════════════════════
# 2. Which candidate answers
# ═══════════════════════════════════════════════════════════════════════


class TestPreferEstablished:
    """Recorded counts first, the lower bound second, absence last."""

    def test_a_recorded_row_wins_over_the_lower_bound(self):
        """The ``load_metrics`` row is the recorded source. The lower bound
        exists for runs that have no such row, so it must never override one."""
        recorded = _patch_tables.runs._load_outcome_from_row(_load_metrics_row(rows_read=100, rows_written=100))
        lower_bound = _patch_tables.runs._load_outcome_lower_bound_from_dq_row(
            _glue_dq_row(rows_passed=0, rows_quarantined=5)
        )

        chosen = _patch_tables.runs._prefer_established(recorded, lower_bound)

        assert chosen == recorded
        assert chosen["determined_from"] == "load_metrics_row"

    def test_a_recorded_no_rows_loaded_still_wins(self):
        """Same verdict from two sources: the recorded one is chosen because its
        ``rows_read`` is a count rather than a bound."""
        recorded = _patch_tables.runs._load_outcome_from_row(_load_metrics_row(rows_read=42, rows_written=0))
        lower_bound = _patch_tables.runs._load_outcome_lower_bound_from_dq_row(
            _glue_dq_row(rows_passed=0, rows_quarantined=5)
        )

        chosen = _patch_tables.runs._prefer_established(recorded, lower_bound)

        assert chosen["outcome"] == lower_bound["outcome"] == "no_rows_loaded"
        assert chosen["determined_from"] == "load_metrics_row"
        assert chosen["rows_read"] == 42

    def test_the_lower_bound_answers_when_there_is_no_recorded_row(self):
        """The whole point of C5: a run that completed before the counts row
        existed still gets its outcome."""
        lower_bound = _patch_tables.runs._load_outcome_lower_bound_from_dq_row(
            _glue_dq_row(rows_passed=0, rows_quarantined=5)
        )

        chosen = _patch_tables.runs._prefer_established(None, lower_bound)

        assert chosen["outcome"] == "no_rows_loaded"
        assert chosen["determined_from"] == "dq_report_row_lower_bound"

    def test_the_lower_bound_answers_for_an_incomplete_recorded_row(self):
        """A row that exists and carries no usable counts establishes nothing,
        so it does not get to block an answer that does."""
        incomplete = _patch_tables.runs._load_outcome_from_row(_load_metrics_row(rows_read=7))
        lower_bound = _patch_tables.runs._load_outcome_lower_bound_from_dq_row(
            _glue_dq_row(rows_passed=0, rows_quarantined=5)
        )
        assert incomplete["outcome"] == "not_established"

        chosen = _patch_tables.runs._prefer_established(incomplete, lower_bound)

        assert chosen["outcome"] == "no_rows_loaded"
        assert chosen["determined_from"] == "dq_report_row_lower_bound"

    def test_incomplete_is_preferred_over_absent_when_neither_establishes(self):
        """``LGR-78ce0b7-8`` (**template / ratified**): "the row was there and
        said nothing" and "there was no row" must not collapse into one value.
        Both are ``not_established``, so the distinction survives only in
        ``determined_from``."""
        incomplete = _patch_tables.runs._load_outcome_from_row(_load_metrics_row(rows_read=7))
        absent = _patch_tables.runs._load_outcome_absent()

        chosen = _patch_tables.runs._prefer_established(incomplete, absent)

        assert chosen["outcome"] == "not_established"
        assert chosen["determined_from"] == "load_metrics_row_incomplete"

    def test_an_incomplete_row_survives_a_dq_row_that_settles_nothing(self):
        """The real pairing on a run with both rows and no usable counts: the
        reader passes ``None`` for the lower bound, and the incomplete provenance
        must reach the surface rather than being flattened to absent."""
        incomplete = _patch_tables.runs._load_outcome_from_row(_load_metrics_row(rows_read=7))

        chosen = _patch_tables.runs._prefer_established(incomplete, None)

        assert chosen["determined_from"] == "load_metrics_row_incomplete"

    def test_no_candidate_at_all_is_absent(self):
        """A run with neither row: honest, and a reachable steady state rather
        than a legacy one."""
        chosen = _patch_tables.runs._prefer_established(None, None)

        assert chosen["outcome"] == "not_established"
        assert chosen["determined_from"] == "load_metrics_row_absent"
        assert chosen["rows_read"] is None
        assert chosen["rows_written"] is None


# ═══════════════════════════════════════════════════════════════════════
# 3. One Load_Outcome per run, on both surfaces (R1.10)
# ═══════════════════════════════════════════════════════════════════════


class TestBothSurfacesAgree:
    """R1.10 — the same value on the run list and the run detail.

    A fallback wired into one surface only would disagree for exactly the
    historical runs C5 serves, so each case below derives from both entry points
    over one set of rows and compares the **whole** record, not just the outcome
    name.
    """

    def test_a_lower_bound_run_reads_the_same_on_both(self):
        rows = [_step_row("run_pipeline", sequence=10), _glue_dq_row(rows_passed=0, rows_quarantined=5)]
        _seed_both_surfaces(rows)

        assert _run_list_outcome() == _run_detail_outcome()

    def test_and_that_shared_value_is_the_established_lower_bound(self):
        """Stated separately so agreement on a wrong answer cannot pass: both
        surfaces have to agree *and* the answer has to be the outcome C5
        establishes."""
        rows = [_step_row("run_pipeline", sequence=10), _glue_dq_row(rows_passed=0, rows_quarantined=5)]
        _seed_both_surfaces(rows)

        for outcome in (_run_list_outcome(), _run_detail_outcome()):
            assert outcome["outcome"] == "no_rows_loaded"
            assert outcome["determined_from"] == "dq_report_row_lower_bound"
            assert outcome["rows_read"] == 5
            assert outcome["rows_written"] == 0

    def test_a_recorded_run_reads_the_same_on_both_and_prefers_the_counts_row(self):
        """With both rows present, both surfaces have to make the same choice
        between them — the preference has to live in shared code, not in one
        reader."""
        rows = [
            _step_row("run_pipeline", sequence=10),
            _load_metrics_row(rows_read=100, rows_written=100),
            _glue_dq_row(rows_passed=0, rows_quarantined=5),
        ]
        _seed_both_surfaces(rows)

        list_outcome, detail_outcome = _run_list_outcome(), _run_detail_outcome()

        assert list_outcome == detail_outcome
        assert list_outcome["outcome"] == "complete_load"
        assert list_outcome["determined_from"] == "load_metrics_row"

    def test_an_on_demand_agent_row_leaves_both_surfaces_not_established(self):
        """The agent's row describes a check, not a load, so neither surface may
        derive anything from it — and both must fall through to absence."""
        rows = [_step_row("run_pipeline", sequence=10), _on_demand_agent_dq_row()]
        _seed_both_surfaces(rows)

        list_outcome, detail_outcome = _run_list_outcome(), _run_detail_outcome()

        assert list_outcome == detail_outcome
        assert list_outcome["outcome"] == "not_established"
        assert list_outcome["determined_from"] == "load_metrics_row_absent"

    def test_a_run_with_neither_row_reads_the_same_on_both(self):
        rows = [_step_row("run_pipeline", sequence=10)]
        _seed_both_surfaces(rows)

        assert _run_list_outcome() == _run_detail_outcome()
        assert _run_list_outcome()["determined_from"] == "load_metrics_row_absent"

    def test_a_dq_row_that_settles_nothing_reads_the_same_on_both(self):
        """The common historical case: an inline-DQ run that quarantined some
        rows and wrote the rest. Neither surface may invent an outcome from it."""
        rows = [_step_row("run_pipeline", sequence=10), _glue_dq_row(rows_passed=95, rows_quarantined=5)]
        _seed_both_surfaces(rows)

        list_outcome, detail_outcome = _run_list_outcome(), _run_detail_outcome()

        assert list_outcome == detail_outcome
        assert list_outcome["outcome"] == "not_established"

    def test_the_dq_row_is_still_not_a_step_on_the_run_detail(self):
        """R4.4, asserted alongside the derivation because C5 made the run
        detail *read* this row: reading it must not put it back in the tree."""
        rows = [_step_row("run_pipeline", sequence=10), _glue_dq_row(rows_passed=0, rows_quarantined=5)]
        _seed_both_surfaces(rows)

        _, body = _parse(_patch_tables.runs.get_run_details(RUN_ID))

        assert [s["step_name"] for s in body["steps"]] == ["run_pipeline"]
        assert body["load_outcome"]["outcome"] == "no_rows_loaded"

    def test_a_lower_bound_run_still_reads_succeeded(self):
        """The Load_Outcome describes the run; it does not re-derive its status
        (R1.12, do-NOT 4). The zero-row load shows up in the indicator, not by
        editing the Run_Status chain."""
        rows = [_step_row("run_pipeline", sequence=10), _glue_dq_row(rows_passed=0, rows_quarantined=5)]
        _seed_both_surfaces(rows)

        _, body = _parse(_patch_tables.runs.get_run_details(RUN_ID))

        assert body["status"] == "SUCCEEDED"
        assert body["load_outcome"]["outcome"] == "no_rows_loaded"
        assert body["load_outcome"]["blocks_run"] is False
