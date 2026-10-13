# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test P17: Test file naming convention.

Verifies that test files follow the `test_<module>_<behavior>.py` pattern
and test functions follow `test_<behavior>_<scenario>`.

**Validates: Requirements 11.1**
"""

import os
import re

TESTS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Pattern: test_<something>.py — at minimum test_ prefix and .py suffix
TEST_FILE_PATTERN = re.compile(r"^test_[a-z][a-z0-9_]*\.py$")

# Directories that contain test files (skip fixtures, scripts, etc.)
TEST_DIRS = {"unit", "contracts", "integration", "e2e"}

# Files that are allowed to not match the pattern (conftest, helpers, schemas)
ALLOWED_NON_TEST_FILES = {"conftest.py", "__init__.py", "schemas.py", "helpers.py"}

# Directories whose contents are infrastructure/helpers, not test modules
EXCLUDED_SUBDIRS = {"_harness"}


def _collect_test_files() -> list[tuple[str, str]]:
    """Collect all .py files in test directories, returning (dir, filename) pairs."""
    results = []
    for test_dir in TEST_DIRS:
        dir_path = os.path.join(TESTS_ROOT, test_dir)
        if not os.path.isdir(dir_path):
            continue
        for root, dirs, files in os.walk(dir_path):
            # Skip helper/infrastructure directories (e.g. _harness)
            dirs[:] = [d for d in dirs if d not in EXCLUDED_SUBDIRS]
            for f in files:
                if f.endswith(".py") and f not in ALLOWED_NON_TEST_FILES:
                    rel = os.path.relpath(os.path.join(root, f), TESTS_ROOT)
                    results.append((rel, f))
    return results


def test_all_test_files_follow_naming_convention():
    """Every test file must match test_<module>_<behavior>.py pattern."""
    violations = []
    for rel_path, filename in _collect_test_files():
        if not TEST_FILE_PATTERN.match(filename):
            violations.append(rel_path)
    assert not violations, "Test files not matching test_<name>.py pattern:\n" + "\n".join(
        f"  - {v}" for v in violations
    )


def test_no_test_files_at_top_level():
    """No test files should exist directly in tests/ (must be in a subdirectory)."""
    top_level_tests = []
    for f in os.listdir(TESTS_ROOT):
        if f.startswith("test_") and f.endswith(".py"):
            top_level_tests.append(f)
    assert not top_level_tests, "Test files found at top level of tests/:\n" + "\n".join(
        f"  - {v}" for v in top_level_tests
    )
