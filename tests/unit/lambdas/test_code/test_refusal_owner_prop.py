# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p41 task 8.3 — D2's payload property: a refusal names the operation's owner.

**Validates: Requirements 2.4, 2.5, 2.6, 3.5, Property 3, Property 4**

══════════════════════════════════════════════════════════════════════
ORACLE RULE FOR THIS FILE: THE OWNER COMES FROM THE GENERATED SHAPE.
CURRENT CONVERTER OUTPUT IS **NEVER** CONSULTED. READ THIS BEFORE EDITING.
══════════════════════════════════════════════════════════════════════

`LGR-28b4d75-5` (template / ratified): *a test written from the producer's output
cannot detect the producer drifting, because the output is its own oracle.* p41
runs **two opposite rules** inside `tests/unit/lambdas/test_code/`, so every new
file has to say which one it follows.

**This file follows the semantics rule.** Every expected construct below is an
**input to generation**, not a reading of the converter: the generator picks the
owning construct *first* and then writes a block whose refusal belongs to it. The
expectation for `col("a").alias('plain')` is `alias` because the generator put the
literal in `alias`'s new-name argument — not because the converter says so.

**THE OWNER RULE, stated once, from `bugfix.md` 2.4/2.6 semantics.** For a
generated block, the construct that *owns* the refusal is the construct whose own
argument or own operation the proxy could not perform:

* a literal in a construct's own argument slot is owned by **that construct** —
  `rlike`'s pattern, `regexp_replace`'s pattern and replacement, `alias`'s new
  name, `cast`'s target key, `to_date`'s format, `concat_ws`'s separator;
* a **column name** at a frame method's own argument slot is owned by that
  **frame method** — `df.withColumn('name', …)` is `withColumn`'s operation, which
  is exactly why task 8.1 deliberately did not parameterise `_column_name`;
* a call spelled through a module alias is owned by the **function**, never by the
  alias (2.6) — `F.initcap(...)` is `initcap`'s operation, and `F` is a spelling;
* an unsupported **method** is owned by itself, an unsupported **function** by
  itself, and a Spark **entry point** (`spark`, `sc`, `sqlContext`) by itself,
  because in those statements no other converter-vocabulary token is present;
* a `when(...).otherwise(...)` chain is owned by **`when`** — the chain is one
  operation and `when` is the construct that names it.

**Why this is NOT the production rule restated, and it is machine-checked rather
than argued.** The production derivation for a failure with no owning construct
is `_refuse_here`'s **leftmost occurrence in the enclosing expression**
(`pyspark_to_pandas.py:902`) — a *textual position* rule with no notion of
ownership. Reusing it as this file's oracle would make the property tautological
and it would have passed on the exact misnaming it exists to catch:
:class:`TestTheOwnerOracleIsNotTheProductionRule` evaluates the production rule
on three of the generated shapes and shows it answers **`col`** where this file's
owner rule answers `rlike` / `alias` / `cast`. That is the same argument p40 made
for not reusing `_is_scalar_emission` as its own oracle.

Production names are imported for exactly two purposes, neither of which is an
expectation: to **enumerate the generator's domain** (`_UNSUPPORTED_FUNCTIONS`
etc. decide which names get *written into* a generated block, and the owner of a
block containing `initcap` is `initcap` because the generator wrote it), and to
**audit** the oracle's independence in the class named above.

──────────────────────────────────────────────────────────────────────
WHY A VACUOUS SWEEP IS THE RISK HERE, AND WHAT GUARDS IT
──────────────────────────────────────────────────────────────────────

`design.md` §9 Q6 records a templated sweep in which **all 376 generated
statements refused**, so its zero offenders proved nothing. Here the risk is
inverted twice over, so there are three floors rather than one
(:class:`TestTheGeneratedSpaceIsNotVacuous`, deterministic and over the WHOLE
grid, not over a Hypothesis sample):

1. **A block that converts proves nothing about a refusal payload.** Property 3's
   antecedent is "a block that refuses", so every family here promises a refusal
   and :data:`P41_REFUSAL_FLOOR` asserts how many blocks actually deliver one. A
   converting draw fails the property as a **generator** defect, named as such.
2. **A refusal with no wrong answer available proves nothing either.** If no
   other construct sits textually left of the owner, the leftmost rule and the
   owner rule cannot disagree and the case is green for free. So the grid is
   audited for **decoys** — a `col(`, a module alias, or another construct token
   strictly left of the owner — with a floor on each of the three. `col`, `F` and
   `functions` are the three answers actually observed in production
   (`bugfix.md` 1.6, 1.8), so those are the three decoys counted.
3. **A block that refuses as `python_syntax` says nothing about vocabulary.**
   Every generated block is parsed and no case may refuse under that name.

──────────────────────────────────────────────────────────────────────
THE SPELLINGS, AND WHY THEY ARE ALL SINGLE- OR TRIPLE-QUOTED
──────────────────────────────────────────────────────────────────────

