# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p42 gap 2 — ``isNull`` / ``isNotNull`` as a general boolean EXPRESSION.

**Validates: Spark's null-predicate semantics over any column expression (gap 2)**

══════════════════════════════════════════════════════════════════════
ORACLE RULE FOR THIS FILE: SPARK SEMANTICS FOR THE CONVERSION CLAIMS.
CURRENT CONVERTER OUTPUT IS THE ORACLE **ONLY** IN THE TWO CLASSES THAT
SAY SO IN THEIR NAME. READ THIS BEFORE EDITING.
══════════════════════════════════════════════════════════════════════
`LGR-28b4d75-5` (template / ratified): *a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle.* This directory runs two opposite rules, so every new file has to say
which one it follows. Gap 1's `test_bare_when_null_fallback.py` is the model for
the split, and this file follows it.

**Rule A — the oracle is Spark.** :class:`TestNullPredicateOverAColumn`,
:class:`TestNullPredicateOverAnExpression`, :class:`TestNullPredicateInsideWhen`,
:class:`TestTheLiveTransformConverts`, :class:`TestScalarReceiverRefusesUnderP40sFence`.
PySpark's ``e.isNull()`` is true exactly where ``e`` is NULL and
``e.isNotNull()`` is its complement, **for any column expression ``e``** — the
receiver is not restricted to a bare column. pandas spells those two
``.isna()`` and ``.notna()``, which are Series methods and therefore compose
onto whatever the receiver emitted. Every expectation in those classes is
written from those two sentences plus this module's already-pinned emission for
the receiver (``col`` → ``df["x"]``, a numeric cast → ``pd.to_numeric(...)
.astype("Int64")``, ``trim`` → ``.str.strip()``), never from reading what
``convert()`` returns.

EXPECTED RESULT BEFORE THE FIX: **RED on all of Rule A except**
:class:`TestScalarReceiverRefusesUnderP40sFence`, which is red for a different
and weaker reason — the shapes refuse today too, but naming the wrong construct.

**Rule B — current output IS the oracle, legitimately.**
:class:`TestTheFilterPathIsUnchanged` and :class:`TestRlikeStaysRefusingOutsideFilter`
are *preservation* claims. There is no better oracle for "unchanged" than the
current answers, and they were captured by calling the real converter against
the working tree before this file was written. They are **GREEN before the fix**
and must stay green after it.

WHY ``rlike`` IS OUT OF SCOPE, pinned rather than described.
:class:`TestRlikeStaysRefusingOutsideFilter` holds the third member of the
branch this change splits. ``rlike`` needs a pattern argument passed through the
``_raw`` fence, and p41 (`LGR-f6519e1-1`) has just reworked that exact emission
site — ``_filter_mask``'s ``rlike`` arm is the ONLY place an ``rlike`` pattern
reaches the fence, which is what bounds that record's blast radius to one call
site. Widening ``rlike`` to expression position would create a second such site
and move the boundary p41 just drew. It stays refusing, and this class is what
says so out loud.

WHY THE PARITY CLASS EXISTS. :class:`TestBothPathsAgree` asserts that
``_filter_mask``'s ``filter()`` rule and the new expression rule emit the SAME
text for the shapes both can express. Two rules for one construct is the
duplication class this repo dislikes; ``_filter_mask`` is left untouched on
purpose (it carries baseline pins and p41 just changed its ``rlike`` arm), so
the parity test is what keeps the two honest — the same arrangement
`test_declared_source_fields_parity.py` and `test_unsourced_definition_parity.py`
use for the cross-Lambda copies.

