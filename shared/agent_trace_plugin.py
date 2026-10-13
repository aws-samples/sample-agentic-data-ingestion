# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Agent trace plugin for capturing structured observability data from Strands agents.

Implements the Strands ``HookProvider`` protocol to capture model invocations,
MCP tool calls, token usage, and estimated cost during agent execution.  Trace
data is written to the ``agentic_data_ingestion_agent_traces`` DynamoDB table on ``flush()``.

Failures are logged but never propagated — callers are guaranteed that trace
writes will not affect the agent's response.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger

from env_utils import require_env

if TYPE_CHECKING:
    from strands.hooks.events import (
        AfterInvocationEvent,
        AfterModelCallEvent,
        AfterToolCallEvent,
        BeforeInvocationEvent,
        BeforeModelCallEvent,
        BeforeToolCallEvent,
    )
    from strands.hooks.registry import HookRegistry

logger = Logger(service="agentic_data_ingestion")

# ---------------------------------------------------------------------------
# Cost estimation model — configurable per-model pricing
# ---------------------------------------------------------------------------

# Prices are per 1 000 tokens (input / output).
# Cache-read tokens are free (already paid on first invocation).
MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    "default": {
        "input_per_1k": Decimal("0.003"),
        "output_per_1k": Decimal("0.015"),
    },
    "claude-sonnet-4-5": {
        "input_per_1k": Decimal("0.003"),
        "output_per_1k": Decimal("0.015"),
    },
    "claude-sonnet-4": {
        "input_per_1k": Decimal("0.003"),
        "output_per_1k": Decimal("0.015"),
    },
    "nova-pro": {
        "input_per_1k": Decimal("0.0008"),
        "output_per_1k": Decimal("0.0032"),
    },
    "nova-micro": {
        "input_per_1k": Decimal("0.000035"),
        "output_per_1k": Decimal("0.00014"),
    },
}

_PREVIEW_MAX_CHARS = 10_000
_TTL_SECONDS = 90 * 86_400  # 90 days

_dynamodb = boto3.resource("dynamodb")
_traces_table: Any = None


def _get_traces_table() -> Any:
    """Lazy-init the traces table on first use, not at import time."""
    global _traces_table
    if _traces_table is None:
        table_name = require_env("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
        _traces_table = _dynamodb.Table(table_name)
    return _traces_table


def _truncate(value: str, max_chars: int = _PREVIEW_MAX_CHARS) -> str:
    """Truncate a string to *max_chars*, appending '…' when trimmed."""
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1] + "…"


def _safe_json(obj: Any, max_chars: int = _PREVIEW_MAX_CHARS) -> str:
    """Serialise *obj* to a truncated JSON string, swallowing errors."""
    try:
        return _truncate(json.dumps(obj, default=str), max_chars)
    except Exception:
        return _truncate(str(obj), max_chars)


# ---------------------------------------------------------------------------
# Sample-value redaction (p52 C5)
# ---------------------------------------------------------------------------
#
# LEDGER GAP, stated as a gap and not as an all-clear.  Nothing in the decision
# ledger governed this module before p52: ``why`` on it returns four broad
# snapshot commits — ``2b45b72912``, ``7ad6ca1ad9``, ``7c18e81fcc``,
# ``b48a349b7b`` — every one of them with ``records: []``, and
# ``decisions(scope=…)`` returns only ``LGR-d5bcee9-1`` (about cdk.out) and
# ``LGR-819980f-1`` (the JDBC-credentials invariant, scoped ``shared/**``).
# The **same** gap covers ``agentcore/complex_mapping_agent/main.py``, the other
# file p52 task 3.1 touches: five broad snapshots, all ``records: []``, and one
# unrelated scoped record (``LGR-d5bcee9-1`` again).  So the redaction posture
# below is not constrained by any prior ruling — it is not that a ruling
# permitted it.  ``LGR-1e135d2-4``'s ``revisit_when`` ("the trace table's
# retention/redaction posture changes") is the record this change answers to.

_SAMPLE_MARKER = "Sample values: ["