Task 4.3 (T3) made a raw **double**-quoted literal *convert*. A D2 test written
with `r"…"` would therefore stop refusing and go silently vacuous from T3 onward
(`tasks.md` bite 3). Every literal-fence family here uses a spelling the **kept
quote fence** refuses before and after T3 — `'…'`, `r'…'`, `\"\"\"…\"\"\"` (Q1,
`_string`'s predicate 2) — so the construct name is the only thing that moves.

The `cast` family also carries a double-quoted **unrecognised** target
(`cast("nonsense")`), which refuses through the `_CAST_TARGETS` miss rather than
through the fence. Both halves of that site now answer `cast`; before task 8.1
one operation answered two different constructs depending on which of its own
guards fired (`observations.md`, task 8.1 appendix).

──────────────────────────────────────────────────────────────────────
TWO THINGS `observations.md` HANDED THIS FILE, AND WHERE THEY LAND
──────────────────────────────────────────────────────────────────────

**The `:961` / `:985` scope question is CLOSED, and this file generates those
shapes deliberately rather than working around them.** Task 2.5's cross-cutting
observation recorded that `alias` and `cast` named `col` for literals they own,
outside task 8.1's original three-site scope, and left task 8.3 to either
constrain its generator or widen 8.1. Task 8.1 **widened**: five sites now pass a
construct. So the `alias` and `cast` families below are generated at full width
and :class:`TestTheOwnedLiteralSitesAreCleanWhenGeneratedDeliberately` asserts
them deterministically as well. Constraining the generator away from a known true
counterexample is the vacuity failure mode `tasks.md` 10.1 exists to forbid,
applied to Property 3.

**`F.when(c, v).otherwise(w)` refuses where the unaliased spelling converts, and
that needs no constraint.** `_when_chain` walks the chain itself and is on none of
task 8.2's three reader paths, so the aliased spelling still refuses — but it
refuses naming **`when`**, which *owns* the operation, so 2.6 and Property 3 both
hold. Recorded in the 8.2 appendix and asserted here by
:class:`TestTheAliasedWhenChainDivergenceStillNamesItsOwner`, so a `when`-shaped
draw is never mistaken for a failure.

──────────────────────────────────────────────────────────────────────
WHAT THIS FILE DOES NOT ASSERT
──────────────────────────────────────────────────────────────────────

The refusal **carrier** — `ConversionError`'s three positional fields, `reason`
staying `None`, and `str(exc)`'s rendering (`bugfix.md` 3.6) — is pinned by tasks
1.x and 2.x and is deliberately not re-asserted here. D2 changes *which* construct
is named, never the shape that carries it, and this property is about the one
field that moved.

Property 4 (`test_converter_vocabulary_closure_prop.py`'s two generative
properties) is a **fence, re-run unamended** as this task's gate, never weakened
to accommodate anything here. It builds
`df.withColumn("out", {name}(col("{column}")))` with plain double quotes, so it
cannot reach `_string`'s failure path at all (`design.md` §9 Q2, verified by
reading).

**EXPECTED RESULT: GREEN ON THE FIRST RUN**, since tasks 8.1 and 8.2 have landed.
**A red is a real finding about the implementation, not a test to adjust** — with
one exception that is a finding about *this file*: a case that **converts** means a
family stopped refusing, which is a generator defect and is reported as one.

pandas is not installed in this venv and must not be (`bugfix.md` 3.14). Nothing
here executes the emitted pandas — there is no emission on a refusal path — so
every assertion below is evidence about the **translation**.

