# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Alias-shape conversion tests for PySpark2PandasConverter (P28b R1a).

P28b requirement 1.1: generated transforms containing ``col(...).alias(...)``
in the p28 task 9.2 template shapes — bare ``col("x").alias("y")``,
``col("x").cast(...).alias("y")``, ``to_date(col("x"), ...).alias("y")``, and
each inside chained ``withColumn``/``select`` — convert to pandas without
ConversionError AND, once executed, the output DataFrame contains a column
named by the alias carrying the aliased expression's values (the
observability clause).

P28b requirement 1.2: constructs the converter cannot express still raise
ConversionError naming the construct and line — ``alias(`` stays in the
residual scan as the safety net, and an expression-args ``select`` with an
unresolvable output name is refused honestly instead of shipped mangled
(the p28 R10 contract, unchanged).

pandas is not installed in the test venv, so execution reuses the
fake-pandas harness from ``test_to_date_regression.py`` (its ``_FakeSeries``
gained ``.rename`` for the converted alias shape); this module adds
``pd.to_numeric`` and ``pd.DataFrame`` stand-ins for the cast and
expression-select shapes.

**Validates: Requirements 1.1, 1.2, 1.8**
"""

import os
import sys
import types
from datetime import date
from unittest.mock import patch

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

from .test_to_date_regression import _FakeDataFrame, _FakeSeries, _make_fake_pandas

# ──────────────────────────────────────────────────────────────────────
# Real template-output lines (the p28 task 9.2 shapes, requirement 1.1).
# ──────────────────────────────────────────────────────────────────────

# The dominant observed shape from the R1a design: to_date + alias inside
# the canonical first-transform withColumn assignment.
DOMINANT_SHAPE = (
    'df_transformed = df.withColumn("order_date", to_date(col("order_date"), "dd-MM-yyyy").alias("order_date"))'
)

# Day-first source data: "05-01-2023" is 5 January — a dropped format would
# yield 1 May (month-first) or a parse miss.
RAW_DATES = ["25-12-2023", "05-01-2023", "not-a-date"]
DAY_FIRST_DATES = [date(2023, 12, 25), date(2023, 1, 5), None]


def convert(code: str) -> dict:
    return PySpark2PandasConverter.convert(code)


# ──────────────────────────────────────────────────────────────────────
# Fake-pandas harness extensions (cast + expression-select shapes).
# ──────────────────────────────────────────────────────────────────────


def _fake_to_numeric(series, errors=None):
    """``pandas.to_numeric`` stand-in: float-parse with coerce semantics."""
    parsed = []
    for value in series:
        try:
            parsed.append(float(value))
        except (TypeError, ValueError):
            if errors == "coerce":
                parsed.append(None)
            else:
                raise
    return _FakeSeries(parsed)


def _execute_converted(code: str, namespace: dict, recorded_formats: list | None = None) -> None:
    """Execute the FULL converted code (import header included) with the
    fake-pandas harness, extended with to_numeric and DataFrame."""
    fake_pandas = _make_fake_pandas([] if recorded_formats is None else recorded_formats)
    fake_pandas.to_numeric = _fake_to_numeric
    fake_pandas.DataFrame = _FakeDataFrame
    fake_numpy = types.ModuleType("numpy")
    with patch.dict(sys.modules, {"pandas": fake_pandas, "numpy": fake_numpy}):
        exec(code, namespace)


# ──────────────────────────────────────────────────────────────────────
# Shape 1 — bare col("x").alias("y").
# ──────────────────────────────────────────────────────────────────────


class TestBareColAlias:
    """**Validates: Requirements 1.1**"""

    CODE = 'renamed = col("order_date").alias("od")'

    def test_converts_to_rename(self):
        result = convert(self.CODE)
        assert 'renamed = df["order_date"].rename("od")' in result["code"]

    def test_report_records_alias_construct(self):
        report = convert(self.CODE)["conversion_report"]
        assert "alias" in report["constructs_converted"]

    def test_executes_carrying_alias_name_and_values(self):
        """The aliased expression's values survive and the series carries the
        alias name (pandas Series.rename semantics)."""
        namespace = {"df": _FakeDataFrame({"order_date": list(RAW_DATES)})}

        _execute_converted(convert(self.CODE)["code"], namespace)

        assert list(namespace["renamed"]) == RAW_DATES
        assert namespace["renamed"].name == "od"


# ──────────────────────────────────────────────────────────────────────
# Shape 2 — col("x").cast(...).alias("y").
# ──────────────────────────────────────────────────────────────────────


class TestCastAlias:
    """**Validates: Requirements 1.1**"""

    CODE = 'total = col("amount").cast("double").alias("total")'

    def test_converts_cast_then_rename(self):
        result = convert(self.CODE)
        assert 'total = pd.to_numeric(df["amount"], errors="coerce").rename("total")' in result["code"]

    def test_converts_type_object_cast_with_alias(self):
        code = 'age_int = col("age").cast(IntegerType()).alias("age_int")'
        result = convert(code)
        assert 'pd.to_numeric(df["age"], errors="coerce").astype("Int64").rename("age_int")' in result["code"]

    def test_executes_carrying_cast_values_and_alias_name(self):
        namespace = {"df": _FakeDataFrame({"amount": ["10", "2.5", "oops"]})}

        _execute_converted(convert(self.CODE)["code"], namespace)

        assert list(namespace["total"]) == [10.0, 2.5, None]  # coerce, not crash
        assert namespace["total"].name == "total"


# ──────────────────────────────────────────────────────────────────────
# Shape 3 — to_date(col("x"), ...).alias("y").
# ──────────────────────────────────────────────────────────────────────


class TestToDateAlias:
    """**Validates: Requirements 1.1**"""

    CODE = 'order_date = to_date(col("order_date"), "dd-MM-yyyy").alias("order_date")'

    def test_converts_to_date_then_rename_with_format(self):
        result = convert(self.CODE)
        assert (
            'pd.to_datetime(df["order_date"], format="%d-%m-%Y", errors="coerce").dt.date.rename("order_date")'
            in result["code"]
        )

    def test_executes_with_day_first_format_reaching_the_call(self):
        namespace = {"df": _FakeDataFrame({"order_date": list(RAW_DATES)})}
        recorded: list = []

        _execute_converted(convert(self.CODE)["code"], namespace, recorded)

        assert recorded == ["%d-%m-%Y"]  # the format string survived conversion
        assert list(namespace["order_date"]) == DAY_FIRST_DATES
        assert namespace["order_date"].name == "order_date"


# ──────────────────────────────────────────────────────────────────────
# Shape 4a — the alias shapes inside chained withColumn.
# ──────────────────────────────────────────────────────────────────────


class TestAliasInsideChainedWithColumn:
    """**Validates: Requirements 1.1**"""

    CHAINED = (
        DOMINANT_SHAPE + "\n"
        'df_transformed = df_transformed.withColumn("amount", col("amount").cast("double").alias("amount"))'
    )

    def test_dominant_shape_converts_to_design_expected_output(self):
        """The dominant observed template shape converts to exactly the
        R1a-designed pandas output (rename is a no-op on assignment — the
        enclosing withColumn name wins in both engines)."""
        result = convert(DOMINANT_SHAPE)
        assert (
            'df_transformed = df.copy(); df_transformed["order_date"] = '
            'pd.to_datetime(df["order_date"], format="%d-%m-%Y", errors="coerce").dt.date.rename("order_date")'
        ) in result["code"]

    def test_dominant_shape_report_records_constructs(self):
        report = convert(DOMINANT_SHAPE)["conversion_report"]
        assert {"alias", "to_date", "withColumn", "col"} <= set(report["constructs_converted"])
        assert report["input_lines"] == 1
        assert report["output_lines"] == 1  # nothing dropped

    def test_dominant_shape_executes_with_alias_named_column(self):
        """Requirement 1.1 observability: the executed output frame contains
        the alias-named column carrying day-first-parsed dates."""
        namespace = {"df": _FakeDataFrame({"order_id": [1, 2, 3], "order_date": list(RAW_DATES)})}
        recorded: list = []

        _execute_converted(convert(DOMINANT_SHAPE)["code"], namespace, recorded)

        frame = namespace["df_transformed"]
        assert "order_date" in frame.columns
        assert list(frame["order_date"]) == DAY_FIRST_DATES
        assert recorded == ["%d-%m-%Y"]
        # withColumn returns a NEW frame — the raw df stays pristine.
        assert list(namespace["df"]["order_date"]) == RAW_DATES

    def test_chained_with_column_binds_alias_expr_to_current_frame(self):
        """The second chained line reads amount from df_transformed (not the
        raw df) and its cast+alias values land in the output frame."""
        result = convert(self.CHAINED)
        assert (
            'df_transformed["amount"] = pd.to_numeric(df_transformed["amount"], errors="coerce").rename("amount")'
        ) in result["code"]

        namespace = {"df": _FakeDataFrame({"amount": ["10", "2.5", "oops"], "order_date": list(RAW_DATES)})}
        _execute_converted(convert(self.CHAINED)["code"], namespace)

        frame = namespace["df_transformed"]
        assert list(frame["amount"]) == [10.0, 2.5, None]
        assert list(frame["order_date"]) == DAY_FIRST_DATES
        assert list(namespace["df"]["amount"]) == ["10", "2.5", "oops"]


# ──────────────────────────────────────────────────────────────────────
# Shape 4b — the alias shapes inside select.
# ──────────────────────────────────────────────────────────────────────


class TestAliasInsideSelect:
    """**Validates: Requirements 1.1**"""

    SELECT = 'df_selected = df.select(col("order_id"), to_date(col("order_date"), "dd-MM-yyyy").alias("order_date"))'

    def test_expression_select_converts_to_new_frame(self):
        result = convert(self.SELECT)
        assert (
            'df_selected = pd.DataFrame({"order_id": df["order_id"], '
            '"order_date": pd.to_datetime(df["order_date"], format="%d-%m-%Y", '
            'errors="coerce").dt.date.rename("order_date")})'
        ) in result["code"]

    def test_expression_select_report_records_constructs(self):
        report = convert(self.SELECT)["conversion_report"]
        assert {"alias", "select", "to_date", "col"} <= set(report["constructs_converted"])

    def test_expression_select_executes_with_alias_named_column(self):
        """Requirement 1.1 observability: the selected frame contains the
        alias-named column with the aliased expression's values, and the
        plain col() ref keeps its own values."""
        namespace = {"df": _FakeDataFrame({"order_id": [1, 2, 3], "order_date": list(RAW_DATES)})}
        recorded: list = []

        _execute_converted(convert(self.SELECT)["code"], namespace, recorded)

        frame = namespace["df_selected"]
        assert frame.columns == ["order_id", "order_date"]
        assert list(frame["order_id"]) == [1, 2, 3]
        assert list(frame["order_date"]) == DAY_FIRST_DATES
        assert recorded == ["%d-%m-%Y"]
        # select builds a NEW frame — the source stays pristine.
        assert list(namespace["df"]["order_date"]) == RAW_DATES

    def test_chained_select_binds_exprs_to_source_frame(self):
        """cast+alias inside a select chained on df_transformed reads from
        df_transformed — executing with ONLY df_transformed in scope proves
        the binding is complete (a stray df ref would NameError)."""
        code = 'df_final = df_transformed.select(col("age").cast("double").alias("age_num"), "name")'
        namespace = {"df_transformed": _FakeDataFrame({"age": ["30", "40"], "name": ["ann", "bob"]})}

        _execute_converted(convert(code)["code"], namespace)

        frame = namespace["df_final"]
        assert frame.columns == ["age_num", "name"]
        assert list(frame["age_num"]) == [30.0, 40.0]
        assert list(frame["name"]) == ["ann", "bob"]

    def test_all_string_select_keeps_todays_subscript_shape(self):
        """Plain string-column selects keep today's [[...]] shape (existing
        behavior untouched), in both assignment and method position."""
        assert 'df2 = df[["a", "b"]]' in convert('df2 = df.select("a", "b")')["code"]
        assert '[["a", "b", "c"]]' in convert('df.select("a", "b", "c")')["code"]


# ──────────────────────────────────────────────────────────────────────
# Negatives — the p28 R10 ConversionError contract is unchanged (R1.2).
# ──────────────────────────────────────────────────────────────────────


class TestAliasNegatives:
    """**Validates: Requirements 1.2**"""

    def test_non_literal_alias_raises_named_construct(self):
        """alias(var) has no literal column name — the residual safety net
        raises with the construct and line named."""
        with pytest.raises(ConversionError) as exc_info:
            convert('x = col("a").alias(alias_var)')
        assert exc_info.value.construct == "alias"
        assert exc_info.value.line_number == 1
        assert "alias" in str(exc_info.value)
        assert "line 1" in str(exc_info.value)

    def test_non_literal_alias_inside_with_column_raises_with_line_number(self):
        code = DOMINANT_SHAPE + '\ndf_transformed = df_transformed.withColumn("d", to_date(col("d")).alias(dyn))'
        with pytest.raises(ConversionError) as exc_info:
            convert(code)
        assert exc_info.value.construct == "alias"
        assert exc_info.value.line_number == 2

    def test_expression_select_arg_without_resolvable_name_raises(self):
        """An expression select arg with no alias has no resolvable output
        column — refused honestly as ConversionError("select"), never
        shipped as mangled [[...]] output."""
        with pytest.raises(ConversionError) as exc_info:
            convert('df_selected = df.select(trim(col("name")))')
        assert exc_info.value.construct == "select"
        assert exc_info.value.line_number == 1
        assert "select" in str(exc_info.value)

    def test_compound_expression_select_arg_raises(self):
        with pytest.raises(ConversionError) as exc_info:
            convert('df_selected = df.select(col("a") + lit(1))')
        assert exc_info.value.construct == "select"
        assert exc_info.value.line_number == 1
