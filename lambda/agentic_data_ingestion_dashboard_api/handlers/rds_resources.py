# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
API handlers for listing RDS instances, Aurora clusters, Glue JDBC connections,
and Secrets Manager secrets for the RDS Database Target feature.

Feature: p14-rds-database-targets
"""

import re
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# `create_response` is the shared HTTP-response builder, re-exported by
# `.common` from `shared/response_utils.py` (P21 Req 5.2). It applies the
# canonical Content-Type + CORS headers and Decimal-aware JSON encoding so
# these handlers no longer hand-roll `{"statusCode", "headers", "body"}` dicts.
from handlers.common import create_response, logger

# `jdbc_helpers` ships in the shared Lambda layer at /opt/python, as for the
# sibling handlers `jdbc_resources.py` and `pipelines_jdbc.py`.
from jdbc_helpers import is_managed_glue_connection

# Initialize AWS clients
rds_client = boto3.client("rds")
glue_client = boto3.client("glue")
secretsmanager_client = boto3.client("secretsmanager")

# Pattern for filtering secrets by name. Aligned with P20's broader
# JDBC_CANDIDATE_PATTERN so the legacy `select_rds`-mode Secret dropdown
# also surfaces secrets named after the engine (e.g. test-fixtures'
# `agentic-data-ingestion/test-fixtures/mysql-source`). The same regex is now used in
# both this endpoint and `list_jdbc_candidate_secrets`; they remain
# separate functions because they project different response shapes
# (RdsSecret with `last_changed_date` vs the lighter Secret shape).
SECRET_NAME_PATTERN = re.compile(
    r"db|database|rds|jdbc|postgres|mysql|oracle|sqlserver",
    re.IGNORECASE,
)

# P20 Req 4.7 — broader pattern for the manual-mode JDBC-candidate
# secrets dropdown. Adds engine-name keywords so the user finds their
# Postgres / MySQL / Oracle / SQL Server credential secrets even when
# the secret name doesn't include the generic "rds" / "db" keywords.
JDBC_CANDIDATE_PATTERN = re.compile(
    r"db|database|rds|jdbc|postgres|mysql|oracle|sqlserver",
    re.IGNORECASE,
)


def list_rds_instances(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    List available RDS database instances.

    GET /api/resources/rds/instances

    Each entry includes the instance's subnet IDs (from its
    DBSubnetGroup) and security group IDs (from VpcSecurityGroups).
    The frontend uses these to populate `source_config.subnet_id` /
    `source_config.security_group_ids` when the user picks an
    instance, so the schema-discovery and connection-provisioning
    backends can build PhysicalConnectionRequirements without an
    extra round-trip.
    """
    try:
        logger.info("Listing RDS instances")

        response = rds_client.describe_db_instances()

        instances = []
        for db in response.get("DBInstances", []):
            if db.get("DBInstanceStatus") != "available":
                continue
            endpoint = db.get("Endpoint") or {}
            db_subnet_group = db.get("DBSubnetGroup") or {}
            # Subnet group exposes a list of {SubnetIdentifier,
            # SubnetAvailabilityZone, SubnetStatus, ...} entries. Filter
            # to active subnets and pick the first AZ as the canonical
            # availability_zone for the instance (RDS instances live in
            # a single AZ; the subnet group is multi-AZ for failover).
            subnets_in_group = db_subnet_group.get("Subnets", []) or []
            subnet_ids = [s.get("SubnetIdentifier", "") for s in subnets_in_group if s.get("SubnetIdentifier")]
            availability_zone = ""
            for s in subnets_in_group:
                az = s.get("SubnetAvailabilityZone") or {}
                az_name = az.get("Name") if isinstance(az, dict) else az
                if az_name:
                    availability_zone = az_name
                    break

            security_group_ids = [
                sg.get("VpcSecurityGroupId", "")
                for sg in db.get("VpcSecurityGroups", []) or []
                if sg.get("VpcSecurityGroupId") and sg.get("Status") in ("active", "adding")
            ]

            instances.append(
                {
                    "db_instance_identifier": db.get("DBInstanceIdentifier", ""),
                    "engine": db.get("Engine", ""),
                    "engine_version": db.get("EngineVersion", ""),
                    "endpoint_address": endpoint.get("Address", ""),
                    "endpoint_port": endpoint.get("Port", 0),
                    "db_name": db.get("DBName", ""),
                    "db_instance_status": db.get("DBInstanceStatus", ""),
                    "vpc_id": db_subnet_group.get("VpcId", ""),
                    "subnet_ids": subnet_ids,
                    "security_group_ids": security_group_ids,
                    "availability_zone": availability_zone,
                }
            )

        logger.info(f"Found {len(instances)} available RDS instances")

        return create_response(200, {"instances": instances, "count": len(instances)})

    except (ClientError, BotoCoreError) as e:
        logger.error(f"Error listing RDS instances: {e}", exc_info=True)
        return create_response(500, {"error": "Failed to list RDS instances", "message": str(e)})


