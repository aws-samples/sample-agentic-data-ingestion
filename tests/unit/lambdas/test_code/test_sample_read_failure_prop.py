# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 4: a failed source read is a harness non-verification, never a transform failure.

Spec `p44-verify-against-real-source-data` task 6.2 — requirements 2.1, 2.2, 2.3,
2.4, 3.4, 4.1.

# Feature: p44-verify-against-real-source-data, Property 4: A failed source read
# is a harness non-verification, never a transform failure.

    *For any* Sample_Frame run whose frame-build stage fails — across every
    terminal statement state, every combination of present and absent
    ``ErrorName`` / ``ErrorValue``, every traceback shape, and the deadline route
    — the result carries ``not_verified_by_harness`` true,
    ``not_verified_reason == "source_read_failed"``, ``executed`` false, no
    ``exit_code``, and ``error_details`` naming the source read and the object;
    and the Verification_Session is released. Rewriting the traceback text
    changes none of it.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 3.4, 4.1**

NO GLUE SESSION IS CREATED ANYWHERE IN THIS FILE, AND NONE MAY BE
---------------------------------------------------------------
do-NOT 1 — interactive sessions bill. That fence needs measuring rather than
asserting in prose, because this file's whole subject is a run that *does* reach
``CreateSession``: unlike ``test_frame_provenance.py``, whose paths all stop
before it, Property 4 is about what happens once the frame-build statement has
been submitted. So ``create_session.assert_not_called()`` is not available here
and something stronger is needed in its place. Three measures, in order of how
much they prove:

1. **Every runner is constructed with ``glue_session_runner.boto3.client``
   patched**, so ``self.client`` is a ``MagicMock`` and no real client object
   exists on the instance to call. Asserted per drive
   (:func:`_drive` guards ``runner.client is client``) rather than trusted, since
   a ``None`` client would make every property below vacuous by taking
   ``run_test``'s first pre-flight branch.
