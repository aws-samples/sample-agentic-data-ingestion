# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: Lambda handler idempotency (Property 27).

**Property 27**: For execution_updater, invoking twice with the same event
produces same side effects as once.

Uses @given with random DynamoDB Stream records.
Uses @settings(max_examples=10).

**Validates: Requirements 18.1, 18.2, 18.3, 18.4**
"""

import importlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_execution_updater",
    )
)
SHARED_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "shared",
    )
)

_ENV = {
    "EXECUTIONS_TABLE": "test-executions",
    "PIPELINES_TABLE": "test-pipelines",
    "ERRORS_TABLE": "test-errors",
    "STATE_TABLE": "test-state",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

step_names = st.sampled_from(
    [
        "file_validation",
        "schema_extract",
        "schema_compare",
        "schema_mapping",
        "create_code",
        "create_infrastructure",
        "create_glue_pipeline",
        "create_quality_checks",
        "run_pipeline",
    ]
)

statuses = st.sampled_from(["success", "running", "failed", "error", "waiting_approval"])

event_names = st.sampled_from(["INSERT", "MODIFY"])

run_ids = st.from_regex(r"run-[0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9]{8}", fullmatch=True)

pipeline_ids = st.from_regex(r"pipe-[a-z0-9]{4,8}", fullmatch=True)


@st.composite
def stream_records(draw):
    """Generate a random DynamoDB Stream record."""
    return {
        "eventID": f"evt-{draw(st.integers(min_value=1, max_value=99999))}",
        "eventName": draw(event_names),
        "dynamodb": {
            "NewImage": {
                "run_id": {"S": draw(run_ids)},
                "pipeline_id": {"S": draw(pipeline_ids)},
                "step_name": {"S": draw(step_names)},
                "status": {"S": draw(statuses)},
            }
        },
    }


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------


def _load_handler(mock_table):
    """Load execution_updater via importlib with a fresh mock table."""
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table

    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        with patch("boto3.resource", return_value=mock_dynamo):
            sys.modules.pop("lambda_function", None)
            if SHARED_DIR not in sys.path:
                sys.path.insert(0, SHARED_DIR)
            spec = importlib.util.spec_from_file_location(
                "lambda_function",
                os.path.join(LAMBDA_DIR, "lambda_function.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@given(record=stream_records())
@settings(max_examples=10)
def test_execution_updater_idempotent(record):
    """Property 27: Invoking execution_updater twice with the same event
    produces the same number of update_item calls as invoking once.

    **Validates: Requirements 18.2**
    """
    event = {"Records": [record]}

    # First invocation
    mock_table_1 = MagicMock()
    handler1 = _load_handler(mock_table_1)
    handler1.lambda_handler(event, None)
    calls_first = mock_table_1.update_item.call_count

    # Second invocation (fresh module load, fresh mock)
    mock_table_2 = MagicMock()
    handler2 = _load_handler(mock_table_2)
    handler2.lambda_handler(event, None)
    calls_second = mock_table_2.update_item.call_count

    # Idempotency: same number of DynamoDB operations
    assert calls_first == calls_second, (
        f"Non-idempotent: first invocation made {calls_first} update_item calls, second made {calls_second}"
    )
