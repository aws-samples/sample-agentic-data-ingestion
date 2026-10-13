# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK assertion tests for RDS Database Target IAM permissions in foundation-stack.ts.

Validates Requirement 11 (IAM Permission Updates):
- 11.1: rds:DescribeDBInstances and rds:DescribeDBClusters on infrastructureRole
- 11.2: glue:GetConnections, glue:GetConnection scoped to Glue catalog resources
- 11.3: secretsmanager:ListSecrets and secretsmanager:GetSecretValue scoped to agentic-data-ingestion/* secrets
- 11.4: glue:CreateConnection scoped to Glue catalog resources
"""

import os
import re

import pytest

FOUNDATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "foundation-stack.ts"
)


@pytest.fixture(scope="module")
def foundation_stack_content():
    with open(FOUNDATION_STACK_PATH) as f:
        return f.read()


def _extract_infrastructure_role_section(content: str) -> str:
    """Extract the infrastructureRole definition and all its addToPolicy calls."""
    # Find from 'this.infrastructureRole = lambdaRole' to the next role definition
    pattern = r"(this\.infrastructureRole\s*=\s*lambdaRole.*?)(?=this\.\w+Role\s*=\s*(?:lambdaRole|new\s+iam\.Role))"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return ""
    return match.group(1)


def _find_policy_statement_containing(section: str, action: str) -> str:
    """Find the full PolicyStatement block containing the given action string."""
    # Find all addToPolicy calls with PolicyStatement
    for match in re.finditer(r"new iam\.PolicyStatement\(\{", section):
        start = match.start()
        # Track brace depth to find the matching closing
        depth = 0
        i = match.end() - 1  # start at the opening {
        while i < len(section):
            if section[i] == "{":
                depth += 1
            elif section[i] == "}":
                depth -= 1
                if depth == 0:
                    # Find the closing ')' after '}'
                    end = section.index(")", i) + 1
                    block = section[start:end]
                    if action in block:
                        return block
                    break
            i += 1
    return ""


class TestRdsDiscoveryPermissions:
    """infrastructureRole has rds:DescribeDBInstances and rds:DescribeDBClusters."""

    def test_rds_policy_statement_exists(self, foundation_stack_content):
        section = _extract_infrastructure_role_section(foundation_stack_content)
        assert section, "infrastructureRole section not found in foundation-stack.ts"
        assert "rds:DescribeDBInstances" in section
        assert "rds:DescribeDBClusters" in section

    def test_rds_actions_in_same_statement(self, foundation_stack_content):
        section = _extract_infrastructure_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "rds:DescribeDBInstances")
        assert block, "PolicyStatement with rds:DescribeDBInstances not found"
        assert "rds:DescribeDBClusters" in block

    def test_rds_resources_wildcard(self, foundation_stack_content):
        section = _extract_infrastructure_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "rds:DescribeDBInstances")
        assert block, "PolicyStatement with rds:DescribeDBInstances not found"
        assert "resources: ['*']" in block


class TestGlueConnectionPermissions:
    """infrastructureRole has glue:GetConnections, glue:GetConnection, glue:CreateConnection."""

    def test_glue_connection_actions_exist(self, foundation_stack_content):
        section = _extract_infrastructure_role_section(foundation_stack_content)
        assert "glue:GetConnections" in section
        assert "glue:GetConnection" in section
        assert "glue:CreateConnection" in section

    def test_glue_connection_actions_in_same_statement(self, foundation_stack_content):
        section = _extract_infrastructure_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "glue:GetConnections")
        assert block, "PolicyStatement with glue:GetConnections not found"
        assert "glue:GetConnection" in block
        assert "glue:CreateConnection" in block

    def test_glue_connection_scoped_to_catalog(self, foundation_stack_content):
        section = _extract_infrastructure_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "glue:GetConnections")
        assert block, "PolicyStatement with glue:GetConnections not found"
        assert ":glue:${region}:${account}:catalog" in block

    def test_glue_connection_scoped_to_connections(self, foundation_stack_content):
        section = _extract_infrastructure_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "glue:GetConnections")
        assert block, "PolicyStatement with glue:GetConnections not found"
        assert ":glue:${region}:${account}:connection/*" in block

    def test_glue_connection_statement_includes_tag_resource(self, foundation_stack_content):
        """CreateConnection with a Tags argument requires glue:TagResource.

        Glue evaluates the tagging permission against the catalog resource
        ARN, so without TagResource in this statement CreateConnection fails
        with "not authorized to perform: glue:TagResource on resource:
        ...:catalog". TagResource must therefore live in the SAME statement
        as CreateConnection (which is scoped to catalog + connection/*).
        """
        section = _extract_infrastructure_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "glue:CreateConnection")
        assert block, "PolicyStatement with glue:CreateConnection not found"
        assert "glue:TagResource" in block
        # The tagging permission must reach the catalog ARN — that is the
        # resource Glue checks TagResource against on CreateConnection.
        assert ":glue:${region}:${account}:catalog" in block


class TestSecretsManagerPermissions:
    """infrastructureRole has secretsmanager:ListSecrets and secretsmanager:GetSecretValue."""

    def test_list_secrets_action_exists(self, foundation_stack_content):
        section = _extract_infrastructure_role_section(foundation_stack_content)
        assert "secretsmanager:ListSecrets" in section

    def test_get_secret_value_action_exists(self, foundation_stack_content):
        section = _extract_infrastructure_role_section(foundation_stack_content)
        assert "secretsmanager:GetSecretValue" in section

    def test_list_secrets_uses_wildcard_resource(self, foundation_stack_content):
        """ListSecrets does not support resource-level permissions — must use *."""
        section = _extract_infrastructure_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "secretsmanager:ListSecrets")
        assert block, "PolicyStatement with secretsmanager:ListSecrets not found"
        assert "resources: ['*']" in block

    def test_get_secret_value_scoped_to_adi(self, foundation_stack_content):
        """GetSecretValue should be scoped to agentic-data-ingestion/* secrets."""
        section = _extract_infrastructure_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "secretsmanager:GetSecretValue")
        assert block, "PolicyStatement with secretsmanager:GetSecretValue not found"
        assert ":secretsmanager:${region}:${account}:secret:agentic-data-ingestion/*" in block

    def test_list_and_get_are_separate_statements(self, foundation_stack_content):
        """ListSecrets and GetSecretValue should be in separate statements due to different resource scoping."""
        section = _extract_infrastructure_role_section(foundation_stack_content)
        # ListSecrets statement should NOT contain GetSecretValue
        list_pattern = r"new iam\.PolicyStatement\(\{[^}]*secretsmanager:ListSecrets[^}]*?\}\)"
        list_match = re.search(list_pattern, section, re.DOTALL)
        assert list_match, "PolicyStatement with secretsmanager:ListSecrets not found"
        list_block = list_match.group(0)
        assert "secretsmanager:GetSecretValue" not in list_block
