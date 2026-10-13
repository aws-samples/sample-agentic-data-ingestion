# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
P28 R17 — stubbed-boto3 tests asserting the converted dashboard read paths
use Query (never Scan).

Each converted path from task 13.2 is exercised against MagicMock table
resources (the module-level boto3 Table objects are patched), asserting:

- ``table.query`` IS called with the expected IndexName and a
  KeyConditionExpression of the expected shape (partition equality, plus the
  time-window / sort-key condition where the path uses one), and
- ``table.scan`` is NEVER called.

Covered paths:
- metrics.py     — ``_get_stage_metrics`` (one Query per stage step),
                   ``_get_rows_loaded`` (load_metrics partition, no
                   filter). The cost fallback's StatusIndex enumeration
                   was covered here until p39 task 6.2 deleted the fallback;
                   ``metrics.py`` now reads the executions table on no path.
- glue_jobs.py   — ``_query_schema_extractions`` (schema_extract partition +
                   original_pipeline_id filter), ``_query_generated_code``
                   (pipeline_id partition, optional source_schema_id filter),
                   plus the ``get_script_versions`` endpoint end-to-end
- approvals.py   — ``_query_approvals_by_status`` / filtered + unfiltered +
                   pending listings on status-created-index
- pipelines_runs.py — per-run approval lookups (3 sites in get_run_details)
                   on the approvals run_id-index; cancel_run recovery
                   fallback on the executions run_id-index

