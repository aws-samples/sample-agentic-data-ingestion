# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p44-verify-against-real-source-data, Property 9: Truncation bounds
# every quoted literal, says what it removed, and preserves the diagnostic
"""Property 9 — truncation bounds every quoted literal, states and preserves.

Spec ``p44-verify-against-real-source-data`` task 8.2 — requirements 6.6, 6.8,
6.9; design §4.8, resolved decision 4.

    *For any* error text containing zero or more quoted literals of arbitrary
    length, every quoted literal in the output is at most
    :data:`_ERROR_LITERAL_MAX_CHARS` characters, every truncated literal states
    the number of characters removed, all text outside quoted literals is
    byte-preserved, a text with no over-long literal is returned unchanged, and a
    second application changes nothing. Two literals of different lengths above
    the bound do not render identically.

**Validates: Requirements 6.6, 6.8, 6.9**

No Glue session, and none is needed
-----------------------------------
do-NOT 1. Everything under test is a pure function of a ``str`` or of a
``Mapping``: :func:`_truncate_quoted_literals` takes text,
:func:`exception_summary` and :func:`synthesised_traceback` take a statement
dict. No runner is constructed, no client — mocked or otherwise — is built, and
nothing here can bill.

How the property is stated: a relation between two partitions
-------------------------------------------------------------
Every arm below is derived from **one** comparison — the partition of the input
into (plain span, quoted literal, plain span, …) against the partition of the
output. :func:`_partition` does that split, in strict alternation, so a text with
``n`` literals always yields ``n`` literals and ``n + 1`` plain spans on every
line, and the two partitions align index for index. Truncation neither creates
nor destroys a literal, so:

* **the bound** (R6.6) is a claim about each output literal's own length;
* **byte-preservation** (R6.9) is equality of the plain spans, except the one
  immediately after a truncated literal, which gains the elision marker;
* **the statement** (R6.8) is that marker, and the count in it is checked against
  ``len(input literal) - len(kept prefix)`` — read off the two partitions rather
  than recomputed from ``_ERROR_LITERAL_MAX_CHARS``, so the assertion is R6.8's
  own words ("the number of characters removed") and not a second copy of the
  implementation's arithmetic.

**:func:`_partition` is written here rather than imported.** Using the module's
own :func:`_closing_quote_index` to decide what counts as a literal in the output
would make the producer its own oracle — ``LGR-28b4d75-5`` (**template /
ratified**): "a test written from the producer's output cannot detect the producer
drifting". It is written from the two scanning rules the helper's docstring
states, and :func:`test_the_partition_recovers_the_literals_it_was_built_from`
guards it against the structured generator so a scanner bug cannot pass as a
production pass.

The two design choices task 8.1 flagged, and where this file lands on them
--------------------------------------------------------------------------
Both are asserted as documented. Neither is contradicted, and no production code
is touched by this task.

1. **An unterminated quote is left byte-identical**
   (:func:`test_an_unterminated_quote_is_left_byte_identical`). Agreed, and it is
   the only answer consistent with do-NOT 16: where an unterminated value ends is
   unknowable, so "to end of line" would delete diagnostic text that merely
   happens to follow a stray quote — the exception type and the failing column are
   exactly the text that would go. It costs a stated residual rather than a
   silent one: such a value is bounded only by ``_bound_stderr``'s head-2000 +
   tail-1500 (``code_interpreter_runner.py:55-56``), which
   ``test_stderr_bound_prop.py`` owns. Worth naming as a residual rather than a
   guarantee, because a Spark message truncated by an earlier layer is precisely
   how the shape arises in the wild.
2. **A literal never spans a newline**
   (:func:`test_a_literal_never_spans_a_newline`). Agreed, and for the stronger of
   the two reasons: the alternative deletes evidence. One stray quote early in a
   multi-line Py4J message would otherwise make the whole remainder "the literal"
   and cut it to 64 characters, taking every frame and the exception type with it
   — a direct R6.9 violation, and self-defeating for a bound whose purpose is to
   keep the diagnostic.

Single quotes and backticks are deliberately unbounded and this file asserts
that (:func:`test_single_quotes_and_backticks_are_not_bounded`). Spark quotes
*identifiers* that way and a column name is schema, not data — resolved decision
3, whose residual is the point rather than the price: a header that disagrees
with the declared schema is among the likeliest real causes of a transform
producing zero rows.

Both channels, because R6.10 holds per channel and not per sink
---------------------------------------------------------------
The helper is applied at two sites, one per channel, so the same relation is
asserted end to end through both: :func:`exception_summary` for the
``ErrorName`` / ``ErrorValue`` channel and :func:`synthesised_traceback`'s frame
rendering for the ``Output.Traceback`` channel. The idempotence arm is checked on
each channel's *output* as well as on the helper, which is where "once per
channel, not once per sink" is actually falsifiable: ``synthesised_traceback``
composes an already-truncated exception summary with freshly truncated frames, so
a non-idempotent truncation would double-elide there first.

The stdout channel is **not** asserted here, because it is a stated residual
rather than an oversight (do-NOT 24): ``Output.Data.TextPlain`` is what
``_parse_results`` reads the ``GENERATED:`` and ``VALIDATION:`` markers out of, so
truncating it would corrupt the marker parse.
``GlueSessionTestRunner._observe``'s own comment records that, and this file adds
no expectation that contradicts it.

The bound is asserted twice, on purpose
---------------------------------------
Against the imported constant everywhere — R6.6's whole point is that the number
is a named module constant a property test can import rather than an inline
literal — and against the bare ``64`` once, in
:func:`test_the_bound_is_the_sixty_four_resolved_decision_4_proposes`. The two
fail for different reasons: the imported form fails when truncation stops
honouring its own bound, the literal form fails when the *value* is retuned
without the requirement being revisited. Only the second would notice someone
quietly setting the constant to 4096.

Six shapes a reviewer recognises
--------------------------------
:data:`_REAL_SHAPES` is resolved decision 4's own enumeration — a thousands
separator, a timestamp in a numeric column, a currency prefix, exponent notation,
a sentinel, padding. Every one is comfortably under the bound and must survive
untouched, which is the decision's central claim: the bound is not there for
these, it is there for the delimiter-collapse case where "the value" is an entire
CSV record. Both halves are pinned per shape, and the shapes are also seeded into
the property as explicit ``@example``\\ s so a counterexample report names a shape
rather than random text.

Ledger and structure notes
--------------------------
``decisions(concept="security", include_backfilled=True)`` returns **nothing**,
which corroborates requirements C10: this repository has **no ratified decision
about data protection for customer rows**, so Requirement 6 is first-of-kind. The
constant's own comment presents itself as a proposal for ratification, and this
file reads that as a **gap in the ledger, not as licence** — it asserts what R6.6,
R6.8 and R6.9 say and invents no further data-protection rule of its own.
``concept="error-handling"`` holds one record, ``LGR-b2f79ef-13``, about narrowing
a blanket ``except Exception`` in ``schema_mapping``, which itself records the
concept as undocumented; ``concept="logging"`` and ``concept="testing"`` carry
nothing about error text leaving a session either. ``why`` on
``glue_session_runner.py`` returns the three ``5c30733`` records (dedent, the
transform namespace, the executor caveat) and five ``ad78946`` records, all five
carrying ``NOTE: BROAD`` over a 60-file commit — **no fence on this code**. The
conventions followed instead are ``LGR-3e45e65-1`` (the unit layer pins its region
in ``tests/unit/conftest.py``) and ``LGR-28b4d75-5``, cited above.

Codegraph `explore`, on the five symbols this file exercises. Counts are as
reported and are inflated by test-site attribution to the module under test:

* ``_truncate_quoted_literals`` (``:3293``) — 2 callers, both production, both in
  ``glue_session_runner.py``: :func:`exception_summary` and
  :func:`synthesised_traceback`. ⚠️ **no covering tests** — this file is its
  first, which is why the arms above are stated over the helper directly as well
  as through the two channels.
* ``_truncate_line_quoted_literals`` (``:3357``) — 1 caller. ⚠️ no covering tests.
* ``_closing_quote_index`` (``:3272``) — 1 caller. ⚠️ no covering tests.
* ``exception_summary`` (``:3385``) — 6 callers; covered by
  ``test_glue_result_mapping.py`` and ``test_stderr_bound_prop.py``.
* ``synthesised_traceback`` (``:3409``) — 8 callers; same two covering files.
* ``_bound_stderr`` (``code_interpreter_runner.py:469``) — the other, unrelated
  bound. Not exercised here; ``test_stderr_bound_prop.py`` owns it, and the two
  bounds cap different things (one the whole message, one a single value inside
  it). ``_SYNTHESISED_TRACEBACK_HEADER`` (``:3186``) is flagged with no covering
  tests, which ``test_stderr_bound_prop.py:85-88`` already records as a false
  negative of the kind the spec notes for ``lambda/**``; this file adds another
  covering reference either way.

Nothing in ``shared/`` is in the blast radius: the constant, the elision, the
helper and both channel sites all live in
``lambda/agentic_data_ingestion_test_code/glue_session_runner.py``, and the one
cross-module reference (``_bound_stderr``) is to the sibling Lambda module in the
same directory.
"""

