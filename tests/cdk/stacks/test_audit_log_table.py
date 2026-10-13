# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK assertion tests for the Audit Log Table in data-stack.ts.

Validates Requirement 5 (Audit Log Table):
- 5.1: Table with resource_id (PK) and timestamp (SK)
- 5.2: TTL attribute named 'ttl'
- 5.3: PAY_PER_REQUEST billing mode
- 5.4: ActionIndex GSI with action (PK) and timestamp (SK), ALL projection
- RETAIN removal policy
"""

import os
import re

import pytest

DATA_STACK_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "data-stack.ts")


@pytest.fixture(scope="module")
def data_stack_content():
    with open(DATA_STACK_PATH) as f:
        return f.read()


def _extract_section(content: str, header_keyword: str) -> str:
    """Extract a section between `// ── <header_keyword>` and the next `// ──` marker."""
    pattern = rf"(// ──[^\n]*{re.escape(header_keyword)}[^\n]*\n)(.*?)(?=// ──|\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return ""
    return match.group(1) + match.group(2)


class TestAuditLogTableDefinition:
    """Audit log table has correct PK, SK, billing, TTL, and removal policy."""

    def test_section_exists(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        assert section, "Audit Log Table section not found in data-stack.ts"

    def test_table_name(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        assert "tableName: 'agentic_data_ingestion_audit_log'" in section

    def test_partition_key(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        assert "name: 'resource_id'" in section
        assert re.search(r"partitionKey:\s*\{[^}]*name:\s*'resource_id'", section)

    def test_sort_key(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        assert re.search(r"sortKey:\s*\{[^}]*name:\s*'timestamp'", section)

    def test_billing_mode_pay_per_request(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        assert "BillingMode.PAY_PER_REQUEST" in section

    def test_ttl_attribute(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        assert "timeToLiveAttribute: 'ttl'" in section

    def test_retain_removal_policy(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        assert "RemovalPolicy.RETAIN" in section


class TestAuditLogTableGSI:
    """ActionIndex GSI has correct keys and projection."""

    def test_action_index_present(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        assert "'ActionIndex'" in section

    def test_action_index_partition_key(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        # Find the GSI block and verify action is the partition key
        gsi_match = re.search(
            r"addGlobalSecondaryIndex\(\{(.*?)\}\);",
            section,
            re.DOTALL,
        )
        assert gsi_match, "GSI definition not found"
        gsi_block = gsi_match.group(1)
        assert re.search(r"partitionKey:\s*\{[^}]*name:\s*'action'", gsi_block)

    def test_action_index_sort_key(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        gsi_match = re.search(
            r"addGlobalSecondaryIndex\(\{(.*?)\}\);",
            section,
            re.DOTALL,
        )
        assert gsi_match, "GSI definition not found"
        gsi_block = gsi_match.group(1)
        assert re.search(r"sortKey:\s*\{[^}]*name:\s*'timestamp'", gsi_block)

    def test_action_index_all_projection(self, data_stack_content):
        section = _extract_section(data_stack_content, "Audit Log Table")
        gsi_match = re.search(
            r"addGlobalSecondaryIndex\(\{(.*?)\}\);",
            section,
            re.DOTALL,
        )
        assert gsi_match, "GSI definition not found"
        gsi_block = gsi_match.group(1)
        assert "ProjectionType.ALL" in gsi_block


class TestAuditLogTableExport:
    """auditLogTable is exported as a public readonly property."""

    def test_public_readonly_property(self, data_stack_content):
        assert re.search(
            r"public\s+readonly\s+auditLogTable:\s*dynamodb\.Table",
            data_stack_content,
        )