# Substituted for a whole Preview_Field when the redactor raises.  A fixed
# literal, carrying no source-derived text (R7.3).
_REDACTION_FAILED = "[trace redaction failed — preview withheld]"

# The four free-text fields a trace carries per call.  All four are redacted:
# the redactor is marker-keyed and inert without the marker (R5.6), so covering
# the three that rarely carry a Sample_Block costs nothing and closes the paths
# where a tool echoes a prompt fragment back.
_PREVIEW_FIELDS = ("prompt_preview", "response_preview", "tool_params", "result_preview")

# A complete quoted run, i.e. one rendered sample value.  Both producers render
# the list as ``", ".join(f'"{v}"')`` — schema_mapping's
# ``render_sample_values_section`` (lambda_function.py:1012) and
# complex_mapping_agent's ``build_evidence_prompt`` (main.py:291) — so counting
# these counts values.  Cited by name as well as line: p52 gate 1.2 found three
# line cites in this area already stale.
_QUOTED_RUN = re.compile(r'"[^"]*"')


def _redact_sample_values(text: str) -> str:
    """Replace every Sample_Block's value list with a count.

    For each occurrence of the literal marker ``Sample values: [`` everything
    from the marker to the **end of that line** is discarded and replaced with
    ``Sample values: [N values redacted]``.  A string carrying no marker is
    returned unchanged (R5.6).  No AWS call, no network call, no file read
    (R7.5).

    End-of-line is the boundary rather than the matching ``]`` because
    ``_truncate_sample_value`` escapes nothing: a sample value containing ``]``
    or ``"`` defeats any bracket- or quote-balancing parse, and a leaked value
    is the failure this exists to prevent.  Discarding to end of line cannot
    leak a value that stays on its line, whatever else it contains.

    **What N means, and what it reads in the newline case.**  N counts complete
    quoted runs in the discarded span, so it is exact for both producers' normal
    output — every rendered value is one ``"…"`` run.  It is *not* derivable
    when the span ends on an unterminated quote, which happens two ways:

    * a sample value carries an embedded line break, so the marker's line holds
      only the head of the list (p52 risk N1, rendered by gate 1.2); or
    * a value contains a raw or JSON-escaped ``"``, so runs straddle values.

    Both read ``1+ values redacted, exact count unavailable: <reason>``.  Two
    things that shape says deliberately.  First, **never a zero**: a naive count
    reads ``0`` in the newline case for a field that had one value, and ``0``
    again for a three-value list, so the count is not merely imprecise there but
    *inverted* — and an inverted count is worse than an admitted floor.  Second,
    **no computed floor either**: ``complete + 1`` looks sound and is not, because
    an escaped ``"`` makes runs straddle values, so the computed floor can exceed
    the true count.  ``1+`` is the only claim the span always supports.

    The newline half of that is closed **at the producers**, which is why it
    should not arise in practice: ``_truncate_sample_value``
    (``schema_mapping/lambda_function.py:958``) and ``read_sample_data``
    (``agentcore/complex_mapping_agent/main.py:133``) both collapse ``\\n`` and
    ``\\r`` to spaces before a value is ever rendered.  Redaction here is total
    only while that holds — a residue on an unmarked line carries no marker, so
    no later pass reaches it.  The count is a diagnostic; the removal is the
    requirement.

    One accepted cost of the end-of-line boundary: a marker inside a
    JSON-serialised field (``tool_params`` via :func:`_safe_json`) has no real
    line break after it, so the discard runs to the end of that field.  That
    over-deletes the JSON tail — a diagnostic loss, never a leak.
    """
    if not text or _SAMPLE_MARKER not in text:
        return text

    out: list[str] = []
    pos = 0
    marker_len = len(_SAMPLE_MARKER)
    while True:
        start = text.find(_SAMPLE_MARKER, pos)
        if start == -1:
            out.append(text[pos:])
            break

        # End of the marker's own line.  Both \n and \r terminate, so a lone
        # \r cannot carry a residue past the boundary.
        eol = len(text)
        for newline in ("\n", "\r"):
            idx = text.find(newline, start)
            if idx != -1:
                eol = min(eol, idx)

        discarded = text[start + marker_len : eol]
        if discarded.count('"') % 2:
            # Unterminated quoted run: the span stops part-way through a value,
            # so no exact count is derivable — see the docstring for why a
            # computed floor is unsound here.  "1+" is: a value began.
            reason = "list continued past a line break" if eol < len(text) else "unbalanced quoting"
            summary = f"1+ values redacted, exact count unavailable: {reason}"
        else:
            summary = f"{len(_QUOTED_RUN.findall(discarded))} values redacted"

        out.append(text[pos:start])
        out.append(f"{_SAMPLE_MARKER}{summary}]")
        pos = eol

    return "".join(out)