import os
import sys

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from glue_session_runner import (
    _ERROR_LITERAL_ELISION,
    _ERROR_LITERAL_MAX_CHARS,
    _SYNTHESISED_TRACEBACK_HEADER,
    _truncate_quoted_literals,
    exception_summary,
    synthesised_traceback,
)

#: A realistic unquoted exception type. Its survival is R6.9's own example, and it
#: is deliberately a fixture rather than a draw: a Spark exception class is not
#: arbitrary text, and generating one would test the generator.
_EXCEPTION_NAME = "NumberFormatException"

#: An unquoted column identifier. R6.9 requires it to survive byte-identical, and
#: do-NOT 16 forbids asserting the opposite — so it is placed OUTSIDE the quotes
#: in every end-to-end draw and asserted present, never asserted truncated.
_COLUMN_IDENTIFIER = "order_total_amount"

#: Resolved decision 4's own six shapes, with its own labels. Every one is under
#: the bound, which is the decision's point: the bound exists for the
#: delimiter-collapse case, not for these.
_REAL_SHAPES = [
    ("a thousands separator", "1,234.56"),
    ("a timestamp in a numeric column", "2026-08-18T19:45:13Z"),
    ("a currency prefix", "$1200"),
    ("exponent notation", "1.2e5"),
    ("a sentinel", "N/A"),
    ("padding", " 42 "),
]


