# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for RDS discovery handlers.

Feature: p14-rds-database-targets, Property 1: Discovery response field completeness
Feature: p14-rds-database-targets, Property 2: Discovery status filtering
Feature: p14-rds-database-targets, Property 3: Secret name filtering

Uses Hypothesis to verify that discovery handlers always return complete
fields, correctly filter by status, and correctly filter secrets by name.

**Validates: Requirements 1.2, 1.3, 2.2, 2.3, 3.2, 4.2, 4.3**
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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
# Strategies
# ---------------------------------------------------------------------------
identifier_st = st.from_regex(r"[a-z][a-z0-9\-]{0,30}", fullmatch=True)
engine_st = st.sampled_from(
    [
        "postgres",
        "mysql",
        "aurora-postgresql",
        "aurora-mysql",
        "mariadb",
        "oracle-ee",
        "oracle-se2",
        "sqlserver-ee",
        "sqlserver-se",
        "sqlserver-ex",
        "sqlserver-web",
    ]
)
version_st = st.from_regex(r"[0-9]{1,2}\.[0-9]{1,2}", fullmatch=True)
host_st = st.from_regex(r"[a-z][a-z0-9\-]{0,20}\.[a-z]{2,6}", fullmatch=True)
port_st = st.integers(min_value=1, max_value=65535)
db_name_st = st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)
vpc_st = st.from_regex(r"vpc-[a-f0-9]{8}", fullmatch=True)
status_st = st.sampled_from(["available", "creating", "deleting", "modifying", "backing-up", "stopped"])
secret_name_st = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/"),
    min_size=1,
    max_size=40,
)


# Strategy for a single RDS instance as returned by AWS API
rds_instance_st = st.fixed_dictionaries(
    {
        "DBInstanceIdentifier": identifier_st,
        "Engine": engine_st,
        "EngineVersion": version_st,
        "Endpoint": st.fixed_dictionaries({"Address": host_st, "Port": port_st}),
        "DBName": db_name_st,
        "DBInstanceStatus": status_st,
        "DBSubnetGroup": st.fixed_dictionaries({"VpcId": vpc_st}),
    }
)

# Strategy for a single Aurora cluster as returned by AWS API
rds_cluster_st = st.fixed_dictionaries(
    {
        "DBClusterIdentifier": identifier_st,
        "Engine": engine_st,
        "EngineVersion": version_st,
        "Endpoint": host_st,
        "Port": port_st,
        "DatabaseName": db_name_st,
        "Status": status_st,
    }
)

# Strategy for a Glue JDBC connection as returned by AWS API
glue_conn_st = st.fixed_dictionaries(
    {
        "Name": identifier_st,
        "ConnectionType": st.just("JDBC"),
        "ConnectionProperties": st.fixed_dictionaries(
            {
                "JDBC_CONNECTION_URL": st.from_regex(
                    r"jdbc:postgresql://[a-z]{3,10}:[0-9]{4}/[a-z]{3,10}", fullmatch=True
                ),
                "JDBC_DRIVER_CLASS_NAME": st.just("org.postgresql.Driver"),
            }
        ),
    }
)

# Strategy for a secret as returned by AWS API
secret_st = st.fixed_dictionaries(
    {
        "Name": secret_name_st,
        "ARN": st.from_regex(r"arn:aws:secretsmanager:us-west-2:123456789012:secret:[a-z\-]{5,20}", fullmatch=True),
        "Description": st.text(max_size=50),
    }
)


# ---------------------------------------------------------------------------
# Feature: p14-rds-database-targets, Property 1: Discovery response field
# completeness
# ---------------------------------------------------------------------------
INSTANCE_REQUIRED_FIELDS = {
    "db_instance_identifier",
    "engine",
    "engine_version",
    "endpoint_address",
    "endpoint_port",
    "db_name",
    "db_instance_status",
    "vpc_id",
}
CLUSTER_REQUIRED_FIELDS = {
    "db_cluster_identifier",
    "engine",
    "engine_version",
    "endpoint",
    "port",
    "database_name",
    "status",
    "vpc_id",
}
CONNECTION_REQUIRED_FIELDS = {
    "name",
    "connection_type",
    "jdbc_url",
    "jdbc_driver",
    "status",
}
SECRET_REQUIRED_FIELDS = {
    "name",
    "arn",
    "description",
    "last_changed_date",
}


