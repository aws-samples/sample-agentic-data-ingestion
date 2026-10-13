# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 10.1 — AST-based anti-pattern detector for PySpark templates.

Implements ColumnMethodVisitor to detect .lower()/.upper()/.trim()/.substring()/.length()
called on PySpark Column objects (method-call style) inside withColumn/filter/when contexts.
Does NOT flag function-call style like lower(col("x")) which is correct PySpark usage.

Also tests the Lambda-level validate_pyspark_antipatterns() function from create_code Lambda.
"""

import ast
import os
import types

import jinja2
import pytest


# --- Load validate_pyspark_antipatterns without importing full Lambda module ---
def _load_validate_function():
    """Extract validate_pyspark_antipatterns from transform_generation.py source
    without importing the full module (avoids strands/boto3 dependencies)."""
    # P28 R19.1: function moved from lambda_function.py to transform_generation.py
    source_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_create_code",
        "transform_generation.py",
    )
    with open(source_path) as f:
        source = f.read()

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "validate_pyspark_antipatterns":
            func_source = ast.get_source_segment(source, node)
            mod = types.ModuleType("_pyspark_validation")
            exec(compile(func_source, "<validate_pyspark_antipatterns>", "exec"), mod.__dict__)
            return mod.validate_pyspark_antipatterns
    raise ImportError("Could not find validate_pyspark_antipatterns in transform_generation.py")


validate_pyspark_antipatterns = _load_validate_function()

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = "df_transformed = df"

TEMPLATE_FILES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_jdbc.py.j2",
]

BASE_VARS = {
    "source_format": "json",
    "partition_columns": None,
    "write_mode": "append",
    "merge_key": "id",
    "checkpoint_enabled": False,
    "checkpoint_bucket": "",
}


class ColumnMethodVisitor(ast.NodeVisitor):
    """Detect .lower()/.upper()/.trim()/.substring()/.length() calls likely on PySpark Column objects.

    Heuristic: flag method calls where the receiver chain originates from a Column producer
    (col, when, cast, withColumn, otherwise, rlike, isin). Calls on args.get() or plain
    string variables are excluded.
    """

    SUSPECT_METHODS = {"lower", "upper", "trim", "substring", "length"}
    COLUMN_PRODUCERS = {"col", "when", "cast", "withColumn", "otherwise", "rlike", "isin"}

    def __init__(self):
        self.violations = []

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method in self.SUSPECT_METHODS:
                if self._is_likely_column(node.func.value):
                    self.violations.append((node.lineno, method))
        self.generic_visit(node)

    def _is_likely_column(self, node) -> bool:
        """Check if the expression is likely a PySpark Column (not a plain string)."""
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in self.COLUMN_PRODUCERS:
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in self.COLUMN_PRODUCERS:
                return True
            # Recurse into chained calls: col("x").cast("string").lower()
            if isinstance(node.func, ast.Attribute):
                return self._is_likely_column(node.func.value)
        return False


def validate_pyspark_ast(rendered_code: str) -> list:
    """Parse rendered PySpark code and return list of (lineno, method) violations."""
    tree = ast.parse(rendered_code)
    visitor = ColumnMethodVisitor()
    visitor.visit(tree)
    return visitor.violations


@pytest.fixture
def jinja_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


# --- Validator unit tests ---


def test_detects_column_lower():
    """col("x").lower() should be flagged."""
    code = 'result = col("name").lower()'
    violations = validate_pyspark_ast(code)
    assert len(violations) == 1
    assert violations[0][1] == "lower"


def test_detects_column_upper():
    """col("x").upper() should be flagged."""
    code = 'result = col("name").upper()'
    violations = validate_pyspark_ast(code)
    assert len(violations) == 1
    assert violations[0][1] == "upper"


def test_detects_column_trim():
    """col("x").trim() should be flagged."""
    code = 'result = col("name").trim()'
    violations = validate_pyspark_ast(code)
    assert len(violations) == 1
    assert violations[0][1] == "trim"


def test_detects_chained_column_lower():
    """col("x").cast("string").lower() should be flagged."""
    code = 'result = col("name").cast("string").lower()'
    violations = validate_pyspark_ast(code)
    assert len(violations) == 1


def test_ignores_function_call_lower():
    """lower(col("x")) is correct PySpark — should NOT be flagged."""
    code = 'result = lower(col("name"))'
    violations = validate_pyspark_ast(code)
    assert len(violations) == 0


def test_ignores_function_call_trim():
    """trim(col("x")) is correct PySpark — should NOT be flagged."""
    code = 'result = trim(col("name"))'
    violations = validate_pyspark_ast(code)
    assert len(violations) == 0


def test_ignores_string_lower():
    """Plain string .lower() should NOT be flagged."""
    code = 'result = "hello".lower()'
    violations = validate_pyspark_ast(code)
    assert len(violations) == 0


def test_ignores_args_get_lower():
    """args.get("key").lower() should NOT be flagged (not a Column)."""
    code = 'result = args.get("key", "default").lower()'
    violations = validate_pyspark_ast(code)
    assert len(violations) == 0


# --- Template validation: rendered templates should have zero violations ---


@pytest.mark.parametrize("template_name", TEMPLATE_FILES)
def test_rendered_template_has_no_column_antipatterns(jinja_env, template_name):
    """Each rendered template should have zero suspect Column method calls."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, **BASE_VARS)
    violations = validate_pyspark_ast(rendered)
    assert violations == [], f"Found Column anti-patterns in {template_name}: {violations}"


