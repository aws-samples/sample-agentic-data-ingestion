# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p41 T1 — raw regex literals at the two RAW-FENCE emission sites.

**Validates: Requirements 1.1, 1.2, 1.4, 1.16, 1.17, 2.1, 2.2, 2.3, 2.12, Property 1**

══════════════════════════════════════════════════════════════════════
ORACLE RULE FOR THIS FILE: REGEX AND SPARK SEMANTICS. NEVER CURRENT
CONVERTER OUTPUT. READ THIS BEFORE EDITING.
══════════════════════════════════════════════════════════════════════

`LGR-28b4d75-5` (template / ratified): *a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle.* p41 runs two opposite rules inside `tests/unit/lambdas/test_code/`,
and this file follows the semantics one:

**Every expectation below is derived from what the regex means, and is checked
against the standard-library `re`.** For a **raw** literal the source text *is*
the value — no escape processing happens in a raw fence — so the expected
pattern value is the raw body verbatim, and nothing here was transcribed from
what the converter emits. `bugfix.md` 2.15, `design.md` §10.

**The files that follow the OPPOSITE rule, where current output IS the
oracle,** and which must not be reconciled with anything here:

- `test_string_site_preservation.py` (task 3.1) — the 15-site plain-literal
  pins, transcribed from the unfixed converter.
