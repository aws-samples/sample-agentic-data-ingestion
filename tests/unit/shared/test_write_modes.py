# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``shared/write_modes.py`` — which write modes each target implements.

Expectations come from the writers themselves, read 2026-10-10: only the S3
Tables writer has a ``merge`` branch (``MERGE INTO``); the JDBC, S3 Parquet and
Glue Catalog writers branch on one value and send everything else to an
``else`` arm. The templates compare the value with exact Jinja2 equality, so a
mixed-case spelling is not the mode it resembles.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from write_modes import WRITE_MODES, supported_write_modes, write_mode_refusal

S3_TABLES_SPELLINGS = ["NEW_S3_TABLE_BUCKET", "EXISTING_S3_TABLE_BUCKET", "s3_tables", "S3_TABLE_BUCKET"]
OTHER_TARGETS = ["JDBC", "jdbc", "RDS_DATABASE", "S3_PARQUET", "GLUE_CATALOG"]


@pytest.mark.parametrize("target_type", S3_TABLES_SPELLINGS)
def test_s3_tables_supports_every_mode(target_type):
    assert supported_write_modes(target_type) == WRITE_MODES
    for mode in WRITE_MODES:
        assert write_mode_refusal(target_type, mode) is None


@pytest.mark.parametrize("target_type", OTHER_TARGETS)
def test_other_targets_support_append_and_overwrite_only(target_type):
    assert supported_write_modes(target_type) == ("overwrite", "append")
    assert write_mode_refusal(target_type, "append") is None
    assert write_mode_refusal(target_type, "overwrite") is None


@pytest.mark.parametrize("target_type", OTHER_TARGETS)
def test_merge_outside_s3_tables_names_the_target_and_the_alternatives(target_type):
    message = write_mode_refusal(target_type, "merge")
    assert message is not None
    assert target_type in message
    assert "S3 Tables" in message
    assert "overwrite, append" in message


@pytest.mark.parametrize("target_type", [None, "", "   ", 42, "SOMETHING_NEW"])
def test_an_absent_or_unknown_target_gets_no_merge(target_type):
    """An unrecognised type gets no template at all, so it cannot merge."""
    assert supported_write_modes(target_type) == ("overwrite", "append")
    assert write_mode_refusal(target_type, "merge") is not None


@pytest.mark.parametrize("write_mode", ["Overwrite", "APPEND", " append", "upsert", "", None, 1])
def test_anything_but_the_exact_spelling_is_refused(write_mode):
    """`"Overwrite"` renders the writer's `else` arm, so accepting it as
    `overwrite` would approve a write that never happens."""
    for target_type in ["NEW_S3_TABLE_BUCKET", "JDBC"]:
        message = write_mode_refusal(target_type, write_mode)
        assert message is not None
        assert repr(write_mode) in message
