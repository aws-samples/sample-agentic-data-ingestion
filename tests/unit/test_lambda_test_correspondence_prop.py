# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: Lambda-to-test directory correspondence (Property 1).

Feature: testing-overhaul, Property 1: Lambda-to-test directory correspondence

For any Lambda function directory under `lambda/agentic_data_ingestion_*/`, a
corresponding test directory exists under `tests/unit/lambdas/` with at least
one test file.

**Validates: Requirements 1.2, 11.5**
"""

import pathlib

TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECT_ROOT = TESTS_ROOT.parent

LAMBDA_DIR = PROJECT_ROOT / "lambda"
UNIT_LAMBDAS_DIR = TESTS_ROOT / "unit" / "lambdas"

# Lambda directories that are known exceptions (no handler, no tests needed)
KNOWN_EXCEPTIONS: set[str] = set()

# Lambda directories that have test directories but no test files yet
# These are documented coverage gaps from design Section 0.2
KNOWN_EMPTY_TEST_DIRS = {
    "approval_config",  # Tests exist in dashboard_api/ (test_create_pipeline_approval_config.py)
    "approval_timeout_handler",  # No dedicated tests yet (coverage gap)
    "create_quality_checks",  # No dedicated tests yet (coverage gap per design 0.2)
    "job_completion",  # No dedicated tests yet (coverage gap per design 0.2)
    "run_pipeline",  # No dedicated tests yet (coverage gap per design 0.2)
    "schema_compare",  # No dedicated tests yet (coverage gap per design 0.2)
}

# Mapping from Lambda directory name (without agentic_data_ingestion_ prefix) to
# test directory name under tests/unit/lambdas/
# Most follow a direct mapping, but some have shortened names
LAMBDA_TO_TEST_DIR = {
    "approval_config": "approval_config",
    "approval_handler": "approval_handler",
    "approval_timeout_handler": "approval_timeout",
    "create_code": "create_code",
    "create_glue_pipeline": "create_glue_pipeline",
    "create_infrastructure": "create_infrastructure",
    "create_quality_checks": "create_quality_checks",
    "dashboard_api": "dashboard_api",
    "execution_updater": "execution_updater",
    "file_validation": "file_validation",
    "job_completion": "job_completion",
    "pipeline_trigger": "pipeline_trigger",
    "run_pipeline": "run_pipeline",
    "schema_compare": "schema_compare",
    "schema_extract": "schema_extract",
    "schema_mapping": "schema_mapping",
    "test_code": "test_code",
    "ws_broadcast": "ws_broadcast",
    "ws_connect": "ws_connect",
    "ws_disconnect": "ws_disconnect",
}


def _get_lambda_functions() -> list[str]:
    """Get all Lambda function names that have a lambda_function.py handler."""
    lambda_dirs = []
    if not LAMBDA_DIR.is_dir():
        return []

    for entry in sorted(LAMBDA_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if not entry.name.startswith("agentic_data_ingestion_"):
            continue

        # Extract the short name (without agentic_data_ingestion_ prefix)
        short_name = entry.name.replace("agentic_data_ingestion_", "")

        # Skip known exceptions
        if short_name in KNOWN_EXCEPTIONS:
            continue

        # Check if it has a lambda_function.py
        handler = entry / "lambda_function.py"
        if handler.is_file():
            lambda_dirs.append(short_name)

    return lambda_dirs


def _get_test_dir_for_lambda(lambda_name: str) -> pathlib.Path:
    """Get the expected test directory for a Lambda function."""
    test_dir_name = LAMBDA_TO_TEST_DIR.get(lambda_name, lambda_name)
    return UNIT_LAMBDAS_DIR / test_dir_name


class TestLambdaTestCorrespondence:
    """Property 1: Lambda-to-test directory correspondence.

    **Validates: Requirements 1.2, 11.5**
    """

    def test_every_lambda_has_test_directory(self):
        """For any Lambda function directory with a handler, a corresponding
        test directory exists under tests/unit/lambdas/.

        **Validates: Requirements 1.2, 11.5**
        """
        lambda_functions = _get_lambda_functions()
        assert len(lambda_functions) > 0, "No Lambda functions found"

        missing_dirs = []
        for lambda_name in lambda_functions:
            test_dir = _get_test_dir_for_lambda(lambda_name)
            if not test_dir.is_dir():
                missing_dirs.append(lambda_name)

        assert len(missing_dirs) == 0, (
            f"Lambda functions without test directories: {missing_dirs}\nExpected directories under tests/unit/lambdas/"
        )

    def test_every_lambda_test_dir_has_test_file(self):
        """For any Lambda function directory with a handler, the corresponding
        test directory contains at least one test_*.py file.

        Known coverage gaps (documented in design Section 0.2) are excluded.

        **Validates: Requirements 1.2, 11.5**
        """
        lambda_functions = _get_lambda_functions()
        assert len(lambda_functions) > 0, "No Lambda functions found"

        empty_dirs = []
        for lambda_name in lambda_functions:
            if lambda_name in KNOWN_EMPTY_TEST_DIRS:
                continue  # Documented coverage gap

            test_dir = _get_test_dir_for_lambda(lambda_name)
            if not test_dir.is_dir():
                continue  # Covered by test_every_lambda_has_test_directory

            test_files = list(test_dir.glob("test_*.py"))
            if len(test_files) == 0:
                empty_dirs.append(lambda_name)

        assert len(empty_dirs) == 0, (
            f"Lambda test directories with no test files: {empty_dirs}\n"
            f"Each directory under tests/unit/lambdas/ must contain at least "
            f"one test_*.py file"
        )

    def test_known_exceptions_documented(self):
        """Known exceptions (Lambdas without tests) are documented and valid.

        **Validates: Requirements 1.2, 11.5**
        """
        for exception_name in KNOWN_EXCEPTIONS:
            lambda_dir = LAMBDA_DIR / f"agentic_data_ingestion_{exception_name}"
            assert lambda_dir.is_dir(), (
                f"Known exception '{exception_name}' does not exist as a "
                f"Lambda directory — remove from KNOWN_EXCEPTIONS"
            )
            # Verify the exception is valid (no handler)
            handler = lambda_dir / "lambda_function.py"
            assert not handler.is_file(), (
                f"Known exception '{exception_name}' now has a "
                f"lambda_function.py — it needs tests and should be removed "
                f"from KNOWN_EXCEPTIONS"
            )

    def test_lambda_count_is_expected(self):
        """Sanity check: we find the expected number of Lambda functions.

        **Validates: Requirements 1.2, 11.5**
        """
        lambda_functions = _get_lambda_functions()
        # We expect 20 Lambda dirs with handlers
        assert len(lambda_functions) >= 18, (
            f"Expected at least 18 Lambda functions with handlers, found {len(lambda_functions)}: {lambda_functions}"
        )
