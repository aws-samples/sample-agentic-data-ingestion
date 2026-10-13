# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for manual-mode validation and provisioning (P20).

Covers the new code paths in ``handlers/pipelines.py``:

    * ``_validate_jdbc_source_config_manual`` (P20 Task 5.2)
    * ``_validate_jdbc_target_config_manual`` (P20 Task 5.4)
    * ``_build_jdbc_source_physical_requirements`` mode-dispatch (Task 5.3)
    * ``_build_jdbc_target_physical_requirements`` mode-dispatch (Task 5.4)
    * ``_normalise_sg_list`` JSON round-trip (Task 5.1)

Test scope (P20 Task 5.13):
    * Happy path: source-only manual, target-only manual, both
    * Each required field missing → HTTP 400 with field-named message
    * Invalid regex on secret_arn / subnet_id / sg-id → HTTP 400
    * Invalid JSON for security_group_ids → HTTP 400
    * Existing P17 / P19 select_rds and glue_connection cases unchanged
    * Optional availability_zone (Req 6.3, 7.2)
    * Helper makes no RDS calls in manual mode (Req 7.3)

Validates: P20 Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 7.1, 7.2, 7.3, 7.4, 7.5, 12.5
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

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

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)
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
    from handlers.pipelines import (
        _build_jdbc_source_physical_requirements,
        _build_jdbc_target_physical_requirements,
        _normalise_sg_list,
        _validate_jdbc_source_config,
        _validate_jdbc_target_config,
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


def _valid_manual_source_config() -> dict:
    """Return a valid manual-mode JDBC source config."""
    return {
        "_connection_mode": "manual",
        "engine": "postgres",
        "jdbc_url": "jdbc:postgresql://onprem.example.com:5432/analytics",
        "jdbc_driver": "org.postgresql.Driver",
        "jdbc_schema": "public",
        "jdbc_table": "orders",
        "secret_arn": ("arn:aws:secretsmanager:us-west-2:123456789012:secret:onprem-pg-XYZ"),
        "subnet_id": "subnet-0a1b2c3d4e5f6789a",
        "security_group_ids": json.dumps(["sg-0a1b2c3d4e5f6789a"]),
        "read_mode": "full_table",
    }


def _valid_manual_target_config() -> dict:
    """Return a valid manual-mode JDBC target config."""
    return {
        "_connection_mode": "manual",
        "type": "JDBC",
        "engine": "postgres",
        "jdbc_url": "jdbc:postgresql://onprem.example.com:5432/analytics",
        "jdbc_driver": "org.postgresql.Driver",
        "jdbc_table": "analytics.users_mirror",
        "database_name": "analytics",
        "table_name": "users_mirror",
        "secret_arn": ("arn:aws:secretsmanager:us-west-2:123456789012:secret:onprem-pg-XYZ"),
        "subnet_id": "subnet-0a1b2c3d4e5f6789a",
        "security_group_ids": json.dumps(["sg-0a1b2c3d4e5f6789a"]),
    }


# ---------------------------------------------------------------------------
# _normalise_sg_list (Task 5.1)
# ---------------------------------------------------------------------------


class TestNormaliseSgList:
    """Validates: P20 Req 9.3."""

    def test_accepts_list(self):
        result = _normalise_sg_list(["sg-aaaa1111", "sg-bbbb2222"], "field")
        assert result == ["sg-aaaa1111", "sg-bbbb2222"]

    def test_accepts_json_encoded_string(self):
        result = _normalise_sg_list('["sg-aaaa1111", "sg-bbbb2222"]', "field")
        assert result == ["sg-aaaa1111", "sg-bbbb2222"]

    def test_rejects_invalid_json(self):
        with pytest.raises(ValueError, match="JSON-encoded"):
            _normalise_sg_list("not-json[]", "field")

    def test_rejects_non_list_json(self):
        with pytest.raises(ValueError, match="must be a list"):
            _normalise_sg_list('"a-string"', "field")

    def test_rejects_mixed_type_list(self):
        with pytest.raises(ValueError, match="mixed types"):
            _normalise_sg_list(["sg-aaaa1111", 42], "field")  # type: ignore

    def test_rejects_other_types(self):
        with pytest.raises(ValueError, match="must be a list"):
            _normalise_sg_list({"key": "val"}, "field")  # type: ignore


# ---------------------------------------------------------------------------
# Source-side manual validation
# ---------------------------------------------------------------------------


class TestSourceManualValidation:
    """Validates: P20 Req 6.1, 6.2, 6.4."""

    def test_happy_path_passes(self):
        config = _valid_manual_source_config()
        # Should not raise.
        _validate_jdbc_source_config(config)
        # security_group_ids was normalised to a list.
        assert config["security_group_ids"] == ["sg-0a1b2c3d4e5f6789a"]

    @pytest.mark.parametrize(
        "missing_field",
        ["engine", "jdbc_url", "jdbc_driver", "secret_arn", "subnet_id", "security_group_ids"],
    )
    def test_missing_required_field_raises(self, missing_field: str):
        config = _valid_manual_source_config()
        del config[missing_field]
        with pytest.raises(ValueError, match=f"source_config.{missing_field}"):
            _validate_jdbc_source_config(config)

    def test_invalid_jdbc_url_raises(self):
        config = _valid_manual_source_config()
        config["jdbc_url"] = "postgresql://db:5432/a"  # missing 'jdbc:' prefix
        with pytest.raises(ValueError, match="must start with 'jdbc:'"):
            _validate_jdbc_source_config(config)

    def test_invalid_secret_arn_raises(self):
        config = _valid_manual_source_config()
        config["secret_arn"] = "not-an-arn"
        with pytest.raises(ValueError, match="not a valid Secrets Manager ARN"):
            _validate_jdbc_source_config(config)

    def test_invalid_subnet_id_raises(self):
        config = _valid_manual_source_config()
        config["subnet_id"] = "subnet-INVALID"
        with pytest.raises(ValueError, match="not a valid subnet ID"):
            _validate_jdbc_source_config(config)

    def test_invalid_security_group_id_raises(self):
        config = _valid_manual_source_config()
        config["security_group_ids"] = json.dumps(["not-a-sg-id"])
        with pytest.raises(ValueError, match="invalid security group ID"):
            _validate_jdbc_source_config(config)

    def test_empty_security_group_list_raises(self):
        config = _valid_manual_source_config()
        config["security_group_ids"] = json.dumps([])
        with pytest.raises(ValueError, match="non-empty list"):
            _validate_jdbc_source_config(config)

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be an object"):
            _validate_jdbc_source_config("not-a-dict")  # type: ignore

    def test_unknown_engine_raises(self):
        config = _valid_manual_source_config()
        config["engine"] = "mongo"
        with pytest.raises(ValueError, match="must be one of"):
            _validate_jdbc_source_config(config)


# ---------------------------------------------------------------------------
# Target-side manual validation
# ---------------------------------------------------------------------------


class TestTargetManualValidation:
    """Validates: P20 Req 6.5, 7.5."""

    def test_happy_path_passes(self):
        config = _valid_manual_target_config()
        _validate_jdbc_target_config(config)
        assert config["security_group_ids"] == ["sg-0a1b2c3d4e5f6789a"]

    @pytest.mark.parametrize(
        "missing_field",
        ["engine", "jdbc_url", "jdbc_driver", "secret_arn", "subnet_id", "security_group_ids"],
    )
    def test_missing_required_field_raises(self, missing_field: str):
        config = _valid_manual_target_config()
        del config[missing_field]
        with pytest.raises(ValueError, match=f"target_config.{missing_field}"):
            _validate_jdbc_target_config(config)

    def test_invalid_jdbc_url_raises(self):
        config = _valid_manual_target_config()
        config["jdbc_url"] = "missing-scheme"
        with pytest.raises(ValueError, match="must start with 'jdbc:'"):
            _validate_jdbc_target_config(config)

    def test_invalid_secret_arn_raises(self):
        config = _valid_manual_target_config()
        config["secret_arn"] = "not-an-arn"
        with pytest.raises(ValueError, match="not a valid Secrets Manager ARN"):
            _validate_jdbc_target_config(config)

    def test_unknown_engine_raises(self):
        config = _valid_manual_target_config()
        config["engine"] = "mongo"
        with pytest.raises(ValueError, match="must be one of"):
            _validate_jdbc_target_config(config)

    def test_non_manual_mode_adds_no_networking_checks(self):
        """Non-manual modes add no NETWORKING checks in this validator.

        `select_rds` still owes the credential reference — it creates the
        Glue Connection, and Glue rejects a credential-less JDBC connection
        — so the config below carries `secret_arn` and nothing else. The
        point of this test is what it does NOT require: no `subnet_id`, no
        `security_group_ids`, no `engine`, no `jdbc_*`. Those either come
        from the described RDS instance or are checked inline in
        create_pipeline's `JDBC` / `RDS_DATABASE` branches, which run for
        all three modes (P31 R9.2) — see test_jdbc_target_mode_validation.py.
        """
        config = {
            "_connection_mode": "select_rds",
            "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:onprem-pg-XYZ",
        }
        _validate_jdbc_target_config(config)

    def test_select_rds_without_secret_arn_raises(self):
        """`select_rds` provisions the connection, so it needs a credential.

        Previously this returned None: the dispatcher had no `select_rds`
        branch, and its only caller gated the whole call on
        `_connection_mode == "manual"`. A target config like this one was
        accepted with HTTP 201 and then failed `CreateConnection` with
        Glue's generic "Validation for connection properties failed".
        """
        config = {"_connection_mode": "select_rds"}
        with pytest.raises(ValueError, match=r"target_config\.secret_arn is required"):
            _validate_jdbc_target_config(config)

    def test_legacy_payload_with_no_connection_mode_defaults_to_select_rds(self):
        """No `_connection_mode` means `select_rds` (Req 12.1), so the same rule applies."""
        with pytest.raises(ValueError, match=r"target_config\.secret_arn is required"):
            _validate_jdbc_target_config({"jdbc_url": "jdbc:postgresql://h:5432/d"})

    def test_glue_connection_mode_needs_no_secret_arn(self):
        """The user's existing connection supplies the credential reference.

        `_augment_glue_connection_config` fills `secret_arn` from the
        connection's `SECRET_ID`, so the request itself need not carry one.
        Since T-19 (`66e97e5`) it refuses a target connection that has no
        `SECRET_ID`; the connection's inline credentials are never used
        (invariant `LGR-819980f-1`).
        """
        _validate_jdbc_target_config({"_connection_mode": "glue_connection"})

    def test_named_glue_connection_exempts_any_mode(self):
        """The exemption keys on `glue_connection_name`, not the mode string.

        `_provision_jdbc_target_connection` early-returns on that field in
        ANY mode, so no connection is created and no credential is needed.
        """
        _validate_jdbc_target_config({"_connection_mode": "select_rds", "glue_connection_name": "my-existing-conn"})


# ---------------------------------------------------------------------------
# Backward compatibility — existing P17 / P19 modes unchanged
# ---------------------------------------------------------------------------


class TestExistingModesUnchanged:
    """Validates: P20 Req 6.6, 12.5."""

    def test_select_rds_legacy_payload_works(self):
        """Pre-P20 payloads with no _connection_mode default to select_rds."""
        config = {
            # No _connection_mode field — should default to select_rds.
            "engine": "postgres",
            "jdbc_url": "jdbc:postgresql://db.example.com:5432/orders",
            "jdbc_driver": "org.postgresql.Driver",
            "jdbc_schema": "public",
            "jdbc_table": "orders",
            "secret_arn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds-AbCdEf"),
            "read_mode": "full_table",
        }
        _validate_jdbc_source_config(config)

    def test_glue_connection_mode_works(self):
        config = {
            "_connection_mode": "glue_connection",
            "engine": "postgres",
            "jdbc_url": "jdbc:postgresql://db.example.com:5432/orders",
            "jdbc_driver": "org.postgresql.Driver",
            "jdbc_schema": "public",
            "jdbc_table": "orders",
            "secret_arn": ("arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds-AbCdEf"),
            "read_mode": "full_table",
        }
        _validate_jdbc_source_config(config)


