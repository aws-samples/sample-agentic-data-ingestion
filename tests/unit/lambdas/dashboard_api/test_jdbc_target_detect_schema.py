# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for JDBC target-side discovery endpoints (P19 Task 2 + tests).

Covers the two new public handlers added by P19 Tasks 2.2 and 2.3:

    * ``detect_target_schema_start``
        ``POST /api/resources/jdbc/detect-target-schema``
    * ``detect_target_schema_status``
        ``GET  /api/resources/jdbc/detect-target-schema/{name}``

Test scope (P19 Tasks 2.4-2.7):
    * Property tests for identifier validation (P6), hash equivalence
      (P4), and request-body validation per connection mode (P7).
    * Example tests for happy-path crawler creation, ``select_rds``
      connection creation, ``glue_connection`` mode reuse, identifier-
      validation 400s, missing-field 400s, and Glue API failure 500s.

Validates: P19 Requirements 4.1-4.11
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler module.
# Mirrors test_jdbc_resources.py so the dashboard API handler imports
# cleanly in a unit-test process without the real Lambda layer.
# ---------------------------------------------------------------------------

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
mock_env_utils.get_aws_account_id = lambda: "123456789012"


@pytest.fixture(autouse=True)
def _env_utils_stub(monkeypatch):
    """Install the env_utils stub for each test, and only for the test.

    The handler modules are imported inside the tests, so the stub must be in
    sys.modules then. Installed at module scope it leaked into every module
    collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    """
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)


os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Make ``shared/`` importable so ``handlers.jdbc_resources`` can resolve
# its ``from jdbc_helpers import ...`` line.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

# Add handler package to path
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_glue_role_env(monkeypatch):
    """Ensure GLUE_PIPELINE_ROLE_ARN is set before each test.

    The handler module reads this env var at import time into a
    module-level constant; patch the module attribute directly so test
    ordering doesn't matter.
    """
    monkeypatch.setattr(
        "handlers.jdbc_resources.GLUE_PIPELINE_ROLE_ARN",
        "arn:aws:iam::123456789012:role/agentic-data-ingestion-glue-pipeline-role",
    )


def _select_rds_target_body() -> dict:
    """Return a valid select_rds-mode request body for detect_target_schema_start."""
    return {
        "target_config": {
            "jdbc_url": "jdbc:postgresql://db.example.com:5432/orders",
            "jdbc_driver": "org.postgresql.Driver",
            "database_name": "analytics",
            "table_name": "users_mirror",
            "secret_arn": ("arn:aws:secretsmanager:us-west-2:123456789012:secret:db-creds-AbCdEf"),
            "subnet_id": "subnet-0abc1234def5678ab",
            "security_group_ids": ["sg-0abc1234def5678ab"],
            "availability_zone": "us-west-2a",
        }
    }


def _glue_connection_target_body() -> dict:
    """Return a valid glue_connection-mode request body."""
    return {
        "target_config": {
            "glue_connection_name": "my-existing-connection",
            "database_name": "analytics",
            "table_name": "users_mirror",
        }
    }


def _route_table_with_s3_endpoint() -> dict:
    return {
        "RouteTables": [
            {
                "Routes": [
                    {"GatewayId": "local"},
                    {
                        "GatewayId": "vpce-0abcdef1234567890",
                        "DestinationPrefixListId": "pl-68a54001",
                    },
                ]
            }
        ]
    }


def _route_table_without_s3_endpoint() -> dict:
    return {
        "RouteTables": [
            {
                "Routes": [
                    {"GatewayId": "local"},
                    {"GatewayId": "igw-0abcdef1234567890"},
                ]
            }
        ]
    }


# ---------------------------------------------------------------------------
# Example unit tests — detect_target_schema_start
# ---------------------------------------------------------------------------