@pytest.mark.parametrize("template_name", TEMPLATE_FILES)
def test_rendered_template_with_checkpoint_has_no_antipatterns(jinja_env, template_name):
    """Checkpoint-enabled templates should also have zero violations."""
    template = jinja_env.get_template(template_name)
    vars_with_checkpoint = {**BASE_VARS, "checkpoint_enabled": True, "checkpoint_bucket": "test-bucket"}
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, **vars_with_checkpoint)
    violations = validate_pyspark_ast(rendered)
    assert violations == [], f"Found Column anti-patterns in {template_name} (checkpoint): {violations}"


# --- Tests for Lambda-level validate_pyspark_antipatterns() ---


class TestValidatePysparkAntipatterns:
    """Test the validate_pyspark_antipatterns() function from create_code Lambda."""

    def test_detects_lower_antipattern(self):
        code = 'result = col("name").lower()'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".lower()" in issues[0]
        assert "pyspark.sql.functions.lower()" in issues[0]

    def test_detects_upper_antipattern(self):
        code = 'result = col("name").upper()'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".upper()" in issues[0]

    def test_detects_trim_antipattern(self):
        code = 'result = col("name").trim()'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".trim()" in issues[0]

    def test_detects_substring_antipattern(self):
        code = 'result = col("name").substring(0, 5)'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".substring()" in issues[0]

    def test_detects_length_antipattern(self):
        code = 'result = col("name").length()'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert ".length()" in issues[0]

    def test_clean_function_calls_pass(self):
        """Correct PySpark function-call style should return no issues."""
        code = """
from pyspark.sql.functions import lower, upper, trim
result = lower(col("name"))
result2 = upper(col("name"))
result3 = trim(col("name"))
"""
        issues = validate_pyspark_antipatterns(code)
        assert issues == []

    def test_empty_code_passes(self):
        issues = validate_pyspark_antipatterns("")
        assert issues == []

    def test_syntax_error_returns_empty(self):
        issues = validate_pyspark_antipatterns("def broken(:")
        assert issues == []

    def test_multiple_antipatterns_detected(self):
        code = 'a = col("x").lower()\nb = col("y").upper()\nc = col("z").trim()\n'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 3

    def test_line_numbers_reported(self):
        code = 'x = 1\nresult = col("name").lower()\ny = 2'
        issues = validate_pyspark_antipatterns(code)
        assert len(issues) == 1
        assert "Line 2:" in issues[0]

    def test_passthrough_transform_passes(self):
        issues = validate_pyspark_antipatterns("df_transformed = df")
        assert issues == []
