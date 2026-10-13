# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /tests/{code_version_id} — payload shape.

Covers ``handlers.pipelines_artifacts.get_tests_by_version``, whose response
body is::

    {
        code_version_id: string,
        test_count: int,
        latest_test: TestEntry | null,
        all_tests: TestEntry[],
    }

This is the endpoint's **first** payload-shape coverage. A codegraph
``explore`` on ``get_tests_by_version`` reported "no covering tests found", and
a search of ``tests/`` for ``latest_test`` / ``all_tests`` returned zero hits —
which is why a key the frontend reads could be absent from the response for as
long as it was. Scope is deliberately the shape only, not the whole handler;
the forwarding behaviour itself is pinned in
``tests/unit/lambdas/dashboard_api/test_tests_by_version_error_details.py``.

**Why ``error_details`` is required rather than optional.** The handler forwards
an explicit key list from the ``agentic_data_ingestion_test_results`` row, and
that list omitted ``error_details``. The frontend reads exactly that key —
``use-code.ts:50`` assigns ``test?.error_details`` onto the ``TestResult`` it
returns, and ``TestResultsViewer`` renders it in its Error Details block
(``:346``, and ``:399`` in ``TestResultsDisplay``). The producer writes it on
every non-passing run
(``test_code/lambda_function.py:2990-2993``: ``None`` when ``test_status`` is
``passed``, otherwise ``f"Failed checks: {…}"``), so the data existed and only
the read path was missing: the UI's error detail was empty on every failure.

**Why the forbidden-key set exists.** The explicit key list is an allowlist, not
an oversight — ``LGR-69ca78b-5`` and ``LGR-f077f1b-1`` (both template /
ratified) are the reason ``approval_required`` was *removed* from this module's
``get_pipeline_code_versions`` rather than re-sourced: a field with no consumer
is dead weight. Three attributes present on the stored row are therefore
withheld, and the absence is asserted here so that widening the list stays a
deliberate act rather than a spread:

* ``execution_verified`` — p40 R6's statement of whether the transform actually
  ran. Its producer comment anticipated this list gaining it, but there is **no**
  frontend reader: zero hits for ``execution_verified`` or ``verification_note``
  anywhere under ``dashboard-react/``. The three-state check rendering in
  ``approvals/ApprovalArtifact.tsx`` reads the per-check ``verified`` /
  ``not_verified_reason`` keys off the *approval payload*, a different surface
  with a different producer, and never reads this row. Forwarding it would
  publish a field nothing renders.
* ``test_report_s3_uri`` — no reader; the report is fetched via
  ``test_output_s3_uri`` (``use-code.ts:36``).
* ``fixed_code_version_id`` — no reader.

Expectations are transcribed from the frontend and from the producer, NOT read
back out of the handler: a test whose oracle is the thing under test cannot
detect that thing drifting (``LGR-28b4d75-5``, template / ratified).
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# ---------------------------------------------------------------------------
# Environment variables required at import time
# ---------------------------------------------------------------------------
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CODE_VERSION_ID = "cv-tests-001"

TOP_LEVEL_KEYS = {"code_version_id", "test_count", "latest_test", "all_tests"}

#: Every key a ``TestEntry`` must publish. ``error_details`` is in this set, not
#: in an "optional" one: the frontend's read is unconditional, so an entry
#: without the key silently yields ``undefined`` and the Error Details block
#: never renders — the exact failure this test exists to prevent recurring.
TEST_ENTRY_KEYS = {
    "test_execution_id",
    "code_version_id",
    "test_timestamp",
    "test_status",
    "test_results",
    "test_output_s3_uri",
    "validation_status",
    "error_details",
    "agent_session_id",
    "retry_count",
}

#: Keys this endpoint must NOT publish on a test entry. See the module docstring:
#: none of the three has a frontend reader (``LGR-f077f1b-1``, template /
#: ratified). Widening the response is allowed — flipping a key from this set to
#: ``TEST_ENTRY_KEYS`` is the deliberate act — but it must not happen by
#: accident.
TEST_ENTRY_FORBIDDEN_KEYS = {
    "execution_verified",
    "test_report_s3_uri",
    "fixed_code_version_id",
}

#: The producer's failure text, transcribed from
#: ``test_code/lambda_function.py:2990-2993`` rather than imported. Shape only —
#: the check names are the producer's business.
FAILED_DETAILS = "Failed checks: code_interpreter_execution, schema_compliance"