class TestDetectTargetSchemaStart:
    """Tests for the POST /detect-target-schema entry point.

    Validates: P19 Requirements 4.1, 4.2, 4.4, 4.5, 4.6, 4.7, 4.10, 4.11.
    """

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_select_rds_mode_happy_path_returns_202(self, mock_ec2, mock_glue, mock_create_conn):
        """select_rds: pre-flight passes → connection + crawler created → 202.

        Validates: Req 4.4 (crawler tags/role/database), Req 4.5
        (select_rds connection creation), Req 4.7 (RUNNING response shape).
        """
        mock_ec2.describe_route_tables.return_value = _route_table_with_s3_endpoint()
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})
        # Crawler does not yet exist → helper takes the create-with-Tags path.
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")

        from handlers.jdbc_resources import detect_target_schema_start

        event = {"body": json.dumps(_select_rds_target_body())}
        response = detect_target_schema_start(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["status"] == "RUNNING"
        # Crawler name uses the deterministic agentic-data-ingestion-discover-{hash}
        # convention (Req 4.4).
        assert body["crawler_name"].startswith("agentic-data-ingestion-discover-")
        # Table prefix mirrors the same hash (Req 4.4).
        assert body["table_prefix"].startswith("agentic_data_ingestion_")
        assert body["table_prefix"].endswith("_")
        assert "glue_database" in body

        mock_ec2.describe_route_tables.assert_called_once()
        mock_create_conn.assert_called_once()
        mock_glue.create_crawler.assert_called_once()
        mock_glue.start_crawler.assert_called_once()

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_crawler_carries_discovery_tag_set_role_and_database(self, mock_ec2, mock_glue, mock_create_conn):
        """Target-side crawler MUST tag with Type=agentic-data-ingestion-discovery and
        carry the gluePipelineRole + DISCOVERY_DB (Req 4.4, Req 4.11).

        The P17 daily janitor filters on this tag — without it, target-side
        wizard-time crawlers would leak. The role/database assertions
        anchor the rest of Req 4.4 (same role and same database as the
        source-side flow) so a future refactor that rewires either knob
        fails this test rather than silently producing orphaned crawlers
        in the wrong database.
        """
        mock_ec2.describe_route_tables.return_value = _route_table_with_s3_endpoint()
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})
        # Crawler does not yet exist → helper takes the create-with-Tags path.
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")

        from handlers.jdbc_resources import DISCOVERY_DB, detect_target_schema_start

        event = {"body": json.dumps(_select_rds_target_body())}
        response = detect_target_schema_start(event, None)
        assert response["statusCode"] == 202

        # The crawler creation kwargs include the documented tag set.
        crawler_kwargs = mock_glue.create_crawler.call_args.kwargs
        tags = crawler_kwargs.get("Tags") or {}
        assert tags.get("Type") == "agentic-data-ingestion-discovery"
        assert "CreatedAt" in tags
        assert "Hash" in tags
        # Hash matches the crawler-name suffix (single source of truth).
        body = json.loads(response["body"])
        crawler_name = body["crawler_name"]
        assert tags["Hash"] == crawler_name.removeprefix("agentic-data-ingestion-discover-")

        # Req 4.4: same gluePipelineRole as the source-side flow. The
        # autouse fixture pins the env-derived constant to a known ARN.
        assert crawler_kwargs.get("Role") == "arn:aws:iam::123456789012:role/agentic-data-ingestion-glue-pipeline-role"
        # Req 4.4: target Glue database matches DISCOVERY_DB
        # (`agentic_data_ingestion_jdbc_discovery_{stage}`) so target-side discoveries
        # land alongside source-side ones and the daily janitor sweeps
        # both with one query.
        assert crawler_kwargs.get("DatabaseName") == DISCOVERY_DB

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_glue_connection_mode_skips_connection_creation(self, mock_ec2, mock_glue, mock_create_conn):
        """glue_connection: no Glue connection create (Req 4.6); pre-flight N/A.

        Target-side mirror of the source-side test of the same name. The
        connection declares no ``PhysicalConnectionRequirements``, so the S3
        pre-flight resolves ``not_vpc_attached`` and reaches no EC2 call — which
        is why ``describe_route_tables`` is not called. Before 2026-09 the
        pre-flight was skipped here unconditionally.
        """
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})
        mock_glue.get_connection.return_value = {"Connection": {"Name": "my-existing-connection"}}
        # Crawler does not yet exist → helper takes the create-with-Tags path.
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")

        from handlers.jdbc_resources import detect_target_schema_start

        event = {"body": json.dumps(_glue_connection_target_body())}
        response = detect_target_schema_start(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["status"] == "RUNNING"

        assert mock_glue.get_connection.call_args.kwargs["HidePassword"] is True
        # No route-table read (not VPC-attached) and no connection creation.
        mock_ec2.describe_route_tables.assert_not_called()
        mock_create_conn.assert_not_called()

        # Crawler IS created and points at the user's existing connection.
        mock_glue.create_crawler.assert_called_once()
        crawler_kwargs = mock_glue.create_crawler.call_args.kwargs
        jdbc_targets = crawler_kwargs["Targets"]["JdbcTargets"]
        assert jdbc_targets[0]["ConnectionName"] == "my-existing-connection"
        mock_glue.start_crawler.assert_called_once()

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_select_rds_mode_missing_s3_endpoint_returns_400(self, mock_ec2, mock_glue, mock_create_conn):
        """No S3 Gateway Endpoint → pre-flight fails, no Glue calls made."""
        mock_ec2.describe_route_tables.return_value = _route_table_without_s3_endpoint()

        from handlers.jdbc_resources import detect_target_schema_start

        event = {"body": json.dumps(_select_rds_target_body())}
        response = detect_target_schema_start(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body
        assert "S3" in body["error"]

        mock_create_conn.assert_not_called()
        mock_glue.create_crawler.assert_not_called()
        mock_glue.start_crawler.assert_not_called()

    def test_missing_target_config_returns_400(self):
        """Body without target_config object → 400."""
        from handlers.jdbc_resources import detect_target_schema_start

        event = {"body": json.dumps({})}
        response = detect_target_schema_start(event, None)
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "target_config" in body["error"]

    def test_missing_database_name_returns_400(self):
        """Missing database_name → 400."""
        from handlers.jdbc_resources import detect_target_schema_start

        body = _glue_connection_target_body()
        del body["target_config"]["database_name"]
        event = {"body": json.dumps(body)}
        response = detect_target_schema_start(event, None)
        assert response["statusCode"] == 400
        assert "database_name" in json.loads(response["body"])["error"]

    def test_missing_table_name_returns_400(self):
        """Missing table_name → 400."""
        from handlers.jdbc_resources import detect_target_schema_start

        body = _glue_connection_target_body()
        del body["target_config"]["table_name"]
        event = {"body": json.dumps(body)}
        response = detect_target_schema_start(event, None)
        assert response["statusCode"] == 400
        assert "table_name" in json.loads(response["body"])["error"]

    def test_invalid_database_name_returns_400(self):
        """SQL-injection-shaped database_name → 400 (Req 4.10)."""
        from handlers.jdbc_resources import detect_target_schema_start

        body = _glue_connection_target_body()
        body["target_config"]["database_name"] = "evil; DROP TABLE"
        event = {"body": json.dumps(body)}
        response = detect_target_schema_start(event, None)
        assert response["statusCode"] == 400
        assert "Invalid SQL identifier" in json.loads(response["body"])["error"]

    def test_invalid_table_name_returns_400(self):
        """SQL-injection-shaped table_name → 400 (Req 4.10)."""
        from handlers.jdbc_resources import detect_target_schema_start

        body = _glue_connection_target_body()
        body["target_config"]["table_name"] = "1bad-name"
        event = {"body": json.dumps(body)}
        response = detect_target_schema_start(event, None)
        assert response["statusCode"] == 400
        assert "Invalid SQL identifier" in json.loads(response["body"])["error"]

    def test_select_rds_missing_required_field_returns_400(self):
        """select_rds mode missing required field → 400 with field name."""
        from handlers.jdbc_resources import detect_target_schema_start

        for missing in (
            "secret_arn",
            "jdbc_url",
            "jdbc_driver",
            "subnet_id",
            "security_group_ids",
        ):
            body = _select_rds_target_body()
            del body["target_config"][missing]
            event = {"body": json.dumps(body)}
            response = detect_target_schema_start(event, None)
            assert response["statusCode"] == 400, (
                f"Expected 400 when {missing} is missing; got {response['statusCode']}"
            )
            err = json.loads(response["body"])["error"]
            assert missing in err
            assert "select_rds" in err

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_glue_create_crawler_failure_returns_500(self, mock_ec2, mock_glue, mock_create_conn):
        """Glue API failure during create_crawler → 500."""
        from botocore.exceptions import ClientError

        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})
        # Crawler does not yet exist → helper takes the create path where the
        # ClientError below is raised (and propagates to a 500).
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")
        mock_glue.create_crawler.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceException", "Message": "Glue boom"}},
            "CreateCrawler",
        )

        from handlers.jdbc_resources import detect_target_schema_start

        event = {"body": json.dumps(_glue_connection_target_body())}
        response = detect_target_schema_start(event, None)
        assert response["statusCode"] == 500