def list_rds_clusters(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    List available Aurora DB clusters.

    GET /api/resources/rds/clusters

    Each entry includes the cluster's subnet IDs (resolved via
    `describe_db_subnet_groups`) and security group IDs (from
    VpcSecurityGroups). Same purpose as list_rds_instances — populate
    the wizard's source_config so the discovery backend has a full
    PhysicalConnectionRequirements without re-describing.
    """
    try:
        logger.info("Listing Aurora DB clusters")

        response = rds_client.describe_db_clusters()

        # Collect distinct DBSubnetGroup names so we can resolve their
        # subnet IDs in a single follow-up call.
        subnet_group_names = {
            cluster.get("DBSubnetGroup") for cluster in response.get("DBClusters", []) if cluster.get("DBSubnetGroup")
        }
        subnet_group_to_subnets: dict[str, dict[str, Any]] = {}
        for sg_name in subnet_group_names:
            try:
                sg_response = rds_client.describe_db_subnet_groups(
                    DBSubnetGroupName=sg_name,
                )
                for sg in sg_response.get("DBSubnetGroups", []):
                    subnet_ids = [
                        s.get("SubnetIdentifier", "") for s in sg.get("Subnets", []) or [] if s.get("SubnetIdentifier")
                    ]
                    az = ""
                    for s in sg.get("Subnets", []) or []:
                        az_field = s.get("SubnetAvailabilityZone") or {}
                        az_name = az_field.get("Name") if isinstance(az_field, dict) else az_field
                        if az_name:
                            az = az_name
                            break
                    subnet_group_to_subnets[sg_name] = {
                        "vpc_id": sg.get("VpcId", ""),
                        "subnet_ids": subnet_ids,
                        "availability_zone": az,
                    }
            except (ClientError, BotoCoreError) as exc:
                logger.warning(
                    "Failed to describe DBSubnetGroup %s: %s — cluster's subnet_ids will be empty",
                    sg_name,
                    exc,
                )

        clusters = []
        for cluster in response.get("DBClusters", []):
            if cluster.get("Status") != "available":
                continue
            sg_name = cluster.get("DBSubnetGroup", "")
            sg_info = subnet_group_to_subnets.get(sg_name, {})

            security_group_ids = [
                sg.get("VpcSecurityGroupId", "")
                for sg in cluster.get("VpcSecurityGroups", []) or []
                if sg.get("VpcSecurityGroupId") and sg.get("Status") in ("active", "adding")
            ]

            clusters.append(
                {
                    "db_cluster_identifier": cluster.get("DBClusterIdentifier", ""),
                    "engine": cluster.get("Engine", ""),
                    "engine_version": cluster.get("EngineVersion", ""),
                    "endpoint": cluster.get("Endpoint", ""),
                    "port": cluster.get("Port", 0),
                    "database_name": cluster.get("DatabaseName", ""),
                    "status": cluster.get("Status", ""),
                    "vpc_id": sg_info.get("vpc_id", ""),
                    "subnet_ids": sg_info.get("subnet_ids", []),
                    "security_group_ids": security_group_ids,
                    "availability_zone": sg_info.get("availability_zone", ""),
                }
            )

        logger.info(f"Found {len(clusters)} available Aurora clusters")

        return create_response(200, {"clusters": clusters, "count": len(clusters)})

    except (ClientError, BotoCoreError) as e:
        logger.error(f"Error listing Aurora clusters: {e}", exc_info=True)
        return create_response(500, {"error": "Failed to list Aurora clusters", "message": str(e)})


def _derive_jdbc_driver_from_url(jdbc_url: str) -> str:
    """Return the driver class name implied by a JDBC URL prefix.

    AgenticDataIngestion's canonical Glue Connection provisioner
    (``shared/jdbc_helpers.py::create_glue_jdbc_connection``) intentionally
    omits ``JDBC_DRIVER_CLASS_NAME`` from connection properties because Glue
    auto-detects the driver from the URL prefix for built-in engines. The
    wizard, however, still needs a concrete driver string to pre-fill the
    "JDBC Driver" input when the user selects an existing Glue Connection.

    This helper recovers that string by inspecting the URL prefix. Returns
    an empty string for unrecognised URLs so the wizard's existing "manual
    override" affordance still works.

    The mapping mirrors :data:`shared.jdbc_helpers.JDBC_DRIVER_MAP` keyed
    by URL protocol rather than engine name. We avoid importing the shared
    map here because this file is bundled into the dashboard API Lambda
    and the keys we need are a strict subset.
    """
    if not isinstance(jdbc_url, str) or not jdbc_url.startswith("jdbc:"):
        return ""
    # Strip the leading ``jdbc:`` then split off the protocol up to the
    # first ``:`` or ``//`` boundary.
    after = jdbc_url[len("jdbc:") :]
    # Special-case Oracle's ``jdbc:oracle:thin:@host:port/svc`` form —
    # ``oracle:thin:@`` rather than a URL-style ``oracle://`` prefix.
    if after.startswith("oracle:"):
        return "oracle.jdbc.OracleDriver"
    protocol = after.split(":", 1)[0].split("/", 1)[0]
    return {
        "postgresql": "org.postgresql.Driver",
        "mysql": "com.mysql.cj.jdbc.Driver",
        "mariadb": "org.mariadb.jdbc.Driver",
        "sqlserver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
    }.get(protocol, "")


def list_glue_jdbc_connections(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    List the Glue JDBC connections a user can pick in "Use Glue Connection" mode.

    GET /api/resources/rds/glue-connections

    Connections this system created for its own use are left out
    (:func:`jdbc_helpers.is_managed_glue_connection`): each pipeline's ``-source``
    and ``-rds`` connection, and the wizard's ``discover-`` connections. That mode
    is for a connection the user owns. A ``-source`` connection is deleted with
    its pipeline and a ``discover-`` one by the discovery janitor, so a second
    pipeline that picked one would break when its owner went. Before this filter
    the picker listed every one of them, which in a dev account was most of the
    list.

    Every page is read (``NextToken``), and ``HidePassword`` keeps the inline
    ``PASSWORD`` of a user connection that carries one out of this Lambda. Only
    the URL and driver class are read from the properties.
    """
    try:
        logger.info("Listing Glue JDBC connections")

        connections = []
        managed_skipped = 0
        request: dict[str, Any] = {"Filter": {"ConnectionType": "JDBC"}, "HidePassword": True}
        while True:
            response = glue_client.get_connections(**request)
            for conn in response.get("ConnectionList", []):
                name = conn.get("Name", "")
                if is_managed_glue_connection(name, conn.get("Description")):
                    managed_skipped += 1
                    continue
                props = conn.get("ConnectionProperties", {})
                jdbc_url = props.get("JDBC_CONNECTION_URL", "")
                # AgenticDataIngestion-managed connections (and the test-fixtures
                # connections, post-cleanup) intentionally do not store the
                # driver class name — Glue auto-detects it at runtime from
                # the URL prefix. Derive it here so the wizard still has a
                # concrete value to pre-fill in the "JDBC Driver" input.
                jdbc_driver = props.get("JDBC_DRIVER_CLASS_NAME") or _derive_jdbc_driver_from_url(jdbc_url)
                connections.append(
                    {
                        "name": name,
                        "connection_type": conn.get("ConnectionType", ""),
                        "jdbc_url": jdbc_url,
                        "jdbc_driver": jdbc_driver,
                        "status": conn.get("Status", "READY"),
                    }
                )
            next_token = response.get("NextToken")
            if not next_token:
                break
            request["NextToken"] = next_token

        logger.info(
            f"Found {len(connections)} Glue JDBC connections "
            f"({managed_skipped} created by this system for its own use, not listed)"
        )

        return create_response(200, {"connections": connections, "count": len(connections)})

    except (ClientError, BotoCoreError) as e:
        logger.error(f"Error listing Glue JDBC connections: {e}", exc_info=True)
        return create_response(500, {"error": "Failed to list Glue JDBC connections", "message": str(e)})


