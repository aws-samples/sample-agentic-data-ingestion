# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pipeline trigger-input contract — one typed builder for every SFN start (P28 R9.2).

Why this module exists
----------------------
The July 2026 reliability review (P28 R9) found the self-healing agent's
rerun input drifting from what ``file_validation`` actually consumes: the
agent started executions with ``{pipeline_id, run_id, triggered_by,
original_run_id, fix_description}`` — no ``source_bucket`` and no source key
or prefix — so heal-triggered reruns died at the first pipeline step
(R9.1). The fix-enabler is a single, contract-tested builder that every
execution producer calls, so the trigger shapes can't silently drift again
(R9.2). Adoption in pipeline_trigger, retry-from-step, and the
self-healing agent is P28 task 8.1; this module only defines and tests the
contract.

Producer shapes cataloged (July 2026), superseded by this builder at
adoption:

    ==================================  ====================================================
    Producer                            SFN input keys
    ==================================  ====================================================
    pipeline_trigger (SQS S3 upload)    pipeline_id*, source_type="S3_UPLOAD", source_bucket,
                                        source_key, trigger="s3_upload"
    pipeline_trigger (Run Now, S3)      same, trigger="manual"
    pipeline_trigger (schedule, S3)     same, trigger="scheduled"
    pipeline_trigger (JDBC)             pipeline_id, source_type="JDBC",
                                        trigger="manual"|"scheduled"
    dashboard upload.py                 source_bucket, source_key, pipeline_key,
                                        trigger="manual_upload"
    dashboard trigger_batch             trigger_mode="prefix", source_bucket, source_prefix,
                                        pipeline_id (no trigger key)
    dashboard retry_run                 run_id, pipeline_id, retry_from_step,
                                        trigger="retry", {step}_result…
    self-healing agent (BROKEN, R9.1)   pipeline_id, run_id,
                                        triggered_by="self_healing_agent",
                                        original_run_id, fix_description  ← no source keys
    ==================================  ====================================================

    * The top-level ``pipeline_id`` is the LOCK KEY: for S3 uploads it is the
      S3 prefix derived from the uploaded key (equal to the pipeline record's
      id for standard ``{pipeline_id}/file.csv`` uploads); for JDBC it is the
      pipeline id itself. Callers on the S3-upload path therefore pass a
      minimal mapping ``{"pipeline_id": <derived prefix>, "source_type":
      "S3_UPLOAD"}`` rather than the raw pipeline record.

What ``file_validation`` actually consumes (the KeyError surface this
contract must always satisfy — see
``lambda/agentic_data_ingestion_file_validation/lambda_function.py``):

* single-file mode (default): ``source_bucket`` + ``source_key`` (both keys
  checked together), plus optional ``pipeline_id`` / ``execution_arn``.
* prefix mode (``trigger_mode == "prefix"``): hard ``event["source_bucket"]``
  and ``event["source_prefix"]`` subscripts — KeyError when missing. The
  builder therefore ALWAYS pairs a prefix-only S3 input with
  ``trigger_mode="prefix"`` so the execution is routed to the handler that
  can actually process it.
* JDBC mode (``source_type == "JDBC"``, checked first): ``pipeline_id``.

Contract rules enforced here (each violation raises :class:`ValueError`
naming the offending field — the "typed builder = drift protection" rule):

* ``pipeline_id`` non-empty, ``triggered_by`` one of :data:`TRIGGER_VALUES`,
  ``source_type`` one of :data:`SOURCE_TYPES`.
