# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for NL pipeline builder: type inference, format detection, config assembly, JSON parsing.

Also pins threat-model T-05: the agent only proposes (a ``confirm`` payload
writes no pipeline) and source discovery reads only ``SOURCE_BUCKET``.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "nl_pipeline_builder")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

_mock_s3 = MagicMock()
_mock_boto3 = MagicMock()
_mock_boto3.client.return_value = _mock_s3
_mock_boto3.resource.return_value = MagicMock()

with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": MagicMock(),
        "bedrock_agentcore.runtime": MagicMock(),
        "boto3": _mock_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
    },
):
    import main as nlpb_main

    nlpb_main._s3_client = _mock_s3


# ── _infer_type ─────────────────────────────────────────────────────


class TestInferType:
    def test_integers(self):
        assert nlpb_main._infer_type(["1", "2", "300"]) == "integer"

    def test_negative_integers(self):
        assert nlpb_main._infer_type(["-1", "0", "42"]) == "integer"

    def test_doubles(self):
        assert nlpb_main._infer_type(["1.5", "2.0", "3.14"]) == "double"

    def test_dates(self):
        assert nlpb_main._infer_type(["2025-01-01", "2025-06-15"]) == "date"

    def test_booleans(self):
        assert nlpb_main._infer_type(["true", "false", "True"]) == "boolean"

    def test_strings(self):
        assert nlpb_main._infer_type(["hello", "world"]) == "string"

    def test_empty_values_return_string(self):
        assert nlpb_main._infer_type(["", "None", "null"]) == "string"

    def test_mixed_types_return_string(self):
        assert nlpb_main._infer_type(["123", "abc", "456"]) == "string"


# ── _detect_format ──────────────────────────────────────────────────


class TestDetectFormat:
    def test_csv(self):
        assert nlpb_main._detect_format("data/customers.csv") == "csv"

    def test_json(self):
        assert nlpb_main._detect_format("data/events.json") == "json"

    def test_jsonl(self):
        assert nlpb_main._detect_format("logs/stream.jsonl") == "jsonl"

    def test_parquet(self):
        assert nlpb_main._detect_format("warehouse/table.parquet") == "parquet"

    def test_tsv(self):
        assert nlpb_main._detect_format("export/data.tsv") == "tsv"

    def test_unknown_defaults_to_csv(self):
        assert nlpb_main._detect_format("data/file.xyz") == "csv"

    def test_case_insensitive(self):
        assert nlpb_main._detect_format("DATA/FILE.CSV") == "csv"
        assert nlpb_main._detect_format("DATA/FILE.JSON") == "json"


# ── build_pipeline_config ───────────────────────────────────────────


class TestBuildPipelineConfig:
    def test_assembles_config(self):
        intent = {
            "pipeline_name": "customer-pipeline",
            "source_location": "s3://bucket/data/",
            "source_format": "csv",
            "target_type": "s3_tables",
            "table_name": "customers",
            "partitioning": ["region"],
            "custom_transforms": [],
            "description_summary": "Load customer data",
        }
        source_info = {
            "files_found": 3,
            "format": "csv",
            "representative_file": "s3://bucket/data/file1.csv",
        }
        target_schema = {"fields": [{"name": "id", "type": "integer"}]}
        mappings = [{"source": "id", "target": "id", "confidence": 0.95}]

        config = nlpb_main.build_pipeline_config(intent, source_info, target_schema, mappings)

        assert config["pipeline_name"] == "customer-pipeline"
        assert config["source"]["location"] == "s3://bucket/data/"
        assert config["source"]["format"] == "csv"
        assert config["target"]["type"] == "s3_tables"
        assert config["target"]["table_name"] == "customers"
        assert config["target"]["partitioning"] == ["region"]
        assert config["schema"] == target_schema
        assert len(config["mappings"]) == 1

    def test_defaults_for_missing_fields(self):
        config = nlpb_main.build_pipeline_config({}, {}, {"fields": []}, [])
        assert config["pipeline_name"] == "new-pipeline"
        assert config["target"]["type"] == "s3_tables"

    def test_idempotent_config(self):
        intent = {"pipeline_name": "test", "source_location": "s3://b/d/"}
        c1 = nlpb_main.build_pipeline_config(intent, {}, {"fields": []}, [])
        c2 = nlpb_main.build_pipeline_config(intent, {}, {"fields": []}, [])
        assert c1 == c2


