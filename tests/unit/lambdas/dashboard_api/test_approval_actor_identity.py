# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: the approver is the authenticated principal, not a body field.

``approve_request`` / ``reject_request`` used to read the deciding identity from
the request body with a ``"dashboard-user"`` default, on the two endpoints whose
entire purpose is attributing a governance decision. Both now derive it from the
same resolver every audit write uses (``extract_request_context``), reading
``requestContext.authorizer.claims``.

Expectations here come from ``requirements.md`` R2, not from current output:
before this change the persisted value was whatever the client sent, so current
output is the defect.

Two things these tests deliberately pin as *unchanged*:

* the persisted attribute names stay ``approved_by`` / ``rejected_by``. The
  frontend's ``decided_by`` is synthesised on the read path at
  ``handlers/pipelines_runs.py:1049``, so R2.4's wire compatibility needs no
  rename here.
* the attributed identity is the resolver's stable ``sub``, not the ``email``.
  On a pool configured ``UsernameAttributes: ["email"]`` that is an opaque UUID,
  so the ``email`` claim is recorded as a separate ``actor_email`` audit detail
  to keep the trail legible.

p50 (task 3.1) adds the last class below. ``LGR-c58f276-2``'s ``revisit_when``
named this follow-up, and the two bullets above are **unchanged by it**: the
email now also reaches the approvals row, as an *additional*
``approved_by_email`` / ``rejected_by_email`` attribute, while ``approved_by`` /
``rejected_by`` keep carrying the ``sub`` (R3.3). What the UI renders is a
separate task; this file only pins the write.

Validates: Requirements R2.1, R2.2, R2.3, R2.4, R2.5; p50 R2.2, R3.2, R3.3
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors test_approval_state_echo_canonical.py)
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

APPROVAL_ID = "approval-identity-001"

# The real shape off this deployment: `UsernameAttributes: ["email"]`, so `sub`
# and `cognito:username` are the same UUID and `email` is the only readable claim.
SUB = "6d1f8c02-9a3e-4b7d-8f10-2c5e9b4a7d31"
CLAIMS_EVENT: dict = {
    "requestContext": {
        "authorizer": {
            "claims": {
                "sub": SUB,
                "cognito:username": SUB,
                "email": "reviewer@example.com",
                "token_use": "id",
            }
        },
        "identity": {"sourceIp": "203.0.113.7"},
    }
}

# IAM / direct execute-api caller — the R1.2 fallback, no claims at all.
IAM_EVENT: dict = {
    "requestContext": {
        "identity": {
            "caller": "arn:aws:iam::123456789012:user/test-user",
            "sourceIp": "10.0.0.1",
        }
    }
}

# Nothing resolvable: R2.3's terminal fallback case.
EMPTY_EVENT: dict = {}

mock_state_table = MagicMock()


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    mock_state_table.reset_mock()
    approvals_mod.approvals_table = mock_approvals_table
    approvals_mod.task_tokens_table = mock_task_tokens_table
    approvals_mod.state_table = mock_state_table
    yield


def _approval_record(has_task_token: bool = False) -> dict:
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "has_task_token": has_task_token,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "approval_type": "first_run",
        "created_at": "2025-01-15T12:00:00Z",
    }


def _persisted(attribute: str) -> str:
    """Value written to ``attribute`` by the approvals-table update."""
    calls = [c.kwargs for c in mock_approvals_table.update_item.call_args_list]
    matching = [c for c in calls if f":{attribute}" in c.get("ExpressionAttributeValues", {})]
    assert matching, f"no approvals update wrote :{attribute} (calls: {calls})"
    return matching[-1]["ExpressionAttributeValues"][f":{attribute}"]


def _decide(action: str, event: dict | None, body: dict) -> tuple[dict, MagicMock]:
    """Run approve or reject with ``body``/``event``, returning (response, audit mock).

    Both endpoints are driven through one helper on purpose: R2.1 requires them
    to behave identically, and a helper that can only express one of them would
    hide a one-sided change.
    """
    mock_approvals_table.get_item.return_value = {"Item": _approval_record()}
    fn = approvals_mod.approve_request if action == "approve" else approvals_mod.reject_request
    with patch.object(approvals_mod, "write_audit_entry") as mock_audit:
        response = fn(APPROVAL_ID, json.dumps(body), event=event)
    return response, mock_audit


