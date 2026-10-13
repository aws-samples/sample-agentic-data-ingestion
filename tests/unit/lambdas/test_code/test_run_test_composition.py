# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``GlueSessionTestRunner.run_test``: the composition and its pre-flight order.

Spec ``real-spark-verification`` task 8.1.
**Validates: Requirements 1.1, 1.2, 1.4, 3.2.**

Everything ``run_test`` needs existed before this task; what it adds is the
*order* the pieces run in and the *routing* of each outcome onto the
Test_Result_Contract. Both are only checkable at the public method, which is what
this file exercises. Worked examples, deliberately — the generalisations live
elsewhere and are named at the bottom, so nothing here is a second, weaker copy
of a property.

**No Glue session is created anywhere in this file.** Every call goes through a
``MagicMock`` and ``time.sleep`` is patched out. Nothing is deployed.

The pre-flight order, and why each position is asserted
------------------------------------------------------

Copied from ``CodeInterpreterTestRunner.run_test`` (``code_interpreter_runner.py:268-305``):

1. **client unavailable** → a failure naming the client, before anything else.
   Asserted because it is the one step that must precede the frame refusal: a
   runner with no client cannot be asked whether a schema is buildable.
2. **no conversion step.** Asserted as an *absence*, twice over: the module
   imports no converter, and no result carries the proxy marker.
3. **frame refusal** before ``CreateSession``.
4. **capacity** at ``CreateSession``, routed to a harness non-verification.

R1.4 — "execute the Transform_Block without translating it" — is the claim that
distinguishes this executor from the other one, so it is asserted on the
submitted statement text rather than inferred from the absence of a converter:
the transform reaches Spark **byte for byte**, including a construct the pandas
proxy refuses by name.

*One correction to that claim, and the file that carries it.* "Byte for byte"
holds for every transform in this file because every one of them is written at
**column zero**, and on that input ``transform_statement``'s dedent is an
identity. It is not the general claim: the live block arrives at the four spaces
the generated job gives it, and a Glue statement is a fresh top-level scope, so
submitting it unchanged is the ``IndentationError`` that failed every Glue
verification on run ``run-2026-08-18-17-48-19-bd6f4deb``. The general claim is
"no statement is altered", asserted line-for-line on realistically indented input
in ``test_transform_statement_indentation.py``. Column-zero fixtures here are
what let that defect ship, so do not read a passing file as coverage of the
indented case.

Ledger notes
------------

* ``LGR-4dd9b42-1`` (**template / ratified**) is this spec's authorisation, and
  its revisit condition is what step 2's absence delivers: the refused shapes can
  now "be compared against real Spark and converted on evidence instead of
  refused on caution". ``initcap`` is used as the worked transform below because
  design C10 records it *executing successfully* on the measured probe session
  while being a ``_UNSUPPORTED_FUNCTIONS`` member (``pyspark_to_pandas.py:573-582``).
* ``why`` on ``glue_session_runner.py`` returns **nothing** — the file is not yet
  committed, so there is no record governing it. Stated as a gap rather than as
  an all-clear. ``decisions(scope=".../agentic_data_ingestion_test_code/**",
  include_backfilled=True)`` returns ten records; the two that bear on this file
  are ``LGR-b2f79ef-9`` (the ``{id, value}`` fallback stays "but never scored")
  and ``LGR-819980f-1`` (**invariant**, no JDBC credentials), neither of which
  this task's composition touches.

Sibling coverage — not duplicated here
--------------------------------------

