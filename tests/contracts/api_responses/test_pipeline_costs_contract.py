# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /api/pipelines/{id}/costs response shape (p30 R3.5).

``get_pipeline_costs`` aggregates ``output_data.actual_cost`` off the
``run_pipeline`` state rows and returns a Cost_Summary. Nothing in that response
is validated anywhere between the handler and the React component that renders
it: ``apiClient.get<PipelineCosts>(...)`` is a cast, not a parse. So a producer
that drops a key, renames one, or narrows ``most_expensive_run`` from
``null`` to absent type-checks on both sides and fails at runtime — or, worse,
does not fail at all and renders a wrong number.

**Pinned against the frontend type, not against the handler.** The expected
field sets and types below are PARSED at test time out of::

    dashboard-react/src/hooks/use-pipeline-costs.ts

(``PipelineCosts`` / ``PipelineCostRun``), which is the type
``usePipelineCosts`` instantiates ``apiClient.get<…>`` with. This is the ratified
pattern from ``LGR-28b4d75-5``: a contract test written from the producer's
current output cannot detect the producer drifting, because that output is its
own oracle. Parsed this way, adding a required field to the TypeScript interface
fails this test until the handler supplies it, and deleting one from the handler
fails it immediately.

The parser lives in ``helpers.py``. It grew a nullable branch for this file:
``most_expensive_run: PipelineCostRun | null`` is the true wire shape and the
single-file reader raised ``Unmapped TypeScript type`` on it. The union is the
thing being pinned, so it was the reader that had to change.

Three properties get their own tests because each is a place a naive contract
test pins a falsehood:

* **``most_expensive_run`` null versus absent.** The interface declares it
  ``PipelineCostRun | null`` — not ``most_expensive_run?:`` — so the key is
  always present and the absence of a value is spelled ``null``. That
  distinction is load-bearing for the natural narrowing: a consumer writing
  ``if (data.most_expensive_run !== null) { …actual_cost }`` is exactly what the
  declared type invites, and an *absent* key makes the value ``undefined``,
  which passes ``!== null`` and then throws on property access. The current sole
  consumer (``PipelineCostPanel``) happens to survive either way because it uses
  ``?.`` and ``formatUsd`` renders nullish as ``—``, so this failure mode would
  ship silently today and surface on the second consumer.

* **``run_count`` is neither ``len(cost_trend)`` nor the pipeline's run count.**
  It counts runs that carried an ``actual_cost``. It exceeds
  ``len(cost_trend)`` above seven runs, and it is *below* the number of rows the
  query returned whenever a run is still in flight. A test asserting either
  equality would pin a falsehood, and ``PipelineCostPanel``'s footer branches on
  ``run_count > cost_trend.length`` precisely because they diverge.

* **``timestamp`` can be the empty string.** The handler defaults it
  (``item.get("timestamp", "")``) rather than dropping the run, and the
  interface declares ``string`` — not ``string | null``. The run must still
  appear and still count; the panel renders ``—`` for it.

Deliberately NOT covered here, to say where it already lives rather than
triplicate it: the aggregation arithmetic and the 7-entry newest-first trimming
are ``tests/unit/lambdas/dashboard_api/test_costs_handler.py``; the three async
states, currency formatting, trend order on screen and the scope note are
``dashboard-react/src/components/__tests__/PipelineCostPanel.async.test.tsx``.
This file pins only the **wire format**.

Validates: Requirements 3.5
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — handlers/common.py resolves its table names at import time
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parents[3]
LAMBDA_DIR = REPO_DIR / "lambda" / "agentic_data_ingestion_dashboard_api"
SHARED_DIR = REPO_DIR / "shared"
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
# handlers/common.py calls require_env for each table at import time, before any
# fixture can run — mirrors test_cost_recommendations_contract.py. The real table
# handle is replaced by a mock in the fixtures below; nothing here touches AWS.
for _table_env, _table_name in (
    ("STATE_TABLE", "agentic_data_ingestion_state"),
    ("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"),
    ("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"),
    ("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code"),
    ("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results"),
    ("APPROVALS_TABLE", "agentic_data_ingestion_approvals"),
    ("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"),
    ("AUDIT_TABLE", "agentic_data_ingestion_audit_log"),
    ("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces"),
):
    os.environ.setdefault(_table_env, _table_name)

sys.modules.pop("lambda_function", None)

from handlers import costs as _costs

