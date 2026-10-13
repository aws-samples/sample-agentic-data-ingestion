# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Job Update Handler

Integrates schema evolution logic into the Glue job creation/update workflow.
Handles checking for existing jobs and determining whether to update or create new versions.
"""

import logging
import os
import sys
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Add shared directory to path for imports
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))

# Import shared env_utils via the Layer_Import_Fallback (fix path + import the
# canonical resolver — do not redefine it inline; P21 Req 5.3/5.4/5.7).
try:
    from env_utils import get_aws_account_id
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from env_utils import get_aws_account_id

# Deterministic, 64-char-bounded execution-role ARN (sibling module, no deps).
from naming import pipeline_execution_role_arn

# Initialize AWS clients
glue_client = boto3.client("glue")


def get_existing_job_and_schema(pipeline_id: str, pipelines_table) -> tuple[str | None, dict[str, Any] | None]:
    """
    Get existing Glue job name and schema for a pipeline.

    Args:
        pipeline_id: Pipeline identifier
        pipelines_table: DynamoDB pipelines table resource

    Returns:
        tuple: (job_name, schema) or (None, None) if no existing job
    """
    try:
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            logger.info(f"No existing pipeline found for {pipeline_id}")
            return None, None

        item = response["Item"]

        # Get Glue job metadata
        glue_metadata = item.get("glue_job_metadata", {})
        job_name = glue_metadata.get("job_name")

        if not job_name:
            logger.info(f"No Glue job found for pipeline {pipeline_id}")
            return None, None

        # Check if job actually exists in Glue
        try:
            glue_client.get_job(JobName=job_name)
            logger.info(f"Found existing Glue job: {job_name}")
        except glue_client.exceptions.EntityNotFoundException:
            logger.warning(f"Job {job_name} referenced in metadata but not found in Glue")
            return None, None

        # Get previous schema
        target_config = item.get("target_config", {})
        previous_schema = target_config.get("schema", {})

        if not previous_schema:
            logger.warning(f"No previous schema found for pipeline {pipeline_id}")
            return job_name, None

        return job_name, previous_schema

    except Exception as e:
        logger.error(f"Error getting existing job and schema: {e}")
        return None, None


def should_use_schema_evolution(
    pipeline_id: str, new_schema: dict[str, Any], pipelines_table
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """
    Determine if schema evolution should be used for this pipeline.

    Args:
        pipeline_id: Pipeline identifier
        new_schema: New schema definition
        pipelines_table: DynamoDB pipelines table resource

    Returns:
        tuple: (use_evolution, existing_job_name, old_schema)
    """
    try:
        # Get existing job and schema
        existing_job_name, old_schema = get_existing_job_and_schema(pipeline_id, pipelines_table)

        # If no existing job, create new one (no evolution needed)
        if not existing_job_name:
            logger.info(f"No existing job for pipeline {pipeline_id}, creating new job")
            return False, None, None

        # If no old schema, can't do evolution (create new)
        if not old_schema:
            logger.warning(f"No old schema available for pipeline {pipeline_id}, creating new job")
            return False, existing_job_name, None

        # Both job and schema exist, use evolution
        logger.info(f"Existing job and schema found for pipeline {pipeline_id}, using schema evolution")
        return True, existing_job_name, old_schema

    except Exception as e:
        logger.error(f"Error determining if schema evolution should be used: {e}")
        # Default to not using evolution (safer)
        return False, None, None


def create_or_update_job_with_evolution(
    pipeline_id: str,
    target_type: str,
    target_schema: dict,
    generated_code_s3_path: str,
    s3_table_bucket: dict,
    worker_recommendation,
    target_config: dict[str, Any],
    test_results: dict[str, Any],
    run_id: str,
    pipelines_table,
    create_glue_job_func,
) -> dict[str, Any]:
    """
    Create or update Glue job with schema evolution support.

    This function:
    1. Checks if an existing job exists for the pipeline
    2. If exists, compares schemas and determines evolution strategy
    3. Either updates existing job (compatible) or creates new version (breaking)
    4. If no existing job, creates new one

    Args:
        pipeline_id: Pipeline identifier
        target_type: Target type
        target_schema: New target schema
        generated_code_s3_path: S3 location of generated code
        s3_table_bucket: S3 Table Bucket info
        worker_recommendation: Worker configuration
        target_config: Target configuration
        test_results: Test results
        run_id: Run identifier
        pipelines_table: DynamoDB pipelines table
        create_glue_job_func: Function to create Glue job

    Returns:
        dict: Job creation/update result
    """
    try:
        # Check if schema evolution should be used
        use_evolution, existing_job_name, old_schema = should_use_schema_evolution(
            pipeline_id, target_schema, pipelines_table
        )

        if not use_evolution:
            # No existing job or schema, create new job normally
            logger.info(f"Creating new Glue job for pipeline {pipeline_id}")

            result = create_glue_job_func(
                pipeline_id=pipeline_id,
                target_type=target_type,
                target_schema=target_schema,
                generated_code_s3_path=generated_code_s3_path,
                s3_table_bucket=s3_table_bucket,
                worker_recommendation=worker_recommendation,
                target_config=target_config,
                test_results=test_results,
                run_id=run_id,
            )

            result["schema_evolution_used"] = False
            result["action"] = "created_new"

            return result

        # Use schema evolution
        logger.info(f"Using schema evolution for pipeline {pipeline_id}")

        # Import schema evolution module
        try:
            from schema_evolution import handle_schema_evolution
        except ImportError as e:
            logger.error(f"Schema evolution module not available: {e}")
            logger.warning("Falling back to standard job creation")

            result = create_glue_job_func(
                pipeline_id=pipeline_id,
                target_type=target_type,
                target_schema=target_schema,
                generated_code_s3_path=generated_code_s3_path,
                s3_table_bucket=s3_table_bucket,
                worker_recommendation=worker_recommendation,
                target_config=target_config,
                test_results=test_results,
                run_id=run_id,
            )

            result["schema_evolution_used"] = False
            result["schema_evolution_error"] = str(e)

            return result

        # Build job configuration for evolution
        job_config = build_job_config_for_evolution(
            pipeline_id,
            target_type,
            target_schema,
            generated_code_s3_path,
            s3_table_bucket,
            worker_recommendation,
            target_config,
        )

        # Handle schema evolution
        evolution_result = handle_schema_evolution(
            pipeline_id=pipeline_id,
            old_schema=old_schema,
            new_schema=target_schema,
            new_script_location=generated_code_s3_path,
            job_config=job_config,
            pipelines_table=pipelines_table,
        )

        if not evolution_result.get("success"):
            logger.error(f"Schema evolution failed: {evolution_result.get('message')}")

            # If evolution failed due to job running, return error
            if evolution_result.get("error") == "job_running":
                return {
                    "statusCode": 409,  # Conflict
                    "error": "job_running",
                    "message": evolution_result.get("message"),
                    "run_id": evolution_result.get("run_id"),
                    "job_name": existing_job_name,
                }

            # For other errors, fall back to creating new job
            logger.warning("Falling back to standard job creation after evolution failure")

            result = create_glue_job_func(
                pipeline_id=pipeline_id,
                target_type=target_type,
                target_schema=target_schema,
                generated_code_s3_path=generated_code_s3_path,
                s3_table_bucket=s3_table_bucket,
                worker_recommendation=worker_recommendation,
                target_config=target_config,
                test_results=test_results,
                run_id=run_id,
            )

            result["schema_evolution_used"] = False
            result["schema_evolution_error"] = evolution_result.get("message")

            return result

        # Schema evolution succeeded
        logger.info(f"Schema evolution completed successfully: {evolution_result.get('action')}")

        # Build result
        result = {
            "job_name": evolution_result.get("job_name"),
            "job_arn": f"arn:aws:glue:{boto3.Session().region_name}:{get_aws_account_id()}:job/{evolution_result.get('job_name')}",
            "status": "success",
            "schema_evolution_used": True,
            "change_type": evolution_result.get("change_type"),
            "action": evolution_result.get("action"),
            "differences": evolution_result.get("differences", []),
            "script_location": generated_code_s3_path,
        }

        # Add version info if new version was created
        if "version" in evolution_result:
            result["version"] = evolution_result["version"]
            result["previous_version"] = evolution_result.get("previous_version")

        # Add worker recommendation if available
        if worker_recommendation:
            result["worker_type"] = worker_recommendation.worker_type
            result["number_of_workers"] = worker_recommendation.number_of_workers
            result["estimated_cost_per_run"] = worker_recommendation.estimated_cost_per_run

        return result

    except Exception as e:
        logger.error(f"Error in create_or_update_job_with_evolution: {e}")

        # Fall back to standard creation
        logger.warning("Falling back to standard job creation after exception")

        result = create_glue_job_func(
            pipeline_id=pipeline_id,
            target_type=target_type,
            target_schema=target_schema,
            generated_code_s3_path=generated_code_s3_path,
            s3_table_bucket=s3_table_bucket,
            worker_recommendation=worker_recommendation,
            target_config=target_config,
            test_results=test_results,
            run_id=run_id,
        )

        result["schema_evolution_used"] = False
        result["schema_evolution_error"] = str(e)

        return result


def build_job_config_for_evolution(
    pipeline_id: str,
    target_type: str,
    target_schema: dict,
    generated_code_s3_path: str,
    s3_table_bucket: dict,
    worker_recommendation,
    target_config: dict[str, Any],
) -> dict[str, Any]:
    """
    Build job configuration dict for schema evolution.

    This creates a configuration that can be used by both update_job and create_job.

    Args:
        pipeline_id: Pipeline identifier
        target_type: Target type
        target_schema: Target schema
        generated_code_s3_path: S3 location of script
        s3_table_bucket: S3 Table Bucket info
        worker_recommendation: Worker configuration
        target_config: Target configuration

    Returns:
        dict: Job configuration
    """
    try:
        # Get role ARN (simplified - in real implementation would call create_pipeline_specific_role)
        role_arn = pipeline_execution_role_arn(pipeline_id, get_aws_account_id())

        # Use worker recommendation if available
        if worker_recommendation:
            worker_type = worker_recommendation.worker_type
            number_of_workers = worker_recommendation.number_of_workers
            timeout = worker_recommendation.timeout_minutes
        else:
            worker_type = "G.1X"
            number_of_workers = 2
            timeout = 60

        # Build base configuration
        job_config = {
            "Description": f"AgenticDataIngestion ETL job for pipeline {pipeline_id}",
            "Role": role_arn,
            "Command": {"Name": "glueetl", "ScriptLocation": generated_code_s3_path, "PythonVersion": "3"},
            "DefaultArguments": {
                "--job-language": "python",
                "--job-bookmark-option": "job-bookmark-enable",
                "--enable-metrics": "true",
                "--enable-continuous-cloudwatch-log": "true",
                "--enable-spark-ui": "true",
                "--enable-auto-scaling": "true",
                "--pipeline-id": pipeline_id,
                "--target-type": target_type,
            },
            # No Glue-native auto-retry — see job_builders.py: retries are
            # owned by self-healing / p16; an auto-retry's completion event
            # can never match a token and its run blocks the heal rerun's
            # start_job_run (MaxConcurrentRuns=1).
            "MaxRetries": 0,
            "Timeout": timeout,
            "WorkerType": worker_type,
            "NumberOfWorkers": number_of_workers,
            "GlueVersion": "5.0",
            "Tags": {"Project": "AgenticDataIngestion", "PipelineId": pipeline_id, "WorkerType": worker_type},
        }

        # Add target-specific arguments (simplified)
        if target_type in ["S3_TABLES", "S3_TABLE_BUCKET"]:
            job_config["DefaultArguments"]["--target-bucket-name"] = s3_table_bucket.get("bucket_name", "")
            job_config["DefaultArguments"]["--target-table-name"] = s3_table_bucket.get("table_name", "")

        return job_config

    except Exception as e:
        logger.error(f"Error building job config for evolution: {e}")
        raise
