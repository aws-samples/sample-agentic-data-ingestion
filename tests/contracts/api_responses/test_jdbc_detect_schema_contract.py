# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: /api/resources/jdbc/detect-schema response shapes.

The Create-Pipeline Wizard's JDBC source step depends on a strict
contract for the shape of the four responses produced by the
schema-discovery endpoints. This test pins the wire format that the
frontend (``dashboard-react/src/api/jdbc.ts`` and
``components/wizard/JDBCConnectionSelector``) consumes:

* **POST 202** — ``detect_schema_start`` happy path:
  ``{crawler_name, glue_database, table_prefix, status="RUNNING"}``
* **GET 202** — ``detect_schema_status`` while crawler is still running:
  ``{status="RUNNING", crawler_name, glue_database, table_prefix}``
* **GET 200** — ``detect_schema_status`` when crawler succeeded with
  exactly one matching table:
  ``{status="READY", schema=[{name,type,nullable}], glue_database,
  glue_table}``
* **GET 500** — ``detect_schema_status`` for any failure mode (crawler
  FAILED, CANCELLED, zero tables, multiple tables):
  ``{status="FAILED", crawler_name, error_message}``

Behavioural correctness is covered by the unit tests in
``tests/unit/lambdas/dashboard_api/test_jdbc_resources.py`` (Task 4.6).
This file pins the **wire format** so that any future refactor of the
handler that changes a field name or status code immediately fails
this contract test rather than silently breaking the frontend wizard.

Validates: Requirements 22.1
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Lambda-layer dependency stubs — must run BEFORE importing the handler.
# Mirrors the pattern in tests/unit/lambdas/dashboard_api/test_jdbc_resources.py
# so this file can run in a unit-test process without the real Lambda
# layer mounted at /opt/python.
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
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Make ``shared/`` importable so handlers.jdbc_resources can resolve its
# ``from jdbc_helpers import ...`` line.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)

# Add the dashboard API handler package to sys.path.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
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
    """Patch the module-level Glue role ARN so detect_schema_start does not
    refuse to call create_crawler with a configuration error."""
    monkeypatch.setattr(
        "handlers.jdbc_resources.GLUE_PIPELINE_ROLE_ARN",
        "arn:aws:iam::123456789012:role/agentic-data-ingestion-glue-pipeline-role",
    )


def _select_rds_body() -> dict:
    """Valid select_rds-mode request body for detect_schema_start."""
    return {
        "source_config": {
            "jdbc_url": "jdbc:postgresql://db.example.com:5432/orders",
            "jdbc_driver": "org.postgresql.Driver",
            "jdbc_schema": "public",
            "jdbc_table": "orders",
            "secret_arn": ("arn:aws:secretsmanager:us-west-2:123456789012:secret:db-creds-AbCdEf"),
            "subnet_id": "subnet-0abc1234def5678ab",
            "security_group_ids": ["sg-0abc1234def5678ab"],
            "availability_zone": "us-west-2a",
        }
    }


def _route_table_with_s3_endpoint() -> dict:
    """describe_route_tables payload that satisfies the S3 endpoint pre-flight."""
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


def _parse(response):
    """Extract (statusCode, body_dict) from an API Gateway response."""
    return response["statusCode"], json.loads(response["body"])


def _assert_json_response(response):
    """Every response must declare itself as application/json."""
    assert response["headers"]["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# POST 202 — detect_schema_start happy path
# ---------------------------------------------------------------------------


class TestDetectSchemaStartPost202Contract:
    """Wire-format pin for POST /api/resources/jdbc/detect-schema (202)."""

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_post_202_response_shape(self, mock_ec2, mock_glue, mock_create_conn):
        """POST 202 body has exactly {crawler_name, glue_database,
        table_prefix, status} with the documented types and prefixes."""
        mock_ec2.describe_route_tables.return_value = _route_table_with_s3_endpoint()
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})

        from handlers.jdbc_resources import detect_schema_start

        response = detect_schema_start({"body": json.dumps(_select_rds_body())}, None)
        _assert_json_response(response)
        status, body = _parse(response)

        # Status code is the documented 202 (async accept).
        assert status == 202

        # All four required keys present.
        for key in ("crawler_name", "glue_database", "table_prefix", "status"):
            assert key in body, f"POST 202 response missing required key: {key}"

        # Field types.
        assert isinstance(body["crawler_name"], str)
        assert isinstance(body["glue_database"], str)
        assert isinstance(body["table_prefix"], str)
        assert isinstance(body["status"], str)

        # Status literal.
        assert body["status"] == "RUNNING"

        # Naming conventions documented in handlers.jdbc_resources.
        assert body["crawler_name"].startswith("agentic-data-ingestion-discover-"), (
            "crawler_name must start with 'agentic-data-ingestion-discover-' so the frontend can derive table_prefix on reload"
        )
        assert body["table_prefix"].startswith("agentic_data_ingestion_"), (
            "table_prefix must start with 'agentic_data_ingestion_'"
        )
        assert body["table_prefix"].endswith("_"), (
            "table_prefix must end with '_' so glue:GetTables Expression wildcard matches engine-mangled table names"
        )

        # The 12-char hash following 'agentic-data-ingestion-discover-' must be the
        # same hash following 'agentic_data_ingestion_' in the table_prefix — this
        # mapping is what lets the GET endpoint derive table_prefix from
        # crawler_name when the frontend doesn't round-trip it.
        crawler_hash = body["crawler_name"][len("agentic-data-ingestion-discover-") :]
        prefix_hash = body["table_prefix"][len("agentic_data_ingestion_") : -1]
        assert crawler_hash == prefix_hash, (
            "crawler_name hash must equal table_prefix hash so the GET endpoint can derive one from the other"
        )

        # glue_database must be non-empty.
        assert body["glue_database"], "glue_database must not be empty"


