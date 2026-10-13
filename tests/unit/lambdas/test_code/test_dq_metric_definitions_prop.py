# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 13 — data-quality metrics equal their definitions.

Spec ``real-spark-verification`` task 6.4. **Validates: Requirement 10.2.**

    THE Glue_Session_Runner SHALL compute ``completeness`` as the proportion of
    non-null cells, ``uniqueness`` as the proportion of rows that are not exact
    duplicates of an earlier row, and ``schema_compliance`` as the proportion of
    declared target columns present in the transform output.

``test_validation_statement.py`` establishes this at the example level on task
1.1's five hand-built frames. What follows generalises it over *generated* frames
carrying nulls, float ``NaN`` and exact duplicate rows — task 6.4's explicit
instruction — so that the emitted arithmetic is checked against the definitions
across the input space rather than at five points in it.

════════════════════════════════════════════════════════════════════════
THE ORACLE IS TASK 1.1'S, IMPORTED — NOT RESTATED
════════════════════════════════════════════════════════════════════════

Every expectation comes from ``test_dq_definitions_characterization``'s
``*_from_definition`` functions, imported. Restating the arithmetic here would
give the emitted statement a second oracle written by the same hand at the same
time, which is the circularity ``LGR-28b4d75-5`` (template / ratified) names — "a
test written from the producer's output cannot detect the producer drifting,
because the output is its own oracle." Requirement 10.5 is the direction:
expectations come from the definitions, **never** from what the pandas path
currently prints.

That is also why the oracles are used rather than re-derived per generated frame:
task 1.1's ``TestOracleAgreesWithHandComputedAnswers`` validates the oracle
against five sets of hand-computed literals, so the oracle has an independent
check that a private copy in this file would not have.

════════════════════════════════════════════════════════════════════════
WHAT THE STUB PROVES, AND WHAT IT DOES NOT
════════════════════════════════════════════════════════════════════════

No Spark and no pandas run in this repository, and neither may be installed
(design §3 do-NOT 7, ``LGR-4dd9b42-1`` — template / ratified). ``build_validation_code``
returns a **string of PySpark**, so what is executed here is that string against
the Spark stub ``test_validation_statement.py`` builds.

That stub encodes three pieces of *documented* Spark semantics the emitted
arithmetic depends on:

1. ``Column.isNull()`` is True for a null and **False for a float NaN** — design
   C7's one value divergence and the reason ``completeness`` moves.
2. ``count(DISTINCT expr)`` skips rows where *expr* is null, while a struct built
   by ``struct(...)`` is itself never null — which is why the distinct count is
   taken over a struct rather than over the bare column list.
3. NaN is normalised in a distinct key (``NormalizeFloatingNumbers``), so a row
   carrying NaN is a duplicate of an identical row.

**So the property establishes that the emitted arithmetic computes the R10.2
definitions GIVEN those semantics.** It does not measure real Spark. Design C7 is
itself explicit that the divergence it predicts is "derived from documented
semantics" rather than measured, and the runner's own docstring repeats it at the
``count_distinct`` substitution. A real-Spark check is task 11.4's e2e, and the
first thing that measures the pair actually in use is shadow mode (task 10.1).

**Why the stub is imported rather than copied.** The three rules above are the
load-bearing part; two encodings of them is two oracles for Spark's semantics,
and they can drift apart silently — the property could then pass while the
example baseline fails, or worse, both could pass against different beliefs. The
cross-test-module import is also the precedent this spec already set: task 6.1's
``test_validation_statement.py`` imports task 1.1's oracle by dotted package path
for exactly this reason. The cost is that importing re-runs that module's
top-level ``sys.path.insert``; it inserts the same Lambda source directory this
module inserts, so the effect is a duplicate ``sys.path`` entry and nothing else.
Measured, not assumed: running the three modules together collects 242 tests —
27 here, 134 in ``test_validation_statement.py``, 81 in
``test_dq_definitions_characterization.py`` — so nothing is collected twice.

════════════════════════════════════════════════════════════════════════
WHAT IS GENERATED, AND WHAT IS REACHED DELIBERATELY
════════════════════════════════════════════════════════════════════════

