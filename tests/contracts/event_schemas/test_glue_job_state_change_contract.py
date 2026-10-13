# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test for Glue Job State Change event format.

Validates that the Glue Job State Change fixture matches the JSON Schema
and that missing required fields cause validation failure.

Task 8.2 — Requirement 5.4
"""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from tests.contracts.event_schemas.schemas import GLUE_JOB_STATE_CHANGE_SCHEMA

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def glue_event() -> dict:
    return _load_fixture("events/glue_job_state_change.json")


class TestGlueJobStateChangeContract:
    """Glue Job State Change event matches the expected schema."""

    def test_fixture_validates_against_schema(self, glue_event):
        jsonschema.validate(glue_event, GLUE_JOB_STATE_CHANGE_SCHEMA)

    def test_detail_job_name_present(self, glue_event):
        assert "jobName" in glue_event["detail"]

    def test_detail_state_present(self, glue_event):
        assert "state" in glue_event["detail"]

    def test_detail_job_run_id_present(self, glue_event):
        assert "jobRunId" in glue_event["detail"]

    def test_missing_detail_fails(self, glue_event):
        event = copy.deepcopy(glue_event)
        del event["detail"]
        with pytest.raises(jsonschema.ValidationError, match="'detail'"):
            jsonschema.validate(event, GLUE_JOB_STATE_CHANGE_SCHEMA)

    def test_missing_detail_job_name_fails(self, glue_event):
        event = copy.deepcopy(glue_event)
        del event["detail"]["jobName"]
        with pytest.raises(jsonschema.ValidationError, match="'jobName'"):
            jsonschema.validate(event, GLUE_JOB_STATE_CHANGE_SCHEMA)

    def test_missing_detail_state_fails(self, glue_event):
        event = copy.deepcopy(glue_event)
        del event["detail"]["state"]
        with pytest.raises(jsonschema.ValidationError, match="'state'"):
            jsonschema.validate(event, GLUE_JOB_STATE_CHANGE_SCHEMA)

    def test_missing_detail_job_run_id_fails(self, glue_event):
        event = copy.deepcopy(glue_event)
        del event["detail"]["jobRunId"]
        with pytest.raises(jsonschema.ValidationError, match="'jobRunId'"):
            jsonschema.validate(event, GLUE_JOB_STATE_CHANGE_SCHEMA)

    def test_wrong_source_fails(self, glue_event):
        event = copy.deepcopy(glue_event)
        event["source"] = "aws.ec2"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(event, GLUE_JOB_STATE_CHANGE_SCHEMA)
