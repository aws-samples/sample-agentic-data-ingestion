# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 3 — the validation marker round-trips through the production parser.

Spec ``real-spark-verification`` task 6.2. **Validates: Requirement 1.3.**

    THE Glue_Session_Runner SHALL emit the ``GENERATED:`` and ``VALIDATION:``
    stdout markers in the form ``_parse_results`` reads, so that result parsing
    is shared rather than duplicated.

Design §2 states why this is the cheapest true claim in the whole feature:

    Emitting the same two markers is sufficient to reuse ``_parse_results``
    verbatim. That is the reason the change is a change of executor rather than
    of contract.

``test_validation_statement.py::TestTheMarkersRoundTripThroughTheProductionParser``
establishes the example-level baseline over task 1.1's five hand-built frames.
What follows generalises it over **generated payload values and generated column
names**, which is task 6.2's instruction, and it generates **braces in column
names** so that the parser's brace-depth scanner is the thing under test rather
than an assumption.

Production code is **not** modified by this task. Design §3 do-NOT 8 forbids
touching ``_parse_results`` or the marker protocol, which is exactly what makes
the parser reusable; ``pyspark`` and ``pandas`` are not installed and must not be
(design §3 do-NOT 7, ``LGR-4dd9b42-1`` — template / ratified).

════════════════════════════════════════════════════════════════════════
WHAT ROUND-TRIP MEANS HERE, AND WHAT THE ORACLE IS
════════════════════════════════════════════════════════════════════════

The claim is about the **parser**, not about the metrics. R1.3 asks that the
markers be emitted *in the form the parser reads*, so the oracle is the payload
that was actually emitted, and the property is that the production parser carries
those values into the contract unchanged. Task 6.4 owns whether the numbers are
*right* (R10.2's definitions); task 6.3 owns whether the payload's KEY SET is
executor-independent (R10.1). Neither is re-asserted here.

The oracle is read by a **line-based** extractor, :func:`_emitted_payload`, and
that choice is load-bearing rather than stylistic. Both builders emit their
payload with a single ``print``, so the JSON is exactly the rest of the marker
line — no scanning required. Reading it that way makes the oracle **immune to the
very brace-depth defect under test**, so a parser that mis-slices is detected
instead of being agreed with.

Using ``_extract_json``'s own algorithm as the oracle would have made every arm
below vacuously true, and that is the failure ``LGR-28b4d75-5`` (template /
ratified) names in one sentence: *a test written from the producer's output cannot
detect the producer drifting, because the output is its own oracle.* Here the
producer is the parser and the temptation was to read the payload with the
parser's own scan.

:func:`_first_balanced_slice` does re-implement that algorithm, and is used for
one narrow purpose: to **predict** which arm an example falls into. It is never
an oracle for a value. Its agreement with the production parser is itself what
:func:`test_the_parser_round_trips_or_fails_exactly_as_the_brace_scan_predicts`
asserts, so a divergence between the two fails rather than hides.

════════════════════════════════════════════════════════════════════════
THE STUB AND THE FRAMES ARE TASK 6.1'S, IMPORTED — NOT RESTATED
════════════════════════════════════════════════════════════════════════

``_StubFrame``, ``_stub_frame``, ``_install_pyspark_functions``, ``_execute`` and
``_target_schema`` come from ``test_validation_statement.py``, whose docstring
records that tasks 6.2/6.3/6.4 generalise from it. A second stub written here
would be a second encoding of Spark's documented semantics — ``isNull()`` false
for a float ``NaN``, ``count(DISTINCT …)`` skipping nulls, ``NaN`` normalised in a
distinct key — and two encodings by one hand can agree with each other while both
being wrong about Spark, so the divergence would be invisible in exactly the case
it matters. One stub, one place to correct when real Spark contradicts it.

What that stub does and does not prove is stated in full in 6.1's module
docstring and is not restated: no Spark runs in this repository.

════════════════════════════════════════════════════════════════════════
THE BRACE LIMITATION: PRE-EXISTING, PINNED, AND NOT REPAIRABLE HERE
════════════════════════════════════════════════════════════════════════

``_extract_json`` counts braces without tracking string literals
(``code_interpreter_runner.py:599-614``, read end to end). A column name's braces
therefore perturb the scan, and there are exactly **three** outcomes, all three
of which this module generates and pins:

1. **The scan closes where the payload ends** — the value round-trips. A name
   with no braces, and a name whose braces are *properly nested* such as
   ``a{b}``, both land here: the depth trace is perturbed upward only and returns
   to the same value, so the closing point does not move.
2. **The scan closes early** — the slice handed to ``json.loads`` is cut either
   mid-string or at an inner ``}``, leaving the outer object unterminated either
   way, so it raises ``JSONDecodeError``, which propagates out of
   ``_parse_results``. Task 6.1's ``c}d`` example is this arm.
