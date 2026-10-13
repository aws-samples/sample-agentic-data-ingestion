# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Audit logging utilities for fire-and-forget audit trail writes.

Provides ``write_audit_entry()`` to append immutable records to the
``agentic_data_ingestion_audit_log`` DynamoDB table, and ``extract_request_context()``
to pull actor/IP from API Gateway events.

Failures are logged but never propagated — callers are guaranteed that
audit writes will not affect their response.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import boto3
from aws_lambda_powertools import Logger

from env_utils import require_env

logger = Logger(service="agentic_data_ingestion")

_table_name: str = ""
_dynamodb = boto3.resource("dynamodb")
_audit_table: Any = None


def _get_audit_table() -> Any:
    """Lazy-init the audit table on first use, not at import time."""
    global _audit_table, _table_name
    if _audit_table is None:
        _table_name = require_env("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
        _audit_table = _dynamodb.Table(_table_name)
    return _audit_table


_TTL_SECONDS: int = 365 * 86_400  # 1 year


def write_audit_entry(
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
    actor: str = "system",
    ip_address: str = "",
) -> None:
    """Write a single audit entry to the audit log table.

    This is fire-and-forget: any exception is caught and logged so that
    the calling handler's response is never affected.
    """
    try:
        now = datetime.now(UTC)
        _get_audit_table().put_item(
            Item={
                "resource_id": resource_id,
                "timestamp": now.isoformat(),
                "actor": actor,
                "action": action,
                "resource_type": resource_type,
                "details": details or {},
                "ip_address": ip_address,
                "ttl": int(time.time()) + _TTL_SECONDS,
            }
        )
    except Exception:
        logger.exception(
            "Failed to write audit entry: action=%s resource=%s",
            action,
            resource_id,
        )


# Claim keys a Cognito **User Pool** authorizer forwards at
# ``requestContext.authorizer.claims``, in preference order: the stable opaque
# identifier first, the mutable ones after. The casing is verbatim from a real
# authorizer event captured off this deployment — ``cognito:username`` is all
# lowercase with a single literal colon and no underscore.
#
# On a pool configured ``UsernameAttributes: ["email"]``, ``cognito:username``
# is byte-identical to ``sub`` (Cognito generates a UUID as the real username),
# so the second preference is not a fallback to anything more readable. ``email``
# is the only human-meaningful claim, and it is deliberately last: R1.1 prefers a
# stable identifier over a mutable one for an immutable audit row.
_CLAIM_ACTOR_KEYS: tuple[str, ...] = ("sub", "cognito:username", "email")

# Retained fallback for IAM and Cognito **Identity Pool** callers, including any
# direct ``execute-api`` invocation. Both are empty under a User Pool authorizer,
# which is why the claims branch has to come first rather than after these.
_IDENTITY_ACTOR_KEYS: tuple[str, ...] = ("caller", "cognitoIdentityId")


def _first_non_empty_str(source: Any, keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value found under ``keys``, else ``""``.

    A non-dict ``source``, a missing key, a non-string value and an empty string
    all yield ``""`` rather than raising or resolving. Two reasons, both required:

    * ``extract_request_context`` must not raise for any input shape, and API
      Gateway is not the only thing that reaches it — a direct invocation can
      arrive with anything at all.
    * An empty claim is not a principal. Keying on presence (``"sub" in claims``)
      would resolve an actor of ``""``, skip the identity fallback entirely, and
      write a blank identity into an append-only table.
    """
    if not isinstance(source, dict):
        return ""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def extract_request_context(event: dict[str, Any]) -> dict[str, str]:
    """Extract actor and ip_address from an API Gateway event.

    Returns a dict with exactly ``actor`` and ``ip_address`` — the two keyword
    arguments ``write_audit_entry()`` takes. Every call site splats the result
    (``write_audit_entry(..., **ctx)``), so the two-key shape is a contract: a
    third key would be a ``TypeError`` at each of them, not a widened return.

    Actor resolution order:

    1. ``requestContext.authorizer.claims`` — ``sub``, then ``cognito:username``,
       then ``email``. This is where a Cognito **User Pool** authorizer puts the
       authenticated principal, and it is the shape every dashboard-driven
       request arrives in.
    2. ``requestContext.identity`` — ``caller``, then ``cognitoIdentityId``, which
       API Gateway populates for IAM and Cognito Identity Pool auth.
    3. The literal ``"unknown"``, which stays the terminal fallback.

    ``ip_address`` comes from ``identity.sourceIp``, which is populated under
    every authorizer type, and is read independently of the actor — an
    unresolvable actor does not suppress the IP.

    The actor is read only out of ``requestContext``. It is never taken from the
    request body, a header or the query string: those are client-supplied and
    unverifiable, so trusting them would make the audit trail assert something it
    has not checked.

    This never raises, for any input shape. It runs on the fire-and-forget audit
    path and must not be able to fail the request it is describing.
    """
    request_context = event.get("requestContext") if isinstance(event, dict) else None
    if not isinstance(request_context, dict):
        request_context = {}

    authorizer = request_context.get("authorizer")
    claims = authorizer.get("claims") if isinstance(authorizer, dict) else None
    identity = request_context.get("identity")

    actor: str = (
        _first_non_empty_str(claims, _CLAIM_ACTOR_KEYS)
        or _first_non_empty_str(identity, _IDENTITY_ACTOR_KEYS)
        or "unknown"
    )
    ip_address: str = _first_non_empty_str(identity, ("sourceIp",))

    return {"actor": actor, "ip_address": ip_address}


def write_agent_audit_entry(
    agent_type: str,
    action: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
    run_id: str = "",
    pipeline_id: str = "",
) -> None:
    """Write an audit entry for an AI agent decision.

    Delegates to ``write_audit_entry()`` with ``actor="agent:{agent_type}"``
    and enriches details with ``run_id`` and ``pipeline_id``.
    """
    enriched_details: dict[str, Any] = {
        **(details or {}),
        "run_id": run_id,
        "pipeline_id": pipeline_id,
    }
    write_audit_entry(
        action=action,
        resource_type=f"agent:{agent_type}",
        resource_id=resource_id,
        details=enriched_details,
        actor=f"agent:{agent_type}",
    )
