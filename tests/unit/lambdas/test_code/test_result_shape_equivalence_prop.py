# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 2 — result-shape equivalence across executors.

Spec ``real-spark-verification`` task 8.2.
**Validates: Requirements 1.1, 1.2, 8.4.**

What makes "the same dict shape" checkable rather than aspirational: for any
outcome the Glue executor can be driven to, the dict
``GlueSessionTestRunner.run_test`` returns carries the same key set and the same
per-key value type as the Test_Result_Contract declared in
``tests/fixtures/run_test_result_shapes.py`` (task 1.2) — and, for the five
outcomes both executors can reach, the same key set and type map as the dict
``CodeInterpreterTestRunner.run_test`` returns for the same outcome.

That second form is the one requirement 1.1 and 1.2 actually ask for, and it is
asserted directly rather than inferred from two separate conformance checks: two
results can each satisfy a table and still disagree with each other if the table
admits a union type on a key. ``exit_code: (int, NONE)`` is exactly such a key.

**Nothing here creates a Glue session or deploys anything.** Every AWS call goes
through a ``MagicMock`` and ``time.sleep`` is patched out.

The oracle, and the one exclusion it carries
--------------------------------------------

Compared against :data:`~run_test_result_shapes.EXECUTOR_INDEPENDENT_OUTCOMES`,
not against every entry in the table. The excluded entry is
``conversion_refusal``: it is the pandas proxy's own limitation and requirement
3.2 forbids this runner from producing it, so comparing against it would demand
the very marker R3.2 prohibits — the fixture's own docstring says so, and
:func:`test_the_oracle_never_reaches_for_the_proxy_refusal` pins that this module
honours it.

``conversion_report``'s **narrowing** is excluded from every comparison, and its
key is not. The distinction is the whole of task 8.1's decision: the contract
declares ``conversion_report: (dict, NONE)`` at the base
(``run_test_result_shapes._BASE``), and the per-outcome narrowings to ``dict`` —
on ``success``, ``statement_failure``, ``frame_refusal`` and
``lifecycle_exception`` — are narrowings of the *pandas call path*, driven by a
conversion having run there. This executor converts nothing, so the key's
documented meaning ("None when conversion failed or never ran",
``code_interpreter_runner.py:247-249``) already assigns it ``None``. Task 8.1
verified by grep that the key has **no consumer** — only the two runner modules
write it and only tests read it; ``lambda_function.py`` never reads it — so
``None`` degrades no report and no screen. Excluding the narrowing is the
established pattern in this spec's own tests, at
``test_glue_result_mapping.py:157`` and ``test_glue_result_mapping_prop.py:293``,
and it is why **the fixture needs no change**: a shared oracle four other test
files import is not the place to absorb one executor's call-path difference.

Two shapes the pandas path has no counterpart for
-------------------------------------------------

The Glue executor produces two harness non-verifications the pandas path cannot:
capacity exhaustion at ``CreateSession`` (R8.1) and a frame the builder could not
type at all (R7.3). Both carry the nine base keys plus
``not_verified_by_harness`` and ``not_verified_reason``, and deliberately **not**
``not_verified_field_count`` / ``not_verified_frame_ceiling`` — there is no width
problem in either, so carrying a count and a ceiling would render a false
sentence about the schema while removing a false one about the transform
(``session_capacity_results`` and ``frame_build_failure_results`` both record
that reasoning).

:data:`_HARNESS_WITHOUT_A_WIDTH` is therefore **derived** from the fixture's
``FRAME_REFUSAL`` by dropping exactly those two keys, rather than transcribed.
``LGR-28b4d75-5`` (**template / ratified**) is why the derivation matters: "a
test written from the producer's output cannot detect the producer drifting,
because the output is its own oracle". Deriving keeps the base nine and every
shared type coming from the one declared table, so a contract key added to
``_BASE`` reaches these two shapes without an edit here.

Ledger notes
------------

* ``why`` on ``glue_session_runner.py`` and on
  ``tests/fixtures/run_test_result_shapes.py`` both return **nothing** — neither
  file is committed yet, so no record governs either. Stated as a gap, not as an
  all-clear.
* ``LGR-4dd9b42-1`` (**template / ratified**) is this spec's authorisation, and
  its revisit condition is what the shape equivalence protects: only if the two
  executors' results are interchangeable in shape can shadow mode attribute a
  *difference* to the executor rather than to the envelope.
* ``LGR-69ca78b-1`` (**invariant**) is why R8.4 is asserted here rather than
  assumed: the governance Choice states fail closed, so a key no condition reads
  is inert — but that is a claim about the Choice conditions, so
  :func:`test_no_outcome_adds_a_field_a_choice_state_reads` reads them out of
  ``orchestration-stack.ts`` instead of trusting a comment.
