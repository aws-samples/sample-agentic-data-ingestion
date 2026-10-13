# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the WebSocket $connect authorizer Lambda.

Validates Requirements 5.1–5.4: WebSocket API requires a valid Cognito JWT
token passed as a query-string parameter on $connect.

Also covers recommendation R-05: a token must have been issued to one of the
app clients listed in USER_POOL_CLIENT_IDS (`aud` on an ID token, `client_id`
on an access token). A validly signed token from another app client in the
same user pool is denied, and an unset or empty list denies everything.
"""

import base64
import json
import os
import sys
import time
import urllib.error
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_ws_authorizer"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# ---------------------------------------------------------------------------
# Environment required before import
# ---------------------------------------------------------------------------
# The authorizer freezes ISSUER = f"https://cognito-idp.{AWS_REGION}...
# /{USER_POOL_ID}" into a module-level constant at import time, so these values
# must be exact before ``import lambda_function`` runs.
#
# These were previously ``os.environ.setdefault`` calls, which are no-ops once
# the variable exists. Any earlier test module (tests/unit/lambdas/create_code/*
# pins us-east-1) or a stray ``export AWS_REGION=...`` in the shell therefore
# won, ISSUER was built for the wrong region, and every claims test failed with
# "Invalid issuer" — in a full-suite run only, passing in isolation. Assign
# explicitly so this module's expectations hold regardless of import order.
#
# USER_POOL_CLIENT_IDS is NOT needed at import: the authorizer reads it per
# invocation, so the autouse fixture below sets it per test with monkeypatch.
TEST_AWS_REGION = "us-west-2"
TEST_USER_POOL_ID = "us-west-2_testPool123"
TEST_USER_POOL_CLIENT_ID = "test-client-id-123"
SECOND_CLIENT_ID = "e2e-client-id-456"
OTHER_CLIENT_ID = "other-app-client-789"

os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ["USER_POOL_ID"] = TEST_USER_POOL_ID
os.environ["AWS_REGION"] = TEST_AWS_REGION

# Clear cached module to pick up fresh env
sys.modules.pop("lambda_function", None)
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

import lambda_function as ws_authorizer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
METHOD_ARN = f"arn:aws:execute-api:{TEST_AWS_REGION}:123456789012:abc123def/prod/$connect"

# Derived from the same values the authorizer was imported with, so the two can
# never drift apart.
TEST_ISSUER = f"https://cognito-idp.{TEST_AWS_REGION}.amazonaws.com/{TEST_USER_POOL_ID}"

ALLOWED = frozenset({TEST_USER_POOL_CLIENT_ID})
TEST_KID = "test-kid-1"


@pytest.fixture(autouse=True)
def _client_ids_env(monkeypatch):
    """Default config for every test: one allowed client, empty JWKS cache.

    The legacy singular USER_POOL_CLIENT_ID is removed so that no test passes
    because of it; the authorizer must not read it.
    """
    monkeypatch.setenv("USER_POOL_CLIENT_IDS", TEST_USER_POOL_CLIENT_ID)
    monkeypatch.delenv("USER_POOL_CLIENT_ID", raising=False)
    # No SSM parameter by default, so no test reaches SSM unless it opts in.
    monkeypatch.delenv("USER_POOL_CLIENT_ID_PARAMETERS", raising=False)
    monkeypatch.setattr(ws_authorizer, "_jwks_cache", None)
    monkeypatch.setattr(ws_authorizer, "_jwks_fetched_at", None)
    monkeypatch.setattr(ws_authorizer, "_unknown_kids", OrderedDict())
    monkeypatch.setattr(ws_authorizer, "_parameter_cache", None)
    monkeypatch.setattr(ws_authorizer, "_ssm_client", None)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_jwt_parts(header: dict, payload: dict) -> str:
    """Create a fake JWT string (header.payload.signature) with base64url encoding."""
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(payload).encode())
    # Fake signature (won't pass verification but useful for extraction tests)
    s = _b64url(b"fakesignature")
    return f"{h}.{p}.{s}"


def _valid_payload(
    sub: str = "user-123",
    iss: str | None = None,
    exp: float | None = None,
    token_use: str = "access",
    client_id: str = TEST_USER_POOL_CLIENT_ID,
) -> dict:
    """Build a valid Cognito access token payload."""
    return {
        "sub": sub,
        "iss": iss or TEST_ISSUER,
        "exp": exp or (time.time() + 3600),
        "token_use": token_use,
        "client_id": client_id,
    }


def _id_token_payload(sub: str = "user-123", aud: str = TEST_USER_POOL_CLIENT_ID) -> dict:
    """Build a valid Cognito ID token payload (client in `aud`, no `client_id`)."""
    now = time.time()
    return {
        "sub": sub,
        "iss": TEST_ISSUER,
        "aud": aud,
        "token_use": "id",
        "auth_time": int(now),
        "iat": int(now),
        "exp": now + 3600,
        "cognito:username": "someone",
        "email": "someone@example.com",
    }


def _connect_event(token: str | None = None) -> dict:
    """Build a WebSocket $connect authorizer event."""
    event: dict = {
        "methodArn": METHOD_ARN,
        "requestContext": {
            "connectionId": "test-conn-id",
            "routeKey": "$connect",
        },
        "headers": {},
        "queryStringParameters": {},
    }
    if token is not None:
        event["queryStringParameters"]["token"] = token
    return event


def _effect(result: dict) -> str:
    return result["policyDocument"]["Statement"][0]["Effect"]


# ---------------------------------------------------------------------------
# Real RS256 signing: an independent signer (the `cryptography` package) makes
# tokens that the authorizer's hand-written verifier must accept or reject.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _int_b64url(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _jwks_for(key, kid: str = TEST_KID) -> dict:
    numbers = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kid": kid,
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "n": _int_b64url(numbers.n),
                "e": _int_b64url(numbers.e),
            }
        ]
    }


def _sign(key, payload: dict, kid: str = TEST_KID, alg: str = "RS256") -> str:
    header = {"kid": kid, "alg": alg}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


@pytest.fixture
def jwks(monkeypatch, rsa_key):
    """Serve the test key as the user pool JWKS. No network."""
    calls = []

    def _fake_fetch():
        calls.append(1)
        return _jwks_for(rsa_key)

    monkeypatch.setattr(ws_authorizer, "_fetch_jwks", _fake_fetch)
    return calls


# ---------------------------------------------------------------------------
# Tests: Token extraction
# ---------------------------------------------------------------------------


class TestTokenExtraction:
    """Token is extracted from query string or Authorization header."""

    def test_extract_from_query_string(self):
        event = _connect_event(token="my-token")
        result = ws_authorizer._extract_token(event)
        assert result == "my-token"

    def test_extract_from_authorization_header(self):
        event = _connect_event()
        event["queryStringParameters"] = {}
        event["headers"] = {"Authorization": "Bearer header-token"}
        result = ws_authorizer._extract_token(event)
        assert result == "header-token"

    def test_no_token_returns_none(self):
        event = _connect_event()
        event["queryStringParameters"] = {}
        event["headers"] = {}
        result = ws_authorizer._extract_token(event)
        assert result is None

    def test_query_string_takes_precedence_over_header(self):
        event = _connect_event(token="qs-token")
        event["headers"] = {"Authorization": "Bearer header-token"}
        result = ws_authorizer._extract_token(event)
        assert result == "qs-token"

    def test_none_query_params_handled(self):
        event = _connect_event()
        event["queryStringParameters"] = None
        event["headers"] = None
        result = ws_authorizer._extract_token(event)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: Policy generation
# ---------------------------------------------------------------------------


class TestPolicyGeneration:
    """IAM policy documents are generated correctly."""

    def test_allow_policy_structure(self):
        policy = ws_authorizer._generate_policy("user-sub-123", "Allow", METHOD_ARN)
        assert policy["principalId"] == "user-sub-123"
        assert policy["policyDocument"]["Version"] == "2012-10-17"
        stmt = policy["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Allow"
        assert stmt["Action"] == "execute-api:Invoke"
        # Resource should be wildcard for the stage
        assert "abc123def/prod/*" in stmt["Resource"]

    def test_deny_policy_structure(self):
        policy = ws_authorizer._generate_policy("user", "Deny", METHOD_ARN)
        stmt = policy["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Deny"


# ---------------------------------------------------------------------------
# Tests: USER_POOL_CLIENT_IDS parsing
# ---------------------------------------------------------------------------


class TestAllowedClientIdsParsing:
    """USER_POOL_CLIENT_IDS is comma-separated, stripped, empty entries dropped."""

    def test_single_id(self, monkeypatch):
        monkeypatch.setenv("USER_POOL_CLIENT_IDS", "abc")
        assert ws_authorizer._allowed_client_ids() == frozenset({"abc"})

    def test_multiple_ids_with_whitespace_and_empty_entries(self, monkeypatch):
        monkeypatch.setenv("USER_POOL_CLIENT_IDS", " a , b,, c ,")
        assert ws_authorizer._allowed_client_ids() == frozenset({"a", "b", "c"})

    def test_unset_is_empty(self, monkeypatch):
        monkeypatch.delenv("USER_POOL_CLIENT_IDS", raising=False)
        assert ws_authorizer._allowed_client_ids() == frozenset()

    @pytest.mark.parametrize("raw", ["", "   ", ",", " , ,, "])
    def test_blank_values_are_empty(self, monkeypatch, raw):
        monkeypatch.setenv("USER_POOL_CLIENT_IDS", raw)
        assert ws_authorizer._allowed_client_ids() == frozenset()

    def test_legacy_singular_var_is_ignored(self, monkeypatch):
        monkeypatch.delenv("USER_POOL_CLIENT_IDS", raising=False)
        monkeypatch.setenv("USER_POOL_CLIENT_ID", TEST_USER_POOL_CLIENT_ID)
        assert ws_authorizer._allowed_client_ids() == frozenset()


# ---------------------------------------------------------------------------
# Tests: USER_POOL_CLIENT_ID_PARAMETERS (dashboard client from Hosting, e2e from Auth)
# ---------------------------------------------------------------------------

DASHBOARD_PARAMETER = "/agentic-data-ingestion/test/dashboard-client-id"
E2E_PARAMETER = "/agentic-data-ingestion/test/e2e-client-id"
DASHBOARD_CLIENT_ID = "dashboard-client-id-321"


class _FakeSsm:
    """Stands in for the SSM client: a name -> value map, and a call log.

    Names absent from the map come back in InvalidParameters, as GetParameters
    reports them. ``fail`` makes the call itself raise.
    """

    def __init__(self, values: dict[str, str] | None = None, fail: bool = False):
        self.values = {DASHBOARD_PARAMETER: DASHBOARD_CLIENT_ID} if values is None else values
        self.fail = fail
        self.calls: list[list[str]] = []

    def get_parameters(self, Names):  # boto3's keyword spelling
        self.calls.append(list(Names))
        if self.fail:
            raise RuntimeError("AccessDeniedException")
        return {
            "Parameters": [{"Name": n, "Value": self.values[n]} for n in Names if n in self.values],
            "InvalidParameters": [n for n in Names if n not in self.values],
        }


class TestClientIdParameters:
    """The SSM parameters add client IDs to the env list, read lazily and cached."""

    def test_parameter_ids_join_the_env_ids(self, monkeypatch):
        monkeypatch.setenv("USER_POOL_CLIENT_ID_PARAMETERS", DASHBOARD_PARAMETER)
        fake = _FakeSsm({DASHBOARD_PARAMETER: f" {DASHBOARD_CLIENT_ID} , "})
        monkeypatch.setattr(ws_authorizer, "_ssm_client", fake)
        assert ws_authorizer._allowed_client_ids() == frozenset({TEST_USER_POOL_CLIENT_ID, DASHBOARD_CLIENT_ID})
        assert fake.calls == [[DASHBOARD_PARAMETER]]

    def test_both_parameters_are_read_in_one_call(self, monkeypatch):
        monkeypatch.delenv("USER_POOL_CLIENT_IDS", raising=False)
        monkeypatch.setenv("USER_POOL_CLIENT_ID_PARAMETERS", f"{DASHBOARD_PARAMETER},{E2E_PARAMETER}")
        fake = _FakeSsm({DASHBOARD_PARAMETER: DASHBOARD_CLIENT_ID, E2E_PARAMETER: SECOND_CLIENT_ID})
        monkeypatch.setattr(ws_authorizer, "_ssm_client", fake)
        assert ws_authorizer._allowed_client_ids() == frozenset({DASHBOARD_CLIENT_ID, SECOND_CLIENT_ID})
        assert len(fake.calls) == 1

    def test_a_missing_parameter_contributes_nothing(self, monkeypatch):
        """The e2e client is not deployed: its parameter does not exist."""
        monkeypatch.delenv("USER_POOL_CLIENT_IDS", raising=False)
        monkeypatch.setenv("USER_POOL_CLIENT_ID_PARAMETERS", f"{DASHBOARD_PARAMETER},{E2E_PARAMETER}")
        monkeypatch.setattr(ws_authorizer, "_ssm_client", _FakeSsm())
        assert ws_authorizer._allowed_client_ids() == frozenset({DASHBOARD_CLIENT_ID})

    def test_parameter_alone_is_enough(self, monkeypatch, jwks, rsa_key):
        """The default deployed shape: env list empty, dashboard parameter present."""
        monkeypatch.delenv("USER_POOL_CLIENT_IDS", raising=False)
        monkeypatch.setenv("USER_POOL_CLIENT_ID_PARAMETERS", DASHBOARD_PARAMETER)
        monkeypatch.setattr(ws_authorizer, "_ssm_client", _FakeSsm())
        token = _sign(rsa_key, _id_token_payload(aud=DASHBOARD_CLIENT_ID))
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Allow"

    def test_parameter_does_not_admit_other_clients(self, monkeypatch, jwks, rsa_key):
        monkeypatch.delenv("USER_POOL_CLIENT_IDS", raising=False)
        monkeypatch.setenv("USER_POOL_CLIENT_ID_PARAMETERS", DASHBOARD_PARAMETER)
        monkeypatch.setattr(ws_authorizer, "_ssm_client", _FakeSsm())
        token = _sign(rsa_key, _id_token_payload(aud=OTHER_CLIENT_ID))
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Deny"

    def test_reads_are_cached_within_the_window(self, monkeypatch):
        monkeypatch.setenv("USER_POOL_CLIENT_ID_PARAMETERS", DASHBOARD_PARAMETER)
        fake = _FakeSsm()
        monkeypatch.setattr(ws_authorizer, "_ssm_client", fake)
        for _ in range(3):
            ws_authorizer._allowed_client_ids()
        assert len(fake.calls) == 1

    def test_cache_expires_after_the_window(self, monkeypatch):
        monkeypatch.setenv("USER_POOL_CLIENT_ID_PARAMETERS", DASHBOARD_PARAMETER)
        fake = _FakeSsm()
        monkeypatch.setattr(ws_authorizer, "_ssm_client", fake)
        clock = [1000.0]
        monkeypatch.setattr(ws_authorizer.time, "monotonic", lambda: clock[0])
        ws_authorizer._allowed_client_ids()
        clock[0] += ws_authorizer.PARAMETER_CACHE_SECONDS + 1
        ws_authorizer._allowed_client_ids()
        assert len(fake.calls) == 2

    def test_a_failed_read_contributes_nothing_and_is_cached(self, monkeypatch):
        """SSM unreachable or denied: the env list still works, and SSM is not hammered."""
        monkeypatch.setenv("USER_POOL_CLIENT_ID_PARAMETERS", DASHBOARD_PARAMETER)
        fake = _FakeSsm(fail=True)
        monkeypatch.setattr(ws_authorizer, "_ssm_client", fake)
        with patch.object(ws_authorizer.logger, "warning") as log_warning:
            first = ws_authorizer._allowed_client_ids()
            second = ws_authorizer._allowed_client_ids()
        assert first == second == frozenset({TEST_USER_POOL_CLIENT_ID})
        assert len(fake.calls) == 1
        log_warning.assert_called_once()

    def test_no_parameters_and_empty_env_denies_and_logs(self, monkeypatch, jwks, rsa_key):
        """Hosting not deployed yet and no static ID: deny, and say why."""
        monkeypatch.delenv("USER_POOL_CLIENT_IDS", raising=False)
        monkeypatch.setenv("USER_POOL_CLIENT_ID_PARAMETERS", DASHBOARD_PARAMETER)
        monkeypatch.setattr(ws_authorizer, "_ssm_client", _FakeSsm({}))
        token = _sign(rsa_key, _id_token_payload())
        with patch.object(ws_authorizer.logger, "error") as log_error:
            result = ws_authorizer.lambda_handler(_connect_event(token=token), None)
        assert _effect(result) == "Deny"
        log_error.assert_called_once()
        assert "USER_POOL_CLIENT_ID_PARAMETERS" in log_error.call_args.args

    def test_no_parameter_names_means_no_ssm_call(self, monkeypatch):
        fake = _FakeSsm()
        monkeypatch.setattr(ws_authorizer, "_ssm_client", fake)
        assert ws_authorizer._allowed_client_ids() == frozenset({TEST_USER_POOL_CLIENT_ID})
        assert fake.calls == []


# ---------------------------------------------------------------------------
# Tests: Claims validation
# ---------------------------------------------------------------------------


class TestClaimsValidation:
    """JWT claims are validated correctly."""

    def test_valid_access_token_claims_pass(self):
        payload = _valid_payload()
        # Should not raise
        ws_authorizer._validate_claims(payload, ALLOWED)

    def test_valid_id_token_claims_pass(self):
        payload = _valid_payload(token_use="id")
        payload["aud"] = TEST_USER_POOL_CLIENT_ID
        del payload["client_id"]
        ws_authorizer._validate_claims(payload, ALLOWED)

    def test_invalid_issuer_raises(self):
        payload = _valid_payload(iss="https://evil.example.com")
        with pytest.raises(ValueError, match="Invalid issuer"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    def test_expired_token_raises(self):
        payload = _valid_payload(exp=time.time() - 100)
        with pytest.raises(ValueError, match="expired"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    def test_invalid_token_use_raises(self):
        payload = _valid_payload(token_use="refresh")
        with pytest.raises(ValueError, match="Invalid token_use"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    def test_invalid_client_id_raises(self):
        payload = _valid_payload(client_id="wrong-client")
        with pytest.raises(ValueError, match="Invalid client_id"):
            ws_authorizer._validate_claims(payload, ALLOWED)


class TestExpiryAndNotBefore:
    """`exp` must be a number in the future; `nbf`, when present, in the past."""

    NOW = 1_800_000_000.0

    @pytest.fixture(autouse=True)
    def _frozen_clock(self, monkeypatch):
        monkeypatch.setattr(ws_authorizer, "time", SimpleNamespace(time=lambda: self.NOW))

    def test_exp_equal_to_now_is_expired(self):
        with pytest.raises(ValueError, match="expired"):
            ws_authorizer._validate_claims(_valid_payload(exp=self.NOW), ALLOWED)

    def test_exp_missing_is_rejected(self):
        payload = _valid_payload(exp=self.NOW + 60)
        del payload["exp"]
        with pytest.raises(ValueError, match="expired"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    @pytest.mark.parametrize("bad_exp", ["9999999999", True, None, [1]])
    def test_exp_non_numeric_is_rejected(self, bad_exp):
        payload = _valid_payload(exp=self.NOW + 60)
        payload["exp"] = bad_exp
        with pytest.raises(ValueError, match="expired"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    def test_nbf_in_future_is_rejected(self):
        payload = _valid_payload(exp=self.NOW + 60)
        payload["nbf"] = self.NOW + 30
        with pytest.raises(ValueError, match="nbf"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    def test_nbf_in_past_passes(self):
        payload = _valid_payload(exp=self.NOW + 60)
        payload["nbf"] = self.NOW - 30
        ws_authorizer._validate_claims(payload, ALLOWED)

    def test_nbf_non_numeric_is_rejected(self):
        payload = _valid_payload(exp=self.NOW + 60)
        payload["nbf"] = "yesterday"
        with pytest.raises(ValueError, match="nbf"):
            ws_authorizer._validate_claims(payload, ALLOWED)


class TestAppClientCheck:
    """R-05: the token must name an allowed app client in the right claim."""

    def test_id_token_with_allowed_aud_passes(self):
        ws_authorizer._validate_claims(_id_token_payload(), ALLOWED)

    def test_access_token_with_allowed_client_id_passes(self):
        ws_authorizer._validate_claims(_valid_payload(), ALLOWED)

    def test_id_token_with_other_aud_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid aud in id token"):
            ws_authorizer._validate_claims(_id_token_payload(aud=OTHER_CLIENT_ID), ALLOWED)

    def test_access_token_with_other_client_id_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid client_id in access token"):
            ws_authorizer._validate_claims(_valid_payload(client_id=OTHER_CLIENT_ID), ALLOWED)

    def test_id_token_missing_aud_is_rejected_even_with_allowed_client_id(self):
        """An ID token is judged on `aud` only; a `client_id` claim does not substitute."""
        payload = _id_token_payload()
        del payload["aud"]
        payload["client_id"] = TEST_USER_POOL_CLIENT_ID
        with pytest.raises(ValueError, match="Invalid aud"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    def test_access_token_missing_client_id_is_rejected_even_with_allowed_aud(self):
        """An access token is judged on `client_id` only; an `aud` claim does not substitute."""
        payload = _valid_payload()
        del payload["client_id"]
        payload["aud"] = TEST_USER_POOL_CLIENT_ID
        with pytest.raises(ValueError, match="Invalid client_id"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    def test_aud_as_list_is_rejected(self):
        """Cognito issues a string `aud`; an array is not accepted even if it contains our id."""
        payload = _id_token_payload()
        payload["aud"] = [TEST_USER_POOL_CLIENT_ID]
        with pytest.raises(ValueError, match="Invalid aud"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    @pytest.mark.parametrize("token_use", ["refresh", "ID", "Access", "", None, 1, ["id"]])
    def test_unknown_token_use_is_rejected(self, token_use):
        payload = _id_token_payload()
        payload["token_use"] = token_use
        with pytest.raises(ValueError, match="Invalid token_use"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    def test_missing_token_use_is_rejected(self):
        payload = _id_token_payload()
        del payload["token_use"]
        with pytest.raises(ValueError, match="Invalid token_use"):
            ws_authorizer._validate_claims(payload, ALLOWED)

    def test_empty_allowed_set_rejects_a_correct_token(self):
        with pytest.raises(ValueError, match="not an allowed app client"):
            ws_authorizer._validate_claims(_id_token_payload(), frozenset())

    def test_any_id_in_a_multi_id_list_is_accepted(self):
        allowed = frozenset({TEST_USER_POOL_CLIENT_ID, SECOND_CLIENT_ID})
        ws_authorizer._validate_claims(_id_token_payload(aud=SECOND_CLIENT_ID), allowed)
        ws_authorizer._validate_claims(_valid_payload(client_id=SECOND_CLIENT_ID), allowed)
        with pytest.raises(ValueError, match="not an allowed app client"):
            ws_authorizer._validate_claims(_id_token_payload(aud=OTHER_CLIENT_ID), allowed)


class TestHeaderChecks:
    """The header must say alg=RS256 and carry a kid, before any key lookup."""

    @pytest.mark.parametrize("alg", ["none", "HS256", "RS512", "rs256"])
    def test_wrong_alg_is_rejected(self, alg):
        token = _make_jwt_parts({"kid": TEST_KID, "alg": alg}, _valid_payload())
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            ws_authorizer._verify_token(token, ALLOWED)

    def test_missing_alg_is_rejected_not_assumed(self):
        token = _make_jwt_parts({"kid": TEST_KID}, _valid_payload())
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            ws_authorizer._verify_token(token, ALLOWED)

    def test_missing_kid_is_rejected(self):
        token = _make_jwt_parts({"alg": "RS256"}, _valid_payload())
        with pytest.raises(ValueError, match="missing 'kid'"):
            ws_authorizer._verify_token(token, ALLOWED)


# ---------------------------------------------------------------------------
# Tests: JWT decoding
# ---------------------------------------------------------------------------


class TestJwtDecoding:
    """JWT base64url parts are decoded correctly."""

    def test_decode_jwt_part_valid(self):
        data = {"sub": "user-123", "iss": "test"}
        encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
        result = ws_authorizer._decode_jwt_part(encoded)
        assert result == data

    def test_decode_jwt_part_with_padding(self):
        # Short payload that needs padding
        data = {"a": 1}
        encoded = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
        result = ws_authorizer._decode_jwt_part(encoded)
        assert result == data


# ---------------------------------------------------------------------------
# Tests: Handler integration (mocking verification)
# ---------------------------------------------------------------------------


class TestHandlerIntegration:
    """Full handler returns Allow/Deny based on token presence and verification."""

    def test_no_token_returns_deny(self):
        event = _connect_event()
        event["queryStringParameters"] = {}
        result = ws_authorizer.lambda_handler(event, None)
        stmt = result["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Deny"

    def test_invalid_format_token_returns_deny(self):
        event = _connect_event(token="not-a-jwt")
        result = ws_authorizer.lambda_handler(event, None)
        stmt = result["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Deny"

    @patch.object(ws_authorizer, "_verify_token")
    def test_valid_token_returns_allow(self, mock_verify):
        mock_verify.return_value = {"sub": "user-abc"}
        event = _connect_event(token="valid.jwt.token")
        result = ws_authorizer.lambda_handler(event, None)
        assert result["principalId"] == "user-abc"
        stmt = result["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Allow"

    @patch.object(ws_authorizer, "_verify_token")
    def test_verification_exception_returns_deny(self, mock_verify):
        mock_verify.side_effect = ValueError("Token is expired")
        event = _connect_event(token="expired.jwt.token")
        result = ws_authorizer.lambda_handler(event, None)
        stmt = result["policyDocument"]["Statement"][0]
        assert stmt["Effect"] == "Deny"

    @patch.object(ws_authorizer, "_verify_token")
    def test_handler_passes_parsed_client_ids_to_verifier(self, mock_verify, monkeypatch):
        monkeypatch.setenv("USER_POOL_CLIENT_IDS", f"{TEST_USER_POOL_CLIENT_ID}, {SECOND_CLIENT_ID}")
        mock_verify.return_value = {"sub": "user-abc"}
        ws_authorizer.lambda_handler(_connect_event(token="valid.jwt.token"), None)
        mock_verify.assert_called_once_with("valid.jwt.token", frozenset({TEST_USER_POOL_CLIENT_ID, SECOND_CLIENT_ID}))


# ---------------------------------------------------------------------------
# Tests: end to end with real RS256 signatures (JWKS stubbed, no network)
# ---------------------------------------------------------------------------


class TestSignedTokens:
    """Handler outcome for genuinely signed tokens from the same user pool."""

    def test_id_token_for_our_client_is_allowed(self, rsa_key, jwks):
        token = _sign(rsa_key, _id_token_payload(sub="user-id-1"))
        result = ws_authorizer.lambda_handler(_connect_event(token=token), None)
        assert _effect(result) == "Allow"
        assert result["principalId"] == "user-id-1"

    def test_access_token_for_our_client_is_allowed(self, rsa_key, jwks):
        token = _sign(rsa_key, _valid_payload(sub="user-access-1"))
        result = ws_authorizer.lambda_handler(_connect_event(token=token), None)
        assert _effect(result) == "Allow"
        assert result["principalId"] == "user-access-1"

    def test_id_token_for_another_client_in_the_same_pool_is_denied(self, rsa_key, jwks):
        """The R-05 finding: same pool, same key, valid signature, wrong app client."""
        token = _sign(rsa_key, _id_token_payload(aud=OTHER_CLIENT_ID))
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Deny"

    def test_access_token_for_another_client_in_the_same_pool_is_denied(self, rsa_key, jwks):
        token = _sign(rsa_key, _valid_payload(client_id=OTHER_CLIENT_ID))
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Deny"

    def test_second_id_in_env_list_is_allowed(self, rsa_key, jwks, monkeypatch):
        monkeypatch.setenv("USER_POOL_CLIENT_IDS", f"{TEST_USER_POOL_CLIENT_ID},{SECOND_CLIENT_ID}")
        token = _sign(rsa_key, _id_token_payload(aud=SECOND_CLIENT_ID))
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Allow"

    def test_tampered_payload_is_denied(self, rsa_key, jwks):
        """Swap in a payload for an allowed client under another client's signature."""
        good = _sign(rsa_key, _id_token_payload(aud=OTHER_CLIENT_ID))
        header, _, signature = good.split(".")
        forged_payload = _b64url(json.dumps(_id_token_payload()).encode())
        forged = f"{header}.{forged_payload}.{signature}"
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=forged), None)) == "Deny"

    def test_zero_padded_signature_is_denied(self, rsa_key, jwks):
        """A leading zero byte leaves the integer unchanged but breaks the k-octet length rule."""
        good = _sign(rsa_key, _id_token_payload())
        header, payload, signature = good.split(".")
        padded = _b64url(b"\x00" + ws_authorizer._base64url_decode(signature))
        token = f"{header}.{payload}.{padded}"
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Deny"

    def test_unknown_kid_on_a_cold_container_is_denied_with_one_fetch(self, rsa_key, jwks):
        """S8: the JWKS loaded for this very token is current; it is not fetched again.

        This used to assert two fetches (the load plus an immediate refresh),
        which is the cold-container half of the S8 finding.
        """
        token = _sign(rsa_key, _id_token_payload(), kid="not-in-jwks")
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Deny"
        assert len(jwks) == 1


