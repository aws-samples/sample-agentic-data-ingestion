# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the ResourceLedger Cleanup_Contract.

Validates:
- LIFO teardown order
- Cleanup continues past individual item failures
- All registration methods work correctly with mocked boto3/REST
- Glue mirror teardown is absent-tolerant without hiding real failures

These tests use NO real AWS resources — all boto3 calls are mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError
from tests.e2e._harness.ledger import ResourceLedger


class TestLedgerLIFOOrder:
    """Verify teardown happens in reverse registration order."""

    def test_cleanup_executes_in_lifo_order(self):
        """Items registered first are cleaned up last."""
        call_order: list[str] = []

        ledger = ResourceLedger()
        ledger.add_callable(lambda: call_order.append("first"))
        ledger.add_callable(lambda: call_order.append("second"))
        ledger.add_callable(lambda: call_order.append("third"))

        ledger.cleanup()

        assert call_order == ["third", "second", "first"]

    def test_cleanup_with_single_item(self):
        """Single-item cleanup works correctly."""
        called = []
        ledger = ResourceLedger()
        ledger.add_callable(lambda: called.append("only"))
        ledger.cleanup()
        assert called == ["only"]

    def test_cleanup_with_no_items(self):
        """Cleanup on empty ledger does nothing and returns no errors."""
        ledger = ResourceLedger()
        errors = ledger.cleanup()
        assert errors == []


class TestLedgerErrorResilience:
    """Verify cleanup continues when individual teardown items raise."""

    def test_cleanup_continues_past_raising_item(self):
        """A failing teardown does not prevent subsequent items from running."""
        call_order: list[str] = []

        def _raise():
            raise RuntimeError("boom")

        ledger = ResourceLedger()
        ledger.add_callable(lambda: call_order.append("first"))
        ledger.add_callable(_raise)
        ledger.add_callable(lambda: call_order.append("third"))

        errors = ledger.cleanup()

        # Both non-failing items executed (LIFO: third first, then first)
        assert call_order == ["third", "first"]
        # One error collected
        assert len(errors) == 1
        assert "boom" in str(errors[0][1])

    def test_cleanup_continues_when_first_item_raises(self):
        """If the last-registered (first to execute) item raises, others still run."""
        call_order: list[str] = []

        def _raise():
            raise ValueError("last registered fails")

        ledger = ResourceLedger()
        ledger.add_callable(lambda: call_order.append("first_registered"))
        ledger.add_callable(lambda: call_order.append("second_registered"))
        ledger.add_callable(_raise)

        errors = ledger.cleanup()

        # The two non-failing items ran in LIFO order
        assert call_order == ["second_registered", "first_registered"]
        assert len(errors) == 1

    def test_cleanup_when_multiple_items_raise(self):
        """Multiple failures are all collected; all items attempted."""
        call_order: list[str] = []

        def _raise_b():
            raise RuntimeError("fail-B")

        def _raise_d():
            raise RuntimeError("fail-D")

        ledger = ResourceLedger()
        ledger.add_callable(lambda: call_order.append("A"))
        ledger.add_callable(_raise_b)
        ledger.add_callable(lambda: call_order.append("C"))
        ledger.add_callable(_raise_d)

        errors = ledger.cleanup()

        # LIFO execution: D(fail), C, B(fail), A
        assert call_order == ["C", "A"]
        assert len(errors) == 2

    def test_entries_cleared_after_cleanup(self):
        """After cleanup, the ledger is empty regardless of errors."""

        def _raise():
            raise RuntimeError("x")

        ledger = ResourceLedger()
        ledger.add_callable(lambda: None)
        ledger.add_callable(_raise)

        ledger.cleanup()

        assert len(ledger) == 0


class TestLedgerS3Object:
    """Verify add_s3_object registration and teardown."""

    def test_add_s3_object_deletes_on_cleanup(self):
        """Registered S3 object gets delete_object called."""
        mock_session = MagicMock()
        mock_s3 = MagicMock()
        mock_session.client.return_value = mock_s3

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_s3_object("my-bucket", "path/to/file.csv")

        ledger.cleanup()

        mock_session.client.assert_called_with("s3")
        mock_s3.delete_object.assert_called_once_with(Bucket="my-bucket", Key="path/to/file.csv")


class TestLedgerExecution:
    """Verify add_execution registration and teardown."""

    def test_add_execution_stops_running(self):
        """A RUNNING execution gets stopped."""
        mock_session = MagicMock()
        mock_sfn = MagicMock()
        mock_session.client.return_value = mock_sfn
        mock_sfn.describe_execution.return_value = {"status": "RUNNING"}

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_execution("arn:aws:states:us-west-2:123:execution:sm:exec-1")

        ledger.cleanup()

        mock_sfn.describe_execution.assert_called_once_with(
            executionArn="arn:aws:states:us-west-2:123:execution:sm:exec-1"
        )
        mock_sfn.stop_execution.assert_called_once_with(
            executionArn="arn:aws:states:us-west-2:123:execution:sm:exec-1",
            cause="e2e test cleanup",
        )

    def test_add_execution_skips_stop_when_not_running(self):
        """A SUCCEEDED execution does not get stopped."""
        mock_session = MagicMock()
        mock_sfn = MagicMock()
        mock_session.client.return_value = mock_sfn
        mock_sfn.describe_execution.return_value = {"status": "SUCCEEDED"}

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_execution("arn:aws:states:us-west-2:123:execution:sm:exec-1")

        ledger.cleanup()

        mock_sfn.stop_execution.assert_not_called()


