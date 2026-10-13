# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every emitted string literal carries the VALUE of the source literal it came from.

Spec: `.kiro/specs/p41-converter-literal-fidelity/`, task 10.1, `bugfix.md` 2.16,
`design.md` §8 — **Property 6, the class**.

**ORACLE RULE FOR THIS FILE: expectations come from Python's own literal
semantics, never from current converter output.** A literal's value is whatever
:func:`ast.literal_eval` says it is; that is the definition of the string the
model wrote, and it is what Spark would receive. At the one site where the
converter is *supposed* to transform the value — ``to_date``/``to_timestamp``'s
format string — the expectation comes from the SimpleDateFormat→strftime
correspondence, written out by hand in :data:`P41_JAVA_FORMAT_REMAP` with its
derivation. Nothing here reads the converter's output and calls it correct.
`LGR-28b4d75-5` (template / ratified) is the record: a test written from the
producer's output cannot detect the producer drifting, because the output is its
own oracle. The files in this directory that follow the **opposite** rule — where
current output IS the oracle — are `test_string_site_preservation.py` and
`test_string_site_preservation_prop.py`; do not read this file as one of those.

**`LGR-4dd9b42-1`** (template / ratified, supersedes `LGR-5c33c04-1`) is the
record governing this converter's translation rules. p41 does not contradict it;
this file is p41's machine-checkable form of the class it left open.

═══════════════════════════════════════════════════════════════════════
PROPERTY 6 IS A DIRECTION WITH A NAMED REMAINDER. IT IS NOT A CLOSURE.
═══════════════════════════════════════════════════════════════════════

`design.md` §8 says so, and it has to be said *here* rather than only there,
because a file that presents itself as closing the class is worse than no file:
the next reader stops looking. Three specific holes remain, each stated as a
thing **this property cannot see**.

1. **It is a property about literals, not about semantics.** Every check below
   compares a *string value* on one side of the translation with a string value
   on the other. That is blind to a translation whose literals are all perfect
   and whose *meaning* is wrong — and three such divergences are known and
   deliberately out of scope:

   * ``concat_ws``'s hardcoded ``.astype(str)`` on **column** parts (`bugfix.md`
     3.11, p40 design §5 N3a): Spark skips null inputs where pandas splices in
     the string ``"None"``. The separator literal this file checks at that site
     is preserved exactly; the null behaviour beside it is not Spark's.
   * the ``boolean`` / ``BooleanType`` cast's ``astype(bool)``, which maps null →
     ``False`` (`bugfix.md` 3.12, p40 N3b). This file generates ``cast("boolean")``
     and asserts the target selection is spelling-invariant. It says nothing
     about what the emitted cast then does to a null.
   * ``substring``'s 1-based/0-based trap (`bugfix.md` "Out of scope"): Spark's
     ``substring(str, pos, len)`` is 1-based and takes a length, pandas'
     ``.str.slice(start, stop)`` is 0-based and takes a stop. It refuses today, so
     it is not reachable here at all — and if it were ever implemented, this
     property would pass on the off-by-one.

   Those are false statements about a valid block that **no** literal-value
   property reaches. The principle `bugfix.md` records for them — *"expressible"
   is not "safe to express"* — is exactly the part of 2.16 that stays open.

2. **``_is_scalar_emission``'s permissive default stays permissive**
   (`pyspark_to_pandas.py:421`, the default itself at `:455` —
   `design.md` §8 cites `:261`, which was that line BEFORE T3 added the fence
   helpers above it; and `test_scalar_member_emission_prop.py`'s module docstring
   is the long form). An emission form the predicate does not
   recognise is assumed to be a Series and the guard stays out of its way. That
   is deliberate and correct — refusing by default there would refuse
   conversions that work today — but it is an **open class by construction**: a
   future translation rule that emits a new kind of scalar passes the fence. This
   property cannot see it either, and for a sharper reason than "different
   subject": `design.md` §9 Q6 measured that the two properties are blind in
   *complementary* directions. ``col(r"a\\b")`` under a naive D1 fix emits a
   column named with a backspace, and ``series_only_members_on_scalars`` returns
   ``[]`` for it — correctly, because a wrong column name is not a
   scalar-receiver problem. This file is the half that sees that one. Neither
   half sees an unrecognised scalar emission carrying a correct literal.

