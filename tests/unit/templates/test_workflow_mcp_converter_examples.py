# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The converter code examples in `docs/WORKFLOW.md` and `docs/MCP-INTEGRATION.md` are run.

Third sibling of ``test_pyspark_readme_examples.py`` and
``test_troubleshooting_converter_claims.py`` (p32 2.13), applied to the two documents
that carry **executable code examples** of the converter API.

Both examples had gone wrong in the same way after the p32 AST rewrite: they treated
``convert`` as an instance method returning a string, when it is a classmethod
returning ``{"code": ..., "conversion_report": ...}``. MCP-INTEGRATION.md then passed
that value into ``run_test``, whose real first argument is the raw transform block —
so the snippet named a variable ``pandas_code`` that was not code and handed it to a
function that would have converted it a second time.

A code example in a document is a promise that it runs, which is the strongest case in
this document set for executing rather than proof-reading. Nothing here reads either
page for meaning:

* WORKFLOW.md's example is **executed verbatim** against the real converter, and the
  values it binds are asserted to be what it says they are;
* MCP-INTEGRATION.md's example cannot run without AWS, so its ``run_test`` call is
  compared to the real signature by AST, and the result keys it prints are compared to
  the keys the runner actually builds;
* the check name, the two refusal reasons and the message format both pages quote are
  compared to the strings the code builds;
* ARCHITECTURE.md's one narrative sentence is checked only for the two falsehoods it
  used to carry and for the cross-reference it now relies on.

