# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for JDBC pipeline-lifecycle helpers (Task 5.6).

Covers Requirement 22.1: the source-side connection provisioner, the
runtime crawler provisioner, rollback semantics in ``create_pipeline``,
and the JDBC cleanup blocks (5i / 5j / 5k) in ``delete_pipeline``.

Test groups:
    1. ``TestBuildJdbcSourcePhysicalRequirements`` — pure helper that
       builds the ``PhysicalConnectionRequirements`` shape from
       ``source_config`` networking fields.
    2. ``TestProvisionJdbcSourceConnection`` — direct unit tests for
       ``_provision_jdbc_source_connection`` covering all three
       connection modes: ``select_rds`` and ``glue_connection``
       (Req 11.1 / 11.2) plus ``manual`` (P20; p31 R9.6).
    3. ``TestProvisionJdbcRuntimeCrawler`` — direct unit tests for
       ``_provision_jdbc_runtime_crawler`` including idempotent fall-
       through and missing-role configuration error (Req 11.8).
    4. ``TestCreatePipelineJdbcRollback`` — integration-style tests
       exercising ``create_pipeline`` end-to-end with the source /
       crawler helpers patched, asserting that DDB rows AND any
       AgenticDataIngestion-owned Glue Connection are rolled back on failure
       (Req 11.5).
    5. ``TestDeletePipelineJdbcCleanup`` — integration-style tests
       exercising ``delete_pipeline`` against archived JDBC pipelines,
       asserting that the runtime crawler, discovery-DB tables, and
       (in ``select_rds`` mode only) the source connection are deleted,
       AND that ``EntityNotFoundException`` from any of the three
       Glue calls is treated as success (Req 11.8).

Validates: Requirements 22.1
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler. Mirrors
# the bootstrap from ``test_create_pipeline_jdbc_source.py`` and
# ``test_pipelines_delete.py`` so the dashboard API handler imports
# cleanly in a unit-test process without the real Lambda layer.
# ---------------------------------------------------------------------------

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "agentic-data-ingestion-source",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "PIPELINE_TRIGGER_ARN": "",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

# Make ``shared/`` importable so ``handlers.pipelines`` can resolve its
# ``from jdbc_helpers import ...`` line.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

# Add handler package to path
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)

try:
    from handlers import pipelines
    from handlers.pipelines import (
        _build_jdbc_source_physical_requirements,
        _provision_jdbc_runtime_crawler,
        _provision_jdbc_source_connection,
        create_pipeline,
        delete_pipeline,
        edit_pipeline,
    )
    from handlers.pipelines_jdbc import _reject_plaintext_target_credentials
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

PIPELINE_ID = "lifecycle-test-abc12345"

_SELECT_RDS_NETWORKING: dict = {
    "subnet_id": "subnet-0abc1234def5678ab",
    "security_group_ids": ["sg-0aaa", "sg-0bbb"],
    "availability_zone": "us-east-1a",
}

_VALID_SELECT_RDS_SOURCE_CONFIG: dict = {
    "jdbc_url": "jdbc:postgresql://db.example.com:5432/orders",
    "jdbc_driver": "org.postgresql.Driver",
    "jdbc_schema": "public",
    "jdbc_table": "orders",
    "secret_arn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds-AbCdEf"),
    "engine": "postgres",
    "read_mode": "full_table",
    **_SELECT_RDS_NETWORKING,
}

_VALID_MANUAL_SOURCE_CONFIG: dict = {
    # P20 manual mode — the user enters engine / URL / driver / secret and
    # picks VPC + subnet + security groups themselves. `glue_connection_name`
    # is deliberately ABSENT: `JdbcConnectionSelector.handleModeChange`
    # deletes it on every mode transition, which is what routes manual mode
    # down the create-or-update branch of `_provision_jdbc_source_connection`
    # rather than the reuse-the-user's-connection branch.
    "_connection_mode": "manual",
    "jdbc_url": "jdbc:mysql://onprem-db.corp.example:3306/inventory",
    "jdbc_driver": "com.mysql.cj.jdbc.Driver",
    "jdbc_schema": "inventory",
    "jdbc_table": "stock_levels",
    "secret_arn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:onprem-creds-XyZ123"),
    "engine": "mysql",
    "read_mode": "full_table",
    # Networking entered via <ManualConnectionForm>. The wizard's
    # `Record<string, string>` config shape means `security_group_ids`
    # arrives JSON-encoded (Req 9.3).
    "vpc_id": "vpc-0123456789abcdef0",
    "subnet_id": "subnet-0fedcba987654321f",
    "security_group_ids": '["sg-0dddd4444", "sg-0eeee5555"]',
    # `availability_zone` intentionally omitted — manual mode does not
    # require it (see the `_JDBC_MANUAL_REQUIRED_FIELDS` comment); Glue
    # derives it from the subnet.
}

_VALID_GLUE_CONNECTION_SOURCE_CONFIG: dict = {
    # In glue_connection mode the wizard still sends the bare JDBC URL /
    # driver / schema / table because the schema_extract Lambda needs
    # them, but the connection itself is owned by the user.
    "jdbc_url": "jdbc:postgresql://customer-db.internal:5432/orders",
    "jdbc_driver": "org.postgresql.Driver",
    "jdbc_schema": "public",
    "jdbc_table": "orders",
    "secret_arn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds-AbCdEf"),
    "engine": "postgres",
    "read_mode": "full_table",
    "glue_connection_name": "my-existing-connection",
}


def _make_create_body(source_config: dict, *, name: str = "JDBC pipe") -> str:
    """Build a ``create_pipeline`` request body for a JDBC pipeline."""
    return json.dumps(
        {
            "pipeline_name": name,
            "target_schema": "uploaded_test_schema",
            "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
            "source_type": "JDBC",
            "source_config": source_config,
        }
    )


# ---------------------------------------------------------------------------
# Module-level mocks shared across the create_pipeline / delete_pipeline
# integration tests so each test method doesn't have to wire DDB / S3 /
# EventBridge from scratch.
# ---------------------------------------------------------------------------

_mock_pipelines_table = MagicMock()
_mock_state_table = MagicMock()
_mock_executions_table = MagicMock()
_mock_approvals_table = MagicMock()
_mock_task_tokens_table = MagicMock()
_mock_locks_table = MagicMock()
_mock_s3_client = MagicMock()
_mock_dynamodb = MagicMock()


def _table_for(name: str) -> MagicMock:
    return {
        "agentic_data_ingestion_task_tokens": _mock_task_tokens_table,
        "agentic_data_ingestion_pipeline_locks": _mock_locks_table,
    }.get(name, MagicMock())


