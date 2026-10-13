# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pipeline lock is released when no execution starts, and only then.

p30 R10.3: "IF start_execution fails after the pipeline lock has been acquired,
THEN THE handler SHALL release the lock before returning, so the pipeline is not
blocked for the 24-hour lock TTL."

Both polarities matter and the negative one matters more. The lock IS the
per-pipeline concurrency gate, so a fix that released it on the success path
would satisfy the letter of R10.3 while removing the deduplication the lock
exists to provide — and nothing else in the suite would notice. Every test here
that asserts a release is paired with one asserting the lock survives a
successful start.

Covers all four acquire → start windows in the trigger Lambda: the SQS
(s3_upload) path, the manual S3_UPLOAD path, the manual/scheduled JDBC path, and
the scheduled S3_UPLOAD path.

Validates: Requirements 10.3
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_pipeline_trigger",
    )
)

_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
    "MAX_CONCURRENT_EXECUTIONS": "10",
    "STATE_TABLE": "test-state",
    "EXECUTIONS_TABLE": "test-executions",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "SOURCE_BUCKET": "src-bucket",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}


@pytest.fixture(autouse=True)
def _env():
    """Keep _ENV in place for the handler call, not just for module import.

    SOURCE_BUCKET is read from os.environ at call time inside the S3_UPLOAD
    paths, so patching only around the import leaves them returning 500.
    """
    with patch.dict(os.environ, _ENV):
        yield


class _ExecutionAlreadyExists(Exception):
    """Stand-in for the botocore-generated SFN exception.

    The production code references ``sfn_client.exceptions.ExecutionAlreadyExists``
    in an ``except`` clause. On a bare MagicMock that attribute is not an
    exception class, and Python raises TypeError the moment anything inside the
    ``try`` raises — so every test that makes ``start_execution`` fail must
    install a real class. Same approach as ``test_error_paths.py``.
    """


def _load_handler(mock_sfn, mock_locks_table, mock_s3=None):
    """Load the trigger Lambda with mocked SFN / DynamoDB / S3."""
    mock_executions_table = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.side_effect = lambda name: (
        mock_locks_table if name == _ENV["LOCKS_TABLE"] else mock_executions_table
    )

    with (
        patch.dict(os.environ, _ENV),
        patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
        patch("boto3.client", return_value=mock_sfn),
        patch("boto3.resource", return_value=mock_dynamodb),
    ):
        sys.modules.pop("lambda_function", None)
        spec = importlib.util.spec_from_file_location(
            "lambda_function",
            os.path.join(LAMBDA_DIR, "lambda_function.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    # The module builds its clients at import time; rebind so the paths under
    # test use these mocks even after the patch context exits.
    mod.sfn_client = mock_sfn
    mod.locks_table = mock_locks_table
    mod.executions_table = mock_executions_table
    if mock_s3 is not None:
        # boto3.client("s3") is called inside the handler, not at import.
        mod.boto3 = MagicMock()
        mod.boto3.client.return_value = mock_s3
    return mod


def _sfn_mock(*, running=0, start_error=None):
    mock_sfn = MagicMock()
    mock_sfn.list_executions.return_value = {"executions": [{}] * running}
    mock_sfn.exceptions = MagicMock()
    mock_sfn.exceptions.ExecutionAlreadyExists = _ExecutionAlreadyExists
    if start_error is not None:
        mock_sfn.start_execution.side_effect = start_error
    else:
        mock_sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}
    return mock_sfn


def _s3_mock(key="my-pipeline/file.csv"):
    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {"Contents": [{"Key": key, "LastModified": 1}]}
    return mock_s3


def _sqs_event(bucket="src-bucket", key="my-pipeline/file.csv", message_id="msg-1"):
    body = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": bucket}, "object": {"key": key}},
    }
    # messageId as real SQS sends it. Since p30 R9.3 the SQS path reports a
    # failed record through batchItemFailures instead of raising, so the
    # assertions below check the release AND the report together: a record that
    # fails after acquiring the lock must release it before being reported,
    # otherwise the redelivery is rejected by the lock this invocation left.
    return {"Records": [{"messageId": message_id, "body": json.dumps(body)}]}


def _reported(result):
    """The messageIds the handler asked SQS to redeliver (p30 R9.3)."""
    return [f["itemIdentifier"] for f in result["batchItemFailures"]]


def _throttling_error():
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "StartExecution",
    )


def _released_keys(mock_locks_table):
    """Return the pipeline_prefix of every conditional lock release performed."""
    return [call.kwargs["Key"]["pipeline_prefix"] for call in mock_locks_table.delete_item.call_args_list]


# ---------------------------------------------------------------------------
# The lock is released when start_execution raises
# ---------------------------------------------------------------------------


