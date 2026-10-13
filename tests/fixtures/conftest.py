# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared fixtures with moto/real-AWS toggle.

Provides the AWS backend toggle and session prefix for test data isolation.
When AGENTIC_DATA_INGESTION_TEST_REAL_AWS is 'true', fixtures use real AWS boto3 clients;
when 'false' or unset (default), fixtures use moto mocks.

The session prefix ensures parallel test runs against the same AWS account
don't collide by prefixing all test-created resource identifiers with
``test-{uuid[:8]}-``.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

# --- AWS backend toggle (Requirement 3.7) ---
# When true, shared fixtures connect to real AWS using caller credentials.
# When false/unset (default), shared fixtures use moto in-memory mocks.
USE_REAL_AWS: bool = os.environ.get("AGENTIC_DATA_INGESTION_TEST_REAL_AWS", "false").lower() == "true"

# --- Session prefix for real-AWS isolation (Requirement 12.1) ---
# Every test-created resource (pipeline IDs, S3 keys, DynamoDB items) is
# prefixed with this value so parallel runs against the same account don't
# collide.
TEST_SESSION_PREFIX: str = f"test-{uuid.uuid4().hex[:8]}-"

# ---------------------------------------------------------------------------
# DynamoDB table definitions — derived from deploy-cdk/lib/data-stack.ts
# (analysis-0.3-dynamodb-schemas.md).  All key types are STRING ("S").
# All GSI projections are ALL.
# ---------------------------------------------------------------------------

# Each entry: (table_name, pk, sk_or_None, list_of_gsi_dicts)
# GSI dict: {"IndexName": ..., "pk": ..., "sk": ... or None}
TABLE_DEFINITIONS: list[dict] = [
    {
        "TableName": "agentic_data_ingestion_state",
        "pk": "run_id",
        "sk": "step_name",
        "gsis": [
            {"IndexName": "PipelineIndex", "pk": "pipeline_id", "sk": "timestamp"},
            {"IndexName": "GlueJobRunIndex", "pk": "glue_job_run_id", "sk": None},
            {"IndexName": "step_name-timestamp-index", "pk": "step_name", "sk": "timestamp"},
        ],
    },
    {
        "TableName": "agentic_data_ingestion_pipelines",
        "pk": "pipeline_id",
        "sk": None,
        "gsis": [
            {"IndexName": "status-created_at-index", "pk": "status", "sk": "created_at"},
            {"IndexName": "target_schema_name-index", "pk": "target_schema_name", "sk": "created_at"},
        ],
    },
    {
        "TableName": "agentic_data_ingestion_schema_mappings",
        "pk": "mapping_id",
        "sk": None,
        "gsis": [
            {"IndexName": "source-schema-index", "pk": "source_schema_hash", "sk": "created_timestamp"},
            {"IndexName": "target-schema-index", "pk": "target_schema_hash", "sk": "created_timestamp"},
            {"IndexName": "schema-pair-index", "pk": "source_schema_hash", "sk": "target_schema_hash"},
        ],
    },
    {
        "TableName": "agentic_data_ingestion_generated_code",
        "pk": "PK",
        "sk": "SK",
        "gsis": [
            {"IndexName": "pipeline_id-index", "pk": "pipeline_id", "sk": "generation_timestamp"},
            {"IndexName": "run_id-index", "pk": "run_id", "sk": "generation_timestamp"},
        ],
    },
    {
        "TableName": "agentic_data_ingestion_test_results",
        "pk": "PK",
        "sk": "SK",
        "gsis": [
            {"IndexName": "test_status-index", "pk": "test_status", "sk": "test_timestamp"},
        ],
    },
    {
        "TableName": "agentic_data_ingestion_approvals",
        "pk": "approval_id",
        "sk": None,
        "gsis": [
            {"IndexName": "pipeline-status-index", "pk": "pipeline_id", "sk": "status"},
            {"IndexName": "status-created-index", "pk": "status", "sk": "created_at"},
            {"IndexName": "approval-type-created-index", "pk": "approval_type", "sk": "created_at"},
            {"IndexName": "run_id-index", "pk": "run_id", "sk": None},
        ],
    },
    {
        "TableName": "agentic_data_ingestion_notifications",
        "pk": "notification_id",
        "sk": None,
        "gsis": [
            {"IndexName": "PriorityIndex", "pk": "priority", "sk": "created_at"},
        ],
    },
    {
        "TableName": "agentic_data_ingestion_executions",
        "pk": "pipeline_id",
        "sk": "run_id",
        "gsis": [
            {"IndexName": "StatusIndex", "pk": "status", "sk": "started_at"},
            {"IndexName": "run_id-index", "pk": "run_id", "sk": None},
            # p48 R5.2 — the chronological read index (data-stack.ts:209-214).
            # SPARSE by construction: moto, like DynamoDB, indexes only items
            # carrying BOTH key attributes, so a row with no `started_at` is
            # absent from it. That is the property R9.1 asserts against.
            {"IndexName": "RunsByTimeIndex", "pk": "pipeline_id", "sk": "started_at"},
        ],
    },
    {
        "TableName": "agentic_data_ingestion_task_tokens",
        "pk": "approval_id",
        "sk": None,
        "gsis": [],
    },
    {
        # The pipeline concurrency lock (data-stack.ts): one row per pipeline
        # prefix, holder in `execution_name`, TTL in `ttl`.
        "TableName": "agentic_data_ingestion_pipeline_locks",
        "pk": "pipeline_prefix",
        "sk": None,
        "gsis": [],
    },
    {
        "TableName": "agentic_data_ingestion_ws_connections",
        "pk": "connection_id",
        "sk": "pipeline_id",
        "gsis": [
            {"IndexName": "PipelineIndex", "pk": "pipeline_id", "sk": None},
        ],
    },
    {
        "TableName": "agentic_data_ingestion_errors",
        "pk": "error_date",
        "sk": "timestamp",
        "gsis": [],
    },
    {
        "TableName": "agentic_data_ingestion_agent_traces",
        "pk": "run_id",
        "sk": "step_name",
        "gsis": [],
    },
    {
        "TableName": "agentic_data_ingestion_audit_log",
        "pk": "resource_id",
        "sk": "timestamp",
        "gsis": [
            {"IndexName": "ActionIndex", "pk": "action", "sk": "timestamp"},
        ],
    },
]


@pytest.fixture(scope="session")
def aws_session_prefix() -> str:
    """Unique prefix for test data isolation in real AWS.

    Returns the session-scoped prefix string (e.g. ``test-a1b2c3d4-``).
    All test-created resources should incorporate this prefix so that
    concurrent test sessions and cleanup routines can identify test data.
    """
    return TEST_SESSION_PREFIX


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_key_schema(pk: str, sk: str | None) -> list[dict]:
    """Build a DynamoDB KeySchema list from partition/sort key names."""
    schema = [{"AttributeName": pk, "KeyType": "HASH"}]
    if sk:
        schema.append({"AttributeName": sk, "KeyType": "RANGE"})
    return schema


