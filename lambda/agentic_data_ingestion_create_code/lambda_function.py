# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""create_code Step_Lambda — orchestrating handler (P28 R19.1 decomposition).

The former ~1,200-line module is decomposed into testable units that live
alongside this file (the whole directory ships to both the Lambda asset and
the AgentCore container):

- ``mapping_resolution.py`` — mapping/metadata read layer
  (get_pipeline_metadata, get_regeneration_feedback, freshest-mapping
  selection). Table handles are injected; this module keeps the boto3
  resources and passes them via the delegating wrappers below.
- ``transform_generation.py`` — AI transform-block generation + prompt
  feedback + AST anti-pattern validation. The strands ``Agent`` constructor
  is injected via the wrapper below.
- ``code_assembly.py`` — TEMPLATE_RULES selection, Jinja2 render, DQDL
  inference. The transform generator + quarantine bucket are injected via
  the wrapper below.

Approval-side helpers (calculate_code_quality_metrics,
create_approval_payload) stay HERE by design: they are
handler-orchestration concerns (the payload the handler assembles), are
already unit-tested through this module, and are small.

``determine_approval_requirement`` is DELETED (p34 design D2,
``LGR-f077f1b-1``, template / ratified). It read
``approval_config.code_generation_enabled`` and wrote the answer to
``$.create_code_result.approval_required`` — a path no state in the ASL has
ever read, because ``CheckCodeApproval`` reads
``$.test_code_result.approval_required``. The code-generation approval
decision now lives in ``test_code``, the step whose result the gate does
read (``resolve_code_approval_setting`` +
``determine_merged_approval_requirement``). Amended, ratified R3.4: this
step publishes no code-generation approval field at all.

Wrapper/re-export seam: existing tests patch attributes on THIS module
(``patch.object(lambda_function, "pipelines_table"|"Agent"|...)``). The
delegating wrappers resolve those globals at call time, so both
handler-internal calls and direct test calls keep being intercepted without
the extracted modules needing any AWS clients or strands imports of their
own.
"""

import csv
import io
import json
import os
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import boto3

# Initialize logger first
from aws_lambda_powertools import Logger
from strands import Agent

logger = Logger(service="agentic_data_ingestion")

# Add shared directory to path for EventBridge publisher
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

# P28 R19.1: extracted create_code units (siblings in this Lambda dir — the
# CDK asset and the container Dockerfile both COPY the whole directory, and
# the dir is on sys.path in the Lambda runtime, the container PYTHONPATH,
# and the test suites). Imported AFTER the sys.path fix-ups above because
# they import shared/ modules themselves.
import code_assembly
import mapping_resolution
import transform_generation

# Re-exported names (consumed via this module by tests/contracts and by the
# handler below; pure functions with no injected dependencies).
from code_assembly import (  # noqa: F401  (TEMPLATE_RULES re-exported for tests/contracts)
    TEMPLATE_RULES,
    _generate_dqdl_rules,
    _select_template,
    _uniqueness_hint_fields,
)
from code_validator import extract_code_only, validate_generated_code
from mapping_resolution import _extract_field_mappings
from transform_generation import _build_feedback_section, validate_pyspark_antipatterns

from agent_trace_plugin import AgentTracePlugin

# Import standardized artifact schema
from artifact_schema import create_output_data

# Import lazy AWS account ID resolver
from audit_utils import write_agent_audit_entry

# P28 R19.2: single shared float↔Decimal converters (replace the copies that
# were nested inside lambda_handler).
from decimal_utils import convert_floats_to_decimal

# Typed accessors for DynamoDB item values (P22 Req 2)
from dynamo_typing import item_str
from env_utils import get_aws_account_id, require_env

# Single source of truth for the Bedrock model ID (P21 Req 1).
from model_config import resolve_model_id

# Import prompt template loader
from prompt_template_loader import load_prompt_template

# P28 R1.1: Step_Lambdas raise on failure instead of returning error payloads.
from sfn_task_token import step_task_token
from step_errors import StepFailure, step_handler
from step_names import STEP_SEQUENCE, StepName

# p33 D5 / task 4.6: one definition of the target-schema bucket-and-key read,
# shared with test_code and schema_mapping (LGR-7c9378d-1, template/ratified —
# resolve the pair where the pair is used, never accept a bucket from a caller).
from target_schema_resolution import resolve_target_schema

try:
    from metrics_facade import PipelineMetrics
except ImportError:
    PipelineMetrics = None  # type: ignore[misc,assignment]  # optional dep; guarded by `is not None` at use sites

from eventbridge_utils import get_eventbridge_publishers

publish_pipeline_state_change, publish_approval_status_change, publish_mcp_operation_event = (
    get_eventbridge_publishers()
)

# Environment variables
GENERATED_CODE_TABLE = require_env("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
PIPELINE_RUNS_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
PIPELINES_TABLE = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
MAPPINGS_TABLE = require_env("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

# Shared bucket names (from CloudFormation) - uses lazy account ID resolution
GENERATED_CODE_BUCKET = os.environ.get(
    "GENERATED_CODE_BUCKET", f"adi-generated-code-{get_aws_account_id()}-{AWS_REGION}"
)

# Initialize AWS clients
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
generated_code_table = dynamodb.Table(GENERATED_CODE_TABLE)
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)
pipelines_table = dynamodb.Table(PIPELINES_TABLE)
mappings_table = dynamodb.Table(MAPPINGS_TABLE)


# ── Delegating wrappers over the extracted units ────────────────────────
# Same public signatures as before the P28 R19.1 split. Each wrapper reads
# its dependencies (tables, Agent, bucket, transform generator) from THIS
# module's globals at call time, so `patch.object(lambda_function, ...)`
# keeps intercepting both direct calls and handler-internal calls.


def get_pipeline_metadata(pipeline_id: str) -> dict:
    """Pipeline metadata read (see mapping_resolution.get_pipeline_metadata)."""
    return cast(dict, mapping_resolution.get_pipeline_metadata(pipeline_id, pipelines_table))


def get_regeneration_feedback(run_id: str) -> dict:
    """Prior-iteration reviewer feedback read (see mapping_resolution)."""
    return cast(dict, mapping_resolution.get_regeneration_feedback(run_id, pipeline_runs_table))


def _select_freshest_mapping(
    pipeline_id: str, state_mapping_id: str, current_mapping_response: dict, run_schema_hash: str = ""
) -> tuple[str, dict]:
    """Freshest-approved-mapping selection, P28 R6 (see mapping_resolution).

    ``run_schema_hash`` is the ``schema_hash`` this run's ``schema_extract``
    state row carries. It gates the compatibility filter: a candidate mapping
    built against a different source schema is never substituted. Defaulted to
    ``""`` so the filter is inert when the hash is unavailable — see
    ``mapping_resolution._select_freshest_mapping``.
    """
    return cast(
        "tuple[str, dict]",
        mapping_resolution._select_freshest_mapping(
            pipeline_id,
            state_mapping_id,
            current_mapping_response,
            pipeline_runs_table=pipeline_runs_table,
            mappings_table=mappings_table,
            run_schema_hash=run_schema_hash,
        ),
    )


def generate_transform_with_ai(
    mappings: list,
    source_schema: dict | None = None,
    target_schema: dict | None = None,
    regeneration_feedback: dict | None = None,
    unsourced_target_fields: list[str] | None = None,
    trace_plugin=None,
) -> str:
    """AI transform-block generation (see transform_generation).

    Injects this module's ``Agent`` so existing ``patch.object(lambda_function,
    "Agent", ...)`` tests keep working. ``trace_plugin`` replaces the old
    function-attribute injection hack with an explicit parameter.

    ``target_schema`` (p33 task 4.7 / requirement 2.5) is threaded through this
    shim rather than around it. The shim exists only to inject ``Agent``, so
    shortcutting past it would look harmless — and would break every test that
    reaches the real prompt builder through ``patch.object(_mod, "Agent", ...)``
    (``test_silent_fallback_removal.py:87/121``,
    ``test_no_keyword_self_approval.py:217``). The parameter is added at every
    layer for that reason.

    ``unsourced_target_fields`` (p37 R3.5 / design C12) is threaded the same way
    and for the same reason: the target fields the mapping claims but supplies no
    source for, which the prompt annotates so the model sets them to an explicit
    null/default instead of improvising a source. ``code_assembly.generate_code``
    computes it from the field mappings it already resolved.
    """
    return cast(
        str,
        transform_generation.generate_transform_with_ai(
            mappings,
            source_schema=source_schema,
            target_schema=target_schema,
            regeneration_feedback=regeneration_feedback,
            unsourced_target_fields=unsourced_target_fields,
            agent_factory=Agent,
            trace_plugin=trace_plugin,
        ),
    )


# ── p49 C1 — the source-sample read this module owns ────────────────────
#
# `code_assembly` has no AWS client and must not gain one (its module
# docstring, and p49 do-NOT 10), so the read lives HERE and travels to the
# check as an injected callable — the same arrangement `generate_transform`
# and `quarantine_bucket` already use.
#
# Shape reused from `complex_mapping_agent.read_sample_data:84-137`: one
# `get_object`, at most `_MAX_SAMPLE_VALUES_PER_FIELD` unique non-empty values
# per field over `_MAX_SAMPLE_ROWS` rows, CSV and JSON/JSONL, and `{}` rather
# than an exception on any failure. Two deliberate differences from the
# reference, both stated rather than silent:
#
# 1. **The body read is BYTE-BOUNDED.** The reference reads the whole object.
#    That runs on AgentCore Runtime; this runs in a 512 MiB Lambda
#    (`compute-stack.ts:387`), where an unbounded read of a large source file
#    is an OOM — and an OOM here would FAIL code generation, which is exactly
#    what p49 CP2 and `LGR-9e76249-4` forbid. The consequence is documented on
#    `read_source_samples`: a JSON *array* source larger than the ceiling goes
#    inert rather than sampling. Inert is the correct failure for a telemetry
#    read; an OOM is not.
# 2. **A declined URI logs at info, not warning.** For a JDBC pipeline the
#    `jdbc://pipeline/{id}` sentinel is the NORMAL state, not a fault
#    (`schema_extract/lambda_function.py:1735`).
_MAX_SAMPLE_ROWS = 100
_MAX_SAMPLE_VALUES_PER_FIELD = 10
_MAX_SAMPLE_BYTES = 1_048_576


def _sample_rows_from_csv(body: str, max_rows: int) -> list[dict[str, Any]]:
    """Parse CSV text into row dicts, capped at ``max_rows``."""
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(csv.DictReader(io.StringIO(body))):
        if index >= max_rows:
            break
        rows.append(dict(row))
    return rows


def _sample_rows_from_json(body: str, max_rows: int) -> list[dict[str, Any]]:
    """Parse a JSON array or JSON Lines body into row dicts, capped at ``max_rows``."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(data, list):
            return [row for row in data[:max_rows] if isinstance(row, dict)]
        if isinstance(data, dict):
            return [data]

    rows: list[dict[str, Any]] = []
    for line in body.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
            if len(rows) >= max_rows:
                break
    return rows


