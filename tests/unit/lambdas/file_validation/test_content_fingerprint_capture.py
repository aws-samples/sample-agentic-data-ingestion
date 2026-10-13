# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""file_validation captures the content fingerprint from the HEAD it already issues (p30 R9.1).

R9.1's fingerprint is "size + ETag from the existing HEAD". This Lambda already
HEADs the source object for its size check, and the same response carries the
ETag — so the fingerprint costs **no additional S3 call**, which the
``head_object.call_count`` assertions below pin.

The value is persisted on the run's identity ``file_validation`` state row for
the same reason ``original_run_id`` is: the row is keyed by the run_id this
handler generates, and ``job_completion`` resolves it by that run_id when the
Glue job reports SUCCEEDED — the only moment a "processed" claim is true. See
``tests/unit/lambdas/job_completion/test_processed_file_ledger_write.py``.

Validates: Requirements 9.1
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test_table")

# Single-part ETag as boto3 returns it from head_object: QUOTED.
_ETAG = "d41d8cd98f00b204e9800998ecf8427e"


def _s3_event(bucket="src-bucket", key="orders/data.csv"):
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "s3": {"bucket": {"name": bucket}, "object": {"key": key}},
            }
        ]
    }


@pytest.fixture()
def lambda_env():
    """Load file_validation with mocked AWS clients, capturing store_metadata kwargs."""
    mock_s3 = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table
    mock_s3.head_object.return_value = {"ContentLength": 1024, "ETag": f'"{_ETAG}"'}
    mock_table.put_item.return_value = {}

    captured: list[dict] = []

    def _fake_store(**kwargs):
        captured.append(kwargs)
        return ("run-1", "orders")

    _lambda_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_file_validation"
        )
    )
    sys.path.insert(0, _lambda_dir)

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3
    mock_boto3.resource.return_value = mock_dynamodb

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        sys.modules.pop("lambda_function", None)
        spec = importlib.util.spec_from_file_location(
            "lambda_function", os.path.join(_lambda_dir, "lambda_function.py")
        )
        lf = importlib.util.module_from_spec(spec)
        sys.modules["lambda_function"] = lf
        spec.loader.exec_module(lf)
        lf.s3_client = mock_s3
        lf.table = mock_table
        # The handler tests need the call intercepted; the persistence tests need
        # the real writer, so keep a handle on it rather than reloading the module.
        lf._real_store_metadata = lf.store_metadata_in_dynamodb
        lf.store_metadata_in_dynamodb = _fake_store
        yield lf, mock_s3, captured

    sys.modules.pop("lambda_function", None)
    sys.path.remove(_lambda_dir)


class TestFingerprintFromTheExistingHead:
    def test_fingerprint_is_derived_and_passed_through(self, lambda_env):
        lf, _s3, captured = lambda_env

        lf.lambda_handler(_s3_event(), None)

        assert captured, "store_metadata_in_dynamodb was not called"
        assert captured[0]["content_fingerprint_value"] == lf.content_fingerprint(1024, _ETAG)

    def test_no_additional_s3_call_is_made_for_the_fingerprint(self, lambda_env):
        """The point of taking it from the existing HEAD."""
        lf, s3, _captured = lambda_env

        lf.lambda_handler(_s3_event(), None)

        assert s3.head_object.call_count == 1
        s3.get_object.assert_not_called()

    def test_quoted_head_etag_matches_the_bare_event_form(self, lambda_env):
        """The cross-Lambda equivalence the ledger depends on: the trigger reads
        a BARE ETag off the EventBridge detail, this handler reads a QUOTED one
        from boto3. Both must produce one fingerprint or the ledger never
        matches and dedup silently stops working."""
        lf, _s3, captured = lambda_env

        lf.lambda_handler(_s3_event(), None)

        assert captured[0]["content_fingerprint_value"] == lf.content_fingerprint(1024, _ETAG)

    def test_missing_etag_yields_no_fingerprint_and_does_not_fail_validation(self, lambda_env):
        """A fingerprint is a nice-to-have. An otherwise-good file must still
        validate — no fingerprint simply means no future dedup."""
        lf, s3, captured = lambda_env
        s3.head_object.return_value = {"ContentLength": 1024}

        result = lf.lambda_handler(_s3_event(), None)

        assert result["statusCode"] == 200
        assert captured[0]["content_fingerprint_value"] is None


class TestStateRowPersistence:
    """The identity row is the carrier job_completion reads back."""

    def test_fingerprint_is_written_to_the_state_row(self, lambda_env):
        lf, _s3, _captured = lambda_env
        table = MagicMock()
        lf.table = table

        lf._real_store_metadata(
            original_s3_uri="s3://src-bucket/orders/data.csv",
            json_s3_uri=None,
            file_type="csv",
            status="success",
            pipeline_id_override="orders",
            content_fingerprint_value="f" * 64,
        )

        item = table.put_item.call_args.kwargs["Item"]
        assert item["content_fingerprint"] == "f" * 64
        assert item["step_name"] == "file_validation"

    def test_absent_fingerprint_leaves_the_attribute_off_the_row(self, lambda_env):
        """Follows the existing optional-attribute convention on this row
        (``triggered_by``, ``original_run_id``): written only when there is a
        value, so job_completion's ``item_str`` read is the sole check needed."""
        lf, _s3, _captured = lambda_env
        table = MagicMock()
        lf.table = table

        lf._real_store_metadata(
            original_s3_uri="s3://src-bucket/orders/data.csv",
            json_s3_uri=None,
            file_type="csv",
            status="success",
            pipeline_id_override="orders",
        )

        assert "content_fingerprint" not in table.put_item.call_args.kwargs["Item"]
