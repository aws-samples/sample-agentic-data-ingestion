# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the refactored pluggable notification service."""

import os
import sys
from unittest.mock import MagicMock, patch

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Set env before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


class TestNotificationChannelProtocol:
    """Verify the protocol is structurally correct."""

    def test_protocol_has_send_method(self):
        from notification_service import NotificationChannelProtocol

        assert hasattr(NotificationChannelProtocol, "send")


class TestChannelLoading:
    """Verify channels are loaded from NOTIFICATION_CHANNELS env var."""

    def test_default_channels_are_sns_and_dashboard(self):
        with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": "sns,dashboard"}):
            with patch("notification_service.boto3"):
                # Reimport to pick up env change
                if "notification_service" in sys.modules:
                    del sys.modules["notification_service"]
                from notification_service import DashboardChannel, NotificationService, SNSChannel

                svc = NotificationService()
                assert "sns" in svc._channels
                assert "dashboard" in svc._channels
                assert isinstance(svc._channels["sns"], SNSChannel)
                assert isinstance(svc._channels["dashboard"], DashboardChannel)

    def test_unknown_channel_skipped_silently(self):
        with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": "sns,nonexistent"}):
            with patch("notification_service.boto3"):
                if "notification_service" in sys.modules:
                    del sys.modules["notification_service"]
                from notification_service import NotificationService

                svc = NotificationService()
                assert "sns" in svc._channels
                assert "nonexistent" not in svc._channels

    def test_empty_channels_env_loads_nothing(self):
        with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": ""}), patch("notification_service.boto3"):
            if "notification_service" in sys.modules:
                del sys.modules["notification_service"]
            from notification_service import NotificationService

            svc = NotificationService()
            assert len(svc._channels) == 0


class TestRegisterChannel:
    """Verify runtime channel registration."""

    def test_register_custom_channel(self):
        with patch("notification_service.boto3"):
            if "notification_service" in sys.modules:
                del sys.modules["notification_service"]
            from notification_service import NotificationService

        with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": ""}):
            svc = NotificationService()

        mock_channel = MagicMock()
        mock_channel.send.return_value = {"success": True}
        svc.register_channel("custom", mock_channel)

        assert "custom" in svc._channels
        result = svc.send_notification(
            notification_type="test",
            title="Test",
            message="Hello",
        )
        mock_channel.send.assert_called_once()
        assert result["success"] is True


class TestSendNotification:
    """Verify send_notification dispatches to loaded channels."""

    def test_sends_to_all_loaded_channels(self):
        with patch("notification_service.boto3"):
            if "notification_service" in sys.modules:
                del sys.modules["notification_service"]
            from notification_service import NotificationPriority, NotificationService

        with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": ""}):
            svc = NotificationService()

        ch1 = MagicMock()
        ch1.send.return_value = {"success": True}
        ch2 = MagicMock()
        ch2.send.return_value = {"success": True}
        svc.register_channel("a", ch1)
        svc.register_channel("b", ch2)

        result = svc.send_notification(
            notification_type="test",
            title="T",
            message="M",
            priority=NotificationPriority.HIGH,
        )
        ch1.send.assert_called_once()
        ch2.send.assert_called_once()
        assert result["success"] is True
        assert "a" in result["delivery_results"]
        assert "b" in result["delivery_results"]

    def test_unconfigured_channel_skipped_when_requested(self):
        with patch("notification_service.boto3"):
            if "notification_service" in sys.modules:
                del sys.modules["notification_service"]
            from notification_service import NotificationService

        with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": ""}):
            svc = NotificationService()

        ch1 = MagicMock()
        ch1.send.return_value = {"success": True}
        svc.register_channel("a", ch1)

        # Request channels=['a', 'missing'] — 'missing' should be skipped
        result = svc.send_notification(
            notification_type="test",
            title="T",
            message="M",
            channels=["a", "missing"],
        )
        ch1.send.assert_called_once()
        assert "missing" not in result["delivery_results"]

    def test_channel_exception_does_not_break_others(self):
        with patch("notification_service.boto3"):
            if "notification_service" in sys.modules:
                del sys.modules["notification_service"]
            from notification_service import NotificationService

        with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": ""}):
            svc = NotificationService()

        failing = MagicMock()
        failing.send.side_effect = RuntimeError("boom")
        passing = MagicMock()
        passing.send.return_value = {"success": True}
        svc.register_channel("fail", failing)
        svc.register_channel("pass", passing)

        result = svc.send_notification(
            notification_type="test",
            title="T",
            message="M",
        )
        assert result["delivery_results"]["fail"]["success"] is False
        assert result["delivery_results"]["pass"]["success"] is True
        assert result["success"] is True


