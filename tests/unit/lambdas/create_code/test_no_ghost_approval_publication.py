# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_code publishes NO fabricated (ghost) approval event.

P28 R19.5: the handler used to fabricate an approval_id
(``{run_id}_code_generation_{timestamp}``) and publish an 'Approval Status
Change' event for it when approval was required — an id that never had an
approvals-table record. The approval_handler owns the real approval record
and publishes the honest event with the real id.

Validates: Requirements 19.5
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir and shared dir to path
_create_code_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_create_code",
    )
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

# Stub heavy dependencies (same harness as test_duration_metrics.py)
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(
        extract_code_only=MagicMock(return_value="# code"),
        validate_generated_code=MagicMock(return_value={"is_valid": True, "errors": [], "summary": {}}),
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={
                "required_params": [],
                "optional_params": [],
                "parameter_defaults": {},
            }
        )
    ),
    "artifact_schema": MagicMock(
        create_output_data=MagicMock(return_value={}),
    ),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _happy_path_patches():
    """Context-manager patches for a successful handler run.

    p34 design §4.7 row 10 (task 3.2): the ``approval_required`` parameter and
    its ``patch.object(_mod, "determine_approval_requirement", ...)`` entry are
    GONE. Design D2 deletes that function, and ``patch.object`` raises
    ``AttributeError`` on a deleted attribute — so keeping the patch would break
    this file at COLLECTION rather than as a readable assertion failure. Nothing
    in this file's subject depended on the patch: the handler no longer makes an
    approval decision at all.
    """
    return [
        patch.object(_mod, "publish_pipeline_state_change"),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(
            _mod,
            "get_pipeline_metadata",
            return_value={
                "target_config": {
                    "type": "s3_tables",
                    "bucket_name": "b",
                    "namespace_name": "n",
                    "table_name": "t",
                }
            },
        ),
        patch.object(
            _mod,
            "pipeline_runs_table",
            MagicMock(
                get_item=MagicMock(
                    return_value={
                        "Item": {
                            "extracted_schema": '{"properties": {}}',
                        }
                    }
                )
            ),
        ),
        patch.object(
            _mod,
            "mappings_table",
            # p33 task 4.6 harness amendment, NOT an assertion change. The two
            # schema hashes were absent here and the handler substituted
            # `str(uuid.uuid4())` for each; requirement 2.9 makes their absence a
            # StepFailure instead, because a generated identifier matches no
            # schema. `store_mapping_separately` writes both on every real record
            # (`schema_mapping/lambda_function.py:1166-1169`), so the fixture was
            # under-specified relative to the live writer. This file is about
            # ghost approval publication, so it needs a record that resolves.
            MagicMock(
                get_item=MagicMock(
                    return_value={
                        "Item": {
                            "mapping_id": "m1",
                            "source_schema_hash": "src-hash-m1",
                            "target_schema_hash": "tgt-hash-m1",
                        }
                    }
                )
            ),
        ),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(
            _mod,
            "generate_code",
            return_value="# code\ndf = spark.read.json('s3://b/k')\n",
        ),
        patch.object(
            _mod,
            "validate_generated_code",
            return_value={"is_valid": True, "errors": [], "summary": {}},
        ),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
    ]


def _run_handler():
    event = {
        "run_id": "test-run-ghost",
        "pipeline_id": "test-pipe-ghost",
        "mapping_id": "test-map-ghost",
    }
    patches = _happy_path_patches()
    with (
        patches[0] as mock_publish_state,
        patches[1] as mock_publish_approval,
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
    ):
        result = _mod.lambda_handler(event, None)
    return result, mock_publish_state, mock_publish_approval


def test_approval_required_publishes_no_ghost_approval_event():
    """NO 'Approval Status Change' event is published from this step.

    **Name kept verbatim; one assertion REVERSED — p34 design §4.7 row 8, task
    3.2** (``LGR-5906ec6-2``, ``LGR-60c4985-6``, both template / ratified).

    Surviving subject, unchanged: **P28 R19.5 / ``LGR-620555b-1``** — this step
    publishes no fabricated approval *event*. That is what
    ``mock_publish_approval.assert_not_called()`` proves, and it is untouched.

    What reversed: this test used to run the handler with
    ``approval_required=True`` and assert ``result["approval_required"] is
    True``. Under amended, ratified R3.4 the handler publishes **no**
    code-generation approval field, so the assertion is that the key is
    **absent** — and there is no longer a ``True``/``False`` axis to run it on,
    because the decision moved to ``test_code`` (design D2,
    ``LGR-f077f1b-1``). The old field was never read: ``CheckCodeApproval``
    reads ``$.test_code_result.approval_required``.
    """
    result, mock_publish_state, mock_publish_approval = _run_handler()

    assert result["statusCode"] == 200
    # REVERSED per amended R3.4: the key is gone, not True and not False.
    assert "approval_required" not in result
    # The ghost publication is gone — the approval_handler owns the honest event.
    mock_publish_approval.assert_not_called()
    # The regular pipeline-state event still goes out.
    mock_publish_state.assert_called_once()


def test_no_approval_required_also_publishes_no_approval_event():
    """The same holds on the run that used to be the not-required case.

    **Name kept verbatim; one assertion REVERSED — p34 design §4.7 row 8, task
    3.2.** Its sibling above carries the full note. Kept as a separate test
    rather than folded into that one: it is a held assertion, and the fact that
    both of its former branches now describe one behaviour is the reversal
    itself, not a reason to drop half the proof.
    """
    result, _, mock_publish_approval = _run_handler()

    assert result["statusCode"] == 200
    # REVERSED per amended R3.4: absent, where this once asserted `is False`.
    assert "approval_required" not in result
    mock_publish_approval.assert_not_called()


def test_no_fabricated_approval_id_pattern_in_source():
    """The fabricated '{run_id}_code_generation_{timestamp}' id is gone from the handler."""
    import inspect

    source = inspect.getsource(_mod)
    assert "_code_generation_{int(datetime" not in source
