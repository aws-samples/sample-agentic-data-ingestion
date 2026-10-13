# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for JDBC driver mapping functions.

Feature: p14-rds-database-targets, Property 10: Frontend/backend driver mapping consistency
Feature: p14-rds-database-targets, Property 11: build_jdbc_url supports all engine types

Uses Hypothesis to verify that JDBC mappings are consistent and that
build_jdbc_url produces valid URLs for all supported engine types.

**Validates: Requirements 16.1, 16.4**
"""

import importlib.util
import os
import sys
import unittest.mock as mock

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Module loading — mock heavy deps so we can import the pure functions
# ---------------------------------------------------------------------------

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

os.environ.setdefault("MCP_SIGV4_ENABLED", "false")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
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


def _load_module():
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_glue_pipeline_lf_prop",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
get_jdbc_driver = _mod.get_jdbc_driver
build_jdbc_url = _mod.build_jdbc_url

# ---------------------------------------------------------------------------
# The frontend JDBC_DRIVER_MAP (source of truth from design doc)
# ---------------------------------------------------------------------------
FRONTEND_JDBC_DRIVER_MAP = {
    "postgres": {"driver": "org.postgresql.Driver", "port": 5432, "protocol": "postgresql"},
    "aurora-postgresql": {
        "driver": "org.postgresql.Driver",
        "port": 5432,
        "protocol": "postgresql",
    },
    "mysql": {"driver": "com.mysql.cj.jdbc.Driver", "port": 3306, "protocol": "mysql"},
    "aurora-mysql": {"driver": "com.mysql.cj.jdbc.Driver", "port": 3306, "protocol": "mysql"},
    "mariadb": {"driver": "org.mariadb.jdbc.Driver", "port": 3306, "protocol": "mariadb"},
    "oracle-ee": {
        "driver": "oracle.jdbc.OracleDriver",
        "port": 1521,
        "protocol": "oracle:thin:@",
    },
    "oracle-se2": {
        "driver": "oracle.jdbc.OracleDriver",
        "port": 1521,
        "protocol": "oracle:thin:@",
    },
    "sqlserver-ee": {
        "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
        "port": 1433,
        "protocol": "sqlserver",
    },
    "sqlserver-se": {
        "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
        "port": 1433,
        "protocol": "sqlserver",
    },
    "sqlserver-ex": {
        "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
        "port": 1433,
        "protocol": "sqlserver",
    },
    "sqlserver-web": {
        "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
        "port": 1433,
        "protocol": "sqlserver",
    },
}

ALL_ENGINES = list(FRONTEND_JDBC_DRIVER_MAP.keys())

# Strategies
engine_strategy = st.sampled_from(ALL_ENGINES)
host_strategy = st.from_regex(r"[a-z][a-z0-9\-]{0,30}\.[a-z]{2,6}", fullmatch=True)
port_strategy = st.integers(min_value=1, max_value=65535)
db_name_strategy = st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)


# ---------------------------------------------------------------------------
# Feature: p14-rds-database-targets, Property 10: Frontend/backend driver
# mapping consistency
# ---------------------------------------------------------------------------
class TestFrontendBackendDriverConsistency:
    """Property 10: Frontend/backend driver mapping consistency.

    For each supported engine type, verify get_jdbc_driver(engine) matches
    the frontend JDBC_DRIVER_MAP[engine].driver.

    **Validates: Requirements 16.1**
    """

    @given(engine=engine_strategy)
    @settings(max_examples=100)
    def test_backend_driver_matches_frontend(self, engine):
        """Backend get_jdbc_driver matches frontend JDBC_DRIVER_MAP for every engine."""
        backend_driver = get_jdbc_driver(engine)
        frontend_driver = FRONTEND_JDBC_DRIVER_MAP[engine]["driver"]
        assert backend_driver == frontend_driver, (
            f"Mismatch for engine '{engine}': backend={backend_driver}, frontend={frontend_driver}"
        )


# ---------------------------------------------------------------------------
# Feature: p14-rds-database-targets, Property 11: build_jdbc_url supports
# all engine types
# ---------------------------------------------------------------------------
class TestBuildJdbcUrlAllEngines:
    """Property 11: build_jdbc_url supports all engine types.

    For each supported engine type with random host/port/db, verify
    build_jdbc_url returns a non-empty string starting with 'jdbc:'.

    **Validates: Requirements 16.4**
    """

    @given(
        engine=engine_strategy,
        host=host_strategy,
        port=port_strategy,
        database=db_name_strategy,
    )
    @settings(max_examples=100)
    def test_build_jdbc_url_returns_valid_jdbc_string(self, engine, host, port, database):
        """build_jdbc_url produces a non-empty JDBC URL for every supported engine."""
        # P17 — the canonical helper now takes the explicit
        # ``(engine, host, port, database)`` signature; the legacy
        # dict-based variant was removed in P17 Task 2.2.
        url = build_jdbc_url(engine, host, port, database)
        assert isinstance(url, str)
        assert len(url) > 0
        assert url.startswith("jdbc:")