# ---------------------------------------------------------------------------
# The partition, written from the documented rules rather than imported
# ---------------------------------------------------------------------------


def _independent_closing_quote(line: str, start: int) -> int:
    """Index of the ``"`` closing a literal whose content begins at *start*.

    ``-1`` when the line carries none. Written from the helper's docstring: a
    backslash escapes whatever follows it, so ``\\"`` does not close a literal and
    a ``\\\\`` pair escapes itself. Deliberately NOT
    :func:`glue_session_runner._closing_quote_index` — see the module docstring.
    """
    index = start
    while index < len(line):
        if line[index] == "\\":
            index += 2
            continue
        if line[index] == '"':
            return index
        index += 1
    return -1


def _partition_line(line: str) -> tuple[list[str], list[str]]:
    """Split one line into ``(plain spans, quoted literals)``, strictly alternating.

    ``len(plains) == len(literals) + 1`` always, so two partitions of the same
    line before and after truncation align index for index. The opening quote is
    found with a plain search, not an escape-aware one, matching the helper: an
    outside backslash does not stop the next ``"`` from opening a literal.

    An unterminated quote ends the scan — the stray quote and everything after it
    on the line land in the final plain span, which is what makes "left
    byte-identical" fall out of the plain-span equality assertion rather than
    needing a rule of its own.
    """
    plains: list[str] = []
    literals: list[str] = []
    cursor = 0
    while True:
        opening = line.find('"', cursor)
        if opening < 0:
            plains.append(line[cursor:])
            return plains, literals

        closing = _independent_closing_quote(line, opening + 1)
        if closing < 0:
            plains.append(line[cursor:])
            return plains, literals

        plains.append(line[cursor:opening])
        literals.append(line[opening + 1 : closing])
        cursor = closing + 1


def _partition(text: str) -> list[tuple[list[str], list[str]]]:
    """:func:`_partition_line` over every line — a literal never spans a newline."""
    return [_partition_line(line) for line in text.split("\n")]


def _assert_truncation_relation(source: str, result: str) -> None:
    """Property 9 over one ``(input, output)`` pair.

    Every arm the property names, stated once here so that the helper, both
    channels and the named shapes are all measured by the same yardstick.
    """
    source_lines = _partition(source)
    result_lines = _partition(result)

    assert len(result_lines) == len(source_lines), "truncation changed the number of lines"

    any_over_bound = False

    for line_index, ((source_plains, source_literals), (result_plains, result_literals)) in enumerate(
        zip(source_lines, result_lines, strict=True)
    ):
        assert len(result_literals) == len(source_literals), (
            f"line {line_index}: truncation changed the number of quoted literals "
            f"({len(source_literals)} in, {len(result_literals)} out)"
        )

        #: The elision marker each literal contributed to the plain span that
        #: follows it — empty for a literal that was left alone.
        markers: list[str] = []

        for literal_index, (before, after) in enumerate(zip(source_literals, result_literals, strict=True)):
            # R6.6: the bound holds on what a reader sees between the quotes.
            assert len(after) <= _ERROR_LITERAL_MAX_CHARS, (
                f"line {line_index} literal {literal_index}: {len(after)} characters "
                f"exceeds the bound of {_ERROR_LITERAL_MAX_CHARS}: {after!r}"
            )

            if len(before) <= _ERROR_LITERAL_MAX_CHARS:
                # Nothing to remove, so nothing may be removed and nothing stated.
                assert after == before, (
                    f"line {line_index} literal {literal_index}: a literal within the bound was altered"
                )
                markers.append("")
                continue

            any_over_bound = True
            assert after.endswith(_ERROR_LITERAL_ELISION), (
                f"line {line_index} literal {literal_index}: a truncated literal does not "
                f"end with {_ERROR_LITERAL_ELISION!r}: {after!r}"
            )
            kept = after[: -len(_ERROR_LITERAL_ELISION)]
            assert before.startswith(kept), (
                f"line {line_index} literal {literal_index}: the kept text is not a prefix of the value"
            )
            # R6.8: the number of characters removed, read off the two partitions
            # rather than recomputed from the bound.
            markers.append(f" (+{len(before) - len(kept)} chars elided)")

        # R6.9: everything outside a quoted literal is byte-preserved, and the
        # only insertion anywhere is the elision marker a truncated literal owes.
        for plain_index, (before, after) in enumerate(zip(source_plains, result_plains, strict=True)):
            expected = (markers[plain_index - 1] if plain_index else "") + before
            assert after == expected, (
                f"line {line_index} plain span {plain_index}: text outside a quoted literal "
                f"was not preserved\n  expected: {expected!r}\n  actual:   {after!r}"
            )

    # A text with no over-long literal is returned unchanged — asserted as
    # whole-string equality, which is stronger than the per-span arms above.
    if not any_over_bound:
        assert result == source, "a text with no over-long literal was not returned unchanged"

    # Idempotent: the helper renders into two sinks, so a second pass must not
    # double-elide.
    assert _truncate_quoted_literals(result) == result, "truncation is not idempotent"


