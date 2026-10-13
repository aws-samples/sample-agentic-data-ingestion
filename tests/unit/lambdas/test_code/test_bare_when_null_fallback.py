# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p42 — a bare ``when(cond, value)`` with no ``.otherwise(...)`` converts.

**Validates: Spark's ``when`` null-fallback semantics (gap 1)**

══════════════════════════════════════════════════════════════════════
ORACLE RULE FOR THIS FILE: SPARK SEMANTICS. CURRENT CONVERTER OUTPUT IS
**NEVER** CONSULTED FOR THE CONVERSION CLAIMS. READ THIS BEFORE EDITING.
══════════════════════════════════════════════════════════════════════
`LGR-28b4d75-5` (template / ratified): *a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle.* This directory runs two opposite rules, so every new file has to say
which one it follows.

**Rule A — :class:`TestBareWhenConverts`, :class:`TestChainedWhenWithoutOtherwise`,
:class:`TestBareWhenReportsWhen`, :class:`TestRefusalMovesToTheHonestOwner`: the
oracle is Spark.** PySpark's ``when(c, v)`` with no ``.otherwise(...)`` yields
``v`` where ``c`` is true and **NULL** everywhere else. So the expected emission
is the same nested ``np.where`` the paired form already produces, with the
fallback fixed at null instead of user-supplied. Every expectation in those
classes is written from that sentence, not from reading what ``convert()``
returns. EXPECTED RESULT BEFORE THE FIX: **RED on all four** — the converter
refuses ``construct="when"`` today.

:class:`TestRefusalMovesToTheHonestOwner` is Rule A for a reason worth stating,
because it is easy to mistake for preservation. Today ``when`` refuses at
``_function_call`` **before its condition or value is ever translated**, so
``when`` is the name reported for every one of those shapes. After the fix the
sub-expressions ARE translated, so each refuses at its own real obstacle and
names *that* construct. The shapes keep refusing either way; what changes is
that the reported construct stops being ``when`` and starts being the operation
that actually cannot be expressed — p41's ownership rule (`LGR-f6519e1-1`'s D2a
sites), reached here as a consequence rather than as a new mechanism.

**Rule B — :class:`TestRefusalsThatMustNotMove`: current output IS the oracle,
legitimately.** Those are *preservation* claims — shapes that refuse today and
must keep refusing with the SAME construct named, plus the paired form's
emission and report. There is no better oracle for "unchanged" than the current
answers, and they were captured by calling the real converter against the
working tree before this file was written. They are **GREEN before the fix** and
must stay green after it. (Verified on the pre-fix run: 6 items in that class
passed, plus the two paired-form preservation tests that are housed in the Rule A
classes beside the shapes they contrast with —
``test_the_paired_form_is_byte_identical_to_before`` and
``test_the_paired_form_still_reports_when_otherwise`` — for 8 green in total.)