class TestReleasedOnStartFailure:
    """R10.3 — every acquire → start window releases the lock on failure."""

    def test_sqs_path_releases_on_throttling(self):
        """A transient StartExecution error must not wedge the pipeline for 24 h."""
        locks = MagicMock()
        sfn = _sfn_mock(start_error=_throttling_error())
        handler = _load_handler(sfn, locks)

        result = handler.lambda_handler(_sqs_event(), None)

        assert _released_keys(locks) == ["my-pipeline"]
        # The redelivery only helps if the lock is gone by the time it arrives.
        assert _reported(result) == ["msg-1"]

    def test_sqs_path_releases_on_name_collision(self):
        """ExecutionAlreadyExists returns the record for SQS retry, lock released.

        Without the release, the retry the report exists to trigger would be
        rejected by the lock gate rather than getting a fresh timestamp.
        """
        locks = MagicMock()
        sfn = _sfn_mock(start_error=_ExecutionAlreadyExists("collision"))
        handler = _load_handler(sfn, locks)

        result = handler.lambda_handler(_sqs_event(), None)

        assert _released_keys(locks) == ["my-pipeline"]
        assert _reported(result) == ["msg-1"]

    def test_manual_s3_upload_releases_on_throttling(self):
        locks = MagicMock()
        sfn = _sfn_mock(start_error=_throttling_error())
        handler = _load_handler(sfn, locks, mock_s3=_s3_mock())

        with pytest.raises(ClientError):
            handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert _released_keys(locks) == ["my-pipeline"]

    def test_manual_s3_upload_releases_on_name_collision(self):
        """The 500 "please retry" response is only honest if the lock is gone."""
        locks = MagicMock()
        sfn = _sfn_mock(start_error=_ExecutionAlreadyExists("collision"))
        handler = _load_handler(sfn, locks, mock_s3=_s3_mock())

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 500
        assert _released_keys(locks) == ["my-pipeline"]

    def test_jdbc_releases_on_throttling(self):
        """JDBC keys the lock on pipeline_id, not an S3-derived prefix."""
        locks = MagicMock()
        sfn = _sfn_mock(start_error=_throttling_error())
        handler = _load_handler(sfn, locks)

        with pytest.raises(ClientError):
            handler.lambda_handler({"trigger": "manual", "pipeline_id": "jdbc-pipeline", "source_type": "JDBC"}, None)

        assert _released_keys(locks) == ["jdbc-pipeline"]

    def test_scheduled_releases_on_throttling(self):
        locks = MagicMock()
        sfn = _sfn_mock(start_error=_throttling_error())
        handler = _load_handler(sfn, locks, mock_s3=_s3_mock())

        with pytest.raises(ClientError):
            handler.lambda_handler({"trigger": "scheduled", "pipeline_id": "my-pipeline"}, None)

        assert _released_keys(locks) == ["my-pipeline"]


# ---------------------------------------------------------------------------
# The lock is NOT released when the execution actually starts
# ---------------------------------------------------------------------------