class TestFailClosedConfiguration:
    """No allowed app client configured means every connection is denied."""

    @pytest.mark.parametrize("raw", [None, "", "  ", ",,"])
    def test_correct_signed_token_is_denied_without_client_ids(self, rsa_key, jwks, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv("USER_POOL_CLIENT_IDS", raising=False)
        else:
            monkeypatch.setenv("USER_POOL_CLIENT_IDS", raw)
        token = _sign(rsa_key, _id_token_payload())
        with (
            patch.object(ws_authorizer.logger, "error") as log_error,
            patch.object(ws_authorizer, "_verify_token", wraps=ws_authorizer._verify_token) as verify,
        ):
            result = ws_authorizer.lambda_handler(_connect_event(token=token), None)
        assert _effect(result) == "Deny"
        assert result["principalId"] == "user"
        # Denied on configuration, before the token is even looked at.
        verify.assert_not_called()
        log_error.assert_called_once()
        assert "USER_POOL_CLIENT_IDS" in log_error.call_args.args
        assert "Configuration error" in log_error.call_args.args[0]

    def test_legacy_singular_var_does_not_enable_access(self, rsa_key, jwks, monkeypatch):
        monkeypatch.delenv("USER_POOL_CLIENT_IDS", raising=False)
        monkeypatch.setenv("USER_POOL_CLIENT_ID", TEST_USER_POOL_CLIENT_ID)
        token = _sign(rsa_key, _id_token_payload())
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Deny"


# ---------------------------------------------------------------------------
# Tests: error paths (malformed input, JWKS fetch failure)
# ---------------------------------------------------------------------------
# This directory has no test_error_paths.py; adding one would be a new test file,
# so the error-path cases live here.


class TestErrorPaths:
    """Malformed tokens and JWKS failures deny rather than raise."""

    @pytest.mark.parametrize(
        "token",
        [
            "a.b",  # too few parts
            "a.b.c.d",  # too many parts
            "!!!.???.***",  # not base64url
            f"{_b64url(b'not json')}.{_b64url(b'{}')}.sig",  # header not JSON
        ],
    )
    def test_malformed_token_is_denied(self, token):
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Deny"

    def test_payload_that_is_a_json_array_is_denied(self):
        token = f"{_b64url(json.dumps({'kid': TEST_KID, 'alg': 'RS256'}).encode())}.{_b64url(b'[1, 2]')}.sig"
        with pytest.raises(ValueError, match="JSON objects"):
            ws_authorizer._verify_token(token, ALLOWED)
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Deny"

    def test_jwks_fetch_failure_is_denied(self, rsa_key, monkeypatch):
        def _unreachable():
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(ws_authorizer, "_fetch_jwks", _unreachable)
        token = _sign(rsa_key, _id_token_payload())
        assert _effect(ws_authorizer.lambda_handler(_connect_event(token=token), None)) == "Deny"


# ---------------------------------------------------------------------------
# Tests: unknown `kid` refresh interval and negative cache (security finding S8)
# ---------------------------------------------------------------------------
# The claims checked before the key lookup can be forged from public values, so
# any caller reaches the `kid` lookup. An unknown `kid` used to refetch the JWKS
# on every request. Required now:
#   (a) an unknown `kid` refreshes the JWKS at most once per interval; inside
#       it the `kid` is refused from the cache with no fetch;
#   (b) refused `kid`s are remembered in a bounded cache whose entries live no
#       longer than the interval, so a repeated bogus `kid` costs nothing;
#   a rotated key is still found on the first refresh after the interval, and
#   every failure denies.

BOGUS_KID = "bogus-kid"
ROTATED_KID = "rotated-kid-2"
INTERVAL = ws_authorizer.JWKS_REFRESH_MIN_INTERVAL_SECONDS


@pytest.fixture(scope="module")
def rotated_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def clock(monkeypatch):
    """The monotonic clock, advanced by hand. Index 0 is "now"."""
    now = [50_000.0]
    monkeypatch.setattr(ws_authorizer.time, "monotonic", lambda: now[0])
    return now


class _JwksEndpoint:
    """Stands in for the user pool's JWKS URL: serves `keys`, counts every fetch, can fail."""

    def __init__(self, keys: list[dict]):
        self.keys = keys
        self.fetches = 0
        self.fail = False

    def __call__(self) -> dict:
        self.fetches += 1
        if self.fail:
            raise urllib.error.URLError("timed out")
        return {"keys": list(self.keys)}


@pytest.fixture
def endpoint(monkeypatch, rsa_key):
    ep = _JwksEndpoint(_jwks_for(rsa_key)["keys"])
    monkeypatch.setattr(ws_authorizer, "_fetch_jwks", ep)
    return ep


def _connect(token: str) -> dict:
    return ws_authorizer.lambda_handler(_connect_event(token=token), None)


def _assert_denied(result: dict) -> None:
    assert _effect(result) == "Deny"
    assert result["principalId"] == "user"


class TestUnknownKidLimits:
    def test_bounds_are_the_ones_the_brief_asks_for(self):
        assert INTERVAL > 0
        assert 0 < ws_authorizer.UNKNOWN_KID_CACHE_SECONDS <= INTERVAL
        assert isinstance(ws_authorizer.UNKNOWN_KID_CACHE_MAX_ENTRIES, int)
        assert ws_authorizer.UNKNOWN_KID_CACHE_MAX_ENTRIES > 0

    def test_unknown_kid_twice_within_the_interval_costs_exactly_one_fetch_after_the_initial_one(
        self, rsa_key, endpoint, clock
    ):
        """Warm container whose JWKS is older than the interval."""
        assert _effect(_connect(_sign(rsa_key, _id_token_payload()))) == "Allow"
        assert endpoint.fetches == 1  # the initial load

        clock[0] += INTERVAL + 1
        bogus = _sign(rsa_key, _id_token_payload(), kid=BOGUS_KID)
        _assert_denied(_connect(bogus))
        clock[0] += 1
        _assert_denied(_connect(bogus))

        assert endpoint.fetches - 1 == 1

    def test_distinct_unknown_kids_within_the_interval_cost_no_further_fetch(self, rsa_key, endpoint, clock):
        """Varying the `kid` does not get round the interval."""
        _connect(_sign(rsa_key, _id_token_payload()))
        clock[0] += INTERVAL + 1
        for i in range(300):
            _assert_denied(_connect(_sign(rsa_key, _id_token_payload(), kid=f"bogus-{i}")))
            clock[0] += 0.5  # 150 s in all, still inside the interval
        assert endpoint.fetches == 2

    def test_unknown_kid_right_after_the_first_load_is_not_refetched(self, rsa_key, endpoint, clock):
        _assert_denied(_connect(_sign(rsa_key, _id_token_payload(), kid=BOGUS_KID)))
        clock[0] += INTERVAL - 1
        _assert_denied(_connect(_sign(rsa_key, _id_token_payload(), kid="another-bogus-kid")))
        assert endpoint.fetches == 1

    def test_a_still_unknown_kid_refreshes_once_per_interval(self, rsa_key, endpoint, clock):
        bogus = _sign(rsa_key, _id_token_payload(), kid=BOGUS_KID)
        _connect(bogus)  # initial load
        clock[0] += INTERVAL
        _connect(bogus)
        assert endpoint.fetches == 2
        clock[0] += INTERVAL - 1
        _connect(bogus)
        assert endpoint.fetches == 2
        clock[0] += 1
        _assert_denied(_connect(bogus))
        assert endpoint.fetches == 3

    def test_a_rotated_key_is_found_on_the_first_refresh_after_the_interval(
        self, rsa_key, rotated_rsa_key, endpoint, clock
    ):
        start = clock[0]
        assert _effect(_connect(_sign(rsa_key, _id_token_payload()))) == "Allow"

        # Cognito publishes the new key; this container's JWKS predates it.
        endpoint.keys = endpoint.keys + _jwks_for(rotated_rsa_key, kid=ROTATED_KID)["keys"]
        rotated = _sign(rotated_rsa_key, _id_token_payload(sub="rotated-user"), kid=ROTATED_KID)

        # Inside the interval it is refused from the cache, which also puts it
        # in the negative cache, late in the window.
        clock[0] = start + INTERVAL - 1
        _assert_denied(_connect(rotated))
        assert endpoint.fetches == 1

        # The first request after the interval refreshes and finds it; the
        # negative cache entry does not hold it back.
        clock[0] = start + INTERVAL
        result = _connect(rotated)
        assert _effect(result) == "Allow"
        assert result["principalId"] == "rotated-user"
        assert endpoint.fetches == 2

        # The old key is still accepted, and neither costs another fetch.
        assert _effect(_connect(_sign(rsa_key, _id_token_payload()))) == "Allow"
        assert _effect(_connect(rotated)) == "Allow"
        assert endpoint.fetches == 2

    def test_a_repeated_bogus_kid_is_refused_without_a_key_scan_or_a_fetch(self, rsa_key, endpoint, clock):
        _connect(_sign(rsa_key, _id_token_payload()))
        clock[0] += INTERVAL + 1
        bogus = _sign(rsa_key, _id_token_payload(), kid=BOGUS_KID)
        _connect(bogus)
        assert BOGUS_KID in ws_authorizer._unknown_kids
        fetches = endpoint.fetches

        with patch.object(ws_authorizer, "_find_key", wraps=ws_authorizer._find_key) as find_key:
            for _ in range(50):
                clock[0] += 1
                _assert_denied(_connect(bogus))
        find_key.assert_not_called()
        assert endpoint.fetches == fetches

    def test_the_negative_cache_is_bounded_and_evicts_the_oldest(self, rsa_key, endpoint, clock):
        bound = ws_authorizer.UNKNOWN_KID_CACHE_MAX_ENTRIES
        _connect(_sign(rsa_key, _id_token_payload()))  # load the JWKS
        kids = [f"bogus-{i}" for i in range(bound + 40)]
        for kid in kids:
            with pytest.raises(ValueError, match="Unable to find signing key"):
                ws_authorizer._get_signing_key(kid)
        assert len(ws_authorizer._unknown_kids) == bound
        assert list(ws_authorizer._unknown_kids) == kids[-bound:]
        assert endpoint.fetches == 1

    def test_a_negative_entry_never_outlives_the_interval_or_its_ttl(self, rsa_key, endpoint, clock):
        _connect(_sign(rsa_key, _id_token_payload()))
        loaded_at = clock[0]
        clock[0] += INTERVAL / 2
        refused_at = clock[0]
        with pytest.raises(ValueError):
            ws_authorizer._get_signing_key(BOGUS_KID)
        expires_at = ws_authorizer._unknown_kids[BOGUS_KID]
        assert expires_at <= loaded_at + INTERVAL
        assert expires_at <= refused_at + ws_authorizer.UNKNOWN_KID_CACHE_SECONDS

    def test_a_fetch_clears_the_negative_cache(self, rsa_key, endpoint, clock):
        _connect(_sign(rsa_key, _id_token_payload()))
        for kid in ("old-bogus-1", "old-bogus-2"):
            with pytest.raises(ValueError):
                ws_authorizer._get_signing_key(kid)
        clock[0] += INTERVAL + 1
        with pytest.raises(ValueError):
            ws_authorizer._get_signing_key(BOGUS_KID)  # triggers the refresh
        assert endpoint.fetches == 2
        assert list(ws_authorizer._unknown_kids) == [BOGUS_KID]

    def test_a_failed_refresh_denies_keeps_the_old_keys_and_waits_out_the_interval(self, rsa_key, endpoint, clock):
        good = _sign(rsa_key, _id_token_payload())
        bogus = _sign(rsa_key, _id_token_payload(), kid=BOGUS_KID)
        assert _effect(_connect(good)) == "Allow"

        clock[0] += INTERVAL + 1
        endpoint.fail = True
        _assert_denied(_connect(bogus))
        assert endpoint.fetches == 2

        # The keys cached before the failure keep working, without a fetch.
        assert _effect(_connect(good)) == "Allow"
        # A failing endpoint is not retried inside the interval.
        clock[0] += 1
        _assert_denied(_connect(_sign(rsa_key, _id_token_payload(), kid="another-bogus-kid")))
        assert endpoint.fetches == 2

        endpoint.fail = False
        clock[0] += INTERVAL
        _assert_denied(_connect(bogus))
        assert endpoint.fetches == 3

    def test_a_failed_first_load_denies_and_is_retried_by_the_next_request(self, rsa_key, endpoint, clock):
        """With no keys cached nothing can be verified, so the first load is not rate limited."""
        good = _sign(rsa_key, _id_token_payload())
        endpoint.fail = True
        _assert_denied(_connect(good))
        endpoint.fail = False
        assert _effect(_connect(good)) == "Allow"
        assert endpoint.fetches == 2

    def test_every_unknown_kid_path_denies(self, rsa_key, endpoint, clock):
        """Each refusal path, confirmed by its reason, ends in Deny for principal "user"."""
        bogus = _sign(rsa_key, _id_token_payload(), kid=BOGUS_KID)
        reasons = []

        def _deny_with_reason():
            with patch.object(ws_authorizer.logger, "warning") as log_warning:
                _assert_denied(_connect(bogus))
            log_warning.assert_called_once()
            reasons.append(str(log_warning.call_args.args[1]))

        _deny_with_reason()  # cold: the JWKS just loaded is current
        _deny_with_reason()  # remembered from the first refusal
        clock[0] += INTERVAL
        _deny_with_reason()  # refreshed, still unknown
        clock[0] += INTERVAL
        endpoint.fail = True
        _deny_with_reason()  # refresh failed

        assert "fetched too recently" in reasons[0]
        assert "recently refused" in reasons[1]
        assert reasons[2] == f"Unable to find signing key for kid: {BOGUS_KID}"
        assert "timed out" in reasons[3]
        assert endpoint.fetches == 3
