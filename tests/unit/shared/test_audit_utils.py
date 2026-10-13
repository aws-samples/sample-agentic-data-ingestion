# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/audit_utils.py.

Covers: successful write, write failure isolation, TTL calculation,
field completeness, and extract_request_context helper.
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Set env before import so require_env() uses the default
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


def _fresh_audit_utils():
    """Force reimport of audit_utils inside a clean context."""
    for mod_name in list(sys.modules):
        if "audit_utils" in mod_name:
            del sys.modules[mod_name]
    import audit_utils

    return audit_utils


@pytest.fixture
def audit_env():
    """Provide a mock audit table and a fresh audit_utils module."""
    mock_table = MagicMock()
    # Capture all put_item calls so we can inspect the written items
    written_items: list[dict] = []

    def _capture_put(Item, **kwargs):
        written_items.append(Item)

    mock_table.put_item.side_effect = _capture_put

    mod = _fresh_audit_utils()
    mod._audit_table = mock_table

    yield mod, mock_table, written_items


class TestWriteAuditEntrySuccess:
    """Verify successful audit writes land in DynamoDB with correct fields."""

    def test_write_audit_entry_creates_item(self, audit_env):
        mod, mock_table, written_items = audit_env

        mod.write_audit_entry(
            action="pipeline_created",
            resource_type="pipeline",
            resource_id="pipe-123",
            details={"name": "My Pipeline"},
            actor="user@example.com",
            ip_address="10.0.0.1",
        )

        assert len(written_items) == 1
        item = written_items[0]
        assert item["action"] == "pipeline_created"
        assert item["resource_type"] == "pipeline"
        assert item["resource_id"] == "pipe-123"
        assert item["details"] == {"name": "My Pipeline"}
        assert item["actor"] == "user@example.com"
        assert item["ip_address"] == "10.0.0.1"

    def test_write_audit_entry_defaults(self, audit_env):
        """Actor defaults to 'system', ip_address to '', details to {}."""
        mod, mock_table, written_items = audit_env

        mod.write_audit_entry(
            action="run_cancelled",
            resource_type="run",
            resource_id="run-456",
        )

        item = written_items[0]
        assert item["actor"] == "system"
        assert item["ip_address"] == ""
        assert item["details"] == {}


class TestWriteAuditEntryFieldCompleteness:
    """Every audit entry must contain all required fields."""

    REQUIRED_FIELDS = {
        "resource_id",
        "timestamp",
        "actor",
        "action",
        "resource_type",
        "details",
        "ip_address",
        "ttl",
    }

    def test_all_required_fields_present(self, audit_env):
        mod, mock_table, written_items = audit_env

        mod.write_audit_entry(
            action="approval_approved",
            resource_type="approval",
            resource_id="apr-789",
            actor="admin",
            ip_address="192.168.1.1",
        )

        item = written_items[0]
        assert self.REQUIRED_FIELDS.issubset(item.keys())

    def test_timestamp_is_iso8601(self, audit_env):
        mod, mock_table, written_items = audit_env

        mod.write_audit_entry(
            action="pipeline_edited",
            resource_type="pipeline",
            resource_id="pipe-ts",
        )

        ts = written_items[0]["timestamp"]
        # ISO 8601 timestamps contain 'T' separator
        assert "T" in ts


class TestTTLCalculation:
    """TTL must be set to ~365 days from now."""

    def test_ttl_is_approximately_365_days_from_now(self, audit_env):
        mod, mock_table, written_items = audit_env

        before = int(time.time())
        mod.write_audit_entry(
            action="pipeline_archived",
            resource_type="pipeline",
            resource_id="pipe-ttl",
        )
        after = int(time.time())

        ttl = int(written_items[0]["ttl"])

        one_year = 365 * 86_400
        assert before + one_year <= ttl <= after + one_year


