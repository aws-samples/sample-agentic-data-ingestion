# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: test file hierarchy placement (Property 2).

Feature: testing-overhaul, Property 2: Test file hierarchy placement

For any Python test file in `tests/`, it resides under exactly one of the
defined subdirectories (`unit/`, `contracts/`, `integration/`, `e2e/`, `cdk/`)
and not at the top level of `tests/`.

This test scans the `tests/` directory and verifies that no `test_*.py` files
exist at the top level. All test files should be organized into the layered
hierarchy.

NOTE: This test is skipped until Task 5 (migration) is complete, since the
91 existing test files are still at the top level. The property documents the
target state of the test hierarchy.

Validates: Requirements 1.1
"""

import pathlib

import pytest

# The allowed subdirectories under tests/ where test files should reside
ALLOWED_SUBDIRS = {"unit", "contracts", "integration", "e2e", "cdk"}

TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _find_top_level_test_files() -> list[str]:
    """Find all test_*.py files sitting directly in the tests/ root directory."""
    top_level_tests = []
    for entry in TESTS_ROOT.iterdir():
        if entry.is_file() and entry.name.startswith("test_") and entry.suffix == ".py":
            top_level_tests.append(entry.name)
    return sorted(top_level_tests)


def _find_test_files_in_subdirs() -> dict[str, list[str]]:
    """Find all test_*.py files in each allowed subdirectory, recursively."""
    result: dict[str, list[str]] = {}
    for subdir in ALLOWED_SUBDIRS:
        subdir_path = TESTS_ROOT / subdir
        if not subdir_path.is_dir():
            result[subdir] = []
            continue
        test_files = sorted(str(p.relative_to(TESTS_ROOT)) for p in subdir_path.rglob("test_*.py"))
        result[subdir] = test_files
    return result


def test_no_test_files_at_top_level():
    """**Validates: Requirements 1.1**

    Property 2: For any Python test file in tests/, it resides under exactly
    one of the defined subdirectories and not at the top level.

    No test_*.py files should exist directly in the tests/ root directory.
    All test files must be organized into unit/, contracts/, integration/,
    e2e/, or cdk/ subdirectories.
    """
    top_level = _find_top_level_test_files()
    assert len(top_level) == 0, (
        f"Found {len(top_level)} test file(s) at the top level of tests/ "
        f"that should be in a subdirectory: {top_level[:10]}"
        + (f" ... and {len(top_level) - 10} more" if len(top_level) > 10 else "")
    )


def test_every_test_file_in_exactly_one_subdir():
    """**Validates: Requirements 1.1**

    Property 2: Each test file resides under exactly one of the defined
    top-level subdirectories. A test file with the same basename may appear
    in multiple directories within the same layer (e.g., different Lambda
    handler dirs under unit/lambdas/), but should not appear across
    different layers (e.g., both unit/ and contracts/).
    """
    files_by_subdir = _find_test_files_in_subdirs()

    # Collect all test file basenames per top-level subdir and check for
    # duplicates ACROSS subdirs (not within the same subdir)
    seen: dict[str, str] = {}  # basename -> first top-level subdir
    duplicates: list[str] = []

    for subdir, files in files_by_subdir.items():
        # Deduplicate within the same subdir (same basename in different
        # Lambda handler dirs is fine)
        basenames_in_subdir = set()
        for filepath in files:
            basename = pathlib.PurePosixPath(filepath).name
            basenames_in_subdir.add(basename)

        for basename in basenames_in_subdir:
            if basename in seen and seen[basename] != subdir:
                duplicates.append(f"{basename} found in both {seen[basename]}/ and {subdir}/")
            elif basename not in seen:
                seen[basename] = subdir

    assert len(duplicates) == 0, f"Test files found in multiple top-level subdirectories: {duplicates}"


def test_allowed_subdirectories_exist():
    """**Validates: Requirements 1.1**

    Structural check: all defined subdirectories exist under tests/.
    This test passes now since Task 1.1 already created the directory structure.
    """
    for subdir in ALLOWED_SUBDIRS:
        subdir_path = TESTS_ROOT / subdir
        assert subdir_path.is_dir(), f"Expected subdirectory tests/{subdir}/ does not exist"


def test_top_level_test_count_documented():
    """**Validates: Requirements 1.1**

    Informational check: report how many test files are still at the top level.
    This test always passes but logs the current state for visibility.
    Once Task 5 migration is complete, the count should be 0.
    """
    top_level = _find_top_level_test_files()
    # This is informational — it always passes but documents the current state
    if top_level:
        pytest.skip(
            f"{len(top_level)} test file(s) still at top level of tests/ "
            f"(expected after Task 5 migration): {top_level[:5]}"
            + (f" ... and {len(top_level) - 5} more" if len(top_level) > 5 else "")
        )
