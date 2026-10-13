# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/load_outcome.py — the named boundaries (p37 R1.2-R1.6).

What these pin, and why examples rather than a property
------------------------------------------------------
``test_load_outcome_prop.py`` owns the space: Property 1 draws over the whole
integer-and-``None`` input space and asserts the deriver is total, returns
exactly one of the five outcomes, and never reports a negative
``rows_not_written``. This file pins the **named boundaries a reviewer looks
for**, with the expected outcome and the expected counts written out by hand:

* ``rows_read == rows_written == 0`` versus both greater than zero — the two
  cases the arm ordering has to keep apart;
* ``0 < rows_written < rows_read`` and ``rows_written == rows_read``;
* each of the three ``determined_from`` values with ``missing`` populated;
* the inverted / negative-count guard.

A property says "some outcome always comes back". These say "**this** outcome
comes back for **this** pair", which is the half a partition property cannot
express.

The ordering these tests exist to protect
-----------------------------------------
``(0, 0)`` satisfies ``rows_written == rows_read`` as well as
``rows_written == 0``, so ``no_rows_loaded`` and ``empty_source`` must both be
decided before the equality arm or an empty source reports ``complete_load``.
:class:`TestArmOrdering` asserts that directly rather than leaving it implied
by the two arms passing individually.

Expectations come from R1.2-R1.6, not from the module
-----------------------------------------------------
Every outcome, count and ``missing`` list below was transcribed from the
acceptance criteria and from design.md C2's arm table, not recorded from a run
of :func:`derive_load_outcome`. A test written from the producer's own output
cannot detect the producer drifting, because the output is its own oracle.

