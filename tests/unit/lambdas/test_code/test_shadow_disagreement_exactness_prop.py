# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 16 — shadow records exactly the disagreements.

Spec ``real-spark-verification`` task 10.2. Requirements 4.2 and 4.5, design
§4.7.

    *For any* pair of executor results, the recorded comparison contains an entry
    for every compared dimension whose values differ and no entry for any
    dimension whose values are equal, and reports a non-zero omitted count
    exactly when the recorded entries were bounded.

**Validates: Requirements 4.2, 4.5**

"Exactly" has two directions, and only one of them is easy
---------------------------------------------------------
Every genuine disagreement is recorded (up to the cap), **and** no agreement is
recorded. A comparison that recorded everything satisfies the first half and is
useless; one that recorded nothing satisfies the second half and is worse. Both
directions are asserted against the same generated pair, so neither can be met
by leaning on the other.

Where the expected answer comes from — the oracle is the generator, not a rewrite
-------------------------------------------------------------------------------
The obvious way to check "exactly the disagreements" is to re-implement the
comparison in the test and diff the two. That proves the two copies agree and
nothing else. Instead every generated pair is built **from a plan that already
knows the answer**: :class:`ValuePair` carries its own ``disagrees`` label,
written down next to the two values by hand, and the expected set of recorded
dimensions is assembled from those labels. So the test's oracle is a table a
reader can audit line by line, not a second traversal that would share a bug
with the first.

Two consequences of that choice, both deliberate:

* The labels encode the module's contract — exact-type, structural agreement —
  as *decisions about specific pairs*: ``0`` against ``False`` disagrees,
  ``100`` against ``100.0`` disagrees, two NaNs agree. If any label is wrong the
  test fails and the argument for it is right there to re-read. That is the
  point.
* The labels are branch-independent, which is what makes them a valid oracle for
  the per-column dimensions too. ``({"a": 1}, {"a": True})`` is labelled a
  disagreement, and it is one whether ``_per_column_comparison`` expands it (both
  sides are mappings, so the entry is ``null_counts[a]``) or
  ``_scalar_comparison`` handles it (one side is not, so the entry is
  ``null_counts``). The family-level assertions therefore hold across a dispatch
  the labels never mention.

:data:`COMPARED_DIMENSIONS` is iterated, not restated
-----------------------------------------------------
The module exports the closed list as data so that this file can walk it, and
:func:`test_the_generator_covers_every_compared_dimension` fails if a dimension
is added to it without being taught to the generator here — so a ninth dimension
cannot be silently unquantified. The three group tuples are imported for the same
reason: which dimensions sit at the top level and which nest inside
``data_quality_results`` is the module's fact, and reading it means a dimension
moving between groups does not need an edit here.

What is generated, and what is deliberately held fixed
------------------------------------------------------
Every result pair is checked against
``tests/fixtures/run_test_result_shapes.py`` as it is built, so no property here
passes on a dict that ``run_test`` could not return. The near-misses the task
asks for live **inside** ``data_quality_results``, whose interior the contract
does not type (only the nine top-level keys are typed), so ``0`` against
``False`` and ``None`` against ``""`` are reachable without leaving the contract.
The two top-level dimensions keep their contract types — ``execution_successful``
is always ``bool`` on both sides and ``output_records`` always ``int`` — because
a result that violated those is not a result either executor can produce.

The ``conversion_report`` narrowing is excluded from the Glue side's contract
check for the reason ``test_shadow_comparison.py:83`` and
``test_result_shape_equivalence_prop.py:259`` both record: the Glue path returns
``None`` for that key on every outcome (task 8.1), so a builder that could not
express a Glue-shaped success could not test the asymmetry this comparison must
ignore.

``dtypes`` is generated even though it cannot reach a result dict today
----------------------------------------------------------------------
``_parse_results`` drops the key on both executor paths — ``shadow_comparison``'s
docstring records this at length and ``test_shadow_comparison.py``'s
``test_dtypes_does_not_reach_a_result_dict_today`` pins it. This file generates
it anyway, because ``compare_results`` is a pure function over two dicts and its
declared behaviour for the dimension is what task 10.2 must quantify: the day the
parser carries the key, C7's every-column disagreement starts arriving and the
cap has to already work. Generating it is not a claim that it is observable
end-to-end today.

Ledger and structure notes
--------------------------
``why`` on ``shadow_comparison.py`` and on ``test_shadow_comparison.py`` both
return **no records** — task 10.1 wrote them and they are still untracked, so the
ledger has nothing to say about either yet. That is a gap, not an all-clear.
``rootmap`` reports 122 records, **all** template provenance and none backfilled,
so ``include_backfilled=True`` changes nothing in this ledger (the steering note
describing backfilled records as the majority is stale).

``decisions(concept="testing", include_backfilled=True)`` gives the two
conventions this file follows: ``LGR-3e45e65-1`` — the unit layer pins
``AWS_REGION``/``AWS_DEFAULT_REGION`` in ``tests/unit/conftest.py``, so no
environment dance is needed here (and this module reads no environment at all) —
and ``LGR-b46a5bb-4``, a property test with **no allowlist**, "because an
allowlist entry is a door". No dimension is exempted here for being awkward;
``dtypes`` is generated rather than skipped for exactly that reason.
``decisions(concept="database", include_backfilled=True)`` returns
``LGR-28b4d75-3``, which is the precedent ``compare_results``' docstring cites for
``dimensions_compared``, and it says what is claimed of it: an early return "made
'found nothing' indistinguishable in the database from an agent that had never
run". That is why ``dimensions_compared`` is asserted here rather than treated as
decoration. ``LGR-4dd9b42-1``'s revisit condition — "PySpark becomes available to
the test environment, at which point the refused shapes can be compared against
real Spark and converted on evidence instead of refused on caution" — is the
reason ``proxy_refusal_glue_success`` gets its own dedicated assertions: it is the
count that condition asks for.

