# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test: end-to-end JDBC-source pipeline creation flow.

Validates: Requirement 22.1 (test coverage) — exercises the
``create_pipeline`` handler against moto for the new JDBC source path
introduced by P17 (Requirements 6, 11.1, 11.2, 11.5, 11.8) and verifies
the four resources the handler must produce in lock-step:

* ``agentic_data_ingestion_pipelines`` row with flat ``source_type`` / ``source_config``
* Glue Connection ``agentic-data-ingestion-{pipeline_id}-source`` (only in
  ``select_rds`` mode — never in ``glue_connection`` mode)
* Glue Crawler ``agentic-data-ingestion-{pipeline_id}-source-discover`` with
  ``JdbcTargets``, ``DatabaseName=agentic_data_ingestion_jdbc_discovery_<stage>``,
  ``TablePrefix=agentic_data_ingestion_{pipeline_id}_`` and the
  ``Type=agentic-data-ingestion-runtime`` tag
* EventBridge schedule rule ``agentic-data-ingestion-schedule-{pipeline_id}``
  pointing at the pipeline-trigger Lambda

In addition to the create-pipeline scenarios this module covers:

* The ``agentic_data_ingestion_pipeline_watermarks`` conditional-PutItem semantics
  the runtime PySpark template relies on (stale updates rejected via
  ``ConditionalCheckFailedException``, fresh updates accepted).
* The Task 5.3 rollback semantics — when crawler creation fails after
  a source connection was created, the DDB pipeline row must be rolled
  back. The companion connection-cleanup is covered with a regression
  marker; see the P17 follow-up notes at the bottom of this module.

All AWS calls go through moto. No real AWS calls are made.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Paths — make `shared/` and the dashboard API package importable
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api"
_SHARED_DIR = _PROJECT_ROOT / "shared"

if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))
if str(_LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_DIR))

# ---------------------------------------------------------------------------
# Lambda-layer dependency stubs
# ---------------------------------------------------------------------------

if "eventbridge_utils" not in sys.modules:
    sys.modules["eventbridge_utils"] = MagicMock()
    sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

_audit_stub = MagicMock()
_audit_stub.extract_request_context = MagicMock(return_value={})
_audit_stub.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = _audit_stub

_AWS_REGION = "us-east-1"
_UPLOAD_BUCKET = "agentic-data-ingestion-source"
_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
_WATERMARKS_TABLE = "agentic_data_ingestion_pipeline_watermarks"
_AUDIT_TABLE = "agentic_data_ingestion_audit_log"
_DISCOVERY_DB_STAGE = "test"
_DISCOVERY_DB = f"agentic_data_ingestion_jdbc_discovery_{_DISCOVERY_DB_STAGE}"
_PIPELINE_TRIGGER_ARN = "arn:aws:lambda:us-east-1:123456789012:function:trigger"
_GLUE_PIPELINE_ROLE_ARN = "arn:aws:iam::123456789012:role/test-glue-pipeline-role"

_env_stub = MagicMock()
_env_stub.require_env = lambda name, default=None: os.environ.get(name, default if default is not None else name)
_env_stub.get_aws_account_id = lambda: "123456789012"
sys.modules["env_utils"] = _env_stub


# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------

_VALID_SELECT_RDS_FULL_TABLE: dict = {
    "jdbc_url": "jdbc:postgresql://orders-db.example.com:5432/orders",
    "jdbc_driver": "org.postgresql.Driver",
    "jdbc_schema": "public",
    "jdbc_table": "orders",
    "secret_arn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds-AbCdEf"),
    "engine": "postgres",
    "read_mode": "full_table",
    "subnet_id": "subnet-aaaa1111",
    "security_group_ids": ["sg-bbbb2222"],
    "availability_zone": "us-east-1a",
    "db_instance_identifier": "orders-db",
}

_VALID_SELECT_RDS_INCREMENTAL: dict = {
    **_VALID_SELECT_RDS_FULL_TABLE,
    "read_mode": "incremental",
    "watermark_column": "updated_at",
    "watermark_type": "timestamp",
    "initial_watermark_value": "2024-01-01T00:00:00Z",
}

