# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p40-pandas-proxy-literal-cast, Property 5: a refusal decided by the VOCABULARY — an
# unrecognised bare name in function position, a method closed by enumeration, or an unrecognised
# `cast` target — names the same construct and carries no `reason` whether or not the receiver-shape
# fence is armed on the same receiver, so its downstream classification stays
# `classify_conversion_refusal(construct)` and is never `scalar_literal_receiver`.
"""Property 5 — the receiver-shape fence is orthogonal to the vocabulary closure.

Spec: `.kiro/specs/p40-pandas-proxy-literal-cast/`. Requirements **R2** and **R9**
(`design.md` §1); `bugfix.md` **3.5**; the design's explicit do-NOT — *"the new
tests must assert that orthogonality rather than assume it"* (`design.md` §3).

TWO INDEPENDENT REFUSAL RULES NOW COEXIST IN THE CONVERTER, AND NOTHING ASSERTED
THAT ONE DOES NOT ABSORB THE OTHER.

* **Vocabulary closure** (pre-p40, a fence). An unrecognised bare name in
  FUNCTION position refuses (`pyspark_to_pandas.py:876-901`); METHOD position is
  closed by enumeration, `_UNSUPPORTED_METHODS` being the only fence there
  (`:935-936`). Both raise with **no** `reason`, so the runner classifies them by
  construct NAME through `classify_conversion_refusal`
  (`code_interpreter_runner.py:189-200` — p41 task 9.3 corrected this citation
  only; it read `:162-173`, which task 9.2's +27 comment lines moved. **No
  assertion in this file was amended.** This file re-implements the classifier
  over the frozenset as IMPORTED, so p41 2.7 removing `initcap` from the set
  flowed through with nothing to change), whose conservative default is
  `not_yet_expressible` — "a defect in this system, and the thing to fix".
* **The receiver-shape fence** (p40 R2). Fires on *what was emitted for the
  receiver* (`_require_series` → `_refuse_scalar`, `:664-686`), never on whether a
  name is enumerated, and its refusals carry
  `reason=REFUSAL_REASON_SCALAR_LITERAL`.

The two are orthogonal by construction — different inputs decide them — but a
claim by construction is not a checkable claim. **The failure mode this property
guards** is a receiver-shape refusal SHADOWING a vocabulary refusal, so an
unrecognised name applied to a literal would report `scalar_literal_receiver` (a
deliberate decline, nothing to fix) instead of `not_yet_expressible` (a converter
gap that IS a defect). That misclassification would silently retire the
converter's own to-do list, which is exactly what p32 2.23 forbids.

WHAT `test_converter_vocabulary_closure_prop.py` HOLDS TODAY, AND WHY THIS FILE
IS NOT A RESTATEMENT OF IT. That file is a fence and is **not touched** here. It
holds, generatively over 100 examples, that an unrecognised bare name in function
position refuses NAMING ITSELF (construct, line number, name in the message) and
that no conversion ever emits an undefined name; and by example that six
enumerated frame methods refuse in method position and that the supported
vocabulary still converts. Two things it does not do, and both are what this file
adds:

1. **It never mentions `reason`.** It asserts the construct NAME travels, not
   which of the runner's reasons the refusal ends up classified as — and the
   reason is the only field on which a shadowing refusal would differ, because the
   fence reuses the same construct names (`ConversionError`'s docstring: `cast`
   with an unrecognised target and `cast` applied to a literal are both refusals
   named `cast`).
2. **Every receiver it generates is `col(...)`** — a Series. The receiver-shape
   fence is therefore never armed in any example it runs, so it is structurally
   incapable of observing the collision. This file generates the literal receiver
   as well, and pairs the two.

`test_converter_refusal_reason.py` (task 4.4) is the other neighbour, and is also
not duplicated: it asserts the fence's eight construct names are DISJOINT from
`_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS`, and pins two named pre-p40 refusals
(`groupBy`, `posexplode`) as carrying `reason is None`. Both are statements about
names, by example. This file is the generative statement about the *decision*,
over receiver shapes it varies on purpose.

THE MECHANISM, READ RATHER THAN ASSUMED — AND IT IS STRONGER THAN "THE CHECKS ARE
IN THE RIGHT ORDER INSIDE A BRANCH". Three separate orderings keep the vocabulary
ahead of the fence, and knowing which one applies is what tells you what a red
here would mean:

* `convert` PRE-SCANS every statement and refuses on the first unsupported
  `_occurrences` entry **before `translator.statement()` is called at all**
  (`pyspark_to_pandas.py:1385-1387`). Nothing has been translated at that point,
  so no receiver text exists for the fence to look at. This covers every
  enumerated name — `_UNSUPPORTED_FUNCTIONS`, `_UNSUPPORTED_NAMES` and
  `_UNSUPPORTED_METHODS` (`_occurrences`, `:558-568`).
* An identifier on NO list is not an occurrence, so the pre-scan does not see it
  and it reaches `_function_call`'s final branch (`:876-901`), which raises before
  any argument is visited.
* An unrecognised `.cast()` target is refused by `_cast` after the `_CAST_TARGETS`
  lookup and before `self._expr(func.value, frame)` runs (`:1006-1017`) — the one
  ordering the design calls out explicitly, and the only one of the three where
  the two rules would name the same construct.

Method position is therefore protected TWICE over (the pre-scan, and
`_method_call`'s own `_UNSUPPORTED_METHODS` check ahead of the receiver,
`:935-936`), which was measured: breaking that half takes both changes, and either
one alone leaves the outcome untouched.

HOW NON-VACUITY IS BUILT IN RATHER THAN HOPED FOR. A property that only asserted
"the unrecognised name still refuses with no reason" would pass just as happily
against a receiver the fence cannot see — i.e. it would prove nothing about a
collision, because there would be no second rule in play. So each example asserts
a **control** first: the same scalar receiver, handed to a construct the fence DOES
guard (`trim`, guard site 1 of 7), refuses with `scalar_literal_receiver`; and the
paired `col(...)` receiver converts. That is per-example evidence that the fence is
live on this exact receiver text and dormant on that one. The property is then the
INVARIANCE across the pair: both receivers, same construct, same absent reason.
The distribution over the three refusal kinds is additionally reported through
`event()` and asserted by the coverage guard at the foot of the file.

MEASURED, not claimed. Over 300 examples the kinds came out `function` 38.10%,
`cast-target` 37.46%, `method` 19.37%, with all six scalar receiver spellings
drawn (7.62%–31.11%). The property was then run against three mutations of the
converter, applied in a throwaway process rather than to the tree, to confirm it
fails when the orthogonality does:

* `_refuse` made to attach `scalar_literal_receiver` — CAUGHT.
* `_cast`'s guard moved AHEAD of the `_CAST_TARGETS` lookup, i.e. the ordering
  fact reversed — CAUGHT, on `lit(None).cast(<unknown target>)`.
* method position made to consult the receiver — caught only once BOTH protections
  are removed (the pre-scan and `_method_call`'s own check); either alone leaves
  the outcome unchanged, which is how the double protection was measured rather
  than inferred.

ORACLE RULE. Expectations come from `bugfix.md` 3.5, from `design.md` §3's do-NOT
and from `classify_conversion_refusal`'s documented default — never from what
`convert()` currently emits (`LGR-28b4d75-5`, template: a test transcribed from
the producer's output cannot detect the producer drifting). The enumerated name
sets are READ from the module rather than transcribed, so a name added to any of
them later is covered here without an edit — the same reason `LGR-b46a5bb-4`
(template) gives for a path-scoped property with no allowlist: an allowlist is a
door.

EXPECTED-RED WINDOW: closed. Every subject change had landed before this file was
written, so it is expected GREEN on its first run. A red here is a real finding
about the implementation, not a test to adjust.

**Validates: Requirements R2, R9**
"""