# ── _parse_json_response ────────────────────────────────────────────


class TestParseJsonResponse:
    def test_plain_json(self):
        text = json.dumps({"key": "value"})
        result = nlpb_main._parse_json_response(text)
        assert result["key"] == "value"

    def test_json_with_code_fences(self):
        text = '```json\n{"key": "value"}\n```'
        result = nlpb_main._parse_json_response(text)
        assert result["key"] == "value"

    def test_generic_code_fences(self):
        text = '```\n{"key": "value"}\n```'
        result = nlpb_main._parse_json_response(text)
        assert result["key"] == "value"

    def test_json_embedded_in_text(self):
        text = 'Here is the result:\n{"key": "value"}'
        result = nlpb_main._parse_json_response(text)
        assert result["key"] == "value"

    def test_invalid_json_returns_fallback(self):
        result = nlpb_main._parse_json_response("not json", fallback={"default": True})
        assert result["default"] is True

    def test_no_fallback_returns_empty(self):
        result = nlpb_main._parse_json_response("not json")
        assert result == {}

    def test_idempotent_parsing(self):
        text = json.dumps({"a": 1, "b": 2})
        r1 = nlpb_main._parse_json_response(text)
        r2 = nlpb_main._parse_json_response(text)
        assert r1 == r2


# ── T-05: the real handler, under a private alias ───────────────────
#
# ``nlpb_main`` above is imported with BedrockAgentCoreApp fully mocked, which
# turns ``handler`` into a MagicMock. The T-05 tests exercise the real handler,
# so the module is loaded a second time under a private name with
# ``entrypoint`` as the identity decorator, and is not published to
# sys.modules (same pattern as
# tests/unit/agents/complex_mapping_agent/test_complex_mapping.py). Its boto3
# handles are swapped for moto-backed ones from the shared fixtures per test.

_MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

_mock_runtime = MagicMock()
_mock_runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn
_mock_bedrock_agentcore = MagicMock()
_mock_bedrock_agentcore.runtime = _mock_runtime

_spec = importlib.util.spec_from_file_location("_t05_nlpb_main_real_handler", _MAIN_PATH)
assert _spec is not None and _spec.loader is not None, f"cannot load {_MAIN_PATH}"
nlpb = importlib.util.module_from_spec(_spec)
with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": _mock_bedrock_agentcore,
        "bedrock_agentcore.runtime": _mock_runtime,
        "boto3": MagicMock(),
    },
):
    _spec.loader.exec_module(nlpb)

assert not isinstance(nlpb.handler, MagicMock), (
    "the passthrough @app.entrypoint did not take — nlpb.handler is still a MagicMock, so the "
    "T-05 handler tests below would exercise nothing"
)

#: Buckets and tables created by the shared moto fixtures.
SOURCE_BUCKET = "agentic-data-ingestion-source"
FOREIGN_BUCKET = "agentic-data-ingestion-processed"
PIPELINES_TABLE_NAME = "agentic_data_ingestion_pipelines"
STATE_TABLE_NAME = "agentic_data_ingestion_state"

_CSV_KEY = "customers/part-0000.csv"
_CSV_BODY = b"customer_id,region\n1,eu\n2,us\n"

#: What the (stubbed) intent parser returns. No source location, so the
#: handler skips discovery and the two LLM steps and still builds a proposal.
_INTENT = {
    "source_location": "",
    "source_format": "auto",
    "target_type": "s3_tables",
    "table_name": "customers",
    "pipeline_name": "customers-from-chat",
    "partitioning": [],
    "custom_transforms": [],
    "description_summary": "Load customers",
}

