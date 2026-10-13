# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: API response contract conformance (Property 7).

Feature: testing-overhaul, Property 7: API response contract conformance

For any API endpoint and valid backing DynamoDB data, the handler's response
validates against the JSON Schema without errors.

Uses @given with hypothesis strategies to generate random pipeline/execution
data, monkeypatch.setattr on handler module objects to mock DynamoDB tables,
and jsonschema.validate() to verify responses.

Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.8
"""

import json
import os
import sys
from unittest.mock import MagicMock

import jsonschema
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment setup — must happen before handler imports
# ---------------------------------------------------------------------------
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
os.environ.setdefault("ERRORS_TABLE", "agentic_data_ingestion_errors")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

sys.modules.pop("lambda_function", None)

from handlers import approvals as _approvals_mod
from handlers import common as _common
from handlers.approvals import get_approvals, get_pending_approvals
from handlers.pipelines import get_pipeline_executions, get_pipelines, get_queued_files

from .schemas import (
    APPROVAL_SCHEMA,
    PAGINATED_EXECUTIONS_SCHEMA,
    PAGINATED_PIPELINES_SCHEMA,
    QUEUED_FILES_RESPONSE_SCHEMA,
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_safe_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), blacklist_characters="\x00"),
    min_size=1,
    max_size=20,
)

_pipeline_id_st = st.from_regex(r"pipe-[a-z0-9]{4,8}", fullmatch=True)
_run_id_st = st.from_regex(r"run-[a-z0-9]{4,8}", fullmatch=True)
_iso_ts_st = st.from_regex(
    r"2025-0[1-9]-[012][0-9]T[01][0-9]:[0-5][0-9]:[0-5][0-9]Z",
    fullmatch=True,
)
_status_st = st.sampled_from(["RUNNING", "SUCCEEDED", "FAILED", "WAITING_APPROVAL"])
_approval_type_st = st.sampled_from(["schema_mapping", "code_generation", "quality_checks"])
_approval_status_st = st.sampled_from(["pending", "approved", "rejected"])


@st.composite
def pipeline_item_st(draw):
    """Generate a realistic pipeline DynamoDB item."""
    return {
        "pipeline_id": draw(_pipeline_id_st),
        "pipeline_name": draw(_safe_text),
        "status": draw(st.sampled_from(["active", "archived", "provisioning"])),
        "target_schema_name": draw(_safe_text),
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "pipeline_description": draw(_safe_text),
        "created_at": draw(_iso_ts_st),
        "updated_at": draw(_iso_ts_st),
        "glue_job_metadata": {},
    }


@st.composite
def execution_item_st(draw):
    """Generate a realistic execution DynamoDB item."""
    return {
        "pipeline_id": draw(_pipeline_id_st),
        "run_id": draw(_run_id_st),
        "status": draw(_status_st),
        "started_at": draw(_iso_ts_st),
        "updated_at": draw(_iso_ts_st),
        "current_step": "schema_extract",
        "file_name": draw(_safe_text) + ".csv",
    }


@st.composite
def queued_file_item_st(draw):
    """Generate a realistic queued file DynamoDB item."""
    return {
        "pipeline_id": draw(_pipeline_id_st),
        "run_id": draw(_run_id_st),
        "status": "QUEUED",
        "started_at": draw(_iso_ts_st),
        "file_name": draw(_safe_text) + ".csv",
        "source_key": "uploads/" + draw(_safe_text) + ".csv",
    }


@st.composite
def approval_item_st(draw):
    """Generate a realistic approval DynamoDB item."""
    return {
        "approval_id": "appr-" + draw(st.from_regex(r"[a-z0-9]{6}", fullmatch=True)),
        "pipeline_key": draw(_pipeline_id_st),
        "pipeline_id": draw(_pipeline_id_st),
        "run_id": draw(_run_id_st),
        "approval_type": draw(_approval_type_st),
        "status": draw(_approval_status_st),
        "created_at": draw(_iso_ts_st),
        "decided_at": None,
        "decided_by": None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


def _patch_all_tables(monkeypatch, pipeline_items=None, exec_items=None, approval_items=None):
    """Patch DynamoDB table objects on handler modules with mocks."""
    mock_pipelines = MagicMock()
    mock_exec = MagicMock()
    mock_state = MagicMock()
    mock_approvals = MagicMock()
    mock_mappings = MagicMock()
    mock_dynamodb = MagicMock()

    mock_pipelines.scan.return_value = {"Items": pipeline_items or []}
    mock_exec.query.return_value = {"Items": exec_items or []}
    mock_state.get_item.return_value = {}
    mock_state.query.return_value = {"Items": [], "ScannedCount": 0}
    # P28 R17.1: approval reads now Query GSIs instead of scanning
    mock_approvals.query.return_value = {"Items": approval_items or []}
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    # Patch on common module
    monkeypatch.setattr(_common, "pipelines_table", mock_pipelines)
    monkeypatch.setattr(_common, "executions_table", mock_exec)
    monkeypatch.setattr(_common, "state_table", mock_state)
    monkeypatch.setattr(_common, "approvals_table", mock_approvals)
    monkeypatch.setattr(_common, "mappings_table", mock_mappings)
    monkeypatch.setattr(_common, "dynamodb", mock_dynamodb)

    # P21 Task 7.2: get_pipelines lives in handlers.pipelines_crud while
    # get_pipeline_executions / get_queued_files live in handlers.pipelines_runs.
    # Patch the table handles on both owning modules so every relocated handler
    # sees the mocks (module references only — behaviour-preserving).
    from handlers import pipelines_crud as _crud
    from handlers import pipelines_runs as _runs

    for _mod in (_crud, _runs):
        for _attr, _val in (
            ("pipelines_table", mock_pipelines),
            ("executions_table", mock_exec),
            ("state_table", mock_state),
            ("approvals_table", mock_approvals),
            ("mappings_table", mock_mappings),
            ("dynamodb", mock_dynamodb),
        ):
            if hasattr(_mod, _attr):
                monkeypatch.setattr(_mod, _attr, _val)

    # Patch on approvals module
    monkeypatch.setattr(_approvals_mod, "approvals_table", mock_approvals)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestGetPipelinesContractConformance:
    """Property 7 — get_pipelines validates against PAGINATED_PIPELINES_SCHEMA."""

    @given(items=st.lists(pipeline_item_st(), min_size=0, max_size=3))
    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_response_validates_against_schema(self, items, monkeypatch):
        """**Validates: Requirements 4.1**

        For any list of pipeline items, the handler response validates
        against PAGINATED_PIPELINES_SCHEMA.
        """
        _patch_all_tables(monkeypatch, pipeline_items=items)
        status, body = _parse(get_pipelines())
        assert status == 200
        jsonschema.validate(body, PAGINATED_PIPELINES_SCHEMA)


class TestGetPipelineExecutionsContractConformance:
    """Property 7 — get_pipeline_executions validates against PAGINATED_EXECUTIONS_SCHEMA."""

    @given(
        pipeline_id=_pipeline_id_st,
        items=st.lists(execution_item_st(), min_size=0, max_size=3),
    )
    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_response_validates_against_schema(self, pipeline_id, items, monkeypatch):
        """**Validates: Requirements 4.2**

        For any pipeline_id and list of execution items, the handler response
        validates against PAGINATED_EXECUTIONS_SCHEMA.
        """
        for item in items:
            item["pipeline_id"] = pipeline_id

        _patch_all_tables(monkeypatch, exec_items=items)
        status, body = _parse(get_pipeline_executions(pipeline_id))
        assert status == 200
        jsonschema.validate(body, PAGINATED_EXECUTIONS_SCHEMA)


class TestGetQueuedFilesContractConformance:
    """Property 7 — get_queued_files validates against QUEUED_FILES_RESPONSE_SCHEMA."""

    @given(
        pipeline_id=_pipeline_id_st,
        items=st.lists(queued_file_item_st(), min_size=0, max_size=3),
    )
    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_response_validates_against_schema(self, pipeline_id, items, monkeypatch):
        """**Validates: Requirements 4.3**

        For any pipeline_id and list of queued file items, the handler response
        validates against QUEUED_FILES_RESPONSE_SCHEMA.
        """
        for item in items:
            item["pipeline_id"] = pipeline_id

        _patch_all_tables(monkeypatch, exec_items=items)
        status, body = _parse(get_queued_files(pipeline_id))
        assert status == 200
        jsonschema.validate(body, QUEUED_FILES_RESPONSE_SCHEMA)


class TestGetApprovalsContractConformance:
    """Property 7 — get_approvals validates each item against APPROVAL_SCHEMA."""

    @given(items=st.lists(approval_item_st(), min_size=0, max_size=3))
    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_response_validates_against_schema(self, items, monkeypatch):
        """**Validates: Requirements 4.5, 4.6**

        For any list of approval items, the handler response validates
        each item against APPROVAL_SCHEMA.
        """
        _patch_all_tables(monkeypatch, approval_items=items)
        status, body = _parse(get_approvals({}))
        assert status == 200
        assert isinstance(body, list)
        for approval in body:
            jsonschema.validate(approval, APPROVAL_SCHEMA)


class TestGetPendingApprovalsContractConformance:
    """Property 7 — get_pending_approvals validates against APPROVAL_SCHEMA."""

    @given(items=st.lists(approval_item_st(), min_size=0, max_size=3))
    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_response_validates_against_schema(self, items, monkeypatch):
        """**Validates: Requirements 4.4, 4.8**

        For any list of approval items, the pending approvals handler response
        validates each item against APPROVAL_SCHEMA.
        """
        # Only include pending items
        for item in items:
            item["status"] = "pending"

        _patch_all_tables(monkeypatch, approval_items=items)
        status, body = _parse(get_pending_approvals())
        assert status == 200
        assert isinstance(body, list)
        for approval in body:
            jsonschema.validate(approval, APPROVAL_SCHEMA)
