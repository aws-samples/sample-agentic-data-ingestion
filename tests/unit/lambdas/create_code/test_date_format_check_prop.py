# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Properties of the p49 date-format check (design C1, correctness properties CP1/CP2).

The example tests live in ``test_rule_contradiction_check.py``'s p49 section —
including the regression, the observed run's five MM/dd/yyyy values against
``yyyy-MM-dd``. These two properties cover the spaces those examples can only
sample:

* **CP1 — zero parses over a non-empty sample is ALWAYS a finding.** Quantified
  over understood formats and over generated value lists, because the defect
  this spec exists for is one point in that space and the requirement (R1.1) is
  about all of it. "No sample size or column type exempts it" is the part an
  example test cannot assert.
* **CP2 — the check NEVER raises and never fails the step.** Quantified over
  arbitrary transform-block text, arbitrary format strings, arbitrary sample
  shapes, and a reader that raises. This is the property that makes
  ``LGR-9e76249-4``'s non-blocking stance a checked fact rather than a claim:
  that record rejected making the sibling check blocking because "a false gate
  on code generation is worse than a missed finding", and a check that raised
  out of ``generate_code`` would be exactly such a gate by accident.
* **CP3 — ambiguity is NEVER silently resolved (p49 task 5.1, design C3).**
  Quantified over generated day/month pairs that both readings accept, because
  the requirement (R5.1) is about the whole space and ``05/12/2024`` is one
  point in it. What is asserted is what the requirement asks for and nothing
  more: the outcome names both readings, marks which one the code assumed, and
  records no mismatch — it never emits a single format as settled.

Deliberately NOT properties here:

* **Which format is correct.** ``05/12/2024`` satisfies MM/dd/yyyy and
  dd/MM/yyyy alike; naming a single winner is forbidden (do-NOT 8, R5.1), and
  CP3 below asserts that the checker declines to, rather than asserting an
  answer it must not have.
* **The wrapped-expression skip and the pattern table's refusals.** Each is a
  single decided behaviour with a written reason, so each belongs in the example
  file beside that reason.

