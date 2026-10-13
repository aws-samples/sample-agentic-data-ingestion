# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Grep tests: no Lambda code references removed GSI names or schema_metadata table.

Validates AC-GSI.3: No Lambda code references the removed GSIs or the
schema_metadata table.

Walks all .py files under lambda/ and checks that none contain references
to dead GSI names or the deleted schema_metadata table.
"""

import os

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda")

DEAD_PATTERNS = [
    "RunIdIndex",
    "OriginalPipelineIndex",
    "TimestampIndex",
    "GlueJobRunIdIndex",
    "schema_metadata",
    "CreatedAtIndex",
    "DashboardReadIndex",
]
# NOTE: 'StatusIndex' is intentionally NOT in this list. It is an active GSI
# on the Executions Summary table (data-stack.ts) and is queried by
# handlers/health.py to count executions by status — it is not a dead GSI.


def _collect_py_files():
    """Collect all .py files under lambda/, excluding __pycache__."""
    py_files = []
    for root, dirs, files in os.walk(LAMBDA_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(root, f))
    return py_files


def _find_references(pattern: str):
    """Return list of (filepath, line_number, line) tuples where pattern appears."""
    hits = []
    for path in _collect_py_files():
        with open(path) as fh:
            for lineno, line in enumerate(fh, 1):
                if pattern in line:
                    rel = os.path.relpath(path, LAMBDA_DIR)
                    hits.append((rel, lineno, line.rstrip()))
    return hits


class TestNoDeadGSIReferences:
    """No Lambda .py file references removed GSI names."""

    def test_no_run_id_index(self):
        hits = _find_references("RunIdIndex")
        assert hits == [], f"RunIdIndex found in Lambda code: {hits}"

    def test_no_original_pipeline_index(self):
        hits = _find_references("OriginalPipelineIndex")
        assert hits == [], f"OriginalPipelineIndex found in Lambda code: {hits}"

    def test_no_timestamp_index(self):
        hits = _find_references("TimestampIndex")
        assert hits == [], f"TimestampIndex found in Lambda code: {hits}"

    def test_no_glue_job_run_id_index(self):
        hits = _find_references("GlueJobRunIdIndex")
        assert hits == [], f"GlueJobRunIdIndex found in Lambda code: {hits}"


class TestNoSchemaMetadataReferences:
    """No Lambda .py file references the deleted schema_metadata table."""

    def test_no_schema_metadata(self):
        hits = _find_references("schema_metadata")
        assert hits == [], f"schema_metadata found in Lambda code: {hits}"


class TestNoDeadNotificationGSIReferences:
    """No Lambda .py file references removed notification GSI names."""

    def test_no_created_at_index(self):
        hits = _find_references("CreatedAtIndex")
        assert hits == [], f"CreatedAtIndex found in Lambda code: {hits}"

    def test_no_dashboard_read_index(self):
        hits = _find_references("DashboardReadIndex")
        assert hits == [], f"DashboardReadIndex found in Lambda code: {hits}"
