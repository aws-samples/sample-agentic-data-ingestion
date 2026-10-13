# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: RecordFailure canonical step-name attribution (P28 R8.2, task 14.2).

The RecordFailure Lambda is deployed WITHOUT the shared layer (deliberate —
its dependency surface is stdlib + boto3 so a RecordFailure crash can never
mask the original failure). It therefore vendors a minimal canonicalization
table mirroring ``shared/step_names``. These tests:

- pin PROGRESS_ORDER's step attributions to canonical names (the legacy
  ``schema_mapping_approval`` / ``code_approval`` gate aliases are retired),
- verify the tolerant ``canonicalize_step_name`` parser, and
- cross-check the vendored table against ``shared/step_names`` so the two
  copies cannot silently drift.

Validates: P28 Requirements 8.2, 8.5
"""

import json
import os
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_record_failure_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_record_failure")
)
sys.path.insert(0, _record_failure_dir)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")))

sys.modules.pop("lambda_function", None)
import lambda_function as _mod

import step_names as shared_step_names


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _step_failure_cause(step_name: str) -> str:
    payload = json.dumps({"step_name": step_name, "error_type": "permanent", "message": "boom", "details": {}})
    return json.dumps({"errorMessage": payload, "errorType": "StepFailure"})


# ── PROGRESS_ORDER carries canonical names ───────────────────────────


def test_progress_order_step_names_are_canonical():
    """Every PROGRESS_ORDER attribution parses to itself via shared from_any."""
    for _key, step in _mod.PROGRESS_ORDER:
        assert str(shared_step_names.from_any(step)) == step, (
            f"PROGRESS_ORDER step {step!r} is not the canonical spelling"
        )


def test_legacy_gate_aliases_are_gone_from_progress_order():
    steps = {step for _key, step in _mod.PROGRESS_ORDER}
    assert "schema_mapping_approval" not in steps
    assert "code_approval" not in steps
    assert "check_schema_mapping_approval" in steps
    assert "check_code_approval" in steps


def test_progress_order_result_keys_unchanged():
    """The first elements are ASL ResultPath keys — an SFN contract that the
    canonical-name adoption must NOT touch."""
    assert [key for key, _step in _mod.PROGRESS_ORDER] == [
        "file_validation_result",
        "schema_extract_result",
        "schema_compare_result",
        "schema_mapping_result",
        "schema_mapping_approval",
        "create_code_result",
        "test_code_result",
        "test_review_approval",
        "code_approval",
        "create_infrastructure_result",
        "create_glue_pipeline_result",
        "create_quality_checks_result",
        "run_pipeline_result",
    ]


def test_inference_at_gate_names_canonical_check_step():
    """Failing right after schema_mapping names the canonical gate."""
    state = {
        "file_validation_result": {},
        "schema_extract_result": {},
        "schema_compare_result": {},
        "schema_mapping_result": {},
    }
    assert _mod.infer_failed_step("States.Timeout", state) == "check_schema_mapping_approval"


# ── canonicalize_step_name (vendored from_any subset) ────────────────


class TestCanonicalizeStepName:
    def test_canonical_names_pass_through(self):
        assert _mod.canonicalize_step_name("schema_mapping") == "schema_mapping"
        assert _mod.canonicalize_step_name("run_pipeline") == "run_pipeline"

    def test_sfn_state_ids_normalize(self):
        assert _mod.canonicalize_step_name("CreateCode") == "create_code"
        assert _mod.canonicalize_step_name("SchemaMapping") == "schema_mapping"

    def test_legacy_variants_normalize(self):
        assert _mod.canonicalize_step_name("CodeGeneration") == "create_code"
        assert _mod.canonicalize_step_name("CodeTesting") == "test_code"
        assert _mod.canonicalize_step_name("code_approval") == "check_code_approval"
        assert _mod.canonicalize_step_name("schema_mapping_approval") == "check_schema_mapping_approval"
        assert _mod.canonicalize_step_name("infrastructure") == "create_infrastructure"
        assert _mod.canonicalize_step_name("first_run") == "create_quality_checks"

    def test_unknown_passes_through_and_empty_is_unknown(self):
        """Tolerant by design — RecordFailure never raises on a bad name."""
        assert _mod.canonicalize_step_name("something_else") == "something_else"
        assert _mod.canonicalize_step_name("") == "unknown"
        assert _mod.canonicalize_step_name(None) == "unknown"


def test_vendored_table_matches_shared_step_names():
    """The layer-free vendored lookup must agree with shared/step_names for
    every canonical name, SFN state id, and cataloged legacy alias."""
    for step in shared_step_names.StepName:
        assert _mod.canonicalize_step_name(step.value) == str(step)
        assert _mod.canonicalize_step_name(shared_step_names.to_sfn_state_name(step)) == str(step)
    for alias, step in shared_step_names._LEGACY_ALIASES.items():
        assert _mod.canonicalize_step_name(alias) == str(step)


# ── failed_step attribution through parse_error ──────────────────────


def test_step_failure_payload_with_legacy_variant_is_canonicalized():
    parsed = _mod.parse_error({"Error": "StepFailure", "Cause": _step_failure_cause("CodeGeneration")}, {})
    assert parsed["failed_step"] == "create_code"
    assert parsed["failed_step_source"] == "step_failure"


def test_step_failure_payload_with_canonical_name_unchanged():
    parsed = _mod.parse_error({"Error": "StepFailure", "Cause": _step_failure_cause("test_code")}, {})
    assert parsed["failed_step"] == "test_code"