# ---------------------------------------------------------------------------
# Physical-requirements helper
# ---------------------------------------------------------------------------


class TestSourcePhysicalRequirements:
    """Validates: P20 Req 7.1, 7.2, 7.3, 7.4."""

    def test_manual_mode_builds_correct_shape(self):
        config = _valid_manual_source_config()
        _validate_jdbc_source_config(config)  # normalises sg list
        physical = _build_jdbc_source_physical_requirements(config)
        assert physical == {
            "SubnetId": "subnet-0a1b2c3d4e5f6789a",
            "SecurityGroupIdList": ["sg-0a1b2c3d4e5f6789a"],
        }

    def test_manual_mode_includes_az_when_present(self):
        config = _valid_manual_source_config()
        config["availability_zone"] = "us-west-2a"
        _validate_jdbc_source_config(config)
        physical = _build_jdbc_source_physical_requirements(config)
        assert physical["AvailabilityZone"] == "us-west-2a"

    def test_manual_mode_omits_az_when_absent(self):
        config = _valid_manual_source_config()
        _validate_jdbc_source_config(config)
        # No availability_zone key in the config.
        physical = _build_jdbc_source_physical_requirements(config)
        assert "AvailabilityZone" not in physical

    def test_glue_connection_mode_raises(self):
        """Helper MUST NOT be called for glue_connection mode."""
        config = {
            "_connection_mode": "glue_connection",
            "subnet_id": "subnet-aaaa1111",
            "security_group_ids": ["sg-aaaa1111"],
        }
        with pytest.raises(ValueError, match="should not be called"):
            _build_jdbc_source_physical_requirements(config)

    def test_select_rds_mode_unchanged(self):
        """select_rds shape is the same as manual mode."""
        config = {
            "_connection_mode": "select_rds",
            "subnet_id": "subnet-aaaa1111",
            "security_group_ids": ["sg-aaaa1111"],
            "availability_zone": "us-west-2a",
        }
        physical = _build_jdbc_source_physical_requirements(config)
        assert physical == {
            "SubnetId": "subnet-aaaa1111",
            "SecurityGroupIdList": ["sg-aaaa1111"],
            "AvailabilityZone": "us-west-2a",
        }


class TestTargetPhysicalRequirements:
    """Validates: P20 Req 7.5."""

    def test_manual_mode_builds_correct_shape(self):
        config = _valid_manual_target_config()
        _validate_jdbc_target_config(config)
        physical = _build_jdbc_target_physical_requirements(config)
        assert physical == {
            "SubnetId": "subnet-0a1b2c3d4e5f6789a",
            "SecurityGroupIdList": ["sg-0a1b2c3d4e5f6789a"],
        }

    def test_glue_connection_mode_returns_none(self):
        """User-supplied connection's PhysicalConnectionRequirements is reused as-is."""
        config = {
            "_connection_mode": "glue_connection",
            "glue_connection_name": "my-existing",
        }
        assert _build_jdbc_target_physical_requirements(config) is None

    def test_legacy_payload_returns_none(self):
        """P14 backward-compat — legacy payloads without subnet_id return None."""
        config = {
            # No _connection_mode field, no subnet_id.
            "jdbc_url": "jdbc:postgresql://db:5432/orders",
        }
        assert _build_jdbc_target_physical_requirements(config) is None
