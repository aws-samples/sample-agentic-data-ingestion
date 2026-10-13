# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The scheduled-skip notification says the load is lost (p30 task 4.9, R10.1).

R10.1 pairs a Skipped_Run_Record with a notification. The record's shape and the
which-contentions question are pinned in
tests/unit/lambdas/pipeline_trigger/test_skipped_run_record.py; this file pins
the message ``send_scheduled_run_skipped_alert`` builds, and specifically the
three things that make it worth sending:

1. it states that **nothing retried** the load — that is the whole difference
   between this and the SQS path's contention, which is a redelivery;
2. a saturated count arrives with its **floor** qualification, never as a bare
   total (4.11) — "11 running" when 40 are running understates saturation in the
   reassuring direction;
3. it tells the reader the alert is a **summary over a window**, so the absence
   of 288 identical notifications is not read as the absence of 288 skips.

Priority is MEDIUM, not HIGH, and that is asserted rather than left incidental:
a contention skip is the concurrency gate working as designed, and grading it
alongside a pipeline failure would make the two indistinguishable in an inbox.

**Validates: Requirements 10.1**
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


def _send(**overrides) -> dict:
    """Call the alert with the notification service captured; return its kwargs."""
    from notification_service import send_scheduled_run_skipped_alert

    kwargs = {
        "pipeline_id": "orders",
        "run_id": "run-2026-08-02-14-00-00-skip-global_concurrency",
        "skip_reason": "global_concurrency",
        "skip_detail": (
            "Global concurrency limit reached: 10 running of a maximum 10, observed when the "
            "trigger was checked. Scheduled trigger skipped."
        ),
        "window_seconds": 3600,
        **overrides,
    }
    with patch("notification_service.notification_service.send_notification") as send:
        send.return_value = {"notification_id": "n-1", "delivery_results": {}, "success": True}
        send_scheduled_run_skipped_alert(**kwargs)
    return send.call_args.kwargs


class TestShapeMatchesTheOtherAlerts:
    def test_uses_sns_and_dashboard(self):
        assert _send()["channels"] == ["sns", "dashboard"]

    def test_carries_alert_type_for_the_alarm_keying_convention(self):
        assert _send()["metadata"]["alert_type"] == "scheduled_run_skipped"

    def test_priority_is_medium_not_high(self):
        """The gate refusing work is the gate succeeding. HIGH is reserved for
        things that went wrong and will stay wrong."""
        from notification_service import NotificationPriority

        assert _send()["priority"] == NotificationPriority.MEDIUM

    def test_names_the_pipeline_in_the_title(self):
        assert "orders" in _send()["title"]


class TestSaysTheLoadIsLost:
    def test_states_that_nothing_will_retry(self):
        message = _send()["message"]
        assert "nothing will retry" in message.lower()

    def test_points_at_the_skipped_run_record(self):
        run_id = "run-2026-08-02-14-00-00-skip-global_concurrency"
        assert run_id in _send()["message"]
        assert _send()["metadata"]["run_id"] == run_id

    def test_reproduces_the_recorded_detail(self):
        detail = "Pipeline 'orders' already has a running execution. Scheduled trigger skipped."
        assert detail in _send(skip_detail=detail)["message"]

    def test_carries_the_reason_code_for_a_machine_reader(self):
        assert _send(skip_reason="pipeline_lock_contention")["metadata"]["skip_reason"] == (
            "pipeline_lock_contention"
        )


class TestCountFloorHonesty:
    def test_a_complete_count_is_reported_plainly(self):
        message = _send(running_count=10, running_max=10, running_count_is_floor=False)["message"]
        assert "10 of a maximum 10" in message
        assert "at least" not in message
        assert "LOWER BOUND" not in message

    def test_a_truncated_count_is_reported_as_a_floor(self):
        message = _send(running_count=11, running_max=10, running_count_is_floor=True)["message"]
        assert "at least 11 of a maximum 10" in message
        assert "LOWER BOUND" in message

    def test_the_floor_flag_travels_with_the_count_in_metadata(self):
        metadata = _send(running_count=11, running_max=10, running_count_is_floor=True)["metadata"]
        assert metadata["running_count"] == 11
        assert metadata["running_count_is_floor"] is True

    def test_a_lock_skip_reports_no_counts_at_all(self):
        """A lock rejection has no fleet-wide number, and a zero would read as
        one. The detail is overridden too: the default one is the concurrency
        message, which carries counts of its own."""
        result = _send(
            skip_reason="pipeline_lock_contention",
            skip_detail="Pipeline 'orders' already has a running execution. Scheduled trigger skipped.",
        )
        assert "of a maximum" not in result["message"]
        assert "running_count" not in result["metadata"]
        assert "running_max" not in result["metadata"]


class TestSaysItIsASummary:
    def test_states_the_collapse_window_in_minutes(self):
        assert "60 minute(s)" in _send(window_seconds=3600)["message"]

    def test_says_further_skips_do_not_notify_again(self):
        message = _send()["message"]
        assert "do NOT send another notification" in message

    def test_a_sub_minute_window_still_renders_a_whole_minute(self):
        """Guards the floor division: a 30-second window must not render as
        '0 minute(s)', which reads as 'no window'."""
        assert "1 minute(s)" in _send(window_seconds=30)["message"]

    def test_the_window_is_in_metadata_for_a_machine_reader(self):
        assert _send(window_seconds=1800)["metadata"]["notify_window_seconds"] == 1800
