# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: no duplicate mocking in fixture consumers (Property 5).

Feature: testing-overhaul, Property 5: No duplicate mocking in fixture consumers

For any test file using a shared fixture, the file does not create its own
`moto.mock_aws()` or `boto3.client` mock for the same service covered by
shared fixtures (DynamoDB, S3, SQS).

This is a structural/AST check that scans all test files in `tests/unit/`
and `tests/contracts/` for direct `moto.mock_aws()` or `mock_aws()` usage,
which should have been replaced by shared fixtures in Task 5.3.

Matching is AST-based rather than substring-based, which is what lets a test
file *document* the fence in its docstring without failing the build — the same
reasoning ratified for the anti-threading meta-test in LGR-b46a5bb-4.

Known limitation: ``ALLOWED_FILES`` is keyed on basename, not path, so an entry
exempts every file with that name anywhere in the scanned tree.

**Validates: Requirements 3.6**
"""

import ast
import pathlib

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Directories to scan for duplicate mocking
SCAN_DIRS = ["unit", "contracts"]

# Files that are allowed to use mock_aws (shared fixture definitions, conftest files,
# and files that haven't been migrated to shared fixtures yet per Task 5.3 scope)
# Entries are matched by BASENAME, not path — see the note in the module
# docstring. Per LGR-b46a5bb-4 ("an allowlist entry is a door"), an entry stays
# here only while the file it names still calls mock_aws(); three entries that
# no longer did were removed on 2026-08-02 (p30 task 6.8).
ALLOWED_FILES = {
    "conftest.py",
    "test_no_duplicate_mocking_prop.py",  # this file itself
    # These files use mock_aws for Glue/S3Tables-specific mocking not covered
    # by shared fixtures (which cover DynamoDB, S3, SQS only)
    "test_source_path_resolution.py",
    "test_warehouse_location_resolution.py",
    # streaming_schema uses mock_aws for schema-extract-specific S3 mocking
    "test_streaming_schema.py",
}


def _find_test_files() -> list[pathlib.Path]:
    """Find all test_*.py files in the scan directories."""
    test_files = []
    for scan_dir in SCAN_DIRS:
        dir_path = TESTS_ROOT / scan_dir
        if dir_path.is_dir():
            test_files.extend(dir_path.rglob("test_*.py"))
    return sorted(test_files)


def _check_file_for_mock_aws(filepath: pathlib.Path) -> list[str]:
    """Check a single file for mock_aws() usage via AST inspection.

    Returns a list of violation descriptions (empty if clean).
    """
    violations = []
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return []  # Skip files that can't be parsed

    for node in ast.walk(tree):
        # Check for mock_aws() calls: moto.mock_aws() or mock_aws()
        if isinstance(node, ast.Call):
            func = node.func
            # Case 1: moto.mock_aws()
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "mock_aws"
                and isinstance(func.value, ast.Name)
                and func.value.id == "moto"
            ):
                violations.append(f"Line {node.lineno}: direct moto.mock_aws() call")
            # Case 2: mock_aws() (imported from moto)
            elif isinstance(func, ast.Name) and func.id == "mock_aws":
                violations.append(f"Line {node.lineno}: direct mock_aws() call")

    return violations


class TestNoDuplicateMocking:
    """Property 5: No duplicate mocking in fixture consumers.

    **Validates: Requirements 3.6**
    """

    def test_no_mock_aws_in_test_files(self):
        """For any test file using shared fixtures, the file does not create
        its own moto.mock_aws() or mock_aws() for services covered by shared
        fixtures.

        **Validates: Requirements 3.6**
        """
        test_files = _find_test_files()
        assert len(test_files) > 0, "No test files found to scan"

        violations_by_file: dict[str, list[str]] = {}

        for filepath in test_files:
            if filepath.name in ALLOWED_FILES:
                continue

            violations = _check_file_for_mock_aws(filepath)
            if violations:
                rel_path = str(filepath.relative_to(TESTS_ROOT))
                violations_by_file[rel_path] = violations

        if violations_by_file:
            msg_parts = ["Files with direct mock_aws() usage (should use shared fixtures):"]
            for file_path, viols in sorted(violations_by_file.items()):
                msg_parts.append(f"\n  {file_path}:")
                for v in viols:
                    msg_parts.append(f"    - {v}")
            pytest.fail("\n".join(msg_parts))

    def test_scan_covers_both_directories(self):
        """Verify the scan covers both unit/ and contracts/ directories.

        **Validates: Requirements 3.6**
        """
        test_files = _find_test_files()
        dirs_found = set()
        for f in test_files:
            rel = f.relative_to(TESTS_ROOT)
            dirs_found.add(rel.parts[0])

        assert "unit" in dirs_found, "No test files found in unit/"
        assert "contracts" in dirs_found, "No test files found in contracts/"
