# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: the two Sample_Read_Failure builders' SHAPE and their sentence.

Spec `p44-verify-against-real-source-data` task 3.2 — requirements 2.1, 2.2, 9.2.

Task 3.1 added a fifth `not_verified_reason` and the two builders that emit it:
``sample_read_failure_results`` (the frame-build statement ran against the
pipeline's own source object and Glue reported a failure) and
``sample_read_deadline_results`` (it never reached a terminal state inside
``_STATEMENT_DEADLINE_SECONDS``). Both are pure functions of their arguments —
**no Glue client, mocked or otherwise, is constructed in this file, and no
session is created** (p44 do-NOT 1: sessions bill).

WHY THIS FILE EXISTS ALONGSIDE TWO PROPERTIES
---------------------------------------------
It is the readable failure message. Task 6.2's Property 4 asserts the same shape
across the whole failure space and task 7.1's amended nine-outcome enumeration
asserts the marker and the reason through ``run_test``; when one of those goes
red it reports a Hypothesis counterexample or a ten-way enumeration mismatch.
These are the worked examples underneath both, so a break names the key.

It is also the first direct coverage of ``_failure_reason_and_stderr``, which
task 3.1 extracted out of ``statement_failure_results`` and which CodeGraph
flags with no covering tests. That function is R2.3's classification branch and
it is deliberately shared by two builders, so the tests below use it as the
ORACLE for the reason rather than transcribing "AccessDenied: …" — a transcribed
expectation would keep passing after the two copies of the branch drifted apart,
which is the whole failure mode the extraction removes.

WHAT IS ASSERTED AS AN ABSENCE, AND WHY EACH ABSENCE IS THE POINT
-----------------------------------------------------------------
* **No frame-width keys.** ``session_capacity_results``' reasoning, verbatim:
  there is no width problem in a source read, so carrying
  ``HARNESS_FIELD_COUNT_KEY`` / ``HARNESS_FRAME_CEILING_KEY`` as zeroes would
  render "the source schema declares 0 field(s) against a test-frame width
  ceiling of 0" — a false claim about the schema, made while removing a false
  one about the transform.
* **No stage label in ``error_details``.** ``STAGE_FRAME_BUILD`` is
  ``"test data generation"``, so routing this observation through
  ``statement_failure_results`` yields "test data generation failed: …" for a
  stage that was generating nothing. R2.1 exists to remove exactly that
  sentence, which is why the text names the source read and the object instead.
* **``executed`` false and no ``exit_code``.** R2.2 — a Sample_Read_Failure must
  be distinguishable from a transform that ran and failed. The transform
  statement is never submitted after a failed stage.
* **``conversion_report`` is ``None``** — ``LGR-ad78946-2``
  (**template / ratified**), read in full: the key means "None when conversion
  failed or never ran", the Glue runner runs no conversion by design, and
  synthesising a report was rejected as manufacturing a diagnostic about a
  translation that did not happen.
* **``stderr`` stays ``""``.** The synthesised traceback renders the transform's
  execution and this run has none; the observed reason travels in
  ``error_details``, which is the one sentence a reviewer reads.

The last test is the other direction, and it is not decoration:
``statement_failure_results`` must still report an ``executed`` run with the
"test data generation failed: …" sentence for a SYNTHETIC frame. Task 6.1 routes
only the sample path away from it. A change that made the honest sentence
unreachable for every run would be a regression this file names.

Not asserted here, and deliberately: the second spelling in ``lambda_function``,
the Python reason→label entry and the dashboard label are task 4's four-place
obligation, and ``test_not_verified_reason_labels_cross_language.py`` is their
gate.

Validates: p44 Requirements 2.1, 2.2, 9.2
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
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

# The REAL producer module. Every result below is produced by the shipped
# builders rather than transcribed, so a producer renaming a key fails these
# tests instead of passing them vacuously.
from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    NOT_VERIFIED_REASON_SOURCE_READ_FAILED,
    STAGE_FRAME_BUILD,
    FrameSource,
    GlueSessionError,
    StatementOutcome,
    _failure_reason_and_stderr,
    sample_read_deadline_results,
    sample_read_failure_results,
    sample_read_format,
    statement_failure_results,
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
# Oracles
# ---------------------------------------------------------------------------

#: The two extra keys a harness non-verification carries, DERIVED rather than
#: transcribed: the frame refusal's declared key set, minus the nine base keys,
#: minus the two width keys the renderer owns. ``LGR-28b4d75-5``
#: (**template / ratified**) is the posture — an expectation read off the thing
#: under test cannot detect it drifting — and the derivation is load-bearing in
#: both directions: a rename of either width constant in ``lambda_function``
#: leaves three keys here and fails the anti-vacuity assertion below rather than
#: quietly weakening every absence test in this file.
HARNESS_MARKER_KEYS = (
    FRAME_REFUSAL.keys - BASE_CONTRACT_KEYS - {_mod.HARNESS_FIELD_COUNT_KEY, _mod.HARNESS_FRAME_CEILING_KEY}
)

#: What both builders must return: the nine base keys plus exactly those two.
EXPECTED_KEYS = BASE_CONTRACT_KEYS | HARNESS_MARKER_KEYS

#: A `sourceBucket` object URI of the shape `file_validation` writes —
#: `original_s3_uri = f"s3://{bucket}/{key}"` from the upload event's own bucket
#: (`file_validation/lambda_function.py:632`).
SOURCE_URI = "s3://adi-source-files-123456789012-us-west-2/uploads/orders.csv"

ACCESS_DENIED_NAME = "AccessDeniedException"
ACCESS_DENIED_VALUE = "An error occurred (403) when calling the HeadObject operation: Forbidden"


def test_the_derived_marker_key_set_is_the_two_keys_it_claims_to_be():
    """Anti-vacuity for every assertion built on :data:`HARNESS_MARKER_KEYS`.

    If the derivation ever yielded the empty set — a width constant renamed, a
    fixture key added — the shape equalities below would still hold while
    asserting less, and the two absence tests would pass on a result carrying
    neither marker. This is the one place that cannot be derived, so it is the
    one place the count is written down.
    """
    assert len(HARNESS_MARKER_KEYS) == 2, HARNESS_MARKER_KEYS
    assert sorted(HARNESS_MARKER_KEYS) == ["not_verified_by_harness", "not_verified_reason"]


# ---------------------------------------------------------------------------
# Fixtures — built through the shipped resolver, not transcribed
# ---------------------------------------------------------------------------


def _sample_source(uri: str = SOURCE_URI) -> FrameSource:
    """A Sample_Frame decision, with the format resolved by production code.

    ``read_format`` comes from :func:`sample_read_format` rather than the string
    ``"csv"``, so the fixture cannot describe a frame source the resolver would
    never produce — and ``FrameSource.__post_init__`` refuses a ``sample`` mode
    without both a URI and a format, so a resolver change that stopped
    recognising ``.csv`` fails here loudly instead of silently.
    """
    read_format = sample_read_format(uri)
    assert read_format is not None, f"{uri} must resolve a reader for this fixture to be a sample"
    return FrameSource(
        mode=FRAME_MODE_SAMPLE,
        requested_mode=FRAME_MODE_SAMPLE,
        source_uri=uri,
        read_format=read_format,
    )


def _synthetic_source() -> FrameSource:
    """The frame every run gets today: no caller supplies ``frame_mode`` yet."""
    return FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode=FRAME_MODE_SYNTHETIC)


