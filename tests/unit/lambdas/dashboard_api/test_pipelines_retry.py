# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for retry failed execution (Req 2, AC 1–10).

Validates: Requirements 2.1–2.10
Tests the retry_run function including precondition checks,
SFN input construction, execution record creation, and cross-referencing.

p38-retry-run-identity (task 3.7, design §4.9) corrected this file. Under the
continuation model a retry executes AND reports under the Original_Run's id:
there is no fabricated `retry-{run_id[:40]}-{hash}` run id, no phantom
executions row, and no forward `retried_by_run_id` pointer. Three tests here
pinned the broken behaviour as correct and are corrected below; each carries a
comment naming what it asserted before, so the change reads as a deliberate
reversal rather than as a regression (the convention `LGR-9e76249-10` /
`LGR-60c4985-6` set for amending a template assertion in place).

`test_correct_sfn_input_construction` is **deliberately unchanged** — see the
comment block above it. Its `sfn_input["run_id"] == RUN_ID` was always right
and is the assertion proving the Executing_Run_Id does not move.
"""

import contextlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "agentic-data-ingestion-source",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:agentic-data-ingestion-pipeline",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add handler package to path
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"),
)

# `shared/` and the Lambda package are both on sys.path by the time this module
# is imported — this directory's conftest inserts them at collection start.
try:
    from handlers.pipelines import retry_run

    import rerun_contract
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINE_ID = "test-pipeline-abc12345"
RUN_ID = "run-abc123"

# A retry of a retry, at its observed live length (47 chars). Longer than the
# self-imposed `[:40]` slice p38 §4.1 deletes, so R6.2's latent corruption is
# reachable through it: the old code would have reported
# `retry-run-2026-08-09-15-38-19-5c5f5f8a-5`, an id that still parses and
# identifies nothing.
LONG_RUN_ID = "retry-run-2026-08-09-15-38-19-5c5f5f8a-537b2303"


class _ExecutionAlreadyExists(Exception):
    """Stand-in for `sfn_client.exceptions.ExecutionAlreadyExists`.

    botocore generates that class off the service model at client-construction
    time, so a MagicMock client does not carry a real exception class and
    `except mock.exceptions.ExecutionAlreadyExists` would raise TypeError
    instead of matching. Tests exercising the §4.8 handler bind this class onto
    the mock's `exceptions` namespace and raise it from `start_execution`.
    """


# ---------------------------------------------------------------------------
# Mock tables and clients
# ---------------------------------------------------------------------------
mock_pipelines_table = MagicMock()
mock_state_table = MagicMock()
mock_executions_table = MagicMock()
mock_approvals_table = MagicMock()
mock_locks_table = MagicMock()
mock_dynamodb = MagicMock()

_table_map = {
    "agentic_data_ingestion_pipeline_locks": mock_locks_table,
}
mock_dynamodb.Table.side_effect = lambda name: _table_map.get(name, MagicMock())


@pytest.fixture(autouse=True)
def _reset_mocks(monkeypatch):
    """Reset all mocks before each test and wire them into the module."""
    import importlib

    # The reloads below re-run each module's `from env_utils import ...`, so this
    # file's stub has to be in sys.modules while they run. Module scope restores
    # it after the first import (LGR-c58f276-4); install it again per test here.
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)

    # P21 Task 7.2: retry_run now lives in handlers.pipelines_runs. Reload the
    # owning module (plus common + façade) to shed mock-tainted bindings other
    # test files may have installed. Guard each reload so a missing namespace
    # parent (a pure-isolation test-ordering artefact) doesn't error the fixture.
    for _name in ("handlers.common", "handlers.pipelines_runs", "handlers.pipelines"):
        _mod = sys.modules.get(_name)
        if _mod is not None and "handlers" in sys.modules:
            try:
                importlib.reload(_mod)
            except ImportError:
                pass

    from handlers import pipelines as _fresh_pipelines
    from handlers import pipelines_runs as _fresh_runs

    global pipelines, retry_run
    pipelines = _fresh_pipelines
    retry_run = _fresh_runs.retry_run

    mock_pipelines_table.reset_mock()
    mock_state_table.reset_mock()
    mock_executions_table.reset_mock()
    mock_approvals_table.reset_mock()
    mock_locks_table.reset_mock()
    mock_dynamodb.reset_mock()
    mock_dynamodb.Table.side_effect = lambda name: _table_map.get(name, MagicMock())

    # Default: no items
    mock_state_table.query.return_value = {"Items": []}
    mock_executions_table.query.return_value = {"Items": []}
    # Pipeline record backing the rerun contract's source resolution
    mock_pipelines_table.get_item.return_value = {
        "Item": {
            "pipeline_id": PIPELINE_ID,
            "source_type": "S3_UPLOAD",
            "source_bucket": "test-source-bucket",
        }
    }

    # Wire mocks into the module. P21 Task 7.2: retry_run now lives in
    # handlers.pipelines_runs, so wire the owning module too (module
    # references only — behaviour-preserving).
    for _mod in (pipelines, _fresh_runs):
        _mod.pipelines_table = mock_pipelines_table
        _mod.state_table = mock_state_table
        _mod.executions_table = mock_executions_table
        _mod.approvals_table = mock_approvals_table
        _mod.dynamodb = mock_dynamodb
    yield


def _failed_state_entries(run_id: str = RUN_ID):
    """Return state entries for a failed run with one completed and one failed step."""
    return [
        {
            "run_id": run_id,
            "step_name": "file_validation",
            "status": "success",
            "pipeline_id": PIPELINE_ID,
            "original_pipeline_id": PIPELINE_ID,
            "output_data": {"validated": True, "file_size": 1024},
            "timestamp": "2024-01-01T00:00:00Z",
        },
        {
            "run_id": run_id,
            "step_name": "schema_extract",
            "status": "error",
            "pipeline_id": PIPELINE_ID,
            "original_pipeline_id": PIPELINE_ID,
            "error_message": "Schema extraction failed",
            "output_data": {},
            "timestamp": "2024-01-01T00:01:00Z",
        },
    ]


def _running_state_entries():
    """Return state entries for a running (non-failed) run."""
    return [
        {
            "run_id": RUN_ID,
            "step_name": "file_validation",
            "status": "success",
            "pipeline_id": PIPELINE_ID,
            "original_pipeline_id": PIPELINE_ID,
            "output_data": {"validated": True},
            "timestamp": "2024-01-01T00:00:00Z",
        },
    ]


def _failed_execution_record(run_id: str = RUN_ID):
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": "FAILED",
        "execution_id": "arn:aws:states:us-east-1:123456789012:execution:test:run-1",
    }


def _arrange_failed_run(run_id: str = RUN_ID, exec_items=None):
    """Wire the three reads a successful retry performs, and return nothing.

    `exec_items` defaults to the run's own FAILED executions row. Pass `[]` to
    exercise the branch where the pre-flight executions read finds no row —
    `retry_run` then legitimately omits the `attempts` SET clauses rather than
    writing them from nothing.
    """
    mock_state_table.query.return_value = {"Items": _failed_state_entries(run_id)}
    mock_executions_table.query.return_value = {
        "Items": [_failed_execution_record(run_id)] if exec_items is None else exec_items
    }
    mock_locks_table.get_item.return_value = {}


def _sole_update_kwargs():
    """The kwargs of the one executions-table `update_item` a retry issues."""
    update_calls = mock_executions_table.update_item.call_args_list
    assert len(update_calls) == 1, f"expected exactly one update_item, got {len(update_calls)}"
    return update_calls[0][1]


@contextlib.contextmanager
def _temporary_side_effect(mock_method, effect):
    """Install a side effect on a module-level mock and restore it afterwards.

    The autouse fixture calls `reset_mock()` with its defaults, which clears
    recorded calls but NOT `side_effect`. A side effect assigned in place would
    therefore leak into every test collected after this one, so every test that
    needs one takes it through here.
    """
    previous = mock_method.side_effect
    mock_method.side_effect = effect
    try:
        yield
    finally:
        mock_method.side_effect = previous


class TestRetryRunNotFound:
    """Validates: Requirement 2.1 — 404 for missing run."""

    def test_returns_404_when_run_not_found(self):
        mock_state_table.query.return_value = {"Items": []}

        result = retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 404
        assert "not found" in body["error"].lower()


class TestRetryRunNotFailed:
    """Validates: Requirement 2.2 — 409 for non-failed run."""

    def test_returns_409_for_running_run(self):
        mock_state_table.query.return_value = {"Items": _running_state_entries()}
        mock_executions_table.query.return_value = {
            "Items": [
                {
                    "pipeline_id": PIPELINE_ID,
                    "run_id": RUN_ID,
                    "status": "RUNNING",
                }
            ]
        }

        result = retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 409
        assert "not in a failed state" in body["error"].lower()

    def test_returns_409_for_succeeded_run(self):
        entries = _running_state_entries()
        mock_state_table.query.return_value = {"Items": entries}
        mock_executions_table.query.return_value = {
            "Items": [
                {
                    "pipeline_id": PIPELINE_ID,
                    "run_id": RUN_ID,
                    "status": "SUCCEEDED",
                }
            ]
        }

        result = retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 409


class TestRetryRunLockedPipeline:
    """Validates: Requirement 2.9 — 409 for locked pipeline."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_returns_409_when_pipeline_locked(self, mock_boto_client):
        mock_state_table.query.return_value = {"Items": _failed_state_entries()}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}

        # Pipeline lock exists
        mock_locks_table.get_item.return_value = {
            "Item": {"pipeline_prefix": PIPELINE_ID, "execution_name": "some-exec"}
        }

        result = retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 409
        assert "locked" in body["error"].lower()


