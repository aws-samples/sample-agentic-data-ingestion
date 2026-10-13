# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests: no client-supplied value can become the approver.

The unit tests beside this file pin specific shapes. This pins the universal
claim R2.2 actually makes: *for any* body a client can post, the identity
persisted on the approvals row and attributed on the audit row is the one the
authorizer supplied — never the one the body did.

The generators put the same forged string in every client-controlled slot at
once (``approved_by``, ``rejected_by``, ``user``, ``actor``, and the headers and
query string for good measure), and draw it from a namespace disjoint from the
server-supplied principal's. So any appearance of the forged value in the
attributed identity means a client-controlled field reached it.

The last class is p50's addition (design CP3): adding an email *beside* the
attributed identity is the change most likely to quietly turn into adding it
*instead*, so the ratified sub-first ruling is asserted over generated claim
shapes rather than one example.

Validates: Requirements R2.1, R2.2, R2.3; p50 R3.1, R3.3
"""

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup (mirrors test_approval_actor_identity.py)
# ---------------------------------------------------------------------------
DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
HANDLERS_DIR = os.path.join(DASHBOARD_API_DIR, "handlers")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(DASHBOARD_API_DIR))
sys.path.insert(0, os.path.abspath(HANDLERS_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")
os.environ.setdefault("AUDIT_TABLE", "test-audit")

mock_approvals_table = MagicMock()
mock_task_tokens_table = MagicMock()

_table_map = {
    "test-approvals": mock_approvals_table,
    "test-task-tokens": mock_task_tokens_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    mock_client.return_value = MagicMock()
    from handlers import approvals as approvals_mod

APPROVAL_ID = "approval-identity-prop-001"

mock_state_table = MagicMock()

# Disjoint namespaces. A forged value can never equal a server value, so an
# assertion on "the result is the server value" is also an assertion on
# "the result is not the forged one".
_FORGED = st.sampled_from(
    [
        "attacker@example.invalid",
        "dashboard-user",
        "admin",
        "root",
        "",
        "  ",
        "unknown",
        "sub",
        "00000000-0000-0000-0000-000000000000",
    ]
)
_SERVER_SUB = st.sampled_from(
    [
        "6d1f8c02-9a3e-4b7d-8f10-2c5e9b4a7d31",
        "b7e41a55-0c2d-4e19-9a83-5f60d1c8e402",
    ]
)
_ACTIONS = st.sampled_from(["approve", "reject"])


@pytest.fixture(autouse=True)
def _wire_tables():
    approvals_mod.approvals_table = mock_approvals_table
    approvals_mod.task_tokens_table = mock_task_tokens_table
    approvals_mod.state_table = mock_state_table
    yield


def _forged_body(forged: str) -> str:
    """Every client-controlled identity key, all set to the forged value."""
    return json.dumps(
        {
            "approved_by": forged,
            "rejected_by": forged,
            "user": forged,
            "actor": forged,
            "comments": forged,
            "reason": forged,
        }
    )


def _forged_event(request_context: Any, forged: str) -> dict[str, Any]:
    return {
        "body": _forged_body(forged),
        "headers": {"X-Actor": forged, "Authorization": f"Bearer {forged}"},
        "queryStringParameters": {"user": forged, "approved_by": forged},
        "pathParameters": {"user": forged},
        "requestContext": request_context,
    }


def _run(action: str, body: str, event: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Decide, returning (persisted identity, audit kwargs).

    Fresh mocks per example: hypothesis runs this many times in one process and a
    leaked ``call_args_list`` would make one example's write answer for another's.
    """
    mock_approvals_table.reset_mock()
    mock_state_table.reset_mock()
    mock_approvals_table.get_item.return_value = {
        "Item": {
            "approval_id": APPROVAL_ID,
            "status": "pending",
            "has_task_token": False,
            "pipeline_id": "pipe-001",
            "run_id": "run-001",
            "approval_type": "first_run",
            "created_at": "2025-01-15T12:00:00Z",
        }
    }
    attribute = "approved_by" if action == "approve" else "rejected_by"
    fn = approvals_mod.approve_request if action == "approve" else approvals_mod.reject_request
    with patch.object(approvals_mod, "write_audit_entry") as mock_audit:
        response = fn(APPROVAL_ID, body, event=event)
    assert response["statusCode"] == 200, response

    calls = [c.kwargs for c in mock_approvals_table.update_item.call_args_list]
    matching = [c for c in calls if f":{attribute}" in c.get("ExpressionAttributeValues", {})]
    assert matching, f"no approvals update wrote :{attribute}"
    persisted = matching[-1]["ExpressionAttributeValues"][f":{attribute}"]

    mock_audit.assert_called_once()
    return persisted, mock_audit.call_args[1]


