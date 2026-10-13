# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run history is ordered by event time, not by ``run_id`` (p48 C1, task 2.2).

WHY THIS IS A SEPARATE FILE FROM ``test_get_pipeline_executions.py``
--------------------------------------------------------------------
That file drives the handler through an autouse ``MagicMock`` executions table, so
``query`` returns whatever list the test hands it. It can pin the *call* — and it
does: ``TestScanIndexForward`` asserts ``ScanIndexForward is False``, and task 2.2
added ``IndexName`` beside it. It cannot pin the *ordering*, because a mock does no
sorting; a test written there would assert that a list the test itself ordered came
back ordered. The defect this file guards against is precisely that the sort key
was the wrong attribute, so the sort has to be performed by something other than
the test. Hence moto, with ``RunsByTimeIndex`` on the real key schema.

THE DEFECT, IN THE OPERATOR'S TERMS
-----------------------------------
An operator reported that ``latesttesting`` skipped ``customer_data_3.csv`` and
that skipped rows "always stay on top". They did, permanently. The executions
table's sort key is ``run_id`` and SIX id prefixes share it, so a descending read
groups BY KIND — descending, that order is
``unknown`` > ``skipped-`` > ``run-`` > ``retry-`` > ``queued-`` > ``file-`` >
``e2e-``. Every duplicate skip therefore pinned above every run for as long as the
row lived, and the row lives forever: the duplicate-skip id is deterministic in the
content fingerprint (``pipeline_trigger/lambda_function.py:189``), so it never ages
out and never re-sorts.

``LGR-21a1b5b-4`` (**template / ratified**) named this exact defect when it
rejected the ``skipped-`` prefix for the contention-skip id, and its
``revisit_when`` is "the run-history query stops paging by run id" — which C1
satisfies. It is **not** superseded (p48 R7.2).

THE FIXTURE IS LIVE DATA, NOT INVENTED DATA
-------------------------------------------
``LIVE_PARTITION`` below is ``latesttesting-19ca826a`` as measured against
the validation account / us-west-2 on 2026-08-24 (``requirements.md`` §Introduction, re-measured
at task 2.1). Both orderings in this file were confirmed against the live table
before being asserted here, so a failure means the handler diverged from observed
DynamoDB behaviour rather than from a guess.

WHAT IS DELIBERATELY *NOT* ASSERTED HERE
----------------------------------------
* **The status filter's own ``partial_counts`` entry.** Task 4.3 adds it. C3's two
  count keys are asserted below; nothing here may be read as pinning the list to
  exactly those two for ever.
* **The cursor codec's own edge cases.** ``tests/unit/lambdas/dashboard_api/
  test_run_cursor.py`` owns ``encode_cursor`` / ``decode_cursor`` in isolation,
  including every rejection path. ``TestCursorPaging`` below asserts the handler's
  *behaviour* through them, not their internals.

* **Sparseness.** ``tests/unit/lambdas/test_executions_index_sparseness.py`` owns
  R9, including ``TestSparsenessIsNotAFilter`` — the proof that index membership is
  NOT the phantom-row predicate (p48 do-NOT 2, R9.4). Nothing here may be read as
  licence to filter on sparseness.

WHAT TASK 3.3 ADDED
-------------------
The exact total (design C3). ``total_count`` was ``len(executions)`` and
``total_pages`` the literal ``1``; both now come from a windowless
``Select="COUNT"`` walk over the same index, bounded by a page cap and a wall-clock
deadline, with ``partial_counts`` naming both keys when a bound binds. That is
``LGR-9a35b2d-3``'s shape reused rather than a new mechanism, so
``TestTheBoundBinds`` pins the figures against ``stats.py``'s own.

Two existing assertions were **amended in place** because the second query per
request makes them false while the properties they defend still hold —
``test_one_page_read_per_request_however_deep_the_page`` (renamed) and
``test_get_queued_files_passes_no_cursor``. Both now partition the recorded calls by
kind via :func:`_page_reads` / :func:`_count_reads`. See the first for why the
scoped form is a *sharper* instrument against Arm B than the old total was.

WHAT TASK 3.2 ADDED
-------------------
Keyed paging (design C2 **Arm A**, chosen by the user at task 3.1). ``page`` was in
the signature and referenced nowhere in the body, so every page request returned
page 1; ``test_adjacent_pages_neither_duplicate_nor_omit`` was skipped at 2.2 with
task 3.2 named, because a two-page test would have asserted page 2 equalled page 1
and passed. It is unskipped and implemented below, and ``TestCursorPaging`` carries
the round trip, the last page, the page label and the rejection arms. CP2 — the
same property over generated time mixes and page sizes — is in
``test_run_history_paging_prop.py``.

FIXTURES
--------
Shared fixtures only — ``mock_dynamodb_tables`` from ``tests/fixtures/conftest.py``.
No ``moto.mock_aws()`` and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``). Table names are set at module
scope because ``handlers/common.py`` calls ``require_env`` at **import** time,
before any fixture runs — the same reason
``test_stats_characterization.py`` does it there.

Validates: p48 Requirements 1.1, 1.2, 1.3, 1.4, 1.5
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any

import boto3
import pytest
from boto3.dynamodb.conditions import Attr

# ``FilterExpression`` / ``KeyConditionExpression`` are compared STRUCTURALLY, never as
# strings: ``ConditionBase.get_expression()`` returns a dict whose ``values`` hold
# further condition objects with the default ``repr``, so no attribute name and no
# compared literal ever appears in ``repr()`` of it. See the module docstring of
# ``tests/fixtures/dynamodb_conditions.py`` for the full account of the trap.
from tests.fixtures.dynamodb_conditions import flatten_condition

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
from handlers import pipelines_runs as runs_handler

EXECUTIONS_TABLE = "agentic_data_ingestion_executions"
STATE_TABLE = "agentic_data_ingestion_state"
MOTO_REGION = "us-east-1"  # matches conftest._create_moto_dynamodb_tables

PIPELINE_ID = "latesttesting-19ca826a"

#: ``latesttesting-19ca826a`` as it stands in the validation account / us-west-2, measured
#: 2026-08-24. ``(run_id, started_at, status)``, listed here in **run_id
#: descending** order — i.e. the order the handler returned BEFORE this change, and
#: the order the operator reported. The two skips on top are the complaint.
#:
#: The ``queued-ee4ce802…`` row from ``requirements.md`` §Introduction is absent:
#: queued placeholders are deleted on processing by ``_remove_queued_file`` and
#: carry a 1-hour TTL, so by the task 2.1 re-measure it was gone. That is why R1.2
#: is fixture-based below rather than asserted against this partition.
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

#: The same nine rows in ``started_at`` descending order — confirmed by querying
#: ``RunsByTimeIndex`` against the validation account directly, not derived by sorting the tuple
#: above in Python. This is the order the handler must now return.
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

#: The operator's case, isolated. The older skip sat at position 2 under ``run_id``
#: and must sit BELOW the newer run it predates by two days.
OLDER_SKIP = "skipped-42e546628820e9630a45c6bcfd8792e1"
NEWER_RUN = "run-2026-08-22-12-43-57-f009d524"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def runs_tables(mock_dynamodb_tables, monkeypatch) -> dict[str, Any]:
    """Bind moto handles onto the handler's namespace.

    ``handlers/pipelines_runs.py`` does ``from .common import executions_table,
    state_table, dynamodb``, which copies the references into its own namespace, so
    patching ``common`` alone would patch names the handler never reads. Both are
    patched so a future change to either access shape cannot silently un-test this
    file.

    ``dynamodb`` is rebuilt inside the fixture rather than reused from import time:
    moto intercepts at the botocore layer, so the module-level resource created
    before ``mock_aws()`` activated is not moto-backed and its
    ``batch_get_item`` would reach real AWS.
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


def _row(run_id: str, started_at: str, status: str, **extra: Any) -> dict[str, Any]:
    """An executions row shaped like the live ones.

    ``current_step`` is always set because both skip records carry it — the
    duplicate-content skip writes ``current_step="duplicate_check"`` — so neither
    is a Phantom_Execution_Row and both must survive
    ``is_phantom_execution_row`` (``handlers/common.py:59-79``, which documents
    that this is deliberate). Seeding rows without it would make every assertion
    below pass for the wrong reason: the rows would be filtered out, not ordered.
    """
    row: dict[str, Any] = {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "started_at": started_at,
        "status": status,
        "current_step": "duplicate_check" if run_id.startswith("skipped-") else "run_pipeline",
    }
    if not run_id.startswith("skipped-"):
        row["steps_status"] = {"run_pipeline": status.lower()}
    row.update(extra)
    return row


def _seed(table: Any, rows: tuple[tuple[str, str, str], ...] = LIVE_PARTITION) -> None:
    for run_id, started_at, status in rows:
        table.put_item(Item=_row(run_id, started_at, status))


def _raw_call(
    page_size: int = 10,
    cursor: str | None = None,
    page: int = 1,
    pipeline_id: str = PIPELINE_ID,
    status: str | None = None,
) -> dict[str, Any]:
    """The handler's raw response, status code included.

    Separate from :func:`_call` because task 3.2 introduced a **400** arm — a
    malformed or foreign cursor — and a helper that asserts 200 cannot see it. Task
    4.3 added two more 400 arms on ``status``, so the separation earns its keep twice
    over.
    """
    return runs_handler.get_pipeline_executions(pipeline_id, page, page_size, cursor=cursor, status=status)


def _call(
    page_size: int = 10,
    cursor: str | None = None,
    page: int = 1,
    pipeline_id: str = PIPELINE_ID,
    status: str | None = None,
) -> dict[str, Any]:
    response = _raw_call(page_size=page_size, cursor=cursor, page=page, pipeline_id=pipeline_id, status=status)
    assert response["statusCode"] == 200, response
    return json.loads(response["body"])


def _run_ids(body: dict[str, Any]) -> list[str]:
    return [e["run_id"] for e in body["executions"]]


def _page_reads(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The recorded queries that read a page of rows (task 3.3).

    Since C3 the handler issues **two** kinds of query per request and they must be
    told apart before any call count means anything: a page read carries ``Limit``
    and no ``Select``; a count read carries ``Select="COUNT"`` and no ``Limit``.
    Partitioning on ``Select`` rather than on ``Limit`` because ``Select`` is the
    attribute that decides whether items come back at all.

    **Amended for p54 R10.3's lost-file read, which is a THIRD kind.** That read is a
    keyed query on the **base table** — no ``IndexName`` and no ``Select`` — so
    partitioning on ``Select`` alone would have counted it as a page read and every
    call count below would have shifted by one. ``IndexName`` is the discriminator
    because it is the property that distinguishes the two: both page reads and count
    reads name ``RunsByTimeIndex``, and the lost-file read deliberately names no index
    at all so it cannot inherit that index's sparseness (``LGR-7d19eca-2``, do-NOT 2).
    Amended rather than loosened, so the three kinds stay disjoint and exhaustive —
    which :meth:`TestCursorPaging.test_one_page_read_per_request_however_deep_the_page`
    asserts directly.
    """
    return [c for c in calls if c.get("Select") != "COUNT" and "IndexName" in c]


