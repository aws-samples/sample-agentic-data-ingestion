# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /api/resources/vpc/list response shape (P20).

Pins the wire shape that the wizard's `<ManualConnectionForm>` VPC
dropdown consumes:

    HTTP 200 → {"vpcs": [{vpc_id, name, is_default, cidr}], "count": N}

Validates: P20 Requirements 5.1
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
mock_env_utils.get_aws_account_id = lambda: "123456789012"
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)
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


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


def _assert_json(response):
    assert response["headers"]["Content-Type"] == "application/json"


class TestListVpcsResponseShape:
    @patch("handlers.vpc_resources.ec2_client")
    def test_response_shape(self, mock_ec2):
        mock_ec2.describe_vpcs.return_value = {
            "Vpcs": [
                {
                    "VpcId": "vpc-0a1b2c3d",
                    "CidrBlock": "10.0.0.0/16",
                    "IsDefault": False,
                    "Tags": [{"Key": "Name", "Value": "AgenticDataIngestion-dev"}],
                },
                {
                    "VpcId": "vpc-1234abcd",
                    "CidrBlock": "172.31.0.0/16",
                    "IsDefault": True,
                    "Tags": [],
                },
            ]
        }

        from handlers.vpc_resources import list_vpcs

        response = list_vpcs({}, None)
        _assert_json(response)
        status, body = _parse(response)
        assert status == 200

        # Top-level keys.
        assert set(body.keys()) == {"vpcs", "count"}
        assert isinstance(body["vpcs"], list)
        assert isinstance(body["count"], int)
        assert body["count"] == len(body["vpcs"])

        # Each VPC entry has exactly the documented keys.
        for vpc in body["vpcs"]:
            assert set(vpc.keys()) == {"vpc_id", "name", "is_default", "cidr"}
            assert isinstance(vpc["vpc_id"], str)
            assert isinstance(vpc["name"], str)
            assert isinstance(vpc["is_default"], bool)
            assert isinstance(vpc["cidr"], str)
