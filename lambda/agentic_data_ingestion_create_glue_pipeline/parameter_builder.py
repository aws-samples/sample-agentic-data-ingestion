# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Parameter Builder for Glue Jobs
Builds DefaultArguments for Glue jobs based on generated code requirements
"""

import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")


def get_code_requirements(pipeline_id: str, generated_code_table_name: str) -> dict[str, Any]:
    """
    Retrieve the parameter requirements from the generated code metadata

    Args:
        pipeline_id: Pipeline identifier
        generated_code_table_name: Name of the generated code DynamoDB table

    Returns:
        Dictionary containing required_params, optional_params, and other metadata
    """
    try:
        table = dynamodb.Table(generated_code_table_name)

        # Query for the latest code generation for this pipeline
        response = table.query(
            IndexName="pipeline_id-index",
            KeyConditionExpression="pipeline_id = :pid",
            ExpressionAttributeValues={":pid": pipeline_id},
            ScanIndexForward=False,  # Get latest first
            Limit=1,
        )

        if not response.get("Items"):
            logger.warning(f"No generated code found for pipeline {pipeline_id}")
            return {}

        item = response["Items"][0]

        return {
            "required_params": item.get("required_parameters", item.get("required_params", [])),
            "optional_params": item.get("optional_parameters", item.get("optional_params", [])),
            "s3_path": item.get("s3_path", ""),
            "code_hash": item.get("code_hash", ""),
            "timestamp": item.get("timestamp", ""),
        }

    except ClientError as e:
        logger.error(f"Error retrieving code requirements: {e}")
        return {}


def build_parameter_values(
    pipeline_id: str,
    required_params: list[str],
    optional_params: list[str],
    pipeline_metadata: dict[str, Any],
    target_config: dict[str, Any],
    s3_table_bucket_name: str = None,
) -> dict[str, str]:
    """
    Build actual parameter values based on pipeline configuration

    Args:
        pipeline_id: Pipeline identifier
        required_params: List of required parameter names from generated code
        optional_params: List of optional parameter names from generated code
        pipeline_metadata: Pipeline configuration from DynamoDB
        target_config: Target configuration (buckets, tables, etc.)
        s3_table_bucket_name: Actual S3 Table Bucket name (if applicable)

    Returns:
        Dictionary of parameter names to values
    """
    params = {}

    # Determine target path based on target type
    def get_target_path():
        target_type = target_config.get("type", "").upper()
        if target_type in ["S3_TABLES", "S3_TABLE_BUCKET", "NEW_S3_TABLE_BUCKET", "EXISTING_S3_TABLE_BUCKET"]:
            # For S3 Table Buckets, use the actual created bucket name
            # Priority: s3_table_bucket_name parameter > target_config.bucket_name
            bucket_name = s3_table_bucket_name or target_config.get("bucket_name", "")
            return f"s3://{bucket_name}/" if bucket_name else ""
        else:
            # For other targets, use the configured path. The React wizard sends
            # `s3_path` for S3_PARQUET; older configs use target_path/output_path.
            # Accept all so the resolved path is never silently empty.
            return (
                target_config.get("s3_path")
                or target_config.get("target_path")
                or target_config.get("output_path")
                or ""
            )

    # Map parameter names to actual values
    param_mapping = {
        "SOURCE_S3_PATH": lambda: (
            f"s3://{pipeline_metadata.get('processed_bucket', '')}/{pipeline_id}/validated/*.json"
        ),
        "TARGET_S3_PATH": get_target_path,
        "SOURCE_FORMAT": lambda: pipeline_metadata.get("source_format", "json"),
        "TARGET_FORMAT": lambda: target_config.get("target_format", "parquet"),
        "SOURCE_DATABASE": lambda: pipeline_metadata.get("source_database", ""),
        "SOURCE_TABLE": lambda: pipeline_metadata.get("source_table", ""),
        "TARGET_DATABASE": lambda: target_config.get("catalog_database", ""),
        "TARGET_TABLE": lambda: target_config.get("table_name", target_config.get("catalog_table", "")),
        "TARGET_NAMESPACE": lambda: target_config.get("namespace_name", ""),
        "PARTITION_KEYS": lambda: str(target_config.get("partition_keys", [])),
        "WRITE_MODE": lambda: target_config.get("write_mode", "append"),
        "COMPRESSION": lambda: target_config.get("compression", "snappy"),
        "ENABLE_GLUE_CATALOG": lambda: str(target_config.get("enable_catalog", True)),
        "ENABLE_CONTINUOUS_LOGGING": lambda: "true",
        "ENABLE_METRICS": lambda: "true",
        "AWS_REGION": lambda: pipeline_metadata.get("region", "us-east-1"),
        "TEMP_DIR": lambda: f"s3://{pipeline_metadata.get('temp_bucket', '')}/glue-temp/",
    }

    # Build required parameters
    for param in required_params:
        if param == "JOB_NAME":
            continue  # JOB_NAME is provided by Glue automatically

        if param in param_mapping:
            value = param_mapping[param]()
            if value:  # Only add if value is not empty
                params[f"--{param}"] = value
            else:
                logger.warning(f"Required parameter {param} has no value")
        else:
            logger.warning(f"Unknown required parameter: {param}")

    # Build optional parameters
    for param in optional_params:
        if param in param_mapping:
            value = param_mapping[param]()
            if value:  # Only add if value is not empty
                params[f"--{param}"] = value

    return params


def build_glue_job_parameters(
    pipeline_id: str,
    generated_code_table_name: str,
    pipeline_metadata: dict[str, Any],
    target_config: dict[str, Any],
    additional_params: dict[str, str] = None,
) -> dict[str, str]:
    """
    Build complete DefaultArguments for Glue job based on generated code requirements

    Args:
        pipeline_id: Pipeline identifier
        generated_code_table_name: Name of the generated code DynamoDB table
        pipeline_metadata: Pipeline configuration from DynamoDB
        target_config: Target configuration (buckets, tables, etc.)
        additional_params: Additional parameters to merge (e.g., Iceberg-specific)

    Returns:
        Complete DefaultArguments dictionary for Glue job
    """
    # Get code requirements
    code_reqs = get_code_requirements(pipeline_id, generated_code_table_name)

    if not code_reqs:
        logger.warning(f"No code requirements found for {pipeline_id}, using defaults")
        required_params = ["SOURCE_S3_PATH", "TARGET_S3_PATH", "SOURCE_FORMAT", "TARGET_FORMAT"]
        optional_params = ["PARTITION_KEYS", "WRITE_MODE", "COMPRESSION"]
    else:
        required_params = code_reqs.get("required_params", [])
        optional_params = code_reqs.get("optional_params", [])
        logger.info(f"Building parameters for {pipeline_id}: required={required_params}, optional={optional_params}")

    # Extract S3 Table Bucket name from additional_params if present
    s3_table_bucket_name = None
    if additional_params:
        s3_table_bucket_name = additional_params.get("--target-bucket-name")

    # Build parameter values
    params = build_parameter_values(
        pipeline_id, required_params, optional_params, pipeline_metadata, target_config, s3_table_bucket_name
    )

    # Add standard Glue parameters (not code-related, but operational)
    standard_params = {
        "--enable-continuous-cloudwatch-log": "true",
        "--enable-metrics": "true",
        "--enable-spark-ui": "true",
        "--enable-auto-scaling": "true",
        "--job-bookmark-option": "job-bookmark-enable",
        "--job-language": "python",
        "--pipeline-id": pipeline_id,
        # Performance tuning defaults
        "--COALESCE_PARTITIONS": "false",
        "--SHUFFLE_PARTITIONS": "200",
        "--DATA_QUALITY_THRESHOLD": "0.95",
        "--MAX_ERROR_RATE": "0.05",
        # Security defaults
        "--ENCRYPTION_TYPE": "none",
        "--KMS_KEY_ID": "",
        "--SECRETS_MANAGER_SECRET": "",
        # Monitoring
        "--METRICS_NAMESPACE": "AgenticDataIngestion/Pipeline",
        # Incremental processing
        "--INCREMENTAL_MODE": "false",
        "--BOOKMARK_KEY": "",
    }
    params.update(standard_params)

    # Merge additional parameters (e.g., Iceberg-specific)
    if additional_params:
        params.update(additional_params)

    logger.info(f"Built {len(params)} parameters for Glue job")

    return params


def validate_parameters(params: dict[str, str], required_params: list[str]) -> tuple[bool, list[str]]:
    """
    Validate that all required parameters are present and have values

    Args:
        params: Dictionary of parameter names to values
        required_params: List of required parameter names

    Returns:
        Tuple of (is_valid, list_of_missing_params)
    """
    missing = []

    for param in required_params:
        if param == "JOB_NAME":
            continue  # JOB_NAME is provided by Glue

        param_key = f"--{param}"
        if param_key not in params:
            missing.append(param)
        elif not params[param_key] or params[param_key] == "":
            missing.append(f"{param} (empty value)")

    return len(missing) == 0, missing