WHAT THIS FILE DOES NOT CLAIM. It does not claim the emitted dtype matches
Spark's. ``np.where`` with the `_WHEN_NULL_FALLBACK` ``np.nan`` widens an
integer column to float — an ACCEPTED COST already recorded at
`pyspark_to_pandas.py:_WHEN_NULL_FALLBACK` and in gap 1's file. pandas is not
installed in `agentic_data_ingestion/.venv` and this spec does not add it
(`LGR-4dd9b42-1`'s environment constraint), so the assertions here are over
emitted TEXT, and the dtype consequence is reasoned about in the design rather
than measured here.
"""

import os
import sys

import pytest

# Add Lambda source to path (same shape as the rest of this directory).
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter


def convert_code(code: str) -> str:
    """Convert and return just the pandas code."""
    return PySpark2PandasConverter.convert(code)["code"]


def constructs(code: str) -> list:
    """``constructs_converted`` for *code*."""
    return PySpark2PandasConverter.convert(code)["conversion_report"]["constructs_converted"]


def refusal(code: str) -> ConversionError:
    """Convert *code*, requiring a refusal, and return it."""
    with pytest.raises(ConversionError) as caught:
        PySpark2PandasConverter.convert(code)
    return caught.value


class TestNullPredicateOverAColumn:
    """Rule A — the simplest receiver: a bare ``col("x")``.

    ``col("x")`` emits ``df["x"]``, so ``col("x").isNull()`` is
    ``df["x"].isna()`` and ``isNotNull`` is ``df["x"].notna()``. Both are
    written from Spark's definition of the two predicates, not from the
    converter — and deliberately spelled identically to what ``_filter_mask``
    already emits in filter position, because a construct that means one thing
    should not emit two different texts (see :class:`TestBothPathsAgree`).
    """

    def test_isnull_on_a_bare_column(self):
        assert 'df["email"].isna()' in convert_code('flag = col("email").isNull()')

    def test_isnotnull_on_a_bare_column(self):
        assert 'df["email"].notna()' in convert_code('flag = col("email").isNotNull()')

    def test_isnotnull_inside_with_column(self):
        """``withColumn`` is the position the generator actually emits."""
        code = 'df_transformed = df.withColumn("has_email", col("email").isNotNull())'
        assert 'df_transformed["has_email"] = df["email"].notna()' in convert_code(code)

    def test_isnull_inside_with_column(self):
        code = 'df_transformed = df.withColumn("missing_email", col("email").isNull())'
        assert 'df_transformed["missing_email"] = df["email"].isna()' in convert_code(code)

    def test_the_emitted_block_is_valid_python(self):
        """A refusal is honest; emitted code that does not compile is not."""
        code = 'df_transformed = df.withColumn("has_email", col("email").isNotNull())'
        compile(convert_code(code), "<converted>", "exec")

    @pytest.mark.parametrize("predicate", ["isNull", "isNotNull"])
    def test_an_argument_to_either_predicate_refuses(self, predicate):
        """Spark's ``isNull``/``isNotNull`` take no arguments, and pandas'
        ``isna``/``notna`` take none either. A call carrying one is not the
        construct this rule expresses, so it refuses naming itself rather than
        silently dropping the argument."""
        assert refusal(f'flag = col("a").{predicate}("x")').construct == predicate


class TestNullPredicateOverAnExpression:
    """Rule A — the receiver is an EXPRESSION, which is the whole gap.

    ``_filter_mask``'s rule resolves a column NAME off the receiver and so can
    only ever see a bare ``col("x")``. Spark places no such restriction:
    ``e.isNotNull()`` is defined for any column expression, and because pandas'
    ``.notna()`` is a Series method it composes onto whatever the receiver
    emitted. These are the shapes the old rule could not reach even in filter
    position.
    """

    def test_isnotnull_on_a_numeric_cast(self):
        """The receiver shape from the live incident. The cast's emission is
        pinned byte-exactly by four baseline cases, so the only new text here is
        the appended ``.notna()``."""
        result = convert_code('flag = col("customer_id").cast(IntegerType()).isNotNull()')
        assert 'pd.to_numeric(df["customer_id"], errors="coerce").astype("Int64").notna()' in result

    def test_isnull_on_a_numeric_cast(self):
        result = convert_code('flag = col("customer_id").cast(IntegerType()).isNull()')
        assert 'pd.to_numeric(df["customer_id"], errors="coerce").astype("Int64").isna()' in result

    def test_isnotnull_on_a_string_cast(self):
        result = convert_code('flag = col("name").cast("string").isNotNull()')
        assert 'df["name"].astype("string").notna()' in result

    def test_isnotnull_on_a_trim(self):
        assert 'df["name"].str.strip().notna()' in convert_code('flag = trim(col("name")).isNotNull()')

    def test_isnull_on_a_nested_string_composition(self):
        """`LGR-5c33c04-1`'s nested composition, with a predicate on top."""
        result = convert_code('flag = lower(trim(col("name"))).isNull()')
        assert 'df["name"].str.strip().str.lower().isna()' in result

    def test_isnotnull_on_a_to_date(self):
        result = convert_code('flag = to_date(col("order_date")).isNotNull()')
        assert 'pd.to_datetime(df["order_date"], errors="coerce").dt.date.notna()' in result

    def test_a_refused_receiver_still_refuses_naming_the_receiver(self):
        """2.6's rule, reached through the new site: a broken receiver is never
        carried into a surrounding expression that converts. ``initcap`` is in
        `_UNSUPPORTED_FUNCTIONS`, so it refuses naming ``initcap`` — not
        ``isNotNull``, which is perfectly expressible."""
        assert refusal('flag = initcap(col("name")).isNotNull()').construct == "initcap"


class TestNullPredicateInsideWhen:
    """Rule A — inside a ``when`` condition, which is the path that had no route.

    A ``when`` condition goes through ``_expr`` (via ``_when_branch``), never
    through ``_filter_mask``, so this is the position the old refusal made
    unreachable. Gap 1 made the bare ``when`` convert; this is what makes its
    condition able to be a null test.
    """

    def test_isnotnull_as_a_bare_when_condition(self):
        result = convert_code('df_t = df.withColumn("f", when(col("a").isNotNull(), "present"))')
        assert 'np.where(df["a"].notna(), "present", np.nan)' in result

    def test_isnull_as_a_bare_when_condition(self):
        result = convert_code('df_t = df.withColumn("f", when(col("a").isNull(), "missing"))')
        assert 'np.where(df["a"].isna(), "missing", np.nan)' in result

    def test_isnotnull_as_a_paired_when_condition(self):
        """The ``.otherwise(...)`` form takes the same condition path."""
        result = convert_code('df_t = df.withColumn("f", when(col("a").isNotNull(), "y").otherwise("n"))')
        assert 'np.where(df["a"].notna(), "y", "n")' in result

    def test_isnull_in_a_chained_when_without_otherwise(self):
        code = 'df_t = df.withColumn("f", when(col("a").isNull(), "x").when(col("b").isNotNull(), "y"))'
        result = convert_code(code)
        assert 'np.where(df["a"].isna(), "x", np.where(df["b"].notna(), "y", np.nan))' in result

    def test_the_when_block_is_valid_python(self):
        code = 'df_t = df.withColumn("f", when(col("a").isNotNull(), "y"))'
        compile(convert_code(code), "<converted>", "exec")


class TestTheLiveTransformConverts:
    """Rule A — the payoff, traced to the incident that motivated it.

    The exact generated line from pipeline ``latesttest-50e07536`` (generated
    line 5), byte-for-byte. The repo already ties tests to pipeline ids this way
    for ``newtest-ab2a08a0`` and ``run-2026-08-09-15-38-19-5c5f5f8a``.

    Note the receiver is ``col("customer_id").cast(IntegerType())`` — an
    EXPRESSION, not a bare column — so ``_filter_mask``'s name resolution could
    not have handled it even if the condition had reached ``_filter_mask``. That
    is why gap 2 is a new expression rule and not a widening of the filter rule.

    ON SEMANTICS, stated rather than assumed: ``when(X.isNotNull(), X)`` is a
    no-op in Spark — it yields ``X`` where ``X`` is non-null and NULL elsewhere,
    which is just ``X``. The converted pandas is therefore expected to be
    *value*-equivalent to the cast alone. The emitted ``np.where(...)`` wrapper
    is redundant, not wrong: it reproduces the redundancy that is in the
    generated source. Its one real consequence is dtype, the accepted
    `_WHEN_NULL_FALLBACK` cost.
    """

    LIVE_LINE = (
        'df_transformed = df.withColumn("customer_id", '
        'when(col("customer_id").cast(IntegerType()).isNotNull(), col("customer_id").cast(IntegerType())))'
    )

    #: The receiver's emission, pinned byte-exactly by the `cast-IntegerType`
    #: baseline case — so it is quoted here rather than re-derived.
    CAST = 'pd.to_numeric(df["customer_id"], errors="coerce").astype("Int64")'

    def test_the_live_line_converts(self):
        """It refused on ``isNotNull`` before gap 2 (pinned by gap 1's file)."""
        assert PySpark2PandasConverter.convert(self.LIVE_LINE)["code"]

    def test_the_live_lines_emission_is_the_condition_over_the_same_cast(self):
        expected = f'df_transformed["customer_id"] = np.where({self.CAST}.notna(), {self.CAST}, np.nan)'
        assert expected in convert_code(self.LIVE_LINE)

    def test_the_live_lines_emission_compiles(self):
        compile(convert_code(self.LIVE_LINE), "<converted>", "exec")

    def test_the_live_line_reports_the_constructs_its_source_contains(self):
        """``cast``, ``col``, ``when`` and ``withColumn`` are in the source and
        are reported. ``isNotNull`` is deliberately NOT in `_REPORTED_METHODS`
        and so contributes nothing — see the design's trap-1 decision. This
        asserts the decision rather than leaving it implicit."""
        reported = set(constructs(self.LIVE_LINE))
        assert {"cast", "col", "when", "withColumn"} <= reported
        assert "isNotNull" not in reported
        assert "when_otherwise" not in reported


class TestScalarReceiverRefusesUnderP40sFence:
    """Rule A — p40's receiver-shape fence, `LGR-4dd9b42-1` (template/ratified).

    ``.notna()`` and ``.isna()`` do not exist on a Python scalar — ``None.notna()``
    and ``"x".isna()`` both raise ``AttributeError`` — so the new emission site is
    a `_SERIES_ONLY_MEMBERS` site and must go through ``_require_series`` like the
    other seven. The asymmetry IS the fence working:
    ``col("x").cast(IntegerType()).isNotNull()`` converts (a Series receiver)
    while ``lit(5).isNotNull()`` refuses (a scalar receiver).

    The construct named is ``isNull``/``isNotNull`` — the construct that OWNS the
    member, never the ``lit(...)`` that produced the scalar, which is
    `LGR-4dd9b42-1`'s own rule and `_require_series`'s documented contract. These
    shapes refuse today as well, but naming themselves only by accident of the
    blanket branch; after the change they name themselves because the fence
    fired, and the refusal REASON becomes the scalar-literal one.
    """

    @pytest.mark.parametrize("literal", ["lit(5)", "lit(1.5)", "lit(True)", "lit(None)", 'lit("x")'])
    @pytest.mark.parametrize("predicate", ["isNull", "isNotNull"])
    def test_a_scalar_literal_receiver_refuses_naming_the_predicate(self, literal, predicate):
        assert refusal(f"flag = {literal}.{predicate}()").construct == predicate

    @pytest.mark.parametrize("predicate", ["isNull", "isNotNull"])
    def test_the_refusal_reason_is_the_scalar_literal_one(self, predicate):
        """`LGR-4dd9b42-1` added a third refusal reason for exactly this class;
        `code_interpreter_runner.classify_conversion_refusal` reads it."""
        from pyspark_to_pandas import REFUSAL_REASON_SCALAR_LITERAL

        assert refusal(f"flag = lit(5).{predicate}()").reason == REFUSAL_REASON_SCALAR_LITERAL

    @pytest.mark.parametrize("predicate", ["isNull", "isNotNull"])
    def test_a_numeric_cast_of_a_literal_still_refuses(self, predicate):
        """p40's numeric-cast arm keeps the emission scalar
        (`pd.to_numeric(0, …)` → ``0``), so the fence still fires — and names
        ``cast``, the construct that owns THAT member, which is reached first."""
        assert refusal(f'flag = lit(0).cast("int").{predicate}()').construct == "cast"

    @pytest.mark.parametrize("predicate", ["isNull", "isNotNull"])
    def test_a_string_cast_of_a_literal_converts_because_it_broadcasts(self, predicate):
        """p40 Arm A's one converting family: the string cast of a string/None
        literal broadcasts to a real Series, so a predicate on top of it is a
        member on a Series and there is nothing for the fence to refuse."""
        member = "isna" if predicate == "isNull" else "notna"
        result = convert_code(f'flag = lit("x").cast("string").{predicate}()')
        assert f'pd.Series("x", index=df.index, dtype="string").{member}()' in result

    @pytest.mark.parametrize("predicate", ["isNull", "isNotNull"])
    def test_a_scalar_receiver_inside_a_when_condition_refuses(self, predicate):
        """The fence reaches through ``when``: gap 1 made the condition
        translate, so a scalar receiver in the condition now hits the guard
        instead of the old blanket refusal."""
        assert refusal(f'df_t = df.withColumn("f", when(lit(5).{predicate}(), "y"))').construct == predicate


class TestBothPathsAgree:
    """PARITY — ``_filter_mask``'s rule and the new expression rule must not drift.

    ``_filter_mask`` (`pyspark_to_pandas.py:1779`) is left untouched by design: it
    carries baseline pins and p41 just reworked its ``rlike`` arm. That leaves two
    code paths able to express ``col("x").isNotNull()``. For every shape BOTH can
    express, the emitted predicate text must be identical, so a future change to
    either rule cannot quietly make one spelling mean something else.

    Only bare-``col`` shapes appear here, because that is exactly the set
    ``_filter_mask`` can express — it resolves a column NAME off its receiver.
    The expression receivers in :class:`TestNullPredicateOverAnExpression` have no
    filter-path counterpart to compare against, which is the gap itself.
    """

    @pytest.mark.parametrize(("predicate", "member"), [("isNull", "isna"), ("isNotNull", "notna")])
    def test_both_paths_emit_the_same_predicate_text(self, predicate, member):
        via_filter = convert_code(f'df_t = df.filter(col("email").{predicate}())')
        via_expression = convert_code(f'df_t = df.withColumn("f", col("email").{predicate}())')
        expected = f'df["email"].{member}()'
        assert expected in via_filter
        assert expected in via_expression

    @pytest.mark.parametrize("predicate", ["isNull", "isNotNull"])
    def test_the_two_paths_differ_only_in_the_frame_operation(self, predicate):
        """Stronger than "both contain the text": the whole emitted statement is
        the same predicate wrapped by each path's own frame operation, so a
        divergence anywhere in the predicate fails here."""
        mask = convert_code(f'df_t = df.filter(col("email").{predicate}())')
        column = convert_code(f'df_t = df.withColumn("f", col("email").{predicate}())')
        predicate_text = 'df["email"].isna()' if predicate == "isNull" else 'df["email"].notna()'
        assert mask.endswith(f"df_t = df[{predicate_text}]")
        assert column.endswith(f'df_t = df.copy(); df_t["f"] = {predicate_text}')


class TestTheFilterPathIsUnchanged:
    """Rule B — preservation. ``_filter_mask`` is not refactored, so its answers
    must be byte-identical. Four of these are pinned in the 79-case baseline as
    well; they are restated here so a reader of this file can see the boundary.
    """

    def test_filter_isnotnull(self):
        assert '[df["email"].notna()]' in convert_code('df.filter(col("email").isNotNull())')

    def test_filter_isnull(self):
        assert '[df["phone"].isna()]' in convert_code('df.filter(col("phone").isNull())')

    def test_filter_assignment_binds_the_mask_to_the_source(self):
        result = convert_code('df_transformed = df_transformed.filter(col("email").isNotNull())')
        assert 'df_transformed = df_transformed[df_transformed["email"].notna()]' in result

    def test_filter_reports_col_and_filter_only(self):
        """The trap-1 decision, pinned on the shape that would have moved: the
        four baseline cases carrying an ``isNull``/``isNotNull`` filter report
        ``["col", "filter"]``, and adding the predicates to `_REPORTED_METHODS`
        would have changed all four. They are not added, so this is unchanged."""
        assert sorted(constructs('df.filter(col("email").isNotNull())')) == ["col", "filter"]

    def test_a_non_col_receiver_in_filter_position_still_refuses_naming_filter(self):
        """``_filter_mask``'s own receiver check is untouched: it refuses naming
        ``filter``, NOT naming the predicate. The new expression rule does not
        reach into filter position."""
        assert refusal('df_t = df.filter(lit(5).isNotNull())').construct == "filter"

    def test_a_cast_receiver_in_filter_position_still_refuses_naming_filter(self):
        """The receiver shape from the live incident, in filter position. It is
        STILL a refusal — gap 2 deliberately does not widen ``_filter_mask``, so
        this shape's route is a ``withColumn``, not a ``filter``. Characterization,
        not endorsement."""
        assert refusal('df_t = df.filter(col("a").cast(IntegerType()).isNotNull())').construct == "filter"


class TestRlikeStaysRefusingOutsideFilter:
    """Rule B — the third member of the split branch stays exactly as it was.

    ``rlike`` is OUT OF SCOPE for gap 2. p41 (`LGR-f6519e1-1`) established that
    ``_filter_mask``'s ``rlike`` arm is the only site handing an ``rlike`` pattern
    to the ``_raw`` fence, which is what bounds that record's blast radius to one
    call site. Converting ``rlike`` in expression position would create a second
    such site. These pins are what make "out of scope" checkable.
    """

    def test_rlike_in_expression_position_refuses_naming_rlike(self):
        assert refusal('flag = col("email").rlike("@")').construct == "rlike"

    def test_rlike_inside_with_column_refuses_naming_rlike(self):
        assert refusal('df_t = df.withColumn("f", col("email").rlike("@"))').construct == "rlike"

    def test_rlike_in_a_when_condition_refuses_naming_rlike(self):
        """Gap 1 made the condition translate, so this reaches ``_method_call``
        and refuses there — the same place it refused before."""
        assert refusal('df_t = df.withColumn("f", when(col("email").rlike("@"), "y"))').construct == "rlike"

    def test_rlike_in_filter_position_still_converts(self):
        """p41's emission, unmoved."""
        result = convert_code('df_t = df.filter(col("email").rlike("@"))')
        assert 'df["email"].str.contains(r"@", na=False)' in result
