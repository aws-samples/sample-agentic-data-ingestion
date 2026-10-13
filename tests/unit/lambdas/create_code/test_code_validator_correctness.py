# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Correctness tests for code_validator (P21 Req 2 — Task 3.4).

These example-based and property-based tests encode the *corrected* validator
contract introduced by Tasks 3.1-3.3 (design.md "Workstream 2", Property 2 and
Property 3):

- Any input that fails ``ast.parse`` (a ``SyntaxError`` OR any other exception)
  must be reported invalid, with ``summary["syntax_valid"]`` False and a
  descriptive entry in ``errors`` (Req 2.1/2.2/2.3).
- ``summary["quality_score"]`` is a computed integer in ``[0, 100]`` (Req 2.4);
  invalid code scores strictly lower than otherwise-equivalent valid code
  (Req 2.5).

Note (P21 Req 2): the validator previously hard-coded ``quality_score = 85`` and
downgraded non-``SyntaxError`` parse failures to a warning while leaving
``syntax_valid`` True. No test in this repo asserted that old behavior, so these
are net-new tests (Req 8.8) rather than corrections of existing assertions
(Req 8.7).
"""

import ast
import logging
import os
import sys

import hypothesis.strategies as st
from hypothesis import given, settings

# Add lambda directory to import path (mirrors test_code_validator_props.py)
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)

# Ensure logger is configured so code_validator doesn't fail on logger calls
logging.basicConfig(level=logging.WARNING)

import code_validator
from code_validator import validate_generated_code

# A syntactically valid sample with imports + a definition, long enough to clear
# the 50-character minimum-length check.
VALID_CODE = "import os\nimport sys\ndef foo():\n    return os.path.join(sys.prefix, 'x')\n"

# A syntactically malformed sample (the parameter list ``broken(:`` is invalid),
# padded so the "too short" check is not what trips it.
MALFORMED_CODE = (
    "import os\ndef broken(:\n    return 1\n# extra comment line so the sample clears the 50-char minimum length\n"
)


# ---------------------------------------------------------------------------
# Req 2.1 — malformed syntax is rejected with non-empty errors
# ---------------------------------------------------------------------------


def test_validate_malformed_syntax_is_invalid():
    """Malformed Python -> is_valid False, syntax_valid False, errors non-empty."""
    result = validate_generated_code(MALFORMED_CODE)

    assert result["is_valid"] is False
    assert result["summary"]["syntax_valid"] is False
    assert result["errors"], "expected at least one error entry for malformed syntax"
    # The parser-reported reason is surfaced in errors (Req 2.3).
    assert any("Syntax error" in e for e in result["errors"])


def test_validate_unclosed_paren_is_invalid():
    """A second malformed example (unclosed parenthesis) is also rejected."""
    code = "x = (\n" + "# padding comment to exceed the fifty character minimum length\n"
    result = validate_generated_code(code)

    assert result["is_valid"] is False
    assert result["summary"]["syntax_valid"] is False
    assert result["errors"]


# ---------------------------------------------------------------------------
# Req 2.1/2.2/2.3 — a NON-SyntaxError parse failure is still rejected.
#
# ast.parse raises ValueError (not SyntaxError) for source containing null bytes
# on some CPython versions; on others it raises SyntaxError. To exercise the
# generic ``except Exception`` branch deterministically across versions, force
# ast.parse to raise a non-SyntaxError. This tests the real validator branch
# logic — only the parser's failure mode is simulated, not the result.
# ---------------------------------------------------------------------------


def test_validate_non_syntaxerror_parse_failure_is_invalid(monkeypatch):
    """A non-SyntaxError raised during parsing still marks the code invalid."""

    def _raise_value_error(*_args, **_kwargs):
        raise ValueError("source code string cannot contain null bytes")

    monkeypatch.setattr(code_validator.ast, "parse", _raise_value_error)

    result = validate_generated_code(VALID_CODE)

    assert result["is_valid"] is False
    assert result["summary"]["syntax_valid"] is False
    assert result["errors"]
    assert any("could not be parsed" in e for e in result["errors"])


def test_validate_null_byte_input_is_invalid():
    """Null-byte source is rejected regardless of which parse branch fires."""
    code = "import os\x00\ndef foo():\n    return 1\n"
    result = validate_generated_code(code)

    assert result["is_valid"] is False
    assert result["summary"]["syntax_valid"] is False
    assert result["errors"]


# ---------------------------------------------------------------------------
# Req 2.4 — quality_score is a computed integer in [0, 100], > 0 for valid code
# ---------------------------------------------------------------------------


def test_valid_code_quality_score_in_bounds_and_positive():
    """Valid code yields an integer score in [0, 100] that is strictly positive."""
    result = validate_generated_code(VALID_CODE)
    score = result["summary"]["quality_score"]

    assert isinstance(score, int)
    assert 0 <= score <= 100
    assert score > 0


# ---------------------------------------------------------------------------
# Req 2.5 — invalid code scores strictly lower than valid code
# ---------------------------------------------------------------------------


def test_invalid_code_scores_strictly_lower_than_valid():
    """Invalid (malformed) code scores strictly below valid code (0 < valid)."""
    valid_score = validate_generated_code(VALID_CODE)["summary"]["quality_score"]
    invalid_score = validate_generated_code(MALFORMED_CODE)["summary"]["quality_score"]

    assert invalid_score == 0
    assert valid_score > invalid_score


# ---------------------------------------------------------------------------
# Req 2.6 — antipattern findings lower the score and surface a warning
# ---------------------------------------------------------------------------


def test_antipattern_count_lowers_score_and_adds_warning():
    """A positive antipattern_count reduces the score and records a warning."""
    baseline = validate_generated_code(VALID_CODE, antipattern_count=0)
    with_antipatterns = validate_generated_code(VALID_CODE, antipattern_count=2)

    assert with_antipatterns["summary"]["quality_score"] < baseline["summary"]["quality_score"]
    assert any("antipattern" in w.lower() for w in with_antipatterns["warnings"])
    # Score remains within bounds even after the penalty.
    assert 0 <= with_antipatterns["summary"]["quality_score"] <= 100


# ---------------------------------------------------------------------------
# Property 2: Invalid syntax is never valid
# **Validates: Requirements 2.1, 2.2, 2.3**
# ---------------------------------------------------------------------------


@st.composite
def maybe_malformed_code(draw):
    """Mix syntactically valid and known-malformed snippets, plus random suffix.

    The malformed entries guarantee the parse-failure branch is genuinely
    exercised (the property would trivially pass if every sample parsed).
    """
    base = draw(
        st.sampled_from(
            [
                "import os\ndef foo():\n    return 1\n",  # valid
                "import sys\n",  # valid
                "class C:\n    pass\n",  # valid
                "def f(:\n    pass\n",  # malformed
                "x = (\n",  # malformed
                "class C(:\n    pass\n",  # malformed
                "return = 5\n",  # malformed
                "def g(:\n",  # malformed
            ]
        )
    )
    suffix = draw(st.text(max_size=200))
    return base + suffix


@given(maybe_malformed_code())
@settings(max_examples=200)
def test_parse_failure_never_coexists_with_valid_prop(code: str):
    """A parse failure never coexists with is_valid/syntax_valid True, and
    syntax_valid True implies the code actually parses."""
    result = validate_generated_code(code)

    try:
        ast.parse(code)
        parses = True
    except Exception:
        parses = False

    if not parses:
        assert result["is_valid"] is False
        assert result["summary"].get("syntax_valid") is False

    # Conversely: if the validator reports syntax_valid True, the code must parse.
    if result["summary"].get("syntax_valid") is True:
        assert parses


# ---------------------------------------------------------------------------
# Property 3: Quality score bounds
# **Validates: Requirements 2.4, 2.5**
# ---------------------------------------------------------------------------


@given(st.text(max_size=3000))
@settings(max_examples=200)
def test_quality_score_within_bounds_prop(code: str):
    """Whenever a quality_score is reported it is an integer in [0, 100]."""
    result = validate_generated_code(code)
    score = result["summary"].get("quality_score")
    if score is not None:
        assert isinstance(score, int)
        assert 0 <= score <= 100
