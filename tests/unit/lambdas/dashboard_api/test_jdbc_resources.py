# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for JDBC discovery endpoints in handlers/jdbc_resources.py.

Covers the wizard-time schema discovery flow added by Task 4 of the P17
JDBC data source spec — specifically the two public handlers:

    * ``detect_schema_start``  (POST /api/resources/jdbc/detect-schema)
    * ``detect_schema_status`` (GET  /api/resources/jdbc/detect-schema/{name})

Test scope (per Task 4.6):
    1. Happy path — RUNNING → READY transition across two status polls.
    2. ``glue_connection`` mode — existing connection reused, no
       Glue connection is created.
    3. S3 VPC Gateway Endpoint pre-flight failure in ``select_rds`` mode.
    4. Crawler ``LastCrawl.Status == FAILED`` propagation.
    5. Zero-table SUCCEEDED case.
    6. Multi-table SUCCEEDED case.

The cleanup janitor (``cleanup_discovery_resources``) is intentionally
NOT covered here — that is Task 4.7's scope.

Validates: Requirements 22.1
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler module.
# Mirrors the pattern in test_rds_resources.py / test_create_pipeline_jdbc_source.py
# so the dashboard API handler imports cleanly in a unit-test process
# without the real Lambda layer being available.
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
# its ``from jdbc_helpers import ...`` line. In a real Lambda environment
# the shared layer is mounted at ``/opt/python``; for unit tests we point
# sys.path at the in-tree shared directory.
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
    module-level constant; we patch the module attribute directly so
    test ordering doesn't matter.
    """
    monkeypatch.setattr(
        "handlers.jdbc_resources.GLUE_PIPELINE_ROLE_ARN",
        "arn:aws:iam::123456789012:role/agentic-data-ingestion-glue-pipeline-role",
    )


def _select_rds_body() -> dict:
    """Return a valid select_rds-mode request body for detect_schema_start."""
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


def _glue_connection_body() -> dict:
    """Return a valid glue_connection-mode request body."""
    return {
        "source_config": {
            "glue_connection_name": "my-existing-connection",
            "jdbc_schema": "public",
            "jdbc_table": "orders",
        }
    }


def _route_table_with_s3_endpoint() -> dict:
    """describe_route_tables payload that includes an S3 Gateway Endpoint."""
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
    """describe_route_tables payload missing any S3 Gateway Endpoint route."""
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
# detect_schema_start — happy path / glue_connection mode / pre-flight
# ---------------------------------------------------------------------------


class TestDetectSchemaStart:
    """Tests for the POST /detect-schema entry point."""

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_select_rds_mode_happy_path_returns_202(self, mock_ec2, mock_glue, mock_create_conn):
        """select_rds mode: pre-flight passes → connection + crawler created → 202."""
        mock_ec2.describe_route_tables.return_value = _route_table_with_s3_endpoint()
        # No-op create_crawler / start_crawler.
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})
        # Crawler does not yet exist → helper takes the create-with-Tags path.
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")

        from handlers.jdbc_resources import detect_schema_start

        event = {"body": json.dumps(_select_rds_body())}
        response = detect_schema_start(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["status"] == "RUNNING"
        # crawler_name uses the deterministic agentic-data-ingestion-discover-{hash} convention
        assert body["crawler_name"].startswith("agentic-data-ingestion-discover-")
        # table_prefix mirrors the same hash
        assert body["table_prefix"].startswith("agentic_data_ingestion_")
        assert body["table_prefix"].endswith("_")
        assert "glue_database" in body

        # AWS calls were exercised
        mock_ec2.describe_route_tables.assert_called_once()
        mock_create_conn.assert_called_once()
        mock_glue.create_crawler.assert_called_once()
        mock_glue.start_crawler.assert_called_once()

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_glue_connection_mode_skips_connection_creation(self, mock_ec2, mock_glue, mock_create_conn):
        """glue_connection mode: no Glue connection create; pre-flight N/A here.

        The connection declares no ``PhysicalConnectionRequirements``, so it is
        not VPC-attached and subnet-level S3 routing is not the question — the
        pre-flight resolves ``not_vpc_attached`` and reaches no EC2 call. That is
        why ``describe_route_tables`` is not called, and it is a different claim
        from the pre-2026-09 behaviour, where ``glue_connection`` mode skipped the
        pre-flight unconditionally.
        """
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})
        mock_glue.get_connection.return_value = {"Connection": {"Name": "my-existing-connection"}}
        # Crawler does not yet exist → helper takes the create-with-Tags path.
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")

        from handlers.jdbc_resources import detect_schema_start

        event = {"body": json.dumps(_glue_connection_body())}
        response = detect_schema_start(event, None)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["status"] == "RUNNING"

        # Never read a credential to answer a networking question.
        assert mock_glue.get_connection.call_args.kwargs["HidePassword"] is True
        # No route-table read (connection is not VPC-attached) and no connection
        # creation (the user's connection is reused as-is).
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
    def test_select_rds_mode_missing_s3_endpoint_returns_error(self, mock_ec2, mock_glue, mock_create_conn):
        """No S3 Gateway Endpoint → pre-flight fails, no Glue calls made."""
        mock_ec2.describe_route_tables.return_value = _route_table_without_s3_endpoint()

        from handlers.jdbc_resources import detect_schema_start

        event = {"body": json.dumps(_select_rds_body())}
        response = detect_schema_start(event, None)

        # Handler returns a non-2xx with a descriptive error and never
        # touches Glue / connection creation.
        assert response["statusCode"] >= 400
        body = json.loads(response["body"])
        assert "error" in body
        # Error message mentions S3 Gateway Endpoint remediation.
        assert "S3" in body["error"]

        mock_create_conn.assert_not_called()
        mock_glue.create_crawler.assert_not_called()
        mock_glue.start_crawler.assert_not_called()


# ---------------------------------------------------------------------------
# detect_schema_status — RUNNING → READY happy path, failure modes
# ---------------------------------------------------------------------------


class TestDetectSchemaStatus:
    """Tests for the GET /detect-schema/{crawler_name} entry point."""

    @patch("handlers.jdbc_resources.glue_client")
    def test_running_then_ready_transition(self, mock_glue):
        """Two polls: first returns 202 RUNNING, second returns 200 READY."""
        crawler_name = "agentic-data-ingestion-discover-abc123def456"

        # ----- poll 1: crawler is still running -----
        mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        mock_glue.get_crawler.return_value = {
            "Crawler": {
                "Name": crawler_name,
                "State": "RUNNING",
                "LastCrawl": {},
            }
        }

        from handlers.jdbc_resources import detect_schema_status

        event = {"pathParameters": {"crawler_name": crawler_name}}
        response_running = detect_schema_status(event, None)

        assert response_running["statusCode"] == 202
        body_running = json.loads(response_running["body"])
        assert body_running["status"] == "RUNNING"
        assert body_running["crawler_name"] == crawler_name
        assert body_running["table_prefix"] == "agentic_data_ingestion_abc123def456_"

        # ----- poll 2: crawler finished, exactly one table produced -----
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
                            {
                                "Name": "id",
                                "Type": "bigint",
                                "Comment": "Primary key",
                            },
                            {
                                "Name": "email",
                                "Type": "string",
                                "Comment": "",
                            },
                        ]
                    },
                    "PartitionKeys": [],
                }
            ]
        }

        response_ready = detect_schema_status(event, None)

        assert response_ready["statusCode"] == 200
        body_ready = json.loads(response_ready["body"])
        assert body_ready["status"] == "READY"
        assert body_ready["glue_table"] == "agentic_data_ingestion_abc123def456_orders"
        assert "glue_database" in body_ready

        schema = body_ready["schema"]
        assert len(schema) == 2
        assert schema[0]["name"] == "id"
        assert schema[0]["type"] == "bigint"
        assert schema[0]["nullable"] is True
        assert schema[1]["name"] == "email"

    @patch("handlers.jdbc_resources.glue_client")
    def test_crawler_failed_returns_500_with_error_message(self, mock_glue):
        """LastCrawl.Status=FAILED surfaces the crawler's error verbatim."""
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

        event = {"pathParameters": {"crawler_name": crawler_name}}
        response = detect_schema_status(event, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert body["error_message"] == ("Connection timed out reaching db.example.com:5432")
        assert body["crawler_name"] == crawler_name

        # When the crawler reports FAILED, we don't call get_tables.
        mock_glue.get_tables.assert_not_called()

    @patch("handlers.jdbc_resources.glue_client")
    def test_zero_tables_returns_500(self, mock_glue):
        """Crawler SUCCEEDED but produced zero tables → 500 with hint."""
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

        event = {"pathParameters": {"crawler_name": crawler_name}}
        response = detect_schema_status(event, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert body["crawler_name"] == crawler_name
        # Error message points operators at the two most likely root causes.
        assert "no table" in body["error_message"].lower()

    @patch("handlers.jdbc_resources.glue_client")
    def test_multi_tables_returns_500(self, mock_glue):
        """Crawler produced multiple tables → 500 listing the unexpected names."""
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

        event = {"pathParameters": {"crawler_name": crawler_name}}
        response = detect_schema_status(event, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["status"] == "FAILED"
        assert body["crawler_name"] == crawler_name
        # Error message names BOTH unexpected tables so the operator
        # can investigate.
        assert "agentic_data_ingestion_multitable12_orders" in body["error_message"]
        assert "agentic_data_ingestion_multitable12_orders_audit" in body["error_message"]


# ---------------------------------------------------------------------------
# _create_or_update_crawler — idempotency + Glue tag-deletion race tolerance
# ---------------------------------------------------------------------------


class TestCreateOrUpdateCrawler:
    """Tests for the private crawler create-or-update helper.

    Root cause these tests pin (confirmed via CloudWatch): the helper used
    to unconditionally call ``create_crawler(..., Tags=...)``. When the
    crawler already exists, Glue can return a sticky
    ``InvalidInputException`` ("Tags ... in deleting state") — NOT an
    ``AlreadyExistsException`` — which was uncaught and surfaced as an
    HTTP 500 on every wizard retry. The fix checks existence via
    ``get_crawler`` first (update in place, no Tags) and additionally
    tolerates the sticky ``InvalidInputException`` on the create path.
    """

    @staticmethod
    def _install_glue_exceptions(mock_glue) -> None:
        """Attach the glue_client exception classes the helper references.

        Mirrors the ``type(name, (Exception,), {})`` factory style used by
        the handler tests above so ``except glue_client.exceptions.X``
        clauses resolve to real exception classes and ``str(exc)`` equals
        the message we raise with (needed for the "deleting state" check).
        """
        mock_glue.exceptions.EntityNotFoundException = type(
            "EntityNotFoundException", (Exception,), {}
        )
        mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_glue.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})

    _CRAWLER_NAME = "agentic-data-ingestion-discover-abc123def456"
    _TABLE_PREFIX = "agentic_data_ingestion_abc123def456_"
    _TAGS = {"Type": "agentic-data-ingestion-discovery", "Hash": "abc123def456"}

    def _invoke(self):
        from handlers.jdbc_resources import _create_or_update_crawler

        _create_or_update_crawler(
            crawler_name=self._CRAWLER_NAME,
            connection_name=self._CRAWLER_NAME,
            schema="public",
            table="orders",
            table_prefix=self._TABLE_PREFIX,
            tags=self._TAGS,
        )

    @patch("handlers.jdbc_resources.glue_client")
    def test_crawler_absent_creates_with_tags_no_update(self, mock_glue):
        """(a) get_crawler → EntityNotFoundException → create WITH Tags, no update."""
        self._install_glue_exceptions(mock_glue)
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")

        self._invoke()

        # create_crawler called exactly once, WITH the Tags kwarg.
        mock_glue.create_crawler.assert_called_once()
        assert mock_glue.create_crawler.call_args.kwargs.get("Tags") == self._TAGS
        # No update on the create path.
        mock_glue.update_crawler.assert_not_called()

    @patch("handlers.jdbc_resources.glue_client")
    def test_crawler_exists_updates_in_place_no_create(self, mock_glue):
        """(b) get_crawler returns → update_crawler called (no Tags), no create."""
        self._install_glue_exceptions(mock_glue)
        mock_glue.get_crawler.return_value = {"Crawler": {"Name": self._CRAWLER_NAME}}

        self._invoke()

        # Existing crawler → update in place, create_crawler never called.
        mock_glue.update_crawler.assert_called_once()
        mock_glue.create_crawler.assert_not_called()
        # Tags are intentionally NOT passed on update (Glue rejects them).
        assert "Tags" not in mock_glue.update_crawler.call_args.kwargs

    @patch("handlers.jdbc_resources.glue_client")
    def test_tag_deletion_race_falls_back_to_update(self, mock_glue):
        """(c) create raises InvalidInputException 'deleting state' → update, no raise."""
        self._install_glue_exceptions(mock_glue)
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")
        mock_glue.create_crawler.side_effect = mock_glue.exceptions.InvalidInputException(
            "Tags for resource agentic-data-ingestion-discover-abc123def456 in deleting state"
        )

        # Must NOT propagate — the helper converges via update_crawler.
        self._invoke()

        mock_glue.create_crawler.assert_called_once()
        mock_glue.update_crawler.assert_called_once()
        assert "Tags" not in mock_glue.update_crawler.call_args.kwargs

    @patch("handlers.jdbc_resources.glue_client")
    def test_non_tag_deletion_invalid_input_propagates(self, mock_glue):
        """(d) a non-'deleting state' InvalidInputException still propagates."""
        self._install_glue_exceptions(mock_glue)
        mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")
        mock_glue.create_crawler.side_effect = mock_glue.exceptions.InvalidInputException(
            "Role cannot be empty"
        )

        with pytest.raises(mock_glue.exceptions.InvalidInputException):
            self._invoke()

        # We did not swallow it via update_crawler.
        mock_glue.update_crawler.assert_not_called()


# ---------------------------------------------------------------------------
# glue_connection-mode S3 pre-flight (2026-09 diagnosability fix)
# ---------------------------------------------------------------------------


def _client_error(code: str, message: str = "boom", operation: str = "GetConnection"):
    """Return a real botocore ClientError carrying ``code``."""
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def _vpc_connection(subnet_id: str = "subnet-0abc1234def5678ab") -> dict:
    """GetConnection payload for a VPC-attached connection."""
    return {
        "Connection": {
            "Name": "my-existing-connection",
            "PhysicalConnectionRequirements": {
                "SubnetId": subnet_id,
                "SecurityGroupIdList": ["sg-0abc1234def5678ab"],
            },
        }
    }


def _install_glue_exceptions(mock_glue) -> None:
    """Install the typed exception classes the crawler helper catches."""
    mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
    mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})
    mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
    mock_glue.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})
    mock_glue.get_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException("not found")


