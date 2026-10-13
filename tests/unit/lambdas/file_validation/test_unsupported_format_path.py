# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The unsupported-extension path of file_validation fails honestly.

This is the unit-level guard for a defect that reached a deployed environment:
an `.xlsx` upload made `detect_format` return `"unknown"`, the unsupported
branch wrote its metadata row, and then execution fell through to the SUCCESS
return — which reads `file_size_bytes`, a name only ever assigned in the
sibling `else`. The Lambda raised

    UnboundLocalError: cannot access local variable 'file_size_bytes'
    where it is not associated with a value

which `lambda_handler`'s except block wrapped as
`StepFailure("File processing failed: cannot access local variable ...")`.
The failure was real but its message named a Python variable instead of the
unsupported file, and the shape of the bug meant the branch's own intent (write
an `unsupported` row and stop) was never actually reached.

What the fix asserts here:

* an unrecognised extension raises a PERMANENT `StepFailure` whose message
  names the unsupported extension — never an `UnboundLocalError`;
* the `unsupported` audit row is still written before the failure;
* the success return is not taken, so no downstream state is handed a payload
  built from ids that were never minted;
* the sibling doorway onto the same unbound name — an event carrying no S3
  record at all — is named too.

Why a *unit* test: the deployed-environment layer caught this only because a
`.xlsx` happened to be uploaded there, and it reported the crash as a Lambda
error rather than as the missing-format-support finding it is. A crash on a
branch this reachable belongs to the layer that runs on every commit.

