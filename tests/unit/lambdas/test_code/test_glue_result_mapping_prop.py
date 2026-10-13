# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 5 — Glue observations map onto the execution contract.

Spec `real-spark-verification` task 3.4, design §4.5's mapping table. **Pure
functions over observations. No Glue client, mocked or otherwise, is constructed
anywhere in this file** — the builders under test take a statement dict or an
exception and return a dict, so a client would be scenery.

    *For any* Glue observation — any combination of statement state, output
    status, reported exception name and value, and traceback shape, including a
    traceback containing no Python traceback header and including a failure at
    session creation — the mapped result satisfies the whole mapping table.

**Validates: Requirements 1.5, 5.1, 5.2, 5.3, 5.5, 5.6, 5.7, 8.5**

What this generalises
---------------------
``test_glue_result_mapping.py`` (task 3.3) pins each row of §4.5's table as a
worked example — 68 tests, including the degenerate cases this generator also
has to reach: an absent ``ErrorName``, an absent ``ErrorValue``, both absent, an
empty ``Traceback``, and a ``Traceback`` sent as a bare string rather than a
list. Those stay. A named regression is easier to read than a counterexample,
and they are the examples this file was written from. What is added here is the
**cross product**: state × output status × exception name/value × traceback
shape, so an observation nobody thought to write down is covered by construction
rather than by remembering to add a parametrize case.

R5.5 is covered two ways, on purpose
------------------------------------
The task's instruction is to put **header-free tracebacks in the generator** so
the trap is covered by construction, and :func:`_tracebacks` does that — every
frame it emits is asserted header-free in
:func:`test_the_generator_emits_only_header_free_tracebacks`, so the premise is
checked rather than assumed. That alone would not catch a *stdout*-sniffing
reimplementation, though, and it cannot show that classification is
traceback-**independent** — only that it happens to be right on header-free
input.

So the second form is
:func:`test_classification_is_invariant_under_the_traceback_reported`, which
swaps one observation's ``Traceback`` for another (including for one that DOES
carry ``Traceback (most recent call last``) and asserts the verdict fields do
not move. A runner that classified by sniffing would answer differently for the
two, on the same ``Output.Status``, and fail there.

R5.7 is checked, not assumed
----------------------------
``glue_session_runner.py:783-787`` claims R5.7 holds *by construction*: the only
builder that can set ``execution_successful`` true is also the only one that sets
``exit_code``, and it sets it to zero. That claim is exactly the kind that is
true until someone edits one of the two lines, so
:func:`test_no_builder_reports_success_with_a_non_zero_exit_code` applies **every
builder to every generated observation** — including the misrouted combinations
the module's own docstring reasons about, such as ``success_results`` over a
failed statement — rather than only the routed one. The builders are total
functions over their argument types, so a misroute is a legal input and the
absolute has to hold there too.

Not tested here
---------------
* **The stderr bound (R5.4)** is task 3.5's property. Tracebacks long enough to
  cross the bound are generated (a bound crossed by accident is still a crossing
  the mapping has to survive), but nothing here asserts anything about the
  bound's *shape*. What is asserted instead is that ``error_details`` carries the
  reported exception regardless — which holds because the reason is interpolated
  before the stderr block and is never truncated
  (``glue_session_runner.py:838``).
* **Capacity exhaustion.** ``ResourceNumberLimitExceededException`` is
  deliberately excluded from :data:`_CREATE_FAILURE_CODES`: R8.5 is scoped to a
  creation failure "for a reason other than a resource number limit", and
  routing capacity away from :func:`session_creation_failure_results` is task
  3.6's. Generating it here would pin the wrong owner's behaviour.
* **``conversion_report``.** Its contract type is set by the CALL PATH, not by
  the builder — every builder returns ``None`` and task 8.1 owns the real
  decision — so :func:`_violations` excludes it from the shape comparison, the
  same way ``test_glue_result_mapping.py::_violations`` does.

Ledger and structure notes
--------------------------
``why`` on ``glue_session_runner.py`` and on
``tests/fixtures/run_test_result_shapes.py`` returns **no records**: both are
this spec's own untracked files, so the ledger has nothing on them yet. That is
a gap, not an all-clear. What does govern this file:

* ``LGR-4dd9b42-1`` (template) — the record whose revisit condition this whole
  spec exists to answer: "PySpark becomes available to the test environment, at
  which point the refused shapes can be compared against real Spark and
  converted on evidence instead of refused on caution."
* ``LGR-b2f79ef-13`` (template, the only ``error-handling`` record) — narrow a
  clause that re-labels a classified exception in the same change, rather than
  later. The analogue honoured here: :func:`session_creation_failure_results`
  carries ``str(exc)`` and the property asserts the reported text survives into
  ``error_details`` rather than being paraphrased away.
* ``LGR-3e45e65-1`` (template) — the unit layer pins its region in
  ``tests/unit/conftest.py``, so no ``os.environ`` dance is needed here.
