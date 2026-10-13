# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Schema Evolution and Job Update Management

This module handles:
- Job versioning logic for schema changes
- Validation of job status before updates
- Rollback capability for failed updates
- Version history tracking in DynamoDB

Implements Task 10 from glue-job-generation spec.
"""

import json
import logging
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
glue_client = boto3.client("glue")
dynamodb = boto3.resource("dynamodb")


class SchemaChangeType(Enum):
    """Types of schema changes"""

    COMPATIBLE = "compatible"  # Can update existing job
    BREAKING = "breaking"  # Requires new versioned job
    IDENTICAL = "identical"  # No changes needed


class JobStatus(Enum):
    """Glue job execution statuses"""

    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


def classify_schema_change(
    old_schema: dict[str, Any], new_schema: dict[str, Any]
) -> tuple[SchemaChangeType, list[dict[str, Any]]]:
    """
    Classify schema change as compatible, breaking, or identical.

    Compatible changes:
    - Adding new nullable fields
    - Widening data types (int -> bigint, float -> double)
    - Adding optional fields

    Breaking changes:
    - Removing fields
    - Changing field types (incompatible)
    - Making nullable fields non-nullable
    - Renaming fields

    Args:
        old_schema: Previous schema definition
        new_schema: New schema definition

    Returns:
        tuple: (SchemaChangeType, list of differences)
    """
    try:
        old_fields = {f["name"]: f for f in old_schema.get("fields", [])}
        new_fields = {f["name"]: f for f in new_schema.get("fields", [])}

        differences = []
        is_breaking = False
        has_changes = False

        # Check for removed fields (BREAKING)
        for field_name in old_fields:
            if field_name not in new_fields:
                differences.append({"type": "field_removed", "field": field_name, "severity": "breaking"})
                is_breaking = True
                has_changes = True

        # Check for added fields
        for field_name in new_fields:
            if field_name not in old_fields:
                new_field = new_fields[field_name]
                # Adding nullable field is compatible
                if new_field.get("nullable", True):
                    differences.append(
                        {"type": "field_added", "field": field_name, "severity": "compatible", "nullable": True}
                    )
                    has_changes = True
                else:
                    # Adding non-nullable field is breaking
                    differences.append(
                        {"type": "field_added", "field": field_name, "severity": "breaking", "nullable": False}
                    )
                    is_breaking = True
                    has_changes = True

        # Check for modified fields
        for field_name in old_fields:
            if field_name in new_fields:
                old_field = old_fields[field_name]
                new_field = new_fields[field_name]

                # Check type changes
                if old_field.get("type") != new_field.get("type"):
                    type_change_compatible = is_type_change_compatible(old_field.get("type"), new_field.get("type"))

                    differences.append(
                        {
                            "type": "type_changed",
                            "field": field_name,
                            "old_type": old_field.get("type"),
                            "new_type": new_field.get("type"),
                            "severity": "compatible" if type_change_compatible else "breaking",
                        }
                    )

                    if not type_change_compatible:
                        is_breaking = True
                    has_changes = True

                # Check nullability changes
                old_nullable = old_field.get("nullable", True)
                new_nullable = new_field.get("nullable", True)

                if old_nullable and not new_nullable:
                    # Making field non-nullable is breaking
                    differences.append(
                        {
                            "type": "nullability_changed",
                            "field": field_name,
                            "old_nullable": old_nullable,
                            "new_nullable": new_nullable,
                            "severity": "breaking",
                        }
                    )
                    is_breaking = True
                    has_changes = True
                elif not old_nullable and new_nullable:
                    # Making field nullable is compatible
                    differences.append(
                        {
                            "type": "nullability_changed",
                            "field": field_name,
                            "old_nullable": old_nullable,
                            "new_nullable": new_nullable,
                            "severity": "compatible",
                        }
                    )
                    has_changes = True

        # Determine change type
        if not has_changes:
            change_type = SchemaChangeType.IDENTICAL
        elif is_breaking:
            change_type = SchemaChangeType.BREAKING
        else:
            change_type = SchemaChangeType.COMPATIBLE

        logger.info(f"Schema change classified as: {change_type.value}")
        logger.info(f"Found {len(differences)} differences")

        return change_type, differences

    except Exception as e:
        logger.error(f"Error classifying schema change: {e}")
        # Default to breaking change for safety
        return SchemaChangeType.BREAKING, [{"type": "error", "message": str(e)}]


def is_type_change_compatible(old_type: str, new_type: str) -> bool:
    """
    Check if type change is compatible (widening).

    Compatible type changes:
    - int -> bigint, long
    - float -> double
    - string -> text (same in most systems)
    - date -> timestamp

    Args:
        old_type: Original data type
        new_type: New data type

    Returns:
        bool: True if compatible, False if breaking
    """
    # Normalize types
    old_type = old_type.lower().strip()
    new_type = new_type.lower().strip()

    # Same type is always compatible
    if old_type == new_type:
        return True

    # Define compatible type transitions (widening)
    compatible_transitions = {
        "int": ["bigint", "long", "double", "decimal"],
        "integer": ["bigint", "long", "double", "decimal"],
        "smallint": ["int", "integer", "bigint", "long", "double", "decimal"],
        "tinyint": ["smallint", "int", "integer", "bigint", "long", "double", "decimal"],
        "float": ["double", "decimal"],
        "date": ["timestamp", "datetime"],
        "string": ["text", "varchar"],
        "varchar": ["text"],
    }

    allowed_new_types = compatible_transitions.get(old_type, [])
    return new_type in allowed_new_types


def check_job_running_status(job_name: str) -> tuple[bool, str | None]:
    """
    Check if Glue job is currently running.

    Args:
        job_name: Name of the Glue job

    Returns:
        tuple: (is_running, run_id) - True if running with run ID, False otherwise
    """
    try:
        # Get recent job runs
        response = glue_client.get_job_runs(JobName=job_name, MaxResults=10)

        job_runs = response.get("JobRuns", [])

        # Check if any runs are in progress
        for run in job_runs:
            status = run.get("JobRunState", "")

            if status in [JobStatus.RUNNING.value, JobStatus.STOPPING.value]:
                run_id = run.get("Id", "")
                logger.warning(f"Job {job_name} is currently {status}, run ID: {run_id}")
                return True, run_id

        logger.info(f"Job {job_name} is not currently running")
        return False, None

    except glue_client.exceptions.EntityNotFoundException:
        logger.info(f"Job {job_name} does not exist")
        return False, None
    except Exception as e:
        logger.error(f"Error checking job status: {e}")
        # Assume not running if we can't check
        return False, None


def get_current_job_version(pipeline_id: str, pipelines_table) -> int:
    """
    Get current job version number from pipeline metadata.

    Args:
        pipeline_id: Pipeline identifier
        pipelines_table: DynamoDB pipelines table resource

    Returns:
        int: Current version number (0 if no version exists)
    """
    try:
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            logger.warning(f"Pipeline {pipeline_id} not found")
            return 0

        item = response["Item"]

        # Check for version in glue_job_metadata
        glue_metadata = item.get("glue_job_metadata", {})
        current_version = glue_metadata.get("version", 1)

        logger.info(f"Current job version for pipeline {pipeline_id}: v{current_version}")
        return current_version

    except Exception as e:
        logger.error(f"Error getting current job version: {e}")
        return 0


def create_versioned_job_name(pipeline_id: str, version: int) -> str:
    """
    Generate versioned job name.

    Args:
        pipeline_id: Pipeline identifier
        version: Version number

    Returns:
        str: Versioned job name (e.g., agentic-data-ingestion-job-pipeline123-v2)
    """
    return f"agentic-data-ingestion-job-{pipeline_id}-v{version}"


def store_job_version_history(
    pipeline_id: str,
    version: int,
    job_name: str,
    schema_change_type: str,
    differences: list[dict[str, Any]],
    job_config: dict[str, Any],
    pipelines_table,
) -> None:
    """
    Store job version history in DynamoDB.

    Args:
        pipeline_id: Pipeline identifier
        version: Version number
        job_name: Glue job name
        schema_change_type: Type of schema change (compatible/breaking)
        differences: List of schema differences
        job_config: Complete job configuration for rollback
        pipelines_table: DynamoDB pipelines table resource
    """
    try:
        version_record = {
            "version": version,
            "job_name": job_name,
            "schema_change_type": schema_change_type,
            "differences": differences,
            "job_config": job_config,
            "created_at": datetime.now(UTC).isoformat(),
            "status": "active",
        }

        # Update pipeline metadata with version history
        pipelines_table.update_item(
            Key={"pipeline_id": pipeline_id},
            UpdateExpression="""
                SET job_version_history = list_append(
                    if_not_exists(job_version_history, :empty_list),
                    :version_record
                ),
                current_job_version = :version,
                last_updated = :timestamp
            """,
            ExpressionAttributeValues={
                ":empty_list": [],
                ":version_record": [version_record],
                ":version": version,
                ":timestamp": datetime.now(UTC).isoformat(),
            },
        )

        logger.info(f"Stored version history for pipeline {pipeline_id}, version {version}")

    except Exception as e:
        logger.error(f"Error storing job version history: {e}")
        raise


def backup_current_job_config(job_name: str) -> dict[str, Any]:
    """
    Backup current Glue job configuration for rollback capability.

    Args:
        job_name: Name of the Glue job

    Returns:
        dict: Complete job configuration
    """
    try:
        response = glue_client.get_job(JobName=job_name)

        job = response.get("Job", {})

        # Extract configuration (excluding metadata)
        job_config = {
            "Name": job.get("Name"),
            "Description": job.get("Description"),
            "Role": job.get("Role"),
            "Command": job.get("Command"),
            "DefaultArguments": job.get("DefaultArguments"),
            "MaxRetries": job.get("MaxRetries"),
            "Timeout": job.get("Timeout"),
            "WorkerType": job.get("WorkerType"),
            "NumberOfWorkers": job.get("NumberOfWorkers"),
            "GlueVersion": job.get("GlueVersion"),
            "Connections": job.get("Connections"),
            "MaxCapacity": job.get("MaxCapacity"),
            "SecurityConfiguration": job.get("SecurityConfiguration"),
            "Tags": job.get("Tags", {}),
            "backed_up_at": datetime.now(UTC).isoformat(),
        }

        logger.info(f"Backed up configuration for job {job_name}")
        return job_config

    except glue_client.exceptions.EntityNotFoundException:
        logger.warning(f"Job {job_name} does not exist, cannot backup")
        return {}
    except Exception as e:
        logger.error(f"Error backing up job configuration: {e}")
        # Deliberate fallback: return empty config so the update flow proceeds without a
        # rollback snapshot rather than aborting the job update (P21 Req 6.4).
        return {}


def update_existing_job(
    job_name: str, new_script_location: str, new_job_config: dict[str, Any], pipelines_table, pipeline_id: str
) -> dict[str, Any]:
    """
    Update existing Glue job for compatible schema changes.

    Args:
        job_name: Name of the Glue job to update
        new_script_location: S3 location of new PySpark script
        new_job_config: New job configuration
        pipelines_table: DynamoDB pipelines table resource
        pipeline_id: Pipeline identifier

    Returns:
        dict: Update result with status and details
    """
    try:
        # Check if job is running
        is_running, run_id = check_job_running_status(job_name)

        if is_running:
            error_msg = f"Cannot update job {job_name} - currently running (run ID: {run_id})"
            logger.error(error_msg)
            return {"success": False, "error": "job_running", "message": error_msg, "run_id": run_id}

        # Backup current configuration
        logger.info(f"Backing up current configuration for {job_name}")
        backup_config = backup_current_job_config(job_name)

        # Store backup in DynamoDB
        if backup_config:
            store_job_backup(pipeline_id, backup_config, pipelines_table)

        # Update job configuration
        logger.info(f"Updating Glue job {job_name} with new configuration")

        # Update script location
        new_job_config["Command"]["ScriptLocation"] = new_script_location

        # Perform update
        glue_client.update_job(JobName=job_name, JobUpdate=new_job_config)

        logger.info(f"Successfully updated job {job_name}")

        return {
            "success": True,
            "job_name": job_name,
            "action": "updated",
            "script_location": new_script_location,
            "backup_stored": bool(backup_config),
            "updated_at": datetime.now(UTC).isoformat(),
        }

    except Exception as e:
        logger.error(f"Error updating job {job_name}: {e}")
        return {"success": False, "error": "update_failed", "message": str(e)}


def create_new_versioned_job(
    pipeline_id: str, new_version: int, new_script_location: str, job_config: dict[str, Any], pipelines_table
) -> dict[str, Any]:
    """
    Create new versioned Glue job for breaking schema changes.

    Args:
        pipeline_id: Pipeline identifier
        new_version: New version number
        new_script_location: S3 location of new PySpark script
        job_config: Job configuration
        pipelines_table: DynamoDB pipelines table resource

    Returns:
        dict: Creation result with new job details
    """
    try:
        # Generate versioned job name
        new_job_name = create_versioned_job_name(pipeline_id, new_version)

        logger.info(f"Creating new versioned job: {new_job_name}")

        # Update job name and script location
        job_config["Name"] = new_job_name
        job_config["Command"]["ScriptLocation"] = new_script_location
        job_config["Description"] = f"{job_config.get('Description', '')} (Version {new_version})"

        # Add version tag
        if "Tags" not in job_config:
            job_config["Tags"] = {}
        job_config["Tags"]["Version"] = str(new_version)
        job_config["Tags"]["PipelineId"] = pipeline_id

        # Create new job
        glue_client.create_job(**job_config)

        logger.info(f"Successfully created versioned job {new_job_name}")

        # Update pipeline metadata to point to new version
        pipelines_table.update_item(
            Key={"pipeline_id": pipeline_id},
            UpdateExpression="""
                SET glue_job_metadata.job_name = :job_name,
                    glue_job_metadata.version = :version,
                    glue_job_metadata.script_location = :script_location,
                    glue_job_metadata.updated_at = :timestamp,
                    current_job_version = :version
            """,
            ExpressionAttributeValues={
                ":job_name": new_job_name,
                ":version": new_version,
                ":script_location": new_script_location,
                ":timestamp": datetime.now(UTC).isoformat(),
            },
        )

        return {
            "success": True,
            "job_name": new_job_name,
            "version": new_version,
            "action": "created_new_version",
            "script_location": new_script_location,
            "created_at": datetime.now(UTC).isoformat(),
        }

    except Exception as e:
        logger.error(f"Error creating versioned job: {e}")
        return {"success": False, "error": "creation_failed", "message": str(e)}


def store_job_backup(pipeline_id: str, backup_config: dict[str, Any], pipelines_table) -> None:
    """
    Store job backup configuration in DynamoDB for rollback.

    Args:
        pipeline_id: Pipeline identifier
        backup_config: Complete job configuration backup
        pipelines_table: DynamoDB pipelines table resource
    """
    try:
        pipelines_table.update_item(
            Key={"pipeline_id": pipeline_id},
            UpdateExpression="""
                SET job_backup = :backup,
                    job_backup_timestamp = :timestamp
            """,
            ExpressionAttributeValues={":backup": backup_config, ":timestamp": datetime.now(UTC).isoformat()},
        )

        logger.info(f"Stored job backup for pipeline {pipeline_id}")

    except Exception as e:
        logger.error(f"Error storing job backup: {e}")
        # Don't fail the operation if backup storage fails
        logger.warning("Continuing without backup storage")


def rollback_job_to_previous_version(pipeline_id: str, pipelines_table) -> dict[str, Any]:
    """
    Rollback Glue job to previous configuration.

    Args:
        pipeline_id: Pipeline identifier
        pipelines_table: DynamoDB pipelines table resource

    Returns:
        dict: Rollback result with status and details
    """
    try:
        # Get pipeline metadata
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return {"success": False, "error": "pipeline_not_found", "message": f"Pipeline {pipeline_id} not found"}

        item = response["Item"]

        # Get backup configuration
        backup_config = item.get("job_backup")

        if not backup_config:
            return {
                "success": False,
                "error": "no_backup_found",
                "message": f"No backup configuration found for pipeline {pipeline_id}",
            }

        # Get current job name
        glue_metadata = item.get("glue_job_metadata", {})
        current_job_name = glue_metadata.get("job_name")

        if not current_job_name:
            return {
                "success": False,
                "error": "no_job_found",
                "message": f"No Glue job found for pipeline {pipeline_id}",
            }

        # Check if job is running
        is_running, run_id = check_job_running_status(current_job_name)

        if is_running:
            return {
                "success": False,
                "error": "job_running",
                "message": f"Cannot rollback - job {current_job_name} is currently running (run ID: {run_id})",
                "run_id": run_id,
            }

        # Restore backup configuration
        logger.info(f"Rolling back job {current_job_name} to previous configuration")

        # Remove backup-specific metadata
        backup_config_clean = {k: v for k, v in backup_config.items() if k != "backed_up_at"}

        # Update job
        glue_client.update_job(JobName=current_job_name, JobUpdate=backup_config_clean)

        logger.info(f"Successfully rolled back job {current_job_name}")

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

        return {
            "success": True,
            "job_name": current_job_name,
            "action": "rolled_back",
            "backup_timestamp": backup_config.get("backed_up_at"),
            "rolled_back_at": datetime.now(UTC).isoformat(),
        }

    except Exception as e:
        logger.error(f"Error rolling back job: {e}")
        return {"success": False, "error": "rollback_failed", "message": str(e)}


def handle_schema_evolution(
    pipeline_id: str,
    old_schema: dict[str, Any],
    new_schema: dict[str, Any],
    new_script_location: str,
    job_config: dict[str, Any],
    pipelines_table,
) -> dict[str, Any]:
    """
    Main function to handle schema evolution and job updates.

    This function:
    1. Classifies schema change (compatible/breaking/identical)
    2. Checks if job is running
    3. Updates existing job (compatible) or creates new version (breaking)
    4. Stores version history

    Args:
        pipeline_id: Pipeline identifier
        old_schema: Previous schema definition
        new_schema: New schema definition
        new_script_location: S3 location of new PySpark script
        job_config: Job configuration for new/updated job
        pipelines_table: DynamoDB pipelines table resource

    Returns:
        dict: Result with action taken and details
    """
    try:
        logger.info(f"Handling schema evolution for pipeline {pipeline_id}")

        # Classify schema change
        change_type, differences = classify_schema_change(old_schema, new_schema)

        logger.info(f"Schema change type: {change_type.value}")
        logger.info(f"Differences: {json.dumps(differences, indent=2)}")

        # Get current version
        current_version = get_current_job_version(pipeline_id, pipelines_table)
        current_job_name = (
            f"agentic-data-ingestion-job-{pipeline_id}"
            if current_version == 0
            else create_versioned_job_name(pipeline_id, current_version)
        )

        result = {}

        if change_type == SchemaChangeType.IDENTICAL:
            # No changes needed
            logger.info("Schema is identical, no job update needed")
            result = {
                "success": True,
                "action": "no_change_needed",
                "job_name": current_job_name,
                "change_type": change_type.value,
                "message": "Schema is identical to previous version",
            }

        elif change_type == SchemaChangeType.COMPATIBLE:
            # Update existing job
            logger.info("Schema change is compatible, updating existing job")
            result = update_existing_job(
                current_job_name, new_script_location, job_config, pipelines_table, pipeline_id
            )
            result["change_type"] = change_type.value
            result["differences"] = differences

            # Store version history
            if result.get("success"):
                store_job_version_history(
                    pipeline_id,
                    current_version,
                    current_job_name,
                    change_type.value,
                    differences,
                    job_config,
                    pipelines_table,
                )

        elif change_type == SchemaChangeType.BREAKING:
            # Create new versioned job
            logger.info("Schema change is breaking, creating new versioned job")
            new_version = current_version + 1

            result = create_new_versioned_job(
                pipeline_id, new_version, new_script_location, job_config, pipelines_table
            )
            result["change_type"] = change_type.value
            result["differences"] = differences
            result["previous_version"] = current_version

            # Store version history
            if result.get("success"):
                store_job_version_history(
                    pipeline_id,
                    new_version,
                    result["job_name"],
                    change_type.value,
                    differences,
                    job_config,
                    pipelines_table,
                )

        return result

    except Exception as e:
        logger.error(f"Error handling schema evolution: {e}")
        return {"success": False, "error": "schema_evolution_failed", "message": str(e)}