class TestFieldCompleteness:
    """Property 1: Discovery response field completeness.

    For any resource returned by the discovery APIs, the response object
    SHALL contain all required fields for its resource type.

    **Validates: Requirements 1.2, 2.2, 3.2, 4.2**
    """

    @given(instance=rds_instance_st)
    @settings(max_examples=100, deadline=None)
    @patch("handlers.rds_resources.rds_client")
    def test_rds_instance_has_all_required_fields(self, mock_rds, instance):
        # Force available so it passes the filter
        instance["DBInstanceStatus"] = "available"
        mock_rds.describe_db_instances.return_value = {"DBInstances": [instance]}

        from handlers.rds_resources import list_rds_instances

        response = list_rds_instances({}, None)
        body = json.loads(response["body"])
        assert len(body["instances"]) == 1
        assert INSTANCE_REQUIRED_FIELDS.issubset(body["instances"][0].keys())

    @given(cluster=rds_cluster_st)
    @settings(max_examples=100, deadline=None)
    @patch("handlers.rds_resources.rds_client")
    def test_rds_cluster_has_all_required_fields(self, mock_rds, cluster):
        cluster["Status"] = "available"
        mock_rds.describe_db_clusters.return_value = {"DBClusters": [cluster]}

        from handlers.rds_resources import list_rds_clusters

        response = list_rds_clusters({}, None)
        body = json.loads(response["body"])
        assert len(body["clusters"]) == 1
        assert CLUSTER_REQUIRED_FIELDS.issubset(body["clusters"][0].keys())

    @given(conn=glue_conn_st)
    @settings(max_examples=100, deadline=None)
    @patch("handlers.rds_resources.glue_client")
    def test_glue_connection_has_all_required_fields(self, mock_glue, conn):
        mock_glue.get_connections.return_value = {"ConnectionList": [conn]}

        from handlers.rds_resources import list_glue_jdbc_connections

        response = list_glue_jdbc_connections({}, None)
        body = json.loads(response["body"])
        assert len(body["connections"]) == 1
        assert CONNECTION_REQUIRED_FIELDS.issubset(body["connections"][0].keys())

    @given(secret=secret_st)
    @settings(max_examples=100, deadline=None)
    @patch("handlers.rds_resources.secretsmanager_client")
    def test_secret_has_all_required_fields(self, mock_sm, secret):
        # Force a matching name so it passes the filter
        secret["Name"] = "my-rds-" + secret["Name"]
        mock_sm.list_secrets.return_value = {"SecretList": [secret]}

        from handlers.rds_resources import list_rds_secrets

        response = list_rds_secrets({}, None)
        body = json.loads(response["body"])
        assert len(body["secrets"]) >= 1
        assert SECRET_REQUIRED_FIELDS.issubset(body["secrets"][0].keys())


# ---------------------------------------------------------------------------
# Feature: p14-rds-database-targets, Property 2: Discovery status filtering
# ---------------------------------------------------------------------------
class TestStatusFiltering:
    """Property 2: Discovery status filtering.

    For any set of RDS instances or Aurora clusters with mixed statuses,
    the handler SHALL return only items where status is 'available'.
    The result length SHALL be <= input length, and every item SHALL
    have status 'available'.

    **Validates: Requirements 1.3, 2.3**
    """

    @given(instances=st.lists(rds_instance_st, min_size=0, max_size=15))
    @settings(max_examples=100, deadline=None)
    @patch("handlers.rds_resources.rds_client")
    def test_only_available_instances_returned(self, mock_rds, instances):
        mock_rds.describe_db_instances.return_value = {"DBInstances": instances}

        from handlers.rds_resources import list_rds_instances

        response = list_rds_instances({}, None)
        body = json.loads(response["body"])

        # Result length <= input length
        assert len(body["instances"]) <= len(instances)

        # Every returned instance has status 'available'
        for inst in body["instances"]:
            assert inst["db_instance_status"] == "available"

        # Count matches expected
        expected_count = sum(1 for i in instances if i.get("DBInstanceStatus") == "available")
        assert body["count"] == expected_count

    @given(clusters=st.lists(rds_cluster_st, min_size=0, max_size=15))
    @settings(max_examples=100, deadline=None)
    @patch("handlers.rds_resources.rds_client")
    def test_only_available_clusters_returned(self, mock_rds, clusters):
        mock_rds.describe_db_clusters.return_value = {"DBClusters": clusters}

        from handlers.rds_resources import list_rds_clusters

        response = list_rds_clusters({}, None)
        body = json.loads(response["body"])

        assert len(body["clusters"]) <= len(clusters)

        for cluster in body["clusters"]:
            assert cluster["status"] == "available"

        expected_count = sum(1 for c in clusters if c.get("Status") == "available")
        assert body["count"] == expected_count


# ---------------------------------------------------------------------------
# Feature: p14-rds-database-targets, Property 3: Secret name filtering
# ---------------------------------------------------------------------------
import re

# Aligned with the broader P20 ``SECRET_NAME_PATTERN`` in
# ``handlers/rds_resources.py`` so the test asserts the same shape the
# code now uses (P20 widened the filter from "rds|database|db|jdbc" to
# also include the engine names so test-fixtures secrets like
# ``agentic-data-ingestion/test-fixtures/mysql-source`` surface in the dropdown).
_PATTERN = re.compile(r"db|database|rds|jdbc|postgres|mysql|oracle|sqlserver", re.IGNORECASE)


class TestSecretNameFiltering:
    """Property 3: Secret name filtering.

    When show_all is not set, only secrets whose name contains 'rds',
    'database', 'db', or 'jdbc' (case-insensitive) are returned.
    When show_all=true, all secrets are returned unchanged.

    **Validates: Requirements 4.3**
    """

    @given(secrets=st.lists(secret_st, min_size=0, max_size=15))
    @settings(max_examples=100, deadline=None)
    @patch("handlers.rds_resources.secretsmanager_client")
    def test_filtered_mode_only_matching_names(self, mock_sm, secrets):
        mock_sm.list_secrets.return_value = {"SecretList": secrets}

        from handlers.rds_resources import list_rds_secrets

        response = list_rds_secrets({}, None)
        body = json.loads(response["body"])

        # Every returned secret name must match the pattern
        for s in body["secrets"]:
            assert _PATTERN.search(s["name"]), f"Secret '{s['name']}' should not pass filter"

        # Count matches expected
        expected = sum(1 for s in secrets if _PATTERN.search(s.get("Name", "")))
        assert body["count"] == expected

    @given(secrets=st.lists(secret_st, min_size=0, max_size=15))
    @settings(max_examples=100, deadline=None)
    @patch("handlers.rds_resources.secretsmanager_client")
    def test_show_all_returns_everything(self, mock_sm, secrets):
        mock_sm.list_secrets.return_value = {"SecretList": secrets}

        from handlers.rds_resources import list_rds_secrets

        event = {"queryStringParameters": {"show_all": "true"}}
        response = list_rds_secrets(event, None)
        body = json.loads(response["body"])

        assert body["count"] == len(secrets)