def _denied_frame_build() -> StatementOutcome:
    """The failure C11 predicts: the object is there, the role cannot open it.

    ``AVAILABLE`` with an ``Output.Status`` other than ``ok`` — Spark raised —
    which is :attr:`StatementOutcome.failed`'s first arm.
    """
    return StatementOutcome(
        stage=STAGE_FRAME_BUILD,
        state="AVAILABLE",
        output_status="error",
        stdout="",
        statement={
            "State": "AVAILABLE",
            "Output": {
                "Status": "error",
                "ErrorName": ACCESS_DENIED_NAME,
                "ErrorValue": ACCESS_DENIED_VALUE,
                "Traceback": ['  File "<stdin>", line 1, in <module>\n'],
                "Data": {"TextPlain": ""},
            },
        },
    )


def _cancelled_frame_build(state: str = "CANCELLED") -> StatementOutcome:
    """A statement that stopped without producing an ``Output`` at all.

    :attr:`StatementOutcome.failed`'s second arm, and the reason the shared
    classifier has two branches: Glue reports no ``Output`` here, so the reached
    state IS the evidence.
    """
    return StatementOutcome(
        stage=STAGE_FRAME_BUILD,
        state=state,
        output_status=None,
        stdout="",
        statement={"State": state},
    )


def _deadline_exception() -> GlueSessionError:
    """What ``_await_statement`` raises when nothing reached a terminal state."""
    return GlueSessionError("Glue statement did not reach a terminal state within 120s")


