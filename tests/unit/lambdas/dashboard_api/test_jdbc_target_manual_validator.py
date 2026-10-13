# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Direct unit tests for ``_validate_jdbc_target_config_manual`` (p31 task 4.3).

Scope — the manual-mode *target* validator itself
(``handlers/pipelines_jdbc.py:489``), called directly rather than through its
dispatcher. The dispatcher ``_validate_jdbc_target_config`` is already covered
by ``test_pipelines_manual_mode.py`` and ``test_pipelines_manual_mode_prop.py``;
codegraph reports the helper as having no covering tests of its own, so the
regex and list-normalisation branches below were only ever exercised
incidentally. Those two files are NOT modified (p31 C7).

Expectations are derived from P20 Req 6.5 — which enumerates the manual-mode
field set as ``engine``, ``jdbc_url``, ``jdbc_driver``, ``secret_arn``,
``subnet_id``, ``security_group_ids`` plus *optional* ``availability_zone`` —
and from Req 6.3 (Glue derives the AZ from the subnet when omitted), not from
the validator's current output. Notably ``jdbc_table`` is NOT in that set: it is
enforced by the inline target-type branch in ``create_pipeline``, so no test
here asserts this validator checks it.

Credential handling (p31 C8, invariant ``LGR-819980f-1``): every secret value in
this file is a realistically *shaped* but entirely fake Secrets Manager ARN — a
resource identifier, never a plaintext credential. Nothing here exercises or
asserts anything about credential material.

Covers, per design §6 (R9.5):
    * every field in ``_JDBC_MANUAL_REQUIRED_FIELDS`` (missing and empty)
    * the ``jdbc:`` URL prefix rule
    * ``SECRET_ARN_RE`` / ``SUBNET_ID_RE`` / ``SG_ID_RE``
    * ``_normalise_sg_list``'s list-vs-JSON-string handling through the
      validator, including the in-place normalisation it documents
    * ``engine`` membership in ``SUPPORTED_ENGINES``