Generated: column counts from 1 up, per-column type families (so a column is
type-homogeneous, as a Spark column is), cells drawn from ``NULL``, float ``NaN``
and small value domains so exact duplicate rows arise, row counts from 0 up, and
declared target sets that are any subset of the columns plus any number of absent
names.

Six cases are reached **by construction** rather than left to luck, each named in
task 6.4's instructions:

* ``test_a_frame_whose_every_row_carries_a_null_is_still_counted`` — the case the
  ``struct`` exists for. Without it ``count(DISTINCT a, b)`` drops every row and
  scores ``uniqueness`` at 0.0 for distinct rows.
* ``test_rows_differing_only_by_a_null_are_distinct``
* ``test_a_nan_bearing_row_is_not_a_duplicate_of_a_null_bearing_one``
* ``test_zero_declared_targets_reproduces_the_recorded_vacuous_score``
* ``test_one_absent_declared_target_costs_exactly_its_share``
* ``test_widths_from_one_up`` — a width sweep, so no width is covered only by
  chance.

Two deliberate scope decisions in the generators:

1. **Braces in a column name are excluded.** ``_payload``'s brace-depth scan, and
   ``_parse_results``' identical one (``code_interpreter_runner.py:604-614``),
   count braces without tracking string literals, so an unbalanced ``}`` in a
   name truncates the slice handed to ``json.loads``. Task 6.1 pins that as a
   pre-existing, executor-independent parser limitation and design §3 do-NOT 8
   forbids repairing it; **task 6.2 owns generating braces**. Excluding them here
   keeps a payload-extraction failure from being reported as a metric defect.
2. **Every other awkward spelling is included on purpose** — dots, backticks,
   quotes, backslashes, spaces, non-ASCII. Those exercise the backtick quoting
   the null counts depend on: an unquoted ``a.b`` resolves as struct-field access
   on ``a`` rather than as the column named ``a.b``, which produces a *wrong null
   count*. That is a metric defect, so it is in scope here, and the mutation check
   below confirms this file catches it.

════════════════════════════════════════════════════════════════════════
FALSIFIABILITY, MEASURED
════════════════════════════════════════════════════════════════════════

A property that cannot fail proves nothing, so the emitted statement was mutated
— as a string, at run time, with production code on disk untouched — and this
file re-run against each mutant:

===========================================  ========
mutation                                     outcome
===========================================  ========
``_agg[_index + 2]`` → ``_agg[_index + 1]``  killed
null-count aggregates emitted in reverse     killed
``struct(...)`` → the first bare column      killed
column references left unquoted              killed
``completeness`` denominator rows not cells  killed
``schema_compliance`` denominator ``_cols``  killed
a no-op comment (the control)                survived
===========================================  ========

The control surviving is the half that matters: it shows the six kills are the
assertions discriminating, not the harness failing on any edit at all. One
mutation — reversing the ``struct``'s field order — also survived and *should*:
reversing every row's fields identically preserves row identity, so no metric
moves.

Reproduce by wrapping ``glue_session_runner.build_validation_code`` in a pytest
plugin that string-replaces in its return value.

``dtypes`` is asserted only for its key set. Task 1.1 leaves its values unpinned
and records why: C7 predicts every entry moves, and the pandas side is not one
vocabulary either — a string column reports ``object`` on pandas 2.2.3 and ``str``
on 3.0.5, with nothing pinning which pandas the managed interpreter runs.

Ledger notes
------------

* ``LGR-4dd9b42-1`` (**template / ratified**) is why neither runtime may be
  installed, so the statement is checked as emitted text executed against a stub.
* ``LGR-b2f79ef-12`` (**template / ratified**) governs the frame width ceiling
  (``_MAX_TEST_FRAME_COLUMNS = 512``), whose derivation is the DynamoDB item-size
  limit on the state row and is therefore executor-independent (R10.4). Frames
  here are far narrower on purpose: this file is about the arithmetic, and the
  ceiling is task 5.3/5.4's.
* ``LGR-b2f79ef-1`` (**template / ratified**) makes ``schema_compliance`` a check
  that can fail a run, which is why its definition is worth pinning over generated
  inputs rather than at four columns.
