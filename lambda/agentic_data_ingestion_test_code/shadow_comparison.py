# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shadow-mode comparison: where the two verification executors disagree.

Spec ``real-spark-verification`` task 10.1. Requirements 4.2 and 4.5, design
§4.7.

Under ``VERIFICATION_EXECUTOR=shadow`` the pandas proxy runs first and
authoritatively, the Glue runner runs on the same transform and the same seeded
frame, and this module computes what the two disagreed about.
:func:`compare_results` is the whole of it: **one pure function over two result
dicts**. No boto3, no DynamoDB, no environment read, no clock. Writing the
result to the ``(run_id, "shadow_verification")`` state row is task 10.3, and
orchestrating the two runs is task 10.3 as well — this module cannot reach
either, which is what makes R4.3 ("no gate reads it") hold here by having
nothing to reach.

**Disagreements only** (R4.2). A dimension whose two values are equal
contributes no entry. That is not only a size decision: a record that listed
every dimension would have to be read to find the two lines that matter, and the
count this exists to produce — how often the proxy refused a transform real
Spark ran — is a count of entries, not a count of rows.

════════════════════════════════════════════════════════════════════════
WHAT IS COMPARED, AND WHAT IS DELIBERATELY NOT
════════════════════════════════════════════════════════════════════════

:data:`COMPARED_DIMENSIONS` is the closed list, declared as data so task 10.2's
property test iterates it rather than restating it. Eight dimensions, exactly
the ones the task enumerates:

``execution_successful``
    The headline. The one dimension that measures the converter's refusal
    population directly.

``proxy_refusal_glue_success``
    Whether the pandas path set ``not_verified_by_proxy`` while the Glue path
    succeeded. **This is the number the whole feature exists to produce** — the
    specific evidence ``LGR-4dd9b42-1``'s revisit condition asks for ("PySpark
    becomes available to the test environment, at which point the refused shapes
    can be compared against real Spark and converted on evidence instead of
    refused on caution"), and what turns converter retirement from a leap into a
    measured decision.

``output_records``, ``duplicates``, ``schema_compliance``, ``missing_columns``
    Scalar values. The last three live inside ``data_quality_results``.

``null_counts``, ``dtypes``
    Per-column maps inside ``data_quality_results``, expanded to one entry per
    disagreeing column (see the cap section).

**Not compared, each for a reason:**

* ``input_records`` — the *input* frame's row count. Task 5.1 seeds the
  synthetic frame deterministically precisely so both executors receive the same
  frame (Property 14), so a disagreement here would be a frame-builder defect,
  not an executor difference. Comparing it would file that defect under the
  wrong heading.
* ``conversion_report`` — ``None`` on **every** Glue path and a dict on most
  pandas paths (task 8.1's decision; the key's documented meaning is "None when
  conversion failed or never ran", and the Glue runner never runs a conversion
  by design, R1.4/R3.2). Comparing it would record a disagreement on every
  single shadow run while saying nothing about either executor.
* ``executed``, ``exit_code``, ``stderr``, ``error_details`` — ``stderr`` and
  ``error_details`` are free text that differ on every failure without being
  informative (the two executors report a Spark exception and a pandas exception,
  which are different exceptions by construction); ``executed``/``exit_code``
  move with ``execution_successful``, which is already the headline dimension.
  The task's dimension list is closed and does not include them; widening it is a
  later decision, not a silent one.

════════════════════════════════════════════════════════════════════════
THE ``dtypes`` DIMENSION IS PRESENTLY UNMEASURABLE, AND THAT IS NOT A DEFECT HERE
════════════════════════════════════════════════════════════════════════

Design C7 predicts that **every** ``dtypes`` entry moves, because the two
vocabularies are disjoint (``object``/``int64``/``float64`` against
``string``/``bigint``/``double``). It cannot be observed from a result dict
today, and a reader of a shadow record needs to know why before concluding the
executors agree:

``dtypes`` is in the emitted ``VALIDATION:`` payload on both sides — it is in
``glue_session_runner.VALIDATION_PAYLOAD_KEYS`` and in the pandas builder's
payload (``code_interpreter_runner.py:568``) — but
``CodeInterpreterTestRunner._parse_results`` copies seven keys into
``data_quality_results`` and ``dtypes`` is not one of them
(``code_interpreter_runner.py:626-634``). The Glue runner reuses that parser
verbatim by import (``glue_session_runner.py:2573``, design §4.1), so the key is
dropped identically on both paths. Both sides therefore report the dimension as
absent, the two absences agree, and no entry is recorded.