3. **The scan never closes** — an unmatched ``{`` in a name raises the depth so
   the object never returns to zero, ``_extract_json`` returns ``None``, and the
   run is reported as ``execution_successful: False`` with **no error text at
   all**. This arm was not previously recorded and is the quieter of the two
   failures: arm 2 raises, arm 3 loses the payload silently.

Arm 3 is reported to the spec as a finding about the existing parser, not as a
defect of this task's code and not as something to fix — design §3 do-NOT 8 is
explicit, and the exposure is **executor-independent**: both builders print
``"columns": list(df.columns)`` and key ``null_counts`` by column name, so the
pandas path mis-parses identically on the same names.
:func:`test_the_brace_outcome_is_the_same_for_the_pandas_shaped_marker_line`
generalises 6.1's single hand-built demonstration of that over generated names,
and needs no pandas to do it.

Column names carrying ``"`` and ``\\`` are generated deliberately and are
expected to round-trip: neither builder interpolates a column name into its
emitted source — the names arrive at run time off ``df.columns`` — and
``json.dumps`` escapes both, while the brace scanner does not track quoting at
all, so quoting cannot move the closing point.

**Coverage, measured by instrumenting this module's own generators rather than
asserted in prose** (the discipline ``test_converter_refusal_orthogonality_prop
.py`` and ``test_not_verified_advisory_prop.py`` both follow). At the settings
below:

* ``_properly_nested_brace_names`` — 200/200 examples land ``(intact, intact)``
  and **502 of 502** generated names carry a brace, so the safe-brace arm is
  exercised on every example rather than incidentally.
* ``_safe_names`` (the headline property) — 200/200 intact, 394 of 541 names
  (72.8%) carrying a brace.
* ``_unbalanced_brace_names`` — 173 of 200 examples are non-intact on at least
  one marker, so the hazard arm is genuinely reached and not just requested.
* ``_any_names`` (the totality property) — all three arms on both markers, with
  the rarest single arm (``GENERATED:never_closed``) at 4 draws in 200 examples
  against 36 in 400. That ratio is why the totality property gets 400; see
  :data:`_TOTALITY_SETTINGS`.

``test_the_totality_property_exercised_all_three_arms_on_both_markers`` keeps that
last claim from rotting silently, and records which part of it is measured rather
than asserted.

════════════════════════════════════════════════════════════════════════
LEDGER AND STRUCTURE NOTES
════════════════════════════════════════════════════════════════════════

* ``rootmap``: 122 records @ ``4099d91``, all template-provenance. **No record
  governs the marker protocol or ``_parse_results``.** ``why`` on
  ``code_interpreter_runner.py:604`` returns one commit (``feb6a1db18``, the
  testing overhaul) carrying **no records at all**; ``why`` on
  ``glue_session_runner.py`` returns nothing, the file being uncommitted. Stated
  as a ledger gap, not as an all-clear.
* ``decisions(scope="…/agentic_data_ingestion_test_code")`` returns eight records;
  the ones that bear on this file are ``LGR-4dd9b42-1`` (neither runtime may be
  installed to check this) and ``LGR-b2f79ef-12`` (``null_counts``/``dtypes`` are
  per-column maps that reach the DynamoDB state row through *this* parser — which
  is why a name that defeats it is a data-path concern and not only cosmetic).
  ``decisions(concept="testing")`` returns five, none about marker parsing;
  ``LGR-3e45e65-1`` is the one touching this file's environment, so the
  ``setdefault`` calls below are consistent no-ops.
* Blast radius, from ``codegraph explore``: ``build_validation_code`` 17
  references, ``build_generated_marker_code`` 10, both inside
  ``glue_session_runner.py``; the only covering test file for either is
  ``test_validation_statement.py``, which this module joins.
  ``_parse_results``/``_build_validation_code`` carry 18 references and five
  covering test files. Nothing in ``shared/`` is touched, so there is no
  Lambda-layer blast radius; per ``.kiro/steering/test-mapping.md`` the covering
  directory for this Lambda is ``tests/unit/lambdas/test_code/``, which is where
  this file lives.
"""

import json
import os
import sys
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, assume, given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from code_interpreter_runner import CodeInterpreterTestRunner
from glue_session_runner import (
    FRAME_VARIABLE,
    VALIDATION_PAYLOAD_KEYS,
    build_generated_marker_code,
    build_validation_code,
)
from tests.unit.lambdas.test_code.test_dq_definitions_characterization import Frame
from tests.unit.lambdas.test_code.test_validation_statement import (
    _execute,
    _install_pyspark_functions,
    _stub_frame,
    _StubFrame,
    _target_schema,
)

_GENERATED = "GENERATED"
_VALIDATION = "VALIDATION"

#: The seven keys ``_parse_results`` builds ``data_quality_results`` from, paired
#: with the payload key each is sourced from (``code_interpreter_runner.py:
#: 626-634``). Written out rather than derived, for the reason
#: ``_VALIDATION_PAYLOAD_EXPRESSIONS`` is: four of the seven are renamed on the
#: way through (``completeness`` -> ``completeness_score``), so a mapping
#: generated from either key set would be wrong.
_DQ_KEY_SOURCES: dict[str, str] = {
    "completeness_score": "completeness",
    "uniqueness_score": "uniqueness",
    "schema_compliance": "schema_compliance",
    "overall_quality_score": "overall_quality",
    "null_counts": "null_counts",
    "duplicates": "duplicates",
    "missing_columns": "missing_columns",
}


# ── Reading what was emitted, without scanning braces ─────────────────


def _emitted_payload_text(stdout: str, marker: str) -> str:
    """The raw JSON text printed after *marker*, read by LINE.

    Both builders emit their payload with a single ``print``, and ``json.dumps``
    escapes any newline inside a column name, so the JSON is exactly the rest of
    the marker's line. Reading it this way makes this oracle immune to the
    brace-depth defect under test — see the module docstring.
    """
    for line in stdout.splitlines():
        if line.startswith(f"{marker}:"):
            return line[len(marker) + 1 :]
    raise AssertionError(f"no {marker}: line in {stdout!r}")


def _emitted_payload(stdout: str, marker: str) -> dict:
    """The payload *as emitted*, which is this module's oracle."""
    return json.loads(_emitted_payload_text(stdout, marker))


def _first_balanced_slice(stdout: str, marker: str) -> str | None:
    """``_extract_json``'s brace scan, re-run to PREDICT which arm applies.

    Never an oracle for a value; see the module docstring. Returns the substring
    the production parser would hand to ``json.loads``, or ``None`` where the
    depth never returns to zero and the parser would give up.
    """
    index = stdout.find(f"{marker}:")
    assert index != -1, f"no {marker}: marker in {stdout!r}"
    start = stdout.index("{", index)
    depth = 0
    for position in range(start, len(stdout)):
        if stdout[position] == "{":
            depth += 1
        elif stdout[position] == "}":
            depth -= 1
            if depth == 0:
                return stdout[start : position + 1]
    return None


#: What the brace scan does to one marker: the payload survives intact, the slice
#: is cut short, or it is never closed at all. The three arms of the module
#: docstring's list, in order.
_INTACT, _CUT_SHORT, _NEVER_CLOSED = "intact", "cut_short", "never_closed"


def _brace_outcome(stdout: str, marker: str) -> str:
    sliced = _first_balanced_slice(stdout, marker)
    if sliced is None:
        return _NEVER_CLOSED
    return _INTACT if sliced == _emitted_payload_text(stdout, marker) else _CUT_SHORT


# ── Generation ────────────────────────────────────────────────────────

#: Deliberately small and deliberately awkward. ``"`` and ``\\`` are in it
#: because they are expected to round-trip (module docstring); ``.`` and a
#: backtick because they are what the emitted statement's quoting exists for; a
#: space and a non-ASCII letter because ``json.dumps`` handles both and the
#: scanner should not care. No newline: it would split the marker line the
#: line-based oracle relies on, and ``json.dumps`` escapes it anyway.
_NAME_ALPHABET = 'ab.`" \\é'


def _plain_fragment() -> st.SearchStrategy[str]:
    return st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=4)