* ``test_frame_refusal_precedes_session_prop.py`` — R7.3 generalised over schema
  width and emptiness, including ``run_test``'s mapping of both host-side routes
  (task 5.4 plus 8.1's half).
* ``test_glue_session_lifecycle.py`` / ``test_glue_session_release_prop.py`` —
  the session shape and the release-on-every-path property (task 3.1/3.2).
* ``test_glue_result_mapping.py`` / ``test_glue_result_mapping_prop.py`` — every
  builder's own shape (task 3.3/3.4/3.5).
* ``test_session_capacity_non_verification.py`` — the capacity routing seam
  (task 3.6).
* ``tests/contracts/api_responses/test_run_test_shape.py`` — the pandas side of
  the contract (task 1.2). Task 8.2 is what holds the two executors to it.
* ``test_run_test_signature_stability.py`` — R9.6, the arity contract, over
  **both** executors (task 9.4). This file carried a one-executor signature
  assertion of its own until 2026-08-18; see that file's "Sibling coverage"
  note for why it was removed rather than kept. Nothing here asserts a
  signature now, and that is deliberate: 9.4 owns it.
* ``test_transform_statement_indentation.py`` — the transform statement on
  realistically indented input, built from committed golden renders through the
  real extractor. Owns R1.4 for every block that is not already at column zero.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from tests.fixtures.run_test_result_shapes import BASE_CONTRACT_KEYS

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from glue_session_runner import (
    NOT_VERIFIED_REASON_SESSION_CAPACITY,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    STAGE_VALIDATION,
    GlueSessionTestRunner,
    frame_build_statement,
    validation_statement,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

#: A buildable schema: one column, a type the frame builder knows.
SCHEMA = {"properties": {"name": {"type": "string"}}}
TARGET_SCHEMA = {"properties": {"name": {"type": "string"}}}

#: The worked transform, and it is chosen rather than arbitrary: ``initcap`` is a
#: member of the pandas proxy's ``_UNSUPPORTED_FUNCTIONS`` — the construct design
#: C10 measured EXECUTING SUCCESSFULLY on the probe session. Under this executor
#: it needs no rule, which is R3.1 in one line.
TRANSFORM = "df_transformed = df.withColumn('name', initcap(col('name')))"

#: What the frame-build stage prints, in the form ``_parse_results`` reads.
GENERATED_STDOUT = 'GENERATED:{"record_count": 100, "columns": ["name"]}'
#: What the validation stage prints. Only the keys this file asserts on are
#: given real values; the payload's full nine keys are task 6.x's subject.
VALIDATION_STDOUT = 'VALIDATION:{"record_count": 100, "schema_compliance": 100.0}'


def _ok(text: str = "") -> dict:
    return {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": text}}}


def _capacity_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNumberLimitExceededException", "Message": "limit reached"}},
        "CreateSession",
    )


@pytest.fixture
def glue():
    """A Glue client wired so a full three-stage run succeeds.

    The frame-build and validation stages return their markers, so
    ``_parse_results`` has something real to read and a success is a parsed
    success rather than an asserted one.
    """
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.side_effect = [{"Id": 1}, {"Id": 2}, {"Id": 3}]
    client.get_statement.side_effect = [
        {"Statement": _ok(GENERATED_STDOUT)},
        {"Statement": _ok()},
        {"Statement": _ok(VALIDATION_STDOUT)},
    ]
    return client


@pytest.fixture
def runner(glue):
    with patch("glue_session_runner.boto3.client", return_value=glue), patch("glue_session_runner.time.sleep"):
        yield GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)


def _submitted(glue: MagicMock) -> list[str]:
    return [call.kwargs["Code"] for call in glue.run_statement.call_args_list]


# ---------------------------------------------------------------------------
# The three statements, in order — R1.4
# ---------------------------------------------------------------------------


def test_the_transform_reaches_spark_byte_for_byte(runner, glue):
    """R1.4: the Transform_Block is executed without being translated.

    **Validates: Requirement 1.4**

    Equality against the source string, not a containment check: a containment
    check would pass on a runner that wrapped or appended to the block, and
    "without translating it" is a claim about the whole statement.

    ``TRANSFORM`` is at column zero, so this is exact equality *and* the dedent
    ``transform_statement`` applies is an identity on it. Re-indentation is
    therefore the one difference this assertion does NOT detect, deliberately —
    see the module docstring, and
    ``test_transform_statement_indentation.py`` for the indented case, where the
    equality is line-for-line on stripped text because the leading whitespace is
    what has to change.
    """
    runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)

    assert _submitted(glue)[1] == TRANSFORM


def test_the_three_stages_are_submitted_in_the_lifecycle_order(runner, glue):
    """Frame build, then transform, then validation — one statement each.

    Parity with ``_run_lifecycle`` (``code_interpreter_runner.py:351-376``), which
    keeps three separate executions so a failure is attributable to a stage. The
    frame and validation statements are compared against the module's own
    builders, so this cannot drift from what those emit.
    """
    runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)

    assert _submitted(glue) == [
        frame_build_statement(runner.resolve_frame_source(), SCHEMA),
        TRANSFORM,
        validation_statement(TARGET_SCHEMA)[1],
    ]


