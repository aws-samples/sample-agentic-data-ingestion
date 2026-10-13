# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import sys
from datetime import UTC, datetime

import boto3

# Initialize logger first
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# Add shared directory to path for imports
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
# Add this module's own directory so sibling modules (e.g. workers) import
# both in the Lambda runtime and in local/test execution.
sys.path.append(os.path.dirname(__file__))

from agent_trace_plugin import AgentTracePlugin

# Import shared MCP factory
from strands_mcp_factory import initialize_strands_agent_with_mcp, initialize_strands_agent_with_multi_mcp

# Import shared audit utils
try:
    from audit_utils import write_agent_audit_entry
except ImportError:
    logger.warning("audit_utils not available, agent audit entries will be skipped")
    write_agent_audit_entry = None

# Import shared env_utils via the Layer_Import_Fallback (fix path + import the
# canonical resolver — do not redefine it inline; P21 Req 5.3/5.4/5.7).
try:
    from env_utils import require_env
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from env_utils import require_env

# Worker sizing, cost estimation, and recommendation helpers (extracted to a
# sibling module — P21 Req 4.2). One-way dependency: this entrypoint imports
# from workers; workers never imports lambda_function. ``WorkerRecommendation``,
# ``get_worker_recommendation_with_agent``, and
# ``incorporate_test_recommendations_into_job_config`` are re-exported for
# facade/test compatibility (their only call sites moved to job_builders).
# S3/code/test artifact resolution + prerequisite validation (extracted to a
# sibling module — P21 Req 4.2). One-way dependency: this entrypoint imports
# from artifacts; artifacts never imports lambda_function. Names are re-exported
# here so orchestration call sites (and tests resolving through
# ``lambda_function``) keep working unchanged. ``parse_s3_uri`` and
# ``get_generated_code_location`` are re-exports for test/facade compatibility
# (no internal call site).
from artifacts import (
    get_generated_code_from_pipeline_runs,
    get_generated_code_location,
    get_source_path,
    get_test_results_from_pipeline_runs,
    get_warehouse_location,
    parse_s3_uri,
    validate_job_prerequisites,
)

# JDBC connection / database-secret / VPC + security-group provisioning
# (extracted to a sibling module — P21 Req 4.2). One-way dependency: this
# entrypoint imports from connections; connections never imports
# lambda_function. ``create_glue_security_group`` is re-exported for test
# compatibility (resolved via ``lambda_function`` by test_bare_except_fixes).
#
# ``create_jdbc_connection`` used to be imported and re-exported here. It had no
# caller anywhere and wrote a plaintext USERNAME into ConnectionProperties, which
# ``LGR-819980f-1`` forbids; ``LGR-38510fe-8`` recorded it as dead code to delete
# once P17 Task 5.5 was finished, and it has now been deleted. The canonical
# provisioner is ``create_glue_jdbc_connection`` in ``shared/jdbc_helpers.py``.
from connections import (
    create_glue_security_group,
)

# IAM execution-role + Glue-database provisioning (extracted to a sibling
# module — P21 Req 4.2). One-way dependency: this entrypoint imports from
# iam_roles; iam_roles never imports lambda_function. The ``lambda_handler``
# fallback calls ``create_glue_database``; ``create_pipeline_specific_role`` is
# re-exported for facade/test compatibility (its only call site moved to
# job_builders). There is no shared fallback role any more (threat model T-02):
# a role-creation failure fails the step.
from iam_roles import (
    create_glue_database,
    create_pipeline_specific_role,
)

# Per-target Glue job argument builders (extracted to a sibling module — P21
# Req 4.2). One-way dependency: this entrypoint imports from job_arguments;
# job_arguments never imports lambda_function. Names are re-exported here so
# orchestration call sites (and tests resolving through ``lambda_function``) keep
# working unchanged. The builders' only call sites moved to job_builders; they are
# kept here as facade re-exports. ``resolve_parameter_value`` is no longer among
# them: it had no production caller and resolved plaintext JDBC credentials from
# Secrets Manager inside this Lambda (``LGR-819980f-1``), so it was deleted.
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
    estimate_job_cost,
    get_baseline_worker_config,
    get_data_characteristics,
    get_default_worker_recommendation,
    get_worker_recommendation_with_agent,
    incorporate_test_recommendations_into_job_config,
)

