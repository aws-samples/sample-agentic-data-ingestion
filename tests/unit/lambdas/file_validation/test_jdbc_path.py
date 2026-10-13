# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the JDBC source path and explicit-pipeline_id wiring in
the ``file_validation`` Lambda (Tasks 6.1 / 6.2 / 6.3).

Covers:
- ``handle_jdbc_source_validation`` synthetic state-row shape (both
  ``pipeline_id`` and ``original_pipeline_id`` set to ``event["pipeline_id"]``)
- ``handle_jdbc_source_validation`` response shape, missing-pipeline_id,
  missing pipeline record, and missing-source_config error paths
- ``lambda_handler`` honours an explicit ``event["pipeline_id"]`` for S3
  events and falls back to S3-key-prefix derivation when absent
- ``handle_prefix_trigger`` honours explicit ``pipeline_id`` and falls back
  to S3-key-prefix derivation when absent

Validates: Requirements 12.4, 13.1, 13.2, 22.1
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_s3_event(bucket: str, key: str, **extras) -> dict:
    """Build a minimal S3-record event for the lambda handler."""
    event = {
        "Records": [
            {
                "eventSource": "aws:s3",
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                },
            }
        ],
    }
    event.update(extras)
    return event


def _put_item_args(mock_table: MagicMock) -> dict:
    """Return the ``Item`` kwarg passed to the most recent ``put_item`` call."""
    assert mock_table.put_item.called, "put_item was never called"
    return mock_table.put_item.call_args.kwargs["Item"]


# ── Shared fixture: import the file_validation Lambda with mocked AWS ───


@pytest.fixture()
def lambda_env():
    """Import file_validation lambda_function with fully mocked AWS clients.

    Mirrors the pattern in test_dual_path_routing.py / test_native_passthrough.py
    but additionally patches the JDBC ``pipelines_table`` so the JDBC branch
    can fetch ``source_config`` without hitting real DynamoDB.

    Yields ``(lf, mock_s3, mock_state_table, mock_pipelines_table)``.
    """
    lambda_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "..",
            "lambda",
            "agentic_data_ingestion_file_validation",
        )
    )
    lambda_path = os.path.join(lambda_dir, "lambda_function.py")
    shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

    os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
    os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
    os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")

    mock_s3 = MagicMock()
    mock_state_table = MagicMock()
    mock_pipelines_table = MagicMock()
    mock_dynamodb = MagicMock()

    # boto3.resource("dynamodb").Table(name) → return state vs pipelines
    # by name so module-level initialization wires both correctly.
    def _table_router(name):
        if name == "agentic_data_ingestion_pipelines":
            return mock_pipelines_table
        return mock_state_table

    mock_dynamodb.Table.side_effect = _table_router

    # Sensible defaults used by S3-path tests
    mock_s3.head_object.return_value = {"ContentLength": 1024}
    mock_state_table.put_item.return_value = {}
    mock_pipelines_table.put_item.return_value = {}

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3
    mock_boto3.resource.return_value = mock_dynamodb

    sys.path.insert(0, lambda_dir)
    sys.path.insert(0, shared_dir)

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        sys.modules.pop("lambda_function", None)

        spec = importlib.util.spec_from_file_location("lambda_function", lambda_path)
        lf = importlib.util.module_from_spec(spec)
        sys.modules["lambda_function"] = lf
        spec.loader.exec_module(lf)

        # Defensive: re-patch module-level handles so any reassignment during
        # import (or future refactors) doesn't leave them pointing at real
        # AWS clients in test contexts.
        lf.s3_client = mock_s3
        lf.table = mock_state_table
        lf.pipelines_table = mock_pipelines_table

        yield lf, mock_s3, mock_state_table, mock_pipelines_table

    sys.modules.pop("lambda_function", None)
    if lambda_dir in sys.path:
        sys.path.remove(lambda_dir)
    if shared_dir in sys.path:
        sys.path.remove(shared_dir)


# ── handle_jdbc_source_validation ────────────────────────────────────────


