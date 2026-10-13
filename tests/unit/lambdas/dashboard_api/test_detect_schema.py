# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test: detect_schema handler returns correct schema for Glue Catalog table
and returns 400 for unsupported target types.

Validates: AC-6.4
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

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
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)


@patch("handlers.resources.glue_client")
def test_detect_schema_glue_catalog_returns_columns(mock_glue):
    """detect_schema returns correct schema for a Glue Catalog table."""
    # The database name carries the `agentic_data_ingestion_` prefix because the
    # pre-flight now refuses anything else: the per-pipeline Glue role is scoped
    # to `database/agentic_data_ingestion_*`, so a pipeline targeting a database
    # named `mydb` (what this test used to assert a 200 for) would be denied at
    # run time. This table has no `StorageDescriptor.Location`, which keeps it on
    # the pre-flight's proceed-on-unknown path — see
    # `test_detect_schema_glue_catalog_proceeds_when_location_unreadable`.
    mock_glue.get_table.return_value = {
        "Table": {
            "Name": "users",
            "DatabaseName": "agentic_data_ingestion_mydb",
            "StorageDescriptor": {
                "Columns": [
                    {"Name": "user_id", "Type": "bigint", "Comment": "Primary key"},
                    {"Name": "email", "Type": "string", "Comment": "Email address"},
                    {"Name": "created_at", "Type": "timestamp", "Comment": ""},
                ],
            },
            "PartitionKeys": [
                {"Name": "year", "Type": "int", "Comment": "Partition year"},
            ],
        }
    }

    from handlers.resources import detect_schema

    event = {
        "queryStringParameters": {
            "target_type": "glue_catalog",
            "target_config": json.dumps(
                {
                    "database_name": "agentic_data_ingestion_mydb",
                    "table_name": "users",
                }
            ),
        },
    }

    response = detect_schema(event, None)
    assert response["statusCode"] == 200

    body = json.loads(response["body"])
    schema = body["schema"]
    assert len(schema) == 4  # 3 columns + 1 partition key

    assert schema[0]["name"] == "user_id"
    assert schema[0]["type"] == "bigint"
    assert schema[0]["description"] == "Primary key"

    assert schema[1]["name"] == "email"
    assert schema[1]["type"] == "string"

    # Partition key should be included
    assert schema[3]["name"] == "year"
    assert schema[3]["type"] == "int"

    mock_glue.get_table.assert_called_once_with(
        DatabaseName="agentic_data_ingestion_mydb",
        Name="users",
    )


def test_detect_schema_unsupported_type_returns_400():
    """detect_schema returns 400 for unsupported target types like jdbc."""
    from handlers.resources import detect_schema

    event = {
        "queryStringParameters": {
            "target_type": "jdbc",
            "target_config": json.dumps({"connection_url": "jdbc:postgresql://host/db"}),
        },
    }

    response = detect_schema(event, None)
    assert response["statusCode"] == 400

    body = json.loads(response["body"])
    assert "not supported" in body["error"].lower()


def test_detect_schema_missing_config_returns_400():
    """detect_schema returns 400 when required config fields are missing."""
    from handlers.resources import detect_schema

    event = {
        "queryStringParameters": {
            "target_type": "glue_catalog",
            "target_config": json.dumps({}),  # Missing database_name and table_name
        },
    }

    response = detect_schema(event, None)
    assert response["statusCode"] == 400

    body = json.loads(response["body"])
    assert "required" in body["error"].lower()


def test_detect_schema_invalid_json_returns_400():
    """detect_schema returns 400 for invalid target_config JSON."""
    from handlers.resources import detect_schema

    event = {
        "queryStringParameters": {
            "target_type": "glue_catalog",
            "target_config": "not-valid-json{{{",
        },
    }

    response = detect_schema(event, None)
    assert response["statusCode"] == 400

    body = json.loads(response["body"])
    assert "invalid" in body["error"].lower() or "json" in body["error"].lower()


@patch("handlers.resources.s3tables_client")
@patch("handlers.resources.boto3")
def test_detect_schema_s3_tables_returns_schema(mock_boto3, mock_s3tables):
    """detect_schema returns schema for S3 Tables target."""
    # The source reads metadataLocation from get_table, then fetches the
    # Iceberg metadata JSON from S3 to extract the schema.

    mock_s3tables.get_table.return_value = {
        "name": "events",
        "format": "ICEBERG",
        "metadataLocation": "s3://my-bucket/metadata/v1.metadata.json",
    }

    iceberg_metadata = json.dumps(
        {
            "format-version": 2,
            "current-schema-id": 0,
            "schemas": [
                {
                    "schema-id": 0,
                    "fields": [
                        {"id": 1, "name": "event_id", "type": "string", "required": True},
                        {"id": 2, "name": "timestamp", "type": "timestamptz", "required": False},
                    ],
                }
            ],
        }
    ).encode()

    mock_s3 = MagicMock()
    mock_boto3.client.return_value = mock_s3
    body_stream = MagicMock()
    body_stream.read.return_value = iceberg_metadata
    mock_s3.get_object.return_value = {"Body": body_stream}

    from handlers.resources import detect_schema

    event = {
        "queryStringParameters": {
            "target_type": "s3_tables",
            "target_config": json.dumps(
                {
                    "table_bucket_arn": "arn:aws:s3tables:us-west-2:123456789012:bucket/my-bucket",
                    "namespace": "my_ns",
                    "table_name": "events",
                }
            ),
        },
    }

    response = detect_schema(event, None)
    assert response["statusCode"] == 200

    body = json.loads(response["body"])
    schema = body["schema"]
    assert len(schema) == 2
    assert schema[0]["name"] == "event_id"