# Shared JDBC helpers (Task 2.2 of p17-jdbc-data-source). Single source of
# truth for JDBC driver / default-port / URL construction. The internal call
# sites moved to the connections/job_builders siblings (P21 Req 4.2), but these
# names are re-exported here for facade/test compatibility — tests resolve
# ``get_jdbc_driver`` / ``build_jdbc_url`` through ``lambda_function``
# (e.g. dashboard_api/test_jdbc_mapping_prop.py).
from jdbc_helpers import build_jdbc_url, get_default_port, get_jdbc_driver

# Single source of truth for the Bedrock model ID (P21 Req 1).
from model_config import resolve_model_id

# P28 R1.1: Step_Lambdas raise on failure instead of returning error payloads.
from sfn_task_token import step_task_token
from step_errors import StepFailure, step_handler
from step_names import STEP_SEQUENCE, StepName

# Import prompt template loader
try:
    from prompt_template_loader import load_prompt_template
except ImportError:
    logger.warning("prompt_template_loader not available")
    load_prompt_template = None

# Import shared modules. ``lambda_handler`` uses ``PipelineMetrics`` for stage
# telemetry; the per-target publish_* helpers moved to job_builders with
# ``create_glue_job`` (P21 Req 4.4 decomposition), so only ``PipelineMetrics``
# is imported here now.
try:
    from metrics_facade import PipelineMetrics

    METRICS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Could not import metrics_facade module: {e}")
    METRICS_AVAILABLE = False
    PipelineMetrics = None

# Schema-evolution availability probe. Imported inside try/except (robust to
# the sibling-module test loader, which can leave __spec__ unset and break
# importlib.util.find_spec). The symbols themselves are re-exports — their
# consumers moved to sibling modules in the P21 decomposition.
try:
    from schema_evolution import (  # noqa: F401  (re-exports / availability probe)
        SchemaChangeType,
        check_job_running_status,
        classify_schema_change,
        handle_schema_evolution,
        rollback_job_to_previous_version,
    )

    SCHEMA_EVOLUTION_AVAILABLE = True
except ImportError as e:
    SCHEMA_EVOLUTION_AVAILABLE = False
    logger.warning(f"Could not import schema_evolution module: {e}")
    SCHEMA_EVOLUTION_AVAILABLE = False

# Enhanced MCP integration (availability probe). Imported inside try/except so
# the module loads even when MCP libs are absent; the client usage itself moved
# to sibling modules in the P21 decomposition, so the names are only re-exported.
try:
    from mcp import ClientSession  # noqa: F401  (re-export / availability probe)
    from mcp.client.streamable_http import streamablehttp_client  # noqa: F401

    MCP_AVAILABLE = True
    logger.info("MCP client libraries loaded successfully")
except ImportError:
    MCP_AVAILABLE = False
    logger.warning("MCP dependencies not available, using basic agent mode")

# SigV4 authentication availability for AgentCore (streamable_http_sigv4).
try:
    from botocore.credentials import Credentials  # noqa: F401  (availability probe)
    from streamable_http_sigv4 import streamablehttp_client_with_sigv4  # noqa: F401

    SIGV4_AVAILABLE = True
    logger.info("SigV4 authentication available for AgentCore (using streamable_http_sigv4)")
except ImportError:
    SIGV4_AVAILABLE = False
    logger.warning("streamable_http_sigv4 not available, using fallback")

