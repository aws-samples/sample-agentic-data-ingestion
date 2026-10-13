# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for RDS_DATABASE target config resolution.

Feature: p14-rds-database-targets, Property 7: RDS_DATABASE target values reach the Glue job

For any Target_Config with type = RDS_DATABASE containing jdbc_url, jdbc_driver,
and jdbc_table values, the Glue job arguments built for it SHALL carry each of
those values unchanged.

**The function this property was written against is gone.** It used to call
``resolve_parameter_value`` once per parameter name. That function was deleted
with the JDBC-target credential fix: it had no production caller, and for an
RDS_DATABASE target it resolved ``jdbc_user`` / ``jdbc_password`` by fetching the
PLAINTEXT secret inside the Lambda — the mechanism standing invariant
``LGR-819980f-1`` rules out. Its three test functions were deleted with it. The
property is restated against ``build_jdbc_arguments``, the code path that has
always actually produced these arguments, and gains the half the old resolver
inverted: an RDS_DATABASE target's credential reaches the job as the
``--secret_arn`` reference and never as a plaintext pair, even when the record
still carries one.

**Validates: Requirements 9.1, 9.2, 9.3**
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
        "create_glue_pipeline_lf_rds_prop",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
build_jdbc_arguments = _mod.build_jdbc_arguments

# ---------------------------------------------------------------------------
# Strategies — generate realistic JDBC values
# ---------------------------------------------------------------------------

# JDBC URLs: jdbc:<protocol>://<host>:<port>/<database>
jdbc_protocol = st.sampled_from(["postgresql", "mysql", "mariadb", "oracle:thin:@", "sqlserver"])
jdbc_host = st.from_regex(r"[a-z][a-z0-9\-]{1,20}\.[a-z]{2,6}", fullmatch=True)
jdbc_port = st.integers(min_value=1024, max_value=65535)
jdbc_db = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)


@st.composite
def jdbc_url_strategy(draw):
    proto = draw(jdbc_protocol)
    host = draw(jdbc_host)
    port = draw(jdbc_port)
    db = draw(jdbc_db)
    return f"jdbc:{proto}://{host}:{port}/{db}"


jdbc_driver_strategy = st.sampled_from(
    [
        "org.postgresql.Driver",
        "com.mysql.cj.jdbc.Driver",
        "org.mariadb.jdbc.Driver",
        "oracle.jdbc.OracleDriver",
        "com.microsoft.sqlserver.jdbc.SQLServerDriver",
    ]
)

# jdbc_table: database.table format
jdbc_table_strategy = st.builds(
    lambda db, tbl: f"{db}.{tbl}",
    st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True),
    st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True),
)


_secret_arn_strategy = st.builds(
    lambda name, suffix: f"arn:aws:secretsmanager:us-west-2:123456789012:secret:{name}-{suffix}",
    st.from_regex(r"[a-z][a-z0-9/_\-]{2,20}", fullmatch=True),
    st.from_regex(r"[A-Za-z0-9]{6}", fullmatch=True),
)

# Plaintext stand-ins a pre-fix RDS_DATABASE record may still carry. Drawn from
# an alphabet no other generated field uses, so "this value reached an argument"
# cannot be a coincidental match with a URL, driver or table.
_plaintext_strategy = st.text(alphabet="!#%&*+=?@^~", min_size=10, max_size=20)


@st.composite
def rds_database_target_config(draw):
    """Generate a random RDS_DATABASE Target_Config dict."""
    config = {
        "type": "RDS_DATABASE",
        "jdbc_connection_name": "agentic-data-ingestion-rds-prop-jdbc",
        "jdbc_url": draw(jdbc_url_strategy()),
        "jdbc_driver": draw(jdbc_driver_strategy),
        "jdbc_table": draw(jdbc_table_strategy),
        "secret_arn": draw(_secret_arn_strategy),
    }
    if draw(st.booleans()):
        config["jdbc_user"] = draw(_plaintext_strategy)
        config["jdbc_password"] = draw(_plaintext_strategy)
    return config


def _no_glue(*_args, **_kwargs):
    raise AssertionError("every value is on the config; building the arguments must not read Glue")


# ---------------------------------------------------------------------------
# Feature: p14-rds-database-targets, Property 7: RDS_DATABASE target values
# reach the Glue job
# ---------------------------------------------------------------------------


class TestRdsDatabaseTargetArguments:
    """Property 7, restated on ``build_jdbc_arguments`` (see the module docstring).

    For any Target_Config with type = RDS_DATABASE containing jdbc_url,
    jdbc_driver, jdbc_table and a secret_arn, the built Glue job arguments SHALL
    carry each value unchanged, SHALL carry the credential only as
    ``--secret_arn``, and SHALL NOT carry a ``--jdbc_user`` / ``--jdbc_password``
    argument or either plaintext value.

    The module's Glue client accessor is replaced through the function's own
    globals, so the property cannot silently become a real AWS call.

    **Validates: Requirements 9.1, 9.2, 9.3**
    """

    @given(config=rds_database_target_config())
    @settings(max_examples=10)
    def test_config_values_reach_the_job_and_credentials_only_by_reference(self, config):
        # Feature: p14-rds-database-targets, Property 7: RDS_DATABASE target values reach the Glue job
        with mock.patch.dict(build_jdbc_arguments.__globals__, {"_glue_client": _no_glue}):
            args = build_jdbc_arguments(dict(config), source_path="s3://bucket/data.csv")

        for key in ("jdbc_url", "jdbc_driver", "jdbc_table", "secret_arn"):
            assert args[f"--{key}"] == config[key], f"{key} mismatch: got {args[f'--{key}']!r}"
        assert "--jdbc_user" not in args
        assert "--jdbc_password" not in args
        for key in ("jdbc_user", "jdbc_password"):
            if key in config:
                assert config[key] not in args.values(), f"the plaintext {key} reached a job argument"
