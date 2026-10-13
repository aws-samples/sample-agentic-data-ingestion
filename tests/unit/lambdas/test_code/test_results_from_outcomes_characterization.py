# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization — ``results_from_outcomes``' three routes, as they read today.

Spec ``p44-verify-against-real-source-data`` task 2.1. **No production change
accompanies this file.** It pins ``glue_session_runner.results_from_outcomes``
(``:3366``, routing at ``:3386-3391``) exactly as it reads on disk now, so that
task 6.1's deliberate change to one of those routes is legible as a change.

*Every line number in this file was read against the working tree with p44 task
3.1 already landed* — that task inserted ``NOT_VERIFIED_REASON_SOURCE_READ_FAILED``
and the two sample-read builders above these functions, so citations taken from
the spec documents (which predate it) read about 80 lines low. Symbol names are
the durable reference; the numbers are a convenience.

Why a direct pin is owed
------------------------

CodeGraph reports ``results_from_outcomes`` with **one caller** (``run_test``,
``:770``) and ⚠️ **no covering tests**. That flag is right about *pins* and wrong
about *coverage*, and the distinction is the whole reason this file exists:
``test_no_proxy_non_verification_prop.py:129-132`` says so in its own words —
"Two symbols report no covering tests: ``results_from_outcomes`` and
``frame_build_failure_results`` … This file drives both through the public
method, so it *adds* their first coverage."

So there is **behavioural** coverage through ``run_test`` and **no direct pin**.
The only instrument that can currently observe a routing change is that file's
nine-outcome enumeration (``ALL_KINDS``, ``:290-300``) — which p44 task 7.1
edits, in this same spec, to add a tenth. Without the pin below, the evidence
that task 6.1 changed nothing but the sample path would be an *absence of
failures in a property whose generator was being edited at the same time*.

Nothing here constructs a Glue client, mocked or otherwise, and nothing creates a
session: ``results_from_outcomes`` is a pure function of a sequence of
observations. ``glue_session_runner`` imports only the standard library,
``boto3`` and ``code_interpreter_runner`` (``:70-81``), so there is no
``shared/`` module in this file's blast radius.

Where each expectation comes from
---------------------------------

``LGR-28b4d75-5`` (**template / ratified**): "a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle." Nothing below was snapshotted from a run. The three routes are read off
``results_from_outcomes``' own docstring — "the **last** outcome is the whole
verdict", and the empty list is "mapped rather than asserted because the
alternative is an ``IndexError`` from a result-mapping function" — and the
per-route values off the builders' documented contracts:
``not_executed_results`` is "``executed`` false, no ``exit_code``" (``:3193``),
``statement_failure_results`` is "``executed`` true … non-zero ``exit_code``"
(``:3144``), ``success_results`` is "``execution_successful`` true, ``executed``
true, ``exit_code`` zero" (``:3074``). ``conversion_report`` is ``None`` on every
Glue result by ``LGR-ad78946-2`` (**template / ratified**).

What task 6.1 will invert, and why that is a reversal rather than a regression
-----------------------------------------------------------------------------

``test_a_failed_frame_build_is_reported_as_a_failed_transform_today`` pins a
**defect**, on purpose and with its expiry written down.

Today a frame-build statement that fails routes to
``statement_failure_results``, which sets ``executed=True``, ``exit_code=1`` and
an ``error_details`` opening with the stage label ``"test data generation
failed: "``. For a synthetic frame that is defensible — the harness built the
frame, so a failure there is the harness's own. For a **sample** frame it is the
dishonesty p44 R2 exists to remove: p44 requirements R2.1 and R2.2 require that a
run whose *source object* could not be read reports ``executed`` **false** and a
harness non-verification naming the source read, because no transform ran.

Task 6.1 satisfies that in ``run_test`` — the one place holding both
``run.frame_source`` and ``run.outcomes`` — and deliberately leaves
``results_from_outcomes`` untouched, so every synthetic run keeps its routing
byte-identical. **This file should therefore still pass unchanged after task
6.1.** If it ever needs amending, ``LGR-60c4985-6`` (**template / ratified**) is
the practice to follow: amend the assertion, never delete it, and record the
reversal at the assertion site so it reads as a reversal and not as a
regression.