# ---------------------------------------------------------------------------
# Example unit tests — detect_target_schema_status
# ---------------------------------------------------------------------------


class TestDetectTargetSchemaStatus:
    """Tests for the GET /detect-target-schema/{crawler_name} endpoint.

    Functionally identical wire-shape to the source-side polling endpoint —
    these tests focus on the target-side specific dispatch path.

    Validates: P19 Requirements 4.7, 4.8, 4.9.
    """

    @patch("handlers.jdbc_resources.glue_client")
    def test_running_then_ready_transition(self, mock_glue):
        """Two polls: first 202 RUNNING, second 200 READY (Req 4.8, 4.9)."""
        crawler_name = "agentic-data-ingestion-discover-abc123def456"

        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "RUNNING",
                "LastCrawl": {},
            }
        }

        from handlers.jdbc_resources import detect_target_schema_status

        event = {"pathParameters": {"crawler_name": crawler_name}}
        response_running = detect_target_schema_status(event, None)
        assert response_running["statusCode"] == 202
        body_running = json.loads(response_running["body"])
        assert body_running["status"] == "RUNNING"
        assert body_running["crawler_name"] == crawler_name

        # Second poll: SUCCEEDED with exactly one matching table.
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "READY",
                "LastCrawl": {"Status": "SUCCEEDED"},
            }
        }
        mock_glue.get_tables.return_value = {
            "TableList": [
                {
                    "Name": "agentic_data_ingestion_abc123def456_users_mirror",
                    "StorageDescriptor": {
                        "Columns": [
                            {"Name": "id", "Type": "bigint", "Comment": "PK"},
                            {"Name": "email", "Type": "string", "Comment": ""},
                        ]
                    },
                    "PartitionKeys": [],
                }
            ]
        }
        response_ready = detect_target_schema_status(event, None)
        assert response_ready["statusCode"] == 200
        body_ready = json.loads(response_ready["body"])
        assert body_ready["status"] == "READY"
        schema = body_ready["schema"]
        assert len(schema) == 2
        # Wire-shape contract: {name, type, nullable, description} (Req 4.7).
        assert {"name", "type", "nullable", "description"}.issubset(schema[0].keys())
        assert schema[0]["name"] == "id"
        assert schema[0]["type"] == "bigint"

    @patch("handlers.jdbc_resources.glue_client")
    def test_crawler_failed_returns_500(self, mock_glue):
        """LastCrawl.Status=FAILED → 500 with error_message verbatim."""
        crawler_name = "agentic-data-ingestion-discover-failed1234ab"
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "READY",
                "LastCrawl": {
                    "Status": "FAILED",
                    "ErrorMessage": "Connection timed out",
                },
            }
        }

        from handlers.jdbc_resources import detect_target_schema_status

        event = {"pathParameters": {"crawler_name": crawler_name}}
        response = detect_target_schema_status(event, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert body["error_message"] == "Connection timed out"

    @patch("handlers.jdbc_resources.glue_client")
    def test_zero_tables_returns_500(self, mock_glue):
        """SUCCEEDED + zero matching tables → 500 (Req 4.9)."""
        crawler_name = "agentic-data-ingestion-discover-empty5678ab90"
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "READY",
                "LastCrawl": {"Status": "SUCCEEDED"},
            }
        }
        mock_glue.get_tables.return_value = {"TableList": []}

        from handlers.jdbc_resources import detect_target_schema_status

        event = {"pathParameters": {"crawler_name": crawler_name}}
        response = detect_target_schema_status(event, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert "no table" in body["error_message"].lower()

    @patch("handlers.jdbc_resources.glue_client")
    def test_multi_tables_returns_500(self, mock_glue):
        """SUCCEEDED + multiple matching tables → 500 listing names (Req 4.9)."""
        crawler_name = "agentic-data-ingestion-discover-multitable12"
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "READY",
                "LastCrawl": {"Status": "SUCCEEDED"},
            }
        }
        mock_glue.get_tables.return_value = {
            "TableList": [
                {"Name": "agentic_data_ingestion_multitable12_users_mirror"},
                {"Name": "agentic_data_ingestion_multitable12_users_mirror_audit"},
            ]
        }

        from handlers.jdbc_resources import detect_target_schema_status

        event = {"pathParameters": {"crawler_name": crawler_name}}
        response = detect_target_schema_status(event, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "agentic_data_ingestion_multitable12_users_mirror" in body["error_message"]
        assert "agentic_data_ingestion_multitable12_users_mirror_audit" in body["error_message"]


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


# Identifier strategy matching ^[A-Za-z_][A-Za-z0-9_]*$ — used by Property 4
# and indirectly by Property 6's "valid" branch.
_VALID_IDENTIFIER_REGEX = r"^[A-Za-z_][A-Za-z0-9_]{0,31}$"
_valid_identifier_strategy = st.from_regex(_VALID_IDENTIFIER_REGEX, fullmatch=True)


# Feature: p19-jdbc-target-modes, Property 6: Server-side identifier validation is fail-closed
class TestProperty6IdentifierValidationFailClosed:
    """Property 6: Server-side identifier validation is fail-closed.

    *For any* string ``s``, ``POST /api/resources/jdbc/detect-target-schema``
    with ``target_config.database_name = s`` (and an otherwise valid body)
    accepts the request (proceeds past identifier validation) if and only
    if ``s`` matches ``^[A-Za-z_][A-Za-z0-9_]*$``. The same property holds
    for ``target_config.table_name``.

    Validates: Requirements 4.10
    """

    @given(s=st.text(min_size=0, max_size=64))
    @settings(max_examples=100)
    @patch("handlers.jdbc_resources.glue_client")
    def test_database_name_accepted_iff_regex_matches(self, mock_glue, s: str):
        """database_name validation is the regex test, no other constraints."""
        import re

        valid = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s))

        # Stub Glue so we don't actually create resources for valid inputs;
        # we only care whether the handler returns a non-validation response.
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})

        from handlers.jdbc_resources import detect_target_schema_start

        body = _glue_connection_target_body()
        body["target_config"]["database_name"] = s
        event = {"body": json.dumps(body)}
        response = detect_target_schema_start(event, None)

        if valid:
            # Validation passes; handler proceeds to crawler creation. We
            # accept any non-400-with-identifier-error response.
            err = ""
            if response["statusCode"] == 400:
                err = json.loads(response["body"]).get("error", "")
            assert "Invalid SQL identifier" not in err, f"Valid identifier {s!r} should pass validation; got {err}"
        else:
            # Validation fails fail-closed — either the identifier check
            # rejected the input (preferred) or an upstream
            # required-field check did, but never an HTTP 202 success.
            assert response["statusCode"] == 400, (
                f"Invalid identifier {s!r} must not return 202; got {response['statusCode']}"
            )

    @given(s=st.text(min_size=0, max_size=64))
    @settings(max_examples=100)
    @patch("handlers.jdbc_resources.glue_client")
    def test_table_name_accepted_iff_regex_matches(self, mock_glue, s: str):
        """table_name validation has the same biconditional shape."""
        import re

        valid = bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s))

        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})

        from handlers.jdbc_resources import detect_target_schema_start

        body = _glue_connection_target_body()
        body["target_config"]["table_name"] = s
        event = {"body": json.dumps(body)}
        response = detect_target_schema_start(event, None)

        if valid:
            err = ""
            if response["statusCode"] == 400:
                err = json.loads(response["body"]).get("error", "")
            assert "Invalid SQL identifier" not in err, f"Valid identifier {s!r} should pass validation; got {err}"
        else:
            assert response["statusCode"] == 400, (
                f"Invalid identifier {s!r} must not return 202; got {response['statusCode']}"
            )


