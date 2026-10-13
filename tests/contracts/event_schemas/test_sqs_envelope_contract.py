# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test for SQS message envelope format.

Validates that the SQS message envelope fixture matches the JSON Schema
and that missing required fields cause validation failure.

Task 8.2 — Requirement 5.2
"""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from tests.contracts.event_schemas.schemas import SQS_MESSAGE_ENVELOPE_SCHEMA

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sqs_event() -> dict:
    return _load_fixture("events/sqs_message_envelope.json")


class TestSqsEnvelopeContract:
    """SQS message envelope matches the expected schema."""

    def test_fixture_validates_against_schema(self, sqs_event):
        jsonschema.validate(sqs_event, SQS_MESSAGE_ENVELOPE_SCHEMA)

    def test_records_body_present(self, sqs_event):
        assert "body" in sqs_event["Records"][0]

    def test_records_message_id_present(self, sqs_event):
        assert "messageId" in sqs_event["Records"][0]

    def test_records_event_source_present(self, sqs_event):
        assert "eventSource" in sqs_event["Records"][0]

    def test_missing_records_fails(self, sqs_event):
        event = copy.deepcopy(sqs_event)
        del event["Records"]
        with pytest.raises(jsonschema.ValidationError, match="'Records'"):
            jsonschema.validate(event, SQS_MESSAGE_ENVELOPE_SCHEMA)

    def test_missing_body_fails(self, sqs_event):
        event = copy.deepcopy(sqs_event)
        del event["Records"][0]["body"]
        with pytest.raises(jsonschema.ValidationError, match="'body'"):
            jsonschema.validate(event, SQS_MESSAGE_ENVELOPE_SCHEMA)

    def test_missing_message_id_fails(self, sqs_event):
        event = copy.deepcopy(sqs_event)
        del event["Records"][0]["messageId"]
        with pytest.raises(jsonschema.ValidationError, match="'messageId'"):
            jsonschema.validate(event, SQS_MESSAGE_ENVELOPE_SCHEMA)

    def test_missing_event_source_fails(self, sqs_event):
        event = copy.deepcopy(sqs_event)
        del event["Records"][0]["eventSource"]
        with pytest.raises(jsonschema.ValidationError, match="'eventSource'"):
            jsonschema.validate(event, SQS_MESSAGE_ENVELOPE_SCHEMA)

    def test_empty_records_array_fails(self, sqs_event):
        event = {"Records": []}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(event, SQS_MESSAGE_ENVELOPE_SCHEMA)
