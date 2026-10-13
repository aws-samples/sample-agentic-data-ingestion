# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Error path unit tests for the file_validation Lambda.

Covers:
- Missing S3 object (NoSuchKey exception)
- File exceeding MAX_FILE_SIZE_BYTES
- Empty file (0 bytes)

P28 R1.1: failure paths raise StepFailure (the Step Functions task must fail)
instead of returning ``{"statusCode": 500, ...}`` payloads.

Validates: Requirements 17.1, 17.2, 17.3
"""

import importlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_file_validation",
    )
)
SHARED_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "shared",
    )
)

_ENV = {
    "STATE_TABLE": "test-state",
    "MAX_FILE_SIZE_BYTES": str(100 * 1024 * 1024),  # 100MB for tests
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

from step_errors import StepFailure


def _load_handler(mock_s3=None, mock_dynamo_table=None):
    """Load file_validation via importlib, patching boto3 before import."""
    if mock_s3 is None:
        mock_s3 = MagicMock()
    if mock_dynamo_table is None:
        mock_dynamo_table = MagicMock()

    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_dynamo_table

    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        with patch("boto3.client", return_value=mock_s3), patch("boto3.resource", return_value=mock_dynamo):
            sys.modules.pop("lambda_function", None)
            if SHARED_DIR not in sys.path:
                sys.path.insert(0, SHARED_DIR)
            spec = importlib.util.spec_from_file_location(
                "lambda_function",
                os.path.join(LAMBDA_DIR, "lambda_function.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, mock_s3, mock_dynamo_table


def _s3_event(bucket="test-bucket", key="pipeline-1/data.csv"):
    """Build a Step Functions input format event."""
    return {
        "source_bucket": bucket,
        "source_key": key,
    }


# ---------------------------------------------------------------------------
# Tests — Missing S3 object (NoSuchKey)
# ---------------------------------------------------------------------------


class TestMissingS3Object:
    """S3 NoSuchKey exception when the file doesn't exist."""

    def test_no_such_key_raises_step_failure(self):
        """head_object raising NoSuchKey fails the step (P28 R1.1: raise, not 500)."""
        mock_s3 = MagicMock()
        error_response = {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}
        mock_s3.head_object.side_effect = ClientError(error_response, "HeadObject")

        handler, _, _ = _load_handler(mock_s3=mock_s3)
        with pytest.raises(StepFailure) as exc_info:
            handler.lambda_handler(_s3_event(), None)
        assert exc_info.value.step_name == "file_validation"
        assert exc_info.value.error_type == "permanent"

    def test_no_such_key_error_message_contains_details(self):
        """Raised StepFailure message carries the exception details."""
        mock_s3 = MagicMock()
        error_response = {"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}}
        mock_s3.head_object.side_effect = ClientError(error_response, "HeadObject")

        handler, _, _ = _load_handler(mock_s3=mock_s3)
        with pytest.raises(StepFailure) as exc_info:
            handler.lambda_handler(_s3_event(), None)
        assert "failed" in exc_info.value.message.lower() or "error" in exc_info.value.message.lower()


# ---------------------------------------------------------------------------
# Tests — File exceeding MAX_FILE_SIZE_BYTES
# ---------------------------------------------------------------------------


class TestFileSizeExceeded:
    """Files larger than MAX_FILE_SIZE_BYTES are rejected."""

    def test_oversized_file_raises_step_failure(self):
        """File exceeding MAX_FILE_SIZE_BYTES fails the step with a limit message."""
        mock_s3 = MagicMock()
        # Return a file larger than the 100MB limit
        mock_s3.head_object.return_value = {"ContentLength": 200 * 1024 * 1024}

        handler, _, _ = _load_handler(mock_s3=mock_s3)
        with pytest.raises(StepFailure) as exc_info:
            handler.lambda_handler(_s3_event(), None)
        assert exc_info.value.step_name == "file_validation"
        message = exc_info.value.message.lower()
        assert "limit" in message or "exceed" in message

    def test_file_at_exact_limit_succeeds(self):
        """File exactly at MAX_FILE_SIZE_BYTES is accepted."""
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 100 * 1024 * 1024}

        handler, _, mock_table = _load_handler(mock_s3=mock_s3)
        result = handler.lambda_handler(_s3_event(), None)
        assert result["statusCode"] == 200


# ---------------------------------------------------------------------------
# Tests — Empty file (0 bytes)
# ---------------------------------------------------------------------------


