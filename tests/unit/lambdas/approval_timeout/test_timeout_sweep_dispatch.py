# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: the scheduled-sweep mechanics of the approval_timeout_handler.

Companion to `test_timeout_send_ordering.py`, which pins the callback half of
this Lambda (Send_Before_Status ordering, the `has_task_token` short-circuit,
the consumed-token `expired` outcome, and the transient-failure retry). That
file covers everything that touches a task token. This file covers everything
around it — the parts of the sweep that decide *which* records are processed,
*which* branch each one takes, and what the sweep reports afterwards:

- `scan_expired_approvals` — the filter is pending-AND-past-due, with a
  freshly computed UTC `:current_time`; a scan error degrades to an empty list
  rather than raising, which is what makes the sweep idempotent across runs.
- `process_approval_timeout` — the dispatch on `timeout_behavior`, including
  the two cases the integration layer never reaches: the attribute being
  absent, and it holding a value nobody implemented. Both must land on
  `manual`, because manual is the only branch that consumes no task token.
- `create_timeout_alarm` — the full metric and alarm parameter set, not just
  the alarm name.
- `_mark_approval_expired` — the exact attributes written, to keep `expired`
  distinguishable from a decision (no `processed_by`, no `timeout_reason`).
- `generate_timeout_report` / `lambda_handler` — the per-item accounting, the
  empty-scan early return, and the per-item exception isolation that stops one
  malformed record from aborting the rest of the sweep.

Where `tests/integration/approval_flow/test_approval_system.py` already
asserts something (it checks that `FilterExpression` is merely present, and
two of the five alarm parameters), the assertions here are the exact-value
form of the same claim.

Loading follows `test_timeout_send_ordering.py`: several Lambda dirs contain a
module named `lambda_function`, so the handler is loaded from its path under a
unique alias rather than by import name, and the shared-module stubs are
installed by assignment so a sibling test file's stubs cannot win.