def _count_reads(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The recorded ``Select="COUNT"`` queries — C3's total (task 3.3)."""
    return [c for c in calls if c.get("Select") == "COUNT"]


def _lost_reads(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The recorded lost-file reads — p54 R10.3's keyed base-table query.

    Identified by the absence of ``IndexName``, which is the read's defining property
    rather than an incidental one: it goes to the base table so a Lost_File record is
    reachable on the strength of its key alone.
    """
    return [c for c in calls if c.get("Select") != "COUNT" and "IndexName" not in c]


def _b64(payload: str) -> str:
    """Hand-build a cursor-shaped token, bypassing ``run_cursor.encode_cursor``.

    Deliberately not the real encoder: these are the strings an attacker or a buggy
    client submits, and a test that could only produce well-formed cursors could not
    exercise a single rejection path.
    """
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


# ===========================================================================
# R1.1 — the regression the operator reported
# ===========================================================================
class TestSkipsNoLongerPinToTheTop:
    def test_an_older_skip_sorts_below_a_newer_run(self, runs_tables):
        """The operator's report, as an assertion (p48 R1.1).

        ``skipped-42e54662…`` started 2026-08-20T20:51:59 and
        ``run-2026-08-22-12-43-57-f009d524`` started two days later. Under the
        base table's ``run_id`` sort key the skip was at position 2 and the run at
        position 5; ordering by ``started_at`` puts the skip at position 6, below
        the run. Both positions were confirmed against the validation account.
        """
        _seed(runs_tables["executions_table"])

        order = _run_ids(_call())

        assert order.index(OLDER_SKIP) > order.index(NEWER_RUN), (
            f"the 08-20 skip must sort below the 08-22 run; got {order}"
        )
        assert order.index(OLDER_SKIP) == 5, "position 6 of 9, as measured live through RunsByTimeIndex"

    def test_the_whole_partition_matches_the_live_index_order(self, runs_tables):
        """All nine rows, against the order the live index actually returns.

        Stronger than the single pair above and worth having both: the pair states
        the operator's case, this states that nothing else moved in the process.
        """
        _seed(runs_tables["executions_table"])

        assert _run_ids(_call()) == list(EXPECTED_BY_TIME)

    def test_prefix_ordering_would_have_failed_this(self, runs_tables):
        """Guards against the test passing for the wrong reason.

        If the handler ever reverts to the base table, the assertions above must
        fail rather than coincide. They can only coincide if the two orderings are
        the same — so assert here that they are genuinely different, using the
        real ids. This is a property of the fixture data, not of the handler, and
        that is the point: it proves the fixture can distinguish the two.
        """
        by_run_id = [run_id for run_id, _, _ in LIVE_PARTITION]

        assert by_run_id != list(EXPECTED_BY_TIME)
        assert by_run_id.index(OLDER_SKIP) == 1, "position 2 of 9 under run_id — the reported symptom"


# ===========================================================================
# R1.4 — the ordering attribute is ``started_at``, and only that
# ===========================================================================
class TestOrderingAttribute:
    def test_the_query_goes_through_the_time_index(self, runs_tables, monkeypatch):
        """R1.4 stated rather than left implicit, at the call.

        A GSI sorts on exactly one attribute, so ``started_at`` is not a
        preference among the ``last_skipped_at → started_at → created_at`` chain
        an in-memory sort could have expressed — it is the only reachable option.
        Asserted at the call site so that a future change to a different index, or
        back to the base table, is visible here and not only in an order
        comparison.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        recorded: list[dict[str, Any]] = []
        original = table.query

        def _spy(**kwargs: Any) -> Any:
            recorded.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(table, "query", _spy)

        _call()

        assert recorded, "the handler must query the executions table"
        assert recorded[0]["IndexName"] == "RunsByTimeIndex"
        assert recorded[0]["ScanIndexForward"] is False
        assert recorded[0]["Limit"] == 10

    def test_started_at_descends_monotonically(self, runs_tables):
        """The returned order is the ``started_at`` order, checked on the values.

        Reading the timestamps off the response rather than comparing ids catches
        the case where the ids happen to be in the right order for another reason.
        """
        _seed(runs_tables["executions_table"])

        timestamps = [e["started_at"] for e in _call()["executions"]]

        assert timestamps == sorted(timestamps, reverse=True)

    def test_a_duplicate_skip_sorts_at_its_real_skip_instant(self, runs_tables):
        """R1.4: a duplicate skip's ``started_at`` IS the skip instant.

        ``skipped-7412636f…`` carries ``2026-08-24T13:04:04``, four minutes after
        the 13:00-hour run it duplicated and 44 minutes before the 13:48 run — so
        it must land between them. Verified against the live row.

        A **contention** skip is the documented exception: its ``started_at`` is
        the collapse-window start rather than the skip instant, a cost
        ``LGR-21a1b5b-4`` accepted explicitly when it shaped that id like a run
        id. R1.4 inherits that cost; it is not asserted away here.
        """
        _seed(runs_tables["executions_table"])

        order = _run_ids(_call())
        newest_skip = "skipped-7412636fccb938719673179658c05773"

        assert order.index("run-2026-08-24-13-48-26-4fccdf03") < order.index(newest_skip)
        assert order.index(newest_skip) < order.index("run-2026-08-24-13-04-01-421fc544")


# ===========================================================================
# R1.2 — the queued placeholder sorts by its own time
# ===========================================================================
class TestQueuedPlaceholderSortsByTime:
    """R1.2, and it can only be tested against a fixture.

    Task 1.3 established that **no ``QUEUED`` row exists anywhere in the live
    table**: placeholders are deleted on processing by ``_remove_queued_file`` and
    carry a 1-hour TTL, so the ``queued-ee4ce802…`` row in ``requirements.md``
    §Introduction was gone by the time the partition was re-measured. There is
    therefore no live observation to assert against and these rows are seeded.
    """

    #: The queued row from ``requirements.md`` §Introduction, at its observed time.
    #: 13:27:46 sits between the 13:48 run and the 13:04 skip — second newest.
    QUEUED = ("queued-ee4ce8021b4d4c7a", "2026-08-24T13:27:46.000000+00:00", "QUEUED")

    def _seed_with_queued(self, table: Any) -> None:
        _seed(table)
        run_id, started_at, status = self.QUEUED
        table.put_item(
            Item=_row(
                run_id,
                started_at,
                status,
                current_step="queued_trigger",
                file_name="customer_data_6.csv",
                source_key="uploads/customer_data_6.csv",
            )
        )

    def test_a_queued_row_is_read_on_a_page_prefix_ordering_would_have_excluded(self, runs_tables):
        """The defect R1.2 names: ``Limit`` used to exclude every queued row.

        ``queued-`` sorts below ``run-`` and ``skipped-``, so under the base table
        a ``Limit=3`` read returned the two skips and the newest run and the queued
        row was never read at all — ``queued_files`` came back empty while a file
        was genuinely queued. Ordering by ``started_at`` puts this row second
        newest, so it is on the page.

        ``page_size=3`` is what makes this observable; at the default 10 the whole
        ten-row partition fits and ``Limit`` excludes nothing.
        """
        self._seed_with_queued(runs_tables["executions_table"])

        body = _call(page_size=3)

        assert [q["file_name"] for q in body["queued_files"]] == ["customer_data_6.csv"]
        # The other two of the three rows read are the 13:48 run and the 13:04
        # skip — the queued row consumed the middle slot, by time.
        assert _run_ids(body) == [
            "run-2026-08-24-13-48-26-4fccdf03",
            "skipped-7412636fccb938719673179658c05773",
        ]

    def test_the_queued_split_still_keeps_it_out_of_executions(self, runs_tables):
        """do-NOT 4 / R3.3: the split's position and behaviour are unchanged.

        The ``QUEUED`` split runs on the raw page and the phantom filter runs on
        the post-split ``executions`` list — that relative order is deliberate and
        defensive (``LGR-21a1b5b-2``'s reasoning, plus the in-code fence at
        ``data-stack.ts``). Changing the query must not have moved either, so
        assert the observable consequence: a ``QUEUED`` row appears in
        ``queued_files`` and never in ``executions``.
        """
        self._seed_with_queued(runs_tables["executions_table"])

        body = _call()

        assert self.QUEUED[0] not in _run_ids(body)
        assert len(body["queued_files"]) == 1
        assert body["queued_files"][0]["queued_at"] == self.QUEUED[1]
        assert body["queued_files"][0]["status"] == "queued"

    def test_get_queued_files_still_delegates(self, runs_tables):
        """do-NOT 3: ``get_queued_files`` calls this function with defaults."""
        self._seed_with_queued(runs_tables["executions_table"])

        response = runs_handler.get_queued_files(PIPELINE_ID)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert body["count"] == 1
        assert body["queued_files"][0]["file_name"] == "customer_data_6.csv"


# ===========================================================================
# R1.3 / CP1 — stability
# ===========================================================================
class TestOrderIsStable:
    def test_repeated_reads_return_the_same_order(self, runs_tables):
        """CP1: deterministic across repeated reads.

        Asserted as determinism rather than as a tie-break rule, per design
        §Data Models: DynamoDB does not document an ordering for items with equal
        sort keys, and two rows sharing a microsecond-precision ``started_at`` is
        not a case worth engineering for.
        """
        _seed(runs_tables["executions_table"])

        assert _run_ids(_call()) == _run_ids(_call()) == _run_ids(_call())

    def test_a_short_page_is_a_prefix_of_the_full_order(self, runs_tables):
        """The property that makes a page boundary unable to duplicate or drop.

        The order is **total** — every row has a distinct ``started_at`` and the
        index sorts on it — so a page of ``k`` rows is the first ``k`` of the same
        sequence for every ``k``. Duplication or omission at a boundary requires
        the underlying order to differ between reads, which
        ``test_repeated_reads_return_the_same_order`` rules out.

        This is what is assertable at task 2.2. It is necessary for R1.3 and not
        sufficient — see the skipped test below.
        """
        _seed(runs_tables["executions_table"])
        full = _run_ids(_call(page_size=10))

        for k in (1, 2, 3, 5, 8):
            assert _run_ids(_call(page_size=k)) == full[:k], f"page_size={k} is not a prefix of the full order"

    def test_adjacent_pages_neither_duplicate_nor_omit(self, runs_tables):
        """R1.3 in full: page 1 ++ page 2 is the ordered set, with no repeats.

        **Unskipped in task 3.2** — this was skipped at 2.2 with that task named,
        because ``page`` was in the signature and referenced nowhere in the body, so
        a two-page test would have asserted that page 2 equalled page 1 and passed.
        Cursor paging (C2 Arm A, chosen by the user in 3.1) is what makes the
        assertion real: page 2 is positioned by the ``next_cursor`` page 1 returned.

        Nine rows at four per page is 4 + 4 + 1, so the walk crosses **two**
        boundaries rather than one — a cursor echoed back wrongly (off by one row,
        or the previous page's key resent) shows up as a duplicate or a gap in the
        concatenation, not merely as a short last page.

        The third page returning no cursor is asserted here rather than only in
        ``TestCursorPaging`` because "no omission" needs a defined end: a walk that
        never terminates cannot be compared against a finite expected order.
        """
        _seed(runs_tables["executions_table"])

        first = _call(page_size=4)
        assert first["pagination"]["next_cursor"], "nine rows read four at a time must offer a next page"
        second = _call(page_size=4, cursor=first["pagination"]["next_cursor"])
        assert second["pagination"]["next_cursor"], "eight of nine rows consumed — one page remains"
        third = _call(page_size=4, cursor=second["pagination"]["next_cursor"])

        walked = _run_ids(first) + _run_ids(second) + _run_ids(third)

        assert walked == list(EXPECTED_BY_TIME), f"the walked pages are not the ordered set; got {walked}"
        assert len(set(walked)) == len(walked), f"a row was returned twice across a page boundary: {walked}"
        assert third["pagination"]["next_cursor"] is None
        assert third["pagination"]["has_next"] is False


# ===========================================================================
# R2.1 / C2 Arm A — keyed paging, and what a bad cursor does
# ===========================================================================
class TestCursorPaging:
    """Task 3.2. The mechanism is ``ExclusiveStartKey``, not an offset.

    The live shape these tests encode was confirmed by querying the validation account
    directly (read-only) rather than inferred from moto: a ``RunsByTimeIndex``
    query returns ``LastEvaluatedKey = {pipeline_id, started_at, run_id}``, all
    strings, and feeding exactly that back returns the following rows with no
    overlap. A cursor from one pipeline replayed against another is refused by
    DynamoDB itself with ``ValidationException: The provided starting key is
    outside query boundaries based on provided conditions`` — also confirmed live,
    and the reason the handler refuses it *before* the query: that exception would
    otherwise land in the outer ``except`` and render as 200-with-empty-lists,
    i.e. "this pipeline has no runs".
    """

    def test_the_cursor_from_one_page_positions_the_next(self, runs_tables):
        """Round trip: page 1's cursor yields exactly the rows page 1 stopped at."""
        _seed(runs_tables["executions_table"])

        first = _call(page_size=2)
        second = _call(page_size=2, cursor=first["pagination"]["next_cursor"])

        assert _run_ids(first) == list(EXPECTED_BY_TIME[0:2])
        assert _run_ids(second) == list(EXPECTED_BY_TIME[2:4])

    def test_the_last_page_offers_no_cursor_and_says_so(self, runs_tables):
        """``has_next`` is "a cursor was returned" — C2 Arm A's definition.

        Asserted on the whole partition in one page: nine rows at ``page_size=10``
        exhausts the partition, so DynamoDB returns no ``LastEvaluatedKey``, no
        cursor is minted and ``has_next`` must be false. The two facts are derived
        from one value in the handler precisely so they cannot disagree here.
        """
        _seed(runs_tables["executions_table"])

        pagination = _call(page_size=10)["pagination"]

        assert pagination["next_cursor"] is None
        assert pagination["has_next"] is False
        assert pagination["has_prev"] is False

    def test_a_cursorless_request_is_page_one_whatever_page_was_asked_for(self, runs_tables):
        """The label cannot outrun the rows.

        DynamoDB has no offset addressing, so ``?page=7`` with no cursor is not a
        satisfiable request — the newest page is the only page reachable without a
        key. Reporting ``page: 7`` over page 1's rows would be the original defect
        ("every page request returns page 1") wearing a different number, and
        ``ExecutionsPage`` would render it. So the label is forced to 1.
        """
        _seed(runs_tables["executions_table"])

        pagination = _call(page_size=4, page=7)["pagination"]

        assert pagination["page"] == 1
        assert pagination["has_prev"] is False
        assert _run_ids(_call(page_size=4, page=7)) == list(EXPECTED_BY_TIME[0:4])

    def test_the_page_label_comes_from_the_cursor_not_the_query_string(self, runs_tables):
        """One source of truth for the label, and it is the cursor.

        The cursor carries the page number it addresses, so a client that sends a
        page-2 cursor while claiming ``page=99`` still gets ``page: 2``. Sourcing
        the label from the query string would let the two disagree, which is the
        same failure as R2.2's count describing something other than the set.
        """
        _seed(runs_tables["executions_table"])
        cursor = _call(page_size=4)["pagination"]["next_cursor"]

        pagination = _call(page_size=4, cursor=cursor, page=99)["pagination"]

        assert pagination["page"] == 2
        assert pagination["has_prev"] is True

    def test_one_page_read_per_request_however_deep_the_page(self, runs_tables, monkeypatch):
        """CP6's mechanism: the read is one page, not a walk to page N.

        This is the property the rejected Arm B would have broken — walking and
        discarding ``page - 1`` pages makes the enrichment key count a function of
        walk depth rather than of ``page_size``. Asserted as a call count because
        that is where the difference lives: Arm B is indistinguishable from Arm A
        in the returned rows and distinguishable here.

        **AMENDED IN TASK 3.3, and renamed with it** — it was
        ``test_one_query_per_request_however_deep_the_page`` and asserted
        ``len(calls) == 1``. C3's exact total adds a second query per request, a
        windowless ``Select="COUNT"`` walk, so the old assertion is now false while
        the property it was defending is not. Amended in place with the reason at the
        assertion rather than deleted or loosened, per ``LGR-60c4985-6`` /
        ``LGR-9e76249-10``: a deliberate hold encoded as an assertion should stay
        legible as a reversal.

        **Why the amended form still catches Arm B, which is the point.** Arm B's
        signature is *page reads scaling with page depth*. So the calls are
        partitioned by kind — a page read carries ``Limit`` and no ``Select``; the
        count read carries ``Select="COUNT"`` and no ``Limit`` — and the assertion is
        made on the **page reads only**. That is a sharper instrument than the old
        total: ``len(calls) == 1`` would also have been satisfied by one page read
        plus one spurious extra call, and would have *failed* for a correct
        implementation that added any second read. The two kinds are asserted to be
        disjoint as well, so a future change that turned the page read into a COUNT
        (or dropped ``Limit`` from it) fails here rather than silently.

        **AMENDED AGAIN IN TASK 4.3, and the amendment is a third leg rather than a
        loosened assertion.** C5's filter walk reads more than one index page, so
        "one page read per request" is no longer true of *every* request — but it is
        still exactly true of every request this test makes, because none of them
        filters. The assertions above are therefore unchanged. What 4.3 adds is the
        leg that keeps Arm B in the crosshairs now that a second reason for multiple
        page reads exists: **a filtered request's page-read count is a function of how
        selective the filter is, never of which page was asked for.** Held by holding
        selectivity fixed (a filter every row matches, so the walk fills its page on
        the first read) and varying page depth — under Arm B that would have cost one
        read for page 1 and two for page 2. Without this leg, a future reader could
        take the filter walk as licence to walk for *depth* too, and nothing would
        object. The genuinely multi-page filtered case is asserted in
        :class:`TestStatusFilter`, where the extra reads are the behaviour rather
        than a defect.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        original = table.query
        calls: list[dict[str, Any]] = []

        def _spy(**kwargs: Any) -> Any:
            calls.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(table, "query", _spy)

        first = _call(page_size=2)
        assert len(_page_reads(calls)) == 1, "page 1 must cost exactly one page read"
        assert "ExclusiveStartKey" not in _page_reads(calls)[0]
        assert len(_count_reads(calls)) == 1, "the exact total costs one COUNT walk over a one-page partition"

        _call(page_size=2, cursor=first["pagination"]["next_cursor"])

        assert len(_page_reads(calls)) == 2, "a second page must cost exactly one more PAGE read"
        assert _page_reads(calls)[1]["ExclusiveStartKey"] == {
            "pipeline_id": PIPELINE_ID,
            "started_at": LIVE_PARTITION[0][1],
            "run_id": EXPECTED_BY_TIME[1],
        }
        # Every recorded call is exactly one of the three kinds — no call is two and
        # none is neither, so the partition cannot hide a read. **Amended for p54
        # R10.3's lost-file read**, which is the third kind: without naming it here the
        # sum would simply stop balancing, which is a weaker failure than saying why.
        assert len(_page_reads(calls)) + len(_count_reads(calls)) + len(_lost_reads(calls)) == len(calls)
        assert len(_lost_reads(calls)) == 2, (
            "one keyed lost-file read per request, on both requests — it is page-independent by "
            "design, so it does not scale with page depth either"
        )

        # Task 4.3's leg: page depth must still cost nothing extra under a filter.
        # Selectivity is held fixed by using a partition where **every** row matches,
        # so the walk fills its page on its first read at any depth — LIVE_PARTITION
        # would not do, because its two skips sit between the SUCCEEDED rows and the
        # read count would then vary with position rather than with depth, which is a
        # different fact and not the one under test.
        homogeneous = "all-succeeded-partition"
        for n in range(6):
            table.put_item(
                Item=_row(
                    f"run-2026-08-2{n}-00-00-0{n}-uniform",
                    f"2026-08-2{n}T00:00:0{n}.000000+00:00",
                    "SUCCEEDED",
                    pipeline_id=homogeneous,
                )
            )
        calls.clear()
        first_filtered = _call(page_size=2, pipeline_id=homogeneous, status="SUCCEEDED")
        reads_for_page_one = len(_page_reads(calls))
        calls.clear()
        _call(
            page_size=2,
            pipeline_id=homogeneous,
            cursor=first_filtered["pagination"]["next_cursor"],
            status="SUCCEEDED",
        )

        assert len(_page_reads(calls)) == reads_for_page_one == 1, (
            "a filtered request's page reads must depend on the filter's selectivity, never on "
            "which page was asked for — that dependency is Arm B"
        )
        assert _count_reads(calls) == [], "a filtered request issues no COUNT walk (it counts the wrong set)"

    def test_a_malformed_cursor_is_a_400_not_an_empty_200(self, runs_tables):
        """A bad request must not read as "this pipeline has no runs".

        The outer ``except`` returns 200 with empty lists, which is right for a read
        failure and wrong for a bad cursor — it is exactly the quiet wrongness this
        spec opened on. So the decode happens above that ``try`` and a rejected
        cursor never reaches the query.
        """
        _seed(runs_tables["executions_table"])

        # Each input trips a different check, named so a future relaxation of any
        # one of them fails here rather than silently widening what is accepted.
        refused = {
            "non-base64 characters": "not-base64-at-all!!",
            "valid base64, not JSON": _b64("nonsense bytes"),
            "JSON, but not an object": _b64("[1, 2, 3]"),
            "an object with no pipeline binding": _b64('{"page": 2}'),
            "bound, but naming no page": _b64(json.dumps({"pipeline_id": PIPELINE_ID})),
            "bound and paged, but carrying an extra field": _b64(
                json.dumps({"pipeline_id": PIPELINE_ID, "page": 2, "Limit": 9999})
            ),
        }
        for what, bad in refused.items():
            response = _raw_call(cursor=bad)
            assert response["statusCode"] == 400, f"{what} should be refused; got {response}"
            assert json.loads(response["body"])["error"] == "Invalid pagination cursor"

    def test_an_empty_cursor_reads_as_absent_not_as_invalid(self, runs_tables):
        """``?cursor=`` is a client that has no cursor, not a client with a bad one.

        Handled in two places on purpose: ``router.py`` normalises an empty
        query-string value to ``None``, and the handler's own guard is truthiness
        rather than ``is not None``, so a direct caller gets the same answer.
        Pinned because the alternative — refusing it — would make a freshly-loaded
        page fail whenever a client serialises its empty cursor state into the URL.
        """
        _seed(runs_tables["executions_table"])

        response = _raw_call(cursor="")

        assert response["statusCode"] == 200
        assert _run_ids(json.loads(response["body"])) == list(EXPECTED_BY_TIME)

    def test_a_cursor_minted_for_another_pipeline_cannot_page_this_one(self, runs_tables):
        """The security property, at the handler boundary.

        A cursor is bound to the ``pipeline_id`` it was minted for, and the
        ``pipeline_id`` a request reads comes from the **path**, never from the
        cursor. So a cursor cannot be used to reach a partition the caller did not
        name. DynamoDB would also refuse this (confirmed live), but only as a
        ``ValidationException`` the outer ``except`` converts to an empty 200 —
        which is why the check is here and not left to the service.
        """
        _seed(runs_tables["executions_table"])
        cursor = _call(page_size=4)["pagination"]["next_cursor"]

        response = _raw_call(cursor=cursor, pipeline_id="some-other-pipeline")

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["message"] == "cursor was issued for a different pipeline"
        # The refusal must not leak the other pipeline's rows in any form.
        assert "executions" not in body

    def test_a_stale_cursor_naming_a_deleted_row_still_pages(self, runs_tables):
        """Staleness is not an error, and needs no handling.

        DynamoDB accepts an ``ExclusiveStartKey`` naming a row that has since been
        deleted and starts after that position in the key space. So a run ageing
        out between two page requests degrades to "the next page starts where it
        would have" rather than to a failure — worth pinning, because the obvious
        defensive move (verifying the row still exists) would cost a read and
        change nothing.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        cursor = _call(page_size=2)["pagination"]["next_cursor"]
        table.delete_item(Key={"pipeline_id": PIPELINE_ID, "run_id": EXPECTED_BY_TIME[1]})

        second = _call(page_size=2, cursor=cursor)

        assert _run_ids(second) == list(EXPECTED_BY_TIME[2:4])

    def test_get_queued_files_passes_no_cursor(self, runs_tables, monkeypatch):
        """do-NOT 3, at the new signature.

        ``get_queued_files`` calls this function with defaults and reads
        ``queued_files`` only. ``cursor`` was added last in the signature with a
        ``None`` default, so that delegation is unchanged — asserted on the query
        kwargs rather than only on the output, because the output would look
        identical if a cursor had somehow been threaded in.

        **Amended in task 3.3** for the same reason as
        ``test_one_page_read_per_request_however_deep_the_page``: the delegated call
        now also issues C3's ``Select="COUNT"`` walk, so ``len(calls) == 1`` became
        false. Scoped to the page read, which is the thing the assertion was about.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        original = table.query
        calls: list[dict[str, Any]] = []

        def _spy(**kwargs: Any) -> Any:
            calls.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(table, "query", _spy)

        response = runs_handler.get_queued_files(PIPELINE_ID)

        assert response["statusCode"] == 200
        assert len(_page_reads(calls)) == 1
        assert "ExclusiveStartKey" not in _page_reads(calls)[0]
        assert _page_reads(calls)[0]["Limit"] == 10


# ===========================================================================
# R2.2 / R2.3 / R2.4 / R2.5 / CP4 — the exact total and its bounds (task 3.3)
# ===========================================================================
#: 300 KB of filler per row. Four rows exceed DynamoDB's 1 MB page ceiling, which is
#: what forces the ``Select="COUNT"`` walk to paginate so its bounds can be made to
#: bind. Copied from ``test_stats_characterization.py:_THIRD_OF_A_PAGE`` deliberately
#: — same mechanism, same technique for exercising it. The index projects ALL, so an
#: index entry carries the whole item including the filler; that is also why
#: ``Select="COUNT"`` saves bytes on the wire and not read capacity.
_THIRD_OF_A_PAGE = "x" * 300_000

#: A partition of its own for the multi-page tests, so the oversized rows cannot
#: perturb any assertion made about ``LIVE_PARTITION``.
MULTIPAGE_PIPELINE_ID = "multipage-count-partition"


def _seed_multipage(table: Any, rows: int = 4) -> None:
    """Seed ``rows`` oversized executions rows into :data:`MULTIPAGE_PIPELINE_ID`."""
    for n in range(rows):
        table.put_item(
            Item=_row(
                f"run-2026-08-2{n}-00-00-0{n}-oversized",
                f"2026-08-2{n}T00:00:0{n}.000000+00:00",
                "SUCCEEDED",
                pipeline_id=MULTIPAGE_PIPELINE_ID,
                filler=_THIRD_OF_A_PAGE,
            )
        )


def _spy_queries(table: Any, monkeypatch: Any, fail_count_query: bool = False) -> list[dict[str, Any]]:
    """Record every ``query`` the handler issues against ``table``.

    ``fail_count_query`` raises on the ``Select="COUNT"`` call only, leaving the page
    read working — the failure mode that must degrade the *count* without discarding
    a page of correctly-ordered rows already in hand.
    """
    original = table.query
    calls: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if fail_count_query and kwargs.get("Select") == "COUNT":
            raise RuntimeError("ProvisionedThroughputExceededException")
        return original(**kwargs)

    monkeypatch.setattr(table, "query", _spy)
    return calls


class TestExactTotalCount:
    """R2.2 / R2.3: ``total_count`` describes the set being paged; ``total_pages``
    derives from it.

    Before task 3.3 ``total_count`` was ``len(executions)`` — the current page's
    length after filtering — and ``total_pages`` was the literal ``1``, which
    contradicted ``has_next`` in the same envelope for every pipeline with more than
    one page. Both defects are gone.

    **What the number counts, because R2.2 is that it must describe something
    real.** It is the count of raw rows in the pipeline's partition of
    ``RunsByTimeIndex`` — the rows the cursor walk traverses. It includes ``QUEUED``
    placeholders and Phantom_Execution_Rows, and excludes rows with no
    ``started_at``. So ``total_pages`` is the exact number of page *reads*, and the
    number of executions *rendered* can be smaller (R2.4). That direction is
    deliberate; see :func:`handlers.pipelines_runs._count_partition_rows`.

    **Validates: p48 Requirements 2.2, 2.3, 2.4**
    """

    def test_the_total_is_the_whole_partition_not_the_page(self, runs_tables):
        """The defect, inverted: nine rows read two at a time still total nine.

        ``len(executions)`` would have reported ``2`` here, and ``total_pages`` the
        literal ``1`` — which is the pair that made ``ExecutionsPage``'s
        ``Math.ceil(total_count / 10) > 1`` gate false and suppressed the pager
        (R2.6, task 5.1's prerequisite).
        """
        _seed(runs_tables["executions_table"])

        pagination = _call(page_size=2)["pagination"]

        assert pagination["total_count"] == len(LIVE_PARTITION) == 9
        assert pagination["total_pages"] == 5, "ceil(9 / 2)"
        assert pagination["partial_counts"] == [], "a one-page count walk truncated nothing"

    def test_every_page_agrees_on_the_total_and_the_page_count(self, runs_tables):
        """The total is a property of the partition, not of where the walk is.

        Walked rather than asserted on one page, because a count derived from the
        page — the old defect — varies by page and a count derived from the
        partition cannot. The last page also pins the boundary: ``page`` equals
        ``total_pages`` exactly when there is no next cursor.
        """
        _seed(runs_tables["executions_table"])

        cursor: str | None = None
        seen_pages: list[int] = []
        for _ in range(len(LIVE_PARTITION) + 2):
            body = _call(page_size=2, cursor=cursor)
            pagination = body["pagination"]
            assert pagination["total_count"] == 9, "every page must agree on the total"
            assert pagination["total_pages"] == 5
            assert pagination["partial_counts"] == []
            seen_pages.append(pagination["page"])
            cursor = pagination["next_cursor"]
            if cursor is None:
                break

        assert seen_pages == [1, 2, 3, 4, 5]

    def test_total_pages_agrees_with_has_next_at_both_ends(self, runs_tables):
        """R2.3: the two must not contradict each other in one envelope.

        That contradiction was the requirement's own example — ``total_pages: 1``
        beside ``has_next: true``. Asserted at both ends because one end passing is
        not the property: on a non-final page there must be a page after this one
        *and* the count must know it; on the final page neither.
        """
        _seed(runs_tables["executions_table"])

        first = _call(page_size=4)["pagination"]
        assert first["has_next"] is True
        assert first["page"] < first["total_pages"], "has_next says more; total_pages must agree"

        cursor = first["next_cursor"]
        last = first
        while cursor is not None:
            last = _call(page_size=4, cursor=cursor)["pagination"]
            cursor = last["next_cursor"]

        assert last["has_next"] is False
        assert last["page"] == last["total_pages"], "the final page is the last page the count knows about"

    def test_an_empty_partition_reports_zero_pages_not_one(self, runs_tables):
        """**The success-arm / error-arm reconciliation task 3.2 left for 3.3.**

        The success arm returned the literal ``total_pages: 1`` and the error arm
        ``0`` — two conventions for the same envelope, so a client could not read the
        field at all. With ``total_pages`` derived, an empty partition gives ``0``
        from the *same expression* the error arm reports, so the two agree. ``0``
        rather than ``1`` also matches the in-tree convention for an empty listing
        (``test_pipelines_list_characterization``'s
        ``total_pages == 0`` on an empty table).

        ``partial_counts`` is ``[]`` here and that distinction is the point: an empty
        partition is a *total* of zero. The error arm's zero is a floor, and is
        marked — see :meth:`TestTheCountDegrades.test_the_error_arm_marks_its_zeros`.
        """
        pagination = _call()["pagination"]

        assert pagination["total_count"] == 0
        assert pagination["total_pages"] == 0, "an empty listing has zero pages, not one"
        assert pagination["has_next"] is False
        assert pagination["partial_counts"] == [], "zero rows is an exact answer, not a floor"

    def test_the_count_query_is_windowless_and_asks_for_no_items(self, runs_tables, monkeypatch):
        """``LGR-9a35b2d-3``'s shape, asserted at the call.

        Four properties, each of which a plausible "improvement" would break:

        * ``Select="COUNT"`` — the whole reason the walk is affordable over a large
          partition is that no item crosses the wire;
        * **no ``Limit``** — windowless, so the walk counts the partition rather than
          a window of it. This is the defect ``LGR-9a35b2d-2``'s scope names in
          ``get_pipeline_file_count`` and this must not repeat;
        * **no ``ProjectionExpression``** — real DynamoDB rejects that paired with
          ``Select="COUNT"`` even though moto accepts it, so moto cannot catch this
          and an assertion has to;
        * **a ``FilterExpression`` that excludes Queued_Placeholder rows** — see
          below. This leg is REVERSED from its original form.

        **AMENDED for p54 R10.3, and this one is a genuine reversal rather than a
        rescoping.** The leg used to assert ``"FilterExpression" not in query``, on the
        ground that "the page walk carries none, so filtering only the count would make
        ``total_pages`` disagree with the number of page reads it describes". That trade
        was sound while a ``QUEUED`` placeholder lived about an hour and was deleted on
        processing. p54's loss recorder writes a row of the same shape with a **30-day**
        TTL, so a pipeline that loses files accumulates page capacity that never renders
        and never expires. ``total_count`` now describes the set being paged rather than
        the reads it costs, and ``total_pages`` is a label — navigation is ``has_next``
        / ``next_cursor``, minted from the page query's own ``LastEvaluatedKey`` and
        untouched by this walk, so no row becomes unreachable. Amended in place with the
        reason at the assertion (``LGR-60c4985-6`` / ``LGR-9e76249-10``) rather than
        deleted, so the reversal stays legible.

        The predicate's *shape* is asserted, not just its presence: a bare ``<>`` would
        be wrong, because DynamoDB comparisons are false for a path that does not exist
        and a row carrying no ``status`` at all would be dropped from a count of rows
        that do occupy page slots.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        calls = _spy_queries(table, monkeypatch)

        _call()

        count_reads = _count_reads(calls)
        assert len(count_reads) == 1
        query = count_reads[0]
        assert query["Select"] == "COUNT"
        assert query["IndexName"] == "RunsByTimeIndex"
        assert "Limit" not in query, "a windowed count counts a window, not the partition"
        assert "ProjectionExpression" not in query, "real DynamoDB rejects this with Select=COUNT"
        # Compared structurally, NOT as a string. ``get_expression()`` returns a dict
        # whose ``values`` hold further condition objects with the default ``repr``
        # (``<boto3.dynamodb.conditions.NotEquals object at 0x…>``), so no attribute
        # name and no literal ever appears in ``repr()`` of it — a substring assertion
        # here can never pass, however correct the filter. That is the trap this
        # assertion was originally written into; see
        # ``tests/fixtures/dynamodb_conditions.py``. The expected condition is spelled
        # in the same vocabulary as the requirement, and the comparison is
        # order-insensitive across the OR because that operator is commutative.
        expected = Attr("status").not_exists() | Attr("status").ne("QUEUED")
        actual = flatten_condition(query["FilterExpression"])
        assert actual == flatten_condition(expected), (
            "the count must exclude Queued_Placeholder rows while keeping rows that carry no status "
            f"at all — a bare <> drops the latter. Got {actual}"
        )
        assert "ExclusiveStartKey" not in query, "the walk starts at the top of the partition"

    def test_the_total_excludes_the_queued_placeholder(self, runs_tables):
        """R2.4 stated rather than left implicit: what the number is *over*.

        **REVERSED for p54 R10.3, and renamed with it** — it was
        ``test_the_total_counts_raw_index_rows_including_the_queued_placeholder`` and
        asserted ``total_count == 10`` for a partition of nine runs plus one placeholder,
        on the reasoning that "a page read spends a slot on it" and so it belongs in a
        number about page reads.

        That reasoning was right for the row it was written about — a placeholder that
        lived about an hour and was deleted on processing. p54's loss recorder writes a
        row of the *same shape* with a **30-day** TTL, so a pipeline that loses files
        accumulates page capacity that never renders and never expires: a total that
        drifts permanently away from anything an operator can count on the screen. The
        number now describes **the set being paged**.

        The old form named the alternative as a *correctness* failure — "counting
        post-filter rows would make ``total_pages`` under-state and hide a page that
        exists". That is true of a numeric-offset pager and false of this one:
        ``has_next`` and ``next_cursor`` come off the page query's own
        ``LastEvaluatedKey``, independent of this walk, and ``CursorPagination``
        navigates on those while rendering ``totalPages`` as a label. So the exchange is
        an under-stated *label* for a number that matches the rows — not an unreachable
        page. Amended in place with the reason (``LGR-60c4985-6`` / ``LGR-9e76249-10``).

        The phantom-row over-statement is deliberately still here and is asserted
        nowhere in this test: it is transient and rare, and no server-side predicate can
        express ``is_phantom_execution_row``.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        table.put_item(
            Item=_row(
                "queued-ee4ce8021b4d4c7a",
                "2026-08-24T13:27:46.000000+00:00",
                "QUEUED",
                current_step="queued_trigger",
                file_name="customer_data_6.csv",
            )
        )

        body = _call(page_size=10)

        assert len(body["executions"]) == 9, "nine rows render as executions"
        assert len(body["queued_files"]) == 1, "the tenth left through queued_files"
        assert body["pagination"]["total_count"] == 9, (
            "the total describes the set being paged, so the placeholder is not in it — it can "
            "never appear in executions and, as a 30-day loss record, it never expires either"
        )
        assert body["pagination"]["partial_counts"] == [], "an exact count, not a floor"

    def test_a_row_carrying_no_status_is_still_counted(self, runs_tables):
        """The exclusion is "is a placeholder", not "is not a run".

        A bare ``status <> "QUEUED"`` would drop this row, because DynamoDB's comparison
        operators are false for a path that does not exist — so a statusless row, which
        does occupy a page slot and does reach ``executions``, would vanish from the
        total. This is the test that fails if the ``attribute_not_exists`` disjunct is
        ever "simplified" away.
        """
        table = runs_tables["executions_table"]
        statusless = _row(
            "run-2026-08-24-19-00-00-nostatus",
            "2026-08-24T19:00:00.000000+00:00",
            "SUCCEEDED",
            pipeline_id="statusless-count-partition",
        )
        del statusless["status"]
        table.put_item(Item=statusless)

        body = _call(pipeline_id="statusless-count-partition")

        assert body["pagination"]["total_count"] == 1, "a row with no status is not a placeholder"
        assert body["pagination"]["partial_counts"] == []

    def test_the_total_never_reads_below_the_rows_in_the_same_response(self, runs_tables):
        """The envelope must not contradict itself, on any page or any bound.

        A total below the number of rows the same body carries is not a floor of
        anything — it is the class of contradiction R2.3 named. Held on the exact
        path (where the partition count dominates trivially) and on the degraded path
        (where it is the reason ``max(counted, len(executions))`` exists).
        """
        table = runs_tables["executions_table"]
        _seed(table)

        for page_size in (1, 2, 4, 10):
            body = _call(page_size=page_size)
            assert body["pagination"]["total_count"] >= len(body["executions"])


class TestTheBoundBinds:
    """R2.5: a count the bound stopped is a floor, and the response says which keys.

    Two guards, tested separately because they fail independently and one is
    load-bearing while the other is a backstop — ``LGR-9a35b2d-3``'s pairing, reused:

    * the **wall-clock deadline**, derived at call time from the request's remaining
      budget, which "makes the bound hold by construction and independently of
      per-read latency";
    * the **page cap**, following ``stats.py:_MAX_READ_PAGES``.

    Both are exercised by shortening the constant, never by sleeping: a
    ``sleep``-based test would make its own runtime part of its assertion.

    **Validates: p48 Requirements 2.5 (CP4)**
    """

    def test_the_page_cap_marks_the_total_and_the_page_count(self, runs_tables, monkeypatch):
        """One page of a multi-page partition is a floor, and both derived keys say so.

        The count is not asserted to be a particular number — how many oversized rows
        fit in a 1 MB page is DynamoDB's business. What is asserted is the pair that
        makes the answer honest: it is short of the truth, and it is named as short.
        """
        table = runs_tables["executions_table"]
        _seed_multipage(table)
        monkeypatch.setattr(runs_handler, "_MAX_COUNT_PAGES", 1)

        response = _raw_call(pipeline_id=MULTIPAGE_PIPELINE_ID, page_size=2)
        assert response["statusCode"] == 200, response
        pagination = json.loads(response["body"])["pagination"]

        assert pagination["total_count"] < 4, "the page cap did not bind — nothing to mark"
        assert pagination["partial_counts"] == ["total_count", "total_pages"], (
            "a truncated count must be named, and a value derived from it with it — an unmarked "
            "floor is indistinguishable from a total (R2.5)"
        )

    def test_a_deadline_that_has_passed_marks_the_same_two_keys(self, runs_tables, monkeypatch):
        """The guard that holds regardless of per-read latency, without a sleep.

        Driven by shortening the *budget* to zero: the deadline is
        ``started + max(0, budget - reserve)``, so a zero budget puts it at the
        instant the request began and every page boundary is already past it.

        The first page is still read — deliberately, and this pins it. A count of
        zero marked partial says nothing useful, so the walk always attempts one page
        and the bound applies to *continuing*. Exactly one COUNT query is issued,
        which is what says the deadline stopped the walk rather than the request.
        """
        table = runs_tables["executions_table"]
        _seed_multipage(table)
        monkeypatch.setattr(runs_handler, "_RUNS_TIME_BUDGET_SECONDS", 0.0)
        calls = _spy_queries(table, monkeypatch)

        response = _raw_call(pipeline_id=MULTIPAGE_PIPELINE_ID, page_size=2)
        assert response["statusCode"] == 200, response
        pagination = json.loads(response["body"])["pagination"]

        assert pagination["total_count"] > 0, "the first page must always be read"
        assert pagination["total_count"] < 4
        assert pagination["partial_counts"] == ["total_count", "total_pages"]

        count_reads = _count_reads(calls)
        assert len(count_reads) == 1, "the deadline stopped the walk at its first continuation"
        assert "ExclusiveStartKey" not in count_reads[0]

    def test_navigation_stays_exact_when_the_count_is_a_floor(self, runs_tables, monkeypatch):
        """The reconciliation that makes a floor survivable: ``has_next`` is not a count.

        ``pipelines_crud.py`` marks ``has_more`` alongside its floor because there
        ``has_more = page < total_pages``. Here ``has_next`` is "a cursor was
        returned", minted from the page query's own ``LastEvaluatedKey`` — a fact
        about the page read, independent of the count walk. So a truncated count
        cannot strand a reachable page, and neither ``has_next`` nor ``next_cursor``
        belongs in ``partial_counts``.

        Asserted by *using* the cursor a floor-count response returned, which is the
        behaviour the property is about rather than a restatement of the marker.
        """
        table = runs_tables["executions_table"]
        _seed_multipage(table)
        monkeypatch.setattr(runs_handler, "_MAX_COUNT_PAGES", 1)

        first = json.loads(_raw_call(pipeline_id=MULTIPAGE_PIPELINE_ID, page_size=1)["body"])
        assert first["pagination"]["partial_counts"] == ["total_count", "total_pages"]
        assert "has_next" not in first["pagination"]["partial_counts"]
        assert "next_cursor" not in first["pagination"]["partial_counts"]
        assert "executions" not in first["pagination"]["partial_counts"], (
            "the page is bounded server-side by Limit, so it is an exact ordered prefix"
        )
        assert first["pagination"]["has_next"] is True

        second = json.loads(
            _raw_call(
                pipeline_id=MULTIPAGE_PIPELINE_ID,
                page_size=1,
                cursor=first["pagination"]["next_cursor"],
            )["body"]
        )

        assert [e["run_id"] for e in second["executions"]] != [e["run_id"] for e in first["executions"]], (
            "a floor count must still page correctly — navigation does not depend on the count"
        )
        assert second["pagination"]["page"] == 2

    def test_the_deadline_is_derived_from_the_budget_and_shrinks_with_elapsed_time(self, runs_tables):
        """``_runs_read_deadline`` is relative to the request's start, never negative.

        Three properties, each of which a plausible refactor would break:

        1. it is ``started + (budget - reserve)``, so the window is what is left of
           the budget — anchoring at *now* would give a slow request a full fresh
           window and put the 25 s Lambda timeout back in reach;
        2. a later anchor yields a later deadline by exactly the same offset, so the
           window shrinks as the work ahead of it consumes budget rather than
           resetting;
        3. a budget below the reserve clamps to the anchor rather than going
           negative, so a misconfiguration degrades to "read one page and mark it"
           and not to "read backwards in time".

        The budget is asserted to fit inside the Lambda's 25 s timeout
        (``compute-stack.ts:770-788``, where it is recorded as load-bearing) with the
        reserve still in hand, because the whole point of the figure is that an
        overrun is a 200 carrying ``partial_counts`` rather than a gateway timeout
        carrying no body at all.
        """
        anchor = 1_000.0
        window = runs_handler._RUNS_TIME_BUDGET_SECONDS - runs_handler._RUNS_READ_RESERVE_SECONDS

        assert runs_handler._runs_read_deadline(anchor) == anchor + window
        assert runs_handler._runs_read_deadline(anchor + 5.0) == anchor + 5.0 + window
        assert runs_handler._RUNS_READ_RESERVE_SECONDS > 0
        assert runs_handler._RUNS_TIME_BUDGET_SECONDS + runs_handler._RUNS_READ_RESERVE_SECONDS < 25.0

    def test_a_budget_below_the_reserve_clamps_to_the_anchor(self, runs_tables, monkeypatch):
        """Property 3 above, isolated: the deadline is never before the request began."""
        monkeypatch.setattr(runs_handler, "_RUNS_TIME_BUDGET_SECONDS", 0.5)
        monkeypatch.setattr(runs_handler, "_RUNS_READ_RESERVE_SECONDS", 2.0)

        assert runs_handler._runs_read_deadline(1_000.0) == 1_000.0

    def test_the_bounds_match_the_mechanism_they_reuse(self, runs_tables):
        """R2.5: this is ``LGR-9a35b2d-3``'s shape reused, so the figures are its own.

        Pinned by comparison against ``stats.py`` rather than against a second copy
        of the numbers, so the two cannot drift into unexplained constants for one
        job. ``LGR-99050e6-5``'s argument against two divergent implementations of one
        mechanism is what this defends.

        ``pipelines_crud.py``'s cap is deliberately *not* matched: 20 there bounds
        bytes accumulated before an in-memory sort, and a ``Select="COUNT"`` walk
        accumulates nothing but an int.
        """
        from handlers import stats as stats_handler

        assert runs_handler._MAX_COUNT_PAGES == stats_handler._MAX_READ_PAGES == 100
        assert runs_handler._RUNS_TIME_BUDGET_SECONDS == stats_handler._STATS_TIME_BUDGET_SECONDS
        assert runs_handler._RUNS_READ_RESERVE_SECONDS == stats_handler._STATS_READ_RESERVE_SECONDS


class TestTheCountDegrades:
    """A count that cannot be obtained must degrade — never 500, never 400.

    Distinct from the bounds above: a bound that binds is not an error and the walk
    returns normally. These are the arms where the read *raises*.

    **Validates: p48 Requirements 2.5 (CP4)**
    """

    def test_a_count_query_failure_keeps_the_page_and_marks_the_total(self, runs_tables, monkeypatch):
        """The count is the only thing that degrades.

        A count failure must not reach the outer ``except``: that arm returns 200
        with **empty** lists, which would discard a page of correctly-ordered rows
        already in hand and render "this pipeline has no runs" — the class of quiet
        wrongness this spec opened on. Nor may it 400: a bad cursor is a client
        error and a throttled count is not.

        ``page_size=4`` makes the floor visible rather than coincidentally right: the
        degraded total falls back to the four rows in this very body, so
        ``total_pages`` reads ``1`` while ``has_next`` is ``True``. That pair looks
        like the defect R2.3 named — and is not, because ``partial_counts`` says the
        number is a floor. Clamping ``total_pages`` up to ``page`` would have
        manufactured a number that merely looked consistent, which is what the
        literal ``1`` did.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        _spy_queries(table, monkeypatch, fail_count_query=True)

        response = _raw_call(page_size=4)
        assert response["statusCode"] == 200, response
        body = json.loads(response["body"])

        assert _run_ids(body) == list(EXPECTED_BY_TIME[0:4]), "the page survives a count failure intact"
        assert body["pagination"]["total_count"] == 4, "the floor is the rows in hand, never below them"
        assert body["pagination"]["partial_counts"] == ["total_count", "total_pages"]
        assert body["pagination"]["has_next"] is True, "navigation is exact regardless"
        assert body["pagination"]["next_cursor"] is not None

    def test_the_error_arm_marks_its_zeros(self, runs_tables, monkeypatch):
        """The other half of the reconciliation: what the error arm's ``0`` means.

        On the success arm ``total_count: 0`` is a total — the partition is empty. On
        this arm the read failed, so ``0`` is a floor: "I could not look", which must
        not read as "I looked and found none" (``LGR-b2f79ef-14``'s reasoning, as
        ``pipelines_crud.py`` and ``LGR-9a35b2d-2`` both apply it). ``stats.py`` made
        the same extension when it brought its swallowed approvals failure under the
        marker rather than marking one neighbour honestly and inheriting the other's
        misreport.

        ``total_pages`` is ``0`` on both arms now, so the value alone cannot tell
        them apart; ``partial_counts`` is what does.
        """
        table = runs_tables["executions_table"]
        _seed(table)

        def _raise(**_kwargs: Any) -> Any:
            raise RuntimeError("Requested resource not found: Index not found")

        monkeypatch.setattr(table, "query", _raise)

        response = _raw_call()
        assert response["statusCode"] == 200
        pagination = json.loads(response["body"])["pagination"]

        assert pagination["total_count"] == 0
        assert pagination["total_pages"] == 0
        assert pagination["partial_counts"] == ["total_count", "total_pages"]

    def test_get_queued_files_still_works_when_the_count_fails(self, runs_tables, monkeypatch):
        """do-NOT 3: the delegation is unaffected by anything the count does.

        ``get_queued_files`` reads ``queued_files`` only, so a degraded count must be
        invisible to it. Asserted with the count failing, which is the case where a
        careless implementation would have let an exception escape the delegate.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        table.put_item(
            Item=_row(
                "queued-ee4ce8021b4d4c7a",
                "2026-08-24T13:27:46.000000+00:00",
                "QUEUED",
                current_step="queued_trigger",
                file_name="customer_data_6.csv",
            )
        )
        _spy_queries(table, monkeypatch, fail_count_query=True)

        response = runs_handler.get_queued_files(PIPELINE_ID)
        body = json.loads(response["body"])

        assert response["statusCode"] == 200
        assert body["count"] == 1
        assert body["queued_files"][0]["file_name"] == "customer_data_6.csv"


def test_partial_counts_is_present_on_every_arm_and_is_a_list_of_strings(runs_tables, monkeypatch):
    """CP4: the key is present in every response, and ``[]`` exactly when nothing is short.

    Design §P3 and ``LGR-9a35b2d-3``: "one answer is assertable with ``==``; two are
    not". An optional key makes the envelope's key set unassertable, so it is never
    omitted — the same reasoning task 3.2 applied to ``next_cursor``.

    All four reachable arms in one test, because the property is about the key set
    being uniform across them rather than about any one of them.
    """
    table = runs_tables["executions_table"]

    # 1. empty partition — exact
    empty = _call()["pagination"]
    assert empty["partial_counts"] == []

    # 2. populated partition, count completes — exact
    _seed(table)
    exact = _call()["pagination"]
    assert exact["partial_counts"] == []

    # 3. count fails — floor
    with monkeypatch.context() as patched:
        original = table.query

        def _fail_count(**kwargs: Any) -> Any:
            if kwargs.get("Select") == "COUNT":
                raise RuntimeError("throttled")
            return original(**kwargs)

        patched.setattr(table, "query", _fail_count)
        floor = _call()["pagination"]
    assert floor["partial_counts"] == ["total_count", "total_pages"]

    # 4. the whole read fails — floor
    with monkeypatch.context() as patched:

        def _raise(**_kwargs: Any) -> Any:
            raise RuntimeError("boom")

        patched.setattr(table, "query", _raise)
        errored = _call()["pagination"]
    assert errored["partial_counts"] == ["total_count", "total_pages"]

    for pagination in (empty, exact, floor, errored):
        assert isinstance(pagination["partial_counts"], list)
        assert all(isinstance(name, str) for name in pagination["partial_counts"])


# ===========================================================================
# Guards against the whole file passing vacuously
# ===========================================================================
def test_the_index_exists_in_the_test_fixture():
    """Every ordering assertion rests on ``RunsByTimeIndex`` being on the moto
    table with the deployed key schema. Without it the handler's query raises and
    the error arm returns 200 with an EMPTY executions list — which would turn
    most assertions above into comparisons of empty lists. Assert the definition
    directly so that failure mode is named rather than mysterious.
    """
    from tests.fixtures.conftest import TABLE_DEFINITIONS

    executions = next(t for t in TABLE_DEFINITIONS if t["TableName"] == EXECUTIONS_TABLE)
    index = next((g for g in executions["gsis"] if g["IndexName"] == "RunsByTimeIndex"), None)

    assert index is not None, "RunsByTimeIndex is missing from the shared moto table definitions"
    assert (index["pk"], index["sk"]) == ("pipeline_id", "started_at")


def test_the_handler_names_the_index_by_the_deployed_constant():
    """The ``IndexName`` string and the CDK ``indexName`` must be the same literal.

    A mismatch is a ``ValidationException`` at request time, not an import error,
    and the error arm converts it to a 200 with empty lists — so run history would
    go silently blank rather than fail loudly. Compared against the CDK source
    rather than against a second copy of the string.
    """
    from pathlib import Path

    data_stack = Path(__file__).resolve().parents[4] / "deploy-cdk" / "lib" / "data-stack.ts"

    assert f"indexName: '{runs_handler.RUNS_BY_TIME_INDEX}'" in data_stack.read_text()


def test_an_index_query_failure_degrades_to_200_not_500(runs_tables, monkeypatch):
    """The new failure mode this change introduces, landed in the existing arm.

    A query against a missing or still-backfilling index raises. CloudFormation
    reported ``UPDATE_COMPLETE`` about seven minutes before the index reached
    ``ACTIVE`` on a 61-row table (task 2.1), and a backfilling index returns
    partial results with no error (p48 O5) — so "the index is not queryable yet"
    is a real state, not a hypothetical. It must reach the outer ``except`` and
    return 200 with empty lists: a read failure must never become a 500.
    """
    table = runs_tables["executions_table"]
    _seed(table)

    def _raise(**_kwargs: Any) -> Any:
        raise RuntimeError("Requested resource not found: Index not found")

    monkeypatch.setattr(table, "query", _raise)

    response = runs_handler.get_pipeline_executions(PIPELINE_ID)
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["executions"] == []
    assert body["queued_files"] == []


# ===========================================================================
# R4.2 / R4.3 / R4.4 — the derived-status filter (task 4.3, design C5 Arm 1)
# ===========================================================================
#: A row stored ``RUNNING`` with an approval pending. The read path rewrites its
#: ``status`` to ``WAITING_APPROVAL``, so it is the row that tells the derived status
#: apart from the stored one — the pair CP3 is about.
AWAITING_APPROVAL_RUN = "run-2026-08-25-09-00-00-awaiting"


class TestStatusFilter:
    """The filter matches the **derived** status, over a bounded walk.

    **Where the filter sits, and why it is the whole of the design.** It runs after
    the ``QUEUED`` split, after the phantom-row filter, and after the
    ``WAITING_APPROVAL`` derivation — all four steps live in
    :func:`handlers.pipelines_runs._split_derive_and_filter` precisely so the order is
    spelled once and a mutation to it is visible in one place. A filter placed before
    the derivation returns **nothing** for ``WAITING_APPROVAL``, the one status an
    operator most wants to find, because no writer ever stores that string (do-NOT 8,
    R4.2). ``test_run_history_status_filter_prop.py`` carries that as CP3 over
    generated rows; the deterministic complement is here.

    **Arm 1, the bounded filter walk, is what the multi-page tests below are about.**
    Arm 2 — filtering within one page only — was rejected because an operator
    filtering ``SKIPPED`` on a pipeline whose skips are older than one page sees "no
    runs match" when three exist.
    :meth:`test_a_skip_older_than_one_page_is_still_found` is that defect as an
    assertion, on the live partition where the older skip sits at raw position 6.

    **Validates: p48 Requirements 4.2, 4.3, 4.4**
    """

    # -- R4.4: SKIPPED is first-class -------------------------------------
    def test_skipped_is_filterable_and_returns_both_skips_in_time_order(self, runs_tables):
        """The case that opened the spec, as a filter (R4.4)."""
        _seed(runs_tables["executions_table"])

        body = _call(status="SKIPPED")

        assert _run_ids(body) == [
            "skipped-7412636fccb938719673179658c05773",
            OLDER_SKIP,
        ], "both skips, newest first — the filter must not disturb C1's ordering"

    def test_a_skip_older_than_one_page_is_still_found(self, runs_tables):
        """**Arm 2's defect, as an assertion.**

        ``page_size=2`` means the first index page is the two newest rows, of which
        exactly one is a skip. Arm 2 would have returned that one and stopped, and
        ``skipped-42e54662…`` — at raw position 6 of 9 by ``started_at`` — would have
        been unreachable under the filter for as long as five newer rows existed. The
        walk finds it, on the first filtered page, which is the point: the filtered
        page is *full* in the common case rather than truncated by where the raw page
        boundary happened to fall.
        """
        _seed(runs_tables["executions_table"])

        body = _call(page_size=2, status="SKIPPED")

        assert _run_ids(body) == ["skipped-7412636fccb938719673179658c05773", OLDER_SKIP]
        # The count is a **floor** here even though two is the true total, and that is
        # the honest answer rather than a shortcoming: the walk stopped the moment it
        # had a full page, at raw position 6 of 9, so it never established that rows
        # 7-9 hold no further skips. Reporting "exactly 2" would be asserting something
        # the read did not check — ``LGR-b2f79ef-14``'s reasoning, and the same
        # direction of error task 3.3 accepted for the count walk. Exactness needs the
        # walk to have seen the whole partition, which
        # :meth:`test_the_filtered_count_counts_matches_not_the_whole_partition`
        # exercises at the default page size.
        assert body["pagination"]["partial_counts"] == ["total_count", "total_pages"]
        assert "executions" not in body["pagination"]["partial_counts"], (
            "the page is full, so no bound truncated it — only the count is short"
        )

    def test_the_walk_reads_more_than_one_index_page_to_fill_a_filtered_page(self, runs_tables, monkeypatch):
        """The mechanism behind the test above, asserted at the calls.

        Named separately from the behaviour so that an implementation which passed the
        previous test by, say, dropping ``Limit`` and reading the whole partition in
        one go would still be distinguishable. Arm 1 is specifically *successive
        bounded pages*, and every page's ``Limit`` is the number of matches still
        needed — which is what keeps the collected set inside ``page_size`` (CP6) and
        the cursor exact.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        calls = _spy_queries(table, monkeypatch)

        _call(page_size=2, status="SKIPPED")

        page_reads = _page_reads(calls)
        assert len(page_reads) > 1, "one page read cannot reach a skip at raw position 6"
        assert page_reads[0]["Limit"] == 2, "the first page asks for the whole page"
        assert page_reads[1]["Limit"] == 1, "later pages ask only for the matches still needed"
        assert all("ExclusiveStartKey" in read for read in page_reads[1:]), "the walk follows the continuation token"

    # -- R4.2 / CP3: the derived status ------------------------------------
    def test_waiting_approval_is_selectable_and_its_stored_status_is_not(self, runs_tables):
        """do-NOT 8, deterministically. The filter is on the derived value.

        ``AWAITING_APPROVAL_RUN`` is stored ``RUNNING`` with ``pending_approval_id``
        set, so the read path rewrites it. Both halves matter: a filter placed above
        the derivation would return nothing for ``WAITING_APPROVAL`` **and** would
        return this row for ``RUNNING``, so asserting only the first half would pass
        for an implementation that filtered the stored value and got lucky.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        table.put_item(
            Item=_row(
                AWAITING_APPROVAL_RUN,
                "2026-08-25T09:00:00.000000+00:00",
                "RUNNING",
                pending_approval_id="appr-7c1f",
                pending_approval_type="code_review",
            )
        )

        assert _run_ids(_call(status="WAITING_APPROVAL")) == [AWAITING_APPROVAL_RUN]
        assert _run_ids(_call(status="RUNNING")) == [], "the stored status must not match once it is derived away"

    def test_the_filter_runs_after_the_phantom_filter_too(self, runs_tables):
        """do-NOT 4 preserved: a phantom row cannot be resurrected by a filter.

        A Phantom_Execution_Row carrying a matching status must still be dropped — the
        filter narrows the list the phantom filter already produced, it does not
        re-derive it from ``all_items``. Seeded with neither ``current_step`` nor
        ``steps_status``, which is what ``is_phantom_execution_row`` looks at.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        table.put_item(
            Item={
                "pipeline_id": PIPELINE_ID,
                "run_id": "run-2026-08-25-10-00-00-phantom0",
                "started_at": "2026-08-25T10:00:00.000000+00:00",
                "status": "SUCCEEDED",
            }
        )

        assert "run-2026-08-25-10-00-00-phantom0" not in _run_ids(_call(status="SUCCEEDED"))

    # -- R4.3: an empty match, and honest scope ----------------------------
    def test_an_empty_match_is_a_200_with_zero_rows_and_an_accurate_count(self, runs_tables):
        """Design §Error Handling: an empty filter match is not an error.

        Not a 404 and not a 500 — an empty ``executions`` array with an accurate
        ``total_count`` of 0, which the UI renders as "no runs match". And ``0`` here is
        a *total*, not a floor, because the walk saw the whole partition: an unmarked
        zero is only honest when it was actually established.
        """
        _seed(runs_tables["executions_table"])

        response = _raw_call(status="CANCELLED")
        assert response["statusCode"] == 200, response
        body = json.loads(response["body"])

        assert body["executions"] == []
        assert body["pagination"]["total_count"] == 0
        assert body["pagination"]["total_pages"] == 0
        assert body["pagination"]["has_next"] is False
        assert body["pagination"]["partial_counts"] == [], "nothing matched, and that is an exact answer"

    def test_the_filtered_count_counts_matches_not_the_whole_partition(self, runs_tables, monkeypatch):
        """R4.3: the count must not imply the whole history was searched and matched.

        Two skips in a nine-row partition must read ``total_count: 2``, not ``9``.
        Reporting the partition total beside a filtered view is exactly the scope claim
        R4.3 forbids, so the ``Select="COUNT"`` walk — which counts raw index rows — is
        skipped entirely under a filter rather than reused.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        calls = _spy_queries(table, monkeypatch)

        pagination = _call(status="SKIPPED")["pagination"]

        assert pagination["total_count"] == 2, "the matches, not the nine rows walked to find them"
        assert pagination["total_pages"] == 1
        assert _count_reads(calls) == [], "the COUNT walk counts raw rows and would answer the wrong question"

    def test_a_full_filtered_page_reports_its_count_as_a_floor(self, runs_tables):
        """A full filtered page reports "at least this many match", not a total.

        With ``page_size=1`` the walk stops at its first match with the partition
        unfinished, so ``1`` is a floor and both derived keys say so. Marking a
        conservative floor is never wrong; reporting it unmarked would be the "checked
        and found none" defect (``LGR-b2f79ef-14``'s reasoning) in its "checked and
        found exactly one" form.
        """
        _seed(runs_tables["executions_table"])

        pagination = _call(page_size=1, status="SKIPPED")["pagination"]

        assert pagination["total_count"] == 1
        assert pagination["has_next"] is True
        assert pagination["partial_counts"] == ["total_count", "total_pages"]
        assert "executions" not in pagination["partial_counts"], (
            "the page is full, so nothing about it is short — only the count is"
        )

    def test_a_cursored_filtered_walk_can_never_report_an_exact_count(self, runs_tables):
        """A walk that started mid-partition cannot know the total, and says so.

        Exactness needs **both** ends — no cursor in and no continuation key out — so
        every page after the first is a floor by construction, *including the last one*.
        That last page is the interesting case and the one asserted here: it exhausts
        the partition, so ``has_next`` is ``False`` and nothing is left to read, and its
        count is still marked because the matches from earlier pages are not in this
        body. An implementation that keyed exactness off the continuation key alone
        would pass every other test in this class and fail here.
        """
        _seed(runs_tables["executions_table"])
        cursor = _call(page_size=1, status="SKIPPED")["pagination"]["next_cursor"]

        last = {}
        for _ in range(len(LIVE_PARTITION) + 2):
            body = _call(page_size=1, cursor=cursor, status="SKIPPED")
            assert body["pagination"]["partial_counts"][-2:] == ["total_count", "total_pages"], (
                "no page reached through a cursor can claim an exact filtered total"
            )
            last = body
            cursor = body["pagination"]["next_cursor"]
            if cursor is None:
                break

        assert last["pagination"]["has_next"] is False, "the walk must terminate at the end of the partition"
        assert last["executions"] == [], "the final filtered page is past the last match"
        assert last["pagination"]["partial_counts"] == ["total_count", "total_pages"]

    def test_the_cursor_stays_exact_under_a_filter(self, runs_tables):
        """CP2 under a walk: no duplicate, no omission across filtered pages.

        The property the walk's shrinking ``Limit`` buys. Because each page asks for
        only the matches still needed, the walk never reads past a row it returns, so
        the final ``LastEvaluatedKey`` sits immediately after the last raw row
        consumed. Walked to exhaustion and compared against the single-page filter
        result, so the assertion is about the cursor rather than about the predicate.
        """
        _seed(runs_tables["executions_table"])
        expected = _run_ids(_call(status="SKIPPED"))

        walked: list[str] = []
        cursor: str | None = None
        for _ in range(len(LIVE_PARTITION) + 2):
            body = _call(page_size=1, cursor=cursor, status="SKIPPED")
            walked.extend(_run_ids(body))
            cursor = body["pagination"]["next_cursor"]
            if cursor is None:
                break

        assert walked == expected
        assert len(set(walked)) == len(walked)

    # -- CP6 under a walk --------------------------------------------------
    def test_the_enrichment_stays_bounded_by_page_size_not_by_walk_length(self, runs_tables, monkeypatch):
        """CP6, which the walk had to preserve and does — by construction.

        Design §Architecture is explicit that ``Limit=page_size`` is what keeps the
        enrichment key count a function of page size alone: 4 keys per run, one
        ``batch_get_item`` for a default page. A walk that collected everything it read
        and then sliced would have broken it; a walk whose ``Limit`` is the number of
        matches still needed cannot. Asserted at ``batch_get_item``, because that is
        where the cost actually lands.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        batches: list[dict[str, Any]] = []
        original = runs_handler.dynamodb.batch_get_item

        def _spy(**kwargs: Any) -> Any:
            batches.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(runs_handler.dynamodb, "batch_get_item", _spy)

        body = _call(page_size=1, status="SKIPPED")

        assert len(body["executions"]) <= 1, "the walk must not over-collect past the page"
        keys = [k for batch in batches for k in batch["RequestItems"][STATE_TABLE]["Keys"]]
        assert len(keys) == 4, "4 keys for the one run on the page, not for the six rows walked to find it"

    # -- The bounds, and what they are named as ----------------------------
    def test_the_page_cap_names_executions_and_the_count(self, runs_tables, monkeypatch):
        """A bound that stops the walk short must not render as a complete answer.

        ``executions`` is named **first** and the count keys follow, so the marker is
        deterministic and assertable with ``==``. Task 3.3 deliberately left
        ``executions`` out of its list on the ground that ``Limit`` made the page an
        exact prefix; under a filter walk a bound can stop the walk with matches still
        unread, which is the premise changing rather than the rule bending.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        monkeypatch.setattr(runs_handler, "_MAX_FILTER_PAGES", 1)

        body = _call(page_size=2, status="SKIPPED")

        assert _run_ids(body) == ["skipped-7412636fccb938719673179658c05773"], "one page's worth of matches"
        assert body["pagination"]["partial_counts"] == ["executions", "total_count", "total_pages"]
        assert body["pagination"]["has_next"] is True, "the bound must never strand the unread remainder"

    def test_the_deadline_names_the_same_keys_and_is_tested_without_sleeping(self, runs_tables, monkeypatch):
        """The guard that holds regardless of per-read latency.

        Driven by shortening the walk's *share* of the budget to zero, exactly as
        ``TestTheBoundBinds`` shortens the count's budget: the deadline becomes the
        instant the request began, so every continuation check is already past it. A
        ``sleep``-based test would make its own runtime part of its assertion.

        The first page is still read, deliberately — a filtered page of zero marked
        partial says nothing useful, so the bound applies to *continuing*.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        monkeypatch.setattr(runs_handler, "_FILTER_WALK_TIME_SHARE_SECONDS", 0.0)

        body = _call(page_size=2, status="SKIPPED")

        assert len(body["executions"]) == 1, "the first page is always read"
        assert body["pagination"]["partial_counts"] == ["executions", "total_count", "total_pages"]

    def test_a_short_filtered_page_with_a_next_cursor_always_marks_executions(self, runs_tables, monkeypatch):
        """The invariant task 5.5 can render against, stated as a test.

        A filtered page shorter than ``page_size`` carrying ``has_next: true`` can only
        have arisen from a bound — the other two exits are "page full" and "partition
        exhausted", and neither produces that pair. So the marker is present whenever
        the pair appears, which is what lets the UI tell "the filtered list ends here"
        from "we stopped looking".
        """
        table = runs_tables["executions_table"]
        _seed(table)
        marked = 0
        for cap in (1, 2, 3):
            monkeypatch.setattr(runs_handler, "_MAX_FILTER_PAGES", cap)
            body = _call(page_size=3, status="SKIPPED")
            pagination = body["pagination"]
            if len(body["executions"]) < 3 and pagination["has_next"]:
                assert "executions" in pagination["partial_counts"], f"cap={cap} produced an unmarked short page"
                marked += 1
        assert marked > 0, "no cap produced the short-page-with-next pair — the test proved nothing"

    def test_the_walk_deadline_is_a_share_of_the_count_deadline(self, runs_tables):
        """The carve-out task 3.3 did not need and this one does.

        3.3's argument for no carve-out was positional — the count runs **last**, so it
        is the thing that yields. The filter walk runs **first**, ahead of the schema
        map, the cost / ``load_outcome`` enrichment and the count, so without a share of
        its own it could starve all three. Asserted as a strict inequality against the
        count's deadline from the same anchor, plus the ``pipelines_crud`` figure it
        borrows, so the two cannot drift into unexplained constants.
        """
        anchor = 1_000.0

        assert runs_handler._filter_walk_deadline(anchor) < runs_handler._runs_read_deadline(anchor)
        assert runs_handler._FILTER_WALK_TIME_SHARE_SECONDS == 8.0
        # At least the reserve plus a working window is left for everything behind it.
        remaining = runs_handler._RUNS_TIME_BUDGET_SECONDS - runs_handler._FILTER_WALK_TIME_SHARE_SECONDS
        assert remaining - runs_handler._RUNS_READ_RESERVE_SECONDS >= 10.0

    def test_a_share_larger_than_the_budget_cannot_outlive_the_count_deadline(self, runs_tables, monkeypatch):
        """A misconfiguration must not invert the carve-out silently."""
        monkeypatch.setattr(runs_handler, "_FILTER_WALK_TIME_SHARE_SECONDS", 999.0)

        assert runs_handler._filter_walk_deadline(1_000.0) == 1_000.0 + runs_handler._RUNS_TIME_BUDGET_SECONDS

    # -- The rejection arms ------------------------------------------------
    def test_an_unrecognised_status_is_a_400_not_an_empty_200(self, runs_tables):
        """Task 3.2's precedent, applied to the second query-string input.

        ``?status=SUCEEDED`` returning ``executions: []`` would read as "this pipeline
        has no successful runs" — a false statement about the data caused by a client
        typo, and the class of quiet wrongness this spec exists to remove. The message
        names the accepted set and never echoes the submitted value, matching the
        cursor arm.
        """
        _seed(runs_tables["executions_table"])

        # ``" SKIPPED"`` is in the list to pin that the value is NOT stripped: an
        # accepted value with stray whitespace is a client bug worth naming, and
        # ``LGR-ad78946-1`` rejected normalising for exactly that reason — "normalising
        # removes the only signal that the deployed value is not the intended one".
        for bad in ("SUCEEDED", "succeeded", " SKIPPED", "DELETED", "*"):
            response = _raw_call(status=bad)
            assert response["statusCode"] == 400, f"{bad!r} should be refused; got {response}"
            body = json.loads(response["body"])
            assert body["error"] == "Invalid status filter"
            assert "SKIPPED" in body["message"], "it should name what is accepted instead"

        # Checked separately on values that cannot appear inside the accepted set as a
        # substring, so the assertion is about echoing and not about coincidence —
        # ``" SKIPPED"`` occurs in ``", SKIPPED"`` and would fail this for the wrong
        # reason.
        for distinctive in ("SUCEEDED", "<script>", "pipeline-id-leak"):
            message = json.loads(_raw_call(status=distinctive)["body"])["message"]
            assert distinctive not in message, "the message must not echo client-controlled content"

    def test_queued_is_refused_with_a_message_naming_queued_files(self, runs_tables):
        """p48 O2, resolved — and resolved *structurally*, which is why it is a 400.

        The ``QUEUED`` split runs before the filter, so by the time the filter sees the
        list every ``QUEUED`` row is already in ``queued_files``. ``status=QUEUED`` is
        therefore a query over an array that provably cannot contain a match: not "no
        runs match", which is a fact about the data, but "this question cannot be asked
        of this array". An empty 200 would read as the former, which is the "control
        that returns an empty table" O2's own fallback warns against.

        Task 5.5 must not offer ``QUEUED``; this arm is what makes that a contract
        rather than a convention.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        table.put_item(
            Item=_row(
                "queued-ee4ce8021b4d4c7a",
                "2026-08-24T13:27:46.000000+00:00",
                "QUEUED",
                current_step="queued_trigger",
                file_name="customer_data_6.csv",
            )
        )

        response = _raw_call(status="QUEUED")

        assert response["statusCode"] == 400
        message = json.loads(response["body"])["message"]
        assert "queued_files" in message, "the message must point at where queued files actually are"

    def test_an_empty_status_reads_as_absent_not_as_invalid(self, runs_tables):
        """``?status=`` is a client with no filter, not a client with a bad one.

        Guarded in two places for the reason the cursor is: ``router.py`` normalises the
        empty query-string value to ``None``, and the handler's own guard is truthiness,
        so a direct caller gets the same answer. Refusing it would make a
        freshly-loaded page fail whenever a client serialises its empty filter state
        into the URL.
        """
        _seed(runs_tables["executions_table"])

        response = _raw_call(status="")

        assert response["statusCode"] == 200
        assert _run_ids(json.loads(response["body"])) == list(EXPECTED_BY_TIME)

    # -- do-NOT 3 and the unfiltered path ----------------------------------
    def test_queued_files_does_not_vary_with_the_status_filter(self, runs_tables):
        """``queued_files`` is page-scoped and must stay a function of the page alone.

        Accumulating it across the walk would make the array depend on how far the
        filter had to search, so the same request with ``status=SKIPPED`` and
        ``status=SUCCEEDED`` would return different queued files. It comes from the
        first page read, always.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        table.put_item(
            Item=_row(
                "queued-ee4ce8021b4d4c7a",
                "2026-08-30T13:27:46.000000+00:00",
                "QUEUED",
                current_step="queued_trigger",
                file_name="customer_data_6.csv",
            )
        )

        unfiltered = _call(page_size=2)["queued_files"]
        skipped = _call(page_size=2, status="SKIPPED")["queued_files"]
        succeeded = _call(page_size=2, status="SUCCEEDED")["queued_files"]

        assert (
            unfiltered
            == skipped
            == succeeded
            == [
                {
                    "file_name": "customer_data_6.csv",
                    "source_key": "",
                    "queued_at": "2026-08-30T13:27:46.000000+00:00",
                    "status": "queued",
                }
            ]
        )

    def test_get_queued_files_passes_no_status(self, runs_tables, monkeypatch):
        """do-NOT 3 at the newest signature.

        ``status`` was added last with a ``None`` default, so the delegation is
        unchanged: one page read, no walk. Asserted on the call count rather than only
        on the output, because a filtered delegation could still return the same
        ``queued_files`` while costing up to 100 reads to do it.
        """
        table = runs_tables["executions_table"]
        _seed(table)
        table.put_item(
            Item=_row(
                "queued-ee4ce8021b4d4c7a",
                "2026-08-24T13:27:46.000000+00:00",
                "QUEUED",
                current_step="queued_trigger",
                file_name="customer_data_6.csv",
            )
        )
        calls = _spy_queries(table, monkeypatch)

        response = runs_handler.get_queued_files(PIPELINE_ID)

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["count"] == 1
        assert len(_page_reads(calls)) == 1, "an unfiltered delegation reads exactly one page"
        assert len(_count_reads(calls)) == 1, "and still gets C3's count, since status is None"

    def test_the_unfiltered_envelope_is_unchanged_by_task_4_3(self, runs_tables):
        """The regression guard for the path 4.3 must not have touched.

        ``status=None`` has to be equivalent to the pre-4.3 behaviour, because that is
        what keeps do-NOT 3, CP6 and every C1–C3 assertion in this file meaningful.
        Asserted as the whole pagination block rather than field by field, so an
        accidental extra key or a changed default fails here.
        """
        _seed(runs_tables["executions_table"])

        body = _call()

        assert _run_ids(body) == list(EXPECTED_BY_TIME)
        assert body["pagination"] == {
            "page": 1,
            "page_size": 10,
            "total_count": 9,
            "total_pages": 1,
            "has_next": False,
            "has_prev": False,
            "next_cursor": None,
            "partial_counts": [],
        }


# ===========================================================================
# R6.1 / C6 — the schema-version map is paginated (task 6.1)
# ===========================================================================
#: A partition of its own, so the oversized ``schema_extract`` rows cannot perturb any
#: assertion made about :data:`LIVE_PARTITION` or :data:`MULTIPAGE_PIPELINE_ID`.
SCHEMA_WALK_PIPELINE_ID = "multipage-schema-version-partition"

#: The number of ``schema_extract`` rows seeded below. At 300 KB each, moto returns
#: **three per page** — verified by walking the index directly rather than assumed — so
#: eight rows is three pages and the newest hash sits on the third. Before task 6.1 the
#: enrichment read page one and stopped, so hashes 4–8 did not exist as far as the
#: version map was concerned.
_SCHEMA_ROWS = 8


def _seed_schema_history(table: Any, rows: int = _SCHEMA_ROWS, pipeline_id: str = SCHEMA_WALK_PIPELINE_ID) -> None:
    """Seed ``rows`` oversized ``schema_extract`` state rows, oldest first.

    ``timestamp`` is ``PipelineIndex``'s sort key (``data-stack.ts:51-56``) and the
    filler is what forces the 1 MB page boundary — the same technique
    :func:`_seed_multipage` uses on the executions table, and
    ``test_stats_characterization.py``'s before that.

    ``extracted_schema`` is where the filler goes because that is the attribute a real
    ``schema_extract`` row carries at size, and the 1 MB ceiling is measured against
    the bytes DynamoDB *reads* rather than the one attribute the projection returns.
    """
    for n in range(rows):
        table.put_item(
            Item={
                "run_id": f"run-schema-{n}",
                "step_name": "schema_extract",
                "pipeline_id": pipeline_id,
                "timestamp": f"2026-08-{10 + n:02d}T00:00:00.000000+00:00",
                "schema_hash": f"hash-{n}",
                "extracted_schema": _THIRD_OF_A_PAGE,
            }
        )


def _seed_run_carrying(table: Any, schema_hash: str, *, run_id: str = "run-2026-08-24-00-00-00-schemaver") -> None:
    """One execution row on :data:`SCHEMA_WALK_PIPELINE_ID` carrying ``schema_hash``."""
    table.put_item(
        Item=_row(
            run_id,
            "2026-08-24T00:00:00.000000+00:00",
            "SUCCEEDED",
            pipeline_id=SCHEMA_WALK_PIPELINE_ID,
            schema_hash=schema_hash,
        )
    )


class TestSchemaVersionMapIsPaginated:
    """R6.1: a partition larger than one read page yields a **complete** version map.

    The defect: ``PipelineIndex`` was queried with a ``FilterExpression``, no ``Limit``
    and **no ``LastEvaluatedKey`` follow**, while the comment above it claimed it read
    "ALL distinct schema hashes". DynamoDB applies a ``FilterExpression`` *after* the
    1 MB page read, so the map was silently the first page's worth.

    This is ``LGR-9a35b2d-2``'s defect **literally** — same mechanism, a filter with a
    discarded continuation token — and not merely its consequence class. That record
    exempted this file's executions probe precisely because the probe carries no
    ``FilterExpression``; this query does.

    **Validates: p48 Requirements 6.1**
    """

    def test_a_hash_beyond_the_first_page_still_gets_its_version(self, runs_tables):
        """The defect, inverted: eight schema rows over three pages, newest hash used.

        Before 6.1 this row carried **no** ``schema_version`` key at all — the hash was
        real, the row was real, and the map simply did not reach it.
        """
        _seed_schema_history(runs_tables["state_table"])
        _seed_run_carrying(runs_tables["executions_table"], f"hash-{_SCHEMA_ROWS - 1}")

        body = _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)

        assert body["executions"][0]["schema_version"] == _SCHEMA_ROWS, (
            "the newest hash is the eighth distinct one, so it is version 8; a first-page-only "
            "read leaves this key absent entirely"
        )
        assert body["pagination"]["partial_counts"] == [], "a completed walk truncated nothing"

    def test_every_version_on_every_page_is_the_ordinal_of_first_appearance(self, runs_tables):
        """Numbering is a property of the partition, walked in full.

        Asserted across all eight hashes rather than one, because a walk that
        accumulated pages in the wrong order — or restarted its counter per page —
        would still get hash 1 right.
        """
        _seed_schema_history(runs_tables["state_table"])
        for n in range(_SCHEMA_ROWS):
            _seed_run_carrying(
                runs_tables["executions_table"],
                f"hash-{n}",
                run_id=f"run-2026-08-{10 + n:02d}-00-00-00-schemaver",
            )

        body = _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID, page_size=_SCHEMA_ROWS)

        versions = {e["schema_hash"]: e["schema_version"] for e in body["executions"]}
        assert versions == {f"hash-{n}": n + 1 for n in range(_SCHEMA_ROWS)}

    def test_a_page_the_filter_empties_does_not_end_the_walk(self, runs_tables):
        """``LGR-eee8ac8-3``'s case, on the index that record reasoned about.

        "An all-filtered page returns empty with a ``LastEvaluatedKey``." Oversized rows
        of a *different* ``step_name`` fill page one, so the filter removes everything
        on it while the token still points at more — the shape a loop that stopped on
        "this page yielded nothing" would get wrong.

        **Four noise rows, not three, and the number is load-bearing.** At 300 KB each,
        three fit in one page alongside the small ``schema_extract`` row, so the
        matching row would arrive on page one and the test would pass without the
        continuation being followed at all — verified by mutation: with three, this
        assertion held even with the ``LastEvaluatedKey`` follow removed. A fourth
        pushes the boundary ahead of the matching row.
        """
        state_table = runs_tables["state_table"]
        for n in range(4):
            state_table.put_item(
                Item={
                    "run_id": f"run-noise-{n}",
                    "step_name": "run_pipeline",
                    "pipeline_id": SCHEMA_WALK_PIPELINE_ID,
                    "timestamp": f"2026-08-0{n + 1}T00:00:00.000000+00:00",
                    "output_data": _THIRD_OF_A_PAGE,
                }
            )
        state_table.put_item(
            Item={
                "run_id": "run-schema-behind-the-noise",
                "step_name": "schema_extract",
                "pipeline_id": SCHEMA_WALK_PIPELINE_ID,
                "timestamp": "2026-08-20T00:00:00.000000+00:00",
                "schema_hash": "hash-behind-the-noise",
            }
        )
        _seed_run_carrying(runs_tables["executions_table"], "hash-behind-the-noise")

        body = _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)

        assert body["executions"][0]["schema_version"] == 1
        assert body["pagination"]["partial_counts"] == []


