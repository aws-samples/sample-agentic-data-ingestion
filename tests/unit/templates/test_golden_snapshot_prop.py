# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 10.8 — Property 33: Golden script snapshot fidelity.

For any target type × write mode, rendered template matches golden-file script.

**Validates: Requirements 20.4**
"""

import os

import jinja2
from hypothesis import given, settings
from hypothesis import strategies as st

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")
GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures", "golden_scripts")

SAMPLE_TRANSFORM = "df_transformed = df"

# (golden_file, template_file, render_vars)
GOLDEN_MAPPINGS = [
    (
        "s3_tables_append.py",
        "glue_job_s3_tables.py.j2",
        {"write_mode": "append", "checkpoint_enabled": False, "checkpoint_bucket": ""},
    ),
    (
        "s3_tables_overwrite.py",
        "glue_job_s3_tables.py.j2",
        {"write_mode": "overwrite", "checkpoint_enabled": False, "checkpoint_bucket": ""},
    ),
    (
        "s3_tables_merge.py",
        "glue_job_s3_tables.py.j2",
        {"write_mode": "merge", "checkpoint_enabled": False, "checkpoint_bucket": ""},
    ),
    (
        "s3_tables_append_checkpoint.py",
        "glue_job_s3_tables.py.j2",
        {
            "write_mode": "append",
            "checkpoint_enabled": True,
            "checkpoint_bucket": "agentic-data-ingestion-checkpoint-bucket",
        },
    ),
    (
        "glue_catalog_append.py",
        "glue_job_glue_catalog.py.j2",
        {"write_mode": "append", "checkpoint_enabled": False, "checkpoint_bucket": ""},
    ),
    (
        "glue_catalog_overwrite.py",
        "glue_job_glue_catalog.py.j2",
        {"write_mode": "overwrite", "checkpoint_enabled": False, "checkpoint_bucket": ""},
    ),
    (
        "s3_parquet_append.py",
        "glue_job_s3_parquet.py.j2",
        {"write_mode": "append", "checkpoint_enabled": False, "checkpoint_bucket": ""},
    ),
    (
        "s3_parquet_overwrite.py",
        "glue_job_s3_parquet.py.j2",
        {"write_mode": "overwrite", "checkpoint_enabled": False, "checkpoint_bucket": ""},
    ),
    (
        "jdbc_append.py",
        "glue_job_jdbc.py.j2",
        {"write_mode": "append", "checkpoint_enabled": False, "checkpoint_bucket": ""},
    ),
    (
        "jdbc_overwrite.py",
        "glue_job_jdbc.py.j2",
        {"write_mode": "overwrite", "checkpoint_enabled": False, "checkpoint_bucket": ""},
    ),
]


def _make_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


mapping_strategy = st.sampled_from(GOLDEN_MAPPINGS)


@settings(max_examples=10)
@given(mapping=mapping_strategy)
def test_golden_snapshot_fidelity(mapping):
    """P33: For any target type × write mode, rendered template matches golden-file script."""
    golden_name, template_file, extra_vars = mapping

    golden_path = os.path.join(GOLDEN_DIR, golden_name)
    assert os.path.exists(golden_path), f"Golden script not found: {golden_path}"

    with open(golden_path) as f:
        golden = f.read()

    env = _make_env()
    template = env.get_template(template_file)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns=None,
        merge_key="id",
        **extra_vars,
    )

    # Normalize trailing newline (Jinja2 keep_trailing_newline may differ from golden files)
    assert rendered.rstrip("\n") == golden.rstrip("\n"), (
        f"Rendered output for {golden_name} does not match golden script. Template: {template_file}, vars: {extra_vars}"
    )
