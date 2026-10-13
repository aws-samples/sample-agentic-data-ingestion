# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`_dq` and `_raw` in isolation — the round-trip that DEFINES them (p41 task 4.4).

**Validates: Requirements 2.1, 2.12, Property 1**

══════════════════════════════════════════════════════════════════════
ORACLE RULE FOR THIS FILE: PYTHON LITERAL SEMANTICS. `ast.literal_eval`
AND THE COMPILER ARE THE ORACLE — NEVER CURRENT CONVERTER OUTPUT.
READ THIS BEFORE EDITING.
══════════════════════════════════════════════════════════════════════

`LGR-28b4d75-5` (template / ratified): *a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle.* p41 runs two opposite rules inside `tests/unit/lambdas/test_code/`,
and this file follows the semantics one — here the semantics are **Python's**,
not a regex engine's.

Nothing below was transcribed from what `_dq` or `_raw` returns. Every
expectation is one of:

- **`ast.literal_eval`** — what the literal the helper builds actually means.
- **`compile(..., "eval")`** — whether that literal is even *syntax*.
- **Python's grammar for a raw fence**, checked by attempting the fence rather
  than by reasoning about it.

So a `_dq` rewritten tomorrow from a different primitive still passes if it is
correct, and fails if it is not. That is the property `design.md` §5.1 and
`tasks.md` 4.1 both asked for in as many words: *"Derive it from a
round-trip-safe primitive rather than two hand-rolled substitutions, and let
4.4's round-trip test be the definition."*

**This file IS that definition.** It is not incidental coverage of two small
helpers. `_dq` and `_raw` are the whole of the value contract's escaping half —
`_string` returns a literal's *value*, and these two put that value back behind
the fence each of the 7 wrap points emits into (`tasks.md` 4.3). If the
round-trip does not hold, the converter emits pandas that either means
something else or does not parse.

**The files that follow the OPPOSITE rule, where current converter output IS
the oracle,** and which must not be reconciled with anything here:

- `test_string_site_preservation.py` (task 3.1) — the 15-site plain-literal
  pins, transcribed from the unfixed converter.