No shared fixture from `tests/fixtures/conftest.py` is relevant: the converter is
pure text-to-text and touches no AWS surface.
"""

# Feature: p41-converter-literal-fidelity, Property 3: for any transform block
# that refuses, the construct the refusal names is the construct that OWNS the
# operation the proxy could not perform — the construct whose own argument or own
# operation the failure belongs to, as the generator constructed it — and is never
# `col`, `F` or `functions`, the three answers production was observed giving
# instead.

import ast
import os
import sys
import warnings
from collections.abc import Callable
from typing import NamedTuple

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# Add Lambda source to path (same shape as the rest of this directory).
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

# The frozensets are the generator's DOMAIN, never an expectation: they decide
# which names get written into a generated block, and the owner of a block
# carrying `initcap` is `initcap` because this file wrote it there. `_occurrences`
# is imported for ONE purpose — the independence audit in
# `TestTheOwnerOracleIsNotTheProductionRule` — and never to build an expectation.
from pyspark_to_pandas import (
    _CAST_TARGETS,
    _FUNCTION_MODULE_ALIASES,
    _SUPPORTED_FUNCTIONS,
    _SUPPORTED_METHODS,
    _UNSUPPORTED_FUNCTIONS,
    _UNSUPPORTED_METHODS,
    _UNSUPPORTED_NAMES,
    ConversionError,
    PySpark2PandasConverter,
    _occurrences,
)

#: The three answers production was observed giving instead of the owner —
#: `col` for a literal inside `rlike`'s pattern (`bugfix.md` 1.6) and `F` /
#: `functions` for anything reached through a module alias (1.8, and
#: `observations.md` task 1.4's correction that `functions` answers under its own
#: name). Asserted separately from the ownership comparison so a red reads as
#: "the leftmost race was lost again" rather than only as a mismatch.
P41_NEVER_NAMED = frozenset({"col", "F", "functions"})

#: Floors for the deterministic non-vacuity sweep. `tasks.md` 8.3 requires a floor
#: on the number of generated blocks that actually refused; the three decoy floors
#: are this file's own addition, because a refusal with no wrong answer available
#: is green for free (see the module docstring).
P41_MINIMUM_GENERATED = 100
P41_REFUSAL_FLOOR = 100
P41_COL_DECOY_FLOOR = 20
P41_ALIAS_DECOY_FLOOR = 20
P41_OTHER_DECOY_FLOOR = 20


class P41OwnedRefusal(NamedTuple):
    """One generated block, with the owner the GENERATOR gave it.

    *owner* is an input: the family builder chose it and then wrote a block whose
    refusal belongs to it. It is never read back from the converter.
    """

    family: str
    owner: str
    source: str


# ══════════════════════════════════════════════════════════════════════
# THE SPELLINGS — every one refuses through the KEPT quote fence, before
# and after T3 (Q1). A raw DOUBLE-quoted literal is deliberately absent:
# it converts from T3 onward and would make a D2 assertion vacuous.
# ══════════════════════════════════════════════════════════════════════


def p41_single_quoted(body: str) -> str:
    return f"'{body}'"


def p41_raw_single_quoted(body: str) -> str:
    return f"r'{body}'"


def p41_triple_quoted(body: str) -> str:
    return f'"""{body}"""'


P41_REFUSING_SPELLINGS: tuple[Callable[[str], str], ...] = (
    p41_single_quoted,
    p41_raw_single_quoted,
    p41_triple_quoted,
)

#: Pattern-ish bodies. No `'` in any of them, so every spelling above is a valid
#: Python literal; one carries a backslash and one is empty.
P41_PATTERN_BODIES = ("[^0-9]", r"@x\.com$", "^[a-z]+$")

#: Column-name-ish bodies, drawn separately: a pattern and a column name are not
#: the same kind of string (`bugfix.md` 3.1).
P41_NAME_BODIES = ("plain", "snake_case_name")

#: Column names used as plain double-quoted arguments — chosen so none of them
#: contains a construct name, which is what makes the decoy audit's textual
#: `str.index(owner)` meaningful.
P41_COLUMNS = ("email", "phone", "a", "d")


# ══════════════════════════════════════════════════════════════════════
# THE FAMILIES. One builder per owner kind. Each returns blocks whose
# refusal the OWNER RULE (module docstring) attributes to `owner`.
# ══════════════════════════════════════════════════════════════════════


def p41_rlike_pattern_cases() -> list[P41OwnedRefusal]:
    """`rlike`'s pattern — D2a's original defect (`bugfix.md` 1.6, `:1232`).

    Owner `rlike`: the literal is the predicate's own pattern argument. `col(`
    starts left of `.rlike`, which is the race the leftmost rule lost.
    """
    return [
        P41OwnedRefusal("rlike-pattern", "rlike", f'df_transformed = df.filter(col("{column}").rlike({spell(body)}))')
        for spell in P41_REFUSING_SPELLINGS
        for body in P41_PATTERN_BODIES
        for column in ("email",)
    ] + [
        # A multi-line fluent chain: the owner is unchanged and the refusal is
        # reported against the line the receiver starts on.
        P41OwnedRefusal(
            "rlike-pattern",
            "rlike",
            'df_transformed = df.filter(\n    col("email")\n    .rlike(' + p41_raw_single_quoted(r"@x\.com$") + ")\n)",
        )
    ]


def p41_regexp_replace_cases() -> list[P41OwnedRefusal]:
    """`regexp_replace`'s pattern (`:845`) and replacement (`:846`).

    Owner `regexp_replace` at both: each literal is one of that call's own
    arguments. The pattern shape also carries a `withColumn` decoy to its left.
    """
    pattern_cases = [
        P41OwnedRefusal(
            "regexp_replace-pattern",
            "regexp_replace",
            f'df_transformed = df.withColumn("out", regexp_replace(col("phone"), {spell(body)}, ""))',
        )
        for spell in P41_REFUSING_SPELLINGS
        for body in P41_PATTERN_BODIES
    ]
    replacement_cases = [
        P41OwnedRefusal(
            "regexp_replace-replacement",
            "regexp_replace",
            f'x = regexp_replace(col("phone"), "[^0-9]", {spell(body)})',
        )
        for spell in P41_REFUSING_SPELLINGS
        for body in ("X", "")
    ]
    aliased = [
        # Crosses task 8.2's alias resolution with task 8.1's construct
        # parameter: the module alias must resolve AND the site must name itself.
        P41OwnedRefusal(
            "regexp_replace-pattern",
            "regexp_replace",
            f'x = {alias}.regexp_replace(col("a"), {p41_single_quoted("[^0-9]")}, "")',
        )
        for alias in sorted(_FUNCTION_MODULE_ALIASES)
    ]
    return pattern_cases + replacement_cases + aliased


def p41_alias_new_name_cases() -> list[P41OwnedRefusal]:
    """`alias`'s new name (`:961`) — one of the two sites task 8.1 widened to.

    Owner `alias`: the literal is this operation's own new-name argument.
    `col(` starts left of `.alias`, so this is the same leftmost race as D2a and
    it named `col` before task 8.1 (`observations.md` task 2.5).
    """
    return [
        P41OwnedRefusal("alias-new-name", "alias", f'x = col("a").alias({spell(body)})')
        for spell in P41_REFUSING_SPELLINGS
        for body in P41_NAME_BODIES
    ] + [
        P41OwnedRefusal("alias-new-name", "alias", f'df_transformed = df.select(col("a").alias({spell(body)}))')
        for spell in P41_REFUSING_SPELLINGS
        for body in P41_NAME_BODIES
    ]


def p41_cast_target_cases() -> list[P41OwnedRefusal]:
    """`cast`'s target key (`:985`) — the other site task 8.1 widened to.

    Owner `cast` on both halves of the site, which is the point: the fence half
    (a single-quoted target) and the `_CAST_TARGETS`-miss half (an unrecognised
    double-quoted target) used to answer `col` and `cast` respectively — one
    operation, two answers (`observations.md` task 8.1 appendix).
    """
    fence_cases = [
        P41OwnedRefusal("cast-target", "cast", f'x = col("a").cast({spell(key)})')
        for spell in P41_REFUSING_SPELLINGS
        for key in ("int", "string")
    ]
    miss_cases = [
        P41OwnedRefusal("cast-target", "cast", f'x = col("a").cast("{key}")') for key in ("nonsense", "notatype")
    ]
    chained = [
        P41OwnedRefusal(
            "cast-target", "cast", f'df_transformed = df.withColumn("out", col("a").cast({p41_single_quoted("int")}))'
        )
    ]
    return fence_cases + miss_cases + chained


def p41_aliased_unsupported_function_cases() -> list[P41OwnedRefusal]:
    """`F.<fn>(...)` / `functions.<fn>(...)` — D2b (`bugfix.md` 1.8, 2.6).

    Owner is the **function**, because the alias is a spelling. Every member of
    `_UNSUPPORTED_FUNCTIONS` is generated under `F.`, so a name added to that
    frozenset later is covered here without an edit; six curated names are
    generated under `functions.` as well, since `observations.md` task 1.4
    recorded that spelling answering under its own name and one predicate closes
    both.

    Arity is deliberately not tailored per function: `convert`'s pre-scan refuses
    on the first unsupported occurrence before the translator runs, so the shape
    refuses under the resolved name regardless (`design.md` §12 item 2).
    """
    every_name = [
        P41OwnedRefusal(
            "aliased-unsupported-function", name, f'df_transformed = df.withColumn("out", F.{name}(col("a")))'
        )
        for name in sorted(_UNSUPPORTED_FUNCTIONS)
    ]
    functions_spelling = [
        P41OwnedRefusal("aliased-unsupported-function", name, f'x = functions.{name}(col("a"))')
        for name in ("initcap", "substring", "coalesce", "split", "length", "expr")
    ]
    return every_name + functions_spelling


def p41_bare_unsupported_function_cases() -> list[P41OwnedRefusal]:
    """The unaliased spelling of the same claim.

    Owner is the function. Curated rather than exhaustive on purpose:
    `test_converter_vocabulary_closure_prop.py` owns the total sweep over every
    offered function and is re-run as this task's gate, so the job here is only
    to span aliased and unaliased spellings of one claim inside one family set.
    """
    return [
        P41OwnedRefusal("bare-unsupported-function", name, f'df_transformed = df.withColumn("out", {name}(col("a")))')
        for name in ("initcap", "substring", "coalesce", "split", "length", "expr", "udf", "posexplode")
    ]


def p41_unsupported_method_cases() -> list[P41OwnedRefusal]:
    """An unsupported frame method — owner is the method itself.

    Every member of `_UNSUPPORTED_METHODS`, so an addition is covered without an
    edit, plus three chained shapes where a `withColumn` and a `col(` sit left of
    the owner and the leftmost rule could have taken either.
    """
    plain = [
        P41OwnedRefusal("unsupported-method", name, f'df_transformed = df.{name}("a")')
        for name in sorted(_UNSUPPORTED_METHODS)
    ]
    chained = [
        P41OwnedRefusal("unsupported-method", name, f'df_transformed = df.withColumn("b", col("a")).{name}("b")')
        for name in ("groupBy", "cache", "distinct")
    ]
    return plain + chained


def p41_column_name_site_cases() -> list[P41OwnedRefusal]:
    """A column name at a frame method's own argument slot (`_column_name`).

    Owner is the **frame method**, and this family is here to assert the
    *negative* half of task 8.1's decision: `_column_name`'s eight sites
    deliberately do NOT pass a construct, because leftmost is already the owner
    there. Baseline case `refuse-single-quoted-col` pins the same fact by hand.
    """
    shapes = (
        ("withColumn", 'df_transformed = df.withColumn({lit}, col("a"))'),
        ("withColumnRenamed", 'df_transformed = df.withColumnRenamed({lit}, "new_name")'),
        ("drop", "df_transformed = df.drop({lit})"),
        ("select", 'df_transformed = df.select({lit}, "other")'),
    )
    return [
        P41OwnedRefusal("column-name-site", owner, template.replace("{lit}", spell(body)))
        for owner, template in shapes
        for spell in P41_REFUSING_SPELLINGS
        for body in P41_NAME_BODIES
    ]


def p41_spark_entry_point_cases() -> list[P41OwnedRefusal]:
    """A Spark entry point — owner is the entry point.

    Its statement contains no other converter-vocabulary token (`conf`, `set`,
    `parallelize`, `sql` are in no frozenset), so the entry point is the only
    construct the operation can belong to. **Load-bearing rather than decorative:**
    `spark.conf.set(...)`'s `func.value` is an `Attribute`, so task 8.2's alias
    predicate must not fire and the answer must stay `spark` — `bugfix.md` 1.14's
    whole argument (the 3 measured `set` refusals come from the Python builtin,
    not from the scaffolds) rests on it.
    """
    return [
        P41OwnedRefusal("spark-entry-point", "spark", 'spark.conf.set("k", "v")'),
        P41OwnedRefusal(
            "spark-entry-point",
            "spark",
            'spark.conf.set("spark.sql.catalog.s3tablesbucket", "org.apache.iceberg.spark.SparkCatalog")',
        ),
        P41OwnedRefusal("spark-entry-point", "spark", 'df_transformed = spark.read.csv("data.csv")'),
        P41OwnedRefusal("spark-entry-point", "sc", 'x = sc.parallelize("a")'),
        P41OwnedRefusal("spark-entry-point", "sqlContext", 'x = sqlContext.sql("select 1")'),
        P41OwnedRefusal("spark-entry-point", "StructField", 'x = StructField("a", "b")'),
    ]


def p41_aliased_when_chain_cases() -> list[P41OwnedRefusal]:
    """`F.when(...).otherwise(...)` — the divergence task 8.2 recorded.

    Owner `when`: the chain is one operation and `when` is the construct that
    names it. `_when_chain` walks the chain itself and is on none of 8.2's three
    reader paths, so this spelling still refuses where the unaliased one converts
    — but it refuses naming its owner, so no generator constraint is needed.
    """
    return [
        P41OwnedRefusal("aliased-when-chain", "when", f'x = {alias}.when(col("a") == "x", "y").otherwise("z")')
        for alias in sorted(_FUNCTION_MODULE_ALIASES)
    ]


def p41_unknown_column_method_cases() -> list[P41OwnedRefusal]:
    """An invented method on a `col(...)` inside `filter` — owner is the method.

    The unsupported operation is the method call itself, so the method name owns
    it; `filter` converts perfectly well and `col` is incidental. Both of them sit
    left of the owner, which makes this the widest decoy in the grid.
    """
    return [
        P41OwnedRefusal("unknown-column-method", name, f'df_transformed = df.filter(col("a").{name}({spell("x")}))')
        for name in ("mystery", "frobnicate")
        for spell in P41_REFUSING_SPELLINGS
    ]


def p41_datetime_format_cases() -> list[P41OwnedRefusal]:
    """`to_date` / `to_timestamp`'s format string (`:915`).

    Owner is the function whose format argument it is. Task 8.1 deliberately did
    **not** widen to this site, because the owner node it hands `_string` starts
    at the function name and so wins the leftmost race on its own — this family
    asserts that stays true rather than assuming it.
    """
    return [
        P41OwnedRefusal("datetime-format", name, f'x = {name}(col("d"), {spell("yyyy-MM-dd")})')
        for name in ("to_date", "to_timestamp")
        for spell in P41_REFUSING_SPELLINGS
    ]


def p41_concat_ws_separator_cases() -> list[P41OwnedRefusal]:
    """`concat_ws`'s separator (`:852`) — owner `concat_ws`, same argument as above."""
    return [
        P41OwnedRefusal("concat-ws-separator", "concat_ws", f'x = concat_ws({spell(body)}, col("a"), col("b"))')
        for spell in P41_REFUSING_SPELLINGS
        for body in ("-", "_")
    ]


