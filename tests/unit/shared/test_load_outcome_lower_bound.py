# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the DQ_Report_Row lower-bound path (p37 C5, R1.6).

What this path is, and what it may claim
----------------------------------------
Runs that completed before the counts row existed (p37 C1) carry no
``load_metrics`` row. For one family of them — inline-DQ ``s3_tables`` runs — the
DQ_Report_Row already carries ``rows_passed``, which **is** Rows_Written
(``glue_job_s3_tables.py.j2:307`` writes ``transformed_count`` under that name).
Rows_Read is **not** on that row: both the transform filter and the quarantine
reduce the frame and neither pre-transform count is written.

So exactly one combination settles anything. ``rows_passed == 0`` with
``rows_quarantined > 0``: the quarantined rows came from the source, so
Rows_Read > 0 *follows* and ``no_rows_loaded`` is established rather than
guessed. Every other combination is ``not_established``.

The truth table these tests pin, transcribed from design.md C5 and R1.6
----------------------------------------------------------------------
============  =================  ==================  =====================
rows_passed   rows_quarantined   outcome             ``missing``
============  =================  ==================  =====================
0             > 0                ``no_rows_loaded``  ``[]``
0             0                  ``not_established`` ``["rows_read"]``
0             absent / unusable  ``not_established`` ``["rows_read"]``
> 0           any                ``not_established`` ``["rows_read"]``
absent        any                ``not_established`` ``["rows_written"]``
negative      any                ``not_established`` ``["rows_read"]``
============  =================  ==================  =====================

Expectations come from the requirements, not from the module
------------------------------------------------------------
Every outcome, count, ``missing`` list and reason claim below was transcribed
from R1.6 and design.md C5, not recorded from a run of
:func:`derive_load_outcome`. A test written from the producer's own output cannot
detect the producer drifting, because the output is its own oracle.

Three things here that a reviewer should look for specifically
--------------------------------------------------------------
1. **``complete_load`` is asserted unreachable, not sampled.**
   :class:`TestCompleteLoadIsUnreachableOnThisPath` enumerates the whole
   small-integer grid plus every unusable input shape and asserts the set of
   outcomes the path can produce is exactly
   ``{no_rows_loaded, not_established}``. The ``rows_passed ==
   rows_quarantined`` case is called out separately because that is the one that
   would otherwise satisfy R1.5's equality arm and turn two numbers that cannot
   support a claim into a confident "everything loaded".

2. **``written > read`` reports ``["rows_read"]``, not ``["consistent_counts"]``.**
   The lower-bound guard sits deliberately **before** the inconsistent-counts
   guard, because on this path more rows passing the check than were quarantined
   is an ordinary successful run rather than an impossible pair. Asserting the
   ``missing`` value — and that ``consistent_counts`` never appears on this path
   at all — is what catches a future reorder of those two guards.

3. **The reason string says out loud that Rows_Read is a lower bound.** R1.6's
   honesty requirement applies to this path most of all, so the assertion is on
   the wording ("at least", "lower bound", "not a recorded count"), not merely on
   a reason being present.