def _lookup_pricing(model_id: str) -> dict[str, Decimal]:
    """Return pricing dict for *model_id*, falling back to ``default``."""
    model_lower = model_id.lower()
    for key in MODEL_PRICING:
        if key != "default" and key in model_lower:
            return MODEL_PRICING[key]
    return MODEL_PRICING["default"]


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model_id: str = "",
) -> Decimal:
    """Calculate estimated USD cost from token counts and model pricing."""
    pricing = _lookup_pricing(model_id)
    input_cost = Decimal(str(input_tokens)) / Decimal("1000") * pricing["input_per_1k"]
    output_cost = Decimal(str(output_tokens)) / Decimal("1000") * pricing["output_per_1k"]
    return input_cost + output_cost


# ---------------------------------------------------------------------------
# AgentTracePlugin — Strands HookProvider
# ---------------------------------------------------------------------------


class AgentTracePlugin:
    """Captures structured trace data from Strands agent execution.

    Implements the Strands ``HookProvider`` protocol.  Register an instance
    via ``Agent(hooks=[plugin])`` and call ``flush()`` after the agent
    invocation completes to persist the trace to DynamoDB.

    Args:
        pipeline_id: Pipeline identifier for the current run.
        run_id: Unique run identifier.
        step_name: Pipeline step (e.g. ``schema_mapping``, ``code_generation``).
    """

    def __init__(self, pipeline_id: str, run_id: str, step_name: str) -> None:
        self.pipeline_id = pipeline_id
        self.run_id = run_id
        self.step_name = step_name

        # Accumulated trace data
        self._model_calls: list[dict[str, Any]] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_cache_read_tokens: int = 0
        self._model_id: str = ""
        self._start_time: float | None = None
        self._end_time: float | None = None
        self._error: str | None = None

        # In-flight tracking for pairing before/after events
        self._current_model_call_start: float | None = None
        self._current_prompt_text: str = ""
        self._current_tool_call: dict[str, Any] | None = None

    # -- HookProvider protocol ------------------------------------------------

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register callbacks for all agent lifecycle events we care about."""
        # Import event types locally so the module can be imported even when
        # strands is not installed (e.g. during mypy / lint on dev machines).
        from strands.hooks.events import (
            AfterInvocationEvent,
            AfterModelCallEvent,
            AfterToolCallEvent,
            BeforeInvocationEvent,
            BeforeModelCallEvent,
            BeforeToolCallEvent,
        )

        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)
        registry.add_callback(AfterInvocationEvent, self._on_after_invocation)
        registry.add_callback(BeforeModelCallEvent, self._on_before_model)
        registry.add_callback(AfterModelCallEvent, self._on_after_model)
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool)

    # -- Invocation callbacks -------------------------------------------------

    def _on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        self._start_time = time.time()

    def _on_after_invocation(self, event: AfterInvocationEvent) -> None:
        self._end_time = time.time()
        # Extract accumulated token usage from AgentResult metrics
        result = event.result
        if result is not None and hasattr(result, "metrics"):
            usage = getattr(result.metrics, "accumulated_usage", {}) or {}
            self._total_input_tokens = usage.get("inputTokens", 0)
            self._total_output_tokens = usage.get("outputTokens", 0)
            self._total_cache_read_tokens = usage.get("cacheReadInputTokens", 0)
        # Capture error if the agent stopped due to an error
        if result is not None and hasattr(result, "stop_reason"):
            if result.stop_reason in ("content_filtered", "guardrail_intervened"):
                self._error = f"stop_reason={result.stop_reason}"

    # -- Model call callbacks -------------------------------------------------

    def _on_before_model(self, event: BeforeModelCallEvent) -> None:
        self._current_model_call_start = time.time()
        # Capture the prompt from the agent's conversation messages.
        # The last user-role message(s) represent the prompt sent to the model.
        self._current_prompt_text = ""
        try:
            if hasattr(event, "agent") and hasattr(event.agent, "messages"):
                messages = event.agent.messages
                # Extract text from the last user message (the prompt)
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        parts: list[str] = []
                        for block in msg.get("content", []):
                            if isinstance(block, dict) and "text" in block:
                                parts.append(block["text"])
                            elif isinstance(block, str):
                                parts.append(block)
                        self._current_prompt_text = "\n".join(parts)
                        break
        except Exception:
            pass  # Never let trace capture break the agent

    def _on_after_model(self, event: AfterModelCallEvent) -> None:
        start = self._current_model_call_start or time.time()
        end = time.time()
        duration_ms = (end - start) * 1000
        self._current_model_call_start = None

        # Extract model_id from the agent's model config dict.
        # Strands models store model_id in model.config, not as a direct attribute.
        model_id = ""
        if hasattr(event, "agent") and hasattr(event.agent, "model"):
            model = event.agent.model
            if hasattr(model, "config") and isinstance(model.config, dict):
                model_id = model.config.get("model_id", "")
            else:
                model_id = getattr(model, "model_id", "")
        if model_id:
            self._model_id = model_id

        # Build the model call record
        call: dict[str, Any] = {
            "model_id": model_id,
            "duration_ms": round(duration_ms, 1),
            "prompt_preview": _truncate(getattr(self, "_current_prompt_text", "") or ""),
            "response_preview": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
        }

        if event.stop_response is not None:
            # Extract per-call token usage from the stop_response
            usage = getattr(event.stop_response, "usage", None) or {}
            if isinstance(usage, dict):
                call["input_tokens"] = usage.get("inputTokens", 0)
                call["output_tokens"] = usage.get("outputTokens", 0)
                call["cache_read_tokens"] = usage.get("cacheReadInputTokens", 0)

            # Extract response preview from the message content
            msg = event.stop_response.message
            content_blocks = msg.get("content", [])
            text_parts: list[str] = []
            for block in content_blocks:
                if "text" in block:
                    text_parts.append(block["text"])
            call["response_preview"] = _truncate(" ".join(text_parts))

        if event.exception is not None:
            call["error"] = str(event.exception)[:_PREVIEW_MAX_CHARS]

        self._model_calls.append(call)

    # -- Tool call callbacks --------------------------------------------------

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        self._current_tool_call = {
            "start_time": time.time(),
            "tool_name": tool_use.get("name", "unknown"),
            "tool_params": _safe_json(tool_use.get("input", {})),
        }

    def _on_after_tool(self, event: AfterToolCallEvent) -> None:
        current = self._current_tool_call
        if current is None:
            return
        self._current_tool_call = None

        end = time.time()
        duration_ms = (end - current["start_time"]) * 1000

        # Determine status from the ToolResult
        result = event.result
        status: str = "success"
        result_preview: str = ""
        if event.exception is not None:
            status = "error"
            result_preview = str(event.exception)[:_PREVIEW_MAX_CHARS]
        elif result is not None:
            status = result.get("status", "success")
            # Build preview from result content blocks
            content_blocks = result.get("content", [])
            parts: list[str] = []
            for block in content_blocks:
                if "text" in block:
                    parts.append(block["text"])
                elif "json" in block:
                    parts.append(_safe_json(block["json"]))
            result_preview = _truncate(" ".join(parts))

        self._tool_calls.append(
            {
                "tool_name": current["tool_name"],
                "tool_params": current["tool_params"],
                "duration_ms": round(duration_ms, 1),
                "status": status,
                "result_preview": result_preview,
            }
        )

    # -- Flush to DynamoDB ----------------------------------------------------

    def flush(self) -> None:
        """Calculate cost and write the trace record to DynamoDB.

        This is fire-and-forget: any exception is caught and logged so that
        the calling handler's response is never affected.
        """
        try:
            cost = estimate_cost(
                self._total_input_tokens,
                self._total_output_tokens,
                self._model_id,
            )

            # Distribute accumulated token totals across model calls.
            # The Strands SDK doesn't expose per-call usage in AfterModelCallEvent,
            # so we split the totals evenly across calls as a best-effort approximation.
            # For single-call invocations (the common case), this is exact.
            num_calls = len(self._model_calls)
            if num_calls > 0 and self._total_input_tokens > 0:
                per_call_input = self._total_input_tokens // num_calls
                per_call_output = self._total_output_tokens // num_calls
                per_call_cache = self._total_cache_read_tokens // num_calls
                for i, call in enumerate(self._model_calls):
                    if call.get("input_tokens", 0) == 0:
                        call["input_tokens"] = per_call_input
                        call["output_tokens"] = per_call_output
                        call["cache_read_tokens"] = per_call_cache
                        # Give remainder to the last call
                        if i == num_calls - 1:
                            call["input_tokens"] += self._total_input_tokens % num_calls
                            call["output_tokens"] += self._total_output_tokens % num_calls

            # Redact Sample_Blocks before the row is built (p52 C5, R5.1/R5.2).
            # This is the choke point on purpose: every writer reaches flush(),
            # including schema_mapping/lambda_function.py:2125, which appends to
            # _model_calls by hand because its Converse call is not a strands
            # agent and no hook fires for it.  A redactor at _on_after_model
            # would miss that writer today.
            #
            # Each field gets its OWN try/except.  flush()'s outer except at the
            # bottom of this method already keeps the caller safe, so an
            # unguarded raise here would not break the caller — it would lose
            # the entire trace row, which is the diagnostic the trace exists to
            # provide (R7.2).  Degrade one field, keep the row and the other
            # three fields redacted.
            for entry in (*self._model_calls, *self._tool_calls):
                for field in _PREVIEW_FIELDS:
                    raw = entry.get(field)
                    if not isinstance(raw, str):
                        continue  # absent, None or non-str: inert, not an error
                    try:
                        entry[field] = _redact_sample_values(raw)
                    except Exception as exc:
                        entry[field] = _REDACTION_FAILED
                        # WARNING, not INFO, per LGR-0fe3e5d-2's reasoning: a
                        # redaction failure leaves no other trace, and WARNING
                        # is what survives an INFO-filtered log view.
                        logger.warning(
                            "Trace sample redaction failed; preview withheld: run_id=%s step=%s field=%s error_type=%s",
                            self.run_id,
                            self.step_name,
                            field,
                            type(exc).__name__,
                        )

            trace_data: dict[str, Any] = {
                "model_calls": self._model_calls,
                "tool_calls": self._tool_calls,
                "total_input_tokens": self._total_input_tokens,
                "total_output_tokens": self._total_output_tokens,
                "total_cache_read_tokens": self._total_cache_read_tokens,
                "estimated_cost_usd": cost,
                "start_time": Decimal(str(self._start_time)) if self._start_time else None,
                "end_time": Decimal(str(self._end_time)) if self._end_time else None,
                "model_id": self._model_id,
                "error": self._error,
            }

            now = datetime.now(UTC)
            # Convert trace_data to DynamoDB-safe types (no floats — use Decimal)
            safe_trace = json.loads(
                json.dumps(trace_data, default=str),
                parse_float=Decimal,
            )
            _get_traces_table().put_item(
                Item={
                    "run_id": self.run_id,
                    "step_name": self.step_name,
                    "pipeline_id": self.pipeline_id,
                    "trace_data": safe_trace,
                    "timestamp": now.isoformat(),
                    "ttl": int(time.time()) + _TTL_SECONDS,
                }
            )
            logger.info(
                "Agent trace written",
                extra={
                    "run_id": self.run_id,
                    "step_name": self.step_name,
                    "model_calls": len(self._model_calls),
                    "tool_calls": len(self._tool_calls),
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                    "estimated_cost_usd": str(cost),
                },
            )
        except Exception:
            logger.exception(
                "Failed to write agent trace: run_id=%s step=%s",
                self.run_id,
                self.step_name,
            )
