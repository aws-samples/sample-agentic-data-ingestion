# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: test_code read helper classifies infra errors (P28 R17.3).

``get_pipeline_metadata`` previously swallowed every exception into {}, so a
DynamoDB throttle silently dropped approval config. Narrowed behavior:

- genuine not-found (missing item, ResourceNotFoundException) → ``None``
- throttles / transient infra codes → TransientStepFailure (SFN Retry fires)
- access denied / other ClientErrors → StepFailure (permanent)

**AMENDED by p33 task 4.3 (design D8), not rewritten.** The first clause used to
read "→ {} (unchanged)" and cited that as the P28 R17.3 contract. p33 D8 changes
exactly that value: ``{}`` cannot separate "no pipeline record" (p33 requirement
2.8 — a lookup problem, fail the step) from "the record exists and carries no
target schema" (requirement 2.6 — a legitimate state, report not verified),
because ``{}`` has no ``target_schema_s3_key`` either, so a missing record would
silently present as "no target schema". The three-way classification R17.3
established is untouched — only the not-found *value* moved, to ``None``, which
is the convention already used at ``schema_compare:351-366`` and
``schema_mapping:133-149``. The docstring is amended here rather than left
contradicting the code.

Validates: P28 Requirements 17.3; p33 Requirements 2.6, 2.8
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

# Add Lambda dir and shared dir to path
_test_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _test_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy dependencies before import (mirrors test_step_failure_raise.py)
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "code_interpreter_runner": MagicMock(CodeInterpreterTestRunner=MagicMock()),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "artifact_schema": MagicMock(create_output_data=MagicMock(return_value={})),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetItem")


def _patched_dynamodb(table: MagicMock) -> MagicMock:
    dynamodb = MagicMock()
    dynamodb.Table.return_value = table
    return dynamodb


PIPELINE_ID = "pipe-read-path"


def test_throttle_raises_transient_step_failure():
    table = MagicMock()
    table.get_item.side_effect = _client_error("ThrottlingException")
    with patch.object(_mod, "dynamodb", _patched_dynamodb(table)), pytest.raises(_mod.StepFailure) as exc_info:
        _mod.get_pipeline_metadata(PIPELINE_ID)
    assert type(exc_info.value).__name__ == "TransientStepFailure"
    assert exc_info.value.error_type == "transient"
    assert exc_info.value.step_name == "test_code"
    assert exc_info.value.details["operation"] == "get_pipeline_metadata"


def test_access_denied_raises_step_failure():
    table = MagicMock()
    table.get_item.side_effect = _client_error("AccessDeniedException")
    with patch.object(_mod, "dynamodb", _patched_dynamodb(table)), pytest.raises(_mod.StepFailure) as exc_info:
        _mod.get_pipeline_metadata(PIPELINE_ID)
    assert type(exc_info.value).__name__ == "StepFailure"


def test_missing_item_returns_none():
    table = MagicMock()
    table.get_item.return_value = {}
    with patch.object(_mod, "dynamodb", _patched_dynamodb(table)):
        # AMENDED by p33 task 4.3 (design D8, C11): this asserted `== {}`.
        # D8 separates "no pipeline record" (p33 requirement 2.8 — fail the
        # step) from "pipeline exists but has no target schema" (requirement
        # 2.6 — report not verified). `{}` cannot carry that distinction: it
        # has no `target_schema_s3_key`, so a missing record would silently
        # become "no target schema" and report `verified: False` — a run
        # reported as unverifiable when the real problem is a missing record,
        # which is requirement 2.8's exact prohibition. The test is renamed to
        # match the value; the P28 R17.3 classification it was written for is
        # unchanged and is still asserted by the two tests above.
        assert _mod.get_pipeline_metadata(PIPELINE_ID) is None
