# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""record_failure publishes the failure half of the pipeline error rate.

Every caught pipeline failure reaches RecordFailure, GlueJobFailed included,
so this is the one place failures are counted; job_completion counts only
successes. Each failed run publishes one ``Pipeline.ErrorRate`` datapoint of 100
with dimension ``{Environment}``, the series PipelineErrorRateAlarm averages.
An ``ApprovalRejected`` run is a human decision and publishes nothing, the
rule the metrics dashboard already applies. A publish failure must not mask
the original failure: the handler still records it and returns.
"""

import os
import sys
from unittest.mock import MagicMock, patch

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_record_failure_dir = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_record_failure"
)
sys.path.insert(0, _record_failure_dir)
sys.modules.pop("lambda_function", None)
import lambda_function as _mod
import pytest


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod


def _event(error_name: str) -> dict:
    return {
        "state": {
            "pipeline_id": "orders-pipeline",
            "file_validation_result": {"run_id": "run-1", "original_pipeline_id": "orders-pipeline"},
            "error": {"Error": error_name, "Cause": "boom"},
        },
        "execution_name": "exec-1",
    }


def _run(event: dict, cloudwatch: MagicMock, environment: str = "prod") -> dict:
    table = MagicMock(name="state_table")
    with (
        patch.object(_mod, "state_table", table),
        patch.object(_mod.boto3, "client", return_value=cloudwatch) as client,
        patch.dict(os.environ, {"ENVIRONMENT": environment}),
    ):
        result = _mod.lambda_handler(event, None)
    for call in client.call_args_list:
        assert call.args[0] == "cloudwatch"
    return result


@pytest.mark.parametrize("error_name", ["GlueJobFailed", "States.Timeout", "StepFailure", "States.TaskFailed"])
def test_a_failed_run_publishes_one_datapoint_of_100(error_name):
    cloudwatch = MagicMock()

    result = _run(_event(error_name), cloudwatch)

    assert result["recorded"] is True
    cloudwatch.put_metric_data.assert_called_once_with(
        Namespace="AgenticDataIngestion/Pipeline",
        MetricData=[
            {
                "MetricName": "Pipeline.ErrorRate",
                "Dimensions": [{"Name": "Environment", "Value": "prod"}],
                "Value": 100.0,
                "Unit": "Percent",
            }
        ],
    )


def test_an_approval_rejection_is_not_a_failure():
    cloudwatch = MagicMock()

    result = _run(_event("ApprovalRejected"), cloudwatch)

    assert result["recorded"] is True, "the Failure_Record is still written"
    cloudwatch.put_metric_data.assert_not_called()


def test_a_publish_failure_does_not_mask_the_original_failure():
    cloudwatch = MagicMock()
    cloudwatch.put_metric_data.side_effect = RuntimeError("AccessDenied")

    result = _run(_event("GlueJobFailed"), cloudwatch)

    assert result["status"] == "FAILED"
    assert result["recorded"] is True
