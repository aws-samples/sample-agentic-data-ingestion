# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test: presign_upload handler generates valid presigned POST URL
with correct bucket, key prefix, and size constraints.

Validates: AC-11.1
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


@pytest.fixture(autouse=True)
def _env_utils_stub(monkeypatch):
    """Install the env_utils stub for each test, and only for the test.

    The handler modules are imported inside the tests, so the stub must be in
    sys.modules then. Installed at module scope it leaked into every module
    collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    """
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)


# ``handlers.upload`` reads AWS_REGION at call time to build the source bucket
# name, so the region must be fixed for the duration of each test rather than
# merely defaulted at import. ``setdefault`` was a no-op whenever another test
# module (or the developer's shell) had already exported a region, which made
# the bucket assertions below depend on collection order.
TEST_AWS_REGION = "us-west-2"


@pytest.fixture(autouse=True)
def _pin_bucket_resolution_env(monkeypatch):
    """Force the region, and the *arm*, these assertions are written against.

    ``UPLOAD_BUCKET`` is cleared for the same reason the region is pinned, and
    the reason is not hypothetical: sibling modules in this directory set it at
    **import** time — ``test_audit_writes.py:40`` does
    ``os.environ.setdefault("UPLOAD_BUCKET", "test-upload-bucket")`` — and the
    whole layer runs in one pytest process, so that value leaks in here
    whenever collection order puts it first. Since p64 task 1d.4 made
    ``presign_upload`` prefer the variable over the name derivation, an
    uncleared environment would silently move the bucket assertions below onto
    the other arm and fail them by collection order alone. Tests that want the
    variable's arm set it themselves.
    """
    monkeypatch.setenv("AWS_REGION", TEST_AWS_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", TEST_AWS_REGION)
    monkeypatch.delenv("UPLOAD_BUCKET", raising=False)


# Add handler package to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_upload_returns_url_fields_key(mock_account, mock_s3):
    """presign_upload returns url, fields, and key in response."""
    mock_s3.generate_presigned_post.return_value = {
        "url": "https://adi-source-files-123456789012-us-west-2.s3.amazonaws.com",
        "fields": {
            "key": "my-pipeline/data.csv",
            "AWSAccessKeyId": "AKIAIOSFODNN7EXAMPLE",
            "policy": "base64-encoded-policy",
            "signature": "signature-value",
        },
    }

    from handlers.upload import presign_upload

    event = {
        "body": json.dumps(
            {
                "pipeline_id": "my-pipeline",
                "file_name": "data.csv",
            }
        ),
    }

    response = presign_upload(event)
    assert response["statusCode"] == 200

    body = json.loads(response["body"])
    assert "url" in body
    assert "fields" in body
    assert "key" in body
    assert body["key"] == "my-pipeline/data.csv"
    assert body["url"].startswith("https://")


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_upload_uses_correct_bucket(mock_account, mock_s3):
    """presign_upload calls generate_presigned_post with the correct source bucket.

    This is now the **fallback** arm specifically: the autouse fixture clears
    ``UPLOAD_BUCKET``, so what this pins is the name derivation p64 task 1d.4
    kept rather than deleted (``LGR-4ea43f3-7``). The variable's arm is pinned by
    ``test_presign_upload_prefers_upload_bucket_env`` below, and the two agreeing
    is asserted by ``test_presign_upload_env_and_derivation_agree``.
    """
    mock_s3.generate_presigned_post.return_value = {
        "url": "https://example.s3.amazonaws.com",
        "fields": {"key": "test/file.csv"},
    }

    from handlers.upload import presign_upload

    event = {
        "body": json.dumps(
            {
                "pipeline_id": "test-pipeline",
                "file_name": "file.csv",
            }
        ),
    }

    presign_upload(event)

    call_args = mock_s3.generate_presigned_post.call_args
    assert call_args.kwargs["Bucket"] == f"adi-source-files-123456789012-{TEST_AWS_REGION}"


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_upload_key_prefix_matches_pipeline(mock_account, mock_s3):
    """presign_upload generates key with pipeline_id prefix."""
    mock_s3.generate_presigned_post.return_value = {
        "url": "https://example.s3.amazonaws.com",
        "fields": {"key": "my-pipeline/report.json"},
    }

    from handlers.upload import presign_upload

    event = {
        "body": json.dumps(
            {
                "pipeline_id": "my-pipeline",
                "file_name": "report.json",
            }
        ),
    }

    presign_upload(event)

    call_args = mock_s3.generate_presigned_post.call_args
    assert call_args.kwargs["Key"] == "my-pipeline/report.json"


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_upload_has_size_constraint(mock_account, mock_s3):
    """presign_upload sets content-length-range condition (1 byte to 500 MB)."""
    mock_s3.generate_presigned_post.return_value = {
        "url": "https://example.s3.amazonaws.com",
        "fields": {"key": "p/f.csv"},
    }

    from handlers.upload import presign_upload

    event = {
        "body": json.dumps(
            {
                "pipeline_id": "p",
                "file_name": "f.csv",
            }
        ),
    }

    presign_upload(event)

    call_args = mock_s3.generate_presigned_post.call_args
    conditions = call_args.kwargs["Conditions"]
    assert ["content-length-range", 1, 500 * 1024 * 1024] in conditions


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_upload_has_one_hour_expiry(mock_account, mock_s3):
    """presign_upload sets 1-hour (3600s) expiry."""
    mock_s3.generate_presigned_post.return_value = {
        "url": "https://example.s3.amazonaws.com",
        "fields": {"key": "p/f.csv"},
    }

    from handlers.upload import presign_upload

    event = {
        "body": json.dumps(
            {
                "pipeline_id": "p",
                "file_name": "f.csv",
            }
        ),
    }

    presign_upload(event)

    call_args = mock_s3.generate_presigned_post.call_args
    assert call_args.kwargs["ExpiresIn"] == 3600


def test_presign_upload_missing_pipeline_id_returns_400():
    """presign_upload returns 400 when pipeline_id is missing."""
    from handlers.upload import presign_upload

    event = {
        "body": json.dumps({"file_name": "data.csv"}),
    }

    response = presign_upload(event)
    assert response["statusCode"] == 400

    body = json.loads(response["body"])
    assert "required" in body["error"].lower()


def test_presign_upload_missing_file_name_returns_400():
    """presign_upload returns 400 when file_name is missing."""
    from handlers.upload import presign_upload

    event = {
        "body": json.dumps({"pipeline_id": "my-pipeline"}),
    }

    response = presign_upload(event)
    assert response["statusCode"] == 400

    body = json.loads(response["body"])
    assert "required" in body["error"].lower()


# ---------------------------------------------------------------------------
# p64 task 1d.4 (R4.5) — bucket resolution: environment first, derivation kept
# ---------------------------------------------------------------------------
# R4.5 asks this handler to read `UPLOAD_BUCKET` instead of reconstructing
# `adi-source-files-{account}-{region}`. `LGR-4ea43f3-7` (template / ratified)
# forbids the second half of that sentence: it settled the identical question
# for `PROCESSED_BUCKET` on `schema_extract` by setting the variable AND keeping
# the derivation, explicitly rejecting removal, because the fallback keeps a
# fail-closed refusal reachable and covers a deploy landing code before the
# variable. So there are three behaviours to pin, not one — each arm, and the
# refusal that only exists because the arms can both come up empty.


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_upload_prefers_upload_bucket_env(mock_account, mock_s3, monkeypatch):
    """UPLOAD_BUCKET wins over the name derivation when it is set.

    Deliberately set to a name the derivation could never compose, so a pass
    cannot be explained by the two happening to agree.

    Validates: Requirements R4.5
    """
    monkeypatch.setenv("UPLOAD_BUCKET", "some-other-bucket-name")
    mock_s3.generate_presigned_post.return_value = {
        "url": "https://example.s3.amazonaws.com",
        "fields": {"key": "p/f.csv"},
    }

    from handlers.upload import presign_upload

    response = presign_upload({"body": json.dumps({"pipeline_id": "p", "file_name": "f.csv"})})

    assert response["statusCode"] == 200
    assert mock_s3.generate_presigned_post.call_args.kwargs["Bucket"] == "some-other-bucket-name"


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_upload_env_and_derivation_agree(mock_account, mock_s3, monkeypatch):
    """The deployed value and the derivation compose the same name.

    This is the claim that makes 1d.4 a second-definition cleanup rather than a
    behaviour change, and it is the claim that would break first if the bucket
    naming scheme moved — which is exactly `LGR-4ea43f3-7`'s stated revisit
    condition. The env value here is the literal CDK injects
    (`compute-stack.ts:1047` → `foundation.sourceBucket.bucketName`, declared
    `adi-source-files-{account}-{region}` in foundation-stack).

    Validates: Requirements R4.5
    """
    from handlers.upload import presign_upload

    event = {"body": json.dumps({"pipeline_id": "p", "file_name": "f.csv"})}
    mock_s3.generate_presigned_post.return_value = {"url": "https://x", "fields": {}}

    # Fallback arm — UPLOAD_BUCKET cleared by the autouse fixture.
    presign_upload(event)
    derived = mock_s3.generate_presigned_post.call_args.kwargs["Bucket"]

    # Environment arm, using the name the deploy actually injects.
    monkeypatch.setenv("UPLOAD_BUCKET", f"adi-source-files-123456789012-{TEST_AWS_REGION}")
    presign_upload(event)
    from_env = mock_s3.generate_presigned_post.call_args.kwargs["Bucket"]

    assert derived == from_env == f"adi-source-files-123456789012-{TEST_AWS_REGION}"


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="")
def test_presign_upload_refuses_when_bucket_unresolvable(mock_account, mock_s3):
    """Neither arm resolves -> 500 naming UPLOAD_BUCKET, and NOTHING is signed.

    The "nothing is signed" half is the point. `generate_presigned_post` signs a
    URL against any name handed to it, so an unresolved bucket would otherwise
    produce a 200 carrying a URL that fails opaquely at upload time — a success
    report for an upload that cannot land. This refusal is what
    `LGR-4ea43f3-7` means by the fallback keeping a fail-closed path reachable.

    Validates: Requirements R4.5
    """
    from handlers.upload import presign_upload

    response = presign_upload({"body": json.dumps({"pipeline_id": "p", "file_name": "f.csv"})})

    assert response["statusCode"] == 500
    assert "UPLOAD_BUCKET" in json.loads(response["body"])["details"]
    mock_s3.generate_presigned_post.assert_not_called()


