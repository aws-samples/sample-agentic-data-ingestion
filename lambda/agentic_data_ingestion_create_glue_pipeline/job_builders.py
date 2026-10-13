# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Glue-job orchestration builders for the create_glue_pipeline entrypoint.

Extracted from ``lambda_function.py`` (P21 Req 4.2/4.4) as the final step of
decomposing the oversized create_glue_pipeline entrypoint so the orchestration
entrypoint stays at or below the documented 800-line module-size guideline
(see ``shared/README.md`` → "Module Size Guideline"). This module owns the
heavy Glue-job assembly + deployment orchestration:

* ``get_target_config_from_pipeline`` — target_config lookup from the
  pipelines table.
* ``create_glue_job`` — assembles the ``glue create_job`` / ``update_job``
  request (IAM role, worker config, per-target arguments, parameter
  validation, checkpoints, metrics) and creates/updates the Glue job.
* ``deploy_glue_pipeline_with_mcp`` — MCP-enhanced deployment with a
  rule-based fallback.
* ``convert_floats_to_decimal`` — re-exported from ``shared/decimal_utils``
  (P28 R19.2 single copy) for ``lambda_function``'s import compatibility.
* ``update_pipeline_metadata_with_glue_job`` — persists Glue-job metadata
  back to the pipelines table.

