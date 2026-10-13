# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``get_tests_by_version`` forwards ``error_details`` from the stored row.

The handler publishes a test-results row through an **explicit key list**
(``handlers/pipelines_artifacts.py``), and that list omitted ``error_details``.
The producer writes it on every non-passing run —
``test_code/lambda_function.py:2990-2993`` stores ``None`` when ``test_status``
is ``passed`` and ``f"Failed checks: {', '.join(failed_check_names)}"``
otherwise — and ``use-code.ts:50`` reads exactly that key onto the
``TestResult`` the dashboard renders. So the data existed, the reader existed,
and the response dropped it: the UI's Error Details block
(``TestResultsViewer.tsx:346``, ``:399``) was empty on every failing run.

These are the handler's first tests of any kind. A codegraph ``explore`` on
``get_tests_by_version`` reported "no covering tests found", and a search of
``tests/`` for ``latest_test`` / ``all_tests`` returned zero hits. The
expectations below are therefore derived from the producer's write and the
frontend's read, not from the handler's current output — current output is where
the defect lived (``LGR-28b4d75-5``, template / ratified, on why a test must not
use the thing under test as its own oracle).

Response *shape* is pinned separately, in
``tests/contracts/api_responses/test_tests_by_version_contract.py``. This file
is about the forwarding and the ordering the frontend depends on.
"""

import json
from unittest.mock import MagicMock

import pytest

CODE_VERSION_ID = "cv-err-001"

#: The producer's text for a failing run, transcribed from
#: ``test_code/lambda_function.py:2990-2993``.
FAILED_DETAILS = "Failed checks: code_interpreter_execution, schema_compliance"


def _row(**overrides) -> dict:
    """A test-results row as ``test_results_table.query`` returns it."""
    item = {
        "PK": f"TEST#{CODE_VERSION_ID}",
        "SK": "RESULT#2026-08-14T10:00:00Z",
        "test_execution_id": "te-001",
        "code_version_id": CODE_VERSION_ID,
        "test_timestamp": "2026-08-14T10:00:00Z",
        "test_status": "failed",
        "test_results": {"checks_run": 4, "checks_passed": 2, "checks_failed": 2},
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


@pytest.fixture
def test_results_table(monkeypatch):
    """Patch the module-level table the handler imports from ``handlers.common``.

    A ``MagicMock`` rather than moto: the handler's only AWS interaction is one
    ``query`` whose return value is the entire input to the code under test, and
    ``tests/unit/test_no_duplicate_mocking_prop.py`` forbids standing up a
    second ``moto.mock_aws()``. Patching the attribute on ``handlers.common`` is
    what the sibling contract tests for this module already do — the handler
    resolves ``test_results_table`` through a function-local
    ``from .common import …``, so the patch is seen at call time.
    """
    from handlers import common as _common

    table = MagicMock()
    monkeypatch.setattr(_common, "test_results_table", table)
    return table


def _call(table, items, code_version_id: str = CODE_VERSION_ID) -> tuple[int, dict]:
    from handlers.pipelines import get_tests_by_version

    table.query.return_value = {"Items": items}
    response = get_tests_by_version(code_version_id)
    return response["statusCode"], json.loads(response["body"])


class TestErrorDetailsForwarding:
    def test_failure_text_reaches_the_response(self, test_results_table):
        """The stored failure text is published verbatim, not summarised."""
        status, body = _call(test_results_table, [_row()])

        assert status == 200
        assert body["latest_test"]["error_details"] == FAILED_DETAILS

    def test_passing_run_forwards_none(self, test_results_table):
        """``None`` is the producer's "no error" value and survives as ``null``.

        Not ``""``: a passing row stores ``None`` explicitly, so an empty-string
        default would be a second spelling of the same absent state. Both are
        falsy in the two renderers, so this pins the value rather than the
        rendered outcome.
        """
        _, body = _call(test_results_table, [_row(test_status="passed", error_details=None)])

        assert body["latest_test"]["error_details"] is None

    def test_legacy_row_without_the_attribute_yields_none(self, test_results_table):
        """A row predating the producer's write must not raise or invent text."""
        row = _row()
        del row["error_details"]

        status, body = _call(test_results_table, [row])

        assert status == 200
        assert body["latest_test"]["error_details"] is None

    def test_every_entry_carries_its_own_text(self, test_results_table):
        """``all_tests`` forwards per-row text, not the newest row's for all.

        A retried code version accumulates several rows, and the UI can address
        any of them; collapsing them onto one message would misattribute a
        failure.
        """
        rows = [
            _row(test_execution_id="te-002", error_details="Failed checks: schema_compliance"),
            _row(test_execution_id="te-001", error_details=FAILED_DETAILS),
        ]
        _, body = _call(test_results_table, rows)

        assert [entry["error_details"] for entry in body["all_tests"]] == [
            "Failed checks: schema_compliance",
            FAILED_DETAILS,
        ]

    def test_latest_test_is_the_first_row_returned(self, test_results_table):
        """``latest_test`` is ``all_tests[0]``, which is what the frontend reads.

        ``use-code.ts:24`` takes ``body.latest_test ?? body``, so the entry the
        UI renders is this one. The query asks DynamoDB for descending SK order
        (``ScanIndexForward=False``), and the handler does not re-sort — this
        pins that it forwards the query's order rather than reversing it.
        """
        rows = [_row(test_execution_id="te-newest"), _row(test_execution_id="te-older")]
        _, body = _call(test_results_table, rows)

        assert body["latest_test"]["test_execution_id"] == "te-newest"
        assert test_results_table.query.call_args.kwargs["ScanIndexForward"] is False


