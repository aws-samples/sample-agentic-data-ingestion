# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
WebSocket $connect Authorizer Lambda.

Validates a Cognito JWT token passed as the `token` query-string parameter on
the WebSocket handshake. Returns an IAM policy document (Allow/Deny) per the
API Gateway WebSocket authorizer contract.

Environment variables:
  USER_POOL_ID          - Cognito User Pool ID (e.g. us-west-2_abc123)
  USER_POOL_CLIENT_IDS  - Comma-separated Cognito app client IDs whose tokens
                          are accepted. Whitespace around each ID is stripped
                          and empty entries are ignored.
  USER_POOL_CLIENT_ID_PARAMETERS
                        - Comma-separated names of SSM String parameters, each
                          holding more client IDs in the same format. The
                          dashboard's app client is created in the Hosting
                          stack, which deploys after the Compute stack that owns
                          this function, so its ID cannot be an environment
                          variable here without a stack cycle; Hosting writes it
                          to a parameter instead. The Auth stack does the same
                          for its optional e2e client. A parameter that does not
                          exist contributes nothing. Read at run time, cached.
  AWS_REGION            - AWS region (set automatically by Lambda runtime)

The accepted clients are the union of the two sources. If that union is empty
(nothing set, or every parameter missing or unreadable), every connection is
denied.

