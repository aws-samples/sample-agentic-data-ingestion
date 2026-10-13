# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 6 — stderr is bounded regardless of traceback length.

Spec `real-spark-verification` task 3.5. **Pure functions over observations. No
Glue client, mocked or otherwise, is constructed anywhere in this file** — the
mapping builders need none, so building one would be inventing a dependency in
order to test around it.

    *For any* traceback of any length, the ``stderr`` on the returned result is
    no longer than the existing head-plus-tail bound and retains both its first
    and its last characters.

**Validates: Requirement 5.4**

What this generalises
---------------------
``test_glue_result_mapping.py`` (task 3.3) pins the bound on one worked example:
``test_a_long_traceback_is_bounded_but_keeps_its_head_and_tail``, 400 realistic
frames, comfortably over the bound. That stays — a named regression reads better
than a counterexample. This file quantifies the same claim over **lengths that
cross the bound in both directions**, which one example at one length cannot:
the interesting inputs are the ones adjacent to ``_STDERR_HEAD_CHARS +
_STDERR_TAIL_CHARS``, where an off-by-one in the comparison is invisible to a
test that only ever passes 30 KB.

Generated against the UNBOUNDED text, asserted against the bounded result
--------------------------------------------------------------------------
:func:`synthesised_traceback` is deliberately unbounded — the bound belongs to
the builder that writes the result (``glue_session_runner.py:723-742``, read in
full). That is what makes this property checkable rather than circular: the
*reported* traceback is what the generator controls and what R5.4's "first and
last characters of the reported traceback" refers to, and
``statement_failure_results``' ``stderr`` is the value under test. Comparing the
bounded result against a locally re-bounded copy of the same text would only
prove that two calls to one function agree.

How the bound is expressed
--------------------------
Not as a copy of the production truncation marker's wording. The assertions are
that the output *starts with* the reported text's first ``_STDERR_HEAD_CHARS``
characters, *ends with* its last ``_STDERR_TAIL_CHARS``, and that whatever sits
between them is short and names the number of characters dropped. That states
the head-and-tail semantics R5.4 asks for without pinning a string literal that
would silently go stale — and it is what makes the total-length claim fall out
rather than needing to be asserted separately.

The identity of the bound itself is pinned elsewhere, by object identity:
``test_bound_stderr_is_the_pandas_path_function_itself`` asserts
``glue_session_runner._bound_stderr is CodeInterpreterTestRunner._bound_stderr``.
R5.4 says the *same* limits, and one function object is the only way that cannot
drift, so this file does not re-derive the numbers — it imports them.

Both branches that write stderr
-------------------------------
``statement_failure_results`` composes ``stderr`` two ways
(``glue_session_runner.py:826-837``, read in full): from the synthesised
traceback on the ``AVAILABLE``-with-error branch, and from the reached state plus
the statement's last output on the ``CANCELLED``/``ERROR`` branch (R5.6). R5.4 is
unqualified about which, so both are quantified over here. The second branch is
the one where an unbounded ``stdout`` would reach DynamoDB, which is what the
bound exists to prevent.

