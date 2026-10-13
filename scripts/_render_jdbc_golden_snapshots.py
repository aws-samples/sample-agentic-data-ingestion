# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One-shot renderer for the JDBC source golden snapshots (Task 10.7).

Renders ``glue_job_jdbc_source.py.j2`` against the five canonical
(engine × target × mode × write_mode) combinations specified in
``.kiro/specs/p17-jdbc-data-source/tasks.md`` Task 10.7 and writes each result
to ``tests/fixtures/golden_scripts/``.

Each rendered output is also parsed via :func:`ast.parse` to guarantee the
snapshot is syntactically valid Python before being committed — a regression
in the template will surface here rather than in CI.

Run from the repo root with: ``python scripts/_render_jdbc_golden_snapshots.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import jinja2

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "pyspark_templates"
GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "golden_scripts"

# Match the SAMPLE_TRANSFORM used by the existing golden snapshots (see
# ``tests/unit/templates/test_golden_script_snapshot.py``) so the new snapshots
# share the same transform-block contents.
SAMPLE_TRANSFORM = "df_transformed = df"


# (golden_filename, render_vars). Each render_vars dict supplies the FULL
# variable surface required by the new JDBC-source template under
# StrictUndefined — no shared baseline is assumed.
SNAPSHOTS: list[tuple[str, dict[str, object]]] = [
    # 1. Postgres → S3 Tables (incremental + timestamp watermark + append).
    #    Exercises the watermark code path against the most common target.
    (
        "jdbc_source_postgres_s3_tables.py",
        {
            "engine": "postgres",
            "target_type": "S3_TABLES",
            "read_mode": "incremental",
            "write_mode": "append",
            "initial_watermark": "2024-01-01T00:00:00Z",
            "partition_columns": "",
            "merge_key": "id",
            "transform_block": SAMPLE_TRANSFORM,
        },
    ),
    # 2. Postgres → JDBC (full_table + overwrite).
    #    Demonstrates the ``target_`` key prefix mechanism — the rendered code
    #    reads ``args['jdbc_url']`` (source) AND ``args['target_jdbc_url']``
    #    (target) without collision.
    (
        "jdbc_source_postgres_jdbc.py",
        {
            "engine": "postgres",
            "target_type": "JDBC",
            "read_mode": "full_table",
            "write_mode": "overwrite",
            "initial_watermark": "",
            "partition_columns": "",
            "merge_key": "id",
            "transform_block": SAMPLE_TRANSFORM,
        },
    ),
    # 3. MySQL → Glue Catalog (full_table + append).
    #    The ``mysql`` engine string is baked into the ``quote_identifier``
    #    call sites; the runtime backtick-vs-double-quote dispatch happens in
    #    ``jdbc_helpers.py`` and is exercised in ``test_jdbc_helpers``.
    (
        "jdbc_source_mysql_glue_catalog.py",
        {
            "engine": "mysql",
            "target_type": "GLUE_CATALOG",
            "read_mode": "full_table",
            "write_mode": "append",
            "initial_watermark": "",
            "partition_columns": "",
            "merge_key": "id",
            "transform_block": SAMPLE_TRANSFORM,
        },
    ),
    # 4. Oracle → S3 Parquet (full_table + overwrite, no partitions).
    #    The ``oracle-ee`` engine string is baked into the rendered output;
    #    the ``oracle:thin:@`` URL protocol is selected at runtime in
    #    ``jdbc_helpers.build_jdbc_url``.
    (
        "jdbc_source_oracle_s3_parquet.py",
        {
            "engine": "oracle-ee",
            "target_type": "S3_PARQUET",
            "read_mode": "full_table",
            "write_mode": "overwrite",
            "initial_watermark": "",
            "partition_columns": "",
            "merge_key": "id",
            "transform_block": SAMPLE_TRANSFORM,
        },
    ),
    # 5. SQL Server → JDBC (full_table + overwrite).
    #    Companion to (2): same target shape but with the ``sqlserver-ee``
    #    engine string baked in. Exercises the SQL Server family classification
    #    end-to-end.
    (
        "jdbc_source_sqlserver_jdbc.py",
        {
            "engine": "sqlserver-ee",
            "target_type": "JDBC",
            "read_mode": "full_table",
            "write_mode": "overwrite",
            "initial_watermark": "",
            "partition_columns": "",
            "merge_key": "id",
            "transform_block": SAMPLE_TRANSFORM,
        },
    ),
]


def main() -> None:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    template = env.get_template("glue_job_jdbc_source.py.j2")

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    for filename, render_vars in SNAPSHOTS:
        rendered = template.render(**render_vars)
        # Parse-check before writing — a syntactically broken render would
        # poison the snapshot and the matching test would still pass.
        ast.parse(rendered)
        out_path = GOLDEN_DIR / filename
        out_path.write_text(rendered)
        print(f"wrote {out_path.relative_to(REPO_ROOT)} ({len(rendered)} chars)")


if __name__ == "__main__":
    main()
