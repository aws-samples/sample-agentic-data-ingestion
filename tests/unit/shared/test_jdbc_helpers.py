# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``shared/jdbc_helpers.py``.

Covers every public symbol of the JDBC helpers module:

* :data:`SUPPORTED_ENGINES` / :data:`JDBC_DRIVER_MAP` consistency.
* :func:`get_jdbc_driver`, :func:`get_default_port`, :func:`build_jdbc_url`
  for every engine (positive) and one negative (unknown engine).
* :func:`validate_identifier` and :func:`validate_watermark_value`
  positive / negative cases.
* :func:`quote_identifier` for every engine — backticks for the MySQL
  family, double quotes for Postgres / Oracle / SQL Server.
* :func:`build_watermark_predicate` golden output for all
  ``11 engines × {numeric, timestamp}`` = 22 cases.
* :func:`create_glue_jdbc_connection` create vs. update branches,
  ``PhysicalConnectionRequirements`` derivation, ``Tags`` forwarding,
  and ``SECRET_ID`` inclusion semantics.

Requirements: 22.1
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

# Make shared modules importable without relying on the Lambda layer mount.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)

import jdbc_helpers
from jdbc_helpers import (
    JDBC_DRIVER_MAP,
    SUPPORTED_ENGINES,
    build_jdbc_url,
    build_watermark_predicate,
    create_glue_jdbc_connection,
    get_default_port,
    get_jdbc_driver,
    is_managed_glue_connection,
    quote_identifier,
    validate_identifier,
    validate_watermark_value,
)

# ---------------------------------------------------------------------------
# A. Engine list / driver map consistency
# ---------------------------------------------------------------------------


class TestSupportedEngines:
    """``SUPPORTED_ENGINES`` must stay in lock-step with ``JDBC_DRIVER_MAP``."""

    def test_supported_engines_match_driver_map_keys(self):
        assert set(SUPPORTED_ENGINES) == set(JDBC_DRIVER_MAP.keys())

    def test_supported_engines_count(self):
        # Sanity check: exactly 11 engines per the spec.
        assert len(SUPPORTED_ENGINES) == 11


# ---------------------------------------------------------------------------
# B. get_jdbc_driver
# ---------------------------------------------------------------------------


_DRIVER_EXPECTATIONS: list[tuple[str, str]] = [
    ("postgres", "org.postgresql.Driver"),
    ("aurora-postgresql", "org.postgresql.Driver"),
    ("mysql", "com.mysql.cj.jdbc.Driver"),
    ("aurora-mysql", "com.mysql.cj.jdbc.Driver"),
    ("mariadb", "org.mariadb.jdbc.Driver"),
    ("oracle-ee", "oracle.jdbc.OracleDriver"),
    ("oracle-se2", "oracle.jdbc.OracleDriver"),
    ("sqlserver-ee", "com.microsoft.sqlserver.jdbc.SQLServerDriver"),
    ("sqlserver-se", "com.microsoft.sqlserver.jdbc.SQLServerDriver"),
    ("sqlserver-ex", "com.microsoft.sqlserver.jdbc.SQLServerDriver"),
    ("sqlserver-web", "com.microsoft.sqlserver.jdbc.SQLServerDriver"),
]


class TestGetJdbcDriver:
    @pytest.mark.parametrize("engine,expected", _DRIVER_EXPECTATIONS)
    def test_returns_expected_driver(self, engine: str, expected: str):
        assert get_jdbc_driver(engine) == expected

    def test_unknown_engine_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported JDBC engine"):
            get_jdbc_driver("not-a-real-engine")


# ---------------------------------------------------------------------------
# C. get_default_port
# ---------------------------------------------------------------------------


_PORT_EXPECTATIONS: list[tuple[str, int]] = [
    ("postgres", 5432),
    ("aurora-postgresql", 5432),
    ("mysql", 3306),
    ("aurora-mysql", 3306),
    ("mariadb", 3306),
    ("oracle-ee", 1521),
    ("oracle-se2", 1521),
    ("sqlserver-ee", 1433),
    ("sqlserver-se", 1433),
    ("sqlserver-ex", 1433),
    ("sqlserver-web", 1433),
]