* S3 pipelines always carry ``source_bucket`` AND ``source_prefix`` in the
  output (R9.1's "full trigger contract"); the prefix defaults to the
  pipeline id (the upload prefix) and the bucket may be sourced from the
  pipeline record (``source_bucket`` or ``pipeline_config.source_bucket``,
  matching trigger_batch's resolution order).
* JDBC pipelines reject S3 source arguments outright.
* Reruns (``triggered_by`` in :data:`RERUN_TRIGGERS`) require ``run_id``;
  Heal_Guard (R9.4) relies on ``triggered_by == "self_healing"`` being
  present in the heal-rerun input. (The pre-contract agent wrote
  ``"self_healing_agent"`` — ``"self_healing"`` is the canonical value from
  the requirements glossary.)
* ``retry_from_step`` / ``cached_step_results`` belong to the retry flavor
  only. ``retry_from_step`` accepts any known step-name variant and is
  normalized to the SFN state id (matching ``retry_utils.STEP_ORDER``);
  cached results are emitted as ``{canonical_step}_result`` keys — the exact
  ``resultPath`` names the state machine dereferences
  (``$.file_validation_result.run_id`` …), unlike the pre-contract
  PascalCase keys that never matched.
* ``regeneration_attempt`` belongs to the rerun flavors only — the
  :data:`RERUN_TRIGGERS` members ``retry`` and ``self_healing``, the two
  producers that re-run after a failure (p32 Decision D1). Present on any
  other flavor it raises naming the field: a fresh run must behave
  identically whether or not the key is in its input, and a field that may
  be present but must have no effect is the shape that left the
  regeneration loop wired-but-dead (p32 Defect 4). The value must be a
  non-negative ``int`` — the counter ``create_code`` and ``test_code`` read
  as ``event.get("regeneration_attempt", 0)`` and compare against
  ``DEFAULT_REGENERATION_MAX_ATTEMPTS``, so a str or a DynamoDB ``Decimal``
  would make the bound silently unreachable rather than fail.
* ``job_config_overrides`` (R9.5 WorkerType persistence) and cached results
  must be JSON-serializable — Step Functions inputs are JSON. DynamoDB
  ``Decimal`` values are rejected with a pointer to ``shared/decimal_utils``
  rather than converted silently (loud beats lossy).

Layer 1 in ``shared/__init__._LAYER_MAP`` — imports ``step_names`` (layer 0).
"""

import json
from collections.abc import Mapping
from typing import Any, Final, Literal

from step_names import from_any, to_sfn_state_name

# Trigger values cataloged from the codebase (July 2026):
#   "s3_upload"     pipeline_trigger SQS path
#   "manual"        pipeline_trigger Run Now (S3 + JDBC)
#   "manual_upload" dashboard upload.py
#   "scheduled"     pipeline_trigger EventBridge schedules (S3 + JDBC)
#   "retry"         dashboard retry_run (retry-from-step)
#   "self_healing"  Heal_Guard canonical value (requirements glossary; the
#                   pre-contract agent wrote "self_healing_agent")
TriggeredBy = Literal["s3_upload", "manual", "manual_upload", "scheduled", "retry", "self_healing"]

TRIGGER_VALUES: Final[frozenset[str]] = frozenset(
    {"s3_upload", "manual", "manual_upload", "scheduled", "retry", "self_healing"}
)

# Triggers that re-run an earlier run and must therefore carry run identity.
RERUN_TRIGGERS: Final[frozenset[str]] = frozenset({"retry", "self_healing"})

SourceType = Literal["S3_UPLOAD", "JDBC"]

SOURCE_TYPES: Final[frozenset[str]] = frozenset({"S3_UPLOAD", "JDBC"})

# The S3 source-location argument names, used by the JDBC rejection loop.
_S3_SOURCE_FIELDS: Final[tuple[str, ...]] = ("source_bucket", "source_key", "source_prefix")


def _ensure_json_serializable(value: Any, field: str) -> None:
    """Raise ValueError naming *field* when *value* cannot be JSON-encoded.

    Step Functions execution input is JSON; a Decimal smuggled in from a
    DynamoDB read would otherwise only fail at ``start_execution`` time,
    far from the caller that introduced it.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field} is not JSON-serializable ({exc}) — Step Functions input must be "
            f"JSON; convert DynamoDB Decimals with shared/decimal_utils before building"
        ) from exc


def _reject_blank(value: str | None, field: str, hint: str) -> None:
    """Raise ValueError naming *field* when an explicitly-passed string is blank."""
    if value is not None and not value.strip():
        raise ValueError(f"{field} is empty — {hint}")


def _resolve_source_bucket(pipeline: Mapping[str, Any], explicit: str | None) -> str:
    """Resolve the source bucket: explicit argument, then the pipeline record.

    Record resolution order matches the dashboard's trigger_batch handler:
    top-level ``source_bucket``, then ``pipeline_config.source_bucket``.
    """
    if explicit is not None:
        _reject_blank(explicit, "source_bucket", "pass a non-empty bucket name or omit it to use the pipeline record")
        return explicit

    candidate = pipeline.get("source_bucket")
    if not isinstance(candidate, str) or not candidate.strip():
        pipeline_config = pipeline.get("pipeline_config")
        candidate = pipeline_config.get("source_bucket") if isinstance(pipeline_config, Mapping) else None
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(
            "source_bucket is missing or empty — pass it explicitly or store it on the "
            "pipeline record (source_bucket / pipeline_config.source_bucket)"
        )
    return candidate