def _make_stored_item(**overrides) -> dict:
    """A test-results table row as the PK query returns it.

    Carries every attribute the producer writes, including the three the
    endpoint withholds — a row without them could not tell "withheld" apart
    from "never stored", which is the only distinction
    ``test_entry_omits_the_forbidden_keys`` is making.
    """
    item = {
        "PK": f"TEST#{CODE_VERSION_ID}",
        "SK": "RESULT#2026-08-14T10:00:00Z",
        "test_execution_id": "te-001",
        "code_version_id": CODE_VERSION_ID,
        "test_timestamp": "2026-08-14T10:00:00Z",
        "test_status": "failed",
        "test_results": {
            "checks_run": 4,
            "checks_passed": 2,
            "checks_failed": 2,
            "tests_run": 4,
            "tests_passed": 2,
            "tests_failed": 2,
            "execution_time_ms": 1234,
        },
        "test_output_s3_uri": "s3://bucket/tests/te-001.json",
        "test_report_s3_uri": "s3://bucket/tests/te-001.md",
        "error_details": FAILED_DETAILS,
        "agent_session_id": "sess-001",
        "validation_status": "failed",
        "execution_verified": False,
        "retry_count": 0,
        "fixed_code_version_id": None,
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# Fixture — patch the test-results table
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_boto(monkeypatch):
    from handlers import common as _common

    mock_table = MagicMock()
    monkeypatch.setattr(_common, "test_results_table", mock_table)
    _patch_boto.test_results_table = mock_table
    yield


def _set_query_items(items: list) -> None:
    _patch_boto.test_results_table.query.return_value = {"Items": items}


def _call(code_version_id: str = CODE_VERSION_ID) -> tuple[int, dict]:
    # Imported through handlers.pipelines, which re-exports the name for
    # router/test compatibility (P21 Task 7.2 module decomposition).
    from handlers.pipelines import get_tests_by_version

    response = get_tests_by_version(code_version_id)
    return response["statusCode"], json.loads(response["body"])


# ===========================================================================
# Contract tests
# ===========================================================================


class TestTestsByVersionTopLevelShape:
    def test_response_has_exactly_the_top_level_keys(self):
        _set_query_items([_make_stored_item()])
        status, body = _call()

        assert status == 200
        assert set(body.keys()) == TOP_LEVEL_KEYS

    def test_test_count_matches_all_tests_length(self):
        _set_query_items([_make_stored_item(), _make_stored_item(test_execution_id="te-002")])
        _, body = _call()

        assert body["test_count"] == len(body["all_tests"]) == 2

    def test_latest_test_is_the_first_entry(self):
        _set_query_items([_make_stored_item(), _make_stored_item(test_execution_id="te-002")])
        _, body = _call()

        assert body["latest_test"] == body["all_tests"][0]

    def test_no_results_is_a_404(self):
        """An absent row is 404, which the frontend maps to a pending result.

        ``use-code.ts:56-63`` catches ``status === 404`` and returns
        ``test_status: 'pending'``. A 200 with an empty list would render as a
        completed run with no tests, so the status code is part of the contract.
        """
        _set_query_items([])
        status, body = _call()

        assert status == 404
        assert "error" in body


class TestTestEntryShape:
    def test_each_entry_has_exactly_the_test_entry_keys(self):
        _set_query_items([_make_stored_item()])
        _, body = _call()

        for entry in body["all_tests"]:
            assert set(entry.keys()) == TEST_ENTRY_KEYS

    def test_entry_publishes_error_details(self):
        """The key the frontend reads is present and carries the stored text."""
        _set_query_items([_make_stored_item()])
        _, body = _call()

        entry = body["all_tests"][0]
        assert "error_details" in entry
        assert entry["error_details"] == FAILED_DETAILS

    def test_latest_test_publishes_error_details(self):
        """``latest_test`` is the object the frontend actually reads.

        ``use-code.ts:24`` takes ``body.latest_test ?? body``, so a key present
        only on ``all_tests`` entries would still leave the UI empty.
        """
        _set_query_items([_make_stored_item()])
        _, body = _call()

        assert body["latest_test"]["error_details"] == FAILED_DETAILS

    def test_entry_omits_the_forbidden_keys(self):
        _set_query_items([_make_stored_item()])
        _, body = _call()

        for entry in body["all_tests"]:
            present = TEST_ENTRY_FORBIDDEN_KEYS & set(entry.keys())
            assert not present, f"Keys this endpoint must not publish: {present}"

    def test_error_details_is_string_or_null(self):
        """A passing run stores ``None``; the response must carry it as ``null``.

        The frontend coalesces with ``?? undefined`` and both renderers gate on
        truthiness, so ``null`` is the honest "no error" value rather than a
        missing key or an empty string.
        """
        _set_query_items([_make_stored_item(test_status="passed", error_details=None)])
        _, body = _call()

        entry = body["all_tests"][0]
        assert entry["error_details"] is None

    def test_test_results_is_a_dict(self):
        _set_query_items([_make_stored_item()])
        _, body = _call()

        for entry in body["all_tests"]:
            assert isinstance(entry["test_results"], dict)

    def test_code_version_id_echoes_the_request(self):
        """The entry's ``code_version_id`` is the requested one, not the row's.

        The handler substitutes the argument, so a row whose stored attribute
        disagrees must not surface the stored value.
        """
        _set_query_items([_make_stored_item(code_version_id="cv-stale-999")])
        _, body = _call()

        assert body["code_version_id"] == CODE_VERSION_ID
        assert body["all_tests"][0]["code_version_id"] == CODE_VERSION_ID