P41_FAMILY_BUILDERS: tuple[Callable[[], list[P41OwnedRefusal]], ...] = (
    p41_rlike_pattern_cases,
    p41_regexp_replace_cases,
    p41_alias_new_name_cases,
    p41_cast_target_cases,
    p41_aliased_unsupported_function_cases,
    p41_bare_unsupported_function_cases,
    p41_unsupported_method_cases,
    p41_column_name_site_cases,
    p41_spark_entry_point_cases,
    p41_aliased_when_chain_cases,
    p41_unknown_column_method_cases,
    p41_datetime_format_cases,
    p41_concat_ws_separator_cases,
)

#: The whole grid, built once. Every row's `owner` was chosen by its builder
#: before the block was written, and nothing below consults the converter to
#: decide it.
P41_ALL_CASES: tuple[P41OwnedRefusal, ...] = tuple(case for builder in P41_FAMILY_BUILDERS for case in builder())

P41_FAMILY_NAMES: tuple[str, ...] = tuple(dict.fromkeys(case.family for case in P41_ALL_CASES))

P41_CASES_BY_FAMILY: dict[str, tuple[P41OwnedRefusal, ...]] = {
    family: tuple(case for case in P41_ALL_CASES if case.family == family) for family in P41_FAMILY_NAMES
}


