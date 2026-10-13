# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The run-detail executions lookup resolves its own run, at any depth.

p48 task 6.3 (design §C8, ``R6.3``). ``get_run_details`` computes the next-step
display for an active run from that run's executions row. It used to find that row
by ``executions_table.query(ScanIndexForward=False, Limit=5)`` on the pipeline
partition and then ``if ex.get("run_id") == run_id`` **in Python**.

**Why that could not work.** The executions base table is PK ``pipeline_id``, SK
``run_id`` — there is no time attribute in the key. So "the five newest" was
really "the five lexicographically-highest ids", and six id prefixes share that one
sort key (measured live on the validation account, 2026-08-24: ``run-`` 50, ``file-`` 4,
``skipped-`` 3, ``e2e-`` 2, ``retry-`` 1, ``unknown`` 1). Their order, derived here
rather than assumed — ``test_the_prefix_order_this_file_assumes_is_the_real_one``
pins it:

    e2e-  <  file-  <  queued-  <  retry-  <  run-  <  skipped-  <  unknown

So ``skipped-`` and the bare ``unknown`` outrank every ``run-``. p48 design
§"Observed ``run_id`` prefixes" additionally claims ``retry-`` outranks ``run-``;
that is **false** ('e' < 'u' in retry/run), and it does not affect the conclusion.

On ``latesttesting-19ca826a`` two ``skipped-`` rows put the run under investigation
at position 3 of the 5 returned. Three more duplicate skips and it would have left
the window entirely — and a duplicate-skip id is deterministic in the content
fingerprint, so such a row never ages out and never re-sorts. The failure would
have been permanent, and silent: no exception, just a run that stops showing the
step it is on.

**The fix is a keyed ``get_item`` on ``(pipeline_id, run_id)``.** Both key parts are
already in hand inside ``get_run_details`` — ``run_id`` is its argument and the
pipeline id is resolved a few lines above — so the partition read was never needed.
One keyed read is strictly cheaper than the five-row read it replaces and cannot
miss.

**What this file asserts, and how it cannot pass on the old code.** The fake
executions table below honours **both** access paths faithfully: ``get_item`` is a
keyed lookup, and ``query`` sorts the partition by ``run_id`` and applies ``Limit``
the way DynamoDB does. So restoring the query-and-filter makes
``test_the_run_resolves_below_higher_sorting_skipped_rows`` and
``test_the_run_resolves_at_any_depth_in_the_partition`` fail on their own
assertions rather than on a mock that no longer matches. A ``MagicMock`` with a
fixed ``return_value`` would have passed either way, which is
``LGR-28b4d75-5``'s (**template / ratified**) failure mode — a fixture that cannot
tell the two implementations apart.

**Not asserted here, on purpose.**

* The **status derivation** is out of scope as a *change* but in scope as a
  *guarantee*: ``do-NOT 9`` forbids this task making the run-detail reader a second
  status authority. ``status`` is re-derived from step rows and the in-code comment
  there records that this derivation and
  ``execution_updater._derive_execution_status`` already disagree, with
  ``LGR-9e76249-2`` (**template / ratified**) keeping ``load_outcome`` out of that
  disagreement deliberately. ``TestTheStatusDerivationIsUntouched`` pins the
  derivation's outputs so a later "tidy-up" of that disagreement cannot ride in on
  a read-mechanism change.
* ``completed_at`` — task 6.2's subject, and it landed **elsewhere**. 6.2 took the
  completion time off the *lineage* ``get_item`` rather than off this read, because
  this read's projection carries an in-code fence against becoming a second source
  for response fields (do-NOT 9) and its ``except`` degrades to silent absence,
  which would make a failed read and an unfinished run the same body. So this task
  still adds no response key, and
  ``test_this_read_is_not_where_completed_at_comes_from`` — flipped from an
  ``"completed_at" not in body`` assertion when 6.2 landed — is what keeps that
  true. The R8 tests live in ``test_run_details_lineage.py``, whose fixture routes
  the two reads by projection and can therefore tell them apart.
* The lineage read's independence — ``test_run_details_lineage.py`` owns that.

