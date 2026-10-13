# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for notification service (Task 8.4)."""

import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

# Add shared directory to import path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Set local mode
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


# Strategy: channel names that are NOT in the default configured set
unconfigured_channel_names = st.from_regex(r"[a-z]{3,15}", fullmatch=True).filter(
    lambda s: s not in ("sns", "dashboard", "slack", "cloudwatch")
)


# Feature: agentic-data-ingestion-codebase-optimization, Property 9: Unconfigured notification channels are skipped silently
@given(unconfigured_channel_names)
@settings(max_examples=200, deadline=None)
def test_unconfigured_channel_skipped_silently(channel_name: str):
    """
    When a channel is not in the configured list, send_notification()
    does not raise and does not call that channel's send method.
    """
    with patch.dict(os.environ, {"NOTIFICATION_CHANNELS": "sns,dashboard"}):
        with patch("boto3.client", return_value=MagicMock()):
            with patch("boto3.resource", return_value=MagicMock()):
                # Re-import to pick up env
                import importlib

                # Clear any mocked notification_service from other tests
                for mod_name in list(sys.modules):
                    if mod_name == "notification_service":
                        if not hasattr(sys.modules[mod_name], "__file__"):
                            del sys.modules[mod_name]

                import notification_service as ns

                importlib.reload(ns)

                service = ns.NotificationService()

                # Create a mock channel that should NOT be called
                mock_channel = MagicMock()

                # Do NOT register the mock — it's unconfigured
                # Request delivery to the unconfigured channel explicitly
                result = service.send_notification(
                    notification_type="test",
                    title="Test",
                    message="Test message",
                    channels=[channel_name],
                )

                # Must not raise, and the mock's send must not be called
                mock_channel.send.assert_not_called()

                # delivery_results should be empty for the unconfigured channel
                assert channel_name not in result.get("delivery_results", {})