``test_no_route_here_carries_a_harness_non_verification_marker`` is the guard for
that split: it is true today and must stay true after task 6.1, because the new
route belongs to ``run_test``. A failure there means the harness marker was added
one level too deep, where it would attach to synthetic runs as well.
"""

import os
import sys

from tests.fixtures.run_test_result_shapes import BASE_CONTRACT_KEYS

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from glue_session_runner import (
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    STAGE_VALIDATION,
    StatementOutcome,
    not_executed_results,
    results_from_outcomes,
    statement_failure_results,
    success_results,
)

# The markers the stages print, in the shape `_parse_results` reads them. Only
# `success_results` consults stdout; the other two routes ignore it.
GENERATED_MARKER = 'GENERATED:{"record_count": 100}'
VALIDATION_MARKER = (
    'VALIDATION:{"record_count": 100, "completeness": 0.95, "uniqueness": 1.0, '
    '"schema_compliance": 1.0, "overall_quality": 0.98, "null_counts": {"value": 5}, '
    '"duplicates": 0, "missing_columns": []}'
)

ERROR_NAME = "AnalysisException"
ERROR_VALUE = "[UNRESOLVED_COLUMN] cannot resolve `nope` given input columns: [id, value]"

# Header-free by construction — a Glue `Traceback` is a list of frame strings and
# carries no `Traceback (most recent call last):` line. Present here only so the
# failure route has a realistic statement to render; the classification never
# reads it (see `StatementOutcome.failed`, `:305`).
GLUE_TRACEBACK = [
    '  File "<stdin>", line 1, in <module>\n',
    '  File "/opt/spark/python/pyspark/sql/dataframe.py", line 5107, in withColumn\n',
]


# ---------------------------------------------------------------------------
# Observations, built by hand rather than captured
# ---------------------------------------------------------------------------


def _ok(stage: str, stdout: str = "") -> StatementOutcome:
    """A statement that reached ``AVAILABLE`` with ``Output.Status`` ``ok``."""
    return StatementOutcome(
        stage=stage,
        state="AVAILABLE",
        output_status="ok",
        stdout=stdout,
        statement={"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": stdout}}},
    )


def _failed(stage: str, stdout: str = "") -> StatementOutcome:
    """A statement that reached ``AVAILABLE`` and reported an error status."""
    return StatementOutcome(
        stage=stage,
        state="AVAILABLE",
        output_status="error",
        stdout=stdout,
        statement={
            "State": "AVAILABLE",
            "Output": {
                "Status": "error",
                "ErrorName": ERROR_NAME,
                "ErrorValue": ERROR_VALUE,
                "Traceback": GLUE_TRACEBACK,
                "Data": {"TextPlain": stdout},
            },
        },
    )


def _clean_run() -> list[StatementOutcome]:
    """The three stages, every one ``ok`` — what a passing verification observes."""
    return [
        _ok(STAGE_FRAME_BUILD, GENERATED_MARKER),
        _ok(STAGE_TRANSFORM),
        _ok(STAGE_VALIDATION, VALIDATION_MARKER),
    ]


# ---------------------------------------------------------------------------
# Route 1 — an empty outcome list
# ---------------------------------------------------------------------------


def test_an_empty_outcome_list_is_mapped_and_never_raises():
    """The docstring's own claim: mapped, not asserted, so no ``IndexError``.

    ``run_guarded_session`` always submits the frame build first, so this input is
    unreachable through ``run_test``. It is handled anyway because the alternative
    the docstring names — "an ``IndexError`` from a result-mapping function" — is
    "the least diagnosable failure available".
    """
    result = results_from_outcomes([])

    assert isinstance(result, dict)
    assert set(result) == BASE_CONTRACT_KEYS


def test_an_empty_outcome_list_routes_to_the_not_executed_shape():
    """``not_executed_results``: ``executed`` false, no ``exit_code`` (``:3193``).

    The route is pinned two ways on purpose. The value assertions state the shape
    independently, and the equality states *which builder* was chosen — a value
    check alone would stay green if the routing moved to another builder that
    happened to agree on these four fields.
    """
    result = results_from_outcomes([])

    assert result["executed"] is False
    assert result["execution_successful"] is False
    assert result["exit_code"] is None
    assert result["conversion_report"] is None
    assert result == not_executed_results(result["error_details"])


def test_an_empty_outcome_list_says_nothing_was_submitted_and_nothing_verified():
    """A result that proved nothing has to say so, and name why.

    ``not_executed_results`` is a *failure* rather than a non-verification
    precisely because it "named no reason a reviewer can act on" in its own docstring,
    so the one thing this route owes is a reason. Asserted as substrings rather
    than as the whole sentence: the claim being pinned is that the detail names
    the absent statement, not the constant's exact punctuation.
    """
    detail = results_from_outcomes([])["error_details"]

    assert isinstance(detail, str)
    assert "submitted" in detail
    assert "nothing was verified" in detail


# ---------------------------------------------------------------------------
# Route 2 — the last outcome failed
# ---------------------------------------------------------------------------


def test_a_failed_last_outcome_routes_to_the_statement_failure_shape():
    """``statement_failure_results``: it ran and failed (``:3144``).

    ``executed`` true is the load-bearing half — the transform reached Spark, so
    this is an observation about the transform rather than about the harness — and
    ``exit_code`` is the non-zero ``_FAILED_EXIT_CODE``.
    """
    outcomes = [_ok(STAGE_FRAME_BUILD, GENERATED_MARKER), _failed(STAGE_TRANSFORM)]

    result = results_from_outcomes(outcomes)

    assert result["executed"] is True
    assert result["execution_successful"] is False
    assert result["exit_code"] == 1
    assert result["conversion_report"] is None
    assert set(result) == BASE_CONTRACT_KEYS
    assert result == statement_failure_results(outcomes[-1])


def test_a_failed_last_outcome_names_its_own_stage_and_the_reported_exception():
    """``"{stage} failed: {ErrorName}: {ErrorValue}"`` — the failing stage is named.

    Which stage failed is what makes the next section's pin visible: the same
    layout applied to a frame-build failure produces a sentence about *test data
    generation*, and p44 R2.1 is the requirement that a sample read failing must
    not be reported that way.
    """
    result = results_from_outcomes([_ok(STAGE_FRAME_BUILD), _failed(STAGE_TRANSFORM)])

    assert result["error_details"].startswith(f"{STAGE_TRANSFORM} failed: ")
    assert ERROR_NAME in result["error_details"]
    assert ERROR_VALUE in result["error_details"]


def test_a_failed_frame_build_is_reported_as_a_failed_transform_today():
    """**Today's behaviour, and p44 R2.1/R2.2 are why it will change.**

    A frame-build statement that fails takes the same route as a failed
    transform: ``executed`` **true**, ``exit_code`` 1, and an ``error_details``
    opening with the stage label ``"test data generation failed: "``. For a
    synthetic frame that is honest. For a **sample** frame — the frame p44 wires
    a caller for — it reports a failed transform for a transform that never ran,
    which is exactly what R2.1 and R2.2 remove: a source that cannot be read must
    report ``executed`` false with a harness reason naming the source read.

    Pinned here as *today's* routing so that task 6.1's change reads as the
    deliberate reversal it is (``LGR-60c4985-6``). Task 6.1 makes that change in
    ``run_test``, which is the only place holding both the frame source and the
    outcomes, so **this assertion is expected to keep passing** — a synthetic
    run's routing is unchanged. Should it ever need amending, amend it and record
    the reason at this site; do not delete it.
    """
    outcomes = [_failed(STAGE_FRAME_BUILD)]

    result = results_from_outcomes(outcomes)

    assert result["executed"] is True
    assert result["exit_code"] == 1
    assert result["error_details"].startswith(f"{STAGE_FRAME_BUILD} failed: ")
    assert result == statement_failure_results(outcomes[-1])


# ---------------------------------------------------------------------------
# Route 3 — every stage reported ok
# ---------------------------------------------------------------------------


def test_every_stage_ok_routes_to_the_success_shape():
    """``success_results``: ``execution_successful`` true, ``exit_code`` zero (``:3074``)."""
    outcomes = _clean_run()

    result = results_from_outcomes(outcomes)

    assert result["execution_successful"] is True
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert result["error_details"] is None
    assert result["conversion_report"] is None
    assert set(result) == BASE_CONTRACT_KEYS
    assert result == success_results(outcomes)


def test_success_carries_the_counts_parsed_from_the_stages_stdout():
    """The whole outcome list reaches the parser, not only the last entry.

    The verdict comes from the last outcome; the *numbers* come from every
    outcome's stdout joined. Pinned because task 6.1 changes which builder is
    called on one route and must not change what a success reports.
    """
    result = results_from_outcomes(_clean_run())

    assert result["input_records"] == 100
    assert result["output_records"] == 100


# ---------------------------------------------------------------------------
# The rule underneath all three — only the last outcome decides
# ---------------------------------------------------------------------------


def test_only_the_last_outcome_decides_the_verdict():
    """The docstring's "reads only the last one", asserted behaviourally.

    A list whose **first** entry failed and whose **last** entry passed reports
    success. That is not a leniency: ``_run_statements`` breaks at the first
    failure, so such a list cannot be produced by a real run — it is constructed
    here only to isolate the rule. Scanning for *any* failure instead "would be a
    second, weaker statement of the same rule, free to disagree with it after an
    edit", which is the alternative the docstring rejects.
    """
    outcomes = [
        _failed(STAGE_FRAME_BUILD, GENERATED_MARKER),
        _ok(STAGE_TRANSFORM),
        _ok(STAGE_VALIDATION, VALIDATION_MARKER),
    ]

    result = results_from_outcomes(outcomes)

    assert result["execution_successful"] is True
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert result == success_results(outcomes)


def test_a_single_failed_outcome_is_both_first_and_last_so_it_decides():
    """The complement, so the rule is pinned in both directions."""
    outcomes = [_failed(STAGE_TRANSFORM)]

    result = results_from_outcomes(outcomes)

    assert result["execution_successful"] is False
    assert result["executed"] is True
    assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# The split task 6.1 depends on
# ---------------------------------------------------------------------------


def test_no_route_here_carries_a_harness_non_verification_marker():
    """Every route returns exactly the nine contract keys — no harness marker.

    True today, and it must stay true after task 6.1: the new
    ``source_read_failed`` route belongs to ``run_test``, which is the only place
    that knows whether the frame was a sample. A harness marker appearing here
    would mean it was added one level too deep, where ``not_executed_results``'
    own docstring warns it "would attach to all of them" — including every
    synthetic run, which verified something (p44 C9, R9.2).
    """
    for outcomes in ([], [_failed(STAGE_TRANSFORM)], _clean_run()):
        result = results_from_outcomes(outcomes)

        assert set(result) == BASE_CONTRACT_KEYS
        assert "not_verified_by_harness" not in result
        assert "not_verified_reason" not in result