``blocks_run`` is asserted in **every** arm
-------------------------------------------
It is R1.12 and do-NOT 4 encoded as a field, and it is the thing a later branch
edit is most likely to drop silently. The module routes all five arms through
one assembly point so that cannot happen; :class:`TestEveryArmIsAPersistableRecord`
asserts the guarantee holds rather than trusting the structure.

Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.6
"""

import pathlib
import sys

import pytest

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

# Imported after the sys.path shim above, per this tree's convention for shared/
# modules (E402 is globally ignored in pyproject.toml for exactly this).
from load_outcome import (
    FROM_LOAD_METRICS_ROW,
    FROM_LOAD_METRICS_ROW_ABSENT,
    FROM_LOAD_METRICS_ROW_INCOMPLETE,
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

# The full key set the record carries in every arm, transcribed from design.md
# C2 rather than from a returned dict. One stable shape is what lets a caller
# read `load_outcome["rows_not_written"]` without first asking which outcome it
# got.
EXPECTED_KEYS = {
    "outcome",
    "rows_read",
    "rows_written",
    "rows_not_written",
    "blocks_run",
    OUTCOME_KEY_MISSING,
    "determined_from",
    "reason",
}

ALL_THREE_PROVENANCES = [
    FROM_LOAD_METRICS_ROW,
    FROM_LOAD_METRICS_ROW_ABSENT,
    FROM_LOAD_METRICS_ROW_INCOMPLETE,
]


# ── R1.2 — the source had rows and none of them landed ───────────────


class TestNoRowsLoaded:
    """R1.2: ``rows_written == 0`` and ``rows_read > 0`` → ``no_rows_loaded``."""

    @pytest.mark.parametrize("rows_read", [1, 5, 1000])
    def test_zero_written_from_a_non_empty_source(self, rows_read):
        outcome = derive_load_outcome(rows_read=rows_read, rows_written=0)
        assert outcome["outcome"] == OUTCOME_NO_ROWS_LOADED

    def test_one_row_read_and_none_written_is_the_lower_boundary(self):
        """``rows_read == 1`` is the smallest source that makes writing nothing
        a loss rather than a no-op."""
        outcome = derive_load_outcome(rows_read=1, rows_written=0)
        assert outcome["outcome"] == OUTCOME_NO_ROWS_LOADED
        assert outcome["rows_read"] == 1
        assert outcome["rows_written"] == 0
        assert outcome["rows_not_written"] == 1

    def test_the_observed_run_shape(self):
        """The run this spec exists for: 5 rows read, every one quarantined,
        nothing written, and the execution still reported SUCCEEDED."""
        outcome = derive_load_outcome(rows_read=5, rows_written=0)
        assert outcome["outcome"] == OUTCOME_NO_ROWS_LOADED
        assert outcome["rows_not_written"] == 5
        assert outcome[OUTCOME_KEY_MISSING] == []

    def test_no_rows_loaded_does_not_block_the_run(self):
        """R1.12 and do-NOT 4 on the one arm whose name invites the opposite
        reading. The token is already consumed by the time this is knowable."""
        assert derive_load_outcome(rows_read=5, rows_written=0)["blocks_run"] is False


# ── R1.3 — an empty source is a no-op, not a loss ────────────────────


class TestEmptySource:
    """R1.3: ``rows_written == 0`` and ``rows_read == 0`` → ``empty_source``."""

    def test_zero_and_zero_is_an_empty_source(self):
        outcome = derive_load_outcome(rows_read=0, rows_written=0)
        assert outcome["outcome"] == OUTCOME_EMPTY_SOURCE
        assert outcome["rows_read"] == 0
        assert outcome["rows_written"] == 0

    def test_an_empty_source_withheld_nothing(self):
        """Nothing was read, so nothing can be outstanding — a non-zero count
        here would make every empty scheduled poll look like data loss."""
        assert derive_load_outcome(rows_read=0, rows_written=0)["rows_not_written"] == 0

    def test_empty_source_reports_no_missing_fact(self):
        """Both counts were established. ``0`` is a value here, not an absence,
        so ``missing`` must stay empty."""
        assert derive_load_outcome(rows_read=0, rows_written=0)[OUTCOME_KEY_MISSING] == []

    def test_empty_source_does_not_block_the_run(self):
        assert derive_load_outcome(rows_read=0, rows_written=0)["blocks_run"] is False


# ── R1.4 — some rows landed and some did not ─────────────────────────


class TestPartialLoad:
    """R1.4: ``0 < rows_written < rows_read`` → ``partial_load``, and the
    number of rows not written is recorded."""

    @pytest.mark.parametrize(
        ("rows_read", "rows_written", "expected_not_written"),
        [
            (2, 1, 1),  # the smallest partial load there is
            (10, 9, 1),  # one row withheld out of ten
            (1000, 998, 2),  # design.md D1's "two out of a thousand" case
            (100, 1, 99),  # almost everything withheld, still partial
        ],
    )
    def test_partial_load_records_the_rows_not_written(self, rows_read, rows_written, expected_not_written):
        outcome = derive_load_outcome(rows_read=rows_read, rows_written=rows_written)
        assert outcome["outcome"] == OUTCOME_PARTIAL_LOAD
        assert outcome["rows_not_written"] == expected_not_written
        assert outcome["rows_read"] == rows_read
        assert outcome["rows_written"] == rows_written

    def test_partial_load_is_distinguishable_from_no_rows_loaded(self):
        """``rows_written == 1`` is the boundary: one row landing is a partial
        load, none landing is R1.2's total loss."""
        partial = derive_load_outcome(rows_read=2, rows_written=1)
        total_loss = derive_load_outcome(rows_read=2, rows_written=0)
        assert partial["outcome"] == OUTCOME_PARTIAL_LOAD
        assert total_loss["outcome"] == OUTCOME_NO_ROWS_LOADED

    def test_partial_load_does_not_block_the_run(self):
        """D1: the count is displayed, the run keeps its status, and there is
        no threshold anywhere in this spec."""
        assert derive_load_outcome(rows_read=1000, rows_written=998)["blocks_run"] is False


# ── R1.5 — everything that was read landed ───────────────────────────


class TestCompleteLoad:
    """R1.5: ``rows_written == rows_read`` → ``complete_load``."""

    @pytest.mark.parametrize("count", [1, 5, 1000])
    def test_equal_counts_are_a_complete_load(self, count):
        outcome = derive_load_outcome(rows_read=count, rows_written=count)
        assert outcome["outcome"] == OUTCOME_COMPLETE_LOAD
        assert outcome["rows_not_written"] == 0

    def test_one_row_read_and_one_written_is_the_lower_boundary(self):
        outcome = derive_load_outcome(rows_read=1, rows_written=1)
        assert outcome["outcome"] == OUTCOME_COMPLETE_LOAD
        assert outcome["rows_read"] == 1
        assert outcome["rows_written"] == 1

    def test_complete_load_reports_no_missing_fact(self):
        assert derive_load_outcome(rows_read=5, rows_written=5)[OUTCOME_KEY_MISSING] == []

    def test_complete_load_does_not_block_the_run(self):
        assert derive_load_outcome(rows_read=5, rows_written=5)["blocks_run"] is False


