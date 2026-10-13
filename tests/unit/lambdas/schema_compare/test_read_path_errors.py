# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: schema_compare read helpers classify infra errors (P28 R17.3).

``get_pipeline_metadata``, ``get_target_schema_from_s3``, and
``check_target_schema_changed`` previously swallowed every exception, so a
throttle produced "Pipeline metadata not found" / a false schema-version
change / a silent "not changed" verdict. Narrowed behavior:

- genuine not-found (missing item, NoSuchKey/NoSuchBucket) → None / (None, None)
- throttles / transient infra codes → TransientStepFailure (SFN Retry fires)
- access denied / other ClientErrors → StepFailure (permanent)
- check_target_schema_changed no longer swallows read errors

Validates: P28 Requirements 17.3
"""

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_schema_compare",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_mods = {
    "metrics_facade": mock.MagicMock(PipelineMetrics=None),
}


def _load_module():
    """Load the schema_compare lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "schema_compare_lf_p28_read_paths",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["schema_compare_lf_p28_read_paths"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()


def _client_error(code: str, operation: str = "GetItem") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, operation)


PIPELINE_ID = "pipe-read-path"


class TestGetPipelineMetadata:
    def test_throttle_raises_transient_step_failure(self):
        table = mock.MagicMock()
        table.get_item.side_effect = _client_error("ThrottlingException")
        with mock.patch.object(_lf, "pipelines_table", table), pytest.raises(_lf.StepFailure) as exc_info:
            _lf.get_pipeline_metadata(PIPELINE_ID)
        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert exc_info.value.error_type == "transient"
        assert exc_info.value.step_name == "schema_compare"
        assert exc_info.value.details["operation"] == "get_pipeline_metadata"

    def test_access_denied_raises_step_failure(self):
        table = mock.MagicMock()
        table.get_item.side_effect = _client_error("AccessDeniedException")
        with mock.patch.object(_lf, "pipelines_table", table), pytest.raises(_lf.StepFailure) as exc_info:
            _lf.get_pipeline_metadata(PIPELINE_ID)
        assert type(exc_info.value).__name__ == "StepFailure"

    def test_missing_item_returns_none(self):
        table = mock.MagicMock()
        table.get_item.return_value = {}
        with mock.patch.object(_lf, "pipelines_table", table):
            assert _lf.get_pipeline_metadata(PIPELINE_ID) is None


class TestGetTargetSchemaFromS3:
    def test_throttle_raises_transient_step_failure(self):
        s3 = mock.MagicMock()
        s3.get_object.side_effect = _client_error("ThrottlingException", "GetObject")
        with mock.patch.object(_lf, "s3_client", s3), pytest.raises(_lf.StepFailure) as exc_info:
            _lf.get_target_schema_from_s3("bucket", "key.json")
        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert exc_info.value.error_type == "transient"
        assert exc_info.value.details["operation"] == "get_target_schema_from_s3"

    def test_access_denied_raises_step_failure(self):
        s3 = mock.MagicMock()
        s3.get_object.side_effect = _client_error("AccessDenied", "GetObject")
        with mock.patch.object(_lf, "s3_client", s3), pytest.raises(_lf.StepFailure) as exc_info:
            _lf.get_target_schema_from_s3("bucket", "key.json")
        assert type(exc_info.value).__name__ == "StepFailure"

    def test_missing_object_returns_none_pair(self):
        s3 = mock.MagicMock()
        s3.get_object.side_effect = _client_error("NoSuchKey", "GetObject")
        with mock.patch.object(_lf, "s3_client", s3):
            assert _lf.get_target_schema_from_s3("bucket", "key.json") == (None, None)

    def test_valid_object_returns_schema_and_hash(self):
        s3 = mock.MagicMock()
        body = mock.MagicMock()
        body.read.return_value = b'{"type": "object"}'
        s3.get_object.return_value = {"Body": body}
        with mock.patch.object(_lf, "s3_client", s3):
            schema, schema_hash = _lf.get_target_schema_from_s3("bucket", "key.json")
        assert schema == {"type": "object"}
        assert isinstance(schema_hash, str) and len(schema_hash) == 64


class TestCheckTargetSchemaChanged:
    _METADATA = {
        "target_schema_s3_key": "schemas/target.json",
        "target_schema_bucket": "bucket",
        "target_schema_version": "abc123",
    }

    def test_throttle_propagates_instead_of_reporting_not_changed(self):
        """The old broad swallow returned (False, None, None) for a throttle."""
        s3 = mock.MagicMock()
        s3.get_object.side_effect = _client_error("ThrottlingException", "GetObject")
        with mock.patch.object(_lf, "s3_client", s3), pytest.raises(_lf.StepFailure) as exc_info:
            _lf.check_target_schema_changed(self._METADATA)
        assert type(exc_info.value).__name__ == "TransientStepFailure"

    def test_missing_metadata_still_returns_not_changed(self):
        assert _lf.check_target_schema_changed({}) == (False, None, None)

    def test_missing_object_registers_as_version_change(self):
        """Genuine not-found keeps the pre-existing contract: (None) != stored → changed."""
        s3 = mock.MagicMock()
        s3.get_object.side_effect = _client_error("NoSuchKey", "GetObject")
        with mock.patch.object(_lf, "s3_client", s3):
            changed, old, new = _lf.check_target_schema_changed(self._METADATA)
        assert changed is True
        assert old == "abc123"
        assert new is None