class TestBackwardCompatibility:
    """Verify the old NotificationChannel enum import still works."""

    def test_notification_channel_enum_values(self):
        from notification_service import NotificationChannel

        assert NotificationChannel.SNS.value == "sns"
        assert NotificationChannel.DASHBOARD.value == "dashboard"
        assert NotificationChannel.SLACK.value == "slack"
        assert NotificationChannel.CLOUDWATCH.value == "cloudwatch"

    def test_enum_values_work_as_channel_names(self):
        """Old code passes NotificationChannel.SNS to channels= param."""
        with patch("notification_service.boto3"):
            if "notification_service" in sys.modules:
                del sys.modules["notification_service"]
            from notification_service import NotificationChannel, NotificationService

        with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": ""}):
            svc = NotificationService()

        mock_ch = MagicMock()
        mock_ch.send.return_value = {"success": True}
        svc.register_channel("sns", mock_ch)

        result = svc.send_notification(
            notification_type="test",
            title="T",
            message="M",
            channels=[NotificationChannel.SNS],
        )
        mock_ch.send.assert_called_once()
        assert result["success"] is True


class TestConvenienceFunctions:
    """Verify convenience functions still work after refactoring."""

    def test_send_approval_notification_delegates(self):
        with patch("notification_service.boto3"):
            if "notification_service" in sys.modules:
                del sys.modules["notification_service"]
            import notification_service as ns

        with patch.object(ns.notification_service, "send_notification", return_value={"success": True}) as mock_send:
            result = ns.send_approval_notification(
                approval_type="code_review",
                pipeline_id="pipe-1",
                approval_id="apr-1",
                dashboard_url="https://example.com",
            )
            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["notification_type"] == "approval_required"
            assert call_kwargs["priority"] == ns.NotificationPriority.HIGH

    def test_send_pipeline_failure_alert_delegates(self):
        with patch("notification_service.boto3"):
            if "notification_service" in sys.modules:
                del sys.modules["notification_service"]
            import notification_service as ns

        with patch.object(ns.notification_service, "send_notification", return_value={"success": True}) as mock_send:
            result = ns.send_pipeline_failure_alert(
                pipeline_id="pipe-1",
                stage_name="create_code",
                error_message="Syntax error",
            )
            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs["notification_type"] == "pipeline_failure"


# ---------------------------------------------------------------------------
# P28 R19.3 — collision-free notification ids
# ---------------------------------------------------------------------------


def _fresh_notification_service_module():
    """Import notification_service with boto3 patched (fresh module state)."""
    with patch("notification_service.boto3"):
        if "notification_service" in sys.modules:
            del sys.modules["notification_service"]
        import notification_service as ns
    return ns


class TestNotificationIdCollisionFree:
    """Validates: Requirements 19.3 — notification_id is uuid-based, collision-free."""

    def test_dashboard_ids_distinct_for_same_second_sends(self):
        """Two dashboard sends back-to-back (same wall-clock second) get distinct ids."""
        ns = _fresh_notification_service_module()
        with patch.object(ns, "boto3"):
            channel = ns.DashboardChannel()
        channel.notifications_table = MagicMock()

        channel.send("T1", "M1", "medium", {})
        channel.send("T2", "M2", "medium", {})

        ids = [call.kwargs["Item"]["notification_id"] for call in channel.notifications_table.put_item.call_args_list]
        assert len(ids) == 2
        assert ids[0] != ids[1], "same-second dashboard notifications must not collide"

    def test_dashboard_id_format_is_prefixed_uuid_hex(self):
        """notification_id keeps the dashboard_ prefix but embeds uuid4().hex."""
        import re

        ns = _fresh_notification_service_module()
        with patch.object(ns, "boto3"):
            channel = ns.DashboardChannel()
        channel.notifications_table = MagicMock()

        channel.send("T", "M", "medium", {})

        nid = channel.notifications_table.put_item.call_args.kwargs["Item"]["notification_id"]
        assert re.fullmatch(r"dashboard_[0-9a-f]{32}", nid), f"unexpected notification_id format: {nid}"

    def test_send_notification_ids_distinct_for_same_second_sends(self):
        """The service-level notification_id is also uuid-based and collision-free."""
        import re

        ns = _fresh_notification_service_module()
        with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": ""}):
            svc = ns.NotificationService()

        r1 = svc.send_notification(notification_type="test", title="T", message="M")
        r2 = svc.send_notification(notification_type="test", title="T", message="M")

        assert r1["notification_id"] != r2["notification_id"]
        assert re.fullmatch(r"test_[0-9a-f]{32}", r1["notification_id"])


# ---------------------------------------------------------------------------
# P28 R19.4 — bounded, idempotent critical-alert alarm creation
# ---------------------------------------------------------------------------


