# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Run Now endpoint (P17 Task 12.3).

Covers ``handlers.pipelines.run_now`` — the synchronous handler behind
``POST /api/pipelines/{pipeline_id}/run``. The handler:

1. Looks up the pipeline by ``pipeline_id``; returns 404 if missing,
   409 if archived.
2. Reads ``source_type`` (defaulting to ``S3_UPLOAD`` for legacy
   pipelines that pre-date the P17 source-type discriminator).
3. Synchronously invokes the Pipeline_Trigger_Lambda with a manual
   payload via ``boto3.client("lambda").invoke(...)``.
4. Translates the trigger Lambda's response shape:
   - ``statusCode == 200`` → 200 with ``execution_arn`` / ``execution_name``
   - ``statusCode == 429`` → 429 (concurrency limit hit)
   - ``FunctionError == "Unhandled"`` → 504 (Lambda timeout)
   - SDK-level invoke exception → 504 (network / read-timeout)
   - Other status codes → forwarded verbatim
5. Writes a ``pipeline_run_now`` audit entry on success.

Validates: Requirements 5.6, 5.7, 5.8, 22.1
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler. Mirrors
# the bootstrap used by the other dashboard-API tests so the handler
# imports cleanly in a unit-test process without the real Lambda layer.
# ---------------------------------------------------------------------------

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "agentic-data-ingestion-source",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "PIPELINE_TRIGGER_ARN": "arn:aws:lambda:us-east-1:123456789012:function:trigger",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

# Make ``shared/`` importable so ``handlers.pipelines`` resolves its
# ``from jdbc_helpers import ...`` line.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

# Add handler package to path
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)

try:
    from handlers import (
        pipelines,
        pipelines_runs,
    )
    from handlers.pipelines import run_now
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

# ---------------------------------------------------------------------------
# Shared mocks
# ---------------------------------------------------------------------------

PIPELINE_ID = "test-run-now-001"

mock_pipelines_table = MagicMock()
mock_lambda_client = MagicMock()


def _trigger_response(
    *,
    body_payload: dict | None = None,
    status_code: int = 200,
    function_error: str | None = None,
    raw_payload: bytes | None = None,
) -> dict:
    """Build a synthetic boto3 ``invoke`` response.

    ``raw_payload`` overrides ``body_payload`` and ``status_code`` when
    supplied — used for the FunctionError unhandled-error envelope and
    for the malformed-JSON path.
    """
    if raw_payload is None:
        envelope = {"statusCode": status_code}
        if body_payload is not None:
            envelope["body"] = json.dumps(body_payload)
        # The trigger Lambda flattens execution_arn/execution_name to
        # the top level of the response so the dashboard API can read
        # them without parsing ``body``. Mirror that shape.
        if body_payload:
            for key in ("execution_arn", "execution_name"):
                if key in body_payload:
                    envelope[key] = body_payload[key]
        raw_payload = json.dumps(envelope).encode("utf-8")

    streaming_body = MagicMock()
    streaming_body.read.return_value = raw_payload

    response: dict = {"Payload": streaming_body, "StatusCode": 200}
    if function_error:
        response["FunctionError"] = function_error
    return response


def _pipeline_record(
    *,
    source_type: str | None = "S3_UPLOAD",
    status: str = "active",
) -> dict:
    item: dict = {"pipeline_id": PIPELINE_ID, "status": status}
    if source_type is not None:
        item["source_type"] = source_type
    return item


