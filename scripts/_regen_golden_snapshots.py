# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regenerate golden snapshots in tests/fixtures/golden_scripts/.

Runnable helper that mirrors the GOLDEN_CONFIGS map in
tests/unit/templates/test_golden_script_snapshot.py. Usage:

    uv run python scripts/_regen_golden_snapshots.py

Run after intentional changes to any of the PySpark j2 templates
(e.g. adding a new pyspark.sql.functions import). The diff that the
test surfaces on failure is the source of truth — re-running this
script makes the goldens reflect the new template output.

Keeping this in sync with the test is manual, and it has drifted before:
the env below must match the test's ``jinja_env`` fixture (including
``undefined=jinja2.StrictUndefined``, without which a template reading a
variable this script omits renders empty instead of failing loudly), and
GOLDEN_CONFIGS below must contain exactly the same keys with the same
render vars as the test's map. This script must not write any golden the
test does not compare — an unguarded fixture that only this script
produces can be rewritten with nobody noticing. When you change the
test's map or fixture, change this file in the same commit.

Underscore-prefixed because this is a maintenance helper, not part of
the runtime pipeline. Mirrors the existing
``_render_jdbc_golden_snapshots.py`` next to it.
"""

from __future__ import annotations

import os

import jinja2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(ROOT, "pyspark_templates")
GOLDEN_DIR = os.path.join(ROOT, "tests", "fixtures", "golden_scripts")

SAMPLE_TRANSFORM = "df_transformed = df"

GOLDEN_CONFIGS: dict[str, tuple[str, dict]] = {
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


def main() -> None:
    # Must match the jinja_env fixture in
    # tests/unit/templates/test_golden_script_snapshot.py.
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    for golden_name, (template_file, render_vars) in GOLDEN_CONFIGS.items():
        template = env.get_template(template_file)
        rendered = template.render(**render_vars)
        out_path = os.path.join(GOLDEN_DIR, golden_name)
        with open(out_path, "w") as f:
            f.write(rendered)
        print(f"Regenerated {golden_name}")


if __name__ == "__main__":
    main()
