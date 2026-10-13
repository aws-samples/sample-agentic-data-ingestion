# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The data preview resolves the newest *previewable* run (p48 C12, task 5.6).

WHY THIS IS A SEPARATE FILE FROM ``test_data_preview.py``
--------------------------------------------------------
That file drives the handler through an autouse ``MagicMock`` executions table, so
``query`` returns whatever list the test hands it — including the same list for
every call. It can pin the *call* and it does (``TestLatestRunResolution``), but it
cannot pin a **walk**: a mock does no sorting and no ``ExclusiveStartKey`` paging,
so a walk test written there would assert that a list the test itself ordered came
back ordered, and its second page would be its first page again. The defect C12
fixes is precisely that the top row was the wrong row, so the ordering *and* the
paging have to be performed by something other than the test. Hence moto, with
``RunsByTimeIndex`` on the real key schema.

THE DEFECT, AND WHY ORDERING ALONE WOULD HAVE LOOKED LIKE A FIX
---------------------------------------------------------------
``get_latest_preview`` took the top row of a ``run_id``-ordered query. On
``latesttesting-19ca826a`` that is ``skipped-7412636fccb938719673179658c05773``,
the duplicate-skip row for ``customer_data_3.csv`` — the file that opened this
spec. A duplicate-skip row has **zero** state rows, verified live (state-table
``Count: 0``), because ``_record_duplicate_skip`` writes only to the executions
table. So ``_resolve_source_file`` raised ``NotFoundError`` and the panel rendered
"Failed to load data preview". Permanently: the skip id is deterministic in the
content fingerprint (``pipeline_trigger/lambda_function.py:189``), so the row never
ages out and never re-sorts (R12.3).

Ordering by ``started_at`` moves that skip to position 2 on this partition and the
top row becomes a real run — **so ordering alone makes this pipeline work, by
luck.** design §3 do-NOT 10 names that as "the worst kind of fix — it will look
correct in testing and fail in production". Two of the cases below therefore put an
unpreviewable row at the top *by time*, which is the case ordering does not fix,
and :class:`TestRemovingTheWalkBreaksIt` mechanises the do-NOT: it runs the
endpoint against an ordering-only mutant of the resolver and asserts the mutant
404s where the real one succeeds.

THE FIXTURE IS LIVE DATA
------------------------
``LIVE_PARTITION`` is ``latesttesting-19ca826a`` as measured against the validation account /
us-west-2 on 2026-08-24, read-only, and re-confirmed at task 5.6 by querying
``RunsByTimeIndex`` directly. The walk was also confirmed against the live index:
reading descending from the newest row's key returns
``skipped-7412636f… → run-2026-08-24-13-04-01-421fc544``, the skip's state-row
count is 0, and that next row carries
``original_s3_uri = s3://…/latesttesting-19ca826a/customer_data_1.csv`` — so the
"walk past the skip" step below is observed behaviour, not a guess.

WHAT IS DELIBERATELY *NOT* ASSERTED HERE
----------------------------------------
* **Ordering of run history.** ``test_run_history_ordering.py`` owns C1.
* **The 404's rendering.** Task 5.7 owns ``DataPreviewTable.tsx``. What this file
  pins is that the backend answer is a 404 with an ``error`` sentence and no new
  shape (O14).
* **Sparseness as a filter.** ``tests/unit/lambdas/test_executions_index_sparseness.py``
  owns R9 and ``TestSparsenessIsNotAFilter``. Nothing here may be read as licence
  to treat index membership as the previewable predicate (do-NOT 2, R9.4).

