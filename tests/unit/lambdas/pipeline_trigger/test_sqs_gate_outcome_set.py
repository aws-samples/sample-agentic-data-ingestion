# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: `_process_sqs_record`'s three gates produce three DIFFERENT outcomes.

p54 R10.1, R10.5. Each gate is already tested on its own — `test_concurrency_control.py`
(`TestQueuedRecordTTL`, `:387`) pins the placeholder's `ttl`,
`test_sqs_partial_batch_failure.py:442-449` pins that a concurrency rejection still writes
a `QUEUED` row, and `test_sqs_partial_batch_flow.py:232` pins that a real row appears and
is removed again. **Nothing pins the three outcomes as a set**, so nothing would notice a
change that made two gates behave alike — which is exactly what a redelivery-budget or
gate-ordering change is liable to do.

This file adds the two assertions those three do not make, and duplicates none of them:

1. **The outcome table, compared in one equality.** Gate → (raises?, placeholder written?,
   placeholder deleted?) for all three gates at once:

   | gate (`lambda_function.py`)       | raises | placeholder written | placeholder deleted |
   |-----------------------------------|--------|---------------------|---------------------|
   | duplicate content (`:1261-1270`)  | **no** | no                  | **yes** (`:1269`)   |
   | concurrency saturation (`:1275`)  | yes    | **yes** (`:1276`)   | no                  |
   | lock contention (`:1294`)         | yes    | **yes** (`:1295`)   | no                  |

   The duplicate row is the one that must never move. `_process_sqs_record`'s own docstring
   (`:1199-1202`) states it: *"Returning normally reports the record as **successful**, and
   that deliberately includes a duplicate-content skip (R9.2)"* — and `LGR-21a1b5b-3`
   (template / ratified) decided it, because a skip reaching `batchItemFailures` *"would
   redeliver a message whose work is done and eventually DLQ a correct decision"*. A skip is
   a SUCCESS.

   The other two rows are the deferral, and the docstring is equally explicit about why they
   are the *same* outcome for *different* reasons (`:1212-1220`): concurrency saturation is
   a fleet-wide condition re-read per record, lock contention is per-pipeline by definition.
   Both raise, and the raise is the deferral.

2. **The Queued_Placeholder's eight attributes, and only eight** (`_track_queued_file`,
   `:681-692`). Both writers produce the same eight; nothing today asserts the shape.

   **`started_at` (`:688`) is load-bearing and is asserted as such.** It is why these rows
   appear in `RunsByTimeIndex` and therefore reach `_split_derive_and_filter` at all, so
   `LGR-7d19eca-2`'s sparseness invariant — *"a row with no `started_at` is absent from it
   silently"* — does **not** apply to them. Drop `started_at` and the placeholder stops
   being readable by the dashboard without any test failing for that reason.

Expectations are derived from the docstring at `:1187-1230` and from R10, not from current
output. `lambda_function.py` is not modified by this file.

