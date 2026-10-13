# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/model_config.py.

Covers two contracts:

P21 Req 1 — single-source-of-truth model ID:
- resolve_model_id() returns MODEL_ID_DEFAULT when no env var is set (Req 1.1, 1.4)
- resolve_model_id() returns the BEDROCK_MODEL_ID override when set (Req 1.6, 1.7)
- MODEL_ID_DEFAULT and FALLBACK_MODEL_ID are distinct valid IDs (Req 1.5)

P28 R15 — explicit converse params + per-run call budget:
- converse_params() defaults + env overrides (R15.1, R15.3)
- CallBudget: under-budget passes; exceeding raises a permanent StepFailure
  whose message contains "budget"; env-configured limit respected; DynamoDB
  path (moto) increments atomically and enforces cross-invocation totals;
  graceful in-memory-only mode without the table env var (R15.2, R15.3)
- invoke_model(): merges shared defaults into inferenceConfig with caller
  values winning; enforces the budget BEFORE calling converse; passes
  modelId/messages/system through untouched; returns the client response
  (R15.1, R15.2)

Validates: Requirements 15.1, 15.2, 15.3
"""

import os
import sys
from typing import Any

import boto3
import pytest
from tests.fixtures.conftest import USE_REAL_AWS

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

import model_config
from model_config import (
    BUDGET_TABLE_ENV,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_CALL_BUDGET,
    DEFAULT_TEMPERATURE,
    FALLBACK_MODEL_ID,
    MODEL_ID_DEFAULT,
    CallBudget,
    converse_params,
    converse_text,
    get_call_budget,
    invoke_model,
    resolve_model_id,
)
from step_errors import StepFailure

BUDGET_TABLE_NAME = "agentic_data_ingestion_model_call_budget"


@pytest.fixture(autouse=True)
def _clean_budget_state(monkeypatch):
    """Isolate every test from env config and the container budget cache."""
    monkeypatch.delenv("MODEL_MAX_TOKENS", raising=False)
    monkeypatch.delenv("MODEL_TEMPERATURE", raising=False)
    monkeypatch.delenv("MODEL_CALL_BUDGET", raising=False)
    monkeypatch.delenv(BUDGET_TABLE_ENV, raising=False)
    model_config._BUDGETS.clear()
    # p67: the learned sampling-support memo is per-container state, so it must
    # be cleared between tests for the same reason the budget cache is.
    model_config._UNSUPPORTED_SAMPLING_PARAMS.clear()
    model_config._MODEL_OUTPUT_TOKEN_LIMITS.clear()
    yield
    model_config._BUDGETS.clear()
    model_config._UNSUPPORTED_SAMPLING_PARAMS.clear()
    model_config._MODEL_OUTPUT_TOKEN_LIMITS.clear()


@pytest.fixture
def budget_table(mock_dynamodb_tables, monkeypatch):
    """Budget table (pk run_id) created inside the shared moto context.

    The table itself is new P28 infrastructure and not yet part of the
    CDK-derived shared table set, so it is created here — inside the moto
    context provided by the shared ``mock_dynamodb_tables`` fixture (no
    direct ``mock_aws()`` usage). Skipped in real-AWS mode: the table does
    not exist in the deployed stack and must not be created for real.
    """
    if USE_REAL_AWS:
        pytest.skip("budget table is not part of the CDK-deployed table set")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName=BUDGET_TABLE_NAME,
        KeySchema=[{"AttributeName": "run_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "run_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setenv(BUDGET_TABLE_ENV, BUDGET_TABLE_NAME)
    return dynamodb.Table(BUDGET_TABLE_NAME)


class FakeConverseClient:
    """Records converse kwargs and returns a canned response — the injection
    seam invoke_model is designed around (no AWS mocking needed)."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response or {"output": {"message": {"content": [{"text": "ok"}]}}}

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


# ── P21 Req 1: model ID single source of truth ────────────────────────


