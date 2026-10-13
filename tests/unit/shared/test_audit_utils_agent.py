# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for write_agent_audit_entry() in audit_utils.

Validates that agent audit entries are written with the correct actor format,
include run_id/pipeline_id in details, and delegate to write_audit_entry().
"""

from __future__ import annotations

import os
import sys

import pytest

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))


@pytest.fixture(autouse=True)
def _reset_audit_cache(mock_dynamodb_tables, mock_lambda_env):
    """``audit_utils`` caches the boto3 resource and table at module scope, and
    creates the resource at import time with no explicit region. In the full
    suite the module is imported early (while the shell's AWS_REGION — e.g.
    us-west-2 — is active), so its cached resource targets the wrong region and
    PutItem 404s against the us-east-1 moto tables (the tests pass in isolation).
    Reset the cache and rebind the resource to us-east-1 — where
    ``mock_dynamodb_tables`` creates the tables — within the active moto
    context."""
    import boto3

    import audit_utils

    audit_utils._audit_table = None
    audit_utils._table_name = ""
    audit_utils._dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    yield


def test_write_agent_audit_entry_format(mock_dynamodb_tables, mock_lambda_env):
    """Agent audit entries use actor='agent:{agent_type}' and include run/pipeline IDs."""
    from audit_utils import write_agent_audit_entry

    write_agent_audit_entry(
        agent_type="schema_mapping",
        action="generate_mapping",
        resource_id="run-test-001",
        details={"mapping_count": 5, "model_used": "claude-sonnet"},
        run_id="run-test-001",
        pipeline_id="pipe-test-001",
    )

    # Verify the item was written to the audit log table
    table = mock_dynamodb_tables["agentic_data_ingestion_audit_log"]
    response = table.scan()
    items = response["Items"]

    assert len(items) == 1
    item = items[0]
    assert item["actor"] == "agent:schema_mapping"
    assert item["action"] == "generate_mapping"
    assert item["resource_type"] == "agent:schema_mapping"
    assert item["resource_id"] == "run-test-001"
    assert item["details"]["run_id"] == "run-test-001"
    assert item["details"]["pipeline_id"] == "pipe-test-001"
    assert item["details"]["mapping_count"] == 5


def test_write_agent_audit_entry_code_generation(mock_dynamodb_tables, mock_lambda_env):
    """Code generation agent audit entry includes template and line count."""
    from audit_utils import write_agent_audit_entry

    write_agent_audit_entry(
        agent_type="code_generation",
        action="generate_code",
        resource_id="code-v1-abc",
        details={"template_used": "glue_job_s3_tables.py.j2", "line_count": 85},
        run_id="run-test-002",
        pipeline_id="pipe-test-002",
    )

    table = mock_dynamodb_tables["agentic_data_ingestion_audit_log"]
    response = table.scan()
    items = response["Items"]

    assert len(items) == 1
    item = items[0]
    assert item["actor"] == "agent:code_generation"
    assert item["action"] == "generate_code"
    assert item["details"]["template_used"] == "glue_job_s3_tables.py.j2"
    assert item["details"]["line_count"] == 85
    assert item["details"]["run_id"] == "run-test-002"


def test_write_agent_audit_entry_empty_details(mock_dynamodb_tables, mock_lambda_env):
    """Agent audit entry works with no details provided."""
    from audit_utils import write_agent_audit_entry

    write_agent_audit_entry(
        agent_type="glue_pipeline",
        action="recommend_workers",
        resource_id="run-test-003",
        run_id="run-test-003",
        pipeline_id="pipe-test-003",
    )

    table = mock_dynamodb_tables["agentic_data_ingestion_audit_log"]
    response = table.scan()
    items = response["Items"]

    assert len(items) == 1
    item = items[0]
    assert item["actor"] == "agent:glue_pipeline"
    assert item["details"]["run_id"] == "run-test-003"
    assert item["details"]["pipeline_id"] == "pipe-test-003"


def test_write_agent_audit_entry_has_ttl(mock_dynamodb_tables, mock_lambda_env):
    """Agent audit entries include a TTL attribute for auto-expiry."""
    import time

    from audit_utils import write_agent_audit_entry

    write_agent_audit_entry(
        agent_type="code_testing",
        action="test_code",
        resource_id="code-v1-xyz",
        details={"test_status": "passed"},
        run_id="run-test-004",
        pipeline_id="pipe-test-004",
    )

    table = mock_dynamodb_tables["agentic_data_ingestion_audit_log"]
    response = table.scan()
    item = response["Items"][0]

    assert "ttl" in item
    # TTL should be roughly 1 year from now
    assert item["ttl"] > int(time.time()) + 300 * 86_400
