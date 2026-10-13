# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: golden script coverage (Property 24).

Feature: testing-overhaul, Property 24: Golden script coverage

For any combination of target type and write mode, a golden-file PySpark
script exists in `tests/fixtures/golden_scripts/`. At least one
checkpoint-enabled variant exists.

**Validates: Requirements 16.6**
"""

import os

import pytest

# All (target_type, write_mode) combinations that must have golden scripts
GOLDEN_SCRIPT_MATRIX = [
    ("s3_tables", "append"),
    ("s3_tables", "overwrite"),
    ("s3_tables", "merge"),
    ("glue_catalog", "append"),
    ("glue_catalog", "overwrite"),
    ("s3_parquet", "append"),
    ("s3_parquet", "overwrite"),
    ("jdbc", "append"),
    ("jdbc", "overwrite"),
]

# Checkpoint-enabled variants (at least one must exist)
CHECKPOINT_VARIANTS = [
    ("s3_tables", "append", "checkpoint"),
]

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "fixtures", "golden_scripts")


def _golden_path(target_type: str, write_mode: str, suffix: str = "") -> str:
    """Build the expected golden script filename."""
    name = f"{target_type}_{write_mode}"
    if suffix:
        name = f"{name}_{suffix}"
    return os.path.join(GOLDEN_DIR, f"{name}.py")


@pytest.mark.parametrize(
    "target_type,write_mode",
    GOLDEN_SCRIPT_MATRIX,
    ids=[f"{t}-{w}" for t, w in GOLDEN_SCRIPT_MATRIX],
)
def test_golden_script_exists_and_nonempty(target_type, write_mode):
    """**Validates: Requirements 16.6**

    For any combination of target type and write mode, a golden-file
    PySpark script exists in tests/fixtures/golden_scripts/ and is non-empty.
    """
    path = _golden_path(target_type, write_mode)
    assert os.path.isfile(path), f"Missing golden script for {target_type}/{write_mode}: {path}"
    size = os.path.getsize(path)
    assert size > 0, f"Golden script is empty for {target_type}/{write_mode}: {path}"


def test_at_least_one_checkpoint_variant_exists():
    """**Validates: Requirements 16.6**

    At least one checkpoint-enabled golden script variant exists and
    contains checkpoint logic.
    """
    found = False
    for target_type, write_mode, suffix in CHECKPOINT_VARIANTS:
        path = _golden_path(target_type, write_mode, suffix)
        if os.path.isfile(path):
            with open(path) as f:
                content = f.read()
            assert "checkpoint_path" in content, f"Checkpoint variant {path} does not contain checkpoint logic"
            found = True

    assert found, f"No checkpoint-enabled golden script variant found. Expected at least one of: {CHECKPOINT_VARIANTS}"


def test_golden_scripts_directory_contains_expected_count():
    """Verify the golden_scripts directory has exactly the expected number of files."""
    abs_dir = os.path.abspath(GOLDEN_DIR)
    assert os.path.isdir(abs_dir), f"Golden scripts directory missing: {abs_dir}"

    py_files = [f for f in os.listdir(abs_dir) if f.endswith(".py")]
    # 9 base combinations + at least 1 checkpoint variant = 10 minimum
    expected_min = len(GOLDEN_SCRIPT_MATRIX) + len(CHECKPOINT_VARIANTS)
    assert len(py_files) >= expected_min, (
        f"Expected at least {expected_min} golden scripts, found {len(py_files)}: {py_files}"
    )
