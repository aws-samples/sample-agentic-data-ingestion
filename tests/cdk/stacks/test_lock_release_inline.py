# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The inline LockReleaseFunction releases only the lock its execution holds.

The deployed Python is extracted verbatim from orchestration-stack.ts and run
against moto, the same way test_glue_job_state_updater_inline.py exercises its
neighbour. The sequence the first test replays is the 2026-10-11 assessment's F3
probe: an old execution releases its lock, a new execution acquires the same
key, and then the old execution's FAILED status-change event arrives late. The
unconditional delete this function used to do removed the new execution's lock
and a third acquisition succeeded; now the stale event is a no-op.

The acquire side (pipeline_trigger._acquire_pipeline_lock) stores
`execution_name` for exactly this check, and the trigger's own failed-start
release has always been holder-conditional (LGR-21a1b5b-1).
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

STACK_SOURCE = Path(__file__).resolve().parents[3] / "deploy-cdk" / "lib" / "orchestration-stack.ts"
LOCKS_TABLE = "agentic_data_ingestion_pipeline_locks"
PIPELINE = "orders-pipeline-1a2b3c4d"
OLD = "file-orders_csv-20261011T090000-000000000001"
NEW = "file-orders_csv-20261011T091500-000000000002"


def _handler_source() -> str:
    text = STACK_SOURCE.read_text(encoding="utf-8")
    start = text.index("new lambda.Function(this, 'LockReleaseFunction'")
    code_start = text.index("lambda.Code.fromInline(`", start) + len("lambda.Code.fromInline(`")
    code_end = text.index("`)", code_start)
    code = text[code_start:code_end]
    assert "${" not in code, "the inline handler contains a dollar-brace, which TypeScript interpolates"
    assert "\\" not in code, "the inline handler contains a backslash, which a template literal unescapes"
    return code


@pytest.fixture
def release(mock_dynamodb_tables, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("LOCKS_TABLE", LOCKS_TABLE)
    module = types.ModuleType("lock_release_inline")
    exec(compile(_handler_source(), "orchestration-stack.ts:LockReleaseFunction", "exec"), module.__dict__)
    return module


@pytest.fixture
def locks(mock_dynamodb_tables):
    return mock_dynamodb_tables[LOCKS_TABLE]


def _acquire(locks, execution_name: str) -> None:
    locks.put_item(
        Item={"pipeline_prefix": PIPELINE, "execution_name": execution_name, "started_at": "x", "ttl": 9_999_999_999},
        ConditionExpression="attribute_not_exists(pipeline_prefix)",
    )


def _holder(locks) -> str | None:
    item = locks.get_item(Key={"pipeline_prefix": PIPELINE}).get("Item")
    return item["execution_name"] if item else None


def _terminal_event(execution_name: str, status: str = "FAILED") -> dict:
    return {
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": {
            "name": execution_name,
            "status": status,
            "input": json.dumps({"pipeline_id": PIPELINE, "source_type": "S3_UPLOAD"}),
        },
    }


def test_a_stale_terminal_event_does_not_release_a_newer_executions_lock(release, locks):
    _acquire(locks, OLD)
    assert release.lambda_handler({"pipeline_id": PIPELINE, "execution_name": OLD}, None)["released"] is True
    _acquire(locks, NEW)

    result = release.lambda_handler(_terminal_event(OLD), None)

    assert result["released"] is False
    assert result["reason"] == "not_owner"
    assert _holder(locks) == NEW
    with pytest.raises(Exception, match="ConditionalCheckFailed"):
        _acquire(locks, "a-third-execution")


def test_the_holder_releases_through_the_in_machine_task(release, locks):
    _acquire(locks, NEW)
    result = release.lambda_handler({"pipeline_id": PIPELINE, "execution_name": NEW}, None)
    assert result["released"] is True
    assert _holder(locks) is None


def test_the_holder_releases_through_its_own_terminal_event(release, locks):
    _acquire(locks, NEW)
    result = release.lambda_handler(_terminal_event(NEW, "ABORTED"), None)
    assert result["released"] is True
    assert _holder(locks) is None


def test_a_repeated_release_is_a_no_op(release, locks):
    """The failure path releases twice: ReleaseLockFailure, then the FAILED event."""
    _acquire(locks, NEW)
    release.lambda_handler({"pipeline_id": PIPELINE, "execution_name": NEW}, None)

    again = release.lambda_handler(_terminal_event(NEW), None)

    assert again["released"] is False
    assert again["reason"] == "not_owner"
    assert _holder(locks) is None


def test_an_execution_from_before_the_deploy_still_releases(release, locks):
    """An in-flight execution's ReleaseLock* task was synthesised without execution_name."""
    _acquire(locks, OLD)
    result = release.lambda_handler({"pipeline_id": PIPELINE}, None)
    assert result["released"] is True
    assert _holder(locks) is None


def test_an_s3_source_key_still_derives_the_prefix(release, locks):
    locks.put_item(Item={"pipeline_prefix": "incoming/orders", "execution_name": NEW, "ttl": 9_999_999_999})
    result = release.lambda_handler({"source_key": "incoming/orders/file.csv", "execution_name": NEW}, None)
    assert result["released"] is True
    assert "Item" not in locks.get_item(Key={"pipeline_prefix": "incoming/orders"})


def test_an_event_with_no_key_is_skipped(release):
    assert release.lambda_handler({"execution_name": NEW}, None) == {"released": False}