2. **``botocore``'s own call seam is guarded** by :func:`no_glue_api_calls`,
   which raises :class:`GlueApiGuardViolation` from any ``_make_api_call`` naming
   a session operation. This is the measurement rather than the assumption: if a
   real client were ever constructed here — through ``boto3.Session().client``,
   at module import, or because a patch target moved — the guard fires instead of
   AWS. It derives from ``BaseException`` for the reason
   ``tests/unit/conftest.py``'s ``CloudWatchGuardViolation`` does
   (``LGR-3e45e65-1``'s sibling guard, read in full): ``run_test``'s third
   ``except Exception`` clause would otherwise catch it, route it to
   ``create_session_failure_results``, and the test would pass while a real API
   call had been attempted.
3. **The role ARN is a placeholder** with a 123456789012 account, so even a
   escaped call could not assume a real role.

``make test-e2e`` and the two ``tests/e2e/test_glue_*.py`` files are forbidden
for this spec, and ``--override-ini=addopts=`` is never passed.

THE THREE DOORS, AND WHY THE THIRD ONE IS DELIBERATELY IMPRECISE
----------------------------------------------------------------
Property 4 names two routes and task 6.1 landed two. The generator draws three,
because ``run_test``'s deadline arm is wider than R3.4's wording:

* :data:`DOOR_STATEMENT` — the frame-build statement reached a terminal state and
  ``StatementOutcome.failed`` is true. ``sample_read_failure`` is the gate and
  ``sample_read_failure_results`` the destination (R2.1, R2.2, R4.1).
* :data:`DOOR_STATEMENT_DEADLINE` — the statement never reached a terminal state
  inside ``_STATEMENT_DEADLINE_SECONDS``, so ``_await_statement`` raised
  ``GlueSessionError`` and there is no ``StatementOutcome`` to read.
  ``sample_read_deadline_results`` (R3.4).
* :data:`DOOR_SESSION_NEVER_READY` — the session reached a state from which
  ``READY`` is unreachable, so ``_await_session_ready`` raised the same exception
  type before any statement was submitted.

The third one is **not** a frame-build deadline, and it still lands on
``sample_read_deadline_results``. That is a stated imprecision in ``run_test``'s
own docstring, not a defect this file is finding: ``GlueSessionError`` carries no
stage, so narrowing the arm would mean re-deriving the statement from the
exception's *message*, which is the inference path
``_SYNTHESISED_TRACEBACK_HEADER`` documents as the one to avoid. The contract
shape is identical either way and the exception's own text travels inside
``error_details`` naming the state actually reached, so a reader is not misled —
only the framing sentence is imprecise. **This file covers that door rather than
objecting to it**; production code is not changed to make a test tidier.

WHY THE STATEMENT STATE AXIS EXCLUDES THE NON-TERMINAL STATES
-------------------------------------------------------------
``_STATEMENT_STATES_TERMINAL`` is ``{AVAILABLE, CANCELLED, ERROR}``
(``glue_session_runner.py:175``) and ``_await_statement`` *polls until terminal*,
so a stub returning ``CANCELLING`` or ``TIMEOUT`` on :data:`DOOR_STATEMENT` never
leaves the loop and the draw silently exercises the deadline door instead —
passing while testing something other than what it named.
``test_no_proxy_non_verification_prop.py``'s :data:`_BAD_STATEMENT_STATES`
records that exact trap catching an earlier draft of that file. Those states are
reached here on purpose through :data:`DOOR_STATEMENT_DEADLINE`, which is what
they actually produce, and
:func:`test_every_door_is_reachable_and_lands_where_it_names` is what keeps every
draw honest about its route.

THE NON-VACUITY ARGUMENT, WHICH IS THE POINT OF THE CONTROL
-----------------------------------------------------------
"Every result carries a harness marker" is satisfiable by a runner that always
returns one, so the load-bearing test in this file is the *discrimination*:
:func:`test_the_same_observation_on_a_synthetic_frame_is_an_executed_failure`
drives the identical Glue observation through the identical code with
``frame_mode`` left at its default, and asserts it still reaches
``statement_failure_results`` — ``executed`` true, ``exit_code`` 1, the
``"test data generation failed: …"`` sentence, no harness marker. That sentence
is *correct* for a synthetic frame, where the stage really was generating test
data, and task 6.1 routes only the sample path away from it. A change that made
the honest sentence unreachable would be the mirror image of the defect p44
removes, and this is the assertion that would notice.

WHAT DERIVES ITS EXPECTATION FROM WHAT
--------------------------------------
``LGR-28b4d75-5`` (**template / ratified**): an expectation read off the thing
under test cannot detect it drifting.

* The **reason string** is asserted against
  ``NOT_VERIFIED_REASON_SOURCE_READ_FAILED`` *and*, once, against the literal
  ``"source_read_failed"`` R2.1 names — the constant so a rename is a visible
  diff, the literal because that string is the wire value
  ``lambda_function``'s second spelling and the dashboard's
  ``NOT_VERIFIED_LABELS`` are keyed on.
* The **failure reason inside the sentence** is taken from
  ``_failure_reason_and_stderr``, the shared classifier, rather than transcribed.
  A transcribed ``"AccessDenied: …"`` would keep passing after the two copies of
  R2.3's branch drifted apart, which is the failure mode the extraction removed.
  It also keeps this file correct across task 8.1, which is editing
  ``exception_summary``'s rendering of ``ErrorValue`` right now: the oracle moves
  with the producer, and the URI is not inside a quoted literal so no truncation
  can reach it.
* The **key set** is derived from the fixture's declared frame-refusal shape
  minus the two width keys the renderer owns, exactly as
  ``test_sample_read_failure_builders.py`` derives it, with the same anti-vacuity
  count assertion.

WHAT THIS FILE DOES NOT DUPLICATE
---------------------------------
* ``test_sample_read_failure_builders.py`` (task 3.2) calls both builders
  directly, on worked examples, and owns the readable failure message. This file
  owns the **quantifier** and drives the **composed public method**, so the claim
  is about what a caller can actually receive rather than about a builder's
  return.
* ``test_no_proxy_non_verification_prop.py`` (p42 task 8.3) owns marker
  *absence*, and task 7.1 amends its nine-outcome enumeration to ten. Nothing
  here asserts a proxy key.
* ``test_glue_result_mapping_prop.py`` owns the builders' mapping over the same
  ``ErrorName`` / ``ErrorValue`` / ``Traceback`` axes; those axes are modelled on
  its, deliberately, so a shape it found worth generating is generated here too.
* ``test_sample_unavailable_prop.py`` (task 6.3) owns Property 5 — the run whose
  *requested* sample could not be had, which carries no marker at all (C9,
  do-NOT 7). Every ``FrameSource`` here is a built sample.

Ledger notes
------------
* ``why`` on ``glue_session_runner.py`` returns the two p42 commits
  (``5c30733eb7``, ``ad78946449``). ``LGR-5c30733-1/-2/-3`` and
  ``LGR-ad78946-1/-2/-4/-5``; the ``ad78946`` records carry an explicit
  ``NOTE: BROAD`` over 60 files and are background rather than fences here.
  **No record governs ``run_test``'s routing** — task 6.1 found the same, and the
  routing decision is new rather than an amendment. The one record that does bite
  is ``LGR-ad78946-2`` (**template / ratified**): ``conversion_report`` is
  ``None`` on every Glue result, asserted below.
* ``decisions(concept="error-handling")`` returns one record,
  ``LGR-b2f79ef-13``, which notes in its own words that the concept is "an
  undocumented area, stated as a gap" — it governs ``schema_mapping``'s blanket
  ``except``, not this. ``decisions(concept="api")`` returns twelve, none about
  the result contract's routing. ``decisions(concept="testing")`` returns six, of
  which two apply to this file's *form*: ``LGR-b46a5bb-4`` (assert a fence on the
  module namespace, never on source text) and ``LGR-3e45e65-1`` (the unit layer
  pins its region in ``tests/unit/conftest.py``, so the ``os.environ`` lines
  below match the sibling files' shape and are not load-bearing).
* CodeGraph, before this file existed: ``sample_read_failure`` — 1 production
  caller (``run_test``), **no covering tests**; ``sample_read_failure_results``
  and ``sample_read_deadline_results`` — covered by
  ``test_sample_read_failure_builders.py`` only, never through ``run_test``;
  ``run_test`` — 3 callers, covered by three test files. This file adds
  ``sample_read_failure``'s first coverage and the two builders' first coverage
  *through the composed method*. ``glue_session_runner`` imports nothing from
  ``shared/``, so the shared-layer blast radius is nil.
"""

import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import botocore.client
import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings
from tests.fixtures.run_test_result_shapes import BASE_CONTRACT_KEYS, FRAME_REFUSAL

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from glue_session_runner import (
    _STATEMENT_DEADLINE_SECONDS,
    _STATEMENT_STATES_TERMINAL,
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    NOT_VERIFIED_REASON_SOURCE_READ_FAILED,
    STAGE_FRAME_BUILD,
    GlueSessionTestRunner,
    _failure_reason_and_stderr,
    sample_read_format,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

#: A schema the frame builder can type. Used on **both** the sample and the
#: synthetic control, so the only difference between them is ``frame_mode`` — on
#: a sample ``is_sample`` retires the absent-schema refusal arm (R9.4) and a
#: bare ``{}`` would work, but then the control would differ in two ways at once
#: and its failure would not be attributable.
BUILDABLE_SCHEMA: dict = {"properties": {"name": {"type": "string"}}}

TARGET_SCHEMA: dict = {"properties": {"name": {"type": "string"}}}

#: Irrelevant to this property: on every draw the frame-build stage fails, so the
#: transform statement is never submitted. Written as real PySpark rather than a
#: placeholder so a draw that somehow reached the transform stage would fail on
#: the assertion rather than on a syntax error nobody expected.
TRANSFORM = "df_transformed = df.withColumn('name', upper(col('name')))"


# ---------------------------------------------------------------------------
# do-NOT 1, measured rather than assumed
# ---------------------------------------------------------------------------


class GlueApiGuardViolation(BaseException):
    """A test in this file reached a real Glue API call. Stub the client."""


#: The session operations that bill, plus the polls a run would make against a
#: live session. Enumerated rather than "every Glue operation" so the guard's
#: failure message can name what was attempted.
GUARDED_GLUE_OPERATIONS = frozenset(
    {"CreateSession", "GetSession", "RunStatement", "GetStatement", "StopSession", "DeleteSession"}
)

_REAL_MAKE_API_CALL = botocore.client.BaseClient._make_api_call


@pytest.fixture(autouse=True)
def no_glue_api_calls(monkeypatch):
    """Make a real Glue session call unreachable from this file.

    The backstop behind :func:`_drive`'s ``boto3.client`` patch, for a client the
    construction seam cannot see. ``BaseException`` on purpose: ``run_test``'s
    third clause is ``except Exception`` and would swallow an ``Exception`` here,
    route it to ``create_session_failure_results``, and let the test pass while a
    real API call had been attempted — the exact failure mode the guard exists to
    stop. Same mechanism and same reasoning as ``tests/unit/conftest.py``'s
    ``no_cloudwatch_writes``.
    """

    def _guarded_make_api_call(self, operation_name, api_params):
        if operation_name in GUARDED_GLUE_OPERATIONS:
            raise GlueApiGuardViolation(
                f"a unit test reached a real Glue {operation_name} call — p44 do-NOT 1 forbids creating "
                f"an interactive session, which bills. Patch `glue_session_runner.boto3.client` before "
                f"constructing the runner rather than weakening this guard."
            )
        return _REAL_MAKE_API_CALL(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _guarded_make_api_call)


# ---------------------------------------------------------------------------
# The expected key set, derived from the declared contract
# ---------------------------------------------------------------------------

#: The two extra keys a harness non-verification carries, DERIVED: the frame
#: refusal's declared key set, minus the nine base keys, minus the two width keys
#: the renderer owns. Same derivation as
#: ``test_sample_read_failure_builders.py``, and load-bearing for the same
#: reason — a rename of either width constant leaves three keys here and fails
#: the count assertion below rather than quietly weakening every shape assertion.
HARNESS_MARKER_KEYS = (
    FRAME_REFUSAL.keys - BASE_CONTRACT_KEYS - {"not_verified_field_count", "not_verified_frame_ceiling"}
)

#: What the two builders must return: the nine base keys plus exactly those two.
EXPECTED_KEYS = BASE_CONTRACT_KEYS | HARNESS_MARKER_KEYS


def test_the_derived_marker_key_set_is_the_two_keys_it_claims_to_be():
    """Anti-vacuity for every shape assertion built on :data:`HARNESS_MARKER_KEYS`.

    If the derivation ever yielded the empty set — a width key renamed in the
    fixture, a base key added — the shape equality below would still hold while
    asserting less. This is the one place that cannot be derived, so it is the
    one place the count is written down.
    """
    assert len(HARNESS_MARKER_KEYS) == 2, HARNESS_MARKER_KEYS
    assert sorted(HARNESS_MARKER_KEYS) == ["not_verified_by_harness", "not_verified_reason"]


# ---------------------------------------------------------------------------
# The generator axes
# ---------------------------------------------------------------------------


class _Absent:
    """A field Glue omitted entirely, as distinct from one it sent as ``null``.

    The two take different routes through ``exception_summary``'s ``or ""``, so
    they are different observations and both are generated.
    ``test_glue_result_mapping_prop.py`` uses the same sentinel for the same
    reason.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


_FIELD_ABSENT = _Absent()
_STATUS_ABSENT = _Absent()

DOOR_STATEMENT = "statement"
DOOR_STATEMENT_DEADLINE = "statement_deadline"
DOOR_SESSION_NEVER_READY = "session_never_ready"

#: Every door onto :data:`NOT_VERIFIED_REASON_SOURCE_READ_FAILED`. The third is
#: the stated imprecision in ``run_test``'s deadline arm — covered, not objected
#: to. See the module docstring.
ALL_DOORS: tuple[str, ...] = (DOOR_STATEMENT, DOOR_STATEMENT_DEADLINE, DOOR_SESSION_NEVER_READY)

#: Terminal statement states on which the frame build FAILS. ``AVAILABLE`` fails
#: iff ``Output.Status`` is not ``ok``, which the status axis below supplies;
#: ``CANCELLED`` and ``ERROR`` fail by their state alone, which is
#: ``StatementOutcome.failed``'s second arm and the branch that reads no
#: ``Output`` at all.
#:
#: Non-terminal states are deliberately absent from this axis and reached through
#: :data:`DOOR_STATEMENT_DEADLINE` instead — see the module docstring for the
#: trap that costs.
_FAILING_TERMINAL_STATES: tuple[str, ...] = ("AVAILABLE", "CANCELLED", "ERROR")

#: ``Output.Status`` values that are NOT ``ok``, so an ``AVAILABLE`` statement
#: carrying one has failed. The casing near-misses are here on purpose: R2.3
#: classifies from this field by exact comparison, and ``"OK"`` reading as
#: success would be the defect.
_FAILING_OUTPUT_STATUSES: tuple[object, ...] = ("error", "ERROR", "OK", "Ok", "failed", "", _STATUS_ABSENT)

_ERROR_NAMES: tuple[object, ...] = (
    "AnalysisException",
    "Py4JJavaError",
    "AccessDeniedException",
    "IllegalArgumentException",
    "",
    "   ",
    None,
    _FIELD_ABSENT,
)

_ERROR_VALUES: tuple[object, ...] = (
    "An error occurred (403) when calling the HeadObject operation: Forbidden",
    "[PATH_NOT_FOUND] Path does not exist: s3://bucket/missing.csv",
    'java.lang.NumberFormatException: For input string: "N/A"',
    # Braces, because `_parse_results`' brace-depth scanner reads stdout and a
    # value that leaked into it would be the way to break the scanner.
    'Cannot parse {"a": {"b": 1}} as a struct',
    "",
    "\n\t ",
    None,
    _FIELD_ABSENT,
)

#: The header-free frame list Glue actually sends, plus the shapes the mapping
#: has to survive. ``header_bearing`` is NOT a shape Glue produces — it exists so
#: that "classification does not read the traceback" (R2.3) can be stated as a
#: difference that makes no difference.
_TRACEBACK_SHAPES: tuple[str, ...] = (
    "frames",
    "empty_list",
    "blank_frames",
    "bare_string",
    "absent",
    "long_frames",
    "header_bearing",
)

_HEADER = "Traceback (most recent call last"


def _traceback_for(shape: str) -> object:
    """The ``Output.Traceback`` value for *shape*, or :data:`_FIELD_ABSENT`."""
    if shape == "frames":
        return [
            '  File "<stdin>", line 3, in <module>\n',
            '  File "/opt/spark/python/pyspark/sql/readwriter.py", line 540, in csv\n',
            "AnalysisException: [PATH_NOT_FOUND]\n",
        ]
    if shape == "empty_list":
        return []
    if shape == "blank_frames":
        return ["", "   ", "\n"]
    if shape == "bare_string":
        return '  File "<stdin>", line 3, in <module>\n'
    if shape == "long_frames":
        return [f"  frame {index}: " + "x" * 400 + "\n" for index in range(20)]
    if shape == "header_bearing":
        return [f"{_HEADER}):\n", '  File "<stdin>", line 3, in <module>\n']
    return _FIELD_ABSENT


#: Object URIs whose format ``sample_read_format`` recognises, one per reader
#: family plus a compressed one. Several rather than one so "``error_details``
#: names the object" is asserted against distinct URIs and cannot pass on a
#: hardcoded string. Shaped as ``file_validation`` writes them —
#: ``original_s3_uri = f"s3://{bucket}/{key}"`` from the upload event's own
#: bucket (``file_validation/lambda_function.py:632``), which is ``sourceBucket``.
_SOURCE_URIS: tuple[str, ...] = (
    "s3://adi-source-files-123456789012-us-west-2/uploads/orders.csv",
    "s3://adi-source-files-123456789012-us-west-2/uploads/events.json",
    "s3://adi-source-files-123456789012-us-west-2/uploads/facts.parquet",
    "s3://adi-source-files-123456789012-us-west-2/uploads/legacy.avro",
    "s3://adi-source-files-123456789012-us-west-2/uploads/columnar.orc",
    "s3://adi-source-files-123456789012-us-west-2/uploads/orders.csv.gz",
    # A key carrying a space and a quote: `build_sample_frame_code` escapes the
    # URI through `json.dumps` so it cannot emit code reading a different object,
    # and `error_details` must still name it verbatim.
    's3://adi-source-files-123456789012-us-west-2/uploads/q1 "final".csv',
)

#: Session states from which ``READY`` is unreachable, for
#: :data:`DOOR_SESSION_NEVER_READY`. Read from the producer's own frozenset so a
#: state added there is drawn here without an edit.
_SESSION_BAD_STATES: tuple[str, ...] = tuple(sorted(glue_session_runner._SESSION_STATES_TERMINAL_BAD))


@dataclass(frozen=True)
class SampleReadFailure:
    """One Sample_Frame run whose frame build fails, described rather than run.

    ``door`` selects the route; every other field is a variant axis, inert on the
    routes that do not read it — which is what lets one flat strategy cover all
    three doors.
    """

    door: str
    source_uri: str
    #: The reached terminal state, for :data:`DOOR_STATEMENT`.
    state: str = "AVAILABLE"
    #: ``Output.Status``, for an ``AVAILABLE`` statement.
    output_status: object = "error"
    #: Glue's ``ErrorName`` / ``ErrorValue``. ``None`` is a reported ``null``;
    #: :data:`_FIELD_ABSENT` is an omitted key.
    error_name: object = "AnalysisException"
    error_value: object = "boom"
    traceback_shape: str = "frames"
    #: The state reached instead of ``READY``, for
    #: :data:`DOOR_SESSION_NEVER_READY`.
    session_state: str = "FAILED"


@st.composite
def _failures(draw: st.DrawFn) -> SampleReadFailure:
    """Generate any Sample_Frame frame-build failure, across all three doors."""
    return SampleReadFailure(
        door=draw(st.sampled_from(ALL_DOORS)),
        source_uri=draw(st.sampled_from(_SOURCE_URIS)),
        state=draw(st.sampled_from(_FAILING_TERMINAL_STATES)),
        output_status=draw(st.sampled_from(_FAILING_OUTPUT_STATUSES)),
        error_name=draw(st.sampled_from(_ERROR_NAMES)),
        error_value=draw(st.sampled_from(_ERROR_VALUES)),
        traceback_shape=draw(st.sampled_from(_TRACEBACK_SHAPES)),
        session_state=draw(st.sampled_from(_SESSION_BAD_STATES)),
    )


# ---------------------------------------------------------------------------
# Driving one failure through the composed public method
# ---------------------------------------------------------------------------


def _failing_statement(spec: SampleReadFailure) -> dict:
    """The terminal ``Statement`` dict Glue would return for *spec*.

    ``CANCELLED`` and ``ERROR`` carry **no ``Output`` at all**, which is what
    Glue does and is the branch of ``StatementOutcome.failed`` and of
    ``_failure_reason_and_stderr`` that reads the reached state as the evidence.
    Only ``AVAILABLE`` gets the error fields.
    """
    if spec.state != "AVAILABLE":
        return {"State": spec.state}

    output: dict[str, Any] = {"Data": {"TextPlain": ""}}
    if not isinstance(spec.output_status, _Absent):
        output["Status"] = spec.output_status
    if not isinstance(spec.error_name, _Absent):
        output["ErrorName"] = spec.error_name
    if not isinstance(spec.error_value, _Absent):
        output["ErrorValue"] = spec.error_value
    traceback = _traceback_for(spec.traceback_shape)
    if not isinstance(traceback, _Absent):
        output["Traceback"] = traceback
    return {"State": spec.state, "Output": output}


class _StatementPollClock:
    """A monotonic clock that advances only once a statement has been polled.

    The mechanism for :data:`DOOR_STATEMENT_DEADLINE`, and it has to be
    poll-keyed rather than call-counted. ``_await_session_ready``,
    ``_await_statement`` and ``_release_session`` each read ``time.monotonic``
    twice or more, so a clock that jumped on the *n*th call would put the jump in
    a different loop as soon as any of those three changed shape — and the wrong
    loop timing out is a different route with a different builder.

    Keyed on ``get_statement`` calls instead: zero polls means the session-ready
    loop and the statement loop both see time standing still, so the ready poll
    returns on its first ``READY`` and the statement loop enters. The first poll
    then jumps the clock past ``_STATEMENT_DEADLINE_SECONDS``, so the loop exits
    and ``_await_statement`` raises — exactly the shape R3.4 describes, reached
    through the real production loop rather than by patching the raise in.
    """

    #: Comfortably past every deadline in the module, so one poll is enough.
    _JUMP = 10.0 * _STATEMENT_DEADLINE_SECONDS

    def __init__(self) -> None:
        self.statement_polls = 0

    def __call__(self) -> float:
        return self._JUMP * self.statement_polls


def _client(spec: SampleReadFailure, clock: _StatementPollClock) -> MagicMock:
    """A Glue client double wired to produce *spec*'s door and nothing else."""
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    # Three ids, though only the first is ever consumed: the frame build fails,
    # so `_run_statements` breaks before the transform is submitted. A short list
    # would turn "the transform WAS submitted" into a `StopIteration` instead of
    # the assertion failure it should be.
    client.run_statement.side_effect = [{"Id": index + 1} for index in range(3)]

    if spec.door == DOOR_SESSION_NEVER_READY:
        client.get_session.return_value = {"Session": {"Status": spec.session_state}}
        return client

    if spec.door == DOOR_STATEMENT_DEADLINE:

        def _never_terminal(**_kwargs):
            clock.statement_polls += 1
            # `RUNNING` is deliberately not in `_STATEMENT_STATES_TERMINAL`, so
            # the poll loop keeps going and the deadline is what ends it.
            return {"Statement": {"State": "RUNNING"}}

        client.get_statement.side_effect = _never_terminal
        return client

    client.get_statement.side_effect = [{"Statement": _failing_statement(spec)}]
    return client


def _drive(spec: SampleReadFailure, *, frame_mode: str) -> tuple[dict[str, Any], MagicMock]:
    """Run *spec* through ``run_test`` and return ``(result, client_double)``.

    The **composed public method**, deliberately: Property 4 is a claim about
    what a caller receives, and a builder called directly cannot show that
    ``run_test``'s routing sends this observation to it rather than to
    ``statement_failure_results``.

    *frame_mode* is a parameter rather than fixed to ``sample`` so that
    :func:`test_the_same_observation_on_a_synthetic_frame_is_an_executed_failure`
    can drive the identical observation through the identical code with the only
    difference being the frame decision.
    """
    clock = _StatementPollClock()
    client = _client(spec, clock)

    with patch("glue_session_runner.boto3.client", return_value=client):
        runner = GlueSessionTestRunner(
            REGION,
            role_arn=ROLE_ARN,
            frame_mode=frame_mode,
            source_data_uri=spec.source_uri,
        )
    # Guard, not decoration: a `None` client takes `run_test`'s first pre-flight
    # branch and returns before any frame decision, which would make every
    # property in this file vacuously true.
    assert runner.client is client, "the runner must hold the stub, or nothing below tests a routed result"

    # `time` is replaced wholesale rather than `monotonic` patched on the real
    # module, so hypothesis' own timing is untouched. `glue_session_runner` reads
    # exactly two attributes off it (`monotonic`, `sleep`), verified by grep.
    fake_time = SimpleNamespace(monotonic=clock, sleep=lambda _seconds: None)
    with patch.object(glue_session_runner, "time", fake_time):
        result: dict[str, Any] = runner.run_test(TRANSFORM, BUILDABLE_SCHEMA, TARGET_SCHEMA)
    return result, client


def _expected_reason(spec: SampleReadFailure) -> str:
    """What the SHARED classifier says about *spec*, used as the oracle.

    R2.3 in the form the requirement states it: the classification comes from
    ``StatementOutcome.failed``'s vocabulary and ``exception_summary``'s fields,
    through the one branch ``statement_failure_results`` and
    ``sample_read_failure_results`` share. Derived rather than transcribed so
    this file cannot pass while the builder re-derives the reason by a second
    route — and so it stays correct across task 8.1's edit to
    ``exception_summary``.

    Only meaningful for :data:`DOOR_STATEMENT`; the two raise doors carry the
    exception's own text instead.
    """
    outcome = glue_session_runner.StatementOutcome(
        stage=STAGE_FRAME_BUILD,
        state=spec.state,
        output_status=(None if isinstance(spec.output_status, _Absent) else spec.output_status),
        stdout="",
        statement=_failing_statement(spec),
    )
    reason, _stderr = _failure_reason_and_stderr(outcome)
    return reason


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_failures())
def test_a_failed_source_read_is_a_harness_non_verification(spec: SampleReadFailure):
    """Property 4: the marker, the reason, and no transform verdict.

    **Validates: Requirements 2.1, 2.2, 2.3, 3.4, 4.1**

    The whole of R2.1 and R2.2 over the failure space. ``executed`` false with no
    ``exit_code`` is the discrimination the requirement turns on: route the same
    observation through ``statement_failure_results`` — which is where
    ``results_from_outcomes`` sends it, and where a synthetic run still goes —
    and it claims ``executed`` true with ``exit_code`` 1 for a transform that
    never ran.
    """
    result, _client_double = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)

    # Presence before value, so a route that fell through to
    # `statement_failure_results` reports the requirement it broke instead of a
    # bare `KeyError` from the line below.
    assert "not_verified_by_harness" in result, (
        "the result carries no harness marker, so this observation reached a builder that reports a "
        "transform verdict — R2.1's defect exactly"
    )
    assert result["not_verified_by_harness"] is True
    assert result["not_verified_reason"] == NOT_VERIFIED_REASON_SOURCE_READ_FAILED
    assert result["executed"] is False
    assert result["exit_code"] is None
    assert result["execution_successful"] is False
    # `LGR-ad78946-2` (template / ratified): `None` on every Glue result, because
    # the Glue runner runs no conversion by design.
    assert result["conversion_report"] is None


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_failures())
def test_the_reason_is_the_wire_value_the_dashboard_keys_on(spec: SampleReadFailure):
    """The identifier itself, over every door.

    **Validates: Requirement 2.1**

    Asserted against the literal as well as the constant because this string
    crosses three more surfaces — ``lambda_function``'s second spelling, its
    ``NOT_VERIFIED_REASON_LABELS`` entry and the dashboard's
    ``NOT_VERIFIED_LABELS`` — all keyed on these exact bytes. The constant alone
    would let a rename pass here and break there.
    """
    result, _client_double = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)

    assert result["not_verified_reason"] == "source_read_failed"


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_failures())
def test_error_details_names_the_source_read_and_the_object(spec: SampleReadFailure):
    """R2.1: the failing operation is the source read, and the object is named.

    **Validates: Requirements 2.1, 4.1**

    The URI is what makes the sentence actionable — the reviewer's next step is
    to check that object and the session role's reach to it, which they cannot do
    from a message saying only that the source could not be read. Asserted
    against the drawn URI so it cannot pass on a hardcoded string.
    """
    result, _client_double = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)

    detail = result["error_details"]
    assert detail, "a harness non-verification with no sentence tells a reviewer nothing"
    assert "source object could not be read" in detail
    assert spec.source_uri in detail


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_failures())
def test_error_details_never_claims_the_frame_build_stage_failed(spec: SampleReadFailure):
    """The misattribution R2.1 exists to remove, asserted as an absence.

    **Validates: Requirements 2.1, 2.2**

    ``STAGE_FRAME_BUILD`` is ``"test data generation"``, so the builder these
    observations reach *without* task 6.1's route renders "test data generation
    failed: …" — a reported failure of a stage that generated nothing, for a
    transform that never ran. Asserted against the constant rather than the
    literal, so a relabelled stage cannot slip the string back in under a new
    name.

    **The two-tier assertion is a finding, not a weakening, and it is worth
    stating because ``test_sample_read_failure_builders.py`` asserts the stronger
    form.** That file's deadline case hands the builder a hand-written
    ``GlueSessionError``; the real one, out of ``_await_statement``
    (``glue_session_runner.py:1301-1304``, read end to end), is
    ``"Glue statement 1 for stage 'test data generation' did not reach a terminal
    state within 120s (last state RUNNING)"`` — so on the deadline door the stage
    label **does** reach ``error_details``, inside the exception's own text.

    That is not the defect R2.1 names. The sentence still reads "the run's source
    object could not be read within the statement deadline: <uri> — …", so the
    failing operation named is the read and the stage label is diagnostic detail
    about *which statement* was waiting. What must never appear is the claim that
    the stage **failed**, which is the phrase
    ``statement_failure_results`` produces. So:

    * on every door — the ``"{stage} failed"`` phrase is absent, and the sentence
      does not open with the stage label;
    * on the statement door, where the reason comes from the shared classifier and
      not from an exception — the stage label is absent entirely.
    """
    result, _client_double = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)
    detail = result["error_details"]

    assert f"{STAGE_FRAME_BUILD} failed" not in detail
    assert not detail.startswith(STAGE_FRAME_BUILD)

    if spec.door == DOOR_STATEMENT:
        assert STAGE_FRAME_BUILD not in detail


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_failures())
def test_the_reason_comes_from_the_shared_classifier(spec: SampleReadFailure):
    """R2.3: classification from ``state`` and ``Output.Status``, and from nothing else.

    **Validates: Requirement 2.3**

    The expectation is ``_failure_reason_and_stderr``'s own return value, so this
    cannot pass while the builder re-derives the reason from the same fields by a
    second route — the defect the extraction in task 3.1 removed. Restricted to
    :data:`DOOR_STATEMENT`, which is the only door with a ``StatementOutcome`` to
    classify; the two raise doors carry the exception's text and are asserted
    separately.
    """
    if spec.door != DOOR_STATEMENT:
        return

    result, _client_double = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)

    reason = _expected_reason(spec)
    assert reason, "the classifier must always name something, or the sentence trails off"
    assert result["error_details"].endswith(f"— {reason}")


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_failures())
def test_the_result_carries_the_harness_siblings_shape_and_nothing_more(spec: SampleReadFailure):
    """The nine base contract keys plus exactly the two markers.

    **Validates: Requirements 2.1, 2.2**

    ``not_verified_field_count`` / ``not_verified_frame_ceiling`` are absent for
    the reason ``session_capacity_results`` omits them: there is no width problem
    in a source read, so carrying them as zeroes would render "the source schema
    declares 0 field(s) against a test-frame width ceiling of 0" — a false claim
    about the schema, made while removing a false one about the transform.
    """
    result, _client_double = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)

    assert set(result) == EXPECTED_KEYS


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_failures())
def test_the_verification_session_is_released(spec: SampleReadFailure):
    """R2.4: stopped and deleted, on every one of the three doors.

    **Validates: Requirement 2.4**

    Not a new guarantee and deliberately not new logic: ``run_session``'s
    ``finally`` runs **before** any ``except`` clause in ``run_test``, so the
    session is already gone by the time either builder is called. That ordering
    is what this asserts — the release is observable on the raise doors too,
    where no ``FrameGuardedRun`` ever existed.

    ``stop_session`` and ``delete_session`` are asserted **independently**,
    because ``_release_session`` attempts them independently: a failing stop must
    not skip the delete, and the delete is the call that ends the billing.
    """
    _result, client = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)

    client.create_session.assert_called_once()
    client.stop_session.assert_called_once()
    client.delete_session.assert_called_once()
    assert client.delete_session.call_args.kwargs["Id"] == client.stop_session.call_args.kwargs["Id"]


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_failures())
def test_rewriting_the_traceback_changes_nothing(spec: SampleReadFailure):
    """R2.3 as invariance: the traceback is not read to classify anything.

    **Validates: Requirement 2.3**

    ``StatementOutcome.failed`` reads ``state`` and ``Output.Status`` only, and
    ``sample_read_failure_results`` discards the stderr half of the classifier's
    return, so the whole result dict must be **equal** across every traceback
    shape — including one carrying the Python header Glue never sends, which is
    the shape a traceback-sniffing classifier would misread as a success.

    Equality of the whole dict rather than of the two markers: a traceback that
    leaked into ``error_details`` or ``stderr`` would still satisfy the narrower
    claim while putting session text somewhere it does not belong.

    **Restricted to :data:`DOOR_STATEMENT`, and both halves of the reason matter.**
    A traceback only exists on a statement Glue reported an ``Output`` for, so the
    two raise doors have nothing to be invariant *about* — ``_failing_statement``
    is never consulted on them. They are also not byte-comparable across drives:
    ``_create_session`` mints ``f"adi-verify-{uuid4().hex[:12]}"`` and
    ``_await_session_ready``'s message embeds it, so two drives of one spec differ
    in the session id alone. Observed rather than reasoned about — an earlier draft
    of this test quantified over all three doors and failed on exactly that
    difference.
    """
    if spec.door != DOOR_STATEMENT:
        return

    baseline, _first = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)

    for shape in _TRACEBACK_SHAPES:
        rewritten, _client_double = _drive(
            SampleReadFailure(**{**spec.__dict__, "traceback_shape": shape}),
            frame_mode=FRAME_MODE_SAMPLE,
        )
        assert rewritten == baseline, f"the {shape!r} traceback changed the result; classification read it"