Ledger and structure notes
--------------------------
``why`` on ``glue_session_runner.py`` returns **no records** — the file is task
3.1/3.3's and still untracked, so the ledger has nothing to say about it yet.
``why`` on ``code_interpreter_runner.py:469`` (``_bound_stderr`` itself) returns
only ``LGR-0176ccc-1``, the 634-file working-tree snapshot the steering notes
warn matches nearly every query and carries no records: there is no fence on the
bound, and no decision recorded about its two constants. That is a gap, not an
all-clear, so the constants are imported rather than restated.
``decisions(concept="error-handling", include_backfilled=True)`` returns exactly
one record, ``LGR-b2f79ef-13``, about narrowing a blanket ``except Exception`` in
``schema_mapping`` — unrelated to stderr, and itself explicitly recording that
this concept is undocumented. ``concept="testing"`` gives the conventions this
file follows: ``LGR-3e45e65-1`` (the unit layer pins its region in
``tests/unit/conftest.py``, so no ``os.environ`` dance is needed here) and
``LGR-b46a5bb-4`` (a property test with no allowlist, because "an allowlist entry
is a door").

Codegraph, on the symbols this file exercises: ``statement_failure_results``
(``glue_session_runner.py:796``) — 14 callers, covered by
``test_glue_result_mapping.py``; ``synthesised_traceback`` (``:723``) — 4
callers, same covering file; ``_bound_stderr``
(``code_interpreter_runner.py:469``) — 4 callers, covered by
``test_code_interpreter_runner.py``. ``_SYNTHESISED_TRACEBACK_HEADER`` (``:661``)
is flagged "no covering tests"; that flag is a false negative of the kind the
spec's Notes record for ``lambda/**``, and this file adds a second covering
reference to it either way. Nothing in ``shared/`` is touched: the bound, the
mapping and the two constants all live under
``lambda/agentic_data_ingestion_test_code/``, so the blast radius does not reach
the Lambda layer.
"""

import os
import sys

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from code_interpreter_runner import (
    _STDERR_HEAD_CHARS,
    _STDERR_TAIL_CHARS,
    _TRACEBACK_MARKER,
)
from glue_session_runner import (
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    STAGE_VALIDATION,
    StatementOutcome,
    exception_summary,
    statement_failure_results,
    synthesised_traceback,
)

#: The length at or below which ``_bound_stderr`` returns its input untouched.
#: Imported rather than restated — R5.4 is "the same limits", and a second copy
#: of 2000 and 1500 is a second thing a retune can leave disagreeing.
_MAX_BOUNDED = _STDERR_HEAD_CHARS + _STDERR_TAIL_CHARS

#: A ceiling on the elision that replaces the dropped middle, NOT a copy of its
#: wording. The production marker is ~34 characters at the largest omitted count
#: generated here; asserting "short" rather than "exactly this string" is what
#: keeps this test from pinning prose. If the marker is ever rewritten to
#: something genuinely long, this fails loudly instead of passing vacuously.
_MAX_ELISION_CHARS = 64

#: Realistic Spark exception names. The value's own text is generated; the name
#: is sampled, because a Spark exception class is not an arbitrary string and
#: generating one would test the generator rather than the mapping.
_ERROR_NAMES = [
    "AnalysisException",
    "Py4JJavaError",
    "IllegalArgumentException",
    "ParseException",
    "PythonException",
    "SparkUpgradeException",
]

#: Printable ASCII, stripped and non-empty. ``exception_summary`` applies
#: ``.strip()`` to both fields (``glue_session_runner.py:713-720``), so a value
#: with outer whitespace is not the value that reaches ``stderr`` — generating
#: pre-stripped text keeps the tail assertion about the mapping rather than about
#: whitespace handling, which the worked examples already cover.
_EXCEPTION_VALUES = (
    st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=180)
    .map(str.strip)
    .filter(bool)
)

_STAGES = st.sampled_from([STAGE_FRAME_BUILD, STAGE_TRANSFORM, STAGE_VALIDATION])

#: Lengths chosen to cross the bound in both directions, per the task's explicit
#: instruction. Three overlapping bands rather than one wide range: a uniform
#: draw over 0..70000 would put almost nothing within a few characters of
#: ``_MAX_BOUNDED``, which is the only region where the comparison's boundary can
#: be wrong. The deterministic test below then pins just-under / exactly-at /
#: just-over so that coverage does not depend on the draw at all.
_TARGET_LENGTHS = st.one_of(
    st.integers(min_value=0, max_value=_MAX_BOUNDED),
    st.integers(min_value=max(0, _MAX_BOUNDED - 8), max_value=_MAX_BOUNDED + 8),
    st.integers(min_value=_MAX_BOUNDED + 1, max_value=_MAX_BOUNDED * 20),
)

_FRAME_PADDING = "-"


def _frame_text(index: int, length: int) -> str:
    """A traceback frame of exactly *length* characters, never blank.

    ``synthesised_traceback`` drops frames that are blank after stripping, so a
    frame whose length is under three cannot be a slice of the realistic prefix
    — the prefix opens with two spaces, and ``"  "`` would be dropped and take
    the generated length with it.
    """
    prefix = f'  File "/opt/spark/python/pyspark/sql/dataframe_{index}.py", line {index}, in frame_{index}'
    if length >= len(prefix):
        return prefix + _FRAME_PADDING * (length - len(prefix))
    if length >= 3:
        return prefix[:length]
    return _FRAME_PADDING * length


def _frames_of_contribution(total: int, count: int) -> list[str]:
    """Frames contributing exactly *total* characters to the joined traceback.

    Each frame contributes ``len(frame) + 1`` — its own characters plus the
    newline ``"\\n".join`` puts before it — so the frames' characters must sum to
    ``total - count``. A total under two cannot be built from any number of
    non-blank frames, so it yields none and the traceback comes out shorter than
    the target; every assertion below is derived from the ACHIEVED length rather
    than the requested one, so that shortfall costs coverage at the very bottom
    of the range and nothing else.
    """
    if total < 2:
        return []
    count = max(1, min(count, total // 2))
    budget = total - count
    base, remainder = divmod(budget, count)
    return [_frame_text(index, base + (1 if index < remainder else 0)) for index in range(count)]


def _error_statement(*, name: str, value: str, frames: list[str]) -> dict:
    """A Glue statement that reached ``AVAILABLE`` and reported an error."""
    return {
        "State": "AVAILABLE",
        "Output": {
            "Status": "error",
            "ErrorName": name,
            "ErrorValue": value,
            "Traceback": frames,
            "Data": {"TextPlain": ""},
        },
    }


def _reported_traceback_statement(*, target: int, frame_count: int, name: str, value: str) -> dict:
    """A statement whose synthesised traceback is about *target* characters long.

    The header and the trailing ``ErrorName: ErrorValue`` are a floor the frames
    are sized against, so ``target`` is met exactly wherever it exceeds that
    floor by at least two.
    """
    exception = f"{name}: {value}"
    floor = len(glue_session_runner._SYNTHESISED_TRACEBACK_HEADER) + 1 + len(exception)
    return _error_statement(name=name, value=value, frames=_frames_of_contribution(target - floor, frame_count))


def _failed_outcome(statement: dict, stage: str) -> StatementOutcome:
    return StatementOutcome(
        stage=stage,
        state="AVAILABLE",
        output_status="error",
        stdout="",
        statement=statement,
    )


def _stopped_outcome(state: str, stage: str, stdout: str) -> StatementOutcome:
    """A statement that stopped without producing an ``Output`` (R5.6)."""
    return StatementOutcome(
        stage=stage,
        state=state,
        output_status=None,
        stdout=stdout,
        statement={"State": state},
    )


def _assert_bounded_head_and_tail(bounded: str, reported: str) -> None:
    """R5.4 over one (reported, bounded) pair.

    Under the bound the reported text must survive whole — a bound that
    truncated short input would lose characters for no reason. Over it, the head
    and the tail must both be present verbatim and the elision between them must
    be short and must say how much went missing, which is what makes a bounded
    report distinguishable from a complete one.
    """
    if len(reported) <= _MAX_BOUNDED:
        assert bounded == reported
        return

    head = reported[:_STDERR_HEAD_CHARS]
    tail = reported[-_STDERR_TAIL_CHARS:]

    assert bounded.startswith(head), "the head of the reported traceback did not survive"
    assert bounded.endswith(tail), "the tail of the reported traceback did not survive"

    elision = bounded[len(head) : len(bounded) - len(tail)]
    assert len(elision) <= _MAX_ELISION_CHARS, f"the elision is not short: {elision!r}"
    assert str(len(reported) - _MAX_BOUNDED) in elision, "the elision does not say how much was dropped"

    # Falls out of the three assertions above; asserted anyway because "bounded"
    # is the claim R5.4 makes and a reader should not have to derive it.
    assert len(bounded) <= _MAX_BOUNDED + _MAX_ELISION_CHARS


# ---------------------------------------------------------------------------
# Property 6 — the error branch, over lengths crossing the bound
# ---------------------------------------------------------------------------


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    target=_TARGET_LENGTHS,
    frame_count=st.integers(min_value=1, max_value=8),
    name=st.sampled_from(_ERROR_NAMES),
    value=_EXCEPTION_VALUES,
    stage=_STAGES,
)
def test_stderr_is_bounded_regardless_of_traceback_length(
    target: int, frame_count: int, name: str, value: str, stage: str
):
    """**Validates: Requirements 5.4**

    Quantified over the reported traceback's length. ``reported`` is the
    unbounded text the mapping was given; ``bounded`` is what it wrote.
    """
    statement = _reported_traceback_statement(target=target, frame_count=frame_count, name=name, value=value)
    reported = synthesised_traceback(statement)
    bounded = statement_failure_results(_failed_outcome(statement, stage))["stderr"]

    _assert_bounded_head_and_tail(bounded, reported)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    target=_TARGET_LENGTHS,
    frame_count=st.integers(min_value=1, max_value=8),
    name=st.sampled_from(_ERROR_NAMES),
    value=_EXCEPTION_VALUES,
    stage=_STAGES,
)
def test_the_first_and_last_characters_of_the_report_always_survive(
    target: int, frame_count: int, name: str, value: str, stage: str
):
    """**Validates: Requirements 5.4**

    R5.4's own words, asserted as literally as they are written: the first and
    the last character of the reported traceback are both present. Stated
    separately from the head-and-tail structure above because it is the claim a
    reviewer reads the requirement for, and because it is what fails if the
    bound is ever changed to keep only a head.

    The stronger reading is asserted alongside it: the first characters are the
    synthesised Python header and the last are the exception, which is the
    ordering that makes the tail worth keeping (``glue_session_runner.py:661``,
    ``:723-742``).
    """
    statement = _reported_traceback_statement(target=target, frame_count=frame_count, name=name, value=value)
    reported = synthesised_traceback(statement)
    bounded = statement_failure_results(_failed_outcome(statement, stage))["stderr"]

    assert reported, "the generator must report something for first/last to mean anything"
    assert bounded[0] == reported[0]
    assert bounded[-1] == reported[-1]

    assert bounded.startswith(_TRACEBACK_MARKER)
    assert bounded.endswith(exception_summary(statement))


# ---------------------------------------------------------------------------
# Property 6 — the stopped-statement branch (R5.6), which also writes stderr
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    target=_TARGET_LENGTHS,
    state=st.sampled_from(["CANCELLED", "ERROR"]),
    stage=_STAGES,
)
def test_a_stopped_statements_last_output_is_bounded_too(target: int, state: str, stage: str):
    """**Validates: Requirements 5.4**

    R5.4 does not say "on the error branch". A cancelled statement's ``stderr``
    is the reached state plus whatever it had printed
    (``glue_session_runner.py:834-835``), and that output is as unbounded a
    quantity as a traceback — this is the branch where a chatty Spark job would
    otherwise put tens of kilobytes into a DynamoDB item.

    ``reported`` is recomposed here the way the builder composes it rather than
    read back out of the result, because the whole point is that the result is
    the bounded copy.
    """
    stdout = _FRAME_PADDING * max(0, target)
    bounded = statement_failure_results(_stopped_outcome(state, stage, stdout))["stderr"]

    reported = "\n".join(part for part in (f"Glue statement reached state {state}", stdout) if part)

    _assert_bounded_head_and_tail(bounded, reported)
    assert bounded.startswith(f"Glue statement reached state {state}"[:_STDERR_HEAD_CHARS])


# ---------------------------------------------------------------------------
# The crossing itself, pinned rather than sampled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "label"),
    [
        (-1, "one character under the bound"),
        (0, "exactly at the bound"),
        (1, "one character over the bound"),
        (_MAX_BOUNDED, "well over the bound"),
    ],
)
def test_the_bound_holds_at_and_around_the_crossing(offset: int, label: str):
    """**Validates: Requirements 5.4**

    The task asks for lengths "just under, exactly at, and well over". Sampling
    can supply those but cannot promise them, and off-by-one in a ``<=`` is
    exactly the defect that survives a generator which never lands on the
    boundary — so the three interesting lengths are named here and the
    achieved length is asserted, which also pins the generator helpers'
    exactness.
    """
    target = _MAX_BOUNDED + offset
    statement = _reported_traceback_statement(
        target=target, frame_count=3, name="AnalysisException", value="[UNRESOLVED_COLUMN] cannot resolve `nope`"
    )
    reported = synthesised_traceback(statement)

    assert len(reported) == target, f"the generator did not hit {label}"

    bounded = statement_failure_results(_failed_outcome(statement, STAGE_TRANSFORM))["stderr"]

    _assert_bounded_head_and_tail(bounded, reported)
    assert (bounded == reported) is (offset <= 0), f"truncation disagreed with the bound {label}"
