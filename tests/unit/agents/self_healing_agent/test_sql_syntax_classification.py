# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A database rejecting the generated SQL is classified, and deliberately NOT retried.

`run-2026-09-22-20-35-04-689a0a7b` failed with::

    An error occurred while calling o157.load. You have an error in your SQL
    syntax; check the manual that corresponds to your MySQL server version for
    the right syntax to use near '"appdb"."customers" WHERE 1=0' at line 1

`classify_error` matched none of its four categories, so the run was labelled
`unknown` and the dashboard told the operator only that "Error type 'unknown'
requires manual intervention" — true, and useless. The cause was a codegen
defect (the target engine's identifier quoting applied to a MySQL source read),
which the message describes precisely and the classifier could not see.

Two halves, and the second is the load-bearing one:

1. The message classifies as `sql_syntax_error`, across engines.
2. That class routes to `flag_for_review` and starts NO re-run.

Half 2 is why this is not simply added to the rerun branch beside
`schema_mismatch`. Template rendering is deterministic: the same pipeline record
renders the same SQL, so a re-run re-emits the identical rejected statement. An
auto-retry would burn one Glue execution per attempt and never converge. The
rerun branch is sound for `schema_mismatch` / `type_cast_error` because those
regenerate the TRANSFORM from mappings that may since have changed; the source
read's SQL has no such input.

Classification ordering is also pinned here: `sql_syntax_error` is appended last
in `_ERROR_PATTERNS`, which is matched first-hit-wins, so it must not reclassify
anything the four existing categories already claim.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "self_healing_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

# Unique sys.modules name so the agent test files do not fight over one module
# identity — same pattern as test_heal_applied_notification.py.
_mock_glue = MagicMock()
_mock_sfn = MagicMock()
_mock_boto3 = MagicMock()
_mock_boto3.client.side_effect = lambda svc, **kw: {
    "glue": _mock_glue,
    "stepfunctions": _mock_sfn,
    "logs": MagicMock(),
}.get(svc, MagicMock())
_mock_boto3.resource.return_value = MagicMock()

_mock_runtime = MagicMock()
_mock_runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn
_mock_bedrock_agentcore = MagicMock()
_mock_bedrock_agentcore.runtime = _mock_runtime

sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": _mock_bedrock_agentcore,
        "bedrock_agentcore.runtime": _mock_runtime,
        "boto3": _mock_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
    },
):
    _spec = importlib.util.spec_from_file_location("sha_sql_syntax_main", MAIN_PATH)
    sha = importlib.util.module_from_spec(_spec)
    sys.modules["sha_sql_syntax_main"] = sha
    _spec.loader.exec_module(sha)
    sha._glue_client = _mock_glue
    sha._sfn_client = _mock_sfn

_HANDLER_MODULE_MOCKS = {
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(),
    "env_utils": MagicMock(),
}

_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
_NORMAL_FV_ROW = {"run_id": "run-1", "step_name": "file_validation", "triggered_by": "s3_upload"}
# Inside the re-run safe set, so if anything DID try to re-run, the partial-write
# guard would not be the thing stopping it. That is what makes the
# "no re-run started" assertions below attributable to the classification.
_SAFE_RECORD = {
    "pipeline_id": "pipe-1",
    "source_type": "JDBC",
    "source_bucket": "src-bkt",
    "target_config": {"type": "NEW_S3_TABLE_BUCKET", "write_mode": "overwrite"},
}

# The verbatim Glue ErrorMessage from run-2026-09-22-20-35-04-689a0a7b.
_PRODUCTION_MESSAGE = (
    "An error occurred while calling o157.load. You have an error in your SQL "
    "syntax; check the manual that corresponds to your MySQL server version for "
    'the right syntax to use near \'"appdb"."customers" WHERE 1=0\' at line 1'
)


def _run_handler(error_message: str) -> dict:
    state_table = MagicMock()
    state_table.get_item.return_value = {"Item": _NORMAL_FV_ROW}
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": _SAFE_RECORD}
    dynamo = MagicMock()
    dynamo.Table.side_effect = lambda name: pipelines_table if name == _PIPELINES_TABLE else state_table
    sha._dynamodb = dynamo
    sha._require_env = MagicMock(side_effect=lambda name, default=None: default)

    _mock_sfn.reset_mock()
    _mock_sfn.start_execution.side_effect = None
    _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:::execution/x"}

    with (
        patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
        patch.object(sha, "_notify_heal_applied"),
        patch.object(sha, "_notify_rerun_declined"),
    ):
        return sha.handler(
            {
                "run_id": "run-1",
                "pipeline_id": "pipe-1",
                "step_name": "glue_etl",
                "error_message": error_message,
                "job_name": "agentic-data-ingestion-pipe-1-etl",
            }
        )