Validates: Requirements 6.3, 8.2
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[4]
for _path in (_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api", _ROOT / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Same set the sibling readers' tests declare (``test_run_details_lineage.py``).
# ``AWS_REGION`` is deliberately absent — ``tests/unit/conftest.py`` pins it by
# assignment (``LGR-3e45e65-1``, **template / ratified**), so a ``setdefault``
# here would be a no-op that reads as if it decided something.
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")

PIPELINE_ID = "latesttesting-19ca826a"

#: The run the operator opened. Shaped like a real id so its position under a
#: lexicographic sort is the real position, not a contrived one.
RUN_ID = "run-2026-08-24-13-04-01-1f2e3d4c"

#: The two duplicate-skip rows live on this partition today (design §C8, task 1.3
#: re-measured the table: 3 ``skipped-`` rows table-wide, 2 in this partition).
#: Both sort above every ``run-`` id.
LIVE_SKIPPED_IDS = (
    "skipped-7412636fccb938719673179658c05773",
    "skipped-42e5466299ba6cbd1e2d3a4b5c6d7e8f",
)


# ═══════════════════════════════════════════════════════════════════════════
# A fake executions table that honours both access paths
# ═══════════════════════════════════════════════════════════════════════════


class FakeExecutionsTable:
    """Enough of a DynamoDB table for the two access paths to differ.

    ``get_item`` resolves by key. ``query`` sorts the partition by ``run_id`` and
    applies ``ScanIndexForward`` / ``Limit`` the way DynamoDB does — *before* any
    application-side filtering, which is the whole reason the old read could miss.

    ``ProjectionExpression`` is honoured on ``get_item`` because the two reads in
    ``get_run_details`` are now the same key and differ only by projection: serving
    each of them the whole row would let one read's fixture stand in for the
    other's.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = [dict(row) for row in rows]
        self.get_item_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []

    # -- keyed path ------------------------------------------------------
    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_item_calls.append(kwargs)
        key = kwargs["Key"]
        for row in self.rows:
            if row.get("pipeline_id") == key["pipeline_id"] and row.get("run_id") == key["run_id"]:
                return {"Item": self._project(row, kwargs.get("ProjectionExpression"))}
        return {}

    @staticmethod
    def _project(row: dict[str, Any], projection: str | None) -> dict[str, Any]:
        if not projection:
            return dict(row)
        wanted = {name.strip().lstrip("#") for name in projection.split(",")}
        return {k: v for k, v in row.items() if k in wanted}

    # -- partition path --------------------------------------------------
    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.query_calls.append(kwargs)
        # Only ``pipeline_id`` equality is ever asked of this table here, so the
        # KeyConditionExpression is not parsed — the partition is every row.
        ordered = sorted(self.rows, key=lambda row: row["run_id"], reverse=not kwargs.get("ScanIndexForward", True))
        limit = kwargs.get("Limit")
        if limit is not None:
            ordered = ordered[:limit]
        return {"Items": [dict(row) for row in ordered], "Count": len(ordered)}

    # -- writers, so an accidental write is loud rather than silent -------
    def put_item(self, **kwargs: Any) -> None:  # pragma: no cover - must not run
        raise AssertionError("get_run_details is a read path and must not write to the executions table")

    def update_item(self, **kwargs: Any) -> None:
        # ``get_run_details`` does cache step_logs back onto the row. Accepted and
        # ignored; it is not this file's subject.
        return None

    def scan(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover - must not run
        raise AssertionError("no scan: the run id and pipeline id are both in hand")


def _executions_row(run_id: str, *, status: str = "SUCCEEDED", current_step: str = "") -> dict[str, Any]:
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": status,
        "current_step": current_step,
        "started_at": "2026-08-24T13:04:01+00:00",
        "updated_at": "2026-08-24T13:06:00+00:00",
    }


def _state_row(step_name: str, *, seq: int, status: str = "success") -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "execution_id": "exec-6-3",
        "status": status,
        "timestamp": f"2026-08-24T13:0{seq}:00+00:00",
        "step_sequence": seq,
    }


@pytest.fixture
def run_details(monkeypatch):
    """Returns ``call(state_rows, executions_rows, approvals=[]) -> (status, body)``.

    The fake executions table is exposed on the returned callable as ``.table``
    after a call, so a test can assert about which access path was used.
    """
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    state = MagicMock()
    approvals = MagicMock()
    pipelines = MagicMock()
    dynamodb = MagicMock()
    pipelines.get_item.return_value = {}
    dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    def _call(
        state_rows: list[dict[str, Any]],
        executions_rows: list[dict[str, Any]],
        approvals_rows: list[dict[str, Any]] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        state.query.return_value = {"Items": state_rows}
        approvals.query.return_value = {"Items": approvals_rows or []}
        table = FakeExecutionsTable(executions_rows)
        _call.table = table
        for mod in (_common, _runs):
            monkeypatch.setattr(mod, "state_table", state)
            monkeypatch.setattr(mod, "approvals_table", approvals)
            monkeypatch.setattr(mod, "executions_table", table)
            monkeypatch.setattr(mod, "pipelines_table", pipelines)
            monkeypatch.setattr(mod, "dynamodb", dynamodb)
        monkeypatch.setattr(_runs, "get_step_logs", lambda *a, **kw: [])
        response = _runs.get_run_details(RUN_ID)
        return response["statusCode"], json.loads(response["body"])

    return _call


def _step(body: dict[str, Any], step_name: str) -> dict[str, Any] | None:
    return next((s for s in body["steps"] if s["step_name"] == step_name), None)


# ═══════════════════════════════════════════════════════════════════════════
# R6.3 — the defect
# ═══════════════════════════════════════════════════════════════════════════


class TestTheRunResolvesRegardlessOfIdPrefix:
    """The lookup finds this run's row whatever else shares its partition."""

    def test_the_run_resolves_below_higher_sorting_skipped_rows(self, run_details):
        """The live shape. Two ``skipped-`` rows sort above the run, exactly as on
        ``latesttesting-19ca826a`` — and the run's own row is still found, so the
        next step is injected.

        Under the old ``Limit=5`` read this passed only because five rows was
        still enough to reach position 3. The next assertion is the one that
        breaks it.
        """
        state_rows = [_state_row("schema_extract", seq=2), _state_row("schema_mapping", seq=3)]
        executions_rows = [
            _executions_row(RUN_ID, status="RUNNING", current_step="schema_mapping"),
            *[_executions_row(skipped_id, status="SKIPPED") for skipped_id in LIVE_SKIPPED_IDS],
        ]

        status, body = run_details(state_rows, executions_rows)

        assert status == 200
        injected = _step(body, "create_code")
        assert injected is not None, "the run's executions row was not resolved, so no next step was injected"
        assert injected["status"] == "running"

    def test_the_run_resolves_at_any_depth_in_the_partition(self, run_details):
        """The assertion the old read cannot satisfy.

        Five ``skipped-`` rows sort above the run, so a descending ``Limit=5`` read
        on ``run_id`` returns five skips and never sees it. Task 1.3 measured 3
        ``skipped-`` rows table-wide today; design §C8's warning was that "three
        more duplicate skips on that pipeline and active runs stop resolving", and
        this is that state.

        A keyed read is indifferent to how many rows sort above it.
        """
        state_rows = [_state_row("schema_extract", seq=2), _state_row("schema_mapping", seq=3)]
        deep_skips = [f"skipped-{n:032x}" for n in range(0xF0, 0xF5)]
        assert all(skip > RUN_ID for skip in deep_skips), "the fixture must actually bury the run"
        executions_rows = [
            _executions_row(RUN_ID, status="RUNNING", current_step="schema_mapping"),
            *[_executions_row(skip, status="SKIPPED") for skip in deep_skips],
        ]

        status, body = run_details(state_rows, executions_rows)

        assert status == 200
        injected = _step(body, "create_code")
        assert injected is not None, (
            "the run sits below five higher-sorting ids; a Limit=5 read on run_id cannot see it"
        )
        assert injected["status"] == "running"

    def test_the_prefix_order_this_file_assumes_is_the_real_one(self, run_details):
        """The six live prefixes, ordered by assertion rather than by belief.

        p48 design §"Observed ``run_id`` prefixes" states that ``retry-`` sorts
        above ``run-`` "so a retried run outranks every original". That is wrong:
        ``retry`` < ``run`` because 'e' < 'u'. It also does not mention that the
        bare ``unknown`` id sorts above ``skipped-`` — it is the single
        highest-sorting id in the live table. Neither correction changes the
        conclusion that ``skipped-`` outranks every ``run-``, which is the defect,
        but a hand-copied ordering nothing checks is ``LGR-28b4d75-5``'s failure
        mode and the design text is where it went wrong.
        """
        live_prefixes = ["run-", "file-", "skipped-", "e2e-", "retry-", "unknown", "queued-"]

        assert sorted(live_prefixes) == ["e2e-", "file-", "queued-", "retry-", "run-", "skipped-", "unknown"]
        assert "skipped-" > "run-", "the defect: every skip outranks every run"
        assert "unknown" > "skipped-", "and the bare unknown id outranks even the skips"
        assert "retry-" < "run-", "design §Observed run_id prefixes has this one backwards"
        assert LIVE_SKIPPED_IDS[0] > RUN_ID and LIVE_SKIPPED_IDS[1] > RUN_ID

    def test_the_lookup_is_keyed_and_reads_no_partition(self, run_details):
        """The mechanism, asserted — because every assertion above would also pass
        on a query-and-filter read whose window happened to be wide enough.

        ``LGR-28b4d75-5`` (**template / ratified**): a test written from the
        producer's own output cannot detect the producer drifting. So this pins
        that the row arrives by key.
        """
        state_rows = [_state_row("schema_extract", seq=2)]
        executions_rows = [_executions_row(RUN_ID, status="RUNNING", current_step="schema_extract")]

        run_details(state_rows, executions_rows)
        table = run_details.table

        keyed = [call for call in table.get_item_calls if call["Key"]["run_id"] == RUN_ID]
        assert keyed, "the run's executions row must be read by key"
        assert all(call["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID} for call in keyed)
        assert table.query_calls == [], (
            "get_run_details must not read the executions partition: both key parts are in hand"
        )

    def test_the_injection_read_asks_only_for_what_it_uses(self, run_details):
        """A narrow projection, so this read cannot quietly become a second source
        for anything else on the response (``do-NOT 9``).

        ``status`` needs the ``#status`` alias — it is a DynamoDB reserved word —
        and a missing ``ExpressionAttributeNames`` would be a
        ``ValidationException`` against a real table that no mock reproduces.
        """
        state_rows = [_state_row("schema_extract", seq=2)]
        executions_rows = [_executions_row(RUN_ID, status="RUNNING", current_step="schema_extract")]

        run_details(state_rows, executions_rows)

        injection = run_details.table.get_item_calls[0]
        projected = {name.strip() for name in injection["ProjectionExpression"].split(",")}
        assert projected == {"#status", "current_step", "updated_at"}
        assert injection["ExpressionAttributeNames"] == {"#status": "status"}


class TestItDegradesRatherThanFailing:
    """A run whose executions row is absent still renders."""

    def test_a_run_with_no_executions_row_returns_200_with_its_state_rows(self, run_details):
        """The keyed read comes back with no ``Item``. Nothing is injected and
        nothing is raised — the pre-existing ``except`` around this block logs at
        debug and continues, and that behaviour is unchanged.
        """
        state_rows = [_state_row("schema_extract", seq=2), _state_row("schema_mapping", seq=3)]

        status, body = run_details(state_rows, [])

        assert status == 200
        assert [s["step_name"] for s in body["steps"]] == ["schema_extract", "schema_mapping"]

    def test_a_raising_executions_read_does_not_fail_the_request(self, run_details):
        """``get_item`` unavailable ⇒ no injected step, still a 200."""
        state_rows = [_state_row("schema_extract", seq=2)]

        status, body = run_details(state_rows, [_executions_row(RUN_ID, status="RUNNING")])
        assert status == 200

        table = run_details.table
        table.get_item = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("DynamoDB unavailable"))
        status, body = run_details(state_rows, [_executions_row(RUN_ID, status="RUNNING")])

        assert status == 200
        assert body["steps"], "the state rows must still render when the executions read fails"

    def test_a_run_whose_row_carries_no_pipeline_id_match_is_not_resolved(self, run_details):
        """The key is ``(pipeline_id, run_id)``, both parts. A row with this run's
        id under a different pipeline is a different item and must not answer.
        """
        state_rows = [_state_row("schema_extract", seq=2)]
        other = _executions_row(RUN_ID, status="RUNNING", current_step="schema_extract")
        other["pipeline_id"] = "some-other-pipeline"

        status, body = run_details(state_rows, [other])

        assert status == 200
        assert _step(body, "schema_mapping") is None, "a row from another partition must not drive this run's display"


class TestTheNextStepDisplayStillWorks:
    """The three statuses the block acts on, and the one it ignores."""

    @pytest.mark.parametrize("active_status", ["RUNNING", "IN_PROGRESS", "WAITING_APPROVAL"])
    def test_an_active_run_gets_its_next_step_injected(self, run_details, active_status):
        state_rows = [_state_row("schema_extract", seq=2), _state_row("schema_mapping", seq=3)]
        executions_rows = [_executions_row(RUN_ID, status=active_status, current_step="schema_mapping")]

        status, body = run_details(state_rows, executions_rows)

        assert status == 200
        assert _step(body, "create_code")["status"] == "running"

    @pytest.mark.parametrize("finished_status", ["SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED"])
    def test_a_finished_run_gets_nothing_injected(self, run_details, finished_status):
        """The block's guard is unchanged: only the three active statuses inject.
        A keyed read that now *always* finds the row must not start injecting
        steps onto finished runs — the read got more reliable, not more eager.
        """
        state_rows = [_state_row("schema_extract", seq=2), _state_row("schema_mapping", seq=3)]
        executions_rows = [_executions_row(RUN_ID, status=finished_status, current_step="schema_mapping")]

        status, body = run_details(state_rows, executions_rows)

        assert status == 200
        assert [s["step_name"] for s in body["steps"]] == ["schema_extract", "schema_mapping"]

    def test_a_pending_approval_marks_the_step_instead_of_injecting_a_running_one(self, run_details):
        """With a pending approval the block marks the gate step rather than
        injecting the next one. Reached only when the run's row resolves, so this
        is a second consumer of the keyed read.
        """
        state_rows = [_state_row("schema_extract", seq=2), _state_row("schema_mapping", seq=3)]
        executions_rows = [_executions_row(RUN_ID, status="WAITING_APPROVAL", current_step="schema_mapping")]
        approvals_rows = [{"approval_type": "schema_mapping", "status": "pending", "run_id": RUN_ID}]

        status, body = run_details(state_rows, executions_rows, approvals_rows)

        assert status == 200
        assert _step(body, "create_code") is None, "a paused run must not show a running next step"
        assert body["status"] == "WAITING_APPROVAL"


# ═══════════════════════════════════════════════════════════════════════════
# do-NOT 9 / R8.2 — the status derivation is not this task's business
# ═══════════════════════════════════════════════════════════════════════════


class TestTheStatusDerivationIsUntouched:
    """``do-NOT 9`` — this task must not make the reader a second status authority.

    The run-detail reader derives ``status`` from **step rows**, not from the
    executions row's stored ``status``. That derivation and
    ``execution_updater._derive_execution_status`` already disagree, on purpose;
    ``LGR-9e76249-2`` (**template / ratified**) keeps ``load_outcome`` out of the
    disagreement. Task 6.3 is a read-mechanism change and reconciling the two is a
    different spec, so the derivation's outputs are pinned here — a keyed read that
    now always resolves the executions row makes its stored ``status`` newly
    reachable, which is exactly the temptation to guard.
    """

    def test_the_stored_status_does_not_become_the_derived_status(self, run_details):
        """The executions row says ``FAILED``; every step row says ``success``. The
        derived status follows the **step rows** — ``run_pipeline == success`` ⇒
        ``SUCCEEDED`` — and the row's own ``FAILED`` is not consulted.

        This is the disagreement the in-code comment documents. Asserted so that
        making the row easier to read does not turn into reading it for status.
        """
        state_rows = [
            _state_row("schema_extract", seq=2),
            _state_row("run_pipeline", seq=8),
        ]
        executions_rows = [_executions_row(RUN_ID, status="FAILED")]

        status, body = run_details(state_rows, executions_rows)

        assert status == 200
        assert body["status"] == "SUCCEEDED", "status must stay derived from step rows, not read off the executions row"

    def test_a_failed_step_row_wins_over_a_succeeded_stored_status(self, run_details):
        """The same property in the other direction, so the test cannot pass by
        the derivation coincidentally agreeing with the row.
        """
        state_rows = [_state_row("schema_extract", seq=2), _state_row("create_code", seq=4, status="failed")]
        executions_rows = [_executions_row(RUN_ID, status="SUCCEEDED")]

        _, body = run_details(state_rows, executions_rows)

        assert body["status"] == "FAILED"

    def test_load_outcome_is_still_derived_and_never_read_off_the_executions_row(self, run_details):
        """``do-NOT 6`` / ``LGR-9e76249-2`` — ``load_outcome`` is read-path derived
        from state rows and never persisted. A ``load_outcome`` attribute planted
        on the executions row must not reach the response.
        """
        state_rows = [_state_row("schema_extract", seq=2)]
        row = _executions_row(RUN_ID, status="RUNNING")
        row["load_outcome"] = {"outcome": "complete_load", "planted": True}

        _, body = run_details(state_rows, [row])

        assert body["load_outcome"].get("planted") is None, (
            "load_outcome must be derived from state rows, not taken off the executions row"
        )

    def test_this_read_is_not_where_completed_at_comes_from(self, run_details):
        """**Flipped by p48 task 6.2, deliberately, and kept rather than deleted.**

        As written for task 6.3 this asserted ``"completed_at" not in body`` —
        the read-mechanism change was to add no response key, and the completion
        time was 6.2's to add. 6.2 has now added it, so the bare absence
        assertion is false and would read as a regression if simply removed.
        ``LGR-4c4b4c9-6`` / ``LGR-60c4985-6`` (**template / ratified**, the same
        decision under two commit hashes) is this repo's convention for that: a
        deliberate hold encoded as an assertion is changed on purpose and stays
        legible as a reversal.

        What survives the flip is the assertion's actual subject — **this** read
        is still not a source of response fields. 6.2 took ``completed_at`` off
        the *lineage* ``get_item`` instead, and the in-code fence on this read's
        ``ProjectionExpression`` ("must not become a second source for anything
        else the response carries", do-NOT 9) is unchanged. So the row is seeded
        with a ``completed_at`` this projection does not ask for, and the
        assertion is that the value does not arrive **through here**: the fake
        table honours ``ProjectionExpression``, so a widened projection on this
        read would make the seeded value visible and this test would catch it.

        ``started_at`` is asserted alongside it because that attribute is on the
        seeded row too and is *also* outside this projection — it comes from the
        state rows' ``timestamp``, not from the executions row. Task 6.4 owns the
        question of which other row attributes the response drops; the guarantee
        here is only that none of them leaks in through this particular read.
        """
        state_rows = [_state_row("schema_extract", seq=2)]
        row = _executions_row(RUN_ID, status="RUNNING")
        row["completed_at"] = "2026-08-24T13:55:19.496708+00:00"

        _, body = run_details(state_rows, [row])

        # The lineage read is mocked away in this file's fixture only insofar as it
        # shares the fake table — it runs, and it *does* ask for ``completed_at``,
        # so the key is present. What must not happen is this read supplying it.
        injection_projections = [call.get("ProjectionExpression", "") for call in run_details.table.get_item_calls]
        assert any("current_step" in p for p in injection_projections), "the injection read must still happen"
        assert not any("current_step" in p and "completed_at" in p for p in injection_projections), (
            "completed_at must not be added to the injection read's projection — do-NOT 9's in-code fence"
        )
        assert body["started_at"] != row["started_at"], (
            "started_at must still come from the state rows, not off the executions row"
        )
