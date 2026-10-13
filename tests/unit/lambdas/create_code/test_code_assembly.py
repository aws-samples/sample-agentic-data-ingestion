# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: assembled script (template + transform) passes py_compile.compile()."""

import ast
import os
import py_compile
import tempfile

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "pyspark_templates")

# Realistic multi-line transform blocks of varying complexity
SIMPLE_TRANSFORM = 'df_transformed = df.select("id", "name")'

MEDIUM_TRANSFORM = """\
df_transformed = df.withColumn("name", col("name").cast(StringType()))
df_transformed = df_transformed.withColumn("amount", col("amount").cast(DoubleType()))
df_transformed = df_transformed.withColumnRenamed("old_id", "new_id")
df_transformed = df_transformed.filter(col("amount") > 0)"""

COMPLEX_TRANSFORM = """\
df_transformed = df.withColumn("full_name", concat_ws(" ", col("first_name"), col("last_name")))
df_transformed = df_transformed.withColumn("created_date", to_date(col("created_at")))
df_transformed = df_transformed.withColumn("amount", col("amount").cast(DoubleType()))
df_transformed = df_transformed.withColumn("is_active", when(col("status") == "active", True).otherwise(False))
df_transformed = df_transformed.withColumn("category", trim(col("category")))
df_transformed = df_transformed.drop("first_name", "last_name", "created_at", "status")
df_transformed = df_transformed.filter(col("amount") > 0)"""

TEMPLATES = {
    "glue_job_s3_tables.py.j2": {
        "source_format": "json",
        "partition_columns": "",
        "write_mode": "overwrite",
        "merge_key": "",
        "checkpoint_enabled": False,
        "checkpoint_bucket": "",
    },
    "glue_job_glue_catalog.py.j2": {
        "source_format": "json",
        "partition_columns": "",
        "write_mode": "overwrite",
        "merge_key": "",
        "checkpoint_enabled": False,
        "checkpoint_bucket": "",
    },
    "glue_job_jdbc.py.j2": {
        "source_format": "json",
        "write_mode": "overwrite",
        "merge_key": "",
        "checkpoint_enabled": False,
        "checkpoint_bucket": "",
    },
    "glue_job_s3_parquet.py.j2": {
        "source_format": "json",
        "partition_columns": "",
        "write_mode": "overwrite",
        "merge_key": "",
        "checkpoint_enabled": False,
        "checkpoint_bucket": "",
    },
}


@pytest.fixture
def jinja_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


@pytest.mark.parametrize("template_name,extra_vars", list(TEMPLATES.items()))
@pytest.mark.parametrize(
    "transform", [SIMPLE_TRANSFORM, MEDIUM_TRANSFORM, COMPLEX_TRANSFORM], ids=["simple", "medium", "complex"]
)
def test_assembled_script_compiles(jinja_env, template_name, extra_vars, transform):
    """Assembled script (template + transform block) must pass py_compile.compile()."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=transform, **extra_vars)

    # First check: AST parse
    ast.parse(rendered)

    # Second check: py_compile (catches additional issues)
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(rendered)
        f.flush()
        try:
            py_compile.compile(f.name, doraise=True)
        finally:
            os.unlink(f.name)


@pytest.mark.parametrize("template_name,extra_vars", list(TEMPLATES.items()))
def test_assembled_script_stays_a_flat_script(jinja_env, template_name, extra_vars):
    """The assembled script stays a flat script, not a framework (AC-18.5).

    AMENDED, not deleted, by p37 task 3.3, following the amend-in-place
    convention ``LGR-4c4b4c9-6`` / ``LGR-60c4985-6`` (template / ratified)
    record for this repo, and matching how p37 task 3.1 amended
    ``tests/unit/templates/test_checkpoint_templates.py``.

    This was ``test_assembled_script_under_120_lines`` asserting
    ``line_count < 160``. Note the name and the assertion had already drifted
    apart before p37 — AC-18.5 says 120, the assertion said 160 — so the
    number was already a soft budget rather than the criterion.

    **Why the number had to move.** p37 R1.1/R1.11 requires every generated
    job to persist Rows_Read/Rows_Written on the ``(run_id, "load_metrics")``
    state row, at TWO sites per template: inside the empty-source early exit
    (without which R1.3's ``empty_source`` is unrecordable — the job is gone
    before ``transformed_count`` exists) and after the write block (so
    ``write_confirmed`` means something, p37 risk N1). That is
    ``_partials/_load_counts.py.j2`` twice, ~96 rendered lines.

    **This is not a ceiling that could have been avoided by writing the block
    more tightly.** Measured under this test's own render vars,
    ``glue_job_s3_tables.py.j2`` is already 144 lines after task 3.1 made the
    ``_runtime_args`` include unconditional, so even a minimal ~15-line
    DynamoDB write at two sites lands near 174. No formulation of R1.1 keeps
    it under 160. Measured after 3.3: s3_tables 240, glue_catalog 216,
    jdbc 191, s3_parquet 185.

    **What still holds AC-18.5's actual intent.** The criterion is about the
    generated script being flat and readable rather than a wrapper framework;
    ``test_no_forbidden_classes`` below is the assertion that pins that
    directly, and ``test_assembled_script_compiles`` above pins correctness
    across all three transform complexities. The line count is a proxy, and it
    is kept — at a measured ceiling, so a future regression is still visible —
    rather than removed.
    """
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=MEDIUM_TRANSFORM, **extra_vars)
    line_count = len(rendered.strip().splitlines())
    assert line_count < 260, f"{template_name} rendered {line_count} lines (limit: 260)"


@pytest.mark.parametrize("template_name,extra_vars", list(TEMPLATES.items()))
def test_no_forbidden_classes(jinja_env, template_name, extra_vars):
    """Template output must not contain forbidden wrapper classes (AC-18.6)."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=MEDIUM_TRANSFORM, **extra_vars)
    forbidden = [
        "GlueJobMetrics",
        "DataQualityValidator",
        "DataTransformer",
        "S3DataReader",
        "S3DataWriter",
        "GlueETLJob",
    ]
    for cls in forbidden:
        assert cls not in rendered, f"Forbidden class '{cls}' found in {template_name}"
