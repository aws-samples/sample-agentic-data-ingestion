# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the `GET /pipeline/{id}/executions` envelope, pinned against
the frontend interface (p48 R3.1, R3.2 — design P4).

``get_pipeline_executions`` (``handlers/pipelines_runs.py:403``) returns
``{executions, queued_files, pagination}``, and the ``pagination`` block carries
eight keys on **both** arms — the success arm and the read-failure degrade arm —
so a client never has to branch on which one answered. This file is the first
thing to actually govern that envelope.

**Why "first".** Three files already assert this shape and every one of them
derives its expectations from the producer's own output:

* ``test_executions_response_contract.py`` — ``MagicMock``s the tables, calls the
  handler, asserts on the return. Its docstring *claimed* it validated the
  response "as expected by the PaginatedExecutions TypeScript interface"; it
  never read that file, and it asserted a six-key ``pagination`` block that the
  interface did not declare at all while all four of the fields the interface
  *did* declare (``total_count``, ``page``, ``page_size``, ``has_more``) read
  ``undefined`` in the browser. That docstring is corrected as part of this task.
* ``test_api_contract_conformance_prop.py`` — validates the same handler against
  ``schemas.py:PAGINATED_EXECUTIONS_SCHEMA``, a hand-transcribed JSON Schema.
* ``test_api_contract_props.py:194`` — the same schema, same handler.

None of the three is *wrong*: they pass, they pin real behaviour, and their
canned ``query.return_value`` carries no ``Count``, so the count degrades and
they happen to exercise the ``partial_counts`` marker path. They are left in
place. But they are the mechanism ``LGR-28b4d75-5`` (**template / ratified**)
rejects **by name**, in both of its spellings:

    Rejected transcribing the field list into the test *or into a JSON schema*: a
    test written from the producer's output cannot detect the producer drifting,
    because the output is its own oracle.

So the expectations below are PARSED at test time out of::

    dashboard-react/src/hooks/use-executions.ts

(``PaginatedExecutions`` / ``ExecutionsPagination`` / ``QueuedFile``), which is
the type ``fetchPipelineExecutions`` instantiates ``apiClient.get<…>`` with. Add
a required field to the TypeScript and this test fails until the handler supplies
it; rename one on the wire and it fails until the interface follows.

