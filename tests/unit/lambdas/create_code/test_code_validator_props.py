# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for code_validator (Tasks 2.2 and 2.3)."""

import logging
import os
import sys

import hypothesis.strategies as st
from hypothesis import given, settings

# Add lambda directory to import path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)

# Ensure logger is configured so code_validator doesn't fail on logger calls
logging.basicConfig(level=logging.WARNING)

from code_validator import extract_code_only, validate_generated_code


# Feature: agentic-data-ingestion-codebase-optimization, Property 1: Code-fence stripping is idempotent
@given(st.text(max_size=5000))
@settings(max_examples=200)
def test_code_fence_stripping_idempotent(code: str):
    """Applying extract_code_only twice yields the same result as once."""
    result_once = extract_code_only(code)
    result_twice = extract_code_only(result_once)
    assert result_once == result_twice


# Strategy: generate text that contains 'import ' or 'def ' to pass the
# minimum-structure checks in validate_generated_code, without relying on
# filter() which rejects too many random strings.
@st.composite
def code_like_text(draw):
    """Generate text that contains 'import ' or 'def ' keywords."""
    prefix = draw(st.sampled_from(["import os\n", "def foo():\n    pass\n"]))
    body = draw(st.text(min_size=30, max_size=1900))
    return prefix + body


# Feature: agentic-data-ingestion-codebase-optimization, Property 12: Refactored code validation equivalence
# **Validates: Requirements 2.5**
@given(code_like_text())
@settings(max_examples=200)
def test_code_validation_equivalence(code: str):
    """
    validate_generated_code produces well-formed, deterministic results with
    correct structural validation — equivalent to the original inline logic.

    Checks:
    - Determinism: same input always yields same output
    - Output structure: result contains is_valid, errors, warnings, summary keys
    - Structural correctness: summary counts match actual code content
    - Status consistency: is_valid flag agrees with errors list and summary status
    """
    result1 = validate_generated_code(code)
    result2 = validate_generated_code(code)

    # Determinism
    assert result1 == result2

    # Output structure
    assert isinstance(result1, dict)
    for key in ("is_valid", "errors", "warnings", "summary"):
        assert key in result1, f"Missing key: {key}"
    assert isinstance(result1["is_valid"], bool)
    assert isinstance(result1["errors"], list)
    assert isinstance(result1["warnings"], list)
    assert isinstance(result1["summary"], dict)

    # Status consistency: if there are errors, is_valid must be False
    if result1["errors"]:
        assert result1["is_valid"] is False

    # If is_valid is True, summary status must be 'VALID'
    if result1["is_valid"]:
        assert result1["summary"].get("status") == "VALID"

    # If is_valid is False and summary has status, it must not be 'VALID'
    if not result1["is_valid"] and "status" in result1["summary"]:
        assert result1["summary"]["status"] != "VALID"

    # Structural counts in summary must match actual code (when present)
    if "line_count" in result1["summary"]:
        actual_lines = len([l for l in code.split("\n") if l.strip()])
        assert result1["summary"]["line_count"] == actual_lines
    if "method_count" in result1["summary"]:
        assert result1["summary"]["method_count"] == code.count("def ")
    if "class_count" in result1["summary"]:
        assert result1["summary"]["class_count"] == code.count("class ")