def _audit_kwargs(mock_audit: MagicMock) -> dict:
    mock_audit.assert_called_once()
    return mock_audit.call_args[1]


# ===================================================================
# R2.2 — the authenticated principal wins over a body-supplied one
# ===================================================================


@pytest.mark.parametrize(
    ("action", "attribute", "body_key"),
    [("approve", "approved_by", "approved_by"), ("reject", "rejected_by", "rejected_by")],
)
class TestClaimsBeatBody:
    def test_claims_sub_is_persisted_not_the_body_field(self, action, attribute, body_key):
        """R2.2: an explicit body identity must not populate the attributed one."""
        response, _ = _decide(action, CLAIMS_EVENT, {body_key: "attacker@example.invalid"})
        assert response["statusCode"] == 200
        assert _persisted(attribute) == SUB

    def test_claims_sub_is_persisted_not_the_body_user_alias(self, action, attribute, body_key):
        """`user` is the key the live dashboard actually posts — same rule."""
        response, _ = _decide(action, CLAIMS_EVENT, {"user": "attacker@example.invalid"})
        assert response["statusCode"] == 200
        assert _persisted(attribute) == SUB

    def test_body_identity_survives_only_as_a_claimed_by_detail(self, action, attribute, body_key):
        """R2.2: retained for diagnostics, beside the verified actor, never as it."""
        _, mock_audit = _decide(action, CLAIMS_EVENT, {"user": "someone-else@example.com"})
        details = _audit_kwargs(mock_audit)["details"]
        assert details["claimed_by"] == "someone-else@example.com"
        assert _audit_kwargs(mock_audit)["actor"] == SUB

    def test_audit_actor_and_persisted_identity_agree(self, action, attribute, body_key):
        """One resolution feeds both writes, so they cannot disagree (R2.5 hoist)."""
        _, mock_audit = _decide(action, CLAIMS_EVENT, {"user": "someone-else@example.com"})
        assert _audit_kwargs(mock_audit)["actor"] == _persisted(attribute)

    def test_no_claimed_by_detail_when_body_supplies_no_identity(self, action, attribute, body_key):
        _, mock_audit = _decide(action, CLAIMS_EVENT, {})
        assert "claimed_by" not in _audit_kwargs(mock_audit)["details"]


# ===================================================================
# R2.3 — "dashboard-user" is gone; the fallback is "unknown"
# ===================================================================


@pytest.mark.parametrize(
    ("action", "attribute"),
    [("approve", "approved_by"), ("reject", "rejected_by")],
)
class TestFallbackIsUnknown:
    def test_unresolvable_principal_persists_unknown(self, action, attribute):
        """R2.3: the R1.3 terminal fallback, not a flattering default."""
        response, _ = _decide(action, EMPTY_EVENT, {})
        assert response["statusCode"] == 200
        assert _persisted(attribute) == "unknown"

    def test_missing_event_entirely_persists_unknown(self, action, attribute):
        """The `event` parameter is optional; five in-tree callers omit it."""
        mock_approvals_table.get_item.return_value = {"Item": _approval_record()}
        fn = approvals_mod.approve_request if action == "approve" else approvals_mod.reject_request
        with patch.object(approvals_mod, "write_audit_entry"):
            response = fn(APPROVAL_ID, json.dumps({"user": "tester"}))
        assert response["statusCode"] == 200
        assert _persisted(attribute) == "unknown"

    def test_dashboard_user_never_appears_anywhere(self, action, attribute):
        """Even when the client asks for it by name, on every surface."""
        _, mock_audit = _decide(action, EMPTY_EVENT, {"user": "dashboard-user"})
        assert _persisted(attribute) == "unknown"
        kwargs = _audit_kwargs(mock_audit)
        assert kwargs["actor"] == "unknown"
        # It is legal for the *claim* to echo the string the client sent — that is
        # the diagnostic. What must not happen is it being attributed.
        assert kwargs["details"].get("claimed_by") == "dashboard-user"

    def test_iam_caller_fallback_still_resolves(self, action, attribute):
        """R1.2 retained fallback reaches the approvals row unchanged."""
        response, mock_audit = _decide(action, IAM_EVENT, {"user": "tester"})
        assert response["statusCode"] == 200
        assert _persisted(attribute) == "arn:aws:iam::123456789012:user/test-user"
        assert "actor_email" not in _audit_kwargs(mock_audit)["details"]


