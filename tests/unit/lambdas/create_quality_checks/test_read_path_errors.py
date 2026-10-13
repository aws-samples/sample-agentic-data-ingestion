# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_quality_checks read helper classifies infra errors (P28 R17.3).

``get_target_schema_for_quality_checks`` previously swallowed every
exception into {}, so a DynamoDB throttle silently degraded rule generation
to no-rules. Narrowed behavior:

- genuine not-found (missing item, ResourceNotFoundException) → {} (unchanged)
- throttles / transient infra codes → TransientStepFailure (SFN Retry fires)
- access denied / other ClientErrors → StepFailure (permanent)

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
    "agentic_data_ingestion_create_quality_checks",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


def _load_module():
    """Load the create_quality_checks lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_quality_checks_lf_p28_read_paths",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["create_quality_checks_lf_p28_read_paths"] = mod
    spec.loader.exec_module(mod)
    return mod


_lf = _load_module()


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetItem")


MAPPING_ID = "map-read-path"


def test_throttle_raises_transient_step_failure():
    table = mock.MagicMock()
    table.get_item.side_effect = _client_error("ProvisionedThroughputExceededException")
    with mock.patch.object(_lf, "mappings_table", table), pytest.raises(_lf.StepFailure) as exc_info:
        _lf.get_target_schema_for_quality_checks(MAPPING_ID)
    assert type(exc_info.value).__name__ == "TransientStepFailure"
    assert exc_info.value.error_type == "transient"
    assert exc_info.value.step_name == "create_quality_checks"
    assert exc_info.value.details["operation"] == "get_target_schema_for_quality_checks"


def test_access_denied_raises_step_failure():
    table = mock.MagicMock()
    table.get_item.side_effect = _client_error("AccessDeniedException")
    with mock.patch.object(_lf, "mappings_table", table), pytest.raises(_lf.StepFailure) as exc_info:
        _lf.get_target_schema_for_quality_checks(MAPPING_ID)
    assert type(exc_info.value).__name__ == "StepFailure"


def test_missing_item_returns_empty_dict():
    table = mock.MagicMock()
    table.get_item.return_value = {}
    with mock.patch.object(_lf, "mappings_table", table):
        assert _lf.get_target_schema_for_quality_checks(MAPPING_ID) == {}


def test_missing_mapping_id_returns_empty_dict_without_read():
    table = mock.MagicMock()
    with mock.patch.object(_lf, "mappings_table", table):
        assert _lf.get_target_schema_for_quality_checks("") == {}
    table.get_item.assert_not_called()
