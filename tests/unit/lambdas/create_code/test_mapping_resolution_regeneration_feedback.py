# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: ``mapping_resolution.get_regeneration_feedback`` (p32 T13).

This is the read half of the regeneration feedback loop, and it is the copy
that has no test naming it. codegraph flags BOTH copies as uncovered:

    get_regeneration_feedback (create_code/lambda_function.py:143)  — 1 caller
    get_regeneration_feedback (create_code/mapping_resolution.py:90) — 1 caller

design.md §5 (V3) records the first flag as FALSE — ``lambda_function``'s copy
is exercised by ``test_read_path_errors.py:125-144``. This file covers the
second, and it also answers the question T13 asks: **do the two copies agree?**

**They do, and not by coincidence — they cannot disagree.** The task text calls
``mapping_resolution.py:90`` "the duplicate copy"; in the current tree it is
not a duplicate. ``lambda_function.py:143-145`` is a three-line delegating
wrapper that resolves ``pipeline_runs_table`` from its own module globals and
calls straight through (the P28 R19.1 decomposition — see
``mapping_resolution.py``'s module docstring: "Extracted verbatim from
lambda_function.py … lambda_function keeps the boto3 resources and passes them
in via thin delegating wrappers"). So there is ONE implementation with two
entry points. ``test_delegation`` below pins that structurally, and
``test_both_entry_points_agree`` pins it behaviourally across every outcome,
so if task 16 forks them while wiring C8, this file fails.

**Where expectations come from.** `LGR-28b4d75-5` (template) forbids
transcribing a test's expectations from the producer's own output — the output
is its own oracle and cannot detect drift. That applies with extra force to a
characterization test, which is normally written from current behaviour. So the
assertions here are derived from bugfix.md 2.16 instead:

    2.16 WHEN create_code runs with regeneration_attempt > 0 THEN it SHALL read
    the prior iteration's reviewer feedback and the resulting prompt SHALL
    differ from the original.

Three consequences are asserted, each traceable to that clause rather than to
the current return values:

1. Feedback that the test step stored must come back — otherwise the
   "resulting prompt differs" half of 2.16 is unsatisfiable.
2. The DynamoDB key must name the step that WROTE the feedback
   (``store_regeneration_feedback``, test_code/lambda_function.py:510, keys on
   ``StepName.TEST_CODE``). A reader looking anywhere else finds nothing and
   2.16 fails silently. The cross-module round-trip is asserted in
   ``tests/unit/lambdas/test_code/test_store_regeneration_feedback.py``.
3. Absent feedback and a FAILED read must stay distinguishable. Absence is a
   legitimate first pass ({}); a throttle that returned {} would produce
   exactly the byte-identical retry prompt 2.16 forbids, with no signal.

**Divergence from current behaviour: none here, and that is the finding.** The
read layer already satisfies 2.16. Defect 4 is not in this function — it is in
its caller, the gate at ``create_code/lambda_function.py:476``
(``if regeneration_attempt > 0``), which never fires because nothing writes
``regeneration_attempt`` (bugfix.md 1.14/1.15). These tests are therefore
expected to keep passing through task 16; what task 16 changes is how often
this code runs, not what it returns. V8 in design.md notes the corollary: the
raise paths below go live in production for the first time.

**Validates: Requirements 2.16; V3**
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Pre-import the condition classes before any patch.dict(sys.modules) block
# (see test_mapping_freshness.py:38-45 — a re-imported boto3.dynamodb.conditions
# carries Key/Attr classes moto's isinstance checks don't recognize).
import boto3.dynamodb.conditions  # noqa: F401  (import order is the point)

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _CREATE_CODE_DIR)
sys.path.insert(0, _SHARED_DIR)

# Stub the heavy / AWS-touching imports so lambda_function loads in isolation
# (same set as test_read_path_errors.py and test_mapping_freshness.py).
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
    "prompt_template_loader": MagicMock(
        load_prompt_template=MagicMock(return_value="mock prompt"),
    ),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "artifact_schema": MagicMock(
        create_output_data=MagicMock(return_value={}),
    ),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    sys.modules.pop("mapping_resolution", None)
    import lambda_function as _mod

    # Imported inside the block so this is the SAME module object the
    # lambda_function wrappers delegate to.
    import mapping_resolution as _mr


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


RUN_ID = "run-2026-08-04-00-00-00-regenfb"

# The feedback_context shape analyze_test_results_for_regeneration produces and
# _build_feedback_section consumes (transform_generation.py:32-59). Its keys are
# what makes a regeneration prompt differ from the original (2.16).
FEEDBACK_CONTEXT = {
    "summary": "Code regeneration needed (attempt 1/2): 2 reviewer issue(s), 1 failed check(s)",
    "issues_found": ["executed checks failed: 1 out of 4", "[critical] to_date format string is wrong."],
    "reviewer_issues": [
        {"severity": "critical", "category": "Correctness", "description": "to_date format string is wrong."},
        {"severity": "major", "category": "Security", "description": "Credentials logged in plaintext."},
    ],
    "recommendations": ["Address every reviewer issue listed above and fix failing checks"],
}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetItem")