# ══════════════════════════════════════════════════════════════════════
# Running the converter, and auditing the decoys.
# ══════════════════════════════════════════════════════════════════════


def p41_outcome(source: str) -> "tuple[str, str | None]":
    """``("refused", <construct>)`` or ``("converted", <emitted code>)``."""
    try:
        result = PySpark2PandasConverter.convert(source)
    except ConversionError as exc:
        return "refused", exc.construct
    return "converted", result["code"]


#: Every construct token a decoy audit looks for, minus `col` and the two module
#: aliases, which are counted in their own categories because they are the three
#: answers production actually gave.
P41_OTHER_CONSTRUCT_TOKENS: frozenset = frozenset(
    (_SUPPORTED_FUNCTIONS | _SUPPORTED_METHODS | _UNSUPPORTED_FUNCTIONS | _UNSUPPORTED_METHODS | _UNSUPPORTED_NAMES)
    - {"col"}
    - set(_FUNCTION_MODULE_ALIASES)
)


def p41_decoys(case: P41OwnedRefusal) -> set[str]:
    """Which wrong answers were textually available to the leftmost rule.

    A construct token strictly left of the owner's own token is a construct the
    leftmost-occurrence rule *would* have named. With no decoy, the two
    derivations cannot disagree and the case is green for free — which is why
    :class:`TestTheGeneratedSpaceIsNotVacuous` puts a floor on each category.
    """
    source = case.source
    owner_at = source.index(case.owner)
    found: set[str] = set()
    if "col(" in source and source.index("col(") < owner_at:
        found.add("col")
    for alias in _FUNCTION_MODULE_ALIASES:
        marker = f"{alias}."
        if marker in source and source.index(marker) < owner_at:
            found.add("module-alias")
    for token in P41_OTHER_CONSTRUCT_TOKENS:
        if token == case.owner:
            continue
        at = source.find(token)
        if at != -1 and at < owner_at:
            found.add("other-construct")
            break
    return found


def p41_sweep() -> dict:
    """Deterministic pass over the WHOLE grid, for the non-vacuity guards.

    A function rather than inlined, so the same numbers can be reported outside
    pytest without re-deriving the grid.
    """
    refused = 0
    converted: list[P41OwnedRefusal] = []
    misnamed: list[tuple[P41OwnedRefusal, str]] = []
    syntax_refusals: list[str] = []
    forbidden: list[tuple[P41OwnedRefusal, str]] = []
    decoy_counts = {"col": 0, "module-alias": 0, "other-construct": 0}

    for case in P41_ALL_CASES:
        for decoy in p41_decoys(case):
            decoy_counts[decoy] += 1
        kind, payload = p41_outcome(case.source)
        if kind != "refused":
            converted.append(case)
            continue
        refused += 1
        named = payload or ""
        if named == "python_syntax":
            syntax_refusals.append(case.source)
        if named != case.owner:
            misnamed.append((case, named))
        if named in P41_NEVER_NAMED:
            forbidden.append((case, named))

    return {
        "cases": len(P41_ALL_CASES),
        "families": len(P41_FAMILY_NAMES),
        "refused": refused,
        "converted": converted,
        "misnamed": misnamed,
        "syntax_refusals": syntax_refusals,
        "forbidden": forbidden,
        "decoys": decoy_counts,
    }


# ══════════════════════════════════════════════════════════════════════
# The oracle's independence, machine-checked rather than argued.
# ══════════════════════════════════════════════════════════════════════