3. **The module's own argument applies to this spec.** ``_function_call``'s last
   branch (`pyspark_to_pandas.py:1296-1321` — `design.md` §8 cites `:880-902`,
   the same branch before T3) refuses an unrecognised bare name, and
   its stated reason is that *enumerating instances moves the hole rather than
   closing it*. `bugfix.md` 1.14 is that argument's own counterexample: its
   premise is "a bare name called inside a transform block can only have come
   from ``pyspark.sql.functions``", and ``set`` — a Python builtin — refuses
   correctly, but for a **reason** (``not_yet_expressible``, "a defect in this
   system") that blames the converter for a model error. So the refuse-by-default
   that closed the *emission* hole opened a *reason* hole. **p41 closes neither
   side of that**, and this property is on the wrong axis to help: it treats a
   refusal as one of two legal outcomes and never inspects why the refusal was
   given. `bugfix.md` 2.11 puts the reason's owner in
   `test_code/lambda_function.py`, out of scope.

So: five instances (D1, D2a, D2b, D3, D4) are closed, and the class is not.

═══════════════════════════════════════════════════════════════════════
HOW AN EMITTED LITERAL IS MATCHED TO THE SOURCE LITERAL IT DERIVES FROM
═══════════════════════════════════════════════════════════════════════

This is the hard part of the property, because the emitted literal set is **not**
a subset of the source literal set: the converter also authors constants of its
own (``errors="coerce"``, ``.astype("Int64")``), remaps one value on purpose, and
consumes one value without emitting it.

The correspondence is established by **exhaustion of origins**, not by position.
``convert`` is a pure function of the source block, so a string literal in the
emission can only have come from one of three places:

* a string the converter **authors** itself — the closed set
  :data:`P41_AUTHORED_CONSTANTS`, each member cited to the line that writes it;
* the block's **scaffolding** literals, whose values this file computes from the
  template rather than transcribing (:func:`p41_scaffold_values`);
* the **one generated slot** — every template below has exactly one ``{lit}``,
  and its value is guaranteed distinct from every scaffolding value and from
  every authored constant (asserted, not assumed:
  :meth:`TestTheGeneratedDomainIsSound.test_no_generated_value_collides_with_a_scaffold_or_an_authored_constant`).

So an emitted literal that is neither authored nor a scaffolding value has
exactly one possible origin, and the assertion is that it equals that origin's
value. That is :func:`p41_offenders`, and it is the whole of tier 2.

**Why sound.** The origin set is exhaustive because the converter has no other
input: there is no configuration, no environment read and no I/O on the
translation path. The generated slot's value is known independently — it is
``ast.literal_eval`` of the spelling — never read back from the emission.

**Where it would break, stated rather than left to be discovered.**

* **A newly-authored constant** presents as an offender. That is a false positive
  in the strict sense and a real review item in practice: a new string the
  converter writes into every emission should be looked at once and then declared
  in :data:`P41_AUTHORED_CONSTANTS` with its line. The alternative — inferring the
  authored set from the converter's output — would make the property consult its
  subject as its own oracle, which is the failure `LGR-28b4d75-5` names.
* **A permutation of two source literals within one block is invisible to tier
  2.** ``withColumnRenamed(A, B)`` emitting ``{B: A}`` leaves every emitted value
  inside the allowed set, so tier 2 passes. Tier 2 pins *values*, not
  *positions*. That boundary is acceptable here because the defect class p41
  addresses is a **fence change corrupting a value**, which is
  position-invariant, and positional correctness at all 15 sites is pinned
  byte-exactly elsewhere — `test_string_site_preservation.py`'s per-site pins and
  the 79 cases in `test_converter_equivalence_baseline.py`. Naming the division
  of labour is better than implying this file covers both.

**Tier 1 adds the positional claim the value contract actually makes**, cheaply:
*the emission depends only on the literal's VALUE, not on its SPELLING.* For each
generated case the same block is also converted with a **control** spelling — a
plain ``"…"`` literal of the *same* value, built here by :func:`p41_plain_fence`
— and when both convert, the two emissions must agree on their skeleton **and**
on their literal-value sequence, position by position. That is the one-sentence
contract `_string`'s docstring now states, machine-checked.

Tier 1 is **not** an oracle claim and must not be read as one: neither side is
evidence for the other's correctness. Both are subjects; tier 2 supplies the
external oracle for both. What tier 1 adds is that a divergence *between*
spellings cannot hide — which is precisely how D1 and D3 differed from each other
(`bugfix.md` 1.20: D1 refused where it should convert, D3 converted to something
else) and why a property over one spelling could never have seen both.

Comparison is over the **parsed** emitted text — ``tokenize`` for exact spans,
then ``ast.literal_eval`` per token — never over substrings, per `LGR-b46a5bb-4`
(template / ratified): a substring rule forbids documenting the thing it forbids,
and a backslash inside a comment is not a regex.

═══════════════════════════════════════════════════════════════════════
THE THREE LEGITIMATE EXCEPTIONS, HANDLED RATHER THAN TRIPPED OVER
═══════════════════════════════════════════════════════════════════════

* **``to_date``/``to_timestamp``'s format is REMAPPED** (`pyspark_to_pandas.py:1344`).
  ``yyyy-MM-dd`` becomes ``%Y-%m-%d``. That is not a value divergence, it is the
  site's job (`bugfix.md` 3.5 freezes the mapping). Handled by
  :data:`P41_JAVA_FORMAT_REMAP`, whose expected outputs are derived by hand from
  the token correspondence and stated there — so the *fence* around the remapped
  result is still checked, including a format carrying a backslash and one
  carrying a quote.
* **``cast``'s target key SELECTS an emission rather than appearing in one**
  (`pyspark_to_pandas.py:1431`). The value is a dict key into ``_CAST_TARGETS``
  and is never emitted, so at that site the property asserts the key does **not**
  appear and that the selection is spelling-invariant — a raw ``r"int"`` must
  select the same target a plain ``"int"`` does, which is D1 at a site that never
  emits a literal at all. **One key is exempt from the absence half and the
  exemption is a real limit rather than a convenience**: ``cast("string")``
  selects a target whose authored dtype is spelled ``string`` too, so a
  value-level detector cannot separate "the key leaked into the emission" from
  "the dtype was authored". See :data:`P41_UNDECIDABLE_CAST_KEYS`; the exemption
  is declared, not inline, and a guard fails if a second key joins it.
* **``lit`` re-emits SOURCE TEXT through ``_segment``** into the identical fence
  it came from (`pyspark_to_pandas.py:1222`), which is genuinely byte-preserving
  and is the one place the deleted source-text rationale was ever true. It needs
  no exception in tier 2 — text into an identical fence has the same value — but
  it is the reason tier 1 compares *skeleton and values* rather than raw bytes:
  ``lit(r"a\\b")`` and ``lit("a\\\\b")`` emit different bytes and the same value,
  and a byte comparison would have called that a divergence. ``lit`` is also the
  only site here that accepts a single- or triple-quoted spelling, since it never
  reaches ``_string``'s quote fence — and ``lit(0)`` emits a non-string constant,
  which contributes no emitted literal and is therefore silently outside this
  property's subject.

**Validates: Requirements 2.16**
"""

# Feature: p41-converter-literal-fidelity, Property 6: for a generated transform
# block over a generated literal spelling — plain / raw / single-quoted /
# escaped / triple-quoted — `convert` either refuses, or every string literal in
# the emitted pandas has a VALUE equal to the value of the source literal it
# derives from.

import ast
import io
import json
import os
import sys
import tokenize
import warnings
from dataclasses import dataclass

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# Add Lambda source to path (same shape as the rest of this directory).
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

# ──────────────────────────────────────────────────────────────────────
# Literal mechanics, written here rather than imported from production.
#
# `_dq`, `_raw` and `_raw_spellable` are the SUBJECT. Building the generated
# spellings out of them would make the property agree with them by construction.
# The independence is not asserted, it is *verified*: every spelling this file
# constructs is kept only if `ast.literal_eval` returns the intended value, so a
# bug in the constructors below drops cases rather than passing wrong ones —
# and the drop is visible as a floor failure, not as a silent green.
# ──────────────────────────────────────────────────────────────────────

#: A marker that cannot occur in emitted pandas, used to blank literal spans.
#: Asserted absent before substitution rather than assumed.
P41_LITERAL_PLACEHOLDER = "\x00p41-literal-{}\x00"


def p41_plain_fence(value: str) -> str:
    """A plain ``"…"`` Python literal whose value is *value*.

    ``json.dumps`` is the primitive, deliberately **not** :func:`repr` and not
    production's ``_dq``: JSON's string grammar is a subset of Python's for
    ``str`` — it always chooses the double quote, escapes ``\\`` and ``"``, and
    spells control characters as ``\\n``/``\\uXXXX`` — so its output is a valid
    Python double-quoted literal with the same value. Independent of the subject,
    and the round-trip is asserted over the whole domain by
    :meth:`TestTheGeneratedDomainIsSound.test_every_spelling_really_has_the_value_it_claims`.
    """
    return json.dumps(value)


def p41_raw_fence_possible(value: str) -> bool:
    """Whether ``r"<value>"`` is a literal that parses and means *value*.

    Written from the three properties of a single-line raw fence rather than
    from production's ``_raw_spellable``: the fence quote closes it, a line
    terminator ends the line, and a trailing odd run of backslashes escapes the
    closing quote. A NUL cannot appear in Python source at all, and a lone
    surrogate cannot be encoded into the emitted module.

    A wrong answer here cannot produce a wrong assertion — it can only drop a
    spelling, because every constructed spelling is round-trip verified before
    it enters the domain.
    """
    if any(ch in value for ch in ('"', "\n", "\r", "\x00")):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (len(value) - len(value.rstrip("\\"))) % 2 == 0


def p41_spellings(value: str) -> "tuple[tuple[str, str], ...]":
    """Every way this file knows to spell *value*, as ``(kind, source_text)``.

    The five kinds `design.md` §8 names, plus the sixth that matters most to D3:

    * ``plain`` — the body is the value verbatim, so source text == value. The
      preservation class, and where the old source-text contract was accidentally
      correct.
    * ``escaped`` — a double-quoted literal that spells at least one character
      with a backslash escape. **This is D3's shape**: ``"[^\\\\d]"`` means the
      five characters ``[^\\d]``, and re-wrapping its source text in ``r"…"``
      produced a six-character regex that deleted everything.
    * ``unrecognised_escape`` — ``"[^\\d]"``: a single backslash Python leaves
      alone, so text == value anyway. The control that converts correctly even
      before p41 (`bugfix.md` 3.10) and the reason D3 needs a *doubled*
      backslash to manifest.
    * ``raw`` — ``r"…"``. **D1's shape**: 5 of 5 measured production regexes,
      5 of 5 refused before p41.
    * ``single`` — ``'…'``. Refused by the kept quote fence at every ``_string``
      site (Q1), so it is here to exercise a *legal refusal*, and it converts at
      ``lit``, which never reaches that fence.
    * ``triple`` — ``\"\"\"…\"\"\"``. Same, via ``_string``'s explicit ``\"\"\"``
      clause.

    Duplicates are dropped, so a value whose plain and unrecognised-escape
    spellings coincide contributes one case rather than two.
    """
    candidates: list[tuple[str, str]] = []
    body = json.dumps(value)[1:-1]
    candidates.append(("plain" if body == value else "escaped", json.dumps(value)))
    # Only distinct from the above when Python leaves the body's backslashes
    # alone; the round-trip filter below discards it otherwise.
    candidates.append(("unrecognised_escape", '"' + value + '"'))
    if p41_raw_fence_possible(value):
        candidates.append(("raw", 'r"' + value + '"'))
    candidates.append(("single", "'" + body.replace("'", "\\'") + "'"))
    candidates.append(("triple", '"""' + body + '"""'))

    kept: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, text in candidates:
        if text in seen:
            continue
        try:
            with warnings.catch_warnings():
                # An unrecognised escape is a SyntaxWarning on 3.12+. It is a
                # fact about the model's text, not about this file, and
                # `convert` suppresses the same warning for the same reason at
                # `:1795-1799` (`tasks.md` 7.1 cites `:1372-1376`, the same
                # block before T3). Suppressing it here is therefore matching
                # production's own handling, not hiding a finding — and
                # `tasks.md` 7.1 is explicit that no test may depend on the
                # warning being raised.
                warnings.simplefilter("ignore", SyntaxWarning)
                parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            continue
        if parsed != value:
            continue
        seen.add(text)
        kept.append((kind, text))
    return tuple(kept)


def p41_literal_split(code: str) -> "tuple[str, tuple[str, ...]]":
    """*code*'s **skeleton** and the **values** of its string literals, in order.

    The skeleton is *code* byte-for-byte with every string literal replaced by a
    positional placeholder. Tokenizing rather than parsing on purpose:
    ``ast.unparse`` normalises quote style and spacing, and a normalising
    comparison cannot see the skeleton change tier 1 exists to catch.

    Code that does not tokenize raises :class:`AssertionError` naming the
    problem, and that is a feature. `tasks.md` 4.1 records that the hazard a
    substitution-list ``_dq`` creates is an emission that does not *parse* — a
    line break inside a plain fence — so an unparseable emission is a real
    finding about the fence helpers, not a comparison to work around.
    """
    if not code.endswith("\n"):
        code += "\n"
    assert "\x00" not in code, f"placeholder marker occurs in the code under test: {code!r}"

    # Line starts over "\n" ONLY: `str.splitlines` also splits on U+2028/U+2029/
    # U+0085, which `tokenize` does not, so it would misalign every span after
    # such a character.
    line_starts = [0]
    for index, character in enumerate(code):
        if character == "\n":
            line_starts.append(index + 1)

    spans: list[tuple[int, int]] = []
    values: list[str] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            for token in tokenize.generate_tokens(io.StringIO(code).readline):
                if token.type != tokenize.STRING:
                    continue
                start = line_starts[token.start[0] - 1] + token.start[1]
                end = line_starts[token.end[0] - 1] + token.end[1]
                literal = ast.literal_eval(token.string)
                assert isinstance(literal, str), f"non-str literal {token.string!r} in {code!r}"
                spans.append((start, end))
                values.append(literal)
    except (SyntaxError, tokenize.TokenError, IndentationError) as exc:
        raise AssertionError(
            f"emitted or generated pandas does not tokenize: {exc!r}\n  code: {code!r}\n"
            f"An emission that does not parse is a real finding (`tasks.md` 4.1: the hazard a "
            f"substitution-list `_dq` creates is a SyntaxError in the generated pandas, not a wrong "
            f"string), not a comparison to work around."
        ) from exc

    skeleton = code
    for index, (start, end) in reversed(list(enumerate(spans))):
        skeleton = skeleton[:start] + P41_LITERAL_PLACEHOLDER.format(index) + skeleton[end:]
    return skeleton, tuple(values)


def p41_literal_values(code: str) -> "tuple[str, ...]":
    """Just the values, for the checks that do not need the skeleton."""
    return p41_literal_split(code)[1]


# ──────────────────────────────────────────────────────────────────────
# The converter's own string vocabulary.
#
# The only emitted literals that derive from NO source literal. Each is cited to
# the line that writes it, so a reader can check the list against the module
# instead of trusting it. A member appearing in an emission that is not declared
# here is reported as an offender — see the module docstring on why inferring
# this set from output was rejected.
# ──────────────────────────────────────────────────────────────────────

P41_AUTHORED_CONSTANTS = frozenset(
    {
        # `pd.to_datetime(..., errors="coerce")` — `_to_datetime`, `:1345`/`:1347`;
        # and `pd.to_numeric(..., errors="coerce")` — `_cast`, `:1475`.
        "coerce",
        # `_CAST_STRING_NULLABLE` — `.astype("string")` at `:630`, and
        # `_string_cast_broadcast`'s `dtype="string"` at `:1010`.
        "string",
        # `_CAST_NUMERIC_INT` — `.astype("Int64")` at `:609`.
        "Int64",
        # `FloatType` — `.astype("float32")` at `:646`.
        "float32",
    }
)


# ──────────────────────────────────────────────────────────────────────
# The generated space: one slot per template, all 15 `_string` sites plus `lit`.
#
# `bugfix.md` 3.1's table is the source of the site list, and
# `test_site_coverage_is_total_over_the_string_fan_in` below checks this table
# against it rather than leaving the correspondence to a reading.
#
# EXACTLY ONE `{lit}` per template is the load-bearing constraint: it is what
# makes "the source literal it derives from" a single, known string, so the
# exhaustion-of-origins argument in the module docstring closes.
# ──────────────────────────────────────────────────────────────────────

#: The generated slot's value is *preserved* into the emission: the emitted
#: literal must have exactly that value, and it must appear.
P41_PRESERVING = "preserving"
#: The value is *transformed* on purpose (the strftime map). The expectation is
#: :data:`P41_JAVA_FORMAT_REMAP`'s hand-derived output, which must appear; the
#: input value must NOT.
P41_REMAPPED = "remapped"
#: The value *selects* an emission rather than appearing in one (the cast key).
#: Nothing derived appears, and the input value must not leak in.
P41_SELECTS = "selects"


@dataclass(frozen=True)
class P41Site:
    """One `_string` (or `_segment`) call site, reached by one generated slot."""

    site: str
    what: str
    template: str
    kind: str


P41_SITES: "tuple[P41Site, ...]" = (
    # ── the eight `_column_name` sites (`:1149`, one wrap point) ──────
    P41Site(":1218", "col column name", 'df_transformed = df.withColumn("out_col", col({lit}))', P41_PRESERVING),
    P41Site(":1620", "withColumn column", 'df_transformed = df.withColumn({lit}, col("src_col"))', P41_PRESERVING),
    P41Site(
        ":1642", "withColumnRenamed old", 'df_transformed = df.withColumnRenamed({lit}, "new_col")', P41_PRESERVING
    ),
    P41Site(
        ":1643", "withColumnRenamed new", 'df_transformed = df.withColumnRenamed("src_col", {lit})', P41_PRESERVING
    ),
    P41Site(":1648", "drop column", "df_transformed = df.drop({lit})", P41_PRESERVING),
    P41Site(":1668", "filter's col name", "df_transformed = df.filter(col({lit}).isNotNull())", P41_PRESERVING),
    P41Site(":1707", "select all-string form", "df_transformed = df.select({lit})", P41_PRESERVING),
    P41Site(":1713", "select string argument", 'df_transformed = df.select({lit}, col("src_col"))', P41_PRESERVING),
    # ── the four direct plain-fence sites ─────────────────────────────
    P41Site(
        ":1263",
        "regexp_replace replacement",
        'df_transformed = df.withColumn("out_col", regexp_replace(col("src_col"), "pat_txt", {lit}))',
        P41_PRESERVING,
    ),
    P41Site(
        ":1272",
        "concat_ws separator",
        'df_transformed = df.withColumn("out_col", concat_ws({lit}, col("src_col"), col("other_col")))',
        P41_PRESERVING,
    ),
    P41Site(
        ":1344",
        "_to_datetime format",
        'df_transformed = df.withColumn("out_col", to_date(col("src_col"), {lit}))',
        P41_REMAPPED,
    ),
    P41Site(
        ":1401",
        "alias new name",
        'df_transformed = df.withColumn("out_col", trim(col("src_col")).alias({lit}))',
        P41_PRESERVING,
    ),
    # ── the two raw-fence sites ───────────────────────────────────────
    P41Site(
        ":1262",
        "regexp_replace pattern",
        'df_transformed = df.withColumn("out_col", regexp_replace(col("src_col"), {lit}, "rep_txt"))',
        P41_PRESERVING,
    ),
    P41Site(":1691", "rlike pattern", 'df_transformed = df.filter(col("src_col").rlike({lit}))', P41_PRESERVING),
    # ── the no-fence site ─────────────────────────────────────────────
    P41Site(
        ":1431",
        "_cast string target key",
        'df_transformed = df.withColumn("out_col", col("src_col").cast({lit}))',
        P41_SELECTS,
    ),
    # ── not a `_string` site at all, and here because of that ─────────
    P41Site(":1222", "lit source text", 'df_transformed = df.withColumn("out_col", lit({lit}))', P41_PRESERVING),
)

#: `bugfix.md` 3.1's fifteen `_string` call sites, by working-tree line. Kept
#: separate from :data:`P41_SITES` so the coverage check is a comparison of two
#: independently written lists rather than a restatement of one.
P41_STRING_FAN_IN = (
    ":1218",
    ":1262",
    ":1263",
    ":1272",
    ":1344",
    ":1401",
    ":1431",
    ":1620",
    ":1642",
    ":1643",
    ":1648",
    ":1668",
    ":1691",
    ":1707",
    ":1713",
)

TAB = "\t"
LF = "\n"

#: The generated values for every site except the two whose value has a
#: *meaning* to the converter. Each is here for a stated reason, and every one is
#: chosen so that no two collide and none equals a scaffold or an authored
#: constant (asserted below).
P41_VALUES: "tuple[tuple[str, str], ...]" = (
    ("phone_number", "a plain \\w name — the preservation class, and 8 of the 15 sites take one"),
    ("[^0-9]", "the measured regex that converts byte-identically today (`bugfix.md` 3.10)"),
    ("[^\\d]", "D1's measured shape: 5 of 5 production regexes, backslash left alone by Python"),
    ("@x\\.com$", "D3's rlike shape (`bugfix.md` 1.17) — inverted match set when the fence changed"),
    ("a\\b", "D5's shape: a plain fence resolves `\\b` to a BACKSPACE, so text != value"),
    (f"a{TAB}b", "a TAB is legal unescaped in BOTH fences, so it must not force the fallback"),
    (f"a{LF}b", "an LF is fatal in both fences — forces `_raw`'s plain-fence fallback"),
    ('a"b', "the fence quote itself — forces the fallback and exercises `_dq`'s escaping"),
    ("a\\", "a trailing ODD backslash run — the second thing a raw fence cannot spell"),
    ("a\\\\b", "an EVEN backslash run, which a raw fence CAN spell — the fallback's boundary"),
    ("café", "non-ASCII, which `_dq` deliberately leaves as itself rather than escaping"),
    ("", "the empty string — the shortest literal either fence has to carry"),
)

#: ``cast``'s key must be a ``_CAST_TARGETS`` member or the site refuses, so this
#: site gets its own domain. The point at this site is D1 applied to a *lookup*:
#: a raw ``r"int"`` must select what a plain ``"int"`` selects.
P41_CAST_KEY_VALUES = ("int", "string", "boolean", "double", "float")

#: Cast keys whose VALUE is indistinguishable from an authored constant, so the
#: "the key is never emitted" half of the SELECTS check is **not decidable** from
#: the emitted literal values.
#:
#: ``cast("string")`` selects ``_CAST_STRING_NULLABLE`` (`:630`), whose emission
#: is ``.astype("string")`` — the authored *dtype*, which happens to be spelled
#: the same word as the key. So the value ``string`` legitimately appears in that
#: emission and a value-level detector cannot tell "the key leaked" from "the
#: dtype was authored". Found by this file's own red on its first run, and
#: recorded here rather than dropped: it is a real limit on this property's
#: reach, of exactly the kind `design.md` §8 asks to be stated instead of
#: implied. The spelling-invariance half of the check (tier 1) still covers the
#: key at this value, and byte-level coverage of the cast emission lives in the
#: eight `P40_UNPINNED_CAST_KEY_PINS`
#: (`test_converter_equivalence_baseline.py:1185`).
#:
#: Declared as a set rather than special-cased inline so it cannot grow silently:
#: :meth:`TestTheGeneratedDomainIsSound.test_only_the_declared_cast_keys_collide_with_an_authored_constant`
#: fails if another key starts colliding.
P41_UNDECIDABLE_CAST_KEYS = frozenset({"string"})

#: ``to_date``'s format is remapped, so the expectation is the remap — derived
#: here by hand from the SimpleDateFormat→strftime token correspondence
#: (``yyyy``→``%Y``, ``MM``→``%m``, ``dd``→``%d``, ``HH``→``%H``, ``mm``→``%M``,
#: ``ss``→``%S``; a run of letters with no token maps to itself; a non-letter
#: passes through as a literal). `bugfix.md` 3.5 freezes the mapping, so a
#: disagreement here is a finding about the mapping in either direction.
#:
#: The last two entries are the ones that matter to p41: they carry a backslash
#: and a quote THROUGH the remap and into the plain fence, so the fence is still
#: under test at a site whose value legitimately changes.
P41_JAVA_FORMAT_REMAP: "tuple[tuple[str, str], ...]" = (
    ("yyyy-MM-dd", "%Y-%m-%d"),
    ("dd/MM/yyyy", "%d/%m/%Y"),
    ("yyyy-MM-dd HH:mm:ss", "%Y-%m-%d %H:%M:%S"),
    ("yyyy\\MM", "%Y\\%m"),
    ('yyyy"MM', '%Y"%m'),
)


def p41_site_values(site: P41Site) -> "tuple[str, ...]":
    """The values generated into *site*'s slot."""
    if site.kind == P41_SELECTS:
        return P41_CAST_KEY_VALUES
    if site.kind == P41_REMAPPED:
        return tuple(java for java, _strftime in P41_JAVA_FORMAT_REMAP)
    return tuple(value for value, _why in P41_VALUES)


def p41_expected_derived(site: P41Site, value: str) -> "str | None":
    """The value the emission must carry for *value* at *site*, or ``None``.

    ``None`` means "nothing derived from this slot reaches the emission", which
    is true of exactly one site — ``_cast``'s key, a dict lookup rather than an
    emission.
    """
    if site.kind == P41_SELECTS:
        return None
    if site.kind == P41_REMAPPED:
        return dict(P41_JAVA_FORMAT_REMAP)[value]
    return value


def p41_scaffold_values(site: P41Site) -> "tuple[str, ...]":
    """The template's OWN literal values, computed rather than transcribed.

    A sentinel goes into the slot and is removed afterwards, so this cannot
    drift out of step with the template above it.
    """
    sentinel = "\uffff-p41-slot-\uffff"
    filled = site.template.format(lit=json.dumps(sentinel))
    return tuple(value for value in p41_literal_values(filled) if value != sentinel)


def p41_allowed_values(site: P41Site, value: str) -> "frozenset[str]":
    """Every value an emission for this case may legitimately contain."""
    allowed = set(p41_scaffold_values(site)) | set(P41_AUTHORED_CONSTANTS)
    derived = p41_expected_derived(site, value)
    if derived is not None:
        allowed.add(derived)
    return frozenset(allowed)


def p41_offenders(emitted: str, allowed: "frozenset[str]") -> "list[str]":
    """Emitted literal values with no legitimate origin — tier 2's detector.

    A non-empty result is the class p41 exists to remove: a conversion that
    *succeeded* and put a string into the emitted pandas whose value is neither
    the converter's own nor any source literal's.
    """
    return [value for value in p41_literal_values(emitted) if value not in allowed]


# ──────────────────────────────────────────────────────────────────────
# The grid and the deterministic sweep.
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class P41Case:
    site: P41Site
    value: str
    spelling: str
    source_text: str

    @property
    def statement(self) -> str:
        return self.site.template.format(lit=self.source_text)

    @property
    def control(self) -> str:
        """The same block with a plain ``"…"`` spelling of the same value."""
        return self.site.template.format(lit=p41_plain_fence(self.value))


P41_ALL_CASES: "tuple[P41Case, ...]" = tuple(
    P41Case(site=site, value=value, spelling=kind, source_text=text)
    for site in P41_SITES
    for value in p41_site_values(site)
    for kind, text in p41_spellings(value)
)

P41_SPELLING_KINDS = ("plain", "escaped", "unrecognised_escape", "raw", "single", "triple")


def p41_outcome(statement: str) -> "tuple[str, str]":
    """``("refused", <construct>)`` or ``("converted", <emitted code>)``."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return "converted", PySpark2PandasConverter.convert(statement)["code"]
    except ConversionError as exc:
        return "refused", exc.construct


def p41_sweep() -> dict:
    """One deterministic pass over the WHOLE grid.

    Runs the same three checks the property runs, over all 726 cases rather than
    over a 250-example sample, and counts what converted so the vacuity floors
    have something to stand on. Kept as a function rather than inlined so the
    same numbers can be reported outside pytest without re-deriving the grid, and
    so every guard below reads one sweep rather than several different samples.
    """
    converted = refused = 0
    syntax_refusals: list[str] = []
    offenders: list[tuple[str, str, str]] = []
    missing: list[tuple[str, str]] = []
    leaked: list[tuple[str, str]] = []
    divergences: list[tuple[str, str, str]] = []
    control_refused: list[tuple[str, str]] = []
    per_site: dict[str, int] = {site.site: 0 for site in P41_SITES}
    per_spelling: dict[str, int] = dict.fromkeys(P41_SPELLING_KINDS, 0)

    for case in P41_ALL_CASES:
        state, payload = p41_outcome(case.statement)
        if state == "refused":
            refused += 1
            if payload == "python_syntax":
                syntax_refusals.append(case.statement)
            continue
        converted += 1
        per_site[case.site.site] += 1
        per_spelling[case.spelling] += 1

        emitted_values = p41_literal_values(payload)
        offenders.extend(
            (case.site.site, case.statement, offender)
            for offender in p41_offenders(payload, p41_allowed_values(case.site, case.value))
        )
        derived = p41_expected_derived(case.site, case.value)
        if derived is None:
            if case.value not in P41_UNDECIDABLE_CAST_KEYS and case.value in emitted_values:
                leaked.append((case.site.site, case.statement))
        elif derived not in emitted_values:
            missing.append((case.site.site, case.statement))

        control_state, control_payload = p41_outcome(case.control)
        if control_state != "converted":
            control_refused.append((case.site.site, case.statement))
        elif p41_literal_split(payload) != p41_literal_split(control_payload):
            divergences.append((case.site.site, case.statement, case.control))

    return {
        "cases": len(P41_ALL_CASES),
        "converted": converted,
        "refused": refused,
        "syntax_refusals": syntax_refusals,
        "offenders": offenders,
        "missing": missing,
        "leaked": leaked,
        "divergences": divergences,
        "control_refused": control_refused,
        "per_site": per_site,
        "per_spelling": per_spelling,
    }


# ──────────────────────────────────────────────────────────────────────
# NON-VACUITY GUARDS.
#
# `design.md` §9 Q6 records a templated sweep over this converter in which ALL
# 376 generated statements refused, because the substitution did not match the
# placeholder — so its zero offenders proved nothing while staying green. That
# has already happened once in this spec, which is why the floors below are
# mandatory and why they fail NAMING VACUITY rather than naming a count.
#
# The global floor alone is not enough: it can be met while one site, or one
# spelling kind, contributes nothing. So the floor is three floors.
# ──────────────────────────────────────────────────────────────────────

#: Measured over the whole grid on the fixed converter: **726 cases, 394
#: converted, 332 refused**, 0 offenders, 0 dropped literals. Every refusal is
#: either the kept quote fence at a `_string` site (Q1), a cast key outside
#: ``_CAST_TARGETS``, or a `select` output name that will not resolve — no
#: refusal in the grid is about a literal's value.
#:
#: The floor sits below the measurement so a legitimate new fence does not fail
#: it, and far above the vacuity it exists to catch. **It is not a floor to
#: lower**: a drop below it means the generated blocks stopped converting, which
#: is the finding, not the obstacle.
P41_CONVERT_FLOOR = 300
#: No site may go silently quiet. Measured: 25 converting cases at each of the
#: twelve value-carrying `_string` sites, 49 at `lit`, 10 at the cast key and 10
#: at the remapped format (those two draw from smaller, site-specific domains).
P41_PER_SITE_CONVERT_FLOOR = 3


class TestTheSweepIsNotVacuous:
    """**Validates: Requirements 2.16**"""

    def test_the_grid_is_generative_in_size(self):
        assert len(P41_ALL_CASES) >= 100, (
            f"VACUOUS: only {len(P41_ALL_CASES)} generated cases; the grid stopped being generative"
        )

    def test_enough_generated_cases_actually_convert(self):
        """The mandatory convert floor. A sweep where everything refused FAILS.

        `design.md` §9 Q6's 376-refusal sweep is the reason this test exists: a
        property whose every case refuses satisfies the disjunction trivially
        and inspects no emission at all, so it is indistinguishable from a file
        that tests nothing.
        """
        report = p41_sweep()
        assert report["converted"] >= P41_CONVERT_FLOOR, (
            f"VACUOUS SWEEP: only {report['converted']} of {report['cases']} generated cases converted "
            f"(floor {P41_CONVERT_FLOOR}, measured 394 of 726). Every refusal satisfies Property 6's "
            f"disjunction without inspecting a single emitted literal, so a sweep this thin proves "
            f"nothing and this file would be indistinguishable from one that tests nothing. This is "
            f"NOT a floor to lower — find out why the generated blocks stopped converting. "
            f"`design.md` §9 Q6 records the sweep that failed exactly this way and stayed green."
        )

    def test_every_site_contributes_converting_cases(self):
        """A per-site floor, because the global floor can hide a quiet site."""
        report = p41_sweep()
        quiet = {site: count for site, count in report["per_site"].items() if count < P41_PER_SITE_CONVERT_FLOOR}
        assert quiet == {}, (
            f"VACUOUS AT A SITE: {quiet} converted fewer than {P41_PER_SITE_CONVERT_FLOOR} cases. "
            f"The global floor can be met while one site emits nothing, and a site that never "
            f"converts is a site this property does not cover — which is how a fix applied at only "
            f"the regex sites would slip through (`design.md` §9 Q5's rejected arm)."
        )

    def test_every_spelling_kind_contributes_a_converting_case(self):
        """Including `single` and `triple`, which convert only through `lit`.

        If `raw` stopped converting anywhere, this file would say nothing about
        D1 — the whole measured production failure — while staying green.
        """
        report = p41_sweep()
        quiet = sorted(kind for kind, count in report["per_spelling"].items() if count == 0)
        assert quiet == [], (
            f"VACUOUS FOR A SPELLING: {quiet} never converted anywhere in the grid. Each spelling "
            f"kind carries a specific defect (`raw` is D1, `escaped` is D3, `single`/`triple` are "
            f"the kept quote fence reaching `lit`), so a kind that never converts removes that "
            f"defect from this property's reach."
        )

    def test_no_generated_statement_refuses_for_a_syntax_reason(self):
        """Every rendered block is valid Python, and none refuses as one.

        A `SyntaxError` in a generated block refuses — the property's legal
        outcome — for a reason that has nothing to do with a literal's value.
        That is the other way a generative test goes quietly vacuous.
        """
        for case in P41_ALL_CASES:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    ast.parse(case.statement)
            except SyntaxError as exc:  # pragma: no cover - a template bug, not a converter finding
                pytest.fail(f"{case.site.site} {case.spelling}: generated block is not valid Python ({exc})")
        report = p41_sweep()
        assert report["syntax_refusals"] == [], f"cases refused as python_syntax: {report['syntax_refusals']}"

    def test_site_coverage_is_total_over_the_string_fan_in(self):
        """All 15 `_string` sites are reached, and `lit` besides.

        Two independently written lists compared, so this is a check rather than
        a restatement: :data:`P41_STRING_FAN_IN` is `bugfix.md` 3.1's table,
        :data:`P41_SITES` is this file's templates.
        """
        covered = {site.site for site in P41_SITES}
        missing = set(P41_STRING_FAN_IN) - covered
        assert missing == set(), f"`_string` call sites with no generated case: {sorted(missing)}"
        assert ":1222" in covered, "`lit`'s `_segment` site is part of this property's subject"
        assert len(P41_SITES) == len(P41_STRING_FAN_IN) + 1, (
            "one template per `_string` site plus `lit`; a site with two templates breaks the "
            "one-slot-per-block constraint the correspondence argument rests on"
        )

    def test_every_template_has_exactly_one_generated_slot(self):
        """The constraint the whole correspondence argument rests on.

        Two slots in one block and "the source literal it derives from" stops
        being a single known string, so the exhaustion-of-origins argument in
        the module docstring no longer closes.
        """
        for site in P41_SITES:
            assert site.template.count("{lit}") == 1, f"{site.site} ({site.what}) does not have exactly one slot"


class TestTheWholeGridIsClean:
    """The **enumerated complement** of the property, not a second property.

    The property samples 250 examples; this runs the identical three checks over
    all 726 deterministically, which is what makes a failure reproducible without
    a Hypothesis seed. It exists for the same reason `design.md` §13 wants both
    the hand-written per-site pins and the generative property: the enumeration
    makes a failure legible, the generation makes it findable.

    **Validates: Requirements 2.16**
    """

    def test_no_emitted_literal_in_the_grid_has_an_impossible_origin(self):
        """Tier 2, over the whole grid. Measured: 0 offenders in 394 conversions."""
        report = p41_sweep()
        assert report["offenders"] == [], (
            f"emitted literals with no legitimate origin: {report['offenders']!r}\n"
            f"This is the class D3 and D5 belong to — a conversion that SUCCEEDED and changed what a "
            f"string means. Expectations come from Python's literal semantics, never from converter "
            f"output (`LGR-28b4d75-5`)."
        )

    def test_no_site_in_the_grid_drops_or_leaks_its_literal(self):
        """The appearance direction, over the whole grid."""
        report = p41_sweep()
        assert report["missing"] == [], f"a site converted without emitting the value it derives: {report['missing']!r}"
        assert report["leaked"] == [], (
            f"`_cast`'s target key reached the emitted text: {report['leaked']!r} — `:1431` is a dict "
            f"lookup, not an emission, so a wrap was added where `tasks.md` 4.3 says none belongs."
        )

    def test_no_emission_in_the_grid_depends_on_a_literals_spelling(self):
        """Tier 1, over the whole grid. Measured: 0 divergences, 0 asymmetric refusals."""
        report = p41_sweep()
        assert report["divergences"] == [], (
            f"two spellings of one value produced different emissions: {report['divergences']!r}\n"
            f"`_string` returns the value and each site escapes it for its own fence, so the emission "
            f"is a function of the value alone."
        )
        assert report["control_refused"] == [], (
            f"the plain-fence spelling of the same value refuses where another spelling converts: "
            f"{report['control_refused']!r} — the kept quote fence can only refuse MORE spellings "
            f"than the plain one, never fewer."
        )


class TestTheGeneratedDomainIsSound:
    """The preconditions the correspondence argument needs, asserted not assumed.

    **Validates: Requirements 2.16**
    """

    def test_every_spelling_really_has_the_value_it_claims(self):
        """Python's literal semantics are the oracle, so this is the oracle check.

        If a spelling constructor were wrong, every downstream assertion would
        compare against the wrong expected value. Checked over the whole grid
        rather than sampled.
        """
        for case in P41_ALL_CASES:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                parsed = ast.literal_eval(case.source_text)
            assert parsed == case.value, (
                f"{case.spelling} spelling {case.source_text!r} has value {parsed!r}, not {case.value!r}"
            )

    def test_the_control_spelling_is_a_plain_double_quoted_literal_of_the_same_value(self):
        """Tier 1's control has to be a *plain* fence, or it proves nothing."""
        for case in P41_ALL_CASES:
            control_literal = p41_plain_fence(case.value)
            assert control_literal.startswith('"') and control_literal.endswith('"')
            assert not control_literal.startswith('"""')
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                assert ast.literal_eval(control_literal) == case.value

    def test_no_generated_value_collides_with_a_scaffold_or_an_authored_constant(self):
        """The uniqueness that makes an emitted literal's origin unambiguous.

        If a generated value equalled a scaffolding value or an authored
        constant, tier 2 would allow a corrupted emission at that position
        without noticing — the check would go vacuous for that one literal while
        the sweep stayed green.
        """
        for site in P41_SITES:
            scaffold = set(p41_scaffold_values(site))
            for value in p41_site_values(site):
                assert value not in scaffold, f"{site.site}: generated value {value!r} collides with a scaffold literal"
                if site.kind == P41_SELECTS:
                    # `cast("string")` legitimately selects an emission whose
                    # authored constant IS "string"; the site asserts absence of
                    # a derived value rather than presence, so no collision
                    # hazard exists here. Stated so the exemption is visible.
                    continue
                assert value not in P41_AUTHORED_CONSTANTS, (
                    f"{site.site}: generated value {value!r} collides with an authored constant"
                )

    def test_only_the_declared_cast_keys_collide_with_an_authored_constant(self):
        """The SELECTS site's undecidable-key exemption cannot grow silently.

        A cast key whose value equals an authored constant makes "the key is
        never emitted" undecidable from emitted values alone. One key does
        (``string``, whose target's dtype is the same word); a second one
        appearing means the exemption has widened and needs the same written
        reason rather than an inherited one.
        """
        colliding = {key for key in P41_CAST_KEY_VALUES if key in P41_AUTHORED_CONSTANTS}
        assert colliding == set(P41_UNDECIDABLE_CAST_KEYS), (
            f"cast keys colliding with an authored constant are {sorted(colliding)}, but "
            f"P41_UNDECIDABLE_CAST_KEYS declares {sorted(P41_UNDECIDABLE_CAST_KEYS)}. The absence "
            f"half of the SELECTS check is exempted only for declared keys, each with its reason."
        )

    def test_every_generated_value_carries_a_stated_reason(self):
        """The domain is chosen, not accumulated."""
        for value, why in P41_VALUES:
            assert why.strip(), f"value {value!r} has no stated reason for being in the domain"
        assert len({value for value, _ in P41_VALUES}) == len(P41_VALUES), "duplicate value in the domain"

    def test_the_remap_table_is_a_transformation_at_every_entry(self):
        """A fixed point would silently turn the remapped site into a plain one."""
        for java, strftime in P41_JAVA_FORMAT_REMAP:
            assert java != strftime, f"remap entry {java!r} is a fixed point, so it tests no transformation"
        assert any("\\" in java for java, _ in P41_JAVA_FORMAT_REMAP), (
            "no remapped format carries a backslash, so the fence around the remapped result is untested"
        )
        assert any('"' in java for java, _ in P41_JAVA_FORMAT_REMAP), (
            "no remapped format carries a quote, so `_dq`'s escaping of the remapped result is untested"
        )


# ──────────────────────────────────────────────────────────────────────
# THE DETECTOR CAN FIRE.
#
# A property whose detector cannot fail is worth nothing. These pin the recorded
# pre-fix emissions — the counterexamples tasks 2.5 and `design.md` §9 Q5
# observed against the unfixed converter and against a simulated Q5-shaped fix —
# and the correct emissions beside them, so both polarities are demonstrated
# rather than asserted.
# ──────────────────────────────────────────────────────────────────────

#: ``(source block, emitted pandas, the offending value)``. Each emission is
#: quoted from the recorded reproduction, not reconstructed.
P41_PRE_FIX_EMISSIONS = (
    # D3 at `regexp_replace`, verbatim from task 2.5's record
    # (`observations.md`, case `nonraw-escaped-negated-digit-class`): source text
    # `[^\\d]` re-wrapped into an `r"…"` fence became a SIX-character regex that
    # deletes everything instead of the non-digits.
    (
        'df_transformed = df.withColumn("phone", regexp_replace(col("phone_number"), "[^\\\\d]", ""))',
        'df_transformed = df.copy(); df_transformed["phone"] = '
        'df["phone_number"].str.replace(r"[^\\\\d]", "", regex=True)',
        "[^\\\\d]",
    ),
    # D3 at `rlike` (`bugfix.md` 1.17): the divergence is a changed MATCH SET
    # rather than a changed replacement — the emitted pattern matches a literal
    # backslash and no longer matches the address it was written for.
    (
        'df_transformed = df.filter(col("email").rlike("@example\\\\.com$"))',
        'df_transformed = df[df["email"].str.contains(r"@example\\\\.com$", na=False)]',
        "@example\\\\.com$",
    ),
    # D5, reproduced against a simulated Q5-shaped fix (`design.md` §9 Q5): a raw
    # literal accepted but returned as SOURCE TEXT reaches a plain fence, where
    # `\b` resolves to a BACKSPACE. A column named with a control character, from
    # a conversion that succeeded.
    (
        'df_transformed = df.withColumn("out_col", col(r"a\\b"))',
        'df_transformed = df.copy(); df_transformed["out_col"] = df["a\\b"]',
        "a\x08",
    ),
)

#: Emissions that are correct and must NOT be reported. The last three are the
#: three legitimate exceptions from the module docstring, so a careless detector
#: that tripped on any of them would fail here rather than in the property.
P41_LEGAL_EMISSIONS = (
    # D1 fixed: a raw pattern converts and the raw fence carries the same value.
    (
        'df_transformed = df.withColumn("phone", regexp_replace(col("phone_number"), r"[^\\d]", ""))',
        'df_transformed = df.copy(); df_transformed["phone"] = '
        'df["phone_number"].str.replace(r"[^\\d]", "", regex=True)',
    ),
    # `_raw`'s plain-fence fallback: a different SPELLING of the same regex value.
    (
        'df_transformed = df.filter(col("src_col").rlike("a\\"b"))',
        'df_transformed = df[df["src_col"].str.contains("a\\"b", na=False)]',
    ),
    # Exception 1 — the strftime remap.
    (
        'df_transformed = df.withColumn("out_col", to_date(col("src_col"), "yyyy-MM-dd"))',
        'df_transformed = df.copy(); df_transformed["out_col"] = '
        'pd.to_datetime(df["src_col"], format="%Y-%m-%d", errors="coerce").dt.date',
    ),
    # Exception 2 — the cast key selects rather than appears.
    (
        'df_transformed = df.withColumn("out_col", col("src_col").cast("int"))',
        'df_transformed = df.copy(); df_transformed["out_col"] = '
        'pd.to_numeric(df["src_col"], errors="coerce").astype("Int64")',
    ),
    # Exception 3 — `lit` re-emits source text into an identical fence.
    (
        'df_transformed = df.withColumn("out_col", lit(r"a\\b"))',
        'df_transformed = df.copy(); df_transformed["out_col"] = r"a\\b"',
    ),
)


def p41_allowed_from_source(source: str, extra: "tuple[str, ...]" = ()) -> "frozenset[str]":
    """Tier 2's allowed set for a hand-written pair, built from the source block.

    The same shape :func:`p41_allowed_values` builds for a generated case, but
    read off a literal source block, so the guards below exercise the detector
    the property uses rather than a second copy of it.
    """
    return frozenset(set(p41_literal_values(source)) | set(P41_AUTHORED_CONSTANTS) | set(extra))


class TestTheDetectorCanFire:
    """**Validates: Requirements 2.16**"""

    @pytest.mark.parametrize(("source", "emitted", "offender"), P41_PRE_FIX_EMISSIONS)
    def test_it_reports_the_recorded_pre_fix_emissions(self, source, emitted, offender):
        """The counterexamples the unfixed converter (and the rejected arm) produced.

        If any of these stopped being reported, this property would have stayed
        green through the whole of the defect it exists to prevent recurring.
        """
        assert p41_offenders(emitted, p41_allowed_from_source(source)) == [offender]

    @pytest.mark.parametrize(("source", "emitted"), P41_LEGAL_EMISSIONS)
    def test_it_stays_quiet_on_the_emissions_that_are_correct(self, source, emitted):
        """Including all three legitimate exceptions."""
        extra: tuple[str, ...] = ()
        if "to_datetime" in emitted:
            extra = tuple(strftime for _java, strftime in P41_JAVA_FORMAT_REMAP)
        assert p41_offenders(emitted, p41_allowed_from_source(source, extra)) == []

    def test_the_skeleton_half_of_tier_one_can_fire(self):
        """A moved emission with identical literal values is still a divergence.

        Tier 1 compares skeletons as well as values precisely so a change
        outside a literal — a dropped `.copy()`, a `df["x"]` that became
        `df.loc[:, "x"]` — is not invisible.
        """
        one = 'df_transformed = df.copy(); df_transformed["a"] = df["b"]'
        two = 'df_transformed = df.copy(); df_transformed["a"] = df.loc[:, "b"]'
        assert p41_literal_values(one) == p41_literal_values(two)
        assert p41_literal_split(one) != p41_literal_split(two)

    def test_the_value_half_of_tier_one_can_fire(self):
        """And a changed value with an identical skeleton is one too."""
        one = 'df_transformed = df.copy(); df_transformed["a"] = df["b"]'
        two = 'df_transformed = df.copy(); df_transformed["a"] = df["c"]'
        assert p41_literal_split(one)[0] == p41_literal_split(two)[0]
        assert p41_literal_split(one) != p41_literal_split(two)


# ──────────────────────────────────────────────────────────────────────
# PROPERTY 6.
# ──────────────────────────────────────────────────────────────────────

#: Drawn as a product — site, then one of that site's values, then a spelling of
#: that value — rather than as an index into the grid, so a counterexample
#: shrinks to a site and a literal a reader can act on.
p41_cases = (
    st.sampled_from(P41_SITES)
    .flatmap(
        lambda site: st.tuples(
            st.just(site),
            st.sampled_from(p41_site_values(site)),
        )
    )
    .flatmap(
        lambda pair: st.tuples(
            st.just(pair[0]),
            st.just(pair[1]),
            st.sampled_from(p41_spellings(pair[1])),
        )
    )
)

P41_SETTINGS = settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@P41_SETTINGS
@given(case=p41_cases)
def test_convert_refuses_or_preserves_every_literals_value(case):
    """Refuse, or carry every source literal's VALUE into the emission (2.16).

    The two outcomes are a disjunction, not a preference. A refusal is a legal
    answer — the kept quote fence gives one for every single- and
    triple-quoted literal at a `_string` site (Q1), and a cast key outside
    `_CAST_TARGETS` gives one too. What is forbidden is the third outcome, the
    one this spec is about: a conversion that **succeeds** and emits pandas
    carrying a string whose value is not the string the model wrote.

    Stated over **values**, which is the only formulation that survives a fence
    change, and the reason it is stronger than either half of the existing
    generative pair: a property asserting "did not raise" passes on D3, and one
    asserting "refused honestly" passes on D1 (`bugfix.md` 1.20).

    Three checks, in the order a failure is easiest to read:

    * **tier 2** — no emitted literal has an origin it cannot have had;
    * **appearance** — what the site is supposed to derive is actually there,
      so a silently dropped literal is not mistaken for a preserved one;
    * **tier 1** — the emission depends on the literal's value and not on its
      spelling.

    Validates: Requirements 2.16
    """
    site, value, (spelling, source_text) = case
    trial = P41Case(site=site, value=value, spelling=spelling, source_text=source_text)
    state, payload = p41_outcome(trial.statement)
    event(f"site={site.site} {site.what}")
    event(f"spelling={spelling}")
    event(f"outcome={state}")

    if state == "refused":
        # A refusal is legal, but it must be a refusal ABOUT a construct: a block
        # that refuses as `python_syntax` says nothing about a literal's value.
        assert payload != "python_syntax", f"generated block did not parse: {trial.statement}"
        assert payload, f"refused with no construct named: {trial.statement}"
        return

    emitted = payload
    allowed = p41_allowed_values(site, value)

    # ── TIER 2 — exhaustion of origins. The load-bearing check. ────────
    offenders = p41_offenders(emitted, allowed)
    assert offenders == [], (
        f"site {site.site} ({site.what}), spelling={spelling}\n"
        f"  source literal : {source_text}\n"
        f"  its value      : {value!r}\n"
        f"  block          : {trial.statement}\n"
        f"  emitted        : {emitted!r}\n"
        f"  offending      : {offenders!r}\n"
        f"  allowed        : {sorted(allowed)!r}\n"
        f"A string literal in the emitted pandas has a value with no legitimate origin. `convert` is "
        f"a pure function of the block, so an emitted literal can only come from a string the "
        f"converter authors, a scaffolding literal, or this one generated slot — and this value is "
        f"none of them. That is the class D3 and D5 belong to: a conversion that SUCCEEDED and "
        f"changed what the string means. Expectations here come from Python's literal semantics, "
        f"never from converter output (`LGR-28b4d75-5`), so do NOT adjust the expected value to "
        f"match the emission. The one benign cause is a newly-authored converter constant, which "
        f"belongs in P41_AUTHORED_CONSTANTS with the line that writes it."
    )

    # ── APPEARANCE — the derived value is actually present. ────────────
    derived = p41_expected_derived(site, value)
    emitted_values = p41_literal_values(emitted)
    if derived is None:
        if value not in P41_UNDECIDABLE_CAST_KEYS:
            assert value not in emitted_values, (
                f"site {site.site} ({site.what}), spelling={spelling}\n"
                f"  block   : {trial.statement}\n"
                f"  emitted : {emitted!r}\n"
                f"`_cast`'s target key SELECTS an emission and is never emitted itself (`:1431` is a "
                f"dict lookup into `_CAST_TARGETS`, and `tasks.md` 4.3 is explicit that it needs no "
                f"fence wrap). The key reaching the emitted text means a wrap was added there."
            )
        else:
            # Not decidable at this value — see P41_UNDECIDABLE_CAST_KEYS. Record
            # it in the statistics so a reader of `--hypothesis-show-statistics`
            # sees the exemption being taken rather than having to infer it.
            event(f"selects-undecidable={value}")
    else:
        assert derived in emitted_values, (
            f"site {site.site} ({site.what}), spelling={spelling}\n"
            f"  source literal : {source_text}\n"
            f"  expected value : {derived!r}\n"
            f"  block          : {trial.statement}\n"
            f"  emitted        : {emitted!r}\n"
            f"  emitted values : {emitted_values!r}\n"
            f"The conversion succeeded and the value this site is supposed to carry is not in the "
            f"emission at all. Tier 2 alone cannot see this — a DROPPED literal leaves no offending "
            f"value behind — which is why the appearance direction is asserted separately."
        )

    # ── TIER 1 — the emission depends on the VALUE, not the SPELLING. ──
    control_state, control_payload = p41_outcome(trial.control)
    if control_state != "converted":
        # The control is always a plain `"…"` fence, so it passes `_string`'s
        # quote check by construction; it can still refuse for a reason that is
        # about the VALUE (a key outside `_CAST_TARGETS`, a `select` name that
        # will not resolve). In that case the trial should have refused too, and
        # a trial that converts where the plain spelling of the same value
        # refuses is itself a spelling dependence.
        pytest.fail(
            f"site {site.site} ({site.what}), spelling={spelling}\n"
            f"  block           : {trial.statement}   -> CONVERTED\n"
            f"  control block   : {trial.control}   -> REFUSED ({control_payload})\n"
            f"The plain-fence spelling of the SAME value refuses where this spelling converts, so "
            f"the outcome depends on the literal's spelling rather than on its value. Under the "
            f"value contract the only spelling-sensitive decision left is `_string`'s quote fence, "
            f"and that fence can only refuse MORE spellings than the plain one, never fewer."
        )
    assert p41_literal_split(emitted) == p41_literal_split(control_payload), (
        f"site {site.site} ({site.what}), spelling={spelling}\n"
        f"  source literal  : {source_text}\n"
        f"  its value       : {value!r}\n"
        f"  block           : {trial.statement}\n"
        f"  control block   : {trial.control}\n"
        f"  emitted         : {emitted!r}\n"
        f"  control emitted : {control_payload!r}\n"
        f"Two spellings of ONE value produced different emissions. `_string` returns the value and "
        f"each site escapes that value for its own fence, so the emission is a function of the "
        f"value alone — that is the one sentence `_string`'s docstring now states, and this is it "
        f"machine-checked. Compared as skeleton plus literal VALUES rather than bytes, because "
        f'`lit` re-emits source text through `_segment` into an identical fence: `lit(r"a\\b")` '
        f'and `lit("a\\\\b")` differ in bytes and agree in value, and that is correct.'
    )