#: Both builders, each with the argument its own door supplies, so every shape
#: assertion below runs against both without being written twice.
BOTH_BUILDERS = [
    pytest.param(lambda: sample_read_failure_results(_denied_frame_build(), _sample_source()), id="statement_failure"),
    pytest.param(lambda: sample_read_deadline_results(_deadline_exception(), _sample_source()), id="deadline"),
]


# ---------------------------------------------------------------------------
# Shape — the nine base keys plus exactly two (R9.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", BOTH_BUILDERS)
def test_builder_returns_the_capacity_refusals_sibling_shape(build):
    """R9.2: the nine base contract keys plus the two harness markers, no more.

    Compared against the fixture's declared frame-refusal key set minus the two
    width keys, which is how ``session_capacity_results`` and
    ``frame_build_failure_results`` are already held. Same shape, third and
    fourth producers of it.
    """
    observed = set(build())

    assert observed >= BASE_CONTRACT_KEYS
    assert observed == EXPECTED_KEYS


def test_the_two_builders_return_the_same_key_set():
    """Two doors onto one reason must not be two shapes.

    ``aggregate_check_results`` reports both through the same harness arm, so a
    key present on one and absent from the other would make the rendered check
    depend on which door was taken.
    """
    statement_result = sample_read_failure_results(_denied_frame_build(), _sample_source())
    deadline_result = sample_read_deadline_results(_deadline_exception(), _sample_source())

    assert set(statement_result) == set(deadline_result)


@pytest.mark.parametrize("build", BOTH_BUILDERS)
def test_builder_reports_that_nothing_ran(build):
    """R2.2: ``executed`` false, nothing succeeded, and NO ``exit_code``.

    The distinction the whole requirement turns on. ``executed=True`` with
    ``exit_code=1`` — what ``statement_failure_results`` produces — is "it ran
    and failed"; this is "it never ran", and a reviewer must be able to tell
    them apart from the result alone.
    """
    result = build()

    assert result["executed"] is False
    assert result["execution_successful"] is False
    assert result["exit_code"] is None
    assert result["input_records"] == 0
    assert result["output_records"] == 0
    # Load-bearing rather than incidental: an empty `data_quality_results` is
    # what suppresses the `schema_compliance` score for a run that produced no
    # validation output.
    assert result["data_quality_results"] == {}


@pytest.mark.parametrize("build", BOTH_BUILDERS)
def test_builder_leaves_the_conversion_report_none(build):
    """``LGR-ad78946-2`` (**template / ratified**): ``None`` on every Glue result.

    The Glue runner runs no conversion by design, and the record rejected
    synthesising a report as manufacturing a diagnostic about a translation that
    did not happen. Asserted per builder because the value is a builder's to get
    wrong — nothing downstream re-derives it on a non-verification path.
    """
    assert build()["conversion_report"] is None


