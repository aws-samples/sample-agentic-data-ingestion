# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Previous/Next Run resolve from the index, at any depth (p48 C10, R10.2, task 5.2).

THE DEFECT
----------
``RunDetailPage`` fetched ONE page of ``get_pipeline_executions`` and derived
prev/next by ``findIndex`` + array arithmetic. Two consequences, and the second is
the requirement:

1. it read a whole page — plus that page's ``Select="COUNT"`` walk and its
   ``load_metrics`` batch-gets — to extract two ids; and
2. **it went dead at the page edge.** The neighbour of the tenth run is the
   eleventh, which is on page 2, and nothing on page 1 can name it.

``get_run_neighbours`` answers with three keyed reads at ``Limit=1``:
one ``get_item`` for this run's ``started_at``, then a ``Limit=1`` Query over
``RunsByTimeIndex`` in each direction with this run's index key as
``ExclusiveStartKey``.

WHY MOTO AND NOT A MOCK
-----------------------
The same reason ``test_run_history_ordering.py`` gives for itself: the thing under
test is *which row DynamoDB returns for a seek*, and a ``MagicMock`` returns
whatever the test hands it. A mock could pin that ``ExclusiveStartKey`` was passed;
it cannot pin that the row on the other side of it is the right one. So the seek is
performed by moto against the real key schema, and the assertions are about rows.

THE FIXTURE IS LIVE DATA, AND SO IS THE EXPECTED ANSWER
-------------------------------------------------------
``LIVE_PARTITION`` / ``EXPECTED_BY_TIME`` are ``latesttesting-19ca826a`` as measured
against the validation account / us-west-2 (identical to ``test_run_history_ordering.py``'s, on
purpose — one partition, one ordering authority, R10.4). The neighbour answers were
additionally confirmed by running the two ``Limit=1`` queries against the live table
at task 5.2 before being asserted here:

* seeking from ``run-2026-08-21-17-13-33-9c2bc0e6`` (index position 4) returned
  ``skipped-42e546628820e9630a45c6bcfd8792e1`` as ``older`` and
  ``run-2026-08-22-12-43-57-f009d524`` as ``newer``;
* seeking ``newer`` from the newest row and ``older`` from the oldest both returned
  ``Count: 0``.

So a failure here means the handler diverged from observed DynamoDB behaviour rather
than from a guess.

THE DIRECTION CONVENTION, PINNED
--------------------------------
``older`` = started earlier = the **Previous Run** button. ``newer`` = started later
= **Next Run**. Getting this backwards is an invisible bug — both arms return a
plausible run id and the buttons still work, they just go the wrong way — so
``TestDirectionConvention`` asserts it against a partition whose chronology is
unambiguous, independently of the live fixture.

WHAT IS DELIBERATELY *NOT* ASSERTED HERE
----------------------------------------
* **Ordering of the run-history list.** ``test_run_history_ordering.py`` owns C1.
  This file asserts only that a *seek* lands on the right neighbour.
* **Row filtering.** ``SKIPPED`` and ``QUEUED`` rows are legitimate navigation
  targets and ``TestNonRunRowsAreNeighbours`` pins that they are NOT skipped.
  Task 5.6's previewable-run resolver skips such rows, which is a different
  question — choosing a run to render data FROM, not one to navigate TO.
* **Sparseness as a filter.** ``tests/unit/lambdas/test_executions_index_sparseness.py``
  owns R9 and do-NOT 2. ``TestRowAbsentFromIndex`` asserts only the handler's own
  degrade behaviour for one such row.

Validates: p48 Requirements 10.2, 10.4
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import boto3
import pytest

