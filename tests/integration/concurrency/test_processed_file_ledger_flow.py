# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Processed_File_Ledger end to end through one real DynamoDB table (p30 R9.4).

WHAT THIS LEVEL PROVES THAT A UNIT TEST CANNOT
----------------------------------------------
The ledger has **two producers of the same fingerprint, reading the ETag from
different places with different quoting**:

===================================  ==========================  ==============
Producer                             ETag form                   Size field
===================================  ==========================  ==============
``pipeline_trigger`` (SQS path)       EventBridge detail, **bare**  ``object.size``
``pipeline_trigger`` (manual path)    ``list_objects_v2``, quoted   ``Size``
``file_validation`` (the writer)      ``head_object``, quoted       ``ContentLength``
===================================  ==========================  ==============

``normalize_etag`` reconciles them. If it ever stopped doing so the ledger would
simply never match: no exception, no failing assertion, dedup quietly off. Unit
tests on either side cannot catch that, because each one checks a fingerprint
against *its own mock's* spelling — they agree with the mock, not with each
other. Only a test where ``file_validation`` writes and ``pipeline_trigger``
reads **one real table** closes that gap, so that agreement is the point of
these tests rather than an incidental step in them.

The same silent-divergence argument applies to the row's partition key: the
trigger reads the ledger under ``_pipeline_prefix(source_key)`` while
``job_completion`` writes it under the state row's ``original_pipeline_id``. Both
are asserted equal here from values the production code produced, not from
constants this file chose.