@pytest.mark.parametrize("build", BOTH_BUILDERS)
def test_builder_omits_the_frame_width_keys(build):
    """No field count and no ceiling, because a source read has neither.

    Carrying them as zeroes would render "the source schema declares 0 field(s)
    against a test-frame width ceiling of 0" — a false claim about the schema,
    made in the course of removing a false claim about the transform.
    """
    result = build()

    assert _mod.HARNESS_FIELD_COUNT_KEY not in result
    assert _mod.HARNESS_FRAME_CEILING_KEY not in result


@pytest.mark.parametrize("build", BOTH_BUILDERS)
def test_builder_claims_no_proxy_non_verification(build):
    """p42 R3.2 holds here too: no Glue result carries the pandas proxy's marker."""
    assert "not_verified_by_proxy" not in build()


@pytest.mark.parametrize("build", BOTH_BUILDERS)
def test_builder_leaves_stderr_empty(build):
    """``stderr`` stays ``""``, as both harness siblings leave it.

    Not an omission: the synthesised traceback is a rendering of the transform's
    execution, and a run whose source could not be read has none to render. The
    observed reason travels in ``error_details``. ``_failure_reason_and_stderr``
    returns a stderr half and ``sample_read_failure_results`` discards it, which
    is deliberate — this is the assertion that it still does.
    """
    assert build()["stderr"] == ""


# ---------------------------------------------------------------------------
# The marker and the fifth reason (R2.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", BOTH_BUILDERS)
def test_builder_reports_the_harness_marker_and_the_source_read_reason(build):
    """R2.1: a harness non-verification naming the source read as the failure."""
    result = build()

    assert result["not_verified_by_harness"] is True
    assert result["not_verified_reason"] == NOT_VERIFIED_REASON_SOURCE_READ_FAILED


def test_the_two_doors_report_one_reason():
    """One reason, two doors — a statement failure and an elapsed deadline.

    Both are "the harness could not read the run's source object", so they share
    the reason and differ only in what ``error_details`` says. Two reasons here
    would demand two dashboard labels for one fact.
    """
    statement_result = sample_read_failure_results(_denied_frame_build(), _sample_source())
    deadline_result = sample_read_deadline_results(_deadline_exception(), _sample_source())

    assert statement_result["not_verified_reason"] == deadline_result["not_verified_reason"]


def test_the_reason_is_the_wire_value_the_dashboard_will_key_on():
    """The identifier itself, written down once.

    It crosses into ``lambda_function``'s second spelling, its label map and the
    dashboard's ``NOT_VERIFIED_LABELS`` — all task 4's, all keyed on this exact
    string. The cross-language contract test is the gate for those; this pins
    the producer's half so a rename is a visible diff here too.
    """
    assert NOT_VERIFIED_REASON_SOURCE_READ_FAILED == "source_read_failed"


# ---------------------------------------------------------------------------
# The sentence — names the read and the object, never the stage label
# ---------------------------------------------------------------------------


def test_error_details_names_the_source_read_and_the_object():
    """R2.1: the failing operation is the source read, and the object is named.

    The URI is what makes the sentence actionable — the reviewer's next step is
    to check that object and the session role's reach to it, which they cannot
    do from a message that says only "the source could not be read".
    """
    detail = sample_read_failure_results(_denied_frame_build(), _sample_source())["error_details"]

    assert "source object could not be read" in detail
    assert SOURCE_URI in detail


def test_deadline_error_details_names_the_deadline_and_the_object():
    """R3.4: the same reason, and a sentence that says which door was taken.

    ``lifecycle_failure_results`` — where this raise lands today — already
    reports ``executed`` false, so the deadline builder's whole contribution is
    the marker plus a sentence naming the source read. If the text stopped
    distinguishing the deadline from a statement failure, that contribution
    would be invisible to a reviewer.
    """
    exc = _deadline_exception()
    detail = sample_read_deadline_results(exc, _sample_source())["error_details"]

    assert "source object could not be read" in detail
    assert "deadline" in detail
    assert SOURCE_URI in detail
    assert str(exc) in detail