* ``LGR-28b4d75-5`` (template, cited by the fixture) — a test written from the
  producer's output cannot detect the producer drifting. Every expectation below
  is derived from the requirement or from the declared contract table, never
  from what the builders currently return.

Codegraph, before this file existed: ``statement_failure_results`` 14 callers,
``success_results`` 8, ``lifecycle_failure_results`` 4,
``not_executed_results`` 4, ``StatementOutcome`` 7 — every caller in
``glue_session_runner.py`` itself or in the two task 3.1/3.3 test files, because
executor selection is task 9.1 and nothing production-side constructs the runner
yet. All five report covering tests, so no characterization test is owed before
this one. ``glue_session_runner`` imports nothing from ``shared/``, so the
shared-layer blast radius is nil; this file adds coverage and changes no
behaviour.
"""

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import hypothesis.strategies as st
from botocore.exceptions import ClientError
from hypothesis import given, settings
from tests.fixtures.run_test_result_shapes import (
    BASE_CONTRACT_KEYS,
    CLIENT_UNAVAILABLE,
    STATEMENT_FAILURE,
    SUCCESS,
    OutcomeShape,
    shape_violations,
)

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from code_interpreter_runner import _TRACEBACK_MARKER
from glue_session_runner import (
    _SESSION_STATES_TERMINAL_BAD,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    STAGE_VALIDATION,
    GlueSessionError,
    GlueSessionTestRunner,
    StatementOutcome,
    lifecycle_failure_results,
    not_executed_results,
    session_creation_failure_results,
    statement_failure_results,
    success_results,
)

STAGES = (STAGE_FRAME_BUILD, STAGE_TRANSFORM, STAGE_VALIDATION)

# ── The state axis ────────────────────────────────────────────────────
#
# `GetStatement` documents `State` ∈ {WAITING, RUNNING, AVAILABLE, CANCELLING,
# CANCELLED, ERROR} (design §4.5). Only the terminal ones reach the mapping —
# `_await_statement` polls until `_STATEMENT_STATES_TERMINAL` — so those are
# what is generated, plus two shapes that are NOT in the documented set:
#
#   * `""`, which is what `_observe` produces from a statement dict with no
#     `State` key at all (`glue_session_runner.py:496`)
#   * `TIMEOUT`, standing in for a state a future API version adds
#
# Both must map to a failure rather than falling through to success, because
# `StatementOutcome.failed` is written as "not AVAILABLE ⇒ failed" rather than
# as an enumeration of bad states. Generating them is what checks that.
_STATE_AVAILABLE = "AVAILABLE"
_TERMINAL_STATES = (_STATE_AVAILABLE, "CANCELLED", "ERROR")
_UNMODELLED_STATES = ("TIMEOUT", "")
_ALL_STATES = _TERMINAL_STATES + _UNMODELLED_STATES


class _Absent:
    """Glue omitted the key entirely.

    A distinct object rather than a sentinel string, so ``isinstance`` is the
    test and no generated value can ever collide with it — an absent key and a
    key carrying the sentinel's own text are different observations, and a
    string sentinel cannot keep them apart.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


# ── The output-status axis ────────────────────────────────────────────
#
# `ok` and `error` are the two Glue documents. The rest are near-misses that
# must NOT be read as success: casing differences, a blank status, and an absent
# `Status` key on a statement that otherwise reported an `Output`.
_STATUS_ABSENT = _Absent()
_OUTPUT_STATUSES: tuple[object, ...] = ("ok", "error", "OK", "Error", "", _STATUS_ABSENT)

# ── The exception axis ────────────────────────────────────────────────
#
# `None` and `_FIELD_ABSENT` are different observations and both are generated:
# Glue sending `ErrorName: null` and Glue omitting the key entirely take
# different routes through `exception_summary`'s `or ""`. The whitespace-only
# entry reaches the branch where a field was reported but carries nothing, which
# is why `exception_summary` strips before testing truthiness.
_FIELD_ABSENT = _Absent()

_ERROR_NAMES: tuple[object, ...] = (
    "AnalysisException",
    "Py4JJavaError",
    "PythonException",
    "IllegalArgumentException",
    "",
    "   ",
    None,
    _FIELD_ABSENT,
)

_ERROR_VALUES: tuple[object, ...] = (
    "[UNRESOLVED_COLUMN] cannot resolve `nope` given input columns: [id, value]",
    "An error occurred while calling o123.showString.\n: java.lang.RuntimeException",
    "division by zero",
    # Braces, because `_parse_results`' brace-depth scanner reads stdout and a
    # value that leaked into it would be the way to break the scanner.
    'Cannot parse {"a": {"b": 1}} as a struct',
    "",
    "\n\t ",
    None,
    _FIELD_ABSENT,
)