# ---------------------------------------------------------------------------
# Generators — error text rich in the four shapes the task names
# ---------------------------------------------------------------------------

#: Plain (outside-the-quotes) text. Free of ``"`` so that the structured
#: generator's own literals are the only ones in the text, which is what
#: :func:`test_the_partition_recovers_the_literals_it_was_built_from` checks. A
#: backslash IS allowed: both the helper and :func:`_partition_line` find the
#: opening quote with a plain search, so an outside backslash changes neither.
_PLAIN_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126, exclude_characters='"'),
    max_size=40,
)

#: Literal content, built from tokens so that it can never end in a lone
#: backslash — which would escape its own closing quote and make it an
#: unterminated literal instead of the shape being generated. ``\\"`` is the
#: escaped-quote shape the task requires the generator to reach; ``\\\\`` is the
#: self-escaping pair that lets a following quote close.
_LITERAL_TOKEN = st.one_of(
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126, exclude_characters='"\\'),
        min_size=1,
        max_size=12,
    ),
    st.just('\\"'),
    st.just("\\\\"),
)

#: Lengths that cross the bound in both directions. Three overlapping bands
#: rather than one wide range: a uniform draw would put almost nothing within a
#: character or two of the bound, and the middle band is what makes "a literal
#: exactly at the bound" — the task's fourth required shape — a routine draw
#: rather than a lucky one. The named tests below pin it regardless of the draw.
_LITERAL_LENGTHS = st.one_of(
    st.integers(min_value=0, max_value=_ERROR_LITERAL_MAX_CHARS),
    st.integers(min_value=max(0, _ERROR_LITERAL_MAX_CHARS - 4), max_value=_ERROR_LITERAL_MAX_CHARS + 4),
    st.integers(min_value=_ERROR_LITERAL_MAX_CHARS + 1, max_value=_ERROR_LITERAL_MAX_CHARS * 8),
)

_LITERAL_FILLER = st.sampled_from(["x", "0", ",", " ", "é"])


@st.composite
def _literal_contents(draw) -> str:
    """Literal content of a drawn length, ending in filler or in a whole token.

    Padding rather than slicing, so the token structure at the tail survives: a
    slice could cut a ``\\"`` pair in half and leave a lone trailing backslash,
    which is a different shape (an unterminated literal) from the one being
    generated.
    """
    target = draw(_LITERAL_LENGTHS)
    filler = draw(_LITERAL_FILLER)
    base = "".join(draw(st.lists(_LITERAL_TOKEN, max_size=8)))
    if len(base) >= target:
        return filler * target
    return base + filler * (target - len(base))


@st.composite
def _structured_error_texts(draw) -> tuple[str, list[list[str]]]:
    """Multi-line error text, plus the literal contents each line was built from.

    Reaches all four shapes the task names: several literals in one message (up to
    three per line over up to three lines), an escaped quote inside a literal (a
    ``_LITERAL_TOKEN`` choice), an unterminated quote (the ``stray`` tail), and a
    literal exactly at the bound (the middle length band).

    The built literals are returned alongside the text so that
    :func:`test_the_partition_recovers_the_literals_it_was_built_from` can check
    the partition against them. The stray tail is always appended **last**, so
    every literal a line was built from is still terminated and every one of them
    must be found — which is what makes that guard able to fail.
    """
    lines: list[str] = []
    built: list[list[str]] = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        pieces = [draw(_PLAIN_TEXT)]
        contents: list[str] = []
        for _ in range(draw(st.integers(min_value=0, max_value=3))):
            content = draw(_literal_contents())
            contents.append(content)
            pieces.append(f'"{content}"')
            pieces.append(draw(_PLAIN_TEXT))
        stray = draw(st.one_of(st.none(), _PLAIN_TEXT))
        if stray is not None:
            pieces.append(f'"{stray}')
        lines.append("".join(pieces))
        built.append(contents)
    return "\n".join(lines), built


#: The same generator with the bookkeeping dropped, for the arms that only need
#: text. Mapped rather than written twice, so the two cannot drift.
_ERROR_TEXTS = _structured_error_texts().map(lambda generated: generated[0])


def _collapsed_csv_record(value: str, *, fields: int = 40) -> str:
    """A whole CSV record collapsed into one field — the case the bound exists for.

    Resolved decision 4: "a CSV read with the wrong delimiter collapses an entire
    record into one field, so an untruncated message reproduces a whole customer
    row". Comfortably over the bound for any *value*, and *value* leads so the
    kept prefix is the recognisable shape.
    """
    return ",".join([value, *(f"customer_{index}@example.test" for index in range(fields))])