# Façade re-export surface (P21 Req 4.2 decomposition). These names are imported
# above purely so callers and tests that resolve them through ``lambda_function``
# (e.g. ``_mod.build_jdbc_arguments`` in the create_glue_pipeline unit tests, and
# ``get_jdbc_driver``/``build_jdbc_url`` in dashboard_api/test_jdbc_mapping_prop.py)
# keep working unchanged after their implementations moved to sibling modules.
# Listing them in __all__ documents the intentional re-exports and tells the
# linter they are not dead imports. Do NOT delete these imports. (Sorted per RUF022.)
__all__ = [
    "WorkerRecommendation",
    "build_glue_catalog_arguments",
    "build_jdbc_arguments",
    "build_jdbc_source_arguments",
    "build_jdbc_url",
    "build_s3_parquet_arguments",
    "build_s3_table_bucket_arguments",
    "create_glue_security_group",
    "create_pipeline_specific_role",
    "get_default_port",
    "get_generated_code_location",
    "get_jdbc_driver",
    "get_worker_recommendation_with_agent",
    "incorporate_test_recommendations_into_job_config",
    "lambda_handler",
    "parse_s3_uri",
    "validate_required_parameters",
]

# Environment variables
PIPELINE_RUNS_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
PIPELINES_TABLE = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
GENERATED_CODE_TABLE = require_env("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
AGENTCORE_ENDPOINT = os.environ.get("AGENTCORE_ENDPOINT", "")
S3_TABLES_JAR_PATH = os.environ.get("S3_TABLES_JAR_PATH", "")
MCP_ENABLED = os.environ.get("MCP_ENABLED", "false").lower() == "true"

# Initialize AWS clients
# Convention: one module-level client per AWS service (see shared/README.md).
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
glue_client = boto3.client("glue")
iam_client = boto3.client("iam")
s3tables_client = boto3.client("s3tables")
secretsmanager_client = boto3.client("secretsmanager")

# DynamoDB tables
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)
pipelines_table = dynamodb.Table(PIPELINES_TABLE)
generated_code_table = dynamodb.Table(GENERATED_CODE_TABLE)

# Bedrock model ID — resolved from the single source of truth (P21 Req 1).
CLAUDE_SONNET_45_MODEL = resolve_model_id()

# System prompt for Glue job optimization (loaded from template)
if load_prompt_template:
    GLUE_JOB_EXPERT_PROMPT = load_prompt_template("glue_pipeline.j2")
else:
    GLUE_JOB_EXPERT_PROMPT = "You are an AWS Glue expert specializing in ETL job optimization."


# System prompt for Glue pipeline creation with MCP tools (loaded from template)
if load_prompt_template:
    GLUE_PIPELINE_PROMPT = load_prompt_template("glue_pipeline_creation.j2")
else:
    GLUE_PIPELINE_PROMPT = "You are an expert AWS Glue and ETL pipeline specialist."


# Glue-job orchestration builders (extracted to a sibling module —
# P21 Req 4.2/4.4 final decomposition so this entrypoint stays at/under the
# documented 800-line module-size guideline). One-way dependency: this
# entrypoint imports from job_builders; job_builders never imports
# lambda_function. Names are re-exported here so orchestration call sites and
# tests resolving through ``lambda_function`` keep working unchanged.
# ``get_target_config_from_pipeline`` is a re-export for facade/test
# compatibility (resolved via ``lambda_function`` by callers/tests).
from job_builders import (
    convert_floats_to_decimal,
    create_glue_job,
    deploy_glue_pipeline_with_mcp,
    get_target_config_from_pipeline,
    update_pipeline_metadata_with_glue_job,
)


