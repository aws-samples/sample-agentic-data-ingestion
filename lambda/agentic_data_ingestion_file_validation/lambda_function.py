# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os

# Add shared directory to path
import sys
from datetime import UTC, datetime
from urllib.parse import unquote_plus

import boto3

sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from artifact_schema import create_output_data
from decimal_utils import convert_floats_to_decimal
from env_utils import require_env

# p30 R9.1: this Lambda already HEADs the source object for its size check, and
# that same response carries the ETag. The fingerprint is derived here — from a
# call that already happens — and persisted on the run's identity row so
# job_completion can write the ledger row once the run has actually succeeded.
from processed_file_ledger import content_fingerprint

# p32 2.18: FileValidation is the only state whose Lambda receives the whole
# execution state, so it is the only place `retry_from_step` is readable
# without a Payload path — and `$.file_validation_result` is the anchor every
# downstream Payload already dereferences. Echoing the marker through this
# step's result is what lets later states read it with no new ASL state and no
# unresolvable path on a fresh run.
from retry_utils import RESUME_MARKER_KEY, resume_marker
from step_errors import StepFailure, step_handler

try:
    from metrics_facade import PipelineMetrics
except ImportError:
    PipelineMetrics = None  # type: ignore[misc,assignment]  # optional dep; guarded by `is not None` at use sites

# Initialize logger
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# Environment variables
DYNAMODB_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
PIPELINES_TABLE = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
MAX_FILE_SIZE_BYTES = int(os.environ.get("MAX_FILE_SIZE_BYTES", str(5 * 1024 * 1024 * 1024 * 1024)))  # 5TB (S3 limit)

# Initialize AWS clients
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE)
pipelines_table = dynamodb.Table(PIPELINES_TABLE)

# Format detection map — maps file extensions to canonical format names
FORMAT_MAP = {
    "csv": "csv",
    "tsv": "csv",
    # p64 R3.1. Collapses onto the `csv` Canonical_Format exactly as `tsv` does,
    # for the same reason: the delimiter is not part of the Canonical_Format.
    # `FORMAT_DELIMITERS` (`shared/format_delimiters.py`) is keyed on the
    # EXTENSION and supplies the pipe, which is why this needs no new value here
    # and no new extractor, converter or template branch.
    #
    # R3.6 accepts the residual this buys: `.txt` is a weak signal, so a `.txt`
    # file that is not pipe-delimited parses as one column. That is the tradeoff
    # `tsv` already carries, not a new class of defect — content sniffing is out
    # of scope (R11.4).
    "txt": "csv",
    "json": "json",
    "jsonl": "json",
    "ndjson": "json",
    "parquet": "parquet",
    "pq": "parquet",
    "avro": "avro",
    "orc": "orc",
    # p55 R6.1. The Canonical_Format is `"excel"`, the format FAMILY, not the
    # extension `"xlsx"` — the same reason `jsonl`/`ndjson`/`json` all collapse
    # to `json`. A future `.xls` or `.xlsm` key therefore needs no second value.
    "xlsx": "excel",
}

#: Canonical_Formats a Glue job reads DIRECTLY FROM THE SOURCE OBJECT, with no
#: prior conversion of that object (p55 R13.1, R13.2, R13.5). Its complement
#: within `FORMAT_MAP`'s values is the Conversion_Required_Set — `{"excel"}`.
#:
#: `csv` and `json` are members: Spark reads both. They were ALWAYS native under
#: this name, and their absence before p55 is why the old three-member set
#: `{"parquet", "avro", "orc"}` did not mean what its name said — it meant
#: something narrower and unnamed, closest to "columnar or container formats
#: needing no text parsing". A consumer reading that set as the conversion gate
#: would have concluded a CSV needs converting, which is the concrete hazard
#: p55 R13 exists to remove; the meaning is fixed here, before the gate's
#: consumer is wired in `schema_extract`.
#:
#: Deliberately a HAND-WRITTEN set literal, never `set(FORMAT_MAP.values()) -
#: {"excel"}` (p55 do-NOT 8). Deriving it would make guard 3 of
#: `tests/contracts/test_format_vocabulary_cross_lambda.py` a tautology and
#: would break that guard's `isinstance(node, ast.Set)` assertion outright. Two
#: literals that must agree, checked by a contract test, is the shape this
#: repository already chose for `_SOURCE_FORMAT_BY_EXTENSION`.
NATIVE_FORMATS = {"csv", "json", "parquet", "avro", "orc"}


