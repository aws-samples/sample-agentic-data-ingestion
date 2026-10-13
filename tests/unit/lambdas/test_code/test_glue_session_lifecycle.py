# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for GlueSessionTestRunner's session lifecycle (mocked boto3).

Spec `real-spark-verification` task 3.1. **No real Glue session is created
anywhere in this file** — every call goes through a MagicMock.

What is pinned here:

* the session shape (R6.4, R7.4) — the verification role ARN, ``IdleTimeout: 1``,
  ``G.1X``, 2 workers
* release on **every** exit path, including one taken because an exception was
  raised (R7.1, R7.2), with the stop and the delete attempted independently
* the reuse seam (R7.5) — an externally supplied session id is neither created
  nor released
* failure classified from ``Output.Status`` rather than from traceback text
  (R5.5's trap, covered here with a header-free traceback by construction)

Task 3.2 generalises the release assertions into a property test over the
lifecycle step at which a failure is injected. These are the worked examples.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from glue_session_runner import (
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    STAGE_VALIDATION,
    GlueSessionError,
    GlueSessionTestRunner,
    StatementOutcome,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

STATEMENTS = [
    (STAGE_FRAME_BUILD, "df = spark.createDataFrame([], schema)"),
    (STAGE_TRANSFORM, "df_transformed = df"),
    (STAGE_VALIDATION, "print('VALIDATION:{}')"),
]

# A Glue traceback: a list of frame strings with NO
# "Traceback (most recent call last" header. R5.5 / task 3.3's trap — a runner
# that sniffed for the header would read this as a success.
GLUE_TRACEBACK = [
    '  File "<stdin>", line 1, in <module>\n',
    '  File "/opt/spark/python/pyspark/sql/dataframe.py", line 1, in withColumn\n',
]


def _client_error(code: str, operation: str = "DeleteSession") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _ok_statement(text: str = "") -> dict:
    return {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": text}}}


def _error_statement() -> dict:
    return {
        "State": "AVAILABLE",
        "Output": {
            "Status": "error",
            "ErrorName": "AnalysisException",
            "ErrorValue": "cannot resolve 'nope'",
            "Traceback": GLUE_TRACEBACK,
            "Data": {"TextPlain": ""},
        },
    }


@pytest.fixture
def glue():
    """A mock Glue client wired for a clean happy path."""
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.return_value = {"Id": 1}
    client.get_statement.return_value = {"Statement": _ok_statement()}
    return client


@pytest.fixture
def runner(glue):
    """A runner over the mock client, with sleeping patched out."""
    with patch("glue_session_runner.boto3.client", return_value=glue), patch("glue_session_runner.time.sleep"):
        yield GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)


@pytest.fixture
def no_sleep():
    with patch("glue_session_runner.time.sleep") as sleep:
        yield sleep


def _clock(step: float = 1.0):
    """Patch ``time.monotonic`` with a clock that advances *step* per reading.

    A finite ``side_effect`` list cannot work here: the release path reads the
    clock too, and how many times depends on how the run failed. A stepping
    clock is deterministic and unbounded, so a test can shorten one deadline
    constant without starving another loop's budget.
    """
    counter = iter(range(0, 10_000_000))
    return patch("glue_session_runner.time.monotonic", side_effect=lambda: next(counter) * step)


# ---------------------------------------------------------------------------
# Session shape — R6.4, R7.4
# ---------------------------------------------------------------------------


def test_create_session_passes_the_verification_role(runner, glue, no_sleep):
    """R6.4: the session's runtime role is the one the caller named."""
    runner.run_session(STATEMENTS)

    kwargs = glue.create_session.call_args.kwargs
    assert kwargs["Role"] == ROLE_ARN


def test_create_session_uses_the_measured_session_shape(runner, glue, no_sleep):
    """R7.4 plus design C10: idle timeout 1 minute, G.1X, 2 workers."""
    runner.run_session(STATEMENTS)

    kwargs = glue.create_session.call_args.kwargs
    assert kwargs["IdleTimeout"] == 1, "design §4.3 — 1 minute bounds a leaked session to ~$0.015"
    assert kwargs["WorkerType"] == "G.1X"
    assert kwargs["NumberOfWorkers"] == 2
    assert kwargs["GlueVersion"] == "5.0"
    assert kwargs["Command"] == {"Name": "glueetl", "PythonVersion": "3"}


def test_session_ids_are_unique_across_runners(glue, no_sleep):
    """Two verifications can start in the same second; ids must not collide."""
    with patch("glue_session_runner.boto3.client", return_value=glue):
        for _ in range(2):
            GlueSessionTestRunner(REGION, role_arn=ROLE_ARN).run_session([])

    ids = [call.kwargs["Id"] for call in glue.create_session.call_args_list]
    assert len(set(ids)) == 2, f"session ids collided: {ids}"


def test_client_unavailable_creates_no_session(glue, no_sleep):
    """An unavailable client refuses before spending a session."""
    with patch("glue_session_runner.boto3.client", side_effect=RuntimeError("no creds")):
        runner = GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)

    assert runner.client is None
    with pytest.raises(GlueSessionError, match="not available"):
        runner.run_session(STATEMENTS)
    glue.create_session.assert_not_called()


# ---------------------------------------------------------------------------
# Release on every exit path — R7.1, R7.2
# ---------------------------------------------------------------------------


def test_success_path_stops_and_deletes(runner, glue, no_sleep):
    """R7.1 / R7.2 on the ordinary path."""
    runner.run_session(STATEMENTS)

    glue.stop_session.assert_called_once()
    glue.delete_session.assert_called_once()
    assert runner.session_id is None


def test_statement_exception_still_stops_and_deletes(runner, glue, no_sleep):
    """R7.1 / R7.2 on an exit path taken because an exception was raised."""
    glue.run_statement.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        runner.run_session(STATEMENTS)

    glue.stop_session.assert_called_once()
    glue.delete_session.assert_called_once()


def test_session_never_reaching_ready_still_stops_and_deletes(runner, glue, no_sleep):
    """A session that failed to provision was still created, so it is released."""
    glue.get_session.return_value = {"Session": {"Status": "FAILED"}}

    with pytest.raises(GlueSessionError, match="FAILED"):
        runner.run_session(STATEMENTS)

    glue.stop_session.assert_called_once()
    glue.delete_session.assert_called_once()


def test_delete_is_attempted_even_when_stop_fails(runner, glue, no_sleep):
    """R7.1 and R7.2 are two requirements — a failing stop cannot skip the delete."""
    glue.stop_session.side_effect = _client_error("IllegalSessionStateException", "StopSession")

    runner.run_session(STATEMENTS)

    glue.delete_session.assert_called_once()


def test_release_never_raises_over_the_real_failure(runner, glue, no_sleep):
    """A teardown failure must not replace the failure being unwound."""
    glue.run_statement.side_effect = RuntimeError("the real failure")
    glue.stop_session.side_effect = RuntimeError("stop blew up")
    glue.delete_session.side_effect = RuntimeError("delete blew up")

    with pytest.raises(RuntimeError, match="the real failure"):
        runner.run_session(STATEMENTS)


def test_refused_creation_deletes_nothing(runner, glue, no_sleep):
    """Nothing reached creation, so there is nothing to release."""
    glue.create_session.side_effect = _client_error("ResourceNumberLimitExceededException", "CreateSession")

    with pytest.raises(ClientError):
        runner.run_session(STATEMENTS)

    glue.stop_session.assert_not_called()
    glue.delete_session.assert_not_called()


def test_delete_retries_while_provisioning_then_succeeds(runner, glue, no_sleep):
    """Glue refuses DeleteSession while PROVISIONING, so the delete polls."""
    glue.delete_session.side_effect = [
        _client_error("IllegalSessionStateException"),
        _client_error("IllegalSessionStateException"),
        None,
    ]

    runner.run_session(STATEMENTS)

    assert glue.delete_session.call_count == 3


def test_delete_stops_retrying_on_an_already_gone_session(runner, glue, no_sleep):
    """Released is released — EntityNotFound ends the loop rather than retrying it."""
    glue.delete_session.side_effect = _client_error("EntityNotFoundException")

    runner.run_session(STATEMENTS)

    assert glue.delete_session.call_count == 1


def test_delete_does_not_retry_an_unrelated_error(runner, glue, no_sleep):
    """Only a not-yet-deletable state is worth retrying."""
    glue.delete_session.side_effect = _client_error("AccessDeniedException")

    runner.run_session(STATEMENTS)

    assert glue.delete_session.call_count == 1


# ---------------------------------------------------------------------------
# The reuse seam — R7.5
# ---------------------------------------------------------------------------


def test_external_session_is_neither_created_nor_released(glue, no_sleep):
    """R7.5: when a session id is supplied, the caller owns the lifecycle."""
    with patch("glue_session_runner.boto3.client", return_value=glue):
        runner = GlueSessionTestRunner(REGION, role_arn=ROLE_ARN, session_id="caller-owned")

    assert runner.owns_session is False
    runner.run_session(STATEMENTS)

    glue.create_session.assert_not_called()
    glue.stop_session.assert_not_called()
    glue.delete_session.assert_not_called()


def test_external_session_still_runs_the_statements(glue, no_sleep):
    """The seam skips the lifecycle, not the work."""
    with patch("glue_session_runner.boto3.client", return_value=glue):
        runner = GlueSessionTestRunner(REGION, role_arn=ROLE_ARN, session_id="caller-owned")

    outcomes = runner.run_session(STATEMENTS)

    assert len(outcomes) == 3
    assert all(call.kwargs["SessionId"] == "caller-owned" for call in glue.run_statement.call_args_list)


def test_a_runner_with_no_session_id_owns_its_session(runner):
    assert runner.owns_session is True


# ---------------------------------------------------------------------------
# Statement sequencing and stage attribution
# ---------------------------------------------------------------------------


def test_three_statements_run_as_three_stages(runner, glue, no_sleep):
    """Design §4.1: three statements, so a failure is attributable to a stage."""
    outcomes = runner.run_session(STATEMENTS)

    assert [o.stage for o in outcomes] == [STAGE_FRAME_BUILD, STAGE_TRANSFORM, STAGE_VALIDATION]
    assert glue.run_statement.call_count == 3


def test_execution_stops_at_the_first_failing_stage(runner, glue, no_sleep):
    """A failed frame build must not be followed by a transform."""
    glue.get_statement.return_value = {"Statement": _error_statement()}

    outcomes = runner.run_session(STATEMENTS)

    assert len(outcomes) == 1
    assert outcomes[0].stage == STAGE_FRAME_BUILD
    assert glue.run_statement.call_count == 1


def test_the_failing_stage_is_the_last_outcome(runner, glue, no_sleep):
    """A mid-lifecycle failure names the stage that failed."""
    glue.get_statement.side_effect = [
        {"Statement": _ok_statement("GENERATED:{}")},
        {"Statement": _error_statement()},
    ]

    outcomes = runner.run_session(STATEMENTS)

    assert [o.stage for o in outcomes] == [STAGE_FRAME_BUILD, STAGE_TRANSFORM]
    assert outcomes[-1].failed is True


def test_statement_polls_until_terminal(runner, glue, no_sleep):
    """WAITING and RUNNING are transitional, not terminal."""
    glue.get_statement.side_effect = [
        {"Statement": {"State": "WAITING"}},
        {"Statement": {"State": "RUNNING"}},
        {"Statement": _ok_statement("done")},
    ] + [{"Statement": _ok_statement()}] * 4

    outcomes = runner.run_session(STATEMENTS)

    assert outcomes[0].stdout == "done"
    assert no_sleep.call_count >= 2


def test_stdout_is_read_from_output_data_textplain(runner, glue, no_sleep):
    glue.get_statement.return_value = {"Statement": _ok_statement('GENERATED:{"record_count": 100}')}

    outcomes = runner.run_session(STATEMENTS)

    assert outcomes[0].stdout == 'GENERATED:{"record_count": 100}'


def test_outcome_carries_the_raw_statement_for_the_mapping(runner, glue, no_sleep):
    """Task 3.3 extracts ErrorName/ErrorValue/Traceback from here."""
    glue.get_statement.return_value = {"Statement": _error_statement()}

    outcome = runner.run_session(STATEMENTS)[0]

    assert outcome.statement["Output"]["ErrorName"] == "AnalysisException"
    assert outcome.statement["Output"]["Traceback"] == GLUE_TRACEBACK


# ---------------------------------------------------------------------------
# Failure classification — R5.5 and its trap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "output_status", "expected_failed"),
    [
        ("AVAILABLE", "ok", False),
        ("AVAILABLE", "error", True),
        ("CANCELLED", None, True),
        ("ERROR", None, True),
    ],
)
def test_failed_is_classified_from_state_and_output_status(state, output_status, expected_failed):
    """R5.5: classification comes from the reported status, not from text."""
    outcome = StatementOutcome(
        stage=STAGE_TRANSFORM,
        state=state,
        output_status=output_status,
        stdout="",
        statement={},
    )

    assert outcome.failed is expected_failed


