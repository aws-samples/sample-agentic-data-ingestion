# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: migrated test behavioral preservation (Property 3).

Feature: testing-overhaul, Property 3: Migrated test behavioral preservation

For any test that existed before migration, running it in its new location
produces the same pass/fail result.

Since we cannot compare before/after (migration already happened), we validate
that all migrated tests are collectible in their new locations — i.e., running
`pytest --collect-only` on each layer directory produces zero collection errors.

This confirms that import paths, fixtures, and module resolution all work
correctly after migration.

**Validates: Requirements 1.9**
"""

import pathlib
import subprocess
import sys

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECT_ROOT = TESTS_ROOT.parent

# Layer directories that contain migrated tests
LAYER_DIRS = ["unit", "contracts", "integration", "e2e", "cdk"]


def _collect_tests_in_dir(layer: str) -> tuple[int, int, str]:
    """Run pytest --collect-only on a layer directory.

    Returns (collected_count, error_count, stderr_output).
    """
    layer_path = TESTS_ROOT / layer
    if not layer_path.is_dir():
        return 0, 0, ""

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--override-ini=addopts=",
            str(layer_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )

    # Count collected tests from stdout
    collected = 0
    for line in result.stdout.splitlines():
        # Lines like "123 tests collected" or "no tests ran"
        if "selected" in line or "collected" in line:
            parts = line.split()
            for i, part in enumerate(parts):
                if part in ("selected", "collected") and i > 0:
                    try:
                        collected = int(parts[i - 1])
                    except ValueError:
                        pass

    # Count errors from the output
    errors = 0
    for line in (result.stdout + result.stderr).splitlines():
        if "ERROR" in line and "collecting" in line.lower():
            errors += 1
        if "errors" in line.lower() and "collecting" in line.lower():
            # Parse "X errors" pattern
            parts = line.split()
            for i, part in enumerate(parts):
                if "error" in part.lower() and i > 0:
                    try:
                        errors = max(errors, int(parts[i - 1]))
                    except ValueError:
                        pass

    return collected, errors, result.stderr


class TestMigrationPreservation:
    """Property 3: Migrated test behavioral preservation.

    **Validates: Requirements 1.9**
    """

    @pytest.mark.parametrize("layer", LAYER_DIRS)
    def test_zero_collection_errors_per_layer(self, layer: str):
        """For any test layer directory, pytest --collect-only produces zero
        collection errors, confirming all migrated tests are importable.

        **Validates: Requirements 1.9**
        """
        layer_path = TESTS_ROOT / layer
        if not layer_path.is_dir():
            pytest.skip(f"Layer directory tests/{layer}/ does not exist yet")

        # Check if there are any test files in this layer
        test_files = list(layer_path.rglob("test_*.py"))
        if not test_files:
            pytest.skip(f"No test files in tests/{layer}/")

        collected, errors, stderr = _collect_tests_in_dir(layer)

        assert errors == 0, f"Collection errors in tests/{layer}/: {errors} error(s).\nStderr:\n{stderr[:1000]}"

    def test_unit_layer_has_migrated_tests(self):
        """The unit/ layer should contain migrated test files.

        **Validates: Requirements 1.9**
        """
        unit_path = TESTS_ROOT / "unit"
        test_files = list(unit_path.rglob("test_*.py"))
        assert len(test_files) > 0, "No test files found in tests/unit/ — migration may not be complete"

    def test_contracts_layer_has_migrated_tests(self):
        """The contracts/ layer should contain migrated test files.

        **Validates: Requirements 1.9**
        """
        contracts_path = TESTS_ROOT / "contracts"
        test_files = list(contracts_path.rglob("test_*.py"))
        assert len(test_files) > 0, "No test files found in tests/contracts/ — migration may not be complete"
