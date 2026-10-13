# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cleanup_Contract — ResourceLedger for e2e test artifact teardown.

Records every AWS artifact created during a test and tears them down in
**LIFO order** inside a ``finally`` block, so cleanup runs even when a test
raises mid-run. Individual teardown errors are swallowed and logged so that
one failure cannot strand remaining cleanup items.

Usage (via the ``e2e_ledger`` fixture):

    def test_something(e2e_ledger, boto3_session, resource_names):
        e2e_ledger.set_boto3_session(boto3_session)
        e2e_ledger.add_s3_object(resource_names["source_bucket"], "key.csv")
        # ... test logic ...
        # cleanup() called automatically in fixture teardown
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

#: Glue's own "the entity is not there". The ONE error class the Glue teardowns
#: below treat as a recorded non-event rather than a failure.
#:
#: Absence is the *expected* outcome here, not the edge case. ``delete_database``
#: removes the tables it contains, so by the time the database's own teardown
#: runs the table teardown has already succeeded — and the mirror is registered
#: only after a pipeline creation returns, while the Glue entries themselves are
#: written later by ``create_infrastructure``. A run that fails before
#: provisioning reaches the catalog therefore has nothing to delete, and a run
#: that was torn down twice finds the second attempt empty. Neither is a fault.
#:
#: Everything else is left to raise into ``cleanup``'s per-entry ``except``,
#: deliberately: an ``AccessDeniedException`` or a throttle is not an absence,
#: and folding one into "already gone" would report a clean teardown over Glue
#: entries that are still standing — the same distinction ``destroy.sh``'s
#: ``is_absence_error`` draws for the operator-facing teardown.
_GLUE_ABSENT_ERROR_CODE = "EntityNotFoundException"


def _is_glue_absence(exc: BaseException) -> bool:
    """Is this exception Glue reporting that the entity does not exist?

    A narrow test on the modelled error code, not on the message text: Glue's
    ``EntityNotFoundException`` message differs between ``delete_table`` and
    ``delete_database`` and is not part of the API contract, whereas the code
    is.
    """
    if not isinstance(exc, ClientError):
        return False
    return exc.response.get("Error", {}).get("Code") == _GLUE_ABSENT_ERROR_CODE


@dataclass
class _LedgerEntry:
    """Internal record of a registered teardown action."""

    kind: str
    teardown_fn: Callable[[], Any]
    description: str