class TestJdbcSourceValidation:
    """JDBC dispatch writes a synthetic row and returns the standard shape.

    Validates: Requirements 13.1, 13.2.
    """

    def _seed_pipeline(
        self,
        mock_pipelines_table: MagicMock,
        pipeline_id: str,
        engine: str = "postgres",
        jdbc_schema: str = "public",
        jdbc_table: str = "orders",
    ) -> None:
        mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": pipeline_id,
                "source_type": "JDBC",
                "source_config": {
                    "engine": engine,
                    "jdbc_schema": jdbc_schema,
                    "jdbc_table": jdbc_table,
                },
            }
        }

    def test_synthetic_row_uses_event_pipeline_id_for_both_keys(self, lambda_env):
        """Both pipeline_id AND original_pipeline_id equal event["pipeline_id"]."""
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        self._seed_pipeline(mock_pipelines_table, "test-pipe-abc")

        event = {"source_type": "JDBC", "pipeline_id": "test-pipe-abc"}
        result = lf.lambda_handler(event, MagicMock())

        assert result["statusCode"] == 200
        item = _put_item_args(mock_state_table)
        assert item["pipeline_id"] == "test-pipe-abc"
        assert item["original_pipeline_id"] == "test-pipe-abc"
        assert item["pipeline_id"] == item["original_pipeline_id"]

    def test_synthetic_row_shape(self, lambda_env):
        """State row carries the documented JDBC fields per Requirement 13.1."""
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        self._seed_pipeline(
            mock_pipelines_table,
            "test-pipe-abc",
            engine="postgres",
            jdbc_schema="public",
            jdbc_table="orders",
        )

        event = {"source_type": "JDBC", "pipeline_id": "test-pipe-abc"}
        lf.lambda_handler(event, MagicMock())

        item = _put_item_args(mock_state_table)
        assert item["step_name"] == "file_validation"
        assert item["step_sequence"] == 1
        assert item["status"] == "success"
        assert item["file_type"] == "jdbc"
        # Synthetic URI shape per design.md / Requirement 13.1
        assert item["original_s3_uri"] == "jdbc://postgres/public/orders"

    def test_synthetic_row_output_data_summary(self, lambda_env):
        """output_data.summary carries file_format=jdbc and zero size, and no row estimate.

        AMENDED 2026-10-10: the summary used to carry ``record_count_estimate: 0``.
        A JDBC source has no file to estimate from, so 0 claimed "no records" and
        the Metrics Dashboard summed it into a measured zero. The key is now absent,
        which the metrics reader reports as Not established (p39 R4.9).
        """
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        self._seed_pipeline(mock_pipelines_table, "test-pipe-abc")

        event = {"source_type": "JDBC", "pipeline_id": "test-pipe-abc"}
        lf.lambda_handler(event, MagicMock())

        item = _put_item_args(mock_state_table)
        summary = item["output_data"]["summary"]
        assert summary["file_format"] == "jdbc"
        assert summary["file_size_bytes"] == 0
        assert "record_count_estimate" not in summary
        assert item["output_data"]["artifact_type"] == "file_validation"
        assert item["output_data"]["status"] == "success"

    def test_response_shape(self, lambda_env):
        """Response mirrors the S3-path return shape with source_format=jdbc."""
        lf, _, _, mock_pipelines_table = lambda_env
        self._seed_pipeline(mock_pipelines_table, "test-pipe-abc")

        event = {"source_type": "JDBC", "pipeline_id": "test-pipe-abc"}
        result = lf.lambda_handler(event, MagicMock())

        assert result["statusCode"] == 200
        assert result["original_pipeline_id"] == "test-pipe-abc"
        assert result["source_format"] == "jdbc"
        assert result["file_size_bytes"] == 0
        assert result["file_count"] == 1
        # run_id is generated; just sanity-check shape
        assert isinstance(result["run_id"], str)
        assert result["run_id"].startswith("run-")
        # body is JSON-encoded success message
        body = json.loads(result["body"])
        assert body["status"] == "success"
        assert body["original_pipeline_id"] == "test-pipe-abc"

    def test_jdbc_branch_does_not_call_s3(self, lambda_env):
        """JDBC dispatch never touches S3 — no HEAD, no GET, no LIST."""
        lf, mock_s3, _, mock_pipelines_table = lambda_env
        self._seed_pipeline(mock_pipelines_table, "test-pipe-abc")

        event = {"source_type": "JDBC", "pipeline_id": "test-pipe-abc"}
        lf.lambda_handler(event, MagicMock())

        mock_s3.head_object.assert_not_called()
        mock_s3.get_object.assert_not_called()
        mock_s3.list_objects_v2.assert_not_called()

    def test_missing_pipeline_id_raises_step_failure(self, lambda_env):
        """source_type=JDBC without pipeline_id fails the step (P28 R1.1: raise, not 500)."""
        lf, _, mock_state_table, _ = lambda_env

        event = {"source_type": "JDBC"}  # no pipeline_id
        with pytest.raises(lf.StepFailure) as exc_info:
            lf.lambda_handler(event, MagicMock())

        assert exc_info.value.step_name == "file_validation"
        assert "pipeline_id" in exc_info.value.message
        # No state row written when validation fails up-front
        mock_state_table.put_item.assert_not_called()

    def test_missing_pipeline_record_raises_step_failure(self, lambda_env):
        """Pipeline absent from agentic_data_ingestion_pipelines fails the step."""
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        mock_pipelines_table.get_item.return_value = {}  # no Item key

        event = {"source_type": "JDBC", "pipeline_id": "ghost-pipe"}
        with pytest.raises(lf.StepFailure) as exc_info:
            lf.lambda_handler(event, MagicMock())

        message = exc_info.value.message
        assert "ghost-pipe" in message or "not found" in message.lower()
        mock_state_table.put_item.assert_not_called()

    def test_missing_source_config_raises_step_failure(self, lambda_env):
        """Pipeline record without source_config fails the step."""
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        mock_pipelines_table.get_item.return_value = {
            "Item": {"pipeline_id": "broken-pipe", "source_type": "JDBC"}
            # no source_config
        }

        event = {"source_type": "JDBC", "pipeline_id": "broken-pipe"}
        with pytest.raises(lf.StepFailure) as exc_info:
            lf.lambda_handler(event, MagicMock())

        assert "source_config" in exc_info.value.message
        mock_state_table.put_item.assert_not_called()


