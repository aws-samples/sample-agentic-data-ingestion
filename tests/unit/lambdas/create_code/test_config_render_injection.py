# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Configuration values the job templates emit as code are validated before the render.

``generate_code`` merges ``source_config`` and ``target_config`` into the render
context, and three configured values reach a template as Python source:

* ``partition_columns``, spliced into ``partitionBy(...)``. An external review
  rendered ``__import__("os").getcwd()`` there as a call.
* ``checkpoint_bucket``, inside an f-string, when ``checkpoint_enabled`` is set.
* ``engine``, inside a quoted literal in ``glue_job_jdbc_source.py.j2``.

Expectations come from the requirement, not the output: a hostile value fails
code generation before the AI call is spent, and a valid one renders as data.
The structural sweep over every template output is
``tests/unit/templates/test_template_config_render_surface.py``.
"""

import ast
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CREATE_CODE_DIR = _REPO_ROOT / "lambda" / "agentic_data_ingestion_create_code"
for _path in (_CREATE_CODE_DIR, _REPO_ROOT / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from identifier_utils import IdentifierValidationError

_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "parameter_extractor": MagicMock(),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _lambda_function

import code_assembly

SAFE_BLOCK = 'df_transformed = df.withColumnRenamed("cust_id", "customer_id")'
_MAPPING_RESULT = {"field_mappings": [{"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.9}]}

#: Each would be code in the job if emitted unescaped.
HOSTILE_COLUMNS = [
    pytest.param('__import__("os").getcwd()', id="python-call"),
    pytest.param('region"); import os; ("', id="quote-break-out"),
    pytest.param(["region", "__import__('os').getcwd()"], id="list-entry-call"),
    pytest.param("region,,dt", id="empty-entry"),
]

HOSTILE_BUCKETS = [
    pytest.param('ckpt/{__import__("os").getcwd()}', id="f-string-field"),
    pytest.param('ckpt"; import os; x = "', id="quote-break-out"),
    pytest.param("", id="missing"),
    pytest.param("Upper_Case", id="not-an-s3-name"),
]


@pytest.fixture
def _lambda_module():
    sys.modules["lambda_function"] = _lambda_function
    yield _lambda_function


def _metadata(**target_overrides: object) -> dict:
    target_config = {
        "type": "s3_tables",
        "bucket_name": "my-table-bucket",
        "namespace": "analytics",
        "table_name": "customers",
        "write_mode": "append",
        "source_format": "json",
        **target_overrides,
    }
    return {
        "source_type": "S3_UPLOAD",
        "source_config": {},
        "target_config": target_config,
        "source_schema": {"properties": {}},
    }


def _generate(module, metadata: dict) -> tuple[str, MagicMock]:
    ai = MagicMock(return_value=SAFE_BLOCK)
    with patch.object(module, "generate_transform_with_ai", ai):
        code = module.generate_code(pipeline_metadata=metadata, mapping_result=_MAPPING_RESULT)
    return code, ai


def _calls_import(code: str) -> bool:
    return any(isinstance(node, ast.Name) and node.id == "__import__" for node in ast.walk(ast.parse(code)))


@pytest.mark.parametrize("columns", HOSTILE_COLUMNS)
def test_hostile_partition_columns_fail_before_the_ai_call(_lambda_module, columns):
    ai = MagicMock(return_value=SAFE_BLOCK)
    with (
        patch.object(_lambda_module, "generate_transform_with_ai", ai),
        pytest.raises(IdentifierValidationError, match="partition columns"),
    ):
        _lambda_module.generate_code(
            pipeline_metadata=_metadata(partition_columns=columns), mapping_result=_MAPPING_RESULT
        )

    ai.assert_not_called()


@pytest.mark.parametrize(
    "columns", [pytest.param("region, dt", id="comma-string"), pytest.param(["region", "dt"], id="list")]
)
def test_partition_columns_render_as_string_literals(_lambda_module, columns):
    code, _ = _generate(_lambda_module, _metadata(partition_columns=columns))

    assert '.partitionedBy("region", "dt")' in code
    assert not _calls_import(code)


def test_no_partition_columns_render_no_partitioning(_lambda_module):
    code, _ = _generate(_lambda_module, _metadata())

    assert "partitionedBy" not in code


@pytest.mark.parametrize("bucket", HOSTILE_BUCKETS)
def test_an_invalid_checkpoint_bucket_fails_before_the_ai_call(_lambda_module, bucket):
    ai = MagicMock(return_value=SAFE_BLOCK)
    with (
        patch.object(_lambda_module, "generate_transform_with_ai", ai),
        pytest.raises(ValueError, match="checkpoint_bucket"),
    ):
        _lambda_module.generate_code(
            pipeline_metadata=_metadata(checkpoint_enabled=True, checkpoint_bucket=bucket),
            mapping_result=_MAPPING_RESULT,
        )

    ai.assert_not_called()


def test_a_valid_checkpoint_bucket_renders_into_the_path(_lambda_module):
    code, _ = _generate(_lambda_module, _metadata(checkpoint_enabled=True, checkpoint_bucket="adi-checkpoints-1"))

    assert 'checkpoint_path = f"s3://adi-checkpoints-1/checkpoints/' in code


def test_a_checkpoint_bucket_is_ignored_while_checkpointing_is_off(_lambda_module):
    """The templates read the bucket only under `{% if checkpoint_enabled %}`, so
    an unusable value with checkpointing off fails nothing and renders nowhere."""
    code, _ = _generate(_lambda_module, _metadata(checkpoint_bucket='x"; import os; y = "'))

    assert "import os; y" not in code


def _jdbc_source_context(engine: str) -> dict:
    """The JDBC source template's render surface, as `test_dq_template_vars_contract` builds it."""
    return {
        "engine": engine,
        "target_type": "S3_TABLES",
        "read_mode": "incremental",
        "write_mode": "append",
        "initial_watermark": "2024-01-01T00:00:00Z",
        "partition_columns": "",
        "merge_key": "id",
        "transform_block": "df_transformed = df",
        "dqdl_rules": "Rules = [\n    RowCount > 0\n]",
        "quarantine_bucket": "adi-quarantine-bucket",
        "data_quality_enabled": True,
    }


def _render_jdbc_source(engine: str) -> str:
    template = code_assembly._get_pyspark_jinja_env().get_template("glue_job_jdbc_source.py.j2")
    return template.render(**_jdbc_source_context(engine))


def test_the_engine_renders_as_a_string_even_when_it_is_code():
    code = _render_jdbc_source('postgres", __import__("os").getcwd(), "')

    assert not _calls_import(code)


def test_a_supported_engine_renders_exactly_as_before():
    """`tojson` gives the same double-quoted literal the template used to write by hand."""
    code = _render_jdbc_source("aurora-postgresql")

    assert "quote_identifier(\"aurora-postgresql\", args['jdbc_schema'])" in code
    assert "quote_identifier(\"aurora-postgresql\", args['jdbc_table'])" in code