class ResourceLedger:
    """LIFO cleanup ledger for e2e test artifacts.

    Supports six registration methods corresponding to the artifact types
    created during e2e tests:

    - ``add_s3_object(bucket, key)`` — deletes an S3 object
    - ``add_execution(execution_arn)`` — stops a Step Functions execution if RUNNING
    - ``add_pipeline(pipeline_id)`` — archives then deletes a pipeline via REST
    - ``add_glue_table(database, table)`` — deletes an S3 Tables Glue mirror table
    - ``add_glue_database(database)`` — deletes an S3 Tables Glue mirror database
    - ``add_callable(fn, *args, **kwargs)`` — arbitrary teardown callable

    Call ``cleanup()`` to execute all registered teardowns in LIFO order.
    Each item's errors are caught, logged, and swallowed so cleanup of
    subsequent items is never blocked.
    """

    def __init__(self) -> None:
        self._entries: list[_LedgerEntry] = []
        self._boto3_session: Any | None = None
        self._rest_client: Any | None = None

    def set_boto3_session(self, session: Any) -> None:
        """Provide the boto3 session for S3/SFN cleanup operations."""
        self._boto3_session = session

    def set_rest_client(self, client: Any) -> None:
        """Provide the SigV4 REST client for pipeline archive/delete."""
        self._rest_client = client

    # ── Registration methods ──────────────────────────────────────────

    def add_s3_object(self, bucket: str, key: str) -> None:
        """Register an S3 object for deletion on cleanup."""

        def _delete_s3_object() -> None:
            assert self._boto3_session is not None, "boto3_session not set on ledger"
            s3 = self._boto3_session.client("s3")
            s3.delete_object(Bucket=bucket, Key=key)

        self._entries.append(
            _LedgerEntry(
                kind="s3_object",
                teardown_fn=_delete_s3_object,
                description=f"s3://{bucket}/{key}",
            )
        )

    def add_execution(self, execution_arn: str) -> None:
        """Register a Step Functions execution — stops it if still RUNNING."""

        def _stop_execution() -> None:
            assert self._boto3_session is not None, "boto3_session not set on ledger"
            sfn = self._boto3_session.client("stepfunctions")
            desc = sfn.describe_execution(executionArn=execution_arn)
            if desc["status"] == "RUNNING":
                sfn.stop_execution(
                    executionArn=execution_arn,
                    cause="e2e test cleanup",
                )

        self._entries.append(
            _LedgerEntry(
                kind="execution",
                teardown_fn=_stop_execution,
                description=execution_arn,
            )
        )

    def add_pipeline(self, pipeline_id: str) -> None:
        """Register a pipeline for archive → delete on cleanup."""

        def _teardown_pipeline() -> None:
            assert self._rest_client is not None, "rest_client not set on ledger"
            # Archive first (delete requires status=="archived")
            self._rest_client.put(f"/api/pipelines/{pipeline_id}/archive")
            # Then delete
            self._rest_client.delete(f"/api/pipelines/{pipeline_id}")

        self._entries.append(
            _LedgerEntry(
                kind="pipeline",
                teardown_fn=_teardown_pipeline,
                description=f"pipeline:{pipeline_id}",
            )
        )

    def add_glue_table(self, database: str, table: str) -> None:
        """Register one Glue Data Catalog table for deletion on cleanup.

        Used for the **Glue mirror** ``create_infrastructure`` writes for every
        S3 Tables Iceberg table it provisions (``LGR-acca05d-1``, template /
        ratified). Nothing else in the harness deleted it, so every e2e run
        against a ``NEW_S3_TABLE_BUCKET`` target left a ``glue_catalog`` row
        behind on the Schemas page — permanently, because the dashboard's
        ``DELETE /api/schemas/{id}`` refuses a catalog-derived id by design.

        An absent table is a non-event: see ``_GLUE_ABSENT_ERROR_CODE``.

        Args:
            database: The mirror's Glue database name.
            table: The mirror's Glue table name.
        """

        def _delete_glue_table() -> None:
            assert self._boto3_session is not None, "boto3_session not set on ledger"
            glue = self._boto3_session.client("glue")
            try:
                glue.delete_table(DatabaseName=database, Name=table)
            except Exception as exc:
                if _is_glue_absence(exc):
                    logger.info(
                        "Glue table %s.%s already absent — nothing to delete",
                        database,
                        table,
                    )
                    return
                raise

        self._entries.append(
            _LedgerEntry(
                kind="glue_table",
                teardown_fn=_delete_glue_table,
                description=f"glue:{database}.{table}",
            )
        )

    def add_glue_database(self, database: str) -> None:
        """Register one Glue Data Catalog database for deletion on cleanup.

        The mirror's database half. Register it **before** the table so that
        LIFO deletes the table first — ``delete_database`` would take the table
        with it, but an explicit table delete keeps the teardown legible in the
        log and keeps each half independently absent-tolerant.

        An absent database is a non-event: see ``_GLUE_ABSENT_ERROR_CODE``.

        Args:
            database: The mirror's Glue database name.
        """

        def _delete_glue_database() -> None:
            assert self._boto3_session is not None, "boto3_session not set on ledger"
            glue = self._boto3_session.client("glue")
            try:
                glue.delete_database(Name=database)
            except Exception as exc:
                if _is_glue_absence(exc):
                    logger.info("Glue database %s already absent — nothing to delete", database)
                    return
                raise

        self._entries.append(
            _LedgerEntry(
                kind="glue_database",
                teardown_fn=_delete_glue_database,
                description=f"glue:{database}",
            )
        )

    def add_callable(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Register an arbitrary teardown callable."""

        def _call() -> None:
            fn(*args, **kwargs)

        desc = getattr(fn, "__name__", repr(fn))
        self._entries.append(
            _LedgerEntry(
                kind="callable",
                teardown_fn=_call,
                description=f"callable:{desc}({args}, {kwargs})",
            )
        )

    # ── Cleanup ───────────────────────────────────────────────────────

    def cleanup(self) -> list[tuple[str, Exception]]:
        """Execute all registered teardowns in LIFO order.

        Each item is executed inside a try/except — errors are logged and
        collected but never re-raised, so one failing cleanup cannot strand
        subsequent items.

        Returns:
            A list of (description, exception) tuples for any items that
            raised during teardown.
        """
        errors: list[tuple[str, Exception]] = []
        try:
            for entry in reversed(self._entries):
                try:
                    entry.teardown_fn()
                    logger.debug("Cleaned up %s: %s", entry.kind, entry.description)
                except Exception as exc:
                    logger.warning(
                        "Cleanup failed for %s (%s): %s",
                        entry.kind,
                        entry.description,
                        exc,
                    )
                    errors.append((entry.description, exc))
        finally:
            # Always clear the entries regardless of what happened
            self._entries.clear()
        return errors

    @property
    def entries(self) -> list[_LedgerEntry]:
        """Read-only access to current entries (for testing/inspection)."""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
