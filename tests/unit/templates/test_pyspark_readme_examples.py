# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The examples in `pyspark_templates/README.md` are checked against the converter.

p32 2.13: the document has to state what the converter actually does. Before this
suite it did not — its worked example was a parenthesised fluent chain that
``PySpark2PandasConverter`` refuses (bugfix.md 1.12), and it described unconvertible
patterns as being wrapped in ``# UNCONVERTED:`` comments, a behaviour the hard raise
replaced.

Prose cannot be trusted to stay in step with code, so nothing here reads the README
for meaning. Every claim the document makes about the converter is executed against
the converter:

* the worked transform blocks are run through :meth:`PySpark2PandasConverter.convert`;
* every row of the shape table is run, and its documented outcome — converts, or
  refuses naming a construct — is asserted;
* the construct lists are compared to the module's own frozensets rather than to a
  transcription of them, which is what stops the two sides drifting again.
"""

import ast
import os
import re
import sys

import pytest

# Add Lambda source to path (same shape as tests/unit/lambdas/test_code/).
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
import pyspark_to_pandas
from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

README_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates", "README.md")
)

with open(README_PATH, encoding="utf-8") as handle:
    README = handle.read()


# ──────────────────────────────────────────────────────────────────────
# Extraction helpers — structural, never semantic.
# ──────────────────────────────────────────────────────────────────────

_FENCE = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)
_CODE_SPAN = re.compile(r"`([^`]+)`")
#: A shape-table row: | label | `statement` | converts / refuses `construct` |
_TABLE_ROW = re.compile(r"^\|(?P<label>[^|]+)\|(?P<example>[^|]+)\|(?P<outcome>[^|]+)\|\s*$", re.MULTILINE)


def _transform_blocks() -> list[str]:
    """Fenced python blocks in the README that are transform blocks.

    A transform block is one that produces ``df_transformed`` — which excludes the
    ``TEMPLATE_MAP`` snippet and any other Python the document shows.
    """
    return [block for block in _FENCE.findall(README) if "df_transformed" in block]


def _shape_rows() -> list[tuple[str, str, str]]:
    """``(label, statement, outcome)`` for every row of the shape table."""
    rows: list[tuple[str, str, str]] = []
    for match in _TABLE_ROW.finditer(README):
        example = match.group("example").strip()
        spans = _CODE_SPAN.findall(example)
        if len(spans) != 1 or "df_transformed" not in spans[0]:
            continue  # a header, a separator, or one of the parameter-contract tables
        rows.append((match.group("label").strip(), spans[0], match.group("outcome").strip()))
    return rows


def _documented_vocabulary(marker: str) -> set[str]:
    """The backticked identifiers the README lists for *marker*.

    Keyed on the frozenset's own name in parentheses, so the document has to name
    the thing it is claiming to mirror.
    """
    for line in README.split("\n"):
        if f"(`{marker}`):" in line:
            _prefix, _sep, listed = line.partition(f"(`{marker}`):")
            return set(_CODE_SPAN.findall(listed))
    raise AssertionError(f"README does not document {marker}")


VOCABULARY_MARKERS = [
    "_SUPPORTED_FUNCTIONS",
    "_SUPPORTED_METHODS",
    "_UNSUPPORTED_FUNCTIONS",
    "_UNSUPPORTED_METHODS",
    "_UNSUPPORTED_NAMES",
]


# ──────────────────────────────────────────────────────────────────────
# The worked example converts.
# ──────────────────────────────────────────────────────────────────────


class TestWorkedExampleConverts:
    """**Validates: Requirements 2.13**"""

    def test_readme_shows_at_least_one_transform_block(self):
        """Guard: an extraction bug must not make the rest of this suite vacuous."""
        assert _transform_blocks(), "no transform block found in the README"

    @pytest.mark.parametrize("block", _transform_blocks())
    def test_transform_block_converts(self, block):
        result = PySpark2PandasConverter.convert(block)
        assert result["code"].strip()
        assert result["conversion_report"]["output_lines"] >= 1

    @pytest.mark.parametrize("block", _transform_blocks())
    def test_converted_block_is_valid_python(self, block):
        """The emitted pandas is what the Code Interpreter executes."""
        ast.parse(PySpark2PandasConverter.convert(block)["code"])

    def test_worked_example_is_not_a_parenthesised_fluent_chain(self):
        """The 1.12 shape, pinned out.

        `convert` accepting the block is the real assertion; this one names the
        specific shape that made the old example wrong, so a reviewer sees why the
        document is written one-statement-per-line.
        """
        for block in _transform_blocks():
            assert "= (\n" not in block, "a parenthesised fluent chain refuses"


# ──────────────────────────────────────────────────────────────────────
# Every row of the shape table behaves as documented.
# ──────────────────────────────────────────────────────────────────────


class TestShapeTableIsAccurate:
    """**Validates: Requirements 2.13**"""

    def test_table_has_rows_of_both_kinds(self):
        outcomes = [outcome for _label, _example, outcome in _shape_rows()]
        assert any(outcome.startswith("converts") for outcome in outcomes)
        assert any(outcome.startswith("refuses") for outcome in outcomes)

    @pytest.mark.parametrize(("label", "statement", "outcome"), _shape_rows())
    def test_row_outcome_matches_the_converter(self, label, statement, outcome):
        if outcome.startswith("converts"):
            PySpark2PandasConverter.convert(statement)
            return

        expected = _CODE_SPAN.findall(outcome)
        assert expected, f"row {label!r} documents a refusal without naming a construct"
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(statement)
        assert raised.value.construct == expected[0]


# ──────────────────────────────────────────────────────────────────────
# The construct lists are the module's, not a transcription.
# ──────────────────────────────────────────────────────────────────────


class TestConstructListsMatchTheConverter:
    """**Validates: Requirements 2.13**"""

    @pytest.mark.parametrize("marker", VOCABULARY_MARKERS)
    def test_documented_list_equals_module_frozenset(self, marker):
        assert _documented_vocabulary(marker) == set(getattr(pyspark_to_pandas, marker))


# ──────────────────────────────────────────────────────────────────────
# The failure behaviour the document describes is the one that exists.
# ──────────────────────────────────────────────────────────────────────


class TestFailureBehaviourIsTheRaise:
    """**Validates: Requirements 2.13**"""

    def test_unconverted_comment_marker_is_gone_from_both_sides(self):
        """Neither the converter nor the document carries the marker.

        The README documented unconvertible patterns as being wrapped in
        ``# UNCONVERTED:`` comments (1.12). No such path exists in the converter, so
        the sentence was false; asserting the string's absence on both sides keeps
        the claim and the code from re-diverging.
        """
        with open(pyspark_to_pandas.__file__, encoding="utf-8") as handle:
            converter_source = handle.read()
        assert "# UNCONVERTED:" not in converter_source
        assert "# UNCONVERTED:" not in README

    def test_readme_names_the_raise(self):
        assert "ConversionError" in README
        assert "all-or-nothing" in README

    def test_documented_message_format_is_the_real_one(self):
        """The message shape the README prints is the one ConversionError builds."""
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert('df_transformed = df.groupBy("state").agg(count("*"))')
        assert str(raised.value).startswith("unconvertible PySpark construct 'groupBy' on line 1: ")
        assert "unconvertible PySpark construct '<construct>' on line <n>: <line>" in README