# ---------------------------------------------------------------------------
# p64 task 1d.4 — the body-less request answers 400, not 500
# ---------------------------------------------------------------------------


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_upload_null_body_returns_400_not_500(mock_account, mock_s3):
    """A real gateway event for a body-less POST carries body=None -> 400.

    `json.loads(None)` raises TypeError, and the outer `except` used to convert
    that into `500 Failed to generate presigned upload URL` — the server taking
    blame for a client's malformed request. The contract test
    `test_presign_contract.py::test_presign_missing_body_returns_400` claimed to
    cover this but sends `json.dumps({})`, so the shape its name describes went
    unexercised. `None` is how API Gateway spells "no body"; a missing `body`
    key is not.

    Validates: Requirements R4.5
    """
    from handlers.upload import presign_upload

    response = presign_upload({"httpMethod": "POST", "path": "/api/upload/presign", "body": None})

    assert response["statusCode"] == 400
    assert "required" in json.loads(response["body"])["error"].lower()
    mock_s3.generate_presigned_post.assert_not_called()


# ---------------------------------------------------------------------------
# p64 hotfix — the response carries a SIGNED confirmation URL
# ---------------------------------------------------------------------------
# Task 1d.5 shipped a landing confirmation that fetched the bare object URL
# anonymously. The source bucket is `BlockPublicAccess.BLOCK_ALL`
# (`foundation-stack.ts:64`, applied to `SourceFiles` at `:74`) with no
# `AnyPrincipal` statement anywhere in `deploy-cdk/lib/`, so S3 answered **403
# for every object, present or absent** — it returns 403 rather than 404 to
# unauthorized callers so keys cannot be enumerated. The check could not ever
# have passed, and it reported failure for uploads that had succeeded.
#
# The justification given was that the bucket's CORS already allowed `HEAD`.
# CORS decides whether a browser exposes a response to JavaScript; it grants no
# S3 authorization. Nothing about a CORS configuration can make an anonymous
# request authorized.
#
# These tests sign with a **real** botocore client rather than a mock, because a
# mocked signer cannot be unauthorized — mocking the signature is how the
# frontend suite stayed green while the feature was unusable. `_confirm_s3_client`
# is patched only to inject deterministic credentials, never to fake the signing.