Validates: Requirements 1.6
"""

import pathlib
import sys
from decimal import Decimal

import pytest

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

# Imported after the sys.path shim above, per this tree's convention for shared/
# modules (E402 is globally ignored in pyproject.toml for exactly this).
from load_outcome import (
    FROM_DQ_REPORT_ROW_LOWER_BOUND,
    FROM_LOAD_METRICS_ROW,
    MISSING_CONSISTENT_COUNTS,
    MISSING_ROWS_READ,
    MISSING_ROWS_WRITTEN,
    OUTCOME_COMPLETE_LOAD,
    OUTCOME_EMPTY_SOURCE,
    OUTCOME_KEY_MISSING,
    OUTCOME_NO_ROWS_LOADED,
    OUTCOME_NOT_ESTABLISHED,
    OUTCOME_PARTIAL_LOAD,
    derive_load_outcome,
)


def _lower_bound(*, rows_passed, rows_quarantined):
    """Call the deriver the way p37 C5's reader does.

    ``rows_quarantined`` goes in as ``rows_read`` because it is a **lower bound**
    on how many rows the source had, and ``rows_passed`` goes in as
    ``rows_written`` because for the inline-DQ case the two are the same number.
    Spelled out here so each test below reads in the DQ report's vocabulary
    rather than the deriver's.
    """
    return derive_load_outcome(
        rows_read=rows_quarantined,
        rows_written=rows_passed,
        determined_from=FROM_DQ_REPORT_ROW_LOWER_BOUND,
    )


# Inputs that are not usable counts. ``Decimal`` is in this list deliberately: it
# is what DynamoDB hands back for a number attribute, and the deriver rejects it
# on purpose so a caller has to convert rather than have a type silently read as
# a count. ``True``/``False`` are ``int`` subclasses, and reading ``True`` as one
# row would invent a count.
UNUSABLE_INPUTS = [None, True, False, 1.0, "3", Decimal("5"), [5], {"n": 5}]


# ── The one combination that establishes an outcome ───────────────────


class TestTheOneEstablishedCase:
    """``rows_passed == 0`` and ``rows_quarantined > 0`` → ``no_rows_loaded``.

    Established rather than guessed: the quarantined rows came from the source,
    so Rows_Read > 0 follows from the row itself.
    """

    @pytest.mark.parametrize("rows_quarantined", [1, 5, 1000])
    def test_zero_passed_with_quarantined_rows_is_no_rows_loaded(self, rows_quarantined):
        assert _lower_bound(rows_passed=0, rows_quarantined=rows_quarantined)["outcome"] == OUTCOME_NO_ROWS_LOADED

    def test_the_observed_run_shape(self):
        """The run this spec exists for, as its DQ report recorded it: 5 rows
        quarantined, none passing, and the execution still read SUCCEEDED."""
        outcome = _lower_bound(rows_passed=0, rows_quarantined=5)

        assert outcome["outcome"] == OUTCOME_NO_ROWS_LOADED
        assert outcome["determined_from"] == FROM_DQ_REPORT_ROW_LOWER_BOUND
        assert outcome["rows_read"] == 5
        assert outcome["rows_written"] == 0
        assert outcome["rows_not_written"] == 5
        assert outcome[OUTCOME_KEY_MISSING] == []

    def test_the_provenance_names_the_row_the_counts_came_off(self):
        """A reader has to be able to tell this apart from a recorded count, and
        ``determined_from`` is where that survives."""
        assert _lower_bound(rows_passed=0, rows_quarantined=5)["determined_from"] == "dq_report_row_lower_bound"

    def test_one_quarantined_row_is_the_lower_boundary(self):
        """A single quarantined row is enough to establish the source was not
        empty, which is what separates this from ``empty_source``."""
        outcome = _lower_bound(rows_passed=0, rows_quarantined=1)

        assert outcome["outcome"] == OUTCOME_NO_ROWS_LOADED
        assert outcome["outcome"] != OUTCOME_EMPTY_SOURCE

    def test_it_does_not_block_the_run(self):
        """R1.12 and do-NOT 4 on the one arm whose name invites the opposite
        reading. By the time this is knowable the task token is consumed."""
        assert _lower_bound(rows_passed=0, rows_quarantined=5)["blocks_run"] is False


class TestTheReasonStatesTheLowerBoundOutLoud:
    """R1.6's honesty requirement, which applies to this path most of all.

    ``rows_read`` here is the quarantine count: the true number read can only be
    **higher**. A record that presents it as a count is a claim the row cannot
    support, so the wording is asserted rather than the mere presence of a
    reason.
    """

    def test_the_reason_says_at_least_and_names_it_a_lower_bound(self):
        reason = _lower_bound(rows_passed=0, rows_quarantined=5)["reason"].lower()

        assert "at least" in reason
        assert "lower bound" in reason

    def test_the_reason_says_the_count_was_not_recorded(self):
        """The distinction that matters operationally: this number came from a
        quality report, not from the job's own count of what it read."""
        reason = _lower_bound(rows_passed=0, rows_quarantined=5)["reason"].lower()

        assert "not a recorded count" in reason
        assert "quarantined" in reason

    def test_the_recorded_path_does_not_borrow_the_lower_bound_wording(self):
        """The same counts under ``load_metrics_row`` provenance ARE exact, so
        that reason must not hedge — otherwise the hedge stops meaning anything
        and every run reads as approximate."""
        recorded = derive_load_outcome(rows_read=5, rows_written=0, determined_from=FROM_LOAD_METRICS_ROW)["reason"]

        assert "lower bound" not in recorded.lower()
        assert "at least" not in recorded.lower()

    def test_the_two_paths_reach_the_same_outcome_with_different_reasons(self):
        """Same verdict, different warrant. If the reasons were identical the
        run detail page could not tell an operator which one it is looking at."""
        recorded = derive_load_outcome(rows_read=5, rows_written=0, determined_from=FROM_LOAD_METRICS_ROW)
        lower_bound = _lower_bound(rows_passed=0, rows_quarantined=5)

        assert recorded["outcome"] == lower_bound["outcome"] == OUTCOME_NO_ROWS_LOADED
        assert recorded["reason"] != lower_bound["reason"]

    def test_the_unestablished_reason_also_says_it_is_only_a_bound(self):
        """The other arm of this path still has to explain itself: a run with
        rows passing the check is not a run whose read count is known."""
        reason = _lower_bound(rows_passed=3, rows_quarantined=2)["reason"].lower()

        assert "lower bound" in reason
        assert "does not establish" in reason


