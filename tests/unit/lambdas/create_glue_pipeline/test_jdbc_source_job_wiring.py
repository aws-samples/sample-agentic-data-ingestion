# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the JDBC-source CreateGluePipeline gaps (P17 follow-up audit).

Covers the five-finding audit fixes that live in the create_glue_pipeline
Lambda:

* iam_roles.create_pipeline_specific_role — JDBC-SOURCE grants applied
  regardless of TARGET type: VPC/ENI, source secret, source connection,
  dependencies-bucket read, and (incremental) watermark table.
* job_builders._add_jdbc_source_job_wiring — attaches the source Glue
  connection to the job and ships jdbc_helpers via --extra-py-files.

It also carries the JDBC-TARGET credential path, standing invariant
``LGR-819980f-1``: the IAM secret grant, ``build_jdbc_arguments``,
``resolve_jdbc_target_secret``, ``secretsmanager_resource_arn``, the log lines,
and ``create_glue_job`` end to end — no plaintext credential job argument, and no
``GetSecretValue`` call from this Lambda.

These are the runtime-only gaps that a JDBC-source → non-JDBC-target
pipeline would otherwise hit at the Glue ETL job (outside-VPC connection
timeout, ModuleNotFoundError on jdbc_helpers, AccessDenied on the source
secret / watermark table).
"""

import importlib.util
import json
import os
import re
import sys
import unittest.mock as mock

import boto3
import pytest
from botocore.client import BaseClient
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

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

os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")

ACCOUNT = "123456789012"
REGION = "us-west-2"

# A JDBC target's Secrets Manager reference. An ARN is a reference, not a
# credential, which is what `LGR-819980f-1` permits in a job argument.
_TARGET_SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/tgt-AbCdEf"

# Plaintext stand-ins. Every assertion on them is NEGATIVE: they must never appear
# in a job argument, a log line or an exception message.
_PLAINTEXT_USER = "placeholder-user-not-a-credential"
_PLAINTEXT_PASSWORD = "placeholder-password-not-a-credential"


class _FakeGlue:
    """A Glue client fake that answers ``get_connection`` like the real API.

    Two properties of the real API are reproduced rather than simplified away,
    because a fake that accepts what the real API refuses is a false statement
    about the contract (``LGR-cb3fa38-2``):

    * ``get_tags`` raises — Glue Connections are not a taggable resource type.
    * ``HidePassword=True`` drops ``PASSWORD`` from the returned properties.

    ``calls`` records every ``get_connection`` keyword set, so a test can assert
    both that Glue was (or was not) read and how.
    """

    def __init__(self, props: dict | None = None, error: BaseException | None = None):
        self.props = props or {}
        self.error = error
        self.calls: list[dict] = []

    def get_connection(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        props = dict(self.props)
        if kwargs.get("HidePassword"):
            props.pop("PASSWORD", None)
        return {"Connection": {"Name": kwargs.get("Name"), "ConnectionProperties": props}}

    def get_tags(self, **_kwargs):
        raise AssertionError("Glue Connections are not taggable; nothing may read a connection tag")


def _load(mod_name: str):
    """Load a sibling module of the create_glue_pipeline Lambda standalone."""
    mod_path = os.path.join(LAMBDA_DIR, f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(f"cgp_{mod_name}", mod_path, submodule_search_locations=[LAMBDA_DIR])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"cgp_{mod_name}"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── iam_roles.create_pipeline_specific_role — JDBC source grants ─────────


def _get_inline_policy(iam_client, role_name: str) -> dict:
    doc = iam_client.get_role_policy(RoleName=role_name, PolicyName="PipelineSpecificAccess")
    return doc["PolicyDocument"]


def _actions(statements: list[dict]) -> set[str]:
    out: set[str] = set()
    for s in statements:
        a = s.get("Action", [])
        out.update([a] if isinstance(a, str) else a)
    return out


class TestCreatePipelineSpecificRoleJdbcSource:
    @mock_aws
    def test_jdbc_source_with_s3_target_gets_vpc_secret_and_connection(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        iam_roles = _load("iam_roles")
        iam_roles.iam_client.attach_role_policy = mock.MagicMock()
        monkeypatch.setattr("time.sleep", lambda *_: None)

        source_config = {
            "jdbc_url": "jdbc:mysql://host:3306/appdb",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:db-Abc",
            "jdbc_schema": "appdb",
            "jdbc_table": "customers",
            "read_mode": "full_table",
        }
        # Target is S3 Tables (NON-JDBC) — the case that was previously broken.
        target_config = {"type": "S3_TABLES", "bucket_name": "agentic-data-ingestion-table-pid"}

        iam_roles.create_pipeline_specific_role(
            "pid-abc",
            target_config,
            s3_table_bucket={"bucket_name": "agentic-data-ingestion-table-pid"},
            source_config=source_config,
        )

        iam_client = boto3.client("iam", region_name=REGION)
        policy = _get_inline_policy(iam_client, "AgenticDataIngestion-pid-abc-ExecutionRole")
        actions = _actions(policy["Statement"])

        # VPC / ENI present even though the target is S3.
        assert "ec2:CreateNetworkInterface" in actions
        assert "ec2:DescribeSubnets" in actions
        # Source secret read present and scoped to the SOURCE secret.
        secret_stmts = [
            s
            for s in policy["Statement"]
            if "secretsmanager:GetSecretValue" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        ]
        assert any(source_config["secret_arn"] == s["Resource"] for s in secret_stmts)
        # Source connection read present.
        assert "glue:GetConnection" in actions
        # Dependencies bucket read present (for --extra-py-files).
        assert "s3:GetObject" in actions

    @mock_aws
    def test_full_table_source_omits_watermark_grant(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        iam_roles = _load("iam_roles")
        iam_roles.iam_client.attach_role_policy = mock.MagicMock()
        monkeypatch.setattr("time.sleep", lambda *_: None)
        iam_roles.create_pipeline_specific_role(
            "pid-ft",
            {"type": "GLUE_CATALOG"},
            source_config={
                "jdbc_url": "jdbc:postgresql://h:5432/db",
                "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:s-X",
                "read_mode": "full_table",
            },
        )
        iam_client = boto3.client("iam", region_name=REGION)
        policy = _get_inline_policy(iam_client, "AgenticDataIngestion-pid-ft-ExecutionRole")
        watermark_stmts = [
            s
            for s in policy["Statement"]
            if "dynamodb:PutItem" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
            and "agentic_data_ingestion_pipeline_watermarks" in json.dumps(s["Resource"])
        ]
        assert watermark_stmts == []

    @mock_aws
    def test_incremental_source_gets_watermark_grant(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        iam_roles = _load("iam_roles")
        iam_roles.iam_client.attach_role_policy = mock.MagicMock()
        monkeypatch.setattr("time.sleep", lambda *_: None)
        iam_roles.create_pipeline_specific_role(
            "pid-inc",
            {"type": "S3_PARQUET"},
            source_config={
                "jdbc_url": "jdbc:postgresql://h:5432/db",
                "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:s-X",
                "read_mode": "incremental",
                "watermark_column": "updated_at",
                "watermark_type": "timestamp",
            },
        )
        iam_client = boto3.client("iam", region_name=REGION)
        policy = _get_inline_policy(iam_client, "AgenticDataIngestion-pid-inc-ExecutionRole")
        watermark_stmts = [
            s
            for s in policy["Statement"]
            if "agentic_data_ingestion_pipeline_watermarks" in json.dumps(s.get("Resource", ""))
        ]
        assert watermark_stmts, "incremental JDBC source must grant watermark table access"
        assert "dynamodb:GetItem" in _actions(watermark_stmts)
        assert "dynamodb:PutItem" in _actions(watermark_stmts)

    @mock_aws
    def test_non_jdbc_source_unchanged(self, monkeypatch):
        """S3_UPLOAD source (no source_config) must NOT get JDBC grants."""
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        iam_roles = _load("iam_roles")
        iam_roles.iam_client.attach_role_policy = mock.MagicMock()
        monkeypatch.setattr("time.sleep", lambda *_: None)
        iam_roles.create_pipeline_specific_role(
            "pid-s3",
            # A valid table bucket name: the builder refuses an invalid one (S1),
            # and "b" (one character) was a placeholder this test never read.
            {"type": "S3_TABLES", "bucket_name": "orders-table-bucket"},
            s3_table_bucket={"bucket_name": "orders-table-bucket"},
            source_config=None,
        )
        iam_client = boto3.client("iam", region_name=REGION)
        policy = _get_inline_policy(iam_client, "AgenticDataIngestion-pid-s3-ExecutionRole")
        actions = _actions(policy["Statement"])
        # No ENI / NetworkInterface grants for a pure S3 upload pipeline.
        assert "ec2:CreateNetworkInterface" not in actions


# ── iam_roles.create_pipeline_specific_role — JDBC TARGET connection grant ──


class TestCreatePipelineSpecificRoleJdbcTargetConnection:
    """The JDBC-target branch must scope its glue:GetConnection grant to the
    actual connection, accepting BOTH key spellings.

    Regression guard: the branch read only ``jdbc_connection_name`` and fell
    back to the ``agentic-data-ingestion-{pipeline_id}-jdbc`` default when a
    glue_connection-mode target supplied ``glue_connection_name`` — granting
    GetConnection on the WRONG connection ARN (least-privilege gap).
    """

    @mock_aws
    def test_glue_connection_name_alias_scopes_grant(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        iam_roles = _load("iam_roles")
        iam_roles.iam_client.attach_role_policy = mock.MagicMock()
        monkeypatch.setattr("time.sleep", lambda *_: None)
        # The branch now reads the connection's SECRET_ID for the secret grant, so
        # the connection has to exist — with the shape real Glue accepts (URL +
        # SECRET_ID, no inline pair). Created through the module's own client so
        # it lands in the backend and region the resolver reads from.
        iam_roles.glue_client.create_connection(
            ConnectionInput={
                "Name": "agentic-data-ingestion-test-fixtures-postgres-target",
                "ConnectionType": "JDBC",
                "ConnectionProperties": {
                    "JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db",
                    "SECRET_ID": _TARGET_SECRET_ARN,
                },
            }
        )
        iam_roles.create_pipeline_specific_role(
            "pid-tgt",
            {
                "type": "JDBC",
                # glue_connection mode: only glue_connection_name is set.
                "glue_connection_name": "agentic-data-ingestion-test-fixtures-postgres-target",
                "jdbc_table": "public.customers",
            },
            source_config=None,
        )
        iam_client = boto3.client("iam", region_name=REGION)
        policy = _get_inline_policy(iam_client, "AgenticDataIngestion-pid-tgt-ExecutionRole")
        conn_stmts = [
            s
            for s in policy["Statement"]
            if "glue:GetConnection" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        ]
        resources = json.dumps([s.get("Resource", "") for s in conn_stmts])
        assert "connection/agentic-data-ingestion-test-fixtures-postgres-target" in resources
        # Must NOT fall back to the generated default when the alias is present.
        assert "connection/agentic-data-ingestion-pid-tgt-jdbc" not in resources

    @mock_aws
    def test_jdbc_connection_name_still_scopes_grant(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        iam_roles = _load("iam_roles")
        iam_roles.iam_client.attach_role_policy = mock.MagicMock()
        monkeypatch.setattr("time.sleep", lambda *_: None)
        iam_roles.create_pipeline_specific_role(
            "pid-tgt2",
            {
                "type": "JDBC",
                "jdbc_connection_name": "my-conn",
                "jdbc_table": "public.t",
                "secret_arn": _TARGET_SECRET_ARN,
            },
            source_config=None,
        )
        iam_client = boto3.client("iam", region_name=REGION)
        policy = _get_inline_policy(iam_client, "AgenticDataIngestion-pid-tgt2-ExecutionRole")
        conn_stmts = [
            s
            for s in policy["Statement"]
            if "glue:GetConnection" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        ]
        resources = json.dumps([s.get("Resource", "") for s in conn_stmts])
        assert "connection/my-conn" in resources


# ── iam_roles.create_pipeline_specific_role — JDBC TARGET secret grant ──────


def _secret_grant_resources(policy: dict) -> list:
    """Every Resource granted secretsmanager:GetSecretValue, normalised to a list."""
    out: list = []
    for s in policy["Statement"]:
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        if "secretsmanager:GetSecretValue" in actions:
            res = s.get("Resource", [])
            out.extend(res if isinstance(res, list) else [res])
    return out


class TestCreatePipelineSpecificRoleJdbcTargetSecretGrant:
    """The JDBC-target secret grant names the secret the job will read.

    Standing invariant ``LGR-819980f-1``: the generated JDBC write now ALWAYS
    calls ``get_secret_value`` at run time, by the ``--{prefix}secret_arn``
    reference ``build_jdbc_arguments`` emits. Both the grant and the argument come
    from ``job_arguments.resolve_jdbc_target_secret``. The defect these pin: the
    grant used to read ``target_config.get("secret_arn", <pipeline wildcard>)``,
    so a secret found only on the connection's SECRET_ID was passed to the job and
    never granted to it.

    Expectations are derived from that requirement — "the role may read exactly
    the secret the job is told to read" — not from the policy the code happens to
    emit.
    """

    def _role(self, monkeypatch, pipeline_id: str, target_config: dict, connection_props: dict | None = None):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        # Several test modules install a MagicMock as sys.modules["env_utils"] at
        # module scope, so in a full tests/unit/ run iam_roles would bind a mocked
        # get_aws_account_id and these exact-ARN assertions would compare against a
        # mock repr. Same remedy as test_source_sample_write_grant.py: load the real
        # shared/env_utils.py for the duration of this test only.
        env_spec = importlib.util.spec_from_file_location("env_utils", os.path.join(SHARED_DIR, "env_utils.py"))
        real_env_utils = importlib.util.module_from_spec(env_spec)
        env_spec.loader.exec_module(real_env_utils)
        monkeypatch.setitem(sys.modules, "env_utils", real_env_utils)
        iam_roles = _load("iam_roles")
        assert iam_roles.get_aws_account_id() == ACCOUNT, "iam_roles bound a stubbed account resolver"
        iam_roles.iam_client.attach_role_policy = mock.MagicMock()
        monkeypatch.setattr("time.sleep", lambda *_: None)
        if connection_props is not None:
            iam_roles.glue_client.create_connection(
                ConnectionInput={
                    "Name": target_config.get("jdbc_connection_name") or target_config["glue_connection_name"],
                    "ConnectionType": "JDBC",
                    "ConnectionProperties": connection_props,
                }
            )
        role_arn = iam_roles.create_pipeline_specific_role(pipeline_id, target_config, source_config=None)
        policy = _get_inline_policy(
            boto3.client("iam", region_name=REGION), f"AgenticDataIngestion-{pipeline_id}-ExecutionRole"
        )
        return iam_roles, role_arn, policy

    @mock_aws
    def test_grant_names_the_connections_secret_id_arn(self, monkeypatch):
        """A SECRET_ID found only on the connection is granted — the old bug."""
        target_config = {"type": "JDBC", "glue_connection_name": "pg-target", "jdbc_table": "public.t"}
        _, role_arn, policy = self._role(
            monkeypatch,
            "pid-sid",
            target_config,
            {"JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db", "SECRET_ID": _TARGET_SECRET_ARN},
        )
        assert role_arn.endswith("AgenticDataIngestion-pid-sid-ExecutionRole"), (
            "the pipeline-specific role must be built, not the shared fallback role"
        )
        assert _secret_grant_resources(policy) == [_TARGET_SECRET_ARN]

    @mock_aws
    def test_secret_id_name_is_granted_as_its_suffix_wildcard_arn(self, monkeypatch):
        """SECRET_ID may be a NAME; IAM needs an ARN, so it is granted as `name-??????`."""
        target_config = {"type": "JDBC", "glue_connection_name": "pg-by-name", "jdbc_table": "public.t"}
        iam_roles, _, policy = self._role(
            monkeypatch,
            "pid-name",
            target_config,
            {"JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db", "SECRET_ID": "prod/warehouse-db"},
        )
        expected = f"arn:aws:secretsmanager:{iam_roles.AWS_REGION}:{ACCOUNT}:secret:prod/warehouse-db-??????"
        assert _secret_grant_resources(policy) == [expected]

    @mock_aws
    def test_target_config_secret_arn_wins_without_reading_the_connection(self, monkeypatch):
        """``secret_arn`` on the config is granted; no connection need exist."""
        target_config = {
            "type": "JDBC",
            "jdbc_connection_name": "not-created",
            "jdbc_table": "public.t",
            "secret_arn": _TARGET_SECRET_ARN,
        }
        _, _, policy = self._role(monkeypatch, "pid-cfg", target_config)
        assert _secret_grant_resources(policy) == [_TARGET_SECRET_ARN]

    @mock_aws
    def test_resolved_secret_is_written_back_for_the_argument_builder(self, monkeypatch):
        """One resolution: the role builder writes back what the argument builder then reads.

        ``create_glue_job`` passes the same dict to the role builder and then to
        ``build_jdbc_arguments``; after the first, ``secret_arn`` is set, so the
        second emits exactly the granted value without a second GetConnection.
        """
        target_config = {"type": "JDBC", "glue_connection_name": "pg-wb", "jdbc_table": "public.t"}
        _, _, policy = self._role(
            monkeypatch,
            "pid-wb",
            target_config,
            {"JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db", "SECRET_ID": _TARGET_SECRET_ARN},
        )
        assert target_config["secret_arn"] == _TARGET_SECRET_ARN
        ja = _load("job_arguments")
        with mock.patch.object(ja, "_glue_client", side_effect=AssertionError("no second GetConnection")):
            args = ja.build_jdbc_arguments(
                {**target_config, "jdbc_url": "jdbc:postgresql://h:5432/db"}, source_path="s3://b/d"
            )
        assert args["--secret_arn"] == _TARGET_SECRET_ARN
        assert args["--secret_arn"] in _secret_grant_resources(policy)

    @mock_aws
    def test_unresolvable_secret_keeps_the_pipeline_role_and_does_not_raise(self, monkeypatch):
        """A connection without SECRET_ID must not push the job onto the shared role.

        ``create_pipeline_specific_role`` converts ANY exception into the shared
        fallback role (AmazonS3FullAccess) and leaves the pipeline role without its
        inline policy, so the branch logs instead of raising. ``build_jdbc_arguments``
        is the step that refuses — asserted here so the pair is pinned together.
        """
        target_config = {"type": "JDBC", "glue_connection_name": "pg-nosid", "jdbc_table": "public.t"}
        iam_roles, role_arn, policy = self._role(
            monkeypatch,
            "pid-nosid",
            target_config,
            {"JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db"},
        )
        assert role_arn.endswith("AgenticDataIngestion-pid-nosid-ExecutionRole")
        # The pre-fix pipeline-scoped pattern, for a role whose job is never created.
        assert _secret_grant_resources(policy) == [
            f"arn:aws:secretsmanager:{iam_roles.AWS_REGION}:{ACCOUNT}:secret:agentic-data-ingestion/pid-nosid/*"
        ]
        assert "secret_arn" not in target_config
        ja = _load("job_arguments")
        with (
            mock.patch.object(ja, "_glue_client", return_value=_FakeGlue({"JDBC_CONNECTION_URL": "jdbc:x"})),
            pytest.raises(ValueError, match="carries no SECRET_ID"),
        ):
            ja.build_jdbc_arguments(target_config, source_path="s3://b/d")


# ── job_builders._add_jdbc_source_job_wiring ─────────────────────────────


class TestAddJdbcSourceJobWiring:
    def _load_job_builders(self):
        # job_builders imports parameter_builder/metrics/workers — guard the
        # optional ones with mocks so import succeeds standalone.
        with mock.patch.dict(
            sys.modules,
            {
                "metrics_facade": mock.MagicMock(),
                "schema_evolution": mock.MagicMock(),
            },
        ):
            return _load("job_builders")

    def test_attaches_source_connection_and_extra_py_files(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        jb = self._load_job_builders()
        # Stub the S3 upload so we don't need a real bucket/layer file.
        monkeypatch.setattr(
            jb,
            "_ensure_jdbc_helpers_on_s3",
            lambda: f"s3://adi-dependencies-{ACCOUNT}-{REGION}/glue-deps/jdbc_helpers.py",
        )
        job_config: dict = {"DefaultArguments": {"--pipeline-id": "pid-x"}}
        pipeline_metadata = {"source_connection_name": "agentic-data-ingestion-pid-x-source"}
        jb._add_jdbc_source_job_wiring(job_config, pipeline_metadata, {}, "pid-x")

        assert job_config["Connections"] == {"Connections": ["agentic-data-ingestion-pid-x-source"]}
        assert job_config["DefaultArguments"]["--extra-py-files"].endswith("/glue-deps/jdbc_helpers.py")

    def test_merges_with_existing_target_connection(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        jb = self._load_job_builders()
        monkeypatch.setattr(jb, "_ensure_jdbc_helpers_on_s3", lambda: "s3://deps/jdbc_helpers.py")
        # JDBC target already attached its connection.
        job_config = {"Connections": {"Connections": ["target-conn"]}, "DefaultArguments": {}}
        jb._add_jdbc_source_job_wiring(job_config, {"source_connection_name": "src-conn"}, {}, "pid-y")
        assert job_config["Connections"]["Connections"] == ["target-conn", "src-conn"]

    def test_falls_back_to_glue_connection_name_then_deterministic(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        jb = self._load_job_builders()
        monkeypatch.setattr(jb, "_ensure_jdbc_helpers_on_s3", lambda: "s3://deps/jdbc_helpers.py")
        # No source_connection_name on the record → use glue_connection_name.
        job_config = {"DefaultArguments": {}}
        jb._add_jdbc_source_job_wiring(job_config, {}, {"glue_connection_name": "user-conn"}, "pid-z")
        assert job_config["Connections"]["Connections"] == ["user-conn"]

        # Neither present → deterministic agentic-data-ingestion-{pid}-source.
        job_config2 = {"DefaultArguments": {}}
        jb._add_jdbc_source_job_wiring(job_config2, {}, {}, "pid-z")
        assert job_config2["Connections"]["Connections"] == ["agentic-data-ingestion-pid-z-source"]

    def test_extra_py_files_appends_preserving_existing(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        jb = self._load_job_builders()
        monkeypatch.setattr(jb, "_ensure_jdbc_helpers_on_s3", lambda: "s3://deps/jdbc_helpers.py")
        job_config = {"DefaultArguments": {"--extra-py-files": "s3://deps/other.py"}}
        jb._add_jdbc_source_job_wiring(job_config, {"source_connection_name": "c"}, {}, "pid-q")
        assert job_config["DefaultArguments"]["--extra-py-files"] == ("s3://deps/other.py,s3://deps/jdbc_helpers.py")


# ── job_arguments.build_jdbc_arguments — glue_connection-mode target ─────


class TestBuildJdbcArgumentsGlueConnectionMode:
    """The JDBC target builder must accept the wizard's glue_connection shape.

    Regression guard for "jdbc_connection_name is required for JDBC target":
    glue_connection-mode targets persist ``glue_connection_name`` (not
    ``jdbc_connection_name``) and supply ``jdbc_table`` directly (the database
    is embedded in the JDBC URL), so the old hard requirement on
    ``jdbc_connection_name`` + ``database_name``/``table_name`` rejected every
    such pipeline at CreateGluePipeline.
    """

    # Fixtures in this class carry `secret_arn` + `jdbc_url` where they used to carry
    # `jdbc_user` / `jdbc_password`. The tests are about the connection-name and
    # table-identity rules, not credentials; the inline pair only stood in for "the
    # credential is resolved", and since `LGR-819980f-1`'s fix that role belongs to
    # a Secrets Manager reference. `jdbc_url` is supplied so the URL backfill has
    # nothing to read either: without it the builder calls GetConnection, which in
    # a unit test means a real AWS call on any machine with ambient credentials.

    def _load_job_arguments(self):
        return _load("job_arguments")

    def test_glue_connection_name_accepted_as_connection(self):
        ja = self._load_job_arguments()
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "agentic-data-ingestion-test-fixtures-postgres-target",
            "jdbc_url": "jdbc:postgresql://host:5432/testdb",
            "jdbc_driver": "org.postgresql.Driver",
            "jdbc_table": "public.customers",
            "secret_arn": _TARGET_SECRET_ARN,
            "write_mode": "overwrite",
        }
        args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")
        assert args["--jdbc-connection-name"] == "agentic-data-ingestion-test-fixtures-postgres-target"
        assert args["--target_jdbc_table"] == "public.customers"

    def test_jdbc_table_derived_from_database_and_table(self):
        ja = self._load_job_arguments()
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "conn",
            "database_name": "analytics",
            "table_name": "users_mirror",
            "jdbc_url": "jdbc:postgresql://host:5432/analytics",
            "secret_arn": _TARGET_SECRET_ARN,
            "write_mode": "append",
        }
        args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")
        assert args["--target_jdbc_table"] == "analytics.users_mirror"

    def test_explicit_jdbc_table_wins_over_database_table(self):
        ja = self._load_job_arguments()
        cfg = {
            "type": "JDBC",
            "jdbc_connection_name": "conn",
            "jdbc_table": "public.orders",
            "database_name": "ignored",
            "table_name": "ignored",
            "jdbc_url": "jdbc:postgresql://host:5432/orders",
            "secret_arn": _TARGET_SECRET_ARN,
        }
        args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d")
        assert args["--jdbc_table"] == "public.orders"

    def test_missing_connection_raises_clear_error(self):
        ja = self._load_job_arguments()
        cfg = {"type": "JDBC", "jdbc_table": "public.t"}
        with pytest.raises(ValueError, match="Glue connection is required"):
            ja.build_jdbc_arguments(cfg, source_path="s3://b/d")

    def test_missing_table_identity_raises_clear_error(self):
        ja = self._load_job_arguments()
        cfg = {"type": "JDBC", "glue_connection_name": "conn"}
        with pytest.raises(ValueError, match="target table is required"):
            ja.build_jdbc_arguments(cfg, source_path="s3://b/d")


# ── job_arguments.build_jdbc_arguments — target credential resolution ────


def _assert_no_plaintext_credential_argument(args: dict, *plaintext: str) -> None:
    """No ``--*jdbc_user`` / ``--*jdbc_password`` key, and no plaintext value, in ``args``.

    The key check is a pattern rather than two literals so a prefixed spelling
    (``--target_jdbc_password``) or a renamed one (``--target_password``) is
    caught too. The value check is by equality and substring, because a credential
    concatenated into another argument is the same leak.
    """
    leaked_keys = sorted(k for k in args if re.search(r"user|password|passwd|pwd", k, re.IGNORECASE))
    assert not leaked_keys, f"credential argument(s) emitted: {leaked_keys} (LGR-819980f-1)"
    for value in plaintext:
        carriers = sorted(k for k, v in args.items() if value in str(v))
        assert not carriers, f"a plaintext credential value reached argument(s) {carriers} (LGR-819980f-1)"


class TestBuildJdbcArgumentsCredentialResolution:
    """The JDBC target's credential is a Secrets Manager reference, or nothing.

    **These four tests used to assert the opposite**, and each one was rewritten
    rather than deleted because its fixture is still a shape the builder meets:

    * an inline-credential connection used to have its USERNAME/PASSWORD copied
      into ``--target_jdbc_user`` / ``--target_jdbc_password``;
    * a SECRET_ID connection used to have its secret DEREFERENCED by this Lambda
      (``GetSecretValue``) and the plaintext copied into the same two arguments;
    * inline ``target_config.jdbc_user`` / ``jdbc_password`` used to "win";
    * a connection with neither used to raise "credentials could not be resolved".

    Every one of those put a plaintext credential in Glue job arguments — visible
    in the Glue console, CloudTrail and job-run history — which is exactly what
    standing invariant ``LGR-819980f-1`` forbids, and the inline arm is what leaked
    a target credential. The expectations below are derived from the invariant,
    not from the current output: the builder emits ``--{prefix}secret_arn`` (a
    reference), never dereferences it, and raises rather than falling back when no
    reference exists.
    """

    @mock_aws
    def test_inline_connection_credentials_are_refused_not_copied(self):
        """A connection with inline USERNAME/PASSWORD and no SECRET_ID cannot be used.

        Was ``test_inline_username_password_resolved_from_connection``, which
        asserted the connection's password landed in ``--target_jdbc_password``.
        """
        # The Glue client is created on first use and then cached, so calling the
        # accessor inside the mock_aws context binds it to the moto-backed client.
        ja = _load("job_arguments")
        ja._glue_client().create_connection(
            ConnectionInput={
                "Name": "agentic-data-ingestion-test-fixtures-postgres-target",
                "ConnectionType": "JDBC",
                "ConnectionProperties": {
                    "JDBC_CONNECTION_URL": "jdbc:postgresql://host:5432/testdb",
                    "USERNAME": _PLAINTEXT_USER,
                    "PASSWORD": _PLAINTEXT_PASSWORD,
                },
            }
        )
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "agentic-data-ingestion-test-fixtures-postgres-target",
            "jdbc_table": "public.customers",
            "jdbc_driver": "org.postgresql.Driver",
            "write_mode": "overwrite",
        }
        with pytest.raises(ValueError, match="carries no SECRET_ID") as excinfo:
            ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")
        message = str(excinfo.value)
        assert _PLAINTEXT_USER not in message
        assert _PLAINTEXT_PASSWORD not in message
        assert "secret_arn" not in cfg, "nothing may be written back when no reference resolved"

    @mock_aws
    def test_secret_id_arn_is_passed_as_the_reference_never_dereferenced(self):
        """A SECRET_ID connection yields ``--target_secret_arn``, not the secret's contents.

        Was ``test_secret_id_credentials_resolved_from_secrets_manager``, which
        created a real (moto) secret and asserted its username/password came back
        as job arguments. No secret is created here on purpose: the builder must
        succeed without one existing, because it must never read it.
        """
        ja = _load("job_arguments")
        ja._glue_client().create_connection(
            ConnectionInput={
                "Name": "conn-with-secret",
                "ConnectionType": "JDBC",
                "ConnectionProperties": {
                    "JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db",
                    "SECRET_ID": _TARGET_SECRET_ARN,
                },
            }
        )
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "conn-with-secret",
            "jdbc_table": "public.customers",
        }
        args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")
        assert args["--target_secret_arn"] == _TARGET_SECRET_ARN
        # URL backfilled from the connection when target_config omits it.
        assert args["--target_jdbc_url"] == "jdbc:postgresql://h:5432/db"
        _assert_no_plaintext_credential_argument(args)
        # Written back, so the IAM grant and this argument name the same secret.
        assert cfg["secret_arn"] == _TARGET_SECRET_ARN

    def test_secret_id_bare_name_is_passed_unchanged(self):
        """SECRET_ID may be a secret NAME; the job's get_secret_value accepts it as-is.

        Only the IAM grant needs it widened to an ARN pattern
        (``secretsmanager_resource_arn``); the argument is the reference verbatim.
        """
        ja = _load("job_arguments")
        fake = _FakeGlue({"JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db", "SECRET_ID": "prod/warehouse-db"})
        cfg = {"type": "JDBC", "glue_connection_name": "by-name", "jdbc_table": "public.t"}
        with mock.patch.object(ja, "_glue_client", return_value=fake):
            args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d")
        assert args["--secret_arn"] == "prod/warehouse-db"
        _assert_no_plaintext_credential_argument(args)

    def test_inline_target_config_creds_are_ignored_not_forwarded(self):
        """``target_config.jdbc_user`` / ``jdbc_password`` no longer "win" — they are ignored.

        Was ``test_inline_target_config_creds_win_over_connection``. The pair is
        what a record written before the dashboard API refused it can still carry;
        the builder must take the connection's SECRET_ID and drop the pair.
        """
        ja = _load("job_arguments")
        fake = _FakeGlue({"JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db", "SECRET_ID": _TARGET_SECRET_ARN})
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "conn-x",
            "jdbc_table": "public.t",
            "jdbc_user": _PLAINTEXT_USER,
            "jdbc_password": _PLAINTEXT_PASSWORD,
        }
        with mock.patch.object(ja, "_glue_client", return_value=fake):
            args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")
        assert args["--target_secret_arn"] == _TARGET_SECRET_ARN
        _assert_no_plaintext_credential_argument(args, _PLAINTEXT_USER, _PLAINTEXT_PASSWORD)

    def test_inline_target_config_creds_do_not_rescue_a_missing_secret(self):
        """With no reference anywhere, the inline pair is NOT a fallback: it raises."""
        ja = _load("job_arguments")
        fake = _FakeGlue({"JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db"})
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "conn-no-secret",
            "jdbc_table": "public.t",
            "jdbc_user": _PLAINTEXT_USER,
            "jdbc_password": _PLAINTEXT_PASSWORD,
        }
        with (
            mock.patch.object(ja, "_glue_client", return_value=fake),
            pytest.raises(ValueError, match="carries no SECRET_ID") as excinfo,
        ):
            ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")
        assert _PLAINTEXT_USER not in str(excinfo.value)
        assert _PLAINTEXT_PASSWORD not in str(excinfo.value)

    @mock_aws
    def test_connection_without_secret_id_raises_clear_error(self):
        """Was ``test_connection_without_credentials_raises_clear_error``; the message changed.

        The old message offered inline USERNAME/PASSWORD or jdbc_user/jdbc_password
        as remedies, both of which the invariant forbids. The new one must name the
        connection, SECRET_ID, and the same-call removal of the inline pair that
        Glue's mutual-exclusivity rule makes necessary.
        """
        ja = _load("job_arguments")
        ja._glue_client().create_connection(
            ConnectionInput={
                "Name": "conn-no-creds",
                "ConnectionType": "JDBC",
                "ConnectionProperties": {
                    "JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db",
                },
            }
        )
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "conn-no-creds",
            "jdbc_table": "public.t",
        }
        with pytest.raises(ValueError, match="carries no SECRET_ID") as excinfo:
            ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")
        message = str(excinfo.value)
        assert "'conn-no-creds'" in message
        assert "UpdateConnection" in message
        assert "USERNAME/PASSWORD" in message


class TestNoCodePathEmitsAPlaintextCredentialArgument:
    """``LGR-819980f-1`` over every reference source, both key prefixes, and any pair.

    Quantified over the credential VALUES, because the property is "whatever the
    target config carries, none of it reaches an argument" — a fixed placeholder
    would only show that one string is dropped.
    """

    @given(
        user=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=12, max_size=24),
        password=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#%", min_size=12, max_size=24),
        key_prefix=st.sampled_from(["", "target_"]),
        reference_on=st.sampled_from(["target_config", "connection"]),
    )
    @settings(max_examples=10)
    def test_inline_pair_never_reaches_a_job_argument(self, user, password, key_prefix, reference_on):
        ja = _load("job_arguments")
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "pg-target",
            "jdbc_url": "jdbc:postgresql://h:5432/db",
            "jdbc_table": "public.t",
            "jdbc_user": user,
            "jdbc_password": password,
        }
        props = {"JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db"}
        if reference_on == "target_config":
            cfg["secret_arn"] = _TARGET_SECRET_ARN
        else:
            props["SECRET_ID"] = _TARGET_SECRET_ARN
        with mock.patch.object(ja, "_glue_client", return_value=_FakeGlue(props)):
            args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix=key_prefix)
        assert args[f"--{key_prefix}secret_arn"] == _TARGET_SECRET_ARN
        _assert_no_plaintext_credential_argument(args, user, password)


class TestBuildJdbcArgumentsLogsNoCredential:
    """The INFO line names argument KEYS, and no log line carries a credential.

    The INFO line used to interpolate the whole argument dict, which put the
    plaintext ``--{prefix}jdbc_password`` into CloudWatch on every inline-credential
    pipeline — the same leak as the job argument, one console over. The logger is
    replaced with a mock so every call at every level is inspected, not only the
    records a handler happens to capture.
    """

    _CONNECTION_PASSWORD = "placeholder-connection-password-not-a-credential"

    @staticmethod
    def _logged(mock_logger) -> list[str]:
        out: list[str] = []
        for level in ("debug", "info", "warning", "error", "exception", "critical"):
            for call in getattr(mock_logger, level).call_args_list:
                out.append(" ".join(str(a) for a in call.args))
        return out

    def test_info_line_carries_key_names_and_no_credential_or_reference(self):
        ja = _load("job_arguments")
        # The connection notionally still holds an inline PASSWORD. Real Glue would
        # refuse it alongside SECRET_ID, but the fake stands for "the connection has
        # a password the Lambda must never see" — and drops it only when asked with
        # HidePassword=True, so this also pins that the read asks.
        fake = _FakeGlue(
            {
                "JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db",
                "SECRET_ID": _TARGET_SECRET_ARN,
                "PASSWORD": self._CONNECTION_PASSWORD,
            }
        )
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "pg-target",
            "jdbc_table": "public.t",
            "jdbc_user": _PLAINTEXT_USER,
            "jdbc_password": _PLAINTEXT_PASSWORD,
        }
        with (
            mock.patch.object(ja, "_glue_client", return_value=fake),
            mock.patch.object(ja, "logger") as mock_logger,
        ):
            args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")

        info_lines = [line for line in self._logged(mock_logger) if "Built JDBC job arguments" in line]
        assert len(info_lines) == 1, f"expected one summary line, got {info_lines}"
        info = info_lines[0]
        for key in args:
            assert key in info, f"the summary line must name every emitted key; {key} missing"
        # Key names only: not even the reference, so a future credential-bearing
        # key cannot reopen the hole by being added to the dict.
        assert _TARGET_SECRET_ARN not in info
        for line in self._logged(mock_logger):
            for value in (_PLAINTEXT_USER, _PLAINTEXT_PASSWORD, self._CONNECTION_PASSWORD):
                assert value not in line, f"a credential reached a log line: {line!r}"
        # The ignored inline pair is reported by KEY name.
        assert any("jdbc_password" in line and "ignored" in line for line in self._logged(mock_logger))
        assert all(call.get("HidePassword") is True for call in fake.calls), fake.calls

    def test_error_path_logs_no_connection_credential(self):
        """The refusal is logged at ERROR and raised; neither carries the connection's pair."""
        ja = _load("job_arguments")
        fake = _FakeGlue(
            {
                "JDBC_CONNECTION_URL": "jdbc:postgresql://h:5432/db",
                "USERNAME": _PLAINTEXT_USER,
                "PASSWORD": self._CONNECTION_PASSWORD,
            }
        )
        cfg = {"type": "JDBC", "glue_connection_name": "inline-only", "jdbc_table": "public.t"}
        with (
            mock.patch.object(ja, "_glue_client", return_value=fake),
            mock.patch.object(ja, "logger") as mock_logger,
            pytest.raises(ValueError) as excinfo,
        ):
            ja.build_jdbc_arguments(cfg, source_path="s3://b/d")
        for text in [*self._logged(mock_logger), str(excinfo.value)]:
            assert _PLAINTEXT_USER not in text
            assert self._CONNECTION_PASSWORD not in text