Validates: Requirements 10.1, 10.5
"""

import hashlib
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
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

_LOCKS_TABLE = "agentic_data_ingestion_pipeline_locks"
_PROCESSED_TABLE = "test-processed-files"

_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
    "MAX_CONCURRENT_EXECUTIONS": "10",
    "STATE_TABLE": "test-state",
    "EXECUTIONS_TABLE": "test-executions",
    "LOCKS_TABLE": _LOCKS_TABLE,
    "PROCESSED_FILES_TABLE": _PROCESSED_TABLE,
    "SOURCE_BUCKET": "src-bucket",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

#: Single-part ETag (the content MD5); bare/unquoted is the EventBridge spelling.
_ETAG = "d41d8cd98f00b204e9800998ecf8427e"
_SIZE = 1024

_SOURCE_KEY = "mynewtesting/customer_data_7.csv"
_PIPELINE_PREFIX = "mynewtesting"
_FILE_NAME = "customer_data_7.csv"
_BUCKET = "src-bucket"

#: The eight attributes `_track_queued_file` writes (`lambda_function.py:683-690`).
_PLACEHOLDER_ATTRIBUTES = frozenset(
    {
        "pipeline_id",
        "run_id",
        "status",
        "file_name",
        "source_key",
        "started_at",
        "current_step",
        "ttl",
    }
)


def _queued_run_id(source_key: str) -> str:
    """`queued-{sha256(source_key)[:16]}` — the content-addressed placeholder id."""
    return f"queued-{hashlib.sha256(source_key.encode()).hexdigest()[:16]}"


@pytest.fixture(autouse=True)
def _env():
    with patch.dict(os.environ, _ENV):
        yield


def _load_handler(*, running: int, duplicate: bool, lock_held: bool):
    """Load pipeline_trigger with the three tables independently addressable.

    Per ``LGR-b7d5490-1`` this directory loads the module through
    ``importlib.util.spec_from_file_location`` after dropping any cached
    ``lambda_function`` — several Lambdas share that module name and a plain import
    cross-contaminates. Same loader shape as ``test_sqs_partial_batch_failure.py``.
    """
    locks = MagicMock()
    locks.put_item.return_value = {}
    if lock_held:
        locks.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "held"}},
            "PutItem",
        )

    executions = MagicMock()

    processed = MagicMock()
    # `find_processed_file` reads `.get_item(...).get("Item")`; a row means "this
    # content already completed a successful run for this pipeline".
    processed.get_item.return_value = {"Item": {"run_id": "run-earlier"}} if duplicate else {}

    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": [{}] * running}
    sfn.exceptions = MagicMock()
    sfn.exceptions.ExecutionAlreadyExists = type("_ExecutionAlreadyExists", (Exception,), {})
    sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}

    dynamodb = MagicMock()

    def _table(name):
        if name == _LOCKS_TABLE:
            return locks
        if name == _PROCESSED_TABLE:
            return processed
        return executions

    dynamodb.Table.side_effect = _table

    with (
        patch.dict(os.environ, _ENV),
        patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
        patch("boto3.client", return_value=sfn),
        patch("boto3.resource", return_value=dynamodb),
    ):
        sys.modules.pop("lambda_function", None)
        spec = importlib.util.spec_from_file_location("lambda_function", os.path.join(LAMBDA_DIR, "lambda_function.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    mod.sfn_client = sfn
    mod.locks_table = locks
    mod.executions_table = executions
    mod.processed_files_table = processed
    return mod, executions


def _record(source_key: str = _SOURCE_KEY) -> dict:
    """One SQS record wrapping an EventBridge S3 Object Created notification."""
    body = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": _BUCKET},
            "object": {"key": source_key, "size": _SIZE, "etag": _ETAG},
        },
    }
    return {"messageId": "m-1", "body": json.dumps(body)}


def _placeholder_writes(executions: MagicMock) -> list[dict]:
    """Every `put_item` whose Item is a QUEUED placeholder.

    Filtered on ``status`` because the duplicate path ALSO writes to this table — a
    ``SKIPPED`` Skipped_Run_Record via ``_record_duplicate_skip`` — and counting raw
    ``put_item`` calls would read that as a placeholder.
    """
    writes = []
    for call in executions.put_item.call_args_list:
        item = call.kwargs.get("Item") or (call.args[0] if call.args else {})
        if isinstance(item, dict) and item.get("status") == "QUEUED":
            writes.append(item)
    return writes


def _placeholder_deletes(executions: MagicMock, source_key: str = _SOURCE_KEY) -> list[dict]:
    """Every `delete_item` addressed at THIS key's content-addressed placeholder."""
    expected = _queued_run_id(source_key)
    deletes = []
    for call in executions.delete_item.call_args_list:
        key = call.kwargs.get("Key") or (call.args[0] if call.args else {})
        if isinstance(key, dict) and key.get("run_id") == expected:
            deletes.append(key)
    return deletes


def _probe(*, running: int, duplicate: bool, lock_held: bool) -> tuple[dict, MagicMock]:
    """Drive one gate and report its outcome as three booleans.

    `raises` is the deferral signal: `_handle_sqs_batch` turns it into this record's
    `batchItemFailures` entry, and returning normally reports the record SUCCESSFUL.
    """
    mod, executions = _load_handler(running=running, duplicate=duplicate, lock_held=lock_held)
    raised = False
    try:
        mod._process_sqs_record(_record())
    except Exception:
        raised = True
    outcome = {
        "raises": raised,
        "placeholder_written": bool(_placeholder_writes(executions)),
        "placeholder_deleted": bool(_placeholder_deletes(executions)),
    }
    return outcome, executions


#: The three gates, each as the conditions that reach it and nothing else.
_GATES = {
    # `:1261` — a ledger row exists for this content, so nothing is going to run.
    "duplicate_content": {"running": 0, "duplicate": True, "lock_held": False},
    # `:1275` — running_count >= MAX_CONCURRENT_EXECUTIONS (10 per _ENV).
    "concurrency_saturation": {"running": 10, "duplicate": False, "lock_held": False},
    # `:1294` — the conditional put on the locks table fails: someone holds it.
    "lock_contention": {"running": 0, "duplicate": False, "lock_held": True},
}