class TestRetryRunNoFailedStep:
    """Validates: Requirement 2.8 — 400 when no failed step found."""

    def test_returns_400_when_no_failed_step(self):
        # All steps succeeded but execution record says FAILED
        entries = [
            {
                "run_id": RUN_ID,
                "step_name": "file_validation",
                "status": "success",
                "pipeline_id": PIPELINE_ID,
                "original_pipeline_id": PIPELINE_ID,
                "output_data": {"validated": True},
            },
        ]
        mock_state_table.query.return_value = {"Items": entries}
        mock_executions_table.query.return_value = {
            "Items": [
                {
                    "pipeline_id": PIPELINE_ID,
                    "run_id": RUN_ID,
                    "status": "FAILED",
                }
            ]
        }
        # No lock
        mock_locks_table.get_item.return_value = {}

        result = retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 400
        assert "no failed step" in body["error"].lower()


class TestRetryRunSuccess:
    """Validates: Requirements 2.3–2.7, 2.10 — successful retry flow."""

    # ─────────────────────────────────────────────────────────────────────
    # DELIBERATELY UNCHANGED by p38 task 3.7 — not an oversight.
    #
    # `test_correct_sfn_input_construction` below is byte-identical to its
    # pre-p38 form, and design §4.9's table says so explicitly. It was already
    # right: its `sfn_input["run_id"] == RUN_ID` assertion is the one that
    # proves the Executing_Run_Id does NOT move under the continuation model.
    # Three of its siblings changed because they pinned the fabricated id as
    # correct; this one changed nothing because it never did.
    #
    # A reviewer scanning the diff will see three corrected tests and this one
    # untouched. That asymmetry is the point: p38 deletes the second identifier
    # rather than moving the first, so the assertion about the first must stay
    # exactly as it was to be evidence of anything.
    # ─────────────────────────────────────────────────────────────────────
    @patch("handlers.pipelines_runs.boto3.client")
    def test_correct_sfn_input_construction(self, mock_boto_client):
        """Validates: Req 2.4 — SFN input includes run_id, pipeline_id, retry_from_step, and completed step data."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        mock_state_table.query.return_value = {"Items": _failed_state_entries()}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        # No lock
        mock_locks_table.get_item.return_value = {}

        result = retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["success"] is True
        assert body["pipeline_id"] == PIPELINE_ID
        # P28 R9.2: the rerun contract normalizes the auto-detected failed
        # step ("schema_extract" state row) to its SFN state id
        assert body["retry_from_step"] == "SchemaExtract"

        # Verify SFN was called with correct input
        mock_sfn.start_execution.assert_called_once()
        call_kwargs = mock_sfn.start_execution.call_args[1]
        sfn_input = json.loads(call_kwargs["input"])

        assert sfn_input["run_id"] == RUN_ID
        assert sfn_input["pipeline_id"] == PIPELINE_ID
        assert sfn_input["retry_from_step"] == "SchemaExtract"
        assert sfn_input["trigger"] == "retry"
        assert sfn_input["triggered_by"] == "retry"
        # Full trigger contract (P28 R9.1): source location always present
        assert sfn_input["source_bucket"] == "test-source-bucket"
        assert sfn_input["source_prefix"] == PIPELINE_ID
        # Completed step data should be included
        assert "file_validation_result" in sfn_input
        assert sfn_input["file_validation_result"]["validated"] is True

    @patch("handlers.pipelines_runs.boto3.client")
    def test_execution_name_contains_retry_and_run_id(self, mock_boto_client):
        """Validates: Req 2.5 — execution name contains 'retry' and original run_id."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        mock_state_table.query.return_value = {"Items": _failed_state_entries()}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        result = retry_run(RUN_ID)

        call_kwargs = mock_sfn.start_execution.call_args[1]
        exec_name = call_kwargs["name"]
        assert "retry" in exec_name
        assert RUN_ID in exec_name

    @patch("handlers.pipelines_runs.boto3.client")
    def test_no_execution_row_created_and_retry_fields_land_on_original(self, mock_boto_client):
        """Validates: Requirements 3.1, 2.1 — no phantom row; retry metadata on the original.

        p38 task 3.7 INVERTED this test (was
        `test_new_execution_record_has_retry_fields`, design §4.9's table). It
        asserted `len(put_calls) == 1` and inspected the phantom's `Item`. The
        `put_item` is gone (do-NOT 2, `LGR-21a1b5b-2`): a row keyed on a
        fabricated run id is the Phantom_Execution_Row three readers would each
        have to learn to ignore. The same three fields — `trigger`,
        `retry_source_run_id`, `execution_name` — now arrive on the ORIGINAL
        row's conditional `update_item`.
        """
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn
        _arrange_failed_run()

        result = retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        # R3.1: not one row is created, for any key.
        assert mock_executions_table.put_item.call_args_list == []

        update_kwargs = _sole_update_kwargs()
        values = update_kwargs["ExpressionAttributeValues"]
        expression = update_kwargs["UpdateExpression"]

        assert update_kwargs["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}
        assert values[":retry"] == "retry"
        assert values[":run_id"] == RUN_ID
        assert values[":running"] == "RUNNING"
        assert values[":execution_name"] == body["execution_name"]
        # `status` and `trigger` are both DynamoDB reserved words, so the
        # expression carries aliases rather than the plain names.
        assert update_kwargs["ExpressionAttributeNames"] == {"#status": "status", "#trigger": "trigger"}
        assert "#trigger = :retry" in expression
        assert "#status = :running" in expression
        assert "retry_source_run_id = :run_id" in expression

    @patch("handlers.pipelines_runs.boto3.client")
    def test_original_record_snapshots_attempt_and_writes_no_forward_id(self, mock_boto_client):
        """Validates: Requirements 2.1, 3.1 — one guarded update, one attempt node, no forward id.

        p38 task 3.7 REPLACED this test's assertion (was
        `test_original_record_updated_with_retried_by`, design §4.9's table).
        It bound `new_run_id = body["run_id"]` and asserted the update's
        `:new_run` equalled it — a round trip of the fabricated id into
        `retried_by_run_id`, i.e. the broken pair pinned as correct. Under one
        shared id there is no forward id to write: the only candidate is the
        run's own, and rendering a link to it is what do-NOT 5 / R5.3 forbid
        (`LGR-453f12d-5`). Lineage becomes `(run_id, attempt_number)`.
        """
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn
        _arrange_failed_run()

        result = retry_run(RUN_ID)

        assert result["statusCode"] == 200
        update_kwargs = _sole_update_kwargs()
        values = update_kwargs["ExpressionAttributeValues"]
        expression = update_kwargs["UpdateExpression"]

        assert update_kwargs["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}
        # The guard `cache_step_result` uses one table over
        # (`retry_utils.py:236-247`, `LGR-60c4985-4`): without it `update_item`
        # CREATES the row, which is the phantom this change deletes.
        assert update_kwargs["ConditionExpression"] == "attribute_exists(run_id)"

        # Exactly one attempt node, carrying the displaced attempt's status.
        assert values[":attempts"] == [
            {"attempt": 1, "failed_step": "SchemaExtract", "status": "FAILED"},
        ]
        assert values[":attempt_count"] == 2

        # No forward identifier is written, under any spelling.
        assert ":new_run" not in values
        assert "retried_by_run_id = " not in expression
        assert not any(key for key in values if "retried_by" in key)
        # The stale field written before the fix is cleared, along with the
        # prior attempt's terminal timestamp.
        assert expression.endswith("REMOVE completed_at, retried_by_run_id")

    @patch("handlers.pipelines_runs.boto3.client")
    def test_returns_200_with_retry_details(self, mock_boto_client):
        """Validates: Requirements 1.1, 1.2, 2.7 — the reported run id IS the requested run id.

        p38 task 3.7 STRENGTHENED one assertion here: `assert "run_id" in body`
        became `assert body["run_id"] == RUN_ID`. The membership check was
        satisfied by the fabricated id too, and its weakness is why R1.1
        shipped broken — nothing in the suite asserted that the id the endpoint
        hands the dashboard is the id the run records state under.
        """
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn
        _arrange_failed_run()

        result = retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["run_id"] == RUN_ID
        assert "execution_name" in body
        assert body["pipeline_id"] == PIPELINE_ID
        # Normalized to the SFN state id by the rerun contract (P28 R9.2)
        assert body["retry_from_step"] == "SchemaExtract"
        assert body["retry_source_run_id"] == RUN_ID

    @patch("handlers.pipelines_runs.boto3.client")
    def test_snapshot_precedes_start_execution(self, mock_boto_client):
        """Validates: Requirement 2.1 — the snapshot is written before the retry starts.

        Ordering is load-bearing, not incidental (design §4.1). The
        `execution_updater` overwrites `status`, `current_step` and the
        `steps_status` entries on this row from the retry's first reported
        step, so a snapshot taken after `start_execution` races the writer for
        the very values it is preserving.
        """
        call_order: list[str] = []
        mock_sfn = MagicMock()
        mock_sfn.start_execution.side_effect = lambda **kwargs: call_order.append("start_execution")
        mock_boto_client.return_value = mock_sfn
        _arrange_failed_run()

        with _temporary_side_effect(
            mock_executions_table.update_item,
            lambda **kwargs: call_order.append("update_item"),
        ):
            result = retry_run(RUN_ID)

        assert result["statusCode"] == 200
        assert call_order == ["update_item", "start_execution"]

    @patch("handlers.pipelines_runs.boto3.client")
    def test_attempts_omitted_when_no_executions_row_was_read(self, mock_boto_client):
        """Validates: Requirements 2.1, 3.6 — an unseen row's attempt list is not fabricated.

        The attempt list is a read-modify-write, so it is only rewritten from a
        row this call actually read. When the pre-flight executions read finds
        no row, writing `attempts` would either clobber history this call
        cannot see or fabricate an Attempt_Record — and `LGR-99050e6-2`
        (template / ratified) refuses both: absent is reported as unknown,
        never defaulted. The rest of the reset still applies, still guarded.
        """
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn
        _arrange_failed_run(exec_items=[])

        result = retry_run(RUN_ID)

        assert result["statusCode"] == 200
        update_kwargs = _sole_update_kwargs()
        values = update_kwargs["ExpressionAttributeValues"]

        assert ":attempts" not in values
        assert ":attempt_count" not in values
        assert "attempts = " not in update_kwargs["UpdateExpression"]
        # The guard and the live-field reset are unconditional.
        assert update_kwargs["ConditionExpression"] == "attribute_exists(run_id)"
        assert values[":running"] == "RUNNING"
        assert mock_executions_table.put_item.call_args_list == []


# ═══════════════════════════════════════════════════════════════════════════
# p38-retry-run-identity task 3.7 — the tests §6 adds
# ═══════════════════════════════════════════════════════════════════════════


class TestRetrySnapshotNeverFailsTheRun:
    """Validates: Requirement 2.5 — the attempt snapshot must not fail the retry.

    Design §4.12's first two rows, and the shape `LGR-eee8ac8-4` (template /
    ratified) sets for post-callback bookkeeping: log at `warning` and continue.
    Three arms because the three failures are distinguishable in kind and the
    handler must not distinguish them in outcome — a conditional-check failure
    means the row is simply absent (R3.6's "or absent"), a `ClientError` means
    the write was refused, and a bare `Exception` means something unforeseen.

    Deferring the snapshot is the one forbidden implementation of R2.5:
    `LGR-b46a5bb-1` is a standing invariant forbidding `threading.Thread` for
    post-response work in `lambda/**`, enforced by
    `tests/unit/test_no_lambda_threads_prop.py` (`LGR-b46a5bb-4`). These tests
    pin the alternative — synchronous, in the request path, best-effort.
    """

    @pytest.mark.parametrize(
        ("label", "error"),
        [
            (
                "conditional_check_failed",
                ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "The conditional request failed",
                        }
                    },
                    "UpdateItem",
                ),
            ),
            (
                "client_error",
                ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}},
                    "UpdateItem",
                ),
            ),
            ("generic_exception", RuntimeError("boom")),
        ],
    )
    @patch("handlers.pipelines_runs.boto3.client")
    def test_snapshot_failure_still_returns_200_and_starts_execution(self, mock_boto_client, label, error):
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn
        _arrange_failed_run()

        with _temporary_side_effect(mock_executions_table.update_item, error):
            result = retry_run(RUN_ID)

        body = json.loads(result["body"])

        assert result["statusCode"] == 200, f"{label} must not fail the retry"
        assert body["run_id"] == RUN_ID
        # The retry proceeds without its bookkeeping — this is the assertion
        # that separates "best-effort" from "swallowed and skipped".
        mock_sfn.start_execution.assert_called_once()
        # And no row is created to compensate for the failed update.
        assert mock_executions_table.put_item.call_args_list == []