def _cast_failure_text(value: str) -> str:
    """A Spark cast failure quoting one value and naming an unquoted column."""
    return f'{_EXCEPTION_NAME}: For input string: "{value}" while casting column {_COLUMN_IDENTIFIER}'


# ---------------------------------------------------------------------------
# Property 9 — over the helper
# ---------------------------------------------------------------------------


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(text=_ERROR_TEXTS)
@example(text=f'{_EXCEPTION_NAME}: For input string: "{"9" * 400}" at column {_COLUMN_IDENTIFIER}')
@example(text=f'first "{"a" * 200}" and second "{"b" * 90}" and third "{"c" * 3}"')
@example(text=f'escaped quote inside: "{"z" * 80}\\"{"z" * 80}" tail')
@example(text=f'unterminated: "{"q" * 300} and no closing quote')
@example(text=f'exactly at the bound: "{"e" * _ERROR_LITERAL_MAX_CHARS}"')
@example(text=f'one over the bound: "{"e" * (_ERROR_LITERAL_MAX_CHARS + 1)}"')
@example(text=f'{_EXCEPTION_NAME}: For input string: "{_collapsed_csv_record("1,234.56")}"')
@example(text="no quoted literal here at all")
@example(text="")
@example(text=f'multi\nline "{"m" * 300}"\nwith frames "{"n" * 10}"')
def test_truncation_bounds_states_and_preserves(text: str):
    """**Validates: Requirements 6.6, 6.8, 6.9**

    The whole property in one assertion helper: the bound, the stated count, the
    byte-preserved surroundings, unchanged when there is nothing to remove, and
    idempotent.

    The ``@example`` seeds are not decoration. They make the four shapes task 8.2
    names — several literals in one message, an escaped quote inside a literal, an
    unterminated quote, a literal exactly at the bound — *executed* rather than
    merely reachable, and they add the delimiter-collapse case resolved decision
    4 says the bound exists for.
    """
    _assert_truncation_relation(text, _truncate_quoted_literals(text))


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(text=st.text(max_size=400))
def test_truncation_holds_over_wholly_arbitrary_text(text: str):
    """**Validates: Requirements 6.6, 6.8, 6.9**

    The same relation over text with no structure imposed at all — unbalanced
    quotes, control characters, lone surrogates' worth of oddity. The structured
    generator above is built to reach interesting shapes and therefore cannot
    prove the helper never mangles an uninteresting one; this arm can, and it is
    the one that would catch a scanner that lost a character on a shape nobody
    thought to enumerate.
    """
    _assert_truncation_relation(text, _truncate_quoted_literals(text))


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(generated=_structured_error_texts())
def test_the_partition_recovers_the_literals_it_was_built_from(generated: tuple[str, list[list[str]]]):
    """Guard on :func:`_partition`, so a scanner bug cannot pass as a production pass.

    **This is the file's anti-vacuity check, and it is load-bearing.** Every arm of
    Property 9 is stated through :func:`_partition`, and a partition that found no
    literals at all would satisfy all of them trivially — the bound holds over an
    empty list. So the generator's own structure is checked back out of it: every
    literal a line was built from is a literal the partition finds, in order, and
    reassembling the partition returns the line byte for byte, which is what makes
    "text outside a literal is preserved" a claim about the whole line rather than
    about whatever the scanner happened to notice.
    """
    text, built = generated
    for line, contents, (plains, literals) in zip(text.split("\n"), built, _partition(text), strict=True):
        assert literals == contents, "the partition did not recover the literals the line was built from"
        assert len(plains) == len(literals) + 1, "the partition is not strictly alternating"
        rebuilt = plains[0] + "".join(
            f'"{literal}"{plain}' for literal, plain in zip(literals, plains[1:], strict=True)
        )
        assert rebuilt == line, "the partition did not round-trip the line"


# ---------------------------------------------------------------------------
# The four shapes and the bound, pinned rather than sampled
# ---------------------------------------------------------------------------


def test_multiple_literals_in_one_message_are_each_bounded_and_each_counted():
    """**Validates: Requirements 6.6, 6.8**

    Two over-bound literals in one message. Each is bounded on its own and each
    states its own count — a truncation that bounded only the first, or that
    reported one total for the pair, fails here.
    """
    first, second = "1" * 300, "2" * 100
    text = f'{_EXCEPTION_NAME}: first "{first}" then "{second}" for {_COLUMN_IDENTIFIER}'
    result = _truncate_quoted_literals(text)

    _assert_truncation_relation(text, result)

    _, literals = _partition(result)[0]
    assert len(literals) == 2
    assert all(len(literal) == _ERROR_LITERAL_MAX_CHARS for literal in literals)
    keep = _ERROR_LITERAL_MAX_CHARS - len(_ERROR_LITERAL_ELISION)
    assert f"(+{len(first) - keep} chars elided)" in result
    assert f"(+{len(second) - keep} chars elided)" in result
    # R6.9, and do-NOT 16 read the other way round: these are asserted PRESENT.
    assert _EXCEPTION_NAME in result
    assert _COLUMN_IDENTIFIER in result