# ===================================================================
# Ratified arm — email as a separate detail, not as the actor
# ===================================================================


@pytest.mark.parametrize("action", ["approve", "reject"])
class TestEmailIsADetailNotTheActor:
    def test_email_claim_lands_in_audit_details(self, action):
        _, mock_audit = _decide(action, CLAIMS_EVENT, {})
        assert _audit_kwargs(mock_audit)["details"]["actor_email"] == "reviewer@example.com"

    def test_actor_stays_the_stable_sub(self, action):
        """The attributed identity is `sub` per R2.1's "same resolver", even
        though `email` is the only human-readable claim on this pool."""
        _, mock_audit = _decide(action, CLAIMS_EVENT, {})
        kwargs = _audit_kwargs(mock_audit)
        assert kwargs["actor"] == SUB
        assert kwargs["actor"] != "reviewer@example.com"

    def test_details_still_carry_pipeline_id(self, action):
        """The pre-existing detail is not displaced by the new ones."""
        _, mock_audit = _decide(action, CLAIMS_EVENT, {})
        assert _audit_kwargs(mock_audit)["details"]["pipeline_id"] == "pipe-001"

    def test_no_actor_email_detail_when_no_email_claim(self, action):
        event = {"requestContext": {"authorizer": {"claims": {"sub": SUB}}}}
        _, mock_audit = _decide(action, event, {})
        details = _audit_kwargs(mock_audit)["details"]
        assert "actor_email" not in details
        assert _audit_kwargs(mock_audit)["actor"] == SUB


# ===================================================================
# R2.4 — persisted attribute names are unchanged
# ===================================================================


class TestPersistedFieldNamesUnchanged:
    def test_approve_still_writes_approved_by(self):
        """`decided_by` is synthesised on read (`pipelines_runs.py:1049`); the
        stored attribute must stay `approved_by` or 7 live rows stop resolving."""
        _decide("approve", CLAIMS_EVENT, {})
        expression = mock_approvals_table.update_item.call_args_list[-1].kwargs["UpdateExpression"]
        assert "approved_by = :approved_by" in expression
        assert "decided_by" not in expression

    def test_reject_still_writes_rejected_by(self):
        _decide("reject", CLAIMS_EVENT, {})
        expression = mock_approvals_table.update_item.call_args_list[-1].kwargs["UpdateExpression"]
        assert "rejected_by = :rejected_by" in expression
        assert "decided_by" not in expression


# ===================================================================
# p50 R2.2 — the email reaches the approvals row, beside the sub
# ===================================================================


def _persisted_or_none(attribute: str) -> str | None:
    """Value written to ``attribute``, or ``None`` if it was never written.

    ``None`` and ``""`` are different answers here and the distinction is the
    behaviour under test: `LGR-c58f276-2` records that an identity detail is
    **omitted when empty** rather than written blank, "so an audit row never
    asserts an identity it does not have".
    """
    for call in reversed([c.kwargs for c in mock_approvals_table.update_item.call_args_list]):
        values = call.get("ExpressionAttributeValues", {})
        if f":{attribute}" in values:
            return values[f":{attribute}"]
    return None