class TestResolveModelId:
    def test_returns_default_without_env(self, monkeypatch):
        """With no BEDROCK_MODEL_ID set, resolve_model_id() returns MODEL_ID_DEFAULT."""
        monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
        assert resolve_model_id() == MODEL_ID_DEFAULT

    def test_returns_override_when_env_set(self, monkeypatch):
        """When BEDROCK_MODEL_ID is set to a non-empty value, that value is returned."""
        override = "us.anthropic.claude-opus-4-1-20250805-v1:0"
        monkeypatch.setenv("BEDROCK_MODEL_ID", override)
        assert resolve_model_id() == override

    def test_ignores_empty_override(self, monkeypatch):
        """An empty BEDROCK_MODEL_ID falls back to the default (Req 1.7 non-empty value)."""
        monkeypatch.setenv("BEDROCK_MODEL_ID", "")
        assert resolve_model_id() == MODEL_ID_DEFAULT

    def test_default_differs_from_fallback(self):
        """The primary and fallback model IDs must be distinct (Req 1.5)."""
        assert MODEL_ID_DEFAULT != FALLBACK_MODEL_ID

    def test_canonical_default_value(self):
        """MODEL_ID_DEFAULT is the canonical dateless Claude 4.6 inference-profile ID (Req 1.1)."""
        assert MODEL_ID_DEFAULT == "us.anthropic.claude-sonnet-4-6"


# ── R15.1 / R15.3: converse_params ────────────────────────────────────


class TestConverseParams:
    def test_documented_defaults_without_env(self):
        """Defaults match the documented values: maxTokens=16000, temperature=0.0.

        16000 was 4000. It is a ceiling, billed only for what the reply uses, and
        4000 truncated a mapping of roughly 20 target fields, or fewer when the
        model reasons first. See `DEFAULT_MAX_TOKENS`.
        """
        assert converse_params() == {"maxTokens": 16000, "temperature": 0.0}
        assert DEFAULT_MAX_TOKENS == 16000
        assert DEFAULT_TEMPERATURE == 0.0

    def test_env_overrides_apply(self, monkeypatch):
        monkeypatch.setenv("MODEL_MAX_TOKENS", "8192")
        monkeypatch.setenv("MODEL_TEMPERATURE", "0.7")
        assert converse_params() == {"maxTokens": 8192, "temperature": 0.7}

    def test_empty_env_values_fall_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("MODEL_MAX_TOKENS", "")
        monkeypatch.setenv("MODEL_TEMPERATURE", "  ")
        assert converse_params() == {"maxTokens": DEFAULT_MAX_TOKENS, "temperature": DEFAULT_TEMPERATURE}

    def test_garbage_max_tokens_raises_naming_the_var(self, monkeypatch):
        """Misconfiguration fails loudly — no silent fallback (P28 spirit)."""
        monkeypatch.setenv("MODEL_MAX_TOKENS", "lots")
        with pytest.raises(ValueError, match="MODEL_MAX_TOKENS"):
            converse_params()

    def test_garbage_temperature_raises_naming_the_var(self, monkeypatch):
        monkeypatch.setenv("MODEL_TEMPERATURE", "warm")
        with pytest.raises(ValueError, match="MODEL_TEMPERATURE"):
            converse_params()

    def test_returns_fresh_dict_each_call(self):
        """Caller mutation must not leak into later calls."""
        first = converse_params()
        first["maxTokens"] = 1
        assert converse_params()["maxTokens"] == DEFAULT_MAX_TOKENS


# ── R15.2 / R15.3: CallBudget ─────────────────────────────────────────


