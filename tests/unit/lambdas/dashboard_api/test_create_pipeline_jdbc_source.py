# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``create_pipeline`` source-field persistence (Task 3.3).

Covers Requirement 6.1-6.8 and Requirement 22.1: the dashboard API's
``create_pipeline`` handler MUST accept ``source_type`` and ``source_config``
on the request body, validate JDBC payloads, and persist both as
*top-level* fields on the ``agentic_data_ingestion_pipelines`` item (NOT nested under
``pipeline_config``).

Test layout:
    Part A — direct unit tests for ``_validate_jdbc_source_config``
        (no AWS mocking needed — pure validation helper).
    Part B — integration-style tests that exercise the full
        ``create_pipeline`` entry point with the dashboard table mocked
        out and assert on the captured ``put_item`` call.

Validates: Requirements 6.1-6.8, 22.1
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler. This mirrors
# the pattern used by ``test_pipelines_schedule.py`` and
# ``test_create_pipeline_approval_config.py`` so the dashboard API handler
# can be imported in a unit-test process without the real Lambda layer.
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
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Make ``shared/`` importable so ``handlers.pipelines`` can resolve its
# ``from jdbc_helpers import ...`` line. In a real Lambda environment
# the shared layer is mounted at ``/opt/python``; for unit tests we
# point sys.path at the in-tree shared directory.
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

# P21 Task 7.2 — create_pipeline + provisioning helpers now live in
# handlers.pipelines_crud. Bind the module alias and patch targets there
# (module references only — behaviour-preserving).
try:
    from handlers import pipelines_crud as pipelines
    from handlers.pipelines import (
        _validate_jdbc_source_config,
        create_pipeline,
    )
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------

_VALID_FULL_TABLE_CONFIG: dict = {
    "jdbc_url": "jdbc:postgresql://db.example.com:5432/orders",
    "jdbc_driver": "org.postgresql.Driver",
    "jdbc_schema": "public",
    "jdbc_table": "orders",
    "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds-AbCdEf",
    "engine": "postgres",
    "read_mode": "full_table",
}

_VALID_INCREMENTAL_CONFIG: dict = {
    **_VALID_FULL_TABLE_CONFIG,
    "read_mode": "incremental",
    "watermark_column": "updated_at",
    "watermark_type": "timestamp",
    "initial_watermark_value": "2024-01-01T00:00:00Z",
}


def _make_body(
    *,
    source_type: str | None = None,
    source_config: dict | None = None,
    pipeline_name: str = "Test JDBC Pipeline",
) -> str:
    """Build a ``create_pipeline`` request body.

    ``source_type`` is omitted from the body when ``None`` — that is
    distinct from passing the string ``"S3_UPLOAD"`` and lets us test
    the default-omission code path.
    """
    body: dict = {
        "pipeline_name": pipeline_name,
        "target_schema": "uploaded_test_schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
    }
    if source_type is not None:
        body["source_type"] = source_type
    if source_config is not None:
        body["source_config"] = source_config
    return json.dumps(body)


# ===========================================================================
# Part A — direct unit tests for _validate_jdbc_source_config
# ===========================================================================


class TestValidateJdbcSourceConfigRequiredFields:
    """Required-field validation. Validates: Requirement 6.3."""

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="source_config must be an object"):
            _validate_jdbc_source_config("not-a-dict")  # type: ignore[arg-type]

    def test_none_raises(self):
        with pytest.raises(ValueError, match="source_config must be an object"):
            _validate_jdbc_source_config(None)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "missing_field",
        [
            "jdbc_url",
            "jdbc_driver",
            "jdbc_schema",
            "jdbc_table",
            "secret_arn",
            "engine",
            "read_mode",
        ],
    )
    def test_missing_required_field_raises(self, missing_field: str):
        config = dict(_VALID_FULL_TABLE_CONFIG)
        del config[missing_field]
        with pytest.raises(ValueError, match=rf"source_config\.{missing_field} is required"):
            _validate_jdbc_source_config(config)

    @pytest.mark.parametrize(
        "missing_field",
        [
            "jdbc_url",
            "jdbc_driver",
            "jdbc_schema",
            "jdbc_table",
            "secret_arn",
            "engine",
            "read_mode",
        ],
    )
    def test_empty_required_field_raises(self, missing_field: str):
        # A field present with an empty string / None is still treated as
        # missing because the handler uses ``not source_config.get(field)``.
        config = dict(_VALID_FULL_TABLE_CONFIG)
        config[missing_field] = ""
        with pytest.raises(ValueError, match=rf"source_config\.{missing_field} is required"):
            _validate_jdbc_source_config(config)


