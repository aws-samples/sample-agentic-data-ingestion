# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for self-healing agent error classification and remediation selection."""

import os
import sys
from unittest.mock import MagicMock, patch

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "self_healing_agent")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

# Mock heavy dependencies before importing the module
with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": MagicMock(),
        "bedrock_agentcore.runtime": MagicMock(),
        "boto3": MagicMock(),
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
    },
):
    from main import classify_error


# ── Schema mismatch errors ──────────────────────────────────────────


class TestClassifySchemaErrors:
    def test_analysis_exception(self):
        assert classify_error("AnalysisException: cannot resolve column") == "schema_mismatch"

    def test_cannot_resolve(self):
        assert classify_error("cannot resolve 'col_x' given input columns") == "schema_mismatch"

    def test_schema_mismatch_phrase(self):
        assert classify_error("schema mismatch between source and target") == "schema_mismatch"

    def test_column_not_found(self):
        assert classify_error("column not found: customer_id") == "schema_mismatch"


# ── OOM errors ──────────────────────────────────────────────────────


class TestClassifyOomErrors:
    def test_out_of_memory_error(self):
        assert classify_error("java.lang.OutOfMemoryError: Java heap space") == "oom_error"

    def test_container_killed_by_yarn(self):
        assert classify_error("Container killed by YARN for exceeding memory") == "oom_error"

    def test_memory_exceeded(self):
        assert classify_error("memory exceeded during shuffle phase") == "oom_error"


# ── Type cast errors ────────────────────────────────────────────────


class TestClassifyTypeCastErrors:
    def test_number_format_exception(self):
        assert classify_error("NumberFormatException: For input string: 'abc'") == "type_cast_error"

    def test_datetime_parse_exception(self):
        assert classify_error("DateTimeParseException: Text '2025-13-01'") == "type_cast_error"

    def test_cannot_cast(self):
        assert classify_error("cannot cast string to integer") == "type_cast_error"

    def test_type_mismatch(self):
        assert classify_error("type mismatch: expected int, got string") == "type_cast_error"


# ── Permission errors ───────────────────────────────────────────────


class TestClassifyPermissionErrors:
    def test_access_denied(self):
        assert classify_error("AccessDeniedException: User is not authorized") == "permission_error"

    def test_not_authorized(self):
        assert classify_error("not authorized to perform glue:GetTable") == "permission_error"

    def test_permission_denied(self):
        assert classify_error("permission denied on s3://bucket/key") == "permission_error"


# ── Unknown / edge cases ────────────────────────────────────────────


class TestClassifyUnknownErrors:
    def test_unknown_error(self):
        assert classify_error("Something completely unexpected happened") == "unknown"

    def test_empty_string(self):
        assert classify_error("") == "unknown"

    def test_mixed_case_matching(self):
        """Error patterns should match case-insensitively."""
        assert classify_error("ANALYSISEXCEPTION: CANNOT RESOLVE") == "schema_mismatch"
        assert classify_error("outofmemoryerror") == "oom_error"
        assert classify_error("NUMBERFORMATEXCEPTION") == "type_cast_error"
        assert classify_error("PERMISSION DENIED") == "permission_error"
