# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A two-record SQS batch against real DynamoDB and Step Functions (p30 R9.3).

WHAT THIS PROVES — AND WHAT IT DOES NOT
---------------------------------------
**Read this before treating a failure here as a production incident.** The
deployed event source mapping is ``batchSize: 1``
(``orchestration-stack.ts:1233``, pinned by a CDK assertion), so a batch never
holds two records in production today. These tests exercise the **handler
contract** — what ``_handle_sqs_batch`` does when a batch *does* hold two records
— which is the behaviour that must already be correct for the day ``batchSize``
is raised. They are not evidence about a live flow.

What only this level can show, given 4.8's unit tests already cover the response
shape and the per-record classification against mocks:

* record 1's execution exists **exactly once** in Step Functions and record 2's
  ``messageId`` is the **only** entry in ``batchItemFailures`` — asserted against
  a real lock table, where the conditional ``put_item`` that decides the conflict
  actually runs, rather than against a mock told when to fail;
* the blocked record leaves a real ``QUEUED`` tracker row, and a **redelivery
  succeeds once the lock clears**, with that tracker deleted. That is two real
  DynamoDB writes and a real delete across two invocations — the sequence a
  mocked table cannot order.

``itemIdentifier`` must be the record's ``messageId`` verbatim: Lambda silently
treats an unmatched identifier as a *success*, so a derived key would look like
partial-batch reporting while discarding files.

ON ``@mock_aws`` IN THIS FILE
-----------------------------
``tests/unit/test_no_duplicate_mocking_prop.py`` fences direct ``mock_aws()``
usage to ``tests/unit`` and ``tests/contracts``. The integration layer sits
outside that fence and builds its own infrastructure — see
``test_pipeline_deduplication.py`` in this directory. The shared
``mock_dynamodb_tables`` fixture creates neither the pipeline-locks table nor the
ledger table, so it cannot serve this flow as it stands.

