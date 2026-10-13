# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""No emission applies a Series-only pandas member to a scalar sub-expression.

Spec: `.kiro/specs/p40-pandas-proxy-literal-cast/`, task 12.1, R3.

**This file is design §4.8's post-emission scan, living in CI instead of in
production.** Q3 was answered "emission time only": the fence
(`_is_scalar_emission` / `_require_series`) refuses at the point a member is
appended, and a second scan before the interpreter session was deliberately NOT
added to production, because the only class it catches beyond the fence is *a
translation rule that emits a scalar form the fence's predicate does not
recognise* — a code condition, not a data one, so it belongs in a test run.

That one class is real, and it is the price of a deliberate decision. The
production predicate's default is **permissive**: an emission form it does not
recognise is assumed to be a Series and the guard stays out of its way, because
an unrecognised form may be any legitimate Series expression and refusing those
would refuse conversions that work today (`_is_scalar_emission`'s docstring,
design §4.1). So a future translation rule that emits a *new kind of scalar*
passes the fence, and this property is what fails when it does.

**Why the scalar test here is written from scratch instead of calling
`_is_scalar_emission`.** Reusing the production predicate would make this
property a tautology. The fence refuses exactly what `_is_scalar_emission`
recognises, so a check built on the same predicate agrees with the fence by
construction — and it agrees *precisely* on the one input it exists to catch, an
emission form the predicate does not recognise as scalar. It would pass, forever,
on the only failure it was written for. `LGR-28b4d75-5` (template / ratified) is
the record behind that rule: a test whose expectations come from the producer
cannot detect the producer drifting, because the output is its own oracle.

So :func:`series_like` below is a **second, independent detector, with the
opposite polarity**:

* production asks *"is this provably a scalar?"* and defaults unrecognised → not
  scalar, i.e. permissive — it lets the member through;
* this file asks *"is this provably a Series or a frame?"* and defaults
  unrecognised → **not** Series, i.e. strict — it reports the member.

The two therefore disagree on exactly the set that matters: a form neither
recognises. Production allows it; this property flags it. The recursion, the form
list and the polarity are all written independently, and nothing here imports the
production predicate. A false positive is possible by construction — a genuinely
new *Series* form would also be flagged — and that is the intended trade for a CI
check: it fails loudly and a human decides which side to teach.

Detection is over the **parsed** emitted pandas, never over substrings, per
`LGR-b46a5bb-4` (template / ratified): a substring rule forbids documenting the
thing it forbids, and `.astype` inside a string literal or a comment is not a
member access.

