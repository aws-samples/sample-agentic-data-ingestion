# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: /api/resources/jdbc/detect-target-schema response shapes.

Pins the wire format that the frontend (``WizardStepTarget``'s JDBC
detect panel + ``useJdbcDetectTargetSchemaStart`` /
``useJdbcDetectTargetSchemaStatus`` hooks) consumes:

* **POST 202** — ``detect_target_schema_start`` happy path:
  ``{crawler_name, glue_database, table_prefix, status="RUNNING"}``
* **POST 400** — validation failure (bad identifier / missing field):
  ``{error}``
* **POST 500** — Glue API failure: ``{error}``
* **GET 202** — ``detect_target_schema_status`` while crawler is still
  running: ``{status="RUNNING", crawler_name, glue_database, table_prefix}``
* **GET 200** — crawler succeeded with exactly one matching table:
  ``{status="READY", schema=[{name,type,nullable,description}],
  glue_database, glue_table}``
* **GET 500** — any failure mode (crawler FAILED, CANCELLED, zero
  tables, multiple tables): ``{status="FAILED", crawler_name,
  error_message}``

Behavioural correctness is in
``tests/unit/lambdas/dashboard_api/test_jdbc_target_detect_schema.py``.
This file pins the **wire format** so any future refactor that changes
a field name or status code immediately fails the contract.

Validates: P19 Requirements 4.7, 4.8, 4.9
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Lambda-layer dependency stubs — must run BEFORE importing the handler.
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


@pytest.fixture(autouse=True)
def _set_glue_role_env(monkeypatch):
    """Patch the module-level Glue role ARN so the handler does not
    refuse to call create_crawler with a configuration error."""
    monkeypatch.setattr(
        "handlers.jdbc_resources.GLUE_PIPELINE_ROLE_ARN",
        "arn:aws:iam::123456789012:role/agentic-data-ingestion-glue-pipeline-role",
    )


def _select_rds_target_body() -> dict:
    """Valid select_rds-mode request body for detect_target_schema_start."""
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
    """Valid glue_connection-mode request body for detect_target_schema_start."""
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


def _parse(response):
    """Extract (statusCode, body_dict) from an API Gateway response."""
    return response["statusCode"], json.loads(response["body"])


def _assert_json_response(response):
    """Every response must declare itself as application/json."""
    assert response["headers"]["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# POST 202 — detect_target_schema_start happy path
# ---------------------------------------------------------------------------


class TestDetectTargetSchemaStartPost202Contract:
    """Wire-format pin for POST .../detect-target-schema (202)."""

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_post_202_response_shape(self, mock_ec2, mock_glue, mock_create_conn):
        """POST 202 body has exactly {crawler_name, glue_database,
        table_prefix, status} with the documented types and prefixes.

        Validates: Req 4.7.
        """
        mock_ec2.describe_route_tables.return_value = _route_table_with_s3_endpoint()
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})

        from handlers.jdbc_resources import detect_target_schema_start

        response = detect_target_schema_start({"body": json.dumps(_select_rds_target_body())}, None)
        _assert_json_response(response)
        status, body = _parse(response)
        assert status == 202

        for key in ("crawler_name", "glue_database", "table_prefix", "status"):
            assert key in body, f"POST 202 response missing required key: {key}"

        assert isinstance(body["crawler_name"], str)
        assert isinstance(body["glue_database"], str)
        assert isinstance(body["table_prefix"], str)
        assert isinstance(body["status"], str)
        assert body["status"] == "RUNNING"

        # Naming conventions match the source-side endpoint exactly so the
        # frontend can reuse the same DetectStartResponse / DetectStatusResponse
        # types (P19 Req 4.7).
        assert body["crawler_name"].startswith("agentic-data-ingestion-discover-")
        assert body["table_prefix"].startswith("agentic_data_ingestion_")
        assert body["table_prefix"].endswith("_")

        crawler_hash = body["crawler_name"][len("agentic-data-ingestion-discover-") :]
        prefix_hash = body["table_prefix"][len("agentic_data_ingestion_") : -1]
        assert crawler_hash == prefix_hash, "crawler_name hash must equal table_prefix hash"
        assert body["glue_database"]


