# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests for VPC / Subnet / Security Group endpoints (P20).

Validates: P20 Requirements 5.5
"""

from __future__ import annotations

import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler module.
# ---------------------------------------------------------------------------

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
mock_env_utils.get_aws_account_id = lambda: "123456789012"


@pytest.fixture(autouse=True)
def _env_utils_stub(monkeypatch):
    """Install the env_utils stub for each test, and only for the test.

    The handler modules are imported inside the tests, so the stub must be in
    sys.modules then. Installed at module scope it leaked into every module
    collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    """
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)


os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)


# Feature: p20-jdbc-manual-mode, Property 10: vpc_id path-parameter regex validation
class TestProperty10VpcIdRegexFailClosed:
    """Property 10: ``vpc_id`` path-parameter regex enforces fail-closed.

    *For any* string ``vpc_id``, ``GET .../vpc/{vpc_id}/subnets`` and
    ``GET .../vpc/{vpc_id}/security-groups`` accept the request and
    invoke EC2 if and only if ``vpc_id`` matches
    ``^vpc-[a-f0-9]{8,17}$``. Non-matching inputs return HTTP 400 with
    ``{error: "Invalid vpc_id format"}`` AND zero EC2 calls.

    Validates: P20 Requirements 5.5
    """

    _VPC_ID_REGEX = r"^vpc-[a-f0-9]{8,17}$"

    @given(s=st.text(min_size=0, max_size=64))
    @settings(max_examples=100)
    @patch("handlers.vpc_resources.ec2_client")
    def test_subnets_endpoint_biconditional(self, mock_ec2, s: str):
        """list_subnets accepts iff ``vpc_id`` matches the regex."""
        mock_ec2.describe_subnets.return_value = {"Subnets": []}

        from handlers.vpc_resources import list_subnets

        valid = bool(re.fullmatch(self._VPC_ID_REGEX, s))
        response = list_subnets({"pathParameters": {"vpc_id": s}}, None)

        if valid:
            assert response["statusCode"] == 200, f"Valid vpc_id {s!r} must return 200; got {response['statusCode']}"
            assert mock_ec2.describe_subnets.call_count == 1
        else:
            assert response["statusCode"] == 400, f"Invalid vpc_id {s!r} must return 400; got {response['statusCode']}"
            body = json.loads(response["body"])
            assert body["error"] == "Invalid vpc_id format"
            # EC2 MUST NOT have been called for invalid input.
            assert mock_ec2.describe_subnets.call_count == 0

    @given(s=st.text(min_size=0, max_size=64))
    @settings(max_examples=100)
    @patch("handlers.vpc_resources.ec2_client")
    def test_security_groups_endpoint_biconditional(self, mock_ec2, s: str):
        """list_security_groups accepts iff ``vpc_id`` matches the regex."""
        mock_ec2.describe_security_groups.return_value = {"SecurityGroups": []}

        from handlers.vpc_resources import list_security_groups

        valid = bool(re.fullmatch(self._VPC_ID_REGEX, s))
        response = list_security_groups({"pathParameters": {"vpc_id": s}}, None)

        if valid:
            assert response["statusCode"] == 200
            assert mock_ec2.describe_security_groups.call_count == 1
        else:
            assert response["statusCode"] == 400
            assert mock_ec2.describe_security_groups.call_count == 0