**The parser is the shared one, and reusing it is mandatory rather than
stylistic.** ``LGR-99050e6-5`` (**template / ratified**, scope
``tests/contracts/api_responses/**``) extracted the reader into ``helpers.py``
rather than copying it per endpoint, because "the copies would have diverged
immediately … and the next contract test would inherit whichever it happened to
copy". ``LGR-cd58706-3`` (**template / ratified**) applied that to the ninth
consumer and said so in as many words — a local copy would have been "the eighth
parser and the first divergence". This file is the **tenth** consumer and adds no
parser: it imports ``load_interfaces``, ``required_fields``, ``declares_null``
and ``matches_json_type``, and needed no widening of any of them (verified by
running the real reader against the file: ``ExecutionsPagination`` parses,
``string | null`` maps to ``(str, NoneType)``, ``string[]`` to ``(list,)``, and
``pagination`` maps to ``dict`` once the sibling interface is named in
:data:`OBJECT_TYPES`).

Semantics pinned here that key-presence alone would miss, because each is a
decision rather than an accident:

* ``next_cursor`` is **always present**, ``null`` on the last page rather than
  omitted, so the envelope's key set stays assertable with ``==``.
* ``partial_counts`` is **always present**, ``[]`` when both counts are totals
  (``LGR-9a35b2d-3``, ``LGR-b2f79ef-14`` — an unmarked "could-not-check" reported
  as "checked and found none" is the defect those records exist to prevent).
* ``total_pages`` is ``0`` for an empty set on **both** arms; task 3.3 reconciled
  the success arm's old literal ``1`` with the degrade arm's ``0``.
* ``has_next`` is ``next_cursor is not None`` — one fact with one spelling, so
  "there is a next page" and "here is how to ask for it" cannot drift.
* the two arms emit the **same key set**. Tasks 3.2 and 3.3 both maintained that
  deliberately and nothing pinned it until now.

Deliberately **not** asserted here, so the omission is a statement rather than an
oversight: ``PipelineExecution``'s required fields. That interface declares
``execution_id`` required (``use-executions.ts:11``), and the handler returns raw
executions rows, which are keyed ``(pipeline_id, run_id)`` and carry no
``execution_id`` at all. That is a real divergence, in a type task 4.1 did not
touch and this task does not own; asserting it would fail this file for a reason
outside R3.1's envelope question, and asserting its *negation* would pin the
divergence. It is reported instead. What this file does assert about the two
element types is that ``executions`` and ``queued_files`` are arrays of the
declared shape, and that ``queued_files`` entries satisfy ``QueuedFile`` — the
handler builds those four keys itself (``pipelines_runs.py:536-546``), so that
one is fully in the envelope's gift.

Validates: Requirements R3.1, R3.2
"""

from __future__ import annotations

import json
import os
import sys
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
# fixture can run — mirrors test_chat_response_contract.py. Every table handle is
# replaced by a mock below; nothing here touches AWS.
for _table_env, _table_name in (
    ("STATE_TABLE", "agentic_data_ingestion_state"),
    ("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"),
    ("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"),
    ("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code"),
    ("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results"),
    ("APPROVALS_TABLE", "agentic_data_ingestion_approvals"),
    ("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"),
    ("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens"),
    ("AUDIT_TABLE", "agentic_data_ingestion_audit_log"),
    ("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces"),
):
    os.environ.setdefault(_table_env, _table_name)

sys.modules.pop("lambda_function", None)

from handlers import pipelines_runs as _runs

from .helpers import declares_null, load_interfaces, matches_json_type, required_fields

#: The consumer this contract is pinned against. ``load_interfaces`` asserts the
#: file exists and that each interface parsed non-empty, so a moved hook fails
#: loudly instead of quietly deriving an empty field set and passing.
FRONTEND_TYPE_FILE = REPO_DIR / "dashboard-react" / "src" / "hooks" / "use-executions.ts"
RESPONSE_TYPE = "PaginatedExecutions"
PAGINATION_TYPE = "ExecutionsPagination"
QUEUED_TYPE = "QueuedFile"
LOST_TYPE = "LostFile"
EXECUTION_TYPE = "PipelineExecution"

#: Named interfaces in scope that serialize to a JSON object. The reader will not
#: guess this — a bare identifier it was not told about raises, so a renamed or
#: mistyped sibling type cannot slip through as "some object". ``LoadOutcome`` is
#: imported into the hook from ``../types`` and is named here because
#: ``PipelineExecution.load_outcome`` declares it.
OBJECT_TYPES = frozenset({PAGINATION_TYPE, EXECUTION_TYPE, QUEUED_TYPE, LOST_TYPE, "LoadOutcome"})

PIPELINE_ID = "pipe-envelope-contract-001"

#: A ``LastEvaluatedKey`` shaped as ``RunsByTimeIndex`` really returns one —
#: verified live on that index. ``encode_cursor`` mints a token only when the key
#: names the partition that was queried, so the ``pipeline_id`` here is load-bearing.
NEXT_PAGE_KEY: dict[str, str] = {
    "pipeline_id": PIPELINE_ID,
    "started_at": "2026-08-20T20:51:00+00:00",
    "run_id": "run-2026-08-20-20-51-00-abcdef01",
}


def _execution_row(run_id: str, *, started_at: str, status: str = "SUCCEEDED") -> dict[str, Any]:
    """An executions row as ``RunsByTimeIndex`` returns it (``ProjectionType.ALL``).

    ``current_step`` is present so the row is not read as a
    Phantom_Execution_Row and dropped before it reaches the envelope — the filter
    at ``pipelines_runs.py:590`` is deliberately positioned and is not this
    file's subject.
    """
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "updated_at": started_at,
        "current_step": "run_pipeline",
        "file_name": "customer_data_1.csv",
    }


def _queued_row(run_id: str, *, started_at: str) -> dict[str, Any]:
    """The ``QUEUED`` placeholder ``_track_queued_file`` writes.

    It reaches the response through the ``queued_files`` split, not through
    ``executions``, which is why ``QueuedFile`` is a separate assertion below.
    """
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": "QUEUED",
        "file_name": "customer_data_6.csv",
        "source_key": "s3://bucket/customer_data_6.csv",
        "started_at": started_at,
        "current_step": "queued_trigger",
    }


@pytest.fixture(scope="module")
def frontend_types() -> dict[str, dict[str, tuple[bool, str]]]:
    """The four interfaces this endpoint's envelope must satisfy, parsed at test time."""
    return load_interfaces(
        FRONTEND_TYPE_FILE, RESPONSE_TYPE, PAGINATION_TYPE, QUEUED_TYPE, LOST_TYPE, EXECUTION_TYPE
    )


@pytest.fixture
def tables(monkeypatch) -> MagicMock:
    """Mock every table handle ``get_pipeline_executions`` reads.

    ``executions_table.query`` serves **two** different reads — the single page
    query and the ``Select="COUNT"`` walk — so the side effect branches on
    ``Select`` rather than on call order. A positional ``side_effect`` list would
    silently mis-attribute responses the moment either read changed position, and
    the count deliberately runs last (``_runs_read_deadline``).
    """
    executions_table = MagicMock()
    state_table = MagicMock()
    approvals_table = MagicMock()
    dynamodb = MagicMock()

    # No schema_extract rows: the schema_version enrichment finds no hashes and
    # assigns nothing. Keeps the envelope the subject.
    state_table.query.return_value = {"Items": []}
    approvals_table.query.return_value = {"Items": []}
    dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    monkeypatch.setattr(_runs, "executions_table", executions_table)
    monkeypatch.setattr(_runs, "state_table", state_table)
    monkeypatch.setattr(_runs, "approvals_table", approvals_table)
    monkeypatch.setattr(_runs, "dynamodb", dynamodb)
    return executions_table


def _wire(
    executions_table: MagicMock,
    *,
    items: list[dict[str, Any]],
    last_evaluated_key: dict[str, str] | None = None,
    count: int | None = None,
    count_truncated: bool = False,
) -> None:
    """Point the mock at one page plus one COUNT answer.

    ``count`` defaults to ``len(items)`` — the honest total for a single-page
    partition. ``count_truncated`` returns a ``LastEvaluatedKey`` on the COUNT
    page so the walk has somewhere to continue to; the deadline is what stops it,
    shortened by the fixture that uses this rather than by waiting on a clock
    (``_count_partition_rows``' own guidance).
    """
    total = len(items) if count is None else count

    def query(**kwargs: Any) -> dict[str, Any]:
        if kwargs.get("Select") == "COUNT":
            answer: dict[str, Any] = {"Count": total}
            if count_truncated:
                answer["LastEvaluatedKey"] = dict(NEXT_PAGE_KEY)
            return answer
        page: dict[str, Any] = {"Items": items}
        if last_evaluated_key is not None:
            page["LastEvaluatedKey"] = dict(last_evaluated_key)
        return page

    executions_table.query.side_effect = query


def _parse(response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Response fixtures — one per envelope state this file pins
# ---------------------------------------------------------------------------
@pytest.fixture
def last_page(tables) -> dict[str, Any]:
    """A full page that is also the last one: no ``LastEvaluatedKey``, exact count."""
    _wire(
        tables,
        items=[
            _execution_row("run-2026-08-24-13-04-01-aaaa1111", started_at="2026-08-24T13:04:01+00:00"),
            _execution_row(
                "skipped-7412636fccb938719673179658c05773", started_at="2026-08-24T13:04:04+00:00", status="SKIPPED"
            ),
        ],
    )
    _, body = _parse(_runs.get_pipeline_executions(PIPELINE_ID))
    return body


@pytest.fixture
def page_with_a_successor(tables) -> dict[str, Any]:
    """A page followed by another, so ``next_cursor`` is a real token."""
    _wire(
        tables,
        items=[_execution_row("run-2026-08-24-13-04-01-aaaa1111", started_at="2026-08-24T13:04:01+00:00")],
        last_evaluated_key=NEXT_PAGE_KEY,
        count=37,
    )
    _, body = _parse(_runs.get_pipeline_executions(PIPELINE_ID, page_size=10))
    return body


@pytest.fixture
def page_with_a_queued_placeholder(tables) -> dict[str, Any]:
    """One run plus one ``QUEUED`` row, which splits into ``queued_files``."""
    _wire(
        tables,
        items=[
            _execution_row("run-2026-08-24-13-04-01-aaaa1111", started_at="2026-08-24T13:04:01+00:00"),
            _queued_row("queued-ee4ce802", started_at="2026-08-24T13:27:00+00:00"),
        ],
    )
    _, body = _parse(_runs.get_pipeline_executions(PIPELINE_ID))
    return body


@pytest.fixture
def empty_partition(tables) -> dict[str, Any]:
    """A pipeline with no runs at all: the shape must still hold."""
    _wire(tables, items=[])
    _, body = _parse(_runs.get_pipeline_executions(PIPELINE_ID))
    return body


@pytest.fixture
def bounded_count(tables, monkeypatch) -> dict[str, Any]:
    """The count walk stopped by its deadline, so both derived numbers are floors.

    The budget is shortened rather than the clock advanced — the deadline is
    derived at call time from ``_RUNS_TIME_BUDGET_SECONDS``, and
    ``_count_partition_rows`` checks it only *after* reading a page, so the first
    page is always read and the bound applies to continuing.
    """
    monkeypatch.setattr(_runs, "_RUNS_TIME_BUDGET_SECONDS", 0.0)
    _wire(
        tables,
        items=[_execution_row("run-2026-08-24-13-04-01-aaaa1111", started_at="2026-08-24T13:04:01+00:00")],
        count=12,
        count_truncated=True,
    )
    _, body = _parse(_runs.get_pipeline_executions(PIPELINE_ID))
    return body


@pytest.fixture
def read_failure(tables) -> dict[str, Any]:
    """The degrade arm: the page query raises, and the response is still a 200.

    A read failure must not become a 500 — that is the function's standing
    contract — so this arm is a first-class envelope state, not an error case.
    """
    tables.query.side_effect = RuntimeError("RunsByTimeIndex is still backfilling")
    status, body = _parse(_runs.get_pipeline_executions(PIPELINE_ID))
    assert status == 200, "a read failure degrades the response; it never becomes a 500"
    return body


# ---------------------------------------------------------------------------
# The oracle itself
# ---------------------------------------------------------------------------
class TestTheOracleIsNotVacuous:
    """Guards on the parse, so no assertion below can pass by checking nothing."""

    def test_the_envelope_declares_exactly_the_four_keys_this_file_pins(self, frontend_types):
        """A fifth top-level field must fail here rather than pass unnoticed.

        Every other test reads the interface for names and types, so a field
        added to the TypeScript with no assertion here would be invisible. This
        is the one place the *set* is pinned: if the frontend starts reading
        something new, extend this file rather than widening the guard.

        **Amended from three keys to four, deliberately, and the guard did its job.**
        ``lost_files`` was added because ``queued_files`` publishes a Lost_File only
        while its row sits on page 1 of a newest-first index — about an hour on an
        active pipeline, against a 30-day record — so the durable statement needed a
        page-independent array of its own (p54 R10.3,
        ``pipelines_runs._lost_files``). Widening ``queued_files`` instead was refused:
        its four keys are pinned by set equality in
        ``test_split_derive_and_filter_order_characterization.py`` (do-NOT 3) and its
        page-1 scoping is deliberate. Amended in place with the reason at the
        assertion rather than loosened to a subset check, which is this repo's
        convention for a deliberate reversal (``LGR-e59edd7-5``, ``LGR-9e76249-10``):
        the set stays exactly asserted, so the next addition fails here too.
        """
        assert set(frontend_types[RESPONSE_TYPE]) == {
            "executions",
            "queued_files",
            "lost_files",
            "pagination",
        }, (
            f"{RESPONSE_TYPE} in {FRONTEND_TYPE_FILE.name} now declares "
            f"{sorted(frontend_types[RESPONSE_TYPE])}. usePipelineExecutions reads this body directly, so a new "
            f"field is a new contract obligation on GET /pipeline/{{id}}/executions."
        )

    def test_the_lost_file_entry_declares_exactly_the_four_keys_the_producer_sends(self, frontend_types):
        """``LostFile``'s field set, pinned the way ``QueuedFile``'s is.

        **No ``run_id``, and that absence is the assertion.** The row behind an entry
        has a real one, but ``get_run_details`` 404s on it — a placeholder row carries
        no state rows at all — so publishing it would hand this surface something that
        looks navigable and is not (p48 O2, p54 do-NOT 3 / do-NOT 15). A ``run_id``
        appearing here is the regression this pins, not a widening to wave through.
        """
        assert set(frontend_types[LOST_TYPE]) == {
            "file_name",
            "source_key",
            "lost_at",
            "lost_reason",
        }, (
            f"{LOST_TYPE} now declares {sorted(frontend_types[LOST_TYPE])}. "
            f"pipelines_runs._lost_files builds each entry from four keys and deliberately omits run_id."
        )

    def test_the_pagination_block_declares_exactly_the_eight_keys_this_file_pins(self, frontend_types):
        """The eight-key block, as both handler arms emit it."""
        assert set(frontend_types[PAGINATION_TYPE]) == {
            "page",
            "page_size",
            "total_count",
            "total_pages",
            "has_next",
            "has_prev",
            "next_cursor",
            "partial_counts",
        }, (
            f"{PAGINATION_TYPE} now declares {sorted(frontend_types[PAGINATION_TYPE])}. Both arms of "
            f"get_pipeline_executions emit one fixed key set (pipelines_runs.py:778, :811) precisely so a "
            f"client never branches on which answered — add the new key to both arms and to this file."
        )

    def test_every_pagination_field_is_required(self, frontend_types):
        """No ``?`` anywhere in the block, which is what makes the diffs below strong.

        Stated rather than left implicit: if a field is ever made optional, the
        required-field diff silently stops covering it and the per-field
        assertions become the weaker statement.
        """
        assert required_fields(frontend_types[PAGINATION_TYPE]) == set(frontend_types[PAGINATION_TYPE]), (
            f"{PAGINATION_TYPE} now declares optional fields "
            f"{sorted(set(frontend_types[PAGINATION_TYPE]) - required_fields(frontend_types[PAGINATION_TYPE]))}; "
            f"the required-field diff no longer covers them."
        )

    def test_pagination_is_declared_as_the_named_sibling_interface(self, frontend_types):
        """``pagination``'s declared type must still be ``ExecutionsPagination``.

        :data:`OBJECT_TYPES` tells the reader that name serializes to a JSON
        object. Retyped, and every assertion about the block would be checking a
        type nothing declares. The named spelling is also a constraint rather
        than a style choice: ``read_interface`` asserts on any line inside an
        interface that is not ``field[?]: type;``, so a multi-line inline
        ``pagination: { … }`` literal would make this file unwritable.
        """
        _, declared = frontend_types[RESPONSE_TYPE]["pagination"]
        assert declared == PAGINATION_TYPE, (
            f"{RESPONSE_TYPE}.pagination is declared {declared!r}, not {PAGINATION_TYPE!r} — repoint "
            f"PAGINATION_TYPE/OBJECT_TYPES rather than leaving these assertions pinned to a type the "
            f"interface no longer uses."
        )

    def test_next_cursor_is_declared_required_and_nullable(self, frontend_types):
        """ "Always present, ``null`` on the last page" has to be the interface's claim too.

        Asserted rather than trusted, so nobody drops the ``| null`` to quiet a
        type error and turns a documented nullable into an accidental optional.
        """
        required, ts_type = frontend_types[PAGINATION_TYPE]["next_cursor"]
        assert required, f"{PAGINATION_TYPE}.next_cursor became optional; the producer always emits the key"
        assert declares_null(ts_type), (
            f"{PAGINATION_TYPE}.next_cursor is declared {ts_type}, which admits no null. The producer emits "
            f"null on the last page rather than omitting the key, so the union is the true wire shape."
        )

    def test_the_element_types_are_the_ones_declared_on_the_arrays(self, frontend_types):
        """``executions``/``queued_files``/``lost_files`` element types, off the declarations."""
        assert frontend_types[RESPONSE_TYPE]["executions"][1] == f"{EXECUTION_TYPE}[]"
        assert frontend_types[RESPONSE_TYPE]["queued_files"][1] == f"{QUEUED_TYPE}[]"
        # ``LostFile[]`` and NOT ``QueuedFile[]``: the two entries share two field names
        # and mean different things — a queued entry's ``queued_at`` is when the file
        # was deferred, a lost entry's ``lost_at`` is when the loss was recorded — so
        # one type for both would let a surface read either timestamp as the other.
        assert frontend_types[RESPONSE_TYPE]["lost_files"][1] == f"{LOST_TYPE}[]"


# ---------------------------------------------------------------------------
# R3.1 — the success arm satisfies PaginatedExecutions
# ---------------------------------------------------------------------------
class TestSuccessArmSatisfiesTheFrontendType:
    """The envelope a working read produces."""

    def test_every_required_top_level_field_is_present(self, last_page, frontend_types):
        missing = required_fields(frontend_types[RESPONSE_TYPE]) - set(last_page)
        assert not missing, (
            f"Response is missing {sorted(missing)}, which {RESPONSE_TYPE} ({FRONTEND_TYPE_FILE.name}) "
            f"declares required. usePipelineExecutions reads these off the top level of the body."
        )

    def test_top_level_field_types_match_the_frontend_type(self, last_page, frontend_types):
        for field, (required, ts_type) in frontend_types[RESPONSE_TYPE].items():
            if not required and field not in last_page:
                continue
            assert matches_json_type(last_page[field], ts_type, OBJECT_TYPES), (
                f"{RESPONSE_TYPE}.{field} is declared {ts_type}, response carried {type(last_page[field]).__name__}"
            )

    def test_every_required_pagination_field_is_present(self, last_page, frontend_types):
        missing = required_fields(frontend_types[PAGINATION_TYPE]) - set(last_page["pagination"])
        assert not missing, (
            f"pagination is missing {sorted(missing)}, which {PAGINATION_TYPE} "
            f"({FRONTEND_TYPE_FILE.name}) declares required. This is the R3.1 mismatch: before p48 the "
            f"interface declared a flat total_count/page/page_size/has_more and all four read undefined."
        )

    def test_pagination_field_types_match_the_frontend_type(self, last_page, frontend_types):
        pagination = last_page["pagination"]
        for field, (required, ts_type) in frontend_types[PAGINATION_TYPE].items():
            if field not in pagination:
                # A missing *required* field is the previous test's subject and
                # says so there; skipping here keeps this test about types only.
                assert not required, f"{PAGINATION_TYPE}.{field} is required and absent"
                continue
            assert matches_json_type(pagination[field], ts_type, OBJECT_TYPES), (
                f"{PAGINATION_TYPE}.{field} is declared {ts_type}, pagination carried "
                f"{type(pagination[field]).__name__} ({pagination[field]!r})"
            )

    def test_next_cursor_is_present_and_null_on_the_last_page(self, last_page):
        """Present-and-null, never omitted — which is what keeps the key set assertable."""
        pagination = last_page["pagination"]
        assert "next_cursor" in pagination, (
            "next_cursor was omitted on the last page. It is declared required and nullable, and an "
            "optional key makes the envelope's key set unassertable with ==."
        )
        assert pagination["next_cursor"] is None
        assert pagination["has_next"] is False

    def test_next_cursor_is_an_opaque_string_when_another_page_exists(self, page_with_a_successor):
        pagination = page_with_a_successor["pagination"]
        assert isinstance(pagination["next_cursor"], str) and pagination["next_cursor"], (
            f"next_cursor is {pagination['next_cursor']!r}; the page query returned a LastEvaluatedKey, so "
            f"the client needs a token to ask for the next page."
        )

    def test_has_next_is_exactly_whether_a_cursor_was_returned(self, last_page, page_with_a_successor):
        """One fact, one spelling. ``has_next`` is derived from ``next_cursor``.

        Pinned as an equality across both states rather than as two separate
        truth values, so a producer that started deriving ``has_next`` from
        ``"LastEvaluatedKey" in response`` — the same truth value today — cannot
        let the two drift apart later.
        """
        for body in (last_page, page_with_a_successor):
            pagination = body["pagination"]
            assert pagination["has_next"] is (pagination["next_cursor"] is not None), (
                f"has_next={pagination['has_next']!r} disagrees with next_cursor="
                f"{pagination['next_cursor']!r}. 'There is a next page' and 'here is how to ask for it' "
                f"are one fact."
            )

    def test_partial_counts_is_empty_when_both_numbers_are_totals(self, last_page):
        """``[]``, and present — not omitted (``LGR-9a35b2d-3``, ``LGR-b2f79ef-14``)."""
        assert last_page["pagination"]["partial_counts"] == [], (
            f"partial_counts is {last_page['pagination']['partial_counts']!r} on a completed count walk. "
            f"An empty list means 'both numbers above are totals'; naming a key means that number is a floor."
        )

    def test_partial_counts_names_the_derived_numbers_when_the_walk_was_bounded(self, bounded_count):
        """A bound that binds is reported, not hidden.

        The order comes from the producer's own ``_COUNT_DERIVED_KEY_ORDER``, so
        the marker is deterministic and assertable with ``==`` rather than as a
        set membership test.
        """
        assert bounded_count["pagination"]["partial_counts"] == ["total_count", "total_pages"], (
            f"partial_counts is {bounded_count['pagination']['partial_counts']!r} after the count walk hit "
            f"its deadline. An unmarked 'could-not-check' reported as 'checked and found none' is the "
            f"defect LGR-b2f79ef-14 exists to prevent."
        )

    def test_total_pages_is_zero_for_an_empty_partition(self, empty_partition):
        """``ceil(0 / page_size) == 0``, not the old literal ``1``."""
        pagination = empty_partition["pagination"]
        assert pagination["total_count"] == 0
        assert pagination["total_pages"] == 0, (
            f"total_pages is {pagination['total_pages']!r} for an empty partition. The success arm's old "
            f"literal 1 contradicted has_next in the same envelope (R2.3); 0 is what reconciles it with "
            f"the degrade arm."
        )
        assert empty_partition["executions"] == []
        assert empty_partition["queued_files"] == []

    def test_queued_entries_satisfy_the_queued_file_type(self, page_with_a_queued_placeholder, frontend_types):
        """``QUEUED`` rows leave through ``queued_files``, in the declared shape.

        The handler builds these four keys itself rather than passing the row
        through, so unlike ``PipelineExecution`` this element type is entirely in
        the envelope's gift and is asserted in full.
        """
        queued_files = page_with_a_queued_placeholder["queued_files"]
        assert len(queued_files) == 1, f"expected the QUEUED placeholder in queued_files, got {queued_files!r}"
        assert all(row["status"] != "QUEUED" for row in page_with_a_queued_placeholder["executions"]), (
            "a QUEUED placeholder reached `executions`; it belongs to the other array (R3.3 — "
            "get_queued_files reads queued_files off this same body)"
        )
        for idx, row in enumerate(queued_files):
            missing = required_fields(frontend_types[QUEUED_TYPE]) - set(row)
            assert not missing, (
                f"queued_files[{idx}] is missing {sorted(missing)}, which {QUEUED_TYPE} declares required."
            )
            for field, (required, ts_type) in frontend_types[QUEUED_TYPE].items():
                if not required and field not in row:
                    continue
                assert matches_json_type(row[field], ts_type, OBJECT_TYPES), (
                    f"{QUEUED_TYPE}.{field} is declared {ts_type}, queued_files[{idx}] carried "
                    f"{type(row[field]).__name__}"
                )

    def test_executions_entries_are_json_objects_of_the_declared_element_type(self, last_page, frontend_types):
        """The array's element shape, without pinning ``PipelineExecution``'s required set.

        ``PipelineExecution`` declares ``execution_id`` required and the handler
        emits ``run_id`` instead — a real divergence in a type task 4.1 did not
        touch, so it is reported rather than asserted either way (see the module
        docstring). What is asserted is that every element is a JSON object and
        that each field the interface declares AND the row carries matches its
        declared type, which still catches a producer renaming or retyping a
        field the dashboard renders.
        """
        for idx, row in enumerate(last_page["executions"]):
            assert isinstance(row, dict), f"executions[{idx}] is {type(row).__name__}, declared {EXECUTION_TYPE}"
            for field, (_required, ts_type) in frontend_types[EXECUTION_TYPE].items():
                if field not in row:
                    continue
                assert matches_json_type(row[field], ts_type, OBJECT_TYPES), (
                    f"{EXECUTION_TYPE}.{field} is declared {ts_type}, executions[{idx}] carried "
                    f"{type(row[field]).__name__} ({row[field]!r})"
                )


# ---------------------------------------------------------------------------
# R3.1 — the degrade arm satisfies the same type
# ---------------------------------------------------------------------------
class TestDegradeArmSatisfiesTheFrontendType:
    """A failed read still answers 200 with a body the client can parse.

    This arm is the reason the envelope's key set is worth pinning at all: a
    client that had to branch on which arm answered would need to know, and the
    only way it could tell is by a key being missing.
    """

    def test_every_required_top_level_field_is_present(self, read_failure, frontend_types):
        missing = required_fields(frontend_types[RESPONSE_TYPE]) - set(read_failure)
        assert not missing, f"the degrade arm is missing required {RESPONSE_TYPE} fields: {sorted(missing)}"

    def test_top_level_field_types_match_the_frontend_type(self, read_failure, frontend_types):
        for field, (required, ts_type) in frontend_types[RESPONSE_TYPE].items():
            if not required and field not in read_failure:
                continue
            assert matches_json_type(read_failure[field], ts_type, OBJECT_TYPES), (
                f"{RESPONSE_TYPE}.{field} is declared {ts_type}, the degrade arm carried "
                f"{type(read_failure[field]).__name__}"
            )

    def test_every_required_pagination_field_is_present(self, read_failure, frontend_types):
        missing = required_fields(frontend_types[PAGINATION_TYPE]) - set(read_failure["pagination"])
        assert not missing, f"the degrade arm's pagination is missing required fields: {sorted(missing)}"

    def test_pagination_field_types_match_the_frontend_type(self, read_failure, frontend_types):
        pagination = read_failure["pagination"]
        for field, (required, ts_type) in frontend_types[PAGINATION_TYPE].items():
            if field not in pagination:
                assert not required, f"{PAGINATION_TYPE}.{field} is required and absent from the degrade arm"
                continue
            assert matches_json_type(pagination[field], ts_type, OBJECT_TYPES), (
                f"{PAGINATION_TYPE}.{field} is declared {ts_type}, the degrade arm carried "
                f"{type(pagination[field]).__name__} ({pagination[field]!r})"
            )

    def test_total_pages_is_zero_and_marked_as_a_floor(self, read_failure):
        """Both arms answer ``0`` for "no pages"; only here does it mean "I could not look".

        That difference is what ``partial_counts`` reports. Zero on the success
        arm is a total; zero here is a floor, and an unmarked floor would read as
        "I looked and found none".
        """
        pagination = read_failure["pagination"]
        assert pagination["total_count"] == 0
        assert pagination["total_pages"] == 0
        assert pagination["partial_counts"] == ["total_count", "total_pages"], (
            f"the degrade arm reported partial_counts={pagination['partial_counts']!r}. The read failed, so "
            f"neither number was established."
        )

    def test_no_cursor_is_promised_by_a_read_that_failed(self, read_failure):
        pagination = read_failure["pagination"]
        assert pagination["next_cursor"] is None
        assert pagination["has_next"] is False
        assert pagination["has_prev"] is False


# ---------------------------------------------------------------------------
# The both-arms invariant, which nothing pinned until now
# ---------------------------------------------------------------------------
class TestBothArmsEmitOneKeySet:
    """Tasks 3.2 and 3.3 both maintained this deliberately; here it is enforced.

    The key sets are compared against **the interface** as well as against each
    other, so this cannot pass by both arms drifting together.
    """

    def test_the_envelope_key_set_is_identical_on_both_arms(self, last_page, read_failure, frontend_types):
        assert set(last_page) == set(read_failure), (
            f"the two arms emit different top-level keys — success {sorted(last_page)} against degrade "
            f"{sorted(read_failure)}. A client would have to branch on which one answered."
        )
        assert set(last_page) == set(frontend_types[RESPONSE_TYPE]), (
            f"both arms agree on {sorted(last_page)} but {RESPONSE_TYPE} declares "
            f"{sorted(frontend_types[RESPONSE_TYPE])}."
        )

    def test_the_pagination_key_set_is_identical_on_both_arms(self, last_page, read_failure, frontend_types):
        success = set(last_page["pagination"])
        degraded = set(read_failure["pagination"])
        assert success == degraded, (
            f"the two arms emit different pagination keys — success {sorted(success)} against degrade "
            f"{sorted(degraded)}. next_cursor and partial_counts are present on both precisely so this "
            f"set is one set (pipelines_runs.py:795, :821)."
        )
        assert success == set(frontend_types[PAGINATION_TYPE]), (
            f"both arms agree on {sorted(success)} but {PAGINATION_TYPE} declares "
            f"{sorted(frontend_types[PAGINATION_TYPE])}."
        )

    def test_both_arms_agree_on_every_declared_field_type(self, last_page, read_failure, frontend_types):
        """Same key set is not enough: the same key must carry the same declared type.

        A degrade arm that answered ``total_count: None`` would satisfy the key
        comparison above and still break a client calling arithmetic on it.
        """
        for body, arm in ((last_page, "success"), (read_failure, "degrade")):
            pagination = body["pagination"]
            for field, (_required, ts_type) in frontend_types[PAGINATION_TYPE].items():
                # Presence is checked here rather than left to a KeyError so a
                # field renamed on one side names itself in the failure.
                assert field in pagination, (
                    f"{arm} arm: {PAGINATION_TYPE}.{field} is declared but absent from pagination {sorted(pagination)}"
                )
                assert matches_json_type(pagination[field], ts_type, OBJECT_TYPES), (
                    f"{arm} arm: {PAGINATION_TYPE}.{field} is declared {ts_type}, carried "
                    f"{type(pagination[field]).__name__}"
                )
