# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""S3TablesHandler least-privilege defaults and write-tool gating (P28 R16.1).

The s3_tables server previously hardcoded ``allow_write=True`` and
``allow_sensitive_data_access=True`` in both the handler signature and the
bootstrap. These tests pin the fixed behavior:

- Constructor defaults are False for both flags.
- With the defaults, mutation tools (create/rename/delete tables, namespaces,
  buckets, and table policies) are NOT registered — they never appear in the
  tool list — while read/compute tools are.
- ``allow_write=True`` registers the mutation tools (the opt-in still works).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"
S3_TABLES_MODULE = MCP_DIR / "s3_tables" / "agentcore_s3tables_mcp.py"

READ_TOOLS = {
    "test_s3tables_connection",
    "agentcore_list_s3_table_buckets",
    "agentcore_get_s3_table_bucket",
    "agentcore_list_s3_table_namespaces",
    "agentcore_list_s3_tables",
    "agentcore_get_s3_table",
    "agentcore_get_s3_table_metadata_location",
    "agentcore_get_s3_table_policy",
    "agentcore_recommend_iceberg_config",
    "agentcore_estimate_s3_tables_cost",
}

WRITE_TOOLS = {
    "agentcore_create_s3_table_bucket",
    "agentcore_create_s3_table_namespace",
    "agentcore_create_s3_table",
    "agentcore_rename_s3_table",
    "agentcore_delete_s3_table",
    "agentcore_put_s3_table_policy",
    "agentcore_delete_s3_table_policy",
}


class FakeMCP:
    """Minimal FastMCP stand-in recording registered tool names."""

    def __init__(self):
        self.registered: list[str] = []

    def tool(self, name: str):
        self.registered.append(name)

        def decorator(fn):
            return fn

        return decorator


@pytest.fixture(scope="module")
def s3tables_module():
    """Load the s3_tables server module by path (it is not a package)."""
    spec = importlib.util.spec_from_file_location("agentcore_s3tables_mcp_under_test", S3_TABLES_MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.fixture(autouse=True)
def _no_aws_profile(monkeypatch):
    """Handler __init__ builds boto3 clients; a stale AWS_PROFILE would raise."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


class TestDefaultsAreFalse:
    def test_constructor_defaults_false(self, s3tables_module):
        handler = s3tables_module.S3TablesHandler(FakeMCP())
        assert handler.allow_write is False
        assert handler.allow_sensitive_data_access is False

    def test_default_registers_only_read_tools(self, s3tables_module):
        fake = FakeMCP()
        s3tables_module.S3TablesHandler(fake)

        registered = set(fake.registered)
        assert registered == READ_TOOLS, (
            f"unexpected default tool surface; extra={registered - READ_TOOLS}, missing={READ_TOOLS - registered}"
        )
        assert not (registered & WRITE_TOOLS)


class TestWriteOptIn:
    def test_allow_write_registers_mutation_tools(self, s3tables_module):
        fake = FakeMCP()
        s3tables_module.S3TablesHandler(fake, allow_write=True)

        registered = set(fake.registered)
        assert registered == READ_TOOLS | WRITE_TOOLS

    def test_sensitive_flag_alone_adds_nothing(self, s3tables_module):
        """No current tool reads data rows; the flag must not widen the surface."""
        fake = FakeMCP()
        s3tables_module.S3TablesHandler(fake, allow_sensitive_data_access=True)

        assert set(fake.registered) == READ_TOOLS


class TestEnvGatedBootstrap:
    def test_env_flag_default_false(self, s3tables_module, monkeypatch):
        monkeypatch.delenv("MCP_ALLOW_WRITE", raising=False)
        assert s3tables_module._env_flag("MCP_ALLOW_WRITE") is False

    def test_env_flag_true_enables(self, s3tables_module, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_WRITE", "true")
        assert s3tables_module._env_flag("MCP_ALLOW_WRITE") is True

    def test_bootstrap_uses_env_flags_not_literals(self):
        source = S3_TABLES_MODULE.read_text()
        assert "allow_write=True, allow_sensitive_data_access=True" not in source
        assert '_env_flag("MCP_ALLOW_WRITE")' in source
        assert '_env_flag("MCP_ALLOW_SENSITIVE_DATA_ACCESS")' in source
