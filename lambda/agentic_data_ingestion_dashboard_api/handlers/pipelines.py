#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pipeline management handlers for dashboard API — re-export façade.

P21 Task 7.2 (module decomposition, Req 4.1/4.3/4.5/4.6): the original
~4,800-line ``pipelines.py`` was split into cohesive sibling modules under
``handlers/`` while keeping this module as a thin façade. It re-exports the
public API surface so the dashboard ``router.py`` (its top-level
``from handlers.pipelines import …`` line and the many lazy
``from handlers.pipelines import X`` calls) and the existing test suite keep
resolving the same callables unchanged.

The behaviour-bearing code now lives in:

* ``pipelines_jdbc``      — JDBC/RDS source+target validation & Glue-connection
                            provisioning.
* ``pipelines_logs``      — CloudWatch step-log retrieval + in-memory cache.
* ``pipelines_crud``      — pipeline lifecycle CRUD + provisioning/scheduling
                            helpers.
* ``pipelines_runs``      — run / execution operations.
* ``pipelines_artifacts`` — generated-code / test artifact retrieval.
* ``pipelines_mappings``  — schema-mapping operations.

Importing the names here is behaviour-preserving: each callable resolves to the
exact same object defined in its sibling module.
"""

# CRUD + provisioning/scheduling helpers.
# Generated-code / test artifact retrieval.
from .pipelines_artifacts import (
    _read_s3_object,
    fetch_s3_content,
    get_code_by_version,
    get_pipeline_code_versions,
    get_tests_by_version,
)
from .pipelines_crud import (
    _ALLOWED_SOURCE_TYPES,
    _ensure_s3_eventbridge_enabled,
    _manage_schedule_rule,
    _provision_pipeline_resources,
    archive_pipeline,
    create_pipeline,
    delete_pipeline,
    edit_pipeline,
    get_archived_pipelines,
    get_pipeline_details,
    get_pipeline_file_count,
    get_pipeline_schema_history,
    get_pipelines,
    restore_pipeline,
)

# JDBC/RDS validation + Glue-connection provisioning.
from .pipelines_jdbc import (
    _JDBC_MANUAL_REQUIRED_FIELDS,
    _JDBC_READ_MODES,
    _JDBC_SOURCE_INCREMENTAL_REQUIRED_FIELDS,
    _JDBC_SOURCE_REQUIRED_FIELDS,
    _JDBC_WATERMARK_TYPES,
    SECRET_ARN_RE,
    SG_ID_RE,
    SUBNET_ID_RE,
    _augment_glue_connection_config,
    _build_jdbc_source_physical_requirements,
    _build_jdbc_target_physical_requirements,
    _derive_engine_from_jdbc_url,
    _normalise_sg_list,
    _provision_jdbc_runtime_crawler,
    _provision_jdbc_source_connection,
    _provision_jdbc_target_connection,
    _validate_jdbc_source_config,
    _validate_jdbc_source_config_glue_connection,
    _validate_jdbc_source_config_manual,
    _validate_jdbc_source_config_select_rds,
    _validate_jdbc_source_required_common,
    _validate_jdbc_target_config,
    _validate_jdbc_target_config_manual,
    _validate_jdbc_target_config_select_rds,
)

# CloudWatch step-log retrieval + in-memory cache.
from .pipelines_logs import (
    LAMBDA_LOG_GROUPS,
    _cache,
    _clean_log_message,
    _get_agentcore_step_logs,
    _get_cached,
    _set_cache,
    get_step_logs,
)

# Schema-mapping operations.
from .pipelines_mappings import (
    rerun_mapping,
    save_mapping_hints,
    update_mappings,
)

# Run / execution operations.
from .pipelines_runs import (
    cancel_run,
    get_pipeline_executions,
    get_queued_files,
    get_run_artifacts,
    get_run_details,
    get_run_neighbours,
    retry_run,
    run_now,
    trigger_batch,
)

__all__ = [
    # CRUD
    "create_pipeline",
    "edit_pipeline",
    "archive_pipeline",
    "restore_pipeline",
    "delete_pipeline",
    "get_pipelines",
    "get_archived_pipelines",
    "get_pipeline_details",
    "get_pipeline_file_count",
    "get_pipeline_schema_history",
    "_ensure_s3_eventbridge_enabled",
    "_manage_schedule_rule",
    "_provision_pipeline_resources",
    "_ALLOWED_SOURCE_TYPES",
    # Runs
    "get_pipeline_executions",
    "get_queued_files",
    "get_run_details",
    "cancel_run",
    "get_run_artifacts",
    "get_run_neighbours",
    "run_now",
    "trigger_batch",
    "retry_run",
    # Artifacts
    "get_code_by_version",
    "get_tests_by_version",
    "fetch_s3_content",
    "get_pipeline_code_versions",
    "_read_s3_object",
    # Mappings
    "rerun_mapping",
    "save_mapping_hints",
    "update_mappings",
    # Logs / cache
    "get_step_logs",
    "_clean_log_message",
    "_get_agentcore_step_logs",
    "_get_cached",
    "_set_cache",
    "_cache",
    "LAMBDA_LOG_GROUPS",
    # JDBC validation / provisioning
    "_normalise_sg_list",
    "_validate_jdbc_source_config",
    "_validate_jdbc_source_required_common",
    "_derive_engine_from_jdbc_url",
    "_augment_glue_connection_config",
    "_validate_jdbc_source_config_select_rds",
    "_validate_jdbc_source_config_glue_connection",
    "_validate_jdbc_source_config_manual",
    "_validate_jdbc_target_config",
    "_validate_jdbc_target_config_manual",
    "_validate_jdbc_target_config_select_rds",
    "_build_jdbc_source_physical_requirements",
    "_provision_jdbc_source_connection",
    "_provision_jdbc_runtime_crawler",
    "_build_jdbc_target_physical_requirements",
    "_provision_jdbc_target_connection",
    "SECRET_ARN_RE",
    "SUBNET_ID_RE",
    "SG_ID_RE",
    "_JDBC_SOURCE_REQUIRED_FIELDS",
    "_JDBC_SOURCE_INCREMENTAL_REQUIRED_FIELDS",
    "_JDBC_READ_MODES",
    "_JDBC_WATERMARK_TYPES",
    "_JDBC_MANUAL_REQUIRED_FIELDS",
]