# ---------------------------------------------------------------------------
# Non-vacuity: the discrimination, the doors, and the premise
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spec=_failures())
def test_the_same_observation_on_a_synthetic_frame_is_an_executed_failure(spec: SampleReadFailure):
    """The load-bearing discrimination, and the reason Property 4 is not vacuous.

    **Validates: Requirements 2.1, 2.2**

    Identical Glue observation, identical code, one difference: ``frame_mode``
    left at its default. For a Synthetic_Frame the frame-build stage really *was*
    generating test data, so "test data generation failed: …" with ``executed``
    true is the accurate sentence and must stay reachable — task 6.1 routes only
    the sample path away from it.

    Without this, a runner that attached the harness marker to every failure
    would satisfy every assertion above. With it, the marker's presence is a
    discrimination rather than a constant.

    The two raise doors are excluded because they do not discriminate on the
    frame: a ``GlueSessionError`` on a synthetic run reaches
    ``lifecycle_failure_results``, which is also ``executed`` false — the
    difference there is the marker and the sentence, asserted below.
    """
    if spec.door != DOOR_STATEMENT:
        return

    result, _client_double = _drive(spec, frame_mode=FRAME_MODE_SYNTHETIC)

    assert result["executed"] is True
    assert result["exit_code"] == 1
    assert result["execution_successful"] is False
    assert "not_verified_by_harness" not in result
    assert "not_verified_reason" not in result
    assert result["error_details"].startswith(f"{STAGE_FRAME_BUILD} failed: ")