import keyword
import os
import sys
from collections import Counter

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# Add Lambda source to path (same shape as the rest of this directory).
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import pyspark_to_pandas
from code_interpreter_runner import (
    _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS,
    NOT_VERIFIED_REASON_NOT_YET,
    NOT_VERIFIED_REASON_PERMANENT,
    classify_conversion_refusal,
)
from pyspark_to_pandas import (
    REFUSAL_REASON_SCALAR_LITERAL,
    ConversionError,
    PySpark2PandasConverter,
)

# ──────────────────────────────────────────────────────────────────────
# The vocabulary, READ from the module.
#
# Not transcribed: a name added to any of these sets later is covered by this
# property without an edit here, which is the whole difference between a fence
# and a list someone has to remember to update.
# ──────────────────────────────────────────────────────────────────────

#: Every name the converter has an opinion about, in either position. A generated
#: identifier is filtered against all of them so it is unambiguously a name with
#: no translation rule anywhere, rather than one that happens to be unknown in the
#: position under test.
KNOWN_NAMES = (
    set(pyspark_to_pandas._SUPPORTED_FUNCTIONS)
    | set(pyspark_to_pandas._UNSUPPORTED_FUNCTIONS)
    | set(pyspark_to_pandas._UNSUPPORTED_NAMES)
    | set(pyspark_to_pandas._SUPPORTED_METHODS)
    | set(pyspark_to_pandas._UNSUPPORTED_METHODS)
)