def _brace_free_names() -> st.SearchStrategy[str]:
    return _plain_fragment()


def _properly_nested_brace_names() -> st.SearchStrategy[str]:
    """Names whose braces are properly nested — ``a{b}``, ``{a}``, ``a{b}c{d}``.

    The safe shape, and the reason it is safe: the scan's depth goes up and comes
    back to the same value across such a name, so the closing point does not
    move.
    """
    return st.one_of(
        st.tuples(_plain_fragment(), _plain_fragment()).map(lambda parts: f"{parts[0]}{{{parts[1]}}}"),
        _plain_fragment().map(lambda body: "{" + body + "}"),
        st.tuples(_plain_fragment(), _plain_fragment()).map(lambda parts: f"{{{parts[0]}}}{{{parts[1]}}}"),
    )


def _unbalanced_brace_names() -> st.SearchStrategy[str]:
    """Names whose braces do not properly nest — the hazardous shapes.

    Includes ``}``-first (which drives the depth down and cuts the slice short)
    and ``{``-only (which raises it and stops the object ever closing), plus a
    free brace soup so the two can also cancel each other out.
    """
    return st.one_of(
        _plain_fragment().map(lambda body: body + "}"),
        _plain_fragment().map(lambda body: "{" + body),
        st.tuples(_plain_fragment(), _plain_fragment()).map(lambda parts: f"}}{parts[0]}{{{parts[1]}"),
        st.text(alphabet="ab{}", min_size=1, max_size=5),
    )