# ── Every other combination establishes nothing ───────────────────────


class TestEveryOtherCombinationIsNotEstablished:
    """The rest of the truth table. Each row names ``rows_read`` as the fact
    that was unavailable, because ``rows_passed`` did arrive — it **is**
    Rows_Written — and R1.6 asks which fact was missing, not which pair looked
    odd."""

    def test_zero_passed_and_zero_quarantined_establishes_nothing(self):
        """Nothing quarantined means nothing forces Rows_Read > 0, so this is
        NOT ``empty_source``: an empty source and a source whose rows all
        vanished before the check are indistinguishable from this row."""
        outcome = _lower_bound(rows_passed=0, rows_quarantined=0)

        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ]
        assert outcome["outcome"] != OUTCOME_EMPTY_SOURCE

    @pytest.mark.parametrize("rows_passed", [1, 5, 1000])
    def test_a_run_that_passed_rows_establishes_nothing(self, rows_passed):
        """``rows_passed > 0`` means rows did reach the writer, but how many were
        read is still unknown — so ``partial_load`` and ``complete_load`` are
        both out of reach."""
        outcome = _lower_bound(rows_passed=rows_passed, rows_quarantined=2)

        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ]
        assert outcome["outcome"] not in (OUTCOME_PARTIAL_LOAD, OUTCOME_COMPLETE_LOAD)

    def test_rows_written_is_still_echoed_when_rows_read_is_the_missing_fact(self):
        """``rows_passed`` IS Rows_Written, so it is a fact that arrived. Nulling
        it here would throw away the one count this row does carry."""
        outcome = _lower_bound(rows_passed=7, rows_quarantined=2)

        assert outcome["rows_written"] == 7
        assert outcome["rows_read"] is None

    @pytest.mark.parametrize("unusable", UNUSABLE_INPUTS)
    def test_an_unusable_quarantine_count_establishes_nothing(self, unusable):
        """Includes ``Decimal``: DynamoDB's native number type is rejected here
        on purpose, which is why the reader converts before calling."""
        outcome = _lower_bound(rows_passed=0, rows_quarantined=unusable)

        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ]

    @pytest.mark.parametrize("unusable", UNUSABLE_INPUTS)
    def test_an_unusable_rows_passed_names_rows_written(self, unusable):
        """A DQ row with no usable ``rows_passed`` — the on-demand data quality
        agent writes exactly that shape — lacks Rows_Written, and the record
        says so instead of blaming the read count."""
        outcome = _lower_bound(rows_passed=unusable, rows_quarantined=5)

        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_WRITTEN]

    def test_both_counts_absent_names_both_facts(self):
        outcome = _lower_bound(rows_passed=None, rows_quarantined=None)

        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ, MISSING_ROWS_WRITTEN]

    @pytest.mark.parametrize(
        ("rows_passed", "rows_quarantined"),
        [
            (0, -1),  # negative quarantine count
            (0, -5),
            (-1, 5),  # negative rows_passed
            (-1, -1),  # both negative and equal — R1.5's arm arithmetically
            (-2, -1),
        ],
    )
    def test_a_negative_count_establishes_nothing(self, rows_passed, rows_quarantined):
        outcome = _lower_bound(rows_passed=rows_passed, rows_quarantined=rows_quarantined)

        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ]


