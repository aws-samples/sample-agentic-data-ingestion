# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``edit_pipeline`` on an id that names no pipeline returns 404 and writes nothing.

DynamoDB ``UpdateItem`` creates the item when its key is absent. ``edit_pipeline``
issued every write without a condition, so a name-only edit to a stale or
just-deleted id created a partial record (``pipeline_name`` and ``updated_at``,
with no source, target or status) and returned 200 "Pipeline updated
successfully". Every write now carries ``attribute_exists(pipeline_id)``.

These tests run the handler against the shared moto tables
(``tests/fixtures/conftest.py``), so the condition is evaluated by DynamoDB's
own semantics rather than asserted as a keyword argument. Expectations come
from the requirement: an edit never creates a pipeline.
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
MISSING_ID = "no-such-pipeline-12345678"
EXISTING_ID = "existing-pipeline-87654321"


@pytest.fixture
def pipelines_table(mock_dynamodb_tables, monkeypatch):
    table = mock_dynamodb_tables[PIPELINES_TABLE]
    monkeypatch.setattr(pipelines, "pipelines_table", table)
    table.put_item(
        Item={
            "pipeline_id": EXISTING_ID,
            "pipeline_name": "existing",
            "status": "active",
            "source_type": "S3_UPLOAD",
            "target_config": {"type": "NEW_S3_TABLE_BUCKET", "write_mode": "append"},
        }
    )
    return table


def _edit(pipeline_id: str, body: dict) -> dict:
    return pipelines.edit_pipeline(pipeline_id, json.dumps(body), event={})


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"pipeline_name": "renamed"}, id="name-only"),
        pytest.param({"description": "x"}, id="description-only"),
        pytest.param({"target_config": {"write_mode": "overwrite"}}, id="target-config-map-seed"),
        pytest.param({"approval_config": {"schema_mapping_enabled": True}}, id="approval-config-map-seed"),
        pytest.param({"quality_config": {"dedup_enabled": False}}, id="quality-config-map-seed"),
    ],
)
def test_an_edit_to_a_missing_id_is_a_404_and_creates_nothing(pipelines_table, body):
    result = _edit(MISSING_ID, body)

    assert result["statusCode"] == 404, result["body"]
    assert MISSING_ID in json.loads(result["body"])["error"]
    assert "Item" not in pipelines_table.get_item(Key={"pipeline_id": MISSING_ID}), (
        "UpdateItem must not have created a partial record"
    )


def test_a_schedule_edit_to_a_missing_id_creates_no_rule(pipelines_table, monkeypatch):
    """The schedule path reads the record first, and refuses before the rule
    exists rather than after the record write fails."""
    manage = MagicMock()
    monkeypatch.setattr(pipelines, "_manage_schedule_rule", manage)

    result = _edit(MISSING_ID, {"schedule": "rate(1 hour)"})

    assert result["statusCode"] == 404
    manage.assert_not_called()
    assert "Item" not in pipelines_table.get_item(Key={"pipeline_id": MISSING_ID})


def test_an_edit_to_an_existing_pipeline_still_succeeds(pipelines_table):
    result = _edit(EXISTING_ID, {"pipeline_name": "renamed", "target_config": {"write_mode": "overwrite"}})

    assert result["statusCode"] == 200, result["body"]
    item = pipelines_table.get_item(Key={"pipeline_id": EXISTING_ID})["Item"]
    assert item["pipeline_name"] == "renamed"
    assert item["target_config"] == {"type": "NEW_S3_TABLE_BUCKET", "write_mode": "overwrite"}
    assert item["status"] == "active"