Validates: Requirements 9.5
"""

from __future__ import annotations

import json
import os
import re

import pytest

# ``handlers.common`` resolves table names through ``require_env`` at import
# time; ``local`` selects the documented development defaults. Same one-liner
# every other module in this directory uses. ``conftest.py`` here puts
# ``shared/`` and the dashboard-API Lambda package on sys.path, and
# ``tests/unit/conftest.py`` pins the AWS region (LGR-3e45e65-1), so no further
# per-module bootstrapping is needed.
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers.pipelines_jdbc import (  # noqa: I001 - must follow the env default above
    _JDBC_MANUAL_REQUIRED_FIELDS,
    _validate_jdbc_target_config_manual,
)
from jdbc_helpers import SUPPORTED_ENGINES

# A realistically shaped but fake Secrets Manager ARN. Identifier only — see
# the module docstring on C8 / LGR-819980f-1.
FAKE_SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:example/jdbc-target-Ab12Cd"


def _valid_manual_target_config() -> dict:
    """Return a manual-mode target_config carrying exactly the P20 Req 6.5 set.

    Deliberately minimal: no ``jdbc_table``, no ``availability_zone``, no
    ``_connection_mode``. The helper is called directly, so mode dispatch is
    the caller's job, and the two omitted fields are what Req 6.5 and Req 6.3
    describe as out-of-set and optional respectively.
    """
    return {
        "engine": "postgres",
        "jdbc_url": "jdbc:postgresql://onprem.example.com:5432/analytics",
        "jdbc_driver": "org.postgresql.Driver",
        "secret_arn": FAKE_SECRET_ARN,
        "subnet_id": "subnet-0a1b2c3d4e5f6789a",
        "security_group_ids": ["sg-0a1b2c3d4e5f6789a"],
    }


# ---------------------------------------------------------------------------
# Required fields — _JDBC_MANUAL_REQUIRED_FIELDS
# ---------------------------------------------------------------------------


class TestRequiredFields:
    """Validates: Requirements 9.5 (each field in _JDBC_MANUAL_REQUIRED_FIELDS)."""

    def test_minimal_valid_config_passes(self):
        """The Req 6.5 field set alone is sufficient — nothing else required."""
        assert _validate_jdbc_target_config_manual(_valid_manual_target_config()) is None

    def test_required_field_set_matches_p20_req_65(self):
        """The enforced set is exactly the six field names Req 6.5 enumerates.

        ``availability_zone`` is deliberately absent (Req 6.3 — Glue derives it
        from the subnet). Pinning the tuple means a silent addition or removal
        fails here rather than in a tester's deploy.
        """
        assert _JDBC_MANUAL_REQUIRED_FIELDS == (
            "engine",
            "jdbc_url",
            "jdbc_driver",
            "secret_arn",
            "subnet_id",
            "security_group_ids",
        )

    @pytest.mark.parametrize("field", _JDBC_MANUAL_REQUIRED_FIELDS)
    def test_missing_required_field_raises_naming_the_field(self, field: str):
        config = _valid_manual_target_config()
        del config[field]
        with pytest.raises(ValueError, match=rf"target_config\.{field} is required"):
            _validate_jdbc_target_config_manual(config)

    @pytest.mark.parametrize("field", _JDBC_MANUAL_REQUIRED_FIELDS)
    def test_empty_required_field_raises_naming_the_field(self, field: str):
        """An empty value is as unusable as an absent one — same 400."""
        config = _valid_manual_target_config()
        config[field] = ""
        with pytest.raises(ValueError, match=rf"target_config\.{field} is required"):
            _validate_jdbc_target_config_manual(config)

    def test_availability_zone_is_optional(self):
        """Req 6.3 — Glue derives the AZ from the subnet when it is omitted."""
        without_az = _valid_manual_target_config()
        assert "availability_zone" not in without_az
        assert _validate_jdbc_target_config_manual(without_az) is None

        with_az = _valid_manual_target_config()
        with_az["availability_zone"] = "us-west-2a"
        assert _validate_jdbc_target_config_manual(with_az) is None

    def test_jdbc_table_is_outside_this_validators_scope(self):
        """Req 6.5's field set excludes ``jdbc_table``.

        Table identity is enforced by the inline target-type branch in
        ``create_pipeline``, not here. A config with no ``jdbc_table`` must
        therefore pass this validator.
        """
        config = _valid_manual_target_config()
        assert "jdbc_table" not in config
        assert _validate_jdbc_target_config_manual(config) is None


# ---------------------------------------------------------------------------
# jdbc_url prefix
# ---------------------------------------------------------------------------


class TestJdbcUrlPrefix:
    """Validates: Requirements 9.5 (the ``jdbc:`` URL prefix rule)."""

    @pytest.mark.parametrize(
        "url",
        [
            "jdbc:postgresql://onprem.example.com:5432/analytics",
            "jdbc:mysql://onprem.example.com:3306/analytics",
            "jdbc:sqlserver://onprem.example.com:1433;databaseName=analytics",
            "jdbc:oracle:thin:@onprem.example.com:1521/ORCL",
        ],
    )
    def test_jdbc_prefixed_url_passes(self, url: str):
        config = _valid_manual_target_config()
        config["jdbc_url"] = url
        assert _validate_jdbc_target_config_manual(config) is None

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://onprem.example.com:5432/analytics",  # no scheme prefix
            "JDBC:postgresql://onprem.example.com:5432/analytics",  # wrong case
            " jdbc:postgresql://onprem.example.com:5432/analytics",  # leading space
            "onprem.example.com:5432",  # bare host:port
        ],
    )
    def test_url_without_jdbc_prefix_raises(self, url: str):
        config = _valid_manual_target_config()
        config["jdbc_url"] = url
        with pytest.raises(ValueError, match=r"target_config\.jdbc_url must start with 'jdbc:'"):
            _validate_jdbc_target_config_manual(config)


# ---------------------------------------------------------------------------
# SECRET_ARN_RE — fake, realistically shaped ARNs only (C8, LGR-819980f-1)
# ---------------------------------------------------------------------------


class TestSecretArnPattern:
    """Validates: Requirements 9.5 (SECRET_ARN_RE).

    Every value below is a fake ARN. The validator only ever sees a *reference*
    to a secret, which is the whole point of invariant ``LGR-819980f-1``.
    """

    @pytest.mark.parametrize(
        "arn",
        [
            # Secrets Manager appends a random 6-char suffix to the name.
            "arn:aws:secretsmanager:us-west-2:123456789012:secret:example-Ab12Cd",
            # Path-style names are legal and common for scoped secrets.
            "arn:aws:secretsmanager:us-east-1:210987654321:secret:example/jdbc/target-Zz99Yy",
            # The name charset the regex allows: word chars plus / + = . @ -
            "arn:aws:secretsmanager:eu-west-1:123456789012:secret:ex.am_ple+tag=1@host-A1b2C3",
        ],
    )
    def test_valid_secret_arn_passes(self, arn: str):
        config = _valid_manual_target_config()
        config["secret_arn"] = arn
        assert _validate_jdbc_target_config_manual(config) is None

    @pytest.mark.parametrize(
        "arn",
        [
            "not-an-arn",
            # Wrong service.
            "arn:aws:ssm:us-west-2:123456789012:secret:example-Ab12Cd",
            # Account id must be exactly 12 digits.
            "arn:aws:secretsmanager:us-west-2:12345678901:secret:example-Ab12Cd",
            "arn:aws:secretsmanager:us-west-2:1234567890123:secret:example-Ab12Cd",
            # Non-numeric account id.
            "arn:aws:secretsmanager:us-west-2:abcdefghijkl:secret:example-Ab12Cd",
            # Missing the :secret: segment.
            "arn:aws:secretsmanager:us-west-2:123456789012:example-Ab12Cd",
            # Empty secret name.
            "arn:aws:secretsmanager:us-west-2:123456789012:secret:",
            # Uppercase region.
            "arn:aws:secretsmanager:US-WEST-2:123456789012:secret:example-Ab12Cd",
            # Embedded space in the name.
            "arn:aws:secretsmanager:us-west-2:123456789012:secret:exa mple-Ab12Cd",
            # Trailing newline — fullmatch must reject, not silently accept.
            "arn:aws:secretsmanager:us-west-2:123456789012:secret:example-Ab12Cd\n",
        ],
    )
    def test_invalid_secret_arn_raises(self, arn: str):
        config = _valid_manual_target_config()
        config["secret_arn"] = arn
        with pytest.raises(ValueError, match=r"target_config\.secret_arn is not a valid Secrets Manager ARN"):
            _validate_jdbc_target_config_manual(config)


# ---------------------------------------------------------------------------
# SUBNET_ID_RE
# ---------------------------------------------------------------------------


class TestSubnetIdPattern:
    """Validates: Requirements 9.5 (SUBNET_ID_RE)."""

    @pytest.mark.parametrize(
        "subnet_id",
        [
            "subnet-1a2b3c4d",  # 8 hex — the legacy short form
            "subnet-0a1b2c3d4e5f6789a",  # 17 hex — the current form
        ],
    )
    def test_valid_subnet_id_passes(self, subnet_id: str):
        config = _valid_manual_target_config()
        config["subnet_id"] = subnet_id
        assert _validate_jdbc_target_config_manual(config) is None

    @pytest.mark.parametrize(
        "subnet_id",
        [
            "subnet-INVALID",
            "subnet-1A2B3C4D",  # uppercase hex
            "subnet-1a2b3c",  # too short (6)
            "subnet-0a1b2c3d4e5f6789ab",  # too long (18)
            "subnet-1a2b3c4g",  # 'g' is not hex
            "sg-1a2b3c4d",  # right shape, wrong resource
            "subnet-",  # prefix only
            "vpc-1a2b3c4d",
        ],
    )
    def test_invalid_subnet_id_raises(self, subnet_id: str):
        config = _valid_manual_target_config()
        config["subnet_id"] = subnet_id
        with pytest.raises(ValueError, match=r"target_config\.subnet_id is not a valid subnet ID"):
            _validate_jdbc_target_config_manual(config)


# ---------------------------------------------------------------------------
# SG_ID_RE — checked per group, so a single bad entry must be named
# ---------------------------------------------------------------------------


class TestSecurityGroupIdPattern:
    """Validates: Requirements 9.5 (SG_ID_RE, per group)."""

    @pytest.mark.parametrize(
        "sg_ids",
        [
            ["sg-1a2b3c4d"],  # 8 hex
            ["sg-0a1b2c3d4e5f6789a"],  # 17 hex
            ["sg-1a2b3c4d", "sg-0a1b2c3d4e5f6789a", "sg-9f8e7d6c"],  # several
        ],
    )
    def test_valid_security_group_ids_pass(self, sg_ids: list[str]):
        config = _valid_manual_target_config()
        config["security_group_ids"] = sg_ids
        assert _validate_jdbc_target_config_manual(config) is None

    @pytest.mark.parametrize(
        "bad_sg",
        [
            "not-a-sg-id",
            "sg-1A2B3C4D",  # uppercase hex
            "sg-1a2b3c",  # too short
            "sg-0a1b2c3d4e5f6789ab",  # too long
            "sg-1a2b3c4z",  # 'z' is not hex
            "subnet-1a2b3c4d",  # right shape, wrong resource
            "sg-",  # prefix only
        ],
    )
    def test_invalid_security_group_id_raises_naming_the_offender(self, bad_sg: str):
        """Every group is checked, and the message names the failing ID."""
        config = _valid_manual_target_config()
        config["security_group_ids"] = ["sg-1a2b3c4d", bad_sg, "sg-9f8e7d6c"]
        with pytest.raises(
            ValueError,
            match=re.escape(f"target_config.security_group_ids contains invalid security group ID: {bad_sg!r}"),
        ):
            _validate_jdbc_target_config_manual(config)


# ---------------------------------------------------------------------------
# _normalise_sg_list — list vs JSON-encoded string (design §2)
# ---------------------------------------------------------------------------


class TestSecurityGroupListNormalisation:
    """Validates: Requirements 9.5 (_normalise_sg_list list-vs-JSON-string).

    Both shapes are supported on purpose: the wizard JSON-encodes the list in
    manual mode because ``WizardState`` values are strings, while other paths
    send a real array. The validator normalises whichever it gets, in place,
    so downstream readers see one shape.
    """

    def test_real_list_is_accepted_and_preserved(self):
        config = _valid_manual_target_config()
        config["security_group_ids"] = ["sg-1a2b3c4d", "sg-9f8e7d6c"]
        _validate_jdbc_target_config_manual(config)
        assert config["security_group_ids"] == ["sg-1a2b3c4d", "sg-9f8e7d6c"]

    def test_json_encoded_string_is_accepted_and_normalised_in_place(self):
        config = _valid_manual_target_config()
        config["security_group_ids"] = json.dumps(["sg-1a2b3c4d", "sg-9f8e7d6c"])
        _validate_jdbc_target_config_manual(config)
        assert config["security_group_ids"] == ["sg-1a2b3c4d", "sg-9f8e7d6c"]

    def test_both_shapes_normalise_to_the_same_value(self):
        """The two encodings are interchangeable at the API boundary."""
        from_list = _valid_manual_target_config()
        from_list["security_group_ids"] = ["sg-1a2b3c4d"]
        from_json = _valid_manual_target_config()
        from_json["security_group_ids"] = json.dumps(["sg-1a2b3c4d"])

        _validate_jdbc_target_config_manual(from_list)
        _validate_jdbc_target_config_manual(from_json)

        assert from_list["security_group_ids"] == from_json["security_group_ids"] == ["sg-1a2b3c4d"]

    def test_malformed_json_string_raises_naming_the_field(self):
        config = _valid_manual_target_config()
        config["security_group_ids"] = "not-json[]"
        with pytest.raises(
            ValueError,
            match=r"target_config\.security_group_ids must be a JSON-encoded list of strings",
        ):
            _validate_jdbc_target_config_manual(config)

    @pytest.mark.parametrize("encoded", ['"sg-1a2b3c4d"', '{"sg": "sg-1a2b3c4d"}', "42", '["sg-1a2b3c4d", 7]'])
    def test_json_string_that_is_not_a_list_of_strings_raises(self, encoded: str):
        config = _valid_manual_target_config()
        config["security_group_ids"] = encoded
        with pytest.raises(
            ValueError,
            match=r"target_config\.security_group_ids JSON value must be a list of strings",
        ):
            _validate_jdbc_target_config_manual(config)

    def test_json_encoded_empty_list_raises_non_empty_error(self):
        """``"[]"`` is a truthy string, so it reaches the non-empty check."""
        config = _valid_manual_target_config()
        config["security_group_ids"] = json.dumps([])
        with pytest.raises(
            ValueError,
            match=r"target_config\.security_group_ids must be a non-empty list",
        ):
            _validate_jdbc_target_config_manual(config)

    def test_real_empty_list_is_rejected_as_a_missing_field(self):
        """A bare ``[]`` is falsy, so the required-field check catches it first.

        Different message from the JSON-encoded case above, same outcome: an
        empty security-group list never reaches Glue.
        """
        config = _valid_manual_target_config()
        config["security_group_ids"] = []
        with pytest.raises(ValueError, match=r"target_config\.security_group_ids is required"):
            _validate_jdbc_target_config_manual(config)

    def test_mixed_type_list_raises(self):
        config = _valid_manual_target_config()
        config["security_group_ids"] = ["sg-1a2b3c4d", 42]
        with pytest.raises(
            ValueError,
            match=r"target_config\.security_group_ids must be a list of strings; got mixed types",
        ):
            _validate_jdbc_target_config_manual(config)

    def test_neither_list_nor_string_raises_naming_the_type(self):
        config = _valid_manual_target_config()
        config["security_group_ids"] = 42
        with pytest.raises(
            ValueError,
            match=r"target_config\.security_group_ids must be a list of strings or JSON-encoded list; got int",
        ):
            _validate_jdbc_target_config_manual(config)


# ---------------------------------------------------------------------------
# engine membership
# ---------------------------------------------------------------------------


class TestEngineValidity:
    """Validates: Requirements 9.5 (engine ∈ SUPPORTED_ENGINES)."""

    @pytest.mark.parametrize("engine", SUPPORTED_ENGINES)
    def test_every_supported_engine_passes(self, engine: str):
        config = _valid_manual_target_config()
        config["engine"] = engine
        assert _validate_jdbc_target_config_manual(config) is None

    @pytest.mark.parametrize("engine", ["mongo", "POSTGRES", "postgresql", "sqlite", "aurora"])
    def test_unsupported_engine_raises_naming_the_field(self, engine: str):
        config = _valid_manual_target_config()
        config["engine"] = engine
        with pytest.raises(ValueError, match=r"target_config\.engine must be one of"):
            _validate_jdbc_target_config_manual(config)
