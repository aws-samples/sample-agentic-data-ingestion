# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: schema_mapping failure path raises StepFailure (P28 R1.1).

The handler must raise (so the Step Functions task fails and Retry/Catch
fire) instead of returning ``{"statusCode": 500, ...}`` which SFN would
treat as a successful task completion.

Validates: P28 Requirements 1.1
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir and shared dir to path
_schema_mapping_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_schema_mapping",
    )
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

# Stub heavy dependencies (mirrors test_duration_metrics.py)
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

sys.modules.pop("lambda_function", None)
try:
    import lambda_function as _mod
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def test_missing_run_id_raises_step_failure():
    """A failing handler raises StepFailure — never returns an error payload."""
    with (
        patch.object(_mod, "publish_pipeline_state_change"),
        pytest.raises(_mod.StepFailure) as exc_info,
    ):
        _mod.lambda_handler({}, None)

    exc = exc_info.value
    assert exc.step_name == "schema_mapping"
    assert exc.error_type == "permanent"
    assert "run_id is required" in exc.message
    assert isinstance(exc.__cause__, ValueError)


def test_failure_still_publishes_failed_state_event():
    """The failure path keeps its side effects (failure event published) before raising."""
    with (
        patch.object(_mod, "publish_pipeline_state_change") as mock_publish,
        pytest.raises(_mod.StepFailure),
    ):
        _mod.lambda_handler({"run_id": None}, None)

    statuses = [call.kwargs.get("status") for call in mock_publish.call_args_list]
    assert "failed" in statuses
