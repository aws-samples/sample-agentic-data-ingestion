# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP server access-flag resolution tests (P28 R16.1).

Verifies, for every MCP server in mcp/:

- ``_env_flag`` resolves False when the env var is unset and True only when
  the value is exactly "true" (case-insensitive, whitespace-tolerant) — so
  write/sensitive access is an explicit opt-in, never a default.
- All five servers carry a textually identical ``_env_flag`` (they are
  standalone containers, so the helper is deliberately duplicated per server;
  this guard keeps the copies from drifting).
- No server source hardcodes ``allow_write=True`` or
  ``allow_sensitive_data_access=True`` (the R16.1 "SHALL NOT" clause).
- The four dataprocessing-based servers' ``register_handlers`` signatures
  default both flags to False.

Import-light by design: server modules pull in FastMCP and the vendored
awslabs package at import time, so these tests extract the relevant AST
nodes from source instead of importing the modules.
"""

import ast
import re
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[3] / "mcp"

# server name -> entrypoint module
SERVER_ENTRYPOINTS = {
    "analytics_server": MCP_DIR / "analytics_server" / "server.py",
    "codegen_server": MCP_DIR / "codegen_server" / "server.py",
    "data_processing": MCP_DIR / "data_processing" / "server.py",
    "infra_server": MCP_DIR / "infra_server" / "server.py",
    "s3_tables": MCP_DIR / "s3_tables" / "agentcore_s3tables_mcp.py",
}

# The four servers built on the vendored awslabs dataprocessing handlers
DATAPROCESSING_SERVERS = ["analytics_server", "codegen_server", "data_processing", "infra_server"]


def _extract_function_source(path: Path, func_name: str) -> str:
    """Return the source segment of a module-level function definition."""
    source = path.read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"{path} has no module-level function {func_name!r}")


def _load_env_flag(path: Path):
    """Exec just the _env_flag function from *path* and return the callable."""
    func_source = _extract_function_source(path, "_env_flag")
    namespace: dict = {}
    exec(compile(func_source, str(path), "exec"), {"os": __import__("os")}, namespace)
    return namespace["_env_flag"]


class TestEnvFlagResolution:
    """_env_flag: default False when unset; True only for 'true'."""

    @pytest.fixture(params=sorted(SERVER_ENTRYPOINTS))
    def env_flag(self, request):
        return _load_env_flag(SERVER_ENTRYPOINTS[request.param])

    def test_unset_is_false(self, env_flag, monkeypatch):
        monkeypatch.delenv("MCP_ALLOW_WRITE", raising=False)
        assert env_flag("MCP_ALLOW_WRITE") is False

    def test_true_string_enables(self, env_flag, monkeypatch):
        monkeypatch.setenv("MCP_ALLOW_WRITE", "true")
        assert env_flag("MCP_ALLOW_WRITE") is True

    @pytest.mark.parametrize("value", ["TRUE", "True", " true "])
    def test_case_and_whitespace_tolerant(self, env_flag, monkeypatch, value):
        monkeypatch.setenv("MCP_ALLOW_WRITE", value)
        assert env_flag("MCP_ALLOW_WRITE") is True

    @pytest.mark.parametrize("value", ["", "false", "1", "yes", "on", "enabled", "0"])
    def test_everything_else_is_false(self, env_flag, monkeypatch, value):
        monkeypatch.setenv("MCP_ALLOW_WRITE", value)
        assert env_flag("MCP_ALLOW_WRITE") is False

    def test_sensitive_flag_unset_is_false(self, env_flag, monkeypatch):
        monkeypatch.delenv("MCP_ALLOW_SENSITIVE_DATA_ACCESS", raising=False)
        assert env_flag("MCP_ALLOW_SENSITIVE_DATA_ACCESS") is False


class TestServerConsistency:
    """The five standalone servers must behave identically."""

    def test_env_flag_sources_identical_across_servers(self):
        sources = {name: _extract_function_source(path, "_env_flag") for name, path in SERVER_ENTRYPOINTS.items()}
        reference = sources["analytics_server"]
        for name, src in sources.items():
            assert src == reference, f"{name}'s _env_flag drifted from the shared implementation"

    def test_all_servers_read_both_flags_from_env(self):
        for name, path in SERVER_ENTRYPOINTS.items():
            source = path.read_text()
            assert '_env_flag("MCP_ALLOW_WRITE")' in source, f"{name} does not env-gate allow_write"
            assert '_env_flag("MCP_ALLOW_SENSITIVE_DATA_ACCESS")' in source, (
                f"{name} does not env-gate allow_sensitive_data_access"
            )


class TestNoHardcodedTrue:
    """R16.1: MCP servers SHALL NOT hardcode allow_write/sensitive True."""

    @pytest.mark.parametrize("name", sorted(SERVER_ENTRYPOINTS))
    def test_no_hardcoded_true_flags(self, name):
        source = SERVER_ENTRYPOINTS[name].read_text()
        pattern = re.compile(r"allow_(?:write|sensitive_data_access)\s*(?::\s*bool\s*)?=\s*True")
        matches = pattern.findall(source)
        assert not matches, f"{name} hardcodes flag(s) to True: {matches}"


class TestRegisterHandlersDefaults:
    """register_handlers signatures default both flags to False."""

    @pytest.mark.parametrize("name", DATAPROCESSING_SERVERS)
    def test_register_handlers_defaults_false(self, name):
        path = SERVER_ENTRYPOINTS[name]
        tree = ast.parse(path.read_text())
        func = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "register_handlers"
        )
        defaults = dict(
            zip(
                [a.arg for a in func.args.args[-len(func.args.defaults) :]],
                func.args.defaults,
                strict=True,
            )
        )
        for flag in ("allow_write", "allow_sensitive_data_access"):
            assert flag in defaults, f"{name}: register_handlers missing default for {flag}"
            node = defaults[flag]
            assert isinstance(node, ast.Constant) and node.value is False, (
                f"{name}: register_handlers defaults {flag} to something other than False"
            )