That is honest — nothing is being hidden — but it means **C7's prediction is not
measured by shadow mode as it stands**. The dimension is kept in the list anyway:
it costs one comparison of two sentinels per run, and the day either the parser
carries the key or a caller passes the marker payload through, the measurement
starts working with no change here.

*Two in-repo claims this observation contradicts, neither of which is changed
from here.* ``code_interpreter_runner.py:108`` says "``_build_validation_code``
emits ``null_counts`` and ``dtypes`` … ``_parse_results`` puts **both** into
``data_quality_results``", and ``LGR-b2f79ef-12`` (template / ratified) repeats
it — "``null_counts`` and ``dtypes`` are per-column maps that reach the state row
via ``_parse_results`` — ~46 KB at 512 columns". Only ``null_counts`` reaches it,
so the state-row cost that justifies ``_MAX_TEST_FRAME_COLUMNS`` is about half
the figure recorded. The ceiling is not touched here: it is p33's, its revisit
condition is "when ``null_counts``/``dtypes`` stop reaching the state row", and
the honest reading of that condition is that half of it was never true rather
than that it has now been met.

════════════════════════════════════════════════════════════════════════
AGREEMENT IS EXACT-TYPE AND STRUCTURAL
════════════════════════════════════════════════════════════════════════

:func:`values_agree` compares ``type(left) is type(right)`` **and** equality, at
every level of a nested value. Exact type rather than ``isinstance``, for the
reason ``tests/fixtures/run_test_result_shapes.py`` gives for the same choice:
``bool`` is a subclass of ``int``, so ``0 == False`` and an ``isinstance`` check
would let a count silently become a flag. A shadow record that could not tell
``duplicates: 0`` from ``duplicates: False`` would be measuring the wrong thing.

The consequences are deliberate and worth stating, because each looks like a
false positive until you know it is not:

* ``100`` against ``100.0`` is a **disagreement**. The two executors emit
  different JSON, and a numeric type changing between executors is exactly the
  kind of movement C7 is about.
* ``None`` against ``""`` is a disagreement. Different types.
* An empty dict against an **absent** key is a disagreement:
  :data:`ABSENT` is a distinct sentinel whose type is neither ``dict`` nor
  ``NoneType``, so absence never collapses into emptiness.
* Two ``float('nan')`` values **agree**. ``nan != nan`` in IEEE 754, but two
  executors both reporting NaN is not a disagreement about anything.
* ``missing_columns`` is compared **in order**. Both paths iterate the declared
  target columns in declaration order
  (``code_interpreter_runner.py:550``, ``glue_session_runner.declared_target_columns``),
  so an order difference is a real difference in the emitted payload.

════════════════════════════════════════════════════════════════════════
THE CAP, AND WHY THE PRIORITY ORDER IS PART OF IT
════════════════════════════════════════════════════════════════════════

The ``_MAX_TEST_FRAME_COLUMNS`` reasoning applies twice over here (design §4.7,
C5): two executors' ``null_counts`` and ``dtypes`` would be ~92 KB at 512
columns, against DynamoDB's 400 KB item limit on the state row task 10.3 writes.
So the recorded entries are capped at :data:`MAX_RECORDED_DISAGREEMENTS` and the
number dropped is reported as ``omitted`` (R4.5), which is what makes a bounded
record distinguishable from a complete one.

**256, and the arithmetic rather than a round number.** It is half
``_MAX_TEST_FRAME_COLUMNS``, so a full-width frame whose every column's dtype
disagrees — the case C7 predicts — is *guaranteed* to trip the cap and record a
non-zero ``omitted``, rather than fitting by luck. At a generous ~200 serialised
bytes per entry, 256 entries is ~51 KB: an order of magnitude inside the item
limit even before the row's other attributes.

**The cap is only safe because the order is fixed.** C7 predicts every column
disagrees on ``dtypes``, so an unordered cap would let the least informative
dimension crowd out ``execution_successful`` and ``proxy_refusal_glue_success``
— the two the retirement decision is actually made on. :data:`COMPARED_DIMENSIONS`
is therefore in priority order, scalars first and ``dtypes`` last, columns within
a per-column dimension sorted by name so the recorded subset is a function of the
inputs alone and not of dict iteration order.

**DEVIATION from a design parenthetical, recorded rather than quiet.** Design
§4.7 says ``dtypes`` should be "recorded as a mapping rather than as per-column
diffs, or the payload is the whole schema". This module records it per column
instead. Two reasons. A single entry carrying two whole mappings *defeats* the
cap — one entry can then be the whole schema twice, which is the ~92 KB case the
cap exists to prevent — and it puts the omission *inside* an entry, where
``omitted`` cannot describe it, so R4.5 stops being satisfiable. The design's
stated reason for preferring the mapping form is size, and a per-column form
under a priority-ordered cap is strictly smaller. Per-column also keeps the
common shape cheap: expansion happens **only when both sides are dicts**, so a
run where one executor produced no data-quality payload at all records one
container-level entry rather than 512 half-absent ones.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final

