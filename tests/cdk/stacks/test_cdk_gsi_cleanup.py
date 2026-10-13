# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for CDK data-stack.ts GSI cleanup.

Validates:
- AC-GSI.1: Removed GSIs from state table (StatusIndex, TimestampIndex, GlueJobRunIdIndex)
- AC-GSI.2: Removed agentic_data_ingestion_schema_metadata table
- AC-GSI.4: Notifications table has only PriorityIndex GSI

Parses data-stack.ts section-by-section using the `// ──` comment markers
to verify each table has exactly the expected GSIs.
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


# ── State Table GSI Cleanup ─────────────────────────────────────────


class TestStateTableGSIs:
    """State table should have only PipelineIndex GSI."""

    def test_pipeline_index_present(self, data_stack_content):
        section = _extract_section(data_stack_content, "State Table")
        assert section, "State Table section not found in data-stack.ts"
        assert "'PipelineIndex'" in section

    def test_no_run_id_index(self, data_stack_content):
        section = _extract_section(data_stack_content, "State Table")
        assert "'RunIdIndex'" not in section

    def test_no_original_pipeline_index(self, data_stack_content):
        section = _extract_section(data_stack_content, "State Table")
        assert "'OriginalPipelineIndex'" not in section

    def test_no_status_index(self, data_stack_content):
        section = _extract_section(data_stack_content, "State Table")
        assert "'StatusIndex'" not in section

    def test_no_timestamp_index(self, data_stack_content):
        section = _extract_section(data_stack_content, "State Table")
        assert "'TimestampIndex'" not in section

    def test_no_glue_job_run_id_index(self, data_stack_content):
        section = _extract_section(data_stack_content, "State Table")
        assert "'GlueJobRunIdIndex'" not in section


# ── Notifications Table GSI Cleanup ─────────────────────────────────


class TestNotificationsTableGSIs:
    """Notifications table should have only PriorityIndex GSI."""

    def test_priority_index_present(self, data_stack_content):
        section = _extract_section(data_stack_content, "Notifications Table")
        assert section, "Notifications Table section not found in data-stack.ts"
        assert "'PriorityIndex'" in section

    def test_no_created_at_index(self, data_stack_content):
        section = _extract_section(data_stack_content, "Notifications Table")
        assert "'CreatedAtIndex'" not in section

    def test_no_dashboard_read_index(self, data_stack_content):
        section = _extract_section(data_stack_content, "Notifications Table")
        assert "'DashboardReadIndex'" not in section


# ── Schema Metadata Table Removed ───────────────────────────────────


class TestSchemaMetadataTableRemoved:
    """agentic_data_ingestion_schema_metadata table construct should not exist."""

    def test_no_schema_metadata_construct(self, data_stack_content):
        assert "SchemaMetadataTable" not in data_stack_content

    def test_no_schema_metadata_table_name(self, data_stack_content):
        assert "agentic_data_ingestion_schema_metadata" not in data_stack_content