* ``decisions(concept="testing", include_backfilled=True)`` returns five records,
  none about this contract; ``concept="api"`` returns twelve, of which
  ``LGR-819980f-1`` (**invariant**) and ``LGR-b46a5bb-1`` (**invariant**) bear on
  the runner but not on its result shape. No fence governs this file.

Sibling coverage — deliberately not duplicated here
---------------------------------------------------

* ``test_no_proxy_non_verification_prop.py`` (task 8.3) owns Property 4 — that no
  Glue result claims a Proxy_Non_Verification. The key-set claim below forbids
  the proxy keys as a *side effect* of the declared vocabulary excluding
  ``conversion_refusal``; the marker claim as such, with its own reasoning, is
  8.3's.
* ``test_glue_result_mapping.py`` / ``test_glue_result_mapping_prop.py`` — each
  builder's own shape and every field's value (tasks 3.3-3.5).
* ``test_run_test_composition.py`` — the pre-flight order and the routing
  (task 8.1).
* ``test_frame_refusal_precedes_session_prop.py`` — R7.3's position, and that the
  two host-side routes land on one shape (task 5.4).
* ``tests/contracts/api_responses/test_run_test_shape.py`` — the pandas side's
  conformance to the same table (task 1.2).
"""

import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings
from tests.fixtures.run_test_result_shapes import (
    BASE_CONTRACT_KEYS,
    CLIENT_UNAVAILABLE,
    EXECUTOR_INDEPENDENT_OUTCOMES,
    FRAME_REFUSAL,
    LIFECYCLE_EXCEPTION,
    RUN_TEST_OUTCOME_SHAPES,
    STATEMENT_FAILURE,
    SUCCESS,
    OutcomeShape,
    observed_shape,
    shape_violations,
)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_TEST_CODE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code"
)
sys.path.insert(0, _TEST_CODE_DIR)

from code_interpreter_runner import CodeInterpreterTestRunner
from glue_session_runner import (
    _FLOAT_DECLARED_TYPES,
    _INT_DECLARED_TYPES,
    _MAX_TEST_FRAME_COLUMNS,
    _STRING_DECLARED_TYPES,
    GlueSessionTestRunner,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------

#: The shape both Glue-only harness non-verifications carry — capacity (R8.1) and
#: an unbuildable frame (R7.3). Derived from the declared ``FRAME_REFUSAL`` rather
#: than written out, so the nine base keys and every shared type still come from
#: the fixture: see the module docstring on ``LGR-28b4d75-5``.
_HARNESS_WITHOUT_A_WIDTH = OutcomeShape(
    name="harness_non_verification_without_a_width",
    types={
        key: allowed
        for key, allowed in FRAME_REFUSAL.types.items()
        if key not in ("not_verified_field_count", "not_verified_frame_ceiling")
    },
    executor_independent=True,
    source="derived from FRAME_REFUSAL minus the two width keys — glue_session_runner.session_capacity_results / frame_build_failure_results",
)

#: Outcome label → the declared shape the Glue result must match.
#:
#: Every value is an ``executor_independent`` shape or a derivation of one; the
#: proxy's ``conversion_refusal`` appears nowhere, which
#: :func:`test_the_oracle_never_reaches_for_the_proxy_refusal` pins.
_EXPECTED_SHAPE: dict[str, OutcomeShape] = {
    "success": SUCCESS,
    "statement_failure": STATEMENT_FAILURE,
    "frame_refusal": FRAME_REFUSAL,
    "client_unavailable": CLIENT_UNAVAILABLE,
    "lifecycle_failure": LIFECYCLE_EXCEPTION,
    # A boto3 error out of any AWS call. Same envelope as the two "nothing ran"
    # outcomes — `executed` false, no `exit_code`, the reported error in
    # `error_details` — which is what `run_test`'s docstring means when it says
    # the shape is identical whichever call raised and only the prefix is
    # imprecise. Asserting the shape is asserting exactly that.
    "aws_error": CLIENT_UNAVAILABLE,
    "capacity": _HARNESS_WITHOUT_A_WIDTH,
    "frame_build_failure": _HARNESS_WITHOUT_A_WIDTH,
}

#: The outcomes BOTH executors can reach, so a direct comparison is possible.
#: The three absent ones are Glue-only: capacity and the unbuildable frame have no
#: pandas counterpart at all, and a boto3 error out of a Glue call has none by
#: construction.
_SHARED_OUTCOMES: tuple[str, ...] = (
    "success",
    "statement_failure",
    "frame_refusal",
    "client_unavailable",
    "lifecycle_failure",
)

#: Every key the declared contract permits an executor-independent outcome to
#: carry. The union, so the claim below ("no key outside this") is a claim about
#: the contract's whole vocabulary rather than about one outcome.
_DECLARED_CONTRACT_KEYS: frozenset[str] = frozenset().union(
    *(RUN_TEST_OUTCOME_SHAPES[name].keys for name in EXECUTOR_INDEPENDENT_OUTCOMES)
)

_ORCHESTRATION_STACK = Path(__file__).resolve().parents[4] / "deploy-cdk" / "lib" / "orchestration-stack.ts"

#: Every `sfn.Condition.*` call in the state machine that addresses a path under
#: `$.test_code_result`.
_CHOICE_CONDITION = re.compile(r"sfn\.Condition\.[A-Za-z]+\(\s*'\$\.test_code_result((?:\.[A-Za-z_][A-Za-z0-9_]*)+)'")


def _choice_read_field_names() -> frozenset[str]:
    """Field names a Step Functions Choice state reads off the TestCode result.

    Parsed out of the state machine rather than transcribed, following
    ``LGR-28b4d75-5``'s principle: the consumer is the oracle, and a list copied
    into this file would go stale the first time a condition is added. Every path
    segment below ``test_code_result`` counts, including the intermediate
    ``test_result`` — a runner key by that name would collide just as surely as a
    leaf one.
    """
    source = _ORCHESTRATION_STACK.read_text(encoding="utf-8")
    names: set[str] = set()
    for path in _CHOICE_CONDITION.findall(source):
        names.update(segment for segment in path.split(".") if segment)
    return frozenset(names)


def _violations(result: dict, shape: OutcomeShape) -> list[str]:
    """Shape violations, excluding every one that mentions ``conversion_report``.

    The exclusion is by substring — the same three lines as
    ``test_glue_result_mapping.py:157`` — so it drops the type comparison the
    module docstring explains AND, unavoidably, a ``missing key`` report for the
    same key. **The key's presence is therefore not enforced here.** It is
    enforced twice over elsewhere, deliberately rather than by luck:

    * :func:`test_no_glue_outcome_carries_a_key_outside_the_declared_contract`
      asserts ``BASE_CONTRACT_KEYS <= set(result)``, and ``conversion_report`` is
      one of those nine;
    * :func:`test_both_executors_return_one_key_set_and_one_type_map` compares the
      two executors' key sets for equality, so the key going missing on one side
      fails there too.

    Stated rather than left implicit because a reader checking whether R1.1's
    key-set equality survives this exclusion needs to know where it went, not to
    rediscover that a substring filter is wider than the sentence above it.
    """
    return [violation for violation in shape_violations(result, shape) if "conversion_report" not in violation]


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: Declared field types the frame builder knows. Sorted so the strategy is
#: reproducible across runs — `frozenset` iteration order is not.
_KNOWN_DECLARED_TYPES: tuple[str, ...] = tuple(
    sorted(_INT_DECLARED_TYPES | _FLOAT_DECLARED_TYPES | _STRING_DECLARED_TYPES)
)

#: Transform texts. `initcap` is a member of the pandas proxy's
#: `_UNSUPPORTED_FUNCTIONS` and design C10 measured it executing successfully on
#: the probe session — included so the shape claim is shown to hold for a
#: construct the other executor refuses by name.
#:
#: **Double quotes, and do NOT "tidy" them to single ones.** ``LGR-f6519e1-1``
#: (**template / ratified**) governs the converter's literal fences, and
#: ``_string``'s docstring (``pyspark_to_pandas.py:1136-1146``) records the
#: double-quote fence as DELIBERATE: a single-quoted literal refuses, because the
#: generation prompt emits double quotes and all five measured production regexes
#: are double-quoted, "so lifting it would widen the accepted surface for no
#: measured demand". A single-quoted transform therefore makes the pandas driver
#: below return the ``conversion_refusal`` shape — the one outcome this module
#: must not involve — and it looks like an executor disagreement rather than the
#: fence it is.
_TRANSFORMS: tuple[str, ...] = (
    'df_transformed = df.withColumn("name", lower(col("name")))',
    'df_transformed = df.withColumn("name", initcap(col("name")))',
    "df_transformed = df",
)

#: A transform the pandas proxy converts cleanly. Used wherever both executors
#: are driven to one outcome, so the input is identical on both sides and the
#: comparison is about the executor. Double-quoted for the reason above.
CONVERTIBLE_TRANSFORM = 'df_transformed = df.withColumn("name", lower(col("name")))'

#: Glue statement states other than `AVAILABLE` that end a statement (R5.6).
_NON_AVAILABLE_TERMINAL_STATES = ("CANCELLED", "ERROR")

#: Session states from which `READY` is unreachable, so `_await_session_ready`
#: raises and names the state it saw.
_TERMINAL_BAD_SESSION_STATES = ("FAILED", "STOPPED", "STOPPING", "TIMEOUT")

#: boto3 error codes that are NOT the capacity code, so the router's else-branch
#: is taken. `AccessDeniedException` is the realistic one — it is what a wrong
#: `role_arn` produces.
_NON_CAPACITY_ERROR_CODES = (
    "AccessDeniedException",
    "ThrottlingException",
    "ValidationException",
    "InternalServiceException",
)

#: Which AWS call the generated boto3 error is raised from. All three land on the
#: same envelope; that they do is the claim.
_RAISING_CALLS = ("create_session", "run_statement", "get_statement")


@dataclass(frozen=True)
class Scenario:
    """One generated drive of an executor to one outcome.

    Every field is generated for every outcome; each driver reads the ones its
    path uses. Carrying the unused ones costs nothing and keeps the strategy one
    flat ``st.builds`` rather than a tree of branches whose shrinking is harder
    to read.
    """

    outcome: str
    width: int
    declared_type: str
    unknown_type: str
    transform: str
    #: Which of the three stages fails, for `statement_failure`: 1 transform,
    #: 2 validation. The frame build is stage 0 and is covered by
    #: `test_frame_refusal_precedes_session_prop.py`, not here.
    failing_stage: int
    failing_state: str
    output_status: str
    error_name: str
    error_value: str
    session_state: str
    error_code: str
    raising_call: str
    message: str
    input_count: int
    output_count: int
    quality: float
    duplicates: int
    #: `frame_refusal`'s two arms: an empty schema (`no_source_schema`) or one
    #: wider than the 512-column ceiling (`frame_truncated`).
    refusal_is_empty_schema: bool


def _scenarios(outcomes: tuple[str, ...]) -> st.SearchStrategy[Scenario]:
    """Scenarios over *outcomes*.

    ``width`` stays small: the frame statement is a generated code *string*, and a
    wide schema makes every example slower without exercising a different shape —
    the ceiling's own arm is generated separately by
    ``refusal_is_empty_schema``.

    ``input_count`` / ``output_count`` are integers and ``quality`` a float
    because that is what the producer emits: ``_parse_results`` assigns
    ``input_records = gen["record_count"]`` unconverted
    (``code_interpreter_runner.py:617``), so the payload's JSON types ARE the
    result's types. Generating a float count would find a defect in this
    generator rather than in either executor — the parser's robustness to a
    malformed payload is task 6.2's Property 3, not this one.
    """
    return st.builds(
        Scenario,
        outcome=st.sampled_from(outcomes),
        width=st.integers(min_value=1, max_value=6),
        declared_type=st.sampled_from(_KNOWN_DECLARED_TYPES),
        unknown_type=st.text(min_size=1, max_size=12).filter(
            lambda value: (
                value not in _INT_DECLARED_TYPES
                and value not in _FLOAT_DECLARED_TYPES
                and value not in _STRING_DECLARED_TYPES
            )
        ),
        transform=st.sampled_from(_TRANSFORMS),
        failing_stage=st.sampled_from((1, 2)),
        failing_state=st.sampled_from(("AVAILABLE", *_NON_AVAILABLE_TERMINAL_STATES)),
        output_status=st.sampled_from(("error", "failed", "cancelled", "")),
        error_name=st.sampled_from(("AnalysisException", "Py4JJavaError", "ValueError", "")),
        error_value=st.text(max_size=40),
        session_state=st.sampled_from(_TERMINAL_BAD_SESSION_STATES),
        error_code=st.sampled_from(_NON_CAPACITY_ERROR_CODES),
        raising_call=st.sampled_from(_RAISING_CALLS),
        message=st.text(min_size=1, max_size=40),
        input_count=st.integers(min_value=0, max_value=1000),
        output_count=st.integers(min_value=0, max_value=1000),
        quality=st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
        duplicates=st.integers(min_value=0, max_value=50),
        refusal_is_empty_schema=st.booleans(),
    )


def _schema(width: int, declared_type: str) -> dict:
    return {"properties": {f"col_{i}": {"type": declared_type} for i in range(width)}}


def _source_schema(scenario: Scenario) -> dict:
    """The source schema each outcome needs to be reached.

    ``frame_build_failure`` puts one unknown declared type into an otherwise
    buildable schema; ``frame_refusal`` picks between its two arms.
    """
    if scenario.outcome == "frame_build_failure":
        schema = _schema(scenario.width, scenario.declared_type)
        schema["properties"]["col_0"] = {"type": scenario.unknown_type}
        return schema
    if scenario.outcome == "frame_refusal":
        if scenario.refusal_is_empty_schema:
            return {}
        return _schema(_MAX_TEST_FRAME_COLUMNS + 1, scenario.declared_type)
    return _schema(scenario.width, scenario.declared_type)


def _target_schema(scenario: Scenario) -> dict:
    return _schema(scenario.width, scenario.declared_type)


def _generated_marker(scenario: Scenario) -> str:
    return f"GENERATED:{json.dumps({'record_count': scenario.input_count, 'columns': ['col_0']})}"


def _validation_marker(scenario: Scenario) -> str:
    """The nine-key ``VALIDATION:`` payload, in the form ``_parse_results`` reads."""
    return "VALIDATION:" + json.dumps(
        {
            "record_count": scenario.output_count,
            "null_counts": {"col_0": scenario.duplicates},
            "dtypes": {"col_0": "bigint"},
            "duplicates": scenario.duplicates,
            "schema_compliance": scenario.quality,
            "completeness": scenario.quality,
            "uniqueness": scenario.quality,
            "overall_quality": scenario.quality,
            "missing_columns": [],
        }
    )


def _boto_error(code: str, message: str, operation: str) -> Exception:
    """A boto3-shaped error.

    Built as a plain exception carrying a ``response`` dict rather than as a
    ``ClientError``, because that is what the runner reads: ``_error_code``
    (``glue_session_runner.py:1181``) looks at ``exc.response`` precisely so it
    need not import ``botocore`` or catch a per-client generated class.
    """
    error = Exception(f"An error occurred ({code}) when calling the {operation} operation: {message}")
    error.response = {"Error": {"Code": code, "Message": message}}  # type: ignore[attr-defined]
    return error


# ---------------------------------------------------------------------------
# Glue drivers
# ---------------------------------------------------------------------------


def _glue_client(statements: list[dict]) -> MagicMock:
    """A Glue client that walks a session to ``READY`` and returns *statements*."""
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.side_effect = [{"Id": index} for index in range(1, len(statements) + 2)]
    client.get_statement.side_effect = [{"Statement": statement} for statement in statements]
    return client


def _ok_statement(stdout: str = "") -> dict:
    return {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": stdout}}}


def _failed_statement(scenario: Scenario) -> dict:
    """A statement that ran and failed, in one of R5.6's two shapes.

    The generated ``Traceback`` is **header-free** — a list of frame strings, which
    is what Glue actually sends — so R5.5's trap is covered by construction rather
    than by hope.
    """
    if scenario.failing_state == "AVAILABLE":
        return {
            "State": "AVAILABLE",
            "Output": {
                "Status": scenario.output_status,
                "ErrorName": scenario.error_name,
                "ErrorValue": scenario.error_value,
                "Traceback": ['  File "<stdin>", line 1, in <module>\n'],
                "Data": {"TextPlain": ""},
            },
        }
    return {"State": scenario.failing_state, "Output": {}}


@contextmanager
def _glue_runner(client: MagicMock):
    with patch("glue_session_runner.boto3.client", return_value=client), patch("glue_session_runner.time.sleep"):
        yield GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)


def _drive_glue(scenario: Scenario) -> dict:
    """Run the Glue executor down the path *scenario* names, through ``run_test``.

    The public method every time — the routing and the pre-flight order are part
    of what the shape depends on, so driving a builder directly would assert the
    shape of a dict nothing returns.
    """
    outcome = scenario.outcome
    source_schema = _source_schema(scenario)
    target_schema = _target_schema(scenario)

    if outcome == "client_unavailable":
        with patch("glue_session_runner.boto3.client", side_effect=RuntimeError(scenario.message)):
            runner = GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)
        assert runner.client is None
        return runner.run_test(scenario.transform, source_schema, target_schema)

    if outcome == "success":
        statements = [
            _ok_statement(_generated_marker(scenario)),
            _ok_statement(),
            _ok_statement(_validation_marker(scenario)),
        ]
    elif outcome == "statement_failure":
        statements = [_ok_statement(_generated_marker(scenario))]
        if scenario.failing_stage == 2:
            statements.append(_ok_statement())
        statements.append(_failed_statement(scenario))
    else:
        # Refusals never reach a statement; the lifecycle and AWS-error paths
        # raise before or during the first one.
        statements = [_ok_statement(_generated_marker(scenario))]

    client = _glue_client(statements)

    if outcome == "lifecycle_failure":
        client.get_session.return_value = {"Session": {"Status": scenario.session_state}}
    elif outcome == "capacity":
        client.create_session.side_effect = _boto_error(
            "ResourceNumberLimitExceededException", scenario.message, "CreateSession"
        )
    elif outcome == "aws_error":
        getattr(client, scenario.raising_call).side_effect = _boto_error(
            scenario.error_code, scenario.message, scenario.raising_call
        )

    with _glue_runner(client) as runner:
        return runner.run_test(scenario.transform, source_schema, target_schema)


# ---------------------------------------------------------------------------
# Pandas drivers — the other side of the comparison
# ---------------------------------------------------------------------------


def _stream(text: str, stderr: str = "", exit_code: int = 0) -> dict:
    """A fake ``invoke_code_interpreter`` response, as ``test_run_test_shape.py`` builds it."""
    return {
        "stream": [
            {
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": {"stdout": text, "stderr": stderr, "exitCode": exit_code},
                }
            }
        ]
    }


_TRACEBACK_STDERR = 'Traceback (most recent call last):\n  File "<stdin>", line 3\nKeyError: nope'


def _pandas_runner(client: MagicMock) -> CodeInterpreterTestRunner:
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = client
        runner = CodeInterpreterTestRunner(region=REGION)
        runner.client = client
        return runner


def _drive_pandas(scenario: Scenario) -> dict:
    """Run the pandas executor down the same outcome, with the same schemas.

    Always the convertible transform: an unconvertible one returns the
    ``conversion_refusal`` shape, which is the one outcome the comparison must not
    involve (R3.2). What is held identical between the two sides is the *outcome
    and the schemas*; the transform text cannot be, because "a construct the
    proxy refuses" is precisely what one executor and not the other can run.
    """
    source_schema = _source_schema(scenario)
    target_schema = _target_schema(scenario)

    client = MagicMock()
    client.start_code_interpreter_session.return_value = {"sessionId": "sess-equivalence"}
    client.stop_code_interpreter_session.return_value = {}

    if scenario.outcome == "success":
        client.invoke_code_interpreter.side_effect = [
            _stream(_generated_marker(scenario)),
            _stream("transform executed"),
            _stream(_validation_marker(scenario)),
        ]
    elif scenario.outcome == "statement_failure":
        client.invoke_code_interpreter.side_effect = [
            _stream(_generated_marker(scenario)),
            _stream("partial", stderr=_TRACEBACK_STDERR, exit_code=1),
            _stream(_validation_marker(scenario)),
        ]
    elif scenario.outcome == "lifecycle_failure":
        client.invoke_code_interpreter.side_effect = RuntimeError(scenario.message)
    else:
        client.invoke_code_interpreter.side_effect = [
            _stream(_generated_marker(scenario)),
            _stream("transform executed"),
            _stream(_validation_marker(scenario)),
        ]

    runner = _pandas_runner(client)
    if scenario.outcome == "client_unavailable":
        runner.client = None
    return runner.run_test(CONVERTIBLE_TRANSFORM, source_schema, target_schema)


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


@settings(max_examples=250, deadline=None)
@given(_scenarios(tuple(_EXPECTED_SHAPE)))
def test_every_glue_outcome_matches_its_declared_shape(scenario: Scenario):
    """R1.1 / R1.2 — the Glue result conforms to the declared Test_Result_Contract.

    **Validates: Requirements 1.1, 1.2**

    Quantified over every outcome ``run_test`` can be driven to and over the
    variation inside each: schema width and declared type, which stage fails and
    in which of R5.6's two shapes, the reported exception name and value, the
    session state reached, the boto3 error code and which call raised it, and the
    record counts and quality values in the ``VALIDATION:`` payload.

    Compared against ``tests/fixtures/run_test_result_shapes.py`` — types by
    identity, not ``isinstance``, which is the fixture's own deliberate choice:
    ``bool`` subclasses ``int``, so an ``isinstance`` comparison would let
    ``executed=True`` satisfy ``input_records: int``.
    """
    result = _drive_glue(scenario)
    shape = _EXPECTED_SHAPE[scenario.outcome]

    violations = _violations(result, shape)
    assert not violations, (
        f"GlueSessionTestRunner.run_test's {scenario.outcome!r} outcome departs from the "
        f"declared shape {shape.name!r} ({shape.source}):\n  " + "\n  ".join(violations)
    )


@settings(max_examples=200, deadline=None)
@given(_scenarios(_SHARED_OUTCOMES))
def test_both_executors_return_one_key_set_and_one_type_map(scenario: Scenario):
    """R1.1 / R1.2 stated as the equivalence they actually ask for.

    **Validates: Requirements 1.1, 1.2**

    Both executors driven to the same outcome from the same schemas, and their
    results compared **to each other** rather than each to the table. This is the
    stronger claim and it is not implied by the two conformance checks: the
    contract admits union types — ``exit_code: (int, NONE)``, ``error_details:
    (str, NONE)`` — so two results can each satisfy a declared shape while
    disagreeing about which arm of the union they took. A report or a gate reading
    one executor's output and then the other's would see that difference; the
    table would not.

    ``conversion_report`` is compared for its PRESENCE and not its type, which is
    the single documented divergence: task 8.1 leaves it ``None`` on every Glue
    path because no conversion ran, and the key has no consumer. Everything else
    must agree exactly.
    """
    glue_result = _drive_glue(scenario)
    pandas_result = _drive_pandas(scenario)

    assert set(glue_result) == set(pandas_result), (
        f"the two executors' {scenario.outcome!r} results carry different keys: "
        f"only in glue={sorted(set(glue_result) - set(pandas_result))}, "
        f"only in pandas={sorted(set(pandas_result) - set(glue_result))}"
    )

    glue_types = observed_shape(glue_result)
    pandas_types = observed_shape(pandas_result)
    disagreements = {
        key: (glue_types[key].__name__, pandas_types[key].__name__)
        for key in glue_types
        if key != "conversion_report" and glue_types[key] is not pandas_types[key]
    }
    assert not disagreements, (
        f"the two executors' {scenario.outcome!r} results disagree on value type (glue, pandas): {disagreements}"
    )


@settings(max_examples=250, deadline=None)
@given(_scenarios(tuple(_EXPECTED_SHAPE)))
def test_no_glue_outcome_carries_a_key_outside_the_declared_contract(scenario: Scenario):
    """R1.1 over the contract's whole vocabulary, in both directions.

    **Validates: Requirement 1.1**

    The nine base keys are present on every outcome — a report or the gate reading
    one of them must never find it absent — and no outcome carries a key the
    declared executor-independent contract does not permit. The second half is
    what catches a *new* Glue-only path inventing a field: the two harness
    non-verifications this executor adds are new REASONS on existing keys, not new
    keys, and this is where that stays true.

    Because ``conversion_refusal`` is excluded from the vocabulary (R3.2), this
    also forbids the four proxy keys. That is a side effect of the key-set claim;
    Property 4 (task 8.3) owns the marker claim on its own terms.
    """
    result = _drive_glue(scenario)
    keys = set(result)

    assert keys >= BASE_CONTRACT_KEYS, (
        f"{scenario.outcome!r} omits base contract key(s) {sorted(BASE_CONTRACT_KEYS - keys)}"
    )
    assert keys <= _DECLARED_CONTRACT_KEYS, (
        f"{scenario.outcome!r} carries key(s) {sorted(keys - _DECLARED_CONTRACT_KEYS)} that no "
        f"executor-independent outcome in tests/fixtures/run_test_result_shapes.py declares"
    )


@settings(max_examples=150, deadline=None)
@given(_scenarios(tuple(_EXPECTED_SHAPE)))
def test_no_outcome_adds_a_field_a_choice_state_reads(scenario: Scenario):
    """R8.4 — the capacity non-verification adds no Choice-visible field.

    **Validates: Requirement 8.4**

    Generalised past the capacity outcome the requirement names, because the rule
    it states ("a new reason, not a new field and not a new state",
    ``lambda_function.py:508-527``) binds every outcome and a later one is where
    it would be broken.

    ``LGR-69ca78b-1`` (**invariant**) is why the direction matters: both
    governance Choice states fail closed, so a field no condition reads is inert
    rather than permissive — but that makes the claim depend on what the
    conditions actually address, so the names are read out of
    ``orchestration-stack.ts`` rather than transcribed here.
    """
    choice_fields = _choice_read_field_names()
    result = _drive_glue(scenario)

    collisions = sorted(set(result) & choice_fields)
    assert not collisions, (
        f"{scenario.outcome!r} carries {collisions}, which a Step Functions Choice condition "
        f"addresses under $.test_code_result — R8.4 permits a new reason, not a new field"
    )


# ---------------------------------------------------------------------------
# Guards on the property itself
# ---------------------------------------------------------------------------


def test_every_declared_executor_independent_outcome_has_a_glue_driver():
    """The gap the property would otherwise pass through.

    A declared shape with no driver is a shape asserted against nothing, so
    Property 2 would report success for an executor that never produces that
    outcome at all. The converse of
    ``test_run_test_shape.py::test_declared_shapes_all_have_a_driver``, for the
    second executor.

    Checked against the SHAPE names rather than the driver labels: the two do not
    correspond one-to-one and are not meant to. ``lifecycle_failure`` drives the
    shape the fixture calls ``lifecycle_exception``, and ``aws_error`` reaches
    ``client_unavailable``'s shape by a different route — which is itself part of
    the claim (see :data:`_EXPECTED_SHAPE`).
    """
    covered = {shape.name for shape in _EXPECTED_SHAPE.values()}
    undriven = sorted(set(EXECUTOR_INDEPENDENT_OUTCOMES) - covered)
    assert not undriven, (
        f"{undriven} are declared executor-independent but no Glue driver reaches them, "
        "so nothing holds this runner to their shape"
    )


def test_the_oracle_never_reaches_for_the_proxy_refusal():
    """Every shape compared against is executor-independent (R3.2).

    The fixture's docstring states the constraint: comparing the Glue runner
    against ``conversion_refusal`` "would demand the marker R3.2 prohibits". This
    pins that the map above honours it — including through
    :data:`_HARNESS_WITHOUT_A_WIDTH`, whose derivation would carry the refusal's
    keys if it were ever re-based onto the wrong shape.
    """
    assert "conversion_refusal" not in _EXPECTED_SHAPE
    for outcome, shape in _EXPECTED_SHAPE.items():
        assert shape.executor_independent, f"{outcome!r} is compared against an executor-DEPENDENT shape"
    assert RUN_TEST_OUTCOME_SHAPES["conversion_refusal"].keys - _DECLARED_CONTRACT_KEYS, (
        "the proxy refusal's extra keys are inside the declared vocabulary, so the key-set "
        "claim above no longer excludes them"
    )


def test_the_harness_shape_is_derived_from_the_declared_frame_refusal():
    """:data:`_HARNESS_WITHOUT_A_WIDTH` is FRAME_REFUSAL minus exactly two keys.

    Asserted rather than trusted because the derivation is what keeps the base
    nine keys and their types coming from the fixture (``LGR-28b4d75-5``): a
    hand-written copy would drift silently the first time a contract key is added
    to ``_BASE``.
    """
    assert FRAME_REFUSAL.keys - _HARNESS_WITHOUT_A_WIDTH.keys == {
        "not_verified_field_count",
        "not_verified_frame_ceiling",
    }
    assert _HARNESS_WITHOUT_A_WIDTH.keys >= BASE_CONTRACT_KEYS
    for key in _HARNESS_WITHOUT_A_WIDTH.keys:
        assert _HARNESS_WITHOUT_A_WIDTH.types[key] == FRAME_REFUSAL.types[key]


def test_the_choice_condition_oracle_finds_the_conditions_it_parses():
    """The R8.4 oracle is not silently empty.

    A regex that matched nothing — a renamed file, a reformatted condition — would
    make :func:`test_no_outcome_adds_a_field_a_choice_state_reads` pass
    unconditionally. The five field names the requirement's own citation
    (``orchestration-stack.ts:851-923``) names are asserted present, so a parse
    that stops working fails here instead of going quiet.
    """
    assert _ORCHESTRATION_STACK.is_file(), f"the state machine is not at {_ORCHESTRATION_STACK}"
    names = _choice_read_field_names()
    assert {
        "status",
        "regeneration_needed",
        "regeneration_attempt",
        "regeneration_max_attempts",
        "approval_required",
    } <= names, f"the Choice-condition parse found only {sorted(names)}"


@pytest.mark.parametrize("outcome", sorted(_EXPECTED_SHAPE))
def test_each_driver_reaches_the_path_it_names(outcome: str):
    """Each driver produces the discriminators its outcome is defined by.

    Not a shape claim. It exists because a driver that quietly stopped reproducing
    its path — a mocked call that no longer raises, say — would still satisfy every
    property above by landing on a shape it happens to match, and the outcome it
    was meant to pin would go unexercised. The three base-nine outcomes are the
    live case: ``client_unavailable``, ``lifecycle_failure`` and ``aws_error``
    share a key set and differ only in what ``error_details`` says.
    """
    scenario = Scenario(
        outcome=outcome,
        width=2,
        declared_type="string",
        unknown_type="geography",
        transform=CONVERTIBLE_TRANSFORM,
        failing_stage=1,
        failing_state="AVAILABLE",
        output_status="error",
        error_name="AnalysisException",
        error_value="cannot resolve 'nope'",
        session_state="FAILED",
        error_code="AccessDeniedException",
        raising_call="create_session",
        message="driver check",
        input_count=100,
        output_count=95,
        quality=75.0,
        duplicates=2,
        refusal_is_empty_schema=True,
    )
    result = _drive_glue(scenario)

    if outcome == "success":
        assert result["execution_successful"] is True
        assert result["executed"] is True
        assert result["exit_code"] == 0
    elif outcome == "statement_failure":
        assert result["executed"] is True
        assert result["exit_code"] != 0
        assert "AnalysisException" in result["error_details"]
    elif outcome == "frame_refusal":
        assert result["not_verified_by_harness"] is True
        assert "not_verified_field_count" in result
    elif outcome == "capacity":
        assert result["not_verified_by_harness"] is True
        assert result["not_verified_reason"] == "session_capacity_exhausted"
    elif outcome == "frame_build_failure":
        assert result["not_verified_by_harness"] is True
        assert result["not_verified_reason"] == "test_frame_unbuildable"
    elif outcome == "client_unavailable":
        assert "glue client not available" in result["error_details"]
    elif outcome == "lifecycle_failure":
        assert "FAILED" in result["error_details"]
    elif outcome == "aws_error":
        assert "AccessDeniedException" in result["error_details"]
        assert "not_verified_by_harness" not in result
    else:  # pragma: no cover - the parametrize list is _EXPECTED_SHAPE's keys
        raise AssertionError(f"no discriminator asserted for {outcome!r}")