# ── job_arguments.resolve_jdbc_target_secret / secretsmanager_resource_arn ──


class TestResolveJdbcTargetSecret:
    """The one resolver behind both the job argument and the IAM grant.

    Order, from its contract: ``target_config.secret_arn``, else the named
    connection's ``ConnectionProperties.SECRET_ID``, else ``ValueError``. It never
    dereferences the secret; ``_FakeGlue`` records every connection read so each
    test can say whether Glue was consulted.
    """

    def test_target_config_secret_arn_wins_without_reading_glue(self):
        ja = _load("job_arguments")
        fake = _FakeGlue({"SECRET_ID": "arn:aws:secretsmanager:us-west-2:123456789012:secret:other-ZzZzZz"})
        cfg = {"glue_connection_name": "pg", "secret_arn": _TARGET_SECRET_ARN}
        assert ja.resolve_jdbc_target_secret(cfg, glue_client=fake) == _TARGET_SECRET_ARN
        assert fake.calls == [], "a reference already on the config must not cost a GetConnection"

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_secret_arn_falls_through_to_the_connection(self, empty):
        """``or``, not a ``.get`` default: a stored ``""`` / ``None`` is not a reference."""
        ja = _load("job_arguments")
        fake = _FakeGlue({"SECRET_ID": _TARGET_SECRET_ARN})
        cfg = {"glue_connection_name": "pg", "secret_arn": empty}
        assert ja.resolve_jdbc_target_secret(cfg, glue_client=fake) == _TARGET_SECRET_ARN

    @pytest.mark.parametrize(
        "secret_id",
        [_TARGET_SECRET_ARN, "prod/warehouse-db"],
        ids=["arn", "bare-name"],
    )
    def test_falls_back_to_secret_id_and_writes_it_back(self, secret_id):
        ja = _load("job_arguments")
        fake = _FakeGlue({"JDBC_CONNECTION_URL": "jdbc:x", "SECRET_ID": secret_id})
        cfg = {"glue_connection_name": "pg"}
        assert ja.resolve_jdbc_target_secret(cfg, glue_client=fake) == secret_id
        assert cfg["secret_arn"] == secret_id
        assert fake.calls == [{"Name": "pg", "HidePassword": True}]

    def test_jdbc_connection_name_is_read_before_glue_connection_name(self):
        """The same precedence as the job's attached connection and the IAM grant."""
        ja = _load("job_arguments")
        fake = _FakeGlue({"SECRET_ID": _TARGET_SECRET_ARN})
        cfg = {"jdbc_connection_name": "attached", "glue_connection_name": "other"}
        ja.resolve_jdbc_target_secret(cfg, glue_client=fake)
        assert [c["Name"] for c in fake.calls] == ["attached"]

    def test_uses_the_module_client_when_none_is_passed(self):
        ja = _load("job_arguments")
        fake = _FakeGlue({"SECRET_ID": _TARGET_SECRET_ARN})
        with mock.patch.object(ja, "_glue_client", return_value=fake):
            assert ja.resolve_jdbc_target_secret({"glue_connection_name": "pg"}) == _TARGET_SECRET_ARN
        assert len(fake.calls) == 1

    def test_raises_with_remediation_when_no_connection_is_named(self):
        ja = _load("job_arguments")
        fake = _FakeGlue()
        cfg = {"jdbc_user": _PLAINTEXT_USER, "jdbc_password": _PLAINTEXT_PASSWORD}
        with pytest.raises(ValueError) as excinfo:
            ja.resolve_jdbc_target_secret(cfg, glue_client=fake)
        message = str(excinfo.value)
        assert "secret_arn" in message
        assert "SECRET_ID" in message
        assert "glue_connection_name" in message
        assert _PLAINTEXT_PASSWORD not in message
        assert fake.calls == []

    def test_raises_with_remediation_when_the_connection_has_no_secret_id(self):
        ja = _load("job_arguments")
        fake = _FakeGlue({"JDBC_CONNECTION_URL": "jdbc:x", "USERNAME": _PLAINTEXT_USER, "PASSWORD": "pw-x"})
        cfg = {"glue_connection_name": "inline-only"}
        with pytest.raises(ValueError) as excinfo:
            ja.resolve_jdbc_target_secret(cfg, glue_client=fake)
        message = str(excinfo.value)
        assert "'inline-only'" in message
        assert "carries no SECRET_ID" in message
        # The mutual-exclusivity trap: adding SECRET_ID alone is rejected by Glue.
        assert "USERNAME/PASSWORD" in message and "UpdateConnection" in message
        assert _PLAINTEXT_USER not in message
        assert "secret_arn" not in cfg

    def test_unreadable_connection_raises_value_error(self):
        from botocore.exceptions import ClientError

        ja = _load("job_arguments")
        error = ClientError({"Error": {"Code": "EntityNotFoundException", "Message": "nope"}}, "GetConnection")
        with pytest.raises(ValueError, match="Failed to read Glue Connection 'gone'"):
            ja.resolve_jdbc_target_secret({"glue_connection_name": "gone"}, glue_client=_FakeGlue(error=error))


