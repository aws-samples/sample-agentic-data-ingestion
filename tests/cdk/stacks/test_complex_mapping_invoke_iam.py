# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK assertion tests for complex mapping agent InvokeAgentRuntime permission.

Validates Requirements 2.1, 2.2:
- 2.1: schemaMappingRole has bedrock-agentcore:InvokeAgentRuntime scoped to
       arn:aws:bedrock-agentcore:<region>:<account>:runtime/agentic_data_ingestion_agent_complex_mapping-*
- 2.2: No bedrock-agentcore:* action wildcard is used on this role
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


def _extract_schema_mapping_role_section(content: str) -> str:
    """Extract the schemaMappingRole definition and all its addToPolicy calls."""
    pattern = r"(this\.schemaMappingRole\s*=\s*lambdaRole.*?)(?=this\.\w+Role\s*=\s*(?:lambdaRole|new\s+iam\.Role))"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return ""
    return match.group(1)


def _find_policy_statement_containing(section: str, action: str) -> str:
    """Find the full PolicyStatement block containing the given action string."""
    for match in re.finditer(r"new iam\.PolicyStatement\(\{", section):
        start = match.start()
        depth = 0
        i = match.end() - 1
        while i < len(section):
            if section[i] == "{":
                depth += 1
            elif section[i] == "}":
                depth -= 1
                if depth == 0:
                    end = section.index(")", i) + 1
                    block = section[start:end]
                    if action in block:
                        return block
                    break
            i += 1
    return ""


class TestSchemaMappingInvokeAgentRuntimePermission:
    """schemaMappingRole has bedrock-agentcore:InvokeAgentRuntime (Req 2.1)."""

    def test_invoke_agent_runtime_action_present(self, foundation_stack_content):
        """bedrock-agentcore:InvokeAgentRuntime should be granted to schemaMappingRole."""
        section = _extract_schema_mapping_role_section(foundation_stack_content)
        assert section, "schemaMappingRole section not found in foundation-stack.ts"
        assert "bedrock-agentcore:InvokeAgentRuntime" in section

    def test_invoke_agent_runtime_scoped_to_complex_mapping_runtime(self, foundation_stack_content):
        """Resource should be scoped to the complex mapping runtime name pattern."""
        section = _extract_schema_mapping_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "bedrock-agentcore:InvokeAgentRuntime")
        assert block, "PolicyStatement with bedrock-agentcore:InvokeAgentRuntime not found"
        assert "runtime/agentic_data_ingestion_agent_complex_mapping-*" in block

    def test_invoke_agent_runtime_uses_region_account_tokens(self, foundation_stack_content):
        """ARN should reference ${region} and ${account} tokens."""
        section = _extract_schema_mapping_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "bedrock-agentcore:InvokeAgentRuntime")
        assert block, "PolicyStatement with bedrock-agentcore:InvokeAgentRuntime not found"
        assert "${region}" in block
        assert "${account}" in block


class TestNoAgentCoreActionWildcard:
    """schemaMappingRole must NOT use bedrock-agentcore:* action wildcard (Req 2.2)."""

    def test_no_bedrock_agentcore_star_action(self, foundation_stack_content):
        """No bedrock-agentcore:* wildcard should appear in schemaMappingRole policies."""
        section = _extract_schema_mapping_role_section(foundation_stack_content)
        assert section, "schemaMappingRole section not found in foundation-stack.ts"
        # Check for any 'bedrock-agentcore:*' wildcard action (not scoped actions with * in resource)
        # This regex looks for the action wildcard pattern in action arrays
        wildcard_pattern = re.compile(r"'bedrock-agentcore:\*'")
        assert not wildcard_pattern.search(section), (
            "bedrock-agentcore:* action wildcard found in schemaMappingRole — "
            "only specific actions like InvokeAgentRuntime are allowed (Req 2.2)"
        )

    def test_invoke_is_only_agentcore_runtime_action(self, foundation_stack_content):
        """The only bedrock-agentcore runtime action should be InvokeAgentRuntime."""
        section = _extract_schema_mapping_role_section(foundation_stack_content)
        block = _find_policy_statement_containing(section, "bedrock-agentcore:InvokeAgentRuntime")
        assert block, "PolicyStatement with bedrock-agentcore:InvokeAgentRuntime not found"
        # The actions array in this statement should contain exactly InvokeAgentRuntime
        # (not a list of multiple runtime actions)
        actions_match = re.search(r"actions:\s*\[(.*?)\]", block, re.DOTALL)
        assert actions_match, "Could not parse actions array"
        actions_content = actions_match.group(1)
        # Should have only InvokeAgentRuntime
        assert "InvokeAgentRuntime" in actions_content
        # Count quoted action strings — should be exactly 1
        action_count = len(re.findall(r"'[^']+?'", actions_content))
        assert action_count == 1, (
            f"Expected exactly 1 action (InvokeAgentRuntime) but found {action_count} in statement"
        )
