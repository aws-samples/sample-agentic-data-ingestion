# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: the tier-2 test gate's index is complete and accurate.

`.kiro/steering/testing-standards.md` defines the per-task gate as "the directory
that maps to what you changed, per `test-mapping.md`". That makes the table in
`.kiro/steering/test-mapping.md` load-bearing: a Lambda missing from it has no
mapped directory, so the gate resolves to nothing and the developer runs no tests
while believing they followed the guidance.

This test reads that table as the source of truth and diffs it against the
filesystem in both directions. It deliberately does NOT restate the mapping in a
Python dict — `test_lambda_test_correspondence_prop.py` does that, and its copy
drifted stale in exactly the same way the steering table did (both were missing
`agent_dispatch`, `record_failure`, `self_healing_dispatch` and `ws_authorizer`).
A duplicated mapping cannot detect divergence from the thing it duplicates, so
the table is parsed rather than mirrored.

Validates: the tier-2 gate contract in .kiro/steering/testing-standards.md
"""

from __future__ import annotations

import pathlib
import re

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECT_ROOT = TESTS_ROOT.parent
REPO_ROOT = PROJECT_ROOT.parent

LAMBDA_DIR = PROJECT_ROOT / "lambda"
UNIT_LAMBDAS_DIR = TESTS_ROOT / "unit" / "lambdas"
MAPPING_DOC = REPO_ROOT / ".kiro" / "steering" / "test-mapping.md"

LAMBDA_PREFIX = "agentic_data_ingestion_"

# A row's Lambda cell, e.g. `lambda/agentic_data_ingestion_ws_connect/`
_LAMBDA_CELL = re.compile(r"lambda/(" + LAMBDA_PREFIX + r"[a-z0-9_]+)/")
# A row's test cell, e.g. `tests/unit/lambdas/ws_connect/`
_TEST_CELL = re.compile(r"tests/unit/lambdas/([a-z0-9_]+)/")
# The marker for a Lambda with no test directory of its own.
_NO_TESTS = "no tests"


def _require_doc() -> str:
    if not MAPPING_DOC.is_file():
        pytest.skip(f"steering doc not present in this tree: {MAPPING_DOC}")
    return MAPPING_DOC.read_text(encoding="utf-8")


def _parse_rows() -> dict[str, str | None]:
    """Parse the mapping table.

    Returns Lambda directory name -> test directory name, or None for a row
    explicitly marked as having no tests.
    """
    rows: dict[str, str | None] = {}
    for line in _require_doc().splitlines():
        if not line.startswith("|"):
            continue
        cells = line.split("|")
        lambda_match = _LAMBDA_CELL.search(cells[1]) if len(cells) > 1 else None
        if lambda_match is None:
            continue  # header, separator, or prose
        test_cell = cells[2] if len(cells) > 2 else ""
        test_match = _TEST_CELL.search(test_cell)
        if test_match is not None:
            rows[lambda_match.group(1)] = test_match.group(1)
        elif _NO_TESTS in test_cell.lower():
            rows[lambda_match.group(1)] = None
        else:
            pytest.fail(
                f"Row for {lambda_match.group(1)!r} names neither a "
                f"tests/unit/lambdas/<dir>/ path nor {_NO_TESTS!r}: {line.strip()}"
            )
    return rows


def _lambda_dirs_with_handlers() -> set[str]:
    return {
        entry.name
        for entry in LAMBDA_DIR.iterdir()
        if entry.is_dir() and entry.name.startswith(LAMBDA_PREFIX) and (entry / "lambda_function.py").is_file()
    }


def test_every_lambda_appears_in_the_mapping_table():
    """A Lambda absent from the table has no tier-2 gate at all."""
    missing = sorted(_lambda_dirs_with_handlers() - set(_parse_rows()))
    assert not missing, (
        "Lambda directories absent from .kiro/steering/test-mapping.md:\n  "
        + "\n  ".join(missing)
        + "\nA change to these resolves the tier-2 gate to nothing. Add a row "
        "naming the test directory, or the 'no tests' marker if none exists."
    )


def test_no_row_names_a_missing_lambda_directory():
    """A row pointing at a deleted Lambda is as misleading as a missing row."""
    stale = sorted(set(_parse_rows()) - _lambda_dirs_with_handlers())
    assert not stale, (
        "Rows in test-mapping.md naming Lambda directories that do not exist "
        "(or have no lambda_function.py):\n  " + "\n  ".join(stale)
    )


def test_every_mapped_test_directory_exists():
    dangling = sorted(
        f"{lambda_name} -> tests/unit/lambdas/{test_dir}/"
        for lambda_name, test_dir in _parse_rows().items()
        if test_dir is not None and not (UNIT_LAMBDAS_DIR / test_dir).is_dir()
    )
    assert not dangling, "Mapped test directories that do not exist:\n  " + "\n  ".join(dangling)


def test_no_tests_marker_agrees_with_the_filesystem():
    """The marker must be accurate in both directions.

    A row mapped to a directory holding no test files makes the gate report
    success having run nothing — the failure this file exists to prevent. A row
    still marked 'no tests' after tests are added understates real coverage.
    """
    claims_tests_but_empty: list[str] = []
    claims_none_but_has_tests: list[str] = []

    for lambda_name, test_dir in _parse_rows().items():
        if test_dir is None:
            short = lambda_name.removeprefix(LAMBDA_PREFIX)
            candidate = UNIT_LAMBDAS_DIR / short
            if candidate.is_dir() and any(candidate.glob("test_*.py")):
                claims_none_but_has_tests.append(f"{lambda_name} ({candidate})")
            continue
        resolved = UNIT_LAMBDAS_DIR / test_dir
        if resolved.is_dir() and not any(resolved.glob("test_*.py")):
            claims_tests_but_empty.append(f"{lambda_name} -> {resolved}")

    assert not claims_tests_but_empty, (
        "Mapped test directories containing no test_*.py — `make test-scope` on "
        "these runs zero tests and still exits 0:\n  "
        + "\n  ".join(claims_tests_but_empty)
        + "\nEither add tests or mark the row 'no tests'."
    )
    assert not claims_none_but_has_tests, (
        "Rows marked 'no tests' that now have tests — update the row to name the "
        "directory so the tier-2 gate can use it:\n  " + "\n  ".join(claims_none_but_has_tests)
    )