def p41_leftmost_occurrence(source: str, attribute: str) -> str:
    """The production leftmost answer for the node `_string` receives as *owner*.

    Reproduces `_refuse_here`'s derivation — `_occurrences(node)[0].name` — on the
    enclosing call whose attribute is *attribute*, i.e. exactly the node the
    literal's site passes as `owner`. Used **only** to show that derivation is a
    different function from this file's owner rule.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attribute:
            occurrences = _occurrences(node)
            return occurrences[0].name if occurrences else "python_syntax"
    raise AssertionError(f"no `.{attribute}(...)` call in {source!r}")


class TestTheOwnerOracleIsNotTheProductionRule:
    """**Validates: Requirements 2.4, 3.5, Property 3**

    `tasks.md` 8.3: *do not implement the "owns the operation" oracle by calling
    the converter's own refusal path … reusing the production derivation makes the
    property tautological and it would pass on exactly the misnaming it exists to
    catch.* These tests are that argument made checkable instead of promised.
    """

    @pytest.mark.parametrize(
        "attribute,owner,source",
        [
            ("rlike", "rlike", "df_transformed = df.filter(col(\"email\").rlike(r'@x\\.com$'))"),
            ("alias", "alias", "x = col(\"a\").alias('plain')"),
            ("cast", "cast", "x = col(\"a\").cast('int')"),
        ],
    )
    def test_the_leftmost_rule_answers_col_where_the_owner_rule_does_not(self, attribute, owner, source):
        """The two derivations disagree, so one cannot be standing in for the other.

        `_refuse_here`'s rule is a *textual position* rule with no notion of
        ownership: on all three of these shapes it answers **`col`**, because
        `col(` starts left of the method. This file's expectation is `rlike` /
        `alias` / `cast`, derived from where the generator put the literal. Had
        the production rule been reused as the oracle, every one of these
        families would have expected `col` — and the property would have passed
        on the exact misnaming `bugfix.md` 1.6 and `observations.md` task 2.5
        reproduced.
        """
        leftmost = p41_leftmost_occurrence(source, attribute)
        assert leftmost == "col", (
            f"expected the production leftmost rule to answer 'col' for {source!r}, got {leftmost!r} — "
            "if this moved, `_refuse_here` changed and §5.3's whole argument needs re-deriving"
        )
        assert leftmost != owner, "the two derivations agree here, so this case proves nothing about independence"

    def test_no_family_owner_is_read_from_a_converter_call(self):
        """The owner set is a property of the grid, available with no converter.

        Building the grid calls no converter — so if `pyspark_to_pandas` were
        replaced wholesale, every expectation in this file would be unchanged.
        That is what "the expectation is an input to generation" means, and it is
        asserted here rather than left as prose.
        """
        owners = {case.owner for case in P41_ALL_CASES}
        assert len(owners) >= 10, f"only {len(owners)} distinct owners in the grid: {sorted(owners)}"
        assert owners & P41_NEVER_NAMED == set(), (
            "a family whose OWNER is col/F/functions would make the never-named clause contradict the "
            f"ownership clause: {sorted(owners & P41_NEVER_NAMED)}"
        )

    def test_a_col_owned_literal_is_deliberately_outside_the_grid(self):
        """`x = col('plain')` names `col`, correctly — and is excluded, on purpose.

        There the literal *is* `col`'s own argument, so `col` owns it and leftmost
        and owner agree. Generating it would make the never-named clause fire on a
        correct answer. The shape is covered by task 2.2's and task 3.1's
        per-site files instead; this test records the exclusion so the gap is
        deliberate rather than forgotten.
        """
        kind, named = p41_outcome("x = col('plain')")
        assert (kind, named) == ("refused", "col")
        # And the exclusion is structural, not incidental: no generated block ever
        # puts a refusing spelling inside `col`'s OWN argument slot.
        col_owned = [
            case.source
            for case in P41_ALL_CASES
            if any(marker in case.source for marker in ("col('", "col(r'", 'col("""'))
        ]
        assert col_owned == [], f"blocks whose failing literal is `col`'s own argument: {col_owned}"


# ══════════════════════════════════════════════════════════════════════
# Non-vacuity guards — deterministic, over the whole grid.
# ══════════════════════════════════════════════════════════════════════


class TestTheGeneratedSpaceIsNotVacuous:
    """**Validates: Requirements 2.4, 2.5, Property 3**

    `design.md` §9 Q6 records a templated sweep whose 376 statements all refused
    for the wrong reason, so its zero offenders proved nothing. These guards make
    each way this file could go quietly vacuous visible, deterministically.
    """

    def test_the_grid_is_large_enough_and_spans_every_family(self):
        """Size floor, and every builder reaching the grid.

        `families >= builders` rather than `==`: one builder deliberately labels
        two families (`regexp_replace`'s pattern and its replacement are separate
        argument slots of one call), so equality would be a bookkeeping claim
        rather than a coverage one. What matters is that no builder contributes
        nothing — a builder whose shapes all stopped refusing, or one left
        unwired, would silently shrink the quantifier.
        """
        report = p41_sweep()
        assert report["cases"] >= P41_MINIMUM_GENERATED, f"only {report['cases']} generated blocks"
        assert report["families"] >= len(P41_FAMILY_BUILDERS), (
            f"{report['families']} family labels reached the grid from {len(P41_FAMILY_BUILDERS)} builders"
        )
        silent = [builder.__name__ for builder in P41_FAMILY_BUILDERS if not builder()]
        assert silent == [], f"builders contributing no case: {silent}"
        empty = [family for family, cases in P41_CASES_BY_FAMILY.items() if not cases]
        assert empty == [], f"family labels contributing no case: {empty}"

    def test_every_generated_block_actually_refuses(self):
        """The refusal floor `tasks.md` 8.3 requires, and the inverted vacuity risk.

        Property 3's antecedent is "a block that refuses". A block that *converts*
        makes its case vacuous, so every family promises a refusal and this is
        where the promise is counted. **A failure here is a defect in this file's
        generator, not a finding about the converter** — a family stopped
        refusing, most likely because a spelling it uses became convertible.
        """
        report = p41_sweep()
        assert report["converted"] == [], (
            "these generated blocks CONVERT, so their cases prove nothing about a refusal payload "
            "(a generator defect, not a converter finding):\n"
            + "\n".join(f"  {case.family}: {case.source}" for case in report["converted"])
        )
        assert report["refused"] >= P41_REFUSAL_FLOOR, (
            f"only {report['refused']} of {report['cases']} generated blocks refused; the floor is {P41_REFUSAL_FLOOR}"
        )
        assert report["refused"] == report["cases"]

    def test_no_generated_block_refuses_as_python_syntax(self):
        """Every block parses, and none refuses under `python_syntax`.

        A snippet that does not parse refuses — the property's expected outcome —
        for a reason that has nothing to do with which construct owns what, which
        is the third way this sweep could go quietly vacuous.
        """
        for case in P41_ALL_CASES:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    ast.parse(case.source)
            except SyntaxError as exc:  # pragma: no cover - a builder bug, not a converter finding
                pytest.fail(f"{case.family}: generated block is not valid Python ({exc}): {case.source}")
        report = p41_sweep()
        assert report["syntax_refusals"] == [], f"blocks refusing as python_syntax: {report['syntax_refusals']}"

    def test_a_wrong_answer_was_available_in_each_of_the_three_categories(self):
        """The decoy floors — without one, a green case proves nothing.

        If nothing sits left of the owner, `_refuse_here`'s leftmost rule and the
        owner rule cannot disagree. `col`, a module alias, and another construct
        are the three decoys that matter, because `col`, `F` and `functions` are
        the three answers production was observed giving (`bugfix.md` 1.6, 1.8).
        """
        report = p41_sweep()
        decoys = report["decoys"]
        assert decoys["col"] >= P41_COL_DECOY_FLOOR, (
            f"only {decoys['col']} blocks put a `col(` left of their owner — D2a's race is barely exercised"
        )
        assert decoys["module-alias"] >= P41_ALIAS_DECOY_FLOOR, (
            f"only {decoys['module-alias']} blocks put a module alias left of their owner — D2b is barely exercised"
        )
        assert decoys["other-construct"] >= P41_OTHER_DECOY_FLOOR, (
            f"only {decoys['other-construct']} blocks put another construct left of their owner"
        )

    def test_every_owner_name_appears_in_its_own_block(self):
        """The decoy audit indexes the source by the owner's name, so it must be there.

        Also the cheapest possible check that a builder did not drift from the
        owner it declares.
        """
        missing = [case for case in P41_ALL_CASES if case.owner not in case.source]
        assert missing == [], f"blocks whose declared owner does not appear in them: {missing[:3]}"


