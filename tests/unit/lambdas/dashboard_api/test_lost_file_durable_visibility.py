# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A Lost_File stays readable for as long as its record exists — not for an hour.

p54 R10.3 asks that a dead-lettered upload be "readable for longer than the window in
which the loss occurred". That held in DynamoDB — the record carries a 30-day TTL — and
failed on the dashboard, because the only reader was ``queued_files``, which comes from
the FIRST page of a newest-first index. A loss row's ``started_at`` is the instant the
loss was *recorded*, so it enters at the top of that index and is pushed off page 1 by
newer runs within about an hour. The condition that creates a lost file (a busy pipeline
holding an approval lock) is the same condition that buries the evidence.

**Expectations here are derived from that requirement, not from what the code returns.**
The load-bearing test is
``test_a_loss_older_than_page_one_is_still_reported``: the page query answers with rows
that do NOT include the loss, which is exactly what an active pipeline's page 1 looks
like an hour after the event, and the response must still name the file. A test written
from current output could not distinguish the fix from the defect, because on page 1 both
report the loss.

Validates: p54 Requirements 10.3
"""

import json
import os
import sys
from typing import Any

import pytest
from boto3.dynamodb.conditions import Key

# Condition expressions are compared by structure, never by ``repr()`` — see that
# module's docstring, and the comment at the assertion in
# ``TestTheReadIsKeyedAndCannotBecomeA500``.
from tests.fixtures.dynamodb_conditions import condition_literals, flatten_condition

#: ``_QUEUED_ROW_ID_PREFIX`` in ``handlers/pipelines_runs.py`` — spelled here rather
#: than imported so the test states the prefix the requirement names instead of
#: agreeing with the handler about whatever it currently is.
QUEUED_ROW_ID_PREFIX = "queued-"

# ---------------------------------------------------------------------------
# Path / environment setup — the sibling modules' preamble, unchanged.
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

PIPELINE_ID = "pipelines/lost-file-visibility"
LOST_KEY = f"{PIPELINE_ID}/customer_data_7.csv"
LOST_REASON = (
    "The upload's SQS message exhausted its redelivery budget before the pipeline lock "
    "became free, so SQS moved it to the dead-letter queue and no run was started for "
    "this file."
)


def _run_row(run_id: str, started_at: str) -> dict[str, Any]:
    """An ordinary finished run. ``current_step`` keeps it out of the phantom filter."""
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": "SUCCEEDED",
        "started_at": started_at,
        "current_step": "run_pipeline",
    }


def _lost_row(source_key: str = LOST_KEY, *, recorded_at: str = "2026-08-30T04:20:00+00:00") -> dict[str, Any]:
    """The Lost_File record, as ``queue_loss_recorder.record_lost_file`` writes it.

    ``status`` is ``"QUEUED"`` and the loss rides on ``queue_outcome`` — ``LGR-21a1b5b-2``
    / ``LGR-e5e9edd-2`` refuse a fourth executions status, and this fixture must not be
    the place that quietly invents one. ``started_at`` and ``lost_at`` carry the same
    instant, which is what the producer does and what makes the reader's fallback between
    them unable to disagree.
    """
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": "queued-1f2e3d4c5b6a7988",
        "status": "QUEUED",
        "file_name": source_key.rsplit("/", 1)[-1],
        "source_key": source_key,
        "started_at": recorded_at,
        "current_step": "queued_dlq",
        "queue_outcome": "lost_to_dlq",
        "lost_reason": LOST_REASON,
        "lost_at": recorded_at,
    }


def _deferred_row(source_key: str) -> dict[str, Any]:
    """A Queued_Placeholder — waiting, not lost. Carries no ``queue_outcome``."""
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": "queued-aaaabbbbccccdddd",
        "status": "QUEUED",
        "file_name": source_key.rsplit("/", 1)[-1],
        "source_key": source_key,
        "started_at": "2026-08-30T05:00:00+00:00",
        "current_step": "queued_trigger",
    }


class _FakeExecutionsTable:
    """An executions table that tells the three reads apart, the way DynamoDB does.

    ``get_pipeline_executions`` issues three different Queries against this table and a
    ``MagicMock`` cannot distinguish them — it would answer the keyed lost-file read with
    the page's rows and apply no ``FilterExpression`` at all. Dispatching on the kwargs
    is what makes the assertions below statements about the handler rather than about the
    mock:

    * ``Select="COUNT"``     → the ``total_count`` walk;
    * no ``IndexName``       → the lost-file read, which is a BASE-TABLE keyed query;
    * otherwise             → the page read on ``RunsByTimeIndex``.

    The absence of ``IndexName`` is itself the discriminator, and that is deliberate: it
    is the property that keeps the lost-file array off the sparse index, so a row with no
    ``started_at`` could not be silently invisible (invariant ``LGR-7d19eca-2``, do-NOT
    2 / R9.4 — sparseness is not a filter).
    """

    def __init__(self, page_rows, lost_rows, *, count: int | None = None, lost_raises: bool = False):
        self.page_rows = page_rows
        self.lost_rows = lost_rows
        self.count = len(page_rows) if count is None else count
        self.lost_raises = lost_raises
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("Select") == "COUNT":
            return {"Count": self.count}
        if "IndexName" not in kwargs:
            if self.lost_raises:
                raise RuntimeError("simulated DynamoDB failure on the lost-file read")
            return {"Items": list(self.lost_rows)}
        return {"Items": list(self.page_rows)}

    @property
    def lost_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if "IndexName" not in c and c.get("Select") != "COUNT"]


class _EmptyStateTable:
    """No ``schema_extract`` history; the schema walk is not this file's subject."""

    def query(self, **kwargs):
        return {"Items": []}


