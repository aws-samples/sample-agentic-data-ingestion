# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 6 — a timeout failure reason is distinguishable from its two siblings.

An approval gate has exactly three terminal causes, and all three arrive at
RecordFailure through the same `Catch: [{ErrorEquals: ["States.ALL"], Next:
"RecordFailure", ResultPath: "$.error"}]`:

    States.Timeout      nobody answered within TimeoutSeconds
    ApprovalRejected    a human said no (approval_handler:566 /
                        dashboard_api/handlers/approvals.py:450)
    anything else       a Lambda or step failure at the gate's own invoke

p34 R6.5 requires the recorded failure reason to place a timeout in its own
class — naming the approval type and the timeout as the cause — so it cannot be
read as either sibling. `record_failure/lambda_function.py`'s non-JSON-cause
path (the third of its three cause-parsing tiers) is where all three land.

Expectations here derive from R6.5, not from current output: before this spec a
`States.Timeout` at a gate recorded `error_message = "States.Timeout"` with the
approval type named nowhere, and progress inference attributed a
WaitForCodeApproval timeout to `test_code` — indistinguishable from a
ForceTestReviewApproval timeout on the same run.

What is deliberately NOT asserted: the phrase "timed out" alone as the
discriminator. A Lambda's own function timeout carries the cause "Task timed
out after 900.00 seconds", so that phrase appears in a sibling class too — it is
in the generator below for exactly that reason. The discriminator is the raw SFN
`Error` name (never rewritten) plus the approval type the timeout reason names.

