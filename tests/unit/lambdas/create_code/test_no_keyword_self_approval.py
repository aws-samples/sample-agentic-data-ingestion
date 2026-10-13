# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: keyword-count metrics deleted as gate inputs (P28 R13, task 11.2).

The old create_code quality gate scored token presence — ``"logger" in code``
+15, ``"try:"`` +15, ``count("def ")`` +20 — so template boilerplate (which
always contains those tokens) always scored ≥70 and SELF-APPROVED (skipped
human approval, set requires_review=False, claimed best-practices
compliance). All of that is deleted:

  * ``calculate_code_quality_metrics``: keyword quality_score,
    has_error_handling/has_logging booleans, estimated_maintainability, and
    the nonsense complexity_indicators are gone — only honest structural
    counts remain (display data, gating nothing).
  * ``create_approval_payload``: the fabricated code_review_data verdicts
    (best_practices_compliance et al.) are gone.

``determine_approval_requirement`` used to be the third bullet here — "no
quality-score bypass; approval is driven by the pipeline's approval
configuration only". p34 design D2 (``LGR-f077f1b-1``, template / ratified)
**deletes** that function from ``create_code``, because the field it wrote
(``$.create_code_result.approval_required``) was never read by any state in
the state machine.

**The subject did not go away — it moved, and it is proved in its new home.**
The two tests that stood at ``:103`` and ``:130`` are RELOCATED, names kept
verbatim, to
``tests/unit/lambdas/test_code/test_approval_skipped_only_by_configuration.py``,
where they now run against ``test_code.resolve_code_approval_setting`` +
``determine_merged_approval_requirement`` — the successors that own the
decision (``LGR-f077f1b-2``, template / ratified; p34 task 3.1, done before
this deletion for exactly that reason). ``:130``'s test carries **P28
R13.1's only guard**, that approval is skipped only by pipeline configuration
and never by a keyword quality score; read it there.

What stays here is the half of P28 R13.1 that is about the *code text* rather
than the decision: the metrics dict, the approval payload, and the
source-text grep that no keyword-scoring machinery remains.

Regeneration prompts (R13.3): the reviewer's issues are embedded in the
transform-generation prompt, so a regen prompt differs from the original.

