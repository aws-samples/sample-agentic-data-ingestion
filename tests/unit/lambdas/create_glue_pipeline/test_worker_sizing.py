# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for get_baseline_worker_config — tier boundaries, edge cases, file_count multiplier."""

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

# ---------------------------------------------------------------------------
# Module loading — mock heavy deps so we can import the pure function
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("MCP_SIGV4_ENABLED", "false")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

_mods = {
    "strands": mock.MagicMock(),
    "strands.agent": mock.MagicMock(),
    "strands.tools.mcp": mock.MagicMock(),
    "streamable_http_sigv4": mock.MagicMock(),
    "mcp": mock.MagicMock(),
    "mcp.client": mock.MagicMock(),
    "mcp.client.streamable_http": mock.MagicMock(),
    "schema_evolution": mock.MagicMock(),
    "metrics_facade": mock.MagicMock(),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock prompt"),
    ),
}


def _load_module():
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_glue_pipeline_lf",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
get_baseline_worker_config = _mod.get_baseline_worker_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GB = 1024**3

TIER_1 = {"worker_type": "G.1X", "number_of_workers": 2, "timeout": 30}
TIER_2 = {"worker_type": "G.1X", "number_of_workers": 5, "timeout": 60}
TIER_3 = {"worker_type": "G.2X", "number_of_workers": 10, "timeout": 90}
TIER_4 = {"worker_type": "G.2X", "number_of_workers": 20, "timeout": 120}


# ---------------------------------------------------------------------------
# Representative values per tier
# ---------------------------------------------------------------------------


class TestRepresentativeValues:
    """Each tier returns the correct config for a typical value."""

    @pytest.mark.parametrize(
        "size_bytes, expected",
        [
            pytest.param(500 * 1024 * 1024, TIER_1, id="500MB-tier1"),
            pytest.param(5 * GB, TIER_2, id="5GB-tier2"),
            pytest.param(25 * GB, TIER_3, id="25GB-tier3"),
            pytest.param(100 * GB, TIER_4, id="100GB-tier4"),
        ],
    )
    def test_representative(self, size_bytes, expected):
        assert get_baseline_worker_config(size_bytes) == expected


# ---------------------------------------------------------------------------
# Tier boundaries — values exactly at thresholds
# ---------------------------------------------------------------------------


class TestTierBoundaries:
    """Validates: AC-6 — boundary values land in the correct tier."""

    @pytest.mark.parametrize(
        "size_bytes, expected, description",
        [
            # Just below 1 GB → tier 1
            pytest.param(GB - 1, TIER_1, "just-under-1GB"),
            # Exactly 1 GB → tier 2 (total_gb == 1.0, not < 1)
            pytest.param(1 * GB, TIER_2, "exactly-1GB"),
            # Just below 10 GB → tier 2
            pytest.param(10 * GB - 1, TIER_2, "just-under-10GB"),
            # Exactly 10 GB → tier 3
            pytest.param(10 * GB, TIER_3, "exactly-10GB"),
            # Just below 50 GB → tier 3
            pytest.param(50 * GB - 1, TIER_3, "just-under-50GB"),
            # Exactly 50 GB → tier 4
            pytest.param(50 * GB, TIER_4, "exactly-50GB"),
        ],
    )
    def test_boundary(self, size_bytes, expected, description):
        assert get_baseline_worker_config(size_bytes) == expected


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Zero bytes, 1 byte, and very large files."""

    def test_zero_bytes(self):
        assert get_baseline_worker_config(0) == TIER_1

    def test_one_byte(self):
        assert get_baseline_worker_config(1) == TIER_1

    def test_very_large_file(self):
        """1 TB file → tier 4."""
        assert get_baseline_worker_config(1024 * GB) == TIER_4


# ---------------------------------------------------------------------------
# file_count multiplier
# ---------------------------------------------------------------------------


class TestFileCountMultiplier:
    """Total volume = file_size_bytes * file_count determines the tier."""

    def test_small_files_aggregate_to_higher_tier(self):
        """100 MB * 15 files = 1.5 GB → tier 2."""
        size = 100 * 1024 * 1024  # 100 MB
        assert get_baseline_worker_config(size, file_count=15) == TIER_2

    def test_multiplier_crosses_10gb(self):
        """1 GB * 12 files = 12 GB → tier 3."""
        assert get_baseline_worker_config(GB, file_count=12) == TIER_3

    def test_multiplier_crosses_50gb(self):
        """5 GB * 11 files = 55 GB → tier 4."""
        assert get_baseline_worker_config(5 * GB, file_count=11) == TIER_4

    def test_default_file_count_is_one(self):
        """Calling without file_count uses default=1."""
        result_default = get_baseline_worker_config(5 * GB)
        result_explicit = get_baseline_worker_config(5 * GB, file_count=1)
        assert result_default == result_explicit == TIER_2
