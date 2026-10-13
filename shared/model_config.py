# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bedrock model configuration: canonical model IDs, per-call inference
parameters, and per-run model-call budgets.

Single source of truth for model IDs (P21 Req 1) and for Converse invocation
parameters and budgets (P28 R15).

Environment configuration (P28 R15.3 — sane defaults, documented)
    BEDROCK_MODEL_ID
        Operator override for the model ID. Default: ``MODEL_ID_DEFAULT``.
    MODEL_MAX_TOKENS
        ``maxTokens`` for every Converse call. Default: ``16000`` (was
        ``4000``, the value schema_mapping passed before P28); see
        ``DEFAULT_MAX_TOKENS`` for why, and for the wall-clock bound that
        caps it. Raising this past ~16,000 needs the schema mapping Lambda's
        timeout and client ``read_timeout`` raised with it.
    MODEL_TEMPERATURE
        ``temperature`` for every Converse call. Default: ``0.0`` — no call
        site set a temperature before P28 (they silently inherited the model
        default); our workloads are structured-output generation (JSON
        mappings, code) where deterministic, reproducible output is wanted,
        so the explicit default is 0.0.

        Some model generations REJECT this parameter outright. Anthropic's
        Claude Opus 4.7 model card states that ``temperature``, ``top_p`` and
        ``top_k`` "are no longer supported" starting with that model, and that
        the migration path is to omit them entirely. A Converse call carrying
        one fails with ``ValidationException`` naming the parameter.
        :func:`invoke_model` recovers from that by retrying once without the
        rejected parameter — see its docstring. The default stays 0.0 because
        it is still honoured by every model that accepts it.

        The determinism intent above is stated as the reason for 0.0 and is
        kept, but it was always weaker than it reads: the same model card notes
        that ``temperature = 0`` "did not guarantee identical responses across
        invocations". 0.0 lowers sampling variance; it never promised
        byte-identical output.
    MODEL_CALL_BUDGET
        Maximum model calls per run (R15.2). Default: ``50`` — an order of
        magnitude above a normal run's needs (a run makes a handful of
        model calls per AI step), while capping a runaway retry loop's cost.
    MODEL_CALL_BUDGET_TABLE
        Optional DynamoDB table name (partition key ``run_id``, string) for
        the cross-Lambda per-run call counter. When absent, budgets are
        enforced in-memory only (per Lambda container / per process), which
        keeps this module usable inside the AgentCore container without any
        extra infrastructure.

Configuration ownership (P28 R18.2, amended p34 R6): this module is the
single home for model budgets and per-call Converse parameters —
``shared/config.py`` (the home for approval-window/threshold/TTL constants)
deliberately does not duplicate them; it documents the split. The
budget-item TTL below is now owned outright here rather than derived from
``config.APPROVAL_WAIT_DAYS``: see ``BUDGET_ITEM_TTL_DAYS``.

Misconfigured values (non-numeric MODEL_MAX_TOKENS, budget < 1, …) raise
``ValueError`` immediately — honest failure over a silent fallback default.