def _safe_names() -> st.SearchStrategy[str]:
    return st.one_of(_brace_free_names(), _properly_nested_brace_names())


def _any_names() -> st.SearchStrategy[str]:
    return st.one_of(_brace_free_names(), _properly_nested_brace_names(), _unbalanced_brace_names())


@dataclass(frozen=True)
class Run:
    """One run's two frames and its declared target columns.

    The input and the output frame are **distinct**, which is what makes
    ``input_records`` and ``output_records`` separable: a real transform changes
    the frame, and binding one stub to both names would let a parser that
    confused the two markers still pass.
    """

    input_columns: tuple[str, ...]
    input_row_count: int
    output: Frame
    declared_targets: tuple[str, ...]

    @property
    def every_name(self) -> tuple[str, ...]:
        """Every column name that reaches a marker payload, from both frames."""
        return self.input_columns + self.output.columns

    def stdout(self, monkeypatch: pytest.MonkeyPatch) -> str:
        """The combined stdout of a whole run: frame-build marker, then validation."""
        namespace = {
            FRAME_VARIABLE: _StubFrame(
                column_names=list(self.input_columns),
                rows=[{} for _ in range(self.input_row_count)],
                spark_dtypes=[],
            ),
            "df_transformed": _stub_frame(self.output),
        }
        return _execute(
            build_generated_marker_code() + build_validation_code(self.target_schema),
            namespace,
            monkeypatch,
        )

    @property
    def target_schema(self) -> dict:
        return _target_schema(self.output)


#: Cell values. ``None`` is an absent cell and ``NaN`` a present one — the
#: distinction C7 turns on — and neither ever reaches the JSON, because the
#: payload carries counts and scores rather than cells. That is worth stating:
#: were a cell value emitted, a ``NaN`` would render as the invalid-JSON literal
#: ``NaN`` and this property would be measuring something else entirely.
_CELLS = st.one_of(
    st.none(),
    st.integers(min_value=-3, max_value=3),
    st.floats(allow_nan=True, allow_infinity=False, width=16),
    st.text(alphabet="xy", max_size=2),
    st.booleans(),
)


@st.composite
def _runs(draw: st.DrawFn, names: st.SearchStrategy[str]) -> Run:
    """A run whose every column name is drawn from *names*.

    Names are unique because the stub keys its rows by name, so a duplicate would
    silently collapse a column rather than test anything. Widths start at zero:
    an empty output frame exercises the emitted statement's ``if _quoted`` arm and
    still has to round-trip.
    """
    output_columns = tuple(draw(st.lists(names, min_size=0, max_size=4, unique=True)))
    row_count = draw(st.integers(min_value=0, max_value=4))
    rows = tuple(tuple(draw(_CELLS) for _ in output_columns) for _ in range(row_count))

    # Declared targets mix present and absent columns, so `missing_columns` and
    # `schema_compliance` are sometimes a shortfall rather than always vacuous.
    declared = tuple(
        draw(
            st.lists(
                st.sampled_from(output_columns) | names if output_columns else names,
                min_size=0,
                max_size=4,
                unique=True,
            )
        )
    )

    return Run(
        input_columns=tuple(draw(st.lists(names, min_size=0, max_size=3, unique=True))),
        input_row_count=draw(st.integers(min_value=0, max_value=6)),
        output=Frame(name="generated", columns=output_columns, rows=rows, declared_targets=declared),
        declared_targets=declared,
    )


#: Each example compiles and executes the emitted statement, so examples are not
#: free; 200 covers the composed space many times over in a couple of seconds.
#: ``deadline=None`` because the first example pays for the import and the compile.
_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])

#: The totality property gets **400**, and the number is measured rather than
#: chosen. Its rarest single arm is an unmatched ``{`` in an *input* column name,
#: which makes the ``GENERATED`` scan run off the end of the text: instrumenting
#: this module's own generator drew it **4 times in 200** examples and **36 times
#: in 400**. Four draws is too thin to call an arm covered, so 200 was below the
#: floor for the claim this property makes — which is why this is a separate
#: setting rather than a copy of :data:`_SETTINGS`.
_TOTALITY_SETTINGS = settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])