def _cloudwatch_channel(ns, existing_alarm_names=()):
    """Build a CloudWatchChannel whose client mock reports the given alarms."""
    with patch.object(ns, "boto3"):
        channel = ns.CloudWatchChannel()
    channel.cloudwatch = MagicMock()
    channel.cloudwatch.get_paginator.return_value.paginate.return_value = [
        {"MetricAlarms": [{"AlarmName": name} for name in existing_alarm_names]}
    ]
    return channel


class TestCloudWatchAlarmIdempotency:
    """Validates: Requirements 19.4 — stable alarm names, no per-occurrence growth."""

    def test_same_alert_type_twice_uses_same_alarm_name(self):
        ns = _fresh_notification_service_module()
        channel = _cloudwatch_channel(ns)

        channel.send("T1", "M1", "critical", {"alert_type": "approval_timeout"})
        channel.send("T2", "M2", "critical", {"alert_type": "approval_timeout"})

        names = [call.kwargs["AlarmName"] for call in channel.cloudwatch.put_metric_alarm.call_args_list]
        assert names == [
            "AgenticDataIngestion-Critical-approval_timeout",
            "AgenticDataIngestion-Critical-approval_timeout",
        ], "alarm name must be stable per alert_type (idempotent put_metric_alarm)"

    def test_alarm_name_has_no_timestamp_component(self):
        ns = _fresh_notification_service_module()
        channel = _cloudwatch_channel(ns)

        result = channel.send("T", "M", "critical", {"alert_type": "pipeline_failure"})

        assert result["success"] is True
        assert result["alarm_name"] == "AgenticDataIngestion-Critical-pipeline_failure"
        assert result["alarm_created"] is True

    def test_non_critical_priority_still_rejected(self):
        ns = _fresh_notification_service_module()
        channel = _cloudwatch_channel(ns)

        result = channel.send("T", "M", "high", {"alert_type": "x"})

        assert result["success"] is False
        channel.cloudwatch.put_metric_data.assert_not_called()
        channel.cloudwatch.put_metric_alarm.assert_not_called()


class TestCloudWatchAlarmCap:
    """Validates: Requirements 19.4 — distinct-alarm count cap from config."""

    def test_new_alert_type_beyond_cap_skips_alarm_but_emits_metric(self, caplog):
        import logging

        ns = _fresh_notification_service_module()
        existing = [f"AgenticDataIngestion-Critical-type{i}" for i in range(2)]
        channel = _cloudwatch_channel(ns, existing_alarm_names=existing)

        with (
            patch.dict(os.environ, {"MAX_CRITICAL_ALERT_ALARMS": "2"}),
            caplog.at_level(logging.ERROR, logger="notification_service"),
        ):
            result = channel.send("T", "M", "critical", {"alert_type": "brand_new_type"})

        # Metric still emitted — the metric is the signal
        channel.cloudwatch.put_metric_data.assert_called_once()
        # No new alarm created
        channel.cloudwatch.put_metric_alarm.assert_not_called()
        assert result == {
            "success": True,
            "alarm_name": None,
            "alarm_created": False,
            "reason": "alarm_cap_reached",
        }
        # Loud log
        assert any("alarm cap reached" in r.message.lower() for r in caplog.records)

    def test_existing_alert_type_at_cap_still_updates_alarm(self):
        """Updating an EXISTING alarm name does not grow the set — allowed at cap."""
        ns = _fresh_notification_service_module()
        existing = [
            "AgenticDataIngestion-Critical-approval_timeout",
            "AgenticDataIngestion-Critical-pipeline_failure",
        ]
        channel = _cloudwatch_channel(ns, existing_alarm_names=existing)

        with patch.dict(os.environ, {"MAX_CRITICAL_ALERT_ALARMS": "2"}):
            result = channel.send("T", "M", "critical", {"alert_type": "approval_timeout"})

        channel.cloudwatch.put_metric_alarm.assert_called_once()
        assert result["success"] is True
        assert result["alarm_name"] == "AgenticDataIngestion-Critical-approval_timeout"

    def test_default_cap_is_documented_25(self):
        ns = _fresh_notification_service_module()
        assert ns.DEFAULT_MAX_CRITICAL_ALERT_ALARMS == 25
        with patch.dict(os.environ):
            os.environ.pop("MAX_CRITICAL_ALERT_ALARMS", None)
            assert ns._max_critical_alert_alarms() == 25

    def test_misconfigured_cap_is_honest_error_not_silent_fallback(self):
        ns = _fresh_notification_service_module()
        channel = _cloudwatch_channel(ns)

        with patch.dict(os.environ, {"MAX_CRITICAL_ALERT_ALARMS": "not-a-number"}):
            result = channel.send("T", "M", "critical", {"alert_type": "x"})

        assert result["success"] is False
        assert "MAX_CRITICAL_ALERT_ALARMS" in result["error"]
        channel.cloudwatch.put_metric_alarm.assert_not_called()