@pytest.fixture(autouse=True)
def _reset_module_mocks(monkeypatch):
    """Reset shared mocks and rebind them onto the imported module."""
    # This fixture imports `handlers.pipelines_crud` at test time, so this file's
    # env_utils stub has to be in sys.modules then. Module scope restores it after
    # the first import (LGR-c58f276-4); install it again per test here.
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)
    for mock in (
        _mock_pipelines_table,
        _mock_state_table,
        _mock_executions_table,
        _mock_approvals_table,
        _mock_task_tokens_table,
        _mock_locks_table,
        _mock_s3_client,
        _mock_dynamodb,
    ):
        mock.reset_mock(side_effect=True)

    # Sensible defaults — empty pagination, no items.
    _mock_state_table.query.return_value = {"Items": []}
    _mock_executions_table.query.return_value = {"Items": []}
    _mock_approvals_table.scan.return_value = {"Items": []}
    # delete_pipeline scans the pipelines table for other pipelines that still
    # reference a connection before deleting it. Default: no other pipeline.
    _mock_pipelines_table.scan.return_value = {"Items": []}
    _mock_dynamodb.Table.side_effect = _table_for

    pipelines.pipelines_table = _mock_pipelines_table
    pipelines.state_table = _mock_state_table
    pipelines.executions_table = _mock_executions_table
    pipelines.approvals_table = _mock_approvals_table
    pipelines.dynamodb = _mock_dynamodb
    pipelines.s3_client = _mock_s3_client
    # P21 Task 7.2: create_pipeline / delete_pipeline now live in
    # handlers.pipelines_crud; wire the same mocks into the owning module
    # (module references only — behaviour-preserving).
    from handlers import pipelines_crud as _crud

    _crud.pipelines_table = _mock_pipelines_table
    _crud.state_table = _mock_state_table
    _crud.executions_table = _mock_executions_table
    _crud.approvals_table = _mock_approvals_table
    _crud.dynamodb = _mock_dynamodb
    _crud.s3_client = _mock_s3_client
    yield


# ===========================================================================
# Group 1 — _build_jdbc_source_physical_requirements
# ===========================================================================


class TestBuildJdbcSourcePhysicalRequirements:
    """Direct unit tests for the PhysicalConnectionRequirements builder.

    Validates: Requirement 22.1 (and the ``select_rds`` networking
    contract documented in Req 11.4 / 14.5).
    """

    def test_select_rds_returns_full_payload(self):
        result = _build_jdbc_source_physical_requirements(_VALID_SELECT_RDS_SOURCE_CONFIG)
        assert result == {
            "SubnetId": "subnet-0abc1234def5678ab",
            "SecurityGroupIdList": ["sg-0aaa", "sg-0bbb"],
            "AvailabilityZone": "us-east-1a",
        }

    def test_omits_availability_zone_when_missing(self):
        cfg = dict(_VALID_SELECT_RDS_SOURCE_CONFIG)
        del cfg["availability_zone"]

        result = _build_jdbc_source_physical_requirements(cfg)

        assert "AvailabilityZone" not in result, (
            "AvailabilityZone must be omitted entirely when not supplied "
            "— Glue treats it as optional and rejects empty-string values."
        )
        assert result["SubnetId"] == "subnet-0abc1234def5678ab"
        assert result["SecurityGroupIdList"] == ["sg-0aaa", "sg-0bbb"]

    def test_missing_subnet_raises(self):
        cfg = dict(_VALID_SELECT_RDS_SOURCE_CONFIG)
        del cfg["subnet_id"]
        with pytest.raises(ValueError, match=r"source_config\.subnet_id is required"):
            _build_jdbc_source_physical_requirements(cfg)

    def test_missing_security_group_ids_raises(self):
        cfg = dict(_VALID_SELECT_RDS_SOURCE_CONFIG)
        del cfg["security_group_ids"]
        with pytest.raises(
            ValueError,
            match=r"source_config\.security_group_ids must be a non-empty list",
        ):
            _build_jdbc_source_physical_requirements(cfg)

    def test_non_list_security_group_ids_raises(self):
        # P20 — the helper now accepts a JSON-encoded list AS WELL as
        # a real list. A plain non-JSON string still raises, but with
        # the JSON-decode error rather than the "must be a non-empty
        # list" error.
        cfg = dict(_VALID_SELECT_RDS_SOURCE_CONFIG)
        cfg["security_group_ids"] = "sg-0aaa"  # not a JSON list
        with pytest.raises(
            ValueError,
            match=r"source_config\.security_group_ids must be a JSON-encoded list",
        ):
            _build_jdbc_source_physical_requirements(cfg)

    def test_json_encoded_security_group_ids_accepted(self):
        # P20 — the wizard sends `security_group_ids` as a JSON-
        # encoded string because `WizardState.sourceConfig` is shaped
        # `Record<string, string>`. The helper transparently decodes
        # it before passing to Glue.
        cfg = dict(_VALID_SELECT_RDS_SOURCE_CONFIG)
        cfg["security_group_ids"] = '["sg-aaaa1111", "sg-bbbb2222"]'
        physical = _build_jdbc_source_physical_requirements(cfg)
        assert physical["SecurityGroupIdList"] == ["sg-aaaa1111", "sg-bbbb2222"]


# ===========================================================================
# Group 2 — _provision_jdbc_source_connection
# ===========================================================================