def _step_names():
    """Load shared/step_names.py under its own alias.

    The expected sort key is derived from the shared enum rather than from a
    transcribed literal, so a rename of StepName.TEST_CODE breaks this test
    instead of silently making the reader and the writer disagree
    (`LGR-28b4d75-5`).
    """
    if "step_names" in sys.modules:
        return sys.modules["step_names"]
    spec = importlib.util.spec_from_file_location("step_names", os.path.join(_SHARED_DIR, "step_names.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state_table(mock_dynamodb_tables):
    """The real (moto) pipeline-runs/state table: PK run_id, SK step_name."""
    return mock_dynamodb_tables["agentic_data_ingestion_state"]


# ---------------------------------------------------------------------------
# 2.16 consequence 1 — stored feedback comes back
# ---------------------------------------------------------------------------


def test_stored_feedback_context_is_returned(state_table):
    """The reviewer feedback the test step stored is what the reader returns.

    2.16's "the resulting prompt SHALL differ from the original" is only
    reachable if the reviewer's issues survive the read. Asserted against a
    real DynamoDB engine (moto), so the map/list round-trip is genuine rather
    than a MagicMock echoing its own input back.

    **Validates: Requirements 2.16**
    """
    state_table.put_item(
        Item={
            "run_id": RUN_ID,
            "step_name": _step_names().StepName.TEST_CODE,
            "status": "success",
            "regeneration_feedback": FEEDBACK_CONTEXT,
        }
    )

    result = _mr.get_regeneration_feedback(RUN_ID, state_table)

    assert result == FEEDBACK_CONTEXT
    # The two keys _build_feedback_section renders must survive, or the
    # regeneration prompt is byte-identical to the original (1.15).
    assert result["reviewer_issues"][0]["description"] == "to_date format string is wrong."
    assert "executed checks failed: 1 out of 4" in result["issues_found"]


# ---------------------------------------------------------------------------
# 2.16 consequence 2 — the read key names the step that wrote the feedback
# ---------------------------------------------------------------------------


def test_read_key_is_run_id_plus_the_test_code_step(state_table):
    """The item key is (run_id, StepName.TEST_CODE) — the writer's key.

    ``store_regeneration_feedback`` (test_code/lambda_function.py:510) writes
    under the TEST_CODE sort key. A reader keyed on anything else returns {},
    the prompt does not differ, and 2.16 fails with no error anywhere. Pinning
    the key is what makes that class of drift loud.

    **Validates: Requirements 2.16**
    """
    spy = MagicMock(return_value={})
    _mr.get_regeneration_feedback(RUN_ID, MagicMock(get_item=spy))

    spy.assert_called_once_with(Key={"run_id": RUN_ID, "step_name": _step_names().StepName.TEST_CODE})


def test_feedback_for_another_run_is_not_returned(state_table):
    """Feedback is per-run: a different run's row must not be read.

    The prior iteration whose feedback 2.16 requires is THIS run's, so a read
    that leaked another run's issues would inject the wrong reviewer feedback
    into the regeneration prompt.

    **Validates: Requirements 2.16**
    """
    state_table.put_item(
        Item={
            "run_id": "run-some-other-run",
            "step_name": _step_names().StepName.TEST_CODE,
            "regeneration_feedback": FEEDBACK_CONTEXT,
        }
    )

    assert _mr.get_regeneration_feedback(RUN_ID, state_table) == {}


# ---------------------------------------------------------------------------
# 2.16 consequence 3 — absence and failure stay distinguishable
# ---------------------------------------------------------------------------


def test_missing_row_returns_empty_dict(state_table):
    """No prior iteration → {} (a legitimate first-pass generation, P21 6.4).

    **Validates: Requirements 2.16**
    """
    assert _mr.get_regeneration_feedback(RUN_ID, state_table) == {}


def test_row_without_feedback_attribute_returns_empty_dict(state_table):
    """A test_code row that never stored feedback → {}, not a partial read.

    **Validates: Requirements 2.16**
    """
    state_table.put_item(Item={"run_id": RUN_ID, "step_name": _step_names().StepName.TEST_CODE, "status": "success"})

    assert _mr.get_regeneration_feedback(RUN_ID, state_table) == {}


def test_empty_feedback_attribute_returns_empty_dict(state_table):
    """A stored-but-empty feedback map is treated as absence.

    Consistent with ``_build_feedback_section``, which returns "" for a falsy
    feedback dict (transform_generation.py:44-45) — so an empty map and a
    missing attribute must not diverge here either.

    **Validates: Requirements 2.16**
    """
    state_table.put_item(
        Item={"run_id": RUN_ID, "step_name": _step_names().StepName.TEST_CODE, "regeneration_feedback": {}}
    )

    assert _mr.get_regeneration_feedback(RUN_ID, state_table) == {}


@pytest.mark.parametrize(
    ("code", "expected_class", "expected_error_type"),
    [
        ("ThrottlingException", "TransientStepFailure", "transient"),
        ("ProvisionedThroughputExceededException", "TransientStepFailure", "transient"),
        ("AccessDeniedException", "StepFailure", "permanent"),
    ],
)
def test_infrastructure_read_failure_raises_rather_than_returning_empty(code, expected_class, expected_error_type):
    """A throttle or a denial RAISES; it must not masquerade as "no feedback".

    P28 R17.3, and load-bearing for 2.16: a swallowed read error returns {},
    ``_build_feedback_section({})`` returns "", and the regeneration prompt is
    byte-identical to the one that already failed — the exact defect 1.15
    describes, but arriving silently. The class NAME is asserted because that
    is what SFN ``ErrorEquals`` matches on.

    design.md V8: this path has never run in production, because the gate at
    create_code/lambda_function.py:476 never fired. Task 16 makes it live.

    **Validates: Requirements 2.16**
    """
    table = MagicMock()
    table.get_item.side_effect = _client_error(code)

    with pytest.raises(Exception) as exc_info:
        _mr.get_regeneration_feedback(RUN_ID, table)

    exc = exc_info.value
    assert type(exc).__name__ == expected_class
    assert exc.error_type == expected_error_type
    assert exc.step_name == "create_code"
    assert exc.details["operation"] == "get_regeneration_feedback"


def test_missing_table_returns_empty_dict():
    """ResourceNotFoundException is genuine not-found → {} (P28 R17.3).

    The counterpart to the test above: only INFRASTRUCTURE errors raise. A
    table that does not exist is the not-found case, so it keeps the
    first-pass semantics.

    **Validates: Requirements 2.16**
    """
    table = MagicMock()
    table.get_item.side_effect = _client_error("ResourceNotFoundException")

    assert _mr.get_regeneration_feedback(RUN_ID, table) == {}


# ---------------------------------------------------------------------------
# V3 — do the two flagged copies agree? (they are one implementation)
# ---------------------------------------------------------------------------


def test_lambda_function_copy_delegates_to_mapping_resolution():
    """``lambda_function.get_regeneration_feedback`` is a delegating wrapper.

    Structural pin on the claim this task had to settle: the two codegraph
    entries are not two implementations that might disagree — the one at
    ``lambda_function.py:143`` forwards to the one at
    ``mapping_resolution.py:90``, passing the table resolved from
    lambda_function's globals at call time (which is what keeps
    ``patch.object(lambda_function, "pipeline_runs_table", ...)`` working, per
    mapping_resolution.py's module docstring).

    If task 16 gives create_code its own feedback read instead of widening
    this one, this test fails — deliberately, so the fork is a decision rather
    than an accident.

    **Validates: Requirements 2.16; V3**
    """
    table = MagicMock()
    sentinel = {"summary": "delegated", "reviewer_issues": [], "issues_found": [], "recommendations": []}

    with (
        patch.object(_mod, "pipeline_runs_table", table),
        patch.object(_mr, "get_regeneration_feedback", return_value=sentinel) as delegate,
    ):
        assert _mod.get_regeneration_feedback(RUN_ID) is sentinel

    delegate.assert_called_once_with(RUN_ID, table)


@pytest.mark.parametrize(
    "seed",
    [
        pytest.param("absent", id="no-row"),
        pytest.param("no-attribute", id="row-without-feedback"),
        pytest.param("present", id="feedback-stored"),
    ],
)
def test_both_entry_points_agree(state_table, seed):
    """The wrapper and the implementation return the same thing, case by case.

    Behavioural half of the agreement question. Run against the real table so
    both paths traverse the same DynamoDB round-trip.

    **Validates: Requirements 2.16; V3**
    """
    step_name = _step_names().StepName.TEST_CODE
    if seed == "no-attribute":
        state_table.put_item(Item={"run_id": RUN_ID, "step_name": step_name, "status": "success"})
    elif seed == "present":
        state_table.put_item(Item={"run_id": RUN_ID, "step_name": step_name, "regeneration_feedback": FEEDBACK_CONTEXT})

    direct = _mr.get_regeneration_feedback(RUN_ID, state_table)
    with patch.object(_mod, "pipeline_runs_table", state_table):
        through_wrapper = _mod.get_regeneration_feedback(RUN_ID)

    assert direct == through_wrapper


def test_both_entry_points_agree_on_infrastructure_failure():
    """Agreement extends to the raise path, not just the happy path.

    **Validates: Requirements 2.16; V3**
    """
    table = MagicMock()
    table.get_item.side_effect = _client_error("ThrottlingException")

    with pytest.raises(Exception) as direct_exc:
        _mr.get_regeneration_feedback(RUN_ID, table)

    with patch.object(_mod, "pipeline_runs_table", table), pytest.raises(Exception) as wrapper_exc:
        _mod.get_regeneration_feedback(RUN_ID)

    assert type(direct_exc.value).__name__ == type(wrapper_exc.value).__name__
    assert direct_exc.value.details == wrapper_exc.value.details