class TestSecretsmanagerResourceArn:
    """IAM needs an ARN; SECRET_ID may be a name. The name maps to its 6-char-suffix form."""

    def test_arn_is_returned_unchanged(self):
        ja = _load("job_arguments")
        assert ja.secretsmanager_resource_arn(_TARGET_SECRET_ARN, "us-west-2", ACCOUNT) == _TARGET_SECRET_ARN

    @pytest.mark.parametrize("name", ["warehouse-db", "prod/warehouse-db"])
    def test_name_becomes_the_suffix_wildcard_arn(self, name):
        ja = _load("job_arguments")
        assert ja.secretsmanager_resource_arn(name, "eu-west-1", "111122223333") == (
            f"arn:aws:secretsmanager:eu-west-1:111122223333:secret:{name}-??????"
        )


# ── job_arguments.build_jdbc_arguments — secret-ARN-at-runtime mode ──────


class TestBuildJdbcArgumentsSecretArnMode:
    """When target_config carries a secret_arn (secret-backed connection — the
    dashboard API copies the connection's SECRET_ID onto target_config at
    pipeline-creation time), the Glue job args must pass ONLY the secret ARN,
    NOT plaintext user/password.

    Security guard: Glue job arguments are visible in the console, CloudTrail,
    and job-run history. Emitting --target_jdbc_password as a plaintext arg
    leaks the target credential. The generated write block fetches the secret
    at runtime instead (mirrors the source-side pattern).
    """

    _SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/tgt-AbCdEf"

    def test_secret_arn_emitted_and_plaintext_creds_omitted(self):
        ja = _load("job_arguments")
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "pg-target",
            "jdbc_url": "jdbc:postgresql://h:5432/testdb",
            "jdbc_driver": "org.postgresql.Driver",
            "jdbc_table": "public.customers",
            "secret_arn": self._SECRET_ARN,
            "write_mode": "overwrite",
        }
        args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")

        # Secret ARN (a reference) is passed.
        assert args["--target_secret_arn"] == self._SECRET_ARN
        # Plaintext credential args are NOT present.
        assert "--target_jdbc_user" not in args
        assert "--target_jdbc_password" not in args
        # URL / driver / table still flow through.
        assert args["--target_jdbc_url"] == "jdbc:postgresql://h:5432/testdb"
        assert args["--target_jdbc_table"] == "public.customers"

    def test_secret_arn_mode_does_not_require_inline_creds(self):
        """With a secret ARN, absent inline user/password must NOT raise —
        the runtime fetch supplies them."""
        ja = _load("job_arguments")
        cfg = {
            "type": "JDBC",
            "glue_connection_name": "pg-target",
            "jdbc_url": "jdbc:postgresql://h:5432/testdb",
            "jdbc_driver": "org.postgresql.Driver",
            "jdbc_table": "public.customers",
            "secret_arn": self._SECRET_ARN,
        }
        # Must not raise "credentials could not be resolved".
        args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d", key_prefix="target_")
        assert args["--target_secret_arn"] == self._SECRET_ARN

    def test_no_prefix_secret_arn_key(self):
        """Default key_prefix yields the unprefixed --secret_arn key."""
        ja = _load("job_arguments")
        cfg = {
            "type": "JDBC",
            "jdbc_connection_name": "pg-target",
            "jdbc_url": "jdbc:postgresql://h:5432/testdb",
            "jdbc_driver": "org.postgresql.Driver",
            "jdbc_table": "public.customers",
            "secret_arn": self._SECRET_ARN,
        }
        args = ja.build_jdbc_arguments(cfg, source_path="s3://b/d")
        assert args["--secret_arn"] == self._SECRET_ARN
        assert "--jdbc_password" not in args