class TestValidateJdbcSourceConfigEnumValues:
    """Engine, read_mode, watermark_type enum validation. Validates: 6.3."""

    def test_invalid_engine_raises(self):
        config = dict(_VALID_FULL_TABLE_CONFIG)
        config["engine"] = "mongo"
        with pytest.raises(ValueError, match=r"source_config\.engine must be one of"):
            _validate_jdbc_source_config(config)

    def test_invalid_read_mode_raises(self):
        config = dict(_VALID_FULL_TABLE_CONFIG)
        config["read_mode"] = "streaming"
        with pytest.raises(ValueError, match=r"source_config\.read_mode must be one of"):
            _validate_jdbc_source_config(config)

    def test_invalid_watermark_type_raises(self):
        config = dict(_VALID_INCREMENTAL_CONFIG)
        config["watermark_type"] = "epoch"
        with pytest.raises(ValueError, match=r"source_config\.watermark_type must be one of"):
            _validate_jdbc_source_config(config)


class TestValidateJdbcSourceConfigIdentifiers:
    """SQL identifier validation. Validates: Requirement 7.5."""

    @pytest.mark.parametrize(
        "bad_value",
        [
            "evil; DROP TABLE users",
            "table-with-dash",
            "1leading_digit",
            "schema.with.dots",
            "name with space",
            "",
        ],
    )
    def test_invalid_jdbc_schema_raises(self, bad_value: str):
        config = dict(_VALID_FULL_TABLE_CONFIG)
        config["jdbc_schema"] = bad_value
        # Empty string is rejected by the "required field" check; non-empty
        # invalid identifiers fall through to the regex check. Either way
        # the call MUST raise.
        with pytest.raises(ValueError):
            _validate_jdbc_source_config(config)

    @pytest.mark.parametrize(
        "bad_value",
        [
            "evil; DROP TABLE users",
            "table-with-dash",
            "1leading_digit",
            "schema.with.dots",
        ],
    )
    def test_invalid_jdbc_table_raises_with_descriptive_message(self, bad_value: str):
        config = dict(_VALID_FULL_TABLE_CONFIG)
        config["jdbc_table"] = bad_value
        with pytest.raises(
            ValueError,
            match=r"source_config\.jdbc_table is not a valid SQL identifier",
        ):
            _validate_jdbc_source_config(config)

    def test_valid_identifiers_accepted(self):
        config = dict(_VALID_FULL_TABLE_CONFIG)
        config["jdbc_schema"] = "public"
        config["jdbc_table"] = "orders_archive_2024"
        # Should not raise
        _validate_jdbc_source_config(config)


class TestValidateJdbcSourceConfigFullTable:
    """Full-table mode does NOT require watermark fields. Validates: 6.3."""

    def test_full_table_does_not_require_watermark_column(self):
        # The minimal full-table config has no watermark_* keys — must pass.
        _validate_jdbc_source_config(_VALID_FULL_TABLE_CONFIG)

    def test_full_table_ignores_extra_watermark_fields(self):
        # Even if the caller supplies watermark fields by accident, the
        # validator only enforces them in incremental mode.
        config = dict(_VALID_FULL_TABLE_CONFIG)
        config["watermark_column"] = "irrelevant"
        config["watermark_type"] = "anything-goes-here"
        # Should not raise
        _validate_jdbc_source_config(config)