* ``LGR-3e45e65-1`` (**template / ratified**) pins ``AWS_REGION`` in
  ``tests/unit/conftest.py`` by assignment, so the ``setdefault`` calls below are
  consistent no-ops.
* ``why`` on ``glue_session_runner.py``, on ``test_dq_definitions_characterization.py``
  and on ``test_validation_statement.py`` all return **nothing** — the three files
  are not yet committed. Stated as a gap in the ledger, not as an all-clear.
  ``decisions(concept="testing")`` returns five records, none about metric
  definitions; ``decisions(scope=".../agentic_data_ingestion_test_code")`` returns
  the converter records plus ``LGR-b2f79ef-*``, cited above where they bear.

Production code is **not** modified by this task.

Validates: Requirements 10.2
"""

import math
import os
import sys

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, assume, given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from glue_session_runner import VALIDATION_PAYLOAD_KEYS

# Task 1.1's oracle and frame representation. Imported, never restated — see the
# module docstring for why the direction matters (`LGR-28b4d75-5`).
from tests.unit.lambdas.test_code.test_dq_definitions_characterization import (
    NAN,
    NULL,
    Frame,
    completeness_from_definition,
    duplicate_rows_from_definition,
    missing_target_columns_from_definition,
    null_counts_from_definition,
    overall_from_components,
    schema_compliance_from_definition,
    uniqueness_from_definition,
)

# Task 6.1's Spark stub and its execution harness. Imported for the same reason:
# the three documented Spark rules it encodes must have exactly one encoding.
from tests.unit.lambdas.test_code.test_validation_statement import (
    _DERIVE_FROM_FRAME,
    _validate,
)

# ══════════════════════════════════════════════════════════════════════
# Execution
# ══════════════════════════════════════════════════════════════════════


def _payload_for(frame: Frame, *, target_schema=_DERIVE_FROM_FRAME) -> dict:
    """Run the emitted validation statement against *frame*; return its payload.

    ``pytest.MonkeyPatch.context()`` rather than the ``monkeypatch`` fixture: a
    function-scoped fixture under ``@given`` is set up once for the whole property
    and torn down after every example has run, which Hypothesis flags as a health
    check. The context manager gives the same install-and-restore of the stub
    ``pyspark`` modules per example, using task 6.1's installer unchanged.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        payload = _validate(frame, monkeypatch, target_schema=target_schema)
    payload.pop("_stub", None)
    return payload


# ══════════════════════════════════════════════════════════════════════
# The reading pandas `isna()` implements — for the direction assertion only
# ══════════════════════════════════════════════════════════════════════


def _absent_or_nan_counts(frame: Frame) -> dict:
    """Per-column count of cells that are absent **or** a float ``NaN``.

    This is the *reading* pandas ``isna()`` implements, not executed pandas: it
    counts a NaN as missing where R10.2's "non-null" does not. Task 1.1 measured
    the two against real pandas 2.2.3 and 3.0.5 on its five frames; what is used
    here is only the set relation between the two readings — absent-or-NaN is a
    superset of absent — which needs no runtime to know.
    """
    return {
        name: sum(
            1 for row in frame.rows if row[index] is None or (isinstance(row[index], float) and math.isnan(row[index]))
        )
        for index, name in enumerate(frame.columns)
    }


def _pandas_reading_completeness(frame: Frame) -> float:
    missing = sum(_absent_or_nan_counts(frame).values())
    return round((1 - missing / max(frame.cell_count, 1)) * 100, 1)


def _carries_a_nan(frame: Frame) -> bool:
    return any(isinstance(cell, float) and math.isnan(cell) for row in frame.rows for cell in row)


# ══════════════════════════════════════════════════════════════════════
# Strategies
# ══════════════════════════════════════════════════════════════════════

#: Column names whose *spelling* is awkward in a way the arithmetic must survive.
#: A dot must not be read as struct-field access; a backtick must be doubled in
#: the quoted reference. Both are exercised because getting either wrong produces
#: a wrong null count, which is a metric defect and so in scope here. Braces are
#: excluded — see the module docstring, exclusion 1.
_AWKWARD_COLUMN_NAMES = (
    "id",
    "amount",
    "label",
    "a.b",
    "g`h",
    "x y",
    "A-1",
    "n\u00e4me",
    'q"r',
    "s\\t",
    "UPPER",
)

