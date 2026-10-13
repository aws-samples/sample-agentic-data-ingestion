# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: event schema contract conformance (Property 8).

**Property 8**: For any internal event type, the event payload validates
against the event's JSON Schema without errors.

Uses @given with hypothesis strategies to generate random event payloads
that conform to the expected structure, then validates them against the
corresponding JSON Schema.

**Validates: Requirements 5.1, 5.2, 5.3, 5.4**
"""

import json
from pathlib import Path

import jsonschema
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.contracts.event_schemas.schemas import (
    EVENT_SCHEMAS,
    EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA,
    GLUE_JOB_STATE_CHANGE_SCHEMA,
    S3_OBJECT_CREATED_SCHEMA,
    SFN_TASK_TOKEN_SCHEMA,
    SQS_MESSAGE_ENVELOPE_SCHEMA,
    WS_MESSAGE_SCHEMA,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Hypothesis strategies for generating valid event payloads
# ---------------------------------------------------------------------------

_non_empty_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P"), min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=50,
)

_uuid_text = st.from_regex(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", fullmatch=True)

_iso_timestamp = st.from_regex(r"2024-0[1-9]-[012][0-9]T[012][0-9]:[0-5][0-9]:[0-5][0-9]Z", fullmatch=True)


@st.composite
def s3_object_created_events(draw):
    """Generate valid S3 Object Created events."""
    return {
        "version": "0",
        "id": draw(_uuid_text),
        "detail-type": "Object Created",
        "source": "aws.s3",
        "account": "123456789012",
        "time": draw(_iso_timestamp),
        "region": "us-east-1",
        "resources": [f"arn:aws:s3:::{draw(_non_empty_text)}"],
        "detail": {
            "version": "0",
            "bucket": {"name": draw(_non_empty_text)},
            "object": {
                "key": draw(_non_empty_text),
                "size": draw(st.integers(min_value=0, max_value=5_000_000_000)),
                "etag": draw(_non_empty_text),
                "sequencer": draw(_non_empty_text),
            },
            "request-id": draw(_non_empty_text),
            "requester": "123456789012",
            "source-ip-address": "10.0.0.1",
            "reason": "PutObject",
        },
    }


@st.composite
def sqs_message_envelope_events(draw):
    """Generate valid SQS message envelope events."""
    return {
        "Records": [
            {
                "messageId": draw(_uuid_text),
                "receiptHandle": draw(_non_empty_text),
                "body": draw(_non_empty_text),
                "attributes": {},
                "messageAttributes": {},
                "md5OfBody": draw(_non_empty_text),
                "eventSource": "aws:sqs",
                "eventSourceARN": f"arn:aws:sqs:us-east-1:123456789012:{draw(_non_empty_text)}",
                "awsRegion": "us-east-1",
            }
        ],
    }


@st.composite
def sfn_task_token_events(draw):
    """Generate valid SFN task token events."""
    return {
        "TaskToken": draw(
            st.text(min_size=1, max_size=100, alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
        ),
        "Input": {
            "pipeline_id": draw(_non_empty_text),
            "run_id": draw(_non_empty_text),
            "approval_type": "schema_mapping",
            "step_name": "SchemaMapping",
        },
    }


@st.composite
def eventbridge_execution_update_events(draw):
    """Generate valid EventBridge execution update events."""
    return {
        "version": "0",
        "id": draw(_uuid_text),
        "detail-type": "AgenticDataIngestion Execution Update",
        "source": "agentic_data_ingestion.execution-updater",
        "account": "123456789012",
        "time": draw(_iso_timestamp),
        "region": "us-east-1",
        "resources": [],
        "detail": {
            "pipeline_id": draw(_non_empty_text),
            "run_id": draw(_non_empty_text),
            "status": draw(st.sampled_from(["RUNNING", "SUCCEEDED", "FAILED", "WAITING_APPROVAL"])),
            "current_step": draw(_non_empty_text),
            "started_at": draw(_iso_timestamp),
            "updated_at": draw(_iso_timestamp),
        },
    }


@st.composite
def glue_job_state_change_events(draw):
    """Generate valid Glue Job State Change events."""
    return {
        "version": "0",
        "id": draw(_uuid_text),
        "detail-type": "Glue Job State Change",
        "source": "aws.glue",
        "account": "123456789012",
        "time": draw(_iso_timestamp),
        "region": "us-east-1",
        "resources": [],
        "detail": {
            "jobName": draw(_non_empty_text),
            "severity": "INFO",
            "state": draw(st.sampled_from(["SUCCEEDED", "FAILED", "STOPPED", "RUNNING"])),
            "jobRunId": f"jr_{draw(_non_empty_text)}",
            "message": draw(_non_empty_text),
        },
    }


@st.composite
def ws_message_events(draw):
    """Generate valid WebSocket message events."""
    return {
        "type": "execution_update",
        "pipeline_id": draw(_non_empty_text),
        "run_id": draw(_non_empty_text),
        "status": draw(st.sampled_from(["running", "completed", "failed", "waiting_approval"])),
        "current_step": draw(_non_empty_text),
        "updated_at": draw(_iso_timestamp),
    }


# Map event type names to their strategies
EVENT_STRATEGIES = {
    "s3_object_created": s3_object_created_events(),
    "sqs_message_envelope": sqs_message_envelope_events(),
    "sfn_task_token": sfn_task_token_events(),
    "eventbridge_execution_update": eventbridge_execution_update_events(),
    "glue_job_state_change": glue_job_state_change_events(),
    "ws_message": ws_message_events(),
}


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestEventSchemaConformanceProperty:
    """**Validates: Requirements 5.1, 5.2, 5.3, 5.4**"""

    @given(event=s3_object_created_events())
    @settings(max_examples=10)
    def test_s3_object_created_conforms(self, event):
        """Generated S3 Object Created events validate against schema."""
        jsonschema.validate(event, S3_OBJECT_CREATED_SCHEMA)

    @given(event=sqs_message_envelope_events())
    @settings(max_examples=10)
    def test_sqs_message_envelope_conforms(self, event):
        """Generated SQS message envelope events validate against schema."""
        jsonschema.validate(event, SQS_MESSAGE_ENVELOPE_SCHEMA)

    @given(event=sfn_task_token_events())
    @settings(max_examples=10)
    def test_sfn_task_token_conforms(self, event):
        """Generated SFN task token events validate against schema."""
        jsonschema.validate(event, SFN_TASK_TOKEN_SCHEMA)

    @given(event=eventbridge_execution_update_events())
    @settings(max_examples=10)
    def test_eventbridge_execution_update_conforms(self, event):
        """Generated EventBridge execution update events validate against schema."""
        jsonschema.validate(event, EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA)

    @given(event=glue_job_state_change_events())
    @settings(max_examples=10)
    def test_glue_job_state_change_conforms(self, event):
        """Generated Glue Job State Change events validate against schema."""
        jsonschema.validate(event, GLUE_JOB_STATE_CHANGE_SCHEMA)

    @given(event=ws_message_events())
    @settings(max_examples=10)
    def test_ws_message_conforms(self, event):
        """Generated WebSocket message events validate against schema."""
        jsonschema.validate(event, WS_MESSAGE_SCHEMA)


class TestFixturesConformToSchemas:
    """Verify that all fixture files validate against their schemas."""

    @pytest.mark.parametrize(
        "fixture_file,schema_key",
        [
            ("events/s3_object_created.json", "s3_object_created"),
            ("events/sqs_message_envelope.json", "sqs_message_envelope"),
            ("events/sfn_task_token.json", "sfn_task_token"),
            ("events/eventbridge_execution_update.json", "eventbridge_execution_update"),
            ("events/glue_job_state_change.json", "glue_job_state_change"),
        ],
    )
    def test_fixture_validates(self, fixture_file, schema_key):
        """Each fixture file validates against its corresponding schema."""
        event = _load_fixture(fixture_file)
        schema = EVENT_SCHEMAS[schema_key]
        jsonschema.validate(event, schema)
