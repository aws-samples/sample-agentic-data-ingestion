# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: a Glue capacity refusal is a harness NON-verification.

Spec `real-spark-verification` task 3.6 — requirements 8.1, 8.2, 8.3, 8.4.

An account already holding its maximum number of Glue interactive sessions is a
fact about the ACCOUNT. Reporting it as "your transform failed" is the
misattribution requirement 8 exists to remove, and reporting it through the
frame-width copy would be a second one: it would state a field count and a
ceiling that no capacity refusal has.

Both halves of the seam are covered here, because the requirement spans them:

* the producer — ``glue_session_runner.session_capacity_results`` and the routing
  in ``create_session_failure_results``, which must recognise capacity BEFORE
  ``session_creation_failure_results`` (the else-branch) is reached
* the consumer — ``lambda_function.aggregate_check_results``' harness branch,
  which now chooses its opening clause by reason so that the capacity copy names
  capacity and the two width reasons render exactly the sentence they did before

WHAT IS ASSERTED AS AN ABSENCE, AND WHY THAT IS THE POINT
---------------------------------------------------------
The task's central constraint is **a new reason, not a new field and not a new
state**. Three tests are therefore about things that must NOT have appeared:

* ``test_capacity_refusal_omits_the_frame_width_keys`` — no
  ``not_verified_field_count`` / ``not_verified_frame_ceiling``. With them the
  detail would read "the source schema declares 0 field(s) against a test-frame
  width ceiling of 0", a false claim about the schema made while removing a false
  claim about the transform.
* ``test_capacity_check_carries_no_key_the_width_check_does_not`` — R8.4 made
  checkable at this seam. ``LGR-b2f79ef-2`` (**template**) refused a third check
  field once already, and the comment at ``lambda_function.py:508-527`` records
  it; the state stays p32's ``verified: False``.
* ``test_test_status_is_unchanged_by_the_capacity_check`` — R8.3, and the other
  half of R8.4. ``test_status`` IS the Choice-read path
  (``$.test_code_result.test_result.status``, ``orchestration-stack.ts:851-854``),
  so a capacity entry that cannot move it cannot move the gate.
  ``LGR-69ca78b-1`` (**invariant**) is why an unread key is inert rather than
  permissive: the governance Choice states fail closed.

R8.3 must hold WITHOUT new partition logic — the ``verified: False``
discriminator already does it — so ``is_not_verified_check`` is asserted to
recognise the capacity check while being itself unmodified.

Not duplicated here: the property that a harness non-verification never carries a
passing state, generalised over the reason string, is task 3.7's
(**Property 9**). These are the worked examples it will generalise.

Validates: p42 Requirements 8.1, 8.2, 8.3, 8.4
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from tests.fixtures.run_test_result_shapes import BASE_CONTRACT_KEYS, FRAME_REFUSAL

# Region/account before any boto3 import (LGR-3e45e65-1 pins these in
# tests/unit/conftest.py; these setdefaults are consistent no-ops).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The REAL producer module. Every fixture below is produced by the shipped
# builders rather than transcribed, so a producer renaming a key fails these
# tests instead of passing them vacuously.
from code_interpreter_runner import (
    NOT_VERIFIED_REASON_FRAME_TRUNCATED,
    NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA,
    CodeInterpreterTestRunner,
)
from glue_session_runner import (
    NOT_VERIFIED_REASON_FRAME_UNBUILDABLE,
    NOT_VERIFIED_REASON_SESSION_CAPACITY,
    create_session_failure_results,
    session_capacity_results,
    session_creation_failure_results,
)

_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}
with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: The Glue error code for an account at its session limit. Spelled once here
#: and compared against the producer's own constant below, so a rename in the
#: producer is caught rather than mirrored.
CAPACITY_CODE = "ResourceNumberLimitExceededException"
CAPACITY_MESSAGE = "Maximum number of concurrent sessions reached"


def _capacity_error() -> ClientError:
    """A ``CreateSession`` refusal as boto3 raises it."""
    return ClientError({"Error": {"Code": CAPACITY_CODE, "Message": CAPACITY_MESSAGE}}, "CreateSession")


def _access_denied() -> ClientError:
    """A ``CreateSession`` failure that is NOT capacity (R8.5's path)."""
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "CreateSession")