class TestTheGeneratorsDomainTracksTheConvertersVocabulary:
    """**Validates: Requirements 2.6, 3.5, Property 3**

    The frozensets are read as the generator's domain, so a name added to the
    converter later is generated without an edit. These tests fail if that stops
    being true, which is what keeps the quantifier total rather than a sample.
    """

    def test_every_unsupported_function_is_generated_through_the_alias(self):
        generated = {case.owner for case in P41_CASES_BY_FAMILY["aliased-unsupported-function"]}
        missing = set(_UNSUPPORTED_FUNCTIONS) - generated
        assert missing == set(), f"unsupported functions with no aliased shape: {sorted(missing)}"

    def test_every_unsupported_method_is_generated(self):
        generated = {case.owner for case in P41_CASES_BY_FAMILY["unsupported-method"]}
        missing = set(_UNSUPPORTED_METHODS) - generated
        assert missing == set(), f"unsupported methods with no shape: {sorted(missing)}"

    def test_both_module_alias_spellings_are_generated(self):
        aliased = " ".join(
            case.source
            for case in P41_ALL_CASES
            if case.family in ("aliased-unsupported-function", "aliased-when-chain")
        )
        for alias in _FUNCTION_MODULE_ALIASES:
            assert f"{alias}." in aliased, f"the {alias!r} spelling is never generated"

    def test_the_invented_method_names_are_in_no_frozenset(self):
        """`mystery` / `frobnicate` must be unknown, or that family tests something else."""
        enumerated = (
            _SUPPORTED_FUNCTIONS
            | _SUPPORTED_METHODS
            | _UNSUPPORTED_FUNCTIONS
            | _UNSUPPORTED_METHODS
            | _UNSUPPORTED_NAMES
        )
        for case in P41_CASES_BY_FAMILY["unknown-column-method"]:
            assert case.owner not in enumerated, f"{case.owner!r} is enumerated, so this family is misnamed"

    def test_the_cast_family_uses_real_and_unreal_target_keys(self):
        """Both halves of `:985` are generated: a fence miss and a table miss."""
        sources = [case.source for case in P41_CASES_BY_FAMILY["cast-target"]]
        assert any(f"'{key}'" in source for key in _CAST_TARGETS for source in sources), (
            "no single-quoted REAL cast target is generated, so the fence half of `:985` is unexercised"
        )
        assert any('"nonsense"' in source for source in sources), (
            "no unrecognised cast target is generated, so the `_CAST_TARGETS`-miss half is unexercised"
        )


# ══════════════════════════════════════════════════════════════════════
# The two shapes `observations.md` handed this task, asserted directly.
# ══════════════════════════════════════════════════════════════════════