Validates: Requirements 3.2, 3.4, 3.5, 3.6
"""

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_timeout_handler"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

# ---------------------------------------------------------------------------
# Deterministic sys.modules stubs — installed with assignment (not setdefault)
# so this file is immune to stub pollution from sibling test modules. Note that
# tests/integration/approval_flow/test_approval_system.py installs a bare
# Mock() (not a MagicMock) into these same keys, so a setdefault here would
# silently inherit it.
# ---------------------------------------------------------------------------
_metrics_stub = MagicMock()
_metrics_stub.time_function = lambda *_a, **_k: lambda f: f  # pass-through decorator factory
_metrics_stub.ApprovalMetrics = MagicMock()
_metrics_stub.record_error = MagicMock()
sys.modules["metrics_facade"] = _metrics_stub

_env_stub = MagicMock()
_env_stub.require_env = lambda name, default=None: os.environ.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = _env_stub

sys.modules["eventbridge_utils"] = MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock()))
sys.modules["notification_service"] = MagicMock(
    notification_service=MagicMock(send_notification=MagicMock(return_value={"success": True})),
    send_approval_timeout_alert=MagicMock(return_value={"success": True}),
)

# ---------------------------------------------------------------------------
# Mock setup — patch boto3 before importing the handler module
# ---------------------------------------------------------------------------
mock_task_tokens_table = MagicMock()
mock_approvals_table = MagicMock()
mock_sfn_client = MagicMock()
mock_cloudwatch_client = MagicMock()

_table_map = {
    "test-task-tokens": mock_task_tokens_table,
    "test-approvals": mock_approvals_table,
}

try:
    with patch("boto3.resource") as mock_resource, patch("boto3.client"):
        mock_dynamo = MagicMock()
        mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
        mock_resource.return_value = mock_dynamo

        sys.modules.pop("lambda_function", None)
        _spec = importlib.util.spec_from_file_location(
            "approval_timeout_sweep_dispatch_lf",
            os.path.join(LAMBDA_DIR, "lambda_function.py"),
        )
        handler = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(handler)
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


APPROVAL_ID = "pipe-001_code_generation_1700000000"
TASK_TOKEN = "timeout-sweep-test-token-value"


@pytest.fixture(autouse=True)
def _reset_mocks():
    for m in (mock_task_tokens_table, mock_approvals_table, mock_dynamo, mock_cloudwatch_client):
        m.reset_mock(return_value=True, side_effect=True)
    mock_sfn_client.reset_mock(return_value=True, side_effect=True)
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    handler.task_tokens_table = mock_task_tokens_table
    handler.approvals_table = mock_approvals_table
    handler.stepfunctions = mock_sfn_client
    handler.cloudwatch = mock_cloudwatch_client
    handler.dynamodb = mock_dynamo
    yield


def _approval_record(**overrides) -> dict:
    """A pending, past-due approval record in the shape production writes.

    `has_task_token` is the canonical boolean on the approval record; the token
    itself lives only in the task-tokens table.
    """
    record = {
        "approval_id": APPROVAL_ID,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "approval_type": "code_generation",
        "status": "pending",
        "timeout_at": "2020-01-01T00:00:00+00:00",
        "timeout_behavior": "manual",
        "has_task_token": False,
    }
    record.update(overrides)
    return record


def _wire_scan(*records: dict):
    mock_approvals_table.scan.return_value = {"Items": list(records)}
    mock_task_tokens_table.get_item.return_value = {"Item": {"approval_id": APPROVAL_ID, "task_token": TASK_TOKEN}}


def _run_sweep() -> dict:
    """Drive the scheduled sweep through lambda_handler; return the report."""
    result = handler.lambda_handler({}, None)
    assert result["statusCode"] == 200
    return json.loads(result["body"])["report"]


# ---------------------------------------------------------------------------
# scan_expired_approvals — the filter that decides what gets swept
# ---------------------------------------------------------------------------


class TestScanExpiredApprovals:
    def test_scan_filters_on_pending_status_and_past_timeout(self):
        """The filter is pending AND past-due — both halves, by exact value.

        The integration layer only asserts that a `FilterExpression` key is
        present, which would still pass if either condition were dropped.
        """
        mock_approvals_table.scan.return_value = {"Items": []}

        before = datetime.now(UTC).isoformat()
        handler.scan_expired_approvals()
        after = datetime.now(UTC).isoformat()

        kwargs = mock_approvals_table.scan.call_args.kwargs
        assert kwargs["FilterExpression"] == "#status = :status AND timeout_at < :current_time"
        # `status` is a DynamoDB reserved word, so it must go through a name
        # placeholder or the scan is rejected at runtime.
        assert kwargs["ExpressionAttributeNames"] == {"#status": "status"}
        assert kwargs["ExpressionAttributeValues"][":status"] == "pending"

        # `:current_time` is computed fresh on each sweep, in UTC. A frozen or
        # stale value would make the sweep progressively miss newer timeouts.
        current_time = kwargs["ExpressionAttributeValues"][":current_time"]
        assert before <= current_time <= after
        assert current_time.endswith("+00:00")

    def test_scan_returns_every_expired_item_in_order(self):
        records = [_approval_record(approval_id="expired-1"), _approval_record(approval_id="expired-2")]
        mock_approvals_table.scan.return_value = {"Items": records}

        result = handler.scan_expired_approvals()

        assert [r["approval_id"] for r in result] == ["expired-1", "expired-2"]

    def test_scan_with_no_expired_approvals_returns_empty_list(self):
        mock_approvals_table.scan.return_value = {"Items": []}

        assert handler.scan_expired_approvals() == []

    def test_scan_error_degrades_to_empty_list_without_raising(self):
        """A failed scan must not propagate.

        The sweep is scheduled every 15 minutes and `timeout_at` is still past
        on the next run, so returning an empty list retries the whole batch for
        free. Raising here would surface as a Lambda error with nothing gained.
        """
        mock_approvals_table.scan.side_effect = RuntimeError("DynamoDB unavailable")

        assert handler.scan_expired_approvals() == []


# ---------------------------------------------------------------------------
# process_approval_timeout — dispatch on timeout_behavior
# ---------------------------------------------------------------------------


class TestTimeoutBehaviorDispatch:
    @pytest.mark.parametrize(
        ("timeout_behavior", "expected_target"),
        [
            ("auto_approve", "auto_approve_timeout"),
            ("auto_reject", "auto_reject_timeout"),
            ("manual", "manual_timeout_handling"),
        ],
    )
    def test_declared_behavior_dispatches_to_its_branch(self, timeout_behavior, expected_target):
        record = _approval_record(timeout_behavior=timeout_behavior)

        with patch.object(handler, expected_target, return_value=True) as target:
            assert handler.process_approval_timeout(record) is True

        target.assert_called_once_with(record)

    def test_absent_timeout_behavior_defaults_to_manual(self):
        """No `timeout_behavior` attribute means manual, not auto-anything.

        This is the safe default: manual is the only branch that consumes no
        task token and writes no decision, so a record whose behaviour was
        never configured cannot be silently auto-approved.
        """
        record = _approval_record()
        del record["timeout_behavior"]

        with patch.object(handler, "manual_timeout_handling", return_value=True) as manual:
            assert handler.process_approval_timeout(record) is True

        manual.assert_called_once_with(record)

    def test_unrecognised_timeout_behavior_falls_through_to_manual(self):
        """An unimplemented value is treated as manual, for the same reason."""
        record = _approval_record(timeout_behavior="escalate_to_oncall")

        with patch.object(handler, "manual_timeout_handling", return_value=True) as manual:
            assert handler.process_approval_timeout(record) is True

        manual.assert_called_once_with(record)

    def test_dispatch_propagates_a_false_result_from_its_branch(self):
        """A branch reporting failure is reported as failure, not swallowed.

        `auto_approve_timeout` returns False on a transient send failure; that
        has to reach the sweep report or the retry never happens (R3.4).
        """
        record = _approval_record(timeout_behavior="auto_approve")

        with patch.object(handler, "auto_approve_timeout", return_value=False):
            assert handler.process_approval_timeout(record) is False

    def test_record_missing_a_required_attribute_returns_false(self):
        """A malformed record fails its own item rather than raising."""
        record = _approval_record()
        del record["approval_type"]

        assert handler.process_approval_timeout(record) is False

    def test_dispatch_records_the_timeout_metric_with_the_behavior(self):
        record = _approval_record(timeout_behavior="manual")
        handler.ApprovalMetrics.record_approval_timeout.reset_mock()

        with patch.object(handler, "manual_timeout_handling", return_value=True):
            handler.process_approval_timeout(record)

        handler.ApprovalMetrics.record_approval_timeout.assert_called_once_with(
            approval_type="code_generation", pipeline_id="pipe-001", timeout_behavior="manual"
        )


# ---------------------------------------------------------------------------
# manual_timeout_handling — marked timed-out, still pending
# ---------------------------------------------------------------------------


class TestManualTimeoutHandling:
    def test_manual_marks_timeout_occurred_without_touching_status(self):
        """The approval stays pending: a human still has to decide.

        Asserted as the exact UpdateExpression because the distinction that
        matters is what is *absent* — no `#status`, so no decision is written
        and the execution keeps waiting on its token.
        """
        record = _approval_record(timeout_behavior="manual")

        with patch.object(handler, "create_timeout_alarm"):
            assert handler.manual_timeout_handling(record) is True

        kwargs = mock_approvals_table.update_item.call_args.kwargs
        assert kwargs["Key"] == {"approval_id": APPROVAL_ID}
        assert kwargs["UpdateExpression"] == (
            "SET timeout_occurred = :timeout_occurred, timeout_notified_at = :notified_at"
        )
        assert "ExpressionAttributeNames" not in kwargs
        assert kwargs["ExpressionAttributeValues"][":timeout_occurred"] is True
        assert ":status" not in kwargs["ExpressionAttributeValues"]

    def test_manual_escalates_by_creating_the_alarm(self):
        record = _approval_record(timeout_behavior="manual")

        with patch.object(handler, "create_timeout_alarm") as alarm:
            handler.manual_timeout_handling(record)

        alarm.assert_called_once_with(record)

    def test_manual_write_failure_returns_false(self):
        mock_approvals_table.update_item.side_effect = RuntimeError("conditional check failed")

        assert handler.manual_timeout_handling(_approval_record()) is False


# ---------------------------------------------------------------------------
# _mark_approval_expired — `expired` must stay distinguishable from a decision
# ---------------------------------------------------------------------------


class TestMarkApprovalExpired:
    def test_expired_write_carries_no_decision_provenance(self):
        """`expired` sets status and expired_at, and nothing else.

        The decision writes set `processed_by` and `timeout_reason`; if those
        appeared here too, an already-consumed token would be indistinguishable
        from a timeout outcome in the audit record (R3.5).
        """
        handler._mark_approval_expired(APPROVAL_ID)

        kwargs = mock_approvals_table.update_item.call_args.kwargs
        assert kwargs["Key"] == {"approval_id": APPROVAL_ID}
        assert kwargs["UpdateExpression"] == "SET #status = :status, expired_at = :expired_at"
        assert kwargs["ExpressionAttributeNames"] == {"#status": "status"}
        values = kwargs["ExpressionAttributeValues"]
        assert values[":status"] == "expired"
        assert set(values) == {":status", ":expired_at"}


# ---------------------------------------------------------------------------
# create_timeout_alarm — the full parameter set
# ---------------------------------------------------------------------------


class TestCreateTimeoutAlarm:
    def test_alarm_emits_the_escalation_metric_with_both_dimensions(self):
        handler.create_timeout_alarm(_approval_record())

        kwargs = mock_cloudwatch_client.put_metric_data.call_args.kwargs
        assert kwargs["Namespace"] == "AgenticDataIngestion/Approvals"
        datum = kwargs["MetricData"][0]
        assert datum["MetricName"] == "ManualTimeoutEscalation"
        assert datum["Value"] == 1
        assert datum["Unit"] == "Count"
        # Both dimensions are required: the alarm below is scoped to exactly
        # this pair, so a missing dimension yields an alarm that never fires.
        assert datum["Dimensions"] == [
            {"Name": "ApprovalId", "Value": APPROVAL_ID},
            {"Name": "PipelineId", "Value": "pipe-001"},
        ]

    def test_alarm_fires_on_the_first_datapoint(self):
        """Threshold 1 / one evaluation period — escalation is immediate.

        A manual timeout needs a human now, so the alarm is deliberately
        configured to trip on a single datapoint rather than averaging.
        """
        handler.create_timeout_alarm(_approval_record())

        kwargs = mock_cloudwatch_client.put_metric_alarm.call_args.kwargs
        assert kwargs["AlarmName"] == f"AgenticDataIngestion-Approval-Timeout-{APPROVAL_ID}"
        assert kwargs["MetricName"] == "ManualTimeoutEscalation"
        assert kwargs["Namespace"] == "AgenticDataIngestion/Approvals"
        assert kwargs["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
        assert kwargs["Threshold"] == 1.0
        assert kwargs["EvaluationPeriods"] == 1
        assert kwargs["Statistic"] == "Sum"
        assert kwargs["Period"] == 60
        assert kwargs["ActionsEnabled"] is True
        assert kwargs["Dimensions"] == [
            {"Name": "ApprovalId", "Value": APPROVAL_ID},
            {"Name": "PipelineId", "Value": "pipe-001"},
        ]

    def test_alarm_actions_are_empty_when_no_topic_is_configured(self):
        """`CLOUDWATCH_ALARM_TOPIC` is optional, so the list may be empty.

        `AlarmActions=[None]` would be rejected by CloudWatch, which is why the
        handler builds the list conditionally rather than wrapping the value.
        """
        with patch.object(handler, "CLOUDWATCH_ALARM_TOPIC", None):
            handler.create_timeout_alarm(_approval_record())

        assert mock_cloudwatch_client.put_metric_alarm.call_args.kwargs["AlarmActions"] == []

    def test_alarm_actions_carry_the_configured_topic(self):
        topic = "arn:aws:sns:us-west-2:123456789012:adi-alarms"

        with patch.object(handler, "CLOUDWATCH_ALARM_TOPIC", topic):
            handler.create_timeout_alarm(_approval_record())

        assert mock_cloudwatch_client.put_metric_alarm.call_args.kwargs["AlarmActions"] == [topic]

    def test_alarm_failure_is_swallowed_so_the_sweep_continues(self):
        """Alarm creation is best-effort; it must not fail the escalation.

        The approval has already been marked timed out by this point, so
        raising would lose that work on a CloudWatch hiccup.
        """
        mock_cloudwatch_client.put_metric_alarm.side_effect = RuntimeError("cloudwatch throttled")

        handler.create_timeout_alarm(_approval_record())  # does not raise


# ---------------------------------------------------------------------------
# generate_timeout_report — per-behaviour accounting
# ---------------------------------------------------------------------------


class TestGenerateTimeoutReport:
    def test_report_counts_each_action_separately(self):
        processed = [
            {
                "approval_id": "a1",
                "pipeline_id": "p1",
                "approval_type": "t",
                "action": "auto_approved",
                "success": True,
            },
            {
                "approval_id": "a2",
                "pipeline_id": "p1",
                "approval_type": "t",
                "action": "auto_rejected",
                "success": True,
            },
            {
                "approval_id": "a3",
                "pipeline_id": "p1",
                "approval_type": "t",
                "action": "manual_escalation",
                "success": True,
            },
        ]

        report = handler.generate_timeout_report(processed)

        assert report["total_timeouts_processed"] == 3
        assert report["auto_approved"] == 1
        assert report["auto_rejected"] == 1
        assert report["manual_escalations"] == 1
        assert report["processing_errors"] == 0

    def test_report_counts_unsuccessful_items_as_processing_errors(self):
        processed = [
            {
                "approval_id": "a1",
                "pipeline_id": "p1",
                "approval_type": "t",
                "action": "auto_approved",
                "success": True,
            },
            {
                "approval_id": "a2",
                "pipeline_id": "p1",
                "approval_type": "t",
                "action": "auto_approved",
                "success": False,
            },
        ]

        report = handler.generate_timeout_report(processed)

        assert report["auto_approved"] == 1
        assert report["processing_errors"] == 1

    def test_report_lists_one_detail_row_per_item(self):
        processed = [
            {
                "approval_id": "a1",
                "pipeline_id": "p1",
                "approval_type": "code_generation",
                "action": "x",
                "success": True,
            }
        ]

        report = handler.generate_timeout_report(processed)

        assert report["timeout_details"] == [
            {
                "approval_id": "a1",
                "pipeline_id": "p1",
                "approval_type": "code_generation",
                "action": "x",
                "success": True,
            }
        ]

    def test_empty_input_reports_zero_of_everything(self):
        report = handler.generate_timeout_report([])

        assert report["total_timeouts_processed"] == 0
        assert report["auto_approved"] == 0
        assert report["auto_rejected"] == 0
        assert report["manual_escalations"] == 0
        assert report["processing_errors"] == 0
        assert report["timeout_details"] == []


# ---------------------------------------------------------------------------
# lambda_handler — the scheduled entry point
# ---------------------------------------------------------------------------


class TestLambdaHandlerSweep:
    def test_no_expired_approvals_returns_early_without_processing(self):
        mock_approvals_table.scan.return_value = {"Items": []}

        result = handler.lambda_handler({}, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["processed_count"] == 0
        # Nothing written, nothing sent — the sweep is a no-op on an idle table
        mock_approvals_table.update_item.assert_not_called()
        mock_sfn_client.send_task_success.assert_not_called()
        mock_sfn_client.send_task_failure.assert_not_called()

    def test_sweep_processes_every_expired_record(self):
        _wire_scan(
            _approval_record(approval_id="a1", timeout_behavior="manual"),
            _approval_record(approval_id="a2", timeout_behavior="manual"),
        )

        with patch.object(handler, "create_timeout_alarm"):
            report = _run_sweep()

        assert report["total_timeouts_processed"] == 2
        assert [d["approval_id"] for d in report["timeout_details"]] == ["a1", "a2"]
        assert all(d["success"] for d in report["timeout_details"])

    def test_one_failing_record_does_not_abort_the_rest_of_the_sweep(self):
        """Per-item isolation — the whole point of a batch sweep.

        Without it, a single malformed record parks every other timeout in the
        table until someone notices, which is the failure mode a 15-minute
        schedule is meant to rule out.
        """
        good_first = _approval_record(approval_id="good-1", timeout_behavior="manual")
        good_second = _approval_record(approval_id="good-2", timeout_behavior="manual")
        _wire_scan(good_first, good_second)

        def _explode_on_the_first(record):
            if record["approval_id"] == "good-1":
                raise RuntimeError("boom")
            return True

        with patch.object(handler, "process_approval_timeout", side_effect=_explode_on_the_first):
            report = _run_sweep()

        assert report["total_timeouts_processed"] == 2
        details = {d["approval_id"]: d for d in report["timeout_details"]}
        assert details["good-1"]["success"] is False
        assert details["good-1"]["action"] == "error"
        # The second record was still processed
        assert details["good-2"]["success"] is True
        assert report["processing_errors"] == 1

    def test_report_action_column_echoes_each_records_timeout_behavior(self):
        _wire_scan(
            _approval_record(approval_id="a1", timeout_behavior="auto_approve"),
            _approval_record(approval_id="a2", timeout_behavior="manual"),
        )

        with patch.object(handler, "process_approval_timeout", return_value=True):
            report = _run_sweep()

        actions = {d["approval_id"]: d["action"] for d in report["timeout_details"]}
        assert actions == {"a1": "auto_approve", "a2": "manual"}

    def test_scan_failure_reports_no_work_rather_than_an_error(self):
        """`scan_expired_approvals` absorbs its own errors, so the handler
        sees an empty batch and returns its no-work response."""
        mock_approvals_table.scan.side_effect = RuntimeError("DynamoDB unavailable")

        result = handler.lambda_handler({}, None)

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["processed_count"] == 0

    def test_status_write_failing_after_a_delivered_send_ends_as_expired(self):
        """Documents current behaviour, and names a real inconsistency.

        Send_Before_Status buys the guarantee that a *failed* send never leaves
        a record claiming a decision. It does not cover the mirror case: the
        send succeeds (`lambda_function.py:207`), the token record is deleted
        (`:241`), and then the status write at `:266` fails. The item is
        reported failed and the next sweep re-picks it (still pending, still
        past due) — but the token record is gone now, so
        `_send_timeout_callback` takes the token-absent branch at `:197` and
        marks the approval `expired` rather than `approved`.

        Net effect: the execution received `approved` and resumed, while the
        approval record says `expired`. The direction is the safe one (the
        record never over-claims), but `expired` is conflating "somebody else
        consumed this token" with "we consumed it and failed to record it".

        Note this is where the two handlers deliberately diverge: on a missing
        token record the approval_handler returns 410 and leaves the approval
        pending (see approval_handler/test_approval_send_ordering.py
        ::TestTokenRecordAbsent), because a caller can retry. A 15-minute sweep
        has no such caller, so leaving it pending would re-sweep forever.

        Pinned so the tradeoff is visible rather than latent. Changing it is a
        product decision, not a test fix.
        """
        _wire_scan(_approval_record(timeout_behavior="auto_approve", has_task_token=True))

        # First sweep: send lands, token record deleted, then the status write fails
        mock_approvals_table.update_item.side_effect = RuntimeError("write throttled")

        first_report = _run_sweep()

        mock_sfn_client.send_task_success.assert_called_once()
        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        assert first_report["timeout_details"][0]["success"] is False

        # Second sweep: the record is still pending and past due, but its token
        # record is gone, so it resolves to "expired" — not "approved".
        mock_approvals_table.update_item.side_effect = None
        mock_approvals_table.update_item.reset_mock()
        mock_task_tokens_table.get_item.return_value = {}

        second_report = _run_sweep()

        statuses = [
            c.kwargs.get("ExpressionAttributeValues", {}).get(":status")
            for c in mock_approvals_table.update_item.call_args_list
        ]
        assert statuses == ["expired"]
        assert "approved" not in statuses
        # The sweep considers the item handled, so it stops retrying.
        assert second_report["timeout_details"][0]["success"] is True

    def test_unexpected_failure_in_the_sweep_returns_500(self):
        """A failure outside the per-item guard surfaces as a Lambda error.

        The per-item `try` covers `process_approval_timeout`; anything after the
        loop (here, report generation) is not covered by it, and the scheduled
        invocation should be recorded as failed rather than reporting success.
        """
        _wire_scan(_approval_record(timeout_behavior="manual"))

        with (
            patch.object(handler, "process_approval_timeout", return_value=True),
            patch.object(handler, "generate_timeout_report", side_effect=RuntimeError("report blew up")),
        ):
            result = handler.lambda_handler({}, None)

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "InternalError"
        assert "report blew up" in body["message"]