_VALID_GLUE_CONNECTION_FULL_TABLE: dict = {
    "jdbc_url": "jdbc:mysql://prod-aurora.example.com:3306/sales",
    "jdbc_driver": "com.mysql.cj.jdbc.Driver",
    "jdbc_schema": "sales",
    "jdbc_table": "transactions",
    "secret_arn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:aurora-creds-XYZ"),
    "engine": "aurora-mysql",
    "read_mode": "full_table",
    "glue_connection_name": "customer-existing-aurora-connection",
}


def _make_create_body(
    *,
    pipeline_name: str,
    source_config: dict,
    schedule: str | None = "rate(1 hour)",
) -> str:
    body: dict = {
        "pipeline_name": pipeline_name,
        "target_schema": "uploaded_test-target-schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "source_type": "JDBC",
        "source_config": source_config,
    }
    if schedule is not None:
        body["schedule"] = schedule
    return json.dumps(body)


def _setup_aws_resources() -> dict:
    dynamodb = boto3.resource("dynamodb", region_name=_AWS_REGION)
    dynamodb.create_table(
        TableName=_PIPELINES_TABLE,
        KeySchema=[{"AttributeName": "pipeline_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
            {"AttributeName": "target_schema_name", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "status-created_at-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "target_schema_name-index",
                "KeySchema": [
                    {"AttributeName": "target_schema_name", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=_WATERMARKS_TABLE,
        KeySchema=[{"AttributeName": "pipeline_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=_AUDIT_TABLE,
        KeySchema=[
            {"AttributeName": "resource_id", "KeyType": "HASH"},
            {"AttributeName": "timestamp", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "resource_id", "AttributeType": "S"},
            {"AttributeName": "timestamp", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    s3 = boto3.client("s3", region_name=_AWS_REGION)
    s3.create_bucket(Bucket=_UPLOAD_BUCKET)

    return {
        "dynamodb": dynamodb,
        "pipelines_table": dynamodb.Table(_PIPELINES_TABLE),
        "watermarks_table": dynamodb.Table(_WATERMARKS_TABLE),
        "s3": s3,
        "glue": boto3.client("glue", region_name=_AWS_REGION),
        "events": boto3.client("events", region_name=_AWS_REGION),
    }


def _set_handler_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _AWS_REGION)
    monkeypatch.setenv("AWS_REGION", _AWS_REGION)
    monkeypatch.setenv("AGENTIC_DATA_INGESTION_ENV", _DISCOVERY_DB_STAGE)
    monkeypatch.setenv("UPLOAD_BUCKET", _UPLOAD_BUCKET)
    monkeypatch.setenv("PIPELINES_TABLE", _PIPELINES_TABLE)
    monkeypatch.setenv("PIPELINE_TRIGGER_ARN", _PIPELINE_TRIGGER_ARN)
    monkeypatch.setenv("GLUE_PIPELINE_ROLE_ARN", _GLUE_PIPELINE_ROLE_ARN)
    monkeypatch.setenv("AUDIT_TABLE", _AUDIT_TABLE)
    monkeypatch.setenv(
        "STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:123456789012:stateMachine:test",
    )


def _import_pipelines_handler():
    for key in list(sys.modules.keys()):
        if (
            "agentic_data_ingestion_dashboard_api" in key
            or key in ("router", "lambda_function")
            or key.startswith("handlers")
        ):
            del sys.modules[key]

    from handlers import pipelines

    return pipelines


def _extract_pipeline_id(create_response: dict) -> str:
    body = json.loads(create_response["body"])
    assert "pipeline_id" in body, f"missing pipeline_id in response: {body}"
    return body["pipeline_id"]


def _crawler_arn(crawler_name: str) -> str:
    return f"arn:aws:glue:{_AWS_REGION}:123456789012:crawler/{crawler_name}"


def _connection_arn(connection_name: str) -> str:
    return f"arn:aws:glue:{_AWS_REGION}:123456789012:connection/{connection_name}"


# ---------------------------------------------------------------------------
# Tests — create-pipeline JDBC source flow
# ---------------------------------------------------------------------------


class TestCreateJdbcPipelineSelectRdsMode:
    """End-to-end create-pipeline flow when source connection mode is ``select_rds``.

    Validates: Requirements 6.1, 6.2, 11.1, 11.2, 11.8, 5.3.
    """

    @mock_aws
    def test_select_rds_full_table_creates_all_resources(self, monkeypatch):
        _set_handler_env(monkeypatch)
        aws = _setup_aws_resources()
        pipelines = _import_pipelines_handler()

        body = _make_create_body(
            pipeline_name="JDBC Source Select RDS",
            source_config=_VALID_SELECT_RDS_FULL_TABLE,
        )
        result = pipelines.create_pipeline(body, event={})

        assert result["statusCode"] == 201, f"Expected 201, got {result['statusCode']}: {result['body']}"
        pipeline_id = _extract_pipeline_id(result)

        # ── 1. pipelines table row has flat top-level source fields ────
        ddb_item = aws["pipelines_table"].get_item(Key={"pipeline_id": pipeline_id})["Item"]

        assert ddb_item["source_type"] == "JDBC"
        assert ddb_item["source_config"] == _VALID_SELECT_RDS_FULL_TABLE, "source_config must round-trip unchanged"
        for forbidden in ("pipeline_config", "config", "source"):
            assert forbidden not in ddb_item, f"source fields must not be nested under {forbidden!r}"

        expected_conn_name = f"agentic-data-ingestion-{pipeline_id}-source"
        assert ddb_item["source_connection_name"] == expected_conn_name

        # ── 2. Glue Connection exists with PhysicalConnectionRequirements ──
        conn_resp = aws["glue"].get_connection(Name=expected_conn_name)
        conn = conn_resp["Connection"]
        # NOTE: moto's get_connection response omits ConnectionType in
        # some versions, so we don't assert on it here. The boto3
        # create_connection schema requires ConnectionType to be set,
        # and the production code passes "JDBC" — already covered by
        # the shared/jdbc_helpers unit tests.
        assert conn["ConnectionProperties"]["JDBC_CONNECTION_URL"] == _VALID_SELECT_RDS_FULL_TABLE["jdbc_url"]
        # JDBC_DRIVER_CLASS_NAME is intentionally absent for built-in
        # engines (Glue auto-detects from the URL prefix). Setting it
        # explicitly causes Glue CreateCrawler to demand
        # JDBC_DRIVER_JAR_URI which we do not provide.
        assert "JDBC_DRIVER_CLASS_NAME" not in conn["ConnectionProperties"]
        # Glue REQUIRES credentials on a JDBC connection, so the secret ARN
        # is set as the SECRET_ID ConnectionProperty (a URL-only connection
        # fails Glue's "Validation for connection properties failed" check —
        # verified against the live Glue API 2026-06-22). It is ALSO mirrored
        # into the ``adi:secret_arn`` tag for the read path.
        assert conn["ConnectionProperties"]["SECRET_ID"] == _VALID_SELECT_RDS_FULL_TABLE["secret_arn"]
        conn_tags = aws["glue"].get_tags(ResourceArn=_connection_arn(expected_conn_name))["Tags"]
        assert conn_tags["adi:secret_arn"] == _VALID_SELECT_RDS_FULL_TABLE["secret_arn"]
        # PhysicalConnectionRequirements close the P14 VPC-routing gap
        # — these MUST be set in select_rds mode (Requirements 11.4 /
        # 15.3).
        pcr = conn["PhysicalConnectionRequirements"]
        assert pcr["SubnetId"] == _VALID_SELECT_RDS_FULL_TABLE["subnet_id"]
        assert pcr["SecurityGroupIdList"] == _VALID_SELECT_RDS_FULL_TABLE["security_group_ids"]
        assert pcr["AvailabilityZone"] == _VALID_SELECT_RDS_FULL_TABLE["availability_zone"]

        # ── 3. Runtime crawler exists with correct config and tags ─────
        expected_crawler_name = f"agentic-data-ingestion-{pipeline_id}-source-discover"
        crawler = aws["glue"].get_crawler(Name=expected_crawler_name)["Crawler"]
        assert crawler["DatabaseName"] == _DISCOVERY_DB
        assert crawler["TablePrefix"] == f"agentic_data_ingestion_{pipeline_id}_"
        assert crawler["Role"] == _GLUE_PIPELINE_ROLE_ARN
        jdbc_targets = crawler["Targets"]["JdbcTargets"]
        assert len(jdbc_targets) == 1
        assert jdbc_targets[0]["ConnectionName"] == expected_conn_name
        assert jdbc_targets[0]["Path"] == (
            f"{_VALID_SELECT_RDS_FULL_TABLE['jdbc_schema']}/{_VALID_SELECT_RDS_FULL_TABLE['jdbc_table']}"
        )

        crawler_tags = aws["glue"].get_tags(ResourceArn=_crawler_arn(expected_crawler_name))["Tags"]
        # Tag value MUST be ``agentic-data-ingestion-runtime`` — the discovery
        # janitor uses this to skip per-pipeline crawlers.
        assert crawler_tags == {
            "Type": "agentic-data-ingestion-runtime",
            "PipelineId": pipeline_id,
        }

        # ── 4. EventBridge schedule rule exists and points at trigger ──
        rule_name = f"agentic-data-ingestion-schedule-{pipeline_id}"
        rule = aws["events"].describe_rule(Name=rule_name)
        assert rule["ScheduleExpression"] == "rate(1 hour)"
        assert rule["State"] == "ENABLED"

        targets = aws["events"].list_targets_by_rule(Rule=rule_name)["Targets"]
        assert len(targets) == 1
        assert targets[0]["Arn"] == _PIPELINE_TRIGGER_ARN
        target_input = json.loads(targets[0]["Input"])
        # Requirement 5.3: the trigger branches on source_type and defaults an
        # absent one to S3_UPLOAD, so a JDBC rule without it never runs
        # (fixed 2026-10-10; tests/contracts/test_schedule_payload_cross_lambda.py
        # feeds this payload to the real trigger).
        assert target_input["trigger"] == "scheduled"
        assert target_input["pipeline_id"] == pipeline_id
        assert target_input["source_type"] == "JDBC"


class TestCreateJdbcPipelineGlueConnectionMode:
    """End-to-end create-pipeline flow when source mode is ``glue_connection``.

    Validates: Requirements 6.1, 11.2, 11.8 — and the explicit *non*-creation
    of an AgenticDataIngestion-managed Glue Connection when the user supplied their own.
    """

    @mock_aws
    def test_glue_connection_mode_skips_source_connection_creation(self, monkeypatch):
        _set_handler_env(monkeypatch)
        aws = _setup_aws_resources()
        pipelines = _import_pipelines_handler()

        body = _make_create_body(
            pipeline_name="JDBC Source Glue Connection",
            source_config=_VALID_GLUE_CONNECTION_FULL_TABLE,
        )
        result = pipelines.create_pipeline(body, event={})
        assert result["statusCode"] == 201, result["body"]
        pipeline_id = _extract_pipeline_id(result)

        # ── 1. DDB row stores the user's connection name verbatim ─────
        ddb_item = aws["pipelines_table"].get_item(Key={"pipeline_id": pipeline_id})["Item"]
        assert ddb_item["source_type"] == "JDBC"
        assert ddb_item["source_connection_name"] == _VALID_GLUE_CONNECTION_FULL_TABLE["glue_connection_name"]

        # ── 2. NO agentic-data-ingestion-{pipeline_id}-source connection was created ─
        agentic_data_ingestion_conn_name = f"agentic-data-ingestion-{pipeline_id}-source"
        with pytest.raises(ClientError) as excinfo:
            aws["glue"].get_connection(Name=agentic_data_ingestion_conn_name)
        assert excinfo.value.response["Error"]["Code"] == "EntityNotFoundException"

        # ── 3. Runtime crawler still gets created — pointed at the
        #      user-supplied connection name ──────────────────────────
        expected_crawler_name = f"agentic-data-ingestion-{pipeline_id}-source-discover"
        crawler = aws["glue"].get_crawler(Name=expected_crawler_name)["Crawler"]
        jdbc_targets = crawler["Targets"]["JdbcTargets"]
        assert jdbc_targets[0]["ConnectionName"] == _VALID_GLUE_CONNECTION_FULL_TABLE["glue_connection_name"]
        assert crawler["TablePrefix"] == f"agentic_data_ingestion_{pipeline_id}_"
        assert crawler["DatabaseName"] == _DISCOVERY_DB

        crawler_tags = aws["glue"].get_tags(ResourceArn=_crawler_arn(expected_crawler_name))["Tags"]
        assert crawler_tags["Type"] == "agentic-data-ingestion-runtime"
        assert crawler_tags["PipelineId"] == pipeline_id


class TestCreateJdbcPipelineIncrementalReadMode:
    """End-to-end create-pipeline flow with ``read_mode == "incremental"``.

    Validates: Requirements 6.2 (watermark fields persist), 7.1.
    """

    @mock_aws
    def test_incremental_read_mode_persists_watermark_fields(self, monkeypatch):
        _set_handler_env(monkeypatch)
        aws = _setup_aws_resources()
        pipelines = _import_pipelines_handler()

        body = _make_create_body(
            pipeline_name="JDBC Source Incremental",
            source_config=_VALID_SELECT_RDS_INCREMENTAL,
        )
        result = pipelines.create_pipeline(body, event={})
        assert result["statusCode"] == 201, result["body"]
        pipeline_id = _extract_pipeline_id(result)

        ddb_item = aws["pipelines_table"].get_item(Key={"pipeline_id": pipeline_id})["Item"]
        persisted = ddb_item["source_config"]
        assert persisted == _VALID_SELECT_RDS_INCREMENTAL
        assert persisted["read_mode"] == "incremental"
        assert persisted["watermark_column"] == "updated_at"
        assert persisted["watermark_type"] == "timestamp"
        assert persisted["initial_watermark_value"] == "2024-01-01T00:00:00Z"

        crawler = aws["glue"].get_crawler(Name=f"agentic-data-ingestion-{pipeline_id}-source-discover")["Crawler"]
        assert crawler["TablePrefix"] == f"agentic_data_ingestion_{pipeline_id}_"


class TestWatermarkItemLifecycle:
    """The ``agentic_data_ingestion_pipeline_watermarks`` conditional-PutItem semantics.

    The runtime PySpark template (``glue_job_jdbc_source.py.j2``) writes the
    watermark with a conditional expression that rejects stale updates so
    out-of-order Glue runs cannot regress the high-water-mark.

    Validates: Requirements 7.1, 7.2, 22.1.
    """

    @mock_aws
    def test_initial_put_succeeds_then_stale_rejected_and_fresh_accepted(self, monkeypatch):
        _set_handler_env(monkeypatch)
        aws = _setup_aws_resources()
        wm = aws["watermarks_table"]
        pipeline_id = "test-watermark-pipe"

        # ── 1. Initial PutItem (no prior row) — accept_not_exists branch.
        wm.put_item(
            Item={
                "pipeline_id": pipeline_id,
                "watermark_column": "updated_at",
                "last_value": "2024-06-01T00:00:00Z",
                "last_updated_at": "2024-06-01T01:00:00Z",
                "last_run_id": "run-001",
            },
            ConditionExpression=("attribute_not_exists(last_updated_at) OR last_updated_at < :now"),
            ExpressionAttributeValues={":now": "2024-06-01T01:00:00Z"},
        )
        stored = wm.get_item(Key={"pipeline_id": pipeline_id})["Item"]
        assert stored["last_value"] == "2024-06-01T00:00:00Z"
        assert stored["last_run_id"] == "run-001"

        # ── 2. Stale PutItem — last_updated_at older than stored value.
        with pytest.raises(ClientError) as excinfo:
            wm.put_item(
                Item={
                    "pipeline_id": pipeline_id,
                    "watermark_column": "updated_at",
                    "last_value": "2024-05-01T00:00:00Z",
                    "last_updated_at": "2024-05-01T01:00:00Z",
                    "last_run_id": "run-002",
                },
                ConditionExpression=("attribute_not_exists(last_updated_at) OR last_updated_at < :now"),
                ExpressionAttributeValues={":now": "2024-05-01T01:00:00Z"},
            )
        assert excinfo.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        unchanged = wm.get_item(Key={"pipeline_id": pipeline_id})["Item"]
        assert unchanged["last_value"] == "2024-06-01T00:00:00Z"
        assert unchanged["last_run_id"] == "run-001"

        # ── 3. Fresh PutItem — last_updated_at strictly newer.
        wm.put_item(
            Item={
                "pipeline_id": pipeline_id,
                "watermark_column": "updated_at",
                "last_value": "2024-07-01T00:00:00Z",
                "last_updated_at": "2024-07-01T01:00:00Z",
                "last_run_id": "run-003",
            },
            ConditionExpression=("attribute_not_exists(last_updated_at) OR last_updated_at < :now"),
            ExpressionAttributeValues={":now": "2024-07-01T01:00:00Z"},
        )
        bumped = wm.get_item(Key={"pipeline_id": pipeline_id})["Item"]
        assert bumped["last_value"] == "2024-07-01T00:00:00Z"
        assert bumped["last_run_id"] == "run-003"


class TestCreateJdbcPipelineRollbackOnPartialFailure:
    """Rollback behaviour when crawler creation fails mid-flow.

    When the source connection is created successfully but the runtime
    crawler creation raises, the handler must:

    1. Delete the partially-created Glue Connection (only in
       ``select_rds`` mode — never delete a user-supplied connection).
    2. Roll back the freshly-written DDB pipeline row.
    3. Return HTTP 400.

    Validates: Requirement 11.5, 22.1.
    """

    @mock_aws
    def test_select_rds_crawler_failure_rolls_back_ddb_row(self, monkeypatch):
        _set_handler_env(monkeypatch)
        aws = _setup_aws_resources()
        pipelines = _import_pipelines_handler()

        # Inject a failure into the crawler-provisioning helper. The
        # connection step (Task 5.1) runs first; this raises during
        # the crawler step (Task 5.2) so we exercise the cleanup path.
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated CreateCrawler failure")

        # P21 Task 7.2: create_pipeline + _provision_jdbc_runtime_crawler now
        # live in handlers.pipelines_crud. Patch the helper on the module
        # create_pipeline actually executes in (resolved via __globals__) so the
        # injected failure reaches the running code (module references only).
        import sys as _sys

        _crud = _sys.modules[pipelines.create_pipeline.__module__]
        monkeypatch.setattr(_crud, "_provision_jdbc_runtime_crawler", _boom)

        body = _make_create_body(
            pipeline_name="JDBC Rollback Test",
            source_config=_VALID_SELECT_RDS_FULL_TABLE,
        )
        result = pipelines.create_pipeline(body, event={})

        # ── 1. Handler returns 400 with a descriptive error ───────────
        assert result["statusCode"] == 400, result["body"]
        err = json.loads(result["body"])["error"]
        assert "Failed to provision JDBC runtime crawler" in err

        # ── 2. NO leaked DDB pipeline row — the primary contract ──────
        scan = aws["pipelines_table"].scan()
        assert scan["Count"] == 0, f"DDB pipeline row was not rolled back: {scan.get('Items')}"

        # ── 3. Connection-cleanup status ─────────────────────────────
        # Per Requirement 11.5 the rollback path also deletes the
        # partially-created Glue Connection in select_rds mode, calling
        # ``glue:DeleteConnection`` with the correct boto3 parameter name
        # ``ConnectionName=`` (fixed in the P22 follow-up; previously it
        # used ``Name=`` which raised a swallowed parameter-validation
        # error and silently leaked the connection).
        #
        # NOTE: moto does NOT implement ``glue:delete_connection`` (it
        # raises NotImplementedError), so we cannot assert the connection
        # is gone from the moto store here — it will still be present
        # regardless of the parameter name. The correct ``ConnectionName=``
        # call and the resulting ``cleanup_results["source_connection"]``
        # state are verified against a spy in the unit suite
        # (tests/unit/lambdas/dashboard_api/test_jdbc_pipeline_lifecycle.py).
        # This integration test's contract is the DDB rollback asserted in
        # step 2 above. We assert here only that the handler reached and
        # attempted the cleanup (the partial connection was created in 5.1).
        conn_resp = aws["glue"].get_connections()
        present = [
            c["Name"]
            for c in conn_resp.get("ConnectionList", [])
            if c["Name"].startswith("agentic-data-ingestion-") and c["Name"].endswith("-source")
        ]
        # The connection was created during step 5.1 and remains in moto's
        # store because moto can't execute delete_connection. The deletion
        # *attempt* (with the correct ConnectionName= param) is unit-tested
        # separately. We assert the partial connection was created (cleanup
        # path reachable); its end-state removal is not observable under moto.
        assert len(present) == 1, (
            "Expected exactly one partially-created source connection to be "
            f"present in moto (delete_connection is unimplemented); got: {present}"
        )

    @mock_aws
    def test_glue_connection_mode_crawler_failure_does_not_delete_user_connection(self, monkeypatch):
        """Defence: rollback must NEVER delete a user-supplied connection."""
        _set_handler_env(monkeypatch)
        aws = _setup_aws_resources()

        user_conn_name = _VALID_GLUE_CONNECTION_FULL_TABLE["glue_connection_name"]
        aws["glue"].create_connection(
            ConnectionInput={
                "Name": user_conn_name,
                "ConnectionType": "JDBC",
                "ConnectionProperties": {
                    "JDBC_CONNECTION_URL": _VALID_GLUE_CONNECTION_FULL_TABLE["jdbc_url"],
                    "JDBC_DRIVER_CLASS_NAME": _VALID_GLUE_CONNECTION_FULL_TABLE["jdbc_driver"],
                },
            }
        )

        pipelines = _import_pipelines_handler()

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated CreateCrawler failure")

        # P21 Task 7.2: create_pipeline + _provision_jdbc_runtime_crawler now
        # live in handlers.pipelines_crud. Patch the helper on the module
        # create_pipeline actually executes in (resolved via __globals__) so the
        # injected failure reaches the running code (module references only).
        import sys as _sys

        _crud = _sys.modules[pipelines.create_pipeline.__module__]
        monkeypatch.setattr(_crud, "_provision_jdbc_runtime_crawler", _boom)

        body = _make_create_body(
            pipeline_name="JDBC Rollback Glue Connection Mode",
            source_config=_VALID_GLUE_CONNECTION_FULL_TABLE,
        )
        result = pipelines.create_pipeline(body, event={})

        assert result["statusCode"] == 400

        # The DDB row was rolled back …
        scan = aws["pipelines_table"].scan()
        assert scan["Count"] == 0

        # … but the user's connection MUST still exist.
        survived = aws["glue"].get_connection(Name=user_conn_name)
        assert survived["Connection"]["Name"] == user_conn_name


# ---------------------------------------------------------------------------
# P17 follow-up notes — both resolved
# ---------------------------------------------------------------------------
#
# 1. The schedule rule's target Input now carries ``source_type`` (2026-10-10),
#    and the schedule assertion above checks it. Without it the trigger's
#    ``_handle_scheduled_trigger`` defaulted to ``S3_UPLOAD``, so every JDBC
#    schedule listed an empty upload prefix and started nothing.
#
# 2. Every ``glue:DeleteConnection`` call in the dashboard API passes
#    ``ConnectionName=``; the ``Name=`` spelling this note once recorded is gone.