None of the four standing invariants reaches this file. ``LGR-b46a5bb-1``
(``threading.Thread`` forbidden in ``lambda/**``) is the nearest, and neither the
module under test nor this file starts a thread.

Codegraph, on the symbols exercised, with one number corrected by hand
----------------------------------------------------------------------
``compare_results`` (``shadow_comparison.py:445``) — codegraph reports **40
callers in ``lambda_function.py``**, and that figure is wrong: grep finds exactly
one call site (``lambda_function.py:923``, inside task 10.3's orchestration) plus
the local import at ``:855``. The reported count appears to attribute the whole
enclosing file rather than the calling function, so **the real blast radius of
this function is one production call site and two test files** — this one and
``test_shadow_comparison.py``. Recorded rather than repeated, because a spec that
plans against 40 callers would be planning against nothing.

``values_agree`` (``:282``) — 9 callers, all inside the module, covered by
``test_shadow_comparison.py``. ``_scalar_comparison`` (``:371``, 2 callers) and
``_per_column_comparison`` (``:382``, 1 caller) are both flagged **"no covering
tests"**; that is a false negative of the kind the spec's Notes record for
``lambda/**`` (``sys.path`` manipulation plus bare-name imports, which the symbol
graph does not follow) — both are reached through ``compare_results`` by the
existing suite, and this file quantifies over both branches. Nothing in
``shared/`` is in the blast radius: ``shadow_comparison`` imports ``math``,
``collections.abc`` and ``typing`` and nothing else, so the Lambda layer is not
involved.

Verified to have teeth, not just to be green
--------------------------------------------
A passing property test proves nothing on its own — it can pass because the code
is right or because the property is weak. Seven wrong implementations were
injected in memory (the module file was never edited) and each is caught by this
file: comparing with ``==`` only so ``0`` and ``False`` agree; recording every
dimension whether or not it differs; visiting per-column entries in dict order
instead of sorted order; truncating to the cap but reporting ``omitted`` as zero;
reading ``proxy_refusal_glue_success`` as a value comparison; dropping
``missing_columns`` from the compared list; and keeping the cap's *suffix* rather
than its priority prefix. The unmutated module passes all 17 tests.
"""

from __future__ import annotations

import copy
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Final

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings
from tests.fixtures.run_test_result_shapes import (
    CONVERSION_REFUSAL,
    STATEMENT_FAILURE,
    SUCCESS,
    OutcomeShape,
    shape_violations,
)

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from shadow_comparison import (
    _DQ_PER_COLUMN_DIMENSIONS,
    _DQ_SCALAR_DIMENSIONS,
    _TOP_LEVEL_DIMENSIONS,
    COMPARED_DIMENSIONS,
    DIMENSION_EXECUTION_SUCCESSFUL,
    DIMENSION_OUTPUT_RECORDS,
    DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS,
    MAX_RECORDED_DISAGREEMENTS,
    compare_results,
)

#: A cap no generated pair can reach, used to obtain the complete record that the
#: capped calls are checked against. Asserted to have omitted nothing wherever it
#: is used, so "complete" is verified rather than assumed.
_UNCAPPED: Final = 100_000

#: "This side does not carry the key." A local sentinel rather than
#: ``shadow_comparison.ABSENT``: absence is expressed by a **missing key** in the
#: dict the module is given, and passing the module's own internal sentinel in as
#: a value would be testing a shape no executor can produce.
#:
#: Identity is checked before any ``deepcopy``, because ``deepcopy(object())``
#: returns a *new* object and would silently stop being this sentinel.
_DROP: Final = object()

_NAN: Final = float("nan")


# ---------------------------------------------------------------------------
# The labelled value pairs — this file's oracle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValuePair:
    """Two sides' values for one dimension, and whether that is a disagreement.

    ``disagrees`` is written down by hand next to the values. It is the whole
    oracle: see the module docstring on why a second traversal would not be one.
    """

    label: str
    pandas: Any
    glue: Any
    disagrees: bool


#: Values for the scalar dimensions inside ``data_quality_results``. The contract
#: types that key as ``dict`` and says nothing about its interior
#: (``run_test_result_shapes.py:_BASE``), so the near-misses the task names are
#: reachable here without producing a result no executor could return.
_SCALAR_PAIRS: Final[tuple[ValuePair, ...]] = (
    ValuePair("two equal zeroes", 0, 0, False),
    ValuePair("two equal counts", 7, 7, False),
    ValuePair("two different counts", 0, 3, True),
    # The near-miss the task names first: `bool` is a subclass of `int` and
    # `0 == False`, so an isinstance-based comparison lets a count become a flag.
    ValuePair("zero against False", 0, False, True),
    ValuePair("False against zero", False, 0, True),
    ValuePair("one against True", 1, True, True),
    ValuePair("two equal Falses", False, False, False),
    # The second near-miss the task names.
    ValuePair("None against the empty string", None, "", True),
    ValuePair("the empty string against None", "", None, True),
    ValuePair("None on both sides", None, None, False),
    ValuePair("an int against an equal float", 100, 100.0, True),
    ValuePair("two equal floats", 100.0, 100.0, False),
    ValuePair("two different floats", 100.0, 75.0, True),
    # `nan != nan` in IEEE 754, but two executors both reporting NaN are not
    # disagreeing about anything.
    ValuePair("two NaNs", _NAN, _NAN, False),
    ValuePair("a NaN against a number", _NAN, 0.0, True),
    ValuePair("two equal lists", ["a", "b"], ["a", "b"], False),
    # `missing_columns` is order-sensitive: both paths iterate the declared
    # target columns in declaration order, so a reordering is a real difference
    # in the emitted payload.
    ValuePair("a reordered list", ["a", "b"], ["b", "a"], True),
    ValuePair("a shorter list", ["a"], ["a", "b"], True),
    ValuePair("two empty lists", [], [], False),
    ValuePair("a value against an absent key", 0, _DROP, True),
    ValuePair("an absent key against a value", _DROP, 0, True),
    ValuePair("an empty list against an absent key", [], _DROP, True),
    ValuePair("absent on both sides", _DROP, _DROP, False),
)

#: Values for the per-column map dimensions. Includes the third near-miss the
#: task names — an empty mapping against an absent key — and the two shapes that
#: decide whether the comparison expands per column or reports the container:
#: two mappings, or a mapping against something that is not one.
_MAP_PAIRS: Final[tuple[ValuePair, ...]] = (
    ValuePair("two equal maps", {"id": 0, "name": 5}, {"id": 0, "name": 5}, False),
    ValuePair("one column differing", {"id": 0, "name": 5}, {"id": 0, "name": 4}, True),
    ValuePair("a column on one side only", {"id": 0}, {"id": 0, "age": 1}, True),
    ValuePair("a column value int against float", {"id": 0}, {"id": 0.0}, True),
    ValuePair("a column value int against bool", {"id": 0}, {"id": False}, True),
    ValuePair("a column value None against empty string", {"id": None}, {"id": ""}, True),
    ValuePair("two NaN column values", {"id": _NAN}, {"id": _NAN}, False),
    # The third near-miss the task names.
    ValuePair("an empty map against an absent key", {}, _DROP, True),
    ValuePair("two empty maps", {}, {}, False),
    ValuePair("absent on both sides", _DROP, _DROP, False),
    ValuePair("a map against an absent key", {"id": 0}, _DROP, True),
    # Both sides ARE mappings here, so this expands per column and the entry is
    # `null_counts[id]` with the Glue side absent — not a container entry.
    ValuePair("a map against an empty map", {"id": 0}, {}, True),
    ValuePair("two equal nested values", {"id": {"a": [1, None]}}, {"id": {"a": [1, None]}}, False),
    ValuePair("a nested near-miss", {"id": {"a": [1, None]}}, {"id": {"a": [1, False]}}, True),
    # Not both mappings, so the disagreement is about the container.
    ValuePair("a map against a string", {"id": 0}, "unavailable", True),
    ValuePair("two equal strings", "n/a", "n/a", False),
)

#: ``output_records`` is typed ``int`` by the contract on every outcome, so its
#: pairs stay within that type. The cross-type near-misses live in the
#: data-quality interior, which the contract leaves untyped.
_OUTPUT_RECORD_PAIRS: Final[tuple[ValuePair, ...]] = (
    ValuePair("two equal row counts", 100, 100, False),
    ValuePair("two different row counts", 100, 98, True),
    ValuePair("a row count against zero", 100, 0, True),
    ValuePair("two zero row counts", 0, 0, False),
)

#: Which pair table feeds which dimension. ``execution_successful`` and
#: ``proxy_refusal_glue_success`` are absent because they are driven by the two
#: generated flags instead — the second is a *condition* over both results, and
#: the first is coupled to it (a proxy refusal never executed).
#: :func:`test_the_generator_covers_every_compared_dimension` is what keeps this
#: mapping honest against :data:`COMPARED_DIMENSIONS`.
_PAIRS_BY_DIMENSION: Final[dict[str, tuple[ValuePair, ...]]] = {
    DIMENSION_OUTPUT_RECORDS: _OUTPUT_RECORD_PAIRS,
    **{dimension: _SCALAR_PAIRS for dimension in _DQ_SCALAR_DIMENSIONS},
    **{dimension: _MAP_PAIRS for dimension in _DQ_PER_COLUMN_DIMENSIONS},
}

#: The dimensions this file's generator drives from flags rather than from a pair.
_FLAG_DRIVEN_DIMENSIONS: Final[tuple[str, ...]] = (
    DIMENSION_EXECUTION_SUCCESSFUL,
    DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS,
)


# ---------------------------------------------------------------------------
# Building contract-shaped results
# ---------------------------------------------------------------------------


def _place(payload: dict[str, Any], key: str, value: Any) -> None:
    """Put *value* under *key*, or leave the key out entirely for :data:`_DROP`.

    Identity against the sentinel is tested **before** copying, because
    ``deepcopy`` of a bare ``object()`` produces a different object.
    """
    if value is _DROP:
        return
    payload[key] = copy.deepcopy(value)


def _violations(result: dict[str, Any], shape: OutcomeShape) -> list[str]:
    """Shape violations, excluding every one that mentions ``conversion_report``.

    The same substring exclusion, for the same recorded reason, as
    ``test_shadow_comparison.py:83`` and ``test_result_shape_equivalence_prop.py:259``:
    the Glue path returns ``None`` for that key on every outcome (task 8.1).
    """
    return [violation for violation in shape_violations(result, shape) if "conversion_report" not in violation]


def _pandas_result(
    *,
    proxy_refused: bool,
    execution_successful: bool,
    top_level: dict[str, Any],
    data_quality: dict[str, Any],
) -> dict[str, Any]:
    """A contract-shaped Pandas_Proxy result.

    Three of the six declared outcomes are reachable from here — the proxy
    refusal (the one shape a second executor must never produce, R3.2), a
    success, and a statement failure — and each is checked against its own shape
    as it is built.
    """
    if proxy_refused:
        result: dict[str, Any] = {
            "execution_successful": False,
            "executed": False,
            "exit_code": None,
            "stderr": "",
            "input_records": 0,
            "output_records": 0,
            "data_quality_results": data_quality,
            "error_details": "unsupported construct",
            "conversion_report": None,
            "not_verified_by_proxy": True,
            "not_verified_reason": "not_yet_expressible",
            "not_verified_construct": "initcap",
            "not_verified_line_number": 3,
            "not_verified_source_line": "df.withColumn('n', initcap(col('n')))",
        }
        result.update(top_level)
        result["execution_successful"] = False
        assert shape_violations(result, CONVERSION_REFUSAL) == [], result
        return result

    shape = SUCCESS if execution_successful else STATEMENT_FAILURE
    result = {
        "execution_successful": execution_successful,
        "executed": True,
        "exit_code": 0 if execution_successful else 1,
        # Varied against the Glue side on purpose: `stderr` is deliberately not
        # compared, and a generator that made the two sides equal could not
        # notice it starting to be.
        "stderr": "pandas stderr",
        "input_records": 100,
        "output_records": 100,
        "data_quality_results": data_quality,
        "error_details": None if execution_successful else "AttributeError: 'Series' object has no attribute 'zip'",
        "conversion_report": {"constructs_converted": ["col", "when"]},
    }
    result.update(top_level)
    result["execution_successful"] = execution_successful
    assert shape_violations(result, shape) == [], result
    return result


def _glue_result(
    *,
    execution_successful: bool,
    top_level: dict[str, Any],
    data_quality: dict[str, Any],
) -> dict[str, Any]:
    """A contract-shaped Glue_Session_Runner result.

    ``conversion_report`` is ``None`` on every Glue outcome and no
    ``not_verified_by_proxy`` marker is ever present (R3.2) — the two asymmetries
    the comparison must ignore, present here so that it can be observed ignoring
    them.
    """
    shape = SUCCESS if execution_successful else STATEMENT_FAILURE
    result: dict[str, Any] = {
        "execution_successful": execution_successful,
        "executed": True,
        "exit_code": 0 if execution_successful else 1,
        "stderr": "spark stderr",
        "input_records": 50,
        "output_records": 100,
        "data_quality_results": data_quality,
        "error_details": None if execution_successful else "AnalysisException: [UNRESOLVED_COLUMN] `zip`",
        "conversion_report": None,
    }
    result.update(top_level)
    result["execution_successful"] = execution_successful
    assert _violations(result, shape) == [], result
    return result


@dataclass(frozen=True)
class ResultPair:
    """A generated pair of executor results and the answer it was built to have."""

    pandas_result: dict[str, Any]
    glue_result: dict[str, Any]
    #: Every dimension whose two values were made to differ. The expected set of
    #: recorded dimension *families*, assembled from the pairs' own labels.
    expected: frozenset[str]
    #: Dimension → the pair that fed it, for readable counterexamples and for the
    #: entry-payload assertions.
    pairs: dict[str, ValuePair] = field(default_factory=dict)
    proxy_refused: bool = False
    glue_succeeded: bool = False


@st.composite
def result_pairs(draw: st.DrawFn) -> ResultPair:
    """Generate two executor results together with the disagreements they carry.

    Walks :data:`COMPARED_DIMENSIONS` so that the generated pair covers whatever
    the module declares, and places each dimension's values at the level the
    module says it lives at — top-level or inside ``data_quality_results`` — by
    reading the exported group tuples rather than by knowing.
    """
    proxy_refused = draw(st.booleans())
    glue_succeeded = draw(st.booleans())
    # A proxy refusal never executed, so its `execution_successful` is False.
    # This is a coupling in the *inputs*, not in the comparison.
    pandas_exec = False if proxy_refused else draw(st.booleans())

    pairs: dict[str, ValuePair] = {}
    pandas_top: dict[str, Any] = {}
    glue_top: dict[str, Any] = {}
    # Four data-quality keys `_parse_results` carries that no dimension names.
    # Differing on both sides, so "not compared" is observable rather than
    # assumed.
    pandas_dq: dict[str, Any] = {"completeness_score": 95.0, "uniqueness_score": 100.0, "overall_quality_score": 98.3}
    glue_dq: dict[str, Any] = {"completeness_score": 91.5, "uniqueness_score": 99.0, "overall_quality_score": 95.2}

    for dimension in COMPARED_DIMENSIONS:
        if dimension in _FLAG_DRIVEN_DIMENSIONS:
            continue
        pair = draw(st.sampled_from(_PAIRS_BY_DIMENSION[dimension]))
        pairs[dimension] = pair
        left, right = (pandas_top, glue_top) if dimension in _TOP_LEVEL_DIMENSIONS else (pandas_dq, glue_dq)
        _place(left, dimension, pair.pandas)
        _place(right, dimension, pair.glue)

    expected = {dimension for dimension, pair in pairs.items() if pair.disagrees}
    if pandas_exec != glue_succeeded:
        expected.add(DIMENSION_EXECUTION_SUCCESSFUL)
    if proxy_refused and glue_succeeded:
        expected.add(DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS)

    return ResultPair(
        pandas_result=_pandas_result(
            proxy_refused=proxy_refused,
            execution_successful=pandas_exec,
            top_level=pandas_top,
            data_quality=pandas_dq,
        ),
        glue_result=_glue_result(
            execution_successful=glue_succeeded,
            top_level=glue_top,
            data_quality=glue_dq,
        ),
        expected=frozenset(expected),
        pairs=pairs,
        proxy_refused=proxy_refused,
        glue_succeeded=glue_succeeded,
    )


# ---------------------------------------------------------------------------
# Reading a comparison back
# ---------------------------------------------------------------------------


def _names(comparison: dict[str, Any]) -> list[str]:
    return [entry["dimension"] for entry in comparison["disagreements"]]


def _family(name: str) -> str:
    """The dimension a recorded entry belongs to, ``null_counts[age]`` → ``null_counts``.

    Unambiguous because no member of :data:`COMPARED_DIMENSIONS` is a prefix of
    another, which :func:`test_the_generator_covers_every_compared_dimension`
    also checks.
    """
    return name.split("[", 1)[0]


def _families(comparison: dict[str, Any]) -> set[str]:
    return {_family(name) for name in _names(comparison)}


def _entry_for(comparison: dict[str, Any], dimension: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = [entry for entry in comparison["disagreements"] if entry["dimension"] == dimension]
    assert len(matches) == 1, f"expected exactly one {dimension!r} entry, got {matches}"
    return matches[0]


def _same(left: Any, right: Any) -> bool:
    """Equality for the entry-payload assertions, NaN-tolerant.

    Three lines of local equality rather than borrowing ``values_agree``: the
    claim being checked is that an entry carries *the value it was given*, and
    checking it with the module's own comparator would let a comparator bug hide
    a plumbing bug.
    """
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    return bool(left == right)


# ---------------------------------------------------------------------------
# Property 16 — exactness, both directions
# ---------------------------------------------------------------------------


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pair=result_pairs())
def test_every_disagreement_is_recorded_and_no_agreement_is(pair: ResultPair) -> None:
    """**Validates: Requirements 4.2**

    The property in one assertion, in both directions at once: the set of
    dimensions the record names equals the set of dimensions that were made to
    differ. A missing family is a disagreement that went unrecorded; an extra one
    is an agreement reported as a difference.

    The uncapped call is used so that this stays a statement about *finding*
    disagreements. The cap is a separate mechanism with its own properties below,
    and mixing the two would let a capped-away entry read as a failure to find
    it.
    """
    comparison = compare_results(pair.pandas_result, pair.glue_result, max_entries=_UNCAPPED)

    assert comparison["omitted"] == 0, "the uncapped call must be complete for this to be about finding"
    assert _families(comparison) == set(pair.expected), (
        f"unrecorded disagreements: {sorted(set(pair.expected) - _families(comparison))}; "
        f"spurious entries: {sorted(_families(comparison) - set(pair.expected))}; "
        f"pairs: { {dimension: value.label for dimension, value in pair.pairs.items()} }"
    )
    # A name outside the closed list would pass the equality above only if the
    # expected set had also been wrong; asserted separately because "nothing else
    # is ever recorded" is a claim in its own right.
    assert _families(comparison) <= set(COMPARED_DIMENSIONS)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pair=result_pairs())
def test_a_recorded_entry_carries_each_sides_value_and_omits_an_absent_side(pair: ResultPair) -> None:
    """**Validates: Requirements 4.2**

    An entry is only evidence if it carries what the two executors said. And a
    side that carried nothing must be expressed by the **absence of its key**,
    not by an in-band marker — any marker is a value an executor could itself
    emit, so ``"pandas": None`` could not be told from a genuine ``None``.

    Restricted to the scalar dimensions, where an entry's name is the dimension
    itself; the per-column expansion has its own property below.
    """
    comparison = compare_results(pair.pandas_result, pair.glue_result, max_entries=_UNCAPPED)
    scalar_dimensions = set(_TOP_LEVEL_DIMENSIONS) | set(_DQ_SCALAR_DIMENSIONS)

    for dimension, value_pair in pair.pairs.items():
        if dimension not in scalar_dimensions or not value_pair.disagrees:
            continue
        entry = _entry_for(comparison, dimension)

        assert ("pandas" in entry) is (value_pair.pandas is not _DROP), f"{dimension}: {value_pair.label}"
        assert ("glue" in entry) is (value_pair.glue is not _DROP), f"{dimension}: {value_pair.label}"
        if value_pair.pandas is not _DROP:
            assert _same(entry["pandas"], value_pair.pandas), f"{dimension}: {value_pair.label}"
        if value_pair.glue is not _DROP:
            assert _same(entry["glue"], value_pair.glue), f"{dimension}: {value_pair.label}"


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pair=result_pairs())
def test_the_proxy_refusal_dimension_is_the_condition_and_not_a_value_comparison(pair: ResultPair) -> None:
    """**Validates: Requirements 4.2**

    The count ``LGR-4dd9b42-1``'s revisit condition asks for, quantified over all
    four combinations of (the proxy refused, the Glue path succeeded).

    Recorded in exactly one of the four, and its payload is ``True``/``True`` —
    which is why it cannot be a value comparison: read as two readings of one
    thing, ``True`` against ``True`` is *agreement*, and the one case worth
    recording would record nothing.
    """
    comparison = compare_results(pair.pandas_result, pair.glue_result, max_entries=_UNCAPPED)
    should_record = pair.proxy_refused and pair.glue_succeeded

    assert (DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS in _families(comparison)) is should_record

    if should_record:
        assert _entry_for(comparison, DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS) == {
            "dimension": DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS,
            "pandas": True,
            "glue": True,
        }


# ---------------------------------------------------------------------------
# Property 16 — the per-column expansion names exactly the disagreeing columns
# ---------------------------------------------------------------------------

_COLUMN_NAMES: Final[tuple[str, ...]] = ("id", "name", "age", "zip", "amount", "created_at", "alpha", "zeta")

#: Per-column value pairs, labelled the same way as the dimension-level tables.
_AGREEING_COLUMN_VALUES: Final[tuple[Any, ...]] = (0, 5, _NAN, None, "int64", [], {"nested": 1})
_DISAGREEING_COLUMN_VALUES: Final[tuple[tuple[Any, Any], ...]] = (
    (0, 1),
    (0, False),
    (None, ""),
    (0, 0.0),
    (5, "5"),
    (_NAN, 0.0),
    ("int64", "bigint"),
    ({"nested": 1}, {"nested": True}),
)


@dataclass(frozen=True)
class ColumnPlan:
    """Two column maps and the columns that were made to differ."""

    dimension: str
    pandas_map: dict[str, Any]
    glue_map: dict[str, Any]
    disagreeing: frozenset[str]


@st.composite
def column_plans(draw: st.DrawFn) -> ColumnPlan:
    """Generate two per-column maps with a known set of disagreeing columns.

    Four states per column — agreeing, disagreeing, present on the pandas side
    only, present on the Glue side only — because a column missing from one
    executor's payload is as real a difference as a differing value, and is the
    shape a transform that dropped a column produces.
    """
    dimension = draw(st.sampled_from(_DQ_PER_COLUMN_DIMENSIONS))
    columns = draw(st.lists(st.sampled_from(_COLUMN_NAMES), min_size=0, max_size=len(_COLUMN_NAMES), unique=True))

    pandas_map: dict[str, Any] = {}
    glue_map: dict[str, Any] = {}
    disagreeing: set[str] = set()

    for column in columns:
        state = draw(st.sampled_from(("agree", "differ", "pandas_only", "glue_only")))
        if state == "agree":
            value = draw(st.sampled_from(_AGREEING_COLUMN_VALUES))
            pandas_map[column] = copy.deepcopy(value)
            glue_map[column] = copy.deepcopy(value)
        elif state == "differ":
            left, right = draw(st.sampled_from(_DISAGREEING_COLUMN_VALUES))
            pandas_map[column] = copy.deepcopy(left)
            glue_map[column] = copy.deepcopy(right)
            disagreeing.add(column)
        elif state == "pandas_only":
            pandas_map[column] = draw(st.sampled_from(_AGREEING_COLUMN_VALUES))
            disagreeing.add(column)
        else:
            glue_map[column] = draw(st.sampled_from(_AGREEING_COLUMN_VALUES))
            disagreeing.add(column)

    return ColumnPlan(
        dimension=dimension,
        pandas_map=pandas_map,
        glue_map=glue_map,
        disagreeing=frozenset(disagreeing),
    )


def _dq_only(dimension: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A data-quality payload carrying one per-column dimension and nothing else."""
    return {dimension: payload}


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(plan=column_plans())
def test_per_column_entries_name_exactly_the_disagreeing_columns(plan: ColumnPlan) -> None:
    """**Validates: Requirements 4.2**

    Exactness one level down. Both sides carry a mapping in every example here,
    so the comparison expands per column, and the entries must name the
    disagreeing columns and no others — a column whose two counts match
    contributes nothing, which is the whole reason the record stays proportional
    to the disagreement.

    The order is asserted too, because it is load-bearing for the cap: which
    columns survive a bound must be a function of the inputs, so the recorded
    names come out sorted rather than in whichever order the dicts happened to
    be built.
    """
    comparison = compare_results(
        _pandas_result(
            proxy_refused=False,
            execution_successful=True,
            top_level={},
            data_quality=_dq_only(plan.dimension, plan.pandas_map),
        ),
        _glue_result(
            execution_successful=True,
            top_level={},
            data_quality=_dq_only(plan.dimension, plan.glue_map),
        ),
        max_entries=_UNCAPPED,
    )

    recorded = [name for name in _names(comparison) if _family(name) == plan.dimension]
    expected = [f"{plan.dimension}[{column}]" for column in sorted(plan.disagreeing)]

    assert recorded == expected


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(plan=column_plans())
def test_a_container_disagreement_records_one_entry_and_not_one_per_column(plan: ColumnPlan) -> None:
    """**Validates: Requirements 4.2, 4.5**

    The other branch: when one side carries no map at all — the shape a failed or
    refused run produces, ``data_quality_results: {}`` — the disagreement is
    about the container. One entry says so, and expanding it into one
    half-absent entry per column would put the wide-frame payload straight back
    into the row the cap exists to keep it out of.

    Skipped when the pandas map is empty, because then both sides carry nothing
    for the dimension and the two absences agree.
    """
    if not plan.pandas_map:
        return

    comparison = compare_results(
        _pandas_result(
            proxy_refused=False,
            execution_successful=True,
            top_level={},
            data_quality=_dq_only(plan.dimension, plan.pandas_map),
        ),
        _glue_result(execution_successful=True, top_level={}, data_quality={}),
        max_entries=_UNCAPPED,
    )

    assert [name for name in _names(comparison) if _family(name) == plan.dimension] == [plan.dimension]


# ---------------------------------------------------------------------------
# Nothing outside the compared list is ever recorded
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pair=result_pairs())
def test_the_deliberately_uncompared_keys_never_appear(pair: ResultPair) -> None:
    """**Validates: Requirements 4.2**

    Every generated pair differs on ``input_records``, ``stderr``,
    ``error_details``, ``exit_code``, ``conversion_report`` and the three
    data-quality scores, all of which are outside the closed dimension list for
    reasons ``shadow_comparison``'s docstring gives one by one. This asserts the
    exclusion generatively rather than trusting it: each of those keys differing
    on every single run is exactly the shape that would flood the record if one
    were added to the list by accident.

    ``conversion_report`` is the sharpest of them — ``None`` on every Glue path
    by design (task 8.1), so comparing it would add one guaranteed entry per
    shadow run saying only that the Glue path is the Glue path.
    """
    uncompared = (
        "input_records",
        "stderr",
        "error_details",
        "exit_code",
        "conversion_report",
        "executed",
        "completeness_score",
        "uniqueness_score",
        "overall_quality_score",
        "not_verified_by_proxy",
        "not_verified_reason",
    )
    comparison = compare_results(pair.pandas_result, pair.glue_result, max_entries=_UNCAPPED)
    recorded = _names(comparison)

    for key in uncompared:
        assert not any(key in name for name in recorded), f"{key!r} reached the record: {recorded}"


# ---------------------------------------------------------------------------
# Requirement 4.5 — the cap and the omitted count
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pair=result_pairs(), cap=st.integers(min_value=-3, max_value=12))
def test_the_cap_keeps_a_prefix_and_omits_exactly_the_rest(pair: ResultPair, cap: int) -> None:
    """**Validates: Requirements 4.5**

    Four claims about the bound, checked against the complete record for the same
    inputs — which is the honest oracle here, because the cap's job is precisely
    to be a *truncation of what was found*, so what was found has to come from
    somewhere other than a re-derivation:

    * the recorded entries are the leading ``cap`` of the complete record, in
      order — not an arbitrary subset;
    * ``len(disagreements) + omitted`` equals the total found, so the two cannot
      drift (the total is derived rather than stored for this reason);
    * ``omitted`` is zero **exactly** when the record is complete, which is what
      makes a bounded record distinguishable from a complete one (R4.5's own
      words);
    * a negative cap is clamped to zero rather than doing something exotic — a
      slice with a negative bound would otherwise drop entries from the *end*
      and silently record the wrong ones.
    """
    complete = compare_results(pair.pandas_result, pair.glue_result, max_entries=_UNCAPPED)
    assert complete["omitted"] == 0
    total = len(complete["disagreements"])

    bounded = compare_results(pair.pandas_result, pair.glue_result, max_entries=cap)
    effective = max(0, cap)

    assert _names(bounded) == _names(complete)[:effective]
    assert len(bounded["disagreements"]) + bounded["omitted"] == total
    assert bounded["omitted"] == max(0, total - effective)
    assert (bounded["omitted"] == 0) is (len(bounded["disagreements"]) == total)
    # The cap drops records, not comparisons: how much was looked at is not a
    # function of how much fitted.
    assert bounded["dimensions_compared"] == complete["dimensions_compared"]


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pair=result_pairs(), cap=st.integers(min_value=-3, max_value=12))
def test_the_comparison_count_never_understates_what_was_recorded(pair: ResultPair, cap: int) -> None:
    """**Validates: Requirements 4.5**

    ``dimensions_compared`` is what distinguishes "no disagreements because the
    two agreed" from "no disagreements because there was almost nothing
    comparable" — and only the first is evidence of agreement, so the retirement
    tally needs the two apart. ``LGR-28b4d75-3`` is the precedent: an early
    return "made 'found nothing' indistinguishable in the database from an agent
    that had never run".

    Every disagreement came out of a comparison, so the count can never be below
    the total found. It is legitimately *above* it whenever dimensions agreed,
    which is the normal case.
    """
    bounded = compare_results(pair.pandas_result, pair.glue_result, max_entries=cap)
    total = len(bounded["disagreements"]) + bounded["omitted"]

    assert bounded["dimensions_compared"] >= total


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    null_columns=st.integers(min_value=MAX_RECORDED_DISAGREEMENTS + 1, max_value=MAX_RECORDED_DISAGREEMENTS + 150),
    dtype_columns=st.integers(min_value=1, max_value=40),
    scalars_differ=st.booleans(),
)
def test_a_disagreement_wider_than_the_default_cap_reports_what_it_dropped(
    null_columns: int, dtype_columns: int, scalars_differ: bool
) -> None:
    """**Validates: Requirements 4.5**

    Widened past the **default** cap, which is where R4.5 earns its keep: 256 is
    half the 512-column frame ceiling precisely so that a full-width frame whose
    every column disagrees is guaranteed to report a non-zero ``omitted`` rather
    than fitting inside the bound by luck.

    Two claims that only appear at this width. The record is exactly
    ``MAX_RECORDED_DISAGREEMENTS`` long and ``omitted`` accounts for the rest.
    And the cap drops a **suffix of the priority order**, so with ``null_counts``
    alone over the bound, not one ``dtypes`` entry survives — which is the
    behaviour that keeps the least informative dimension (C7 predicts every
    ``dtypes`` column disagrees) from crowding out the two the retirement
    decision is made on.
    """
    nulls_left = {f"c{index:04d}": index for index in range(null_columns)}
    nulls_right = {column: value + 1 for column, value in nulls_left.items()}
    dtypes_left = {f"c{index:04d}": "int64" for index in range(dtype_columns)}
    dtypes_right = {column: "bigint" for column in dtypes_left}

    pandas_dq = {"null_counts": nulls_left, "dtypes": dtypes_left, "duplicates": 0, "schema_compliance": 100.0}
    glue_dq = {
        "null_counts": nulls_right,
        "dtypes": dtypes_right,
        "duplicates": 2 if scalars_differ else 0,
        "schema_compliance": 75.0 if scalars_differ else 100.0,
    }

    pandas_result = _pandas_result(proxy_refused=False, execution_successful=True, top_level={}, data_quality=pandas_dq)
    glue_result = _glue_result(execution_successful=True, top_level={}, data_quality=glue_dq)

    complete = compare_results(pandas_result, glue_result, max_entries=_UNCAPPED)
    bounded = compare_results(pandas_result, glue_result)

    assert complete["omitted"] == 0
    assert len(bounded["disagreements"]) == MAX_RECORDED_DISAGREEMENTS
    assert bounded["omitted"] == len(complete["disagreements"]) - MAX_RECORDED_DISAGREEMENTS
    assert bounded["omitted"] > 0
    assert not any(_family(name) == "dtypes" for name in _names(bounded)), "the cap did not drop the priority suffix"
    if scalars_differ:
        assert {"duplicates", "schema_compliance"} <= _families(bounded)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pair=result_pairs(), cap=st.integers(min_value=0, max_value=12))
def test_the_record_follows_the_declared_priority_order(pair: ResultPair, cap: int) -> None:
    """**Validates: Requirements 4.5**

    The cap is only safe because the order is fixed, so the order is a property
    rather than an incidental. Entries appear in :data:`COMPARED_DIMENSIONS`
    order — read off the exported tuple, so this cannot go stale against a
    reordering the module makes deliberately, only against one it makes by
    accident.
    """
    bounded = compare_results(pair.pandas_result, pair.glue_result, max_entries=cap)
    priority = {dimension: index for index, dimension in enumerate(COMPARED_DIMENSIONS)}
    positions = [priority[_family(name)] for name in _names(bounded)]

    assert positions == sorted(positions), f"out of priority order: {_names(bounded)}"


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pair=result_pairs(), cap=st.integers(min_value=0, max_value=12))
def test_the_record_is_a_function_of_the_inputs_and_not_of_insertion_order(pair: ResultPair, cap: int) -> None:
    """**Validates: Requirements 4.2, 4.5**

    Same inputs, same record — including *which* entries survive the cap. Dict
    iteration order follows insertion order in Python, so a comparison that
    walked the payloads as given would let the same two results produce two
    different records, and a disagreement that appeared in one run and not the
    next would read as movement in the transform rather than as noise in the
    measurement.

    Asserted on the recorded dimension names, which are plain strings: comparing
    whole entries with ``==`` would be defeated by a NaN payload, and using the
    module's own comparator to check its own determinism would be borrowing the
    thing under test.
    """
    rebuilt_pandas = {key: pair.pandas_result[key] for key in reversed(list(pair.pandas_result))}
    rebuilt_glue = {key: pair.glue_result[key] for key in reversed(list(pair.glue_result))}
    for result in (rebuilt_pandas, rebuilt_glue):
        payload = result["data_quality_results"]
        result["data_quality_results"] = {key: payload[key] for key in reversed(list(payload))}

    first = compare_results(pair.pandas_result, pair.glue_result, max_entries=cap)
    second = compare_results(rebuilt_pandas, rebuilt_glue, max_entries=cap)

    assert _names(first) == _names(second)
    assert first["omitted"] == second["omitted"]
    assert first["dimensions_compared"] == second["dimensions_compared"]


# ---------------------------------------------------------------------------
# A missing Glue result is a normal input, not an error
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(pair=result_pairs())
def test_a_missing_glue_result_records_every_dimension_the_pandas_side_carried(pair: ResultPair) -> None:
    """**Validates: Requirements 4.2**

    ``glue_result=None`` is what task 10.3's fail-soft discipline
    (``LGR-60c4985-5``) produces when the Glue side raised, timed out, or was
    refused for capacity, so it is a normal input rather than an error. Nothing
    raises, and the record is exact in the same two directions: every dimension
    the pandas side carried disagrees with the nothing on the other side, and
    every dimension neither side carried agrees.

    ``proxy_refusal_glue_success`` is never recorded here whatever the pandas
    side said, because the Glue path reported no success — which is the right
    answer and worth pinning: a refusal with no Glue result is not evidence for
    retiring the converter.
    """
    comparison = compare_results(pair.pandas_result, None, max_entries=_UNCAPPED)

    expected = {DIMENSION_EXECUTION_SUCCESSFUL, DIMENSION_OUTPUT_RECORDS}
    data_quality = pair.pandas_result["data_quality_results"]
    expected |= {dimension for dimension in pair.pairs if dimension in data_quality}

    assert _families(comparison) == expected
    assert DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS not in _families(comparison)


def test_two_missing_results_agree_about_everything() -> None:
    """**Validates: Requirements 4.2**

    The degenerate end of the same input. Nothing on either side is not a
    disagreement, and ``dimensions_compared`` still says comparisons were made —
    so "both sides produced nothing" is distinguishable from "the comparison
    never ran", which is the distinction ``LGR-28b4d75-3`` is about.
    """
    comparison = compare_results(None, None)

    assert comparison["disagreements"] == []
    assert comparison["omitted"] == 0
    assert comparison["dimensions_compared"] > 0


# ---------------------------------------------------------------------------
# The generator is held to the module's own declaration
# ---------------------------------------------------------------------------


def test_the_generator_covers_every_compared_dimension() -> None:
    """No dimension can be added to the closed list without being quantified.

    :data:`COMPARED_DIMENSIONS` is iterated by the generator, but a dimension it
    had no pair table for would raise a ``KeyError`` deep inside a Hypothesis
    example, which is a poor way to learn about it. This states the requirement
    directly.

    The prefix check is what :func:`_family` depends on: if a future dimension
    were named such that another dimension plus ``"["`` began with it, splitting
    a recorded name at the bracket would attribute entries to the wrong family
    and every exactness assertion here would go soft.
    """
    covered = set(_PAIRS_BY_DIMENSION) | set(_FLAG_DRIVEN_DIMENSIONS)

    assert covered == set(COMPARED_DIMENSIONS), (
        f"not quantified: {sorted(set(COMPARED_DIMENSIONS) - covered)}; "
        f"no longer a dimension: {sorted(covered - set(COMPARED_DIMENSIONS))}"
    )

    for dimension in COMPARED_DIMENSIONS:
        others = [other for other in COMPARED_DIMENSIONS if other != dimension]
        assert not any(other.startswith(f"{dimension}[") for other in others)


@pytest.mark.parametrize("table", [_SCALAR_PAIRS, _MAP_PAIRS, _OUTPUT_RECORD_PAIRS])
def test_every_pair_table_carries_both_answers(table: tuple[ValuePair, ...]) -> None:
    """A table of only-disagreements would make one direction of the property vacuous.

    The second direction of "exactly" — no agreement is recorded — is only
    tested if agreements are generated. Cheap to state, and it fails loudly if
    the tables are ever edited down to the interesting cases.
    """
    assert any(pair.disagrees for pair in table)
    assert any(not pair.disagrees for pair in table)