class TestRetryDoesNotMoveTheResumeCache:
    """Validates: Requirement 1.5 — the Resume_Cache key does not move.

    The negative delta §6 names. `build_trigger_input` already accepts an
    `original_run_id` and gates it to the rerun flavors
    (`rerun_contract.py:266-272`), so the distinct-run model would have threaded
    it through here. The continuation model must NOT: the Executing_Run_Id is
    unchanged, so the `(run_id, step_name)` cache key resolves exactly as
    before and nothing in `shared/retry_utils.py` needs to change. do-NOT 3 /
    `LGR-60c4985-4` is satisfied by not acting, and this test is the evidence.

    `tests/unit/shared/test_retry_resume.py` stays untouched — its passing
    unamended is the other half of that evidence.
    """

    @patch("handlers.pipelines_runs.boto3.client")
    def test_build_trigger_input_receives_no_original_run_id(self, mock_boto_client):
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn
        _arrange_failed_run()

        real_build = rerun_contract.build_trigger_input
        captured: list[dict] = []

        def _spy(pipeline, **kwargs):
            captured.append(kwargs)
            return real_build(pipeline, **kwargs)

        # `retry_run` imports `build_trigger_input` from the module inside its
        # own body, so patching the module attribute is what the call resolves.
        with patch.object(rerun_contract, "build_trigger_input", _spy):
            result = retry_run(RUN_ID)

        assert result["statusCode"] == 200
        assert len(captured) == 1
        kwargs = captured[0]
        assert "original_run_id" not in kwargs
        assert kwargs["run_id"] == RUN_ID
        assert kwargs["triggered_by"] == "retry"

        # And the built input carries no `original_run_id` either — the field is
        # emitted only when passed, so its absence in the SFN input is the
        # observable half of the same fact.
        sfn_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
        assert "original_run_id" not in sfn_input
        assert sfn_input["run_id"] == RUN_ID


