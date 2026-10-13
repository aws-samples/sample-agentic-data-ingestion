# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: pipeline trigger deduplication (P9).

For any pipeline receiving duplicate file events, at most one SFN execution
is started. Tests the deduplication logic in the pipeline trigger Lambda.

**Validates: Requirements 6.3**
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_LAMBDA_DIR = Path(__file__).resolve().parents[3] / "lambda"
_SHARED_DIR = Path(__file__).resolve().parents[3] / "shared"


def _make_sqs_event(source_bucket: str, source_key: str) -> dict:
    """Build an SQS event wrapping an EventBridge S3 Object Created event."""
    eb_event = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": source_bucket},
            "object": {"key": source_key},
        },
    }
    return {
        "Records": [
            # messageId as real SQS sends it: p30 R9.3 reports a blocked record
            # through batchItemFailures keyed on it instead of raising.
            {"messageId": "msg-1", "body": json.dumps(eb_event)},
        ]
    }


# Strategy: generate S3 keys with a pipeline prefix and filename
_s3_key_strategy = st.tuples(
    st.from_regex(r"[a-z][a-z0-9\-]{2,20}", fullmatch=True),  # pipeline prefix
    st.from_regex(r"[a-z][a-z0-9_]{1,15}\.(csv|json|parquet)", fullmatch=True),  # filename
).map(lambda t: f"{t[0]}/{t[1]}")


@given(source_key=_s3_key_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_duplicate_events_start_at_most_one_execution(source_key):
    """For any pipeline receiving duplicate file events, at most one SFN execution is started.

    **Validates: Requirements 6.3**

    Strategy: Generate random S3 keys, send the same event twice through the
    trigger logic. The first call sees no running executions and starts one.
    The second call sees a running execution for the same pipeline prefix
    and raises (defers to SQS retry).
    """
    source_bucket = "test-source-bucket"
    pipeline_prefix = source_key.rsplit("/", 1)[0] if "/" in source_key else source_key

    # Track start_execution calls
    start_execution_calls = []

    # State: after first successful invocation, simulate a running execution
    invocation_count = [0]

    mock_sfn = MagicMock()

    def mock_list_executions(**kwargs):
        """After first invocation succeeds, report a running execution."""
        if invocation_count[0] == 0:
            return {"executions": []}
        else:
            return {"executions": [{"executionArn": "arn:aws:states:us-east-1:123:execution:test:run-1"}]}

    mock_sfn.list_executions = mock_list_executions

    def mock_get_paginator(name):
        paginator = MagicMock()
        if invocation_count[0] == 0:
            paginator.paginate.return_value = [{"executions": []}]
        else:
            paginator.paginate.return_value = [
                {"executions": [{"executionArn": "arn:aws:states:us-east-1:123:execution:test:run-1"}]}
            ]
        return paginator

    mock_sfn.get_paginator = mock_get_paginator

    def mock_describe_execution(**kwargs):
        return {"input": json.dumps({"source_key": source_key})}

    mock_sfn.describe_execution = mock_describe_execution

    def mock_start_execution(**kwargs):
        start_execution_calls.append(kwargs)
        invocation_count[0] += 1  # Mark that an execution is now running
        return {"executionArn": f"arn:aws:states:us-east-1:123:execution:test:{kwargs.get('name', 'unknown')}"}

    mock_sfn.start_execution = mock_start_execution
    mock_sfn.exceptions = MagicMock()
    mock_sfn.exceptions.ExecutionAlreadyExists = type("ExecutionAlreadyExists", (Exception,), {})

    # Mock DynamoDB for queued file tracking
    mock_table = MagicMock()

    # Mock the pipeline locks table. The handler dedups via a conditional
    # put_item on agentic_data_ingestion_pipeline_locks (attribute_not_exists(pipeline_prefix)):
    # the first put for a prefix succeeds, a second raises
    # ConditionalCheckFailedException. Model exactly that so the property holds
    # regardless of the (mocked) SFN state.
    from botocore.exceptions import ClientError

    _held_locks: set[str] = set()

    def mock_lock_put(**kwargs):
        prefix = kwargs.get("Item", {}).get("pipeline_prefix")
        cond = kwargs.get("ConditionExpression", "")
        if "attribute_not_exists" in str(cond) and prefix in _held_locks:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "lock held"}},
                "PutItem",
            )
        _held_locks.add(prefix)
        return {}

    mock_locks_table = MagicMock()
    mock_locks_table.put_item = mock_lock_put

    # Set env vars BEFORE importing the trigger module (it reads them at import time)
    os.environ["STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123:stateMachine:test"
    os.environ["MAX_CONCURRENT_EXECUTIONS"] = "10"
    os.environ["STATE_TABLE"] = "agentic_data_ingestion_state"
    os.environ["EXECUTIONS_TABLE"] = "agentic_data_ingestion_executions"
    os.environ["LOCKS_TABLE"] = "agentic_data_ingestion_pipeline_locks"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_REGION"] = "us-east-1"
    os.environ["AGENTIC_DATA_INGESTION_ENV"] = "test"

    # Import the trigger module fresh
    trigger_path = _LAMBDA_DIR / "agentic_data_ingestion_pipeline_trigger" / "lambda_function.py"
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))

    import importlib.util

    sys.modules.pop("lambda_function", None)
    for mod_name in list(sys.modules.keys()):
        if "pipeline_trigger" in mod_name:
            del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location("trigger_module", trigger_path)
    trigger_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trigger_mod)

    # Patch the module's clients
    trigger_mod.sfn_client = mock_sfn
    trigger_mod.executions_table = mock_table
    trigger_mod.locks_table = mock_locks_table

    event = _make_sqs_event(source_bucket, source_key)

    # First invocation — should succeed and start an execution
    trigger_mod.lambda_handler(event, None)
    assert len(start_execution_calls) == 1, (
        f"Expected exactly 1 start_execution call on first invocation, got {len(start_execution_calls)}"
    )

    # Second invocation — dedup gate blocks and returns the record to the queue
    # as a partial batch failure (p30 R9.3), rather than raising.
    second = trigger_mod.lambda_handler(event, None)
    assert second["batchItemFailures"] == [{"itemIdentifier": "msg-1"}]

    # Total should still be 1 (second was blocked by dedup gate)
    assert len(start_execution_calls) == 1, (
        f"Deduplication failed: {len(start_execution_calls)} executions started "
        f"for duplicate events with key '{source_key}'"
    )
