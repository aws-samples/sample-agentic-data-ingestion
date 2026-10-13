# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for compute-stack.ts Lambda environment variable standardization.

Validates AC-14.3: All Lambda environment variables in compute-stack.ts
reference the correct new table names and old inconsistent names are removed.
"""

import os
import re

import pytest

COMPUTE_STACK_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "compute-stack.ts")


@pytest.fixture(scope="module")
def compute_stack_content():
    with open(COMPUTE_STACK_PATH) as f:
        return f.read()


# ── Removed env var names ───────────────────────────────────────────


class TestRemovedEnvVars:
    """Old/inconsistent env var names are no longer used."""

    def test_no_dynamodb_table_env_var(self, compute_stack_content):
        """DYNAMODB_TABLE replaced by STATE_TABLE everywhere."""
        # Match DYNAMODB_TABLE as an env var key (not in comments)
        env_lines = [
            line.strip()
            for line in compute_stack_content.splitlines()
            if "DYNAMODB_TABLE" in line and not line.strip().startswith("//")
        ]
        assert len(env_lines) == 0, f"Found DYNAMODB_TABLE references (should be STATE_TABLE): {env_lines}"

    def test_no_pipeline_runs_table_env_var(self, compute_stack_content):
        """PIPELINE_RUNS_TABLE replaced by STATE_TABLE everywhere."""
        env_lines = [
            line.strip()
            for line in compute_stack_content.splitlines()
            if "PIPELINE_RUNS_TABLE" in line and not line.strip().startswith("//")
        ]
        assert len(env_lines) == 0, f"Found PIPELINE_RUNS_TABLE references (should be STATE_TABLE): {env_lines}"

    def test_no_schema_metadata_table_env_var(self, compute_stack_content):
        """SCHEMA_METADATA_TABLE removed (table deleted)."""
        env_lines = [
            line.strip()
            for line in compute_stack_content.splitlines()
            if "SCHEMA_METADATA_TABLE" in line and not line.strip().startswith("//")
        ]
        assert len(env_lines) == 0, f"Found SCHEMA_METADATA_TABLE references (table deleted): {env_lines}"


# ── Standardized env var names ──────────────────────────────────────


class TestStandardizedEnvVars:
    """Dashboard API Lambda uses correct standardized env var names."""

    def test_dashboard_api_has_approvals_table(self, compute_stack_content):
        """Dashboard API uses APPROVALS_TABLE (not APPROVALS_TABLE_NAME)."""
        # Anchored on the CONSTRUCTION, not on a bare `DashboardApi` substring.
        #
        # The previous pattern was `DashboardApi.*?environment:\s*\{(.*?)\}`, which
        # started at the first occurrence of that token anywhere in the file — so a
        # PROSE MENTION of the construct id in a comment above another Lambda made
        # this test read a different function's environment block and report the
        # dashboard API as missing a variable it still had. That happened.
        #
        # The sibling files in this directory (`test_verification_frame_mode_env.py`,
        # `test_verification_executor_env.py`) already anchor on
        # `<name>Lambda = new lambda.Function(this, '<Id>',`, which is the shape
        # adopted here. The closing bound is `\n    },` — the four-space-indented
        # brace that ends the environment object — rather than the first `}`, which
        # a nested object or an inline template literal would have truncated at.
        dashboard_match = re.search(
            r"dashboardApiLambda = new lambda\.Function\(this, 'DashboardApi',.*?environment:\s*\{(.*?)\n\s{6}\},",
            compute_stack_content,
            re.DOTALL,
        )
        assert dashboard_match, "DashboardApi Lambda definition not found"
        env_block = dashboard_match.group(1)

        assert "APPROVALS_TABLE:" in env_block, "Dashboard API missing APPROVALS_TABLE env var"

    def test_no_approvals_table_name_env_var(self, compute_stack_content):
        """APPROVALS_TABLE_NAME (old inconsistent name) is not used anywhere."""
        env_lines = [
            line.strip()
            for line in compute_stack_content.splitlines()
            if "APPROVALS_TABLE_NAME" in line and not line.strip().startswith("//")
        ]
        assert len(env_lines) == 0, f"Found APPROVALS_TABLE_NAME (should be APPROVALS_TABLE): {env_lines}"