Budget semantics (P28 R15.2)
    Exceeding the budget raises ``StepFailure(step_name, "model call budget
    exceeded", error_type="permanent")`` so the Step Functions task fails
    honestly per R1. The DynamoDB counter (when configured) is incremented
    atomically (``ADD``) and is authoritative across Lambdas/containers; the
    in-memory counter always enforces a per-process floor. If a configured
    DynamoDB increment fails at runtime (throttle, missing table), the
    failure is logged loudly and enforcement falls back to the in-memory
    count for that call — model calls stay available, and the degradation
    is visible in logs, never silent.
"""

import logging
import os
import re
import time
from typing import Any

from step_errors import StepFailure

logger = logging.getLogger(__name__)

# ── Canonical model IDs (P21 Req 1) ──────────────────────────────────

# Primary model — Claude 4.6 generation uses the dateless Bedrock inference-profile ID.
# The `us.` prefix selects the US cross-region inference profile.
MODEL_ID_DEFAULT = "us.anthropic.claude-sonnet-4-6"

# Fallback model used by code paths that intentionally default to a secondary model.
# Must be a valid Bedrock inference-profile ID, distinct from MODEL_ID_DEFAULT.
FALLBACK_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def resolve_model_id() -> str:
    """Return the operator override (BEDROCK_MODEL_ID) if set, else the default."""
    return os.environ.get("BEDROCK_MODEL_ID") or MODEL_ID_DEFAULT


# ── Per-call inference parameters (P28 R15.1, R15.3) ─────────────────

#: Default ``maxTokens``, a CEILING: Bedrock bills the tokens a reply actually
#: uses, so raising it costs nothing on a reply that finishes below it. 16,000
#: (was 4,000) because the one caller, schema mapping, writes roughly 200 output
#: tokens per target field (measured across 62 mapping calls in both dev
#: accounts: median 1,038-1,182 and at most 1,501 output tokens, for test schemas
#: of a handful of target fields; the 2026-10-09 run mapped 7 in 1,224), and a
#: model that reasons first spends
#: its reasoning from the same budget. 4,000 therefore truncated a mapping at
#: roughly 20 target fields, and a truncated reply falls back to substring
#: matching. 16,000 covers about 70 fields with room to reason. It is bounded
#: by wall clock, not cost: ``converse`` is non-streaming, so the schema
#: mapping client's ``read_timeout`` has to cover a full 16,000-token reply and
#: two attempts have to fit in that Lambda's timeout (``LGR-b397ee2-4``; the
#: arithmetic is at ``_BEDROCK_READ_TIMEOUT_SECONDS`` in
#: ``schema_mapping/lambda_function.py``). A model whose own limit is lower is
#: clamped to it on first contact (``_MODEL_OUTPUT_TOKEN_LIMITS``).
DEFAULT_MAX_TOKENS = 16000

#: Default ``temperature`` — deterministic structured output. See module docstring.
DEFAULT_TEMPERATURE = 0.0


def _env_int(name: str, default: int) -> int:
    """Read an integer env var; empty/absent → default; garbage → ValueError."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    """Read a float env var; empty/absent → default; garbage → ValueError."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number, got {raw!r}") from exc