def _build_attribute_definitions(table_def: dict) -> list[dict]:
    """Collect all unique attribute names used in keys/GSIs for a table."""
    attrs: set[str] = set()
    attrs.add(table_def["pk"])
    if table_def["sk"]:
        attrs.add(table_def["sk"])
    for gsi in table_def["gsis"]:
        attrs.add(gsi["pk"])
        if gsi["sk"]:
            attrs.add(gsi["sk"])
    return [{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)]


def _build_create_table_kwargs(table_def: dict, table_name: str) -> dict:
    """Build the full ``create_table`` kwargs for a single table definition."""
    kwargs: dict = {
        "TableName": table_name,
        "KeySchema": _build_key_schema(table_def["pk"], table_def["sk"]),
        "AttributeDefinitions": _build_attribute_definitions(table_def),
        "BillingMode": "PAY_PER_REQUEST",
    }
    if table_def["gsis"]:
        kwargs["GlobalSecondaryIndexes"] = []
        for gsi in table_def["gsis"]:
            gsi_def: dict = {
                "IndexName": gsi["IndexName"],
                "KeySchema": _build_key_schema(gsi["pk"], gsi["sk"]),
                "Projection": {"ProjectionType": "ALL"},
            }
            kwargs["GlobalSecondaryIndexes"].append(gsi_def)
    return kwargs


