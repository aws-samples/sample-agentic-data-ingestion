# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`docs/TROUBLESHOOTING.md`'s converter section is checked against the converter.

Sibling of ``test_pyspark_readme_examples.py`` (p32 2.13), applied to the operator-facing
page. That page told an operator two things the p32 rewrite had invalidated: it listed
chained ``when().when().otherwise()`` as an unconvertible pattern (it converts, to
nested ``np.where`` with every branch preserved — bugfix.md 2.9), and it described
unconverted lines as being wrapped in ``# UNCONVERTED:`` comments (no such path exists;
a hard raise replaced it). It also listed ``posexplode``, which the converter does not
name at all — it passes through and fails later at execution.

Two documents had now drifted from the same module in the same way, which is the
argument for executing the claims rather than proof-reading them. Nothing here reads
the page for meaning:

* the constructs the page calls refusals are compared to the module's own frozensets;
* the shapes it describes as converting or refusing are run through
  :meth:`PySpark2PandasConverter.convert` and the outcome asserted;
* the message format it prints, the check name it tells an operator to look for, and
  the two refusal reasons it names are compared to the strings the code builds.

**Validates: Requirements 2.13**
"""

import ast
import os
import re
import sys

import pytest

# Add Lambda source to path (same shape as tests/unit/lambdas/test_code/).
_TEST_CODE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
import pyspark_to_pandas
from code_interpreter_runner import (
    NOT_VERIFIED_REASON_NOT_YET,
    NOT_VERIFIED_REASON_PERMANENT,
    classify_conversion_refusal,
)
from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

_DOCS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs"))
TROUBLESHOOTING_PATH = os.path.join(_DOCS_DIR, "TROUBLESHOOTING.md")
TEMPLATES_README_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates", "README.md")
)

with open(TROUBLESHOOTING_PATH, encoding="utf-8") as handle:
    TROUBLESHOOTING = handle.read()
with open(TEMPLATES_README_PATH, encoding="utf-8") as handle:
    TEMPLATES_README = handle.read()


# ──────────────────────────────────────────────────────────────────────
# Extraction helpers — structural, never semantic.
# ──────────────────────────────────────────────────────────────────────

_SECTION_HEADING = "### PySpark-to-Pandas Conversion Refuses"
_CODE_SPAN = re.compile(r"`([^`]+)`")
_CONSTRUCT_LIST_MARKER = "**Constructs that refuse**"
_SHAPE_LIST_MARKER = "**Shapes that refuse**"


def _section() -> str:
    """The converter section, from its heading to the next ``###``."""
    start = TROUBLESHOOTING.find(_SECTION_HEADING)
    assert start != -1, f"{_SECTION_HEADING!r} not found in TROUBLESHOOTING.md"
    body = TROUBLESHOOTING[start + len(_SECTION_HEADING) :]
    end = body.find("\n### ")
    return body if end == -1 else body[:end]


SECTION = _section()


def _bullets_under(marker: str) -> list[str]:
    """The bullet lines of the list introduced by *marker*."""
    start = SECTION.find(marker)
    assert start != -1, f"the section no longer has a {marker!r} list"
    bullets: list[str] = []
    for line in SECTION[start + len(marker) :].split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped)
        elif bullets and stripped:
            break  # the list ended; the following paragraph is prose
    return bullets


def _refusal_constructs() -> list[str]:
    """Every identifier the "Constructs that refuse" list names.

    Each code span is decomposed into its identifiers rather than required to BE
    one, because the falsehood this suite exists to catch was spelled
    ``when().when().otherwise()`` — a span that is not a bare name, and whose
    identifiers are both *supported*. Requiring a bare identifier would have let
    that exact sentence back in.

    The shape rules live in their own list (:func:`_shape_bullets`) and are
    asserted by execution, so a span like ``.withColumnRenamed(...).withColumn(...)``
    never reaches this function.
    """
    names: list[str] = []
    for bullet in _bullets_under(_CONSTRUCT_LIST_MARKER):
        for span in _CODE_SPAN.findall(bullet):
            names.extend(re.findall(r"[A-Za-z_]\w*", span))
    return names


def _module_constant(module_filename: str, name: str) -> str:
    """A string constant read out of a Lambda module's source.

    Via ``ast`` rather than an import: ``lambda_function`` pulls in the Strands
    runtime, and stubbing that to read two strings would put a mock between the
    document and the code it is being checked against.
    """
    path = os.path.join(_TEST_CODE_DIR, module_filename)
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {module_filename}")


UNSUPPORTED_VOCABULARY = (
    set(pyspark_to_pandas._UNSUPPORTED_FUNCTIONS)
    | set(pyspark_to_pandas._UNSUPPORTED_METHODS)
    | set(pyspark_to_pandas._UNSUPPORTED_NAMES)
)