class TestWriteFailureIsolation:
    """Audit write failures must be caught and logged, never propagated."""

    def test_exception_is_caught_and_logged(self, audit_env):
        mod, mock_table, written_items = audit_env

        # Override put_item to raise
        mock_table.put_item.side_effect = Exception("DynamoDB down")

        with patch.object(mod.logger, "exception") as mock_log:
            # Should NOT raise
            mod.write_audit_entry(
                action="pipeline_created",
                resource_type="pipeline",
                resource_id="pipe-fail",
            )
            mock_log.assert_called_once()
            assert "Failed to write audit entry" in mock_log.call_args[0][0]


# ── User Pool claims fixtures (p47 task A2 / R1.1) ─────────────────────────
#
# Shapes derived from a real Cognito User Pool authorizer event captured against
# the primary dev account: fourteen claims, `token_use=id`.
#
# Values below are fabricated or masked. The KEY NAMES AND CASING are verbatim
# from that capture — `cognito:username` is all lowercase with a single literal
# colon, no underscore.
#
# Claims that were ABSENT from the captured token, so no test here may depend on
# them: `cognito:groups`, `username`, `scope`, `client_id`, `identities`.
#
# `email_verified` is given as the string `"true"` because API Gateway stringifies
# claims into the Lambda event. That is documented platform behaviour rather than
# something observed on this deployment (observation-notes §3), and it does not
# affect the three keys R1.1 reads, which are strings either way.

# On the live pool `sub` and `cognito:username` are BYTE-IDENTICAL — the pool is
# configured `UsernameAttributes: ["email"]`, so Cognito generates a UUID as the
# real username (observation-notes §5). These fixtures deliberately use DISTINCT
# values for the two: a fixture reusing one value cannot distinguish R1.1's
# `sub`-first order from a `cognito:username`-first one, so it would assert
# nothing about the preference order.
_CLAIM_SUB = "11111111-2222-3333-4444-555555555555"
_CLAIM_COGNITO_USERNAME = "99999999-8888-7777-6666-555555555555"
_CLAIM_EMAIL = "masked.user@example.com"

_SOURCE_IP = "198.51.100.7"


def _user_pool_claims(drop: tuple[str, ...] = (), **overrides) -> dict[str, str]:
    """Return the 14-claim ID-token claim set from observation-notes §2.

    ``drop`` removes claims (to exercise R1.1's fallback order); ``overrides``
    replaces values.
    """
    claims: dict[str, str] = {
        "at_hash": "bWFza2VkLWF0LWhhc2g",
        "sub": _CLAIM_SUB,
        "email_verified": "true",
        "iss": "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_maskedpool",
        "cognito:username": _CLAIM_COGNITO_USERNAME,
        "origin_jti": "00000000-1111-2222-3333-444444444444",
        "aud": "4lbclt5m8n3n859k48httrpuup",
        "event_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "token_use": "id",
        "auth_time": "1785000000",
        "exp": "1785003600",
        "iat": "1785000000",
        "jti": "ffffffff-0000-1111-2222-333333333333",
        "email": _CLAIM_EMAIL,
    }
    for key in drop:
        claims.pop(key, None)
    claims.update(overrides)
    return claims


def _user_pool_event(claims, source_ip: str = _SOURCE_IP, identity_extra: dict | None = None) -> dict:
    """Wrap ``claims`` in the event shape a Cognito User Pool authorizer produces.

    Claims land at ``requestContext.authorizer.claims``; ``identity`` carries
    ``sourceIp`` but neither ``caller`` nor ``cognitoIdentityId`` under this
    authorizer type (observation-notes §4, established over 52 real requests).
    """
    identity: dict = {"sourceIp": source_ip}
    if identity_extra:
        identity.update(identity_extra)
    return {"requestContext": {"authorizer": {"claims": claims}, "identity": identity}}