def _parsed_results(*, verdict_passed: bool = True) -> dict:
    """A reviewer block, built by the shipped parsers rather than transcribed.

    Run through ``parse_review_verdict`` → ``verdict_to_parsed_results`` so the
    shape is whatever production produces; a transcribed dict silently produced
    NO checks at all during development, which made the partition empty and every
    status "failed" for the wrong reason.

    A full check list is on purpose. ``LGR-b2f79ef-2``'s caveat is that a
    ``verified: False`` entry must never be the ONLY check, and the partition
    derives "failed" from an empty verified set — so drawing a real reviewer block
    is what lets the R8.3 assertions distinguish "excluded from the partition"
    from "the partition was empty".
    """
    verdict = {
        "verdict": "pass" if verdict_passed else "fail",
        # A ``fail`` verdict carrying no issue is rejected as malformed by
        # ``parse_review_verdict`` (R13.3: regeneration needs something to act
        # on), so the failing fixture carries one.
        "issues": (
            []
            if verdict_passed
            else [{"severity": "error", "description": "wrong join key", "attribution": "transform_block"}]
        ),
        "categories": {"Correctness": "pass", "Security": "pass"},
    }
    return _mod.verdict_to_parsed_results(_mod.parse_review_verdict(json.dumps(verdict)))


def _frame_refusal_results(reason: str) -> dict:
    """A width refusal from the shipped producer, for the copy-parity tests."""
    return CodeInterpreterTestRunner._frame_truncation_results(600, 512, reason)


def _harness_check(checks: list[dict]) -> dict:
    """The single ``not_verified_by_harness`` entry in *checks*."""
    matches = [c for c in checks if c.get("name") == _mod.NOT_VERIFIED_BY_HARNESS_CHECK_NAME]
    assert len(matches) == 1, f"expected exactly one harness check, got {[c.get('name') for c in checks]}"
    return matches[0]


# ---------------------------------------------------------------------------
# Producer: the capacity result (R8.1)
# ---------------------------------------------------------------------------


def test_the_two_spellings_of_the_capacity_reason_agree():
    """The producer's constant and ``lambda_function``'s spelling are one string.

    They are two spellings by necessity — ``lambda_function``'s sibling producers
    are stubbed with ``MagicMock`` in every test that imports it, so an imported
    constant would arrive as an auto-attribute mock and never match. This is the
    "change both or neither" contract made checkable.
    """
    assert NOT_VERIFIED_REASON_SESSION_CAPACITY == _mod.SESSION_CAPACITY_REASON


def test_the_two_spellings_of_the_frame_unbuildable_reason_agree():
    """The same "change both or neither" contract, for the FOURTH harness reason.

    ``FRAME_UNBUILDABLE_REASON`` (task 8.1) is a second spelling for exactly the
    reason the capacity pair above is — ``lambda_function.py:320-324`` says so in
    those words — but it landed without the assertion that makes the contract
    checkable, while its sibling got one. Added at checkpoint 7: the asymmetry is
    only visible from outside either task.

    Nothing is wrong today; the two literals do match. What is asserted is that a
    rename of either one is *caught* rather than mirrored, which is the whole
    value of duplicating the string deliberately.
    """
    assert NOT_VERIFIED_REASON_FRAME_UNBUILDABLE == _mod.FRAME_UNBUILDABLE_REASON


def test_the_frame_unbuildable_reason_has_consumer_copy():
    """Its rendering arm (``lambda_function.py:664``) can find a label.

    The reason is chosen by the producer and rendered by the consumer, so a reason
    present in one and absent from :data:`NOT_VERIFIED_REASON_LABELS` would reach
    the report as a bare identifier. Asserted for the frame-unbuildable reason
    because it is the one that arrived without a covering test.
    """
    assert _mod.FRAME_UNBUILDABLE_REASON in _mod.NOT_VERIFIED_REASON_LABELS
    label = _mod.NOT_VERIFIED_REASON_LABELS[_mod.FRAME_UNBUILDABLE_REASON]
    assert label and not label.startswith(_mod.FRAME_UNBUILDABLE_REASON), (
        "the label must be prose for the report, not the identifier echoed back"
    )


def test_capacity_refusal_reports_the_harness_marker():
    """R8.1: a harness non-verification naming capacity as the reason."""
    result = session_capacity_results(_capacity_error())

    assert result["not_verified_by_harness"] is True
    assert result["not_verified_reason"] == NOT_VERIFIED_REASON_SESSION_CAPACITY


def test_capacity_refusal_reports_that_nothing_ran():
    """R8.2 at the producer: nothing executed, nothing succeeded, no exit code."""
    result = session_capacity_results(_capacity_error())

    assert result["execution_successful"] is False
    assert result["executed"] is False
    assert result["exit_code"] is None
    # Load-bearing, not incidental: an empty `data_quality_results` is what
    # suppresses the `schema_compliance` score for a run that produced no
    # validation output (C13).
    assert result["data_quality_results"] == {}


def test_capacity_refusal_names_the_limit_in_error_details():
    """The reported error, so a reader knows it was a limit and not their code."""
    result = session_capacity_results(_capacity_error())

    assert CAPACITY_MESSAGE in result["error_details"]
    assert "limit" in result["error_details"]


