# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Data quality agent report-safety tests (P28 task 10.3, R11.3/R11.4).

Covers the three report-safety rules:

- Sample failing rows (raw data / PII) are offloaded to
  ``s3://<PROCESSED_BUCKET>/dq-reports/<run_id>/samples.json`` — the
  DynamoDB state item carries only the S3 URI reference and per-check
  ``sample_count``, never raw rows.
- The DynamoDB report item is size-bounded: an oversized report degrades
  to a summary item (serialized < 100KB) with the full report preserved in
  S3, so a 400KB overflow can no longer lose the whole write.
- The row-count check is skipped (status ``skipped``) when the payload
  carries no expected count — it must not be evaluated against a
  fabricated expected=0. An explicit 0 remains a real expectation.

**Validates: Requirements 11.3, 11.4, 11.5**
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "data_quality_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

# ── Import the agent with a PASSTHROUGH @app.entrypoint ─────────────
#
# test_data_quality.py imports the module with BedrockAgentCoreApp fully
# mocked, which turns `handler` into a MagicMock. These tests exercise the
# real handler, so this file loads its own copy of the module (unique
# sys.modules name) with `entrypoint` behaving as the identity decorator.

_mock_boto3 = MagicMock()
_mock_runtime = MagicMock()
_mock_runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn
_mock_bedrock_agentcore = MagicMock()
_mock_bedrock_agentcore.runtime = _mock_runtime

sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": _mock_bedrock_agentcore,
        "bedrock_agentcore.runtime": _mock_runtime,
        "boto3": _mock_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
    },
):
    _spec = importlib.util.spec_from_file_location("dqa_bounds_main", MAIN_PATH)
    dqa = importlib.util.module_from_spec(_spec)
    sys.modules["dqa_bounds_main"] = dqa
    _spec.loader.exec_module(dqa)


_ENV = {
    "PROCESSED_BUCKET": "adi-processed-files-test",
    "STATE_TABLE": "agentic_data_ingestion_state",
}


def _fake_require_env(name, default=None):
    return _ENV.get(name, default or "unset")


def _wire(monkeypatch, athena_router):
    """Wire per-test mocks; returns (state_table, s3_client)."""
    state_table = MagicMock()
    dynamo = MagicMock()
    dynamo.Table.return_value = state_table
    s3 = MagicMock()
    monkeypatch.setattr(dqa, "_dynamodb", dynamo)
    monkeypatch.setattr(dqa, "_s3_client", s3)
    monkeypatch.setattr(dqa, "_require_env", _fake_require_env)
    monkeypatch.setattr(dqa, "run_athena_query", athena_router)
    return state_table, s3


def _call_handler(payload):
    """Invoke the real handler with in-handler imports mocked."""
    with patch.dict(
        "sys.modules",
        {
            "agent_trace_plugin": MagicMock(),
            "audit_utils": MagicMock(),
            "env_utils": MagicMock(),
        },
    ):
        return dqa.handler(payload)


# ── R11.4: row-count check skipped without an expectation ───────────


class TestRowCountExpectationAbsent:
    def test_none_expectation_is_skipped_and_queries_nothing(self, monkeypatch):
        def _no_query(query, database, execution_parameters=None):  # pragma: no cover - guard
            raise AssertionError(f"row_count must not query Athena when skipped: {query}")

        monkeypatch.setattr(dqa, "run_athena_query", _no_query)
        result = dqa.check_row_count("db", "tbl", None)
        assert result["status"] == "skipped"
        assert result["name"] == "row_count"
        assert "message" in result

    def test_explicit_zero_expectation_is_still_evaluated(self, monkeypatch):
        monkeypatch.setattr(dqa, "run_athena_query", lambda q, d: [{"row_count": "5"}])
        result = dqa.check_row_count("db", "tbl", 0)
        assert result["status"] == "fail"
        assert result["expected"] == 0
        assert result["actual"] == 5

    def test_skipped_check_does_not_fail_report_or_recommend(self):
        checks = [{"name": "row_count", "status": "skipped", "message": "no expectation"}]
        report = dqa.generate_report("pipe-1", "run-1", checks)
        assert report["overall_status"] == "pass"
        assert report["root_cause"] is None
        assert report["recommendations"] == []


# ── R11.3: sample extraction (raw rows must not reach DynamoDB) ─────


