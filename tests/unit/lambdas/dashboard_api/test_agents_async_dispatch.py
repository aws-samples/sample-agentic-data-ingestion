# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: handlers/agents.py dispatches agents asynchronously (p30a R2).

The on-demand agent endpoints used to start a ``threading.Thread`` doing the
long synchronous AgentCore call and then return 202 "triggered". Lambda freezes
the execution environment the moment the handler returns, so the thread was
killed mid-call and the work was silently lost — a 202 that meant nothing. The
fixed behaviour, mirroring ``job_completion`` -> ``SELF_HEALING_FN``:

    POST cost apply (and, until 2026-10-11, POST quality-report)
      → lambda.invoke(FunctionName=AGENT_DISPATCH_FN,
                      InvocationType="Event",
                      Payload={agent_endpoint_env, step_name, payload})
        → the dispatch Lambda owns the long synchronous AgentCore call

``agent_endpoint_env`` carries the NAME of an env var, not a URL: the dispatch
Lambda resolves it in its own process from the same CDK props the dashboard API
reads (compute-stack.ts).

These handlers had NO test coverage before this file (codegraph flagged both as
uncovered), so every expectation below is derived from the requirements rather
than from the previous output — the previous output was the bug.

Validates: Requirements 2.1, 2.2, 2.3, 2.5 (p30a); Requirement 1.4 handler side

Note: the repo-wide "no threading.Thread in lambda/**" property test is a
separate task (R2.4). The source check here is scoped to this one module and
asserts R2.1 for these two handlers only.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — handlers/common.py reads its table names at import time
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, LAMBDA_DIR)
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

from handlers import agents

PIPELINE_ID = "pipe-123"
RUN_ID = "run-2026-001"
RECOMMENDATION_ID = "agentic-data-ingestion-pipe-123:worker_downsize"
DISPATCH_FN = "agentic_data_ingestion_agent_dispatch"


def _parse(response: dict) -> tuple[int, dict]:
    """Return ``(status_code, parsed_body)`` from a create_response dict."""
    return response["statusCode"], json.loads(response["body"])


def _dispatch_event(mock_lambda_client: MagicMock) -> dict:
    """Return the dispatch event that was sent, asserting the invoke shape."""
    mock_lambda_client.invoke.assert_called_once()
    kwargs = mock_lambda_client.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == DISPATCH_FN
    assert kwargs["InvocationType"] == "Event"
    return json.loads(kwargs["Payload"])


@pytest.fixture
def agent_env(mock_lambda_env, monkeypatch):
    """Both agent endpoints and the dispatch function name configured.

    ``mock_lambda_env`` supplies the shared Lambda env baseline; the four vars
    these handlers actually branch on are not in it, so they are set here.
    ``AUTO_APPLY_ENABLED`` is deliberately left UNSET — that matches the
    deployed cost-optimization runtime (design §5 O4).
    """
    monkeypatch.setenv("DATA_QUALITY_AGENT_ENDPOINT", "https://bedrock-agentcore.us-west-2.amazonaws.com/dq")
    monkeypatch.setenv("COST_OPTIMIZATION_AGENT_ENDPOINT", "https://bedrock-agentcore.us-west-2.amazonaws.com/cost")
    monkeypatch.setenv("AGENT_DISPATCH_FN", DISPATCH_FN)
    monkeypatch.delenv("AUTO_APPLY_ENABLED", raising=False)


@pytest.fixture
def mock_lambda_client():
    """Patch ``boto3.client`` so the handler's lambda client is a mock."""
    client = MagicMock()
    with patch("boto3.client", return_value=client):
        yield client


# ---------------------------------------------------------------------------
# The on-demand quality check is disabled (2026-10-11)
# ---------------------------------------------------------------------------


class TestOnDemandQualityCheckIsDisabled:
    """POST .../quality-report dispatched the data quality agent with no target
    table, to a runtime with no Athena grant, so its only possible result was a
    failing report written over the inline Glue report on the same state-row
    key. The route and its handler are gone; the GET reader stays."""

    def test_the_handler_is_gone(self):
        assert not hasattr(agents, "trigger_quality_check")

    def test_a_post_reaches_no_handler_and_dispatches_nothing(self, agent_env, mock_lambda_client):
        import router

        response = router.pipelines_router(
            {"httpMethod": "POST", "path": f"/api/pipelines/{PIPELINE_ID}/runs/{RUN_ID}/quality-report"},
            f"{PIPELINE_ID}/runs/{RUN_ID}/quality-report",
        )

        assert response["statusCode"] == 404
        mock_lambda_client.invoke.assert_not_called()

    def test_the_report_reader_is_still_routed(self, agent_env):
        import router

        with patch("handlers.agents.get_quality_report", return_value={"statusCode": 200, "body": "{}"}) as reader:
            response = router.pipelines_router(
                {"httpMethod": "GET", "path": f"/api/pipelines/{PIPELINE_ID}/runs/{RUN_ID}/quality-report"},
                f"{PIPELINE_ID}/runs/{RUN_ID}/quality-report",
            )

        assert response["statusCode"] == 200
        reader.assert_called_once_with(PIPELINE_ID, RUN_ID)