WHY ``np.nan`` AND NOT ``pd.NA`` / ``None`` — measured, not preferred. The
fallback is an *element* handed to ``np.where``, not a dtype, and all three
candidates report correctly under ``isna()`` (so none of them inflates the
harness's ``completeness`` metric the way p40's `F6`/`F10` ``astype(str)`` did).
They differ in the dtype ``np.where`` then produces over a NUMERIC column:
``np.nan`` yields ``float64``, while ``None`` and ``pd.NA`` both force
**object**, which stops the column being numeric at all. Measured in throwaway
venvs OUTSIDE the repo (pandas 2.2.3 / numpy 2.5.2 and pandas 3.0.5 / numpy
2.5.2), following p40's precedent — pandas is **not** installed in
`agentic_data_ingestion/.venv`, is not in `pyproject.toml`, and this spec does
not add it (`LGR-4dd9b42-1`'s environment constraint). The accepted cost is
recorded rather than hidden: ``np.nan`` widens an integer column to float, where
Spark would give a null inside an int column.

WHAT THIS FILE DELIBERATELY DID NOT DO — AND WHAT GAP 2 THEN DID. Gap 1 did not
touch ``isNull`` / ``isNotNull``, which were supported only as filter predicates,
so the live transform that motivated this spec — ``isNotNull`` inside the ``when``
condition — still refused after gap 1, on ``isNotNull`` rather than on ``when``.
That boundary was asserted rather than described, and gap 2 has now moved it:
``isNull``/``isNotNull`` are general boolean expressions over any convertible
receiver (`pyspark_to_pandas.py:_NULL_PREDICATES`), so the live transform
CONVERTS. The one assertion that encoded the old boundary,
:meth:`TestRefusalMovesToTheHonestOwner.test_isnotnull_in_a_when_condition_now_converts`,
was **amended in place** rather than deleted, and its docstring records the
reversal (`LGR-60c4985-6` / `LGR-5906ec6-2`). ``rlike`` is still filter-position
only; ``test_null_predicate_expressions.py`` owns both the new emission and that
remaining boundary.
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


class TestBareWhenConverts:
    """Rule A — the oracle is Spark's null fallback, not converter output."""

    def test_bare_when_with_a_literal_value(self):
        """``when(c, "y")`` is ``"y"`` where c holds, null elsewhere."""
        result = convert_code('when(col("status") == "active", "yes")')
        assert 'np.where(df["status"] == "active", "yes", np.nan)' in result

    def test_bare_when_with_a_column_value(self):
        """The value may be a column, exactly as in the paired form."""
        result = convert_code('when(col("status") == "active", col("status"))')
        assert 'np.where(df["status"] == "active", df["status"], np.nan)' in result

    def test_bare_when_inside_with_column(self):
        """The shape the live pipeline generated — nested in ``withColumn``."""
        code = 'df_transformed = df.withColumn("flag", when(col("status") == "active", "yes"))'
        result = convert_code(code)
        assert 'df_transformed["flag"] = np.where(df["status"] == "active", "yes", np.nan)' in result

    @pytest.mark.parametrize("operator", ["==", "!=", "<", "<=", ">", ">="])
    def test_every_supported_comparison_works_as_the_condition(self, operator):
        """All six `_COMPARE_OPS` members, since the condition goes through
        ``_expr`` and a comparison is the form the generator actually emits."""
        result = convert_code(f'df_t = df.withColumn("f", when(col("a") {operator} 1, "y"))')
        assert f'np.where(df["a"] {operator} 1, "y", np.nan)' in result

    def test_a_scalar_literal_value_does_not_trip_p40s_fence(self):
        """``lit(...)`` as the VALUE is fine: ``np.where`` broadcasts a scalar
        and appends no Series-only member, so `LGR-4dd9b42-1`'s guard is not
        reached. The fence is about members applied to a scalar, not about
        scalars appearing at all."""
        result = convert_code('df_t = df.withColumn("f", when(col("a") > 1, lit("x")))')
        assert 'np.where(df["a"] > 1, "x", np.nan)' in result

    def test_a_string_cast_literal_value_still_broadcasts(self):
        """p40's Arm A composes: the one converting literal-cast family emits a
        Series, so it drops into the value slot unchanged."""
        result = convert_code('df_t = df.withColumn("f", when(col("a") > 1, lit("x").cast("string")))')
        assert 'np.where(df["a"] > 1, pd.Series("x", index=df.index, dtype="string"), np.nan)' in result

    def test_the_emitted_block_is_valid_python(self):
        """A refusal is honest; emitted code that does not compile is not."""
        result = convert_code('df_transformed = df.withColumn("flag", when(col("status") == "active", "yes"))')
        compile(result, "<converted>", "exec")

    def test_the_paired_form_is_byte_identical_to_before(self):
        """The `.otherwise(...)` path must not move — it is pinned in the 79-case
        baseline and three other files."""
        result = convert_code('when(col("status") == "active", "yes").otherwise("no")')
        assert 'np.where(df["status"] == "active", "yes", "no")' in result
        assert "np.nan" not in result


class TestChainedWhenWithoutOtherwise:
    """Rule A — decision 3: ``when().when()`` with no ``.otherwise()``.

    Covered by the same rule rather than left refusing, because the fallback is
    a property of the CHAIN having no ``.otherwise()``, not of how many branches
    it has. Leaving one branch converting and two refusing would be a seam with
    no semantic justification.
    """

    def test_two_branches_no_otherwise_nests_with_a_null_fallback(self):
        code = 'df_t = df.withColumn("f", when(col("a") > 1, "x").when(col("b") < 2, "y"))'
        result = convert_code(code)
        assert 'np.where(df["a"] > 1, "x", np.where(df["b"] < 2, "y", np.nan))' in result

    def test_branch_order_is_first_match_wins(self):
        """Spark takes the FIRST matching branch, so the earliest ``when`` must
        end up as the OUTERMOST ``np.where``."""
        result = convert_code('df_t = df.withColumn("f", when(col("a") > 1, "first").when(col("a") > 0, "second"))')
        assert result.index('"first"') < result.index('"second"')

    def test_three_branches_no_otherwise(self):
        code = 'df_t = df.withColumn("f", when(col("a") > 3, "x").when(col("a") > 2, "y").when(col("a") > 1, "z"))'
        result = convert_code(code)
        expected = 'np.where(df["a"] > 3, "x", np.where(df["a"] > 2, "y", np.where(df["a"] > 1, "z", np.nan)))'
        assert expected in result


class TestBareWhenReportsWhen:
    """Rule A — decision 2: the report names the construct the SOURCE contains.

    `_REPORTED_ALIASES` maps ``when`` to ``when_otherwise`` "because that is the
    pair it belongs to". A bare ``when`` has no ``otherwise``, so reporting the
    pair would name a construct that is not in the source — the misnaming class
    p41 closed for refusals, applied to the conversion report.
    """

    def test_a_bare_when_reports_when(self):
        assert "when" in constructs('df_t = df.withColumn("f", when(col("a") > 1, "y"))')

    def test_a_bare_when_does_not_report_when_otherwise(self):
        assert "when_otherwise" not in constructs('df_t = df.withColumn("f", when(col("a") > 1, "y"))')

    def test_the_paired_form_still_reports_when_otherwise(self):
        """Preservation: three baseline cases pin this spelling."""
        reported = constructs('df_t = df.withColumn("f", when(col("a") > 1, "y").otherwise("n"))')
        assert "when_otherwise" in reported
        assert "when" not in reported

    def test_a_chain_without_otherwise_reports_when(self):
        reported = constructs('df_t = df.withColumn("f", when(col("a") > 1, "x").when(col("b") < 2, "y"))')
        assert "when" in reported
        assert "when_otherwise" not in reported

    def test_both_shapes_in_one_statement_report_both_names(self):
        """The pre-pass is keyed by node identity, so one statement carrying
        both shapes reports both — neither name swallows the other."""
        code = (
            'df_t = df.withColumn("a", when(col("x") > 1, "y")).withColumn("b", when(col("x") > 2, "y").otherwise("n"))'
        )
        reported = constructs(code)
        assert "when" in reported
        assert "when_otherwise" in reported


class TestRefusalMovesToTheHonestOwner:
    """Rule A — the shape keeps refusing; the NAMED construct becomes the real
    obstacle instead of ``when``.

    Red before the fix (``when`` short-circuits before translating its own
    arguments), green after it. See the module header for why this is a
    semantics claim rather than a preservation claim.

    ONE MEMBER HAS SINCE INVERTED, and is kept here rather than moved so the
    reversal sits where the hold was:
    :meth:`test_isnotnull_in_a_when_condition_now_converts` was a refusal claim
    under gap 1 and is a conversion claim under gap 2. Its docstring carries the
    amendment.
    """

    def test_bitwise_and_condition_refuses_naming_col(self):
        """``&`` is not in `_BIN_OPS` (`pyspark_to_pandas.py:666`), so an ``&``
        condition refuses via ``_refuse_here``'s leftmost rule — naming ``col``.
        Measured, not assumed: the PAIRED form already answers ``col`` today
        (pinned in Rule B below), so this change does not open that gap, it
        makes the bare form agree with the paired form."""
        assert refusal('df_t = df.withColumn("f", when((col("a") > 1) & (col("b") < 2), "y"))').construct == "col"

    def test_bitwise_or_condition_refuses_naming_col(self):
        assert refusal('df_t = df.withColumn("f", when((col("a") > 1) | (col("b") < 2), "y"))').construct == "col"

    def test_isnotnull_in_a_when_condition_now_converts(self):
        """**AMENDED BY GAP 2, NOT DELETED.** This assertion was gap 1's stated
        boundary: it read ``assert refusal(code).construct == "isNotNull"`` and
        existed to stop anyone reading gap 1 as having unblocked pipeline
        ``latesttest-50e07536``. Gap 2 made ``isNull``/``isNotNull`` general
        boolean expressions over any convertible receiver, so the boundary moved
        and the assertion inverts with it — from "refuses, naming ``isNotNull``"
        to "converts".

        Amended in place with its reason rather than deleted, per `LGR-60c4985-6`
        / `LGR-5906ec6-2`: this repo encodes a deliberate hold as a test
        assertion, so releasing the hold means changing a test on purpose, and
        the change must stay legible as a reversal rather than read as a
        regression. Gap 1's reasoning about WHY it refused — ``when``'s condition
        goes through ``_expr``, never ``_filter_mask`` — is still the correct
        mechanism; what changed is that ``_expr`` now has a rule for the two null
        predicates instead of a blanket refusal
        (`pyspark_to_pandas.py:_NULL_PREDICATES`).

        The emission is pinned in full by
        ``test_null_predicate_expressions.TestTheLiveTransformConverts``, which
        also owns the traceability to the pipeline id. What is asserted here is
        only the inversion itself, at the place the hold used to be."""
        code = 'df_t = df.withColumn("c", when(col("c").cast(IntegerType()).isNotNull(), col("c").cast(IntegerType())))'
        cast = 'pd.to_numeric(df["c"], errors="coerce").astype("Int64")'
        assert f'df_t["c"] = np.where({cast}.notna(), {cast}, np.nan)' in convert_code(code)

    def test_a_numeric_cast_literal_value_refuses_naming_cast(self):
        """p40's fence is untouched and now reachable through ``when``: a scalar
        receiver under a numeric cast refuses naming ``cast`` — the construct
        that owns the member (`LGR-4dd9b42-1`) — rather than ``when``."""
        assert refusal('df_t = df.withColumn("f", when(col("a") > 1, lit(0).cast("int")))').construct == "cast"


class TestRefusalsThatMustNotMove:
    """Rule B — preservation. These refuse today, with these construct names,
    and both must be unchanged.

    Captured against the working tree before this file was written; all passed
    on the pre-fix run.
    """

    def test_bitwise_and_with_otherwise_still_refuses_naming_col(self):
        """The pre-existing answer for the paired form, pinned so the two shapes
        cannot drift apart."""
        assert (
            refusal('df_t = df.withColumn("f", when((col("a") > 1) & (col("b") < 2), "y").otherwise("n"))').construct
            == "col"
        )

    def test_a_when_method_on_a_non_when_receiver_still_refuses(self):
        """``col("a").when(...)`` is not a when-chain. The walk reaches a
        non-``when`` callee and refuses, still naming ``when``."""
        assert refusal('df_t = df.withColumn("f", col("a").when(col("b") > 1, "y"))').construct == "when"

    @pytest.mark.parametrize("args", ["", '"only_one"', '"a", "b", "c"'])
    def test_a_when_with_the_wrong_argument_count_still_refuses(self, args):
        """``_when_branch`` requires exactly two arguments."""
        assert refusal(f'df_t = df.withColumn("f", when({args}))').construct == "when"

    def test_otherwise_with_the_wrong_argument_count_still_refuses(self):
        assert refusal('df_t = df.withColumn("f", when(col("a") > 1, "y").otherwise())').construct == "otherwise"

    def test_a_when_inside_select_still_refuses_naming_select(self):
        """``select`` cannot resolve an output column name for a ``when``
        expression, so it refuses naming ``select`` — the same answer the PAIRED
        form gives today, and the same class as the existing baseline case
        ``refuse-select-unresolvable-expression-arg``. Pinned for both shapes so
        neither drifts from the other."""
        assert refusal('df_s = df.select(when(col("a") > 1, "y"))').construct == "select"
        assert refusal('df_s = df.select(when(col("a") > 1, "y").otherwise("n"))').construct == "select"


class TestAliasOnAWhenMatchesThePairedFormExactly:
    """Rule B — parity pin over a KNOWN-DIVERGENT emission. Read the warning.

    ``.alias(...)`` on a ``when`` chain emits ``np.where(...).rename(...)``, and
    ``np.where`` returns a **numpy ndarray**, which has no ``.rename``. That
    emission raises ``AttributeError`` in the Code Interpreter.

    **THIS IS PRE-EXISTING AT HEAD AND IS NOT THIS SPEC'S DOING.** Verified by
    stashing the production change and re-running: the PAIRED form emits the
    byte-identical ``np.where(df["a"] > 1, "y", "n").rename("flag")`` on both the
    ``select`` and ``withColumn`` paths before this change. p42 makes the same
    divergence reachable through one additional spelling; it does not create it.

    It is a p40 permissive-default gap, not an oversight: `_is_scalar_emission`'s
    docstring names ``np.where(...)`` explicitly as a form assumed to be a Series
    (`pyspark_to_pandas.py:446`), so `_require_series` — which separates scalars
    from Series — cannot see an ndarray/Series distinction by construction.
    Closing it means widening that predicate, which is a different spec.

    These assertions therefore pin **parity between the two shapes**, NOT
    correctness of the emitted text. When the ndarray gap is fixed, both
    assertions must move together, and this class exists so they cannot be
    fixed for one shape and left broken for the other.
    """

    def test_bare_and_paired_alias_emissions_differ_only_in_the_fallback(self):
        bare = convert_code('df_t = df.withColumn("f", when(col("a") > 1, "y").alias("flag"))')
        paired = convert_code('df_t = df.withColumn("f", when(col("a") > 1, "y").otherwise("n").alias("flag"))')
        assert 'np.where(df["a"] > 1, "y", np.nan).rename("flag")' in bare
        assert 'np.where(df["a"] > 1, "y", "n").rename("flag")' in paired
        assert bare.replace("np.nan", '"n"') == paired
