# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E test: CSV → RDS_DATABASE (Aurora Postgres via TestFixtures) full pipeline run.

This test exercises the JDBC target happy path:
1. Checks if the TestFixtures stack is deployed (skips gracefully if not)
2. Discovers the RDS cluster ARN and secret ARN from CloudFormation outputs
3. Provisions a throwaway RDS_DATABASE pipeline with appropriate target_config
4. Uploads a uniquely-keyed CSV source file
5. Locates the triggered execution and releases approval gates
6. Waits for SUCCEEDED within the extended JDBC budget (900s)
7. Verifies rows via the RDS Data API target verifier
8. Cleans up via the ledger

Requirements: 10.1, 10.2, 10.3, 10.4, 10.5
"""

from __future__ import annotations

import logging
import time
import uuid

import pytest
from tests.e2e._harness.provisioner import ProvisionedPipeline
from tests.e2e.conftest import find_execution_by_input, poll_execution_by_input

logger = logging.getLogger(__name__)

# ── CSV test data — 4 rows with unique IDs for verification ───────────

CSV_TEST_DATA = """\
id,name,value
jdbc-001,Alice,100
jdbc-002,Bob,200
jdbc-003,Charlie,300
jdbc-004,Diana,400
"""

EXPECTED_ROWS = [
    {"id": "jdbc-001", "name": "Alice", "value": "100"},
    {"id": "jdbc-002", "name": "Bob", "value": "200"},
    {"id": "jdbc-003", "name": "Charlie", "value": "300"},
    {"id": "jdbc-004", "name": "Diana", "value": "400"},
]

# ── TestFixtures discovery constants ──────────────────────────────────
#
# The P18 TestFixtures stack exports the Postgres TARGET via a pre-created
# Glue Connection (glue_connection mode) rather than a raw cluster ARN. The
# connection carries the VPC PhysicalConnectionRequirements, so the pipeline
# routes into the VPC through it (P17 consolidated the target type to ``JDBC``).
#
# The target CREDENTIAL is the stack's exported target secret, passed as
# target_config.secret_arn. This test used to keep that secret OUT of the
# pipeline so the write ran in "inline mode", with the connection's inline
# USERNAME/PASSWORD copied into Glue job arguments. That mode is deleted
# (standing invariant LGR-819980f-1: plaintext credentials never reach Glue job
# arguments): build_jdbc_arguments now passes only a Secrets Manager reference,
# glue_job_jdbc.py.j2 fetches it at run time through the shared
# _write_jdbc.py.j2 partial, and with no reference — the fixture connection has
# no SECRET_ID — job creation fails with "carries no SECRET_ID".
#
# Verification reads back over the RDS Data API (HTTP, no VPC) using the
# cluster ARN (derived from the endpoint host) + the same target secret.

TEST_FIXTURES_STACK_PREFIX = "AgenticDataIngestion-TestFixtures"
GLUE_CONNECTION_OUTPUT_KEY = "PostgresGlueConnectionName"
SECRET_OUTPUT_KEY = "PostgresTargetSecretArn"
ENDPOINT_OUTPUT_KEY = "PostgresTargetEndpoint"
DATABASE_NAME = "testdb"
TARGET_SCHEMA = "public"
JDBC_DRIVER = "org.postgresql.Driver"
DB_PORT = 5432


def _derive_cluster_arn(endpoint_host: str, account_id: str, region: str) -> str:
    """Derive the Aurora cluster ARN from its writer endpoint host.

    The TestFixtures stack does not export a cluster ARN, but the RDS Data
    API verifier needs one. An Aurora endpoint is
    ``<cluster-id>.cluster-<hash>.<region>.rds.amazonaws.com`` so the cluster
    identifier is the label before ``.cluster-``.
    """
    cluster_id = endpoint_host.split(".cluster-", 1)[0]
    return f"arn:aws:rds:{region}:{account_id}:cluster:{cluster_id}"


def _discover_test_fixtures(boto3_session):
    """Discover the P18 TestFixtures Postgres target (glue_connection mode).

    Returns:
        dict with ``glue_connection_name``, ``secret_arn``, ``endpoint`` (host),
        ``jdbc_url``, ``cluster_arn``, and ``database`` — or None if the
        TestFixtures stack (or its required outputs) is not deployed.
    """
    cfn = boto3_session.client("cloudformation")

    # Find the TestFixtures stack by prefix
    paginator = cfn.get_paginator("list_stacks")
    stack_name = None
    for page in paginator.paginate(StackStatusFilter=["CREATE_COMPLETE", "UPDATE_COMPLETE"]):
        for stack in page.get("StackSummaries", []):
            if stack["StackName"].startswith(TEST_FIXTURES_STACK_PREFIX):
                stack_name = stack["StackName"]
                break
        if stack_name:
            break

    if not stack_name:
        return None

    # Read outputs from the stack
    resp = cfn.describe_stacks(StackName=stack_name)
    stacks = resp.get("Stacks", [])
    if not stacks:
        return None

    outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}

    glue_connection_name = outputs.get(GLUE_CONNECTION_OUTPUT_KEY)
    secret_arn = outputs.get(SECRET_OUTPUT_KEY)
    endpoint = outputs.get(ENDPOINT_OUTPUT_KEY)

    if not glue_connection_name or not secret_arn or not endpoint:
        logger.warning(
            "TestFixtures stack %s found but missing required outputs: "
            "glue_connection_name=%s, secret_arn=%s, endpoint=%s",
            stack_name,
            glue_connection_name,
            secret_arn,
            endpoint,
        )
        return None

    # Derive the cluster ARN (for the RDS Data API verifier) + account/region.
    sts = boto3_session.client("sts")
    account_id = sts.get_caller_identity()["Account"]
    region = boto3_session.region_name or "us-west-2"
    cluster_arn = _derive_cluster_arn(endpoint, account_id, region)

    jdbc_url = f"jdbc:postgresql://{endpoint}:{DB_PORT}/{DATABASE_NAME}"

    logger.info(
        "Discovered TestFixtures: stack=%s, glue_connection=%s, secret=%s, endpoint=%s, cluster=%s",
        stack_name,
        glue_connection_name,
        secret_arn,
        endpoint,
        cluster_arn,
    )

    return {
        "glue_connection_name": glue_connection_name,
        "secret_arn": secret_arn,
        "endpoint": endpoint,
        "jdbc_url": jdbc_url,
        "cluster_arn": cluster_arn,
        "database": DATABASE_NAME,
    }


@pytest.fixture
def test_fixtures_config(boto3_session):
    """Discover TestFixtures RDS config; skip if not deployed.

    The TestFixtures stack is only deployed when CDK is invoked with
    ``-c adi:deployTestFixtures=true``. If the stack is absent,
    the test is gracefully skipped rather than failed.
    """
    config = _discover_test_fixtures(boto3_session)
    if config is None:
        pytest.skip("TestFixtures stack not deployed. Deploy with: cdk deploy --all -c adi:deployTestFixtures=true")
    return config


@pytest.mark.e2e
@pytest.mark.slow
@pytest.mark.cost_budget_override(900)
class TestJdbcTargetRun:
    """Full pipeline run: CSV source → RDS_DATABASE (Aurora Postgres) target."""

    def test_csv_to_rds_database_succeeds(
        self,
        requires_trigger,
        test_fixtures_config,
        e2e_ledger,
        provisioner,
        approval_driver,
        target_verifier,
        cost_budget,
        boto3_session,
        resource_names,
    ):
        """Provision → upload → approve gates → SUCCEEDED → verify rows via RDS Data API.

        Exercises the full JDBC target happy-path using the pre-created
        ``glue_connection`` Postgres fixture (VPC routing). The pipeline uses
        the consolidated ``JDBC`` target type (P17) and references the target
        secret by ARN; the Glue job fetches it at run time. Verification reads
        back over the RDS Data API.
        """
        glue_connection_name = test_fixtures_config["glue_connection_name"]
        secret_arn = test_fixtures_config["secret_arn"]
        cluster_arn = test_fixtures_config["cluster_arn"]
        jdbc_url = test_fixtures_config["jdbc_url"]
        database = test_fixtures_config["database"]

        # Unique table per run (Postgres ``public`` schema) to avoid collisions.
        table_name = f"e2e_jdbc_{uuid.uuid4().hex[:8]}"
        qualified_table = f"{TARGET_SCHEMA}.{table_name}"

        # ── Step 1: Provision a throwaway JDBC (glue_connection) pipeline ──
        # glue_connection_name supplies VPC routing; secret_arn supplies the
        # credential, by reference (LGR-819980f-1). It used to be omitted so the
        # build read username/password off the connection into plaintext job
        # arguments — the path the JDBC-target credential fix deleted.
        pipeline = provisioner.create(
            target_type="JDBC",
            target_config={
                "type": "JDBC",
                "glue_connection_name": glue_connection_name,
                "jdbc_url": jdbc_url,
                "jdbc_driver": JDBC_DRIVER,
                "jdbc_table": qualified_table,
                "database_name": database,
                "secret_arn": secret_arn,
            },
        )
        logger.info(
            "Provisioned JDBC pipeline: %s (connection=%s, table=%s)",
            pipeline.pipeline_id,
            glue_connection_name,
            qualified_table,
        )

        # ── Step 2: Upload a uniquely-keyed CSV to the source prefix ──
        unique_key = f"e2e-jdbc-{uuid.uuid4().hex[:12]}.csv"
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

        # Record the upload timestamp for execution polling
        started_after = time.time() - 5  # small lookback buffer

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

        # ── Step 4: Release approval gates ────────────────────────────
        if status == "RUNNING":
            # Release schema-mapping gate (required with default config)
            logger.info("Releasing schema_mapping gate...")
            approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="schema_mapping",
                decision="approve",
                timeout=cost_budget,
            )

            # Release code-generation gate (optional — may not trigger)
            logger.info("Releasing code_generation gate (optional)...")
            approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="code_generation",
                decision="approve",
                timeout=min(cost_budget, 120),  # short wait for optional gate
                optional=True,
            )

            # ── Step 5: Wait for SUCCEEDED ────────────────────────────
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
            f"JDBC pipeline execution did not succeed: status={status}, execution_arn={execution_arn}"
        )

        # ── Step 6: Verify target rows via RDS Data API ───────────────
        # Build a synthetic RDS_DATABASE target so the harness verifier reads
        # back over the RDS Data API. It carries cluster_arn, which the real
        # pipeline's target_config does not, and the same secret_arn the
        # pipeline writes with.
        logger.info(
            "Verifying target data for pipeline %s (table=%s) via RDS Data API",
            pipeline.pipeline_id,
            qualified_table,
        )
        verify_target = ProvisionedPipeline(
            pipeline_id=pipeline.pipeline_id,
            target_type="RDS_DATABASE",
            target_config={
                "cluster_arn": cluster_arn,
                "secret_arn": secret_arn,
                "database_name": database,
                "jdbc_table": qualified_table,
            },
        )
        result = target_verifier.verify(
            verify_target,
            expected_rows=EXPECTED_ROWS,
            key_field="id",
        )

        assert result.success, f"JDBC target verification failed (method={result.method}): {result.message}"
        logger.info(
            "JDBC target verification passed: method=%s, rows=%d",
            result.method,
            result.row_count,
        )

        # ── Step 7: Clean up the RDS table (best-effort) ─────────────
        # Register table cleanup with the ledger so it runs even on failure
        def _cleanup_rds_table():
            try:
                rds_data = boto3_session.client("rds-data")
                rds_data.execute_statement(
                    resourceArn=cluster_arn,
                    secretArn=secret_arn,
                    database=database,
                    sql=f"DROP TABLE IF EXISTS {qualified_table}",
                )
                logger.info("Cleaned up RDS table: %s", qualified_table)
            except Exception as exc:
                logger.warning("Failed to clean up RDS table %s: %s", qualified_table, exc)

        e2e_ledger.add_callable(_cleanup_rds_table)