class TestClassification:
    def test_the_production_message_is_classified(self):
        assert sha.classify_error(_PRODUCTION_MESSAGE) == "sql_syntax_error"

    @pytest.mark.parametrize(
        "message",
        [
            "You have an error in your SQL syntax; check the manual",  # MySQL / MariaDB
            'ERROR: syntax error at or near "customers"',  # PostgreSQL
            "Incorrect syntax near the keyword 'FROM'.",  # SQL Server
            "ORA-00933: SQL command not properly ended",  # Oracle
            "ORA-00936: missing expression",  # Oracle
            "ORA-00900: invalid SQL statement",  # Oracle
            "java.sql.SQLSyntaxErrorException: bad statement",  # JDBC wrapper
        ],
    )
    def test_each_engine_dialect_is_recognised(self, message: str):
        assert sha.classify_error(message) == "sql_syntax_error"

    def test_matching_is_case_insensitive(self):
        assert sha.classify_error("you have an error in your sql syntax") == "sql_syntax_error"


class TestOrderingDoesNotStealExistingCategories:
    """Appended last, first-hit-wins — the four existing classes are untouched."""

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("AnalysisException: cannot resolve 'customer_id'", "schema_mismatch"),
            ("column not found: customer_id", "schema_mismatch"),
            ("java.lang.OutOfMemoryError: Java heap space", "oom_error"),
            ("Container killed by YARN for exceeding memory", "oom_error"),
            ("NumberFormatException: For input string: 'abc'", "type_cast_error"),
            ("cannot cast string to integer", "type_cast_error"),
            ("AccessDeniedException: not authorized", "permission_error"),
            ("Something completely unexpected happened", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_existing_classification_unchanged(self, message: str, expected: str):
        assert sha.classify_error(message) == expected

    def test_a_schema_error_mentioning_syntax_stays_a_schema_error(self):
        """The specific collision the append order guards against.

        Spark's AnalysisException text sometimes contains the word "syntax"
        while the fault is genuinely a missing column.
        """
        message = "AnalysisException: cannot resolve 'cust_id'; check your syntax and input columns"
        assert sha.classify_error(message) == "schema_mismatch"


class TestItIsNotAutoRetried:
    """The load-bearing half: a deterministic codegen defect must not loop."""

    def test_routes_to_flag_for_review(self):
        result = _run_handler(_PRODUCTION_MESSAGE)
        assert result["action"] == "flag_for_review"
        assert result["error_classification"] == "sql_syntax_error"

    def test_starts_no_step_functions_execution(self):
        _run_handler(_PRODUCTION_MESSAGE)
        assert _mock_sfn.start_execution.call_count == 0, (
            "a re-run was started for a deterministic codegen defect — it would "
            "render the identical SQL and fail identically, burning one Glue "
            "execution per attempt"
        )

    def test_reports_the_rerun_as_skipped_and_not_started(self):
        result = _run_handler(_PRODUCTION_MESSAGE)
        assert result["rerun_status"] == "skipped"
        assert result["rerun_started"] is False

    def test_the_remediation_text_names_the_cause_and_says_it_is_not_retried(self):
        """Replaces "Error type 'unknown' requires manual intervention"."""
        result = _run_handler(_PRODUCTION_MESSAGE)
        description = result.get("remediation_applied") or result.get("description") or ""
        assert "code-generation defect" in description
        assert "NOT retried" in description
        # Points at the actual cause rather than leaving the operator to guess.
        assert "backticks" in description

    def test_a_permission_error_also_gets_a_specific_description(self):
        """The generic fallback now covers only genuinely unrecognised errors."""
        result = _run_handler("AccessDeniedException: not authorized to perform s3:GetObject")
        description = result.get("remediation_applied") or result.get("description") or ""
        assert "grant" in description.lower()
        assert "requires manual intervention" not in description

    def test_an_unrecognised_error_keeps_the_generic_fallback(self):
        result = _run_handler("Something completely unexpected happened")
        description = result.get("remediation_applied") or result.get("description") or ""
        assert "unknown" in description
        assert "manual intervention" in description