# ---------------------------------------------------------------------------
# Path and import-time env — must precede importing the handler
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(_SHARED_DIR), os.path.abspath(_LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# ``handlers/common.py`` calls ``require_env`` at IMPORT time, before any fixture
# runs — same reason ``test_run_history_ordering.py`` sets these at module scope.
for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "ERRORS_TABLE": "agentic_data_ingestion_errors",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

from handlers import common as common_handler
from handlers import pipelines_runs as runs_handler

EXECUTIONS_TABLE = "agentic_data_ingestion_executions"
STATE_TABLE = "agentic_data_ingestion_state"
MOTO_REGION = "us-east-1"  # matches conftest._create_moto_dynamodb_tables

PIPELINE_ID = "latesttesting-19ca826a"

#: ``latesttesting-19ca826a`` in the validation account / us-west-2, measured 2026-08-24.
#: ``(run_id, started_at, status)``. Same nine rows as
#: ``test_run_history_ordering.py``'s fixture, deliberately.
LIVE_PARTITION: tuple[tuple[str, str, str], ...] = (
    ("skipped-7412636fccb938719673179658c05773", "2026-08-24T13:04:04.864784+00:00", "SKIPPED"),
    ("skipped-42e546628820e9630a45c6bcfd8792e1", "2026-08-20T20:51:59.783707+00:00", "SKIPPED"),
    ("run-2026-08-24-13-48-26-4fccdf03", "2026-08-24T13:48:26.863631+00:00", "SUCCEEDED"),
    ("run-2026-08-24-13-04-01-421fc544", "2026-08-24T13:04:01.691321+00:00", "SUCCEEDED"),
    ("run-2026-08-22-12-43-57-f009d524", "2026-08-22T12:43:57.671036+00:00", "SUCCEEDED"),
    ("run-2026-08-21-17-13-33-9c2bc0e6", "2026-08-21T17:13:33.751749+00:00", "SUCCEEDED"),
    ("run-2026-08-20-18-08-57-70909254", "2026-08-20T18:08:57.353328+00:00", "SUCCEEDED"),
    ("run-2026-08-20-17-59-33-8505fa5b", "2026-08-20T17:59:33.097863+00:00", "SUCCEEDED"),
    ("run-2026-08-20-17-52-21-248afa7a", "2026-08-20T17:52:21.739239+00:00", "SUCCEEDED"),
)

#: The same nine rows newest-first, confirmed by querying ``RunsByTimeIndex``
#: against the validation account rather than by sorting the tuple above in Python.
EXPECTED_BY_TIME: tuple[str, ...] = (
    "run-2026-08-24-13-48-26-4fccdf03",
    "skipped-7412636fccb938719673179658c05773",
    "run-2026-08-24-13-04-01-421fc544",
    "run-2026-08-22-12-43-57-f009d524",
    "run-2026-08-21-17-13-33-9c2bc0e6",
    "skipped-42e546628820e9630a45c6bcfd8792e1",
    "run-2026-08-20-18-08-57-70909254",
    "run-2026-08-20-17-59-33-8505fa5b",
    "run-2026-08-20-17-52-21-248afa7a",
)

#: Rows in a synthetic partition wide enough that a run in its middle can never
#: appear on page 1. ``EXECUTIONS_PAGE_SIZE`` is 10
#: (``dashboard-react/src/hooks/use-executions.ts``), so 25 puts the target run on
#: page 2 and its ``newer`` neighbour on page 2 as well — the case the old array
#: arithmetic could not express at all.
DEEP_PARTITION_ROWS = 25

#: 0-based index into the deep partition's newest-first order. 17 is page 2 of a
#: 10-row pager, comfortably past the edge and not adjacent to either end.
DEEP_TARGET_INDEX = 17


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def runs_tables(mock_dynamodb_tables, monkeypatch) -> dict[str, Any]:
    """Bind moto handles onto the handler's namespace.

    ``handlers/pipelines_runs.py`` does ``from .common import executions_table,
    state_table, dynamodb``, copying the references into its own namespace, so
    patching ``common`` alone would patch names the handler never reads. Both are
    patched, exactly as ``test_run_history_ordering.py`` does, so a future change to
    either access shape cannot silently un-test this file.
    """
    handles = {
        "executions_table": mock_dynamodb_tables[EXECUTIONS_TABLE],
        "state_table": mock_dynamodb_tables[STATE_TABLE],
    }
    for attribute, table in handles.items():
        monkeypatch.setattr(runs_handler, attribute, table)
        monkeypatch.setattr(common_handler, attribute, table)
    moto_dynamodb = boto3.resource("dynamodb", region_name=MOTO_REGION)
    monkeypatch.setattr(runs_handler, "dynamodb", moto_dynamodb)
    monkeypatch.setattr(common_handler, "dynamodb", moto_dynamodb)
    return handles


def _row(run_id: str, started_at: str | None, status: str, pipeline_id: str = PIPELINE_ID) -> dict[str, Any]:
    """An executions row shaped like the live ones.

    ``current_step`` is always set because both live skip records carry it, so
    neither is a Phantom_Execution_Row. Navigation does not apply the phantom filter
    — a neighbour is a position in the index, not a rendered list entry — but the
    rows are shaped faithfully so this file's fixture cannot diverge from
    ``test_run_history_ordering.py``'s.

    ``started_at=None`` omits the attribute entirely, which is how a row becomes
    absent from the sparse index (R9.1).
    """
    row: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "run_id": run_id,
        "status": status,
        "current_step": "duplicate_check" if run_id.startswith("skipped-") else "run_pipeline",
    }
    if started_at is not None:
        row["started_at"] = started_at
    return row


