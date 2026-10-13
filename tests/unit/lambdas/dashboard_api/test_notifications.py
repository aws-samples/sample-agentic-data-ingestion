# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for notification endpoints — get notifications, limit param,
unread_only filter, mark-read success, mark-read 404, error handling.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("NOTIFICATIONS_TABLE", "agentic_data_ingestion_notifications")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(response: dict) -> tuple[int, dict]:
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Sample notification data
# ---------------------------------------------------------------------------

SAMPLE_NOTIFICATIONS = [
    {
        "notification_id": "dashboard_1001",
        "title": "Pipeline completed",
        "message": "Pipeline pipe-001 finished successfully",
        "priority": "medium",
        "created_at": "2025-01-15T10:00:00+00:00",
        "dashboard_visible": "true",
        "dashboard_read": "false",
    },
    {
        "notification_id": "dashboard_1002",
        "title": "Approval required",
        "message": "Pipeline pipe-002 needs approval",
        "priority": "high",
        "created_at": "2025-01-15T09:00:00+00:00",
        "dashboard_visible": "true",
        "dashboard_read": "true",
    },
]


# ---------------------------------------------------------------------------
# Import handler under test
# ---------------------------------------------------------------------------
from handlers.health import get_notifications, mark_notification_read

# ===========================================================================
# GET /api/notifications — default response
# ===========================================================================


class TestGetNotificationsDefault:
    @patch("notification_service.DashboardChannel")
    def test_default_returns_notifications_and_count(self, mock_channel_cls):
        mock_instance = MagicMock()
        mock_instance.get_dashboard_notifications.return_value = SAMPLE_NOTIFICATIONS
        mock_channel_cls.return_value = mock_instance

        status, body = _resp(get_notifications({}))

        assert status == 200
        assert body["count"] == 2
        assert len(body["notifications"]) == 2
        mock_instance.get_dashboard_notifications.assert_called_once_with(limit=50, unread_only=False)

    @patch("notification_service.DashboardChannel")
    def test_empty_notifications(self, mock_channel_cls):
        mock_instance = MagicMock()
        mock_instance.get_dashboard_notifications.return_value = []
        mock_channel_cls.return_value = mock_instance

        status, body = _resp(get_notifications({}))

        assert status == 200
        assert body["count"] == 0
        assert body["notifications"] == []


# ===========================================================================
# GET /api/notifications?limit=N
# ===========================================================================


class TestGetNotificationsLimit:
    @patch("notification_service.DashboardChannel")
    def test_limit_param_passed_to_channel(self, mock_channel_cls):
        mock_instance = MagicMock()
        mock_instance.get_dashboard_notifications.return_value = []
        mock_channel_cls.return_value = mock_instance

        get_notifications({"limit": "10"})

        mock_instance.get_dashboard_notifications.assert_called_once_with(limit=10, unread_only=False)

    @patch("notification_service.DashboardChannel")
    def test_limit_clamped_to_200(self, mock_channel_cls):
        mock_instance = MagicMock()
        mock_instance.get_dashboard_notifications.return_value = []
        mock_channel_cls.return_value = mock_instance

        get_notifications({"limit": "500"})

        mock_instance.get_dashboard_notifications.assert_called_once_with(limit=200, unread_only=False)


# ===========================================================================
# GET /api/notifications?unread_only=true
# ===========================================================================


class TestGetNotificationsUnreadOnly:
    @patch("notification_service.DashboardChannel")
    def test_unread_only_true(self, mock_channel_cls):
        mock_instance = MagicMock()
        mock_instance.get_dashboard_notifications.return_value = [SAMPLE_NOTIFICATIONS[0]]
        mock_channel_cls.return_value = mock_instance

        status, body = _resp(get_notifications({"unread_only": "true"}))

        assert status == 200
        assert body["count"] == 1
        mock_instance.get_dashboard_notifications.assert_called_once_with(limit=50, unread_only=True)

    @patch("notification_service.DashboardChannel")
    def test_unread_only_false_by_default(self, mock_channel_cls):
        mock_instance = MagicMock()
        mock_instance.get_dashboard_notifications.return_value = []
        mock_channel_cls.return_value = mock_instance

        get_notifications({})

        mock_instance.get_dashboard_notifications.assert_called_once_with(limit=50, unread_only=False)


# ===========================================================================
# PUT /api/notifications/{id}/read — success
# ===========================================================================


class TestMarkNotificationReadSuccess:
    @patch("notification_service.DashboardChannel")
    def test_mark_read_returns_success(self, mock_channel_cls):
        mock_instance = MagicMock()
        mock_instance.mark_notification_read.return_value = True
        mock_channel_cls.return_value = mock_instance

        status, body = _resp(mark_notification_read("dashboard_1001"))

        assert status == 200
        assert body["success"] is True
        mock_instance.mark_notification_read.assert_called_once_with("dashboard_1001")


# ===========================================================================
# PUT /api/notifications/{id}/read — 404
# ===========================================================================


class TestMarkNotificationRead404:
    @patch("notification_service.DashboardChannel")
    def test_mark_read_not_found(self, mock_channel_cls):
        mock_instance = MagicMock()
        mock_instance.mark_notification_read.return_value = False
        mock_channel_cls.return_value = mock_instance

        status, body = _resp(mark_notification_read("nonexistent-id"))

        assert status == 404
        assert "not found" in body["error"].lower()


# ===========================================================================
# Error handling
# ===========================================================================


class TestNotificationErrorHandling:
    @patch("notification_service.DashboardChannel")
    def test_get_notifications_handles_exception(self, mock_channel_cls):
        mock_channel_cls.side_effect = RuntimeError("DynamoDB unavailable")

        status, body = _resp(get_notifications({}))

        assert status == 500
        assert "failed to fetch notifications" in body["error"].lower()

    @patch("notification_service.DashboardChannel")
    def test_mark_read_handles_exception(self, mock_channel_cls):
        mock_channel_cls.side_effect = RuntimeError("DynamoDB unavailable")

        status, body = _resp(mark_notification_read("dashboard_1001"))

        assert status == 500
        assert "failed to mark notification" in body["error"].lower()