def test_a_successful_run_reports_the_success_contract(runner, glue):
    """R1.5: ``execution_successful`` true, ``executed`` true, ``exit_code`` zero.

    **Validates: Requirements 1.1, 1.5**

    The record count comes from the ``GENERATED:`` marker through the production
    ``_parse_results``, which is what makes this a check on the composition rather
    than on a dict this test wrote.
    """
    result = runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)

    assert result["execution_successful"] is True
    assert result["executed"] is True
    assert result["exit_code"] == 0
    assert result["input_records"] == 100
    assert result["error_details"] is None
    assert set(result) >= BASE_CONTRACT_KEYS


def test_a_failing_transform_is_attributed_to_the_transform_stage(runner, glue):
    """A Spark exception in the transform: ``executed`` true, non-zero exit.

    **Validates: Requirements 1.1, 1.2**

    The failure is classified from ``Output.Status`` — the Glue ``Traceback`` below
    carries no ``Traceback (most recent call last`` header, which is R5.5's trap.
    And the validation stage is never submitted, because ``run_session`` stops at
    the first failure; that is what makes the stage label meaningful.
    """
    glue.get_statement.side_effect = [
        {"Statement": _ok(GENERATED_STDOUT)},
        {
            "Statement": {
                "State": "AVAILABLE",
                "Output": {
                    "Status": "error",
                    "ErrorName": "AnalysisException",
                    "ErrorValue": "cannot resolve 'nope'",
                    "Traceback": ['  File "<stdin>", line 1, in <module>\n'],
                    "Data": {"TextPlain": ""},
                },
            }
        },
    ]

    result = runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)

    assert result["executed"] is True
    assert result["execution_successful"] is False
    assert result["exit_code"] != 0
    assert STAGE_TRANSFORM in result["error_details"]
    assert "AnalysisException: cannot resolve 'nope'" in result["error_details"]
    assert len(_submitted(glue)) == 2, "the validation stage ran after the transform failed"


# ---------------------------------------------------------------------------
# Pre-flight step 1 — the client, before anything else
# ---------------------------------------------------------------------------


def test_an_unavailable_client_is_refused_before_the_frame_is_considered():
    """Step 1 of the pre-flight order, asserted as an order and not just an outcome.

    **Validates: Requirements 1.1, 1.2**

    A failure, not a non-verification: an executor that could not be reached
    proved nothing *and named no reason a reviewer can act on* (p32 3.22), which
    is the posture ``_empty_results`` holds on the pandas path.

    The schema passed is one the frame refusal WOULD reject, so a runner that
    evaluated the refusal first would return a harness non-verification here. That
    it does not is what pins the position.
    """
    with patch("glue_session_runner.boto3.client", side_effect=RuntimeError("no credentials")):
        runner = GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)

    assert runner.client is None
    result = runner.run_test(TRANSFORM, {}, TARGET_SCHEMA)

    assert result["executed"] is False
    assert result["execution_successful"] is False
    assert result["exit_code"] is None
    assert "glue client not available" in result["error_details"]
    assert "not_verified_by_harness" not in result, (
        "an unavailable client returned the FRAME refusal, so the refusal was evaluated first"
    )


# ---------------------------------------------------------------------------
# Pre-flight step 2 — the conversion step's absence (R3.2)
# ---------------------------------------------------------------------------


def test_the_module_imports_no_converter(runner, glue):
    """R3.2, at its root: there is no proxy in this module to produce a marker.

    **Validates: Requirement 3.2**

    Asserted on the module's namespace rather than on a result, because that is
    where the guarantee lives. Task 8.3's property asserts the consequence over
    every outcome; this asserts the cause, so a future edit that imported the
    converter "just for a helper" fails here with the reason attached.
    """
    assert not hasattr(glue_session_runner, "PySpark2PandasConverter")
    assert not hasattr(glue_session_runner, "ConversionError")
    assert "pyspark_to_pandas" not in glue_session_runner.__dict__


@pytest.mark.parametrize(
    "outcome",
    ["success", "statement_failure", "frame_refusal", "capacity"],
)
def test_no_outcome_carries_a_proxy_non_verification(runner, glue, outcome):
    """R3.2 on the four outcomes this file can drive.

    **Validates: Requirement 3.2**

    Worked cases, not the generalisation — task 8.3's Property 4 quantifies over
    every outcome the executor can produce. What these add is that the *composed*
    method reaches them without acquiring the marker on the way.
    """
    if outcome == "statement_failure":
        glue.get_statement.side_effect = [
            {"Statement": _ok(GENERATED_STDOUT)},
            {"Statement": {"State": "CANCELLED", "Output": {}}},
        ]
        result = runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)
    elif outcome == "frame_refusal":
        result = runner.run_test(TRANSFORM, {}, TARGET_SCHEMA)
    elif outcome == "capacity":
        glue.create_session.side_effect = _capacity_error()
        result = runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)
    else:
        result = runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)

    assert "not_verified_by_proxy" not in result
    assert "not_verified_construct" not in result
    assert "not_verified_line_number" not in result
    assert "not_verified_source_line" not in result
    # Task 8.1's decision: this runner converted nothing, so the key it would
    # have reported on is honestly empty rather than filled with a fabricated
    # report. See `run_test`'s docstring for the rejected alternatives.
    assert result["conversion_report"] is None