_COLUMN_NAMES = st.one_of(
    st.sampled_from(_AWKWARD_COLUMN_NAMES),
    st.text(alphabet="abcXYZ019_", min_size=1, max_size=4),
)

#: Names guaranteed absent from any generated column list: longer than the text
#: strategy's ceiling and absent from :data:`_AWKWARD_COLUMN_NAMES`.
_UNDECLARED_NAMES = ("email", "created_at", "not_declared_here")

#: One value domain per column, so a generated column is type-homogeneous the way
#: a Spark column is. Domains are small on purpose: exact duplicate rows have to
#: arise often, and task 6.4 asks for them explicitly. ``NaN`` lives only in the
#: float domain, which is the only place Spark could put it.
_PRESENT_VALUES_BY_FAMILY = {
    "int": st.integers(min_value=-3, max_value=3),
    "float": st.sampled_from([0.0, 1.5, -2.25, NAN]),
    "str": st.sampled_from(["a", "b", ""]),
    "bool": st.booleans(),
}
_TYPE_FAMILIES = tuple(_PRESENT_VALUES_BY_FAMILY)


def _cells(family: str):
    """A cell in a column of *family*: absent, or a present value of that family.

    ``NULL`` is one of two branches, so roughly half of all cells are absent.
    Deliberately far above the synthetic frame's ~5%: the null-count arithmetic and
    the ``struct`` in the distinct count are what this file is hardest on, and both
    only bite where nulls are dense.
    """
    return st.one_of(st.just(NULL), _PRESENT_VALUES_BY_FAMILY[family])


@st.composite
def _declared_targets(draw, names: list[str]) -> tuple[str, ...]:
    """Any subset of *names*, plus any number of absent names, in any order."""
    present = draw(st.lists(st.sampled_from(names), unique=True, max_size=len(names))) if names else []
    absent = draw(st.lists(st.sampled_from(_UNDECLARED_NAMES), unique=True, max_size=len(_UNDECLARED_NAMES)))
    return tuple(draw(st.permutations([*present, *absent])))


@st.composite
def _frames(
    draw,
    *,
    min_columns: int = 1,
    max_columns: int = 5,
    max_rows: int = 6,
    every_row_carries_a_null: bool = False,
) -> Frame:
    """A frame with nulls, ``NaN`` and exact duplicate rows (task 6.4's brief).

    Duplicates are produced by drawing a small pool of base rows and then drawing
    an index sequence over it, which yields exact repeats far more often than
    drawing rows independently would. An empty index sequence gives a zero-row
    frame, which is generated rather than filtered out: the emitted arithmetic
    guards every denominator with ``max(…, 1)`` and so does the oracle.
    """
    names = draw(st.lists(_COLUMN_NAMES, min_size=min_columns, max_size=max_columns, unique=True))
    families = draw(st.lists(st.sampled_from(_TYPE_FAMILIES), min_size=len(names), max_size=len(names)))
    base = draw(st.lists(st.tuples(*[_cells(family) for family in families]), min_size=1, max_size=3))

    if every_row_carries_a_null and names:
        positions = draw(
            st.lists(st.integers(min_value=0, max_value=len(names) - 1), min_size=len(base), max_size=len(base))
        )
        base = [
            tuple(NULL if index == positions[row_index] else cell for index, cell in enumerate(row))
            for row_index, row in enumerate(base)
        ]

    indices = draw(st.lists(st.integers(min_value=0, max_value=len(base) - 1), min_size=0, max_size=max_rows))
    return Frame(
        name="generated",
        columns=tuple(names),
        rows=tuple(base[index] for index in indices),
        declared_targets=draw(_declared_targets(names)),
    )


#: Executing a compiled statement per example is the cost here, so the budgets are
#: in line with the spec's other statement-executing properties
#: (``test_absent_schema_refusal_exactness_prop.py`` uses 200/60).
_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
_SWEEP_SETTINGS = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# ══════════════════════════════════════════════════════════════════════
# R10.2 — every metric equals its definition, over generated frames
# ══════════════════════════════════════════════════════════════════════