# Feature: p19-jdbc-target-modes, Property 4: Target-side hash equals source-side hash for analogous inputs
class TestProperty4TargetSideHashMatchesSourceSide:
    """Property 4: target-side hash byte-equals source-side hash for analogous
    ``(connection, identifier, identifier)`` inputs.

    *For any* connection identifier ``C`` and any pair of identifiers
    ``(D, T)`` matching ``^[A-Za-z_][A-Za-z0-9_]*$``, the target-side
    discovery hash computed from ``(C, database_name=D, table_name=T)`` is
    byte-equal to the source-side hash computed from
    ``(C, jdbc_schema=D, jdbc_table=T)``.

    Implementation: ``detect_target_schema_start`` projects
    ``database_name`` into the ``jdbc_schema`` slot and ``table_name``
    into the ``jdbc_table`` slot before passing the dict to
    ``_compute_discovery_hash``, which is the single source of truth.

    Validates: Requirements 4.3
    """

    @given(
        connection_name=_valid_identifier_strategy,
        db=_valid_identifier_strategy,
        table=_valid_identifier_strategy,
    )
    @settings(max_examples=100)
    def test_glue_connection_mode_hash_equality(self, connection_name: str, db: str, table: str):
        """In glue_connection mode the hashes are byte-equal."""
        from handlers.jdbc_resources import _compute_discovery_hash

        source_config = {
            "glue_connection_name": connection_name,
            "jdbc_schema": db,
            "jdbc_table": table,
        }
        target_config = {
            "glue_connection_name": connection_name,
            "database_name": db,
            "table_name": table,
        }

        # Source-side hash is computed directly.
        source_hash = _compute_discovery_hash(source_config)

        # Target-side performs the documented projection before hashing.
        target_hash_input = dict(target_config)
        target_hash_input["jdbc_schema"] = target_config["database_name"]
        target_hash_input["jdbc_table"] = target_config["table_name"]
        target_hash = _compute_discovery_hash(target_hash_input)

        assert source_hash == target_hash, (
            f"Hashes diverged for connection={connection_name!r}, db={db!r}, "
            f"table={table!r}: source={source_hash!r}, target={target_hash!r}"
        )

    @given(
        secret_arn=st.from_regex(
            r"arn:aws:secretsmanager:[a-z0-9-]{8,16}:[0-9]{12}:secret:[A-Za-z0-9_-]{4,16}",
            fullmatch=True,
        ),
        jdbc_url=st.from_regex(
            r"jdbc:postgresql://[a-z0-9-]{4,16}:[0-9]{4,5}/[a-z]{4,8}",
            fullmatch=True,
        ),
        db=_valid_identifier_strategy,
        table=_valid_identifier_strategy,
    )
    @settings(max_examples=100)
    def test_select_rds_mode_hash_equality(self, secret_arn: str, jdbc_url: str, db: str, table: str):
        """In select_rds mode the hashes are byte-equal too."""
        from handlers.jdbc_resources import _compute_discovery_hash

        source_config = {
            "secret_arn": secret_arn,
            "jdbc_url": jdbc_url,
            "jdbc_schema": db,
            "jdbc_table": table,
        }
        target_config = {
            "secret_arn": secret_arn,
            "jdbc_url": jdbc_url,
            "database_name": db,
            "table_name": table,
        }

        source_hash = _compute_discovery_hash(source_config)

        target_hash_input = dict(target_config)
        target_hash_input["jdbc_schema"] = target_config["database_name"]
        target_hash_input["jdbc_table"] = target_config["table_name"]
        target_hash = _compute_discovery_hash(target_hash_input)

        assert source_hash == target_hash

        # Cross-check the documented hash recipe so a future refactor that
        # changes the hash material is caught immediately.
        material = f"{secret_arn}{jdbc_url}{db}{table}"
        expected = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
        assert source_hash == expected


