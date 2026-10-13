# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test: S3 event -> SQS -> Pipeline_Trigger -> SFN start_execution.

Validates: Requirement 6.1
- Simulates an S3 Object Created event arriving via SQS
- Invokes the Pipeline_Trigger Lambda handler
- Verifies SFN start_execution is called with correct input (source_bucket, source_key)
- Verifies the execution input contains the expected pipeline_id derivation

Uses importlib to load the Lambda handler with boto3 mocked via moto.
"""

import importlib
import importlib.util
import json
import os
import sys
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


def _create_sfn_and_table(region: str):
    """Create moto SFN state machine and executions DynamoDB table. Returns SM ARN."""
    sfn = boto3.client("stepfunctions", region_name=region)
    iam = boto3.client("iam", region_name=region)
    role_resp = iam.create_role(
        RoleName="test-sfn-role",
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
        name="test-state-machine",
        definition=json.dumps({"StartAt": "Pass", "States": {"Pass": {"Type": "Pass", "End": True}}}),
        roleArn=role_resp["Role"]["Arn"],
    )
    dynamodb = boto3.resource("dynamodb", region_name=region)
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
    # Pipeline locks table — the trigger handler acquires a per-pipeline lock
    # via a conditional put_item before starting an execution. Without this
    # table the handler raises ResourceNotFoundException.
    dynamodb.create_table(
        TableName="agentic_data_ingestion_pipeline_locks",
        KeySchema=[{"AttributeName": "pipeline_prefix", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pipeline_prefix", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return sm_resp["stateMachineArn"]


class TestTriggerToSFN:
    """Simulate S3 event -> SQS -> Pipeline_Trigger -> verify SFN start_execution."""

    @mock_aws
    def test_s3_event_triggers_sfn_execution(self, mock_lambda_env):
        """Pipeline_Trigger should call SFN start_execution with correct input."""
        region = "us-east-1"
        sm_arn = _create_sfn_and_table(region)

        env = {
            "STATE_MACHINE_ARN": sm_arn,
            "MAX_CONCURRENT_EXECUTIONS": "10",
            "STATE_TABLE": "agentic_data_ingestion_state",
            "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
            "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_trigger_handler(env)

        source_bucket = "my-source-bucket"
        source_key = "my-pipeline/data.csv"
        sqs_event = _build_sqs_event(source_bucket, source_key)

        result = module.lambda_handler(sqs_event, MagicMock())
        assert result["statusCode"] == 200

        sfn = boto3.client("stepfunctions", region_name=region)
        executions = sfn.list_executions(stateMachineArn=sm_arn, statusFilter="RUNNING")["executions"]
        assert len(executions) == 1, f"Expected 1 execution, got {len(executions)}"

        exec_desc = sfn.describe_execution(executionArn=executions[0]["executionArn"])
        exec_input = json.loads(exec_desc["input"])
        assert exec_input["source_bucket"] == source_bucket
        assert exec_input["source_key"] == source_key
        assert exec_input["trigger"] == "s3_upload"

    @mock_aws
    def test_sfn_input_contains_pipeline_id_derivation(self, mock_lambda_env):
        """The SFN execution input should contain source_key for pipeline_id derivation."""
        region = "us-east-1"
        sm_arn = _create_sfn_and_table(region)

        env = {
            "STATE_MACHINE_ARN": sm_arn,
            "MAX_CONCURRENT_EXECUTIONS": "10",
            "STATE_TABLE": "agentic_data_ingestion_state",
            "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
            "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_trigger_handler(env)

        source_key = "customer-pipeline/uploads/report.csv"
        sqs_event = _build_sqs_event("test-bucket", source_key)

        result = module.lambda_handler(sqs_event, MagicMock())
        assert result["statusCode"] == 200

        sfn = boto3.client("stepfunctions", region_name=region)
        executions = sfn.list_executions(stateMachineArn=sm_arn, statusFilter="RUNNING")["executions"]
        exec_desc = sfn.describe_execution(executionArn=executions[0]["executionArn"])
        exec_input = json.loads(exec_desc["input"])

        assert exec_input["source_key"] == source_key
        assert exec_input["source_bucket"] == "test-bucket"