- `test_passthrough_and_alias_refusal.py`'s `TestPassthrough*` and
  `TestAlias*Today*` classes (task 1.1 / 1.2's today-pins).

──────────────────────────────────────────────────────────────────────
EXPECTED-RED WINDOW — the failure is the deliverable
──────────────────────────────────────────────────────────────────────

**Every bug-condition test in this file FAILS against the current converter,
and that is the point.** Do NOT "fix" a test here, and do NOT touch a
production line to make one pass; the window closes at **task 4.3**, when
`_string` returns the literal's value and the two raw-fence sites wrap it with
`_raw`.

D1's failure mode is a **refusal where conversion should succeed** — a
`ConversionError`, not a text mismatch — so `p41_emitted_body` spells the
refusal payload out rather than letting a bare exception traceback stand in for
a diagnosis. What the unfixed converter answers today, for orientation only
(prose, deliberately not asserted anywhere in this file — asserting it would
create a pin that task 4.3 then has to amend):

- `regexp_replace(col("p"), r"…", "")` → refuses naming **`regexp_replace`**,
  classified `not_yet_expressible` — "the proxy cannot yet express
  `regexp_replace`", which is false: it expresses the identical pattern spelled
  without the `r` (`bugfix.md` 1.1, 1.5).
- `df.filter(col("e").rlike(r"…"))` → refuses naming **`col`**, which is D2a on
  top of D1 (`bugfix.md` 1.6). This file asserts nothing about the construct
  name; task 1.3 owns that, and task 8.1 closes it.

**`TestFixtureOracleIntegrity` is the one class here that is green at HEAD,**
and it is not a bug-condition case: it checks this file's own expectation table
against `re`, so it is the evidence that the oracle is regex semantics and not
the converter. Task 2.5's red/green bookkeeping should count it as an
oracle-integrity check, the way `TestOracleIntegrity` is counted in
`test_converter_equivalence_baseline.py`.

──────────────────────────────────────────────────────────────────────
WHAT LATER TASKS APPEND TO THIS FILE
──────────────────────────────────────────────────────────────────────

Structured so each can be added as its own labelled class, reusing the helpers
above the first class and adding rows to the tables rather than reshaping them:

- **Task 2.3 — the kept single-quote fence, GREEN throughout.**
  `regexp_replace(col("p"), r'[^0-9]', '')` refuses before *and* after the fix
  (Q1: accept the `r` prefix, keep the quote fence), and the refusal identifies
  the **quote fence** as the failing half rather than the prefix. It asserts a
  refusal, so it wants `pytest.raises(ConversionError)` and not
  `p41_emitted_body`, which fails on a refusal by design.
- **Task 2.4 — one D3 counterexample, RED, and red for a different reason than
  everything else here.** `regexp_replace(col("p"), "[^\\\\d]", "")` — non-raw,
  escaped — *converts* today and emits a pattern whose value is `[^\\d]`, which
  deletes every character instead of the non-digits. So the failure is a value
  mismatch out of `p41_emitted_pattern_value`, not a `ConversionError`. That
  difference is `bugfix.md` 1.20's argument in concrete form and is worth
  recording as such in task 2.5's notes.
- **Task 7.1 — the site-complete escape-fidelity sweep at both sites**, green
  once T3 has landed. It covers the escapes that differ between fences (`\\\\`,
  `\\n`, `\\t`, `\\b`, `\\"`) and at least one that does not (`\\.`, `\\d`,
  `\\w`, `\\s` — left alone by Python, which is why D3 needs a *double*
  backslash to manifest). Note for whoever writes it: a value carrying LF or CR
  cannot be spelled in a raw fence at all, so those cases exercise `_raw`'s
  plain-fence fallback (`design.md` §5.1).

──────────────────────────────────────────────────────────────────────
Scope notes
──────────────────────────────────────────────────────────────────────

Raw literals at the **twelve plain-fence sites** and the `_cast` key site are
task 2.2's file, `test_raw_literal_plain_sites.py`. This file is only the two
sites that emit into an `r"…"` fence: `regexp_replace`'s pattern
(`pyspark_to_pandas.py:845`, emitted at `:847`) and `rlike`'s pattern in filter
position (`:1232`, emitted at `:1233`).

pandas is **not** installed in this venv and must not become one
(`bugfix.md` 3.14). Nothing here executes the emitted pandas: every assertion
compares regex **values** using the standard-library `re`, which shares pandas'
regex-engine semantics for the patterns at issue. So a green run here is
evidence about the **translation**, not about the execution.

No shared fixture from `tests/fixtures/conftest.py` is relevant — the converter
is pure text-to-text and touches no AWS surface.
"""

import ast
import os
import re
import sys
import warnings
from typing import NamedTuple

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter, _Translator, _Unconvertible

#: The two-import header `convert()` prepends to every emission (`bugfix.md` 3.7).
P41_PANDAS_HEADER = "import pandas as pd\nimport numpy as np\n\n"

#: `.str.<accessor>` the two raw-fence sites emit into. `regexp_replace` becomes
#: `.str.replace(r"<pattern>", "<replacement>", regex=True)` (`:847`); `rlike` in
#: filter position becomes `.str.contains(r"<pattern>", na=False)` (`:1233`).
P41_REPLACE_ACCESSOR = "replace"
P41_CONTAINS_ACCESSOR = "contains"


# ──────────────────────────────────────────────────────────────────────
# Helpers. `p41_` prefixed per `design.md` §10 — and note the standing
# naming fence: `is_bug_condition_d3` already exists at
# `test_converter_equivalence_baseline.py:910` and means an EARLIER spec's
# Defect 3, so no p41 helper may take that name.
# ──────────────────────────────────────────────────────────────────────


def p41_emitted_body(source: str) -> str:
    """The pandas `convert()` emits for *source*, with the import header removed.

    **Fails loudly on a refusal**, because D1's failure mode *is* a refusal
    where 2.1 requires a conversion. A bare `ConversionError` traceback would
    say "unconvertible construct" and leave the reader to work out that the
    construct is expressible and the literal's spelling is the only reason —
    which is exactly the untruth `bugfix.md` 1.5 is about. So the payload is
    spelled out here instead.
    """
    try:
        code: str = PySpark2PandasConverter.convert(source)["code"]
    except ConversionError as exc:
        pytest.fail(
            "conversion REFUSED where requirement 2.1 says it must succeed.\n"
            f"  source     : {source}\n"
            f"  construct  : {exc.construct!r}\n"
            f"  line_number: {exc.line_number!r}\n"
            f"  reason     : {exc.reason!r}\n"
            f"  str(exc)   : {exc}\n"
            "This is D1's expected-red mode (tasks.md 2.1). Do NOT adjust this test; "
            "the window closes at task 4.3."
        )
    assert code.startswith(P41_PANDAS_HEADER), code
    return code[len(P41_PANDAS_HEADER) :]


def p41_emitted_pattern_value(source: str, accessor: str) -> str:
    """The **value** of the regex pattern literal in the pandas `convert()` emits.

    The whole point of this file, and the reason it cannot assert over emitted
    text. `bugfix.md` 1.20: a property asserting only "did not raise" passes on
    D3, and one asserting only "refused honestly" passes on D1. A *value* is the
    one formulation that survives a fence change — which is the single cause of
    both defects (`design.md` "Hypothesized Root Cause").

    The emitted literal is located structurally (the first positional argument
    of the sole `.str.<accessor>(...)` call), its source text is taken verbatim,
    and `ast.literal_eval` resolves it under **its own** fence. So a `r"…"`
    emission and a `"…"` emission carrying the same regex are correctly read as
    equal, and `_raw`'s plain-fence fallback (`design.md` §5.1) needs no
    special case here.
    """
    body = p41_emitted_body(source)
    tree = ast.parse(body)

    literals: list[ast.expr] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != accessor:
            continue
        # `.str.<accessor>` and not some other `.<accessor>` — the accessor is
        # what makes this the regex site rather than, say, a frame method.
        if not (isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "str"):
            continue
        assert node.args, f"no positional argument to .str.{accessor} in:\n{body}"
        literals.append(node.args[0])

    assert len(literals) == 1, f"expected exactly one .str.{accessor} pattern in:\n{body}\nfound {len(literals)}"

    segment = ast.get_source_segment(body, literals[0])
    assert segment is not None, body
    # Annotated `object` rather than left as `literal_eval`'s `Any` so the
    # isinstance narrowing below is the thing that establishes the return type.
    value: object = ast.literal_eval(segment)
    assert isinstance(value, str), f"emitted pattern {segment!r} is not a string literal"
    return value


class P41RawReplaceCase(NamedTuple):
    """A raw pattern at `regexp_replace`'s pattern site (`:845`), plus its regex fact.

    `pattern_value` is the raw literal's body **verbatim** — derived, not
    observed: a raw fence performs no escape processing, so for `r"[^\\d]"` the
    value is the two characters backslash and `d`. `sub_result` is what `re.sub`
    does with that value against `subject`, and `TestFixtureOracleIntegrity`
    checks that claim against `re` rather than trusting this table.
    """

    case_id: str
    source: str
    pattern_value: str
    subject: str
    sub_result: str


class P41RawRlikeCase(NamedTuple):
    """A raw pattern at `rlike`'s filter-position site (`:1232`), plus its match facts.

    `matching` is a subject the **intended** pattern matches and `non_matching`
    one it does not. The pair is chosen so that D3's mis-escaped emission
    inverts them exactly (`bugfix.md` 1.17): the value `@example\\\\.com$`
    matches `a@example\\.com` and fails `a@example.com`, which is the intent
    backwards. Asserting only "some subject matches" would not see that.
    """

    case_id: str
    source: str
    pattern_value: str
    matching: str
    non_matching: str


def p41_replace_block(pattern_literal: str) -> str:
    """A one-line `withColumn`/`regexp_replace` block carrying *pattern_literal* verbatim.

    Built as text rather than kept as 12 near-identical string constants so the
    **only** thing that varies between cases is the literal's spelling — which
    is the variable D1 is about (`bugfix.md` 1.2: the trigger is the `r` prefix,
    not the content).
    """
    return f'df_transformed = df.withColumn("phone", regexp_replace(col("phone_number"), {pattern_literal}, ""))'


def p41_filter_rlike_block(pattern_literal: str) -> str:
    """A one-line `df.filter(col(...).rlike(...))` block carrying *pattern_literal* verbatim.

    Filter position deliberately: it is the **only** path that hands `rlike`'s
    pattern to `_string` (`:1232`). Outside it, `_method_call` refuses `rlike`
    by name at `:972-974` before any literal is read, so a `withColumn`-position
    shape could never exercise this site (`design.md` §12 item 1, re-confirmed
    by observation in `observations.md` task 1.4).
    """
    return f'df_transformed = df.filter(col("email").rlike({pattern_literal}))'


# ══════════════════════════════════════════════════════════════════════
# TASK 2.1 — `:845`, `regexp_replace`'s PATTERN.
# ORACLE RULE: regex semantics. Current output is NOT consulted.
# EXPECTED: every test in the two bug-condition classes below is RED
# until task 4.3.
# ══════════════════════════════════════════════════════════════════════

P41_RAW_REPLACE_CASES: list[P41RawReplaceCase] = [
    # ── `bugfix.md` 1.1's reproduced shape: a raw pattern containing a
    # backslash escape that only means something to the REGEX engine. Python's
    # raw fence leaves `\d` as two characters; the regex engine reads it as
    # "digit". `re.sub(r"[^\d]", "", "ab-12 34c")` therefore strips everything
    # that is not a digit.
    P41RawReplaceCase(
        case_id="raw-pattern-negated-digit-class",
        source=p41_replace_block(r'r"[^\d]"'),
        pattern_value=r"[^\d]",
        subject="ab-12 34c",
        sub_result="1234",
    ),
    # ── `bugfix.md` 1.2's shape, and the one that settles the question of what
    # the trigger IS: **no backslash anywhere in the pattern.** If this refuses,
    # the fence check cannot be about escaping — it is about the `r` prefix
    # alone, i.e. about spelling. Kept `+` inside the class so the expectation
    # is not the same string as the case below it.
    P41RawReplaceCase(
        case_id="raw-pattern-no-backslash-keeps-plus",
        source=p41_replace_block(r'r"[^0-9+]"'),
        pattern_value="[^0-9+]",
        subject="+1 (206) 555-0100",
        sub_result="+12065550100",
    ),
    # ── The raw twin of the baseline's plain `"[^0-9]"` shape, which converts
    # today (`bugfix.md` 3.10). Same value, different spelling — the pair
    # `TestTriggerIsThePrefixNotTheEscaping` compares.
    P41RawReplaceCase(
        case_id="raw-pattern-no-backslash-digits-only",
        source=p41_replace_block(r'r"[^0-9]"'),
        pattern_value="[^0-9]",
        subject="ab-12 34c",
        sub_result="1234",
    ),
]

#: The plain-fence spelling of `raw-pattern-no-backslash-digits-only`'s pattern.
#: Converts today and must keep converting (`bugfix.md` 3.10), so it is the
#: control that makes "the prefix is the only difference" checkable.
P41_PLAIN_TWIN_SOURCE = p41_replace_block('"[^0-9]"')

#: The value both spellings denote. A plain fence with no escape sequences and a
#: raw fence with no backslash agree by construction — derived from Python's
#: literal semantics, not from an emission.
P41_PLAIN_TWIN_PATTERN_VALUE = "[^0-9]"


class TestRawPatternAtRegexpReplaceSite:
    """`:845` — a raw pattern converts and keeps its regex value.

    RED until task 4.3. `bugfix.md` 1.1, 1.2, 2.1, 2.2; Property 1.
    """

    @pytest.mark.parametrize("case", P41_RAW_REPLACE_CASES, ids=[c.case_id for c in P41_RAW_REPLACE_CASES])
    def test_raw_pattern_converts_with_the_raw_body_as_its_value(self, case: P41RawReplaceCase) -> None:
        """2.1: the emitted pattern's VALUE is the raw literal's body verbatim.

        Not its source text, and not the raw body re-escaped. For a raw literal
        the two coincide by definition of the fence — which is precisely why
        2.1 can say "stripping the `r` prefix and re-wrapping in the emission's
        `r"…"` is value-preserving".
        """
        assert p41_emitted_pattern_value(case.source, P41_REPLACE_ACCESSOR) == case.pattern_value

    @pytest.mark.parametrize("case", P41_RAW_REPLACE_CASES, ids=[c.case_id for c in P41_RAW_REPLACE_CASES])
    def test_emitted_pattern_behaves_as_the_intended_regex(self, case: P41RawReplaceCase) -> None:
        """2.1 checked as behaviour rather than as a string: `re.sub` agrees.

        A value comparison alone would not distinguish a pattern that is equal
        by accident from one that is equal by meaning. `re` is the oracle here;
        pandas is never imported (`bugfix.md` 3.14), so this is evidence about
        the translation and not about the execution.
        """
        emitted = p41_emitted_pattern_value(case.source, P41_REPLACE_ACCESSOR)
        assert re.sub(emitted, "", case.subject) == case.sub_result

    def test_a_raw_pattern_with_no_backslash_anywhere_still_converts(self) -> None:
        """1.2: the trigger is the `r` prefix alone, not escaping.

        The case that makes the mechanism unambiguous. `[^0-9+]` carries no
        backslash at all, so nothing about escapes, re-escaping or fences-as-
        value-maps can explain a refusal — only the literal's spelling can.
        2.1: the outcome SHALL NOT depend on whether the pattern contains a
        backslash.
        """
        case = P41_RAW_REPLACE_CASES[1]
        assert "\\" not in case.source, f"this case is only meaningful with no backslash: {case.source}"
        assert p41_emitted_pattern_value(case.source, P41_REPLACE_ACCESSOR) == case.pattern_value


class TestTriggerIsThePrefixNotTheEscaping:
    """The raw and plain spellings of one pattern emit one regex value.

    RED until task 4.3 — the raw half refuses today while the plain half
    converts, which is `bugfix.md` 1.1's reproduction stated as a property of
    the pair rather than of either input. 2.1's "SHALL NOT refuse a literal for
    carrying an `r` prefix", made checkable.
    """

    def test_plain_spelling_converts_to_the_expected_value(self) -> None:
        """The control, and it is green at HEAD (`bugfix.md` 3.10).

        Its expectation is still derived, not transcribed: a plain fence with no
        escape sequences has value equal to its body, so `"[^0-9]"` denotes
        `[^0-9]`. That it also happens to be what the converter emits today is
        a preservation fact task 3.1 pins, not this file's oracle.
        """
        assert p41_emitted_pattern_value(P41_PLAIN_TWIN_SOURCE, P41_REPLACE_ACCESSOR) == P41_PLAIN_TWIN_PATTERN_VALUE

    def test_raw_and_plain_spellings_emit_the_same_regex_value(self) -> None:
        """Two spellings of one regex must not have two outcomes.

        Both sides are compared against the derived value as well as against
        each other, so the assertion cannot be satisfied by both being wrong in
        the same way.
        """
        raw_case = P41_RAW_REPLACE_CASES[2]
        raw_value = p41_emitted_pattern_value(raw_case.source, P41_REPLACE_ACCESSOR)
        plain_value = p41_emitted_pattern_value(P41_PLAIN_TWIN_SOURCE, P41_REPLACE_ACCESSOR)
        assert raw_value == P41_PLAIN_TWIN_PATTERN_VALUE
        assert plain_value == P41_PLAIN_TWIN_PATTERN_VALUE
        assert raw_value == plain_value


# ══════════════════════════════════════════════════════════════════════
# TASK 2.1 — `:1232`, `rlike`'s PATTERN in filter position.
# ORACLE RULE: regex semantics. Current output is NOT consulted.
# EXPECTED: RED until task 4.3.
# ══════════════════════════════════════════════════════════════════════

P41_RAW_RLIKE_CASES: list[P41RawRlikeCase] = [
    # ── `bugfix.md` 1.17's shape, with its own subjects. The intended value
    # `@example\.com$` matches a real address and fails a string carrying a
    # literal backslash; the mis-escaped value `@example\\.com$` does the
    # reverse. So the pair detects the inversion rather than merely detecting
    # "a pattern was emitted".
    P41RawRlikeCase(
        case_id="raw-rlike-escaped-dot-example",
        source=p41_filter_rlike_block(r'r"@example\.com$"'),
        pattern_value=r"@example\.com$",
        matching="a@example.com",
        non_matching="a@example\\.com",
    ),
    # ── The shorter spelling task 1.3's shapes use, so the two files describe
    # the same predicate. Same structure, different literal.
    P41RawRlikeCase(
        case_id="raw-rlike-escaped-dot-short-domain",
        source=p41_filter_rlike_block(r'r"@x\.com$"'),
        pattern_value=r"@x\.com$",
        matching="a@x.com",
        non_matching="a@x\\.com",
    ),
]


class TestRawPatternAtRlikeSite:
    """`:1232` — a raw `rlike` pattern converts and keeps its match set.

    RED until task 4.3. `bugfix.md` 1.1, 1.17, 2.1; Property 1.

    This file asserts nothing about **which construct** the refusal names on the
    way there. Today it is `col` rather than `rlike`, which is D2a; task 1.3
    owns that assertion and task 8.1 closes it. Keeping the two apart is what
    lets each go green for its own reason.
    """

    @pytest.mark.parametrize("case", P41_RAW_RLIKE_CASES, ids=[c.case_id for c in P41_RAW_RLIKE_CASES])
    def test_raw_pattern_converts_with_the_raw_body_as_its_value(self, case: P41RawRlikeCase) -> None:
        """2.1 at the second raw-fence site."""
        assert p41_emitted_pattern_value(case.source, P41_CONTAINS_ACCESSOR) == case.pattern_value

    @pytest.mark.parametrize("case", P41_RAW_RLIKE_CASES, ids=[c.case_id for c in P41_RAW_RLIKE_CASES])
    def test_emitted_pattern_matches_the_intent_and_not_its_inverse(self, case: P41RawRlikeCase) -> None:
        """1.17: the divergence at this site is a changed MATCH SET, so assert both directions.

        `re.search` is the oracle. Asserting only that the intended subject
        matches would pass on a pattern that matches everything; asserting only
        that the other subject fails would pass on a pattern that matches
        nothing. The inversion `bugfix.md` 1.17 reproduces is only visible from
        the pair.
        """
        emitted = p41_emitted_pattern_value(case.source, P41_CONTAINS_ACCESSOR)
        assert re.search(emitted, case.matching) is not None
        assert re.search(emitted, case.non_matching) is None


# ══════════════════════════════════════════════════════════════════════
# ORACLE INTEGRITY. GREEN AT HEAD, and NOT a bug-condition case — it
# checks this file's expectation table against `re`, which is what makes
# the oracle auditable. Counted the way `TestOracleIntegrity` is counted
# in `test_converter_equivalence_baseline.py`, not as one of task 2.1's
# expected reds.
# ══════════════════════════════════════════════════════════════════════


class TestFixtureOracleIntegrity:
    """The tables above state regex facts. These tests check them against `re`.

    Without this, a wrong `sub_result` or a badly chosen subject pair would make
    a bug-condition test above fail for a reason that has nothing to do with the
    converter — and at task 4.3 it would fail to go green and read as an
    implementation problem. `LGR-28b4d75-5` cuts both ways: the oracle has to be
    checkable too.
    """

    @pytest.mark.parametrize("case", P41_RAW_REPLACE_CASES, ids=[c.case_id for c in P41_RAW_REPLACE_CASES])
    def test_replace_expectation_is_a_regex_fact(self, case: P41RawReplaceCase) -> None:
        """`re.sub` with the intended value produces the recorded result."""
        assert re.sub(case.pattern_value, "", case.subject) == case.sub_result

    @pytest.mark.parametrize("case", P41_RAW_RLIKE_CASES, ids=[c.case_id for c in P41_RAW_RLIKE_CASES])
    def test_rlike_subject_pair_is_a_regex_fact(self, case: P41RawRlikeCase) -> None:
        """The intended value matches one subject of the pair and not the other."""
        assert re.search(case.pattern_value, case.matching) is not None
        assert re.search(case.pattern_value, case.non_matching) is None

    @pytest.mark.parametrize("case", P41_RAW_RLIKE_CASES, ids=[c.case_id for c in P41_RAW_RLIKE_CASES])
    def test_the_mis_escaped_value_inverts_the_pair(self, case: P41RawRlikeCase) -> None:
        """1.17 stated as a fact about `re`, independent of the converter.

        Doubling the backslashes — which is what emitting source text into a raw
        fence does to an escaped literal — swaps which subject matches. This is
        why the two-direction assertion above is the one that can see D3, and
        why "did not raise" cannot (1.20).
        """
        mis_escaped = case.pattern_value.replace("\\", "\\\\")
        assert re.search(mis_escaped, case.matching) is None
        assert re.search(mis_escaped, case.non_matching) is not None

    def test_the_raw_and_plain_twin_patterns_denote_one_value(self) -> None:
        """The pair `TestTriggerIsThePrefixNotTheEscaping` rests on, checked as a literal fact.

        `r"[^0-9]"` and `"[^0-9]"` evaluate to the same string in Python, so a
        converter treating them differently is treating spelling as meaning.
        """
        assert ast.literal_eval(r'r"[^0-9]"') == P41_PLAIN_TWIN_PATTERN_VALUE
        assert ast.literal_eval('"[^0-9]"') == P41_PLAIN_TWIN_PATTERN_VALUE


# ══════════════════════════════════════════════════════════════════════
# TASK 2.3 — THE KEPT SINGLE-QUOTE FENCE, AND WHICH HALF FAILS.
#
# **Validates: Requirements 1.4, 2.3**
#
# EXPECTED OUTCOME: **GREEN on the first run and GREEN after T3.** Nothing
# in this section has a red window — it asserts CONTINUITY (`tasks.md`
# 2.3: "the only task in task 2 that is green from the start"). A red here
# is a finding: either the working tree moved, or T3 removed the
# single-quote fence as the side effect 2.3 forbids.
#
# ORACLE RULE: the same one the rest of this file follows — the
# requirement, never current output. The expectation that these shapes
# refuse comes from **2.3 + Q1** (accept the `r` prefix, KEEP the quote
# fence), and the expectation that `regexp_replace` is the construct named
# comes from **2.4** ("a refusal arising inside `regexp_replace`'s
# arguments SHALL name `regexp_replace`"). That HEAD already answers both
# is continuity, not the oracle — which is precisely why this section has
# no red window while everything above it does.
#
# The oracle-integrity checks for THIS section's table live inside it
# (`test_both_spellings_denote_one_value`, `test_the_fence_predicate_agrees_
# with_the_converter`), because `TestFixtureOracleIntegrity` above covers
# only task 2.1's two tables.
#
# ──────────────────────────────────────────────────────────────────────
# THE "SAYS WHICH HALF FAILED" HALF — conclusion, and what is handed to 4.2
# ──────────────────────────────────────────────────────────────────────
#
# `design.md` §5.2 step 3 requires the refusal to say **which half failed**
# — `r'…'` fails the quote fence, not the prefix check — so the two fences
# read as separable and a future reader can lift one without the other.
# `bugfix.md` 2.3 requires the decision about `r'…'` to be *stated* rather
# than implied. Working out what is assertable for that produced a
# conclusion worth writing down, because it is not what the phrase
# "refusal message" suggests:
#
# **The distinction cannot live in the refusal payload at all, and this
# spec's own fences are what forbid it.** Three findings, each read rather
# than assumed:
#
#   1. There is no message field to extend. `ConversionError` carries
#      exactly `(construct, line, line_number, reason)` (`:123-128`), and
#      `bugfix.md` 3.6 freezes `str(exc)` at
#      `unconvertible PySpark construct '<c>' on line <n>: <line>`.
#   2. `reason` — the one free field — is **already pinned to `None` on a
#      single-quote-fence refusal** by task 1.3.
#      `test_passthrough_and_alias_refusal.py`'s
#      `TestFilterRlikePatternRefusalNamesRlike::test_refusal_names_rlike_
#      and_never_col` asserts `exc.reason is None` for
#      `df.filter(col("email").rlike(r'@x\.com$'))`, which refuses at this
#      very fence (`:753`), and `tasks.md` 8.1 requires that class to go
#      green **unamended**. Setting a reason here turns 8.1's green into a
#      red.
#   3. Even without (2), a carried reason is not diagnostic metadata — it
#      is operator-facing output. `code_interpreter_runner.py:650` is
#      `reason = exc.reason or classify_conversion_refusal(exc.construct)`,
#      so a carried reason **wins** over the construct-keyed
#      classification. A new reason string would silently change
#      `not_verified_reason` for every single-quoted-literal refusal that
#      reaches a report — a change to what a refusal MEANS downstream,
#      which `bugfix.md` 2.11 puts explicitly out of scope for p41.
#
# So "which half failed" is discharged in **code structure and observable
# behaviour**, not in a payload, and part of it is a requirement handed to
# **task 4.2** rather than something a test can assert at HEAD:
#
#   **HANDED TO TASK 4.2** — `_string`'s prefix check and its quote fence
#   must be **two separately-evaluable predicates** (strip a leading
#   `r`/`R`, *then* apply the `:753` check to the remainder — `design.md`
#   §5.2 step 1), and the rewritten docstring must record that the quote
#   fence is the half that refuses `r'…'`. Do NOT discharge §5.2 step 3 by
#   adding a `reason`, a fifth field, or a widened `str(exc)`: (1)-(3)
#   above are why. The single-quote half of the current docstring survives
#   the 2.13 rewrite (`design.md` §9 Q1 says so in as many words), and
#   `test_the_kept_fence_is_a_stated_decision_and_not_an_implied_one`
#   below is what checks it still does.
#
# What a test CAN assert, and what this section therefore asserts — the
# strongest true statements available:
#
#   a. **Continuity.** Single-quoted literals refuse at both
#      `regexp_replace` sites, before and after T3, with 3.6's carrier
#      unchanged.
#   b. **Which half failed, behaviourally.** `r'…'` and `'…'` refuse
#      **identically** — the `r` prefix contributes nothing to the outcome
#      — so the quote fence ALONE accounts for the refusal. That is
#      `bugfix.md` 1.4's "two independent reasons at once" resolved into
#      one, and it is the same sentence §5.2 step 3 wants, stated over
#      behaviour instead of over a string. It is green today because both
#      spellings trip one combined `if`, and green after T3 because the
#      prefix is stripped and the same check runs on the remainder.
#   c. **Structural separability.** T3's predicate is a two-factor
#      decision, and the four spellings of one pattern populate its full
#      truth table (prefix × fence). Asserted by pure `p41_` helpers that
#      re-implement §5.2 step 1 over source text — never by asking the
#      converter — and cross-checked one-directionally against it so the
#      helpers cannot drift into fiction.
#   d. **The refusal is about spelling, not content.** Every single-quoted
#      body denotes the same VALUE as its accepted double-quoted twin, and
#      the twin converts. That is what makes keeping the fence a decision
#      (Q1) rather than an expressiveness limit — and what 2.3 means by
#      requiring the decision to be argued either way.
#
# NOT asserted here, deliberately: the other direction of (c) — "fence
# passes ⟹ converts" — is FALSE at HEAD for `r"…"` (that is D1 itself) and
# is task 2.1's red, closing at 4.3. Asserting it here would give this
# section a red window it is specified not to have. The behavioural
# differential that makes the two halves visible at a glance — `r"…"`
# converts while `r'…'` refuses — therefore only becomes observable when
# task 2.1's classes go green; this section holds the half of the pair that
# is true throughout.
# ══════════════════════════════════════════════════════════════════════

#: The two raw-string prefixes Python accepts, and the two `design.md` §5.2
#: step 1 strips. Byte-for-byte the set task 4.2 must recognise.
P41_RAW_PREFIXES = ("r", "R")

#: The construct 2.4 requires for a refusal arising inside `regexp_replace`'s
#: arguments. Both `_string` call sites hand `_refuse_here` the
#: `regexp_replace(...)` call as *owner* (`:845`, `:846`), whose leftmost
#: occurrence is `regexp_replace` itself — and `tasks.md` 8.1 makes the same
#: two sites pass this string explicitly, so it survives T3 and T6 alike.
P41_REGEXP_REPLACE_CONSTRUCT = "regexp_replace"


def p41_replace_block_with_replacement(pattern_literal: str, replacement_literal: str) -> str:
    """A `regexp_replace` block carrying *both* argument literals verbatim.

    The generalisation of `p41_replace_block` above, which fixes the
    replacement at `""`. Needed because task 2.3 must cover
    `regexp_replace`'s **replacement** site (`:846`) as well as its pattern
    site (`:845`), and the fence is per-argument: a block can pass at one and
    fail at the other. `test_the_two_block_builders_agree` pins the two
    helpers together so they cannot drift.
    """
    return (
        'df_transformed = df.withColumn("phone", '
        f'regexp_replace(col("phone_number"), {pattern_literal}, {replacement_literal}))'
    )


def p41_strip_raw_prefix(text: str) -> "tuple[str, str]":
    """Split *text* into its raw prefix (possibly empty) and the remainder.

    Step 1 of `design.md` §5.2, as a pure function over **source text**. The
    converter is never consulted: this is the predicate task 4.2 is specified
    to implement, so a test that asked the converter for it would be asking
    the subject to grade its own paper.
    """
    if text[:1] in P41_RAW_PREFIXES:
        return text[:1], text[1:]
    return "", text


def p41_passes_double_quote_fence(text: str) -> bool:
    """`:753`'s fence, transcribed verbatim, evaluated over *text*.

    Kept identical to the source predicate — including the `\"\"\"` clause,
    which is the same fence's third half and is also required to survive T3
    (§5.2 step 1 re-applies *the existing check* to the prefix-stripped
    remainder, and this is that check).

    Transcribing a predicate is not the same failure mode as transcribing an
    output: `test_the_fence_predicate_agrees_with_the_converter` checks this
    against observed refusals in the one direction that is true at HEAD, so
    it cannot silently stop describing the fence.
    """
    return not (len(text) < 2 or not text.startswith('"') or not text.endswith('"') or text.startswith('"""'))


class P41QuoteFenceCase(NamedTuple):
    """One literal the quote fence refuses, in both spellings, plus its accepted twin.

    *raw_literal* and *unprefixed_literal* differ by exactly one character —
    the `r` — which is what makes the pair able to say that the prefix
    contributes nothing to the outcome. *twin_literal* is the same VALUE
    spelled double-quoted, i.e. the spelling the fence accepts, so the
    refusal is demonstrably about spelling.

    No body carries a backslash. Deliberate: `'@x\\\\.com$'` and `r'@x\\.com$'`
    also denote one value (Python leaves `\\.` alone), but only because of a
    second fact about escapes, and mixing the two arguments would make a
    failure ambiguous. `bugfix.md` 1.2's point in a different register — the
    fence is about spelling, so the case for it should not need escaping to
    make itself.
    """

    case_id: str
    #: `"pattern"` (`:845`) or `"replacement"` (`:846`) — which `_string` call
    #: this case's literal reaches.
    site: str
    raw_literal: str
    unprefixed_literal: str
    twin_literal: str
    #: The value all three spellings denote. A Python literal fact, checked by
    #: `test_both_spellings_denote_one_value` rather than asserted on trust.
    value: str
    #: The block carrying *raw_literal* at *site*, and the same block carrying
    #: *unprefixed_literal* / *twin_literal*. Built at table-construction time
    #: so every source string in this section is auditable in one place.
    raw_block: str
    unprefixed_block: str
    twin_block: str
    #: The value of the PATTERN literal in *twin_block*, i.e. what
    #: `p41_emitted_pattern_value` must read back out of the converted twin.
    #: Equal to *value* at the pattern site; at the replacement site the pattern
    #: is the fixed acceptable one, so it is that instead.
    twin_pattern_value: str


#: The pattern literal a replacement-site case holds fixed: double-quoted, so it
#: passes the fence and the only thing under test is the replacement.
P41_ACCEPTABLE_PATTERN_LITERAL = '"[^0-9]"'


def p41_quote_fence_case(case_id: str, site: str, raw_literal: str, twin_literal: str, value: str) -> P41QuoteFenceCase:
    """Build a `P41QuoteFenceCase`, deriving the unprefixed spelling and the blocks.

    The unprefixed spelling is *raw_literal* minus its prefix, so the pair
    cannot accidentally differ in anything else — the invariant the
    identical-payload assertion rests on.
    """
    prefix, unprefixed_literal = p41_strip_raw_prefix(raw_literal)
    assert prefix in P41_RAW_PREFIXES, f"{raw_literal!r} must carry a raw prefix"
    if site == "pattern":
        blocks = (
            p41_replace_block_with_replacement(raw_literal, '""'),
            p41_replace_block_with_replacement(unprefixed_literal, '""'),
            p41_replace_block_with_replacement(twin_literal, '""'),
        )
        twin_pattern_value = value
    elif site == "replacement":
        blocks = (
            p41_replace_block_with_replacement(P41_ACCEPTABLE_PATTERN_LITERAL, raw_literal),
            p41_replace_block_with_replacement(P41_ACCEPTABLE_PATTERN_LITERAL, unprefixed_literal),
            p41_replace_block_with_replacement(P41_ACCEPTABLE_PATTERN_LITERAL, twin_literal),
        )
        twin_pattern_value = ast.literal_eval(P41_ACCEPTABLE_PATTERN_LITERAL)
    else:  # pragma: no cover - the table below only uses the two real sites
        raise AssertionError(f"unknown _string site {site!r}")
    return P41QuoteFenceCase(
        case_id=case_id,
        site=site,
        raw_literal=raw_literal,
        unprefixed_literal=unprefixed_literal,
        twin_literal=twin_literal,
        value=value,
        raw_block=blocks[0],
        unprefixed_block=blocks[1],
        twin_block=blocks[2],
        twin_pattern_value=twin_pattern_value,
    )


P41_QUOTE_FENCE_CASES: list[P41QuoteFenceCase] = [
    # ── `bugfix.md` 1.4's and `tasks.md` 2.3's named literal, at the pattern
    # site. Refuses today for TWO reasons at once (prefix + quote) and after
    # T3 for one (quote), which is the whole subject of this section.
    p41_quote_fence_case(
        case_id="pattern-single-quoted",
        site="pattern",
        raw_literal="r'[^0-9]'",
        twin_literal='"[^0-9]"',
        value="[^0-9]",
    ),
    # ── The **replacement** site (`:846`), which the hard requirement for this
    # task names alongside the pattern. The fence is per-argument, so a block
    # whose pattern is perfectly acceptable still refuses on its replacement —
    # and after T3 the pattern converts while the replacement still refuses,
    # which is `design.md` §5.1's per-site table made observable at one site.
    p41_quote_fence_case(
        case_id="replacement-single-quoted",
        site="replacement",
        raw_literal="r'-'",
        twin_literal='"-"',
        value="-",
    ),
    # ── The same fence's THIRD clause: `startswith('\"\"\"')` (`:753`). Included
    # because 2.3 forbids removing the quote fence as a side effect, and the
    # fence is one predicate — a T3 that strips the prefix and then re-applies
    # only *part* of the check would leave this case converting a spelling
    # "this checker has never verified" (`_string`'s docstring, `:744-748`).
    # A red here means exactly that happened.
    p41_quote_fence_case(
        case_id="pattern-triple-quoted",
        site="pattern",
        raw_literal='r"""[^0-9]"""',
        twin_literal='"[^0-9]"',
        value="[^0-9]",
    ),
]

#: `tasks.md` 2.3's shape, verbatim: BOTH arguments single-quoted, so the
#: block trips the fence at `:845` and would trip it again at `:846`. Kept
#: whole and separate from the table because it is the sentence the task is
#: written in, and a reader should be able to find it unaltered.
P41_TASK_2_3_VERBATIM_BLOCK = p41_replace_block_with_replacement("r'[^0-9]'", "''")

#: The four spellings of one pattern, as `(literal, prefix_present,
#: fence_passes)`. The truth table of T3's two-factor decision, and the
#: machine-checkable form of "the two fences are separable": all four
#: combinations exist, so neither factor determines the other.
P41_FENCE_TRUTH_TABLE: list[tuple[str, bool, bool]] = [
    ('"[^0-9]"', False, True),  # accepted today and after T3
    ('r"[^0-9]"', True, True),  # refused today (D1); converts after T3 — task 2.1's red
    ("'[^0-9]'", False, False),  # refused today and after T3 — the quote fence
    ("r'[^0-9]'", True, False),  # refused today and after T3 — the quote fence ALONE, after T3
]


def p41_refusal(source: str) -> ConversionError:
    """The `ConversionError` *source* refuses with, or a failure naming the emission.

    The mirror image of `p41_emitted_body`: this section's shapes are ones
    2.3 requires to KEEP refusing, so a conversion is the failure worth
    spelling out. A bare `pytest.raises` would report "DID NOT RAISE" and
    leave the reader to work out that the fence had been lifted.
    """
    try:
        code: str = PySpark2PandasConverter.convert(source)["code"]
    except ConversionError as exc:
        return exc
    pytest.fail(
        "conversion SUCCEEDED where requirement 2.3 keeps the single-quote fence.\n"
        f"  source  : {source}\n"
        f"  emitted : {code[len(P41_PANDAS_HEADER) :].strip()}\n"
        "Q1 accepts the `r` prefix and KEEPS the quote fence; 2.3 forbids removing the quote "
        "fence as a side effect of removing the prefix fence. If lifting it was intended, that is "
        "a decision to re-open with the user, not a test to adjust."
    )


def p41_regexp_replace_arg_nodes(source: str) -> "tuple[_Translator, ast.Constant, ast.Constant, ast.Call]":
    """`(translator, pattern_node, replacement_node, owner)` for the `regexp_replace` call.

    *owner* is the `regexp_replace(...)` `Call` — exactly the node the two
    sites hand `_string` as `owner` (`:845`, `:846`). For calling the fence
    directly, which is how the quote-fence half is asserted without depending
    on which construct the payload happens to name.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "regexp_replace":
            assert len(node.args) == 3, source
            pattern, replacement = node.args[1], node.args[2]
            assert isinstance(pattern, ast.Constant) and isinstance(replacement, ast.Constant), source
            return _Translator(source), pattern, replacement, node
    raise AssertionError(f"no regexp_replace call found in {source!r}")


class TestSingleQuoteFenceIsKeptThroughT3:
    """2.3 — the quote fence survives D1's fix, at both `regexp_replace` sites.

    **Validates: Requirements 1.4, 2.3**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN**, before and after T3. No red window.
    ══════════════════════════════════════════════════════════════════

    Q1 (`design.md` §9): the `r` prefix is accepted, the single-quote fence is
    **kept**, because all 5 measured production cases are raw *double*-quoted
    — lifting the quote fence would widen the accepted surface for zero
    measured demand. 2.3 makes that a decision the system must state and must
    not reverse by accident.

    The carrier is asserted alongside the refusal (`bugfix.md` 3.6): three
    positionals, `reason` defaulted to `None`, `str(exc)` unchanged. So a T3
    that "said which half failed" by widening the payload fails here rather
    than passing quietly — which is the point of the conclusion recorded in
    this section's header.
    """

    @pytest.mark.parametrize("case", P41_QUOTE_FENCE_CASES, ids=lambda case: case.case_id)
    def test_the_raw_spelling_keeps_refusing(self, case: P41QuoteFenceCase) -> None:
        """2.3: accepting the `r` prefix does not accept `r'…'`."""
        exc = p41_refusal(case.raw_block)
        assert exc.construct == P41_REGEXP_REPLACE_CONSTRUCT
        assert exc.line == case.raw_block
        assert exc.line_number == 1
        assert exc.reason is None
        assert str(exc) == (
            f"unconvertible PySpark construct '{P41_REGEXP_REPLACE_CONSTRUCT}' on line 1: {case.raw_block}"
        )

    @pytest.mark.parametrize("case", P41_QUOTE_FENCE_CASES, ids=lambda case: case.case_id)
    def test_the_unprefixed_spelling_keeps_refusing(self, case: P41QuoteFenceCase) -> None:
        """2.3: and neither does it accept `'…'`, which never had a prefix to accept."""
        exc = p41_refusal(case.unprefixed_block)
        assert exc.construct == P41_REGEXP_REPLACE_CONSTRUCT
        assert exc.line == case.unprefixed_block
        assert exc.line_number == 1
        assert exc.reason is None

    def test_the_task_shape_refuses_verbatim(self) -> None:
        """`tasks.md` 2.3's own shape, unaltered: both arguments single-quoted."""
        exc = p41_refusal(P41_TASK_2_3_VERBATIM_BLOCK)
        assert exc.construct == P41_REGEXP_REPLACE_CONSTRUCT
        assert exc.line == P41_TASK_2_3_VERBATIM_BLOCK
        assert exc.reason is None

    @pytest.mark.parametrize("case", P41_QUOTE_FENCE_CASES, ids=lambda case: case.case_id)
    def test_the_fence_itself_refuses_when_called_directly(self, case: P41QuoteFenceCase) -> None:
        """The fence, not the enclosing shape, is what refuses.

        Called with **two positional arguments**, which stays valid at task
        8.1 because `construct` is added optional and last (`design.md` §5.3).
        Deliberately silent about the construct named: `_string` without a
        `construct` keeps using `_refuse_here` even after 8.1, so pinning a
        name here would pin the wrong thing.
        """
        translator, pattern, replacement, owner = p41_regexp_replace_arg_nodes(case.raw_block)
        node = pattern if case.site == "pattern" else replacement
        with pytest.raises(_Unconvertible):
            translator._string(node, owner)

    def test_the_kept_fence_is_a_stated_decision_and_not_an_implied_one(self) -> None:
        """2.3: the system SHALL **state** a decision about `r'…'`; silence is not acceptable.

        The only place a test can check "stated", given the conclusion in this
        section's header: `_string`'s own docstring. Today it records the
        single-quote fence as deliberate rather than inherited (`:744-748`),
        and `design.md` §9 Q1 says that half of the docstring **survives** the
        2.13 rewrite while the falsified source-text rationale is deleted.

        Matched tolerantly on purpose — this pins that the decision is
        recorded where the next reader will look, not any particular wording.
        A red at task 4.2 means the rewrite dropped the surviving half, which
        is 2.3's "silence" outcome arriving through the docstring instead of
        through the code.
        """
        doc = _Translator._string.__doc__
        assert doc is not None, "`_string` lost its docstring; 2.13 requires it rewritten, not removed"
        assert "single-quot" in doc.lower(), (
            "`_string`'s docstring no longer records the single-quote fence as a decision. 2.3 "
            "requires the decision about `r'…'` to be STATED, and `design.md` §9 Q1 keeps this half "
            "of the docstring through the 2.13 rewrite. Restore it rather than deleting this test."
        )


class TestTheQuoteFenceIsTheHalfThatFails:
    """§5.2 step 3 — the failing half is identified, behaviourally and structurally.

    **Validates: Requirements 1.4, 2.3**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN**, before and after T3. No red window.
    ══════════════════════════════════════════════════════════════════

    `bugfix.md` 1.4: `r'…'` refuses "for two independent reasons at once — the
    `r` prefix and the single quote — [and] the two halves are not separable
    by the current predicate". §5.2 step 3 requires the fix to make them
    separable and to say which half a refusal came from.

    The payload cannot carry that sentence — see this section's header for the
    three reasons and for what is consequently handed to task 4.2. What is
    assertable, and asserted here, is the same claim in the two forms a test
    can hold:

    - **behavioural** — the prefix makes no difference to the outcome, so the
      quote fence alone accounts for the refusal;
    - **structural** — the decision has two independent factors, and all four
      of their combinations are realised.

    Both are true at HEAD and after T3, for different mechanical reasons, and
    that is what makes them continuity assertions rather than pins.
    """

    @pytest.mark.parametrize("case", P41_QUOTE_FENCE_CASES, ids=lambda case: case.case_id)
    def test_the_prefix_makes_no_difference_to_the_refusal(self, case: P41QuoteFenceCase) -> None:
        """The two spellings refuse identically ⟹ the quote fence is the failing half.

        The strongest true form of §5.2 step 3 available to a test. If the
        prefix contributed anything to the outcome, the payloads would differ
        somewhere; they differ only where the source text differs, by the one
        `r` character. After T3 this is true because the prefix has been
        stripped before the fence runs — the same sentence, now for the reason
        the design gives it.
        """
        raw = p41_refusal(case.raw_block)
        unprefixed = p41_refusal(case.unprefixed_block)
        assert raw.construct == unprefixed.construct
        assert raw.line_number == unprefixed.line_number
        assert raw.reason == unprefixed.reason
        # The sources themselves differ by exactly the prefix, so the only
        # payload field that may differ is the one that echoes the source.
        assert case.raw_literal == "r" + case.unprefixed_literal
        assert raw.line == case.raw_block
        assert unprefixed.line == case.unprefixed_block
        assert raw.line.replace(case.raw_literal, case.unprefixed_literal, 1) == unprefixed.line

    @pytest.mark.parametrize("literal,prefix_present,fence_passes", P41_FENCE_TRUTH_TABLE, ids=lambda v: repr(v))
    def test_the_two_factors_are_independent(self, literal: str, prefix_present: bool, fence_passes: bool) -> None:
        """The decision T3 implements is two-factor, and each factor stands alone.

        `design.md` §5.2 step 1: strip a leading `r`/`R`, *then* apply `:753`'s
        check to the remainder. Evaluated here by `p41_` helpers over source
        text — the converter is not asked, because this is the predicate task
        4.2 is specified to implement and the point is to state it
        independently.
        """
        prefix, remainder = p41_strip_raw_prefix(literal)
        assert bool(prefix) is prefix_present
        assert p41_passes_double_quote_fence(remainder) is fence_passes

    def test_all_four_combinations_are_covered(self) -> None:
        """Separability, as one assertion: neither factor determines the other.

        If the truth table were missing a row, "the two fences are separable"
        would be an unchecked claim — a fence that only ever fails together
        with another is indistinguishable from one fence. All four cells
        occupied is what makes lifting one without the other meaningful, which
        is what 2.3 asks a future reader to be able to do.
        """
        combinations = {(prefix, fence) for _literal, prefix, fence in P41_FENCE_TRUTH_TABLE}
        assert combinations == {(False, True), (True, True), (False, False), (True, False)}

    @pytest.mark.parametrize("case", P41_QUOTE_FENCE_CASES, ids=lambda case: case.case_id)
    def test_the_fence_predicate_agrees_with_the_converter(self, case: P41QuoteFenceCase) -> None:
        """One-directional cross-check: fence fails ⟹ the converter refuses.

        Keeps `p41_passes_double_quote_fence` honest without making it the
        oracle. **Only this direction**: the converse — fence passes ⟹
        converts — is false at HEAD for `r"…"`, because that IS D1. It is task
        2.1's red and closes at 4.3, so asserting it here would give this
        section a red window `tasks.md` 2.3 says it must not have.
        """
        for literal, block in ((case.raw_literal, case.raw_block), (case.unprefixed_literal, case.unprefixed_block)):
            _prefix, remainder = p41_strip_raw_prefix(literal)
            assert not p41_passes_double_quote_fence(remainder), literal
            p41_refusal(block)

    @pytest.mark.parametrize("case", P41_QUOTE_FENCE_CASES, ids=lambda case: case.case_id)
    def test_both_spellings_denote_one_value(self, case: P41QuoteFenceCase) -> None:
        """The refusal is about spelling, not content — a Python literal fact.

        Oracle integrity for this section's table, and the reason keeping the
        fence is a *decision* (Q1) rather than an expressiveness limit: the
        value is one the proxy expresses perfectly well when it arrives
        double-quoted. 2.3 requires either outcome to be argued; this is the
        fact the argument is about.
        """
        assert ast.literal_eval(case.raw_literal) == case.value
        assert ast.literal_eval(case.unprefixed_literal) == case.value
        assert ast.literal_eval(case.twin_literal) == case.value

    @pytest.mark.parametrize("case", P41_QUOTE_FENCE_CASES, ids=lambda case: case.case_id)
    def test_the_double_quoted_twin_converts(self, case: P41QuoteFenceCase) -> None:
        """And the accepted spelling of that same value converts, today and after T3.

        The anti-vacuity guard for the class: without it, every assertion above
        would be satisfied by a converter that refused the whole shape for some
        unrelated reason. `bugfix.md` 3.10 pins this shape as unchanged, so a
        red here is a preservation finding and not a task 2.3 finding.

        Asserted over the emitted pattern's VALUE rather than over "it did not
        raise", for the reason the whole file exists: `bugfix.md` 1.20, a
        property that only checks for the absence of a `ConversionError` passes
        on D3.
        """
        assert p41_emitted_pattern_value(case.twin_block, P41_REPLACE_ACCESSOR) == case.twin_pattern_value

    def test_the_two_block_builders_agree(self) -> None:
        """`p41_replace_block_with_replacement` generalises task 2.1's builder.

        Pins the two together so a later edit to one cannot silently change
        what the other means. Task 2.1's builder fixes the replacement at
        `""`; that is this one's `replacement_literal='""'` case.
        """
        for literal, _prefix, _fence in P41_FENCE_TRUTH_TABLE:
            assert p41_replace_block_with_replacement(literal, '""') == p41_replace_block(literal)


# ══════════════════════════════════════════════════════════════════════
# TASK 2.4 — ONE D3 COUNTEREXAMPLE: A NON-RAW ESCAPED PATTERN CONVERTS
# TO A DIFFERENT REGEX.
#
# **Validates: Requirements 1.16, 1.17, 2.12, Property 1**
#
# ══════════════════════════════════════════════════════════════════
# EXPECTED OUTCOME: **RED — and red for a DIFFERENT REASON than every
# other red in this file.**
# ══════════════════════════════════════════════════════════════════
#
# Every red above is a **refusal** where 2.1 requires a conversion, so it
# arrives as `p41_emitted_body`'s spelled-out `ConversionError` payload.
# **Here `convert()` SUCCEEDS.** The failure is a **value mismatch** out
# of `p41_emitted_pattern_value` — a plain `assert a == b`, no exception
# anywhere. That difference is `bugfix.md` 1.20's argument in concrete
# form: a property asserting only *"did not raise a `ConversionError`"*
# passes on this shape, and one asserting only *"refused honestly"* passes
# on D1, so **neither half of the existing generative pair can see either**.
#
# The distinction is not left to prose. `TestTheD3FailureModeIsAValueMismatch`
# asserts it directly — this shape converts, therefore any red in this
# section is a mismatch and never a refusal — and that class is **GREEN at
# HEAD and GREEN after T3**, because the literal is plain double-quoted and
# passes the `:753` fence both before and after the fix. Only its *value*
# moves at 4.3. So a future reader cannot mistake one failure mode for the
# other, and a red there would mean the fence started refusing a spelling
# it has always accepted.
#
# **Expected-red window closed by task 4.3**, when `_string` returns
# `node.value` and `:845` wraps it with `_raw`.
#
# ──────────────────────────────────────────────────────────────────────
# WHAT THE UNFIXED CONVERTER ANSWERS TODAY — prose, deliberately not
# asserted anywhere in this section
# ──────────────────────────────────────────────────────────────────────
#
# Reproduced against the working tree at HEAD `4dd9b42` by calling
# `PySpark2PandasConverter.convert` directly, and recorded here for
# orientation only; asserting it would create a pin task 4.3 then has to
# amend, which is the trap the file header names for D1's payload.
#
#   input PySpark  : df_transformed = df.withColumn("phone",
#                      regexp_replace(col("phone_number"), "[^\\d]", ""))
#   -> CONVERTS (no ConversionError)
#   emitted pandas : df_transformed = df.copy(); df_transformed["phone"] =
#                      df["phone_number"].str.replace(r"[^\\d]", "", regex=True)
#   emitted pattern source text : r"[^\\d]"
#   emitted pattern VALUE       : [^\\d]      (backslash, backslash, d)
#   re.sub(that value, "", "ab-12 34c")  == ''        <- deletes EVERYTHING
#   re.sub(the intent, "", "ab-12 34c")  == '1234'    <- what Spark does
#
# The mechanism is one line: `_string` returns `segment[1:-1]` — the source
# **text** (`:756`) — and `:847` re-wraps that text in an `r"…"` fence. A
# plain→raw fence change is value-preserving only for a body whose value
# already equals its text, which is the whole of `design.md`'s
# "Hypothesized Root Cause". `p41_plain_literal_body` below is `:756`
# transcribed, so the divergence is computable here without asking the
# converter for it.
#
# ──────────────────────────────────────────────────────────────────────
# WHY THE CONDITION NEEDS A *DOUBLE* BACKSLASH, AND WHY IT IS MEASURED AT
# ZERO
# ──────────────────────────────────────────────────────────────────────
#
# Python leaves **unrecognised** escapes alone, so `\.`, `\d`, `\w` and
# `\s` all have a value equal to their source text and pass through the
# fence change unharmed. Only **recognised** escapes — `\\`, `\n`, `\t`,
# `\b`, `\"` — differ. That is why `rlike("@example\.com$")` converts
# correctly today (`bugfix.md` 3.10 pins it), why D3 needs the doubled
# backslash this section uses, and why **0 of 56 measured generated
# versions hit it** (`bugfix.md` 1.19). D3 is therefore latent and is
# fixed as defence-in-depth in the same function as D1, never as this
# spec's headline (2.14) — which is also why exactly **one** counterexample
# lives here rather than a sweep.
#
# `P41_UNRECOGNISED_ESCAPE_CONTROL` is that boundary, made checkable: the
# same class spelled with a *single* backslash is **not** a D3 shape and
# converts correctly today. Without it, a red above would be consistent
# with "the converter mangles every escaped literal", which is false and
# would misdirect task 4.3.
#
# **`convert` suppresses the `SyntaxWarning` Python raises for an
# unrecognised escape** (`:1372-1376`), and that is correct — the warning
# is about the model's text, not about the converter's behaviour. **Nothing
# in this section depends on it being raised**, and nothing should
# (`tasks.md` 7.1 repeats the instruction).
#
# ──────────────────────────────────────────────────────────────────────
# SCOPE — one counterexample, and what is NOT here
# ──────────────────────────────────────────────────────────────────────
#
# `tasks.md` 2.4 is deliberately narrow so that D3 does not **block** D1
# (`bugfix.md` 2.14, `design.md` §13's per-defect red rule). So:
#
# - **One converter-facing counterexample**, at `regexp_replace`'s pattern
#   site (`:845`, emitted `:847`), where the divergence shows as a changed
#   **replacement result**.
# - **1.17's `rlike` half is asserted here only as a fact about `re`** —
#   doubling the backslashes changes a **match set** rather than a
#   replacement, and inverts it exactly. The converter assertion at
#   `:1232`/`:1233` belongs to **task 7.1**, alongside the full escape
#   sweep (`\\`, `\n`, `\t`, `\b`, `\"`, plus the unrecognised set) at both
#   sites. `TestFixtureOracleIntegrity::test_the_mis_escaped_value_inverts_the_pair`
#   above already holds the same `re` fact over task 2.1's `rlike` table.
# - Values carrying LF or CR cannot be spelled in a raw fence at all and
#   exercise `_raw`'s plain-fence fallback; that is task 7.1's and 4.4's,
#   not this section's.
#
# ORACLE RULE: unchanged from the rest of the file — regex semantics via
# the standard library `re`, never current converter output. The intended
# value comes from **Python's own literal semantics** (`ast.literal_eval`
# of the literal as the model wrote it), which is what Spark receives; the
# expected `re.sub` result is derived from that value. The one number that
# is *derived from the fence change* rather than from `re` —
# `source_text_sub_result` — is used only in
# `TestEscapedPatternOracleIntegrity` to prove the two candidate values
# give **different** answers, i.e. that the red is not vacuous. It is never
# an expectation for the converter.
# ══════════════════════════════════════════════════════════════════════


def p41_plain_literal_body(literal_text: str) -> str:
    """`_string`'s current return (`:756`) for a plain `"…"` literal: the text between the quotes.

    Transcribed the same way `p41_passes_double_quote_fence` transcribes
    `:753`, and for the same reason: D3's cause is that this **source text**
    becomes the emitted pattern's **value**, so the divergence has to be
    computable here without asking the converter to report it.

    Guarded by the fence helper so it can only be handed a spelling `:756`
    would actually reach — at HEAD `segment[1:-1]` runs only after the
    `:753` check has passed.
    """
    assert p41_passes_double_quote_fence(literal_text), literal_text
    return literal_text[1:-1]


def p41_refusal_or_none(source: str) -> ConversionError | None:
    """The `ConversionError` *source* refuses with, or `None` when it converts.

    Neither `p41_emitted_body` (fails on a refusal) nor `p41_refusal`
    (fails on a conversion) can express *"this shape converts, so the
    failure mode here is a mismatch"* — both would report the fact as a
    failure rather than return it. This one returns the outcome so the
    failure-mode distinction can be **asserted** instead of narrated.
    """
    try:
        PySpark2PandasConverter.convert(source)
    except ConversionError as exc:
        return exc
    return None


class P41EscapedPatternCase(NamedTuple):
    """A NON-RAW pattern whose parsed value differs from its source text, plus its regex facts.

    `literal` is the pattern exactly as a model would write it in the
    PySpark block, quotes included. `intended_value` is its **parsed
    value** — what Spark receives, and what 2.12 requires the emitted
    pattern to be equivalent to — derived from Python's literal semantics
    rather than from any emission.

    `source_text_sub_result` is what you get when the **source text**
    becomes the value, which is exactly what a plain→raw fence change does
    (`design.md` "Hypothesized Root Cause"). It is here to prove the two
    candidate values disagree, so the bug-condition red below cannot be
    satisfied by both being right; it is **not** an expectation for the
    converter, and no assertion compares an emission against it.
    """

    case_id: str
    #: The pattern literal as written in the PySpark source, with its quotes.
    literal: str
    source: str
    #: The literal's parsed value — what Spark receives (2.12's oracle).
    intended_value: str
    subject: str
    #: `re.sub(intended_value, "", subject)` — what Spark's regex does.
    intended_sub_result: str
    #: `re.sub(p41_plain_literal_body(literal), "", subject)` — what the
    #: fence change produces instead. Non-vacuity evidence only.
    source_text_sub_result: str


def p41_escaped_pattern_case(
    case_id: str, literal: str, subject: str, intended_sub_result: str, source_text_sub_result: str
) -> P41EscapedPatternCase:
    """Build a `P41EscapedPatternCase`, deriving the intended value and the block.

    The intended value is `ast.literal_eval(literal)` — Python's own answer
    for the literal the model wrote — so the table cannot state a value that
    disagrees with the language. The block reuses task 2.1's builder, which
    keeps the literal's spelling the only thing that varies across this
    file's `regexp_replace` cases.

    The `SyntaxWarning` suppression mirrors `convert`'s own (`:1372-1376`)
    and is for the same reason: `literal_eval` compiles the literal, and an
    **unrecognised** escape such as the control's `\\d` warns. The warning is
    about the model's text, not about anything under test, and **no test here
    depends on it being raised** (`design.md`'s D3 section, `tasks.md` 7.1).
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        value = ast.literal_eval(literal)
    assert isinstance(value, str), literal
    return P41EscapedPatternCase(
        case_id=case_id,
        literal=literal,
        source=p41_replace_block(literal),
        intended_value=value,
        subject=subject,
        intended_sub_result=intended_sub_result,
        source_text_sub_result=source_text_sub_result,
    )


#: `bugfix.md` 1.16's reproduced shape, and the ONE D3 counterexample this
#: task carries. The literal is `"[^\\d]"` as the model writes it: non-raw,
#: with a **doubled** backslash, so Python's recognised `\\` escape makes the
#: parsed value `[^\d]` — a negated digit class — while the source text is
#: `[^\\d]`, a class negating "backslash or d", which deletes every character
#: of the subject instead of the non-digits.
P41_D3_ESCAPED_CASE = p41_escaped_pattern_case(
    case_id="nonraw-escaped-negated-digit-class",
    literal=r'"[^\\d]"',
    subject="ab-12 34c",
    intended_sub_result="1234",
    source_text_sub_result="",
)

#: The boundary the design draws, as a control: the same class spelled with a
#: **single** backslash. `\d` is an *unrecognised* escape, so Python leaves it
#: alone and the parsed value equals the source text — which means the fence
#: change is harmless and this shape is **not** a D3 shape. It converts
#: correctly today (`bugfix.md` 3.10's family) and must keep doing so, so it is
#: GREEN throughout and is what stops the red above being read as "escaped
#: literals are mangled".
P41_UNRECOGNISED_ESCAPE_CONTROL = p41_escaped_pattern_case(
    case_id="nonraw-unrecognised-escape-digit-class",
    literal=r'"[^\d]"',
    subject="ab-12 34c",
    intended_sub_result="1234",
    source_text_sub_result="1234",
)


class TestTheD3FailureModeIsAValueMismatch:
    """This shape CONVERTS, so a red in this section is a mismatch and never a refusal.

    **Validates: Requirements 1.16, 1.20's distinction, 2.12**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN**, at HEAD and after T3. No red window.
    ══════════════════════════════════════════════════════════════════

    The class that makes *"red for a different reason"* a fact rather than a
    comment. `bugfix.md` 1.16 and 1.20: D3's shape is a **successful**
    conversion whose meaning differs from the input, which is why a property
    checking only for the absence of a `ConversionError` passes on it.

    Green before and after 4.3 because the literal is plain double-quoted and
    passes the `:753` fence either way — the fix moves its **value**, not its
    acceptance. A red here therefore means something entirely different from
    a red in the next class: the fence started refusing a spelling this
    checker has always accepted, which no clause of this spec asks for.
    """

    def test_the_shape_converts_and_does_not_refuse(self) -> None:
        """1.16: conversion SUCCEEDS. Stated as an assertion so the failure mode is pinned.

        Deliberately the weakest possible check on the converter — `did it
        raise` — and that weakness is the point: `bugfix.md` 1.20 is that
        this is *all* the existing generative property asks, and that D3
        sails through it. Everything of substance is asserted in the next
        class, over the emitted pattern's value.
        """
        exc = p41_refusal_or_none(P41_D3_ESCAPED_CASE.source)
        assert exc is None, (
            "conversion REFUSED, so this section's failure mode is no longer the one it documents.\n"
            f"  source     : {P41_D3_ESCAPED_CASE.source}\n"
            f"  construct  : {exc.construct!r}\n"
            f"  reason     : {exc.reason!r}\n"
            "D3's shape is a plain double-quoted literal: it passes `:753`'s fence before AND after "
            "T3, so it must convert throughout. A refusal here is a finding about the fence, not a "
            "D3 finding."
        )

    def test_the_control_shape_also_converts(self) -> None:
        """The unrecognised-escape control converts too, so the pair differ only in the value."""
        assert p41_refusal_or_none(P41_UNRECOGNISED_ESCAPE_CONTROL.source) is None

    def test_the_emitted_pattern_is_a_readable_string_literal(self) -> None:
        """And the emission is one `.str.replace` pattern whose value can be read back.

        The anti-vacuity guard for the next class: if the emission's shape
        moved, `p41_emitted_pattern_value` would fail on its structural
        asserts and the value mismatch below would never be reached, so a
        real D3 red and a moved emission would be indistinguishable.
        """
        value = p41_emitted_pattern_value(P41_D3_ESCAPED_CASE.source, P41_REPLACE_ACCESSOR)
        assert isinstance(value, str)
        assert value, "the emitted pattern is empty, so nothing about its regex value is meaningful"


class TestNonRawEscapedPatternKeepsItsRegexValue:
    """`:845` — a non-raw escaped pattern's emitted value is the literal's PARSED value.

    **Validates: Requirements 1.16, 2.12, Property 1**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **RED until task 4.3**, as a VALUE MISMATCH.
    ══════════════════════════════════════════════════════════════════

    2.12: *the regex that reaches pandas SHALL have the same value as the
    regex Spark would receive.* Today it does not — `_string` returns source
    text (`:756`) and `:847` re-wraps it in an `r"…"` fence, so the doubled
    backslash the model wrote to mean one backslash survives as two.

    **Do NOT adjust either assertion, and do NOT touch a production line to
    make one pass.** The window closes at task 4.3.
    """

    def test_the_emitted_pattern_keeps_the_literals_parsed_value(self) -> None:
        """2.12: the emitted pattern's VALUE equals the literal's parsed value.

        `tasks.md` 2.4's assertion, verbatim: the emitted pattern's value is
        `[^\\d]` — five characters, `[`, `^`, backslash, `d`, `]`. Today it is
        `[^\\\\d]`, six characters with the backslash doubled.

        Read through `p41_emitted_pattern_value`, which `ast.literal_eval`s
        the emitted literal under **its own** fence, so a `r"…"` emission and
        a plain `"…"` emission carrying the same regex compare equal. The
        assertion is about the regex, not about how the converter chose to
        spell it — which is what lets 4.3 satisfy it with either fence.
        """
        emitted = p41_emitted_pattern_value(P41_D3_ESCAPED_CASE.source, P41_REPLACE_ACCESSOR)
        assert emitted == P41_D3_ESCAPED_CASE.intended_value

    def test_the_emitted_pattern_behaves_as_the_intended_regex(self) -> None:
        """1.16 as behaviour: `re.sub` with the emitted pattern strips the non-digits.

        A value comparison alone would not say what the divergence *costs*.
        `re` is the oracle — pandas is never imported (`bugfix.md` 3.14) — so
        this is evidence about the translation, not about the execution. The
        two candidate values are not subtly different: the intent keeps
        `'1234'` and the source text deletes the whole string.
        """
        emitted = p41_emitted_pattern_value(P41_D3_ESCAPED_CASE.source, P41_REPLACE_ACCESSOR)
        assert re.sub(emitted, "", P41_D3_ESCAPED_CASE.subject) == P41_D3_ESCAPED_CASE.intended_sub_result

    def test_the_unrecognised_escape_control_already_converts_correctly(self) -> None:
        """GREEN throughout: the single-backslash spelling is not a D3 shape.

        Value equals source text, so the plain→raw fence change is harmless
        and this pattern is correct today. `bugfix.md` 1.19 / 3.10 and
        `design.md`'s narrowness argument, made checkable in the same
        register as the red above: the defect is the *doubled* backslash, not
        escaping in general.
        """
        emitted = p41_emitted_pattern_value(P41_UNRECOGNISED_ESCAPE_CONTROL.source, P41_REPLACE_ACCESSOR)
        assert emitted == P41_UNRECOGNISED_ESCAPE_CONTROL.intended_value
        assert (
            re.sub(emitted, "", P41_UNRECOGNISED_ESCAPE_CONTROL.subject)
            == P41_UNRECOGNISED_ESCAPE_CONTROL.intended_sub_result
        )


class TestEscapedPatternOracleIntegrity:
    """This section's table states Python and regex facts. These tests check them.

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN** at HEAD and after T3. Not bug-condition
    cases — count them the way `TestOracleIntegrity` is counted in
    `test_converter_equivalence_baseline.py`.
    ══════════════════════════════════════════════════════════════════

    `LGR-28b4d75-5` cuts both ways: the oracle has to be checkable too. A
    wrong `intended_value` or a subject that both candidate patterns treat
    alike would make the red above fail for a reason that has nothing to do
    with the converter — and at 4.3 it would fail to go green and read as an
    implementation problem.
    """

    def test_the_intended_value_is_pythons_own_answer(self) -> None:
        """The value Spark receives is `ast.literal_eval` of the literal the model wrote.

        Not a transcription of anything: the intended value is fixed by
        Python's literal semantics, and `\\\\` is a **recognised** escape, so
        `"[^\\\\d]"` denotes `[^\\d]`.
        """
        assert ast.literal_eval(P41_D3_ESCAPED_CASE.literal) == P41_D3_ESCAPED_CASE.intended_value
        assert P41_D3_ESCAPED_CASE.intended_value == r"[^\d]"

    def test_the_case_satisfies_the_d3_bug_condition(self) -> None:
        """`design.md`'s D3 predicate, evaluated over source text: not raw, and value != text.

        The literal carries no raw prefix, and `p41_plain_literal_body` — the
        transcription of what `_string` returns today (`:756`) — differs from
        the parsed value. That difference *is* the defect, computed here from
        the language rather than observed from an emission.
        """
        prefix, _remainder = p41_strip_raw_prefix(P41_D3_ESCAPED_CASE.literal)
        assert prefix == "", "this case must be NON-raw; a raw body is task 2.1's subject"
        assert p41_plain_literal_body(P41_D3_ESCAPED_CASE.literal) != P41_D3_ESCAPED_CASE.intended_value
        assert p41_plain_literal_body(P41_D3_ESCAPED_CASE.literal) == r"[^\\d]"

    def test_the_control_does_not_satisfy_the_d3_bug_condition(self) -> None:
        """The boundary, as the same predicate: an unrecognised escape has text == value.

        Why D3 needs a *double* backslash, why `rlike("@example\\.com$")`
        converts correctly today, and why 0 of 56 measured generated versions
        hit it (`bugfix.md` 1.19). Python leaves `\\d` alone.
        """
        prefix, _remainder = p41_strip_raw_prefix(P41_UNRECOGNISED_ESCAPE_CONTROL.literal)
        assert prefix == ""
        assert (
            p41_plain_literal_body(P41_UNRECOGNISED_ESCAPE_CONTROL.literal)
            == P41_UNRECOGNISED_ESCAPE_CONTROL.intended_value
        )

    def test_both_candidate_values_produce_the_recorded_results(self) -> None:
        """`re.sub` agrees with the table, for the intent and for the source text alike."""
        case = P41_D3_ESCAPED_CASE
        assert re.sub(case.intended_value, "", case.subject) == case.intended_sub_result
        assert re.sub(p41_plain_literal_body(case.literal), "", case.subject) == case.source_text_sub_result

    def test_the_two_candidate_values_disagree_on_this_subject(self) -> None:
        """Non-vacuity: the subject distinguishes the intent from the fence-changed value.

        Without this, the red above would be satisfiable by a subject both
        patterns treat identically — a test that looks like it detects D3 and
        does not. The two answers here are `'1234'` and `''`: the intent
        keeps the digits, the source text deletes everything, which is
        `bugfix.md` 1.16's reproduction as a fact about `re`.
        """
        case = P41_D3_ESCAPED_CASE
        assert case.intended_sub_result != case.source_text_sub_result
        assert case.intended_sub_result == "1234"
        assert case.source_text_sub_result == ""

    def test_the_same_doubling_inverts_a_match_set_at_the_rlike_site(self) -> None:
        """1.17, in this section's register and as a fact about `re` only.

        At `regexp_replace`'s site the fence change shows as a changed
        **replacement result**; at `rlike`'s it shows as a changed **match
        set**, and the inversion is exact — `@example\\\\.com$` matches
        `a@example\\.com` and fails `a@example.com`, which is the intent
        backwards. The converter assertion at `:1232`/`:1233` is **task
        7.1's**; this is the semantic half, so 1.17 is stated where it is
        argued rather than assumed.
        """
        intended = r"@example\.com$"
        fence_changed = p41_plain_literal_body(r'"@example\\.com$"')
        assert fence_changed == r"@example\\.com$"
        assert re.search(intended, "a@example.com") is not None
        assert re.search(intended, "a@example\\.com") is None
        # Exactly inverted, which is why a one-directional match assertion
        # cannot see this defect (the file header's argument for the pair).
        assert re.search(fence_changed, "a@example.com") is None
        assert re.search(fence_changed, "a@example\\.com") is not None

    def test_the_case_source_is_the_task_shape(self) -> None:
        """`tasks.md` 2.4's own shape, so a reader can find it unaltered.

        The task names `regexp_replace(col("p"), "[^\\\\d]", "")`; this file's
        builder spells the column `phone_number` and wraps it in the
        `withColumn` statement the converter needs, which is the same shape
        with the same literal.
        """
        assert '"[^\\\\d]"' in P41_D3_ESCAPED_CASE.source
        assert P41_D3_ESCAPED_CASE.source == p41_replace_block(r'"[^\\d]"')
        assert "regexp_replace(" in P41_D3_ESCAPED_CASE.source


# ══════════════════════════════════════════════════════════════════════
# TASK 7.1 — SITE-COMPLETE ESCAPE FIDELITY AT BOTH RAW-FENCE SITES.
#
# **Validates: Requirements 1.16, 1.17, 1.18, 2.12, 2.14, Property 1**
#
# ══════════════════════════════════════════════════════════════════
# EXPECTED OUTCOME: **GREEN on the first run.** No red window.
# ══════════════════════════════════════════════════════════════════
#
# Task 2.4 carried D3's one red, closed at 4.3. This section is the
# site-complete sweep, and it is green by construction: the value contract
# *prevents* D3 rather than fixing it (`design.md` §5.6), so these
# assertions exist to make a future revert to source text fail here, at
# both `r"…"` sites, for every escape class.
#
# **A red here is a real finding about the value contract, not a test to
# adjust** (`tasks.md` 7.1's hard constraint). Nothing in this section may
# be made green by moving a baseline pin; if that were ever the only way,
# 2.14 and §7's fallback say to record D3's remainder as a known divergence
# **at the assertion** and ship T3 regardless. **Nothing had to be
# recorded** — every case below is green with no pin moved.
#
# ORACLE RULE: unchanged from the rest of the file — **regex semantics via
# the standard library `re`, and Python's own literal semantics. Never
# current converter output.** Each case states the value Spark receives
# (`ast.literal_eval` of the literal the model wrote), the `re.sub` result
# that value produces on a fixture, and the match/non-match pair it
# separates. `TestTheEscapeSweepOracleIntegrity` checks every one of those
# claims against `re` before any of them is used as an expectation.
#
# ──────────────────────────────────────────────────────────────────────
# THE THREE ESCAPE CLASSES, AND WHY THE MIDDLE ONE HAD TO BE NAMED
# ──────────────────────────────────────────────────────────────────────
#
# `tasks.md` 7.1 asks for "the escapes that actually differ between fences
# — `\\`, `\n`, `\t`, `\b`, `\"` — and at least one that does not". Writing
# the sweep showed that "differ between fences" and "differ as regexes" are
# **not the same set**, and saying so is the difference between a sweep that
# reports what it found and one that overclaims. Verified against `re` in
# `TestTheEscapeSweepOracleIntegrity`, which is why it is stated as a
# finding rather than as a guess:
#
#   1. **recognised-divergent (4 cases)** — value differs from source text
#      AND the two are different regexes. `\\` and `\b` only. These are the
#      escapes that make D3 a *wrong answer*: `"[^\\d]"` means "not a
#      digit" and its source text means "not a backslash and not d";
#      `"\\bcat\\b"` means word-boundary-delimited `cat` and its source text
#      means a literal backslash followed by `bcat`.
#   2. **recognised-equivalent (4 cases)** — value differs from source text,
#      but `re` happens to read both the same way, because **the regex
#      engine recognises the same escape the Python fence does**: `\n`, `\r`,
#      `\t` and `\"`. A newline character and the two characters `\n` are
#      one regex. So for these the fence change never produced a wrong
#      regex — what it produced (or, under the value contract, what it must
#      cope with) is a **value a raw fence cannot spell**, which is a
#      different hazard and the one this section drives deliberately.
#   3. **unrecognised (4 cases)** — value **equals** source text, because
#      Python leaves unrecognised escapes alone: `\.`, `\d`, `\w`, `\s`.
#      Nothing differs, at either fence, under either contract. This is why
#      D3 needs a **doubled** backslash to manifest, why
#      `rlike("@example\\.com$")` converts correctly today
#      (`bugfix.md` 3.10 pins it), and why **0 of 56 measured generated
#      versions hit D3** (1.19) — the generator writes class 3.
#
# `TestUnrecognisedEscapesPinTheBoundary` states the boundary as one
# assertion in the other direction too: after the fix, `"[^\\d]"` and
# `"[^\d]"` — the doubled and single spellings — emit **the same regex**,
# which is precisely why only the doubled one was ever wrong.
#
# ──────────────────────────────────────────────────────────────────────
# `_raw`'s PLAIN-FENCE FALLBACK — THIS SECTION IS THE FIRST THING THAT
# DRIVES IT DELIBERATELY, SO THE ARM IS ASSERTED AND NOT ASSUMED
# ──────────────────────────────────────────────────────────────────────
#
# `_raw` returns `("r", value)` when the value can be spelled in a raw
# fence and `("", _dq(value))` when it cannot (`design.md` §5.1,
# `tasks.md` 4.1). **The fallback is reached by nothing measured**: 0 of the
# 79 baseline cases and 0 of the 8 `P40_UNPINNED_CAST_KEY_PINS` take it,
# which is exactly what makes the value contract's blast radius on existing
# behaviour zero — and also what makes a third code path a real cost
# (`tasks.md` 13.1's "cost taken in exchange").
#
# So three of the twelve cases below reach it, on purpose:
#
# | case | value | arm | why |
# |---|---|---|---|
# | `escaped-newline-forces-fallback` | LF | **fallback** | a raw fence cannot carry a line break at all |
# | `escaped-carriage-return-forces-fallback` | CR LF | **fallback** | same, for the newline's twin |
# | `escaped-quote-forces-fallback` | `[^"]` | **fallback** | the `"` closes the fence early |
# | the other nine | — | raw | spellable, so byte-identical to what these sites always wrote |
#
# **The arm is asserted per case, at both sites**, so a future change that
# moved a value between arms is visible here rather than silent — and
# `test_the_fallback_arm_is_reached_by_this_file` fails if the fallback set
# ever empties, which would mean this section stopped exercising the third
# path it was written to exercise.
#
# **The expected arm is derived from the LANGUAGE, not from
# `_RAW_UNSPELLABLE`.** `p41_raw_fence_can_spell` asks the compiler whether
# `r"<value>"` parses *and* means the value — the same oracle
# `test_fence_helpers_round_trip.py` (task 4.4) uses, and stronger than
# transcribing the production tuple, which would make the test agree with
# the implementation by construction rather than with Python.
#
# **Both arms carry the same regex, which is the property that makes the
# fallback safe** (`_raw`'s docstring): the engine is handed a *value* and
# never sees the literal's spelling. Every assertion here is therefore over
# the value — `p41_emitted_pattern_value` `ast.literal_eval`s the emitted
# literal under **its own** fence — so a raw emission and a plain emission
# carrying one regex compare equal, and the two arms are compared on equal
# terms.
#
# ──────────────────────────────────────────────────────────────────────
# `\b` IS THE ONE WORTH READING TWICE
# ──────────────────────────────────────────────────────────────────────
#
# `\b` means different things in the two languages this file straddles, and
# the sweep covers both sides:
#
# - `"\\bcat\\b"` (**doubled**) has value `\bcat\b` — backslash, `b` — which
#   `re` reads as a **word boundary**. Emitted `r"\bcat\b"`, raw arm.
# - `"\b"` (**single**) has value `\x08` — a **backspace character** — which
#   `re` reads as a literal backspace. Emitted `r"<BS>"`, raw arm; the
#   control character round-trips through a raw fence, which task 4.4's
#   exhaustive code-point sweep already establishes for every code point
#   below U+E000.
#
# So the same two source characters mean "word boundary" in one spelling and
# "backspace" in the other, and `re` is the oracle for which is which. A
# sweep that covered only one of them would look complete and would miss
# the half where the fence and the engine disagree.
#
# ──────────────────────────────────────────────────────────────────────
# THE `SyntaxWarning`, AND WHY NO TEST HERE DEPENDS ON IT
# ──────────────────────────────────────────────────────────────────────
#
# `convert` suppresses the `SyntaxWarning` Python raises for an unrecognised
# escape (`:1596-1602`), and that stays: the warning is about the **model's**
# text, not about the converter's behaviour, and it is also why class 3
# above is invisible in production. **No assertion here depends on the
# warning being raised** (`tasks.md` 7.1's explicit instruction).
# `test_an_unrecognised_escape_converts_even_when_warnings_are_errors`
# asserts the opposite direction — the suppression holds — which is a pin on
# the behaviour that stays, not on a warning being emitted. The case
# builders suppress the warning locally around `ast.literal_eval`, exactly
# as `p41_escaped_pattern_case` above does, because compiling the model's
# literal to read its value is not a place a warning is a finding.
#
# ──────────────────────────────────────────────────────────────────────
# Scope
# ──────────────────────────────────────────────────────────────────────
#
# Both raw-fence sites, every case through both: `regexp_replace`'s pattern
# (`:845`, emitted `:847`) where the divergence shows as a changed
# **replacement result**, and `rlike`'s pattern in filter position
# (`:1232`, emitted `:1233`) where it shows as a changed **match set**
# (1.17). Plain-fence sites are task 6.1's, in
# `test_raw_literal_plain_sites.py`.
#
# pandas is never imported (`bugfix.md` 3.14), so a green run here is
# evidence about the **translation** and not about the execution.
# ══════════════════════════════════════════════════════════════════════

#: The fence `_raw` chose, as it appears at the start of the emitted literal.
#: `"r"` for the raw arm, `""` for the plain-fence fallback.
P41_RAW_ARM = "r"
P41_FALLBACK_ARM = ""

#: The three escape classes the sweep partitions into. Named rather than
#: inlined because the middle one is a finding (see this section's header) and
#: a reader needs to be able to grep for it.
P41_ESCAPE_RECOGNISED_DIVERGENT = "recognised-divergent"
P41_ESCAPE_RECOGNISED_EQUIVALENT = "recognised-equivalent"
P41_ESCAPE_UNRECOGNISED = "unrecognised"

#: The escapes `tasks.md` 7.1 requires covered because they differ between the
#: two Python fences. Class 1 and class 2 together must contain all five.
P41_REQUIRED_DIFFERING_ESCAPES = (r"\\", r"\n", r"\t", r"\b", r"\"")

#: The escapes 7.1 requires at least one of because they do **not** differ —
#: Python leaves them alone. All four are covered, which is a stronger
#: statement than the task asks for and costs nothing.
P41_REQUIRED_UNRECOGNISED_ESCAPES = (r"\.", r"\d", r"\w", r"\s")


def p41_raw_fence_can_spell(value: str) -> bool:
    """Whether ``r"<value>"`` is a literal that PARSES and MEANS *value*.

    The expected-arm oracle, and it is **the language** rather than a
    transcription of the production exclusion tuple. A test that re-stated
    ``_RAW_UNSPELLABLE`` would agree with the implementation by construction;
    this one agrees with Python, so an exclusion added or dropped for the
    wrong reason shows up as a disagreement.

    Three failure modes, all folded into one answer because all three mean the
    same thing to a caller — the raw arm is not available:

    - the literal does not parse (``"``, LF, CR each end it early; a NUL makes
      the source unusable; an odd trailing backslash run escapes the closing
      quote);
    - the literal parses but means something else (no such value reaches these
      sites today, and the equality check is what would notice if one did);
    - the source cannot be encoded as UTF-8 at all, which is the lone-surrogate
      class — it breaks the emitted **module** rather than the literal, so it
      is checked before parsing.
    """
    literal = P41_RAW_ARM + '"' + value + '"'
    try:
        literal.encode("utf-8")
    except UnicodeEncodeError:
        return False
    try:
        parsed = ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return False
    return bool(parsed == value)


def p41_expected_fence(value: str) -> str:
    """The arm `_raw` must take for *value*: raw when spellable, fallback otherwise."""
    return P41_RAW_ARM if p41_raw_fence_can_spell(value) else P41_FALLBACK_ARM


def p41_emitted_pattern_segment(source: str, accessor: str) -> str:
    """The emitted regex literal's **source text**, fence included.

    The segment-level twin of :func:`p41_emitted_pattern_value`, which resolves
    the same literal to its value. Both are needed here and neither can be
    written in terms of the other: the value is what every fidelity assertion
    is about, and the *fence* — which only the segment shows — is what makes a
    move between `_raw`'s two arms visible.

    The walk is duplicated rather than factored out so task 2.1's helper is
    left byte-unchanged; `test_the_two_pattern_readers_agree` pins the two
    together so the duplication cannot drift into disagreement.
    """
    body = p41_emitted_body(source)
    tree = ast.parse(body)

    segments: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != accessor:
            continue
        if not (isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "str"):
            continue
        assert node.args, f"no positional argument to .str.{accessor} in:\n{body}"
        segment = ast.get_source_segment(body, node.args[0])
        assert segment is not None, body
        segments.append(segment)

    assert len(segments) == 1, f"expected exactly one .str.{accessor} pattern in:\n{body}\nfound {len(segments)}"
    return segments[0]


def p41_emitted_pattern_fence(source: str, accessor: str) -> str:
    """Which of `_raw`'s two arms the emitted literal came out of."""
    segment = p41_emitted_pattern_segment(source, accessor)
    return P41_RAW_ARM if segment[:1] in P41_RAW_PREFIXES else P41_FALLBACK_ARM


class P41EscapeCase(NamedTuple):
    """One NON-RAW pattern literal, swept at both raw-fence sites.

    *literal* is the pattern exactly as a model would write it in the PySpark
    block, quotes included. Everything named "intended" is what **Spark**
    receives — ``ast.literal_eval`` of that literal, i.e. Python's own answer
    for what the model wrote — and is what 2.12 requires the emitted pattern to
    be equivalent to.

    Everything named "source_text" is what the **pre-fix** converter used as the
    value instead (``segment[1:-1]``, transcribed by
    :func:`p41_plain_literal_body`). Those fields are **non-vacuity evidence
    only**: they are what makes each case's escape class checkable, and no
    assertion ever compares an emission against them.
    """

    case_id: str
    #: One of the three `P41_ESCAPE_*` classes. Checked against `re` in
    #: `TestTheEscapeSweepOracleIntegrity`, never taken on trust.
    escape_class: str
    #: The pattern literal as written in the PySpark source, with its quotes.
    literal: str
    #: `ast.literal_eval(literal)` — the regex Spark receives.
    intended_value: str
    #: `literal[1:-1]` — the regex the pre-fix converter emitted instead.
    source_text_body: str
    #: `P41_RAW_ARM` or `P41_FALLBACK_ARM`, derived from the language by
    #: :func:`p41_raw_fence_can_spell`.
    expected_fence: str
    #: The block carrying *literal* at `:845` and at `:1232`.
    replace_source: str
    rlike_source: str
    #: `re.sub(intended_value, "", subject) == sub_result` — the `:845` fact.
    subject: str
    sub_result: str
    #: What the source-text candidate substitutes instead. Evidence only.
    source_text_sub_result: str
    #: `re.search(intended_value, …)` matches *matching* and not *non_matching*
    #: — the `:1232` fact, as a pair, because one direction alone cannot see an
    #: inverted match set (1.17).
    matching: str
    non_matching: str
    #: What the source-text candidate answers for that same pair. Evidence only.
    source_text_match_set: "tuple[bool, bool]"


def p41_escape_case(
    case_id: str,
    escape_class: str,
    literal: str,
    subject: str,
    sub_result: str,
    source_text_sub_result: str,
    matching: str,
    non_matching: str,
    source_text_match_set: "tuple[bool, bool]",
) -> P41EscapeCase:
    """Build a :class:`P41EscapeCase`, deriving the value, the arm and both blocks.

    The intended value is ``ast.literal_eval(literal)``, so the table cannot
    state a value that disagrees with the language; the source-text body is
    :func:`p41_plain_literal_body`, the transcription of what `_string` returned
    before T3; and the expected arm is :func:`p41_expected_fence`, derived from
    whether Python can spell the value in a raw fence. Nothing here is
    observed from an emission.

    The ``SyntaxWarning`` suppression mirrors `convert`'s own
    (`:1596-1602`) and is for the same reason: ``literal_eval`` compiles the
    literal, and class 3's unrecognised escapes warn. **No test depends on that
    warning being raised.**
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        value = ast.literal_eval(literal)
    assert isinstance(value, str), literal
    return P41EscapeCase(
        case_id=case_id,
        escape_class=escape_class,
        literal=literal,
        intended_value=value,
        source_text_body=p41_plain_literal_body(literal),
        expected_fence=p41_expected_fence(value),
        replace_source=p41_replace_block(literal),
        rlike_source=p41_filter_rlike_block(literal),
        subject=subject,
        sub_result=sub_result,
        source_text_sub_result=source_text_sub_result,
        matching=matching,
        non_matching=non_matching,
        source_text_match_set=source_text_match_set,
    )


P41_ESCAPE_CASES: list[P41EscapeCase] = [
    # ══════════════════════════════════════════════════════════════════
    # CLASS 1 — recognised AND regex-divergent. `\\` and `\b` only.
    # These are the escapes that make D3 a WRONG ANSWER rather than an
    # unspellable value.
    # ══════════════════════════════════════════════════════════════════
    # ── `bugfix.md` 1.16's shape. Value `[^\d]` is "not a digit"; the source
    # text `[^\\d]` is "not a backslash and not d", which matches digits too,
    # so it deletes the whole subject instead of the non-digits.
    p41_escape_case(
        case_id="escaped-backslash-negated-digit-class",
        escape_class=P41_ESCAPE_RECOGNISED_DIVERGENT,
        literal=r'"[^\\d]"',
        subject="ab-12 34c",
        sub_result="1234",
        source_text_sub_result="",
        matching="ab",
        non_matching="1234",
        source_text_match_set=(True, True),
    ),
    # ── The escape at its plainest: a pattern that matches ONE literal
    # backslash. Value `\\` (two characters) is an escaped backslash; the
    # source text `\\\\` (four) matches TWO consecutive backslashes and so
    # misses the subject entirely. Also the even-trailing-run case at the
    # converter level — an even run stays on the raw arm, and this is the
    # site-level version of that rule.
    p41_escape_case(
        case_id="escaped-backslash-literal-backslash",
        escape_class=P41_ESCAPE_RECOGNISED_DIVERGENT,
        literal=r'"\\\\"',
        subject="a\\b",
        sub_result="ab",
        source_text_sub_result="a\\b",
        matching="a\\b",
        non_matching="ab",
        source_text_match_set=(False, False),
    ),
    # ── `\b` DOUBLED: value `\bcat\b`, which `re` reads as a WORD BOUNDARY,
    # so it strips the standalone word and leaves `concatenate` alone. The
    # source text `\\bcat\\b` is a literal backslash followed by `bcat`, which
    # matches neither subject. Half one of the pair the header calls out.
    p41_escape_case(
        case_id="escaped-b-doubled-is-a-word-boundary",
        escape_class=P41_ESCAPE_RECOGNISED_DIVERGENT,
        literal=r'"\\bcat\\b"',
        subject="cat concatenate",
        sub_result=" concatenate",
        source_text_sub_result="cat concatenate",
        matching="a cat here",
        non_matching="concatenate",
        source_text_match_set=(False, False),
    ),
    # ── `\b` SINGLE: value is one BACKSPACE character (`\x08`), which `re`
    # reads as that literal control character. The source text `\b` is a word
    # boundary, which matches `ab` — a subject the intent must NOT match. Half
    # two of the pair, and the one that shows the fence and the regex engine
    # disagreeing about the same two source characters.
    p41_escape_case(
        case_id="escaped-b-single-is-a-backspace",
        escape_class=P41_ESCAPE_RECOGNISED_DIVERGENT,
        literal=r'"\b"',
        subject="a\x08b",
        sub_result="ab",
        source_text_sub_result="a\x08b",
        matching="a\x08b",
        non_matching="ab",
        source_text_match_set=(True, True),
    ),
    # ══════════════════════════════════════════════════════════════════
    # CLASS 2 — recognised, value differs, but `re` reads both the same
    # because the regex engine recognises the SAME escape. The hazard here
    # is not a wrong regex; it is a value a raw fence cannot spell.
    # ══════════════════════════════════════════════════════════════════
    # ── LF. **Drives `_raw`'s plain-fence fallback.** A raw fence cannot carry
    # a line break at all, so `r"<LF>"` is not a literal — the value has to be
    # emitted through `_dq` into a plain `"…"` fence carrying the same regex.
    p41_escape_case(
        case_id="escaped-newline-forces-fallback",
        escape_class=P41_ESCAPE_RECOGNISED_EQUIVALENT,
        literal=r'"\n"',
        subject="line1\nline2",
        sub_result="line1line2",
        source_text_sub_result="line1line2",
        matching="a\nb",
        non_matching="ab",
        source_text_match_set=(True, False),
    ),
    # ── CR LF, the newline's twin, and a realistic one: a Windows line ending
    # in a pattern. Also drives the fallback, and its non-matching subject is
    # an LF-only string, so the case would notice a pattern that had quietly
    # lost its CR.
    p41_escape_case(
        case_id="escaped-carriage-return-forces-fallback",
        escape_class=P41_ESCAPE_RECOGNISED_EQUIVALENT,
        literal=r'"\r\n"',
        subject="a\r\nb",
        sub_result="ab",
        source_text_sub_result="ab",
        matching="a\r\nb",
        non_matching="a\nb",
        source_text_match_set=(True, False),
    ),
    # ── TAB, and it is the CONTRAST with LF rather than another instance of
    # it: a tab is legal unescaped in **both** fences, so its value stays on
    # the raw arm. That is what says the exclusion set is not over-broad —
    # pushing a tab onto the fallback would change a pattern's emitted
    # spelling for no reason.
    p41_escape_case(
        case_id="escaped-tab-stays-raw",
        escape_class=P41_ESCAPE_RECOGNISED_EQUIVALENT,
        literal=r'"\t"',
        subject="a\tb",
        sub_result="ab",
        source_text_sub_result="ab",
        matching="a\tb",
        non_matching="a b",
        source_text_match_set=(True, False),
    ),
    # ── The escaped quote, as a realistic regex: a class negating `"`. Value
    # `[^"]` carries the fence's own quote, so it **drives the fallback** too.
    p41_escape_case(
        case_id="escaped-quote-forces-fallback",
        escape_class=P41_ESCAPE_RECOGNISED_EQUIVALENT,
        literal=r'"[^\"]"',
        subject='a"b',
        sub_result='"',
        source_text_sub_result='"',
        matching='a"b',
        non_matching='""',
        source_text_match_set=(True, False),
    ),
    # ══════════════════════════════════════════════════════════════════
    # CLASS 3 — unrecognised. Value EQUALS source text, so nothing differs
    # under either contract. The boundary that explains why D3 needed a
    # doubled backslash and why 0 of 56 generated versions hit it.
    # ══════════════════════════════════════════════════════════════════
    # ── `\.` — and it is `_string`'s own falsified docstring example
    # (`bugfix.md` 1.18), which is why it is worth having in the sweep by name:
    # this spelling always survived, and the docstring generalised from it to
    # the doubled spelling, which does not.
    p41_escape_case(
        case_id="unrecognised-escape-dot",
        escape_class=P41_ESCAPE_UNRECOGNISED,
        literal=r'"@example\.com$"',
        subject="a@example.com",
        sub_result="a",
        source_text_sub_result="a",
        matching="a@example.com",
        non_matching="a@exampleXcom",
        source_text_match_set=(True, False),
    ),
    # ── `\d` — the single-backslash twin of class 1's first case. The pair is
    # what `TestUnrecognisedEscapesPinTheBoundary` compares.
    p41_escape_case(
        case_id="unrecognised-escape-d",
        escape_class=P41_ESCAPE_UNRECOGNISED,
        literal=r'"[^\d]"',
        subject="ab-12 34c",
        sub_result="1234",
        source_text_sub_result="1234",
        matching="ab",
        non_matching="1234",
        source_text_match_set=(True, False),
    ),
    p41_escape_case(
        case_id="unrecognised-escape-w",
        escape_class=P41_ESCAPE_UNRECOGNISED,
        literal=r'"\w+"',
        subject="ab 12",
        sub_result=" ",
        source_text_sub_result=" ",
        matching="ab",
        non_matching="  ",
        source_text_match_set=(True, False),
    ),
    p41_escape_case(
        case_id="unrecognised-escape-s",
        escape_class=P41_ESCAPE_UNRECOGNISED,
        literal=r'"\s+"',
        subject="a  b",
        sub_result="ab",
        source_text_sub_result="ab",
        matching="a b",
        non_matching="ab",
        source_text_match_set=(True, False),
    ),
]

#: The cases whose value cannot be spelled raw, so `_raw`'s plain-fence
#: fallback is the arm taken. **Three of twelve, and this section is the first
#: thing in the spec that reaches the fallback at all** — 0 of the 79 baseline
#: cases and 0 of the 8 `P40_UNPINNED_CAST_KEY_PINS` do.
P41_FALLBACK_ARM_ESCAPE_IDS = frozenset(
    {
        "escaped-newline-forces-fallback",
        "escaped-carriage-return-forces-fallback",
        "escaped-quote-forces-fallback",
    }
)


def p41_escape_cases_in_class(escape_class: str) -> list[P41EscapeCase]:
    """The sweep's cases in one escape class, for per-class parametrisation."""
    return [case for case in P41_ESCAPE_CASES if case.escape_class == escape_class]


P41_DIVERGENT_CASES = p41_escape_cases_in_class(P41_ESCAPE_RECOGNISED_DIVERGENT)
P41_EQUIVALENT_CASES = p41_escape_cases_in_class(P41_ESCAPE_RECOGNISED_EQUIVALENT)
P41_UNRECOGNISED_CASES = p41_escape_cases_in_class(P41_ESCAPE_UNRECOGNISED)

#: Class 1 and class 2 together are "the recognised escapes" — the ones whose
#: value differs from their source text at all.
P41_RECOGNISED_CASES = P41_DIVERGENT_CASES + P41_EQUIVALENT_CASES


def p41_escape_ids(cases: list[P41EscapeCase]) -> list[str]:
    return [case.case_id for case in cases]


class TestEscapeFidelityAtRegexpReplaceSite:
    """`:845` — every escape class keeps its regex value, as a REPLACEMENT result.

    **Validates: Requirements 1.16, 2.12, Property 1**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN.** No red window — the value contract
    prevents D3, so this is the assertion that makes a revert fail.
    ══════════════════════════════════════════════════════════════════

    2.12: *the regex that reaches pandas SHALL have the same value as the regex
    Spark would receive.* Asserted here at the site where a divergence shows up
    as a changed replacement result, which is the half of 1.17 that
    `regexp_replace` owns.
    """

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_emitted_pattern_keeps_the_literals_parsed_value(self, case: P41EscapeCase) -> None:
        """2.12, over all three escape classes and both `_raw` arms.

        Read through :func:`p41_emitted_pattern_value`, which resolves the
        emitted literal under **its own** fence — so a raw emission and a
        fallback emission carrying one regex both satisfy this. The assertion
        is about the regex, not about how the converter chose to spell it.
        """
        assert p41_emitted_pattern_value(case.replace_source, P41_REPLACE_ACCESSOR) == case.intended_value

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_emitted_pattern_produces_the_intended_replacement(self, case: P41EscapeCase) -> None:
        """The same claim as behaviour: `re.sub` with the emitted pattern agrees.

        A value comparison alone cannot distinguish equal-by-accident from
        equal-by-meaning, and it does not say what a divergence would cost.
        `re` is the oracle; pandas is never imported (`bugfix.md` 3.14), so this
        is evidence about the translation.
        """
        emitted = p41_emitted_pattern_value(case.replace_source, P41_REPLACE_ACCESSOR)
        assert re.sub(emitted, "", case.subject) == case.sub_result

    @pytest.mark.parametrize("case", P41_DIVERGENT_CASES, ids=p41_escape_ids(P41_DIVERGENT_CASES))
    def test_the_emitted_pattern_is_not_the_source_texts_replacement(self, case: P41EscapeCase) -> None:
        """Anti-vacuity for class 1: the emission is not what the pre-fix value gave.

        Without this, the two assertions above would be satisfied by a
        converter whose emission happened to agree with the intent on a subject
        the source-text candidate also agreed on — i.e. by a case that cannot
        see D3. Class 1 is exactly the set where the two candidates disagree,
        and this is that disagreement asserted at the emission.
        """
        emitted = p41_emitted_pattern_value(case.replace_source, P41_REPLACE_ACCESSOR)
        assert re.sub(emitted, "", case.subject) != re.sub(case.source_text_body, "", case.subject)


class TestEscapeFidelityAtRlikeSite:
    """`:1232` — every escape class keeps its regex value, as a MATCH SET.

    **Validates: Requirements 1.16, 1.17, 2.12, Property 1**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN.** No red window.
    ══════════════════════════════════════════════════════════════════

    1.17: at this site the divergence is a changed **match set** rather than a
    changed replacement, and for the `\\\\`-class escapes it is an exact
    inversion. Both directions are asserted for that reason — a one-directional
    match assertion passes on a pattern that matches everything, and a
    one-directional non-match assertion passes on one that matches nothing.
    """

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_emitted_pattern_keeps_the_literals_parsed_value(self, case: P41EscapeCase) -> None:
        """2.12 at the second raw-fence site, over all three escape classes."""
        assert p41_emitted_pattern_value(case.rlike_source, P41_CONTAINS_ACCESSOR) == case.intended_value

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_emitted_pattern_matches_the_intent_and_not_its_inverse(self, case: P41EscapeCase) -> None:
        """1.17 as a pair, which is the only formulation that can see an inversion."""
        emitted = p41_emitted_pattern_value(case.rlike_source, P41_CONTAINS_ACCESSOR)
        assert re.search(emitted, case.matching) is not None
        assert re.search(emitted, case.non_matching) is None

    @pytest.mark.parametrize("case", P41_DIVERGENT_CASES, ids=p41_escape_ids(P41_DIVERGENT_CASES))
    def test_the_emitted_pattern_is_not_the_source_texts_match_set(self, case: P41EscapeCase) -> None:
        """Anti-vacuity for class 1 at this site: the match sets genuinely differ.

        Stated over the pair rather than over either subject, because that is
        the granularity at which the two candidates differ: for
        `escaped-b-single-is-a-backspace` both match the matching subject and
        only the source text also matches the non-matching one.
        """
        emitted = p41_emitted_pattern_value(case.rlike_source, P41_CONTAINS_ACCESSOR)
        emitted_pair = (
            re.search(emitted, case.matching) is not None,
            re.search(emitted, case.non_matching) is not None,
        )
        source_text_pair = (
            re.search(case.source_text_body, case.matching) is not None,
            re.search(case.source_text_body, case.non_matching) is not None,
        )
        assert emitted_pair == (True, False)
        assert emitted_pair != source_text_pair


class TestWhichRawArmEachEscapedValueTakes:
    """`_raw`'s two arms, asserted per case at both sites.

    **Validates: Requirements 2.12, Property 1**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN.** No red window.
    ══════════════════════════════════════════════════════════════════

    **This section is the first thing in the spec that drives `_raw`'s
    plain-fence fallback deliberately**, so the arm is asserted rather than
    assumed: 0 of the 79 baseline cases and 0 of the 8
    `P40_UNPINNED_CAST_KEY_PINS` reach it, and until now the third code path
    was present but unexercised end to end.

    The expected arm comes from :func:`p41_raw_fence_can_spell` — Python's own
    answer to whether ``r"<value>"`` parses and means the value — never from
    the production exclusion tuple. Both arms carry the same regex, which is
    what makes the fallback safe rather than merely smaller; the fidelity
    assertions in the two classes above are what check that, and these
    assertions are what make an arm **moving** visible instead of silent.
    """

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_replace_site_emits_the_fence_the_value_can_be_spelled_in(self, case: P41EscapeCase) -> None:
        """`:845` takes the raw arm exactly when a raw fence can carry the value."""
        assert p41_emitted_pattern_fence(case.replace_source, P41_REPLACE_ACCESSOR) == case.expected_fence

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_rlike_site_emits_the_fence_the_value_can_be_spelled_in(self, case: P41EscapeCase) -> None:
        """`:1232` agrees with `:845` — one helper, two sites, one rule."""
        assert p41_emitted_pattern_fence(case.rlike_source, P41_CONTAINS_ACCESSOR) == case.expected_fence

    def test_the_fallback_arm_is_reached_by_this_file(self) -> None:
        """The fallback set is non-empty and is exactly the three documented cases.

        The anti-vacuity guard for this class. A sweep that reached only the
        raw arm would assert nothing about the third code path while looking
        like it did, and `_raw`'s fallback is unreachable from every measured
        input — so if this file stops reaching it, nothing else does.
        """
        reached = {case.case_id for case in P41_ESCAPE_CASES if case.expected_fence == P41_FALLBACK_ARM}
        assert reached, (
            "no case in this sweep reaches `_raw`'s plain-fence fallback. The fallback is a third "
            "code path that no baseline case and no measured generated version reaches, so this "
            "file is the only thing that exercises it end to end."
        )
        assert reached == set(P41_FALLBACK_ARM_ESCAPE_IDS)

    def test_the_raw_arm_is_reached_by_the_majority_of_cases(self) -> None:
        """And the raw arm is still the ordinary answer — the fallback is the exception.

        The other direction of the same guard: a change that pushed every value
        onto the fallback would satisfy the test above and would silently
        rewrite the spelling of every pattern these two sites emit.
        """
        raw_arm = {case.case_id for case in P41_ESCAPE_CASES if case.expected_fence == P41_RAW_ARM}
        assert len(raw_arm) == len(P41_ESCAPE_CASES) - len(P41_FALLBACK_ARM_ESCAPE_IDS)
        assert raw_arm.isdisjoint(P41_FALLBACK_ARM_ESCAPE_IDS)

    @pytest.mark.parametrize(
        "case",
        [c for c in P41_ESCAPE_CASES if c.expected_fence == P41_RAW_ARM],
        ids=[c.case_id for c in P41_ESCAPE_CASES if c.expected_fence == P41_RAW_ARM],
    )
    def test_the_raw_arm_carries_the_value_verbatim(self, case: P41EscapeCase) -> None:
        """On the raw arm the body IS the value, so the emitted line is unchanged.

        `_raw` returns ``("r", value)``, so `r"<value>"` is emitted byte for
        byte — which is why the value contract has zero blast radius on the 79
        baseline cases (`design.md` §9 Q5). Asserted over the segment rather
        than the value, because that is the claim: the *spelling* did not move.
        """
        segment = p41_emitted_pattern_segment(case.replace_source, P41_REPLACE_ACCESSOR)
        assert segment == P41_RAW_ARM + '"' + case.intended_value + '"'

    @pytest.mark.parametrize(
        "case",
        [c for c in P41_ESCAPE_CASES if c.expected_fence == P41_FALLBACK_ARM],
        ids=[c.case_id for c in P41_ESCAPE_CASES if c.expected_fence == P41_FALLBACK_ARM],
    )
    def test_the_fallback_arm_is_a_plain_fence_carrying_the_same_regex(self, case: P41EscapeCase) -> None:
        """On the fallback arm the spelling changes and the regex does not.

        Three things, in the order that makes a failure legible: the emitted
        literal is **not** raw; the raw spelling was genuinely unavailable
        rather than merely declined; and the plain fence still resolves to the
        intended value. The last one is the property that makes the fallback
        safe — the regex engine is handed a value and never sees the fence.
        """
        segment = p41_emitted_pattern_segment(case.replace_source, P41_REPLACE_ACCESSOR)
        assert segment[:1] not in P41_RAW_PREFIXES
        assert not p41_raw_fence_can_spell(case.intended_value)
        assert ast.literal_eval(segment) == case.intended_value

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_both_sites_emit_the_same_pattern_for_the_same_literal(self, case: P41EscapeCase) -> None:
        """One literal, two sites, one emitted pattern — spelling included.

        `:845` and `:1232` share `_raw`, so a divergence between them would
        mean one site had grown a rule of its own. Asserted over the segment,
        which catches a fence difference that a value comparison would not.
        """
        assert p41_emitted_pattern_segment(case.replace_source, P41_REPLACE_ACCESSOR) == p41_emitted_pattern_segment(
            case.rlike_source, P41_CONTAINS_ACCESSOR
        )


class TestUnrecognisedEscapesPinTheBoundary:
    """Why D3 needed a DOUBLED backslash, stated as assertions over the table.

    **Validates: Requirements 1.16, 1.18, 1.19, 2.12**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN**, at HEAD and after T3.
    ══════════════════════════════════════════════════════════════════

    Python leaves **unrecognised** escapes alone, so for `\\\\.`, `\\\\d`,
    `\\\\w` and `\\\\s` the literal's value equals its source text and a
    plain-to-raw fence change is harmless. That single fact explains three
    things `bugfix.md` states separately: why the D3 condition needs a doubled
    backslash (1.16), why `_string`'s old docstring generalised wrongly from
    its own example (1.18), and why **0 of 56 measured generated versions hit
    D3** (1.19) — the generator writes this class.
    """

    @pytest.mark.parametrize("case", P41_UNRECOGNISED_CASES, ids=p41_escape_ids(P41_UNRECOGNISED_CASES))
    def test_an_unrecognised_escape_has_source_text_equal_to_its_value(self, case: P41EscapeCase) -> None:
        """The boundary itself: nothing to preserve, because nothing differs."""
        assert case.source_text_body == case.intended_value

    @pytest.mark.parametrize("case", P41_RECOGNISED_CASES, ids=p41_escape_ids(P41_RECOGNISED_CASES))
    def test_a_recognised_escape_has_source_text_different_from_its_value(self, case: P41EscapeCase) -> None:
        """And the other side of it, for both recognised classes.

        Class 2 belongs here as much as class 1 does: its value differs from
        its source text even though `re` reads the two alike, and it is that
        difference which forces the fallback.
        """
        assert case.source_text_body != case.intended_value

    def test_the_doubled_and_single_spellings_now_emit_one_regex(self) -> None:
        """`"[^\\\\\\\\d]"` and `"[^\\\\d]"` converge after the fix — which is why only one was wrong.

        The pair the whole boundary argument rests on, asserted at the
        emission. Both denote a negated digit class to Spark, and after T3 both
        reach pandas as the same five characters. Before T3 the doubled
        spelling reached pandas as six. So the defect was never "escaped
        literals are mangled"; it was "a doubled backslash is mangled", which
        is what keeps D3 defence-in-depth rather than the headline (2.14).
        """
        doubled = next(c for c in P41_ESCAPE_CASES if c.case_id == "escaped-backslash-negated-digit-class")
        single = next(c for c in P41_ESCAPE_CASES if c.case_id == "unrecognised-escape-d")
        doubled_emitted = p41_emitted_pattern_value(doubled.replace_source, P41_REPLACE_ACCESSOR)
        single_emitted = p41_emitted_pattern_value(single.replace_source, P41_REPLACE_ACCESSOR)
        assert doubled_emitted == single_emitted == r"[^\d]"
        assert re.sub(doubled_emitted, "", doubled.subject) == re.sub(single_emitted, "", single.subject) == "1234"
        # And they were spelled differently in the PySpark the model wrote —
        # otherwise the convergence would be trivial.
        assert doubled.literal != single.literal

    def test_an_unrecognised_escape_converts_even_when_warnings_are_errors(self) -> None:
        """`convert`'s `SyntaxWarning` suppression stays (`:1596-1602`).

        Asserted in the direction the task permits: the suppression **holds**,
        so a model writing `[^\\\\d]` still gets a conversion. **No test here
        depends on the warning being raised** — the warning is about the
        model's text, not about the converter's behaviour, and it is also why
        this escape class is invisible in production.
        """
        case = next(c for c in P41_ESCAPE_CASES if c.case_id == "unrecognised-escape-d")
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            assert p41_emitted_pattern_value(case.replace_source, P41_REPLACE_ACCESSOR) == case.intended_value


class TestTheEscapeSweepOracleIntegrity:
    """The sweep's table states Python and regex facts. These check them.

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN.** Not bug-condition cases — count them the
    way `TestOracleIntegrity` is counted in
    `test_converter_equivalence_baseline.py`.
    ══════════════════════════════════════════════════════════════════

    `LGR-28b4d75-5` cuts both ways: the oracle has to be checkable too. A
    wrong `sub_result`, a subject pair both candidate patterns treat alike, or
    a miscategorised escape class would make an assertion above pass or fail
    for a reason that has nothing to do with the converter.
    """

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_intended_value_is_pythons_own_answer(self, case: P41EscapeCase) -> None:
        """What Spark receives is `ast.literal_eval` of the literal the model wrote."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            assert ast.literal_eval(case.literal) == case.intended_value

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_recorded_sub_results_are_regex_facts(self, case: P41EscapeCase) -> None:
        """`re.sub` agrees with the table, for the intent and the source text alike."""
        assert re.sub(case.intended_value, "", case.subject) == case.sub_result
        assert re.sub(case.source_text_body, "", case.subject) == case.source_text_sub_result

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_subject_pair_separates_the_intended_pattern(self, case: P41EscapeCase) -> None:
        """Every case's pair is a real pair: the intent matches one and not the other."""
        assert re.search(case.intended_value, case.matching) is not None
        assert re.search(case.intended_value, case.non_matching) is None

    @pytest.mark.parametrize("case", P41_ESCAPE_CASES, ids=p41_escape_ids(P41_ESCAPE_CASES))
    def test_the_recorded_source_text_match_set_is_a_regex_fact(self, case: P41EscapeCase) -> None:
        """And the source-text candidate answers what the table says it does."""
        observed = (
            re.search(case.source_text_body, case.matching) is not None,
            re.search(case.source_text_body, case.non_matching) is not None,
        )
        assert observed == case.source_text_match_set

    @pytest.mark.parametrize("case", P41_DIVERGENT_CASES, ids=p41_escape_ids(P41_DIVERGENT_CASES))
    def test_class_one_really_diverges(self, case: P41EscapeCase) -> None:
        """Class 1's claim: the two candidate values are DIFFERENT regexes.

        Checked on both fixtures, so the classification cannot rest on one of
        them: the replacement result differs, and so does the match-set pair.
        """
        assert case.source_text_sub_result != case.sub_result
        assert case.source_text_match_set != (True, False)

    @pytest.mark.parametrize(
        "case",
        P41_EQUIVALENT_CASES + P41_UNRECOGNISED_CASES,
        ids=p41_escape_ids(P41_EQUIVALENT_CASES + P41_UNRECOGNISED_CASES),
    )
    def test_classes_two_and_three_really_agree(self, case: P41EscapeCase) -> None:
        """Classes 2 and 3 claim `re` reads both candidates alike. Checked, not assumed.

        This is the finding the section header names: `\\\\n`, `\\\\r`,
        `\\\\t` and `\\\\"` differ between the two Python **fences** and not
        between the two **regexes**, because the regex engine recognises the
        same escapes. Saying "all five recognised escapes change the regex"
        would have been the easy overclaim, and this assertion is what makes
        the honest version checkable.
        """
        assert case.source_text_sub_result == case.sub_result
        assert case.source_text_match_set == (True, False)

    def test_the_three_classes_partition_the_sweep(self) -> None:
        """Every case is in exactly one class, and no class is empty."""
        assert len(P41_DIVERGENT_CASES) + len(P41_EQUIVALENT_CASES) + len(P41_UNRECOGNISED_CASES) == len(
            P41_ESCAPE_CASES
        )
        assert P41_DIVERGENT_CASES and P41_EQUIVALENT_CASES and P41_UNRECOGNISED_CASES
        ids = [case.case_id for case in P41_ESCAPE_CASES]
        assert len(set(ids)) == len(ids), "duplicate case_id would make a parametrised failure ambiguous"

    def test_every_escape_the_task_names_is_covered(self) -> None:
        """`tasks.md` 7.1's required escape list, checked against the table.

        The five that differ between fences must appear among the recognised
        cases, and the four that do not must appear among the unrecognised
        ones. Auditable against the task text rather than trusted.
        """
        recognised_literals = "".join(case.literal for case in P41_RECOGNISED_CASES)
        for escape in P41_REQUIRED_DIFFERING_ESCAPES:
            assert escape in recognised_literals, f"no recognised-escape case covers {escape!r}"
        unrecognised_literals = "".join(case.literal for case in P41_UNRECOGNISED_CASES)
        for escape in P41_REQUIRED_UNRECOGNISED_ESCAPES:
            assert escape in unrecognised_literals, f"no unrecognised-escape case covers {escape!r}"

    def test_the_raw_spellability_oracle_is_the_language(self) -> None:
        """:func:`p41_raw_fence_can_spell` against the shapes whose answer is known.

        Oracle integrity for the arm predicate. Each `False` below is a
        specific `SyntaxError` — the quote closes the fence, LF and CR end the
        line, a NUL cannot appear in source at all, and an odd trailing
        backslash escapes the closing quote — and each `True` is a value a raw
        fence carries unharmed, including the tab that must NOT be excluded and
        the even backslash run that must not be confused with an odd one.
        """
        for unspellable in ('a"b', "a\nb", "a\rb", "a\x00b", "a\\", "\\"):
            assert not p41_raw_fence_can_spell(unspellable), unspellable
        for spellable in ("plain", "a\tb", "a\\\\", r"[^\d]", r"\bcat\b", "\x08"):
            assert p41_raw_fence_can_spell(spellable), spellable

    def test_the_two_pattern_readers_agree(self) -> None:
        """:func:`p41_emitted_pattern_segment` and task 2.1's value reader are one reader.

        The segment walk is duplicated so task 2.1's helper stays byte-unchanged
        (`tasks.md` 7.1: do not modify tasks 2.1/2.3/2.4's classes). This pins
        the two together over every case at both sites, so the duplication
        cannot drift into disagreement — the same guard
        `test_the_two_block_builders_agree` provides for the block builders.
        """
        for case in P41_ESCAPE_CASES:
            for source, accessor in (
                (case.replace_source, P41_REPLACE_ACCESSOR),
                (case.rlike_source, P41_CONTAINS_ACCESSOR),
            ):
                segment = p41_emitted_pattern_segment(source, accessor)
                assert ast.literal_eval(segment) == p41_emitted_pattern_value(source, accessor)

    def test_the_sweep_reaches_both_raw_fence_sites(self) -> None:
        """Site completeness, as one assertion over the blocks the table built.

        `tasks.md` 7.1 requires both sites. A table that had quietly built only
        `regexp_replace` blocks would pass every fidelity assertion above and
        cover half of what was asked.
        """
        for case in P41_ESCAPE_CASES:
            assert case.replace_source == p41_replace_block(case.literal)
            assert "regexp_replace(" in case.replace_source
            assert case.rlike_source == p41_filter_rlike_block(case.literal)
            assert ".rlike(" in case.rlike_source
            assert case.literal in case.replace_source
            assert case.literal in case.rlike_source
