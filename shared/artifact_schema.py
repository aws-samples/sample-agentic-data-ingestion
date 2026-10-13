# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Standardized output_data schema for step Lambda artifacts.

Every step Lambda writes output_data as a DynamoDB Map with this consistent schema:
    artifact_type (str), primary_id (str), status (str), summary (dict), s3_uris (dict)

Usage:
    from artifact_schema import validate_output_data, create_output_data

    output_data = create_output_data(
        artifact_type='code_generation',
        primary_id=code_version_id,
        status='success',
        summary={'line_count': 85, 'template_used': 'glue_job_s3_tables.py.j2'},
        s3_uris={'code_file': f's3://{bucket}/{key}'},
    )
    validate_output_data(output_data)
"""

ARTIFACT_TYPES = frozenset(
    {
        "file_validation",
        "schema_extraction",
        "schema_comparison",
        "schema_mapping",
        "code_generation",
        "test_results",
        "infrastructure",
        "glue_pipeline",
        "quality_checks",
        "run_pipeline",
    }
)

VALID_STATUSES = frozenset({"success", "failed", "skipped"})

REQUIRED_KEYS = frozenset({"artifact_type", "primary_id", "status", "summary", "s3_uris"})


def validate_output_data(output_data: dict) -> None:
    """Validate that output_data conforms to the standardized schema.

    Raises ValueError if any check fails.
    """
    if not isinstance(output_data, dict):
        raise ValueError(f"output_data must be a dict, got {type(output_data).__name__}")

    missing = REQUIRED_KEYS - set(output_data.keys())
    if missing:
        raise ValueError(f"output_data missing required keys: {sorted(missing)}")

    artifact_type = output_data["artifact_type"]
    if not isinstance(artifact_type, str):
        raise ValueError(f"artifact_type must be str, got {type(artifact_type).__name__}")
    if artifact_type not in ARTIFACT_TYPES:
        raise ValueError(f"artifact_type '{artifact_type}' is not valid. Must be one of: {sorted(ARTIFACT_TYPES)}")

    primary_id = output_data["primary_id"]
    if not isinstance(primary_id, str):
        raise ValueError(f"primary_id must be str, got {type(primary_id).__name__}")

    status = output_data["status"]
    if not isinstance(status, str):
        raise ValueError(f"status must be str, got {type(status).__name__}")
    if status not in VALID_STATUSES:
        raise ValueError(f"status '{status}' is not valid. Must be one of: {sorted(VALID_STATUSES)}")

    summary = output_data["summary"]
    if not isinstance(summary, dict):
        raise ValueError(f"summary must be a dict, got {type(summary).__name__}")

    s3_uris = output_data["s3_uris"]
    if not isinstance(s3_uris, dict):
        raise ValueError(f"s3_uris must be a dict, got {type(s3_uris).__name__}")


def create_output_data(
    artifact_type: str,
    primary_id: str,
    status: str,
    summary: dict,
    s3_uris: dict,
) -> dict:
    """Create a validated output_data dict.

    Raises ValueError if any argument is invalid.
    """
    output_data = {
        "artifact_type": artifact_type,
        "primary_id": primary_id,
        "status": status,
        "summary": summary,
        "s3_uris": s3_uris,
    }
    validate_output_data(output_data)
    return output_data