#: The doors that reach ``run_test``'s ``except GlueSessionError`` arm — derived
#: as the complement of the statement door rather than listed, so a fourth door
#: added to :data:`ALL_DOORS` is covered without an edit here.
RAISE_DOORS: tuple[str, ...] = tuple(door for door in ALL_DOORS if door != DOOR_STATEMENT)


@pytest.mark.parametrize("door", RAISE_DOORS)
def test_the_deadline_doors_discriminate_on_the_frame_too(door: str):
    """A synthetic run's raise carries no marker and does not name the source read.

    **Validates: Requirements 3.4, 4.1**

    The other half of the discrimination, for the two doors the test above
    excludes. ``lifecycle_failure_results`` is ``not_executed_results``, so
    ``executed`` is already false there — what R3.4 adds on a Sample_Frame run is
    the harness marker and a sentence naming the source read. If that were the
    whole of it for *every* run, the marker would say nothing about the frame.
    """
    spec = SampleReadFailure(door=door, source_uri=_SOURCE_URIS[0])

    synthetic, _synthetic_client = _drive(spec, frame_mode=FRAME_MODE_SYNTHETIC)
    assert "not_verified_by_harness" not in synthetic
    assert spec.source_uri not in synthetic["error_details"]
    assert synthetic["executed"] is False

    sample, _sample_client = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)
    assert sample["not_verified_reason"] == NOT_VERIFIED_REASON_SOURCE_READ_FAILED
    assert spec.source_uri in sample["error_details"]


