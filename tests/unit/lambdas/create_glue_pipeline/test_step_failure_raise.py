# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_glue_pipeline failure path raises StepFailure (P28 R1.1).

The handler must raise (so the Step Functions task fails and routes to
UpdateFailureState via addCatch) instead of returning an error payload that
SFN would treat as a successful task completion.

Validates: P28 Requirements 1.1
"""

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ["MCP_SIGV4_ENABLED"] = "false"
os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_mods = {
    "strands": mock.MagicMock(),
    "strands.agent": mock.MagicMock(),
    "strands.tools.mcp": mock.MagicMock(),
    "streamable_http_sigv4": mock.MagicMock(),
    "mcp": mock.MagicMock(),
    "mcp.client": mock.MagicMock(),
    "mcp.client.streamable_http": mock.MagicMock(),
    "schema_evolution": mock.MagicMock(),
    "metrics_facade": mock.MagicMock(PipelineMetrics=None),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock prompt"),
    ),
}


def _load_module():
    """Load the create_glue_pipeline lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_glue_pipeline_lf_p28",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["create_glue_pipeline_lf_p28"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()


def test_missing_required_ids_raises_step_failure():
    """A failing handler raises StepFailure — never returns an error payload."""
    with (
        mock.patch.object(_lf, "pipeline_runs_table", mock.MagicMock()),
        mock.patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
        pytest.raises(_lf.StepFailure) as exc_info,
    ):
        _lf.lambda_handler({}, None)

    exc = exc_info.value
    assert exc.step_name == "create_glue_pipeline"
    assert exc.error_type == "permanent"
    assert "run_id and pipeline_id are required" in exc.message
    assert isinstance(exc.__cause__, ValueError)