class TestSchemaVersionWalkIsBounded:
    """R6.1: the bound binds, and says so in ``partial_counts``.

    The deadline is moved rather than waited on — ``monkeypatch`` to ``0.0``, never a
    ``sleep`` — which is how tasks 3.3, 4.3 and 5.6 all exercise theirs.

    Both shares are shortened because :func:`_schema_version_walk_deadline` clamps its
    instant to be **no earlier** than the filter walk's. That clamp is deliberate (a
    schema deadline behind the filter walk's would reinstate the single-page read
    whenever the filter walk used its full share), so moving one figure alone correctly
    does nothing, and this test would silently stop binding if written that way.

    **Validates: p48 Requirements 6.1**
    """

    @staticmethod
    def _shorten(monkeypatch: Any) -> None:
        monkeypatch.setattr(runs_handler, "_FILTER_WALK_TIME_SHARE_SECONDS", 0.0)
        monkeypatch.setattr(runs_handler, "_SCHEMA_VERSION_WALK_TIME_SHARE_SECONDS", 0.0)

    def test_a_bound_walk_names_schema_version_and_nothing_else(self, runs_tables, monkeypatch):
        """The marker is observable, and it can appear alone.

        Alone matters: the count walk is untouched by this deadline, so an exact total
        beside a short version map is a real state and the list has to be able to
        express it. It also pins the ordering — ``schema_version`` sits between
        ``executions`` and the count keys.

        **AMENDED: the seeded run now carries the LAST hash rather than the first.** The
        walk takes the caller's wanted set and stops once every hash on the page has been
        resolved, so a run carrying ``hash-0`` — the oldest, resolved on the walk's first
        page — is now answered completely however short the deadline is, and correctly
        reports nothing. That is the narrowing, not a lost assertion: see
        :meth:`test_a_bound_that_resolves_every_wanted_hash_names_nothing`, which pins
        the other half. To make a bound *bind* the run has to want something the bound
        walk cannot reach, which is what the last hash is.
        """
        _seed_schema_history(runs_tables["state_table"])
        _seed_run_carrying(runs_tables["executions_table"], f"hash-{_SCHEMA_ROWS - 1}")
        self._shorten(monkeypatch)

        pagination = _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)["pagination"]

        assert pagination["partial_counts"] == ["schema_version"]
        assert pagination["total_count"] == 1, "the count walk keeps its own share and stays exact"

    def test_a_bound_that_resolves_every_wanted_hash_names_nothing(self, runs_tables, monkeypatch):
        """The narrowing, asserted as its own property.

        ``complete`` means "no hash the caller asked about went unresolved because a
        bound stopped the walk" — not "the whole partition was read". With the deadline
        set to fire immediately, a run whose hash first appears on the walk's first page
        still gets its version, and ``partial_counts`` stays empty because nothing the
        response could have shown is missing.

        This is what the change buys and it is the client-visible contract preserved
        exactly: "named ⇒ may be missing" still holds, and the old form reported
        may-be-missing for a row whose version was right there in the body. An unmarked
        could-not-check reported as checked-and-found-none is the defect
        (``LGR-b2f79ef-14``); a *marked* could-not-check over an answer that was found is
        the mirror-image noise, and it trains a reader to ignore the marker.
        """
        _seed_schema_history(runs_tables["state_table"])
        _seed_run_carrying(runs_tables["executions_table"], "hash-0")
        self._shorten(monkeypatch)

        body = _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)

        assert body["executions"][0]["schema_version"] == 1, (
            "the oldest hash's ordinal is settled on the first page read, so a deadline behind "
            "that read cannot take it away"
        )
        assert body["pagination"]["partial_counts"] == [], (
            "nothing the response could have shown is missing, so the marker must stay silent"
        )

    def test_a_page_wanting_no_hash_at_all_is_complete_without_reading(self, runs_tables, monkeypatch):
        """An empty wanted set is satisfied before the first read.

        A page of skips carries no ``schema_hash`` anywhere, so there is nothing to
        resolve and nothing that could be missing. The distinction that matters is
        between an empty wanted set and ``None`` (resolve everything): collapsing them
        would send this page on a full-partition walk to number hashes no row could
        render.
        """
        _seed_schema_history(runs_tables["state_table"])
        table = runs_tables["executions_table"]
        table.put_item(
            Item=_row(
                "skipped-2026-08-24-00-00-00-nohash",
                "2026-08-24T00:00:00.000000+00:00",
                "SKIPPED",
                pipeline_id=SCHEMA_WALK_PIPELINE_ID,
            )
        )
        self._shorten(monkeypatch)
        calls = _spy_queries(runs_tables["state_table"], monkeypatch)

        body = _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)

        assert body["pagination"]["partial_counts"] == []
        assert calls == [], "no schema_extract read at all when the page names no hash"

    def test_a_short_map_loses_entries_and_never_renumbers_them(self, runs_tables, monkeypatch):
        """Truncation produces a **missing** version, never a wrong one.

        The property this rests on: the walk reads ``timestamp`` ascending and a version
        is the ordinal of a hash's first appearance, so a truncated read is a *prefix*
        and every ordinal inside a prefix equals its ordinal over the whole partition.
        Asserted on both sides of the cut in one response — the first hash keeps
        version 1, the last hash has no key — because a test that only checked the
        absence could not tell a short map from a renumbered one.
        """
        _seed_schema_history(runs_tables["state_table"])
        _seed_run_carrying(runs_tables["executions_table"], "hash-0", run_id="run-2026-08-24-00-00-01-first")
        _seed_run_carrying(
            runs_tables["executions_table"],
            f"hash-{_SCHEMA_ROWS - 1}",
            run_id="run-2026-08-24-00-00-02-last",
        )
        self._shorten(monkeypatch)

        body = _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)

        by_hash = {e["schema_hash"]: e for e in body["executions"]}
        assert by_hash["hash-0"]["schema_version"] == 1, "a number that survives the cut is still right"
        assert "schema_version" not in by_hash[f"hash-{_SCHEMA_ROWS - 1}"], (
            "a hash whose first appearance sat past the cut is absent from the map, so the row "
            "carries no version — which is what partial_counts is there to qualify"
        )
        assert body["pagination"]["partial_counts"] == ["schema_version"]

    def test_the_page_cap_is_the_backstop_when_the_clock_is_not(self, runs_tables, monkeypatch):
        """The cap binds independently of the deadline (``LGR-eee8ac8-3``'s spin case).

        A page cap of one with the deadline left alone isolates the cap: nothing here
        depends on wall clock at all.
        """
        _seed_schema_history(runs_tables["state_table"])
        _seed_run_carrying(runs_tables["executions_table"], f"hash-{_SCHEMA_ROWS - 1}")
        monkeypatch.setattr(runs_handler, "_MAX_SCHEMA_VERSION_PAGES", 1)

        body = _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)

        assert body["pagination"]["partial_counts"] == ["schema_version"]
        assert "schema_version" not in body["executions"][0]