class TestCallBudgetInMemory:
    def test_under_budget_passes_and_counts(self):
        budget = CallBudget("run-1", max_calls=3)
        assert budget.check_and_increment() == 1
        assert budget.check_and_increment() == 2
        assert budget.check_and_increment() == 3
        assert budget.local_count == 3

    def test_exceeding_raises_permanent_step_failure_with_budget_message(self):
        budget = CallBudget("run-1", max_calls=2)
        budget.check_and_increment()
        budget.check_and_increment()
        with pytest.raises(StepFailure) as excinfo:
            budget.check_and_increment("schema_mapping")
        err = excinfo.value
        assert err.error_type == "permanent"
        assert "budget" in err.message
        assert err.step_name == "schema_mapping"
        assert err.details["run_id"] == "run-1"
        assert err.details["max_calls"] == 2

    def test_default_budget_from_env(self, monkeypatch):
        """MODEL_CALL_BUDGET configures the limit when max_calls is omitted (R15.3)."""
        monkeypatch.setenv("MODEL_CALL_BUDGET", "2")
        budget = CallBudget("run-env")
        assert budget.max_calls == 2
        budget.check_and_increment()
        budget.check_and_increment()
        with pytest.raises(StepFailure, match="budget"):
            budget.check_and_increment()

    def test_explicit_max_calls_beats_env(self, monkeypatch):
        monkeypatch.setenv("MODEL_CALL_BUDGET", "2")
        assert CallBudget("run-x", max_calls=7).max_calls == 7

    def test_documented_default_budget_without_env(self):
        assert CallBudget("run-x").max_calls == DEFAULT_MODEL_CALL_BUDGET
        assert DEFAULT_MODEL_CALL_BUDGET == 50

    def test_budget_below_one_rejected(self):
        with pytest.raises(ValueError, match="max_calls"):
            CallBudget("run-x", max_calls=0)

    def test_garbage_env_budget_raises(self, monkeypatch):
        monkeypatch.setenv("MODEL_CALL_BUDGET", "unlimited")
        with pytest.raises(ValueError, match="MODEL_CALL_BUDGET"):
            CallBudget("run-x")

    def test_in_memory_only_mode_without_table_env(self):
        """Without MODEL_CALL_BUDGET_TABLE the budget degrades gracefully to
        in-memory counting — no boto3 resource is ever created (R15.3)."""
        budget = CallBudget("run-1", max_calls=2)
        assert budget._table_name is None
        budget.check_and_increment()
        budget.check_and_increment()
        with pytest.raises(StepFailure, match="budget"):
            budget.check_and_increment()
        assert budget._table is None  # DynamoDB path never touched


class TestCallBudgetDynamoDB:
    def test_increments_are_atomic_adds_on_the_run_item(self, budget_table):
        budget = CallBudget("run-ddb", max_calls=10)
        budget.check_and_increment()
        budget.check_and_increment()
        item = budget_table.get_item(Key={"run_id": "run-ddb"})["Item"]
        assert item["call_count"] == 2
        assert item["expires_at"] > 0  # TTL attribute for self-cleanup

    def test_cross_invocation_totals_enforced(self, budget_table):
        """Two CallBudget instances (≈ two Lambda invocations/containers)
        share the per-run DynamoDB total — the budget holds across them."""
        first = CallBudget("run-shared", max_calls=3)
        first.check_and_increment()
        first.check_and_increment()

        second = CallBudget("run-shared", max_calls=3)  # fresh "invocation"
        assert second.check_and_increment() == 3  # per-run total, not local 1
        with pytest.raises(StepFailure) as excinfo:
            second.check_and_increment("create_code")
        assert excinfo.value.details["call_count"] == 4
        assert excinfo.value.error_type == "permanent"

    def test_runs_do_not_share_counters(self, budget_table):
        CallBudget("run-a", max_calls=2).check_and_increment()
        budget_b = CallBudget("run-b", max_calls=2)
        budget_b.check_and_increment()
        assert budget_b.check_and_increment() == 2  # run-a's call didn't count here

    def test_dynamodb_failure_degrades_to_in_memory_with_loud_log(self, budget_table, monkeypatch, caplog):
        """A broken table config logs loudly and falls back to the in-memory
        floor for that call — model calls stay available, never silent."""
        monkeypatch.setenv(BUDGET_TABLE_ENV, "no-such-table")
        budget = CallBudget("run-broken", max_calls=1)
        assert budget.check_and_increment() == 1  # local count used
        assert any("DynamoDB increment failed" in r.message for r in caplog.records)
        with pytest.raises(StepFailure, match="budget"):
            budget.check_and_increment()  # in-memory floor still enforces


