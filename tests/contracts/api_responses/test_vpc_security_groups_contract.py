# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /api/resources/vpc/{vpc_id}/security-groups response shape (P20).

Pins the wire shape:

    HTTP 200 → {"security_groups": [{sg_id, name, description}], "count": N}
    HTTP 400 → {"error": "Invalid vpc_id format"}

Validates: P20 Requirements 5.3, 5.5
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


class TestListSecurityGroupsResponseShape:
    @patch("handlers.vpc_resources.ec2_client")
    def test_200_response_shape(self, mock_ec2):
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-0a1b2c3d",
                    "GroupName": "default",
                    "Description": "default VPC security group",
                }
            ]
        }

        from handlers.vpc_resources import list_security_groups

        response = list_security_groups({"pathParameters": {"vpc_id": "vpc-0a1b2c3d"}}, None)
        _assert_json(response)
        status, body = _parse(response)
        assert status == 200

        assert set(body.keys()) == {"security_groups", "count"}
        assert body["count"] == len(body["security_groups"])

        for sg in body["security_groups"]:
            assert set(sg.keys()) == {"sg_id", "name", "description"}

    def test_400_invalid_vpc_id_response_shape(self):
        from handlers.vpc_resources import list_security_groups

        response = list_security_groups({"pathParameters": {"vpc_id": "not-a-vpc-id"}}, None)
        _assert_json(response)
        status, body = _parse(response)
        assert status == 400
        assert body["error"] == "Invalid vpc_id format"
