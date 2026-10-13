# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test for EventBridge execution update event format.

Validates that the EventBridge event fixture matches the JSON Schema
and that missing required fields cause validation failure.

Task 8.2 — Requirement 5.4
"""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from tests.contracts.event_schemas.schemas import EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def eb_event() -> dict:
    return _load_fixture("events/eventbridge_execution_update.json")


class TestEventBridgeContract:
    """EventBridge execution update event matches the expected schema."""

    def test_fixture_validates_against_schema(self, eb_event):
        jsonschema.validate(eb_event, EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA)

    def test_detail_pipeline_id_present(self, eb_event):
        assert "pipeline_id" in eb_event["detail"]

    def test_detail_run_id_present(self, eb_event):
        assert "run_id" in eb_event["detail"]

    def test_detail_status_present(self, eb_event):
        assert "status" in eb_event["detail"]

    def test_detail_current_step_present(self, eb_event):
        assert "current_step" in eb_event["detail"]

    def test_missing_detail_fails(self, eb_event):
        event = copy.deepcopy(eb_event)
        del event["detail"]
        with pytest.raises(jsonschema.ValidationError, match="'detail'"):
            jsonschema.validate(event, EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA)

    def test_missing_detail_pipeline_id_fails(self, eb_event):
        event = copy.deepcopy(eb_event)
        del event["detail"]["pipeline_id"]
        with pytest.raises(jsonschema.ValidationError, match="'pipeline_id'"):
            jsonschema.validate(event, EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA)

    def test_missing_detail_run_id_fails(self, eb_event):
        event = copy.deepcopy(eb_event)
        del event["detail"]["run_id"]
        with pytest.raises(jsonschema.ValidationError, match="'run_id'"):
            jsonschema.validate(event, EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA)

    def test_missing_detail_status_fails(self, eb_event):
        event = copy.deepcopy(eb_event)
        del event["detail"]["status"]
        with pytest.raises(jsonschema.ValidationError, match="'status'"):
            jsonschema.validate(event, EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA)

    def test_missing_detail_current_step_fails(self, eb_event):
        event = copy.deepcopy(eb_event)
        del event["detail"]["current_step"]
        with pytest.raises(jsonschema.ValidationError, match="'current_step'"):
            jsonschema.validate(event, EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA)