# ---------------------------------------------------------------------------
# GET 202 — detect_schema_status while crawler is still running
# ---------------------------------------------------------------------------


class TestDetectSchemaStatusGet202Contract:
    """Wire-format pin for GET /api/resources/jdbc/detect-schema/{name} (202)."""

    @patch("handlers.jdbc_resources.glue_client")
    def test_get_202_response_shape_while_running(self, mock_glue):
        """GET 202 body has {status='RUNNING', crawler_name,
        glue_database, table_prefix}."""
        crawler_name = "agentic-data-ingestion-discover-abc123def456"
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "RUNNING",
                "LastCrawl": {},
            }
        }

        from handlers.jdbc_resources import detect_schema_status

        response = detect_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        _assert_json_response(response)
        status, body = _parse(response)

        # Status code is 202 — frontend keeps polling.
        assert status == 202

        # All four required keys present.
        for key in ("status", "crawler_name", "glue_database", "table_prefix"):
            assert key in body, f"GET 202 response missing required key: {key}"

        # Field types.
        assert isinstance(body["status"], str)
        assert isinstance(body["crawler_name"], str)
        assert isinstance(body["glue_database"], str)
        assert isinstance(body["table_prefix"], str)

        # Status literal.
        assert body["status"] == "RUNNING"

        # Echoes the requested crawler_name verbatim — frontend uses
        # this to confirm the polling loop is talking to the same
        # crawler.
        assert body["crawler_name"] == crawler_name

        # Derived table_prefix matches the documented format.
        assert body["table_prefix"] == "agentic_data_ingestion_abc123def456_"


# ---------------------------------------------------------------------------
# GET 200 — detect_schema_status when crawler succeeded with one table
# ---------------------------------------------------------------------------


class TestDetectSchemaStatusGet200Contract:
    """Wire-format pin for GET /api/resources/jdbc/detect-schema/{name} (200)."""

    @patch("handlers.jdbc_resources.glue_client")
    def test_get_200_response_shape_when_ready(self, mock_glue):
        """GET 200 body has {status='READY', schema, glue_database,
        glue_table} with each schema entry having {name, type, nullable}."""
        crawler_name = "agentic-data-ingestion-discover-abc123def456"
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
                {
                    "Name": "agentic_data_ingestion_abc123def456_orders",
                    "StorageDescriptor": {
                        "Columns": [
                            {"Name": "id", "Type": "bigint", "Comment": "PK"},
                            {"Name": "email", "Type": "string", "Comment": ""},
                            {
                                "Name": "total",
                                "Type": "decimal(10,2)",
                                "Comment": "",
                            },
                        ]
                    },
                    "PartitionKeys": [],
                }
            ]
        }

        from handlers.jdbc_resources import detect_schema_status

        response = detect_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        _assert_json_response(response)
        status, body = _parse(response)

        # Status code is 200 — frontend stops polling and renders schema.
        assert status == 200

        # All four required top-level keys present.
        for key in ("status", "schema", "glue_database", "glue_table"):
            assert key in body, f"GET 200 response missing required key: {key}"

        # Top-level field types.
        assert isinstance(body["status"], str)
        assert isinstance(body["schema"], list)
        assert isinstance(body["glue_database"], str)
        assert isinstance(body["glue_table"], str)

        # Status literal.
        assert body["status"] == "READY"

        # glue_database / glue_table non-empty so the frontend can
        # persist them into the wizard state.
        assert body["glue_database"], "glue_database must not be empty"
        assert body["glue_table"] == "agentic_data_ingestion_abc123def456_orders"

        # Schema must be a list of dicts, each with the documented keys.
        assert len(body["schema"]) > 0, (
            "schema must be non-empty when status=READY (zero-table case should return 500 FAILED instead)"
        )
        for column in body["schema"]:
            assert isinstance(column, dict), "each schema entry must be an object"
            assert "name" in column, "schema entry missing 'name'"
            assert "type" in column, "schema entry missing 'type'"
            assert "nullable" in column, "schema entry missing 'nullable'"
            assert isinstance(column["name"], str)
            assert isinstance(column["type"], str)
            assert isinstance(column["nullable"], bool)
            # Glue catalog drops JDBC NOT NULL — every column is reported
            # as nullable per the handler contract.
            assert column["nullable"] is True

        # Type strings preserve their JDBC catalog form verbatim so
        # downstream Spark codegen can map them with full fidelity.
        types = [c["type"] for c in body["schema"]]
        assert "bigint" in types
        assert "string" in types
        assert "decimal(10,2)" in types