def detect_format(key: str) -> str:
    """Detect file format from S3 key extension, handling compressed variants.

    Supports .gz, .bz2, .snappy, .zst compression suffixes — strips them
    to find the real data extension underneath.

    Returns canonical format name (csv, json, parquet, avro, orc, excel) or
    'unknown'.
    """
    ext = key.lower().rsplit(".", 1)[-1] if "." in key else ""
    if ext in ("gz", "bz2", "snappy", "zst"):
        parts = key.lower().rsplit(".", 2)
        ext = parts[-2] if len(parts) >= 3 else ext
    return FORMAT_MAP.get(ext, "unknown")


def get_file_extension(key):
    """Extract file extension from S3 key"""
    return key.lower().split(".")[-1] if "." in key else ""


def get_pipeline_key(s3_uri):
    """Generate pipeline key based on S3 folder structure"""
    # Extract folder path (excluding bucket and filename)
    # s3://bucket/pipeline-id/file.csv -> pipeline-id
    # s3://bucket/pipeline-id/subfolder/file.csv -> pipeline-id/subfolder
    parts = s3_uri.replace("s3://", "").split("/")
    # Skip bucket (parts[0]) and filename (parts[-1])
    folder_path = "/".join(parts[1:-1]) if len(parts) > 2 else ""
    return folder_path if folder_path else "default"


def generate_run_id():
    """Generate unique run ID for pipeline execution"""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d-%H-%M-%S")
    import uuid

    short_uuid = str(uuid.uuid4())[:8]
    return f"run-{timestamp}-{short_uuid}"


# Rough average bytes-per-record by canonical format (see detect_format). Used
# to derive a size-based record_count_estimate WITHOUT reading file content —
# the validation Lambda only issues a HEAD for size. Deliberately approximate;
# the field is surfaced on the dashboard as an *estimate*. Binary/columnar
# formats (parquet/avro/orc) and unknown types return 0 (size is not a usable
# row proxy for them).
_AVG_BYTES_PER_RECORD: dict[str, int] = {
    "csv": 100,
    "tsv": 100,
    "json": 200,
    "jsonl": 200,
}


