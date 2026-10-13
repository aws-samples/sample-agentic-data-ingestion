# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: shared fixture environment completeness (Property 4).

Feature: testing-overhaul, Property 4: Shared fixture environment completeness

For any Lambda handler module that reads ``os.environ``, the
``mock_lambda_env`` fixture sets that variable to a matching value.

This test cross-references the ``mock_lambda_env`` fixture's env_vars dict
against the consolidated env var summary from analysis-0.1 to verify
completeness.

**Validates: Requirements 3.5**
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Consolidated env var list from analysis-0.1-lambda-handlers.md
# ---------------------------------------------------------------------------
# These are all environment variables read by Lambda handlers via os.environ.
# The mock_lambda_env fixture must set every one of them.

REQUIRED_ENV_VARS: set[str] = {
    # DynamoDB table names (used by 14+ handlers)
    "STATE_TABLE",
    "PIPELINES_TABLE",
    "EXECUTIONS_TABLE",
    "APPROVALS_TABLE",
    "MAPPINGS_TABLE",
    "GENERATED_CODE_TABLE",
    "TEST_RESULTS_TABLE",
    "ERRORS_TABLE",
    "TASK_TOKENS_TABLE",
    "WS_CONNECTIONS_TABLE",
    "NOTIFICATIONS_TABLE",
    # WebSocket
    "WS_API_ENDPOINT",
    # Step Functions
    "STATE_MACHINE_ARN",
    # Concurrency
    "MAX_CONCURRENT_EXECUTIONS",
    # File validation
    "MAX_FILE_SIZE_BYTES",
    # Bedrock
    "BEDROCK_MODEL_ID",
    # S3 buckets
    "TARGET_SCHEMA_BUCKET",
    "GENERATED_CODE_BUCKET",
    "TEST_OUTPUTS_BUCKET",
    # AgentCore
    "AGENTCORE_ENDPOINT",
    # Glue
    "S3_TABLES_JAR_PATH",
    # Feature flags
    "MCP_ENABLED",
    # SNS
    "SNS_TOPIC_ARN",
    "CLOUDWATCH_ALARM_TOPIC",
    # Timeouts
    "DEFAULT_TIMEOUT_HOURS",
    # AWS region
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
}

# Env vars grouped by handler for traceability
ENV_VAR_HANDLER_MAP: dict[str, list[str]] = {
    "STATE_TABLE": [
        "pipeline_trigger",
        "file_validation",
        "schema_extract",
        "schema_compare",
        "schema_mapping",
        "create_code",
        "create_infrastructure",
        "create_glue_pipeline",
        "create_quality_checks",
        "run_pipeline",
        "approval_handler",
        "test_code",
        "dashboard_api",
    ],
    "PIPELINES_TABLE": [
        "schema_compare",
        "schema_mapping",
        "create_code",
        "create_infrastructure",
        "create_glue_pipeline",
        "create_quality_checks",
        "run_pipeline",
        "job_completion",
        "execution_updater",
        "approval_config",
        "test_code",
        "dashboard_api",
    ],
    "EXECUTIONS_TABLE": ["pipeline_trigger", "execution_updater"],
    "APPROVALS_TABLE": [
        "schema_mapping",
        "approval_handler",
        "approval_config",
        "approval_timeout",
        "dashboard_api",
    ],
    "MAPPINGS_TABLE": [
        "schema_mapping",
        "create_code",
        "create_quality_checks",
        "test_code",
        "dashboard_api",
    ],
    "GENERATED_CODE_TABLE": [
        "create_code",
        "create_glue_pipeline",
        "test_code",
        "dashboard_api",
    ],
    "TEST_RESULTS_TABLE": ["test_code", "dashboard_api"],
    "ERRORS_TABLE": ["execution_updater"],
    "TASK_TOKENS_TABLE": ["approval_handler"],
    "WS_CONNECTIONS_TABLE": ["ws_broadcast", "ws_connect", "ws_disconnect"],
    "WS_API_ENDPOINT": ["ws_broadcast"],
    "STATE_MACHINE_ARN": ["pipeline_trigger"],
    "MAX_CONCURRENT_EXECUTIONS": ["pipeline_trigger"],
    "MAX_FILE_SIZE_BYTES": ["file_validation"],
    "BEDROCK_MODEL_ID": ["schema_mapping"],
    "TARGET_SCHEMA_BUCKET": ["schema_mapping"],
    "GENERATED_CODE_BUCKET": ["create_code"],
    "TEST_OUTPUTS_BUCKET": ["test_code"],
    "AGENTCORE_ENDPOINT": ["create_code", "create_glue_pipeline"],
    "S3_TABLES_JAR_PATH": ["create_glue_pipeline"],
    "MCP_ENABLED": ["create_glue_pipeline"],
    "SNS_TOPIC_ARN": ["approval_handler", "approval_timeout"],
    "CLOUDWATCH_ALARM_TOPIC": ["approval_timeout"],
    "DEFAULT_TIMEOUT_HOURS": ["approval_handler"],
    "AWS_DEFAULT_REGION": [
        "schema_extract",
        "create_code",
        "create_infrastructure",
        "create_glue_pipeline",
        "run_pipeline",
        "job_completion",
        "dashboard_api",
    ],
    "AWS_REGION": [
        "schema_extract",
        "create_code",
        "create_infrastructure",
        "create_glue_pipeline",
        "run_pipeline",
        "job_completion",
        "dashboard_api",
    ],
}