The moves are behavior-preserving (verbatim function bodies); ``lambda_function.py``
imports these names back so the ``lambda_handler`` entrypoint keeps working
unchanged. Dependency direction is one-way: ``lambda_function`` → ``job_builders``
→ (``workers``, ``job_arguments``, ``artifacts``, ``iam_roles``). This module
MUST NOT import ``lambda_function``.
"""

import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger

if TYPE_CHECKING:
    from mypy_boto3_glue.type_defs import JobUpdateTypeDef

logger = Logger(service="agentic_data_ingestion")

# Add shared directory + this module's own directory to path for imports
# (Layer_Import_Fallback — /opt/python in the Lambda layer, else the local
# paths for local/test execution).
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
sys.path.append(os.path.dirname(__file__))

# Import shared env_utils via the Layer_Import_Fallback (fix path + import the
# canonical resolver — do not redefine it inline; P21 Req 5.7).
# decimal_utils: P28 R19.2 single shared float→Decimal converter (replaces the
# local copy; re-exported for lambda_function's import compatibility).
try:
    from decimal_utils import convert_floats_to_decimal
    from env_utils import get_aws_account_id, require_env
    from identifier_utils import validate_identifier
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from decimal_utils import convert_floats_to_decimal
    from env_utils import get_aws_account_id, require_env
    from identifier_utils import validate_identifier

# Sibling-module helpers (one-way dependency — these never import
# lambda_function or job_builders).
from artifacts import (
    get_test_results_from_pipeline_runs,
)
from iam_roles import (
    create_glue_database,
    create_pipeline_specific_role,
)
from job_arguments import (
    build_glue_catalog_arguments,
    build_jdbc_arguments,
    build_jdbc_source_arguments,
    build_s3_parquet_arguments,
    build_s3_table_bucket_arguments,
    validate_required_parameters,
)
from workers import (
    WorkerRecommendation,
    get_data_characteristics,
    get_default_worker_recommendation,
    get_worker_recommendation_with_agent,
    incorporate_test_recommendations_into_job_config,
)

# Typed DynamoDB item accessor (P22 Req 2) — narrow boto3-stubs' wide
# AttributeValueTypeDef union to a concrete dict at the item read site.
from dynamo_typing import item_dict

# Import parameter builder module
try:
    from parameter_builder import build_glue_job_parameters, get_code_requirements, validate_parameters

    PARAMETER_BUILDER_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import parameter_builder module: {e}")
    PARAMETER_BUILDER_AVAILABLE = False

# Import shared metrics
try:
    from metrics_facade import (
        publish_glue_job_creation_metrics,
    )

    METRICS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import metrics_facade module: {e}")
    METRICS_AVAILABLE = False

# Environment variables (mirror lambda_function.py so behavior is identical).
PIPELINES_TABLE = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
GENERATED_CODE_TABLE = require_env("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
MCP_ENABLED = os.environ.get("MCP_ENABLED", "false").lower() == "true"

# Initialize AWS clients
# Convention: one module-level client per AWS service (see shared/README.md).
dynamodb = boto3.resource("dynamodb")
glue_client = boto3.client("glue")
s3_client = boto3.client("s3")

# DynamoDB tables
pipelines_table = dynamodb.Table(PIPELINES_TABLE)


def _ensure_jdbc_helpers_on_s3() -> str:
    """Upload ``jdbc_helpers.py`` to the dependencies bucket and return its S3 URI.

    The JDBC-source PySpark template imports ``jdbc_helpers`` at job runtime
    (identifier/watermark validation — Req 7.5). Glue only puts a module on
    the job's Python path when it is listed in ``--extra-py-files``, so the
    shared module must live in S3. The module ships in this Lambda's layer at
    ``/opt/python/jdbc_helpers.py``; we copy it to the per-account
    dependencies bucket (same bucket that already hosts the S3 Tables JAR).

    Returns:
        The ``s3://`` URI of the uploaded module.

    Raises:
        FileNotFoundError: If the layer module is missing (would indicate a
            packaging regression). RuntimeError on upload failure — both are
            surfaced to the caller so the pipeline create fails loudly rather
            than producing a job that crashes at import time.
    """
    bucket = f"adi-dependencies-{get_aws_account_id()}-{AWS_REGION}"
    key = "glue-deps/jdbc_helpers.py"
    # Resolve the module from the layer first, then the local shared/ dir
    # (mirrors the Layer_Import_Fallback used for imports above).
    candidates = [
        "/opt/python/jdbc_helpers.py",
        os.path.join(os.path.dirname(__file__), "..", "..", "shared", "jdbc_helpers.py"),
    ]
    source_path = next((p for p in candidates if os.path.exists(p)), None)
    if source_path is None:
        raise FileNotFoundError(
            "jdbc_helpers.py not found in the Lambda layer (/opt/python) or local shared/ — "
            "the JDBC-source Glue job requires it via --extra-py-files."
        )
    try:
        s3_client.upload_file(source_path, bucket, key)
    except Exception as exc:
        raise RuntimeError(f"Failed to upload jdbc_helpers.py to s3://{bucket}/{key}: {exc}") from exc
    logger.info("Uploaded jdbc_helpers.py to s3://%s/%s for JDBC-source Glue job", bucket, key)
    return f"s3://{bucket}/{key}"


def _add_jdbc_source_job_wiring(
    job_config: dict[str, Any],
    pipeline_metadata: dict[str, Any],
    source_config: dict[str, Any],
    pipeline_id: str,
) -> None:
    """Attach the source Glue connection + ship jdbc_helpers for a JDBC source.

    Mutates ``job_config`` in place:

    * ``Connections``: a JDBC-source job must run inside the source VPC, which
      Glue only does when the source connection is listed here. Merges with
      any target connection already set (JDBC-target case) so both the source
      and target connections are attached.
    * ``DefaultArguments["--extra-py-files"]``: makes ``jdbc_helpers`` importable
      at job runtime. Preserves any existing value by comma-appending.

    Source connection name is read from the pipeline record's pre-computed
    ``source_connection_name`` (set by create_pipeline), falling back to the
    glue_connection_name / deterministic ``agentic-data-ingestion-{pipeline_id}-source``.
    """
    source_connection_name = (
        pipeline_metadata.get("source_connection_name")
        or source_config.get("glue_connection_name")
        or f"agentic-data-ingestion-{pipeline_id}-source"
    )

    # Merge the source connection with any connection already attached (the
    # JDBC-target branch sets job_config["Connections"] = {"Connections": [t]}).
    existing = job_config.get("Connections", {}).get("Connections", [])
    merged = list(dict.fromkeys([*existing, source_connection_name]))  # de-dup, preserve order
    job_config["Connections"] = {"Connections": merged}
    logger.info("Attached source Glue connection(s) to JDBC-source job: %s", merged)

    # Ship jdbc_helpers via --extra-py-files (comma-append to preserve any
    # pre-existing value).
    helpers_uri = _ensure_jdbc_helpers_on_s3()
    args = job_config.setdefault("DefaultArguments", {})
    existing_py = args.get("--extra-py-files", "")
    args["--extra-py-files"] = f"{existing_py},{helpers_uri}" if existing_py else helpers_uri


def _attach_jdbc_target_connection(job_config: dict[str, Any], target_config: dict[str, Any]) -> None:
    """Attach the JDBC target's Glue connection to the job (merging, not clobbering).

    Without the target connection on the job, Glue places the job's ENIs
    outside the target's VPC/subnet and the Spark ``write.jdbc(...)`` cannot
    reach the target database — the run fails at the write phase with
    ``org.postgresql.util.PSQLException: The connection attempt failed`` (and
    the equivalent for other engines).

    Two behaviours this fixes:

    * **Key alias** — ``glue_connection`` mode persists the connection name
      under ``glue_connection_name`` (the user-selected existing connection),
      while ``select_rds`` / ``manual`` / ``RDS_DATABASE`` modes use
      ``jdbc_connection_name``. Reading only the latter silently dropped the
      target connection for every glue_connection-mode pipeline.
    * **Merge, don't clobber** — a JDBC-source → JDBC-target pipeline needs
      BOTH the source and target connections attached. Overwriting
      ``Connections`` here would drop whichever connection was attached first.
      We de-dup while preserving order.

    No-op when no target connection name is resolvable (e.g. a target that does
    not route through a Glue connection).
    """
    connection_name = target_config.get("jdbc_connection_name") or target_config.get("glue_connection_name") or ""
    if not connection_name:
        return

    existing = job_config.get("Connections", {}).get("Connections", [])
    merged = list(dict.fromkeys([*existing, connection_name]))  # de-dup, preserve order
    job_config["Connections"] = {"Connections": merged}
    logger.info("Attached JDBC target connection to job: %s", connection_name)


def _normalize_override_values(obj: Any) -> Any:
    """Recursively convert DynamoDB ``Decimal`` values back to native numbers.

    ``job_config_overrides`` is read back from the file_validation state row
    (P28 R9.5 delivery path), so numeric values arrive as ``Decimal``. boto3's
    Glue client rejects ``Decimal`` (and ``float``) for integer shapes like
    ``NumberOfWorkers``/``Timeout``, so integral Decimals become ``int`` and
    fractional ones become ``float``.
    """
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _normalize_override_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_override_values(item) for item in obj]
    return obj


# Identity keys a rerun override must never rewrite: the job name is the
# stable join key between the pipeline record, run rows, and the Glue API
# call being assembled (update_job strips Name anyway; create_job with a
# different Name would fork a second job and orphan the pipeline record).
_OVERRIDE_PROTECTED_KEYS = frozenset({"Name"})


def _merge_job_config_overrides(
    job_config: dict[str, Any],
    job_config_overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge heal-remediation overrides OVER the generated job config (P28 R9.5).

    A heal rerun regenerates the Glue job config from scratch (worker
    recommendation / baseline / defaults), and the closing ``update_job``
    would clobber the self-healing agent's fix (e.g. a WorkerType upgrade for
    OOM) back down. This merge — applied as the LAST config step before
    ``create_job``/``update_job`` — makes the rerun input's overrides win:

    * top-level override keys replace the generated value (``WorkerType``,
      ``NumberOfWorkers``, ``Timeout``, …);
    * when BOTH sides are dicts (e.g. ``DefaultArguments``), the override's
      entries are merged INTO the generated dict (override entries win,
      generated entries survive) — wholesale replacement would silently drop
      the assembled ``--pipeline-id``/target arguments;
    * ``Name`` is protected (identity, never overridable);
    * the ``Tags`` copies of WorkerType/NumberOfWorkers are kept in sync so
      a create-path job carries truthful tags.

    Mutates ``job_config`` in place and returns the dict of overrides that
    were actually applied (normalized, protected keys removed) — ``{}`` when
    nothing was applied. Application is logged with the overridden keys and
    their generated→override values (R9.5 observability).
    """
    if not job_config_overrides:
        return {}

    applied: dict[str, Any] = {}
    override_log: dict[str, dict[str, Any]] = {}
    for key, raw_value in job_config_overrides.items():
        if key in _OVERRIDE_PROTECTED_KEYS:
            logger.warning(f"Ignoring protected job_config_overrides key {key!r} (job identity is not overridable)")
            continue
        value = _normalize_override_values(raw_value)
        generated = job_config.get(key)
        if isinstance(generated, dict) and isinstance(value, dict):
            # Dict-on-dict (DefaultArguments): override entries win, generated
            # entries survive.
            merged = {**generated, **value}
            override_log[key] = {"generated": {k: generated.get(k) for k in value}, "override": value}
            job_config[key] = merged
        else:
            override_log[key] = {"generated": generated, "override": value}
            job_config[key] = value
        applied[key] = value

    if not applied:
        return {}

    # Keep the tag copies truthful for the create path (tags are
    # creation-only in Glue, so update-path jobs keep their original tags).
    tags = job_config.get("Tags")
    if isinstance(tags, dict):
        if "WorkerType" in applied:
            tags["WorkerType"] = applied["WorkerType"]
        if "NumberOfWorkers" in applied:
            tags["NumberOfWorkers"] = str(applied["NumberOfWorkers"])

    logger.info(
        "Applied job_config_overrides over generated config (heal-rerun persistence, P28 R9.5): "
        f"overridden_keys={sorted(applied.keys())}, values={override_log}"
    )
    return applied


