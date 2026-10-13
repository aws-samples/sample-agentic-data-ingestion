# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for RDS discovery handlers in handlers/rds_resources.py.

Tests cover happy path, error handling, and empty results for all four handlers:
- list_rds_instances
- list_rds_clusters
- list_glue_jdbc_connections
- list_rds_secrets

Validates: Requirements 1.1-1.4, 2.1-2.4, 3.1-3.3, 4.1-4.4
"""

import json
import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name


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


def _client_error(code="AccessDeniedException", msg="Access denied"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, "TestOp")


# ---------------------------------------------------------------------------
# list_rds_instances
# ---------------------------------------------------------------------------
class TestListRdsInstances:
    """Tests for list_rds_instances handler."""

    @patch("handlers.rds_resources.rds_client")
    def test_happy_path_returns_available_instances(self, mock_rds):
        mock_rds.describe_db_instances.return_value = {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": "mydb-1",
                    "Engine": "postgres",
                    "EngineVersion": "15.4",
                    "Endpoint": {"Address": "mydb-1.xxx.us-west-2.rds.amazonaws.com", "Port": 5432},
                    "DBName": "appdb",
                    "DBInstanceStatus": "available",
                    "DBSubnetGroup": {"VpcId": "vpc-123"},
                },
                {
                    "DBInstanceIdentifier": "mydb-2",
                    "Engine": "mysql",
                    "EngineVersion": "8.0",
                    "Endpoint": {"Address": "mydb-2.xxx.us-west-2.rds.amazonaws.com", "Port": 3306},
                    "DBName": "shop",
                    "DBInstanceStatus": "creating",
                    "DBSubnetGroup": {"VpcId": "vpc-456"},
                },
            ]
        }

        from handlers.rds_resources import list_rds_instances

        response = list_rds_instances({}, None)
        assert response["statusCode"] == 200

        body = json.loads(response["body"])
        assert body["count"] == 1
        inst = body["instances"][0]
        assert inst["db_instance_identifier"] == "mydb-1"
        assert inst["engine"] == "postgres"
        assert inst["endpoint_address"] == "mydb-1.xxx.us-west-2.rds.amazonaws.com"
        assert inst["endpoint_port"] == 5432
        assert inst["db_instance_status"] == "available"
        assert inst["vpc_id"] == "vpc-123"

    @patch("handlers.rds_resources.rds_client")
    def test_empty_results(self, mock_rds):
        mock_rds.describe_db_instances.return_value = {"DBInstances": []}

        from handlers.rds_resources import list_rds_instances

        response = list_rds_instances({}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["instances"] == []
        assert body["count"] == 0

    @patch("handlers.rds_resources.rds_client")
    def test_error_returns_500(self, mock_rds):
        mock_rds.describe_db_instances.side_effect = _client_error()

        from handlers.rds_resources import list_rds_instances

        response = list_rds_instances({}, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body


# ---------------------------------------------------------------------------
# list_rds_clusters
# ---------------------------------------------------------------------------
class TestListRdsClusters:
    """Tests for list_rds_clusters handler."""

    @patch("handlers.rds_resources.rds_client")
    def test_happy_path_returns_available_clusters(self, mock_rds):
        mock_rds.describe_db_clusters.return_value = {
            "DBClusters": [
                {
                    "DBClusterIdentifier": "aurora-1",
                    "Engine": "aurora-postgresql",
                    "EngineVersion": "15.4",
                    "Endpoint": "aurora-1.cluster-xxx.us-west-2.rds.amazonaws.com",
                    "Port": 5432,
                    "DatabaseName": "mydb",
                    "Status": "available",
                },
                {
                    "DBClusterIdentifier": "aurora-2",
                    "Engine": "aurora-mysql",
                    "EngineVersion": "8.0",
                    "Endpoint": "aurora-2.cluster-xxx.us-west-2.rds.amazonaws.com",
                    "Port": 3306,
                    "DatabaseName": "shop",
                    "Status": "modifying",
                },
            ]
        }

        from handlers.rds_resources import list_rds_clusters

        response = list_rds_clusters({}, None)
        assert response["statusCode"] == 200

        body = json.loads(response["body"])
        assert body["count"] == 1
        cluster = body["clusters"][0]
        assert cluster["db_cluster_identifier"] == "aurora-1"
        assert cluster["engine"] == "aurora-postgresql"
        assert cluster["endpoint"] == "aurora-1.cluster-xxx.us-west-2.rds.amazonaws.com"
        assert cluster["port"] == 5432
        assert cluster["status"] == "available"

    @patch("handlers.rds_resources.rds_client")
    def test_empty_results(self, mock_rds):
        mock_rds.describe_db_clusters.return_value = {"DBClusters": []}

        from handlers.rds_resources import list_rds_clusters

        response = list_rds_clusters({}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["clusters"] == []
        assert body["count"] == 0

    @patch("handlers.rds_resources.rds_client")
    def test_error_returns_500(self, mock_rds):
        mock_rds.describe_db_clusters.side_effect = _client_error()

        from handlers.rds_resources import list_rds_clusters

        response = list_rds_clusters({}, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body


# ---------------------------------------------------------------------------
# list_glue_jdbc_connections
# ---------------------------------------------------------------------------
class TestListGlueJdbcConnections:
    """Tests for list_glue_jdbc_connections handler."""

    @patch("handlers.rds_resources.glue_client")
    def test_happy_path_returns_connections(self, mock_glue):
        mock_glue.get_connections.return_value = {
            "ConnectionList": [
                {
                    "Name": "my-jdbc-conn",
                    "ConnectionType": "JDBC",
                    "ConnectionProperties": {
                        "JDBC_CONNECTION_URL": "jdbc:postgresql://host:5432/db",
                        "JDBC_DRIVER_CLASS_NAME": "org.postgresql.Driver",
                    },
                },
            ]
        }

        from handlers.rds_resources import list_glue_jdbc_connections

        response = list_glue_jdbc_connections({}, None)
        assert response["statusCode"] == 200

        body = json.loads(response["body"])
        assert body["count"] == 1
        conn = body["connections"][0]
        assert conn["name"] == "my-jdbc-conn"
        assert conn["connection_type"] == "JDBC"
        assert conn["jdbc_url"] == "jdbc:postgresql://host:5432/db"
        assert conn["jdbc_driver"] == "org.postgresql.Driver"

    @patch("handlers.rds_resources.glue_client")
    def test_empty_results(self, mock_glue):
        mock_glue.get_connections.return_value = {"ConnectionList": []}

        from handlers.rds_resources import list_glue_jdbc_connections

        response = list_glue_jdbc_connections({}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["connections"] == []
        assert body["count"] == 0

    @patch("handlers.rds_resources.glue_client")
    def test_connections_this_system_created_are_not_listed(self, mock_glue):
        """'Use Glue Connection' is for a connection the user owns.

        Each managed connection here is deleted with its owner (a pipeline, or
        the discovery janitor), so offering it would let a second pipeline
        depend on it. The two kept are a user's and a test fixture's.
        """
        url = {"JDBC_CONNECTION_URL": "jdbc:mysql://h:3306/appdb"}
        mock_glue.get_connections.return_value = {
            "ConnectionList": [
                {
                    "Name": "agentic-data-ingestion-mylatesttest-773284d9-source",
                    "Description": "AgenticDataIngestion JDBC connection (agentic-data-ingestion-mylatesttest-773284d9-source)",
                    "ConnectionProperties": url,
                },
                {
                    "Name": "agentic-data-ingestion-mynew233-503c3151-rds",
                    "Description": "AgenticDataIngestion JDBC connection (agentic-data-ingestion-mynew233-503c3151-rds)",
                    "ConnectionProperties": url,
                },
                # Description edited by hand: the name pattern still catches it.
                {
                    "Name": "agentic-data-ingestion-discover-c84a5680b394",
                    "Description": "test url + secret",
                    "ConnectionProperties": url,
                },
                {"Name": "agentic-data-ingestion-test-fixtures-mysql-source", "ConnectionProperties": url},
                {"Name": "warehouse-conn", "Description": "Owned by the data team", "ConnectionProperties": url},
            ]
        }

        from handlers.rds_resources import list_glue_jdbc_connections

        body = json.loads(list_glue_jdbc_connections({}, None)["body"])
        assert [c["name"] for c in body["connections"]] == [
            "agentic-data-ingestion-test-fixtures-mysql-source",
            "warehouse-conn",
        ]
        assert body["count"] == 2

    @patch("handlers.rds_resources.glue_client")
    def test_every_page_is_read_with_passwords_hidden(self, mock_glue):
        def page(name, token=None):
            response = {"ConnectionList": [{"Name": name, "ConnectionProperties": {}}]}
            if token:
                response["NextToken"] = token
            return response

        mock_glue.get_connections.side_effect = [page("first-conn", "t1"), page("second-conn")]

        from handlers.rds_resources import list_glue_jdbc_connections

        body = json.loads(list_glue_jdbc_connections({}, None)["body"])
        assert [c["name"] for c in body["connections"]] == ["first-conn", "second-conn"]

        calls = mock_glue.get_connections.call_args_list
        assert len(calls) == 2
        for call in calls:
            assert call.kwargs["Filter"] == {"ConnectionType": "JDBC"}
            assert call.kwargs["HidePassword"] is True
        assert "NextToken" not in calls[0].kwargs
        assert calls[1].kwargs["NextToken"] == "t1"

    @patch("handlers.rds_resources.glue_client")
    def test_error_returns_500(self, mock_glue):
        mock_glue.get_connections.side_effect = _client_error()

        from handlers.rds_resources import list_glue_jdbc_connections

        response = list_glue_jdbc_connections({}, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body


# ---------------------------------------------------------------------------
# list_rds_secrets
# ---------------------------------------------------------------------------
class TestListRdsSecrets:
    """Tests for list_rds_secrets handler."""

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_happy_path_filters_by_name(self, mock_sm):
        mock_sm.list_secrets.return_value = {
            "SecretList": [
                {
                    "Name": "agentic-data-ingestion/rds-creds",
                    "ARN": "arn:aws:secretsmanager:us-west-2:123:secret:agentic-data-ingestion/rds-creds",
                    "Description": "RDS credentials",
                    "LastChangedDate": datetime(2024, 1, 15, tzinfo=UTC),
                },
                {
                    "Name": "my-database-secret",
                    "ARN": "arn:aws:secretsmanager:us-west-2:123:secret:my-database-secret",
                    "Description": "DB creds",
                },
                {
                    "Name": "unrelated-api-key",
                    "ARN": "arn:aws:secretsmanager:us-west-2:123:secret:unrelated-api-key",
                    "Description": "API key",
                },
            ]
        }

        from handlers.rds_resources import list_rds_secrets

        response = list_rds_secrets({}, None)
        assert response["statusCode"] == 200

        body = json.loads(response["body"])
        assert body["count"] == 2
        names = [s["name"] for s in body["secrets"]]
        assert "agentic-data-ingestion/rds-creds" in names
        assert "my-database-secret" in names
        assert "unrelated-api-key" not in names

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_show_all_returns_everything(self, mock_sm):
        mock_sm.list_secrets.return_value = {
            "SecretList": [
                {"Name": "agentic-data-ingestion/rds-creds", "ARN": "arn:1", "Description": ""},
                {"Name": "unrelated-api-key", "ARN": "arn:2", "Description": ""},
            ]
        }

        from handlers.rds_resources import list_rds_secrets

        event = {"queryStringParameters": {"show_all": "true"}}
        response = list_rds_secrets(event, None)
        assert response["statusCode"] == 200

        body = json.loads(response["body"])
        assert body["count"] == 2

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_empty_results(self, mock_sm):
        mock_sm.list_secrets.return_value = {"SecretList": []}

        from handlers.rds_resources import list_rds_secrets

        response = list_rds_secrets({}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["secrets"] == []
        assert body["count"] == 0

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_error_returns_500(self, mock_sm):
        mock_sm.list_secrets.side_effect = _client_error()

        from handlers.rds_resources import list_rds_secrets

        response = list_rds_secrets({}, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_null_query_params_defaults_to_filtered(self, mock_sm):
        mock_sm.list_secrets.return_value = {
            "SecretList": [
                {"Name": "my-db-secret", "ARN": "arn:1", "Description": ""},
                {"Name": "api-key", "ARN": "arn:2", "Description": ""},
            ]
        }

        from handlers.rds_resources import list_rds_secrets

        response = list_rds_secrets({"queryStringParameters": None}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["count"] == 1
        assert body["secrets"][0]["name"] == "my-db-secret"