FIXTURES
--------
Shared fixtures only — ``mock_dynamodb_tables`` and ``mock_s3_buckets`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``). Table names are set at module
scope because ``handlers/common.py`` calls ``require_env`` at **import** time,
before any fixture runs.

Validates: p48 Requirements 12.1, 12.2, 12.3
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import boto3
import pytest
from boto3.dynamodb.conditions import Key

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
from handlers import data_preview as dp
from handlers import pipelines_runs as runs_handler

EXECUTIONS_TABLE = "agentic_data_ingestion_executions"
STATE_TABLE = "agentic_data_ingestion_state"
SOURCE_BUCKET = "agentic-data-ingestion-source"
MOTO_REGION = "us-east-1"  # matches conftest._create_moto_dynamodb_tables

PIPELINE_ID = "latesttesting-19ca826a"

#: ``latesttesting-19ca826a`` as it stands in the validation account, measured 2026-08-24:
#: ``(run_id, started_at, previewable)``. The skip carries a real ``started_at``
#: (the skip instant, R1.4) and **no state rows**, which is why it is
#: unpreviewable — not because of its id and not because of its status.
LIVE_PARTITION: tuple[tuple[str, str, bool], ...] = (
    ("run-2026-08-24-13-48-26-4fccdf03", "2026-08-24T13:48:26.863631+00:00", True),
    ("skipped-7412636fccb938719673179658c05773", "2026-08-24T13:04:04.864784+00:00", False),
    ("run-2026-08-24-13-04-01-421fc544", "2026-08-24T13:04:01.691321+00:00", True),
    ("run-2026-08-22-12-43-57-f009d524", "2026-08-22T12:43:57.671036+00:00", True),
    ("skipped-42e546628820e9630a45c6bcfd8792e1", "2026-08-20T20:51:59.783707+00:00", False),
)

#: The newest row by time on the live partition, and the row a correct read returns
#: when it is present.
LIVE_NEWEST = "run-2026-08-24-13-48-26-4fccdf03"

#: The skip. Newest by ``run_id`` on the live partition — the pre-C12 answer — and
#: second-newest by ``started_at``.
LIVE_SKIP = "skipped-7412636fccb938719673179658c05773"

#: The row immediately below the skip by time. Confirmed live as the row the walk
#: lands on once the newest row is out of the way.
LIVE_NEXT_BELOW_SKIP = "run-2026-08-24-13-04-01-421fc544"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class _CountingTable:
    """A moto table that records every ``query`` it is asked to perform.

    Needed because the walk's cost bound is a claim about the **number of reads**,
    and that is invisible in the response. Delegates everything else, so the
    ordering and paging under test are still moto's and not this class's.
    """

    def __init__(self, table: Any) -> None:
        self._table = table
        self.queries: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> Any:
        self.queries.append(kwargs)
        return self._table.query(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._table, name)


@pytest.fixture
def preview_tables(mock_dynamodb_tables, mock_s3_buckets, monkeypatch) -> dict[str, Any]:
    """Bind moto handles onto the handler's namespace.

    ``handlers/data_preview.py`` does ``from .common import executions_table,
    state_table, s3_client``, which copies the references into its own namespace, so
    patching ``common`` alone would patch names the handler never reads. Both are
    patched so a future change to either access shape cannot silently un-test this
    file.

    ``s3_client`` is rebuilt inside the fixture rather than reused from import time:
    moto intercepts at the botocore layer, so the module-level client created before
    ``mock_aws()`` activated is not moto-backed and would reach real AWS.
    """
    executions = _CountingTable(mock_dynamodb_tables[EXECUTIONS_TABLE])
    state = mock_dynamodb_tables[STATE_TABLE]
    s3 = boto3.client("s3", region_name=MOTO_REGION)

    for module in (dp, common_handler):
        monkeypatch.setattr(module, "executions_table", executions, raising=False)
        monkeypatch.setattr(module, "state_table", state, raising=False)
        monkeypatch.setattr(module, "s3_client", s3, raising=False)

    return {"executions_table": executions, "state_table": state, "s3_client": s3, "buckets": mock_s3_buckets}


def _exec_row(run_id: str, started_at: str | None) -> dict[str, Any]:
    """An executions row shaped like the live ones.

    ``current_step`` is always set because both skip records carry it — the
    duplicate-content skip writes ``current_step="duplicate_check"`` — so no row
    seeded here is a Phantom_Execution_Row. That matters even though this read has
    no phantom filter: it keeps the fixture honest about *why* a skip row is
    unpreviewable, which is that it has no state rows, not that it looks empty.
    """
    row: dict[str, Any] = {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": "SKIPPED" if run_id.startswith("skipped-") else "SUCCEEDED",
        "current_step": "duplicate_check" if run_id.startswith("skipped-") else "run_pipeline",
    }
    if started_at is not None:
        row["started_at"] = started_at
    return row


def _seed_previewable(tables: dict[str, Any], run_id: str, *, body: str = '[{"name": "Alice"}]') -> str:
    """Give ``run_id`` a readable S3 source and return its URI."""
    key = f"{PIPELINE_ID}/{run_id}.json"
    tables["s3_client"].put_object(Bucket=SOURCE_BUCKET, Key=key, Body=body.encode("utf-8"))
    uri = f"s3://{SOURCE_BUCKET}/{key}"
    tables["state_table"].put_item(Item={"run_id": run_id, "step_name": "file_validation", "original_s3_uri": uri})
    return uri


def _seed_partition(
    tables: dict[str, Any],
    rows: tuple[tuple[str, str, bool], ...] = LIVE_PARTITION,
) -> dict[str, str]:
    """Seed executions rows, and state rows for the previewable ones only.

    A row marked unpreviewable gets **no** state row at all, which is the live shape
    of both a duplicate skip and a ``QUEUED`` placeholder.
    """
    uris: dict[str, str] = {}
    for run_id, started_at, previewable in rows:
        tables["executions_table"].put_item(Item=_exec_row(run_id, started_at))
        if previewable:
            uris[run_id] = _seed_previewable(tables, run_id)
    return uris


def _resp(response: dict) -> tuple[int, dict]:
    return response["statusCode"], json.loads(response["body"])


def _index_reads(tables: dict[str, Any]) -> list[dict[str, Any]]:
    return [q for q in tables["executions_table"].queries if q.get("IndexName") == dp.RUNS_BY_TIME_INDEX]


# ===========================================================================
# The walk resolves the newest previewable run
# ===========================================================================


class TestTheNewestPreviewableRunIsResolved:
    def test_the_live_partition_resolves_its_newest_run(self, preview_tables):
        """The baseline: when the newest row by time IS previewable, take it.

        Asserted so the walk cannot pass the harder cases below by always skipping
        the first row.
        """
        uris = _seed_partition(preview_tables)

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 200
        assert body["source_file"] == uris[LIVE_NEWEST]

    def test_a_skip_that_is_newest_by_time_is_walked_past(self, preview_tables):
        """**The case ordering does not fix** (do-NOT 10, R12.2).

        The live partition with its newest run removed, so the duplicate skip — which
        has zero state rows — is now the top row by ``started_at`` as well as by
        ``run_id``. Ordering has nothing left to correct here; only the walk can
        answer.
        """
        uris = _seed_partition(preview_tables, rows=LIVE_PARTITION[1:])

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 200
        assert body["source_file"] == uris[LIVE_NEXT_BELOW_SKIP]

    def test_the_pre_c12_ordering_would_have_returned_the_skip(self, preview_tables):
        """The defect, pinned as an ordering fact rather than as an anecdote.

        Descending on the base table's ``run_id`` sort key, the top row of the live
        partition is the skip. This is what ``get_latest_preview`` used to hand to
        ``get_data_preview``, and the assertion below is what makes the fixture's
        relevance checkable rather than asserted in a docstring.
        """
        _seed_partition(preview_tables)

        by_run_id = preview_tables["executions_table"].query(
            KeyConditionExpression=Key("pipeline_id").eq(PIPELINE_ID),
            ScanIndexForward=False,
            Limit=1,
        )

        assert by_run_id["Items"][0]["run_id"] == LIVE_SKIP

    def test_a_queued_placeholder_as_the_newest_row_is_walked_past(self, preview_tables):
        """R1.2 makes this reachable, so fixing the ordering *creates* this case.

        A ``QUEUED`` placeholder sorts last under ``run_id`` (``'q' < 'r' < 's'``) and
        was therefore never the top row before C1. Ordered by ``started_at`` it can be
        the newest event on the list — it was, at 13:27:46 on the afternoon this spec
        was written — and it has no state rows under its own ``queued-`` id, because
        the real run gets a different one.
        """
        uris = _seed_partition(preview_tables)
        preview_tables["executions_table"].put_item(
            Item=_exec_row("queued-ee4ce802f6a34f1c9d0e5b2a71c3d489", "2026-08-24T13:27:46.000000+00:00")
        )

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 200
        assert body["source_file"] == uris[LIVE_NEWEST], "the queued placeholder was newest by time and was taken"

    def test_several_consecutive_unpreviewable_rows_are_walked_past(self, preview_tables):
        """O14's motivating shape: a run of skips at the top of the history."""
        rows = (
            *((f"skipped-{n:032d}", f"2026-08-24T2{n}:00:00.000000+00:00", False) for n in range(6)),
            ("run-2026-08-20-17-52-21-248afa7a", "2026-08-20T17:52:21.739239+00:00", True),
        )
        uris = _seed_partition(preview_tables, rows=rows)

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 200
        assert body["source_file"] == uris["run-2026-08-20-17-52-21-248afa7a"]

    def test_a_row_whose_state_rows_carry_no_source_uri_is_walked_past(self, preview_tables):
        """Not every unpreviewable row is a skip.

        A run that failed before ``file_validation`` recorded a URI has state rows and
        no source. That is a fact about the row, so an older row may still be
        previewable — and a status-based predicate would have missed it entirely,
        which is the reason ``_run_names_a_source`` asks the resolver instead
        (task 1.4, R9.1).
        """
        uris = _seed_partition(preview_tables, rows=LIVE_PARTITION[2:3])
        preview_tables["executions_table"].put_item(
            Item=_exec_row("run-2026-08-24-14-00-00-deadbeef", "2026-08-24T14:00:00.000000+00:00")
        )
        preview_tables["state_table"].put_item(
            Item={"run_id": "run-2026-08-24-14-00-00-deadbeef", "step_name": "file_validation"}
        )

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 200
        assert body["source_file"] == uris[LIVE_NEXT_BELOW_SKIP]


# ===========================================================================
# Nothing previewable — the existing 404 (O14)
# ===========================================================================


class TestNothingPreviewable:
    def test_no_previewable_run_returns_the_existing_404(self, preview_tables):
        _seed_partition(preview_tables, rows=tuple((r, s, False) for r, s, _ in LIVE_PARTITION))

        response = dp.get_latest_preview(PIPELINE_ID, {})
        status, body = _resp(response)

        assert status == 404
        assert set(body) == {"error"}, "O14: the existing 404 body shape, not a new one"
        assert "previewable" in body["error"].lower()

    def test_an_empty_partition_keeps_its_own_404_message(self, preview_tables):
        """Unchanged from before C12, and distinct from the message above.

        "This pipeline has no runs" and "none of its runs can be previewed" are
        different facts, and an operator reading the panel deserves the difference.
        """
        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 404
        assert "no runs found" in body["error"].lower()

    def test_a_non_s3_source_stops_the_walk_and_keeps_its_own_404(self, preview_tables):
        """A JDBC pipeline must not spend the whole bound to reach the same answer.

        A non-S3 pseudo-URI is a fact about the pipeline's **source type**, so every
        older row says the same thing. The walk stops on it and ``get_data_preview``
        produces the message added for JDBC sources in ``5feb9b7647`` — which
        ``use-data-preview.ts:26-31`` already treats as "no data to preview".
        """
        preview_tables["executions_table"].put_item(
            Item=_exec_row("run-2026-08-24-13-48-26-4fccdf03", "2026-08-24T13:48:26.863631+00:00")
        )
        preview_tables["state_table"].put_item(
            Item={
                "run_id": "run-2026-08-24-13-48-26-4fccdf03",
                "step_name": "file_validation",
                "original_s3_uri": "jdbc://mysql/appdb/customers",
            }
        )
        _seed_partition(preview_tables, rows=LIVE_PARTITION[2:3])

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 404
        assert "not available for this source type" in body["error"]
        assert len(_index_reads(preview_tables)) == 1, "the walk stopped on the first row rather than continuing"

    def test_a_partition_absent_from_the_sparse_index_404s_rather_than_falling_back(self, preview_tables):
        """R9's accepted cost, and the fallback that is deliberately absent.

        A row with no ``started_at`` is not in the index, so it cannot be ordered.
        Reading the base table instead would reintroduce the ``run_id`` ordering this
        spec removes — the same refusal ``get_run_neighbours`` records — so the honest
        answer is the 404.
        """
        preview_tables["executions_table"].put_item(Item=_exec_row("run-with-no-started-at", None))
        _seed_previewable(preview_tables, "run-with-no-started-at")

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 404
        assert "no runs found" in body["error"].lower()


# ===========================================================================
# The bound holds, and it is asserted without sleeping
# ===========================================================================


class TestTheBoundHolds:
    def test_the_row_cap_stops_the_walk(self, preview_tables, monkeypatch):
        """Shortened rather than exercised at its real figure.

        Seeding 25 unpreviewable rows to make the real cap bind would make this test
        a slow proxy for a bound that is already visible. Lowering the cap tests the
        same branch.
        """
        monkeypatch.setattr(dp, "_MAX_PREVIEW_CANDIDATES", 2)
        rows = (
            *((f"skipped-{n:032d}", f"2026-08-24T2{n}:00:00.000000+00:00", False) for n in range(3)),
            ("run-2026-08-20-17-52-21-248afa7a", "2026-08-20T17:52:21.739239+00:00", True),
        )
        _seed_partition(preview_tables, rows=rows)

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 404
        assert "previewable" in body["error"].lower()
        assert len(_index_reads(preview_tables)) == 2, "the cap bounds the number of reads, not just the answer"

    def test_the_deadline_stops_the_walk(self, preview_tables, monkeypatch):
        """No ``sleep``: the budget is shortened, which is how tasks 3.3 and 4.3 do it."""
        monkeypatch.setattr(dp, "_PREVIEW_WALK_TIME_SHARE_SECONDS", 0.0)
        _seed_partition(preview_tables, rows=LIVE_PARTITION[1:])

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 404
        assert "previewable" in body["error"].lower()
        assert len(_index_reads(preview_tables)) == 1, "the first row is always examined; the bound stops continuing"

    def test_the_first_row_is_always_examined_even_at_a_zero_budget(self, preview_tables, monkeypatch):
        """The property ``_count_partition_rows`` and the 4.3 filter walk both hold.

        A zero budget must not mean "answer nothing"; it means "read one row and
        stop". Without this, a clock-starved request would 404 on a pipeline whose
        newest run is perfectly previewable.
        """
        monkeypatch.setattr(dp, "_PREVIEW_WALK_TIME_SHARE_SECONDS", 0.0)
        uris = _seed_partition(preview_tables)

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 200
        assert body["source_file"] == uris[LIVE_NEWEST]

    def test_the_bounds_are_the_documented_figures(self):
        """The reuse claim, made checkable.

        The share and the page-cap figure are the ones ``pipelines_runs.py`` already
        carries, so a change to either here should be a visible diff against them
        rather than a silent divergence. The **row cap** is deliberately NOT equal to
        ``_MAX_COUNT_PAGES``: this walk's cost is per row (one state read each), not
        per page, so its bound is scaled to that.
        """
        assert dp._PREVIEW_TIME_BUDGET_SECONDS == runs_handler._RUNS_TIME_BUDGET_SECONDS
        assert dp._PREVIEW_WALK_TIME_SHARE_SECONDS == runs_handler._FILTER_WALK_TIME_SHARE_SECONDS
        assert dp._MAX_PREVIEW_CANDIDATES < runs_handler._MAX_FILTER_PAGES

    def test_the_walk_reads_only_the_run_id_it_needs(self, preview_tables):
        """One row per read, projecting one attribute.

        ``Limit=1`` is what keeps the common case — no live partition in the validation account had
        a skip or a queued row newest by ``started_at`` on 2026-08-24, across all 24
        indexed partitions — costing exactly what the pre-C12 read cost. A page-sized
        read would charge it for rows it never looks at.
        """
        _seed_partition(preview_tables)
        dp.get_latest_preview(PIPELINE_ID, {})

        first = _index_reads(preview_tables)[0]
        assert first["Limit"] == 1
        assert first["ProjectionExpression"] == "run_id"
        assert first["ScanIndexForward"] is False


# ===========================================================================
# The index, not the table's sort key
# ===========================================================================


class TestTheIndexIsUsed:
    def test_the_walk_reads_the_index_and_never_the_base_table(self, preview_tables):
        _seed_partition(preview_tables)
        dp.get_latest_preview(PIPELINE_ID, {})

        reads = preview_tables["executions_table"].queries
        assert reads, "the walk issued no executions read at all"
        assert all(q.get("IndexName") == dp.RUNS_BY_TIME_INDEX for q in reads)

    def test_the_index_name_is_the_one_constant_both_readers_share(self):
        """One literal, two readers (task 5.6).

        ``data_preview.py`` and ``pipelines_runs.py`` must name the same index, and a
        mistyped ``IndexName`` is a ``ValidationException`` at request time rather than
        an import error — so a second spelling would fail silently through this
        module's ``except`` arm as a 404. ``test_run_history_ordering.py`` compares the
        constant against ``data-stack.ts``; this asserts there is only one of it.
        """
        assert dp.RUNS_BY_TIME_INDEX is common_handler.RUNS_BY_TIME_INDEX
        assert runs_handler.RUNS_BY_TIME_INDEX is common_handler.RUNS_BY_TIME_INDEX


# ===========================================================================
# do-NOT 10, mechanised
# ===========================================================================


class TestRemovingTheWalkBreaksIt:
    """The mutation test. **The single most important assertion in task 5.6.**

    do-NOT 10 says the walk is the load-bearing half and that ordering alone "will
    look correct in testing and fail in production". A prose warning cannot stop
    someone simplifying the walk away, so the mutant is built here and run against
    the endpoint: ordering fixed, walk removed, top row taken blindly. If a future
    change makes the mutant pass, this test fails and says why.
    """

    @staticmethod
    def _ordering_only(pipeline_id: str) -> tuple[str | None, int]:
        """C12 change (1) with change (2) removed — the tempting half-fix."""
        response = dp.executions_table.query(
            IndexName=dp.RUNS_BY_TIME_INDEX,
            KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items", [])
        return (items[0]["run_id"], 1) if items else (None, 0)

    def test_ordering_alone_fails_where_the_walk_succeeds(self, preview_tables, monkeypatch):
        uris = _seed_partition(preview_tables, rows=LIVE_PARTITION[1:])

        real_status, real_body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))
        assert real_status == 200
        assert real_body["source_file"] == uris[LIVE_NEXT_BELOW_SKIP]

        monkeypatch.setattr(dp, "_resolve_previewable_run", self._ordering_only)
        mutant_status, mutant_body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert mutant_status == 404, (
            "the ordering-only mutant returned a preview — the walk has stopped being "
            "load-bearing, or this fixture no longer puts an unpreviewable row on top"
        )
        assert LIVE_SKIP in mutant_body["error"], "the mutant 404'd for a different reason than the skip row"

    def test_ordering_alone_passes_on_the_live_partition(self, preview_tables, monkeypatch):
        """Why the mutation test needs its own fixture, stated as an assertion.

        On the live partition exactly as it stands, the newest row by time is a real
        run, so **the mutant passes**. That is do-NOT 10's warning reproduced: a test
        written against today's data would have ratified the half-fix. Pinning it here
        keeps the reason the harder fixture exists from being optimised away.
        """
        uris = _seed_partition(preview_tables)
        monkeypatch.setattr(dp, "_resolve_previewable_run", self._ordering_only)

        status, body = _resp(dp.get_latest_preview(PIPELINE_ID, {}))

        assert status == 200
        assert body["source_file"] == uris[LIVE_NEWEST]


# ===========================================================================
# The exception that carries the distinction
# ===========================================================================


class TestSourceNotPreviewableError:
    def test_it_is_a_not_found_error_so_existing_callers_are_unchanged(self):
        """``get_data_preview``'s ``except NotFoundError`` must still catch it.

        The subclass exists only so the walk can tell "no source recorded" from "not
        an S3 source" without reading the message text. It must not change the
        per-run route's status code or its wording.
        """
        assert issubclass(dp.SourceNotPreviewableError, dp.NotFoundError)

    def test_a_non_s3_uri_raises_the_subclass(self, preview_tables):
        preview_tables["state_table"].put_item(
            Item={
                "run_id": "run-jdbc",
                "step_name": "file_validation",
                "original_s3_uri": "jdbc://mysql/appdb/customers",
            }
        )

        with pytest.raises(dp.SourceNotPreviewableError):
            dp._resolve_source_file("run-jdbc")

    def test_a_missing_run_raises_the_base_class_only(self, preview_tables):
        """The two must not collapse: this one means "keep walking"."""
        with pytest.raises(dp.NotFoundError) as caught:
            dp._resolve_source_file("no-such-run")

        assert not isinstance(caught.value, dp.SourceNotPreviewableError)