# ---------------------------------------------------------------------------
# Property A5.1 — a resolvable principal always wins over the body
# ---------------------------------------------------------------------------


class TestAuthenticatedPrincipalAlwaysWins:
    """**Validates: Requirements R2.1, R2.2**"""

    @settings(max_examples=60, deadline=None)
    @given(action=_ACTIONS, forged=_FORGED, server_sub=_SERVER_SUB, with_email=st.booleans())
    def test_claims_sub_is_the_attributed_identity_for_any_body(self, action, forged, server_sub, with_email):
        claims: dict[str, Any] = {"sub": server_sub, "cognito:username": server_sub}
        if with_email:
            claims["email"] = "reviewer@example.com"
        event = _forged_event({"authorizer": {"claims": claims}, "identity": {"sourceIp": "203.0.113.7"}}, forged)

        persisted, audit_kwargs = _run(action, _forged_body(forged), event)

        assert persisted == server_sub
        assert audit_kwargs["actor"] == server_sub
        # Same resolution feeds both writes, so they cannot disagree.
        assert audit_kwargs["actor"] == persisted

    @settings(max_examples=40, deadline=None)
    @given(action=_ACTIONS, forged=_FORGED, server=_SERVER_SUB)
    def test_iam_caller_fallback_also_wins_over_the_body(self, action, forged, server):
        """R1.2's retained fallback must not be bypassable from the body either."""
        event = _forged_event({"identity": {"caller": server, "sourceIp": "10.0.0.1"}}, forged)

        persisted, audit_kwargs = _run(action, _forged_body(forged), event)

        assert persisted == server
        assert audit_kwargs["actor"] == server


# ---------------------------------------------------------------------------
# Property A5.2 — with nothing to authenticate, the answer is "unknown"
# ---------------------------------------------------------------------------


class TestUnresolvablePrincipalIsUnknown:
    """**Validates: Requirements R2.3**"""

    @settings(max_examples=60, deadline=None)
    @given(
        action=_ACTIONS,
        forged=_FORGED,
        request_context=st.sampled_from(
            [
                {},
                {"identity": {}},
                {"identity": {"sourceIp": "203.0.113.7"}},
                {"authorizer": {}},
                {"authorizer": {"claims": {}}, "identity": {}},
                {"authorizer": {"claims": {"sub": ""}}, "identity": {"caller": ""}},
                {"authorizer": None, "identity": None},
                {"authorizer": "not-a-dict", "identity": "not-a-dict"},
            ]
        ),
    )
    def test_no_principal_persists_unknown_never_the_body(self, action, forged, request_context):
        persisted, audit_kwargs = _run(action, _forged_body(forged), _forged_event(request_context, forged))

        assert persisted == "unknown"
        assert audit_kwargs["actor"] == "unknown"
        # The specific string R2.3 removes, whatever the client asked for.
        assert persisted != "dashboard-user"


# ---------------------------------------------------------------------------
# Property A5.3 — a body identity is only ever a claim
# ---------------------------------------------------------------------------


class TestBodyIdentityIsOnlyEverAClaim:
    """**Validates: Requirements R2.2**"""

    @settings(max_examples=60, deadline=None)
    @given(action=_ACTIONS, forged=_FORGED, server_sub=_SERVER_SUB)
    def test_forged_value_appears_only_under_claimed_by(self, action, forged, server_sub):
        event = _forged_event({"authorizer": {"claims": {"sub": server_sub, "email": "r@example.com"}}}, forged)

        persisted, audit_kwargs = _run(action, _forged_body(forged), event)
        details = audit_kwargs["details"]

        assert persisted == server_sub
        assert audit_kwargs["actor"] == server_sub
        assert details["actor_email"] == "r@example.com"

        # The forged string may appear as `claimed_by` — that is the diagnostic —
        # and nowhere else among the identity-bearing values.
        identity_values = {k: v for k, v in details.items() if k != "claimed_by"}
        assert forged not in identity_values.values()
        if forged:
            assert details["claimed_by"] == forged
        else:
            # An empty claim is not a claim: it is omitted, not written blank.
            assert "claimed_by" not in details

    @settings(max_examples=40, deadline=None)
    @given(action=_ACTIONS, server_sub=_SERVER_SUB)
    def test_a_body_with_no_identity_keys_yields_no_claimed_by(self, action, server_sub):
        event = {"requestContext": {"authorizer": {"claims": {"sub": server_sub}}}}
        body = json.dumps({"comments": "fine", "reason": "fine"})

        persisted, audit_kwargs = _run(action, body, event)

        assert persisted == server_sub
        assert "claimed_by" not in audit_kwargs["details"]


