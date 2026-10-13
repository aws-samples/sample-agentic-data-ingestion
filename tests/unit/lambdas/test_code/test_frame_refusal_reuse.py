# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Glue runner reuses the pre-session frame refusal, before CreateSession.

Spec `real-spark-verification` task 5.3. Requirements 7.3 and 10.4; design §2's
``_frame_refusal`` paragraph and C5.

**No real Glue session is created anywhere in this file** — every call goes
through a MagicMock, and the point of most of these tests is that even the mock's
``create_session`` is never reached.

What is pinned here:

* **reuse by object identity, not by equivalence.** ``_frame_refusal`` and
  ``_frame_truncation_results`` are the pandas path's own function objects, bound
  by name in ``glue_session_runner``. The same technique
  ``test_glue_result_mapping.py`` uses for ``_bound_stderr`` and
  ``test_synthetic_frame_builder.py`` for ``declared_source_fields``, and for a
  stronger reason than tidiness: the refusal is a *judgement* about what a frame
  proves, and two executors disagreeing about which schemas are verifiable at
  all would corrupt shadow mode at the root. "An equivalent reimplementation"
  cannot satisfy an identity assertion.
* **the refusal precedes session creation (R7.3)** — the worked examples. Task
  5.4 generalises them over schema width and emptiness as a property.
* **the 512-column ceiling survives, imported rather than restated** (R10.4,
  design C5). Its derivation is the DynamoDB item-size limit on the state row
  (``code_interpreter_runner.py:105-127``), which real Spark does not change, and
  its own comment says that when ``null_counts``/``dtypes`` stop reaching that
  row the ceiling "can be removed, not raised".
* **a raised ``FrameBuildError`` also precedes creation.** It is the other
  host-side frame failure, and R7.3 constrains its *position* identically even
  though mapping it onto the contract is task 8.1's.