**Validates: Requirements 2.13**
"""

import ast
import inspect
import os
import re
import sys

import pytest

# Add Lambda source to path (same shape as tests/unit/lambdas/test_code/).
_TEST_CODE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)

from code_interpreter_runner import (
    NOT_VERIFIED_REASON_NOT_YET,
    NOT_VERIFIED_REASON_PERMANENT,
    CodeInterpreterTestRunner,
)
from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

_DOCS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs"))
_TEMPLATES_README_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates", "README.md")
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


WORKFLOW = _read(os.path.join(_DOCS_DIR, "WORKFLOW.md"))
MCP_INTEGRATION = _read(os.path.join(_DOCS_DIR, "MCP-INTEGRATION.md"))
ARCHITECTURE = _read(os.path.join(_DOCS_DIR, "ARCHITECTURE.md"))
TEMPLATES_README = _read(_TEMPLATES_README_PATH)
TEST_CODE_README = _read(os.path.join(_TEST_CODE_DIR, "README.md"))


# ──────────────────────────────────────────────────────────────────────
# Extraction helpers — structural, never semantic.
# ──────────────────────────────────────────────────────────────────────

_FENCE = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)

#: WORKFLOW.md's conversion subsection, and the section boundary that ends it.
_WORKFLOW_HEADING = "##### 2. PySpark-to-Pandas Conversion"
#: MCP-INTEGRATION.md's Code Interpreter pattern.
_MCP_HEADING = "### Pattern 2: Testing with Code Interpreter"
#: ARCHITECTURE.md's key-components list for the test Lambda.
_ARCHITECTURE_HEADING = "### Optimization 1: Code Interpreter for Testing"


def _section(page: str, heading: str, boundary: str) -> str:
    """The body under *heading*, up to the next heading at *boundary* level."""
    start = page.find(heading)
    assert start != -1, f"{heading!r} not found"
    body = page[start + len(heading) :]
    end = body.find(f"\n{boundary} ")
    return body if end == -1 else body[:end]


WORKFLOW_SECTION = _section(WORKFLOW, _WORKFLOW_HEADING, "#####")
MCP_SECTION = _section(MCP_INTEGRATION, _MCP_HEADING, "###")
ARCHITECTURE_SECTION = _section(ARCHITECTURE, _ARCHITECTURE_HEADING, "###")
#: The Step 6 error table plus the paragraph that follows it.
WORKFLOW_ERRORS_SECTION = _section(WORKFLOW, "#### Error Scenarios", "---")


def _comment_dict(block: str) -> dict:
    """The dict literal a snippet prints inside ``#`` comments, evaluated.

    The example documents ``run_test``'s return value as a commented-out dict. Every
    value in it is a Python literal, so it round-trips through ``ast.literal_eval`` —
    which turns a block of prose-shaped comment into something comparable to the keys
    the runner actually builds.
    """
    lines = [line.strip().lstrip("#").strip() for line in block.split("\n")]
    try:
        start = lines.index("{")
    except ValueError as exc:  # pragma: no cover - guarded by a non-vacuity test
        raise AssertionError("snippet documents no return-value dict") from exc
    depth = 0
    for offset, line in enumerate(lines[start:]):
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return ast.literal_eval("\n".join(lines[start : start + offset + 1]))
    raise AssertionError("unterminated return-value dict in snippet")


def _call_nodes(block: str, func_name: str) -> list[ast.Call]:
    """Every call to *func_name* in a snippet, by AST rather than by substring."""
    tree = ast.parse(block)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == func_name)
            or (isinstance(node.func, ast.Name) and node.func.id == func_name)
        )
    ]


def _module_constant(module_filename: str, name: str) -> str:
    """A string constant read out of a Lambda module's source.

    Via ``ast`` rather than an import: ``lambda_function`` pulls in the Strands
    runtime, and stubbing that to read one string would put a mock between the
    document and the code it is being checked against.
    """
    tree = ast.parse(_read(os.path.join(_TEST_CODE_DIR, module_filename)))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {module_filename}")


#: A transform block in the shape the generation prompt emits — the input the
#: WORKFLOW.md snippet is written to be handed.
SAMPLE_TRANSFORM_BLOCK = (
    'df_transformed = df.withColumnRenamed("customer_id", "cust_id")\n'
    'df_transformed = df_transformed.withColumn("fname", trim(col("first_name")))\n'
    'df_transformed = df_transformed.withColumn("signup_date", to_date(col("signup_date"), "yyyy-MM-dd"))\n'
    'df_transformed = df_transformed.filter(col("cust_id").isNotNull())\n'
    'df_transformed = df_transformed.select("cust_id", "fname", "signup_date")\n'
)


# ──────────────────────────────────────────────────────────────────────
# WORKFLOW.md's example runs.
# ──────────────────────────────────────────────────────────────────────


class TestWorkflowExampleExecutes:
    """**Validates: Requirements 2.13**"""

    def test_section_carries_exactly_one_snippet(self):
        """Guard: an extraction bug must not make the rest of this class vacuous."""
        assert len(_FENCE.findall(WORKFLOW_SECTION)) == 1

    def test_snippet_runs_verbatim_and_binds_what_it_claims(self):
        """The example is executed as written, not paraphrased."""
        block = _FENCE.findall(WORKFLOW_SECTION)[0]
        namespace: dict = {"transform_block": SAMPLE_TRANSFORM_BLOCK}
        exec(compile(block, "WORKFLOW.md", "exec"), namespace)

        assert isinstance(namespace["conversion"], dict)
        assert isinstance(namespace["pandas_code"], str)
        assert namespace["pandas_code"].startswith("import pandas as pd")
        assert set(namespace["report"]) == {"input_lines", "output_lines", "constructs_converted"}
        # The emitted code is what the Code Interpreter executes.
        ast.parse(namespace["pandas_code"])

    def test_snippet_uses_the_classmethod_rather_than_an_instance(self):
        """The pre-rewrite falsehood: ``PySpark2PandasConverter()`` then ``.convert``."""
        block = _FENCE.findall(WORKFLOW_SECTION)[0]
        assert isinstance(inspect.getattr_static(PySpark2PandasConverter, "convert"), classmethod), (
            "convert is no longer a classmethod — the documented call shape needs revisiting"
        )
        assert not _call_nodes(block, "PySpark2PandasConverter"), "the snippet instantiates the converter"

    def test_snippet_treats_the_result_as_a_dict(self):
        """``convert`` returns a mapping; a snippet that names it ``pandas_code`` is wrong."""
        block = _FENCE.findall(WORKFLOW_SECTION)[0]
        tree = ast.parse(block)
        subscripts = {
            node.slice.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
        }
        assert {"code", "conversion_report"} <= subscripts

    def test_section_states_the_all_or_nothing_rule(self):
        assert "all-or-nothing" in WORKFLOW_SECTION
        assert "ConversionError" in WORKFLOW_SECTION

    def test_section_does_not_claim_a_coverage_percentage(self):
        """ "~90%" described the try/except fallback that the hard raise replaced."""
        assert "90%" not in WORKFLOW_SECTION

    def test_documented_shape_rule_is_the_converters(self):
        """The two grammars the section names as converting actually convert."""
        for statement in (
            'df_transformed = df.withColumn("a", trim(col("x"))).withColumn("b", trim(col("y")))',
            'df_transformed = df.withColumn("a", trim(col("x"))).select("a")',
            'df_transformed = df.withColumnRenamed("x", "a")',
            'df_transformed = df.drop("x")',
            'df_transformed = df.filter(col("x").isNotNull())',
            'df_transformed = df.select("x")',
        ):
            PySpark2PandasConverter.convert(statement)

    def test_cross_reference_anchor_resolves(self):
        assert "../pyspark_templates/README.md#what-the-pandas-proxy-converts" in WORKFLOW_SECTION
        assert "## What the pandas proxy converts" in TEMPLATES_README


# ──────────────────────────────────────────────────────────────────────
# WORKFLOW.md's error table tells the truth about a refusal.
# ──────────────────────────────────────────────────────────────────────


class TestWorkflowErrorTable:
    """**Validates: Requirements 2.13**"""

    def test_conversion_failed_row_is_gone(self):
        """``CONVERSION_FAILED`` is not a code this system emits."""
        assert "CONVERSION_FAILED" not in WORKFLOW

    def test_no_fallback_to_static_analysis_claim(self):
        """The ``# UNCONVERTED:`` falsehood in different words."""
        assert "Falls back to static analysis" not in WORKFLOW_ERRORS_SECTION

    def test_check_name_matches_the_handler_constant(self):
        check_name = _module_constant("lambda_function.py", "NOT_VERIFIED_CHECK_NAME")
        assert f"`{check_name}`" in WORKFLOW_ERRORS_SECTION

    def test_both_refusal_reasons_match_the_runner_constants(self):
        assert f"`{NOT_VERIFIED_REASON_PERMANENT}`" in WORKFLOW_ERRORS_SECTION
        assert f"`{NOT_VERIFIED_REASON_NOT_YET}`" in WORKFLOW_ERRORS_SECTION

    def test_troubleshooting_cross_reference_anchor_resolves(self):
        assert "TROUBLESHOOTING.md#pyspark-to-pandas-conversion-refuses" in WORKFLOW_ERRORS_SECTION
        assert "### PySpark-to-Pandas Conversion Refuses" in _read(os.path.join(_DOCS_DIR, "TROUBLESHOOTING.md"))