# ── The arm ordering, asserted rather than implied ───────────────────


class TestArmOrdering:
    """``(0, 0)`` satisfies ``rows_written == rows_read`` too, so the equality
    arm must be reached last. This is the single ordering mistake that would
    make an empty source report as a successful load."""

    def test_an_empty_source_is_not_reported_as_a_complete_load(self):
        assert derive_load_outcome(rows_read=0, rows_written=0)["outcome"] != OUTCOME_COMPLETE_LOAD

    def test_an_empty_source_is_not_reported_as_no_rows_loaded(self):
        """The other half of the same ordering: R1.3 exists so an empty
        scheduled JDBC poll is not a warning."""
        assert derive_load_outcome(rows_read=0, rows_written=0)["outcome"] != OUTCOME_NO_ROWS_LOADED

    def test_zero_written_is_split_by_whether_the_source_had_rows(self):
        assert derive_load_outcome(rows_read=0, rows_written=0)["outcome"] == OUTCOME_EMPTY_SOURCE
        assert derive_load_outcome(rows_read=1, rows_written=0)["outcome"] == OUTCOME_NO_ROWS_LOADED


# ── R1.6 — the three provenance values, with ``missing`` populated ───


class TestProvenanceLoadMetricsRow:
    """``load_metrics_row`` — the row was read. An individual fact can still be
    absent or unusable, and R1.6 requires naming **which**."""

    def test_absent_rows_read_names_that_fact(self):
        outcome = derive_load_outcome(rows_read=None, rows_written=10, determined_from=FROM_LOAD_METRICS_ROW)
        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ]

    def test_absent_rows_written_names_that_fact(self):
        outcome = derive_load_outcome(rows_read=10, rows_written=None, determined_from=FROM_LOAD_METRICS_ROW)
        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_WRITTEN]

    def test_both_absent_names_both_facts(self):
        outcome = derive_load_outcome(rows_read=None, rows_written=None, determined_from=FROM_LOAD_METRICS_ROW)
        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ, MISSING_ROWS_WRITTEN]

    @pytest.mark.parametrize("unusable", ["10", 10.0, True, [10], {"n": 10}])
    def test_a_wrongly_typed_count_names_the_field_not_the_relationship(self, unusable):
        """Deliberate: an unusable **fact** reports its own name.
        ``consistent_counts`` is reserved for a failed *relationship* between
        two counts that both arrived, so naming it here would say the pair
        disagreed when in truth one value never arrived usably. R1.6 asks which
        fact was unavailable, and this is the answer to that question.

        ``True`` is in this list on purpose: it is an ``int`` subclass, and
        reading it as one row would invent a count.
        """
        outcome = derive_load_outcome(rows_read=unusable, rows_written=0, determined_from=FROM_LOAD_METRICS_ROW)
        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ]

    def test_an_unestablished_count_is_reported_as_absent_not_as_zero(self):
        """Deliberate, and the reason is inside the record: ``0`` is a
        meaningful count in this vocabulary — it is what ``empty_source`` and
        ``no_rows_loaded`` are made of — so reporting ``0`` for a count that was
        never established would contradict ``missing`` in the same breath."""
        outcome = derive_load_outcome(rows_read=None, rows_written=None, determined_from=FROM_LOAD_METRICS_ROW)
        assert outcome["rows_read"] is None
        assert outcome["rows_written"] is None
        assert outcome["rows_not_written"] == 0


class TestProvenanceLoadMetricsRowAbsent:
    """``load_metrics_row_absent`` — no counts row exists for the run. Every
    pipeline created before R1.1 shipped reads this way, indefinitely."""

    def test_no_row_names_both_facts(self):
        outcome = derive_load_outcome(rows_read=None, rows_written=None, determined_from=FROM_LOAD_METRICS_ROW_ABSENT)
        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ, MISSING_ROWS_WRITTEN]
        assert outcome["determined_from"] == FROM_LOAD_METRICS_ROW_ABSENT

    def test_valid_counts_arriving_with_an_absent_row_are_still_not_established(self):
        """Provenance wins over content. Numbers that arrive stamped "there is
        no row they could have come from" are not established facts, and the
        two would otherwise combine into a confident ``complete_load`` built on
        a contradiction."""
        outcome = derive_load_outcome(rows_read=100, rows_written=100, determined_from=FROM_LOAD_METRICS_ROW_ABSENT)
        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ, MISSING_ROWS_WRITTEN]
        assert outcome["rows_read"] is None
        assert outcome["rows_written"] is None