def _create_moto_dynamodb_tables() -> dict[str, boto3.resources.factory.dynamodb.Table]:
    """Create all 12 DynamoDB tables in moto and return a name→Table map."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    tables: dict[str, boto3.resources.factory.dynamodb.Table] = {}
    for table_def in TABLE_DEFINITIONS:
        table_name = table_def["TableName"]
        kwargs = _build_create_table_kwargs(table_def, table_name)
        dynamodb.create_table(**kwargs)
        table = dynamodb.Table(table_name)
        table.wait_until_exists()
        tables[table_name] = table
    return tables


def _real_dynamodb_tables(prefix: str) -> dict[str, boto3.resources.factory.dynamodb.Table]:
    """Return boto3 Table resources for real AWS tables with session prefix.

    In real-AWS mode the tables already exist (created by CDK).  We prefix
    the *logical* names so callers can look them up, but the underlying
    physical table names are the originals (no prefix).  The prefix is used
    for *item-level* isolation — callers should prefix pipeline_id / run_id
    values with the session prefix.
    """
    dynamodb = boto3.resource("dynamodb")
    tables: dict[str, boto3.resources.factory.dynamodb.Table] = {}
    for table_def in TABLE_DEFINITIONS:
        table_name = table_def["TableName"]
        table = dynamodb.Table(table_name)
        tables[table_name] = table
    return tables


def _cleanup_real_dynamodb_items(
    tables: dict[str, boto3.resources.factory.dynamodb.Table],
    prefix: str,
) -> None:
    """Delete test-created items from real DynamoDB tables.

    Scans each table for items whose partition key value starts with the
    session prefix and deletes them.  This is best-effort cleanup — if a
    scan page fails we log and continue.
    """
    for table_def in TABLE_DEFINITIONS:
        table_name = table_def["TableName"]
        table = tables.get(table_name)
        if table is None:
            continue
        pk_attr = table_def["pk"]
        sk_attr = table_def["sk"]
        try:
            scan_kwargs: dict = {}
            while True:
                response = table.scan(**scan_kwargs)
                items = response.get("Items", [])
                with table.batch_writer() as batch:
                    for item in items:
                        pk_val = str(item.get(pk_attr, ""))
                        if pk_val.startswith(prefix):
                            key = {pk_attr: item[pk_attr]}
                            if sk_attr and sk_attr in item:
                                key[sk_attr] = item[sk_attr]
                            batch.delete_item(Key=key)
                # Handle pagination
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_dynamodb_tables(aws_session_prefix):
    """Create all 12 DynamoDB tables with GSIs matching CDK-defined schemas.

    In moto mode (default): creates fresh in-memory tables for each test.
    In real-AWS mode: returns handles to existing tables; cleans up
    test-prefixed items after the test.

    Returns a dict mapping canonical table names to boto3 Table resources.

    Validates: Requirements 3.1, 3.7, 3.8, 3.9, 12.2, 12.3
    """
    if USE_REAL_AWS:
        tables = _real_dynamodb_tables(aws_session_prefix)
        yield tables
        _cleanup_real_dynamodb_items(tables, aws_session_prefix)
    else:
        with mock_aws():
            tables = _create_moto_dynamodb_tables()
            yield tables


# ---------------------------------------------------------------------------
# S3 bucket definitions (Task 2.3 — Requirement 3.2)
# ---------------------------------------------------------------------------

S3_BUCKET_DEFINITIONS: list[str] = [
    "agentic-data-ingestion-source",
    "agentic-data-ingestion-processed",
    "agentic-data-ingestion-checkpoints",
    "agentic-data-ingestion-generated-code",
    "agentic-data-ingestion-test-outputs",
    "agentic-data-ingestion-target-schemas",
]


def _create_moto_s3_buckets() -> dict[str, str]:
    """Create all S3 buckets in moto and return a name→name map."""
    s3 = boto3.client("s3", region_name="us-east-1")
    buckets: dict[str, str] = {}
    for bucket_name in S3_BUCKET_DEFINITIONS:
        s3.create_bucket(Bucket=bucket_name)
        buckets[bucket_name] = bucket_name
    return buckets


def _real_s3_buckets(prefix: str) -> dict[str, str]:
    """Return real S3 bucket names (buckets already exist from CDK deploy).

    In real-AWS mode the buckets already exist.  The prefix is used for
    *object-level* isolation — callers should prefix S3 keys with the
    session prefix.
    """
    buckets: dict[str, str] = {}
    for bucket_name in S3_BUCKET_DEFINITIONS:
        buckets[bucket_name] = bucket_name
    return buckets


def _cleanup_real_s3_objects(buckets: dict[str, str], prefix: str) -> None:
    """Delete test-prefixed objects from real S3 buckets (best-effort)."""
    s3 = boto3.client("s3")
    for bucket_name in buckets.values():
        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
                objects = page.get("Contents", [])
                if objects:
                    delete_keys = [{"Key": obj["Key"]} for obj in objects]
                    s3.delete_objects(
                        Bucket=bucket_name,
                        Delete={"Objects": delete_keys},
                    )
        except Exception:
            pass


@pytest.fixture
def mock_s3_buckets(aws_session_prefix):
    """Create S3 buckets for source, processed, checkpoint, and other storage.

    In moto mode (default): creates fresh in-memory buckets for each test.
    In real-AWS mode: returns existing bucket names; cleans up test-prefixed
    objects after the test.

    Returns a dict mapping bucket names to bucket name strings.

    Validates: Requirements 3.2, 3.7, 3.8, 12.2
    """
    if USE_REAL_AWS:
        buckets = _real_s3_buckets(aws_session_prefix)
        yield buckets
        _cleanup_real_s3_objects(buckets, aws_session_prefix)
    else:
        with mock_aws():
            buckets = _create_moto_s3_buckets()
            yield buckets


# ---------------------------------------------------------------------------
# SQS queue definitions (Task 2.4 — Requirement 3.3)
# ---------------------------------------------------------------------------

SQS_QUEUE_DEFINITIONS: list[str] = [
    "agentic-data-ingestion-file-ingestion",
]


def _create_moto_sqs_queues() -> dict[str, str]:
    """Create all SQS queues in moto and return a name→URL map."""
    sqs = boto3.client("sqs", region_name="us-east-1")
    queues: dict[str, str] = {}
    for queue_name in SQS_QUEUE_DEFINITIONS:
        response = sqs.create_queue(QueueName=queue_name)
        queues[queue_name] = response["QueueUrl"]
    return queues


def _real_sqs_queues(prefix: str) -> dict[str, str]:
    """Return real SQS queue URLs (queues already exist from CDK deploy).

    In real-AWS mode the queues already exist.  The prefix is used for
    *message-level* isolation — callers should prefix message attributes
    with the session prefix.
    """
    sqs = boto3.client("sqs")
    queues: dict[str, str] = {}
    for queue_name in SQS_QUEUE_DEFINITIONS:
        response = sqs.get_queue_url(QueueName=queue_name)
        queues[queue_name] = response["QueueUrl"]
    return queues


def _cleanup_real_sqs_messages(queues: dict[str, str], prefix: str) -> None:
    """Purge test messages from real SQS queues (best-effort).

    SQS doesn't support selective deletion by prefix, so we purge the
    entire queue.  This is acceptable for test queues.
    """
    sqs = boto3.client("sqs")
    for queue_url in queues.values():
        try:
            sqs.purge_queue(QueueUrl=queue_url)
        except Exception:
            pass


@pytest.fixture
def mock_sqs_queues(aws_session_prefix):
    """Create SQS queue for file ingestion.

    In moto mode (default): creates fresh in-memory queue for each test.
    In real-AWS mode: returns existing queue URL; purges messages after
    the test.

    Returns a dict mapping queue names to queue URL strings.

    Validates: Requirements 3.3, 3.7, 3.8, 12.2
    """
    if USE_REAL_AWS:
        queues = _real_sqs_queues(aws_session_prefix)
        yield queues
        _cleanup_real_sqs_messages(queues, aws_session_prefix)
    else:
        with mock_aws():
            queues = _create_moto_sqs_queues()
            yield queues


# ---------------------------------------------------------------------------
# Lambda environment variables (Task 2.5 — Requirement 3.5)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_lambda_env(monkeypatch):
    """Set all required Lambda environment variables.

    Covers all 26+ env vars identified in analysis-0.1-lambda-handlers.md.
    Uses ``monkeypatch.setenv`` so variables are automatically restored
    after each test.

    Validates: Requirements 3.5, 3.9
    """
    env_vars: dict[str, str] = {
        # DynamoDB table names
        "STATE_TABLE": "agentic_data_ingestion_state",
        "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
        "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
        "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
        "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
        "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
        "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
        "ERRORS_TABLE": "agentic_data_ingestion_errors",
        "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
        # job_completion reads it through require_env at import, so a test that
        # loads job_completion under this fixture fails without it, unless an
        # earlier module happened to import env_utils in local mode first.
        "PROCESSED_FILES_TABLE": "agentic_data_ingestion_processed_files",
        "WS_CONNECTIONS_TABLE": "agentic_data_ingestion_ws_connections",
        "NOTIFICATIONS_TABLE": "agentic_data_ingestion_notifications",
        # WebSocket
        "WS_API_ENDPOINT": "https://test.execute-api.us-east-1.amazonaws.com/prod",
        # Step Functions
        "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
        # Concurrency
        "MAX_CONCURRENT_EXECUTIONS": "10",
        # File validation
        "MAX_FILE_SIZE_BYTES": "5368709120",
        # Bedrock
        "BEDROCK_MODEL_ID": "anthropic.claude-3-haiku-20240307-v1:0",
        # S3 buckets
        "TARGET_SCHEMA_BUCKET": "agentic-data-ingestion-target-schemas",
        "GENERATED_CODE_BUCKET": "agentic-data-ingestion-generated-code",
        "TEST_OUTPUTS_BUCKET": "agentic-data-ingestion-test-outputs",
        # AgentCore
        "AGENTCORE_ENDPOINT": "https://test-agentcore.us-east-1.amazonaws.com",
        # Glue
        "S3_TABLES_JAR_PATH": "s3://test-jars/s3-tables.jar",
        # Feature flags
        "MCP_ENABLED": "false",
        "MCP_SIGV4_ENABLED": "false",
        "POWERTOOLS_ENABLED": "false",
        "EVENTBRIDGE_ENABLED": "false",
        # SNS
        "SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic",
        "CLOUDWATCH_ALARM_TOPIC": "arn:aws:sns:us-east-1:123456789012:test-alarms",
        # Timeouts — mirrors config.APPROVAL_WINDOW_HOURS (the advertised
        # approval window, = the CDK gate's APPROVAL_TIMEOUT_SECONDS, p34 R6).
        # Was "168" while the advertised window was 7 days.
        "DEFAULT_TIMEOUT_HOURS": "72",
        # AWS region
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        # Environment
        "AGENTIC_DATA_INGESTION_ENV": "test",
        # Notifications
        "NOTIFICATION_CHANNELS": "sns,dashboard",
        # Pipeline locks
        "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
        # Agent traces
        "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
        # Audit log
        "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    }
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    return env_vars


# ---------------------------------------------------------------------------
# Event fixture helpers and fixtures (Task 2.6 — Requirement 3.4)
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent


def _load_fixture(filename: str) -> dict:
    """Load a JSON fixture file relative to the fixtures directory.

    Args:
        filename: Path relative to ``tests/fixtures/`` (e.g.
            ``events/s3_object_created.json``).

    Returns:
        Parsed JSON as a Python dict.
    """
    filepath = _FIXTURES_DIR / filename
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sample_s3_event() -> dict:
    """Load canonical S3 Object Created event (EventBridge format).

    Validates: Requirement 3.4
    """
    return _load_fixture("events/s3_object_created.json")


@pytest.fixture
def sample_sqs_message() -> dict:
    """Load canonical SQS message envelope.

    Validates: Requirement 3.4
    """
    return _load_fixture("events/sqs_message_envelope.json")


@pytest.fixture
def sample_sfn_task_token_event() -> dict:
    """Load canonical Step Functions task token event.

    Validates: Requirement 3.4
    """
    return _load_fixture("events/sfn_task_token.json")


@pytest.fixture
def sample_eventbridge_event() -> dict:
    """Load canonical EventBridge execution update event.

    Validates: Requirement 3.4
    """
    return _load_fixture("events/eventbridge_execution_update.json")


# ---------------------------------------------------------------------------
# schema_mapping approval payload — the Decimal carrier (p50 R1.6)
# ---------------------------------------------------------------------------
# WHY THIS EXISTS
# ---------------
# `approval_handler`'s serialization boundary converts Decimal once, immediately
# before `json.dumps` (`lambda_function.py:551`, ratified by `LGR-86698d4-1`).
# That boundary has never had a populated `schema_mapping` payload to convert:
# the branch at `:245-263` reads all nine keys off the state row's TOP level,
# where the producer writes almost nothing, so every numeric it could surface
# resolves to `None`. `LGR-86698d4-1` named this exactly — "`schema_mapping` is
# one correct read away from the same crash … the real `Decimal`-converted
# payload sits at `output_data.approval`" — and p50's C1 is the correct read.
#
# So this fixture is the input that first makes `Decimal` reachable on that
# path, and `confidence_scores` is its carrier: the producer keys it by target
# field from each mapping's `confidence`
# (`schema_mapping/lambda_function.py:1705-1707`), those are floats, and the
# whole payload is written as `convert_floats_to_decimal(approval_payload)`
# (`:1927`). DynamoDB has a single numeric type, so every numeric comes back
# `Decimal` regardless of what was written — which is why every number below is
# a real `decimal.Decimal` and not a `float`. A float fixture would serialize
# cleanly and prove nothing about the path it is standing in for (design O2).
#
# WHAT IS FROM THE LIVE RECORD, AND WHAT IS NOT
# ---------------------------------------------
# From the measured run (the validation account / us-west-2, `schema_mapping` approval,
# `requirements.md` Symptom 1): the run id, `generated_by`, and the real mapping
# `signup_date` → `registration_date` at confidence **0.96**. `requirements.md`
# does not record that run's pipeline id or its full schemas, so the pipeline
# id, the field names around the documented mapping, and the two schemas are
# fixture-local — shaped to the producer's own output, not copied from AWS.
#
# WHERE EACH OF THE NINE READ KEYS ACTUALLY LIVES — read before using this for C1
# ------------------------------------------------------------------------------
# Nine keys are read from an address; a tenth, `mapping_count`, is derived by p50
# C2 from `proposed_mappings` and has no address at all. Of the nine:
#
# Seven of the nine sit under `output_data.approval`, built by
# `create_approval_payload` (`schema_mapping/lambda_function.py:1734-1780`):
# `source_schema`, `target_schema`, `proposed_mappings`, `confidence_scores`,
# `unmapped_fields`, `mapping_summary`, `generated_by`.
#
# The other two are **not** in that payload at all. `evidence` and `questions`
# are written to the row's TOP level, by a different writer: the complex-mapping
# agent's `update_item` at `:2277-2291`. So the top-level read is CORRECT for
# those two and must survive C1 — they were `None` on the observed run because
# that agent did not run, not because the address is wrong. Compose
# ``SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES`` into the row to exercise them.
# `generated_by` resolves in both places (`:1946` writes it at the top level
# too), which is the in-file precedent `code_version_id` follows at
# `approval_handler:300`.

#: The measured run. Kept verbatim so a failure here is comparable with the
#: record in `requirements.md`.
SCHEMA_MAPPING_DECIMAL_RUN_ID = "run-2026-08-24-13-48-26-4fccdf03"

#: Fixture-local — `requirements.md` does not record the live pipeline id.
SCHEMA_MAPPING_DECIMAL_PIPELINE_ID = "schemamapping-4fccdf03"

#: The one mapping `requirements.md` names, and its observed confidence. Held as
#: named constants because they are the fixture's only load-bearing live values.
SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD = "signup_date"
SCHEMA_MAPPING_DECIMAL_TARGET_FIELD = "registration_date"
SCHEMA_MAPPING_DECIMAL_CONFIDENCE = Decimal("0.96")

#: The eleven keys `get_approval_data`'s `schema_mapping` branch emits, in the
#: order it writes them — the order is part of the serialized bytes.
#:
#: p50 C2 extended this from nine to ten. `mapping_count` is appended rather than
#: inserted so no existing position moves, and the tuple is EXTENDED rather than
#: the assertions on it loosened: the order is asserted because it is part of the
#: serialized bytes, so a pin that stopped pinning order would be worth less than
#: the churn of updating it. Nine keys are READ; the tenth is DERIVED — see
#: ``SCHEMA_MAPPING_APPROVAL_KEYS_DERIVED`` below.
#:
#: p55 R10.1 extends it from ten to ELEVEN. `rename_manifest` is appended for the
#: same reason `mapping_count` was, and the tuple is extended for the same reason
#: too — no assertion on it is loosened. Ten keys now have an address (this one's
#: is `output_data.approval.rename_manifest`, written by
#: `create_approval_payload`); `mapping_count` remains the only derived one.
#:
#: WHY THIS GROWS WHERE p54's THREE REUSE KEYS DID NOT. ``LGR-e5e9edd-6``
#: (template / ratified) refused to grow this tuple to thirteen because those
#: three keys appear on the reuse path ONLY, so a base tuple of thirteen would
#: have failed the exact-equality assertions on a non-reuse payload and the only
#: repair would have been loosening them to subset checks — which p54 do-NOT 6
#: forbids by name. `rename_manifest` is projected with `.get`, so it is present
#: with a `None` value on EVERY path including the degraded and pre-p55 ones.
#: Exact equality therefore still holds everywhere once the member is stated, and
#: no assertion changes shape. Same record, same reasoning, different key.
SCHEMA_MAPPING_APPROVAL_KEYS = (
    "source_schema",
    "target_schema",
    "proposed_mappings",
    "confidence_scores",
    "unmapped_fields",
    "mapping_summary",
    "evidence",
    "questions",
    "generated_by",
    "mapping_count",
    "rename_manifest",
)

#: The three keys p54 R1.1 APPENDS on the reuse path, in the order the branch
#: writes them. They label the payload; they do not re-address any of the ten.
#:
#: `reused_mapping` and `reused_mapping_id` appear on BOTH reuse states — resolved
#: and unreadable. `reused_from_run_id` appears only when resolution succeeded,
#: because it names the origin run the payload was actually read from and there is
#: no honest value for it otherwise.
SCHEMA_MAPPING_REUSED_APPROVAL_KEYS_APPENDED = (
    "reused_mapping",
    "reused_mapping_id",
    "reused_from_run_id",
)

#: The RESOLVED reuse payload's keys, in order: the ten above, then the three
#: appended. p54 do-NOT 6 — appended, never inserted, so no existing position
#: moves, and the base tuple is EXTENDED rather than the assertions on it loosened.
#:
#: WHY A SIBLING RATHER THAN THIRTEEN ENTRIES IN ``SCHEMA_MAPPING_APPROVAL_KEYS``.
#: Two in-tree assertions pin that tuple against a NON-reuse payload by exact list
#: equality — ``test_schema_mapping_payload_read.py``'s
#: ``list(payload) == list(SCHEMA_MAPPING_APPROVAL_KEYS)`` and
#: ``test_schema_mapping_decimal_fixture.py``'s tuple-agreement equality. Growing
#: the base tuple to thirteen breaks both, and the only repair is to weaken them to
#: subset checks, which is the loosening do-NOT 6 forbids by name. So the reuse
#: shape is expressed as the base tuple PLUS an appended triple: the order still
#: says the three keys come last, the ten keep their positions, and every existing
#: assertion passes unmodified. The reuse payload IS this tuple's order.
SCHEMA_MAPPING_APPROVAL_KEYS_REUSED = SCHEMA_MAPPING_APPROVAL_KEYS + SCHEMA_MAPPING_REUSED_APPROVAL_KEYS_APPENDED

#: The UNREADABLE reuse payload's keys, in order (p54 R1.4, do-NOT 7).
#:
#: Five keys and no more. It is deliberately NOT the ten-key shape with nulls: an
#: unresolvable reused mapping is a *named reference to something absent*
#: (``LGR-b2f79ef-14``), not a mapping with no fields, and a ten-key payload of
#: nulls is exactly the second thing. ``proposed_mappings`` is ``None`` so
#: ``mapping_count`` stays ``None`` — never ``0``, never ``[]``, never ``{}``.
#: ``{}`` is reserved for the *no reuse* state: no state row AND no `mapping_id`.
SCHEMA_MAPPING_APPROVAL_KEYS_UNREADABLE = (
    "reused_mapping",
    "reused_mapping_id",
    "mapping_unreadable",
    "proposed_mappings",
    "mapping_count",
)

#: The key(s) the branch DERIVES rather than reads from any address (p50 C2).
#:
#: `mapping_count` is `len(proposed_mappings)` where that is a list and `None`
#: otherwise — three-state per `LGR-4854b3d-1`'s `field_count` ruling, where
#: `None` means NOT ESTABLISHED and is never collapsed to `0`. The state row does
#: carry a top-level `mapping_count` (`schema_mapping/lambda_function.py:1938`)
#: and the branch deliberately does not read it: `LGR-28b4d75-1` and
#: `LGR-9e76249-2` both reject a second authority for a derivable number.
SCHEMA_MAPPING_APPROVAL_KEYS_DERIVED = ("mapping_count",)

#: The eight `create_approval_payload` puts under `output_data.approval` — seven
#: of the original nine, plus p55 R10.1's `rename_manifest`. `evidence` and
#: `questions` are deliberately absent — see the note above; their producer is the
#: top-level `update_item` at `schema_mapping/lambda_function.py:2277-2291`.
SCHEMA_MAPPING_APPROVAL_KEYS_UNDER_OUTPUT_DATA = (
    "source_schema",
    "target_schema",
    "proposed_mappings",
    "confidence_scores",
    "unmapped_fields",
    "mapping_summary",
    "generated_by",
    # p55 R10.1. Its address is real, so it belongs in this tuple and not in
    # ``SCHEMA_MAPPING_APPROVAL_KEYS_DERIVED`` — the projection reads it rather
    # than recomputing it, deliberately (design risk R5.7: the stored manifest is
    # the authority and is used verbatim or not at all).
    "rename_manifest",
)

_SCHEMA_MAPPING_SOURCE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "cust_id": {"type": "string"},
        "full_name": {"type": "string"},
        "email_addr": {"type": "string"},
        "signup_date": {"type": "string"},
        "legacy_flag": {"type": "string"},
    },
}

_SCHEMA_MAPPING_TARGET_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "customer_id": {"type": "string"},
        "customer_name": {"type": "string"},
        "email": {"type": "string"},
        "registration_date": {"type": "string"},
        "status": {"type": "string"},
    },
    "required": ["customer_id", "registration_date"],
}

#: `mapping_result["mappings"]` as the model is instructed to emit it
#: (`schema_mapping/lambda_function.py:1086-1094`), after
#: `convert_floats_to_decimal`: every `confidence` is a `Decimal`.
_SCHEMA_MAPPING_PROPOSED_MAPPINGS: list[dict] = [
    {
        "target_field": "customer_id",
        "source_field": "cust_id",
        "transformation": "string conversion",
        "confidence": Decimal("0.99"),
        "notes": "Direct mapping with type conversion",
    },
    {
        "target_field": "customer_name",
        "source_field": "full_name",
        "transformation": "direct",
        "confidence": Decimal("0.94"),
        "notes": "Same semantics, different name",
    },
    {
        "target_field": "email",
        "source_field": "email_addr",
        "transformation": "direct",
        "confidence": Decimal("0.98"),
        "notes": "Abbreviated source name",
    },
    {
        # The mapping `requirements.md` records for this run.
        "target_field": SCHEMA_MAPPING_DECIMAL_TARGET_FIELD,
        "source_field": SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD,
        "transformation": "to_date(signup_date, 'MM/dd/yyyy')",
        "confidence": SCHEMA_MAPPING_DECIMAL_CONFIDENCE,
        "notes": "Signup and registration are the same event",
    },
]

#: Keyed by target field, from each mapping's `confidence` (`:1705-1707`). This
#: is the field R1.6 names as the likely `Decimal` carrier, and it is the
#: densest one — every value is a numeric, so nothing about it survives a
#: converter that misses a level.
_SCHEMA_MAPPING_CONFIDENCE_SCORES: dict = {
    "customer_id": Decimal("0.99"),
    "customer_name": Decimal("0.94"),
    "email": Decimal("0.98"),
    SCHEMA_MAPPING_DECIMAL_TARGET_FIELD: SCHEMA_MAPPING_DECIMAL_CONFIDENCE,
}

#: `create_approval_payload`'s output (`:1734-1780`) after
#: `convert_floats_to_decimal` — this is the sub-tree C1 points the read at.
_SCHEMA_MAPPING_APPROVAL_PAYLOAD: dict = {
    "approval_type": "schema_mapping",
    "source_schema": _SCHEMA_MAPPING_SOURCE_SCHEMA,
    "target_schema": _SCHEMA_MAPPING_TARGET_SCHEMA,
    "source_schema_hash": "sha256:4fccdf03aaaa",
    "target_schema_hash": "sha256:4fccdf03bbbb",
    "proposed_mappings": _SCHEMA_MAPPING_PROPOSED_MAPPINGS,
    "confidence_scores": _SCHEMA_MAPPING_CONFIDENCE_SCORES,
    "unmapped_fields": {
        "source": ["legacy_flag"],
        "target": [{"field": "status", "default_value": "active", "reason": "Not present in source, using default"}],
        # p37 R3.2 — names, not counts; required ones first. `status` is not in
        # the target schema's `required`, so `target_required` is empty.
        "target_unsourced": ["status"],
        "target_required": [],
    },
    "mapping_visualization": {
        "total_mappings": Decimal("4"),
        "direct_mappings": Decimal("2"),
        "transformation_mappings": Decimal("2"),
        "field_mapping_details": [
            {
                "source_field": mapping["source_field"],
                "target_field": mapping["target_field"],
                "transformation": mapping["transformation"],
                "confidence": mapping["confidence"],
                "notes": mapping["notes"],
            }
            for mapping in _SCHEMA_MAPPING_PROPOSED_MAPPINGS
        ],
    },
    "mapping_statistics": {
        "total_source_fields": Decimal("5"),
        "total_target_fields": Decimal("5"),
        "mapped_fields": Decimal("4"),
        "unmapped_source_fields": Decimal("1"),
        "unmapped_target_fields": Decimal("1"),
        # round((0.99 + 0.94 + 0.98 + 0.96) / 4, 3)
        "average_confidence": Decimal("0.968"),
        # round(4 / 5 * 100, 1)
        "mapping_coverage_percent": Decimal("80.0"),
    },
    "mapping_summary": (
        "Four of five target fields are sourced; signup_date carries the registration "
        "date and needs a date parse. status has no source and takes its default."
    ),
    "generated_by": "us.anthropic.claude-sonnet-4-6",
    # True because `unmapped_source` is non-empty (`:1765-1780`).
    "requires_review": True,
    # p55 R10.1 — appended last, exactly where the producer appends it.
    #
    # `renames` is EMPTY, and that is the honest value for this row rather than a
    # gap: all five of `_SCHEMA_MAPPING_SOURCE_SCHEMA`'s column names already
    # satisfy the Identifier_Allowlist, so a real `derive_rename_manifest` over
    # them proposes nothing. Fabricating a rename here would make this fixture
    # stop being the observed run. What matters for
    # ``SCHEMA_MAPPING_APPROVAL_KEYS_UNDER_OUTPUT_DATA`` is that the key is
    # present and non-`None` — "no renames proposed" and "this payload says
    # nothing about renames" are different facts and the empty list is the first.
    #
    # `manifest_id` is the row's own `mapping_id`, per design §Data Models — no
    # new identifier space. `approval_id` is `None`: this row is the pre-decision
    # state (`approval_required: True` above), and task 6.4 is what records an
    # approval's id onto a manifest.
    "rename_manifest": {
        "manifest_id": "map-20260824134826",
        "source_schema_hash": "sha256:4fccdf03aaaa",
        "approval_id": None,
        "renames": [],
    },
}

#: The full `(run_id, "schema_mapping")` state row as `store_mapping_result`
#: writes it (`:1928-1948`), read back through boto3's resource deserializer —
#: so every numeric is a `Decimal` and every boolean is still a `bool`.
#:
#: `evidence` and `questions` are absent, matching the observed run: the
#: complex-mapping agent did not run, so its top-level `update_item`
#: (`:2277-2291`) never fired. Merge
#: ``SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES`` in to cover that path.
SCHEMA_MAPPING_DECIMAL_STATE_ROW: dict = {
    "run_id": SCHEMA_MAPPING_DECIMAL_RUN_ID,
    "step_name": "schema_mapping",
    "step_sequence": Decimal("4"),
    "status": "success",
    "timestamp": "2026-08-24T13:48:31.204416+00:00",
    "mapping_id": "map-20260824134826",
    "source_schema_hash": "sha256:4fccdf03aaaa",
    "target_schema_hash": "sha256:4fccdf03bbbb",
    "mapping_count": Decimal("4"),
    "unmapped_source_count": Decimal("1"),
    "unmapped_target_count": Decimal("1"),
    "original_pipeline_id": SCHEMA_MAPPING_DECIMAL_PIPELINE_ID,
    "pipeline_id": SCHEMA_MAPPING_DECIMAL_PIPELINE_ID,
    "approval_required": True,
    # P28 R3.5 provenance — the one of the nine keys that resolves today.
    "generated_by": "us.anthropic.claude-sonnet-4-6",
    "output_data": {
        "artifact_type": "schema_mapping",
        "primary_id": "map-20260824134826",
        "status": "success",
        "summary": {
            "mapped_fields": Decimal("4"),
            "unmapped_fields": Decimal("1"),
            "avg_confidence": Decimal("0.968"),
            "approval_required": True,
        },
        "s3_uris": {},
        "approval": _SCHEMA_MAPPING_APPROVAL_PAYLOAD,
    },
}

#: The two top-level attributes the complex-mapping agent writes when it runs
#: (`schema_mapping/lambda_function.py:2277-2291`), both through
#: `convert_floats_to_decimal`. Merge into
#: ``SCHEMA_MAPPING_DECIMAL_STATE_ROW`` when the `evidence` / `questions` reads
#: are in scope — they are the two of the nine keys whose correct address IS the
#: row's top level.
SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES: dict = {
    "evidence": {
        SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD: {
            "sample_values": ["03/14/2026", "11/02/2025"],
            "inferred_format": "MM/dd/yyyy",
            "support": Decimal("0.87"),
        }
    },
    "questions": [
        {
            "field": "status",
            "question": "Should unsourced status default to 'active'?",
            "confidence": Decimal("0.41"),
        }
    ],
}


# ---------------------------------------------------------------------------
# p54 R1.1 — the Reuse_Run trio: an origin run, a mappings row naming it, and a
# Reuse_Run that has no `schema_mapping` state row at all.
#
# A Reuse_Run takes the `ReuseExistingMapping` Pass instead of the schema_mapping
# Lambda, and a Pass writes nothing to DynamoDB — so the ABSENCE of a
# `(SCHEMA_MAPPING_REUSE_RUN_ID, "schema_mapping")` row is the fixture's most
# load-bearing property. There is deliberately no constant for it: a test seeds the
# origin row and the mappings row, and seeds nothing for the reuse run.
#
# The origin row is ``SCHEMA_MAPPING_DECIMAL_STATE_ROW`` unchanged, so the payload
# the reuse path resolves is byte-for-byte the one p50's own tests assert on. That
# equality is the point of route A′ and a separate fixture would have destroyed it.
# ---------------------------------------------------------------------------

#: The Reuse_Run. A different id shape from the origin only so a failure message
#: makes it obvious which run is which; nothing keys on the shape.
SCHEMA_MAPPING_REUSE_RUN_ID = "run-2026-08-28-09-12-04-7bb1e40f"

#: The mapping the Reuse_Run reuses — the origin row's own ``mapping_id``.
SCHEMA_MAPPING_REUSED_MAPPING_ID = SCHEMA_MAPPING_DECIMAL_STATE_ROW["mapping_id"]

#: The mappings-table item as ``store_mapping_separately`` writes it
#: (``schema_mapping/lambda_function.py:1836-1861``), read back through boto3's
#: resource deserializer. Enumerated from the writer, not invented.
#:
#: ``run_id`` is what makes route A′'s second read possible, and the writer sets it
#: **conditionally** — ``if run_id:`` at ``:1858`` — so its absence is a real state.
#: ``SCHEMA_MAPPING_REUSED_MAPPINGS_ITEM_WITHOUT_RUN_ID`` below is that state.
#:
#: Note what is NOT here: no ``source_schema`` and no ``target_schema``. Those two
#: live only inside the origin run's approval payload, which is why plain route A —
#: read the mappings table and stop — cannot satisfy R1.1.
SCHEMA_MAPPING_REUSED_MAPPINGS_ITEM: dict = {
    "mapping_id": SCHEMA_MAPPING_REUSED_MAPPING_ID,
    "source_schema_hash": "sha256:4fccdf03aaaa",
    "target_schema_hash": "sha256:4fccdf03bbbb",
    "mapping_result": json.dumps(
        {
            "mappings": [
                {
                    "target_field": mapping["target_field"],
                    "source_field": mapping["source_field"],
                    "transformation": mapping["transformation"],
                    "confidence": float(mapping["confidence"]),
                    "notes": mapping["notes"],
                }
                for mapping in _SCHEMA_MAPPING_PROPOSED_MAPPINGS
            ],
            "unmapped_source_fields": ["legacy_flag"],
            "unmapped_target_fields": ["status"],
            "generated_by": "us.anthropic.claude-sonnet-4-6",
        }
    ),
    "updated_timestamp": "2026-08-24T13:48:31.204416+00:00",
    "created_timestamp": "2026-08-24T13:48:31.204416+00:00",
    "mapping_version": Decimal("1"),
    "ai_model_used": "us.anthropic.claude-sonnet-4-6",
    "generated_by": "us.anthropic.claude-sonnet-4-6",
    "confidence_score": Decimal("0.97"),
    "update_type": "initial_mapping",
    #: The origin run — route A′'s step 2.
    "run_id": SCHEMA_MAPPING_DECIMAL_RUN_ID,
}

#: The same item with no ``run_id``, which is what the writer produces when it was
#: called without one (``:1858``). The second of p54 R1.4's resolution failures.
SCHEMA_MAPPING_REUSED_MAPPINGS_ITEM_WITHOUT_RUN_ID: dict = {
    key: value for key, value in SCHEMA_MAPPING_REUSED_MAPPINGS_ITEM.items() if key != "run_id"
}


@pytest.fixture
def schema_mapping_reused_mappings_item() -> dict:
    """The mappings-table item a Reuse_Run's ``mapping_id`` resolves to.

    A deep copy per test, so a test that strips ``run_id`` or corrupts the item to
    seed a resolution failure cannot leak that into the next one.

    Validates: p54 R1.1, R1.4
    """
    return copy.deepcopy(SCHEMA_MAPPING_REUSED_MAPPINGS_ITEM)


@pytest.fixture
def schema_mapping_decimal_state_row() -> dict:
    """A populated ``schema_mapping`` state row whose numerics are real ``Decimal``s.

    A deep copy per test, so a test that mutates the row (to seed a degraded
    shape, say) cannot leak that into the next one.

    Validates: p50 R1.6 — Design O2, CP1
    """
    return copy.deepcopy(SCHEMA_MAPPING_DECIMAL_STATE_ROW)


@pytest.fixture
def schema_mapping_decimal_approval_payload() -> dict:
    """Just the ``output_data.approval`` sub-tree, deep-copied.

    The path `LGR-86698d4-1` names as where the real ``Decimal``-converted
    payload sits, for tests that assert on the payload without needing a row.

    Validates: p50 R1.6 — Design O2, CP1
    """
    return copy.deepcopy(_SCHEMA_MAPPING_APPROVAL_PAYLOAD)


# ---------------------------------------------------------------------------
# p50 C5 — the `first_run` twin of the wrong-level read
# ---------------------------------------------------------------------------
# `get_approval_data`'s `first_run` branch read `quality_checks` and
# `data_quality_rules` off the `(run_id, "create_quality_checks")` row's TOP
# level, where `create_quality_checks` writes NEITHER — and, unlike the
# `schema_mapping` twin, writes neither under that spelling ANYWHERE. So this
# fixture's job is to carry the writer's real addresses:
#
#   quality_checks       -> output_data.approval.pre_run_validation.quality_checks
#   data_quality_rules   -> output_data.approval.quality_check_configuration
#                             .quality_rules
#                           (also output_data.quality_configuration.quality_rules
#                            — the same object at write time)
#   quality_verdict      -> output_data.quality_verdict  (already correct, R4.3)
#
# Every numeric is a real `decimal.Decimal`, for the same reason
# `SCHEMA_MAPPING_DECIMAL_STATE_ROW` is: `create_quality_checks` puts the whole
# item through `convert_floats_to_decimal` (`:407`), so boto3's resource
# deserializer hands every `N` attribute back as `Decimal`. C5 is the change that
# first makes those reachable on this branch, exactly as C1 was for
# `confidence_scores` — a `float` fixture would serialize cleanly and pass while
# the real path raised (design O2 / R1.6).

FIRST_RUN_DECIMAL_RUN_ID = "run-2026-08-24-13-48-26-4fccdf03"
FIRST_RUN_DECIMAL_PIPELINE_ID = "latesttesting"

#: `generate_quality_check_rules`' output (`create_quality_checks:123-207`) after
#: `convert_floats_to_decimal`. The two numeric-bearing rule shapes are both
#: represented deliberately: `min_value` / `max_value` off the target schema's
#: `minimum` / `maximum` (`:178-179`) and `min_rows` (`:192`). Those are the
#: `Decimal` carriers on this branch.
_FIRST_RUN_QUALITY_RULES: list = [
    {
        "rule_name": "customer_id_type_check",
        "rule_type": "data_type_validation",
        "field": "customer_id",
        "expected_type": "integer",
        "severity": "error",
        "description": "Validate that customer_id is of type integer",
    },
    {
        "rule_name": "customer_id_null_check",
        "rule_type": "null_validation",
        "field": "customer_id",
        "allow_null": False,
        "severity": "error",
        "description": "Validate that required field customer_id is not null",
    },
    {
        "rule_name": "customer_id_range_check",
        "rule_type": "range_validation",
        "field": "customer_id",
        # The schema's `minimum` / `maximum`, so `Decimal` after the write.
        "min_value": Decimal("1"),
        "max_value": Decimal("999999.5"),
        "severity": "warning",
        "description": "Validate that customer_id is within expected range",
    },
    {
        "rule_name": "row_count_check",
        "rule_type": "completeness_validation",
        "field": "*",
        "min_rows": Decimal("1"),
        "severity": "error",
        "description": "Validate that dataset contains at least one row",
    },
    {
        "rule_name": "duplicate_check",
        "rule_type": "uniqueness_validation",
        "field": "*",
        "check_duplicates": True,
        "severity": "warning",
        "description": "Check for duplicate rows in the dataset",
    },
]

#: `create_quality_check_configuration`'s output (`create_quality_checks:216-259`).
#: One object at write time, stored at BOTH `output_data.quality_configuration`
#: and `output_data.approval.quality_check_configuration` — which is what makes
#: the two-source read on `data_quality_rules` safe rather than a second
#: authority.
_FIRST_RUN_QUALITY_CONFIGURATION: dict = {
    "quality_rules": _FIRST_RUN_QUALITY_RULES,
    "severity_config": {"error_action": "fail", "warning_action": "log"},
    "rule_summary": {
        "total_rules": Decimal("5"),
        "error_rules": Decimal("3"),
        "warning_rules": Decimal("2"),
    },
}

#: `derive_quality_verdict`'s output (`shared/quality_verdict.py`, ratified by
#: `LGR-eee8ac8-6`). Already read correctly at the branch's third key; present so
#: a test can assert R4.3's "unchanged" against a resolving value rather than
#: against `None`.
_FIRST_RUN_QUALITY_VERDICT: dict = {
    "mode": "advisory",
    "template_used": "s3_tables_etl",
    "dqdl_rule_count": Decimal("4"),
    "determined_from": "create_code_row",
}

#: The FOUR LINES `combine_infrastructure_and_quality_approval_data` writes at
#: `pre_run_validation.quality_checks` (`create_quality_checks:307-312`).
#:
#: Only the first is derived from anything; the other three are unconditional
#: string literals in the producer. That is a producer-side weakness C5
#: deliberately does NOT fix — recorded at the read site and here so a test
#: asserting on this list is not read as endorsing the boilerplate.
_FIRST_RUN_PRE_RUN_QUALITY_CHECKS: list = [
    "5 quality rules configured",
    "Data type validations enabled",
    "Null value checks configured",
    "Completeness validation active",
]

#: The `first_run` approval payload as
#: `combine_infrastructure_and_quality_approval_data` builds it
#: (`create_quality_checks:287-322`), after `convert_floats_to_decimal`.
_FIRST_RUN_APPROVAL_PAYLOAD: dict = {
    "approval_type": "first_run",
    # A DICT, as `create_infrastructure` writes it — the reason the branch's
    # separate `infrastructure_status` read cannot simply be re-pointed. Out of
    # C5's scope; see the note in the handler.
    "infrastructure_status": {
        "overall_status": "success",
        "s3_table_bucket": {
            "bucket_name": "adi-latesttesting-tables",
            "table_name": "customers",
            "namespace": "raw",
            "table_format": "ICEBERG",
        },
        "resources_created": ["s3_table_bucket", "namespace", "table"],
        "estimated_monthly_cost": Decimal("42.5"),
    },
    "quality_check_configuration": _FIRST_RUN_QUALITY_CONFIGURATION,
    "quality_verdict": _FIRST_RUN_QUALITY_VERDICT,
    "combined_assessment": {
        "infrastructure_ready": True,
        "quality_checks_configured": True,
        "estimated_monthly_cost": Decimal("42.5"),
        "total_quality_rules": Decimal("5"),
        "error_rules_count": Decimal("3"),
    },
    "pre_run_validation": {
        "infrastructure_checks": [
            "RDS instances are available",
            "S3 buckets are accessible",
            "IAM roles have correct permissions",
            "Glue resources are created",
        ],
        "quality_checks": _FIRST_RUN_PRE_RUN_QUALITY_CHECKS,
    },
    "risk_summary": {"cost_risk": "medium", "data_quality_risk": "low", "overall_risk": "medium"},
}

#: The full `(run_id, "create_quality_checks")` state row as the writer's
#: `put_item` produces it (`create_quality_checks:406-436`), read back through
#: boto3's resource deserializer.
#:
#: Note what is NOT here: no top-level `quality_checks`, no top-level
#: `data_quality_rules`. Their absence is the fixture's whole point — the row is
#: faithful to the writer, and the pre-C5 read returns `None` for both against
#: it.
FIRST_RUN_QUALITY_CHECKS_STATE_ROW: dict = {
    "run_id": FIRST_RUN_DECIMAL_RUN_ID,
    "step_name": "create_quality_checks",
    "status": "success",
    "step_sequence": Decimal("8"),
    "timestamp": "2026-08-24T13:52:07.881204+00:00",
    "pipeline_id": FIRST_RUN_DECIMAL_PIPELINE_ID,
    "original_pipeline_id": FIRST_RUN_DECIMAL_PIPELINE_ID,
    "approval_required": True,
    "output_data": {
        "quality_configuration": _FIRST_RUN_QUALITY_CONFIGURATION,
        "total_rules": Decimal("5"),
        "error_rules": Decimal("3"),
        "warning_rules": Decimal("2"),
        "quality_verdict": _FIRST_RUN_QUALITY_VERDICT,
        "approval": _FIRST_RUN_APPROVAL_PAYLOAD,
    },
}

#: The matching `(run_id, "create_infrastructure")` row, as
#: `create_infrastructure` writes it (`:943-976`). Present so a `first_run` read
#: exercises BOTH `get_item` calls rather than only the quality one — and so the
#: out-of-scope infra reads are visibly `None` against a faithful row rather than
#: silently untested.
FIRST_RUN_INFRASTRUCTURE_STATE_ROW: dict = {
    "run_id": FIRST_RUN_DECIMAL_RUN_ID,
    "step_name": "create_infrastructure",
    "status": "success",
    "step_sequence": Decimal("7"),
    "timestamp": "2026-08-24T13:51:44.010355+00:00",
    "pipeline_id": FIRST_RUN_DECIMAL_PIPELINE_ID,
    "original_pipeline_id": FIRST_RUN_DECIMAL_PIPELINE_ID,
    "approval_required": False,
    "output_data": {
        "artifact_type": "infrastructure",
        "primary_id": FIRST_RUN_DECIMAL_RUN_ID,
        "status": "success",
        "summary": {
            "overall_status": "success",
            "resources_created": ["s3_table_bucket", "namespace", "table"],
            "bucket_name": "adi-latesttesting-tables",
            "table_name": "customers",
            "namespace": "raw",
            "table_format": "ICEBERG",
        },
        "s3_uris": {},
        "infrastructure_status": _FIRST_RUN_APPROVAL_PAYLOAD["infrastructure_status"],
        "s3_table_bucket": _FIRST_RUN_APPROVAL_PAYLOAD["infrastructure_status"]["s3_table_bucket"],
    },
}


@pytest.fixture
def first_run_quality_checks_state_row() -> dict:
    """A populated ``create_quality_checks`` state row whose numerics are ``Decimal``.

    A deep copy per test, so a test that strips a level to seed a degraded shape
    cannot leak that into the next one.

    Validates: p50 R4.1, R4.3 — Design C5
    """
    return copy.deepcopy(FIRST_RUN_QUALITY_CHECKS_STATE_ROW)


@pytest.fixture
def first_run_infrastructure_state_row() -> dict:
    """The matching ``create_infrastructure`` row, deep-copied.

    Validates: p50 R4.1 — Design C5
    """
    return copy.deepcopy(FIRST_RUN_INFRASTRUCTURE_STATE_ROW)
