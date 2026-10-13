#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Common utilities and shared resources for dashboard API handlers"""

import os
import sys
from collections.abc import Mapping
from typing import Any

import boto3
from aws_lambda_powertools import Logger

# Add shared directory to path
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# `get_aws_account_id` is re-exported so dashboard handlers obtain the canonical
# resolver via `.common` rather than redefining it or calling STS ad hoc (P21 Req 5.3/5.4).
from env_utils import get_aws_account_id, require_env

# Re-export shared response helpers so existing dashboard call sites
# (`from handlers.common import create_response`, etc.) keep working unchanged
# while the canonical implementation lives in shared/response_utils.py (P21 Req 5).
from response_utils import (
    DecimalEncoder,
    create_response,
    handle_error,
)

# Structured logging via Powertools
logger = Logger(service="agentic_data_ingestion")
logger.append_keys(step_name="DashboardAPI")

# Environment variables
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
STATE_TABLE_NAME = require_env("STATE_TABLE", "agentic_data_ingestion_state")
MAPPINGS_TABLE_NAME = require_env("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
PIPELINES_TABLE_NAME = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
GENERATED_CODE_TABLE_NAME = require_env("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
TEST_RESULTS_TABLE_NAME = require_env("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
APPROVALS_TABLE_NAME = require_env("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
EXECUTIONS_TABLE_NAME = require_env("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
AUDIT_TABLE_NAME = require_env("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
AGENT_TRACES_TABLE_NAME = require_env("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")

# ── Chronological ordering on the executions table (p48 C1) ──────────────
# ``(pipeline_id, started_at)``, ``ProjectionType.ALL``
# (``deploy-cdk/lib/data-stack.ts``). Spelled ONCE, here, so every reader that
# delegates ordering to it cannot drift from the deployed index name — a mistyped
# ``IndexName`` is a ``ValidationException`` at request time, not an import error,
# and this file's own error arms turn that into a 200 with empty lists or a 404,
# so the failure would be silent. ``test_run_history_ordering.py``'s
# ``test_the_handler_names_the_index_by_the_deployed_constant`` compares this
# string against ``data-stack.ts`` rather than against a second copy of it.
#
# **Why here rather than in ``handlers/pipelines_runs.py``, where task 3.3 put it.**
# Task 5.6 gave it a second reader — ``handlers/data_preview.py`` — and
# ``router.py`` imports each handler module **lazily, inside its own route branch**
# (``:97-99`` for the preview route). Importing ``pipelines_runs`` from
# ``data_preview`` for one string would pull that module and its own imports
# (``pipelines_artifacts``, ``pipelines_logs``, ``run_cursor``, ``load_outcome``,
# ``step_names``) into the preview route's cold start. ``.common`` is already
# imported by both, and it is where ``EXECUTIONS_TABLE_NAME`` and
# ``executions_table`` live — an index name on that table is the same kind of fact.
# ``pipelines_runs`` re-binds the name via ``from .common import …``, so
# ``runs_handler.RUNS_BY_TIME_INDEX`` still resolves for its existing tests.
#
# *Rejected: a new module for it.* ``run_cursor.py`` earned its own module by
# needing to import **nothing** from ``.common`` (which calls ``require_env`` at
# import time). A bare string has no such constraint, and both readers already pay
# ``.common``'s import cost.
RUNS_BY_TIME_INDEX = "RunsByTimeIndex"

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)
logs_client = boto3.client("logs", region_name=AWS_REGION)
state_table = dynamodb.Table(STATE_TABLE_NAME)
mappings_table = dynamodb.Table(MAPPINGS_TABLE_NAME)
pipelines_table = dynamodb.Table(PIPELINES_TABLE_NAME)
generated_code_table = dynamodb.Table(GENERATED_CODE_TABLE_NAME)
test_results_table = dynamodb.Table(TEST_RESULTS_TABLE_NAME)
approvals_table = dynamodb.Table(APPROVALS_TABLE_NAME)
executions_table = dynamodb.Table(EXECUTIONS_TABLE_NAME)
audit_table = dynamodb.Table(AUDIT_TABLE_NAME)


def is_phantom_execution_row(row: Mapping[str, Any]) -> bool:
    """True when no pipeline step has ever reported state for this row.

    ``execution_updater`` sets BOTH ``current_step`` and ``steps_status`` on its
    first write for a run (``execution_updater/lambda_function.py:238-253``,
    issued at ``:340-345``), so a row carrying neither cannot describe a run
    that has recorded state. The test is structural, not heuristic: it asks
    whether the writer that runs when a step reports has ever touched the row.

    **It tests two fields and not one on purpose.** Both skip records carry
    ``current_step`` and no ``steps_status`` — the duplicate-content skip
    (``pipeline_trigger/lambda_function.py:226-247``,
    ``current_step="duplicate_check"``) and the scheduled-contention skip
    (``:487-511``, ``"concurrency_gate"`` / ``"pipeline_lock_gate"``) — so both
    survive this predicate. That is deliberate, not incidental:
    ``LGR-21a1b5b-4`` (template / ratified) shaped the Skipped_Run_Record to
    interleave chronologically with real runs, rejecting a ``skipped-`` prefix
    on exactly that ground. A predicate keyed on ``steps_status`` alone would
    hide those rows and undo that decision.
    """
    return not row.get("current_step") and not row.get("steps_status")


# Public surface re-exported for sibling handlers (`from .common import …`).
# Listing the canonical-resolver and shared-response-helper re-exports in
# __all__ both documents them and marks them as intentional re-exports so the
# linter does not flag them as unused (P21 Req 5).
__all__ = [
    "AGENT_TRACES_TABLE_NAME",
    "APPROVALS_TABLE_NAME",
    "AUDIT_TABLE_NAME",
    "AWS_REGION",
    "EXECUTIONS_TABLE_NAME",
    "GENERATED_CODE_TABLE_NAME",
    "MAPPINGS_TABLE_NAME",
    "PIPELINES_TABLE_NAME",
    "RUNS_BY_TIME_INDEX",
    "STATE_TABLE_NAME",
    "TEST_RESULTS_TABLE_NAME",
    "DecimalEncoder",
    "approvals_table",
    "audit_table",
    "create_response",
    "dynamodb",
    "executions_table",
    "generated_code_table",
    "get_aws_account_id",
    "handle_error",
    "is_phantom_execution_row",
    "logger",
    "logs_client",
    "mappings_table",
    "pipelines_table",
    "require_env",
    "s3_client",
    "state_table",
    "test_results_table",
]