from .helpers import declares_null, load_interfaces, matches_json_type, required_fields

#: The consumer this contract is pinned against. Asserted rather than assumed by
#: ``load_interfaces``: if the hook moves, this test must fail loudly instead of
#: quietly deriving an empty field set and passing.
FRONTEND_TYPE_FILE = REPO_DIR / "dashboard-react" / "src" / "hooks" / "use-pipeline-costs.ts"
RESPONSE_TYPE = "PipelineCosts"
RUN_TYPE = "PipelineCostRun"

#: Named interfaces in scope that serialize to a JSON object. The reader will not
#: guess this — an identifier it was not told about raises, so a renamed or
#: mistyped type cannot slip through as "some object".
OBJECT_TYPES = frozenset({RUN_TYPE})

PIPELINE_ID = "pipe-cost-contract"


# ---------------------------------------------------------------------------
# Fixture data — shaped as job_completion actually writes it
# ---------------------------------------------------------------------------
# ``collect_job_metrics`` builds the whole metrics dict and
# ``job_completion.lambda_handler`` writes it to ``output_data`` through
# ``convert_floats_to_decimal``, so every number comes back off DynamoDB as a
# ``Decimal`` — including ``actual_cost``, already rounded to 3 places at write
# time. The rows carry the other metrics keys too: the handler must keep reading
# only ``actual_cost`` out of a populated map, not assume a cost-only one.
def _run_row(
    run_id: str,
    actual_cost: float | None,
    timestamp: str | None,
    *,
    step_name: str = "run_pipeline",
) -> dict[str, Any]:
    """One state row for a finished (or in-flight) pipeline run.

    ``actual_cost=None`` omits the cost key, which is what a run still in flight
    looks like: the row exists, ``job_completion`` has not written metrics yet.
    ``timestamp=None`` omits the timestamp attribute entirely — the case the
    handler defaults to ``""``.

    ``step_name`` defaults to the canonical ``run_pipeline``; legacy rows carry
    ``RunPipeline`` and the handler's ``FilterExpression`` accepts both. Not
    asserted here: with a mocked table DynamoDB applies no filter, so a fixture
    could not tell the two apart.
    """
    output_data: dict[str, Any] = {
        "job_run_id": f"jr_{run_id}",
        "state": "SUCCEEDED",
        "execution_time_seconds": Decimal("642"),
        "duration_minutes": Decimal("10.7"),
        "worker_type": "G.1X",
        "number_of_workers": Decimal("2"),
        "dpu_hours": Decimal("0.3567"),
        "cost_per_dpu_hour": Decimal("0.44"),
        "error_message": None,
        "glue_version": "5.0",
    }
    if actual_cost is not None:
        output_data["actual_cost"] = Decimal(str(actual_cost))

    row: dict[str, Any] = {
        "run_id": run_id,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "status": "success" if actual_cost is not None else "running",
        "output_data": output_data,
    }
    if timestamp is not None:
        row["timestamp"] = timestamp
    return row


#: Three finished runs plus one still in flight. ``run_count`` must be 3 while
#: the query returned 4 rows.
THREE_COSTED_ONE_IN_FLIGHT: list[dict[str, Any]] = [
    _run_row("run-a", 1.507, "2026-07-25T09:00:00+00:00"),
    _run_row("run-b", 2.5, "2026-07-26T09:00:00+00:00"),
    _run_row("run-c", 12.75, "2026-07-27T09:00:00+00:00"),
    _run_row("run-d-inflight", None, "2026-07-28T09:00:00+00:00"),
]

#: Rows exist, none carries a cost. This is the response a pipeline gives while
#: its first run is still going, and the shape most easily broken by an early
#: return, so the full contract is asserted against it too.
NO_COSTED_RUNS: list[dict[str, Any]] = [
    _run_row("run-x-inflight", None, "2026-07-27T09:00:00+00:00"),
    _run_row("run-y-inflight", None, "2026-07-28T09:00:00+00:00"),
]

#: Ten costed runs, so ``run_count`` (10) and ``len(cost_trend)`` (7) diverge.
TEN_COSTED_RUNS: list[dict[str, Any]] = [
    _run_row(f"run-{idx:02d}", float(idx), f"2026-07-{idx:02d}T09:00:00+00:00") for idx in range(1, 11)
]