Observation for tasks 8.1 / 8.2, recorded rather than pre-empted
---------------------------------------------------------------
``FRAME_REFUSAL`` in ``tests/fixtures/run_test_result_shapes.py`` declares
``conversion_report`` as a ``dict``, because on the pandas path conversion ran
and succeeded before the refusal, so ``run_test:304`` overwrites the builder's
``None``. The Glue runner converts nothing, so what it returns here is ``None`` —
matching every other builder in ``glue_session_runner``. So the **key set** is
identical (asserted below) while the ``conversion_report`` *type* is the one key
Property 2 (task 8.2) will have to reconcile, and the module's result-mapping
section already records that value as task 8.1's decision rather than a
builder's. Naming it here so 8.2 finds it as a known difference rather than as a
surprise.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from tests.fixtures.run_test_result_shapes import FRAME_REFUSAL

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import code_interpreter_runner
import glue_session_runner
from code_interpreter_runner import (
    _MAX_TEST_FRAME_COLUMNS,
    NOT_VERIFIED_REASON_FRAME_TRUNCATED,
    NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA,
    CodeInterpreterTestRunner,
)
from glue_session_runner import (
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    FrameBuildError,
    FrameGuardedRun,
    GlueSessionTestRunner,
    frame_refusal_results,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

#: A schema the refusal admits: some declared fields, well under the ceiling.
BUILDABLE_SCHEMA = {
    "properties": {
        "customer_id": {"type": "integer"},
        "name": {"type": "string"},
        "amount": {"type": "double"},
    }
}


def _wide_schema(field_count: int) -> dict:
    return {"properties": {f"col_{index}": {"type": "string"} for index in range(field_count)}}


@pytest.fixture
def glue():
    """A mock Glue client wired for a clean happy path."""
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.return_value = {"Id": 1}
    client.get_statement.return_value = {
        "Statement": {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": ""}}}
    }
    return client


@pytest.fixture
def runner(glue):
    """A runner over the mock client, with sleeping patched out."""
    with patch("glue_session_runner.boto3.client", return_value=glue), patch("glue_session_runner.time.sleep"):
        yield GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)


# ---------------------------------------------------------------------------
# Reuse, by object identity
# ---------------------------------------------------------------------------


def test_frame_refusal_is_the_pandas_paths_function_itself():
    """R7.3: the refusal is REUSED, not reimplemented.

    Object identity rather than behavioural equality, because the requirement is
    that there is only one judgement about which schemas can be verified. A
    reimplementation that happened to agree today would satisfy any behavioural
    test and would still be free to drift tomorrow.
    """
    assert glue_session_runner._frame_refusal is CodeInterpreterTestRunner._frame_refusal


def test_frame_truncation_results_is_the_pandas_paths_function_itself():
    """The refusal's *report* is the same function too.

    Which is what makes a Spark-side refusal and a Code Interpreter refusal
    literally the same dict — including the ``not_verified_field_count`` /
    ``not_verified_frame_ceiling`` pair ``lambda_function`` renders as text.
    """
    assert glue_session_runner._frame_truncation_results is CodeInterpreterTestRunner._frame_truncation_results


def test_the_column_ceiling_is_imported_not_restated():
    """R10.4 / design C5: one ceiling, and it is the pandas path's.

    A second copy of 512 in this module would be a second thing to keep in step
    with a bound whose derivation lives entirely in the other file.
    """
    assert glue_session_runner._MAX_TEST_FRAME_COLUMNS is code_interpreter_runner._MAX_TEST_FRAME_COLUMNS
    assert _MAX_TEST_FRAME_COLUMNS == 512, (
        "design C5 — this is a DynamoDB item-size bound on the state row, not an "
        "interpreter bound, so real Spark changes neither side of the arithmetic. "
        "Its own comment says it can be removed, not raised."
    )


# ---------------------------------------------------------------------------
# What the refusal answers
# ---------------------------------------------------------------------------


def test_a_buildable_schema_is_not_refused():
    """``None`` means "the frame carries one column per declared field"."""
    assert frame_refusal_results(BUILDABLE_SCHEMA) is None


@pytest.mark.parametrize("source_schema", [None, {}, {"properties": {}}, {"fields": {}}])
def test_a_schema_declaring_nothing_is_refused(source_schema):
    """The ``{id, value}`` fallback frame proves nothing, so it is reported.

    Four ways of declaring nothing, all of which ``declared_source_fields``
    reduces to an empty map. ``None`` included: this runner's caller reads the
    schema off a payload, and an absent one must not become the two-column
    fallback silently.
    """
    results = frame_refusal_results(source_schema)

    assert results is not None
    assert results["not_verified_by_harness"] is True
    assert results["not_verified_reason"] == NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA
    assert results["not_verified_field_count"] == 0
    assert results["not_verified_frame_ceiling"] == _MAX_TEST_FRAME_COLUMNS


def test_a_schema_wider_than_the_ceiling_is_refused():
    """R10.4: past the ceiling the frame would silently drop columns.

    Which is the hazard, not the saving: Spark's ``withColumnRenamed`` is a no-op
    on an absent column, so the dropped columns would surface as a plausible
    **low** ``schema_compliance`` attributed to the transform.
    """
    field_count = _MAX_TEST_FRAME_COLUMNS + 1
    results = frame_refusal_results(_wide_schema(field_count))

    assert results is not None
    assert results["not_verified_reason"] == NOT_VERIFIED_REASON_FRAME_TRUNCATED
    assert results["not_verified_field_count"] == field_count
    assert results["not_verified_frame_ceiling"] == _MAX_TEST_FRAME_COLUMNS


def test_a_schema_exactly_at_the_ceiling_is_admitted():
    """The boundary: 512 is admitted, 513 is refused.

    Pinned because the ceiling is a ``>`` comparison in ``_frame_refusal`` and an
    off-by-one either way is invisible in any other test — one direction refuses
    a legitimate schema, the other admits a frame that drops a column.
    """
    assert frame_refusal_results(_wide_schema(_MAX_TEST_FRAME_COLUMNS)) is None
    assert frame_refusal_results(_wide_schema(_MAX_TEST_FRAME_COLUMNS + 1)) is not None


def test_the_refusal_reports_nothing_verified_and_nothing_scored():
    """The non-verification posture, and the empty ``data_quality_results``.

    The empty dict is load-bearing rather than incidental: it is what suppresses
    the ``schema_compliance`` score (design C13), so a frame the harness caused
    to be short cannot report a shortfall against the transform.
    """
    results = frame_refusal_results({})

    assert results is not None
    assert results["execution_successful"] is False
    assert results["executed"] is False
    assert results["exit_code"] is None
    assert results["data_quality_results"] == {}
    assert results["stderr"] == ""
    assert isinstance(results["error_details"], str) and results["error_details"]


def test_the_refusal_carries_the_frame_refusal_contract_key_set():
    """R1.1: the same keys the pandas path's frame refusal carries.

    The key set only. See this module's docstring for the one *type* that
    differs — ``conversion_report`` — and why settling it belongs to task 8.1.
    """
    results = frame_refusal_results({})

    assert results is not None
    assert set(results) == set(FRAME_REFUSAL.types)


def test_the_refusal_adds_no_new_vocabulary():
    """No new field and no new state — R8.4's rule, applied here too.

    The reasons are the two ``code_interpreter_runner`` already declares, so
    ``is_not_verified_check`` and the verified-check partition need no change.
    """
    reasons = {
        frame_refusal_results({})["not_verified_reason"],
        frame_refusal_results(_wide_schema(_MAX_TEST_FRAME_COLUMNS + 1))["not_verified_reason"],
    }
    assert reasons == {NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA, NOT_VERIFIED_REASON_FRAME_TRUNCATED}


# ---------------------------------------------------------------------------
# Position — the refusal precedes CreateSession (R7.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_schema",
    [None, {}, {"properties": {}}, _wide_schema(_MAX_TEST_FRAME_COLUMNS + 1)],
    ids=["absent", "empty", "empty-properties", "too-wide"],
)
def test_a_refused_schema_creates_no_session(runner, glue, source_schema):
    """R7.3, the requirement itself: no session is spent on a refused schema.

    Task 5.4 generalises this over generated widths and emptiness. These are the
    four shapes that reach the two reasons.
    """
    run = runner.run_guarded_session(source_schema, [(STAGE_TRANSFORM, "df_t = df")])

    assert run.refused
    glue.create_session.assert_not_called()
    glue.run_statement.assert_not_called()
    assert run.outcomes == []