class TestGetCallBudget:
    def test_same_instance_per_run(self):
        assert get_call_budget("run-1") is get_call_budget("run-1")

    def test_distinct_instances_per_run(self):
        assert get_call_budget("run-1") is not get_call_budget("run-2")

    def test_cache_is_bounded(self):
        for i in range(model_config._MAX_CACHED_BUDGETS + 10):
            get_call_budget(f"run-{i}")
        assert len(model_config._BUDGETS) <= model_config._MAX_CACHED_BUDGETS


# ── R15.1 / R15.2: invoke_model ───────────────────────────────────────


class TestInvokeModel:
    def test_merges_shared_defaults_into_inference_config(self):
        client = FakeConverseClient()
        invoke_model(client, step_name="schema_mapping", modelId="m1", messages=[])
        assert client.calls[0]["inferenceConfig"] == {
            "maxTokens": DEFAULT_MAX_TOKENS,
            "temperature": DEFAULT_TEMPERATURE,
        }

    def test_explicit_caller_inference_config_wins(self):
        client = FakeConverseClient()
        invoke_model(
            client,
            step_name="schema_mapping",
            modelId="m1",
            messages=[],
            inferenceConfig={"maxTokens": 4000, "topP": 0.9},
        )
        cfg = client.calls[0]["inferenceConfig"]
        assert cfg["maxTokens"] == 4000  # caller value kept
        assert cfg["topP"] == 0.9  # caller extras pass through
        assert cfg["temperature"] == DEFAULT_TEMPERATURE  # default fills the gap

    def test_env_config_reaches_the_call(self, monkeypatch):
        monkeypatch.setenv("MODEL_MAX_TOKENS", "1234")
        client = FakeConverseClient()
        invoke_model(client, step_name="s", modelId="m1", messages=[])
        assert client.calls[0]["inferenceConfig"]["maxTokens"] == 1234

    def test_passes_converse_kwargs_through_untouched(self):
        """The mechanical-adoption contract: modelId/messages/system arrive
        at client.converse exactly as the call site passed them."""
        client = FakeConverseClient()
        messages = [{"role": "user", "content": [{"text": "hello"}]}]
        system = [{"text": "You are a data engineer."}]
        invoke_model(
            client,
            step_name="schema_mapping",
            modelId="us.anthropic.claude-sonnet-4-6",
            messages=messages,
            system=system,
        )
        call = client.calls[0]
        assert call["modelId"] == "us.anthropic.claude-sonnet-4-6"
        assert call["messages"] is messages
        assert call["system"] is system
        assert set(call.keys()) == {"modelId", "messages", "system", "inferenceConfig"}

    def test_returns_client_response_unchanged(self):
        response = {"output": {"message": {"content": [{"text": "hi"}]}}, "usage": {"inputTokens": 1}}
        client = FakeConverseClient(response=response)
        assert invoke_model(client, step_name="s", modelId="m", messages=[]) is response

    def test_budget_enforced_when_run_id_given(self, monkeypatch):
        monkeypatch.setenv("MODEL_CALL_BUDGET", "2")
        client = FakeConverseClient()
        invoke_model(client, step_name="s", run_id="run-1", modelId="m", messages=[])
        invoke_model(client, step_name="s", run_id="run-1", modelId="m", messages=[])
        with pytest.raises(StepFailure, match="budget"):
            invoke_model(client, step_name="s", run_id="run-1", modelId="m", messages=[])

    def test_exhausted_budget_raises_without_calling_converse(self, monkeypatch):
        """An over-budget invocation never reaches Bedrock (R15.2)."""
        monkeypatch.setenv("MODEL_CALL_BUDGET", "1")
        client = FakeConverseClient()
        invoke_model(client, step_name="s", run_id="run-1", modelId="m", messages=[])
        with pytest.raises(StepFailure):
            invoke_model(client, step_name="s", run_id="run-1", modelId="m", messages=[])
        assert len(client.calls) == 1  # second call blocked before converse

    def test_budget_failure_carries_step_name(self, monkeypatch):
        monkeypatch.setenv("MODEL_CALL_BUDGET", "1")
        client = FakeConverseClient()
        invoke_model(client, step_name="create_code", run_id="run-1", modelId="m", messages=[])
        with pytest.raises(StepFailure) as excinfo:
            invoke_model(client, step_name="create_code", run_id="run-1", modelId="m", messages=[])
        assert excinfo.value.step_name == "create_code"

    def test_no_run_id_skips_budget_enforcement(self, monkeypatch):
        monkeypatch.setenv("MODEL_CALL_BUDGET", "1")
        client = FakeConverseClient()
        for _ in range(3):  # would exceed any budget if one were enforced
            invoke_model(client, step_name="s", modelId="m", messages=[])
        assert len(client.calls) == 3
        assert model_config._BUDGETS == {}  # no budget was created