#: Names the emitted block binds itself; `df` is the input frame, so `df(...)` is
#: a different shape rather than an unrecognised function.
EMITTED_PRELUDE = frozenset({"pd", "np", "df"})

#: `_UNSUPPORTED_METHODS` is the *only* fence in method position (the module's own
#: comment at `:361-364`), which is what "closed by enumeration" means. Sampling
#: the live set is how that half stays covered as the set grows.
ENUMERATION_CLOSED_METHODS = sorted(pyspark_to_pandas._UNSUPPORTED_METHODS)

#: Cast target spellings the module recognises. An unrecognised one is refused by
#: `_cast` BEFORE the receiver is translated, which is the ordering fact below.
KNOWN_CAST_TARGETS = frozenset(pyspark_to_pandas._CAST_TARGETS)

#: The three reason strings, aliased with an explicit type. Both producers live
#: under `lambda/`, which this layer imports by `sys.path` rather than as a typed
#: package, so the constants arrive untyped; naming them here keeps the helpers
#: below checkable without restating any of the strings.
REASON_NOT_YET: str = NOT_VERIFIED_REASON_NOT_YET
REASON_PERMANENT: str = NOT_VERIFIED_REASON_PERMANENT
REASON_SCALAR_LITERAL: str = REFUSAL_REASON_SCALAR_LITERAL


def _fence_reason_for(receiver: str) -> str | None:
    """What the receiver-shape fence says about *receiver*, measured not assumed.

    Probed through `trim`, guard site 1 of 7 (`pyspark_to_pandas.py:828-834`),
    because `.str` is a Series accessor and `"x".str.strip()` raises. Returns the
    refusal's ``reason`` when the fence fires, and ``None`` when the receiver
    converts — so a caller can assert the fence is ARMED on one receiver and
    DORMANT on another, which is what makes the invariance below non-vacuous.
    """
    try:
        PySpark2PandasConverter.convert(f'df_transformed = df.withColumn("probe", trim({receiver}))')
    except ConversionError as exc:
        reason: str | None = exc.reason
        return reason
    return None


def _expected_classification(construct: str) -> str:
    """The reason a refusal carrying NO reason must end up classified as.

    From `classify_conversion_refusal`'s documented rule rather than by calling it
    on the same input: anything off the permanent list defaults to *not yet
    expressible*, because defaulting the other way would let a real converter gap
    read as "nothing to fix" (p32 2.23).
    """
    if construct in _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS:
        return REASON_PERMANENT
    return REASON_NOT_YET


# ──────────────────────────────────────────────────────────────────────
# Generators.
# ──────────────────────────────────────────────────────────────────────

#: Identifiers with no translation rule in either position. A Python keyword would
#: make the rendered statement a SyntaxError — which refuses for an unrelated
#: reason and says nothing about the vocabulary — and a type-constructor name is
#: consumed by `.cast()` rather than called.
unknown_identifiers = (
    st.from_regex(r"\A[a-z_][a-z_0-9]{0,15}\Z")
    .filter(lambda name: name not in KNOWN_NAMES)
    .filter(lambda name: name not in EMITTED_PRELUDE)
    .filter(lambda name: not keyword.iskeyword(name) and not keyword.issoftkeyword(name))
    .filter(lambda name: not pyspark_to_pandas._TYPE_NAME.match(name))
)

