# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test layer conftest — auto-applies @pytest.mark.integration and
ensures no integration test can accidentally hit real AWS.

Safety guard: sets AWS_DEFAULT_REGION and AWS_ACCESS_KEY_ID to dummy values
so that any boto3 call outside a moto mock will fail fast with an auth error
rather than silently hitting a real AWS account.
"""

import os

import pytest

_THIS_DIR = os.path.dirname(__file__)

# ---------------------------------------------------------------------------
# Safety guard: prevent accidental real-AWS calls from integration tests.
# These env vars are set BEFORE any test runs.  Tests that use @mock_aws
# will work fine because moto intercepts all boto3 calls.  Tests that
# accidentally skip moto will get an immediate auth error instead of
# silently writing to real DynamoDB tables.
# ---------------------------------------------------------------------------
# Only apply the guard when NOT explicitly running in real-AWS mode
if os.environ.get("AGENTIC_DATA_INGESTION_TEST_REAL_AWS", "false").lower() != "true":
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    os.environ.setdefault("AWS_REGION", "us-east-1")
    # Dummy credentials ensure boto3 never falls through to real AWS
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"


def pytest_collection_modifyitems(items):
    """Automatically add the 'integration' marker to every test in this directory.

    Also skip any test that requires real AWS when AGENTIC_DATA_INGESTION_TEST_REAL_AWS is not set.
    """
    use_real_aws = os.environ.get("AGENTIC_DATA_INGESTION_TEST_REAL_AWS", "false").lower() == "true"
    for item in items:
        if os.path.commonpath([str(item.fspath), _THIS_DIR]) == _THIS_DIR:
            item.add_marker(pytest.mark.integration)
            # Auto-skip tests in the websocket/test_ws_integration.py module
            # unless real AWS mode is enabled — these tests require a deployed
            # WebSocket API and write to real DynamoDB.
            if not use_real_aws and "test_ws_integration" in str(item.fspath):
                item.add_marker(
                    pytest.mark.skip(
                        reason="Requires AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true and deployed WebSocket API"
                    )
                )