# ──────────────────────────────────────────────────────────────────────
# MCP-INTEGRATION.md's example matches the real API.
# ──────────────────────────────────────────────────────────────────────


class TestMcpIntegrationExampleMatchesTheApi:
    """**Validates: Requirements 2.13**

    The snippet drives ``run_test``, which needs AWS, so it is checked by comparing its
    call to the real signature and its printed result to the real keys — not executed.
    """

    def test_section_carries_exactly_one_snippet(self):
        assert len(_FENCE.findall(MCP_SECTION)) == 1

    def test_snippet_is_valid_python(self):
        ast.parse(_FENCE.findall(MCP_SECTION)[0])

    def test_run_test_is_called_with_the_raw_transform_block(self):
        """The pre-rewrite falsehood: converted code was passed in as the first argument."""
        block = _FENCE.findall(MCP_SECTION)[0]
        calls = _call_nodes(block, "run_test")
        assert len(calls) == 1, "the snippet should call run_test exactly once"
        passed = [arg.id for arg in calls[0].args if isinstance(arg, ast.Name)]
        real = [p for p in inspect.signature(CodeInterpreterTestRunner.run_test).parameters if p != "self"]
        assert passed == real, f"snippet passes {passed}, run_test takes {real}"

    def test_snippet_does_not_convert_before_calling_run_test(self):
        """``run_test`` converts internally; converting first double-handles the block."""
        block = _FENCE.findall(MCP_SECTION)[0]
        assert not _call_nodes(block, "convert"), "the snippet converts before calling run_test"

    def test_runner_is_constructed_with_the_real_signature(self):
        block = _FENCE.findall(MCP_SECTION)[0]
        calls = _call_nodes(block, "CodeInterpreterTestRunner")
        assert len(calls) == 1
        keywords = {keyword.arg for keyword in calls[0].keywords}
        real = {p for p in inspect.signature(CodeInterpreterTestRunner.__init__).parameters if p != "self"}
        assert keywords <= real, f"snippet passes {keywords}, __init__ takes {real}"

    def test_documented_top_level_keys_are_the_real_ones(self):
        """Every key the snippet prints is a key ``run_test`` actually returns."""
        documented = _comment_dict(_FENCE.findall(MCP_SECTION)[0])
        # The refusal shape carries every top-level key run_test can return, which is
        # why it is the comparison basis: it needs no session and no AWS.
        real = set(
            CodeInterpreterTestRunner._conversion_failure_results(
                ConversionError(construct="groupBy", line="df.groupBy(...)", line_number=1)
            )
        )
        assert set(documented) <= real, f"snippet invents {set(documented) - real}"

    def test_documented_quality_keys_are_the_real_ones(self):
        """``data_quality_results``'s inner keys come from the runner's own parser."""
        documented = _comment_dict(_FENCE.findall(MCP_SECTION)[0])["data_quality_results"]
        runner = CodeInterpreterTestRunner.__new__(CodeInterpreterTestRunner)
        parsed = runner._parse_results(
            'GENERATED:{"record_count": 500}\n'
            'VALIDATION:{"record_count": 500, "completeness": 95, "uniqueness": 100, '
            '"schema_compliance": 100, "overall_quality": 98, "null_counts": {}, '
            '"duplicates": 0, "missing_columns": []}'
        )
        assert set(documented) == set(parsed["data_quality_results"])

    def test_documented_refusal_keys_are_the_real_ones(self):
        """The prose after the snippet names the non-verification fields."""
        refusal = CodeInterpreterTestRunner._conversion_failure_results(
            ConversionError(construct="groupBy", line="df.groupBy(...)", line_number=1)
        )
        for field in (
            "not_verified_by_proxy",
            "not_verified_reason",
            "not_verified_construct",
            "not_verified_line_number",
        ):
            assert f"`{field}`" in MCP_SECTION
            assert field in refusal
        assert refusal["executed"] is False
        assert refusal["exit_code"] is None
        assert refusal["conversion_report"] is None

    def test_cross_reference_anchor_resolves(self):
        assert "../pyspark_templates/README.md#what-the-pandas-proxy-converts" in MCP_SECTION
        assert "## What the pandas proxy converts" in TEMPLATES_README