@pytest.fixture(autouse=True)
def _reset_mocks():
    """Reset shared mocks between tests and rewire the handler-module
    references so each test starts with a clean slate.

    Also rebind the names ``pipelines.py`` imported from ``audit_utils``
    to the mocks defined in this file. Under pytest-xdist
    ``--dist=loadfile`` a sibling test file (e.g. ``test_pipelines_retry.py``)
    may run first in the same worker process and install its own
    ``MagicMock`` into ``sys.modules['audit_utils']`` before importing
    ``handlers.pipelines``. The handler module then has
    ``write_audit_entry`` bound to the *other* file's mock, so this
    file's ``mock_audit_utils.write_audit_entry.assert_called_once()``
    spuriously fails. Rebinding here pins the call to our mock for
    every test in this file.
    """
    mock_pipelines_table.reset_mock()
    mock_lambda_client.reset_mock(side_effect=True)
    mock_audit_utils.write_audit_entry.reset_mock()

    pipelines.pipelines_table = mock_pipelines_table
    # Pin audit-related names on the handler module to this file's mocks
    # so tests can assert on them regardless of the mock that was active
    # when ``handlers.pipelines`` was first imported.
    pipelines.write_audit_entry = mock_audit_utils.write_audit_entry
    pipelines.extract_request_context = mock_audit_utils.extract_request_context

    # P21 Task 7.2: run_now now lives in handlers.pipelines_runs; wire the
    # same mocks into the owning module so the relocated handler sees them
    # (module references only — behaviour-preserving).
    pipelines_runs.pipelines_table = mock_pipelines_table
    pipelines_runs.write_audit_entry = mock_audit_utils.write_audit_entry
    pipelines_runs.extract_request_context = mock_audit_utils.extract_request_context

    # Default: pipeline exists, S3_UPLOAD source, active
    mock_pipelines_table.get_item.return_value = {
        "Item": _pipeline_record(source_type="S3_UPLOAD", status="active"),
    }

    # Default: trigger Lambda happy path
    mock_lambda_client.invoke.return_value = _trigger_response(
        body_payload={
            "execution_arn": "arn:aws:states:us-east-1:123:execution:sm:exec-1",
            "execution_name": "exec-1",
        },
        status_code=200,
    )

    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_invoke_payload() -> dict:
    """Decode the JSON payload passed to ``lambda_client.invoke``."""
    call_kwargs = mock_lambda_client.invoke.call_args[1]
    return json.loads(call_kwargs["Payload"])


# ===========================================================================
# 1. Happy path — JDBC source
# ===========================================================================


class TestRunNowHappyPathJdbcSource:
    """JDBC pipeline → trigger 200 → handler 200 with execution metadata."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_jdbc_source_returns_200_with_execution_metadata(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        mock_pipelines_table.get_item.return_value = {
            "Item": _pipeline_record(source_type="JDBC"),
        }
        mock_lambda_client.invoke.return_value = _trigger_response(
            body_payload={
                "execution_arn": "arn:aws:states:us-east-1:123:execution:sm:jdbc-1",
                "execution_name": "jdbc-1",
            },
            status_code=200,
        )

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["success"] is True
        assert body["pipeline_id"] == PIPELINE_ID
        assert body["source_type"] == "JDBC"
        assert body["execution_arn"] == "arn:aws:states:us-east-1:123:execution:sm:jdbc-1"
        assert body["execution_name"] == "jdbc-1"

    @patch("handlers.pipelines_runs.boto3.client")
    def test_jdbc_source_invokes_trigger_with_manual_payload(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        mock_pipelines_table.get_item.return_value = {
            "Item": _pipeline_record(source_type="JDBC"),
        }

        run_now(PIPELINE_ID, event={})

        # Lambda invoked exactly once, RequestResponse, with the right
        # function name and payload shape.
        mock_lambda_client.invoke.assert_called_once()
        call_kwargs = mock_lambda_client.invoke.call_args[1]
        assert call_kwargs["InvocationType"] == "RequestResponse"
        assert call_kwargs["FunctionName"] == "agentic_data_ingestion_pipeline_trigger"

        payload = _get_invoke_payload()
        assert payload["trigger"] == "manual"
        assert payload["pipeline_id"] == PIPELINE_ID
        assert payload["source_type"] == "JDBC"


# ===========================================================================
# 2. Happy path — S3_UPLOAD source
# ===========================================================================


class TestRunNowHappyPathS3UploadSource:
    """S3_UPLOAD pipeline → trigger 200 → handler 200 with source_type=S3_UPLOAD."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_s3_upload_source_invokes_trigger_with_s3_upload_source_type(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        mock_pipelines_table.get_item.return_value = {
            "Item": _pipeline_record(source_type="S3_UPLOAD"),
        }

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["source_type"] == "S3_UPLOAD"

        payload = _get_invoke_payload()
        assert payload["source_type"] == "S3_UPLOAD"
        assert payload["pipeline_id"] == PIPELINE_ID
        assert payload["trigger"] == "manual"


# ===========================================================================
# 3. Legacy pipeline with no source_type — defaults to S3_UPLOAD
# ===========================================================================


class TestRunNowLegacyPipelineDefault:
    """Pipeline missing ``source_type`` → handler defaults to S3_UPLOAD."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_legacy_pipeline_defaults_to_s3_upload(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        # Legacy pipeline has no source_type field.
        mock_pipelines_table.get_item.return_value = {
            "Item": _pipeline_record(source_type=None),
        }

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["source_type"] == "S3_UPLOAD"

        payload = _get_invoke_payload()
        assert payload["source_type"] == "S3_UPLOAD"


# ===========================================================================
# 4. Pipeline not found → 404
# ===========================================================================


class TestRunNowPipelineNotFound:
    """``get_item`` returns no Item → handler returns 404 and skips invoke."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_missing_pipeline_returns_404(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        mock_pipelines_table.get_item.return_value = {}  # No Item key

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert PIPELINE_ID in body["error"]
        # Do not invoke the trigger Lambda for a missing pipeline.
        mock_lambda_client.invoke.assert_not_called()