class TestExtractSamples:
    def test_strips_raw_rows_and_records_counts(self):
        checks = [
            {"name": "null_rate_email", "status": "warning", "sample_nulls": ["{'ssn': '1'}", "{'ssn': '2'}"]},
            {"name": "duplicate_keys", "status": "fail", "duplicates": 1, "sample_duplicates": [{"id": "7"}]},
            {"name": "row_count", "status": "pass"},
        ]
        samples = dqa._extract_samples(checks)

        assert samples == {
            "null_rate_email": {"sample_nulls": ["{'ssn': '1'}", "{'ssn': '2'}"]},
            "duplicate_keys": {"sample_duplicates": [{"id": "7"}]},
        }
        serialized = json.dumps(checks)
        assert "sample_nulls" not in serialized
        assert "sample_duplicates" not in serialized
        assert checks[0]["sample_count"] == 2
        assert checks[1]["sample_count"] == 1
        assert "sample_count" not in checks[2]

    def test_empty_sample_lists_are_removed_without_counts(self):
        checks = [{"name": "duplicate_keys", "status": "pass", "sample_duplicates": []}]
        samples = dqa._extract_samples(checks)
        assert samples == {}
        assert "sample_duplicates" not in checks[0]
        assert "sample_count" not in checks[0]


# ── R11.3: bounded state item ───────────────────────────────────────


class TestBoundStateItem:
    def test_small_item_is_unchanged(self):
        item = {"run_id": "r", "checks": [{"name": "row_count", "status": "pass"}]}
        assert dqa._bound_state_item(item) is item

    def test_huge_report_is_bounded_under_100kb_with_s3_ref(self):
        checks = [{"name": f"null_rate_col_{i}", "status": "fail", "error": "x" * 10_000} for i in range(500)]
        item = {
            "run_id": "r",
            "step_name": "data_quality",
            "overall_status": "fail",
            "checks": checks,
            "recommendations": ["fix " * 1000] * 50,
        }
        bounded = dqa._bound_state_item(item, report_s3_uri="s3://b/dq-reports/r/report.json")

        assert dqa._serialized_size(bounded) < 100_000
        assert bounded["report_truncated"] is True
        assert bounded["report_s3_uri"] == "s3://b/dq-reports/r/report.json"
        assert bounded["checks_total"] == 500
        assert len(bounded["checks"]) <= dqa._MAX_SUMMARY_CHECKS
        # Core identity survives the degrade
        assert bounded["overall_status"] == "fail"
        assert bounded["run_id"] == "r"


# ── Handler wiring ──────────────────────────────────────────────────


def _pii_athena_router(query, database, execution_parameters=None):
    """Canned Athena responses producing null-rate + duplicate samples."""
    if "information_schema" in query:
        return [{"column_name": "email"}]
    if "AS row_count" in query:
        return [{"row_count": "100"}]
    if "SUM(CASE WHEN" in query:  # null-rate aggregate
        return [{"total_rows": "100", "null_rate_email": "0.5"}]
    if "IS NULL" in query:  # null sample fetch (raw rows — PII)
        return [{"email": "", "name": "Jane Doe", "ssn": "123-45-6789"}]
    if "dup_count" in query:
        return [{"dup_count": "2"}]
    if "HAVING COUNT(*) > 1" in query:  # duplicate samples
        return [{"customer_id": "1001", "cnt": "2"}]
    raise AssertionError(f"unexpected query: {query}")


_PII_PAYLOAD = {
    "run_id": "run-pii-1",
    "pipeline_id": "pipe-1",
    "target_table_info": {"database": "db", "table": "tbl"},
    "source_row_count": 100,
    "primary_key_columns": ["customer_id"],
}


