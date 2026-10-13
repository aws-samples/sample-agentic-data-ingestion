# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test: end-to-end code generation for each target type.

Verifies the rendered script contains the correct import block, source read,
transform, and target write sections for each target type.
"""

import ast
import os
import sys
import unittest.mock as mock

import pytest

# Add Lambda dir and shared to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Patch heavy dependencies before importing
_mods = {
    "strands": mock.MagicMock(),
    "strands_mcp_factory": mock.MagicMock(),
    "agentcore_client": mock.MagicMock(),
    "eventbridge_utils": mock.MagicMock(
        get_eventbridge_publishers=mock.MagicMock(return_value=(mock.MagicMock(), mock.MagicMock(), mock.MagicMock())),
    ),
    "code_validator": mock.MagicMock(
        validate_generated_code=mock.MagicMock(return_value={"is_valid": True, "errors": [], "summary": {}}),
        extract_code_only=mock.MagicMock(side_effect=lambda x: x),
    ),
    "env_utils": mock.MagicMock(
        get_aws_account_id=mock.MagicMock(return_value="123456789012"),
        require_env=mock.MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "parameter_extractor": mock.MagicMock(),
}

with mock.patch.dict("sys.modules", _mods):
    # Remove cached lambda_function from other test files to force fresh import
    sys.modules.pop("lambda_function", None)
    import lambda_function as lf

MOCK_TRANSFORM = """\
df_transformed = df.withColumn("full_name", trim(col("name")))
df_transformed = df_transformed.withColumn("amount", col("amount").cast(DoubleType()))
df_transformed = df_transformed.drop("name")"""

SAMPLE_MAPPINGS = [
    {"source_field": "id", "source_type": "integer", "target_field": "id", "target_type": "integer", "transform": None},
    {
        "source_field": "name",
        "source_type": "string",
        "target_field": "full_name",
        "target_type": "string",
        "transform": "trim",
    },
    {
        "source_field": "amount",
        "source_type": "string",
        "target_field": "amount",
        "target_type": "double",
        "transform": None,
    },
]

TARGET_CONFIGS = {
    "s3_tables": {
        "type": "s3_tables",
        "target_type": "s3_tables",
        "source_format": "json",
        "bucket_name": "my-bucket",
        "namespace_name": "my_ns",
        "table_name": "my_table",
        "warehouse_location": "s3://my-bucket/warehouse",
    },
    "glue_catalog": {
        "type": "glue_catalog",
        "target_type": "glue_catalog",
        "source_format": "csv",
        "database_name": "my_db",
        "table_name": "my_table",
    },
    "jdbc": {
        "type": "jdbc",
        "target_type": "jdbc",
        "source_format": "json",
        "jdbc_url": "jdbc:postgresql://host:5432/db",
        "jdbc_table": "public.my_table",
        "jdbc_driver": "org.postgresql.Driver",
    },
    "s3_parquet": {
        "type": "s3_parquet",
        "target_type": "s3_parquet",
        "source_format": "parquet",
        "output_path": "s3://output-bucket/data/",
        "partition_columns": "",
    },
}


@pytest.fixture(autouse=True)
def mock_ai_transform(monkeypatch):
    """Mock the AI call so tests are deterministic and don't call Bedrock."""
    monkeypatch.setattr(lf, "generate_transform_with_ai", lambda **kw: MOCK_TRANSFORM)


def _generate(target_type: str) -> str:
    config = TARGET_CONFIGS[target_type]
    pipeline_metadata = {
        "target_config": config,
        # Every mapped source field must be declared: `generate_code` refuses a
        # mapping that names a column the source schema lacks
        # (`code_assembly._validate_mapped_source_fields`). `amount` is mapped
        # above and was missing here, which failed every render in this module.
        "source_schema": {
            "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "amount": {"type": "string"}}
        },
    }
    return lf.generate_code(pipeline_metadata, {"field_mappings": SAMPLE_MAPPINGS})


@pytest.mark.parametrize("target_type", list(TARGET_CONFIGS.keys()))
def test_e2e_generates_valid_python(target_type):
    """Generated script must be syntactically valid Python."""
    code = _generate(target_type)
    ast.parse(code)


