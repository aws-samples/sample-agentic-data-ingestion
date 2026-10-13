# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""JSON Schema definitions for internal event formats.

Derived from the fixture files in tests/fixtures/events/ and the event shapes
consumed by Lambda handlers (analysis-0.1-lambda-handlers.md).

Used by contract tests to validate that event producers emit payloads matching
the shapes expected by event consumers.

Task 8.1 — Requirements 5.1, 5.2, 5.3, 5.4
"""

# ---------------------------------------------------------------------------
# S3 Object Created (EventBridge format)
# Consumed by Pipeline_Trigger via SQS → EventBridge rule.
# Must have: detail.bucket.name, detail.object.key, detail.object.size
# ---------------------------------------------------------------------------

S3_OBJECT_CREATED_SCHEMA = {
    "type": "object",
    "required": ["version", "id", "detail-type", "source", "account", "time", "region", "detail"],
    "properties": {
        "version": {"type": "string"},
        "id": {"type": "string"},
        "detail-type": {"type": "string", "const": "Object Created"},
        "source": {"type": "string", "const": "aws.s3"},
        "account": {"type": "string"},
        "time": {"type": "string"},
        "region": {"type": "string"},
        "resources": {"type": "array", "items": {"type": "string"}},
        "detail": {
            "type": "object",
            "required": ["bucket", "object"],
            "properties": {
                "version": {"type": "string"},
                "bucket": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                    },
                },
                "object": {
                    "type": "object",
                    "required": ["key", "size"],
                    "properties": {
                        "key": {"type": "string"},
                        "size": {"type": ["number", "integer"]},
                        "etag": {"type": "string"},
                        "sequencer": {"type": "string"},
                    },
                },
                "request-id": {"type": "string"},
                "requester": {"type": "string"},
                "source-ip-address": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    },
}

# ---------------------------------------------------------------------------
# SQS Message Envelope
# Wraps S3 notification events for the Pipeline_Trigger Lambda.
# Must have: Records[].body, Records[].messageId, Records[].eventSource
# ---------------------------------------------------------------------------

SQS_MESSAGE_ENVELOPE_SCHEMA = {
    "type": "object",
    "required": ["Records"],
    "properties": {
        "Records": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["messageId", "body", "eventSource"],
                "properties": {
                    "messageId": {"type": "string"},
                    "receiptHandle": {"type": "string"},
                    "body": {"type": "string"},
                    "attributes": {"type": "object"},
                    "messageAttributes": {"type": "object"},
                    "md5OfBody": {"type": "string"},
                    "eventSource": {"type": "string", "const": "aws:sqs"},
                    "eventSourceARN": {"type": "string"},
                    "awsRegion": {"type": "string"},
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# SFN Task Token
# Passed to approval-related Lambda functions.
# Must have: TaskToken, Input.pipeline_id, Input.run_id
# ---------------------------------------------------------------------------

SFN_TASK_TOKEN_SCHEMA = {
    "type": "object",
    "required": ["TaskToken", "Input"],
    "properties": {
        "TaskToken": {"type": "string", "minLength": 1},
        "Input": {
            "type": "object",
            "required": ["pipeline_id", "run_id"],
            "properties": {
                "pipeline_id": {"type": "string"},
                "run_id": {"type": "string"},
                "approval_type": {"type": "string"},
                "step_name": {"type": "string"},
                "source_schema": {"type": "object"},
                "target_schema": {"type": "object"},
            },
        },
    },
}

# ---------------------------------------------------------------------------
# EventBridge Execution Update
# Published by the execution updater and notification service.
# Must have: detail.pipeline_id, detail.run_id, detail.status, detail.current_step
# ---------------------------------------------------------------------------

EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA = {
    "type": "object",
    "required": ["version", "id", "detail-type", "source", "account", "time", "region", "detail"],
    "properties": {
        "version": {"type": "string"},
        "id": {"type": "string"},
        "detail-type": {"type": "string"},
        "source": {"type": "string"},
        "account": {"type": "string"},
        "time": {"type": "string"},
        "region": {"type": "string"},
        "resources": {"type": "array"},
        "detail": {
            "type": "object",
            "required": ["pipeline_id", "run_id", "status", "current_step"],
            "properties": {
                "pipeline_id": {"type": "string"},
                "run_id": {"type": "string"},
                "status": {"type": "string"},
                "current_step": {"type": "string"},
                "previous_step": {"type": "string"},
                "started_at": {"type": "string"},
                "updated_at": {"type": "string"},
                "steps_status": {"type": "object"},
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Glue Job State Change
# Published by AWS Glue via EventBridge.
# Must have: detail.jobName, detail.state, detail.jobRunId
# ---------------------------------------------------------------------------

GLUE_JOB_STATE_CHANGE_SCHEMA = {
    "type": "object",
    "required": ["version", "id", "detail-type", "source", "account", "time", "region", "detail"],
    "properties": {
        "version": {"type": "string"},
        "id": {"type": "string"},
        "detail-type": {"type": "string", "const": "Glue Job State Change"},
        "source": {"type": "string", "const": "aws.glue"},
        "account": {"type": "string"},
        "time": {"type": "string"},
        "region": {"type": "string"},
        "resources": {"type": "array"},
        "detail": {
            "type": "object",
            "required": ["jobName", "state", "jobRunId"],
            "properties": {
                "jobName": {"type": "string"},
                "severity": {"type": "string"},
                "state": {"type": "string"},
                "jobRunId": {"type": "string"},
                "message": {"type": "string"},
                "startedOn": {"type": "string"},
                "completedOn": {"type": "string"},
                "executionTime": {"type": ["number", "integer"]},
                "timeout": {"type": ["number", "integer"]},
                "maxCapacity": {"type": "number"},
                "workerType": {"type": "string"},
                "numberOfWorkers": {"type": ["number", "integer"]},
            },
        },
    },
}

# ---------------------------------------------------------------------------
# WebSocket Message (server → client)
# Broadcast by ws_broadcast Lambda to connected dashboard clients.
# Must have: type, pipeline_id, run_id, status, current_step
# ---------------------------------------------------------------------------

WS_MESSAGE_SCHEMA = {
    "type": "object",
    "required": ["type", "pipeline_id", "run_id", "status", "current_step"],
    "properties": {
        "type": {"type": "string"},
        "pipeline_id": {"type": "string"},
        "run_id": {"type": "string"},
        "status": {"type": "string"},
        "current_step": {"type": "string"},
        "updated_at": {"type": "string"},
    },
}

# ---------------------------------------------------------------------------
# Registry: maps event type names to their schemas for property tests
# ---------------------------------------------------------------------------

EVENT_SCHEMAS = {
    "s3_object_created": S3_OBJECT_CREATED_SCHEMA,
    "sqs_message_envelope": SQS_MESSAGE_ENVELOPE_SCHEMA,
    "sfn_task_token": SFN_TASK_TOKEN_SCHEMA,
    "eventbridge_execution_update": EVENTBRIDGE_EXECUTION_UPDATE_SCHEMA,
    "glue_job_state_change": GLUE_JOB_STATE_CHANGE_SCHEMA,
    "ws_message": WS_MESSAGE_SCHEMA,
}