@pytest.mark.parametrize(
    ("action", "actor_attribute", "email_attribute"),
    [
        ("approve", "approved_by", "approved_by_email"),
        ("reject", "rejected_by", "rejected_by_email"),
    ],
)
class TestEmailReachesTheApprovalsRow:
    """p50 C3. `LGR-c58f276-2` captured the email as an audit **detail** and
    accepted, knowingly, that "Decided by" would render a UUID until a follow-up
    landed: that projection reads the **approvals** row via `get_run_details`, and
    the row carried no email. This is the write that closes it.

    Validates: Requirements R2.2, R3.3
    """

    def test_the_email_claim_is_persisted_on_the_row(self, action, actor_attribute, email_attribute):
        response, _ = _decide(action, CLAIMS_EVENT, {})
        assert response["statusCode"] == 200
        assert _persisted_or_none(email_attribute) == "reviewer@example.com"

    def test_the_row_email_matches_the_audit_detail(self, action, actor_attribute, email_attribute):
        """One resolution, two writes. `_authenticated_email` is pure over
        `event`, so the row and the audit row cannot name different people."""
        _, mock_audit = _decide(action, CLAIMS_EVENT, {})
        assert _persisted_or_none(email_attribute) == _audit_kwargs(mock_audit)["details"]["actor_email"]

    def test_the_attributed_identity_is_still_the_sub(self, action, actor_attribute, email_attribute):
        """R3.3 / CP3: the email is *additional*, never a replacement. Anything
        reading `approved_by` as a stable id has to keep working."""
        _decide(action, CLAIMS_EVENT, {})
        assert _persisted_or_none(actor_attribute) == SUB
        expression = mock_approvals_table.update_item.call_args_list[-1].kwargs["UpdateExpression"]
        assert f"{actor_attribute} = :{actor_attribute}" in expression

    def test_no_email_attribute_when_the_claim_is_absent(self, action, actor_attribute, email_attribute):
        event = {"requestContext": {"authorizer": {"claims": {"sub": SUB}}}}
        _decide(action, event, {})
        assert _persisted_or_none(email_attribute) is None
        expression = mock_approvals_table.update_item.call_args_list[-1].kwargs["UpdateExpression"]
        assert email_attribute not in expression

    def test_no_blank_email_attribute_when_the_claim_is_empty(self, action, actor_attribute, email_attribute):
        """Present-but-blank is not an identity: omitted, not written as ``""``."""
        event = {"requestContext": {"authorizer": {"claims": {"sub": SUB, "email": ""}}}}
        _decide(action, event, {})
        assert _persisted_or_none(email_attribute) is None

    def test_no_email_attribute_for_an_iam_caller(self, action, actor_attribute, email_attribute):
        """The R1.2 fallback has no claims at all, so there is no email to write."""
        _decide(action, IAM_EVENT, {})
        assert _persisted_or_none(email_attribute) is None
        assert _persisted_or_none(actor_attribute) == "arn:aws:iam::123456789012:user/test-user"

    def test_no_email_attribute_when_the_event_is_absent(self, action, actor_attribute, email_attribute):
        """Five in-tree callers omit `event`; the row then carries the `sub`
        fallback only, which is the state every pre-p50 row is in."""
        _decide(action, None, {})
        assert _persisted_or_none(email_attribute) is None
        assert _persisted_or_none(actor_attribute) == "unknown"

    def test_the_body_can_never_supply_the_email(self, action, actor_attribute, email_attribute):
        """R1.5's rule, now applied to a *persisted* field rather than a detail.
        The authorizer is the only source; a body value is unverifiable."""
        event = {"requestContext": {"authorizer": {"claims": {"sub": SUB}}}}
        _decide(action, event, {"email": "attacker@example.invalid", "actor_email": "attacker@example.invalid"})
        assert _persisted_or_none(email_attribute) is None

    def test_the_context_return_is_not_widened(self, action, actor_attribute, email_attribute):
        """CP4, re-asserted at the call site. `extract_request_context` returns
        exactly `{"actor", "ip_address"}` and both branches splat it into
        `write_audit_entry(..., **ctx)`; a third key would be a `TypeError` at all
        seven production call sites (`LGR-c58f276-3`, a **PROPOSED** invariant, not
        ratified). C3 is exactly the change that tempts that widening, so the
        no-widening claim is checked where a widening would land — not by importing
        the resolver here, which `LGR-c58f276-4`'s `sys.modules` pollution across
        four sibling modules makes order-dependent.
        """
        _, mock_audit = _decide(action, CLAIMS_EVENT, {})
        assert set(_audit_kwargs(mock_audit)) == {
            "action",
            "resource_type",
            "resource_id",
            "details",
            "actor",
            "ip_address",
        }


# ===================================================================
# The Step Functions payload carries the verified identity too
# ===================================================================