#: A finished run whose state row carries no ``timestamp`` attribute at all.
COSTED_RUN_WITHOUT_TIMESTAMP: list[dict[str, Any]] = [
    _run_row("run-dated", 3.25, "2026-07-27T09:00:00+00:00"),
    _run_row("run-undated", 1.125, None),
]


def _state_table(items: list[dict[str, Any]]) -> MagicMock:
    table = MagicMock()
    table.query.return_value = {"Items": items}
    return table


@pytest.fixture
def state_table(mock_lambda_env, monkeypatch):
    """Install a state table returning the rows a test asks for.

    ``handlers/costs.py`` binds ``state_table`` by value at import
    (``from .common import … state_table``), so the handler module is the object
    to patch. Nothing here creates a ``moto.mock_aws()`` or its own boto3
    client — the response shape is the subject, not DynamoDB.
    """

    def _install(items: list[dict[str, Any]]) -> MagicMock:
        table = _state_table(items)
        monkeypatch.setattr(_costs, "state_table", table)
        return table

    return _install


@pytest.fixture(scope="module")
def frontend_types() -> dict[str, dict[str, tuple[bool, str]]]:
    """The two frontend interfaces this endpoint's response must satisfy."""
    return load_interfaces(FRONTEND_TYPE_FILE, RESPONSE_TYPE, RUN_TYPE)


def _parse(response: dict) -> tuple[int, Any]:
    return response["statusCode"], json.loads(response["body"])


def _get(state_table_factory, items: list[dict[str, Any]]) -> tuple[int, Any]:
    state_table_factory(items)
    return _parse(_costs.get_pipeline_costs(PIPELINE_ID))


def _assert_types(obj: dict, interface: dict[str, tuple[bool, str]], label: str) -> None:
    """Check every declared field of ``interface`` against ``obj``.

    A required field that is absent is reported as such rather than raising
    ``KeyError``, so a rename in the handler produces a message naming the type
    it drifted from.
    """
    for field, (required, ts_type) in interface.items():
        if field not in obj:
            assert not required, (
                f"{label}.{field} is declared required ({ts_type}) by {FRONTEND_TYPE_FILE.name} and is "
                f"absent from the response. Keys present: {sorted(obj)}"
            )
            continue
        assert matches_json_type(obj[field], ts_type, OBJECT_TYPES), (
            f"{label}.{field} is declared {ts_type}, response carried {type(obj[field]).__name__} ({obj[field]!r})"
        )


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------
class TestPipelineCostsResponseContract:
    """GET /api/pipelines/{id}/costs must satisfy ``PipelineCosts``."""

    def test_response_has_every_required_top_level_field(self, state_table, frontend_types):
        status, body = _get(state_table, THREE_COSTED_ONE_IN_FLIGHT)
        assert status == 200
        missing = required_fields(frontend_types[RESPONSE_TYPE]) - set(body)
        assert not missing, (
            f"Response is missing {sorted(missing)}, which the frontend type {RESPONSE_TYPE} "
            f"({FRONTEND_TYPE_FILE.name}) declares required. usePipelineCosts casts the body to that "
            f"type without validating it, so a missing key reaches the component as undefined."
        )

    def test_top_level_field_types_match_the_frontend_type(self, state_table, frontend_types):
        _, body = _get(state_table, THREE_COSTED_ONE_IN_FLIGHT)
        _assert_types(body, frontend_types[RESPONSE_TYPE], RESPONSE_TYPE)

    def test_empty_state_still_satisfies_the_whole_contract(self, state_table, frontend_types):
        """No run carries a cost yet: every declared key must still be there.

        This is the response a pipeline gives before its first Glue job
        finishes, so it is the shape most likely to be relied on, and an early
        return is the easiest way to break it.
        """
        status, body = _get(state_table, NO_COSTED_RUNS)
        assert status == 200
        missing = required_fields(frontend_types[RESPONSE_TYPE]) - set(body)
        assert not missing, f"Empty-state response is missing required {RESPONSE_TYPE} fields: {sorted(missing)}"
        _assert_types(body, frontend_types[RESPONSE_TYPE], RESPONSE_TYPE)
        assert body["cost_trend"] == []
        assert body["pipeline_id"] == PIPELINE_ID

    def test_decimal_costs_reach_the_wire_as_json_numbers(self, state_table):
        """DynamoDB hands back ``Decimal``; the wire must carry JSON numbers.

        The fixtures store ``actual_cost`` as ``Decimal`` because that is what
        the table returns. ``PipelineCosts`` declares ``number`` on the two
        aggregates and ``number`` on ``PipelineCostRun.actual_cost``, and the panel
        passes all of them to ``formatUsd``, which is arithmetic on a number —
        a server-side string (say a pre-formatted ``"1.507"``) would type-check
        on neither side and render as ``NaN``-ish output.
        """
        _, body = _get(state_table, THREE_COSTED_ONE_IN_FLIGHT)
        numeric = [body["total_cost"], body["average_cost_per_run"]]
        numeric += [entry["actual_cost"] for entry in body["cost_trend"]]
        numeric.append(body["most_expensive_run"]["actual_cost"])
        for value in numeric:
            assert isinstance(value, (int, float)) and not isinstance(value, bool), (
                f"cost value reached the wire as {type(value).__name__} ({value!r}); every cost field "
                f"is declared number"
            )