def test_a_header_free_traceback_still_counts_as_a_failure(runner, glue, no_sleep):
    """The named trap: Glue's Traceback has no Python header line.

    A runner that classified by searching for "Traceback (most recent call
    last" would pass this Spark exception as a success.
    """
    statement = _error_statement()
    assert not any("Traceback (most recent call last" in frame for frame in statement["Output"]["Traceback"]), (
        "the fixture must stay header-free or this test stops covering the trap"
    )
    glue.get_statement.return_value = {"Statement": statement}

    outcome = runner.run_session(STATEMENTS)[0]

    assert outcome.failed is True


def test_a_cancelled_statement_fails_without_an_output(runner, glue, no_sleep):
    """R5.6: a cancelled statement carries no Output.Status to read."""
    glue.get_statement.return_value = {"Statement": {"State": "CANCELLED"}}

    outcome = runner.run_session(STATEMENTS)[0]

    assert outcome.failed is True
    assert outcome.output_status is None
    assert outcome.state == "CANCELLED"


# ---------------------------------------------------------------------------
# Deadlines — the bound that stops a hang, given C3
# ---------------------------------------------------------------------------


def test_a_statement_that_never_terminates_hits_its_deadline(runner, glue, no_sleep):
    """Design C3: a stalled waitForTaskToken step hangs for the full 1200 s,
    so the runner has to bound itself."""
    glue.get_statement.return_value = {"Statement": {"State": "RUNNING"}}

    with (
        _clock(step=1.0),
        patch("glue_session_runner._STATEMENT_DEADLINE_SECONDS", 5),
        pytest.raises(GlueSessionError, match="did not reach a terminal state"),
    ):
        runner.run_session(STATEMENTS)

    # It polled rather than giving up on the first reading...
    assert glue.get_statement.call_count >= 2
    # ...and the session is still released.
    glue.stop_session.assert_called_once()
    glue.delete_session.assert_called_once()


def test_a_session_that_never_becomes_ready_hits_its_deadline(runner, glue, no_sleep):
    glue.get_session.return_value = {"Session": {"Status": "PROVISIONING"}}

    with (
        _clock(step=1.0),
        patch("glue_session_runner._SESSION_READY_DEADLINE_SECONDS", 5),
        pytest.raises(GlueSessionError, match="did not reach READY"),
    ):
        runner.run_session(STATEMENTS)

    assert glue.get_session.call_count >= 2
    glue.delete_session.assert_called_once()


def test_provisioning_is_not_treated_as_a_dead_end(runner, glue, no_sleep):
    """PROVISIONING is the state the ready poll exists for."""
    glue.get_session.side_effect = [
        {"Session": {"Status": "PROVISIONING"}},
        {"Session": {"Status": "PROVISIONING"}},
        {"Session": {"Status": "READY"}},
    ]

    outcomes = runner.run_session(STATEMENTS)

    assert len(outcomes) == 3


def test_an_already_ready_session_does_not_sleep(runner, glue, no_sleep):
    """The ready poll returns on its first reading when there is nothing to wait for."""
    runner.run_session([])

    no_sleep.assert_not_called()