# ── p67: sampling parameters a model generation refuses ───────────────
#
# The live failure, verbatim from CloudWatch (pipeline `jdkjdkjd-af07e284`,
# run `run-2026-09-18-18-55-38-27baf5da`):
#
#     Error calling Bedrock: An error occurred (ValidationException) when
#     calling the Converse operation: The model returned the following
#     errors: `temperature` is deprecated for this model.
#
# which `generate_mapping_with_bedrock`'s outer `except Exception` turned into
# a substring-match mapping. The two halves asserted below are: the rejection
# path now succeeds, and a model that accepts `temperature` still gets it.


#: The observed message, kept as a literal so a message-shape change surfaces here.
_OBSERVED_REJECTION = (
    "An error occurred (ValidationException) when calling the Converse operation: "
    "The model returned the following errors: `temperature` is deprecated for this model."
)


class FakeClientError(Exception):
    """A botocore-shaped ``ClientError`` stand-in.

    Duck-typed on ``.response`` rather than constructed from botocore, which is
    what ``_rejected_sampling_params`` reads — `model_config` deliberately keeps
    botocore off its import path.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class RejectsSamplingParamsClient:
    """Fails any converse call carrying one of ``rejects``; records every call."""

    def __init__(self, rejects: tuple[str, ...] = ("temperature",), message: str = _OBSERVED_REJECTION) -> None:
        self.rejects = rejects
        self.message = message
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        offending = [p for p in self.rejects if p in kwargs.get("inferenceConfig", {})]
        if offending:
            raise FakeClientError("ValidationException", self.message)
        return {"output": {"message": {"content": [{"text": "ok"}]}}, "usage": {"inputTokens": 1}}


class TestSamplingParameterRecovery:
    def test_temperature_rejection_succeeds_after_one_retry_without_it(self, caplog):
        """The live failure now returns a response instead of raising."""
        client = RejectsSamplingParamsClient()

        response = invoke_model(client, step_name="schema_mapping", modelId="us.anthropic.claude-sonnet-5", messages=[])

        assert response["output"]["message"]["content"][0]["text"] == "ok"
        assert len(client.calls) == 2
        # First attempt carried it, the retry did not.
        assert "temperature" in client.calls[0]["inferenceConfig"]
        assert "temperature" not in client.calls[1]["inferenceConfig"]
        # Everything else survived the retry untouched.
        assert client.calls[1]["inferenceConfig"]["maxTokens"] == DEFAULT_MAX_TOKENS
        assert client.calls[1]["modelId"] == "us.anthropic.claude-sonnet-5"
        # And it said so — a silent recovery is still a silent degradation.
        assert any("refuses inference parameter" in r.message for r in caplog.records)

    def test_temperature_accepting_model_still_receives_it(self):
        """The determinism default is NOT discarded for models that accept it."""
        client = FakeConverseClient()

        invoke_model(client, step_name="schema_mapping", modelId="us.anthropic.claude-sonnet-4-6", messages=[])

        assert len(client.calls) == 1  # no retry, no second wire call
        assert client.calls[0]["inferenceConfig"]["temperature"] == DEFAULT_TEMPERATURE
        assert model_config._UNSUPPORTED_SAMPLING_PARAMS == {}  # nothing learned

    def test_rejection_is_remembered_so_later_calls_pay_no_retry(self):
        """The second call to a refusing model pre-strips the parameter."""
        client = RejectsSamplingParamsClient()

        invoke_model(client, step_name="s", modelId="m-refuses", messages=[])
        assert len(client.calls) == 2

        invoke_model(client, step_name="s", modelId="m-refuses", messages=[])
        assert len(client.calls) == 3  # one call, not two
        assert "temperature" not in client.calls[2]["inferenceConfig"]

    def test_memo_does_not_leak_between_models(self):
        """A refusal by one model says nothing about another."""
        refusing = RejectsSamplingParamsClient()
        invoke_model(refusing, step_name="s", modelId="m-refuses", messages=[])

        accepting = FakeConverseClient()
        invoke_model(accepting, step_name="s", modelId="m-accepts", messages=[])
        assert accepting.calls[0]["inferenceConfig"]["temperature"] == DEFAULT_TEMPERATURE

    def test_memo_is_bounded(self):
        for i in range(model_config._MAX_CACHED_SAMPLING_SUPPORT + 10):
            invoke_model(RejectsSamplingParamsClient(), step_name="s", modelId=f"m-{i}", messages=[])
        assert len(model_config._UNSUPPORTED_SAMPLING_PARAMS) <= model_config._MAX_CACHED_SAMPLING_SUPPORT

    def test_top_p_rejection_is_recovered_too(self):
        """``top_p`` is named unsupported alongside temperature; both are droppable.

        The error spells it ``top_p`` while the Converse key is ``topP``, which
        is why the match carries per-key aliases rather than the key alone.
        """
        client = RejectsSamplingParamsClient(
            rejects=("topP",), message="ValidationException: `top_p` is deprecated for this model."
        )

        invoke_model(client, step_name="s", modelId="m", messages=[], inferenceConfig={"topP": 0.9, "temperature": 0.0})

        assert len(client.calls) == 2
        assert "topP" not in client.calls[1]["inferenceConfig"]
        # Only what was blamed is dropped — temperature was not named.
        assert client.calls[1]["inferenceConfig"]["temperature"] == 0.0

    def test_validation_exception_naming_nothing_droppable_propagates(self):
        """This is NOT a blind retry-on-400."""

        class BadRequestClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def converse(self, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(kwargs)
                raise FakeClientError("ValidationException", "messages: at least one message is required")

        client = BadRequestClient()
        with pytest.raises(FakeClientError):
            invoke_model(client, step_name="s", modelId="m", messages=[])
        assert len(client.calls) == 1  # no retry
        assert model_config._UNSUPPORTED_SAMPLING_PARAMS == {}

    def test_non_validation_exception_propagates_untouched(self):
        """A throttle mentioning nothing droppable is not retried here."""

        class ThrottledClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def converse(self, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(kwargs)
                raise FakeClientError("ThrottlingException", "Rate exceeded")

        client = ThrottledClient()
        with pytest.raises(FakeClientError):
            invoke_model(client, step_name="s", modelId="m", messages=[])
        assert len(client.calls) == 1

    def test_plain_exception_propagates_and_is_logged_loudly(self, caplog):
        """An unrecoverable rejection is logged at ERROR before it propagates."""

        class BrokenClient:
            def converse(self, **kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("bedrock unavailable")

        with pytest.raises(RuntimeError):
            invoke_model(BrokenClient(), step_name="schema_mapping", modelId="m", messages=[])

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("not recoverable" in r.message for r in errors)

    def test_the_retry_does_not_double_count_the_budget(self, monkeypatch):
        """A recovered call costs ONE unit of the per-run budget, not two."""
        monkeypatch.setenv("MODEL_CALL_BUDGET", "2")
        client = RejectsSamplingParamsClient()

        invoke_model(client, step_name="schema_mapping", run_id="run-1", modelId="m", messages=[])
        assert get_call_budget("run-1").local_count == 1
        assert len(client.calls) == 2  # two wire calls, one budgeted call

        # The second logical call is still allowed; a third is not.
        invoke_model(client, step_name="schema_mapping", run_id="run-1", modelId="m", messages=[])
        with pytest.raises(StepFailure, match="budget"):
            invoke_model(client, step_name="schema_mapping", run_id="run-1", modelId="m", messages=[])

    def test_exhausted_budget_still_blocks_before_any_attempt(self, monkeypatch):
        """Recovery does not create a path around the budget (R15.2)."""
        monkeypatch.setenv("MODEL_CALL_BUDGET", "1")
        client = RejectsSamplingParamsClient()
        invoke_model(client, step_name="s", run_id="run-1", modelId="m", messages=[])
        calls_before = len(client.calls)
        with pytest.raises(StepFailure):
            invoke_model(client, step_name="s", run_id="run-1", modelId="m", messages=[])
        assert len(client.calls) == calls_before


# ---------------------------------------------------------------------------
# converse_text — the reply's text, whatever block the model put first. The
# reply shapes are the ones Bedrock returned on 2026-10-09 (Opus 5.5:
# [reasoningContent, text]), not the function's output.
# ---------------------------------------------------------------------------


def _reply(*blocks: dict, stop: str = "end_turn") -> dict:
    return {"output": {"message": {"role": "assistant", "content": list(blocks)}}, "stopReason": stop}


_REASONING = {"reasoningContent": {"reasoningText": {"text": "Split name on the first space.", "signature": "sig"}}}


class TestConverseText:
    def test_a_text_only_reply(self):
        assert converse_text(_reply({"text": '{"mappings": []}'})) == '{"mappings": []}'

    def test_a_reasoning_block_before_the_answer_is_skipped(self):
        # The shape that raised KeyError: 'text' through content[0]["text"].
        assert converse_text(_reply(_REASONING, {"text": '{"mappings": []}'})) == '{"mappings": []}'

    def test_text_split_across_blocks_reads_back_exactly(self):
        reply = _reply({"text": '{"mappings": '}, _REASONING, {"text": "[]}"})
        assert converse_text(reply) == '{"mappings": []}'

    def test_a_reply_with_no_text_names_what_it_held(self):
        with pytest.raises(ValueError, match=r"no text block.*max_tokens.*reasoningContent"):
            converse_text(_reply(_REASONING, stop="max_tokens"))

    def test_an_empty_reply_is_a_value_error_not_an_index_error(self):
        with pytest.raises(ValueError, match="no text block"):
            converse_text({"output": {"message": {"content": []}}})


# ---------------------------------------------------------------------------
# Output-token ceiling recovery. The message is Bedrock's, verbatim, as it
# answered on 2026-10-09 for Amazon Nova Lite (limit 10,000).
# ---------------------------------------------------------------------------

_NOVA_LIMIT_REFUSAL = (
    "The maximum tokens you requested exceeds the model limit of 10000. "
    "Try again with a maximum tokens value that is lower than 10000."
)


class CeilingClient:
    """Refuses any maxTokens above ``limit`` with Bedrock's wording; can also refuse temperature."""

    def __init__(self, limit: int = 10000, refuses_temperature: bool = False) -> None:
        self.limit = limit
        self.refuses_temperature = refuses_temperature
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        config = kwargs.get("inferenceConfig", {})
        if self.refuses_temperature and "temperature" in config:
            raise FakeClientError("ValidationException", _OBSERVED_REJECTION)
        if config.get("maxTokens", 0) > self.limit:
            raise FakeClientError(
                "ValidationException",
                f"The maximum tokens you requested exceeds the model limit of {self.limit}. "
                f"Try again with a maximum tokens value that is lower than {self.limit}.",
            )
        return {"output": {"message": {"content": [{"text": "ok"}]}}, "usage": {"inputTokens": 1}}


