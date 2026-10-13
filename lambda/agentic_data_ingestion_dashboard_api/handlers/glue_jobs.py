#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Glue job management handlers for dashboard API"""

from datetime import UTC, datetime
from typing import Any

import boto3

# Shared typed-accessor helpers (P22 Req 2). Resolved via the same path the
# dashboard handlers use for other shared modules: ``.common`` runs the
# Layer_Import_Fallback ``sys.path.append`` (``/opt/python`` in the Lambda layer,
# or the local ``shared/`` dir in test/local execution) at import time.
from dynamo_typing import item_dict, item_int, item_list, item_str

from .common import (
    AWS_REGION,
    create_response,
    generated_code_table,
    handle_error,
    logger,
    pipelines_table,
    state_table,
)

# Initialize AWS clients
glue_client = boto3.client("glue", region_name=AWS_REGION)
logs_client = boto3.client("logs", region_name=AWS_REGION)


def _query_schema_extractions(pipeline_id: str) -> list[dict[str, Any]]:
    """All schema_extract state rows for a pipeline, via the step_name GSI.

    Query, not Scan (P28 R17.1): the key condition bounds the read to the
    ``schema_extract`` partition of ``step_name-timestamp-index``; the
    ``original_pipeline_id`` filter (the exact predicate the old scan used —
    it survives heal/retry runs whose ``pipeline_id`` diverges) runs
    server-side on that bounded set. Paginated: schema history must be
    complete for stable version numbering.
    """
    from boto3.dynamodb.conditions import Attr, Key

    query_kwargs: dict[str, Any] = {
        "IndexName": "step_name-timestamp-index",
        "KeyConditionExpression": Key("step_name").eq("schema_extract"),
        "FilterExpression": Attr("original_pipeline_id").eq(pipeline_id),
    }
    items: list[dict[str, Any]] = []
    while True:
        response = state_table.query(**query_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key
    return items


def _query_generated_code(pipeline_id: str, **extra_kwargs: Any) -> list[dict[str, Any]]:
    """All generated-code rows for a pipeline, via the pipeline_id GSI.

    Query, not Scan (P28 R17.1): reads only the pipeline's partition of
    ``pipeline_id-index``. Optional ``extra_kwargs`` (e.g. a FilterExpression
    on source_schema_id) narrow the result server-side. Paginated.
    """
    from boto3.dynamodb.conditions import Key

    query_kwargs: dict[str, Any] = {
        "IndexName": "pipeline_id-index",
        "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
        **extra_kwargs,
    }
    items: list[dict[str, Any]] = []
    while True:
        response = generated_code_table.query(**query_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key
    return items


def get_glue_job_details(pipeline_id: str) -> dict[str, Any]:
    """
    Get Glue job details for a specific pipeline.

    Returns:
        - Job name, ARN, status, worker config
        - Last run timestamp and success rate
        - Estimated cost per run

    Args:
        pipeline_id: Pipeline identifier

    Returns:
        API Gateway response with Glue job details
    """
    try:
        logger.info(f"Fetching Glue job details for pipeline: {pipeline_id}")

        # Get pipeline metadata from DynamoDB
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        pipeline_item = response["Item"]

        # Extract Glue job metadata
        glue_metadata = item_dict(pipeline_item, "glue_job_metadata")
        worker_config = item_dict(pipeline_item, "worker_configuration")
        cost_info = item_dict(pipeline_item, "cost_information")
        mcp_recommendation = item_dict(pipeline_item, "mcp_recommendation")

        # Check if Glue job exists
        if not glue_metadata or not glue_metadata.get("glue_job_name"):
            return create_response(
                404,
                {
                    "error": f"No Glue job found for pipeline: {pipeline_id}",
                    "message": "Glue job has not been created yet for this pipeline",
                },
            )

        job_name = item_str(glue_metadata, "glue_job_name")

        # Get job run history from AWS Glue API
        job_runs_info = get_job_runs_from_glue(job_name)

        # Get script versions from generated_code_table and correlate with schema versions
        script_versions = []
        try:
            logger.info(f"Building script version history for pipeline: {pipeline_id}")

            # Step 1: Get all schema extractions via the step_name GSI (P28 R17.1)
            schema_extractions_items = _query_schema_extractions(pipeline_id)

            logger.info(f"Fetched {len(schema_extractions_items)} schema extraction records")

            # Build schema hash to version mapping
            schema_hash_map: dict[Any, dict[str, Any]] = {}
            for idx, extraction in enumerate(
                sorted(schema_extractions_items, key=lambda x: item_str(x, "timestamp")), start=1
            ):
                schema_hash = extraction.get("schema_hash")
                if schema_hash:
                    schema_hash_map[schema_hash] = {
                        "version": idx,
                        "timestamp": extraction.get("timestamp"),
                        "run_id": extraction.get("run_id"),
                    }

            logger.info(f"Found {len(schema_hash_map)} unique schema versions")

            # Step 2: Get all generated code via the pipeline_id GSI (P28 R17.1)
            code_items = _query_generated_code(pipeline_id)

            logger.info(f"Fetched {len(code_items)} code generation records")

            # Step 3: Match code to schema versions by hash
            for item in code_items:
                source_schema_hash = item_str(item, "source_schema_id")  # This is the schema hash
                schema_info = schema_hash_map.get(source_schema_hash, {})

                if schema_info:
                    code_entry = {
                        "code_version_id": item.get("code_version_id"),
                        "script_location": item.get("code_s3_uri"),
                        "timestamp": item.get("generation_timestamp"),
                        "source_schema_id": source_schema_hash,
                        "target_schema_id": item.get("target_schema_id"),
                        "mapping_id": item.get("mapping_id"),
                        "run_id": item.get("run_id"),
                        "version": schema_info["version"],
                        "schema_version": schema_info["version"],
                        "schema_timestamp": schema_info["timestamp"],
                    }

                    script_versions.append(code_entry)
                    logger.info(
                        f"✓ Matched code to schema version {schema_info['version']}: {item_str(item, 'code_s3_uri')}"
                    )
                else:
                    logger.warning(
                        f"Could not match code {item_str(item, 'code_version_id')} with schema_hash {source_schema_hash}"
                    )

            # Sort by version number
            script_versions.sort(key=lambda x: x.get("version", 0))

            logger.info(f"Returning {len(script_versions)} script versions with proper schema correlation")

        except Exception as e:
            logger.error(f"Error fetching script versions: {e}", exc_info=True)
            # Return empty array on error but log the full exception
            script_versions = []

        # Build response
        glue_job_details = {
            # Job identification
            "pipeline_id": pipeline_id,
            "job_name": job_name,
            "job_arn": glue_metadata.get("glue_job_arn", ""),
            "glue_version": glue_metadata.get("glue_version", "5.0"),
            # Job status
            "status": glue_metadata.get("status", "created"),
            "created_at": glue_metadata.get("created_at", ""),
            # Worker configuration
            "worker_type": worker_config.get("worker_type", "G.1X"),
            "number_of_workers": worker_config.get("number_of_workers", 2),
            "timeout_minutes": worker_config.get("timeout_minutes", 60),
            "estimated_duration_minutes": worker_config.get("estimated_duration_minutes", 30),
            # Cost information
            "estimated_cost_per_run": cost_info.get("estimated_cost_per_run", 0.0),
            "estimated_monthly_cost": cost_info.get("estimated_monthly_cost", 0.0),
            "cost_per_gb": cost_info.get("cost_per_gb", 0.0),
            "total_dpu": cost_info.get("total_dpu", 0.0),
            "dpu_hours": cost_info.get("dpu_hours", 0.0),
            # Job run statistics
            "last_run_timestamp": job_runs_info.get("last_run_timestamp", ""),
            "last_run_status": job_runs_info.get("last_run_status", ""),
            "success_rate": job_runs_info.get("success_rate", 0.0),
            "total_runs": job_runs_info.get("total_runs", 0),
            "successful_runs": job_runs_info.get("successful_runs", 0),
            "failed_runs": job_runs_info.get("failed_runs", 0),
            "average_duration_minutes": job_runs_info.get("average_duration_minutes", 0.0),
            # Individual job runs
            "runs": job_runs_info.get("runs", []),
            # MCP recommendation details
            "mcp_enhanced": mcp_recommendation.get("mcp_enhanced", False),
            "recommendation_rationale": mcp_recommendation.get("rationale", ""),
            "recommendation_confidence": mcp_recommendation.get("confidence", 0.0),
            "mcp_tools_used": mcp_recommendation.get("mcp_tools_used", []),
            # Additional metadata
            "script_location": glue_metadata.get("script_location", ""),
            "role_arn": glue_metadata.get("role_arn", ""),
            # Script versions from generated_code_table
            "script_versions": script_versions,
        }

        logger.info(f"Successfully retrieved Glue job details for {pipeline_id}")
        return create_response(200, glue_job_details)

    except Exception as e:
        logger.error(f"Error fetching Glue job details: {e!s}")
        return handle_error(e, f"Error fetching Glue job details for pipeline {pipeline_id}")


def start_glue_job_run(pipeline_id: str) -> dict[str, Any]:
    """
    Start a Glue job execution for a specific pipeline.

    Validates that the job exists before starting execution.

    Args:
        pipeline_id: Pipeline identifier

    Returns:
        API Gateway response with execution ID and job details
    """
    try:
        logger.info(f"Starting Glue job run for pipeline: {pipeline_id}")

        # Get pipeline metadata from DynamoDB
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        pipeline_item = response["Item"]

        # Extract Glue job metadata
        glue_metadata = item_dict(pipeline_item, "glue_job_metadata")

        # Validate that Glue job exists
        if not glue_metadata or not glue_metadata.get("glue_job_name"):
            return create_response(
                404,
                {
                    "error": f"No Glue job found for pipeline: {pipeline_id}",
                    "message": "Glue job has not been created yet for this pipeline. Complete pipeline provisioning first.",
                },
            )

        job_name = item_str(glue_metadata, "glue_job_name")

        # Verify job exists in AWS Glue before starting
        try:
            glue_client.get_job(JobName=job_name)
        except glue_client.exceptions.EntityNotFoundException:
            logger.error(f"Glue job not found in AWS: {job_name}")
            return create_response(
                404,
                {
                    "error": f"Glue job does not exist in AWS: {job_name}",
                    "message": "The job may have been deleted or not yet created. Check pipeline status.",
                },
            )

        # Start the Glue job
        logger.info(f"Starting Glue job: {job_name}")
        start_response = glue_client.start_job_run(JobName=job_name)

        job_run_id = start_response.get("JobRunId", "")

        logger.info(f"Successfully started Glue job {job_name} with run ID: {job_run_id}")

        # Build response
        response_data = {
            "pipeline_id": pipeline_id,
            "job_name": job_name,
            "job_run_id": job_run_id,
            "status": "RUNNING",
            "started_at": datetime.now(UTC).isoformat(),
            "message": "Glue job execution started successfully",
            "cloudwatch_logs_url": f"https://console.aws.amazon.com/cloudwatch/home?region={AWS_REGION}#logsV2:log-groups/log-group/$252Faws-glue$252Fjobs$252Foutput/log-events/{job_run_id}",
        }

        return create_response(200, response_data)

    except glue_client.exceptions.ConcurrentRunsExceededException as e:
        logger.warning(f"Concurrent runs limit exceeded for job: {e!s}")
        return create_response(
            429,
            {
                "error": "Concurrent runs limit exceeded",
                "message": "The job is already running. Wait for the current execution to complete before starting a new run.",
                "pipeline_id": pipeline_id,
            },
        )
    except glue_client.exceptions.InvalidInputException as e:
        logger.error(f"Invalid input for job start: {e!s}")
        return create_response(
            400, {"error": "Invalid job configuration", "message": str(e), "pipeline_id": pipeline_id}
        )
    except Exception as e:
        logger.error(f"Error starting Glue job: {e!s}")
        return handle_error(e, f"Error starting Glue job for pipeline {pipeline_id}")


def get_glue_job_runs(pipeline_id: str, max_results: int = 25, next_token: str | None = None) -> dict[str, Any]:
    """
    Get job run history for a specific pipeline with pagination.

    Returns execution history with status, duration, and CloudWatch log links.
    Supports pagination for large result sets.

    Args:
        pipeline_id: Pipeline identifier
        max_results: Maximum number of results to return (default: 25, max: 100)
        next_token: Pagination token from previous request

    Returns:
        API Gateway response with job run history
    """
    try:
        logger.info(f"Fetching Glue job runs for pipeline: {pipeline_id}, max_results: {max_results}")

        # Validate max_results
        max_results = min(max(1, max_results), 100)  # Clamp between 1 and 100

        # Get pipeline metadata from DynamoDB
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        pipeline_item = response["Item"]

        # Extract Glue job metadata
        glue_metadata = item_dict(pipeline_item, "glue_job_metadata")

        # Check if Glue job exists
        if not glue_metadata or not glue_metadata.get("glue_job_name"):
            return create_response(
                404,
                {
                    "error": f"No Glue job found for pipeline: {pipeline_id}",
                    "message": "Glue job has not been created yet for this pipeline",
                },
            )

        job_name = item_str(glue_metadata, "glue_job_name")

        # Query Glue API for job runs with pagination
        glue_params: dict[str, Any] = {"JobName": job_name, "MaxResults": max_results}

        if next_token:
            glue_params["NextToken"] = next_token

        logger.info(f"Querying Glue API for job runs: {job_name}")
        glue_response = glue_client.get_job_runs(**glue_params)

        job_runs = glue_response.get("JobRuns", [])
        next_token_response = glue_response.get("NextToken")

        # Transform job runs to API response format
        runs = []
        for run in job_runs:
            run_id = run.get("Id", "")
            started_on = run.get("StartedOn")
            completed_on = run.get("CompletedOn")
            status = run.get("JobRunState", "")
            error_message = run.get("ErrorMessage", "")

            # Calculate duration
            duration_seconds = 0
            if started_on and completed_on:
                duration_seconds = int((completed_on - started_on).total_seconds())
            elif started_on and status == "RUNNING":
                # For running jobs, calculate elapsed time
                duration_seconds = int((datetime.now(UTC) - started_on.replace(tzinfo=UTC)).total_seconds())

            duration_minutes = round(duration_seconds / 60, 2)

            # Build CloudWatch logs URL
            cloudwatch_logs_url = (
                f"https://console.aws.amazon.com/cloudwatch/home?region={AWS_REGION}"
                f"#logsV2:log-groups/log-group/$252Faws-glue$252Fjobs$252Foutput"
                f"/log-events/{run_id}"
            )

            # Get DPU seconds if available
            dpu_seconds = run.get("DPUSeconds", 0)
            max_capacity = run.get("MaxCapacity", 0)
            worker_type = run.get("WorkerType", "")
            number_of_workers = run.get("NumberOfWorkers", 0)

            # Calculate cost estimate for this run
            cost_estimate = 0.0
            if dpu_seconds > 0:
                # Glue 5.0 pricing: $0.44 per DPU-hour
                dpu_hours = dpu_seconds / 3600
                cost_estimate = round(dpu_hours * 0.44, 4)

            runs.append(
                {
                    "run_id": run_id,
                    "status": status,
                    "started_at": started_on.strftime("%Y-%m-%dT%H:%M:%SZ") if started_on else "",
                    "completed_at": completed_on.strftime("%Y-%m-%dT%H:%M:%SZ") if completed_on else "",
                    "duration_seconds": duration_seconds,
                    "duration_minutes": duration_minutes,
                    "error_message": error_message,
                    "cloudwatch_logs_url": cloudwatch_logs_url,
                    "dpu_seconds": dpu_seconds,
                    "max_capacity": max_capacity,
                    "worker_type": worker_type,
                    "number_of_workers": number_of_workers,
                    "cost_estimate": cost_estimate,
                    "attempt": run.get("Attempt", 0),
                    "execution_time": run.get("ExecutionTime", 0),
                    "timeout": run.get("Timeout", 0),
                }
            )

        logger.info(f"Retrieved {len(runs)} job runs for {job_name}")

        # Build response
        response_data = {
            "pipeline_id": pipeline_id,
            "job_name": job_name,
            "runs": runs,
            "total_returned": len(runs),
            "has_more": next_token_response is not None,
            "next_token": next_token_response,
        }

        return create_response(200, response_data)

    except glue_client.exceptions.EntityNotFoundException:
        logger.error(f"Glue job not found in AWS: {job_name}")
        return create_response(
            404,
            {
                "error": "Glue job does not exist in AWS",
                "message": "The job may have been deleted. Check pipeline status.",
            },
        )
    except glue_client.exceptions.InvalidInputException as e:
        logger.error(f"Invalid input for get_job_runs: {e!s}")
        return create_response(400, {"error": "Invalid request parameters", "message": str(e)})
    except Exception as e:
        logger.error(f"Error fetching Glue job runs: {e!s}")
        return handle_error(e, f"Error fetching Glue job runs for pipeline {pipeline_id}")


def get_job_runs_from_glue(job_name: str) -> dict[str, Any]:
    """
    Query AWS Glue API for job run history and calculate statistics.

    Args:
        job_name: Glue job name

    Returns:
        dict: Job run statistics including last run, success rate, etc.
    """
    try:
        logger.info(f"Fetching job runs from Glue API for job: {job_name}")

        # Get job runs from Glue (most recent 100)
        response = glue_client.get_job_runs(JobName=job_name, MaxResults=100)

        job_runs = response.get("JobRuns", [])

        if not job_runs:
            logger.info(f"No job runs found for {job_name}")
            return {
                "last_run_timestamp": "",
                "last_run_status": "",
                "success_rate": 0.0,
                "total_runs": 0,
                "successful_runs": 0,
                "failed_runs": 0,
                "average_duration_minutes": 0.0,
                "runs": [],
            }

        # Calculate statistics
        total_runs = len(job_runs)
        successful_runs = sum(1 for run in job_runs if run.get("JobRunState") == "SUCCEEDED")
        failed_runs = sum(1 for run in job_runs if run.get("JobRunState") in ["FAILED", "ERROR", "TIMEOUT"])
        success_rate = (successful_runs / total_runs * 100) if total_runs > 0 else 0.0

        # Calculate average duration (only for completed runs)
        completed_runs = [run for run in job_runs if run.get("JobRunState") in ["SUCCEEDED", "FAILED"]]
        if completed_runs:
            total_duration_seconds = 0.0
            duration_count = 0

            for run in completed_runs:
                started_on = run.get("StartedOn")
                completed_on = run.get("CompletedOn")

                if started_on and completed_on:
                    duration_seconds = (completed_on - started_on).total_seconds()
                    total_duration_seconds += duration_seconds
                    duration_count += 1

            average_duration_minutes = (total_duration_seconds / duration_count / 60) if duration_count > 0 else 0.0
        else:
            average_duration_minutes = 0.0

        # Get last run info
        last_run = job_runs[0]  # Most recent run
        last_run_started = last_run.get("StartedOn")
        last_run_timestamp = last_run_started.isoformat() if last_run_started else ""
        last_run_status = last_run.get("JobRunState", "")

        logger.info(
            f"Job run statistics for {job_name}: {total_runs} total, {successful_runs} succeeded, {success_rate:.1f}% success rate"
        )

        # Build runs list for frontend
        runs_list = []
        for run in job_runs:
            started_on = run.get("StartedOn")
            completed_on = run.get("CompletedOn")
            duration_seconds = 0
            if started_on and completed_on:
                duration_seconds = int((completed_on - started_on).total_seconds())

            runs_list.append(
                {
                    "run_id": run.get("Id", ""),
                    "status": run.get("JobRunState", ""),
                    "started_at": started_on.strftime("%Y-%m-%dT%H:%M:%SZ") if started_on else "",
                    "completed_at": completed_on.strftime("%Y-%m-%dT%H:%M:%SZ") if completed_on else "",
                    "duration_seconds": duration_seconds,
                    "error_message": run.get("ErrorMessage", ""),
                    "attempt": run.get("Attempt", 0),
                }
            )

        return {
            "last_run_timestamp": last_run_timestamp,
            "last_run_status": last_run_status,
            "success_rate": round(success_rate, 2),
            "total_runs": total_runs,
            "successful_runs": successful_runs,
            "failed_runs": failed_runs,
            "average_duration_minutes": round(average_duration_minutes, 2),
            "runs": runs_list,
        }

    except glue_client.exceptions.EntityNotFoundException:
        logger.warning(f"Glue job not found: {job_name}")
        return {
            "last_run_timestamp": "",
            "last_run_status": "",
            "success_rate": 0.0,
            "total_runs": 0,
            "successful_runs": 0,
            "failed_runs": 0,
            "average_duration_minutes": 0.0,
            "runs": [],
        }
    except Exception as e:
        logger.error(f"Error fetching job runs from Glue: {e!s}")
        # Return empty stats on error
        return {
            "last_run_timestamp": "",
            "last_run_status": "",
            "success_rate": 0.0,
            "total_runs": 0,
            "successful_runs": 0,
            "failed_runs": 0,
            "average_duration_minutes": 0.0,
            "runs": [],
        }


def get_glue_job_logs(
    pipeline_id: str,
    run_id: str,
    log_type: str = "output",
    filter_pattern: str | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    limit: int = 100,
    next_token: str | None = None,
) -> dict[str, Any]:
    """
    Fetch CloudWatch logs for a specific Glue job run.

    Supports log filtering, search, and pagination for dashboard display.

    Args:
        pipeline_id: Pipeline identifier
        run_id: Glue job run ID
        log_type: Type of logs to fetch ('output', 'error', or 'driver')
        filter_pattern: CloudWatch Logs filter pattern for searching
        start_time: Start time for log retrieval (Unix timestamp in milliseconds)
        end_time: End time for log retrieval (Unix timestamp in milliseconds)
        limit: Maximum number of log events to return (default: 100, max: 10000)
        next_token: Pagination token from previous request

    Returns:
        API Gateway response with structured log data
    """
    try:
        logger.info(f"Fetching logs for pipeline {pipeline_id}, run {run_id}, type: {log_type}")

        # Validate limit
        limit = min(max(1, limit), 10000)  # Clamp between 1 and 10000

        # Get pipeline metadata from DynamoDB
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        pipeline_item = response["Item"]

        # Extract Glue job metadata
        glue_metadata = item_dict(pipeline_item, "glue_job_metadata")

        # Check if Glue job exists
        if not glue_metadata or not glue_metadata.get("glue_job_name"):
            return create_response(
                404,
                {
                    "error": f"No Glue job found for pipeline: {pipeline_id}",
                    "message": "Glue job has not been created yet for this pipeline",
                },
            )

        job_name = item_str(glue_metadata, "glue_job_name")

        # Verify the job run exists
        try:
            job_run = glue_client.get_job_run(JobName=job_name, RunId=run_id)
            job_run_data = job_run.get("JobRun", {})
        except glue_client.exceptions.EntityNotFoundException:
            return create_response(
                404,
                {
                    "error": f"Job run not found: {run_id}",
                    "message": f"No job run found with ID {run_id} for job {job_name}",
                },
            )

        # Determine CloudWatch log group and stream based on log type
        log_group_name = get_log_group_name(log_type)
        log_stream_name = run_id

        # Check if log stream exists
        try:
            logs_client.describe_log_streams(logGroupName=log_group_name, logStreamNamePrefix=log_stream_name, limit=1)
        except logs_client.exceptions.ResourceNotFoundException:
            return create_response(
                404,
                {
                    "error": "Log stream not found",
                    "message": f"No logs available yet for run {run_id}. The job may still be starting or logs may not have been created.",
                    "log_group": log_group_name,
                    "log_stream": log_stream_name,
                },
            )

        # Build parameters for CloudWatch Logs query
        logs_params: dict[str, Any] = {
            "logGroupName": log_group_name,
            "logStreamNames": [log_stream_name],
            "limit": limit,
        }

        # Add optional parameters
        if filter_pattern:
            logs_params["filterPattern"] = filter_pattern

        if start_time:
            logs_params["startTime"] = start_time

        if end_time:
            logs_params["endTime"] = end_time

        if next_token:
            logs_params["nextToken"] = next_token

        # Fetch logs from CloudWatch
        logger.info(f"Querying CloudWatch logs: {log_group_name}/{log_stream_name}")
        logs_response = logs_client.filter_log_events(**logs_params)

        # Extract log events
        events = logs_response.get("events", [])
        next_forward_token = logs_response.get("nextForwardToken")
        next_backward_token = logs_response.get("nextBackwardToken")

        # Transform log events to structured format
        log_entries = []
        for event in events:
            timestamp = event.get("timestamp", 0)
            message = event.get("message", "")

            # Parse log level from message if present
            log_level = parse_log_level(message)

            log_entries.append(
                {
                    "timestamp": timestamp,
                    "timestamp_iso": datetime.fromtimestamp(timestamp / 1000).isoformat() + "Z",
                    "message": message,
                    "log_level": log_level,
                    "ingestion_time": event.get("ingestionTime", 0),
                }
            )

        # Get job run status for context
        job_status = job_run_data.get("JobRunState", "")
        started_on = job_run_data.get("StartedOn")
        completed_on = job_run_data.get("CompletedOn")

        # Build CloudWatch console URL
        cloudwatch_console_url = (
            f"https://console.aws.amazon.com/cloudwatch/home?region={AWS_REGION}"
            f"#logsV2:log-groups/log-group/{log_group_name.replace('/', '$252F')}"
            f"/log-events/{run_id.replace('/', '$252F')}"
        )

        # Build response
        response_data = {
            "pipeline_id": pipeline_id,
            "job_name": job_name,
            "run_id": run_id,
            "log_type": log_type,
            "log_group": log_group_name,
            "log_stream": log_stream_name,
            "logs": log_entries,
            "total_returned": len(log_entries),
            "has_more": next_forward_token is not None,
            "next_forward_token": next_forward_token,
            "next_backward_token": next_backward_token,
            "filter_pattern": filter_pattern,
            "job_status": job_status,
            "started_at": started_on.strftime("%Y-%m-%dT%H:%M:%SZ") if started_on else "",
            "completed_at": completed_on.strftime("%Y-%m-%dT%H:%M:%SZ") if completed_on else "",
            "cloudwatch_console_url": cloudwatch_console_url,
        }

        logger.info(f"Retrieved {len(log_entries)} log entries for run {run_id}")
        return create_response(200, response_data)

    except logs_client.exceptions.InvalidParameterException as e:
        logger.error(f"Invalid parameter for CloudWatch Logs: {e!s}")
        return create_response(400, {"error": "Invalid log query parameters", "message": str(e)})
    except logs_client.exceptions.ResourceNotFoundException as e:
        logger.error(f"CloudWatch log resource not found: {e!s}")
        return create_response(
            404,
            {
                "error": "Log resource not found",
                "message": "The requested log group or stream does not exist. Logs may not have been created yet.",
            },
        )
    except Exception as e:
        logger.error(f"Error fetching Glue job logs: {e!s}")
        return handle_error(e, f"Error fetching logs for pipeline {pipeline_id}, run {run_id}")


def get_log_group_name(log_type: str) -> str:
    """
    Get CloudWatch log group name based on log type.

    Args:
        log_type: Type of logs ('output', 'error', or 'driver')

    Returns:
        CloudWatch log group name
    """
    log_group_mapping = {
        "output": "/aws-glue/jobs/output",
        "error": "/aws-glue/jobs/error",
        "driver": "/aws-glue/jobs/logs-v2",
    }

    return log_group_mapping.get(log_type, "/aws-glue/jobs/output")


def parse_log_level(message: str) -> str:
    """
    Parse log level from log message.

    Attempts to extract log level (INFO, WARN, ERROR, DEBUG) from the message.

    Args:
        message: Log message text

    Returns:
        Log level string ('INFO', 'WARN', 'ERROR', 'DEBUG', or 'UNKNOWN')
    """
    message_upper = message.upper()

    # Check for common log level patterns
    if "ERROR" in message_upper or "EXCEPTION" in message_upper:
        return "ERROR"
    elif "WARN" in message_upper or "WARNING" in message_upper:
        return "WARN"
    elif "DEBUG" in message_upper:
        return "DEBUG"
    elif "INFO" in message_upper:
        return "INFO"
    else:
        return "UNKNOWN"


def rollback_glue_job(pipeline_id: str) -> dict[str, Any]:
    """
    Rollback Glue job to previous configuration.

    This endpoint:
    1. Checks if job is currently running (cannot rollback if running)
    2. Retrieves backup configuration from DynamoDB
    3. Restores previous job configuration
    4. Updates pipeline metadata

    Args:
        pipeline_id: Pipeline identifier

    Returns:
        API Gateway response with rollback result
    """
    try:
        logger.info(f"Rolling back Glue job for pipeline: {pipeline_id}")

        # Get pipeline metadata
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        pipeline_item = response["Item"]
        glue_metadata = item_dict(pipeline_item, "glue_job_metadata")
        job_name = item_str(glue_metadata, "glue_job_name")

        if not job_name:
            return create_response(404, {"error": f"No Glue job found for pipeline: {pipeline_id}"})

        # Check if job is currently running
        try:
            job_runs_response = glue_client.get_job_runs(JobName=job_name, MaxResults=5)

            for run in job_runs_response.get("JobRuns", []):
                if run.get("JobRunState") in ["RUNNING", "STOPPING"]:
                    return create_response(
                        409,
                        {  # Conflict
                            "error": "job_running",
                            "message": f"Cannot rollback - job is currently {run.get('JobRunState')}",
                            "run_id": run.get("Id"),
                            "job_name": job_name,
                        },
                    )
        except Exception as e:
            logger.warning(f"Could not check job run status: {e}")

        # Get backup configuration
        backup_config = item_dict(pipeline_item, "job_backup")

        if not backup_config:
            return create_response(
                404,
                {
                    "error": "no_backup_found",
                    "message": f"No backup configuration found for pipeline {pipeline_id}",
                    "hint": "Backup is created automatically when job is updated",
                },
            )

        # Remove backup-specific metadata
        backup_config_clean = {k: v for k, v in backup_config.items() if k != "backed_up_at"}

        # Perform rollback
        try:
            # backup_config_clean is a previously-stored Glue JobUpdate payload restored
            # from DynamoDB; it's a dict[str, Any], not the precise boto3 JobUpdateTypeDef.
            glue_client.update_job(JobName=job_name, JobUpdate=backup_config_clean)  # type: ignore[arg-type]

            logger.info(f"Successfully rolled back job {job_name}")

            # Update pipeline metadata
            pipelines_table.update_item(
                Key={"pipeline_id": pipeline_id},
                UpdateExpression="""
                    SET glue_job_metadata.rolled_back_at = :timestamp,
                        glue_job_metadata.rollback_count = if_not_exists(glue_job_metadata.rollback_count, :zero) + :one,
                        last_updated = :timestamp
                """,
                ExpressionAttributeValues={":timestamp": datetime.now(UTC).isoformat(), ":zero": 0, ":one": 1},
            )

            return create_response(
                200,
                {
                    "success": True,
                    "job_name": job_name,
                    "action": "rolled_back",
                    "backup_timestamp": backup_config.get("backed_up_at"),
                    "rolled_back_at": datetime.now(UTC).isoformat(),
                    "message": f"Successfully rolled back job {job_name} to previous configuration",
                },
            )

        except Exception as e:
            logger.error(f"Error performing rollback: {e}")
            return create_response(500, {"error": "rollback_failed", "message": str(e)})

    except Exception as e:
        logger.error(f"Error in rollback_glue_job: {e}")
        return handle_error(e, "rollback_glue_job")


def get_job_version_history(pipeline_id: str) -> dict[str, Any]:
    """
    Get version history for Glue job.

    Returns:
    - List of all job versions
    - Schema changes for each version
    - Deployment timestamps
    - Change classifications (compatible/breaking)

    Args:
        pipeline_id: Pipeline identifier

    Returns:
        API Gateway response with version history
    """
    try:
        logger.info(f"Fetching job version history for pipeline: {pipeline_id}")

        # Get pipeline metadata
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        pipeline_item = response["Item"]

        # Get version history
        version_history = item_list(pipeline_item, "job_version_history")
        current_version = item_int(pipeline_item, "current_job_version", 1)
        glue_metadata = item_dict(pipeline_item, "glue_job_metadata")

        # Format version history for response
        formatted_history = []

        for version_record in version_history:
            formatted_version = {
                "version": version_record.get("version", 0),
                "job_name": version_record.get("job_name", ""),
                "schema_change_type": version_record.get("schema_change_type", "unknown"),
                "created_at": version_record.get("created_at", ""),
                "status": version_record.get("status", "unknown"),
                "is_current": version_record.get("version") == current_version,
                "differences_count": len(version_record.get("differences", [])),
                "differences": version_record.get("differences", []),
            }

            formatted_history.append(formatted_version)

        # Sort by version (newest first)
        formatted_history.sort(key=lambda x: x["version"], reverse=True)

        # Build response
        response_data = {
            "pipeline_id": pipeline_id,
            "current_version": current_version,
            "current_job_name": glue_metadata.get("glue_job_name", ""),
            "total_versions": len(formatted_history),
            "version_history": formatted_history,
            "has_backup": "job_backup" in pipeline_item,
            "backup_timestamp": item_dict(pipeline_item, "job_backup").get("backed_up_at", ""),
            "rollback_count": glue_metadata.get("rollback_count", 0),
        }

        return create_response(200, response_data)

    except Exception as e:
        logger.error(f"Error in get_job_version_history: {e}")
        return handle_error(e, "get_job_version_history")


def validate_job_update_allowed(pipeline_id: str) -> dict[str, Any]:
    """
    Validate if job update is allowed (job not running).

    This is a utility endpoint that can be called before attempting updates
    to check if the job is in a state that allows updates.

    Args:
        pipeline_id: Pipeline identifier

    Returns:
        API Gateway response with validation result
    """
    try:
        logger.info(f"Validating job update allowed for pipeline: {pipeline_id}")

        # Get pipeline metadata
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        pipeline_item = response["Item"]
        glue_metadata = item_dict(pipeline_item, "glue_job_metadata")
        job_name = item_str(glue_metadata, "glue_job_name")

        if not job_name:
            return create_response(404, {"error": f"No Glue job found for pipeline: {pipeline_id}"})

        # Check if job is running
        try:
            job_runs_response = glue_client.get_job_runs(JobName=job_name, MaxResults=5)

            running_jobs = []
            for run in job_runs_response.get("JobRuns", []):
                if run.get("JobRunState") in ["RUNNING", "STOPPING"]:
                    run_started = run.get("StartedOn")
                    running_jobs.append(
                        {
                            "run_id": run.get("Id"),
                            "status": run.get("JobRunState"),
                            "started_on": run_started.isoformat() if run_started else "",
                        }
                    )

            if running_jobs:
                return create_response(
                    200,
                    {
                        "update_allowed": False,
                        "reason": "job_running",
                        "message": f"Job {job_name} is currently running",
                        "running_jobs": running_jobs,
                        "job_name": job_name,
                    },
                )

            return create_response(
                200,
                {
                    "update_allowed": True,
                    "job_name": job_name,
                    "message": f"Job {job_name} is not running, update allowed",
                },
            )

        except glue_client.exceptions.EntityNotFoundException:
            return create_response(404, {"error": "job_not_found", "message": f"Job {job_name} not found in AWS Glue"})

    except Exception as e:
        logger.error(f"Error in validate_job_update_allowed: {e}")
        return handle_error(e, "validate_job_update_allowed")


def get_glue_job_script(pipeline_id: str, version: str | None = None) -> dict[str, Any]:
    """
    Get the PySpark script content for a Glue job from S3.

    Args:
        pipeline_id: Pipeline identifier
        version: Optional version number to fetch specific version

    Returns:
        API Gateway response with script content
    """
    try:
        logger.info(f"Fetching Glue job script for pipeline: {pipeline_id}, version: {version}")

        # Get pipeline metadata from DynamoDB
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        pipeline_item = response["Item"]

        # Get script location - either from version history or current metadata
        script_location = None

        if version:
            # First, look for the script location in schema_history for this version
            schema_history = item_list(pipeline_item, "schema_history")
            for schema_entry in schema_history:
                if str(schema_entry.get("version")) == str(version):
                    script_location = schema_entry.get("script_location")
                    if script_location:
                        logger.info(f"Found script location in schema history for version {version}: {script_location}")
                        break

            # If not found in schema_history, look up by version number using state table
            if not script_location:
                try:
                    from boto3.dynamodb.conditions import Attr

                    logger.info(f"Looking for code version {version} for pipeline {pipeline_id}")

                    # Get schema extractions via the step_name GSI (P28 R17.1)
                    schema_extractions_found = _query_schema_extractions(pipeline_id)

                    # Sort by timestamp and find the version
                    sorted_extractions = sorted(schema_extractions_found, key=lambda x: item_str(x, "timestamp"))

                    # Version is 1-indexed
                    version_idx = int(version) - 1

                    if 0 <= version_idx < len(sorted_extractions):
                        schema_hash = item_str(sorted_extractions[version_idx], "schema_hash")
                        logger.info(f"Found schema hash for version {version}: {schema_hash}")

                        if schema_hash:
                            # Find code with this schema hash via the pipeline_id GSI (P28 R17.1)
                            code_items_found = _query_generated_code(
                                pipeline_id,
                                FilterExpression=Attr("source_schema_id").eq(schema_hash),
                            )
                            if code_items_found:
                                script_location = item_str(code_items_found[0], "code_s3_uri")
                                logger.info(f"✓ Found code for version {version}: {script_location}")
                            else:
                                logger.warning(f"No code found for schema hash {schema_hash}")
                    else:
                        logger.warning(f"Version {version} out of range (have {len(sorted_extractions)} versions)")

                except Exception as e:
                    logger.warning(f"Error looking up code by version: {e}")

        # If no version specified or version not found, use current script location
        if not script_location:
            glue_metadata = item_dict(pipeline_item, "glue_job_metadata")
            script_location = item_str(glue_metadata, "script_location")
            logger.info(f"Using current script location: {script_location}")

        if not script_location:
            return create_response(
                404,
                {
                    "error": "script_not_found",
                    "message": f"No script location found for pipeline {pipeline_id}"
                    + (f" version {version}" if version else ""),
                },
            )

        # Parse S3 location: s3://bucket/path/to/script.py
        if script_location.startswith("s3://"):
            parts = script_location.replace("s3://", "").split("/")
            bucket = parts[0]
            s3_key = "/".join(parts[1:])
        else:
            return create_response(
                400, {"error": "invalid_script_location", "message": f"Invalid S3 location format: {script_location}"}
            )

        # Fetch script content from S3
        s3_client = boto3.client("s3", region_name=AWS_REGION)

        try:
            logger.info(f"Fetching script from S3: s3://{bucket}/{s3_key}")
            s3_response = s3_client.get_object(Bucket=bucket, Key=s3_key)
            script_content = s3_response["Body"].read().decode("utf-8")

            return create_response(
                200,
                {
                    "script_content": script_content,
                    "script_location": f"s3://{bucket}/{s3_key}",
                    "version": version,
                    "size_bytes": len(script_content),
                    "last_modified": s3_response["LastModified"].isoformat() if "LastModified" in s3_response else None,
                },
            )

        except s3_client.exceptions.NoSuchKey:
            logger.warning(f"Script not found in S3: s3://{bucket}/{s3_key}")
            return create_response(
                404,
                {
                    "error": "script_not_found",
                    "message": f"Script file not found in S3: s3://{bucket}/{s3_key}",
                    "script_location": f"s3://{bucket}/{s3_key}",
                },
            )
        except s3_client.exceptions.NoSuchBucket:
            logger.error(f"S3 bucket not found: {bucket}")
            return create_response(404, {"error": "bucket_not_found", "message": f"S3 bucket not found: {bucket}"})

    except Exception as e:
        logger.error(f"Error fetching Glue job script: {e}")
        return handle_error(e, "get_glue_job_script")


def get_script_versions(pipeline_id: str) -> dict[str, Any]:
    """
    Get all script versions for a pipeline from the generated_code_table.

    Args:
        pipeline_id: Pipeline identifier

    Returns:
        API Gateway response with list of script versions
    """
    try:
        logger.info(f"Fetching script versions for pipeline: {pipeline_id}")

        # Query the pipeline_id GSI for all code generated for this pipeline (P28 R17.1)
        code_items = _query_generated_code(pipeline_id)

        script_versions = []
        for item in code_items:
            script_versions.append(
                {
                    "code_version_id": item.get("code_version_id"),
                    "script_location": item.get("code_s3_uri"),
                    "timestamp": item.get("generation_timestamp"),
                    "source_schema_id": item.get("source_schema_id"),
                    "target_schema_id": item.get("target_schema_id"),
                    "status": item.get("generation_status"),
                    "template_used": item.get("template_used"),
                }
            )

        # Sort by timestamp
        script_versions.sort(key=lambda x: item_str(x, "timestamp"), reverse=True)

        return create_response(
            200, {"pipeline_id": pipeline_id, "script_versions": script_versions, "count": len(script_versions)}
        )

    except Exception as e:
        logger.error(f"Error fetching script versions: {e}")
        return handle_error(e, "get_script_versions")