class TestHandlerSamplesToS3:
    @pytest.fixture()
    def invoked(self, monkeypatch):
        state_table, s3 = _wire(monkeypatch, _pii_athena_router)
        report = _call_handler(dict(_PII_PAYLOAD))
        return state_table, s3, report

    def test_samples_written_to_s3_under_dq_reports_prefix(self, invoked):
        _, s3, _ = invoked
        assert s3.put_object.call_count == 1
        kwargs = s3.put_object.call_args.kwargs
        assert kwargs["Bucket"] == "adi-processed-files-test"
        assert kwargs["Key"] == "dq-reports/run-pii-1/samples.json"
        body = json.loads(kwargs["Body"].decode("utf-8"))
        assert "123-45-6789" in json.dumps(body["null_rate_email"]["sample_nulls"])
        assert body["duplicate_keys"]["sample_duplicates"] == [{"customer_id": "1001", "cnt": "2"}]

    def test_dynamodb_item_has_ref_and_no_raw_rows(self, invoked):
        state_table, _, _ = invoked
        item = state_table.put_item.call_args.kwargs["Item"]
        serialized = json.dumps(item, default=str)
        assert "sample_nulls" not in serialized
        assert "sample_duplicates" not in serialized
        assert "123-45-6789" not in serialized

        expected_uri = "s3://adi-processed-files-test/dq-reports/run-pii-1/samples.json"
        assert item["samples_s3_uri"] == expected_uri
        by_name = {c["name"]: c for c in item["checks"]}
        assert by_name["null_rate_email"]["sample_count"] == 1
        assert by_name["null_rate_email"]["samples_s3_uri"] == expected_uri
        assert by_name["duplicate_keys"]["sample_count"] == 1

    def test_returned_report_carries_the_reference(self, invoked):
        _, _, report = invoked
        assert report["samples_s3_uri"].endswith("dq-reports/run-pii-1/samples.json")

    def test_s3_failure_drops_samples_but_still_writes_report(self, monkeypatch):
        state_table, s3 = _wire(monkeypatch, _pii_athena_router)
        s3.put_object.side_effect = RuntimeError("s3 down")
        report = _call_handler(dict(_PII_PAYLOAD))

        state_table.put_item.assert_called_once()
        item = state_table.put_item.call_args.kwargs["Item"]
        serialized = json.dumps(item, default=str)
        assert "123-45-6789" not in serialized  # PII never falls back inline
        assert "samples_s3_uri" not in item
        assert report["overall_status"] == "fail"  # duplicates check failed


def _huge_athena_router(query, database, execution_parameters=None):
    """1200 columns of passing null-rate checks — a >100KB metadata report."""
    n_cols = 1200
    if "information_schema" in query:
        return [{"column_name": f"col_{i:04d}"} for i in range(n_cols)]
    if "AS row_count" in query:
        return [{"row_count": "100"}]
    if "SUM(CASE WHEN" in query:
        row = {"total_rows": "100"}
        row.update({f"null_rate_col_{i:04d}": "0.0" for i in range(n_cols)})
        return [row]
    if "dup_count" in query:
        return [{"dup_count": "0"}]
    raise AssertionError(f"unexpected query: {query}")


class TestHandlerBoundedItem:
    def test_huge_report_writes_bounded_item_and_full_report_to_s3(self, monkeypatch):
        state_table, s3 = _wire(monkeypatch, _huge_athena_router)
        payload = {
            "run_id": "run-huge-1",
            "pipeline_id": "pipe-1",
            "target_table_info": {"database": "db", "table": "tbl"},
            "source_row_count": 100,
            "primary_key_columns": ["col_0000"],
        }
        report = _call_handler(payload)

        # The write succeeded and the stored item is bounded.
        state_table.put_item.assert_called_once()
        item = state_table.put_item.call_args.kwargs["Item"]
        assert len(json.dumps(item, default=str).encode("utf-8")) < 100_000
        assert item["report_truncated"] is True
        assert item["checks_total"] == len(report["checks"])
        assert item["overall_status"] == "pass"

        # S3 carries the full-fidelity report.
        assert s3.put_object.call_count == 1
        kwargs = s3.put_object.call_args.kwargs
        assert kwargs["Key"] == "dq-reports/run-huge-1/report.json"
        full = json.loads(kwargs["Body"].decode("utf-8"))
        assert len(full["checks"]) == len(report["checks"])
        assert item["report_s3_uri"] == "s3://adi-processed-files-test/dq-reports/run-huge-1/report.json"


class TestHandlerRowCountSkip:
    def test_payload_without_expectation_skips_row_count(self, monkeypatch):
        def _router(query, database, execution_parameters=None):
            if "information_schema" in query:
                return []  # no columns → no null-rate/freshness queries
            raise AssertionError(f"unexpected query: {query}")

        state_table, _ = _wire(monkeypatch, _router)
        payload = {
            "run_id": "run-noexp-1",
            "pipeline_id": "pipe-1",
            "target_table_info": {"database": "db", "table": "tbl"},
            # no source_row_count
        }
        report = _call_handler(payload)

        row_check = next(c for c in report["checks"] if c["name"] == "row_count")
        assert row_check["status"] == "skipped"
        assert report["overall_status"] == "pass"
        assert report["recommendations"] == []

        item = state_table.put_item.call_args.kwargs["Item"]
        stored_row_check = next(c for c in item["checks"] if c["name"] == "row_count")
        assert stored_row_check["status"] == "skipped"