import boto3
from botocore.config import Config as BotoConfig


def _real_presigning_client():
    """A real S3 client with fixed fake credentials, configured as the handler is.

    The explicit `signature_version="s3v4"` is not decoration. Measured on
    botocore 1.43.33: a default `boto3.client("s3")` reports
    `meta.config.signature_version == "s3v4"` and still presigns **SigV2**
    (`AWSAccessKeyId`/`Signature`/`Expires`). Only an explicit Config yields
    `X-Amz-Signature`, and SigV2 presigned URLs are rejected by S3 in every region
    launched after January 2014. This mirrors `handlers/upload.py`'s own client so
    a drift in that construction shows up here.
    """
    return boto3.client(
        "s3",
        region_name=TEST_AWS_REGION,
        aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
        aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_response_confirm_url_is_signed(mock_account, mock_s3):
    """`confirm_url` carries SigV4 credentials — the property 1d.5 violated.

    Validates: Requirements R4.4
    """
    mock_s3.generate_presigned_post.return_value = {"url": "https://x", "fields": {}}

    from handlers import upload

    with patch.object(upload, "_confirm_s3_client", _real_presigning_client()):
        response = upload.presign_upload({"body": json.dumps({"pipeline_id": "p", "file_name": "f.csv"})})

    assert response["statusCode"] == 200
    confirm_url = json.loads(response["body"])["confirm_url"]

    parsed = urlparse(confirm_url)
    params = parse_qs(parsed.query)

    # Without a credential the URL is anonymous, and an anonymous HEAD against
    # this bucket is 403 for every key. This is the whole defect, in one assert.
    assert "X-Amz-Signature" in params, f"confirmation URL is not signed: {confirm_url}"
    assert "X-Amz-Credential" in params, f"confirmation URL names no credential: {confirm_url}"
    assert parsed.scheme == "https"
    # SigV2 (`AWSAccessKeyId`/`Signature`) would be rejected outright in any region
    # launched after January 2014, so a *presigned but SigV2* URL is still broken.
    assert "AWSAccessKeyId" not in params, "SigV2 presign — rejected in post-2014 regions"


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_confirm_url_targets_the_signed_key_in_the_upload_bucket(mock_account, mock_s3):
    """The confirmation points at the same bucket/key the POST was signed for.

    A signed URL for the wrong object would confirm the wrong fact, which is a
    quieter failure than a 403 and therefore worth pinning separately.

    Validates: Requirements R4.4
    """
    mock_s3.generate_presigned_post.return_value = {"url": "https://x", "fields": {}}

    from handlers import upload

    with patch.object(upload, "_confirm_s3_client", _real_presigning_client()):
        response = upload.presign_upload(
            {"body": json.dumps({"pipeline_id": "my-pipeline", "file_name": "report.json"})}
        )

    body = json.loads(response["body"])
    parsed = urlparse(body["confirm_url"])

    expected_bucket = f"adi-source-files-123456789012-{TEST_AWS_REGION}"
    assert parsed.netloc.startswith(f"{expected_bucket}."), parsed.netloc
    assert parsed.path == f"/{body['key']}" == "/my-pipeline/report.json"


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_confirm_url_expiry_matches_the_post_expiry(mock_account, mock_s3):
    """Both presigns share one expiry, so the confirmation cannot outlive-fail.

    The POST URL is minted at the start of an upload and the confirmation is
    fetched at the end of it. A shorter confirmation expiry would start returning
    403 on exactly the large, slow uploads most worth confirming — reintroducing
    "the check can only fail" for a subset of files instead of all of them. One
    shared constant is what makes that unrepresentable.

    Validates: Requirements R4.4
    """
    mock_s3.generate_presigned_post.return_value = {"url": "https://x", "fields": {}}

    from handlers import upload

    with patch.object(upload, "_confirm_s3_client", _real_presigning_client()):
        response = upload.presign_upload({"body": json.dumps({"pipeline_id": "p", "file_name": "f.csv"})})

    post_expiry = mock_s3.generate_presigned_post.call_args.kwargs["ExpiresIn"]
    confirm_expiry = int(parse_qs(urlparse(json.loads(response["body"])["confirm_url"]).query)["X-Amz-Expires"][0])

    assert confirm_expiry == post_expiry == upload._PRESIGN_EXPIRY_SECONDS


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_still_succeeds_when_the_confirmation_url_cannot_be_minted(mock_account, mock_s3):
    """A guard that cannot be built must not become a gate.

    `LGR-4863153-2` records the confirmation as a guard that makes a silent no-op
    visible. If signing fails, the correct outcome is an upload that proceeds and
    reports itself unconfirmed — blocking would recreate the outage this hotfix
    removes, from the other direction. `confirm_url` is then `None`, and the
    client renders a warning rather than treating absence as confirmation.

    Validates: Requirements R4.4
    """
    from handlers import upload

    broken = MagicMock()
    broken.generate_presigned_url.side_effect = RuntimeError("no credentials")
    mock_s3.generate_presigned_post.return_value = {"url": "https://x", "fields": {"key": "p/f.csv"}}

    with patch.object(upload, "_confirm_s3_client", broken):
        response = upload.presign_upload({"body": json.dumps({"pipeline_id": "p", "file_name": "f.csv"})})

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["confirm_url"] is None
    # The upload itself is untouched — url/fields/key still present.
    assert body["url"] and body["key"] == "p/f.csv"


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="")
def test_presign_mints_no_confirmation_url_when_the_bucket_is_unresolvable(mock_account, mock_s3):
    """The fail-closed refusal signs NOTHING — not even a confirmation URL.

    Companion to `test_presign_upload_refuses_when_bucket_unresolvable`. Signing a
    confirmation URL against an unresolved bucket name would produce a URL that
    fails opaquely later, which is the family of silent failure R4.4 exists to end.

    Validates: Requirements R4.4, R4.5
    """
    from handlers import upload

    confirm_client = MagicMock()
    with patch.object(upload, "_confirm_s3_client", confirm_client):
        response = upload.presign_upload({"body": json.dumps({"pipeline_id": "p", "file_name": "f.csv"})})

    assert response["statusCode"] == 500
    confirm_client.generate_presigned_url.assert_not_called()
    mock_s3.generate_presigned_post.assert_not_called()