# ── Explicit pipeline_id wiring (Task 6.3) ────────────────────────────────


class TestExplicitPipelineIdPath:
    """``lambda_handler`` and ``handle_prefix_trigger`` honour explicit
    ``event["pipeline_id"]`` and fall back to S3-key-prefix derivation when
    absent.

    Validates: Requirement 12.4.
    """

    # --- Single-file S3 path -------------------------------------------------

    def test_explicit_pipeline_id_is_used_for_both_keys(self, lambda_env):
        """Explicit pipeline_id on the event drives both DDB keys."""
        lf, _, mock_state_table, _ = lambda_env

        event = _make_s3_event("src-bucket", "legacy-pipe-id/data.csv", pipeline_id="explicit-pid-123")
        result = lf.lambda_handler(event, MagicMock())

        assert result["statusCode"] == 200
        item = _put_item_args(mock_state_table)
        assert item["pipeline_id"] == "explicit-pid-123"
        assert item["original_pipeline_id"] == "explicit-pid-123"
        # NOT derived from the S3 prefix
        assert item["pipeline_id"] != "legacy-pipe-id"

    def test_fallback_to_s3_prefix_when_pipeline_id_absent(self, lambda_env):
        """No explicit pipeline_id → derive from S3 key prefix."""
        lf, _, mock_state_table, _ = lambda_env

        event = _make_s3_event("src-bucket", "legacy-pipe-id/data.csv")
        result = lf.lambda_handler(event, MagicMock())

        assert result["statusCode"] == 200
        item = _put_item_args(mock_state_table)
        # get_pipeline_key("s3://src-bucket/legacy-pipe-id/data.csv") → "legacy-pipe-id"
        assert item["pipeline_id"] == "legacy-pipe-id"
        assert item["original_pipeline_id"] == "legacy-pipe-id"

    def test_explicit_pipeline_id_overrides_mismatched_s3_prefix(self, lambda_env):
        """Explicit pipeline_id wins even when it differs from the S3 prefix.

        This is the failure mode P17 fixes: prior to Task 6.3, two upload paths
        for the same pipeline would store DIFFERENT ``pipeline_id`` values on
        the state row depending on the source bucket layout. The explicit value
        from ``pipeline_trigger`` is now authoritative.
        """
        lf, _, mock_state_table, _ = lambda_env

        event = _make_s3_event(
            "weird-bucket",
            "some/nested/folder/structure/file.parquet",
            pipeline_id="canonical-pid",
        )
        lf.lambda_handler(event, MagicMock())

        item = _put_item_args(mock_state_table)
        assert item["pipeline_id"] == "canonical-pid"
        assert item["original_pipeline_id"] == "canonical-pid"

    # --- Prefix-trigger path -------------------------------------------------

    def _seed_prefix_listing(self, mock_s3: MagicMock) -> None:
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "prefix-pipe-456/incoming/2024-01-01.csv", "Size": 1024},
                {"Key": "prefix-pipe-456/incoming/2024-01-02.csv", "Size": 2048},
            ],
            "IsTruncated": False,
        }

    def test_prefix_trigger_explicit_pipeline_id(self, lambda_env):
        """handle_prefix_trigger uses explicit pipeline_id when provided."""
        lf, mock_s3, mock_state_table, _ = lambda_env
        self._seed_prefix_listing(mock_s3)

        event = {
            "trigger_mode": "prefix",
            "source_bucket": "src-bucket",
            "source_prefix": "prefix-pipe-456/incoming/",
            "pipeline_id": "explicit-prefix-pid",
        }
        result = lf.lambda_handler(event, MagicMock())

        assert result["statusCode"] == 200
        assert result["trigger_mode"] == "prefix"
        assert result["original_pipeline_id"] == "explicit-prefix-pid"
        item = _put_item_args(mock_state_table)
        assert item["pipeline_id"] == "explicit-prefix-pid"
        assert item["original_pipeline_id"] == "explicit-prefix-pid"

    def test_prefix_trigger_fallback_to_s3_prefix(self, lambda_env):
        """handle_prefix_trigger falls back to S3 derivation when absent."""
        lf, mock_s3, mock_state_table, _ = lambda_env
        self._seed_prefix_listing(mock_s3)

        event = {
            "trigger_mode": "prefix",
            "source_bucket": "src-bucket",
            "source_prefix": "prefix-pipe-456/incoming/",
            # no pipeline_id
        }
        result = lf.lambda_handler(event, MagicMock())

        assert result["statusCode"] == 200
        # get_pipeline_key("s3://src-bucket/prefix-pipe-456/incoming/") joins
        # all path components between bucket and trailing slot →
        # "prefix-pipe-456/incoming". This codifies the legacy fallback shape
        # that the explicit-pipeline_id path supersedes.
        assert result["original_pipeline_id"] == "prefix-pipe-456/incoming"
        item = _put_item_args(mock_state_table)
        assert item["pipeline_id"] == "prefix-pipe-456/incoming"
        assert item["original_pipeline_id"] == "prefix-pipe-456/incoming"