@pytest.mark.parametrize("door", ALL_DOORS)
def test_every_door_is_reachable_and_lands_where_it_names(door: str):
    """Each door takes its own route, so no draw silently exercises another.

    **Validates: Requirements 2.1, 3.4**

    ``test_no_proxy_non_verification_prop.py`` records what this costs when it is
    missing: three non-terminal statement states in an earlier draft of that file
    stalled ``_await_statement``'s poll loop, arrived at a different builder, and
    the property passed while testing nothing it named. The route is identified
    from the *stub's call record*, which is evidence independent of the result
    dict every other test in this file reads.
    """
    spec = SampleReadFailure(door=door, source_uri=_SOURCE_URIS[0])
    result, client = _drive(spec, frame_mode=FRAME_MODE_SAMPLE)

    assert result["not_verified_reason"] == NOT_VERIFIED_REASON_SOURCE_READ_FAILED

    if door == DOOR_SESSION_NEVER_READY:
        # The session never became usable, so nothing was ever submitted.
        client.run_statement.assert_not_called()
        client.get_statement.assert_not_called()
        return

    # Exactly ONE statement: the frame build. The transform is never submitted
    # after a failed stage, which is why `executed` is false rather than a
    # convention.
    client.run_statement.assert_called_once()
    assert client.get_statement.call_count >= 1

    if door == DOOR_STATEMENT_DEADLINE:
        # The poll loop ended on the DEADLINE, having seen no terminal state: the
        # stub answers `RUNNING` forever, so `_await_statement` can only have left
        # its loop by exhausting the budget. Read off the stub's own return rather
        # than off the result, so the evidence is independent of the mapping.
        polled = client.get_statement.side_effect(SessionId="created", Id=1)
        assert polled["Statement"]["State"] not in _STATEMENT_STATES_TERMINAL, (
            "the deadline door's stub must never answer a terminal state, or the draw takes the statement door instead"
        )