# ---------------------------------------------------------------------------
# S5 — presign only for a pipeline that exists, and only for a key that stays
# inside that pipeline's prefix
# ---------------------------------------------------------------------------
# An object landing in the source bucket starts a run of the pipeline its key
# names, and `pipeline_trigger._pipeline_prefix` reads that pipeline as
# everything before the LAST `/`. The handler used to sign any
# `{pipeline_id}/{file_name}` it was handed, so a caller could write under a
# prefix no pipeline owns, or put a `/` in `file_name` and land in a different
# prefix from the one it named.
#
# Expectations come from that requirement, not from running the handler: the
# filename rule is a DENYLIST because the dashboard sends the browser's raw
# `File.name`, so real names with spaces, brackets, accented letters and several
# dots must keep producing exactly `f"{pipeline_id}/{file_name}"`.
#
# Everything for S5 is appended here, below the rest of the file, including the
# autouse fixture that seeds the pipelines every test above presigns for. ASH
# suppresses the documentation-example secret in `_real_presigning_client` by
# LINE (`.ash/.ash.yaml`), so an insertion above it would un-suppress it.
# pytest applies an autouse fixture to the whole module wherever it is defined.

_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"

# Every pipeline id a test in this module presigns for. Before S5 the handler
# never read the table, so these tests never needed the rows.
_SEEDED_PIPELINE_IDS = ("my-pipeline", "test-pipeline", "p")