def test_an_escaped_quote_inside_a_literal_does_not_end_it():
    """**Validates: Requirements 6.6, 6.9**

    ``\\"`` inside a literal is content, not a terminator. Reading it as one would
    split a single value into two literals and bound neither of them at its real
    length — the escaped half would look short enough to leave alone, so the
    truncation would silently stop applying to the shape it most needs to.
    """
    content = ("a" * 60) + '\\"' + ("b" * 200)
    text = f'{_EXCEPTION_NAME}: For input string: "{content}" in {_COLUMN_IDENTIFIER}'
    result = _truncate_quoted_literals(text)

    _assert_truncation_relation(text, result)

    _, literals = _partition(result)[0]
    assert len(literals) == 1, "the escaped quote was read as a terminator"
    keep = _ERROR_LITERAL_MAX_CHARS - len(_ERROR_LITERAL_ELISION)
    assert f"(+{len(content) - keep} chars elided)" in result
    assert ("b" * 200) not in result, "the tail of the value survived truncation"


def test_an_unterminated_quote_is_left_byte_identical():
    """**Validates: Requirements 6.9**

    8.1's first flagged choice, asserted as documented and agreed with. An
    unterminated quote is not a quoted literal: where the value ends is
    unknowable, and cutting "to end of line" would delete diagnostic text that
    merely happens to follow a stray quote (do-NOT 16) — here, the exception type
    and the column name that follow it.

    Stated as a residual rather than a guarantee: such text is bounded only by
    ``_bound_stderr``'s head-2000 + tail-1500, which ``test_stderr_bound_prop.py``
    owns. If Requirement 6 is later read as demanding a bound on this shape too,
    that is a requirements change and belongs in the requirements — not a silent
    expectation here.
    """
    text = f'For input string: "{"q" * 500} then {_EXCEPTION_NAME} on {_COLUMN_IDENTIFIER}'

    assert _truncate_quoted_literals(text) == text
    assert _EXCEPTION_NAME in text and _COLUMN_IDENTIFIER in text


def test_a_literal_never_spans_a_newline():
    """**Validates: Requirements 6.9**

    8.1's second flagged choice, asserted as documented and agreed with. Lines are
    scanned independently, so a stray quote on one line cannot swallow the rest of
    a multi-line message: the frames and the exception type on the following lines
    survive byte-identical, which is exactly what R6.9 requires and what a
    whole-remainder read would destroy.

    The over-bound literal on the *last* line is still bounded, so this is not the
    trivial case of a helper that gave up at the first stray quote.
    """
    payload = "v" * 300
    text = "\n".join(
        [
            f'Py4JJavaError: an error occurred while calling o42.showString: "{"stray" * 3}',
            f'  File "/opt/spark/python/pyspark/sql/dataframe.py", line 1, in {_COLUMN_IDENTIFIER}',
            f'{_EXCEPTION_NAME}: For input string: "{payload}"',
        ]
    )
    result = _truncate_quoted_literals(text)

    _assert_truncation_relation(text, result)

    result_lines = result.split("\n")
    assert result_lines[0] == text.split("\n")[0], "the unterminated line was altered"
    assert result_lines[1] == text.split("\n")[1], "a following line was swallowed by the stray quote"
    assert _EXCEPTION_NAME in result_lines[2]
    assert payload not in result, "the last line's over-long literal was not bounded"


@pytest.mark.parametrize(
    ("length", "label"),
    [
        (_ERROR_LITERAL_MAX_CHARS - 1, "one character under the bound"),
        (_ERROR_LITERAL_MAX_CHARS, "exactly at the bound"),
        (_ERROR_LITERAL_MAX_CHARS + 1, "one character over the bound"),
        (_ERROR_LITERAL_MAX_CHARS * 20, "well over the bound"),
    ],
)
def test_the_bound_holds_at_and_around_the_crossing(length: int, label: str):
    """**Validates: Requirements 6.6, 6.8**

    The task names "a literal exactly at the bound" as a shape the generator must
    reach. Sampling can supply it but cannot promise it, and an off-by-one in a
    ``>`` is precisely the defect a generator that never lands on the boundary
    misses — so the three adjacent lengths are named, and the truncation's own
    decision is asserted against the crossing rather than around it.
    """
    value = "d" * length
    text = f'{_EXCEPTION_NAME}: For input string: "{value}"'
    result = _truncate_quoted_literals(text)

    _assert_truncation_relation(text, result)

    was_truncated = result != text
    assert was_truncated is (length > _ERROR_LITERAL_MAX_CHARS), f"truncation disagreed with the bound {label}"

    _, literals = _partition(result)[0]
    assert len(literals[0]) == min(length, _ERROR_LITERAL_MAX_CHARS)