# ── Heal_Guard trigger persistence (P28 task 8.3) ─────────────────────────


class TestTriggeredByPersistence:
    """P28 R9.4 Heal_Guard: file_validation persists the SFN input's
    ``triggered_by`` on the run's identity row for the single-file and JDBC
    paths (the prefix path is covered in test_multi_file_ingestion.py).

    **Validates: Requirements 9.4**
    """

    def _seed_jdbc_pipeline(self, mock_pipelines_table: MagicMock, pipeline_id: str) -> None:
        mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": pipeline_id,
                "source_type": "JDBC",
                "source_config": {"engine": "postgres", "jdbc_schema": "public", "jdbc_table": "orders"},
            }
        }

    # --- Single-file S3 path -------------------------------------------------

    def test_single_file_persists_triggered_by(self, lambda_env):
        lf, _, mock_state_table, _ = lambda_env
        event = _make_s3_event("src-bucket", "pipe-1/data.csv", triggered_by="self_healing")

        lf.lambda_handler(event, MagicMock())

        item = _put_item_args(mock_state_table)
        assert item["triggered_by"] == "self_healing"

    def test_single_file_legacy_trigger_key_fallback(self, lambda_env):
        lf, _, mock_state_table, _ = lambda_env
        event = _make_s3_event("src-bucket", "pipe-1/data.csv", trigger="s3_upload")

        lf.lambda_handler(event, MagicMock())

        assert _put_item_args(mock_state_table)["triggered_by"] == "s3_upload"

    def test_single_file_without_trigger_omits_attribute(self, lambda_env):
        lf, _, mock_state_table, _ = lambda_env
        event = _make_s3_event("src-bucket", "pipe-1/data.csv")

        lf.lambda_handler(event, MagicMock())

        assert "triggered_by" not in _put_item_args(mock_state_table)

    # --- JDBC path ------------------------------------------------------------

    def test_jdbc_persists_triggered_by(self, lambda_env):
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        self._seed_jdbc_pipeline(mock_pipelines_table, "pipe-jdbc")
        event = {"source_type": "JDBC", "pipeline_id": "pipe-jdbc", "triggered_by": "self_healing"}

        lf.lambda_handler(event, MagicMock())

        item = _put_item_args(mock_state_table)
        assert item["step_name"] == "file_validation"
        assert item["triggered_by"] == "self_healing"

    def test_jdbc_without_trigger_omits_attribute(self, lambda_env):
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        self._seed_jdbc_pipeline(mock_pipelines_table, "pipe-jdbc")
        event = {"source_type": "JDBC", "pipeline_id": "pipe-jdbc"}

        lf.lambda_handler(event, MagicMock())

        assert "triggered_by" not in _put_item_args(mock_state_table)