class TestWithheldKeys:
    def test_execution_verified_is_not_published(self, test_results_table):
        """p40 R6's field stays withheld while it has no frontend reader.

        Verified by search, not assumed: zero hits for ``execution_verified`` or
        ``verification_note`` anywhere under ``dashboard-react/``. The
        three-state check rendering in ``approvals/ApprovalArtifact.tsx`` reads
        the per-check ``verified`` / ``not_verified_reason`` keys off the
        approval payload — a different surface, a different producer, and never
        this row. Publishing a field nothing renders is what ``LGR-f077f1b-1``
        and ``LGR-69ca78b-5`` (both template / ratified) rejected.

        Asserted against a row that HAS the attribute, which is the only shape
        that can tell "withheld" apart from "never stored".
        """
        _, body = _call(test_results_table, [_row(execution_verified=False)])

        assert "execution_verified" not in body["latest_test"]

    def test_report_uri_and_fixed_version_are_not_published(self, test_results_table):
        """Neither has a reader; the report is fetched via ``test_output_s3_uri``."""
        _, body = _call(test_results_table, [_row()])

        entry = body["latest_test"]
        assert "test_report_s3_uri" not in entry
        assert "fixed_code_version_id" not in entry

    def test_dynamo_keys_are_not_published(self, test_results_table):
        """``PK``/``SK`` are storage addressing and never leave the handler."""
        _, body = _call(test_results_table, [_row()])

        entry = body["latest_test"]
        assert "PK" not in entry
        assert "SK" not in entry


class TestAbsentResults:
    def test_no_rows_is_a_404(self, test_results_table):
        """404 is what ``use-code.ts:56-63`` maps to a pending result."""
        status, body = _call(test_results_table, [])

        assert status == 404
        assert "error" in body

    def test_query_failure_is_a_500_not_a_404(self, test_results_table):
        """An infrastructure error must not read as "no tests yet".

        Same distinction ``_read_s3_object`` in this module makes for S3 (P28
        R17.3): absent content and an unreadable store are different answers,
        and only the first one means the run has no results.
        """
        from handlers.pipelines import get_tests_by_version

        test_results_table.query.side_effect = RuntimeError("throttled")

        response = get_tests_by_version(CODE_VERSION_ID)

        assert response["statusCode"] == 500
