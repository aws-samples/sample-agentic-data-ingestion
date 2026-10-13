# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``target_config.partition_columns`` is refused at create and edit unless every entry is a column name.

Code generation emits the stored value into the Glue job's ``partitionBy(...)``.
Neither endpoint looked at it before 2026-10-10, and the edit endpoint stores a
submitted ``target_config`` key verbatim, so ``__import__("os").getcwd()`` could
be stored and then rendered as a call. Code generation now refuses it too
(``test_config_render_injection.py``); these tests cover the API boundary, where
the user gets a 400 instead of a pipeline that fails at its first run.

Expectations come from the requirement: each entry passes the
Identifier_Allowlist, and the stored form is the comma-separated string the
``--partition_columns`` Glue job argument carries.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"),
)

try:
    from handlers import pipelines_crud as pipelines
finally:
    # A stub left in sys.modules leaks into every module collected after this
    # one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
PIPELINE_ID = "partitioned-pipeline-1234abcd"

HOSTILE = [
    pytest.param('__import__("os").getcwd()', id="python-call"),
    pytest.param('region"); import os; ("', id="quote-break-out"),
    pytest.param(["region", "dt)"], id="list-entry-with-paren"),
    pytest.param("region,,dt", id="empty-entry"),
    pytest.param([1], id="non-string-entry"),
    pytest.param({"a": "b"}, id="mapping"),
]


@pytest.mark.parametrize(
    ("submitted", "stored"),
    [
        pytest.param("region,dt", "region,dt", id="comma-string"),
        pytest.param(" region , dt ", "region,dt", id="spaces-around-entries"),
        pytest.param(["region", "dt"], "region,dt", id="list"),
        pytest.param([], "", id="empty-list"),
        pytest.param("", "", id="empty-string"),
        pytest.param(None, "", id="null"),
    ],
)
def test_accepted_values_are_stored_as_the_comma_string(submitted, stored):
    assert pipelines._normalised_partition_columns(submitted) == stored


@pytest.mark.parametrize("submitted", HOSTILE)
def test_a_value_that_is_not_column_names_is_refused(submitted):
    with pytest.raises(ValueError, match=r"target_config\.partition_columns"):
        pipelines._normalised_partition_columns(submitted)


@pytest.fixture
def pipelines_table(mock_dynamodb_tables, monkeypatch):
    table = mock_dynamodb_tables[PIPELINES_TABLE]
    monkeypatch.setattr(pipelines, "pipelines_table", table)
    table.put_item(
        Item={
            "pipeline_id": PIPELINE_ID,
            "pipeline_name": "partitioned",
            "status": "active",
            "source_type": "S3_UPLOAD",
            "target_config": {"type": "NEW_S3_TABLE_BUCKET", "write_mode": "append", "partition_columns": "region"},
        }
    )
    return table


def _edit(body: dict) -> dict:
    return pipelines.edit_pipeline(PIPELINE_ID, json.dumps(body), event={})


@pytest.mark.parametrize("submitted", HOSTILE)
def test_edit_refuses_the_value_and_leaves_the_record_unchanged(pipelines_table, submitted):
    result = _edit({"target_config": {"partition_columns": submitted}})

    assert result["statusCode"] == 400, result["body"]
    assert "partition_columns" in json.loads(result["body"])["error"]
    stored = pipelines_table.get_item(Key={"pipeline_id": PIPELINE_ID})["Item"]["target_config"]
    assert stored["partition_columns"] == "region"


def test_edit_stores_a_list_as_the_comma_string(pipelines_table):
    result = _edit({"target_config": {"partition_columns": ["region", "dt"]}})

    assert result["statusCode"] == 200, result["body"]
    stored = pipelines_table.get_item(Key={"pipeline_id": PIPELINE_ID})["Item"]["target_config"]
    assert stored["partition_columns"] == "region,dt"
    assert stored["write_mode"] == "append", "the per-key merge must keep the other stored keys"


@pytest.mark.parametrize("submitted", HOSTILE)
def test_create_refuses_the_value(mock_dynamodb_tables, monkeypatch, submitted):
    monkeypatch.setattr(pipelines, "pipelines_table", mock_dynamodb_tables[PIPELINES_TABLE])
    body = {
        "pipeline_name": "partition probe",
        "source_type": "S3_UPLOAD",
        "target_schema": "customers-schema",
        "ai_model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "target_config": {
            "type": "NEW_S3_TABLE_BUCKET",
            "bucket_name": "analytics-tables",
            "namespace": "sales",
            "table_name": "orders",
            "write_mode": "append",
            "partition_columns": submitted,
        },
    }

    result = pipelines.create_pipeline(json.dumps(body), event={})

    assert result["statusCode"] == 400, result["body"]
    assert "partition_columns" in json.loads(result["body"])["error"]
    assert mock_dynamodb_tables[PIPELINES_TABLE].scan()["Items"] == [], "a refused create writes no record"