# ── The traceback axis ────────────────────────────────────────────────
#
# HEADER-FREE BY CONSTRUCTION, which is the task's explicit instruction and the
# whole of R5.5's trap: Glue's `Traceback` is a list of frame strings with no
# `Traceback (most recent call last` line, so a runner that sniffed for one
# would find nothing in a real Spark exception and pass every one of them as a
# success. `test_the_generator_emits_only_header_free_tracebacks` asserts the
# absence rather than trusting this comment.
#
# The last template deliberately NAMES an exception class without being a
# header, so "contains the word AnalysisException" cannot be mistaken for
# "contains a traceback header".
_FRAME_TEMPLATES = (
    '  File "<stdin>", line {n}, in <module>\n',
    '  File "/opt/spark/python/pyspark/sql/dataframe.py", line {n}, in withColumn\n',
    '  File "/opt/spark/python/pyspark/errors/exceptions/captured.py", line {n}, in deco\n',
    "    raise converted from None\n",
    "AnalysisException: [UNRESOLVED_COLUMN] cannot resolve column {n}\n",
)

#: Shapes the mapping has to survive. `absent` and `empty_list` are the same
#: observation to `synthesised_traceback`'s `or []`; `blank_frames` is the one
#: that must not render as a header over whitespace; `bare_string` is not a
#: documented Glue shape but is the one that would otherwise be iterated
#: character by character; `long_frames` crosses `_bound_stderr`'s limit, whose
#: own behaviour is task 3.5's to assert.
_TRACEBACK_SHAPES = ("frames", "empty_list", "blank_frames", "bare_string", "absent", "long_frames")

#: Tracebacks that DO carry the Python header — used only by the invariance
#: property, never by the generator that stands in for Glue. Glue does not send
#: these; they exist so that "classification does not read the traceback" can be
#: stated as a difference that makes no difference.
_HEADER_BEARING_TRACEBACKS: tuple[object, ...] = (
    [f"{_TRACEBACK_MARKER}):\n", '  File "<stdin>", line 1, in <module>\n'],
    f'{_TRACEBACK_MARKER}):\n  File "<stdin>", line 1, in <module>\n',
)

# ── The validation axis ───────────────────────────────────────────────
#
# What `_parse_results` finds in the combined stdout, which is what decides
# whether a run whose statements all reported `ok` is a SUCCESS or a run that
# validated nothing. R1.5 binds on `clean` only — a run with no `VALIDATION:`
# payload did not execute successfully in the contract's sense.
_VALIDATION_CLEAN = "clean"
_VALIDATION_ERROR = "error"
_VALIDATION_ABSENT = "absent"

#: A line of noise that contains the Python traceback header, to be dropped into
#: a SUCCESSFUL statement's stdout. Glue reports no stderr stream, so the only
#: place a header could appear on an `ok` statement is its printed output — and
#: `Output.Status` still says `ok`, so it is still a success (R5.5).
_STDOUT_HEADER_NOISE = f"{_TRACEBACK_MARKER}):\n  printed by the transform, not raised\n"


def _violations(result: dict, shape: OutcomeShape) -> list[str]:
    """Shape violations, excluding the one key a BUILDER cannot own.

    Duplicated from ``test_glue_result_mapping.py::_violations`` rather than
    imported: importing a test module re-runs its module-level ``sys.path``
    insertion, and three lines is cheaper than that coupling.
    ``conversion_report``'s contract type is set by the call path — every
    builder returns ``None`` and task 8.1 owns the decision — so it is excluded
    here and asserted separately.
    """
    return [violation for violation in shape_violations(result, shape) if "conversion_report" not in violation]


def _reported(field: object) -> str:
    """What ``exception_summary`` will read out of a generated error field.

    Mirrors ``str(output.get(...) or "").strip()`` — the expectation is derived
    from the requirement's words ("the reported exception name and value") plus
    the one normalisation the mapping documents, not from calling the mapping.
    """
    if field is None or isinstance(field, _Absent):
        return ""
    return str(field).strip()


def _render_frames(shape: str, seed: int) -> object:
    """One ``Traceback`` value in *shape*. Header-free in every case."""
    if shape == "absent":
        return _FIELD_ABSENT
    if shape == "empty_list":
        return []
    if shape == "blank_frames":
        return ["", "\n", "  \n"]
    if shape == "bare_string":
        return _FRAME_TEMPLATES[seed % len(_FRAME_TEMPLATES)].format(n=seed)
    count = 400 if shape == "long_frames" else 1 + (seed % 5)
    return [_FRAME_TEMPLATES[(seed + i) % len(_FRAME_TEMPLATES)].format(n=seed + i) for i in range(count)]


def _validation_payload(kind: str, records: int, error: str) -> str:
    if kind == _VALIDATION_ABSENT:
        return ""
    if kind == _VALIDATION_ERROR:
        return "VALIDATION:" + json.dumps({"error": error})
    return "VALIDATION:" + json.dumps(
        {
            "record_count": records,
            "completeness": 0.95,
            "uniqueness": 1.0,
            "schema_compliance": 1.0,
            "overall_quality": 0.98,
            "null_counts": {"value": 5},
            "duplicates": 0,
            "missing_columns": [],
        }
    )


