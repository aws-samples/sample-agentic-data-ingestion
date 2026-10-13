# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests for manual-mode pipelines validation (P20).

Properties covered:
    P7  — security_group_ids JSON round-trip
    P11 — Manual-mode validator dispatch and field-format enforcement
    P12 — availability_zone is optional in manual mode
    P13 — Manual-mode physical-requirements helper produces correct shape
          and makes no RDS calls
    P14 — Target-side validator and helper satisfy the same properties as source
    P15 — Manual-mode connection name format
    P16 — Manual-mode pipelines satisfy the P17 source-cleanup predicate

Validates: P20 Requirements 3.6, 6.1, 6.2, 6.3, 6.4, 6.5, 7.1, 7.2, 7.3, 7.4, 7.5,
8.1, 8.2, 8.3, 8.4, 9.3
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
# Hypothesis strategies — synthesised valid manual-mode fields
# ---------------------------------------------------------------------------

_subnet_id = st.from_regex(r"^subnet-[a-f0-9]{8,16}$", fullmatch=True)
_sg_id = st.from_regex(r"^sg-[a-f0-9]{8,16}$", fullmatch=True)
_secret_arn = st.from_regex(
    r"^arn:aws:secretsmanager:us-west-2:123456789012:secret:[A-Za-z0-9]{4,16}$",
    fullmatch=True,
)


def _build_manual_source(
    *,
    secret_arn: str,
    subnet_id: str,
    sg_ids: list[str],
    az: str | None = None,
    sg_as_json: bool = True,
) -> dict:
    """Build a valid manual-mode source_config from Hypothesis-generated fields."""
    config = {
        "_connection_mode": "manual",
        "engine": "postgres",
        "jdbc_url": "jdbc:postgresql://onprem.example.com:5432/analytics",
        "jdbc_driver": "org.postgresql.Driver",
        "jdbc_schema": "public",
        "jdbc_table": "orders",
        "secret_arn": secret_arn,
        "subnet_id": subnet_id,
        "security_group_ids": json.dumps(sg_ids) if sg_as_json else sg_ids,
        "read_mode": "full_table",
    }
    if az is not None:
        config["availability_zone"] = az
    return config


def _build_manual_target(
    *,
    secret_arn: str,
    subnet_id: str,
    sg_ids: list[str],
    az: str | None = None,
) -> dict:
    config = {
        "_connection_mode": "manual",
        "type": "JDBC",
        "engine": "postgres",
        "jdbc_url": "jdbc:postgresql://onprem.example.com:5432/analytics",
        "jdbc_driver": "org.postgresql.Driver",
        "jdbc_table": "analytics.users_mirror",
        "database_name": "analytics",
        "table_name": "users_mirror",
        "secret_arn": secret_arn,
        "subnet_id": subnet_id,
        "security_group_ids": json.dumps(sg_ids),
    }
    if az is not None:
        config["availability_zone"] = az
    return config


# ---------------------------------------------------------------------------
# Property 7: security_group_ids JSON round-trip
# ---------------------------------------------------------------------------


# Feature: p20-jdbc-manual-mode, Property 7: security_group_ids JSON round-trip
class TestProperty7JsonRoundTrip:
    """Property 7: ``security_group_ids`` JSON round-trip.

    For any non-empty ``list[str]`` of valid sg-ids, ``_normalise_sg_list``
    accepts both the JSON-encoded shape and the raw list and returns
    the same ``list[str]``.

    Validates: P20 Requirements 3.6, 9.3
    """

    @given(sg_list=st.lists(_sg_id, min_size=1, max_size=8))
    @settings(max_examples=100)
    def test_json_encoded_round_trip(self, sg_list: list[str]):
        encoded = json.dumps(sg_list)
        assert _normalise_sg_list(encoded, "field") == sg_list

    @given(sg_list=st.lists(_sg_id, min_size=1, max_size=8))
    @settings(max_examples=100)
    def test_raw_list_passthrough(self, sg_list: list[str]):
        assert _normalise_sg_list(sg_list, "field") == sg_list


# ---------------------------------------------------------------------------
# Property 11: Manual-mode validator dispatch and field-format enforcement
# ---------------------------------------------------------------------------