class TestProvisionJdbcSourceConnection:
    """Direct unit tests for the source-side Glue connection provisioner.

    Validates: Requirement 22.1 (and Req 11.1 / 11.2 — select_rds creates
    ``agentic-data-ingestion-{pid}-source`` while glue_connection mode skips creation),
    plus p31 Requirement 9.6 — ``manual`` mode shares the create-or-update
    path with ``select_rds``.
    """

    @patch("handlers.pipelines_jdbc.create_glue_jdbc_connection")
    def test_glue_connection_mode_skips_creation_and_returns_user_name(self, mock_create):
        result = _provision_jdbc_source_connection(PIPELINE_ID, _VALID_GLUE_CONNECTION_SOURCE_CONFIG)
        assert result == "my-existing-connection"
        # CRITICAL: AgenticDataIngestion must NEVER touch a user-supplied connection.
        mock_create.assert_not_called()

    @patch("handlers.pipelines_jdbc.create_glue_jdbc_connection")
    def test_select_rds_mode_creates_connection_with_expected_args(self, mock_create):
        result = _provision_jdbc_source_connection(PIPELINE_ID, _VALID_SELECT_RDS_SOURCE_CONFIG)

        # The deterministic per-pipeline name (Req 11.1).
        assert result == f"agentic-data-ingestion-{PIPELINE_ID}-source"
        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        assert kwargs["connection_name"] == f"agentic-data-ingestion-{PIPELINE_ID}-source"
        assert kwargs["jdbc_url"] == _VALID_SELECT_RDS_SOURCE_CONFIG["jdbc_url"]
        assert kwargs["jdbc_driver"] == _VALID_SELECT_RDS_SOURCE_CONFIG["jdbc_driver"]
        assert kwargs["secret_arn"] == _VALID_SELECT_RDS_SOURCE_CONFIG["secret_arn"]
        # PhysicalConnectionRequirements forwarded so Glue routes through
        # the customer's VPC (Req 11.4).
        assert kwargs["physical_requirements"] == {
            "SubnetId": "subnet-0abc1234def5678ab",
            "SecurityGroupIdList": ["sg-0aaa", "sg-0bbb"],
            "AvailabilityZone": "us-east-1a",
        }
        # Source-tag distinct from the target tag (target = "agentic-data-ingestion-target",
        # source = "agentic-data-ingestion-source") so the daily janitor and the
        # delete_pipeline cleanup can tell them apart.
        assert kwargs["tags"] == {
            "Type": "agentic-data-ingestion-source",
            "PipelineId": PIPELINE_ID,
        }

    @patch("handlers.pipelines_jdbc.create_glue_jdbc_connection")
    def test_select_rds_mode_missing_networking_raises_without_creating(self, mock_create):
        bad_config = dict(_VALID_SELECT_RDS_SOURCE_CONFIG)
        del bad_config["subnet_id"]

        with pytest.raises(ValueError, match=r"source_config\.subnet_id is required"):
            _provision_jdbc_source_connection(PIPELINE_ID, bad_config)

        # The validator runs BEFORE the Glue call, so the canonical helper
        # must not be invoked at all.
        mock_create.assert_not_called()

    # -- P20 manual mode (p31 R9.6) -------------------------------------
    # The provisioner branches on the PRESENCE of `glue_connection_name`,
    # not on `_connection_mode`, so manual mode shares the create-or-update
    # path with `select_rds`. These three cases pin that behaviour, which
    # was previously covered for the other two modes only.

    @patch("handlers.pipelines_jdbc.create_glue_jdbc_connection")
    def test_manual_mode_creates_connection_with_expected_args(self, mock_create):
        result = _provision_jdbc_source_connection(PIPELINE_ID, _VALID_MANUAL_SOURCE_CONFIG)

        # Same deterministic per-pipeline name as select_rds — the caller
        # persists one `source_connection_name` regardless of mode.
        assert result == f"agentic-data-ingestion-{PIPELINE_ID}-source"
        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        assert kwargs["connection_name"] == f"agentic-data-ingestion-{PIPELINE_ID}-source"
        assert kwargs["jdbc_url"] == _VALID_MANUAL_SOURCE_CONFIG["jdbc_url"]
        assert kwargs["jdbc_driver"] == _VALID_MANUAL_SOURCE_CONFIG["jdbc_driver"]
        # The wizard's JSON-encoded security_group_ids is decoded to a real
        # list before reaching Glue, and AvailabilityZone is omitted rather
        # than sent empty (Glue rejects empty-string values).
        assert kwargs["physical_requirements"] == {
            "SubnetId": "subnet-0fedcba987654321f",
            "SecurityGroupIdList": ["sg-0dddd4444", "sg-0eeee5555"],
        }
        # Manual-mode connections are AgenticDataIngestion-owned, so they carry the
        # same source tag select_rds does — delete_pipeline cleanup and the
        # daily janitor rely on it.
        assert kwargs["tags"] == {
            "Type": "agentic-data-ingestion-source",
            "PipelineId": PIPELINE_ID,
        }

    @patch("handlers.pipelines_jdbc.create_glue_jdbc_connection")
    def test_manual_mode_passes_credentials_by_arn_reference_only(self, mock_create):
        """Invariant LGR-819980f-1 — credentials by Secrets Manager ARN only.

        Manual mode is the one path where the user supplies the secret
        directly, so it is the most likely place for a plaintext
        credential to leak into the Glue connection.
        """
        _provision_jdbc_source_connection(PIPELINE_ID, _VALID_MANUAL_SOURCE_CONFIG)

        kwargs = mock_create.call_args.kwargs
        assert kwargs["secret_arn"] == _VALID_MANUAL_SOURCE_CONFIG["secret_arn"]
        assert kwargs["secret_arn"].startswith("arn:aws:secretsmanager:")
        # No plaintext credential ever reaches the connection helper.
        forbidden = {"username", "password", "user", "jdbc_user", "jdbc_password"}
        assert forbidden.isdisjoint(kwargs), f"plaintext credential kwarg leaked: {forbidden & set(kwargs)}"
        assert forbidden.isdisjoint(kwargs["tags"])

    @patch("handlers.pipelines_jdbc.create_glue_jdbc_connection")
    def test_manual_mode_missing_networking_raises_without_creating(self, mock_create):
        bad_config = dict(_VALID_MANUAL_SOURCE_CONFIG)
        del bad_config["subnet_id"]

        # The error message names the mode, so a 400 tells the caller which
        # branch rejected the payload.
        with pytest.raises(
            ValueError,
            match=r"source_config\.subnet_id is required .* connection mode is manual",
        ):
            _provision_jdbc_source_connection(PIPELINE_ID, bad_config)

        mock_create.assert_not_called()


# ===========================================================================
# Group 3 — _provision_jdbc_runtime_crawler
# ===========================================================================


class _FakeAlreadyExists(Exception):
    """Stand-in for ``glue_client.exceptions.AlreadyExistsException``."""


def _make_fake_glue_client(*, exists: bool = False) -> MagicMock:
    """Build a MagicMock glue client with raisable exception classes.

    boto3-generated client exceptions live on ``client.exceptions`` as
    real ``Exception`` subclasses; MagicMock can't recreate that out
    of the box, so we attach plain Exception subclasses and (optionally)
    wire ``create_crawler`` to raise the AlreadyExists variant.
    """
    glue = MagicMock()
    glue.exceptions.AlreadyExistsException = _FakeAlreadyExists
    if exists:
        glue.create_crawler.side_effect = _FakeAlreadyExists("exists")
    return glue