@pytest.mark.parametrize("state", sorted(_STATEMENT_STATES_TERMINAL))
def test_the_statement_state_axis_is_terminal_and_the_failing_ones_are_covered(state: str):
    """The axis is drawn from the producer's own terminal set, minus success.

    Two ways :data:`_FAILING_TERMINAL_STATES` could rot silently, both closed:
    a state added to ``_STATEMENT_STATES_TERMINAL`` and never drawn here, and a
    non-terminal state creeping into the axis and stalling the poll loop.
    """
    assert state in _FAILING_TERMINAL_STATES, (
        f"{state!r} became terminal in the producer but is not drawn on the statement door — "
        "add it to _FAILING_TERMINAL_STATES, or record why it cannot fail"
    )
    assert set(_FAILING_TERMINAL_STATES) <= set(_STATEMENT_STATES_TERMINAL), (
        "a non-terminal state in the axis stalls `_await_statement` and silently moves the draw onto the deadline door"
    )


@pytest.mark.parametrize("status", _FAILING_OUTPUT_STATUSES)
def test_no_generated_output_status_reads_as_success(status: object):
    """The ``AVAILABLE`` arm's premise: every drawn status is a failure.

    ``StatementOutcome.failed`` compares ``Output.Status`` to ``"ok"`` exactly, so
    a draw of ``"ok"`` would make an ``AVAILABLE`` statement a *success* and the
    frame-build-failed antecedent false — the draw would then exercise the
    success path while claiming to test a failed read.
    """
    resolved = None if isinstance(status, _Absent) else status
    assert resolved != glue_session_runner._OUTPUT_STATUS_OK