@pytest.mark.parametrize("build", BOTH_BUILDERS)
def test_error_details_never_names_the_frame_build_stage(build):
    """The misattribution R2.1 exists to remove, asserted as an absence.

    ``STAGE_FRAME_BUILD`` is ``"test data generation"``, so the builder these
    observations reach today renders "test data generation failed: …" — a
    reported failure of a stage that was generating nothing, for a transform
    that never ran. Asserted against the constant rather than the literal so a
    relabelled stage cannot slip the string back in under a new name.
    """
    detail = build()["error_details"]

    assert STAGE_FRAME_BUILD not in detail
    assert f"{STAGE_FRAME_BUILD} failed" not in detail


# ---------------------------------------------------------------------------
# R2.3 — the classification comes from the SHARED classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(_denied_frame_build(), id="available_with_error_status"),
        pytest.param(_cancelled_frame_build(), id="cancelled"),
        pytest.param(_cancelled_frame_build("ERROR"), id="session_error"),
    ],
)
def test_the_reason_is_the_shared_classifiers_own_output(outcome):
    """R2.3: both arms of ``_failure_reason_and_stderr``, used and not copied.

    The expectation is the classifier's return value, so this cannot pass while
    the builder re-derives the reason from the same fields by a second route —
    which is the defect the extraction in task 3.1 removed. The ``CANCELLED``
    and ``ERROR`` params are the arm that reads no ``Output`` at all: Glue
    reports none, so the reached state is the evidence.
    """
    reason, _stderr = _failure_reason_and_stderr(outcome)
    detail = sample_read_failure_results(outcome, _sample_source())["error_details"]

    assert reason, "the classifier must always name something, or the sentence trails off"
    assert detail.endswith(f"— {reason}")


def test_both_builders_of_one_observation_agree_on_the_reason():
    """One classifier, two builders — asserted across the seam it spans.

    ``statement_failure_results`` keeps its own sentence shape
    (``"{stage} failed: {reason}"``); what the two must share is the *reason*.
    A second copy of the ``if`` would be free to disagree about what a
    ``CANCELLED`` statement means, and this is the test that would notice.
    """
    outcome = _cancelled_frame_build()
    reason, _stderr = _failure_reason_and_stderr(outcome)

    harness_detail = sample_read_failure_results(outcome, _sample_source())["error_details"]
    executed_detail = statement_failure_results(outcome)["error_details"]

    assert reason in harness_detail
    assert reason in executed_detail


# ---------------------------------------------------------------------------
# The other direction — the honest sentence for a SYNTHETIC frame survives
# ---------------------------------------------------------------------------


def test_statement_failure_results_still_reports_an_executed_run():
    """R9.2 and the scope boundary: task 3.1 changed no existing routing.

    For a Synthetic_Frame, a failed frame build IS a stage that was generating
    test data, so "test data generation failed: …" with ``executed`` true is the
    accurate sentence and must stay reachable. Task 6.1 routes only the sample
    path away from it; a change that made this unreachable for every run would
    be the mirror image of the defect p44 removes.
    """
    result = statement_failure_results(_denied_frame_build())

    assert result["executed"] is True
    assert result["exit_code"] == 1
    assert result["execution_successful"] is False
    assert result["error_details"].startswith(f"{STAGE_FRAME_BUILD} failed: ")
    assert "not_verified_by_harness" not in result


def test_a_synthetic_frame_source_is_not_a_sample():
    """The predicate task 6.1 routes on, pinned where these fixtures are read.

    ``is_sample`` is what selects the two builders above, and it is false for
    every run today because no production caller supplies ``frame_mode``. Stated
    here so the fixtures' asymmetry is deliberate rather than incidental.
    """
    assert _sample_source().is_sample is True
    assert _synthetic_source().is_sample is False
