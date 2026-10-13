# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: the regeneration prompt is not a byte-identical retry (p32 2.16).

Defect 4's second half (bugfix.md 1.15): reviewer feedback is gated on
``regeneration_attempt > 0`` at ``create_code/lambda_function.py:476``, and
nothing ever set that counter — so ``get_regeneration_feedback(run_id)`` was
never called and a retry rebuilt an identical prompt, including through the
injection slot ``transform_generation.j2:5-9`` reserves for exactly this.

Task 15 wired the counter (the Step Functions loop-back edge in
``orchestration-stack.ts`` increments it), which makes the gate fire. This file
pins the two halves of 2.16 that the gate firing is supposed to produce:

1. **The gate fires, and only on a regeneration.** ``regeneration_attempt > 0``
   reads the prior iteration's feedback; ``= 0`` does not read it at all
   (3.18 — adding the key must not change first-attempt behaviour).
2. **The resulting prompt differs.** Asserted through the REAL Jinja2 render,
   not a mocked template loader: a mock would make "the prompt differs" a
   statement about the mock. `LGR-28b4d75-5` (template) is the rule — a test
   whose expectation is transcribed from the wrong side of the wire cannot
   detect the two sides drifting.

**V7 (`LGR-d3c81fc-1`).** ``transform_generation.j2`` renders under
``StrictUndefined``, so a referenced-but-unsupplied variable raises at render
time — mid-pipeline, not at synth. The absent/present/empty render cases live
in ``test_transform_prompt.py`` (task T11a). What is added here is the budget
consequence: T11a left the template at ~498 of the 500 tokens
``test_prompt_under_500_tokens_for_10_fields`` asserts, so the test below pins
that the feedback slot costs NOTHING on a first attempt — otherwise this
change would spend headroom it does not have.

**V8.** ``get_regeneration_feedback``'s failure modes go live in production for
the first time once this gate fires. They are pinned at
``test_read_path_errors.py`` (``details["operation"] == "get_regeneration_feedback"``);
this file deliberately does not restate them.

**Validates: Requirements 2.16; 3.18; V7**
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _CREATE_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Imported OUTSIDE the stub block below, so `load_prompt_template` is the real
# loader and `transform_generation.j2` is really rendered. This module touches
# no AWS: the strands Agent constructor arrives via `agent_factory`.
import transform_generation as _tg

# Stub heavy dependencies for the handler-level tests (mirrors
# test_mapping_freshness.py). `prompt_template_loader` is deliberately NOT
# stubbed — see the module docstring.
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
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "artifact_schema": MagicMock(create_output_data=MagicMock(return_value={})),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    sys.modules["transform_generation"] = _tg
    yield


# The feedback_context shape test_code stores. Written by
# `store_regeneration_feedback` (test_code/lambda_function.py) verbatim from
# `analyze_test_results_for_regeneration`'s return, and read back by
# `mapping_resolution.get_regeneration_feedback`. The cross-module round-trip
# is asserted in tests/unit/lambdas/test_code/test_store_regeneration_feedback.py
# and tests/unit/lambdas/create_code/test_mapping_resolution_regeneration_feedback.py
# — this is the consumer side.
_STORED_FEEDBACK = {
    "summary": "Code regeneration needed (attempt 1/2): 1 reviewer issue(s), 2 failed check(s)",
    "issues_found": ["review verdict: fail", "executed checks failed: 2 out of 6"],
    "reviewer_issues": [
        {
            "severity": "major",
            "category": "Correctness",
            "description": "to_date called without an explicit format string",
        }
    ],
    "recommendations": ["Address every reviewer issue listed above and fix failing checks"],
}

_MAPPINGS = [
    {
        "source_field": "signup_date",
        "source_type": "string",
        "target_field": "signup_date",
        "target_type": "date",
        "transform": "to_date",
    },
    {
        "source_field": "name",
        "source_type": "string",
        "target_field": "name_clean",
        "target_type": "string",
        "transform": "trim",
    },
]


def _capture_prompt(regeneration_feedback):
    """Render the transform prompt for real and return it.

    Goes through `generate_transform_with_ai`, so the render context is the one
    production supplies — not one this test assembled.
    """
    seen: list[str] = []

    def _agent_factory(**_kwargs):
        def _agent(prompt_text):
            seen.append(prompt_text)
            return 'df_transformed = df.withColumn("name_clean", trim(col("name")))'

        return _agent

    _tg.generate_transform_with_ai(
        _MAPPINGS,
        source_schema={"properties": {"name": {"type": "string"}}},
        regeneration_feedback=regeneration_feedback,
        agent_factory=_agent_factory,
    )
    assert len(seen) == 1, "expected exactly one model call"
    return seen[0]


