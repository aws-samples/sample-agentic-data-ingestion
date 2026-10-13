#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""JDBC schema-discovery endpoints for the dashboard API.

This module implements the wizard-time schema discovery flow for JDBC
sources (P17, Requirements 4.1-4.5, 4.8, 4.9, 15.6). The ``start`` and
``status`` endpoints live here; the cleanup janitor is added by Task
4.3, and router wiring is Task 4.5.

Flow (``select_rds`` mode):
    1. Validate the source identifiers (``jdbc_schema``, ``jdbc_table``)
       up-front via :func:`jdbc_helpers.validate_identifier`.
    2. Compute a deterministic 12-char hash of the connection identity
       so concurrent wizard sessions converge on the same crawler /
       connection rather than spawning duplicates.
    3. Pre-flight that the chosen RDS subnet has S3 connectivity via an
       S3 VPC Gateway Endpoint — without it the crawler cannot upload
       its temporary script artefacts and would fail with an opaque
       error in Glue. Failing fast here surfaces a clear remediation.
    4. Idempotently create the wizard-time Source_Connection
       (``agentic-data-ingestion-discover-{hash}``) with PhysicalConnectionRequirements
       so Glue routes the crawler through the customer's VPC.
    5. Idempotently create or update a Glue Crawler with a deterministic
       ``TablePrefix=agentic_data_ingestion_{hash}_`` (so :func:`glue:GetTables` can
       find the produced table later regardless of engine-specific name
       mangling) and the ``Type=agentic-data-ingestion-discovery`` tag (so the daily
       janitor in Task 4.3 can find resources to expire).
    6. Start the crawler and return HTTP 202 — the frontend polls the
       (Task 4.2) status endpoint to learn when results are ready.

Flow (``glue_connection`` mode):
    Same as above except step 4 (connection creation) is skipped — the
    user's existing Glue Connection is reused as-is. We do NOT tag the
    user's connection because it does not belong to
    AgenticDataIngestion's lifecycle.

    Step 3 (the S3-endpoint pre-flight) *does* run, via
    :func:`glue:GetConnection` →
    ``PhysicalConnectionRequirements.SubnetId`` → the same
    :func:`_check_s3_endpoint_exists`. It used to be skipped here
    because the request carries no ``subnet_id``, which meant a
    connection pointing at a subnet with no S3 route produced an opaque
    crawler failure where ``select_rds`` mode produced an actionable
    400. The pre-flight never blocks on its own unavailability: see
    :func:`_connection_s3_preflight` for the five verdicts and which two
    of them refuse.

Cleanup janitor (Task 4.3) — :func:`cleanup_discovery_resources` is
the daily TTL janitor invoked from the EventBridge ``aws.events``
dispatch wired up in Task 4.4. It paginates :func:`glue:GetCrawlers`,
filters to crawlers tagged ``Type=agentic-data-ingestion-discovery`` whose
``CreationTime`` is older than 7 days, and idempotently deletes the
crawler, the matching ``agentic-data-ingestion-discover-{hash}`` Glue Connection,
and any tables matching ``agentic_data_ingestion_{hash}_*`` in the discovery
database. Per-pipeline runtime crawlers (tagged
``Type=agentic-data-ingestion-runtime``) are explicitly skipped — their lifecycle
is bound to the pipeline, not the wizard session.