Also asserted, because they are the timing property the whole design turns on
(R9.4 and 4.7's note):

* the manual override works **after** a row has been recorded — the ledger must
  not block a deliberate Run Now;
* a **failed** run leaves **no** row, so the same file stays re-submittable;
* the TTL survives the DynamoDB round trip as a future epoch (R9.4's first
  clause), which a mocked ``put_item`` cannot show.

ON ``@mock_aws`` IN THIS FILE
-----------------------------
``tests/unit/test_no_duplicate_mocking_prop.py`` fences direct ``mock_aws()``
usage to ``tests/unit`` and ``tests/contracts`` (its ``SCAN_DIRS``). The
integration layer is deliberately outside that fence and builds its own
infrastructure — see ``test_pipeline_deduplication.py`` in this directory, whose
shape this file follows. The shared ``mock_dynamodb_tables`` fixture creates
neither the ledger table nor the pipeline-locks table, so it could not serve this
flow as it stands.

Validates: Requirements 9.4 (R9.5's integration clause: "identical re-upload
skipped, manual override permitted")
"""

import contextlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, NonCallableMock

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _PROJECT_ROOT / "shared"
_TRIGGER_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_pipeline_trigger"
_FILE_VALIDATION_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_file_validation"
_JOB_COMPLETION_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_job_completion"

_REGION = "us-east-1"
_BUCKET = "agentic-data-ingestion-source"
_PIPELINE = "orders"
_KEY = f"{_PIPELINE}/data.csv"
_BODY = b"id,name\n1,alice\n2,bob\n"

# Lambda context. Deliberately None rather than a MagicMock: file_validation
# reads `getattr(context, "execution_arn", None)`, and on a MagicMock that
# auto-creates a child mock which then goes into the state row and blows up
# botocore's serializer. A real Lambda context has no such attribute, so None is
# the faithful stand-in. None of the three handlers uses the context otherwise.
_CONTEXT = None

_STATE_TABLE = "agentic_data_ingestion_state"
_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
_EXECUTIONS_TABLE = "agentic_data_ingestion_executions"
_LOCKS_TABLE = "agentic_data_ingestion_pipeline_locks"
_LEDGER_TABLE = "agentic_data_ingestion_processed_files"

# Three Lambdas share the module name ``lambda_function``; each is loaded under
# its own sys.modules alias so they coexist inside one test. Cleared after every
# test (the directory conftest only clears ``lambda_function``).
_ALIASES = ("lambda_function", "fv_lambda_function", "jc_lambda_function")


@pytest.fixture(autouse=True)
def _clear_lambda_modules():
    yield
    for alias in _ALIASES:
        sys.modules.pop(alias, None)


# Shared modules the three Lambdas under test import at module level. Any of
# these may already be a Mock in ``sys.modules`` — see _real_shared_modules.
_SHARED_IMPORTS = (
    "artifact_schema",
    "config",
    "decimal_utils",
    "dynamo_typing",
    "env_utils",
    "metrics_facade",
    "processed_file_ledger",
    "rerun_contract",
    "step_errors",
    "step_names",
)


@contextlib.contextmanager
def _real_shared_modules():
    """Load against the real shared modules, then put back any sibling's mocks.

    ``tests/integration/approval_flow/test_approval_system.py`` assigns Mocks into
    ``sys.modules`` for shared modules at IMPORT time (``artifact_schema``,
    ``env_utils``, ``metrics_facade`` …) and never restores them. Since pytest
    imports every test module during collection, those Mocks are in place before
    any test in this file runs. ``test_pipeline_deduplication.py`` never noticed —
    the trigger imports none of those names — but ``file_validation`` does, and a
    Mock ``create_output_data`` goes straight into a DynamoDB item, where it
    surfaces as an unbounded recursion inside botocore rather than as anything
    legible.

    Dropping the Mocks permanently is not an option either: later integration
    modules depend on the mocked ``env_utils.require_env`` returning defaults for
    env vars they never set. So the swap is scoped to the import itself. The
    loaded modules bind the real functions directly (``from x import y``), so
    restoring the Mocks afterwards leaves this file working and every sibling
    seeing exactly the state it saw before.
    """
    saved: dict[str, object] = {}
    for name in _SHARED_IMPORTS:
        module = sys.modules.get(name)
        if isinstance(module, NonCallableMock):
            saved[name] = module
            del sys.modules[name]
    try:
        yield
    finally:
        sys.modules.update(saved)


def _load_module(alias: str, directory: Path):
    """Load a Lambda's ``lambda_function.py`` under its own module alias.

    Called from inside the moto context so the boto3 clients the module creates
    at import time are the mocked ones.
    """
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    if "aws_lambda_powertools" not in sys.modules:
        mock_powertools = MagicMock()
        mock_powertools.Logger.return_value = MagicMock()
        sys.modules["aws_lambda_powertools"] = mock_powertools

    sys.modules.pop(alias, None)
    spec = importlib.util.spec_from_file_location(alias, str(directory / "lambda_function.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    with _real_shared_modules():
        spec.loader.exec_module(module)
    return module


def _sqs_event(key: str, size: int, etag: str, message_id: str) -> dict:
    """An SQS record wrapping an EventBridge S3 notification.

    ``detail.object.etag`` is the **bare** spelling, exactly as the S3
    notification delivers it — the whole point of the equivalence under test.
    """
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
    return {"Records": [{"messageId": message_id, "body": json.dumps(eb_event), "eventSource": "aws:sqs"}]}


def _create_infrastructure() -> str:
    """Create the SFN state machine, the four tables and the source bucket.

    Returns the state machine ARN. The ledger table is created for real rather
    than left absent: ``find_processed_file`` fails **open**, so a missing table
    would make every one of these tests pass for the wrong reason.
    """
    iam = boto3.client("iam", region_name=_REGION)
    role = iam.create_role(
        RoleName="test-sfn-role-ledger",
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
        name="test-sm-ledger",
        definition=json.dumps({"StartAt": "Pass", "States": {"Pass": {"Type": "Pass", "End": True}}}),
        roleArn=role["Role"]["Arn"],
    )

    dynamodb = boto3.resource("dynamodb", region_name=_REGION)

    # State table — the fingerprint's carrier row. Only a keyed get_item is used
    # on this path (job_completion resolves run_id + step_name), so no GSI.
    dynamodb.create_table(
        TableName=_STATE_TABLE,
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "step_name", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "step_name", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=_PIPELINES_TABLE,
        KeySchema=[{"AttributeName": "pipeline_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pipeline_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
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
    # The ledger: PK pipeline_id, SK fingerprint (data-stack.ts ProcessedFilesTable).
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
        "STATE_TABLE": _STATE_TABLE,
        "PIPELINES_TABLE": _PIPELINES_TABLE,
        "EXECUTIONS_TABLE": _EXECUTIONS_TABLE,
        "LOCKS_TABLE": _LOCKS_TABLE,
        "PROCESSED_FILES_TABLE": _LEDGER_TABLE,
        "SOURCE_BUCKET": _BUCKET,
        "AWS_DEFAULT_REGION": _REGION,
        "AWS_REGION": _REGION,
    }.items():
        monkeypatch.setenv(key, value)


def _upload(key: str = _KEY, body: bytes = _BODY) -> tuple[int, str, str]:
    """Put the object and return ``(size, quoted_etag, bare_etag)``.

    moto computes a real ETag, so the quoted/bare pair here is S3's own value
    rather than a hand-written fixture.
    """
    s3 = boto3.client("s3", region_name=_REGION)
    resp = s3.put_object(Bucket=_BUCKET, Key=key, Body=body)
    quoted = resp["ETag"]
    return len(body), quoted, quoted.strip('"')


def _running_executions(state_machine_arn: str) -> list[dict]:
    sfn = boto3.client("stepfunctions", region_name=_REGION)
    return sfn.list_executions(stateMachineArn=state_machine_arn)["executions"]


def _execution_input(execution_arn: str) -> dict:
    sfn = boto3.client("stepfunctions", region_name=_REGION)
    return json.loads(sfn.describe_execution(executionArn=execution_arn)["input"])


def _release_lock(pipeline_prefix: str) -> None:
    """Delete the pipeline lock, as the state machine's ReleaseLock step does.

    Without this the second submission would be blocked by the LOCK, and the
    test would prove nothing about the ledger.
    """
    boto3.resource("dynamodb", region_name=_REGION).Table(_LOCKS_TABLE).delete_item(
        Key={"pipeline_prefix": pipeline_prefix}
    )


def _table(name: str):
    return boto3.resource("dynamodb", region_name=_REGION).Table(name)


def _let_the_clock_tick() -> None:
    """Sleep past the current whole second before re-submitting the SAME key.

    ``_execution_name`` is ``file-{name}-{YYYYmmddTHHMMSS}-{sha256(key)[:12]}``,
    so two submissions of one key inside a single second mint the *same*
    execution name and Step Functions rejects the second with
    ``ExecutionAlreadyExists``. In production the re-submission is minutes or
    days later; here it is milliseconds. This reproduces elapsed time rather than
    papering over a defect — the collision path has its own unit coverage
    (``test_sqs_partial_batch_failure.py::test_execution_already_exists_is_per_record``).
    Only the tests that expect a SECOND execution to start need it; a duplicate
    skip returns before any name is minted.
    """
    time.sleep(1.05)


def _complete_run_1(trigger, fv, state_machine_arn: str) -> tuple[str, str, int, str, str]:
    """Drive one successful run through trigger → file_validation.

    Returns ``(run_id, pipeline_id, size, quoted_etag, bare_etag)``. The
    file_validation input is the SFN input the trigger *actually* produced, read
    back from the started execution, so nothing about the hand-off is assumed.
    """
    size, quoted_etag, bare_etag = _upload()

    result = trigger.lambda_handler(_sqs_event(_KEY, size, bare_etag, "msg-1"), _CONTEXT)
    assert result["batchItemFailures"] == [], "first submission should start a run, not come back"

    executions = _running_executions(state_machine_arn)
    assert len(executions) == 1, f"expected exactly 1 execution, got {len(executions)}"

    sfn_input = _execution_input(executions[0]["executionArn"])
    fv_result = fv.lambda_handler(sfn_input, _CONTEXT)
    assert fv_result["statusCode"] == 200

    return fv_result["run_id"], fv_result["original_pipeline_id"], size, quoted_etag, bare_etag


class TestIdenticalReuploadIsSkippedThroughOneRealTable:
    """R9.2/R9.4 end to end: the writer's quoted ETag and the reader's bare one
    resolve to one row in one real table."""

    @mock_aws
    def test_reupload_of_identical_bytes_is_skipped(self, mock_lambda_env, monkeypatch):
        state_machine_arn = _create_infrastructure()
        _set_env(monkeypatch, state_machine_arn)
        trigger = _load_module("lambda_function", _TRIGGER_DIR)
        fv = _load_module("fv_lambda_function", _FILE_VALIDATION_DIR)
        jc = _load_module("jc_lambda_function", _JOB_COMPLETION_DIR)

        run_id, pipeline_id, size, quoted_etag, bare_etag = _complete_run_1(trigger, fv, state_machine_arn)

        # The divergence this test exists for is real, not hypothetical.
        assert quoted_etag != bare_etag
        assert quoted_etag.startswith('"') and not bare_etag.startswith('"')

        # The Glue job reports SUCCEEDED: job_completion reads the fingerprint
        # off the state row file_validation wrote and writes the ledger row.
        jc._record_processed_file(run_id, pipeline_id, "SUCCEEDED")

        # The row the WRITER produced is keyed by the value the READER computes.
        reader_fingerprint = trigger.content_fingerprint(size, bare_etag)
        assert reader_fingerprint, "the bare event ETag must be fingerprintable"
        row = (
            _table(_LEDGER_TABLE)
            .get_item(Key={"pipeline_id": pipeline_id, "fingerprint": reader_fingerprint})
            .get("Item")
        )
        assert row is not None, (
            "the ledger row written from file_validation's QUOTED head_object ETag was not "
            "found under the fingerprint the trigger derives from the BARE event ETag — "
            "normalize_etag no longer reconciles the two spellings and dedup is silently off"
        )
        assert row["run_id"] == run_id
        # Partition key agreement: the trigger reads under the S3-key prefix.
        assert pipeline_id == trigger._pipeline_prefix(_KEY)

        # R9.4's TTL clause, through a real round trip (Decimal on the way back).
        now = int(time.time())
        ttl = int(row["ttl"])
        assert now < ttl <= now + 30 * 24 * 60 * 60 + 5

        # Re-upload the same bytes. The lock is released first, exactly as the
        # state machine does when a run terminates, so the skip below can only
        # come from the ledger.
        _release_lock(pipeline_id)
        result = trigger.lambda_handler(_sqs_event(_KEY, size, bare_etag, "msg-2"), _CONTEXT)

        # A skip is a SUCCESS: reporting it would return the message to SQS and
        # eventually DLQ a correct decision.
        assert result["batchItemFailures"] == []
        assert len(_running_executions(state_machine_arn)) == 1, "the duplicate started a second run"
        assert _table(_LOCKS_TABLE).get_item(Key={"pipeline_prefix": pipeline_id}).get("Item") is None, (
            "the skip left a pipeline lock behind — the pipeline would be wedged for the 24h lock TTL"
        )

    @mock_aws
    def test_the_skip_is_visible_in_run_history(self, mock_lambda_env, monkeypatch):
        """R9.2: the skip is *recorded with a reason*, not merely logged."""
        state_machine_arn = _create_infrastructure()
        _set_env(monkeypatch, state_machine_arn)
        trigger = _load_module("lambda_function", _TRIGGER_DIR)
        fv = _load_module("fv_lambda_function", _FILE_VALIDATION_DIR)
        jc = _load_module("jc_lambda_function", _JOB_COMPLETION_DIR)

        run_id, pipeline_id, size, _quoted, bare_etag = _complete_run_1(trigger, fv, state_machine_arn)
        jc._record_processed_file(run_id, pipeline_id, "SUCCEEDED")
        _release_lock(pipeline_id)

        trigger.lambda_handler(_sqs_event(_KEY, size, bare_etag, "msg-2"), _CONTEXT)

        skipped = [item for item in _table(_EXECUTIONS_TABLE).scan()["Items"] if item.get("status") == "SKIPPED"]
        assert len(skipped) == 1, f"expected one SKIPPED run-history row, got {len(skipped)}"
        assert skipped[0]["skip_reason"] == "duplicate_content"
        assert skipped[0]["previous_run_id"] == run_id
        assert skipped[0]["source_key"] == _KEY

        # Deterministic in the fingerprint: ten re-uploads overwrite ONE row.
        trigger.lambda_handler(_sqs_event(_KEY, size, bare_etag, "msg-3"), _CONTEXT)
        still_one = [i for i in _table(_EXECUTIONS_TABLE).scan()["Items"] if i.get("status") == "SKIPPED"]
        assert len(still_one) == 1


class TestManualOverrideAfterARecordedRun:
    """R9.4: the ledger must not prevent a deliberate re-run from the UI, and the
    override has to work *after* a row exists — before it, there is nothing to
    override."""

    @mock_aws
    def test_manual_run_is_blocked_then_permitted_with_acknowledgement(self, mock_lambda_env, monkeypatch):
        state_machine_arn = _create_infrastructure()
        _set_env(monkeypatch, state_machine_arn)
        trigger = _load_module("lambda_function", _TRIGGER_DIR)
        fv = _load_module("fv_lambda_function", _FILE_VALIDATION_DIR)
        jc = _load_module("jc_lambda_function", _JOB_COMPLETION_DIR)

        run_id, pipeline_id, _size, _quoted, _bare = _complete_run_1(trigger, fv, state_machine_arn)
        jc._record_processed_file(run_id, pipeline_id, "SUCCEEDED")
        _release_lock(pipeline_id)

        manual_event = {"trigger": "manual", "pipeline_id": _PIPELINE, "source_type": "S3_UPLOAD"}

        # Without acknowledgement: 409. This path reads the THIRD ETag spelling
        # (list_objects_v2's quoted form) and still finds the same row.
        blocked = trigger.lambda_handler(dict(manual_event), _CONTEXT)
        assert blocked["statusCode"] == 409
        assert blocked["skip_reason"] == "duplicate_content"
        assert len(_running_executions(state_machine_arn)) == 1

        # With acknowledgement: the run starts.
        _let_the_clock_tick()
        allowed = trigger.lambda_handler({**manual_event, "acknowledge_duplicate": True}, _CONTEXT)
        assert allowed["statusCode"] == 200, allowed
        assert len(_running_executions(state_machine_arn)) == 2
        assert _table(_LEDGER_TABLE).scan()["Count"] == 1, (
            "the override must not delete the ledger row; the next run's completion refreshes it"
        )


class TestAFailedRunLeavesNoRow:
    """4.7's timing property: the row exists only for a run that succeeded, so a
    failure leaves the file re-submittable."""

    @mock_aws
    def test_failed_run_records_nothing_and_the_reupload_runs(self, mock_lambda_env, monkeypatch):
        state_machine_arn = _create_infrastructure()
        _set_env(monkeypatch, state_machine_arn)
        trigger = _load_module("lambda_function", _TRIGGER_DIR)
        fv = _load_module("fv_lambda_function", _FILE_VALIDATION_DIR)
        jc = _load_module("jc_lambda_function", _JOB_COMPLETION_DIR)

        run_id, pipeline_id, size, _quoted, bare_etag = _complete_run_1(trigger, fv, state_machine_arn)

        # The Glue job failed. The state row (and its fingerprint) exists, so
        # this asserts the SUCCEEDED gate, not an absent fingerprint.
        assert _table(_STATE_TABLE).get_item(Key={"run_id": run_id, "step_name": "file_validation"})["Item"][
            "content_fingerprint"
        ]
        jc._record_processed_file(run_id, pipeline_id, "FAILED")

        assert _table(_LEDGER_TABLE).scan()["Count"] == 0, "a failed run must leave no ledger row"

        _release_lock(pipeline_id)
        _let_the_clock_tick()
        result = trigger.lambda_handler(_sqs_event(_KEY, size, bare_etag, "msg-2"), _CONTEXT)

        assert result["batchItemFailures"] == []
        assert len(_running_executions(state_machine_arn)) == 2, "the re-submission after a failure was skipped"
        assert not [i for i in _table(_EXECUTIONS_TABLE).scan()["Items"] if i.get("status") == "SKIPPED"]