#: A proposal a client posts back with ``confirm``. Its name differs from
#: ``_INTENT``'s so the test can tell a fresh proposal from an echoed one.
_POSTED_PROPOSAL = {
    "pipeline_name": "posted-back-by-client",
    "source": {"type": "s3", "location": f"s3://{FOREIGN_BUCKET}/anything/", "format": "csv"},
    "target": {"type": "s3_tables", "table_name": "customers", "partitioning": []},
    "schema": {"fields": [{"name": "customer_id", "type": "string"}]},
    "mappings": [{"source": "customer_id", "target": "customer_id", "confidence": 1.0}],
}


@pytest.fixture
def agent_tables(mock_dynamodb_tables, mock_lambda_env, monkeypatch):
    """Point the agent at the moto tables from the shared fixture.

    ``_require_env`` is replaced by a plain env read so the table name comes
    from ``mock_lambda_env`` rather than from whatever ``env_utils`` another
    test module left in sys.modules.
    """
    monkeypatch.setattr(nlpb, "_dynamodb", boto3.resource("dynamodb", region_name="us-east-1"))
    monkeypatch.setattr(nlpb, "_require_env", lambda name, default=None: os.environ.get(name, default))
    return mock_dynamodb_tables


@pytest.fixture
def source_objects(mock_s3_buckets, monkeypatch):
    """The same CSV in the source bucket AND a foreign bucket, read through moto.

    The foreign copy is what makes a refusal observable without mocking S3: if
    discovery read the foreign bucket, it would find this file.
    """
    client = boto3.client("s3", region_name="us-east-1")
    for bucket in (SOURCE_BUCKET, FOREIGN_BUCKET):
        client.put_object(Bucket=bucket, Key=_CSV_KEY, Body=_CSV_BODY)
    monkeypatch.setattr(nlpb, "_s3_client", client)
    return client


def _run_handler(payload: dict) -> tuple[dict, MagicMock]:
    """Call the real handler; return its result and the audit-write mock.

    ``agent_trace_plugin`` and ``audit_utils`` are imported inside the handler,
    so their stubs must be in sys.modules at call time.
    """
    audit = MagicMock()
    with patch.dict("sys.modules", {"agent_trace_plugin": MagicMock(), "audit_utils": audit}):
        result = nlpb.handler(payload)
    return result, audit.write_agent_audit_entry


class TestConfirmNoLongerCreatesAPipeline:
    """T-05: the agent only proposes. Pipelines are created via ``POST /pipelines``."""

    @pytest.mark.parametrize("confirm", [True, "true", 1], ids=["bool-true", "string-true", "int-one"])
    def test_a_confirm_payload_writes_nothing_to_the_pipelines_table(self, agent_tables, monkeypatch, confirm):
        """The removed path, by its effect: a confirmed proposal is not saved.

        ``confirm: true`` with a proposal is exactly what used to reach the
        direct ``put_item`` on the pipelines table. Now it is ignored and the
        request is answered as an ordinary message.
        """
        monkeypatch.setattr(nlpb, "parse_intent", lambda description: dict(_INTENT))

        result, audit_write = _run_handler(
            {
                "message": "Yes, create it",
                "conversation_id": "conv-t05",
                "confirm": confirm,
                "proposal": dict(_POSTED_PROPOSAL),
            }
        )

        assert agent_tables[PIPELINES_TABLE_NAME].scan()["Items"] == [], (
            "the agent wrote to the pipelines table. Since T-05 only POST /pipelines creates pipelines."
        )
        assert result["status"] == "proposal"
        assert "pipeline_id" not in result and "pipeline_name" not in result
        assert result["proposal"]["pipeline_name"] == _INTENT["pipeline_name"], (
            "the answer must be a fresh proposal, not the client's posted-back one"
        )
        assert [c.kwargs["action"] for c in audit_write.call_args_list] == ["propose_pipeline"]

    def test_the_proposal_is_still_recorded_in_the_state_table(self, agent_tables, monkeypatch):
        """The proposal flow's own write is unchanged: one state row per answer."""
        monkeypatch.setattr(nlpb, "parse_intent", lambda description: dict(_INTENT))

        _run_handler({"message": "Load customers", "confirm": True, "proposal": dict(_POSTED_PROPOSAL)})

        rows = agent_tables[STATE_TABLE_NAME].scan()["Items"]
        assert len(rows) == 1
        assert rows[0]["step_name"] == "nl_pipeline_builder"
        assert rows[0]["proposal"]["pipeline_name"] == _INTENT["pipeline_name"]

    def test_the_agent_has_no_pipeline_write_function(self):
        """A fence on the deleted code, so it is not quietly restored."""
        assert not hasattr(nlpb, "create_pipeline_from_config")


