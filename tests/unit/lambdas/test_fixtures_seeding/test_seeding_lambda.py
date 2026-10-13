# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the test-fixtures seeding Lambda's pure helpers.

The module under test lives at ``tests/fixtures/cdk/seeding_lambda/index.py``
and is bundled into a Lambda asset by CDK at synth time (it is not installed
into the project venv as a package). The sibling ``conftest.py`` splices that
directory onto ``sys.path`` so this file can ``import index`` directly.

Validates: Requirements 4.7, 6.10, 6.11, 6.12, 6.13, 6.14
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# pg8000 is the Postgres driver shipped only inside the seeding Lambda's
# CDK asset bundle (via Docker bundling at synth time) — it is not part
# of the project venv. Skip this test module entirely when pg8000 is
# unavailable so the unit-test gate stays green for developers who have
# not installed the seeding Lambda's runtime dependencies. CI installs
# them via ``make test-fixtures-deps`` (or equivalent) before running
# this test file directly.
pytest.importorskip(
    "pg8000",
    reason=(
        "pg8000 is bundled with the test-fixtures seeding Lambda but is not "
        "in the project venv; install with `pip install pg8000 pymysql` to "
        "run these tests."
    ),
)

import index
import pymysql
import pymysql.err

# ---------------------------------------------------------------------------
# Local fixture: env vars the seeding Lambda reads at runtime.
# The shared ``mock_lambda_env`` does not set these RDS-fixture-specific
# variables, so we set them locally per-test.
# ---------------------------------------------------------------------------