class TestTheOwnedLiteralSitesAreCleanWhenGeneratedDeliberately:
    """**Validates: Requirements 2.4, 2.5, Property 3**

    `observations.md` task 2.5 left `:961` (`alias`) and `:985` (`cast`) open:
    both named `col` for a literal they own, outside task 8.1's original
    three-site scope, and the shapes **survive T3** because the kept quote fence
    still refuses them. Task 8.1 resolved it by widening to five sites. These
    tests confirm that deliberately rather than trusting it — the generator is not
    constrained around either shape, and if one still named `col` it would be a
    real finding here and in the property.
    """

    @pytest.mark.parametrize(
        "owner,source",
        [
            ("alias", "x = col(\"a\").alias('plain')"),
            ("alias", 'x = col("a").alias("""plain""")'),
            ("alias", "df_transformed = df.select(col(\"a\").alias('renamed'))"),
            ("cast", "x = col(\"a\").cast('int')"),
            ("cast", "x = col(\"a\").cast(r'string')"),
            ("cast", 'x = col("a").cast("nonsense")'),
        ],
    )
    def test_the_961_and_985_shapes_name_their_owner_and_never_col(self, owner, source):
        kind, named = p41_outcome(source)
        assert kind == "refused", f"{source!r} converted; the fence that made it a D2 shape is gone"
        assert named == owner, f"{source!r} named {named!r}, not its owner {owner!r}"
        assert named != "col", "the leftmost race was lost again at an owned-literal site"

    def test_the_two_sites_answer_one_construct_across_both_of_their_guards(self):
        """One operation, one answer — the inconsistency task 8.1 decided on.

        Before the widening, `cast("nonsense")` named `cast` (the table miss) while
        `cast('int')` named `col` (the fence), and `alias(alias_var)` named `alias`
        while `alias('plain')` named `col`. Two guards inside one site answered two
        different constructs; that is what made extending the site *consistency*
        rather than scope creep (`observations.md` task 8.1 appendix).
        """
        assert p41_outcome('x = col("a").cast("nonsense")')[1] == p41_outcome("x = col(\"a\").cast('int')")[1] == "cast"
        assert (
            p41_outcome('x = col("a").alias(alias_var)')[1]
            == p41_outcome("x = col(\"a\").alias('plain')")[1]
            == ("alias")
        )


class TestTheAliasedWhenChainDivergenceStillNamesItsOwner:
    """**Validates: Requirements 2.6, Property 3**

    `observations.md` task 8.2's appendix records this as a known remaining
    divergence: `F.when(c, v).otherwise(w)` refuses where the unaliased spelling
    converts, because `_when_chain` walks the chain itself and is on none of the
    three readers' paths. It is **not** a Property 3 failure — it names `when`,
    which owns the operation — so the generator needs no constraint for it, and
    this class is what stops a `when`-shaped draw being read as a red.
    """

    @pytest.mark.parametrize("alias", sorted(_FUNCTION_MODULE_ALIASES))
    def test_the_aliased_chain_refuses_naming_when(self, alias):
        kind, named = p41_outcome(f'x = {alias}.when(col("a") == "x", "y").otherwise("z")')
        assert (kind, named) == ("refused", "when")
        assert named not in P41_NEVER_NAMED, "the alias reached the payload again"

    def test_the_unaliased_spelling_converts(self):
        """The divergence's other half, so the asymmetry is recorded, not implied."""
        kind, payload = p41_outcome('x = when(col("a") == "x", "y").otherwise("z")')
        assert kind == "converted", f"the unaliased when-chain refused naming {payload!r}"
        assert "np.where(" in (payload or "")


# ══════════════════════════════════════════════════════════════════════
# Property 3.
# ══════════════════════════════════════════════════════════════════════

#: Drawn as a product — family, then one of that family's blocks — rather than as
#: an index into the grid, so a counterexample shrinks to a family and a block a
#: reader can act on rather than to an integer.
p41_generated_refusals = st.sampled_from(P41_FAMILY_NAMES).flatmap(
    lambda family: st.sampled_from(P41_CASES_BY_FAMILY[family])
)

#: `tasks.md` 8.3's minimum is 100. `deadline=None` because the first example pays
#: for importing and parsing, and `too_slow` is suppressed for the same reason.
P41_PROP_SETTINGS = settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@P41_PROP_SETTINGS
@given(case=p41_generated_refusals)
def test_a_refusal_names_the_construct_that_owns_the_operation(case: P41OwnedRefusal):
    """Property 3: the refusal names the owner, and never `col` / `F` / `functions`.

    The owner is the construct the **generator** built the block around, derived
    from where it put the failing element (module docstring, "THE OWNER RULE"),
    never from a converter call. `TestTheOwnerOracleIsNotTheProductionRule` shows
    the production leftmost derivation answers `col` on three of these families,
    so the two rules are genuinely different functions and this assertion is not
    a restatement.

    The third assertion is not redundant with the second. No family's owner is
    `col`, `F` or `functions` — `test_no_family_owner_is_read_from_a_converter_call`
    holds that — so `named == owner` already implies it. It is asserted separately
    because those three are the answers production actually gave (`bugfix.md` 1.6,
    1.8), and a reader of a failure should see *which* wrong answer came back, not
    only that something mismatched.

    `event()` records the family, the owner and the decoys available, so
    `--hypothesis-show-statistics` reports which part of the grid the sample
    reached instead of leaving coverage assumed. The deterministic guards above
    cover the whole grid; this reports the sample.

    Validates: Requirements 2.4, 2.5, 2.6, 3.5, Property 3
    """
    kind, payload = p41_outcome(case.source)
    event(f"family={case.family}")
    event(f"owner={case.owner}")
    for decoy in sorted(p41_decoys(case)) or ["decoy=none"]:
        event(f"decoy={decoy}")

    # The antecedent, guarded rather than assumed: Property 3 is about a block
    # that refuses, so a converting draw makes the case vacuous. A failure here
    # is a generator defect in this file — see
    # `TestTheGeneratedSpaceIsNotVacuous.test_every_generated_block_actually_refuses`.
    assert kind == "refused", (
        f"{case.family}: this block CONVERTS, so it says nothing about a refusal payload "
        f"(a generator defect, not a converter finding)\n  input: {case.source}"
    )
    named = payload or ""
    assert named != "python_syntax", f"{case.family}: generated block did not parse: {case.source}"

    assert named == case.owner, (
        f"{case.family}: the refusal names a construct that does not own the operation (2.4).\n"
        f"  input:    {case.source}\n"
        f"  owns it:  {case.owner!r}  (from where the generator put the failing element)\n"
        f"  named:    {named!r}\n"
        f"  decoys available to the leftmost rule: {sorted(p41_decoys(case)) or ['none']}"
    )
    assert named not in P41_NEVER_NAMED, (
        f"{case.family}: the refusal names {named!r} — one of the three answers production was observed "
        f"giving instead of the owner (1.6, 1.8).\n  input: {case.source}\n  owns it: {case.owner!r}"
    )