@pytest.fixture
def call_handler(monkeypatch):
    """Return a callable that runs ``get_pipeline_executions`` against a fake table."""
    from handlers import pipelines_runs as runs

    def _call(page_rows, lost_rows, **table_kwargs):
        table = _FakeExecutionsTable(page_rows, lost_rows, **table_kwargs)
        monkeypatch.setattr(runs, "executions_table", table)
        monkeypatch.setattr(runs, "state_table", _EmptyStateTable())
        # No cost / load_outcome enrichment: it reads a different table and would
        # otherwise need its own fake. Its own ``try`` already degrades it.
        monkeypatch.setattr(runs, "dynamodb", None)
        response = runs.get_pipeline_executions(PIPELINE_ID)
        return table, response["statusCode"], json.loads(response["body"])

    return _call


class TestTheLossOutlivesThePage:
    """R10.3 — the record is 30 days; page 1 is about an hour."""

    def test_a_loss_older_than_page_one_is_still_reported(self, call_handler):
        """**The defect, stated as a test.**

        The page read answers with ten newer runs and no queued rows at all — which is
        precisely what ``RunsByTimeIndex`` page 1 looks like an hour after a loss on an
        active pipeline. ``queued_files`` is therefore empty, correctly, and the response
        must still name the lost file.
        """
        page = [_run_row(f"run-{i:02d}", f"2026-08-30T1{i}:00:00+00:00") for i in range(10)]
        _table, status, body = call_handler(page, [_lost_row()])

        assert status == 200
        assert body["queued_files"] == [], (
            "the premise of this test is that the loss has fallen off page 1; if queued_files "
            "carries it, the fixture is not exercising the defect"
        )
        assert [entry["file_name"] for entry in body["lost_files"]] == ["customer_data_7.csv"]
        assert body["lost_files"][0]["source_key"] == LOST_KEY

    def test_the_array_is_present_and_empty_when_nothing_was_lost(self, call_handler):
        """``[]`` rather than an omitted key, so the envelope's key set is assertable.

        The same reason ``partial_counts`` and ``next_cursor`` are always present
        (``LGR-9a35b2d-3``: "one answer is assertable with ``==``; two are not").
        """
        _table, status, body = call_handler([_run_row("run-01", "2026-08-30T10:00:00+00:00")], [])

        assert status == 200
        assert body["lost_files"] == []
        assert "lost_files" in body

    def test_the_reason_and_the_recorded_instant_travel_with_the_entry(self, call_handler):
        """R10.3 wants the file **and the reason**, so both must reach the wire."""
        _table, _status, body = call_handler([], [_lost_row()])

        entry = body["lost_files"][0]
        assert entry["lost_reason"] == LOST_REASON
        assert entry["lost_at"] == "2026-08-30T04:20:00+00:00"

    def test_the_entry_carries_no_run_id(self, call_handler):
        """do-NOT 3 / do-NOT 15 / p48 O2 — the row's real ``run_id`` 404s on run details.

        A placeholder row has no state rows at all, so ``get_run_details`` on its id is a
        404. Publishing the id would hand the frontend something that looks navigable and
        is not, which is R6's defect reintroduced by R10's fix.
        """
        _table, _status, body = call_handler([], [_lost_row()])

        assert set(body["lost_files"][0]) == {"file_name", "source_key", "lost_at", "lost_reason"}

    def test_newest_first(self, call_handler):
        """The sort key is a content hash, so order has to be imposed after the read."""
        older = _lost_row("pipelines/lost-file-visibility/a.csv", recorded_at="2026-08-01T00:00:00+00:00")
        older["run_id"] = "queued-0000000000000001"
        newer = _lost_row("pipelines/lost-file-visibility/b.csv", recorded_at="2026-08-30T00:00:00+00:00")
        newer["run_id"] = "queued-0000000000000002"

        _table, _status, body = call_handler([], [older, newer])

        assert [entry["file_name"] for entry in body["lost_files"]] == ["b.csv", "a.csv"]