class TestDetectTargetSchemaStartPostErrorsContract:
    """Wire-format pins for POST 400 (validation) and POST 500 (Glue failure)."""

    def test_post_400_invalid_identifier_response_shape(self):
        """Bad database_name → 400 with {error}."""
        from handlers.jdbc_resources import detect_target_schema_start

        body = _glue_connection_target_body()
        body["target_config"]["database_name"] = "evil; DROP TABLE"
        response = detect_target_schema_start({"body": json.dumps(body)}, None)
        _assert_json_response(response)
        status, parsed = _parse(response)
        assert status == 400
        assert "error" in parsed
        assert isinstance(parsed["error"], str)

    def test_post_400_missing_required_field_response_shape(self):
        """select_rds mode missing secret_arn → 400 with {error}."""
        from handlers.jdbc_resources import detect_target_schema_start

        body = _select_rds_target_body()
        del body["target_config"]["secret_arn"]
        response = detect_target_schema_start({"body": json.dumps(body)}, None)
        _assert_json_response(response)
        status, parsed = _parse(response)
        assert status == 400
        assert "error" in parsed

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_post_500_glue_failure_response_shape(self, mock_ec2, mock_glue, mock_create_conn):
        """Glue API failure → 500 with {error}."""
        from botocore.exceptions import ClientError

        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})
        # Crawler does not yet exist → helper takes the create path where the
        # ClientError below is raised (and propagates to a 500).
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")
        mock_glue.create_crawler.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceException", "Message": "boom"}},
            "CreateCrawler",
        )

        from handlers.jdbc_resources import detect_target_schema_start

        response = detect_target_schema_start({"body": json.dumps(_glue_connection_target_body())}, None)
        _assert_json_response(response)
        status, body = _parse(response)
        assert status == 500
        assert "error" in body
        assert isinstance(body["error"], str)


# ---------------------------------------------------------------------------
# GET 202 — detect_target_schema_status while crawler is still running
# ---------------------------------------------------------------------------


class TestDetectTargetSchemaStatusGet202Contract:
    """Wire-format pin for GET .../detect-target-schema/{name} (202)."""

    @patch("handlers.jdbc_resources.glue_client")
    def test_get_202_response_shape_while_running(self, mock_glue):
        """GET 202 body has {status='RUNNING', crawler_name,
        glue_database, table_prefix}.

        Validates: Req 4.8.
        """
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

        response = detect_target_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        _assert_json_response(response)
        status, body = _parse(response)
        assert status == 202

        for key in ("status", "crawler_name", "glue_database", "table_prefix"):
            assert key in body, f"GET 202 response missing required key: {key}"

        assert body["status"] == "RUNNING"
        assert body["crawler_name"] == crawler_name
        assert body["table_prefix"] == "agentic_data_ingestion_abc123def456_"


# ---------------------------------------------------------------------------
# GET 200 — detect_target_schema_status when crawler succeeded with 1 table
# ---------------------------------------------------------------------------


class TestDetectTargetSchemaStatusGet200Contract:
    """Wire-format pin for GET .../detect-target-schema/{name} (200)."""

    @patch("handlers.jdbc_resources.glue_client")
    def test_get_200_response_shape_when_ready(self, mock_glue):
        """GET 200 body has {status='READY', schema, glue_database,
        glue_table} with each schema entry having
        {name, type, nullable, description}.

        Validates: Req 4.7, 4.9.
        """
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

        from handlers.jdbc_resources import detect_target_schema_status

        response = detect_target_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        _assert_json_response(response)
        status, body = _parse(response)
        assert status == 200

        for key in ("status", "schema", "glue_database", "glue_table"):
            assert key in body, f"GET 200 response missing required key: {key}"

        assert body["status"] == "READY"
        assert body["glue_table"] == "agentic_data_ingestion_abc123def456_users_mirror"

        # Schema entries match the documented {name, type, nullable, description}
        # shape (Req 4.7).
        assert len(body["schema"]) > 0
        for column in body["schema"]:
            assert isinstance(column, dict)
            for key in ("name", "type", "nullable", "description"):
                assert key in column, f"schema entry missing {key!r}"
            assert isinstance(column["name"], str)
            assert isinstance(column["type"], str)
            assert isinstance(column["nullable"], bool)
            assert isinstance(column["description"], str)


# ---------------------------------------------------------------------------
# GET 500 — detect_target_schema_status for any failure mode
# ---------------------------------------------------------------------------


class TestDetectTargetSchemaStatusGet500Contract:
    """Wire-format pin for GET .../detect-target-schema/{name} (500)."""

    def _assert_failed_response_shape(self, response, expected_crawler_name):
        _assert_json_response(response)
        status, body = _parse(response)
        assert status == 500
        for key in ("status", "crawler_name", "error_message"):
            assert key in body
        assert body["status"] == "FAILED"
        assert body["crawler_name"] == expected_crawler_name
        assert body["error_message"]

    @patch("handlers.jdbc_resources.glue_client")
    def test_get_500_when_crawler_reports_failed(self, mock_glue):
        """LastCrawl.Status=FAILED → 500 FAILED with crawler error verbatim."""
        crawler_name = "agentic-data-ingestion-discover-failed1234ab"
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "READY",
                "LastCrawl": {
                    "Status": "FAILED",
                    "ErrorMessage": "boom",
                },
            }
        }

        from handlers.jdbc_resources import detect_target_schema_status

        response = detect_target_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        self._assert_failed_response_shape(response, crawler_name)


