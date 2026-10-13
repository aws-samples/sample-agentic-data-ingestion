# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK assertion test for the P20 EC2 DescribeVpcs grant.

The Foundation_Stack adds ``ec2:DescribeVpcs`` to the dashboard API
role's existing EC2 statement to back the manual JDBC connection mode's
VPC dropdown (P20 Req 5.7). This test pins the IAM policy so future
edits cannot silently delete any of the three describe actions
(``DescribeVpcs``, ``DescribeSubnets``, ``DescribeSecurityGroups``).

Validates: P20 Requirements 5.7, 14.6
"""

import os
import re

import pytest

FOUNDATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "deploy-cdk",
    "lib",
    "foundation-stack.ts",
)


@pytest.fixture(scope="module")
def foundation_stack_content():
    with open(FOUNDATION_STACK_PATH) as f:
        return f.read()


def _ec2_policy_block(content: str) -> str:
    """Return the body of the dashboard-API EC2 PolicyStatement.

    The Foundation stack now defines more than one EC2 ``Describe*``
    PolicyStatement (the gluePipelineRole crawler-networking block and the
    infrastructureRole dashboard block). The dashboard block is uniquely
    identified by ``ec2:DescribeVpcs`` (P20 Req 5.7), so we select that one.
    """
    pattern = r"new iam\.PolicyStatement\(\{[^{}]*ec2:Describe[^{}]*?\}\)"
    matches = re.findall(pattern, content)
    assert matches, "Could not locate any EC2 PolicyStatement"
    # The dashboard-API EC2 statement is the one carrying DescribeVpcs.
    candidate = [m for m in matches if "ec2:DescribeVpcs" in m]
    assert candidate, (
        "EC2 PolicyStatement with DescribeVpcs not found — the dashboard API "
        "role's VPC-dropdown grant (P20 Req 5.7) is missing"
    )
    assert len(candidate) == 1, f"Expected exactly one EC2 PolicyStatement with DescribeVpcs, found {len(candidate)}"
    return candidate[0]


class TestP20Ec2DescribeVpcsGrant:
    """Validates: P20 Req 5.7."""

    def test_describe_vpcs_present(self, foundation_stack_content):
        """P20 Req 5.7 — ``ec2:DescribeVpcs`` is added to the EC2 statement."""
        block = _ec2_policy_block(foundation_stack_content)
        assert "ec2:DescribeVpcs" in block, (
            "ec2:DescribeVpcs missing from the dashboard API role's EC2 statement (P20 Req 5.7)"
        )

    def test_describe_subnets_still_present(self, foundation_stack_content):
        """P17 grant preserved (P20 Req 14.6 — no regressions)."""
        block = _ec2_policy_block(foundation_stack_content)
        assert "ec2:DescribeSubnets" in block

    def test_describe_security_groups_still_present(self, foundation_stack_content):
        """P17 grant preserved."""
        block = _ec2_policy_block(foundation_stack_content)
        assert "ec2:DescribeSecurityGroups" in block

    def test_resources_wildcard(self, foundation_stack_content):
        """EC2 read APIs do not support resource-level scoping — must use ``*``."""
        block = _ec2_policy_block(foundation_stack_content)
        assert "resources: ['*']" in block
