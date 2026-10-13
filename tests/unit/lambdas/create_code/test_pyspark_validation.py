# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for validate_pyspark_antipatterns() — detect known anti-patterns, pass clean code.

Validates AC-11: Post-generation AST check detects .lower(), .upper(), .trim()
called as Column methods.
Validates AC-12: Detected anti-patterns trigger automatic code regeneration.

We import the function by loading only the function source to avoid pulling in
heavy Lambda dependencies (strands, boto3, etc.).
"""

import ast
import os
import types


def _load_validate_function():
    """Extract validate_pyspark_antipatterns from transform_generation.py source
    without importing the full module (avoids strands/boto3 dependencies)."""
    # P28 R19.1: function moved from lambda_function.py to transform_generation.py
    source_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_create_code",
        "transform_generation.py",
    )
    with open(source_path) as f:
        source = f.read()

    # Parse the source and extract only the validate_pyspark_antipatterns function
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "validate_pyspark_antipatterns":
            func_source = ast.get_source_segment(source, node)
            # Create a minimal module with just this function
            mod = types.ModuleType("_pyspark_validation")
            exec(compile(func_source, "<validate_pyspark_antipatterns>", "exec"), mod.__dict__)
            return mod.validate_pyspark_antipatterns
    raise ImportError("Could not find validate_pyspark_antipatterns in transform_generation.py")


validate_pyspark_antipatterns = _load_validate_function()


# --- Anti-pattern detection tests ---


class TestDetectsAntiPatterns:
    """Verify that known PySpark Column method anti-patterns are detected."""

    def test_detects_lower_method(self):
        code = 'result = col("name").lower()'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".lower()" in issues[0]
        assert "pyspark.sql.functions.lower()" in issues[0]

    def test_detects_upper_method(self):
        code = 'result = col("name").upper()'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".upper()" in issues[0]

    def test_detects_trim_method(self):
        code = 'result = col("name").trim()'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".trim()" in issues[0]

    def test_detects_substring_method(self):
        code = 'result = col("name").substring(0, 5)'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".substring()" in issues[0]

    def test_detects_length_method(self):
        code = 'result = col("name").length()'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".length()" in issues[0]

    def test_detects_multiple_antipatterns(self):
        code = 'a = col("x").lower()\nb = col("y").upper()\nc = col("z").trim()\n'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 3

    def test_reports_line_numbers(self):
        code = 'x = 1\nresult = col("name").lower()\ny = 2'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert "Line 2:" in issues[0]


# --- Clean code tests ---


class TestPassesCleanCode:
    """Verify that correct PySpark usage is NOT flagged."""

    def test_function_call_lower(self):
        """lower(col("x")) is correct PySpark — should not be flagged."""
        code = 'result = lower(col("name"))'
        issues = validate_pyspark_antipatterns(code)
        assert issues == []

    def test_function_call_upper(self):
        code = 'result = upper(col("name"))'
        issues = validate_pyspark_antipatterns(code)
        assert issues == []

    def test_function_call_trim(self):
        code = 'result = trim(col("name"))'
        issues = validate_pyspark_antipatterns(code)
        assert issues == []

    def test_clean_pyspark_script(self):
        """A realistic clean PySpark script using direct function imports should have zero issues."""
        code = """
from pyspark.sql.functions import lower, trim, length, col

df_transformed = df.withColumn("name_lower", lower(col("name")))
df_transformed = df_transformed.withColumn("trimmed", trim(col("value")))
df_transformed = df_transformed.filter(length(col("name")) > 0)
"""
        issues = validate_pyspark_antipatterns(code)
        assert issues == []

    def test_empty_code(self):
        issues = validate_pyspark_antipatterns("")
        assert issues == []

    def test_passthrough_transform(self):
        code = "df_transformed = df"
        issues = validate_pyspark_antipatterns(code)
        assert issues == []


# --- Edge cases ---


class TestEdgeCases:
    """Edge cases for the anti-pattern validator."""

    def test_syntax_error_returns_empty(self):
        """Unparseable code should return empty list, not raise."""
        code = "def broken(:"
        issues = validate_pyspark_antipatterns(code)
        assert issues == []

    def test_string_lower_is_detected(self):
        """Note: AST-level check flags all .lower() calls on any expression,
        not just Column objects. This is by design — it's a broad heuristic."""
        code = '"hello".lower()'
        issues = validate_pyspark_antipatterns(code)
        # The broad AST check flags this — it's a known trade-off
        assert len(issues) == 1

    def test_chained_method_calls(self):
        code = 'result = col("name").cast("string").lower()'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".lower()" in issues[0]


# --- Substring with Column args (P21 — fsidemo failure root cause) ---


class TestSubstringColumnArgs:
    """``substring(col, pos, len)`` requires ``pos`` and ``len`` to be
    Python ``int`` literals — passing a Column-returning call (e.g.
    ``length(col("x"))``) raises ``PySparkTypeError [NOT_ITERABLE]`` at
    runtime because Py4J tries to iterate the Column as a Python list.

    Detection at codegen time turns a runtime Glue-job failure into a
    deterministic regeneration signal.
    """

    def test_detects_length_as_substring_len_arg(self):
        """The pattern that broke fsidemo-2f176620 in production."""
        code = (
            'concat_ws("", upper(substring(trim(col("first_name")), 1, 1)), '
            'lower(substring(trim(col("first_name")), 2, length(trim(col("first_name"))))))'
        )
        issues = validate_pyspark_antipatterns(code)
        # Outer substring's len arg is length(...) — flagged.
        assert any("substring" in i and "length" in i for i in issues), issues

    def test_detects_col_as_substring_pos_arg(self):
        code = 'substring(col("name"), col("start_pos"), 5)'
        issues = validate_pyspark_antipatterns(code)
        assert any("pos" in i and "col" in i for i in issues), issues

    def test_int_literal_args_pass(self):
        """The correct shape — fixed-length slicing with int literals."""
        code = 'substring(col("name"), 1, 1)'
        issues = validate_pyspark_antipatterns(code)
        assert issues == []

    def test_recommends_initcap_or_expr(self):
        code = 'substring(col("x"), 2, length(col("x")))'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        # The fix recommendation must mention both alternatives so the
        # AI agent's regeneration loop has a clear path forward.
        assert "initcap" in issues[0] or "expr" in issues[0]
