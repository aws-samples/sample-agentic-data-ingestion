# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for compute_schema_hash (Task 1.4).

Validates: AC-15.1, AC-15.2, AC-15.3
"""

import hashlib
import json
import os
import re
import sys
from unittest.mock import MagicMock

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

SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "active": {"type": "boolean"},
    },
}


def test_deterministic_output():
    """Same schema dict produces identical hash on every call."""
    h1 = compute_schema_hash(SAMPLE_SCHEMA)
    h2 = compute_schema_hash(SAMPLE_SCHEMA)
    assert h1 == h2


def test_hex_digest_format():
    """Hash is a 64-character lowercase hex string (SHA-256)."""
    h = compute_schema_hash(SAMPLE_SCHEMA)
    assert len(h) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", h)


def test_uses_compact_separators():
    """Hash matches manual SHA-256 with sort_keys=True, separators=(',', ':')."""
    expected_json = json.dumps(SAMPLE_SCHEMA, sort_keys=True, separators=(",", ":"))
    expected_hash = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    assert compute_schema_hash(SAMPLE_SCHEMA) == expected_hash


def test_key_order_irrelevant():
    """Dicts with same keys in different insertion order produce the same hash."""
    schema_a = {"b": {"type": "string"}, "a": {"type": "integer"}}
    schema_b = {"a": {"type": "integer"}, "b": {"type": "string"}}
    assert compute_schema_hash(schema_a) == compute_schema_hash(schema_b)


def test_empty_dict():
    """Empty dict produces a valid 64-char hex hash."""
    h = compute_schema_hash({})
    assert len(h) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", h)
