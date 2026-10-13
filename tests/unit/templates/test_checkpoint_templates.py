# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: checkpoint on/off behaviour across all 4 PySpark templates."""

import ast
import os

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = (
    'df_transformed = df.withColumn("name", col("name").cast(StringType()))\n'
    'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")'
)

TEMPLATE_NAMES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
]

BASE_VARS = {
    "source_format": "json",
    "partition_columns": "",
    "write_mode": "overwrite",
    "merge_key": "",
}


@pytest.fixture
def jinja_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


# --- Checkpoint OFF ---


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_off_no_checkpoint_path(jinja_env, template_name):
    """When checkpoint_enabled=False, rendered code must NOT contain checkpoint_path."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=False,
        checkpoint_bucket="",
        **BASE_VARS,
    )
    assert "checkpoint_path" not in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_off_valid_python(jinja_env, template_name):
    """Checkpoint OFF renders syntactically valid Python."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=False,
        checkpoint_bucket="",
        **BASE_VARS,
    )
    ast.parse(rendered)


# --- Checkpoint ON ---


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_on_has_checkpoint_path(jinja_env, template_name):
    """When checkpoint_enabled=True, rendered code must contain checkpoint_path."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-checkpoint-bucket",
        **BASE_VARS,
    )
    assert "checkpoint_path" in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_on_has_spark_read_parquet(jinja_env, template_name):
    """When checkpoint_enabled=True, rendered code must contain spark.read.parquet."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-checkpoint-bucket",
        **BASE_VARS,
    )
    assert "spark.read.parquet" in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_on_has_bucket_name(jinja_env, template_name):
    """When checkpoint_enabled=True, rendered code must reference the bucket name."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-checkpoint-bucket",
        **BASE_VARS,
    )
    assert "my-checkpoint-bucket" in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_on_valid_python(jinja_env, template_name):
    """Checkpoint ON renders syntactically valid Python."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-checkpoint-bucket",
        **BASE_VARS,
    )
    ast.parse(rendered)


# --- Checkpoint ON: per-run path via getResolvedOptions (P28 R19.9) ---


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_path_includes_run_id_via_get_resolved_options(jinja_env, template_name):
    """R19.9: checkpoint path includes run_id resolved via getResolvedOptions."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-checkpoint-bucket",
        **BASE_VARS,
    )
    # The single optional-argument mechanism: getResolvedOptions gated on
    # sys.argv presence (manual job starts pass no Arguments).
    assert 'def _runtime_arg(_name, _default=""):' in rendered
    assert "getResolvedOptions(sys.argv, [_name])[_name]" in rendered
    assert '_adi_run_id = _runtime_arg("run_id")' in rendered
    # Manual-run fallback: Glue-provided JOB_RUN_ID (parsed by the top-level
    # getResolvedOptions call as a reserved optional argument).
    assert '_adi_effective_run_id = _adi_run_id or args.get("JOB_RUN_ID") or "unknown"' in rendered
    # The checkpoint location is per-run — never a shared 'default'.
    assert "checkpoints/{args['JOB_NAME']}/{_adi_effective_run_id}/" in rendered
    assert "args.get('run_id', 'default')" not in rendered


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_off_still_emits_runtime_arg_helper(jinja_env, template_name):
    """p37 R1.11: the runtime-arg helper renders even with checkpoint (and DQ) off.

    AMENDED, NOT DELETED. This assertion previously read
    ``assert "_runtime_arg" not in rendered`` under the name
    ``test_checkpoint_off_no_runtime_arg_helper``, and its docstring gave the
    reason: "Templates without checkpoint (or DQ) emit no *unused* runtime-arg
    helper." That premise was true under P28 R19.9 — nothing but the checkpoint
    path and the inline DQ block read the resolved run_id, so gating the
    include kept an unused helper out of the rendered job.

    p37 R1.1/R1.11 retires the premise: every generated job must record
    Rows_Read/Rows_Written on the ``(run_id, "load_metrics")`` state row for
    **every** (source_type, target_type) combination, so the resolved run_id
    and state table are read on every run. The helper is no longer unused, and
    the four ``{% if %}`` guards are gone from
    ``glue_job_s3_tables.py.j2``/``_glue_catalog``/``_jdbc``/``_s3_parquet``.
    ``glue_job_jdbc_source.py.j2`` has always included it unconditionally and
    is the existing proof this is safe: the partial's own sys.argv-presence
    gate is what keeps manual "start job" runs (which pass no Arguments)
    working.

    The reversal is amended in place rather than deleted so it stays legible as
    a reversal, following the convention ``LGR-4c4b4c9-6`` /
    ``LGR-60c4985-6`` (template / ratified) record for this repo: a deliberate
    hold encoded as a test assertion is changed on purpose, not removed.

    The broader render assertion — all **five** templates, regardless of
    ``checkpoint_enabled``, ``dqdl_rules`` and ``write_mode`` — belongs to p37
    task 3.4 and is not duplicated here.
    """
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=False,
        checkpoint_bucket="",
        **BASE_VARS,
    )
    assert 'def _runtime_arg(_name, _default=""):' in rendered
    assert '_adi_run_id = _runtime_arg("run_id")' in rendered


# --- Checkpoint ON: AnalysisException import ---


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
def test_checkpoint_on_imports_analysis_exception(jinja_env, template_name):
    """When checkpoint_enabled=True, rendered code must import AnalysisException."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        checkpoint_enabled=True,
        checkpoint_bucket="my-checkpoint-bucket",
        **BASE_VARS,
    )
    assert "from pyspark.sql.utils import AnalysisException" in rendered
    assert "except _AnalysisException:" in rendered
