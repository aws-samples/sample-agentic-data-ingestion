# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET pipeline code versions — payload shape.

Covers ``handlers.pipelines_artifacts.get_pipeline_code_versions``, whose
response body is::

    {
        pipeline_id: string,
        code_versions: CodeVersion[],
        total_count: int,
    }

This is the endpoint's **first** payload-shape coverage. p34 task 3.2 searched
``tests/`` for ``code_versions`` and found zero hits, which is why a field that
lost its producer could go stale unnoticed. Scope is deliberately the shape
only, not the whole handler.

**Why the forbidden-key set exists.** ``approval_required`` used to appear on
every ``code_versions[]`` entry, read from the generated-code table with a
``.get(…, False)`` default. p34 task 3.2 deleted its producer —
``create_code.determine_approval_requirement`` and its six sites — under
amended, ratified R3.4, and the default absorbed the deletion silently: no
exception, but a constant ``False`` on rows written after the change and a
stale stored value on rows written before it. The field was dropped rather than
re-sourced from ``test_code``'s state row, because ``LGR-f077f1b-1``
(template / ratified) rejected *renaming* that same field for exactly this
reason — a field with no consumer is dead weight. Asserting the absence, in the
``SUCCESS_FORBIDDEN_KEYS`` style task 3.2 established in
``test_create_code_contract.py``, makes the removal a contract in its own right
rather than a silent drop (``LGR-5906ec6-2``, template / ratified).
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# ---------------------------------------------------------------------------
# Environment variables required at import time
# ---------------------------------------------------------------------------
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PIPELINE_ID = "pipe-codever-001"

TOP_LEVEL_KEYS = {"pipeline_id", "code_versions", "total_count"}

CODE_VERSION_KEYS = {
    "code_version_id",
    "run_id",
    "code_s3_uri",
    "generation_timestamp",
    "generation_status",
    "template_used",
    "regeneration_attempt",
    "code_metadata",
    "agent_reasoning",
    "required_parameters",
    "optional_parameters",
    "parameter_defaults",
}

# Keys this endpoint must NOT publish on a code version. See the module
# docstring: the producer of this one was deleted by p34 task 3.2, and it is
# not re-sourced (`LGR-f077f1b-1`, template / ratified).
CODE_VERSION_FORBIDDEN_KEYS = {
    "approval_required",
}


def _make_stored_item(**overrides) -> dict:
    """A generated-code table row as the GSI query returns it."""
    item = {
        "code_version_id": "cv-001",
        "pipeline_id": PIPELINE_ID,
        "run_id": "run-001",
        "code_s3_uri": "s3://bucket/code/cv-001.py",
        "generation_timestamp": "2026-08-06T13:37:54Z",
        "generation_status": "completed",
        "template_used": "glue_job_s3_tables.py.j2",
        "regeneration_attempt": 0,
        "code_metadata": {"line_count": 42},
        "agent_reasoning": "selected the S3 Tables template",
        "required_parameters": ["source_path"],
        "optional_parameters": [],
        "parameter_defaults": {},
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# Fixture — patch the generated-code table
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_boto(monkeypatch):
    from handlers import common as _common

    mock_table = MagicMock()
    monkeypatch.setattr(_common, "generated_code_table", mock_table)
    _patch_boto.generated_code_table = mock_table
    yield


def _set_query_items(items: list) -> None:
    _patch_boto.generated_code_table.query.return_value = {"Items": items}


def _call(pipeline_id: str = PIPELINE_ID) -> tuple[int, dict]:
    # Imported through handlers.pipelines, which re-exports the name for
    # router/test compatibility (P21 Task 7.2 module decomposition).
    from handlers.pipelines import get_pipeline_code_versions

    response = get_pipeline_code_versions(pipeline_id)
    return response["statusCode"], json.loads(response["body"])


# ===========================================================================
# Contract tests
# ===========================================================================


class TestCodeVersionsTopLevelShape:
    def test_response_has_exactly_the_top_level_keys(self):
        _set_query_items([_make_stored_item()])
        status, body = _call()

        assert status == 200
        assert set(body.keys()) == TOP_LEVEL_KEYS

    def test_total_count_matches_code_versions_length(self):
        _set_query_items([_make_stored_item(), _make_stored_item(code_version_id="cv-002")])
        _, body = _call()

        assert body["total_count"] == len(body["code_versions"]) == 2

    def test_empty_result_is_an_empty_list_not_an_error(self):
        _set_query_items([])
        status, body = _call()

        assert status == 200
        assert body["code_versions"] == []
        assert body["total_count"] == 0


class TestCodeVersionEntryShape:
    def test_each_entry_has_exactly_the_code_version_keys(self):
        _set_query_items([_make_stored_item()])
        _, body = _call()

        for entry in body["code_versions"]:
            assert set(entry.keys()) == CODE_VERSION_KEYS

    def test_entry_omits_the_forbidden_keys(self):
        _set_query_items([_make_stored_item()])
        _, body = _call()

        for entry in body["code_versions"]:
            present = CODE_VERSION_FORBIDDEN_KEYS & set(entry.keys())
            assert not present, f"Keys this endpoint must not publish: {present}"

    def test_stored_approval_required_is_not_passed_through(self):
        """A pre-p34 row carrying the attribute must not resurface it.

        Rows written before p34 task 3.2 still hold a stored
        ``approval_required``. Dropping the field from the response means those
        stale values stop being served, not merely that new rows default to
        ``False`` — so the assertion is made against a row that HAS the
        attribute, which is the only shape that can tell the two apart.
        """
        _set_query_items([_make_stored_item(approval_required=True)])
        _, body = _call()

        entry = body["code_versions"][0]
        assert "approval_required" not in entry
        # The rest of the entry is unaffected by the removal.
        assert set(entry.keys()) == CODE_VERSION_KEYS
