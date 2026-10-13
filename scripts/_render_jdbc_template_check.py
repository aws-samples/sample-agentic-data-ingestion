# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Quick render check for glue_job_jdbc_source.py.j2.

Renders against several (engine, target_type, read_mode) combinations and
asserts each result parses as valid Python via ast.parse. Used as a manual
smoke check during template authoring (Task 10.3); not run in CI.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import jinja2

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "pyspark_templates"

env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
    undefined=jinja2.StrictUndefined,
    keep_trailing_newline=True,
)

SAMPLE_TRANSFORM = (
    'df_transformed = df.withColumn("name", col("name").cast(StringType()))\n'
    'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")'
)

CASES = [
    # (engine, target_type, read_mode, write_mode, partition_columns, initial_watermark, label)
    ("postgres", "S3_TABLES", "full_table", "overwrite", None, "", "pg_full_s3tables"),
    ("postgres", "S3_TABLES", "incremental", "merge", None, "2024-01-01T00:00:00Z", "pg_inc_s3tables_merge"),
    ("mysql", "GLUE_CATALOG", "full_table", "append", None, "", "mysql_full_gluecatalog"),
    ("mariadb", "JDBC", "incremental", "overwrite", None, "0", "mariadb_inc_jdbc"),
    ("oracle-ee", "RDS_DATABASE", "incremental", "append", None, "", "oracle_inc_rds"),
    ("sqlserver-ee", "S3_PARQUET", "full_table", "overwrite", None, "", "sqlserver_full_parquet"),
    ("sqlserver-ee", "S3_PARQUET", "incremental", "append", "['year', 'month']", "", "sqlserver_inc_parquet_partitioned"),
    ("aurora-postgresql", "S3_TABLES", "incremental", "append", None, "", "aurora_pg_inc_s3tables_append"),
]

template = env.get_template("glue_job_jdbc_source.py.j2")

# Per-case structural assertions to catch regressions before full unit tests.
def _assert_in(rendered: str, label: str, needle: str) -> None:
    assert needle in rendered, f"{label}: missing required substring {needle!r}"


def _assert_not_in(rendered: str, label: str, needle: str) -> None:
    assert needle not in rendered, f"{label}: unexpected substring {needle!r}"


failures = 0
for engine, target, mode, wmode, partitions, initial_wm, label in CASES:
    rendered = template.render(
        engine=engine,
        target_type=target,
        read_mode=mode,
        write_mode=wmode,
        partition_columns=partitions,
        merge_key="id",
        initial_watermark=initial_wm,
        transform_block=SAMPLE_TRANSFORM,
        dqdl_rules="",
    )
    try:
        ast.parse(rendered)

        # Engine-specific quoting baked at render time.
        _assert_in(rendered, label, f'quote_identifier("{engine}", args[\'jdbc_schema\'])')
        _assert_in(rendered, label, f'quote_identifier("{engine}", args[\'jdbc_table\'])')

        # Source read invocation.
        _assert_in(rendered, label, 'spark.read.format("jdbc")')

        # Secrets Manager runtime credential fetch.
        _assert_in(rendered, label, 'boto3.client("secretsmanager").get_secret_value')

        # Watermark-update conditional structure.
        if mode == "incremental":
            _assert_in(rendered, label, "df = df.cache()")
            _assert_in(rendered, label, "spark_max(col(args['watermark_column']))")
            _assert_in(rendered, label, "ConditionExpression='attribute_not_exists(last_updated_at) OR last_updated_at < :now'")
            _assert_in(rendered, label, "df.unpersist()")
            # Cache must precede MAX; MAX must precede transform_block; PutItem
            # must follow target write. The post-write `df.unpersist()` is the
            # LAST occurrence (an earlier one fires in the empty-source guard
            # branch); use rindex to pick it up.
            cache_idx = rendered.index("df = df.cache()")
            max_idx = rendered.index("spark_max(col(args['watermark_column']))")
            transform_idx = rendered.index("=== AI-Generated Transform Block ===")
            putitem_idx = rendered.index("_watermarks_table.put_item(")
            unpersist_idx = rendered.rindex("df.unpersist()")
            assert cache_idx < max_idx, f"{label}: df.cache() must precede MAX"
            assert max_idx < transform_idx, f"{label}: MAX must precede transform_block"
            assert transform_idx < putitem_idx, f"{label}: target write must precede put_item"
            assert putitem_idx < unpersist_idx, f"{label}: put_item must precede df.unpersist()"
        else:
            _assert_not_in(rendered, label, "df.cache()")
            # `spark_max` is imported at module top so it appears even in
            # full_table mode; the watermark-specific call site
            # `spark_max(col(args['watermark_column']))` does NOT.
            _assert_not_in(rendered, label, "spark_max(col(args['watermark_column']))")
            _assert_not_in(rendered, label, "_watermarks_table")
            _assert_not_in(rendered, label, "df.unpersist()")

        # Target-specific dispatch.
        if target == "S3_TABLES":
            _assert_in(rendered, label, "writeTo(target_table)")
        elif target == "GLUE_CATALOG":
            _assert_in(rendered, label, "glue_context.write_dynamic_frame.from_catalog")
        elif target in ("JDBC", "RDS_DATABASE"):
            _assert_in(rendered, label, "df_transformed.write.jdbc(")
            _assert_in(rendered, label, "args['target_jdbc_url']")
        elif target == "S3_PARQUET":
            _assert_in(rendered, label, ".parquet(output_path)")

        print(f"OK   {label}: {len(rendered)} chars, {rendered.count(chr(10))} lines")
    except (SyntaxError, AssertionError) as e:
        failures += 1
        print(f"FAIL {label}: {e}")
        if isinstance(e, SyntaxError):
            lines = rendered.split("\n")
            lo = max(0, (e.lineno or 1) - 5)
            hi = min(len(lines), (e.lineno or 1) + 5)
            for i, ln in enumerate(lines[lo:hi], start=lo + 1):
                marker = ">>>" if i == e.lineno else "   "
                print(f"  {marker} {i:4d} | {ln}")

if failures:
    sys.exit(failures)
print("\nAll render+parse+structural checks passed.")