@pytest.fixture
def seeding_env(monkeypatch, tmp_path: Path) -> dict[str, str]:
    """Set the four env vars the seeding Lambda reads.

    Returns the env-var mapping so individual tests can introspect values
    (e.g., the seed-SQL paths). The seed-SQL files are created empty by
    default — tests that need real content overwrite them.
    """
    mysql_sql = tmp_path / "mysql.sql"
    postgres_sql = tmp_path / "postgres.sql"
    mysql_sql.write_text("CREATE TABLE IF NOT EXISTS t (id INT);", encoding="utf-8")
    postgres_sql.write_text("SELECT 1;", encoding="utf-8")

    env = {
        "MYSQL_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:000000000000:secret:agentic-data-ingestion/test-fixtures/mysql-source-AbCdEf",
        "POSTGRES_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:000000000000:secret:agentic-data-ingestion/test-fixtures/postgres-target-XyZaBc",
        "MYSQL_SEED_SQL_PATH": str(mysql_sql),
        "POSTGRES_SEED_SQL_PATH": str(postgres_sql),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


# ---------------------------------------------------------------------------
# 1. _split_statements
# ---------------------------------------------------------------------------


def test_split_statements_drops_blank_and_whitespace():
    """Empty fragments and whitespace-only fragments must be dropped.

    Validates: Requirements 4.7, 6.10
    """
    result = index._split_statements(";\n  ;  CREATE TABLE foo;")
    assert result == ["CREATE TABLE foo"]


def test_split_statements_preserves_statement_content_and_order():
    """Multi-statement input round-trips with order preserved and surrounding whitespace stripped.

    Validates: Requirements 4.7, 6.10
    """
    sql = "  CREATE TABLE customers (id INT);\n  INSERT INTO customers VALUES (1);\n\n  SELECT * FROM customers;  "
    result = index._split_statements(sql)
    assert result == [
        "CREATE TABLE customers (id INT)",
        "INSERT INTO customers VALUES (1)",
        "SELECT * FROM customers",
    ]


# ---------------------------------------------------------------------------
# 2. handler — Delete short-circuit
# ---------------------------------------------------------------------------


def test_handler_delete_returns_immediately_without_connecting(seeding_env):
    """Delete events return success without touching SM or either DB.

    Validates: Requirement 6.11
    """
    boto_mock = MagicMock(side_effect=AssertionError("boto3.client must not be invoked on Delete"))
    pymysql_mock = MagicMock(side_effect=AssertionError("pymysql.connect must not be invoked on Delete"))
    pg8000_mock = MagicMock(side_effect=AssertionError("pg8000.dbapi.connect must not be invoked on Delete"))

    with (
        patch("index.boto3.client", boto_mock),
        patch("index.pymysql.connect", pymysql_mock),
        patch("index.pg8000.dbapi.connect", pg8000_mock),
    ):
        response = index.handler({"RequestType": "Delete"}, MagicMock())

    assert response == {"PhysicalResourceId": "agentic-data-ingestion-test-fixtures-seeding"}
    boto_mock.assert_not_called()
    pymysql_mock.assert_not_called()
    pg8000_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 3. handler — Create / Update dispatch to both seed paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("request_type", ["Create", "Update"])
def test_handler_create_calls_both_seed_paths(seeding_env, request_type):
    """Both Create and Update invocations must run _seed_mysql AND _seed_postgres exactly once.

    The test mocks the external boundary (Secrets Manager + DB connections via
    the ``_get_secret`` helper) and spies on the two ``_seed_*`` helpers so a
    regression that drops one path is caught.

    Validates: Requirement 6.10
    """
    fake_secret = {
        "host": "test.cluster.example",
        "port": 3306,
        "username": "agentic_data_ingestion_admin",
        "password": "redacted",
        "dbname": "appdb",
    }

    seed_mysql_spy = MagicMock()
    seed_postgres_spy = MagicMock()

    with (
        patch("index._get_secret", return_value=fake_secret) as get_secret_mock,
        patch("index._seed_mysql", seed_mysql_spy),
        patch("index._seed_postgres", seed_postgres_spy),
    ):
        response = index.handler({"RequestType": request_type}, MagicMock())

    assert response == {"PhysicalResourceId": "agentic-data-ingestion-test-fixtures-seeding"}
    # Secrets fetched for both DBs.
    assert get_secret_mock.call_count == 2
    get_secret_mock.assert_any_call(seeding_env["MYSQL_SECRET_ARN"])
    get_secret_mock.assert_any_call(seeding_env["POSTGRES_SECRET_ARN"])
    # Both seed paths invoked with the env-var-resolved SQL paths.
    seed_mysql_spy.assert_called_once_with(fake_secret, seeding_env["MYSQL_SEED_SQL_PATH"])
    seed_postgres_spy.assert_called_once_with(fake_secret, seeding_env["POSTGRES_SEED_SQL_PATH"])


# ---------------------------------------------------------------------------
# 4. _retry_connect — backoff sequence on transient errors
# ---------------------------------------------------------------------------


def test_retry_connect_succeeds_after_two_transient_failures():
    """Two transient OperationalErrors → sleep 1s, 2s, then a successful third call.

    Validates: Requirement 6.12
    """
    sentinel_conn = object()
    connect_fn = MagicMock(
        side_effect=[
            pymysql.err.OperationalError("transient 1"),
            pymysql.err.OperationalError("transient 2"),
            sentinel_conn,
        ]
    )

    with patch("index.time.sleep") as sleep_mock:
        result = index._retry_connect(connect_fn, label="MySQL")

    assert result is sentinel_conn
    assert connect_fn.call_count == 3
    sleep_args = [call.args[0] for call in sleep_mock.call_args_list]
    assert sleep_args == [1, 2]


def test_retry_connect_gives_up_after_five_attempts_with_correct_backoff_sequence():
    """All five attempts fail → backoff is 1s, 2s, 4s, 8s, 16s and a RuntimeError is raised.

    Validates: Requirement 6.12
    """
    connect_fn = MagicMock(
        side_effect=[pymysql.err.OperationalError(f"transient #{i}") for i in range(index.MAX_RETRIES)]
    )

    with patch("index.time.sleep") as sleep_mock, pytest.raises(RuntimeError) as exc_info:
        index._retry_connect(connect_fn, label="MySQL")

    assert connect_fn.call_count == index.MAX_RETRIES
    sleep_args = [call.args[0] for call in sleep_mock.call_args_list]
    assert sleep_args == [1, 2, 4, 8, 16]
    assert "MySQL connect failed after 5 retries" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. _seed_mysql — error truncation
# ---------------------------------------------------------------------------


def test_seed_statement_failure_truncates_error_message_to_500_chars(tmp_path: Path):
    """A failing statement longer than 500 chars surfaces as a RuntimeError whose
    message contains exactly the first 500 characters of the offending statement.

    Validates: Requirement 6.13
    """
    long_payload = "A" * 750
    long_stmt = f"INSERT INTO t VALUES ('{long_payload}')"
    assert len(long_stmt) > index.TRUNCATE_LEN

    sql_path = tmp_path / "long.sql"
    sql_path.write_text(long_stmt + ";", encoding="utf-8")

    cursor_mock = MagicMock()
    cursor_mock.execute.side_effect = pymysql.err.OperationalError("simulated MySQL crash")
    cursor_ctx = MagicMock()
    cursor_ctx.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_ctx.__exit__ = MagicMock(return_value=False)

    conn_mock = MagicMock()
    conn_mock.cursor.return_value = cursor_ctx

    with patch("index._connect_mysql", return_value=conn_mock), pytest.raises(RuntimeError) as exc_info:
        index._seed_mysql({}, str(sql_path))

    expected_truncated = long_stmt[: index.TRUNCATE_LEN]
    # The error message embeds the truncated statement via repr (...!r), so the
    # truncated content appears verbatim inside the quoted form.
    assert expected_truncated in str(exc_info.value)
    # The chars that were truncated off must NOT appear in the error message.
    excess_tail = long_stmt[index.TRUNCATE_LEN :]
    assert excess_tail not in str(exc_info.value)
    conn_mock.close.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Idempotency — running _seed_mysql twice produces the same cursor calls
# ---------------------------------------------------------------------------


def test_handler_idempotency_against_already_seeded_db(tmp_path: Path):
    """Re-running _seed_mysql against an in-process recording cursor produces
    the same set of cur.execute calls without exceptions.

    The seed SQL files use ``CREATE TABLE IF NOT EXISTS`` and ``INSERT … ON
    DUPLICATE KEY UPDATE id = id`` so a second run is, by design, a no-op
    against the database. This test exercises the Lambda's contribution to
    that idempotency: every statement in the file is re-issued exactly once
    on each run, the connection is opened and closed cleanly, and no
    exception escapes.

    Validates: Requirement 6.14
    """
    sql_text = (
        "CREATE TABLE IF NOT EXISTS customers (id INT PRIMARY KEY);\n"
        "INSERT INTO customers (id) VALUES (1) ON DUPLICATE KEY UPDATE id = id;\n"
        "INSERT INTO customers (id) VALUES (2) ON DUPLICATE KEY UPDATE id = id;\n"
    )
    sql_path = tmp_path / "mysql.sql"
    sql_path.write_text(sql_text, encoding="utf-8")

    expected_statements = index._split_statements(sql_text)

    def make_recording_connection() -> tuple[MagicMock, list[str]]:
        recorded: list[str] = []
        cursor_mock = MagicMock()
        cursor_mock.execute.side_effect = lambda stmt: recorded.append(stmt)
        cursor_ctx = MagicMock()
        cursor_ctx.__enter__ = MagicMock(return_value=cursor_mock)
        cursor_ctx.__exit__ = MagicMock(return_value=False)
        conn_mock = MagicMock()
        conn_mock.cursor.return_value = cursor_ctx
        return conn_mock, recorded

    first_conn, first_recorded = make_recording_connection()
    second_conn, second_recorded = make_recording_connection()

    with patch("index._connect_mysql", side_effect=[first_conn, second_conn]):
        # First run — seeds a fresh DB.
        index._seed_mysql({}, str(sql_path))
        # Second run — same SQL against an "already seeded" DB. Still no error.
        index._seed_mysql({}, str(sql_path))

    assert first_recorded == expected_statements
    assert second_recorded == expected_statements
    # Both runs commit and close.
    first_conn.commit.assert_called_once()
    first_conn.close.assert_called_once()
    second_conn.commit.assert_called_once()
    second_conn.close.assert_called_once()