class TestRetryOfARetry:
    """Validates: Requirements 6.1, 6.2 — repeated retries keep one identifier."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_five_successive_retries_all_report_the_original_run_id(self, mock_boto_client):
        """Validates: Requirement 6.1 — every identifier stays the original run id.

        Each retry's write is fed back onto the mocked executions row, so the
        fifth call sees the state the fourth produced. Under the old code the
        second retry would have taken the 47-character fabricated id from the
        first, sliced it to 40, and reported an id that identifies nothing.
        """
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        row = _failed_execution_record()
        mock_state_table.query.return_value = {"Items": _failed_state_entries()}
        mock_locks_table.get_item.return_value = {}

        def _apply_update(**kwargs):
            """Fold the retry's own write back onto the row, as DynamoDB would."""
            values = kwargs["ExpressionAttributeValues"]
            row["status"] = values[":running"]
            row["execution_name"] = values[":execution_name"]
            row["retry_source_run_id"] = values[":run_id"]
            if ":attempts" in values:
                row["attempts"] = values[":attempts"]
                row["attempt_count"] = values[":attempt_count"]
            row.pop("completed_at", None)

        with (
            _temporary_side_effect(mock_executions_table.query, lambda **kwargs: {"Items": [row]}),
            _temporary_side_effect(mock_executions_table.update_item, _apply_update),
        ):
            bodies = []
            for _ in range(5):
                result = retry_run(RUN_ID)
                assert result["statusCode"] == 200
                bodies.append(json.loads(result["body"]))

        # Every identifier in every body is the original run id.
        for body in bodies:
            assert body["run_id"] == RUN_ID
            assert body["retry_source_run_id"] == RUN_ID

        # Every identifier in every update expression, too.
        update_calls = mock_executions_table.update_item.call_args_list
        assert len(update_calls) == 5
        for call in update_calls:
            kwargs = call[1]
            assert kwargs["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}
            assert kwargs["ExpressionAttributeValues"][":run_id"] == RUN_ID
            assert kwargs["ConditionExpression"] == "attribute_exists(run_id)"

        # R2.4: attempt numbers are unique and strictly increasing, and the list
        # is capped at five, so five retries fill it exactly.
        final_attempts = row["attempts"]
        numbers = [node["attempt"] for node in final_attempts]
        assert numbers == [1, 2, 3, 4, 5]
        assert row["attempt_count"] == 6
        # Nothing was created along the way.
        assert mock_executions_table.put_item.call_args_list == []

    @patch("handlers.pipelines_runs.boto3.client")
    def test_no_run_id_is_derived_by_slicing(self, mock_boto_client):
        """Validates: Requirement 6.2 — the self-imposed length bound is gone.

        R6.2 is satisfied vacuously: no identifier is built by concatenation or
        by truncation, so there is no bound to exceed and nothing to coerce
        (do-NOT 11). A 47-character run id — the observed live shape of a retry
        of a retry — round-trips intact through the body, the SFN input and the
        update key. The execution NAME keeps its own 80-character truncation,
        which is AWS's bound and correct (§4.8); the two are not unified.
        """
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn
        _arrange_failed_run(LONG_RUN_ID)

        result = retry_run(LONG_RUN_ID)
        body = json.loads(result["body"])

        # The premise: this id is long enough that the old `[:40]` slice would
        # have corrupted it into one that still parses and identifies nothing.
        assert len(LONG_RUN_ID) > 40
        assert LONG_RUN_ID[:40] != LONG_RUN_ID

        assert result["statusCode"] == 200
        assert body["run_id"] == LONG_RUN_ID
        assert body["retry_source_run_id"] == LONG_RUN_ID

        sfn_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
        assert sfn_input["run_id"] == LONG_RUN_ID
        assert _sole_update_kwargs()["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": LONG_RUN_ID}

    @patch("handlers.pipelines_runs.boto3.client")
    def test_execution_already_exists_returns_409_not_500(self, mock_boto_client):
        """Validates: Requirement 6.2 — a duplicate execution name is classified.

        Design §4.8. Without the narrow handler this reaches `retry_run`'s
        outermost `except Exception` and `handle_error` returns 500
        (`shared/response_utils.py:47-50`), where every sibling refusal in this
        function returns a named 4xx. The clause NARROWS — do-NOT 10 /
        `LGR-b2f79ef-13` forbids the opposite move, letting a blanket clause
        re-label an already-classified exception.
        """
        mock_sfn = MagicMock()
        mock_sfn.exceptions.ExecutionAlreadyExists = _ExecutionAlreadyExists
        mock_sfn.start_execution.side_effect = _ExecutionAlreadyExists("already exists")
        mock_boto_client.return_value = mock_sfn
        _arrange_failed_run()

        result = retry_run(RUN_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 409
        assert "already started" in body["error"].lower()
        assert body["run_id"] == RUN_ID
        # The response names the execution name, which is the condition's
        # subject — the run id alone would not tell a caller what collided.
        assert body["execution_name"].startswith(f"retry-{RUN_ID}-")
