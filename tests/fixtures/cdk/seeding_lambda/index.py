# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CloudFormation Custom Resource Lambda that seeds the JDBC test fixture databases.

Invoked by the ``cr.Provider`` framework on stack Create/Update/Delete events.

- ``Create`` / ``Update``: connect to MySQL_Source and Postgres_Target with retry
  backoff, then run the seed SQL files baked into the Lambda asset under
  ``/var/task/seed_sql/``.
- ``Delete``: short-circuit and return immediately. The data is destroyed
  with the RDS instances themselves; there is no cleanup work to do.

All errors are re-raised as ``RuntimeError`` with the original message
truncated to ``TRUNCATE_LEN`` characters so the CloudFormation reason field
stays diagnosable but bounded.

See ``.kiro/specs/p18-jdbc-test-fixtures/design.md`` (Component 3) for the
reference pseudocode this module implements.
"""

from __future__ import annotations

import json
import os
import time

import boto3
import pg8000.dbapi
import pg8000.exceptions
import pymysql
import pymysql.err

MAX_RETRIES = 5
INITIAL_BACKOFF_SEC = 1
TRUNCATE_LEN = 500

# Errors classified as "transient" → trigger retry-with-backoff.
TRANSIENT_ERRORS = (
    TimeoutError,
    ConnectionRefusedError,
    pymysql.err.OperationalError,
    pg8000.exceptions.InterfaceError,
)


def handler(event: dict, context) -> dict:
    """Seeding Lambda entrypoint.

    Returns ``{"PhysicalResourceId": "agentic-data-ingestion-test-fixtures-seeding"}`` so
    the ``cr.Provider`` framework reports the same physical resource ID across
    Create/Update/Delete cycles. Raises ``RuntimeError`` (with a truncated
    message) on any failure so the failure propagates as a CloudFormation
    rollback with a diagnosable reason.
    """
    request_type = event["RequestType"]

    # Delete: no-op, return success immediately without connecting (Req 6.11).
    if request_type == "Delete":
        return {"PhysicalResourceId": "agentic-data-ingestion-test-fixtures-seeding"}

    # Create / Update: seed both databases.
    try:
        mysql_secret = _get_secret(os.environ["MYSQL_SECRET_ARN"])
        pg_secret = _get_secret(os.environ["POSTGRES_SECRET_ARN"])

        _seed_mysql(mysql_secret, os.environ["MYSQL_SEED_SQL_PATH"])
        _seed_postgres(pg_secret, os.environ["POSTGRES_SEED_SQL_PATH"])

        return {"PhysicalResourceId": "agentic-data-ingestion-test-fixtures-seeding"}
    except Exception as exc:  # propagated to cr.Provider, which translates to FAILED
        # Truncate to TRUNCATE_LEN chars so the CFN reason is diagnosable but
        # not absurdly long (Req 6.13).
        msg = str(exc)[:TRUNCATE_LEN]
        raise RuntimeError(f"Seeding failed: {msg}") from exc


def _get_secret(arn: str) -> dict:
    """Fetch and JSON-decode a Secrets Manager secret by ARN."""
    sm = boto3.client("secretsmanager")
    raw = sm.get_secret_value(SecretId=arn)["SecretString"]
    return json.loads(raw)


def _connect_mysql(secret: dict):
    """Connect to MySQL_Source with exponential-backoff retries on transient errors."""
    return _retry_connect(
        lambda: pymysql.connect(
            host=secret["host"],
            port=int(secret["port"]),
            user=secret["username"],
            password=secret["password"],
            database=secret["dbname"],
            connect_timeout=10,
        ),
        label="MySQL",
    )


def _connect_postgres(secret: dict):
    """Connect to Postgres_Target's ``testdb`` database with retry backoff.

    The Postgres database name is hardcoded to ``"testdb"`` because the cluster
    creates it via ``defaultDatabaseName`` — it is not stored in the secret
    JSON.
    """
    return _retry_connect(
        lambda: pg8000.dbapi.connect(
            host=secret["host"],
            port=int(secret["port"]),
            user=secret["username"],
            password=secret["password"],
            database="testdb",
            timeout=10,
        ),
        label="Postgres",
    )


def _retry_connect(connect_fn, *, label: str):
    """Retry ``connect_fn`` with exponential backoff on transient errors.

    Backoff sequence: ``1s, 2s, 4s, 8s, 16s`` over ``MAX_RETRIES`` attempts —
    total ~31s before giving up (Req 6.12). On exhaustion raises a
    ``RuntimeError`` whose message includes ``label`` and the last exception.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return connect_fn()
        except TRANSIENT_ERRORS as exc:
            last_exc = exc
            sleep_for = INITIAL_BACKOFF_SEC * (2**attempt)
            print(f"{label} connect attempt {attempt + 1}/{MAX_RETRIES} failed ({exc!r}); sleeping {sleep_for}s")
            time.sleep(sleep_for)
    raise RuntimeError(f"{label} connect failed after {MAX_RETRIES} retries: {last_exc!r}")


def _split_statements(sql: str) -> list[str]:
    """Split ``sql`` on ``;`` and drop empty/whitespace-only fragments.

    Implements the simple statement-splitter the seed SQL files are written
    against (Req 4.7, 6.10). Each non-empty statement is stripped of leading
    and trailing whitespace.
    """
    return [s.strip() for s in sql.split(";") if s.strip()]


def _seed_mysql(secret: dict, sql_path: str) -> None:
    """Read ``sql_path``, split into statements, and execute against MySQL_Source.

    On per-statement failure raises ``RuntimeError`` carrying the offending
    statement (truncated to ``TRUNCATE_LEN``) chained to the original cause.
    Always closes the connection.
    """
    with open(sql_path, encoding="utf-8") as f:
        statements = _split_statements(f.read())
    conn = _connect_mysql(secret)
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                try:
                    cur.execute(stmt)
                except Exception as exc:
                    raise RuntimeError(f"MySQL seed statement failed: {stmt[:TRUNCATE_LEN]!r}") from exc
        conn.commit()
    finally:
        conn.close()


def _seed_postgres(secret: dict, sql_path: str) -> None:
    """Read ``sql_path``, split into statements, and execute against Postgres_Target.

    The Postgres seed file is intentionally minimal — currently runs a
    ``SELECT 1`` health-check (Req 5.2, 5.3) — but this helper handles
    arbitrary statement counts so future expansion is trivial. On
    per-statement failure raises ``RuntimeError`` chained to the original
    cause. Always closes the connection.
    """
    with open(sql_path, encoding="utf-8") as f:
        statements = _split_statements(f.read())
    conn = _connect_postgres(secret)
    try:
        cur = conn.cursor()
        for stmt in statements:
            try:
                cur.execute(stmt)
            except Exception as exc:
                raise RuntimeError(f"Postgres seed statement failed: {stmt[:TRUNCATE_LEN]!r}") from exc
        conn.commit()
    finally:
        conn.close()