**Validates: Requirements R3**
"""

# Feature: p40-pandas-proxy-literal-cast, Property 4: for every construct in
# _SUPPORTED_FUNCTIONS ∪ _SUPPORTED_METHODS, built with a lit(...) receiver, the
# conversion either refuses, or emits pandas whose parse contains no
# _SERIES_ONLY_MEMBERS access on a scalar sub-expression.

import ast
import os
import sys

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# Add Lambda source to path (same shape as the rest of this directory).
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import pyspark_to_pandas
from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

#: Read from the module, never transcribed, so a member added to the fence later
#: is covered by this property without anyone editing this file. Same reason the
#: construct vocabulary below is read rather than listed.
SERIES_ONLY_MEMBERS = frozenset(pyspark_to_pandas._SERIES_ONLY_MEMBERS)

#: The quantifier of the property, read from the module for the same reason: a
#: name added to either frozenset is exercised here from the moment it lands.
SUPPORTED_CONSTRUCTS = frozenset(pyspark_to_pandas._SUPPORTED_FUNCTIONS) | frozenset(
    pyspark_to_pandas._SUPPORTED_METHODS
)


# ──────────────────────────────────────────────────────────────────────
# The independent detector.
#
# Polarity is the point: this proves SERIES, where production proves SCALAR.
# Written from first principles about what the converter can emit, not derived
# from `_is_scalar_node`'s four-form list — see the module docstring.
# ──────────────────────────────────────────────────────────────────────

#: The two modules `convert()` prepends. Every other bare name in emitted pandas
#: is a dataframe variable: the input frame, or an assignment target the emitted
#: block binds. So a Name that is not one of these two is frame-like.
_EMITTED_MODULES = frozenset({"pd", "np"})

#: `pd.<name>(...)` / `np.<name>(...)` calls that produce a Series, a frame or an
#: array regardless of what they are given. Everything the converter emits that
#: is not in here and not in :data:`_ELEMENTWISE_HELPERS` is treated as NOT
#: provably a Series — the strict default.
_CONSTRUCTING_HELPERS = frozenset({"Series", "DataFrame", "array", "where"})

#: Helpers that map their first argument elementwise: Series in, Series out —
#: and, just as importantly, scalar in, scalar out. So they are transparent to
#: this question rather than evidence for either answer, and the verdict comes
#: from the argument.
_ELEMENTWISE_HELPERS = frozenset({"to_numeric", "to_datetime", "isna", "notna"})


def series_like(node: ast.AST) -> bool:
    """True when *node* is provably a pandas Series, frame or array.

    The default is **strict**: a form this function does not recognise is not
    provably a Series, so it is reported as a scalar receiver. That is the
    inverse of the production predicate's permissive default, and the
    disagreement is deliberate (see the module docstring).
    """
    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, ast.Name):
        # A dataframe variable. `pd`/`np` are the only non-frame bare names the
        # emitted block ever reads.
        return node.id not in _EMITTED_MODULES
    if isinstance(node, ast.Subscript):
        # `df["a"]`, `df[["a", "b"]]`, `df[mask]` — a Series or a frame.
        return series_like(node.value)
    if isinstance(node, ast.Attribute):
        # An accessor or a bound method: `df["a"].str`, `df.index`.
        return series_like(node.value)
    if isinstance(node, ast.Call):
        return _call_series_like(node)
    if isinstance(node, ast.BinOp):
        # A Series on either side makes the whole expression a Series.
        return series_like(node.left) or series_like(node.right)
    if isinstance(node, ast.Compare):
        return series_like(node.left) or any(series_like(operand) for operand in node.comparators)
    if isinstance(node, ast.BoolOp):
        return any(series_like(value) for value in node.values)
    if isinstance(node, ast.UnaryOp):
        return series_like(node.operand)
    return False


def _call_series_like(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        receiver = func.value
        if isinstance(receiver, ast.Name) and receiver.id in _EMITTED_MODULES:
            if func.attr in _CONSTRUCTING_HELPERS:
                return True
            if func.attr in _ELEMENTWISE_HELPERS:
                return bool(node.args) and series_like(node.args[0])
            # An unrecognised module helper. Strict default: not provably a
            # Series. This is the branch that catches a translation rule that
            # starts emitting a new scalar-producing `pd.*` call.
            return False
        # A method call: the receiver decides. `df["a"].astype("string")` is a
        # Series because `df["a"]` is; `None.astype(str)` is not because `None`
        # is not.
        return series_like(receiver)
    # A bare name called: nothing binds it in the emitted block, so nothing can
    # be proven about it. `test_converter_vocabulary_closure_prop.py` owns that
    # failure mode; here it is simply not evidence of a Series.
    return False


def series_only_members_on_scalars(emitted: str) -> list[str]:
    """Every ``_SERIES_ONLY_MEMBERS`` access in *emitted* landing on a non-Series.

    A non-empty result is the defect class p40 exists to remove, found in the
    emitted text rather than at runtime in the Code Interpreter.
    """
    tree = ast.parse(emitted)
    return [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in SERIES_ONLY_MEMBERS and not series_like(node.value)
    ]


# ──────────────────────────────────────────────────────────────────────
# The generated space: every supported construct, with a lit(...) receiver.
#
# Column names are irrelevant to this property — the subject is the literal
# receiver — so they are fixed rather than drawn, which keeps the generated
# space entirely about the construct and the constant.
#
# Where a construct has no receiver slot a literal can occupy (`col`, `drop`,
# `withColumnRenamed` take column NAMES), the literal goes in the argument
# position instead. Those refuse, and a refusal is one of the property's two
# legal outcomes, so the quantification stays total rather than skipping names.
#
# Every construct that CAN compose over a Series also gets a variant where the
# literal has been through `.cast("string")` — Arm A, the one converting family.
# That is what puts converted emissions in the sample at all: without them the
# property would only ever observe refusals and would never exercise the scan.
# ──────────────────────────────────────────────────────────────────────

#: The five constant types design §4.2 rules on: `None` and `str` convert under
#: Arm A, the other three refuse.
LITERAL_SOURCES = ("lit(None)", 'lit("x")', "lit(0)", "lit(1.5)", "lit(True)")

CONSTRUCT_STATEMENTS: dict[str, tuple[str, ...]] = {
    "col": ('df_transformed = df.withColumn("out", col({lit}))',),
    "lit": (
        'df_transformed = df.withColumn("out", {lit})',
        'df_transformed = df.withColumn("out", {lit}).withColumn("b", trim(col("name")))',
    ),
    "trim": (
        'df_transformed = df.withColumn("out", trim({lit}))',
        'df_transformed = df.withColumn("out", trim({lit}.cast("string")))',
    ),
    "lower": (
        'df_transformed = df.withColumn("out", lower({lit}))',
        'df_transformed = df.withColumn("out", lower({lit}.cast("string")))',
    ),
    "upper": (
        'df_transformed = df.withColumn("out", upper({lit}))',
        'df_transformed = df.withColumn("out", upper({lit}.cast("string")))',
    ),
    "to_date": (
        'df_transformed = df.withColumn("out", to_date({lit}, "yyyy-MM-dd"))',
        'df_transformed = df.withColumn("out", to_date({lit}))',
        'df_transformed = df.withColumn("out", to_date({lit}.cast("string"), "yyyy-MM-dd"))',
    ),
    "to_timestamp": (
        'df_transformed = df.withColumn("out", to_timestamp({lit}, "yyyy-MM-dd"))',
        'df_transformed = df.withColumn("out", to_timestamp({lit}))',
        'df_transformed = df.withColumn("out", to_timestamp({lit}.cast("string"), "yyyy-MM-dd"))',
    ),
    "regexp_replace": (
        'df_transformed = df.withColumn("out", regexp_replace({lit}, "a", "b"))',
        'df_transformed = df.withColumn("out", regexp_replace({lit}.cast("string"), "a", "b"))',
    ),
    "concat_ws": (
        'df_transformed = df.withColumn("out", concat_ws("-", {lit}, col("name")))',
        'df_transformed = df.withColumn("out", concat_ws("-", {lit}.cast("string"), col("name")))',
    ),
    "when": (
        'df_transformed = df.withColumn("out", when(col("state") == "CA", {lit}).otherwise(col("name")))',
        'df_transformed = df.withColumn("out", when(col("state") == "CA", {lit}.cast("string")).otherwise(col("name")))',
    ),
    "otherwise": (
        'df_transformed = df.withColumn("out", when(col("state") == "CA", col("name")).otherwise({lit}))',
        'df_transformed = df.withColumn("out", when(col("state") == "CA", col("name")).otherwise({lit}.cast("string")))',
    ),
    "cast": (
        'df_transformed = df.withColumn("out", {lit}.cast("string"))',
        'df_transformed = df.withColumn("out", {lit}.cast(StringType()))',
        'df_transformed = df.withColumn("out", {lit}.cast("int"))',
        'df_transformed = df.withColumn("out", {lit}.cast(IntegerType()))',
        'df_transformed = df.withColumn("out", {lit}.cast("double"))',
        'df_transformed = df.withColumn("out", {lit}.cast("boolean"))',
    ),
    "alias": (
        'df_transformed = df.withColumn("out", {lit}.alias("z"))',
        'df_transformed = df.select({lit}.cast("string").alias("z"), col("name"))',
    ),
    "withColumn": (
        'df_transformed = df.withColumn("out", {lit})',
        'df_transformed = df.withColumn("a", {lit}.cast("string")).withColumn("b", trim(col("name")))',
    ),
    "withColumnRenamed": (
        'df_transformed = df.withColumnRenamed({lit}, "renamed")',
        'df_transformed = df.withColumn("out", {lit}.cast("string")).withColumnRenamed("name", "renamed")',
    ),
    "select": (
        "df_transformed = df.select({lit})",
        'df_transformed = df.withColumn("out", {lit}.cast("string")).select("out")',
    ),
    "drop": (
        "df_transformed = df.drop({lit})",
        'df_transformed = df.withColumn("out", {lit}.cast("string")).drop("name")',
    ),
    "filter": (
        "df_transformed = df.filter({lit}.isNull())",
        'df_transformed = df.filter({lit}.cast("string").isNotNull())',
    ),
    # p42 gap 2 widened these two from filter-only predicates to general boolean
    # EXPRESSIONS (`pyspark_to_pandas.py:_NULL_PREDICATES`), which added guard
    # site 8 of 8 and put `notna`/`isna` into `_SERIES_ONLY_MEMBERS`. The
    # filter-position shapes below refuse inside `_filter_mask` — naming
    # `filter`, before any receiver is translated — so on their own they never
    # reach the new site. The `withColumn` shapes are what exercise it: the
    # scalar variants hit the guard, and the `.cast("string")` variants take Arm
    # A's broadcast and CONVERT, so the emission scan actually runs over a
    # predicate emission rather than only over a refusal.
    "isNull": (
        "df_transformed = df.filter({lit}.isNull())",
        'df_transformed = df.filter({lit}.cast("string").isNull())',
        'df_transformed = df.withColumn("out", {lit}.isNull())',
        'df_transformed = df.withColumn("out", {lit}.cast("string").isNull())',
    ),
    "isNotNull": (
        "df_transformed = df.filter({lit}.isNotNull())",
        'df_transformed = df.filter({lit}.cast("string").isNotNull())',
        'df_transformed = df.withColumn("out", {lit}.isNotNull())',
        'df_transformed = df.withColumn("out", {lit}.cast("string").isNotNull())',
    ),
    "rlike": (
        'df_transformed = df.filter({lit}.rlike("a"))',
        'df_transformed = df.filter({lit}.cast("string").rlike("a"))',
    ),
}

CONSTRUCT_NAMES = tuple(sorted(CONSTRUCT_STATEMENTS))

#: The whole grid, for the deterministic coverage guards below. The property
#: itself draws from the product rather than sampling this list, so Hypothesis
#: shrinks to a construct/constant pair rather than to an index.
ALL_CASES = tuple(
    (name, template, literal)
    for name in CONSTRUCT_NAMES
    for template in CONSTRUCT_STATEMENTS[name]
    for literal in LITERAL_SOURCES
)


def render(template: str, literal: str) -> str:
    return template.format(lit=literal)


def outcome(statement: str) -> "tuple[str, str | None]":
    """``("refused", <construct>)`` or ``("converted", <emitted code>)``."""
    try:
        result = PySpark2PandasConverter.convert(statement)
    except ConversionError as exc:
        return "refused", exc.construct
    return "converted", result["code"]


def sweep() -> dict:
    """Deterministic pass over the whole grid, for the coverage guards.

    Kept as a function rather than inlined so the same numbers can be reported
    outside pytest without re-deriving the grid.
    """
    refused = 0
    converted = 0
    syntax_refusals: list[str] = []
    offenders: list[tuple[str, str]] = []
    for _name, template, literal in ALL_CASES:
        statement = render(template, literal)
        kind, payload = outcome(statement)
        if kind == "refused":
            refused += 1
            if payload == "python_syntax":
                syntax_refusals.append(statement)
        else:
            converted += 1
            found = series_only_members_on_scalars(payload or "")
            offenders.extend((statement, offender) for offender in found)
    return {
        "cases": len(ALL_CASES),
        "constructs": len(CONSTRUCT_NAMES),
        "refused": refused,
        "converted": converted,
        "syntax_refusals": syntax_refusals,
        "offenders": offenders,
    }


# ──────────────────────────────────────────────────────────────────────
# Non-vacuity guards.
#
# A property that generates nothing, or whose every case refuses for a reason
# unrelated to the receiver's shape, proves nothing while staying green. These
# three make each of those failure modes visible, deterministically, over the
# WHOLE grid rather than over a sample.
# ──────────────────────────────────────────────────────────────────────


class TestTheGeneratedSpaceIsNotVacuous:
    """**Validates: Requirements R3**"""

    def test_every_supported_construct_has_a_generated_shape(self):
        """The quantifier is total: no name in either frozenset is skipped.

        Read from the module, so a construct added to `_SUPPORTED_FUNCTIONS` or
        `_SUPPORTED_METHODS` later fails here until it is given a shape — which
        is what keeps R3's "a construct added later" clause checkable rather
        than aspirational.
        """
        missing = SUPPORTED_CONSTRUCTS - set(CONSTRUCT_NAMES)
        assert missing == set(), f"supported constructs with no generated lit(...) shape: {sorted(missing)}"
        stray = set(CONSTRUCT_NAMES) - SUPPORTED_CONSTRUCTS
        assert stray == set(), f"shapes for names the converter does not support: {sorted(stray)}"

    def test_no_generated_statement_refuses_for_a_syntax_reason(self):
        """Every rendered snippet is valid Python, and none refuses as one.

        A `SyntaxError` in a generated snippet would refuse — the property's
        legal outcome — for a reason that has nothing to do with a scalar
        receiver, which is how a generative test goes quietly vacuous.
        """
        for name, template, literal in ALL_CASES:
            statement = render(template, literal)
            try:
                ast.parse(statement)
            except SyntaxError as exc:  # pragma: no cover - a template bug, not a converter finding
                pytest.fail(f"{name}: generated statement is not valid Python ({exc}): {statement}")
        report = sweep()
        assert report["syntax_refusals"] == [], f"cases refused as python_syntax: {report['syntax_refusals']}"

    def test_the_grid_both_refuses_and_converts(self):
        """Both outcomes occur, so both halves of the disjunction are exercised.

        If everything refused, the emission scan would never run and the
        property would be a restatement of the fence. The converted half is
        Arm A's broadcast and the constructs that compose over it.
        """
        report = sweep()
        assert report["cases"] >= 100, f"only {report['cases']} generated cases"
        assert report["refused"] > 0, "nothing refuses — the fence is not being reached"
        assert report["converted"] >= 20, f"only {report['converted']} cases convert; the scan is barely exercised"


# ──────────────────────────────────────────────────────────────────────
# Property 4.
# ──────────────────────────────────────────────────────────────────────

#: Drawn as a product — construct, then one of that construct's shapes, then the
#: constant — rather than as an index into `ALL_CASES`, so a counterexample
#: shrinks to a construct and a constant a reader can act on.
cases = st.sampled_from(CONSTRUCT_NAMES).flatmap(
    lambda name: st.tuples(
        st.just(name),
        st.sampled_from(CONSTRUCT_STATEMENTS[name]),
        st.sampled_from(LITERAL_SOURCES),
    )
)

PROP_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@PROP_SETTINGS
@given(case=cases)
def test_no_emission_lands_a_series_only_member_on_a_scalar(case: "tuple[str, str, str]"):
    """Refuse, or emit pandas with no Series-only member on a scalar (R3).

    The two legal outcomes are a disjunction, not a preference. A refusal is
    correct — that is the fence doing its job. Arm A's
    `pd.Series(<const>, index=df.index, dtype="string")` broadcast is equally
    correct: a `pd.Series(...)` is a Series, so a `.rename(...)` or `.astype(...)`
    on top of it is a member on a Series and this property has nothing to say
    about it.

    What is forbidden is the third outcome, the one this whole spec is about: a
    conversion that **succeeds** and emits pandas which raises in the Code
    Interpreter because a Series-only member was appended to something that
    evaluates to a scalar.

    `event()` records the construct and the outcome, so
    `--hypothesis-show-statistics` reports which half of the disjunction each
    construct actually took rather than leaving coverage assumed. The
    deterministic guards above cover the whole grid; this reports the sample.

    Validates: Requirements R3
    """
    name, template, literal = case
    statement = render(template, literal)
    kind, payload = outcome(statement)
    event(f"construct={name}")
    event(f"outcome={kind}")

    if kind == "refused":
        # A refusal is legal, but it must be a refusal ABOUT the construct — a
        # snippet that refuses as `python_syntax` says nothing about the fence.
        assert payload != "python_syntax", f"generated snippet did not parse: {statement}"
        assert payload, f"refused with no construct named: {statement}"
        return

    emitted = payload or ""
    offenders = series_only_members_on_scalars(emitted)
    assert offenders == [], (
        f"{name}: conversion succeeded but applies a Series-only member to a scalar.\n"
        f"  input:     {statement}\n"
        f"  emitted:   {emitted}\n"
        f"  offending: {offenders}"
    )


# ──────────────────────────────────────────────────────────────────────
# The detector is capable of firing — and differs from production's.
#
# A property whose detector cannot fail is worth nothing, and "written
# independently" is a claim, not a fact, until something demonstrates the two
# detectors DISAGREE. These pin both.
# ──────────────────────────────────────────────────────────────────────

#: The emissions recorded in task 3.4, verbatim from the counterexamples the
#: UNFIXED converter produced. Each is pandas that raises in the Code
#: Interpreter, and each must be reported here — otherwise this property would
#: have stayed green through the whole of the bug it exists to prevent recurring.
PRE_FIX_EMISSIONS = (
    ('import pandas as pd\ndf_transformed = df.copy(); df_transformed["m"] = None.astype(str)', "None.astype"),
    ('import pandas as pd\ndf_transformed = df.copy(); df_transformed["m"] = "x".str.strip()', "'x'.str"),
    (
        "import pandas as pd\ndf_transformed = df.copy(); "
        'df_transformed["m"] = pd.to_datetime("2020-01-01", errors="coerce").dt.date',
        "pd.to_datetime('2020-01-01', errors='coerce').dt",
    ),
    (
        "import pandas as pd\ndf_transformed = df.copy(); "
        'df_transformed["m"] = pd.to_numeric(0, errors="coerce").astype("Int64")',
        "pd.to_numeric(0, errors='coerce').astype",
    ),
    ('import pandas as pd\ndf_transformed = pd.DataFrame({"z": "x".rename("z")})', "'x'.rename"),
)

#: Emissions that are correct and must NOT be reported: Arm A's broadcast, the
#: members that compose over it, the amended column path, and
#: `withColumnRenamed`'s frame-level `.rename(columns=...)` — which uses one of
#: `_SERIES_ONLY_MEMBERS`' names on a FRAME, and so is the obvious way a
#: careless detector produces a false positive.
LEGAL_EMISSIONS = (
    "import pandas as pd\ndf_transformed = df.copy(); "
    'df_transformed["m"] = pd.Series(None, index=df.index, dtype="string")',
    "import pandas as pd\ndf_transformed = df.copy(); "
    'df_transformed["m"] = pd.Series("x", index=df.index, dtype="string").rename("z")',
    'import pandas as pd\ndf_transformed = df.copy(); df_transformed["m"] = df["a"].astype("string")',
    'import pandas as pd\ndf_transformed = df.rename(columns={"a": "b"})',
    "import pandas as pd\ndf_transformed = df.copy(); "
    'df_transformed["m"] = pd.to_datetime(df["a"], errors="coerce").dt.date',
)


class TestTheDetectorCanFireAndIsNotProductionsDetector:
    """**Validates: Requirements R3**"""

    @pytest.mark.parametrize(("emitted", "expected"), PRE_FIX_EMISSIONS)
    def test_it_reports_the_recorded_pre_fix_emissions(self, emitted, expected):
        """Task 3.4's counterexamples, as the unfixed converter emitted them."""
        assert series_only_members_on_scalars(emitted) == [expected]

    @pytest.mark.parametrize("emitted", LEGAL_EMISSIONS)
    def test_it_stays_quiet_on_the_emissions_that_are_correct(self, emitted):
        """Arm A's Series, the column path, and a frame-level `.rename(columns=)`."""
        assert series_only_members_on_scalars(emitted) == []

    @pytest.mark.parametrize(
        ("emitted", "expected"),
        [
            ('import pandas as pd\ndf_transformed = df.copy(); df_transformed["m"] = None.notna()', "None.notna"),
            ('import pandas as pd\ndf_transformed = df.copy(); df_transformed["m"] = "x".isna()', "'x'.isna"),
        ],
    )
    def test_the_p42_members_are_load_bearing(self, emitted, expected):
        """p42 gap 2 added ``notna``/``isna`` to `_SERIES_ONLY_MEMBERS`, and this
        is what makes that widening more than a comment: both forms raise
        ``AttributeError`` in the Code Interpreter and both are now reported. The
        pair below shows the widening does not over-fire — a predicate on a real
        Series stays quiet."""
        assert series_only_members_on_scalars(emitted) == [expected]

    @pytest.mark.parametrize(
        "emitted",
        [
            'import pandas as pd\ndf_transformed = df.copy(); df_transformed["m"] = df["a"].notna()',
            "import pandas as pd\ndf_transformed = df.copy(); "
            'df_transformed["m"] = pd.to_numeric(df["a"], errors="coerce").astype("Int64").notna()',
            "import pandas as pd\ndf_transformed = df.copy(); "
            'df_transformed["m"] = pd.Series("x", index=df.index, dtype="string").isna()',
        ],
    )
    def test_the_p42_members_do_not_over_fire_on_a_series(self, emitted):
        """The three receiver shapes p42's own tests pin: a column, a numeric
        cast, and Arm A's broadcast."""
        assert series_only_members_on_scalars(emitted) == []

    def test_it_disagrees_with_production_on_an_unrecognised_scalar_helper(self):
        """The independence claim, demonstrated rather than asserted.

        `pd.timedelta_range(0)` stands in for the class the permissive default
        leaves open: a `pd.*` helper that yields a scalar and is not in
        `_SCALAR_PRESERVING_HELPERS`. Production does not recognise it as a
        scalar, so the fence would let a member land on it; this detector does
        not recognise it as a Series, so the property reports it.

        `_is_scalar_emission` is read HERE, in this one guard, only to show the
        two answers differ. The property's own check never calls it — that is the
        whole point, and a check built on it could not fail on this input.
        """
        scalar_form = "pd.timedelta_range(0)"
        assert pyspark_to_pandas._is_scalar_emission(scalar_form) is False, (
            "production now recognises this form; pick another unrecognised scalar helper"
        )
        emitted = (
            f'import pandas as pd\ndf_transformed = df.copy(); df_transformed["m"] = {scalar_form}.astype("Int64")'
        )
        assert series_only_members_on_scalars(emitted) == ["pd.timedelta_range(0).astype"]