# ── job_builders._attach_jdbc_target_connection — target VPC wiring ──────


class TestAttachJdbcTargetConnection:
    """The JDBC target's Glue connection must be attached to the job so the
    job's ENIs land in the target VPC and the Spark write.jdbc() can reach the
    target database.

    Regression guard for the write-phase failure
    ``org.postgresql.util.PSQLException: The connection attempt failed`` —
    caused by (a) reading only ``jdbc_connection_name`` so glue_connection-mode
    targets (which persist ``glue_connection_name``) attached NO connection,
    and (b) clobbering instead of merging, which would drop a JDBC source
    connection on a JDBC-source → JDBC-target pipeline.
    """

    def _load_job_builders(self):
        return _load("job_builders")

    def test_glue_connection_name_alias_is_attached(self):
        jb = self._load_job_builders()
        job_config: dict = {}
        jb._attach_jdbc_target_connection(
            job_config,
            {"type": "JDBC", "glue_connection_name": "agentic-data-ingestion-test-fixtures-postgres-target"},
        )
        assert job_config["Connections"] == {"Connections": ["agentic-data-ingestion-test-fixtures-postgres-target"]}

    def test_jdbc_connection_name_still_attached(self):
        jb = self._load_job_builders()
        job_config: dict = {}
        jb._attach_jdbc_target_connection(job_config, {"type": "JDBC", "jdbc_connection_name": "my-conn"})
        assert job_config["Connections"] == {"Connections": ["my-conn"]}

    def test_target_connection_merges_with_existing_source_connection(self):
        """JDBC source + JDBC target: BOTH connections must remain attached."""
        jb = self._load_job_builders()
        job_config = {"Connections": {"Connections": ["agentic-data-ingestion-pid-source"]}}
        jb._attach_jdbc_target_connection(job_config, {"type": "JDBC", "glue_connection_name": "pg-target"})
        assert job_config["Connections"]["Connections"] == [
            "agentic-data-ingestion-pid-source",
            "pg-target",
        ]

    def test_duplicate_connection_not_added_twice(self):
        jb = self._load_job_builders()
        job_config = {"Connections": {"Connections": ["pg-target"]}}
        jb._attach_jdbc_target_connection(job_config, {"type": "JDBC", "glue_connection_name": "pg-target"})
        assert job_config["Connections"]["Connections"] == ["pg-target"]

    def test_no_connection_name_is_noop(self):
        jb = self._load_job_builders()
        job_config: dict = {}
        jb._attach_jdbc_target_connection(job_config, {"type": "JDBC"})
        assert "Connections" not in job_config