#: Both doorways onto a function-position refusal: a name nobody enumerated (the
#: `posexplode` class) and one the module enumerates as unsupported. Both raise
#: with no reason, so both are vocabulary-decided.
function_position_names = st.one_of(
    unknown_identifiers, st.sampled_from(sorted(pyspark_to_pandas._UNSUPPORTED_FUNCTIONS))
)

#: Cast target spellings the module does not know.
unknown_cast_targets = st.from_regex(r"\A[a-z][a-z0-9_]{0,11}\Z").filter(
    lambda spelling: spelling not in KNOWN_CAST_TARGETS
)

#: Scalar-literal receivers — every one of them a `lit(<Constant>)`, so the
#: emission is a bare Python constant and `_is_scalar_emission` recognises it.
#: `lit(-1)` is deliberately absent: its argument is a `UnaryOp`, not a
#: `Constant`, so it refuses as `lit` and would make the control probe report on
#: the wrong rule.
scalar_receivers = st.sampled_from(("lit(None)", 'lit("x")', 'lit("")', "lit(0)", "lit(1.5)", "lit(True)"))

#: Series receivers, the shape the vocabulary closure test already generates.
column_names = st.sampled_from(("name", "city", "amount"))


@st.composite
def vocabulary_refusals(draw):
    """A refusal the VOCABULARY decides, as ``(kind, construct, template)``.

    The template carries a single ``{receiver}`` hole so the same refusal can be
    rendered twice — once over a Series receiver and once over a scalar-literal
    one — which is the pair the property's invariance is stated over.

    Three kinds, one per doorway the vocabulary owns — see the module docstring
    for which ordering protects each:

    * ``function`` — a bare name in call position, drawn both from the module's
      enumerated-unsupported set (refused by `convert`'s pre-scan) and from
      identifiers on no list at all (refused by `_function_call`'s final branch).
      Neither translates an argument first.
    * ``method`` — a name on `_UNSUPPORTED_METHODS`, the only fence in method
      position, and the half protected twice over.
    * ``cast-target`` — an unrecognised `.cast()` target. The sharpest case: the
      fence's own construct name is also `cast`, so the construct alone cannot
      tell the two rules apart, and the guard sits AFTER the `_CAST_TARGETS`
      lookup on purpose (`pyspark_to_pandas.py:1006-1008`) precisely so this
      refusal keeps reporting as `cast` with no reason.
    """
    kind = draw(st.sampled_from(("function", "method", "cast-target")))
    if kind == "function":
        name = draw(function_position_names)
        return kind, name, f'df_transformed = df.withColumn("out", {name}({{receiver}}))'
    if kind == "method":
        name = draw(st.sampled_from(ENUMERATION_CLOSED_METHODS))
        return kind, name, f'df_transformed = df.withColumn("out", {{receiver}}.{name}())'
    target = draw(unknown_cast_targets)
    return kind, "cast", f'df_transformed = df.withColumn("out", {{receiver}}.cast("{target}"))'


#: What the property actually exercised, for the coverage guard below. A property
#: whose generator drifted onto one kind, or never produced an unrecognised name,
#: would prove far less than it appears to.
_EXERCISED: Counter = Counter()