class TestGuardOrderingIsAssertedNotImplied:
    """The lower-bound guard runs **before** the inconsistent-counts guard, and
    that ordering is a decision rather than an accident.

    On the recorded path ``written > read`` is an impossible pair and reports
    ``consistent_counts``. On this path it means "more rows passed the quality
    check than were quarantined" — an ordinary successful run. Reporting that as
    counts which "cannot describe a load" would be a false accusation, so the
    record names the fact that is genuinely unavailable: Rows_Read.
    """

    def test_more_passed_than_quarantined_reports_rows_read_not_the_relationship(self):
        outcome = _lower_bound(rows_passed=9, rows_quarantined=5)

        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ]
        assert MISSING_CONSISTENT_COUNTS not in outcome[OUTCOME_KEY_MISSING]

    def test_the_recorded_path_still_calls_that_pair_inconsistent(self):
        """The contrast is the point: the same two numbers under recorded
        provenance ARE an impossible pair. If this assertion and the one above
        ever agree, one of the two guards has moved."""
        recorded = derive_load_outcome(rows_read=5, rows_written=9, determined_from=FROM_LOAD_METRICS_ROW)

        assert recorded[OUTCOME_KEY_MISSING] == [MISSING_CONSISTENT_COUNTS]

    def test_consistent_counts_is_never_reported_on_this_path(self):
        """Structural: every pair that is not (0, > 0) is caught by the
        lower-bound guard first, so the inconsistent-counts arm is unreachable
        here. A reorder of the two guards fails this."""
        for rows_passed in range(-3, 12):
            for rows_quarantined in range(-3, 12):
                outcome = _lower_bound(rows_passed=rows_passed, rows_quarantined=rows_quarantined)
                assert MISSING_CONSISTENT_COUNTS not in outcome[OUTCOME_KEY_MISSING], (
                    f"consistent_counts reported for rows_passed={rows_passed}, rows_quarantined={rows_quarantined}"
                )


# ── complete_load is unreachable, asserted rather than sampled ────────


class TestCompleteLoadIsUnreachableOnThisPath:
    """Rows_Read is not recoverable from a DQ_Report_Row, so R1.4 and R1.5
    cannot be told apart here and neither may be claimed.

    This is enforced by a guard inside the deriver rather than by the caller
    keeping a promise, and these assertions are what hold that guard in place.
    The grid below is the sweep task 12.1 ran once, kept as a standing
    assertion: a one-off sweep is something a later reader has to trust, and an
    assertion is something a later change has to survive.
    """

    #: Small integers around every boundary the arms test, plus every unusable
    #: shape. Deliberately includes equal pairs, inverted pairs and negatives.
    GRID = [*range(-3, 40), *UNUSABLE_INPUTS]

    def test_the_path_can_only_produce_two_outcomes(self):
        reachable = {
            _lower_bound(rows_passed=passed, rows_quarantined=quarantined)["outcome"]
            for passed in self.GRID
            for quarantined in self.GRID
        }

        assert reachable == {OUTCOME_NO_ROWS_LOADED, OUTCOME_NOT_ESTABLISHED}

    def test_complete_load_is_produced_for_no_input_at_all(self):
        """Stated separately from the set equality above so a failure message
        names this outcome rather than a set difference."""
        for passed in self.GRID:
            for quarantined in self.GRID:
                outcome = _lower_bound(rows_passed=passed, rows_quarantined=quarantined)
                assert outcome["outcome"] != OUTCOME_COMPLETE_LOAD, (
                    f"complete_load reached with rows_passed={passed!r}, rows_quarantined={quarantined!r}"
                )

    @pytest.mark.parametrize("count", [1, 5, 1000])
    def test_equal_counts_do_not_reach_complete_load(self, count):
        """The case the guard exists for. With ``rows_read`` carrying the
        quarantine count, ``rows_passed == rows_quarantined`` satisfies R1.5's
        equality arm arithmetically — and would report a confident
        ``complete_load`` built on two numbers that cannot support one."""
        outcome = _lower_bound(rows_passed=count, rows_quarantined=count)

        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ]

    def test_partial_and_empty_source_are_unreachable_too(self):
        """Both need a real Rows_Read. ``partial_load`` would need to know how
        many rows were read to say how many were withheld; ``empty_source`` would
        need to know the source was empty, which a zero quarantine count does
        not establish."""
        for passed in self.GRID:
            for quarantined in self.GRID:
                outcome = _lower_bound(rows_passed=passed, rows_quarantined=quarantined)["outcome"]
                assert outcome not in (OUTCOME_PARTIAL_LOAD, OUTCOME_EMPTY_SOURCE)