def estimate_record_count(file_size_bytes, file_type) -> int:
    """Best-effort record-count estimate from file size and format.

    Returns 0 when size is unavailable or the format is not size-estimable
    (e.g. parquet/avro/orc/unknown).
    """
    try:
        size = int(file_size_bytes or 0)
    except (TypeError, ValueError):
        return 0
    if size <= 0:
        return 0
    avg = _AVG_BYTES_PER_RECORD.get((file_type or "").lower())
    if not avg:
        return 0
    return max(0, size // avg)


def store_metadata_in_dynamodb(
    original_s3_uri,
    json_s3_uri,
    file_type,
    status,
    execution_arn=None,
    error_message=None,
    run_id=None,
    file_name=None,
    file_size_bytes=None,
    record_count_estimate=None,
    pipeline_id_override: str | None = None,
    triggered_by: str | None = None,
    job_config_overrides: dict | None = None,
    original_run_id: str | None = None,
    content_fingerprint_value: str | None = None,
):
    """Store file processing metadata in DynamoDB using new run_id + sequence structure.

    When ``pipeline_id_override`` is provided (post-P17 trigger Lambda sets
    ``event["pipeline_id"]`` explicitly per Requirement 12.4), both
    ``pipeline_id`` and ``original_pipeline_id`` on the state row are set to
    that value. When ``None``, falls back to deriving ``original_pipeline_id``
    from the S3 key prefix via ``get_pipeline_key()`` for backward
    compatibility with in-flight executions that started before the deploy.

    ``triggered_by`` (P28 R9.4 Heal_Guard): persists the SFN input's trigger
    marker on the run's identity row. file_validation GENERATES the run_id
    every downstream step (and the Glue failure payload) carries, so this row
    is the one place the self-healing agent can later resolve "was this run a
    heal rerun?" from just a run_id. Absent for legacy events (attribute is
    only written when the trigger input carried a value).

    ``job_config_overrides`` (P28 R9.5 WorkerType persistence): persists the
    SFN input's remediation config (e.g. ``{"WorkerType": "G.2X"}`` from an
    OOM heal) on the same identity row. The rerun contract only emits the key
    when overrides exist, and the SFN CreateGluePipeline payload cannot carry
    an optional key (an absent-key JSONPath is a States.Runtime failure), so
    the state row — keyed by the FRESH run_id this handler generates — is the
    delivery path: create_glue_pipeline reads it back by run_id and merges the
    overrides over its generated job config instead of clobbering the fix.
    Absent for normal runs (attribute only written when overrides exist).

    ``original_run_id`` (P28 R9.7 rerun-result linking): persists the SFN
    input's link back to the run being re-run on the same identity row. When
    the heal rerun completes, job_completion resolves this row by the FRESH
    run_id (``triggered_by="self_healing"`` + ``original_run_id``) and links
    the rerun result onto the ORIGINAL run's ``self_healing`` healing record.
    Absent for normal runs (attribute only written when the trigger input
    carried a value).

    ``content_fingerprint_value`` (p30 R9.1 Processed_File_Ledger): persists the
    size+ETag fingerprint of the object this run is about to process, taken from
    the HEAD this handler already issues. It is carried on the identity row for
    the same reason ``original_run_id`` is — the row is keyed by the run_id this
    handler generates, and ``job_completion`` resolves it by that run_id when the
    Glue job reports SUCCEEDED, which is the only moment a "processed" claim is
    true. Recording the fingerprint from the HEAD rather than from the trigger's
    event is also the more accurate choice: if the object were replaced between
    the upload notification and this validation, the fingerprint stored is the
    one whose bytes actually reached the target. Absent for JDBC runs and for
    unsupported-format files (neither has, or needs, a fingerprint).
    """
    try:
        timestamp = datetime.now(UTC).isoformat()
        if pipeline_id_override:
            original_pipeline_id = pipeline_id_override
        else:
            # Back-compat: derive the pipeline id from the S3 key prefix. The
            # trigger Lambda passes pipeline_id explicitly, so this arm is only
            # reached by an execution started before that became the contract.
            # Safe to delete once no such execution can still be in flight.
            original_pipeline_id = get_pipeline_key(original_s3_uri)

        # Generate run_id if not provided (for new pipeline runs)
        if not run_id:
            run_id = generate_run_id()

        # Create base item — PK=run_id, SK=step_name
        pipeline_id = original_pipeline_id  # For PipelineIndex GSI
        item = {
            "run_id": run_id,  # Partition key
            "step_sequence": 1,  # Step number in pipeline
            "step_name": "file_validation",
            "status": status,
            "timestamp": timestamp,
            "original_s3_uri": original_s3_uri,
            "file_type": file_type,
            "pipeline_id": pipeline_id,  # For PipelineIndex GSI
            "original_pipeline_id": original_pipeline_id,  # Groups all runs for this pipeline
        }

        # Add execution ID if available
        if execution_arn:
            item["execution_id"] = execution_arn

        # Add optional fields
        if json_s3_uri:
            item["json_s3_uri"] = json_s3_uri
        if error_message:
            item["error_message"] = error_message
        # P28 R9.4 Heal_Guard: record how the run was triggered so the
        # self-healing agent can refuse to heal a run that was itself a heal.
        if triggered_by:
            item["triggered_by"] = triggered_by
        # P28 R9.5 WorkerType persistence: carry heal-remediation job config
        # to create_glue_pipeline via the run's identity row (float→Decimal
        # for the DynamoDB write; the reader normalizes back to native).
        if job_config_overrides:
            item["job_config_overrides"] = convert_floats_to_decimal(dict(job_config_overrides))
        # P28 R9.7 rerun-result linking: the heal rerun's identity row carries
        # the link back to the run being healed so job_completion can update
        # the original run's healing record with the rerun outcome.
        if original_run_id:
            item["original_run_id"] = original_run_id
        # p30 R9.1 Processed_File_Ledger: the content fingerprint job_completion
        # reads back on SUCCEEDED to write the ledger row. Only present for
        # S3 objects whose HEAD produced both a size and an ETag.
        if content_fingerprint_value:
            item["content_fingerprint"] = content_fingerprint_value

        # Build standardized output_data Map
        artifact_status = "success" if status == "success" else "failed"
        summary = {
            "file_name": file_name or "",
            "file_size_bytes": file_size_bytes or 0,
            "record_count_estimate": record_count_estimate or 0,
            "file_format": file_type or "",
        }
        s3_uris = {
            "source_file": original_s3_uri,
        }
        if json_s3_uri:
            s3_uris["json_file"] = json_s3_uri
        item["output_data"] = create_output_data(
            artifact_type="file_validation",
            primary_id=run_id,
            status=artifact_status,
            summary=summary,
            s3_uris=s3_uris,
        )

        table.put_item(Item=item)
        logger.info(
            f"Metadata stored in DynamoDB - run_id: {run_id}, pipeline_id: {pipeline_id}, execution: {execution_arn}"
        )

        return run_id, original_pipeline_id

    except Exception as e:
        logger.error(f"Error storing metadata in DynamoDB: {e}")
        raise


def list_s3_files(bucket, prefix):
    """List all files in an S3 prefix with pagination support.

    Uses s3_client.list_objects_v2() with ContinuationToken to handle
    prefixes with more than 1000 objects. Filters out directory markers
    (keys ending with '/').

    Returns a list of file objects, each with 'Key' and 'Size'.
    """
    files = []
    params = {"Bucket": bucket, "Prefix": prefix}

    while True:
        response = s3_client.list_objects_v2(**params)
        for obj in response.get("Contents", []):
            # Skip directory markers
            if not obj["Key"].endswith("/"):
                files.append({"Key": obj["Key"], "Size": obj.get("Size", 0)})

        if response.get("IsTruncated"):
            params["ContinuationToken"] = response["NextContinuationToken"]
        else:
            break

    return files


def handle_prefix_trigger(event, context):
    """Handle prefix-based multi-file trigger mode.

    Lists all files under the given S3 prefix, validates the first file's
    format, and returns aggregated metadata for the entire prefix.
    """
    source_bucket = event["source_bucket"]
    source_prefix = event["source_prefix"]
    # Normalize to directory form (trailing slash). The rerun contract
    # (shared/rerun_contract.py) defaults source_prefix to the bare pipeline
    # id, but every downstream prefix consumer keys on the trailing slash:
    # schema_extract's prefix-mode detection is `source_s3_uri.endswith("/")`
    # and would otherwise treat the prefix as an object key (NoSuchKey — a
    # heal rerun died exactly this way in task 17.2 live verification), and
    # get_pipeline_key's legacy derivation mis-parses a slashless URI. The
    # slash also makes the S3 listing stricter (`pid/` cannot match
    # `pid-other/...`).
    if not source_prefix.endswith("/"):
        source_prefix = f"{source_prefix}/"

    logger.info(f"Prefix trigger: s3://{source_bucket}/{source_prefix}")

    files = list_s3_files(source_bucket, source_prefix)
    if not files:
        raise ValueError(f"No files found in s3://{source_bucket}/{source_prefix}")

    # Validate first file format as representative sample
    first_file = files[0]
    source_format = detect_format(first_file["Key"])

    total_size = sum(f.get("Size", 0) for f in files)
    source_s3_uri = f"s3://{source_bucket}/{source_prefix}"

    # Generate run tracking
    run_id = generate_run_id()
    # Prefer explicit pipeline_id from the trigger Lambda (Requirement 12.4);
    # fall back to S3-key-prefix derivation for in-flight legacy executions.
    explicit_pipeline_id = event.get("pipeline_id")
    # The `or` arm is back-compat for executions started before pipeline_id
    # became an explicit input. Safe to delete once none can still be in flight.
    original_pipeline_id = explicit_pipeline_id or get_pipeline_key(source_s3_uri)

    # Store metadata for tracking. triggered_by (P28 R9.4): heal reruns run in
    # prefix mode, and this handler GENERATES a fresh run_id (the SFN input's
    # run_id is lineage only) — persisting the trigger marker on the identity
    # row is what lets the Heal_Guard resolve the marker from the fresh id.
    store_metadata_in_dynamodb(
        original_s3_uri=source_s3_uri,
        json_s3_uri=None,
        file_type=source_format,
        status="success",
        execution_arn=event.get("execution_arn"),
        run_id=run_id,
        file_name=source_prefix,
        file_size_bytes=total_size,
        record_count_estimate=estimate_record_count(total_size, source_format),
        pipeline_id_override=explicit_pipeline_id,
        triggered_by=event.get("triggered_by") or event.get("trigger"),
        job_config_overrides=event.get("job_config_overrides"),
        original_run_id=event.get("original_run_id"),
    )

    return {
        "statusCode": 200,
        "source_s3_uri": source_s3_uri,
        "source_format": source_format,
        "file_size_bytes": total_size,
        "file_count": len(files),
        "trigger_mode": "prefix",
        "run_id": run_id,
        "original_pipeline_id": original_pipeline_id,
        # p32 2.18 — always a string (`""` when this is not a retry) so every
        # downstream Payload's `$.file_validation_result.retry_from_step` path
        # resolves. An absent path there is a States.ParameterPathFailure that
        # no Retry or Catch can intercept.
        RESUME_MARKER_KEY: resume_marker(event),
    }


def handle_jdbc_source_validation(event, context):
    """JDBC source validation entry point — writes a synthetic file_validation
    state row for JDBC-sourced pipelines (no S3 file, no HEAD, no format
    detection).

    The pipeline_trigger Lambda sets ``source_type="JDBC"`` and ``pipeline_id``
    on the inbound event. We fetch ``source_config`` from ``agentic_data_ingestion_pipelines``,
    construct a synthetic ``original_s3_uri`` of the form
    ``jdbc://{engine}/{schema}/{table}`` for traceability, and write a state
    row whose ``pipeline_id`` AND ``original_pipeline_id`` are BOTH the
    explicit ``event["pipeline_id"]`` (JDBC sources have no schema-evolution
    clone semantics, so there is no separate "original" pipeline).

    Validates: Requirements 13.1, 13.2.
    """
    import time as _time

    _metrics_start = _time.time()
    logger.append_keys(step_name="FileValidation")

    pipeline_id = event.get("pipeline_id")
    if not pipeline_id:
        logger.error("JDBC source validation missing required 'pipeline_id' on event")
        # P28 R1.1: raise so the Step Functions task fails and Retry/Catch fire
        # (an error payload return would be treated as task success).
        raise StepFailure("file_validation", "JDBC source validation requires 'pipeline_id' on the event")

    run_id = generate_run_id()
    logger.append_keys(run_id=run_id, pipeline_id=pipeline_id)
    execution_arn = event.get("execution_arn")

    try:
        # Fetch source_config from agentic_data_ingestion_pipelines (top-level field per
        # Requirement 6.2)
        pipeline_resp = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})
        pipeline_item = pipeline_resp.get("Item")
        if not pipeline_item:
            raise ValueError(f"Pipeline {pipeline_id} not found in {PIPELINES_TABLE}")

        source_config = pipeline_item.get("source_config")
        if not isinstance(source_config, dict):
            raise ValueError(f"Pipeline {pipeline_id} has no source_config — cannot validate JDBC source")

        engine = source_config.get("engine", "unknown")
        jdbc_schema = source_config.get("jdbc_schema", "unknown")
        jdbc_table = source_config.get("jdbc_table", "unknown")

        # Synthetic URI for traceability and consistent display in lineage views
        synthetic_uri = f"jdbc://{engine}/{jdbc_schema}/{jdbc_table}"
        timestamp = datetime.now(UTC).isoformat()

        item = {
            "run_id": run_id,
            "step_sequence": 1,
            "step_name": "file_validation",
            "status": "success",
            "timestamp": timestamp,
            "original_s3_uri": synthetic_uri,
            "file_type": "jdbc",
            "pipeline_id": pipeline_id,
            "original_pipeline_id": pipeline_id,  # JDBC has no clone semantics
            "output_data": create_output_data(
                artifact_type="file_validation",
                primary_id=run_id,
                status="success",
                summary={
                    "file_format": "jdbc",
                    "engine": engine,
                    "table": f"{jdbc_schema}.{jdbc_table}",
                    "file_name": f"{jdbc_schema}.{jdbc_table}",
                    "file_size_bytes": 0,
                    # No `record_count_estimate`: a JDBC source has no file to
                    # estimate from, so the count is unknown, not zero. Writing
                    # 0 made the Metrics Dashboard's Records Accepted tile read a
                    # measured "0" for windows full of JDBC runs that loaded rows;
                    # the reader treats an absent key as "no estimate" and reports
                    # Not established (p39 R4.9).
                },
                s3_uris={},
            ),
        }
        if execution_arn:
            item["execution_id"] = execution_arn
        # P28 R9.4 Heal_Guard: persist the trigger marker on the run's
        # identity row (see store_metadata_in_dynamodb docstring).
        triggered_by = event.get("triggered_by") or event.get("trigger")
        if triggered_by:
            item["triggered_by"] = triggered_by
        # P28 R9.5 WorkerType persistence: heal-remediation job config rides
        # the identity row to create_glue_pipeline (see
        # store_metadata_in_dynamodb docstring).
        job_config_overrides = event.get("job_config_overrides")
        if job_config_overrides:
            item["job_config_overrides"] = convert_floats_to_decimal(dict(job_config_overrides))
        # P28 R9.7 rerun-result linking: link back to the run being healed
        # (see store_metadata_in_dynamodb docstring).
        original_run_id = event.get("original_run_id")
        if original_run_id:
            item["original_run_id"] = original_run_id

        table.put_item(Item=item)
        logger.info(
            f"JDBC file_validation row written - run_id: {run_id}, pipeline_id: {pipeline_id}, "
            f"engine: {engine}, table: {jdbc_schema}.{jdbc_table}"
        )

        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="FileValidation",
                    pipeline_id=pipeline_id,
                    status="completed",
                    duration_ms=_duration_ms,
                )
            except Exception:
                pass

        return {
            "statusCode": 200,
            "run_id": run_id,
            "original_pipeline_id": pipeline_id,
            "source_format": "jdbc",
            "file_size_bytes": 0,
            "file_count": 1,
            # p32 2.18 — see the prefix-mode return; always a string.
            RESUME_MARKER_KEY: resume_marker(event),
            "body": json.dumps(
                {
                    "message": "JDBC source validation completed successfully",
                    "status": "success",
                    "run_id": run_id,
                    "original_pipeline_id": pipeline_id,
                }
            ),
        }

    except Exception as e:
        logger.error(f"Error in JDBC source validation: {e!s}")
        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="FileValidation",
                    pipeline_id=pipeline_id,
                    status="failed",
                    duration_ms=_duration_ms,
                )
            except Exception:
                pass
        # P28 R1.1: raise so the Step Functions task fails and Retry/Catch fire.
        raise StepFailure(
            "file_validation",
            f"JDBC source validation failed: {e!s}",
            details={"pipeline_id": pipeline_id, "run_id": run_id},
        ) from e