class TestGetDefaultPort:
    @pytest.mark.parametrize("engine,expected", _PORT_EXPECTATIONS)
    def test_returns_expected_port(self, engine: str, expected: int):
        assert get_default_port(engine) == expected

    def test_unknown_engine_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported JDBC engine"):
            get_default_port("not-a-real-engine")


# ---------------------------------------------------------------------------
# D. build_jdbc_url
# ---------------------------------------------------------------------------
#
# Each row is (engine, expected_full_url) — built from a fixed
# (host, port, database) tuple so the assertions are byte-exact.

_HOST = "db.example.com"
_DB = "mydb"
_URL_EXPECTATIONS: list[tuple[str, int, str]] = [
    (
        "postgres",
        5432,
        "jdbc:postgresql://db.example.com:5432/mydb?ssl=true&sslmode=require",
    ),
    (
        "aurora-postgresql",
        5432,
        "jdbc:postgresql://db.example.com:5432/mydb?ssl=true&sslmode=require",
    ),
    (
        "mysql",
        3306,
        "jdbc:mysql://db.example.com:3306/mydb?useSSL=true&requireSSL=true",
    ),
    (
        "aurora-mysql",
        3306,
        "jdbc:mysql://db.example.com:3306/mydb?useSSL=true&requireSSL=true",
    ),
    (
        "mariadb",
        3306,
        "jdbc:mariadb://db.example.com:3306/mydb?useSSL=true&requireSSL=true",
    ),
    ("oracle-ee", 1521, "jdbc:oracle:thin:@db.example.com:1521/mydb"),
    ("oracle-se2", 1521, "jdbc:oracle:thin:@db.example.com:1521/mydb"),
    (
        "sqlserver-ee",
        1433,
        "jdbc:sqlserver://db.example.com:1433;databaseName=mydb;encrypt=true",
    ),
    (
        "sqlserver-se",
        1433,
        "jdbc:sqlserver://db.example.com:1433;databaseName=mydb;encrypt=true",
    ),
    (
        "sqlserver-ex",
        1433,
        "jdbc:sqlserver://db.example.com:1433;databaseName=mydb;encrypt=true",
    ),
    (
        "sqlserver-web",
        1433,
        "jdbc:sqlserver://db.example.com:1433;databaseName=mydb;encrypt=true",
    ),
]


class TestBuildJdbcUrl:
    @pytest.mark.parametrize("engine,port,expected", _URL_EXPECTATIONS)
    def test_returns_expected_url(self, engine: str, port: int, expected: str):
        assert build_jdbc_url(engine, _HOST, port, _DB) == expected

    def test_sql_server_uses_semicolon_form(self):
        url = build_jdbc_url("sqlserver-ee", _HOST, 1433, _DB)
        assert ";databaseName=mydb;" in url
        assert "encrypt=true" in url
        # No `/database` path component for SQL Server.
        assert "/mydb" not in url

    def test_oracle_uses_slash_separator(self):
        url = build_jdbc_url("oracle-ee", _HOST, 1521, _DB)
        # Oracle uses `host:port/service` with a forward slash, not the
        # MySQL/Postgres ``/databaseName`` query-string form.
        assert url.endswith("/mydb")
        assert "?" not in url
        assert ";" not in url

    def test_unknown_engine_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported JDBC engine"):
            build_jdbc_url("not-a-real-engine", _HOST, 5432, _DB)


# ---------------------------------------------------------------------------
# E. validate_identifier
# ---------------------------------------------------------------------------