class TestEveryMetricEqualsItsDefinition:
    """Property 13. **Validates: Requirement 10.2.**"""

    @_SETTINGS
    @given(frame=_frames())
    def test_record_count_is_the_row_count(self, frame):
        assert _payload_for(frame)["record_count"] == len(frame.rows)

    @_SETTINGS
    @given(frame=_frames())
    def test_null_counts_equal_the_definition(self, frame):
        """One count per column, of genuinely absent cells only — a NaN is a value."""
        assert _payload_for(frame)["null_counts"] == null_counts_from_definition(frame)

    @_SETTINGS
    @given(frame=_frames())
    def test_completeness_is_the_proportion_of_non_null_cells(self, frame):
        assert _payload_for(frame)["completeness"] == completeness_from_definition(frame)

    @_SETTINGS
    @given(frame=_frames())
    def test_duplicates_are_rows_that_repeat_an_earlier_row(self, frame):
        assert _payload_for(frame)["duplicates"] == duplicate_rows_from_definition(frame)

    @_SETTINGS
    @given(frame=_frames())
    def test_uniqueness_is_the_proportion_of_non_duplicate_rows(self, frame):
        assert _payload_for(frame)["uniqueness"] == uniqueness_from_definition(frame)

    @_SETTINGS
    @given(frame=_frames())
    def test_missing_columns_are_the_declared_targets_absent_from_the_output(self, frame):
        assert _payload_for(frame)["missing_columns"] == missing_target_columns_from_definition(frame)

    @_SETTINGS
    @given(frame=_frames())
    def test_schema_compliance_is_the_proportion_of_declared_targets_present(self, frame):
        """Only where the definition decides it — see the zero-declared case below.

        The emitted expression is ``1 - missing/declared`` where the oracle is
        ``present/declared``. Algebraically equal; asserted rather than assumed
        because the two forms can differ in the last bit before rounding.
        """
        assume(frame.declared_targets)
        assert _payload_for(frame)["schema_compliance"] == schema_compliance_from_definition(frame)

    @_SETTINGS
    @given(frame=_frames())
    def test_overall_quality_is_the_mean_of_the_rounded_components(self, frame):
        payload = _payload_for(frame)
        assert payload["overall_quality"] == overall_from_components(
            payload["completeness"], payload["uniqueness"], payload["schema_compliance"]
        )

    @_SETTINGS
    @given(frame=_frames())
    def test_the_payload_carries_the_nine_keys_and_a_dtype_per_column(self, frame):
        """The key set is task 6.3's property; what is pinned here is that every
        metric asserted above was read from a payload of the declared shape, and
        that ``dtypes`` covers every column without its values being pinned.
        """
        payload = _payload_for(frame)
        assert tuple(payload) == VALIDATION_PAYLOAD_KEYS
        assert set(payload["dtypes"]) == set(frame.columns)
        assert all(isinstance(value, str) for value in payload["dtypes"].values())


# ══════════════════════════════════════════════════════════════════════
# The cases reached by construction rather than by luck
# ══════════════════════════════════════════════════════════════════════