class TestEmptyFile:
    """Empty files (0 bytes) are refused at step 1 (p64 R1.1)."""

    def test_empty_file_raises_permanent_step_failure(self):
        """**Validates: Requirements R1.1**

        A 0-byte file fails the step. **This is an amended assertion, not a
        regression** — it was `test_empty_file_succeeds`, asserting
        ``statusCode == 200`` on the stated grounds that "0 bytes is under the
        limit, so it should succeed". p64 R1.1 reverses that on purpose, and the
        reversal is amended in place with its reason at the assertion per this
        repo's convention for a deliberate reversal (`LGR-60c4985-6` /
        `LGR-9e76249-10`) rather than deleted.

        Why the old expectation was wrong: admitting a zero-byte object bought
        nine steps of work on a file with nothing in it — an empty schema
        reported as success at step 2 (`schema_extract:160-161`) and a Glue run
        whose zero-row arm warns, commits and exits 0.

        Permanent, not transient, for the reason `LGR-60c4985-7` records for the
        sibling refusals: `stepRetry` matches only `TransientStepFailure`, and an
        empty object cannot become non-empty on a retry.
        """
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 0}

        handler, _, _ = _load_handler(mock_s3=mock_s3)
        with pytest.raises(StepFailure) as exc_info:
            handler.lambda_handler(_s3_event(), None)

        failure = exc_info.value
        assert failure.step_name == "file_validation"
        assert failure.error_type == "permanent"

    def test_empty_file_failure_names_the_object_and_says_it_is_empty(self):
        """**Validates: Requirements R1.1, R1.4**

        R1.1 requires the refusal to name the object and the reason. R1.4 then
        requires this message to be distinguishable from two others, so the
        negative assertions below are the load-bearing half: "header" and "data
        row" belong to `schema_extract`'s headers-only refusal (p64 1a.4) and
        "unsupported"/"recognised" to the unsupported-format one. A message that
        reached for those words would collapse three distinct refusals into one.
        """
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 0}

        handler, _, _ = _load_handler(mock_s3=mock_s3)
        with pytest.raises(StepFailure) as exc_info:
            handler.lambda_handler(_s3_event(), None)

        message = exc_info.value.message
        assert "test-bucket" in message and "pipeline-1/data.csv" in message, (
            f"R1.1: the refusal must name the object; got {message!r}"
        )
        lowered = message.lower()
        assert "empty" in lowered, f"R1.1: the refusal must state the reason; got {message!r}"
        assert "header" not in lowered, (
            f"R1.4: this refusal must not claim anything about a header — that is "
            f"schema_extract's headers-only message, and a zero-byte file has no header: {message!r}"
        )
        assert "unsupported" not in lowered, (
            f"R1.4: this refusal must stay distinguishable from the unsupported-format one: {message!r}"
        )
        # The regression the sibling unsupported-format branch already guards:
        # a refusal that falls through to the success return names a Python
        # local instead of the file (LGR-60c4985-7).
        assert "file_size_bytes" not in message
        assert "local variable" not in message

    def test_empty_file_refusal_reads_no_object_bytes(self):
        """**Validates: Requirements R1.2**

        do-NOT 1. The size comes from the HEAD response the ceiling check already
        reads, so the refusal must add no `get_object`. This is the assertion that
        would fail if someone ever "improved" the check by peeking at the body to
        tell an empty file from a headers-only one — which is R1.3's job at step 2.
        """
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 0}

        handler, _, _ = _load_handler(mock_s3=mock_s3)
        with pytest.raises(StepFailure):
            handler.lambda_handler(_s3_event(), None)

        mock_s3.head_object.assert_called_once()
        mock_s3.get_object.assert_not_called()
        mock_s3.download_file.assert_not_called()

    def test_one_byte_file_still_succeeds(self):
        """**Validates: Requirements R1.5**

        The floor is exclusive: R1.5 requires a source that has content to keep
        its current path, so the smallest non-empty object must still validate.
        Pinned because the natural over-reach here is `<=` or a minimum-plausible
        -size heuristic, either of which would start refusing real files.
        """
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ContentLength": 1}

        handler, _, _ = _load_handler(mock_s3=mock_s3)
        result = handler.lambda_handler(_s3_event(), None)
        assert result["statusCode"] == 200
        assert result["file_size_bytes"] == 1
