# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Contract tests: for each of the 4 Jinja2 templates, render with mock data,
extract getResolvedOptions params, build DefaultArguments using the updated
build_*_arguments functions, and verify all required params are present.

Validates AC-2, AC-5, AC-7 from Phase 7 requirements.
"""

import importlib.util
import os
import re
import sys
import unittest.mock as mock

import jinja2
import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = (
    'df_transformed = df.withColumn("name", col("name").cast(StringType()))\n'
    'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")'
)

os.environ["MCP_SIGV4_ENABLED"] = "false"
os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

_mods = {
    "strands": mock.MagicMock(),
    "strands.agent": mock.MagicMock(),
    "strands.tools.mcp": mock.MagicMock(),
    "streamable_http_sigv4": mock.MagicMock(),
    "mcp": mock.MagicMock(),
    "mcp.client": mock.MagicMock(),
    "mcp.client.streamable_http": mock.MagicMock(),
    "schema_evolution": mock.MagicMock(),
    "metrics_facade": mock.MagicMock(),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock prompt"),
    ),
}


def _load_glue_pipeline_module():
    """Load create_glue_pipeline lambda_function by path to avoid name collision."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_glue_pipeline_lf",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["create_glue_pipeline_lf"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_glue_pipeline_module()
build_s3_table_bucket_arguments = _lf.build_s3_table_bucket_arguments
build_glue_catalog_arguments = _lf.build_glue_catalog_arguments
build_jdbc_arguments = _lf.build_jdbc_arguments
build_s3_parquet_arguments = _lf.build_s3_parquet_arguments
# `resolve_parameter_value` used to be imported here too. It was deleted with the
# JDBC-target credential fix: it had no production caller and resolved PLAINTEXT
# JDBC credentials from Secrets Manager inside the Lambda (`LGR-819980f-1`). Its
# `TestResolveParameterValue` class went with it; the per-target builders below
# are what actually produce the arguments.
validate_required_parameters = _lf.validate_required_parameters


def _render(name, extra=None):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    v = {
        "transform_block": SAMPLE_TRANSFORM,
        "source_format": "json",
        "partition_columns": "",
        "write_mode": "overwrite",
        "merge_key": "",
        "checkpoint_enabled": False,
        "checkpoint_bucket": "",
    }
    if extra:
        v.update(extra)
    return env.get_template(name).render(**v)


def _params(code):
    m = re.search(
        r"getResolvedOptions\s*\(\s*sys\.argv\s*,\s*\[(.*?)\]\s*\)",
        code,
        re.DOTALL,
    )
    assert m, "getResolvedOptions not found"
    return re.findall(r"'([^']+)'", m.group(1))


SRC = "s3://adi-processed-files-dev/pipe-abc/data.json"
WH = "s3://aws-s3-table-bucket-us-west-2-abc123/warehouse"

S3T_CFG = {
    "type": "NEW_S3_TABLE_BUCKET",
    "bucket_name": "my-table-bucket",
    "namespace_name": "my_namespace",
    "table_name": "my_table",
}
S3T_BUCKET = {
    "bucket_name": "my-table-bucket",
    "namespace": "my_namespace",
    "table_name": "my_table",
    "data_location": "s3://my-table-bucket/data",
    "iceberg_metadata_location": "s3://my-table-bucket/metadata",
}
GC_CFG = {"type": "GLUE_CATALOG", "database": "my_database", "table": "my_table", "format": "parquet"}
JDBC_CFG = {
    "type": "JDBC",
    "jdbc_connection_name": "my-conn",
    "database_name": "my_db",
    "table_name": "my_table",
    "jdbc_url": "jdbc:postgresql://host:5432/mydb",
    "jdbc_table": "public.my_table",
    "jdbc_driver": "org.postgresql.Driver",
    # The credential is a Secrets Manager reference. This fixture used to carry
    # `jdbc_user` / `jdbc_password`, matching a template that declared both; the
    # template now declares `secret_arn` instead (LGR-819980f-1).
    "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:contract-AbCdEf",
}
# A record written before the dashboard API refused the inline pair. Used only to
# show the builder ignores it.
JDBC_CFG_WITH_INLINE_PAIR = {
    **JDBC_CFG,
    "jdbc_user": "placeholder-user-not-a-credential",
    "jdbc_password": "placeholder-password-not-a-credential",
}
S3P_CFG = {"type": "S3_PARQUET", "output_path": "s3://my-output-bucket/output/"}


class TestS3TablesParameterContract:
    def test_all_params_present(self):
        args = build_s3_table_bucket_arguments(S3T_CFG, S3T_BUCKET, source_path=SRC, warehouse_location=WH)
        for p in _params(_render("glue_job_s3_tables.py.j2")):
            if p == "JOB_NAME":
                continue
            k = f"--{p}"
            assert k in args, f"Missing {k}"
            assert args[k], f"Empty {k}"

    def test_expected_params(self):
        assert _params(_render("glue_job_s3_tables.py.j2")) == [
            "JOB_NAME",
            "source_path",
            "table_bucket_arn",
            "namespace",
            "table_name",
        ]


class TestGlueCatalogParameterContract:
    def test_all_params_present(self):
        args = build_glue_catalog_arguments(GC_CFG, source_path=SRC)
        for p in _params(_render("glue_job_glue_catalog.py.j2")):
            if p == "JOB_NAME":
                continue
            assert f"--{p}" in args, f"Missing --{p}"
            assert args[f"--{p}"], f"Empty --{p}"

    def test_expected_params(self):
        assert _params(_render("glue_job_glue_catalog.py.j2")) == [
            "JOB_NAME",
            "source_path",
            "database_name",
            "table_name",
        ]


class TestJdbcParameterContract:
    def test_all_params_present(self):
        args = build_jdbc_arguments(JDBC_CFG, source_path=SRC)
        for p in _params(_render("glue_job_jdbc.py.j2")):
            if p == "JOB_NAME":
                continue
            assert f"--{p}" in args, f"Missing --{p}"
            assert args[f"--{p}"], f"Empty --{p}"

    def test_expected_params(self):
        # `secret_arn` replaces the `jdbc_user` / `jdbc_password` pair this list used
        # to end with: the write now resolves the credential from Secrets Manager at
        # run time by that reference (LGR-819980f-1).
        assert _params(_render("glue_job_jdbc.py.j2")) == [
            "JOB_NAME",
            "source_path",
            "jdbc_url",
            "jdbc_table",
            "jdbc_driver",
            "secret_arn",
        ]

    def test_every_emitted_template_key_is_declared(self):
        """The reverse direction of ``test_all_params_present``.

        ``getResolvedOptions`` ignores an undeclared argument, so an emitted key
        the template does not declare is dead weight — or, for a credential, a
        plaintext value in DefaultArguments that nothing even reads. The builder
        is given a record still carrying the inline pair; none of it may surface.
        """
        args = build_jdbc_arguments(JDBC_CFG_WITH_INLINE_PAIR, source_path=SRC)
        declared = set(_params(_render("glue_job_jdbc.py.j2")))
        # Template-matching keys are the underscore-spelled ones; the hyphenated
        # keys are operational metadata the generated code never reads.
        template_keys = {k[2:] for k in args if "-" not in k[2:]}
        assert template_keys <= declared, sorted(template_keys - declared)
        assert not [k for k in args if re.search(r"user|password", k)], sorted(args)
        assert JDBC_CFG_WITH_INLINE_PAIR["jdbc_password"] not in args.values()


class TestS3ParquetParameterContract:
    def test_all_params_present(self):
        args = build_s3_parquet_arguments(S3P_CFG, source_path=SRC)
        for p in _params(_render("glue_job_s3_parquet.py.j2", {"partition_columns": ""})):
            if p == "JOB_NAME":
                continue
            assert f"--{p}" in args, f"Missing --{p}"
            assert args[f"--{p}"], f"Empty --{p}"

    def test_expected_params(self):
        assert _params(_render("glue_job_s3_parquet.py.j2", {"partition_columns": ""})) == [
            "JOB_NAME",
            "source_path",
            "output_path",
        ]


class TestValidateRequiredParameters:
    def test_passes_when_all_present(self):
        a = {
            "--source_path": SRC,
            "--table_bucket_arn": "arn:x",
            "--namespace": "ns",
            "--table_name": "t",
            "--warehouse_location": WH,
        }
        validate_required_parameters(a, list(k.lstrip("-") for k in a), "S3_TABLE_BUCKET")

    def test_raises_on_missing(self):
        a = {"--namespace": "ns", "--table_name": "t", "--warehouse_location": WH, "--table_bucket_arn": "arn:x"}
        with pytest.raises(ValueError, match="source_path"):
            validate_required_parameters(
                a,
                ["source_path", "namespace", "table_name", "warehouse_location", "table_bucket_arn"],
                "S3_TABLE_BUCKET",
            )

    def test_raises_on_empty(self):
        a = {
            "--source_path": "",
            "--table_bucket_arn": "arn:x",
            "--namespace": "ns",
            "--table_name": "t",
            "--warehouse_location": WH,
        }
        with pytest.raises(ValueError, match="source_path"):
            validate_required_parameters(a, list(k.lstrip("-") for k in a), "S3_TABLE_BUCKET")