class TestValidateIdentifier:
    @pytest.mark.parametrize(
        "name",
        ["abc", "_abc", "abc123", "Abc_123", "A", "_", "snake_case_99"],
    )
    def test_accepts_valid_identifier(self, name: str):
        # Should return None and not raise.
        assert validate_identifier(name) is None

    @pytest.mark.parametrize(
        "name",
        [
            "",  # empty
            "123abc",  # leading digit
            "abc-def",  # hyphen
            "abc def",  # space
            "abc;DROP TABLE",  # SQL injection attempt
            'abc"def',  # embedded quote
            "abc.def",  # dot
            "abc'def",  # single quote
        ],
    )
    def test_rejects_invalid_string(self, name: str):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_identifier(name)

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_identifier(None)  # type: ignore[arg-type]

    def test_rejects_non_string_types(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_identifier(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# F. validate_watermark_value
# ---------------------------------------------------------------------------


class TestValidateWatermarkValueNumeric:
    @pytest.mark.parametrize("value", ["42", "-1", "0", "0.5", "-3.14", "1000000"])
    def test_accepts_valid_numeric(self, value: str):
        assert validate_watermark_value(value, "numeric") is None

    @pytest.mark.parametrize(
        "value",
        [
            "",  # empty
            "abc",  # non-numeric
            "1e5",  # scientific notation excluded
            "1.2.3",  # double decimal
            "+1",  # leading plus excluded
            "1.",  # trailing decimal
            ".5",  # leading decimal
            " 42 ",  # whitespace
        ],
    )
    def test_rejects_invalid_numeric(self, value: str):
        with pytest.raises(ValueError, match="Invalid numeric watermark value"):
            validate_watermark_value(value, "numeric")


class TestValidateWatermarkValueTimestamp:
    @pytest.mark.parametrize(
        "value",
        [
            "2024-12-15",
            "2024-12-15T08:42:31",
            "2024-12-15T08:42:31Z",
            "2024-12-15T08:42:31.123456",
            "2024-12-15 08:42:31",
            "2024-12-15T08:42:31+02:00",
            "2024-12-15T08:42:31+0200",
            "2024-12-15T08:42:31-05:00",
            "2024-12-15T08:42:31.5Z",
        ],
    )
    def test_accepts_valid_timestamp(self, value: str):
        assert validate_watermark_value(value, "timestamp") is None

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "2024",  # incomplete
            "15-12-2024",  # wrong format
            "not-a-date",
            "2024/12/15",  # slash separator
            "2024-12-15T08:42",  # missing seconds
        ],
    )
    def test_rejects_invalid_timestamp(self, value: str):
        with pytest.raises(ValueError, match="Invalid timestamp watermark value"):
            validate_watermark_value(value, "timestamp")


class TestValidateWatermarkValueUnknownType:
    def test_rejects_unknown_watermark_type(self):
        with pytest.raises(ValueError, match="Unknown watermark_type"):
            validate_watermark_value("42", "decimal")

    def test_rejects_empty_watermark_type(self):
        with pytest.raises(ValueError, match="Unknown watermark_type"):
            validate_watermark_value("42", "")


# ---------------------------------------------------------------------------
# G. quote_identifier
# ---------------------------------------------------------------------------


_BACKTICK_ENGINES = ["mysql", "aurora-mysql", "mariadb"]
_DOUBLE_QUOTE_ENGINES = [
    "postgres",
    "aurora-postgresql",
    "oracle-ee",
    "oracle-se2",
    "sqlserver-ee",
    "sqlserver-se",
    "sqlserver-ex",
    "sqlserver-web",
]


