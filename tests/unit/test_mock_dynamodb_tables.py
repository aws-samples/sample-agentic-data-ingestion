# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the mock_dynamodb_tables fixture.

Validates that all 14 DynamoDB tables are created with correct schemas
and GSIs matching the CDK-defined definitions. The count is pinned on
purpose: adding a table to the fixture is a deliberate act (it changes what
every moto-backed test can reach), so it has to be matched here.
"""

import pytest
from tests.fixtures.conftest import TABLE_DEFINITIONS


class TestMockDynamoDBTables:
    """Verify the mock_dynamodb_tables fixture creates all tables correctly."""

    def test_returns_all_14_tables(self, mock_dynamodb_tables):
        """Fixture should return exactly 14 table resources.

        13 until 2026-10-11, when agentic_data_ingestion_pipeline_locks joined:
        the fixture's env had named it (LOCKS_TABLE) all along without creating
        it, and tests/integration/pipeline_flow/test_step_failure_chain.py built
        its own copy to compensate. (The concurrency modules under @mock_aws
        build theirs in a separate moto context and are unaffected.)
        """
        assert len(mock_dynamodb_tables) == 14

    def test_all_table_names_present(self, mock_dynamodb_tables):
        """Every CDK-defined table name should be in the returned dict."""
        expected_names = {td["TableName"] for td in TABLE_DEFINITIONS}
        assert set(mock_dynamodb_tables.keys()) == expected_names

    def test_tables_are_active(self, mock_dynamodb_tables):
        """Each table should be in ACTIVE status."""
        for name, table in mock_dynamodb_tables.items():
            table.reload()
            assert table.table_status == "ACTIVE", f"{name} is not ACTIVE"

    @pytest.mark.parametrize(
        "table_name,expected_pk,expected_sk",
        [
            ("agentic_data_ingestion_state", "run_id", "step_name"),
            ("agentic_data_ingestion_pipelines", "pipeline_id", None),
            ("agentic_data_ingestion_schema_mappings", "mapping_id", None),
            ("agentic_data_ingestion_generated_code", "PK", "SK"),
            ("agentic_data_ingestion_test_results", "PK", "SK"),
            ("agentic_data_ingestion_approvals", "approval_id", None),
            ("agentic_data_ingestion_notifications", "notification_id", None),
            ("agentic_data_ingestion_executions", "pipeline_id", "run_id"),
            ("agentic_data_ingestion_task_tokens", "approval_id", None),
            ("agentic_data_ingestion_ws_connections", "connection_id", "pipeline_id"),
            ("agentic_data_ingestion_errors", "error_date", "timestamp"),
            ("agentic_data_ingestion_pipeline_locks", "pipeline_prefix", None),
        ],
    )
    def test_key_schema(self, mock_dynamodb_tables, table_name, expected_pk, expected_sk):
        """Each table should have the correct partition and sort key."""
        table = mock_dynamodb_tables[table_name]
        table.reload()
        key_schema = {ks["AttributeName"]: ks["KeyType"] for ks in table.key_schema}
        assert key_schema[expected_pk] == "HASH"
        if expected_sk:
            assert key_schema[expected_sk] == "RANGE"
        else:
            assert len(key_schema) == 1

    @pytest.mark.parametrize(
        "table_name,expected_gsi_count",
        [
            ("agentic_data_ingestion_state", 3),
            ("agentic_data_ingestion_pipelines", 2),
            ("agentic_data_ingestion_schema_mappings", 3),
            ("agentic_data_ingestion_generated_code", 2),
            ("agentic_data_ingestion_test_results", 1),
            ("agentic_data_ingestion_approvals", 4),
            ("agentic_data_ingestion_notifications", 1),
            ("agentic_data_ingestion_executions", 3),
            ("agentic_data_ingestion_task_tokens", 0),
            ("agentic_data_ingestion_ws_connections", 1),
            ("agentic_data_ingestion_errors", 0),
        ],
    )
    def test_gsi_count(self, mock_dynamodb_tables, table_name, expected_gsi_count):
        """Each table should have the correct number of GSIs."""
        table = mock_dynamodb_tables[table_name]
        table.reload()
        gsis = table.global_secondary_indexes or []
        assert len(gsis) == expected_gsi_count, f"{table_name}: expected {expected_gsi_count} GSIs, got {len(gsis)}"

    def test_state_table_gsi_names(self, mock_dynamodb_tables):
        """agentic_data_ingestion_state should have all 3 GSIs (incl. the P28 R17 step_name index)."""
        table = mock_dynamodb_tables["agentic_data_ingestion_state"]
        table.reload()
        gsi_names = {g["IndexName"] for g in table.global_secondary_indexes}
        assert gsi_names == {"PipelineIndex", "GlueJobRunIndex", "step_name-timestamp-index"}

    def test_approvals_table_gsi_names(self, mock_dynamodb_tables):
        """agentic_data_ingestion_approvals should have all 4 GSIs (incl. the P28 R17 run_id index)."""
        table = mock_dynamodb_tables["agentic_data_ingestion_approvals"]
        table.reload()
        gsi_names = {g["IndexName"] for g in table.global_secondary_indexes}
        assert gsi_names == {
            "pipeline-status-index",
            "status-created-index",
            "approval-type-created-index",
            "run_id-index",
        }

    def test_executions_table_gsi_names(self, mock_dynamodb_tables):
        """executions should have StatusIndex, the P28 R17 run_id index, and p48's RunsByTimeIndex.

        The expected sets here are deliberately hardcoded rather than derived from
        TABLE_DEFINITIONS: the fixture builds these tables *from* TABLE_DEFINITIONS,
        so a derived expectation would assert the fixture against itself. These
        literals are an independent restatement of what the CDK stacks define, and
        going stale (as this one did when p48 added RunsByTimeIndex) is the test
        doing its job.
        """
        table = mock_dynamodb_tables["agentic_data_ingestion_executions"]
        table.reload()
        gsi_names = {g["IndexName"] for g in table.global_secondary_indexes}
        assert gsi_names == {"StatusIndex", "run_id-index", "RunsByTimeIndex"}

    def test_schema_mappings_gsi_names(self, mock_dynamodb_tables):
        """agentic_data_ingestion_schema_mappings should have all 3 GSIs."""
        table = mock_dynamodb_tables["agentic_data_ingestion_schema_mappings"]
        table.reload()
        gsi_names = {g["IndexName"] for g in table.global_secondary_indexes}
        assert gsi_names == {
            "source-schema-index",
            "target-schema-index",
            "schema-pair-index",
        }

    def test_can_put_and_get_item(self, mock_dynamodb_tables):
        """Smoke test: write and read an item from the pipelines table."""
        table = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
        table.put_item(Item={"pipeline_id": "test-pipe-1", "status": "created"})
        resp = table.get_item(Key={"pipeline_id": "test-pipe-1"})
        assert resp["Item"]["status"] == "created"

    def test_tables_are_isolated_per_test(self, mock_dynamodb_tables):
        """Each test invocation should get fresh empty tables (moto mode)."""
        table = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
        resp = table.scan()
        assert resp["Count"] == 0, "Table should be empty at start of test"

    def test_all_gsi_projections_are_all(self, mock_dynamodb_tables):
        """Every GSI across all tables should use ALL projection."""
        for name, table in mock_dynamodb_tables.items():
            table.reload()
            for gsi in table.global_secondary_indexes or []:
                proj = gsi["Projection"]["ProjectionType"]
                assert proj == "ALL", f"{name}.{gsi['IndexName']} projection is {proj}, expected ALL"