@dataclass(frozen=True)
class Observation:
    """One Glue statement as the API reported it, plus what it reported.

    ``statement`` is the raw ``Statement`` dict — the actual observation. It is
    turned into a :class:`StatementOutcome` by the runner's own
    :meth:`GlueSessionTestRunner._observe`, so the property is quantified over
    Glue's shape rather than over a hand-built outcome, and the field extraction
    under test is the production one.

    The remaining fields are the generated axes, kept so assertions can be
    derived from what was generated instead of from what came back.
    """

    stage: str
    statement: Mapping[str, Any]
    state: str
    status: str
    reported_name: str
    reported_value: str
    traceback_shape: str
    validation: str
    validation_error: str
    generated_records: int
    validation_records: int

    @property
    def outcome(self) -> StatementOutcome:
        return GlueSessionTestRunner._observe(self.stage, self.statement)

    @property
    def expects_failure(self) -> bool:
        """Whether §4.5's table maps this observation onto a failure.

        Derived from the two things R5.5 permits — the reached state and the
        reported output status — and from nothing else. This is the property's
        own oracle, written from the requirement rather than delegated to
        ``StatementOutcome.failed``, so that the two agreeing is a result
        instead of a tautology.
        """
        return self.state != _STATE_AVAILABLE or self.status != "ok"

    @property
    def expected_reason(self) -> str:
        """The exception text ``error_details`` must carry, per R5.3."""
        if self.reported_name and self.reported_value:
            return f"{self.reported_name}: {self.reported_value}"
        return self.reported_name or self.reported_value

    def with_traceback(self, traceback: object) -> "Observation":
        """The same observation with a different ``Traceback`` reported.

        Used only by the invariance property. The state, status, error name and
        error value are all held fixed, so anything that moves moved because of
        the traceback.

        A statement reporting no ``Output`` at all has nowhere to carry a
        traceback, so the swap is a no-op and that example proves nothing —
        stated rather than hidden. It is roughly a fifth of the draw; the rest
        do vary, and a probe against a simulated traceback-sniffing classifier
        measured its verdict moving on 198 of 1200 examples, which is what makes
        the invariance assertion below a falsifiable one rather than a
        restatement.
        """
        output = dict(self.statement.get("Output") or {})
        if not output:
            return self
        if isinstance(traceback, _Absent):
            output.pop("Traceback", None)
        else:
            output["Traceback"] = traceback
        return Observation(**{**self.__dict__, "statement": {**self.statement, "Output": output}})


@st.composite
def _observations(draw) -> Observation:
    """One Glue observation from the full cross product §4.5's table covers.

    Composed rather than assembled from independent draws because the axes are
    not independent: a statement that never reached ``AVAILABLE`` reports no
    ``Output`` at all, so its status and exception axes have nowhere to live,
    and drawing them anyway would generate shapes Glue cannot produce.
    """
    stage = draw(st.sampled_from(STAGES))

    # AVAILABLE is drawn about half the time rather than one-in-five, and the
    # weighting is measured rather than aesthetic: three of the four axes the
    # task names — output status, exception name/value, traceback shape — exist
    # ONLY on an AVAILABLE statement, because Glue reports no `Output` on one
    # that stopped. An even draw over the five states spent 82% of the budget on
    # observations with nothing left to vary (266 of 1500 reached AVAILABLE, so
    # a traceback shape was exercised 51 times), which a coverage probe measured
    # before this branch was added.
    if draw(st.booleans()):
        state = _STATE_AVAILABLE
    else:
        state = draw(st.sampled_from(tuple(s for s in _ALL_STATES if s != _STATE_AVAILABLE)))

    seed = draw(st.integers(min_value=0, max_value=64))

    name = draw(st.sampled_from(_ERROR_NAMES))
    value = draw(st.sampled_from(_ERROR_VALUES))
    traceback_shape = draw(st.sampled_from(_TRACEBACK_SHAPES))
    validation = draw(st.sampled_from((_VALIDATION_CLEAN, _VALIDATION_ERROR, _VALIDATION_ABSENT)))
    validation_error = draw(st.sampled_from(("df_transformed is not defined", "validation raised")))
    generated_records = draw(st.integers(min_value=0, max_value=1000))
    validation_records = draw(st.integers(min_value=0, max_value=1000))

    stdout_parts = [f'GENERATED:{{"record_count": {generated_records}}}']
    payload = _validation_payload(validation, validation_records, validation_error)
    if payload:
        stdout_parts.append(payload)
    if draw(st.booleans()):
        # R5.5's other direction: a header in the PRINTED output of a statement
        # Glue called `ok`. Still a success, because the status says so.
        stdout_parts.append(_STDOUT_HEADER_NOISE)
    stdout = "\n".join(stdout_parts)

    statement: dict[str, Any] = {"State": state}

    if state == _STATE_AVAILABLE:
        status = draw(st.sampled_from(_OUTPUT_STATUSES))
        output: dict[str, Any] = {"Data": {"TextPlain": stdout}}
        if not isinstance(status, _Absent):
            output["Status"] = status
        if not isinstance(name, _Absent):
            output["ErrorName"] = name
        if not isinstance(value, _Absent):
            output["ErrorValue"] = value
        rendered = _render_frames(traceback_shape, seed)
        if not isinstance(rendered, _Absent):
            output["Traceback"] = rendered
        statement["Output"] = output
        # `_observe` reads no Status key as None, and the mapping's "not ok"
        # branch is what must catch it. Recorded as "" here so the derived
        # oracle above compares against something.
        effective_status = "" if isinstance(status, _Absent) else str(status)
    else:
        # A statement that stopped without reaching AVAILABLE. Glue reports no
        # `Output.Status` on either CANCELLED or ERROR, but it may have printed
        # something before it stopped — which is the "last output" design §4.5
        # asks `stderr` to carry (R5.6). Modelled as an `Output` with data and
        # no status, because that is the only way `_observe` can see stdout.
        if draw(st.booleans()):
            statement["Output"] = {"Data": {"TextPlain": stdout}}
        effective_status = ""
        name = _FIELD_ABSENT
        value = _FIELD_ABSENT
        traceback_shape = "absent"

    return Observation(
        stage=stage,
        statement=statement,
        state=state,
        status=effective_status,
        reported_name=_reported(name),
        reported_value=_reported(value),
        traceback_shape=traceback_shape,
        validation=validation,
        validation_error=validation_error,
        generated_records=generated_records,
        validation_records=validation_records,
    )


