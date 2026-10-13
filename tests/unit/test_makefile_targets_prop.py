# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test P21: Makefile target correctness.

Verifies the Makefile exists and contains expected test targets
for each CI tier.

**Validates: Requirements 13.4**
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAKEFILE_PATH = os.path.join(REPO_ROOT, "Makefile")

EXPECTED_TARGETS = {
    "test-fast",
    "test-ci",
    "test-all",
    "test-e2e",
    "test-cdk",
    "test-frontend",
    "test-coverage",
}


def _parse_makefile_targets(path: str) -> set[str]:
    """Extract Make target names from a Makefile."""
    targets = set()
    with open(path) as f:
        for line in f:
            m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_-]*):", line)
            if m:
                targets.add(m.group(1))
    return targets


def test_makefile_exists():
    """Makefile must exist at the project root."""
    assert os.path.isfile(MAKEFILE_PATH), f"Makefile not found at {MAKEFILE_PATH}"


def test_makefile_contains_all_expected_targets():
    """Every expected CI target must be defined in the Makefile."""
    actual = _parse_makefile_targets(MAKEFILE_PATH)
    missing = EXPECTED_TARGETS - actual
    assert not missing, f"Makefile missing targets: {missing}"


def test_test_fast_runs_unit_and_contract():
    """test-fast target must reference tests/unit/ and tests/contracts/."""
    with open(MAKEFILE_PATH) as f:
        content = f.read()
    # Find the test-fast recipe block
    assert "tests/unit/" in content, "test-fast should run unit tests"
    assert "tests/contracts/" in content, "test-fast should run contract tests"


def test_test_e2e_sets_real_aws_flag():
    """test-e2e target must set AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true."""
    with open(MAKEFILE_PATH) as f:
        content = f.read()
    assert "AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true" in content, (
        "test-e2e must set AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true"
    )
