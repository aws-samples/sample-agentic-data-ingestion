# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Glue → Test_Result_Contract mapping.

Spec `real-spark-verification` task 3.3, design §4.5's table. Pure functions over
observations — **no Glue client, mocked or otherwise, is constructed anywhere in
this file**, because nothing the mapping does needs one.

What is pinned here:

* the success row (R1.5) — ``execution_successful`` true, ``executed`` true,
  ``exit_code`` zero, with the records and data-quality values coming from the
  shared ``_parse_results``
* the error row (R5.1, R5.2, R5.3) — ran, failed, non-zero exit, the reported
  exception name and value in ``error_details``
* **the named trap (R5.5)** — a Glue traceback carries NO
  ``Traceback (most recent call last`` header, so classification is from
  ``Output.Status``; the header is synthesised into ``stderr`` afterwards.
  ``test_glue_traceback_carries_no_python_header`` is the test that fails if
  anyone reintroduces the ``_has_traceback`` sniff
* the cancelled / session-error row (R5.6) — the reached state is named
* the ``CreateSession`` row (R8.5) — nothing ran, the reported error carried
* R5.7 as an absolute across every builder
* ``_bound_stderr`` reused **verbatim by import** — asserted by object identity,
  which "an equivalent reimplementation" cannot satisfy

Task 3.4 generalises the mapping over state × output status × exception ×
traceback shape; task 3.5 generalises the stderr bound over lengths crossing it.
These are the worked examples those properties are built from.
"""

import os
import sys

import pytest
from botocore.exceptions import ClientError
from tests.fixtures.run_test_result_shapes import (
    BASE_CONTRACT_KEYS,
    CLIENT_UNAVAILABLE,
    STATEMENT_FAILURE,
    SUCCESS,
    OutcomeShape,
    shape_violations,
)

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from code_interpreter_runner import (
    _STDERR_HEAD_CHARS,
    _STDERR_TAIL_CHARS,
    _TRACEBACK_MARKER,
    CodeInterpreterTestRunner,
)
from glue_session_runner import (
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    STAGE_VALIDATION,
    GlueSessionError,
    StatementOutcome,
    exception_summary,
    lifecycle_failure_results,
    not_executed_results,
    session_creation_failure_results,
    statement_failure_results,
    success_results,
    synthesised_traceback,
)

# A Glue traceback as the API reports it: a list of frame strings with NO
# "Traceback (most recent call last" header line. Header-free BY CONSTRUCTION —
# that absence is the whole of R5.5's trap, so it is asserted, not assumed, in
# `test_glue_traceback_carries_no_python_header`.
GLUE_TRACEBACK = [
    '  File "<stdin>", line 1, in <module>\n',
    '  File "/opt/spark/python/pyspark/sql/dataframe.py", line 5107, in withColumn\n',
    '  File "/opt/spark/python/pyspark/errors/exceptions/captured.py", line 185, in deco\n',
]

ERROR_NAME = "AnalysisException"
ERROR_VALUE = "[UNRESOLVED_COLUMN] cannot resolve `nope` given input columns: [id, value]"

GENERATED_MARKER = 'GENERATED:{"record_count": 100}'
VALIDATION_MARKER = (
    'VALIDATION:{"record_count": 100, "completeness": 0.95, "uniqueness": 1.0, '
    '"schema_compliance": 1.0, "overall_quality": 0.98, "null_counts": {"value": 5}, '
    '"duplicates": 0, "missing_columns": []}'
)


def _ok(stage: str, stdout: str = "") -> StatementOutcome:
    return StatementOutcome(
        stage=stage,
        state="AVAILABLE",
        output_status="ok",
        stdout=stdout,
        statement={"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": stdout}}},
    )


def _error_statement(
    *,
    name: str | None = ERROR_NAME,
    value: str | None = ERROR_VALUE,
    traceback: object = None,
) -> dict:
    output: dict = {"Status": "error", "Data": {"TextPlain": ""}}
    if name is not None:
        output["ErrorName"] = name
    if value is not None:
        output["ErrorValue"] = value
    output["Traceback"] = GLUE_TRACEBACK if traceback is None else traceback
    return {"State": "AVAILABLE", "Output": output}


def _failed(stage: str = STAGE_TRANSFORM, **kwargs) -> StatementOutcome:
    statement = _error_statement(**kwargs)
    return StatementOutcome(
        stage=stage,
        state="AVAILABLE",
        output_status="error",
        stdout="",
        statement=statement,
    )


def _non_terminal_output(stage: str, state: str, stdout: str = "") -> StatementOutcome:
    """A statement that stopped without producing an ``Output`` (R5.6)."""
    return StatementOutcome(
        stage=stage,
        state=state,
        output_status=None,
        stdout=stdout,
        statement={"State": state},
    )


def _clean_run() -> list[StatementOutcome]:
    return [
        _ok(STAGE_FRAME_BUILD, GENERATED_MARKER),
        _ok(STAGE_TRANSFORM),
        _ok(STAGE_VALIDATION, VALIDATION_MARKER),
    ]


def _violations(result: dict, shape: OutcomeShape) -> list[str]:
    """Shape violations, excluding the one key a BUILDER cannot own.

    ``conversion_report``'s contract type is set by the CALL PATH, not by the
    builder: all five pandas builders return ``None`` and ``run_test``
    overwrites it (``code_interpreter_runner.py:304``, ``:316``). Every builder
    here returns ``None`` for the same reason, and what the Glue ``run_test``
    finally puts there is task 8.1's decision. The value is asserted separately
    in ``test_every_builder_defers_the_conversion_report``.
    """
    return [violation for violation in shape_violations(result, shape) if "conversion_report" not in violation]


# ---------------------------------------------------------------------------
# The success row — R1.5
# ---------------------------------------------------------------------------


def test_clean_run_reports_success_with_a_zero_exit_code():
    """R1.5: execution_successful true, executed true, exit_code zero."""
    result = success_results(_clean_run())

    assert result["execution_successful"] is True
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert result["error_details"] is None


def test_clean_run_carries_the_parsed_records_and_quality_values():
    """The shared parser supplies them, so they cannot drift between executors."""
    result = success_results(_clean_run())

    assert result["input_records"] == 100
    assert result["output_records"] == 100
    assert result["data_quality_results"]["completeness_score"] == 0.95
    assert result["data_quality_results"]["uniqueness_score"] == 1.0
    assert result["data_quality_results"]["schema_compliance"] == 1.0
    assert result["data_quality_results"]["null_counts"] == {"value": 5}


def test_clean_run_matches_the_declared_success_shape():
    assert _violations(success_results(_clean_run()), SUCCESS) == []


def test_success_stderr_is_empty_because_glue_reports_no_stderr_stream():
    """A Glue statement Output has no stderr field — so the key stays empty.

    Asserted rather than left implicit: an executor that invented text here
    would put harness prose where a reviewer expects the runtime's own output.
    """
    assert success_results(_clean_run())["stderr"] == ""


def test_success_results_cannot_manufacture_a_success_without_validation():
    """No VALIDATION payload ⇒ nothing was validated, whatever the states said.

    The safety property behind reusing ``_parse_results``: a misrouted call
    reports that nothing was validated rather than claiming a passing run.
    """
    result = success_results([_ok(STAGE_FRAME_BUILD, GENERATED_MARKER), _ok(STAGE_TRANSFORM)])

    assert result["execution_successful"] is False
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert result["output_records"] == 0


def test_success_results_reports_a_validation_error_without_claiming_success():
    """The validation-error variant of the success row: same keys, no success."""
    outcomes = [
        _ok(STAGE_FRAME_BUILD, GENERATED_MARKER),
        _ok(STAGE_TRANSFORM),
        _ok(STAGE_VALIDATION, 'VALIDATION:{"error": "df_transformed is not defined"}'),
    ]
    result = success_results(outcomes)

    assert result["execution_successful"] is False
    assert result["error_details"] == "df_transformed is not defined"
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# The error row — R5.1, R5.2, R5.3
# ---------------------------------------------------------------------------


def test_output_status_error_reports_executed_and_not_successful():
    """R5.1: it ran, and it failed."""
    result = statement_failure_results(_failed())

    assert result["executed"] is True
    assert result["execution_successful"] is False


def test_output_status_error_reports_a_non_zero_exit_code():
    """R5.2. Glue reports no exit code, so one is synthesised."""
    result = statement_failure_results(_failed())

    assert isinstance(result["exit_code"], int)
    assert result["exit_code"] != 0


def test_output_status_error_names_the_exception_and_its_value():
    """R5.3: error_details contains the reported exception name AND value."""
    result = statement_failure_results(_failed())

    assert ERROR_NAME in result["error_details"]
    assert ERROR_VALUE in result["error_details"]


def test_output_status_error_names_the_failing_stage():
    """Stage attribution is why the lifecycle runs three statements, not one."""
    result = statement_failure_results(_failed(stage=STAGE_VALIDATION))

    assert result["error_details"].startswith(f"{STAGE_VALIDATION} failed:")


def test_output_status_error_matches_the_declared_statement_failure_shape():
    assert _violations(statement_failure_results(_failed()), STATEMENT_FAILURE) == []


def test_error_without_name_or_value_names_the_observed_status():
    """Nothing renders an empty reason: the observed status is the fallback."""
    result = statement_failure_results(_failed(name=None, value=None, traceback=[]))

    assert "error" in result["error_details"]
    assert result["error_details"].startswith(f"{STAGE_TRANSFORM} failed:")
    assert result["execution_successful"] is False


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        (ERROR_NAME, ERROR_VALUE, f"{ERROR_NAME}: {ERROR_VALUE}"),
        (ERROR_NAME, None, ERROR_NAME),
        (None, ERROR_VALUE, ERROR_VALUE),
        (None, None, ""),
    ],
)
def test_exception_summary_reports_whichever_fields_glue_sent(name, value, expected):
    assert exception_summary(_error_statement(name=name, value=value)) == expected


# ---------------------------------------------------------------------------
# The named trap — R5.5
# ---------------------------------------------------------------------------


def test_glue_traceback_carries_no_python_header():
    """The premise of R5.5, asserted rather than assumed.

    If this ever fails, the fixture stopped being representative and the two
    tests below stopped testing the trap.
    """
    for frame in GLUE_TRACEBACK:
        assert _TRACEBACK_MARKER not in frame


def test_a_spark_exception_is_a_failure_although_its_traceback_has_no_header():
    """R5.5 — THE TRAP.

    Reusing ``_has_traceback`` (``code_interpreter_runner.py:464-467``) would
    search Glue's header-free frames for the Python header, find nothing, and
    pass this Spark exception as a success. Classification is from
    ``Output.Status`` instead, so it does not.
    """
    outcome = _failed()
    raw_glue_traceback = "".join(GLUE_TRACEBACK)

    # The sniff the mapping must NOT use, run here only to show it would lie.
    assert CodeInterpreterTestRunner._has_traceback(raw_glue_traceback) is False

    # What the mapping does use.
    assert outcome.failed is True
    assert statement_failure_results(outcome)["execution_successful"] is False


def test_the_synthesised_stderr_reads_like_a_python_traceback():
    """R5.5's other half: the header is put back so reports read the same.

    Header first, frames in the middle, ``ErrorName: ErrorValue`` last — Python's
    own order, which is what makes ``_bound_stderr``'s tail retain the exception.
    """
    stderr = statement_failure_results(_failed())["stderr"]

    assert stderr.startswith(_TRACEBACK_MARKER)
    assert "withColumn" in stderr
    assert stderr.endswith(f"{ERROR_NAME}: {ERROR_VALUE}")


@pytest.mark.parametrize("traceback", [[], [""], ["", "\n"]])
def test_synthesised_traceback_is_empty_when_glue_reported_nothing(traceback):
    """A lone header describing nothing is worse than no stderr at all.

    ``[""]`` means the same as ``[]`` here: blank frames are dropped rather than
    rendered, so an empty traceback cannot produce a header over whitespace.
    """
    assert synthesised_traceback(_error_statement(name=None, value=None, traceback=traceback)) == ""


def test_synthesised_traceback_tolerates_a_bare_string_traceback():
    """Not a documented Glue shape — but the one that would otherwise be
    iterated character by character."""
    stderr = synthesised_traceback(_error_statement(traceback='  File "<stdin>", line 1\n'))

    assert stderr.startswith(_TRACEBACK_MARKER)
    assert '  File "<stdin>", line 1' in stderr
    assert stderr.count("File") == 1


# ---------------------------------------------------------------------------
# stderr is bounded — R5.4 (the property is task 3.5)
# ---------------------------------------------------------------------------


def test_bound_stderr_is_the_pandas_path_function_itself():
    """ "Reuse verbatim by import", stated as object identity.

    An equivalent reimplementation passes a behavioural test and fails this one,
    which is the point: R5.4 says the SAME bound, and the only way that cannot
    drift is for there to be one function.
    """
    assert glue_session_runner._bound_stderr is CodeInterpreterTestRunner._bound_stderr


def test_a_long_traceback_is_bounded_but_keeps_its_head_and_tail():
    """R5.4: both the first and the last characters of the report survive."""
    frames = [f'  File "<stdin>", line {index}, in frame_{index}' for index in range(400)]
    result = statement_failure_results(_failed(traceback=frames))
    stderr = result["stderr"]

    assert len(stderr) < len("\n".join(frames))
    assert len(stderr) <= _STDERR_HEAD_CHARS + _STDERR_TAIL_CHARS + len("\n... [999999 chars truncated] ...\n")
    assert stderr.startswith(_TRACEBACK_MARKER)
    assert stderr.endswith(f"{ERROR_NAME}: {ERROR_VALUE}")


# ---------------------------------------------------------------------------
# The cancelled / session-error row — R5.6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["CANCELLED", "ERROR"])
def test_a_statement_that_stopped_names_the_reached_state(state):
    """R5.6: name the reached state in error_details."""
    result = statement_failure_results(_non_terminal_output(STAGE_TRANSFORM, state))

    assert state in result["error_details"]
    assert result["execution_successful"] is False
    assert result["executed"] is True
    assert result["exit_code"] != 0


@pytest.mark.parametrize("state", ["CANCELLED", "ERROR"])
def test_a_statement_that_stopped_carries_its_state_and_last_output(state):
    """design §4.5: "state name + last output"."""
    result = statement_failure_results(_non_terminal_output(STAGE_TRANSFORM, state, "partial output"))

    assert state in result["stderr"]
    assert "partial output" in result["stderr"]


@pytest.mark.parametrize("state", ["CANCELLED", "ERROR"])
def test_a_statement_that_stopped_matches_the_statement_failure_shape(state):
    outcome = _non_terminal_output(STAGE_TRANSFORM, state)

    assert _violations(statement_failure_results(outcome), STATEMENT_FAILURE) == []


def test_a_session_that_never_became_ready_names_the_state_it_reached():
    """R5.6's session branch — the lifecycle's own message carries the state."""
    exc = GlueSessionError("Glue session adi-verify-abc reached FAILED before READY")
    result = lifecycle_failure_results(exc)

    assert "FAILED" in result["error_details"]
    assert result["execution_successful"] is False
    assert result["executed"] is False
    assert result["exit_code"] is None


def test_a_lifecycle_failure_matches_the_nothing_ran_shape():
    exc = GlueSessionError("Glue statement 1 for stage 'transform execution' did not reach a terminal state")

    assert _violations(lifecycle_failure_results(exc), CLIENT_UNAVAILABLE) == []


# ---------------------------------------------------------------------------
# The CreateSession row — R8.5
# ---------------------------------------------------------------------------


def test_session_creation_failure_reports_nothing_executed():
    """R8.5: execution_successful false AND executed false."""
    exc = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "CreateSession")
    result = session_creation_failure_results(exc)

    assert result["execution_successful"] is False
    assert result["executed"] is False
    assert result["exit_code"] is None


def test_session_creation_failure_carries_the_reported_error():
    """R8.5: the reported error, not a paraphrase of it."""
    exc = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "CreateSession")
    result = session_creation_failure_results(exc)

    assert "AccessDeniedException" in result["error_details"]
    assert "not authorized" in result["error_details"]


def test_session_creation_failure_matches_the_nothing_ran_shape():
    exc = ClientError({"Error": {"Code": "InvalidInputException", "Message": "bad role"}}, "CreateSession")

    assert _violations(session_creation_failure_results(exc), CLIENT_UNAVAILABLE) == []


# ---------------------------------------------------------------------------
# Cross-builder invariants
# ---------------------------------------------------------------------------


def _every_result() -> dict[str, dict]:
    """One result from each builder in this module, keyed by what produced it."""
    return {
        "success": success_results(_clean_run()),
        "success_without_validation": success_results([_ok(STAGE_TRANSFORM)]),
        "statement_error": statement_failure_results(_failed()),
        "statement_cancelled": statement_failure_results(_non_terminal_output(STAGE_TRANSFORM, "CANCELLED")),
        "statement_state_error": statement_failure_results(_non_terminal_output(STAGE_TRANSFORM, "ERROR")),
        "not_executed": not_executed_results("glue client not available"),
        "session_creation": session_creation_failure_results(RuntimeError("boom")),
        "lifecycle": lifecycle_failure_results(GlueSessionError("did not reach READY")),
    }


@pytest.mark.parametrize("name", sorted(_every_result()))
def test_every_builder_returns_exactly_the_nine_contract_keys(name):
    """R1.1. No key missing, and no key added — including no non-verification
    marker, which R3.2 forbids this runner from ever claiming."""
    assert set(_every_result()[name]) == BASE_CONTRACT_KEYS


@pytest.mark.parametrize("name", sorted(_every_result()))
def test_no_result_is_successful_with_a_non_zero_exit_code(name):
    """R5.7, an absolute. The property over generated inputs is task 3.4."""
    result = _every_result()[name]

    if result["execution_successful"]:
        assert result["exit_code"] == 0


@pytest.mark.parametrize("name", sorted(_every_result()))
def test_every_builder_defers_the_conversion_report(name):
    """``conversion_report``'s type is set by the call path, not the builder.

    All five pandas builders return ``None`` and ``run_test`` overwrites it
    (``code_interpreter_runner.py:304``, ``:316``). These match, so the Glue
    ``run_test`` (task 8.1) owns the only real decision about the key — this
    module must not pre-empt it.
    """
    assert _every_result()[name]["conversion_report"] is None


@pytest.mark.parametrize("name", sorted(_every_result()))
def test_no_result_claims_a_proxy_non_verification(name):
    """R3.2. There is no proxy here; that absence is the feature."""
    assert "not_verified_by_proxy" not in _every_result()[name]