# ---------------------------------------------------------------------------
# Property 4: For any required env var, mock_lambda_env sets it
# ---------------------------------------------------------------------------

# Strategy: pick any required env var
_env_var_strategy = st.sampled_from(sorted(REQUIRED_ENV_VARS))


@given(env_var=_env_var_strategy)
@settings(
    max_examples=len(REQUIRED_ENV_VARS),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_mock_lambda_env_sets_required_var(env_var: str, mock_lambda_env):
    """**Validates: Requirements 3.5**

    For any Lambda handler env var from analysis-0.1, the mock_lambda_env
    fixture sets it to a non-empty string value.
    """
    assert env_var in mock_lambda_env, (
        f"mock_lambda_env is missing '{env_var}' (used by: {ENV_VAR_HANDLER_MAP.get(env_var, ['unknown'])})"
    )
    assert isinstance(mock_lambda_env[env_var], str), f"mock_lambda_env['{env_var}'] should be a string"
    assert len(mock_lambda_env[env_var]) > 0, f"mock_lambda_env['{env_var}'] should not be empty"


def test_mock_lambda_env_covers_all_required_vars(mock_lambda_env):
    """**Validates: Requirements 3.5**

    The mock_lambda_env fixture must set ALL required env vars from
    the analysis-0.1 consolidated summary.
    """
    missing = REQUIRED_ENV_VARS - set(mock_lambda_env.keys())
    assert not missing, f"mock_lambda_env is missing {len(missing)} required env vars: {sorted(missing)}"


def test_mock_lambda_env_table_vars_match_known_tables(mock_lambda_env):
    """**Validates: Requirements 3.5**

    DynamoDB table name env vars should reference known agentic_data_ingestion table names.
    """
    table_env_vars = [
        "STATE_TABLE",
        "PIPELINES_TABLE",
        "EXECUTIONS_TABLE",
        "APPROVALS_TABLE",
        "MAPPINGS_TABLE",
        "GENERATED_CODE_TABLE",
        "TEST_RESULTS_TABLE",
        "ERRORS_TABLE",
        "TASK_TOKENS_TABLE",
        "WS_CONNECTIONS_TABLE",
        "NOTIFICATIONS_TABLE",
    ]
    for var in table_env_vars:
        value = mock_lambda_env.get(var, "")
        assert value.startswith("agentic_data_ingestion_"), (
            f"{var}='{value}' should reference an agentic_data_ingestion_ table"
        )


def test_mock_lambda_env_region_vars_are_valid(mock_lambda_env):
    """**Validates: Requirements 3.5**

    AWS region env vars should be valid AWS region strings.
    """
    for var in ("AWS_DEFAULT_REGION", "AWS_REGION"):
        value = mock_lambda_env.get(var, "")
        assert value, f"{var} should be set"
        # Basic region format check: xx-xxxx-N
        assert "-" in value, f"{var}='{value}' doesn't look like an AWS region"