# ---------------------------------------------------------------------------
# most_expensive_run: null, not absent
# ---------------------------------------------------------------------------
class TestMostExpensiveRunNullVersusAbsent:
    """``most_expensive_run: PipelineCostRun | null`` — the key is always there."""

    def test_frontend_type_still_declares_the_nullable_union(self, frontend_types):
        """Guard on the oracle, not on the handler.

        The two tests below are only meaningful while the interface declares
        ``most_expensive_run`` as required and nullable. Dropping the ``| null``
        or adding a ``?`` would make the handler's ``null`` a type violation or
        make absence legal — either would quietly change what the tests below
        mean, so the shape of the oracle is asserted rather than assumed.
        """
        required, ts_type = frontend_types[RESPONSE_TYPE]["most_expensive_run"]
        assert required, (
            f"{RESPONSE_TYPE}.most_expensive_run became optional (`?:`). The handler always emits the "
            f"key, so making it optional tells consumers absence is possible when it is not."
        )
        assert declares_null(ts_type), (
            f"{RESPONSE_TYPE}.most_expensive_run is declared {ts_type!r}, which does not admit null. "
            f"The handler emits null whenever no run carries a cost, so this type is now wrong — do "
            f"not resolve that by narrowing the type."
        )

    def test_key_is_present_and_null_when_no_run_carries_a_cost(self, state_table):
        """Null, never omitted.

        ``PipelineCosts`` declares the key non-optional, so ``!== null`` is the
        narrowing the type invites. An absent key arrives as ``undefined``,
        passes that check, and throws on the property access immediately after.
        """
        _, body = _get(state_table, NO_COSTED_RUNS)
        assert "most_expensive_run" in body, (
            "most_expensive_run was omitted from the body. PipelineCosts declares it "
            "`PipelineCostRun | null`, not optional: a consumer narrowing with `!== null` accepts "
            "undefined and then throws on .actual_cost."
        )
        assert body["most_expensive_run"] is None, (
            f"Expected null when no run carries a cost, got {body['most_expensive_run']!r}"
        )

    def test_most_expensive_run_satisfies_pipeline_cost_run_when_present(self, state_table, frontend_types):
        _, body = _get(state_table, THREE_COSTED_ONE_IN_FLIGHT)
        run = body["most_expensive_run"]
        assert isinstance(run, dict)
        missing = required_fields(frontend_types[RUN_TYPE]) - set(run)
        assert not missing, f"most_expensive_run is missing {sorted(missing)} declared by {RUN_TYPE}"
        _assert_types(run, frontend_types[RUN_TYPE], f"{RESPONSE_TYPE}.most_expensive_run")


