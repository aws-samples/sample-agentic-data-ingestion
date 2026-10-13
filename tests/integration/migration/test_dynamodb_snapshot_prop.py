# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: DynamoDB item snapshot stability (P34).

For any pipeline's DynamoDB item, comparing against golden snapshot
detects unexpected field changes.

**Validates: Requirements 21.2**
"""

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"


def _load_golden_snapshot(name: str) -> dict:
    """Load a golden DynamoDB item snapshot from fixtures."""
    path = _FIXTURES_DIR / "dynamodb" / name
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _compare_keys(actual: dict, snapshot: dict) -> list[str]:
    """Compare keys between actual and snapshot, returning differences."""
    diffs = []
    snapshot_keys = set(snapshot.keys())
    actual_keys = set(actual.keys())

    for key in snapshot_keys - actual_keys:
        diffs.append(f"MISSING: {key}")
    for key in actual_keys - snapshot_keys:
        diffs.append(f"EXTRA: {key}")

    return diffs


# Strategy: random extra fields that might be added by a migration
_extra_field_strategy = st.fixed_dictionaries(
    {
        "field_name": st.from_regex(r"[a-z_]{3,15}", fullmatch=True),
        "field_value": st.one_of(
            st.text(min_size=1, max_size=20),
            st.integers(min_value=0, max_value=1000),
            st.booleans(),
        ),
    }
)


@given(extra=_extra_field_strategy)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_snapshot_detects_unexpected_field_additions(extra):
    """Adding any unexpected field to a pipeline item is detected by snapshot comparison.

    **Validates: Requirements 21.2**
    """
    snapshot = _load_golden_snapshot("pipeline_created.json")

    # Create a modified item with an extra field
    actual = dict(snapshot)
    actual[extra["field_name"]] = extra["field_value"]

    diffs = _compare_keys(actual, snapshot)

    # If the extra field name happens to already be in the snapshot, no diff expected
    if extra["field_name"] not in snapshot:
        assert any("EXTRA" in d for d in diffs), (
            f"Snapshot comparison failed to detect extra field '{extra['field_name']}'"
        )


@given(
    field_to_remove=st.sampled_from(
        [
            "pipeline_name",
            "status",
            "target_config",
            "approval_config",
        ]
    )
)
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_snapshot_detects_field_removals(field_to_remove):
    """Removing any expected field from a pipeline item is detected.

    **Validates: Requirements 21.2**
    """
    snapshot = _load_golden_snapshot("pipeline_created.json")

    actual = dict(snapshot)
    if field_to_remove in actual:
        del actual[field_to_remove]

        diffs = _compare_keys(actual, snapshot)
        assert any("MISSING" in d and field_to_remove in d for d in diffs), (
            f"Snapshot comparison failed to detect missing field '{field_to_remove}'"
        )