# Feature: p19-jdbc-target-modes, Property 7: Endpoint accepts iff body conforms to required-fields-per-connection-mode rules
class TestProperty7BodyConformsToRequiredFields:
    """Property 7: endpoint accepts iff body conforms to required-fields-per-
    connection-mode rules.

    *For any* request body shape ``B``, ``POST .../detect-target-schema``
    accepts ``B`` (returns HTTP 202) if and only if
    ``target_config.{database_name,table_name}`` are present and identifier-
    valid AND either (a) ``glue_connection_name`` is present and non-empty
    (glue_connection mode) or (b) all of
    ``{secret_arn, jdbc_url, jdbc_driver, subnet_id, security_group_ids}``
    are present and non-empty (select_rds mode).

    Validates: Requirements 4.2
    """

    @given(
        # Independently toggle each required field on/off so we cover the
        # required-fields-per-mode matrix without enumerating it by hand.
        has_db_name=st.booleans(),
        has_table_name=st.booleans(),
        has_glue_conn=st.booleans(),
        has_secret_arn=st.booleans(),
        has_jdbc_url=st.booleans(),
        has_jdbc_driver=st.booleans(),
        has_subnet_id=st.booleans(),
        has_security_groups=st.booleans(),
    )
    @settings(max_examples=100)
    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_required_fields_per_mode_biconditional(
        self,
        mock_ec2,
        mock_glue,
        mock_create_conn,
        has_db_name: bool,
        has_table_name: bool,
        has_glue_conn: bool,
        has_secret_arn: bool,
        has_jdbc_url: bool,
        has_jdbc_driver: bool,
        has_subnet_id: bool,
        has_security_groups: bool,
    ):
        """Build a body with each field present/absent; assert biconditional."""
        # Stub AWS so we don't fail on side effects when the body is valid.
        mock_ec2.describe_route_tables.return_value = _route_table_with_s3_endpoint()
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})

        target_config: dict = {}
        if has_db_name:
            target_config["database_name"] = "analytics"
        if has_table_name:
            target_config["table_name"] = "users_mirror"
        if has_glue_conn:
            target_config["glue_connection_name"] = "my-conn"
        if has_secret_arn:
            target_config["secret_arn"] = "arn:aws:secretsmanager:us-west-2:123456789012:secret:c-AbCdEf"
        if has_jdbc_url:
            target_config["jdbc_url"] = "jdbc:postgresql://db:5432/orders"
        if has_jdbc_driver:
            target_config["jdbc_driver"] = "org.postgresql.Driver"
        if has_subnet_id:
            target_config["subnet_id"] = "subnet-0abc"
        if has_security_groups:
            target_config["security_group_ids"] = ["sg-0abc"]

        from handlers.jdbc_resources import detect_target_schema_start

        event = {"body": json.dumps({"target_config": target_config})}
        response = detect_target_schema_start(event, None)

        ids_present = has_db_name and has_table_name
        glue_mode = has_glue_conn
        select_rds_mode = has_secret_arn and has_jdbc_url and has_jdbc_driver and has_subnet_id and has_security_groups
        body_conforms = ids_present and (glue_mode or select_rds_mode)

        if body_conforms:
            assert response["statusCode"] == 202, (
                f"Conforming body must produce 202; got {response['statusCode']} for target_config={target_config!r}"
            )
        else:
            assert response["statusCode"] == 400, (
                f"Non-conforming body must produce 400; got "
                f"{response['statusCode']} for target_config={target_config!r}"
            )
