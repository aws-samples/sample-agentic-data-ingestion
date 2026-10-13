# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test: create pipeline via API, verify S3 prefix exists.

Validates: AC-10.1, AC-10.6
- Creates a pipeline via the dashboard API create_pipeline handler
- Verifies the S3 prefix was created in the upload bucket
- Verifies EventBridge notification is configured on the bucket

All tests use moto mocks — no real AWS calls are made.
"""

import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration

AWS_REGION = "us-east-1"

# Stub out Lambda-layer dependencies
mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: os.environ.get(name, default or name)
sys.modules["env_utils"] = mock_env_utils
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

os.environ.setdefault("AWS_DEFAULT_REGION", AWS_REGION)
os.environ.setdefault("AWS_REGION", AWS_REGION)

# Add handler package to path
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)

# Add shared/ to path so handler modules can resolve their Lambda-layer imports
# (audit_utils, env_utils, …) when imported fresh — i.e. when this file runs
# first and no other test has pre-loaded them. Previously this worked only by
# accident of collection order.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)

# NOTE: handler functions are imported INSIDE each test (via
# ``import handlers.pipelines_crud``) rather than here at module scope. These
# integration files purge handlers.* from sys.modules at collection time, so a
# top-level import would capture a stale module object and break patch().

TEST_ID = uuid.uuid4().hex[:8]
TEST_PIPELINE_ID = f"inttest-pipeline-{TEST_ID}"
UPLOAD_BUCKET = "adi-source-files-test"


class TestProvisionPipelineResources:
    """Test pipeline provisioning with moto S3 mocks."""

    @mock_aws
    def test_provision_creates_s3_prefix(self):
        """_provision_pipeline_resources should create the pipeline prefix in S3."""
        # Resolve the handler through the LIVE module object. These integration
        # files do manual sys.modules surgery at collection time (and the P21
        # Task 7.2 façade split fans handlers out into many siblings), so a
        # function captured by a top-level import can become stale — patch()
        # would then target a different module object than the function's
        # __globals__. Importing here binds both to the current sys.modules entry.
        import handlers.pipelines_crud as crud

        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.create_bucket(Bucket=UPLOAD_BUCKET)

        with patch.object(crud, "s3_client", s3), patch.object(crud, "require_env", return_value=UPLOAD_BUCKET):
            crud._provision_pipeline_resources(TEST_PIPELINE_ID, "Integration Test Pipeline")

        # Verify the prefix object exists
        response = s3.list_objects_v2(
            Bucket=UPLOAD_BUCKET,
            Prefix=f"{TEST_PIPELINE_ID}/",
            MaxKeys=1,
        )
        assert response.get("KeyCount", 0) >= 1, f"Expected S3 prefix {TEST_PIPELINE_ID}/ to exist in {UPLOAD_BUCKET}"

    @mock_aws
    def test_eventbridge_enabled_on_bucket(self):
        """_ensure_s3_eventbridge_enabled should configure EventBridge on the bucket."""
        import handlers.pipelines_crud as crud

        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.create_bucket(Bucket=UPLOAD_BUCKET)

        # moto 5.x accepts put_bucket_notification_configuration but does NOT
        # echo EventBridgeConfiguration back on read, so asserting on the
        # readback would test moto rather than our handler. Instead, spy on the
        # put call to verify the handler requests EventBridge with the correct
        # NotificationConfiguration payload.
        captured: dict = {}
        real_put = s3.put_bucket_notification_configuration

        def _spy_put(**kwargs):
            captured.update(kwargs)
            return real_put(**kwargs)

        s3.put_bucket_notification_configuration = _spy_put

        with patch.object(crud, "s3_client", s3):
            result = crud._ensure_s3_eventbridge_enabled(UPLOAD_BUCKET)

        assert result is True
        assert captured.get("Bucket") == UPLOAD_BUCKET
        assert "EventBridgeConfiguration" in captured.get("NotificationConfiguration", {}), (
            "Handler should request EventBridgeConfiguration on the bucket"
        )