Why the verifier below is hand-written rather than PyJWT
--------------------------------------------------------
PyJWT[crypto] is importable in this function today only by accident: `mcp`
depends on it, and `mcp` reaches this function through StrandsAgentsLayer.
Nothing declares it for this function, and the planned base/strands layer
split (`LGR-bb8073d-1`) would move this function onto the shared base layer,
which drops `mcp` and its transitive closure (`AGENT_ONLY_LAYER_PACKAGES` in
deploy-cdk/lib/compute-stack.ts). `import jwt` would then be an ImportError at
cold start that no unit test can see, because the test venv has PyJWT. PyJWT's
`audience=` check also does not fit Cognito access tokens, which carry the app
client in `client_id` rather than `aud`, so the client check would stay custom
either way. So the stdlib verifier stays.
"""

import json
import os
import sys
import time
import urllib.request
from collections import OrderedDict
from typing import Any, TypeGuard

import boto3

sys.path.append("/opt/python")
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# ── Configuration ──────────────────────────────────────────────────────────
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# Read per invocation (see _allowed_client_ids), not frozen at import.
CLIENT_IDS_ENV_VAR = "USER_POOL_CLIENT_IDS"
CLIENT_ID_PARAMETERS_ENV_VAR = "USER_POOL_CLIENT_ID_PARAMETERS"

# How long a parameter read (or a failed read) is reused. Short enough that a
# Hosting redeploy that replaces the dashboard client takes effect within
# minutes; long enough that $connect does not cost an SSM call each time.
PARAMETER_CACHE_SECONDS = 300.0
_parameter_cache: tuple[tuple[str, ...], float, frozenset[str]] | None = None
_ssm_client: Any = None

# The only algorithm Cognito user pools sign with. Anything else is rejected,
# including a header with no `alg` at all.
EXPECTED_ALG = "RS256"

# Accepted `token_use` values, mapped to the claim that names the app client on
# that kind of Cognito token. Any `token_use` not listed here is rejected.
_CLIENT_CLAIM_BY_USE = {"id": "aud", "access": "client_id"}

# Construct issuer and JWKS URL from the User Pool ID
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"

# Module-level cache for JWKS keys (persisted across warm Lambda invocations)
_jwks_cache: dict[str, Any] | None = None
# time.monotonic() of the last JWKS fetch attempt: the first load or a refresh.
_jwks_fetched_at: float | None = None

# ── Unknown `kid` limits ───────────────────────────────────────────────────
# A token whose `kid` is not in the cached JWKS used to refetch the JWKS on
# every request. That lookup comes after the claim checks, but every claim it
# follows can be forged from public values (the issuer is built from the pool
# ID; the client ID is in the dashboard's runtime config), so an
# unauthenticated caller reaches it with any `kid`, and the WebSocket edge has
# no WAF. Two limits bound it:
#
# - Refresh interval. An unknown `kid` refreshes the JWKS only when the last
#   fetch is at least JWKS_REFRESH_MIN_INTERVAL_SECONDS old; a younger JWKS is
#   taken as current and the `kid` is refused without a fetch. The first load
#   counts as a fetch, so a cold container does not fetch twice for one token.
#   A failed refresh counts too, so a slow or failing endpoint is also tried at
#   most once per interval, and the keys already cached stay in use.
# - Negative cache. A refused `kid` is remembered, so repeating it costs one
#   dict lookup. At most UNKNOWN_KID_CACHE_MAX_ENTRIES are kept, oldest evicted
#   first, each for at most UNKNOWN_KID_CACHE_SECONDS and never past the end of
#   the current refresh interval; every fetch clears it.
#
# Key rotation: a `kid` published at least one interval before its first use
# is always found, because by then either the cached JWKS has it or the cache
# is old enough to refresh. The negative cache cannot delay that: its entries
# end with the interval, so the first request after the interval refreshes.
JWKS_REFRESH_MIN_INTERVAL_SECONDS = 300.0
UNKNOWN_KID_CACHE_SECONDS = 300.0  # must not exceed JWKS_REFRESH_MIN_INTERVAL_SECONDS
UNKNOWN_KID_CACHE_MAX_ENTRIES = 128
# kid -> time.monotonic() after which it is no longer refused from this cache.
_unknown_kids: OrderedDict[str, float] = OrderedDict()


def lambda_handler(event: dict, context: Any) -> dict:
    """Authorize WebSocket $connect requests using a Cognito JWT."""
    logger.append_keys(step_name="WSAuthorizer")

    # Fail closed on missing configuration: with no allowed app client there is
    # no token this authorizer may accept, so deny before looking at the token.
    allowed_client_ids = _allowed_client_ids()
    if not allowed_client_ids:
        logger.error(
            "Configuration error: no app client is allowed (%s is empty and no parameter "
            "named in %s exists with a value); denying every WebSocket connection. "
            "Deploy the Hosting stack, which writes the dashboard client ID, or set %s.",
            CLIENT_IDS_ENV_VAR,
            CLIENT_ID_PARAMETERS_ENV_VAR,
            CLIENT_IDS_ENV_VAR,
        )
        return _generate_policy("user", "Deny", event["methodArn"])

    # Extract token from query string
    token = _extract_token(event)
    if not token:
        logger.warning("No token provided in query string")
        return _generate_policy("user", "Deny", event["methodArn"])

    try:
        claims = _verify_token(token, allowed_client_ids)
        principal_id = claims.get("sub", "user")
        logger.info("Authorized connection for sub=%s", principal_id)
        return _generate_policy(principal_id, "Allow", event["methodArn"])
    except Exception as e:
        logger.warning("Token verification failed: %s", str(e))
        return _generate_policy("user", "Deny", event["methodArn"])


def _parse_client_ids(raw: str) -> frozenset[str]:
    """Comma-separated IDs, whitespace stripped, empty entries dropped: "a, b,," -> {"a", "b"}."""
    return frozenset(entry.strip() for entry in raw.split(",") if entry.strip())


def _allowed_client_ids() -> frozenset[str]:
    """The accepted app client IDs: USER_POOL_CLIENT_IDS plus the SSM parameters'.

    An empty result is treated by the handler as "deny everything".

    The old singular USER_POOL_CLIENT_ID is deliberately ignored: it made the
    client check optional, and a fallback would keep a third source for the
    same setting.
    """
    return _parse_client_ids(os.environ.get(CLIENT_IDS_ENV_VAR, "")) | _parameter_client_ids()


def _parameter_client_ids() -> frozenset[str]:
    """Client IDs from the SSM parameters named by USER_POOL_CLIENT_ID_PARAMETERS.

    One GetParameters call for all of them. A name that does not exist comes
    back in InvalidParameters and contributes nothing: that is the normal state
    before Hosting is deployed, and for the e2e parameter whenever the e2e
    client is not. Cached for PARAMETER_CACHE_SECONDS, failures included, so a
    missing parameter costs one SSM call per window rather than one per
    $connect. A read error contributes nothing, which fails closed; it is
    logged, not raised, so a static ID in USER_POOL_CLIENT_IDS keeps working.
    """
    global _parameter_cache, _ssm_client

    names = tuple(sorted(_parse_client_ids(os.environ.get(CLIENT_ID_PARAMETERS_ENV_VAR, ""))))
    if not names:
        return frozenset()

    now = time.monotonic()
    if _parameter_cache is not None:
        cached_names, read_at, cached_ids = _parameter_cache
        if cached_names == names and now - read_at < PARAMETER_CACHE_SECONDS:
            return cached_ids

    ids: frozenset[str] = frozenset()
    try:
        if _ssm_client is None:
            _ssm_client = boto3.client("ssm")
        response = _ssm_client.get_parameters(Names=list(names))
        for parameter in response.get("Parameters", []):
            value = parameter.get("Value")
            ids |= _parse_client_ids(value if isinstance(value, str) else "")
        missing = response.get("InvalidParameters", [])
        if missing:
            logger.info("App client ID parameters not present (yet): %s", ", ".join(missing))
    except Exception as e:
        logger.warning("Could not read app client IDs from SSM parameters %s: %s", ", ".join(names), str(e))
        ids = frozenset()

    _parameter_cache = (names, now, ids)
    return ids


def _extract_token(event: dict) -> str | None:
    """Extract the token from the query string parameters."""
    # API Gateway WebSocket authorizer puts query strings in different places
    # depending on the integration version
    query_params = event.get("queryStringParameters") or {}
    token = query_params.get("token")
    if token:
        return token

    # Fallback: check headers (some clients may pass via Sec-WebSocket-Protocol)
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:]

    return None


def _verify_token(token: str, allowed_client_ids: frozenset[str]) -> dict:
    """
    Verify a Cognito JWT token.

    Validates:
    - Token structure (3 parts, valid base64, JSON objects)
    - Header `alg` is exactly RS256 and `kid` is present
    - Issuer matches the User Pool
    - Token is not expired, and not before its `nbf` if it carries one
    - Token use is 'id' or 'access', and the token was issued to an allowed
      app client (`aud` for an ID token, `client_id` for an access token)
    - Signature against the JWKS key named by `kid`

    Claims are checked before the signature only to fail fast; nothing is
    returned until the signature has also verified.
    """

    # Decode token parts without verification first to get the header
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid token format: expected 3 parts")

    header = _decode_jwt_part(parts[0])
    payload = _decode_jwt_part(parts[1])
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise ValueError("Invalid token format: header and payload must be JSON objects")

    # Validate basic claims before signature (fail fast)
    _validate_claims(payload, allowed_client_ids)

    # Verify signature. A missing `alg` is rejected rather than assumed RS256.
    kid = header.get("kid")
    alg = header.get("alg")
    if alg != EXPECTED_ALG:
        raise ValueError(f"Unsupported algorithm: {alg}")
    if not kid:
        raise ValueError("Token header missing 'kid'")

    jwk = _get_signing_key(kid)
    _verify_rs256_signature(token, jwk)

    return payload


def _is_numeric_date(value: Any) -> TypeGuard[int | float]:
    """True for a JWT NumericDate (int or float). bool is excluded on purpose."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_claims(payload: dict, allowed_client_ids: frozenset[str]) -> None:
    """Validate token claims (issuer, expiry, not-before, token_use, app client)."""
    # Check issuer
    if payload.get("iss") != ISSUER:
        raise ValueError(f"Invalid issuer: {payload.get('iss')}")

    now = time.time()

    # Check expiration. RFC 7519 §4.1.4: reject on or after `exp`.
    exp = payload.get("exp")
    if not _is_numeric_date(exp) or now >= exp:
        raise ValueError("Token is expired")

    # Check not-before. Cognito does not normally set it; honour it when present.
    if "nbf" in payload:
        nbf = payload["nbf"]
        if not _is_numeric_date(nbf) or now < nbf:
            raise ValueError("Token is not yet valid (nbf)")

    # Check the token was issued to one of our app clients. Cognito puts the
    # client in `aud` on an ID token and in `client_id` on an access token, so
    # the claim to read depends on `token_use`. Any other `token_use` is
    # rejected. A non-string claim (for example a JSON array `aud`) is rejected
    # too: Cognito always issues a string. An empty allowed set matches nothing,
    # so this check also fails closed if a caller ever skips the handler's
    # configuration check.
    token_use = payload.get("token_use")
    client_claim = _CLIENT_CLAIM_BY_USE.get(token_use) if isinstance(token_use, str) else None
    if client_claim is None:
        raise ValueError(f"Invalid token_use: {token_use}")

    client = payload.get(client_claim)
    if not isinstance(client, str) or client not in allowed_client_ids:
        raise ValueError(f"Invalid {client_claim} in {token_use} token: not an allowed app client")


