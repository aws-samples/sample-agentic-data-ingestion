# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test: verify AgentTracePlugin writes trace records to DynamoDB.

Uses moto mock_aws to simulate DynamoDB and verifies that the plugin's
flush() method correctly persists trace data after simulated hook events.
"""

from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))


@pytest.fixture
def trace_table(monkeypatch):
    """Create the agent_traces DynamoDB table under moto and set env vars."""
    monkeypatch.setenv("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName="agentic_data_ingestion_agent_traces",
            KeySchema=[
                {"AttributeName": "run_id", "KeyType": "HASH"},
                {"AttributeName": "step_name", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "step_name", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = dynamodb.Table("agentic_data_ingestion_agent_traces")
        table.wait_until_exists()

        # Reset the module-level cached table so it picks up the mock
        import agent_trace_plugin as atp

        atp._traces_table = None
        atp._dynamodb = dynamodb

        yield table

        # Reset after test
        atp._traces_table = None


class TestAgentTraceIntegration:
    """Verify trace records are written to DynamoDB after plugin flush.

    NOTE: the ``model_id`` values used in these tests are illustrative sample
    trace data, not a primary-model selection. The canonical primary model is
    ``us.anthropic.claude-sonnet-4-6`` (P21 Req 1); these tests intentionally use
    historical Claude Sonnet 4.x IDs to exercise trace round-tripping through
    DynamoDB.
    """

    def test_flush_writes_trace_record(self, trace_table):
        """After populating plugin state and calling flush(),
        a trace record should appear in DynamoDB with correct keys."""
        from agent_trace_plugin import AgentTracePlugin

        plugin = AgentTracePlugin(
            pipeline_id="pipe-integration-001",
            run_id="run-integration-001",
            step_name="schema_mapping",
        )

        plugin._start_time = time.time() - 2.0
        plugin._end_time = time.time()
        plugin._model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
        plugin._total_input_tokens = 500
        plugin._total_output_tokens = 150

        plugin.flush()

        response = trace_table.get_item(Key={"run_id": "run-integration-001", "step_name": "schema_mapping"})
        assert "Item" in response, "Trace record not found in DynamoDB"

        item = response["Item"]
        assert item["pipeline_id"] == "pipe-integration-001"
        assert item["run_id"] == "run-integration-001"
        assert item["step_name"] == "schema_mapping"
        assert "trace_data" in item
        assert "timestamp" in item
        assert "ttl" in item

        trace_data = item["trace_data"]
        assert trace_data["model_id"] == "us.anthropic.claude-sonnet-4-20250514-v1:0"
        assert trace_data["total_input_tokens"] == 500
        assert trace_data["total_output_tokens"] == 150

    def test_flush_writes_with_model_and_tool_calls(self, trace_table):
        """Trace record should include model_calls and tool_calls arrays."""
        from agent_trace_plugin import AgentTracePlugin

        plugin = AgentTracePlugin(
            pipeline_id="pipe-integration-002",
            run_id="run-integration-002",
            step_name="code_generation",
        )

        plugin._start_time = time.time() - 5.0
        plugin._end_time = time.time()
        plugin._model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
        plugin._total_input_tokens = 1200
        plugin._total_output_tokens = 400

        plugin._model_calls.append(
            {
                "model_id": "us.anthropic.claude-sonnet-4-20250514-v1:0",
                "duration_ms": 2341,
                "response_preview": "df_transformed = df.select(...)",
                "input_tokens": 1200,
                "output_tokens": 400,
                "cache_read_tokens": 0,
            }
        )

        plugin._tool_calls.append(
            {
                "tool_name": "create_glue_job",
                "tool_params": '{"job_name": "test-job"}',
                "duration_ms": 1200,
                "status": "success",
                "result_preview": '{"job_name": "test-job"}',
            }
        )

        plugin.flush()

        response = trace_table.get_item(Key={"run_id": "run-integration-002", "step_name": "code_generation"})
        assert "Item" in response

        trace_data = response["Item"]["trace_data"]
        assert len(trace_data["model_calls"]) == 1
        assert len(trace_data["tool_calls"]) == 1
        assert trace_data["model_calls"][0]["model_id"] == "us.anthropic.claude-sonnet-4-20250514-v1:0"
        assert trace_data["tool_calls"][0]["tool_name"] == "create_glue_job"

    def test_flush_calculates_estimated_cost(self, trace_table):
        """Estimated cost should be calculated from token counts."""
        from agent_trace_plugin import AgentTracePlugin

        plugin = AgentTracePlugin(
            pipeline_id="pipe-integration-003",
            run_id="run-integration-003",
            step_name="code_testing",
        )

        plugin._start_time = time.time() - 1.0
        plugin._end_time = time.time()
        plugin._model_id = "claude-sonnet-4-5"
        plugin._total_input_tokens = 1000
        plugin._total_output_tokens = 1000

        plugin.flush()

        response = trace_table.get_item(Key={"run_id": "run-integration-003", "step_name": "code_testing"})
        assert "Item" in response

        trace_data = response["Item"]["trace_data"]
        # Cost = (1000/1000 * 0.003) + (1000/1000 * 0.015) = 0.018
        cost = float(trace_data["estimated_cost_usd"])
        assert cost > 0, "Estimated cost should be positive"
        assert abs(cost - 0.018) < 0.001, f"Expected ~$0.018, got ${cost}"


# ---------------------------------------------------------------------------
# Sample-value redaction at the choke point (p52 task 4.1, C5)
# ---------------------------------------------------------------------------

# Distinctive fragments, so a leak is unmistakable in an assertion message
# rather than something a reader has to spot inside a plausible address.
_HOOK_SECRET_A = "123 Main St APT-4-HOOK-LEAK-A"
_HOOK_SECRET_B = "456 Second Ave HOOK-LEAK-B"
_HOOK_SECRET_CITY = "Seattle-HOOK-LEAK-CITY"
_HOOK_SECRET_ECHO = "789 Echoed Way HOOK-LEAK-ECHO"
_MANUAL_SECRET_A = "12 Manual Row MANUAL-LEAK-A"
_MANUAL_SECRET_B = "34 Manual Row MANUAL-LEAK-B"

# The complex-mapping-agent render, verbatim in the shape
# ``build_evidence_prompt`` emits: ``f"  - {name} (type: {ftype})"`` (main.py:289)
# then ``f"\n    Sample values: [{sample_str}]"`` (main.py:292).  This is the
# shape that reaches a Preview_Field through the hook path.
_HOOK_PROMPT = (
    "Resolve these ambiguous field mappings using the sample data.\n"
    "\n"
    "Source fields:\n"
    f'  - addr_1 (type: string)\n    Sample values: ["{_HOOK_SECRET_A}", "{_HOOK_SECRET_B}"]\n'
    f'  - city (type: string)\n    Sample values: ["{_HOOK_SECRET_CITY}"]\n'
    "\n"
    "Target fields:\n"
    "  - street_address (type: string)\n"
)

# What a model may echo back.  Marker-keyed, so the choke point catches it in
# ``response_preview`` too — one of C5's reasons for redacting all four
# Preview_Fields rather than only ``prompt_preview``.
_HOOK_RESPONSE = f'Mapping addr_1 → street_address. The Sample values: ["{_HOOK_SECRET_ECHO}"] confirm a street.'

# The schema_mapping render, verbatim in the shape
# ``render_sample_values_section`` emits:
# ``f'  - {field}: Sample values: [{rendered}]'``
# (lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1013).
# Note the two producers' shapes differ — one line per field here, a ``\n    ``
# continuation on the hook path — which is why one flush() over both is the
# thing worth asserting.
_MANUAL_PROMPT = (
    "Map the source schema onto the target schema.\n"
    "\n"
    "Sample values from the source data:\n"
    f'  - addr_1: Sample values: ["{_MANUAL_SECRET_A}", "{_MANUAL_SECRET_B}"]\n'
)

_ALL_SECRETS = (
    _HOOK_SECRET_A,
    _HOOK_SECRET_B,
    _HOOK_SECRET_CITY,
    _HOOK_SECRET_ECHO,
    _MANUAL_SECRET_A,
    _MANUAL_SECRET_B,
)


class TestBothWritersRedactedThroughOneFlush:
    """Both Preview_Field writers, one plugin, one ``flush()`` — R5.2's claim.

    This is the only test that makes the **choke-point** argument checkable, and
    that is the whole reason it exists separately from the unit coverage in
    ``tests/unit/shared/test_trace_sample_redaction*.py``.  R5.2 says the
    redactor is applied at ``flush()`` "so that both Preview_Field writers are
    covered by one call site", and the two writers are structurally different:

    * the **hook path** — ``AgentTracePlugin._on_after_model`` appends a model
      call.  Grep-verified: the plugin is registered as a hook at **4** sites
      (``test_code:3275``, ``create_code/transform_generation.py:166``,
      ``create_code:1126``, ``complex_mapping_agent:525``); and
    * the **manual-append path** —
      ``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:2125``
      appends its dict to ``trace_plugin._model_calls`` **by hand**, because its
      Converse call is not a strands agent and no hook fires for it.  It then
      calls ``flush()`` at ``:2138``.

    Those two are the *whole* set: grep for ``_model_calls``/``_tool_calls``
    across ``lambda/``, ``agentcore/`` and ``mcp/`` finds exactly one writer
    outside the plugin itself — the ``:2125`` append above.  Everything else
    arrives through the hooks.  So a test that covers both writers covers every
    way a Preview_Field can be populated today.

    ``design.md`` do-NOT 9 turns on exactly this: a redactor placed at
    ``_on_after_model`` instead of at ``flush()`` would miss the manual writer
    entirely.  So the case below drives **both** writers into the **same**
    plugin instance and calls ``flush()`` **once**.  Two separate tests, or two
    plugins, would not test the claim.

    Every assertion reads the item back **from the moto-backed table**, never
    from the plugin's in-memory ``_model_calls``.  That is the difference between
    this and a unit test: ``flush()`` serialises through
    ``json.loads(json.dumps(...), parse_float=Decimal)`` before ``put_item``,
    and only a persisted-item assertion catches a serialisation step that
    reintroduces the raw text.

    Not asserted here, deliberately: TTL/retention.  ``_TTL_SECONDS`` and the
    ``ttl`` attribute are already correct and already covered
    (``test_flush_writes_trace_record``); re-asserting them would blur what this
    test is for.

    Gate: ``make test-scope DIR=tests/integration/``.  Risk N4 — ``make
    test-fast`` runs ``tests/unit/`` and ``tests/contracts/`` only
    (``Makefile:85-86``) and ``pyproject.toml:113``'s ``addopts`` carries
    ``-m 'not e2e and not integration'``, so nothing in this file is visible to
    any tier gate.  A green ``make test-fast`` is not evidence about this file.
    """

    def test_hook_and_manual_writers_are_both_redacted_by_one_flush(self, trace_table):
        """One plugin, two writers, one flush: no value persists, both counts do."""
        from agent_trace_plugin import (
            _PREVIEW_FIELDS,
            _REDACTION_FAILED,
            _SAMPLE_MARKER,
            AgentTracePlugin,
        )

        plugin = AgentTracePlugin(
            pipeline_id="pipe-integration-redaction",
            run_id="run-integration-redaction",
            step_name="schema_mapping",
        )
        plugin._start_time = time.time() - 3.0

        # -- Writer 1: the hook path, driven through the real callbacks --------
        # _on_before_model extracts the prompt from event.agent.messages, so the
        # Sample_Block reaches _current_prompt_text the way it does in
        # production rather than by being assigned onto the plugin.
        before_event = SimpleNamespace(
            agent=SimpleNamespace(messages=[{"role": "user", "content": [{"text": _HOOK_PROMPT}]}])
        )
        plugin._on_before_model(before_event)

        after_event = MagicMock()
        after_event.exception = None
        after_event.agent = SimpleNamespace(model=SimpleNamespace(config={"model_id": "hook-path-model"}))
        after_event.stop_response = SimpleNamespace(
            message={"content": [{"text": _HOOK_RESPONSE}]},
            usage={"inputTokens": 900, "outputTokens": 120, "cacheReadInputTokens": 0},
        )
        plugin._on_after_model(after_event)

        # -- Writer 2: the manual append, mirroring schema_mapping:2125-2137 ---
        plugin._end_time = time.time()
        plugin._total_input_tokens = 1500
        plugin._total_output_tokens = 300
        plugin._model_calls.append(
            {
                "model_id": "us.manual-append-model",
                "input_tokens": 600,
                "output_tokens": 180,
                "cache_read_tokens": 0,
                "start_time": plugin._start_time,
                "end_time": plugin._end_time,
                "duration_ms": 2100,
                "prompt_preview": _MANUAL_PROMPT[:10000],
                "response_preview": '{"mappings": [{"target_field": "street_address"}]}',
            }
        )

        # -- One flush() over both -------------------------------------------
        plugin.flush()

        response = trace_table.get_item(Key={"run_id": "run-integration-redaction", "step_name": "schema_mapping"})
        assert "Item" in response, "Trace record not found in DynamoDB"
        item = response["Item"]

        # 1. Both entries survived.  A redactor that dropped one writer's entry
        #    — or a per-field raise that cost the whole row — fails here.
        model_calls = item["trace_data"]["model_calls"]
        assert len(model_calls) == 2, (
            f"both writers must persist through one flush(); got {len(model_calls)} entry/entries: "
            f"{[c.get('model_id') for c in model_calls]}"
        )
        by_model = {call["model_id"]: call for call in model_calls}
        assert set(by_model) == {"hook-path-model", "us.manual-append-model"}, (
            f"expected one entry per writer, got {sorted(by_model)}"
        )
        hook_call = by_model["hook-path-model"]
        manual_call = by_model["us.manual-append-model"]

        # 2. No sampled value survives anywhere in the PERSISTED item, from
        #    either writer.  Scanned over the whole item rather than field by
        #    field, so a serialisation step that reintroduces raw text is caught
        #    wherever it puts it.
        persisted = json.dumps(item, default=str)
        leaked = [secret for secret in _ALL_SECRETS if secret in persisted]
        assert not leaked, f"sampled value(s) persisted in the trace item: {leaked}"

        # 3. No field degraded to the failure placeholder — the counts below are
        #    real redaction output, not a withheld preview.
        assert _REDACTION_FAILED not in persisted, (
            "a Preview_Field degraded to the redaction-failure placeholder; the count assertions below would be vacuous"
        )

        # 4. The count survives for BOTH entries (R6.1, R6.2), and so do the
        #    marker (R6.4) and the field name (R6.3).  Values here carry no
        #    embedded quote and no newline, so the span is balanced and
        #    _redact_sample_values yields an exact count rather than its
        #    "1+ values redacted, exact count unavailable: …" form.
        hook_prompt = hook_call["prompt_preview"]
        assert f"{_SAMPLE_MARKER}2 values redacted]" in hook_prompt, (
            f"hook-path addr_1 count missing from persisted prompt_preview: {hook_prompt!r}"
        )
        assert f"{_SAMPLE_MARKER}1 values redacted]" in hook_prompt, (
            f"hook-path city count missing from persisted prompt_preview: {hook_prompt!r}"
        )
        assert "  - addr_1 (type: string)" in hook_prompt, (
            f"hook-path field name did not survive redaction: {hook_prompt!r}"
        )

        manual_prompt = manual_call["prompt_preview"]
        assert f"{_SAMPLE_MARKER}2 values redacted]" in manual_prompt, (
            f"manual-append count missing from persisted prompt_preview: {manual_prompt!r}"
        )
        assert f"  - addr_1: {_SAMPLE_MARKER}" in manual_prompt, (
            f"manual-append field name did not survive redaction: {manual_prompt!r}"
        )

        # 5. The choke point covers the other Preview_Fields on the same entry,
        #    not just prompt_preview: the model echoed a marked block back and
        #    it is redacted with its count in the persisted response_preview.
        hook_response = hook_call["response_preview"]
        assert f"{_SAMPLE_MARKER}1 values redacted]" in hook_response, (
            f"echoed Sample_Block not redacted in persisted response_preview: {hook_response!r}"
        )

        # 6. Redaction is inert where there is no marker (R5.6) — the manual
        #    writer's response carried none and is persisted unchanged.
        assert manual_call["response_preview"] == '{"mappings": [{"target_field": "street_address"}]}', (
            "an unmarked Preview_Field must persist unchanged"
        )

        # 7. Every Preview_Field this test relies on is one flush() actually
        #    walks, so the assertions above cannot be satisfied by a field the
        #    redaction loop skips.
        assert {"prompt_preview", "response_preview"} <= set(_PREVIEW_FIELDS)