Validates: Requirements 9.4 (R9.5's integration clause: "a two-record SQS batch
where record 2 conflicts leaves record 1 processed exactly once")
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _PROJECT_ROOT / "shared"
_TRIGGER_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_pipeline_trigger"

_REGION = "us-east-1"
_BUCKET = "agentic-data-ingestion-source"
_PIPELINE = "orders"
_KEY_1 = f"{_PIPELINE}/first.csv"
_KEY_2 = f"{_PIPELINE}/second.csv"

_EXECUTIONS_TABLE = "agentic_data_ingestion_executions"
_LOCKS_TABLE = "agentic_data_ingestion_pipeline_locks"
_LEDGER_TABLE = "agentic_data_ingestion_processed_files"

# The trigger never reads its context; None keeps the harness honest.
_CONTEXT = None


@pytest.fixture(autouse=True)
def _clear_lambda_modules():
    yield
    sys.modules.pop("lambda_function", None)


def _load_trigger():
    """Load the trigger Lambda from inside the moto context."""
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    if "aws_lambda_powertools" not in sys.modules:
        mock_powertools = MagicMock()
        mock_powertools.Logger.return_value = MagicMock()
        sys.modules["aws_lambda_powertools"] = mock_powertools

    sys.modules.pop("lambda_function", None)
    spec = importlib.util.spec_from_file_location("lambda_function", str(_TRIGGER_DIR / "lambda_function.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["lambda_function"] = module
    spec.loader.exec_module(module)
    return module


def _record(key: str, size: int, etag: str, message_id: str) -> dict:
    eb_event = {
        "version": "0",
        "detail-type": "Object Created",
        "source": "aws.s3",
        "time": "2024-01-01T00:00:00Z",
        "region": _REGION,
        "detail": {
            "bucket": {"name": _BUCKET},
            "object": {"key": key, "size": size, "etag": etag},
        },
    }
    return {"messageId": message_id, "body": json.dumps(eb_event), "eventSource": "aws:sqs"}


def _create_infrastructure() -> str:
    """SFN state machine, executions table, locks table, ledger table, bucket.

    The ledger table is created even though nothing is recorded in these tests:
    ``find_processed_file`` fails open, so an absent table would hide a
    duplicate-skip regression behind a passing batch assertion.
    """
    iam = boto3.client("iam", region_name=_REGION)
    role = iam.create_role(
        RoleName="test-sfn-role-batch",
        AssumeRolePolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Principal": {"Service": "states.amazonaws.com"}, "Action": "sts:AssumeRole"}
                ],
            }
        ),
        Path="/",
    )
    sfn = boto3.client("stepfunctions", region_name=_REGION)
    sm = sfn.create_state_machine(
        name="test-sm-batch",
        definition=json.dumps({"StartAt": "Pass", "States": {"Pass": {"Type": "Pass", "End": True}}}),
        roleArn=role["Role"]["Arn"],
    )

    dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    dynamodb.create_table(
        TableName=_EXECUTIONS_TABLE,
        KeySchema=[
            {"AttributeName": "pipeline_id", "KeyType": "HASH"},
            {"AttributeName": "run_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "StatusIndex",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "started_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=_LOCKS_TABLE,
        KeySchema=[{"AttributeName": "pipeline_prefix", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pipeline_prefix", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=_LEDGER_TABLE,
        KeySchema=[
            {"AttributeName": "pipeline_id", "KeyType": "HASH"},
            {"AttributeName": "fingerprint", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
            {"AttributeName": "fingerprint", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
    return sm["stateMachineArn"]


def _set_env(monkeypatch, state_machine_arn: str) -> None:
    for key, value in {
        "STATE_MACHINE_ARN": state_machine_arn,
        "MAX_CONCURRENT_EXECUTIONS": "10",
        "EXECUTIONS_TABLE": _EXECUTIONS_TABLE,
        "LOCKS_TABLE": _LOCKS_TABLE,
        "PROCESSED_FILES_TABLE": _LEDGER_TABLE,
        "SOURCE_BUCKET": _BUCKET,
        "AWS_DEFAULT_REGION": _REGION,
        "AWS_REGION": _REGION,
    }.items():
        monkeypatch.setenv(key, value)


def _upload(key: str) -> tuple[int, str]:
    """Put an object whose bytes are unique to its key; return (size, bare etag).

    Distinct content per record keeps the two records' fingerprints distinct, so
    nothing here can be skipped as a duplicate — the only reason record 2 comes
    back is the lock.
    """
    body = f"id,name\n1,{key}\n".encode()
    resp = boto3.client("s3", region_name=_REGION).put_object(Bucket=_BUCKET, Key=key, Body=body)
    return len(body), resp["ETag"].strip('"')


def _table(name: str):
    return boto3.resource("dynamodb", region_name=_REGION).Table(name)


def _executions(state_machine_arn: str) -> list[dict]:
    return boto3.client("stepfunctions", region_name=_REGION).list_executions(stateMachineArn=state_machine_arn)[
        "executions"
    ]


def _execution_source_keys(state_machine_arn: str) -> set[str]:
    """The ``source_key`` of every started execution, read from its SFN input."""
    sfn = boto3.client("stepfunctions", region_name=_REGION)
    keys = set()
    for execution in _executions(state_machine_arn):
        sfn_input = json.loads(sfn.describe_execution(executionArn=execution["executionArn"])["input"])
        keys.add(sfn_input["source_key"])
    return keys


def _queued_rows() -> list[dict]:
    return [item for item in _table(_EXECUTIONS_TABLE).scan()["Items"] if item.get("status") == "QUEUED"]


class TestTwoRecordBatchForOnePipeline:
    """Record 1 starts, record 2 conflicts on the lock. Handler contract only —
    see the module docstring on ``batchSize: 1``."""

    @mock_aws
    def test_record_1_starts_once_and_only_record_2_is_returned(self, mock_lambda_env, monkeypatch):
        state_machine_arn = _create_infrastructure()
        _set_env(monkeypatch, state_machine_arn)
        trigger = _load_trigger()

        size_1, etag_1 = _upload(_KEY_1)
        size_2, etag_2 = _upload(_KEY_2)
        event = {
            "Records": [
                _record(_KEY_1, size_1, etag_1, "msg-1"),
                _record(_KEY_2, size_2, etag_2, "msg-2"),
            ]
        }

        result = trigger.lambda_handler(event, _CONTEXT)

        # Only the conflicting record comes back, under its own messageId.
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-2"}]
        assert result["statusCode"] == 200, "the invocation itself must succeed, or SQS fails the whole batch"

        # Record 1 ran exactly once — the guarantee R9.3 exists to protect.
        assert len(_executions(state_machine_arn)) == 1
        assert _execution_source_keys(state_machine_arn) == {_KEY_1}

        # The lock is real and is held by record 1's execution.
        lock = _table(_LOCKS_TABLE).get_item(Key={"pipeline_prefix": _PIPELINE})["Item"]
        started = _executions(state_machine_arn)[0]["name"]
        assert lock["execution_name"] == started

        # The blocked record is tracked as queued rather than lost.
        queued = _queued_rows()
        assert [row["source_key"] for row in queued] == [_KEY_2]

    @mock_aws
    def test_redelivered_record_2_succeeds_once_the_lock_clears(self, mock_lambda_env, monkeypatch):
        state_machine_arn = _create_infrastructure()
        _set_env(monkeypatch, state_machine_arn)
        trigger = _load_trigger()

        size_1, etag_1 = _upload(_KEY_1)
        size_2, etag_2 = _upload(_KEY_2)
        first = trigger.lambda_handler(
            {
                "Records": [
                    _record(_KEY_1, size_1, etag_1, "msg-1"),
                    _record(_KEY_2, size_2, etag_2, "msg-2"),
                ]
            },
            _CONTEXT,
        )
        assert first["batchItemFailures"] == [{"itemIdentifier": "msg-2"}]

        # Record 1's execution terminates: the state machine's ReleaseLock step
        # deletes the row. This is the only thing that changes between the two
        # deliveries.
        _table(_LOCKS_TABLE).delete_item(Key={"pipeline_prefix": _PIPELINE})

        redelivered = trigger.lambda_handler({"Records": [_record(_KEY_2, size_2, etag_2, "msg-2")]}, _CONTEXT)

        assert redelivered["batchItemFailures"] == []
        assert len(_executions(state_machine_arn)) == 2
        assert _execution_source_keys(state_machine_arn) == {_KEY_1, _KEY_2}
        assert _queued_rows() == [], "the queued tracker must be removed when the file starts processing"

    @mock_aws
    def test_two_pipelines_in_one_batch_both_start(self, mock_lambda_env, monkeypatch):
        """The lock is per-pipeline, so two records for two pipelines are
        independent — R9.3's own example, here against the real lock table."""
        state_machine_arn = _create_infrastructure()
        _set_env(monkeypatch, state_machine_arn)
        trigger = _load_trigger()

        key_a, key_b = "pipeline-a/file.csv", "pipeline-b/file.csv"
        size_a, etag_a = _upload(key_a)
        size_b, etag_b = _upload(key_b)

        result = trigger.lambda_handler(
            {"Records": [_record(key_a, size_a, etag_a, "msg-a"), _record(key_b, size_b, etag_b, "msg-b")]},
            _CONTEXT,
        )

        assert result["batchItemFailures"] == []
        assert _execution_source_keys(state_machine_arn) == {key_a, key_b}
        assert _table(_LOCKS_TABLE).get_item(Key={"pipeline_prefix": "pipeline-a"}).get("Item") is not None
        assert _table(_LOCKS_TABLE).get_item(Key={"pipeline_prefix": "pipeline-b"}).get("Item") is not None