class TestQuoteIdentifier:
    @pytest.mark.parametrize("engine", _BACKTICK_ENGINES)
    def test_mysql_family_uses_backticks(self, engine: str):
        assert quote_identifier(engine, "updated_at") == "`updated_at`"

    @pytest.mark.parametrize("engine", _DOUBLE_QUOTE_ENGINES)
    def test_other_engines_use_double_quotes(self, engine: str):
        assert quote_identifier(engine, "updated_at") == '"updated_at"'

    def test_validates_identifier_first(self):
        # quote_identifier MUST call validate_identifier first as
        # defence-in-depth — passing an invalid name raises before any
        # quoting happens.
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            quote_identifier("postgres", "abc; DROP TABLE")

    def test_rejects_empty_name(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            quote_identifier("postgres", "")


# ---------------------------------------------------------------------------
# H. build_watermark_predicate — 22 golden-output cases
# ---------------------------------------------------------------------------
#
# Layout: (engine, watermark_type, validated_value, expected_predicate)
# Column is fixed at "updated_at" for all 22 cases.

_NUMERIC_VALUE = "42"
_TS_VALUE = "2024-12-15T08:42:31Z"

_WATERMARK_PREDICATE_GOLDEN: list[tuple[str, str, str, str]] = [
    # ---- numeric (11 cases) — uniform: <quoted_col> > <raw_value>
    ("postgres", "numeric", _NUMERIC_VALUE, '"updated_at" > 42'),
    ("aurora-postgresql", "numeric", _NUMERIC_VALUE, '"updated_at" > 42'),
    ("mysql", "numeric", _NUMERIC_VALUE, "`updated_at` > 42"),
    ("aurora-mysql", "numeric", _NUMERIC_VALUE, "`updated_at` > 42"),
    ("mariadb", "numeric", _NUMERIC_VALUE, "`updated_at` > 42"),
    ("oracle-ee", "numeric", _NUMERIC_VALUE, '"updated_at" > 42'),
    ("oracle-se2", "numeric", _NUMERIC_VALUE, '"updated_at" > 42'),
    ("sqlserver-ee", "numeric", _NUMERIC_VALUE, '"updated_at" > 42'),
    ("sqlserver-se", "numeric", _NUMERIC_VALUE, '"updated_at" > 42'),
    ("sqlserver-ex", "numeric", _NUMERIC_VALUE, '"updated_at" > 42'),
    ("sqlserver-web", "numeric", _NUMERIC_VALUE, '"updated_at" > 42'),
    # ---- timestamp (11 cases) — engine-specific cast/format
    (
        "postgres",
        "timestamp",
        _TS_VALUE,
        "\"updated_at\" > '2024-12-15T08:42:31Z'::timestamp",
    ),
    (
        "aurora-postgresql",
        "timestamp",
        _TS_VALUE,
        "\"updated_at\" > '2024-12-15T08:42:31Z'::timestamp",
    ),
    (
        "mysql",
        "timestamp",
        _TS_VALUE,
        "`updated_at` > '2024-12-15T08:42:31Z'",
    ),
    (
        "aurora-mysql",
        "timestamp",
        _TS_VALUE,
        "`updated_at` > '2024-12-15T08:42:31Z'",
    ),
    (
        "mariadb",
        "timestamp",
        _TS_VALUE,
        "`updated_at` > '2024-12-15T08:42:31Z'",
    ),
    (
        "oracle-ee",
        "timestamp",
        _TS_VALUE,
        '"updated_at" > TO_TIMESTAMP(\'2024-12-15T08:42:31Z\', \'YYYY-MM-DD"T"HH24:MI:SS.FF"Z"\')',
    ),
    (
        "oracle-se2",
        "timestamp",
        _TS_VALUE,
        '"updated_at" > TO_TIMESTAMP(\'2024-12-15T08:42:31Z\', \'YYYY-MM-DD"T"HH24:MI:SS.FF"Z"\')',
    ),
    (
        "sqlserver-ee",
        "timestamp",
        _TS_VALUE,
        "\"updated_at\" > CONVERT(datetime2, '2024-12-15T08:42:31Z', 127)",
    ),
    (
        "sqlserver-se",
        "timestamp",
        _TS_VALUE,
        "\"updated_at\" > CONVERT(datetime2, '2024-12-15T08:42:31Z', 127)",
    ),
    (
        "sqlserver-ex",
        "timestamp",
        _TS_VALUE,
        "\"updated_at\" > CONVERT(datetime2, '2024-12-15T08:42:31Z', 127)",
    ),
    (
        "sqlserver-web",
        "timestamp",
        _TS_VALUE,
        "\"updated_at\" > CONVERT(datetime2, '2024-12-15T08:42:31Z', 127)",
    ),
]


class TestBuildWatermarkPredicate:
    """Golden-output coverage: 11 engines × 2 watermark types = 22 cases."""

    def test_golden_matrix_has_22_rows(self):
        # Sanity: 11 engines × {numeric, timestamp} = 22.
        assert len(_WATERMARK_PREDICATE_GOLDEN) == 22

    @pytest.mark.parametrize(
        "engine,watermark_type,validated_value,expected",
        _WATERMARK_PREDICATE_GOLDEN,
    )
    def test_predicate_matches_golden_output(
        self,
        engine: str,
        watermark_type: str,
        validated_value: str,
        expected: str,
    ):
        result = build_watermark_predicate(engine, "updated_at", watermark_type, validated_value)
        assert result == expected

    def test_validates_column_name(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            build_watermark_predicate("postgres", "bad name", "numeric", "42")

    def test_validates_watermark_value(self):
        with pytest.raises(ValueError, match="Invalid numeric watermark value"):
            build_watermark_predicate("postgres", "updated_at", "numeric", "abc")

    def test_unknown_watermark_type_raises(self):
        with pytest.raises(ValueError, match="Unknown watermark_type"):
            build_watermark_predicate("postgres", "updated_at", "decimal", "42")


# ---------------------------------------------------------------------------
# I. create_glue_jdbc_connection
# ---------------------------------------------------------------------------


def _make_glue_client_mock() -> MagicMock:
    """Build a fake Glue client with a stub ``EntityNotFoundException``."""
    client = MagicMock()
    # The function imports ``ClientError`` from ``botocore.exceptions`` and
    # branches on ``error_code == "EntityNotFoundException"`` — so for unit
    # tests we raise a real ``ClientError`` with that code from get_connection.
    return client


def _entity_not_found_error():
    """Build a ClientError that mimics the EntityNotFoundException shape."""
    from botocore.exceptions import ClientError

    return ClientError(
        error_response={
            "Error": {
                "Code": "EntityNotFoundException",
                "Message": "Connection not found",
            }
        },
        operation_name="GetConnection",
    )


@pytest.fixture
def stub_glue_client(monkeypatch):
    """Inject a MagicMock Glue client into ``jdbc_helpers._glue_client``."""
    client = _make_glue_client_mock()
    monkeypatch.setattr(jdbc_helpers, "_glue_client", lambda: client)
    # Also clear any cached client from a prior test so this fixture wins.
    monkeypatch.setattr(jdbc_helpers, "_glue_client_cache", None, raising=False)
    # The update branch tags the connection, and building its ARN asks STS for
    # the account id. Without this seam the test passed only when an earlier
    # test had filled `_account_id_cache` or real credentials were present; run
    # on its own under `make test-scope` (no credentials) it raised
    # NoCredentialsError.
    monkeypatch.setattr(jdbc_helpers, "_account_id", lambda: "123456789012")
    return client


class TestCreateGlueJdbcConnectionCreateBranch:
    """Verify create branch when get_connection raises EntityNotFoundException."""

    def test_create_called_when_connection_missing(self, stub_glue_client):
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()

        result = create_glue_jdbc_connection(
            connection_name="agentic-data-ingestion-pipe-1-source",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements=None,
        )

        assert result == "agentic-data-ingestion-pipe-1-source"
        stub_glue_client.create_connection.assert_called_once()
        stub_glue_client.update_connection.assert_not_called()

        kwargs = stub_glue_client.create_connection.call_args.kwargs
        ci = kwargs["ConnectionInput"]
        assert ci["Name"] == "agentic-data-ingestion-pipe-1-source"
        assert ci["ConnectionType"] == "JDBC"
        assert ci["ConnectionProperties"]["JDBC_CONNECTION_URL"] == "jdbc:postgresql://h:5432/db"
        # JDBC_DRIVER_CLASS_NAME is intentionally NOT set on the
        # connection — Glue auto-detects the driver from the URL
        # protocol prefix for built-in engines, and setting the class
        # explicitly causes Glue's CreateCrawler to demand
        # JDBC_DRIVER_JAR_URI alongside (which we don't provide).
        assert "JDBC_DRIVER_CLASS_NAME" not in ci["ConnectionProperties"]
        assert ci["ConnectionProperties"]["JDBC_ENFORCE_SSL"] == "true"

    def test_tags_omitted_when_none(self, stub_glue_client):
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()

        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements=None,
            tags=None,
        )

        kwargs = stub_glue_client.create_connection.call_args.kwargs
        assert "Tags" not in kwargs


class TestCreateGlueJdbcConnectionUpdateBranch:
    def test_update_called_when_connection_exists(self, stub_glue_client):
        stub_glue_client.get_connection.return_value = {"Connection": {"Name": "conn-1"}}

        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements=None,
        )

        stub_glue_client.update_connection.assert_called_once()
        stub_glue_client.create_connection.assert_not_called()

        kwargs = stub_glue_client.update_connection.call_args.kwargs
        assert kwargs["Name"] == "conn-1"
        assert kwargs["ConnectionInput"]["ConnectionType"] == "JDBC"

    def test_update_branch_does_not_pass_tags(self, stub_glue_client):
        # update_connection has no Tags parameter — ensure callers can supply
        # tags without breaking the update branch.
        stub_glue_client.get_connection.return_value = {"Connection": {"Name": "conn-1"}}

        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements=None,
            tags={"Type": "agentic-data-ingestion-discovery"},
        )

        kwargs = stub_glue_client.update_connection.call_args.kwargs
        assert "Tags" not in kwargs


