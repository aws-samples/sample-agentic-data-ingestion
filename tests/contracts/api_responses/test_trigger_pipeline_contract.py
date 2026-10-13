# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: POST /api/trigger-pipeline, through the router.

Success keeps its shape: `success`, `message`, `execution_arn`, `execution_name`.
Each refusal is `{"error": string}` and carries none of the success fields, the
same refusal contract `test_presign_contract.py` holds for the presign route,
because a client reads `body.error` (`dashboard-react/src/services/api-client.ts`).
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)

ACCOUNT = "123456789012"
REGION = "us-west-2"
UPLOAD_BUCKET = f"adi-source-files-{ACCOUNT}-{REGION}"

_SUCCESS_FIELDS = ("success", "message", "execution_arn", "execution_name")


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    stub = MagicMock()
    stub.require_env = lambda name, default=None: default or name
    stub.get_aws_account_id = lambda: ACCOUNT
    monkeypatch.setitem(sys.modules, "env_utils", stub)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("UPLOAD_BUCKET", UPLOAD_BUCKET)


@pytest.fixture(autouse=True)
def sfn(_environment, mock_dynamodb_tables, monkeypatch):
    from handlers import upload

    table = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
    table.put_item(Item={"pipeline_id": "p", "pipeline_name": "p", "status": "active", "source_type": "S3_UPLOAD"})
    table.put_item(Item={"pipeline_id": "p-archived", "pipeline_name": "p-archived", "status": "archived"})
    monkeypatch.setattr(upload, "pipelines_table", table)
    monkeypatch.setattr(upload, "get_aws_account_id", lambda: ACCOUNT)

    client = MagicMock()
    client.start_execution.return_value = {"executionArn": f"arn:aws:states:{REGION}:{ACCOUNT}:execution:x:y"}
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = client
    monkeypatch.setattr(upload, "boto3", fake_boto3)
    return client


def _route(pipeline_key, s3_uri):
    from router import route_request

    return route_request(
        {
            "httpMethod": "POST",
            "path": "/api/trigger-pipeline",
            "body": json.dumps({"pipeline_key": pipeline_key, "s3_uri": s3_uri}),
            "queryStringParameters": None,
        }
    )


def _assert_refusal_shape(response, status):
    assert response["statusCode"] == status
    assert response["headers"]["Content-Type"] == "application/json"
    body = json.loads(response["body"])
    assert isinstance(body.get("error"), str) and body["error"], '"error" must be a non-empty string'
    for field in _SUCCESS_FIELDS:
        assert field not in body, f'a refusal must not carry "{field}"'
    return body


def test_an_accepted_trigger_keeps_the_success_shape(sfn):
    response = _route("p", f"s3://{UPLOAD_BUCKET}/p/data.csv")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["success"] is True
    assert isinstance(body["message"], str)
    assert body["execution_arn"].startswith("arn:aws:states:")
    assert body["execution_name"].startswith("upload-p-")
    sfn.start_execution.assert_called_once()


@pytest.mark.parametrize(
    ("pipeline_key", "s3_uri", "status"),
    [
        pytest.param("no-such", f"s3://{UPLOAD_BUCKET}/no-such/data.csv", 404, id="unknown-pipeline"),
        pytest.param("p-archived", f"s3://{UPLOAD_BUCKET}/p-archived/data.csv", 409, id="archived-pipeline"),
        pytest.param("p", "s3://other-bucket/p/data.csv", 400, id="another-bucket"),
        pytest.param("p", f"s3://{UPLOAD_BUCKET}/p/../x.csv", 400, id="outside-the-prefix"),
    ],
)
def test_a_refused_trigger_returns_the_error_shape_and_starts_nothing(sfn, pipeline_key, s3_uri, status):
    _assert_refusal_shape(_route(pipeline_key, s3_uri), status)
    sfn.start_execution.assert_not_called()