def _seed(table: Any, rows: tuple[tuple[str, str, str], ...] = LIVE_PARTITION, **kwargs: Any) -> None:
    for run_id, started_at, status in rows:
        table.put_item(Item=_row(run_id, started_at, status, **kwargs))


def _deep_partition() -> tuple[tuple[str, str, str], ...]:
    """``DEEP_PARTITION_ROWS`` rows, one per day, newest LAST in this tuple.

    Built in ascending order so the tuple reads chronologically; the index sorts it
    and :func:`_newest_first` is what the assertions compare against.
    """
    return tuple(
        (f"run-deep-{index:03d}", f"2026-07-{index + 1:02d}T12:00:00.000000+00:00", "SUCCEEDED")
        for index in range(DEEP_PARTITION_ROWS)
    )


def _newest_first(rows: tuple[tuple[str, str, str], ...]) -> list[str]:
    """Run ids in ``started_at`` descending order — the order the index returns."""
    return [run_id for run_id, _started_at, _status in sorted(rows, key=lambda r: r[1], reverse=True)]


def _raw_call(run_id: str, pipeline_id: str = PIPELINE_ID) -> dict[str, Any]:
    """The handler's raw response, status code included — 404 and 200 both visible."""
    return runs_handler.get_run_neighbours(pipeline_id, run_id)


def _call(run_id: str, pipeline_id: str = PIPELINE_ID) -> dict[str, Any]:
    response = _raw_call(run_id, pipeline_id=pipeline_id)
    assert response["statusCode"] == 200, response
    return json.loads(response["body"])