#: Which arm each example landed in, recorded so the coverage claim above is
#: checkable rather than a sentence in a docstring. A totality property whose
#: generator drifted onto the intact arm would still pass every assertion while
#: proving nothing about the other two, which is the failure mode
#: ``test_the_totality_property_exercised_all_three_arms_on_both_markers`` closes.
_EXERCISED: Counter = Counter()


# ── A helper for the assertions every arm shares ──────────────────────


def _assert_round_trips(stdout: str) -> None:
    """The whole of R1.3's positive claim, in one place.

    Both markers reach the production parser and every value it reports is the
    value that was emitted: the two record counts, the four contract fields, and
    all seven ``data_quality_results`` keys with their four renames.
    """
    generated = _emitted_payload(stdout, _GENERATED)
    validation = _emitted_payload(stdout, _VALIDATION)
    parsed = glue_session_runner._parse_results(None, stdout)

    assert parsed["input_records"] == generated["record_count"]
    assert parsed["output_records"] == validation["record_count"]
    assert parsed["execution_successful"] is True
    assert parsed["error_details"] is None
    assert parsed["executed"] is True
    assert parsed["exit_code"] == 0

    assert parsed["data_quality_results"] == {
        contract_key: validation[payload_key] for contract_key, payload_key in _DQ_KEY_SOURCES.items()
    }


# ══════════════════════════════════════════════════════════════════════
# The reuse is real: the same function object, not a copy
# ══════════════════════════════════════════════════════════════════════


def test_the_parser_under_test_is_the_production_function_object():
    """Design §3 do-NOT 8, pinned by identity rather than by behaviour.

    Every property below would still pass against a faithful copy. This is what
    makes them statements about the *production* parser: a copy could drift, and
    R1.3's whole point is that parsing is shared rather than duplicated.
    """
    assert glue_session_runner._parse_results is CodeInterpreterTestRunner._parse_results


# ══════════════════════════════════════════════════════════════════════
# Property 3 — the round-trip
# ══════════════════════════════════════════════════════════════════════


@_SETTINGS
@given(run=_runs(_safe_names()))
def test_a_run_round_trips_into_the_contract_for_any_generated_values(run: Run):
    """**Property 3. Validates: Requirement 1.3.**

    For any generated payload values — over generated column names, including
    names whose braces properly nest — a run's ``GENERATED:`` and ``VALIDATION:``
    markers parse through the production parser into the contract carrying the
    values that were emitted.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        stdout = run.stdout(monkeypatch)
    _assert_round_trips(stdout)


@_SETTINGS
@given(run=_runs(_safe_names()))
def test_the_round_trip_reports_the_nine_emitted_keys_and_loses_only_dtypes(run: Run):
    """Which of the nine emitted keys survives into the contract, and which does not.

    Eight of :data:`VALIDATION_PAYLOAD_KEYS` reach the parsed result —
    ``record_count`` as ``output_records`` and seven under
    ``data_quality_results``. ``dtypes`` is emitted and **dropped**: the parser
    does not read it (``code_interpreter_runner.py:626-634``). Pinned because
    R1.3 asks for the marker *in the form the parser reads* rather than for the
    subset the parser happens to use, so the emitted-but-unread key is deliberate
    and a later reader should not "tidy" it away.

    The first assertion reads the emitted keys against **this module's own
    declared tuple**, as the precondition for the two that follow. It is not a
    comparison between executors — R10.1's key-set equality across the two paths
    is task 6.3's Property 12 and is not restated here.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        stdout = run.stdout(monkeypatch)
    emitted = _emitted_payload(stdout, _VALIDATION)
    assert tuple(emitted) == VALIDATION_PAYLOAD_KEYS

    parsed = glue_session_runner._parse_results(None, stdout)
    assert set(_DQ_KEY_SOURCES.values()) | {"record_count", "dtypes"} == set(VALIDATION_PAYLOAD_KEYS)
    assert "dtypes" not in parsed["data_quality_results"]
    assert "dtypes" not in parsed


@_SETTINGS
@given(run=_runs(_brace_free_names()))
def test_a_brace_free_run_always_round_trips(run: Run):
    """The floor: with no brace anywhere, the scan cannot be perturbed at all.

    Stated separately from the properly-nested case so that a regression in the
    brace handling cannot be mistaken for a regression in the round-trip itself.
    """
    assert not any("{" in name or "}" in name for name in run.every_name)
    with pytest.MonkeyPatch.context() as monkeypatch:
        stdout = run.stdout(monkeypatch)
    _assert_round_trips(stdout)


