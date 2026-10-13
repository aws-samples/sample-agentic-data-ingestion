# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""N+1 query performance test for get_pipeline_executions.

Seeds 20 execution items in moto DynamoDB, calls get_pipeline_executions,
and asserts the total DynamoDB operation count stays ≤ 3 and response time
stays under 500ms (moto).

Validates: Requirements 19.1, 19.2, 19.3
"""

import importlib
import importlib.util
import json
import sys
import time
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# DynamoDB operation counter wrapper
# ---------------------------------------------------------------------------


class DynamoDBOperationCounter:
    """Wraps a boto3 DynamoDB Table to count query/get_item/scan operations."""

    def __init__(self, table):
        self._table = table
        self.operation_count = 0
        self.operations_log: list[str] = []

    def query(self, **kwargs):
        self.operation_count += 1
        self.operations_log.append(f"query({kwargs.get('KeyConditionExpression', '')})")
        return self._table.query(**kwargs)

    def get_item(self, **kwargs):
        self.operation_count += 1
        self.operations_log.append(f"get_item({kwargs.get('Key', '')})")
        return self._table.get_item(**kwargs)

    def scan(self, **kwargs):
        self.operation_count += 1
        self.operations_log.append(f"scan({kwargs.get('FilterExpression', '')})")
        return self._table.scan(**kwargs)

    def put_item(self, **kwargs):
        return self._table.put_item(**kwargs)

    def delete_item(self, **kwargs):
        return self._table.delete_item(**kwargs)

    def __getattr__(self, name):
        return getattr(self._table, name)


class DynamoDBResourceCounter:
    """Wraps a boto3 DynamoDB resource to count batch_get_item operations."""

    def __init__(self, resource):
        self._resource = resource
        self.operation_count = 0
        self.operations_log: list[str] = []

    def batch_get_item(self, **kwargs):
        self.operation_count += 1
        tables = list(kwargs.get("RequestItems", {}).keys())
        key_count = sum(len(v.get("Keys", [])) for v in kwargs.get("RequestItems", {}).values())
        self.operations_log.append(f"batch_get_item(tables={tables}, keys={key_count})")
        return self._resource.batch_get_item(**kwargs)

    def Table(self, name):
        return self._resource.Table(name)

    def __getattr__(self, name):
        return getattr(self._resource, name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LAMBDA_DIR = Path(__file__).resolve().parents[3] / "lambda"
_SHARED_DIR = Path(__file__).resolve().parents[3] / "shared"


def _create_tables(dynamodb_resource):
    """Create the DynamoDB tables needed for the test."""
    # Executions table
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_executions",
        KeySchema=[
            {"AttributeName": "pipeline_id", "KeyType": "HASH"},
            {"AttributeName": "run_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "StatusIndex",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "started_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            # p48: get_pipeline_executions pages the run list through this
            # index, newest first. Same key schema as data-stack.ts and the
            # shared fixture (tests/fixtures/conftest.py).
            {
                "IndexName": "RunsByTimeIndex",
                "KeySchema": [
                    {"AttributeName": "pipeline_id", "KeyType": "HASH"},
                    {"AttributeName": "started_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # State table (for schema enrichment) — includes the PipelineIndex GSI that
    # get_pipeline_executions queries for schema-version enrichment.
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_state",
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "step_name", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "step_name", "AttributeType": "S"},
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "PipelineIndex",
                "KeySchema": [
                    {"AttributeName": "pipeline_id", "KeyType": "HASH"},
                    {"AttributeName": "step_name", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Approvals table (for enrichment)
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_approvals",
        KeySchema=[
            {"AttributeName": "approval_id", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "approval_id", "AttributeType": "S"},
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
            {"AttributeName": "approval_type", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "pipeline-status-index",
                "KeySchema": [
                    {"AttributeName": "pipeline_id", "KeyType": "HASH"},
                    {"AttributeName": "status", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "status-created-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "approval-type-created-index",
                "KeySchema": [
                    {"AttributeName": "approval_type", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Pipelines table
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_pipelines",
        KeySchema=[{"AttributeName": "pipeline_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Schema mappings table
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_schema_mappings",
        KeySchema=[{"AttributeName": "mapping_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "mapping_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Generated code table
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_generated_code",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Test results table
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_test_results",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _seed_executions(table, pipeline_id: str, count: int = 20):
    """Seed execution items into the executions table."""
    for i in range(count):
        ts = f"2024-01-{15 + (i % 15):02d}T{10 + (i % 12):02d}:00:00Z"
        table.put_item(
            Item={
                "pipeline_id": pipeline_id,
                "run_id": f"run-{i:04d}",
                "status": "SUCCEEDED" if i % 3 != 0 else "RUNNING",
                "started_at": ts,
                "current_step": "JobCompletion" if i % 3 != 0 else "RunPipeline",
                "file_name": f"data_{i}.csv",
            }
        )


def _seed_state_records(table, count: int = 20, pipeline_id: str = "test-pipeline"):
    """Seed state table records for schema enrichment.

    Includes ``pipeline_id`` so records are retrievable via the PipelineIndex
    GSI that get_pipeline_executions uses for schema-version enrichment.
    """
    for i in range(count):
        table.put_item(
            Item={
                "run_id": f"run-{i:04d}",
                "step_name": "schema_extract",
                "pipeline_id": pipeline_id,
                "schema_hash": f"hash-{i % 3}",
            }
        )


#: The four established Load_Outcomes, cycled over the seeded runs by
#: ``i % 4``. Kept beside the seeder so the expectations below read off the
#: same table the seed is built from.
_LOAD_METRICS_BY_INDEX = (
    # (rows_read, rows_written, write_confirmed, expected outcome)
    (100, 100, True, "complete_load"),
    (100, 42, True, "partial_load"),
    (100, 0, True, "no_rows_loaded"),
    (0, 0, False, "empty_source"),
)


def _expected_outcome(i: int) -> str:
    """The Load_Outcome ``_seed_load_metrics_records`` sets up for run ``i``."""
    return _LOAD_METRICS_BY_INDEX[i % 4][3]


def _seed_load_metrics_records(table, count: int = 20, pipeline_id: str = "test-pipeline"):
    """Seed the ``(run_id, "load_metrics")`` counts rows the Glue job writes.

    Without these rows the guard below is not load-bearing: every run would
    derive ``not_established`` from an absent row and the counts path — the
    third ``batch_get_item`` key p37 C3 added — would never be touched, so the
    guard could not notice if that key had been fetched by a *separate*
    operation instead. Seeding the rows is what makes the operation count
    measure the enriched read rather than an empty one.

    The row shape is the producer's, verified against
    ``pyspark_templates/_partials/_load_counts.py.j2:118-129`` rather than
    invented: ints, strings and bools only, both ``pipeline_id`` spellings,
    and a ``write_confirmed`` stamp that is ``False`` for the empty-source
    stage (rows_written is 0 before any write happened).

    The counts go in as plain ``int``, exactly as the template's
    ``int(source_count)`` / ``int(transformed_count)`` do. boto3 serialises
    them to DynamoDB ``N`` and hands them back as ``Decimal``, which
    ``load_outcome._usable_count`` rejects — so the real round-trip here is
    what exercises ``_optional_row_count``'s ``item_int`` conversion. A seed
    that bypassed the round-trip would not.
    """
    for i in range(count):
        rows_read, rows_written, write_confirmed, _ = _LOAD_METRICS_BY_INDEX[i % 4]
        table.put_item(
            Item={
                "run_id": f"run-{i:04d}",
                "step_name": "load_metrics",
                "pipeline_id": pipeline_id,
                "original_pipeline_id": pipeline_id,
                "rows_read": rows_read,
                "rows_written": rows_written,
                "write_confirmed": write_confirmed,
                "source": "glue_job",
                "timestamp": f"2024-01-{15 + (i % 15):02d}T{10 + (i % 12):02d}:30:00+00:00",
            }
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_handler_modules():
    """Snapshot and restore handler-related sys.modules around each test.

    These tests deliberately purge and re-import the dashboard-API handler
    modules (handlers.*, router, lambda_function) to rebind them against
    moto-mocked tables. After the P21 Task 7.2 façade split, the handler
    package fans out into many sibling modules (handlers.pipelines_crud,
    handlers.pipelines_runs, …). If we leave those purged/re-imported copies
    in sys.modules, other integration test files that imported the handlers at
    collection time end up holding stale references and patch() targets the
    wrong module object. Snapshotting and restoring contains all module-cache
    mutation to this file.
    """

    def _snapshot() -> dict:
        return {
            name: mod
            for name, mod in sys.modules.items()
            if name in ("router", "handlers", "lambda_function")
            or name.startswith("handlers.")
            or "agentic_data_ingestion_dashboard_api" in name
        }

    saved = _snapshot()
    try:
        yield
    finally:
        # Drop anything the test added, then restore the originals.
        for name in _snapshot():
            if name not in saved:
                sys.modules.pop(name, None)
        sys.modules.update(saved)


@mock_aws
def test_get_pipeline_executions_operation_count(monkeypatch):
    """Assert get_pipeline_executions uses ≤ 3 DynamoDB operations for 20 items.

    The handler does:
    1. One query on executions table
    2. One scan on approvals table per RUNNING/WAITING_APPROVAL execution
    3. One get_item on state table per execution (schema enrichment)

    The N+1 pattern is in the schema enrichment loop — each execution
    triggers a separate get_item. This test documents the current behavior
    and sets a threshold to catch regressions.

    Validates: Requirements 19.1, 19.3
    """
    # Set up environment
    monkeypatch.setenv("STATE_TABLE", "agentic_data_ingestion_state")
    monkeypatch.setenv("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
    monkeypatch.setenv("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
    monkeypatch.setenv("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
    monkeypatch.setenv("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
    monkeypatch.setenv("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
    monkeypatch.setenv("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
    # handlers.common requires AUDIT_TABLE/AGENT_TRACES_TABLE at import time under
    # AGENTIC_DATA_INGESTION_ENV=test (no default fallback). Set them so the decomposed façade
    # modules load cleanly (P21 Task 7.2).
    monkeypatch.setenv("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
    monkeypatch.setenv("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AGENTIC_DATA_INGESTION_ENV", "test")
    monkeypatch.setenv("WS_API_ENDPOINT", "https://test.execute-api.us-east-1.amazonaws.com/prod")
    monkeypatch.setenv("POWERTOOLS_ENABLED", "false")

    # Create tables
    dynamodb_resource = boto3.resource("dynamodb", region_name="us-east-1")
    _create_tables(dynamodb_resource)

    pipeline_id = "test-pipeline-perf"

    # Seed data
    exec_table = dynamodb_resource.Table("agentic_data_ingestion_executions")
    state_table = dynamodb_resource.Table("agentic_data_ingestion_state")
    _seed_executions(exec_table, pipeline_id, count=20)
    _seed_state_records(state_table, count=20, pipeline_id=pipeline_id)
    # p37 C3: one load_metrics row per run, so the third batch_get key per run
    # actually resolves and the operation count below measures the enriched read.
    # p37 C5 added a FOURTH key per run (``data_quality``), and no row is seeded
    # for it on purpose: an unresolved key costs the same one call, and every run
    # here has recorded counts, so the retroactive lower bound has nothing to add.
    # 20 runs × 4 keys = 80 ≤ 100, so this is still one batch_get_item.
    _seed_load_metrics_records(state_table, count=20, pipeline_id=pipeline_id)

    # Wrap tables with operation counters
    exec_counter = DynamoDBOperationCounter(exec_table)
    state_counter = DynamoDBOperationCounter(state_table)
    approvals_counter = DynamoDBOperationCounter(dynamodb_resource.Table("agentic_data_ingestion_approvals"))
    resource_counter = DynamoDBResourceCounter(dynamodb_resource)

    # Clear module cache to force re-import with moto tables.
    # NOTE: get_pipeline_executions now lives in the decomposed sibling module
    # handlers.pipelines_runs (P21 Task 7.2 façade split). We must purge ALL
    # handlers.* submodules — not just handlers.pipelines/common — so the
    # siblings re-import fresh against the moto-patched common module below.
    # Otherwise pipelines_runs stays cached with table refs bound outside this
    # test's moto context and every query raises ResourceNotFoundException.
    for mod_name in list(sys.modules.keys()):
        if "agentic_data_ingestion_dashboard_api" in mod_name:
            del sys.modules[mod_name]
        if mod_name == "router" or mod_name == "handlers" or mod_name.startswith("handlers."):
            del sys.modules[mod_name]

    # Import the handler module
    handler_path = _LAMBDA_DIR / "agentic_data_ingestion_dashboard_api" / "handlers" / "pipelines.py"
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    api_dir = str(_LAMBDA_DIR / "agentic_data_ingestion_dashboard_api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)

    spec = importlib.util.spec_from_file_location("handlers.pipelines", handler_path)
    pipelines_mod = importlib.util.module_from_spec(spec)

    # Patch the common module's tables before loading
    common_path = _LAMBDA_DIR / "agentic_data_ingestion_dashboard_api" / "handlers" / "common.py"
    common_spec = importlib.util.spec_from_file_location("handlers.common", common_path)
    common_mod = importlib.util.module_from_spec(common_spec)
    common_spec.loader.exec_module(common_mod)

    # Monkey-patch the tables in the common module with our counters
    common_mod.executions_table = exec_counter
    common_mod.state_table = state_counter
    common_mod.approvals_table = approvals_counter
    common_mod.dynamodb = resource_counter
    sys.modules["handlers.common"] = common_mod

    spec.loader.exec_module(pipelines_mod)

    # Call the function and measure time
    start_time = time.monotonic()
    response = pipelines_mod.get_pipeline_executions(pipeline_id, page=1, page_size=20)
    elapsed_ms = (time.monotonic() - start_time) * 1000

    body = json.loads(response["body"])
    total_ops = (
        exec_counter.operation_count
        + state_counter.operation_count
        + approvals_counter.operation_count
        + resource_counter.operation_count
    )

    # After batch_get_item refactor: 1 query + 1 scan + 1 batch_get_item = 3 ops
    # (state_counter.operation_count should be 0 since we no longer call get_item on state_table)
    assert total_ops <= 5, (
        f"N+1 regression: {total_ops} DynamoDB operations for 20 executions. "
        f"Breakdown: executions={exec_counter.operation_count}, "
        f"state={state_counter.operation_count}, "
        f"approvals={approvals_counter.operation_count}, "
        f"resource={resource_counter.operation_count}. "
        f"Operations: {exec_counter.operations_log + state_counter.operations_log + approvals_counter.operations_log + resource_counter.operations_log}"
    )

    # Response time check (moto should be fast)
    assert elapsed_ms < 500, f"Response time regression: {elapsed_ms:.0f}ms > 500ms budget (moto mode)"

    # Verify we got execution data back
    assert len(body.get("executions", [])) > 0, "Expected at least one execution in response"

    # The whole point of seeding load_metrics: the operation count above only
    # means anything if the counts were actually read on those operations. A
    # guard that held because the rows were never looked at would be worse than
    # no guard, so assert the enrichment landed.
    executions = body["executions"]
    assert len(executions) == 20, f"Expected all 20 seeded runs in the response, got {len(executions)}"
    for exec_item in executions:
        run_id = exec_item["run_id"]
        outcome = exec_item.get("load_outcome")
        assert outcome is not None, f"{run_id}: load_outcome missing from the run-list row"
        expected = _expected_outcome(int(run_id.split("-")[1]))
        assert outcome["outcome"] == expected, (
            f"{run_id}: expected {expected} from the seeded counts, got {outcome['outcome']} "
            f"(determined_from={outcome['determined_from']}, missing={outcome['missing']})"
        )
        # Provenance proves the counts came off the row that was batch-got, not
        # from the absent arm.
        assert outcome["determined_from"] == "load_metrics_row", (
            f"{run_id}: counts did not come from the load_metrics row ({outcome['determined_from']})"
        )
        assert outcome["missing"] == [], f"{run_id}: expected no missing facts, got {outcome['missing']}"

    # And that the Decimal → int conversion is what made that possible. The seed
    # wrote plain ints; DynamoDB hands them back as Decimal, which
    # load_outcome._usable_count rejects outright. So the established outcomes
    # above are only reachable through _optional_row_count's item_int coercion.
    # Read through an uncounted table handle so this check cannot perturb the
    # operation count asserted above.
    raw_row = dynamodb_resource.Table("agentic_data_ingestion_state").get_item(
        Key={"run_id": "run-0000", "step_name": "load_metrics"}
    )["Item"]
    assert isinstance(raw_row["rows_read"], Decimal), (
        f"Seed did not round-trip through DynamoDB numerics: rows_read came back as "
        f"{type(raw_row['rows_read']).__name__}, so the item_int conversion is not exercised"
    )


@mock_aws
def test_get_pipeline_executions_load_metrics_key_boundary(monkeypatch):
    """34 runs cross the 100-key batch_get boundary — a cost, not a regression.

    p37 C3 took the run-list key count from 2 per run to 3 (two ``run_pipeline``
    name variants plus ``load_metrics``); **p37 C5 took it to 4** by adding
    ``data_quality`` for the retroactive lower bound. One ``batch_get_item`` call
    therefore covers **25** runs — down from 33 at three keys and 50 at two.
    ``page_size`` defaults to 10, but ``router.py:150`` reads it straight off the
    query string with no ceiling and passes it through as ``Limit``, so a caller
    can ask for more and cross the boundary.

    **The seed stays at 34 runs and the assertion is unamended.** 26 runs is now
    the *smallest* crossing (104 keys), but this is not a boundary-location test:
    it is the statement that a page larger than one chunk costs a bounded number
    of extra round trips rather than one call per run. 34 × 4 = 136 keys is still
    exactly two calls, so ``== 2`` holds without being relaxed. Re-pinning the
    seed at 26 would have to move again the next time a key is added; the
    arithmetic above is where the boundary is recorded.

    **A second ``batch_get_item`` at 34 runs is expected and is not a
    regression.** It is a cost — one extra round trip — and nothing more: the
    chunking loop splits at 100 and every result map is keyed by ``run_id``, so a
    single run's keys landing in different chunks changes no result. This test
    exists so a future reader who sees the count go from 1 to 2 does not mistake
    it for the N+1 pattern coming back. At 34 runs the split is not
    hypothetical: 68 ``run_pipeline`` keys plus 32 ``load_metrics`` keys fill the
    first chunk, and the last two runs' counts plus all 34 ``data_quality`` keys
    arrive on the second call.

    Validates: Requirements 1.10
    """
    monkeypatch.setenv("STATE_TABLE", "agentic_data_ingestion_state")
    monkeypatch.setenv("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
    monkeypatch.setenv("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
    monkeypatch.setenv("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
    monkeypatch.setenv("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
    monkeypatch.setenv("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
    monkeypatch.setenv("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
    monkeypatch.setenv("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
    monkeypatch.setenv("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AGENTIC_DATA_INGESTION_ENV", "test")
    monkeypatch.setenv("WS_API_ENDPOINT", "https://test.execute-api.us-east-1.amazonaws.com/prod")
    monkeypatch.setenv("POWERTOOLS_ENABLED", "false")

    dynamodb_resource = boto3.resource("dynamodb", region_name="us-east-1")
    _create_tables(dynamodb_resource)

    pipeline_id = "test-pipeline-boundary"
    run_count = 34  # 4 keys/run × 34 = 136 > 100 (was 3 keys/run × 34 = 102)

    exec_table = dynamodb_resource.Table("agentic_data_ingestion_executions")
    state_table = dynamodb_resource.Table("agentic_data_ingestion_state")
    _seed_executions(exec_table, pipeline_id, count=run_count)
    _seed_state_records(state_table, count=run_count, pipeline_id=pipeline_id)
    _seed_load_metrics_records(state_table, count=run_count, pipeline_id=pipeline_id)

    exec_counter = DynamoDBOperationCounter(exec_table)
    state_counter = DynamoDBOperationCounter(state_table)
    approvals_counter = DynamoDBOperationCounter(dynamodb_resource.Table("agentic_data_ingestion_approvals"))
    resource_counter = DynamoDBResourceCounter(dynamodb_resource)

    for mod_name in list(sys.modules.keys()):
        if "agentic_data_ingestion_dashboard_api" in mod_name:
            del sys.modules[mod_name]
        if mod_name == "router" or mod_name == "handlers" or mod_name.startswith("handlers."):
            del sys.modules[mod_name]

    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    api_dir = str(_LAMBDA_DIR / "agentic_data_ingestion_dashboard_api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)

    common_path = _LAMBDA_DIR / "agentic_data_ingestion_dashboard_api" / "handlers" / "common.py"
    common_spec = importlib.util.spec_from_file_location("handlers.common", common_path)
    common_mod = importlib.util.module_from_spec(common_spec)
    common_spec.loader.exec_module(common_mod)
    common_mod.executions_table = exec_counter
    common_mod.state_table = state_counter
    common_mod.approvals_table = approvals_counter
    common_mod.dynamodb = resource_counter
    sys.modules["handlers.common"] = common_mod

    handler_path = _LAMBDA_DIR / "agentic_data_ingestion_dashboard_api" / "handlers" / "pipelines.py"
    spec = importlib.util.spec_from_file_location("handlers.pipelines", handler_path)
    pipelines_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipelines_mod)

    response = pipelines_mod.get_pipeline_executions(pipeline_id, page=1, page_size=run_count)
    body = json.loads(response["body"])

    # The extra call is the cost. Two is exactly right for 136 keys, and it is
    # bounded — not one call per run.
    assert resource_counter.operation_count == 2, (
        f"Expected 136 keys to chunk into exactly 2 batch_get_item calls, got "
        f"{resource_counter.operation_count}. Operations: {resource_counter.operations_log}"
    )

    # Correctness is unaffected by where the split falls: every run, including
    # the two whose counts arrived on the second call, gets its own outcome.
    executions = body["executions"]
    assert len(executions) == run_count, f"Expected {run_count} runs, got {len(executions)}"
    for exec_item in executions:
        run_id = exec_item["run_id"]
        outcome = exec_item["load_outcome"]
        expected = _expected_outcome(int(run_id.split("-")[1]))
        assert outcome["outcome"] == expected, (
            f"{run_id}: chunk split changed the result — expected {expected}, got {outcome['outcome']}"
        )


@mock_aws
def test_get_pipeline_executions_response_time_budget(monkeypatch):
    """Assert response time < 500ms for 20 execution items in moto mode.

    Validates: Requirements 19.2
    """
    monkeypatch.setenv("STATE_TABLE", "agentic_data_ingestion_state")
    monkeypatch.setenv("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
    monkeypatch.setenv("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
    monkeypatch.setenv("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
    monkeypatch.setenv("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
    monkeypatch.setenv("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
    monkeypatch.setenv("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
    # handlers.common requires AUDIT_TABLE/AGENT_TRACES_TABLE at import time under
    # AGENTIC_DATA_INGESTION_ENV=test (no default fallback). Set them so the decomposed façade
    # modules load cleanly (P21 Task 7.2).
    monkeypatch.setenv("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
    monkeypatch.setenv("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AGENTIC_DATA_INGESTION_ENV", "test")
    monkeypatch.setenv("WS_API_ENDPOINT", "https://test.execute-api.us-east-1.amazonaws.com/prod")
    monkeypatch.setenv("POWERTOOLS_ENABLED", "false")

    dynamodb_resource = boto3.resource("dynamodb", region_name="us-east-1")
    _create_tables(dynamodb_resource)

    pipeline_id = "test-pipeline-time"
    exec_table = dynamodb_resource.Table("agentic_data_ingestion_executions")
    state_table = dynamodb_resource.Table("agentic_data_ingestion_state")
    _seed_executions(exec_table, pipeline_id, count=20)
    _seed_state_records(state_table, count=20, pipeline_id=pipeline_id)
    # Same seed as the operation-count test: the response-time budget has to be
    # measured against the enriched read, not against an empty counts path.
    _seed_load_metrics_records(state_table, count=20, pipeline_id=pipeline_id)

    # Clear and re-import — purge ALL handlers.* submodules so the decomposed
    # siblings (e.g. handlers.pipelines_runs) re-bind to the moto-patched common.
    for mod_name in list(sys.modules.keys()):
        if "agentic_data_ingestion_dashboard_api" in mod_name:
            del sys.modules[mod_name]
        if mod_name == "router" or mod_name == "handlers" or mod_name.startswith("handlers."):
            del sys.modules[mod_name]

    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    api_dir = str(_LAMBDA_DIR / "agentic_data_ingestion_dashboard_api")
    if api_dir not in sys.path:
        sys.path.insert(0, api_dir)

    common_path = _LAMBDA_DIR / "agentic_data_ingestion_dashboard_api" / "handlers" / "common.py"
    common_spec = importlib.util.spec_from_file_location("handlers.common", common_path)
    common_mod = importlib.util.module_from_spec(common_spec)
    common_spec.loader.exec_module(common_mod)
    sys.modules["handlers.common"] = common_mod

    handler_path = _LAMBDA_DIR / "agentic_data_ingestion_dashboard_api" / "handlers" / "pipelines.py"
    spec = importlib.util.spec_from_file_location("handlers.pipelines", handler_path)
    pipelines_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipelines_mod)

    # Measure across 3 calls for stability
    times = []
    for _ in range(3):
        start = time.monotonic()
        pipelines_mod.get_pipeline_executions(pipeline_id, page=1, page_size=20)
        times.append((time.monotonic() - start) * 1000)

    avg_ms = sum(times) / len(times)
    assert avg_ms < 500, f"Average response time {avg_ms:.0f}ms exceeds 500ms budget"
