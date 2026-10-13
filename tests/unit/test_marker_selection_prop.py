# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: marker-based test selection (Property 16).

Feature: testing-overhaul, Property 16: Marker-based test selection

For any marker `m` in {unit, contract, integration, e2e, cdk},
`pytest -m m` runs exactly the tests in `tests/{m}/` (or `tests/contracts/`
for contract) and no tests from other layers.

This test creates temporary stub test files in each layer directory, then
verifies that `pytest --collect-only -m <marker>` collects the stub from
the expected directory and does NOT collect stubs from other layer directories.

Legacy flat test files at the top level of tests/ are excluded from the
assertion since they have not yet been migrated to the layered hierarchy.

Validates: Requirements 10.2, 10.3
"""

import os
import subprocess
import textwrap

import pytest

# The test layers: marker name -> directory name under tests/
LAYER_MARKERS = {
    "unit": "unit",
    "contract": "contracts",
    "integration": "integration",
    "e2e": "e2e",
    "cdk": "cdk",
}

# Unique stub filename so we can identify our stubs in collected output
STUB_FILENAME = "test_stub_marker_prop.py"

TESTS_ROOT = os.path.join(os.path.dirname(__file__), os.pardir)


def _tests_root():
    """Return the absolute path to the tests/ directory."""
    return os.path.abspath(TESTS_ROOT)


def _stub_path(layer_dir: str) -> str:
    """Return the path for a temporary stub test file in the given layer directory."""
    return os.path.join(_tests_root(), layer_dir, STUB_FILENAME)


STUB_CONTENT = textwrap.dedent("""\
    \"\"\"Temporary stub test for marker selection property test.\"\"\"

    def test_stub_for_marker_selection():
        assert True
""")


def _create_stubs():
    """Create a temporary stub test file in each layer directory."""
    for _marker, layer_dir in LAYER_MARKERS.items():
        path = _stub_path(layer_dir)
        with open(path, "w") as f:
            f.write(STUB_CONTENT)


def _remove_stubs():
    """Remove all temporary stub test files."""
    for _marker, layer_dir in LAYER_MARKERS.items():
        path = _stub_path(layer_dir)
        if os.path.exists(path):
            os.remove(path)


def _collect_tests_for_marker(marker: str) -> list[str]:
    """Run pytest --collect-only -m <marker> and return collected test node IDs."""
    project_root = os.path.abspath(os.path.join(_tests_root(), os.pardir))
    result = subprocess.run(
        [
            "python",
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            marker,
            "--no-header",
            "--override-ini=addopts=",
            "--continue-on-collection-errors",
        ],
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    lines = result.stdout.strip().splitlines()
    # Filter to only test node IDs (contain "::" separator)
    return [line.strip() for line in lines if "::" in line]


@pytest.mark.parametrize("marker,expected_dir", list(LAYER_MARKERS.items()))
def test_marker_selects_only_tests_from_expected_directory(marker, expected_dir):
    """**Validates: Requirements 10.2, 10.3**

    For any marker m in {unit, contract, integration, e2e, cdk},
    `pytest -m m` collects the stub test from `tests/{expected_dir}/`
    and does NOT collect stub tests from other layer directories.

    Legacy flat test files at the top level of tests/ are excluded from
    the assertion — they will be migrated in a later task.
    """
    try:
        _create_stubs()
        collected = _collect_tests_for_marker(marker)

        # Filter to only our stub tests (ignore legacy flat tests and other tests)
        stub_nodes = [n for n in collected if STUB_FILENAME in n]

        # The stub from the expected directory must be collected
        expected_stubs = [n for n in stub_nodes if f"tests/{expected_dir}/" in n or f"tests\\{expected_dir}\\" in n]
        assert len(expected_stubs) == 1, (
            f"pytest -m {marker} should collect exactly 1 stub from "
            f"tests/{expected_dir}/, got {len(expected_stubs)}: {expected_stubs}"
        )

        # Stubs from OTHER layer directories must NOT be collected
        other_dirs = [d for m, d in LAYER_MARKERS.items() if m != marker]
        for other_dir in other_dirs:
            leaked_stubs = [n for n in stub_nodes if f"tests/{other_dir}/" in n or f"tests\\{other_dir}\\" in n]
            assert len(leaked_stubs) == 0, (
                f"pytest -m {marker} should NOT collect stubs from tests/{other_dir}/, but found: {leaked_stubs}"
            )
    finally:
        _remove_stubs()


def test_each_layer_conftest_exists_and_applies_marker():
    """Verify that each layer directory has a conftest.py with marker auto-application.

    This is a structural check that the conftest.py files exist and contain
    the expected pytest_collection_modifyitems hook with the correct marker.
    """
    for marker, layer_dir in LAYER_MARKERS.items():
        conftest_path = os.path.join(_tests_root(), layer_dir, "conftest.py")
        assert os.path.isfile(conftest_path), f"Missing conftest.py in tests/{layer_dir}/"

        with open(conftest_path) as f:
            content = f.read()

        assert "pytest_collection_modifyitems" in content, (
            f"tests/{layer_dir}/conftest.py missing pytest_collection_modifyitems hook"
        )
        assert f"pytest.mark.{marker}" in content, f"tests/{layer_dir}/conftest.py does not apply pytest.mark.{marker}"


def test_markers_registered_in_root_conftest():
    """Verify that all layer markers are registered in the root conftest.py."""
    root_conftest = os.path.join(_tests_root(), "conftest.py")
    assert os.path.isfile(root_conftest), "Missing root tests/conftest.py"

    with open(root_conftest) as f:
        content = f.read()

    for marker in LAYER_MARKERS:
        assert marker in content, f"Root conftest.py does not register the '{marker}' marker"