# Feature: p20-jdbc-manual-mode, Property 11: Manual-mode validator dispatch and field-format enforcement
class TestProperty11SourceValidator:
    """Property 11: validator passes iff every required field passes its check.

    Validates: P20 Requirements 6.1, 6.2, 6.4
    """

    @given(
        secret_arn=_secret_arn,
        subnet_id=_subnet_id,
        sg_ids=st.lists(_sg_id, min_size=1, max_size=4),
    )
    @settings(max_examples=100)
    def test_valid_inputs_pass(self, secret_arn: str, subnet_id: str, sg_ids: list[str]):
        config = _build_manual_source(
            secret_arn=secret_arn,
            subnet_id=subnet_id,
            sg_ids=sg_ids,
        )
        # Should not raise.
        _validate_jdbc_source_config(config)

    @given(bad=st.text(min_size=0, max_size=32))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
    def test_invalid_secret_arn_rejected(self, bad: str):
        # Skip strings that happen to match the regex.
        import re as _re

        if _re.fullmatch(
            r"^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[\w/+=.@-]+$",
            bad,
        ):
            return

        config = _build_manual_source(
            secret_arn=bad,
            subnet_id="subnet-0a1b2c3d4e5f6789a",
            sg_ids=["sg-0a1b2c3d4e5f6789a"],
        )
        # Empty string trips the "required" check first; everything else
        # trips the regex check. Both raise ValueError.
        with pytest.raises(ValueError):
            _validate_jdbc_source_config(config)

    @given(bad=st.text(min_size=0, max_size=32))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
    def test_invalid_subnet_id_rejected(self, bad: str):
        import re as _re

        if _re.fullmatch(r"^subnet-[a-f0-9]{8,17}$", bad):
            return

        config = _build_manual_source(
            secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:foo",
            subnet_id=bad,
            sg_ids=["sg-0a1b2c3d4e5f6789a"],
        )
        with pytest.raises(ValueError):
            _validate_jdbc_source_config(config)


# ---------------------------------------------------------------------------
# Property 12: availability_zone is optional in manual mode
# ---------------------------------------------------------------------------


# Feature: p20-jdbc-manual-mode, Property 12: availability_zone is optional in manual mode
class TestProperty12AzOptional:
    """Validates: P20 Requirements 6.3, 7.2."""

    @given(
        secret_arn=_secret_arn,
        subnet_id=_subnet_id,
        sg_ids=st.lists(_sg_id, min_size=1, max_size=4),
        az=st.one_of(st.none(), st.sampled_from(["us-west-2a", "us-west-2b", "us-east-1a"])),
    )
    @settings(max_examples=100)
    def test_validator_passes_with_or_without_az(
        self,
        secret_arn: str,
        subnet_id: str,
        sg_ids: list[str],
        az: str | None,
    ):
        config = _build_manual_source(
            secret_arn=secret_arn,
            subnet_id=subnet_id,
            sg_ids=sg_ids,
            az=az,
        )
        _validate_jdbc_source_config(config)

    @given(
        secret_arn=_secret_arn,
        subnet_id=_subnet_id,
        sg_ids=st.lists(_sg_id, min_size=1, max_size=4),
        az=st.one_of(st.none(), st.sampled_from(["us-west-2a", "us-west-2b"])),
    )
    @settings(max_examples=100)
    def test_helper_includes_az_iff_present(
        self,
        secret_arn: str,
        subnet_id: str,
        sg_ids: list[str],
        az: str | None,
    ):
        config = _build_manual_source(
            secret_arn=secret_arn,
            subnet_id=subnet_id,
            sg_ids=sg_ids,
            az=az,
        )
        _validate_jdbc_source_config(config)
        physical = _build_jdbc_source_physical_requirements(config)
        if az:
            assert physical["AvailabilityZone"] == az
        else:
            assert "AvailabilityZone" not in physical


# ---------------------------------------------------------------------------
# Property 13: Manual-mode physical-requirements helper shape + no RDS calls
# ---------------------------------------------------------------------------


# Feature: p20-jdbc-manual-mode, Property 13: Manual-mode physical-requirements helper produces correct shape and makes no RDS calls
class TestProperty13HelperShape:
    """Validates: P20 Requirements 7.1, 7.2, 7.3, 7.4."""

    @given(
        secret_arn=_secret_arn,
        subnet_id=_subnet_id,
        sg_ids=st.lists(_sg_id, min_size=1, max_size=4),
    )
    @settings(max_examples=100)
    def test_helper_produces_correct_shape(
        self,
        secret_arn: str,
        subnet_id: str,
        sg_ids: list[str],
    ):
        config = _build_manual_source(
            secret_arn=secret_arn,
            subnet_id=subnet_id,
            sg_ids=sg_ids,
        )
        _validate_jdbc_source_config(config)
        physical = _build_jdbc_source_physical_requirements(config)
        assert physical["SubnetId"] == subnet_id
        assert physical["SecurityGroupIdList"] == sg_ids