class TestGlueConnectionModeS3Preflight:
    """The pre-flight gap that made an opaque crawler failure undiagnosable.

    ``select_rds`` mode has always answered an actionable 400 when the chosen
    subnet has no route to S3. ``glue_connection`` mode ran no such check, so the
    same misconfiguration surfaced as ``Failed to create discovery crawler``
    (HTTP 500) from Glue instead. The connection carries the missing input:
    ``glue:GetConnection`` returns ``PhysicalConnectionRequirements.SubnetId``.

    The asymmetry these tests pin is the *behaviour when the check cannot run*.
    A pre-flight that refuses on its own unavailability is a new way to fail, so
    only two verdicts refuse — "no route" and "no such connection" — and every
    flavour of "could not check" proceeds.
    """

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_vpc_attached_connection_with_s3_route_proceeds(self, mock_ec2, mock_glue, mock_create_conn):
        """Route table grants S3 → 202, and the pre-flight really ran."""
        _install_glue_exceptions(mock_glue)
        mock_glue.get_connection.return_value = _vpc_connection()
        mock_ec2.describe_route_tables.return_value = _route_table_with_s3_endpoint()

        from handlers.jdbc_resources import detect_schema_start

        response = detect_schema_start({"body": json.dumps(_glue_connection_body())}, None)

        assert response["statusCode"] == 202
        # The check ran against the subnet the CONNECTION named, not a request field.
        mock_ec2.describe_route_tables.assert_called()
        assert mock_ec2.describe_route_tables.call_args_list[0].kwargs["Filters"][0]["Values"] == [
            "subnet-0abc1234def5678ab"
        ]
        mock_glue.create_crawler.assert_called_once()
        mock_create_conn.assert_not_called()

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_vpc_attached_connection_without_s3_route_returns_400(self, mock_ec2, mock_glue, mock_create_conn):
        """No S3 route → actionable 400 BEFORE any crawler call is made.

        This is the arm that previously produced the opaque 500.
        """
        _install_glue_exceptions(mock_glue)
        mock_glue.get_connection.return_value = _vpc_connection()
        mock_ec2.describe_route_tables.return_value = _route_table_without_s3_endpoint()

        from handlers.jdbc_resources import detect_schema_start

        response = detect_schema_start({"body": json.dumps(_glue_connection_body())}, None)

        assert response["statusCode"] == 400
        error = json.loads(response["body"])["error"]
        # Names the connection, the subnet, and the remediation.
        assert "my-existing-connection" in error
        assert "subnet-0abc1234def5678ab" in error
        assert "S3 VPC Gateway Endpoint" in error
        # And names the OTHER reading of the same verdict, because
        # _check_s3_endpoint_exists returns False for a failed EC2 call too.
        assert "ec2:DescribeRouteTables" in error
        mock_glue.create_crawler.assert_not_called()
        mock_glue.start_crawler.assert_not_called()

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_missing_connection_returns_400_naming_it(self, mock_ec2, mock_glue, mock_create_conn):
        """GetConnection EntityNotFound → 400 naming connection and region.

        Not an unavailable check: a definite answer that the connection does not
        exist. CreateCrawler would fail on it moments later with less context, so
        refusing here is strictly more diagnostic. This is the wrong-name and
        wrong-region case.
        """
        _install_glue_exceptions(mock_glue)
        mock_glue.get_connection.side_effect = _client_error("EntityNotFoundException")

        from handlers.jdbc_resources import detect_schema_start

        response = detect_schema_start({"body": json.dumps(_glue_connection_body())}, None)

        assert response["statusCode"] == 400
        error = json.loads(response["body"])["error"]
        assert "my-existing-connection" in error
        assert "region" in error.lower()
        mock_glue.create_crawler.assert_not_called()

    @pytest.mark.parametrize(
        "side_effect",
        [
            _client_error("AccessDeniedException"),
            _client_error("ThrottlingException"),
            RuntimeError("transport exploded"),
        ],
        ids=["access-denied", "throttled", "transport"],
    )
    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_unavailable_preflight_proceeds(self, mock_ec2, mock_glue, mock_create_conn, side_effect):
        """A pre-flight that cannot run must not block the flow."""
        _install_glue_exceptions(mock_glue)
        mock_glue.get_connection.side_effect = side_effect

        from handlers.jdbc_resources import detect_schema_start

        response = detect_schema_start({"body": json.dumps(_glue_connection_body())}, None)

        assert response["statusCode"] == 202
        mock_glue.create_crawler.assert_called_once()

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_unavailable_preflight_is_named_in_a_later_crawler_failure(self, mock_ec2, mock_glue, mock_create_conn):
        """An unavailable check is carried forward, not forgotten.

        When the pre-flight could not run AND the crawler then fails, the
        response says the S3 route was never ruled out — otherwise the operator
        chases the crawler error while the real cause went unexamined.
        """
        _install_glue_exceptions(mock_glue)
        mock_glue.get_connection.side_effect = _client_error("AccessDeniedException")
        mock_glue.create_crawler.side_effect = _client_error("InternalServiceException", "boom", "CreateCrawler")

        from handlers.jdbc_resources import detect_schema_start

        response = detect_schema_start({"body": json.dumps(_glue_connection_body())}, None)

        assert response["statusCode"] == 500
        error = json.loads(response["body"])["error"]
        assert "pre-flight could not run" in error
        assert "has not been ruled out" in error