class TestProvisionJdbcRuntimeCrawler:
    """Direct unit tests for the runtime crawler provisioner.

    Validates: Requirement 22.1 (and Req 11.8 — every JDBC pipeline owns
    a per-pipeline runtime crawler; CreateCrawler / UpdateCrawler is
    idempotent; missing role ARN surfaces as a clean configuration
    error rather than a confusing Glue InvalidInputException).
    """

    @pytest.fixture(autouse=True)
    def _set_glue_role(self, monkeypatch):
        """Ensure GLUE_PIPELINE_ROLE_ARN is set for the happy-path tests."""
        monkeypatch.setattr(
            "handlers.jdbc_resources.GLUE_PIPELINE_ROLE_ARN",
            "arn:aws:iam::123456789012:role/agentic-data-ingestion-glue-pipeline-role",
        )
        monkeypatch.setattr(
            "handlers.jdbc_resources.DISCOVERY_DB",
            "agentic_data_ingestion_jdbc_discovery_test",
        )

    @patch("handlers.pipelines_jdbc.boto3.client")
    def test_happy_path_creates_crawler_with_expected_args(self, mock_boto_client):
        mock_glue = _make_fake_glue_client(exists=False)
        mock_boto_client.return_value = mock_glue

        result = _provision_jdbc_runtime_crawler(
            PIPELINE_ID,
            _VALID_SELECT_RDS_SOURCE_CONFIG,
            f"agentic-data-ingestion-{PIPELINE_ID}-source",
        )

        assert result == f"agentic-data-ingestion-{PIPELINE_ID}-source-discover"
        mock_boto_client.assert_called_with("glue")
        mock_glue.create_crawler.assert_called_once()
        mock_glue.update_crawler.assert_not_called()

        kwargs = mock_glue.create_crawler.call_args.kwargs
        assert kwargs["Name"] == f"agentic-data-ingestion-{PIPELINE_ID}-source-discover"
        assert kwargs["DatabaseName"] == "agentic_data_ingestion_jdbc_discovery_test"
        assert kwargs["Role"].endswith("agentic-data-ingestion-glue-pipeline-role")
        assert kwargs["TablePrefix"] == f"agentic_data_ingestion_{PIPELINE_ID}_"
        # Path is "{schema}/{table}" — see the function's docstring.
        assert kwargs["Targets"] == {
            "JdbcTargets": [
                {
                    "ConnectionName": f"agentic-data-ingestion-{PIPELINE_ID}-source",
                    "Path": "public/orders",
                }
            ]
        }
        # Tag MUST be ``agentic-data-ingestion-runtime`` (not ``agentic-data-ingestion-discovery``)
        # so the daily janitor in handlers.jdbc_resources skips this
        # crawler — runtime crawlers live for the pipeline's lifetime.
        assert kwargs["Tags"] == {
            "Type": "agentic-data-ingestion-runtime",
            "PipelineId": PIPELINE_ID,
        }

    @patch("handlers.pipelines_jdbc.boto3.client")
    def test_already_exists_falls_through_to_update_without_tags(self, mock_boto_client):
        mock_glue = _make_fake_glue_client(exists=True)
        mock_boto_client.return_value = mock_glue

        result = _provision_jdbc_runtime_crawler(
            PIPELINE_ID,
            _VALID_SELECT_RDS_SOURCE_CONFIG,
            f"agentic-data-ingestion-{PIPELINE_ID}-source",
        )

        assert result == f"agentic-data-ingestion-{PIPELINE_ID}-source-discover"
        mock_glue.create_crawler.assert_called_once()
        mock_glue.update_crawler.assert_called_once()
        update_kwargs = mock_glue.update_crawler.call_args.kwargs
        # Glue rejects ``Tags`` on UpdateCrawler — the implementation MUST
        # strip it on the update path.
        assert "Tags" not in update_kwargs
        assert update_kwargs["Name"] == f"agentic-data-ingestion-{PIPELINE_ID}-source-discover"
        assert update_kwargs["TablePrefix"] == f"agentic_data_ingestion_{PIPELINE_ID}_"

    def test_missing_role_arn_raises_runtime_error(self, monkeypatch):
        # Override the autouse fixture for this case.
        monkeypatch.setattr("handlers.jdbc_resources.GLUE_PIPELINE_ROLE_ARN", "")
        with patch("handlers.pipelines_jdbc.boto3.client") as mock_boto_client:
            with pytest.raises(
                RuntimeError,
                match=r"GLUE_PIPELINE_ROLE_ARN environment variable is not set",
            ):
                _provision_jdbc_runtime_crawler(
                    PIPELINE_ID,
                    _VALID_SELECT_RDS_SOURCE_CONFIG,
                    f"agentic-data-ingestion-{PIPELINE_ID}-source",
                )
            # The role check runs BEFORE any AWS call.
            mock_boto_client.assert_not_called()


# ===========================================================================
# Group 4 — create_pipeline JDBC rollback semantics (integration-style)
# ===========================================================================


def _captured_pipeline_id() -> str:
    """Return the pipeline_id from the most recent put_item call."""
    assert _mock_pipelines_table.put_item.called
    return _mock_pipelines_table.put_item.call_args[1]["Item"]["pipeline_id"]


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestCreatePipelineJdbcRollback:
    """End-to-end tests of create_pipeline's JDBC rollback semantics.

    Validates: Requirement 22.1 (and Req 11.5 — DDB row + AgenticDataIngestion-owned
    Glue Connection are rolled back together on failure).
    """

    @patch("handlers.pipelines_crud._provision_jdbc_runtime_crawler")
    @patch("handlers.pipelines_crud._provision_jdbc_source_connection")
    def test_connection_failure_rolls_back_ddb_row(
        self,
        mock_provision_conn,
        mock_provision_crawler,
        _mock_provision_resources,
    ):
        # Simulate the connection step failing — e.g. malformed subnet.
        mock_provision_conn.side_effect = RuntimeError("subnet not found")
        body = _make_create_body(_VALID_SELECT_RDS_SOURCE_CONFIG)

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "Failed to provision JDBC source connection" in error
        assert "subnet not found" in error
        # DDB row was written, then deleted as rollback.
        _mock_pipelines_table.put_item.assert_called_once()
        pid = _captured_pipeline_id()
        _mock_pipelines_table.delete_item.assert_called_once_with(Key={"pipeline_id": pid})
        # Crawler step must NOT have been attempted.
        mock_provision_crawler.assert_not_called()

    @patch("handlers.pipelines_crud.boto3.client")
    @patch("handlers.pipelines_crud._provision_jdbc_runtime_crawler")
    @patch("handlers.pipelines_crud._provision_jdbc_source_connection")
    def test_crawler_failure_in_select_rds_mode_deletes_owned_connection(
        self,
        mock_provision_conn,
        mock_provision_crawler,
        mock_boto_client,
        _mock_provision_resources,
    ):
        # Connection step succeeds; crawler step fails.
        mock_provision_conn.return_value = None  # return value unused here
        mock_provision_crawler.side_effect = RuntimeError("crawler IAM denied")
        # The rollback path instantiates a Glue client to delete the
        # partially-created connection.
        mock_glue = MagicMock()
        mock_glue.exceptions.EntityNotFoundException = type("_FakeNotFound", (Exception,), {})
        mock_boto_client.return_value = mock_glue

        body = _make_create_body(_VALID_SELECT_RDS_SOURCE_CONFIG)
        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "Failed to provision JDBC runtime crawler" in error
        assert "crawler IAM denied" in error

        # AgenticDataIngestion owns the source connection in select_rds mode →
        # rollback MUST delete it.
        pid = _captured_pipeline_id()
        mock_glue.delete_connection.assert_called_once_with(ConnectionName=f"agentic-data-ingestion-{pid}-source")
        # DDB row was rolled back.
        _mock_pipelines_table.delete_item.assert_called_once_with(Key={"pipeline_id": pid})

    @patch("handlers.pipelines_crud.boto3.client")
    @patch("handlers.pipelines_crud._provision_jdbc_runtime_crawler")
    @patch("handlers.pipelines_crud._provision_jdbc_source_connection")
    def test_crawler_failure_in_glue_connection_mode_preserves_user_connection(
        self,
        mock_provision_conn,
        mock_provision_crawler,
        mock_boto_client,
        _mock_provision_resources,
    ):
        # User supplied an existing Glue Connection → AgenticDataIngestion must
        # NOT delete it on rollback even though the crawler step failed.
        mock_provision_conn.return_value = "my-existing-connection"
        mock_provision_crawler.side_effect = RuntimeError("crawler IAM denied")
        mock_glue = MagicMock()
        mock_boto_client.return_value = mock_glue

        body = _make_create_body(_VALID_GLUE_CONNECTION_SOURCE_CONFIG)
        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "Failed to provision JDBC runtime crawler" in error

        # CRITICAL: never call delete_connection on a user-supplied
        # connection. boto3.client may still be instantiated by other
        # paths in the handler, but no delete_connection call MUST
        # have been made.
        assert mock_glue.delete_connection.call_count == 0
        # DDB row WAS still rolled back.
        pid = _captured_pipeline_id()
        _mock_pipelines_table.delete_item.assert_called_once_with(Key={"pipeline_id": pid})


# ===========================================================================
# Group 5 — delete_pipeline JDBC cleanup blocks (integration-style)
# ===========================================================================