class TestDiscoverSourceIsScopedToTheSourceBucket:
    """T-05: discovery reads only ``SOURCE_BUCKET`` and fails closed without it."""

    def test_the_source_bucket_is_read(self, source_objects, monkeypatch):
        monkeypatch.setenv("SOURCE_BUCKET", SOURCE_BUCKET)

        result = nlpb.discover_source(f"s3://{SOURCE_BUCKET}/customers/")

        assert "error" not in result
        assert result["files_found"] == 1
        assert result["representative_file"] == f"s3://{SOURCE_BUCKET}/{_CSV_KEY}"
        assert [f["name"] for f in result["schema"]["fields"]] == ["customer_id", "region"]

    @pytest.mark.parametrize(
        "uri",
        [
            pytest.param(f"s3://{FOREIGN_BUCKET}/customers/", id="another-bucket-holding-data"),
            pytest.param(f"s3://{SOURCE_BUCKET}-evil/customers/", id="lookalike-prefix"),
            pytest.param(f"s3://x{SOURCE_BUCKET}/customers/", id="lookalike-suffix"),
        ],
    )
    def test_a_foreign_bucket_is_refused(self, source_objects, monkeypatch, uri):
        """The foreign bucket holds the same file, so ``files_found == 0`` means it was not read."""
        monkeypatch.setenv("SOURCE_BUCKET", SOURCE_BUCKET)

        result = nlpb.discover_source(uri)

        assert result["files_found"] == 0
        assert "file_list" not in result
        assert result["schema"] == {"fields": []}
        assert "outside the source bucket" in result["error"]

    @pytest.mark.parametrize("value", [None, "", "   "], ids=["unset", "empty", "whitespace"])
    def test_no_configured_source_bucket_refuses_every_read(self, source_objects, monkeypatch, value):
        """Fail closed: without ``SOURCE_BUCKET`` even the real source bucket is not read."""
        if value is None:
            monkeypatch.delenv("SOURCE_BUCKET", raising=False)
        else:
            monkeypatch.setenv("SOURCE_BUCKET", value)

        result = nlpb.discover_source(f"s3://{SOURCE_BUCKET}/customers/")

        assert result["files_found"] == 0
        assert "file_list" not in result
        assert "SOURCE_BUCKET" in result["error"]

    def test_the_refusal_is_shown_in_the_reasoning_steps(self, agent_tables, source_objects, monkeypatch):
        """End to end: the user sees why nothing was discovered."""
        monkeypatch.setenv("SOURCE_BUCKET", SOURCE_BUCKET)
        intent = dict(_INTENT, source_location=f"s3://{FOREIGN_BUCKET}/customers/")
        monkeypatch.setattr(nlpb, "parse_intent", lambda description: intent)

        result, _ = _run_handler({"message": "Load customers from the processed bucket"})

        refused = [s for s in result["reasoning_steps"] if s.startswith("Source discovery refused:")]
        assert len(refused) == 1, result["reasoning_steps"]
        assert result["proposal"]["source"]["files_found"] == 0