# ── WorkerType persistence delivery path (P28 task 8.4) ───────────────────


class TestJobConfigOverridesPersistence:
    """P28 R9.5 WorkerType persistence: file_validation persists the SFN
    input's ``job_config_overrides`` on the run's identity row for the
    single-file and JDBC paths (the prefix path — the one heal reruns
    actually take — is covered in test_multi_file_ingestion.py).
    create_glue_pipeline reads it back by run_id and merges it over the
    generated job config so the self-healing agent's fix persists.

    **Validates: Requirements 9.5**
    """

    def _seed_jdbc_pipeline(self, mock_pipelines_table: MagicMock, pipeline_id: str) -> None:
        mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": pipeline_id,
                "source_type": "JDBC",
                "source_config": {"engine": "postgres", "jdbc_schema": "public", "jdbc_table": "orders"},
            }
        }

    # --- Single-file S3 path -------------------------------------------------

    def test_single_file_persists_job_config_overrides(self, lambda_env):
        lf, _, mock_state_table, _ = lambda_env
        event = _make_s3_event(
            "src-bucket",
            "pipe-1/data.csv",
            triggered_by="self_healing",
            job_config_overrides={"WorkerType": "G.2X"},
        )

        lf.lambda_handler(event, MagicMock())

        item = _put_item_args(mock_state_table)
        assert item["job_config_overrides"] == {"WorkerType": "G.2X"}

    def test_single_file_without_overrides_omits_attribute(self, lambda_env):
        lf, _, mock_state_table, _ = lambda_env
        event = _make_s3_event("src-bucket", "pipe-1/data.csv")

        lf.lambda_handler(event, MagicMock())

        assert "job_config_overrides" not in _put_item_args(mock_state_table)

    # --- JDBC path ------------------------------------------------------------

    def test_jdbc_persists_job_config_overrides(self, lambda_env):
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        self._seed_jdbc_pipeline(mock_pipelines_table, "pipe-jdbc")
        event = {
            "source_type": "JDBC",
            "pipeline_id": "pipe-jdbc",
            "triggered_by": "self_healing",
            "job_config_overrides": {"WorkerType": "G.4X", "NumberOfWorkers": 8},
        }

        lf.lambda_handler(event, MagicMock())

        item = _put_item_args(mock_state_table)
        assert item["step_name"] == "file_validation"
        assert item["job_config_overrides"] == {"WorkerType": "G.4X", "NumberOfWorkers": 8}

    def test_jdbc_without_overrides_omits_attribute(self, lambda_env):
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        self._seed_jdbc_pipeline(mock_pipelines_table, "pipe-jdbc")
        event = {"source_type": "JDBC", "pipeline_id": "pipe-jdbc"}

        lf.lambda_handler(event, MagicMock())

        assert "job_config_overrides" not in _put_item_args(mock_state_table)


