# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/mcp_integration.py.

Covers the circuit breaker state machine, MCPClient caching / cost-tracking
logic, the rule-based fallback implementations, the cache-key behaviour of
``generate_pyspark_code``, the boto3-boundary fallbacks, and the module-level
health/cost helpers.

Mocking style follows the sibling ``tests/unit/shared`` suites
(``test_strands_mcp_factory.py`` / ``test_notification_service.py``): real
logic is exercised directly and AWS SDK calls are mocked at the boto3 client
boundary via ``unittest.mock``. No ``moto`` / live AWS is used.
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# Make shared modules importable (matches test_notification_service.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Set local mode before import (consistent with sibling shared tests).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("AWS_REGION", "us-east-1")

import mcp_integration
from mcp_integration import (
    CircuitBreaker,
    CircuitBreakerError,
    MCPClient,
    MCPIntegrationError,
    check_mcp_health,
    get_mcp_costs,
    with_mcp_fallback,
)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------
class TestCircuitBreaker:
    """The circuit-breaker state machine: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0
        assert cb.last_failure_time is None

    def test_successful_call_returns_result_and_stays_closed(self):
        cb = CircuitBreaker()
        result = cb.call(lambda x: x + 1, 41)
        assert result == 42
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0

    def test_failure_increments_count_and_reraises(self):
        cb = CircuitBreaker(failure_threshold=3)

        def boom():
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            cb.call(boom)
        assert cb.failure_count == 1
        assert cb.state == "CLOSED"
        assert cb.last_failure_time is not None

    def test_opens_after_failure_threshold(self):
        cb = CircuitBreaker(failure_threshold=2)

        def boom():
            raise RuntimeError("x")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                cb.call(boom)
        assert cb.state == "OPEN"
        assert cb.failure_count == 2

    def test_open_breaker_short_circuits_without_calling_func(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60)

        def boom():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            cb.call(boom)
        assert cb.state == "OPEN"

        # Now the breaker is OPEN and recovery time has not elapsed: the
        # protected function must NOT be invoked.
        sentinel = MagicMock(return_value="should-not-run")
        with pytest.raises(CircuitBreakerError):
            cb.call(sentinel)
        sentinel.assert_not_called()

    def test_half_open_after_recovery_timeout_then_closes_on_success(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=30)

        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert cb.state == "OPEN"

        # Simulate recovery window elapsing.
        cb.last_failure_time = datetime.now() - timedelta(seconds=31)
        assert cb._should_attempt_reset() is True

        # A successful call in HALF_OPEN closes the breaker again.
        result = cb.call(lambda: "ok")
        assert result == "ok"
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0

    def test_should_attempt_reset_true_when_no_failure_recorded(self):
        cb = CircuitBreaker()
        assert cb._should_attempt_reset() is True

    def test_unexpected_exception_type_is_not_counted(self):
        # Only ValueError is "expected"; a TypeError should propagate untouched
        # and must not increment the failure counter.
        cb = CircuitBreaker(failure_threshold=1, expected_exception=ValueError)

        def raise_type_error():
            raise TypeError("unexpected")

        with pytest.raises(TypeError):
            cb.call(raise_type_error)
        assert cb.failure_count == 0
        assert cb.state == "CLOSED"


# ---------------------------------------------------------------------------
# MCPClient construction + caching
# ---------------------------------------------------------------------------
class TestMCPClientConfig:
    def test_cost_threshold_read_from_env(self):
        with patch.dict(os.environ, {"MCP_COST_THRESHOLD": "250.5"}):
            client = MCPClient()
            assert client.cost_threshold == 250.5

    def test_default_circuit_breakers_present(self):
        client = MCPClient()
        assert set(client.circuit_breakers) == {
            "aws_operations",
            "code_generation",
            "optimization",
        }
        assert all(cb.state == "CLOSED" for cb in client.circuit_breakers.values())


class TestMCPClientCache:
    def test_cache_roundtrip_within_ttl(self):
        client = MCPClient()
        client._cache_response("k", {"v": 1})
        assert client._is_cache_valid("k") is True
        assert client._get_cached_response("k") == {"v": 1}

    def test_missing_key_is_invalid(self):
        client = MCPClient()
        assert client._is_cache_valid("absent") is False
        assert client._get_cached_response("absent") is None

    def test_expired_entry_is_invalid(self):
        client = MCPClient()
        client.cache["k"] = {
            "data": {"v": 1},
            "timestamp": datetime.now() - timedelta(seconds=client.cache_ttl + 10),
        }
        assert client._is_cache_valid("k") is False
        assert client._get_cached_response("k") is None

    def test_clear_cache_empties_store(self):
        client = MCPClient()
        client._cache_response("k", {"v": 1})
        client.clear_cache()
        assert client.cache == {}


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------
class TestCostTracking:
    def test_within_threshold_allows_operation(self):
        client = MCPClient()
        client.cost_threshold = 100.0
        client.daily_cost = 10.0
        assert client._check_cost_threshold(5.0) is True

    def test_exceeding_threshold_blocks_operation(self):
        client = MCPClient()
        client.cost_threshold = 100.0
        client.daily_cost = 99.0
        assert client._check_cost_threshold(5.0) is False

    def test_daily_cost_resets_on_new_day(self):
        client = MCPClient()
        client.cost_threshold = 100.0
        client.daily_cost = 99.0
        client.cost_reset_date = (datetime.now() - timedelta(days=1)).date()
        # New day → daily_cost reset to 0, so the operation now fits.
        assert client._check_cost_threshold(5.0) is True
        assert client.daily_cost == 0.0
        assert client.cost_reset_date == datetime.now().date()

    def test_track_cost_accumulates(self):
        client = MCPClient()
        client.daily_cost = 0.0
        client._track_cost(1.25)
        client._track_cost(0.75)
        assert client.daily_cost == 2.0


# ---------------------------------------------------------------------------
# _call_agentcore — boto3 (bedrock-agent-runtime) boundary
# ---------------------------------------------------------------------------
class TestCallAgentcore:
    def test_raises_when_not_configured(self):
        client = MCPClient()
        client.agentcore_endpoint = None
        client.agent_id = None
        with pytest.raises(MCPIntegrationError, match="configuration not available"):
            client._call_agentcore("op", {})

    def test_raises_when_cost_threshold_exceeded(self):
        client = MCPClient()
        client.agentcore_endpoint = "https://endpoint"
        client.agent_id = "agent-1"
        client.agent_alias_id = "alias-1"
        client.cost_threshold = 0.0  # any operation exceeds
        client.daily_cost = 0.0
        with pytest.raises(MCPIntegrationError, match="cost threshold exceeded"):
            client._call_agentcore("op", {})

    def _streaming_response(self, payload: bytes):
        return {"completion": [{"chunk": {"bytes": payload}}]}

    def test_parses_json_completion(self):
        client = MCPClient()
        client.agentcore_endpoint = "https://endpoint"
        client.agent_id = "agent-1"
        client.agent_alias_id = "alias-1"
        client.cost_threshold = 100.0
        client.daily_cost = 0.0

        mock_runtime = MagicMock()
        mock_runtime.invoke_agent.return_value = self._streaming_response(b'{"answer": 42}')
        with patch("mcp_integration.boto3.client", return_value=mock_runtime):
            result = client._call_agentcore("op", {"p": 1})

        assert result == {"answer": 42}
        mock_runtime.invoke_agent.assert_called_once()
        # Cost is tracked after a successful call.
        assert client.daily_cost > 0.0

    def test_non_json_completion_returns_raw_response(self):
        client = MCPClient()
        client.agentcore_endpoint = "https://endpoint"
        client.agent_id = "agent-1"
        client.agent_alias_id = "alias-1"
        client.cost_threshold = 100.0
        client.daily_cost = 0.0

        mock_runtime = MagicMock()
        mock_runtime.invoke_agent.return_value = self._streaming_response(b"plain text answer")
        with patch("mcp_integration.boto3.client", return_value=mock_runtime):
            result = client._call_agentcore("op", {})

        assert result == {"result": "plain text answer", "raw_response": True}

    def test_invoke_failure_wrapped_in_mcp_error(self):
        client = MCPClient()
        client.agentcore_endpoint = "https://endpoint"
        client.agent_id = "agent-1"
        client.agent_alias_id = "alias-1"
        client.cost_threshold = 100.0
        client.daily_cost = 0.0

        mock_runtime = MagicMock()
        mock_runtime.invoke_agent.side_effect = RuntimeError("bedrock down")
        with patch("mcp_integration.boto3.client", return_value=mock_runtime):
            with pytest.raises(MCPIntegrationError, match="AgentCore operation failed"):
                client._call_agentcore("op", {})


# ---------------------------------------------------------------------------
# list_s3_objects fallback — boto3 (s3) boundary
# ---------------------------------------------------------------------------
class TestListS3ObjectsFallback:
    def test_fallback_parses_object_keys(self):
        client = MCPClient()
        # No AgentCore config → MCP path raises → boto3 fallback is used.
        client.agentcore_endpoint = None
        client.agent_id = None

        mock_s3 = MagicMock()
        mock_s3.list_objects_v2.return_value = {"Contents": [{"Key": "a.csv"}, {"Key": "b.csv"}]}
        with patch.object(client, "_get_boto3_client", return_value=mock_s3):
            result = client.list_s3_objects("bucket", "prefix/")

        assert result["objects"] == ["a.csv", "b.csv"]
        assert result["fallback_used"] is True
        assert result["operation"] == "list_s3_objects"
        mock_s3.list_objects_v2.assert_called_once_with(Bucket="bucket", Prefix="prefix/", MaxKeys=1000)

    def test_fallback_handles_empty_bucket(self):
        client = MCPClient()
        client.agentcore_endpoint = None
        client.agent_id = None

        mock_s3 = MagicMock()
        mock_s3.list_objects_v2.return_value = {}  # no Contents key
        with patch.object(client, "_get_boto3_client", return_value=mock_s3):
            result = client.list_s3_objects("bucket")
        assert result["objects"] == []

    def test_cache_hit_short_circuits_before_fallback(self):
        client = MCPClient()
        cache_key = "s3_list_bucket_prefix/"
        client._cache_response(cache_key, {"objects": ["cached"], "cached": True})

        # _get_boto3_client should never be reached on a cache hit.
        with patch.object(client, "_get_boto3_client") as mock_get:
            result = client.list_s3_objects("bucket", "prefix/")
        assert result == {"objects": ["cached"], "cached": True}
        mock_get.assert_not_called()

    def test_fallback_client_error_raises_mcp_error(self):
        from botocore.exceptions import ClientError

        client = MCPClient()
        client.agentcore_endpoint = None
        client.agent_id = None

        mock_s3 = MagicMock()
        mock_s3.list_objects_v2.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2"
        )
        with patch.object(client, "_get_boto3_client", return_value=mock_s3):
            with pytest.raises(MCPIntegrationError, match="Both MCP and fallback failed"):
                client.list_s3_objects("bucket")


# ---------------------------------------------------------------------------
# optimize_glue_job — rule-based fallback
# ---------------------------------------------------------------------------
class TestOptimizeGlueJobFallback:
    @pytest.mark.parametrize(
        "data_size,worker_type,workers,capacity",
        [
            ("small", "G.1X", 2, 2),
            ("medium", "G.1X", 5, 10),
            ("large", "G.2X", 10, 20),
            ("xlarge", "G.2X", 20, 40),
        ],
    )
    def test_fallback_rules_per_size(self, data_size, worker_type, workers, capacity):
        client = MCPClient()
        result = client._fallback_optimize_glue_job("job", data_size, "basic")
        assert result["worker_type"] == worker_type
        assert result["number_of_workers"] == workers
        assert result["max_capacity"] == capacity
        assert result["fallback_used"] is True
        assert result["optimization_level"] == "basic_rules"

    def test_unknown_size_defaults_to_medium(self):
        client = MCPClient()
        result = client._fallback_optimize_glue_job("job", "gigantic", "basic")
        assert result["number_of_workers"] == 5
        assert result["max_capacity"] == 10

    def test_complex_transformation_scales_workers(self):
        client = MCPClient()
        result = client._fallback_optimize_glue_job("job", "medium", "complex")
        # medium baseline (5 workers / 10 capacity) scaled by 1.5 → 7 / 15.
        assert result["number_of_workers"] == 7
        assert result["max_capacity"] == 15
        assert isinstance(result["number_of_workers"], int)

    def test_optimize_glue_job_uses_fallback_when_unconfigured(self):
        client = MCPClient()
        client.agentcore_endpoint = None
        client.agent_id = None
        result = client.optimize_glue_job("job", "large", "basic")
        assert result["fallback_used"] is True
        assert result["worker_type"] == "G.2X"


# ---------------------------------------------------------------------------
# generate_pyspark_code — cache key behaviour + template fallback
# ---------------------------------------------------------------------------
class TestGeneratePysparkCode:
    def test_cache_key_is_input_sensitive_and_hits(self):
        client = MCPClient()
        src = {"id": "int", "name": "string"}
        tgt = {"customer_id": "int"}
        mappings = {"customer_id": "id"}

        # Pre-seed the cache using the exact key the production code computes.
        cache_key = f"pyspark_gen_{hash(str(src))}_{hash(str(tgt))}_{hash(str(mappings))}"
        client.cache[cache_key] = {"data": {"cached": True}, "timestamp": datetime.now()}

        # Identical inputs → cache hit returns the seeded value.
        assert client.generate_pyspark_code(src, tgt, mappings) == {"cached": True}

        # Different source schema → different key → cache miss → fallback.
        client.agentcore_endpoint = None
        client.agent_id = None
        other = client.generate_pyspark_code({"other": "x"}, tgt, mappings)
        assert other.get("fallback_used") is True

    def test_fallback_builds_simple_column_aliases(self):
        client = MCPClient()
        result = client._fallback_generate_pyspark_code({"id": "int"}, {"customer_id": "int"}, {"customer_id": "id"})
        assert result["fallback_used"] is True
        assert result["template_used"] == "basic_etl"
        assert 'col("id").alias("customer_id")' in result["code"]
        assert result["estimated_cost"] == 0.0

    def test_fallback_concats_list_source_fields(self):
        client = MCPClient()
        result = client._fallback_generate_pyspark_code(
            {"first": "string", "last": "string"},
            {"full_name": "string"},
            {"full_name": ["first", "last"]},
        )
        assert 'concat(col("first"), col("last")).alias("full_name")' in result["code"]

    def test_generate_pyspark_code_falls_back_when_unconfigured(self):
        client = MCPClient()
        client.agentcore_endpoint = None
        client.agent_id = None
        result = client.generate_pyspark_code({"a": "int"}, {"b": "int"}, {"b": "a"})
        assert result["fallback_used"] is True
        assert result["operation"] == "generate_pyspark_code"


# ---------------------------------------------------------------------------
# Health / reset helpers
# ---------------------------------------------------------------------------
class TestHealthAndReset:
    def test_health_status_reports_unconfigured(self):
        client = MCPClient()
        client.agentcore_endpoint = None
        client.agent_id = None
        status = client.get_health_status()
        assert status["agentcore_configured"] is False
        assert status["fallback_available"] is True
        assert set(status["circuit_breakers"]) == {
            "aws_operations",
            "code_generation",
            "optimization",
        }
        for cb_status in status["circuit_breakers"].values():
            assert cb_status["state"] == "CLOSED"
            assert cb_status["failure_count"] == 0
            assert cb_status["last_failure"] is None

    def test_health_status_reports_configured(self):
        client = MCPClient()
        client.agentcore_endpoint = "https://endpoint"
        client.agent_id = "agent-1"
        status = client.get_health_status()
        assert status["agentcore_configured"] is True

    def test_health_status_reflects_open_breaker(self):
        client = MCPClient()
        cb = client.circuit_breakers["aws_operations"]
        cb.state = "OPEN"
        cb.failure_count = 3
        cb.last_failure_time = datetime.now()
        status = client.get_health_status()
        aws = status["circuit_breakers"]["aws_operations"]
        assert aws["state"] == "OPEN"
        assert aws["failure_count"] == 3
        assert aws["last_failure"] is not None

    def test_reset_circuit_breakers_restores_closed(self):
        client = MCPClient()
        cb = client.circuit_breakers["optimization"]
        cb.state = "OPEN"
        cb.failure_count = 9
        cb.last_failure_time = datetime.now()

        client.reset_circuit_breakers()
        for cb in client.circuit_breakers.values():
            assert cb.state == "CLOSED"
            assert cb.failure_count == 0
            assert cb.last_failure_time is None


# ---------------------------------------------------------------------------
# with_mcp_fallback decorator
# ---------------------------------------------------------------------------
class TestWithMcpFallbackDecorator:
    def test_returns_function_result_on_success(self):
        mcp_integration.mcp_client.reset_circuit_breakers()

        @with_mcp_fallback("aws_operations")
        def add(a, b):
            return a + b

        assert add(2, 3) == 5

    def test_falls_back_to_function_when_breaker_open(self):
        breaker = mcp_integration.mcp_client.circuit_breakers["aws_operations"]
        original_state = breaker.state
        original_failures = breaker.failure_count
        original_time = breaker.last_failure_time
        try:
            # Force the breaker OPEN so the wrapped call raises
            # CircuitBreakerError; the decorator must then call the original
            # function directly as the fallback.
            breaker.state = "OPEN"
            breaker.last_failure_time = datetime.now()

            calls = {"n": 0}

            @with_mcp_fallback("aws_operations")
            def work():
                calls["n"] += 1
                return "fallback-result"

            assert work() == "fallback-result"
            # Called once as the fallback (the breaker short-circuited the
            # protected attempt without invoking the function).
            assert calls["n"] == 1
        finally:
            breaker.state = original_state
            breaker.failure_count = original_failures
            breaker.last_failure_time = original_time


# ---------------------------------------------------------------------------
# Module-level monitoring helpers
# ---------------------------------------------------------------------------
class TestModuleHelpers:
    def test_check_mcp_health_returns_status_dict(self):
        status = check_mcp_health()
        assert "agentcore_configured" in status
        assert "circuit_breakers" in status
        assert "cache_size" in status

    def test_get_mcp_costs_computes_utilization(self):
        client = mcp_integration.mcp_client
        original_cost = client.daily_cost
        original_threshold = client.cost_threshold
        try:
            client.daily_cost = 25.0
            client.cost_threshold = 100.0
            costs = get_mcp_costs()
            assert costs["daily_cost"] == 25.0
            assert costs["cost_threshold"] == 100.0
            assert costs["cost_utilization"] == 25.0
            assert "reset_date" in costs
        finally:
            client.daily_cost = original_cost
            client.cost_threshold = original_threshold