Validates: Requirements 6.5
"""

# Feature: p34-code-approval-gate-honors-config, Property 6: A timeout failure reason is distinguishable from its two siblings

import os
import sys
from unittest.mock import MagicMock, patch

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir to path
_record_failure_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_record_failure")
)
sys.path.insert(0, _record_failure_dir)

sys.modules.pop("lambda_function", None)
import lambda_function as _mod
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


# ── generators ───────────────────────────────────────────────────────

_GATES = (
    _mod.APPROVAL_TYPE_SCHEMA_MAPPING,
    _mod.APPROVAL_TYPE_TEST_FAILURE,
    _mod.APPROVAL_TYPE_CODE_GENERATION,
)

_ABSENT = object()

# Everything a Choice state must NOT read as "no approval was pending".
# `False` is excluded on purpose: an explicit boolean false is the one value
# that skips a gate, so a state carrying it is not parked on that gate.
_NON_FALSE_FLAGS = (True, _ABSENT, None, "false", "true", 0, 1, {}, [])

# Non-passed statuses TestResultChoice routes to ForceTestReviewApproval.
_NON_PASSED_STATUSES = ("failed", "not_verified", "error", "")

# Sibling class 1: a Lambda or step failure at the gate's invoke. `States.Timeout`
# is excluded — that is the class under test. "Task timed out after 900.00
# seconds" is a Lambda's OWN timeout, included deliberately: it is the sibling
# most likely to be mistaken for an approval timeout.
_STEP_ERROR_NAMES = (
    "Lambda.ServiceException",
    "Lambda.TooManyRequestsException",
    "Lambda.Unknown",
    "States.TaskFailed",
    "StepFailure",
    "TransientStepFailure",
)
_STEP_ERROR_CAUSES = (
    "",
    "Task timed out after 900.00 seconds",
    "Rate exceeded",
    "The role defined for the function cannot be assumed by Lambda",
)

# Sibling class 2: a rejection. Shape from approval_handler:566 —
# f"Approval rejected by {user}: {comment}".
_REJECTION_USERS = ("alice", "bob@example.com", "ops-oncall")
_REJECTION_COMMENTS = ("mapping looks wrong", "needs a second reviewer", "")


def _flagged(payload: dict, flag) -> dict:
    """Attach an `approval_required` value, or leave the key absent."""
    if flag is _ABSENT:
        return payload
    return {**payload, "approval_required": flag}


@st.composite
def _parked_at_a_gate(draw):
    """(approval_type, execution state) for a run parked at that gate.

    Each state is built the way the state machine builds it: the keys the
    upstream steps wrote, the routing field the gate's Choice state read, and
    the gate's own ResultPath key ABSENT (it is written only on a grant).
    """
    gate = draw(st.sampled_from(_GATES))
    flag = draw(st.sampled_from(_NON_FALSE_FLAGS))
    run_id = draw(st.text(alphabet="abcdef0123456789-", min_size=3, max_size=14))
    state: dict = {
        "pipeline_id": "uploads/customer-a",
        "file_validation_result": {"run_id": f"run-{run_id}", "original_pipeline_id": "pipe-9"},
        "schema_extract_result": {"schema_version_id": "sv-1"},
        "schema_compare_result": {"action": "create_new_pipeline"},
    }

    if gate == _mod.APPROVAL_TYPE_SCHEMA_MAPPING:
        state["schema_mapping_result"] = _flagged({"mapping_id": "map-1"}, flag)
        return gate, state

    # Past the mapping gate: it was skipped (explicit false) or granted.
    if draw(st.booleans()):
        state["schema_mapping_result"] = {"mapping_id": "map-1", "approval_required": False}
    else:
        state["schema_mapping_result"] = {"mapping_id": "map-1", "approval_required": True}
        state["schema_mapping_approval"] = {"approved": True}
    state["create_code_result"] = {"code_version_id": "cv-1"}

    if gate == _mod.APPROVAL_TYPE_TEST_FAILURE:
        # Reached only when the regeneration branch did NOT take the run back
        # to CreateCode: no regeneration info, not needed, or budget spent.
        regeneration = draw(
            st.sampled_from(
                (
                    {},
                    {"regeneration_needed": False},
                    {"regeneration_needed": True, "regeneration_attempt": 2, "regeneration_max_attempts": 2},
                    {"regeneration_needed": True, "regeneration_attempt": 3, "regeneration_max_attempts": 2},
                )
            )
        )
        status = draw(st.sampled_from([s for s in _NON_PASSED_STATUSES if s]))
        state["test_code_result"] = {
            "test_result": {"status": status, "checks": []},
            "approval_required": True,
            **regeneration,
        }
        return gate, state

    # code_generation: reachable from a passed test, or from approve-anyway on
    # a failed one (ForceTestReviewApproval resumes at CheckCodeApproval).
    if draw(st.booleans()):
        state["test_review_approval"] = {"approved": True, "decision": "approve_anyway"}
        status = draw(st.sampled_from(_NON_PASSED_STATUSES))
    else:
        status = "passed"
    state["test_code_result"] = _flagged({"test_result": {"status": status, "checks": []}}, flag)
    return gate, state


_timeout_errors = st.sampled_from(("", None, "Task timed out")).map(
    lambda cause: {"Error": _mod.TIMEOUT_ERROR_NAME, "Cause": cause}
)

_step_errors = st.tuples(st.sampled_from(_STEP_ERROR_NAMES), st.sampled_from(_STEP_ERROR_CAUSES)).map(
    lambda pair: {"Error": pair[0], "Cause": pair[1]}
)

_rejection_errors = st.tuples(st.sampled_from(_REJECTION_USERS), st.sampled_from(_REJECTION_COMMENTS)).map(
    lambda pair: {
        "Error": _mod.APPROVAL_REJECTED_ERROR_NAME,
        "Cause": f"Approval rejected by {pair[0]}: {pair[1]}",
    }
)


# ── the consumer's view ──────────────────────────────────────────────


def _names_the_approval_timeout(reason: str, approval_type: str) -> bool:
    """Does this reason name the approval type AND the timeout as the cause?"""
    lowered = reason.lower()
    return approval_type in reason and "timed out" in lowered and _mod.TIMEOUT_ERROR_NAME.lower() in lowered


def _recorded_class(parsed: dict, approval_type: str) -> str:
    """How a Failure_Record consumer tells the three classes apart.

    Reads only the recorded fields — no knowledge of which payload produced
    them — so a reason that lost its Error name or failed to name the approval
    type lands in the wrong class here.
    """
    if parsed["error_name"] == _mod.APPROVAL_REJECTED_ERROR_NAME:
        return "approval_rejected"
    if parsed["error_name"] == _mod.TIMEOUT_ERROR_NAME and _names_the_approval_timeout(
        parsed["error_message"], approval_type
    ):
        return "approval_timeout"
    return "step_error"


# ── Property 6 ───────────────────────────────────────────────────────


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(parked=_parked_at_a_gate(), timeout=_timeout_errors, step=_step_errors, rejection=_rejection_errors)
def test_timeout_reason_is_distinguishable_from_its_two_siblings(parked, timeout, step, rejection):
    """R6.5: for any $.error from the three classes at an approval gate, the
    recorded reason places it in the right class, and a timeout reason
    additionally names the approval type.

    All three are parsed against the SAME parked state, so nothing but the
    error payload can account for a difference between them.
    """
    approval_type, state = parked

    timeout_record = _mod.parse_error(timeout, state)
    step_record = _mod.parse_error(step, state)
    rejection_record = _mod.parse_error(rejection, state)

    # Right class, each.
    assert _recorded_class(timeout_record, approval_type) == "approval_timeout"
    assert _recorded_class(step_record, approval_type) == "step_error"
    assert _recorded_class(rejection_record, approval_type) == "approval_rejected"

    # Distinguishable: three payloads, three classes, three reasons.
    assert (
        len(
            {
                _recorded_class(timeout_record, approval_type),
                _recorded_class(step_record, approval_type),
                _recorded_class(rejection_record, approval_type),
            }
        )
        == 3
    )
    assert len({timeout_record["error_message"], step_record["error_message"], rejection_record["error_message"]}) == 3

    # The timeout reason names the approval type and the timeout as the cause.
    assert approval_type in timeout_record["error_message"]
    assert _mod.TIMEOUT_ERROR_NAME in timeout_record["error_message"]

    # Neither sibling is dressed up as an approval timeout, and neither loses
    # its own Error name — that name is the record's class discriminator.
    assert step_record["error_name"] == step["Error"]
    assert approval_type not in step_record["error_message"]
    assert rejection_record["error_name"] == _mod.APPROVAL_REJECTED_ERROR_NAME
    assert approval_type not in rejection_record["error_message"]

    # A rejection keeps the human's stated reason verbatim.
    assert rejection_record["error_message"] == rejection["Cause"]


# ── examples for the R6.5 reason (the property's per-gate anchors) ────


@pytest.mark.parametrize(
    ("gate", "state"),
    [
        (
            "schema_mapping",
            {
                "file_validation_result": {"run_id": "run-1", "original_pipeline_id": "pipe-1"},
                "schema_mapping_result": {"approval_required": True},
            },
        ),
        (
            "test_failure",
            {
                "file_validation_result": {"run_id": "run-1", "original_pipeline_id": "pipe-1"},
                "schema_mapping_result": {"approval_required": False},
                "create_code_result": {"code_version_id": "cv-1"},
                "test_code_result": {"test_result": {"status": "failed"}, "approval_required": True},
            },
        ),
        (
            "code_generation",
            {
                "file_validation_result": {"run_id": "run-1", "original_pipeline_id": "pipe-1"},
                "schema_mapping_result": {"approval_required": False},
                "create_code_result": {"code_version_id": "cv-1"},
                "test_code_result": {"test_result": {"status": "passed"}, "approval_required": True},
            },
        ),
    ],
)
def test_each_gate_timeout_names_its_own_approval_type(gate, state):
    parsed = _mod.parse_error({"Error": "States.Timeout", "Cause": ""}, state)

    assert _mod.pending_approval_type(state) == gate
    assert gate in parsed["error_message"]
    assert "timed out" in parsed["error_message"].lower()
    assert parsed["error_name"] == "States.Timeout"
    # error_type's domain is unchanged: permanent | transient | unknown.
    assert parsed["error_type"] == "unknown"


@pytest.mark.parametrize(
    ("case", "state"),
    [
        # CreateCode running after the mapping gate was skipped.
        (
            "create_code",
            {
                "file_validation_result": {"run_id": "run-1"},
                "schema_mapping_result": {"approval_required": False},
            },
        ),
        # TestCode running: the mapping gate was granted, CreateCode is done.
        (
            "test_code",
            {
                "file_validation_result": {"run_id": "run-1"},
                "schema_mapping_result": {"approval_required": True},
                "schema_mapping_approval": {"approved": True},
                "create_code_result": {"code_version_id": "cv-1"},
            },
        ),
        # CreateInfrastructure running: the code gate was skipped (explicit false).
        (
            "create_infrastructure",
            {
                "file_validation_result": {"run_id": "run-1"},
                "schema_mapping_result": {"approval_required": False},
                "create_code_result": {"code_version_id": "cv-1"},
                "test_code_result": {"test_result": {"status": "passed"}, "approval_required": False},
            },
        ),
        # A failing run looping back to CreateCode with budget left (P32 D2):
        # the previous test_code_result is still in the state.
        (
            "regenerating_create_code",
            {
                "file_validation_result": {"run_id": "run-1"},
                "schema_mapping_result": {"approval_required": False},
                "create_code_result": {"code_version_id": "cv-1"},
                "test_code_result": {
                    "test_result": {"status": "failed"},
                    "approval_required": True,
                    "regeneration_needed": True,
                    "regeneration_attempt": 0,
                    "regeneration_max_attempts": 2,
                },
            },
        ),
    ],
)
def test_task_state_timeout_outside_a_gate_keeps_the_bare_reason(case, state):
    """A container/Lambda task timing out is not an approval timeout.

    The R6.5 reason must not be attached to a state that is not parked at a
    gate — that would be a misattribution created by this change rather than
    discovered by it.
    """
    assert _mod.pending_approval_type(state) == "", case
    parsed = _mod.parse_error({"Error": "States.Timeout", "Cause": ""}, state)
    assert parsed["error_message"] == "States.Timeout"


def test_handler_records_the_approval_timeout_reason():
    """The reason reaches the Failure_Record the dashboard reads."""
    mock_table = MagicMock()
    state = {
        "pipeline_id": "uploads/customer-a",
        "file_validation_result": {"run_id": "run-123", "original_pipeline_id": "pipe-9"},
        "schema_mapping_result": {"approval_required": False},
        "create_code_result": {"code_version_id": "cv-1"},
        "test_code_result": {"test_result": {"status": "passed"}, "approval_required": True},
        "error": {"Error": "States.Timeout", "Cause": ""},
    }

    with patch.object(_mod, "state_table", mock_table):
        result = _mod.lambda_handler({"state": state, "execution_name": "exec-abc"}, None)

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert "code_generation" in item["error_message"]
    assert "timed out" in item["error_message"].lower()
    assert item["error_name"] == "States.Timeout"
    assert item["run_id"] == "run-123"
    assert result["recorded"] is True


def test_raw_cause_is_appended_not_discarded():
    """Whatever the service supplied stays in the record."""
    reason = _mod.approval_timeout_reason("code_generation", "state exited on timeout")
    assert "code_generation" in reason
    assert "state exited on timeout" in reason
    assert _mod.approval_timeout_reason("code_generation", None) == _mod.approval_timeout_reason("code_generation")