@pytest.mark.parametrize("shape", _TRACEBACK_SHAPES)
def test_only_the_invariance_shape_carries_a_python_header(shape: str):
    """Glue sends header-free frame lists; one shape breaks that on purpose.

    R2.3's trap stated as a property of the generator: ``Output.Traceback`` is a
    list of frame strings with no ``Traceback (most recent call last`` line, so a
    classifier sniffing for one would find nothing in a real Spark exception and
    pass every one of them as a success. ``header_bearing`` exists so the
    invariance property can state that as a difference making no difference — and
    every other shape must NOT carry it, or the invariance claim is untested in
    the direction that matters.
    """
    traceback = _traceback_for(shape)
    rendered = "" if isinstance(traceback, _Absent) else "".join(traceback)

    if shape == "header_bearing":
        assert _HEADER in rendered
    else:
        assert _HEADER not in rendered


@pytest.mark.parametrize("uri", _SOURCE_URIS)
def test_every_generated_uri_actually_resolves_a_sample(uri: str):
    """The antecedent, checked against the shipped resolver rather than assumed.

    A URI whose format ``sample_read_format`` does not recognise falls back to a
    Synthetic_Frame with an ``unavailable_reason``, so ``is_sample`` is false and
    the whole property becomes vacuous for that draw — it would pass while
    exercising task 6.3's territory instead. Checked through production code so a
    resolver change that stopped recognising one of these extensions fails here,
    loudly, rather than silently emptying the generator.
    """
    assert sample_read_format(uri) is not None

    runner_frame_source = glue_session_runner.FrameSource(
        mode=FRAME_MODE_SAMPLE,
        requested_mode=FRAME_MODE_SAMPLE,
        source_uri=uri,
        read_format=sample_read_format(uri),
    )
    assert runner_frame_source.is_sample is True