# ---------------------------------------------------------------------------
# 2.16 — the resulting prompt differs from the original
# ---------------------------------------------------------------------------


def test_regeneration_prompt_differs_from_the_first_attempt_prompt():
    """2.16: a regeneration attempt SHALL NOT rebuild a byte-identical prompt."""
    first = _capture_prompt(None)
    regenerated = _capture_prompt(_STORED_FEEDBACK)

    assert regenerated != first, "regeneration rebuilt a byte-identical prompt (bugfix.md 1.15)"
    # The difference is additive: the original instructions all survive, so the
    # regeneration is not a different task, it is the same task plus feedback.
    assert first.strip() in regenerated or all(line in regenerated for line in first.splitlines() if line.strip()), (
        "the regeneration prompt dropped part of the original instruction set"
    )


def test_regeneration_prompt_carries_the_reviewers_issues():
    """2.16: the prior iteration's reviewer feedback reaches the prompt."""
    regenerated = _capture_prompt(_STORED_FEEDBACK)

    assert "PREVIOUS ATTEMPT WAS REJECTED BY CODE REVIEW" in regenerated
    # The reviewer's own words, severity and category — not a generic "retry".
    assert "to_date called without an explicit format string" in regenerated
    assert "[major]" in regenerated
    assert "Correctness" in regenerated
    # The aggregated triggers and the summary come along too.
    assert "executed checks failed: 2 out of 6" in regenerated
    assert "Reviewer summary:" in regenerated


def test_first_attempt_prompt_carries_no_feedback_block():
    """3.18: first-attempt behaviour is unchanged — no feedback, no marker."""
    first = _capture_prompt(None)

    assert "PREVIOUS ATTEMPT WAS REJECTED" not in first
    assert "Reviewer summary:" not in first
    # Still a usable prompt.
    assert "df_transformed" in first
    assert "signup_date" in first


def test_feedback_present_but_empty_still_differs():
    """A stored feedback_context with nothing actionable must still change the
    prompt — otherwise the retry is the identical-prompt retry 2.16 forbids,
    with no signal that anything was wrong
    (`transform_generation._build_feedback_section`'s len(lines) == 1 arm).
    """
    first = _capture_prompt(None)
    hollow = _capture_prompt({"summary": "", "issues_found": [], "reviewer_issues": [], "recommendations": []})

    assert hollow != first
    assert "PREVIOUS ATTEMPT WAS REJECTED BY CODE REVIEW" in hollow


# ---------------------------------------------------------------------------
# V7 — the injection must not spend token budget it does not have
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Same estimator test_transform_prompt.py's budget assertions use."""
    return len(text) // 4


def test_feedback_slot_costs_nothing_on_a_first_attempt():
    """V7: T11a left the template at ~498 of a 500-token budget, so the slot
    must be free when no feedback is supplied.

    `test_prompt_under_500_tokens_for_10_fields` /`_for_5_fields` are the
    budget assertions; this pins WHY they still hold — the first-attempt render
    is byte-identical whether `feedback_section` is omitted or passed as "",
    which is the production first-attempt shape
    (`_build_feedback_section` returns "" for no feedback).
    """
    from prompt_template_loader import load_prompt_template

    omitted = load_prompt_template("transform_generation.j2", mappings=_MAPPINGS)
    empty = load_prompt_template("transform_generation.j2", mappings=_MAPPINGS, feedback_section="")

    assert omitted == empty, "the feedback slot is not free when unsupplied — budget headroom is gone"


def test_regeneration_prompt_growth_is_only_the_feedback():
    """V7: the regeneration prompt is allowed to exceed the first-attempt
    budget (it carries the reviewer's issues on purpose), but by nothing more
    than the feedback block itself.
    """
    first = _capture_prompt(None)
    regenerated = _capture_prompt(_STORED_FEEDBACK)

    feedback_block = _tg._build_feedback_section(_STORED_FEEDBACK)
    growth = _estimate_tokens(regenerated) - _estimate_tokens(first)
    block_tokens = _estimate_tokens(feedback_block)

    # Allow a couple of tokens for the surrounding newlines the slot emits.
    assert growth <= block_tokens + 2, (
        f"regeneration render grew by ~{growth} tokens but the feedback block is only ~{block_tokens}"
    )


# ---------------------------------------------------------------------------
# 2.16 / 3.18 — the gate at create_code/lambda_function.py:476
# ---------------------------------------------------------------------------