def _ids(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(older_run_id, newer_run_id)``, with ``None`` preserved as ``None``."""
    older = body["older"]
    newer = body["newer"]
    return (
        older["run_id"] if older else None,
        newer["run_id"] if newer else None,
    )


# ---------------------------------------------------------------------------
# The requirement: beyond page 1
# ---------------------------------------------------------------------------
class TestBeyondPageOne:
    """R10.2 — the buttons must step past the edge of page 1.

    This is the requirement, and it is the one thing a test against a 9-row
    partition cannot check: nine rows fit on one page, so every neighbour is already
    in hand and a passing test proves nothing about the defect.
    """

    def test_the_target_run_is_not_on_page_one(self, runs_tables):
        """The premise, asserted rather than assumed.

        If this run WERE on page 1 the rest of this class would be vacuous, so the
        precondition is checked against the real pager. This is also the proof that
        the mechanism being replaced could not have answered: the old code had only
        page 1's rows and could not name a run that is not in them.
        """
        rows = _deep_partition()
        _seed(runs_tables["executions_table"], rows)
        target = _newest_first(rows)[DEEP_TARGET_INDEX]

        page_one = json.loads(runs_handler.get_pipeline_executions(PIPELINE_ID, 1, 10)["body"])
        page_one_ids = [execution["run_id"] for execution in page_one["executions"]]

        assert target not in page_one_ids
        assert len(page_one_ids) == 10

    def test_both_neighbours_resolve_for_a_run_on_page_two(self, runs_tables):
        rows = _deep_partition()
        _seed(runs_tables["executions_table"], rows)
        order = _newest_first(rows)
        target = order[DEEP_TARGET_INDEX]

        older, newer = _ids(_call(target))

        # Newest-first order, so the NEXT entry is the next-older and the PREVIOUS
        # entry is the next-newer. Spelled out because this is exactly the mapping
        # that is easy to invert.
        assert older == order[DEEP_TARGET_INDEX + 1]
        assert newer == order[DEEP_TARGET_INDEX - 1]

    def test_every_run_in_a_deep_partition_resolves_its_neighbours(self, runs_tables):
        """A full sweep, so no single position can pass by luck.

        Chains the whole partition end to end: stepping ``older`` from the newest run
        must visit all 25 rows in order and stop, which is a stronger statement than
        checking any one pair. If the seek were off by one, or dropped a row, or
        looped, this fails.
        """
        rows = _deep_partition()
        _seed(runs_tables["executions_table"], rows)
        order = _newest_first(rows)

        visited = [order[0]]
        while True:
            older, _newer = _ids(_call(visited[-1]))
            if older is None:
                break
            assert older not in visited, f"{older} revisited — the walk looped"
            visited.append(older)
            assert len(visited) <= DEEP_PARTITION_ROWS, "walk exceeded the partition"

        assert visited == order


# ---------------------------------------------------------------------------
# The direction convention
# ---------------------------------------------------------------------------
class TestDirectionConvention:
    """``older`` is earlier in time; ``newer`` is later. Nothing else decides it.

    Asserted against three rows whose chronology is unambiguous and whose ``run_id``
    lexicographic order is the REVERSE of their chronological order — so a handler
    that had fallen back to ``run_id`` ordering would fail here, and a handler that
    had the two directions swapped would fail too.
    """

    #: ``zzz`` is oldest and ``aaa`` newest, so ``run_id`` ordering and time ordering
    #: disagree completely.
    INVERTED: tuple[tuple[str, str, str], ...] = (
        ("run-zzz", "2026-01-01T00:00:00.000000+00:00", "SUCCEEDED"),
        ("run-mmm", "2026-02-01T00:00:00.000000+00:00", "SUCCEEDED"),
        ("run-aaa", "2026-03-01T00:00:00.000000+00:00", "SUCCEEDED"),
    )

    def test_older_is_the_earlier_run_and_newer_the_later(self, runs_tables):
        _seed(runs_tables["executions_table"], self.INVERTED)

        older, newer = _ids(_call("run-mmm"))

        assert older == "run-zzz", "older must be the run that started EARLIER"
        assert newer == "run-aaa", "newer must be the run that started LATER"

    def test_the_response_reports_each_neighbour_started_at(self, runs_tables):
        """The timestamps are on the wire, so the claim is checkable by a client."""
        _seed(runs_tables["executions_table"], self.INVERTED)

        body = _call("run-mmm")

        assert body["older"]["started_at"] < body["newer"]["started_at"]
        assert body["older"]["started_at"] == "2026-01-01T00:00:00.000000+00:00"
        assert body["newer"]["started_at"] == "2026-03-01T00:00:00.000000+00:00"


# ---------------------------------------------------------------------------
# The ends
# ---------------------------------------------------------------------------
class TestTheEnds:
    """Absence is ``None`` and both keys are always present.

    A client must never have to tell "there is no such run" from "the question was
    not answered", so neither key is ever omitted.
    """

    def test_the_newest_run_has_no_newer_neighbour(self, runs_tables):
        _seed(runs_tables["executions_table"])

        older, newer = _ids(_call(EXPECTED_BY_TIME[0]))

        assert newer is None
        assert older == EXPECTED_BY_TIME[1]

    def test_the_oldest_run_has_no_older_neighbour(self, runs_tables):
        _seed(runs_tables["executions_table"])

        older, newer = _ids(_call(EXPECTED_BY_TIME[-1]))

        assert older is None
        assert newer == EXPECTED_BY_TIME[-2]

    def test_a_pipelines_only_run_has_neither_neighbour(self, runs_tables):
        _seed(
            runs_tables["executions_table"],
            (("run-solo", "2026-05-05T05:05:05.000000+00:00", "SUCCEEDED"),),
        )

        body = _call("run-solo")

        assert body["older"] is None
        assert body["newer"] is None

    def test_both_keys_are_always_present_even_when_null(self, runs_tables):
        _seed(
            runs_tables["executions_table"],
            (("run-solo", "2026-05-05T05:05:05.000000+00:00", "SUCCEEDED"),),
        )

        body = _call("run-solo")

        assert "older" in body
        assert "newer" in body

    def test_a_neighbour_in_another_pipeline_is_not_offered(self, runs_tables):
        """The partition is the boundary. A run in pipeline B is never B's neighbour.

        Seeded so the other pipeline's row is chronologically BETWEEN this
        pipeline's two, which is the arrangement that would surface a missing
        partition binding.
        """
        table = runs_tables["executions_table"]
        _seed(
            table,
            (
                ("run-mine-old", "2026-04-01T00:00:00.000000+00:00", "SUCCEEDED"),
                ("run-mine-new", "2026-04-03T00:00:00.000000+00:00", "SUCCEEDED"),
            ),
        )
        _seed(
            table,
            (("run-theirs", "2026-04-02T00:00:00.000000+00:00", "SUCCEEDED"),),
            pipeline_id="some-other-pipeline",
        )

        older, newer = _ids(_call("run-mine-new"))

        assert older == "run-mine-old"
        assert newer is None


# ---------------------------------------------------------------------------
# Non-run rows are legitimate neighbours
# ---------------------------------------------------------------------------
class TestNonRunRowsAreNeighbours:
    """``SKIPPED`` / ``QUEUED`` rows are positions in the history and are NOT skipped.

    Filtering them here would make Previous/Next disagree with the list they
    navigate, and disagreeing with the index is a second ordering authority — which
    R10.4 forbids. Task 5.6 skips such rows when choosing a run to PREVIEW; that is
    a different question and is not licence to skip them for navigation.
    """

    def test_a_skipped_row_is_returned_as_a_neighbour(self, runs_tables):
        """From the live partition: the newest run's older neighbour IS a skip."""
        _seed(runs_tables["executions_table"])

        body = _call(EXPECTED_BY_TIME[0])

        assert body["older"]["run_id"] == "skipped-7412636fccb938719673179658c05773"
        assert body["older"]["status"] == "SKIPPED"

    def test_a_skipped_row_resolves_its_own_neighbours(self, runs_tables):
        """A skip is navigable FROM as well as TO — it is an ordinary index row."""
        _seed(runs_tables["executions_table"])
        skip = "skipped-42e546628820e9630a45c6bcfd8792e1"
        position = EXPECTED_BY_TIME.index(skip)

        older, newer = _ids(_call(skip))

        assert newer == EXPECTED_BY_TIME[position - 1]
        assert older == EXPECTED_BY_TIME[position + 1]

    def test_a_queued_placeholder_is_returned_as_a_neighbour(self, runs_tables):
        """R1.2's row, which under ``run_id`` ordering sorted last for every pipeline.

        ``QUEUED`` is split into ``queued_files`` by the run-history read and is
        refused as a ``status`` filter value (O2), but it is still a row in the index
        with a real ``started_at`` — and on the live pipeline it was once the NEWEST
        event on the list. Navigation steps onto it.
        """
        _seed(
            runs_tables["executions_table"],
            (
                ("run-before", "2026-08-24T13:00:00.000000+00:00", "SUCCEEDED"),
                ("queued-ee4ce802", "2026-08-24T13:27:46.000000+00:00", "QUEUED"),
            ),
        )

        older, newer = _ids(_call("run-before"))

        assert newer == "queued-ee4ce802"
        assert older is None


# ---------------------------------------------------------------------------
# Degrade paths
# ---------------------------------------------------------------------------
class TestUnknownRun:
    def test_a_run_absent_from_the_partition_is_a_404(self, runs_tables):
        """Not a 200 with two nulls: the neighbour of a run that is not here is not a
        question with an answer, and an empty 200 would read as "this run is alone"."""
        _seed(runs_tables["executions_table"])

        response = _raw_call("run-does-not-exist")

        assert response["statusCode"] == 404
        assert "run-does-not-exist" in json.loads(response["body"])["error"]

    def test_a_run_in_the_wrong_pipeline_is_a_404(self, runs_tables):
        """The lookup is keyed on ``(pipeline_id, run_id)``, so a real run id under
        the wrong pipeline is absent rather than found."""
        _seed(runs_tables["executions_table"])

        response = _raw_call(EXPECTED_BY_TIME[0], pipeline_id="some-other-pipeline")

        assert response["statusCode"] == 404


class TestRowAbsentFromIndex:
    """R9.1's sparse row: present in the base table, absent from the index.

    10 of 61 live rows were measured like this. There is no position to seek from,
    so the honest answer is "no neighbours" — a 200, because the run itself exists,
    and explicitly NOT a fallback to ``run_id`` ordering, which would reintroduce
    this spec's defect on one row's behalf.
    """

    def test_a_row_with_no_started_at_reports_no_neighbours(self, runs_tables):
        table = runs_tables["executions_table"]
        _seed(table)
        table.put_item(Item=_row("run-no-timestamp", None, "SUCCEEDED"))

        body = _call("run-no-timestamp")

        assert body["older"] is None
        assert body["newer"] is None

    def test_a_sparse_row_does_not_disturb_other_rows_neighbours(self, runs_tables):
        """The sparse row is invisible to the seek, so it is not silently interposed."""
        table = runs_tables["executions_table"]
        _seed(table)
        table.put_item(Item=_row("run-no-timestamp", None, "SUCCEEDED"))

        older, newer = _ids(_call(EXPECTED_BY_TIME[4]))

        assert older == EXPECTED_BY_TIME[5]
        assert newer == EXPECTED_BY_TIME[3]


class TestTiedTimestamps:
    """Two runs sharing a ``started_at``. A GSI sort key is not unique.

    ``ExclusiveStartKey`` on an index carries the base-table key as well as the index
    key, which is what makes the seek exact here. The property that matters is that
    no row is its own neighbour and no row is skipped.
    """

    TIED: tuple[tuple[str, str, str], ...] = (
        ("run-tie-a", "2026-06-01T00:00:00.000000+00:00", "SUCCEEDED"),
        ("run-tie-b", "2026-06-01T00:00:00.000000+00:00", "SUCCEEDED"),
        ("run-tie-c", "2026-06-01T00:00:00.000000+00:00", "SUCCEEDED"),
    )

    def test_no_run_is_its_own_neighbour(self, runs_tables):
        _seed(runs_tables["executions_table"], self.TIED)

        for run_id, _started_at, _status in self.TIED:
            older, newer = _ids(_call(run_id))
            assert older != run_id
            assert newer != run_id

    def test_stepping_older_visits_every_tied_row_exactly_once(self, runs_tables):
        _seed(runs_tables["executions_table"], self.TIED)
        # Whichever row the index puts first among the ties, walking `older` from it
        # must reach the other two and stop.
        start = next(
            run_id
            for run_id, _s, _st in self.TIED
            if _ids(_call(run_id))[1] is None  # no newer neighbour ⇒ index-first
        )

        visited = [start]
        while True:
            older, _newer = _ids(_call(visited[-1]))
            if older is None:
                break
            assert older not in visited
            visited.append(older)
            assert len(visited) <= len(self.TIED)

        assert sorted(visited) == sorted(run_id for run_id, _s, _st in self.TIED)


# ---------------------------------------------------------------------------
# The mechanism, pinned where its cost is the point
# ---------------------------------------------------------------------------
class TestReadCost:
    """Three keyed reads, no walk. The cheapness IS the design claim (C10).

    Pinned because a later change that reintroduced a page read or a scan would keep
    every assertion above passing while undoing the reason the neighbour query was
    chosen over the full-page fetch.
    """

    def test_the_seek_uses_limit_one_on_the_index_in_both_directions(self, runs_tables, monkeypatch):
        _seed(runs_tables["executions_table"])
        real_query = runs_tables["executions_table"].query
        calls: list[dict[str, Any]] = []

        def recording_query(**kwargs: Any) -> Any:
            calls.append(kwargs)
            return real_query(**kwargs)

        monkeypatch.setattr(runs_tables["executions_table"], "query", recording_query)
        _call(EXPECTED_BY_TIME[4])

        assert len(calls) == 2, "one Query per direction and nothing else"
        assert {call["ScanIndexForward"] for call in calls} == {True, False}
        for call in calls:
            assert call["IndexName"] == runs_handler.RUNS_BY_TIME_INDEX
            assert call["Limit"] == 1
            # Seeking from THIS run's key is what makes the answer correct at any
            # depth. All three attributes are required on a GSI seek.
            assert set(call["ExclusiveStartKey"]) == {"pipeline_id", "run_id", "started_at"}
            assert call["ExclusiveStartKey"]["run_id"] == EXPECTED_BY_TIME[4]

    def test_the_index_name_comes_from_the_module_constant(self):
        """A mistyped ``IndexName`` is a request-time ``ValidationException``, not an
        import error, so the constant is the only spelling allowed to exist."""
        assert runs_handler.RUNS_BY_TIME_INDEX == "RunsByTimeIndex"

    def test_the_direction_map_is_not_inverted(self):
        """``RunsByTimeIndex`` sorts ascending, so reading FORWARD walks toward later
        times. This is the one line whose inversion no behavioural test above could
        distinguish from a data-ordering bug."""
        assert runs_handler._NEIGHBOUR_SCAN_FORWARD == {"older": False, "newer": True}


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------
class TestRouting:
    """``GET /api/pipeline/{id}/run/{run_id}/neighbours`` reaches the handler.

    A handler no route reaches is dead code, and the frontend hook would 404 against
    it while every test above passed. Filed in this file rather than in its own so
    the endpoint and its behaviour move together.

    Imported lazily inside the test because ``router`` pulls in the whole handler
    package, and this module's moto fixtures must not be shadowed by it at
    collection time.
    """

    def test_the_neighbours_route_dispatches_to_the_handler(self, monkeypatch):
        from unittest.mock import MagicMock

        import handlers.pipelines as pipelines_facade
        import router as dashboard_router

        mock_handler = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        monkeypatch.setattr(pipelines_facade, "get_run_neighbours", mock_handler)

        result = dashboard_router.route_request(
            {
                "httpMethod": "GET",
                "path": "/api/pipeline/latesttesting-19ca826a/run/run-abc/neighbours",
                "queryStringParameters": {},
            }
        )

        mock_handler.assert_called_once_with("latesttesting-19ca826a", "run-abc")
        assert result["statusCode"] == 200

    def test_the_facade_reexports_the_handler(self):
        """``router`` imports from ``handlers.pipelines``, not from
        ``handlers.pipelines_runs``, so the re-export is load-bearing."""
        import handlers.pipelines as pipelines_facade

        assert pipelines_facade.get_run_neighbours is runs_handler.get_run_neighbours
        assert "get_run_neighbours" in pipelines_facade.__all__