# ── Record-shape guarantees on every arm of this path ─────────────────


class TestEveryArmOfThisPathIsAPersistableRecord:
    """R1.12, R1.4's non-negativity, and the echoed provenance — on this path
    specifically, because the fourth ``determined_from`` value was added after
    the arms were written and a new arm is where a field gets dropped."""

    ARMS = [
        ("established (0, > 0)", {"rows_passed": 0, "rows_quarantined": 5}),
        ("zero and zero", {"rows_passed": 0, "rows_quarantined": 0}),
        ("rows passed", {"rows_passed": 7, "rows_quarantined": 2}),
        ("more passed than quarantined", {"rows_passed": 9, "rows_quarantined": 5}),
        ("equal", {"rows_passed": 5, "rows_quarantined": 5}),
        ("absent rows_passed", {"rows_passed": None, "rows_quarantined": 5}),
        ("absent both", {"rows_passed": None, "rows_quarantined": None}),
        ("negative", {"rows_passed": -1, "rows_quarantined": 5}),
        ("Decimal, as DynamoDB returns it", {"rows_passed": Decimal("0"), "rows_quarantined": Decimal("5")}),
    ]

    @pytest.mark.parametrize(("label", "kwargs"), ARMS, ids=[case[0] for case in ARMS])
    def test_blocks_run_is_false(self, label, kwargs):
        """``is False`` rather than ``== False``: a falsy stand-in would satisfy
        equality and would not be the explicit statement R1.12 asks for."""
        assert _lower_bound(**kwargs)["blocks_run"] is False

    @pytest.mark.parametrize(("label", "kwargs"), ARMS, ids=[case[0] for case in ARMS])
    def test_rows_not_written_is_never_negative(self, label, kwargs):
        assert _lower_bound(**kwargs)["rows_not_written"] >= 0

    @pytest.mark.parametrize(("label", "kwargs"), ARMS, ids=[case[0] for case in ARMS])
    def test_the_provenance_is_echoed_unchanged(self, label, kwargs):
        """A reader tells the lower bound from a recorded count by this field
        alone, so it has to survive every arm."""
        assert _lower_bound(**kwargs)["determined_from"] == FROM_DQ_REPORT_ROW_LOWER_BOUND

    @pytest.mark.parametrize(("label", "kwargs"), ARMS, ids=[case[0] for case in ARMS])
    def test_every_arm_states_a_reason(self, label, kwargs):
        assert _lower_bound(**kwargs)["reason"]

    def test_rows_not_written_is_never_negative_across_the_whole_grid(self):
        grid = [*range(-3, 40), *UNUSABLE_INPUTS]
        for passed in grid:
            for quarantined in grid:
                assert _lower_bound(rows_passed=passed, rows_quarantined=quarantined)["rows_not_written"] >= 0

    def test_blocks_run_is_false_across_the_whole_grid(self):
        grid = [*range(-3, 40), *UNUSABLE_INPUTS]
        for passed in grid:
            for quarantined in grid:
                assert _lower_bound(rows_passed=passed, rows_quarantined=quarantined)["blocks_run"] is False