def test_a_refused_schema_returns_the_refusal_result(runner):
    """The caller gets a complete result, not a sentinel it has to interpret."""
    run = runner.run_guarded_session({}, [(STAGE_TRANSFORM, "df_t = df")])

    assert run.refusal == frame_refusal_results({})


def test_a_buildable_schema_reaches_the_session_with_the_frame_first(runner, glue):
    """The other half: when nothing is refused, the session runs.

    Asserted alongside the refusal cases so "no session was created" is a
    statement about the *schema* rather than about the mock never working.
    """
    run = runner.run_guarded_session(BUILDABLE_SCHEMA, [(STAGE_TRANSFORM, "df_t = df")])

    assert not run.refused
    assert run.refusal is None
    glue.create_session.assert_called_once()

    submitted = [call.kwargs["Code"] for call in glue.run_statement.call_args_list]
    assert len(submitted) == 2, "the frame build plus the one stage passed in"
    assert "spark.createDataFrame" in submitted[0]
    assert [outcome.stage for outcome in run.outcomes] == [STAGE_FRAME_BUILD, STAGE_TRANSFORM]


def test_the_frame_build_statement_is_the_synthetic_builders_output(runner, glue):
    """The guard does not rebuild the frame differently from task 5.1.

    The submitted statement is the synthetic builder's output **followed by** task
    6.1's ``GENERATED:`` marker line (``frame_build_statement``), so the assertion
    is byte equality on the composition plus a prefix check on the frame itself —
    which is what pins that the seeded null positions R10.3 requires are the ones
    actually reaching Spark. A plain equality against the builder alone would now
    fail for the right reason and hide the wrong one.
    """
    runner.run_guarded_session(BUILDABLE_SCHEMA)

    submitted = glue.run_statement.call_args.kwargs["Code"]
    assert submitted == glue_session_runner.frame_build_statement(runner.resolve_frame_source(), BUILDABLE_SCHEMA)
    assert submitted.startswith(glue_session_runner.build_synthetic_frame_code(BUILDABLE_SCHEMA))
    assert "GENERATED:" in submitted


def test_an_unknown_declared_type_also_fails_before_any_session(runner, glue):
    """The other host-side frame failure, in the same position.

    ``FrameBuildError`` is raised rather than returned, and mapping it onto the
    contract is task 8.1's — but R7.3 constrains its position identically, and
    that part is settled here.
    """
    with pytest.raises(FrameBuildError, match="outside the known vocabulary"):
        runner.run_guarded_session({"properties": {"weird": {"type": "geography"}}})

    glue.create_session.assert_not_called()
    glue.run_statement.assert_not_called()


def test_guarded_run_outcomes_are_mutually_exclusive(runner):
    """The return type makes the two outcomes unreadable in the wrong order."""
    refused = runner.run_guarded_session({})
    ran = runner.run_guarded_session(BUILDABLE_SCHEMA)

    assert refused.refused and refused.outcomes == []
    assert not ran.refused and ran.refusal is None and ran.outcomes
    assert FrameGuardedRun().refusal is None, "the default is 'nothing refused'"