def converse_params() -> dict[str, Any]:
    """Explicit per-call Converse ``inferenceConfig`` values (R15.1).

    Returns:
        A fresh ``{"maxTokens": int, "temperature": float}`` dict read from
        ``MODEL_MAX_TOKENS`` / ``MODEL_TEMPERATURE`` with the documented
        defaults. Callers may mutate the returned dict freely.
    """
    return {
        "maxTokens": _env_int("MODEL_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        "temperature": _env_float("MODEL_TEMPERATURE", DEFAULT_TEMPERATURE),
    }


# ── Per-run model-call budget (P28 R15.2) ────────────────────────────

#: Default per-run model-call budget. See module docstring for rationale.
DEFAULT_MODEL_CALL_BUDGET = 50

#: Env var naming the optional DynamoDB budget table (pk ``run_id``, string).
BUDGET_TABLE_ENV = "MODEL_CALL_BUDGET_TABLE"

#: step_name used on raised StepFailures when the caller does not identify
#: the step (mirrors agentcore_proxy's convention).
DEFAULT_STEP_NAME = "model_invocation"

#: Self-cleanup TTL on the per-run budget counter item, in days. 8 days.
#:
#: This was ``config.APPROVAL_WAIT_DAYS + 1`` — 7 + 1 — under P28 R18.2, on
#: the reasoning that a budget item should outlive the longest legitimate run
#: and the longest legitimate run was an approval wait plus a job. p34 R6
#: shortened the advertised approval window to 72 h, and the derivation would
#: have quietly dragged this TTL from 8 days to 4 as a side effect. It is
#: pinned at the value it has always held instead, because a housekeeping TTL
#: on a call counter is not a function of how long an approver is given: what
#: it has to outlive is a *run*, and a run can be long for reasons that have
#: nothing to do with approvals (a slow Glue job, retries, a resumed
#: execution). Nothing depends on it being tight — one small item per run on
#: a PAY_PER_REQUEST table — while reaping it early silently resets a
#: cross-Lambda budget mid-run, which is the failure that matters.
BUDGET_ITEM_TTL_DAYS = 8

_BUDGET_ITEM_TTL_SECONDS = BUDGET_ITEM_TTL_DAYS * 24 * 3600


class CallBudget:
    """Per-run model-call budget (R15.2).

    Counts one unit per :meth:`check_and_increment` and raises a permanent
    ``StepFailure`` once the count exceeds ``max_calls``. Two counters:

    - **In-memory** (``self.local_count``): always maintained; the only
      enforcement when ``MODEL_CALL_BUDGET_TABLE`` is not configured.
    - **DynamoDB** (optional): when ``MODEL_CALL_BUDGET_TABLE`` is set, each
      check also performs an atomic ``ADD call_count 1`` on the run's item
      and enforces against the returned per-run total, so the budget holds
      across Lambda containers and the AgentCore container. Items carry an
      ``expires_at`` TTL attribute (8 days) for self-cleanup.

    Args:
        run_id: The pipeline run this budget belongs to.
        max_calls: Budget ceiling. ``None`` (default) reads
            ``MODEL_CALL_BUDGET`` (default ``50``). Must be >= 1.
    """

    def __init__(self, run_id: str, max_calls: int | None = None) -> None:
        if max_calls is None:
            max_calls = _env_int("MODEL_CALL_BUDGET", DEFAULT_MODEL_CALL_BUDGET)
        if max_calls < 1:
            raise ValueError(f"max_calls must be >= 1, got {max_calls}")
        self.run_id = run_id
        self.max_calls = max_calls
        self.local_count = 0
        # Captured at construction: one budget instance serves one run, and
        # the table configuration does not change mid-run.
        self._table_name: str | None = os.environ.get(BUDGET_TABLE_ENV) or None
        self._table: Any = None

    def check_and_increment(self, step_name: str = DEFAULT_STEP_NAME) -> int:
        """Count one model call; raise when the budget is exceeded.

        Args:
            step_name: Canonical step name carried on the StepFailure when
                the budget is exceeded.

        Returns:
            The call total after this increment (the per-run DynamoDB total
            when the table is configured and reachable, else this process's
            count).

        Raises:
            StepFailure: ``error_type="permanent"``, message contains
                "model call budget exceeded" (R15.2 → fails the step per R1).
        """
        self.local_count += 1
        total = self.local_count
        if self._table_name is not None:
            ddb_total = self._increment_dynamodb()
            if ddb_total is not None:
                total = ddb_total
        if total > self.max_calls:
            raise StepFailure(
                step_name,
                f"model call budget exceeded: {total} calls > limit {self.max_calls} for run {self.run_id}",
                error_type="permanent",
                details={
                    "run_id": self.run_id,
                    "call_count": total,
                    "max_calls": self.max_calls,
                },
            )
        return total

    def _increment_dynamodb(self) -> int | None:
        """Atomically ADD 1 to the run's counter; return the new per-run total.

        Returns ``None`` (logging loudly) when the increment fails, so a
        budget-table outage degrades to in-memory enforcement for that call
        instead of failing otherwise-healthy model calls. This degradation
        is bounded (the in-memory floor still applies) and visible in logs.
        """
        try:
            table = self._get_table()
            response = table.update_item(
                Key={"run_id": self.run_id},
                UpdateExpression="ADD call_count :inc SET expires_at = if_not_exists(expires_at, :exp)",
                ExpressionAttributeValues={
                    ":inc": 1,
                    ":exp": int(time.time()) + _BUDGET_ITEM_TTL_SECONDS,
                },
                ReturnValues="UPDATED_NEW",
            )
            return int(response["Attributes"]["call_count"])
        except Exception:
            logger.exception(
                "CallBudget: DynamoDB increment failed for run %s (table %s) — "
                "enforcing in-memory count only for this call",
                self.run_id,
                self._table_name,
            )
            return None

    def _get_table(self) -> Any:
        """Lazily create the boto3 Table resource (kept off the import path)."""
        if self._table is None:
            if self._table_name is None:  # narrowing guard; callers check first
                raise RuntimeError(f"CallBudget table requested without {BUDGET_TABLE_ENV} set")
            import boto3

            self._table = boto3.resource("dynamodb").Table(self._table_name)
        return self._table


# Per-container budget registry so warm Lambda invocations and repeated
# invoke_model() calls within one invocation share the in-memory counter.
# Bounded so a long-lived container cannot grow it without limit; evicting
# an old run's entry only resets its *in-memory* floor (the DynamoDB
# counter, when configured, is unaffected).
_MAX_CACHED_BUDGETS = 256
_BUDGETS: dict[str, CallBudget] = {}


def get_call_budget(run_id: str) -> CallBudget:
    """Return the container-cached :class:`CallBudget` for ``run_id``.

    The first call for a run constructs the budget (reading env config at
    that moment); later calls return the same instance so the in-memory
    count accumulates across :func:`invoke_model` calls.
    """
    budget = _BUDGETS.get(run_id)
    if budget is None:
        budget = CallBudget(run_id)
        _BUDGETS[run_id] = budget
        while len(_BUDGETS) > _MAX_CACHED_BUDGETS:
            _BUDGETS.pop(next(iter(_BUDGETS)))
    return budget


# ── Sampling parameters a model generation may refuse (p67) ──────────
#
# Converse's ``inferenceConfig`` carries two sampling parameters we can drop
# and still make the call: ``temperature`` and ``topP``. ``maxTokens`` and
# ``stopSequences`` are NOT droppable — removing maxTokens changes what the
# model is allowed to produce, so this recovery never drops either. The one
# ``maxTokens`` refusal that can be met, a ceiling above the model's own limit,
# is lowered to that limit by the output-token recovery below, not dropped.
#
# Each entry maps the Converse key to the spellings a Bedrock error message
# may use for it, so the match does not depend on the service echoing our
# camelCase key back.
_DROPPABLE_SAMPLING_PARAMS: dict[str, tuple[str, ...]] = {
    "temperature": ("temperature",),
    "topP": ("topp", "top_p"),
}

#: Per-container memo of model ids observed to reject a sampling parameter.
#:
#: LEARNED from the API, never declared — this is deliberately not a
#: capability list keyed on model id. The model id is open-world here
#: (``BEDROCK_MODEL_ID`` is an operator override and schema_mapping also
#: accepts a per-pipeline model), so a hardcoded list would go stale silently
#: every time a new model generation ships, and going stale means a rejected
#: Converse call — the failure this memo exists to stop repeating.
#:
#: Bounded and evicted like ``_BUDGETS``; losing an entry only costs one
#: rejected call and one retry, never correctness.
_MAX_CACHED_SAMPLING_SUPPORT = 64
_UNSUPPORTED_SAMPLING_PARAMS: dict[str, frozenset[str]] = {}


def _remember_unsupported_sampling_params(model_id: str, params: frozenset[str]) -> None:
    """Record that ``model_id`` refuses ``params``, bounding the memo."""
    _UNSUPPORTED_SAMPLING_PARAMS[model_id] = _UNSUPPORTED_SAMPLING_PARAMS.get(model_id, frozenset()) | params
    while len(_UNSUPPORTED_SAMPLING_PARAMS) > _MAX_CACHED_SAMPLING_SUPPORT:
        _UNSUPPORTED_SAMPLING_PARAMS.pop(next(iter(_UNSUPPORTED_SAMPLING_PARAMS)))


# ── Output-token ceilings, learned from the API ──────────────────────
#
# ``maxTokens`` is a ceiling, billed only for what the model actually writes,
# but Bedrock refuses a ceiling above the model's own limit. The default here
# (16,000) is above some models' limits: on 2026-10-09 Bedrock reported 10,000
# for Amazon Nova Micro, Lite and Pro and 128,000 for Claude Sonnet 4.6,
# Sonnet 5.5 and Opus 5.5, accepting exactly the stated figure and refusing one
# more. The refusal is a ValidationException worded, for every model probed,
#
#     The maximum tokens you requested exceeds the model limit of 10000. Try
#     again with a maximum tokens value that is lower than 10000.
#
# so :func:`invoke_model` reads the limit out of it, retries once at the limit,
# and remembers it per container. Learned, not declared, for the same reason as
# ``_UNSUPPORTED_SAMPLING_PARAMS``: the model id is open-world.
_OUTPUT_TOKEN_LIMIT_RE = re.compile(r"exceeds the model limit of (\d+)")
_MODEL_OUTPUT_TOKEN_LIMITS: dict[str, int] = {}


def _output_token_limit(exc: Exception) -> int | None:
    """The model's output-token limit, if ``exc`` is Bedrock refusing ``maxTokens``."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict) or error.get("Code") != "ValidationException":
        return None
    match = _OUTPUT_TOKEN_LIMIT_RE.search(str(error.get("Message", "") or "") + " " + str(exc))
    if match is None:
        return None
    limit = int(match.group(1))
    return limit if limit > 0 else None


def _remember_output_token_limit(model_id: str, limit: int) -> None:
    """Record ``model_id``'s output-token limit, bounding the memo like the others."""
    _MODEL_OUTPUT_TOKEN_LIMITS[model_id] = limit
    while len(_MODEL_OUTPUT_TOKEN_LIMITS) > _MAX_CACHED_SAMPLING_SUPPORT:
        _MODEL_OUTPUT_TOKEN_LIMITS.pop(next(iter(_MODEL_OUTPUT_TOKEN_LIMITS)))


def _rejected_sampling_params(exc: Exception, inference_config: dict[str, Any]) -> frozenset[str]:
    """Which droppable sampling params a Converse failure blames, if any.

    Answers a non-empty set only when BOTH hold:

    1. the exception is a ``ValidationException`` — read by duck-typing on
       ``exc.response["Error"]["Code"]`` rather than by catching
       ``botocore.exceptions.ClientError``, because this module keeps botocore
       off its import path (the only boto3 import is lazy, inside
       ``CallBudget._get_table``);
    2. its message names at least one parameter in
       :data:`_DROPPABLE_SAMPLING_PARAMS` that this call actually sent.

    Condition 2 is what keeps this from being a blind retry-on-400: a
    ``ValidationException`` about a malformed message list names no sampling
    parameter, so it answers empty and the caller re-raises. Matching the
    message text is fragile and is used because the error code alone does not
    identify which parameter was refused — the set is intersected with what was
    actually sent, so a message mentioning a parameter we did not pass cannot
    trigger a pointless retry.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return frozenset()
    error = response.get("Error")
    if not isinstance(error, dict) or error.get("Code") != "ValidationException":
        return frozenset()
    message = str(error.get("Message", "") or "") + " " + str(exc)
    lowered = message.lower()
    return frozenset(
        key
        for key, aliases in _DROPPABLE_SAMPLING_PARAMS.items()
        if key in inference_config and any(alias in lowered for alias in aliases)
    )


def invoke_model(
    client: Any,
    *,
    step_name: str,
    run_id: str | None = None,
    **converse_kwargs: Any,
) -> dict[str, Any]:
    """Thin Bedrock Converse wrapper: explicit params + budget (R15.1, R15.2).

    Drop-in for direct ``client.converse(...)`` calls — existing call sites
    keep their kwargs (``modelId``, ``messages``, ``system``, …) unchanged::

        response = invoke_model(
            bedrock_runtime,
            step_name="schema_mapping",
            run_id=run_id,
            modelId=selected_model,
            messages=[...],
        )

    Behavior:
        - Merges :func:`converse_params` into ``inferenceConfig`` so every
          call carries explicit ``maxTokens`` and ``temperature``; values the
          caller passes in ``inferenceConfig`` win over the shared defaults.
        - When ``run_id`` is given, enforces the per-run call budget BEFORE
          calling Converse (an exhausted budget never reaches Bedrock).
        - Recovers from a model generation that refuses a sampling parameter:
          see below.
        - Returns the Converse response unchanged.

    Sampling-parameter recovery (p67):
        Newer model generations reject ``temperature`` / ``topP`` outright —
        Anthropic's Claude Opus 4.7 model card says they "are no longer
        supported" and that requests should omit them. Such a call fails with
        ``ValidationException`` naming the parameter, and before this recovery
        existed that exception reached ``generate_mapping_with_bedrock``'s
        outer ``except Exception`` and silently degraded an AI mapping into
        substring matching.

        So: when Converse fails with a ``ValidationException`` that names a
        droppable sampling parameter this call actually sent, the parameter is
        removed and the call is retried **once**. The refusal is remembered per
        container (:data:`_UNSUPPORTED_SAMPLING_PARAMS`) so later calls to the
        same model pre-strip it and pay no second round trip. Any other
        failure — including a ``ValidationException`` naming nothing droppable
        — propagates untouched, apart from the ceiling refusal below.

    Output-token-ceiling recovery:
        A ``maxTokens`` above the model's own limit fails with a
        ``ValidationException`` stating that limit (see
        :data:`_OUTPUT_TOKEN_LIMIT_RE`). The call is retried once with
        ``maxTokens`` lowered to the limit, which is the most that model can
        produce anyway, and the limit is remembered per container. Each kind of
        recovery happens at most once per call, so a model refusing both a
        sampling parameter and the ceiling costs two retries at most, and a
        refusal the call has already recovered from propagates.

        **The budget counts one call, not two.** The budget check happens once,
        before the first attempt, so the retry cannot double-count. The retry
        is a second wire call inside one budgeted logical call, and that is
        stated rather than hidden: it is bounded at one per kind of refusal,
        only reachable on a first-contact rejection for a given model in a
        given container, and the
        alternative (counting it) would let a model-capability quirk consume a
        run's budget twice as fast.

        Both the retry and an unrecoverable failure log loudly — the retry at
        WARNING (recovered, with a stated loss of the temperature setting), a
        failure that cannot be recovered at ERROR with the model id, because a
        Converse rejection that reaches a caller's blanket ``except`` is how
        this defect stayed invisible.

    Args:
        client: A ``bedrock-runtime`` boto3 client (anything with a
            ``.converse(**kwargs)`` method).
        step_name: Canonical step name for budget StepFailures.
        run_id: Enables per-run budget enforcement when provided.
        **converse_kwargs: Passed through to ``client.converse`` verbatim
            (after the ``inferenceConfig`` merge).

    Raises:
        StepFailure: When the model-call budget is exceeded (permanent).
    """
    if run_id is not None:
        get_call_budget(run_id).check_and_increment(step_name)
    caller_config: dict[str, Any] = converse_kwargs.pop("inferenceConfig", None) or {}
    inference_config = {**converse_params(), **caller_config}

    model_id = str(converse_kwargs.get("modelId", "") or "")
    known_unsupported = _UNSUPPORTED_SAMPLING_PARAMS.get(model_id)
    if known_unsupported:
        inference_config = {k: v for k, v in inference_config.items() if k not in known_unsupported}
    known_limit = _MODEL_OUTPUT_TOKEN_LIMITS.get(model_id)
    if known_limit is not None and isinstance(inference_config.get("maxTokens"), int):
        inference_config["maxTokens"] = min(inference_config["maxTokens"], known_limit)

    # At most one retry per KIND of refusal: sampling parameters, then the
    # output-token ceiling. Each recovery has to change the request, so a
    # refusal that names neither, or that names one already handled, ends the
    # loop and propagates.
    recovered: set[str] = set()
    while True:
        try:
            response: dict[str, Any] = client.converse(inferenceConfig=inference_config, **converse_kwargs)
            return response
        except Exception as exc:
            rejected = _rejected_sampling_params(exc, inference_config) if "sampling" not in recovered else frozenset()
            limit = _output_token_limit(exc) if "max_tokens" not in recovered else None
            requested = inference_config.get("maxTokens")
            clamps = limit is not None and isinstance(requested, int) and limit < requested
            if not rejected and not clamps:
                logger.error(
                    "invoke_model: Converse call REJECTED and not recoverable — step=%s model=%s: %s: %s",
                    step_name,
                    model_id or "<unset>",
                    type(exc).__name__,
                    exc,
                )
                raise
            if rejected:
                recovered.add("sampling")
                _remember_unsupported_sampling_params(model_id, rejected)
                logger.warning(
                    "invoke_model: model %s refuses inference parameter(s) %s — retrying once without them "
                    "(step=%s). The setting is not applied for this model: %s",
                    model_id or "<unset>",
                    sorted(rejected),
                    step_name,
                    exc,
                )
                inference_config = {k: v for k, v in inference_config.items() if k not in rejected}
            elif limit is not None:  # always true here: `clamps` required it
                recovered.add("max_tokens")
                _remember_output_token_limit(model_id, limit)
                logger.warning(
                    "invoke_model: model %s allows at most %d output tokens; retrying once with maxTokens=%d "
                    "instead of %s (step=%s)",
                    model_id or "<unset>",
                    limit,
                    limit,
                    requested,
                    step_name,
                )
                inference_config = {**inference_config, "maxTokens": limit}


def converse_text(response: dict[str, Any]) -> str:
    """The reply text of a Converse response: every ``text`` block, in order.

    A Converse reply is a LIST of content blocks, and the first one is not
    always text. Models with adaptive reasoning put a ``reasoningContent`` block
    before the answer. Verified on Bedrock on 2026-10-09: Claude Opus 5.5
    answered ``[reasoningContent, text]`` on every call to a mapping prompt.
    Claude Sonnet 5.5 answered ``[text]`` to a short prompt, yet on a production
    mapping prompt its first block had no ``text`` key, so it led with something
    else; a reasoning block is the likely one. Reading
    ``content[0]["text"]`` therefore raised ``KeyError: 'text'`` on exactly the
    calls where the model reasoned first, and ``generate_mapping_with_bedrock``'s
    outer ``except Exception`` turned a good model reply into the substring
    fallback.

    Reasoning blocks are skipped, not returned: they are the model's working,
    not its answer, and the callers parse the answer. Text blocks are joined
    with no separator so a reply split across blocks reads back exactly as
    written.

    Raises:
        ValueError: When the reply holds no text block at all. The message
            names the block types it did hold, so the failure says what the
            model returned instead of surfacing as a bare ``KeyError``.
    """
    blocks = (response.get("output") or {}).get("message", {}).get("content") or []
    texts = [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]
    if not texts:
        kinds = [next(iter(b), "?") if isinstance(b, dict) and b else type(b).__name__ for b in blocks]
        raise ValueError(
            f"Converse reply holds no text block (stopReason={response.get('stopReason')!r}, blocks={kinds})"
        )
    return "".join(texts)