def read_source_samples(s3_uri: str) -> dict[str, list[str]]:
    """Sampled source values per field, or ``{}``. NEVER raises (p49 CP2).

    ``{}`` — not an exception, and not a partial result — for every one of:

    - an empty or absent ``s3_uri``. A run predating the ``json_s3_uri``
      attribute is not evidence of a format mismatch.
    - ``jdbc://pipeline/{id}``, the sentinel ``schema_extract:1735`` writes
      for a JDBC source (``original_s3_uri or f"jdbc://pipeline/{id}"``, and a
      JDBC source has no uploaded object so it always lands on the sentinel).
      **Declined before any AWS call**, as ``read_sample_data:92-94`` declines,
      and logged at INFO because for a JDBC pipeline this is the normal state.
      One of the two source types is therefore uncovered by this check —
      accepted deliberately: sampling a JDBC source means a ``LIMIT``ed query
      against the customer database, which carries its own credential
      obligations under ``LGR-819980f-1`` (**invariant**).
    - any other non-``s3://`` URI, logged at warning — that one IS unexpected.
    - ``AccessDenied``, ``NoSuchKey``, throttling, a decode error, an
      unparseable body, or an empty object.

    The body is read up to ``_MAX_SAMPLE_BYTES``; see the block comment above
    for why, and for the one behaviour that ceiling costs (a JSON array larger
    than the ceiling truncates to something unparseable and yields ``{}``).
    """
    if not s3_uri or s3_uri.startswith("jdbc://"):
        logger.info(
            "No S3 source object to sample for the date-format check "
            f"(source URI: {s3_uri or 'absent'}); the check is inert for this run"
        )
        return {}
    if not s3_uri.startswith("s3://"):
        logger.warning(f"Unexpected source URI shape, not sampling: {s3_uri}")
        return {}

    try:
        bucket, _, key = s3_uri[len("s3://") :].partition("/")
        if not bucket or not key:
            logger.warning(f"Source URI names no object, not sampling: {s3_uri}")
            return {}

        response = s3_client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read(_MAX_SAMPLE_BYTES).decode("utf-8", errors="replace")

        if key.lower().endswith((".json", ".jsonl")):
            rows = _sample_rows_from_json(body, _MAX_SAMPLE_ROWS)
        else:
            rows = _sample_rows_from_csv(body, _MAX_SAMPLE_ROWS)

        field_values: dict[str, list[str]] = {}
        for row in rows:
            for field_name, value in row.items():
                if not isinstance(field_name, str):
                    continue
                text = str(value).strip() if value is not None else ""
                if not text or text.lower() in ("none", "null", "nan"):
                    continue
                values = field_values.setdefault(field_name, [])
                if text not in values and len(values) < _MAX_SAMPLE_VALUES_PER_FIELD:
                    values.append(text)

        logger.info(f"Sampled {len(field_values)} field(s) from {len(rows)} row(s) of {s3_uri}")
        return field_values

    except Exception as exc:
        # One broad clause, warn, `{}` — `read_sample_data:135-137`'s shape.
        # A telemetry read must never fail code generation (p49 CP2).
        logger.warning(f"Failed to sample source values from {s3_uri}: {type(exc).__name__}: {exc}")
        return {}


def _sample_reader_for(s3_uri: str) -> Callable[[], dict[str, list[str]]]:
    """A zero-argument reader bound to one source URI (p49 C1, do-NOT 10).

    Binding the URI here is what lets ``code_assembly`` take the read as a
    plain callable and stay free of both an AWS client and any S3-URI
    vocabulary. ``read_source_samples`` is resolved from THIS module's globals
    at call time, so ``patch.object(lambda_function, "read_source_samples",
    ...)`` intercepts through the bound reader — the same call-time-resolution
    property every other wrapper in this module relies on.
    """

    def _read() -> dict[str, list[str]]:
        return read_source_samples(s3_uri)

    return _read


def generate_code(
    pipeline_metadata: dict,
    mapping_result: dict,
    regeneration_feedback: dict | None = None,
    trace_plugin=None,
    contradictions: list[dict[str, str]] | None = None,
    sample_reader: Callable[[], dict[str, list[str]]] | None = None,
    date_format_findings: list[dict[str, str]] | None = None,
    date_format_ambiguities: list[dict[str, str]] | None = None,
    rename_manifest: dict | None = None,
) -> str:
    """Template-first script assembly (see code_assembly.generate_code).

    Injects this module's ``generate_transform_with_ai`` binding (so patches
    on it intercept the internal transform call) and the quarantine bucket.

    ``contradictions`` (p37 R2.3) is the out parameter
    ``code_assembly.generate_code`` fills with its contradiction findings.
    It is threaded through this shim rather than around it, for the same
    reason ``target_schema`` is (``LGR-b2f79ef-10``, template / ratified):
    the shim is the only seam the tests patch, so shortcutting past it would
    look harmless and would not be.

    ``sample_reader`` and ``date_format_findings`` (p49 R1.1, design C1) are
    threaded the same way and for the same reason. The reader is the injected
    source-sample read — see ``_sample_reader_for`` — and
    ``date_format_findings`` is the out list ``code_assembly.generate_code``
    fills with date formats that parse none of their column's sampled values.

    ``date_format_ambiguities`` (p49 R5.1, design C3) is the third out list,
    threaded identically: date formats the samples do not *refute* but do not
    *settle* either, each recorded with the format that was assumed.

    ``rename_manifest`` (p55 R11.1/R11.2, design W4-D1) is the approved
    Rename_Manifest, threaded through this shim for the same reason — it is an
    INPUT rather than an out list, but it arrives the same way, because
    ``code_assembly`` owns no AWS client and must not read it for itself
    (``LGR-1e135d2-2``). Reading the stored manifest off the mapping record and
    passing it here is the caller's job; ``None`` leaves the projection absent
    and the rendered script byte-identical to its pre-p55 form.
    """
    return cast(
        str,
        code_assembly.generate_code(
            pipeline_metadata,
            mapping_result,
            regeneration_feedback=regeneration_feedback,
            generate_transform=generate_transform_with_ai,
            quarantine_bucket=GENERATED_CODE_BUCKET,
            trace_plugin=trace_plugin,
            contradictions=contradictions,
            sample_reader=sample_reader,
            date_format_findings=date_format_findings,
            date_format_ambiguities=date_format_ambiguities,
            rename_manifest=rename_manifest,
        ),
    )