# ---------------------------------------------------------------------------
# Crawler-creation failure classification (2026-09 diagnosability fix)
# ---------------------------------------------------------------------------


class TestCrawlerFailureClassification:
    """``handle_error`` returns 500 for everything; three of these are not 500s.

    ``shared/response_utils.handle_error`` is shared by 66 call sites and is
    deliberately unchanged. The classification is local to the two crawler call
    sites, where the codes Glue can return are known and two of them are not
    server faults.
    """

    @staticmethod
    def _start_with_crawler_error(mock_glue, code: str, *, body: dict | None = None):
        _install_glue_exceptions(mock_glue)
        mock_glue.get_connection.return_value = {"Connection": {"Name": "my-existing-connection"}}
        mock_glue.create_crawler.side_effect = _client_error(code, "boom", "CreateCrawler")

        from handlers.jdbc_resources import detect_schema_start

        response = detect_schema_start({"body": json.dumps(body or _glue_connection_body())}, None)
        return response["statusCode"], json.loads(response["body"])

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_entity_not_found_in_glue_connection_mode_is_400(self, mock_ec2, mock_glue, mock_create_conn):
        """The connection name came from the request, so this is user-fixable."""
        status, body = self._start_with_crawler_error(mock_glue, "EntityNotFoundException")

        assert status == 400
        assert body["reason"] == "discovery_entity_not_found"
        # Names both candidate entities rather than parsing Glue's prose.
        assert "my-existing-connection" in body["error"]
        assert "agentic_data_ingestion_jdbc_discovery" in body["error"]
        assert "details" in body

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_entity_not_found_in_select_rds_mode_is_500(self, mock_ec2, mock_glue, mock_create_conn):
        """In select_rds mode both entities are ours, so it is a server fault."""
        mock_ec2.describe_route_tables.return_value = _route_table_with_s3_endpoint()
        status, body = self._start_with_crawler_error(mock_glue, "EntityNotFoundException", body=_select_rds_body())

        assert status == 500
        assert body["reason"] == "discovery_entity_not_found"
        assert "Foundation" in body["error"]

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_access_denied_is_500_naming_the_stack_to_redeploy(self, mock_ec2, mock_glue, mock_create_conn):
        """Not 403: the end user is authorized, the execution role is not.

        No request change can fix it, so a 4xx would be a false claim. The
        message has to name the two grants and the stack that carries them.
        """
        status, body = self._start_with_crawler_error(mock_glue, "AccessDeniedException")

        assert status == 500
        assert body["reason"] == "discovery_access_denied"
        assert "glue:CreateCrawler" in body["error"]
        assert "iam:PassRole" in body["error"]
        assert "AgenticDataIngestion-Foundation" in body["error"]

    @pytest.mark.parametrize("code", ["InvalidInputException", "ValidationException"])
    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_invalid_input_is_400(self, mock_ec2, mock_glue, mock_create_conn, code):
        """Malformed connection / JDBC target / tag is a request fault."""
        status, body = self._start_with_crawler_error(mock_glue, code)

        assert status == 400
        assert body["reason"] == "discovery_invalid_input"

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_unexpected_code_stays_500(self, mock_ec2, mock_glue, mock_create_conn):
        """Anything genuinely unexpected keeps handle_error's status."""
        status, body = self._start_with_crawler_error(mock_glue, "InternalServiceException")

        assert status == 500
        assert body["reason"] == "discovery_crawler_error"
        assert body["error"].startswith("Failed to create discovery crawler")

    @patch("handlers.jdbc_resources.create_glue_jdbc_connection")
    @patch("handlers.jdbc_resources.glue_client")
    @patch("handlers.jdbc_resources.ec2_client")
    def test_missing_glue_role_arn_is_named_not_swallowed(self, mock_ec2, mock_glue, mock_create_conn):
        """An unset GLUE_PIPELINE_ROLE_ARN used to read "Unexpected error".

        ``_create_or_update_crawler`` raises ``RuntimeError`` for it, which is
        neither a ``ClientError`` nor a ``BotoCoreError``, so it missed the call
        site's except tuple and landed on the handler's outer safety net — the
        least diagnostic possible rendering of a one-line deployment fix.
        """
        _install_glue_exceptions(mock_glue)
        mock_glue.get_connection.return_value = {"Connection": {"Name": "my-existing-connection"}}

        import handlers.jdbc_resources as mod
        from handlers.jdbc_resources import detect_schema_start

        with patch.object(mod, "GLUE_PIPELINE_ROLE_ARN", ""):
            response = detect_schema_start({"body": json.dumps(_glue_connection_body())}, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["reason"] == "discovery_configuration_missing"
        assert "GLUE_PIPELINE_ROLE_ARN" in body["error"]
        assert "Compute" in body["error"]


class TestErrorDetailRedaction:
    """``details`` is now operator-visible, so it must not carry a credential.

    ``LGR-819980f-1``: JDBC credentials are only ever referenced by Secrets
    Manager ARN, and plaintext user/password never appear in Glue job arguments
    or ConnectionProperties. The happy path honours that; an error path that
    echoes a request back does not automatically. ``ParamValidationError`` is a
    ``BotoCoreError``, so it is inside the crawler call sites' except tuple, and
    it formats request parameters into its message.
    """

    def test_password_value_is_replaced(self):
        from handlers.jdbc_resources import _redact_aws_detail

        out = _redact_aws_detail(ValueError('Invalid ConnectionProperties: {"PASSWORD": "hunter2", "HOST": "db"}'))
        assert "hunter2" not in out
        assert "***" in out
        # The diagnostic survives the redaction.
        assert "ConnectionProperties" in out
        assert "HOST" in out

    def test_username_and_secret_id_values_are_replaced(self):
        from handlers.jdbc_resources import _redact_aws_detail

        out = _redact_aws_detail(ValueError("USERNAME=admin SECRET_ID=some/secret/path"))
        assert "admin" not in out
        assert "some/secret/path" not in out

    def test_secret_arn_name_goes_but_region_and_account_stay(self):
        from handlers.jdbc_resources import _redact_aws_detail

        out = _redact_aws_detail(
            ValueError("arn:aws:secretsmanager:us-west-2:123456789012:secret:db-creds-AbCdEf not found")
        )
        assert "db-creds-AbCdEf" not in out
        # Region is the live diagnostic for a wrong-region connection.
        assert "us-west-2" in out
        assert "123456789012" in out

    def test_presigned_url_signature_is_replaced(self):
        from handlers.jdbc_resources import _redact_aws_detail

        out = _redact_aws_detail(
            ValueError(
                "failed on https://b.s3.amazonaws.com/k?X-Amz-Credential=AKIAIOSFODNN7/x"
                "&X-Amz-Signature=abc123def&X-Amz-Expires=900"
            )
        )
        assert "abc123def" not in out
        assert "AKIAIOSFODNN7" not in out
        # A non-signing parameter is left alone.
        assert "X-Amz-Expires=900" in out

    def test_ordinary_aws_message_is_unchanged(self):
        from handlers.jdbc_resources import _redact_aws_detail

        message = (
            "An error occurred (AccessDeniedException) when calling the CreateCrawler "
            "operation: User is not authorized to perform: iam:PassRole"
        )
        assert _redact_aws_detail(ValueError(message)) == message
