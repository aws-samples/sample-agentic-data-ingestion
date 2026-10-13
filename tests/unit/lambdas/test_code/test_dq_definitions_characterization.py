# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: what the three data-quality metrics MEAN (R10.2, R10.5).

Spec: `.kiro/specs/p42-real-spark-verification/` task 1.1 — the characterization
baseline that must land **before** the executor gains a Spark counterpart.

════════════════════════════════════════════════════════════════════════
THE ORACLE IS THE DEFINITION, NOT THE PANDAS PATH'S CURRENT OUTPUT.
════════════════════════════════════════════════════════════════════════

Requirement 10.2 and design §4.9 state the three metrics as sentences:

* ``completeness``       — the proportion of **non-null cells**.
* ``uniqueness``         — the proportion of rows that are **not exact
                           duplicates of an earlier row**.
* ``schema_compliance``  — the proportion of **declared target columns
                           present** in the transform output.

Every number in a frame's ``expected`` map below is hand-computed from those
three sentences over a frame small enough to count by eye, and the arithmetic is
written out in each frame's comment. None of them is read back from
``_build_validation_code``'s output.

That direction is the whole point of the task, and it is requirement 10.5's
words: the pandas arithmetic is the approximation being *replaced*, so an
expectation derived from it would pin the bug rather than the requirement.
`LGR-28b4d75-5` (template / ratified) states the general form — "a test written
from the producer's output cannot detect the producer drifting, because the
output is its own oracle."

**Where the definition and pandas disagree, and why the definition wins.**
Design C7 predicts four divergences between the pandas expressions at
``code_interpreter_runner.py:556-563`` and their Spark forms. One of them is a
*value* divergence on the metric this file characterises:

    pandas ``_df[c].isna().sum()`` counts a cell as null when it holds ``None``
    **or** a float ``NaN``. Spark's ``col(c).isNull()`` is False for ``NaN``.

A float ``NaN`` is a present IEEE floating-point value, not an absent cell, so
under the sentence "the proportion of non-null cells" it is **not** a null and
completeness is correspondingly higher. That is the direction C7 records
("completeness rises") and the direction the Spark form implements. The frames
below carry a ``NaN`` on purpose so the divergence is pinned rather than
discovered later as an unexplained regression.

════════════════════════════════════════════════════════════════════════
RECORDED OBSERVATION — WHAT THE PANDAS PATH REPORTS FOR THESE SAME FRAMES
════════════════════════════════════════════════════════════════════════

Each frame's ``observed_pandas`` map records the value the *current* pandas path
produces for that same frame, so the later movement is attributable to the
executor change rather than being noticed as a diff in a report. Two things make
that record trustworthy rather than decorative:

1. ``TestPandasArithmeticIsStillTheOneObserved`` asserts that every pandas
   expression the observations were derived from is still present verbatim in
   ``_build_validation_code``'s output. Edit the arithmetic and this file fails,
   which forces the observation to be re-derived instead of silently ageing.
2. ``TestPandasPathExecuted`` executes the **real** generated validation code
   against the same frames and checks the observations by execution — under
   ``pytest.importorskip("pandas")``.

**That last class SKIPS in this repo, and the skip is the expected outcome.**
pandas is not installed in ``agentic_data_ingestion/.venv`` and is not in
``pyproject.toml``; design §3 do-NOT 7 (`LGR-4dd9b42-1`) forbids adding it, and a
dependency added to satisfy a test is a dependency change wearing a test's
clothes. Same posture as ``test_literal_receiver_pandas_execution.py``.

**Every observation below was nevertheless confirmed by execution**, in two
throwaway venvs created OUTSIDE the repo under ``/tmp`` — the same method design
§5 used for its F-table:

* pandas **3.0.5** / CPython 3.14 — 81 passed, 0 skipped.
* pandas **2.2.3** / CPython 3.12 — 81 passed, 0 skipped.

So the numbers recorded here are measured, not predicted, and they are stable
across both pandas majors. Reproduce with::

    python -m venv /tmp/probe && /tmp/probe/bin/pip install pandas pytest boto3
    /tmp/probe/bin/python -m pytest \
        tests/unit/lambdas/test_code/test_dq_definitions_characterization.py \
        --noconftest -q

