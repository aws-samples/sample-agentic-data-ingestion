# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests: ``execution_updater._normalize_status`` (p37 task 1.3, T2).

``lambda/agentic_data_ingestion_execution_updater/lambda_function.py:513-515``
is a three-line pure function with **one** caller (``_process_record:126``) and
**no test that names it**. It is one of the two zero-coverage findings that
survived ``requirements.md``'s ``lambda/**`` false-negative check: a codegraph
zero-coverage finding under ``lambda/**`` usually means "unresolved" rather
than "untested", because every test call site in that tree reaches its target
through a ``sys.path``-injected ``import lambda_function as _mod`` alias that
carries no static call edge. This one was re-confirmed by searching ``tests/``
for ``_normalize_status`` and for ``_STATUS_MAP``: **zero hits each**. So it is
genuinely untested, and p37 C6 is about to move the code around it.

**What this file is for, stated as the one mistake it exists to catch.**
``R4.1`` says the Execution_Updater must not write a ``steps_status`` entry for
a DQ_Report_Row. The mechanism is a two-part one:

1. the DQ_Report_Row carries ``overall_status`` and **no** ``status``
   (``pyspark_templates/glue_job_s3_tables.py.j2:283-292``, transcribed below;
   ``agentcore/data_quality_agent/main.py:582`` writes the same field), so
   ``_process_record`` reads ``""`` at ``:104``; and
2. ``_normalize_status("")`` returns ``"UNKNOWN"`` (``:515``), which lands in
   ``steps_status.data_quality``.

**The blank arm is CORRECT and must survive C6 unchanged.** ``"UNKNOWN"`` is
the right answer for a *genuine* step whose status is blank — that is a step
the system really does not know the status of. What ``R4.1`` removes is the
**caller** that fed a non-step row into it. ``design.md`` C6 says so directly
("Do NOT change ``_normalize_status``'s ``"" → "UNKNOWN"`` arm ... **R4.1**
removes the caller that fed it a non-step row, not the arm") and ``tasks.md``
do-NOT 5 restates it. Every assertion in the first three classes below is
therefore **green today and must still be green after task 2.1**. A red there
means C6 changed the arm instead of the caller, which is the single failure
mode this file was written to make visible.

**Expected-red window, named so a reviewer seeing red does not revert a
correct change.** The last class holds one caller-level assertion — a
``data_quality`` row writes no ``steps_status`` entry — which is the defect
``R4.1`` describes. It is marked ``xfail(strict=True)`` naming **task 2.1**.
``strict=True`` means the suite fails the moment it starts passing, so task 2.1
cannot land without coming back here and removing the marker.

**Expectations are derived from ``R4.1``, not from current output.** A test
written from the producer's own output cannot detect the producer drifting,
because the output is its own oracle (``LGR-28b4d75-5``, template/ratified).
Concretely: ``_STATUS_MAP`` is **transcribed** below rather than read off
``_mod._STATUS_MAP``, and the DQ_Report_Row fixture is transcribed from the
Jinja2 template rather than generated from it — the same reason
``test_self_healing_visibility.py`` transcribes ``build_outcome_record``'s
output (``LGR-28b4d75-5``).

Ledger provenance:

- ``LGR-453f12d-5`` (**template / ratified**) is the governing decision and the
  precedent C6 generalises: "route self-healing state rows away from the step
  machinery **before any status is derived**". Its ``why`` records the exact
  defect ``R4`` repeats one row-type over — a record with no ``status`` fell
  through ``_derive_execution_status`` to ``RUNNING`` and the run showed as
  "RUNNING -> self_healing" **permanently, inflating the running count**. Its
  guard is ``test_self_healing_visibility.py``, which this file sits beside and
  does not modify.
- ``LGR-453f12d-1`` / ``-2`` (**template / ratified**) supply ``R1.1``'s
  revisit condition ("the generated job records rows written"); they anchor the
  same commit and so appear on any ``why`` of this file, but they govern the
  self-healing rerun guard rather than status normalisation.
- ``LGR-78ce0b7-8`` and ``LGR-b2f79ef-8`` (**template / ratified**, the same
  decision under two commit hashes) are the house rule this arm is an instance
  of: a missing record and a record with nothing in it must not collapse into
  one value. That is why ``"UNKNOWN"`` is a real answer and not a bug.
- ``LGR-b7d5490-1`` (**backfilled**) is why the handler is loaded per test by
  path rather than imported once: the whole unit layer runs in one pytest
  process and ``lambda_function`` is a module name 20+ Lambdas share.
- ``why`` on this file otherwise returns ``LGR-0176ccc-1``, the documented
  634-file working-tree snapshot, plus ``LGR-2eaf8a3-1`` — neither names status
  normalisation. ``decisions`` for ``testing``, ``logging``, ``api`` and ``ui``
  (all with ``include_backfilled=True``) return **nothing** governing
  ``_STATUS_MAP`` or the ``steps_status`` map's shape. That is a ledger gap,
  recorded rather than papered over.

**Validates: Requirements 4.1**
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_execution_updater",
    )
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

_ENV = {
    "EXECUTIONS_TABLE": "test-executions",
    "PIPELINES_TABLE": "test-pipelines",
    "ERRORS_TABLE": "test-errors",
    "STATE_TABLE": "test-state",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

PIPELINE_ID = "newtest-ab2a08a0"
RUN_ID = "run-2026-08-09-15-38-19-5c5f5f8a"


def _load_handler():
    """Load execution_updater with boto3 patched, isolated per call.

    Verbatim in shape from ``test_self_healing_visibility.py::_load_handler``,
    which is this directory's established pattern and the guard for
    ``LGR-453f12d-5``. Two things about it are deliberate:

    * The importlib load rather than a plain import follows ``LGR-b7d5490-1``:
      the whole unit layer runs in one pytest process and ``lambda_function``
      is a name 20+ Lambdas share, so the module is popped and re-loaded by
      path per call.
    * The table is a ``MagicMock``, **not** a moto table. That is not a local
      re-implementation of the shared ``mock_dynamodb_tables`` fixture — it is
      what makes this file's assertion expressible at all. ``R4.1`` is a claim
      about the ``UpdateExpression`` the handler *emits* ("no ``steps_status``
      entry is written"), and a moto-backed table can only be asked what it
      *contains* afterwards, which cannot tell "nothing was written" apart from
      "a setup step did not run". The precedent beside this file makes the same
      call for the same reason.
    """
    mock_table = MagicMock()
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    with (
        patch.dict(os.environ, _ENV),
        patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
        patch("boto3.resource", return_value=mock_dynamo),
    ):
        sys.modules.pop("lambda_function", None)
        if SHARED_DIR not in sys.path:
            sys.path.insert(0, SHARED_DIR)
        spec = importlib.util.spec_from_file_location("lambda_function", os.path.join(LAMBDA_DIR, "lambda_function.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, mock_table


# ---------------------------------------------------------------------------
# The map, transcribed rather than read from the module under test
# ---------------------------------------------------------------------------

# Transcribed from lambda_function.py:59-68. Written out by hand on purpose:
# comparing _mod._STATUS_MAP to itself would pass for any value it ever holds,
# which is LGR-28b4d75-5's "the output is its own oracle". This literal is the
# oracle, so an edit to the map has to be made here too, deliberately.
EXPECTED_STATUS_MAP = {
    "success": "SUCCEEDED",
    "completed": "SUCCEEDED",
    "error": "FAILED",
    "failed": "FAILED",
    "running": "RUNNING",
    "in_progress": "RUNNING",
    "pending": "RUNNING",
    "waiting_approval": "WAITING_APPROVAL",
}


def _image(**attrs) -> dict:
    """Build a NEW_IMAGE from plain string attributes."""
    return {k: {"S": v} for k, v in attrs.items() if v is not None}


def _steps_status_writes(mock_table) -> list[tuple[str, str]]:
    """Every ``(step_name, step_status)`` pair written into the steps_status map.

    Keyed on the second update's shape at ``lambda_function.py:256-263``:
    ``SET #steps_status.#step_name = :step_status``. Returned as a list so a
    failure prints the offending pair rather than just a boolean.
    """
    writes = []
    for call in mock_table.update_item.call_args_list:
        kwargs = call.kwargs
        if "#steps_status.#step_name" not in kwargs.get("UpdateExpression", ""):
            continue
        names = kwargs.get("ExpressionAttributeNames") or {}
        values = kwargs.get("ExpressionAttributeValues") or {}
        writes.append((names.get("#step_name", ""), values.get(":step_status", "")))
    return writes


def _dq_report_record(*, event_name: str = "INSERT", pipeline_id: str | None = PIPELINE_ID) -> dict:
    """A stream record shaped exactly like the DQ_Report_Row the Glue job writes.

    Transcribed from ``pyspark_templates/glue_job_s3_tables.py.j2:283-292``
    (``_dq_report``) rather than generated from it — per ``LGR-28b4d75-5``, a
    fixture derived from the producer cannot detect the producer drifting. The
    on-demand agent writes the same shape at
    ``agentcore/data_quality_agent/main.py:582``.

    **Note the absence of a ``status`` attribute.** The row carries
    ``overall_status``. That absence is the whole mechanism: ``_process_record``
    reads ``status`` at ``:104``, gets ``""``, and ``_normalize_status("")``
    returns ``"UNKNOWN"``. Renaming the field is forbidden (``tasks.md``
    do-NOT 5) — two producers write it and three readers consume it, so the
    defect is in this reader, not in them.
    """
    return {
        "eventID": "evt-data_quality-glue_inline_dq",
        "eventName": event_name,
        "dynamodb": {
            "NewImage": _image(
                run_id=RUN_ID,
                step_name="data_quality",
                pipeline_id=pipeline_id,
                overall_status="fail",
                root_cause="data_issue",
                source="glue_inline_dq",
                quarantine_path="s3://adi-quarantine/newtest-ab2a08a0/",
                timestamp="2026-08-09T15:41:12+00:00",
            )
        },
    }


def _step_record(*, step_name: str, status: str | None) -> dict:
    """A stream record for a GENUINE pipeline step, optionally status-less.

    ``schema_extract`` is a member of ``shared.step_names.STEP_SEQUENCE``
    (sequence 2), so it is a Step_Row under any reading of ``R4.3``'s
    allowlist — including the one task 2.1 introduces. That matters: these
    assertions have to keep holding across C6.
    """
    return {
        "eventID": f"evt-{step_name}-{status or 'blank'}",
        "eventName": "MODIFY",
        "dynamodb": {
            "NewImage": _image(
                run_id=RUN_ID,
                pipeline_id=PIPELINE_ID,
                step_name=step_name,
                status=status,
                timestamp="2026-08-09T15:39:00+00:00",
            )
        },
    }


# ---------------------------------------------------------------------------
# 1. The whole map is pinned, entry by entry
# ---------------------------------------------------------------------------


class TestTheStatusMapIsPinnedWholesale:
    """``_STATUS_MAP`` (``:59-68``) — all eight entries, and nothing more.

    Pinned as a whole rather than sampled because C6 moves the code that reads
    it. Green today; must stay green through task 2.1.
    """

    def test_the_map_has_exactly_the_eight_expected_entries(self):
        updater, _ = _load_handler()
        assert updater._STATUS_MAP == EXPECTED_STATUS_MAP

    def test_no_entry_was_added_or_removed(self):
        updater, _ = _load_handler()
        assert set(updater._STATUS_MAP) == set(EXPECTED_STATUS_MAP)
        assert len(updater._STATUS_MAP) == 8

    @pytest.mark.parametrize(("raw", "expected"), sorted(EXPECTED_STATUS_MAP.items()))
    def test_each_mapped_entry_normalizes_to_its_pinned_value(self, raw, expected):
        updater, _ = _load_handler()
        assert updater._normalize_status(raw) == expected

    def test_the_three_running_spellings_all_collapse_to_running(self):
        """``running`` / ``in_progress`` / ``pending`` are one dashboard state."""
        updater, _ = _load_handler()
        assert {updater._normalize_status(s) for s in ("running", "in_progress", "pending")} == {"RUNNING"}

    def test_both_failure_spellings_collapse_to_failed(self):
        updater, _ = _load_handler()
        assert updater._normalize_status("error") == updater._normalize_status("failed") == "FAILED"

    def test_both_success_spellings_collapse_to_succeeded(self):
        updater, _ = _load_handler()
        assert updater._normalize_status("success") == updater._normalize_status("completed") == "SUCCEEDED"


# ---------------------------------------------------------------------------
# 2. Arm one: a mapped value, matched case-insensitively
# ---------------------------------------------------------------------------


class TestTheMappedArm:
    """``_STATUS_MAP.get(raw.lower(), ...)`` — the lookup is on the lowered input."""

    @pytest.mark.parametrize("raw", ["SUCCESS", "Success", "sUcCeSs"])
    def test_the_lookup_is_case_insensitive(self, raw):
        updater, _ = _load_handler()
        assert updater._normalize_status(raw) == "SUCCEEDED"

    def test_an_uppercase_multiword_key_still_matches(self):
        updater, _ = _load_handler()
        assert updater._normalize_status("WAITING_APPROVAL") == "WAITING_APPROVAL"
        assert updater._normalize_status("In_Progress") == "RUNNING"


# ---------------------------------------------------------------------------
# 3. Arm two: an unmapped value is passed through uppercased
# ---------------------------------------------------------------------------


class TestTheUnmappedUppercasedArm:
    """A status the map does not know is returned ``.upper()``, not rejected.

    This is what lets a new producer status reach the dashboard without a code
    change here, and it is also why an unrecognised value can never raise —
    which matters for risk N2's "the classifier must never raise" (a raising
    normaliser would stall a stream shard the same way).
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("aborted", "ABORTED"),
            ("timed_out", "TIMED_OUT"),
            ("skipped", "SKIPPED"),
            ("cancelled", "CANCELLED"),
            ("start_failed", "START_FAILED"),
        ],
    )
    def test_an_unknown_status_is_uppercased(self, raw, expected):
        updater, _ = _load_handler()
        assert updater._normalize_status(raw) == expected

    def test_an_already_uppercase_unknown_status_passes_through_unchanged(self):
        updater, _ = _load_handler()
        assert updater._normalize_status("ABORTED") == "ABORTED"

    def test_a_whitespace_only_status_is_uppercased_rather_than_treated_as_blank(self):
        """``" "`` is truthy, so it takes the unmapped arm, not the blank arm.

        Pinned because it is the boundary between arm two and arm three, and
        because it is the kind of thing a refactor that adds ``.strip()``
        would silently move.
        """
        updater, _ = _load_handler()
        assert updater._normalize_status(" ") == " "

    def test_an_unknown_status_never_raises(self):
        updater, _ = _load_handler()
        assert updater._normalize_status("\u2603 not a status") == "\u2603 NOT A STATUS"


# ---------------------------------------------------------------------------
# 4. Arm three: blank → UNKNOWN. CORRECT, and it must survive C6 unchanged.
# ---------------------------------------------------------------------------


class TestTheBlankArmIsCorrectAndMustSurviveC6Unchanged:
    """``"" → "UNKNOWN"`` (``:515``) is the right answer for a genuine step.

    **This class is the point of the file.** ``R4.1`` is satisfied by removing
    the *caller* that hands a non-step row to this function, not by changing
    this arm. ``design.md`` C6 and ``tasks.md`` do-NOT 5 both say so. Every
    assertion here is green today and **must still be green after task 2.1** —
    a red here means C6 edited the arm instead of the caller.
    """

    def test_the_empty_string_is_unknown(self):
        updater, _ = _load_handler()
        assert updater._normalize_status("") == "UNKNOWN"

    def test_none_is_unknown_rather_than_an_attribute_error(self):
        """``_get_str`` returns ``""`` for a missing attribute, so ``None`` is
        not reachable from ``_process_record`` — but the guard is a truthiness
        test, so it holds for ``None`` too. Pinned so a change to ``if raw`` is
        a visible change."""
        updater, _ = _load_handler()
        assert updater._normalize_status(None) == "UNKNOWN"

    def test_unknown_is_not_confused_with_a_derived_running(self):
        """The two halves of the ``R4`` defect are distinct values.

        ``_normalize_status("")`` is ``"UNKNOWN"`` (the ``steps_status`` half)
        while ``_derive_execution_status(step, "")`` is ``"RUNNING"`` (the
        ``#status`` half, ``R4.2``'s latent symptom). Keeping them apart is
        what makes a C6 regression diagnosable.
        """
        updater, _ = _load_handler()
        assert updater._normalize_status("") == "UNKNOWN"
        assert updater._derive_execution_status("data_quality", "") == "RUNNING"

    def test_a_genuine_step_with_a_blank_status_still_records_unknown(self):
        """End to end, through the caller: a real Step_Row keeps this behaviour.

        ``schema_extract`` is in ``STEP_SEQUENCE``, so task 2.1's allowlist
        admits it. If this goes red at 2.1, the allowlist rejected a genuine
        step — which is the *other* way C6 can go wrong.
        """
        updater, table = _load_handler()

        updater.lambda_handler({"Records": [_step_record(step_name="schema_extract", status=None)]}, None)

        assert _steps_status_writes(table) == [("schema_extract", "UNKNOWN")]

    def test_the_dq_report_row_carries_overall_status_and_no_status(self):
        """do-NOT 5, asserted from the reader's side.

        The fixture is a transcription of the producer, so this pins that the
        mechanism ``R4.1`` describes is the real one: the row has
        ``overall_status`` and no ``status``, which is why ``_process_record``
        reads ``""``. The fix is not to rename this field — two producers write
        it and three readers consume it.
        """
        image = _dq_report_record()["dynamodb"]["NewImage"]
        assert "status" not in image
        assert image["overall_status"] == {"S": "fail"}


# ---------------------------------------------------------------------------
# 5. The caller-level assertion — EXPECTED RED until task 2.1
# ---------------------------------------------------------------------------


class TestANonStepRowWritesNoStepsStatusEntry:
    """``R4.1`` at the caller. This was the window; the arm above never was.

    **Closed by p37 task 2.1 (C6).** This assertion was marked
    ``xfail(strict=True)`` naming that task; the marker came off when the task
    landed, and the assertion below is unchanged from the form it was written
    in. It now passes because ``_process_record`` classifies the row through
    the Step_Row allowlist (``_drives_execution_row``) and returns **before**
    ``_normalize_status`` or ``_derive_execution_status`` is reached — the fix
    is in the CALLER, exactly as ``LGR-453f12d-5`` (template / ratified) did it
    for ``self_healing``. ``_normalize_status``'s blank arm is untouched, which
    is what the three classes above still assert.
    """

    def test_a_data_quality_row_produces_no_steps_status_entry(self):
        updater, table = _load_handler()

        updater.lambda_handler({"Records": [_dq_report_record()]}, None)

        assert _steps_status_writes(table) == []