def list_rds_secrets(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    List Secrets Manager secrets, filtered by database-related name patterns.

    GET /api/resources/rds/secrets?show_all=true

    By default, only secrets whose name matches the broadened
    database-keyword pattern (P20-aligned: ``db|database|rds|jdbc|
    postgres|mysql|oracle|sqlserver``) are returned. Pass
    ``show_all=true`` to return every secret the role can list.

    Pagination: ``secretsmanager:ListSecrets`` returns at most
    ``MaxResults`` items per page (default 10, max 100). We always
    request the maximum page size and follow ``NextToken`` until the
    full list is exhausted — without this, secrets on page 2+ are
    invisible to the wizard dropdown.
    """
    try:
        logger.info("Listing Secrets Manager secrets")

        query_params = event.get("queryStringParameters") or {}
        show_all = query_params.get("show_all", "").lower() == "true"

        # Walk the entire paginated SecretList. MaxResults=100 keeps
        # the round-trip count down for typical accounts.
        all_secret_entries: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"MaxResults": 100}
            if next_token:
                kwargs["NextToken"] = next_token
            response = secretsmanager_client.list_secrets(**kwargs)
            all_secret_entries.extend(response.get("SecretList", []))
            next_token = response.get("NextToken")
            if not next_token:
                break

        secrets = []
        for secret in all_secret_entries:
            name = secret.get("Name", "")

            if not show_all and not SECRET_NAME_PATTERN.search(name):
                continue

            last_changed = secret.get("LastChangedDate")
            secrets.append(
                {
                    "name": name,
                    "arn": secret.get("ARN", ""),
                    "description": secret.get("Description", ""),
                    "last_changed_date": last_changed.isoformat() if last_changed else "",
                }
            )

        logger.info(f"Found {len(secrets)} secrets (show_all={show_all})")

        return create_response(200, {"secrets": secrets, "count": len(secrets)})

    except (ClientError, BotoCoreError) as e:
        logger.error(f"Error listing secrets: {e}", exc_info=True)
        return create_response(500, {"error": "Failed to list secrets", "message": str(e)})


def list_jdbc_candidate_secrets(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """List Secrets Manager secrets matching the JDBC-credential heuristic.

    GET /api/resources/secrets/jdbc-candidates

    Filters Secrets Manager secrets server-side using
    :data:`JDBC_CANDIDATE_PATTERN` (case-insensitive) so the dashboard's
    manual-mode credential dropdown surfaces the *likely* JDBC secrets
    without scrolling through unrelated team / API tokens / etc.
    secrets.

    The filter is a UX heuristic, NOT a privilege boundary — every
    secret the dashboard role can list is fetchable via the Secrets
    Manager API directly. The wizard's "Paste ARN manually" toggle lets
    the user paste a secret whose name doesn't match the heuristic
    (P20 Req 4.3).

    Returns:
        HTTP 200 ``{secrets: [{arn, name, description}], count: N}`` on
        success. HTTP 500 on Secrets Manager failure.

    Validates: P20 Requirements 4.7
    """
    try:
        logger.info("Listing JDBC-candidate Secrets Manager secrets")
        # Walk the entire paginated SecretList. Without pagination,
        # secrets on page 2+ are invisible to the wizard dropdown
        # (Secrets Manager's default MaxResults is 10).
        all_secret_entries: list[dict[str, Any]] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"MaxResults": 100}
            if next_token:
                kwargs["NextToken"] = next_token
            response = secretsmanager_client.list_secrets(**kwargs)
            all_secret_entries.extend(response.get("SecretList", []))
            next_token = response.get("NextToken")
            if not next_token:
                break

        secrets = [
            {
                "arn": secret.get("ARN", ""),
                "name": secret.get("Name", ""),
                "description": secret.get("Description", ""),
            }
            for secret in all_secret_entries
            if JDBC_CANDIDATE_PATTERN.search(secret.get("Name", ""))
        ]
        logger.info("Found %d JDBC-candidate secrets", len(secrets))
        return create_response(200, {"secrets": secrets, "count": len(secrets)})
    except (ClientError, BotoCoreError) as exc:
        logger.error("Error listing JDBC-candidate secrets: %s", exc, exc_info=True)
        return create_response(
            500,
            {
                "error": "Failed to list JDBC candidate secrets",
                "message": str(exc),
            },
        )