def _common_write_arguments(target_config: dict[str, Any]) -> dict[str, str]:
    """Build the write-mode Glue job arguments shared by all target types.

    P28 R12.1: ``--merge_key`` is read back by the rendered job at runtime and
    f-string'd into the ``spark.sql`` MERGE INTO statement, so the value is
    validated against the Identifier_Allowlist BEFORE it enters
    DefaultArguments. An invalid merge_key fails job creation loudly, naming
    the offending identifier (R12.2) — it is never sanitized or defaulted
    away. (The rendered job re-validates the runtime argument at the SQL sink
    itself, guarding manual job starts that bypass this builder.)

    Raises:
        IdentifierValidationError: When ``target_config.merge_key`` fails the
            allowlist.
    """
    return {
        "--write_mode": target_config.get("write_mode", "overwrite"),
        "--merge_key": validate_identifier(
            target_config.get("merge_key", "id"),
            "MERGE statement merge_key (--merge_key Glue job argument)",
        ),
    }


def get_target_config_from_pipeline(pipeline_id: str) -> dict[str, Any]:
    """
    Retrieve target configuration from pipelines table.

    Args:
        pipeline_id: Pipeline identifier

    Returns:
        dict: Target configuration with type and target-specific fields
    """
    try:
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            raise ValueError(f"Pipeline {pipeline_id} not found in pipelines table")

        target_config = item_dict(response["Item"], "target_config")

        if not target_config:
            raise ValueError(f"target_config not found for pipeline {pipeline_id}")

        # Validate required fields
        target_type = target_config.get("type", "")
        if not target_type:
            raise ValueError("target_config.type is required")

        logger.info(f"Retrieved target_config for pipeline {pipeline_id}: type={target_type}")
        return target_config

    except Exception as e:
        logger.error(f"Error retrieving target_config from pipelines table: {e}")
        raise