# ---------------------------------------------------------------------------
# Property test — Property 5: GET polling response matches the crawler-state
# state machine.
# ---------------------------------------------------------------------------


# Feature: p19-jdbc-target-modes, Property 5: GET polling response matches the crawler-state state machine
class TestProperty5GetPollingStateMachine:
    """Property 5: GET polling response matches the crawler-state state machine.

    *For any* ``(crawler_state, matching_table_count)`` tuple where
    ``crawler_state`` ranges over
    ``{RUNNING, READY-with-SUCCEEDED, READY-with-FAILED, FAILED}`` and
    ``matching_table_count`` ranges over the non-negative integers,
    ``GET /api/resources/jdbc/detect-target-schema/{crawler_name}`` returns:

    * HTTP 202 with ``status="RUNNING"`` iff ``crawler_state == RUNNING``.
    * HTTP 200 with ``status="READY"`` and a non-empty ``schema`` iff
      ``crawler_state == READY-with-SUCCEEDED`` AND
      ``matching_table_count == 1``.
    * HTTP 500 with ``status="FAILED"`` iff
      ``crawler_state == FAILED`` OR (``crawler_state == READY-with-SUCCEEDED``
      AND ``matching_table_count != 1``).

    Validates: P19 Requirements 4.8, 4.9
    """

    @given(
        # Maps to AWS Glue's documented (crawler.State, LastCrawl.Status)
        # state machine. The handler treats:
        #   - State != READY                 → RUNNING (frontend keeps polling)
        #   - State == READY + Status FAILED → FAILED
        #   - State == READY + Status SUCCEEDED + 1 table → READY
        #   - State == READY + Status SUCCEEDED + !=1 tables → FAILED
        crawler_state=st.sampled_from(["RUNNING", "READY_SUCCEEDED", "READY_FAILED"]),
        matching_table_count=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=100)
    @patch("handlers.jdbc_resources.glue_client")
    def test_get_polling_response_state_machine(
        self,
        mock_glue,
        crawler_state: str,
        matching_table_count: int,
    ):
        """For each (crawler_state, table_count) pair, the response biconditional holds."""
        crawler_name = "agentic-data-ingestion-discover-abc123def456"
        table_prefix = "agentic_data_ingestion_abc123def456_"

        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})

        # Map the abstract crawler_state to the AWS Glue API shape.
        if crawler_state == "RUNNING":
            mock_glue.get_crawler.return_value = {
                "Crawler": {
                    "Name": crawler_name,
                    "State": "RUNNING",
                    "LastCrawl": {},
                }
            }
        elif crawler_state == "READY_SUCCEEDED":
            mock_glue.get_crawler.return_value = {
                "Crawler": {
                    "Name": crawler_name,
                    "State": "READY",
                    "LastCrawl": {"Status": "SUCCEEDED"},
                }
            }
            tables = []
            for i in range(matching_table_count):
                tables.append(
                    {
                        "Name": f"{table_prefix}t{i}",
                        "StorageDescriptor": {
                            "Columns": [
                                {
                                    "Name": "id",
                                    "Type": "bigint",
                                    "Comment": "",
                                }
                            ]
                        },
                        "PartitionKeys": [],
                    }
                )
            mock_glue.get_tables.return_value = {"TableList": tables}
        else:  # READY_FAILED
            mock_glue.get_crawler.return_value = {
                "Crawler": {
                    "Name": crawler_name,
                    "State": "READY",
                    "LastCrawl": {
                        "Status": "FAILED",
                        "ErrorMessage": "x",
                    },
                }
            }

        from handlers.jdbc_resources import detect_target_schema_status

        response = detect_target_schema_status({"pathParameters": {"crawler_name": crawler_name}}, None)
        status, body = response["statusCode"], json.loads(response["body"])

        if crawler_state == "RUNNING":
            assert status == 202
            assert body["status"] == "RUNNING"
        elif crawler_state == "READY_SUCCEEDED" and matching_table_count == 1:
            assert status == 200, f"Expected 200 for READY_SUCCEEDED+1 table; got {status}: {body}"
            assert body["status"] == "READY"
            assert isinstance(body.get("schema"), list)
            assert len(body["schema"]) > 0
        else:
            # READY_FAILED, or READY_SUCCEEDED with !=1 tables.
            assert status == 500, (
                f"Expected 500 for {crawler_state} + {matching_table_count} tables; got {status}: {body}"
            )
            assert body["status"] == "FAILED"