def test_capacity_refusal_is_the_frame_refusals_sibling_shape():
    """The nine base contract keys plus the harness marker and its reason.

    Compared against ``FRAME_REFUSAL``'s declared key set rather than a
    transcribed list: the capacity shape is defined as that shape minus the two
    width keys, so the fixture is the oracle for everything else.
    """
    observed = set(session_capacity_results(_capacity_error()))

    assert observed >= BASE_CONTRACT_KEYS
    assert observed == FRAME_REFUSAL.keys - {_mod.HARNESS_FIELD_COUNT_KEY, _mod.HARNESS_FRAME_CEILING_KEY}


def test_capacity_refusal_omits_the_frame_width_keys():
    """No field count and no ceiling, because a capacity refusal has neither.

    Carrying them as zeroes would render "the source schema declares 0 field(s)
    against a test-frame width ceiling of 0" — a false claim about the schema,
    made in the course of removing a false claim about the transform.
    """
    result = session_capacity_results(_capacity_error())

    assert _mod.HARNESS_FIELD_COUNT_KEY not in result
    assert _mod.HARNESS_FRAME_CEILING_KEY not in result


def test_capacity_refusal_claims_no_proxy_non_verification():
    """R3.2 holds here too: no Glue result carries the proxy's marker."""
    assert "not_verified_by_proxy" not in session_capacity_results(_capacity_error())


# ---------------------------------------------------------------------------
# Producer: the routing (R8.1 before R8.5)
# ---------------------------------------------------------------------------


def test_create_session_failure_routes_capacity_to_the_non_verification():
    """R8.1: capacity is recognised BEFORE the else-branch is reached."""
    result = create_session_failure_results(_capacity_error())

    assert result["not_verified_by_harness"] is True
    assert result["not_verified_reason"] == NOT_VERIFIED_REASON_SESSION_CAPACITY


def test_create_session_failure_routes_everything_else_to_a_failure():
    """R8.5 survives: a non-capacity refusal is still a failed verification.

    Asserted by equality against the else-branch builder's own output, so the
    routing cannot quietly start reshaping the non-capacity path.
    """
    exc = _access_denied()

    assert create_session_failure_results(exc) == session_creation_failure_results(exc)


def test_a_non_capacity_refusal_carries_no_harness_marker():
    """The attribution runs both ways: only capacity gets the non-verification."""
    result = create_session_failure_results(_access_denied())

    assert "not_verified_by_harness" not in result
    assert result["execution_successful"] is False
    assert result["executed"] is False
    assert "not authorized" in result["error_details"]


def test_a_non_boto_exception_routes_to_a_failure():
    """An exception with no ``response`` is not capacity and must not read as it.

    ``_error_code`` returns ``""`` for anything that is not a boto3 error, so the
    predicate answers False — which is the safe direction: an unclassifiable
    failure is reported as a failure rather than excused as a limit.
    """
    result = create_session_failure_results(RuntimeError("boom"))

    assert "not_verified_by_harness" not in result
    assert "boom" in result["error_details"]


# ---------------------------------------------------------------------------
# Consumer: the check (R8.2), the partition (R8.3), the copy
# ---------------------------------------------------------------------------


def test_capacity_check_carries_neither_passed_nor_verified():
    """R8.2: ``verified`` false AND ``passed`` false, on the existing name."""
    _, checks = _mod.aggregate_check_results(_parsed_results(), session_capacity_results(_capacity_error()))
    check = _harness_check(checks)

    assert check["passed"] is False
    assert check["verified"] is False
    assert check["not_verified_reason"] == _mod.SESSION_CAPACITY_REASON


def test_no_code_interpreter_execution_check_is_emitted():
    """The capacity entry REPLACES the execution check rather than joining it.

    Same substitution p32 established for a proxy refusal: a report that carried
    both would be claiming an execution result for a run that never executed.
    """
    _, checks = _mod.aggregate_check_results(_parsed_results(), session_capacity_results(_capacity_error()))

    assert not [c for c in checks if c.get("name") == "code_interpreter_execution"]


def test_is_not_verified_check_recognises_the_capacity_check_unchanged():
    """R8.3's mechanism: the existing ``verified: False`` discriminator.

    ``is_not_verified_check`` is not modified by this task, and this is the
    assertion that it did not need to be.
    """
    _, checks = _mod.aggregate_check_results(_parsed_results(), session_capacity_results(_capacity_error()))

    assert _mod.is_not_verified_check(_harness_check(checks)) is True