def _archived_jdbc_item(*, glue_connection_mode: bool = False) -> dict:
    """Return an archived-status JDBC pipeline item for delete tests."""
    if glue_connection_mode:
        source_config = dict(_VALID_GLUE_CONNECTION_SOURCE_CONFIG)
        source_connection_name = "my-existing-connection"
    else:
        source_config = dict(_VALID_SELECT_RDS_SOURCE_CONFIG)
        source_connection_name = f"agentic-data-ingestion-{PIPELINE_ID}-source"
    return {
        "pipeline_id": PIPELINE_ID,
        "pipeline_name": "Test JDBC Pipeline",
        "status": "archived",
        "source_type": "JDBC",
        "source_config": source_config,
        "source_connection_name": source_connection_name,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _archived_s3_upload_item() -> dict:
    return {
        "pipeline_id": PIPELINE_ID,
        "pipeline_name": "Test S3 Pipeline",
        "status": "archived",
        "source_type": "S3_UPLOAD",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


class _FakeNotFound(Exception):
    """Stand-in for ``glue_client.exceptions.EntityNotFoundException``."""


def _build_glue_mock(
    *,
    crawler_missing: bool = False,
    discovery_db_missing: bool = False,
    connection_missing: bool = False,
    discovered_tables: list[str] | None = None,
) -> MagicMock:
    """Build a fake glue client with raisable exception classes.

    By default all calls succeed and ``get_tables`` returns whatever
    ``discovered_tables`` lists. The ``*_missing`` flags wire each call
    to raise EntityNotFoundException — the cleanup blocks must record
    those as success in ``cleanup_results`` rather than as failures.
    """
    glue = MagicMock()
    glue.exceptions.EntityNotFoundException = _FakeNotFound
    glue.exceptions.AlreadyExistsException = type("_FakeAlready", (Exception,), {})

    if crawler_missing:
        glue.delete_crawler.side_effect = _FakeNotFound("missing crawler")

    if discovery_db_missing:
        glue.get_tables.side_effect = _FakeNotFound("missing db")
    else:
        tables = discovered_tables or []
        glue.get_tables.return_value = {"TableList": [{"Name": t} for t in tables]}

    if connection_missing:
        glue.delete_connection.side_effect = _FakeNotFound("missing conn")

    return glue


def _make_boto_client_factory(glue_mock: MagicMock) -> MagicMock:
    """Return a side-effect that maps ``boto3.client(name)`` to a mock.

    Glue calls go to ``glue_mock``; ``events`` and ``stepfunctions``
    return permissive MagicMocks where ``list_targets_by_rule`` raises
    ``ResourceNotFoundException`` so the EventBridge cleanup is a no-op.
    """
    events_mock = MagicMock()
    events_mock.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")
    sfn_mock = MagicMock()
    factory = MagicMock()

    def _side_effect(service: str, *args, **kwargs):
        if service == "glue":
            return glue_mock
        if service == "events":
            return events_mock
        if service == "stepfunctions":
            return sfn_mock
        return MagicMock()

    factory.side_effect = _side_effect
    return factory


class TestDeletePipelineJdbcCleanup:
    """End-to-end tests of delete_pipeline's JDBC cleanup blocks (5i/5j/5k).

    Validates: Requirement 22.1 (and Req 11.8 — runtime crawler,
    discovery-DB tables, and the source-connection-when-owned are
    cleaned up; EntityNotFoundException is treated as success).
    """

    def test_select_rds_mode_cleans_up_crawler_tables_and_connection(self):
        _mock_pipelines_table.get_item.return_value = {"Item": _archived_jdbc_item(glue_connection_mode=False)}
        glue = _build_glue_mock(
            discovered_tables=[
                f"agentic_data_ingestion_{PIPELINE_ID}_public_orders",
                f"agentic_data_ingestion_{PIPELINE_ID}_public_audit",
            ]
        )
        with patch(
            "handlers.pipelines_crud.boto3.client",
            new=_make_boto_client_factory(glue),
        ):
            result = delete_pipeline(PIPELINE_ID)

        assert result["statusCode"] == 200, result["body"]
        body = json.loads(result["body"])
        results = body["cleanup_results"]
        assert body["cleanup_failures"] == []

        # 5i. Runtime crawler deleted.
        glue.delete_crawler.assert_called_once_with(Name=f"agentic-data-ingestion-{PIPELINE_ID}-source-discover")
        assert results["runtime_crawler"] is True

        # 5j. Discovery-DB tables looked up by prefix and each deleted.
        glue.get_tables.assert_called_once()
        get_tables_kwargs = glue.get_tables.call_args.kwargs
        assert get_tables_kwargs["Expression"] == f"agentic_data_ingestion_{PIPELINE_ID}_*"
        assert glue.delete_table.call_count == 2
        assert results["jdbc_tables"] == 2

        # 5k. Source connection deleted (AgenticDataIngestion owns it in select_rds).
        glue.delete_connection.assert_called_once_with(ConnectionName=f"agentic-data-ingestion-{PIPELINE_ID}-source")
        assert results["source_connection"] is True

    def test_glue_connection_mode_preserves_user_connection(self):
        _mock_pipelines_table.get_item.return_value = {"Item": _archived_jdbc_item(glue_connection_mode=True)}
        glue = _build_glue_mock(discovered_tables=[])
        with patch(
            "handlers.pipelines_crud.boto3.client",
            new=_make_boto_client_factory(glue),
        ):
            result = delete_pipeline(PIPELINE_ID)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        results = body["cleanup_results"]
        assert body["cleanup_failures"] == []

        # 5i / 5j still run.
        glue.delete_crawler.assert_called_once_with(Name=f"agentic-data-ingestion-{PIPELINE_ID}-source-discover")
        glue.get_tables.assert_called_once()

        # 5k MUST NOT touch the user-supplied connection.
        glue.delete_connection.assert_not_called()
        assert "source_connection" not in results

    def test_s3_upload_pipeline_skips_all_jdbc_cleanup(self):
        _mock_pipelines_table.get_item.return_value = {"Item": _archived_s3_upload_item()}
        glue = _build_glue_mock(discovered_tables=[])
        with patch(
            "handlers.pipelines_crud.boto3.client",
            new=_make_boto_client_factory(glue),
        ):
            result = delete_pipeline(PIPELINE_ID)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        results = body["cleanup_results"]

        # The JDBC blocks (5i/5j/5k) must be no-ops for S3_UPLOAD.
        glue.delete_crawler.assert_not_called()
        glue.get_tables.assert_not_called()
        glue.delete_connection.assert_not_called()
        for forbidden_key in (
            "runtime_crawler",
            "jdbc_tables",
            "source_connection",
        ):
            assert forbidden_key not in results, (
                f"{forbidden_key!r} must not appear in cleanup_results for S3_UPLOAD pipelines"
            )

    def test_entity_not_found_on_each_glue_call_recorded_as_success(self):
        # Even when every Glue resource is already gone (a re-run delete
        # or a partially-provisioned pipeline), delete_pipeline MUST
        # treat that as success and record it in cleanup_results — never
        # in cleanup_failures.
        _mock_pipelines_table.get_item.return_value = {"Item": _archived_jdbc_item(glue_connection_mode=False)}
        glue = _build_glue_mock(
            crawler_missing=True,
            discovery_db_missing=True,
            connection_missing=True,
        )
        with patch(
            "handlers.pipelines_crud.boto3.client",
            new=_make_boto_client_factory(glue),
        ):
            result = delete_pipeline(PIPELINE_ID)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        results = body["cleanup_results"]

        # No JDBC resource ended up in cleanup_failures.
        failure_resources = {f["resource"] for f in body["cleanup_failures"]}
        for jdbc_resource in (
            "runtime_crawler",
            "jdbc_tables",
            "source_connection",
        ):
            assert jdbc_resource not in failure_resources, (
                f"EntityNotFoundException on {jdbc_resource} must be recorded as success, never as a cleanup failure"
            )

        # Each JDBC block recorded its idempotent-success outcome.
        assert results["runtime_crawler"] is False  # missing crawler
        assert results["jdbc_tables"] == 0  # no tables to delete
        assert results["source_connection"] is False  # missing connection


# ---------------------------------------------------------------------------
# delete_pipeline — the target connection, and connections another pipeline uses
# ---------------------------------------------------------------------------

_RDS_CONNECTION = f"agentic-data-ingestion-{PIPELINE_ID}-rds"
_SOURCE_CONNECTION = f"agentic-data-ingestion-{PIPELINE_ID}-source"


def _archived_s3_to_jdbc_item(**target_overrides) -> dict:
    """An S3-upload pipeline writing to a JDBC target (select_rds mode)."""
    target_config = {
        "type": "JDBC",
        "jdbc_url": "jdbc:postgresql://target-db.example.com:5432/analytics",
        "jdbc_driver": "org.postgresql.Driver",
        "jdbc_table": "analytics.users",
        "jdbc_connection_name": _RDS_CONNECTION,
        **target_overrides,
    }
    return {**_archived_s3_upload_item(), "target_config": target_config}


def _delete_with(item: dict, glue: MagicMock) -> dict:
    _mock_pipelines_table.get_item.return_value = {"Item": item}
    with patch("handlers.pipelines_crud.boto3.client", new=_make_boto_client_factory(glue)):
        result = delete_pipeline(PIPELINE_ID)
    assert result["statusCode"] == 200, result["body"]
    return json.loads(result["body"])


class TestDeletePipelineOwnedConnections:
    def test_an_owned_jdbc_target_connection_is_deleted(self):
        glue = _build_glue_mock()

        body = _delete_with(_archived_s3_to_jdbc_item(), glue)

        glue.delete_connection.assert_called_once_with(ConnectionName=_RDS_CONNECTION)
        assert body["cleanup_results"]["target_connection"] is True
        assert body["cleanup_failures"] == []

    def test_a_user_supplied_target_connection_is_kept(self):
        glue = _build_glue_mock()

        body = _delete_with(_archived_s3_to_jdbc_item(glue_connection_name="warehouse-conn"), glue)

        glue.delete_connection.assert_not_called()
        assert "target_connection" not in body["cleanup_results"]

    def test_only_the_deterministic_target_name_is_ever_deleted(self):
        # A direct API caller can set `jdbc_connection_name` itself. Whatever it
        # names, the cleanup deletes only `agentic-data-ingestion-{id}-rds`.
        glue = _build_glue_mock()

        _delete_with(_archived_s3_to_jdbc_item(jdbc_connection_name="someone-elses-conn"), glue)

        glue.delete_connection.assert_called_once_with(ConnectionName=_RDS_CONNECTION)

    def test_an_already_absent_target_connection_is_success(self):
        glue = _build_glue_mock(connection_missing=True)

        body = _delete_with(_archived_s3_to_jdbc_item(), glue)

        assert body["cleanup_results"]["target_connection"] is False
        assert body["cleanup_failures"] == []

    def test_a_source_connection_another_pipeline_uses_is_kept(self):
        # The live case: pipeline B named A's `-source` connection in
        # glue_connection mode. Deleting A must not break B.
        other = {
            "pipeline_id": "other-pipeline-1a2b3c4d",
            "source_connection_name": _SOURCE_CONNECTION,
            "source_config": {"glue_connection_name": _SOURCE_CONNECTION},
        }
        _mock_pipelines_table.scan.side_effect = [
            {"Items": [{"pipeline_id": PIPELINE_ID}], "LastEvaluatedKey": {"pipeline_id": PIPELINE_ID}},
            {"Items": [other]},
        ]
        glue = _build_glue_mock()

        body = _delete_with(_archived_jdbc_item(glue_connection_mode=False), glue)

        glue.delete_connection.assert_not_called()
        assert body["cleanup_results"]["source_connection_kept_for"] == ["other-pipeline-1a2b3c4d"]
        assert "source_connection" not in body["cleanup_results"]
        assert body["cleanup_failures"] == []
        # Both pages were read, the second from where the first stopped.
        assert _mock_pipelines_table.scan.call_count == 2
        assert _mock_pipelines_table.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"pipeline_id": PIPELINE_ID}

    def test_a_target_connection_another_pipeline_uses_is_kept(self):
        _mock_pipelines_table.scan.return_value = {
            "Items": [
                {"pipeline_id": "other-pipeline-1a2b3c4d", "target_config": {"glue_connection_name": _RDS_CONNECTION}}
            ]
        }
        glue = _build_glue_mock()

        body = _delete_with(_archived_s3_to_jdbc_item(), glue)

        glue.delete_connection.assert_not_called()
        assert body["cleanup_results"]["target_connection_kept_for"] == ["other-pipeline-1a2b3c4d"]

    def test_when_the_reference_check_fails_the_connection_is_kept(self):
        # Fail safe: a leaked connection costs less than a broken pipeline.
        _mock_pipelines_table.scan.side_effect = RuntimeError("scan throttled")
        glue = _build_glue_mock()

        body = _delete_with(_archived_jdbc_item(glue_connection_mode=False), glue)

        glue.delete_connection.assert_not_called()
        failures = [f for f in body["cleanup_failures"] if f["resource"] == "source_connection"]
        assert len(failures) == 1
        assert "kept" in failures[0]["error"] and "scan throttled" in failures[0]["error"]


# ---------------------------------------------------------------------------
# create_pipeline / edit_pipeline — refusing the app's own connections
# ---------------------------------------------------------------------------

_MANAGED_NAMES = [
    "agentic-data-ingestion-otherpipe-1a2b3c4d-source",
    "agentic-data-ingestion-otherpipe-1a2b3c4d-rds",
    "agentic-data-ingestion-discover-0123456789ab",
]

_JDBC_TARGET_GLUE_CONNECTION: dict = {
    "type": "JDBC",
    "_connection_mode": "glue_connection",
    "jdbc_url": "jdbc:postgresql://target-db.example.com:5432/analytics",
    "jdbc_driver": "org.postgresql.Driver",
    "jdbc_table": "analytics.users",
}


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestCreateRefusesManagedConnections:
    @pytest.mark.parametrize("name", _MANAGED_NAMES)
    def test_a_managed_source_connection_is_refused(self, _mock_provision, name):
        source_config = {
            **_VALID_GLUE_CONNECTION_SOURCE_CONFIG,
            "_connection_mode": "glue_connection",
            "glue_connection_name": name,
        }

        result = create_pipeline(_make_create_body(source_config), event={})

        assert result["statusCode"] == 400, result["body"]
        error = json.loads(result["body"])["error"]
        assert f"source_config.glue_connection_name {name!r}" in error
        _mock_pipelines_table.put_item.assert_not_called()

    @pytest.mark.parametrize("name", _MANAGED_NAMES)
    def test_a_managed_target_connection_is_refused(self, _mock_provision, name):
        body = json.dumps(
            {
                "pipeline_name": "S3 to JDBC",
                "target_schema": "uploaded_test_schema",
                "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
                "source_type": "S3_UPLOAD",
                "target_config": {**_JDBC_TARGET_GLUE_CONNECTION, "glue_connection_name": name},
            }
        )

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400, result["body"]
        assert f"target_config.glue_connection_name {name!r}" in json.loads(result["body"])["error"]
        _mock_pipelines_table.put_item.assert_not_called()

    def test_refused_without_a_connection_mode_too(self, _mock_provision):
        # Ownership is decided by the presence of glue_connection_name, so the
        # guard is too. Omitting `_connection_mode` must not get past it.
        assert "_connection_mode" not in _VALID_GLUE_CONNECTION_SOURCE_CONFIG
        source_config = {**_VALID_GLUE_CONNECTION_SOURCE_CONFIG, "glue_connection_name": _MANAGED_NAMES[0]}

        result = create_pipeline(_make_create_body(source_config), event={})

        # The refusal, not some later 400: nothing written, and the error names
        # the connection.
        assert result["statusCode"] == 400, result["body"]
        assert _MANAGED_NAMES[0] in json.loads(result["body"])["error"]
        _mock_pipelines_table.put_item.assert_not_called()


class TestEditRefusesManagedConnections:
    def test_switching_the_target_to_a_managed_connection_is_refused(self):
        _mock_pipelines_table.get_item.return_value = {
            "Item": {"pipeline_id": PIPELINE_ID, "target_config": {"glue_connection_name": "warehouse-conn"}}
        }
        body = json.dumps({"target_config": {"glue_connection_name": _MANAGED_NAMES[1]}})

        result = edit_pipeline(PIPELINE_ID, body)

        assert result["statusCode"] == 400, result["body"]
        _mock_pipelines_table.update_item.assert_not_called()

    def test_resaving_an_unchanged_managed_connection_is_not_refused(self):
        # A pipeline created before the guard keeps working when re-saved.
        _mock_pipelines_table.get_item.return_value = {
            "Item": {"pipeline_id": PIPELINE_ID, "target_config": {"glue_connection_name": _MANAGED_NAMES[1]}}
        }
        body = json.dumps({"target_config": {"glue_connection_name": _MANAGED_NAMES[1]}})

        result = edit_pipeline(PIPELINE_ID, body)

        assert result["statusCode"] != 400, result["body"]


# ---------------------------------------------------------------------------
# _augment_glue_connection_config — SECRET_ID resolution semantics
# ---------------------------------------------------------------------------


def _glue_client_with_connection(props: dict, tags: dict | None = None) -> MagicMock:
    """Return a MagicMock glue client whose get_connection yields ``props``.

    ``tags`` is accepted for callers that want to state a tag notionally exists,
    but it is **never readable** — ``get_tags`` raises here, because that is what
    real Glue does. Glue Connections are not a taggable resource type: both
    ``GetTags`` and ``TagResource`` reject a ``…:connection/<name>`` ARN with
    ``InvalidInputException``, verified against the live API.

    This helper used to return ``{"Tags": tags}``, which let the suite assert a
    credential read path that cannot exist in production, and that is why a
    connection carrying no ``SECRET_ID`` satisfied these tests and then failed
    pipeline creation for real. The secret ARN lives in the ``SECRET_ID``
    connection property and nowhere else.
    """
    glue = MagicMock()

    class _FakeEntityNotFound(Exception):
        pass

    glue.exceptions.EntityNotFoundException = _FakeEntityNotFound

    class _FakeInvalidInput(Exception):
        """Stand-in for Glue's ``InvalidInputException`` on a connection ARN."""

    glue.get_connection.return_value = {"Connection": {"ConnectionProperties": props}}
    glue.get_tags.side_effect = _FakeInvalidInput("Glue Connections are not taggable")
    glue.get_caller_identity.return_value = {"Account": "123456789012"}
    return glue


class TestAugmentGlueConnectionConfig:
    """Cover the glue_connection-mode secret_arn / engine resolution.

    A real-world Glue Connection may carry credentials inline (``USERNAME`` /
    ``PASSWORD``) rather than via a secret. Such a connection is refused on
    BOTH sides. It used to be accepted as a TARGET, because job_arguments.py
    then copied the inline pair into plaintext Glue job arguments — the
    violation of standing invariant ``LGR-819980f-1`` that leaked a target
    credential. That fallback is deleted, so the target side now fails fast
    exactly as the source side always did (superseding part of
    ``LGR-c246ab8-1``).

    The secret ARN is resolved from the connection's ``adi:secret_arn``
    tag (set alongside the ``SECRET_ID`` property by AgenticDataIngestion's provisioner);
    a ``SECRET_ID`` ConnectionProperty is honoured as a fallback for
    user-managed connections.
    """

    _INLINE_CRED_PROPS = {
        "JDBC_CONNECTION_URL": "jdbc:postgresql://host:5432/testdb",
        "USERNAME": "admin",
        "PASSWORD": "hunter2",
    }
    _MYSQL_URL_PROPS = {
        "JDBC_CONNECTION_URL": "jdbc:mysql://host:3306/appdb",
    }
    _SECRET_TAGS = {
        "adi:secret_arn": "arn:aws:secretsmanager:us-west-2:123:secret:db-XYZ",
    }

    def test_target_inline_credentials_no_secret_id_raises(self):
        """A target Glue connection with inline creds (no SECRET_ID) is refused.

        Was ``test_target_inline_credentials_no_secret_id_does_not_raise``, which
        asserted the opposite — see the class docstring for why that tolerance
        is withdrawn. The refusal must not echo the inline values.
        """
        cfg = {
            "_connection_mode": "glue_connection",
            "glue_connection_name": "agentic-data-ingestion-test-fixtures-postgres-target",
        }
        glue = _glue_client_with_connection(self._INLINE_CRED_PROPS)
        with (
            patch("handlers.pipelines_jdbc.boto3.client", return_value=glue),
            pytest.raises(ValueError, match=r"target_config\.secret_arn is required") as excinfo,
        ):
            pipelines._augment_glue_connection_config(cfg, "target_config")

        assert "SECRET_ID" in str(excinfo.value)
        assert self._INLINE_CRED_PROPS["PASSWORD"] not in str(excinfo.value)
        assert "secret_arn" not in cfg

    def test_source_inline_credentials_no_secret_id_raises(self):
        """A source Glue connection without a secret tag still fails fast."""
        cfg = {
            "_connection_mode": "glue_connection",
            "glue_connection_name": "some-source-conn",
        }
        glue = _glue_client_with_connection(self._INLINE_CRED_PROPS)
        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            with pytest.raises(ValueError, match="secret_arn"):
                pipelines._augment_glue_connection_config(cfg, "source_config")

    def test_target_with_secret_id_populates_secret_arn(self):
        """A target connection's ``SECRET_ID`` populates ``secret_arn``.

        Renamed from ``..._with_secret_tag_...``: this asserted that an
        ``adi:secret_arn`` tag supplied the ARN, which cannot happen — Glue
        refuses ``GetTags`` on a connection ARN. The subject is unchanged; only
        the place the ARN is read from is now the one that exists.
        """
        cfg = {
            "_connection_mode": "glue_connection",
            "glue_connection_name": "conn-with-secret",
        }
        secret_arn = self._SECRET_TAGS["adi:secret_arn"]
        glue = _glue_client_with_connection({**self._MYSQL_URL_PROPS, "SECRET_ID": secret_arn})
        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            pipelines._augment_glue_connection_config(cfg, "target_config")

        assert cfg["secret_arn"] == secret_arn
        assert cfg["engine"] == "mysql"

    def test_source_with_secret_id_populates_secret_arn(self):
        """Same as the target case, for a source. See that test's note."""
        cfg = {
            "_connection_mode": "glue_connection",
            "glue_connection_name": "conn-with-secret",
        }
        secret_arn = self._SECRET_TAGS["adi:secret_arn"]
        glue = _glue_client_with_connection({**self._MYSQL_URL_PROPS, "SECRET_ID": secret_arn})
        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            pipelines._augment_glue_connection_config(cfg, "source_config")

        assert cfg["secret_arn"] == secret_arn
        assert cfg["engine"] == "mysql"

    def test_legacy_secret_id_property_still_honored(self):
        """Backward compat: a legacy SECRET_ID ConnectionProperty is used
        as a fallback when no adi:secret_arn tag is present."""
        cfg = {
            "_connection_mode": "glue_connection",
            "glue_connection_name": "legacy-conn",
        }
        legacy_props = {
            **self._MYSQL_URL_PROPS,
            "SECRET_ID": "arn:aws:secretsmanager:us-west-2:123:secret:legacy-XYZ",
        }
        glue = _glue_client_with_connection(legacy_props)
        with patch("handlers.pipelines_jdbc.boto3.client", return_value=glue):
            pipelines._augment_glue_connection_config(cfg, "source_config")

        assert cfg["secret_arn"] == legacy_props["SECRET_ID"]
        assert cfg["engine"] == "mysql"

    def test_non_glue_connection_mode_is_noop(self):
        """select_rds / manual modes never touch Glue."""
        cfg = {"_connection_mode": "select_rds"}
        # boto3.client must not even be called; patch to explode if it is.
        with patch(
            "handlers.pipelines_jdbc.boto3.client",
            side_effect=AssertionError("Glue must not be called"),
        ):
            pipelines._augment_glue_connection_config(cfg, "target_config")
        assert "secret_arn" not in cfg


# ---------------------------------------------------------------------------
# Plaintext target credentials refused at the API — LGR-819980f-1
# ---------------------------------------------------------------------------

_PLAINTEXT_USER = "placeholder-user-not-a-credential"
_PLAINTEXT_PASSWORD = "placeholder-password-not-a-credential"
_PLAINTEXT_VALUES = {"jdbc_user": _PLAINTEXT_USER, "jdbc_password": _PLAINTEXT_PASSWORD}


def _logged_lines(mock_logger: MagicMock) -> list[str]:
    """Every message the mocked logger was given, %-formatted, at every level."""
    lines: list[str] = []
    for level in ("debug", "info", "warning", "error", "exception", "critical"):
        for call in getattr(mock_logger, level).call_args_list:
            fmt, *rest = call.args or ("",)
            lines.append(str(fmt) % tuple(rest) if rest else str(fmt))
    return lines


class TestPlaintextTargetCredentialsRefused:
    """``create_pipeline`` and ``edit_pipeline`` refuse ``target_config.jdbc_user`` / ``jdbc_password``.

    Standing invariant ``LGR-819980f-1``. ``build_jdbc_arguments`` no longer reads
    either key, so accepting them would only store a plaintext credential in the
    pipelines table (and return it from the details endpoint) for no effect. The
    refusal is a 400 that precedes every Glue call and every write, and both the
    response and the log line name the KEYS, never a value — the response goes to
    the client and the log line to CloudWatch, and a value in either is the same
    leak as a value in a job argument.
    """

    @staticmethod
    def _create_body(target_config: dict) -> str:
        return json.dumps(
            {
                "pipeline_name": "jdbc target with inline credentials",
                "target_schema": "uploaded_test_schema",
                "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
                "target_config": target_config,
            }
        )

    @pytest.mark.parametrize("keys", [("jdbc_user",), ("jdbc_password",), ("jdbc_user", "jdbc_password")])
    def test_create_returns_400_naming_the_keys_only(self, keys):
        target_config = {
            "type": "JDBC",
            "_connection_mode": "glue_connection",
            "glue_connection_name": "pg-target",
            "jdbc_table": "public.orders",
            **{key: _PLAINTEXT_VALUES[key] for key in keys},
        }
        with (
            patch("handlers.pipelines_crud.logger") as mock_logger,
            # Refused before the Glue connection is read: a Glue client here would
            # raise, and create_pipeline would turn that into a 500, not a 400.
            patch("handlers.pipelines_jdbc.boto3.client", side_effect=AssertionError("no AWS call before refusal")),
        ):
            result = create_pipeline(self._create_body(target_config), event={})

        assert result["statusCode"] == 400, result
        error = json.loads(result["body"])["error"]
        for key in keys:
            assert f"target_config.{key}" in error
        assert "secret_arn" in error and "SECRET_ID" in error, "the 400 must say what to send instead"
        logged = _logged_lines(mock_logger)
        assert any("plaintext target credentials rejected create" in line for line in logged), logged
        for value in _PLAINTEXT_VALUES.values():
            assert value not in result["body"]
            assert not [line for line in logged if value in line]
        _mock_pipelines_table.put_item.assert_not_called()

    def test_edit_returns_400_and_writes_nothing(self):
        """A per-key edit carrying only ``jdbc_password`` — no ``type`` — is still refused.

        The edit path merges ``target_config`` per key, so without this an edit
        could add a password to an existing JDBC pipeline's stored config. Keyed
        on presence, because the stored ``type`` is not in the request.
        """
        body = json.dumps({"target_config": {"jdbc_password": _PLAINTEXT_PASSWORD}})
        with patch("handlers.pipelines_crud.logger") as mock_logger:
            result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 400, result
        error = json.loads(result["body"])["error"]
        assert "target_config.jdbc_password" in error
        assert "target_config.jdbc_user" not in error, "only the keys actually present are named"
        assert _PLAINTEXT_PASSWORD not in result["body"]
        logged = _logged_lines(mock_logger)
        assert any("plaintext target credentials rejected edit" in line for line in logged), logged
        assert not [line for line in logged if _PLAINTEXT_PASSWORD in line]
        _mock_pipelines_table.update_item.assert_not_called()
        _mock_pipelines_table.put_item.assert_not_called()

    @pytest.mark.parametrize("value", ["", None, "x"])
    def test_refusal_is_on_presence_not_value(self, value):
        with pytest.raises(ValueError, match=r"target_config\.jdbc_user"):
            _reject_plaintext_target_credentials({"type": "S3_PARQUET", "jdbc_user": value})

    @pytest.mark.parametrize(
        "target_config",
        [None, "not-a-dict", [], {}, {"type": "JDBC", "secret_arn": "arn:aws:secretsmanager:r:1:secret:x-AbCdEf"}],
    )
    def test_configs_without_the_keys_pass(self, target_config):
        """Non-dicts are left for the validators' own errors; a reference is fine."""
        _reject_plaintext_target_credentials(target_config)
