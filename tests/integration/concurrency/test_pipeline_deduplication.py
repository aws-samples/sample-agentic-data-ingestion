# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test: pipeline deduplication -- duplicate file events for the same pipeline.

Validates: Requirement 6.3, AC-1, AC-2, AC-3
- Uses DynamoDB lock table (agentic_data_ingestion_pipeline_locks) for per-pipeline dedup
- First invocation acquires lock and starts SFN execution
- Second invocation for same pipeline prefix fails (lock held)
- Different pipeline prefixes each acquire their own lock
- Lock items include TTL for safety-net expiration

Uses importlib to load the Lambda handler with boto3 mocked via moto.
"""

import importlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_pipeline_trigger"
_SHARED_DIR = _PROJECT_ROOT / "shared"


def _build_sqs_event(source_bucket: str, source_key: str) -> dict:
    """Build an SQS event wrapping an EventBridge S3 Object Created notification."""
    eb_event = {
        "version": "0",
        "id": "test-event-id",
        "detail-type": "Object Created",
        "source": "aws.s3",
        "account": "123456789012",
        "time": "2024-01-01T00:00:00Z",
        "region": "us-east-1",
        "detail": {
            "bucket": {"name": source_bucket},
            "object": {"key": source_key, "size": 1024},
        },
    }
    return {
        "Records": [
            {
                "messageId": "test-msg-id",
                "body": json.dumps(eb_event),
                "eventSource": "aws:sqs",
            }
        ]
    }


def _load_trigger_handler(env_vars: dict):
    """Load the pipeline_trigger lambda_function module with env vars set."""
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))

    if "aws_lambda_powertools" not in sys.modules:
        mock_powertools = MagicMock()
        mock_powertools.Logger.return_value = MagicMock()
        sys.modules["aws_lambda_powertools"] = mock_powertools

    for k, v in env_vars.items():
        os.environ[k] = v

    sys.modules.pop("lambda_function", None)

    spec = importlib.util.spec_from_file_location(
        "lambda_function",
        str(_LAMBDA_DIR / "lambda_function.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["lambda_function"] = module
    spec.loader.exec_module(module)
    return module


def _create_infrastructure(region: str):
    """Create moto SFN state machine, executions table, and pipeline locks table.

    Returns the state machine ARN.
    """
    sfn = boto3.client("stepfunctions", region_name=region)
    iam = boto3.client("iam", region_name=region)
    role_resp = iam.create_role(
        RoleName="test-sfn-role-dedup",
        AssumeRolePolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"}, "Action": "sts:AssumeRole"}
                ],
            }
        ),
        Path="/",
    )
    sm_resp = sfn.create_state_machine(
        name="test-sm-dedup",
        definition=json.dumps({"StartAt": "Pass", "States": {"Pass": {"Type": "Pass", "End": True}}}),
        roleArn=role_resp["Role"]["Arn"],
    )

    dynamodb = boto3.resource("dynamodb", region_name=region)

    # Executions table
    dynamodb.create_table(
        TableName="agentic_data_ingestion_executions",
        KeySchema=[
            {"AttributeName": "pipeline_id", "KeyType": "HASH"},
            {"AttributeName": "run_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "StatusIndex",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "started_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Pipeline locks table (DynamoDB lock-based dedup)
    dynamodb.create_table(
        TableName="agentic_data_ingestion_pipeline_locks",
        KeySchema=[
            {"AttributeName": "pipeline_prefix", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_prefix", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    return sm_resp["stateMachineArn"]


class TestPipelineDeduplication:
    """Submit duplicate file events for the same pipeline -> verify lock-based dedup."""

    @mock_aws
    def test_duplicate_event_raises_on_second_invocation(self, mock_lambda_env):
        """Second invocation for the same pipeline prefix should raise (lock held)."""
        region = "us-east-1"
        sm_arn = _create_infrastructure(region)

        env = {
            "STATE_MACHINE_ARN": sm_arn,
            "MAX_CONCURRENT_EXECUTIONS": "10",
            "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
            "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_trigger_handler(env)

        pipeline_prefix = "same-pipeline"
        event_1 = _build_sqs_event("bucket", f"{pipeline_prefix}/file1.csv")
        event_2 = _build_sqs_event("bucket", f"{pipeline_prefix}/file2.csv")

        context = MagicMock()

        # First invocation should succeed — lock acquired
        result = module.lambda_handler(event_1, context)
        assert result["statusCode"] == 200

        # Verify lock was written to the locks table
        dynamodb = boto3.resource("dynamodb", region_name=region)
        locks_table = dynamodb.Table("agentic_data_ingestion_pipeline_locks")
        lock_item = locks_table.get_item(Key={"pipeline_prefix": pipeline_prefix})
        assert "Item" in lock_item, "Lock should exist after first invocation"
        assert lock_item["Item"]["pipeline_prefix"] == pipeline_prefix

        # Second invocation is blocked by the held lock and returned to the queue
        # as a partial batch failure rather than raised (p30 R9.3). The dedup
        # guarantee below is unchanged — only how the block reaches SQS.
        result_2 = module.lambda_handler(event_2, context)
        assert result_2["batchItemFailures"] == [{"itemIdentifier": "test-msg-id"}]

        # Verify only one SFN execution was started
        sfn = boto3.client("stepfunctions", region_name=region)
        executions = sfn.list_executions(stateMachineArn=sm_arn, statusFilter="RUNNING")["executions"]
        assert len(executions) == 1, f"Expected exactly 1 execution, got {len(executions)}"

    @mock_aws
    def test_different_pipelines_both_succeed(self, mock_lambda_env):
        """Events for different pipeline prefixes should each acquire their own lock."""
        region = "us-east-1"
        sm_arn = _create_infrastructure(region)

        env = {
            "STATE_MACHINE_ARN": sm_arn,
            "MAX_CONCURRENT_EXECUTIONS": "10",
            "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
            "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_trigger_handler(env)

        event_a = _build_sqs_event("bucket", "pipeline-a/file.csv")
        event_b = _build_sqs_event("bucket", "pipeline-b/file.csv")

        context = MagicMock()

        result_a = module.lambda_handler(event_a, context)
        assert result_a["statusCode"] == 200

        result_b = module.lambda_handler(event_b, context)
        assert result_b["statusCode"] == 200

        # Verify both locks exist
        dynamodb = boto3.resource("dynamodb", region_name=region)
        locks_table = dynamodb.Table("agentic_data_ingestion_pipeline_locks")
        lock_a = locks_table.get_item(Key={"pipeline_prefix": "pipeline-a"})
        lock_b = locks_table.get_item(Key={"pipeline_prefix": "pipeline-b"})
        assert "Item" in lock_a, "Lock for pipeline-a should exist"
        assert "Item" in lock_b, "Lock for pipeline-b should exist"

        # Verify two SFN executions were started
        sfn = boto3.client("stepfunctions", region_name=region)
        executions = sfn.list_executions(stateMachineArn=sm_arn, statusFilter="RUNNING")["executions"]
        assert len(executions) == 2, f"Expected 2 executions for different pipelines, got {len(executions)}"

    @mock_aws
    def test_lock_item_has_ttl_field(self, mock_lambda_env):
        """Lock items should include a TTL field for safety-net expiration (AC-3)."""
        region = "us-east-1"
        sm_arn = _create_infrastructure(region)

        env = {
            "STATE_MACHINE_ARN": sm_arn,
            "MAX_CONCURRENT_EXECUTIONS": "10",
            "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
            "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_trigger_handler(env)

        event = _build_sqs_event("bucket", "my-pipeline/file.csv")
        module.lambda_handler(event, MagicMock())

        dynamodb = boto3.resource("dynamodb", region_name=region)
        locks_table = dynamodb.Table("agentic_data_ingestion_pipeline_locks")
        lock_item = locks_table.get_item(Key={"pipeline_prefix": "my-pipeline"})["Item"]

        assert "ttl" in lock_item, "Lock item should have a TTL field"
        # TTL is LOCK_TTL_SECONDS from now (11 days, above the state machine's
        # 10-day execution timeout; AC-3). Read off the loaded module, which is
        # the value the handler actually stamped.
        lock_ttl = module.LOCK_TTL_SECONDS
        now_epoch = int(time.time())
        ttl_value = int(lock_item["ttl"])
        assert ttl_value > now_epoch, "TTL should be in the future"
        assert ttl_value <= now_epoch + lock_ttl + 5, "TTL should be LOCK_TTL_SECONDS from now"
        assert ttl_value > now_epoch + lock_ttl - 60, "TTL should be LOCK_TTL_SECONDS from now, not a shorter window"
        assert lock_ttl > 10 * 24 * 60 * 60, "the lock must outlive the state machine's 10-day execution timeout"

    @mock_aws
    def test_lock_contains_execution_metadata(self, mock_lambda_env):
        """Lock items should contain execution_name and started_at for debugging."""
        region = "us-east-1"
        sm_arn = _create_infrastructure(region)

        env = {
            "STATE_MACHINE_ARN": sm_arn,
            "MAX_CONCURRENT_EXECUTIONS": "10",
            "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
            "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_trigger_handler(env)

        event = _build_sqs_event("bucket", "my-pipeline/file.csv")
        module.lambda_handler(event, MagicMock())

        dynamodb = boto3.resource("dynamodb", region_name=region)
        locks_table = dynamodb.Table("agentic_data_ingestion_pipeline_locks")
        lock_item = locks_table.get_item(Key={"pipeline_prefix": "my-pipeline"})["Item"]

        assert "execution_name" in lock_item, "Lock should contain execution_name"
        assert "started_at" in lock_item, "Lock should contain started_at"
        assert lock_item["execution_name"].startswith("file-"), "Execution name should follow naming convention"
