# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the two executions-table reads inside ``get_run_details``.

p38 task 5.6. ``get_run_details`` (``handlers/pipelines_runs.py:459``) reads the
executions table **twice**, for two different jobs, with two different bounds.
Keeping them apart is the whole subject of this file.

**Read A — the current-step injection**, a keyed ``get_item`` on
``{pipeline_id, run_id}`` projecting ``status, current_step, updated_at``. It
injects the step the execution is on so an in-flight run's step surface is not
stuck at whatever last wrote a state row. This is ``R1.3``.

**Read A used to be a Query** with ``ScanIndexForward=False, Limit=5``, scanned in
Python for a matching ``run_id`` — so it found the run only when that run was
among the pipeline's five lexicographically-highest ids. p48 task 6.3 (C8, R6.3)
replaced it with the keyed read, because that bound was never correct even for
this job: the base table's sort key is ``run_id``, not time, and ``skipped-`` /
``retry-`` prefixes outrank ``run-``, so the window could exclude a genuinely
in-flight run. ``tests/unit/lambdas/dashboard_api/test_run_details_keyed_executions_lookup.py``
is where that property is pinned; this file's concern is unchanged — that the two
reads stay separable.

**Read B — the retry lineage, and since p48 task 6.2 the run's ``completed_at``
too** (``TestCompletedAtRidesReadB`` at the foot of this file) — a keyed
``get_item`` on ``{pipeline_id, run_id}``
projecting ``step_logs, attempts, attempt_count, retry_source_run_id``. This is
``R5.1`` / ``R5.2``.

``test_lineage_arrives_for_a_run_read_a_cannot_find`` is the assertion that pins
the difference — Read A has no row to find, Read B still answers.

**The two reads are the same key and are still two reads, deliberately.**
``LGR-8e34f37-3`` (**template / ratified**) pins the three lineage keys to be
assigned inside Read B's own ``try``, so a read that raised omits them rather than
defaulting them. Merging the two would put one ``except`` in charge of two
unrelated degradations. Because they now share a key, the fixture routes them by
``ProjectionExpression`` instead of by table method.

Line references are to ``handlers/pipelines_runs.py`` as it stood when this file
was written; ``get_run_details`` is a long function under active change by
several specs at once, so treat them as a starting point and the quoted code as
the identifier.

**Why ``R1.3`` is the substantive part of this task.** Under the continuation
model a retry executes and reports under the Original_Run's id (``design.md``
§4.1), so there is no second run page for an in-flight retry to appear on. The
original run's page *is* the retry's page, and Read A is the mechanism that makes
the retry's progress visible there. Nothing else in the suite covers it: a search
of ``tests/`` for ``current_step`` before this file returned no matches, so every
existing test of this function leaves Read A returning zero items and the whole
injection block unexercised.

**Deliberately not duplicated here.**
``tests/contracts/api_responses/test_run_details_contract.py``
(``TestRunDetailsRetryLineageContract``, task 5.5) already pins the *shape* of
Read B on the wire: all three keys present on a successful read,
present-and-empty on a row with no lineage, recorded lineage verbatim,
``attempt_count`` ``Decimal``→``int``, the five-node cap, the exact
``ProjectionExpression``, ``retried_by_run_id`` absent, and ``load_outcome``
coexisting. Those are contract assertions and they stay there. What is here
instead is the behaviour the contract layer cannot reach, because every test in
that file leaves Read A empty: the injection itself, and the two reads
succeeding and failing **independently** of each other.

**The error path asserts absence, not emptiness.** The three lineage
assignments sit *inside* the ``try`` (``:837-839``), so a row that was read gets
all three keys — empty there is knowledge — and a read that **raised** gets none
of them. ``LGR-99050e6-2`` (**template / ratified**) is the rule: an absent
counter "is reported as unknown rather than as zero". So
``body["attempts"] == []`` after a raising ``get_item`` is the *prohibited*
shape; the correct assertion is ``"attempts" not in body``.

No moto, no boto3 client mock: the module-level table objects are replaced with
``MagicMock``s, the pattern the sibling readers' tests in this directory already
use (``test_run_details_load_outcome.py``,
``test_run_details_approval_parsing.py``, ``test_run_details_not_found_prop.py``).
``AWS_REGION`` is deliberately **not** set here — ``tests/unit/conftest.py`` pins
it by assignment (``LGR-3e45e65-1``, **template / ratified**), and a module-level
``setdefault`` would be a no-op that reads as if it decided something.