class TestTheThreeGateOutcomesAreThreeDifferentOutcomes:
    """R10.1, R10.5. The set, not the three members separately."""

    def test_the_whole_outcome_table_is_what_it_is(self):
        """One equality over all three gates, so a change collapsing two of them fails here.

        Derived from `_process_sqs_record`'s docstring (`:1199-1220`) and `LGR-21a1b5b-3`,
        not from current output.
        """
        observed = {name: _probe(**conditions)[0] for name, conditions in _GATES.items()}

        assert observed == {
            # A skip is a SUCCESS: `return`, never `raise` (`:1270`). The placeholder is
            # cleared (`:1269`) because the file will never process, so leaving the row
            # would be a lie until its TTL expired.
            "duplicate_content": {
                "raises": False,
                "placeholder_written": False,
                "placeholder_deleted": True,
            },
            # Deferral: the placeholder is written (`:1276`) and THEN the raise (`:1290`).
            "concurrency_saturation": {
                "raises": True,
                "placeholder_written": True,
                "placeholder_deleted": False,
            },
            # Deferral: the placeholder is written (`:1295`) and THEN the raise (`:1302`).
            "lock_contention": {
                "raises": True,
                "placeholder_written": True,
                "placeholder_deleted": False,
            },
        }

    def test_exactly_one_of_the_three_gates_does_not_defer(self):
        """The set's shape stated independently of which gate is which.

        Two deferrals and one success. If a future change made the duplicate gate raise,
        or made a contention gate return, this fails even if the table above were edited
        to match — the counts are the invariant R10.1 cares about.
        """
        outcomes = [_probe(**conditions)[0] for conditions in _GATES.values()]

        assert sum(1 for o in outcomes if o["raises"]) == 2
        assert sum(1 for o in outcomes if not o["raises"]) == 1
        # A written placeholder and a deferral are the same event, never independent.
        for outcome in outcomes:
            assert outcome["placeholder_written"] == outcome["raises"]
            assert not (outcome["placeholder_written"] and outcome["placeholder_deleted"])


class TestTheQueuedPlaceholdersEightAttributes:
    """`_track_queued_file:681-692`. Both deferral gates write the same eight."""

    @pytest.mark.parametrize("gate", ["concurrency_saturation", "lock_contention"])
    def test_the_placeholder_carries_exactly_the_eight_attributes(self, gate):
        """Eight, and only eight — a widened row is a different contract."""
        _outcome, executions = _probe(**_GATES[gate])
        writes = _placeholder_writes(executions)

        assert len(writes) == 1, "one deferral writes one placeholder"
        assert frozenset(writes[0]) == _PLACEHOLDER_ATTRIBUTES

    @pytest.mark.parametrize("gate", ["concurrency_saturation", "lock_contention"])
    def test_the_eight_values_are_what_they_are(self, gate):
        """`run_id` is content-addressed, so a re-delivery overwrites rather than piles up."""
        _outcome, executions = _probe(**_GATES[gate])
        item = _placeholder_writes(executions)[0]

        assert item["pipeline_id"] == _PIPELINE_PREFIX
        assert item["run_id"] == _queued_run_id(_SOURCE_KEY)
        assert item["status"] == "QUEUED"
        assert item["file_name"] == _FILE_NAME
        assert item["source_key"] == _SOURCE_KEY
        assert item["current_step"] == "queued_trigger"
        assert isinstance(item["ttl"], int)
        assert item["ttl"] > int(time.time())

    @pytest.mark.parametrize("gate", ["concurrency_saturation", "lock_contention"])
    def test_started_at_is_present_and_is_a_timestamp(self, gate):
        """Load-bearing, and the reason `LGR-7d19eca-2` does not apply to these rows.

        `RunsByTimeIndex` is sparse — a row with no `started_at` is absent from it
        silently — so this attribute is what makes the placeholder reach
        `_split_derive_and_filter` and therefore the dashboard at all. Asserted as
        present AND parseable, because an empty string is present and still unindexable.
        """
        _outcome, executions = _probe(**_GATES[gate])
        item = _placeholder_writes(executions)[0]

        assert "started_at" in item
        assert item["started_at"]
        # Raises rather than returning a sentinel if the value stops being ISO-8601.
        assert datetime.fromisoformat(item["started_at"]) is not None