@step_handler("create_glue_pipeline")
@step_task_token("create_glue_pipeline")
def lambda_handler(event, context):
    """
    Lambda function to create AWS Glue job infrastructure.

    Args:
        event: Lambda event containing validated code and pipeline specifications
        context: Lambda context object

    Returns:
        dict: Glue job definitions and configurations
    """
    # ── AgentCore Runtime proxy path ────────────────────────────────
    if os.environ.get("USE_AGENTCORE_RUNTIME") == "true":
        from agentcore_proxy import invoke_agentcore_runtime

        return invoke_agentcore_runtime(
            endpoint=os.environ["AGENTCORE_CREATE_PIPELINE_ENDPOINT"],
            payload=event,
            step_name="create_glue_pipeline",
        )

    try:
        import time as _time

        _metrics_start = _time.time()

        logger.info(f"Glue pipeline creation event: {json.dumps(event)}")

        # Extract required data from event
        # Check if data is nested under infrastructure_output (from Step Functions)
        infrastructure_output = event.get("infrastructure_output", {})
        if infrastructure_output:
            # Data comes from infrastructure step via Step Functions
            run_id = infrastructure_output.get("run_id") or event.get("run_id")
            pipeline_id = infrastructure_output.get("pipeline_id") or event.get("pipeline_id")
            s3_table_bucket = infrastructure_output.get("s3_table_bucket", {})
            table_location = infrastructure_output.get("table_location", "")
            target_type = event.get("target_type", "S3 Table Bucket")
            target_schema = event.get("target_schema", {})
        else:
            # Direct invocation format
            run_id = event.get("run_id")
            pipeline_id = event.get("pipeline_id")
            target_type = event.get("target_type", "S3 Table Bucket")
            target_schema = event.get("target_schema", {})
            s3_table_bucket = event.get("s3_table_bucket", {})
            table_location = event.get("table_location", "")

        if not run_id or not pipeline_id:
            raise ValueError("run_id and pipeline_id are required")

        logger.append_keys(run_id=run_id, pipeline_id=pipeline_id, step_name=StepName.CREATE_GLUE_PIPELINE)

        # Initialize trace plugin for observability
        trace_plugin = AgentTracePlugin(
            pipeline_id=pipeline_id,
            run_id=run_id,
            step_name="glue_pipeline_creation",
        )

        # Read file size/count for baseline worker sizing (Phase 11)
        file_size_bytes = event.get("file_size_bytes", 0)
        file_count = event.get("file_count", 1)
        baseline_config = get_baseline_worker_config(file_size_bytes, file_count) if file_size_bytes else None
        if baseline_config:
            logger.info(f"Baseline worker config: {baseline_config} (file_size={file_size_bytes}, count={file_count})")

        if not s3_table_bucket and not table_location:
            logger.info("No S3 table bucket or table location provided — target may not require infrastructure")
            s3_table_bucket = {}
            table_location = ""

        logger.info(f"Creating Glue pipeline for: {pipeline_id}, target_type: {target_type}")
        logger.info(
            f"Using S3 table bucket: {s3_table_bucket.get('bucket_name')}, table: {s3_table_bucket.get('table_name')}"
        )

        # Retrieve generated PySpark script location first (needed for validation)
        logger.info(f"Retrieving generated PySpark script for pipeline {pipeline_id}, run {run_id}")
        try:
            code_info = get_generated_code_from_pipeline_runs(pipeline_id, run_id)
            generated_code_s3_path = code_info["code_s3_uri"]
            logger.info(f"Using PySpark script: {generated_code_s3_path}")
        except Exception as e:
            logger.error(f"Failed to retrieve generated code: {e}")
            raise ValueError(f"Cannot create Glue job without valid PySpark script: {e}")

        # Retrieve target configuration from pipelines table
        try:
            target_config = get_target_config_from_pipeline(pipeline_id)
            logger.info(f"Retrieved target configuration: {target_config.get('type', 'unknown')}")
        except Exception as e:
            logger.warning(f"Could not retrieve target_config: {e}")
            target_config = None

        # Resolve source_path (p55 task 4.3). `get_source_path` reads the
        # schema_extract step's `readable_source_uri` — the object that step
        # resolved as readable for this run, which for an Excel_Workbook is the
        # converted CSV and for every Native_Format_Set member is the original
        # upload. It takes NO `source_format` argument: format is already
        # accounted for in the value, and deciding it again here would be a
        # second reader of the conversion gate (`LGR-28b4d75-1`, derive once).
        # The parameter it used to take was dead and its docstring described
        # behaviour the body did not have (task 4.1, finding 1).
        logger.info(f"Resolving source_path for run_id={run_id}")
        job_config_overrides: dict = {}
        try:
            # The file_validation row carries job_config_overrides (P28 R9.5):
            # file_validation persists the heal rerun's remediation config
            # (e.g. a WorkerType upgrade) keyed by the fresh run_id, and this
            # step merges it over the generated job config instead of clobbering
            # the fix back. `file_type` is projected only for the log line
            # below — it is the DETECTED Canonical_Format, which is not
            # necessarily the format the job reads (an .xlsx is detected as
            # `excel` and read as `csv`); the format the job reads travels on
            # the schema_extract row beside the URI and is consumed by
            # `create_code`, which owns the render.
            _fv_resp = pipeline_runs_table.get_item(
                Key={"run_id": run_id, "step_name": "file_validation"},
                ProjectionExpression="file_type, job_config_overrides",
            )
            _fv_item = _fv_resp.get("Item") or {}
            _detected_format = _fv_item.get("file_type", "")
            job_config_overrides = _fv_item.get("job_config_overrides") or {}
            if job_config_overrides:
                logger.info(
                    f"file_validation row carries job_config_overrides for run {run_id}: "
                    f"{sorted(job_config_overrides.keys())} (heal-rerun remediation config)"
                )
            source_path = get_source_path(run_id)
            logger.info(f"Resolved source_path: {source_path} (detected format={_detected_format})")
        except ValueError as e:
            logger.error(f"Failed to resolve source_path: {e}")
            raise ValueError(f"Cannot create Glue job without source_path: {e}")

        # Resolve warehouse_location for S3 Tables targets (Task 3: warehouse_location resolution)
        warehouse_location = ""
        target_type_check = (target_config or {}).get("type", "").upper()
        if target_type_check in ["S3_TABLES", "S3_TABLE_BUCKET", "NEW_S3_TABLE_BUCKET", "EXISTING_S3_TABLE_BUCKET"]:
            logger.info(f"Resolving warehouse_location for S3 Tables target, run_id={run_id}")
            try:
                warehouse_location = get_warehouse_location(run_id, target_config or {})
                logger.info(f"Resolved warehouse_location: {warehouse_location}")
            except ValueError as e:
                logger.error(f"Failed to resolve warehouse_location: {e}")
                raise ValueError(f"Cannot create Glue job for S3 Tables without warehouse_location: {e}")

        # Comprehensive validation of all prerequisites before job creation
        logger.info("=" * 80)
        logger.info("VALIDATING JOB PREREQUISITES")
        logger.info("=" * 80)

        validation_results = validate_job_prerequisites(
            pipeline_id=pipeline_id,
            run_id=run_id,
            script_location=generated_code_s3_path,
            target_type=target_type,
            s3_table_bucket=s3_table_bucket,
            target_config=target_config,
        )

        # Check validation results
        if not validation_results["valid"]:
            error_summary = f"Job prerequisites validation failed with {len(validation_results['errors'])} errors"
            logger.error(error_summary)
            logger.error("Errors:")
            for error in validation_results["errors"]:
                logger.error(f"  ✗ {error}")

            if validation_results["recommendations"]:
                logger.info("Recommendations to fix errors:")
                for rec in validation_results["recommendations"]:
                    logger.info(f"  → {rec}")

            # Return detailed error response
            if PipelineMetrics:
                try:
                    _duration_ms = (_time.time() - _metrics_start) * 1000
                    PipelineMetrics.record_stage_execution(
                        stage_name="CreateGluePipeline",
                        pipeline_id=pipeline_id or "unknown",
                        status="failed",
                        duration_ms=_duration_ms,
                    )
                except Exception as exc:
                    # Non-fatal: stage metrics are best-effort telemetry; a publish
                    # failure must not mask the validation error response (P21 Req 6.2/6.5).
                    logger.warning(
                        f"Failed to publish CreateGluePipeline (validation-failed) stage metrics "
                        f"for pipeline_id={pipeline_id} run_id={run_id}: {type(exc).__name__}: {exc}"
                    )

            # Fail loud: raise so the Step Functions task enters States.TaskFailed
            # and routes to UpdateFailureState via createGluePipeline.addCatch.
            # Returning a 400 here makes the SFN task *succeed*, letting the run
            # continue to RunPipeline which then fails with a misleading
            # "Glue job name not found" that masks the real prerequisite error.
            raise RuntimeError(f"{error_summary}: {'; '.join(validation_results['errors'])}")

        # Log validation success
        logger.info("✓ All prerequisite validations passed")
        if validation_results["warnings"]:
            logger.warning(f"Proceeding with {len(validation_results['warnings'])} warnings:")
            for warning in validation_results["warnings"]:
                logger.warning(f"  ⚠ {warning}")

        logger.info("=" * 80)

        # Try to create Strands agent with proper MCP integration (following successful infrastructure pattern)
        logger.info("Attempting to initialize Strands Agent with AWS Data Processing MCP tools for Glue pipeline")

        # Use focused MCP endpoints if available, fall back to monolithic
        mcp_endpoints = []
        codegen_ep = os.environ.get("AGENTCORE_CODEGEN_ENDPOINT", "")
        analytics_ep = os.environ.get("AGENTCORE_ANALYTICS_ENDPOINT", "")
        default_ep = os.environ.get("AGENTCORE_ENDPOINT", "")

        if codegen_ep or analytics_ep:
            # Use focused endpoints
            if codegen_ep:
                mcp_endpoints.append(codegen_ep)
            if analytics_ep:
                mcp_endpoints.append(analytics_ep)
            logger.info(f"Using {len(mcp_endpoints)} focused MCP endpoint(s)")
            strands_agent, strands_mcp_client = initialize_strands_agent_with_multi_mcp(
                system_prompt=GLUE_PIPELINE_PROMPT,
                endpoints=mcp_endpoints,
            )
        else:
            # Fall back to monolithic endpoint
            logger.info("Using monolithic MCP endpoint (no focused endpoints configured)")
            strands_agent, strands_mcp_client = initialize_strands_agent_with_mcp(system_prompt=GLUE_PIPELINE_PROMPT)

        if strands_agent and strands_mcp_client:
            logger.info(
                "Successfully initialized Strands Agent with MCP tools for Glue pipeline - using MCP integration"
            )
            agent = strands_agent
            agent.add_hook(trace_plugin)
            mcp_client = strands_mcp_client
            using_strands_mcp = True
        else:
            logger.warning("Failed to initialize Strands Agent with MCP for Glue pipeline, using fallback mode")
            agent = None
            mcp_client = None
            using_strands_mcp = False

        if agent and using_strands_mcp:
            logger.info("Agent will use AWS Data Processing MCP tools for intelligent Glue pipeline configuration")

            # Enhanced deployment with MCP intelligence
            deployment_result = deploy_glue_pipeline_with_mcp(
                agent,
                mcp_client,
                pipeline_id,
                target_type,
                target_schema,
                generated_code_s3_path,
                s3_table_bucket,
                event,
                source_path=source_path,
                warehouse_location=warehouse_location,
                baseline_config=baseline_config,
                job_config_overrides=job_config_overrides,
            )

            database_name = deployment_result["database_name"]
            glue_job_info = deployment_result["glue_job_info"]

        else:
            logger.warning("MCP integration unavailable, falling back to rule-based worker selection")
            logger.info("Using data volume thresholds for worker type selection")

            # Retrieve test results
            try:
                test_results = get_test_results_from_pipeline_runs(pipeline_id, run_id)
                logger.info(f"Retrieved test results: status={test_results.get('test_status')}")
            except Exception as e:
                logger.warning(f"Could not retrieve test results: {e}")
                test_results = None

            # Get data characteristics for fallback recommendation
            data_characteristics = get_data_characteristics(pipeline_id, run_id, event)

            # Use rule-based fallback recommendation
            recommendation = get_default_worker_recommendation(data_characteristics)
            logger.info(
                f"Fallback recommendation: {recommendation.worker_type}, {recommendation.number_of_workers} workers"
            )
            logger.info("Fallback reason: MCP unavailable or disabled")

            # Standard deployment with fallback recommendation
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
            glue_job_info["mcp_enhanced"] = False
            glue_job_info["fallback_reason"] = "MCP unavailable"
            glue_job_info["worker_recommendation"] = recommendation.to_dict()
            glue_job_info["test_results_applied"] = test_results.get("test_passed", False) if test_results else False

            # Create deployment result structure for consistency
            deployment_result = {
                "database_name": database_name,
                "glue_job_info": glue_job_info,
                "worker_recommendation": recommendation.to_dict(),
                "mcp_enhanced": False,
                "test_results": test_results,
            }

        # Generate timestamps
        timestamp = datetime.now(UTC).isoformat()

        # Flush trace data to DynamoDB
        trace_plugin.flush()

        # Audit: record worker config recommendation
        if write_agent_audit_entry:
            write_agent_audit_entry(
                agent_type="glue_pipeline",
                action="recommend_workers",
                resource_id=run_id,
                details={
                    "worker_type": glue_job_info.get("worker_type", "G.1X"),
                    "number_of_workers": glue_job_info.get("number_of_workers", 2),
                    "target_type": target_type,
                },
                run_id=run_id,
                pipeline_id=pipeline_id,
            )

        # Extract worker recommendation from deployment result
        worker_recommendation_dict = deployment_result.get("worker_recommendation", {})

        # Calculate cost estimate based on worker configuration
        logger.info("Calculating cost estimate for Glue job")
        cost_estimate = estimate_job_cost(
            {
                "WorkerType": glue_job_info.get("worker_type", "G.1X"),
                "NumberOfWorkers": glue_job_info.get("number_of_workers", 2),
                "estimated_duration_minutes": worker_recommendation_dict.get("estimated_duration_minutes", 30),
                "data_volume_gb": event.get("data_volume_gb", 1.0),
            }
        )

        # Update pipelines table with Glue job metadata, worker config, and cost estimate
        logger.info("Updating pipelines table with Glue job metadata")
        # Use target_config retrieved earlier during validation
        if not target_config:
            target_config = {"type": target_type}

        update_pipeline_metadata_with_glue_job(
            pipeline_id, glue_job_info, worker_recommendation_dict, cost_estimate, target_config
        )

        # Store Glue pipeline creation result in pipeline runs table
        # Convert floats to Decimal for DynamoDB compatibility
        pipeline_runs_table.put_item(
            Item=convert_floats_to_decimal(
                {
                    "run_id": run_id,  # Partition key
                    # P28 R8.2 (task 14.2): canonical step name — the legacy
                    # "CreateGluePipeline" state-row variant is retired.
                    "step_name": StepName.CREATE_GLUE_PIPELINE,
                    "status": "success",
                    "step_sequence": STEP_SEQUENCE[StepName.CREATE_GLUE_PIPELINE],
                    "timestamp": timestamp,
                    "pipeline_id": pipeline_id,  # For PipelineIndex GSI
                    "original_pipeline_id": pipeline_id,
                    "output_data": {
                        "database_name": database_name,
                        "glue_job": glue_job_info,
                        "target_type": target_type,
                        "target_schema_name": target_schema.get("name", ""),
                        "s3_table_bucket": s3_table_bucket,
                        "table_location": table_location,
                        "script_location": generated_code_s3_path,
                        "worker_recommendation": worker_recommendation_dict,
                        "cost_estimate": cost_estimate,
                        "mcp_enhanced": deployment_result.get("mcp_enhanced", False),
                    },
                }
            )
        )
        logger.info(f"Glue pipeline creation state stored in {PIPELINE_RUNS_TABLE}")

        # Log worker recommendation details
        if worker_recommendation_dict:
            logger.info("Worker recommendation applied:")
            logger.info(f"  - Worker Type: {worker_recommendation_dict.get('worker_type')}")
            logger.info(f"  - Number of Workers: {worker_recommendation_dict.get('number_of_workers')}")
            logger.info(f"  - Timeout: {worker_recommendation_dict.get('timeout_minutes')} minutes")
            logger.info(f"  - Estimated Cost: ${worker_recommendation_dict.get('estimated_cost_per_run', 0):.2f}")
            logger.info(
                f"  - Estimated Duration: {worker_recommendation_dict.get('estimated_duration_minutes')} minutes"
            )
            logger.info(f"  - Confidence: {worker_recommendation_dict.get('confidence', 0):.2f}")
            logger.info(f"  - Rationale: {worker_recommendation_dict.get('rationale', 'N/A')}")

            mcp_tools = worker_recommendation_dict.get("mcp_tools_used", [])
            if mcp_tools:
                logger.info(f"  - MCP Tools Used: {', '.join(mcp_tools)}")
            else:
                logger.info("  - MCP Tools Used: None (fallback mode)")

        # Return output per spec (convert floats for consistency)
        # Publish stage metrics
        if PipelineMetrics:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="CreateGluePipeline",
                    pipeline_id=pipeline_id or "unknown",
                    status="completed",
                    duration_ms=_duration_ms,
                )
            except Exception as exc:
                # Non-fatal: stage metrics are best-effort telemetry; a publish failure
                # must not fail a successful Glue pipeline creation (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish CreateGluePipeline (completed) stage metrics for "
                    f"pipeline_id={pipeline_id} run_id={run_id}: {type(exc).__name__}: {exc}"
                )

        return convert_floats_to_decimal(
            {
                "statusCode": 200,
                "run_id": run_id,
                "pipeline_id": pipeline_id,
                "database_name": database_name,
                "glue_job": glue_job_info,
                "target_type": target_type,
                "script_location": generated_code_s3_path,
                "worker_recommendation": worker_recommendation_dict,
                "cost_estimate": cost_estimate,
                "mcp_enhanced": deployment_result.get("mcp_enhanced", False),
                "next_step": "run_quality_checks",
            }
        )

    except Exception as e:
        error_message = f"Error in Glue pipeline creation: {e!s}"
        logger.error(error_message)

        if PipelineMetrics:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="CreateGluePipeline",
                    pipeline_id=event.get("pipeline_id", "unknown"),
                    status="failed",
                    duration_ms=_duration_ms,
                )
            except Exception as exc:
                # Non-fatal: failure-path stage metrics are best-effort telemetry; the
                # original error is still recorded below (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish CreateGluePipeline (failed) stage metrics for "
                    f"pipeline_id={event.get('pipeline_id', 'unknown')} "
                    f"run_id={event.get('run_id', 'unknown')}: {type(exc).__name__}: {exc}"
                )

        # Write error to pipeline runs table
        try:
            run_id = event.get("run_id", "unknown")
            pipeline_runs_table.put_item(
                Item={
                    "run_id": run_id,  # Partition key
                    "step_name": StepName.CREATE_GLUE_PIPELINE,
                    "status": "failed",
                    "step_sequence": STEP_SEQUENCE[StepName.CREATE_GLUE_PIPELINE],
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pipeline_id": event.get("pipeline_id", "unknown"),  # For PipelineIndex GSI
                    "original_pipeline_id": event.get("pipeline_id", "unknown"),
                    "error_message": error_message,
                    "error_type": "GluePipelineError",
                }
            )
        except Exception as db_error:
            logger.error(f"Failed to write error to DynamoDB: {db_error!s}")

        # Fail loud (P28 R1.1): raise StepFailure so Step Functions sees the task fail
        # and routes to UpdateFailureState via createGluePipeline.addCatch. Returning a
        # 500 here makes the SFN task *succeed* with an error payload, letting the run
        # continue to RunPipeline which then fails with a misleading "Glue job name not
        # found". The failure is already recorded above (metrics + state table).
        raise StepFailure(
            "create_glue_pipeline",
            error_message,
            details={
                "run_id": event.get("run_id", "unknown"),
                "pipeline_id": event.get("pipeline_id", "unknown"),
            },
        ) from e
