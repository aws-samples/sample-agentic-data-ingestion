# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: POST /api/pipelines returns 201 with { pipeline_id, status } (Task 5.10).

Validates: AC-10.6
Ensures the create_pipeline response shape is unchanged after merging
the provisioning Lambda into the dashboard API.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "adi-source-files-123456789012-us-west-2",
}.get(name, default or name)
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ["UPLOAD_BUCKET"] = "adi-source-files-123456789012-us-west-2"

# Add handler package to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)

from handlers.pipelines import create_pipeline

VALID_BODY = json.dumps(
    {
        "pipeline_name": "Contract Test Pipeline",
        "target_schema": "uploaded_test_schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "description": "Contract test",
    }
)


@patch("handlers.pipelines_crud.s3_client")
@patch("handlers.pipelines_crud.pipelines_table")
@patch("handlers.pipelines_crud.boto3")
def test_create_pipeline_returns_201_with_pipeline_id_and_status(mock_boto3, mock_table, mock_s3):
    """POST /api/pipelines must return 201 with pipeline_id and status='created'."""
    # Mock S3 client for schema loading (will fail gracefully)
    mock_s3_client = MagicMock()
    mock_s3_client.get_object.side_effect = Exception("skip")
    mock_sts = MagicMock()
    mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
    mock_boto3.client.side_effect = lambda svc, **kw: mock_sts if svc == "sts" else mock_s3_client

    # Mock the s3_client for provisioning (EventBridge already enabled)
    mock_s3.get_bucket_notification_configuration.return_value = {"EventBridgeConfiguration": {}}

    response = create_pipeline(VALID_BODY)

    # Contract: status code is 201
    assert response["statusCode"] == 201

    body = json.loads(response["body"])

    # Contract: response contains pipeline_id (non-empty string)
    assert "pipeline_id" in body
    assert isinstance(body["pipeline_id"], str)
    assert len(body["pipeline_id"]) > 0

    # Contract: response contains status='created'
    assert body["status"] == "created"

    # Contract: response contains message
    assert "message" in body


def test_create_pipeline_returns_400_without_pipeline_name():
    """POST /api/pipelines without pipeline_name returns 400."""
    body = json.dumps({"target_schema": "test", "ai_model": "test"})
    response = create_pipeline(body)
    assert response["statusCode"] == 400


def test_create_pipeline_returns_400_without_target_schema():
    """POST /api/pipelines without target_schema returns 400."""
    body = json.dumps({"pipeline_name": "test", "ai_model": "test"})
    response = create_pipeline(body)
    assert response["statusCode"] == 400


def test_create_pipeline_returns_400_without_ai_model():
    """POST /api/pipelines without ai_model returns 400."""
    body = json.dumps({"pipeline_name": "test", "target_schema": "test"})
    response = create_pipeline(body)
    assert response["statusCode"] == 400


def test_create_pipeline_returns_400_on_invalid_json():
    """POST /api/pipelines with invalid JSON returns 400."""
    response = create_pipeline("not-json{{{")
    assert response["statusCode"] == 400