@pytest.mark.parametrize("target_type", list(TARGET_CONFIGS.keys()))
def test_e2e_contains_imports(target_type):
    code = _generate(target_type)
    assert "import sys" in code
    assert "from awsglue" in code
    assert "GlueContext" in code


@pytest.mark.parametrize("target_type", list(TARGET_CONFIGS.keys()))
def test_e2e_contains_transform_block(target_type):
    code = _generate(target_type)
    assert "df_transformed" in code
    assert "AI-Generated Transform Block" in code


def test_s3_tables_has_iceberg_write():
    code = _generate("s3_tables")
    assert "writeTo" in code
    assert "S3TablesCatalog" in code


def test_glue_catalog_has_dynamic_frame_write():
    code = _generate("glue_catalog")
    assert "write_dynamic_frame" in code
    assert "from_catalog" in code


def test_glue_catalog_uses_csv_reader():
    """Glue catalog config has source_format=csv, should use csv reader."""
    code = _generate("glue_catalog")
    assert ".csv(" in code
    assert "header" in code


def test_jdbc_has_write_jdbc():
    code = _generate("jdbc")
    assert ".write.jdbc(" in code


def test_s3_parquet_has_parquet_write():
    code = _generate("s3_parquet")
    assert ".parquet(" in code


def test_s3_parquet_uses_parquet_reader():
    """S3 parquet config has source_format=parquet, should use parquet reader."""
    code = _generate("s3_parquet")
    assert ".read.parquet(" in code


# ---------------------------------------------------------------------------
# Harness fence: this module must render with the SAME jinja2 production uses
# ---------------------------------------------------------------------------


def test_render_path_holds_a_single_jinja2_copy():
    """``code_assembly``'s jinja2 must be the live ``sys.modules`` one.

    ``mock.patch.dict("sys.modules", _mods)`` above does not merely restore the
    keys it overrode on exit — it clears ``sys.modules`` and re-applies its
    entry snapshot, evicting every module first imported inside the block.
    ``lambda_function`` → ``code_assembly`` → ``import jinja2`` happens inside
    it, so unless jinja2 is already loaded this module ends up rendering with a
    jinja2 copy that ``sys.modules`` no longer holds, while the compiled
    template's own ``from jinja2.runtime import ... missing`` resolves to a
    second copy.

    Two copies means two ``missing`` sentinels, and jinja2's generated code
    decides "is this variable undefined?" with ``x is missing``. That test is
    always False across copies, so undefined variables reach filters as a raw
    ``_MissingType`` rather than as an ``Undefined``. Measured fallout in this
    very module: ``indent()`` received the sentinel as its width and all 18
    tests failed with "can't multiply sequence by non-int of type
    '_MissingType'", and — silently, before that — ``checkpoint_enabled |
    default(false)`` evaluated TRUE, so every script asserted here carried a
    checkpoint branch writing to ``s3://missing/checkpoints/``.

    ``tests/conftest.py`` imports jinja2 for exactly this reason, which puts it
    in the entry snapshot of every patch.dict block in the suite. This test is
    the fence on that line: if it is removed, this fails here rather than
    resurfacing as a render error nobody can place.
    """
    assert lf.code_assembly.jinja2 is sys.modules["jinja2"], (
        "code_assembly is bound to a jinja2 copy that sys.modules no longer holds — "
        "see tests/conftest.py's jinja2 module-identity guard"
    )


@pytest.mark.parametrize("target_type", list(TARGET_CONFIGS.keys()))
def test_e2e_omits_checkpoint_branch_when_checkpointing_is_off(target_type):
    """No render context here enables checkpointing, so no script may carry it.

    The companion assertion to the module-identity fence above, aimed at the
    *silent* half of that defect: a wrong ``missing`` identity made
    ``checkpoint_enabled | default(false)`` truthy, and nothing in this module
    noticed because every existing assertion here is a positive one ("contains
    imports", "has iceberg write"). A checkpoint block that production never
    emits was being validated as end-to-end output.
    """
    code = _generate(target_type)
    assert "Resumed from checkpoint" not in code
    assert "s3://missing/" not in code