- `test_passthrough_and_alias_refusal.py`'s `TestPassthrough*` and
  `TestAlias*Today*` classes (tasks 1.1 / 1.2's today-pins).

──────────────────────────────────────────────────────────────────────
ENCODING, PARSING AND EVALUATING ARE THREE DIFFERENT FAILURES, AND
THE WORST ONE IS FIRST
──────────────────────────────────────────────────────────────────────

A fence that does not round-trip fails in one of three ways, and this file keeps
them apart on purpose (`p41_evaluate_fence` reports which):

- **NOT_UTF8_ENCODABLE** — the emitted text is not something a source file can
  hold. `.encode("utf-8")` raises, so the module cannot be **written** and
  cannot be **shipped** to the code interpreter; the failure happens before any
  parser sees it, at the point where the converter's output becomes a file. This
  is the lone-surrogate class (see below), and it is the worst of the three
  because the failure surfaces nowhere near the literal that caused it — the
  write fails, and there is no module to point at. **Checked first**, because
  asking whether unencodable text parses is asking the wrong question:
  `compile()` reports it as a `UnicodeEncodeError`, which this file would
  otherwise mislabel `DID_NOT_PARSE` and send a reader looking for a grammar
  problem that is not there.
- **DID_NOT_PARSE** — the emitted pandas is a `SyntaxError`. The generated
  module does not import, so *nothing* runs: not the transform under test, not
  the rest of the block. `regexp_replace` becomes a syntax error two hundred
  lines away from the literal that caused it. This is the failure a naive
  substitution-list `_dq` produces for a value carrying a line break, and it is
  strictly worse than a wrong answer because there is no answer at all.
- **PARSED_BUT_WRONG_VALUE** — the pandas runs and means something else. This
  is D3 (a different regex) and D5 (a column named with a backspace).

`tasks.md` bite 4 and `design.md` §5.1 both name the second one; the third was
found by this file's own sweeps. The assertions below make the distinction
visible in the failure message rather than leaving a reader to infer it from an
exception type — and `UnicodeEncodeError` is a `ValueError` subclass, so without
the first gate the two collapse into one indistinguishable outcome.

──────────────────────────────────────────────────────────────────────
EXPECTED-RED WINDOW: NONE — task 4.1 has landed
──────────────────────────────────────────────────────────────────────

`tasks.md` 4.4 lists this file as red until 4.1. **4.1 has landed, so every
test here is green on the first run.** A red is therefore a **finding about
`_dq` or `_raw`** and must be investigated and reported — not adjusted away, and
not fixed by editing a helper from this file's side.

──────────────────────────────────────────────────────────────────────
A FINDING THIS FILE FOUND, AND NOW PINS: LONE SURROGATES
──────────────────────────────────────────────────────────────────────

**Found by this file's sweeps, and since CLOSED in production. The assertions
below are now real pins, not a record.** The history is kept because the pins
only make sense next to the failure they prevent.

**What was found.** `_RAW_UNSPELLABLE` had four members and there was a **fifth
class it did not cover**: a lone surrogate, `U+D800`–`U+DFFF`. The value is
reachable from plain-ASCII source — `ast.parse('x = "\\ud800"')` yields a
one-character string that *is* a lone surrogate — and `_raw_spellable` returned
`True` for it, so `_raw` emitted the surrogate literally into the `r"…"` fence.
Both raw sites converted, and both emitted modules then failed
`.encode("utf-8")`:

    UnicodeEncodeError: 'utf-8' codec can't encode character '\\ud800' …

so the module could be neither written to a file nor shipped to the code
interpreter. That is `bugfix.md` 2.16's class — a *successful* conversion
producing unusable output — and the same class of hole as the `\\x00` exclusion
task 4.1 added beyond the design's three, one code-point range further out.
Zero occurrences in the 56 measured generated versions.

`_dq` handled the same value correctly all along (`repr` spells it back as the
ASCII escape `\\ud800`), so the **twelve plain-fence sites were never exposed** —
only the two `r"…"` regex sites were.

**What closed it.** `_raw_spellable` gained a fifth exclusion: a value a UTF-8
source file cannot express is not raw-spellable, so it takes the existing `_dq`
fallback. No new machinery — the fallback already handled the value correctly.
The exclusion is tested by the operation that actually fails
(`value.encode("utf-8")`) rather than by enumerating 2048 code points; see
`_raw_spellable`'s comment for why, and
`TestTheSweptDomainIsTheEmittableOne::test_the_encodability_test_and_the_surrogate_range_are_co_extensive`
for the sweep that shows the two formulations agree exactly.

**Consequence for this file:** the sweeps now **include** the surrogate range
rather than stopping below it — `P41_SWEEP_CEILING` moved from `0x3000` to
`0xE000` for exactly that reason, and `TestLoneSurrogateExhaustiveSweep` covers
all 2048 points individually. And "can be encoded at all" is now a **third**
failure mode in this file's contract, alongside "parses" and "means the right
thing": see `P41_NOT_UTF8_ENCODABLE` below.

──────────────────────────────────────────────────────────────────────
Scope notes
──────────────────────────────────────────────────────────────────────

`_dq` and `_raw` **in isolation** — no `convert()` call anywhere in this file.
The converter-level assertions live in `test_raw_literal_regex_fidelity.py`
(tasks 2.1 / 2.4 / 7.1) and `test_raw_literal_plain_sites.py` (tasks 2.2 /
6.1). A helper-level round-trip and a site-level emission are different claims,
and this file makes only the first.

The generative sweep here is a **supporting sweep, not one of the design's six
numbered correctness properties**. It is deliberately untagged: the six
numbered properties are claims about the converter, and adding a seventh from a
unit-test task would put a property in the registry the design never ratified.
`design.md`'s "Unit tests" section asks for exactly this, under exactly this
framing: *"`_dq` and `_raw` in isolation: round-trip every value through each
fence and `ast.literal_eval` it back. This is where `_raw`'s plain-fence
fallback is pinned."*

pandas is **not** installed in this venv and must not become one
(`bugfix.md` 3.14). Nothing here needs it: a fence's round-trip is a fact about
Python, and the two helpers are pure string functions. No shared fixture from
`tests/fixtures/conftest.py` is relevant.
"""

import ast
import itertools
import os
import sys
from typing import NamedTuple

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# Add Lambda source to path (same shape as the rest of this directory).
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import _RAW_UNSPELLABLE, _dq, _raw, _raw_spellable

#: Hypothesis budget. `tasks.md` 4.4 asks for at least 100 iterations over the
#: round-trip; 300 is cheap here because both helpers are pure string functions
#: and the oracle is a single `compile` + `literal_eval`.
P41_SWEEP_SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])

#: Upper bound of the exhaustive single-code-point sweep.
#:
#: **Was `0x3000`, raised to `0xE000`.** The original bound was the range task
#: 4.1's `\x00` claim was about — *"the only code point below U+3000 the
#: three-character test gets wrong"* — and a test asserted the ceiling sat
#: BELOW U+D800 so that the sweep's green could not be hiding the lone-surrogate
#: finding this file recorded. That finding is now fixed, so the ceiling moves
#: past it instead: `0xE000` is the first code point above the surrogate block,
#: so the sweep now **contains** U+D800–U+DFFF and the exclusion is exercised
#: rather than avoided. `TestTheSweptDomainIsTheEmittableOne` asserts the new
#: direction, amended in place with this reason.
#:
#: Not raised to `0x110000`: the full range is 1,114,112 points and the sweep
#: does a `compile` plus a `literal_eval` per point per fence, which costs ~40 s
#: — over this suite's budget for one test. The whole range **is** covered, by
#: the cheaper encodability-only sweep in
#: `TestTheSweptDomainIsTheEmittableOne::test_no_code_point_anywhere_claims_the_raw_arm_unencodably`,
#: which is the exact property the surrogate class broke.
P41_SWEEP_CEILING = 0xE000

#: The lone-surrogate block: the fifth class `_raw_spellable` excludes, and the
#: only code points a UTF-8 source file cannot carry. Written as a range because
#: that is what it is — the production check tests it by `.encode("utf-8")`
#: rather than by enumeration, and
#: `test_the_encodability_test_and_the_surrogate_range_are_co_extensive` is the
#: sweep that shows the two formulations agree over every code point.
P41_SURROGATE_FIRST = 0xD800
P41_SURROGATE_LAST = 0xDFFF

#: The single code points that cannot be spelled in a raw fence **because of the
#: literal's own syntax** — derived from Python's grammar, not from
#: `_RAW_UNSPELLABLE`, and checked against the compiler in
#: `test_raw_spellable_matches_what_the_compiler_accepts`.
#:
#: The first four are `_RAW_UNSPELLABLE`; `\\` is the fifth and arrives by a
#: different rule — a lone backslash is an *odd trailing run*, so it escapes the
#: closing quote rather than being unspellable in itself.
#:
#: **This set is deliberately NOT the whole of what `_raw_spellable` refuses.**
#: The lone-surrogate block refuses for a third reason — the emitted module is
#: unencodable, not the literal unparseable — and is kept in
#: :data:`P41_SURROGATE_FIRST` / :data:`P41_SURROGATE_LAST` rather than folded in
#: here, so a reader can see which mechanism a given exclusion belongs to.
P41_UNSPELLABLE_SINGLETONS = frozenset({'"', "\n", "\r", "\x00", "\\"})


# ──────────────────────────────────────────────────────────────────────
# Helpers. `p41_` prefixed per `design.md` §10 — and note the standing
# naming fence: `is_bug_condition_d3` already exists at
# `test_converter_equivalence_baseline.py:910` and means an EARLIER
# spec's Defect 3, so no p41 helper may take that name.
# ──────────────────────────────────────────────────────────────────────

P41_NOT_UTF8_ENCODABLE = "NOT_UTF8_ENCODABLE"
P41_DID_NOT_PARSE = "DID_NOT_PARSE"
P41_PARSED_BUT_WRONG_VALUE = "PARSED_BUT_WRONG_VALUE"
P41_PARSED_AND_EQUAL = "PARSED_AND_EQUAL"


class P41FenceOutcome(NamedTuple):
    """What a candidate literal actually is, as Python sees it.

    Four outcomes rather than a boolean, because a fence that does not
    round-trip fails in three very different ways and conflating them loses the
    information a reader needs (see the file docstring). `outcome` is one of
    `P41_NOT_UTF8_ENCODABLE`, `P41_DID_NOT_PARSE`, `P41_PARSED_BUT_WRONG_VALUE`,
    `P41_PARSED_AND_EQUAL`.
    """

    outcome: str
    literal: str
    value: "str | None"
    error: str


def p41_evaluate_fence(literal: str, expected: str) -> P41FenceOutcome:
    """Classify *literal* against *expected* using the compiler as the oracle.

    Three gates, in the order the emitted module would hit them.

    **Encodability is checked FIRST**, because a literal that cannot be written
    to a UTF-8 source file never reaches a parser at all: the converter's output
    fails at the write, or at the ship to the code interpreter, and there is no
    module to diagnose. This is the lone-surrogate class the file docstring
    records. It has to be its own gate rather than folded into the parse check,
    because `UnicodeEncodeError` **is a `ValueError` subclass**, so `compile()`
    raising it below would otherwise be reported as `DID_NOT_PARSE` and point a
    reader at a grammar problem that does not exist.

    Parsing is then checked separately from evaluating, because a `SyntaxError`
    in the emitted pandas is a worse failure than a wrong value and is the one a
    substitution-list `_dq` produces. `compile(..., "eval")` is what the
    generated module's own import would do to this text.

    `ValueError` is caught alongside `SyntaxError`: CPython reports a NUL in
    source through either, depending on version, and the *class* of the
    exception is not the thing under test — whether the text is usable Python is.
    """
    try:
        literal.encode("utf-8")
    except UnicodeEncodeError as exc:
        return P41FenceOutcome(P41_NOT_UTF8_ENCODABLE, literal, None, f"{type(exc).__name__}: {exc}")

    try:
        compile(literal, "<p41-fence>", "eval")
    except (SyntaxError, ValueError) as exc:
        return P41FenceOutcome(P41_DID_NOT_PARSE, literal, None, f"{type(exc).__name__}: {exc}")

    try:
        value = ast.literal_eval(literal)
    except (SyntaxError, ValueError) as exc:  # pragma: no cover - compile already accepted it
        return P41FenceOutcome(P41_DID_NOT_PARSE, literal, None, f"{type(exc).__name__}: {exc}")

    if value != expected:
        return P41FenceOutcome(P41_PARSED_BUT_WRONG_VALUE, literal, value, "")
    return P41FenceOutcome(P41_PARSED_AND_EQUAL, literal, value, "")


def p41_plain_literal(value: str) -> str:
    """The plain-fence literal `_dq` builds for *value*: `"<body>"`."""
    return '"' + _dq(value) + '"'


def p41_raw_literal(value: str) -> str:
    """The literal `_raw` builds for *value*, under whichever fence it chose."""
    prefix, body = _raw(value)
    return prefix + '"' + body + '"'


def p41_assert_round_trip(literal: str, value: str, *, fence: str, case_id: str = "") -> None:
    """*literal* must PARSE and must mean *value* — the definition, asserted.

    The message spells out which of the two failure modes happened, because
    "does not parse" and "means something else" send a reader to different
    places in `pyspark_to_pandas.py`.
    """
    got = p41_evaluate_fence(literal, value)
    if got.outcome == P41_PARSED_AND_EQUAL:
        return

    where = f"[{case_id}] " if case_id else ""
    if got.outcome == P41_NOT_UTF8_ENCODABLE:
        pytest.fail(
            f"{where}{fence} fence IS NOT UTF-8 ENCODABLE — the emitted module cannot be\n"
            f"written to a file or shipped to the code interpreter, so the failure surfaces\n"
            f"at the write and nowhere near this literal.\n"
            f"  value   : {value!r}\n"
            f"  literal : {got.literal!r}\n"
            f"  error   : {got.error}\n"
            "This is the lone-surrogate class (see this file's docstring); `_raw_spellable`'s\n"
            "fifth exclusion exists to route such a value onto `_dq`'s ASCII-escaping fallback."
        )
    if got.outcome == P41_DID_NOT_PARSE:
        pytest.fail(
            f"{where}{fence} fence DID NOT PARSE — the emitted pandas would be a SyntaxError,\n"
            f"so nothing in the generated module runs at all.\n"
            f"  value   : {value!r}\n"
            f"  literal : {got.literal!r}\n"
            f"  error   : {got.error}\n"
            "This is the worse of the two round-trip failures (see this file's docstring)."
        )
    pytest.fail(
        f"{where}{fence} fence PARSED BUT MEANS SOMETHING ELSE — the emitted pandas runs\n"
        f"and is wrong, which is D3 at a regex site and D5 at a column-name site.\n"
        f"  value expected: {value!r}\n"
        f"  literal       : {got.literal!r}\n"
        f"  value got     : {got.value!r}"
    )


def p41_naive_dq_substitution_list(value: str) -> str:
    """The `_dq` `design.md` §5.1 rejects: escape `\\` and `"`, nothing else.

    Present only as the counter-example the real `_dq` is measured against. It
    is *not* an alternative implementation and must never be used to build an
    expectation — it is the thing that emits `"a<LF>b"`, a `SyntaxError`.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def p41_naive_dq_quote_only(value: str) -> str:
    """A `_dq` that escapes the quote but forgets the backslash.

    The other naive spelling, kept because it fails in the *other* direction:
    it parses and means something else. `a\\b` comes back as `a<BACKSPACE>b`,
    which is D5's exact shape — a column named with a control character.
    """
    return value.replace('"', '\\"')


def p41_naive_raw_fence(value: str) -> str:
    """`r"<value>"` with no spellability check — what the fallback prevents."""
    return 'r"' + value + '"'


def p41_utf8_source_safe(value: str) -> bool:
    """Can a UTF-8 Python source file carry *value* literally?

    False for lone surrogates only. This is the boundary of the domain the
    sweeps below cover, and the reason is stated in the file docstring: an
    emitted module that cannot be encoded to UTF-8 cannot be written or shipped,
    so it is not a fence question.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# The case table. Every row is exercised through BOTH fences and its arm
# is asserted, so a value silently changing arms is visible.
# ──────────────────────────────────────────────────────────────────────


class P41FenceCase(NamedTuple):
    case_id: str
    value: str
    raw_arm: bool  # True => `_raw` must return ("r", value); False => the fallback
    note: str


#: Values that must stay on the RAW arm. The point of the arm assertion is that
#: the exclusion set is not over-broad: every one of these is a value a regex
#: could plausibly carry, and pushing one onto the fallback would change the
#: emitted spelling of a pattern for no reason.
P41_RAW_ARM_CASES: tuple[P41FenceCase, ...] = (
    P41FenceCase("plain-ascii", "plain", True, "the ordinary case — no escaping needed at all"),
    P41FenceCase("regex-negated-digit-class", r"[^\d]", True, "the measured D1 shape (bugfix.md 1.1)"),
    P41FenceCase("regex-no-backslash", "[^0-9+]", True, "no backslash anywhere — bugfix.md 1.2"),
    P41FenceCase("regex-escaped-dot-anchor", r"@example\.com$", True, "the one backslash in the 79 baseline cases"),
    P41FenceCase("single-quote-inside", "a'b", True, 'legal bare inside a `"` fence; repr would switch quotes'),
    P41FenceCase("tab", "a\tb", True, "THE boundary case — legal unescaped in both fences"),
    P41FenceCase("tab-only", "\t", True, "tab alone, so the check cannot pass by neighbouring characters"),
    P41FenceCase("even-trailing-backslash-run", "a\\\\", True, "an EVEN run — a raw fence keeps both"),
    P41FenceCase("four-trailing-backslashes", "a\\\\\\\\", True, "still even"),
    P41FenceCase("interior-backslash", "a\\b", True, "not trailing, so the odd-run rule does not apply"),
    P41FenceCase("vertical-tab-and-form-feed", "a\x0b\x0cb", True, "control characters that a raw fence CAN carry"),
    P41FenceCase("non-ascii", "café_naïve_日本語", True, "left as itself, not \\x-escaped — _dq's docstring"),
    P41FenceCase("empty", "", True, "the degenerate value"),
)

#: Values that MUST force the plain-fence fallback. `design.md` §5.1 names the
#: first three; task 4.1 found `contains-nul` empirically and its report
#: recommends pinning it alongside them. `contains-carriage-return` is the
#: newline's twin, already a member of `_RAW_UNSPELLABLE`.
P41_FALLBACK_ARM_CASES: tuple[P41FenceCase, ...] = (
    P41FenceCase("contains-double-quote", 'a"b', False, "the fence quote — closes the literal early"),
    P41FenceCase("odd-trailing-backslash-run", "a\\", False, "escapes the closing quote"),
    P41FenceCase("contains-newline", "a\nb", False, "a raw fence cannot express a line break at all"),
    P41FenceCase("contains-nul", "a\x00b", False, "task 4.1's fourth exclusion, found by sweeping"),
    P41FenceCase("contains-carriage-return", "a\rb", False, "LF's twin"),
    P41FenceCase("three-trailing-backslashes", "a\\\\\\", False, "an ODD run of three"),
    P41FenceCase("quote-and-backslash", 'a\\"b', False, "both mechanisms in one value"),
    P41FenceCase("newline-and-quote", 'a\n"b', False, "both fatal characters in one value"),
    P41FenceCase("double-quote-only", '"', False, "the bare fence quote"),
    P41FenceCase("backslash-only", "\\", False, "a lone backslash IS an odd trailing run"),
    P41FenceCase(
        "lone-surrogate",
        "\ud800",
        False,
        "the FIFTH exclusion and the third mechanism — unencodable module, not unparseable literal; "
        "pinned in full by TestTheSweptDomainIsTheEmittableOne and TestLoneSurrogateExhaustiveSweep",
    ),
    P41FenceCase(
        "lone-surrogate-in-a-regex-class",
        "[^\ud800]",
        False,
        "the shape a model would actually write — a \\uD800 escape inside a regex character class",
    ),
)

P41_ALL_CASES: tuple[P41FenceCase, ...] = P41_RAW_ARM_CASES + P41_FALLBACK_ARM_CASES


class P41FallbackShape(NamedTuple):
    """A fallback-forcing shape, with the emitted-code failure it prevents."""

    case_id: str
    value: str
    naive_raw_error_fragment: str
    prevents: str


#: The four shapes `tasks.md` 4.4 requires pinned individually, each with the
#: `SyntaxError` the fallback prevents — asserted, not described, by attempting
#: the unchecked raw fence and reading what the compiler says.
P41_FALLBACK_SHAPES: tuple[P41FallbackShape, ...] = (
    P41FallbackShape(
        "contains-double-quote",
        'a"b',
        "unterminated string literal",
        'r"a"b" closes at the value\'s own quote, leaving `b"` dangling: the emitted '
        "pandas line is a SyntaxError and the whole generated module fails to import.",
    ),
    P41FallbackShape(
        "odd-trailing-backslash-run",
        "a\\",
        "perhaps you escaped the end quote",
        'r"a\\" ends in a backslash that escapes the closing quote, so the literal '
        "swallows the rest of the line — again a SyntaxError, not a wrong value.",
    ),
    P41FallbackShape(
        "contains-newline",
        "a\nb",
        "unterminated string literal",
        "a raw fence cannot express a line break AT ALL, so the value that a plain "
        '`"a\\nb"` literal parses to cannot be re-emitted raw. This is the case a '
        'substitution-list `_dq` and a `"`-and-backslash-only spellability test both '
        "get wrong, and the case task 7.1 reaches.",
    ),
    P41FallbackShape(
        "contains-nul",
        "a\x00b",
        "source code string cannot contain null bytes",
        "the compiler refuses the SOURCE outright, before any literal is parsed — a "
        "different message from the other three and the reason task 4.1 added a fourth "
        "exclusion the design did not name. Reachable through an escaped `\\x00` in the "
        "model's own literal, which parses fine and yields a NUL-bearing value.",
    ),
    P41FallbackShape(
        "contains-carriage-return",
        "a\rb",
        "unterminated string literal",
        "LF's twin, and already a member of `_RAW_UNSPELLABLE`. Pinned beside the four "
        "so a future narrowing of the set has to move a test.",
    ),
)


def p41_case_ids(cases: "tuple[P41FenceCase, ...]") -> list[str]:
    return [case.case_id for case in cases]


# ══════════════════════════════════════════════════════════════════════
# THE DEFINITION — the round-trip, through each fence
# ══════════════════════════════════════════════════════════════════════


class TestPlainFenceRoundTripIsTheDefinition:
    """`ast.literal_eval('"' + _dq(v) + '"') == v`, for every value `v`.

    Stated as the definition and not as a spot check: `design.md` §5.1 and
    `tasks.md` 4.1 both defer `_dq`'s contract to this assertion, so if it is
    weakened there is nothing left that says what `_dq` is.
    """

    @pytest.mark.parametrize("case", P41_ALL_CASES, ids=p41_case_ids(P41_ALL_CASES))
    def test_dq_body_inside_a_plain_fence_evaluates_to_the_value(self, case: P41FenceCase) -> None:
        p41_assert_round_trip(p41_plain_literal(case.value), case.value, fence='plain "…"', case_id=case.case_id)

    @pytest.mark.parametrize("case", P41_ALL_CASES, ids=p41_case_ids(P41_ALL_CASES))
    def test_dq_is_total_over_the_table_including_the_shapes_that_break_a_raw_fence(self, case: P41FenceCase) -> None:
        """`_dq` has NO fallback, so it must be correct for every value.

        `_raw` may hand a value off; `_dq` is the one it hands off *to*. Every
        fallback-arm case above therefore has to round-trip here, or the
        fallback is not a fallback.
        """
        assert p41_evaluate_fence(p41_plain_literal(case.value), case.value).outcome == P41_PARSED_AND_EQUAL


class TestRawFenceRoundTripIsTheDefinition:
    """`(prefix, body) = _raw(v)` ⟹ `literal_eval(prefix + '"' + body + '"') == v`.

    Asserted over the fence `_raw` chose, whichever arm that is — the contract
    is about the *value* reaching pandas, not about which spelling it took.
    """

    @pytest.mark.parametrize("case", P41_ALL_CASES, ids=p41_case_ids(P41_ALL_CASES))
    def test_raw_fence_evaluates_to_the_value(self, case: P41FenceCase) -> None:
        p41_assert_round_trip(p41_raw_literal(case.value), case.value, fence="_raw's chosen", case_id=case.case_id)

    @pytest.mark.parametrize("case", P41_ALL_CASES, ids=p41_case_ids(P41_ALL_CASES))
    def test_raw_returns_a_prefix_that_is_either_r_or_empty(self, case: P41FenceCase) -> None:
        """No third fence. A prefix outside `{"r", ""}` is a new code path."""
        prefix, _body = _raw(case.value)
        assert prefix in ("r", ""), f"{case.case_id}: unexpected fence prefix {prefix!r}"


# ══════════════════════════════════════════════════════════════════════
# PARSING IS A SEPARATE CLAIM FROM EVALUATING
# ══════════════════════════════════════════════════════════════════════


class TestTheEmittedLiteralMustParse:
    """The two failure modes, kept apart and each demonstrated.

    `tasks.md` 4.4: *"The emitted literal must PARSE, not merely evaluate — a
    fence that does not round-trip produces a `SyntaxError` in the generated
    pandas, which is a different and worse failure than a wrong value."*
    """

    @pytest.mark.parametrize("case", P41_ALL_CASES, ids=p41_case_ids(P41_ALL_CASES))
    def test_both_fences_are_syntax_before_they_are_anything_else(self, case: P41FenceCase) -> None:
        for fence, literal in (("plain", p41_plain_literal(case.value)), ("_raw", p41_raw_literal(case.value))):
            try:
                compile(literal, "<p41-fence>", "eval")
            except (SyntaxError, ValueError) as exc:
                pytest.fail(
                    f"[{case.case_id}] the {fence} fence is not valid Python:\n"
                    f"  value   : {case.value!r}\n"
                    f"  literal : {literal!r}\n"
                    f"  error   : {type(exc).__name__}: {exc}"
                )

    def test_the_substitution_list_dq_emits_code_that_does_not_parse(self) -> None:
        """The rejected `_dq`, and the exact newline case it gets wrong.

        `design.md` §5.1: *"Escaping only `\\` and `"` is NOT sufficient — a
        value carrying a newline or carriage return emits a literal that does
        not parse."* Asserted here against the compiler so the claim is checked
        rather than repeated, and so a future rewrite of `_dq` back to a
        substitution list fails a test that names the reason.
        """
        value = "a\nb"

        naive = p41_evaluate_fence('"' + p41_naive_dq_substitution_list(value) + '"', value)
        assert naive.outcome == P41_DID_NOT_PARSE, (
            f"expected the substitution-list _dq to emit unparseable code, got {naive.outcome}: {naive!r}"
        )
        assert "unterminated string literal" in naive.error

        real = p41_evaluate_fence(p41_plain_literal(value), value)
        assert real.outcome == P41_PARSED_AND_EQUAL, f"the real _dq must handle the same value: {real!r}"

    @pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\x00b"], ids=["newline", "carriage-return", "nul"])
    def test_every_control_character_the_substitution_list_swallows(self, value: str) -> None:
        """Not one hole but a class of them — the same gap for LF, CR and NUL."""
        assert p41_evaluate_fence('"' + p41_naive_dq_substitution_list(value) + '"', value).outcome == (
            P41_DID_NOT_PARSE
        )
        assert p41_evaluate_fence(p41_plain_literal(value), value).outcome == P41_PARSED_AND_EQUAL

    def test_a_wrong_value_is_a_different_failure_from_a_syntax_error(self) -> None:
        """The other naive `_dq` parses and lies — D5's exact shape.

        Forgetting the backslash rather than the newline produces
        `PARSED_BUT_WRONG_VALUE`: the three-character value `a\\b` comes back as
        the **two**-character `a<BACKSPACE>`, because `\\b` in a plain fence
        consumes the `b` as well. That is precisely task 6.1's requirement read
        from the other side — *"emits a column named `a\\b` — two characters,
        backslash and b — never `a\\x08`, the backspace that a plain fence would
        resolve `"a\\b"` to"* — and the length change is what makes it a value
        corruption rather than a re-spelling.

        Both naive spellings are wrong; they are wrong in ways that need
        different diagnoses, which is why `p41_evaluate_fence` reports three
        outcomes and not a boolean.
        """
        value = "a\\b"
        assert len(value) == 3, "the value under test is a, backslash, b"

        quote_only = p41_evaluate_fence('"' + p41_naive_dq_quote_only(value) + '"', value)
        assert quote_only.outcome == P41_PARSED_BUT_WRONG_VALUE, quote_only
        assert quote_only.value == "a\x08", f"expected the backspace D5 is about, got {quote_only.value!r}"
        assert quote_only.value is not None and len(quote_only.value) == 2, (
            "the escape swallowed the following character — a length change, not a re-spelling"
        )

        assert p41_evaluate_fence(p41_plain_literal(value), value).outcome == P41_PARSED_AND_EQUAL


# ══════════════════════════════════════════════════════════════════════
# THE FALLBACK — pinned shape by shape, with what each prevents
# ══════════════════════════════════════════════════════════════════════


class TestTheFourFallbackForcingShapes:
    """`_raw`'s plain-fence fallback is a third code path, so it is PINNED.

    `tasks.md` 4.1: *"The fallback is a third code path and it is load-bearing"*
    — and 4.4 asks for the shapes that force it to be pinned individually
    rather than merely reached. Five rows: the three `design.md` §5.1 names, the
    `\\x00` task 4.1 found empirically and recommends pinning alongside them,
    and CR as LF's twin.
    """

    @pytest.mark.parametrize("shape", P41_FALLBACK_SHAPES, ids=[s.case_id for s in P41_FALLBACK_SHAPES])
    def test_the_value_is_not_raw_spellable(self, shape: P41FallbackShape) -> None:
        assert _raw_spellable(shape.value) is False, f"{shape.case_id}: {shape.prevents}"

    @pytest.mark.parametrize("shape", P41_FALLBACK_SHAPES, ids=[s.case_id for s in P41_FALLBACK_SHAPES])
    def test_raw_takes_the_plain_fence_arm(self, shape: P41FallbackShape) -> None:
        prefix, body = _raw(shape.value)
        assert prefix == "", f"{shape.case_id} must fall back, got prefix {prefix!r}"
        assert body == _dq(shape.value), f"{shape.case_id}: the fallback body must be exactly _dq's"

    @pytest.mark.parametrize("shape", P41_FALLBACK_SHAPES, ids=[s.case_id for s in P41_FALLBACK_SHAPES])
    def test_the_unchecked_raw_fence_this_prevents_does_not_parse(self, shape: P41FallbackShape) -> None:
        """The emitted-code failure each fallback prevents, read off the compiler.

        This is the assertion that makes the fallback's *reason* checkable
        instead of documented: `r"<value>"` with no spellability check is not a
        wrong pattern, it is a `SyntaxError`, and the message names why.
        """
        unchecked = p41_evaluate_fence(p41_naive_raw_fence(shape.value), shape.value)
        assert unchecked.outcome == P41_DID_NOT_PARSE, (
            f"{shape.case_id}: expected an unchecked raw fence to be a SyntaxError.\n"
            f"  prevents: {shape.prevents}\n"
            f"  got     : {unchecked!r}"
        )
        assert shape.naive_raw_error_fragment in unchecked.error, (
            f"{shape.case_id}: expected {shape.naive_raw_error_fragment!r} in {unchecked.error!r}"
        )

    @pytest.mark.parametrize("shape", P41_FALLBACK_SHAPES, ids=[s.case_id for s in P41_FALLBACK_SHAPES])
    def test_the_fallback_round_trips_the_same_value(self, shape: P41FallbackShape) -> None:
        """Changing the spelling, not the meaning — `_raw`'s stated premise."""
        p41_assert_round_trip(p41_raw_literal(shape.value), shape.value, fence="fallback", case_id=shape.case_id)

    def test_the_five_pinned_shapes_are_the_documented_exclusion_set_plus_the_backslash_rule(self) -> None:
        """The table is not an arbitrary five — it covers both mechanisms.

        Four characters are unspellable in themselves (`_RAW_UNSPELLABLE`); the
        fifth shape arrives by the odd-trailing-run rule instead. If a member
        were dropped from either mechanism, one of these two sets moves.
        """
        assert set(_RAW_UNSPELLABLE) == {'"', "\n", "\r", "\x00"}, (
            f"_RAW_UNSPELLABLE changed: {_RAW_UNSPELLABLE!r}. It is deliberately still four "
            "members — the lone-surrogate class is `_raw_spellable`'s THIRD mechanism, tested by "
            "`.encode('utf-8')` rather than by adding 2048 entries here, and pinned in "
            "TestTheSweptDomainIsTheEmittableOne / TestLoneSurrogateExhaustiveSweep."
        )
        by_character = {s.case_id for s in P41_FALLBACK_SHAPES if any(c in s.value for c in _RAW_UNSPELLABLE)}
        by_trailing_run = {s.case_id for s in P41_FALLBACK_SHAPES if s.value.endswith("\\")}
        assert by_character, "no pinned shape exercises the unspellable-character mechanism"
        assert by_trailing_run, "no pinned shape exercises the odd-trailing-run mechanism"


# ══════════════════════════════════════════════════════════════════════
# THE RAW ARM IS NOT OVER-BROAD
# ══════════════════════════════════════════════════════════════════════


class TestTabStaysOnTheRawArm:
    """Tab is the boundary case that says the exclusion set is not over-broad.

    `_RAW_UNSPELLABLE`'s own comment: *"TAB is deliberately NOT here: it is
    legal unescaped in a raw fence and round-trips through one, so excluding it
    would push values onto the fallback for no reason."* Asserted here, because
    a comment does not fail when someone adds it to the tuple.
    """

    def test_tab_is_not_in_the_exclusion_set(self) -> None:
        assert "\t" not in _RAW_UNSPELLABLE

    @pytest.mark.parametrize(
        "value", ["\t", "a\tb", "\ta", "a\t", "a\t\tb"], ids=["alone", "middle", "leading", "trailing", "doubled"]
    )
    def test_a_tab_bearing_value_is_raw_spellable_and_takes_the_raw_arm(self, value: str) -> None:
        assert _raw_spellable(value) is True
        assert _raw(value) == ("r", value), "a tab must not push a value onto the fallback"

    @pytest.mark.parametrize("value", ["\t", "a\tb", "a\t"], ids=["alone", "middle", "trailing"])
    def test_tab_is_legal_unescaped_in_both_fences(self, value: str) -> None:
        """Legal in both, so neither fence needs to spell it — but both must
        still round-trip it. `_dq` escapes it to `\\t` (via `repr`) and `_raw`
        carries it literally; the *value* is identical either way, which is the
        contract."""
        p41_assert_round_trip(p41_naive_raw_fence(value), value, fence="unchecked raw")
        p41_assert_round_trip(p41_raw_literal(value), value, fence="_raw's chosen")
        p41_assert_round_trip(p41_plain_literal(value), value, fence='plain "…"')


class TestEvenTrailingBackslashRunStaysRaw:
    """An even run stays raw; an odd run falls back. The parity rule, pinned.

    A raw fence keeps every backslash it contains — *"which is the whole reason
    a regex is worth emitting into one"* (`_raw_spellable`'s own comment). So
    the parity rule must not be simplified to "any trailing backslash falls
    back": that would move `[^\\d]`-shaped patterns onto the fallback and change
    the emitted spelling of the measured D1 shape.
    """

    @pytest.mark.parametrize(
        ("value", "expect_raw"),
        [
            ("a", True),
            ("a\\", False),
            ("a\\\\", True),
            ("a\\\\\\", False),
            ("a\\\\\\\\", True),
            ("a\\\\\\\\\\", False),
            ("\\", False),
            ("\\\\", True),
        ],
        ids=["run-0", "run-1", "run-2", "run-3", "run-4", "run-5", "bare-1", "bare-2"],
    )
    def test_parity_of_the_trailing_run_decides_the_arm(self, value: str, expect_raw: bool) -> None:
        assert _raw_spellable(value) is expect_raw
        assert (_raw(value)[0] == "r") is expect_raw
        p41_assert_round_trip(p41_raw_literal(value), value, fence="_raw's chosen")

    def test_a_raw_fence_keeps_every_backslash_it_carries(self) -> None:
        """The reason the raw arm exists at all, stated over a real pattern."""
        value = r"[^\d]\s+"
        assert _raw(value) == ("r", value), "an interior backslash must not force the fallback"
        assert ast.literal_eval(p41_raw_literal(value)) == value
        assert p41_raw_literal(value) == 'r"[^\\d]\\s+"'

    def test_an_interior_backslash_is_not_a_trailing_run(self) -> None:
        assert _raw_spellable("a\\b") is True
        assert _raw_spellable("a\\b\\c") is True


class TestTheArmEachValueTakes:
    """Which arm every table value takes, asserted so a silent change is visible.

    `tasks.md` 4.4: *"assert `_raw` returns `("r", …)` for the raw arm and
    `("", …)` for the fallback, so a value silently changing arms is visible."*
    A value moving arms is not a correctness failure on its own — both arms
    round-trip — but it changes the emitted *spelling* of a literal, which is
    exactly what the baseline pins are byte-exact about (`tasks.md` 5).
    """

    @pytest.mark.parametrize("case", P41_RAW_ARM_CASES, ids=p41_case_ids(P41_RAW_ARM_CASES))
    def test_the_raw_arm_returns_r_and_the_value_verbatim(self, case: P41FenceCase) -> None:
        assert _raw(case.value) == ("r", case.value), f"{case.case_id}: {case.note}"

    @pytest.mark.parametrize("case", P41_FALLBACK_ARM_CASES, ids=p41_case_ids(P41_FALLBACK_ARM_CASES))
    def test_the_fallback_arm_returns_an_empty_prefix_and_a_dq_body(self, case: P41FenceCase) -> None:
        assert _raw(case.value) == ("", _dq(case.value)), f"{case.case_id}: {case.note}"

    def test_the_table_covers_both_arms(self) -> None:
        """Non-vacuity: a table that had drifted entirely onto one arm would
        still pass every round-trip assertion above while testing half of
        `_raw`."""
        arms = {_raw(case.value)[0] for case in P41_ALL_CASES}
        assert arms == {"r", ""}, f"the case table reaches only {arms!r}"


# ══════════════════════════════════════════════════════════════════════
# SWEEPS — the round-trip is a universal claim
# ══════════════════════════════════════════════════════════════════════


class TestExhaustiveCodePointSweep:
    """Every code point below U+E000, through both fences.

    A sweep is what would have found `\\x00` without an empirical hunt
    (`tasks.md` 4.4), and it is also what found the lone-surrogate class, so the
    sweep is kept rather than replaced by the hand-written pins it produced.

    **`P41_SWEEP_CEILING` was U+3000 and is now U+E000** — see the constant for
    why. The consequence for this class is that the sweep now runs *through* the
    surrogate block instead of stopping below it, so the fifth exclusion is
    exercised on every run.
    """

    def test_every_code_point_round_trips_through_both_fences(self) -> None:
        failures: list[str] = []
        for code_point in range(P41_SWEEP_CEILING):
            char = chr(code_point)
            for value in (char, "a" + char + "b", "a" + char):
                for fence, literal in (("plain", p41_plain_literal(value)), ("_raw", p41_raw_literal(value))):
                    got = p41_evaluate_fence(literal, value)
                    if got.outcome != P41_PARSED_AND_EQUAL:
                        failures.append(
                            f"U+{code_point:04X} {fence} {value!r}: {got.outcome} "
                            f"literal={got.literal!r} value={got.value!r} {got.error}"
                        )
        assert not failures, "round-trip failed for:\n" + "\n".join(failures[:40])

    def test_the_only_single_code_points_forcing_the_fallback_are_the_documented_two_classes(self) -> None:
        """Four unspellable characters, the lone backslash, and the 2048 surrogates.

        **AMENDED, not rewritten.** This assertion previously read *"…are the
        documented five"* and compared against `P41_UNSPELLABLE_SINGLETONS`
        alone, which held only because the ceiling stopped below U+D800 — the
        lone-surrogate class this file had found and recorded but not pinned. The
        finding is now fixed and the ceiling now runs through U+DFFF, so the
        expected set gains the surrogate block **as a second mechanism** rather
        than as five more characters: the singletons break the *literal*
        (`SyntaxError`), the surrogates break the *module* (`UnicodeEncodeError`).
        Keeping them as two named sets is what makes a future narrowing of either
        one visible (`LGR-60c4985-6`, `LGR-5906ec6-2`: amend at the assertion
        with the reason, never delete).

        This is the assertion `\\x00` would have failed before task 4.1 widened
        the exclusion set, and the assertion a lone surrogate would have failed
        the moment the ceiling was raised past it.
        """
        surrogates = {chr(cp) for cp in range(P41_SURROGATE_FIRST, P41_SURROGATE_LAST + 1)}
        expected = set(P41_UNSPELLABLE_SINGLETONS) | surrogates

        forcing = {chr(cp) for cp in range(P41_SWEEP_CEILING) if not _raw_spellable(chr(cp))}
        assert forcing == expected, (
            "the set of single code points that cannot be spelled raw moved: "
            f"unexpected={sorted(forcing - expected)!r} missing={sorted(expected - forcing)!r}"
        )
        # Non-vacuity: the raised ceiling has to actually reach the second
        # mechanism, or this test degrades silently into the old five-member one.
        assert P41_SWEEP_CEILING > P41_SURROGATE_LAST, "the ceiling no longer covers the surrogate block"
        assert surrogates <= forcing, "the surrogate block is inside the sweep but is not being excluded"

    def test_raw_spellable_matches_what_python_will_accept_as_source(self) -> None:
        """`_raw_spellable` is checked against Python, not against itself.

        The load-bearing direction is soundness: if it returns True for a value
        Python will not accept in a raw fence, the emitted pandas is unusable.
        Completeness matters less — an over-conservative predicate only costs a
        spelling — but both are asserted, because a divergence either way is a
        fact about the helper worth surfacing.

        **"What Python will accept" is now three gates, not one**, which is why
        this test was renamed from `…matches_what_the_compiler_accepts`. Over the
        surrogate block the *compiler* is not the operative gate at all: it does
        raise, but it raises `UnicodeEncodeError`, i.e. it is reporting the
        encodability failure rather than a grammar one. `p41_evaluate_fence`
        checks encodability first so the two are told apart, and this test
        consumes its verdict rather than `compile()`'s directly.
        """
        unsound: list[str] = []
        overcautious: list[str] = []
        for code_point in range(P41_SWEEP_CEILING):
            char = chr(code_point)
            for value in (char, "a" + char + "b", "a" + char):
                claimed = _raw_spellable(value)
                actual = p41_evaluate_fence(p41_naive_raw_fence(value), value).outcome == P41_PARSED_AND_EQUAL
                if claimed and not actual:
                    unsound.append(f"U+{code_point:04X} {value!r}")
                elif actual and not claimed:
                    overcautious.append(f"U+{code_point:04X} {value!r}")
        assert not unsound, "_raw_spellable claimed a value is raw-spellable that Python rejects:\n" + "\n".join(
            unsound[:40]
        )
        assert not overcautious, "_raw_spellable refused a value Python accepts:\n" + "\n".join(overcautious[:40])


class TestGenerativeRoundTripSweep:
    """The round-trip as a universal claim, swept generatively.

    A supporting sweep, deliberately NOT tagged as one of the design's six
    numbered correctness properties (see the file docstring). 300 examples,
    over two domains:

    - **`st.characters(codec="utf-8")`** — the whole of Unicode a UTF-8 source
      file can carry, *including* the non-printable class: control characters
      (Cc), format characters (Cf), unassigned code points (Cn) and private use
      (Co) are all in. Lone surrogates are excluded by the codec, which is the
      domain boundary this file records as a finding rather than asserts.
    - **an adversarial alphabet** of exactly the characters the two fences
      disagree about, so short strings hit the fallback arm constantly instead
      of once in a few thousand examples.
    """

    @given(value=st.text(alphabet=st.characters(codec="utf-8"), max_size=40))
    @P41_SWEEP_SETTINGS
    def test_both_fences_round_trip_any_utf8_expressible_text(self, value: str) -> None:
        prefix, _body = _raw(value)
        event(f"_raw arm = {prefix or 'fallback'}")
        p41_assert_round_trip(p41_plain_literal(value), value, fence='plain "…"')
        p41_assert_round_trip(p41_raw_literal(value), value, fence="_raw's chosen")

    @given(value=st.text(alphabet=st.sampled_from(["a", '"', "'", "\\", "\n", "\r", "\t", "\x00", "\x0b"]), max_size=8))
    @P41_SWEEP_SETTINGS
    def test_both_fences_round_trip_the_adversarial_alphabet(self, value: str) -> None:
        prefix, body = _raw(value)
        event(f"_raw arm = {prefix or 'fallback'}")
        assert prefix in ("r", "")
        if prefix == "":
            assert body == _dq(value)
        p41_assert_round_trip(p41_plain_literal(value), value, fence='plain "…"')
        p41_assert_round_trip(p41_raw_literal(value), value, fence="_raw's chosen")

    @given(value=st.text(alphabet=st.characters(codec="utf-8", categories=["Cc", "Cf", "Cn", "Co"]), max_size=12))
    @P41_SWEEP_SETTINGS
    def test_both_fences_round_trip_non_printable_text(self, value: str) -> None:
        """The non-printable class explicitly, not merely reachable by chance.

        `_dq` moves the emitted *bytes* for these — `repr` spells them `\\x07`,
        `\\u200b` and so on — while preserving the *value*. The value is the
        contract; the bytes are not (see task 3.2's Property 2 note).
        """
        p41_assert_round_trip(p41_plain_literal(value), value, fence='plain "…"')
        p41_assert_round_trip(p41_raw_literal(value), value, fence="_raw's chosen")


class TestTheAdversarialProductIsNotVacuous:
    """A deterministic exhaustive product over the adversarial alphabet.

    Hypothesis reports arm coverage through `event()` but does not fail a
    vacuous sweep, and `design.md` §9 Q6 records a templated sweep whose 376
    generated statements all refused, so its zero offenders proved nothing.
    This is the deterministic floor: every string of length ≤ 3 over the
    characters the fences disagree about, with **both arms asserted reached**.
    """

    ALPHABET = ("a", '"', "'", "\\", "\n", "\r", "\t", "\x00")

    def test_every_short_adversarial_string_round_trips_through_both_fences(self) -> None:
        failures: list[str] = []
        checked = 0
        for length in (1, 2, 3):
            for chars in itertools.product(self.ALPHABET, repeat=length):
                value = "".join(chars)
                checked += 1
                for fence, literal in (("plain", p41_plain_literal(value)), ("_raw", p41_raw_literal(value))):
                    got = p41_evaluate_fence(literal, value)
                    if got.outcome != P41_PARSED_AND_EQUAL:
                        failures.append(f"{fence} {value!r}: {got.outcome} literal={got.literal!r} {got.error}")
        assert checked == sum(len(self.ALPHABET) ** n for n in (1, 2, 3)), checked
        assert not failures, "round-trip failed for:\n" + "\n".join(failures[:40])

    def test_the_product_reaches_both_arms(self) -> None:
        arms = {
            _raw("".join(chars))[0] for length in (1, 2, 3) for chars in itertools.product(self.ALPHABET, repeat=length)
        }
        assert arms == {"r", ""}, f"the adversarial product reaches only {arms!r} — the sweep is vacuous"


class TestTheSweptDomainIsTheEmittableOne:
    """The emittable domain and `_raw`'s raw arm now coincide. Pinned.

    **AMENDED IN PLACE, and the amendment is the point.** This class used to
    assert only the *boundary* — that a lone surrogate is not UTF-8-carryable and
    that the sweeps stopped below it — because the lone-surrogate hole was a
    finding this file recorded and deliberately did not pin: pinning it would
    have meant asserting a defect as desired behaviour, and task 4.4 could not
    touch a production line.

    That hole is now **fixed** (`_raw_spellable`'s fifth exclusion), so the same
    facts become real pins:

    1. a lone surrogate is not raw-spellable;
    2. `_raw` takes the **fallback** arm for it;
    3. the fallback body is exactly `_dq`'s;
    4. the resulting emitted literal **is** UTF-8 encodable.

    Each carries its reason at the assertion, per `LGR-60c4985-6` and
    `LGR-5906ec6-2` — a deliberately-held assertion is amended with its reason
    where it lives, never deleted, so the reversal stays legible. The boundary
    assertions the class had before are kept, amended, because the pins only mean
    something next to the fact they rest on.
    """

    # ── the boundary the pins rest on (kept, amended) ──────────────────

    def test_a_lone_surrogate_cannot_be_carried_by_a_utf8_source_file(self) -> None:
        """Unchanged, and still the fact everything else here follows from."""
        assert p41_utf8_source_safe("\ud800") is False
        assert p41_utf8_source_safe("a\udfffb") is False
        assert p41_utf8_source_safe("plain") is True

    def test_dq_still_round_trips_a_lone_surrogate_so_the_plain_sites_are_unexposed(self) -> None:
        """The twelve plain-fence sites were never exposed, which bounded the hole.

        `repr` spells a lone surrogate as a `\\ud800` escape, so `_dq`'s body is
        pure ASCII and the emitted module encodes. Only the two `r"…"` regex
        sites could ever carry one literally — which is why the fix needed no new
        machinery, only a narrowing of `_raw_spellable` so the value reaches this
        already-correct path.
        """
        value = "\ud800"
        assert p41_utf8_source_safe(_dq(value)) is True
        assert _dq(value).isascii(), "the escaped body must be pure ASCII, which is what makes the module writable"
        assert ast.literal_eval(p41_plain_literal(value)) == value

    def test_the_sweep_ceiling_now_sits_ABOVE_the_surrogate_range(self) -> None:
        """**AMENDED: this assertion used to read `<= 0xD800`.**

        It asserted the sweeps stopped *below* U+D800, so that a reader knew the
        green was not hiding the recorded finding. The finding is fixed, so the
        useful claim inverts: the sweeps must now run *through* the surrogate
        block, or the fifth exclusion would be untested by every sweep in this
        file. `P41_SWEEP_CEILING` moved `0x3000` → `0xE000` for exactly this.
        """
        assert P41_SWEEP_CEILING > P41_SURROGATE_LAST, (
            "the exhaustive sweeps must cover the surrogate block now that the exclusion exists"
        )
        assert P41_SURROGATE_FIRST < P41_SWEEP_CEILING

    # ── the pins the fix makes assertable ─────────────────────────────

    @pytest.mark.parametrize(
        "value",
        ["\ud800", "\udfff", "\udc00", "a\ud800b", "[^\ud800]", "a\udfff"],
        ids=["first", "last", "low-surrogate", "embedded", "in-a-regex-class", "trailing"],
    )
    def test_a_lone_surrogate_is_not_raw_spellable(self, value: str) -> None:
        """PIN 1. Without it, `_raw` writes the code point into the `r"…"` fence
        and the emitted module cannot be encoded, written or shipped — a
        `UnicodeEncodeError` at the write, not a `SyntaxError` at the literal, so
        nothing points back at the pattern that caused it."""
        assert _raw_spellable(value) is False, (
            f"{value!r} must not be raw-spellable: a raw fence would carry the surrogate literally "
            "and the emitted module would fail .encode('utf-8')"
        )

    @pytest.mark.parametrize(
        "value",
        ["\ud800", "\udfff", "a\ud800b", "[^\ud800]"],
        ids=["first", "last", "embedded", "in-a-regex-class"],
    )
    def test_raw_takes_the_dq_fallback_arm_for_a_lone_surrogate(self, value: str) -> None:
        """PINS 2 and 3. The fix adds no new machinery — it routes the value onto
        the fallback that already handled it. So the arm and the body are both
        asserted: a fallback that did not delegate to `_dq` would be a second
        escaping rule, which is the thing `design.md` §5.1 has exactly two of."""
        prefix, body = _raw(value)
        assert prefix == "", f"{value!r} must take the plain-fence fallback, got prefix {prefix!r}"
        assert body == _dq(value), "the fallback body must be exactly _dq's — no second escaping rule"

    @pytest.mark.parametrize(
        "value",
        ["\ud800", "\udfff", "a\ud800b", "[^\ud800]", "a\udfff"],
        ids=["first", "last", "embedded", "in-a-regex-class", "trailing"],
    )
    def test_the_emitted_literal_for_a_lone_surrogate_is_utf8_encodable(self, value: str) -> None:
        """PIN 4 — the failure mode itself, asserted rather than described.

        This is the assertion that would have failed before the fix, and the one
        that names the third failure mode: not "does it parse" and not "does it
        mean the right thing", but "can it be written down at all".
        """
        literal = p41_raw_literal(value)
        assert p41_utf8_source_safe(literal) is True, (
            f"the emitted literal {literal!r} cannot be encoded as UTF-8, so the module carrying it "
            "can be neither written to a file nor shipped to the code interpreter"
        )
        assert literal.isascii(), "the fallback escapes the surrogate to an ASCII \\uXXXX form"
        p41_assert_round_trip(literal, value, fence="_raw's chosen (surrogate)")

    def test_the_evaluate_fence_helper_reports_encodability_as_its_own_outcome(self) -> None:
        """The third failure mode is diagnosable, not folded into the second.

        `UnicodeEncodeError` is a `ValueError` subclass, so `compile()` raising it
        would be reported as `DID_NOT_PARSE` without the dedicated first gate —
        sending a reader after a grammar problem that does not exist. Asserted
        over the unchecked raw fence the fix prevents.
        """
        value = "\ud800"
        unchecked = p41_evaluate_fence(p41_naive_raw_fence(value), value)
        assert unchecked.outcome == P41_NOT_UTF8_ENCODABLE, (
            f"expected the unchecked raw fence to be unencodable, got {unchecked!r}"
        )
        assert "utf-8" in unchecked.error and "surrogate" in unchecked.error, unchecked.error
        # …and the fence `_raw` actually chooses is fine.
        assert p41_evaluate_fence(p41_raw_literal(value), value).outcome == P41_PARSED_AND_EQUAL

    def test_the_encodability_test_and_the_surrogate_range_are_co_extensive(self) -> None:
        """Why the production check is `.encode("utf-8")` and not a 2048-member set.

        `_RAW_UNSPELLABLE` is a tuple of single characters; the fifth class is a
        *range*. Enumerating it would mean 2048 tuple members scanned per call,
        and would assert the range's **extent** where its **cause** is available.
        This sweep is the evidence the two formulations agree: over every one of
        the 1,114,112 code points, `.encode("utf-8")` fails for exactly the 2048
        contiguous points U+D800–U+DFFF and for no others.
        """
        unencodable = [cp for cp in range(0x110000) if not p41_utf8_source_safe(chr(cp))]
        assert unencodable == list(range(P41_SURROGATE_FIRST, P41_SURROGATE_LAST + 1)), (
            f"the unencodable code points are not exactly the surrogate block: "
            f"count={len(unencodable)} first={unencodable[0] if unencodable else None:#x}"
        )
        assert len(unencodable) == 2048

    def test_no_code_point_anywhere_claims_the_raw_arm_unencodably(self) -> None:
        """The whole domain, cheaply — the exact property the surrogate class broke.

        `TestExhaustiveCodePointSweep` stops at `P41_SWEEP_CEILING` because a
        `compile` per point per fence over 1.1M points costs ~40 s. This sweep
        drops the compile and checks only encodability, which is affordable over
        the **full** range and is the one direction that was unsound: if
        `_raw_spellable` says yes, the literal `_raw` builds must be writable.
        """
        offenders = [
            cp
            for cp in range(0x110000)
            if _raw_spellable(chr(cp)) and not p41_utf8_source_safe(p41_raw_literal(chr(cp)))
        ]
        assert not offenders, (
            "_raw_spellable claimed the raw arm for code points whose emitted literal is not "
            f"UTF-8 encodable: {[hex(cp) for cp in offenders[:20]]}"
        )


class TestLoneSurrogateExhaustiveSweep:
    """All 2048 surrogates individually, through both fences.

    The dedicated sweep the raised ceiling does not replace. `_dq` is checked
    too, because "only the two raw sites were exposed" is a claim about `_dq`
    being correct here and it is worth holding rather than repeating.
    """

    def test_every_surrogate_is_excluded_and_still_round_trips(self) -> None:
        failures: list[str] = []
        for code_point in range(P41_SURROGATE_FIRST, P41_SURROGATE_LAST + 1):
            char = chr(code_point)
            for value in (char, "a" + char + "b", "[^" + char + "]"):
                if _raw_spellable(value):
                    failures.append(f"U+{code_point:04X} {value!r}: _raw_spellable returned True")
                    continue
                for fence, literal in (("plain", p41_plain_literal(value)), ("_raw", p41_raw_literal(value))):
                    got = p41_evaluate_fence(literal, value)
                    if got.outcome != P41_PARSED_AND_EQUAL:
                        failures.append(f"U+{code_point:04X} {fence} {value!r}: {got.outcome} {got.error}")
        assert not failures, "the surrogate block is not fully handled:\n" + "\n".join(failures[:40])

    def test_every_surrogate_takes_the_fallback_and_emits_ascii(self) -> None:
        """Both fences produce pure-ASCII bodies here, which is what makes the
        emitted module writable. Asserted over the whole block so a partial
        exclusion — a range check that got a boundary wrong — cannot pass."""
        for code_point in range(P41_SURROGATE_FIRST, P41_SURROGATE_LAST + 1):
            char = chr(code_point)
            prefix, body = _raw(char)
            assert prefix == "", f"U+{code_point:04X} took the raw arm"
            assert body == _dq(char), f"U+{code_point:04X} fallback body is not _dq's"
            assert body.isascii(), f"U+{code_point:04X} emitted a non-ASCII body"

    def test_the_boundaries_of_the_block_are_the_right_ones(self) -> None:
        """Off-by-one guard: U+D7FF and U+E000 are ordinary and must stay raw."""
        for code_point in (P41_SURROGATE_FIRST - 1, P41_SURROGATE_LAST + 1):
            char = chr(code_point)
            assert _raw_spellable(char) is True, f"U+{code_point:04X} is not a surrogate and must stay raw-spellable"
            assert _raw(char) == ("r", char)
        for code_point in (P41_SURROGATE_FIRST, P41_SURROGATE_LAST):
            assert _raw_spellable(chr(code_point)) is False, f"U+{code_point:04X} is a surrogate"