# ---------------------------------------------------------------------------
# The absent sentinel
# ---------------------------------------------------------------------------


class _Absent:
    """The type of :data:`ABSENT`.

    A dedicated class rather than ``None`` or a string, so that "this side did
    not carry the value" cannot be confused with a value the side genuinely
    carried. ``None`` is a legitimate value elsewhere in the
    Test_Result_Contract, and any string sentinel is a string an executor could
    in principle emit.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "ABSENT"


#: One side did not carry this dimension at all. Distinct from every value an
#: executor can produce, which is what makes the "empty dict against absent key"
#: near-miss decidable.
ABSENT: Final[_Absent] = _Absent()


# ---------------------------------------------------------------------------
# The dimensions
# ---------------------------------------------------------------------------

DIMENSION_EXECUTION_SUCCESSFUL: Final = "execution_successful"
DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS: Final = "proxy_refusal_glue_success"
DIMENSION_OUTPUT_RECORDS: Final = "output_records"
DIMENSION_DUPLICATES: Final = "duplicates"
DIMENSION_SCHEMA_COMPLIANCE: Final = "schema_compliance"
DIMENSION_MISSING_COLUMNS: Final = "missing_columns"
DIMENSION_NULL_COUNTS: Final = "null_counts"
DIMENSION_DTYPES: Final = "dtypes"

#: Read straight off the result dict.
_TOP_LEVEL_DIMENSIONS: Final[tuple[str, ...]] = (
    DIMENSION_EXECUTION_SUCCESSFUL,
    DIMENSION_OUTPUT_RECORDS,
)

#: Scalar values inside ``data_quality_results``.
_DQ_SCALAR_DIMENSIONS: Final[tuple[str, ...]] = (
    DIMENSION_DUPLICATES,
    DIMENSION_SCHEMA_COMPLIANCE,
    DIMENSION_MISSING_COLUMNS,
)

#: Per-column maps inside ``data_quality_results``, expanded one entry per
#: disagreeing column when — and only when — both sides carry a mapping.
_DQ_PER_COLUMN_DIMENSIONS: Final[tuple[str, ...]] = (
    DIMENSION_NULL_COUNTS,
    DIMENSION_DTYPES,
)

#: The closed list, in **priority order**: the cap keeps a prefix of this and
#: drops the tail. Scalars first, then ``null_counts``, then ``dtypes`` last
#: because C7 predicts every one of its columns disagrees. Declared as data so
#: task 10.2's property test iterates it instead of restating it.
#:
#: The three ``_*_DIMENSIONS`` groups above plus
#: :data:`DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS` partition this tuple, which is
#: what makes :func:`compare_results`' dispatch total.
COMPARED_DIMENSIONS: Final[tuple[str, ...]] = (
    DIMENSION_EXECUTION_SUCCESSFUL,
    DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS,
    DIMENSION_OUTPUT_RECORDS,
    DIMENSION_DUPLICATES,
    DIMENSION_SCHEMA_COMPLIANCE,
    DIMENSION_MISSING_COLUMNS,
    DIMENSION_NULL_COUNTS,
    DIMENSION_DTYPES,
)

#: The key the data-quality dimensions are nested under, on both executors'
#: results (``code_interpreter_runner.py:626``).
_DATA_QUALITY_KEY: Final = "data_quality_results"

#: The pandas proxy's refusal marker. Absent from every Glue result by design
#: (R3.2, asserted by task 8.3's Property 4), which is why the dimension it
#: feeds is a condition rather than a value comparison.
_PROXY_REFUSAL_KEY: Final = "not_verified_by_proxy"

#: Hard cap on recorded entries. Derivation in the module docstring: half
#: ``_MAX_TEST_FRAME_COLUMNS`` so a full-width dtype disagreement is guaranteed
#: to report a non-zero ``omitted`` rather than fitting by luck, and ~51 KB at a
#: generous ~200 bytes per entry against DynamoDB's 400 KB item limit.
MAX_RECORDED_DISAGREEMENTS: Final = 256


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------


def values_agree(left: Any, right: Any) -> bool:
    """Return True when *left* and *right* are the same value, exactly.

    Exact type at every level, then equality. See the module docstring for why
    ``isinstance`` is wrong here and for the five consequences that look like
    false positives and are not.
    """
    if type(left) is not type(right):
        return False

    if isinstance(left, Mapping):
        if set(left) != set(right):
            return False
        return all(values_agree(left[key], right[key]) for key in left)

    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return False
        return all(values_agree(a, b) for a, b in zip(left, right, strict=True))

    if isinstance(left, float) and math.isnan(left) and math.isnan(right):
        # Two executors both reporting NaN is not a disagreement, and
        # `nan == nan` is False. Reached only when both are floats, because the
        # exact-type check above already ran.
        return True

    return bool(left == right)


# ---------------------------------------------------------------------------
# Reading a dimension off a result
# ---------------------------------------------------------------------------


def _side(result: Mapping[str, Any] | None, key: str) -> Any:
    """Return ``result[key]``, or :data:`ABSENT` if it is not there.

    A ``None`` *result* is ABSENT for every key: task 10.3's fail-soft
    orchestration can reach the comparison with no Glue-side result at all when
    the Glue run raised, timed out or was refused for capacity, and that must
    read as "this side carried nothing" rather than raising.
    """
    if not isinstance(result, Mapping):
        return ABSENT
    return result.get(key, ABSENT)


def _data_quality(result: Mapping[str, Any] | None) -> Any:
    """Return the result's ``data_quality_results`` mapping, or :data:`ABSENT`.

    A present-but-not-a-mapping value is reported as ABSENT for the nested
    dimensions rather than being indexed into. The contract types the key as
    ``dict`` on every outcome, so this is a defensive read, not an expected
    path — but a pure function over two dicts must not raise on a shape it did
    not expect, or task 10.3's fail-soft wrapper is doing work this could have
    avoided.
    """
    payload = _side(result, _DATA_QUALITY_KEY)
    return payload if isinstance(payload, Mapping) else ABSENT


def _dq_side(result: Mapping[str, Any] | None, key: str) -> Any:
    payload = _data_quality(result)
    if payload is ABSENT:
        return ABSENT
    return _side(payload, key)


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


def _entry(dimension: str, pandas_value: Any, glue_value: Any) -> dict[str, Any]:
    """Return one disagreement entry.

    A side's key is **omitted** when that side carried nothing, because absence
    expressed as absence is unambiguous in both JSON and DynamoDB, whereas any
    in-band marker is a value an executor could itself emit. So an entry with no
    ``pandas`` key means the pandas result did not carry that dimension.
    """
    entry: dict[str, Any] = {"dimension": dimension}
    if pandas_value is not ABSENT:
        entry["pandas"] = pandas_value
    if glue_value is not ABSENT:
        entry["glue"] = glue_value
    return entry


def _scalar_comparison(
    dimension: str,
    pandas_value: Any,
    glue_value: Any,
) -> tuple[int, list[dict[str, Any]]]:
    """Return ``(comparisons_made, entries)`` for one scalar dimension."""
    if values_agree(pandas_value, glue_value):
        return 1, []
    return 1, [_entry(dimension, pandas_value, glue_value)]


def _per_column_comparison(
    dimension: str,
    pandas_value: Any,
    glue_value: Any,
) -> tuple[int, list[dict[str, Any]]]:
    """Return ``(comparisons_made, entries)`` for one per-column dimension.

    Expands to one entry per disagreeing column **only when both sides carry a
    mapping**. Otherwise the disagreement is about the container, and one
    container-level entry says so — which is what keeps the common
    one-executor-failed shape at one entry instead of 512 half-absent ones.

    Columns are visited in sorted order so that which columns survive the cap is
    a function of the inputs and not of dict iteration order.
    """
    both_mappings = isinstance(pandas_value, Mapping) and isinstance(glue_value, Mapping)
    if not both_mappings:
        return _scalar_comparison(dimension, pandas_value, glue_value)

    entries: list[dict[str, Any]] = []
    columns = sorted(set(pandas_value) | set(glue_value))
    for column in columns:
        left = _side(pandas_value, column)
        right = _side(glue_value, column)
        if not values_agree(left, right):
            entries.append(_entry(f"{dimension}[{column}]", left, right))
    return len(columns), entries


def _proxy_refusal_comparison(
    pandas_result: Mapping[str, Any] | None,
    glue_result: Mapping[str, Any] | None,
) -> tuple[int, list[dict[str, Any]]]:
    """Return ``(1, entries)`` for the proxy-refused / Glue-succeeded dimension.

    **A condition, not a value comparison, and the difference matters.** The two
    quantities are not two readings of one thing: one is the pandas proxy's
    refusal marker, the other is the Glue path's success. Compared as values they
    would be ``True`` against ``True`` in exactly the case worth recording — the
    proxy refused and real Spark ran it — and a value comparison would therefore
    call that *agreement* and record nothing. Backwards, and silently so.

    The Glue path can never carry ``not_verified_by_proxy`` (R3.2), so there is
    no symmetric reading available even in principle.

    The entry keeps the uniform ``{dimension, pandas, glue}`` shape every other
    entry has, so task 10.2's property test needs no special case. Read the two
    values against the dimension's name: ``pandas: True`` is "the proxy set
    ``not_verified_by_proxy``", ``glue: True`` is "the Glue path reported
    ``execution_successful``".
    """
    proxy_refused = _side(pandas_result, _PROXY_REFUSAL_KEY) is True
    glue_succeeded = _side(glue_result, DIMENSION_EXECUTION_SUCCESSFUL) is True
    if proxy_refused and glue_succeeded:
        return 1, [_entry(DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS, True, True)]
    return 1, []


# ---------------------------------------------------------------------------
# The public function
# ---------------------------------------------------------------------------


def compare_results(
    pandas_result: Mapping[str, Any] | None,
    glue_result: Mapping[str, Any] | None,
    *,
    max_entries: int = MAX_RECORDED_DISAGREEMENTS,
) -> dict[str, Any]:
    """Return the Shadow_Comparison for one transform's two executor results.

    Pure: reads only its arguments, returns a plain JSON-shaped dict, and
    touches no AWS, no environment and no clock.

    :param pandas_result: the authoritative Pandas_Proxy result (R4.1).
    :param glue_result: the Glue_Session_Runner result, or ``None`` when the
        Glue side produced nothing — it raised, timed out, or was refused for
        capacity. ``None`` is a normal input here, not an error: task 10.3's
        fail-soft discipline (``LGR-60c4985-5``) means the shadow side must be
        able to have produced nothing without that altering anything.
    :param max_entries: the cap. Defaults to
        :data:`MAX_RECORDED_DISAGREEMENTS`; a caller lowers it only to make the
        bound observable in a test. A value below zero is treated as zero.

    Returns a dict with three keys:

    ``disagreements``
        At most *max_entries* entries, in :data:`COMPARED_DIMENSIONS` priority
        order, one per dimension whose two values differ (R4.2). Each entry
        carries ``dimension`` and the sides that had a value.
    ``omitted``
        How many disagreements were dropped for size (R4.5). Zero exactly when
        the record is complete, so a bounded record is distinguishable from a
        complete one. The total found is ``len(disagreements) + omitted``,
        derived rather than stored so the two cannot drift.
    ``dimensions_compared``
        How many atomic comparisons were made. Distinguishes "no disagreements
        because the two agreed" from "no disagreements because there was almost
        nothing comparable" — a distinction the retirement tally needs, since
        the second is not evidence of agreement. ``LGR-28b4d75-3`` (template /
        ratified) is the precedent: an empty fleet sweep writes its state row
        anyway, because the early return "made 'found nothing' indistinguishable
        in the database from an agent that had never run". Same shape, one domain
        over — an empty ``disagreements`` list alone would be that same
        ambiguity.

    A note for task 10.3, which writes this: the values passed through are the
    executors' own, so ``schema_compliance`` and the quality scores arrive as
    ``float``. DynamoDB will not take a ``float``; converting is the writer's
    job, not this function's, because a conversion here would change the values
    the comparison reports.
    """
    cap = max(0, max_entries)

    comparisons = 0
    found: list[dict[str, Any]] = []

    for dimension in COMPARED_DIMENSIONS:
        if dimension == DIMENSION_PROXY_REFUSAL_GLUE_SUCCESS:
            made, entries = _proxy_refusal_comparison(pandas_result, glue_result)
        elif dimension in _TOP_LEVEL_DIMENSIONS:
            made, entries = _scalar_comparison(
                dimension,
                _side(pandas_result, dimension),
                _side(glue_result, dimension),
            )
        elif dimension in _DQ_SCALAR_DIMENSIONS:
            made, entries = _scalar_comparison(
                dimension,
                _dq_side(pandas_result, dimension),
                _dq_side(glue_result, dimension),
            )
        else:  # _DQ_PER_COLUMN_DIMENSIONS — the fourth and last group
            made, entries = _per_column_comparison(
                dimension,
                _dq_side(pandas_result, dimension),
                _dq_side(glue_result, dimension),
            )
        comparisons += made
        found.extend(entries)

    return {
        "disagreements": found[:cap],
        "omitted": max(0, len(found) - cap),
        "dimensions_compared": comparisons,
    }
