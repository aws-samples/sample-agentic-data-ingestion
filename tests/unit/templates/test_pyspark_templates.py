# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: each Jinja2 PySpark template renders valid Python."""

import ast
import os
import re

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = (
    'df_transformed = df.withColumn("name", col("name").cast(StringType()))\n'
    'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")'
)

TEMPLATE_CONFIGS = {
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


@pytest.mark.parametrize("template_name,extra_vars", list(TEMPLATE_CONFIGS.items()))
def test_template_renders_valid_python(jinja_env, template_name, extra_vars):
    """Each template should render syntactically valid Python."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, **extra_vars)
    # Must parse without SyntaxError
    ast.parse(rendered)


@pytest.mark.parametrize("template_name,extra_vars", list(TEMPLATE_CONFIGS.items()))
def test_template_contains_required_sections(jinja_env, template_name, extra_vars):
    """Each template must include imports, GlueContext init, transform block, and error handling."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, **extra_vars)

    assert "import sys" in rendered
    assert "GlueContext" in rendered
    assert "job.init(" in rendered
    assert "df_transformed" in rendered
    assert "except Exception" in rendered
    assert "job.commit()" in rendered


@pytest.mark.parametrize("template_name,extra_vars", list(TEMPLATE_CONFIGS.items()))
def test_template_has_input_validation(jinja_env, template_name, extra_vars):
    """Each template must validate source_path starts with s3://."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, **extra_vars)
    assert "startswith('s3://')" in rendered


@pytest.mark.parametrize("template_name,extra_vars", list(TEMPLATE_CONFIGS.items()))
def test_template_has_empty_data_guard(jinja_env, template_name, extra_vars):
    """Each template must handle empty source data gracefully."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, **extra_vars)
    assert "source_count == 0" in rendered or "count() == 0" in rendered


@pytest.mark.parametrize("template_name,extra_vars", list(TEMPLATE_CONFIGS.items()))
def test_template_logs_record_counts(jinja_env, template_name, extra_vars):
    """Each template must log source and transformed record counts."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, **extra_vars)
    assert "source_count" in rendered or "Source record count" in rendered
    assert "transformed_count" in rendered or "Transformed record count" in rendered


@pytest.mark.parametrize("template_name,extra_vars", list(TEMPLATE_CONFIGS.items()))
def test_template_imports_lower(jinja_env, template_name, extra_vars):
    """Each template must import lower() since AI transform blocks commonly use it."""
    template = jinja_env.get_template(template_name)
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, **extra_vars)
    assert "lower" in rendered.split("# Initialize")[0]  # In the imports section


@pytest.mark.parametrize("source_format", ["json", "csv", "parquet"])
def test_s3_tables_source_format_variants(jinja_env, source_format):
    """S3 Tables template should handle all three source formats."""
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format=source_format,
        partition_columns="",
        # p55 R4.1: the csv branch reads `.option("sep", …)` from this key and
        # the env is StrictUndefined, so it is supplied on every render here —
        # the same posture `code_assembly.generate_code` takes.
        source_delimiter=",",
    )
    ast.parse(rendered)
    if source_format == "csv":
        assert "header" in rendered
    elif source_format == "parquet":
        assert ".parquet(" in rendered
    else:
        assert "multiLine" in rendered


def test_s3_tables_has_iceberg_config(jinja_env):
    """S3 Tables template must configure Iceberg catalog."""
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, source_format="json", partition_columns="")
    assert "S3TablesCatalog" in rendered
    assert "writeTo" in rendered


def test_s3_tables_has_record_count_reconciliation(jinja_env):
    """S3 Tables template must verify written record count matches transformed count."""
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, source_format="json", partition_columns="")
    assert "output_count != transformed_count" in rendered or "count mismatch" in rendered.lower()


def test_glue_catalog_uses_dynamic_frame(jinja_env):
    """Glue Catalog template must use write_dynamic_frame.from_catalog."""
    template = jinja_env.get_template("glue_job_glue_catalog.py.j2")
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, source_format="json", partition_columns="")
    assert "write_dynamic_frame" in rendered
    assert "from_catalog" in rendered


def test_jdbc_uses_write_jdbc(jinja_env):
    """JDBC template must use df.write.jdbc()."""
    template = jinja_env.get_template("glue_job_jdbc.py.j2")
    rendered = template.render(transform_block=SAMPLE_TRANSFORM, source_format="json")
    assert ".write.jdbc(" in rendered


def test_s3_parquet_with_partition_columns(jinja_env):
    """S3 Parquet template should support partition columns."""
    template = jinja_env.get_template("glue_job_s3_parquet.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns='"year", "month"',
    )
    ast.parse(rendered)
    assert "partitionBy" in rendered


def test_s3_parquet_without_partition_columns(jinja_env):
    """S3 Parquet template should work without partition columns."""
    template = jinja_env.get_template("glue_job_s3_parquet.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns="",
    )
    ast.parse(rendered)
    assert "partitionBy" not in rendered


def test_s3_parquet_validates_output_path(jinja_env):
    """S3 Parquet template must validate output_path."""
    template = jinja_env.get_template("glue_job_s3_parquet.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns="",
    )
    assert "output_path" in rendered
    assert "startswith('s3://')" in rendered


def test_s3_tables_with_partition_columns(jinja_env):
    """S3 Tables template should support partition columns via Iceberg partitionedBy."""
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns='"year", "month"',
    )
    ast.parse(rendered)
    assert "partitionedBy" in rendered


def test_s3_tables_without_partition_columns(jinja_env):
    """S3 Tables template should work without partition columns."""
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns="",
    )
    ast.parse(rendered)
    assert "partitionedBy" not in rendered


def test_glue_catalog_with_partition_columns(jinja_env):
    """Glue Catalog template should repartition DataFrame when partition columns are set."""
    template = jinja_env.get_template("glue_job_glue_catalog.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns='"year", "month"',
    )
    ast.parse(rendered)
    assert "repartition" in rendered


def test_glue_catalog_without_partition_columns(jinja_env):
    """Glue Catalog template should work without partition columns."""
    template = jinja_env.get_template("glue_job_glue_catalog.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns="",
    )
    ast.parse(rendered)
    assert "repartition" not in rendered


# --- Write-mode tests (Phase 10: Data Integrity) ---


def test_s3_tables_merge_mode(jinja_env):
    """S3 Tables template with write_mode='merge' should produce MERGE INTO with merge_key from args."""
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns="",
        write_mode="merge",
        merge_key="customer_id",
    )
    ast.parse(rendered)
    assert "MERGE INTO" in rendered
    # P28 task 17.2 live-caught bug: `args.get('merge_key', 'id')` could never
    # see the --merge_key job argument (not in the top-level
    # getResolvedOptions list), silently merging on 'id'. The sink must
    # resolve the runtime argument via _runtime_arg with the validated
    # template value as the default.
    assert '_runtime_arg("merge_key", "customer_id")' in rendered
    assert "args.get('merge_key'" not in rendered
    # merge mode must pull in the _runtime_arg mechanism even without
    # checkpoints/DQ rules (the include is conditional)
    assert "def _runtime_arg(" in rendered


def test_s3_tables_append_mode(jinja_env):
    """S3 Tables template with write_mode='append' should use .append() with TABLE_OR_VIEW_NOT_FOUND fallback."""
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns="",
        write_mode="append",
        merge_key="",
    )
    ast.parse(rendered)
    assert ".append()" in rendered
    # createOrReplace is used as fallback when table not yet in Spark catalog
    assert "TABLE_OR_VIEW_NOT_FOUND" in rendered


def test_s3_parquet_append_mode(jinja_env):
    """S3 Parquet template with write_mode='append' should use mode("append")."""
    template = jinja_env.get_template("glue_job_s3_parquet.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns="",
        write_mode="append",
        merge_key="",
    )
    ast.parse(rendered)
    assert 'mode("append")' in rendered


def test_glue_catalog_append_mode(jinja_env):
    """Glue Catalog template with write_mode='append' should include enableUpdateCatalog."""
    template = jinja_env.get_template("glue_job_glue_catalog.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        partition_columns="",
        write_mode="append",
        merge_key="",
    )
    ast.parse(rendered)
    assert "enableUpdateCatalog" in rendered


def test_jdbc_overwrite_mode(jinja_env):
    """JDBC template with write_mode='overwrite' should use mode="overwrite"."""
    template = jinja_env.get_template("glue_job_jdbc.py.j2")
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="json",
        write_mode="overwrite",
        merge_key="",
    )
    ast.parse(rendered)
    assert 'mode="overwrite"' in rendered


# ---------------------------------------------------------------------------
# JDBC-target credential path — standing invariant LGR-819980f-1
# ---------------------------------------------------------------------------

# Every JDBC-target render. `target_secret_arn` is varied although no template
# reads it any more: code_assembly still sets it, and before the fix it was the
# switch that selected the plaintext arm, so both values must now render the same
# credential path. RDS_DATABASE is the legacy alias that shares the JDBC write.
_TARGET_SECRET = "arn:aws:secretsmanager:us-west-2:111122223333:secret:tgt-AbCdEf"
_JDBC_TARGET_RENDERS = [
    pytest.param(
        "glue_job_jdbc.py.j2",
        "",
        {"source_format": "json", "write_mode": write_mode, "merge_key": "", "target_secret_arn": secret},
        id=f"glue_job_jdbc-{write_mode}-secret={bool(secret)}",
    )
    for write_mode in ("overwrite", "append")
    for secret in (None, _TARGET_SECRET)
] + [
    pytest.param(
        "glue_job_jdbc_source.py.j2",
        "target_",
        {
            "engine": "postgres",
            "target_type": target_type,
            "read_mode": "full_table",
            "write_mode": write_mode,
            "partition_columns": "",
            "merge_key": "id",
            "initial_watermark": "",
            "target_secret_arn": secret,
        },
        id=f"glue_job_jdbc_source-{target_type}-{write_mode}-secret={bool(secret)}",
    )
    for target_type in ("JDBC", "RDS_DATABASE")
    for write_mode in ("overwrite", "append")
    for secret in (None, _TARGET_SECRET)
]

_CREDENTIAL_NAME = r"user|password|passwd|pwd"


def _declared_options(tree: ast.Module) -> list[str]:
    """The string keys of the script's ``getResolvedOptions(sys.argv, [...])`` list."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getResolvedOptions":
            return [elt.value for elt in node.args[1].elts if isinstance(elt, ast.Constant)]
    raise AssertionError("no getResolvedOptions call in the rendered script")


def _args_subscripts(tree: ast.Module) -> list[str]:
    """Every literal key read as ``args['<key>']`` anywhere in the script."""
    return [
        node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "args"
        and isinstance(node.slice, ast.Constant)
    ]


def _secret_fetches(tree: ast.Module) -> list[str]:
    """The ``args[...]`` key passed as ``SecretId`` to each ``get_secret_value`` call."""
    keys = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "get_secret_value":
            for kw in node.keywords:
                if kw.arg == "SecretId" and isinstance(kw.value, ast.Subscript):
                    keys.append(kw.value.slice.value)
    return keys


@pytest.mark.parametrize(("template_name", "key_prefix", "render_vars"), _JDBC_TARGET_RENDERS)
def test_jdbc_target_write_fetches_from_secrets_manager_and_reads_no_plaintext_arg(
    jinja_env, template_name, key_prefix, render_vars
):
    """Every JDBC-target script resolves its credential with get_secret_value at run time.

    Parsed rather than grepped, so a comment that names a token cannot satisfy or
    fail an assertion. Three halves of one contract with ``build_jdbc_arguments``,
    which emits ``--{prefix}secret_arn`` for every JDBC target and never a
    credential: the script declares that key, passes it to ``get_secret_value``,
    and neither declares nor reads a ``*jdbc_user`` / ``*jdbc_password`` argument.
    """
    rendered = jinja_env.get_template(template_name).render(transform_block=SAMPLE_TRANSFORM, **render_vars)
    tree = ast.parse(rendered)

    declared = _declared_options(tree)
    assert f"{key_prefix}secret_arn" in declared, declared
    assert not [name for name in declared if re.search(_CREDENTIAL_NAME, name)], declared

    assert f"{key_prefix}secret_arn" in _secret_fetches(tree), (
        f"the JDBC target write must call get_secret_value(SecretId=args['{key_prefix}secret_arn'])"
    )
    read = _args_subscripts(tree)
    assert not [key for key in read if re.search(_CREDENTIAL_NAME, key)], (
        f"the script reads a plaintext credential job argument: {read}"
    )
    # The write's properties take the password from the fetched secret.
    assert '"password": _target_secret["password"]' in rendered
    # The shared partial uses json/boto3 from module scope; both must be imported.
    imported = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    assert {"json", "boto3"} <= imported, imported
