# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for VPC / Subnet / Security Group resource endpoints (P20).

Covers the three new public handlers added by P20 Tasks 2.2, 2.3, 2.4:

    * ``list_vpcs``               — GET /api/resources/vpc/list
    * ``list_subnets``            — GET /api/resources/vpc/{vpc_id}/subnets
    * ``list_security_groups``    — GET /api/resources/vpc/{vpc_id}/security-groups

Test scope (P20 Task 2.6):
    * Happy-path — 0 / 1 / many records returned with and without
      ``Name`` tags (Req 5.1)
    * Filter applied to subnet / SG calls (Req 5.2, 5.3)
    * HTTP 500 with ``{error, message}`` shape on ``ClientError`` (Req
      5.6)
    * Idempotency — no AWS-mutating calls (Req 5.8)

Validates: P20 Requirements 5.1, 5.2, 5.3, 5.6, 5.8
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler module.
# Mirrors test_jdbc_resources.py / test_jdbc_target_detect_schema.py.
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


# ---------------------------------------------------------------------------
# list_vpcs
# ---------------------------------------------------------------------------


class TestListVpcs:
    """Tests for GET /api/resources/vpc/list. Validates: Req 5.1, 5.6, 5.8."""

    @patch("handlers.vpc_resources.ec2_client")
    def test_empty_vpc_list(self, mock_ec2):
        mock_ec2.describe_vpcs.return_value = {"Vpcs": []}

        from handlers.vpc_resources import list_vpcs

        response = list_vpcs({}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body == {"vpcs": [], "count": 0}

    @patch("handlers.vpc_resources.ec2_client")
    def test_single_vpc_with_name_tag(self, mock_ec2):
        mock_ec2.describe_vpcs.return_value = {
            "Vpcs": [
                {
                    "VpcId": "vpc-0a1b2c3d",
                    "CidrBlock": "10.0.0.0/16",
                    "IsDefault": False,
                    "Tags": [{"Key": "Name", "Value": "AgenticDataIngestion-dev"}],
                }
            ]
        }

        from handlers.vpc_resources import list_vpcs

        response = list_vpcs({}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["count"] == 1
        assert body["vpcs"][0] == {
            "vpc_id": "vpc-0a1b2c3d",
            "name": "AgenticDataIngestion-dev",
            "is_default": False,
            "cidr": "10.0.0.0/16",
        }

    @patch("handlers.vpc_resources.ec2_client")
    def test_multiple_vpcs_one_without_name_tag(self, mock_ec2):
        mock_ec2.describe_vpcs.return_value = {
            "Vpcs": [
                {
                    "VpcId": "vpc-1111",
                    "CidrBlock": "10.0.0.0/16",
                    "IsDefault": True,
                    "Tags": [],
                },
                {
                    "VpcId": "vpc-2222",
                    "CidrBlock": "10.1.0.0/16",
                    "IsDefault": False,
                    "Tags": [{"Key": "Name", "Value": "prod"}],
                },
            ]
        }

        from handlers.vpc_resources import list_vpcs

        response = list_vpcs({}, None)
        body = json.loads(response["body"])
        assert body["count"] == 2
        # First VPC has no Name tag → empty string per Req 5.1.
        assert body["vpcs"][0]["name"] == ""
        assert body["vpcs"][0]["is_default"] is True
        # Second VPC's Name tag is read.
        assert body["vpcs"][1]["name"] == "prod"
        assert body["vpcs"][1]["is_default"] is False

    @patch("handlers.vpc_resources.ec2_client")
    def test_client_error_returns_500(self, mock_ec2):
        mock_ec2.describe_vpcs.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "ec2 boom"}},
            "DescribeVpcs",
        )

        from handlers.vpc_resources import list_vpcs

        response = list_vpcs({}, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["error"] == "Failed to list VPCs"
        assert "ec2 boom" in body["message"]

    @patch("handlers.vpc_resources.ec2_client")
    def test_no_mutating_ec2_calls(self, mock_ec2):
        """Req 5.8 — endpoint MUST NOT call any AWS-mutating EC2 API."""
        mock_ec2.describe_vpcs.return_value = {"Vpcs": []}

        from handlers.vpc_resources import list_vpcs

        list_vpcs({}, None)
        # Only describe_vpcs was invoked. Any "create" or "delete"
        # invocations would be a regression.
        for attr in (
            "create_vpc",
            "delete_vpc",
            "modify_vpc_attribute",
            "create_subnet",
            "create_security_group",
        ):
            assert not getattr(mock_ec2, attr).called, f"list_vpcs must not call {attr}"


# ---------------------------------------------------------------------------
# list_subnets
# ---------------------------------------------------------------------------


class TestListSubnets:
    """Tests for GET /api/resources/vpc/{vpc_id}/subnets.

    Validates: Req 5.2, 5.5, 5.6, 5.8.
    """

    @patch("handlers.vpc_resources.ec2_client")
    def test_happy_path_filters_by_vpc_id(self, mock_ec2):
        vpc_id = "vpc-0a1b2c3d"
        mock_ec2.describe_subnets.return_value = {
            "Subnets": [
                {
                    "SubnetId": "subnet-aaaa1111",
                    "AvailabilityZone": "us-west-2a",
                    "CidrBlock": "10.0.1.0/24",
                    "Tags": [{"Key": "Name", "Value": "private-2a"}],
                }
            ]
        }

        from handlers.vpc_resources import list_subnets

        response = list_subnets({"pathParameters": {"vpc_id": vpc_id}}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["count"] == 1
        assert body["subnets"][0] == {
            "subnet_id": "subnet-aaaa1111",
            "name": "private-2a",
            "availability_zone": "us-west-2a",
            "cidr": "10.0.1.0/24",
        }

        # The vpc-id filter MUST be applied to the EC2 call.
        called_filters = mock_ec2.describe_subnets.call_args.kwargs["Filters"]
        assert {"Name": "vpc-id", "Values": [vpc_id]} in called_filters

    @patch("handlers.vpc_resources.ec2_client")
    def test_invalid_vpc_id_returns_400_without_calling_ec2(self, mock_ec2):
        """Req 5.5 — fail-closed regex validation."""
        from handlers.vpc_resources import list_subnets

        response = list_subnets({"pathParameters": {"vpc_id": "not-a-vpc-id"}}, None)
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"] == "Invalid vpc_id format"

        # CRITICAL: EC2 must not have been called.
        assert not mock_ec2.describe_subnets.called

    @patch("handlers.vpc_resources.ec2_client")
    def test_missing_vpc_id_returns_400(self, mock_ec2):
        from handlers.vpc_resources import list_subnets

        response = list_subnets({"pathParameters": {}}, None)
        assert response["statusCode"] == 400
        assert not mock_ec2.describe_subnets.called

    @patch("handlers.vpc_resources.ec2_client")
    def test_client_error_returns_500(self, mock_ec2):
        mock_ec2.describe_subnets.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "ec2 boom"}},
            "DescribeSubnets",
        )

        from handlers.vpc_resources import list_subnets

        response = list_subnets({"pathParameters": {"vpc_id": "vpc-0a1b2c3d"}}, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["error"] == "Failed to list subnets"
        assert "ec2 boom" in body["message"]


# ---------------------------------------------------------------------------
# list_security_groups
# ---------------------------------------------------------------------------


class TestListSecurityGroups:
    """Tests for GET /api/resources/vpc/{vpc_id}/security-groups.

    Validates: Req 5.3, 5.5, 5.6, 5.8.
    """

    @patch("handlers.vpc_resources.ec2_client")
    def test_happy_path_filters_by_vpc_id(self, mock_ec2):
        vpc_id = "vpc-0a1b2c3d"
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                {
                    "GroupId": "sg-1111aaaa",
                    "GroupName": "default",
                    "Description": "default VPC security group",
                },
                {
                    "GroupId": "sg-2222bbbb",
                    "GroupName": "rds-postgres",
                    "Description": "Allow inbound 5432 from app",
                },
            ]
        }

        from handlers.vpc_resources import list_security_groups

        response = list_security_groups({"pathParameters": {"vpc_id": vpc_id}}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["count"] == 2
        assert body["security_groups"][0] == {
            "sg_id": "sg-1111aaaa",
            "name": "default",
            "description": "default VPC security group",
        }

        called_filters = mock_ec2.describe_security_groups.call_args.kwargs["Filters"]
        assert {"Name": "vpc-id", "Values": [vpc_id]} in called_filters

    @patch("handlers.vpc_resources.ec2_client")
    def test_invalid_vpc_id_returns_400_without_calling_ec2(self, mock_ec2):
        from handlers.vpc_resources import list_security_groups

        response = list_security_groups({"pathParameters": {"vpc_id": "VPC-NOT-VALID"}}, None)
        assert response["statusCode"] == 400
        assert not mock_ec2.describe_security_groups.called

    @patch("handlers.vpc_resources.ec2_client")
    def test_client_error_returns_500(self, mock_ec2):
        mock_ec2.describe_security_groups.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "ec2 boom"}},
            "DescribeSecurityGroups",
        )

        from handlers.vpc_resources import list_security_groups

        response = list_security_groups({"pathParameters": {"vpc_id": "vpc-0a1b2c3d"}}, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["error"] == "Failed to list security groups"
