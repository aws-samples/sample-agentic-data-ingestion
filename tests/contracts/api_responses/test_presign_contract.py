# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: POST /api/upload/presign response contains `url`, `fields`, `key`.

Validates: AC-11.1

The frontend expects:
  PresignResponse {
    url: string;
    fields: Record<string, string>;
    key: string;
    confirm_url?: string | null;   // p64 hotfix — signed HEAD URL for the R4.4 check
  }

`confirm_url` is nullable by contract, not by accident: the server returns `None`
when it could not be signed, and the client must read that as *unconfirmed* rather
than as *confirmed*. See `handlers/upload.py::_confirmation_url` for why a guard
that cannot be built must not block an upload.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
mock_env_utils.get_aws_account_id = lambda: "123456789012"
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)


@pytest.fixture(autouse=True)
def _deterministic_confirmation_signer():
    """Sign `confirm_url` with fixed fake credentials, not the ambient chain.

    `_confirmation_url` calls a **real** botocore signer — deliberately, because a
    mocked signer cannot be unauthorized, and mocking the signature is precisely
    how task 1d.5's suite stayed green while its request could only ever 403. But
    left unpatched, the signer resolves credentials from the default chain, so this
    module's result would depend on the developer's `~/.aws` config and could reach
    for IMDS on a machine with none. The credentials are stubbed; the **signing is
    not**, which is the half that carries the contract.

    No network call either way: `generate_presigned_url` is a local computation.
    """
    import boto3
    from botocore.config import Config as BotoConfig
    from handlers import upload

    signer = boto3.client(
        "s3",
        region_name="us-west-2",
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    with patch.object(upload, "_confirm_s3_client", signer):
        yield


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_response_contains_url_fields_key(mock_account, mock_s3):
    """POST /api/upload/presign response matches PresignResponse interface."""
    mock_s3.generate_presigned_post.return_value = {
        "url": "https://adi-source-files-123456789012-us-west-2.s3.amazonaws.com",
        "fields": {
            "key": "test-pipeline/data.csv",
            "AWSAccessKeyId": "AKIAIOSFODNN7EXAMPLE",
            "policy": "base64-encoded-policy",
            "signature": "signature-value",
        },
    }

    from router import route_request

    event = {
        "httpMethod": "POST",
        "path": "/api/upload/presign",
        "body": json.dumps(
            {
                "pipeline_id": "test-pipeline",
                "file_name": "data.csv",
            }
        ),
        "queryStringParameters": None,
    }

    response = route_request(event)
    assert response["statusCode"] == 200

    body = json.loads(response["body"])

    # Contract: must have url (string), fields (dict), key (string)
    assert "url" in body, 'Response missing "url"'
    assert isinstance(body["url"], str), '"url" must be a string'

    assert "fields" in body, 'Response missing "fields"'
    assert isinstance(body["fields"], dict), '"fields" must be a dict'

    assert "key" in body, 'Response missing "key"'
    assert isinstance(body["key"], str), '"key" must be a string'

    # p64 hotfix — the field must be PRESENT even when null, so a client can tell
    # "this deployment does not sign confirmations" from "this response is
    # malformed". The type is the contract; nullability is asserted here and the
    # signed shape by `test_presign_confirm_url_when_present_is_signed` below.
    assert "confirm_url" in body, 'Response missing "confirm_url"'
    assert body["confirm_url"] is None or isinstance(body["confirm_url"], str), '"confirm_url" must be a string or null'


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_response_key_has_pipeline_prefix(mock_account, mock_s3):
    """The key in the response starts with the pipeline_id."""
    mock_s3.generate_presigned_post.return_value = {
        "url": "https://example.s3.amazonaws.com",
        "fields": {"key": "my-pipe/report.json"},
    }

    from router import route_request

    event = {
        "httpMethod": "POST",
        "path": "/api/upload/presign",
        "body": json.dumps(
            {
                "pipeline_id": "my-pipe",
                "file_name": "report.json",
            }
        ),
        "queryStringParameters": None,
    }

    response = route_request(event)
    body = json.loads(response["body"])

    assert body["key"].startswith("my-pipe/")


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_response_has_cors_headers(mock_account, mock_s3):
    """Response includes CORS headers for browser-based uploads."""
    mock_s3.generate_presigned_post.return_value = {
        "url": "https://example.s3.amazonaws.com",
        "fields": {"key": "p/f.csv"},
    }

    from router import route_request

    event = {
        "httpMethod": "POST",
        "path": "/api/upload/presign",
        "body": json.dumps(
            {
                "pipeline_id": "p",
                "file_name": "f.csv",
            }
        ),
        "queryStringParameters": None,
    }

    response = route_request(event)
    assert response["headers"]["Content-Type"] == "application/json"
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"


def test_presign_missing_body_returns_400():
    """POST /api/upload/presign with missing fields returns 400."""
    from router import route_request

    event = {
        "httpMethod": "POST",
        "path": "/api/upload/presign",
        "body": json.dumps({}),
        "queryStringParameters": None,
    }

    response = route_request(event)
    assert response["statusCode"] == 400


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_confirm_url_when_present_is_signed(mock_account, mock_s3):
    """If `confirm_url` is a string, it MUST carry a signature.

    The contract a client depends on is not "a URL is present" but "the URL is
    usable". An unsigned confirmation URL is worse than a null one: null routes
    the client to its honest "not verified" warning, whereas an unsigned URL
    routes it to a 403 and an error message about an upload that succeeded. That
    is exactly what task 1d.5 shipped.
    """
    from router import route_request

    mock_s3.generate_presigned_post.return_value = {
        "url": "https://example.s3.amazonaws.com",
        "fields": {"key": "p/f.csv"},
    }

    event = {
        "httpMethod": "POST",
        "path": "/api/upload/presign",
        "body": json.dumps({"pipeline_id": "p", "file_name": "f.csv"}),
        "queryStringParameters": None,
    }

    confirm_url = json.loads(route_request(event)["body"])["confirm_url"]

    # The autouse fixture supplies a deterministic signer, so this is never None
    # here — a None would mean `_confirmation_url` swallowed a real failure.
    assert confirm_url is not None, "signer was available but no confirm_url was minted"

    params = parse_qs(urlparse(confirm_url).query)
    signed = "X-Amz-Signature" in params or "Signature" in params
    assert signed, f"confirm_url present but unsigned — an anonymous HEAD 403s: {confirm_url}"


# ---------------------------------------------------------------------------
# S5 — the refusal shapes: 404 for an unknown pipeline, 400 for a key that
# would leave the pipeline's prefix
# ---------------------------------------------------------------------------
# The handler now reads the pipelines table before signing, so the tests above
# need their pipelines to exist. The seeding fixture is autouse and defined here,
# at the end, because ASH suppresses the documentation-example secret in
# `_deterministic_confirmation_signer` by LINE (`.ash/.ash.yaml`); an insertion
# above it would un-suppress it. pytest applies an autouse fixture to the whole
# module wherever it is defined.
#
# The refusal contract is `{"error": string}` and nothing a client could upload
# with. `FileUploadModal` shows a failed presign through `ApiError`, which reads
# `body.error` (`dashboard-react/src/services/api-client.ts`), so a refusal
# without that string would reach the user as a bare "API error 404".

_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
_SEEDED_PIPELINE_IDS = ("test-pipeline", "my-pipe", "p")


@pytest.fixture(autouse=True)
def _seeded_pipelines_table(mock_dynamodb_tables, monkeypatch):
    """Point the handler at the shared moto pipelines table, seeded."""
    from handlers import upload

    table = mock_dynamodb_tables[_PIPELINES_TABLE]
    for pipeline_id in _SEEDED_PIPELINE_IDS:
        table.put_item(Item={"pipeline_id": pipeline_id, "pipeline_name": pipeline_id, "status": "active"})
    monkeypatch.setattr(upload, "pipelines_table", table)
    return table


def _route_presign(pipeline_id, file_name):
    from router import route_request

    return route_request(
        {
            "httpMethod": "POST",
            "path": "/api/upload/presign",
            "body": json.dumps({"pipeline_id": pipeline_id, "file_name": file_name}),
            "queryStringParameters": None,
        }
    )


def _assert_refusal_shape(response, status):
    assert response["statusCode"] == status
    assert response["headers"]["Content-Type"] == "application/json"
    body = json.loads(response["body"])
    assert isinstance(body.get("error"), str) and body["error"], '"error" must be a non-empty string'
    for field in ("url", "fields", "key", "confirm_url"):
        assert field not in body, f'a refusal must not carry "{field}"'
    return body


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_unknown_pipeline_returns_404_error_shape(mock_account, mock_s3):
    """POST /api/upload/presign for a pipeline that does not exist: 404 `{"error"}`."""
    body = _assert_refusal_shape(_route_presign("no-such-pipeline", "data.csv"), 404)

    assert body["error"] == "Pipeline not found: no-such-pipeline"
    mock_s3.generate_presigned_post.assert_not_called()


@pytest.mark.parametrize("file_name", ["../x.csv", "a/b.csv", "a\\b.csv", "x\nY.csv", ".", ".."])
@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_refused_file_name_returns_400_error_shape(mock_account, mock_s3, file_name):
    """POST /api/upload/presign with a name that leaves the prefix: 400 `{"error"}`."""
    body = _assert_refusal_shape(_route_presign("p", file_name), 400)

    assert "file_name" in body["error"]
    mock_s3.generate_presigned_post.assert_not_called()


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_accepted_file_name_keeps_the_success_shape(mock_account, mock_s3):
    """A real browser name still gets the full PresignResponse, key unchanged."""
    mock_s3.generate_presigned_post.return_value = {"url": "https://example.s3.amazonaws.com", "fields": {}}

    response = _route_presign("p", "Q3 report (final).csv")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["key"] == "p/Q3 report (final).csv"
    assert isinstance(body["url"], str) and isinstance(body["fields"], dict)
    assert "confirm_url" in body
