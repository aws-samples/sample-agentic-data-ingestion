# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_infrastructure read helper classifies infra errors (P28 R17.3).

``get_pipeline_metadata`` previously swallowed every exception into {}, so a
DynamoDB throttle silently dropped the pipeline's target_config. Narrowed
behavior:

- genuine not-found (missing item, ResourceNotFoundException) → {} (unchanged)
- throttles / transient infra codes → TransientStepFailure (SFN Retry fires)
- access denied / other ClientErrors → StepFailure (permanent)
- the inner target_config S3 derivation stays best-effort (unchanged)

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
    "agentic_data_ingestion_create_infrastructure",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_mods = {
    "metrics_facade": mock.MagicMock(PipelineMetrics=None),
    "artifact_schema": mock.MagicMock(create_output_data=mock.MagicMock(return_value={})),
}


def _load_module():
    """Load the create_infrastructure lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_infrastructure_lf_p28_read_paths",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["create_infrastructure_lf_p28_read_paths"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetItem")


PIPELINE_ID = "pipe-read-path"


def test_throttle_raises_transient_step_failure():
    table = mock.MagicMock()
    table.get_item.side_effect = _client_error("ThrottlingException")
    with mock.patch.object(_lf, "pipelines_table", table), pytest.raises(_lf.StepFailure) as exc_info:
        _lf.get_pipeline_metadata(PIPELINE_ID)
    assert type(exc_info.value).__name__ == "TransientStepFailure"
    assert exc_info.value.error_type == "transient"
    assert exc_info.value.step_name == "create_infrastructure"
    assert exc_info.value.details["operation"] == "get_pipeline_metadata"


def test_access_denied_raises_step_failure():
    table = mock.MagicMock()
    table.get_item.side_effect = _client_error("AccessDeniedException")
    with mock.patch.object(_lf, "pipelines_table", table), pytest.raises(_lf.StepFailure) as exc_info:
        _lf.get_pipeline_metadata(PIPELINE_ID)
    assert type(exc_info.value).__name__ == "StepFailure"


def test_missing_item_returns_empty_dict():
    table = mock.MagicMock()
    table.get_item.return_value = {}
    with mock.patch.object(_lf, "pipelines_table", table):
        assert _lf.get_pipeline_metadata(PIPELINE_ID) == {}


def test_target_config_derivation_stays_best_effort():
    """The inner S3 derivation swallow is deliberate — metadata still returned."""
    table = mock.MagicMock()
    table.get_item.return_value = {
        "Item": {
            "pipeline_id": PIPELINE_ID,
            "target_schema_bucket": "bucket",
            "target_schema_s3_key": "schemas/t.json",
        }
    }
    s3 = mock.MagicMock()
    s3.get_object.side_effect = _client_error("ThrottlingException")
    with (
        mock.patch.object(_lf, "pipelines_table", table),
        mock.patch.object(_lf.boto3, "client", return_value=s3),
    ):
        metadata = _lf.get_pipeline_metadata(PIPELINE_ID)
    assert metadata["pipeline_id"] == PIPELINE_ID
    assert "target_config" not in metadata
