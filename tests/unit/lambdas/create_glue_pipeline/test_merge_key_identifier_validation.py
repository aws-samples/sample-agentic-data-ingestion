# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""P28 R12 — merge_key identifier validation at Glue-job argument assembly.

``create_glue_job`` writes ``--merge_key`` into the job's DefaultArguments;
the rendered job reads it back at runtime and f-strings it into the
``spark.sql`` MERGE INTO statement. ``_common_write_arguments`` is the seam
where the value leaves pipeline config and enters the job contract — a
hostile ``target_config.merge_key`` must fail there, naming the identifier,
before it can ever reach Glue.

Hostile inputs cover quotes, semicolons, newlines, and SQL comment tokens
(R12.4).

**Validates: Requirements 12.1, 12.2, 12.4**
"""

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)
sys.path.insert(0, LAMBDA_DIR)

os.environ["MCP_SIGV4_ENABLED"] = "false"
os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")

from identifier_utils import IdentifierValidationError

# Heavy/optional dependencies mocked at import time (same set as
# test_job_config_overrides.py so module loading is hermetic).
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
        load_prompt_template=mock.MagicMock(return_value="mock"),
    ),
}


def _load(mod_name: str, alias: str):
    """Load a create_glue_pipeline module standalone with mocked heavy deps."""
    mod_path = os.path.join(LAMBDA_DIR, f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(alias, mod_path, submodule_search_locations=[LAMBDA_DIR])
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules[alias] = mod
        spec.loader.exec_module(mod)
    return mod


_jb = _load("job_builders", "mkiv_job_builders")


# Hostile identifiers per R12.4: quotes, semicolons, newlines, comment tokens.
HOSTILE_IDENTIFIERS = [
    pytest.param('col"; DROP TABLE x; --', id="double-quote-semicolon-comment"),
    pytest.param("col'; DROP TABLE x; --", id="single-quote-semicolon-comment"),
    pytest.param("col; DELETE FROM t", id="semicolon"),
    pytest.param("col\nDROP TABLE x", id="newline"),
    pytest.param("col--comment", id="line-comment-token"),
    pytest.param("col/*comment*/", id="block-comment-token"),
]


class TestCommonWriteArguments:
    """**Validates: Requirements 12.1, 12.2, 12.4**"""

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_merge_key_fails_argument_assembly(self, hostile):
        with pytest.raises(IdentifierValidationError) as exc_info:
            _jb._common_write_arguments({"write_mode": "merge", "merge_key": hostile})

        # R12.2: the failure names the offending identifier and the sink.
        assert exc_info.value.name == hostile
        assert "merge_key" in str(exc_info.value)

    def test_valid_merge_key_is_passed_through_unchanged(self):
        args = _jb._common_write_arguments({"write_mode": "merge", "merge_key": "customer_id"})
        assert args == {"--write_mode": "merge", "--merge_key": "customer_id"}

    def test_absent_merge_key_defaults_to_id(self):
        args = _jb._common_write_arguments({"write_mode": "overwrite"})
        assert args == {"--write_mode": "overwrite", "--merge_key": "id"}

    def test_overlong_merge_key_fails(self):
        with pytest.raises(IdentifierValidationError):
            _jb._common_write_arguments({"merge_key": "k" * 256})

    def test_non_string_merge_key_fails(self):
        with pytest.raises(IdentifierValidationError):
            _jb._common_write_arguments({"merge_key": ["id"]})