# ===========================================================================
# 5. Archived pipeline → 409
# ===========================================================================


class TestRunNowArchivedPipeline:
    """Archived pipeline cannot be run → handler returns 409 and skips invoke."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_archived_pipeline_returns_409(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        mock_pipelines_table.get_item.return_value = {
            "Item": _pipeline_record(status="archived"),
        }

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert "archived" in body["error"]
        # No trigger invoke for an archived pipeline.
        mock_lambda_client.invoke.assert_not_called()


# ===========================================================================
# 6. Concurrency-limit hit → 429
# ===========================================================================


class TestRunNowConcurrencyLimit:
    """Trigger Lambda returns 429 → handler forwards 429."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_concurrency_limit_hit_returns_429(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _trigger_response(
            body_payload={"error": "Concurrency limit reached: 10/10 running"},
            status_code=429,
        )

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 429
        body = json.loads(result["body"])
        assert "Concurrency limit hit" in body["error"]
        # Body is forwarded verbatim so the UI can show the specific reason.
        assert "details" in body


# ===========================================================================
# 7. Lambda timeout via FunctionError → 504
# ===========================================================================


class TestRunNowFunctionErrorTimeout:
    """``FunctionError == "Unhandled"`` → handler returns 504."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_function_error_unhandled_returns_504(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        # Lambda timeout: FunctionError set, payload is an error envelope.
        unhandled_envelope = json.dumps(
            {
                "errorMessage": "Task timed out after 5.00 seconds",
                "errorType": "Sandbox.Timedout",
            }
        ).encode("utf-8")
        mock_lambda_client.invoke.return_value = _trigger_response(
            function_error="Unhandled",
            raw_payload=unhandled_envelope,
        )

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 504
        body = json.loads(result["body"])
        assert "timed out" in body["error"].lower() or "errored" in body["error"].lower()
        # The errorMessage is surfaced in details so operators can see why.
        assert "Task timed out" in body["details"]


# ===========================================================================
# 8. SDK-level invoke exception (e.g. ReadTimeoutError) → 504
# ===========================================================================


class TestRunNowInvokeException:
    """``lambda_client.invoke`` raises → handler returns 504."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_invoke_raises_returns_504(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        # Simulate botocore ReadTimeoutError surfacing as a generic
        # Exception at the call site. The handler catches it and maps
        # to 504 so the UI can show a "timed out" toast.
        mock_lambda_client.invoke.side_effect = Exception("Read timeout on endpoint URL")

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 504
        body = json.loads(result["body"])
        assert "timed out" in body["error"].lower()
        assert "Read timeout" in body["details"]


# ===========================================================================
# 9. Non-200/429 status from trigger → forwarded verbatim
# ===========================================================================


class TestRunNowForwardedNonSuccess:
    """Trigger Lambda returns 500 → handler forwards 500 with details."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_trigger_500_is_forwarded(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _trigger_response(
            body_payload={"error": "State machine start failed"},
            status_code=500,
        )

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "non-success" in body["error"]
        assert "details" in body


# ===========================================================================
# 10. Audit write on success
# ===========================================================================


class TestRunNowAuditOnSuccess:
    """Successful run writes a ``pipeline_run_now`` audit entry."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_success_writes_audit_entry(self, mock_boto):
        mock_boto.return_value = mock_lambda_client
        mock_pipelines_table.get_item.return_value = {
            "Item": _pipeline_record(source_type="JDBC"),
        }
        mock_lambda_client.invoke.return_value = _trigger_response(
            body_payload={
                "execution_arn": "arn:aws:states:us-east-1:123:execution:sm:audit-1",
                "execution_name": "audit-1",
            },
            status_code=200,
        )

        result = run_now(PIPELINE_ID, event={})

        assert result["statusCode"] == 200
        # Audit entry was written with the right action and resource.
        mock_audit_utils.write_audit_entry.assert_called_once()
        call_kwargs = mock_audit_utils.write_audit_entry.call_args[1]
        assert call_kwargs["action"] == "pipeline_run_now"
        assert call_kwargs["resource_type"] == "pipeline"
        assert call_kwargs["resource_id"] == PIPELINE_ID
        assert call_kwargs["details"]["source_type"] == "JDBC"
        assert call_kwargs["details"]["execution_name"] == "audit-1"
