# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for PySpark2PandasConverter.

P28 R10.2/R10.3 (task 9.2): the converter raises ConversionError instead of
commenting out unconverted lines, returns a conversion report, and handles
the canonical template assignment shape ``df_transformed = df.withColumn(...)``
where source and target names differ.
"""

import os
import sys

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

# T8's detector for a `[[...]]` subscript applied to an expression rather than to
# a frame name — the bugfix.md 1.10 signature. Reused rather than reinvented.
from .test_select_output_column_refusal import _mangled_subscripts

# Real template-output shape (same block used by the template tests): the
# FIRST transform assigns df → df_transformed (source ≠ target), later
# transforms chain on df_transformed.
SAMPLE_TRANSFORM = (
    'df_transformed = df.withColumn("name", col("name").cast(StringType()))\n'
    'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")'
)


def convert_code(code: str) -> str:
    """Convert and return just the pandas code."""
    return PySpark2PandasConverter.convert(code)["code"]


class TestPySparkImportRemoval:
    def test_removes_pyspark_sql_functions_import(self):
        code = "from pyspark.sql.functions import col, lower, trim\ndf = df"
        result = convert_code(code)
        assert "pyspark" not in result

    def test_removes_pyspark_types_import(self):
        code = "from pyspark.sql.types import StringType, IntegerType\ndf = df"
        result = convert_code(code)
        assert "pyspark" not in result


class TestWithColumnRenamed:
    def test_basic_rename(self):
        code = 'df = df.withColumnRenamed("old_col", "new_col")'
        result = convert_code(code)
        assert '.rename(columns={"old_col": "new_col"})' in result

    def test_rename_with_differing_source_and_target(self):
        """Method-position rewrite is valid when target ≠ source (R10.3 check)."""
        code = 'df_transformed = df.withColumnRenamed("old_col", "new_col")'
        result = convert_code(code)
        assert 'df_transformed = df.rename(columns={"old_col": "new_col"})' in result


class TestCastConversions:
    def test_cast_integer_type(self):
        code = 'col("age").cast(IntegerType())'
        result = convert_code(code)
        assert 'pd.to_numeric(df["age"], errors="coerce").astype("Int64")' in result

    def test_cast_string_type(self):
        code = 'col("name").cast(StringType())'
        result = convert_code(code)
        # AMENDED IN PLACE by p40 task 5.2 — `.kiro/specs/p40-pandas-proxy-literal-cast/`, R7.
        # Was `df["name"].astype(str)`. The test is not deleted and its subject
        # is unchanged; only the expected emission moved. Amend-never-delete is
        # this repo's convention for a deliberate expectation change
        # (`LGR-60c4985-6`, template/ratified; design B4).
        #
        # WHY IT MOVED. The nullable `"string"` dtype is the only spelling that
        # preserves nulls on both pandas majors (design §5, F6/F10, *measured*):
        # `astype(str)` writes the literal text "None" into null cells and
        # reports ZERO nulls on pandas 2.2.3, while `astype("string")` yields
        # `<NA>` and one null on both 2.2.3 and 3.0.5. The harness injects ~5%
        # nulls into every generated column, so the old spelling inflated
        # `completeness` for every string-cast column on a pandas-2 interpreter.
        assert 'df["name"].astype("string")' in result

    def test_cast_double_type(self):
        code = 'col("price").cast(DoubleType())'
        result = convert_code(code)
        assert 'pd.to_numeric(df["price"], errors="coerce")' in result

    def test_cast_float_type(self):
        code = 'col("score").cast(FloatType())'
        result = convert_code(code)
        assert 'pd.to_numeric(df["score"], errors="coerce").astype("float32")' in result

    def test_cast_string_literal_int(self):
        code = 'col("id").cast("integer")'
        result = convert_code(code)
        assert 'pd.to_numeric(df["id"], errors="coerce").astype("Int64")' in result

    def test_cast_string_literal_double(self):
        code = 'col("val").cast("double")'
        result = convert_code(code)
        assert 'pd.to_numeric(df["val"], errors="coerce")' in result

    def test_cast_int_in_canonical_with_column_shape(self):
        """Real template shape: cast("int") under the first-transform assignment."""
        code = 'df_transformed = df.withColumn("age", col("age").cast("int"))'
        result = convert_code(code)
        assert 'df_transformed = df.copy(); df_transformed["age"] = ' in result
        assert 'pd.to_numeric(df["age"], errors="coerce").astype("Int64")' in result

    def test_cast_string_in_canonical_with_column_shape(self):
        code = 'df_transformed = df.withColumn("name", col("name").cast("string"))'
        result = convert_code(code)
        # AMENDED IN PLACE by p40 task 5.2 — `.kiro/specs/p40-pandas-proxy-literal-cast/`, R7.
        # Was `... = df["name"].astype(str)`; not deleted (`LGR-60c4985-6`,
        # template/ratified; design B4). Same reason as `test_cast_string_type`
        # above — the nullable `"string"` dtype is the only spelling that
        # preserves nulls on both pandas majors (design §5, F6/F10, *measured*).
        # This case exercises the `"string"` literal target rather than
        # `StringType()`; task 5.1 moved both `_CAST_TARGETS` keys together.
        assert 'df_transformed = df.copy(); df_transformed["name"] = df["name"].astype("string")' in result


class TestStringFunctions:
    def test_trim(self):
        code = 'trim(col("name"))'
        result = convert_code(code)
        assert 'df["name"].str.strip()' in result

    def test_lower(self):
        code = 'lower(col("city"))'
        result = convert_code(code)
        assert 'df["city"].str.lower()' in result

    def test_upper(self):
        code = 'upper(col("state"))'
        result = convert_code(code)
        assert 'df["state"].str.upper()' in result


class TestNestedStringFunctions:
    """P8/P28 gap: nested string-function compositions (e.g.
    lower(trim(col("x")))). The inner func converts col("x") to a pandas
    Series; the OUTER func must then wrap that Series instead of matching
    its col()-only pattern, otherwise it stays unconverted and the residual
    scan raises ConversionError. Observed live on
    ``lower(trim(col("account_status")))``.
    """

    def test_lower_of_trim_converts_without_raising(self):
        code = 'lower(trim(col("account_status")))'
        result = convert_code(code)
        assert 'df["account_status"].str.strip().str.lower()' in result

    def test_canonical_live_failing_with_column_shape(self):
        """The exact live-failing statement: chained withColumn (source ==
        target == df_transformed) wrapping lower(trim(col(...))).

        _rewrite_with_column rebinds bare ``df[`` references to this
        statement's source frame because source ("df_transformed") != "df",
        so the emitted read side is df_transformed["account_status"], not
        df["account_status"].
        """
        code = 'df_transformed = df_transformed.withColumn("status", lower(trim(col("account_status"))))'
        result = convert_code(code)
        assert 'df_transformed["status"] = df_transformed["account_status"].str.strip().str.lower()' in result

    def test_upper_of_trim(self):
        code = 'upper(trim(col("x")))'
        result = convert_code(code)
        assert 'df["x"].str.strip().str.upper()' in result

    def test_trim_of_lower(self):
        code = 'trim(lower(col("x")))'
        result = convert_code(code)
        assert 'df["x"].str.lower().str.strip()' in result

    def test_simple_non_nested_cases_still_convert(self):
        """Sanity: the single-level string funcs are unaffected by the new
        nested-composition patterns."""
        assert 'df["x"].str.lower()' in convert_code('lower(col("x"))')
        assert 'df["x"].str.strip()' in convert_code('trim(col("x"))')


class TestDateFunctions:
    def test_to_date(self):
        code = 'to_date(col("created_at"))'
        result = convert_code(code)
        assert 'pd.to_datetime(df["created_at"], errors="coerce").dt.date' in result

    def test_to_date_with_format_survives_conversion(self):
        """P28 R10.2: the format string is mapped to strftime, not dropped."""
        code = 'to_date(col("date_str"), "yyyy-MM-dd")'
        result = convert_code(code)
        assert 'pd.to_datetime(df["date_str"], format="%Y-%m-%d", errors="coerce").dt.date' in result

    def test_to_date_ddmmyyyy_format_in_with_column(self):
        """The review's concrete story: to_date(col, 'dd-MM-yyyy') in the
        canonical withColumn shape keeps its day-first format."""
        code = 'df_transformed = df.withColumn("d", to_date(col("d"), "dd-MM-yyyy"))'
        result = convert_code(code)
        assert 'df_transformed = df.copy(); df_transformed["d"] = ' in result
        assert 'pd.to_datetime(df["d"], format="%d-%m-%Y", errors="coerce").dt.date' in result

    def test_to_timestamp(self):
        code = 'to_timestamp(col("event_time"))'
        result = convert_code(code)
        assert 'pd.to_datetime(df["event_time"], errors="coerce")' in result

    def test_to_timestamp_with_format_survives_conversion(self):
        code = 'to_timestamp(col("ts"), "yyyy-MM-dd HH:mm:ss")'
        result = convert_code(code)
        assert 'pd.to_datetime(df["ts"], format="%Y-%m-%d %H:%M:%S", errors="coerce")' in result


class TestNullFilters:
    def test_filter_is_not_null(self):
        code = 'df.filter(col("email").isNotNull())'
        result = convert_code(code)
        assert '[df["email"].notna()]' in result

    def test_filter_is_null(self):
        code = 'df.filter(col("phone").isNull())'
        result = convert_code(code)
        assert '[df["phone"].isna()]' in result

    def test_filter_assignment_binds_mask_to_source(self):
        """Assignment-form filters bind the row mask to the statement's source."""
        code = 'df_transformed = df_transformed.filter(col("email").isNotNull())'
        result = convert_code(code)
        assert 'df_transformed = df_transformed[df_transformed["email"].notna()]' in result

    def test_filter_assignment_with_differing_names(self):
        code = 'df_transformed = df.filter(col("email").isNotNull())'
        result = convert_code(code)
        assert 'df_transformed = df[df["email"].notna()]' in result


class TestRegexpReplace:
    def test_basic_regexp_replace(self):
        code = 'regexp_replace(col("phone"), "[^0-9]", "")'
        result = convert_code(code)
        assert 'df["phone"].str.replace(r"[^0-9]", "", regex=True)' in result


class TestRlike:
    def test_rlike_filter(self):
        code = 'df.filter(col("email").rlike("@example\\.com$"))'
        result = convert_code(code)
        assert 'df["email"].str.contains' in result


class TestConcatWs:
    def test_concat_ws_two_cols(self):
        code = 'concat_ws(" ", col("first"), col("last"))'
        result = convert_code(code)
        assert 'df["first"].astype(str) + " " + df["last"].astype(str)' in result


class TestLit:
    def test_lit_string(self):
        code = 'lit("default_value")'
        result = convert_code(code)
        assert '"default_value"' in result

    def test_lit_number(self):
        code = "lit(42)"
        result = convert_code(code)
        assert "42" in result


class TestDropSelect:
    def test_drop_column(self):
        code = 'df.drop("temp_col")'
        result = convert_code(code)
        assert '.drop(columns=["temp_col"])' in result

    def test_select_columns(self):
        code = 'df.select("a", "b", "c")'
        result = convert_code(code)
        assert '[["a", "b", "c"]]' in result


class TestWithColumn:
    """P28 R10.3: the withColumn rewrite over the REAL template shapes."""

    def test_self_assignment_shape(self):
        code = 'df = df.withColumn("status", col("status"))'
        result = convert_code(code)
        assert 'df["status"] = df["status"]' in result

    def test_canonical_first_transform_source_differs_from_target(self):
        """THE regression this task exists for: ``df_transformed = df.withColumn(...)``
        (the first transform in every AI-generated block) must convert — the old
        ``(\\w+)\\s*=\\s*\\1\\.withColumn`` self-reference pattern missed it and
        the transform was silently commented out."""
        code = 'df_transformed = df.withColumn("name", lower(col("name")))'
        result = convert_code(code)
        assert 'df_transformed = df.copy(); df_transformed["name"] = df["name"].str.lower()' in result
        assert "# UNCONVERTED" not in result
        assert "withColumn" not in result

    def test_chained_with_column_multiple_lines(self):
        """Chained shape: first transform binds df → df_transformed, later
        lines chain on df_transformed and reference the CURRENT frame."""
        code = (
            'df_transformed = df.withColumn("name", lower(col("name")))\n'
            'df_transformed = df_transformed.withColumn("age", col("age").cast(IntegerType()))'
        )
        result = convert_code(code)
        assert 'df_transformed = df.copy(); df_transformed["name"] = df["name"].str.lower()' in result
        # The chained line reads from df_transformed, not the original df
        assert 'df_transformed["age"] = pd.to_numeric(df_transformed["age"], errors="coerce").astype("Int64")' in result

    def test_chained_reference_after_rename_binds_to_current_frame(self):
        """A column renamed on line 2 must be read from df_transformed on line 3
        (reading it from the original df would KeyError at execution time)."""
        code = (
            'df_transformed = df.withColumn("name", trim(col("name")))\n'
            'df_transformed = df_transformed.withColumnRenamed("amount", "total")\n'
            'df_transformed = df_transformed.withColumn("total", col("total").cast("double"))'
        )
        result = convert_code(code)
        assert 'df_transformed["total"] = pd.to_numeric(df_transformed["total"], errors="coerce")' in result
        assert 'pd.to_numeric(df["total"]' not in result

    def test_template_indented_block_is_dedented(self):
        """The real extract_transform_block output is indented (Jinja indent
        filter) — the emitted pandas must be valid top-level Python."""
        code = '    df_transformed = df.withColumn("name", lower(col("name")))\n    df_transformed = df_transformed.withColumnRenamed("a", "b")'
        result = convert_code(code)
        compile(result, "<converted>", "exec")  # raises on bad indentation
        assert "\ndf_transformed = df.copy(); " in result


class TestWhenOtherwise:
    def test_when_otherwise(self):
        code = 'when(col("status") == "active", "yes").otherwise("no")'
        result = convert_code(code)
        assert 'np.where(df["status"] == "active", "yes", "no")' in result

    def test_when_otherwise_inside_chained_with_column(self):
        code = (
            'df_transformed = df_transformed.withColumn("flag", when(col("status") == "active", "yes").otherwise("no"))'
        )
        result = convert_code(code)
        assert 'df_transformed["flag"] = np.where(df_transformed["status"] == "active", "yes", "no")' in result


class TestColReference:
    def test_bare_col_reference(self):
        code = 'x = col("amount")'
        result = convert_code(code)
        assert 'df["amount"]' in result


class TestHeaderAlwaysPresent:
    def test_empty_input(self):
        result = convert_code("")
        assert "import pandas as pd" in result
        assert "import numpy as np" in result


class TestSampleTransform:
    def test_sample_transform_converts_fully(self):
        result = convert_code(SAMPLE_TRANSFORM)
        assert "import pandas as pd" in result
        assert "import numpy as np" in result
        assert "pyspark" not in result
        # First transform (source ≠ target) genuinely converts now
        # AMENDED IN PLACE by p40 task 5.2 — `.kiro/specs/p40-pandas-proxy-literal-cast/`, R7.
        # Was `... = df["name"].astype(str)`; not deleted (`LGR-60c4985-6`,
        # template/ratified; design B4). Only this one assertion moved — the
        # header, no-pyspark, `.rename(...)` and no-`# UNCONVERTED` assertions
        # around it are untouched. The nullable `"string"` dtype is the only
        # spelling that preserves nulls on both pandas majors (design §5,
        # F6/F10, *measured*): `astype(str)` writes the literal "None" into null
        # cells and reports zero nulls on pandas 2.2.3, `"string"` yields `<NA>`
        # and one null on both 2.2.3 and 3.0.5.
        assert 'df_transformed = df.copy(); df_transformed["name"] = df["name"].astype("string")' in result
        assert '.rename(columns={"old_col": "new_col"})' in result
        assert "# UNCONVERTED" not in result


class TestChainedOperations:
    def test_chained_with_column(self):
        code = (
            'df = df.withColumn("name", lower(col("name")))\ndf = df.withColumn("age", col("age").cast(IntegerType()))'
        )
        result = convert_code(code)
        assert 'df["name"] = df["name"].str.lower()' in result
        assert 'df["age"] = pd.to_numeric(df["age"], errors="coerce").astype("Int64")' in result


class TestConversionError:
    """P28 R10.2 — unconvertible constructs raise ConversionError naming the
    construct and line; nothing is commented out or silently dropped."""

    def test_groupby_raises_named_construct(self):
        code = 'result = df.groupBy("category").agg({"amount": "sum"})'
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(code)
        assert exc_info.value.construct == "groupBy"
        assert exc_info.value.line_number == 1
        assert "groupBy" in str(exc_info.value)

    def test_join_raises(self):
        code = 'df = df.join(other_df, "id", "left")'
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(code)
        assert exc_info.value.construct == "join"

    def test_udf_raises(self):
        code = "my_udf = udf(lambda x: x.upper(), StringType())"
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(code)
        assert exc_info.value.construct == "udf"

    def test_rdd_access_raises(self):
        code = "rows = df.rdd.collect()"
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(code)
        assert exc_info.value.construct == "rdd"

    def test_spark_session_raises(self):
        code = 'df = spark.read.csv("data.csv")'
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(code)
        assert exc_info.value.construct == "spark"

    def test_error_carries_offending_line_and_number(self):
        code = (
            'df_transformed = df.withColumn("name", lower(col("name")))\nresult = df.groupBy("cat").agg({"v": "sum"})'
        )
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(code)
        assert exc_info.value.line_number == 2
        assert 'df.groupBy("cat")' in exc_info.value.line

    def test_unconverted_function_raises_not_commented_out(self):
        """A prompt-allowed function without a pandas rewrite (initcap) must
        refuse loudly — the old behavior shipped it commented out."""
        code = 'df_transformed = df.withColumn("name", initcap(col("name")))'
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(code)
        assert exc_info.value.construct == "initcap"

    def test_multiline_fluent_chain_raises(self):
        """The parenthesized fluent-chain shape from the template README is
        line-oriented-unconvertible — refuse loudly, never comment out."""
        code = (
            "df_transformed = (\n"
            "    df\n"
            '    .withColumnRenamed("customer_id", "cust_id")\n'
            '    .withColumn("fname", trim(col("first_name")))\n'
            '    .drop("internal_notes")\n'
            ")"
        )
        with pytest.raises(ConversionError):
            PySpark2PandasConverter.convert(code)

    def test_single_quoted_col_raises(self):
        code = "df_transformed = df.withColumn('name', lower(col('name')))"
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(code)
        assert exc_info.value.construct in ("col", "lower", "withColumn")

    def test_blank_lines_and_comments_do_not_raise(self):
        code = "# a comment mentioning withColumn( and col(\n\nx = 1"
        result = PySpark2PandasConverter.convert(code)
        assert "# a comment mentioning withColumn( and col(" in result["code"]

    def test_mixed_convertible_and_unconvertible_raises(self):
        code = (
            'df = df.withColumn("name", lower(col("name")))\n'
            'result = df.groupBy("cat").agg({"val": "sum"})\n'
            'df = df.withColumnRenamed("old", "new")'
        )
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(code)
        assert exc_info.value.construct == "groupBy"
        assert exc_info.value.line_number == 2


class TestConversionReport:
    """P28 R10.2 — the converter documents what WAS converted."""

    def test_report_line_counts(self):
        report = PySpark2PandasConverter.convert(SAMPLE_TRANSFORM)["conversion_report"]
        assert report["input_lines"] == 2
        assert report["output_lines"] == 2

    def test_report_constructs_converted(self):
        report = PySpark2PandasConverter.convert(SAMPLE_TRANSFORM)["conversion_report"]
        assert "withColumn" in report["constructs_converted"]
        assert "withColumnRenamed" in report["constructs_converted"]
        assert "cast" in report["constructs_converted"]
        assert "col" in report["constructs_converted"]

    def test_report_counts_import_lines_as_input(self):
        code = 'from pyspark.sql.functions import col, lower\ndf = df.withColumn("name", lower(col("name")))'
        report = PySpark2PandasConverter.convert(code)["conversion_report"]
        assert report["input_lines"] == 2
        assert report["output_lines"] == 1  # import line replaced by the header
        assert "pyspark_import" in report["constructs_converted"]

    def test_report_empty_input(self):
        report = PySpark2PandasConverter.convert("")["conversion_report"]
        assert report["input_lines"] == 0
        assert report["output_lines"] == 0
        assert report["constructs_converted"] == []

    def test_report_blank_and_comment_lines_not_counted(self):
        code = "# comment\n\nx = 1"
        report = PySpark2PandasConverter.convert(code)["conversion_report"]
        assert report["input_lines"] == 1
        assert report["output_lines"] == 1


# ──────────────────────────────────────────────────────────────────────
# P32 T9 — one case per shape in bugfix.md 1.7-1.10 (requirement 2.14).
#
# Expectations come from `design.md` §1 — 2.7, 2.8, 2.9, 2.10 — and NOT from
# `convert()`'s current output. `LGR-28b4d75-5` (template provenance) is the
# governing rule: a test transcribed from the producer's own output cannot detect
# the producer drifting, because the output is its own oracle. Here that is not a
# theoretical risk — current output for all four shapes IS the defect, and in the
# 1.10 case it is a *passing* conversion emitting pandas that raises at runtime.
# For the same reason the captured equivalence oracle in
# `test_converter_equivalence_baseline.py` deliberately excludes these four.
#
# All four were `xfail(strict=True)` while task T9 was the head of this spec,
# because T9's contract was that only its property test may be red. `strict` was
# the point: the moment T10's AST converter landed they became XPASS failures,
# which forced whoever fixed the converter to come back and remove the markers
# rather than leaving a permanently-quarantined expectation behind. Task T10
# removed them; the assertions below are unchanged and now pass on their merits.
#
# The assertions are behavioural, not textual snapshots of some expected output:
# T10 chose the emitted form, and pinning a string here would have been
# transcribing an expectation from an implementation that did not exist yet.
# ──────────────────────────────────────────────────────────────────────


class TestBugConditionD3Shapes:
    """The four shapes `isBugCondition_D3` names, from `design.md` §1.

    1.7-1.9 must CONVERT: `bugfix.md` 2.7 closes the refusal arm for chained
    `withColumn` explicitly ("it SHALL convert, since the prompt instructs the
    model to produce it"), and 2.8/2.9 offer no refusal arm at all. Only 1.10
    (2.10) may legitimately refuse, because only there is the construct arguably
    beyond a single-frame pandas proxy.

    Property 3 in `test_pyspark_to_pandas_prop.py` generalises all four over the
    prompt's construct vocabulary; these are the named examples.

    **Validates: Requirements 2.14**
    """

    # ── 1.7 → 2.7 ────────────────────────────────────────────────────
    CHAINED_WITH_COLUMN = 'df_transformed = df.withColumn("a", trim(col("x"))).withColumn("b", lower(col("y")))'

    def test_chained_with_column_on_one_statement_converts(self):
        """2.7 — "Any number of chained `.withColumn` calls on one statement
        converts." This is THE live failure: the run that stopped at the approval
        gate chained seven, and the prompt used to instruct the model to produce
        exactly this shape (1.11). Today the pattern at `:351` admits one call
        per statement and its greedy `(.+)` swallows the rest of the chain into
        the value expression, so the residual scan raises."""
        result = convert_code(self.CHAINED_WITH_COLUMN)
        compile(result, "<converted>", "exec")
        # Both columns are assigned — a conversion that dropped either would be
        # the silent-drop outcome P28 R10.2 forbids.
        assert 'df_transformed["a"] = ' in result
        assert 'df_transformed["b"] = ' in result
        assert "withColumn" not in result
        assert "col(" not in result

    # ── 1.8 → 2.8 ────────────────────────────────────────────────────
    OTHERWISE_COLUMN = (
        'df_transformed = df.withColumn("state", when(col("state") == "CA", "California").otherwise(col("state")))'
    )

    def test_otherwise_with_a_column_reference_converts(self):
        """2.8 — "`otherwise(<column reference>)` converts." The fallback passes
        the original value through rather than substituting a literal, so the
        emitted code has to READ the column on the false branch. Today the
        pattern at `:344` requires a quoted literal in every position and does
        not match, so the residual scan raises."""
        result = convert_code(self.OTHERWISE_COLUMN)
        compile(result, "<converted>", "exec")
        assert 'df_transformed["state"] = ' in result
        assert '"California"' in result
        # The false branch reads the column rather than a literal.
        assert result.count('["state"]') >= 2
        assert "otherwise" not in result
        assert "when(" not in result

    # ── 1.9 → 2.9 ────────────────────────────────────────────────────
    CHAINED_WHEN = (
        'df_transformed = df.withColumn("s", when(col("s") == "CA", "CA_full")'
        '.when(col("s") == "NY", "NY_full").otherwise("Other"))'
    )

    def test_two_or_more_chained_when_calls_convert(self):
        """2.9 — "Two or more chained `.when(...)` calls convert." Every branch
        has to survive: dropping one silently changes which rows get which value,
        which is the class of wrong answer the whole spec is about. Today the
        pattern at `:344` admits exactly one `when`."""
        result = convert_code(self.CHAINED_WHEN)
        compile(result, "<converted>", "exec")
        assert 'df_transformed["s"] = ' in result
        for branch_value in ('"CA_full"', '"NY_full"', '"Other"'):
            assert branch_value in result, f"branch value {branch_value} did not survive conversion"
        assert "when(" not in result
        assert "otherwise" not in result

    # ── 1.10 → 2.10 ──────────────────────────────────────────────────
    WITH_COLUMN_THEN_SELECT = 'df_transformed = df.withColumn("a", trim(col("x"))).select("a")'

    def test_with_column_chained_with_select_either_converts_cleanly_or_refuses(self):
        """2.10 — "either emits pandas that executes correctly or refuses; it
        never emits code that raises at runtime, and `constructs_converted` names
        only constructs whose semantics the emitted code actually carries."

        The higher-severity half of Defect 3 and the only one of the four that is
        invisible today: the conversion SUCCEEDS, emitting
        `df_transformed["a"] = df["x"].str.strip()[["a"]]` — a `[["a"]]`
        subscript applied to a Series, which raises `KeyError` when the Code
        Interpreter runs it — while `constructs_converted` claims `select`. A
        post-hoc residual scan cannot catch this because it reads
        already-mangled output (2.11); only parsing the input can.

        Refusing is an acceptable outcome here, so this asserts the disjunction
        rather than a conversion.
        """
        try:
            result = PySpark2PandasConverter.convert(self.WITH_COLUMN_THEN_SELECT)
        except ConversionError as exc:
            # The refusal arm: honest means it names the construct and the line.
            assert exc.construct
            assert exc.line_number == 1
            return

        code = result["code"]
        compile(code, "<converted>", "exec")
        assert _mangled_subscripts(code) == [], (
            f"emitted a [[...]] subscript applied to an expression rather than a frame: {code!r}"
        )
        # `select` may only be claimed if the emitted code carries select
        # semantics — restricting the frame to the named column.
        if "select" in result["conversion_report"]["constructs_converted"]:
            assert '[["a"]]' in code or 'pd.DataFrame({"a":' in code

    def test_all_four_shapes_are_absent_from_the_captured_equivalence_oracle(self):
        """The companion assertion to the four above, and the reason they are
        here rather than in the baseline suite: `bugfix.md` 2.14 requires these
        expectations to come from the requirements, so the captured oracle in
        `test_converter_equivalence_baseline.py` must NOT contain them. That
        file enforces the exclusion with its own `isBugCondition_D3` predicate;
        this checks the two files agree on which four shapes are meant."""
        from .test_converter_equivalence_baseline import BUG_SHAPES_1_7_TO_1_10, is_bug_condition_d3

        assert set(BUG_SHAPES_1_7_TO_1_10) == {"1.7", "1.8", "1.9", "1.10"}
        for source in (
            self.CHAINED_WITH_COLUMN,
            self.OTHERWISE_COLUMN,
            self.CHAINED_WHEN,
            self.WITH_COLUMN_THEN_SELECT,
        ):
            assert is_bug_condition_d3(source)


# ──────────────────────────────────────────────────────────────────────
# P32 T10 — the parse-failure path (requirement 2.11).
#
# 2.11 replaces the per-physical-line rewrite with parsing the block as Python,
# "so that an unrecognised construct is a parse or translation failure rather
# than a text-rewrite artifact". That makes a block which is not valid Python a
# reachable path for the first time, and it has to refuse the same way every
# other unconvertible input does: name the construct, name the line, return
# nothing (3.7, 3.8).
#
# `LGR-8f3f9e1-1` (backfilled) removed bare `except` repo-wide, so the parse
# failure is caught as `SyntaxError` by name — asserted structurally below
# rather than trusted.
# ──────────────────────────────────────────────────────────────────────


class TestParseFailureRefusesHonestly:
    """**Validates: Requirements 2.11**"""

    UNCLOSED_CALL = 'df_transformed = df.withColumn("name", lower(col("name"))\ndf_transformed = df_transformed'

    def test_malformed_python_raises_conversion_error(self):
        with pytest.raises(ConversionError):
            PySpark2PandasConverter.convert(self.UNCLOSED_CALL)

    def test_parse_failure_names_a_construct_and_a_line(self):
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert(self.UNCLOSED_CALL)
        error = exc_info.value
        assert error.construct
        assert error.line_number >= 1
        assert error.line
        assert f"line {error.line_number}" in str(error)

    def test_parse_failure_returns_nothing(self):
        """3.8 — all-or-nothing: a block that does not parse yields no code."""
        result = None
        with pytest.raises(ConversionError):
            result = PySpark2PandasConverter.convert(self.UNCLOSED_CALL)
        assert result is None

    def test_a_parse_failure_with_no_construct_still_refuses_with_a_name(self):
        """An unparseable line carrying no PySpark construct still has to name
        something — an empty `construct` would reach the reviewer as a blank."""
        with pytest.raises(ConversionError) as exc_info:
            PySpark2PandasConverter.convert("x = (")
        assert exc_info.value.construct

    def test_the_module_contains_no_bare_except(self):
        """`LGR-8f3f9e1-1` (backfilled): bare `except` was removed repo-wide, and
        the parse-failure path this rewrite introduces is exactly where one would
        be tempting. Checked on the parsed module, not by substring, so a comment
        naming the fence does not trip it."""
        import ast
        import inspect

        import pyspark_to_pandas

        tree = ast.parse(inspect.getsource(pyspark_to_pandas))
        bare = [
            handler.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if handler.type is None
        ]
        assert bare == [], f"bare except at line(s) {bare}"

    def test_the_converter_parses_the_block_rather_than_scanning_lines(self):
        """2.11's mechanism, asserted on the module rather than inferred from
        behaviour: `ast.parse` is what decides whether the block is Python."""
        import inspect

        import pyspark_to_pandas

        source = inspect.getsource(pyspark_to_pandas)
        assert "ast.parse(" in source