# ── job_builders.create_glue_job — the whole JDBC-target credential path ──


class TestCreateGlueJobJdbcTargetCredentialPath:
    """End to end through ``create_glue_job``: role, arguments, then CreateJob.

    The two halves of the fix meet here, on ONE ``target_config`` dict, in the
    order production runs them (``create_pipeline_specific_role`` at
    ``job_builders.py:465``, then ``build_jdbc_arguments`` at ``:576``):

    * the Lambda never calls ``secretsmanager:GetSecretValue`` — asserted twice,
      on a patched Secrets Manager client AND on every botocore API call made, so
      a client built some other way is caught too;
    * the DefaultArguments handed to Glue carry ``--{prefix}secret_arn`` and no
      ``--*jdbc_user`` / ``--*jdbc_password``, even though the stored target config
      still carries an inline pair;
    * the role is granted exactly the secret the job is told to read.

    IAM and the Glue *connection* are moto; the Glue *job* call is a mock so the
    exact CreateJob request can be read back. Both sources are covered, because
    the JDBC-source template prefixes the target keys with ``target_``.
    """

    _PIPELINE_ID = "pid-e2e"

    def _run(self, monkeypatch, source_type: str):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        monkeypatch.setattr("time.sleep", lambda *_: None)

        # Every Secrets Manager client any module builds from here on is this mock.
        secretsmanager = mock.MagicMock(name="secretsmanager")
        real_client = boto3.client

        def _client(service_name, *args, **kwargs):
            if service_name == "secretsmanager":
                return secretsmanager
            return real_client(service_name, *args, **kwargs)

        monkeypatch.setattr(boto3, "client", _client)

        # Every botocore API call, whichever client makes it.
        operations: list[tuple[str, str]] = []
        real_call = BaseClient._make_api_call

        def _spy(client_self, operation_name, api_params):
            operations.append((client_self.meta.service_model.service_name, operation_name))
            return real_call(client_self, operation_name, api_params)

        monkeypatch.setattr(BaseClient, "_make_api_call", _spy)

        iam_roles = _load("iam_roles")
        iam_roles.iam_client.attach_role_policy = mock.MagicMock()
        ja = _load("job_arguments")
        with mock.patch.dict(sys.modules, {"metrics_facade": mock.MagicMock(), "schema_evolution": mock.MagicMock()}):
            jb = _load("job_builders")

        # Bind job_builders to the modules loaded inside this mock_aws context, so
        # no client cached by an earlier test (possibly a real one) is reached.
        monkeypatch.setattr(jb, "create_pipeline_specific_role", iam_roles.create_pipeline_specific_role)
        monkeypatch.setattr(jb, "build_jdbc_arguments", ja.build_jdbc_arguments)
        monkeypatch.setattr(jb, "build_jdbc_source_arguments", ja.build_jdbc_source_arguments)
        monkeypatch.setattr(jb, "_ensure_jdbc_helpers_on_s3", lambda: "s3://deps/jdbc_helpers.py")
        monkeypatch.setattr(jb, "PARAMETER_BUILDER_AVAILABLE", False)
        monkeypatch.setattr(jb, "METRICS_AVAILABLE", False)

        glue_jobs = mock.MagicMock(name="glue-jobs")
        glue_jobs.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        glue_jobs.get_job.side_effect = glue_jobs.exceptions.EntityNotFoundException("no job yet")
        monkeypatch.setattr(jb, "glue_client", glue_jobs)

        item: dict = {"pipeline_id": self._PIPELINE_ID, "source_type": source_type}
        if source_type == "JDBC":
            item["source_config"] = {
                "jdbc_url": "jdbc:mysql://src:3306/app",
                "jdbc_driver": "com.mysql.cj.jdbc.Driver",
                "jdbc_schema": "app",
                "jdbc_table": "customers",
                "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:src-QqQqQq",
                "read_mode": "full_table",
            }
        ddb = mock.MagicMock(name="dynamodb")
        ddb.Table.return_value.get_item.return_value = {"Item": item}
        monkeypatch.setattr(jb, "dynamodb", ddb)

        iam_roles.glue_client.create_connection(
            ConnectionInput={
                "Name": "pg-target",
                "ConnectionType": "JDBC",
                "ConnectionProperties": {
                    "JDBC_CONNECTION_URL": "jdbc:postgresql://tgt:5432/db",
                    "SECRET_ID": _TARGET_SECRET_ARN,
                },
            }
        )
        # A record written before the dashboard API refused the inline pair.
        target_config = {
            "type": "JDBC",
            "glue_connection_name": "pg-target",
            "jdbc_url": "jdbc:postgresql://tgt:5432/db",
            "jdbc_driver": "org.postgresql.Driver",
            "jdbc_table": "public.customers",
            "write_mode": "append",
            "jdbc_user": _PLAINTEXT_USER,
            "jdbc_password": _PLAINTEXT_PASSWORD,
        }
        jb.create_glue_job(
            self._PIPELINE_ID,
            "JDBC",
            {"name": "tgt", "fields": []},
            "s3://b/code.py",
            target_config=target_config,
            run_id="run-1",
            source_path="s3://src/pid-e2e/data.csv",
        )
        create_kwargs = glue_jobs.create_job.call_args.kwargs
        policy = _get_inline_policy(
            boto3.client("iam", region_name=REGION), f"AgenticDataIngestion-{self._PIPELINE_ID}-ExecutionRole"
        )
        return secretsmanager, operations, create_kwargs, policy

    @pytest.mark.parametrize(("source_type", "key_prefix"), [("S3_UPLOAD", ""), ("JDBC", "target_")])
    @mock_aws
    def test_no_secret_value_read_and_no_plaintext_argument(self, monkeypatch, source_type, key_prefix):
        secretsmanager, operations, create_kwargs, policy = self._run(monkeypatch, source_type)

        secretsmanager.get_secret_value.assert_not_called()
        # The spy is live (it saw the role being built), so its silence on
        # GetSecretValue is evidence rather than an empty list.
        assert ("iam", "CreateRole") in operations, operations
        assert ("secretsmanager", "GetSecretValue") not in operations, operations

        default_arguments = create_kwargs["DefaultArguments"]
        assert default_arguments[f"--{key_prefix}secret_arn"] == _TARGET_SECRET_ARN
        _assert_no_plaintext_credential_argument(default_arguments, _PLAINTEXT_USER, _PLAINTEXT_PASSWORD)
        assert _TARGET_SECRET_ARN in _secret_grant_resources(policy), (
            "the role must be able to read the secret the job is told to read"
        )
        assert "pg-target" in create_kwargs["Connections"]["Connections"]
