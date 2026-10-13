# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for ``agentic_data_ingestion_create_glue_pipeline.lambda_function``
focused on P19 (JDBC Target Modes and Write_Mode Selector) behavior.

These tests cover the legacy-item backward-compatibility contract for
``write_mode``: pre-P19 P17 pipelines persisted with
``target_config.type == "JDBC"`` and NO ``write_mode`` field MUST render
with ``mode="overwrite"`` after the Task 5.1 line-~726 default fix.

Validates: P19 Requirements 9.1
"""

import importlib.util
import os
import sys
import unittest.mock as mock

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

os.environ["MCP_SIGV4_ENABLED"] = "false"
os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

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


def _load_module():
    """Load the create_glue_pipeline lambda_function by path.

    Mirrors the loader used in the sibling create_glue_pipeline unit tests
    (test_source_path_resolution.py, test_warehouse_location_resolution.py)
    so heavy optional imports (strands, mcp, prompt_template_loader) are
    stubbed before module execution.
    """
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_glue_pipeline_lf_p19",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["create_glue_pipeline_lf_p19"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()
build_jdbc_arguments = _lf.build_jdbc_arguments


# Feature: p19-jdbc-target-modes, Example test for Req 9.1 — legacy P17 JDBC
# pipelines persisted without a ``write_mode`` field render with overwrite.
class TestLegacyJdbcWriteModeDefault:
    """Validates Req 9.1: legacy JDBC items default to 'overwrite'."""

    def test_legacy_jdbc_item_without_write_mode_renders_overwrite(self):
        """A persisted ``agentic_data_ingestion_pipelines`` item with ``target_config.type ==
        'JDBC'`` and NO ``write_mode`` field MUST render with ``'overwrite'``.

        Exercises the line-~726 default established by P19 Task 5.1
        (``write_mode = target_config.get("write_mode", "overwrite")``).
        Pre-P19 (P17) DDB items omitted ``target_config.write_mode``; the
        backward-compatibility contract in Req 9.1 says those legacy items
        must render with ``mode="overwrite"``.
        """
        # Simulates a legacy P17 ``agentic_data_ingestion_pipelines`` item: type=JDBC,
        # no ``write_mode`` key in the persisted ``target_config`` Map.
        legacy_target_config = {
            "type": "JDBC",
            "jdbc_connection_name": "my-conn",
            "database_name": "mydb",
            "table_name": "mytable",
            "jdbc_url": "jdbc:postgresql://host:5432/db",
            "jdbc_driver": "org.postgresql.Driver",
            # A Secrets Manager reference where this fixture used to carry an
            # inline `jdbc_user` / `jdbc_password` pair. Since the JDBC-target
            # credential fix (standing invariant `LGR-819980f-1`) the builder
            # ignores that pair and needs a reference instead; without one it
            # reads the connection's SECRET_ID, i.e. calls GetConnection, which is
            # not what this write_mode test is about.
            "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:legacy-p17-AbCdEf",
            # NOTE: no ``write_mode`` field — this is the legacy P17 shape.
        }
        assert "write_mode" not in legacy_target_config

        result = build_jdbc_arguments(
            legacy_target_config,
            source_path="s3://bucket/data.csv",
        )

        # The line-~726 default fires; the resolved value flows into the
        # operational ``--write-mode`` metadata key. Both the value and the
        # template-render contract (Req 7.5: ``mode="overwrite"`` iff
        # ``write_mode == 'overwrite'``) are anchored on the literal string
        # ``"overwrite"``, so asserting on the argument is sufficient
        # to prove the legacy-item path renders with ``mode="overwrite"``.
        assert result["--write-mode"] == "overwrite", (
            "Legacy P17 JDBC pipeline (no write_mode field) must default to "
            f"'overwrite' per Req 9.1; got: {result.get('--write-mode')!r}"
        )