**Validates: Requirements 1.1, 1.2, 1.3, 5.1, 5.2, 5.3, 6.4**
"""

import os
import sys

import hypothesis.strategies as st
import pytest
from hypothesis import assume, given, settings

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1, template /
# ratified); these are no-ops kept for parity with the sibling files here.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _CREATE_CODE_DIR)

import code_assembly as _ca

# Formats the translator understands, so CP1's premise is reachable. A generator
# ranging over arbitrary strings would be refused by `_strptime_format` almost
# every time and the property would pass vacuously — the failure mode
# `LGR-c284fbb-5` describes, where a requirement is left "silently unmet — no
# assertion would have failed".
_UNDERSTOOD_FORMATS = st.sampled_from(
    [
        "yyyy-MM-dd",
        "MM/dd/yyyy",
        "dd-MM-yyyy",
        "dd/MM/yyyy",
        "yyyy/MM/dd",
        "yyyy-MM-dd HH:mm:ss",
        "dd MMM yyyy",
        "yy-MM-dd",
    ]
)

# Column names that survive `validate_identifier` and read plausibly.
_COLUMNS = st.sampled_from(["signup_date", "registration_date", "created_at", "d1", "birth_date"])

# Value shapes a real date column carries: correctly-laid-out dates, dates in a
# different layout, and non-date text. The mix is what makes both arms of CP1
# reachable rather than only the failing one.
_VALUES = st.sampled_from(
    [
        "01/15/2024",
        "05/12/2024",
        "2024-01-15",
        "15-01-2024",
        "15/01/2024",
        "2024/01/15",
        "2024-01-15 10:30:00",
        "15 Jan 2024",
        "24-01-15",
        "not a date",
        "0",
        "2024",
        "1/5/24",
        "Jan 15, 2024",
    ]
)

_READ_SHAPES = st.sampled_from(["col", "column", "subscript"])


def _transform(read_shape: str, column: str, spark_format: str, function: str = "to_date") -> str:
    """A one-line transform block reading ``column`` DIRECTLY (see `_direct_column_read`)."""
    reader = {
        "col": f'col("{column}")',
        "column": f'column("{column}")',
        "subscript": f'df["{column}"]',
    }[read_shape]
    return f'df_transformed = df.withColumn("out", {function}({reader}, "{spark_format}"))'


def _reader(samples):
    """The injected zero-argument sample reader, as `_sample_reader_for` returns."""

    def _read():
        return samples

    return _read


# ── CP1 ─────────────────────────────────────────────────────────────────────


@settings(max_examples=300, deadline=None)
@given(
    read_shape=_READ_SHAPES,
    column=_COLUMNS,
    spark_format=_UNDERSTOOD_FORMATS,
    values=st.lists(_VALUES, min_size=1, max_size=10, unique=True),
    function=st.sampled_from(["to_date", "to_timestamp"]),
)
def test_zero_parses_over_a_non_empty_sample_is_always_a_finding(read_shape, column, spark_format, values, function):
    """CP1: no sample size, read shape, column name or parse function exempts it.

    The premise is established from the translation itself rather than assumed,
    so the property is about the checker's own notion of parsing and cannot
    drift from it.

    **Validates: Requirements 1.1, 1.2, 1.3**
    """
    strptime_format = _ca._strptime_format(spark_format)
    assert strptime_format is not None  # the generator only draws understood formats
    assume(not any(_ca._value_parses(value, strptime_format) for value in values))

    findings = _ca.detect_date_format_mismatches(
        _transform(read_shape, column, spark_format, function), _reader({column: values})
    )

    assert len(findings) == 1
    assert findings[0]["column"] == column
    assert findings[0]["format"] == spark_format
    assert findings[0]["function"] == function
    # The finding is usable by the regeneration wiring: it names the column and
    # shows what the data looks like, and its `reason` is the string p37's
    # `_feedback_with_contradictions` shape renders into the next prompt.
    assert column in findings[0]["reason"]
    assert findings[0]["reason"].strip()


@settings(max_examples=300, deadline=None)
@given(
    read_shape=_READ_SHAPES,
    column=_COLUMNS,
    spark_format=_UNDERSTOOD_FORMATS,
    values=st.lists(_VALUES, min_size=1, max_size=10, unique=True),
)
def test_at_least_one_parse_is_never_a_finding(read_shape, column, spark_format, values):
    """CP1's other direction, which is what keeps the check from crying wolf.

    A finding is recorded where approvers read and feeds a regeneration prompt,
    so a false positive is the expensive direction. If any sampled value parses
    under the emitted format, the format is not refuted and nothing is recorded
    — including the ambiguous case, where settling on a layout is C3's job and
    forbidden here (do-NOT 8).

    **Validates: Requirements 1.1**
    """
    strptime_format = _ca._strptime_format(spark_format)
    assume(any(_ca._value_parses(value, strptime_format) for value in values))

    findings = _ca.detect_date_format_mismatches(
        _transform(read_shape, column, spark_format), _reader({column: values})
    )

    assert findings == []


# ── CP2 ─────────────────────────────────────────────────────────────────────

# Transform-block text including the shapes that break each stage: unparseable
# Python, a parse with no format, a non-literal format, a wrapped read, and a
# non-literal column name (the templates' own cast loops).
_TRANSFORM_TEXT = st.one_of(
    st.text(max_size=120),
    st.sampled_from(
        [
            "",
            "   ",
            'df_transformed = df.withColumn("d", to_date(col("x"), "yyyy-MM-dd")',  # unbalanced
            'df_transformed = df.withColumn("d", to_date(col("x")))',
            'df_transformed = df.withColumn("d", to_date(col("x"), fmt))',
            'df_transformed = df.withColumn("d", to_date(trim(col("x")), "yyyy-MM-dd"))',
            'for f in schema.fields:\n    df = df.withColumn(f.name, to_date(col(f.name), "yyyy-MM-dd"))',
            'df_transformed = df.withColumn("d", to_date())',
            'to_date(col("x"), "yyyy-MM-dd")',
            "to_date = 1",
        ]
    ),
    st.builds(_transform, _READ_SHAPES, _COLUMNS, st.text(min_size=0, max_size=12)),
)

# Sample dicts including every shape a misbehaving reader could hand back.
_SAMPLE_DICTS = st.one_of(
    st.just({}),
    st.dictionaries(st.text(max_size=16), st.lists(st.text(max_size=24), max_size=6), max_size=4),
    st.dictionaries(_COLUMNS, st.lists(_VALUES, max_size=6), max_size=3),
    st.sampled_from(
        [
            {"signup_date": []},
            {"signup_date": ["", "  "]},
            {"signup_date": [None, 1, True]},
            {"signup_date": "01/15/2024"},
            {1: ["01/15/2024"]},
            None,
        ]
    ),
)


# `ast.parse` emits a SyntaxWarning (without raising) for an invalid escape
# sequence in a string literal, and the generated text below produces those.
# Ignored HERE, in the fuzz test, rather than suppressed in production: the
# warning is real information about generated PySpark, and the check's contract
# is that a warning does not raise. Both properties still assert totality.
@pytest.mark.filterwarnings("ignore::SyntaxWarning")
@settings(max_examples=500, deadline=None)
@given(transform_block=_TRANSFORM_TEXT, samples=_SAMPLE_DICTS)
def test_the_check_never_raises_and_always_returns_a_list(transform_block, samples):
    """CP2: a finding or no finding, never an exception. R6.4.

    ``LGR-9e76249-4`` (**template / ratified**) rejected making the sibling
    check blocking, on the ground that "a false gate on code generation is
    worse than a missed finding". A check that raised out of ``generate_code``
    would be that gate by accident, so totality is asserted rather than
    inspected.

    The C3 ambiguity out list is quantified here too, because task 5.1 widened
    the surface CP2 has to cover: the ambiguity arm runs inside the same
    function, over the same arbitrary inputs, and its own totality is the same
    requirement.

    **Validates: Requirements 6.4**
    """
    ambiguities: list[dict[str, str]] = []
    findings = _ca.detect_date_format_mismatches(transform_block, _reader(samples), ambiguities)

    assert isinstance(findings, list)
    for finding in findings:
        assert set(finding) == {"column", "function", "format", "samples", "reason"}
        assert all(isinstance(value, str) for value in finding.values())
    for ambiguity in ambiguities:
        assert set(ambiguity) == {
            "column",
            "function",
            "assumed_format",
            "alternative_format",
            "samples",
            "reason",
        }
        assert all(isinstance(value, str) for value in ambiguity.values())
    # The two outcomes are mutually exclusive per node, by construction: the
    # ambiguity arm runs in the branch that already decided not to record a
    # mismatch. Asserted over arbitrary input rather than trusted.
    assert not ({f["column"] for f in findings} & {a["column"] for a in ambiguities})


@pytest.mark.filterwarnings("ignore::SyntaxWarning")
@settings(max_examples=200, deadline=None)
@given(transform_block=_TRANSFORM_TEXT, message=st.text(max_size=40))
def test_a_reader_that_raises_never_propagates(transform_block, message):
    """CP2 over the read: `AccessDenied`, `NoSuchKey`, throttling, decode errors.

    The reader `lambda_function` injects already returns ``{}`` on failure, but
    the guarantee that matters is ``generate_code``'s, so the check absorbs a
    raising reader too — a telemetry read must never fail code generation
    (design §Error Handling, final row).

    **Validates: Requirements 6.4**
    """

    def _raising():
        raise RuntimeError(message)

    assert _ca.detect_date_format_mismatches(transform_block, _raising) == []


@settings(max_examples=300, deadline=None)
@given(spark_format=st.text(max_size=32))
def test_the_pattern_translation_is_total(spark_format):
    """CP2 over the format: a translation or ``None``, never an exception.

    ``None`` is the safe answer and the caller records no finding for it, so an
    unknown pattern is inert rather than an error (design §Error Handling, row
    4). A translation that came back must additionally be usable — the value
    test must not raise on it either.

    **Validates: Requirements 6.4**
    """
    translated = _ca._strptime_format(spark_format)

    assert translated is None or isinstance(translated, str)
    if translated is not None:
        assert isinstance(_ca._value_parses("01/15/2024", translated), bool)


@settings(max_examples=300, deadline=None)
@given(value=st.text(max_size=32), spark_format=_UNDERSTOOD_FORMATS)
def test_the_value_test_is_total(value, spark_format):
    """CP2 over the value: a bool for any text, never an exception."""
    strptime_format = _ca._strptime_format(spark_format)

    assert isinstance(_ca._value_parses(value, strptime_format), bool)


# ── CP3 ─────────────────────────────────────────────────────────────────────

# `(emitted format, the day/month exchange of it, a template over m/d/y)`.
#
# The middle column is a HAND-WRITTEN oracle, deliberately not derived from
# `_day_month_swapped`. A generator that computed the expectation with the
# function under test would assert only that the function agrees with itself,
# which is the vacuity `LGR-c284fbb-5` describes — a requirement left "silently
# unmet, no assertion would have failed".
#
# The template renders values from a TRUE month and day under the emitted
# reading, which is what lets the two properties below drive the same layouts
# into opposite outcomes by varying only the day: ≤ 12 leaves both readings
# open, ≥ 13 refutes the exchange.
_AMBIGUOUS_LAYOUTS = [
    ("MM/dd/yyyy", "dd/MM/yyyy", "{m:02d}/{d:02d}/{y}"),
    ("dd/MM/yyyy", "MM/dd/yyyy", "{d:02d}/{m:02d}/{y}"),
    ("yyyy-MM-dd", "yyyy-dd-MM", "{y}-{m:02d}-{d:02d}"),
    ("MM-dd-yyyy", "dd-MM-yyyy", "{m:02d}-{d:02d}-{y}"),
    ("dd.MM.yyyy", "MM.dd.yyyy", "{d:02d}.{m:02d}.{y}"),
]

_LAYOUTS = st.sampled_from(_AMBIGUOUS_LAYOUTS)
_YEARS = st.integers(min_value=1990, max_value=2030)
# Both components ≤ 12, so every value is a legal date read either way round.
_AMBIGUOUS_PAIRS = st.lists(
    st.tuples(st.integers(min_value=1, max_value=12), st.integers(min_value=1, max_value=12)),
    min_size=1,
    max_size=5,
)


@settings(max_examples=300, deadline=None)
@given(layout=_LAYOUTS, year=_YEARS, pairs=_AMBIGUOUS_PAIRS, read_shape=_READ_SHAPES, column=_COLUMNS)
def test_a_sample_two_formats_both_explain_names_the_ambiguity_and_the_assumption(
    layout, year, pairs, read_shape, column
):
    """CP3: the outcome names the ambiguity AND the format that was assumed.

    Quantified over every layout whose day and month can trade places and over
    generated component pairs, because R5.1 is a statement about all of them and
    ``05/12/2024`` is one point in that space.

    What is asserted is exactly what R5.1–R5.3 ask for, and deliberately not
    more: **both** readings are named, the one the code took is marked as an
    assumption, and **no mismatch is recorded** — the checker never emits a
    single format as settled. Asserting which reading is correct would be
    asserting the thing do-NOT 8 forbids the production code from deciding.

    **Validates: Requirements 5.1, 5.2, 5.3**
    """
    spark_format, expected_alternative, template = layout
    values = [template.format(m=month, d=day, y=year) for month, day in pairs]
    # Divergence is required, not assumed: `2024-01-01` reads the same either
    # way round, and an ambiguity that cannot change a loaded value is not one
    # worth an approver's attention (design O3).
    assume(any(month != day for month, day in pairs))

    # Non-vacuity, established from the checker's own notion of parsing: BOTH
    # readings really do accept every value, so this is the "not refuted"
    # branch and not the mismatch branch arriving by accident.
    assumed_strptime = _ca._strptime_format(spark_format)
    alternative_strptime = _ca._strptime_format(expected_alternative)
    assert assumed_strptime is not None and alternative_strptime is not None
    assert all(_ca._value_parses(value, assumed_strptime) for value in values)
    assert all(_ca._value_parses(value, alternative_strptime) for value in values)

    ambiguities: list[dict[str, str]] = []
    findings = _ca.detect_date_format_mismatches(
        _transform(read_shape, column, spark_format), _reader({column: values}), ambiguities
    )

    assert findings == []
    assert len(ambiguities) == 1
    ambiguity = ambiguities[0]
    assert ambiguity["column"] == column
    assert ambiguity["assumed_format"] == spark_format
    assert ambiguity["alternative_format"] == expected_alternative
    # Both readings are named in the text an approver reads, and the assumed one
    # is marked as an assumption rather than reported as the format.
    assert spark_format in ambiguity["reason"]
    assert expected_alternative in ambiguity["reason"]
    assert "ASSUMPTION" in ambiguity["reason"]


@settings(max_examples=300, deadline=None)
@given(
    layout=_LAYOUTS,
    year=_YEARS,
    months=st.lists(st.integers(min_value=1, max_value=12), min_size=1, max_size=4),
    day=st.integers(min_value=13, max_value=28),
    read_shape=_READ_SHAPES,
    column=_COLUMNS,
)
def test_a_sample_that_refutes_the_other_reading_is_not_an_ambiguity(layout, year, months, day, read_shape, column):
    """CP3's other direction: data that settles the format produces no finding.

    A day above 12 cannot be read as a month, so the exchange is refuted by the
    data and the emitted format IS settled. This is the property that keeps CP3
    from being satisfiable by a checker that calls everything ambiguous — which
    would be a different way of telling an approver nothing, and the
    approval-fatigue failure design O3 warns about.

    ``01/15/2024`` in the observed run's own sample is exactly this case.

    **Validates: Requirements 5.1**
    """
    spark_format, expected_alternative, template = layout
    values = [template.format(m=month, d=day, y=year) for month in months]

    # Non-vacuity: the emitted reading accepts every value and the exchange
    # accepts none, so the format really is settled by this sample.
    assumed_strptime = _ca._strptime_format(spark_format)
    alternative_strptime = _ca._strptime_format(expected_alternative)
    assert all(_ca._value_parses(value, assumed_strptime) for value in values)
    assert not any(_ca._value_parses(value, alternative_strptime) for value in values)

    ambiguities: list[dict[str, str]] = []
    findings = _ca.detect_date_format_mismatches(
        _transform(read_shape, column, spark_format), _reader({column: values}), ambiguities
    )

    assert findings == []
    assert ambiguities == []


@settings(max_examples=300, deadline=None)
@given(spark_format=st.text(max_size=24))
def test_the_day_month_exchange_is_total_and_never_invents_a_pattern(spark_format):
    """CP2 over C3's new helper: an exchange or ``None``, never an exception.

    And when it does return a pattern, the pattern is real — the translator
    understands it, and its translation is the ``%m``/``%d`` exchange of the
    original's. Without that second half the helper could return a plausible
    string the parser cannot use, and the ambiguity arm would go quietly inert
    for reasons no reader could see.

    **Validates: Requirements 6.4**
    """
    swapped = _ca._day_month_swapped(spark_format)

    assert swapped is None or isinstance(swapped, str)
    if swapped is None:
        return
    original = _ca._strptime_format(spark_format)
    if original is None:
        return
    assert _ca._strptime_format(swapped) == _ca._swapped_day_month_directives(original)