class TestSchemaVersionReadStillDegrades:
    """The read must never fail the request — the task's explicit constraint.

    ``LGR-eee8ac8-4``'s principle (bookkeeping about a run must never fail it) applied
    to a read rather than a write, and the same shape the count's own inner ``try``
    already has: a failed enrichment must not discard a page of correctly-ordered rows
    and render "this pipeline has no runs".

    **Validates: p48 Requirements 6.1**
    """

    def test_a_failed_schema_read_yields_no_version_and_a_200(self, runs_tables, monkeypatch):
        """Rows survive, ordered, with no ``schema_version`` — and the failure is named.

        Naming it is the change: the absence used to be indistinguishable from a run
        whose schema was never extracted, which is the unmarked "could-not-check"
        ``LGR-b2f79ef-14``'s reasoning forbids.
        """
        _seed_schema_history(runs_tables["state_table"])
        _seed_run_carrying(runs_tables["executions_table"], "hash-0")

        def _boom(**_kwargs: Any) -> Any:
            raise RuntimeError("ProvisionedThroughputExceededException")

        monkeypatch.setattr(runs_tables["state_table"], "query", _boom)

        response = _raw_call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["executions"]) == 1, "the page of rows survives a failed enrichment"
        assert "schema_version" not in body["executions"][0]
        assert body["pagination"]["partial_counts"] == ["schema_version"]
        assert body["pagination"]["total_count"] == 1, "and the count is untouched by it"

    def test_a_pipeline_with_no_schema_history_is_not_reported_as_partial(self, runs_tables):
        """An empty partition is a complete answer, not a truncated one.

        The bound is checked only after establishing there is more to read, so a
        pipeline that has extracted no schema reports ``[]`` — "there is no version"
        rather than "there may be one I could not see".
        """
        _seed_run_carrying(runs_tables["executions_table"], "hash-never-extracted")

        body = _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)

        assert "schema_version" not in body["executions"][0]
        assert body["pagination"]["partial_counts"] == []


class TestSchemaVersionEnrichmentCostIsStillPageBound:
    """CP6 at the schema walk: the ``batch_get_item`` key count is unchanged.

    The walk's *page* count is a function of the ``schema_extract`` partition, which is
    what the bound is for; the enrichment key count is a function of ``page_size``
    alone, which is what CP6 asserts. The two must not be confused, so the property is
    pinned here beside the read that could have broken it.

    **Validates: p48 Requirements 2.4**
    """

    def test_the_walk_adds_no_batch_get_keys(self, runs_tables, monkeypatch):
        """Three pages of schema history, still one page of runs, still 4 keys per run."""
        _seed_schema_history(runs_tables["state_table"])
        _seed_run_carrying(runs_tables["executions_table"], "hash-0")
        batch_calls: list[dict[str, Any]] = []
        original = runs_handler.dynamodb.batch_get_item

        def _spy(**kwargs: Any) -> Any:
            batch_calls.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(runs_handler.dynamodb, "batch_get_item", _spy)

        _call(pipeline_id=SCHEMA_WALK_PIPELINE_ID)

        keys = [k for call in batch_calls for request in call["RequestItems"].values() for k in request["Keys"]]
        assert len(keys) == 4, "4 keys for the one run on the page, whatever the schema walk cost"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