class TestCreateGlueJdbcConnectionPhysicalRequirements:
    def test_physical_requirements_forwarded_when_complete(self, stub_glue_client):
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()

        physical = {
            "SubnetId": "subnet-123",
            "SecurityGroupIdList": ["sg-1"],
            "AvailabilityZone": "us-west-2a",
        }
        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements=physical,
        )

        ci = stub_glue_client.create_connection.call_args.kwargs["ConnectionInput"]
        assert ci["PhysicalConnectionRequirements"] == physical

    def test_physical_requirements_omitted_when_none(self, stub_glue_client):
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()

        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements=None,
        )

        ci = stub_glue_client.create_connection.call_args.kwargs["ConnectionInput"]
        assert "PhysicalConnectionRequirements" not in ci

    def test_physical_requirements_omitted_when_partial(self, stub_glue_client):
        # Missing SecurityGroupIdList → defensive omission.
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()

        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements={"SubnetId": "subnet-123"},
        )

        ci = stub_glue_client.create_connection.call_args.kwargs["ConnectionInput"]
        assert "PhysicalConnectionRequirements" not in ci

    def test_physical_requirements_omitted_when_missing_subnet(self, stub_glue_client):
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()

        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements={"SecurityGroupIdList": ["sg-1"]},
        )

        ci = stub_glue_client.create_connection.call_args.kwargs["ConnectionInput"]
        assert "PhysicalConnectionRequirements" not in ci