def _decode_jwt_part(part: str) -> dict:
    """Decode a base64url-encoded JWT part."""
    import base64

    # Add padding
    padding = 4 - len(part) % 4
    if padding != 4:
        part += "=" * padding

    decoded = base64.urlsafe_b64decode(part)
    return json.loads(decoded)


def _get_signing_key(kid: str) -> dict:
    """Return the JWK for `kid` from the cached JWKS, refreshing it at most once per interval.

    Raises ValueError when the `kid` is not found, and lets a fetch error
    propagate; the handler denies on either. See "Unknown `kid` limits" above.
    """
    global _jwks_cache, _jwks_fetched_at

    now = time.monotonic()

    if _jwks_cache is None:
        # First use in this container. Not rate limited: with no keys cached
        # nothing can be verified, so a failure here is retried by the next
        # request rather than denying every connection for an interval.
        _jwks_cache = _fetch_jwks()
        _jwks_fetched_at = now
        _unknown_kids.clear()

    if _is_recently_unknown(kid, now):
        raise ValueError(f"Unable to find signing key for kid: {kid} (recently refused)")

    key = _find_key(_jwks_cache, kid)
    if key is not None:
        return key

    if _jwks_fetched_at is not None and now - _jwks_fetched_at < JWKS_REFRESH_MIN_INTERVAL_SECONDS:
        _remember_unknown_kid(kid, now)
        raise ValueError(f"Unable to find signing key for kid: {kid} (JWKS fetched too recently to refetch)")

    # Key not found and the cache is old enough: refresh it (key rotation).
    # Stamped before the fetch so a failure also waits out the interval; on a
    # failure the old keys stay cached and the exception denies this token.
    _jwks_fetched_at = now
    _jwks_cache = _fetch_jwks()
    _unknown_kids.clear()

    key = _find_key(_jwks_cache, kid)
    if key is not None:
        return key

    _remember_unknown_kid(kid, now)
    raise ValueError(f"Unable to find signing key for kid: {kid}")