class TestTheCasesReachedDeliberately:
    @_SETTINGS
    @given(frame=_frames(every_row_carries_a_null=True))
    def test_a_frame_whose_every_row_carries_a_null_is_still_counted(self, frame):
        """Why the ``struct`` in the distinct count is load-bearing.

        SQL ``count(DISTINCT a, b)`` drops any row with a null argument, so over a
        frame where every row carries one it would see **zero** rows, report
        ``duplicates`` as ``record_count`` and score ``uniqueness`` at 0.0 for
        distinct rows. A struct built by ``struct(...)`` is never null, so every
        row is counted. Generated so this holds for every such frame, not only for
        task 6.1's one hand-built example.
        """
        assume(frame.rows)
        assert all(any(cell is None for cell in row) for row in frame.rows)
        payload = _payload_for(frame)
        assert payload["duplicates"] == duplicate_rows_from_definition(frame)
        assert payload["uniqueness"] == uniqueness_from_definition(frame)

    @_SETTINGS
    @given(
        names=st.lists(_COLUMN_NAMES, min_size=1, max_size=4, unique=True),
        value=st.sampled_from([0, 1, "a", True, 1.5]),
    )
    def test_rows_differing_only_by_a_null_are_distinct(self, names, value):
        """An absent cell is not equal to a present one, so the rows are distinct."""
        position = len(names) - 1
        present = tuple(value if index == position else "k" for index in range(len(names)))
        absent = tuple(NULL if index == position else "k" for index in range(len(names)))
        frame = Frame(
            name="differs_only_by_a_null",
            columns=tuple(names),
            rows=(present, absent),
            declared_targets=tuple(names),
        )
        payload = _payload_for(frame)
        assert duplicate_rows_from_definition(frame) == 0
        assert payload["duplicates"] == 0
        assert payload["uniqueness"] == 100.0

    @_SETTINGS
    @given(names=st.lists(_COLUMN_NAMES, min_size=1, max_size=4, unique=True))
    def test_a_nan_bearing_row_is_not_a_duplicate_of_a_null_bearing_one(self, names):
        """``NaN`` and absent are different values, so the rows are distinct — and
        only the absent one is counted as a null (R10.2, design C7).
        """
        position = len(names) - 1
        nan_row = tuple(NAN if index == position else "k" for index in range(len(names)))
        null_row = tuple(NULL if index == position else "k" for index in range(len(names)))
        frame = Frame(
            name="nan_against_null",
            columns=tuple(names),
            rows=(nan_row, null_row),
            declared_targets=tuple(names),
        )
        payload = _payload_for(frame)
        assert payload["duplicates"] == duplicate_rows_from_definition(frame) == 0
        assert payload["null_counts"] == null_counts_from_definition(frame)
        assert payload["null_counts"][names[position]] == 1

    @_SETTINGS
    @given(frame=_frames(), schema=st.sampled_from([None, {}, {"properties": {}}, {"fields": {}}]))
    def test_zero_declared_targets_reproduces_the_recorded_vacuous_score(self, frame, schema):
        """The definition does not decide a proportion over an empty set.

        Task 1.1's oracle **raises** for zero declared targets, and records the
        current path's short-circuit to ``100.0`` as an *observation* only. The
        emitted statement reproduces that short-circuit knowingly
        (``if expected else 100.0``), so what is asserted is the reproduction plus
        the oracle's refusal — never that ``100.0`` is the definition's answer.
        """
        empty = Frame(
            name=frame.name,
            columns=frame.columns,
            rows=frame.rows,
            declared_targets=(),
        )
        with pytest.raises(ValueError, match="does not decide"):
            schema_compliance_from_definition(empty)

        payload = _payload_for(frame, target_schema=schema)
        assert payload["schema_compliance"] == 100.0
        assert payload["missing_columns"] == []

    @_SETTINGS
    @given(
        names=st.lists(_COLUMN_NAMES, min_size=1, max_size=6, unique=True),
        absent=st.sampled_from(_UNDECLARED_NAMES),
    )
    def test_one_absent_declared_target_costs_exactly_its_share(self, names, absent):
        """``n`` of ``n+1`` declared columns present scores ``n/(n+1)``.

        The share is computed from the definition's sentence rather than from the
        emitted expression, so a change to the expression that happened to keep
        the emitted and oracle forms agreeing would still be caught here.
        """
        frame = Frame(
            name="one_declared_target_absent",
            columns=tuple(names),
            rows=(tuple("v" for _ in names),),
            declared_targets=(*names, absent),
        )
        payload = _payload_for(frame)
        declared = len(names) + 1
        assert payload["missing_columns"] == [absent]
        assert payload["schema_compliance"] == schema_compliance_from_definition(frame)
        assert payload["schema_compliance"] == round(len(names) / declared * 100, 1)

    @pytest.mark.parametrize("width", [1, 2, 3, 4, 5, 6, 7, 8])
    @_SWEEP_SETTINGS
    @given(data=st.data())
    def test_widths_from_one_up(self, width, data):
        """Task 6.4 asks for column counts from 1 up; swept rather than sampled.

        Every metric is checked at each width, because ``completeness``' denominator
        is ``rows × columns`` and an off-by-one in the projection offset would show
        at one width and not another.
        """
        frame = data.draw(_frames(min_columns=width, max_columns=width))
        assert len(frame.columns) == width
        payload = _payload_for(frame)
        assert payload["record_count"] == len(frame.rows)
        assert payload["null_counts"] == null_counts_from_definition(frame)
        assert payload["completeness"] == completeness_from_definition(frame)
        assert payload["duplicates"] == duplicate_rows_from_definition(frame)
        assert payload["uniqueness"] == uniqueness_from_definition(frame)
        assert payload["missing_columns"] == missing_target_columns_from_definition(frame)

    @_SWEEP_SETTINGS
    @given(frame=_frames(min_columns=0, max_columns=0))
    def test_a_frame_with_no_columns_takes_the_lit_arm_and_still_agrees(self, frame):
        """Beyond task 6.4's "1 up", and cheap: the emitted code has a branch for it.

        ``_row_identity`` falls back to ``lit(1)`` when there is no column to build
        a struct from. That is a branch in the emitted statement, so exercising it
        is a claim about the statement rather than about Spark's willingness to
        hold a zero-column frame.
        """
        assert frame.columns == ()
        payload = _payload_for(frame)
        assert payload["null_counts"] == {} == null_counts_from_definition(frame)
        assert payload["completeness"] == completeness_from_definition(frame) == 100.0
        assert payload["duplicates"] == duplicate_rows_from_definition(frame)
        assert payload["uniqueness"] == uniqueness_from_definition(frame)