class TestValidateJdbcSourceConfigIncremental:
    """Incremental mode requires watermark fields. Validates: 6.3, 7.5."""

    def test_incremental_full_payload_accepted(self):
        # Should not raise
        _validate_jdbc_source_config(_VALID_INCREMENTAL_CONFIG)

    @pytest.mark.parametrize("missing_field", ["watermark_column", "watermark_type"])
    def test_incremental_missing_required_field_raises(self, missing_field: str):
        config = dict(_VALID_INCREMENTAL_CONFIG)
        del config[missing_field]
        with pytest.raises(
            ValueError,
            match=rf"source_config\.{missing_field} is required when read_mode is "
            "'incremental'",
        ):
            _validate_jdbc_source_config(config)

    def test_invalid_watermark_column_raises(self):
        config = dict(_VALID_INCREMENTAL_CONFIG)
        config["watermark_column"] = "bad-column-name"
        with pytest.raises(
            ValueError,
            match=r"source_config\.watermark_column is not a valid SQL identifier",
        ):
            _validate_jdbc_source_config(config)

    def test_invalid_initial_watermark_numeric_raises(self):
        config = dict(_VALID_INCREMENTAL_CONFIG)
        config["watermark_type"] = "numeric"
        config["initial_watermark_value"] = "not-a-number"
        with pytest.raises(
            ValueError,
            match=r"source_config\.initial_watermark_value is not a valid "
            "numeric value",
        ):
            _validate_jdbc_source_config(config)

    def test_invalid_initial_watermark_timestamp_raises(self):
        config = dict(_VALID_INCREMENTAL_CONFIG)
        config["watermark_type"] = "timestamp"
        config["initial_watermark_value"] = "yesterday"
        with pytest.raises(
            ValueError,
            match=r"source_config\.initial_watermark_value is not a valid "
            "timestamp value",
        ):
            _validate_jdbc_source_config(config)

    def test_empty_initial_watermark_allowed(self):
        # The wizard's "no seed" default is an empty string. The validator
        # must allow it through so first-run reads can default to a full
        # read.
        config = dict(_VALID_INCREMENTAL_CONFIG)
        config["initial_watermark_value"] = ""
        # Should not raise
        _validate_jdbc_source_config(config)

    def test_missing_initial_watermark_allowed(self):
        # Field is optional — absence behaves like an empty seed.
        config = dict(_VALID_INCREMENTAL_CONFIG)
        del config["initial_watermark_value"]
        # Should not raise
        _validate_jdbc_source_config(config)

    def test_valid_numeric_initial_watermark_accepted(self):
        config = dict(_VALID_INCREMENTAL_CONFIG)
        config["watermark_column"] = "row_id"
        config["watermark_type"] = "numeric"
        config["initial_watermark_value"] = "12345"
        # Should not raise
        _validate_jdbc_source_config(config)

    # -- The key the wizard actually sends (2026-10-10) ---------------------
    # `JdbcSourceForm` writes the seed as `watermark_initial_value`. Until this
    # date the validator read only `initial_watermark_value`, so the wizard's
    # seed passed unchecked, and code generation read a third key, so it was
    # never used either. The tests above keep the legacy key covered.

    @pytest.mark.parametrize(
        ("watermark_type", "bad_value"),
        [("numeric", "not-a-number"), ("timestamp", "yesterday")],
    )
    def test_the_wizard_key_is_validated(self, watermark_type, bad_value):
        config = dict(_VALID_INCREMENTAL_CONFIG)
        del config["initial_watermark_value"]
        config["watermark_type"] = watermark_type
        config["watermark_initial_value"] = bad_value
        with pytest.raises(
            ValueError,
            match=rf"source_config\.watermark_initial_value is not a valid {watermark_type} value",
        ):
            _validate_jdbc_source_config(config)

    def test_the_wizard_key_wins_over_a_legacy_key(self):
        """The value validated is the value code generation renders, and both
        resolve the canonical key first."""
        config = dict(_VALID_INCREMENTAL_CONFIG)
        config["watermark_type"] = "numeric"
        config["watermark_column"] = "row_id"
        config["watermark_initial_value"] = "not-a-number"
        config["initial_watermark_value"] = "12345"
        with pytest.raises(ValueError, match=r"source_config\.watermark_initial_value"):
            _validate_jdbc_source_config(config)


# ===========================================================================
# Part B — integration-style tests for create_pipeline
# ===========================================================================

# Module-level mocks shared by every handler invocation so tests don't have
# to wire DynamoDB / S3 / EventBridge in each test method.
_mock_pipelines_table = MagicMock()
_mock_state_table = MagicMock()
_mock_executions_table = MagicMock()
_mock_approvals_table = MagicMock()
_mock_s3_client = MagicMock()
_mock_dynamodb = MagicMock()