class TestLedgerPipeline:
    """Verify add_pipeline registration and teardown."""

    def test_add_pipeline_archives_then_deletes(self):
        """Pipeline teardown calls archive then delete via REST client."""
        mock_rest = MagicMock()

        ledger = ResourceLedger()
        ledger.set_rest_client(mock_rest)
        ledger.add_pipeline("e2e-test-abc123")

        ledger.cleanup()

        # Verify archive called before delete
        calls = mock_rest.method_calls
        assert len(calls) == 2
        assert calls[0] == ("put", ("/api/pipelines/e2e-test-abc123/archive",), {})
        assert calls[1] == ("delete", ("/api/pipelines/e2e-test-abc123",), {})


class TestLedgerGlueMirror:
    """Verify add_glue_table / add_glue_database registration and teardown.

    These exist because nothing used to delete the Glue mirror
    ``create_infrastructure`` writes for an S3 Tables table, so every
    ``NEW_S3_TABLE_BUCKET`` e2e run left an undeletable ``glue_catalog`` row on
    the Schemas page.
    """

    @staticmethod
    def _entity_not_found() -> ClientError:
        return ClientError(
            {"Error": {"Code": "EntityNotFoundException", "Message": "Entity Not Found"}},
            "DeleteTable",
        )

    def test_add_glue_table_deletes_on_cleanup(self):
        mock_session = MagicMock()
        mock_glue = MagicMock()
        mock_session.client.return_value = mock_glue

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_glue_table("agentic_data_ingestion_e2e_s3tables_34a96b2d", "e2e_namespace_e2e_target_33a29424")

        errors = ledger.cleanup()

        mock_session.client.assert_called_with("glue")
        mock_glue.delete_table.assert_called_once_with(
            DatabaseName="agentic_data_ingestion_e2e_s3tables_34a96b2d",
            Name="e2e_namespace_e2e_target_33a29424",
        )
        assert errors == []

    def test_add_glue_database_deletes_on_cleanup(self):
        mock_session = MagicMock()
        mock_glue = MagicMock()
        mock_session.client.return_value = mock_glue

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_glue_database("agentic_data_ingestion_e2e_s3tables_34a96b2d")

        errors = ledger.cleanup()

        mock_glue.delete_database.assert_called_once_with(Name="agentic_data_ingestion_e2e_s3tables_34a96b2d")
        assert errors == []

    def test_an_absent_glue_table_is_not_an_error(self):
        """A table already gone, or never created, is a non-event.

        Not merely swallowed by ``cleanup``: the returned error list must stay
        empty, because a run that failed before provisioning reached the catalog
        is the *normal* case and must not look like a teardown failure.
        """
        mock_session = MagicMock()
        mock_glue = MagicMock()
        mock_glue.delete_table.side_effect = self._entity_not_found()
        mock_session.client.return_value = mock_glue

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_glue_table("db", "tbl")

        assert ledger.cleanup() == []

    def test_an_absent_glue_database_is_not_an_error(self):
        mock_session = MagicMock()
        mock_glue = MagicMock()
        mock_glue.delete_database.side_effect = self._entity_not_found()
        mock_session.client.return_value = mock_glue

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_glue_database("db")

        assert ledger.cleanup() == []

    def test_a_non_absence_glue_error_is_still_reported(self):
        """An ``AccessDeniedException`` is not an absence.

        Folding one into "already gone" would report a clean teardown over Glue
        entries that are still standing — the distinction ``destroy.sh``'s
        ``is_absence_error`` draws for the operator-facing teardown.
        """
        mock_session = MagicMock()
        mock_glue = MagicMock()
        mock_glue.delete_database.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "DeleteDatabase",
        )
        mock_session.client.return_value = mock_glue

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_glue_database("db")

        errors = ledger.cleanup()

        assert len(errors) == 1
        assert "AccessDenied" in str(errors[0][1])

    def test_table_is_deleted_before_its_database(self):
        """LIFO plus register-database-first gives table-then-database order."""
        mock_session = MagicMock()
        mock_glue = MagicMock()
        mock_session.client.return_value = mock_glue

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_glue_database("db")
        ledger.add_glue_table("db", "tbl")

        ledger.cleanup()

        names = [c[0] for c in mock_glue.method_calls]
        assert names == ["delete_table", "delete_database"]

    def test_a_failing_glue_teardown_does_not_strand_the_rest(self):
        """One raising Glue delete must not block the other registered teardowns."""
        mock_session = MagicMock()
        mock_glue = MagicMock()
        mock_s3 = MagicMock()
        mock_glue.delete_database.side_effect = RuntimeError("boom")
        mock_session.client.side_effect = lambda svc: {"glue": mock_glue, "s3": mock_s3}[svc]

        ledger = ResourceLedger()
        ledger.set_boto3_session(mock_session)
        ledger.add_s3_object("bucket", "key")
        ledger.add_glue_database("db")

        errors = ledger.cleanup()

        assert len(errors) == 1
        mock_s3.delete_object.assert_called_once_with(Bucket="bucket", Key="key")


class TestLedgerCallable:
    """Verify add_callable registration and teardown."""

    def test_add_callable_with_args(self):
        """Callable is invoked with provided args and kwargs."""
        results: list[tuple] = []

        def my_cleanup(a: int, b: str, flag: bool = False) -> None:
            results.append((a, b, flag))

        ledger = ResourceLedger()
        ledger.add_callable(my_cleanup, 42, "hello", flag=True)

        ledger.cleanup()

        assert results == [(42, "hello", True)]