# ---------------------------------------------------------------------------
# Property A5.4 — the two endpoints cannot drift apart
# ---------------------------------------------------------------------------


class TestBothEndpointsResolveIdentically:
    """**Validates: Requirements R2.1**"""

    @settings(max_examples=40, deadline=None)
    @given(
        forged=_FORGED,
        request_context=st.sampled_from(
            [
                {"authorizer": {"claims": {"sub": "6d1f8c02-9a3e-4b7d-8f10-2c5e9b4a7d31"}}},
                {"authorizer": {"claims": {"email": "only-email@example.com"}}},
                {"identity": {"caller": "arn:aws:iam::123456789012:user/u"}},
                {"identity": {"cognitoIdentityId": "us-east-1:abc"}},
                {},
            ]
        ),
    )
    def test_approve_and_reject_agree_on_identity_and_details(self, forged, request_context):
        event = _forged_event(request_context, forged)
        body = _forged_body(forged)

        approved_by, approve_audit = _run("approve", body, event)
        rejected_by, reject_audit = _run("reject", body, event)

        assert approved_by == rejected_by
        assert approve_audit["actor"] == reject_audit["actor"]
        assert approve_audit["details"] == reject_audit["details"]


# ---------------------------------------------------------------------------
# Property CP3 (p50) — the email is additional, never the attributed identity
# ---------------------------------------------------------------------------

# ``None`` means the ``email`` claim is absent; ``""`` means present but blank.
# Both must leave the row with no email attribute at all, rather than a blank one.
_EMAIL_CLAIMS = st.sampled_from([None, "", "reviewer@example.com", "Second.Reviewer@example.com"])


def _persisted_attribute(attribute: str) -> str | None:
    """Value written to ``attribute``, or ``None`` if it was never written."""
    for call in reversed([c.kwargs for c in mock_approvals_table.update_item.call_args_list]):
        values = call.get("ExpressionAttributeValues", {})
        if f":{attribute}" in values:
            return values[f":{attribute}"]
    return None


class TestApprovedByStaysTheStableIdentifier:
    """p50 design CP3: for every approve and reject path, the persisted decision
    identity equals the resolver's ``actor`` and is never replaced by the email.

    ``LGR-c58f276-2`` rejected preferring ``email`` for ``approved_by`` because an
    immutable audit row wants a stable identifier over a mutable one. p50 adds the
    email *beside* it, which is the change that could quietly undo that ruling —
    so the ruling is asserted over generated inputs rather than one shape.

    **Validates: Requirements R3.1, R3.3**
    """

    @settings(max_examples=60, deadline=None)
    @given(action=_ACTIONS, forged=_FORGED, server_sub=_SERVER_SUB, email=_EMAIL_CLAIMS)
    def test_email_never_displaces_the_sub_on_either_path(self, action, forged, server_sub, email):
        claims: dict[str, Any] = {"sub": server_sub, "cognito:username": server_sub}
        if email is not None:
            claims["email"] = email
        event = _forged_event({"authorizer": {"claims": claims}, "identity": {"sourceIp": "203.0.113.7"}}, forged)

        persisted, audit_kwargs = _run(action, _forged_body(forged), event)
        email_attribute = "approved_by_email" if action == "approve" else "rejected_by_email"

        # The attributed identity is the resolver's actor, whatever the claims hold.
        assert persisted == server_sub
        assert audit_kwargs["actor"] == server_sub
        assert persisted != email

        if email:
            # Additional, and agreeing with the audit detail by construction.
            assert _persisted_attribute(email_attribute) == email
            assert audit_kwargs["details"]["actor_email"] == email
        else:
            # Absent or blank: omitted, never written as an empty string.
            assert _persisted_attribute(email_attribute) is None
            assert "actor_email" not in audit_kwargs["details"]

    @settings(max_examples=40, deadline=None)
    @given(forged=_FORGED, server_sub=_SERVER_SUB, email=_EMAIL_CLAIMS)
    def test_the_two_paths_write_the_same_email_or_neither(self, forged, server_sub, email):
        """A one-sided landing is the failure mode `approve`/`reject` symmetry
        exists to prevent, and R2.2 says "after approve and after reject"."""
        claims: dict[str, Any] = {"sub": server_sub}
        if email is not None:
            claims["email"] = email
        event = _forged_event({"authorizer": {"claims": claims}}, forged)
        body = _forged_body(forged)

        _run("approve", body, event)
        approve_email = _persisted_attribute("approved_by_email")
        _run("reject", body, event)
        reject_email = _persisted_attribute("rejected_by_email")

        assert approve_email == reject_email == (email or None)
