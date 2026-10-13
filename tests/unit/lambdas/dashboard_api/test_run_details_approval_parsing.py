# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for approval details parsing in get_run_details.

Validates Requirements 8.1, 8.2, 8.3, 8.4:
- approval_data JSON is parsed into a `details` object on each approval
- Decision fields (gate_name, decided_by, decided_at, comment) are mapped
- Evidence and questions from the agent are available in `details`
- Missing or unparseable approval_data yields empty details (never raises)
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required by handlers/common.py at import time
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("AWS_REGION", "us-west-2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RUN_ID = "run-approval-parse-001"
PIPELINE_ID = "pipe-001"

# Minimal state item for run_id to pass the 404 check
STATE_ITEM = {
    "run_id": RUN_ID,
    "step_name": "schema_extract",
    "pipeline_id": PIPELINE_ID,
    "original_pipeline_id": PIPELINE_ID,
    "execution_id": "exec-001",
    "json_s3_uri": "s3://bucket/data.json",
    "status": "success",
    "timestamp": "2025-01-15T12:00:00+00:00",
    "step_sequence": 1,
}


def _parse_response(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    """Mock DynamoDB tables and disable expensive parallel log fetching."""
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    mock_state = MagicMock()
    mock_approvals = MagicMock()
    mock_executions = MagicMock()
    mock_pipelines = MagicMock()
    mock_dynamodb = MagicMock()

    # State table returns our minimal state item
    mock_state.query.return_value = {"Items": [STATE_ITEM]}

    # Executions table — no items (skip running-step injection)
    mock_executions.query.return_value = {"Items": []}
    mock_executions.get_item.return_value = {}

    # Pipelines table — skip pipeline name lookup
    mock_pipelines.get_item.return_value = {}

    # DynamoDB resource — batch_get_item for cost enrichment
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    # Patch on both the common module and the runs module
    for mod in (_common, _runs):
        monkeypatch.setattr(mod, "state_table", mock_state)
        monkeypatch.setattr(mod, "approvals_table", mock_approvals)
        monkeypatch.setattr(mod, "executions_table", mock_executions)
        monkeypatch.setattr(mod, "pipelines_table", mock_pipelines)
        monkeypatch.setattr(mod, "dynamodb", mock_dynamodb)

    # Disable parallel log fetching (tested elsewhere)
    monkeypatch.setattr(_runs, "get_step_logs", lambda *a, **kw: [])

    _patch_tables.approvals = mock_approvals
    yield


def _set_approvals(items):
    """Configure the approvals table query return value (run_id GSI, P28 R17.1)."""
    _patch_tables.approvals.query.return_value = {"Items": items}


# ---------------------------------------------------------------------------
# Import handler under test
# ---------------------------------------------------------------------------
from handlers.pipelines_runs import get_run_details

# ===========================================================================
# Tests — Req 8.1, 8.2: Parse approval_data into details with evidence/questions
# ===========================================================================


class TestApprovalDetailsParsing:
    """Verify approval_data is parsed into a details dict."""

    def test_parsed_details_includes_evidence_and_questions(self):
        """Req 8.1/8.2: evidence and questions from agent are in details."""
        evidence = {
            "customer_id": {
                "sample_values": ["C001", "C002"],
                "reasoning": "Direct match",
                "memory_matches": [],
                "confidence_before": 0.6,
                "confidence_after": 0.95,
            }
        }
        questions = [
            {
                "field": "address_line_2",
                "question": "Is address_line_2 mapped to secondary_address?",
                "options": ["secondary_address", "notes"],
                "sample_values": ["Apt 4B"],
                "current_confidence": 0.4,
            }
        ]
        approval_data = json.dumps(
            {
                "source_schema": {"customer_id": "string"},
                "target_schema": {"cust_id": "string"},
                "evidence": evidence,
                "questions": questions,
            }
        )

        _set_approvals(
            [
                {
                    "approval_id": "appr-001",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "approved",
                    "approved_by": "user@example.com",
                    "approved_at": "2025-01-15T13:00:00+00:00",
                    "comments": "LGTM",
                    "approval_data": approval_data,
                }
            ]
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        approvals = body["approvals"]
        assert len(approvals) == 1
        details = approvals[0]["details"]
        assert details["evidence"] == evidence
        assert details["questions"] == questions

    def test_approval_data_as_dict_not_string(self):
        """When approval_data is already a dict (not JSON string), use it directly."""
        inner = {"source_schema": {"a": "int"}, "evidence": {"a": {"reasoning": "direct"}}}
        _set_approvals(
            [
                {
                    "approval_id": "appr-002",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "pending",
                    "approval_data": inner,  # Already a dict
                }
            ]
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert body["approvals"][0]["details"] == inner


# ===========================================================================
# Tests — Req 8.3: Decision field mapping
# ===========================================================================


class TestDecisionFieldMapping:
    """Verify gate_name, decided_by, decided_at, comment are mapped."""

    def test_approved_decision_fields(self):
        """Approved approval maps decided_by/decided_at/comment correctly."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-003",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "approved",
                    "approved_by": "alice@example.com",
                    "approved_at": "2025-01-15T14:00:00Z",
                    "comments": "Looks good",
                    "approval_data": json.dumps({"source_schema": {}}),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))
        a = body["approvals"][0]

        assert a["gate_name"] == "schema_mapping"
        assert a["decided_by"] == "alice@example.com"
        assert a["decided_at"] == "2025-01-15T14:00:00Z"
        assert a["comment"] == "Looks good"

    def test_rejected_decision_fields(self):
        """Rejected approval maps rejected_by/rejected_at/rejection_reason."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-004",
                    "run_id": RUN_ID,
                    "approval_type": "code_generation",
                    "status": "rejected",
                    "rejected_by": "bob@example.com",
                    "rejected_at": "2025-01-15T15:00:00Z",
                    "rejection_reason": "Code quality issues",
                    "approval_data": json.dumps({"code_version_id": "v1"}),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))
        a = body["approvals"][0]

        assert a["gate_name"] == "code_generation"
        assert a["decided_by"] == "bob@example.com"
        assert a["decided_at"] == "2025-01-15T15:00:00Z"
        assert a["comment"] == "Code quality issues"

    def test_pending_has_none_decided_fields(self):
        """Pending approvals have None for decided_by/decided_at."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-005",
                    "run_id": RUN_ID,
                    "approval_type": "infrastructure",
                    "status": "pending",
                    "approval_data": json.dumps({}),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))
        a = body["approvals"][0]

        assert a["gate_name"] == "infrastructure"
        assert a["decided_by"] is None
        assert a["decided_at"] is None
        assert a["comment"] is None


# ===========================================================================
# Tests — p50 R2.2 / C3: the actor email is projected beside `decided_by`
# ===========================================================================


class TestDecidedByEmailProjection:
    """``decided_by_email`` is synthesised from the two arm-specific attributes.

    Task 3.1 writes ``approved_by_email`` on the approve branch and
    ``rejected_by_email`` on the reject branch, **omitting either when the email
    is empty** rather than writing it blank — ``LGR-c58f276-2``'s convention that
    "an audit row never asserts an identity it does not have". The persisted names
    stay arm-specific because that record makes ``decided_by`` a *read-path*
    synthesis; the collapse to one field belongs here, not in storage.

    Four states are pinned below because task 4.1's UI contract depends on all
    four: approve-with-email, reject-with-email, decided-without-email (the state
    of **every** row written before 3.1) and still-pending.
    """

    SUB = "583123b0-60b1-7056-51a3-bd7ba804c325"

    def test_approve_arm_projects_approved_by_email(self):
        """R2.2: an approved row's ``approved_by_email`` reaches the wire."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-010",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "approved",
                    "approved_by": self.SUB,
                    "approved_by_email": "alice@example.com",
                    "approved_at": "2025-01-15T14:00:00Z",
                    "comments": "Looks good",
                    "approval_data": json.dumps({}),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))
        a = body["approvals"][0]

        assert a["decided_by_email"] == "alice@example.com"
        # R3.3: additive. ``decided_by`` still carries the stable ``sub``.
        assert a["decided_by"] == self.SUB

    def test_reject_arm_projects_rejected_by_email(self):
        """R2.2: the reject arm's differently-named attribute lands on the same key."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-011",
                    "run_id": RUN_ID,
                    "approval_type": "code_generation",
                    "status": "rejected",
                    "rejected_by": self.SUB,
                    "rejected_by_email": "bob@example.com",
                    "rejected_at": "2025-01-15T15:00:00Z",
                    "rejection_reason": "Code quality issues",
                    "approval_data": json.dumps({}),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))
        a = body["approvals"][0]

        assert a["decided_by_email"] == "bob@example.com"
        assert a["decided_by"] == self.SUB

    def test_decided_without_email_projects_falsy(self):
        """R2.3 / CP5: a pre-3.1 row projects falsy so the UI falls back to the ``sub``.

        This is the dominant live state, not an edge case — no approvals row
        written before task 3.1 carries an email at all. The key must still be
        present and falsy rather than absent-or-blank, because task 4.1's
        ``decided_by_email || decided_by`` fall-through is what keeps the surface
        from rendering blank.
        """
        _set_approvals(
            [
                {
                    "approval_id": "appr-012",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "approved",
                    "approved_by": self.SUB,
                    "approved_at": "2025-01-15T14:00:00Z",
                    "comments": "",
                    "approval_data": json.dumps({}),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))
        a = body["approvals"][0]

        assert "decided_by_email" in a
        assert not a["decided_by_email"]
        # The identity the UI falls back to is still there.
        assert a["decided_by"] == self.SUB

    def test_pending_has_none_decided_by_email(self):
        """A pending row has decided nothing, so it asserts no email either."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-013",
                    "run_id": RUN_ID,
                    "approval_type": "infrastructure",
                    "status": "pending",
                    "approval_data": json.dumps({}),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))
        a = body["approvals"][0]

        assert a["decided_by_email"] is None
        assert a["decided_by"] is None

    def test_approve_arm_wins_when_a_row_carries_both(self):
        """The ``or`` resolves left-to-right, matching ``decided_by``'s own order.

        A row carrying both is not a shape any producer writes — 3.1 appends on
        exactly one branch per request — but pinning the order keeps this
        projection's tie-break identical to the ``decided_by`` line above it, so
        the two fields can never disagree about which arm they describe.
        """
        _set_approvals(
            [
                {
                    "approval_id": "appr-014",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "approved",
                    "approved_by": "approver-sub",
                    "approved_by_email": "alice@example.com",
                    "rejected_by": "rejecter-sub",
                    "rejected_by_email": "bob@example.com",
                    "approval_data": json.dumps({}),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))
        a = body["approvals"][0]

        assert a["decided_by_email"] == "alice@example.com"
        assert a["decided_by"] == "approver-sub"

    def test_blank_email_falls_through_to_the_other_arm(self):
        """Falsiness, not presence, is what the projection keys on.

        3.1 omits the attribute when empty, so a blank one should not occur — but
        if a row ever carries one, an empty string must not shadow a real email on
        the other arm. Same reason ``decided_by`` is an ``or`` and not a
        ``"approved_by" in row`` test.
        """
        _set_approvals(
            [
                {
                    "approval_id": "appr-015",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "rejected",
                    "approved_by_email": "",
                    "rejected_by": self.SUB,
                    "rejected_by_email": "bob@example.com",
                    "approval_data": json.dumps({}),
                }
            ]
        )

        _, body = _parse_response(get_run_details(RUN_ID))

        assert body["approvals"][0]["decided_by_email"] == "bob@example.com"


# ===========================================================================
# Tests — Req 8.4: Missing/unparseable approval_data → empty details
# ===========================================================================


class TestUnparseableApprovalData:
    """Verify that bad approval_data never raises and yields empty details."""

    def test_missing_approval_data_yields_empty_details(self):
        """No approval_data key → details is {}."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-006",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "pending",
                }
            ]
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert body["approvals"][0]["details"] == {}

    def test_invalid_json_approval_data_yields_empty_details(self):
        """Unparseable JSON string → details is {}."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-007",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "pending",
                    "approval_data": "{{not valid json!!!",
                }
            ]
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert body["approvals"][0]["details"] == {}

    def test_none_approval_data_yields_empty_details(self):
        """approval_data is None → details is {}."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-008",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "pending",
                    "approval_data": None,
                }
            ]
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert body["approvals"][0]["details"] == {}

    def test_empty_string_approval_data_yields_empty_details(self):
        """approval_data is '' → details is {}."""
        _set_approvals(
            [
                {
                    "approval_id": "appr-009",
                    "run_id": RUN_ID,
                    "approval_type": "schema_mapping",
                    "status": "pending",
                    "approval_data": "",
                }
            ]
        )

        status, body = _parse_response(get_run_details(RUN_ID))

        assert status == 200
        assert body["approvals"][0]["details"] == {}
