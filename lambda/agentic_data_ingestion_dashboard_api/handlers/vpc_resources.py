# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""VPC, subnet, and security group resource endpoints.

Backs the dashboard's manual JDBC connection mode (P20). The wizard's
``ManualConnectionForm`` calls these endpoints to populate VPC, subnet,
and security-group dropdowns scoped to the AWS account.

Endpoints exposed:
    * ``GET /api/resources/vpc/list`` — :func:`list_vpcs`
    * ``GET /api/resources/vpc/{vpc_id}/subnets`` — :func:`list_subnets`
    * ``GET /api/resources/vpc/{vpc_id}/security-groups`` —
      :func:`list_security_groups`

All three endpoints are read-only (idempotent) and back onto EC2's
``Describe*`` APIs. The path parameter ``vpc_id`` is regex-validated
against ``^vpc-[a-f0-9]{8,17}$`` BEFORE any AWS call so a malformed
client-supplied ID surfaces an HTTP 400 immediately rather than an
opaque EC2 ``InvalidVpcID.NotFound``.

Validates: P20 Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.8
"""

from __future__ import annotations

import re
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# `create_response` is the shared HTTP-response builder, re-exported by
# `.common` from `shared/response_utils.py` (P21 Req 5.2). The local `_ok`/
# `_err` helpers below delegate to it so VPC responses use the canonical
# Content-Type + CORS headers instead of a hand-rolled dict.
from handlers.common import create_response, logger

# ---------------------------------------------------------------------------
# Module-level AWS client
# ---------------------------------------------------------------------------

ec2_client = boto3.client("ec2")

# ---------------------------------------------------------------------------
# Path-parameter validation (P20 Req 5.5)
# ---------------------------------------------------------------------------

# Match the canonical EC2 VPC ID shape. The 8–17 hex range covers both the
# legacy 8-character format and the modern 17-character format AWS rolled
# out account-wide. Anything else is rejected fail-closed before EC2 is
# called.
VPC_ID_RE = re.compile(r"^vpc-[a-f0-9]{8,17}$")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _name_tag(tags: list[dict[str, str]] | None) -> str:
    """Extract the value of the ``Name`` tag, or return ``""`` when absent.

    EC2 surfaces tags as a list of ``{Key, Value}`` dicts. This helper
    converts that to the wire shape's ``name`` field which the dashboard
    UI consumes for human-readable labels.
    """
    for tag in tags or []:
        if tag.get("Key") == "Name":
            return tag.get("Value", "")
    return ""


def _ok(body: dict[str, Any]) -> dict[str, Any]:
    """Return a 200 API Gateway response with the given JSON body.

    Delegates to the shared ``create_response`` builder (P21 Req 5.2) so
    the frontend's existing JSON-decoding fetch helpers work unchanged.
    """
    return create_response(200, body)


def _err(status: int, error: str, message: str = "") -> dict[str, Any]:
    """Return an error API Gateway response.

    The response body shape ``{error, message}`` matches
    ``handlers/rds_resources.py`` for consistency. Callers MUST log the
    underlying exception with ``exc_info=True`` before invoking this
    helper so the stack trace is recoverable from CloudWatch. Delegates
    to the shared ``create_response`` builder (P21 Req 5.2).
    """
    return create_response(status, {"error": error, "message": message})


def _validate_vpc_id(vpc_id: str | None) -> str | None:
    """Return ``None`` when ``vpc_id`` matches :data:`VPC_ID_RE`, else error string.

    Centralised so :func:`list_subnets` and :func:`list_security_groups`
    enforce the regex identically. The regex is defence-in-depth — the
    wizard already validates client-side — so any failure points at a
    misconfigured caller, not at user input.
    """
    if not vpc_id or not VPC_ID_RE.fullmatch(vpc_id):
        return "Invalid vpc_id format"
    return None


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------


def list_vpcs(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """List all VPCs available to the dashboard API role.

    GET /api/resources/vpc/list

    Returns:
        HTTP 200 with body ``{vpcs: [{vpc_id, name, is_default, cidr}],
        count: N}`` on success. Each VPC's ``name`` is its ``Name`` tag
        value, or empty string when no such tag exists.

    Raises:
        Returns HTTP 500 ``{error, message}`` when EC2 throws
        ``ClientError`` or ``BotoCoreError``. The exception is logged
        with ``exc_info=True`` for CloudWatch debugging.

    Validates: P20 Requirements 5.1, 5.6, 5.8
    """
    try:
        logger.info("Listing VPCs")
        response = ec2_client.describe_vpcs()
        vpcs = [
            {
                "vpc_id": vpc.get("VpcId", ""),
                "name": _name_tag(vpc.get("Tags")),
                "is_default": bool(vpc.get("IsDefault", False)),
                "cidr": vpc.get("CidrBlock", ""),
            }
            for vpc in response.get("Vpcs", [])
        ]
        logger.info("Found %d VPCs", len(vpcs))
        return _ok({"vpcs": vpcs, "count": len(vpcs)})
    except (ClientError, BotoCoreError) as exc:
        logger.error("Error listing VPCs: %s", exc, exc_info=True)
        return _err(500, "Failed to list VPCs", str(exc))


def list_subnets(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """List subnets in the given VPC.

    GET /api/resources/vpc/{vpc_id}/subnets

    Args:
        event: API Gateway proxy event. ``pathParameters.vpc_id`` is
            required and must match :data:`VPC_ID_RE`.
        context: Lambda context (unused).

    Returns:
        HTTP 200 with ``{subnets: [{subnet_id, name, availability_zone,
        cidr}], count: N}`` on success. HTTP 400 when ``vpc_id`` is
        missing or malformed (no EC2 call is made — fail-closed per
        Req 5.5). HTTP 500 on EC2 failure.

    Validates: P20 Requirements 5.2, 5.5, 5.6, 5.8
    """
    path_params = event.get("pathParameters") or {}
    vpc_id = path_params.get("vpc_id")

    err = _validate_vpc_id(vpc_id)
    if err is not None:
        logger.warning("Rejecting list_subnets request with vpc_id=%r", vpc_id)
        return _err(400, err)

    try:
        logger.info("Listing subnets in VPC %s", vpc_id)
        response = ec2_client.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        subnets = [
            {
                "subnet_id": subnet.get("SubnetId", ""),
                "name": _name_tag(subnet.get("Tags")),
                "availability_zone": subnet.get("AvailabilityZone", ""),
                "cidr": subnet.get("CidrBlock", ""),
            }
            for subnet in response.get("Subnets", [])
        ]
        logger.info("Found %d subnets in VPC %s", len(subnets), vpc_id)
        return _ok({"subnets": subnets, "count": len(subnets)})
    except (ClientError, BotoCoreError) as exc:
        logger.error("Error listing subnets in VPC %s: %s", vpc_id, exc, exc_info=True)
        return _err(500, "Failed to list subnets", str(exc))


def list_security_groups(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """List security groups in the given VPC.

    GET /api/resources/vpc/{vpc_id}/security-groups

    Args:
        event: API Gateway proxy event. ``pathParameters.vpc_id`` is
            required and must match :data:`VPC_ID_RE`.
        context: Lambda context (unused).

    Returns:
        HTTP 200 with ``{security_groups: [{sg_id, name, description}],
        count: N}`` on success. HTTP 400 when ``vpc_id`` is missing or
        malformed. HTTP 500 on EC2 failure.

    Validates: P20 Requirements 5.3, 5.5, 5.6, 5.8
    """
    path_params = event.get("pathParameters") or {}
    vpc_id = path_params.get("vpc_id")

    err = _validate_vpc_id(vpc_id)
    if err is not None:
        logger.warning("Rejecting list_security_groups request with vpc_id=%r", vpc_id)
        return _err(400, err)

    try:
        logger.info("Listing security groups in VPC %s", vpc_id)
        response = ec2_client.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
        sgs = [
            {
                "sg_id": sg.get("GroupId", ""),
                "name": sg.get("GroupName", ""),
                "description": sg.get("Description", ""),
            }
            for sg in response.get("SecurityGroups", [])
        ]
        logger.info("Found %d security groups in VPC %s", len(sgs), vpc_id)
        return _ok({"security_groups": sgs, "count": len(sgs)})
    except (ClientError, BotoCoreError) as exc:
        logger.error("Error listing security groups in VPC %s: %s", vpc_id, exc, exc_info=True)
        return _err(500, "Failed to list security groups", str(exc))