# ──────────────────────────────────────────────────────────────────────
# Every construct the page calls a refusal really refuses.
# ──────────────────────────────────────────────────────────────────────


class TestRefusalListMatchesTheConverter:
    """**Validates: Requirements 2.13**"""

    def test_the_list_is_not_empty(self):
        """Guard: an extraction bug must not make the rest of this class vacuous."""
        assert len(_refusal_constructs()) >= 10

    def test_the_shape_list_is_separate_and_populated(self):
        """The two lists are kept apart on purpose — see :func:`_refusal_constructs`."""
        assert len(_bullets_under(_SHAPE_LIST_MARKER)) >= 2

    @pytest.mark.parametrize("construct", _refusal_constructs())
    def test_named_construct_is_in_the_unsupported_vocabulary(self, construct):
        """A construct the page calls unconvertible has to be one the module refuses.

        This is the assertion that catches the two kinds of drift observed: naming a
        construct the converter now handles (chained ``when`` — both ``when`` and
        ``otherwise`` are supported), and naming one the converter never knew about
        (``posexplode``, which passes through and fails later at execution instead).
        """
        assert construct in UNSUPPORTED_VOCABULARY

    @pytest.mark.parametrize("construct", _refusal_constructs())
    def test_named_construct_actually_raises(self, construct):
        """Membership is not enough — the refusal is executed.

        A method has to be written in method position to be that construct at all:
        a bare ``groupBy(...)`` is an unknown function call, which passes through.
        """
        if construct in pyspark_to_pandas._UNSUPPORTED_METHODS:
            statement = f'df_transformed = df.{construct}("y")'
        else:
            statement = f'df_transformed = df.withColumn("x", {construct}(col("y")))'
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(statement)
        assert raised.value.construct == construct


# ──────────────────────────────────────────────────────────────────────
# The shapes the page says convert, convert.
# ──────────────────────────────────────────────────────────────────────


class TestConvertingShapes:
    """**Validates: Requirements 2.13**"""

    CHAINED_WHEN = (
        'df_transformed = df.withColumn("tier", when(col("spend") > 1000, "gold")'
        '.when(col("spend") > 100, "silver").otherwise("bronze"))'
    )

    def test_page_claims_chained_when_converts(self):
        """The sentence is present, so the executable assertions below are about it."""
        assert "when(...).when(...).otherwise(...)" in SECTION
        assert "np.where" in SECTION

    def test_chained_when_converts_to_nested_np_where(self):
        code = PySpark2PandasConverter.convert(self.CHAINED_WHEN)["code"]
        assert code.count("np.where") == 2

    def test_every_when_branch_survives_in_order(self):
        """ "every branch preserved in order" is the claim; dropping one silently
        changes which rows get which value (bugfix.md 2.9)."""
        code = PySpark2PandasConverter.convert(self.CHAINED_WHEN)["code"]
        assert code.index("gold") < code.index("silver") < code.index("bronze")
        assert code.index("1000") < code.index("100,")

    def test_chained_with_column_converts_with_a_trailing_select(self):
        result = PySpark2PandasConverter.convert(
            'df_transformed = df.withColumn("fname", trim(col("first_name")))'
            '.withColumn("lname", trim(col("last_name"))).select("fname", "lname")'
        )
        assert result["conversion_report"]["output_lines"] == 1

    def test_splitting_a_mixed_chain_converts(self):
        """The page's remedy for a mixed chain is one statement per line."""
        PySpark2PandasConverter.convert(
            'df_transformed = df.withColumnRenamed("a", "b")\n'
            'df_transformed = df_transformed.withColumn("c", trim(col("b")))'
        )


# ──────────────────────────────────────────────────────────────────────
# The shapes the page says refuse, refuse — and name what it says they name.
# ──────────────────────────────────────────────────────────────────────


class TestRefusingShapes:
    """**Validates: Requirements 2.13**"""

    def test_mixed_chain_refuses_at_the_first_non_composable_step(self):
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert('df_transformed = df.withColumnRenamed("a", "b").withColumn("c", col("b"))')
        assert raised.value.construct == "withColumn"

    def test_single_quoted_column_literals_refuse(self):
        with pytest.raises(ConversionError):
            PySpark2PandasConverter.convert("df_transformed = df.withColumn('fname', trim(col('first_name')))")

    def test_udf_decorated_function_in_the_block_refuses(self):
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(
                "@udf(returnType=StringType())\n"
                "def clean(v):\n"
                "    return v\n"
                'df_transformed = df.withColumn("x", clean(col("y")))'
            )
        assert raised.value.construct == "udf"

    def test_refusal_line_is_the_construct_line_not_the_statement_line(self):
        """The page's "not the line the statement starts on" clause, executed."""
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(
                "df_transformed = (\n"
                "    df\n"
                '    .withColumnRenamed("customer_id", "cust_id")\n'
                '    .withColumn("fname", trim(col("first_name")))\n'
                ")"
            )
        assert raised.value.line_number == 4
        assert "not the line the statement starts on" in SECTION