# ---------------------------------------------------------------------------
# Pre-flight step 4 — capacity, at CreateSession
# ---------------------------------------------------------------------------


def test_a_capacity_refusal_is_a_harness_non_verification(runner, glue):
    """R8.1 through the composed method, not through the builder.

    **Validates: Requirements 1.1, 1.2**

    ``run_test`` routes a ``CreateSession`` failure through
    ``create_session_failure_results``, which recognises capacity FIRST. Driving it
    from the public method is what checks that the ``except`` clause reaches the
    router rather than one of its two builders — the mistake the router's own
    docstring says would report an account limit as "your transform failed".

    The frame is buildable here, so the refusal reached ``CreateSession``: this is
    the capacity arm and not the frame arm.
    """
    glue.create_session.side_effect = _capacity_error()

    result = runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)

    assert result["not_verified_by_harness"] is True
    assert result["not_verified_reason"] == NOT_VERIFIED_REASON_SESSION_CAPACITY
    assert result["executed"] is False
    assert result["exit_code"] is None
    # A capacity refusal has no field count and no ceiling; carrying zeroes would
    # render a false sentence about the schema (task 3.6's recorded reasoning).
    assert "not_verified_field_count" not in result
    assert "not_verified_frame_ceiling" not in result


def test_a_non_capacity_creation_failure_is_a_failure_not_a_non_verification(runner, glue):
    """R8.5: any other ``CreateSession`` error is a failed verification.

    **Validates: Requirements 1.1, 1.2**

    ``AccessDenied`` is the realistic case — it is what a wrong ``role_arn``
    produces, which is why task 2.3 pins the role's shape with a CDK assertion
    rather than leaving it to be discovered here.
    """
    glue.create_session.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized to perform: glue:CreateSession"}},
        "CreateSession",
    )

    result = runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)

    assert "not_verified_by_harness" not in result
    assert result["executed"] is False
    assert result["execution_successful"] is False
    assert result["exit_code"] is None
    assert "AccessDeniedException" in result["error_details"]


def test_a_session_that_never_becomes_ready_is_a_lifecycle_failure(runner, glue):
    """A ``GlueSessionError`` out of the lifecycle reaches the contract (R5.6).

    **Validates: Requirements 1.1, 1.2**

    The session reached ``FAILED``, from which ``READY`` is unreachable, so
    ``_await_session_ready`` raises and the message NAMES THE REACHED STATE — R5.6
    is met by that text rather than by re-deriving it in the mapping. And because
    the session was created, ``run_session``'s ``finally`` released it before the
    exception surfaced here: R7.1/R7.2 hold across an exception path that
    ``run_test`` adds a clause to.
    """
    glue.get_session.return_value = {"Session": {"Status": "FAILED"}}

    result = runner.run_test(TRANSFORM, SCHEMA, TARGET_SCHEMA)

    assert result["executed"] is False
    assert result["execution_successful"] is False
    assert "FAILED" in result["error_details"]
    # The id the runner GENERATED, read back off the call rather than assumed:
    # `_create_session` supplies its own `Id` (Glue requires a caller-supplied
    # one) and ignores whatever the mock returns.
    created_id = glue.create_session.call_args.kwargs["Id"]
    glue.stop_session.assert_called_once_with(Id=created_id)
    glue.delete_session.assert_called_once_with(Id=created_id)


# ---------------------------------------------------------------------------
# The shared stage labels
# ---------------------------------------------------------------------------


def test_the_stage_labels_are_the_pandas_paths_own():
    """The three stage labels are shared, so a report reads the same either way.

    Parity with ``_run_lifecycle``'s labels, which
    ``_failed_execution_results(stage, …)``, the report and
    ``test_honest_aggregation`` already consume. Asserted as literals because the
    strings ARE the contract with those consumers.
    """
    assert STAGE_FRAME_BUILD == "test data generation"
    assert STAGE_TRANSFORM == "transform execution"
    assert STAGE_VALIDATION == "output validation"