class TestCreateGlueJdbcConnectionTags:
    def test_tags_forwarded_on_create(self, stub_glue_client):
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()

        tags = {"Type": "agentic-data-ingestion-discovery", "Hash": "abc123"}
        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements=None,
            tags=tags,
        )

        kwargs = stub_glue_client.create_connection.call_args.kwargs
        assert kwargs["Tags"] == tags


class TestCreateGlueJdbcConnectionSecretArn:
    def test_secret_id_omitted_when_secret_arn_none(self, stub_glue_client):
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()

        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements=None,
        )

        ci = stub_glue_client.create_connection.call_args.kwargs["ConnectionInput"]
        assert "SECRET_ID" not in ci["ConnectionProperties"]

    def test_secret_id_set_in_properties_and_tag_when_provided(self, stub_glue_client):
        # AWS Glue REQUIRES credentials on a JDBC connection. SECRET_ID is
        # the supported property for a Secrets Manager reference and MUST be
        # present in ConnectionProperties — a URL-only connection fails Glue
        # with "Validation for connection properties failed" (verified
        # against the live Glue API 2026-06-22). The ARN is ALSO mirrored
        # into the adi:secret_arn tag for the read path.
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()

        secret_arn = "arn:aws:secretsmanager:us-west-2:123:secret:db-creds-XYZ"
        create_glue_jdbc_connection(
            connection_name="conn-1",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=secret_arn,
            physical_requirements=None,
        )

        kwargs = stub_glue_client.create_connection.call_args.kwargs
        ci = kwargs["ConnectionInput"]
        assert ci["ConnectionProperties"]["SECRET_ID"] == secret_arn
        assert kwargs["Tags"]["adi:secret_arn"] == secret_arn