Future tasks (NOT scope of 4.3):
    * 4.4 — EventBridge dispatch wiring in ``lambda_function.py``
    * 4.5 — Router integration in ``router.py``
    * 4.6 — Unit tests for discovery endpoints
    * 4.7 — Unit tests for cleanup janitor
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ``jdbc_helpers`` ships in the Strands Lambda layer at /opt/python (which
# is on sys.path automatically inside the Lambda runtime), so no explicit
# path manipulation is needed for this import.
from jdbc_helpers import (
    create_glue_jdbc_connection,
    validate_identifier,
)

from .common import AWS_REGION, create_response, get_aws_account_id, handle_error, logger

# ---------------------------------------------------------------------------
# Module-level constants and clients
# ---------------------------------------------------------------------------

# The Glue Catalog database that holds all crawler outputs from the
# wizard-time AND runtime JDBC discovery flows. Provisioned by the
# Foundation Stack (Task 14.1). The ``{stage}`` segment matches the
# CDK-deployed stage name (``dev`` / ``staging`` / ``prod``).
#
# We use :func:`os.environ.get` rather than ``require_env`` here so that
# importing this module never raises at Lambda cold-start — the env var
# may not be wired into compute-stack.ts until Task 14 ships, and other
# handlers in the dashboard API must keep loading even if this module's
# config is incomplete. Calls into the start endpoint will surface a
# clean 500 on missing config rather than crashing the whole Lambda.
DISCOVERY_DB = f"agentic_data_ingestion_jdbc_discovery_{os.environ.get('AGENTIC_DATA_INGESTION_ENV', 'dev')}"

# The Glue role that the crawler executes as. Must already have the
# Glue + S3 + Secrets Manager permissions that gluePipelineRole carries.
# The dashboard API's infrastructureRole holds ``iam:PassRole`` on this
# ARN per Task 14.2.
GLUE_PIPELINE_ROLE_ARN = os.environ.get("GLUE_PIPELINE_ROLE_ARN", "")

# AWS clients — boto3 caches sessions internally so re-creating these at
# module load is cheap and matches the rds_resources.py / pipelines.py
# pattern in this directory.
glue_client = boto3.client("glue")
ec2_client = boto3.client("ec2")
rds_client = boto3.client("rds")

# Tags applied to AgenticDataIngestion-managed wizard-time discovery resources so
# the daily janitor can find them via ``glue:GetTags``. Runtime
# (per-pipeline) crawlers use ``Type=agentic-data-ingestion-runtime`` instead so the
# janitor never touches them.
_DISCOVERY_TAG_KEY = "Type"
_DISCOVERY_TAG_VALUE = "agentic-data-ingestion-discovery"


# ---------------------------------------------------------------------------
# Error detail redaction
# ---------------------------------------------------------------------------
#
# ``handle_error`` puts ``str(error)`` into a ``details`` field, and the frontend
# now surfaces that string to the operator (``api-client.ts``). Everything below
# exists so that widening what an operator can see does not widen what a
# credential can escape through.
#
# ``LGR-819980f-1`` is the standing invariant: JDBC credentials are only ever
# referenced by Secrets Manager ARN, and plaintext user/password never appear in
# Glue job arguments or ConnectionProperties. The happy path honours it; an error
# path that echoes a request back does not automatically. ``ParamValidationError``
# (a ``BotoCoreError``, so inside the ``except`` tuple at both crawler call
# sites) formats the offending request parameters into its message, so a
# malformed ConnectionProperties dict is exactly the shape that could carry a
# password into ``str(exc)``.

#: The trailing name segment of a Secrets Manager ARN. Region and account stay
#: visible deliberately — "which region is this secret in" is a live diagnostic
#: for the wrong-region connection case — and only the secret's own name goes.
_SECRET_ARN_NAME_RE = re.compile(r"(:secret:)[^\s\"',;)\]}]+")

#: ``KEY=value`` / ``"KEY": "value"`` pairs whose key is a Glue
#: ConnectionProperties credential key.
_SENSITIVE_KV_RE = re.compile(
    r"(?i)\b(PASSWORD|USERNAME|SECRET_ID|KAFKA_SASL_PLAIN_PASSWORD)\b"
    r"([\"']?\s*[:=]\s*)([\"']?)([^\s\"',;)\]}]*)"
)

#: SigV4 query parameters. A presigned URL that reaches an exception message
#: carries a usable credential in these three values.
_PRESIGN_PARAM_RE = re.compile(r"(?i)\b(X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)=([^\s&\"']*)")


def _aws_error_code(exc: BaseException) -> str:
    """Return the AWS error code carried by ``exc``, or ``""``.

    Read off ``exc.response`` rather than matched against
    ``glue_client.exceptions.*`` on purpose. Those typed classes are attributes
    of a *live* client, so any caller holding a stubbed client has no usable
    classes to compare against, and evaluating ``except <MagicMock>`` raises
    ``TypeError`` instead of classifying anything. A ``BotoCoreError`` carries no
    code at all and correctly falls through to ``""``. Code-based dispatch
    behaves identically for ``ClientError`` and for the generated subclasses Glue
    actually raises, because the subclasses *are* ``ClientError``\\ s.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""


def _redact_aws_detail(exc: BaseException) -> str:
    """Return ``str(exc)`` with credential-shaped substrings replaced.

    Three substitutions, each for a shape that has a documented route into a
    botocore exception message: the name segment of a Secrets Manager ARN, the
    value of a ConnectionProperties credential key, and the SigV4 signing
    parameters of a presigned URL. Everything else is passed through — the AWS
    error code and message are the whole point of surfacing this field.

    This is a defence-in-depth filter, not a licence: a caller must still not
    hand a credential to an exception. It is applied at the crawler call sites,
    which are the only paths whose ``details`` this module newly promotes to an
    operator-visible surface.
    """
    text = str(exc)
    text = _SECRET_ARN_NAME_RE.sub(r"\1***", text)
    text = _SENSITIVE_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***", text)
    return _PRESIGN_PARAM_RE.sub(r"\1=***", text)


# ---------------------------------------------------------------------------
# Crawler-creation failure classification
# ---------------------------------------------------------------------------
#
# ``handle_error`` returns 500 for everything (``shared/response_utils.py:94``).
# That is right for its 66 other call sites and is deliberately left alone here:
# changing its default would re-status every handler in the dashboard API at
# once. Instead the two crawler call sites classify locally, because three of the
# codes Glue can return at that point are distinguishable and two of them are
# not server faults at all.
#
# ``reason`` is emitted alongside ``error`` so a consumer can branch without
# matching on prose. The precedent is p35's schema-delete classifier, which reads
# ``body.reason`` before ``status`` for exactly that reason
# (``dashboard-react/src/utils/schema-delete-failure.ts``).

_REASON_ENTITY_NOT_FOUND = "discovery_entity_not_found"
_REASON_ACCESS_DENIED = "discovery_access_denied"
_REASON_INVALID_INPUT = "discovery_invalid_input"
_REASON_MISSING_CONFIG = "discovery_configuration_missing"
_REASON_UNEXPECTED = "discovery_crawler_error"

#: Glue codes meaning "the request itself was malformed". Glue's own name for
#: this is ``InvalidInputException``; ``ValidationException`` is the shape the
#: shared AWS SDK validation layer produces for the same class of fault.
_INVALID_INPUT_CODES = frozenset({"InvalidInputException", "ValidationException"})


def _crawler_failure_response(
    exc: BaseException,
    *,
    crawler_name: str,
    connection_name: str,
    glue_connection_mode: bool,
    preflight_note: str = "",
) -> dict[str, Any]:
    """Classify a crawler create/update failure into an actionable response.

    Four named outcomes and a default. The status code is chosen by asking one
    question — *can the caller fix this by changing the request?* — because that
    is what a 4xx claims:

    * ``EntityNotFoundException`` in ``glue_connection`` mode → **400**. The two
      entities in play are the caller's connection name and our discovery
      database; the connection name came from the request, and "no connection by
      that name in this region" is fixable by sending a different one. In
      ``select_rds`` mode both entities are ours (we created the connection three
      statements ago), so the same code is a **500**.
    * ``AccessDeniedException`` → **500**, deliberately, not 403. The end user
      *is* authorized — the Cognito authorizer already passed — so a 403 would
      claim something false about them and sit confusingly close to the client's
      401 session-expiry path. What lacks the grant is this Lambda's own
      execution role, which no request change can fix. The message names the
      stack to redeploy, and ``reason`` makes the case machine-distinguishable
      from an ordinary 500.
    * ``InvalidInputException`` / ``ValidationException`` → **400**. Malformed
      connection, JDBC target or tag. Note that ``_create_or_update_crawler``
      has already absorbed the one benign variant (Glue's sticky "Tags ... in
      deleting state" race), so anything arriving here is a real rejection.
    * ``RuntimeError`` → **500**. ``_create_or_update_crawler`` raises this when
      ``GLUE_PIPELINE_ROLE_ARN`` is unset. It is not a ``ClientError`` or a
      ``BotoCoreError``, so before this change it missed the call sites' except
      tuple entirely and landed on the handler's outer safety net as "Unexpected
      error starting JDBC schema discovery" — the least diagnostic possible
      rendering of a one-line deployment fix.
    * anything else → **500**, unchanged in spirit from ``handle_error``.

    Args:
        exc: The exception raised by :func:`_create_or_update_crawler`.
        crawler_name: Crawler the failure is about, for the log line.
        connection_name: Glue Connection the crawler was pointed at. Named in
            the message because in ``glue_connection`` mode it is the request
            value the operator has to correct.
        glue_connection_mode: True when the connection came from the request
            rather than being created by this handler. Only affects the
            ``EntityNotFoundException`` arm.
        preflight_note: Sentence appended when the S3 pre-flight could not run,
            so a failure that the pre-flight would have caught is not silently
            attributed to something else.

    Returns:
        API Gateway response dict (see :func:`create_response`).
    """
    detail = _redact_aws_detail(exc)
    code = _aws_error_code(exc)
    tail = f" {preflight_note}" if preflight_note else ""

    if isinstance(exc, RuntimeError):
        logger.error(
            "Discovery crawler %s not created — missing configuration: %s",
            crawler_name,
            exc,
        )
        return create_response(
            500,
            {
                "error": (
                    "Schema discovery is not fully configured in this deployment: "
                    "the dashboard API Lambda has no GLUE_PIPELINE_ROLE_ARN. "
                    "Redeploy the Compute stack (AgenticDataIngestion-Compute-<env>) "
                    "and retry. Nothing in the wizard can fix this."
                ),
                "reason": _REASON_MISSING_CONFIG,
                "details": detail,
            },
        )

    if code == "EntityNotFoundException":
        if glue_connection_mode:
            logger.error(
                "Discovery crawler %s not created — Glue entity not found (connection=%s, database=%s): %s",
                crawler_name,
                connection_name,
                DISCOVERY_DB,
                exc,
            )
            return create_response(
                400,
                {
                    "error": (
                        f"Glue could not find something the discovery crawler needs. The "
                        f"candidates are the connection '{connection_name}' and the discovery "
                        f"database '{DISCOVERY_DB}' — the AWS message below names which. "
                        f"Both are looked up in {AWS_REGION}, so a Glue connection created in "
                        f"another region is not visible here even though its name is correct."
                    ),
                    "reason": _REASON_ENTITY_NOT_FOUND,
                    "details": detail,
                },
            )
        logger.error(
            "Discovery crawler %s not created — Glue entity not found on a resource we own "
            "(connection=%s, database=%s): %s",
            crawler_name,
            connection_name,
            DISCOVERY_DB,
            exc,
        )
        return create_response(
            500,
            {
                "error": (
                    f"Glue could not find a resource this deployment owns: the wizard-time "
                    f"connection '{connection_name}' or the discovery database "
                    f"'{DISCOVERY_DB}'. The database is created by the Foundation stack — if "
                    f"the AWS message below names it, redeploy "
                    f"AgenticDataIngestion-Foundation-<env>.{tail}"
                ),
                "reason": _REASON_ENTITY_NOT_FOUND,
                "details": detail,
            },
        )

    if code == "AccessDeniedException":
        logger.error(
            "Discovery crawler %s not created — access denied (connection=%s): %s",
            crawler_name,
            connection_name,
            exc,
        )
        return create_response(
            500,
            {
                "error": (
                    "This deployment is not allowed to create the discovery crawler. "
                    "CreateCrawler needs both glue:CreateCrawler and iam:PassRole on the "
                    "Glue pipeline role; the AWS message below names which one was refused. "
                    "Both are granted by the Foundation stack, so the usual cause is a "
                    "Foundation deployment that predates them — redeploy "
                    "AgenticDataIngestion-Foundation-<env> and retry. Nothing in the wizard "
                    f"can fix this.{tail}"
                ),
                "reason": _REASON_ACCESS_DENIED,
                "details": detail,
            },
        )

    if code in _INVALID_INPUT_CODES:
        logger.error(
            "Discovery crawler %s not created — invalid input (connection=%s): %s",
            crawler_name,
            connection_name,
            exc,
        )
        return create_response(
            400,
            {
                "error": (
                    f"Glue rejected the discovery crawler definition as invalid. The crawler "
                    f"points at connection '{connection_name}' and database "
                    f"'{DISCOVERY_DB}'; the AWS message below names the field it objected to."
                ),
                "reason": _REASON_INVALID_INPUT,
                "details": detail,
            },
        )

    logger.error(
        "Failed to create/update discovery crawler %s (connection=%s, code=%s): %s",
        crawler_name,
        connection_name,
        code or "<none>",
        exc,
    )
    return create_response(
        500,
        {
            "error": f"Failed to create discovery crawler{tail}",
            "reason": _REASON_UNEXPECTED,
            "details": detail,
        },
    )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _compute_discovery_hash(source_config: dict[str, Any]) -> str:
    """Return a deterministic 12-char hash identifying this discovery target.

    The hash collapses concurrent wizard sessions for the same logical
    connection + table onto the same crawler / connection resources, so
    repeated clicks of "Detect Schema" do not spawn duplicates.

    Hash inputs differ per connection mode:
        * ``select_rds`` mode (no ``glue_connection_name`` in config):
          ``secret_arn || jdbc_url || jdbc_schema || jdbc_table``
        * ``glue_connection`` mode (``glue_connection_name`` present):
          ``glue_connection_name || jdbc_schema || jdbc_table``

    Args:
        source_config: Source configuration map from the request body.

    Returns:
        First 12 hex characters of the SHA-256 digest. 12 characters is
        enough to collide-resist within a single AWS account's set of
        active discovery sessions and keeps the resource names readable.
    """
    schema = source_config.get("jdbc_schema", "")
    table = source_config.get("jdbc_table", "")

    glue_connection_name = source_config.get("glue_connection_name")
    if glue_connection_name:
        # glue_connection mode — the user's existing connection IS the
        # connection identity, no need to re-derive from URL+secret.
        material = f"{glue_connection_name}{schema}{table}"
    else:
        # select_rds mode — secret_arn + jdbc_url uniquely identify the
        # backing RDS / Aurora instance for our purposes.
        secret_arn = source_config.get("secret_arn", "")
        jdbc_url = source_config.get("jdbc_url", "")
        material = f"{secret_arn}{jdbc_url}{schema}{table}"

    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# S3 VPC Gateway Endpoint pre-flight (select_rds mode only)
# ---------------------------------------------------------------------------


def _check_s3_endpoint_exists(subnet_id: str) -> bool:
    """Return ``True`` when the subnet's route table has an S3 VPC endpoint.

    Glue Crawlers (and Glue ETL jobs) need S3 access from inside the
    customer's VPC for temporary script staging and log delivery. When
    the source RDS instance is in a private subnet without internet
    access, the only viable path is an S3 VPC Gateway Endpoint
    (``vpce-*`` with a managed-prefix-list destination). Without one the
    crawler fails with an opaque "S3 not reachable" error deep in Glue —
    so we pre-flight here and surface a clean HTTP 400 instead.

    The check inspects the subnet's associated route table(s) for any
    route whose ``GatewayId`` starts with ``vpce-`` AND whose destination
    is a ``DestinationPrefixListId`` (which is how S3 / DynamoDB Gateway
    Endpoints attach — they don't use a CIDR). Subnets that don't have
    an explicit route-table association use the VPC's main route table;
    the helper falls back to looking up the main route table when the
    initial filter returns no results AND when an IGW route exists in
    that table (in which case S3 is reachable via the public internet
    from public subnets, even without a Gateway Endpoint).

    Args:
        subnet_id: EC2 subnet ID (``subnet-...``) of the source RDS
            instance.

    Returns:
        ``True`` iff S3 is reachable from the subnet — either via a
        VPC Gateway Endpoint route or via an Internet Gateway route
        (for public subnets). ``False`` on any condition that means
        "the subnet cannot reach S3" — including no route table found,
        no matching routes, or any AWS API error.
    """

    def _route_tables_grant_s3_access(
        route_tables: list[dict[str, Any]],
    ) -> bool:
        """Return True when any route grants S3 reachability for a Glue ENI.

        Glue ENIs do NOT get public IPs even in public subnets, so an
        Internet Gateway route alone is NOT enough. The valid paths are:

        * A VPC Gateway Endpoint route (``vpce-*`` + prefix-list).
        * A NAT Gateway route (``nat-*`` for ``0.0.0.0/0``) so the
          private ENI can egress to public S3 through the NAT.
        """
        for rt in route_tables:
            for route in rt.get("Routes", []):
                gateway_id = route.get("GatewayId", "") or ""
                nat_gateway_id = route.get("NatGatewayId", "") or ""
                prefix_list_id = route.get("DestinationPrefixListId", "")
                # Path 1: S3 Gateway Endpoint route.
                if gateway_id.startswith("vpce-") and prefix_list_id:
                    return True
                # Path 2: NAT Gateway route to 0.0.0.0/0. The Glue ENI
                # uses the NAT to reach S3's public endpoint.
                if nat_gateway_id.startswith("nat-") and route.get("DestinationCidrBlock") == "0.0.0.0/0":
                    return True
        return False

    try:
        # Step 1 — check explicitly-associated route tables.
        response = ec2_client.describe_route_tables(Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}])
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "describe_route_tables (explicit) failed for subnet %s: %s — treating as no S3 endpoint",
            subnet_id,
            exc,
        )
        return False

    explicit_rts = response.get("RouteTables", []) or []
    if explicit_rts:
        return _route_tables_grant_s3_access(explicit_rts)

    # Step 2 — no explicit association. The subnet uses the VPC's main
    # route table. Resolve the subnet → VPC and look up the main RT.
    try:
        subnet_response = ec2_client.describe_subnets(SubnetIds=[subnet_id])
        subnets = subnet_response.get("Subnets", [])
        if not subnets:
            logger.warning("Subnet %s not found", subnet_id)
            return False
        vpc_id = subnets[0].get("VpcId")
        if not vpc_id:
            return False

        main_rt_response = ec2_client.describe_route_tables(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "association.main", "Values": ["true"]},
            ]
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "Main route-table lookup failed for subnet %s: %s — treating as no S3 endpoint",
            subnet_id,
            exc,
        )
        return False

    return _route_tables_grant_s3_access(main_rt_response.get("RouteTables", []) or [])


# ---------------------------------------------------------------------------
# PhysicalConnectionRequirements helper
# ---------------------------------------------------------------------------


def _build_physical_requirements(source_config: dict[str, Any]) -> dict[str, Any]:
    """Return the ``PhysicalConnectionRequirements`` dict for Glue.

    Mirrors the shape consumed by
    :func:`jdbc_helpers.create_glue_jdbc_connection`.
    ``AvailabilityZone`` is included only when the caller supplied one
    (Glue accepts it as optional).

    Accepts ``security_group_ids`` as either a real ``list[str]`` or a
    JSON-encoded list (the wizard's ``Record<string, string>`` config
    shape encodes the list at submission time). The Glue API requires
    a real list, so we normalise here before passing it through.

    Args:
        source_config: Source configuration map.

    Returns:
        Dict suitable for the ``physical_requirements`` argument of
        :func:`jdbc_helpers.create_glue_jdbc_connection`.
    """
    raw_sg = source_config["security_group_ids"]
    if isinstance(raw_sg, str):
        try:
            sg_list = json.loads(raw_sg)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "source_config.security_group_ids must be a list of "
                f"strings or a JSON-encoded list; got {raw_sg!r}: {exc}"
            ) from exc
        if not isinstance(sg_list, list):
            raise ValueError(
                f"source_config.security_group_ids JSON value must be a list; got {type(sg_list).__name__}"
            )
    elif isinstance(raw_sg, list):
        sg_list = list(raw_sg)
    else:
        raise ValueError(
            f"source_config.security_group_ids must be a list or JSON-encoded list; got {type(raw_sg).__name__}"
        )

    physical: dict[str, Any] = {
        "SubnetId": source_config["subnet_id"],
        "SecurityGroupIdList": sg_list,
    }
    az = source_config.get("availability_zone")
    if az:
        physical["AvailabilityZone"] = az
    return physical


# ---------------------------------------------------------------------------
# S3 pre-flight for a user-supplied Glue Connection (glue_connection mode)
# ---------------------------------------------------------------------------

#: Verdicts from :func:`_connection_s3_preflight`. Only ``_PREFLIGHT_NO_ROUTE``
#: and ``_PREFLIGHT_NO_CONNECTION`` refuse; the rest proceed.
_PREFLIGHT_OK = "reachable"
_PREFLIGHT_NO_ROUTE = "unreachable"
_PREFLIGHT_NOT_VPC = "not_vpc_attached"
_PREFLIGHT_NO_CONNECTION = "connection_not_found"
_PREFLIGHT_UNKNOWN = "unknown"


def _connection_s3_preflight(connection_name: str) -> tuple[str, str]:
    """Run the S3-reachability pre-flight against a user-supplied connection.

    ``select_rds`` mode has always pre-flighted the subnet it was handed, and
    answers a clean 400 naming the missing S3 VPC Gateway Endpoint.
    ``glue_connection`` mode skipped the check entirely, because the request
    carries no ``subnet_id`` — so the same misconfiguration surfaced as an opaque
    crawler failure instead. The connection carries its own networking, though:
    ``glue:GetConnection`` returns ``PhysicalConnectionRequirements.SubnetId``.
    Read that and run the *same* :func:`_check_s3_endpoint_exists`.

    **A pre-flight that cannot run must not become a new way to fail.** The five
    verdicts exist to keep "checked, and there is no S3 route" apart from "could
    not check":

    ``_PREFLIGHT_NO_ROUTE``
        The connection is VPC-attached and its subnet's route tables offer
        neither an S3 Gateway Endpoint nor a NAT route. Refuse — actionable.
    ``_PREFLIGHT_NO_CONNECTION``
        ``GetConnection`` answered ``EntityNotFoundException``. Refuse: this is
        not an unavailable check, it is a definite answer that the named
        connection does not exist, and ``CreateCrawler`` would fail on it a
        moment later with less context.
    ``_PREFLIGHT_NOT_VPC``
        No ``PhysicalConnectionRequirements``, so the connection is not
        VPC-attached and the check does not apply. Proceed.
    ``_PREFLIGHT_UNKNOWN``
        ``GetConnection`` failed for any other reason — a missing
        ``glue:GetConnection`` grant, a throttle, a transport error — or returned
        a payload this function could not read. Proceed, and hand the caller a
        sentence saying so.
    ``_PREFLIGHT_OK``
        Checked, and S3 is reachable.

    **Known residual, inherited not introduced:**
    :func:`_check_s3_endpoint_exists` returns ``False`` both for "route tables
    read, no S3 route" and for "the EC2 describe calls failed" — it logs the
    difference and collapses it in the return value. So a denied
    ``ec2:DescribeRouteTables`` reaches here as ``_PREFLIGHT_NO_ROUTE``. That
    conflation predates this function and applies identically to ``select_rds``
    mode; rather than fork a second checker, the refusal message names the
    route-table read as the source of the finding and names the denied-permission
    case as an alternative reading, so both are diagnosable from the response.

    ``HidePassword=True`` is passed because this function has no business seeing
    a credential: it reads one networking field. ``LGR-819980f-1``.

    Args:
        connection_name: The user's existing Glue Connection name.

    Returns:
        ``(verdict, message)``. ``message`` is a full sentence for the refusing
        verdicts, a note for ``_PREFLIGHT_UNKNOWN``, and ``""`` otherwise.
    """
    try:
        response = glue_client.get_connection(Name=connection_name, HidePassword=True)
    # Deliberately broad: this is a pre-flight, and every failure of it is a
    # "could not check", which must not become a new way to fail the request.
    # Classified by AWS error code below, then proceeded past.
    except Exception as exc:
        code = _aws_error_code(exc)
        if code == "EntityNotFoundException":
            return _PREFLIGHT_NO_CONNECTION, (
                f"Glue has no connection named '{connection_name}' in {AWS_REGION}. Check the "
                f"name, and check the region — a connection created in another region is not "
                f"visible here even when the name is right."
            )
        logger.warning(
            "S3 pre-flight unavailable for Glue connection %s: get_connection failed (code=%s): %s",
            connection_name,
            code or "<none>",
            exc,
        )
        return _PREFLIGHT_UNKNOWN, (
            f"The S3-connectivity pre-flight could not run for connection "
            f"'{connection_name}' (Glue GetConnection failed: {code or type(exc).__name__}), so "
            f"a missing S3 VPC Gateway Endpoint in the connection's subnet has not been ruled "
            f"out."
        )

    connection = response.get("Connection") if isinstance(response, dict) else None
    if not isinstance(connection, dict):
        logger.warning(
            "S3 pre-flight unavailable for Glue connection %s: unreadable GetConnection payload",
            connection_name,
        )
        return _PREFLIGHT_UNKNOWN, (
            f"The S3-connectivity pre-flight could not run for connection '{connection_name}' "
            f"(unreadable GetConnection response), so a missing S3 VPC Gateway Endpoint in the "
            f"connection's subnet has not been ruled out."
        )

    physical = connection.get("PhysicalConnectionRequirements")
    subnet_id = physical.get("SubnetId") if isinstance(physical, dict) else None
    if not isinstance(subnet_id, str) or not subnet_id:
        # Not VPC-attached (or attached without a subnet): Glue does not place an
        # ENI in a customer subnet, so subnet-level S3 routing is not the
        # question. Nothing to check, nothing to refuse.
        logger.info(
            "Glue connection %s declares no PhysicalConnectionRequirements.SubnetId — "
            "S3 subnet pre-flight does not apply",
            connection_name,
        )
        return _PREFLIGHT_NOT_VPC, ""

    if _check_s3_endpoint_exists(subnet_id):
        logger.info(
            "S3 pre-flight passed for Glue connection %s (subnet %s)",
            connection_name,
            subnet_id,
        )
        return _PREFLIGHT_OK, ""

    return _PREFLIGHT_NO_ROUTE, (
        f"Glue connection '{connection_name}' is attached to subnet {subnet_id}, and no route "
        f"to S3 was found in that subnet's route tables. Add an S3 VPC Gateway Endpoint to the "
        f"subnet's VPC and retry — Glue crawlers require S3 access for temporary script staging "
        f"and log delivery. If the VPC does have one, the other reading of this result is that "
        f"ec2:DescribeRouteTables was refused for this deployment, which the Lambda logs "
        f"distinguish."
    )


# ---------------------------------------------------------------------------
# Connection resolution, shared by the source-side and target-side handlers
# ---------------------------------------------------------------------------


class _ResolvedConnection(NamedTuple):
    """Outcome of :func:`_resolve_discovery_connection`.

    ``error_response`` is the discriminator: when it is set the caller returns it
    unchanged and ``connection_name`` is meaningless (``""``). Typed as a plain
    ``str`` rather than ``str | None`` so the success path needs no narrowing
    assertion at either call site.
    """

    connection_name: str
    error_response: dict[str, Any] | None
    preflight_note: str


def _resolve_discovery_connection(
    config: dict[str, Any],
    *,
    config_key: str,
    side: str,
    hash_value: str,
    tags: dict[str, str],
) -> _ResolvedConnection:
    """Resolve the Glue Connection the discovery crawler should use.

    Extracted from :func:`detect_schema_start` and
    :func:`detect_target_schema_start`, which carried this block as
    near-duplicates. That duplication is not incidental to the bug this function
    fixes — it *is* how the bug spread: the ``glue_connection``-mode pre-flight
    gap existed in both copies, and a fix applied to one would have left the
    other asymmetric in the same way. One definition now, two callers.

    The two sides differ only in the words they use for the same things, which is
    why ``config_key`` and ``side`` are parameters rather than branches: the
    source side says ``source_config`` / ``Source``, the target side
    ``target_config`` / ``Target``. Both wordings are preserved exactly as they
    were, so no existing 400 message changes.

    Args:
        config: ``source_config`` or ``target_config`` from the request body.
        config_key: Its name, for the required-field 400 messages.
        side: ``"Source"`` or ``"Target"``, for the subnet 400 message.
        hash_value: Discovery hash, naming the wizard-time connection.
        tags: Discovery tags applied to a wizard-time connection.

    Returns:
        A :class:`_ResolvedConnection`. ``preflight_note`` is non-empty only when
        the S3 pre-flight could not run, and is threaded into a later crawler
        failure so the unchecked possibility is named rather than forgotten.
    """
    glue_connection_name = config.get("glue_connection_name")

    if glue_connection_name:
        # glue_connection mode — reuse the user's connection as-is. We do NOT
        # tag it: it does not belong to AgenticDataIngestion's lifecycle.
        logger.info(
            "Discovery in glue_connection mode using existing connection: %s",
            glue_connection_name,
        )
        verdict, message = _connection_s3_preflight(glue_connection_name)
        if verdict in (_PREFLIGHT_NO_ROUTE, _PREFLIGHT_NO_CONNECTION):
            return _ResolvedConnection("", create_response(400, {"error": message}), "")
        note = message if verdict == _PREFLIGHT_UNKNOWN else ""
        return _ResolvedConnection(glue_connection_name, None, note)

    # select_rds mode — pre-flight S3 endpoint, then create the wizard-time
    # Source_Connection.
    for required_field in (
        "secret_arn",
        "jdbc_url",
        "jdbc_driver",
        "subnet_id",
        "security_group_ids",
    ):
        if not config.get(required_field):
            return _ResolvedConnection(
                "",
                create_response(
                    400,
                    {"error": (f"{config_key}.{required_field} is required for select_rds mode")},
                ),
                "",
            )

    subnet_id = config["subnet_id"]
    if not _check_s3_endpoint_exists(subnet_id):
        return _ResolvedConnection(
            "",
            create_response(
                400,
                {
                    "error": (
                        f"{side} RDS subnet {subnet_id} has no S3 connectivity. "
                        "Add an S3 VPC Gateway Endpoint to the subnet's VPC and "
                        "retry. Glue crawlers require S3 access for temporary "
                        "script staging."
                    )
                },
            ),
            "",
        )

    connection_name = f"agentic-data-ingestion-discover-{hash_value}"
    try:
        create_glue_jdbc_connection(
            connection_name=connection_name,
            jdbc_url=config["jdbc_url"],
            jdbc_driver=config["jdbc_driver"],
            secret_arn=config["secret_arn"],
            physical_requirements=_build_physical_requirements(config),
            tags=tags,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error(
            "Failed to create wizard-time Glue Connection %s: %s",
            connection_name,
            exc,
        )
        return _ResolvedConnection("", handle_error(exc, "Failed to create Glue Connection"), "")

    return _ResolvedConnection(connection_name, None, "")


# ---------------------------------------------------------------------------
# Crawler create-or-update
# ---------------------------------------------------------------------------


def _create_or_update_crawler(
    crawler_name: str,
    connection_name: str,
    schema: str,
    table: str,
    table_prefix: str,
    tags: dict[str, str],
) -> None:
    """Idempotently create or update the wizard-time discovery crawler.

    We check existence via ``GetCrawler`` first and, when the crawler is
    already present, update it in place (never ``CreateCrawler`` with
    ``Tags`` on an existing name). This matters because Glue can return a
    sticky ``InvalidInputException`` ("Tags ... in deleting state") on a
    create-with-``Tags`` call against an existing crawler name (tag
    eventual-consistency) — that is NOT an ``AlreadyExistsException``, so
    without the existence check it would bubble up as an HTTP 500 on every
    wizard retry. As a further safety net we also tolerate that sticky
    ``InvalidInputException`` on the create path by falling back to
    ``UpdateCrawler``, so retries converge instead of surfacing a 500.

    Note: ``Tags`` are accepted only on ``CreateCrawler``. The Glue API
    does NOT support ``Tags`` on ``UpdateCrawler``, so on the update
    path we silently skip them — existing tags are preserved by Glue
    and the daily janitor (Task 4.3) only needs them present, not
    refreshed on every wizard run.

    Args:
        crawler_name: Glue Crawler name (``agentic-data-ingestion-discover-{hash}``).
        connection_name: Glue Connection that the crawler uses to reach
            the source database.
        schema: Source database schema / namespace identifier.
        table: Source table name.
        table_prefix: Forces a deterministic prefix on the produced
            Glue Catalog table so :func:`glue:GetTables` can locate it
            regardless of the engine-specific name mangling Glue applies.
        tags: ``{Key: Value}`` dict applied on create only.
    """
    if not GLUE_PIPELINE_ROLE_ARN:
        # Surface a clear configuration error rather than letting Glue
        # produce a confusing "Role cannot be empty" InvalidInputException.
        raise RuntimeError(
            "GLUE_PIPELINE_ROLE_ARN environment variable is not set; "
            "the dashboard API Lambda must be deployed with this env var "
            "(see Task 14.2 / compute-stack.ts)."
        )

    crawler_input: dict[str, Any] = {
        "Name": crawler_name,
        "Role": GLUE_PIPELINE_ROLE_ARN,
        "DatabaseName": DISCOVERY_DB,
        "Targets": {
            "JdbcTargets": [
                {
                    "ConnectionName": connection_name,
                    "Path": f"{schema}/{table}",
                }
            ]
        },
        "TablePrefix": table_prefix,
    }

    # Idempotency + Glue tag-race tolerance: when the crawler already exists we
    # must NOT call create_crawler(..., Tags=...). Glue can return a sticky
    # InvalidInputException ("Tags ... in deleting state") on create-with-Tags
    # against an existing name (tag eventual-consistency) — that is NOT an
    # AlreadyExistsException, so without this it bubbles to the user as an
    # HTTP 500 on every wizard retry. Going straight to update_crawler (which
    # takes no Tags) is idempotent and unaffected by tag state; start_crawler
    # (called by the caller) likewise does not depend on tag state.
    try:
        glue_client.get_crawler(Name=crawler_name)
        crawler_exists = True
    except glue_client.exceptions.EntityNotFoundException:
        crawler_exists = False

    if crawler_exists:
        # Tags is intentionally NOT passed on update — see docstring.
        glue_client.update_crawler(**crawler_input)
        logger.info("Updated Glue discovery crawler: %s", crawler_name)
        return

    try:
        glue_client.create_crawler(**crawler_input, Tags=tags)
        logger.info("Created Glue discovery crawler: %s", crawler_name)
    except glue_client.exceptions.AlreadyExistsException:
        # Race: crawler created between our get_crawler and create — converge
        # via update (Tags intentionally NOT passed — see docstring).
        glue_client.update_crawler(**crawler_input)
        logger.info("Updated Glue discovery crawler: %s", crawler_name)
    except glue_client.exceptions.InvalidInputException as exc:
        # Sticky Glue tag-deletion state on an existing crawler name: fall back
        # to update (no Tags) so wizard retries converge instead of surfacing an
        # HTTP 500. Re-raise anything that is not the tag-deletion race.
        if "deleting state" in str(exc).lower():
            glue_client.update_crawler(**crawler_input)
            logger.info(
                "Updated Glue discovery crawler after tag-deletion race: %s",
                crawler_name,
            )
        else:
            raise


# ---------------------------------------------------------------------------
# Public handler — POST /api/resources/jdbc/detect-schema
# ---------------------------------------------------------------------------


def detect_schema_start(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Start a Glue Crawler-based schema discovery.

    POST /api/resources/jdbc/detect-schema

    Request body (JSON):
        ``{"source_config": {<JDBC source config>}}``

    Source_config fields consumed:
        Always: ``jdbc_schema``, ``jdbc_table``.
        ``select_rds`` mode (no ``glue_connection_name``):
            ``secret_arn``, ``jdbc_url``, ``jdbc_driver``, ``subnet_id``,
            ``security_group_ids`` (list[str]), optional
            ``availability_zone``.
        ``glue_connection`` mode: ``glue_connection_name``.

    Response:
        HTTP 202 ``{"crawler_name", "glue_database", "table_prefix",
        "status": "RUNNING"}`` on successful crawler start.
        HTTP 400 with ``{"error"}`` on validation, missing-config, or
        S3-endpoint pre-flight failure — including, in
        ``glue_connection`` mode, a connection Glue cannot find and a
        connection whose subnet has no route to S3.
        HTTP 400 with ``{"error", "reason", "details"}`` when Glue
        rejects the crawler definition, or cannot find the named
        connection at crawler-creation time.
        HTTP 500 with ``{"error", "details"}`` on any other AWS API
        failure; crawler-creation failures additionally carry
        ``reason``. See :func:`_crawler_failure_response` for the
        status-code split and why ``AccessDeniedException`` is a 500.

    Args:
        event: API Gateway proxy event.
        context: Lambda context (unused, kept for AWS handler signature).

    Returns:
        API Gateway response dict (see :func:`create_response`).
    """
    try:
        # ------------------------------------------------------------ parse
        raw_body = event.get("body") or "{}"
        try:
            body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
        except json.JSONDecodeError:
            return create_response(400, {"error": "Request body must be valid JSON"})

        source_config = body.get("source_config")
        if not isinstance(source_config, dict):
            return create_response(
                400,
                {"error": "source_config is required and must be an object"},
            )

        schema = source_config.get("jdbc_schema")
        table = source_config.get("jdbc_table")
        if not schema or not table:
            return create_response(
                400,
                {"error": "source_config.jdbc_schema and source_config.jdbc_table are required"},
            )

        # ------------------------------------------------------- validate ids
        # Defence-in-depth: re-run the wizard's identifier regex check
        # server-side. The crawler's TablePrefix and JdbcTargets path
        # interpolate these values into Glue API calls, and Glue does
        # not accept arbitrary characters anyway.
        try:
            validate_identifier(schema)
            validate_identifier(table)
        except ValueError as exc:
            return create_response(400, {"error": str(exc)})

        # ----------------------------------------------------------- hash
        hash_value = _compute_discovery_hash(source_config)
        crawler_name = f"agentic-data-ingestion-discover-{hash_value}"
        table_prefix = f"agentic_data_ingestion_{hash_value}_"
        iso_now = datetime.now(UTC).isoformat()
        tags: dict[str, str] = {
            _DISCOVERY_TAG_KEY: _DISCOVERY_TAG_VALUE,
            "CreatedAt": iso_now,
            "Hash": hash_value,
        }

        # -------------------------------------------- determine connection
        resolved = _resolve_discovery_connection(
            source_config,
            config_key="source_config",
            side="Source",
            hash_value=hash_value,
            tags=tags,
        )
        if resolved.error_response is not None:
            return resolved.error_response
        connection_name = resolved.connection_name

        # -------------------------------------------------- create crawler
        # ``RuntimeError`` joins the tuple: _create_or_update_crawler raises it
        # for an unset GLUE_PIPELINE_ROLE_ARN, which previously fell through to
        # the outer safety net and was reported as "Unexpected error".
        try:
            _create_or_update_crawler(
                crawler_name=crawler_name,
                connection_name=connection_name,
                schema=schema,
                table=table,
                table_prefix=table_prefix,
                tags=tags,
            )
        except (ClientError, BotoCoreError, RuntimeError) as exc:
            return _crawler_failure_response(
                exc,
                crawler_name=crawler_name,
                connection_name=connection_name,
                glue_connection_mode=bool(source_config.get("glue_connection_name")),
                preflight_note=resolved.preflight_note,
            )

        # --------------------------------------------------- start crawler
        try:
            glue_client.start_crawler(Name=crawler_name)
            logger.info("Started discovery crawler: %s", crawler_name)
        except glue_client.exceptions.CrawlerRunningException:
            # Concurrent click — there's already a run in flight for this
            # exact crawler. Fall through and let the frontend poll: the
            # in-flight run produces the same table either way.
            logger.info(
                "Crawler %s is already running; continuing with existing run",
                crawler_name,
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to start discovery crawler %s: %s", crawler_name, exc)
            return handle_error(exc, "Failed to start discovery crawler")

        return create_response(
            202,
            {
                "crawler_name": crawler_name,
                "glue_database": DISCOVERY_DB,
                "table_prefix": table_prefix,
                "status": "RUNNING",
            },
        )

    except Exception as exc:  # pragma: no cover - last-resort safety net
        return handle_error(exc, "Unexpected error starting JDBC schema discovery")


# ---------------------------------------------------------------------------
# Schema mapping helper — Glue table → wire schema
# ---------------------------------------------------------------------------


def _glue_table_to_schema(table_meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a Glue Catalog ``Table`` payload to AgenticDataIngestion's wire schema.

    Mirrors the column shape produced by the existing
    :func:`handlers.resources.detect_schema` so the frontend's schema
    step renders JDBC-discovered schemas through the same code path as
    Glue Catalog and S3 Tables targets.

    Both regular columns (``StorageDescriptor.Columns``) and partition
    keys (``PartitionKeys``) are surfaced as ordinary fields — Glue
    crawlers occasionally promote indexed columns to partition keys for
    JDBC sources, and downstream code does not need to distinguish.

    Glue Catalog columns are always nullable from the catalog's point of
    view (the JDBC NOT NULL constraint is not preserved through the
    crawl), so we report ``nullable=True`` uniformly. Comments are
    surfaced as descriptions when set.

    Args:
        table_meta: The ``Table`` sub-dict returned by
            :func:`glue:GetTables` (single element of ``TableList``).

    Returns:
        ``[{name, type, nullable, description}]`` records, in the order
        Glue returns them — regular columns first, then partition keys.
    """
    storage = table_meta.get("StorageDescriptor", {}) or {}
    columns = storage.get("Columns", []) or []
    partition_keys = table_meta.get("PartitionKeys", []) or []

    schema: list[dict[str, Any]] = []
    for column in list(columns) + list(partition_keys):
        schema.append(
            {
                "name": column.get("Name", ""),
                # Glue catalog types preserve the JDBC catalog form
                # (``string``, ``int``, ``decimal(10,2)`` …). We keep
                # them verbatim so downstream Spark codegen can map
                # them with full fidelity.
                "type": column.get("Type", "string"),
                "nullable": True,
                "description": column.get("Comment", "") or "",
            }
        )

    return schema


# ---------------------------------------------------------------------------
# Public handler — GET /api/resources/jdbc/detect-schema/{crawler_name}
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Polling helper — shared by source-side and target-side status endpoints
# ---------------------------------------------------------------------------


def _poll_discovery_crawler(
    crawler_name: str, table_prefix: str
) -> tuple[str, list[dict[str, Any]] | None, str, str | None, str | None]:
    """Inspect a wizard-time discovery crawler and classify its current state.

    Encapsulates the boto3 calls + crawler-state-machine classification
    + Glue-table-to-schema mapping that both the source-side and the
    target-side polling endpoints share. Both endpoints differ only in
    HTTP wrapping (path parameters in / response shape out) — the
    polling core is identical because the crawler / connection / Glue
    database / tag-set are all created with the same conventions on
    both sides (P19 Req 4.3, 4.4, 4.7, 4.8, 4.9).

    Args:
        crawler_name: Glue Crawler name (``agentic-data-ingestion-discover-{hash}``).
        table_prefix: Forced table prefix that the crawler used when it
            wrote into the discovery Glue database. Used as the
            ``Expression`` argument to :func:`glue:GetTables` so we
            isolate this discovery's output from any other crawler's
            tables that happen to share the discovery database.

    Returns:
        A 5-tuple ``(status, schema, glue_database, glue_table, error_message)`` where:

        * ``status`` is one of ``"RUNNING"`` | ``"READY"`` | ``"FAILED"``.
        * ``schema`` is the discovered column list (per
          :func:`_glue_table_to_schema`) when ``status == "READY"``,
          else ``None``.
        * ``glue_database`` is always :data:`DISCOVERY_DB` so callers
          can include it in 202/200/500 responses without re-deriving.
        * ``glue_table`` is the produced Glue Catalog table name when
          ``status == "READY"``, else ``None``.
        * ``error_message`` is a human-readable description of the
          failure when ``status == "FAILED"``, else ``None``.

    Raises:
        ``glue_client.exceptions.EntityNotFoundException``: Crawler does
            not exist. Callers translate this to HTTP 404. Allowed to
            propagate so the wrapper handlers can choose their own
            response shape.
        ``ClientError`` | ``BotoCoreError``: Any other AWS API failure.
            Callers translate via :func:`handle_error`.
    """
    response = glue_client.get_crawler(Name=crawler_name)
    crawler = response.get("Crawler", {}) or {}
    state = crawler.get("State", "")

    # Glue exposes State ∈ {READY, RUNNING, STOPPING}. ``READY`` is
    # the steady-state name for "not currently running" — the
    # actual outcome lives in ``LastCrawl``. We treat ``STOPPING``
    # (transient between RUNNING and READY) and any unrecognised
    # state as RUNNING for safety so the frontend keeps polling
    # rather than giving up.
    if state != "READY":
        return ("RUNNING", None, DISCOVERY_DB, None, None)

    last_crawl = crawler.get("LastCrawl") or {}
    last_status = last_crawl.get("Status", "")

    if last_status == "FAILED":
        error_message = last_crawl.get("ErrorMessage") or "Crawler reported FAILED with no error message"
        logger.warning("Crawler %s failed: %s", crawler_name, error_message)
        return ("FAILED", None, DISCOVERY_DB, None, error_message)

    if last_status == "CANCELLED":
        return (
            "FAILED",
            None,
            DISCOVERY_DB,
            None,
            "Crawler was cancelled before completion",
        )

    if last_status != "SUCCEEDED":
        # Includes the ``""`` case (crawler exists but has never
        # run — concurrent click before StartCrawler completed) and
        # any unrecognised future status. Treat as still running so
        # the frontend keeps polling.
        return ("RUNNING", None, DISCOVERY_DB, None, None)

    tables_response = glue_client.get_tables(
        DatabaseName=DISCOVERY_DB,
        Expression=table_prefix + "*",
    )
    table_list = tables_response.get("TableList", []) or []

    if len(table_list) == 0:
        # Crawler reports SUCCEEDED but produced no Glue table — the
        # most common cause is that the source table genuinely does
        # not exist or the connection's credentials lack SELECT
        # privilege on it (Glue treats both as "no rows found,
        # nothing to crawl"). Surface a hint pointing at both.
        return (
            "FAILED",
            None,
            DISCOVERY_DB,
            None,
            (
                "Crawler completed but produced no table — check the "
                "source table exists and the credentials have SELECT "
                "privilege"
            ),
        )

    if len(table_list) > 1:
        unexpected = sorted(t.get("Name", "") for t in table_list)
        logger.warning(
            "Crawler %s produced %d tables (expected 1): %s",
            crawler_name,
            len(table_list),
            unexpected,
        )
        return (
            "FAILED",
            None,
            DISCOVERY_DB,
            None,
            f"Crawler produced unexpected tables: {unexpected}",
        )

    # Exactly one match — happy path. Cast the boto3 ``TableTypeDef``
    # to a plain dict for the helper signature; the structural fields
    # we read are identical between the two.
    table_meta: dict[str, Any] = dict(table_list[0])
    glue_table = table_meta.get("Name", "")
    schema = _glue_table_to_schema(table_meta)
    return ("READY", schema, DISCOVERY_DB, glue_table, None)


def _resolve_table_prefix(crawler_name: str, query_params: dict[str, Any] | None) -> str | None:
    """Return the table prefix to use for polling, deriving from name when absent.

    Prefer the explicit value the frontend round-tripped from the
    matching ``detect_*_schema_start`` response. When absent, derive
    from the crawler name — both source-side and target-side discoveries
    share the ``agentic-data-ingestion-discover-{hash}`` ↔ ``agentic_data_ingestion_{hash}_``
    naming convention so the derivation is unambiguous.

    Args:
        crawler_name: Crawler name from the path parameter.
        query_params: ``event.queryStringParameters`` (may be ``None``).

    Returns:
        The resolved table prefix, or ``None`` when neither the explicit
        value nor a derivable convention is available.
    """
    qp = query_params or {}
    table_prefix = qp.get("table_prefix")
    if table_prefix:
        return table_prefix

    expected_prefix = "agentic-data-ingestion-discover-"
    if crawler_name.startswith(expected_prefix):
        hash_value = crawler_name[len(expected_prefix) :]
        return f"agentic_data_ingestion_{hash_value}_"
    return None


def _build_status_response(
    crawler_name: str,
    status: str,
    schema: list[dict[str, Any]] | None,
    glue_database: str,
    glue_table: str | None,
    error_message: str | None,
    table_prefix: str,
) -> dict[str, Any]:
    """Map the :func:`_poll_discovery_crawler` 5-tuple to an HTTP response.

    Both source-side and target-side polling endpoints share this
    response shape (P19 Req 4.7-4.9) so the helper centralises it.
    """
    if status == "RUNNING":
        return create_response(
            202,
            {
                "status": "RUNNING",
                "crawler_name": crawler_name,
                "glue_database": glue_database,
                "table_prefix": table_prefix,
            },
        )
    if status == "READY":
        return create_response(
            200,
            {
                "status": "READY",
                "schema": schema,
                "glue_database": glue_database,
                "glue_table": glue_table,
            },
        )
    # FAILED
    return create_response(
        500,
        {
            "status": "FAILED",
            "crawler_name": crawler_name,
            "error_message": error_message,
        },
    )


def detect_schema_status(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Poll the wizard-time discovery crawler and return its result.

    GET /api/resources/jdbc/detect-schema/{crawler_name}

    Path parameters:
        ``crawler_name``: The crawler name returned by
        :func:`detect_schema_start` (``agentic-data-ingestion-discover-{hash}``).

    Query parameters:
        ``table_prefix`` (optional): The ``TablePrefix`` returned by the
        start endpoint. When omitted, derived from ``crawler_name`` —
        which works because the start endpoint always sets
        ``TablePrefix=agentic_data_ingestion_{hash}_`` matching
        ``crawler_name=agentic-data-ingestion-discover-{hash}``. The frontend
        normally round-trips the value to avoid coupling the two
        formats.

    Response:
        * HTTP 202 ``{status: "RUNNING", crawler_name, glue_database,
          table_prefix}`` while the crawler is still in flight.
        * HTTP 200 ``{status: "READY", schema, glue_database,
          glue_table}`` once the crawler succeeded and produced exactly
          one matching table.
        * HTTP 404 when the crawler does not exist.
        * HTTP 500 ``{status: "FAILED", error_message, ...}`` on any
          failure mode — crawler ``LastCrawl.Status == "FAILED"`` /
          ``"CANCELLED"``, zero tables matched, or multiple tables
          matched. The error message is descriptive enough for the
          frontend to surface verbatim.

    Args:
        event: API Gateway proxy event.
        context: Lambda context (unused, kept for AWS handler signature).

    Returns:
        API Gateway response dict (see :func:`create_response`).
    """
    try:
        path_params = event.get("pathParameters") or {}
        crawler_name = path_params.get("crawler_name")
        if not crawler_name:
            return create_response(
                400,
                {"error": "crawler_name path parameter is required"},
            )

        table_prefix = _resolve_table_prefix(crawler_name, event.get("queryStringParameters"))
        if not table_prefix:
            return create_response(
                400,
                {
                    "error": (
                        "table_prefix query parameter is required when "
                        "crawler_name does not match the agentic-data-ingestion-discover-* "
                        "convention"
                    )
                },
            )

        try:
            status, schema, glue_database, glue_table, error_message = _poll_discovery_crawler(
                crawler_name, table_prefix
            )
        except glue_client.exceptions.EntityNotFoundException:
            return create_response(
                404,
                {"error": f"Crawler not found: {crawler_name}"},
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to poll discovery crawler %s: %s", crawler_name, exc)
            return handle_error(exc, "Failed to poll discovery crawler")

        # Diagnostic logging: the status response was previously never
        # logged, so a wizard-side "did not complete within 5 minutes"
        # timeout could not be correlated with what the poll actually
        # returned. Log the classified status + key fields (never schema
        # column VALUES) so a stuck poll is triagable from CloudWatch.
        logger.info(
            "Discovery poll result: crawler=%s status=%s table_prefix=%s glue_table=%s schema_cols=%s error=%s",
            crawler_name,
            status,
            table_prefix,
            glue_table,
            len(schema) if schema is not None else None,
            error_message,
        )

        return _build_status_response(
            crawler_name=crawler_name,
            status=status,
            schema=schema,
            glue_database=glue_database,
            glue_table=glue_table,
            error_message=error_message,
            table_prefix=table_prefix,
        )

    except Exception as exc:  # pragma: no cover - last-resort safety net
        return handle_error(exc, "Unexpected error polling JDBC schema discovery")


# ---------------------------------------------------------------------------
# Public handler — POST /api/resources/jdbc/detect-target-schema (P19)
# ---------------------------------------------------------------------------


def detect_target_schema_start(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Start a target-side Glue Crawler-based schema discovery.

    POST /api/resources/jdbc/detect-target-schema

    Mirrors :func:`detect_schema_start` byte-for-byte except for the
    body's variable names: target-side discoveries operate on
    ``target_config.database_name`` / ``target_config.table_name``
    where source-side discoveries used
    ``source_config.jdbc_schema`` / ``source_config.jdbc_table``.

    To keep :func:`_compute_discovery_hash` the single source of truth
    (P19 Req 4.3) we project the target-side identifiers into the
    existing ``jdbc_schema`` / ``jdbc_table`` slots before passing the
    config to the hash function — so a target-side discovery against
    ``(connection X, db Y, table Z)`` produces the same crawler name as
    a source-side discovery against ``(connection X, schema Y, table Z)``.
    Hash collisions across the two sides are intended (the same table
    → the same crawler) and produce the same crawl result.

    Request body (JSON):
        ``{"target_config": {<JDBC target config>}}``

    Target_config fields consumed:
        Always: ``database_name``, ``table_name``.
        ``select_rds`` mode (no ``glue_connection_name``):
            ``secret_arn``, ``jdbc_url``, ``jdbc_driver``, ``subnet_id``,
            ``security_group_ids`` (list[str]), optional
            ``availability_zone``.
        ``glue_connection`` mode: ``glue_connection_name``.

    Response shapes match the source-side endpoint exactly so the
    frontend can reuse the same ``DetectStartResponse`` /
    ``DetectStatusResponse`` types (P19 Req 4.7-4.9):
        * HTTP 202 ``{"crawler_name", "glue_database", "table_prefix",
          "status": "RUNNING"}`` on successful crawler start.
        * HTTP 400 with ``{"error"}`` on validation, missing-config, or
          S3-endpoint pre-flight failure — the pre-flight now runs in
          ``glue_connection`` mode too (see the module docstring).
        * HTTP 400 with ``{"error", "reason", "details"}`` when Glue
          rejects the crawler definition, or cannot find the named
          connection at crawler-creation time.
        * HTTP 500 with ``{"error", "details"}`` on any other AWS API
          failure; crawler-creation failures additionally carry
          ``reason``. See :func:`_crawler_failure_response`.

    Args:
        event: API Gateway proxy event.
        context: Lambda context (unused, kept for AWS handler signature).

    Returns:
        API Gateway response dict (see :func:`create_response`).
    """
    try:
        # ------------------------------------------------------------ parse
        raw_body = event.get("body") or "{}"
        try:
            body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
        except json.JSONDecodeError:
            return create_response(400, {"error": "Request body must be valid JSON"})

        target_config = body.get("target_config")
        if not isinstance(target_config, dict):
            return create_response(
                400,
                {"error": "target_config is required and must be an object"},
            )

        database_name = target_config.get("database_name")
        table_name = target_config.get("table_name")
        if not database_name or not table_name:
            return create_response(
                400,
                {"error": ("target_config.database_name and target_config.table_name are required")},
            )

        # ------------------------------------------------------- validate ids
        # Same regex P17 enforces source-side (P19 Req 4.10). Run it
        # server-side as defense-in-depth even though the wizard
        # validates identically before submitting.
        try:
            validate_identifier(database_name)
            validate_identifier(table_name)
        except ValueError as exc:
            return create_response(400, {"error": str(exc)})

        # ----------------------------------------------------------- hash
        # Project target-side identifiers into the source-side slots
        # before hashing so the hash function stays single-source-of-truth
        # (P19 Req 4.3, Property 4). The dict copy is intentional — we
        # don't want to mutate the caller's body.
        hash_input = dict(target_config)
        hash_input["jdbc_schema"] = database_name
        hash_input["jdbc_table"] = table_name
        hash_value = _compute_discovery_hash(hash_input)
        crawler_name = f"agentic-data-ingestion-discover-{hash_value}"
        table_prefix = f"agentic_data_ingestion_{hash_value}_"
        iso_now = datetime.now(UTC).isoformat()
        tags: dict[str, str] = {
            _DISCOVERY_TAG_KEY: _DISCOVERY_TAG_VALUE,
            "CreatedAt": iso_now,
            "Hash": hash_value,
        }

        # -------------------------------------------- determine connection
        # Shared with the source-side handler (P19 Req 4.5 / 4.6). The two used
        # to carry near-duplicate copies of this block, which is how the
        # glue_connection-mode S3 pre-flight gap came to exist in both.
        resolved = _resolve_discovery_connection(
            target_config,
            config_key="target_config",
            side="Target",
            hash_value=hash_value,
            tags=tags,
        )
        if resolved.error_response is not None:
            return resolved.error_response
        connection_name = resolved.connection_name

        # -------------------------------------------------- create crawler
        try:
            _create_or_update_crawler(
                crawler_name=crawler_name,
                connection_name=connection_name,
                schema=database_name,
                table=table_name,
                table_prefix=table_prefix,
                tags=tags,
            )
        except (ClientError, BotoCoreError, RuntimeError) as exc:
            return _crawler_failure_response(
                exc,
                crawler_name=crawler_name,
                connection_name=connection_name,
                glue_connection_mode=bool(target_config.get("glue_connection_name")),
                preflight_note=resolved.preflight_note,
            )

        # --------------------------------------------------- start crawler
        try:
            glue_client.start_crawler(Name=crawler_name)
            logger.info("Started target discovery crawler: %s", crawler_name)
        except glue_client.exceptions.CrawlerRunningException:
            # Concurrent click — there is already a run in flight for this
            # exact crawler. Fall through and let the frontend poll: the
            # in-flight run produces the same table either way.
            logger.info(
                "Crawler %s is already running; continuing with existing run",
                crawler_name,
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error(
                "Failed to start target discovery crawler %s: %s",
                crawler_name,
                exc,
            )
            return handle_error(exc, "Failed to start discovery crawler")

        return create_response(
            202,
            {
                "crawler_name": crawler_name,
                "glue_database": DISCOVERY_DB,
                "table_prefix": table_prefix,
                "status": "RUNNING",
            },
        )

    except Exception as exc:  # pragma: no cover - last-resort safety net
        return handle_error(exc, "Unexpected error starting JDBC target schema discovery")


# ---------------------------------------------------------------------------
# Public handler — GET /api/resources/jdbc/detect-target-schema/{crawler_name}
# ---------------------------------------------------------------------------


def detect_target_schema_status(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Poll the wizard-time target-side discovery crawler.

    GET /api/resources/jdbc/detect-target-schema/{crawler_name}

    Functionally identical to :func:`detect_schema_status` — both
    endpoints share the underlying :func:`_poll_discovery_crawler`
    helper and the same response shapes (P19 Req 4.7-4.9). The
    duplication exists because target-side and source-side polling are
    routed under different URL paths so the frontend can keep its
    React Query keys / mutations distinct, but the wire-shape contract
    is intentionally identical.

    Path parameters:
        ``crawler_name``: Crawler returned by
        :func:`detect_target_schema_start`.

    Query parameters:
        ``table_prefix`` (optional): Default derived from ``crawler_name``
        when absent — same logic as the source-side endpoint.

    Response:
        * HTTP 202 ``{status: "RUNNING", crawler_name, glue_database,
          table_prefix}`` while the crawler is still in flight.
        * HTTP 200 ``{status: "READY", schema, glue_database,
          glue_table}`` once the crawler succeeded with exactly one
          matching table.
        * HTTP 404 when the crawler does not exist.
        * HTTP 500 ``{status: "FAILED", error_message, ...}`` on any
          other terminal state.

    Args:
        event: API Gateway proxy event.
        context: Lambda context (unused, kept for AWS handler signature).

    Returns:
        API Gateway response dict (see :func:`create_response`).
    """
    try:
        path_params = event.get("pathParameters") or {}
        crawler_name = path_params.get("crawler_name")
        if not crawler_name:
            return create_response(
                400,
                {"error": "crawler_name path parameter is required"},
            )

        table_prefix = _resolve_table_prefix(crawler_name, event.get("queryStringParameters"))
        if not table_prefix:
            return create_response(
                400,
                {
                    "error": (
                        "table_prefix query parameter is required when "
                        "crawler_name does not match the agentic-data-ingestion-discover-* "
                        "convention"
                    )
                },
            )

        try:
            status, schema, glue_database, glue_table, error_message = _poll_discovery_crawler(
                crawler_name, table_prefix
            )
        except glue_client.exceptions.EntityNotFoundException:
            return create_response(
                404,
                {"error": f"Crawler not found: {crawler_name}"},
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error(
                "Failed to poll target discovery crawler %s: %s",
                crawler_name,
                exc,
            )
            return handle_error(exc, "Failed to poll discovery crawler")

        return _build_status_response(
            crawler_name=crawler_name,
            status=status,
            schema=schema,
            glue_database=glue_database,
            glue_table=glue_table,
            error_message=error_message,
            table_prefix=table_prefix,
        )

    except Exception as exc:  # pragma: no cover - last-resort safety net
        return handle_error(exc, "Unexpected error polling JDBC target schema discovery")


# ---------------------------------------------------------------------------
# Cleanup janitor — invoked by EventBridge daily at 03:00 UTC (Task 4.3)
# ---------------------------------------------------------------------------

# How long a wizard-time discovery resource lives before the janitor
# expires it. The wizard-time crawler / connection / table set is
# only useful while the user is still in the Create-Pipeline flow;
# once they finish (a pipeline is created, which uses the per-pipeline
# runtime crawler instead) or abandon the session, these resources
# accrue cost and clutter without serving a purpose. Seven days is the
# value Requirement 4.11 contracts — long enough that a paused multi-day
# wizard session does not lose its detected schema, short enough that
# abandoned attempts do not linger indefinitely.
_DISCOVERY_TTL = timedelta(days=7)


def _build_crawler_arn(crawler_name: str) -> str:
    """Return the Glue Crawler ARN for tag lookups.

    :func:`glue:GetTags` requires a full ARN; :func:`glue:GetCrawlers`
    only returns the crawler ``Name``, so we synthesise the ARN locally
    rather than spending a round-trip on :func:`glue:GetCrawler` per
    crawler. The format is fixed by the Glue service contract:
    ``arn:aws:glue:{region}:{account_id}:crawler/{name}``.

    Account ID is resolved via the shared :func:`env_utils.get_aws_account_id`
    helper (re-exported through ``.common``) which caches the STS
    ``GetCallerIdentity`` result on first use, matching the pattern already
    established in this Lambda's ``upload.py`` and ``pipelines.py``.
    """
    account_id = get_aws_account_id()
    return f"arn:aws:glue:{AWS_REGION}:{account_id}:crawler/{crawler_name}"


def _is_expired_discovery_crawler(crawler: dict[str, Any], now: datetime) -> bool:
    """Decide whether a crawler is an expired discovery crawler.

    A crawler is eligible for cleanup when ALL of the following hold:

        1. It carries the ``Type=agentic-data-ingestion-discovery`` tag — runtime
           per-pipeline crawlers (tagged ``agentic-data-ingestion-runtime``) are
           explicitly NOT touched here. Their lifecycle is tied to the
           pipeline, not the wizard session.
        2. It is NOT currently running. Glue refuses to delete a
           ``RUNNING``/``STOPPING`` crawler with ``CrawlerRunningException``
           anyway, so this is a defensive shortcut that avoids the
           spurious error and the next-day retry.
        3. Its ``CreationTime`` is older than :data:`_DISCOVERY_TTL`.
           ``CreationTime`` is on the boto3 :class:`Crawler` payload as
           a timezone-aware :class:`datetime`. We use this rather than
           the ``CreatedAt`` tag (which we set on creation in
           :func:`detect_schema_start`) because it is set by Glue
           itself and cannot drift if a tag rewrite ever fails.

    Args:
        crawler: One element of :func:`glue:GetCrawlers`'s ``Crawlers``
            list, augmented with a ``_tags`` dict (populated by the
            caller via :func:`glue:GetTags`).
        now: Current time, supplied for deterministic testing.

    Returns:
        ``True`` if the crawler is a :data:`_DISCOVERY_TTL`-expired
        discovery crawler that is safe to delete now.
    """
    tags = crawler.get("_tags") or {}
    if tags.get(_DISCOVERY_TAG_KEY) != _DISCOVERY_TAG_VALUE:
        return False

    state = crawler.get("State", "")
    if state in ("RUNNING", "STOPPING"):
        return False

    creation_time = crawler.get("CreationTime")
    if not isinstance(creation_time, datetime):
        # Glue always populates CreationTime, but defend against a
        # malformed payload anyway — without a creation timestamp we
        # cannot prove the crawler is older than the TTL, so we err on
        # the side of leaving it alone.
        return False

    return now - creation_time > _DISCOVERY_TTL


def _delete_discovery_crawler_resources(
    crawler_name: str,
    summary: dict[str, Any],
) -> None:
    """Delete one crawler and its associated wizard-time resources.

    Order of operations matters for idempotency:

        1. Delete the **crawler** first. Once the crawler is gone, no
           future crawl can re-populate the connection or tables.
        2. Delete the **connection** (``agentic-data-ingestion-discover-{hash}``).
           This is best-effort: in ``glue_connection`` mode the
           connection never existed, so :class:`EntityNotFoundException`
           is the expected case there. We DO NOT delete user-owned Glue
           connections — only the AgenticDataIngestion-managed
           ``agentic-data-ingestion-discover-*`` ones, which we name deterministically
           from the crawler name.
        3. Delete any **tables** matching ``agentic_data_ingestion_{hash}_*`` in the
           discovery database. Glue creates exactly one in the happy
           path but the wildcard sweep handles the corner case where a
           prior run left orphaned tables.

    Each delete is wrapped in ``try``/``except`` so a single AWS error
    does not abort the cleanup of an unrelated crawler — the caller
    iterates across many crawlers and we want maximum throughput per
    janitor run. Failures are appended to ``summary["errors"]``.

    Args:
        crawler_name: The crawler being cleaned up. Must match the
            ``agentic-data-ingestion-discover-{hash}`` convention so the connection
            name and table prefix can be derived from it.
        summary: Mutable counters / errors dict; updated in place.
    """
    expected_prefix = "agentic-data-ingestion-discover-"
    if not crawler_name.startswith(expected_prefix):
        # Defensive: the tag filter alone should prevent this, but we
        # NEVER want this janitor to delete a non-AgenticDataIngestion crawler
        # because of a misconfigured tag — pin to the exact name shape.
        msg = f"Refusing to clean up {crawler_name!r}: name does not match agentic-data-ingestion-discover-* convention"
        logger.warning(msg)
        summary["errors"].append({"crawler_name": crawler_name, "error": msg})
        return

    hash_value = crawler_name[len(expected_prefix) :]
    connection_name = crawler_name  # same naming convention by design
    table_prefix = f"agentic_data_ingestion_{hash_value}_"

    # ---- 1. delete crawler -------------------------------------------------
    try:
        glue_client.delete_crawler(Name=crawler_name)
        summary["crawlers_deleted"] += 1
        logger.info("Deleted expired discovery crawler: %s", crawler_name)
    except glue_client.exceptions.EntityNotFoundException:
        # Already gone (a prior janitor run partially completed). Treat
        # as a no-op and continue to the connection / tables — they may
        # still need cleanup.
        logger.info(
            "Crawler %s already absent — continuing with associated resources",
            crawler_name,
        )
    except glue_client.exceptions.CrawlerRunningException as exc:
        # Race: the crawler started running between our STATE check and
        # this delete. Skip and let the next daily run pick it up.
        logger.info(
            "Crawler %s started running before delete; skipping this cycle: %s",
            crawler_name,
            exc,
        )
        summary["errors"].append({"crawler_name": crawler_name, "error": "running at delete time"})
        return
    except (ClientError, BotoCoreError) as exc:
        logger.error("Failed to delete crawler %s: %s", crawler_name, exc)
        summary["errors"].append({"crawler_name": crawler_name, "error": str(exc)})
        # Continue anyway — the connection / tables are independent.

    # ---- 2. delete connection ---------------------------------------------
    try:
        glue_client.delete_connection(ConnectionName=connection_name)
        summary["connections_deleted"] += 1
        logger.info("Deleted discovery connection: %s", connection_name)
    except glue_client.exceptions.EntityNotFoundException:
        # Expected for ``glue_connection`` mode (no AgenticDataIngestion connection
        # was ever created) and for partially-cleaned-up state.
        logger.debug(
            "Connection %s not found (expected for glue_connection mode)",
            connection_name,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error("Failed to delete connection %s: %s", connection_name, exc)
        summary["errors"].append({"connection_name": connection_name, "error": str(exc)})

    # ---- 3. delete tables --------------------------------------------------
    try:
        tables_response = glue_client.get_tables(
            DatabaseName=DISCOVERY_DB,
            Expression=table_prefix + "*",
        )
    except glue_client.exceptions.EntityNotFoundException:
        # The whole discovery database is missing — nothing to clean up.
        logger.debug("Discovery database %s not found; no tables to delete", DISCOVERY_DB)
        return
    except (ClientError, BotoCoreError) as exc:
        logger.error(
            "Failed to list tables for prefix %s in %s: %s",
            table_prefix,
            DISCOVERY_DB,
            exc,
        )
        summary["errors"].append({"table_prefix": table_prefix, "error": str(exc)})
        return

    for table in tables_response.get("TableList", []) or []:
        table_name = table.get("Name")
        if not table_name:
            continue
        try:
            glue_client.delete_table(DatabaseName=DISCOVERY_DB, Name=table_name)
            summary["tables_deleted"] += 1
            logger.info("Deleted discovery table: %s.%s", DISCOVERY_DB, table_name)
        except glue_client.exceptions.EntityNotFoundException:
            # Concurrent cleanup — fine.
            logger.debug("Table %s.%s already absent", DISCOVERY_DB, table_name)
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to delete table %s.%s: %s", DISCOVERY_DB, table_name, exc)
            summary["errors"].append(
                {
                    "table_name": f"{DISCOVERY_DB}.{table_name}",
                    "error": str(exc),
                }
            )


#: The exact name `_resolve_discovery_connection` gives a Detect Schema connection:
#: the fixed prefix plus the 12 lowercase hex characters of the discovery hash.
#: Exact, not a prefix test, because the orphan sweep deletes what matches.
_DISCOVERY_CONNECTION_NAME_RE = re.compile(r"\Aagentic-data-ingestion-discover-[0-9a-f]{12}\Z")


def _sweep_orphaned_discovery_connections(
    crawler_names: set[str],
    now: datetime,
    summary: dict[str, Any],
) -> None:
    """Delete Detect Schema connections whose crawler no longer exists.

    The crawler-keyed pass in :func:`cleanup_discovery_resources` deletes a
    connection only together with its crawler, so a connection outlives it
    whenever that pairing breaks: the connection delete failed after the
    crawler delete succeeded, or the wizard created the connection and then
    failed to create the crawler. Nothing revisited those, and the "Use Glue
    Connection" picker used to list every one of them.

    A connection is deleted only when ALL of these hold:

        1. Its name matches :data:`_DISCOVERY_CONNECTION_NAME_RE` exactly, so a
           user's connection, a pipeline's ``-source``/``-rds`` connection and
           any pre-rename name are never touched.
        2. No crawler of the same name exists (the wizard names both alike).
           While the crawler exists, the crawler-keyed pass owns the pair.
        3. Both its ``CreationTime`` and its ``LastUpdatedTime`` are older than
           :data:`_DISCOVERY_TTL`. A re-run of Detect Schema updates the
           connection in place before creating the crawler, so a recent update
           means a detection may be in flight. A connection with neither
           timestamp is left alone, as the crawler pass does.

    Only the connection is deleted. Its crawler is already gone, and the
    discovery tables were the crawler's to remove; deleting a crawler of that
    name here would race a detection started between listing and delete.
    Deletions count toward ``summary["connections_deleted"]`` and failures go to
    ``summary["errors"]``, so the summary keeps the shape callers already read.
    """
    try:
        paginator = glue_client.get_paginator("get_connections")
        connections = [
            conn
            for page in paginator.paginate(Filter={"ConnectionType": "JDBC"}, HidePassword=True)
            for conn in (page.get("ConnectionList", []) or [])
        ]
    except (ClientError, BotoCoreError) as exc:
        logger.error("Failed to list Glue connections for the orphan sweep: %s", exc)
        summary["errors"].append({"phase": "list_connections", "error": str(exc)})
        return

    orphaned = 0
    for conn in connections:
        name = conn.get("Name") or ""
        if not _DISCOVERY_CONNECTION_NAME_RE.match(name) or name in crawler_names:
            continue
        stamps = [t for t in (conn.get("CreationTime"), conn.get("LastUpdatedTime")) if isinstance(t, datetime)]
        if not stamps or now - max(stamps) <= _DISCOVERY_TTL:
            continue
        orphaned += 1
        try:
            glue_client.delete_connection(ConnectionName=name)
            summary["connections_deleted"] += 1
            logger.info("Deleted orphaned discovery connection (no crawler): %s", name)
        except glue_client.exceptions.EntityNotFoundException:
            logger.debug("Orphaned discovery connection %s already absent", name)
        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to delete orphaned discovery connection %s: %s", name, exc)
            summary["errors"].append({"connection_name": name, "error": str(exc)})

    logger.info("Discovery cleanup orphan sweep found %d connection(s) with no crawler", orphaned)


def cleanup_discovery_resources() -> dict[str, Any]:
    """Daily janitor that expires wizard-time discovery resources.

    Invoked from :func:`lambda_function.lambda_handler` whenever the
    inbound event is the EventBridge schedule rule
    ``agentic-data-ingestion-discovery-cleanup-{stage}`` (detected via
    ``event.get("source") == "aws.events"`` — see Task 4.4).

    Algorithm:

        1. Paginate :func:`glue:GetCrawlers` (Glue caps each page at 100
           crawlers; large accounts can have hundreds).
        2. For each crawler, fetch its tags via
           :func:`glue:GetTags(ResourceArn=...)`. Glue does NOT inline
           tags on ``GetCrawlers``, so this round-trip is unavoidable.
        3. Filter to crawlers that are (a) tagged
           ``Type=agentic-data-ingestion-discovery``, (b) not currently running,
           and (c) older than 7 days.
        4. For each match, delete the crawler, the matching
           ``agentic-data-ingestion-discover-{hash}`` connection (404 OK), and any
           tables matching ``agentic_data_ingestion_{hash}_*`` in the discovery DB.
        5. Sweep the connections: delete any ``agentic-data-ingestion-discover-{hash}``
           connection with no crawler of its name, once it is older than the TTL
           (:func:`_sweep_orphaned_discovery_connections`).

    The handler is fully idempotent — running it twice in a row yields
    the same end state, and partial failures on one resource do not
    abort the cleanup of others. This is critical because EventBridge
    may retry on Lambda errors and the AWS account may have hundreds of
    crawlers to inspect; one transient ``ThrottlingException`` should
    not poison the whole run.

    Returns:
        A summary dict suitable for both unit testing and structured
        logging::

            {
                "crawlers_deleted": int,
                "connections_deleted": int,
                "tables_deleted": int,
                "errors": list[dict],   # one entry per resource that failed
            }
    """
    summary: dict[str, Any] = {
        "crawlers_deleted": 0,
        "connections_deleted": 0,
        "tables_deleted": 0,
        "errors": [],
    }

    now = datetime.now(UTC)

    # Paginate get_crawlers — large accounts can exceed one page.
    try:
        paginator = glue_client.get_paginator("get_crawlers")
        pages = list(paginator.paginate())
    except (ClientError, BotoCoreError) as exc:
        logger.error("Failed to list crawlers: %s", exc)
        summary["errors"].append({"phase": "list_crawlers", "error": str(exc)})
        return summary

    candidate_crawlers: list[dict[str, Any]] = []
    for page in pages:
        candidate_crawlers.extend(page.get("Crawlers", []) or [])

    logger.info(
        "Discovery cleanup janitor scanning %d total crawlers",
        len(candidate_crawlers),
    )

    # Fetch tags for each crawler (Glue does not return them inline) and
    # filter down to the AgenticDataIngestion-discovery TTL-expired set in one pass.
    expired: list[str] = []
    for crawler in candidate_crawlers:
        crawler_name = crawler.get("Name")
        if not crawler_name:
            continue
        # Cheap pre-filter on name — even before fetching tags, only
        # crawlers we conceivably manage match this prefix. This
        # avoids one ``GetTags`` round-trip per non-AgenticDataIngestion crawler
        # in busy accounts.
        if not crawler_name.startswith("agentic-data-ingestion-discover-"):
            continue
        try:
            arn = _build_crawler_arn(crawler_name)
            tags_response = glue_client.get_tags(ResourceArn=arn)
            crawler["_tags"] = tags_response.get("Tags", {}) or {}
        except (ClientError, BotoCoreError) as exc:
            logger.warning(
                "Failed to fetch tags for crawler %s: %s — skipping",
                crawler_name,
                exc,
            )
            summary["errors"].append({"crawler_name": crawler_name, "error": f"get_tags: {exc}"})
            continue

        if _is_expired_discovery_crawler(crawler, now):
            expired.append(crawler_name)

    logger.info(
        "Discovery cleanup janitor found %d expired discovery crawlers",
        len(expired),
    )

    for crawler_name in expired:
        _delete_discovery_crawler_resources(crawler_name, summary)

    # Second pass, keyed on CONNECTIONS: a discovery connection whose crawler is
    # already gone. The pass above starts from crawlers, so it can never reach
    # one. `crawler_names` is every crawler listed at the start of this run, so a
    # crawler the first pass just deleted still counts as present here, and its
    # connection, if that delete failed, is retried on the next run.
    _sweep_orphaned_discovery_connections(
        {str(c.get("Name")) for c in candidate_crawlers if c.get("Name")},
        now,
        summary,
    )

    logger.info(
        "Discovery cleanup janitor complete: %d crawlers, %d connections, %d tables deleted, %d errors",
        summary["crawlers_deleted"],
        summary["connections_deleted"],
        summary["tables_deleted"],
        len(summary["errors"]),
    )

    return summary