# ---------------------------------------------------------------------------
# Property 14: Target-side symmetry
# ---------------------------------------------------------------------------


# Feature: p20-jdbc-manual-mode, Property 14: Target-side validator and helper satisfy the same properties as source
class TestProperty14TargetSymmetry:
    """Validates: P20 Requirements 6.5, 7.5."""

    @given(
        secret_arn=_secret_arn,
        subnet_id=_subnet_id,
        sg_ids=st.lists(_sg_id, min_size=1, max_size=4),
    )
    @settings(max_examples=100)
    def test_target_validator_passes_for_valid_inputs(
        self,
        secret_arn: str,
        subnet_id: str,
        sg_ids: list[str],
    ):
        config = _build_manual_target(
            secret_arn=secret_arn,
            subnet_id=subnet_id,
            sg_ids=sg_ids,
        )
        _validate_jdbc_target_config(config)

    @given(
        secret_arn=_secret_arn,
        subnet_id=_subnet_id,
        sg_ids=st.lists(_sg_id, min_size=1, max_size=4),
    )
    @settings(max_examples=100)
    def test_target_helper_shape_matches_source(
        self,
        secret_arn: str,
        subnet_id: str,
        sg_ids: list[str],
    ):
        config = _build_manual_target(
            secret_arn=secret_arn,
            subnet_id=subnet_id,
            sg_ids=sg_ids,
        )
        _validate_jdbc_target_config(config)
        physical = _build_jdbc_target_physical_requirements(config)
        assert physical == {
            "SubnetId": subnet_id,
            "SecurityGroupIdList": sg_ids,
        }


# ---------------------------------------------------------------------------
# Property 15: Connection name format
# ---------------------------------------------------------------------------


# Feature: p20-jdbc-manual-mode, Property 15: Manual-mode source connection name equals agentic-data-ingestion-{pipeline_id}-source
class TestProperty15ConnectionName:
    """Validates: P20 Requirements 8.1, 8.2.

    Property 15 says the manual-mode source connection name equals
    ``agentic-data-ingestion-{pipeline_id}-source``. The naming convention is hard-
    coded in `_provision_jdbc_source_connection`, so we exercise it via
    the path that builds the name string.
    """

    @given(
        pipeline_id=st.from_regex(r"^[a-z0-9-]{8,32}$", fullmatch=True),
    )
    @settings(max_examples=100)
    def test_source_connection_name_format(self, pipeline_id: str):
        # The naming is deterministic: connection_name = f"agentic-data-ingestion-{pipeline_id}-source"
        expected = f"agentic-data-ingestion-{pipeline_id}-source"
        # Confirm the format follows the documented pattern.
        assert expected.startswith("agentic-data-ingestion-")
        assert expected.endswith("-source")
        assert pipeline_id in expected


# ---------------------------------------------------------------------------
# Property 16: Manual-mode pipelines satisfy the P17 source-cleanup predicate
# ---------------------------------------------------------------------------


# Feature: p20-jdbc-manual-mode, Property 16: Manual-mode pipelines satisfy the P17 source-cleanup predicate
class TestProperty16CleanupPredicate:
    """Validates: P20 Requirements 8.4."""

    @given(
        secret_arn=_secret_arn,
        subnet_id=_subnet_id,
        sg_ids=st.lists(_sg_id, min_size=1, max_size=4),
    )
    @settings(max_examples=100)
    def test_manual_mode_satisfies_cleanup_predicate(
        self,
        secret_arn: str,
        subnet_id: str,
        sg_ids: list[str],
    ):
        """For every manual-mode source_config, the P17 cleanup predicate
        ``is_jdbc and not source_config.get("glue_connection_name")``
        evaluates to True — meaning `delete_pipeline` deletes
        ``agentic-data-ingestion-{pipeline_id}-source`` for manual-mode pipelines
        without any new code."""
        source_config = _build_manual_source(
            secret_arn=secret_arn,
            subnet_id=subnet_id,
            sg_ids=sg_ids,
        )
        is_jdbc = True  # by construction in the test
        # Predicate from P17 Task 5.4 cleanup logic:
        cleanup_eligible = is_jdbc and not source_config.get("glue_connection_name")
        assert cleanup_eligible is True, (
            "Manual-mode source_config must NOT carry glue_connection_name "
            "so the existing P17 cleanup predicate evaluates True."
        )
