# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK assertion tests for the P28 R17 read-path GSIs (Task 13.2).

Validates that data-stack.ts defines every GSI the converted dashboard read
paths query (the index names are pinned literally on both sides — the
handler unit tests in tests/unit/lambdas/dashboard_api/test_query_not_scan.py
assert the Lambdas query these exact names):

- state table:          step_name-timestamp-index (step_name, timestamp)
- approvals table:      status-created-index (status, created_at)
                        run_id-index (run_id, PK-only)
- generated_code table: pipeline_id-index (pipeline_id, generation_timestamp)
                        run_id-index (run_id, generation_timestamp)
- executions table:     run_id-index (run_id, PK-only)
                        StatusIndex (status, started_at) — pre-existing, but
                        the metrics cost fallback now depends on it

The executions table carries a THIRD GSI that is out of this file's scope:
RunsByTimeIndex (pipeline_id, started_at), added by p48 R5.2 to give run
history a chronological sort key. It is asserted at the synthesized template
in deploy-cdk/test/p48-runs-by-time-index.test.ts, which is the stronger
check — this file parses source text. Noted here only so the inventory above
does not read as exhaustive.

Validates: Requirements 17.1, 17.2, 17.4
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


def _extract_gsi_block(content: str, table_keyword: str, index_name: str) -> str:
    """Extract the addGlobalSecondaryIndex({...}) block for one index in one table section."""
    section = _extract_section(content, table_keyword)
    assert section, f"{table_keyword} section not found in data-stack.ts"
    for chunk in section.split("addGlobalSecondaryIndex")[1:]:
        block = chunk.split("});")[0]
        if f"indexName: '{index_name}'" in block:
            return block
    return ""


def _assert_key(block: str, role: str, attr_name: str) -> None:
    """Assert the GSI block declares partitionKey/sortKey with the given STRING attribute."""
    assert re.search(
        rf"{role}:\s*\{{\s*name:\s*'{re.escape(attr_name)}',\s*type:\s*dynamodb\.AttributeType\.STRING\s*\}}",
        block,
    ), f"expected {role} {attr_name} (STRING) in GSI block: {block!r}"


# ── State table: step_name-timestamp-index ──────────────────────────


class TestStateStepTimestampIndex:
    """Metrics stage/lines paths and glue_jobs schema lookups query this index."""

    def test_index_defined(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "State Table", "step_name-timestamp-index")
        assert block, "step_name-timestamp-index not defined on the state table"

    def test_key_schema(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "State Table", "step_name-timestamp-index")
        _assert_key(block, "partitionKey", "step_name")
        _assert_key(block, "sortKey", "timestamp")

    def test_projection_all(self, data_stack_content):
        """Handlers read step payloads (output_data, started_at, …) from the index."""
        block = _extract_gsi_block(data_stack_content, "State Table", "step_name-timestamp-index")
        assert "ProjectionType.ALL" in block


# ── Approvals table: status-created-index + run_id-index ───────────


class TestApprovalsStatusCreatedIndex:
    """Dashboard status-filtered/unfiltered approval listings query this index."""

    def test_index_defined(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Approvals Table", "status-created-index")
        assert block, "status-created-index not defined on the approvals table"

    def test_key_schema(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Approvals Table", "status-created-index")
        _assert_key(block, "partitionKey", "status")
        _assert_key(block, "sortKey", "created_at")


class TestApprovalsRunIdIndex:
    """Per-run approval lookups in pipelines_runs query this index (was an N+1 scan)."""

    def test_index_defined(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Approvals Table", "run_id-index")
        assert block, "run_id-index not defined on the approvals table"

    def test_partition_key_run_id(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Approvals Table", "run_id-index")
        _assert_key(block, "partitionKey", "run_id")

    def test_no_sort_key(self, data_stack_content):
        """PK-only by design: approval rows lacking created_at must stay visible
        to the run-details listing."""
        block = _extract_gsi_block(data_stack_content, "Approvals Table", "run_id-index")
        assert "sortKey" not in block


# ── Generated-code table: pipeline_id-index + run_id-index ─────────


class TestGeneratedCodePipelineIdIndex:
    """glue_jobs script-version lookups query this index."""

    def test_index_defined(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Generated Code Table", "pipeline_id-index")
        assert block, "pipeline_id-index not defined on the generated-code table"

    def test_key_schema(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Generated Code Table", "pipeline_id-index")
        _assert_key(block, "partitionKey", "pipeline_id")
        _assert_key(block, "sortKey", "generation_timestamp")


class TestGeneratedCodeRunIdIndex:
    def test_index_defined(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Generated Code Table", "run_id-index")
        assert block, "run_id-index not defined on the generated-code table"

    def test_key_schema(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Generated Code Table", "run_id-index")
        _assert_key(block, "partitionKey", "run_id")
        _assert_key(block, "sortKey", "generation_timestamp")


# ── Executions table: run_id-index (+ StatusIndex dependency) ──────


class TestExecutionsRunIdIndex:
    """cancel_run's recovery path queries this index (run_id is the table's sort key)."""

    def test_index_defined(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Executions Summary Table", "run_id-index")
        assert block, "run_id-index not defined on the executions table"

    def test_partition_key_run_id(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Executions Summary Table", "run_id-index")
        _assert_key(block, "partitionKey", "run_id")

    def test_no_sort_key(self, data_stack_content):
        """PK-only: the lookup needs just the table keys; a run maps to one execution row."""
        block = _extract_gsi_block(data_stack_content, "Executions Summary Table", "run_id-index")
        assert "sortKey" not in block


class TestExecutionsStatusIndex:
    """The metrics cost fallback enumerates execution statuses against this index."""

    def test_index_defined(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Executions Summary Table", "StatusIndex")
        assert block, "StatusIndex not defined on the executions table"

    def test_key_schema(self, data_stack_content):
        block = _extract_gsi_block(data_stack_content, "Executions Summary Table", "StatusIndex")
        _assert_key(block, "partitionKey", "status")
        _assert_key(block, "sortKey", "started_at")