@pytest.fixture(autouse=True)
def _seeded_pipelines_table(_env_utils_stub, _pin_bucket_resolution_env, mock_dynamodb_tables, monkeypatch):
    """Point the handler at the shared moto pipelines table, seeded.

    Requests the two fixtures above by name so they run first: importing
    `handlers.upload` needs the `env_utils` stub, and the bucket assertions need
    the pinned region. `mock_lambda_env` is deliberately not used, because it sets
    `AWS_REGION=us-east-1` and would move the bucket assertions above onto
    another region.
    """
    from handlers import upload

    table = mock_dynamodb_tables[_PIPELINES_TABLE]
    for pipeline_id in _SEEDED_PIPELINE_IDS:
        table.put_item(Item={"pipeline_id": pipeline_id, "pipeline_name": pipeline_id, "status": "active"})
    monkeypatch.setattr(upload, "pipelines_table", table)
    return table


def _presign(pipeline_id, file_name):
    from handlers.upload import presign_upload

    return presign_upload({"body": json.dumps({"pipeline_id": pipeline_id, "file_name": file_name})})


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_unknown_pipeline_returns_404_and_signs_nothing(mock_account, mock_s3):
    """A pipeline id the table does not hold answers 404 before any signing.

    The 404 body matches `pipelines_crud`'s "pipeline not found" responses, which
    the dashboard's `ApiError` already lifts into the message it shows.
    """
    from handlers import upload

    confirm_client = MagicMock()
    with patch.object(upload, "_confirm_s3_client", confirm_client):
        response = _presign("no-such-pipeline", "data.csv")

    assert response["statusCode"] == 404
    assert json.loads(response["body"]) == {"error": "Pipeline not found: no-such-pipeline"}
    mock_s3.generate_presigned_post.assert_not_called()
    confirm_client.generate_presigned_url.assert_not_called()


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_pipeline_read_error_is_500_not_404(mock_account, mock_s3, _seeded_pipelines_table):
    """Only a missing item is a 404; a failed read is a server error.

    Reporting a throttle or an access-denied as "Pipeline not found" would send
    the user to look for a pipeline that exists. The table is deleted so the read
    fails with DynamoDB's own ResourceNotFoundException.
    """
    _seeded_pipelines_table.delete()

    response = _presign("p", "data.csv")

    assert response["statusCode"] == 500
    assert "not found" not in json.loads(response["body"])["error"].lower()
    mock_s3.generate_presigned_post.assert_not_called()