# ── Rerun-result link persistence (P28 task 8.5) ──────────────────────────


class TestOriginalRunIdPersistence:
    """P28 R9.7 rerun-result linking: file_validation persists the SFN
    input's ``original_run_id`` on the run's identity row for the
    single-file and JDBC paths (the prefix path — the one heal reruns
    actually take — is covered in test_multi_file_ingestion.py).
    job_completion resolves this attribute (with
    ``triggered_by="self_healing"``) to link the heal rerun's terminal
    result back onto the ORIGINAL run's healing record.

    **Validates: Requirements 9.7**
    """

    def _seed_jdbc_pipeline(self, mock_pipelines_table: MagicMock, pipeline_id: str) -> None:
        mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": pipeline_id,
                "source_type": "JDBC",
                "source_config": {"engine": "postgres", "jdbc_schema": "public", "jdbc_table": "orders"},
            }
        }

    # --- Single-file S3 path -------------------------------------------------

    def test_single_file_persists_original_run_id(self, lambda_env):
        lf, _, mock_state_table, _ = lambda_env
        event = _make_s3_event(
            "src-bucket",
            "pipe-1/data.csv",
            triggered_by="self_healing",
            original_run_id="run-orig-1",
        )

        lf.lambda_handler(event, MagicMock())

        item = _put_item_args(mock_state_table)
        assert item["original_run_id"] == "run-orig-1"

    def test_single_file_without_original_run_id_omits_attribute(self, lambda_env):
        lf, _, mock_state_table, _ = lambda_env
        event = _make_s3_event("src-bucket", "pipe-1/data.csv")

        lf.lambda_handler(event, MagicMock())

        assert "original_run_id" not in _put_item_args(mock_state_table)

    # --- JDBC path ------------------------------------------------------------

    def test_jdbc_persists_original_run_id(self, lambda_env):
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        self._seed_jdbc_pipeline(mock_pipelines_table, "pipe-jdbc")
        event = {
            "source_type": "JDBC",
            "pipeline_id": "pipe-jdbc",
            "triggered_by": "self_healing",
            "original_run_id": "run-orig-2",
        }

        lf.lambda_handler(event, MagicMock())

        item = _put_item_args(mock_state_table)
        assert item["step_name"] == "file_validation"
        assert item["original_run_id"] == "run-orig-2"

    def test_jdbc_without_original_run_id_omits_attribute(self, lambda_env):
        lf, _, mock_state_table, mock_pipelines_table = lambda_env
        self._seed_jdbc_pipeline(mock_pipelines_table, "pipe-jdbc")
        event = {"source_type": "JDBC", "pipeline_id": "pipe-jdbc"}

        lf.lambda_handler(event, MagicMock())

        assert "original_run_id" not in _put_item_args(mock_state_table)
