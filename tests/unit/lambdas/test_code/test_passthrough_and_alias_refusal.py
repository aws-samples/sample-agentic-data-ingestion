# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p41 T0 — `_passthrough_call` characterization, and the refusal-payload reds.

**Validates: Requirements 1.8, Property 3**

══════════════════════════════════════════════════════════════════════
TWO OPPOSITE ORACLE RULES LIVE IN THIS FILE. READ THIS BEFORE EDITING.
══════════════════════════════════════════════════════════════════════

`LGR-28b4d75-5` (template / ratified): *a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle.* p41 therefore runs two rules in one directory, and this file holds
both — in separate, labelled classes.

**Task 1.1 (this file's `TestPassthrough*` and `TestAlias*` classes) is
CHARACTERIZATION. The oracle IS current output.** Every expectation below was
transcribed from what the converter emits today, by running it — not derived
from what it ought to emit. The semantics-derived rule that governs tasks
1.2/1.3 and all of tasks 2.x **does not apply here**, and applying it here
would be a mistake: `_passthrough_call` (`pyspark_to_pandas.py:903`) is the one
symbol in p41's change set with no direct test coverage (`design.md` §6), and
these pins exist so that T6 changing the alias path produces a legible *diff*
rather than a surprise. `design.md` §10: **do NOT change `_passthrough_call`'s
behaviour** — that is not this task's job and no clause asks for it.

**Tasks 1.2 and 1.3 add classes to this file whose expectations come from
`bugfix.md` 2.4/2.6 — semantics, never current output.** Those are the
expected-red windows (closed by tasks 8.1/8.2). Keep them in their own labelled
classes; do not merge a 1.2/1.3 assertion into a pin below, and do not
"reconcile" a pin below with a 1.2/1.3 assertion. Where the two describe the
same shape they are *meant* to disagree until T6 lands.

Consequently: **a red in this module's task-1.1 classes is a transcription
error or a moved working tree — a finding to investigate, never a number to
adjust.** These are green by construction.

──────────────────────────────────────────────────────────────────────
What the characterization found, and what it corrects
──────────────────────────────────────────────────────────────────────

`bugfix.md` 1.8 blames the `F`-alias refusal on `_passthrough_call`: *"it falls
to `_passthrough_call` (`:976`) whose receiver translation calls
`_expr(Name('F'))`"*. `design.md` §12 item 2 falsified half of that. Running it
falsifies the other half, and the two corrections are independent:

1. **`convert` never gets that far.** `_occurrences` marks `Name('F')`
   unsupported (`:568`, `F` is in `_UNSUPPORTED_NAMES` at `:434`) and `convert`'s
   pre-scan refuses on the first unsupported occurrence at `:1385-1387`, before
   `translator.statement()` runs at all. This is §12 item 2, re-confirmed here
   by observation rather than by inspection.
2. **Even with the pre-scan bypassed, `_passthrough_call` is still not
   entered.** `_method_call` builds the callee text in its own f-string at
   `:978` — `f"{self._expr(func.value, frame)}.{attr}"` — so the receiver is
   translated while the *argument list* of the call to `_passthrough_call` is
   being evaluated. `_expr(Name('F'))` raises there, one frame short. The
   receiver translation `bugfix.md` 1.8 attributes to `_passthrough_call` does
   not happen inside it.

`_passthrough_call` can still name `F`, but only from an **argument** it
translates itself (`TestAliasRefusalNeverEntersPassthroughCall`'s last pin) —
never from a receiver. T6's alias normalisation therefore has to reach
`_occurrences` and `_call`, exactly as `design.md` §5.4 says; a normalisation
placed in `_passthrough_call` would be dead code.

pandas is not installed in this venv and must not be (`bugfix.md` 3.14). Nothing
here executes the emitted pandas, so every green below is evidence about the
**translation**, not about the execution.

No shared fixture from `tests/fixtures/conftest.py` is relevant: the converter
is pure text-to-text and touches no AWS surface.
"""

import ast
import os
import sys
from contextlib import contextmanager
from typing import NamedTuple

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import (
    _UNSUPPORTED_NAMES,
    ConversionError,
    PySpark2PandasConverter,
    _Translator,
    _Unconvertible,
)

#: The two-import header `convert()` prepends to every emission (`bugfix.md` 3.7).
P41_PANDAS_HEADER = "import pandas as pd\nimport numpy as np\n\n"


# ──────────────────────────────────────────────────────────────────────
# Helpers. `p41_` prefixed per `design.md` §10 — and note the standing
# naming fence: `is_bug_condition_d3` already exists at
# `test_converter_equivalence_baseline.py:910` and means an EARLIER spec's
# Defect 3, so no p41 helper may take that name.
# ──────────────────────────────────────────────────────────────────────


def p41_emitted_body(source: str) -> str:
    """The pandas `convert()` emits for *source*, with the import header removed."""
    code = PySpark2PandasConverter.convert(source)["code"]
    assert code.startswith(P41_PANDAS_HEADER), code
    return code[len(P41_PANDAS_HEADER) :]


@contextmanager
def p41_passthrough_spy():
    """Record every `_passthrough_call` entry, with the callee text it was handed.

    The only way to tell "reached `_passthrough_call` and it passed the call
    through" from "never reached it" — both of which can end in the same
    emitted text, and which is the whole subject of the alias correction above.
    Wraps rather than replaces, so the pinned behaviour is the real one.
    """
    entries: list[str] = []
    original = _Translator._passthrough_call

    def recorder(self, node, callee, frame):
        entries.append(callee)
        return original(self, node, callee, frame)

    _Translator._passthrough_call = recorder
    try:
        yield entries
    finally:
        _Translator._passthrough_call = original


@contextmanager
def p41_string_spy():
    """Record the SOURCE TEXT of every literal handed to `_string` (`:737`).

    The only way to distinguish "refused *inside* the literal fence" from
    "refused before the literal was ever looked at" — which is exactly what
    separates task 1.3's `df.filter(...)` shapes from its controls, and
    therefore what localises D2a to one call site.

    **The signature is `(self, node, owner, *args, **kwargs)` deliberately.**
    Task 8.1 gives `_string` an optional third parameter (`construct`), and a
    recorder pinned to two positional parameters would raise `TypeError` the
    moment `:1232` starts passing `"rlike"`. Forwarding varargs is what lets
    every assertion below survive 8.1 unamended, which `tasks.md` requires.

    Wraps rather than replaces, so the behaviour observed is the real one.
    """
    segments: list[str] = []
    original = _Translator._string

    def recorder(self, node, owner, *args, **kwargs):
        segments.append(ast.get_source_segment(self._source, node) or "")
        return original(self, node, owner, *args, **kwargs)

    _Translator._string = recorder
    try:
        yield segments
    finally:
        _Translator._string = original


def p41_rlike_pattern_node(source: str) -> "tuple[_Translator, ast.Constant, ast.Call]":
    """The `rlike` pattern literal in *source*, its owner, and a `_Translator`.

    *owner* is the `col(...).rlike(...)` `Call` — precisely the node
    `_filter_mask` hands to `_string` as `owner` at `:1232`. For calling the
    fence directly, which is how the quote-fence half is asserted without
    depending on which construct the refusal happens to name.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "rlike":
            assert len(node.args) == 1, source
            pattern = node.args[0]
            assert isinstance(pattern, ast.Constant), source
            return _Translator(source), pattern, node
    raise AssertionError(f"no rlike call found in {source!r}")


def p41_translate_statement(source: str) -> "str | None":
    """Translate one statement through `_Translator`, bypassing `convert`'s pre-scan.

    `convert` refuses on the first unsupported `_occurrences` entry (`:1385-1387`)
    before the translator runs, so this is the only way to observe what the
    translator would have done with a shape the pre-scan intercepts.
    """
    tree = ast.parse(source)
    return _Translator(source).statement(tree.body[0])


def p41_call_node(expression: str) -> "tuple[_Translator, ast.Call]":
    """A `_Translator` over *expression* plus its parsed `ast.Call`.

    For calling `_passthrough_call` directly with a callee string already
    resolved — the only way to reach its own argument-translation path.
    """
    node = ast.parse(expression, mode="eval").body
    assert isinstance(node, ast.Call), expression
    return _Translator(expression), node


class P41PassthroughPin(NamedTuple):
    """One byte-exact characterization pin over a `_passthrough_call` shape."""

    case_id: str
    source: str
    emitted: str
    #: Callee text handed to `_passthrough_call`, in entry order. Length is the
    #: number of times the method was entered for this input.
    callees: tuple[str, ...]
    #: `conversion_report["constructs_converted"]`, exactly as sorted.
    constructs: list[str]


# ══════════════════════════════════════════════════════════════════════
# TASK 1.1 — CHARACTERIZATION PINS. ORACLE RULE: CURRENT OUTPUT IS THE
# ORACLE. Transcribed from observed behaviour on the working tree
# (HEAD `ba3dfab` + p40's uncommitted implementation). NOT derived from
# requirements. The semantics-derived rule that governs tasks 1.2/1.3
# and 2.x does NOT apply to anything in this section.
# ══════════════════════════════════════════════════════════════════════

# HOW `_passthrough_call` IS REACHED, which is the only way it is reachable.
# `_method_call` (`:933`) tries, in order: `_UNSUPPORTED_METHODS`, `otherwise`,
# `cast`, `alias`, `_FRAME_METHODS`, `when`, then
# `isNull`/`isNotNull`/`rlike` — and falls through at `:978` to
# `_passthrough_call` for a method whose attribute is in NONE of those. So the
# reachable input is `<receiver>.<unrecognised attr>(...)` in expression
# position, and the emission is that call spelled with its receiver and
# arguments translated. `_occurrences` (`:547`) only flags attributes in
# `_SUPPORTED_METHODS`/`_UNSUPPORTED_METHODS`, so an unrecognised attribute is
# not an occurrence and `convert`'s pre-scan lets it past.
#
# NOTE what these pins record about the method-position default, because it is
# the asymmetry `_function_call`'s last branch (`:876-901`) documents at length:
# an unrecognised bare name in FUNCTION position refuses, while an unrecognised
# attribute in METHOD position passes through. That is deliberate — a receiver
# can be any object, so the method side is closed by enumeration instead — and
# it is why `x = col("a").mystery()` below converts to a pandas method that
# does not exist rather than refusing. Pinned as-is; p41 does not touch it.

P41_PASSTHROUGH_EMISSION_PINS: list[P41PassthroughPin] = [
    # ── The simplest shape: no arguments at all. ──
    P41PassthroughPin(
        case_id="passthrough-no-args",
        source='x = col("a").strip()',
        emitted='x = df["a"].strip()',
        callees=('df["a"].strip',),
        constructs=["col"],
    ),
    # ── Arguments: translated left to right and joined with ", ". ──
    P41PassthroughPin(
        case_id="passthrough-one-arg",
        source='x = col("a").fillna(0)',
        emitted='x = df["a"].fillna(0)',
        callees=('df["a"].fillna',),
        constructs=["col"],
    ),
    P41PassthroughPin(
        case_id="passthrough-mixed-args",
        source='x = col("a").mystery(1, "two", col("b"))',
        emitted='x = df["a"].mystery(1, "two", df["b"])',
        callees=('df["a"].mystery',),
        constructs=["col"],
    ),
    P41PassthroughPin(
        case_id="passthrough-nested-col-arg",
        source='x = col("a").add(col("b"))',
        emitted='x = df["a"].add(df["b"])',
        callees=('df["a"].add',),
        constructs=["col"],
    ),
    # A starred argument survives because `_expr` falls through to `_segment`
    # for a construct-free node (`:786`), so `*[1]` is emitted verbatim.
    P41PassthroughPin(
        case_id="passthrough-starred-arg",
        source='x = col("a").mystery(*[1])',
        emitted='x = df["a"].mystery(*[1])',
        callees=('df["a"].mystery',),
        constructs=["col"],
    ),
    # An argument that is itself a translated construct: the `when` chain
    # becomes `np.where(...)` before it is spliced in.
    P41PassthroughPin(
        case_id="passthrough-when-arg",
        source='x = col("a").mystery(when(col("b") > 1, 2).otherwise(3))',
        emitted='x = df["a"].mystery(np.where(df["b"] > 1, 2, 3))',
        callees=('df["a"].mystery',),
        constructs=["col", "when_otherwise"],
    ),
    # ── Receiver shapes. `_method_call` translates the receiver at `:978`,
    # so whatever it emits becomes the callee prefix. ──
    P41PassthroughPin(
        case_id="passthrough-supported-fn-receiver",
        source='x = trim(col("a")).mystery()',
        emitted='x = df["a"].str.strip().mystery()',
        callees=('df["a"].str.strip().mystery',),
        constructs=["col", "trim"],
    ),
    # A SCALAR receiver passes through unguarded: this site appends nothing
    # from `_SERIES_ONLY_MEMBERS`, so p40's `_require_series` fence (`:664`) is
    # not applied here and `lit("s").mystery()` emits `"s".mystery()`. Pinned
    # as characterization, NOT endorsed — p40's guard sites are enumerated and
    # this is not one of them.
    P41PassthroughPin(
        case_id="passthrough-lit-receiver",
        source='x = lit("s").mystery()',
        emitted='x = "s".mystery()',
        callees=('"s".mystery',),
        constructs=["lit"],
    ),
    # No PySpark construct anywhere: `_expr`'s construct-free fall-through
    # emits the receiver verbatim and `constructs_converted` stays empty.
    P41PassthroughPin(
        case_id="passthrough-plain-name-receiver",
        source="x = y.mystery()",
        emitted="x = y.mystery()",
        callees=("y.mystery",),
        constructs=[],
    ),
    P41PassthroughPin(
        case_id="passthrough-subscript-receiver",
        source='x = frame["a"].mystery()',
        emitted='x = frame["a"].mystery()',
        callees=('frame["a"].mystery',),
        constructs=[],
    ),
    # ── Composition: the method is entered once per unrecognised attribute,
    # innermost first, and the outer callee carries the inner emission. ──
    P41PassthroughPin(
        case_id="passthrough-chained-twice",
        source='x = col("a").strip().upper2()',
        emitted='x = df["a"].strip().upper2()',
        callees=('df["a"].strip', 'df["a"].strip().upper2'),
        constructs=["col"],
    ),
    # A recognised member applied to a passthrough emission still converts:
    # `.cast` is handled by `_cast` and the passthrough result is its receiver.
    P41PassthroughPin(
        case_id="passthrough-then-cast",
        source='x = col("a").mystery().cast("string")',
        emitted='x = df["a"].mystery().astype("string")',
        callees=('df["a"].mystery',),
        constructs=["cast", "col"],
    ),
    # ── Statement positions other than a bare assignment. ──
    P41PassthroughPin(
        case_id="passthrough-inside-with-column",
        source='df_t = df.withColumn("a", col("b").mystery())',
        emitted='df_t = df.copy(); df_t["a"] = df["b"].mystery()',
        callees=('df["b"].mystery',),
        constructs=["col", "withColumn"],
    ),
    P41PassthroughPin(
        case_id="passthrough-bare-expression",
        source='col("a").mystery()',
        emitted='df["a"].mystery()',
        callees=('df["a"].mystery',),
        constructs=["col"],
    ),
]


class P41PassthroughRefusalPin(NamedTuple):
    """One byte-exact characterization pin over a refusal reached via this site."""

    case_id: str
    source: str
    construct: str
    line: str
    line_number: int
    #: Callee text handed to `_passthrough_call` before the refusal, in order.
    #: Empty means the refusal happened before the method was ever entered.
    callees: tuple[str, ...]


# `_passthrough_call`'s ONLY refusal of its own is the keyword-argument guard
# (`:904-905`), which raises `self._refuse_here(node)`. `_refuse_here` (`:627`)
# names the LEFTMOST occurrence in the whole expression — deliberate, and 8
# other call sites rely on it (`bugfix.md` 3.5), so `design.md` §10 forbids
# changing it. These pins record what that produces here. `reason` is `None`
# on every one; p40's fourth field is not set by this path.
P41_PASSTHROUGH_REFUSAL_PINS: list[P41PassthroughRefusalPin] = [
    # A keyword argument refuses, and the construct named is the leftmost
    # occurrence — `col`, which does not own the operation. This is D2's shape
    # arriving through a third site; p41 scopes D2a to `:1232` only
    # (`design.md` §12 item 1), so this pin records the same class of misnaming
    # at a site the fix deliberately does NOT touch.
    P41PassthroughRefusalPin(
        case_id="passthrough-keyword-refuses-naming-leftmost",
        source='x = col("a").fillna(value=0)',
        construct="col",
        line='x = col("a").fillna(value=0)',
        line_number=1,
        callees=('df["a"].fillna',),
    ),
    # No occurrence anywhere in the expression, so `_refuse_here` falls to its
    # `python_syntax` default (`:641`) — even though the block parses fine.
    P41PassthroughRefusalPin(
        case_id="passthrough-keyword-no-occurrences",
        source="x = y.foo(bar=1)",
        construct="python_syntax",
        line="x = y.foo(bar=1)",
        line_number=1,
        callees=("y.foo",),
    ),
    # Multi-line: the refusal reports the leftmost occurrence's line, and
    # `convert`'s `fail` closure quotes that PHYSICAL line stripped — so the
    # quoted text is the opening fragment, not the whole statement.
    P41PassthroughRefusalPin(
        case_id="passthrough-keyword-multiline",
        source='x = col("a").fillna(\n    value=0\n)',
        construct="col",
        line='x = col("a").fillna(',
        line_number=1,
        callees=('df["a"].fillna',),
    ),
    # A nested passthrough whose INNER call carries the keyword: the outer is
    # entered first, the inner refuses, and the name comes from the inner
    # node's own leftmost occurrence (`col("b")`) — reported as `col` either
    # way, which is why the callee list is the only thing that distinguishes
    # the two mechanisms.
    P41PassthroughRefusalPin(
        case_id="passthrough-nested-keyword",
        source='x = col("a").mystery(col("b").other(k=1))',
        construct="col",
        line='x = col("a").mystery(col("b").other(k=1))',
        line_number=1,
        callees=('df["a"].mystery', 'df["b"].other'),
    ),
    # `_passthrough_call` succeeded and an OUTER fence discarded its emission:
    # `_select` cannot resolve an output column name for it (`:1258-1262`,
    # `bugfix.md` 3.6's fence) and refuses naming `select`. Recorded because it
    # is the one shape where this site's work is done and then thrown away.
    P41PassthroughRefusalPin(
        case_id="passthrough-discarded-by-select-fence",
        source='df_t = df.select(col("a").mystery())',
        construct="select",
        line='df_t = df.select(col("a").mystery())',
        line_number=1,
        callees=('df["a"].mystery',),
    ),
]


class TestPassthroughCallEmission:
    """Task 1.1 — CHARACTERIZATION. Oracle: current output.

    What `_passthrough_call` emits today, byte-exactly, for every shape that
    reaches it. `design.md` §10: do NOT change this behaviour.
    """

    @pytest.mark.parametrize("pin", P41_PASSTHROUGH_EMISSION_PINS, ids=lambda pin: pin.case_id)
    def test_emitted_pandas_is_byte_exact(self, pin: P41PassthroughPin) -> None:
        with p41_passthrough_spy() as entries:
            result = PySpark2PandasConverter.convert(pin.source)
        code = result["code"]
        assert code.startswith(P41_PANDAS_HEADER)
        assert code[len(P41_PANDAS_HEADER) :] == pin.emitted
        assert result["conversion_report"]["constructs_converted"] == pin.constructs
        # The spy is what proves the emission came through THIS site rather
        # than through `_expr`'s construct-free fall-through, which for several
        # of these inputs would emit identical text.
        assert tuple(entries) == pin.callees


class TestPassthroughCallRefusalPayload:
    """Task 1.1 — CHARACTERIZATION. Oracle: current output.

    The refusal payload, byte-exactly, for every refusal reachable through
    this site — including the two that are refused by something else after it
    ran. `bugfix.md` 3.6's carrier shape is asserted here too: three
    positional fields plus an optional `reason`, and `str(exc)`'s rendering.
    """

    @pytest.mark.parametrize("pin", P41_PASSTHROUGH_REFUSAL_PINS, ids=lambda pin: pin.case_id)
    def test_refusal_payload_is_byte_exact(self, pin: P41PassthroughRefusalPin) -> None:
        with p41_passthrough_spy() as entries, pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(pin.source)
        exc = caught.value
        assert exc.construct == pin.construct
        assert exc.line == pin.line
        assert exc.line_number == pin.line_number
        assert exc.reason is None
        assert str(exc) == (f"unconvertible PySpark construct '{pin.construct}' on line {pin.line_number}: {pin.line}")
        assert tuple(entries) == pin.callees

    def test_keyword_arguments_are_the_only_refusal_this_site_raises_itself(self) -> None:
        """The guard at `:904-905` is `_passthrough_call`'s whole refusal surface.

        Everything else it does is translate and re-spell. Asserted so that a
        future edit adding a second refusal to this method has to move a pin.
        """
        assert p41_emitted_body('x = col("a").mystery(1)') == 'x = df["a"].mystery(1)'
        with pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert('x = col("a").mystery(k=1)')
        assert caught.value.construct == "col"


class TestAliasRefusalNeverEntersPassthroughCall:
    """Task 1.1 — CHARACTERIZATION. Oracle: current output.

    `bugfix.md` 1.8's mechanism, corrected twice by observation. This class
    pins WHERE the `F` refusal comes from; it deliberately says nothing about
    what the construct name *ought* to be — that is task 1.2's statement, in
    its own class, derived from `bugfix.md` 2.6 and red until task 8.2.

    The two shapes here are the two `bugfix.md` 1.8 reproduced. Task 1.2 owns
    `F.initcap(...)`, `functions.trim(...)` and `spark.conf.set(...)`; they are
    left out of this class so no shape carries two competing pins.

    ══════════════════════════════════════════════════════════════════
    AMENDED AT TASK 8.2 — see the block comment below. The class's subject
    (`_passthrough_call` is never on the alias path) SURVIVES; the two
    aliased shapes stopped refusing, so what they demonstrate changed.
    ══════════════════════════════════════════════════════════════════
    """

    # ══════════════════════════════════════════════════════════════════
    # AMENDED AT TASK 8.2 (p41 2.6), NOT DELETED — `LGR-60c4985-6` and
    # `LGR-5906ec6-2` (both template / ratified): a deliberately-held
    # assertion is amended in place, with its reason at the assertion, so
    # the reversal stays legible.
    #
    # REASON: *p41 2.6 — the refusal names the function that owns the
    # operation, not the module alias it was reached through.* For these two
    # shapes the consequence is stronger than a rename: `trim`, `lit` and
    # `col` all have translation rules, so resolving the alias makes them
    # CONVERT. 2.6 calls that out as the point — supporting the alias is "a
    # spelling fix and adds no expressiveness".
    #
    # WHAT SURVIVES UNAMENDED, and it is this class's actual subject:
    # `_passthrough_call` is still never entered on the alias path. Before
    # 8.2 that was because `convert`'s pre-scan refused first; after 8.2 it
    # is because `_call` routes an aliased call to `_function_call`. Either
    # way a normalisation placed inside `_passthrough_call` would have been
    # dead code — the claim `design.md` §5.4 rests on, and the reason 8.2
    # changed `_occurrences` and `_call` rather than this site.
    #
    # WHAT WOULD OTHERWISE HAVE BEEN LOST, and is therefore re-pinned on a
    # different shape: the pre-scan half of the mechanism — that `convert`
    # refuses on an unsupported `_occurrences` entry BEFORE
    # `translator.statement()` runs — needs an input that still refuses.
    # `ENTRY_POINT_SHAPES` carries it on `spark` / `sc` / `sqlContext`,
    # which are Spark ENTRY POINTS rather than module aliases and so are
    # untouched by 8.2 by construction (`_aliased_function` fires only for
    # a `Name` in `{"F", "functions"}`).
    # ══════════════════════════════════════════════════════════════════

    #: Amended at 8.2: `bugfix.md` 1.8's two reproduced shapes, which answered
    #: `F` before 8.2 and now CONVERT. Paired with the unaliased spelling each
    #: must match, which is 2.6's own equivalence claim rather than this
    #: file's invention.
    ALIAS_SHAPES = [
        ('x = F.trim(F.col("b"))', 'x = trim(col("b"))'),
        ('x = F.lit("x")', 'x = lit("x")'),
    ]

    #: `<entry point>.<unrecognised attr>(...)` — still refused by the pre-scan
    #: after 8.2, and still reaching `_method_call`'s fall-through, so the two
    #: amended tests below keep demonstrating the mechanism they were written
    #: for. Added at 8.2 for exactly that reason.
    ENTRY_POINT_SHAPES = [
        ('x = spark.foo(col("a"))', "spark"),
        ('x = sc.foo(col("a"))', "sc"),
        ('x = sqlContext.foo(col("a"))', "sqlContext"),
    ]

    @pytest.mark.parametrize(("source", "construct"), ENTRY_POINT_SHAPES)
    def test_convert_refuses_at_the_pre_scan_before_the_translator_runs(self, source: str, construct: str) -> None:
        """Correction 1 (`design.md` §12 item 2), observed rather than inspected.

        AMENDED AT 8.2: the shapes moved from `F.…` to `spark.…` / `sc.…` /
        `sqlContext.…`. Reason: *p41 2.6 — the refusal names the function that
        owns the operation, not the module alias*, and for `F.trim(...)` /
        `F.lit(...)` that means they now convert, so they can no longer carry a
        refusal assertion. The mechanism being pinned is unchanged, and an
        entry point is the right carrier for it: it is unsupported in every
        position and no alias predicate can resolve it.
        """
        with p41_passthrough_spy() as entries, pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(source)
        assert caught.value.construct == construct
        assert caught.value.line_number == 1
        assert entries == []

    @pytest.mark.parametrize(("source", "construct"), ENTRY_POINT_SHAPES)
    def test_translator_also_refuses_one_frame_short_of_this_site(self, source: str, construct: str) -> None:
        """Correction 2: `_method_call` translates the receiver at `:978`.

        With `convert`'s pre-scan bypassed entirely, `_passthrough_call` is
        STILL not entered — the receiver is translated while the argument list
        of the call to it is being evaluated. So the receiver translation
        `bugfix.md` 1.8 attributes to `_passthrough_call` happens in
        `_method_call`, and a fix placed inside `_passthrough_call` would never
        run for these shapes.

        AMENDED AT 8.2, same shape substitution and same reason as the test
        above. The claim is if anything easier to read on an entry point: this
        is `bugfix.md` 1.8's mechanism with the alias question removed from it.
        """
        with p41_passthrough_spy() as entries, pytest.raises(_Unconvertible) as caught:
            p41_translate_statement(source)
        assert caught.value.construct == construct
        assert caught.value.lineno == 1
        assert entries == []

    @pytest.mark.parametrize(("aliased", "unaliased"), ALIAS_SHAPES)
    def test_the_aliased_spelling_converts_and_still_never_enters_this_site(self, aliased: str, unaliased: str) -> None:
        """ADDED AT 8.2, as the amendment to the two shapes that stopped refusing.

        Reason: *p41 2.6 — the refusal names the function that owns the
        operation, not the module alias it was reached through*; `trim`, `lit`
        and `col` have rules, so the aliased spelling converts instead of
        refusing. Recorded here rather than only in task 1.2's class because
        this is where `bugfix.md` 1.8's two reproduced shapes live, and the
        reversal has to be visible at the pin it reverses.

        The `entries == []` assertion is carried over UNCHANGED and is the
        point of keeping this class: `_passthrough_call` is not on the alias
        path before 8.2 (the pre-scan refused first) and is not on it after
        (`_call` routes an aliased call to `_function_call`).
        """
        with p41_passthrough_spy() as entries:
            actual = PySpark2PandasConverter.convert(aliased)
            expected = PySpark2PandasConverter.convert(unaliased)
        assert actual["code"] == expected["code"]
        assert actual["conversion_report"] == expected["conversion_report"]
        assert entries == []
        assert "F." not in actual["code"], f"the alias survived into the emitted pandas: {actual['code']!r}"

    def test_passthrough_call_names_F_only_from_an_argument_it_translates(self) -> None:
        """The one route by which `F` CAN reach this site: an argument.

        Called directly with the callee already resolved — the state
        `_method_call` would have handed it had the receiver been convertible —
        `_passthrough_call` translates its own arguments and raises there.
        This is the pin that says the site is reachable-with-`F` in principle,
        while the tests above say it is not reachable that way in practice.

        AMENDED AT 8.2: the argument moved from `F.col("b")` to a BARE `F`.
        Reason: *p41 2.6 — the refusal names the function that owns the
        operation, not the module alias*, so an aliased **call** argument now
        resolves and converts (`F.col("b")` → `df["b"]`). The only route left
        by which `F` reaches this site is a bare alias in argument position,
        which `_UNSUPPORTED_NAMES` still refuses (all six members kept) — so
        the claim narrows rather than disappearing, and it narrows to exactly
        the fence 8.2 was forbidden to remove.
        """
        translator, node = p41_call_node("y.mystery(F)")
        with pytest.raises(_Unconvertible) as caught:
            _Translator._passthrough_call(translator, node, "already.resolved.callee", "df")
        assert caught.value.construct == "F"
        assert caught.value.lineno == 1
        # And the shape this pin used to carry, recorded as the reversal: an
        # aliased call in argument position now converts through the same site.
        converting_translator, converting_node = p41_call_node('y.mystery(F.col("b"))')
        assert (
            _Translator._passthrough_call(converting_translator, converting_node, "already.resolved.callee", "df")
            == 'already.resolved.callee(df["b"])'
        )


# ══════════════════════════════════════════════════════════════════════
# TASK 1.2 — D2b: THE ALIAS REFUSAL NAMES THE ALIAS, NOT THE FUNCTION.
#
# **Validates: Requirements 1.8, 2.6, Property 3**
#
# FOUR CLASSES BELOW, AND THEY DO NOT SHARE AN ORACLE RULE. Read the
# class docstring before changing an assertion in it.
#
#   1. `TestAliasedFunctionRefusalNamesTheFunction`
#      Oracle: `bugfix.md` 2.6 — SEMANTICS. Current converter output is
#      NOT consulted (`LGR-28b4d75-5`). **EXPECTED RED until task 8.2.**
#   2. `TestAliasRefusalPayloadAsItAnswersToday`
#      Oracle: CURRENT OUTPUT — the exact opposite rule. Green today;
#      **amended at task 8.2**, which is the payload change becoming
#      visible in a diff rather than being inferred.
#   3. `TestSparkEntryPointIsNotAModuleAlias`
#      Green before AND after T6, and load-bearing for `bugfix.md` 1.14.
#   4. `TestBareAliasInNonCallPositionKeepsRefusing`
#      Green before AND after T6. `_UNSUPPORTED_NAMES` keeps all six.
#
# Classes 1 and 2 describe the same three shapes and are MEANT to
# disagree until T6 lands. Do not reconcile them; do not merge either
# into task 1.1's classes above.
#
# Task 1.1's `TestAliasRefusalNeverEntersPassthroughCall` already holds
# the today-answer for `F.trim(F.col("b"))` and `F.lit("x")`, so class 2
# covers only the shapes task 1.2 introduces. No shape carries two
# competing pins under one rule.
# ══════════════════════════════════════════════════════════════════════


class P41AliasExpectation(NamedTuple):
    """One shape whose refusal payload `bugfix.md` 2.6 constrains."""

    case_id: str
    source: str
    #: The construct 2.6 requires the refusal to name — the function that owns
    #: the operation, never the module alias it was reached through.
    owner: str


# `bugfix.md` 2.6: a transform reaching a function through the
# `pyspark.sql.functions as F` alias SHALL NOT report `F` as the construct;
# either support the alias, or "continue to refuse but name the aliased
# function rather than the alias". `initcap` is in `_UNSUPPORTED_FUNCTIONS`
# (`:407`) and p41 deliberately does not implement it ("Out of scope"), so for
# these three shapes 2.6 leaves exactly one permitted answer: refuse, naming
# `initcap`.
#
# `initcap` is chosen rather than an arbitrary unsupported name because it is
# the measured instance — 6 of 20 refusals, the single largest refused
# construct — and because naming it is what lets it reach
# `classify_conversion_refusal` as a real construct, which is the whole payoff
# `design.md` §5.4 claims for D2b and the reason 2.5's aggregation stops
# undercounting.
P41_ALIASED_UNSUPPORTED_SHAPES: list[P41AliasExpectation] = [
    P41AliasExpectation(
        case_id="expect-F-initcap-bare",
        source='x = F.initcap(col("b"))',
        owner="initcap",
    ),
    # The production shape: 1.13 records that `transform_generation.j2:80`
    # instructs the model `title-case: initcap(col("f"))`, and a `withColumn`
    # assignment is how that arrives. This is the case whose refusal an
    # operator actually reads in the refused-construct table (2.5).
    P41AliasExpectation(
        case_id="expect-F-initcap-in-with-column",
        source='df_transformed = df.withColumn("name_clean", F.initcap(col("name")))',
        owner="initcap",
    ),
    # Both the outer function and its argument aliased. `col` has a rule, so
    # the only construct without one is still `initcap`.
    P41AliasExpectation(
        case_id="expect-F-initcap-argument-also-aliased",
        source='x = F.initcap(F.col("b"))',
        owner="initcap",
    ),
    # `functions` is the same question wearing a different name (`bugfix.md`
    # Q3), and `design.md` §5.4 puts both in one predicate: `{"F",
    # "functions"}`. Whichever arm 2.6 is answered with, `functions` is not an
    # acceptable construct name.
    P41AliasExpectation(
        case_id="expect-functions-initcap-in-with-column",
        source='df_transformed = df.withColumn("name_clean", functions.initcap(col("name")))',
        owner="initcap",
    ),
]

#: The two names `bugfix.md`'s Property 3 forbids a refusal from ever carrying
#: for an aliased call, and the two this converter answers today.
P41_FORBIDDEN_ALIAS_CONSTRUCTS = ("F", "functions")


class TestAliasedFunctionRefusalNamesTheFunction:
    """Task 1.2 — EXPECTATION. Oracle: `bugfix.md` 2.6 SEMANTICS.

    **Validates: Requirements 1.8, 2.6, Property 3**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **RED**. Do NOT fix the test, and do NOT touch the
    converter to make it pass. The red IS this task's deliverable.
    ══════════════════════════════════════════════════════════════════

    Every expectation here is derived from `bugfix.md` 2.6 and Property 3.
    **Current converter output was not consulted for any of them**
    (`LGR-28b4d75-5`, template / ratified: a test written from the producer's
    output cannot detect the producer drifting). What the converter answers
    today is recorded separately and deliberately, one class down, under the
    opposite rule — so a reviewer at task 8.2 sees the payload move rather
    than inferring it.

    Closed by **task 8.2** (D2b — the shared `_aliased_function` predicate
    consulted by `_occurrences` AND `_call`). These assertions are written to
    go green there **without amendment**: they constrain only the construct
    name and the carrier fields `bugfix.md` 3.6 freezes, never a mechanism.
    They are deliberately NOT marked `xfail` — the plan wants an observed red
    at task 1.4 and an observed green at 8.2.

    Why `_occurrences` and not just the call path: `convert`'s pre-scan
    (`:1385-1387`) refuses on the first unsupported occurrence **before**
    `translator.statement()` runs, so a normalisation placed only in `_call`
    would never execute for these shapes (`design.md` §12 item 2, re-confirmed
    by task 1.1's `TestAliasRefusalNeverEntersPassthroughCall`). That is why
    these are stated over `PySpark2PandasConverter.convert` — the customer's
    entry point — and not over a translator internal.

    Nothing here executes pandas (`bugfix.md` 3.14), so a green is evidence
    about the **translation**, not the execution.
    """

    @pytest.mark.parametrize("case", P41_ALIASED_UNSUPPORTED_SHAPES, ids=lambda case: case.case_id)
    def test_refusal_names_the_aliased_function_not_the_alias(self, case: P41AliasExpectation) -> None:
        """2.6: the construct named is the function, never the module alias."""
        with pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(case.source)
        exc = caught.value
        assert exc.construct not in P41_FORBIDDEN_ALIAS_CONSTRUCTS, (
            f"{case.source!r} refused naming {exc.construct!r} — 2.6 forbids reporting the module "
            f"alias as the construct; it must name {case.owner!r}, which owns the operation."
        )
        assert exc.construct == case.owner
        # `bugfix.md` 3.6: D2b changes WHICH construct is named, never the
        # carrier. Asserted here so a fix that renames the construct by
        # reshaping the payload fails instead of passing.
        assert exc.line == case.source
        assert exc.line_number == 1
        assert exc.reason is None
        assert str(exc) == f"unconvertible PySpark construct '{case.owner}' on line 1: {case.source}"

    def test_aliased_supported_function_converts_like_the_unaliased_spelling(self) -> None:
        """2.6: `F.trim(F.col("b"))` converts — `trim` and `col` both have rules.

        The oracle is 2.6's own equivalence claim: supporting the alias is
        *"a spelling fix and adds no expressiveness"* — rewrite `F.<name>(...)`
        to `<name>(...)` and *"apply the existing vocabulary unchanged"*. So the
        aliased spelling must emit exactly what the unaliased spelling emits.

        This is NOT the aliased shape being its own oracle: the aliased shape's
        current output is a refusal, and the unaliased spelling's emission is
        independently pinned by `CONVERSION_BASELINE` and by
        `test_literal_receiver_composition.py`. What is asserted is the
        equivalence 2.6 states, plus `trim`'s documented rule surviving into
        the emission and no residual alias text.
        """
        aliased = 'x = F.trim(F.col("b"))'
        unaliased = 'x = trim(col("b"))'

        expected = PySpark2PandasConverter.convert(unaliased)
        actual = PySpark2PandasConverter.convert(aliased)

        assert actual["code"] == expected["code"]
        assert actual["conversion_report"] == expected["conversion_report"]
        body = actual["code"][len(P41_PANDAS_HEADER) :]
        assert ".str.strip()" in body, body
        assert "F." not in body, f"the alias survived into the emitted pandas: {body!r}"

    def test_functions_alias_either_converts_or_refuses_naming_trim(self) -> None:
        """2.6 permits either arm for `functions.trim(col("b"))` — but not `functions`.

        2.6 offers a genuine choice: support the alias, or refuse naming the
        aliased function. `design.md` §5.4 picks support and puts `functions`
        in the same predicate as `F`, so this is expected to convert after T6;
        the assertion admits the refusing arm too, because 2.6 does, and
        because a test that forced one arm would be asserting the design
        rather than the requirement.

        What neither arm permits is naming the alias, which is what makes this
        red today.
        """
        source = 'x = functions.trim(col("b"))'
        try:
            actual = PySpark2PandasConverter.convert(source)
        except ConversionError as exc:
            assert exc.construct not in P41_FORBIDDEN_ALIAS_CONSTRUCTS, (
                f"{source!r} refused naming {exc.construct!r}; 2.6 allows a refusal here only if it "
                "names 'trim', the function that owns the operation."
            )
            assert exc.construct == "trim"
            return
        expected = PySpark2PandasConverter.convert('x = trim(col("b"))')
        assert actual["code"] == expected["code"]
        assert actual["conversion_report"] == expected["conversion_report"]


class P41AliasTodayAnswer(NamedTuple):
    """The refusal payload the converter answers TODAY for one aliased shape.

    AMENDED AT TASK 8.2: gained `answered_before_8_2`, so each pin carries the
    answer it moved **from** as well as the answer it moved **to**. That is
    `LGR-60c4985-6`'s "amend with the reason at the assertion" made
    machine-checkable — `test_every_pin_here_moved_off_a_module_alias` asserts
    the pin actually moved and that what it moved off was a module alias, so a
    silent revert to `F` / `functions` fails here.
    """

    case_id: str
    source: str
    construct: str
    #: What this shape answered before task 8.2 landed. Always a module alias.
    answered_before_8_2: str


# ══════════════════════════════════════════════════════════════════════
# TASK 1.2 — THE TODAY-ANSWER PIN. ORACLE RULE: **CURRENT OUTPUT IS THE
# ORACLE** — the exact opposite of the class above, which is why the two
# are separate and separately labelled (`design.md` §7's T0 instruction).
#
# Transcribed by running `PySpark2PandasConverter.convert` against the
# working tree at HEAD `4dd9b42`. Not derived from any clause.
#
# ONE CORRECTION TO `tasks.md` 1.2, FOUND BY RUNNING IT: the task text
# says the converter answers "`F` for the first two". It does not.
# `functions.<fn>(...)` answers **`functions`**, not `F` — `functions` is
# its own member of `_UNSUPPORTED_NAMES` (`:434`) and the pre-scan names
# whichever unsupported `Name` it meets first. The statement 2.6 makes is
# unaffected (both answers name a module alias rather than the function),
# and so is the red; only the recorded today-answer differs.
#
# ══════════════════════════════════════════════════════════════════════
# AMENDED AT TASK 8.2, IN PLACE, NOT DELETED — `LGR-60c4985-6` and
# `LGR-5906ec6-2` (both template / ratified).
#
# REASON, carried at every amended entry and assertion below:
# *p41 2.6 — the refusal names the function that owns the operation, not
# the module alias it was reached through.*
#
# So "TODAY" now means **after 8.2**, and each entry records both answers:
# `construct` is what it answers now, `answered_before_8_2` is what it
# answered before. Four of the five moved from a module alias to
# `initcap` — which is the payoff, because `initcap` reaches
# `classify_conversion_refusal` as a construct an operator can act on
# where `F` / `functions` could not (1.7, 2.5).
#
# ONE ENTRY LEFT THIS LIST, and it did not vanish: `today-functions-trim`
# (`x = functions.trim(col("b"))`) now **converts**, so it cannot carry a
# refusal-payload pin at all. It moved to
# `P41_ALIAS_SPELLINGS_THAT_NOW_CONVERT` below, where it is asserted to
# emit exactly what the unaliased spelling emits — the amendment the class
# docstring provided for ("or the case is deleted if that spelling
# converts"), done as a move so the reversal is still in the file.
# ══════════════════════════════════════════════════════════════════════

P41_ALIAS_TODAY_ANSWERS: list[P41AliasTodayAnswer] = [
    # AMENDED AT 8.2: `F` -> `initcap`. Reason: 2.6, above.
    P41AliasTodayAnswer(
        case_id="today-F-initcap-bare",
        source='x = F.initcap(col("b"))',
        construct="initcap",
        answered_before_8_2="F",
    ),
    P41AliasTodayAnswer(
        case_id="today-F-initcap-in-with-column",
        source='df_transformed = df.withColumn("name_clean", F.initcap(col("name")))',
        construct="initcap",
        answered_before_8_2="F",
    ),
    P41AliasTodayAnswer(
        case_id="today-F-initcap-argument-also-aliased",
        source='x = F.initcap(F.col("b"))',
        construct="initcap",
        answered_before_8_2="F",
    ),
    # AMENDED AT 8.2: `functions` -> `initcap`. The `functions` spelling
    # resolves through the SAME predicate as `F` (`design.md` §5.4 puts both in
    # `_FUNCTION_MODULE_ALIASES`), so the undercount 1.7 measured is closed
    # under both alias names and not just one.
    P41AliasTodayAnswer(
        case_id="today-functions-initcap-in-with-column",
        source='df_transformed = df.withColumn("name_clean", functions.initcap(col("name")))',
        construct="initcap",
        answered_before_8_2="functions",
    ),
]

#: MOVED HERE AT TASK 8.2 from `P41_ALIAS_TODAY_ANSWERS`, with the reason:
#: *p41 2.6 — the refusal names the function that owns the operation, not the
#: module alias it was reached through* — and for a function that HAS a rule
#: (`trim`) the consequence is that the shape converts rather than refusing.
#: Kept in the file rather than deleted so the reversal stays legible
#: (`LGR-60c4985-6`), and asserted against the unaliased spelling so it is a
#: check and not a comment.
P41_ALIAS_SPELLINGS_THAT_NOW_CONVERT = [
    # (aliased, unaliased, what the aliased spelling answered before 8.2)
    ('x = functions.trim(col("b"))', 'x = trim(col("b"))', "functions"),
]


class TestAliasRefusalPayloadAsItAnswersToday:
    """Task 1.2 — CHARACTERIZATION. Oracle: CURRENT OUTPUT.

    **Validates: Requirements 1.8, 2.5, 2.6, Property 3**

    The other half of task 1.2's statement, kept in its own class so the two
    stay legible side by side: what the converter answers **today** for the
    shapes 1.2 introduces. `bugfix.md` 1.8 reproduced `F` for `F.trim(...)`
    and `F.lit(...)`; these are the aliased shapes p41's D2b payoff is stated
    over, plus the `functions` spelling.

    ══════════════════════════════════════════════════════════════════
    AMENDED AT TASK 8.2, IN PLACE, WITH THE REASON — NOT DELETED. The red
    that preceded the amendment is the pin moving legibly, not a
    regression: it is the same shape task 9.2 → 9.3 takes for D4's pins.
    "TODAY" in this class's name now means **after 8.2**; every entry
    records the answer it moved off in `answered_before_8_2`.
    ══════════════════════════════════════════════════════════════════

    `LGR-60c4985-6` and `LGR-5906ec6-2` (both template / ratified): a
    deliberately-held assertion is amended **in place, with its reason at the
    assertion**, so the reversal stays legible. Done at 8.2, and the reason,
    carried at every amended entry and assertion here and in the table above:
    *p41 2.6 — the refusal names the function that owns the operation, not the
    module alias it was reached through.*

    What moved, concretely — the diff a reviewer should be able to read
    without running anything:

    - `F.initcap(…)` ×3 : `F` → **`initcap`** (still a refusal; `initcap` is
      in `_UNSUPPORTED_FUNCTIONS` and p41 deliberately does not implement it).
    - `functions.initcap(…)` : `functions` → **`initcap`**.
    - `functions.trim(…)` : refused `functions` → **converts**, so it left this
      table for `P41_ALIAS_SPELLINGS_THAT_NOW_CONVERT`.

    The payoff is in the first four: each now reaches
    `classify_conversion_refusal` as `initcap`, a construct an operator can act
    on, where `F` / `functions` were not (1.7, 2.5). `initcap` is separately the
    largest measured refused construct at 6 of 20, and D4 (task 9) is what makes
    the reason it then reads truthful.

    Do NOT reconcile these with `TestAliasedFunctionRefusalNamesTheFunction`.
    They described the same shapes under opposite rules and disagreed by
    design until T6; from 8.2 they agree, which is what closing D2b means.
    """

    @pytest.mark.parametrize("pin", P41_ALIAS_TODAY_ANSWERS, ids=lambda pin: pin.case_id)
    def test_refusal_payload_is_byte_exact(self, pin: P41AliasTodayAnswer) -> None:
        """AMENDED AT 8.2: `pin.construct` moved from a module alias to `initcap`.

        Reason: *p41 2.6 — the refusal names the function that owns the
        operation, not the module alias it was reached through.* The assertion
        body is unchanged, which is the point: `bugfix.md` 3.6's carrier — three
        positional fields, `reason=None`, and `str(exc)`'s rendering — is
        untouched by D2b. Only the value of `construct` moved.
        """
        with p41_passthrough_spy() as entries, pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(pin.source)
        exc = caught.value
        assert exc.construct == pin.construct
        assert exc.line == pin.source
        assert exc.line_number == 1
        assert exc.reason is None
        assert str(exc) == f"unconvertible PySpark construct '{pin.construct}' on line 1: {pin.source}"
        # The refusal happens in `convert`'s pre-scan, so the translator — and
        # therefore `_passthrough_call` — never runs. Task 1.1's classes argue
        # this at length; recorded here too because it is the reason task 8.2
        # had to reach `_occurrences` and not only `_call`. Unchanged by the
        # amendment: 8.2 made the pre-scan name the resolved FUNCTION, it did
        # not move where the refusal happens.
        assert entries == []

    def test_the_alias_is_the_answer_for_every_shape_pinned_here(self) -> None:
        """Stated once as a set, so the scale of the change is one assertion.

        AMENDED AT 8.2: the expected set moved from `{"F", "functions"}` to
        `{"initcap"}`. Reason: *p41 2.6 — the refusal names the function that
        owns the operation, not the module alias it was reached through.*

        Before 8.2 this read: four aliased shapes, four refusals, and not one
        of them naming a function — `bugfix.md` 1.7's instrument problem, since
        the refused-construct table an operator reads counted them under `F` and
        `functions`, which are not constructs anyone can act on. It now reads as
        the closure of that: every one of them is counted under `initcap`, which
        is (2.5) the aggregation being sound.
        """
        answers = set()
        for pin in P41_ALIAS_TODAY_ANSWERS:
            with pytest.raises(ConversionError) as caught:
                PySpark2PandasConverter.convert(pin.source)
            answers.add(caught.value.construct)
        assert answers == {"initcap"}, (
            f"aliased refusals were attributed to {sorted(answers)}; after 8.2 every one of them must "
            "be counted under the function that owns the operation (2.5)."
        )
        assert not answers & set(P41_FORBIDDEN_ALIAS_CONSTRUCTS)

    def test_every_pin_here_moved_off_a_module_alias(self) -> None:
        """ADDED AT 8.2 — the reversal, asserted rather than only commented.

        `LGR-60c4985-6` requires an amended assertion to carry its reason at
        the assertion. This makes the *fact* of the amendment machine-checkable
        too: every pin in this table records the module alias it answered before
        8.2, that recorded answer is one of the two aliases `bugfix.md`'s
        Property 3 forbids, and the answer it moved to is not. A silent revert
        of `_occurrences`' or `_call`'s alias resolution fails
        `test_refusal_payload_is_byte_exact`; a silent revert of *this table*
        back to the alias answers fails here.
        """
        for pin in P41_ALIAS_TODAY_ANSWERS:
            assert pin.answered_before_8_2 in P41_FORBIDDEN_ALIAS_CONSTRUCTS, pin.case_id
            assert pin.construct not in P41_FORBIDDEN_ALIAS_CONSTRUCTS, pin.case_id
            assert pin.construct != pin.answered_before_8_2, pin.case_id

    @pytest.mark.parametrize(
        ("aliased", "unaliased", "answered_before_8_2"),
        P41_ALIAS_SPELLINGS_THAT_NOW_CONVERT,
        ids=["moved-from-today-functions-trim"],
    )
    def test_the_spelling_that_stopped_refusing_converts_like_the_unaliased_one(
        self, aliased: str, unaliased: str, answered_before_8_2: str
    ) -> None:
        """AMENDED AT 8.2: this shape left the refusal table because it converts.

        Reason: *p41 2.6 — the refusal names the function that owns the
        operation, not the module alias it was reached through* — and for a
        function that already has a rule (`trim`) the honest consequence is a
        conversion, not a renamed refusal. 2.6 states that arm explicitly:
        supporting the alias is "a spelling fix and adds no expressiveness".

        Kept as an assertion rather than a comment so the move is checked. The
        oracle is the UNALIASED spelling, which `CONVERSION_BASELINE` pins
        independently — the aliased shape is not its own oracle.
        """
        assert answered_before_8_2 in P41_FORBIDDEN_ALIAS_CONSTRUCTS
        actual = PySpark2PandasConverter.convert(aliased)
        expected = PySpark2PandasConverter.convert(unaliased)
        assert actual["code"] == expected["code"]
        assert actual["conversion_report"] == expected["conversion_report"]
        body = actual["code"][len(P41_PANDAS_HEADER) :]
        assert ".str.strip()" in body, body
        assert "functions." not in body, f"the alias survived into the emitted pandas: {body!r}"


class TestSparkEntryPointIsNotAModuleAlias:
    """Task 1.2 — durable. Green BEFORE and AFTER T6. Do not let it move.

    **Validates: Requirements 1.8, 2.6, Property 3**

    `spark.conf.set("k", "v")` refuses naming **`spark`**, and must keep doing
    so after task 8.2's alias normalisation lands. `bugfix.md` 1.14's whole
    argument depends on it: the origin of the 3 measured `set` refusals is the
    Python builtin `set`, **not** the scaffolds' `spark.conf.set(...)`, which
    refuses under `spark`. If the alias predicate fired here, `spark.conf.set`
    would start answering `set` and 1.14's evidence would silently invert.

    `design.md` §5.4 states the mechanism, and the mechanism is asserted
    directly below rather than trusted: `_aliased_function` fires only when a
    `Call`'s `func` is an `Attribute` whose `.value` is a `Name` in
    `{"F", "functions"}`. In `spark.conf.set(...)` the `.value` is itself an
    `Attribute` (`spark.conf`), so the predicate cannot fire — a structural
    fact about the AST, independent of any implementation.
    """

    SCAFFOLD_LINE = 'spark.conf.set("spark.sql.catalog.s3tablesbucket", "org.apache.iceberg.spark.SparkCatalog")'

    @pytest.mark.parametrize(
        "source",
        [
            'spark.conf.set("k", "v")',
            SCAFFOLD_LINE,
        ],
        ids=["minimal", "scaffold-jdbc-source-line"],
    )
    def test_refuses_naming_spark(self, source: str) -> None:
        with pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(source)
        exc = caught.value
        assert exc.construct == "spark"
        assert exc.line == source
        assert exc.line_number == 1

    def test_the_receiver_is_an_attribute_so_no_alias_predicate_can_fire(self) -> None:
        """The structural reason, asserted rather than assumed (`design.md` §5.4)."""
        spark_call = ast.parse('spark.conf.set("k", "v")').body[0].value
        assert isinstance(spark_call, ast.Call)
        assert isinstance(spark_call.func, ast.Attribute)
        assert isinstance(spark_call.func.value, ast.Attribute)
        assert not isinstance(spark_call.func.value, ast.Name)

        # Contrast, so the two shapes are distinguishable by the predicate the
        # design specifies and not by a name list: an aliased function call
        # DOES have a bare `Name` receiver.
        aliased_call = ast.parse('F.initcap(col("b"))', mode="eval").body
        assert isinstance(aliased_call, ast.Call)
        assert isinstance(aliased_call.func, ast.Attribute)
        assert isinstance(aliased_call.func.value, ast.Name)
        assert aliased_call.func.value.id == "F"


class TestBareAliasInNonCallPositionKeepsRefusing:
    """Task 1.2 — durable. Green BEFORE and AFTER T6. Do not let it move.

    **Validates: Requirements 1.8, 2.6, Property 3**

    `design.md` §10: **do NOT remove `F`, `functions` or `StructField` from
    `_UNSUPPORTED_NAMES`.** D2b normalises an aliased *call*; a bare alias in
    any other position is still a Spark entry point reference with no pandas
    meaning, and must still refuse. This class is the fence that makes the
    difference between "normalise a call" and "make the alias legal" fail
    loudly rather than pass quietly.

    `bugfix.md` Q3 enumerates four members of the set; `design.md` §12 item 5
    corrects that to **six**, at `:434`. Six confirmed by reading the set
    directly below.
    """

    #: `:434`, six members. `design.md` §12 item 5.
    EXPECTED_MEMBERS = frozenset({"spark", "sc", "sqlContext", "StructField", "F", "functions"})

    def test_unsupported_names_keeps_all_six_members(self) -> None:
        assert _UNSUPPORTED_NAMES == self.EXPECTED_MEMBERS
        # Stated as a count as well, because "six confirmed" is the claim
        # `design.md` §12 item 5 makes against `bugfix.md` Q3's four. A member
        # added or removed here is a finding to review against §10's do-NOT,
        # not a number to update.
        assert len(_UNSUPPORTED_NAMES) == 6

    @pytest.mark.parametrize(
        ("source", "construct"),
        [
            ("x = F", "F"),
            ("x = functions", "functions"),
            ("x = StructField", "StructField"),
            ('df_transformed = df.withColumn("a", F)', "F"),
        ],
        ids=["bare-F", "bare-functions", "bare-StructField", "F-as-a-with-column-value"],
    )
    def test_bare_alias_refuses_naming_itself(self, source: str, construct: str) -> None:
        with pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(source)
        exc = caught.value
        assert exc.construct == construct
        assert exc.line == source
        assert exc.line_number == 1


# ══════════════════════════════════════════════════════════════════════
# TASK 1.3 — D2a: A FAILURE INSIDE `rlike`'s PATTERN NAMES `col`.
#
# **Validates: Requirements 1.6, 1.7, 2.4, 2.5, Property 3**
#
# THREE CLASSES BELOW. THEY DO NOT SHARE AN ORACLE RULE. Read the class
# docstring before changing an assertion in it.
#
#   1. `TestFilterRlikePatternRefusalNamesRlike`
#      Oracle: `bugfix.md` 2.4/2.5 — SEMANTICS. Current converter output
#      is NOT consulted (`LGR-28b4d75-5`). **EXPECTED RED until task 8.1.**
#   2. `TestRlikeRefusalOutsideFilterPositionAlreadyNamesRlike`
#      Oracle: CURRENT OUTPUT — the opposite rule. **GREEN today**, and
#      green after 8.1: it is the evidence that D2a's blast radius is ONE
#      call site (`:1232`) rather than the `rlike` surface.
#   3. `TestSingleQuotedPatternsKeepRefusingThroughT3`
#      Durable. Green before AND after T3 and 8.1. The anti-vacuity guard
#      for the shape choice below.
#
# Classes 1 and 2 are the mixed result `tasks.md` 1.3 asks for — red on
# the `df.filter(...)` shape, green on the control — and the mixture is
# the point: it localises the defect. Do NOT reconcile them, and do NOT
# merge either into task 1.1's or task 1.2's classes above.
#
# ──────────────────────────────────────────────────────────────────────
# WHY EVERY PATTERN BELOW IS SINGLE-QUOTED (`tasks.md` bite 3)
# ──────────────────────────────────────────────────────────────────────
#
# A raw **double**-quoted pattern would make this whole section vacuous
# from T3 onward. Today `df.filter(col("e").rlike(r"@x\.com$"))` refuses
# naming `col`, so it looks like a fine D2a shape — but T3 (task 4.2)
# accepts the `r`/`R` prefix, so from T3 onward that shape **converts**
# and a `pytest.raises(ConversionError)` around it never fires. The test
# would pass for the wrong reason and stop measuring anything.
#
# Single-quoted patterns do not have that problem, because Q1 (`design.md`
# §9) **keeps** the single-quote fence: T3 strips a leading `r`/`R` and
# then applies the SAME double-quote check to the remainder (`:753`), and
# `'…'` fails it either way. So these shapes refuse before T3 and refuse
# after it, and the construct name is the only thing 8.1 moves.
#
# Verified against the working tree rather than reasoned about, by
# simulating T3's `_string` and re-running every shape here:
#
#   BEFORE T3                                      AFTER SIMULATED T3
#   filter  r'@x\.com$'    -> REFUSE 'col'          -> REFUSE 'col'
#   filter   '@x\\.com$'   -> REFUSE 'col'          -> REFUSE 'col'
#   control r'@x\.com$'    -> REFUSE 'rlike'        -> REFUSE 'rlike'
#   filter  r"@x\.com$"    -> REFUSE 'col'          -> CONVERTS   <-- vacuous
#
# and by simulating 8.1 (an optional `construct` on `_string`, `"rlike"`
# passed at `:1232`), which turns every `col` above into `rlike` with the
# carrier fields `bugfix.md` 3.6 freezes unchanged. That is why class 1's
# assertions are written to go green at 8.1 **without amendment**: they
# constrain the construct name and the carrier, never a mechanism.
#
# SOURCE-STRING CONVENTION: every PySpark shape here is a raw
# triple-double-quoted Python string, because the PySpark text carries a
# double quote (the column name), a single quote (the pattern) and a
# backslash at once. `r"""…"""` is the one spelling that holds all three
# without escaping, so what is written is exactly what the converter
# parses.
#
# pandas is not installed and must not be (`bugfix.md` 3.14). Nothing here
# executes the emitted pandas; every green is evidence about the
# TRANSLATION, not the execution.
# ══════════════════════════════════════════════════════════════════════


class P41RlikeExpectation(NamedTuple):
    """One `rlike` shape whose refusal payload `bugfix.md` 2.4 constrains."""

    case_id: str
    source: str
    #: The construct 2.4 requires: the one that OWNS the operation the proxy
    #: could not perform. For a failure inside `rlike`'s pattern that is
    #: `rlike`, stated verbatim by 2.4 ("a refusal arising inside `rlike`'s
    #: pattern SHALL name `rlike`").
    owner: str


#: The construct the converter names today for every shape in
#: `P41_FILTER_RLIKE_SINGLE_QUOTED_SHAPES`, and the one `bugfix.md`'s
#: Property 3 forbids a refusal from ever carrying — `col` is an incidental
#: sub-expression of the predicate, not the operation that failed.
P41_FORBIDDEN_RLIKE_CONSTRUCT = "col"

# `bugfix.md` 1.6 / 2.4 / 2.5, and `design.md` §5.3. The refusal originates
# inside `rlike`'s pattern literal: `_filter_mask` calls
# `_string(predicate.args[0], predicate)` at `:1232`, the quote fence at `:753`
# fails, and `_string` raises `self._refuse_here(owner)` where *owner* is the
# whole `col("email").rlike(...)` call. `_refuse_here` (`:627-641`) takes
# `_occurrences(node)[0]`, sorted by `(lineno, col)`, and `col(` starts left of
# `.rlike` — so `col` wins a race it should not be in.
#
# 2.5 is why this is load-bearing rather than cosmetic: the refused-construct
# table an operator reads counts these under `col`, which nobody can act on,
# and that is the instrument that corrupted p40's own measurement (1.7).
P41_FILTER_RLIKE_SINGLE_QUOTED_SHAPES: list[P41RlikeExpectation] = [
    # `tasks.md` 1.3's first named shape: raw, single-quoted.
    P41RlikeExpectation(
        case_id="expect-filter-rlike-raw-single-quoted",
        source=r"""df_transformed = df.filter(col("email").rlike(r'@x\.com$'))""",
        owner="rlike",
    ),
    # `tasks.md` 1.3's second named shape: non-raw, single-quoted, escaped
    # backslash. Refused by the same fence for one reason instead of two (1.4
    # records that `r'…'` trips both halves at once), so it is the shape that
    # shows the misnaming does not depend on the `r` prefix at all.
    P41RlikeExpectation(
        case_id="expect-filter-rlike-nonraw-single-quoted",
        source=r"""df_transformed = df.filter(col("email").rlike('@x\\.com$'))""",
        owner="rlike",
    ),
    # A production-shaped pattern on a differently-named target, so the
    # expectation is not accidentally tied to one pattern body or to the
    # `df_transformed` name the scaffolds happen to use.
    P41RlikeExpectation(
        case_id="expect-filter-rlike-raw-single-quoted-anchored",
        source=r"""df_filtered = df.filter(col("email").rlike(r'^[a-z]+@x\.com$'))""",
        owner="rlike",
    ),
]


class TestFilterRlikePatternRefusalNamesRlike:
    """Task 1.3 — EXPECTATION. Oracle: `bugfix.md` 2.4/2.5 SEMANTICS.

    **Validates: Requirements 1.6, 1.7, 2.4, 2.5, Property 3**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **RED**. Do NOT fix the test, and do NOT touch the
    converter to make it pass. The red IS this task's deliverable.
    ══════════════════════════════════════════════════════════════════

    2.4, verbatim: *"a refusal arising inside `rlike`'s pattern SHALL name
    `rlike`"*. Every expectation here is derived from that clause and from
    Property 3. **Current converter output was not consulted for any of them**
    (`LGR-28b4d75-5`, template / ratified: a test written from the producer's
    output cannot detect the producer drifting). What the converter answers
    today — `col`, for all three shapes — is recorded one class down under the
    opposite rule, so a reviewer at task 8.1 sees the payload move rather than
    inferring it.

    Closed by **task 8.1** (an optional `construct` on `_string`; `:1232`
    passes `"rlike"`). These assertions go green there **without amendment**,
    because they constrain only the construct name and the carrier fields
    `bugfix.md` 3.6 freezes — never a mechanism, and never `_refuse_here`,
    which `design.md` §10 forbids changing and which 8 other call sites rely
    on (3.5). Deliberately NOT marked `xfail`: the plan wants an observed red
    at task 1.4 and an observed green at 8.1.

    Every pattern is single-quoted for the reason set out in the section
    header above — a raw double-quoted pattern converts from T3 onward and the
    assertion goes vacuous.
    """

    @pytest.mark.parametrize("case", P41_FILTER_RLIKE_SINGLE_QUOTED_SHAPES, ids=lambda case: case.case_id)
    def test_refusal_names_rlike_and_never_col(self, case: P41RlikeExpectation) -> None:
        """2.4: the construct named owns the operation, not an incidental one."""
        with pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(case.source)
        exc = caught.value
        assert exc.construct != P41_FORBIDDEN_RLIKE_CONSTRUCT, (
            f"{case.source!r} refused naming {exc.construct!r} — 2.4 forbids naming an incidental "
            f"sub-expression; the failure is inside {case.owner!r}'s pattern, so {case.owner!r} owns it."
        )
        assert exc.construct == case.owner
        # `bugfix.md` 3.6: D2a changes WHICH construct is named, never the
        # carrier. Asserted so a fix that renames the construct by reshaping
        # the payload fails here instead of passing.
        assert exc.line == case.source
        assert exc.line_number == 1
        assert exc.reason is None
        assert str(exc) == f"unconvertible PySpark construct '{case.owner}' on line 1: {case.source}"

    def test_no_filter_rlike_shape_is_attributed_to_col(self) -> None:
        """2.5 as one assertion: the aggregation over these shapes is sound.

        2.5 is a correctness requirement on the **instrument** — every refusal
        counted under the construct a reader would attribute it to. Stated as a
        set so the scale of the misattribution is one line rather than three,
        which is the form 1.7's measurement problem takes: `rlike` reported 2
        where the true figure was 5.
        """
        answers = set()
        for case in P41_FILTER_RLIKE_SINGLE_QUOTED_SHAPES:
            with pytest.raises(ConversionError) as caught:
                PySpark2PandasConverter.convert(case.source)
            answers.add(caught.value.construct)
        assert answers == {"rlike"}, (
            f"refusals inside `rlike`'s pattern were attributed to {sorted(answers)}; 2.5 requires "
            "every one of them to be counted under `rlike`."
        )


class P41RlikeTodayAnswer(NamedTuple):
    """An `rlike` shape the converter ALREADY names correctly, pinned as such."""

    case_id: str
    source: str
    construct: str
    #: `_string` source-text entries observed for this shape, in order. The
    #: pattern literal is absent from every one of them — that is the evidence
    #: the refusal happens before the literal fence is reached.
    string_segments: tuple[str, ...]


# ══════════════════════════════════════════════════════════════════════
# ORACLE RULE FOR THE CLASS BELOW: **CURRENT OUTPUT IS THE ORACLE** — the
# exact opposite of the class above. Transcribed by running
# `PySpark2PandasConverter.convert` against the working tree at HEAD
# `4dd9b42`, and re-confirmed under a simulated 8.1. Not derived from any
# clause.
#
# This is `design.md` §12 item 1: `bugfix.md` 1.6's reproduction is
# incomplete. The `col` answer needs the `df.filter(...)` wrapper; outside
# filter position `_method_call` refuses `rlike` by name at `:972-974`
# before `_string` is reached at all. So D2a's blast radius is ONE call
# site (`:1232`, the `_filter_mask` path), not the `rlike` surface — which
# is why 8.1 parameterises one site instead of touching `_refuse_here`.
# ══════════════════════════════════════════════════════════════════════

P41_RLIKE_ALREADY_CORRECT_SHAPES: list[P41RlikeTodayAnswer] = [
    # `design.md` §12 item 1's control, and `tasks.md` 1.3's: the bare
    # statement. `_method_call`'s `("isNull", "isNotNull", "rlike")` branch
    # refuses first, so no literal is ever read — `_string` records nothing.
    P41RlikeTodayAnswer(
        case_id="control-bare-statement-raw-single-quoted",
        source=r"""x = col("email").rlike(r'@x\.com$')""",
        construct="rlike",
        string_segments=(),
    ),
    # The same control in the other spelling, so the control is not tied to
    # the `r` prefix either.
    P41RlikeTodayAnswer(
        case_id="control-bare-statement-nonraw-single-quoted",
        source=r"""x = col("email").rlike('@x\\.com$')""",
        construct="rlike",
        string_segments=(),
    ),
    # `design.md` §12 item 1's third reproduction: `withColumn` position also
    # already names `rlike`. `_string` IS entered here — once, for the OUTPUT
    # column name `"m"` — and still never for the pattern, which is why the
    # assertion below is stated over the pattern's absence rather than over an
    # empty list.
    P41RlikeTodayAnswer(
        case_id="control-with-column-raw-single-quoted",
        source=r"""df_transformed = df.withColumn("m", col("a").rlike(r'@x\.com$'))""",
        construct="rlike",
        string_segments=('"m"',),
    ),
]


class TestRlikeRefusalOutsideFilterPositionAlreadyNamesRlike:
    """Task 1.3 — CHARACTERIZATION. Oracle: CURRENT OUTPUT.

    **Validates: Requirements 1.6, 2.4, Property 3**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN**, today and after task 8.1. A red here is a
    finding — the working tree moved, or `design.md` §5.3's one-call-site
    scope is wrong — never a number to adjust.
    ══════════════════════════════════════════════════════════════════

    `tasks.md` 1.3 asks for this control by name, because it is *"the evidence
    that D2a's blast radius is one call site (`:1232`, the `_filter_mask`
    path) and not the `rlike` surface"*. Without it, a reader of the class
    above would reasonably conclude that every `rlike` refusal misnames, and
    8.1 would be scoped to `_refuse_here` — a change `design.md` §10 forbids
    and that 8 other call sites depend on not happening (3.5).

    The mixed result across these two classes is deliberate: **red on the
    `df.filter(...)` shape, green here.** That mixture is what localises the
    defect to one site.

    Confirmed still green under a simulated 8.1, so this class does not become
    a hidden second expected-red window.
    """

    @pytest.mark.parametrize("pin", P41_RLIKE_ALREADY_CORRECT_SHAPES, ids=lambda pin: pin.case_id)
    def test_refusal_payload_is_byte_exact(self, pin: P41RlikeTodayAnswer) -> None:
        with p41_string_spy() as segments, pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(pin.source)
        exc = caught.value
        assert exc.construct == pin.construct
        assert exc.line == pin.source
        assert exc.line_number == 1
        assert exc.reason is None
        assert str(exc) == f"unconvertible PySpark construct '{pin.construct}' on line 1: {pin.source}"
        assert tuple(segments) == pin.string_segments

    @pytest.mark.parametrize("pin", P41_RLIKE_ALREADY_CORRECT_SHAPES, ids=lambda pin: pin.case_id)
    def test_the_pattern_literal_is_never_handed_to_the_string_fence(self, pin: P41RlikeTodayAnswer) -> None:
        """The mechanism, observed rather than inspected.

        `_method_call` reaches its `("isNull", "isNotNull", "rlike")` branch
        (`:972-974`) and raises `_refuse(func, attr)` before any literal in the
        predicate is read — it does not translate the receiver first, unlike
        the `alias` branch above it. So the pattern's source text appears in no
        `_string` entry, and `_string`'s fence — the thing D2a's misnaming
        comes out of — is simply not on this path.

        Stated over the pattern's ABSENCE rather than over an empty entry list,
        because the `withColumn` shape legitimately hands `_string` its output
        column name first.
        """
        _translator, pattern, _owner = p41_rlike_pattern_node(pin.source)
        pattern_text = ast.get_source_segment(pin.source, pattern)
        assert pattern_text is not None

        with p41_string_spy() as segments, pytest.raises(ConversionError):
            PySpark2PandasConverter.convert(pin.source)

        assert pattern_text not in segments, (
            f"{pin.source!r} reached `_string` with its pattern {pattern_text!r}; the control's whole "
            "point is that it refuses at `_method_call:972-974` BEFORE the literal fence, which is "
            "what confines D2a to `:1232`."
        )

    def test_the_filter_wrapper_is_what_puts_the_pattern_through_the_fence(self) -> None:
        """The two paths, side by side, as one assertion (`design.md` §12 item 1).

        The same predicate refuses at two different places depending on
        position, and the difference is exactly whether `_string` sees the
        pattern. This is the localisation stated positively: `:1232` is the
        only site that hands an `rlike` pattern to the fence, so it is the only
        site 8.1 needs to parameterise.

        Oracle: current output, for both halves. Nothing here says what the
        filter shape's construct name *ought* to be — that is the class above.
        """
        bare = r"""x = col("email").rlike(r'@x\.com$')"""
        filtered = r"""df_transformed = df.filter(col("email").rlike(r'@x\.com$'))"""
        pattern_text = r"""r'@x\.com$'"""

        with p41_string_spy() as bare_segments, pytest.raises(ConversionError):
            PySpark2PandasConverter.convert(bare)
        with p41_string_spy() as filtered_segments, pytest.raises(ConversionError):
            PySpark2PandasConverter.convert(filtered)

        assert pattern_text not in bare_segments
        assert pattern_text in filtered_segments
        # And the filter path reads the predicate's COLUMN name first, through
        # `_column_name` at `:1222` — recorded because it is the other
        # `_string` call on this path and a future reader should not mistake it
        # for the pattern.
        assert filtered_segments == ['"email"', pattern_text]


class TestSingleQuotedPatternsKeepRefusingThroughT3:
    """Task 1.3 — durable. Green BEFORE and AFTER T3 and 8.1.

    **Validates: Requirements 1.4, 1.6, 2.3, 2.4**

    The anti-vacuity guard for this section's shape choice, and the
    machine-checkable form of `tasks.md` bite 3. It asserts the *reason* every
    pattern above keeps refusing across T3, without asserting which construct
    the refusal names — so it is orthogonal to both classes above and cannot
    go red at 8.1.

    Q1 (`design.md` §9) accepts the `r`/`R` prefix and **keeps** the
    single-quote fence, whose stated rationale is at `_string`'s docstring
    (`:744-748`): the generation prompt emits double quotes, and a block that
    does not is not a block this checker has ever verified. 2.3 requires that
    fence to survive D1's fix as a decision rather than as a side effect.

    Both halves are asserted structurally, so neither depends on the fence's
    current implementation:

    1. The pattern's source text, with any leading `r`/`R` stripped, does not
       begin with `"`. T3's predicate is *strip the prefix, then apply the same
       double-quote check to the remainder* (`:753`, task 4.2), so this is the
       property that makes the refusal survive T3.
    2. `_string` refuses the pattern literal when called directly. Called with
       two positional arguments, which stays valid at 8.1 because `construct`
       is added optional and last.
    """

    ALL_SINGLE_QUOTED_SOURCES = [case.source for case in P41_FILTER_RLIKE_SINGLE_QUOTED_SHAPES] + [
        pin.source for pin in P41_RLIKE_ALREADY_CORRECT_SHAPES
    ]

    @pytest.mark.parametrize("source", ALL_SINGLE_QUOTED_SOURCES)
    def test_pattern_fails_the_quote_fence_with_or_without_the_prefix_rule(self, source: str) -> None:
        """(1) Structural: single-quoted, so T3's kept fence still rejects it."""
        _translator, pattern, _owner = p41_rlike_pattern_node(source)
        text = ast.get_source_segment(source, pattern)
        assert text is not None
        # T3 strips a leading `r`/`R` and re-applies the double-quote check.
        without_prefix = text[1:] if text[:1] in ("r", "R") else text
        assert not without_prefix.startswith('"'), (
            f"{text!r} begins with a double quote after prefix-stripping, so T3 would ACCEPT it and "
            "every assertion in this section would go vacuous. Use a single-quoted pattern "
            "(`tasks.md` bite 3)."
        )
        assert without_prefix.startswith("'")

    @pytest.mark.parametrize("source", ALL_SINGLE_QUOTED_SOURCES)
    def test_the_string_fence_refuses_the_pattern_when_called_directly(self, source: str) -> None:
        """(2) Behavioural: the fence itself refuses, whatever the payload says.

        Deliberately silent about the construct named. `_string` called without
        a `construct` keeps using `_refuse_here` even after 8.1 — that is the
        fallback `design.md` §5.3 specifies — so asserting a name here would
        pin the wrong thing.
        """
        translator, pattern, owner = p41_rlike_pattern_node(source)
        with pytest.raises(_Unconvertible):
            translator._string(pattern, owner)


class P41OwnedLiteralExpectation(NamedTuple):
    """A `_string` failure site whose owning construct the payload must name."""

    case_id: str
    source: str
    #: `design.md` §5.1's row for the site, so the case list is auditable
    #: against the table rather than invented.
    site: str
    #: The construct 2.4 requires: the one that OWNS the operation. The literal
    #: is `alias`'s new name or `cast`'s target key, so `alias` / `cast` own it.
    owner: str


#: The wrong answer, shared with `P41_FORBIDDEN_RLIKE_CONSTRUCT` above: `col`
#: is the receiver of the operation that failed, never the operation.
P41_FORBIDDEN_OWNED_LITERAL_CONSTRUCT = "col"

# ══════════════════════════════════════════════════════════════════════
# ORACLE RULE FOR THE TWO CLASSES BELOW: **`bugfix.md` 2.4 SEMANTICS**, the
# same rule as `TestFilterRlikePatternRefusalNamesRlike`. Current converter
# output was NOT consulted for any expectation — it is recorded in
# `observations.md`'s task 2.5 section instead, and it is `col` for all of
# them.
# ══════════════════════════════════════════════════════════════════════
#
# WHY THESE TWO SITES ARE HERE, when `tasks.md` 8.1 names three (`:1232`,
# `:845`, `:846`). `observations.md`'s task 2.5 section recorded, as a
# cross-cutting finding, that 4 of the 18 D1 reds ALSO misattribute the
# construct — and that two of the four are at `:961` (`alias`) and `:985`
# (`cast`), outside 8.1's stated site list. It further verified that the
# misattribution **survives task 4.3**, because it occurs for a spelling the
# kept quote fence still refuses:
#
#     x = col("a").alias('plain')   -> REFUSED construct='col'
#     x = col("a").cast('int')      -> REFUSED construct='col'
#
# 2.4 states the general principle FIRST — *"the construct named SHALL be the
# construct that owns the operation the proxy cannot perform, not an incidental
# sub-expression"* — and only then names `rlike` and `regexp_replace` with a
# "Specifically". These two sites are the same defect, at two more sites, under
# the same clause.
#
# THE INTERNAL INCONSISTENCY THAT DECIDES IT, and the reason this is not scope
# creep. Both sites ALREADY name themselves for their other refusal paths
# (`_refuse(func, "alias")` and `_refuse(func, "cast")`), so leaving the
# `_string` path on `_refuse_here` makes ONE operation answer TWO different
# constructs depending on which of its own guards fired. Verified by direct
# call, and pinned in `TestTheseSitesAlreadyHoldTheirOwnName` below:
#
#     col("a").cast("nonsense")  -> 'cast'   # the site names itself
#     col("a").cast('int')       -> 'col'    # the site defers to leftmost
#     col("a").alias(alias_var)  -> 'alias'  # the site names itself
#     col("a").alias('plain')    -> 'col'    # the site defers to leftmost
#
# So the fix is to pass a name the site is already holding — no new mechanism,
# and `_refuse_here` untouched for its other 8 call sites (3.5, `design.md`
# §10). `LGR-86698d4-2` (template / ratified) is the precedent it follows:
# *keep the specific clause, stop the blanket clause absorbing everything
# else.*
#
# The alternative — leave these two and constrain task 8.3's generator away
# from them — was rejected: 8.3's property is specified to assert the named
# construct *"owns the operation and is never `col`, `F` or `functions`"*, and
# narrowing a property to dodge a known true counterexample is the vacuity
# failure mode `tasks.md` 10.1 exists to guard against.

P41_OWNED_LITERAL_SHAPES: list[P41OwnedLiteralExpectation] = [
    # ── `:961` — `alias`'s new name. Single-quoted, so the kept quote fence
    # (Q1) refuses it before AND after T3; a raw double-quoted spelling now
    # converts and the assertion would go vacuous (`tasks.md` bite 3). ──
    P41OwnedLiteralExpectation(
        case_id="expect-alias-single-quoted-names-alias",
        source="""x = col("a").alias('plain')""",
        site=":961",
        owner="alias",
    ),
    # The same site in `withColumn` position, so the expectation is not tied to
    # the bare-statement shape.
    P41OwnedLiteralExpectation(
        case_id="expect-alias-single-quoted-in-with-column-names-alias",
        source="""df_transformed = df.withColumn("out", col("a").alias('plain'))""",
        site=":961",
        owner="alias",
    ),
    # ── `:985` — `_cast`'s string target key. `int` is a REAL `_CAST_TARGETS`
    # key, so the refusal can only be the quote fence and never the
    # unrecognised-target branch, which already names `cast` on its own. ──
    P41OwnedLiteralExpectation(
        case_id="expect-cast-single-quoted-names-cast",
        source="""x = col("a").cast('int')""",
        site=":985",
        owner="cast",
    ),
    P41OwnedLiteralExpectation(
        case_id="expect-cast-single-quoted-in-with-column-names-cast",
        source="""df_transformed = df.withColumn("out", col("a").cast('int'))""",
        site=":985",
        owner="cast",
    ),
]


class TestOwnedLiteralRefusalNamesTheOwningConstruct:
    """Task 8.1, extended scope — EXPECTATION. Oracle: `bugfix.md` 2.4 SEMANTICS.

    **Validates: Requirements 1.6, 1.7, 2.4, 2.5, Property 3**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **RED before task 8.1's production change, GREEN
    after.** The red is the evidence the defect exists at these two sites;
    `observations.md`'s task 2.5 section reproduced it independently.
    ══════════════════════════════════════════════════════════════════

    Same clause and same mechanism as
    `TestFilterRlikePatternRefusalNamesRlike`, two sites further along
    `design.md` §5.1's table. The carrier assertions are copied deliberately:
    `bugfix.md` 3.6 freezes `ConversionError`'s three positional fields and
    `str(exc)`, so a "fix" that renamed the construct by reshaping the payload
    fails here rather than passing.
    """

    @pytest.mark.parametrize("case", P41_OWNED_LITERAL_SHAPES, ids=lambda case: case.case_id)
    def test_refusal_names_the_owner_and_never_col(self, case: P41OwnedLiteralExpectation) -> None:
        with pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(case.source)
        exc = caught.value
        assert exc.construct != P41_FORBIDDEN_OWNED_LITERAL_CONSTRUCT, (
            f"{case.source!r} refused naming {exc.construct!r} — 2.4 forbids naming an incidental "
            f"sub-expression; the literal is {case.owner!r}'s own argument at {case.site}, so "
            f"{case.owner!r} owns the operation."
        )
        assert exc.construct == case.owner
        # `bugfix.md` 3.6: the carrier is unchanged — three positional fields,
        # no `reason`, `str(exc)` byte-identical in shape.
        assert exc.line == case.source
        assert exc.line_number == 1
        assert exc.reason is None
        assert str(exc) == f"unconvertible PySpark construct '{case.owner}' on line 1: {case.source}"

    def test_no_owned_literal_shape_is_attributed_to_col(self) -> None:
        """2.5 as one assertion: the aggregation over these shapes is sound."""
        answers = set()
        for case in P41_OWNED_LITERAL_SHAPES:
            with pytest.raises(ConversionError) as caught:
                PySpark2PandasConverter.convert(case.source)
            answers.add(caught.value.construct)
        assert answers == {"alias", "cast"}, (
            f"refusals inside `alias`'s and `cast`'s own literal arguments were attributed to "
            f"{sorted(answers)}; 2.5 requires every one of them counted under the construct a "
            "reader would attribute it to."
        )


class TestTheseSitesAlreadyHoldTheirOwnName:
    """Task 8.1, extended scope — CHARACTERIZATION. Oracle: CURRENT OUTPUT.

    **Validates: Requirements 2.4, 3.5, 3.6**

    ══════════════════════════════════════════════════════════════════
    EXPECTED OUTCOME: **GREEN, before and after task 8.1.** A red here is a
    finding: it would mean the extension changed a refusal path it was not
    meant to touch.
    ══════════════════════════════════════════════════════════════════

    The evidence that extending 8.1 to `:961` and `:985` passes an
    **already-held** name rather than introducing one. Each of these shapes
    refuses through the site's OWN `_refuse(func, <name>)` call — not through
    `_string` — and already answers the owning construct. That is what makes
    the class above an inconsistency inside one site rather than a new
    requirement, and it is why the fix adds no mechanism.

    These four also fence the extension from below: they must keep their
    current answers, so a change that routed the site's other refusals through
    the new parameter would fail here.
    """

    @pytest.mark.parametrize(
        ("source", "construct"),
        [
            # `_cast`'s unrecognised-target branch — `_CAST_TARGETS.get` misses.
            ("""x = col("a").cast("nonsense")""", "cast"),
            # `_cast`'s non-literal, non-type-constructor branch.
            ("""x = col("a").cast(some_var)""", "cast"),
            # `_method_call`'s non-literal alias branch, which is also baseline
            # case `refuse-alias-non-literal`
            # (`test_converter_equivalence_baseline.py`) — it must not move.
            ("""x = col("a").alias(alias_var)""", "alias"),
            # `alias` with the wrong arity.
            ("""x = col("a").alias("a", "b")""", "alias"),
        ],
    )
    def test_the_sites_other_refusals_already_name_the_owner(self, source: str, construct: str) -> None:
        with pytest.raises(ConversionError) as caught:
            PySpark2PandasConverter.convert(source)
        exc = caught.value
        assert exc.construct == construct
        assert exc.line == source
        assert exc.line_number == 1
        assert exc.reason is None

    @pytest.mark.parametrize(
        "source",
        [
            """x = col("a").alias('plain')""",
            """x = col("a").cast('int')""",
        ],
    )
    def test_the_string_fence_still_refuses_when_called_with_two_positionals(self, source: str) -> None:
        """`construct` is optional, keyword-capable and LAST.

        The hard constraint on 8.1's signature change, asserted behaviourally:
        `_string(node, owner)` keeps working, and without a `construct` it
        keeps using `_refuse_here`. Task 2.3's tests call it this way and must
        stay green.
        """
        translator = _Translator(source)
        literal = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in ("plain", "int")
        )
        owner = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call))
        with pytest.raises(_Unconvertible):
            translator._string(literal, owner)

    @pytest.mark.parametrize(
        "source",
        [
            """x = col("a").alias('plain')""",
            """x = col("a").cast('int')""",
        ],
    )
    def test_the_owning_name_is_reachable_as_a_keyword_argument(self, source: str) -> None:
        """The same fence, told which construct owns it — the whole change.

        Asserted over `_string` directly so the parameter's contract is pinned
        independently of which call sites happen to pass it, and as a KEYWORD
        so the "keyword-capable and last" half of the constraint is checked
        rather than assumed.
        """
        translator = _Translator(source)
        literal = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in ("plain", "int")
        )
        owner = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call))
        expected = "alias" if literal.value == "plain" else "cast"
        with pytest.raises(_Unconvertible) as caught:
            translator._string(literal, owner, construct=expected)
        assert caught.value.construct == expected
        assert caught.value.reason is None