class TestSurvivesSuccessfulStart:
    """The lock is the concurrency gate — releasing it on success defeats it."""

    def test_sqs_path_keeps_lock(self):
        locks = MagicMock()
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks)

        result = handler.lambda_handler(_sqs_event(), None)

        assert result["batchItemFailures"] == []
        sfn.start_execution.assert_called_once()
        locks.delete_item.assert_not_called()

    def test_manual_s3_upload_keeps_lock(self):
        locks = MagicMock()
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks, mock_s3=_s3_mock())

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 200
        locks.delete_item.assert_not_called()

    def test_jdbc_keeps_lock(self):
        locks = MagicMock()
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks)

        result = handler.lambda_handler(
            {"trigger": "manual", "pipeline_id": "jdbc-pipeline", "source_type": "JDBC"}, None
        )

        assert result["statusCode"] == 200
        locks.delete_item.assert_not_called()

    def test_scheduled_keeps_lock(self):
        locks = MagicMock()
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks, mock_s3=_s3_mock())

        result = handler.lambda_handler({"trigger": "scheduled", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 200
        locks.delete_item.assert_not_called()

    def test_no_release_when_the_lock_was_never_acquired(self):
        """Contention rejects before acquisition; nothing to release."""
        locks = MagicMock()
        locks.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks)

        result = handler.lambda_handler(_sqs_event(), None)

        assert _reported(result) == ["msg-1"]
        # Releasing here would delete the lock held by the RUNNING execution.
        locks.delete_item.assert_not_called()


# ---------------------------------------------------------------------------
# Release mechanics: holder-conditional, idempotent, never masks the cause
# ---------------------------------------------------------------------------


class TestReleaseMechanics:
    """The release must not become a way to delete someone else's lock."""

    def test_release_is_conditional_on_the_holder(self):
        """Only the invocation that wrote the row may delete it.

        The other release paths (the state machine's lock-release Lambda,
        pipeline delete) delete unconditionally because they run after the
        execution they own has ended. This one runs while other triggers for
        the same pipeline may be live, so it checks execution_name.
        """
        locks = MagicMock()
        sfn = _sfn_mock(start_error=_throttling_error())
        handler = _load_handler(sfn, locks)

        handler.lambda_handler(_sqs_event(), None)

        acquired_name = locks.put_item.call_args.kwargs["Item"]["execution_name"]
        delete_kwargs = locks.delete_item.call_args.kwargs
        assert delete_kwargs["ConditionExpression"] == "execution_name = :execution_name"
        assert delete_kwargs["ExpressionAttributeValues"] == {":execution_name": acquired_name}

    def test_conditional_check_failure_is_not_an_error(self):
        """A lock already gone, or now held by someone else, is a no-op."""
        locks = MagicMock()
        locks.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "not holder"}},
            "DeleteItem",
        )
        sfn = _sfn_mock(start_error=_throttling_error())
        handler = _load_handler(sfn, locks)

        assert handler._release_pipeline_lock("my-pipeline", "exec-1") is False

        # The original StartExecution failure still decides the record's outcome
        # and is still what gets logged. Since p30 R9.3 it reaches the operator
        # through the partial-batch report and the log rather than as a raise.
        result = handler.lambda_handler(_sqs_event(), None)
        assert _reported(result) == ["msg-1"]
        assert "ThrottlingException" in " ".join(str(c) for c in handler.logger.warning.call_args_list)

    def test_release_failure_does_not_replace_the_original_cause(self):
        """The operator needs the StartExecution failure, not a cleanup error."""
        locks = MagicMock()
        locks.delete_item.side_effect = RuntimeError("DynamoDB unavailable")
        sfn = _sfn_mock(start_error=_throttling_error())
        handler = _load_handler(sfn, locks)

        result = handler.lambda_handler(_sqs_event(), None)

        # The record is still reported despite the cleanup failing, and the
        # reported cause is the StartExecution error rather than "DynamoDB
        # unavailable".
        assert _reported(result) == ["msg-1"]
        logged = " ".join(str(c) for c in handler.logger.warning.call_args_list)
        assert "ThrottlingException" in logged

    def test_release_is_idempotent(self):
        """Releasing twice is safe — the second call's condition simply fails."""
        locks = MagicMock()
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks)

        assert handler._release_pipeline_lock("my-pipeline", "exec-1") is True
        locks.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "gone"}},
            "DeleteItem",
        )
        assert handler._release_pipeline_lock("my-pipeline", "exec-1") is False

    def test_non_conditional_client_error_is_swallowed(self):
        """A throttled DeleteItem must not become the reported failure."""
        locks = MagicMock()
        locks.delete_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
            "DeleteItem",
        )
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks)

        assert handler._release_pipeline_lock("my-pipeline", "exec-1") is False


# ---------------------------------------------------------------------------
# The other window between acquisition and a started execution
# ---------------------------------------------------------------------------


class TestContractViolationWindow:
    """build_trigger_input runs after acquisition, so it wedges the same way.

    Not named by R10.3, which says "start_execution" — but it sits inside the
    same acquire → start window and produces the identical 24 h wedge, so the
    invariant enforced here is the whole window: the lock survives only a
    started execution.
    """

    def test_sqs_path_releases_on_contract_violation(self):
        """The record still reaches the DLQ; the lock does not survive it.

        Since p30 R9.3 the route to the DLQ is the record's own
        ``batchItemFailures`` entry plus ``maxReceiveCount``, rather than a raise
        that took the whole batch with it. The release requirement is unchanged.
        """
        locks = MagicMock()
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks)
        handler.build_trigger_input = MagicMock(side_effect=ValueError("bad shape"))

        result = handler.lambda_handler(_sqs_event(), None)

        assert _released_keys(locks) == ["my-pipeline"]
        assert _reported(result) == ["msg-1"]
        sfn.start_execution.assert_not_called()

    def test_manual_s3_upload_releases_on_contract_violation(self):
        locks = MagicMock()
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks, mock_s3=_s3_mock())
        handler.build_trigger_input = MagicMock(side_effect=ValueError("bad shape"))

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 400
        assert _released_keys(locks) == ["my-pipeline"]

    def test_jdbc_releases_on_contract_violation(self):
        locks = MagicMock()
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks)
        handler.build_trigger_input = MagicMock(side_effect=ValueError("bad shape"))

        result = handler.lambda_handler(
            {"trigger": "manual", "pipeline_id": "jdbc-pipeline", "source_type": "JDBC"}, None
        )

        assert result["statusCode"] == 400
        assert _released_keys(locks) == ["jdbc-pipeline"]

    def test_scheduled_releases_on_contract_violation(self):
        locks = MagicMock()
        sfn = _sfn_mock()
        handler = _load_handler(sfn, locks, mock_s3=_s3_mock())
        handler.build_trigger_input = MagicMock(side_effect=ValueError("bad shape"))

        result = handler.lambda_handler({"trigger": "scheduled", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 400
        assert _released_keys(locks) == ["my-pipeline"]