# Each refused class from the S5 brief, plus the 0x7F, NUL, tab and multibyte
# cases of the same rules. Each id says why the name is refused.
_REFUSED_FILE_NAMES = [
    pytest.param("../x.csv", id="parent-traversal"),
    pytest.param("a/b.csv", id="slash"),
    pytest.param("a\\b.csv", id="backslash"),
    pytest.param("x\nY.csv", id="newline"),
    pytest.param("tab\t.csv", id="tab"),
    pytest.param("\x00.csv", id="nul"),
    pytest.param("del\x7f.csv", id="del-0x7f"),
    pytest.param(".", id="dot"),
    pytest.param("..", id="dot-dot"),
    pytest.param("a" * 252 + ".csv", id="256-bytes-ascii"),
    pytest.param("é" * 128, id="256-bytes-multibyte"),
    pytest.param("\ud800.csv", id="lone-surrogate"),
]


@pytest.mark.parametrize("file_name", _REFUSED_FILE_NAMES)
@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_refuses_file_name_outside_the_pipeline_prefix(mock_account, mock_s3, file_name):
    """Each refused name answers 400 naming `file_name`, and nothing is signed.

    The pipeline exists, so the 400 can only come from the name.
    """
    from handlers import upload

    confirm_client = MagicMock()
    with patch.object(upload, "_confirm_s3_client", confirm_client):
        response = _presign("p", file_name)

    assert response["statusCode"] == 400, response
    assert "file_name" in json.loads(response["body"])["error"]
    mock_s3.generate_presigned_post.assert_not_called()
    confirm_client.generate_presigned_url.assert_not_called()