@st.composite
def _clean_runs(draw) -> tuple[list[StatementOutcome], int, int, bool]:
    """Three ``ok`` statements, with the markers distributed across them.

    The live shape: ``GENERATED:`` comes out of the frame-build stage and
    ``VALIDATION:`` out of the validation stage (design §4.1 runs three
    statements so a failure is attributable to a stage), so the combined stdout
    ``success_results`` reads is a join across stages rather than one string.
    Which stage carries which marker is generated, because a mapping that only
    worked when both landed on one statement would still pass a single-statement
    test.

    Returns the outcomes, the two record counts, and whether a Python traceback
    header was printed into a stage's stdout.
    """
    generated_records = draw(st.integers(min_value=0, max_value=1000))
    validation_records = draw(st.integers(min_value=0, max_value=1000))
    generated_stage = draw(st.integers(min_value=0, max_value=2))
    validation_stage = draw(st.integers(min_value=0, max_value=2))
    header_noise = draw(st.booleans())
    noise_stage = draw(st.integers(min_value=0, max_value=2))

    stdouts = ["", "", ""]
    stdouts[generated_stage] = f'GENERATED:{{"record_count": {generated_records}}}'
    payload = _validation_payload(_VALIDATION_CLEAN, validation_records, "")
    stdouts[validation_stage] = "\n".join(part for part in (stdouts[validation_stage], payload) if part)
    if header_noise:
        stdouts[noise_stage] = "\n".join(part for part in (stdouts[noise_stage], _STDOUT_HEADER_NOISE) if part)

    outcomes = [
        StatementOutcome(
            stage=stage,
            state=_STATE_AVAILABLE,
            output_status="ok",
            stdout=stdout,
            statement={"State": _STATE_AVAILABLE, "Output": {"Status": "ok", "Data": {"TextPlain": stdout}}},
        )
        for stage, stdout in zip(STAGES, stdouts, strict=True)
    ]
    return outcomes, generated_records, validation_records, header_noise


# ── The CreateSession axis — R8.5 ─────────────────────────────────────
#
# NOT including `ResourceNumberLimitExceededException`: R8.5 is scoped to a
# creation failure "for a reason other than a resource number limit", and
# routing capacity to a harness non-verification is task 3.6's. See the module
# docstring.
_CREATE_FAILURE_CODES = (
    "AccessDeniedException",
    "InvalidInputException",
    "ValidationException",
    "IllegalSessionStateException",
    "AlreadyExistsException",
    "ThrottlingException",
    "InternalServiceException",
)


@st.composite
def _creation_failures(draw) -> tuple[Exception, tuple[str, ...]]:
    """An exception ``CreateSession`` can raise, and the text R8.5 must carry.

    Both a modelled boto3 error and a bare exception, because ``_error_code``
    reads ``exc.response`` rather than testing ``isinstance(exc, ClientError)``
    and the non-``ClientError`` branch has to report just as honestly.
    """
    if draw(st.booleans()):
        code = draw(st.sampled_from(_CREATE_FAILURE_CODES))
        message = draw(st.sampled_from(("not authorized to perform iam:PassRole", "bad role", "session limit")))
        return ClientError({"Error": {"Code": code, "Message": message}}, "CreateSession"), (code, message)
    message = draw(st.sampled_from(("connection reset", "endpoint unreachable", "boom")))
    return RuntimeError(message), (message,)