class TestOutputTokenCeilingRecovery:
    def test_a_ceiling_above_the_model_limit_is_retried_at_the_limit(self, caplog):
        client = CeilingClient(limit=10000)

        response = invoke_model(client, step_name="schema_mapping", modelId="us.amazon.nova-lite-v1:0", messages=[])

        assert response["output"]["message"]["content"][0]["text"] == "ok"
        assert [c["inferenceConfig"]["maxTokens"] for c in client.calls] == [DEFAULT_MAX_TOKENS, 10000]
        # Only the ceiling moved: temperature is still sent.
        assert client.calls[1]["inferenceConfig"]["temperature"] == DEFAULT_TEMPERATURE
        assert any("allows at most 10000 output tokens" in r.message for r in caplog.records)

    def test_the_limit_is_remembered_so_later_calls_pay_no_retry(self):
        client = CeilingClient(limit=10000)
        invoke_model(client, step_name="schema_mapping", modelId="us.amazon.nova-lite-v1:0", messages=[])
        invoke_model(client, step_name="schema_mapping", modelId="us.amazon.nova-lite-v1:0", messages=[])

        assert len(client.calls) == 3  # two for the first call, one for the second
        assert client.calls[2]["inferenceConfig"]["maxTokens"] == 10000

    def test_a_ceiling_within_the_limit_is_sent_unchanged(self):
        client = CeilingClient(limit=128000)

        invoke_model(client, step_name="schema_mapping", modelId="us.anthropic.claude-sonnet-4-6", messages=[])

        assert len(client.calls) == 1
        assert client.calls[0]["inferenceConfig"]["maxTokens"] == DEFAULT_MAX_TOKENS
        assert model_config._MODEL_OUTPUT_TOKEN_LIMITS == {}

    def test_both_refusals_are_recovered_once_each(self):
        # A model that refuses temperature AND a ceiling above its limit: two
        # retries, each fixing a different thing.
        client = CeilingClient(limit=10000, refuses_temperature=True)

        response = invoke_model(client, step_name="schema_mapping", modelId="us.example.new-model", messages=[])

        assert response["output"]["message"]["content"][0]["text"] == "ok"
        assert len(client.calls) == 3
        assert "temperature" not in client.calls[2]["inferenceConfig"]
        assert client.calls[2]["inferenceConfig"]["maxTokens"] == 10000

    def test_a_limit_that_would_not_lower_the_ceiling_propagates(self):
        # A refusal citing a limit at or above what was asked cannot be met by
        # lowering, so it is not retried.
        class OddClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def converse(self, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(kwargs)
                raise FakeClientError("ValidationException", _NOVA_LIMIT_REFUSAL.replace("10000", "999999"))

        client = OddClient()
        with pytest.raises(FakeClientError):
            invoke_model(client, step_name="schema_mapping", modelId="us.example.odd", messages=[])
        assert len(client.calls) == 1

    def test_a_non_validation_error_with_the_wording_is_not_recovered(self):
        class ThrottledClient:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def converse(self, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(kwargs)
                raise FakeClientError("ThrottlingException", _NOVA_LIMIT_REFUSAL)

        client = ThrottledClient()
        with pytest.raises(FakeClientError):
            invoke_model(client, step_name="schema_mapping", modelId="us.amazon.nova-lite-v1:0", messages=[])
        assert len(client.calls) == 1

    def test_the_retry_does_not_double_count_the_budget(self):
        client = CeilingClient(limit=10000)

        invoke_model(
            client, step_name="schema_mapping", run_id="run-ceiling", modelId="us.amazon.nova-lite-v1:0", messages=[]
        )

        assert get_call_budget("run-ceiling").local_count == 1
        assert len(client.calls) == 2