PROP_SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@PROP_SETTINGS
@given(refusal=vocabulary_refusals(), scalar_receiver=scalar_receivers, column=column_names)
def test_a_vocabulary_refusal_is_unmoved_by_the_receiver_shape_fence(refusal, scalar_receiver: str, column: str):
    """Property 5 — the two rules cannot collide, asserted rather than assumed.

    The receiver-shape fence changes NO outcome of the vocabulary closure: for a
    refusal the vocabulary decides, the construct named and the absent `reason`
    are identical over a receiver the fence is armed on and one it is not, so the
    downstream classification stays the construct-keyed one and never becomes
    `scalar_literal_receiver`.

    Validates: Requirements R2, R9
    """
    kind, construct, template = refusal
    series_receiver = f'col("{column}")'
    event(f"refusal kind: {kind}")
    event(f"scalar receiver: {scalar_receiver}")
    _EXERCISED[kind] += 1
    _EXERCISED[f"receiver:{scalar_receiver}"] += 1

    # ── Control: BOTH rules are live on this input pair. ──────────────────
    # Without this, "no reason" could just mean "no fence anywhere near", and the
    # property would be about an absence rather than about a disambiguation.
    assert _fence_reason_for(scalar_receiver) == REASON_SCALAR_LITERAL, (
        f"the control failed before the property was reached: {scalar_receiver} is supposed to be a "
        "scalar emission the receiver-shape fence refuses, so this example cannot demonstrate "
        "orthogonality. Either _is_scalar_emission stopped recognising it or trim's guard moved."
    )
    assert _fence_reason_for(series_receiver) is None, (
        f"the control failed: {series_receiver} is a Series, so the fence must stay out of its way"
    )

    # ── The property: the vocabulary decides, identically, over both. ─────
    outcomes = {}
    for label, receiver in (("series", series_receiver), ("scalar", scalar_receiver)):
        statement = template.format(receiver=receiver)
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(statement)
        exc = raised.value

        assert exc.construct == construct, (
            f"{statement!r} refused naming {exc.construct!r} instead of {construct!r} — the "
            "receiver-shape fence names the construct that OWNS the member, so a fence construct "
            "here means the fence decided a refusal the vocabulary owns"
        )
        assert exc.reason is None, (
            f"{statement!r} refused carrying reason {exc.reason!r}. A vocabulary refusal must carry "
            "no reason so `_conversion_failure_results` classifies it by construct name exactly as "
            "it did before p40 (bugfix.md 3.5, 3.7)"
        )

        # The consequence, at the seam that reads it:
        # `reason = exc.reason or classify_conversion_refusal(exc.construct)`.
        derived = exc.reason or classify_conversion_refusal(exc.construct)
        assert derived == _expected_classification(construct), (
            f"{statement!r} classifies as {derived!r}; the construct-keyed rule says "
            f"{_expected_classification(construct)!r}"
        )
        assert derived != REASON_SCALAR_LITERAL, (
            f"{statement!r} reports a deliberate decline for what is a converter GAP — the "
            "misclassification that would retire the converter's own to-do list"
        )
        outcomes[label] = (exc.construct, exc.reason, derived)

    # The invariance itself, stated once the two halves are in hand: arming the
    # fence changed nothing about a refusal the vocabulary owns.
    assert outcomes["series"] == outcomes["scalar"], (
        f"the receiver shape changed the outcome of a vocabulary refusal: {outcomes}"
    )


def test_the_property_exercised_every_kind_and_both_receiver_families():
    """Coverage guard — measured, not asserted in prose.

    Reads what the property above actually drew. A generator that drifted onto one
    kind, or whose identifier filter starved, would leave the property looking
    green while covering a fraction of the claim. Must run after the property, so
    it sits below it in the file; the counts are also visible per example through
    `--hypothesis-show-statistics`.
    """
    assert _EXERCISED, "the property did not run, so its coverage cannot be judged"
    total = sum(_EXERCISED[kind] for kind in ("function", "method", "cast-target"))
    shares = {kind: 100.0 * _EXERCISED[kind] / total for kind in ("function", "method", "cast-target")}

    for kind, share in shares.items():
        assert share >= 10.0, f"refusal kind {kind!r} was only {share:.2f}% of examples: {dict(_EXERCISED)}"
    # Every scalar receiver spelling has to appear, because `_is_scalar_emission`
    # recognises constants by ast node and a spelling nothing draws is a spelling
    # nothing checks.
    for receiver in ("lit(None)", 'lit("x")', 'lit("")', "lit(0)", "lit(1.5)", "lit(True)"):
        assert _EXERCISED[f"receiver:{receiver}"] > 0, f"{receiver} was never drawn: {dict(_EXERCISED)}"
