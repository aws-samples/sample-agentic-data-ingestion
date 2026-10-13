# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for CDK data-stack.ts table name standardization.

Validates AC-14.2: CDK data-stack.ts creates tables with the correct
new names following the agentic_data_ingestion_{name} convention.

This is a file-content test — reads data-stack.ts and verifies table
name strings are present/absent as expected.
"""

import os
import re

import pytest

DATA_STACK_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "data-stack.ts")


@pytest.fixture(scope="module")
def data_stack_content():
    with open(DATA_STACK_PATH) as f:
        return f.read()


# ── Correct new table names present ─────────────────────────────────


class TestNewTableNamesPresent:
    """data-stack.ts contains all standardized table names."""

    def test_state_table(self, data_stack_content):
        assert "tableName: 'agentic_data_ingestion_state'" in data_stack_content

    def test_schema_mappings_table(self, data_stack_content):
        assert "tableName: 'agentic_data_ingestion_schema_mappings'" in data_stack_content

    def test_generated_code_table(self, data_stack_content):
        assert "tableName: 'agentic_data_ingestion_generated_code'" in data_stack_content

    def test_test_results_table(self, data_stack_content):
        assert "tableName: 'agentic_data_ingestion_test_results'" in data_stack_content

    def test_approvals_table(self, data_stack_content):
        assert "tableName: 'agentic_data_ingestion_approvals'" in data_stack_content

    def test_notifications_table(self, data_stack_content):
        assert "tableName: 'agentic_data_ingestion_notifications'" in data_stack_content

    def test_executions_table(self, data_stack_content):
        assert "tableName: 'agentic_data_ingestion_executions'" in data_stack_content

    def test_task_tokens_table(self, data_stack_content):
        assert "tableName: 'agentic_data_ingestion_task_tokens'" in data_stack_content


# ── Old table names absent ──────────────────────────────────────────


class TestOldTableNamesAbsent:
    """data-stack.ts does not contain old/inconsistent table names."""

    def test_no_legacy_prefix(self, data_stack_content):
        """Every tableName follows the agentic_data_ingestion_{name} convention.

        (Rewritten after the publication rename: the original asserted the
        legacy project-prefixed state-table name was absent, but the bulk
        rename rewrote that legacy literal into the NEW name, inverting the
        test into a contradiction with test_state_table above. The naming
        convention itself is the invariant AC-14.2 cares about, and checking
        it structurally avoids embedding legacy tokens the publication
        scanner forbids.)
        """
        names = re.findall(r"tableName:\s*'([^']+)'", data_stack_content)
        assert names, "no tableName declarations found in data-stack.ts"
        offenders = [n for n in names if not n.startswith("agentic_data_ingestion_")]
        assert offenders == [], f"table names outside the naming convention: {offenders}"

    def test_no_schema_mappings_env_suffix(self, data_stack_content):
        assert "agentic_data_ingestion_schema_mappings_${environment}" not in data_stack_content
        assert "agentic_data_ingestion_schema_mappings_dev" not in data_stack_content

    def test_no_generated_code_hyphen_env(self, data_stack_content):
        assert "adi-generated-code-${environment}" not in data_stack_content
        assert "adi-generated-code-dev" not in data_stack_content

    def test_no_test_results_hyphen_env(self, data_stack_content):
        assert "agentic-data-ingestion-test-results-${environment}" not in data_stack_content
        assert "agentic-data-ingestion-test-results-dev" not in data_stack_content

    def test_no_pipeline_approvals(self, data_stack_content):
        assert "agentic_data_ingestion_pipeline_approvals" not in data_stack_content

    def test_no_notifications_env_suffix(self, data_stack_content):
        assert "agentic_data_ingestion_notifications_${environment}" not in data_stack_content
        assert "agentic_data_ingestion_notifications_dev" not in data_stack_content


# ── Deleted table absent ────────────────────────────────────────────


class TestDeletedTableAbsent:
    """agentic_data_ingestion_schema_metadata table definition is removed."""

    def test_no_schema_metadata_table(self, data_stack_content):
        assert "agentic_data_ingestion_schema_metadata" not in data_stack_content