Validates: Requirements 1.3, 5.1, 5.2
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path / env setup — same shape as test_run_details_load_outcome.py, which
# covers a sibling concern in the same function.
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(LAMBDA_DIR), os.path.abspath(SHARED_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")

RUN_ID = "run-2026-08-09-15-38-19-5c5f5f8a"
PIPELINE_ID = "pipe-lineage-001"

#: The three keys the lineage read emits. Named as a set so the error-path test
#: asserts about all three at once rather than about whichever one it remembered.
LINEAGE_KEYS = frozenset({"attempts", "attempt_count", "retry_source_run_id"})

#: Fixture-dispatch labels for the two executions reads. Both are ``get_item`` on
#: the same key since p48 task 6.3, so they are told apart by projection.
_READ_A = "read_a_injection"
_READ_B = "read_b_lineage"

#: ``TASK_STEPS`` in execution order, spelled out so the expectations below read
#: as claims about the pipeline rather than as lookups into the module under
#: test. Asserted against the real constant in
#: ``test_the_step_order_this_file_assumes_is_the_shared_one`` — a hand-written
#: copy that nothing checks is ``LGR-28b4d75-5``'s failure mode.
STEP_ORDER = (
    "file_validation",
    "schema_extract",
    "schema_compare",
    "schema_mapping",
    "create_code",
    "test_code",
    "create_infrastructure",
    "create_glue_pipeline",
    "create_quality_checks",
    "run_pipeline",
)


def _parse(response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return response["statusCode"], json.loads(response["body"])


def _state_row(step_name: str, *, status: str = "success", sequence: int | None = None) -> dict[str, Any]:
    """A genuine Step_Row as ``state_table.query`` returns it.

    ``original_pipeline_id`` is carried because Read A keys on
    ``original_pipeline_id or pipeline_id`` (``:596``) — without it ``pid`` is
    empty, the query never runs, and an injection test would pass for the wrong
    reason.
    """
    seq = STEP_ORDER.index(step_name) + 1 if sequence is None else sequence
    return {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "execution_id": "exec-001",
        "status": status,
        "timestamp": f"2026-08-09T15:{seq:02d}:00+00:00",
        "step_sequence": seq,
    }


def _executions_row(
    *,
    run_id: str = RUN_ID,
    status: str = "RUNNING",
    current_step: str = "",
    updated_at: str = "2026-08-09T15:51:30+00:00",
) -> dict[str, Any]:
    """A row as Read A's Query returns it: ``status`` plus ``current_step``.

    ``execution_updater`` writes both on its first touch of a run
    (``execution_updater/lambda_function.py:143-152``), so a row carrying a
    ``current_step`` is a row a step has reported on.
    """
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": status,
        "current_step": current_step,
        "updated_at": updated_at,
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

    # Defaults are the "nothing to say" answers. Every one of them is a
    # *successful* call returning no items — which is a different thing from a
    # call that raises, and that difference is what the error-path test is about.
    mock_state.query.return_value = {"Items": []}
    mock_approvals.query.return_value = {"Items": []}
    mock_executions.query.return_value = {"Items": []}
    mock_pipelines.get_item.return_value = {}
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    # Both reads are now ``get_item`` on the same key (p48 task 6.3), so one
    # ``return_value`` would serve each of them the other's row. The dispatcher
    # routes on ``ProjectionExpression`` — which is what a real table does, since
    # each read only ever sees the attributes it asked for. A shared
    # ``return_value`` would let a Read A assertion pass off Read B's fixture and
    # vice versa; ``LGR-28b4d75-5`` (**template / ratified**) is the principle,
    # that a fixture must not be able to fake past the producer.
    # ``Key`` / ``ProjectionExpression`` are boto3's own kwarg spellings.
    def _dispatch_get_item(*, Key, ProjectionExpression=None, **_kw):
        which = _READ_B if ProjectionExpression and "attempts" in ProjectionExpression else _READ_A
        seeded = _patch_tables.seeded[which]
        if isinstance(seeded, Exception):
            raise seeded
        row = seeded(Key) if callable(seeded) else seeded
        return {"Item": dict(row)} if row else {}

    mock_executions.get_item.side_effect = _dispatch_get_item
    _patch_tables.seeded = {_READ_A: None, _READ_B: None}

    for mod in (_common, _runs):
        monkeypatch.setattr(mod, "state_table", mock_state)
        monkeypatch.setattr(mod, "approvals_table", mock_approvals)
        monkeypatch.setattr(mod, "executions_table", mock_executions)
        monkeypatch.setattr(mod, "pipelines_table", mock_pipelines)
        monkeypatch.setattr(mod, "dynamodb", mock_dynamodb)

    # Log fetching is covered elsewhere and would reach CloudWatch.
    monkeypatch.setattr(_runs, "get_step_logs", lambda *a, **kw: [])

    _patch_tables.state = mock_state
    _patch_tables.approvals = mock_approvals
    _patch_tables.executions = mock_executions
    # Exposed by p48 task 6.4: ``pipeline_key`` used to be assigned only when this
    # read returned an ``Item``, so the tests for it have to vary this read —
    # including making it raise, which is what a run with no
    # ``original_pipeline_id`` provokes from a real table.
    _patch_tables.pipelines = mock_pipelines
    _patch_tables.get_run_details = _runs.get_run_details
    yield


def _set_state_rows(rows: list[dict[str, Any]]) -> None:
    _patch_tables.state.query.return_value = {"Items": rows}


def _set_read_a(rows: list[dict[str, Any]]) -> None:
    """Seed the table Read A reads, as a *keyed* lookup (p48 task 6.3).

    Still takes a list, because what a caller wants to express is "these are the
    rows the pipeline's executions partition holds". Read A now resolves its own
    run's row by key out of that set instead of taking the five
    lexicographically-highest ``run_id``s and scanning them in Python, so a list
    holding no row for ``RUN_ID`` models "this run has no executions row" rather
    than "this run fell out of the window".
    """
    by_key = {(row["pipeline_id"], row["run_id"]): row for row in rows}
    _patch_tables.seeded[_READ_A] = lambda key: by_key.get((key["pipeline_id"], key["run_id"]))


def _set_read_b(**attributes: Any) -> None:
    """Seed Read B — the keyed ``get_item`` that carries the retry lineage.

    Called with nothing it is a *successful* read of a row with no lineage on it.
    """
    _patch_tables.seeded[_READ_B] = dict(attributes)


def _fail_read_b(error: Exception) -> None:
    """Read B raises. The handler logs at debug and continues.

    Only Read B: the dispatcher in the fixture routes by projection, so this
    leaves Read A answering. That independence is the subject of
    ``test_a_raising_read_b_does_not_cost_the_injected_current_step`` and it is
    the reason the fixture cannot be one shared ``side_effect``.
    """
    _patch_tables.seeded[_READ_B] = error


def _set_pending_approval(approval_type: str) -> None:
    _patch_tables.approvals.query.return_value = {
        "Items": [{"approval_type": approval_type, "status": "pending", "run_id": RUN_ID}]
    }


def _get_details(run_id: str = RUN_ID) -> tuple[int, dict[str, Any]]:
    return _parse(_patch_tables.get_run_details(run_id))


def _step(body: dict[str, Any], step_name: str) -> dict[str, Any] | None:
    return next((s for s in body["steps"] if s["step_name"] == step_name), None)


def test_the_step_order_this_file_assumes_is_the_shared_one():
    """``STEP_ORDER`` above is the same tuple the reader injects from.

    The expectations in this file name steps by hand — "the step after
    ``test_code`` is ``create_infrastructure``" — which is only meaningful while
    that is true of ``step_names.TASK_STEPS``. Pinned rather than trusted:
    ``LGR-28b4d75-5`` (**template / ratified**) is the rule that a hand-written
    copy of a producer's data cannot notice the producer moving.
    """
    from step_names import TASK_STEPS

    assert tuple(str(step) for step in TASK_STEPS) == STEP_ORDER


# ===========================================================================
# R1.3 — Read A: the step an execution is on reaches the response
# ===========================================================================


class TestCurrentStepInjection:
    """``R1.3`` — while an execution is in flight, the run's step surface shows
    that execution's steps.

    The state table only holds rows for steps that have **reported**, so the step
    actually executing is missing from it by construction (``:584-586``). Read A
    supplies it. Two branches, both exercised below (``:666-689``):

    * the execution's ``current_step`` has no state row ⇒ that step is injected;
    * it has one ⇒ the step *after* it is injected.
    """

    def test_a_step_with_no_state_row_is_injected_as_running(self):
        """First branch (``:675-676``): the run is on ``create_code``, which has
        not reported, so ``create_code`` itself is what is in flight."""
        _set_state_rows(
            [
                _state_row("file_validation"),
                _state_row("schema_extract"),
                _state_row("schema_compare"),
                _state_row("schema_mapping"),
            ]
        )
        _set_read_a([_executions_row(current_step="create_code")])

        status, body = _get_details()

        assert status == 200
        injected = _step(body, "create_code")
        assert injected is not None, "the step the execution is on must reach the response"
        assert injected["status"] == "running"
        assert injected["timestamp"] == "2026-08-09T15:51:30+00:00", "stamped from the row's updated_at"
        assert body["status"] == "RUNNING"

    def test_the_step_after_a_reported_current_step_is_injected(self):
        """Second branch (``:671-674``): ``create_code`` has reported, so the
        pipeline has moved on to the next step in the shared order."""
        _set_state_rows(
            [
                _state_row("file_validation"),
                _state_row("schema_extract"),
                _state_row("schema_compare"),
                _state_row("schema_mapping"),
                _state_row("create_code"),
            ]
        )
        _set_read_a([_executions_row(current_step="create_code")])

        _, body = _get_details()

        injected = _step(body, "test_code")
        assert injected is not None
        assert injected["status"] == "running"
        assert _step(body, "create_code")["status"] == "success", "the reported step keeps its own status"

    def test_an_in_flight_retry_shows_its_own_progress_on_the_original_runs_page(self):
        """The reason ``R1.3`` is in this spec at all.

        Under the continuation model there is no second run and no second page
        (``design.md`` §4.1): a retry executes and reports under the
        Original_Run's id, so **this** page is where its progress has to appear.
        The run below failed at ``test_code`` on attempt 1; the retry re-ran
        ``create_code`` and ``test_code`` — overwriting those rows, which are
        keyed ``(run_id, step_name)`` — and is now inside
        ``create_infrastructure``, a step attempt 1 never reached and which has
        therefore never written a row.

        Without Read A the page would end at ``test_code`` and the user watching
        their retry would see a run that had stopped. With it the page shows the
        step the retry is actually in, and the run reads ``RUNNING`` rather than
        carrying attempt 1's failure.
        """
        _set_state_rows(
            [
                # Served from the Resume_Cache on the retry — never re-executed
                # (requirements.md Introduction).
                _state_row("file_validation"),
                _state_row("schema_extract"),
                _state_row("schema_compare"),
                _state_row("schema_mapping"),
                _state_row("create_code"),
                # Rewritten by the retry: FAILED on attempt 1, success now.
                _state_row("test_code"),
            ]
        )
        _set_read_a([_executions_row(current_step="test_code")])
        _set_read_b(attempts=[{"attempt": 1, "status": "FAILED", "failed_step": "test_code"}])

        status, body = _get_details()

        assert status == 200
        in_flight = _step(body, "create_infrastructure")
        assert in_flight is not None, "the retry's current step must reach the original run's page"
        assert in_flight["status"] == "running"
        assert body["status"] == "RUNNING"
        # The step surface is this execution's, in order, and stops where the
        # execution has got to.
        assert [s["step_name"] for s in body["steps"]] == list(STEP_ORDER[:7])
        # And the prior attempt is still readable beside it (R5.1) — the two
        # answers arrive from two different reads and neither displaces the other.
        assert body["attempts"][0]["failed_step"] == "test_code"

    def test_a_terminal_execution_injects_nothing(self):
        """``:609`` gates injection on an active status, and the gate is load-bearing.

        The row below is in exactly the shape that *would* inject — a
        ``current_step`` that has reported, with an unreported step after it — and
        differs from the passing cases above only in its status. Were the gate
        removed, ``test_code`` would be added as ``running`` on a run that has
        stopped: a claim about a live execution, made about a dead one.
        """
        _set_state_rows([_state_row("schema_mapping"), _state_row("create_code")])
        _set_read_a([_executions_row(status="FAILED", current_step="create_code")])

        _, body = _get_details()

        assert [s["step_name"] for s in body["steps"]] == ["schema_mapping", "create_code"]
        assert not [s for s in body["steps"] if s["status"] == "running"]

    def test_a_pending_approval_injects_a_waiting_step_and_no_running_one(self):
        """``:666`` — a paused pipeline is not a running one.

        The gate matters for ``R1.3``'s honesty as much as the injection does:
        claiming ``test_code`` is *running* while the run is parked on a
        ``code_generation`` approval would be a false statement about the
        execution, not a harmless default. ``code_generation`` maps to
        ``test_code`` because that gate reviews code **and** test results
        (``:629-637``).
        """
        _set_state_rows(
            [
                _state_row("schema_mapping"),
                _state_row("create_code"),
            ]
        )
        _set_read_a([_executions_row(current_step="create_code")])
        _set_pending_approval("code_generation")

        _, body = _get_details()

        waiting = _step(body, "test_code")
        assert waiting is not None
        assert waiting["status"] == "waiting_approval"
        assert not [s for s in body["steps"] if s["status"] == "running"]
        assert body["status"] == "WAITING_APPROVAL"


# ===========================================================================
# R5.1 / R5.2 — Read B, and why it is not Read A
# ===========================================================================


class TestLineageIsASeparateRead:
    """The two reads answer and fail independently, and lineage needs its own.

    ``design.md`` §4.3 forbade sourcing lineage from Read A, and the reason it
    gave was Read A's ``ScanIndexForward=False, Limit=5`` window: for a run no
    longer among its pipeline's five highest ``run_id``s it matched nothing **and
    raised nothing**, so lineage read that way disappeared on an older retried
    run. p48 task 6.3 removed that window — Read A is now keyed too, so the
    original hazard is gone.

    The separation still holds, and for a reason the window was never the whole
    of: ``LGR-8e34f37-3`` (**template / ratified**) requires the three lineage
    keys to be assigned inside Read B's own ``try``, so the two reads must be
    able to fail independently of each other. One read serving both would put a
    single ``except`` in charge of two different degradations.
    """

    def test_lineage_arrives_for_a_run_read_a_cannot_find(self):
        """The assertion that separates the two reads.

        Read A's partition is seeded with five *other* runs and no row for this
        one — a run with state rows but no executions row, which is now the only
        way Read A comes up empty on a run that exists. Read B is keyed on
        ``{pipeline_id, run_id}`` and answers anyway. ``R5.1`` / ``R5.2`` hold for
        a run whose executions row is missing, which is the requirement.
        """
        _set_state_rows([_state_row("test_code", status="failed")])
        _set_read_a([_executions_row(run_id=f"run-2026-09-0{n}-newer-0000000{n}") for n in range(1, 6)])
        _set_read_b(
            attempts=[
                {
                    "attempt": 1,
                    "status": "FAILED",
                    "steps_status": {"test_code": "failed"},
                    "started_at": "2026-08-09T15:38:19+00:00",
                    "failed_step": "test_code",
                }
            ],
            attempt_count=Decimal("1"),
            retry_source_run_id=RUN_ID,
        )

        status, body = _get_details()

        assert status == 200
        # Read A missed — nothing was injected, and no error was raised either.
        assert [s["step_name"] for s in body["steps"]] == ["test_code"]
        # Read B answered regardless.
        assert body["attempts"][0]["failed_step"] == "test_code"
        assert body["attempt_count"] == 1
        assert body["retry_source_run_id"] == RUN_ID

    def test_the_two_reads_answer_independently_on_one_body(self):
        """Both seeded, both present: the injected current step from Read A and
        the recorded attempts from Read B coexist.

        The contract test (task 5.5) cannot make this assertion — every test in
        that file leaves Read A returning no items, so its bodies carry lineage
        and never an injected step.
        """
        _set_state_rows([_state_row("schema_mapping"), _state_row("create_code")])
        _set_read_a([_executions_row(current_step="create_code")])
        _set_read_b(attempts=[{"attempt": 1, "status": "FAILED"}], attempt_count=Decimal("2"))

        _, body = _get_details()

        assert _step(body, "test_code")["status"] == "running"
        assert body["attempt_count"] == 2
        assert len(body["attempts"]) == 1


class TestAFailedLineageReadIsAbsentNotWrong:
    """``LGR-99050e6-2`` (**template / ratified**) — absent is reported as
    unknown, never as a default.

    The three lineage assignments are *inside* the ``try`` (``:837-839``), which
    is what makes "the row records no attempts" and "we could not find out"
    two distinguishable answers rather than one.
    """

    def test_a_raising_read_b_omits_all_three_keys_and_still_returns_200(self):
        """``"attempts" not in body``, not ``body["attempts"] == []``.

        An empty list would claim the row records no attempts — a claim a read
        that never completed does not support. ``attempt_count: 0`` is the same
        error with a number on it.
        """
        _set_state_rows([_state_row("test_code", status="failed")])
        _fail_read_b(Exception("DynamoDB unavailable"))

        status, body = _get_details()

        assert status == 200, "a failed lineage read is not a failed request"
        for key in sorted(LINEAGE_KEYS):
            assert key not in body, f"{key} must be absent when the lineage read fails, not defaulted"

    def test_a_raising_read_b_does_not_cost_the_injected_current_step(self):
        """The two reads fail independently, in the ``R1.3`` direction.

        Read A has already run by the time Read B is attempted, so an in-flight
        run whose lineage cannot be read still shows the step it is on. Without
        this, one unavailable ``get_item`` would silently truncate the step
        surface of every running run.
        """
        _set_state_rows([_state_row("schema_mapping"), _state_row("create_code")])
        _set_read_a([_executions_row(current_step="create_code")])
        _fail_read_b(Exception("DynamoDB unavailable"))

        status, body = _get_details()

        assert status == 200
        assert _step(body, "test_code")["status"] == "running"
        assert body["status"] == "RUNNING"
        assert LINEAGE_KEYS.isdisjoint(body.keys())

    def test_a_read_row_with_no_lineage_is_a_different_body_from_a_raising_read(self):
        """The two cases asserted against each other rather than one at a time.

        Row read, no lineage ⇒ the keys are present and empty, because the row
        genuinely records nothing. Read raised ⇒ the keys are gone. A caller that
        cannot tell these apart cannot report either honestly.
        """
        _set_state_rows([_state_row("test_code", status="failed")])

        _set_read_b(step_logs={})
        read_ok = _get_details()[1]

        _fail_read_b(Exception("DynamoDB unavailable"))
        read_failed = _get_details()[1]

        assert set(read_ok.keys()) >= LINEAGE_KEYS
        assert read_ok["attempts"] == []
        assert LINEAGE_KEYS.isdisjoint(read_failed.keys())


# ═══════════════════════════════════════════════════════════════════════════
# R8 — ``completed_at`` rides Read B, and which read it rides is the decision
# ═══════════════════════════════════════════════════════════════════════════


class TestCompletedAtRidesReadB:
    """p48 task 6.2 (design §C7, ``R8.1``/``R8.2``/``R8.3``).

    **The defect.** Observed live on ``run-2026-08-24-13-48-26-4fccdf03``
    (the validation account, re-confirmed 2026-08-24): the executions row carries
    ``completed_at = 2026-08-24T13:55:19.496708+00:00`` and ``status =
    SUCCEEDED``, while the run-detail panel rendered ``Completed: —``. The data
    existed and the UI read the right field name
    (``RunMetadataPanel.tsx:56`` reads ``runDetail.completed_at``). The API
    dropped it: ``get_run_details`` assigned ten keys and that was not one.

    **Why this class lives in *this* file.** C7 had two possible sources and
    the choice between them is the whole substance of the task, so the tests
    belong where the two reads can be told apart. This file's fixture routes
    ``get_item`` by ``ProjectionExpression``, so Read A and Read B can be seeded
    with *different* rows — which is what makes "the value came from Read B"
    an assertion rather than an assumption.

    **Reachability did not decide it, and this is measured.** The p48 task text
    suggested that if Read A were not reached for a finished run, that would
    settle the question. It is reached: the ``get_item`` is unconditional inside
    its ``if pid:``, and only the *use* of its result is gated on the three
    active statuses. ``test_read_a_is_reached_for_a_finished_run`` pins that,
    because the argument for Read B has to stand on its real grounds rather than
    on a false claim about reachability.

    **The real grounds** — and one plausible ground that is *not* among them.
    Both reads sit in a ``try`` that omits on failure, so "Read A degrades to
    silent absence and Read B does not" is **false**; it was in an earlier draft
    of this reasoning and mutation testing removed it.

    * Read A's ``ProjectionExpression`` carries an in-code fence — it "must not
      become a second source for anything else the response carries (do-NOT 9)"
      — and ``test_run_details_keyed_executions_lookup.py`` already asserts that
      projection is exactly its three names.
    * Read A's ``try`` wraps the whole next-step computation, so a
      ``completed_at`` assigned there would have its presence made contingent on
      the approvals query and the step-order arithmetic succeeding. Absence would
      mean "something in the injection failed" rather than "the completion time
      could not be read". Read B's ``try`` holds one ``get_item`` and its
      sibling assignments, so absence there means one thing.
    * The **no-row** case is where the two genuinely differ, and
      ``test_a_run_with_no_executions_row_still_reports_completion_as_empty``
      pins it: Read A guards on ``if ex:`` and would omit the key, while Read B
      assigns off ``get("Item", {})`` and yields ``""`` — the present-and-empty
      treatment its three neighbours already have. Sourcing from Read A would
      have given ``completed_at`` a fourth, inconsistent semantics on one body.

    ``LGR-8e34f37-3`` (**template / ratified**) is what makes the resulting three
    states ratified rather than incidental, under ``LGR-99050e6-2``'s
    (**template / ratified**) "absent is reported as unknown, never as zero".

    **R8.3 is a "do not change it" requirement, and it is met by the frontend
    being untouched.** ``RunMetadataPanel.tsx:56`` is
    ``runDetail.completed_at ? formatTimestamp(...) : '—'`` — a truthiness test,
    so absent, ``null`` and ``""`` all render ``—`` and an in-flight run can
    never show an invented time. Nothing in this task changes that component;
    ``test_a_run_in_flight_yields_a_falsy_completed_at`` pins the API side of it,
    which is that the value the component receives for an unfinished run is one
    the ``? :`` treats as absent.
    """

    def test_a_completed_run_gets_its_completion_time(self):
        """``R8.1`` — the observed defect, as the assertion that it is fixed.

        Seeded with the live row's own value so the test is about the reported
        run rather than about a contrived one.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([_executions_row(status="SUCCEEDED")])
        _set_read_b(completed_at="2026-08-24T13:55:19.496708+00:00")

        status, body = _get_details()

        assert status == 200
        assert body["completed_at"] == "2026-08-24T13:55:19.496708+00:00"

    def test_read_a_is_reached_for_a_finished_run(self):
        """The crux, verified rather than reasoned from the docstring.

        Read A's block reads "inject the currently running step", which invites
        the assumption that it does not run for a finished run. It does — the
        ``get_item`` is unconditional inside ``if pid:``; only the injection
        logic is gated. So Read A *could* have supplied ``completed_at``, and the
        reason it does not is the fence and the failure semantics, not
        reachability. Pinned so the recorded reasoning cannot drift into the
        easier, wrong claim.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([_executions_row(status="SUCCEEDED")])
        _set_read_b()

        _get_details()

        projections = [
            call.kwargs.get("ProjectionExpression", "") for call in _patch_tables.executions.get_item.call_args_list
        ]
        assert any("current_step" in p for p in projections), (
            "Read A must still fire for a finished run — the injection is gated, the read is not"
        )

    def test_completed_at_does_not_come_from_read_a(self):
        """do-NOT 9's in-code fence, as an assertion.

        Read A is seeded with a row carrying a ``completed_at`` **that differs
        from Read B's**, and Read B is seeded with none. If Read A's projection
        were widened to carry the value, the response would show Read A's
        string; because it is not, the response shows the empty Read B answer.
        The fixture routes by projection, so this cannot pass by the two reads
        sharing one seeded row.
        """
        _set_state_rows([_state_row("run_pipeline")])
        read_a_row = _executions_row(status="SUCCEEDED")
        read_a_row["completed_at"] = "1999-01-01T00:00:00+00:00"
        _set_read_a([read_a_row])
        _set_read_b()

        _, body = _get_details()

        assert body["completed_at"] != "1999-01-01T00:00:00+00:00", (
            "completed_at must not be sourced from the injection read — its projection is fenced by do-NOT 9"
        )
        assert body["completed_at"] == ""

    def test_the_lineage_projection_carries_completed_at(self):
        """The projection is the mechanism, so it is asserted directly.

        Every value assertion above would still pass against a mock whose
        projection had been narrowed back, while a real table returned an
        ``Item`` with no ``completed_at`` on it. This is the one assertion here
        that a hand-made fixture cannot fake past — ``LGR-28b4d75-5``
        (**template / ratified**), whose lesson is that a producer's own output
        cannot be its own oracle.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([_executions_row(status="SUCCEEDED")])
        _set_read_b()

        _get_details()

        read_b_calls = [
            call.kwargs
            for call in _patch_tables.executions.get_item.call_args_list
            if "attempts" in call.kwargs.get("ProjectionExpression", "")
        ]
        assert len(read_b_calls) == 1, "Read B must still be exactly one keyed read"
        assert read_b_calls[0]["ProjectionExpression"] == (
            "step_logs, attempts, attempt_count, retry_source_run_id, completed_at"
        )
        assert read_b_calls[0]["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}

    def test_a_run_in_flight_yields_a_falsy_completed_at(self):
        """``R8.3`` — the API side of "keep rendering ``—``".

        A row that was read and records no completion is present-and-empty: the
        row genuinely says nothing about a completion, which is knowledge.
        ``""`` is falsy, so ``RunMetadataPanel``'s ``? :`` renders ``—`` and no
        time is invented. Asserted as falsiness rather than as ``== ""`` because
        what the component depends on is the truthiness, and pinning the exact
        spelling would make a later, more honest ``None`` read as a regression —
        the same reasoning the lineage contract gives for not pinning ``0``.
        """
        _set_state_rows([_state_row("create_code")])
        _set_read_a([_executions_row(status="RUNNING", current_step="create_code")])
        _set_read_b(step_logs={})

        status, body = _get_details()

        assert status == 200
        assert "completed_at" in body, "a row that was read reports its (absent) completion as empty"
        assert not body["completed_at"], "an unfinished run must not carry a time"

    def test_a_run_with_no_executions_row_still_reports_completion_as_empty(self):
        """The case where the two candidate reads genuinely differ (ground 3).

        Read B's ``get_item`` comes back with no ``Item`` at all. That is a
        *successful* read of a run that records no completion, so the key is
        present and falsy — the same treatment the three lineage keys get, pinned
        by the contract's
        ``test_all_three_keys_are_present_when_the_read_succeeds``. Read A guards
        on ``if ex:`` and would have omitted the key here, giving ``completed_at``
        a fourth semantics that none of its neighbours has. This test is what
        makes that difference a guarantee rather than an argument.

        **Both** reads are seeded empty, deliberately: that is what a run with no
        executions row actually looks like, and it is what makes the assertion
        discriminating. Seeded with a row on Read A only, the test would pass
        against a Read-A-sourced ``completed_at`` too and prove nothing.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([])
        # Read B seeded with nothing at all — no Item, not an error.
        _patch_tables.seeded[_READ_B] = None

        status, body = _get_details()

        assert status == 200
        assert "completed_at" in body, "no Item is a successful read, so the key is present"
        assert body["completed_at"] == ""
        assert set(body.keys()) >= LINEAGE_KEYS, "the neighbours get the same treatment"

    def test_a_failed_read_omits_completed_at_rather_than_calling_it_unfinished(self):
        """``LGR-99050e6-2`` — absent is reported as unknown, never defaulted.

        Assigned inside Read B's ``try``, a raised read leaves the key **off** the
        body, which is a different fact from the empty string the tests above
        assert. ``LGR-8e34f37-3`` (**template / ratified**) is what ratifies that
        for keys assigned in this ``try`` specifically.

        ``completed_at`` is checked together with the three lineage keys because
        they now share one ``try`` and therefore one degradation.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([_executions_row(status="SUCCEEDED")])
        _fail_read_b(Exception("DynamoDB unavailable"))

        status, body = _get_details()

        assert status == 200, "a failed lineage read is not a failed request"
        assert "completed_at" not in body, (
            "completed_at must be absent when the read fails, not defaulted to an unfinished-looking empty"
        )
        assert LINEAGE_KEYS.isdisjoint(body.keys())

    def test_the_read_row_case_and_the_failed_read_case_are_different_bodies(self):
        """The two absences asserted against each other rather than one at a time.

        Row read, no completion ⇒ the key is present and falsy. Read raised ⇒ the
        key is gone. A caller that cannot tell these apart cannot report either
        honestly, which is the whole content of the C7 source choice.
        """
        _set_state_rows([_state_row("create_code")])
        _set_read_a([_executions_row(status="RUNNING", current_step="create_code")])
        _set_read_b(step_logs={})
        read_ok = _get_details()[1]

        _fail_read_b(Exception("DynamoDB unavailable"))
        read_failed = _get_details()[1]

        assert "completed_at" in read_ok
        assert not read_ok["completed_at"]
        assert "completed_at" not in read_failed

    def test_completed_at_is_not_a_second_status_authority(self):
        """``R8.2`` / do-NOT 9 — R8 adds a timestamp, not a third opinion.

        The executions row says ``FAILED`` and carries a ``completed_at``; every
        step row says ``success``. The derived status must still follow the step
        rows (``run_pipeline == success`` ⇒ ``SUCCEEDED``) — the timestamp
        reaching the response must not drag the row's stored ``status`` along
        with it, and the presence of a completion time must not itself imply a
        terminal status.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([_executions_row(status="FAILED")])
        _set_read_b(completed_at="2026-08-24T13:55:19.496708+00:00")

        _, body = _get_details()

        assert body["status"] == "SUCCEEDED", "status stays derived from step rows"
        assert body["completed_at"] == "2026-08-24T13:55:19.496708+00:00"

    def test_a_completion_time_does_not_make_a_running_run_look_finished(self):
        """The same guarantee in the other direction, so the test above cannot
        pass by the derivation coincidentally agreeing.

        A row carrying a ``completed_at`` on a run whose steps are still running
        keeps the derived status ``RUNNING``. The timestamp is reported as the
        row states it; it is not evidence about the run's status.
        """
        _set_state_rows([_state_row("create_code", status="running")])
        _set_read_a([_executions_row(status="RUNNING", current_step="create_code")])
        _set_read_b(completed_at="2026-08-24T13:55:19.496708+00:00")

        _, body = _get_details()

        assert body["status"] == "RUNNING"
        assert body["completed_at"] == "2026-08-24T13:55:19.496708+00:00"

    def test_no_response_key_other_than_completed_at_was_added(self):
        """Scope, as an assertion. Task 6.4 owns the enumeration of the row's
        other dropped attributes (design §O9), so this task must add exactly one
        key and no more.

        **6.4 has since settled it and these three stay withheld** — see
        ``TestTheRowsOtherAttributesStayWithheld`` at the end of this file, which
        widens the same claim from these three names to all 19 dropped attributes
        and records the reason class for each. This test is kept rather than
        folded into that one: it is scoped to *task 6.2's* change and pins that
        widening the projection by one name added exactly one key, which is a
        different claim from "these 19 are withheld".

        Read B's row is seeded with three attributes 6.4 might later decide to
        forward — ``ai_model_used``, ``file_name`` and ``trigger``, the three O9
        names — and none of them may appear on the body as a side effect of
        widening this projection by one name. ``trigger`` additionally must not
        appear for its own recorded reason: §5 delta D5 of p38 withheld it
        deliberately, because emitting it would make ``StepFlowVisualization``'s
        skipped-step display reachable.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([_executions_row(status="SUCCEEDED")])
        _set_read_b(
            completed_at="2026-08-24T13:55:19.496708+00:00",
            ai_model_used="anthropic.claude-3-5-sonnet",
            file_name="customer_data_6.csv",
            trigger="s3_upload",
        )

        _, body = _get_details()

        for withheld in ("ai_model_used", "file_name", "trigger"):
            assert withheld not in body, f"{withheld} is task 6.4's question, not this task's"


# ═══════════════════════════════════════════════════════════════════════════
# O9 — the executions row enumerated against ``RunDetail``, both directions
# ═══════════════════════════════════════════════════════════════════════════

#: Every attribute the live executions table carries that the run-detail body
#: does **not** emit. Enumerated over the whole attribute space rather than over
#: one row: a full scan of the live table (the validation account, 2026-08-25) returned 61
#: rows carrying **28** distinct attributes, of which these 19 are dropped. Task
#: 6.2 measured one ``run-`` row, saw 14 attributes and reported 8 dropped — that
#: row simply did not carry the other 11, which is exactly the accident-of-looking
#: design §O9 asked about.
#:
#: The four reasons they are dropped are recorded at the projection in
#: ``pipelines_runs.py``; the point of the set HERE is that widening either
#: executions read must not start forwarding them wholesale. Seeded onto both
#: reads by ``test_no_dropped_row_attribute_leaks_onto_the_body`` below, which is
#: the one assertion a ``run_details.update(row)`` could not survive.
WITHHELD_ROW_ATTRIBUTES = frozenset(
    {
        # Structurally unreachable — ``skipped-``-only, and a skip writes no state
        # rows, so the reader 404s before this read (verified: none of the 3 live
        # ``skipped-`` run ids appears in the state table).
        "content_fingerprint",
        "previous_run_id",
        "skip_detail",
        "skip_reason",
        "source_key",
        "triggered_by",
        "ttl",
        # A second status authority — do-NOT 9, R8.2.
        "pending_approval_id",
        "pending_approval_type",
        # Withheld by an existing record — p38 §5 delta D5, and ``LGR-453f12d-5``
        # for the forward identifier (also pinned by the contract file's
        # ``test_the_body_carries_no_forward_identifier``).
        "trigger",
        "retried_by_run_id",
        # No declared consumer — ``RunDetail`` declares none of these and no
        # run-detail component reads them (``LGR-5aad762-1``).
        "ai_model_used",
        "current_step",
        "execution_name",
        "file_name",
        "lineage",
        "schema_hash",
        "steps_status",
        "updated_at",
    }
)


class TestPipelineKeyIsNotContingentOnASecondTable:
    """p48 task 6.4 — the direction-2 defect O9's enumeration actually found.

    Direction 1 is C7's class: the row has a field and the response drops it.
    Direction 2 is ``R3.1``'s: the **frontend type declares a field the API does
    not always send**, so the consumer reads ``undefined`` with nothing to warn
    it. Task 4.2 found the same shape one type over — ``PipelineExecution``
    declares ``execution_id`` required and the run-list handler never emits it —
    and reported it up rather than asserting it, because it sat outside that
    task's envelope. This is that report landing.

    **The defect.** ``RunDetail`` (``types.ts:105``) declares
    ``pipeline_key: string`` — required, no ``?``. ``get_run_details`` assigned it
    only inside the ``try`` around the pipelines-table ``get_item``, and only when
    that row still existed. ``RunMetadataPanel.tsx:48`` renders it with no guard
    (``<Box>{runDetail.pipeline_key}</Box>``), unlike ``started_at`` at ``:52`` and
    ``completed_at`` at ``:56``, which both fall back to ``'—'``. It does not guard
    because the type promises it never has to. So the Pipeline field rendered
    blank for the runs whose pipeline had been deleted.

    **Measured, not reasoned about** (the validation account, 2026-08-25): 10 of the 31
    ``original_pipeline_id`` values referenced by state rows have no row in the
    pipelines table, covering 13 runs; 2 further runs carry no
    ``original_pipeline_id`` at all, so the ``get_item`` is called with an empty
    key attribute and raises.

    **Why deriving it is not a default.** ``LGR-7561acb-7`` and ``LGR-44d601e-1``
    hold that a value nothing established is *omitted*, never defaulted. This one
    is established: the read fetched the row AT key ``original_pipeline_id`` and
    returned that row's ``pipeline_id`` — the partition key it asked with — so the
    answer was always the key. The derivation is also the one this repo already
    uses under this key name at ``handlers/files.py:37`` and
    ``handlers/approvals.py:295-296``; ``get_run_details`` was the only producer
    that made ``pipeline_key`` contingent on a second table.
    """

    def test_the_pipeline_is_named_even_though_its_row_is_gone(self):
        """The 13-run live case. The fixture's ``pipelines_table.get_item``
        returns no ``Item`` by default, which IS the deleted-pipeline case — so
        every other test in this file was already exercising it, and none noticed.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([_executions_row(status="SUCCEEDED")])
        _set_read_b()

        status, body = _get_details()

        assert status == 200
        assert body["pipeline_key"] == PIPELINE_ID

    def test_a_raising_pipelines_read_does_not_cost_the_pipeline_key(self):
        """The 2-run live case, where ``original_pipeline_id`` is absent and the
        keyed read raises on an empty key attribute. Modelled as the raise itself
        rather than as an empty id, because that is what DynamoDB does.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([_executions_row(status="SUCCEEDED")])
        _set_read_b()
        _patch_tables.pipelines.get_item.side_effect = Exception("ValidationException")

        status, body = _get_details()

        assert status == 200
        assert body["pipeline_key"] == PIPELINE_ID

    def test_it_falls_back_to_pipeline_id_when_no_original_is_recorded(self):
        """``original_pipeline_id or pipeline_id`` — ``files.py:37``'s order, and
        the order matters: the original is the pipeline the run belongs to, and
        ``pipeline_id`` on a state row can be the run-suffixed variant."""
        rows = [_state_row("run_pipeline")]
        del rows[0]["original_pipeline_id"]
        rows[0]["pipeline_id"] = "pipe-from-the-row-only"
        _set_state_rows(rows)
        _set_read_b()

        status, body = _get_details()

        assert status == 200
        assert body["pipeline_key"] == "pipe-from-the-row-only"

    def test_it_is_present_and_empty_rather_than_absent_when_no_row_names_a_pipeline(self):
        """A row that names no pipeline at all — the live ``metrics_cache#*`` /
        ``cost-*`` shapes reach this handler by id. ``""`` keeps the required
        declaration true; ``RunMetadataPanel`` renders the same blank it renders
        for ``undefined``, so nothing about the UI changes for this case. What
        changes is that no consumer can be handed a missing required key.
        """
        row = _state_row("run_pipeline")
        del row["original_pipeline_id"]
        del row["pipeline_id"]
        _set_state_rows([row])
        _set_read_b()

        status, body = _get_details()

        assert status == 200
        assert body["pipeline_key"] == ""

    def test_the_pipelines_row_no_longer_decides_the_key_when_it_does_exist(self):
        """Same value either way, which is what makes the change safe: the read
        was only ever confirming the key it asked with. Pinned so a later reader
        does not "restore" the row-sourced assignment believing it differs."""
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_b()
        _patch_tables.pipelines.get_item.return_value = {
            "Item": {"pipeline_id": PIPELINE_ID, "pipeline_name": "Latest Testing"}
        }

        status, body = _get_details()

        assert status == 200
        assert body["pipeline_key"] == PIPELINE_ID
        assert body["pipeline_name"] == "Latest Testing"

    def test_pipeline_name_stays_withheld_when_its_row_is_gone(self):
        """The counter-case, and the reason this task did not "fix" both keys.
        Nothing but the pipelines row establishes a display name, so its absence
        is real not-established and omitting it is correct (``LGR-7561acb-7``).
        ``RunDetail`` does not declare it either, so no consumer is promised it.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_b()

        _, body = _get_details()

        assert "pipeline_name" not in body


class TestTheRowsOtherAttributesStayWithheld:
    """The enumeration itself, as a fence (design §O9).

    O9 asked whether ``completed_at`` was the only dropped field. It was not —
    19 attributes are dropped, in four reason classes recorded at the projection.
    None of them is forwarded, and this class is what keeps that true: the two
    executions reads are narrow on purpose, and the failure mode a later change
    would take is forwarding the held ``Item`` wholesale rather than field by
    field.
    """

    def test_no_dropped_row_attribute_leaks_onto_the_body(self):
        """Seeded on **both** reads, because both hold an ``Item`` a careless
        ``update()`` could splat. The mock honours no ``ProjectionExpression``, so
        this models the worst case rather than the projection's own promise —
        which is separately pinned by ``test_the_lineage_projection_carries_completed_at``
        here and by ``test_the_injection_read_asks_only_for_what_it_uses`` in
        ``test_run_details_keyed_executions_lookup.py``.
        """
        extras = dict.fromkeys(sorted(WITHHELD_ROW_ATTRIBUTES), "seeded-value")
        read_a_row = _executions_row(status="SUCCEEDED") | extras
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_a([read_a_row])
        _set_read_b(completed_at="2026-08-24T13:55:19.496708+00:00", **extras)

        status, body = _get_details()

        assert status == 200
        leaked = WITHHELD_ROW_ATTRIBUTES & set(body)
        assert not leaked, f"withheld row attributes reached the body: {sorted(leaked)}"
        # And the four that DO ride this read still arrive, so the fence above is
        # not passing because the reader stopped reading.
        assert body["completed_at"] == "2026-08-24T13:55:19.496708+00:00"
        assert set(body) >= LINEAGE_KEYS

    def test_the_withheld_set_does_not_overlap_what_the_body_promises(self):
        """A guard on the constant, not on the handler. If a later spec decides to
        emit one of these 19 deliberately, it must remove the name from
        ``WITHHELD_ROW_ATTRIBUTES`` — and this assertion is what stops the set
        being left stale and quietly contradicting the body it describes.
        """
        _set_state_rows([_state_row("run_pipeline")])
        _set_read_b()

        _, body = _get_details()

        assert WITHHELD_ROW_ATTRIBUTES.isdisjoint(body)
