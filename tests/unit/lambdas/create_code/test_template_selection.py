# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: _select_template returns the correct template for each target_type.

Migrated from the deprecated ``TEMPLATE_MAP`` alias (deleted in P28 task
15.2's dead-path sweep) to ``TEMPLATE_RULES`` + ``_select_template``, the
selection surface production code actually uses.

Imports ``code_assembly`` directly — the extracted module is import-light
(no strands, no boto3 clients), so no heavy-dependency stubbing is needed.
"""

import os
import sys

import pytest

# Add Lambda dir and shared dir to path so we can import code_assembly
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Set AWS region before any boto3-adjacent imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

from code_assembly import TEMPLATE_RULES, _select_template

# S3_UPLOAD-source expectations (the former TEMPLATE_MAP contract, plus the
# P14 rds_database entry TEMPLATE_RULES added).
EXPECTED = {
    "s3_tables": "glue_job_s3_tables.py.j2",
    "glue_catalog": "glue_job_glue_catalog.py.j2",
    "jdbc": "glue_job_jdbc.py.j2",
    "rds_database": "glue_job_jdbc.py.j2",
    "s3_parquet": "glue_job_s3_parquet.py.j2",
}


@pytest.mark.parametrize("target_type,expected_template", list(EXPECTED.items()))
def test_select_template_returns_correct_template_for_s3_upload(target_type, expected_template):
    assert _select_template("S3_UPLOAD", target_type) == expected_template


@pytest.mark.parametrize("target_type", list(EXPECTED.keys()))
def test_jdbc_source_wins_over_target_type(target_type):
    """The JDBC source rule precedes S3_UPLOAD rules: a JDBC-source pipeline
    routes to the JDBC source template regardless of target type."""
    assert _select_template("JDBC", target_type) == "glue_job_jdbc_source.py.j2"


def test_unknown_pair_returns_none():
    assert _select_template("S3_UPLOAD", "unknown_target") is None
    assert _select_template("UNKNOWN_SOURCE", "s3_tables") is None


def test_template_rules_cover_all_production_target_types():
    """Every production target type has an S3_UPLOAD rule (the former
    TEMPLATE_MAP coverage contract, now expressed over TEMPLATE_RULES)."""
    s3_upload_targets = {target for source, target, _ in TEMPLATE_RULES if source == "S3_UPLOAD"}
    assert s3_upload_targets == {"s3_tables", "glue_catalog", "jdbc", "rds_database", "s3_parquet"}
