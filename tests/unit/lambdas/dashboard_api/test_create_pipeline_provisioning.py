# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for inlined pipeline provisioning logic (Task 5.9).

Validates: AC-10.1, AC-10.6
Verifies that create_pipeline creates the S3 prefix and enables EventBridge
notifications on the upload bucket via the inlined _provision_pipeline_resources.
"""

import os
import sys
from unittest.mock import MagicMock, patch

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "adi-source-files-123456789012-us-west-2",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ["UPLOAD_BUCKET"] = "adi-source-files-123456789012-us-west-2"

# Add handler package to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)

try:
    from handlers.pipelines import _ensure_s3_eventbridge_enabled, _provision_pipeline_resources
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

UPLOAD_BUCKET = "adi-source-files-123456789012-us-west-2"


class TestEnsureS3EventBridgeEnabled:
    """Tests for _ensure_s3_eventbridge_enabled."""

    @patch("handlers.pipelines_crud.s3_client")
    def test_enables_eventbridge_when_not_configured(self, mock_s3):
        """Should call put_bucket_notification_configuration with EventBridgeConfiguration."""
        mock_s3.get_bucket_notification_configuration.return_value = {}

        result = _ensure_s3_eventbridge_enabled(UPLOAD_BUCKET)

        assert result is True
        mock_s3.put_bucket_notification_configuration.assert_called_once()
        call_args = mock_s3.put_bucket_notification_configuration.call_args
        config = call_args[1]["NotificationConfiguration"]
        assert "EventBridgeConfiguration" in config

    @patch("handlers.pipelines_crud.s3_client")
    def test_skips_when_already_enabled(self, mock_s3):
        """Should not call put when EventBridge is already enabled."""
        mock_s3.get_bucket_notification_configuration.return_value = {"EventBridgeConfiguration": {}}

        result = _ensure_s3_eventbridge_enabled(UPLOAD_BUCKET)

        assert result is True
        mock_s3.put_bucket_notification_configuration.assert_not_called()

    @patch("handlers.pipelines_crud.s3_client")
    def test_preserves_existing_configurations(self, mock_s3):
        """Should preserve existing Lambda/SNS/SQS configs when enabling EventBridge."""
        existing_lambda_config = [{"Id": "existing", "LambdaFunctionArn": "arn:aws:lambda:..."}]
        mock_s3.get_bucket_notification_configuration.return_value = {
            "LambdaFunctionConfigurations": existing_lambda_config,
        }

        result = _ensure_s3_eventbridge_enabled(UPLOAD_BUCKET)

        assert result is True
        call_args = mock_s3.put_bucket_notification_configuration.call_args
        config = call_args[1]["NotificationConfiguration"]
        assert config["LambdaFunctionConfigurations"] == existing_lambda_config
        assert "EventBridgeConfiguration" in config

    @patch("handlers.pipelines_crud.s3_client")
    def test_returns_false_on_put_error(self, mock_s3):
        """Should return False when put_bucket_notification_configuration fails."""
        mock_s3.get_bucket_notification_configuration.return_value = {}
        mock_s3.put_bucket_notification_configuration.side_effect = Exception("Access denied")

        result = _ensure_s3_eventbridge_enabled(UPLOAD_BUCKET)

        assert result is False


class TestProvisionPipelineResources:
    """Tests for _provision_pipeline_resources."""

    @patch("handlers.pipelines_crud.s3_client")
    @patch("handlers.pipelines_crud.require_env", return_value=UPLOAD_BUCKET)
    def test_creates_s3_prefix(self, mock_env, mock_s3):
        """Should call s3.put_object to create the pipeline prefix."""
        mock_s3.get_bucket_notification_configuration.return_value = {"EventBridgeConfiguration": {}}

        _provision_pipeline_resources("test-pipeline-abc123", "Test Pipeline")

        mock_s3.put_object.assert_called_once_with(
            Bucket=UPLOAD_BUCKET,
            Key="test-pipeline-abc123/",
        )

    @patch("handlers.pipelines_crud.s3_client")
    @patch("handlers.pipelines_crud.require_env", return_value=UPLOAD_BUCKET)
    def test_enables_eventbridge(self, mock_env, mock_s3):
        """Should call _ensure_s3_eventbridge_enabled on the upload bucket."""
        mock_s3.get_bucket_notification_configuration.return_value = {}

        _provision_pipeline_resources("test-pipeline-abc123", "Test Pipeline")

        mock_s3.get_bucket_notification_configuration.assert_called_once_with(Bucket=UPLOAD_BUCKET)
        mock_s3.put_bucket_notification_configuration.assert_called_once()

    @patch("handlers.pipelines_crud.s3_client")
    @patch("handlers.pipelines_crud.require_env", return_value=UPLOAD_BUCKET)
    def test_prefix_creation_before_eventbridge(self, mock_env, mock_s3):
        """S3 prefix should be created before EventBridge is enabled."""
        call_order = []
        mock_s3.put_object.side_effect = lambda **kw: call_order.append("put_object")
        mock_s3.get_bucket_notification_configuration.return_value = {}
        mock_s3.get_bucket_notification_configuration.side_effect = lambda **kw: (
            call_order.append("get_notification") or {}
        )

        _provision_pipeline_resources("test-pipeline-abc123", "Test Pipeline")

        assert call_order[0] == "put_object"