def test_the_runner_resolves_a_sample_frame_for_these_arguments():
    """The premise of every drive, asserted once through the shipped resolver.

    ``run_test``'s sample route is gated on ``FrameSource.is_sample``. If the
    constructor arguments this file uses stopped resolving a Sample_Frame — a
    changed keyword name, a resolver narrowing — every property above would pass
    by testing the synthetic path's routing instead. This is the one assertion
    that makes the antecedent observable.
    """
    with patch("glue_session_runner.boto3.client", return_value=MagicMock()):
        runner = GlueSessionTestRunner(
            REGION,
            role_arn=ROLE_ARN,
            frame_mode=FRAME_MODE_SAMPLE,
            source_data_uri=_SOURCE_URIS[0],
        )

    frame_source = runner.resolve_frame_source()

    assert frame_source.is_sample is True
    assert frame_source.unavailable_reason is None
    # `resolve_frame_source` touches no AWS API (R1.5), so this call spent
    # nothing — asserted rather than stated.
    assert runner.client.create_session.call_count == 0


def test_a_real_glue_call_would_be_caught_by_the_guard():
    """The guard itself, so do-NOT 1 is measured rather than asserted in prose.

    Without this, :func:`no_glue_api_calls` could be silently inert — a renamed
    ``botocore`` seam, a monkeypatch that did not take — and every claim that no
    session was created would rest on the ``boto3.client`` patch alone. Here the
    guard is provoked on purpose, through the real ``_make_api_call`` seam.
    """
    real_client = botocore.client.BaseClient

    with pytest.raises(GlueApiGuardViolation):
        real_client._make_api_call(MagicMock(), "CreateSession", {})