def test_the_bound_is_the_sixty_four_resolved_decision_4_proposes():
    """**Validates: Requirements 6.6**

    Asserted against the bare literal exactly once, alongside every other arm
    asserting against the imported constant. The two fail for different reasons:
    the imported form fails when truncation stops honouring its own bound, this
    one fails when the *value* is retuned without Requirement 6 being revisited.
    Only this one notices the constant quietly becoming 4096.

    R6.6 also requires the bound to be a **named module constant** rather than an
    inline literal, which is what makes the rest of this file able to import it —
    so the name's existence is part of the requirement, not a convenience.
    """
    assert _ERROR_LITERAL_MAX_CHARS == 64
    assert len(_ERROR_LITERAL_ELISION) == 1, (
        "the elision is documented as one character, so a truncated literal's "
        "rendered content is exactly the bound including it"
    )


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    shorter=st.integers(min_value=_ERROR_LITERAL_MAX_CHARS + 1, max_value=_ERROR_LITERAL_MAX_CHARS * 4),
    extra=st.integers(min_value=1, max_value=_ERROR_LITERAL_MAX_CHARS * 20),
)
def test_two_over_bound_literals_of_different_lengths_do_not_render_identically(shorter: int, extra: int):
    """**Validates: Requirements 6.8**

    R6.8's reason for existing: without the count, a value one character over the
    bound and a value four hundred characters over it render the same, and a value
    being enormous is itself the diagnostic — it is how the delimiter-collapse
    case announces itself. Same prefix, different lengths, so the count is the
    only thing that can distinguish them.
    """
    longer = shorter + extra
    rendered_shorter = _truncate_quoted_literals(f'For input string: "{"s" * shorter}"')
    rendered_longer = _truncate_quoted_literals(f'For input string: "{"s" * longer}"')

    assert rendered_shorter != rendered_longer
    assert str(shorter - (_ERROR_LITERAL_MAX_CHARS - len(_ERROR_LITERAL_ELISION))) in rendered_shorter
    assert str(longer - (_ERROR_LITERAL_MAX_CHARS - len(_ERROR_LITERAL_ELISION))) in rendered_longer


def test_single_quotes_and_backticks_are_not_bounded():
    """**Validates: Requirements 6.9**

    Resolved decision 3, asserted rather than assumed. Spark quotes *identifiers*
    with backticks and single quotes, and a column name is schema rather than
    data — the declared schema already appears throughout the dashboard, and a
    header that disagrees with it is among the likeliest real causes of a
    transform producing zero rows. Bounding these would delete the most
    diagnostically useful part of an ``AnalysisException``.
    """
    long_identifier = "n" * 300
    text = f"AnalysisException: [UNRESOLVED_COLUMN] cannot resolve `{long_identifier}` or '{long_identifier}'"

    assert _truncate_quoted_literals(text) == text


@pytest.mark.parametrize(("label", "value"), _REAL_SHAPES, ids=[label for label, _ in _REAL_SHAPES])
def test_a_real_diagnostic_shape_survives_untouched(label: str, value: str):
    """**Validates: Requirements 6.6, 6.9**

    Resolved decision 4's central claim: every shape that actually defeats a cast
    fits inside the bound with room over, so the bound costs the diagnostic
    nothing on the cases a reviewer meets. A regression that bounded these would
    turn ``For input string: "N/A"`` into a bounded nothing.
    """
    text = _cast_failure_text(value)

    assert len(value) <= _ERROR_LITERAL_MAX_CHARS, f"{label} is not under the bound"
    assert _truncate_quoted_literals(text) == text
    assert value in _truncate_quoted_literals(text)


@pytest.mark.parametrize(("label", "value"), _REAL_SHAPES, ids=[label for label, _ in _REAL_SHAPES])
def test_a_real_shape_inside_a_collapsed_csv_record_is_bounded_and_counted(label: str, value: str):
    """**Validates: Requirements 6.6, 6.8, 6.9**

    The case the bound exists for, per shape: a CSV read with the wrong delimiter
    collapses a whole record into one field, so the "value" is a customer row.
    The recognisable head survives, the row does not, the count says how much
    went, and the exception type and column name outside the quotes are untouched.

    **What the bound does and does not buy, asserted rather than implied.**
    Resolved decision 4 claims a cap at "a recognisable fragment", not at the
    offending value — and with a short leading value such as ``N/A`` the kept 63
    characters reach into the next field or two of the collapsed row. That is the
    decision as written and not a defect: the exposure is *bounded*, not
    eliminated, which is why R6.11 still enumerates every sink. So the assertion
    here is that the record does not survive and its tail fields do not, which is
    the claim the bound actually supports.
    """
    fields = 40
    row = _collapsed_csv_record(value, fields=fields)
    text = _cast_failure_text(row)
    result = _truncate_quoted_literals(text)

    _assert_truncation_relation(text, result)

    assert row not in result, f"the collapsed record survived for {label}"
    assert value in result, f"the recognisable head of {label} did not survive"
    assert "chars elided)" in result
    assert _EXCEPTION_NAME in result
    assert _COLUMN_IDENTIFIER in result
    assert f"customer_{fields - 1}@example.test" not in result, "the tail of the collapsed record escaped"
    assert len(result) < len(text) - fields, "the bound removed almost nothing from a collapsed record"


