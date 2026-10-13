# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization test — the `select` refusal that clause 3.6 depends on (P32 T8).

Spec: `.kiro/specs/p32-test-gate-truth/`. This file is written BEFORE the AST
converter rewrite (design.md C6 / task T10) replaces the ordered pattern table
at `pyspark_to_pandas.py:249-355` and the residual scan at `:361-379`. Its job
is to make the refusal contract explicit so the rewrite cannot silently drop it:

  bugfix.md 3.6 — WHEN a `select` argument's output column name cannot be
  resolved THEN the system SHALL CONTINUE TO refuse rather than emit a mangled
  `[[...]]` subscript (`_select_output_column`, `pyspark_to_pandas.py:190`;
  `_rewrite_select`, `:207-240`). "A deliberate fence: honest refusal over
  mangled output."

  bugfix.md 3.7 — the refusal names the construct, the offending line and the
  line number.

  bugfix.md 3.8 — conversion is all-or-nothing for the whole block; a partial
  conversion is never returned.

  design.md §1 2.10 / C6 — the public surface is `convert()`, the
  `conversion_report`, and `ConversionError(construct, line, line_number)`;
  `constructs_converted` names only constructs whose semantics the emitted code
  actually carries.

Expectations here derive from those clauses, not from `convert()`'s current
output (`LGR-28b4d75-5`, template provenance: a test transcribed from the
producer's output cannot detect the producer drifting, because the output is its
own oracle). That matters especially in this module, because current output
contains a known defect: bugfix.md 1.10 records that
`df.withColumn("a", trim(col("x"))).select("a")` converts SUCCESSFULLY today and
emits `df_transformed["a"] = df["x"].str.strip()[["a"]]` — a `[["a"]]` subscript
applied to a Series — with `constructs_converted` falsely claiming `select`. That
shape is deliberately NOT exercised here: it is the defect clause 2.10 fixes and
Property 3 (task T9) covers. Pinning it in a characterization test would make the
mangling look like the contract.

Scope note — what is already pinned elsewhere, and not repeated:
`test_alias_conversion.py::TestAliasNegatives` already asserts that
`df.select(trim(col("name")))` and `df.select(col("a") + lit(1))` raise
`ConversionError` naming `select` on line 1, and
`test_pyspark_to_pandas.py::TestDropSelect` pins the all-string select shape.
Neither names `_select_output_column`, and neither asserts the exception's `line`
payload, the line number of a refusal deeper in a block, the method-position
form, or the absence of a mangled subscript. Those are what this file adds.

