# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The schedule rule's payload, from the dashboard API that writes it to the trigger that reads it.

PRODUCER: ``dashboard_api/handlers/pipelines_crud.create_pipeline``, which calls
``_manage_schedule_rule`` to put the rule's target ``Input``.

CONSUMER: ``pipeline_trigger/lambda_function.lambda_handler``, which EventBridge
invokes with that ``Input`` verbatim. It routes ``trigger == "scheduled"`` to
``_handle_scheduled_trigger``, which branches on ``source_type`` and defaults an
absent one to ``S3_UPLOAD``.

Why this file exists (2026-10-10). The producer wrote no ``source_type``, so
every JDBC schedule took the trigger's S3 branch, listed an upload prefix a JDBC
pipeline never uses, returned "No files to process" with a 200, and started
nothing. Each side had unit tests, and each side's tests fed it the shape that
side expected, so neither could see the other. Here the consumer is given the
exact string the producer put on the rule.

Both Lambdas are separate bundles. The trigger is loaded under its own module
name, with its AWS clients replaced, so it cannot collide with another
``lambda_function`` or reach AWS.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("eventbridge_utils", MagicMock())
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

if "audit_utils" not in sys.modules or not isinstance(sys.modules["audit_utils"], MagicMock):
    _mock_audit_utils = MagicMock()
    _mock_audit_utils.extract_request_context = MagicMock(return_value={})
    _mock_audit_utils.write_audit_entry = MagicMock()
    sys.modules["audit_utils"] = _mock_audit_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
for _path in (_REPO_ROOT / "shared", _REPO_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# `handlers.common` resolves table names through `require_env` at import. Stub it
# for the import only, then put back whatever was there, so the stub cannot leak
# into modules collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
_prev_env_utils = sys.modules.get("env_utils")
_stub_env_utils = MagicMock()
_stub_env_utils.require_env = lambda name, default=None: default or name
sys.modules["env_utils"] = _stub_env_utils
try:
    from handlers import pipelines_crud
finally:
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

TRIGGER_ARN = "arn:aws:lambda:us-west-2:123456789012:function:agentic_data_ingestion_pipeline_trigger"
SCHEDULE = "rate(1 hour)"
SOURCE_BUCKET = "adi-source-files-123456789012-us-west-2"


# ---------------------------------------------------------------------------
# The consumer, loaded on its own
# ---------------------------------------------------------------------------


@pytest.fixture
def trigger():
    """The real trigger module with every AWS client it touches replaced."""
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    sfn.start_execution.return_value = {"executionArn": "arn:aws:states:us-west-2:123456789012:execution:sm:x"}

    class _ExecutionAlreadyExists(Exception):
        pass

    sfn.exceptions.ExecutionAlreadyExists = _ExecutionAlreadyExists
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": []}

    def client(service, *args, **kwargs):
        return {"stepfunctions": sfn, "s3": s3}.get(service, MagicMock())

    path = _REPO_ROOT / "lambda" / "agentic_data_ingestion_pipeline_trigger" / "lambda_function.py"
    env = {
        "STATE_MACHINE_ARN": "arn:aws:states:us-west-2:123456789012:stateMachine:sm",
        "SOURCE_BUCKET": SOURCE_BUCKET,
        "MAX_CONCURRENT_EXECUTIONS": "10",
    }
    with patch.dict(os.environ, env), patch("boto3.client", side_effect=client), patch("boto3.resource"):
        spec = importlib.util.spec_from_file_location("_schedule_contract_pipeline_trigger", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.sfn_client = sfn
        module.locks_table = MagicMock(put_item=MagicMock(return_value={}))
        module.executions_table = MagicMock()
        yield module, sfn, s3


# ---------------------------------------------------------------------------
# The producer, through create_pipeline
# ---------------------------------------------------------------------------


def _rule_input_from_create(source_type: str) -> str:
    """Create a pipeline with a schedule; return the Input string put on its rule."""
    events = MagicMock()
    events.put_targets.return_value = {"FailedEntryCount": 0, "FailedEntries": []}
    body: dict = {
        "pipeline_name": f"Scheduled {source_type}",
        "target_schema": "uploaded_test",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "schedule": SCHEDULE,
        "source_type": source_type,
    }
    if source_type == "JDBC":
        body["source_config"] = {"glue_connection_name": "customer-conn", "engine": "postgres"}

    def require_env(name, default=None):
        return {"PIPELINE_TRIGGER_ARN": TRIGGER_ARN, "UPLOAD_BUCKET": SOURCE_BUCKET}.get(name, default or name)

    with (
        patch.object(pipelines_crud, "pipelines_table", MagicMock()),
        patch.object(pipelines_crud, "s3_client", MagicMock()),
        patch.object(pipelines_crud, "require_env", require_env),
        patch.object(pipelines_crud.boto3, "client", return_value=events),
        patch.object(pipelines_crud, "_provision_pipeline_resources"),
        # The JDBC source's own provisioning is not under test here; the
        # schedule payload it leads to is.
        patch.object(pipelines_crud, "_validate_jdbc_source_config"),
        patch.object(pipelines_crud, "_provision_jdbc_source_connection", return_value="customer-conn"),
        patch.object(pipelines_crud, "_provision_jdbc_runtime_crawler", return_value="crawler"),
    ):
        result = pipelines_crud.create_pipeline(json.dumps(body), event={})
    assert result["statusCode"] == 201, result["body"]
    (call,) = events.put_targets.call_args_list
    (target,) = call.kwargs["Targets"]
    assert target["Arn"] == TRIGGER_ARN
    return target["Input"]


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_a_jdbc_schedule_reaches_the_jdbc_branch(trigger):
    module, sfn, s3 = trigger
    rule_input = _rule_input_from_create("JDBC")

    response = module.lambda_handler(json.loads(rule_input), None)

    assert response["statusCode"] == 200, response
    s3.list_objects_v2.assert_not_called()
    sfn.start_execution.assert_called_once()
    execution_input = json.loads(sfn.start_execution.call_args.kwargs["input"])
    assert execution_input["source_type"] == "JDBC"
    assert execution_input["trigger"] == "scheduled"


def test_an_s3_upload_schedule_still_lists_the_upload_prefix(trigger):
    module, sfn, s3 = trigger
    rule_input = _rule_input_from_create("S3_UPLOAD")
    pipeline_id = json.loads(rule_input)["pipeline_id"]

    response = module.lambda_handler(json.loads(rule_input), None)

    s3.list_objects_v2.assert_called_once()
    assert s3.list_objects_v2.call_args.kwargs["Prefix"] == f"{pipeline_id}/"
    assert response == {"statusCode": 200, "body": "No files to process"}
    sfn.start_execution.assert_not_called()


def test_the_defect_shape_is_what_the_consumer_misroutes(trigger):
    """Pins the consumer's default, so the payload above is load-bearing: the
    same JDBC payload without `source_type` takes the S3 branch."""
    module, sfn, s3 = trigger
    without = {k: v for k, v in json.loads(_rule_input_from_create("JDBC")).items() if k != "source_type"}

    response = module.lambda_handler(without, None)

    assert response["body"] == "No files to process"
    sfn.start_execution.assert_not_called()
    s3.list_objects_v2.assert_called_once()