Not asserted here: whether any particular extension SHOULD be supported.
`test_format_detection.py`'s `test_unknown_extension` and `TestConstants::
test_format_map_has_all_extensions` own that question — the first by pinning
what a given extension detects as, the second by pinning the exact `FORMAT_MAP`
key set. Adding format support is a product change that has to go through those
fences deliberately, not through this file.

**Amended by p55 W3 (task 4.2): `.xlsx` is no longer this file's example of an
unsupported extension, because `xlsx` is now IN `FORMAT_MAP`, mapped to the
Canonical_Format `excel` (R6.1).** The historical narrative above is left
standing — it is what happened, and `.xlsx` genuinely was the file that exposed
the crash — but every *assertion* below now uses `.pdf`, an extension outside
`FORMAT_MAP`, so each test still tests the branch its name claims. Keeping
`.xlsx` would have inverted these tests silently: they would have been asserting
that a supported format fails. The two fences p55 amended deliberately are F1
and F2 in `test_format_detection.py`; this file's `.xlsx` usages were not
enumerated by the spec and are corrected here as a consequence of that change,
not as a fence crossing of their own.

Validates: Requirements 17.1, 17.2, 17.3 (honest failure on an invalid input)
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

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
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

_ENV = {
    "STATE_TABLE": "test-state",
    "PIPELINES_TABLE": "test-pipelines",
    "MAX_FILE_SIZE_BYTES": str(100 * 1024 * 1024),
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

from step_errors import StepFailure  # (the sys.path setup above must precede this import)


def _load_handler():
    """Load file_validation with boto3 patched out, as the sibling suites do."""
    mock_s3 = MagicMock()
    mock_table = MagicMock()
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table

    with (
        patch.dict(os.environ, _ENV),
        patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
        patch("boto3.client", return_value=mock_s3),
        patch("boto3.resource", return_value=mock_dynamo),
    ):
        sys.modules.pop("lambda_function", None)
        spec = importlib.util.spec_from_file_location("lambda_function", os.path.join(LAMBDA_DIR, "lambda_function.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, mock_s3, mock_table


class TestUnsupportedExtensionFailsHonestly:
    """An extension outside FORMAT_MAP fails the step, naming the format."""

    # p55 task 4.2: `pipeline-1/data.xlsx` was the first case here and is gone —
    # `xlsx` is now a FORMAT_MAP key. `.rtf` takes its slot so the case count is
    # unchanged and all three remain outside FORMAT_MAP.
    @pytest.mark.parametrize("key", ["pipeline-1/notes.rtf", "pipeline-1/report.pdf", "pipeline-1/archive.zip"])
    def test_unsupported_extension_raises_permanent_step_failure(self, key):
        handler, mock_s3, _ = _load_handler()
        mock_s3.head_object.return_value = {"ContentLength": 4096, "ETag": '"abc123"'}

        with pytest.raises(StepFailure) as exc_info:
            handler.lambda_handler({"source_bucket": "src", "source_key": key}, MagicMock())

        failure = exc_info.value
        assert failure.step_name == "file_validation"
        # Permanent, not transient: `stepRetry` in orchestration-stack.ts matches
        # TransientStepFailure only, and an unrecognised extension is
        # deterministic — retrying it three times buys nothing.
        assert failure.error_type == "permanent"

    def test_failure_message_names_the_unsupported_extension(self):
        """Regression guard: the message must not be a Python-internals leak."""
        handler, mock_s3, _ = _load_handler()
        mock_s3.head_object.return_value = {"ContentLength": 4096}

        with pytest.raises(StepFailure) as exc_info:
            handler.lambda_handler({"source_bucket": "src", "source_key": "p1/multi-page.pdf"}, MagicMock())

        message = exc_info.value.message
        assert "pdf" in message, f"the failure does not name the unsupported extension: {message!r}"
        assert "unsupported" in message.lower()
        # The exact string the deployed environment produced.
        assert "file_size_bytes" not in message, (
            f"the unbound-variable crash is back: {message!r} — the unsupported branch is falling "
            f"through to the success return again"
        )
        assert "local variable" not in message

    def test_unsupported_row_is_still_written_before_the_failure(self):
        """The rejection stays auditable — the `unsupported` row is not lost."""
        handler, mock_s3, mock_table = _load_handler()
        mock_s3.head_object.return_value = {"ContentLength": 4096}

        with pytest.raises(StepFailure):
            handler.lambda_handler({"source_bucket": "src", "source_key": "p1/data.pdf"}, MagicMock())

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["status"] == "unsupported"
        assert item["file_type"] == "pdf"
        assert "not recognized" in item["error_message"]

    def test_no_head_request_for_an_unsupported_extension(self):
        """Format detection is extension-only, so the object is never touched."""
        handler, mock_s3, _ = _load_handler()

        with pytest.raises(StepFailure):
            handler.lambda_handler({"source_bucket": "src", "source_key": "p1/data.pdf"}, MagicMock())

        mock_s3.head_object.assert_not_called()
        mock_s3.get_object.assert_not_called()


class TestEventWithNoRecordToValidate:
    """The sibling doorway onto the same unbound name."""

    def test_event_with_no_s3_record_raises_a_named_failure(self):
        handler, _, mock_table = _load_handler()

        with pytest.raises(StepFailure) as exc_info:
            handler.lambda_handler({"pipeline_id": "p1"}, MagicMock())

        message = exc_info.value.message
        assert exc_info.value.error_type == "permanent"
        assert "no S3 record" in message, f"unhelpful message for an empty event: {message!r}"
        assert "file_size_bytes" not in message
        # Nothing was validated, so nothing is recorded.
        mock_table.put_item.assert_not_called()

    def test_empty_records_list_raises_a_named_failure(self):
        handler, _, _ = _load_handler()

        with pytest.raises(StepFailure) as exc_info:
            handler.lambda_handler({"Records": []}, MagicMock())

        assert "no S3 record" in exc_info.value.message


class TestSupportedFormatStillReturnsTheFullContract:
    """Guard the fix did not disturb the path every real run takes.

    The keys asserted here are the ones the next state dereferences
    (`orchestration-stack.ts:329-334`); an absent one is a
    States.ParameterPathFailure that no Retry or Catch can intercept.
    """

    def test_recognised_extension_returns_the_downstream_keys(self):
        handler, mock_s3, _ = _load_handler()
        mock_s3.head_object.return_value = {"ContentLength": 2048, "ETag": '"deadbeef"'}

        result = handler.lambda_handler({"source_bucket": "src", "source_key": "p1/data.csv"}, MagicMock())

        assert result["statusCode"] == 200
        assert result["source_format"] == "csv"
        assert result["file_size_bytes"] == 2048
        for key in ("run_id", "original_pipeline_id", "source_format", "file_size_bytes", "retry_from_step"):
            assert key in result, f"{key} is absent from the file_validation result"