@_SETTINGS
@given(run=_runs(_properly_nested_brace_names()))
def test_properly_nested_braces_in_every_column_name_still_round_trip(run: Run):
    """The brace-depth scanner exercised on purpose, in the shape that is safe.

    Task 6.2's instruction is to generate braces; this is the arm where they must
    not matter. Every name here carries at least one brace pair, so the scanner's
    depth is genuinely perturbed on every example and returns to where it started.
    """
    assume(run.every_name)
    assert all("{" in name and "}" in name for name in run.every_name)
    with pytest.MonkeyPatch.context() as monkeypatch:
        stdout = run.stdout(monkeypatch)
    _assert_round_trips(stdout)


# ══════════════════════════════════════════════════════════════════════
# The pre-existing brace limitation, pinned over the whole input space
# ══════════════════════════════════════════════════════════════════════


@_TOTALITY_SETTINGS
@given(run=_runs(_any_names()))
def test_the_parser_round_trips_or_fails_exactly_as_the_brace_scan_predicts(run: Run):
    """Totality over arbitrary braces: three arms, and nothing outside them.

    **Not a fix.** ``_extract_json`` counts braces without tracking string
    literals (``code_interpreter_runner.py:599-614``) and design §3 do-NOT 8
    forbids changing it, so what is available is to pin the behaviour and say
    which arm applies. The prediction comes from
    :func:`_first_balanced_slice`, and asserting the production parser against it
    is what holds the two together — see the module docstring on why that is not
    circular.

    Arm 3 (``never_closed``) is the finding worth carrying out of this task: the
    payload is lost and the run reports ``execution_successful: False`` with
    ``error_details`` still ``None``, i.e. a failure with no stated reason. Arm 2
    at least raises.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        stdout = run.stdout(monkeypatch)

    outcomes = {marker: _brace_outcome(stdout, marker) for marker in (_GENERATED, _VALIDATION)}
    for marker, outcome in outcomes.items():
        _EXERCISED[f"{marker}:{outcome}"] += 1
    _EXERCISED[f"pair:{outcomes[_GENERATED]}/{outcomes[_VALIDATION]}"] += 1

    if _CUT_SHORT in outcomes.values():
        # A slice cut short ends either mid-string or at an inner `}` with the
        # outer object still open, so it is never valid JSON.
        with pytest.raises(json.JSONDecodeError):
            glue_session_runner._parse_results(None, stdout)
        return

    parsed = glue_session_runner._parse_results(None, stdout)

    if outcomes[_GENERATED] == _INTACT:
        assert parsed["input_records"] == _emitted_payload(stdout, _GENERATED)["record_count"]
    else:
        assert parsed["input_records"] == 0, "a payload the scan never closed is silently read as zero rows"

    if outcomes[_VALIDATION] == _INTACT:
        assert parsed["execution_successful"] is True
        if outcomes[_GENERATED] == _INTACT:
            _assert_round_trips(stdout)
    else:
        assert parsed["execution_successful"] is False
        assert parsed["data_quality_results"] == {}
        assert parsed["error_details"] is None, (
            "arm 3: the payload is lost and NO reason is reported — pre-existing, "
            "executor-independent, and not repairable under design §3 do-NOT 8"
        )


def test_the_totality_property_exercised_all_three_arms_on_both_markers():
    """Coverage guard — measured, not claimed in prose.

    The property above is total over its outcome space only if its generator
    actually reaches that space. What is asserted is all three arms on **each**
    marker, plus at least one example where the two markers disagree.

    **What is deliberately NOT asserted, and why.** An earlier version required
    all nine ``(GENERATED, VALIDATION)`` pairs. Measured across runs, eight appear
    reliably and ``(never_closed, cut_short)`` does not: it needs an unmatched
    ``{`` in an *input* name and an unmatched ``}`` in an *output* name in the same
    example, which landed 11 times in 400 on one run and zero on the next. A guard
    that depends on a one-in-four-hundred conjunction reports generator luck rather
    than generator coverage. The mixed pair that actually matters —
    ``(never_closed, intact)``, the run that reports success while losing its input
    count — is pinned deterministically by
    :func:`test_an_unmatched_open_brace_silently_zeroes_input_records` instead, so
    nothing is lost by relaxing this.

    Depends on the property having run in the same session, the same way
    ``test_converter_refusal_orthogonality_prop.py``'s guard does.
    """
    assert _EXERCISED, "the property did not run, so its coverage cannot be judged"

    for marker in (_GENERATED, _VALIDATION):
        for arm in (_INTACT, _CUT_SHORT, _NEVER_CLOSED):
            assert _EXERCISED[f"{marker}:{arm}"] > 0, f"{marker} never landed in {arm!r}: {dict(_EXERCISED)}"

    # At least one example where the two markers disagree, because the mixed
    # shapes are the ones that carry information: a run that reports success while
    # having lost its input count is a mixed pair and nothing else produces it.
    mixed = sum(
        count
        for key, count in _EXERCISED.items()
        if key.startswith("pair:") and len(set(key.removeprefix("pair:").split("/"))) > 1
    )
    assert mixed > 0, f"every example had both markers in the same arm: {dict(_EXERCISED)}"


@_SETTINGS
@given(run=_runs(_unbalanced_brace_names()))
def test_an_unbalanced_brace_loses_a_value_but_never_fabricates_one(run: Run):
    """The safety claim inside the limitation: no wrong number ever reaches the contract.

    An unbalanced brace is allowed to raise (arm 2) or to lose a payload
    (arm 3). What it must never do is report a value that **differs** from what
    was emitted — a wrong number would flow into the report and onto the
    pipeline-runs state row, and ``LGR-b2f79ef-12`` (template / ratified) is the
    record that puts ``null_counts`` there. A lost value degrades honestly; a
    fabricated one does not.

    **The two markers are scanned independently and are asserted independently.**
    That is not a formality: the first counterexample here was a run whose
    ``GENERATED`` marker fell into arm 3 while its ``VALIDATION`` marker stayed
    intact, so the run reported success *and* zero input records. The property as
    first written asserted a whole-run round-trip whenever the parse succeeded,
    which that shape falsifies — the assertion was too strong, not the parser
    newly wrong. :func:`test_an_unmatched_open_brace_silently_zeroes_input_records`
    pins the shape as an example.
    """
    assume(run.every_name)
    with pytest.MonkeyPatch.context() as monkeypatch:
        stdout = run.stdout(monkeypatch)

    try:
        parsed = glue_session_runner._parse_results(None, stdout)
    except json.JSONDecodeError:
        return  # arm 2: it raises, which is the loud and preferable failure

    emitted_input = _emitted_payload(stdout, _GENERATED)["record_count"]
    assert parsed["input_records"] in (emitted_input, 0), (
        "an input count is either carried through or lost, never changed"
    )

    if parsed["execution_successful"]:
        validation = _emitted_payload(stdout, _VALIDATION)
        assert parsed["output_records"] == validation["record_count"]
        assert parsed["data_quality_results"] == {
            contract_key: validation[payload_key] for contract_key, payload_key in _DQ_KEY_SOURCES.items()
        }
    else:
        assert parsed["data_quality_results"] == {}
    assert parsed["error_details"] is None


def test_an_unmatched_open_brace_silently_zeroes_input_records():
    """════ ARM 3 ON THE ``GENERATED:`` MARKER — A SHAPE 6.1 DID NOT RECORD ════

    An unmatched ``{`` in an **input** column name raises the scan's depth, so it
    never returns to zero before the text ends, ``_extract_json`` returns ``None``
    and ``input_records`` is reported as **0**. The ``VALIDATION:`` scan starts
    from its own marker and is unaffected, so the run still reports
    ``execution_successful: True``.

    The result is a run that claims success while saying the input frame had no
    rows. Task 6.1 recorded the unmatched-``}`` case, which raises; this one is
    quieter, and quieter is worse.

    **Pre-existing, executor-independent, and not repairable here.** The line
    below is hand-built in the shape *both* builders emit — ``"columns":
    list(df.columns)`` — so no executor is implicated and no runtime is needed to
    show it. Design §3 do-NOT 8 forbids changing ``_parse_results`` or the marker
    protocol, so this is pinned as behaviour and reported as a finding.
    """
    stdout = (
        f'{_GENERATED}:{{"record_count": 7, "columns": ["a{{b"]}}\n'
        f'{_VALIDATION}:{{"record_count": 2, "null_counts": {{}}, "dtypes": {{}}, "duplicates": 0, '
        f'"schema_compliance": 100.0, "completeness": 100.0, "uniqueness": 100.0, '
        f'"overall_quality": 100.0, "missing_columns": []}}\n'
    )
    # The emitted line does say 7, so the zero below is a loss and not a mis-emit.
    assert _emitted_payload(stdout, _GENERATED)["record_count"] == 7
    assert _brace_outcome(stdout, _GENERATED) == _NEVER_CLOSED
    assert _brace_outcome(stdout, _VALIDATION) == _INTACT

    parsed = glue_session_runner._parse_results(None, stdout)
    assert parsed["input_records"] == 0, "the input count is lost, with nothing recording that it was"
    assert parsed["execution_successful"] is True, "and the run still reports success"
    assert parsed["output_records"] == 2

    # The matching unmatched-`}` shape raises instead — task 6.1's arm, restated
    # here only to keep the asymmetry between the two visible in one place.
    with pytest.raises(json.JSONDecodeError):
        glue_session_runner._parse_results(None, f'{_VALIDATION}:{{"record_count": 2, "null_counts": {{"c}}d": 0}}}}')


@_SETTINGS
@given(run=_runs(_any_names()))
def test_the_brace_outcome_is_the_same_for_the_pandas_shaped_marker_line(run: Run):
    """R1.3's exposure is the PARSER's, so it is shared with the pandas path.

    Both builders print ``"columns": list(df.columns)`` and key ``null_counts``
    and ``dtypes`` by column name, so the same names produce the same brace
    trace. Checked by hand-building the pandas path's emitted *shape* — the nine
    keys keyed the same way — which needs no pandas, the same technique task 6.1
    used for its single example.

    Task 6.3 owns the key-set equality itself (R10.1, Property 12); what is
    asserted here is only that the parser's outcome does not depend on which
    executor emitted the line.
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        spark_stdout = run.stdout(monkeypatch)

    spark_payload = _emitted_payload(spark_stdout, _VALIDATION)
    pandas_shaped = (
        f"{_GENERATED}:"
        + json.dumps({"record_count": run.input_row_count, "columns": list(run.input_columns)})
        + "\n"
        + f"{_VALIDATION}:"
        + json.dumps({key: spark_payload[key] for key in VALIDATION_PAYLOAD_KEYS})
        + "\n"
    )

    for marker in (_GENERATED, _VALIDATION):
        assert _brace_outcome(pandas_shaped, marker) == _brace_outcome(spark_stdout, marker), (
            f"the {marker} brace outcome differs between executors for {run.every_name!r}, "
            "which would make the limitation executor-dependent"
        )