# ---------------------------------------------------------------------------
# R1.4 handler side / R2.1 / R2.2 — apply_cost_recommendation
# ---------------------------------------------------------------------------


class TestApplyCostRecommendation:
    def test_passes_recommendation_id_through_the_dispatch_payload(self, agent_env, mock_lambda_client, monkeypatch):
        """R1.4: the id reaches the agent, so the agent can act on that one
        recommendation instead of rescanning the fleet."""
        monkeypatch.setenv("AUTO_APPLY_ENABLED", "true")

        response = agents.apply_cost_recommendation(RECOMMENDATION_ID)

        assert _dispatch_event(mock_lambda_client) == {
            "agent_endpoint_env": "COST_OPTIMIZATION_AGENT_ENDPOINT",
            "step_name": "cost_optimization",
            "payload": {"recommendation_id": RECOMMENDATION_ID, "action": "apply_recommendation"},
        }
        status, body = _parse(response)
        assert status == 202
        assert body["dispatched"] is True
        # Dispatched is not applied — the agent has not run yet.
        assert body["applied"] is False
        assert body["recommendation_id"] == RECOMMENDATION_ID

    def test_auto_apply_unset_says_so_and_does_not_dispatch(self, agent_env, mock_lambda_client):
        """R1.4: when AUTO_APPLY_ENABLED is not 'true' the response says so
        rather than reporting success, and no agent run is started."""
        status, body = _parse(agents.apply_cost_recommendation(RECOMMENDATION_ID))

        assert status == 200
        assert body["applied"] is False
        assert body["dispatched"] is False
        assert body["auto_apply_enabled"] is False
        assert "AUTO_APPLY_ENABLED" in body["message"]
        mock_lambda_client.invoke.assert_not_called()

    @pytest.mark.parametrize("value", ["false", "TRUE ", "1", "yes", ""])
    def test_only_the_exact_string_true_enables_apply(self, agent_env, mock_lambda_client, monkeypatch, value):
        """Anything that is not 'true' (case-insensitive, untrimmed) leaves
        auto-apply off — a truthy-looking value must not enable mutation."""
        monkeypatch.setenv("AUTO_APPLY_ENABLED", value)

        status, body = _parse(agents.apply_cost_recommendation(RECOMMENDATION_ID))

        assert status == 200
        assert body["auto_apply_enabled"] is False
        mock_lambda_client.invoke.assert_not_called()

    def test_unset_endpoint_reports_unconfigured_and_does_not_dispatch(
        self, agent_env, mock_lambda_client, monkeypatch
    ):
        """R2.3: auto-apply on but the endpoint missing → honest body, no
        dispatch, and explicitly not applied."""
        monkeypatch.setenv("AUTO_APPLY_ENABLED", "true")
        monkeypatch.setenv("COST_OPTIMIZATION_AGENT_ENDPOINT", "")

        status, body = _parse(agents.apply_cost_recommendation(RECOMMENDATION_ID))

        assert status == 503
        assert body["error"] == "agent_not_configured"
        assert body["applied"] is False
        assert body["dispatched"] is False
        assert "COST_OPTIMIZATION_AGENT_ENDPOINT" in body["message"]
        mock_lambda_client.invoke.assert_not_called()

    def test_invoke_failure_returns_500_and_says_nothing_applied(self, agent_env, mock_lambda_client, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_ENABLED", "true")
        mock_lambda_client.invoke.side_effect = Exception("Lambda service unavailable")

        status, body = _parse(agents.apply_cost_recommendation(RECOMMENDATION_ID))

        assert status == 500
        assert body["applied"] is False
        assert body["dispatched"] is False


# ---------------------------------------------------------------------------
# R2.1 — the antipattern is gone from this module
# ---------------------------------------------------------------------------


def test_frozen_thread_antipattern_removed_from_source():
    """R2.1: no threading and no in-handler AgentCore call remain in
    handlers/agents.py, and the async invoke is a real Event invoke.

    ``handle_chat_message`` still calls ``invoke_agentcore_runtime`` inline and
    is SUPPOSED to — it is a synchronous request/response chat endpoint, not
    post-response work — so the AgentCore assertion is scoped to the two
    endpoints this requirement covers.

    The assertions target real code, not the word: the module docstring names
    ``threading.Thread`` on purpose, to record why it must not come back. A
    check for the bare substring would forbid documenting the fence.
    """
    with open(os.path.join(LAMBDA_DIR, "handlers", "agents.py")) as f:
        source = f.read()

    assert "import threading" not in source
    assert "threading.Thread(" not in source
    assert 'InvocationType="Event"' in source

    for name in ("def apply_cost_recommendation",):
        start = source.index(name)
        end = source.index("\ndef ", start + 1)
        assert "invoke_agentcore_runtime" not in source[start:end], f"{name} must not call AgentCore directly"
