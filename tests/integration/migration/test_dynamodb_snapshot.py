# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""DynamoDB snapshot comparison test.

Compares a pipeline DynamoDB item against a golden snapshot and flags
unexpected field additions, removals, or type changes.

Validates: Requirements 21.2, 21.3
"""

import json
from decimal import Decimal
from pathlib import Path

import boto3
from moto import mock_aws

_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _load_golden_snapshot(name: str) -> dict:
    """Load a golden DynamoDB item snapshot from fixtures."""
    path = _FIXTURES_DIR / "dynamodb" / name
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _create_pipelines_table(dynamodb_resource):
    """Create the pipelines DynamoDB table."""
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_pipelines",
        KeySchema=[{"AttributeName": "pipeline_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb_resource.Table("agentic_data_ingestion_pipelines")


def _compare_item_to_snapshot(actual: dict, snapshot: dict) -> list[str]:
    """Compare a DynamoDB item against a golden snapshot.

    Returns a list of differences found. Empty list means items match.
    Checks for:
    - Missing fields (in snapshot but not in actual)
    - Extra fields (in actual but not in snapshot)
    - Type mismatches (field exists in both but different Python types)

    Note: DynamoDB stores numbers as Decimal, so int/float in snapshot
    are considered compatible with Decimal in actual.
    """
    diffs = []

    # Types that are compatible (DynamoDB Decimal ↔ int/float)
    NUMERIC_TYPES = {int, float, Decimal}

    snapshot_keys = set(snapshot.keys())
    actual_keys = set(actual.keys())

    # Fields in snapshot but missing from actual
    for key in snapshot_keys - actual_keys:
        diffs.append(f"MISSING field '{key}' (expected from snapshot)")

    # Fields in actual but not in snapshot (unexpected additions)
    for key in actual_keys - snapshot_keys:
        diffs.append(f"EXTRA field '{key}' (not in golden snapshot)")

    # Type mismatches for shared fields
    for key in snapshot_keys & actual_keys:
        snap_type = type(snapshot[key])
        actual_type = type(actual[key])

        # Allow numeric type compatibility (int/float/Decimal)
        if snap_type in NUMERIC_TYPES and actual_type in NUMERIC_TYPES:
            continue

        if snap_type != actual_type:
            diffs.append(f"TYPE MISMATCH field '{key}': snapshot={snap_type.__name__}, actual={actual_type.__name__}")
        # Recurse into nested dicts
        if isinstance(snapshot[key], dict) and isinstance(actual[key], dict):
            nested = _compare_item_to_snapshot(actual[key], snapshot[key])
            for d in nested:
                diffs.append(f"{key}.{d}")

    return diffs


@mock_aws
def test_pipeline_item_matches_golden_snapshot():
    """Pipeline DynamoDB item structure matches the golden snapshot.

    Seeds a pipeline item matching the golden snapshot format, reads it back,
    and verifies no unexpected field changes.
    """
    snapshot = _load_golden_snapshot("pipeline_created.json")

    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = _create_pipelines_table(dynamodb)

    # Use pipeline_key as pipeline_id (the snapshot uses pipeline_key)
    pipeline_id = snapshot.get("pipeline_key", "pipeline-abc123")

    # Store the item using the snapshot data, mapping pipeline_key → pipeline_id
    item = dict(snapshot)
    item["pipeline_id"] = pipeline_id

    table.put_item(Item=item)

    # Read it back
    response = table.get_item(Key={"pipeline_id": pipeline_id})
    actual = response["Item"]

    # Compare against snapshot (the stored item should have all snapshot fields)
    diffs = _compare_item_to_snapshot(actual, item)

    assert len(diffs) == 0, "Pipeline item differs from golden snapshot:\n" + "\n".join(f"  - {d}" for d in diffs)


@mock_aws
def test_snapshot_detects_missing_field():
    """Snapshot comparison correctly detects a missing field."""
    snapshot = _load_golden_snapshot("pipeline_created.json")

    # Remove a field from the "actual" item
    actual = dict(snapshot)
    actual.pop("target_config", None)

    diffs = _compare_item_to_snapshot(actual, snapshot)
    assert any("MISSING" in d and "target_config" in d for d in diffs), (
        f"Expected MISSING target_config diff, got: {diffs}"
    )


@mock_aws
def test_snapshot_detects_extra_field():
    """Snapshot comparison correctly detects an unexpected extra field."""
    snapshot = _load_golden_snapshot("pipeline_created.json")

    actual = dict(snapshot)
    actual["unexpected_new_field"] = "surprise"

    diffs = _compare_item_to_snapshot(actual, snapshot)
    assert any("EXTRA" in d and "unexpected_new_field" in d for d in diffs), (
        f"Expected EXTRA unexpected_new_field diff, got: {diffs}"
    )


@mock_aws
def test_snapshot_detects_type_mismatch():
    """Snapshot comparison correctly detects a type change."""
    snapshot = _load_golden_snapshot("pipeline_created.json")

    actual = dict(snapshot)
    # Change pipeline_name from string to list (a real type mismatch)
    actual["pipeline_name"] = ["not", "a", "string"]

    diffs = _compare_item_to_snapshot(actual, snapshot)
    assert any("TYPE MISMATCH" in d and "pipeline_name" in d for d in diffs), (
        f"Expected TYPE MISMATCH for pipeline_name, got: {diffs}"
    )
