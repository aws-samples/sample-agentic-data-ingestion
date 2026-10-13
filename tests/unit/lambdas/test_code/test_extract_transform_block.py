# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: extract_transform_block marker contract (P28b task 6.1 fix).

Every ``pyspark_templates/*.j2`` closes the AI transform with
``# === End Transform Block ===``. The extractor previously searched for
``# === End AI-Generated Transform Block ===`` — a marker no template
emits — so extraction ran to end-of-file and the Code Interpreter
converted the trailing template scaffolding (DateType auto-cast, DQ,
Iceberg write), failing every run with a ConversionError on scaffolding
constructs (observed live: ``unconvertible PySpark construct 'DateType'``).

These tests pin the extractor to the real template markers.

Validates: Requirements 1.9
"""

import glob
import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

# Region/account before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(_REPO_ROOT, "shared"))

_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


TRANSFORM_LINE = '    df_transformed = df.withColumn("amount", col("amount").cast("double"))'

TEMPLATE_SHAPED_CODE = f"""import sys
from pyspark.sql.functions import col

try:
    df = spark.read.csv(source_path)

    # === AI-Generated Transform Block ===

{TRANSFORM_LINE}

    # === End Transform Block ===

    # === Auto-cast + column selection (single pass using cached schema) ===
    if _target_schema:
        for field in _target_schema:
            if isinstance(field.dataType, DateType):
                pass
    # === End Auto-cast + Select ===

    df_transformed.writeTo(target_table).using("iceberg").append()
finally:
    job.commit()
"""


class TestExtractTransformBlock:
    def test_extraction_stops_at_real_template_end_marker(self):
        """The extracted block must contain ONLY the AI transform — none of
        the trailing scaffolding (auto-cast DateType, Iceberg write) that a
        missed end marker would drag in."""
        block = _mod.extract_transform_block(TEMPLATE_SHAPED_CODE)
        assert TRANSFORM_LINE.strip() in block
        assert "DateType" not in block
        assert "writeTo" not in block
        assert "job.commit" not in block

    def test_extractor_end_marker_matches_every_template(self):
        """Each pyspark_templates/*.j2 that opens a transform block closes it
        with a marker the extractor actually finds."""
        templates = glob.glob(os.path.join(_REPO_ROOT, "pyspark_templates", "*.j2"))
        assert templates, "no templates found — path wiring broken"
        source = open(os.path.join(_TEST_CODE_DIR, "lambda_function.py")).read()
        m = re.search(r'end_marker = "([^"]+)"', source)
        assert m, "end_marker literal not found in lambda_function.py"
        end_marker = m.group(1)
        for path in templates:
            body = open(path).read()
            if "# === AI-Generated Transform Block ===" in body:
                assert end_marker in body, f"{os.path.basename(path)} does not carry the extractor's end marker"

    def test_no_markers_falls_back_to_full_code(self):
        code = "df_transformed = df"
        assert _mod.extract_transform_block(code) == code
