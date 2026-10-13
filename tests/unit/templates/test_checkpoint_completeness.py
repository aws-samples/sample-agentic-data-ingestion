# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 10.3 — Checkpoint template completeness tests.

Validates checkpoint_enabled=True templates contain checkpoint read/write block,
and checkpoint_enabled=False templates do NOT contain checkpoint logic.
Templates use `except _AnalysisException:` (imported from pyspark.sql.utils).
"""

import os

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = "df_transformed = df"

TEMPLATE_NAMES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_jdbc.py.j2",
]

BASE_VARS = {
    "source_format": "json",
    "partition_columns": None,
    "write_mode": "overwrite",
    "merge_key": "id",
}


@pytest.fixture
def jinja_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


# --- checkpoint_enabled=True ---


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_enabled_has_checkpoint_path(jinja_env, template_name):
    """checkpoint_enabled=True must produce checkpoint_path variable."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-bucket",
        **BASE_VARS,
    )
    assert "checkpoint_path" in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_enabled_has_read_parquet(jinja_env, template_name):
    """checkpoint_enabled=True must contain spark.read.parquet for checkpoint resume."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-bucket",
        **BASE_VARS,
    )
    assert "spark.read.parquet(checkpoint_path)" in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_enabled_has_write_parquet(jinja_env, template_name):
    """checkpoint_enabled=True must contain write.mode("overwrite").parquet for checkpoint save."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-bucket",
        **BASE_VARS,
    )
    assert '.write.mode("overwrite").parquet(checkpoint_path)' in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_enabled_has_analysis_exception(jinja_env, template_name):
    """checkpoint_enabled=True uses 'except _AnalysisException:' for checkpoint resume."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-bucket",
        **BASE_VARS,
    )
    # The checkpoint block imports AnalysisException and catches it specifically
    assert "from pyspark.sql.utils import AnalysisException" in rendered
    assert "except _AnalysisException:" in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_enabled_has_bucket_reference(jinja_env, template_name):
    """checkpoint_enabled=True must reference the checkpoint bucket name."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="test-checkpoint-bucket",
        **BASE_VARS,
    )
    assert "test-checkpoint-bucket" in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_enabled_has_resume_log(jinja_env, template_name):
    """checkpoint_enabled=True must log checkpoint resume."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-bucket",
        **BASE_VARS,
    )
    assert "Resumed from checkpoint" in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_enabled_has_save_log(jinja_env, template_name):
    """checkpoint_enabled=True must log checkpoint save."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-bucket",
        **BASE_VARS,
    )
    assert "Checkpoint saved to" in rendered


# --- checkpoint_enabled=False ---


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_disabled_no_checkpoint_path(jinja_env, template_name):
    """checkpoint_enabled=False must NOT contain checkpoint_path."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=False,
        checkpoint_bucket="",
        **BASE_VARS,
    )
    assert "checkpoint_path" not in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_disabled_no_checkpoint_read(jinja_env, template_name):
    """checkpoint_enabled=False must NOT contain checkpoint read logic."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=False,
        checkpoint_bucket="",
        **BASE_VARS,
    )
    assert "Resumed from checkpoint" not in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_disabled_still_has_transform(jinja_env, template_name):
    """checkpoint_enabled=False must still contain the transform block."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=False,
        checkpoint_bucket="",
        **BASE_VARS,
    )
    assert "df_transformed = df" in rendered
