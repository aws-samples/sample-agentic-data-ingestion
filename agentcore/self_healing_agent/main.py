# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Runtime entrypoint for the self-healing agent.

Receives pipeline failure events, classifies the error, applies an
automated remediation, and re-triggers the pipeline through the STANDARD
pipeline (P28 R9.6): the heal rerun's create_code step regenerates the
transform and test_code re-validates it. There is no side-channel code
storage — the old path that stored LLM-regenerated raw code in DynamoDB
(``generated_by="self_healing_agent"``, never read downstream, bypassing
the test_code validation pipeline) is deleted. Config remediations (OOM →
WorkerType upgrade) ride the rerun input as ``job_config_overrides``.

The outcome record is honest (P28 R9.7): ``rerun_started`` is True only
when ``start_execution`` actually succeeded with the corrected input. The
heal rerun's own job_completion later links the rerun RESULT
(``rerun_result``/``linked_run_id``) back onto this record.

Output is written to the state table and audit log so the dashboard
can display self-healing activity.
"""

import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", force=True)
for name in list(logging.Logger.manager.loggerDict):
    logging.getLogger(name).propagate = True

sys.path.insert(0, "/app/shared")

from bedrock_agentcore.runtime import BedrockAgentCoreApp

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()

# ---------------------------------------------------------------------------
# Logging filter — injects run_id into every log record
# ---------------------------------------------------------------------------


class _RunIdFilter(logging.Filter):
    """Injects run_id into every log record so container logs are searchable."""

    run_id = ""
    pipeline_id = ""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id  # type: ignore[attr-defined]
        record.pipeline_id = self.pipeline_id  # type: ignore[attr-defined]
        return True


_run_filter = _RunIdFilter()

# ---------------------------------------------------------------------------
# Error classification (Task 1.2)
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: dict[str, list[str]] = {
    "schema_mismatch": [
        r"AnalysisException",
        r"cannot resolve",
        r"schema mismatch",
        r"column not found",
    ],
    "oom_error": [
        r"OutOfMemoryError",
        r"Container killed by YARN",
        r"memory exceeded",
    ],
    "type_cast_error": [
        r"NumberFormatException",
        r"DateTimeParseException",
        r"cannot cast",
        r"type mismatch",
    ],
    "permission_error": [
        r"AccessDeniedException",
        r"not authorized",
        r"permission denied",
    ],
    # The database rejected the STATEMENT the generated job sent — a
    # code-generation defect, not a data, capacity or credential problem.
    #
    # Appended LAST deliberately: `_ERROR_PATTERNS` is matched in insertion
    # order and the first hit wins, so adding a category here cannot reclassify
    # any message the four above already claim. That matters for Spark's
    # `AnalysisException` text, which sometimes contains the word "syntax" while
    # genuinely being a `schema_mismatch`.
    #
    # Engine-specific because the rejection text is: there is no common string.
    # `SQLSyntaxErrorException` is the JDBC-level wrapper and catches drivers
    # whose server text is not listed.
    "sql_syntax_error": [
        r"You have an error in your SQL syntax",  # MySQL / MariaDB / Aurora-MySQL
        r"syntax error at or near",  # PostgreSQL / Aurora-PostgreSQL
        r"Incorrect syntax near",  # SQL Server
        r"ORA-00900|ORA-00933|ORA-00936",  # Oracle: invalid statement / not ended / missing expr
        r"SQLSyntaxErrorException",  # JDBC-level wrapper, any driver
    ],
}

# Descriptions for the classes that CANNOT be auto-remediated. Keyed so each one
# says what the operator should actually look at, instead of the bare
# "Error type '<x>' requires manual intervention" that told them nothing.
_MANUAL_REVIEW_DESCRIPTIONS: dict[str, str] = {
    "sql_syntax_error": (
        "The database rejected the SQL the generated job sent. This is a "
        "code-generation defect, not a data or capacity problem — a rerun would "
        "render the same statement and fail identically, so it is deliberately "
        "NOT retried. Inspect the generated script's SQL. Identifier quoting is "
        "engine-specific: MySQL, MariaDB and Aurora-MySQL require backticks, "
        "every other supported engine uses double quotes."
    ),
    "permission_error": (
        "An IAM or database grant is missing. Rerunning cannot change the "
        "outcome — grant the permission the message names, then rerun manually."
    ),
}


def classify_error(error_message: str) -> str:
    """Classify an error message into a known category.

    Returns one of: schema_mismatch, oom_error, type_cast_error,
    permission_error, or unknown.
    """
    for category, patterns in _ERROR_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, error_message, re.IGNORECASE):
                return category
    return "unknown"


# ---------------------------------------------------------------------------
# Remediation functions (Tasks 1.3 – 1.5)
# ---------------------------------------------------------------------------

import boto3

_dynamodb = boto3.resource("dynamodb")
_glue_client = boto3.client("glue")
_sfn_client = boto3.client("stepfunctions")
_logs_client = boto3.client("logs")


def _require_env(name: str, default: str | None = None) -> str:
    """Thin wrapper — import shared env_utils at call time."""
    from env_utils import require_env
    return require_env(name, default)


def remediate_by_rerun(error_class: str) -> dict[str, Any]:
    """Remediate a code-level failure by rerunning the standard pipeline.

    P28 R9.6: schema-mismatch and type-cast failures used to be "remediated"
    by an LLM regenerating raw transform code straight into DynamoDB
    (``generated_by="self_healing_agent"``) — code nothing downstream ever
    read, bypassing the test_code validation pipeline entirely. That side
    channel is deleted. The remediation for these classes IS the rerun: the
    heal rerun's create_code step regenerates the transform from the current
    approved mappings and test_code re-validates it before deployment.
    """
    descriptions = {
        "schema_mismatch": (
            "Schema mismatch — rerun regenerates the transform from current mappings "
            "through the standard pipeline (create_code) and re-tests it (test_code)."
        ),
        "type_cast_error": (
            "Type cast error — rerun regenerates the transform through the standard "
            "pipeline (create_code) and re-tests it (test_code)."
        ),
    }
    return {
        "action": "rerun_pipeline",
        "description": descriptions.get(
            error_class,
            f"{error_class} — rerun regenerates and re-tests code through the standard pipeline.",
        ),
    }


# Worker type upgrade path for OOM remediation
_WORKER_UPGRADE_PATH = ["G.1X", "G.2X", "G.4X", "G.8X"]


def remediate_oom(
    pipeline_id: str,
    job_name: str,
    current_worker_type: str | None = None,
) -> dict[str, Any]:
    """Remediate an OOM error by upgrading the Glue job worker type.

    Reads the current job config, determines the next worker tier,
    and updates the job definition.
    """
    # Read current job config
    job_resp = _glue_client.get_job(JobName=job_name)
    job_def = job_resp["Job"]

    current_type = current_worker_type or job_def.get("WorkerType", "G.1X")
    current_idx = (
        _WORKER_UPGRADE_PATH.index(current_type)
        if current_type in _WORKER_UPGRADE_PATH
        else 0
    )

    if current_idx >= len(_WORKER_UPGRADE_PATH) - 1:
        return {
            "action": "no_upgrade_available",
            "description": f"Already at maximum worker type ({current_type}). Manual review required.",
        }

    new_type = _WORKER_UPGRADE_PATH[current_idx + 1]

    # Update the Glue job with the new worker type
    update_kwargs: dict[str, Any] = {
        "JobName": job_name,
        "JobUpdate": {
            "WorkerType": new_type,
            "Role": job_def["Role"],
            "Command": job_def["Command"],
        },
    }
    # Preserve optional fields if present
    if "NumberOfWorkers" in job_def:
        update_kwargs["JobUpdate"]["NumberOfWorkers"] = job_def["NumberOfWorkers"]
    if "GlueVersion" in job_def:
        update_kwargs["JobUpdate"]["GlueVersion"] = job_def["GlueVersion"]
    if "DefaultArguments" in job_def:
        update_kwargs["JobUpdate"]["DefaultArguments"] = job_def["DefaultArguments"]

    _glue_client.update_job(**update_kwargs)

    logger.info(
        "OOM remediation: upgraded %s from %s to %s",
        job_name,
        current_type,
        new_type,
    )
    return {
        "action": "upgrade_worker_type",
        "previous_worker_type": current_type,
        "new_worker_type": new_type,
        "description": f"Upgraded worker type from {current_type} to {new_type}",
    }


# ---------------------------------------------------------------------------
# Re-trigger pipeline (Task 1.6)
# ---------------------------------------------------------------------------


def _load_pipeline_record(pipeline_id: str) -> dict[str, Any]:
    """Fetch the pipeline record the rerun contract sources its input from.

    P28 R9.1: the heal rerun must carry the full trigger contract —
    source_type and (for S3 pipelines) source_bucket/source_prefix come from
    this record. A missing record means the rerun input cannot be built
    correctly, so fail loudly instead of starting an execution that dies at
    file_validation.
    """
    pipelines_table = _dynamodb.Table(
        _require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
    )
    item = pipelines_table.get_item(Key={"pipeline_id": pipeline_id}).get("Item")
    if not item:
        raise ValueError(
            f"Pipeline record not found for {pipeline_id!r} — cannot build the "
            f"heal rerun input (source_type/source_bucket unknown)"
        )
    return item


def _fallback_source_bucket() -> str:
    """Source-bucket fallback for pipeline records without a configured bucket.

    Mirrors the dashboard trigger_batch chain: ``SOURCE_BUCKET`` env var,
    then the deterministic ``adi-source-files-{account}-{region}`` name.
    """
    bucket = os.environ.get("SOURCE_BUCKET")
    if bucket:
        return bucket
    account_id = os.environ.get("AWS_ACCOUNT_ID") or boto3.client("sts").get_caller_identity()["Account"]
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )
    return f"adi-source-files-{account_id}-{region}"


def _resolve_state_machine_arn() -> str:
    """Resolve the pipeline state-machine ARN for re-triggering.

    The AgentCore container is not given ``STATE_MACHINE_ARN`` because the
    pipeline state machine is created in the Orchestration stack, which is
    deployed AFTER this AgentCore stack — a CloudFormation cross-stack
    reference would be circular. So derive the ARN from the deterministic
    state-machine name + account + region, the same construction used by
    compute-stack.ts and the dashboard API. Honour an explicit
    ``STATE_MACHINE_ARN`` if one is ever injected into the container.
    """
    explicit = os.environ.get("STATE_MACHINE_ARN")
    if explicit:
        return explicit
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )
    sm_name = os.environ.get("STATE_MACHINE_NAME", "agentic-data-ingestion-pipeline")
    account_id = boto3.client("sts").get_caller_identity()["Account"]
    return f"arn:aws:states:{region}:{account_id}:stateMachine:{sm_name}"


def retrigger_pipeline(
    pipeline_id: str,
    run_id: str,
    fix_description: str,
    job_config_overrides: dict[str, Any] | None = None,
    pipeline_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a new Step Functions execution with the fixed configuration.

    P28 R9.1/R9.2: the rerun input is built by the shared trigger contract
    (``shared/rerun_contract.build_trigger_input``) from the pipeline record,
    so heal reruns carry the full trigger contract — source_type, and for S3
    pipelines source_bucket + source_prefix — instead of the pre-contract
    ``{pipeline_id, run_id, triggered_by, original_run_id, fix_description}``
    shape that died at file_validation with a KeyError. ``triggered_by`` is
    the canonical Heal_Guard value ``"self_healing"`` (R9.4), and
    ``job_config_overrides`` carries remediation config (e.g. a WorkerType
    upgrade) through the rerun (R9.5).

    Rerun id (R9.4): stable ``f"{run_id}-heal"``, derived once from the run
    being healed. No ``-retry-retry-…`` growth is possible because the
    Heal_Guard — not string surgery — prevents a second heal: the heal
    rerun's own state rows are marked ``triggered_by="self_healing"`` (via
    file_validation persisting the SFN input's marker), so the agent refuses
    to act on its failure. Note the id is lineage/traceability only:
    file_validation generates a FRESH run_id for the rerun's state rows (its
    prefix/JDBC handlers never adopt the SFN input's run_id).

    ``pipeline_record`` (P30 R15.2): the caller may pass a record it has already
    read, and the handler does — the Partial_Write_Guard needs the target type
    and write mode BEFORE the re-run decision, so the read was hoisted above
    the gate. Omitting it preserves the pre-guard behaviour of reading the
    record here, so the ValueError on a missing record is unchanged.

    Returns the new execution ARN and heal rerun_id.
    """
    from rerun_contract import build_trigger_input

    state_machine_arn = _resolve_state_machine_arn()
    rerun_id = f"{run_id}-heal"

    if pipeline_record is None:
        pipeline_record = _load_pipeline_record(pipeline_id)

    # S3 pipelines without a configured bucket use the same fallback chain
    # the dashboard's batch trigger applies.
    build_kwargs: dict[str, Any] = {}
    if pipeline_record.get("source_type", "S3_UPLOAD") == "S3_UPLOAD":
        record_bucket = pipeline_record.get("source_bucket")
        if not (isinstance(record_bucket, str) and record_bucket.strip()):
            cfg = pipeline_record.get("pipeline_config")
            record_bucket = cfg.get("source_bucket") if isinstance(cfg, dict) else None
        if not (isinstance(record_bucket, str) and record_bucket.strip()):
            build_kwargs["source_bucket"] = _fallback_source_bucket()

    execution_input = build_trigger_input(
        pipeline_record,
        triggered_by="self_healing",
        run_id=rerun_id,
        original_run_id=run_id,
        job_config_overrides=job_config_overrides,
        **build_kwargs,
    )

    logger.info("Heal rerun input built (fix: %s)", fix_description)

    ts_hash = uuid.uuid4().hex[:8]
    execution_name = f"self-heal-{pipeline_id}-{ts_hash}"

    resp = _sfn_client.start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps(execution_input),
    )

    execution_arn = resp["executionArn"]
    logger.info(
        "Re-triggered pipeline %s: execution_arn=%s rerun_id=%s",
        pipeline_id,
        execution_arn,
        rerun_id,
    )
    return {
        "execution_arn": execution_arn,
        "rerun_id": rerun_id,
    }