# ──────────────────────────────────────────────────────────────────────
# The failure behaviour the page describes is the one that exists.
# ──────────────────────────────────────────────────────────────────────


class TestFailureBehaviourIsTheRaise:
    """**Validates: Requirements 2.13**"""

    def test_unconverted_comment_marker_is_gone_from_both_sides(self):
        """The page described unconverted lines as wrapped in ``# UNCONVERTED:``
        comments. No such path exists in the converter, so the sentence was false;
        asserting the string's absence on both sides keeps them from re-diverging."""
        with open(pyspark_to_pandas.__file__, encoding="utf-8") as handle:
            converter_source = handle.read()
        assert "UNCONVERTED" not in converter_source
        assert "UNCONVERTED" not in TROUBLESHOOTING

    def test_page_names_the_raise_and_the_all_or_nothing_rule(self):
        assert "ConversionError(construct, line, line_number)" in SECTION
        assert "all-or-nothing" in SECTION
        assert "Nothing is ever commented out, dropped, or partially translated" in SECTION

    def test_documented_message_format_is_the_real_one(self):
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert('df_transformed = df.groupBy("state").agg(count("*"))')
        assert str(raised.value) == (
            "unconvertible PySpark construct 'groupBy' on line 1: "
            'df_transformed = df.groupBy("state").agg(count("*"))'
        )
        assert "unconvertible PySpark construct '<construct>' on line <n>: <line>" in SECTION


# ──────────────────────────────────────────────────────────────────────
# The machine-readable names an operator is told to look for are real.
# ──────────────────────────────────────────────────────────────────────


class TestOperatorFacingNamesAreReal:
    """**Validates: Requirements 2.13**"""

    def test_check_name_matches_the_handler_constant(self):
        check_name = _module_constant("lambda_function.py", "NOT_VERIFIED_CHECK_NAME")
        assert f"`{check_name}`" in SECTION

    def test_both_refusal_reasons_match_the_runner_constants(self):
        assert f"`{NOT_VERIFIED_REASON_PERMANENT}`" in SECTION
        assert f"`{NOT_VERIFIED_REASON_NOT_YET}`" in SECTION

    def test_permanent_examples_classify_as_permanent(self):
        """The page names these as the permanent kind; the classifier has to agree.

        AMENDED by p41 task 9.3 (2.7), not deleted. `initcap` was the sixth name in
        the tuple below and is deliberately no longer: `initcap` is `.str.title()` —
        single-frame and elementwise — so "no pandas equivalent exists" was false; it
        is unimplemented, not inexpressible. This file reads
        `docs/TROUBLESHOOTING.md`, and the page's `permanently_inexpressible` example
        list dropped `initcap` in the same change, so the document and the assertion
        move together. Everything else the test asserted still stands, and the
        reversal is asserted in the other direction below rather than left as a
        silent removal (`LGR-60c4985-6`, `LGR-5906ec6-2`, both template / ratified:
        a deliberately-held assertion is amended in place with its reason at the
        assertion so the reversal stays legible).
        """
        for construct in ("groupBy", "agg", "join", "Window", "udf"):
            assert classify_conversion_refusal(construct) == NOT_VERIFIED_REASON_PERMANENT

        # The other half of the amendment: the name stays pinned at this site under
        # its true reason instead of dropping out of the suite. The page's own item 3
        # already lists `initcap` under "functions the generation prompt offers but
        # the converter has no rewrite for" — which is what `not_yet_expressible`
        # says — so this is the classifier agreeing with the page, same as above.
        assert classify_conversion_refusal("initcap") == NOT_VERIFIED_REASON_NOT_YET

    def test_pandas_proxy_caveat_exists_in_the_handler(self):
        """The page's "proxy, not the Glue job" framing is a real report caveat."""
        caveat = _module_constant("lambda_function.py", "PANDAS_PROXY_CAVEAT")
        assert "not the Glue/Spark job" in caveat
        assert "proxy" in SECTION
        assert "has not been run against Spark" in SECTION

    def test_cross_reference_anchor_resolves(self):
        """The page sends the reader to the templates README for the full vocabulary."""
        assert "../pyspark_templates/README.md#what-the-pandas-proxy-converts" in SECTION
        assert "## What the pandas proxy converts" in TEMPLATES_README