@st.composite
def _lifecycle_failures(draw) -> tuple[GlueSessionError, str]:
    """A ``GlueSessionError`` the lifecycle raises, and the state it names.

    R5.6's session branch. The messages are the lifecycle's own — the session
    state one is built from ``_SESSION_STATES_TERMINAL_BAD`` so a state added to
    that frozenset later is generated here without this file being touched.
    """
    state = draw(st.sampled_from(sorted(_SESSION_STATES_TERMINAL_BAD)))
    session_id = "adi-verify-0123456789ab"
    if draw(st.booleans()):
        return GlueSessionError(f"Glue session {session_id} reached {state} before READY"), state
    return (
        GlueSessionError(f"Glue session {session_id} did not reach READY within 180s (last state {state})"),
        state,
    )


# ---------------------------------------------------------------------------
# The generator's own premise — R5.5 by construction
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(obs=_observations())
def test_the_generator_emits_only_header_free_tracebacks(obs: Observation):
    """The task's instruction, asserted rather than assumed.

    "Header-free tracebacks in the generator so 5.5's trap is covered by
    construction" is only true if the construction holds. If a template ever
    grows a header line, every R5.5 assertion below silently stops testing the
    trap — so the premise is checked on the same generated values the
    properties consume.

    Validates: Requirement 5.5
    """
    output = obs.statement.get("Output") or {}
    frames = output.get("Traceback", [])
    if isinstance(frames, str):
        frames = [frames]

    for frame in frames:
        assert _TRACEBACK_MARKER not in str(frame), f"generator emitted a header-bearing frame: {frame!r}"


# ---------------------------------------------------------------------------
# R1.5 — a successful execution
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(run=_clean_runs())
def test_a_clean_run_reports_success_executed_and_a_zero_exit_code(run):
    """R1.5: ``execution_successful`` true, ``executed`` true, ``exit_code`` zero.

    Holds however the markers were distributed across the three stages, and
    holds when one of those stages printed a Python traceback header into its
    output — because classification comes from the reported output status, and
    every statement here reported ``ok`` (R5.5).

    Validates: Requirements 1.5, 5.5
    """
    outcomes, generated_records, validation_records, header_noise = run

    result = success_results(outcomes)

    assert result["execution_successful"] is True
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert result["error_details"] is None
    assert result["input_records"] == generated_records
    assert result["output_records"] == validation_records
    assert _violations(result, SUCCESS) == [], f"header_noise={header_noise}"


@settings(max_examples=300, deadline=None)
@given(run=_clean_runs())
def test_a_clean_run_is_a_success_even_with_a_traceback_header_in_its_output(run):
    """R5.5, the direction the header-free generator cannot reach.

    Glue reports no stderr stream, so the only place a Python traceback header
    can appear on a statement Glue called ``ok`` is its printed output. A runner
    that sniffed stdout for the header — the shape ``_has_traceback`` has on the
    pandas path, where it exists because the interpreter sometimes exits 0 after
    printing a subprocess traceback — would fail this run. Reading
    ``Output.Status`` does not.

    Validates: Requirement 5.5
    """
    outcomes, _generated, _validated, header_noise = run
    if not header_noise:
        return

    combined = "\n".join(outcome.stdout for outcome in outcomes if outcome.stdout)
    assert _TRACEBACK_MARKER in combined, "guard: this example carries no header, so it tests nothing"

    assert success_results(outcomes)["execution_successful"] is True


# ---------------------------------------------------------------------------
# R5.1, R5.2, R5.3 — an error output status
# ---------------------------------------------------------------------------


@settings(max_examples=500, deadline=None)
@given(obs=_observations())
def test_a_failed_observation_reports_executed_not_successful_and_a_non_zero_exit(obs: Observation):
    """R5.1 and R5.2, over every observation §4.5 maps onto a failure.

    Includes every non-``ok`` status, not just the literal ``"error"``: a status
    of ``"OK"``, ``""`` or an absent ``Status`` key must not be read as success,
    because ``StatementOutcome.failed`` is written as "anything other than
    ``ok``" rather than as an enumeration of bad statuses.

    ``executed`` is true on this whole branch and that is the point of the
    distinction from the ``CreateSession`` row: the transform reached Spark, so
    the observation is about the transform.

    Validates: Requirements 5.1, 5.2
    """
    if not obs.expects_failure:
        return

    outcome = obs.outcome
    assert outcome.failed is True, f"the runner disagrees with §4.5's table for {obs.state}/{obs.status!r}"

    result = statement_failure_results(outcome)

    assert result["executed"] is True
    assert result["execution_successful"] is False
    assert isinstance(result["exit_code"], int)
    assert result["exit_code"] != 0
    assert _violations(result, STATEMENT_FAILURE) == []