# ══════════════════════════════════════════════════════════════════════
# The re-baseline's direction, generalised (R10.5, design C7)
# ══════════════════════════════════════════════════════════════════════


class TestTheReBaselineDirection:
    """C7's one value divergence, as a property over generated frames.

    Task 1.1 measured the two readings against real pandas 2.2.3 and 3.0.5 on five
    frames. What is generalised here is only the **set relation** between them —
    "absent or NaN" is a superset of "absent" — which needs no runtime to know, and
    which fixes the direction: the pandas reading counts at least as many nulls, so
    it scores completeness at most as high.
    """

    @_SETTINGS
    @given(frame=_frames())
    def test_completeness_never_falls_below_the_pandas_reading(self, frame):
        assert _payload_for(frame)["completeness"] >= _pandas_reading_completeness(frame)

    @_SETTINGS
    @given(frame=_frames())
    def test_the_two_readings_differ_exactly_where_a_nan_appears(self, frame):
        """Strictly higher when a NaN is present, equal otherwise.

        With one exception the assertion names rather than hides: a frame with no
        cells at all has nowhere for a NaN to be, and both readings answer 100.0.
        """
        payload = _payload_for(frame)
        strictly_higher = payload["completeness"] > _pandas_reading_completeness(frame)
        assert strictly_higher == (_carries_a_nan(frame) and frame.cell_count > 0)

    @_SETTINGS
    @given(frame=_frames())
    def test_the_null_counts_are_at_most_the_pandas_reading_column_by_column(self, frame):
        counts = _payload_for(frame)["null_counts"]
        pandas_reading = _absent_or_nan_counts(frame)
        assert all(counts[name] <= pandas_reading[name] for name in frame.columns)

    @_SETTINGS
    @given(frame=_frames())
    def test_duplicates_and_schema_compliance_do_not_move_with_nan(self, frame):
        """C7 records that the duplicate expressions agree on exact duplicates and
        that ``schema_compliance`` is set arithmetic over column names on both
        sides (design §2). Both are therefore functions of the frame's *rows* and
        *columns* alone, and re-running the same frame with every NaN replaced by a
        distinct present float must not move either.
        """
        substituted = Frame(
            name=frame.name,
            columns=frame.columns,
            rows=tuple(
                tuple(9.75 if isinstance(cell, float) and math.isnan(cell) else cell for cell in row)
                for row in frame.rows
            ),
            declared_targets=frame.declared_targets,
        )
        original = _payload_for(frame)
        without_nan = _payload_for(substituted)
        for key in ("duplicates", "uniqueness", "schema_compliance", "missing_columns", "record_count"):
            assert original[key] == without_nan[key], key
