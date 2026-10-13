# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E: deployed-infrastructure health checks against real AWS.

Conformance: Requirement 25 — brought under the shared E2E_Harness contracts.

Validates that the core AgenticDataIngestion resources are deployed, ACTIVE, and wired
the way the pipeline runtime expects. These are the **preconditions** every
other e2e test (and the live dashboard) depends on.

**Not mapped to a numbered E2E_TEST_PLAN test case** — this is a precondition
suite that verifies deployment health, not standalone functional coverage.

**Harness conformance:**
- Gated by Real_AWS_Gate (session autouse via ``require_real_aws``)
- Read-only: all assertions query deployed resource state without mutation
- No ``requires_trigger`` needed — does not start or mutate executions
- No Cleanup_Contract obligations (no artifacts created)
- Reuses E2E_Harness fixtures (``boto3_session``, ``resource_names``)

Run with: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true make test-e2e
"""

import pytest


class TestStateMachine:
    def test_state_machine_active(self, boto3_session, resource_names):
        """The pipeline Step Functions state machine exists and is ACTIVE."""
        sfn = boto3_session.client("stepfunctions")
        desc = sfn.describe_state_machine(stateMachineArn=resource_names["state_machine_arn"])
        assert desc["status"] == "ACTIVE"
        assert desc["type"] in ("STANDARD", "EXPRESS")


class TestBuckets:
    @pytest.mark.parametrize(
        "key",
        ["source_bucket", "processed_bucket", "generated_code_bucket", "target_schemas_bucket"],
    )
    def test_bucket_exists(self, boto3_session, resource_names, key):
        """Each core S3 bucket exists and is reachable."""
        s3 = boto3_session.client("s3")
        # head_bucket raises ClientError (404/403) if missing/inaccessible.
        s3.head_bucket(Bucket=resource_names[key])

    def test_source_bucket_eventbridge_enabled(self, boto3_session, resource_names):
        """The source bucket emits EventBridge notifications — this is what
        auto-triggers a pipeline on upload."""
        s3 = boto3_session.client("s3")
        cfg = s3.get_bucket_notification_configuration(Bucket=resource_names["source_bucket"])
        assert "EventBridgeConfiguration" in cfg, (
            "Source bucket must have EventBridge notifications enabled for the upload-triggered pipeline flow to work"
        )


class TestDynamoTables:
    @pytest.mark.parametrize(
        "table",
        ["agentic_data_ingestion_pipelines", "agentic_data_ingestion_executions", "agentic_data_ingestion_state"],
    )
    def test_table_active(self, boto3_session, table):
        """Core DynamoDB tables exist and are ACTIVE."""
        ddb = boto3_session.client("dynamodb")
        desc = ddb.describe_table(TableName=table)["Table"]
        assert desc["TableStatus"] == "ACTIVE"

    def test_state_table_has_pipeline_index(self, boto3_session):
        """The State table exposes the PipelineIndex GSI used for run lookups."""
        ddb = boto3_session.client("dynamodb")
        desc = ddb.describe_table(TableName="agentic_data_ingestion_state")["Table"]
        gsis = {g["IndexName"] for g in desc.get("GlobalSecondaryIndexes", [])}
        assert "PipelineIndex" in gsis


class TestLambdas:
    @pytest.mark.parametrize(
        "fn",
        [
            "agentic_data_ingestion_dashboard_api",
            "agentic_data_ingestion_pipeline_trigger",
            "agentic_data_ingestion_run_pipeline",
            "agentic_data_ingestion_schema_extract",
            "agentic_data_ingestion_create_code",
        ],
    )
    def test_lambda_active(self, boto3_session, fn):
        """Key Lambda functions are deployed and in the Active state."""
        lam = boto3_session.client("lambda")
        cfg = lam.get_function_configuration(FunctionName=fn)
        assert cfg["State"] == "Active", f"{fn} is {cfg['State']}"