def create_glue_job(
    pipeline_id: str,
    target_type: str,
    target_schema: dict,
    generated_code_s3_path: str,
    s3_table_bucket: dict | None = None,
    worker_recommendation: WorkerRecommendation | None = None,
    target_config: dict[str, Any] | None = None,
    test_results: dict[str, Any] | None = None,
    run_id: str | None = None,
    source_path: str = "",
    warehouse_location: str = "",
    baseline_config: dict | None = None,
    job_config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create Glue job for the pipeline with intelligent worker configuration and target-specific arguments.

    Args:
        pipeline_id: Pipeline identifier
        target_type: Target type (S3 Table Bucket, Glue Catalog, JDBC)
        target_schema: Target schema definition
        generated_code_s3_path: S3 location of generated PySpark script
        s3_table_bucket: S3 Table Bucket info (for S3_TABLES target)
        worker_recommendation: Worker configuration recommendation
        target_config: Target configuration from pipelines table
        test_results: Test results and recommendations from CodeTesting step
        run_id: Run identifier for retrieving code and test info
        source_path: S3 URI of the processed source file from file_validation step
        warehouse_location: S3 Tables warehouse location from infrastructure step or S3 Tables API
        baseline_config: Baseline worker config from get_baseline_worker_config() (used when no recommendation)
        job_config_overrides: Heal-remediation config from the rerun input
            (persisted on the file_validation state row — P28 R9.5). Merged
            OVER the generated config as the last step before
            create_job/update_job so the self-healing agent's fix (e.g. a
            WorkerType upgrade for OOM) persists through the rerun instead of
            being clobbered back. Empty/None for normal runs (no behavior
            change).

    Returns:
        dict: Glue job information (reflects the ACTUAL config used,
        including any applied overrides)
    """
    try:
        job_name = f"agentic-data-ingestion-job-{pipeline_id}"

        # Retrieve target_config if not provided
        if not target_config:
            logger.info("target_config not provided, retrieving from pipelines table")
            target_config = get_target_config_from_pipeline(pipeline_id)

        # Get pipeline metadata up-front: the source_config it carries drives
        # both the per-pipeline IAM role (JDBC sources need VPC/ENI + source-
        # secret + watermark grants regardless of target type) and the job
        # argument/connection wiring below.
        pipelines_table = dynamodb.Table(PIPELINES_TABLE)
        pipeline_response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})
        pipeline_metadata = pipeline_response.get("Item", {})
        source_config = item_dict(pipeline_metadata, "source_config")

        # Create pipeline-specific IAM role with least-privilege permissions.
        # source_config is passed so JDBC-source pipelines get the VPC/ENI,
        # source-secret, and (incremental) watermark-table grants even when
        # the TARGET is non-JDBC (S3 Tables / Glue Catalog / S3 Parquet).
        logger.info(f"Creating pipeline-specific IAM role for {pipeline_id}")
        role_arn = create_pipeline_specific_role(
            pipeline_id, target_config, s3_table_bucket, source_config=source_config
        )

        # Use worker recommendation if provided, otherwise use baseline or defaults
        if worker_recommendation:
            worker_type = worker_recommendation.worker_type
            number_of_workers = worker_recommendation.number_of_workers
            timeout = worker_recommendation.timeout_minutes
            logger.info(
                f"Using recommended configuration: {worker_type}, {number_of_workers} workers, {timeout}min timeout"
            )
        else:
            if baseline_config:
                worker_type = baseline_config["worker_type"]
                number_of_workers = baseline_config["number_of_workers"]
                timeout = baseline_config["timeout"]
                logger.info(
                    f"Using baseline configuration: {worker_type}, {number_of_workers} workers, {timeout}min timeout"
                )
            else:
                worker_type = "G.1X"
                number_of_workers = 2
                timeout = 60
                logger.warning("No worker recommendation or baseline provided, using defaults")

        # pipeline_metadata + source_config were fetched up-front (above the
        # IAM role creation) and are reused here.

        # Base job configuration (without DefaultArguments - will be built dynamically)
        job_config = {
            "Name": job_name,
            "Description": f"AgenticDataIngestion ETL job for pipeline {pipeline_id}",
            "Role": role_arn,
            "ExecutionProperty": {
                "MaxConcurrentRuns": 1,
            },
            "Command": {"Name": "glueetl", "ScriptLocation": generated_code_s3_path, "PythonVersion": "3"},
            "DefaultArguments": {
                "--pipeline-id": pipeline_id,
            },
            # No Glue-native auto-retry (P28, caught live in task 17.2):
            # retries are owned by the self-healing agent / p16
            # retry-from-step. A Glue auto-retry is invisible to the state
            # machine — job_completion's task token was consumed by the FIRST
            # attempt's event, so the retry's completion can never be matched
            # (RecordNotFoundError redelivery until DLQ) — and the retry
            # holds the MaxConcurrentRuns=1 slot, blocking the heal rerun's
            # start_job_run with ConcurrentRunsExceededException.
            "MaxRetries": 0,
            "Timeout": timeout,
            "WorkerType": worker_type,
            "NumberOfWorkers": number_of_workers,
            "GlueVersion": "5.0",  # Use Glue 5.0 for better performance
            "Tags": {
                "Project": "AgenticDataIngestion",
                "PipelineId": pipeline_id,
                "CreatedBy": "agentic-data-ingestion-create-glue-pipeline",
                "WorkerType": worker_type,
                "NumberOfWorkers": str(number_of_workers),
            },
        }

        # Build target-specific job arguments based on target type.
        #
        # Source-aware dispatch (Task 11.3 of p17-jdbc-data-source):
        # When ``source_type == "JDBC"`` the rendered template is
        # ``glue_job_jdbc_source.py.j2``, which wraps the target-write block
        # with ``{% with key_prefix = 'target_' %}`` and declares target-side
        # keys as ``target_<key>`` in ``getResolvedOptions``. To satisfy that
        # contract, the runtime ``DefaultArguments`` must use the same prefix,
        # so we pass ``key_prefix="target_"`` into the target-arg builders and
        # additionally merge in source-side args from
        # ``build_jdbc_source_arguments(source_config)``.
        #
        # When ``source_type`` is ``"S3_UPLOAD"`` (or absent), behaviour is
        # unchanged: ``key_prefix=""`` yields byte-identical legacy keys
        # (``--table_name``, ``--jdbc_url``, ...) and no source-side args are
        # emitted. ``--source_path`` continues to flow from the file_validation
        # step as before.
        target_type_normalized = target_config.get("type", "").upper()
        source_type_raw = pipeline_metadata.get("source_type", "S3_UPLOAD") or "S3_UPLOAD"
        source_type_normalized = str(source_type_raw).upper()
        is_jdbc_source = source_type_normalized == "JDBC"
        target_key_prefix = "target_" if is_jdbc_source else ""
        # source_config was resolved up-front (above IAM role creation).
        target_specific_arguments = {}

        if target_type_normalized in [
            "S3_TABLES",
            "S3_TABLE_BUCKET",
            "NEW_S3_TABLE_BUCKET",
            "EXISTING_S3_TABLE_BUCKET",
        ]:
            logger.info("Building job arguments for S3 Table Bucket target")
            target_specific_arguments = build_s3_table_bucket_arguments(
                target_config,
                s3_table_bucket or {},
                source_path=source_path,
                warehouse_location=warehouse_location,
                key_prefix=target_key_prefix,
            )

        elif target_type_normalized == "GLUE_CATALOG":
            logger.info("Building job arguments for Glue Catalog target")
            target_specific_arguments = build_glue_catalog_arguments(
                target_config, source_path=source_path, key_prefix=target_key_prefix
            )

        elif target_type_normalized == "JDBC":
            logger.info("Building job arguments for JDBC target")
            target_specific_arguments = build_jdbc_arguments(
                target_config, source_path=source_path, key_prefix=target_key_prefix
            )
            _attach_jdbc_target_connection(job_config, target_config)

        elif target_type_normalized in ["S3_PARQUET", "S3"]:
            logger.info("Building job arguments for S3 Parquet target")
            target_specific_arguments = build_s3_parquet_arguments(
                target_config, source_path=source_path, key_prefix=target_key_prefix
            )

        else:
            logger.warning(f"Unknown target type: {target_type_normalized}, using fallback configuration")
            target_specific_arguments = {
                "--source_path": source_path,
                "--target-type": target_type,
                "--target-database-type": "s3-parquet",
                "--output-format": "parquet",
            }

        # JDBC source: drop ``--source_path`` (the JDBC source template does
        # NOT declare ``source_path`` in its ``getResolvedOptions`` — the
        # source DataFrame comes from JDBC, not S3) and merge in the source-
        # side args from ``build_jdbc_source_arguments``. Target-side keys
        # are already prefixed with ``target_`` above, so source and target
        # arg namespaces are disjoint by construction. The assertion below
        # is defence-in-depth in case a future builder forgets to apply the
        # prefix to a key the source builder also emits.
        if is_jdbc_source:
            target_specific_arguments.pop("--source_path", None)
            source_arguments = build_jdbc_source_arguments(source_config)
            overlap = set(source_arguments) & set(target_specific_arguments)
            if overlap:
                raise ValueError(
                    f"Source/target Glue argument key collision for JDBC source pipeline: {sorted(overlap)}. "
                    f"Target builders must apply key_prefix='target_' to all template-matching keys."
                )
            target_specific_arguments.update(source_arguments)
            logger.info(
                f"Merged {len(source_arguments)} JDBC source-side args into DefaultArguments "
                f"(read_mode={source_config.get('read_mode', 'unknown')})"
            )

        # Add partition_columns if configured (applies to all target types)
        partition_columns = target_config.get("partition_columns", "")
        if partition_columns:
            target_specific_arguments["--partition_columns"] = partition_columns

        # Add write_mode and merge_key (applies to all target types)
        target_specific_arguments.update(_common_write_arguments(target_config))

        # Build parameters based on generated code requirements
        if PARAMETER_BUILDER_AVAILABLE:
            logger.info("Using parameter builder to align job parameters with generated code")
            code_based_parameters = build_glue_job_parameters(
                pipeline_id=pipeline_id,
                generated_code_table_name=GENERATED_CODE_TABLE,
                pipeline_metadata=pipeline_metadata,
                target_config=target_config,
                additional_params=target_specific_arguments,
            )
            job_config["DefaultArguments"].update(code_based_parameters)

            # Validate parameters
            code_reqs = get_code_requirements(pipeline_id, GENERATED_CODE_TABLE)
            if code_reqs:
                is_valid, missing = validate_parameters(
                    job_config["DefaultArguments"], code_reqs.get("required_params", [])
                )
                if not is_valid:
                    logger.warning(f"Missing required parameters: {missing}")
                else:
                    logger.info("All required parameters validated successfully")
        else:
            logger.warning("Parameter builder not available, using target-specific arguments only")
            job_config["DefaultArguments"].update(target_specific_arguments)

        # Validate that all template-required parameters are present in DefaultArguments (Task 4)
        if PARAMETER_BUILDER_AVAILABLE:
            code_reqs_for_validation = get_code_requirements(pipeline_id, GENERATED_CODE_TABLE)
            template_required_params = (
                code_reqs_for_validation.get("required_params", []) if code_reqs_for_validation else []
            )
            if template_required_params:
                logger.info(
                    f"Validating {len(template_required_params)} template-required parameters against DefaultArguments"
                )
                validate_required_parameters(
                    job_config["DefaultArguments"], template_required_params, target_type_normalized
                )

        # Add schema field information
        if target_schema.get("fields"):
            field_info = json.dumps(
                [
                    {
                        "name": field["name"],
                        "type": field["type"],
                        "nullable": field.get("nullable", True),
                        "context": field.get("context", ""),
                    }
                    for field in target_schema["fields"]
                ]
            )
            job_config["DefaultArguments"]["--target-schema-fields"] = field_info

        # Incorporate test recommendations into job configuration
        if test_results:
            logger.info("Incorporating test recommendations into job configuration")
            job_config = incorporate_test_recommendations_into_job_config(
                job_config, test_results, worker_recommendation
            )
        else:
            logger.info("No test results available, skipping test recommendations")

        # Add checkpoint arguments if enabled
        checkpoint_bucket = os.environ.get("CHECKPOINT_BUCKET", "")
        enable_checkpoints = target_config.get("enable_checkpoints", False) if target_config else False
        if enable_checkpoints and checkpoint_bucket:
            job_config["DefaultArguments"]["--checkpoint_bucket"] = checkpoint_bucket
            job_config["DefaultArguments"]["--checkpoint_enabled"] = "true"
            logger.info(f"Checkpointing enabled: bucket={checkpoint_bucket}")

        # JDBC source: attach the source Glue connection (so the job runs in
        # the source VPC) and ship jdbc_helpers via --extra-py-files (so the
        # rendered source script can import it at runtime). Done last, after
        # all DefaultArguments assembly, so neither Connections nor
        # --extra-py-files can be clobbered by the parameter-builder merge.
        if is_jdbc_source:
            _add_jdbc_source_job_wiring(job_config, pipeline_metadata, source_config, pipeline_id)

        # P28 R9.5 WorkerType persistence: merge heal-remediation overrides
        # OVER everything generated above (recommendation, baseline, test
        # recommendations) as the FINAL config step, so the closing
        # create_job/update_job cannot clobber the self-healing agent's fix.
        applied_overrides = _merge_job_config_overrides(job_config, job_config_overrides)
        if applied_overrides:
            # The locals feed the result/state/metadata below — refresh them so
            # every downstream record reflects the ACTUAL config used.
            worker_type = job_config.get("WorkerType", worker_type)
            number_of_workers = job_config.get("NumberOfWorkers", number_of_workers)
            timeout = job_config.get("Timeout", timeout)

        # Check if job already exists
        try:
            existing_job = glue_client.get_job(JobName=job_name)
            logger.info(f"Updating existing Glue job: {job_name}")

            # Update the job (exclude Name and Tags - Tags can only be set during creation)
            job_update = {k: v for k, v in job_config.items() if k not in ["Name", "Tags"]}
            glue_client.update_job(JobName=job_name, JobUpdate=cast("JobUpdateTypeDef", job_update))

        except glue_client.exceptions.EntityNotFoundException:
            logger.info(f"Creating new Glue job: {job_name}")
            glue_client.create_job(**job_config)

        result = {
            "job_name": job_name,
            "job_arn": f"arn:aws:glue:{AWS_REGION}:{get_aws_account_id()}:job/{job_name}",
            "role_arn": role_arn,
            "script_location": generated_code_s3_path,
            "worker_type": worker_type,
            "number_of_workers": number_of_workers,
            "timeout_minutes": timeout,
            "glue_version": "5.0",
            "status": "created",
        }
        if applied_overrides:
            # Persist which overrides shaped the effective config so the state
            # row / dashboards can see the heal fix was honored (P28 R9.5).
            result["job_config_overrides_applied"] = applied_overrides

        # Add worker recommendation details if available
        estimated_cost_per_run = 0.0
        if worker_recommendation:
            result["estimated_cost_per_run"] = worker_recommendation.estimated_cost_per_run
            result["estimated_duration_minutes"] = worker_recommendation.estimated_duration_minutes
            result["recommendation_confidence"] = worker_recommendation.confidence
            estimated_cost_per_run = worker_recommendation.estimated_cost_per_run

        # Publish Glue job creation metrics
        if METRICS_AVAILABLE:
            try:
                environment = os.environ.get("ENVIRONMENT", "dev")
                mcp_enabled = MCP_ENABLED
                fallback_used = worker_recommendation is None

                publish_glue_job_creation_metrics(
                    pipeline_id=pipeline_id,
                    job_name=job_name,
                    creation_success=True,
                    worker_type=worker_type,
                    num_workers=number_of_workers,
                    estimated_cost_per_run=estimated_cost_per_run,
                    mcp_enabled=mcp_enabled,
                    fallback_used=fallback_used,
                    environment=environment,
                )
                logger.info("Published Glue job creation metrics")
            except Exception as metrics_error:
                logger.warning(f"Failed to publish metrics: {metrics_error}")

        return result

    except Exception as e:
        logger.error(f"Error creating Glue job: {e}")

        # Publish failure metrics
        if METRICS_AVAILABLE:
            try:
                environment = os.environ.get("ENVIRONMENT", "dev")
                publish_glue_job_creation_metrics(
                    pipeline_id=pipeline_id,
                    job_name=f"agentic-data-ingestion-job-{pipeline_id}",
                    creation_success=False,
                    worker_type="unknown",
                    num_workers=0,
                    estimated_cost_per_run=0.0,
                    mcp_enabled=MCP_ENABLED,
                    fallback_used=True,
                    environment=environment,
                )
            except Exception as metrics_error:
                logger.warning(f"Failed to publish failure metrics: {metrics_error}")

        raise


def deploy_glue_pipeline_with_mcp(
    agent,
    mcp_client,
    pipeline_id,
    target_type,
    target_schema,
    generated_code_s3_path,
    s3_table_bucket,
    event,
    source_path="",
    warehouse_location="",
    baseline_config=None,
    job_config_overrides=None,
):
    """Deploy Glue pipeline using MCP tools for intelligent configuration.

    ``job_config_overrides`` (P28 R9.5): heal-remediation config threaded
    through to ``create_glue_job`` on BOTH the agent-recommendation path and
    the rule-based fallback, so a heal rerun's WorkerType fix persists no
    matter which deployment path the rerun takes.
    """
    try:
        run_id = event.get("run_id")

        # Retrieve test results from CodeTesting step (step 6)
        logger.info(f"Retrieving test results from CodeTesting step for run_id: {run_id}")
        test_results = get_test_results_from_pipeline_runs(pipeline_id, run_id)

        if test_results.get("test_passed"):
            logger.info(
                f"Code testing passed, incorporating {len(test_results.get('recommendations', []))} recommendations"
            )
        else:
            logger.warning(f"Code testing status: {test_results.get('test_status')}, proceeding with caution")

        # Extract data characteristics
        data_characteristics = get_data_characteristics(pipeline_id, run_id, event)

        logger.info(f"Data characteristics extracted: {data_characteristics}")

        # Get worker recommendation from agent
        try:
            recommendation = get_worker_recommendation_with_agent(
                agent,
                mcp_client,
                data_characteristics,
                {"target_type": target_type, "target_schema": target_schema, "s3_table_bucket": s3_table_bucket},
            )
            logger.info(
                f"Using agent-powered recommendation: {recommendation.worker_type}, {recommendation.number_of_workers} workers"
            )

        except Exception as e:
            logger.warning(f"Agent recommendation failed: {e}, using fallback")
            recommendation = get_default_worker_recommendation(data_characteristics)
            logger.info(
                f"Using fallback recommendation: {recommendation.worker_type}, {recommendation.number_of_workers} workers"
            )

        # Retrieve target_config from pipelines table
        target_config = get_target_config_from_pipeline(pipeline_id)

        # Create Glue resources with recommended configuration and test results
        database_name = create_glue_database(pipeline_id)
        glue_job_info = create_glue_job(
            pipeline_id,
            target_type,
            target_schema,
            generated_code_s3_path,
            s3_table_bucket,
            worker_recommendation=recommendation,
            target_config=target_config,
            test_results=test_results,
            run_id=run_id,
            source_path=source_path,
            warehouse_location=warehouse_location,
            baseline_config=baseline_config,
            job_config_overrides=job_config_overrides,
        )

        # Add recommendation details to result
        glue_job_info["worker_recommendation"] = recommendation.to_dict()
        glue_job_info["mcp_enhanced"] = len(recommendation.mcp_tools_used) > 0
        glue_job_info["test_results_applied"] = test_results.get("test_passed", False)

        return {
            "database_name": database_name,
            "glue_job_info": glue_job_info,
            "worker_recommendation": recommendation.to_dict(),
            "test_results": test_results,
        }

    except Exception as e:
        logger.error(f"Error in MCP-enhanced Glue deployment: {e}")
        # Fall back to standard deployment
        run_id = event.get("run_id")
        database_name = create_glue_database(pipeline_id)

        # Try to get test results even in fallback mode
        try:
            test_results = get_test_results_from_pipeline_runs(pipeline_id, run_id)
        except Exception as test_error:
            logger.warning(f"Failed to get test results for fallback deployment: {test_error}")
            test_results = None

        # Use default recommendation
        data_characteristics = get_data_characteristics(pipeline_id, run_id, event)
        recommendation = get_default_worker_recommendation(data_characteristics)

        # Retrieve target_config from pipelines table
        target_config = get_target_config_from_pipeline(pipeline_id)

        glue_job_info = create_glue_job(
            pipeline_id,
            target_type,
            target_schema,
            generated_code_s3_path,
            s3_table_bucket,
            worker_recommendation=recommendation,
            target_config=target_config,
            test_results=test_results,
            run_id=run_id,
            source_path=source_path,
            warehouse_location=warehouse_location,
            baseline_config=baseline_config,
            job_config_overrides=job_config_overrides,
        )

        return {
            "database_name": database_name,
            "glue_job_info": glue_job_info,
            "mcp_enhanced": False,
            "fallback_reason": str(e),
            "worker_recommendation": recommendation.to_dict(),
            "test_results": test_results,
        }


def update_pipeline_metadata_with_glue_job(
    pipeline_id: str,
    glue_job_info: dict[str, Any],
    worker_recommendation: dict[str, Any],
    cost_estimate: dict[str, Any],
    target_config: dict[str, Any],
) -> None:
    """
    Store Glue job metadata in pipelines table.

    Updates the pipeline record with:
    - Glue job name and ARN
    - Worker configuration (type, count)
    - Estimated cost per run
    - MCP recommendations and rationale
    - Target configuration details

    Args:
        pipeline_id: Pipeline identifier
        glue_job_info: Glue job information from create_glue_job
        worker_recommendation: Worker configuration recommendation
        cost_estimate: Cost breakdown from estimate_job_cost
        target_config: Target configuration details
    """
    try:
        logger.info(f"Updating pipeline metadata for {pipeline_id} with Glue job information")

        # Prepare Glue job metadata
        glue_metadata = {
            "glue_job_name": glue_job_info.get("job_name", ""),
            "glue_job_arn": glue_job_info.get("job_arn", ""),
            "glue_version": glue_job_info.get("glue_version", "5.0"),
            "script_location": glue_job_info.get("script_location", ""),
            "role_arn": glue_job_info.get("role_arn", ""),
            "status": glue_job_info.get("status", "created"),
            "created_at": datetime.now(UTC).isoformat(),
        }

        # Prepare worker configuration. Prefer the ACTUAL config the job was
        # created/updated with (glue_job_info, which reflects any heal-rerun
        # job_config_overrides — P28 R9.5) over the recommendation, so the
        # pipeline record and dashboards see the truth, not the clobbered-back
        # recommendation values.
        worker_config = {
            "worker_type": glue_job_info.get("worker_type") or worker_recommendation.get("worker_type", "G.1X"),
            "number_of_workers": glue_job_info.get("number_of_workers")
            or worker_recommendation.get("number_of_workers", 2),
            "timeout_minutes": glue_job_info.get("timeout_minutes") or worker_recommendation.get("timeout_minutes", 60),
            "estimated_duration_minutes": worker_recommendation.get("estimated_duration_minutes", 30),
        }
        if glue_job_info.get("job_config_overrides_applied"):
            worker_config["job_config_overrides_applied"] = glue_job_info["job_config_overrides_applied"]

        # Prepare cost information (convert floats to Decimal for DynamoDB)
        cost_info = convert_floats_to_decimal(
            {
                "estimated_cost_per_run": cost_estimate.get("estimated_cost_per_run", 0.0),
                "estimated_monthly_cost": cost_estimate.get("estimated_monthly_cost", 0.0),
                "cost_per_gb": cost_estimate.get("cost_per_gb", 0.0),
                "total_dpu": cost_estimate.get("total_dpu", 0.0),
                "dpu_hours": cost_estimate.get("dpu_hours", 0.0),
                "billing_note": cost_estimate.get("billing_note", ""),
            }
        )

        # Prepare MCP recommendation details (convert floats to Decimal)
        mcp_recommendation = convert_floats_to_decimal(
            {
                "rationale": worker_recommendation.get("rationale", ""),
                "confidence": worker_recommendation.get("confidence", 0.0),
                "mcp_tools_used": worker_recommendation.get("mcp_tools_used", []),
                "mcp_enhanced": glue_job_info.get("mcp_enhanced", False),
                "recommendation_timestamp": datetime.now(UTC).isoformat(),
            }
        )

        # Update pipelines table with Glue job metadata
        pipelines_table.update_item(
            Key={"pipeline_id": pipeline_id},
            UpdateExpression="""
                SET glue_job_metadata = :glue_metadata,
                    worker_configuration = :worker_config,
                    cost_information = :cost_info,
                    mcp_recommendation = :mcp_recommendation,
                    glue_job_created_at = :timestamp,
                    last_updated = :timestamp
            """,
            ExpressionAttributeValues={
                ":glue_metadata": glue_metadata,
                ":worker_config": worker_config,
                ":cost_info": cost_info,
                ":mcp_recommendation": mcp_recommendation,
                ":timestamp": datetime.now(UTC).isoformat(),
            },
        )

        logger.info(f"Successfully updated pipeline metadata for {pipeline_id}")
        logger.info(f"  - Glue Job: {glue_metadata['glue_job_name']}")
        logger.info(f"  - Worker Config: {worker_config['worker_type']}, {worker_config['number_of_workers']} workers")
        logger.info(f"  - Estimated Cost: ${cost_info['estimated_cost_per_run']:.3f}/run")
        logger.info(f"  - MCP Enhanced: {mcp_recommendation['mcp_enhanced']}")

    except Exception as e:
        logger.error(f"Error updating pipeline metadata: {e}")
        # Don't fail the entire job creation if metadata update fails
        logger.warning("Continuing despite metadata update failure")
