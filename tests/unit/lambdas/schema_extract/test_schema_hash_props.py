# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for compute_schema_hash (Task 1.5).

**Validates: Requirements AC-15.1, AC-15.2, AC-15.3**
"""

import os
import re
import sys
from unittest.mock import MagicMock

import hypothesis.strategies as st
from hypothesis import given, settings

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

# Add lambda directory to import path
_schema_extract_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_extract")
)
sys.path.insert(0, _schema_extract_dir)

# Stub out Lambda-layer dependencies (these modules only exist in the Lambda layer)
sys.modules.setdefault(
    "eventbridge_utils", MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock()))
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock())

sys.modules.pop("lambda_function", None)
# Re-insert to ensure our directory is at position 0
sys.path.insert(0, _schema_extract_dir)
try:
    from lambda_function import compute_schema_hash
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

# Strategy: JSON-serializable dicts (matching schema-like structures)
json_values = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-1_000_000, max_value=1_000_000),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=50),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=20), children, max_size=10),
    ),
    max_leaves=30,
)

json_dicts = st.dictionaries(st.text(min_size=1, max_size=20), json_values, max_size=10)


@given(json_dicts)
@settings(max_examples=200)
def test_hash_is_deterministic(schema: dict):
    """For any JSON-serializable dict, calling compute_schema_hash twice produces identical results.

    **Validates: Requirements AC-15.3**
    """
    h1 = compute_schema_hash(schema)
    h2 = compute_schema_hash(schema)
    assert h1 == h2, f"Non-deterministic hash: {h1} != {h2}"


@given(json_dicts)
@settings(max_examples=200)
def test_hash_is_valid_sha256_hex(schema: dict):
    """For any JSON-serializable dict, the hash is a 64-char lowercase hex string.

    **Validates: Requirements AC-15.1**
    """
    h = compute_schema_hash(schema)
    assert len(h) == 64, f"Expected 64 chars, got {len(h)}"
    assert re.fullmatch(r"[0-9a-f]{64}", h), f"Not valid hex: {h}"