# ---------------------------------------------------------------------------
# run_count semantics
# ---------------------------------------------------------------------------
class TestRunCountSemantics:
    """``run_count`` counts costed runs — not rows, and not trend entries."""

    def test_run_count_counts_costed_runs_not_rows_returned(self, state_table):
        """A run still in flight has a state row and no cost.

        So ``run_count`` is below the number of rows the query matched. A test
        that equated the two would pin a falsehood, and a consumer reading
        ``run_count`` as "how many times this pipeline ran" is being misled —
        which is why the panel's empty state takes ``hasRuns`` from the caller's
        execution list instead of inferring it from here.
        """
        _, body = _get(state_table, THREE_COSTED_ONE_IN_FLIGHT)
        costed = sum(1 for row in THREE_COSTED_ONE_IN_FLIGHT if "actual_cost" in row["output_data"])
        assert body["run_count"] == costed == 3
        assert body["run_count"] < len(THREE_COSTED_ONE_IN_FLIGHT), (
            "Fixture drift: this test needs at least one row without a cost, otherwise it cannot tell "
            "'runs with a cost' from 'rows returned'."
        )
        assert len(body["cost_trend"]) == costed, "every costed run should appear in the trend below 7"

    def test_run_count_is_not_the_length_of_cost_trend(self, state_table):
        """``cost_trend`` is capped at 7; ``run_count`` is not.

        ``PipelineCostPanel``'s footer renders "(of N with a recorded cost)"
        only when ``run_count > cost_trend.length``, so the two must be allowed
        to differ. The invariant that does hold is ``>=``.
        """
        _, body = _get(state_table, TEN_COSTED_RUNS)
        assert body["run_count"] == 10
        assert len(body["cost_trend"]) == 7
        assert body["run_count"] >= len(body["cost_trend"]), (
            "run_count must never be below len(cost_trend) — the trend is a slice of the counted runs"
        )


# ---------------------------------------------------------------------------
# cost_trend entries
# ---------------------------------------------------------------------------
class TestCostTrendEntryContract:
    """Every entry must satisfy the frontend ``PipelineCostRun`` type."""

    def test_every_trend_entry_satisfies_pipeline_cost_run(self, state_table, frontend_types):
        _, body = _get(state_table, TEN_COSTED_RUNS)
        required = required_fields(frontend_types[RUN_TYPE])
        assert body["cost_trend"], "fixture must produce entries, or this test asserts nothing"
        for idx, entry in enumerate(body["cost_trend"]):
            missing = required - set(entry)
            assert not missing, (
                f"cost_trend[{idx}] is missing {sorted(missing)}, which the frontend type {RUN_TYPE} "
                f"({FRONTEND_TYPE_FILE.name}) declares required."
            )
            _assert_types(entry, frontend_types[RUN_TYPE], f"cost_trend[{idx}]")

    def test_timestamp_is_the_empty_string_not_null_when_the_row_has_none(self, state_table):
        """A row with no ``timestamp`` still yields a run, with ``""``.

        ``PipelineCostRun.timestamp`` is declared ``string``, and the handler
        defaults the attribute rather than dropping the run — so the run must
        both appear and keep a string type. ``null`` here would violate the
        declared type even though the panel's ``item.timestamp ? … : '—'`` guard
        happens to render the same thing for both.
        """
        _, body = _get(state_table, COSTED_RUN_WITHOUT_TIMESTAMP)
        by_run = {entry["run_id"]: entry for entry in body["cost_trend"]}
        assert "run-undated" in by_run, (
            "The run whose state row carries no timestamp was dropped from cost_trend. The handler "
            "defaults the attribute instead, and its cost is already counted in run_count — dropping "
            "it from the trend would make the two disagree."
        )
        assert by_run["run-undated"]["timestamp"] == "", (
            f"Expected the empty string for a row with no timestamp, got {by_run['run-undated']['timestamp']!r}"
        )
        assert isinstance(by_run["run-undated"]["timestamp"], str), (
            "PipelineCostRun.timestamp is declared `string`, not `string | null`"
        )
        assert body["run_count"] == 2, "the undated run must still be counted"


# ---------------------------------------------------------------------------
# Failure must not look like a free pipeline
# ---------------------------------------------------------------------------
class TestQueryFailureIsNotAZeroCostResponse:
    """A read failure must not arrive as a well-formed all-zero Cost_Summary."""

    def test_query_failure_returns_500_without_a_cost_shaped_body(self, mock_lambda_env, monkeypatch):
        table = MagicMock()
        table.query.side_effect = RuntimeError("ProvisionedThroughputExceededException")
        monkeypatch.setattr(_costs, "state_table", table)

        status, body = _parse(_costs.get_pipeline_costs(PIPELINE_ID))

        assert status == 500
        assert "error" in body
        cost_fields = {"total_cost", "average_cost_per_run", "cost_trend", "most_expensive_run", "run_count"}
        assert not (cost_fields & set(body)), (
            f"The failure response carries cost fields {sorted(cost_fields & set(body))}. A 200 with "
            f"zeros would render as $0.00 — 'this pipeline is free' — instead of the panel's error "
            f"state, which is the misreading the Async_State_Triple exists to prevent (R3.4)."
        )