class TestProvenanceLoadMetricsRowIncomplete:
    """``load_metrics_row_incomplete`` — the row is there and does not carry
    both counts. ``LGR-78ce0b7-8`` (template / ratified) is why this must not
    collapse into ``absent``: "a run reported as unverifiable when the real
    problem is a missing record" is the prohibited outcome."""

    def test_an_incomplete_row_names_the_fact_it_lacks(self):
        outcome = derive_load_outcome(rows_read=42, rows_written=None, determined_from=FROM_LOAD_METRICS_ROW_INCOMPLETE)
        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_WRITTEN]
        assert outcome["determined_from"] == FROM_LOAD_METRICS_ROW_INCOMPLETE

    def test_incomplete_and_absent_are_distinguishable_in_the_record(self):
        """Both are ``not_established``, so the distinction has to survive in
        the other fields or it does not survive at all."""
        absent = derive_load_outcome(rows_read=None, rows_written=None, determined_from=FROM_LOAD_METRICS_ROW_ABSENT)
        incomplete = derive_load_outcome(
            rows_read=None, rows_written=None, determined_from=FROM_LOAD_METRICS_ROW_INCOMPLETE
        )
        assert absent["outcome"] == incomplete["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert absent["determined_from"] != incomplete["determined_from"]
        assert absent["reason"] != incomplete["reason"]


class TestUnrecognisedProvenance:
    """Anything that is not one of the three yields ``not_established`` too —
    including a value of the wrong type, because a provenance annotation must
    never be the reason a page 500s."""

    @pytest.mark.parametrize("provenance", ["", "made_up_source", None, 7])
    def test_an_unrecognised_provenance_establishes_nothing(self, provenance):
        outcome = derive_load_outcome(rows_read=10, rows_written=10, determined_from=provenance)
        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_ROWS_READ, MISSING_ROWS_WRITTEN]


# ── The inverted / negative-count guard ──────────────────────────────


class TestInconsistentCounts:
    """A pair that cannot describe a load reports the **relationship** as
    missing, not either count: both arrived, so naming one would be arbitrary.
    ``rows_not_written`` is never negative."""

    @pytest.mark.parametrize(
        ("rows_read", "rows_written"),
        [
            (5, 9),  # more written than read
            (0, 1),  # the smallest inversion there is
            (-1, 0),  # negative rows_read
            (5, -1),  # negative rows_written
            (-5, -5),  # both negative, and equal, so the R1.5 arm would take it
            (-1, -2),  # both negative, inverted as well
        ],
    )
    def test_an_impossible_pair_is_not_established(self, rows_read, rows_written):
        outcome = derive_load_outcome(rows_read=rows_read, rows_written=rows_written)
        assert outcome["outcome"] == OUTCOME_NOT_ESTABLISHED
        assert outcome[OUTCOME_KEY_MISSING] == [MISSING_CONSISTENT_COUNTS]

    @pytest.mark.parametrize(
        ("rows_read", "rows_written"),
        [(5, 9), (0, 1), (-1, 0), (5, -1), (-5, -5), (-1, -2)],
    )
    def test_rows_not_written_is_never_negative(self, rows_read, rows_written):
        """``read - written`` on an inverted pair is negative arithmetic, and a
        negative "rows withheld" count rendered on a dashboard is worse than no
        count at all."""
        assert derive_load_outcome(rows_read=rows_read, rows_written=rows_written)["rows_not_written"] == 0

    def test_negative_equal_counts_do_not_reach_complete_load(self):
        """``-5 == -5`` satisfies R1.5's condition arithmetically. The guard has
        to run before the equality arm, not after it."""
        assert derive_load_outcome(rows_read=-5, rows_written=-5)["outcome"] != OUTCOME_COMPLETE_LOAD

    def test_negative_zero_pair_does_not_reach_empty_source(self):
        """``rows_written == 0`` with a negative ``rows_read`` must not read as
        a legitimate empty-source no-op."""
        assert derive_load_outcome(rows_read=-1, rows_written=0)["outcome"] != OUTCOME_EMPTY_SOURCE

    def test_an_inconsistent_pair_reports_neither_count(self):
        outcome = derive_load_outcome(rows_read=5, rows_written=9)
        assert outcome["rows_read"] is None
        assert outcome["rows_written"] is None

    def test_an_inconsistent_pair_does_not_block_the_run(self):
        assert derive_load_outcome(rows_read=5, rows_written=9)["blocks_run"] is False


