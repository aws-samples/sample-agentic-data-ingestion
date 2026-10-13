# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: POST /api/trigger-pipeline refuses before it starts a run.

`handlers.upload.trigger_pipeline` used to start a Step Functions execution for
any `pipeline_key` and any `s3_uri` (threat model T-13's residual). The
expectations here come from what an upload can produce, not from the handler's
output: an upload lands at `s3://<upload bucket>/<pipeline_id>/<file_name>`,
with a key `presign_upload` signed, for a pipeline that exists. So a run may
start only for an existing, unarchived S3-upload pipeline, on an object in the
upload bucket directly under that pipeline's prefix.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)

ACCOUNT = "123456789012"
REGION = "us-west-2"
UPLOAD_BUCKET = f"adi-source-files-{ACCOUNT}-{REGION}"

_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"

#: pipeline_id -> the record's other attributes. `p` has no `source_type`, the
#: shape of a record written before the discriminator existed.
_SEEDED = {
    "p": {"status": "active"},
    "p-upload": {"status": "active", "source_type": "S3_UPLOAD"},
    "p-archived": {"status": "archived", "source_type": "S3_UPLOAD"},
    "p-jdbc": {"status": "active", "source_type": "JDBC"},
}


@pytest.fixture(autouse=True)
def _env_utils_stub(monkeypatch):
    """Importing `handlers.upload` needs `env_utils`; installed per test so it does not leak."""
    stub = MagicMock()
    stub.require_env = lambda name, default=None: default or name
    stub.get_aws_account_id = lambda: ACCOUNT
    monkeypatch.setitem(sys.modules, "env_utils", stub)


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("UPLOAD_BUCKET", UPLOAD_BUCKET)


@pytest.fixture(autouse=True)
def pipelines(_env_utils_stub, _environment, mock_dynamodb_tables, monkeypatch):
    from handlers import upload

    table = mock_dynamodb_tables[_PIPELINES_TABLE]
    for pipeline_id, attributes in _SEEDED.items():
        table.put_item(Item={"pipeline_id": pipeline_id, "pipeline_name": pipeline_id, **attributes})
    monkeypatch.setattr(upload, "pipelines_table", table)
    monkeypatch.setattr(upload, "get_aws_account_id", lambda: ACCOUNT)
    return table


@pytest.fixture(autouse=True)
def sfn(monkeypatch):
    """The Step Functions client the handler builds, so a test can see whether a run started."""
    from handlers import upload

    client = MagicMock()
    client.start_execution.return_value = {"executionArn": f"arn:aws:states:{REGION}:{ACCOUNT}:execution:x:y"}
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = client
    monkeypatch.setattr(upload, "boto3", fake_boto3)
    return client


def _trigger(body):
    from handlers.upload import trigger_pipeline

    return trigger_pipeline({"body": json.dumps(body) if body is not None else None})


def _error(response, status):
    assert response["statusCode"] == status
    return json.loads(response["body"])["error"]


# ── Accepted ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("pipeline_id", ["p", "p-upload"])
@pytest.mark.parametrize("file_name", ["data.csv", "Q3 report (final).csv", "données.csv"])
def test_an_object_an_upload_could_create_starts_one_run(sfn, pipeline_id, file_name):
    response = _trigger({"pipeline_key": pipeline_id, "s3_uri": f"s3://{UPLOAD_BUCKET}/{pipeline_id}/{file_name}"})

    assert response["statusCode"] == 200, response["body"]
    sfn.start_execution.assert_called_once()
    execution_input = json.loads(sfn.start_execution.call_args.kwargs["input"])
    assert execution_input["source_bucket"] == UPLOAD_BUCKET
    assert execution_input["source_key"] == f"{pipeline_id}/{file_name}"
    assert execution_input["pipeline_id"] == pipeline_id


# ── Refused: the pipeline ──────────────────────────────────────────────


def test_an_unknown_pipeline_is_404_and_starts_nothing(sfn):
    response = _trigger({"pipeline_key": "no-such", "s3_uri": f"s3://{UPLOAD_BUCKET}/no-such/data.csv"})

    assert _error(response, 404) == "Pipeline not found: no-such"
    sfn.start_execution.assert_not_called()


def test_an_archived_pipeline_is_409_and_starts_nothing(sfn):
    response = _trigger({"pipeline_key": "p-archived", "s3_uri": f"s3://{UPLOAD_BUCKET}/p-archived/data.csv"})

    assert "archived" in _error(response, 409)
    sfn.start_execution.assert_not_called()


def test_a_jdbc_pipeline_is_409_and_starts_nothing(sfn):
    """This route builds an S3-upload run; a JDBC pipeline would get an input its first step rejects."""
    response = _trigger({"pipeline_key": "p-jdbc", "s3_uri": f"s3://{UPLOAD_BUCKET}/p-jdbc/data.csv"})

    error = _error(response, 409)
    assert "JDBC" in error and "/api/pipelines/p-jdbc/run" in error
    sfn.start_execution.assert_not_called()


def test_a_failed_read_is_500_not_404(sfn, pipelines):
    """Reporting a throttle or access-denied as "not found" would hide a pipeline that exists."""
    pipelines.delete()

    response = _trigger({"pipeline_key": "p", "s3_uri": f"s3://{UPLOAD_BUCKET}/p/data.csv"})

    assert "not found" not in _error(response, 500).lower()
    sfn.start_execution.assert_not_called()


# ── Refused: the object ────────────────────────────────────────────────

#: Each names an object no upload for pipeline `p` can create. The id says why.
_REFUSED_URIS = [
    pytest.param("s3://other-bucket/p/data.csv", id="another-bucket"),
    pytest.param(f"s3://{UPLOAD_BUCKET}-evil/p/data.csv", id="bucket-name-with-a-suffix"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/p-upload/data.csv", id="another-pipelines-prefix"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/other/p/data.csv", id="pipeline-id-deeper-in-the-key"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/p/a/b.csv", id="nested-key"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/p/../x.csv", id="traversal"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/p/..", id="dot-dot"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/p/a\\b.csv", id="backslash"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/p/x\ny.csv", id="control-character"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/p/" + "a" * 256, id="file-name-over-255-bytes"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/p/", id="the-prefix-itself"),
    pytest.param(f"s3://{UPLOAD_BUCKET}/p", id="no-object"),
    pytest.param(f"S3://{UPLOAD_BUCKET}/p/data.csv", id="uppercase-scheme"),
    pytest.param(f"{UPLOAD_BUCKET}/p/data.csv", id="no-scheme"),
    pytest.param(f" s3://{UPLOAD_BUCKET}/p/data.csv", id="leading-space"),
]


@pytest.mark.parametrize("s3_uri", _REFUSED_URIS)
def test_an_object_outside_the_pipelines_upload_prefix_is_400_and_starts_nothing(sfn, s3_uri):
    response = _trigger({"pipeline_key": "p", "s3_uri": s3_uri})

    assert "s3_uri" in _error(response, 400)
    sfn.start_execution.assert_not_called()


def test_a_slash_in_the_pipeline_key_is_refused_even_when_the_record_exists(sfn, pipelines):
    """The prefix is everything before the key's last `/`, so `a/b` would be read as a different pipeline."""
    pipelines.put_item(Item={"pipeline_id": "a/b", "pipeline_name": "a/b", "status": "active"})

    response = _trigger({"pipeline_key": "a/b", "s3_uri": f"s3://{UPLOAD_BUCKET}/a/b/data.csv"})

    assert "pipeline_key" in _error(response, 400)
    sfn.start_execution.assert_not_called()


# ── Refused: the request ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(None, id="no-body"),
        pytest.param({}, id="empty-object"),
        pytest.param({"pipeline_key": "p"}, id="no-s3-uri"),
        pytest.param({"s3_uri": f"s3://{UPLOAD_BUCKET}/p/data.csv"}, id="no-pipeline-key"),
    ],
)
def test_a_missing_field_is_400(sfn, body):
    assert "required" in _error(_trigger(body), 400)
    sfn.start_execution.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"pipeline_key": ["p"], "s3_uri": f"s3://{UPLOAD_BUCKET}/p/data.csv"}, id="list-key"),
        pytest.param({"pipeline_key": "p", "s3_uri": {"bucket": UPLOAD_BUCKET}}, id="object-uri"),
        pytest.param({"pipeline_key": 7, "s3_uri": f"s3://{UPLOAD_BUCKET}/7/data.csv"}, id="number-key"),
    ],
)
def test_a_non_string_field_is_400(sfn, body):
    assert "must be strings" in _error(_trigger(body), 400)
    sfn.start_execution.assert_not_called()


def test_an_unresolvable_upload_bucket_fails_closed(sfn, monkeypatch):
    """Without the bucket nothing can hold s3_uri to it, so nothing is started."""
    from handlers import upload

    monkeypatch.delenv("UPLOAD_BUCKET")
    monkeypatch.setattr(upload, "get_aws_account_id", lambda: None)

    response = _trigger({"pipeline_key": "p", "s3_uri": f"s3://{UPLOAD_BUCKET}/p/data.csv"})

    assert _error(response, 500) == "Upload bucket is not configured"
    sfn.start_execution.assert_not_called()