def build_trigger_input(
    pipeline: Mapping[str, Any],
    *,
    triggered_by: TriggeredBy,
    run_id: str | None = None,
    source_bucket: str | None = None,
    source_key: str | None = None,
    source_prefix: str | None = None,
    original_run_id: str | None = None,
    job_config_overrides: Mapping[str, Any] | None = None,
    retry_from_step: str | None = None,
    cached_step_results: Mapping[str, Any] | None = None,
    regeneration_attempt: int | None = None,
) -> dict[str, Any]:
    """Build the full Step Functions execution input for a pipeline run.

    The single contract used by pipeline_trigger, retry-from-step, and the
    self-healing agent (P28 R9.2) — the output always contains every key
    ``file_validation`` consumes for the pipeline's source type, so no
    producer can start an execution that dies at step 1 (R9.1).

    Args:
        pipeline: The pipeline record, or a minimal mapping with at least
            ``pipeline_id`` (the LOCK KEY — see module docstring) and
            optionally ``source_type`` (defaults to ``"S3_UPLOAD"``) and
            ``source_bucket`` / ``pipeline_config.source_bucket``.
        triggered_by: One of :data:`TRIGGER_VALUES`. Emitted as BOTH
            ``triggered_by`` (the canonical Heal_Guard key, R9.4) and
            ``trigger`` (the traceability key every pre-contract producer
            wrote).
        run_id: The run identity. Required for rerun triggers (``"retry"``
            carries the ORIGINAL run id so cached-result lookups resolve;
            ``"self_healing"`` carries the stable ``{original}-heal`` rerun
            id). Omitted for fresh triggers — ``file_validation`` generates
            the run id.
        source_bucket: S3 source bucket. Falls back to the pipeline record;
            required (somewhere) for S3 pipelines, rejected for JDBC.
        source_key: Exact S3 object key → single-file validation mode.
            Rejected for JDBC.
        source_prefix: S3 prefix; defaults to the pipeline id (the upload
            prefix). Always present in S3 output (R9.1). Rejected for JDBC.
            When no ``source_key`` is given the output also carries
            ``trigger_mode="prefix"`` so file_validation routes the run to
            its prefix handler instead of KeyError-ing.
        original_run_id: Link back to the run being re-run; rerun triggers
            only.
        job_config_overrides: Remediation config (e.g. WorkerType upgrades)
            carried through the rerun so create_glue_pipeline can merge
            instead of clobber (R9.5). Empty mappings are omitted.
        retry_from_step: Retry flavor only. Any known step-name variant;
            normalized to the SFN state id (``"SchemaExtract"`` …).
        cached_step_results: Retry flavor only. Mapping of step name (any
            known variant) → that step's cached ``output_data``; emitted as
            ``{canonical_step}_result`` keys matching the state machine's
            resultPath names.
        regeneration_attempt: Rerun flavors only (``retry`` /
            ``self_healing``). The attempt number the run being started or
            advanced is on — 0 for the original generation, 1 for the first
            regeneration. Emitted verbatim so the counter every step reads
            as ``event.get("regeneration_attempt", 0)`` is a real number
            (p32 2.15). Omitted from the output when not passed, which is
            what keeps a fresh run byte-identical (p32 3.18).

    Returns:
        A JSON-serializable dict ready for ``start_execution(input=...)``.

    Raises:
        ValueError: On any contract violation, always naming the offending
            field.
    """
    if triggered_by not in TRIGGER_VALUES:
        raise ValueError(f"triggered_by must be one of {sorted(TRIGGER_VALUES)}; got {triggered_by!r}")

    pipeline_id = pipeline.get("pipeline_id")
    if not isinstance(pipeline_id, str) or not pipeline_id.strip():
        raise ValueError("pipeline_id is missing or empty on the pipeline mapping")

    source_type = pipeline.get("source_type", "S3_UPLOAD")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type must be one of {sorted(SOURCE_TYPES)}; got {source_type!r}")

    # ── Run identity ─────────────────────────────────────────────────────
    if triggered_by in RERUN_TRIGGERS:
        if run_id is None or not run_id.strip():
            raise ValueError(
                f"run_id is required when triggered_by={triggered_by!r} — rerun inputs must carry run identity"
            )
    else:
        _reject_blank(run_id, "run_id", "omit it for fresh triggers (file_validation generates the run id)")

    if original_run_id is not None and triggered_by not in RERUN_TRIGGERS:
        raise ValueError(
            f"original_run_id is only part of the rerun contract "
            f"(triggered_by in {sorted(RERUN_TRIGGERS)}); got triggered_by={triggered_by!r}"
        )
    _reject_blank(original_run_id, "original_run_id", "pass the run id being re-run or omit it")

    # ── The regeneration counter (p32 Decision D1) ───────────────────────
    # Gated exactly like original_run_id: legal on the rerun flavors, which
    # are the only producers that re-run after a failure. A fresh trigger
    # carrying it would be carrying a field with no effect.
    if regeneration_attempt is not None:
        if triggered_by not in RERUN_TRIGGERS:
            raise ValueError(
                f"regeneration_attempt is only part of the rerun contract "
                f"(triggered_by in {sorted(RERUN_TRIGGERS)}); got triggered_by={triggered_by!r}"
            )
        if isinstance(regeneration_attempt, bool) or not isinstance(regeneration_attempt, int):
            raise ValueError(
                f"regeneration_attempt must be an int (the attempt number); got "
                f"{type(regeneration_attempt).__name__} {regeneration_attempt!r} — convert DynamoDB "
                f"Decimals with shared/decimal_utils before building"
            )
        if regeneration_attempt < 0:
            raise ValueError(
                f"regeneration_attempt must be >= 0 (0 is the original generation); got {regeneration_attempt}"
            )

    # ── Retry-flavor arguments ───────────────────────────────────────────
    retry_step_sfn: str | None = None
    if triggered_by == "retry":
        if retry_from_step is None or not retry_from_step.strip():
            raise ValueError("retry_from_step is required when triggered_by='retry'")
        try:
            retry_step_sfn = to_sfn_state_name(from_any(retry_from_step))
        except ValueError as exc:
            raise ValueError(f"retry_from_step: {exc}") from exc
    else:
        if retry_from_step is not None:
            raise ValueError(
                f"retry_from_step is only part of the retry contract (triggered_by='retry'); "
                f"got triggered_by={triggered_by!r}"
            )
        if cached_step_results is not None:
            raise ValueError(
                f"cached_step_results is only part of the retry contract (triggered_by='retry'); "
                f"got triggered_by={triggered_by!r}"
            )

    out: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "source_type": source_type,
        "trigger": triggered_by,
        "triggered_by": triggered_by,
    }

    # ── Source location (the file_validation KeyError surface) ──────────
    if source_type == "JDBC":
        for field, value in zip(_S3_SOURCE_FIELDS, (source_bucket, source_key, source_prefix), strict=True):
            if value is not None:
                raise ValueError(
                    f"{field} is not part of the JDBC trigger contract — JDBC sources have no S3 source location"
                )
    else:
        _reject_blank(source_key, "source_key", "omit it for prefix-mode triggers or pass a non-empty key")
        _reject_blank(source_prefix, "source_prefix", "omit it to default to the pipeline id (the upload prefix)")
        out["source_bucket"] = _resolve_source_bucket(pipeline, source_bucket)
        out["source_prefix"] = source_prefix if source_prefix is not None else pipeline_id
        if source_key is not None:
            out["source_key"] = source_key
        else:
            # No exact key → the run can only be validated in prefix mode;
            # without this flag file_validation would fall through the
            # single-file path and never process the input.
            out["trigger_mode"] = "prefix"

    # ── Optional identity / remediation passthrough ──────────────────────
    if run_id is not None:
        out["run_id"] = run_id
    if original_run_id is not None:
        out["original_run_id"] = original_run_id
    if regeneration_attempt is not None:
        out["regeneration_attempt"] = regeneration_attempt

    if retry_step_sfn is not None:
        out["retry_from_step"] = retry_step_sfn
    if cached_step_results is not None:
        for raw_step, output_data in cached_step_results.items():
            try:
                step = from_any(raw_step)
            except ValueError as exc:
                raise ValueError(f"cached_step_results: {exc}") from exc
            result_key = f"{step.value}_result"
            if result_key in out:
                raise ValueError(
                    f"cached_step_results contains duplicate entries for step {step.value!r} "
                    f"(multiple name variants normalize to the same step)"
                )
            _ensure_json_serializable(output_data, f"cached_step_results[{raw_step!r}]")
            out[result_key] = output_data

    if job_config_overrides is not None:
        overrides = dict(job_config_overrides)
        if overrides:
            _ensure_json_serializable(overrides, "job_config_overrides")
            out["job_config_overrides"] = overrides

    return out