``dtypes`` is deliberately **not** given expected values, and that is the one
thing in this file left unpinned on purpose. C7 predicts that every ``dtypes``
entry moves because the two vocabularies are disjoint
(``object``/``int64``/``float64`` against ``string``/``bigint``/``double``) — but
the pandas side is not one vocabulary either: a string column reports ``object``
on pandas 2.2.3 and ``str`` on 3.0.5, and design §5's N1 records that nothing in
this repo pins which pandas the AWS-managed interpreter runs. A baseline that
disagrees with itself across the two majors measured is not a baseline. The
attribution guard covers the expression; the values are left to shadow mode
(task 10.1), which measures the pair actually in use rather than predicting it.

Validates: Requirements 10.2, 10.5
"""

import io
import json
import math
import os
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)

# The real module — it touches no AWS at import, so the generated validation
# code asserted below is the text production sends to the interpreter.
from code_interpreter_runner import CodeInterpreterTestRunner

# ══════════════════════════════════════════════════════════════════════
# Frame representation
# ══════════════════════════════════════════════════════════════════════

#: An absent cell. Distinct from :data:`NAN` on purpose — the whole divergence
#: this file characterises is that the pandas path cannot tell them apart.
NULL = None

#: A present float that happens to be IEEE NaN. NOT an absent cell.
NAN = float("nan")


def _cell_key(value):
    """Return a hashable identity for *value* for exact-row comparison.

    ``float('nan') != float('nan')`` in Python, so a raw tuple comparison would
    report two byte-identical rows as distinct and the uniqueness oracle would
    disagree with the sentence it implements. "Exact duplicate of an earlier
    row" means the same row, and a row is the same as itself whatever floats it
    carries, so NaN is normalised to a sentinel token here.
    """
    if isinstance(value, float) and math.isnan(value):
        return ("__nan__",)
    return (type(value).__name__, value)


def _is_absent(value) -> bool:
    """Return True only for a genuinely absent cell.

    ``NaN`` is a value, so it is not absent. This one line is where requirement
    10.2's word "non-null" is interpreted, and it is the interpretation the
    Spark form implements (design C7).
    """
    return value is None


@dataclass(frozen=True)
class Frame:
    """A hand-built frame with hand-computed answers.

    ``rows`` is row-major so each literal below reads as a table.
    """

    name: str
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    declared_targets: tuple[str, ...]
    #: Definition-derived expectations. Absent keys are cases the definition
    #: does not decide (see ``NO_DECLARED_TARGETS``).
    expected: dict = field(default_factory=dict)
    #: What the current pandas path reports for this same frame.
    observed_pandas: dict = field(default_factory=dict)

    @property
    def cell_count(self) -> int:
        return len(self.rows) * len(self.columns)


# ══════════════════════════════════════════════════════════════════════
# Definition oracles — pure Python, no pandas, no Spark
# ══════════════════════════════════════════════════════════════════════


def null_counts_from_definition(frame: Frame) -> dict:
    """Per-column count of absent cells."""
    return {name: sum(1 for row in frame.rows if _is_absent(row[idx])) for idx, name in enumerate(frame.columns)}


def completeness_from_definition(frame: Frame) -> float:
    """The proportion of non-null cells, as a percentage rounded to 1 dp."""
    nulls = sum(null_counts_from_definition(frame).values())
    return round((1 - nulls / max(frame.cell_count, 1)) * 100, 1)


def duplicate_rows_from_definition(frame: Frame) -> int:
    """Count of rows that are exact duplicates of an earlier row."""
    seen: set = set()
    duplicates = 0
    for row in frame.rows:
        key = tuple(_cell_key(cell) for cell in row)
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def uniqueness_from_definition(frame: Frame) -> float:
    """The proportion of rows that are NOT exact duplicates of an earlier row."""
    dups = duplicate_rows_from_definition(frame)
    return round((1 - dups / max(len(frame.rows), 1)) * 100, 1)


def missing_target_columns_from_definition(frame: Frame) -> list:
    """Declared target columns absent from the frame."""
    return [c for c in frame.declared_targets if c not in frame.columns]


def schema_compliance_from_definition(frame: Frame) -> float:
    """The proportion of declared target columns present, as a percentage."""
    declared = len(frame.declared_targets)
    if declared == 0:
        raise ValueError(
            "the definition does not decide a proportion over zero declared target columns; "
            "see NO_DECLARED_TARGETS, which is recorded as an observation only"
        )
    present = declared - len(missing_target_columns_from_definition(frame))
    return round(present / declared * 100, 1)


def overall_from_components(completeness: float, uniqueness: float, schema_compliance: float) -> float:
    """Mean of the three already-rounded components, matching the emitted shape.

    The production expression averages the ROUNDED components
    (``code_interpreter_runner.py:564``), so the oracle does too — this one is a
    shape fact about the payload rather than a metric definition, and R10.1 pins
    the payload's key set across executors.
    """
    return round((completeness + uniqueness + schema_compliance) / 3, 1)


# ══════════════════════════════════════════════════════════════════════
# The frames
# ══════════════════════════════════════════════════════════════════════

# ── F1 ────────────────────────────────────────────────────────────────
# The divergence C7 predicts: one absent cell in a float column and one float
# NaN in the same column, so the two readings of "null" give different answers.
#
#   id  amount  label
#   1   10.0    "a"
#   2   NULL    "b"     <- absent
#   3   NAN     NULL    <- NaN is a VALUE; label is absent
#   4   40.0    "d"
#
# 4 rows x 3 columns = 12 cells.
# Definition nulls: amount 1 (row 2), label 1 (row 3)      -> 2
#   completeness = (1 - 2/12) * 100 = 83.333... -> 83.3
# Pandas nulls:   amount 2 (isna() is True for NaN), label 1 -> 3
#   completeness = (1 - 3/12) * 100 = 75.0
# All four rows are distinct       -> uniqueness       = 100.0 (both)
# All declared targets are present -> schema_compliance = 100.0 (both)
NULLS_INCLUDING_NAN = Frame(
    name="nulls_including_nan",
    columns=("id", "amount", "label"),
    rows=(
        (1, 10.0, "a"),
        (2, NULL, "b"),
        (3, NAN, NULL),
        (4, 40.0, "d"),
    ),
    declared_targets=("id", "amount", "label"),
    expected={
        "null_counts": {"id": 0, "amount": 1, "label": 1},
        "duplicates": 0,
        "completeness": 83.3,
        "uniqueness": 100.0,
        "schema_compliance": 100.0,
        "missing_columns": [],
        "overall_quality": 94.4,  # (83.3 + 100.0 + 100.0) / 3
    },
    observed_pandas={
        "null_counts": {"id": 0, "amount": 2, "label": 1},
        "duplicates": 0,
        "completeness": 75.0,
        "uniqueness": 100.0,
        "schema_compliance": 100.0,
        "missing_columns": [],
        "overall_quality": 91.7,  # (75.0 + 100.0 + 100.0) / 3
    },
)

# ── F2 ────────────────────────────────────────────────────────────────
# Exact duplicate rows, no NaN anywhere. C7 records that the two duplicate
# expressions AGREE on exact duplicates, so this frame is the control: it pins
# the definition and records no movement.
#
#   k    v
#   "a"  1
#   "b"  2
#   "a"  1   <- exact duplicate of row 0
#   "a"  1   <- exact duplicate of row 0
#   "c"  3
#
# 5 rows x 2 columns = 10 cells, 0 absent -> completeness = 100.0
# 2 rows duplicate an earlier row -> uniqueness = (1 - 2/5) * 100 = 60.0
EXACT_DUPLICATE_ROWS = Frame(
    name="exact_duplicate_rows",
    columns=("k", "v"),
    rows=(
        ("a", 1),
        ("b", 2),
        ("a", 1),
        ("a", 1),
        ("c", 3),
    ),
    declared_targets=("k", "v"),
    expected={
        "null_counts": {"k": 0, "v": 0},
        "duplicates": 2,
        "completeness": 100.0,
        "uniqueness": 60.0,
        "schema_compliance": 100.0,
        "missing_columns": [],
        "overall_quality": 86.7,  # (100.0 + 60.0 + 100.0) / 3
    },
    observed_pandas={
        "null_counts": {"k": 0, "v": 0},
        "duplicates": 2,
        "completeness": 100.0,
        "uniqueness": 60.0,
        "schema_compliance": 100.0,
        "missing_columns": [],
        "overall_quality": 86.7,
    },
)

# ── F3 ────────────────────────────────────────────────────────────────
# Duplicate rows that CONTAIN a NaN — C7's second row names NaN-versus-itself as
# where the duplicate expressions could part company, and the completeness
# divergence bites here as well.
#
#   k    v
#   "a"  NAN
#   "a"  NAN   <- byte-identical to row 0
#
# 2 rows x 2 columns = 4 cells.
# Definition nulls: 0 (NaN is a value)   -> completeness = 100.0
# Pandas nulls:     v = 2                -> completeness = (1 - 2/4) * 100 = 50.0
# Row 1 duplicates row 0                 -> uniqueness = 50.0
#   pandas `duplicated()` factorizes, which treats NaN as equal to NaN, so it
#   reports the same 1 duplicate. Recorded as agreement.
DUPLICATE_ROWS_CONTAINING_NAN = Frame(
    name="duplicate_rows_containing_nan",
    columns=("k", "v"),
    rows=(
        ("a", NAN),
        ("a", NAN),
    ),
    declared_targets=("k", "v"),
    expected={
        "null_counts": {"k": 0, "v": 0},
        "duplicates": 1,
        "completeness": 100.0,
        "uniqueness": 50.0,
        "schema_compliance": 100.0,
        "missing_columns": [],
        "overall_quality": 83.3,  # (100.0 + 50.0 + 100.0) / 3
    },
    observed_pandas={
        "null_counts": {"k": 0, "v": 2},
        "duplicates": 1,
        "completeness": 50.0,
        "uniqueness": 50.0,
        "schema_compliance": 100.0,
        "missing_columns": [],
        "overall_quality": 66.7,  # (50.0 + 50.0 + 100.0) / 3
    },
)

# ── F4 ────────────────────────────────────────────────────────────────
# A declared target column absent from the output. §2 records that
# schema_compliance is a column-set comparison and is the one metric that does
# NOT move, so this frame pins the definition and records no movement.
#
#   declared targets: id, name, email, created_at   (4)
#   frame columns:    id, name,        created_at   ("email" absent)
#
# schema_compliance = 3/4 * 100 = 75.0
DECLARED_TARGET_COLUMN_ABSENT = Frame(
    name="declared_target_column_absent",
    columns=("id", "name", "created_at"),
    rows=(
        (1, "ann", "2026-01-01"),
        (2, "bob", "2026-01-02"),
    ),
    declared_targets=("id", "name", "email", "created_at"),
    expected={
        "null_counts": {"id": 0, "name": 0, "created_at": 0},
        "duplicates": 0,
        "completeness": 100.0,
        "uniqueness": 100.0,
        "schema_compliance": 75.0,
        "missing_columns": ["email"],
        "overall_quality": 91.7,  # (100.0 + 100.0 + 75.0) / 3
    },
    observed_pandas={
        "null_counts": {"id": 0, "name": 0, "created_at": 0},
        "duplicates": 0,
        "completeness": 100.0,
        "uniqueness": 100.0,
        "schema_compliance": 75.0,
        "missing_columns": ["email"],
        "overall_quality": 91.7,
    },
)

# ── F5 ────────────────────────────────────────────────────────────────
# Zero declared target columns. Carried as an OBSERVATION ONLY: "the proportion
# of declared target columns present" is a proportion over an empty set, which
# the sentence does not decide, and the current path short-circuits to a vacuous
# 100.0 (`code_interpreter_runner.py:563`). Recorded here so the Spark
# counterpart reproduces it knowingly rather than by accident — R12.4's gate on
# whether a score is reported at all lives in `aggregate_check_results`, not in
# this arithmetic, and is a different task's business.
NO_DECLARED_TARGETS = Frame(
    name="no_declared_targets",
    columns=("id",),
    rows=((1,), (2,)),
    declared_targets=(),
    expected={
        "null_counts": {"id": 0},
        "duplicates": 0,
        "completeness": 100.0,
        "uniqueness": 100.0,
        # No `schema_compliance` and no `overall_quality`: both depend on a
        # proportion the definition does not decide.
        "missing_columns": [],
    },
    observed_pandas={
        "null_counts": {"id": 0},
        "duplicates": 0,
        "completeness": 100.0,
        "uniqueness": 100.0,
        "schema_compliance": 100.0,  # the vacuous short-circuit
        "missing_columns": [],
        "overall_quality": 100.0,
    },
)

ALL_FRAMES = (
    NULLS_INCLUDING_NAN,
    EXACT_DUPLICATE_ROWS,
    DUPLICATE_ROWS_CONTAINING_NAN,
    DECLARED_TARGET_COLUMN_ABSENT,
    NO_DECLARED_TARGETS,
)

#: The frames on which the definition and the pandas path are expected to give
#: different answers — exactly the frames carrying a float ``NaN``. C7's
#: prediction, turned into an assertion rather than left as prose.
FRAMES_PREDICTED_TO_MOVE = frozenset({"nulls_including_nan", "duplicate_rows_containing_nan"})


def _target_schema(frame: Frame) -> dict:
    """The `target_schema` shape `_build_validation_code` reads."""
    return {"properties": {name: {"type": "string"} for name in frame.declared_targets}}


@pytest.fixture
def runner():
    """A runner whose boto client is a mock — no builder here touches it."""
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = MagicMock()
        return CodeInterpreterTestRunner(region="us-west-2")


# ══════════════════════════════════════════════════════════════════════
# The oracles are validated BY the hand-computed literals, not the reverse
# ══════════════════════════════════════════════════════════════════════


class TestOracleAgreesWithHandComputedAnswers:
    """The helpers above reproduce the numbers written out in each frame's comment.

    Direction matters: the literals are authoritative and the oracle is what is
    under test here. That keeps the definition tests from being circular through
    a helper, while still leaving a reusable oracle for Property 13 (task 6.4),
    which needs a function rather than five literals.
    """

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_null_counts_oracle(self, frame):
        assert null_counts_from_definition(frame) == frame.expected["null_counts"]

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_duplicate_oracle(self, frame):
        assert duplicate_rows_from_definition(frame) == frame.expected["duplicates"]

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_completeness_oracle(self, frame):
        assert completeness_from_definition(frame) == frame.expected["completeness"]

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_uniqueness_oracle(self, frame):
        assert uniqueness_from_definition(frame) == frame.expected["uniqueness"]

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_missing_columns_oracle(self, frame):
        assert missing_target_columns_from_definition(frame) == frame.expected["missing_columns"]

    @pytest.mark.parametrize(
        "frame",
        [f for f in ALL_FRAMES if "schema_compliance" in f.expected],
        ids=lambda f: f.name,
    )
    def test_schema_compliance_oracle(self, frame):
        assert schema_compliance_from_definition(frame) == frame.expected["schema_compliance"]

    @pytest.mark.parametrize(
        "frame",
        [f for f in ALL_FRAMES if "overall_quality" in f.expected],
        ids=lambda f: f.name,
    )
    def test_overall_quality_oracle(self, frame):
        assert (
            overall_from_components(
                frame.expected["completeness"],
                frame.expected["uniqueness"],
                frame.expected["schema_compliance"],
            )
            == frame.expected["overall_quality"]
        )


# ══════════════════════════════════════════════════════════════════════
# The definitions themselves — R10.2
# ══════════════════════════════════════════════════════════════════════


class TestCompletenessIsTheProportionOfNonNullCells:
    def test_a_nan_is_a_value_and_does_not_reduce_completeness(self):
        """The load-bearing reading of "non-null" (C7, R10.5).

        ``amount`` carries one absent cell and one ``NaN``. Only the absent cell
        is a null, so ``amount``'s null count is 1 rather than 2.
        """
        counts = null_counts_from_definition(NULLS_INCLUDING_NAN)
        assert counts["amount"] == 1
        assert completeness_from_definition(NULLS_INCLUDING_NAN) == 83.3

    def test_a_frame_whose_only_missing_marker_is_nan_is_fully_complete(self):
        frame = DUPLICATE_ROWS_CONTAINING_NAN
        assert null_counts_from_definition(frame) == {"k": 0, "v": 0}
        assert completeness_from_definition(frame) == 100.0

    def test_a_frame_with_no_absent_cells_is_fully_complete(self):
        assert completeness_from_definition(EXACT_DUPLICATE_ROWS) == 100.0

    def test_completeness_counts_cells_not_rows(self):
        """2 of 12 cells absent is 83.3, not 2 of 4 rows (50.0).

        The denominator is ``record_count * column_count``, which is why a frame
        with two affected rows out of four still scores in the eighties.
        """
        frame = NULLS_INCLUDING_NAN
        assert frame.cell_count == 12
        assert completeness_from_definition(frame) == 83.3
        rows_touched = sum(1 for row in frame.rows if any(_is_absent(c) for c in row))
        assert rows_touched == 2
        assert completeness_from_definition(frame) != round((1 - rows_touched / len(frame.rows)) * 100, 1)


class TestUniquenessIsRowsThatAreNotDuplicatesOfAnEarlierRow:
    def test_two_duplicates_of_one_earlier_row_both_count(self):
        """Three identical rows contribute TWO duplicates, not one.

        "duplicates of an earlier row" counts occurrences after the first, so
        the surviving row is the first one — the same keep-first reading the
        pandas expression has.
        """
        assert duplicate_rows_from_definition(EXACT_DUPLICATE_ROWS) == 2
        assert uniqueness_from_definition(EXACT_DUPLICATE_ROWS) == 60.0

    def test_a_row_is_an_exact_duplicate_of_itself_even_carrying_nan(self):
        frame = DUPLICATE_ROWS_CONTAINING_NAN
        assert duplicate_rows_from_definition(frame) == 1
        assert uniqueness_from_definition(frame) == 50.0

    def test_distinct_rows_are_fully_unique(self):
        assert uniqueness_from_definition(NULLS_INCLUDING_NAN) == 100.0

    def test_an_absent_cell_does_not_make_a_row_a_duplicate(self):
        """A row differing only by an absent cell is a distinct row."""
        frame = Frame(
            name="ad_hoc",
            columns=("k", "v"),
            rows=(("a", 1), ("a", NULL)),
            declared_targets=("k", "v"),
        )
        assert duplicate_rows_from_definition(frame) == 0
        assert uniqueness_from_definition(frame) == 100.0


class TestSchemaComplianceIsTheProportionOfDeclaredTargetsPresent:
    def test_one_absent_column_of_four_declared_scores_seventy_five(self):
        frame = DECLARED_TARGET_COLUMN_ABSENT
        assert missing_target_columns_from_definition(frame) == ["email"]
        assert schema_compliance_from_definition(frame) == 75.0

    def test_all_declared_columns_present_scores_one_hundred(self):
        assert schema_compliance_from_definition(NULLS_INCLUDING_NAN) == 100.0

    def test_an_extra_column_beyond_the_declared_set_does_not_reduce_the_score(self):
        """The metric is "declared targets present", not "columns declared".

        An output column nobody declared is outside the proportion entirely.
        """
        frame = Frame(
            name="ad_hoc_extra",
            columns=("id", "name", "surprise"),
            rows=((1, "ann", "x"),),
            declared_targets=("id", "name"),
        )
        assert missing_target_columns_from_definition(frame) == []
        assert schema_compliance_from_definition(frame) == 100.0

    def test_the_definition_does_not_decide_an_empty_declared_set(self):
        """Zero declared targets is a proportion over an empty set.

        The current path answers 100.0 anyway; that is recorded as an
        observation on ``NO_DECLARED_TARGETS``, not derived here.
        """
        with pytest.raises(ValueError, match="does not decide"):
            schema_compliance_from_definition(NO_DECLARED_TARGETS)
        assert NO_DECLARED_TARGETS.observed_pandas["schema_compliance"] == 100.0
        assert "schema_compliance" not in NO_DECLARED_TARGETS.expected


# ══════════════════════════════════════════════════════════════════════
# The recorded observation, and which numbers are predicted to move
# ══════════════════════════════════════════════════════════════════════


class TestRecordedPandasObservation:
    """Pins the recorded observation and the shape of the predicted movement.

    These assertions are over the recorded table, not over executed pandas, so
    what they protect is the record's internal consistency with C7: if someone
    edits an observation to match a new number without also moving the
    prediction, this fails.
    """

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_every_frame_carries_a_full_observation(self, frame):
        """No frame may record a definition value with no observed counterpart."""
        assert set(frame.observed_pandas) == {
            "null_counts",
            "duplicates",
            "completeness",
            "uniqueness",
            "schema_compliance",
            "missing_columns",
            "overall_quality",
        }

    def test_exactly_the_nan_bearing_frames_are_predicted_to_move(self):
        moved = {
            frame.name
            for frame in ALL_FRAMES
            if any(frame.expected[k] != frame.observed_pandas[k] for k in frame.expected)
        }
        assert moved == FRAMES_PREDICTED_TO_MOVE

    def test_every_frame_predicted_to_move_carries_a_nan(self):
        for frame in ALL_FRAMES:
            carries_nan = any(isinstance(cell, float) and math.isnan(cell) for row in frame.rows for cell in row)
            assert carries_nan == (frame.name in FRAMES_PREDICTED_TO_MOVE)

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_completeness_never_falls_when_derived_from_the_definition(self, frame):
        """C7's direction: the pandas reading counts MORE nulls, so it scores lower.

        A future change that made the definition-derived completeness lower than
        the pandas one would contradict the only value divergence C7 predicts,
        and is far likelier to be a mistake than a discovery.
        """
        assert frame.expected["completeness"] >= frame.observed_pandas["completeness"]

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_uniqueness_does_not_move_on_any_frame_here(self, frame):
        """C7 records agreement on exact duplicates; nothing here contradicts it."""
        assert frame.expected["uniqueness"] == frame.observed_pandas["uniqueness"]

    @pytest.mark.parametrize(
        "frame",
        [f for f in ALL_FRAMES if "schema_compliance" in f.expected],
        ids=lambda f: f.name,
    )
    def test_schema_compliance_does_not_move(self, frame):
        """§2: schema_compliance is a column-set comparison and is executor-neutral."""
        assert frame.expected["schema_compliance"] == frame.observed_pandas["schema_compliance"]
        assert frame.expected["missing_columns"] == frame.observed_pandas["missing_columns"]


class TestPandasArithmeticIsStillTheOneObserved:
    """The attribution guard.

    The observations above were derived by reading four expressions in
    ``_build_validation_code``. If the arithmetic changes, the observations stop
    describing anything and the later movement stops being attributable — so
    this asserts the expressions are still there, verbatim, in the code
    production actually sends to the interpreter.

    This is NOT a claim that the pandas arithmetic is correct. It is the fence
    that stops the recorded baseline ageing silently.
    """

    OBSERVED_EXPRESSIONS = (
        # pandas isna() — True for None AND NaN. The source of the one value
        # divergence C7 predicts.
        "null_counts = {c: int(_df[c].isna().sum()) for c in _df.columns}",
        # pandas dtype vocabulary — object/int64/float64. C7 predicts every
        # entry moves; no expected values are pinned for it here.
        "dtypes = {c: str(_df[c].dtype) for c in _df.columns}",
        # keep-first duplicate marking.
        "duplicates = int(_df.duplicated().sum())",
        # The three metric formulas, cells-not-rows denominator included.
        "completeness = round((1 - sum(null_counts.values()) / max(record_count * len(_df.columns), 1)) * 100, 1)",
        "uniqueness = round((1 - duplicates / max(record_count, 1)) * 100, 1)",
        "schema_compliance = round((1 - len(missing) / max(len(expected), 1)) * 100, 1) if expected else 100.0",
        "overall_quality = round((completeness + uniqueness + schema_compliance) / 3, 1)",
    )

    @pytest.mark.parametrize("expression", OBSERVED_EXPRESSIONS)
    def test_expression_is_still_emitted(self, runner, expression):
        code = runner._build_validation_code(_target_schema(DECLARED_TARGET_COLUMN_ABSENT))
        assert expression in code, (
            f"`_build_validation_code` no longer emits `{expression}`. The pandas observations recorded in "
            "this file were derived from that expression, so they must be re-derived before this assertion "
            "is updated — see the module docstring."
        )

    def test_the_declared_targets_reach_the_generated_expected_list(self, runner):
        """`expected` is the declared target set — the schema_compliance input."""
        frame = DECLARED_TARGET_COLUMN_ABSENT
        code = runner._build_validation_code(_target_schema(frame))
        assert f"expected = {json.dumps(list(frame.declared_targets))}" in code

    def test_the_payload_carries_the_nine_keys_r10_1_pins(self, runner):
        """R10.1: the Spark counterpart must emit this same key set."""
        code = runner._build_validation_code(_target_schema(NULLS_INCLUDING_NAN))
        for key in (
            "record_count",
            "null_counts",
            "dtypes",
            "duplicates",
            "schema_compliance",
            "completeness",
            "uniqueness",
            "overall_quality",
            "missing_columns",
        ):
            assert f'"{key}"' in code


# ══════════════════════════════════════════════════════════════════════
# Confirming the observation by execution — SKIPS HERE, BY DESIGN
# ══════════════════════════════════════════════════════════════════════


class TestPandasPathExecuted:
    """Execute the REAL generated validation code and check the observations.

    ════════════════════════════════════════════════════════════════════
    THIS CLASS SKIPS IN THIS REPO AND THE SKIP IS THE EXPECTED OUTCOME.
    ════════════════════════════════════════════════════════════════════

    pandas is not installed and design §3 do-NOT 7 (`LGR-4dd9b42-1`) forbids
    adding it. A green run here means an environment that has pandas, and it is
    the strongest available confirmation of the recorded observations: the
    executed code comes from ``_build_validation_code`` and the numbers are read
    back through the production parser, so nothing is transcribed
    (`LGR-28b4d75-5`).

    These ran green on pandas 3.0.5 / CPython 3.14 and on pandas 2.2.3 /
    CPython 3.12, in throwaway venvs under ``/tmp``. The module docstring has
    the command.

    ``dtypes`` is not asserted — the pandas dtype vocabulary is version-
    dependent, and the module docstring records why no expected values exist
    for it.
    """

    @staticmethod
    def _observed_via_production_path(frame: Frame) -> dict:
        pd = pytest.importorskip("pandas", reason="pandas is not a repository dependency (design §3 do-NOT 7)")

        columns = {name: [row[idx] for row in frame.rows] for idx, name in enumerate(frame.columns)}
        df_transformed = pd.DataFrame(columns, columns=list(frame.columns))

        with patch("code_interpreter_runner.boto3") as mock_boto:
            mock_boto.client.return_value = MagicMock()
            live_runner = CodeInterpreterTestRunner(region="us-west-2")

        code = live_runner._build_validation_code(_target_schema(frame))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exec(code, {"df_transformed": df_transformed})
        return live_runner._parse_results(buffer.getvalue())

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_recorded_observation_matches_execution(self, frame):
        parsed = self._observed_via_production_path(frame)
        dq = parsed["data_quality_results"]
        observed = frame.observed_pandas

        assert parsed["output_records"] == len(frame.rows)
        assert dq["null_counts"] == observed["null_counts"]
        assert dq["duplicates"] == observed["duplicates"]
        assert dq["completeness_score"] == observed["completeness"]
        assert dq["uniqueness_score"] == observed["uniqueness"]
        assert dq["schema_compliance"] == observed["schema_compliance"]
        assert dq["overall_quality_score"] == observed["overall_quality"]
        assert dq["missing_columns"] == observed["missing_columns"]

    def test_execution_confirms_the_nan_divergence(self):
        """The one value divergence, measured rather than reasoned.

        ``amount`` holds one absent cell and one ``NaN``; pandas reports 2 nulls
        where the definition says 1.
        """
        parsed = self._observed_via_production_path(NULLS_INCLUDING_NAN)
        dq = parsed["data_quality_results"]
        assert dq["null_counts"]["amount"] == 2
        assert null_counts_from_definition(NULLS_INCLUDING_NAN)["amount"] == 1
        assert dq["completeness_score"] == 75.0
        assert completeness_from_definition(NULLS_INCLUDING_NAN) == 83.3