class TestCreateGlueJdbcConnectionErrorPropagation:
    def test_unexpected_get_connection_error_propagates(self, stub_glue_client):
        from botocore.exceptions import ClientError

        # Any error code other than EntityNotFoundException must propagate.
        stub_glue_client.get_connection.side_effect = ClientError(
            error_response={"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
            operation_name="GetConnection",
        )

        with pytest.raises(ClientError):
            create_glue_jdbc_connection(
                connection_name="conn-1",
                jdbc_url="jdbc:postgresql://h:5432/db",
                jdbc_driver="org.postgresql.Driver",
                secret_arn=None,
                physical_requirements=None,
            )

        stub_glue_client.create_connection.assert_not_called()
        stub_glue_client.update_connection.assert_not_called()


# ---------------------------------------------------------------------------
# is_managed_glue_connection — which connections the "Use Glue Connection"
# picker leaves out. The expectations come from the names the provisioner's
# callers mint (pipeline id = `{slug}-{8 hex}`, discovery hash = 12 hex), not
# from the function's output.
# ---------------------------------------------------------------------------


class TestIsManagedGlueConnection:
    @pytest.mark.parametrize(
        "name",
        [
            "agentic-data-ingestion-mylatesttest-773284d9-source",
            "agentic-data-ingestion-mylatesttestingsept17-a0ad3b9c-source",
            "agentic-data-ingestion-mynew233-503c3151-rds",
            "agentic-data-ingestion-discover-c84a5680b394",
        ],
    )
    def test_names_this_system_mints_are_managed_without_a_description(self, name: str):
        assert is_managed_glue_connection(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            # A test-fixtures connection is one a user is meant to pick. Its name
            # ends in `-source` but carries no 8-hex pipeline-id segment.
            "agentic-data-ingestion-test-fixtures-mysql-source",
            "agentic-data-ingestion-test-fixtures-postgres-target",
            "my-warehouse-conn",
            "prod-orders-source",
            # Wrong hash length, and an uppercase hash the hasher never emits.
            "agentic-data-ingestion-discover-c84a5680",
            "agentic-data-ingestion-discover-C84A5680B394",
            # The runtime crawler's name is not a connection name.
            "agentic-data-ingestion-mylatesttest-773284d9-source-discover",
            "",
        ],
    )
    def test_other_names_are_not_managed(self, name: str):
        assert is_managed_glue_connection(name, "") is False

    def test_the_provisioner_description_marks_any_name_as_managed(self):
        assert is_managed_glue_connection("renamed-by-hand", "AgenticDataIngestion JDBC connection (x)") is True

    def test_a_user_description_does_not_mark_a_connection_as_managed(self):
        assert is_managed_glue_connection("my-warehouse-conn", "Warehouse, owned by the data team") is False

    def test_what_the_provisioner_writes_is_recognised_as_managed(self, stub_glue_client):
        # The round trip that keeps writer and reader in step: whatever
        # Description `create_glue_jdbc_connection` writes, under a name the
        # name pattern does not match, must read back as managed.
        stub_glue_client.get_connection.side_effect = _entity_not_found_error()
        create_glue_jdbc_connection(
            connection_name="unpatterned-name",
            jdbc_url="jdbc:postgresql://h:5432/db",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=None,
            physical_requirements=None,
        )
        written = stub_glue_client.create_connection.call_args.kwargs["ConnectionInput"]
        assert is_managed_glue_connection("unpatterned-name") is False
        assert is_managed_glue_connection(written["Name"], written["Description"]) is True