# ---------------------------------------------------------------------------
# Property 9 through both channels — R6.10 holds per channel, not per sink
# ---------------------------------------------------------------------------


def _error_statement(*, value: str, frames: list[str] | None = None) -> dict:
    """A Glue statement that reached ``AVAILABLE`` and reported an error."""
    return {
        "State": "AVAILABLE",
        "Output": {
            "Status": "error",
            "ErrorName": _EXCEPTION_NAME,
            "ErrorValue": value,
            "Traceback": frames or [],
            "Data": {"TextPlain": ""},
        },
    }


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(content=_literal_contents())
@example(content=_collapsed_csv_record("1,234.56"))
@example(content="e" * _ERROR_LITERAL_MAX_CHARS)
def test_the_exception_field_channel_truncates_and_keeps_the_exception_type(content: str):
    """**Validates: Requirements 6.6, 6.8, 6.9**

    :func:`exception_summary` is the ``ErrorName`` / ``ErrorValue`` channel's one
    site, and it is what reaches ``error_details``, the ERROR log and — via
    :func:`synthesised_traceback` — ``stderr``. So the same relation is asserted
    on the *composed* text this function builds, which is the text the truncation
    is applied to.

    ``.strip()`` is applied here too, because ``exception_summary`` documents it
    and applies it before truncating; composing without it would compare against
    text the function never saw.
    """
    value = f'For input string: "{content}" while casting {_COLUMN_IDENTIFIER}'
    composed = f"{_EXCEPTION_NAME}: {value.strip()}"

    result = exception_summary(_error_statement(value=value))

    _assert_truncation_relation(composed, result)
    # R6.9 through the channel: the exception type is outside the quotes and
    # leads the summary, so it survives byte-identical.
    assert result.startswith(f"{_EXCEPTION_NAME}: ")
    assert _COLUMN_IDENTIFIER in result


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    frame_contents=st.lists(_literal_contents(), min_size=1, max_size=3),
    value=_literal_contents(),
)
@example(frame_contents=[_collapsed_csv_record("2026-08-18T19:45:13Z")], value="N/A")
def test_the_traceback_frame_channel_truncates_and_keeps_the_frames(frame_contents: list[str], value: str):
    """**Validates: Requirements 6.6, 6.8, 6.9**

    The second channel. ``Output.Traceback`` frames bypass
    :func:`exception_summary` entirely — a Py4J-wrapped Java exception commonly
    embeds its message in frame text — so they are truncated where they are
    rendered, and each frame carries the same relation as the helper.

    The header and the frame's own unquoted identifier survive, which is what
    makes a bounded traceback still a traceback.
    """
    frames = [
        f'  File "/opt/spark/python/pyspark/sql/dataframe_{index}.py", line {index}, '
        f'in {_COLUMN_IDENTIFIER}: "{content}"'
        for index, content in enumerate(frame_contents)
    ]
    statement = _error_statement(value=value, frames=frames)

    result = synthesised_traceback(statement)
    result_lines = result.split("\n")

    assert result_lines[0] == _SYNTHESISED_TRACEBACK_HEADER
    assert len(result_lines) == 1 + len(frames) + 1, "a frame or the exception line went missing"

    for frame, rendered in zip(frames, result_lines[1:-1], strict=True):
        _assert_truncation_relation(frame, rendered)
        assert _COLUMN_IDENTIFIER in rendered

    # Every literal anywhere in the rendered traceback is bounded, across both
    # channels at once — the frames' and the exception summary's.
    for _plains, literals in _partition(result):
        for literal in literals:
            assert len(literal) <= _ERROR_LITERAL_MAX_CHARS

    # R6.10 where it is actually falsifiable: this output composes an
    # already-truncated exception summary with freshly truncated frames, so a
    # non-idempotent truncation would double-elide HERE first.
    assert _truncate_quoted_literals(result) == result
    assert result.endswith(exception_summary(statement))


def test_a_traceback_of_only_short_literals_is_untouched_end_to_end():
    """**Validates: Requirements 6.6, 6.9**

    The negative that keeps the channel arms from passing vacuously: with nothing
    over the bound, neither channel may alter a single byte, and no elision marker
    may appear anywhere. A truncation that always rewrote its input would fail
    here while satisfying every bound above.
    """
    frames = [
        '  File "/opt/spark/python/pyspark/sql/dataframe.py", line 1, in showString',
        f'  File "/tmp/transform.py", line 7, in {_COLUMN_IDENTIFIER}',
    ]
    statement = _error_statement(value='For input string: "1,234.56"', frames=frames)

    result = synthesised_traceback(statement)

    assert result == "\n".join(
        [_SYNTHESISED_TRACEBACK_HEADER, *frames, f'{_EXCEPTION_NAME}: For input string: "1,234.56"']
    )
    assert "elided" not in result