_PIPELINE_ID = "pipe-regen"
_TARGET_CONFIG = {
    "type": "s3_tables",
    "bucket_name": "b",
    "namespace_name": "n",
    "table_name": "t",
}


def _run_handler(regeneration_attempt, feedback):
    """Drive lambda_handler to the regeneration gate and capture what codegen saw."""
    state = MagicMock()

    def _get_item(Key):
        if Key.get("step_name") == "schema_extract":
            return {"Item": {"run_id": Key["run_id"], "step_name": "schema_extract", "extracted_schema": "{}"}}
        return {}

    state.get_item.side_effect = _get_item
    mappings = MagicMock()
    # p33 task 4.6 harness amendment, NOT an assertion change. Two changes, both
    # to the fixture only:
    #   1. The two schema hashes are added. They were absent and the handler
    #      substituted `str(uuid.uuid4())` for each; requirement 2.9 makes their
    #      absence a StepFailure, because a generated identifier matches no
    #      schema. `store_mapping_separately` writes both on every real record
    #      (`schema_mapping/lambda_function.py:1166-1169`), so the fixture was
    #      under-specified relative to the live writer.
    #   2. `target_schema` is kept but is now DEAD in this fixture, and that is
    #      the point of the spec: no writer has ever set a `target_schema`
    #      attribute on a mappings record, so the handler's old read of it always
    #      produced `{}`. The read now goes to the pipeline record instead
    #      (requirement 2.1), and `get_pipeline_metadata` below returns a record
    #      with no `target_schema_s3_key` — requirement 2.6's legitimate
    #      no-schema state, which resolves to `(None, None)` with no S3 call. Left
    #      in place so the diff shows what stopped being read rather than hiding
    #      it; this file is about regeneration feedback threading, unchanged.
    mappings.get_item.return_value = {
        "Item": {
            "mapping_id": "map-1",
            "mapping_result": "{}",
            "field_mappings": _MAPPINGS,
            "target_schema": {"properties": {}},
            "source_schema_hash": "src-hash-map-1",
            "target_schema_hash": "tgt-hash-map-1",
        }
    }

    captured: dict = {}

    def _capture_generate_code(pm, mapping_item, regeneration_feedback=None, **kwargs):
        captured["regeneration_feedback"] = regeneration_feedback
        return "# code\ndf = spark.read.json('s3://b/k')\n"

    event = {"run_id": "run-regen", "pipeline_id": _PIPELINE_ID, "mapping_id": "map-1"}
    if regeneration_attempt is not None:
        event["regeneration_attempt"] = regeneration_attempt

    feedback_reader = MagicMock(return_value=feedback)

    with (
        patch.object(_mod, "pipeline_runs_table", state),
        patch.object(_mod, "mappings_table", mappings),
        patch.object(_mod, "get_pipeline_metadata", return_value={"target_config": _TARGET_CONFIG}),
        patch.object(_mod, "get_regeneration_feedback", feedback_reader),
        # `*_` absorbs the run_schema_hash the handler now passes as a 4th
        # positional argument (the source-schema compatibility filter). This
        # file is about regeneration-feedback threading; the stub keeps the
        # state-carried mapping regardless, exactly as before.
        patch.object(_mod, "_select_freshest_mapping", side_effect=lambda p, m, r, *_: (m, r)),
        patch.object(_mod, "publish_pipeline_state_change"),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(_mod, "generate_code", side_effect=_capture_generate_code),
        patch.object(_mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
    ):
        result = _mod.lambda_handler(event, None)

    return result, feedback_reader, captured


def test_gate_fires_on_a_regeneration_and_threads_the_feedback():
    """2.16: regeneration_attempt > 0 reads the prior feedback and hands it to codegen."""
    result, feedback_reader, captured = _run_handler(1, _STORED_FEEDBACK)

    assert result["statusCode"] == 200
    feedback_reader.assert_called_once_with("run-regen")
    assert captured["regeneration_feedback"] == _STORED_FEEDBACK
    assert result["regeneration_attempt"] == 1


@pytest.mark.parametrize("attempt", [None, 0])
def test_first_attempt_never_reads_regeneration_feedback(attempt):
    """3.18: a regeneration_attempt = 0 run (or one with the key absent) does
    NOT call get_regeneration_feedback — adding the key SHALL NOT change
    first-attempt behaviour, and the read is a DynamoDB call that would now be
    able to fail the step (V8).
    """
    result, feedback_reader, captured = _run_handler(attempt, _STORED_FEEDBACK)

    assert result["statusCode"] == 200
    feedback_reader.assert_not_called()
    assert captured["regeneration_feedback"] is None
    assert result["regeneration_attempt"] == 0