# ══════════════════════════════════════════════════════════════════════
# The df_transformed-absent arm
# ══════════════════════════════════════════════════════════════════════


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(run=_runs(_safe_names()))
def test_the_absent_transform_frame_arm_parses_as_a_failure_naming_its_reason(run: Run):
    """**Property 3, the error arm. Validates: Requirement 1.3.**

    A frame was built — so ``GENERATED:`` was printed and must still round-trip —
    and the transform then failed to bind ``df_transformed``. The emitted
    ``VALIDATION:{"error": …}`` must parse to ``execution_successful`` false with
    the text in ``error_details`` and no data-quality payload.

    The realistic composition, not the validation statement alone: on the live
    path the frame-build statement has already run and succeeded (design §4.1's
    three statements), so ``input_records`` being preserved across a validation
    failure is part of what R1.3 buys.
    """
    # Captured here rather than through `_execute`, which returns its buffer only
    # on a clean exit — this arm PRINTS and then RAISES, so a helper that unwinds
    # first would discard the very line under test. (It did, on the first run.)
    buffer = StringIO()
    with pytest.MonkeyPatch.context() as monkeypatch:
        _install_pyspark_functions(monkeypatch)
        # `df` is bound and `df_transformed` is NOT: P28 R10.4's whole point is
        # that the raw frame is in scope and must never be substituted for the
        # transform output.
        namespace = {
            FRAME_VARIABLE: _StubFrame(
                column_names=list(run.input_columns),
                rows=[{} for _ in range(run.input_row_count)],
                spark_dtypes=[],
            )
        }
        code = build_generated_marker_code() + build_validation_code(run.target_schema)
        with redirect_stdout(buffer), pytest.raises(RuntimeError, match="did not produce df_transformed"):
            exec(compile(code, "<run>", "exec"), namespace)

    stdout = buffer.getvalue()
    parsed = glue_session_runner._parse_results(None, stdout)

    assert parsed["execution_successful"] is False
    assert parsed["error_details"] == glue_session_runner._MISSING_TRANSFORM_FRAME_ERROR
    assert parsed["data_quality_results"] == {}
    assert parsed["output_records"] == 0
    assert parsed["input_records"] == _emitted_payload(stdout, _GENERATED)["record_count"]


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    reason=st.text(alphabet="abc ", min_size=1, max_size=12),
    record_count=st.integers(min_value=0, max_value=99),
)
def test_any_error_payload_short_circuits_before_the_data_quality_mapping(reason: str, record_count: int):
    """The error arm's precedence, over generated reason text.

    ``_parse_results`` returns as soon as ``"error"`` is present, before it reads
    ``record_count`` (``code_interpreter_runner.py:622-624``) — so a payload
    carrying both reports the error and **zero** output records. Generated over
    the reason text because ``error_details`` is the field the report prints, and
    the absent-frame message above is only one of its possible values.
    """
    stdout = f"{_VALIDATION}:{json.dumps({'error': reason, 'record_count': record_count})}\n"
    parsed = glue_session_runner._parse_results(None, stdout)

    assert parsed["execution_successful"] is False
    assert parsed["error_details"] == reason
    assert parsed["output_records"] == 0
    assert parsed["data_quality_results"] == {}