@settings(max_examples=500, deadline=None)
@given(obs=_observations())
def test_an_error_output_names_the_reported_exception_and_its_value(obs: Observation):
    """R5.3: ``error_details`` contains the reported exception name and value.

    Quantified over the degenerate cases too. Where Glue reported only one of
    the two, that one appears; where it reported neither — absent keys, ``null``
    values, or whitespace — nothing renders an empty detail, so the observed
    status is named instead. Whitespace is stripped, which is the one
    normalisation the mapping documents.

    The containment holds regardless of traceback length: the reason is
    interpolated into ``error_details`` before the stderr block and is never
    truncated (``glue_session_runner.py:838``). The bound itself is task 3.5's.

    Validates: Requirement 5.3
    """
    if obs.state != _STATE_AVAILABLE or obs.status == "ok":
        return

    details = statement_failure_results(obs.outcome)["error_details"]

    assert details.startswith(f"{obs.stage} failed: "), f"stage attribution missing from {details!r}"

    if obs.expected_reason:
        if obs.reported_name:
            assert obs.reported_name in details
        if obs.reported_value:
            assert obs.reported_value in details
    else:
        # Nothing reported ⇒ name what WAS seen, so no detail is vacuous.
        assert obs.status in details or repr(obs.status) in details or "output status" in details
        assert details.strip() != f"{obs.stage} failed:"


# ---------------------------------------------------------------------------
# R5.5 — classification is traceback-independent
# ---------------------------------------------------------------------------


@settings(max_examples=400, deadline=None)
@given(obs=_observations(), swap=st.integers(min_value=0, max_value=len(_HEADER_BEARING_TRACEBACKS)))
def test_classification_is_invariant_under_the_traceback_reported(obs: Observation, swap: int):
    """R5.5: the verdict does not move when only the traceback moves.

    THE TRAP, stated as an invariance rather than as a single example. One
    observation's ``Traceback`` is replaced — with a header-bearing one, or with
    none at all — while its state, status, name and value are held fixed. If
    classification read traceback text, the two would disagree; because it reads
    ``Output.Status``, they cannot.

    Only ``stderr`` and the stderr block appended to ``error_details`` may
    differ. The verdict fields and the reason may not.

    Validates: Requirement 5.5
    """
    replacement: object = _FIELD_ABSENT if swap == len(_HEADER_BEARING_TRACEBACKS) else _HEADER_BEARING_TRACEBACKS[swap]
    variant = obs.with_traceback(replacement)

    original = obs.outcome
    mutated = variant.outcome

    assert original.failed is mutated.failed, (
        f"classification moved with the traceback: {original.failed} → {mutated.failed} "
        f"for state={obs.state} status={obs.status!r}"
    )

    if not original.failed:
        return

    first = statement_failure_results(original)
    second = statement_failure_results(mutated)

    for key in ("execution_successful", "executed", "exit_code"):
        assert first[key] == second[key], f"{key} moved with the traceback ({obs.state}/{obs.status!r})"

    # `error_details` is "{stage} failed: {reason}" plus an optional stderr
    # block; the reason is traceback-independent, the block is not.
    assert first["error_details"].split("\nstderr:\n")[0] == second["error_details"].split("\nstderr:\n")[0]


# ---------------------------------------------------------------------------
# R5.6 — a cancelled statement, or an errored session
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(obs=_observations())
def test_a_statement_that_stopped_names_the_reached_state(obs: Observation):
    """R5.6, statement branch: the reached state is named in ``error_details``.

    Generated over the unmodelled states as well as ``CANCELLED`` and ``ERROR``,
    because the mapping's else-branch is reached by "not ``AVAILABLE``" rather
    than by an enumeration — so a state a future API version adds lands here and
    must still name itself.

    Validates: Requirement 5.6
    """
    if obs.state == _STATE_AVAILABLE:
        return

    result = statement_failure_results(obs.outcome)

    assert result["execution_successful"] is False
    assert result["executed"] is True
    assert result["exit_code"] != 0
    if obs.state:
        assert obs.state in result["error_details"], f"state {obs.state!r} not named in {result['error_details']!r}"
        assert obs.state in result["stderr"]
    assert _violations(result, STATEMENT_FAILURE) == []


@settings(max_examples=200, deadline=None)
@given(failure=_lifecycle_failures())
def test_a_session_that_errored_names_the_reached_state(failure):
    """R5.6, session branch: a session state that ends the run is named.

    The lifecycle's own exception text carries the state, so the requirement is
    met by the message rather than by re-deriving it in the builder — which is
    why this asserts on ``error_details`` rather than on a field.

    Validates: Requirement 5.6
    """
    exc, state = failure

    result = lifecycle_failure_results(exc)

    assert result["execution_successful"] is False
    assert result["executed"] is False
    assert result["exit_code"] is None
    assert state in result["error_details"]
    assert _violations(result, CLIENT_UNAVAILABLE) == []