# ---------------------------------------------------------------------------
# GLUE_CATALOG pre-flight
# ---------------------------------------------------------------------------
#
# GLUE_CATALOG is the one target type that does not create its destination
# (`create_infrastructure/lambda_function.py:801`), so it carries three
# preconditions that used to surface only as a mid-run Glue AccessDenied. The
# single `glue:GetTable` this handler already made answers all three. Each
# refusal is asserted below, plus the case where the check could not run — which
# proceeds rather than inventing a refusal, matching
# `jdbc_resources._connection_s3_preflight`'s posture.


def _glue_event(database_name: str, table_name: str) -> dict:
    """Build a detect-schema event for a glue_catalog target."""
    return {
        "queryStringParameters": {
            "target_type": "glue_catalog",
            "target_config": json.dumps({"database_name": database_name, "table_name": table_name}),
        },
    }


def _client_error(code: str, message: str = "boom") -> Exception:
    """A botocore-shaped exception carrying ``code``.

    Built by hand rather than taken from ``glue_client.exceptions.*`` because the
    client is a ``MagicMock`` here — the handler classifies by reading
    ``exc.response["Error"]["Code"]`` for exactly that reason.
    """
    exc = Exception(message)
    exc.response = {"Error": {"Code": code, "Message": message}}  # type: ignore[attr-defined]
    return exc


@patch("handlers.resources.glue_client")
def test_detect_schema_glue_catalog_refuses_unprefixed_database(mock_glue):
    """Precondition 2: a database outside agentic_data_ingestion_* is a 400.

    Also asserts the check runs BEFORE the AWS call — it is a pure string
    comparison and should not spend a round trip to reach the same answer.
    """
    from handlers.resources import detect_schema

    response = detect_schema(_glue_event("analytics_prod", "orders"), None)

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["reason"] == "glue_catalog_database_prefix"
    # Names the required prefix and the name given.
    assert "agentic_data_ingestion_" in body["error"]
    assert "analytics_prod" in body["error"]
    mock_glue.get_table.assert_not_called()


@patch("handlers.resources.glue_client")
def test_detect_schema_glue_catalog_refuses_absent_table(mock_glue):
    """Precondition 1: an unregistered table is a 400, not an opaque 500.

    Before the pre-flight the `get_table` call was unguarded, so this landed on
    the handler's outer `except Exception` as a 500 with no actionable content.
    """
    mock_glue.get_table.side_effect = _client_error("EntityNotFoundException", "Table not found")

    from handlers.resources import detect_schema

    response = detect_schema(_glue_event("agentic_data_ingestion_sales", "orders"), None)

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["reason"] == "glue_catalog_table_not_found"
    # Names database.table and says the pipeline does not create the table.
    assert "agentic_data_ingestion_sales" in body["error"]
    assert "orders" in body["error"]
    assert "does not create catalog tables" in body["error"]


@patch("handlers.resources.get_aws_account_id", return_value="123456789012")
@patch("handlers.resources.glue_client")
def test_detect_schema_glue_catalog_refuses_unwritable_location(mock_glue, _mock_account):
    """Precondition 3: an S3 location outside the writable set is a 400.

    The per-pipeline `agentic-data-ingestion-table-<pipeline_id>` bucket is the
    live trap: the pipeline role's policy names it, but it is never created for a
    GLUE_CATALOG target, so a table pointed at one fails on the write.
    """
    mock_glue.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {
                "Columns": [{"Name": "id", "Type": "string", "Comment": ""}],
                "Location": "s3://agentic-data-ingestion-table-abc123/orders/",
            },
        }
    }

    from handlers.resources import detect_schema

    response = detect_schema(_glue_event("agentic_data_ingestion_sales", "orders"), None)

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["reason"] == "glue_catalog_location_not_writable"
    # Names the location found and what it must be under.
    assert "s3://agentic-data-ingestion-table-abc123/orders/" in body["error"]
    assert "adi-processed-files-123456789012-us-west-2" in body["error"]
    # This message must NOT advertise the source bucket as an acceptable
    # alternative: it is IAM-writable, so it read as one before, but writes there
    # re-trigger the pipeline. It appears only in the list of refusals.
    assert "is the pipeline's own upload trigger source" in body["error"]