# ──────────────────────────────────────────────────────────────────────
# ARCHITECTURE.md's one sentence.
# ──────────────────────────────────────────────────────────────────────


class TestArchitectureComponentLine:
    """**Validates: Requirements 2.13**

    Narrative prose, so only the two claims that were falsifiable are pinned: the
    conversion mechanism and the coverage figure.
    """

    def test_converter_is_not_described_as_regex_based(self):
        assert "Regex-based PySpark" not in ARCHITECTURE
        assert "regex" not in ARCHITECTURE_SECTION.lower()

    def test_no_coverage_percentage_claim(self):
        assert "90%" not in ARCHITECTURE_SECTION

    def test_names_the_real_mechanism_and_failure_mode(self):
        assert "ast.parse" in ARCHITECTURE_SECTION
        assert "all-or-nothing" in ARCHITECTURE_SECTION
        assert "ConversionError" in ARCHITECTURE_SECTION

    def test_names_both_refusal_reasons(self):
        assert f"`{NOT_VERIFIED_REASON_PERMANENT}`" in ARCHITECTURE_SECTION
        assert f"`{NOT_VERIFIED_REASON_NOT_YET}`" in ARCHITECTURE_SECTION

    def test_cross_reference_anchor_resolves(self):
        assert "../pyspark_templates/README.md#what-the-pandas-proxy-converts" in ARCHITECTURE_SECTION
        assert "## What the pandas proxy converts" in TEMPLATES_README