# ---------------------------------------------------------------------------
# R8.5 — CreateSession failed for a reason other than capacity
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(failure=_creation_failures())
def test_a_non_capacity_creation_failure_reports_nothing_executed(failure):
    """R8.5: not successful, not executed, with the reported error carried.

    ``executed`` false is the whole distinction from the statement rows: nothing
    reached Spark, so nothing was observed about the transform. The reported
    error must survive rather than be paraphrased — ``str(exc)`` already names
    the error code, and swapping it for harness prose is the kind of re-labelling
    ``LGR-b2f79ef-13`` argues against.

    Validates: Requirement 8.5
    """
    exc, expected_fragments = failure

    result = session_creation_failure_results(exc)

    assert result["execution_successful"] is False
    assert result["executed"] is False
    assert result["exit_code"] is None
    for fragment in expected_fragments:
        assert fragment in result["error_details"], f"{fragment!r} lost from {result['error_details']!r}"
    assert _violations(result, CLIENT_UNAVAILABLE) == []


# ---------------------------------------------------------------------------
# R5.7 — the absolute
# ---------------------------------------------------------------------------


def _every_mapping(obs: Observation) -> dict[str, dict]:
    """Every builder applied to *obs*, routed and misrouted alike.

    The builders are total functions over their argument types, so a misroute is
    a legal input and R5.7 has to hold there too. This is what turns
    ``glue_session_runner.py:783-787``'s "by construction" claim into something
    checked: ``success_results`` is handed a FAILED statement here, which is
    precisely the misrouted call that docstring reasons about.
    """
    outcome = obs.outcome
    return {
        "routed": statement_failure_results(outcome) if outcome.failed else success_results([outcome]),
        "success_results": success_results([outcome]),
        "statement_failure_results": statement_failure_results(outcome),
        "not_executed_results": not_executed_results(f"{obs.stage}: nothing ran"),
        "session_creation_failure_results": session_creation_failure_results(RuntimeError(obs.state or "no state")),
        "lifecycle_failure_results": lifecycle_failure_results(GlueSessionError(obs.state or "no state")),
    }


@settings(max_examples=400, deadline=None)
@given(obs=_observations())
def test_no_builder_reports_success_with_a_non_zero_exit_code(obs: Observation):
    """R5.7, an absolute over every generated input and every builder.

    "THE Glue_Session_Runner SHALL report no result in which
    ``execution_successful`` is true and ``exit_code`` is non-zero." Checked
    rather than taken on trust: the module claims this holds by construction
    because one builder owns both fields, and this is the test that notices if
    that stops being true.

    Validates: Requirement 5.7
    """
    for label, result in _every_mapping(obs).items():
        if result["execution_successful"]:
            assert result["exit_code"] == 0, (
                f"{label} reported success with exit_code {result['exit_code']!r} for {obs.state}/{obs.status!r}"
            )


@settings(max_examples=400, deadline=None)
@given(obs=_observations())
def test_every_builder_returns_exactly_the_nine_contract_keys(obs: Observation):
    """R1.1's key set, over the whole cross product.

    Underwrites every assertion above: a result missing a key would satisfy some
    of them vacuously. Also the guard that no builder grows a non-verification
    marker, which R3.2 forbids this runner from ever claiming and which task 3.6
    must add as a *reason* rather than as a field.

    Validates: Requirements 5.1, 5.2, 5.3, 5.6, 8.5
    """
    for label, result in _every_mapping(obs).items():
        assert set(result) == BASE_CONTRACT_KEYS, f"{label} returned {sorted(set(result) ^ BASE_CONTRACT_KEYS)}"
        assert result["conversion_report"] is None, f"{label} pre-empted task 8.1's conversion_report decision"


@settings(max_examples=400, deadline=None)
@given(obs=_observations())
def test_the_routed_result_matches_the_shape_the_contract_declares(obs: Observation):
    """Each row of §4.5's table lands on the declared outcome shape.

    The expected shape is derived from the generated axes — a failed observation,
    or a clean run whose validation payload reported an error, carries a string
    ``error_details``; anything else carries ``None`` — rather than read off the
    result, so this cannot pass by agreeing with itself.

    Validates: Requirements 1.5, 5.1, 5.2, 5.3, 5.6
    """
    outcome = obs.outcome
    if outcome.failed:
        expected = STATEMENT_FAILURE
        result = statement_failure_results(outcome)
    else:
        expected = STATEMENT_FAILURE if obs.validation == _VALIDATION_ERROR else SUCCESS
        result = success_results([outcome])

    assert _violations(result, expected) == [], f"{obs.state}/{obs.status!r} validation={obs.validation}"