def _find_key(jwks: dict, kid: str) -> dict | None:
    """The key in `jwks` whose `kid` matches, or None."""
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def _is_recently_unknown(kid: str, now: float) -> bool:
    """True while `kid` is in the negative cache and its entry has not expired."""
    expires_at = _unknown_kids.get(kid)
    if expires_at is None:
        return False
    if now < expires_at:
        return True
    del _unknown_kids[kid]
    return False


def _remember_unknown_kid(kid: str, now: float) -> None:
    """Add `kid` to the negative cache, evicting the oldest entries past the bound.

    The entry ends at the earlier of UNKNOWN_KID_CACHE_SECONDS from now and the
    end of the current refresh interval, so it never stops a refresh that the
    interval would allow.
    """
    expires_at = now + UNKNOWN_KID_CACHE_SECONDS
    if _jwks_fetched_at is not None:
        expires_at = min(expires_at, _jwks_fetched_at + JWKS_REFRESH_MIN_INTERVAL_SECONDS)
    _unknown_kids.pop(kid, None)
    _unknown_kids[kid] = expires_at
    while len(_unknown_kids) > UNKNOWN_KID_CACHE_MAX_ENTRIES:
        _unknown_kids.popitem(last=False)


def _fetch_jwks() -> dict:
    """Fetch JWKS from the Cognito User Pool endpoint."""
    logger.info("Fetching JWKS from %s", JWKS_URL)
    req = urllib.request.Request(JWKS_URL)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _verify_rs256_signature(token: str, jwk: dict) -> None:
    """
    Verify RS256 signature using the JWK public key.

    Uses Python's built-in int arithmetic for RSA verification
    (no external cryptography library needed in Lambda).
    """
    import hashlib

    parts = token.split(".")
    message = f"{parts[0]}.{parts[1]}".encode()
    signature = _base64url_decode(parts[2])

    # Extract RSA public key components from JWK
    n = _base64url_decode_int(jwk["n"])
    e = _base64url_decode_int(jwk["e"])
    key_size = (n.bit_length() + 7) // 8

    # RFC 8017 §8.2.2 step 1: the signature must be exactly k octets and, as an
    # integer, less than the modulus. Without this, s + n or a zero-padded s
    # would verify as the same signature.
    sig_int = int.from_bytes(signature, "big")
    if len(signature) != key_size or sig_int >= n:
        raise ValueError("Invalid signature: wrong length for the signing key")

    # RSA verification: signature^e mod n
    decrypted = pow(sig_int, e, n)

    # Convert back to bytes (key size)
    decrypted_bytes = decrypted.to_bytes(key_size, "big")

    # PKCS#1 v1.5 verification: 0x00 0x01 [padding 0xFF...] 0x00 [DigestInfo + hash]
    # SHA-256 DigestInfo prefix (DER encoded)
    sha256_digest_info = bytes.fromhex("3031300d060960864801650304020105000420")

    # Compute expected hash
    expected_hash = hashlib.sha256(message).digest()
    expected_suffix = sha256_digest_info + expected_hash

    # Verify PKCS#1 v1.5 padding structure
    if decrypted_bytes[0:2] != b"\x00\x01":
        raise ValueError("Invalid signature: bad PKCS#1 v1.5 header")

    # Find the 0x00 separator after the padding
    separator_idx = decrypted_bytes.index(b"\x00", 2)
    padding_bytes = decrypted_bytes[2:separator_idx]

    # All padding bytes must be 0xFF
    if not all(b == 0xFF for b in padding_bytes):
        raise ValueError("Invalid signature: bad PKCS#1 v1.5 padding")

    # Compare DigestInfo + hash
    actual_suffix = decrypted_bytes[separator_idx + 1 :]
    if actual_suffix != expected_suffix:
        raise ValueError("Invalid signature: hash mismatch")


def _base64url_decode(data: str) -> bytes:
    """Decode base64url without padding."""
    import base64

    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data)


def _base64url_decode_int(data: str) -> int:
    """Decode a base64url-encoded big integer."""
    decoded = _base64url_decode(data)
    return int.from_bytes(decoded, "big")


def _generate_policy(principal_id: str, effect: str, method_arn: str) -> dict:
    """
    Generate an IAM policy document for the WebSocket authorizer.

    The policy allows or denies access to the WebSocket API based on
    the token validation result.
    """
    # Parse the method ARN to create a wildcard resource
    # Format: arn:aws:execute-api:region:account:api-id/stage/$connect
    arn_parts = method_arn.split(":")
    api_gateway_arn = ":".join(arn_parts[:5])
    api_id_stage = arn_parts[5].split("/")
    resource_arn = f"{api_gateway_arn}:{api_id_stage[0]}/{api_id_stage[1]}/*"

    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": resource_arn,
                }
            ],
        },
    }