Validates: Requirements 17.4 (17.1/17.2 conversions pinned by these tests)
"""

import json
import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import AttributeBase

# ---------------------------------------------------------------------------
# Path / env setup (same bootstrap as sibling dashboard_api tests)
# ---------------------------------------------------------------------------
DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(DASHBOARD_API_DIR), os.path.abspath(SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")


# ---------------------------------------------------------------------------
# Condition-expression introspection helpers
# ---------------------------------------------------------------------------


def _leaves(condition) -> list[tuple[str, str, tuple]]:
    """Flatten a boto3 condition tree into (operator, attribute_name, operands) leaves.

    ``Key("a").eq("x") & Key("b").between("lo", "hi")`` →
    ``[("=", "a", ("x",)), ("BETWEEN", "b", ("lo", "hi"))]``
    """
    expr = condition.get_expression()
    op = expr["operator"]
    values = expr["values"]
    if op in ("AND", "OR"):
        out: list[tuple[str, str, tuple]] = []
        for sub in values:
            out.extend(_leaves(sub))
        return out
    name = values[0].name if isinstance(values[0], AttributeBase) else str(values[0])
    return [(op, name, tuple(values[1:]))]


def _assert_query_only(*tables: MagicMock) -> None:
    """Every table used Query; none used Scan (R17.4)."""
    for table in tables:
        table.scan.assert_not_called()


def _query_calls(table: MagicMock, index_name: str) -> list[dict]:
    """kwargs of every table.query call that targeted the given index."""
    return [c.kwargs for c in table.query.call_args_list if c.kwargs.get("IndexName") == index_name]


# ═══════════════════════════════════════════════════════════════════════════
# metrics.py
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricsQueryNotScan:
    """Metrics read paths query the state-table GSI; that table is never scanned.

    The executions table dropped out of this class with p39 task 6.2 — see the
    note where its test used to be.
    """

    START = datetime(2025, 1, 1, tzinfo=UTC)
    END = datetime(2025, 1, 2, tzinfo=UTC)

    def test_stage_metrics_queries_step_gsi_per_stage(self):
        """_get_stage_metrics: one Query per stage step-name PARTITION on
        step_name-timestamp-index (step_name eq + timestamp BETWEEN window);
        no Scan. P28 R8.2 (task 14.2): renamed stages carry both their
        canonical and legacy partitions — still Queries, never a Scan."""
        from handlers import metrics as metrics_mod

        mock_state = MagicMock()
        mock_state.query.return_value = {"Items": []}
        mock_cw = MagicMock()
        mock_cw.get_metric_data.return_value = {"MetricDataResults": []}

        with (
            patch.object(metrics_mod, "state_table", mock_state),
            patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        ):
            metrics_mod._get_stage_metrics(self.START, self.END, 300)

        calls = _query_calls(mock_state, "step_name-timestamp-index")
        queried_steps = set()
        for kwargs in calls:
            leaves = _leaves(kwargs["KeyConditionExpression"])
            eq_leaves = [leaf for leaf in leaves if leaf[0] == "=" and leaf[1] == "step_name"]
            assert eq_leaves, f"query missing step_name equality: {leaves}"
            queried_steps.add(eq_leaves[0][2][0])
            between = [leaf for leaf in leaves if leaf[0] == "BETWEEN" and leaf[1] == "timestamp"]
            assert between, f"query missing timestamp BETWEEN window: {leaves}"
            assert between[0][2] == (self.START.isoformat(), self.END.isoformat())

        # every stage partition (canonical + legacy variants) got its own
        # bounded partition Query, plus — p59 — the RecordFailure sentinel
        # partition. That eleventh read is what lets the stage panel see failures
        # no stage row recorded (`LGR-9e76249-6`, template / ratified); it is the
        # SAME keyed, time-windowed Query shape on the same GSI, which is why
        # P28 R17.1 is untouched by it. The expectation is derived from the
        # handler's own constants, not transcribed.
        expected_partitions = {step for steps in metrics_mod._STAGE_STEP_NAMES.values() for step in steps}
        expected_partitions.add(metrics_mod._FAILURE_RECORD_STEP_NAME)
        assert queried_steps == expected_partitions
        _assert_query_only(mock_state)

    def test_stage_metrics_renamed_stages_query_both_partitions(self):
        """The four stages renamed in task 14.2 query BOTH their canonical
        snake_case and legacy PascalCase partitions (pre-rename rows persist
        in live tables — one-partition reads would undercount them)."""
        from handlers import metrics as metrics_mod

        for canonical, legacy in (
            ("create_code", "CodeGeneration"),
            ("create_infrastructure", "CreateInfrastructure"),
            ("create_quality_checks", "CreateQualityChecks"),
            ("run_pipeline", "RunPipeline"),
        ):
            partitions = next(steps for steps in metrics_mod._STAGE_STEP_NAMES.values() if canonical in steps)
            assert legacy in partitions, f"{canonical} stage must also query legacy partition {legacy}"

    def test_rows_loaded_queries_load_metrics_partition(self):
        """_get_rows_loaded: one Query on the step GSI's load_metrics partition,
        bounded to the window; no FilterExpression and no Scan.

        2026-10-10: replaced `test_lines_ingested_queries_file_validation_partition`
        with its subject. The old reader queried the `file_validation` partition
        with a server-side `status = success` filter; the rows-loaded read has no
        status to filter on — confirmation is decided client-side from
        `write_confirmed`."""
        from handlers import metrics as metrics_mod

        mock_state = MagicMock()
        mock_state.query.return_value = {"Items": []}

        with patch.object(metrics_mod, "state_table", mock_state):
            metrics_mod._get_rows_loaded(self.START, self.END)

        calls = _query_calls(mock_state, "step_name-timestamp-index")
        assert len(calls) == 1
        assert mock_state.query.call_count == 1, "every query must target step_name-timestamp-index"
        leaves = _leaves(calls[0]["KeyConditionExpression"])
        assert ("=", "step_name", ("load_metrics",)) in leaves
        assert ("BETWEEN", "timestamp", (self.START.isoformat(), self.END.isoformat())) in leaves
        assert "FilterExpression" not in calls[0]
        _assert_query_only(mock_state)

    # p39 task 6.2: `test_cost_fallback_queries_status_index_per_execution_status`
    # was deleted here, with its subject. It pinned the flat-rate cost
    # fallback's StatusIndex enumeration — one bounded Query per execution
    # status, closed set including UNKNOWN — and that fallback is gone
    # (`metrics.py`, R2.4), along with `_EXECUTION_STATUSES`,
    # `_EXEC_STATUS_INDEX` and the `executions_table` import it needed.
    # `metrics.py` no longer reads the executions table on any path, so there is
    # no Query-not-Scan claim left to make about it from this module.
    #
    # P28 R17.1 is NOT weakened by that deletion: the two metrics read paths
    # above — `_get_stage_metrics` and `_get_rows_loaded` (which replaced
    # `_get_lines_ingested` on 2026-10-10) — still assert bounded Queries on the
    # state table's step_name GSI with no Scan. The executions table's own Query-not-Scan coverage lives in
    # `TestPipelinesRunsQueryNotScan` below (cancel_run's run_id-index lookup).


# ═══════════════════════════════════════════════════════════════════════════
# glue_jobs.py
# ═══════════════════════════════════════════════════════════════════════════


class TestGlueJobsQueryNotScan:
    """Glue-jobs read helpers query GSIs; state/generated-code tables are never scanned."""

    def test_schema_extractions_query_step_gsi_with_pipeline_filter(self):
        """_query_schema_extractions: schema_extract partition on the step GSI,
        original_pipeline_id server-side filter; no Scan."""
        from handlers import glue_jobs as glue_mod

        mock_state = MagicMock()
        mock_state.query.return_value = {"Items": []}

        with patch.object(glue_mod, "state_table", mock_state):
            glue_mod._query_schema_extractions("pipe-1")

        calls = _query_calls(mock_state, "step_name-timestamp-index")
        assert len(calls) == 1
        leaves = _leaves(calls[0]["KeyConditionExpression"])
        assert ("=", "step_name", ("schema_extract",)) in leaves
        filter_leaves = _leaves(calls[0]["FilterExpression"])
        assert ("=", "original_pipeline_id", ("pipe-1",)) in filter_leaves
        _assert_query_only(mock_state)

    def test_generated_code_queries_pipeline_gsi(self):
        """_query_generated_code: pipeline_id partition on pipeline_id-index; no Scan."""
        from handlers import glue_jobs as glue_mod

        mock_code = MagicMock()
        mock_code.query.return_value = {"Items": []}

        with patch.object(glue_mod, "generated_code_table", mock_code):
            glue_mod._query_generated_code("pipe-1")

        calls = _query_calls(mock_code, "pipeline_id-index")
        assert len(calls) == 1
        leaves = _leaves(calls[0]["KeyConditionExpression"])
        assert ("=", "pipeline_id", ("pipe-1",)) in leaves
        _assert_query_only(mock_code)

    def test_generated_code_passes_schema_filter_through(self):
        """_query_generated_code with a source_schema_id FilterExpression keeps the
        filter server-side on the bounded partition read (the get_glue_job_script
        version-lookup pattern); no Scan."""
        from boto3.dynamodb.conditions import Attr
        from handlers import glue_jobs as glue_mod

        mock_code = MagicMock()
        mock_code.query.return_value = {"Items": []}

        with patch.object(glue_mod, "generated_code_table", mock_code):
            glue_mod._query_generated_code("pipe-1", FilterExpression=Attr("source_schema_id").eq("hash-1"))

        calls = _query_calls(mock_code, "pipeline_id-index")
        assert len(calls) == 1
        filter_leaves = _leaves(calls[0]["FilterExpression"])
        assert ("=", "source_schema_id", ("hash-1",)) in filter_leaves
        _assert_query_only(mock_code)

    def test_generated_code_query_paginates_like_the_old_scan(self):
        """Pagination via LastEvaluatedKey/ExclusiveStartKey is preserved: the
        Query-based helper still reads every page."""
        from handlers import glue_jobs as glue_mod

        mock_code = MagicMock()
        mock_code.query.side_effect = [
            {"Items": [{"code_version_id": "v1"}], "LastEvaluatedKey": {"PK": "x"}},
            {"Items": [{"code_version_id": "v2"}]},
        ]

        with patch.object(glue_mod, "generated_code_table", mock_code):
            items = glue_mod._query_generated_code("pipe-1")

        assert [i["code_version_id"] for i in items] == ["v1", "v2"]
        assert mock_code.query.call_count == 2
        second_call = mock_code.query.call_args_list[1].kwargs
        assert second_call["ExclusiveStartKey"] == {"PK": "x"}
        _assert_query_only(mock_code)

    def test_get_script_versions_endpoint_uses_query_not_scan(self):
        """get_script_versions endpoint end-to-end: Query on pipeline_id-index,
        never Scan."""
        from handlers import glue_jobs as glue_mod

        mock_code = MagicMock()
        mock_code.query.return_value = {
            "Items": [
                {
                    "code_version_id": "v1",
                    "code_s3_uri": "s3://b/k.py",
                    "generation_timestamp": "2025-01-01T00:00:00Z",
                }
            ]
        }

        with patch.object(glue_mod, "generated_code_table", mock_code):
            result = glue_mod.get_script_versions("pipe-1")

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["count"] == 1
        assert _query_calls(mock_code, "pipeline_id-index")
        _assert_query_only(mock_code)


# ═══════════════════════════════════════════════════════════════════════════
# approvals.py
# ═══════════════════════════════════════════════════════════════════════════


class TestApprovalsQueryNotScan:
    """Approval listings query the status GSI; the approvals table is never scanned."""

    def _pending_item(self) -> dict:
        return {
            "approval_id": "appr-1",
            "status": "pending",
            "pipeline_id": "pipe-1",
            "created_at": "2025-01-01T00:00:00Z",
        }

    def test_query_by_status_uses_status_gsi(self):
        """_query_approvals_by_status: status partition on status-created-index,
        paginating through all pages; no Scan."""
        from handlers import approvals as approvals_mod

        mock_approvals = MagicMock()
        mock_approvals.query.side_effect = [
            {"Items": [self._pending_item()], "LastEvaluatedKey": {"approval_id": "appr-1"}},
            {"Items": []},
        ]

        with patch.object(approvals_mod, "approvals_table", mock_approvals):
            items = approvals_mod._query_approvals_by_status("pending")

        assert len(items) == 1
        calls = _query_calls(mock_approvals, "status-created-index")
        assert len(calls) == 2  # first page + continuation
        leaves = _leaves(calls[0]["KeyConditionExpression"])
        assert ("=", "status", ("pending",)) in leaves
        assert calls[1]["ExclusiveStartKey"] == {"approval_id": "appr-1"}
        _assert_query_only(mock_approvals)

    def test_filtered_listing_queries_requested_status(self):
        """get_approvals?status=approved: single Query bounded to the approved
        partition; no Scan."""
        from handlers import approvals as approvals_mod

        mock_approvals = MagicMock()
        mock_approvals.query.return_value = {"Items": []}

        with patch.object(approvals_mod, "approvals_table", mock_approvals):
            result = approvals_mod.get_approvals({"status": "approved"})

        assert result["statusCode"] == 200
        calls = _query_calls(mock_approvals, "status-created-index")
        assert len(calls) == 1
        leaves = _leaves(calls[0]["KeyConditionExpression"])
        assert ("=", "status", ("approved",)) in leaves
        _assert_query_only(mock_approvals)

    def test_unfiltered_listing_enumerates_closed_status_set(self):
        """get_approvals without a status filter: one Query per lifecycle status
        (pending/approved/rejected/expired) instead of a table Scan."""
        from handlers import approvals as approvals_mod

        mock_approvals = MagicMock()
        mock_approvals.query.return_value = {"Items": []}

        with patch.object(approvals_mod, "approvals_table", mock_approvals):
            result = approvals_mod.get_approvals({})

        assert result["statusCode"] == 200
        assert set(approvals_mod._APPROVAL_STATUSES) == {"pending", "approved", "rejected", "expired"}
        calls = _query_calls(mock_approvals, "status-created-index")
        queried_statuses = set()
        for kwargs in calls:
            leaves = _leaves(kwargs["KeyConditionExpression"])
            queried_statuses.add(next(operands[0] for op, name, operands in leaves if name == "status"))
        assert queried_statuses == set(approvals_mod._APPROVAL_STATUSES)
        _assert_query_only(mock_approvals)

    def test_pending_listing_queries_pending_partition(self):
        """get_pending_approvals: single Query on the pending partition; no Scan."""
        from handlers import approvals as approvals_mod

        mock_approvals = MagicMock()
        mock_approvals.query.return_value = {"Items": [self._pending_item()]}

        with patch.object(approvals_mod, "approvals_table", mock_approvals):
            result = approvals_mod.get_pending_approvals()

        assert result["statusCode"] == 200
        assert json.loads(result["body"])[0]["approval_id"] == "appr-1"
        calls = _query_calls(mock_approvals, "status-created-index")
        assert len(calls) == 1
        leaves = _leaves(calls[0]["KeyConditionExpression"])
        assert ("=", "status", ("pending",)) in leaves
        _assert_query_only(mock_approvals)


# ═══════════════════════════════════════════════════════════════════════════
# pipelines_runs.py
# ═══════════════════════════════════════════════════════════════════════════


class TestPipelinesRunsQueryNotScan:
    """Per-run approval lookups and the cancel_run recovery path query run_id GSIs."""

    RUN_ID = "run-qns-1"

    def test_get_run_details_approval_lookups_query_run_id_gsi(self):
        """get_run_details: all three per-run approval lookups (pending check in
        the running-step injection, the run-status pending check, and the
        approvals listing) Query the approvals run_id-index; nothing scans."""
        from handlers import pipelines_runs as runs_mod

        mock_state = MagicMock()
        mock_state.query.return_value = {
            "Items": [
                {
                    "run_id": self.RUN_ID,
                    "pipeline_id": "pipe-1",
                    "original_pipeline_id": "pipe-1",
                    "step_name": "schema_extract",
                    "step_sequence": 2,
                    "status": "success",
                    "timestamp": "2025-01-01T00:00:00Z",
                }
            ]
        }
        mock_exec = MagicMock()
        # Active execution for this run → triggers the pending-approval check.
        # Seeded on ``get_item``, not ``query``: p48 task 6.3 replaced the
        # running-step injection's ``ScanIndexForward=False, Limit=5`` query plus
        # Python ``run_id`` filter with a keyed ``get_item`` on
        # ``(pipeline_id, run_id)``. Both executions reads in ``get_run_details``
        # are now keyed, which is why this row also satisfies the ``step_logs`` /
        # lineage read below it — it carries neither ``step_logs`` nor lineage
        # attributes, so that read yields its empty answers as before.
        mock_exec.get_item.return_value = {
            "Item": {
                "pipeline_id": "pipe-1",
                "run_id": self.RUN_ID,
                "status": "RUNNING",
                "current_step": "schema_mapping",
                "updated_at": "2025-01-01T00:01:00Z",
            }
        }
        mock_approvals = MagicMock()
        mock_approvals.query.return_value = {
            "Items": [
                {
                    "approval_id": "appr-1",
                    "approval_type": "schema_mapping",
                    "status": "pending",
                    "run_id": self.RUN_ID,
                }
            ]
        }
        mock_pipelines = MagicMock()
        mock_pipelines.get_item.return_value = {}

        with (
            patch.object(runs_mod, "state_table", mock_state),
            patch.object(runs_mod, "executions_table", mock_exec),
            patch.object(runs_mod, "approvals_table", mock_approvals),
            patch.object(runs_mod, "pipelines_table", mock_pipelines),
            patch.object(runs_mod, "get_step_logs", return_value=[]),
        ):
            result = runs_mod.get_run_details(self.RUN_ID)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "WAITING_APPROVAL"

        approval_calls = _query_calls(mock_approvals, "run_id-index")
        assert len(approval_calls) == 3, (
            f"expected 3 run_id-index approval lookups, got {len(approval_calls)}: "
            f"{[c.kwargs for c in mock_approvals.query.call_args_list]}"
        )
        for kwargs in approval_calls:
            leaves = _leaves(kwargs["KeyConditionExpression"])
            assert ("=", "run_id", (self.RUN_ID,)) in leaves
        # The two pending checks filter server-side on status=pending
        pending_checks = [c for c in approval_calls if "FilterExpression" in c]
        assert len(pending_checks) == 2
        for kwargs in pending_checks:
            assert ("=", "status", ("pending",)) in _leaves(kwargs["FilterExpression"])

        _assert_query_only(mock_approvals, mock_state, mock_exec)

    def test_cancel_run_recovery_queries_executions_run_id_gsi(self):
        """cancel_run finds the run's execution row through the executions
        run_id-index (run_id is only the sort key) instead of scanning."""
        from handlers import pipelines_runs as runs_mod

        mock_state = MagicMock()
        mock_state.get_item.return_value = {}  # no run_pipeline row: no Glue job
        mock_exec = MagicMock()
        mock_exec.query.return_value = {"Items": [{"pipeline_id": "pipe-1", "run_id": self.RUN_ID}]}
        mock_exec.get_item.return_value = {
            "Item": {
                "pipeline_id": "pipe-1",
                "run_id": self.RUN_ID,
                "status": "RUNNING",
                "execution_id": "arn:aws:states:us-east-1:123456789012:execution:sm:file-x",
            }
        }
        mock_client = MagicMock()
        mock_client.describe_execution.return_value = {"status": "RUNNING"}

        # The no-Glue cancel also writes its fence through ``dynamodb.Table(...)``;
        # patched so the test never depends on which file imported handlers.common.
        mock_dynamo = MagicMock()
        mock_dynamo.Table.return_value = MagicMock(name="task_tokens_table")

        with (
            patch.object(runs_mod, "state_table", mock_state),
            patch.object(runs_mod, "executions_table", mock_exec),
            patch.object(runs_mod, "dynamodb", mock_dynamo),
            patch.object(runs_mod, "write_audit_entry"),
            patch.object(runs_mod.boto3, "client", return_value=mock_client),
        ):
            result = runs_mod.cancel_run(self.RUN_ID)

        assert result["statusCode"] == 200
        calls = _query_calls(mock_exec, "run_id-index")
        assert len(calls) == 1
        leaves = _leaves(calls[0]["KeyConditionExpression"])
        assert ("=", "run_id", (self.RUN_ID,)) in leaves
        # The recovered key pair is used for the status update
        mock_exec.update_item.assert_called_once()
        assert mock_exec.update_item.call_args.kwargs["Key"] == {
            "pipeline_id": "pipe-1",
            "run_id": self.RUN_ID,
        }
        _assert_query_only(mock_exec, mock_state)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