@step_handler("file_validation")
def lambda_handler(event, context):
    """
    Validate an uploaded file and record metadata. No file content is read —
    only a HEAD request for size, plus extension-based format detection.
    Glue reads the original file natively using the detected source_format.
    """
    # JDBC-sourced pipelines bypass all S3-specific validation. The
    # pipeline_trigger Lambda sets source_type="JDBC" on the inbound event.
    if event.get("source_type") == "JDBC":
        return handle_jdbc_source_validation(event, context)

    run_id = None
    original_pipeline_id = None
    source_format = "unknown"
    # None until a record is actually validated. The success return below reads
    # this directly, so any path that reaches the return without setting it is
    # a bug — see the post-loop guard for why that is raised as a named failure
    # rather than left to surface as an UnboundLocalError.
    file_size_bytes: int | None = None

    import time as _time

    _metrics_start = _time.time()

    logger.append_keys(step_name="FileValidation")

    try:
        logger.info(f"Processing event: {json.dumps(event)}")

        # Read explicit pipeline_id from the trigger Lambda input once
        # (Requirement 12.4). Forwarded to store_metadata_in_dynamodb on every
        # state-row write below; when None, the helper falls back to S3-key-
        # prefix derivation for backward compatibility with in-flight executions.
        explicit_pipeline_id = event.get("pipeline_id")

        # P28 R9.4 Heal_Guard: trigger marker from the contract-built SFN
        # input ("triggered_by"), with the legacy "trigger" key as fallback.
        event_triggered_by = event.get("triggered_by") or event.get("trigger")

        # P28 R9.5 WorkerType persistence: heal-remediation job config from
        # the contract-built SFN input, persisted on the run's identity row.
        event_job_config_overrides = event.get("job_config_overrides")

        # P28 R9.7 rerun-result linking: the SFN input's link back to the run
        # being re-run, persisted on the run's identity row.
        event_original_run_id = event.get("original_run_id")

        # Route prefix triggers to dedicated handler
        trigger_mode = event.get("trigger_mode", "single_file")
        if trigger_mode == "prefix":
            return handle_prefix_trigger(event, context)

        # Extract execution ARN
        execution_arn = None
        if hasattr(context, "aws_request_id"):
            execution_arn = getattr(context, "execution_arn", None)
        if "execution_arn" in event:
            execution_arn = event["execution_arn"]
        elif "$$.Execution.Name" in str(event):
            execution_arn = event.get("$$.Execution.Name")

        # Handle Step Functions input format
        if "source_bucket" in event and "source_key" in event:
            logger.info("Processing Step Functions input format")
            bucket = event["source_bucket"]
            key = event["source_key"]
            event["Records"] = [{"eventSource": "aws:s3", "s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]

        # Parse S3 event
        for record in event.get("Records", []):
            logger.info(f"Processing record: {json.dumps(record)}")
            if record.get("eventSource") == "aws:s3" or "s3" in record:
                bucket = record["s3"]["bucket"]["name"]
                key = unquote_plus(record["s3"]["object"]["key"])

                logger.info(f"Processing file: s3://{bucket}/{key}")

                filename = key.split("/")[-1]
                source_format = detect_format(key)
                original_s3_uri = f"s3://{bucket}/{key}"

                if source_format == "unknown":
                    extension = get_file_extension(key)
                    logger.info(f"File format not recognized for: {key}")
                    # The unsupported record is still written — it is the audit
                    # trail for the rejection, and the dashboard's file readers
                    # render `status="unsupported"` rows.
                    run_id, original_pipeline_id = store_metadata_in_dynamodb(
                        original_s3_uri=original_s3_uri,
                        json_s3_uri=None,
                        file_type=extension,
                        status="unsupported",
                        execution_arn=execution_arn,
                        error_message=f"File format not recognized for '{key}'",
                        file_name=filename,
                        pipeline_id_override=explicit_pipeline_id,
                        triggered_by=event_triggered_by,
                    )
                    logger.append_keys(run_id=run_id, pipeline_id=original_pipeline_id)
                    # An unsupported extension FAILS the step. It used to fall
                    # through to the success return, which reads
                    # `file_size_bytes` — only ever assigned in the `else`
                    # below — so the honest rejection surfaced as
                    # `UnboundLocalError: cannot access local variable
                    # 'file_size_bytes'` (observed live on a .xlsx upload).
                    #
                    # Returning a success payload marked unsupported was
                    # rejected. SchemaExtract, the next state, dereferences
                    # `$.file_validation_result.run_id`,
                    # `.original_pipeline_id` and `.source_format`
                    # (orchestration-stack.ts:329-334), so a returned payload
                    # keeps the pipeline running on a file nothing downstream
                    # can read, and the only ids it could carry are the
                    # `or "unknown"` placeholders in the return below — which
                    # SchemaExtract would then fail to resolve, recording the
                    # failure against the WRONG step. Preserving the
                    # originating step's error is exactly what the States.ALL
                    # Catch exists to guarantee (orchestration-stack.ts:226-234).
                    #
                    # Raised as a ValueError so the handler's own except block
                    # converts it to a permanent StepFailure, the same route
                    # the sibling size-limit check takes below. Permanent, not
                    # transient: `stepRetry` matches only TransientStepFailure
                    # and Lambda service errors (orchestration-stack.ts:219-224),
                    # and an unrecognised extension cannot succeed on a retry.
                    raise ValueError(
                        f"Unsupported source format '{extension or '(none)'}' for '{key}' — "
                        f"recognised extensions are {', '.join(sorted(FORMAT_MAP))} "
                        f"(optionally .gz/.bz2/.snappy/.zst compressed)"
                    )
                else:
                    # Single path for ALL formats and sizes: HEAD only, no
                    # conversion — still true of THIS step, and the whole of
                    # what this step does.
                    #
                    # p55 R13 corrected the second half of what this comment
                    # used to claim: "Glue reads the original file natively via
                    # source_format" was true of every Canonical_Format then in
                    # FORMAT_MAP and is now FALSE in general. Whether the
                    # original object is readable is decided by NATIVE_FORMATS
                    # (`:90` in this file — `:68` is now the `xlsx` FORMAT_MAP
                    # entry task 4.2 added), the Native_Format_Set — and a
                    # Canonical_Format
                    # outside it (`excel`) requires the source object to be
                    # converted first. That conversion happens at
                    # schema_extract, not here; file_validation still only
                    # HEADs, and it deliberately neither reads nor writes
                    # object bytes on any format.
                    #
                    # This was a code comment, never a ratified fence — `why`
                    # on this line returns only broad snapshot commits with
                    # `records: []` (p55 do-NOT 9). Corrected rather than
                    # relied upon.
                    head_resp = s3_client.head_object(Bucket=bucket, Key=key)
                    file_size_bytes = head_resp["ContentLength"]

                    # p64 R1.1 — the floor, beside the ceiling below. There was
                    # no lower bound at all before this: a 0-byte object passed
                    # step 1, `infer_schema_from_records` then reported success
                    # with zero fields for it (`schema_extract:160-161`), and the
                    # run spent nine steps on a file with nothing in it before
                    # the Glue template's zero-row arm warned, committed and
                    # exited 0 (`glue_job_s3_tables.py.j2:111-124` — an arm R1.6
                    # requires left alone).
                    #
                    # Still HEAD-only. `ContentLength` is the same response the
                    # ceiling already reads, so this costs no extra request and
                    # reads no object bytes: R1.2 forbids a content read here,
                    # and this handler's docstring is the older statement of the
                    # same rule.
                    #
                    # `== 0` rather than `<= 0`: S3 `ContentLength` is
                    # non-negative by API contract and R1.1 names the zero-byte
                    # object only, so widening the comparison would make the
                    # message's "0 bytes" a claim about a value it never
                    # measured.
                    #
                    # `ValueError` for the reason `LGR-60c4985-7` (template /
                    # ratified) records for the two sibling refusals: it never
                    # escapes, because the handler's own `except Exception`
                    # wraps it into `StepFailure("file_validation", …)`, whose
                    # default `error_type` is "permanent"
                    # (`shared/step_errors.py:106`). Permanent is deliberate —
                    # `stepRetry` matches TransientStepFailure and Lambda
                    # service errors only (`orchestration-stack.ts:219-224`),
                    # and an empty object cannot become non-empty on attempt
                    # two. No `unsupported` audit row is written here: that
                    # branch's row exists because the dashboard renders
                    # `status="unsupported"` file rows, and the sibling ceiling
                    # check writes none either.
                    #
                    # The message says EMPTY and says nothing about headers or
                    # data rows, because R1.4 needs three distinguishable
                    # refusals: this one, `schema_extract`'s "carried a header
                    # but no data rows" (p64 1a.4), and the unsupported-format
                    # one above. A headers-only file is NOT zero-byte and is
                    # deliberately not caught here — separating it from an empty
                    # file needs a read, which is why R1.3 puts that refusal at
                    # step 2 instead.
                    if file_size_bytes == 0:
                        raise ValueError(
                            f"File {original_s3_uri} is empty (0 bytes) — there are no rows to ingest, "
                            f"so no schema can be extracted and no Glue run can write anything. "
                            f"Upload a file containing data."
                        )

                    if file_size_bytes > MAX_FILE_SIZE_BYTES:
                        raise ValueError(
                            f"File {key} is {file_size_bytes / (1024 * 1024):.1f}MB, "
                            f"exceeding the {MAX_FILE_SIZE_BYTES / (1024 * 1024):.0f}MB limit"
                        )

                    # p30 R9.1: same HEAD, second use. `.get("ETag")` rather than
                    # `["ETag"]` — a fingerprint is a nice-to-have and must never
                    # be able to fail validation of an otherwise good file.
                    content_fp = content_fingerprint(file_size_bytes, head_resp.get("ETag"))

                    logger.info(f"Validated: format={source_format}, size={file_size_bytes}")

                    run_id, original_pipeline_id = store_metadata_in_dynamodb(
                        original_s3_uri=original_s3_uri,
                        json_s3_uri=None,
                        file_type=source_format,
                        status="success",
                        execution_arn=execution_arn,
                        file_name=filename,
                        file_size_bytes=file_size_bytes,
                        record_count_estimate=estimate_record_count(file_size_bytes, source_format),
                        pipeline_id_override=explicit_pipeline_id,
                        triggered_by=event_triggered_by,
                        job_config_overrides=event_job_config_overrides,
                        original_run_id=event_original_run_id,
                        content_fingerprint_value=content_fp,
                    )
                    logger.append_keys(run_id=run_id, pipeline_id=original_pipeline_id)

        # The second door onto the same unbound-variable failure: an event with
        # neither `Records` nor `source_bucket`/`source_key` never enters the
        # loop, so nothing sets `file_size_bytes` and the success return raised
        # `UnboundLocalError` — a Python-internals message for what is really a
        # malformed input. Named here instead, and still a failure: returning
        # 200 with `run_id="unknown"` would advance the pipeline on a run that
        # validated nothing.
        if file_size_bytes is None:
            raise ValueError(
                "no S3 record to validate — the event carried neither 'Records' nor 'source_bucket'/'source_key'"
            )

        # Publish stage metrics
        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="FileValidation",
                    pipeline_id=original_pipeline_id or run_id or "unknown",
                    status="completed",
                    duration_ms=_duration_ms,
                )
            except Exception:
                pass

        return {
            "statusCode": 200,
            "run_id": run_id or "unknown",
            "original_pipeline_id": original_pipeline_id or "unknown",
            "source_format": source_format,
            "file_size_bytes": file_size_bytes,
            "file_count": 1,
            # p32 2.18 — see the prefix-mode return; always a string.
            RESUME_MARKER_KEY: resume_marker(event),
            "body": json.dumps(
                {
                    "message": "File processing completed successfully",
                    "status": "success",
                    "run_id": run_id or "unknown",
                    "original_pipeline_id": original_pipeline_id or "unknown",
                }
            ),
        }

    except Exception as e:
        logger.error(f"Error in lambda handler: {e!s}")
        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="FileValidation",
                    pipeline_id=original_pipeline_id or run_id or "unknown",
                    status="failed",
                    duration_ms=_duration_ms,
                )
            except Exception:
                pass
        # P28 R1.1: raise so the Step Functions task fails and Retry/Catch fire.
        raise StepFailure(
            "file_validation",
            f"File processing failed: {e!s}",
            details={"run_id": run_id, "pipeline_id": original_pipeline_id},
        ) from e