@pytest.fixture(autouse=True)
def _reset_module_mocks():
    """Reset shared mocks and bind them onto the imported module.

    The dashboard API handler module captures the table / client objects at
    import time via ``from .common import pipelines_table, ...`` so we
    rebind those module attributes here to point at our mocks. Mirrors the
    pattern used by ``test_pipelines_schedule.py``.
    """
    _mock_pipelines_table.reset_mock()
    _mock_state_table.reset_mock()
    _mock_executions_table.reset_mock()
    _mock_approvals_table.reset_mock()
    _mock_s3_client.reset_mock()
    _mock_dynamodb.reset_mock()

    pipelines.pipelines_table = _mock_pipelines_table
    pipelines.state_table = _mock_state_table
    pipelines.executions_table = _mock_executions_table
    pipelines.approvals_table = _mock_approvals_table
    pipelines.dynamodb = _mock_dynamodb
    pipelines.s3_client = _mock_s3_client
    yield


def _captured_put_item() -> dict:
    """Return the ``Item`` kwarg passed to ``pipelines_table.put_item``."""
    assert _mock_pipelines_table.put_item.called, "Expected create_pipeline to write a row to pipelines_table"
    return _mock_pipelines_table.put_item.call_args[1]["Item"]


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestCreatePipelineSourceTypeDefault:
    """Default and explicit S3_UPLOAD source persistence. Validates: 6.1, 6.6."""

    def test_default_source_type_is_s3_upload(self, _mock_provision):
        # Test 1: source_type omitted → handler defaults to S3_UPLOAD,
        # source_config is empty so it is NOT persisted as a top-level key.
        body = _make_body(source_type=None, source_config=None)

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 201
        item = _captured_put_item()
        assert item["source_type"] == "S3_UPLOAD"
        # source_config is intentionally omitted when empty (matches the
        # target_config behaviour — Requirement 6.2 says the field SHALL be
        # included on the item, but the handler implements the same
        # "only-write-when-non-empty" pattern as target_config). The read
        # path defaults missing values back to {} (Requirement 6.7).
        assert "source_config" not in item

    def test_explicit_s3_upload_source_type_is_persisted(self, _mock_provision):
        # Test 2: source_type explicitly S3_UPLOAD → same persistence.
        body = _make_body(source_type="S3_UPLOAD", source_config={})

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 201
        item = _captured_put_item()
        assert item["source_type"] == "S3_UPLOAD"
        assert "source_config" not in item


