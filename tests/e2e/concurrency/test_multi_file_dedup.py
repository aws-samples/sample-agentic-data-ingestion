# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E: concurrency / multi-file dedup mechanism against real AWS.

Conformance: Requirement 22 — brought under the shared E2E_Harness contracts.

The pipeline-trigger Lambda enforces single-flight execution per pipeline via
a conditional write to the pipeline-locks table (HASH key ``pipeline_prefix``)
and a global concurrency gate. This test verifies the deployed dedup substrate
is present and correctly shaped.

**Coverage scope (substrate only):**
- TC-3.8: lock-table shape + conditional-write rejection (single-flight)
- Does NOT trigger concurrent executions (TC-3.7) — see test_concurrency_behavior.py
- Does NOT perform a full same-file re-execution (TC-15.7)

**Harness conformance:**
- Gated by Real_AWS_Gate (session autouse via ``require_real_aws``)
- Lock-probe cleanup routed through ``e2e_ledger`` (Cleanup_Contract)
- No ``requires_trigger`` needed — this test does not start executions
- Reuses E2E_Harness fixtures (``boto3_session``, ``e2e_ledger``)

Run with: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true make test-e2e
"""

import time


def test_pipeline_locks_table_shape(boto3_session):
    """The locks table exists, is ACTIVE, and is keyed by pipeline_prefix."""
    ddb = boto3_session.client("dynamodb")
    desc = ddb.describe_table(TableName="agentic_data_ingestion_pipeline_locks")["Table"]
    assert desc["TableStatus"] == "ACTIVE"
    hash_keys = [k["AttributeName"] for k in desc["KeySchema"] if k["KeyType"] == "HASH"]
    assert hash_keys == ["pipeline_prefix"], f"Dedup lock table must be keyed by pipeline_prefix, got {hash_keys}"


def test_lock_conditional_write_is_single_flight(boto3_session, e2e_ledger):
    """A second lock acquire for the same prefix fails the conditional write —
    this is the exact mechanism the trigger Lambda relies on for dedup.

    Uses a throwaway prefix; cleanup is routed through the e2e_ledger
    (Cleanup_Contract) so it runs even on mid-test failure.
    """
    table = boto3_session.resource("dynamodb").Table("agentic_data_ingestion_pipeline_locks")
    prefix = f"e2e-dedup-probe-{int(time.time())}"
    cond = "attribute_not_exists(pipeline_prefix)"

    # Register cleanup via the ledger — runs in LIFO order regardless of pass/fail
    e2e_ledger.add_callable(table.delete_item, Key={"pipeline_prefix": prefix})

    # First acquire succeeds.
    table.put_item(
        Item={"pipeline_prefix": prefix, "execution_name": "first"},
        ConditionExpression=cond,
    )

    # Second acquire for the same prefix must fail.
    from botocore.exceptions import ClientError

    try:
        table.put_item(
            Item={"pipeline_prefix": prefix, "execution_name": "second"},
            ConditionExpression=cond,
        )
        raise AssertionError("Second lock acquire should have been rejected")
    except ClientError as e:
        assert e.response["Error"]["Code"] == "ConditionalCheckFailedException"