def _feedback_with_contradictions(
    regeneration_feedback: dict | None,
    contradictions: list[dict[str, str]],
    date_format_findings: list[dict[str, str]] | None = None,
) -> dict | None:
    """Merge generation-time findings into the regeneration feedback.

    R2.5: a generation-time finding travels the channel a reviewer finding
    already travels. ``_build_feedback_section``
    (``transform_generation.py:32-61``, P28 R13.3) renders ``issues_found``
    into the next prompt, and that rendering is what makes a regeneration
    prompt differ from the original instead of being byte-identical.

    Returns the input unchanged when there is nothing to merge, so the
    anti-pattern-only retry keeps its exact pre-p37 prompt. Never mutates the
    caller's dict — the same dict is read again on the fallback path.

    ``date_format_findings`` (p49 R6.4, design C1/P1) is the second kind of
    finding to travel this channel, appended AFTER the contradictions so the
    contradiction-only merge is byte-identical to its pre-p49 form. It is
    merged here rather than through a second helper because ``issues_found``
    is one list with one non-mutation invariant, and two assemblers of it
    would be two places for that invariant to be forgotten.

    **The name is kept deliberately even though it now merges both kinds.**
    Five assertions in ``test_rule_contradiction_check.py`` (``:456``,
    ``:457``, ``:468``, ``:483``, ``:499``) name this function, and renaming
    it to widen a docstring would edit a sibling spec's test module for a
    cosmetic reason.

    **What "naming the observed format" means here, and why it stops short of
    naming a source layout.** The reason strings this appends already name the
    column, the emitted format and example sampled values verbatim
    (``code_assembly.detect_date_format_mismatches``). They deliberately do
    NOT assert the layout the samples are *in*: ``05/12/2024`` is valid under
    MM/dd/yyyy and dd/MM/yyyy alike, so naming one would be the silent pick
    do-NOT 8 / R5.1 forbid, and surfacing that ambiguity is C3's job. The
    prohibition is pinned on the producing side by
    ``test_the_observed_runs_values_and_format_produce_a_finding``, which
    asserts ``"MM/dd/yyyy" not in reason``. So the model is shown the data and
    told the format it chose does not match it, which is the evidence — not a
    conclusion drawn from the evidence by a checker that cannot draw it.
    """
    if not contradictions and not date_format_findings:
        return regeneration_feedback
    merged = dict(regeneration_feedback or {})
    merged["issues_found"] = (
        list(merged.get("issues_found") or [])
        + [finding["reason"] for finding in contradictions if finding.get("reason")]
        + [finding["reason"] for finding in date_format_findings or [] if finding.get("reason")]
    )
    return merged


def get_code_bucket_and_prefix(pipeline_id):
    """Get shared bucket and pipeline-specific prefix for generated code"""
    return GENERATED_CODE_BUCKET, f"{pipeline_id}/code/"


def calculate_code_quality_metrics(generated_code: str, template_used: str, agent_reasoning: str) -> dict:
    """Honest structural code metrics — display information, NEVER gate inputs.

    P28 R13.1: the keyword-count quality score is DELETED. The old score
    awarded points for token presence (``"logger" in code`` +15, ``"try:"``
    +15, ``count("def ")`` +20, ...) — template boilerplate always contains
    those tokens, so template output always scored ≥70 and self-approved.
    Also deleted: ``has_error_handling``/``has_logging`` (keyword booleans
    that gated approval), ``estimated_maintainability`` (derived from the
    score), and ``complexity_indicators`` (``count("for ") * count("while ")``
    is not a measurement of anything).

    What remains are plain structural counts, honest as *descriptions* of
    the artifact and consumed by nothing as a gate. The real quality gates
    are the AST-based ``code_validator.validate_generated_code`` is_valid
    check (raises on invalid code) and the structured review verdict in the
    test step (R13.1).
    """
    return {
        "lines_of_code": len(generated_code.split("\n")),
        "functions_generated": generated_code.count("def "),
        "classes_generated": generated_code.count("class "),
        "imports_count": generated_code.count("import "),
        "template_used": template_used,
    }


def create_approval_payload(
    code_version_id: str,
    generated_code: str,
    code_quality_metrics: dict,
    agent_reasoning: str,
    template_used: str,
    code_s3_uri: str,
    dqdl_rules: str = "",
    requires_review: bool = True,
) -> dict:
    """Create the approval payload approvers see for generated code.

    ``dqdl_rules`` (P28 R11.2): the inferred DQDL ruleset baked into the
    generated job, surfaced so code approvers can see exactly which
    Completeness/Uniqueness constraints the job will enforce. The DQDL rules
    are generated here in create_code — AFTER mapping approval — so the code
    approval payload is the earliest approval surface where they exist.
    Empty string when the generation path produced no rules (fallback
    full-AI path, or no field mappings).

    P28 R13.1: the fabricated ``code_review_data`` verdicts are DELETED —
    "best_practices_compliance" (quality_score > 70 from keyword counts),
    "estimated_performance", "security_considerations" (``"boto3" in code``),
    and "scalability_assessment" (``"spark" in code``) presented token
    presence as review conclusions. Real review conclusions come from the
    structured review verdict in the test step. ``requires_review`` is no
    longer derived from the keyword score.

    p34 design D2: the handler no longer passes ``requires_review`` at all, so
    it takes its ``True`` default. That is honest at CreateCode time — this step
    runs BEFORE testing, so review has not been ruled out — and this field was
    never a review conclusion in the first place, per the paragraph above. The
    decision about whether a human actually reviews belongs to ``test_code``,
    the step whose result ``CheckCodeApproval`` reads (``LGR-f077f1b-1``,
    template / ratified).
    """
    return {
        "approval_type": "code_generation",
        "code_version_id": code_version_id,
        "generated_code_s3_uri": code_s3_uri,
        "generated_code_preview": generated_code[:2000] + "..." if len(generated_code) > 2000 else generated_code,
        "code_quality_metrics": code_quality_metrics,
        "dqdl_rules": dqdl_rules,
        "strands_agent_reasoning": agent_reasoning,
        "template_selection": {
            "template_used": template_used,
            "template_selection_reasoning": f"Selected {template_used} based on pipeline requirements and data complexity",
        },
        "requires_review": requires_review,
    }


# p34 design D2 / amended, ratified R3.4 (``LGR-f077f1b-1``, template):
# ``determine_approval_requirement`` stood here. It read
# ``approval_config.code_generation_enabled`` (default ``True``), escalated a
# regeneration attempt, and defaulted to ``True`` on a read error — then wrote
# the answer to ``$.create_code_result.approval_required``, which no state in
# the state machine has ever read. Its four branches are relocated onto
# ``test_code``: enabled / disabled / absent / malformed onto
# ``resolve_code_approval_setting``, the regeneration escalation onto
# ``determine_merged_approval_requirement``, the read-error branch onto three
# exception-classification examples (``LGR-f077f1b-2``, template / ratified).
# Deleting rather than renaming is the point: a renamed field nothing consumes
# is still a field nothing consumes.


