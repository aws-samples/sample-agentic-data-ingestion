# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""S3/code/test artifact resolution and job-prerequisite validation helpers.

Extracted from ``lambda_function.py`` (P21 Req 4.2) as part of decomposing the
oversized create_glue_pipeline entrypoint into focused sibling modules. This
module owns source-path and warehouse-location resolution, generated-code and
test-result retrieval from the state table, S3-URI parsing, and the
comprehensive pre-job-creation prerequisite validation. The moves are
behavior-preserving (verbatim function bodies); ``lambda_function.py`` imports
these names back so the orchestration functions (``create_glue_job``,
``deploy_glue_pipeline_with_mcp``, ``lambda_handler``) keep working unchanged.

Dependency direction is one-way: ``lambda_function`` → ``artifacts``. This
module MUST NOT import ``lambda_function``.
"""

import os
import sys
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger(service="agentic_data_ingestion")

# Add shared directory to path for imports (Layer_Import_Fallback — /opt/python
# in the Lambda layer, else the local ../../shared path for local/test runs).
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))

from step_names import StepName

# `_get_glue_connection_tags_artifacts` used to live here and has been removed —
# see the equivalent note in `job_arguments.py`. Glue Connections are not a
# taggable resource type, so this always returned `{}` and the credential check
# it fed was decided entirely by `ConnectionProperties`.


# Import shared env_utils via the Layer_Import_Fallback (fix path + import the
# canonical resolver — do not redefine it inline; P21 Req 5.7).
try:
    from env_utils import get_aws_account_id, require_env
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from env_utils import get_aws_account_id, require_env

# Typed DynamoDB item accessors (P22 Req 2) — narrow boto3-stubs' wide
# AttributeValueTypeDef union to concrete types at the item read site.
# Deterministic, 64-char-bounded execution-role name (sibling module, no deps).
from naming import pipeline_execution_role_name

from dynamo_typing import item_dict, item_str

# Environment variables (mirror lambda_function.py so behavior is identical).
PIPELINE_RUNS_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
S3_TABLES_JAR_PATH = os.environ.get("S3_TABLES_JAR_PATH", "")

# Initialize AWS clients
# Convention: one module-level client per AWS service (see shared/README.md).
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
glue_client = boto3.client("glue")
iam_client = boto3.client("iam")
s3tables_client = boto3.client("s3tables")

# DynamoDB tables
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)


def get_source_path(run_id: str) -> str:
    """
    Resolve the S3 URI of the object the generated Glue job should read.

    **This is the ONE derivation of ``--source_path``** on the Glue-read path,
    asserted as such by ``tests/contracts/test_source_path_single_derivation.py``
    (p55 task 4.1, risk R5.2). Everything downstream passes this value through;
    nothing else re-derives it.

    It reads ``readable_source_uri`` off the **schema_extract** step's row —
    the value that step resolved once, in Python, and returned on both arms
    (p55 R13.3/R13.4, ``LGR-5c539a6-2``). That value already accounts for
    format: for every Native_Format_Set member it IS the original uploaded
    object, and for an Excel_Workbook it is the converted CSV that step wrote,
    because Spark has no Excel datasource. Deciding that here would be a second
    reader of the conversion gate, which ``LGR-28b4d75-1`` (derive once)
    forbids — hence no ``source_format`` parameter.

    **The removed ``source_format`` parameter (p55 task 4.1, finding 1).** This
    function used to take ``source_format: str = ""`` and its docstring claimed
    it returned the original file for a non-``json`` format and "falls back to
    ``json_s3_uri`` for backward compatibility". The body referenced
    ``source_format`` nowhere and never read ``json_s3_uri``; it returned
    ``original_s3_uri`` unconditionally, and ``lambda_function.py`` passed the
    file_validation row's ``file_type`` into a parameter that was ignored. So
    the docstring was not stale, it was wrong — a third possible reading of a
    function with two. Both are gone rather than edited around.

    The nested fallback is kept and is part of this ONE derivation: a
    ``schema_extract`` row written **before** p55 carries no
    ``readable_source_uri``, and for such a row the readable object is
    necessarily the original one, because no Conversion_Required_Set format
    existed then. That is risk R5.4's accepted cost handled as a Python
    fallback rather than as a Step Functions guard — task 4.4 measured 19
    replayable runs, and this path degrades to correct-by-construction instead
    of failing. It deliberately does NOT read ``original_s3_uri``: a preference
    between the two keys here is what ``LGR-28b4d75-1`` forbids, and the
    default-to-original behaviour belongs in ``schema_extract`` (R13.4).

    Args:
        run_id: Pipeline run identifier

    Returns:
        str: S3 URI of the object the Glue job should read

    Raises:
        ValueError: If no readable source URI can be resolved for this run
    """
    try:
        response = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": "schema_extract"})
        readable_uri = item_str(response.get("Item") or {}, "readable_source_uri")

        # Fallback for a pre-p55 run: the file_validation row's nested
        # output_data.s3_uris.source_file. See the docstring for why this is one
        # derivation rather than two, and why it is not `original_s3_uri`.
        if not readable_uri:
            fallback_response = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": "file_validation"})
            fallback_item = fallback_response.get("Item")
            if not fallback_item:
                raise ValueError(
                    f"No readable source URI on the schema_extract step and no file_validation "
                    f"step found for run_id={run_id}"
                )
            output_data = item_dict(fallback_item, "output_data")
            s3_uris = item_dict(output_data, "s3_uris")
            readable_uri = item_str(s3_uris, "source_file")

            if not readable_uri:
                raise ValueError(
                    f"No source URI found for run_id={run_id}: the schema_extract step carries no "
                    f"readable_source_uri and the file_validation step carries no nested "
                    f"output_data.s3_uris.source_file. Available file_validation fields: "
                    f"{list(fallback_item.keys())}"
                )

        logger.info(f"Resolved source_path for run_id={run_id}: {readable_uri}")
        return readable_uri

    except ClientError as e:
        logger.error(f"Error querying state table for source_path: {e}")
        raise ValueError(f"Failed to resolve source_path for run_id={run_id}: {e}")


def get_warehouse_location(run_id: str, target_config: dict[str, Any]) -> str:
    """
    Retrieve the S3 Tables warehouse location from the CreateInfrastructure step output
    in the state table, or from the S3 Tables API for existing buckets.

    Args:
        run_id: Pipeline run identifier
        target_config: Target configuration from pipeline metadata

    Returns:
        str: S3 Tables warehouse location URI

    Raises:
        ValueError: If warehouse_location cannot be resolved
    """
    warehouse_location = ""

    # 1. Try to get from CreateInfrastructure step output in state table
    try:
        infra_response = pipeline_runs_table.get_item(
            Key={"run_id": run_id, "step_name": StepName.CREATE_INFRASTRUCTURE}
        )
        infra_item = infra_response.get("Item", {})
        if infra_item:
            output_data = item_dict(infra_item, "output_data")
            # Check nested paths where warehouse_location might be stored
            warehouse_location = (
                item_str(item_dict(output_data, "s3_table_bucket"), "warehouse_location")
                or item_str(
                    item_dict(item_dict(output_data, "infrastructure_status"), "s3_table_bucket"),
                    "warehouse_location",
                )
                or item_str(output_data, "warehouse_location")
            )
            if warehouse_location:
                logger.info(f"Resolved warehouse_location from CreateInfrastructure step: {warehouse_location}")
                return warehouse_location
    except ClientError as e:
        logger.warning(f"Error querying CreateInfrastructure step: {e}")

    # 2. For existing S3 Table Buckets, query the S3 Tables API
    target_type = target_config.get("type", "").upper()
    if target_type in ["EXISTING_S3_TABLE_BUCKET", "S3_TABLES", "S3_TABLE_BUCKET", "NEW_S3_TABLE_BUCKET"]:
        bucket_name = target_config.get("bucket_name", "")
        namespace_raw = target_config.get("namespace", target_config.get("namespace_name", ""))
        namespace_name = namespace_raw[0] if isinstance(namespace_raw, list) and namespace_raw else namespace_raw
        table_name = target_config.get("table_name", "")

        if bucket_name and table_name:
            # If namespace is missing, try to find it by listing tables in the bucket
            if not namespace_name:
                try:
                    aws_region = os.environ.get("AWS_REGION", "us-east-1")
                    aws_account_id = get_aws_account_id()
                    table_bucket_arn = (
                        target_config.get("table_bucket_arn", "")
                        or f"arn:aws:s3tables:{aws_region}:{aws_account_id}:bucket/{bucket_name}"
                    )
                    tables_resp = s3tables_client.list_tables(tableBucketARN=table_bucket_arn)
                    for t in tables_resp.get("tables", []):
                        if t.get("name") == table_name:
                            ns = t.get("namespace", [])
                            namespace_name = ns[0] if isinstance(ns, list) and ns else ns
                            logger.info(
                                f"Resolved namespace '{namespace_name}' for table '{table_name}' via list_tables"
                            )
                            break
                except Exception as e:
                    logger.warning(f"Failed to resolve namespace via list_tables: {e}")

            if namespace_name:
                try:
                    aws_region = os.environ.get("AWS_REGION", "us-east-1")
                    aws_account_id = get_aws_account_id()
                    table_bucket_arn = (
                        target_config.get("table_bucket_arn", "")
                        or f"arn:aws:s3tables:{aws_region}:{aws_account_id}:bucket/{bucket_name}"
                    )

                    table_response = s3tables_client.get_table(
                        tableBucketARN=table_bucket_arn, namespace=namespace_name, name=table_name
                    )
                    warehouse_location = item_str(table_response, "warehouseLocation")
                    if warehouse_location:
                        logger.info(f"Resolved warehouse_location from S3 Tables API: {warehouse_location}")
                        return warehouse_location
                except ClientError as e:
                    logger.warning(f"Error querying S3 Tables API for warehouse_location: {e}")
                except Exception as e:
                    logger.warning(f"Unexpected error querying S3 Tables API: {e}")

    # 3. Check target_config directly as last resort
    warehouse_location = item_str(target_config, "warehouse_location")
    if warehouse_location:
        logger.info(f"Resolved warehouse_location from target_config: {warehouse_location}")
        return warehouse_location

    raise ValueError(
        f"warehouse_location could not be resolved for run_id={run_id}. "
        f"Checked: CreateInfrastructure step, S3 Tables API, target_config."
    )


def get_generated_code_from_pipeline_runs(pipeline_id: str, run_id: str) -> dict[str, Any]:
    """
    Retrieve generated PySpark script location and metadata from pipeline_runs_table.
    Queries step 5 (CodeGeneration) to get code_s3_uri.

    Args:
        pipeline_id: Pipeline identifier
        run_id: Run identifier

    Returns:
        dict: Code information including s3_uri, version, and metadata

    Raises:
        ValueError: If code generation step not found or failed
    """
    try:
        # Query pipeline_runs_table for step 5 (CodeGeneration)
        response = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": StepName.CREATE_CODE})

        if "Item" not in response:
            raise ValueError(f"CodeGeneration step not found for run_id {run_id}")

        item = response["Item"]

        # Check if step succeeded
        status = item_str(item, "status")
        if status != "success":
            raise ValueError(f"CodeGeneration step failed with status: {status}")

        # Extract output data
        output_data = item_dict(item, "output_data")
        code_s3_uri = item_str(output_data, "code_s3_uri") or item_str(output_data, "s3_location")

        if not code_s3_uri:
            raise ValueError("code_s3_uri not found in CodeGeneration step output")

        # Validate S3 URI exists
        try:
            bucket, key = parse_s3_uri(code_s3_uri)
            s3_client.head_object(Bucket=bucket, Key=key)
            logger.info(f"Validated PySpark script exists at: {code_s3_uri}")
        except Exception as e:
            logger.error(f"PySpark script not accessible at {code_s3_uri}: {e}")
            raise ValueError(f"Generated PySpark script not found at {code_s3_uri}")

        code_info = {
            "code_s3_uri": code_s3_uri,
            "code_version": output_data.get("code_version", "1.0"),
            "generated_at": item_str(item, "timestamp"),
            "model_used": output_data.get("model_used", "unknown"),
            "generation_method": output_data.get("generation_method", "agent"),
            "script_size_bytes": output_data.get("script_size_bytes", 0),
        }

        logger.info(f"Retrieved generated code info: {code_info}")
        return code_info

    except Exception as e:
        logger.error(f"Error retrieving generated code from pipeline_runs_table: {e}")
        raise


def get_test_results_from_pipeline_runs(pipeline_id: str, run_id: str) -> dict[str, Any]:
    """
    Retrieve test results and recommendations from pipeline_runs_table.
    Queries step 6 (CodeTesting) to get test results and optimization recommendations.

    Args:
        pipeline_id: Pipeline identifier
        run_id: Run identifier

    Returns:
        dict: Test results including status, recommendations, and performance hints
    """
    try:
        # Query pipeline_runs_table for step 6 (CodeTesting)
        response = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": StepName.TEST_CODE})

        if "Item" not in response:
            logger.warning(f"CodeTesting step not found for run_id {run_id}, proceeding without test recommendations")
            return {"test_status": "not_run", "recommendations": [], "performance_hints": {}}

        item = response["Item"]
        output_data = item_dict(item, "output_data")

        # Extract test results
        test_results: dict[str, Any] = {
            "test_status": item.get("status", "unknown"),
            "test_passed": item.get("status") == "success",
            "tested_at": item.get("timestamp", ""),
            "test_duration_seconds": output_data.get("test_duration_seconds", 0),
            "test_output": output_data.get("test_output", ""),
            "validation_errors": output_data.get("validation_errors", []),
            "recommendations": [],
            "performance_hints": {},
        }

        # Extract recommendations from test output
        # Test recommendations may include: memory settings, partition counts, optimization hints
        recommendations_raw = output_data.get("recommendations", [])
        if isinstance(recommendations_raw, list):
            test_results["recommendations"] = recommendations_raw
        elif isinstance(recommendations_raw, str):
            # Parse string recommendations
            test_results["recommendations"] = [r.strip() for r in recommendations_raw.split("\n") if r.strip()]

        # Extract performance hints
        performance_hints = output_data.get("performance_hints", {})
        if isinstance(performance_hints, dict):
            test_results["performance_hints"] = performance_hints

        # Extract specific optimization suggestions
        if "memory_recommendation" in output_data:
            test_results["performance_hints"]["memory_gb"] = output_data["memory_recommendation"]

        if "partition_recommendation" in output_data:
            test_results["performance_hints"]["partition_count"] = output_data["partition_recommendation"]

        if "worker_recommendation" in output_data:
            test_results["performance_hints"]["worker_type"] = output_data["worker_recommendation"]

        logger.info(
            f"Retrieved test results: status={test_results['test_status']}, "
            f"recommendations={len(test_results['recommendations'])}, "
            f"hints={len(test_results['performance_hints'])}"
        )

        return test_results

    except Exception as e:
        logger.error(f"Error retrieving test results from pipeline_runs_table: {e}")
        # Return empty results rather than failing
        return {
            "test_status": "error",
            "test_passed": False,
            "recommendations": [],
            "performance_hints": {},
            "error": str(e),
        }


def parse_s3_uri(s3_uri: str) -> tuple:
    """
    Parse S3 URI into bucket and key components.

    Args:
        s3_uri: S3 URI in format s3://bucket/key

    Returns:
        tuple: (bucket, key)
    """
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {s3_uri}")

    parts = s3_uri[5:].split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid S3 URI format: {s3_uri}")

    return parts[0], parts[1]


def validate_s3_tables_jar_accessible() -> bool:
    """
    Validate that S3 Tables JAR is accessible before job creation.
    This JAR is CRITICAL for S3 Tables with Iceberg format support.

    Returns:
        bool: True if JAR is accessible, False otherwise
    """
    try:
        if not S3_TABLES_JAR_PATH:
            logger.warning("S3_TABLES_JAR_PATH environment variable not set")
            return False

        logger.info(f"Validating S3 Tables JAR accessibility: {S3_TABLES_JAR_PATH}")

        # Parse S3 URI
        bucket, key = parse_s3_uri(S3_TABLES_JAR_PATH)

        # Check if JAR exists
        s3_client.head_object(Bucket=bucket, Key=key)

        logger.info(f"✓ S3 Tables JAR validated: {S3_TABLES_JAR_PATH}")
        return True

    except Exception as e:
        logger.error(f"✗ S3 Tables JAR not accessible: {e}")
        logger.error(f"JAR path: {S3_TABLES_JAR_PATH}")
        logger.error("This JAR is CRITICAL for S3 Tables support. Please run deploy/upload-s3-tables-jar.sh")
        return False


def validate_job_prerequisites(
    pipeline_id: str,
    run_id: str,
    script_location: str,
    target_type: str,
    s3_table_bucket: dict[str, Any] | None = None,
    target_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Comprehensive validation of all prerequisites before Glue job creation.

    Validates:
    - Script location exists and is accessible
    - IAM role exists and has required permissions
    - Target configuration is valid
    - S3 bucket/table is accessible (for S3 targets)
    - Database connection exists (for JDBC targets)
    - Glue database exists
    - S3 Tables JAR is accessible (for S3 Table Bucket targets)

    Args:
        pipeline_id: Pipeline identifier
        run_id: Run identifier
        script_location: S3 URI of PySpark script
        target_type: Type of target (S3_TABLE_BUCKET, GLUE_CATALOG, JDBC)
        s3_table_bucket: S3 table bucket configuration (optional)
        target_config: Target configuration from pipeline metadata (optional)

    Returns:
        dict: Validation results with status and detailed error messages
    """
    validation_results: dict[str, Any] = {
        "valid": False,
        "script_exists": False,
        "script_accessible": False,
        "iam_role_valid": False,
        "iam_permissions_valid": False,
        "target_config_valid": False,
        "bucket_accessible": False,
        "database_exists": False,
        "jar_accessible": False,
        "jdbc_connection_valid": False,
        "errors": [],
        "warnings": [],
        "recommendations": [],
    }

    logger.info(f"Starting comprehensive validation for pipeline {pipeline_id}")

    # 1. Validate script location exists and is accessible
    try:
        if not script_location:
            validation_results["errors"].append("Script location is empty or not provided")
        else:
            logger.info(f"Validating script location: {script_location}")
            bucket, key = parse_s3_uri(script_location)

            # Try to check if script exists
            try:
                s3_client.head_object(Bucket=bucket, Key=key)
                validation_results["script_exists"] = True

                # Try to read metadata
                try:
                    response = s3_client.get_object_attributes(
                        Bucket=bucket, Key=key, ObjectAttributes=["ObjectSize", "ETag"]
                    )
                    script_size = response.get("ObjectSize", 0)

                    if script_size == 0:
                        validation_results["warnings"].append(f"Script file is empty: {script_location}")
                    elif script_size < 100:
                        validation_results["warnings"].append(
                            f"Script file is very small ({script_size} bytes), may be incomplete"
                        )
                    else:
                        validation_results["script_accessible"] = True
                        logger.info(f"✓ Script validated: {script_location} ({script_size} bytes)")
                except ClientError:
                    # If we can't get attributes but head_object worked, assume it's accessible
                    validation_results["script_accessible"] = True
                    validation_results["warnings"].append(
                        f"Script exists but metadata not accessible: {script_location}"
                    )
                    logger.info(f"✓ Script exists (metadata check skipped): {script_location}")

            except ClientError as e:
                error_code = e.response["Error"]["Code"]
                if error_code == "NoSuchKey":
                    validation_results["errors"].append(f"Script not found at location: {script_location}")
                    validation_results["recommendations"].append(
                        "Ensure the CodeGeneration step completed successfully"
                    )
                elif error_code == "NoSuchBucket":
                    validation_results["errors"].append(f"Script bucket does not exist: {bucket}")
                    validation_results["recommendations"].append("Check S3 bucket configuration")
                elif error_code == "AccessDenied" or error_code == "403":
                    # Treat access denied as warning - Glue job role will have access
                    validation_results["warnings"].append(
                        f"Lambda role cannot access script (Glue job role will have access): {script_location}"
                    )
                    validation_results["recommendations"].append(
                        "Ensure Glue job execution role has s3:GetObject permission"
                    )
                    # Mark as accessible since Glue will use different role
                    validation_results["script_exists"] = True
                    validation_results["script_accessible"] = True
                    logger.info(f"⚠ Script access denied for Lambda role (expected): {script_location}")
                else:
                    validation_results["warnings"].append(f"Script validation failed: {e}")
                    validation_results["recommendations"].append("Script will be validated by Glue job at runtime")
                    # Don't fail validation - let Glue job try
                    validation_results["script_exists"] = True
                    validation_results["script_accessible"] = True

    except Exception as e:
        validation_results["warnings"].append(f"Script validation error: {e!s}")
        validation_results["recommendations"].append("Script will be validated by Glue job at runtime")
        # Don't fail validation
        validation_results["script_exists"] = True
        validation_results["script_accessible"] = True

    # 2. Validate IAM role exists and has required permissions
    try:
        logger.info(f"Validating IAM role for pipeline {pipeline_id}")
        role_name = pipeline_execution_role_name(pipeline_id)

        # Check if role exists
        try:
            role_response = iam_client.get_role(RoleName=role_name)
            validation_results["iam_role_valid"] = True
            logger.info(f"✓ IAM role exists: {role_name}")

            # Check role policies
            attached_policies = iam_client.list_attached_role_policies(RoleName=role_name)
            inline_policies = iam_client.list_role_policies(RoleName=role_name)

            if not attached_policies.get("AttachedPolicies") and not inline_policies.get("PolicyNames"):
                validation_results["warnings"].append(f"IAM role {role_name} has no policies attached")
                validation_results["recommendations"].append(
                    "Ensure role has required S3, Glue, and CloudWatch permissions"
                )
            else:
                validation_results["iam_permissions_valid"] = True
                logger.info(f"✓ IAM role has {len(attached_policies.get('AttachedPolicies', []))} attached policies")

        except iam_client.exceptions.NoSuchEntityException:
            validation_results["warnings"].append(f"IAM role {role_name} does not exist yet")
            validation_results["recommendations"].append("Role will be created automatically during job creation")
            # This is not a critical error as role will be created
            validation_results["iam_role_valid"] = True

    except Exception as e:
        validation_results["errors"].append(f"IAM role validation error: {e!s}")

    # 3. Validate target configuration
    try:
        logger.info(f"Validating target configuration for type: {target_type}")

        if not target_config:
            # If target_config not provided but s3_table_bucket is, that's acceptable
            if s3_table_bucket and s3_table_bucket.get("bucket_name"):
                validation_results["warnings"].append("Target configuration not provided, using s3_table_bucket data")
                validation_results["target_config_valid"] = True
            else:
                validation_results["warnings"].append("Target configuration not provided, using defaults")
                validation_results["target_config_valid"] = True  # Not critical
        else:
            # Validate based on target type
            if target_type in [
                "S3_TABLE_BUCKET",
                "S3 Table Bucket",
                "S3_TABLES",
                "NEW_S3_TABLE_BUCKET",
                "EXISTING_S3_TABLE_BUCKET",
            ]:
                # Check if we have either target_config fields OR s3_table_bucket data
                has_bucket_name = target_config.get("bucket_name") or (
                    s3_table_bucket and s3_table_bucket.get("bucket_name")
                )
                has_table_name = target_config.get("table_name") or (
                    s3_table_bucket and s3_table_bucket.get("table_name")
                )

                if not has_bucket_name or not has_table_name:
                    validation_results["warnings"].append(
                        "S3 Table Bucket target configuration incomplete, will use infrastructure output"
                    )
                    validation_results["recommendations"].append(
                        "Ensure CreateInfrastructure step provided s3_table_bucket data"
                    )

                # Mark as valid if we have s3_table_bucket data
                if s3_table_bucket and s3_table_bucket.get("bucket_name"):
                    validation_results["target_config_valid"] = True
                    logger.info("✓ S3 Table Bucket target configuration is valid (using infrastructure output)")
                else:
                    validation_results["target_config_valid"] = True
                    validation_results["warnings"].append("S3 Table Bucket configuration will be determined at runtime")
                    logger.info("✓ S3 Table Bucket target configuration accepted with warnings")

            elif target_type in ["GLUE_CATALOG", "Glue Catalog"]:
                required_fields = ["type", "glue_database", "glue_table", "format"]
                missing_fields = [f for f in required_fields if f not in target_config]

                if missing_fields:
                    validation_results["errors"].append(
                        f"Glue Catalog target missing required fields: {', '.join(missing_fields)}"
                    )
                else:
                    validation_results["target_config_valid"] = True
                    logger.info("✓ Glue Catalog target configuration is valid")

            elif target_type in ["JDBC", "JDBC Database"]:
                required_fields = ["type", "jdbc_connection_name", "database_name", "table_name"]
                missing_fields = [f for f in required_fields if f not in target_config]

                if missing_fields:
                    validation_results["errors"].append(
                        f"JDBC target missing required fields: {', '.join(missing_fields)}"
                    )
                else:
                    validation_results["target_config_valid"] = True
                    logger.info("✓ JDBC target configuration is valid")
            else:
                validation_results["target_config_valid"] = True
                validation_results["warnings"].append(
                    f"Unknown target type: {target_type}, skipping specific validation"
                )

    except Exception as e:
        validation_results["errors"].append(f"Target configuration validation error: {e!s}")

    # 4. Validate S3 bucket accessibility (for S3 targets)
    if target_type in [
        "S3_TABLE_BUCKET",
        "S3 Table Bucket",
        "S3_TABLES",
        "NEW_S3_TABLE_BUCKET",
        "EXISTING_S3_TABLE_BUCKET",
        "GLUE_CATALOG",
        "Glue Catalog",
    ]:
        try:
            if s3_table_bucket and s3_table_bucket.get("bucket_name"):
                bucket_name = s3_table_bucket["bucket_name"]
                logger.info(f"Validating S3 bucket accessibility: {bucket_name}")

                # Check if bucket exists and is accessible
                # For S3 Tables, use the S3 Tables API instead of standard S3 API
                if target_type in [
                    "S3_TABLE_BUCKET",
                    "S3 Table Bucket",
                    "S3_TABLES",
                    "NEW_S3_TABLE_BUCKET",
                    "EXISTING_S3_TABLE_BUCKET",
                ]:
                    try:
                        # Use S3 Tables API to validate the bucket
                        response = s3tables_client.get_table_bucket(
                            tableBucketARN=f"arn:aws:s3tables:{AWS_REGION}:{get_aws_account_id()}:bucket/{bucket_name}"
                        )
                        validation_results["bucket_accessible"] = True
                        logger.info(f"✓ S3 Table Bucket accessible: {bucket_name}")
                    except ClientError as bucket_error:
                        error_code = bucket_error.response["Error"]["Code"]
                        if error_code in ["ResourceNotFoundException", "NoSuchBucket"]:
                            validation_results["errors"].append(f"S3 Table Bucket does not exist: {bucket_name}")
                            validation_results["recommendations"].append(
                                "Ensure CreateInfrastructure step completed successfully and created the S3 Table Bucket"
                            )
                        elif error_code in ["AccessDeniedException", "Forbidden"]:
                            validation_results["errors"].append(f"Access denied to S3 Table Bucket: {bucket_name}")
                            validation_results["recommendations"].append(
                                "Check IAM permissions for s3tables:GetTableBucket action"
                            )
                        else:
                            # Unknown error, log as warning but don't fail
                            validation_results["warnings"].append(
                                f"Could not validate S3 Table Bucket {bucket_name}: {error_code}"
                            )
                            validation_results["bucket_accessible"] = True  # Assume accessible
                            logger.warning(f"⚠ S3 Table Bucket validation returned {error_code}: {bucket_name}")
                    except Exception as e:
                        # If S3 Tables API is not available or other error, log as warning
                        validation_results["warnings"].append(
                            f"Could not validate S3 Table Bucket {bucket_name} (S3 Tables API may not be available)"
                        )
                        validation_results["bucket_accessible"] = True  # Assume accessible
                        logger.warning(f"⚠ S3 Table Bucket validation failed: {e!s}")
                else:
                    # For regular S3 buckets (Glue Catalog targets), use standard S3 API
                    try:
                        s3_client.head_bucket(Bucket=bucket_name)
                        validation_results["bucket_accessible"] = True
                        logger.info(f"✓ S3 bucket accessible: {bucket_name}")
                    except ClientError as bucket_error:
                        error_code = bucket_error.response["Error"]["Code"]
                        if error_code in ["404", "NoSuchBucket"]:
                            validation_results["errors"].append(f"S3 bucket does not exist: {bucket_name}")
                        elif error_code in ["403", "Forbidden", "AccessDenied"]:
                            validation_results["errors"].append(f"Access denied to S3 bucket: {bucket_name}")
                        else:
                            raise bucket_error

                # Check if table location exists (if provided)
                if s3_table_bucket.get("metadata_location"):
                    metadata_location = s3_table_bucket["metadata_location"]
                    try:
                        bucket, key = parse_s3_uri(metadata_location)
                        s3_client.head_object(Bucket=bucket, Key=key)
                        logger.info(f"✓ Table metadata accessible: {metadata_location}")
                    except ClientError:
                        validation_results["warnings"].append(f"Table metadata not found at: {metadata_location}")
                        validation_results["recommendations"].append("Metadata will be created during job execution")
            else:
                validation_results["warnings"].append("S3 bucket information not provided")
                validation_results["bucket_accessible"] = True  # Not critical if not provided

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            http_status = e.response["ResponseMetadata"].get("HTTPStatusCode", 0)
            # S3 Tables buckets return 403 when accessed via standard S3 API - this is expected
            if error_code in ["403", "Forbidden"] or http_status == 403:
                validation_results["warnings"].append(
                    f"S3 Table Bucket {bucket_name} not accessible via standard S3 API (expected for S3 Tables)"
                )
                validation_results["bucket_accessible"] = True  # S3 Tables use different API
                logger.info(f"⚠ S3 Table Bucket validation returned 403 (normal for S3 Tables): {bucket_name}")
            elif error_code == "NoSuchBucket":
                validation_results["errors"].append(f"S3 bucket does not exist: {bucket_name}")
                validation_results["recommendations"].append("Ensure CreateInfrastructure step completed successfully")
            elif error_code == "AccessDenied":
                validation_results["errors"].append(f"Access denied to S3 bucket: {bucket_name}")
                validation_results["recommendations"].append(
                    "Check IAM permissions for Lambda and Glue execution roles"
                )
            else:
                validation_results["errors"].append(f"S3 bucket validation error: {e}")
        except Exception as e:
            validation_results["errors"].append(f"S3 bucket validation error: {e!s}")
    else:
        validation_results["bucket_accessible"] = True  # Not applicable for JDBC targets

    # 5. Validate Glue database exists
    try:
        database_name = f"agentic_data_ingestion_{pipeline_id.lower().replace('-', '_')}"
        logger.info(f"Validating Glue database: {database_name}")

        try:
            glue_client.get_database(Name=database_name)
            validation_results["database_exists"] = True
            logger.info(f"✓ Glue database exists: {database_name}")
        except glue_client.exceptions.EntityNotFoundException:
            validation_results["warnings"].append(f"Glue database {database_name} does not exist yet")
            validation_results["recommendations"].append("Database will be created automatically during job creation")
            # This is not a critical error as database will be created
            validation_results["database_exists"] = True

    except Exception as e:
        validation_results["errors"].append(f"Glue database validation error: {e!s}")

    # 6. Validate S3 Tables JAR accessibility (for S3 Table Bucket targets)
    if target_type in [
        "S3_TABLE_BUCKET",
        "S3 Table Bucket",
        "S3_TABLES",
        "NEW_S3_TABLE_BUCKET",
        "EXISTING_S3_TABLE_BUCKET",
    ]:
        logger.info("Validating S3 Tables JAR for Iceberg support")
        jar_accessible = validate_s3_tables_jar_accessible()

        if not jar_accessible:
            # Treat as warning instead of error - job can still be created
            validation_results["warnings"].append("S3 Tables JAR is not accessible - job may fail at runtime")
            validation_results["recommendations"].append(
                "Run deploy/upload-s3-tables-jar.sh to upload the required JAR file"
            )
            validation_results["recommendations"].append(
                "Set S3_TABLES_JAR_PATH environment variable to the JAR location"
            )
            # Still mark as accessible to not block job creation
            validation_results["jar_accessible"] = True
        else:
            validation_results["jar_accessible"] = True
    else:
        validation_results["jar_accessible"] = True  # Not applicable for non-S3 Tables targets

    # 7. Validate JDBC connection (for JDBC targets)
    if target_type in ["JDBC", "JDBC Database"] and target_config:
        try:
            connection_name = target_config.get("jdbc_connection_name")
            if connection_name:
                logger.info(f"Validating JDBC connection: {connection_name}")

                try:
                    connection = glue_client.get_connection(Name=connection_name)
                    validation_results["jdbc_connection_valid"] = True
                    logger.info(f"✓ JDBC connection exists: {connection_name}")

                    # Validate connection properties
                    conn = connection["Connection"]
                    conn_props = conn.get("ConnectionProperties", {})
                    if not conn_props.get("JDBC_CONNECTION_URL"):
                        validation_results["warnings"].append(
                            f"JDBC connection {connection_name} missing JDBC_CONNECTION_URL"
                        )
                    # Credentials are either inline USERNAME/PASSWORD or a
                    # SECRET_ID reference — Glue rejects both together.
                    #
                    # The `adi:secret_arn` tag is not consulted: Glue Connections
                    # are not taggable, so the `GetTags` call this used to make
                    # always raised and the helper always returned `{}`. Dropping
                    # it is behaviour-preserving and removes a doomed AWS call
                    # from artifact validation.
                    has_inline_creds = conn_props.get("USERNAME") and conn_props.get("PASSWORD")
                    has_secret = bool(conn_props.get("SECRET_ID"))
                    if not has_inline_creds and not has_secret:
                        validation_results["warnings"].append(f"JDBC connection {connection_name} missing credentials")

                except glue_client.exceptions.EntityNotFoundException:
                    validation_results["errors"].append(f"JDBC connection not found: {connection_name}")
                    validation_results["recommendations"].append(
                        "Create JDBC connection in Glue Data Catalog before creating job"
                    )
            else:
                validation_results["warnings"].append("JDBC connection name not provided in target configuration")

        except Exception as e:
            validation_results["errors"].append(f"JDBC connection validation error: {e!s}")
    else:
        validation_results["jdbc_connection_valid"] = True  # Not applicable for non-JDBC targets

    # 8. Overall validation status
    validation_results["valid"] = all(
        [
            validation_results["script_exists"],
            validation_results["script_accessible"],
            validation_results["iam_role_valid"],
            validation_results["target_config_valid"],
            validation_results["bucket_accessible"],
            validation_results["database_exists"],
            validation_results["jar_accessible"],
            validation_results["jdbc_connection_valid"],
        ]
    )

    # Log summary
    if validation_results["valid"]:
        logger.info("✓ All validation checks passed")
    else:
        logger.error(f"✗ Validation failed with {len(validation_results['errors'])} errors")
        for error in validation_results["errors"]:
            logger.error(f"  - {error}")

    if validation_results["warnings"]:
        logger.warning(f"Validation completed with {len(validation_results['warnings'])} warnings")
        for warning in validation_results["warnings"]:
            logger.warning(f"  - {warning}")

    if validation_results["recommendations"]:
        logger.info(f"Validation recommendations ({len(validation_results['recommendations'])}):")
        for rec in validation_results["recommendations"]:
            logger.info(f"  - {rec}")

    return validation_results


def get_generated_code_location(pipeline_id: str, run_id: str) -> str:
    """
    Get the S3 location of the generated PySpark code.
    This is a wrapper around get_generated_code_from_pipeline_runs for backward compatibility.

    Args:
        pipeline_id: Pipeline identifier
        run_id: Run identifier

    Returns:
        str: S3 URI of generated code
    """
    try:
        code_info = get_generated_code_from_pipeline_runs(pipeline_id, run_id)
        return item_str(code_info, "code_s3_uri")
    except Exception as e:
        logger.error(f"Error getting generated code location: {e}")
        # Fallback to default location
        bucket_name = f"agentic-data-ingestion-pipeline-artifacts-{get_aws_account_id()}"
        return f"s3://{bucket_name}/generated-code/{pipeline_id}/{run_id}/etl_script.py"