@patch("handlers.resources.get_aws_account_id", return_value="123456789012")
@patch("handlers.resources.glue_client")
def test_detect_schema_glue_catalog_refuses_self_triggering_location(mock_glue, _mock_account):
    """The source bucket is refused even though the pipeline role can write it.

    This is the regression that matters, because the IAM grant will keep
    suggesting otherwise: `create_glue_pipeline/iam_roles.py:227` genuinely grants
    `s3:PutObject` on `adi-source-files-{account}-{region}/*`, so anyone deriving
    the writable set from that statement alone will want to accept this location.

    It must not be accepted. `foundation-stack.ts:76` sets
    `eventBridgeEnabled: true` on that bucket and `S3UploadTriggerRule`
    (`orchestration-stack.ts:1574-1576`) matches every key that is not a folder
    marker, so each data file the table writes raises an upload event that starts
    another execution — the pipeline re-triggers on its own output.

    The refusal must also carry its own reason and say why, not reuse the
    "role cannot write here" refusal, which would be a false statement about this
    bucket.
    """
    mock_glue.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {
                "Columns": [{"Name": "id", "Type": "string", "Comment": ""}],
                "Location": "s3://adi-source-files-123456789012-us-west-2/gc/orders/",
            },
        }
    }

    from handlers.resources import detect_schema

    response = detect_schema(_glue_event("agentic_data_ingestion_sales", "orders"), None)

    assert response["statusCode"] == 400
    body = json.loads(response["body"])
    assert body["reason"] == "glue_catalog_location_self_triggering"
    error = body["error"]
    # Names the location and the bucket it objected to.
    assert "s3://adi-source-files-123456789012-us-west-2/gc/orders/" in error
    assert "adi-source-files-123456789012-us-west-2" in error
    # States the self-trigger reason, and that this is NOT a permissions refusal
    # — the distinction a reader holding the IAM policy needs.
    assert "re-trigger" in error
    assert "watches for uploads" in error
    assert "not about permissions" in error
    # Points at the bucket that does work.
    assert "adi-processed-files-123456789012-us-west-2" in error


@patch("handlers.resources.get_aws_account_id", return_value="123456789012")
@patch("handlers.resources.glue_client")
def test_detect_schema_glue_catalog_accepts_writable_location(mock_glue, _mock_account):
    """A location in the pipeline-writable set passes and returns the schema."""
    mock_glue.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {
                "Columns": [{"Name": "id", "Type": "string", "Comment": ""}],
                "Location": "s3://adi-processed-files-123456789012-us-west-2/gc/orders/",
            },
        }
    }

    from handlers.resources import detect_schema

    response = detect_schema(_glue_event("agentic_data_ingestion_sales", "orders"), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["schema"][0]["name"] == "id"


@patch("handlers.resources.glue_client")
def test_detect_schema_glue_catalog_proceeds_when_location_unreadable(mock_glue):
    """A check that could not run must not become a new way to fail.

    `get_table` succeeded, so preconditions 1 and 2 are satisfied; `Location` is
    absent, so precondition 3 is unknown. Proceed — the same posture as
    `_connection_s3_preflight`'s `_PREFLIGHT_UNKNOWN` verdict.
    """
    mock_glue.get_table.return_value = {
        "Table": {"StorageDescriptor": {"Columns": [{"Name": "id", "Type": "string", "Comment": ""}]}}
    }

    from handlers.resources import detect_schema

    response = detect_schema(_glue_event("agentic_data_ingestion_sales", "orders"), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["schema"][0]["name"] == "id"


@patch("handlers.resources.get_aws_account_id", side_effect=Exception("STS unavailable"))
@patch("handlers.resources.glue_client")
def test_detect_schema_glue_catalog_proceeds_when_account_id_unresolvable(mock_glue, _mock_account):
    """`get_aws_account_id` reaching STS can fail on its own; that is not a refusal.

    Without the account ID the writable-bucket set cannot be constructed, so the
    check is unavailable — and an unavailable check proceeds, even though this
    location would have been refused had the ID resolved.
    """
    mock_glue.get_table.return_value = {
        "Table": {
            "StorageDescriptor": {
                "Columns": [{"Name": "id", "Type": "string", "Comment": ""}],
                "Location": "s3://someone-elses-bucket/orders/",
            },
        }
    }

    from handlers.resources import detect_schema

    response = detect_schema(_glue_event("agentic_data_ingestion_sales", "orders"), None)

    assert response["statusCode"] == 200


@patch("handlers.resources.glue_client")
def test_detect_schema_glue_catalog_access_denied_is_500_not_403(mock_glue):
    """AccessDenied on the dashboard API's own role is a deployment matter.

    Not 403: the end user is authorized (the Cognito authorizer already passed),
    so a 403 would claim something false about them and sit next to the client's
    401 session-expiry path. Not 400 either: no request change can fix it. Same
    split, same reason, as `jdbc_resources._crawler_failure_response`.
    """
    mock_glue.get_table.side_effect = _client_error("AccessDeniedException", "not authorized")

    from handlers.resources import detect_schema

    response = detect_schema(_glue_event("agentic_data_ingestion_sales", "orders"), None)

    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["reason"] == "glue_catalog_access_denied"
    # Distinguishable from a user error, and names what to redeploy.
    assert "execution role" in body["error"]
    assert "Nothing in the wizard can fix this" in body["error"]