@step_handler("create_code")
@step_task_token("create_code")
def lambda_handler(event, context):
    """
    Step 5: Code Generation with Strands Agent
    Follows DATA_FLOW_SPECIFICATION exactly
    """
    # ── AgentCore Runtime proxy path ────────────────────────────────
    if os.environ.get("USE_AGENTCORE_RUNTIME") == "true":
        from agentcore_proxy import invoke_agentcore_runtime

        return invoke_agentcore_runtime(
            endpoint=os.environ["AGENTCORE_CREATE_CODE_ENDPOINT"],
            payload=event,
            step_name="create_code",
        )

    # NOTE: `time` is imported handler-locally so it resolves from
    # sys.modules at call time (the duration-metrics tests substitute a fake
    # time module). `json` comes from the module-level import.
    import time as _time

    _metrics_start = _time.time()

    try:
        logger.info(f"Event received: {json.dumps(event)}")

        # Extract required data from event
        run_id = event.get("run_id")
        pipeline_id = event.get("pipeline_id")
        mapping_id = event.get("mapping_id", str(uuid.uuid4()))
        regeneration_attempt = event.get("regeneration_attempt", 0)
        data_volume = event.get("data_volume", "unknown")
        performance_requirements = event.get("performance_requirements", {})

        logger.append_keys(run_id=run_id, pipeline_id=pipeline_id, step_name=StepName.CREATE_CODE)

        # Read target_config from pipeline metadata (DynamoDB)
        pipeline_metadata = get_pipeline_metadata(pipeline_id)
        target_config = pipeline_metadata.get("target_config", {})

        logger.info(f"Pipeline target_config: {json.dumps(target_config)}")

        # Determine target type and configuration from target_config.
        # (P28 R19.1 dead-path sweep: the old `target_format`/`s3_table_bucket`
        # locals built here were relics of a prompt shape that no longer
        # exists — nothing read them — and are deleted.)
        target_type = target_config.get("type", "").upper()
        target_location = None

        if target_type in ["NEW_S3_TABLE_BUCKET", "EXISTING_S3_TABLE_BUCKET", "S3_TABLES"]:
            # S3 Tables target (new or existing) - read from target_config
            # For code generation, both are handled the same way
            bucket_name = target_config.get("bucket_name")
            namespace_name = target_config.get("namespace_name")
            table_name = target_config.get("table_name")

            if bucket_name and namespace_name and table_name:
                target_type = "S3_TABLES"

                # Construct warehouse location (S3 Tables standard path)
                # Format: s3://bucket-name/namespace/table-name/
                target_location = f"s3://{bucket_name}/{namespace_name}/{table_name}/"

                logger.info(
                    f"Target: S3 Tables ({'new' if target_config.get('type') == 'NEW_S3_TABLE_BUCKET' else 'existing'})"
                )
                logger.info(f"  Bucket: {bucket_name}")
                logger.info(f"  Namespace: {namespace_name}")
                logger.info(f"  Table: {table_name}")
                logger.info(f"  Warehouse location: {target_location}")
            else:
                logger.warning(
                    f"S3 Tables target selected but missing configuration: bucket_name={bucket_name}, namespace={namespace_name}, table={table_name}"
                )

        # For other target types (GLUE_CATALOG, JDBC, etc.), target_location will be handled differently
        # This will be implemented when those target types are added
        logger.info(f"Target type: {target_type}")

        # FIXED: Read source schema from DynamoDB first (single source of truth)
        schema_response = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": "schema_extract"})

        # `run_schema_hash` identifies WHICH source schema this run extracted.
        # Read off the same row as `source_schema` so the two cannot disagree,
        # and empty whenever the row is absent — the mapping-compatibility
        # filter below is inert in that case rather than refusing candidates on
        # a comparison it cannot make.
        run_schema_hash = ""
        # p49 C1: the source object this run extracted from. Taken off the SAME
        # Item as `source_schema` and `run_schema_hash` — one more attribute on
        # a read that already happens, which is why C1 needs no new persisted
        # attribute and did NOT need `schema_extract` to start storing values
        # (`LGR-0739ab8-1`, template / ratified: that Lambda measures 247.86 MiB
        # against a 250 MiB limit). `""` whenever the row is absent, which
        # leaves the date-format check inert rather than guessing at a URI.
        #
        # p53 C3: `sample_s3_uri` — the bounded Sample_Artifact the generated
        # Glue job writes — is PREFERRED when present, so a JDBC pipeline gets
        # real values from run 2 onwards (R2.4). Absent or empty falls back to
        # `json_s3_uri` exactly as before (R2.5); `json_s3_uri` is NOT repointed
        # (do-NOT 11) because three other readers take it to mean the canonical
        # source URI. The reader is unchanged either way — it is handed an
        # `s3://` URI and does what it always did (R2.1, R2.2, R2.3).
        source_sample_uri = ""
        # p55 R4.1: the SOURCE OBJECT's own URI, for the Source_Delimiter the
        # rendered csv read passes to `.option("sep", …)`. A THIRD attribute off
        # the same Item — the p49 C1 pattern again, no new read.
        #
        # Deliberately NOT `source_sample_uri`: that one prefers `sample_s3_uri`,
        # the bounded Sample_Artifact the Glue job writes (p53 C3), whose
        # extension describes the artifact rather than the upload. A `.tsv`
        # source with a parquet sample would then be read with a comma.
        #
        # p55 W3 (task 4.3): this reads `readable_source_uri` — the object the
        # generated Glue job will actually READ, which `schema_extract` resolved
        # once and stored on this same row (R13.3/R13.4). It is the same value
        # `create_glue_pipeline` points `--source_path` at via
        # `artifacts.get_source_path`, which is the property this line needs: the
        # extension read here must belong to the object the rendered `sep` will
        # be applied to. For every Native_Format_Set member that is the original
        # upload; for an Excel_Workbook it is the converted `.csv`.
        #
        # It falls back to `json_s3_uri` for a schema_extract row written BEFORE
        # p55, which is correct by construction — no Conversion_Required_Set
        # format existed then, so such a row's readable object IS its original.
        # `json_s3_uri` is NOT repointed at the converted object (p53 do-NOT 11 /
        # `LGR-5c539a6-2`, which rejected that by name: three readers take it to
        # mean the canonical source URI), which is exactly why this needs its own
        # attribute rather than riding on a repoint.
        #
        # On the legacy extraction path `json_s3_uri` IS the converted JSON, but
        # that path's `source_format` is `json`, so the csv branch is not
        # rendered and the value is never read. `""` when the row is absent;
        # `delimiter_for_key` is total and answers a comma (R4.6).
        source_object_uri = ""
        # p55 W3: the Canonical_Format the source object should be READ AS, which
        # is not always the format it was DETECTED as. Task 4.1's finding 2: the
        # four templates branch `{% if source_format == 'csv' %} … {% else %}
        # spark.read…json(source_path)`, so an Excel run whose URI is repointed at
        # a converted CSV while its format still says `excel` renders the
        # `{% else %}` and reads that CSV as JSON. The URI and the format are two
        # outputs of one resolution and must travel together. `""` for a pre-p55
        # row, which leaves the event's detected format in charge exactly as
        # before.
        readable_source_format = ""
        if "Item" not in schema_response:
            # Fallback to event if DynamoDB read fails
            logger.warning("Could not read schema from DynamoDB, using event data")
            source_schema = event.get("source_schema", {})
        else:
            source_schema = json.loads(item_str(schema_response["Item"], "extracted_schema"))
            run_schema_hash = item_str(schema_response["Item"], "schema_hash")
            # `item_str` returns `""` for an absent key AND for a non-string
            # value, so one `or` covers all three shapes p53 3.1 can produce:
            # key absent (the DynamoDB item omits it when there is no sample),
            # present-but-empty, and present with a real `s3://` URI.
            source_sample_uri = item_str(schema_response["Item"], "sample_s3_uri") or item_str(
                schema_response["Item"], "json_s3_uri"
            )
            source_object_uri = item_str(schema_response["Item"], "readable_source_uri") or item_str(
                schema_response["Item"], "json_s3_uri"
            )
            readable_source_format = item_str(schema_response["Item"], "readable_source_format")
            logger.info("Retrieved source schema from DynamoDB")

        # P28 R3.4: a failed upstream mapping must fail this step — never
        # fabricate a default_passthrough mapping that ships untransformed
        # data. (No live producer sets `mapping_failed` — schema_mapping
        # raises on failure since R1.1 — kept as a contract guard.)
        mapping_failed = event.get("mapping_failed")
        if mapping_failed and mapping_failed != 200:
            raise StepFailure(
                "create_code",
                f"Upstream schema mapping failed (status {mapping_failed}); "
                "cannot generate code without an approved field mapping",
                details={
                    "mapping_id": event.get("mapping_id"),
                    "mapping_failed_status": mapping_failed,
                },
            )
        if not mapping_id:
            # P28 R3.4: a missing mapping id is a contract violation — fail
            # loudly instead of fabricating a default_passthrough mapping.
            raise StepFailure(
                "create_code",
                "No mapping_id provided in event; create_code requires an approved field mapping",
                details={"mapping_id": event.get("mapping_id")},
            )

        # Try to get mapping from DynamoDB (typed Any: the variable is
        # re-bound by _select_freshest_mapping's plain-dict response below)
        mapping_response: Any = mappings_table.get_item(Key={"mapping_id": mapping_id})

        # P28 R6: check for a newer APPROVED mapping for this pipeline (e.g.
        # from a rerun-mapping) and substitute it when strictly newer than the
        # state-carried mapping — evaluating ALL GSI rows by created_timestamp.
        # `run_schema_hash` additionally restricts candidates to mappings built
        # against the schema THIS run extracted: recency alone once substituted
        # a mapping whose source declared `email` over one that declared
        # `email_address`, and the transform read a column the frame could not
        # contain.
        mapping_id, mapping_response = _select_freshest_mapping(
            pipeline_id, mapping_id, mapping_response, run_schema_hash
        )

        if "Item" not in mapping_response:
            # P28 R3.4: a mapping id that resolves to no record is a lookup
            # failure — fail naming the mapping instead of fabricating a
            # default_passthrough mapping.
            raise StepFailure(
                "create_code",
                f"Field mapping '{mapping_id}' not found in mappings table; "
                "cannot generate code without an approved field mapping",
                details={"mapping_id": mapping_id},
            )
        # Plain-dict view of the DynamoDB item (keeps the pre-existing
        # duck-typed .get() access patterns below type-checkable).
        mapping_item: dict = dict(mapping_response["Item"])
        mapping_config = mapping_item.get("mapping_config", {})

        # ── Schema identities: fail naming the absent one, never invent one ──
        # p33 task 4.6 / D11 / requirement 2.9. These used to default to
        # `str(uuid.uuid4())`. A missing hash then became a fresh, valid-LOOKING
        # identifier that matches nothing. It is written onto the generated-code
        # record as `source_schema_id` / `target_schema_id` (below), and the
        # dashboard joins `source_schema_id` back to the schema-extraction
        # history by hash to work out which schema version a script was generated
        # for (`dashboard_api/handlers/glue_jobs.py:157-180`, verified read). On a
        # miss it logs "Could not match code … with schema_hash" and drops the
        # entry from `script_versions` entirely — so a random UUID makes the code
        # version vanish from the dashboard instead of reporting that the mapping
        # record was incomplete. Same "looks fine, means nothing" failure class as
        # the vacuous `{}` target-schema read this spec exists to remove.
        #
        # Safe to raise: the ONLY writer of these attributes is
        # `schema_mapping.store_mapping_separately`, which writes both
        # unconditionally on every put and substitutes the literal `"no-hash"`
        # when its own value is falsy (`schema_mapping/lambda_function.py:1166-1169`,
        # verified read). So a record reaching here without them was not written
        # by the live mapping path, and codegen identity cannot be recorded for it.
        #
        # NOTE the deliberate asymmetry with `:362`'s `mapping_id` UUID default,
        # which stays: that value is superseded by the mapping lookup a few lines
        # up, which already raises on an id that resolves to nothing (requirement
        # 3.11 preserves that). It looks like the same defect and is not.
        for attribute in ("source_schema_hash", "target_schema_hash"):
            if not mapping_item.get(attribute):
                raise StepFailure(
                    "create_code",
                    f"Field mapping '{mapping_id}' has no {attribute}; the schema identity of the "
                    "generated code cannot be recorded. Every mapping written by schema_mapping "
                    "carries both source_schema_hash and target_schema_hash, so this record is "
                    "incomplete — a generated identifier would match no schema.",
                    details={
                        "mapping_id": mapping_id,
                        "missing_attribute": attribute,
                        "operation": "read_mapping_schema_hashes",
                    },
                )

        source_schema_id = mapping_item["source_schema_hash"]
        target_schema_id = mapping_item["target_schema_hash"]

        # ── Target schema: the canonical location, not the mappings record ──
        # p33 task 4.6 / requirement 2.1. This read used to be
        # `mapping_item.get("target_schema", {})`, and no writer has ever set a
        # `target_schema` attribute on a mappings record
        # (`store_mapping_separately` writes the two hashes, never the schema),
        # so it returned `{}` on every run of every pipeline. The canonical
        # location is the pipeline record's `target_schema_bucket` +
        # `target_schema_s3_key` pair, resolved TOGETHER by the shared resolver
        # (LGR-7c9378d-1: reading the pair where the pair is used is what removes
        # the class of bug, not just this instance).
        #
        # `pipeline_metadata` is REUSED from the read at `:370` above rather than
        # re-fetched — the handler already had the record in hand, and a second
        # get_item would be a second chance for the two reads to disagree.
        #
        # (None, None) means, and only means, that the record carries no
        # `target_schema_s3_key`. A key with no bucket, or a key naming an object
        # that is not there, raises (requirements 2.7 / 2.8, D14).
        resolved_target_schema, resolved_target_schema_hash = resolve_target_schema(pipeline_metadata, "create_code")
        if resolved_target_schema is None:
            logger.warning(f"Pipeline {pipeline_id} carries no target schema on record")
        else:
            logger.info(
                f"Resolved target schema for {pipeline_id}: "
                f"{len(resolved_target_schema.get('properties', {}))} declared field(s), "
                f"hash {resolved_target_schema_hash}"
            )

        # The full-AI fallback prompt at `:673` renders this with json.dumps and
        # cannot express the no-schema state, so it keeps seeing `{}` there —
        # byte-identical to what it saw before this change, since the old read
        # always produced `{}`.
        #
        # p33 task 4.7: this value now ALSO reaches the template-first path's
        # transform prompt, through `pm["target_schema"]` below. That is the
        # load-bearing half of the spec — the generator's only information about
        # target field names was the field mappings, which do not change between
        # regeneration attempts, which is why p32's three attempts produced three
        # byte-identical scripts despite the loop mechanics working.
        target_schema = resolved_target_schema or {}

        # Generate unique code version ID
        code_version_id = str(uuid.uuid4())
        generation_timestamp = datetime.now(UTC).isoformat()

        # Get regeneration feedback if this is a retry
        regeneration_feedback = get_regeneration_feedback(run_id) if regeneration_attempt > 0 else {}

        agent_session_id = str(uuid.uuid4())

        # --- Template-first code generation path ---
        # Initialize trace plugin for observability; threaded explicitly
        # through generate_code → generate_transform_with_ai (the old
        # function-attribute injection hack is deleted — P28 R19.1).
        trace_plugin = AgentTracePlugin(
            pipeline_id=pipeline_id,
            run_id=run_id,
            step_name="code_generation",
        )

        # Normalise target_type for template lookup. Mirrors generate_code's
        # normalisation table; "rds_database" maps to itself so it resolves
        # via TEMPLATE_RULES (Task 9.4 — fixes the P14 codegen bug where
        # rds_database fell through to the full-AI fallback path).
        raw_target = target_config.get("type", target_config.get("target_type", "")).lower()
        normalised_target = {
            "new_s3_table_bucket": "s3_tables",
            "existing_s3_table_bucket": "s3_tables",
            "s3_tables": "s3_tables",
            "glue_catalog": "glue_catalog",
            "jdbc": "jdbc",
            "rds_database": "rds_database",
            "s3_parquet": "s3_parquet",
        }.get(raw_target, raw_target)

        # Read source_type from pipeline metadata; defaults to S3_UPLOAD for
        # back-compat with existing pipelines created before P17.
        pipeline_source_type = pipeline_metadata.get("source_type", "S3_UPLOAD")
        pipeline_source_config = pipeline_metadata.get("source_config", {})

        template_name_j2 = _select_template(pipeline_source_type, normalised_target)

        # p37 R2.3/R2.4: rules the generated transform makes unsatisfiable.
        # Initialised for BOTH generation paths — the fallback full-AI path
        # renders no template and so bakes no DQDL ruleset into the code
        # (``generated_dqdl_rules = ""`` below), which leaves nothing to
        # contradict; the empty list records that honestly rather than
        # leaving the key absent for one path and present for the other.
        code_contradictions: list[dict[str, str]] = []

        # p49 R1.1/R1.2 (design C1): date formats the generated transform
        # applies that parse NONE of the source column's sampled values. Kept
        # separate from `code_contradictions` — the two findings say different
        # things, and the record an approver reads names them separately.
        #
        # Initialised for BOTH generation paths for the same reason the list
        # above is: the fallback full-AI path renders no template, so this list
        # honestly stays empty there rather than being absent on one path.
        date_format_findings: list[dict[str, str]] = []

        # p49 R5.1/R5.2/R5.3 (design C3): date formats the sampled values do not
        # REFUTE but do not SETTLE either — `05/12/2024` is a valid MM/dd/yyyy
        # and a valid dd/MM/yyyy, and the checker is forbidden from picking
        # (do-NOT 8). A third list, on the same reasoning that made the second
        # one separate: the three say different things and carry different field
        # sets, and an approver reading one number could not tell which happened.
        date_format_ambiguities: list[dict[str, str]] = []

        if template_name_j2:
            # Use template-first approach: deterministic template + AI transform block
            logger.info(f"Using template-first code generation: {template_name_j2}")
            # Which format the template renders its source read for. Three
            # sources, in decreasing authority:
            #
            # 1. `readable_source_format` off the schema_extract row (p55 W3) —
            #    the format the object at `source_object_uri` should be READ AS.
            #    It is the only one of the three that knows a conversion
            #    happened: the event's `source_format` comes from
            #    `$.file_validation_result.source_format`
            #    (`orchestration-stack.ts:581`), which is the DETECTED format and
            #    says `excel` for a workbook that is about to be read as CSV.
            # 2. the Step Functions event's `source_format` (runtime detection),
            #    which is what every pre-p55 run has and still the right answer
            #    for every Native_Format_Set member.
            # 3. `target_config`'s own `source_format` (pipeline config default),
            #    applied by `code_assembly.generate_code`.
            event_source_format = event.get("source_format")
            render_source_format = readable_source_format or event_source_format
            pm = {
                "target_config": target_config,
                "source_config": pipeline_source_config,
                "source_type": pipeline_source_type,
                "source_schema": source_schema,
                # p33 task 4.7 / requirement 2.5. `{}` for a pipeline with no
                # target schema on record (requirement 2.6), which renders no
                # target block and leaves the prompt byte-identical to its
                # pre-p33 form for those pipelines.
                "target_schema": target_schema,
                # p55 R4.1: read by `code_assembly.generate_code`, which turns
                # it into the `source_delimiter` render var beside
                # `source_format`. `""` for a run whose schema_extract row is
                # absent — `delimiter_for_key` is total (R4.6).
                "source_object_uri": source_object_uri,
            }
            if render_source_format:
                pm["target_config"] = dict(target_config)
                pm["target_config"]["source_format"] = render_source_format
            # p37 R3.4 (design C11): ONE evaluation of the mapping view, feeding
            # BOTH consumers — the ruleset below and the `- Field mappings: `
            # count in `agent_reasoning` further down this branch.
            #
            # The reasoning string used to count inline from two of the three
            # storage variants (`field_mappings`, `mapping_config.field_mappings`)
            # and miss the third, `mapping_result` as a JSON string, which
            # `_extract_field_mappings` documents as the CURRENT schema_mapping
            # output (`mapping_resolution.py:48-49`). It therefore reported
            # `Field mappings: 0` for run run-2026-08-09-15-38-19-5c5f5f8a, which
            # was generated from eight — a false zero persisted on the state row
            # and rendered to approvers, not a log line.
            #
            # Hoisted rather than evaluated twice: the count the reasoning claims
            # and the ruleset the approval payload shows now come from the same
            # value, so they cannot diverge.
            resolved_field_mappings = _extract_field_mappings(mapping_item)
            # P28 R11.2: same inputs generate_code renders into the job, so
            # the approval payload shows the exact ruleset the job enforces
            # (_generate_dqdl_rules is deterministic over these inputs).
            generated_dqdl_rules = _generate_dqdl_rules(
                resolved_field_mappings,
                unique_hint_fields=_uniqueness_hint_fields(target_config),
            )
            # P28 R13.3: reviewer feedback (when this is a regeneration) is
            # threaded into the transform prompt so it differs from the
            # original by including the reviewer's issues.
            #
            # p37 R2.3: `code_contradictions` is the out list generate_code
            # fills with rules its own transform makes unsatisfiable.
            #
            # p49 C1: `sample_reader` is the injected source-sample read bound
            # to the `json_s3_uri` taken off the schema_extract row above, and
            # `date_format_findings` is the out list it feeds. Built once and
            # reused across both generation calls below.
            sample_reader = _sample_reader_for(source_sample_uri)
            generated_code = generate_code(
                pm,
                mapping_item,
                regeneration_feedback=regeneration_feedback or None,
                trace_plugin=trace_plugin,
                contradictions=code_contradictions,
                sample_reader=sample_reader,
                date_format_findings=date_format_findings,
                date_format_ambiguities=date_format_ambiguities,
            )
            if code_contradictions:
                logger.warning(
                    "Generated ruleset contradicts the generated transform",
                    extra={"code_contradictions": code_contradictions},
                )
            if date_format_findings:
                logger.warning(
                    "Generated transform parses a date column with a format its samples do not match",
                    extra={"date_format_findings": date_format_findings},
                )
            if date_format_ambiguities:
                # WARNING, per `LGR-0fe3e5d-2` (**template / ratified**): a
                # retained non-blocking finding logs at warning, because on a
                # green run it "appears nowhere except inside" the record — and
                # warning is what makes it survive an INFO-filtered log view.
                # That record's other half is about a *blocking* finding, which
                # this is not, so only the retained arm applies.
                logger.warning(
                    "Generated transform assumes a date format the sampled values do not settle",
                    extra={"date_format_ambiguities": date_format_ambiguities},
                )

            # --- PySpark anti-pattern validation with one regeneration attempt ---
            #
            # p37 R2.5 shares this seam rather than adding a second one: one
            # in-process re-call, on the same Step Functions attempt, with the
            # contradiction findings merged into the feedback dict that
            # `_build_feedback_section` renders into the next prompt. That
            # reaches attempt 1 instead of attempt 3, which is where the live
            # run (newtest-ab2a08a0 / run-2026-08-09-15-38-19-5c5f5f8a) ran
            # out of its regeneration budget.
            antipattern_issues = validate_pyspark_antipatterns(generated_code)
            # p49 R6.4: a date-format finding joins the two existing triggers for
            # the ONE in-process regeneration. Adding the term here is what makes
            # the finding act on attempt 1 instead of only being recorded — and it
            # does NOT make the finding blocking: every arm below this branch
            # proceeds, and the branch's only exit is the same "proceeding as-is"
            # the anti-pattern and contradiction arms already take
            # (`LGR-9e76249-4`, template / ratified). A third trigger cannot fail
            # a step that no trigger can fail.
            #
            # **p49 C3 is deliberately NOT a fourth term here, and that is a
            # decision rather than an omission.** A regeneration cannot resolve
            # an ambiguity: the model would be handed the same two readings the
            # checker has and asked to choose between them with no more evidence
            # than the checker had, which is the silent pick do-NOT 8 / R5.1
            # forbid — bought with a Bedrock call. The ambiguity is recorded and
            # surfaced instead. It also must not make the regeneration *count*
            # rise, because the carriers are replaced after the re-call and a
            # regeneration driven by an ambiguity would then discard a finding
            # about the block that shipped.
            if antipattern_issues or code_contradictions or date_format_findings:
                if antipattern_issues:
                    logger.warning(
                        "PySpark anti-patterns detected in generated code",
                        extra={"antipattern_issues": antipattern_issues},
                    )
                # Trigger one regeneration attempt
                logger.info(
                    "Triggering one regeneration attempt",
                    extra={
                        "antipattern_count": len(antipattern_issues),
                        "contradiction_count": len(code_contradictions),
                        "date_format_finding_count": len(date_format_findings),
                    },
                )
                retry_contradictions: list[dict[str, str]] = []
                retry_date_format_findings: list[dict[str, str]] = []
                retry_date_format_ambiguities: list[dict[str, str]] = []
                generated_code = generate_code(
                    pm,
                    mapping_item,
                    regeneration_feedback=_feedback_with_contradictions(
                        regeneration_feedback or None, code_contradictions, date_format_findings
                    ),
                    trace_plugin=trace_plugin,
                    contradictions=retry_contradictions,
                    sample_reader=sample_reader,
                    date_format_findings=retry_date_format_findings,
                    date_format_ambiguities=retry_date_format_ambiguities,
                )
                retry_issues = validate_pyspark_antipatterns(generated_code)
                if retry_issues:
                    logger.warning(
                        "PySpark anti-patterns persist after regeneration, proceeding as-is",
                        extra={"antipattern_issues": retry_issues},
                    )
                if retry_contradictions:
                    # p37 R2.3 do-NOT-block (design C8, risk 4): the same
                    # "proceeding as-is" the arm above already takes. This is
                    # a conservative static analysis of AI output; making it a
                    # hard gate would trade one silent failure for one loud
                    # false one. R2.1/R2.2 are the deterministic half.
                    logger.warning(
                        "Rule/transform contradictions persist after regeneration, proceeding as-is",
                        extra={"code_contradictions": retry_contradictions},
                    )
                if retry_date_format_findings:
                    # p49 C1, same "proceeding as-is" arm and the same reason:
                    # `LGR-9e76249-4` (template / ratified) rejected making a
                    # generation-time heuristic blocking, because a false gate
                    # on code generation is worse than a missed finding. The
                    # deterministic net downstream is the Completeness rule,
                    # which caught the observed defect.
                    logger.warning(
                        "Date-format mismatches persist after regeneration, proceeding as-is",
                        extra={"date_format_findings": retry_date_format_findings},
                    )
                if retry_date_format_ambiguities:
                    # p49 C3: a regeneration that fixed a mismatch can land on a
                    # format the samples do not settle — the corrected format is
                    # not necessarily the derived one. Logged at warning for the
                    # `LGR-0fe3e5d-2` reason, and NOT re-regenerated: one
                    # regeneration is the whole budget (P1), and a second pass
                    # would still have no evidence the first lacked.
                    logger.warning(
                        "Regenerated transform assumes a date format the sampled values do not settle",
                        extra={"date_format_ambiguities": retry_date_format_ambiguities},
                    )
                # The record (R2.4) describes the code that was STORED, so the
                # re-check's findings replace the first attempt's. Same for the
                # p49 date findings: a finding about a transform block that was
                # thrown away describes code nobody will run. Same again for the
                # ambiguities, and here it is load-bearing in both directions:
                # attempt 1's assumption is about a block nobody will run, and
                # the regenerated block's assumption is the one an approver
                # needs — a regeneration that fixed the mismatch can introduce
                # an ambiguity that was not there before.
                code_contradictions = retry_contradictions
                date_format_findings = retry_date_format_findings
                date_format_ambiguities = retry_date_format_ambiguities

            # Validate the assembled script (feed antipattern findings into the gate — P21 Req 2.6)
            final_antipatterns = validate_pyspark_antipatterns(generated_code)
            validation_result = validate_generated_code(generated_code, antipattern_count=len(final_antipatterns))
            if not validation_result["is_valid"]:
                logger.error(f"Template code validation failed: {validation_result['errors']}")
                raise ValueError(f"Template code validation failed: {'; '.join(validation_result['errors'])}")

            logger.info(f"Template code validation passed: {validation_result.get('summary', {})}")
            template_used = template_name_j2
            agent_reasoning = f"""Template-first code generation for attempt {regeneration_attempt + 1}:
- Template: {template_name_j2}
- Target type: {normalised_target}
- Source schema fields: {len(source_schema.get("properties", {}))}
- Field mappings: {len(resolved_field_mappings)}
- Generated {len(generated_code.splitlines())} lines (template + AI transform block)"""
        else:
            # Fallback: full-script AI generation for unknown target types.
            # This path is LIVE — it is how pipelines with target types
            # outside TEMPLATE_RULES still generate code.
            logger.warning(f"No template for target_type '{raw_target}', falling back to full AI generation")
            # No template render → no DQDL block is baked into the code, so
            # the approval payload must not claim any rules (P28 R11.2).
            generated_dqdl_rules = ""

            fallback_prompt = load_prompt_template("code_generation_fallback.j2")
            agent = Agent(
                model=resolve_model_id(),
                system_prompt=fallback_prompt,
                hooks=[trace_plugin],
            )

            enhanced_prompt = load_prompt_template(
                "code_generation_enhanced.j2",
                agentcore_mode=False,
                source_schema=json.dumps(source_schema, indent=2),
                target_schema=json.dumps(target_schema, indent=2),
                mapping_config=json.dumps(mapping_config, indent=2),
                data_volume=data_volume,
                performance_requirements=json.dumps(performance_requirements, indent=2),
                target_type=target_type,
                target_format="parquet",
                agentcore_insights="",
                # P28 R13.3: reviewer feedback embedded on regeneration so the
                # fallback full-AI prompt also differs from the original.
                feedback_section=_build_feedback_section(regeneration_feedback or None),
                available_databases=["default"],
                common_job_configs="[]",
            )

            response = agent(enhanced_prompt)
            generated_code = extract_code_only(str(response))

            # Feed antipattern findings into the validation gate (P21 Req 2.6)
            fallback_antipatterns = validate_pyspark_antipatterns(generated_code)
            validation_result = validate_generated_code(generated_code, antipattern_count=len(fallback_antipatterns))
            if not validation_result["is_valid"]:
                raise ValueError(f"Fallback code validation failed: {'; '.join(validation_result['errors'])}")

            template_used = "fallback_full_ai_v1"
            agent_reasoning = f"Fallback full-AI generation for unknown target type '{raw_target}'"

        logger.info(f"Code generated successfully: {len(generated_code)} characters")

        # Flush trace data to DynamoDB
        trace_plugin.flush()

        # Audit: record code generation decision
        write_agent_audit_entry(
            agent_type="code_generation",
            action="generate_code",
            resource_id=code_version_id,
            details={
                "template_used": template_used,
                "line_count": len(generated_code.splitlines()),
                "regeneration_attempt": regeneration_attempt,
            },
            run_id=run_id,
            pipeline_id=pipeline_id,
        )

        # Get shared bucket and pipeline-specific prefix
        code_bucket, code_prefix = get_code_bucket_and_prefix(pipeline_id)

        # Store code in S3 with versioning for approval history
        version_suffix = f"_v{regeneration_attempt + 1}" if regeneration_attempt > 0 else ""
        code_s3_key = f"{code_prefix}{code_version_id}{version_suffix}.py"
        code_s3_uri = f"s3://{code_bucket}/{code_s3_key}"

        s3_client.put_object(
            Bucket=code_bucket,
            Key=code_s3_key,
            Body=generated_code.encode("utf-8"),
            ContentType="text/x-python",
            Metadata={
                "regeneration_attempt": str(regeneration_attempt),
                "agent_session_id": agent_session_id,
                "generation_timestamp": generation_timestamp,
            },
        )
        logger.info(f"Code stored in S3: {code_s3_uri}")

        # Calculate enhanced code quality metrics (template_used already set above)
        code_quality_metrics = calculate_code_quality_metrics(generated_code, template_used, agent_reasoning)

        # Convert any float values to Decimal for DynamoDB compatibility
        code_quality_metrics = convert_floats_to_decimal(code_quality_metrics)

        # Create approval payload (carries the inferred DQDL rules so
        # approvers see the data-quality constraints — P28 R11.2).
        #
        # p34 D2: ``requires_review`` is no longer passed. It takes its
        # ``True`` default (``create_approval_payload``'s signature), which is
        # honest at CreateCode time — this step runs BEFORE testing, so review
        # has not been ruled out. The payload field was never a review
        # conclusion (see that function's docstring), and the decision about
        # whether a human reviews now belongs to ``test_code``, the step whose
        # result ``CheckCodeApproval`` reads.
        approval_payload = create_approval_payload(
            code_version_id,
            generated_code,
            code_quality_metrics,
            agent_reasoning,
            template_used,
            code_s3_uri,
            dqdl_rules=generated_dqdl_rules,
        )

        # Extract required parameters from generated code
        from parameter_extractor import extract_required_parameters

        parameter_info = extract_required_parameters(generated_code)
        logger.info(f"Extracted parameters from code: {parameter_info['required_params']}")

        # Write to the agentic_data_ingestion_generated_code table with enhanced metadata
        generated_code_table.put_item(
            Item={
                "PK": f"CODE#{code_version_id}",
                "SK": "VERSION",
                "code_version_id": code_version_id,
                "run_id": run_id,
                "pipeline_id": pipeline_id,
                "source_schema_id": source_schema_id,
                "target_schema_id": target_schema_id,
                "mapping_id": mapping_id,
                "code_s3_uri": code_s3_uri,
                "generation_timestamp": generation_timestamp,
                "agent_session_id": agent_session_id,
                "generation_status": "success",
                "template_used": template_used,
                "regeneration_attempt": regeneration_attempt,
                "code_metadata": code_quality_metrics,
                "agent_reasoning": agent_reasoning,
                # p34 D2: "approval_required" is no longer written here.
                "required_parameters": parameter_info["required_params"],
                "optional_parameters": parameter_info.get("optional_params", []),
                "parameter_defaults": parameter_info.get("parameter_defaults", {}),
            }
        )
        logger.info(f"Enhanced metadata stored in {GENERATED_CODE_TABLE}")

        # Write to pipeline runs table (per spec) with approval data
        line_count = len(generated_code.splitlines())
        standardized_output = create_output_data(
            artifact_type="code_generation",
            primary_id=code_version_id,
            status="success",
            summary={
                "line_count": line_count,
                "template_used": template_used,
                "target_type": normalised_target,
                # p37 R2.4: the count sits in `summary` beside the other
                # per-run facts an approver reads; the findings themselves are
                # on `output_data.code_contradictions` below. 0 is a real
                # value — "checked, no contradiction" — for every run that
                # reaches here, including the fallback path.
                "contradiction_count": len(code_contradictions),
                # p49 R6.4: the same two-place shape for the date-format
                # findings, and a SEPARATE count rather than a sum with the
                # line above. The two say different things — one that a rule
                # can never pass, the other that a parse will null a column —
                # and an approver reading one number could not tell which
                # happened. 0 is a real value here too: "checked, no
                # mismatch", including on the fallback path and on a run whose
                # source samples were unreadable.
                "date_format_finding_count": len(date_format_findings),
                # p49 R5.1/R5.3: a third count, for the same reason there is a
                # second. "The emitted format matches nothing" and "the samples
                # do not settle the emitted format" are different facts about
                # different runs, and summing them would tell an approver that
                # something happened to a date column without saying what. 0 is
                # a real value here too: "checked, and the samples settle it".
                "date_format_ambiguity_count": len(date_format_ambiguities),
                # p64 R6.6 — whether the rendered job carries a DQ block for THIS
                # pipeline. Recorded beside `template_used` because it is the
                # second half of the same fact: since R6.1 all five templates
                # include the DQ partial, so the template name alone no longer
                # answers "does this job gate its write on data quality", and
                # `shared/quality_verdict.derive_quality_verdict` is what reports
                # that answer to the dashboard.
                #
                # Resolved by the SAME function `generate_code` used to set the
                # render variable (`code_assembly.resolve_data_quality_enabled`),
                # not by a second derivation here — a renderer and a reporter that
                # each derive the answer are how they come to disagree, which is
                # the divergence `LGR-eee8ac8-6`'s revisit_when points at.
                #
                # `template_name_j2` is `""` on the fallback full-AI path, where
                # the resolver returns False because no template — and therefore no
                # DQ block — was rendered at all. Always a bool, never absent, so a
                # reader can tell "off" from "row predates p64".
                "data_quality_enabled": code_assembly.resolve_data_quality_enabled(
                    pipeline_metadata, template_name_j2
                ),
            },
            s3_uris={
                "code_file": code_s3_uri,
            },
        )
        # p37 R2.4: the named finding, on the `create_code` state row the
        # approval surfaces already read from. Recorded, not enforced — see
        # the "proceeding as-is" arm above and design C8's do-NOT-block.
        standardized_output["code_contradictions"] = code_contradictions
        # p49 R6.4: the date-format findings on the same row, under their own
        # key. `LGR-9e76249-4` (template / ratified) names this two-place shape
        # exactly — "the finding is persisted where approvers already read from
        # (`output_data.code_contradictions` plus
        # `output_data.summary.contradiction_count`) and it is merged into the
        # `regeneration_feedback` dict" — and it is what makes non-blocking mean
        # something rather than nothing.
        #
        # A SEPARATE key, not appended to `code_contradictions`: the record an
        # approver reads names the two kinds separately, and each carries a
        # different field set (`{column, function, format, samples, reason}`
        # against `{column, rule, reason}`), so merging them would produce a
        # list no consumer could read without discriminating on shape.
        #
        # These are the findings from the transform that was STORED — the
        # carrier is replaced with the re-check's results in the regeneration
        # arm above, so a finding about a discarded block never reaches here.
        standardized_output["date_format_findings"] = date_format_findings
        # p49 R5.1/R5.2/R5.3 (design C3): the format ambiguities, on the same
        # row, under their own key, with their own count above. Each entry
        # names BOTH readings the samples permit and marks which one the code
        # assumed (`assumed_format`) — never a single format as settled
        # (do-NOT 8).
        #
        # **This is the RECORD arm, and it does not force a human.** Design O3
        # is the reason and it is a measurement gap, not a preference:
        # `05/12/2024`-shaped values are common, nobody has measured how often
        # C3 fires, and a clause that forced review on an unmeasured frequency
        # could make C3 an approval-fatigue problem rather than a safety
        # feature. `LGR-9e76249-7`'s forcing clause is the *precedent* that a
        # mapping condition CAN require a human — cited in design C3 as
        # precedent, not as instruction. The narrowing inside
        # `_format_ambiguity` (only day/month order, only where the two
        # readings actually disagree about a sampled value) exists so that
        # frequency, when someone measures it from these counts, is a number
        # about real risk rather than about the checker's imagination.
        #
        # **Follow-up, deliberately left open (O3, and backlog.md item 21):**
        # measure `summary.date_format_ambiguity_count` across real runs. If it
        # is rare, the forcing clause becomes cheap and R5.3's "surface the
        # ambiguity to a human" gets its literal reading — at the *mapping*
        # approval gate, which R5.3 names and which sits one step UPSTREAM of
        # here, so implementing it means moving the sample read into
        # `schema_mapping` rather than adding a gate at this row. If it is
        # common, the recording arm is already the right answer and the
        # narrowing is where to spend the next effort. Do not add a gate here
        # without that measurement.
        standardized_output["date_format_ambiguities"] = date_format_ambiguities
        # Keep legacy fields for backward compatibility
        standardized_output["code_version_id"] = code_version_id
        standardized_output["code_s3_uri"] = code_s3_uri
        standardized_output["agent_session_id"] = agent_session_id
        # P28 R13.1: code_quality_score is removed — it was the keyword-count
        # score with no honest measurement behind it.
        # Store approval fields as a native dict inside output_data (not a JSON string)
        standardized_output["approval"] = approval_payload

        pipeline_runs_table.put_item(
            Item={
                "run_id": run_id,  # Partition key
                # P28 R8.2 (task 14.2): canonical step name — the legacy
                # "CodeGeneration" state-row variant is retired.
                "step_name": StepName.CREATE_CODE,
                "status": "success",
                "step_sequence": STEP_SEQUENCE[StepName.CREATE_CODE],
                "timestamp": generation_timestamp,
                "pipeline_id": pipeline_id,  # For PipelineIndex GSI
                "original_pipeline_id": pipeline_id,  # Store original for grouping
                "regeneration_attempt": regeneration_attempt,
                # p34 D2: "approval_required" is no longer written here.
                "output_data": standardized_output,
            }
        )
        logger.info(f"Pipeline state with approval data stored in {PIPELINE_RUNS_TABLE}")

        # Publish pipeline state change event (convert Decimals to floats for JSON)
        # P28 R13.1: quality_score is removed from published metrics — the
        # keyword-count score had no honest measurement behind it.
        metrics = {
            "duration_ms": int((_time.time() - _metrics_start) * 1000),
            "lines_of_code": int(code_quality_metrics.get("lines_of_code", 0)),
            "functions_generated": int(code_quality_metrics.get("functions_generated", 0)),
            "regeneration_attempt": regeneration_attempt,
        }

        publish_pipeline_state_change(
            pipeline_id=pipeline_id,
            execution_id=event.get("execution_arn", "unknown"),
            stage="agentic_data_ingestion_create_code",
            status="completed",
            run_id=run_id,
            metrics=metrics,
        )

        # P28 R19.5: NO approval event is published here. The former code
        # fabricated an approval_id ({run_id}_code_generation_{timestamp})
        # that never had an approvals-table record — a "ghost" id. The
        # approval_handler owns the real approval record and publishes the
        # honest 'Approval Status Change' event with the real id when the
        # state machine reaches the approval step.

        # Publish stage metrics
        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="CodeGeneration",
                    pipeline_id=pipeline_id or "unknown",
                    status="completed",
                    duration_ms=_duration_ms,
                )
            except Exception:
                # p39 R6.5 / requirements constraint 8: log, never fail the step.
                # A stage-duration emission is telemetry; losing it must not turn a
                # successful generation into a failed one. What is NOT acceptable is
                # losing it with no trace, which is what the `except Exception: pass`
                # this replaces did — the same silent-drop shape that once cost a
                # performance-comparison metric (commit 8f3f9e1b30, "Replace 7 bare
                # except clauses with specific types + logging", history rather than
                # a ratified decision). `exc_info` is what makes a recurrence
                # diagnosable.
                logger.warning(
                    "Failed to record CodeGeneration stage metrics for pipeline=%s (success path)",
                    pipeline_id or "unknown",
                    exc_info=True,
                )
        else:
            # p39 R6.6: the import guard at :114-117 left both emits unreachable
            # with no signal at all. Design §4 C5 ruled this cause OUT for the
            # AgentCore container — `agentcore/create_code/Dockerfile` puts
            # `shared` on PYTHONPATH, so `metrics_facade` imports there — but the
            # branch stays reachable if the Lambda layer ever loses the module, and
            # an unreachable emit is indistinguishable from a working one without
            # this line.
            logger.warning(
                "PipelineMetrics is unavailable (metrics_facade did not import), so no CodeGeneration "
                "stage duration was recorded for pipeline=%s (success path)",
                pipeline_id or "unknown",
            )

        # Return output per spec with approval information
        return {
            "statusCode": 200,
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "code_version_id": code_version_id,
            "code_s3_uri": code_s3_uri,
            "agent_session_id": agent_session_id,
            "regeneration_attempt": regeneration_attempt,
            "template_used": template_used,
            # p34 D2 / amended, ratified R3.4: no code-generation approval
            # field is published from this step. The state machine reads
            # $.test_code_result.approval_required and never read this one.
            "next_step": "code_testing",
        }

    except Exception as e:
        logger.error(f"Error: {e!s}", exc_info=True)

        # For StepFailure, str(e) is its JSON serialization — use the
        # human-readable message for dashboard-facing records instead.
        error_message = e.message if isinstance(e, StepFailure) else str(e)

        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="CodeGeneration",
                    pipeline_id=event.get("pipeline_id", "unknown"),
                    status="failed",
                    duration_ms=_duration_ms,
                )
            except Exception:
                # p39 R6.5 / requirements constraint 8: log, never fail the step —
                # and on this path especially, never displace the real failure. The
                # handler is already unwinding an exception it logged at the top of
                # this `except` block and will re-raise below; a metrics problem must
                # not become the error the run is recorded under.
                logger.warning(
                    "Failed to record CodeGeneration stage metrics for pipeline=%s (failure path)",
                    event.get("pipeline_id", "unknown"),
                    exc_info=True,
                )
        else:
            # p39 R6.6, same reasoning as the success path above.
            logger.warning(
                "PipelineMetrics is unavailable (metrics_facade did not import), so no CodeGeneration "
                "stage duration was recorded for pipeline=%s (failure path)",
                event.get("pipeline_id", "unknown"),
            )

        # Write error to pipeline runs table - FIXED: Use correct composite key
        try:
            run_id = event.get("run_id", "unknown")
            pipeline_runs_table.put_item(
                Item={
                    "run_id": run_id,  # Partition key
                    "step_name": StepName.CREATE_CODE,
                    "status": "failed",
                    "step_sequence": STEP_SEQUENCE[StepName.CREATE_CODE],
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pipeline_id": event.get("pipeline_id", "unknown"),  # For PipelineIndex GSI
                    "original_pipeline_id": event.get("pipeline_id", "unknown"),
                    "error_message": error_message,
                    "error_type": "ProcessingError",
                }
            )
        except Exception as db_error:
            logger.error(f"Failed to write error to DynamoDB: {db_error!s}")

        # Publish failure event
        run_id = event.get("run_id", "unknown")

        publish_pipeline_state_change(
            pipeline_id=event.get("pipeline_id", "unknown"),
            execution_id=event.get("execution_arn", "unknown"),
            stage="agentic_data_ingestion_create_code",
            status="failed",
            run_id=run_id,
            error_details=error_message,
        )

        # P28 R3.3/R3.4: re-raise specific StepFailures as-is so their
        # message and details (missing mapping id, AI error) survive to the
        # SFN Cause instead of being double-wrapped.
        if isinstance(e, StepFailure):
            raise

        # P28 R1.1: raise so the Step Functions task fails and Retry/Catch fire
        # (a returned 500 payload would be treated as task success and flow to
        # TestCode as garbage input).
        raise StepFailure(
            "create_code",
            str(e),
            details={
                "run_id": run_id,
                "pipeline_id": event.get("pipeline_id", "unknown"),
                # Deterministic error ID for correlation (was the old payload's code_version_id)
                "code_version_id": f"error-{run_id}",
            },
        ) from e