# ---------------------------------------------------------------------------
# Honest outcome record (P28 R9.7)
# ---------------------------------------------------------------------------


def build_outcome_record(
    *,
    run_id: str,
    pipeline_id: str,
    healing_id: str,
    error_classification: str,
    remediation_action: str,
    remediation_description: str,
    rerun_id: str,
    rerun_started: bool,
    rerun_status: str,
    rerun_execution_arn: str = "",
    rerun_start_error: str = "",
    diagnosis_duration_ms: int = 0,
    total_duration_ms: int = 0,
    rerun_safety: dict[str, Any] | None = None,
    declined_action: str = "",
) -> dict[str, Any]:
    """Build the honest self-healing outcome record (P28 R9.7).

    Design shape: ``{healing_id, root_cause, action, rerun_id,
    rerun_started}`` plus run/pipeline ids and timestamp. ``rerun_started``
    is True ONLY when ``start_execution`` actually succeeded with the
    corrected input — a failed start is recorded as ``rerun_started=False``
    with the error, never as success.

    The record also keeps every field the dashboard consumes
    (``use-self-healing.ts``: agent_type, error_classification,
    remediation_applied, original_run_id, rerun_id, rerun_status,
    diagnosis_duration_ms, total_duration_ms, timestamp) — shape stays
    stable. ``rerun_status`` transitions ``running`` → ``success``/``failed``
    when the heal rerun's own job_completion links the terminal result back
    onto this record (``rerun_result`` / ``linked_run_id``).

    ``rerun_safety`` / ``declined_action`` (P30 R15.2, read by R15.3): the
    Partial_Write_Guard verdict from :func:`evaluate_rerun_safety`, recorded
    whenever a re-run was contemplated. When the verdict is unsafe the record
    also carries ``skip_reason="partial_write_guard"`` and ``skip_detail`` (the
    verdict's reason) — the same shape the Heal_Guard skip record uses — plus
    ``declined_action``, the remediation that WOULD have re-run. The verdict
    names the target type and write mode it graded, which is the only way a
    stale ``write_mode`` (render-time only) can be diagnosed after the fact.
    """
    record: dict[str, Any] = {
        "run_id": run_id,
        "step_name": "self_healing",
        "agent_type": "self_healing",
        # Design shape (P28 R9.7)
        "healing_id": healing_id,
        "root_cause": error_classification,
        "action": remediation_action,
        "rerun_started": rerun_started,
        # Dashboard-consumed fields — keep stable.
        "error_classification": error_classification,
        "remediation_applied": remediation_description,
        "original_run_id": run_id,
        # Pipeline traceability. Deliberately NOT named ``pipeline_id``:
        # that attribute is the PipelineIndex GSI partition key, and healing
        # records are not pipeline-step rows — surfacing them in GSI queries
        # would change mapping-lookup behavior.
        "original_pipeline_id": pipeline_id,
        "rerun_id": rerun_id,
        "rerun_status": rerun_status,
        "diagnosis_duration_ms": diagnosis_duration_ms,
        "total_duration_ms": total_duration_ms,
        # ISO-8601: ``timestamp`` is the RANGE key of the agentic_data_ingestion_state
        # PipelineIndex GSI (typed S); an int is rejected with
        # "Type mismatch for Index Key timestamp Expected: S Actual: N".
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if rerun_execution_arn:
        record["rerun_execution_arn"] = rerun_execution_arn
    if rerun_start_error:
        record["rerun_start_error"] = rerun_start_error
    if rerun_safety is not None:
        record["rerun_safety"] = rerun_safety
        if not rerun_safety.get("safe"):
            record["skip_reason"] = "partial_write_guard"
            record["skip_detail"] = rerun_safety.get("reason", "")
    if declined_action:
        record["declined_action"] = declined_action
    return record


# ---------------------------------------------------------------------------
# Partial_Write_Guard (P30 R15.2 / R15.4)
# ---------------------------------------------------------------------------

# (target, write_mode) combinations where a re-run is safe even though the
# failed run may have written rows.
#
# An ALLOW-list, not a deny-list on write_mode. ``write_mode``'s value space is
# open: ``create_pipeline`` validates it against overwrite|append|merge
# (pipelines_crud.py:632-635) but ``edit_pipeline`` merges arbitrary
# target_config keys with NO validation (:996-1013), and a vestigial ``upsert``
# value exists (job_arguments.py:681-687) that no template reads — it renders as
# append on JDBC. So an unrecognised mode, or a target type added later, must
# fail CLOSED rather than fall through to "safe".
#
# p30 task 6.1 established every entry by rendering each template for each
# write_mode, so the verdicts describe what the generator actually emits rather
# than what the conditionals appear to say.
#
# The PER-ENTRY rationale is NOT repeated here. Each exemption's reason lives in
# ``_SAFE_RERUN_REASONS`` below and is emitted on the verdict, so it travels with
# the decision it justifies (R15.5, p30 task 6.4) instead of sitting in a comment
# that can drift from the set. The two omissions that look like exemptions are
# recorded the same way, in ``_NOT_EXEMPT_REASONS``.
_SAFE_TO_RERUN: frozenset[tuple[str, str]] = frozenset(
    {
        ("s3_tables", "merge"),
        ("s3_tables", "overwrite"),
        ("jdbc", "overwrite"),
        ("s3_parquet", "overwrite"),
    }
)

# R15.5 (p30 task 6.4): why each exemption is safe — one reason per combination,
# stated together with what the reason is NOT.
#
# The "what it is not" halves are the substance, not decoration. R15.5's own
# example ("Iceberg snapshot isolation") is the easiest of these to state
# wrongly: snapshot isolation is why ``overwrite`` is exempt and is NOT why
# ``merge`` is exempt, and a note that collapses the two is false about both.
# Likewise two of the four are safe only BY CLOBBER — the end state converges,
# but the intermediate state is "table dropped" / "path deleted". Anyone auditing
# those two needs that in the same breath as "safe to re-run", so it is in the
# reason text and not only in this comment; it is deliberately absent from the
# two Iceberg reasons, where it would be false.
#
# These strings are emitted verbatim as ``verdict["reason"]`` and persisted on
# the outcome record under ``rerun_safety.reason`` (build_outcome_record:453),
# which is what makes an exercised exemption auditable per run.
_SAFE_RERUN_REASONS: dict[tuple[str, str], str] = {
    ("s3_tables", "merge"): (
        "Re-run permitted: the MERGE statement itself is idempotent — matched "
        "rows are rewritten with the same values and unmatched rows are "
        "inserted, so the table converges on any number of re-runs. The "
        "exemption is a property of the STATEMENT, not of Iceberg being "
        "transactional and not of snapshot isolation: s3_tables + 'append' is "
        "just as atomic and is deliberately NOT exempt."
    ),
    ("s3_tables", "overwrite"): (
        "Re-run permitted: the write is one atomic Iceberg createOrReplace "
        "commit. Snapshot isolation means a killed job leaves no visible rows, "
        "and the re-run replaces the table wholesale. Snapshot isolation is why "
        "'overwrite' is exempt; it is not why 'merge' is exempt."
    ),
    ("jdbc", "overwrite"): (
        "Re-run permitted, safe BY CLOBBER: 'truncate' is unset, so Spark DROPs "
        "and recreates the table and the re-run's end state is correct. This is "
        "NOT transactional and NOT non-destructive — the table is absent "
        "mid-run. Safe here means the end state converges, not that nothing was "
        "destroyed on the way."
    ),
    ("s3_parquet", "overwrite"): (
        "Re-run permitted, safe BY CLOBBER: the output path is deleted, then "
        "rewritten. NOT partition-scoped — 'partitionOverwriteMode' is unset, so "
        "EVERY partition is replaced, not only the ones this run touched. Safe "
        "here means the end state converges, not that nothing was destroyed on "
        "the way."
    ),
}

# R15.5's mirror image (p30 task 6.4): the two combinations that read as
# exemptions and are deliberately outside ``_SAFE_TO_RERUN``. Recorded here, in
# the same shape as the exemptions, because the next reader of the allow-list
# will ask about exactly these two, and a near-miss left unwritten reads as an
# oversight. A test pins these keys as disjoint from ``_SAFE_TO_RERUN``, so
# neither can be quietly promoted.
#
# These strings are NOT emitted. The user-facing decline reason is built in
# ``evaluate_rerun_safety`` and reproduced verbatim by
# ``send_self_healing_declined_alert`` (p30 task 6.3); splicing this rationale
# into it would change wording that task pinned. This is the audit record for
# the omission, not the message about it.
_NOT_EXEMPT_REASONS: dict[tuple[str, str], str] = {
    ("s3_tables", "append"): (
        "Not exempt, and this is a judgement rather than an oversight. The write "
        "is atomic, so no PARTIAL rows are possible — the reason it is excluded "
        "is the commit-then-fail window, which nothing at this decision point "
        "can see. For a JDBC incremental source the window is concrete rather "
        "than theoretical: the watermark put_item runs AFTER the target write "
        "and re-raises (glue_job_jdbc_source.py.j2:326-359), so the commit "
        "stands, the Glue run is FAILED, the watermark did not advance, and the "
        "re-run re-reads and re-appends the same window. p30 task 6.2 declined "
        "to exempt it on that basis."
    ),
    ("glue_catalog", "overwrite"): (
        "Not exempt, and the single most important omission in the guard: a Glue "
        "Catalog target ignores write_mode entirely. Both branches of "
        "_write_glue_catalog.py.j2:8-22 call write_dynamic_frame.from_catalog, "
        "differing only by additional_options={'enableUpdateCatalog': True} on "
        "append; there is no mode parameter and no delete anywhere on the path. "
        "So 'overwrite' APPENDS here and a re-run duplicates. This is why design "
        "§5 O13's proposed write_mode != 'overwrite' fallback is wrong in the "
        "unsafe direction, and why the guard is an allow-list."
    ),
}

# Persisted ``target_config["type"]`` spellings → the target identity whose
# write semantics apply. Mirrors the code generator's own normalisation
# (code_assembly.py:206-216): the guard has to classify a target exactly the way
# the renderer classified it, or it grades a write block that was never emitted.
#
# Two deviations from that table, both deliberate:
#   * ``rds_database`` folds into ``jdbc``. code_assembly keeps the two spellings
#     distinct in its type_map but TEMPLATE_RULES routes BOTH to the same
#     template (code_assembly.py:47-51), so they share these write semantics.
#   * The S3 Tables aliases are kept. Production persists the UPPERCASE wizard
#     spellings — ``NEW_S3_TABLE_BUCKET`` / ``EXISTING_S3_TABLE_BUCKET`` /
#     ``GLUE_CATALOG`` / ``JDBC`` / ``S3_PARQUET`` (CreatePipelinePage.tsx:354,
#     :361, :367, :377, :405), with ``RDS_DATABASE`` the legacy alias. A bare
#     lower-case of ``NEW_S3_TABLE_BUCKET`` matches nothing, so omitting these
#     two rows would fail closed on every S3 Tables pipeline — the default
#     target — and silently switch self-healing off for the common case.
#
# Keys are lower-case because the persisted value is lower-cased before lookup.
_TARGET_TYPE_NORMALISATION: dict[str, str] = {
    "new_s3_table_bucket": "s3_tables",
    "existing_s3_table_bucket": "s3_tables",
    "s3_tables": "s3_tables",
    "glue_catalog": "glue_catalog",
    "jdbc": "jdbc",
    "rds_database": "jdbc",
    "s3_parquet": "s3_parquet",
}

_NO_SIGNAL_NOTE = (
    "No partial-write signal exists at this decision point, so any "
    "non-SUCCEEDED terminal state is treated as 'may have written rows'."
)


def evaluate_rerun_safety(pipeline_record: dict[str, Any]) -> dict[str, Any]:
    """Is a heal re-run safe for this pipeline's target type and write mode?

    R15.2/R15.4. Returns a verdict dict; it never raises and never calls AWS.

    **Matrix-gated, not detection-gated.** This does NOT detect a partial
    write — no such signal exists. The dispatch payload carries only
    ``{pipeline_id, run_id, job_name, job_run_id, error_message, state}``
    (job_completion/lambda_function.py:432-441, relayed verbatim by
    self_healing_dispatch/lambda_function.py:70-75), ``collect_job_metrics``
    returns no row counts, and no state row records rows-written for any target.
    So the guard treats **any** non-``SUCCEEDED`` terminal state as "may have
    written rows" and decides purely on whether re-running this (target, mode)
    would be harmful if rows had landed. The verdict must never be reported as
    a detected partial write.

    **``write_mode`` is render-time only, so the value graded here may not be
    what the failed run did.** No template declares a ``write_mode`` runtime
    argument — ``--write-mode``/``--write_mode`` (job_arguments.py:503, :645;
    job_builders.py:356; parameter_builder.py:118) are never read by the
    generated script. The deployed script's write semantics froze at the last
    codegen, and a mode edited afterwards through the unvalidated
    ``edit_pipeline`` path is not what ran. The verdict therefore names the mode
    it graded (``write_mode`` / ``write_mode_persisted``) so a mismatch between
    the record and the deployed script is diagnosable rather than invisible.

    **``source_type`` is deliberately not read.** Task 6.1 states the matrix key
    as a 3-tuple, but the allow-list is keyed on 2 and that is sufficient: both
    source types emit the *same* write statements for a given (target, mode) —
    the four S3_UPLOAD templates inline the write and glue_job_jdbc_source.py.j2
    ``{% include %}``s the identical ``_partials/_write_*.py.j2``
    (:309, :313, :317, :321). The one source-dependent modifier (a JDBC
    incremental source making the S3 Tables ``append`` commit-then-fail window
    concrete) bears on ``("s3_tables", "append")``, which is already outside the
    allow-list. A 3-tuple key would produce an identical verdict for every
    input, so it is omitted rather than read and ignored.

    **Case handling is asymmetric, and mirrors the renderer on each side.**
    The target type is lower-cased because ``code_assembly`` lower-cases it
    before its own type_map lookup (code_assembly.py:205). ``write_mode`` is
    compared **verbatim** because the templates test it with exact-match Jinja2
    equality (``{% if write_mode == 'overwrite' %}``,
    _write_jdbc.py.j2:27 and siblings) — lower-casing ``"Overwrite"`` here would
    grade it as a safe clobber while the deployed JDBC script actually took the
    ``else`` (append) branch. That is precisely the unsafe direction.

    **Fail-closed on absent or unrecognised values,** for a positive reason
    rather than mere caution: an absent ``write_mode`` renders as ``append``
    for every target type (code_assembly.py:253 sets the render default), and
    ``append`` duplicates on all four targets. So "absent → unsafe" is what the
    generated job actually does. A missing or unrecognised ``target_config.type``
    cannot be graded at all, so it is unsafe by the same allow-list rule.
    ``_load_pipeline_record`` performs no normalisation — it is a bare
    ``get_item`` (main.py:223-241) — so all normalisation happens here.

    Returns::

        {
            "safe": bool,                    # False ⇒ do not re-run
            "target_type": str,              # normalised, "" if unrecognised
            "target_type_persisted": str,    # spelling on the pipeline record
            "write_mode": str,               # the value the matrix was keyed on
            "write_mode_persisted": str,     # spelling on the pipeline record
            "reason": str,                   # honest, never claims detection
        }

    On a SAFE verdict the reason is that combination's own exemption reason from
    ``_SAFE_RERUN_REASONS`` (R15.5) — not a generic "in the safe set" — so the
    outcome record for a permitted re-run carries why no guard was needed for
    that target and mode.
    """
    target_config = pipeline_record.get("target_config")
    if not isinstance(target_config, dict):
        target_config = {}

    raw_target = target_config.get("type", target_config.get("target_type"))
    raw_mode = target_config.get("write_mode")

    target = _TARGET_TYPE_NORMALISATION.get(raw_target.strip().lower(), "") if isinstance(raw_target, str) else ""
    # Verbatim — see the case-handling note above.
    mode = raw_mode if isinstance(raw_mode, str) else ""

    verdict: dict[str, Any] = {
        "safe": (target, mode) in _SAFE_TO_RERUN,
        "target_type": target,
        "target_type_persisted": "" if raw_target is None else str(raw_target),
        "write_mode": mode,
        "write_mode_persisted": "" if raw_mode is None else str(raw_mode),
    }

    if verdict["safe"]:
        # R15.5: the exemption's OWN reason, not a generic "in the safe set" —
        # the point of the requirement is that the exemption reads as
        # established. See _SAFE_RERUN_REASONS for why each one is stated with
        # what it is not.
        verdict["reason"] = _SAFE_RERUN_REASONS.get(
            (target, mode),
            # Unreachable while _SAFE_RERUN_REASONS covers _SAFE_TO_RERUN, which
            # a test pins. If an exemption is ever added without a reason, say so
            # in the record rather than emitting a bare "permitted" that reads as
            # audited when it is not.
            f"Re-run permitted: a {target} target with write_mode {mode!r} is in "
            f"the re-run safe set, but NO exemption reason is recorded for it. "
            f"R15.5 requires one — treat this verdict as unaudited.",
        )
    elif not target:
        verdict["reason"] = (
            f"Re-run declined: the pipeline record's target type "
            f"{verdict['target_type_persisted']!r} is not a recognised target, so "
            f"re-run safety cannot be established. {_NO_SIGNAL_NOTE}"
        )
    elif not mode:
        verdict["reason"] = (
            f"Re-run declined: no usable write_mode is recorded on the {target} "
            f"target (persisted value "
            f"{verdict['write_mode_persisted']!r}), so the generated job renders "
            f"the 'append' default, which duplicates rows on re-run. "
            f"{_NO_SIGNAL_NOTE}"
        )
    else:
        verdict["reason"] = (
            f"Re-run declined: a {target} target with write_mode {mode!r} "
            f"duplicates or corrupts data on re-run, and the failed run may have "
            f"written rows. {_NO_SIGNAL_NOTE}"
        )
    return verdict


# Remediations that have ALREADY mutated AWS state by the time the
# Partial_Write_Guard runs. ``remediate_oom`` calls ``update_job`` before the
# re-run decision, so declining the re-run does not undo the worker-type
# upgrade — the notification has to say the Glue job WAS changed (R15.3).
# ``rerun_pipeline`` is not in this set: for schema-mismatch / type-cast
# failures the remediation IS the re-run (R9.6), so a decline leaves nothing
# behind.
_PRE_GATE_MUTATING_ACTIONS: frozenset[str] = frozenset({"upgrade_worker_type"})


def _notify_rerun_declined(
    *,
    pipeline_id: str,
    run_id: str,
    rerun_safety: dict[str, Any],
    declined_action: str,
    applied_fix: str,
) -> None:
    """Send the user-facing notification for a declined re-run (P30 R15.3).

    R15.3 requires the decision to be visible in BOTH the outcome record and
    the notification. The record half is ``build_outcome_record``'s
    ``skip_reason``/``skip_detail``/``rerun_safety``; this is the notification
    half, and it is the agent's first notification path — the existing
    ``shared/notification_service`` convenience functions were only wired to
    the approval Lambdas.

    Persist-then-notify, matching the approval handler
    (``send_approval_notifications``, called after the approval record is
    written): the durable record exists before anything is sent, and a
    notification failure is logged and swallowed rather than propagated. It
    must never un-decline the guard, flip ``rerun_status``, or fail the
    handler — the decision has already been recorded by the time this runs.
    The import is deliberately at call time, like every other shared-module
    import in this file, so an unavailable notification module degrades to a
    logged error instead of breaking container startup.
    """
    try:
        from notification_service import send_self_healing_declined_alert

        result = send_self_healing_declined_alert(
            pipeline_id=pipeline_id,
            run_id=run_id,
            rerun_safety=rerun_safety,
            declined_action=declined_action,
            applied_fix=applied_fix,
        )
        if result.get("success"):
            logger.info(
                "Partial_Write_Guard: decline notification sent for run %s (%s)",
                run_id,
                result.get("notification_id", ""),
            )
        else:
            # Not fatal, but the user may only have the outcome record.
            logger.error(
                "Partial_Write_Guard: decline notification NOT delivered for run %s — "
                "the reason is still on the outcome record (skip_reason=partial_write_guard). "
                "Channel results: %s",
                run_id,
                result.get("delivery_results", {}),
            )
    except Exception as exc:
        logger.error(
            "Partial_Write_Guard: decline notification failed for run %s: %s — "
            "the reason is still on the outcome record (skip_reason=partial_write_guard)",
            run_id,
            exc,
        )


def _notify_heal_applied(
    *,
    pipeline_id: str,
    run_id: str,
    error_classification: str,
    remediation_action: str,
    remediation_applied: str,
    rerun_started: bool,
    rerun_id: str,
    rerun_start_error: str,
) -> None:
    """Send the user-facing notification for a heal that ran (P30 R14.3).

    R14.3's second clause: a heal that ran must be visible in a notification
    naming what was remediated, not only on the failed run's detail page. The
    first clause (the run-list indicator) is served by ``execution_updater``
    denormalizing this record's ``rerun_status`` onto the run's execution row.

    Sibling of :func:`_notify_rerun_declined`, and mutually exclusive with it:
    the handler sends the declined alert when the Partial_Write_Guard refused
    to act and this one when a remediation was actually applied, so a heal
    never produces two notifications. Nothing is sent when the agent only
    diagnosed and applied no fix (``flag_for_review`` /
    ``no_upgrade_available``) — there is no remediation to name.

    Same posture as the declined notification: persist-then-notify (the
    outcome record is already durable when this runs), call-time import, and
    every failure logged and swallowed. It must never fail the handler or
    change the recorded outcome — by the time it runs, the decision is history.
    """
    try:
        from notification_service import send_self_healing_applied_alert

        result = send_self_healing_applied_alert(
            pipeline_id=pipeline_id,
            run_id=run_id,
            error_classification=error_classification,
            remediation_action=remediation_action,
            remediation_applied=remediation_applied,
            rerun_started=rerun_started,
            rerun_id=rerun_id,
            rerun_start_error=rerun_start_error,
        )
        if result.get("success"):
            logger.info(
                "Self-healing: remediation notification sent for run %s (%s)",
                run_id,
                result.get("notification_id", ""),
            )
        else:
            # Not fatal — the outcome record and the run-list indicator both
            # still carry the remediation.
            logger.error(
                "Self-healing: remediation notification NOT delivered for run %s — "
                "the remediation is still on the outcome record. Channel results: %s",
                run_id,
                result.get("delivery_results", {}),
            )
    except Exception as exc:
        logger.error(
            "Self-healing: remediation notification failed for run %s: %s — "
            "the remediation is still on the outcome record",
            run_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Heal_Guard (P28 R9.4)
# ---------------------------------------------------------------------------

# Canonical trigger marker for heal reruns (requirements glossary; emitted by
# shared/rerun_contract as both `triggered_by` and `trigger`).
_HEAL_TRIGGER = "self_healing"


def was_heal_triggered(payload: dict[str, Any]) -> bool:
    """Heal_Guard lookup — was the failed run itself a self-healing rerun?

    The Glue failure payload assembled by job_completion carries only
    ``{pipeline_id, run_id, job_name, job_run_id, error_message, state}`` —
    the Glue state-change event has no trigger information. The run's trigger
    lives in two places:

    1. ``payload["triggered_by"]`` — fast path, honoured when a caller
       forwards the marker directly.
    2. The run's ``file_validation`` state row — the authoritative path.
       file_validation GENERATES the run_id that every downstream step (and
       therefore the failure payload) carries, and persists the SFN input's
       ``triggered_by`` on that identity row. The heal rerun's SFN input
       ``{original}-heal`` run_id is lineage only — the actual state rows key
       under the fresh file_validation id, which is exactly the id this
       lookup receives.

    Fail-closed: when the state row cannot be read, treat the run as
    heal-triggered and skip. Healing is best-effort for the original run;
    skipping one legitimate heal is cheaper than risking a heal loop — the
    precise failure mode R9.4 exists to prevent.
    """
    if payload.get("triggered_by") == _HEAL_TRIGGER:
        return True

    run_id = payload.get("run_id", "")
    if not run_id or run_id == "unknown":
        # Nothing to look up; the degenerate-payload path is handled (and
        # flagged for review) by the normal flow.
        return False

    try:
        state_table = _dynamodb.Table(
            _require_env("STATE_TABLE", "agentic_data_ingestion_state")
        )
        item = (
            state_table.get_item(Key={"run_id": run_id, "step_name": "file_validation"}).get("Item")
            or {}
        )
        return item.get("triggered_by") == _HEAL_TRIGGER
    except Exception as exc:
        logger.error(
            "Heal_Guard trigger lookup failed for run %s — failing closed (skipping heal): %s",
            run_id,
            exc,
        )
        return True


# ---------------------------------------------------------------------------
# Entrypoint (Task 1.1)
# ---------------------------------------------------------------------------


@app.entrypoint
def handler(payload: dict) -> dict:
    """Self-healing agent entrypoint.

    Payload shape::

        {
            "run_id": "run-2025-...",
            "pipeline_id": "pipe-123",
            "step_name": "glue_etl",
            "error_message": "AnalysisException: cannot resolve ...",
            "job_name": "agentic-data-ingestion-pipe-123-etl",
            "execution_arn": "arn:aws:states:..."
        }
    """
    start_time = time.time()

    run_id: str = payload.get("run_id", "unknown")
    pipeline_id: str = payload.get("pipeline_id", "unknown")
    step_name: str = payload.get("step_name", "unknown")
    error_message: str = payload.get("error_message", "")
    job_name: str = payload.get("job_name", "")

    # Configure logging with run context
    _run_filter.run_id = run_id
    _run_filter.pipeline_id = pipeline_id
    root = logging.getLogger()
    root.addFilter(_run_filter)
    for h in root.handlers:
        h.setFormatter(
            logging.Formatter(f"%(levelname)s [run_id={run_id}] %(name)s %(message)s")
        )

    logger.info(
        "Self-healing agent invoked: pipeline_id=%s step=%s",
        pipeline_id,
        step_name,
    )

    # ── 0. Heal_Guard (P28 R9.4) — before ANY remediation action ─
    if was_heal_triggered(payload):
        logger.info(
            "Heal_Guard: run %s was itself a self-healing rerun — "
            "max one heal per original run; skipping remediation",
            run_id,
        )
        skip_item: dict[str, Any] = {
            "run_id": run_id,
            "step_name": "self_healing",
            "agent_type": "self_healing",
            "status": "skipped",
            "skip_reason": "heal_guard",
            "skip_detail": "Run was itself a self-healing rerun — max one heal per original run.",
            "error_classification": "",
            "remediation_applied": "",
            "original_run_id": run_id,
            "rerun_id": "",
            "rerun_status": "skipped_heal_guard",
            # ISO-8601 — see the result_item timestamp note below.
            "timestamp": datetime.now(UTC).isoformat(),
        }
        # Best-effort visibility record (full outcome records are R9.7,
        # task 8.5): a write failure must not un-skip the guard.
        try:
            state_table = _dynamodb.Table(
                _require_env("STATE_TABLE", "agentic_data_ingestion_state")
            )
            state_table.put_item(Item=skip_item)
        except Exception as exc:
            logger.error("Heal_Guard: failed to write skip record for run %s: %s", run_id, exc)
        return skip_item

    # Set up trace plugin for observability
    from agent_trace_plugin import AgentTracePlugin

    trace_plugin = AgentTracePlugin(
        pipeline_id=pipeline_id,
        run_id=run_id,
        step_name="self_healing",
    )

    # ── 1. Classify the error ───────────────────────────────────
    diagnosis_start = time.time()
    error_class = classify_error(error_message)
    diagnosis_duration_ms = round((time.time() - diagnosis_start) * 1000)

    logger.info(
        "Error classified as %s (diagnosis took %dms)",
        error_class,
        diagnosis_duration_ms,
    )

    # ── 2. Apply remediation ────────────────────────────────────
    remediation_result: dict[str, Any] = {}

    if error_class in ("schema_mismatch", "type_cast_error"):
        # P28 R9.6: code-level failures remediate through the STANDARD
        # pipeline — the rerun's create_code regenerates and test_code
        # re-validates. No side-channel code storage.
        remediation_result = remediate_by_rerun(error_class)
    elif error_class == "oom_error":
        remediation_result = remediate_oom(pipeline_id, job_name)
    elif error_class in ("permission_error", "sql_syntax_error", "unknown"):
        # Cannot self-heal — flag for human review.
        #
        # `sql_syntax_error` belongs HERE and not in the rerun branch above, even
        # though it is a code-level failure like `schema_mismatch`. Template
        # rendering is deterministic: the same pipeline record renders the same
        # SQL, so a rerun re-emits the identical rejected statement. Routing it
        # to `remediate_by_rerun` would burn a Glue execution per attempt and
        # never converge. The rerun branch works for `schema_mismatch` /
        # `type_cast_error` because those regenerate the TRANSFORM from mappings
        # that may since have changed; the source read's SQL has no such input.
        remediation_result = {
            "action": "flag_for_review",
            "description": _MANUAL_REVIEW_DESCRIPTIONS.get(
                error_class,
                f"Error type '{error_class}' requires manual intervention.",
            ),
        }

    remediation_description = remediation_result.get("description", "")
    remediation_action = remediation_result.get("action", "")

    # ── 3. Re-trigger pipeline (only when a fix was actually applied) ──
    healing_id = f"heal-{uuid.uuid4().hex[:12]}"
    rerun_id = ""
    rerun_execution_arn = ""
    rerun_started = False
    rerun_status = "skipped"
    rerun_start_error = ""
    rerun_safety: dict[str, Any] | None = None
    declined_action = ""
    declined_applied_fix = ""

    # Honest gating (R9.7): only actions that constitute a corrected input
    # warrant a rerun. flag_for_review and no_upgrade_available (already at
    # max worker tier — "Manual review required") apply no fix, so rerunning
    # with the same input would just fail the same way.
    if remediation_action in ("rerun_pipeline", "upgrade_worker_type"):
        # R9.5 WorkerType persistence: carry remediation job-config changes
        # (WorkerType upgrade) in the rerun input. file_validation persists
        # them on the rerun's identity row and create_glue_pipeline merges
        # them OVER its regenerated config, so the rerun cannot clobber the
        # fix back. remediate_oom's direct update_job (above) covers the
        # window until the rerun's create_glue_pipeline rewrites the full
        # job definition with these overrides applied.
        job_config_overrides: dict[str, Any] | None = None
        if remediation_action == "upgrade_worker_type":
            job_config_overrides = {"WorkerType": remediation_result["new_worker_type"]}

        # P28 R9.7 honest outcome: "fix applied" (rerun_started=True) ONLY
        # when start_execution actually succeeded with the corrected input.
        # A failed start is recorded with the error — never as success.
        try:
            # ── Partial_Write_Guard (P30 R15.2) ──────────────────
            # The pipeline record is read HERE, ahead of the re-run decision,
            # rather than inside retrigger_pipeline: the guard needs the target
            # type and write mode to decide, and retrigger_pipeline's read was
            # the only thing that had them — after the fact. The read stays
            # inside this try/except so a missing or unreadable record keeps
            # its pre-guard meaning (no re-run, ``start_failed`` with the
            # error), since that record IS the re-run input.
            #
            # This ADDS a condition (R15.4). The Heal_Guard above is untouched:
            # a heal-triggered run never reaches here at all.
            pipeline_record = _load_pipeline_record(pipeline_id)
            rerun_safety = evaluate_rerun_safety(pipeline_record)

            if not rerun_safety["safe"]:
                # Classify for human review instead of re-running (R15.2).
                # rerun_started stays False and rerun_id stays "" — nothing
                # was started, and the record must not imply otherwise.
                declined_action = remediation_action
                # R15.3: captured BEFORE the reason is appended below, so the
                # notification can name the fix that was already applied
                # without repeating the decline reason. Empty unless the
                # declined remediation had already mutated AWS state.
                mutated = declined_action in _PRE_GATE_MUTATING_ACTIONS
                declined_applied_fix = remediation_description if mutated else ""
                rerun_status = "skipped_partial_write_guard"
                # R15.2: the outcome is a human-review classification, not a
                # remediation that ran.
                remediation_action = "flag_for_review"
                # Any fix already APPLIED (remediate_oom's update_job has
                # already mutated the Glue job by this point) stays in the
                # description — the decline must not hide a real mutation.
                remediation_description = f"{remediation_description} {rerun_safety['reason']}".strip()
                logger.warning(
                    "Partial_Write_Guard: declining %s for run %s — %s",
                    declined_action,
                    run_id,
                    rerun_safety["reason"],
                )
            else:
                # R15.5: log the exemption where operators look, symmetric with
                # the decline's warning above. The reason is also persisted on
                # the outcome record, but an exemption that is only visible by
                # reading DynamoDB reads as forgotten.
                logger.info(
                    "Partial_Write_Guard: permitting %s for run %s — %s",
                    remediation_action,
                    run_id,
                    rerun_safety["reason"],
                )
                trigger_result = retrigger_pipeline(
                    pipeline_id,
                    run_id,
                    remediation_description,
                    job_config_overrides=job_config_overrides,
                    pipeline_record=pipeline_record,
                )
                rerun_execution_arn = trigger_result["execution_arn"]
                rerun_id = trigger_result["rerun_id"]
                rerun_started = True
                # The rerun is in flight. Its own job_completion links the
                # terminal result back onto this record (rerun_result /
                # linked_run_id / rerun_status → success|failed).
                rerun_status = "running"
        except Exception as exc:
            logger.error("Heal rerun start failed for run %s: %s", run_id, exc)
            rerun_start_error = str(exc)
            rerun_status = "start_failed"

    total_duration_ms = round((time.time() - start_time) * 1000)

    # ── 4. Store the honest outcome record (P28 R9.7) ───────────
    state_table = _dynamodb.Table(
        _require_env("STATE_TABLE", "agentic_data_ingestion_state")
    )
    result_item = build_outcome_record(
        run_id=run_id,
        pipeline_id=pipeline_id,
        healing_id=healing_id,
        error_classification=error_class,
        remediation_action=remediation_action,
        remediation_description=remediation_description,
        rerun_id=rerun_id,
        rerun_started=rerun_started,
        rerun_status=rerun_status,
        rerun_execution_arn=rerun_execution_arn,
        rerun_start_error=rerun_start_error,
        diagnosis_duration_ms=diagnosis_duration_ms,
        total_duration_ms=total_duration_ms,
        rerun_safety=rerun_safety,
        declined_action=declined_action,
    )
    state_table.put_item(Item=result_item)

    # ── 4a. Make the outcome visible to the user (P30 R15.3, R14.3) ───
    # Exactly one notification per heal, sent AFTER the outcome record is
    # durable so the record it points at always exists.
    #
    # The branches are mutually exclusive by construction: ``declined_action``
    # is set only on a Partial_Write_Guard decline, which sets
    # ``rerun_status="skipped_partial_write_guard"``. The R14.3 branch fires on
    # the two statuses that mean a remediation was actually applied, and on
    # neither of them has ``remediation_action`` been rewritten — so it is the
    # action the agent took. ``rerun_status="skipped"`` (diagnosed, no fix
    # applies) is deliberately silent: there is no remediation to name.
    if declined_action:
        # R15.3 — the guard refused to act.
        _notify_rerun_declined(
            pipeline_id=pipeline_id,
            run_id=run_id,
            rerun_safety=rerun_safety or {},
            declined_action=declined_action,
            applied_fix=declined_applied_fix,
        )
    elif rerun_status in ("running", "start_failed"):
        # R14.3 — a heal ran. ``running`` means the re-run started and its
        # outcome is not known yet; ``start_failed`` means no re-run exists.
        _notify_heal_applied(
            pipeline_id=pipeline_id,
            run_id=run_id,
            error_classification=error_class,
            remediation_action=remediation_action,
            remediation_applied=remediation_description,
            rerun_started=rerun_started,
            rerun_id=rerun_id,
            rerun_start_error=rerun_start_error,
        )

    # Write audit trail
    from audit_utils import write_agent_audit_entry

    write_agent_audit_entry(
        agent_type="self_healing",
        action=f"remediate_{error_class}",
        resource_id=pipeline_id,
        details={
            "healing_id": healing_id,
            "error_classification": error_class,
            "remediation": remediation_description,
            "remediation_action": remediation_action,
            "rerun_id": rerun_id,
            "rerun_started": rerun_started,
            "rerun_status": rerun_status,
        },
        run_id=run_id,
        pipeline_id=pipeline_id,
    )

    # Flush trace data
    trace_plugin.flush()

    logger.info(
        "Self-healing complete: classification=%s rerun_status=%s total_ms=%d",
        error_class,
        rerun_status,
        total_duration_ms,
    )

    return result_item


if __name__ == "__main__":
    app.run()
