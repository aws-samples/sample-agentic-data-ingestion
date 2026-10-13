# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 10.4 — Golden script snapshot comparison tests.

Compares rendered templates against golden-file scripts in tests/fixtures/golden_scripts/.
Uses difflib to show differences on failure.
"""

import difflib
import os

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")
GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures", "golden_scripts")

SAMPLE_TRANSFORM = "df_transformed = df"

# Golden script → (template file, render vars)
GOLDEN_CONFIGS = {
    "s3_tables_append.py": (
        "glue_job_s3_tables.py.j2",
        {
            "write_mode": "append",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
    "s3_tables_overwrite.py": (
        "glue_job_s3_tables.py.j2",
        {
            "write_mode": "overwrite",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
    "s3_tables_merge.py": (
        "glue_job_s3_tables.py.j2",
        {
            "write_mode": "merge",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
    "s3_tables_append_checkpoint.py": (
        "glue_job_s3_tables.py.j2",
        {
            "write_mode": "append",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": True,
            "checkpoint_bucket": "agentic-data-ingestion-checkpoint-bucket",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
    "glue_catalog_append.py": (
        "glue_job_glue_catalog.py.j2",
        {
            "write_mode": "append",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
    "glue_catalog_overwrite.py": (
        "glue_job_glue_catalog.py.j2",
        {
            "write_mode": "overwrite",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
    "s3_parquet_append.py": (
        "glue_job_s3_parquet.py.j2",
        {
            "write_mode": "append",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
    "s3_parquet_overwrite.py": (
        "glue_job_s3_parquet.py.j2",
        {
            "write_mode": "overwrite",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
    "jdbc_append.py": (
        "glue_job_jdbc.py.j2",
        {
            "write_mode": "append",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
    "jdbc_overwrite.py": (
        "glue_job_jdbc.py.j2",
        {
            "write_mode": "overwrite",
            "source_format": "json",
            "transform_block": SAMPLE_TRANSFORM,
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "partition_columns": None,
            "merge_key": "id",
        },
    ),
}


@pytest.fixture
def jinja_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


@pytest.mark.parametrize("golden_name,config", list(GOLDEN_CONFIGS.items()))
def test_rendered_matches_golden_script(jinja_env, golden_name, config):
    """Rendered template must match the golden script exactly."""
    template_file, render_vars = config
    template = jinja_env.get_template(template_file)
    rendered = template.render(**render_vars)

    golden_path = os.path.join(GOLDEN_DIR, golden_name)
    assert os.path.exists(golden_path), f"Golden script not found: {golden_path}"

    with open(golden_path) as f:
        golden = f.read()

    # Normalize trailing newline for comparison (Jinja2 keep_trailing_newline may differ)
    rendered_normalized = rendered.rstrip("\n")
    golden_normalized = golden.rstrip("\n")

    if rendered_normalized != golden_normalized:
        diff = difflib.unified_diff(
            golden_normalized.splitlines(keepends=True),
            rendered_normalized.splitlines(keepends=True),
            fromfile=f"golden/{golden_name}",
            tofile=f"rendered/{golden_name}",
            lineterm="",
        )
        diff_text = "\n".join(diff)
        pytest.fail(f"Rendered output does not match golden script '{golden_name}':\n{diff_text}")