def test_the_256_byte_cases_are_256_bytes():
    """The two length cases above sit one byte past the limit, not anywhere past it."""
    assert len(("a" * 252 + ".csv").encode("utf-8")) == 256
    assert len(("é" * 128).encode("utf-8")) == 256


@pytest.mark.parametrize(
    "file_name",
    [
        pytest.param(123, id="int"),
        pytest.param(["a.csv"], id="list"),
        pytest.param({"name": "a.csv"}, id="object"),
        pytest.param(True, id="bool"),
    ],
)
@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_refuses_non_string_file_name(mock_account, mock_s3, file_name):
    """A JSON value that is not a string answers 400, not a 500 from the f-string."""
    response = _presign("p", file_name)

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "file_name must be a string"}
    mock_s3.generate_presigned_post.assert_not_called()


@pytest.mark.parametrize(
    "pipeline_id",
    [
        pytest.param(123, id="int"),
        pytest.param(["p"], id="list"),
        pytest.param({"id": "p"}, id="object"),
    ],
)
@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_refuses_non_string_pipeline_id(mock_account, mock_s3, pipeline_id):
    response = _presign(pipeline_id, "data.csv")

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "pipeline_id must be a string"}
    mock_s3.generate_presigned_post.assert_not_called()


@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_refuses_slash_in_pipeline_id_even_when_the_record_exists(
    mock_account, mock_s3, _seeded_pipelines_table
):
    """Defence in depth: the refusal does not rely on the existence check.

    The id is seeded, so the existence check alone would let it through and the
    key would gain an extra prefix level.
    """
    _seeded_pipelines_table.put_item(Item={"pipeline_id": "a/b", "pipeline_name": "a/b"})

    response = _presign("a/b", "data.csv")

    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "pipeline_id must not contain '/'"}
    mock_s3.generate_presigned_post.assert_not_called()


@pytest.mark.parametrize(
    "file_name",
    [
        pytest.param("Q3 report (final).csv", id="spaces-and-parens"),
        pytest.param("données.csv", id="accented"),
        pytest.param("a.b.c.tsv", id="many-dots"),
        pytest.param("[draft] v2 – copy.xlsx", id="brackets-and-en-dash"),
        pytest.param("売上データ.csv", id="cjk"),
        pytest.param(".hidden.csv", id="leading-dot"),
        pytest.param("...csv", id="dots-only-prefix"),
        pytest.param("a" * 251 + ".csv", id="255-bytes"),
    ],
)
@patch("handlers.upload.s3_client")
@patch("handlers.upload.get_aws_account_id", return_value="123456789012")
def test_presign_accepts_real_browser_file_names_unchanged(mock_account, mock_s3, file_name):
    """Ordinary `File.name` values presign, with the key exactly as sent.

    "Exactly" is the point of a denylist that refuses rather than repairs: the
    signed key and the response's `key` both equal `f"{pipeline_id}/{file_name}"`
    byte for byte.
    """
    mock_s3.generate_presigned_post.return_value = {"url": "https://x", "fields": {}}

    response = _presign("p", file_name)

    assert response["statusCode"] == 200, response
    expected_key = f"p/{file_name}"
    assert mock_s3.generate_presigned_post.call_args.kwargs["Key"] == expected_key
    assert json.loads(response["body"])["key"] == expected_key
