# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E test: CSV → Glue Catalog (GLUE_CATALOG) full pipeline run.

Provisions a GLUE_CATALOG pipeline with an auto-generated database/table,
runs to SUCCEEDED, verifies rows via the Target_Verifier (Athena with
capability-detection fallback), then tears down the provisioned pipeline.

Requirements: 20.1, 20.2, 20.3, 20.4
"""

from __future__ import annotations

import logging
import time
import uuid

import pytest
from tests.e2e.conftest import find_execution_by_input, poll_execution_by_input

logger = logging.getLogger(__name__)

# ── CSV test data — 4 rows with unique IDs for verification ───────────

CSV_TEST_DATA = """\
id,name,value
e2e-gc-001,Alpha,10
e2e-gc-002,Beta,20
e2e-gc-003,Gamma,30
e2e-gc-004,Delta,40
"""

EXPECTED_ROWS = [
    {"id": "e2e-gc-001", "name": "Alpha", "value": "10"},
    {"id": "e2e-gc-002", "name": "Beta", "value": "20"},
    {"id": "e2e-gc-003", "name": "Gamma", "value": "30"},
    {"id": "e2e-gc-004", "name": "Delta", "value": "40"},
]


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.skip(
    reason=(
        "GLUE_CATALOG is no longer offered for new pipelines: POST /api/pipelines "
        "refuses it with a 400, and this test provisions through that endpoint. "
        "Existing Glue Catalog pipelines still run; delete this test when the "
        "target type's runtime is removed."
    )
)
class TestGlueCatalogTarget:
    """Full pipeline run: CSV source → GLUE_CATALOG target."""

    def test_glue_catalog_run_succeeds(
        self,
        requires_trigger,
        e2e_ledger,
        provisioner,
        approval_driver,
        target_verifier,
        cost_budget,
        boto3_session,
        resource_names,
    ):
        """Provision → upload → approve gates → SUCCEEDED → verify rows → teardown.

        This test exercises a pipeline targeting the Glue Data Catalog,
        which is a target type not currently deployed in the shared dev
        environment.
        """
        # ── Step 1: Create a real, writable Glue Catalog table ────────
        # GLUE_CATALOG targets are "map to an existing catalog table" — the
        # app does NOT create the table (create_infrastructure explicitly
        # skips it: "Using existing Glue Catalog table"), and the
        # schema_mapping step resolves the target schema by calling
        # glue:GetTable on a 'database.table' reference. So the table must
        # pre-exist here. Two real constraints from the pipeline-specific
        # IAM role (create_glue_pipeline/iam_roles.py) shape the setup:
        #   1. The role grants Glue actions only on database/agentic_data_ingestion_* and
        #      table/agentic_data_ingestion_*/* — so the database name MUST start with
        #      'agentic_data_ingestion_' or the Glue job's GetTable/UpdateTable is denied.
        #   2. The role grants S3 write on the system buckets (incl.
        #      adi-processed-files-*) — so the table's S3 Location must
        #      live in a role-writable bucket for the from_catalog write to
        #      succeed. (The role's default agentic-data-ingestion-table-{pipeline_id}
        #      bucket is never created for GLUE_CATALOG.)
        unique_suffix = uuid.uuid4().hex[:8]
        database_name = f"agentic_data_ingestion_e2e_gc_{unique_suffix}"
        table_name = f"target_{unique_suffix}"

        sts = boto3_session.client("sts")
        account_id = sts.get_caller_identity()["Account"]
        region = boto3_session.region_name
        target_bucket = f"adi-processed-files-{account_id}-{region}"
        table_location = f"s3://{target_bucket}/glue-catalog-targets/{database_name}/{table_name}/"

        glue = boto3_session.client("glue")
        glue.create_database(DatabaseInput={"Name": database_name})
        glue.create_table(
            DatabaseName=database_name,
            TableInput={
                "Name": table_name,
                "TableType": "EXTERNAL_TABLE",
                "Parameters": {"classification": "parquet", "EXTERNAL": "TRUE"},
                "StorageDescriptor": {
                    # Columns match the source CSV (id, name, value). The CSV
                    # is read with inferSchema=False, so all source columns are
                    # strings — keep the target columns string to avoid casts.
                    "Columns": [
                        {"Name": "id", "Type": "string"},
                        {"Name": "name", "Type": "string"},
                        {"Name": "value", "Type": "string"},
                    ],
                    "Location": table_location,
                    "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    "SerdeInfo": {
                        "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                        "Parameters": {"serialization.format": "1"},
                    },
                },
            },
        )
        logger.info(
            "Created Glue Catalog table %s.%s (location=%s)",
            database_name,
            table_name,
            table_location,
        )

        # Register Glue database/table + target data prefix for cleanup
        def _cleanup_glue_catalog():
            glue_c = boto3_session.client("glue")
            try:
                glue_c.delete_table(DatabaseName=database_name, Name=table_name)
                logger.info("Deleted Glue table %s.%s", database_name, table_name)
            except Exception as exc:
                logger.debug("Glue table delete (may not exist): %s", exc)
            try:
                glue_c.delete_database(Name=database_name)
                logger.info("Deleted Glue database %s", database_name)
            except Exception as exc:
                logger.debug("Glue database delete (may not exist): %s", exc)
            # Best-effort delete of any Parquet data the job wrote
            try:
                s3_c = boto3_session.client("s3")
                prefix = f"glue-catalog-targets/{database_name}/{table_name}/"
                listed = s3_c.list_objects_v2(Bucket=target_bucket, Prefix=prefix)
                objs = [{"Key": o["Key"]} for o in listed.get("Contents", [])]
                if objs:
                    s3_c.delete_objects(Bucket=target_bucket, Delete={"Objects": objs})
                    logger.info("Deleted %d target data objects under %s", len(objs), prefix)
            except Exception as exc:
                logger.debug("Target data cleanup (best-effort): %s", exc)

        e2e_ledger.add_callable(_cleanup_glue_catalog)

        # ── Provision the pipeline against the pre-created catalog table ─
        # target_schema MUST be the 'database.table' reference the
        # schema_mapping step resolves via glue:GetTable. target_config
        # carries both key spellings (database/table and glue_database/
        # glue_table) plus format, satisfying both build_glue_catalog_arguments
        # and the artifacts.py target-config validation (which hard-fails the
        # create_glue_pipeline step when glue_database/glue_table/format are
        # missing).
        target_config = {
            "type": "GLUE_CATALOG",
            "database": database_name,
            "table": table_name,
            "glue_database": database_name,
            "glue_table": table_name,
            "format": "parquet",
        }

        pipeline = provisioner.create(
            target_type="GLUE_CATALOG",
            target_config=target_config,
            target_schema=f"{database_name}.{table_name}",
        )
        logger.info(
            "Provisioned GLUE_CATALOG pipeline: %s (db=%s, table=%s)",
            pipeline.pipeline_id,
            database_name,
            table_name,
        )

        # ── Step 2: Upload a uniquely-keyed CSV to the source prefix ──
        unique_key = f"e2e-{uuid.uuid4().hex[:12]}.csv"
        source_bucket = resource_names["source_bucket"]
        s3_key = f"{pipeline.pipeline_id}/{unique_key}"

        s3_client = boto3_session.client("s3")
        s3_client.put_object(
            Bucket=source_bucket,
            Key=s3_key,
            Body=CSV_TEST_DATA.encode("utf-8"),
            ContentType="text/csv",
        )
        logger.info("Uploaded source file: s3://%s/%s", source_bucket, s3_key)

        # Register the S3 object for cleanup
        e2e_ledger.add_s3_object(source_bucket, s3_key)

        # Record upload timestamp for execution polling
        started_after = time.time() - 5

        # ── Step 3: Find the triggered execution (returns immediately) ─
        sfn_client = boto3_session.client("stepfunctions")
        state_machine_arn = resource_names["state_machine_arn"]

        logger.info(
            "Finding execution: pipeline_id=%s, state_machine=%s",
            pipeline.pipeline_id,
            state_machine_arn,
        )

        status, execution_arn = find_execution_by_input(
            sfn_client,
            state_machine_arn,
            pipeline_id=pipeline.pipeline_id,
            started_after=started_after,
            timeout=120,
        )

        logger.info("Found execution: arn=%s, status=%s", execution_arn, status)

        # Register the execution for cleanup (stop if still RUNNING)
        e2e_ledger.add_execution(execution_arn)

        # ── Step 4: Release approval gates (while execution is RUNNING) ─
        if status == "RUNNING":
            logger.info("Releasing schema_mapping gate...")
            approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="schema_mapping",
                decision="approve",
                timeout=cost_budget,
            )

            logger.info("Releasing code_generation gate (optional)...")
            approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="code_generation",
                decision="approve",
                timeout=min(cost_budget, 120),
                optional=True,
            )

        # ── Step 5: Poll for terminal state after gates released ──────
        logger.info("Polling for terminal state after gate release...")
        status, execution_arn = poll_execution_by_input(
            sfn_client,
            state_machine_arn,
            pipeline_id=pipeline.pipeline_id,
            started_after=started_after,
            timeout=cost_budget,
        )
        logger.info("Final execution status: %s", status)

        # Assert the pipeline succeeded
        assert status == "SUCCEEDED", (
            f"GLUE_CATALOG pipeline execution did not succeed: status={status}, execution_arn={execution_arn}"
        )

        # ── Step 6: Verify target rows ────────────────────────────────
        logger.info("Verifying Glue Catalog target data for pipeline %s", pipeline.pipeline_id)
        result = target_verifier.verify(
            pipeline,
            expected_rows=EXPECTED_ROWS,
            key_field="id",
        )

        assert result.success, f"GLUE_CATALOG target verification failed (method={result.method}): {result.message}"
        logger.info(
            "Target verification passed: method=%s, rows=%d",
            result.method,
            result.row_count,
        )