**Validates: Requirements 3.6**
"""

import os
import re
import sys

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter, _select_output_column


def convert(code: str) -> dict:
    return PySpark2PandasConverter.convert(code)


def _mangled_subscripts(code: str) -> list[str]:
    """Return the receiver character of every ``[[...]]`` subscript in *code*
    that is applied to an EXPRESSION rather than to a frame name.

    3.6's failure mode is a `[[...]]` subscript emitted where a name could not
    be resolved. A legitimate multi-column subscript is applied to a frame
    identifier (``df[["a", "b"]]``), so the receiver's last character is an
    identifier character. A subscript applied to the result of a call or of
    another subscript (``df["x"].str.strip()[["a"]]``, the 1.10 signature) is
    mangled — it subscripts a Series.
    """
    receivers = re.findall(r"(.)\[\[", code)
    return [ch for ch in receivers if not (ch.isalnum() or ch == "_")]


# ──────────────────────────────────────────────────────────────────────
# Argument shapes. Written as the PySpark the generation prompt may emit;
# the converter rewrites the inner expression before the select rewrite
# sees it, which is exactly why the output name has to be resolved from
# the argument rather than assumed.
# ──────────────────────────────────────────────────────────────────────

# Each of these select arguments is an expression whose result column PySpark
# would name after the expression itself (`trim(name)`, `CAST(a AS DOUBLE)`,
# `concat_ws(-, a, b)`). There is no pandas column name to resolve, so 3.6
# requires a refusal.
UNRESOLVABLE_SELECT_STATEMENTS = [
    pytest.param('df_out = df.select(col("amount").cast("double"))', id="cast-without-alias"),
    pytest.param('df_out = df.select(concat_ws("-", col("a"), col("b")))', id="concat_ws-without-alias"),
    pytest.param('df_out = df.select(upper(col("name")))', id="string-func-without-alias"),
    pytest.param(
        'df_out = df.select(when(col("state") == "CA", "California").otherwise("Other"))',
        id="when-otherwise-without-alias",
    ),
    pytest.param('df_out = df.select(col("a"), lower(col("b")))', id="one-resolvable-one-not"),
    pytest.param('df.select(upper(col("name")))', id="method-position-expression-arg"),
]


class TestSelectOutputColumnResolution:
    """`_select_output_column` is the resolver 3.6's refusal hinges on: `None`
    is the signal `_rewrite_select` turns into an honest refusal (`:224-226`).

    **Validates: Requirements 3.6**
    """

    @pytest.mark.parametrize(
        ("arg", "expected"),
        [
            ('"amount"', "amount"),
            ('col("amount")', "amount"),
            ('df["amount"]', "amount"),
            ('df_transformed["amount"]', "amount"),
            # `.alias("y")` is converted to `.rename("y")` before the select
            # rewrite runs, and an alias names the output column (P28b R1a,
            # pinned in test_alias_conversion.py). So it IS resolvable.
            ('df["x"].str.strip().rename("clean_x")', "clean_x"),
            ('pd.to_numeric(df["amount"], errors="coerce").rename("total")', "total"),
        ],
    )
    def test_resolvable_argument_yields_its_output_column_name(self, arg: str, expected: str):
        """A named column reference, or an expression the alias names, resolves
        — 3.6 refuses only when the name CANNOT be resolved."""
        assert _select_output_column(arg) == expected

    @pytest.mark.parametrize(
        "arg",
        [
            'df["x"].str.strip()',
            'pd.to_numeric(df["amount"], errors="coerce")',
            'df["a"].astype(str) + "-" + df["b"].astype(str)',
            'np.where(df["state"] == "CA", "California", "Other")',
            'col("a") + lit(1)',
            'df["x"].str.strip()[["x"]]',
        ],
    )
    def test_unresolvable_argument_yields_none_rather_than_a_guess(self, arg: str):
        """An expression with no derivable column name resolves to None. It must
        not fall back to a nearby name — a guessed name is the mangled-output
        outcome 3.6 forbids, one level up."""
        assert _select_output_column(arg) is None


class TestUnresolvableSelectRefuses:
    """**Validates: Requirements 3.6**"""

    @pytest.mark.parametrize("source", UNRESOLVABLE_SELECT_STATEMENTS)
    def test_refuses_with_conversion_error(self, source: str):
        with pytest.raises(ConversionError):
            convert(source)

    @pytest.mark.parametrize("source", UNRESOLVABLE_SELECT_STATEMENTS)
    def test_refusal_names_select_as_the_construct(self, source: str):
        """3.7: the refusal names the construct. `select` is the construct the
        converter could not express — naming the inner function instead would
        point a reviewer at code that is not the problem."""
        with pytest.raises(ConversionError) as exc_info:
            convert(source)
        assert exc_info.value.construct == "select"

    @pytest.mark.parametrize("source", UNRESOLVABLE_SELECT_STATEMENTS)
    def test_refusal_carries_the_offending_pyspark_line_and_its_number(self, source: str):
        """3.7: the offending line and the line number travel with the error.
        The line reported is the PySpark the model wrote, not partially
        rewritten text — `code_interpreter_runner.py:84,:385` puts it in front
        of a human."""
        with pytest.raises(ConversionError) as exc_info:
            convert(source)
        error = exc_info.value
        assert error.line == source.strip()
        assert error.line_number == 1
        assert "select" in str(error)
        assert "line 1" in str(error)

    @pytest.mark.parametrize("source", UNRESOLVABLE_SELECT_STATEMENTS)
    def test_refusal_emits_no_mangled_subscript_instead(self, source: str):
        """3.6's whole point: the alternative to refusing is a mangled
        `[[...]]` subscript, and that alternative is never taken. A refusal
        raises, so there is no emitted code at all — asserted by there being no
        return value to inspect."""
        result = None
        with pytest.raises(ConversionError):
            result = convert(source)
        assert result is None


class TestRefusalIsAllOrNothing:
    """3.8: conversion is all-or-nothing for the whole block.

    **Validates: Requirements 3.6**
    """

    BLOCK = (
        'df_transformed = df.withColumn("name", trim(col("name")))\n'
        'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")\n'
        'df_out = df_transformed.select(concat_ws(" ", col("first"), col("last")))\n'
        'df_final = df_out.withColumn("flag", lit("y"))'
    )

    INDENTED_BLOCK = (
        '    df_transformed = df.withColumn("name", trim(col("name")))\n'
        '    df_out = df_transformed.select(upper(col("name")))'
    )

    def test_convertible_lines_around_the_refusal_do_not_rescue_the_block(self):
        """Three of the four lines convert on their own. The block still refuses
        — a partial conversion is never returned (3.8), because the pandas that
        reaches the Code Interpreter would silently be missing a transform."""
        with pytest.raises(ConversionError) as exc_info:
            convert(self.BLOCK)
        assert exc_info.value.construct == "select"

    def test_refusal_reports_the_line_number_of_the_offending_statement(self):
        """The line number is the refusing statement's own 1-based position in
        the block, not the first line and not the last."""
        with pytest.raises(ConversionError) as exc_info:
            convert(self.BLOCK)
        assert exc_info.value.line_number == 3
        assert exc_info.value.line == 'df_out = df_transformed.select(concat_ws(" ", col("first"), col("last")))'

    def test_line_number_survives_dedent_of_a_template_indented_block(self):
        """Real template output indents the transform block inside `main()`, and
        `convert()` dedents before converting. The reported number stays the
        physical line of the input as the author sees it."""
        with pytest.raises(ConversionError) as exc_info:
            convert(self.INDENTED_BLOCK)
        assert exc_info.value.line_number == 2
        assert exc_info.value.line == 'df_out = df_transformed.select(upper(col("name")))'


class TestResolvableSelectStillConverts:
    """The refusal is targeted, not a blanket rejection of `select`: where a
    name IS resolvable the statement converts, and no `[[...]]` subscript is
    ever applied to an expression (3.6, design.md §1 2.10).

    **Validates: Requirements 3.6**
    """

    def test_string_column_select_subscripts_a_frame_not_an_expression(self):
        code = convert('df_out = df.select("a", "b")')["code"]
        assert _mangled_subscripts(code) == []

    def test_aliased_expression_select_converts_without_any_subscript(self):
        """An alias resolves the output name, so the frame is built by name —
        no `[[...]]` is emitted at all, and the report names `select` because
        the emitted code really does carry select semantics (2.10)."""
        result = convert('df_out = df.select(col("id"), trim(col("name")).alias("clean_name"))')
        assert _mangled_subscripts(result["code"]) == []
        assert "[[" not in result["code"]
        assert "select" in result["conversion_report"]["constructs_converted"]