def test_test_status_is_unchanged_by_the_capacity_check():
    """R8.3 and R8.4: the entry neither passes nor fails the run.

    ``test_status`` is compared against the same reviewer block aggregated with a
    SUCCESSFUL execution. Equal statuses mean the capacity entry was partitioned
    out — and because ``test_status`` is the value the only Choice state over this
    step reads (``orchestration-stack.ts:851-854``), it also means the gate cannot
    see the difference.
    """
    executed = {
        "execution_successful": True,
        "executed": True,
        "exit_code": 0,
        "stderr": "",
        "input_records": 100,
        "output_records": 100,
        "data_quality_results": {},
        "error_details": None,
        "conversion_report": None,
    }

    capacity_status, capacity_checks = _mod.aggregate_check_results(
        _parsed_results(), session_capacity_results(_capacity_error())
    )
    executed_status, _ = _mod.aggregate_check_results(_parsed_results(), executed)

    # Anti-vacuity, and it is not hypothetical: a transcribed reviewer block
    # produced ZERO checks during development, which emptied the partition and
    # made both statuses "failed" — the equality would have held for the wrong
    # reason.
    assert [c for c in capacity_checks if not _mod.is_not_verified_check(c)], (
        "the reviewer block produced no verified checks, so this comparison would "
        "hold vacuously — the partition is empty rather than the capacity entry "
        "being excluded from it."
    )
    assert capacity_status == executed_status == "passed"


def test_a_failing_static_check_still_fails_a_capacity_run():
    """Partitioned out is not "ignored": the other checks still decide.

    The verdict half of the derivation reads ``parsed_results`` alone
    (``lambda_function.py:445-484``), so a reviewer failure survives a run in
    which nothing executed.
    """
    status, _ = _mod.aggregate_check_results(
        _parsed_results(verdict_passed=False), session_capacity_results(_capacity_error())
    )

    assert status == "failed"


def test_capacity_check_carries_no_key_the_width_check_does_not():
    """R8.4: a new reason adds no new FIELD to a check.

    The capacity entry's key set must be a subset of the width entry's — same
    name, same three carriers of state, no fourth. ``LGR-b2f79ef-2`` refused a
    third check field once already (``lambda_function.py:508-527``).
    """
    _, capacity_checks = _mod.aggregate_check_results(_parsed_results(), session_capacity_results(_capacity_error()))
    _, width_checks = _mod.aggregate_check_results(
        _parsed_results(), _frame_refusal_results(NOT_VERIFIED_REASON_FRAME_TRUNCATED)
    )

    assert set(_harness_check(capacity_checks)) == set(_harness_check(width_checks))


def test_capacity_check_detail_names_capacity_and_not_a_frame_width():
    """The reason's copy, as TEXT (LGR-99050e6-1's house pattern).

    Both halves matter. It must say what happened — an account at its session
    limit, nothing to fix, retry later — and it must NOT render the width
    sentence, which for a capacity refusal would state a field count and a
    ceiling that do not exist.
    """
    _, checks = _mod.aggregate_check_results(_parsed_results(), session_capacity_results(_capacity_error()))
    detail = _harness_check(checks)["detail"]

    assert "concurrent Glue interactive" in detail
    assert "not a defect in the generated" in detail
    assert _mod.SESSION_CAPACITY_REASON in detail
    # The width copy must not leak onto this path.
    assert "width ceiling" not in detail
    assert "field(s)" not in detail
    assert "None" not in detail


@pytest.mark.parametrize("reason", [NOT_VERIFIED_REASON_FRAME_TRUNCATED, NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA])
def test_the_width_reasons_copy_is_unchanged(reason):
    """Regression on the restructure: the two width sentences are byte-identical.

    The harness branch's opening clause became reason-dependent so capacity could
    have its own. That refactor must be invisible to the two reasons that were
    there first — this is the assertion that it is.
    """
    _, checks = _mod.aggregate_check_results(_parsed_results(), _frame_refusal_results(reason))
    detail = _harness_check(checks)["detail"]

    assert detail.startswith(
        "NOT VERIFIED by the test harness: the source schema declares 600 field(s) "
        "against a test-frame width ceiling of 512, so the transform was never executed. "
        f"Reason ({reason}): "
    )
    assert detail.endswith("No schema-compliance score is reported for this run.")


# ---------------------------------------------------------------------------
# Consumer: the prose sites (R6's two, carrying the new reason)
# ---------------------------------------------------------------------------


def test_the_capacity_reason_has_a_label_so_the_note_is_not_a_raw_string():
    """``execution_verification_note`` renders the label, not the bare reason.

    The fallback to the raw reason string is deliberate and correct for an
    unmapped reason, but reaching it here would mean the reason shipped without
    its copy — the failure mode that left p33's two harness reasons unlabelled for
    a release.
    """
    _, checks = _mod.aggregate_check_results(_parsed_results(), session_capacity_results(_capacity_error()))

    note = _mod.execution_verification_note(checks)

    assert note == f"Transform NOT executed — {_mod.NOT_VERIFIED_REASON_LABELS[_mod.SESSION_CAPACITY_REASON]}."
    assert _mod.SESSION_CAPACITY_REASON not in note
    assert _mod.execution_was_verified(checks) is False