# The claims-branch cases below carried `@_PENDING_A3`
# (`pytest.mark.xfail(strict=True)`) while the resolver was still identity-only.
# Task A3 landed the branch, so the marker and its definition are gone: under
# `strict=True` an XPASS is a failure, which is what made removing it the
# mechanical signal that the resolver arrived. Their expectations still come from
# `requirements.md` R1, not from current output (R1.6).


class TestExtractRequestContext:
    """Verify extraction of actor and ip_address from API Gateway events."""

    def test_extracts_caller_and_source_ip(self):
        mod = _fresh_audit_utils()
        event = {
            "requestContext": {
                "identity": {
                    "caller": "arn:aws:iam::123456789012:user/admin",
                    "sourceIp": "203.0.113.42",
                }
            }
        }
        ctx = mod.extract_request_context(event)
        assert ctx["actor"] == "arn:aws:iam::123456789012:user/admin"
        assert ctx["ip_address"] == "203.0.113.42"

    def test_falls_back_to_cognito_identity(self):
        mod = _fresh_audit_utils()
        event = {
            "requestContext": {
                "identity": {
                    "cognitoIdentityId": "us-east-1:abc-123",
                    "sourceIp": "10.0.0.5",
                }
            }
        }
        ctx = mod.extract_request_context(event)
        assert ctx["actor"] == "us-east-1:abc-123"

    def test_returns_unknown_when_no_identity(self):
        mod = _fresh_audit_utils()
        event = {"requestContext": {"identity": {}}}
        ctx = mod.extract_request_context(event)
        assert ctx["actor"] == "unknown"
        assert ctx["ip_address"] == ""

    def test_handles_missing_request_context(self):
        mod = _fresh_audit_utils()
        ctx = mod.extract_request_context({})
        assert ctx["actor"] == "unknown"
        assert ctx["ip_address"] == ""

    # ── Return shape (R1.3) ────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param({}, id="empty-event"),
            pytest.param({"requestContext": {"identity": {}}}, id="empty-identity"),
            pytest.param(
                {"requestContext": {"identity": {"caller": "arn:aws:iam::123456789012:user/admin"}}},
                id="iam-caller",
            ),
            pytest.param(
                {"requestContext": {"identity": {"cognitoIdentityId": "us-west-2:abc-123"}}},
                id="identity-pool",
            ),
        ],
    )
    def test_returns_exactly_actor_and_ip_address(self, event):
        """The returned dict carries exactly two keys, for every input shape.

        Not cosmetic: all 10 call sites splat the result — ``write_audit_entry(..., **ctx)``
        (e.g. ``handlers/pipelines_crud.py:1065-1074``) — and ``write_audit_entry`` is
        explicitly out of scope for p47 (design.md §2.1). A third key would therefore be
        a ``TypeError`` at every caller, not a widened contract.

        **Validates: Requirements R1.3**
        """
        mod = _fresh_audit_utils()
        ctx = mod.extract_request_context(event)
        assert set(ctx) == {"actor", "ip_address"}
        assert isinstance(ctx["actor"], str)
        assert isinstance(ctx["ip_address"], str)
        assert ctx["actor"] != ""

    # ── User Pool claims — R1.1 preference order ───────────────────────────

    def test_prefers_sub_from_user_pool_claims(self):
        """A full ID-token claim set resolves the actor to `sub`.

        `sub` first, ahead of `cognito:username` and `email`, and ahead of the
        `identity.*` reads. This is the shape every dashboard-driven request
        actually arrives in, and the one that records `"unknown"` today.

        **Validates: Requirements R1.1**
        """
        mod = _fresh_audit_utils()
        ctx = mod.extract_request_context(_user_pool_event(_user_pool_claims()))
        assert ctx["actor"] == _CLAIM_SUB
        assert ctx["ip_address"] == _SOURCE_IP

    def test_falls_back_to_cognito_username_when_sub_absent(self):
        """Second preference is `cognito:username` — exact lowercase, single colon.

        **Validates: Requirements R1.1**
        """
        mod = _fresh_audit_utils()
        claims = _user_pool_claims(drop=("sub",))
        assert "cognito:username" in claims  # guards the fixture, not the resolver
        ctx = mod.extract_request_context(_user_pool_event(claims))
        assert ctx["actor"] == _CLAIM_COGNITO_USERNAME

    def test_falls_back_to_email_when_sub_and_username_absent(self):
        """Third and last claims preference is `email`.

        **Validates: Requirements R1.1**
        """
        mod = _fresh_audit_utils()
        claims = _user_pool_claims(drop=("sub", "cognito:username"))
        ctx = mod.extract_request_context(_user_pool_event(claims))
        assert ctx["actor"] == _CLAIM_EMAIL

    def test_resolves_sub_only_claims_shape(self):
        """A `sub`-only claim set still resolves.

        The frontend sends the **ID** token (`App.tsx:63` wires `id_token`, and the
        captured token carried `token_use=id`), but the comment at
        `api-client.ts:115` says "access token". An access token carries `sub` and
        neither `cognito:username` nor `email` (observation-notes §6), so a switch
        to `access_token` would be a quiet regression — `sub` survives, so the
        resolver keeps working and nothing visibly breaks. This pins that shape.

        **Validates: Requirements R1.1**
        """
        mod = _fresh_audit_utils()
        claims = {"sub": _CLAIM_SUB, "token_use": "access", "client_id": "4lbclt5m8n3n859k48httrpuup"}
        ctx = mod.extract_request_context(_user_pool_event(claims))
        assert ctx["actor"] == _CLAIM_SUB

    def test_claims_take_precedence_over_identity_reads(self):
        """Claims resolve ahead of `identity.caller` when both are present.

        R1.1 makes claims the actor source; R1.2 keeps the `identity.*` reads as a
        *fallback*. Ordering matters for any direct `execute-api` call that carries
        both.

        **Validates: Requirements R1.1, R1.2**
        """
        mod = _fresh_audit_utils()
        event = _user_pool_event(
            _user_pool_claims(),
            identity_extra={"caller": "arn:aws:iam::123456789012:user/admin"},
        )
        ctx = mod.extract_request_context(event)
        assert ctx["actor"] == _CLAIM_SUB

    def test_source_ip_survives_claims_resolution(self):
        """`ip_address` still comes from `identity.sourceIp`, not from claims.

        The two values are read from different branches of the same event, and
        `sourceIp` is populated under every authorizer type (confirmed empirically:
        52 audit rows carry a real IP alongside `actor == "unknown"`).

        **Validates: Requirements R1.4**
        """
        mod = _fresh_audit_utils()
        ctx = mod.extract_request_context(_user_pool_event(_user_pool_claims(), source_ip="203.0.113.99"))
        assert ctx["ip_address"] == "203.0.113.99"
        assert ctx["actor"] == _CLAIM_SUB

    # ── Malformed and hostile shapes — R1.3, no raise ──────────────────────

    @pytest.mark.parametrize(
        "claims",
        [
            pytest.param(None, id="claims-none"),
            pytest.param("not-a-dict", id="claims-str"),
            pytest.param(["sub", _CLAIM_SUB], id="claims-list"),
            pytest.param(42, id="claims-int"),
            pytest.param({}, id="claims-empty"),
            pytest.param({"iss": "https://example.invalid"}, id="claims-no-identifier"),
        ],
    )
    def test_malformed_claims_do_not_raise(self, claims):
        """A malformed or identifier-free claims block degrades to the terminal fallback.

        This function is on a fire-and-forget audit path and must never be able to
        fail a request.

        **Validates: Requirements R1.3**
        """
        mod = _fresh_audit_utils()
        ctx = mod.extract_request_context(_user_pool_event(claims))
        assert set(ctx) == {"actor", "ip_address"}
        assert ctx["actor"] == "unknown"
        assert ctx["ip_address"] == _SOURCE_IP

    @pytest.mark.parametrize(
        "authorizer",
        [
            pytest.param(None, id="authorizer-none"),
            pytest.param("not-a-dict", id="authorizer-str"),
            pytest.param({}, id="authorizer-empty"),
            pytest.param({"principalId": "some-principal"}, id="authorizer-no-claims"),
        ],
    )
    def test_malformed_authorizer_does_not_raise(self, authorizer):
        """A missing or non-dict `authorizer` degrades to the terminal fallback.

        Whether the live authorizer carries siblings beside `claims` (e.g.
        `principalId`) was not verified (observation-notes §3), so both shapes are
        pinned rather than assumed.

        **Validates: Requirements R1.3**
        """
        mod = _fresh_audit_utils()
        event = {"requestContext": {"authorizer": authorizer, "identity": {"sourceIp": _SOURCE_IP}}}
        ctx = mod.extract_request_context(event)
        assert ctx["actor"] == "unknown"
        assert ctx["ip_address"] == _SOURCE_IP

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param({"requestContext": None}, id="request-context-none"),
            pytest.param({"requestContext": "not-a-dict"}, id="request-context-str"),
            pytest.param({"requestContext": []}, id="request-context-list"),
            pytest.param({"requestContext": {"identity": None}}, id="identity-none"),
            pytest.param({"requestContext": {"identity": "not-a-dict"}}, id="identity-str"),
        ],
    )
    def test_non_dict_request_context_or_identity_does_not_raise(self, event):
        """A non-dict `requestContext` or `identity` must not raise.

        These shapes raise `AttributeError` today (`audit_utils.py:82-83` calls
        `.get()` on whatever it finds). R1.3 forbids that: this function must not be
        able to fail a request, and "unrecognised shape" includes shapes API Gateway
        never produces — the direct-invocation path reaches this resolver too
        (`tests/unit/lambdas/dashboard_api/test_audit_writes.py:392`).

        **Validates: Requirements R1.3**
        """
        mod = _fresh_audit_utils()
        ctx = mod.extract_request_context(event)
        assert set(ctx) == {"actor", "ip_address"}
        assert ctx["actor"] == "unknown"

    # ── R1.5 — the actor is never client-supplied ──────────────────────────

    def test_actor_never_read_from_body_or_headers(self):
        """Body and header values must not become the actor.

        Passes today and must keep passing through A3. It is the guard that stops
        R2's "take the approver from the request body" defect being reintroduced one
        layer down, where it would look like an identity resolver.

        **Validates: Requirements R1.5**
        """
        mod = _fresh_audit_utils()
        event = {
            "body": '{"user": "attacker@example.invalid", "approved_by": "admin"}',
            "headers": {
                "X-Actor": "attacker@example.invalid",
                "Authorization": "Bearer forged",
                "x-amzn-oidc-identity": "attacker@example.invalid",
            },
            "queryStringParameters": {"user": "attacker@example.invalid"},
            "requestContext": {"identity": {"sourceIp": _SOURCE_IP}},
        }
        ctx = mod.extract_request_context(event)
        assert ctx["actor"] == "unknown"
        assert ctx["ip_address"] == _SOURCE_IP

    def test_empty_claim_values_fall_through_to_identity_reads(self):
        """Empty-string claims are not a resolved principal; R1.2's fallback applies.

        Guards against a post-A3 resolver that keys on key *presence*
        (`"sub" in claims`) rather than on a truthy value, which would resolve an
        empty actor and defeat R1.2. Passes today; must keep passing after A3.

        **Validates: Requirements R1.2**
        """
        mod = _fresh_audit_utils()
        claims = _user_pool_claims(sub="", **{"cognito:username": "", "email": ""})
        event = _user_pool_event(claims, identity_extra={"caller": "arn:aws:iam::123456789012:user/admin"})
        ctx = mod.extract_request_context(event)
        assert ctx["actor"] == "arn:aws:iam::123456789012:user/admin"