class TestOnlyALossIsReportedAsOne:
    """R9.5's three states — a deferred file is not a lost one."""

    def test_a_deferred_placeholder_never_appears_in_lost_files(self, call_handler):
        """The row is in the placeholder namespace but carries no ``queue_outcome``.

        The server-side ``FilterExpression`` would drop it against real DynamoDB; the
        Python predicate is what makes the answer independent of the filter having been
        applied, and this is the test that would fail if the predicate were dropped as
        redundant.
        """
        deferred = _deferred_row("pipelines/lost-file-visibility/still_waiting.csv")
        _table, _status, body = call_handler([deferred], [deferred])

        assert body["lost_files"] == []
        assert [entry["status"] for entry in body["queued_files"]] == ["queued"]

    def test_an_unrecognised_queue_outcome_is_not_a_loss(self, call_handler):
        """Exact match, no normalising (``LGR-ad78946-1``).

        One producer writes this attribute, so a value it did not write is not evidence
        of loss — claiming loss on it would be the invented fact R9.5's three-state
        discipline exists to prevent.
        """
        odd = _lost_row()
        odd["queue_outcome"] = "Lost_To_DLQ"
        _table, _status, body = call_handler([], [odd])

        assert body["lost_files"] == []


class TestTheReadIsKeyedAndCannotBecomeA500:
    """How the read is shaped, and how it fails."""

    def test_it_is_a_base_table_keyed_query_on_the_placeholder_prefix(self, call_handler):
        """Not the sparse index, and not a partition-wide filter.

        Two properties in one assertion, both load-bearing. **No ``IndexName``**: the
        read goes to the base table, so it cannot inherit ``RunsByTimeIndex``' sparseness
        and a loss record is visible on the strength of its key alone (``LGR-7d19eca-2``,
        do-NOT 2). **``begins_with`` on the sort key**: the placeholder namespace is
        content-addressed under one prefix by both writers, so DynamoDB reads the
        matching range rather than reading the partition and discarding — which is what
        answers ``LGR-e5e9edd-2``'s ``revisit_when`` ("a reader needs to filter lost files
        server-side, which an attribute outside the key cannot do efficiently") without
        adding a key or an index.
        """
        table, _status, _body = call_handler([], [_lost_row()])

        assert len(table.lost_calls) == 1, f"expected one keyed lost-file read, got {table.lost_calls}"
        call = table.lost_calls[0]
        assert "IndexName" not in call
        # Compared STRUCTURALLY, not as a string. ``get_expression()`` returns
        # ``{"format", "operator", "values"}`` and ``values`` holds further condition
        # objects carrying the default ``repr`` (``<…conditions.BeginsWith object at
        # 0x…>``), so neither ``run_id`` nor ``"queued-"`` ever appears in ``repr()`` of
        # it — the ``assert "queued-" in repr(...)`` this assertion used to be could not
        # pass however correct the query was. See ``tests/fixtures/dynamodb_conditions.py``.
        expected = Key("pipeline_id").eq(PIPELINE_ID) & Key("run_id").begins_with(QUEUED_ROW_ID_PREFIX)
        actual = flatten_condition(call["KeyConditionExpression"])
        assert actual == flatten_condition(expected), (
            f"expected a partition-key equality AND a begins_with on the placeholder prefix, got {actual}"
        )
        assert QUEUED_ROW_ID_PREFIX in condition_literals(call["KeyConditionExpression"]), (
            "the prefix the placeholder namespace is content-addressed under must reach the query"
        )

    def test_a_failed_lost_read_degrades_to_an_empty_array_and_a_marker(self, call_handler):
        """A read failure must never become a 500, and must never look like "none".

        The response stays 200 with the page's rows intact — the outer arm would have
        discarded them and rendered "this pipeline has no runs" — and ``partial_counts``
        names ``lost_files`` so an empty array is not read as "nothing was lost".
        ``LGR-b2f79ef-14``'s reasoning: an unmarked could-not-check reported as
        checked-and-found-none is the defect.
        """
        page = [_run_row("run-01", "2026-08-30T10:00:00+00:00")]
        _table, status, body = call_handler(page, [], lost_raises=True)

        assert status == 200
        assert len(body["executions"]) == 1, "a lost-read failure must not cost the page its rows"
        assert body["lost_files"] == []
        assert "lost_files" in body["pagination"]["partial_counts"]

    def test_a_complete_read_names_nothing(self, call_handler):
        """``partial_counts`` stays ``[]`` when every read answered in full."""
        _table, _status, body = call_handler([_run_row("run-01", "2026-08-30T10:00:00+00:00")], [_lost_row()])

        assert body["pagination"]["partial_counts"] == []