# ---------------------------------------------------------------------------
# GET 500 — detect_schema_status for any failure mode
# ---------------------------------------------------------------------------


class TestDetectSchemaStatusGet500Contract:
    """Wire-format pin for GET /api/resources/jdbc/detect-schema/{name} (500).

    All failure modes (crawler FAILED, crawler CANCELLED, zero tables
    matched, multiple tables matched) MUST surface the same shape so
    the frontend's error-rendering branch has a single contract to
    target.
    """

    def _assert_failed_response_shape(self, response, expected_crawler_name):
        """Common shape assertions used by every GET-500 scenario."""
        _assert_json_response(response)
        status, body = _parse(response)
        assert status == 500

        # All three required keys present.
        for key in ("status", "crawler_name", "error_message"):
            assert key in body, f"GET 500 response missing required key: {key}"

        # Field types.
        assert isinstance(body["status"], str)
        assert isinstance(body["crawler_name"], str)
        assert isinstance(body["error_message"], str)

        # Status literal.
        assert body["status"] == "FAILED"

        # Crawler name echoed verbatim so the frontend can clear the
        # right polling session from local state.
        assert body["crawler_name"] == expected_crawler_name

        # error_message must be non-empty so the frontend has something
        # to render to the user.
        assert body["error_message"], (
            "error_message must be non-empty so the frontend has a value to surface in the wizard error banner"
        )

    @patch("handlers.jdbc_resources.glue_client")
    def test_get_500_when_crawler_reports_failed(self, mock_glue):
        """LastCrawl.Status=FAILED → 500 FAILED with crawler's error verbatim."""
        crawler_name = "agentic-data-ingestion-discover-failed1234ab"
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "READY",
                "LastCrawl": {
                    "Status": "FAILED",
                    "ErrorMessage": "Connection timed out reaching db.example.com:5432",
                },
            }
        }

        from handlers.jdbc_resources import detect_schema_status

        response = detect_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        self._assert_failed_response_shape(response, crawler_name)

        # The crawler's error message is surfaced verbatim, not wrapped.
        body = json.loads(response["body"])
        assert body["error_message"] == ("Connection timed out reaching db.example.com:5432")

    @patch("handlers.jdbc_resources.glue_client")
    def test_get_500_when_crawler_succeeded_with_zero_tables(self, mock_glue):
        """SUCCEEDED with no matching tables → 500 FAILED with hint."""
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

        from handlers.jdbc_resources import detect_schema_status

        response = detect_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        self._assert_failed_response_shape(response, crawler_name)

    @patch("handlers.jdbc_resources.glue_client")
    def test_get_500_when_crawler_succeeded_with_multiple_tables(self, mock_glue):
        """SUCCEEDED with multiple matching tables → 500 FAILED."""
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
                {"Name": "agentic_data_ingestion_multitable12_orders"},
                {"Name": "agentic_data_ingestion_multitable12_orders_audit"},
            ]
        }

        from handlers.jdbc_resources import detect_schema_status

        response = detect_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        self._assert_failed_response_shape(response, crawler_name)

    @patch("handlers.jdbc_resources.glue_client")
    def test_get_500_when_crawler_was_cancelled(self, mock_glue):
        """LastCrawl.Status=CANCELLED → 500 FAILED with cancellation message."""
        crawler_name = "agentic-data-ingestion-discover-cancelled12"
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "READY",
                "LastCrawl": {"Status": "CANCELLED"},
            }
        }

        from handlers.jdbc_resources import detect_schema_status

        response = detect_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        self._assert_failed_response_shape(response, crawler_name)