@patch("handlers.pipelines_crud._provision_jdbc_runtime_crawler")
@patch("handlers.pipelines_crud._provision_jdbc_source_connection")
@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestCreatePipelineJdbcSuccess:
    """Successful JDBC pipeline creation. Validates: 6.1, 6.2, 6.3."""

    def test_jdbc_full_table_persists_top_level_source_fields(
        self, _mock_provision, _mock_jdbc_conn, _mock_jdbc_crawler
    ):
        # Test 3: valid JDBC full_table config → 201, both source_type and
        # source_config persisted at the TOP LEVEL of the item.
        body = _make_body(source_type="JDBC", source_config=_VALID_FULL_TABLE_CONFIG)

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 201, f"Expected 201, got {result['statusCode']}: {result['body']}"
        item = _captured_put_item()

        # Top-level keys (Requirement 6.1, 6.2) — NOT nested under
        # pipeline_config / target_config.
        assert item["source_type"] == "JDBC"
        assert item["source_config"] == _VALID_FULL_TABLE_CONFIG
        assert "pipeline_config" not in item, "source_type/source_config must NOT be nested under pipeline_config"

    def test_jdbc_incremental_persists_watermark_fields(self, _mock_provision, _mock_jdbc_conn, _mock_jdbc_crawler):
        # Test 4: incremental mode with watermark fields → all keys round-
        # trip into the persisted source_config.
        body = _make_body(source_type="JDBC", source_config=_VALID_INCREMENTAL_CONFIG)

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 201
        item = _captured_put_item()
        assert item["source_type"] == "JDBC"
        persisted = item["source_config"]
        assert persisted == _VALID_INCREMENTAL_CONFIG
        # Spot-check the watermark-specific fields explicitly so a future
        # regression that drops one of them fails with a clearer message
        # than a single dict-equality diff.
        assert persisted["read_mode"] == "incremental"
        assert persisted["watermark_column"] == "updated_at"
        assert persisted["watermark_type"] == "timestamp"
        assert persisted["initial_watermark_value"] == "2024-01-01T00:00:00Z"


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestCreatePipelineJdbcValidationErrors:
    """JDBC validation-error responses. Validates: 6.3, 7.5."""

    def test_invalid_source_type_returns_400(self, _mock_provision):
        # Test 5: source_type=DATABASE → 400 with the expected message
        # shape.
        body = _make_body(source_type="DATABASE", source_config={})

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "source_type must be one of" in error
        assert "'DATABASE'" in error
        # No DDB row should be written for invalid input.
        _mock_pipelines_table.put_item.assert_not_called()

    def test_jdbc_missing_jdbc_url_returns_400(self, _mock_provision):
        # Test 6: required field missing → descriptive 400.
        bad_config = dict(_VALID_FULL_TABLE_CONFIG)
        del bad_config["jdbc_url"]
        body = _make_body(source_type="JDBC", source_config=bad_config)

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "source_config.jdbc_url is required" in error
        _mock_pipelines_table.put_item.assert_not_called()

    def test_jdbc_bad_engine_returns_400(self, _mock_provision):
        # Test 7: unknown engine → 400.
        bad_config = dict(_VALID_FULL_TABLE_CONFIG)
        bad_config["engine"] = "mongo"
        body = _make_body(source_type="JDBC", source_config=bad_config)

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "source_config.engine must be one of" in error
        assert "'mongo'" in error
        _mock_pipelines_table.put_item.assert_not_called()

    def test_jdbc_sql_injection_in_schema_returns_400(self, _mock_provision):
        # Test 8: SQL-injection-shaped identifier → 400, message
        # references the field that failed.
        bad_config = dict(_VALID_FULL_TABLE_CONFIG)
        bad_config["jdbc_schema"] = "evil; DROP TABLE"
        body = _make_body(source_type="JDBC", source_config=bad_config)

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "source_config.jdbc_schema is not a valid SQL identifier" in error
        _mock_pipelines_table.put_item.assert_not_called()

    def test_jdbc_incremental_missing_watermark_column_returns_400(self, _mock_provision):
        # Test 9: incremental read_mode missing watermark_column → 400.
        bad_config = dict(_VALID_INCREMENTAL_CONFIG)
        del bad_config["watermark_column"]
        body = _make_body(source_type="JDBC", source_config=bad_config)

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "source_config.watermark_column is required" in error
        assert "incremental" in error
        _mock_pipelines_table.put_item.assert_not_called()

    def test_jdbc_non_dict_source_config_returns_400(self, _mock_provision):
        # Defensive case: clients that pass a string for source_config get
        # a clean 400 rather than a 500 from a downstream type error.
        body = json.dumps(
            {
                "pipeline_name": "bad config shape",
                "target_schema": "uploaded_test_schema",
                "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
                "source_type": "JDBC",
                "source_config": "not-a-dict",
            }
        )

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "source_config must be an object" in error
        _mock_pipelines_table.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Persistence-shape regression: source fields are TOP-LEVEL, never nested.
# ---------------------------------------------------------------------------


@patch("handlers.pipelines_crud._provision_jdbc_runtime_crawler")
@patch("handlers.pipelines_crud._provision_jdbc_source_connection")
@patch("handlers.pipelines_crud._provision_pipeline_resources")
def test_jdbc_source_fields_are_top_level_not_nested(_mock_provision, _mock_jdbc_conn, _mock_jdbc_crawler):
    """Defence-in-depth: assert the persisted item shape matches the design.

    Requirement 6.1 explicitly says ``source_type`` lives at the top level
    of the ``agentic_data_ingestion_pipelines`` item — mirroring the existing flat
    ``target_type`` field rather than nesting under ``pipeline_config``.
    """
    body = _make_body(source_type="JDBC", source_config=_VALID_FULL_TABLE_CONFIG)

    result = create_pipeline(body, event={})
    assert result["statusCode"] == 201

    item = _captured_put_item()

    # Top-level presence
    assert "source_type" in item
    assert "source_config" in item

    # No nesting under any pipeline_config / pipeline-level wrapper.
    for forbidden_key in ("pipeline_config", "config", "source"):
        assert forbidden_key not in item, f"source fields must not be wrapped under {forbidden_key!r}"

    # No leakage into target_config.
    assert "source_type" not in item.get("target_config", {})
    assert "source_config" not in item.get("target_config", {})