# ──────────────────────────────────────────────────────────────────────
# The Lambda README's converter claims.
# ──────────────────────────────────────────────────────────────────────


class TestTestCodeReadmeClaims:
    """**Validates: Requirements 2.13**"""

    def test_no_residual_scan_framing(self):
        """``ConversionError`` is a translation failure at a node, not a post-scan."""
        assert "after conversion" not in TEST_CODE_README

    def test_message_format_is_the_real_one(self):
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert('df_transformed = df.groupBy("state").agg(count("*"))')
        assert str(raised.value).startswith("unconvertible PySpark construct 'groupBy' on line 1: ")
        assert "unconvertible PySpark construct '<construct>' on line <n>:" in TEST_CODE_README

    def test_multi_line_pure_withcolumn_chain_converts(self):
        """The README listed "multi-line fluent chains" as unconvertible. It is not so.

        A parenthesised chain of pure ``withColumn`` calls converts to one emitted
        statement; what refuses is a chain that MIXES frame operations.
        """
        converts = (
            "df_transformed = (\n"
            '    df.withColumn("fname", trim(col("first_name")))\n'
            '      .withColumn("lname", trim(col("last_name")))\n'
            ")\n"
        )
        assert PySpark2PandasConverter.convert(converts)["conversion_report"]["output_lines"] == 1
        assert "multi-line fluent chains" not in TEST_CODE_README

        mixed = 'df_transformed = (\n    df.withColumnRenamed("a", "b")\n      .withColumn("c", trim(col("b")))\n)\n'
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(mixed)
        assert raised.value.construct == "withColumn"
        # The line number points at the step that could not compose, not line 1.
        assert raised.value.line_number == 3
        assert "mixes" in TEST_CODE_README

    def test_run_test_first_argument_is_documented_as_the_raw_block(self):
        real = [p for p in inspect.signature(CodeInterpreterTestRunner.run_test).parameters if p != "self"]
        assert f"`run_test({', '.join(real)})`" in TEST_CODE_README
        assert "raw transform block" in TEST_CODE_README

    def test_refusal_is_documented_as_a_non_verification(self):
        check_name = _module_constant("lambda_function.py", "NOT_VERIFIED_CHECK_NAME")
        assert f"`{check_name}`" in TEST_CODE_README
        assert f"`{NOT_VERIFIED_REASON_PERMANENT}`" in TEST_CODE_README
        assert f"`{NOT_VERIFIED_REASON_NOT_YET}`" in TEST_CODE_README

    def test_cross_reference_anchor_resolves(self):
        assert "../../pyspark_templates/README.md#what-the-pandas-proxy-converts" in TEST_CODE_README
        assert "## What the pandas proxy converts" in TEMPLATES_README