Validates: P28 Requirements 13.1, 13.3, 13.4
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_LAMBDA_SOURCE = os.path.join(_CREATE_CODE_DIR, "lambda_function.py")
sys.path.insert(0, _CREATE_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy dependencies. NOTE: prompt_template_loader is NOT stubbed — the
# prompt-differ tests below render the real transform_generation.j2.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
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

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


# Rendered-template-style boilerplate: contains EVERY token the old score
# rewarded (logger, try:, def, import boto3, spark, validate, > 50 lines).
_TEMPLATE_BOILERPLATE = (
    "\n".join(
        [
            "import boto3",
            "import logging",
            "logger = logging.getLogger()",
            "def main():",
            "    try:",
            "        spark = get_spark_session()",
            "        df = spark.read.json('s3://bucket/key')",
            "        validate_schema(df)",
            "        df_transformed = df",
            "    except Exception as e:",
            "        logger.error(e)",
            "        raise",
        ]
    )
    + "\n# padding line" * 60
)


# ---------------------------------------------------------------------------
# R13.1/R13.4: template boilerplate does NOT self-approve
#
# ``test_template_boilerplate_does_not_self_approve`` and
# ``test_approval_skipped_only_by_pipeline_configuration`` stood here. Both
# called ``_mod.determine_approval_requirement``, which p34 design D2 deletes,
# so both are RELOCATED — names verbatim, both behavioural facts intact — to
# ``tests/unit/lambdas/test_code/test_approval_skipped_only_by_configuration.py``
# (p34 task 3.1, ``LGR-f077f1b-2``, template / ratified). Relocated, not
# deleted: P28 R13.1's only guard is one of them, and it was made to pass in its
# new home BEFORE the deletion here. See this module's docstring.
#
# The payload half of the first test survives below, in
# ``test_approval_payload_carries_no_fabricated_review_verdicts`` — which
# already exercises ``create_approval_payload(..., requires_review=True)`` and
# asserts no token-derived verdicts. ``create_approval_payload`` keeps its
# ``requires_review`` parameter and its ``True`` default; what D2 dropped is the
# handler passing a computed value into it.
# ---------------------------------------------------------------------------


def test_quality_metrics_carry_no_keyword_score_or_gate_booleans():
    """**Validates: Requirements 13.1** — the flagship dishonest fields are
    gone from the metrics dict."""
    metrics = _mod.calculate_code_quality_metrics(_TEMPLATE_BOILERPLATE, "tmpl.j2", "reasoning")

    for deleted_key in (
        "quality_score",
        "has_error_handling",
        "has_logging",
        "has_data_validation",
        "estimated_maintainability",
        "complexity_indicators",
    ):
        assert deleted_key not in metrics, f"deleted keyword metric still present: {deleted_key}"

    # Honest structural counts remain (display only).
    assert metrics["lines_of_code"] > 50
    assert metrics["functions_generated"] >= 1
    assert metrics["template_used"] == "tmpl.j2"


def test_approval_payload_carries_no_fabricated_review_verdicts():
    """**Validates: Requirements 13.1** — token-derived review conclusions
    (best_practices_compliance from the keyword score, security from
    ``"boto3" in code``, scalability from ``"spark" in code``) are gone."""
    metrics = _mod.calculate_code_quality_metrics(_TEMPLATE_BOILERPLATE, "tmpl.j2", "reasoning")
    payload = _mod.create_approval_payload(
        "cv-1", _TEMPLATE_BOILERPLATE, metrics, "reasoning", "tmpl.j2", "s3://b/c.py", requires_review=True
    )
    assert "code_review_data" not in payload
    flat = str(payload)
    assert "best_practices_compliance" not in flat
    assert "scalability_assessment" not in flat


def test_keyword_score_machinery_deleted_from_source():
    """Grep assertion: no keyword-bonus scoring remains in create_code.

    Checks the load-bearing code constructs (score accumulation, score
    reads, the fabricated payload key), not names that documentation may
    legitimately mention as deleted history.
    """
    with open(_LAMBDA_SOURCE, encoding="utf-8") as f:
        source = f.read()
    assert "quality_score +=" not in source  # keyword bonus accumulation
    assert '.get("quality_score"' not in source  # score consumed as gate input
    assert '"best_practices_compliance":' not in source  # fabricated payload key


# ---------------------------------------------------------------------------
# R13.3/R13.4: regeneration prompts differ by including reviewer feedback
# ---------------------------------------------------------------------------

_MAPPINGS = [
    {"source_field": "id", "source_type": "integer", "target_field": "id", "target_type": "integer"},
    {"source_field": "name", "source_type": "string", "target_field": "full_name", "target_type": "string"},
]

_FEEDBACK = {
    "summary": "Code regeneration needed (attempt 1/2): 2 reviewer issue(s), 1 failed check(s)",
    "issues_found": ["review verdict: fail"],
    "reviewer_issues": [
        {"severity": "critical", "category": "Correctness", "description": "to_date format string is wrong."},
        {"severity": "major", "category": "Security", "description": "Credentials logged in plaintext."},
    ],
    "recommendations": ["Address every reviewer issue listed above and fix failing checks"],
}


def _capture_transform_prompt(feedback: dict | None) -> str:
    """Run generate_transform_with_ai with a capturing agent; return the prompt."""
    captured: dict = {}

    def _agent_call(prompt):
        captured["prompt"] = prompt
        return "df_transformed = df.select('*')"

    agent = MagicMock(side_effect=_agent_call)
    with patch.object(_mod, "Agent", MagicMock(return_value=agent)):
        _mod.generate_transform_with_ai(_MAPPINGS, source_schema={}, regeneration_feedback=feedback)
    return captured["prompt"]


def test_regeneration_prompt_differs_and_contains_reviewer_issues():
    """**Validates: Requirements 13.3, 13.4** — the regen prompt is not
    identical to the original and contains the issue descriptions."""
    original_prompt = _capture_transform_prompt(None)
    regen_prompt = _capture_transform_prompt(_FEEDBACK)

    assert regen_prompt != original_prompt
    assert "to_date format string is wrong." in regen_prompt
    assert "Credentials logged in plaintext." in regen_prompt
    assert "REJECTED BY CODE REVIEW" in regen_prompt
    # The original prompt carries no feedback block.
    assert "REJECTED BY CODE REVIEW" not in original_prompt


def test_first_attempt_prompt_unchanged_by_empty_feedback():
    """No feedback (first attempt / empty dict) → no feedback block."""
    assert _capture_transform_prompt(None) == _capture_transform_prompt({})


def test_feedback_section_renders_structured_issues():
    section = _mod._build_feedback_section(_FEEDBACK)
    assert "[critical] (Correctness) to_date format string is wrong." in section
    assert "[major] (Security) Credentials logged in plaintext." in section
    assert "review verdict: fail" in section
    assert _mod._build_feedback_section(None) == ""
    assert _mod._build_feedback_section({}) == ""
