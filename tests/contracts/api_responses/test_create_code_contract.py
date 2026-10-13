# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: create_code Lambda response shape matches orchestration-stack expectations.

The Step Functions orchestration stack reads these fields from $.create_code_result:
  - code_version_id  (string, used by TestCode step)
  - code_s3_uri      (string, used by TestCode step)
  - statusCode       (int, 200 on success)
  - run_id           (string)
  - pipeline_id      (string)

CORRECTED — p34 design §4.7 row 11, task 3.2. This list used to end with
"approval_required (bool, used by CheckCodeApproval choice)". That was false
when it was written: `CheckCodeApproval` has never read
`$.create_code_result.approval_required` — it reads
`$.test_code_result.approval_required`. Leaving the line would let the next
reader re-derive from this contract test the exact belief p34 exists to
correct. Under amended, ratified R3.4 this step publishes **no**
code-generation approval field at all; the decision belongs to `test_code`,
the step whose result the gate does read (design D2, `LGR-f077f1b-1`,
template / ratified).

On failure the handler RAISES StepFailure (P28 R1.1) so the SFN task fails and
Retry/Catch fire; str(exc) is the stable Failure_Record JSON (step_name,
error_type, message, details) that the Catch path parses from the Cause.
"""

import json
import os
import sys
import unittest.mock as mock

import pytest

# Add Lambda dir and shared to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

# Patch heavy dependencies before importing
_mods = {
    "strands": mock.MagicMock(),
    "strands_mcp_factory": mock.MagicMock(),
    "agentcore_client": mock.MagicMock(),
    "eventbridge_utils": mock.MagicMock(
        get_eventbridge_publishers=mock.MagicMock(return_value=(mock.MagicMock(), mock.MagicMock(), mock.MagicMock())),
    ),
    "code_validator": mock.MagicMock(
        validate_generated_code=mock.MagicMock(return_value={"is_valid": True, "errors": [], "summary": {}}),
        extract_code_only=mock.MagicMock(side_effect=lambda x: x),
    ),
    "env_utils": mock.MagicMock(
        get_aws_account_id=mock.MagicMock(return_value="123456789012"),
        require_env=mock.MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock prompt"),
    ),
    "parameter_extractor": mock.MagicMock(
        extract_required_parameters=mock.MagicMock(
            return_value={
                "required_params": [],
                "optional_params": [],
                "parameter_defaults": {},
            }
        ),
    ),
}

with mock.patch.dict("sys.modules", _mods):
    # Remove cached lambda_function from other test files to force fresh import
    sys.modules.pop("lambda_function", None)
    import lambda_function as lf

# Required keys the orchestration stack reads via JsonPath
SUCCESS_REQUIRED_KEYS = {
    "statusCode",
    "run_id",
    "pipeline_id",
    "code_version_id",
    "code_s3_uri",
    # p34 §4.7 row 11: "approval_required" removed — see the module docstring.
    "next_step",
}

# p34 §4.7 row 11, task 3.2: keys this step must NOT publish. Amended, ratified
# R3.4 — the Create_Code_Step publishes no code-generation approval field, and
# the state machine reads no approval field from $.create_code_result. Asserted
# as an absence rather than dropped silently, so the removal is a contract in
# its own right (`LGR-5906ec6-2`, template / ratified).
SUCCESS_FORBIDDEN_KEYS = {
    "approval_required",
}

# Keys the RecordFailure/Catch path reads from the serialized StepFailure
# (P28 R1.1 stable serialization contract).
FAILURE_SERIALIZATION_KEYS = {
    "step_name",
    "error_type",
    "message",
    "details",
}


def _make_success_response() -> dict:
    """Simulate a successful create_code response."""
    return {
        "statusCode": 200,
        "run_id": "run-123",
        "pipeline_id": "pipe-456",
        "code_version_id": "cv-789",
        "code_s3_uri": "s3://bucket/code/cv-789.py",
        "agent_session_id": "sess-abc",
        "regeneration_attempt": 0,
        # P28 R13.1 (task 11.2): code_quality_score removed — it was the
        # keyword-count score with no honest measurement behind it.
        "template_used": "glue_job_s3_tables.py.j2",
        # p34 §4.7 row 11: "approval_required": False dropped from the fixture —
        # the handler no longer produces the key, so a fixture carrying it would
        # simulate a response shape the producer cannot emit.
        "next_step": "code_testing",
    }


def test_success_response_has_required_keys():
    resp = _make_success_response()
    missing = SUCCESS_REQUIRED_KEYS - set(resp.keys())
    assert not missing, f"Missing keys in success response: {missing}"

    # REVERSED per amended, ratified R3.4 — p34 §4.7 row 11, task 3.2. Name kept
    # verbatim. The subject (this response shape is the state machine's contract)
    # survives; what changed is that the approval field is no longer part of it.
    present = SUCCESS_FORBIDDEN_KEYS & set(resp.keys())
    assert not present, f"Keys the Create_Code_Step must not publish: {present}"


def test_success_response_types():
    resp = _make_success_response()
    assert isinstance(resp["statusCode"], int)
    assert isinstance(resp["code_version_id"], str)
    assert isinstance(resp["code_s3_uri"], str)
    # REVERSED per amended, ratified R3.4 — p34 §4.7 row 8, task 3.2. Name kept
    # verbatim. This asserted `isinstance(resp["approval_required"], bool)`; the
    # key is now absent, so its type is not a thing this contract can state.
    assert "approval_required" not in resp
    assert resp["code_s3_uri"].startswith("s3://")


def test_failure_raises_step_failure_with_stable_serialization():
    """On failure the handler raises StepFailure (P28 R1.1) — never a 500 payload.

    The serialized exception (what SFN carries in the Cause) must contain the
    stable Failure_Record keys, with the deterministic error-{run_id}
    correlation id preserved in details.
    """
    with (
        mock.patch.object(lf, "get_pipeline_metadata", side_effect=RuntimeError("boom")),
        mock.patch.object(lf, "pipeline_runs_table", mock.MagicMock()),
        mock.patch.object(lf, "publish_pipeline_state_change"),
        pytest.raises(lf.StepFailure) as exc_info,
    ):
        lf.lambda_handler({"run_id": "run-123", "pipeline_id": "pipe-456"}, None)

    serialized = json.loads(str(exc_info.value))
    missing = FAILURE_SERIALIZATION_KEYS - set(serialized.keys())
    assert not missing, f"Missing keys in serialized StepFailure: {missing}"
    assert serialized["step_name"] == "create_code"
    assert serialized["error_type"] == "permanent"
    assert serialized["message"] == "boom"
    assert serialized["details"]["code_version_id"] == "error-run-123"


def test_template_selection_covers_all_target_types():
    """Template selection must cover the target types used in production.

    Migrated from the deprecated ``TEMPLATE_MAP`` alias (deleted in P28 task
    15.2) to ``_select_template`` over ``TEMPLATE_RULES`` — the selection
    surface production code actually uses. Asserted through the
    lambda_function re-export so the entry module keeps carrying the name.
    """
    expected = {
        "s3_tables": "glue_job_s3_tables.py.j2",
        "glue_catalog": "glue_job_glue_catalog.py.j2",
        "jdbc": "glue_job_jdbc.py.j2",
        "rds_database": "glue_job_jdbc.py.j2",  # P14 codegen fix
        "s3_parquet": "glue_job_s3_parquet.py.j2",
    }
    for target_type, template in expected.items():
        assert lf._select_template("S3_UPLOAD", target_type) == template
    # JDBC-source pipelines route to the JDBC source template for any target.
    assert lf._select_template("JDBC", "s3_tables") == "glue_job_jdbc_source.py.j2"