# ── One record shape, and blocks_run, across every arm ───────────────

# One input per outcome, plus the two other ``not_established`` routes, so the
# sweep below covers every arm of the function rather than every outcome name.
EVERY_ARM = [
    ("no_rows_loaded (R1.2)", {"rows_read": 5, "rows_written": 0}, OUTCOME_NO_ROWS_LOADED),
    ("empty_source (R1.3)", {"rows_read": 0, "rows_written": 0}, OUTCOME_EMPTY_SOURCE),
    ("partial_load (R1.4)", {"rows_read": 10, "rows_written": 4}, OUTCOME_PARTIAL_LOAD),
    ("complete_load (R1.5)", {"rows_read": 10, "rows_written": 10}, OUTCOME_COMPLETE_LOAD),
    (
        "not_established, absent fact (R1.6)",
        {"rows_read": None, "rows_written": 10},
        OUTCOME_NOT_ESTABLISHED,
    ),
    (
        "not_established, provenance (R1.6)",
        {"rows_read": 10, "rows_written": 10, "determined_from": FROM_LOAD_METRICS_ROW_ABSENT},
        OUTCOME_NOT_ESTABLISHED,
    ),
    (
        "not_established, inconsistent pair (R1.6)",
        {"rows_read": 4, "rows_written": 10},
        OUTCOME_NOT_ESTABLISHED,
    ),
]


class TestEveryArmIsAPersistableRecord:
    @pytest.mark.parametrize(("label", "kwargs", "expected"), EVERY_ARM, ids=[case[0] for case in EVERY_ARM])
    def test_the_arm_returns_the_outcome_it_is_named_for(self, label, kwargs, expected):
        assert derive_load_outcome(**kwargs)["outcome"] == expected

    @pytest.mark.parametrize(("label", "kwargs", "expected"), EVERY_ARM, ids=[case[0] for case in EVERY_ARM])
    def test_blocks_run_is_false_in_every_arm(self, label, kwargs, expected):
        """R1.12 and do-NOT 4. The module routes all five arms through one
        assembly point precisely so a branch edit cannot drop this field; this
        asserts the guarantee rather than the structure that provides it.

        ``is False`` rather than ``== False``: a falsy stand-in — ``0``,
        ``None``, ``""`` — would satisfy equality and would not be the explicit
        statement R1.12 asks for.
        """
        assert derive_load_outcome(**kwargs)["blocks_run"] is False

    @pytest.mark.parametrize(("label", "kwargs", "expected"), EVERY_ARM, ids=[case[0] for case in EVERY_ARM])
    def test_the_key_set_is_the_same_in_every_arm(self, label, kwargs, expected):
        """One stable shape, so a caller can read ``rows_not_written`` without
        first branching on which outcome it received."""
        assert set(derive_load_outcome(**kwargs)) == EXPECTED_KEYS

    @pytest.mark.parametrize(("label", "kwargs", "expected"), EVERY_ARM, ids=[case[0] for case in EVERY_ARM])
    def test_every_arm_states_a_reason(self, label, kwargs, expected):
        assert derive_load_outcome(**kwargs)["reason"]

    @pytest.mark.parametrize("provenance", ALL_THREE_PROVENANCES)
    def test_the_provenance_is_echoed_unchanged(self, provenance):
        outcome = derive_load_outcome(rows_read=10, rows_written=10, determined_from=provenance)
        assert outcome["determined_from"] == provenance

    def test_missing_is_empty_on_every_established_outcome(self):
        """The four established arms establish both facts, so nothing is
        missing on any of them — a stray entry there would make an honest
        answer read as a partial one."""
        for kwargs in (
            {"rows_read": 5, "rows_written": 0},
            {"rows_read": 0, "rows_written": 0},
            {"rows_read": 10, "rows_written": 4},
            {"rows_read": 10, "rows_written": 10},
        ):
            assert derive_load_outcome(**kwargs)[OUTCOME_KEY_MISSING] == []
