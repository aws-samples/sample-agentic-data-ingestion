# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test for Step Functions task token event format.

Validates that the SFN task token fixture matches the JSON Schema
and that missing required fields cause validation failure.

Task 8.2 — Requirement 5.3
"""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from tests.contracts.event_schemas.schemas import SFN_TASK_TOKEN_SCHEMA

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sfn_event() -> dict:
    return _load_fixture("events/sfn_task_token.json")


class TestSfnTaskTokenContract:
    """SFN task token event matches the expected schema."""

    def test_fixture_validates_against_schema(self, sfn_event):
        jsonschema.validate(sfn_event, SFN_TASK_TOKEN_SCHEMA)

    def test_task_token_present(self, sfn_event):
        assert "TaskToken" in sfn_event
        assert len(sfn_event["TaskToken"]) > 0

    def test_input_pipeline_id_present(self, sfn_event):
        assert "pipeline_id" in sfn_event["Input"]

    def test_input_run_id_present(self, sfn_event):
        assert "run_id" in sfn_event["Input"]

    def test_missing_task_token_fails(self, sfn_event):
        event = copy.deepcopy(sfn_event)
        del event["TaskToken"]
        with pytest.raises(jsonschema.ValidationError, match="'TaskToken'"):
            jsonschema.validate(event, SFN_TASK_TOKEN_SCHEMA)

    def test_missing_input_fails(self, sfn_event):
        event = copy.deepcopy(sfn_event)
        del event["Input"]
        with pytest.raises(jsonschema.ValidationError, match="'Input'"):
            jsonschema.validate(event, SFN_TASK_TOKEN_SCHEMA)

    def test_missing_input_pipeline_id_fails(self, sfn_event):
        event = copy.deepcopy(sfn_event)
        del event["Input"]["pipeline_id"]
        with pytest.raises(jsonschema.ValidationError, match="'pipeline_id'"):
            jsonschema.validate(event, SFN_TASK_TOKEN_SCHEMA)

    def test_missing_input_run_id_fails(self, sfn_event):
        event = copy.deepcopy(sfn_event)
        del event["Input"]["run_id"]
        with pytest.raises(jsonschema.ValidationError, match="'run_id'"):
            jsonschema.validate(event, SFN_TASK_TOKEN_SCHEMA)

    def test_empty_task_token_fails(self, sfn_event):
        event = copy.deepcopy(sfn_event)
        event["TaskToken"] = ""
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(event, SFN_TASK_TOKEN_SCHEMA)