class TestStepFunctionsPayloadIdentity:
    """The decision identity appears in the token send as well as on the row.
    Leaving that read from the body would have made the change half-inert."""

    def _decide_with_token(self, action: str) -> dict:
        mock_approvals_table.get_item.return_value = {"Item": _approval_record(has_task_token=True)}
        mock_sfn = MagicMock()
        fn = approvals_mod.approve_request if action == "approve" else approvals_mod.reject_request
        with (
            patch.object(approvals_mod, "_get_task_token", return_value="tok-abc"),
            patch.object(approvals_mod, "_delete_task_token"),
            patch.object(approvals_mod, "write_audit_entry"),
            patch("boto3.client", return_value=mock_sfn),
        ):
            response = fn(APPROVAL_ID, json.dumps({"user": "attacker@example.invalid"}), event=CLAIMS_EVENT)
        assert response["statusCode"] == 200
        return mock_sfn

    def test_send_task_success_output_carries_the_resolved_actor(self):
        mock_sfn = self._decide_with_token("approve")
        output = json.loads(mock_sfn.send_task_success.call_args.kwargs["output"])
        assert output["approved_by"] == SUB

    def test_send_task_failure_cause_carries_the_resolved_actor(self):
        mock_sfn = self._decide_with_token("reject")
        cause = json.loads(mock_sfn.send_task_failure.call_args.kwargs["cause"])
        assert cause["rejected_by"] == SUB


# ===================================================================
# R2.1 — the two endpoints are not allowed to drift apart
# ===================================================================


class TestBothEndpointsBehaveIdentically:
    def test_same_event_yields_the_same_identity_on_both_paths(self):
        _, approve_audit = _decide("approve", CLAIMS_EVENT, {"user": "claimant@example.com"})
        approved_by = _persisted("approved_by")
        approve_details = _audit_kwargs(approve_audit)["details"]

        mock_approvals_table.reset_mock()
        _, reject_audit = _decide("reject", CLAIMS_EVENT, {"user": "claimant@example.com"})
        rejected_by = _persisted("rejected_by")
        reject_details = _audit_kwargs(reject_audit)["details"]

        assert approved_by == rejected_by == SUB
        assert approve_details == reject_details

    def test_neither_path_reads_an_identity_from_the_body(self):
        """The rejection path is the one a one-sided fix would leave behind."""
        for action, attribute in (("approve", "approved_by"), ("reject", "rejected_by")):
            mock_approvals_table.reset_mock()
            _decide(action, CLAIMS_EVENT, {attribute: "body-wins", "user": "body-wins-too"})
            assert _persisted(attribute) == SUB


# ===================================================================
# The local email reader never raises, for any shape
# ===================================================================


class TestAuthenticatedEmailIsTotal:
    @pytest.mark.parametrize(
        "event",
        [
            None,
            {},
            "not-a-dict",
            {"requestContext": None},
            {"requestContext": "nope"},
            {"requestContext": {"authorizer": None}},
            {"requestContext": {"authorizer": "nope"}},
            {"requestContext": {"authorizer": {"claims": None}}},
            {"requestContext": {"authorizer": {"claims": "nope"}}},
            {"requestContext": {"authorizer": {"claims": {}}}},
            {"requestContext": {"authorizer": {"claims": {"email": None}}}},
            {"requestContext": {"authorizer": {"claims": {"email": 42}}}},
            {"requestContext": {"authorizer": {"claims": {"email": ["a@b.c"]}}}},
        ],
    )
    def test_unreadable_shapes_return_empty_string(self, event):
        assert approvals_mod._authenticated_email(event) == ""

    def test_reads_the_email_claim_when_present(self):
        assert approvals_mod._authenticated_email(CLAIMS_EVENT) == "reviewer@example.com"

    def test_never_reads_an_email_from_the_body_or_headers(self):
        """R1.5's rule, applied to the detail field as well as the actor."""
        event = {
            "body": '{"email": "attacker@example.invalid"}',
            "headers": {"X-Email": "attacker@example.invalid"},
            "queryStringParameters": {"email": "attacker@example.invalid"},
            "requestContext": {"identity": {"caller": "arn:aws:iam::1:user/u"}},
        }
        assert approvals_mod._authenticated_email(event) == ""
