# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: Lambda error path coverage (Property 26).

**Property 26**: For any Lambda handler, unit tests exist covering malformed
input, missing fields, and at least one AWS service exception.

This is a structural check: scan tests/unit/lambdas/*/test_error_paths.py
files and verify they exist for key handlers. Verify each error path test
file contains tests for malformed input and service exceptions.

**Validates: Requirements 17.1**
"""

import ast
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TESTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
UNIT_LAMBDAS_DIR = os.path.join(TESTS_DIR, "unit", "lambdas")

# Key handlers that MUST have error path tests
KEY_HANDLERS = [
    "pipeline_trigger",
    "execution_updater",
    "file_validation",
    "dashboard_api",
]

# Keywords that indicate malformed input tests
MALFORMED_INPUT_KEYWORDS = [
    "malformed",
    "missing",
    "invalid",
    "empty",
    "no_such",
    "not_found",
    "bad_",
    "corrupt",
    "broken",
]

# Keywords that indicate AWS service exception tests
SERVICE_EXCEPTION_KEYWORDS = [
    "exception",
    "error",
    "failed",
    "failure",
    "raises",
    "client_error",
    "no_such_key",
    "already_exists",
    "conditional_check",
    "throttl",
    "timeout",
    "concurrency",
    "exceeded",
    "limit",
    "500",
    "404",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_test_function_names(filepath: str) -> list[str]:
    """Parse a Python file and return all test function/method names."""
    with open(filepath) as f:
        source = f.read()
    tree = ast.parse(source)
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                names.append(node.name.lower())
    return names


def _has_keyword_match(names: list[str], keywords: list[str]) -> bool:
    """Check if any test name contains any of the keywords."""
    for name in names:
        for kw in keywords:
            if kw in name:
                return True
    return False


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------

handler_strategy = st.sampled_from(KEY_HANDLERS)


@given(handler_name=handler_strategy)
@settings(max_examples=10)
def test_error_path_test_file_exists_for_key_handler(handler_name):
    """Property 26: For any key Lambda handler, a test_error_paths.py file
    exists under tests/unit/lambdas/<handler>/.

    **Validates: Requirements 17.1**
    """
    error_path_file = os.path.join(UNIT_LAMBDAS_DIR, handler_name, "test_error_paths.py")
    assert os.path.isfile(error_path_file), (
        f"Missing error path test file for handler '{handler_name}': expected {error_path_file}"
    )


@given(handler_name=handler_strategy)
@settings(max_examples=10)
def test_error_path_tests_cover_malformed_input(handler_name):
    """Property 26: Each error path test file contains tests for malformed input.

    **Validates: Requirements 17.1**
    """
    error_path_file = os.path.join(UNIT_LAMBDAS_DIR, handler_name, "test_error_paths.py")
    if not os.path.isfile(error_path_file):
        pytest.skip(f"No error path file for {handler_name}")

    test_names = _get_test_function_names(error_path_file)
    assert _has_keyword_match(test_names, MALFORMED_INPUT_KEYWORDS), (
        f"Error path tests for '{handler_name}' do not cover malformed input. Test names found: {test_names}"
    )


@given(handler_name=handler_strategy)
@settings(max_examples=10)
def test_error_path_tests_cover_service_exceptions(handler_name):
    """Property 26: Each error path test file contains tests for AWS service exceptions.

    **Validates: Requirements 17.1**
    """
    error_path_file = os.path.join(UNIT_LAMBDAS_DIR, handler_name, "test_error_paths.py")
    if not os.path.isfile(error_path_file):
        pytest.skip(f"No error path file for {handler_name}")

    test_names = _get_test_function_names(error_path_file)
    assert _has_keyword_match(test_names, SERVICE_EXCEPTION_KEYWORDS), (
        f"Error path tests for '{handler_name}' do not cover service exceptions. Test names found: {test_names}"
    )
