# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Glue interactive-session runner for verifying AI-generated transforms.

The second verification executor. Where
:mod:`code_interpreter_runner` translates the transform to pandas and executes
the translation in AgentCore Code Interpreter, this module executes the
transform **as written** on the Spark runtime the generated Glue job will
actually run on.

Beside ``code_interpreter_runner.py`` rather than in ``shared/`` (design §4.1):
``run_test``'s only caller is in this Lambda directory, and ``shared/`` is
bundled into the Lambda layer for cross-Lambda reuse that does not exist here.

**Scope of this module as it stands.** Spec tasks 3.1 (the *session lifecycle*)
3.3 (the *result mapping*), 3.6 (the *capacity refusal*), 5.1 (the *synthetic
frame*), 5.3 (the *pre-session frame refusal*), 5.5 (the *frame-mode
dispatch* and the S3 ``sample`` frame), 6.1 (the *marker protocol*) and 8.1
(the public :meth:`GlueSessionTestRunner.run_test`). The lifecycle is complete and
exercised, every observation it can produce has a place in the
Test_Result_Contract — including the one ``CreateSession`` refusal that is a
non-verification rather than a failure
(:func:`create_session_failure_results`) — the frame the transform runs against
can be generated in either mode, a schema no honest frame can be built from
is refused **before** a session is created (:func:`frame_refusal_results`,
R7.3), and both stdout markers the shared parser reads are emitted — the
``GENERATED:`` line on the frame-build statement (:func:`frame_build_statement`)
and the nine-key ``VALIDATION:`` payload computed in one Spark ``select``
(:func:`build_validation_code`, R10.6). The public entry point
:meth:`GlueSessionTestRunner.run_test` composes all of it, in the pre-flight
order ``CodeInterpreterTestRunner.run_test`` uses today, with the conversion
step **removed rather than replaced** (R1.4, R3.2). Task 9.3 has since supplied
the real ``source_data_uri`` from the TestCode payload
(``lambda_function.py``'s ``glue`` branch), through the constructor, so R9.6
holds without ``run_test``'s parameter list moving.

**``frame_mode`` still has no caller, and that is the remaining gap.** Nothing
selects :data:`FRAME_MODE_SAMPLE`, so :meth:`resolve_frame_source` returns on its
first branch and the wired URI is not read on any live path yet. A sample is
opt-in by construction (design §4.4) and choosing it is not one of this spec's
tasks; until something does, every live run gets a synthetic frame — which is a
fully verified run against a generated frame, not a degraded one.

:meth:`GlueSessionTestRunner.run_session` is the seam those tasks build on: it
owns everything between ``CreateSession`` and ``DeleteSession`` and returns one
:class:`StatementOutcome` per stage, so a failure is attributable to a stage.
:meth:`GlueSessionTestRunner.run_guarded_session` wraps it with the one
pre-flight step that must precede creation, and is the seam that makes R7.3
checkable rather than a comment. The ``*_results`` functions at the bottom of
the module are the other half of that seam: they turn one of those observations
into the dict every downstream reader already understands.

Constructed on one path only: the ``glue`` branch of the executor dispatch in
``lambda_function.py`` (task 9.1). **That branch is now the live path.** Both
stacks publish ``VERIFICATION_EXECUTOR: 'glue'`` — a deliberate reversal of the
R11.2 hold, which pinned ``pandas`` while the concurrent-session ceiling was
unmeasured. The ceiling is still unmeasured (task 1.3's probe was written and
never run, because running it bills), so that is an accepted deviation from R11.2
rather than a satisfaction of it; the reasoning is recorded at both CDK sites.
The practical consequence for this class: it is no longer reached only when an
operator opts in, it is reached on every run, so its refusal paths — above all
the capacity refusal that maps to
:data:`NOT_VERIFIED_REASON_SESSION_CAPACITY` — are now ordinary operating
behaviour rather than edge cases.

**BLOCKING POLL LOOPS ARE LEGITIMATE HERE — see the comment at**
:meth:`_await_statement`. ``LGR-b46a5bb-1`` (standing invariant) does not
apply on this path; the reasoning is recorded there in full rather than
summarised, because the reader who needs it is the reviewer reading the loop.
"""

import json
import logging
import random
import textwrap
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import boto3
from code_interpreter_runner import (
    _MAX_TEST_FRAME_COLUMNS,
    _TRACEBACK_MARKER,
    NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA,
    CodeInterpreterTestRunner,
    declared_source_fields,
)

# p55 R4.1: the Source_Delimiter for the sample read, so the verification frame
# is separated the way the generated Glue job separates it. Guarded, mirroring
# `lambda_function.py:44-45` in this same package: `/opt/python` carries
# `shared/` in the Lambda and in the AgentCore container, while a test that
# imports this module directly may not have put `shared/` on `sys.path`. NOT a
# fourth local copy of the delimiter table — `LGR-eee8ac8-5`'s reason applies
# ("a second definition in either Lambda would disable the check silently by
# never matching"), and `_COMPRESSION_EXTENSIONS` below is already one
# deliberate duplication too many to add to.
try:
    from format_delimiters import delimiter_for_key
except ImportError:  # pragma: no cover — exercised only off the layer path
    import os as _os
    import sys as _sys

    _sys.path.append(_os.path.join(_os.path.dirname(__file__), "..", "..", "shared"))
    from format_delimiters import delimiter_for_key

# Checker-only, the shape `create_glue_pipeline/job_builders.py:33-39` and
# `create_infrastructure/lambda_function.py:11-13` already use for their own
# Glue calls — the stubs are a dev dependency and must not be imported at
# runtime in a Lambda package.
if TYPE_CHECKING:
    from mypy_boto3_glue.client import GlueClient

logger = logging.getLogger(__name__)

# ── Session shape ─────────────────────────────────────────────────────
#
# Matches the measured probe session in design C10 exactly, so the cost and
# timing numbers recorded there describe the sessions this module creates:
# Glue 5.0, Spark 3.5.4-amzn-0, G.1X, 2 workers, MaxCapacity 2.0 —
# 220.388 DPU-seconds = $0.0269 for one create/measure/stop/delete cycle.
_GLUE_VERSION = "5.0"
#: Typed as the Literal the Glue API accepts rather than as `str`, so a typo
#: here is a checker error rather than a runtime `ValidationException`.
_WORKER_TYPE: Literal["G.1X"] = "G.1X"
_NUMBER_OF_WORKERS = 2

# design §4.3 — a DELIBERATE choice, not a default. The Glue default is 15
# minutes; at 2 DPU × $0.44/DPU-hour a session leaked at that setting bills
# ~$0.22 whether or not anything uses it, against ~$0.015 at one minute.
#
# This is the only mechanism that stops a hung verification from also becoming
# a billing incident. Design C3: TestCode is a
# `lambda:invoke.waitForTaskToken` state, and `LGR-60c4985-3` records that
# such states are deliberately excluded from resume caching precisely because
# a short-circuit would return before the token is sent and the state would
# hang for its FULL `TimeoutSeconds` — 1200 s. So a session this module fails
# to release does not get cleaned up by a fast-failing step; the idle timeout
# is what bounds it. Do not raise this to "give a session room" — the room is
# in the deadlines below, which are enforced while the runner is still alive.
_IDLE_TIMEOUT_MINUTES = 1

# `CreateSession` requires a caller-supplied Id. Prefix + random suffix rather
# than a timestamp: two verifications can start in the same second (a
# regeneration loop, or shadow mode), and a collision is an
# `AlreadyExistsException` on a path that has already been paid for.
_SESSION_ID_PREFIX = "adi-verify"

_SESSION_TAGS = {"Project": "AgenticDataIngestion", "Purpose": "transform-verification"}

# ── Time budgets ──────────────────────────────────────────────────────
#
# Explicit, and sized to FIT rather than to the enclosing step's timeout.
# Design C3 states the rule outright: do NOT treat TestCode's 1200 s as
# headroom. The same step already runs a Strands review agent with a reparse
# retry (`lambda_function.py:1720-1751`) — up to two model invocations — BEFORE
# the executor starts.
#
# Worst case here, so it can be checked rather than trusted:
#
#     ready            180
#   + 3 × statement    360   (frame build, transform, validation)
#   + release          120
#   = 660 s
#
# leaving ~540 s of the 1200 for the review agent and the handler around it.
# Measured costs for comparison (C10): 42-46 s to READY, 8 s first statement,
# 17 s first Spark action, 2 s per statement once warm. Every budget below is
# at least 4× its measured value; they exist to bound a hang, not to pace a
# healthy run.
_SESSION_READY_DEADLINE_SECONDS = 180
_STATEMENT_DEADLINE_SECONDS = 120
_RELEASE_DEADLINE_SECONDS = 120

_SESSION_POLL_SECONDS = 5
_STATEMENT_POLL_SECONDS = 2
_RELEASE_POLL_SECONDS = 5

# ── Glue session and statement states ─────────────────────────────────
#
# `GetSession` → `Session.Status`; `GetStatement` → `Statement.State`.
# Enumerated rather than inlined so a poll loop cannot spin forever on a state
# nobody thought about: anything outside the terminal sets keeps polling until
# its deadline, and the deadline is what ends it.
_SESSION_STATE_READY = "READY"
#: Session states from which READY is unreachable. `PROVISIONING` is NOT here —
#: it is the normal pre-READY state and the one the ready poll exists for.
_SESSION_STATES_TERMINAL_BAD = frozenset({"FAILED", "STOPPED", "STOPPING", "TIMEOUT"})

_STATEMENT_STATE_AVAILABLE = "AVAILABLE"
#: A statement is done when it reaches one of these. `WAITING`, `RUNNING` and
#: `CANCELLING` are transitional.
_STATEMENT_STATES_TERMINAL = frozenset({"AVAILABLE", "CANCELLED", "ERROR"})

#: `Output.Status` on an `AVAILABLE` statement. R5.5: failure is classified
#: from THIS, never by sniffing the traceback for a Python header — Glue's
#: `Traceback` is a list of frame strings and has no header line, so the
#: `_has_traceback` sniff `code_interpreter_runner.py:464-467` uses would pass
#: every Spark exception as a success. That is the named trap in task 3.3.
_OUTPUT_STATUS_OK = "ok"

# Stage labels. Parity with `_run_lifecycle` (`code_interpreter_runner.py:351-376`),
# which runs frame build, transform and validation as three separate executions
# so a failure can be attributed to a stage — `_failed_execution_results(stage, …)`
# consumes the label, and the report and `test_honest_aggregation` already read
# it. Three statements rather than one collapsed statement costs 2 s each once
# the executor is warm (C10); the 17 s first-action cost is paid once either way.
STAGE_FRAME_BUILD = "test data generation"
STAGE_TRANSFORM = "transform execution"
STAGE_VALIDATION = "output validation"

# ── Frame modes ───────────────────────────────────────────────────────
#
# Design §4.4's two modes, spelled as constants because `frame_mode` is a
# constructor argument and a typo in a caller must not read as a third mode.
# `resolve_frame_source` treats anything outside this pair as `synthetic` with a
# WARNING — the `USE_AGENTCORE_RUNTIME` / R2.3 posture applied one level down:
# an unrecognised value falls through to the EXISTING behaviour, and says so.

#: A frame generated from the declared source schema. The default, and what
#: shadow mode compares against (R10.3 — see the synthetic section for why
#: parity, not merely determinism, is the requirement).
FRAME_MODE_SYNTHETIC = "synthetic"

#: A frame read from a bounded sample of the run's real source object (R9.1).
#: Opt-in, and only reachable for an ``s3://`` URI — see
#: :func:`is_s3_source_uri` and :meth:`GlueSessionTestRunner.resolve_frame_source`.
FRAME_MODE_SAMPLE = "sample"

# ── Why a requested sample was not built ──────────────────────────────
#
# R9.2 asks for a record that distinguishes "no sample was requested" from "a
# sample was requested and the source has none". The first is
# `FrameSource.unavailable_reason is None`; the two below are the second.
#
# These are NOT `not_verified_reason` values and must not become them. A
# fallback to `synthetic` is a fully verified run against a generated frame —
# exactly what `frame_mode="synthetic"` produces on purpose — so attaching a
# non-verification marker to it would report nothing-was-verified about a run
# that verified something. What the absence of a sample DOES change is narrower
# and is handled where it belongs: the no-source-schema arm of
# :func:`frame_refusal_results` stays armed (R9.5).

#: The URI is not an ``s3://`` object URI, so there is nothing this runner may
#: read. The ordinary case, not an error: ``schema_extract`` writes
#: ``f"jdbc://pipeline/{pipeline_id}"`` as its fallback
#: (``schema_extract/lambda_function.py:1735``), and a JDBC-sourced pipeline's
#: URI is a ``jdbc://`` one by construction (design C8).
SAMPLE_UNAVAILABLE_NOT_S3_SOURCE = "source_uri_not_s3"

#: The URI is an ``s3://`` object URI, but nothing in it says how to read it —
#: see :func:`sample_read_format` for the observed instance (a folder-prefix
#: URI, which carries no extension) and for why guessing is worse than falling
#: back.
SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT = "source_format_unrecognised"


class GlueSessionError(Exception):
    """A session could not be brought to a usable state.

    Raised by the lifecycle for its own failures — a session that never
    reached ``READY``, a statement that never reached a terminal state within
    its deadline. A *statement* that ran and failed is NOT this: it is a
    :class:`StatementOutcome` with ``failed`` true, because that is an
    observation about the transform rather than about the harness.
    """


class FrameBuildError(Exception):
    """No honest test frame could be generated from the declared schema.

    Raised by :func:`build_synthetic_frame_code` — **host-side, before any
    session exists**, so nothing is spent discovering it. Deliberately *not* a
    :class:`GlueSessionError`: no session was involved, and a caller that
    catches ``GlueSessionError`` to mean "the harness could not get Spark
    working" would mislabel this.

    The one thing raises it today is a declared field type outside the
    enumerated vocabulary (:func:`spark_type_for`). Failing is the point: see
    that function for why a silent fall-through to ``StringType`` is worse than
    a refusal.

    Mapping this onto the Test_Result_Contract belongs to ``run_test``
    (task 8.1), which owns the pre-flight order. R7.3's shape — a harness
    non-verification with no session created — is the destination it fits, and
    it is the *same* destination :func:`frame_refusal_results` already reaches:
    both say "the harness could not build a frame, so nothing was verified", and
    both are decided host-side. The two therefore share a route in the one
    respect R7.3 actually constrains —
    :meth:`GlueSessionTestRunner.run_guarded_session` reaches both **before**
    ``CreateSession`` — while the mapping stays 8.1's and is deliberately not
    anticipated here. A caller that maps this exception to something other than
    a harness non-verification would be reporting a harness limitation as a
    transform failure.
    """


@dataclass(frozen=True)
class StatementOutcome:
    """What one Glue statement was observed to do.

    The lifecycle's output, and the input task 3.3 maps onto the
    Test_Result_Contract. Deliberately an *observation* and not a result: it
    carries the raw ``Statement`` dict so the mapping owns every field
    extraction (``ErrorName``, ``ErrorValue``, ``Traceback``) rather than
    having it half-done here.

    ``stage`` is one of the ``STAGE_*`` labels. ``state`` is the terminal Glue
    statement state; ``output_status`` is ``Output.Status`` where the statement
    reached ``AVAILABLE`` and ``None`` otherwise.
    """

    stage: str
    state: str
    output_status: str | None
    stdout: str
    #: A read-only `Mapping` rather than a `dict`: an observation that a later
    #: stage could mutate is not an observation. It is also the type the Glue
    #: `StatementTypeDef` satisfies, so the mapping in task 3.3 needs no cast.
    statement: Mapping[str, Any] = field(repr=False)

    @property
    def failed(self) -> bool:
        """Whether this statement's outcome stops the lifecycle.

        The **sequencing** predicate, and only that — a false here means the
        next stage may run, not that the run passed. Building the result dict
        is task 3.3's job.

        Classified from the reached state and ``Output.Status`` per R5.5, never
        from traceback text. A statement that reached ``CANCELLED`` or
        ``ERROR`` failed by its state; one that reached ``AVAILABLE`` failed
        iff Glue reported ``Output.Status`` as anything other than ``ok``.
        """
        if self.state != _STATEMENT_STATE_AVAILABLE:
            return True
        return self.output_status != _OUTPUT_STATUS_OK


@dataclass(frozen=True)
class FrameSource:
    """Which frame a run will be given, and — if not the requested one — why.

    The whole of task 5.5's decision, resolved **host-side and before anything
    is spent**: :meth:`GlueSessionTestRunner.resolve_frame_source` needs only
    ``frame_mode`` and ``source_data_uri`` to produce one of these, so no
    session, no S3 call and no schema read is involved in deciding.

    ``mode`` is what will actually be built; ``requested_mode`` is what was
    asked for. They differ exactly when a ``sample`` was requested and could
    not be had, which is the state ``unavailable_reason`` names (R9.2). Keeping
    both is the requirement rather than bookkeeping: a single ``mode`` field
    cannot distinguish "no sample was requested" from "a sample was requested
    and the source has none", and R9.2 asks for that distinction by name.

    **``is_sample`` is the retirement predicate**, and it is the only reason
    this type is more than a string. R9.4 retires the *no-source-schema* arm of
    :func:`frame_refusal_results` where the frame is a Sample_Frame — because a
    real sample supplies the columns a declared schema would have — and R9.5
    keeps it everywhere else. A fallback therefore has to be distinguishable
    from a success at the point the refusal is evaluated, or the retirement
    would claim a verification the synthetic path cannot deliver.

    Declared before :class:`FrameGuardedRun` because that class annotates a
    field with this type and the annotation is evaluated at class-creation time
    on Python 3.12, which the test matrix still runs — see
    :class:`FrameGuardedRun`'s own note.
    """

    #: What will be built: :data:`FRAME_MODE_SYNTHETIC` or :data:`FRAME_MODE_SAMPLE`.
    mode: str
    #: What was asked for. Any value, including one outside the two modes — an
    #: unrecognised request is recorded as it arrived rather than normalised
    #: away, so a typo is visible to a reader of the run and not only to
    #: whoever was watching the log.
    requested_mode: str
    #: The URI a sample would be, or was, read from. ``None`` when no sample
    #: was requested; the offending URI when one was requested and refused.
    source_uri: str | None = None
    #: The Spark reader to use, resolved by :func:`sample_read_format`. Set iff
    #: :attr:`is_sample`.
    read_format: str | None = None
    #: Why a *requested* sample was not built (R9.2). ``None`` means either no
    #: sample was requested or one was built — :attr:`is_sample` separates
    #: those.
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Refuse an internally inconsistent decision at construction.

        A ``FrameSource`` claiming :data:`FRAME_MODE_SAMPLE` without a URI and
        a format would make :attr:`is_sample` true — retiring the
        no-source-schema refusal — while :func:`build_frame_code` had nothing
        to read and fell back to a synthetic frame. That combination is the one
        way this design can produce the dishonesty it exists to prevent: a run
        measured against generated data with the "we have real columns"
        refusal disarmed. It is unreachable through
        :meth:`GlueSessionTestRunner.resolve_frame_source`; this makes it
        unreachable through any other constructor too.
        """
        if self.mode == FRAME_MODE_SAMPLE and not (self.source_uri and self.read_format):
            raise FrameBuildError(
                f"a {FRAME_MODE_SAMPLE!r} frame source needs both a source_uri and a read_format, "
                f"got source_uri={self.source_uri!r} read_format={self.read_format!r} — without "
                f"both, the sample cannot be read while the absent-schema refusal would still be "
                f"retired (R9.4/R9.5)"
            )
        if self.mode == FRAME_MODE_SAMPLE and self.unavailable_reason is not None:
            raise FrameBuildError(
                f"a sample was built, so nothing was unavailable, but "
                f"unavailable_reason={self.unavailable_reason!r} was set"
            )

    @property
    def is_sample(self) -> bool:
        """Whether the frame is read from the run's real source (R9.4)."""
        return self.mode == FRAME_MODE_SAMPLE

    @property
    def sample_requested(self) -> bool:
        """Whether a sample was asked for, however it turned out."""
        return self.requested_mode == FRAME_MODE_SAMPLE

    @property
    def sample_unavailable(self) -> bool:
        """Whether a *requested* sample could not be built (R9.2).

        False when no sample was requested, which is the distinction R9.2 turns
        on — :attr:`sample_requested` is the other half of it.
        """
        return self.unavailable_reason is not None

    def provenance(self) -> dict:
        """This run's frame, as a plain dict for the report and the payload (R5.1).

        Six keys, and the first of them is the one a reader wants:

        * ``frame`` — what was actually built. Three values, and the three are
          mutually exclusive and exhaustive rather than by convention:
          :data:`FRAME_MODE_SAMPLE` iff :attr:`is_sample`, in which case
          :meth:`__post_init__` has already refused a set
          :attr:`unavailable_reason`; ``"sample_unavailable"`` when a sample was
          requested and could not be had; :data:`FRAME_MODE_SYNTHETIC`
          otherwise. The middle value is deliberately **not** a
          ``FRAME_MODE_*`` constant — it is not a mode
          :meth:`GlueSessionTestRunner.resolve_frame_source` can put in
          :attr:`mode`, it is a *provenance* distinction between two runs that
          both got a synthetic frame, which is the whole of R5.4.
        * ``requested`` — :attr:`requested_mode` verbatim, including a value
          outside the two modes. Recorded as it arrived for the reason the field
          itself is: a typo stays visible to a reader of the run.
        * ``source_uri``, ``read_format``, ``unavailable_reason`` — the fields,
          unchanged.
        * ``row_bound`` — :data:`_SAMPLE_FRAME_ROWS` on a sample and ``None``
          otherwise. Written from the constant, never as ``100``, so it cannot
          drift from the bound :func:`build_sample_frame_code` actually applies.
          ``None`` off the sample path is not "unbounded": a synthetic frame has
          a height too, but this key is the claim *how much of the customer's own
          data was read*, and on a synthetic run the answer is none of it. A
          number there would read as a quantity of real rows.

        **A plain dict and not this dataclass**, for two independent reasons.
        It crosses into ``create_output_data``'s ``summary`` (R5.6) and thence to
        DynamoDB through ``shared/decimal_utils.convert_floats_to_decimal``,
        which walks dicts and lists and would not see inside a frozen dataclass.
        And the report builder in ``lambda_function.py`` must not import a
        producer type from this module (design §3, C13) — a dict is the seam that
        keeps the dependency one-directional.

        **No cell value, by construction (R6.7).** :class:`FrameSource` has five
        fields and none of them is a row, so nothing here is filtered — there is
        nothing to filter. A sampled value cannot reach this dict without a new
        field being added to the dataclass first, which is a visible change to
        the type rather than an oversight in a serialiser.

        **Why an attribute and a method rather than the two obvious channels.**
        Not a tenth Test_Result_Contract key: R9.2 fixes that key set, and
        ``shape_violations`` reports an addition. Not a fourth
        :meth:`GlueSessionTestRunner.run_test` parameter either: R9.1 fixes its
        arity at three, which 32 references and five covering test files depend
        on. So the constructor carries input, the instance carries output — see
        :attr:`GlueSessionTestRunner.last_frame_source` — and ``run_test``'s
        three arguments are untouched in both directions.

        :data:`_SAMPLE_FRAME_ROWS` is defined *below* this class and that is
        fine: a module-level name in a method body is looked up when the method
        runs, unlike the annotations :class:`FrameGuardedRun`'s own note is
        about, which are evaluated at class-creation time.
        """
        if self.is_sample:
            frame = FRAME_MODE_SAMPLE
        elif self.sample_unavailable:
            frame = "sample_unavailable"
        else:
            frame = FRAME_MODE_SYNTHETIC
        return {
            "frame": frame,
            "requested": self.requested_mode,
            "source_uri": self.source_uri,
            "read_format": self.read_format,
            "row_bound": _SAMPLE_FRAME_ROWS if self.is_sample else None,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class FrameGuardedRun:
    """What :meth:`GlueSessionTestRunner.run_guarded_session` produced.

    Two outcomes, and the shape makes them mutually exclusive so a caller cannot
    read one while the other happened:

    * ``refusal`` is a Test_Result_Contract dict and ``outcomes`` is empty — the
      frame was refused, **no session was created**, and the dict is already the
      harness non-verification a report and the check aggregation understand
      (R7.3).
    * ``refusal`` is ``None`` and ``outcomes`` carries one
      :class:`StatementOutcome` per stage that ran.

    ``refusal`` is a ready-made *result* rather than a reason pair, which is the
    opposite of :class:`StatementOutcome`'s posture. The asymmetry is deliberate:
    a statement's meaning is still open (task 3.3 decides it from the raw
    ``Statement``), whereas a frame refusal's meaning was decided in
    ``code_interpreter_runner`` and is reused here **unchanged** — forming a
    second opinion about it here is the drift the reuse exists to prevent.

    ``frame_source`` is orthogonal to that pair and is set on **both** arms: the
    frame-mode decision is reached before the refusal is evaluated — it is what
    decides whether the no-source-schema arm of the refusal is armed at all
    (R9.4/R9.5) — so a refused run has one too, and a reader can tell a run
    refused for an absent schema from one whose sample was unavailable. It is
    ``None`` only on a default-constructed instance.

    Declared after :class:`StatementOutcome` rather than beside the other
    dataclass on purpose: the annotations below are evaluated at class-creation
    time on Python 3.12, which the test matrix still runs, so a forward
    reference would be a ``NameError`` on import rather than a checker
    complaint. :class:`FrameSource` is declared above for the same reason.
    """

    refusal: dict | None = None
    outcomes: list[StatementOutcome] = field(default_factory=list)
    #: Which frame the run was given and, where a requested sample was
    #: unavailable, why (R9.2).
    frame_source: FrameSource | None = None

    @property
    def refused(self) -> bool:
        """Whether the run was refused before a session could be created."""
        return self.refusal is not None


class GlueSessionTestRunner:
    """Execute a transform block on real Spark in a Glue interactive session.

    The public ``run_test`` is task 8.1. What exists today is the session
    lifecycle: :meth:`run_session` creates a session, brings it to ``READY``,
    runs the statements it is given as three attributable stages, and releases
    the session in a ``finally`` — on every exit path, including one taken
    because an exception was raised (R7.1, R7.2).

    ``role_arn`` is the dedicated read-only
    ``AgenticDataIngestion-VerificationSession-Role`` (spec task 2.1,
    design §4.6) and is passed as the session's runtime role (R6.4). The
    runner does not resolve it — wiring is task 9.x — so a caller that passes
    the wrong role gets an ``AccessDenied`` at session creation, which is why
    task 2.3 pins the role's shape with a CDK assertion instead.

    ``frame_mode`` is :data:`FRAME_MODE_SYNTHETIC` or :data:`FRAME_MODE_SAMPLE`
    (design §4.4), and :meth:`resolve_frame_source` is what reads it. Anything
    else is treated as synthetic with a WARNING naming the value. What a run
    actually *got* is :attr:`last_frame_source`, not this — they differ on every
    run whose requested sample was unavailable, and ``frame_mode`` is the wrong
    one to read when the question is which frame a result was measured against.

    ``source_data_uri`` is the run's real source object, and it is a
    **constructor** argument for a stated reason: R9.6 requires it to reach this
    runner without changing ``run_test``'s parameter list, which 32 references
    and five test files depend on. Task 9.3 wired it: the ``glue`` branch in
    ``lambda_function.py`` passes the value the TestCode payload carries,
    coercing the payload's ``""`` default to ``None`` so that "absent" has one
    spelling here. An absent URI is ``None`` and falls back — recorded as
    :data:`SAMPLE_UNAVAILABLE_NOT_S3_SOURCE`, which is the correct answer for a
    URI that is absent rather than a bug.

    A non-``s3://`` URI is an **ordinary case, not an error**:
    ``schema_extract/lambda_function.py:1735`` writes
    ``f"jdbc://pipeline/{pipeline_id}"`` as its fallback. And this runner reads
    **no JDBC source at all** — R9.3, design C8. That is not a simplification
    to be lifted later: a JDBC read needs VPC configuration, a Glue connection
    and credentials, and ``LGR-819980f-1`` (**standing invariant**) makes
    credentials reachable only by Secrets Manager ARN because plaintext leaked a
    real credential once; ``LGR-819980f-2`` adds that ``SECRET_ID`` in
    ``ConnectionProperties`` is load-bearing and the ``adi:secret_arn`` tag is a
    read-path mirror only. A verification session is granted none of that
    machinery, and the task 2.1 role has no Secrets Manager access at all. Do
    NOT add a JDBC arm to :func:`build_frame_code`; widening the credential
    surface for a *test* is the trade this design refuses.

    ``session_id`` is the reuse seam (R7.5, design §4.3). When supplied, this
    runner neither creates nor releases a session: the caller owns the
    lifecycle. **Nothing supplies it.** v1 rejected reuse on the break-even
    arithmetic — a held session at the default 15-minute idle timeout bills
    ~$0.22 regardless of use, so reuse pays only above roughly 8 verifications
    in one 15-minute window, and pipeline runs are not batched
    (``DEFAULT_REGENERATION_MAX_ATTEMPTS = 2``). The seam exists so a later
    spec can add a registry without reopening this class; do not build the
    registry here.
    """

    def __init__(
        self,
        region: str,
        *,
        role_arn: str,
        frame_mode: str = FRAME_MODE_SYNTHETIC,
        source_data_uri: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.client: GlueClient | None
        try:
            self.client = boto3.client("glue", region_name=region)
        except Exception as exc:
            # Same posture as `CodeInterpreterTestRunner.__init__`: an
            # unavailable client is not an import-time failure, it is a
            # pre-flight refusal at `run_test` (task 8.1, `_empty_results`).
            logger.warning("glue client unavailable: %s", exc)
            self.client = None
        self.region = region
        self.role_arn = role_arn
        self.frame_mode = frame_mode
        #: What `run_guarded_session` last RESOLVED from `frame_mode` and the
        #: URI, or `None` before a run. Named `last_frame_source` and not
        #: `frame_source` deliberately: `frame_mode` above is INPUT
        #: CONFIGURATION and this is the PER-RUN OBSERVATION that came of it, and
        #: a reader who conflates the two goes looking for the wrong one. The
        #: distinction is not cosmetic — an unrecognised `frame_mode` and a
        #: requested-but-unavailable sample both leave `frame_mode` saying
        #: "sample" while this says a synthetic frame was built.
        #:
        #: Set before anything in `run_guarded_session` can raise, so it survives
        #: the two paths that produce no `FrameGuardedRun`: R3.4's statement
        #: deadline and a `FrameBuildError` out of the frame build. R5.1 reads it
        #: for the report and R5.6 for the approval payload.
        self.last_frame_source: FrameSource | None = None
        #: The run's real source object, or `None`. R9.6 — on the constructor so
        #: that `run_test`'s parameter list does not move. Wired by task 9.3.
        self.source_data_uri = source_data_uri
        #: Supplied by the caller ⇒ the caller owns create and release.
        self._external_session_id = session_id
        #: The session this runner is currently working in, external or its own.
        self.session_id: str | None = session_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def _glue(self) -> "GlueClient":
        """The Glue client, narrowed.

        ``self.client`` is optional because an unavailable client is a
        pre-flight refusal rather than an import failure, and every call site
        below would otherwise need its own ``is None`` guard. One narrowing
        point is both shorter and safer: a new call site cannot forget it.

        :meth:`run_session`'s explicit check stays regardless — it is the
        documented pre-flight *position* (task 8.1's step 1, the same order
        ``code_interpreter_runner.run_test`` uses), not a substitute for this.
        """
        if self.client is None:
            raise GlueSessionError("glue client not available")
        return self.client

    @property
    def owns_session(self) -> bool:
        """Whether this runner creates and releases its own session.

        False exactly when an external ``session_id`` was supplied (R7.5).
        """
        return self._external_session_id is None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_test(
        self,
        transform_block: str,
        source_schema: dict,
        target_schema: dict,
    ) -> dict:
        """Execute *transform_block* on real Spark and return one contract dict.

        The Test_Result_Contract, identically to
        ``CodeInterpreterTestRunner.run_test`` (``code_interpreter_runner.py:235-317``):
        the same nine keys with the same value types (R1.1, R1.2), so the report,
        ``aggregate_check_results`` and the gate need no new vocabulary for a
        Spark run. The shapes are written down in
        ``tests/fixtures/run_test_result_shapes.py``; every ``return`` below goes
        through one of the ``*_results`` builders, which is what keeps them there.

        **Three arguments, and the arity is the contract** (R9.6). 32 references
        plus five covering test files depend on it, so ``source_data_uri`` — the
        one value a Spark run wants and the pandas path does not have — reaches
        this runner through ``__init__`` instead. Do not add a fourth parameter.

        **The transform is executed as written** (R1.4). *transform_block* goes
        through :func:`transform_statement` and through nothing else. Nothing
        rewrites it, nothing inspects it, and no vocabulary is consulted — which
        is the whole point of the executor and the reason a construct the pandas
        proxy refuses by name (``pyspark_to_pandas.py:573-582``) runs here (R3.1).

        *One correction to what this docstring used to claim.* It said the block
        was submitted **verbatim**, and it was — which is why every Glue
        verification failed with ``IndentationError: unexpected indent (<stdin>,
        line 2)`` the moment ``glue`` became the active executor, observed on
        run ``run-2026-08-18-17-48-19-bd6f4deb``. The block arrives carrying the
        four spaces it has inside the generated job's ``try:`` block, and a Glue
        statement is a fresh top-level scope. :func:`dedent_for_statement` now
        removes that common indent, which changes no statement and is what the
        pandas path has always done to the same input
        (``pyspark_to_pandas.py:1948``). "As written" is a claim about
        *constructs*, not about columns; do not read it as licence to submit an
        indented block again.

        Pre-flight order, copied from ``run_test`` (``:268-305``) because the
        order is load-bearing:

        1. **client unavailable** → :func:`not_executed_results`. A FAILURE, not
           a non-verification: an executor that could not be reached proved
           nothing *and named no reason a reviewer can act on* (p32 3.22). Same
           posture as ``_empty_results`` at ``:268-270``.
        2. **no conversion step.** This is the change, and it is an *absence*
           rather than a substitution — see the note below.
        3. **frame refusal, before any session** — delegated to
           :meth:`run_guarded_session`, which is where R7.3's position is
           guaranteed and where task 5.4's property is pinned.
        4. **capacity** — ``CreateSession`` may be refused because the account
           is at its concurrent-session limit, which is a harness
           non-verification rather than a failure (R8.1). Routed by
           :func:`create_session_failure_results`, never by either of its two
           builders directly.

        **Step 2 is why ``not_verified_by_proxy`` cannot appear on anything this
        returns** (R3.2). There is no proxy: ``PySpark2PandasConverter`` is not
        imported by this module and is not called from it, so the marker has no
        producer here and the absence holds by construction rather than by an
        assertion. Task 8.3's property asserts it; this is what makes it true.

        **``conversion_report`` is ``None`` on every path, and that is a
        decision.** The key's documented meaning is "what the PySpark→pandas
        conversion handled; None when conversion failed or never ran"
        (``code_interpreter_runner.py:247-249``) — and on this executor it never
        ran, so ``None`` is the value that sentence already assigns. The pandas
        path overwrites the builders' ``None`` with a real report on every path
        that reached a successful conversion (``:304``, ``:316``); this method
        deliberately overwrites nothing.

        *Rejected: synthesise a report* — something like
        ``{"input_lines": n, "output_lines": n, "constructs_converted": []}`` —
        so that a mechanical type comparison against the pandas path passes on
        all six shapes. Rejected because it manufactures a diagnostic about a
        translation that did not happen, and because this key is the one field
        that distinguishes "the proxy translated this" from "real Spark ran
        this". Making the two indistinguishable would remove exactly the signal
        shadow mode exists to measure, which is the evidence
        ``LGR-4dd9b42-1``'s revisit condition asks for before the converter can
        be retired. *Rejected: drop the key* — that breaks R1.1's key-set
        equality, and ``shape_violations`` would report it missing.

        *What this costs, stated so 8.2 does not discover it.* The contract
        declares ``conversion_report: (dict, NONE)`` at the base
        (``run_test_result_shapes._BASE``), so ``None`` is inside the declared
        type. The per-outcome narrowings to ``dict`` — on ``success``,
        ``statement_failure``, ``frame_refusal`` and ``lifecycle_exception`` —
        are narrowings of the *pandas call path*, driven by conversion having
        run there; the fixture's own docstring says the type "is set by the CALL
        PATH, not by the return statement". So Property 2 (task 8.2) compares
        the two executors on the key SET and on every other key's type, and
        excludes this one key's narrowing — which is already the established
        pattern in this spec's own tests, at ``test_glue_result_mapping.py:157``
        and ``test_glue_result_mapping_prop.py:293``. Nothing in the fixture
        needs to move.

        *Verified, not assumed:* ``conversion_report`` has **no consumer**. A
        grep across ``lambda/``, ``shared/`` and ``dashboard-react/src`` finds
        only the two runner modules writing it and tests reading it —
        ``lambda_function.py`` never reads the key. So ``None`` here degrades no
        report and no screen; it is a diagnostic field that is honestly empty.

        **The two raised routes, and where each lands.**
        :meth:`run_guarded_session` *returns* a refusal and *raises* everything
        else, so both need a clause here:

        * :class:`FrameBuildError` — host-side, before any session. R7.3's
          shape: a harness non-verification with no session created, which is
          the destination its own docstring names. See
          :func:`frame_build_failure_results` for why it carries a reason of its
          own rather than borrowing a width reason.
        * :class:`GlueSessionError` — the lifecycle's own failures (a session
          that never reached ``READY``, a statement that never reached a
          terminal state, an unavailable client). :func:`lifecycle_failure_results`,
          whose messages already name the reached state (R5.6) — **except on a
          Sample_Frame run**, where R3.4 sends it to
          :func:`sample_read_deadline_results` instead. See below.
        * anything else is a boto3 error out of an AWS call, and it goes through
          :func:`create_session_failure_results` — the R8 router, which
          recognises the one code that is a non-verification **first**.

        **A Sample_Frame's failed source read is a HARNESS non-verification, and
        it reaches that verdict through two doors** (R2.1, R2.2, R3.4, R4.1).
        Both are decided here rather than one level down, because this is the only
        place that holds both the frame decision and the run's outcomes:

        * the frame-build **statement** failed — Glue reported an
          ``AccessDenied``, a malformed object, a reader that could not open it.
          :func:`sample_read_failure` is the gate and
          :func:`sample_read_failure_results` the destination. Without this route
          the observation goes to :func:`statement_failure_results` and claims
          ``executed`` true with "test data generation failed", because
          :data:`STAGE_FRAME_BUILD` is labelled ``"test data generation"`` — a
          reported transform failure for a transform that never ran, on a stage
          that generated nothing.
        * the frame-build statement **never finished** —
          :meth:`_await_statement` raised :class:`GlueSessionError` past
          :data:`_STATEMENT_DEADLINE_SECONDS`. There is no
          :class:`StatementOutcome` and no :class:`FrameGuardedRun` on a raise
          path, which is why that arm reads :attr:`last_frame_source` and why
          :meth:`run_guarded_session` records it before anything can raise.

        **Routed here and NOT inside :func:`results_from_outcomes`.** That
        function takes one argument, has a documented single responsibility — the
        last outcome is the whole verdict — and four test callers; widening its
        signature to carry a frame decision would make a result mapper depend on a
        configuration fact. Keeping the decision here also leaves the routing of
        every *synthetic* run byte-identical, which is what makes this change
        additive rather than a rewrite of the mapping layer.

        *Known imprecision in the deadline arm, stated in the shape the clause
        above uses.* Every :class:`GlueSessionError` raised out of
        :meth:`run_guarded_session` on a Sample_Frame run takes that route, not
        only a frame-build deadline — a session that never reached ``READY`` would
        take it too, and would be described as a source read that did not finish.
        The contract SHAPE is identical either way (a harness non-verification,
        ``executed`` false, no ``exit_code``) and the exception's own text travels
        inside ``error_details`` naming the state it actually reached, so a reader
        is not misled about what broke — only the framing sentence is imprecise.
        It is left that way because the alternative is to re-derive, from an
        exception, which statement it came from: :class:`GlueSessionError` carries
        no stage, so the classification would have to sniff its message, which is
        the inference path :data:`_SYNTHESISED_TRACEBACK_HEADER` documents as the
        one to avoid. If the two ever need separate wording, the distinction
        belongs where the exception is raised — in :meth:`_await_statement`, which
        knows the stage — and not in a second reading of its text here.

        *Known imprecision in that last clause, with its revisit condition.*
        ``ResourceNumberLimitExceededException`` can only be raised by
        ``CreateSession``, so the capacity arm is always right. A boto3 error
        raised later — a throttled ``GetStatement``, say — reaches the router's
        else-branch and is prefixed "Glue session creation failed", which names
        the wrong call. The contract SHAPE is identical either way
        (``executed`` false, ``exit_code`` ``None``, the error in
        ``error_details``) and boto3's own message inside it names the operation
        that actually failed, so a reader is not misled about what broke — only
        the prefix is imprecise. It is left that way because the alternative is
        to re-test the capacity code at this call site, duplicating the routing
        decision :func:`create_session_failure_results` exists to hold in one
        place. If a mid-lifecycle AWS error ever needs its own wording, the
        classification belongs in :meth:`run_session`, beside the call that
        raised it — not here.

        **Session lifecycle is not re-implemented here** (R7.1, R7.2).
        :meth:`run_session`'s ``finally`` stops and deletes every session it
        created on every exit path including an exception, and this method adds
        no path around it: each ``except`` clause below runs *after* that
        ``finally`` has already released the session. Task 5.4's Property 8 and
        task 3.2's release property are pinned on those methods for that reason,
        and a ``run_test`` built on them inherits both rather than restating
        them.
        """
        if self.client is None:
            logger.warning("Glue client unavailable — returning empty results")
            return not_executed_results(_CLIENT_UNAVAILABLE_DETAIL)

        # 2. NO CONVERSION STEP. The pandas path converts here
        # (`code_interpreter_runner.py:272-276`) and can refuse. There is
        # nothing to convert and therefore nothing to refuse — see the
        # docstring on why that absence is the feature rather than a gap.

        # 3 and 4. The frame refusal precedes `CreateSession` inside
        # `run_guarded_session` (R7.3); the capacity refusal happens at it.
        statements_after_frame = [
            # R1.4 — the transform, untranslated. This is the one line that makes
            # this executor different from the other one. `transform_statement`
            # normalises the block's leading indentation and does nothing else;
            # read `dedent_for_statement` for why a Glue statement cannot carry
            # the indent the generated job gives the block, and why removing it
            # is not a translation.
            transform_statement(transform_block),
            validation_statement(target_schema),
        ]
        try:
            run = self.run_guarded_session(source_schema, statements_after_frame)
        except FrameBuildError as exc:
            return frame_build_failure_results(exc)
        except GlueSessionError as exc:
            # R3.4 — the deadline door onto the same reason. On a Sample_Frame run
            # the statement that did not finish was the SOURCE READ, and
            # `lifecycle_failure_results` names neither the read nor the harness.
            # `self.last_frame_source` and not `run.frame_source`: this is a raise
            # path, so there is no `FrameGuardedRun` to read one off —
            # `run_guarded_session` records the decision on the instance before
            # anything can raise for exactly this reason.
            resolved_frame_source = self.last_frame_source
            if resolved_frame_source is not None and resolved_frame_source.is_sample:
                return sample_read_deadline_results(exc, resolved_frame_source)
            return lifecycle_failure_results(exc)
        except Exception as exc:
            # A boto3 error out of one of the AWS calls. Routed through the R8
            # router, which recognises the one code that is a non-verification
            # first — see the docstring for what the else-branch's wording costs
            # when the failing call was not `CreateSession`.
            logger.error("Glue verification failed on an AWS call: %s", exc, exc_info=True)
            return create_session_failure_results(exc)

        refusal = run.refusal
        if refusal is not None:
            # Already a complete Test_Result_Contract dict, built by the pandas
            # path's own `_frame_truncation_results`. Returned unchanged — and in
            # particular its `conversion_report` is left at `None` rather than
            # overwritten, which is the same decision as everywhere else here.
            return refusal

        # R2.1 / R2.2 / R4.1 — the statement door. Routed HERE and not inside
        # `results_from_outcomes`, which takes one argument and holds one
        # responsibility ("the last outcome is the whole verdict"): this is the only
        # place that holds BOTH the frame decision and the outcomes, and widening a
        # result mapper's signature to carry a configuration fact is the change
        # this shape avoids. Every synthetic run falls straight through to the same
        # call it always made, byte-identically.
        frame_source = run.frame_source
        if frame_source is not None and frame_source.is_sample:
            read_failure = sample_read_failure(run.outcomes)
            if read_failure is not None:
                return sample_read_failure_results(read_failure, frame_source)

        return results_from_outcomes(run.outcomes)

    def run_session(self, statements: list[tuple[str, str]]) -> list[StatementOutcome]:
        """Run *statements* in one session and return one outcome per stage.

        *statements* is ``[(stage_label, code), …]`` — three entries on the
        live path (frame build, transform, validation), each polled to a
        terminal state before the next is submitted. Execution **stops at the
        first failure**, so the returned list may be shorter than *statements*
        and its last entry is the stage that failed. That is what makes a
        failure attributable to a stage, which is why the design keeps three
        statements rather than collapsing them into one.

        R7.1 / R7.2 are absolute here: a session that reaches creation is
        stopped **and** deleted before this returns, on every exit path,
        including one taken because an exception was raised. The ``finally``
        below is the whole mechanism, and :meth:`_release_session` cannot
        raise, so nothing can jump over it.

        Raises :class:`GlueSessionError` when the session or a statement could
        not be brought to a usable state within its deadline. A statement that
        ran and *failed* returns normally, as an outcome with ``failed`` true.
        """
        if self.client is None:
            raise GlueSessionError("glue client not available")

        created_session_id: str | None = None
        try:
            # Read the attribute rather than testing `owns_session`, so the
            # session id below is a `str` to the checker as well as in fact.
            external_session_id = self._external_session_id
            if external_session_id is None:
                created_session_id = self._create_session()
                session_id = created_session_id
            else:
                session_id = external_session_id
            self.session_id = session_id

            # An externally supplied session is polled to READY but never
            # created: a caller holding a session may hand it over while it is
            # still PROVISIONING, and waiting is not creating. If it is
            # already READY this returns on the first poll without sleeping.
            self._await_session_ready(session_id)
            return self._run_statements(statements, session_id)
        finally:
            # R7.1 / R7.2. Release only what this runner created — releasing a
            # caller's session would defeat the reuse seam it exists for
            # (R7.5). `created_session_id` is set only after `CreateSession`
            # returned, so a refused creation has nothing to release and a
            # successful one always does.
            if created_session_id is not None:
                self._release_session(created_session_id)
                self.session_id = None

    def run_guarded_session(
        self,
        source_schema: dict | None,
        statements_after_frame: Sequence[tuple[str, str]] = (),
    ) -> FrameGuardedRun:
        """Refuse an unbuildable frame, else build it and run the session (R7.3).

        The pre-flight *position*, which is the whole point of this method: the
        refusal is evaluated and the frame code is generated **before**
        :meth:`run_session` is reached, so a schema no honest frame can be built
        for costs no ``CreateSession``. Ordering it this way is not an
        optimisation — see :func:`frame_refusal_results` for the hazard it
        prevents, which is a *false low* ``schema_compliance`` rather than a
        crash.

        Returns a :class:`FrameGuardedRun`. When it is ``refused`` the caller
        already holds a complete Test_Result_Contract dict and nothing was
        spent; otherwise ``outcomes`` is what :meth:`run_session` observed, with
        :data:`STAGE_FRAME_BUILD` first.

        *statements_after_frame* is ``[(stage_label, code), …]`` for the stages
        that follow the frame build — the transform and the validation, which
        task 6.1 supplies. It defaults to empty so the guard is exercisable on
        its own; a live run passes both.

        **Two failure routes reach a caller here, and both precede creation.**
        A refusal returns; a :class:`FrameBuildError` out of
        :func:`build_frame_code` *raises* — from a declared type outside the
        vocabulary on the synthetic arm, or a format with no reader on the
        sample arm. Neither is caught and
        mapped here, because mapping a raised frame-build failure onto the
        contract is ``run_test``'s (task 8.1) — R7.3's shape, a harness
        non-verification with no session created, is the destination it fits and
        is the same destination the refusal already reaches. What this method
        guarantees is the part 8.1 cannot add later: whichever route is taken,
        it is taken before ``CreateSession``.

        This is not ``run_test``. There is no client-unavailable pre-flight
        (:meth:`run_session` raises :class:`GlueSessionError` for that), no
        capacity classification, and no mapping of outcomes onto the contract.
        Those are task 8.1's, which composes this.

        **The frame-mode decision comes first, and the order is the point.**
        :meth:`resolve_frame_source` runs before the refusal is evaluated,
        because whether the refusal's *no-source-schema* arm is armed at all
        depends on whether the frame will be a Sample_Frame (R9.4 / R9.5). It is
        free to run first: it reads two attributes and touches no schema, no
        session and no S3 object, so it cannot fail and cannot spend anything.
        The refusal still precedes both the frame BUILD and ``CreateSession``,
        which is the ordering R7.3 constrains and task 5.4 checks.

        **One side effect, and it is recorded before anything can raise.**
        :attr:`last_frame_source` is set the moment the decision is made, so
        every route out of this method — the refusal return, a
        :class:`FrameBuildError` from the frame build, an exception from
        :meth:`run_session`, or the ordinary return — leaves the resolved
        :class:`FrameSource` readable on the runner. That is deliberately a
        stronger guarantee than :attr:`FrameGuardedRun.frame_source` gives, which
        exists only on the two routes that produce a ``FrameGuardedRun`` at all
        (R3.4, R5.1).
        """
        frame_source = self.resolve_frame_source()
        # Recorded HERE — before the refusal is evaluated and before the frame is
        # built — because both of those end the run for some inputs: the refusal
        # returns, and `frame_build_statement` raises `FrameBuildError` for a
        # declared type outside the vocabulary. On a raise there is no
        # `FrameGuardedRun` for a caller to read a `frame_source` off, and R3.4's
        # deadline arm and R5.1's report both still have to name the frame this
        # run was given. This attribute is the only channel that survives those
        # paths; move the assignment below either of them and it stops being one.
        self.last_frame_source = frame_source
        refusal = frame_refusal_results(source_schema, sample_frame=frame_source.is_sample)
        if refusal is not None:
            return FrameGuardedRun(refusal=refusal, frame_source=frame_source)

        # `frame_build_statement` and not `build_frame_code`: the statement that
        # is submitted is the frame code PLUS the `GENERATED:` marker line that
        # reports on it (task 6.1). `build_frame_code` stays a pure mode dispatch.
        frame_code = frame_build_statement(frame_source, source_schema)
        statements = [(STAGE_FRAME_BUILD, frame_code), *statements_after_frame]
        return FrameGuardedRun(outcomes=self.run_session(statements), frame_source=frame_source)

    def resolve_frame_source(self) -> FrameSource:
        """Decide which frame this run gets, from ``frame_mode`` and the URI (R9.1, R9.2).

        Pure with respect to AWS: reads two attributes, inspects a string, and
        returns. **No S3 call, no HEAD, no session** — the URI's *shape* is the
        whole input, which is what makes the decision checkable without a Spark
        runtime or a network and is why task 5.6's property can generate URIs
        against it directly.

        Three answers:

        * ``frame_mode`` is not :data:`FRAME_MODE_SAMPLE` → synthetic, nothing
          requested, ``unavailable_reason`` ``None``. An unrecognised value
          lands here too, with a WARNING naming it — the R2.3 posture: fall
          through to the existing behaviour, but visibly.
        * a sample was requested and the URI is an ``s3://`` object whose format
          is recognised → :data:`FRAME_MODE_SAMPLE` (R9.1).
        * a sample was requested and it cannot be had → synthetic, with the
          reason (R9.2). ``requested_mode`` still says ``sample``, which is what
          makes "a sample was requested and the source has none" distinguishable
          from "no sample was requested".

        **The `only if` direction is the safety-critical one and it holds
        structurally**: :data:`FRAME_MODE_SAMPLE` is returned from exactly one
        place, guarded by :func:`is_s3_source_uri`, so no non-``s3://`` URI can
        reach a read — which is R9.3 and design C8, backed by
        ``LGR-819980f-1`` (**standing invariant**). A ``jdbc://`` URI is not
        merely unhandled here; it is unreachable.

        The ``if`` direction is narrower than "every ``s3://`` URI is read", by
        the second arm of :func:`sample_read_format`. That narrowing is
        deliberate and recorded there.
        """
        requested_mode = self.frame_mode
        if requested_mode != FRAME_MODE_SAMPLE:
            if requested_mode != FRAME_MODE_SYNTHETIC:
                logger.warning(
                    "unrecognised frame_mode %r — building a %s frame, which is the default. "
                    "No sample was requested; the recognised values are %r and %r.",
                    requested_mode,
                    FRAME_MODE_SYNTHETIC,
                    FRAME_MODE_SYNTHETIC,
                    FRAME_MODE_SAMPLE,
                )
            return FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode=requested_mode)

        source_uri = self.source_data_uri
        # `is None` first so the checker narrows `source_uri` to `str` below.
        # `is_s3_source_uri` accepts anything and would narrow nothing.
        if source_uri is None or not is_s3_source_uri(source_uri):
            logger.info(
                "a %s frame was requested but source_data_uri %r is not an s3:// object URI — "
                "falling back to a %s frame. This is the ordinary case for a JDBC-sourced "
                "pipeline (schema_extract writes jdbc://pipeline/<id> as its fallback) and this "
                "runner reads no JDBC source (R9.3, LGR-819980f-1).",
                FRAME_MODE_SAMPLE,
                source_uri,
                FRAME_MODE_SYNTHETIC,
            )
            return FrameSource(
                mode=FRAME_MODE_SYNTHETIC,
                requested_mode=requested_mode,
                source_uri=source_uri,
                unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
            )

        read_format = sample_read_format(source_uri)
        if read_format is None:
            logger.info(
                "a %s frame was requested and source_data_uri %r is an s3:// object URI, but its "
                "format is not recognised, so how to read it is unknown — falling back to a %s "
                "frame rather than guessing a reader.",
                FRAME_MODE_SAMPLE,
                source_uri,
                FRAME_MODE_SYNTHETIC,
            )
            return FrameSource(
                mode=FRAME_MODE_SYNTHETIC,
                requested_mode=requested_mode,
                source_uri=source_uri,
                unavailable_reason=SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
            )

        logger.info(
            "building a %s frame from %r, read as %s (R9.1)",
            FRAME_MODE_SAMPLE,
            source_uri,
            read_format,
        )
        return FrameSource(
            mode=FRAME_MODE_SAMPLE,
            requested_mode=requested_mode,
            source_uri=source_uri,
            read_format=read_format,
        )

    def _create_session(self) -> str:
        """``CreateSession`` with the verification role and the C10 shape.

        Returns the session id. Raises whatever boto3 raises — in particular
        ``ResourceNumberLimitExceededException``, which
        :func:`create_session_failure_results` maps to a capacity harness
        non-verification rather than to a failure, because an account-level limit
        says nothing about the transform. Classifying it HERE is what must not
        happen: the lifecycle raises observations, the mapping decides what they
        mean.
        """
        session_id = f"{_SESSION_ID_PREFIX}-{uuid.uuid4().hex[:12]}"
        self._glue.create_session(
            Id=session_id,
            Role=self.role_arn,
            Command={"Name": "glueetl", "PythonVersion": "3"},
            Description="agentic-data-ingestion transform verification",
            GlueVersion=_GLUE_VERSION,
            WorkerType=_WORKER_TYPE,
            NumberOfWorkers=_NUMBER_OF_WORKERS,
            IdleTimeout=_IDLE_TIMEOUT_MINUTES,
            Tags=_SESSION_TAGS,
        )
        logger.info(
            "Glue session created: %s (%s × %d, idle timeout %d min, role %s)",
            session_id,
            _WORKER_TYPE,
            _NUMBER_OF_WORKERS,
            _IDLE_TIMEOUT_MINUTES,
            self.role_arn,
        )
        return session_id

    def _await_session_ready(self, session_id: str) -> None:
        """Block until *session_id* reports ``READY``.

        Blocking, and legitimately so — the reasoning is at
        :meth:`_await_statement`, which carries the same loop shape and is the
        one a reviewer reaches first.

        Raises :class:`GlueSessionError` on a state from which ``READY`` is
        unreachable, and on the deadline. Both are harness failures, not
        transform failures: nothing about the transform was observed.
        """
        deadline = time.monotonic() + _SESSION_READY_DEADLINE_SECONDS
        last_state: str | None = None
        while time.monotonic() < deadline:
            session = self._glue.get_session(Id=session_id).get("Session", {})
            last_state = session.get("Status")
            if last_state == _SESSION_STATE_READY:
                logger.info("Glue session %s is READY", session_id)
                return
            if last_state in _SESSION_STATES_TERMINAL_BAD:
                raise GlueSessionError(f"Glue session {session_id} reached {last_state} before READY")
            time.sleep(_SESSION_POLL_SECONDS)

        raise GlueSessionError(
            f"Glue session {session_id} did not reach READY within "
            f"{_SESSION_READY_DEADLINE_SECONDS}s (last state {last_state})"
        )

    def _run_statements(self, statements: list[tuple[str, str]], session_id: str) -> list[StatementOutcome]:
        """Submit each statement in turn, stopping at the first failure."""
        outcomes: list[StatementOutcome] = []
        for stage, code in statements:
            outcome = self._run_statement(stage, code, session_id)
            outcomes.append(outcome)
            if outcome.failed:
                logger.error(
                    "Glue statement failed at stage %r (state %s, output status %s) — skipping the remaining stages",
                    stage,
                    outcome.state,
                    outcome.output_status,
                )
                break
        return outcomes

    def _run_statement(self, stage: str, code: str, session_id: str) -> StatementOutcome:
        """``RunStatement`` for one stage, polled to a terminal state.

        ``session_id`` is passed rather than read off ``self``: the id is
        settled once in :meth:`run_session` and every statement in a lifecycle
        must go to that same session. Reading mutable state here would let a
        future edit send one stage's statement somewhere else.
        """
        # Subscript rather than `.get`: a `RunStatement` response with no `Id`
        # is a broken API contract, and a `KeyError` naming it is a better
        # failure than a `GetStatement` poll on `None`.
        statement_id = self._glue.run_statement(SessionId=session_id, Code=code)["Id"]
        logger.info("Glue statement %s submitted for stage %r", statement_id, stage)
        return self._await_statement(stage, statement_id, session_id)

    def _await_statement(self, stage: str, statement_id: int, session_id: str) -> StatementOutcome:
        """Block until the statement reaches a terminal state; observe it.

        THIS LOOP IS BLOCKING AND THAT IS DELIBERATE — do not "fix" it.

        ``LGR-b46a5bb-1`` is a standing invariant forbidding
        ``threading.Thread`` for post-response work anywhere in ``lambda/**``,
        because the Lambda execution environment freezes the moment the handler
        returns and the thread is killed mid-call. Design C2 verifies it does
        not reach this loop, and the verification is a code read rather than an
        assumption:

        * The live path never runs this code in a Lambda process.
          ``USE_AGENTCORE_RUNTIME`` is ``'true'`` on the code-testing Lambda
          (``compute-stack.ts:432``), so the handler's first branch dispatches
          to the AgentCore container and returns immediately
          (``lambda_function.py:1443-1450``).
        * The work then runs on a container thread whose lifetime the AgentCore
          runtime manages: ``add_async_task`` flips ``/ping`` to
          ``HealthyBusy`` and keeps the microVM scheduled for the thread's
          lifetime (``shared/sfn_task_token.py:200-252``).
        * ``LGR-b46a5bb-4`` scopes its AST property test to ``lambda/`` **for
          exactly this reason**, recording that ``shared/sfn_task_token.py``
          "starts a legitimate thread whose lifetime the AgentCore runtime
          manages" and that an allowlist entry would be "a door".

        So a synchronous poll is correct here. What bounds it is not a thread
        but ``_STATEMENT_DEADLINE_SECONDS`` plus ``IdleTimeout: 1`` — and it
        has to be bounded by something, because design C3 records that a
        stalled ``waitForTaskToken`` step does **not** fail fast: it hangs for
        the full 1200 s (``LGR-60c4985-3``).

        The AST guard does not flag this file: it detects ``threading.Thread``
        instantiation, reference and subclassing, none of which appear here,
        and prose naming the antipattern is invisible to it by construction
        (``tests/unit/test_no_lambda_threads_prop.py``,
        ``test_detector_ignores_prose_naming_the_antipattern``).
        """
        deadline = time.monotonic() + _STATEMENT_DEADLINE_SECONDS
        last_state: str | None = None
        while time.monotonic() < deadline:
            statement = self._glue.get_statement(SessionId=session_id, Id=statement_id).get("Statement", {})
            last_state = statement.get("State")
            if last_state in _STATEMENT_STATES_TERMINAL:
                return self._observe(stage, statement)
            time.sleep(_STATEMENT_POLL_SECONDS)

        raise GlueSessionError(
            f"Glue statement {statement_id} for stage {stage!r} did not reach a terminal state "
            f"within {_STATEMENT_DEADLINE_SECONDS}s (last state {last_state})"
        )

    @staticmethod
    def _observe(stage: str, statement: Mapping[str, Any]) -> StatementOutcome:
        """Wrap a terminal ``Statement`` dict as a :class:`StatementOutcome`.

        Reads only what sequencing needs — the state, ``Output.Status`` and
        stdout — and carries the whole statement through. Everything else the
        result mapping wants (``ErrorName``, ``ErrorValue``, ``Traceback``) is
        task 3.3's to extract, from ``outcome.statement``.

        **THE STDOUT CHANNEL IS NOT TRUNCATED HERE, AND THAT IS DELIBERATE**
        (p44 do-NOT 24, R6.12). This looks like the one place that could bound
        every channel at once — a ``Statement`` dict crosses into this module
        here — and it is exactly the place that must not. ``stdout`` is what
        ``_parse_results`` reads the ``GENERATED:`` and ``VALIDATION:`` markers
        out of, so bounding it here would corrupt the marker parse and turn a
        data-protection measure into a wrong record count; and
        :class:`StatementOutcome`'s ``statement`` is documented as the RAW
        observation so that field extraction stays in one mapping layer.
        :func:`_truncate_quoted_literals` is therefore applied per channel, at
        :func:`exception_summary` and :func:`synthesised_traceback` (R6.10).
        What bounds this channel instead is :data:`_bound_stderr`'s head-2000 +
        tail-1500 (``code_interpreter_runner.py:55-56``), applied where the
        ``CANCELLED`` / ``ERROR`` branch writes it into ``stderr``. That is a
        stated residual, not a gap.
        """
        state = statement.get("State", "")
        output = statement.get("Output") or {}
        output_status = output.get("Status") if output else None
        # Glue puts a statement's printed output under `Output.Data.TextPlain`.
        stdout = (output.get("Data") or {}).get("TextPlain", "") or ""
        return StatementOutcome(
            stage=stage,
            state=state,
            output_status=output_status,
            stdout=stdout,
            statement=statement,
        )

    def _release_session(self, session_id: str) -> None:
        """``StopSession`` then ``DeleteSession``.

        **Raises nothing on the path that calls it.** ``run_session`` reaches
        here only with ``created_session_id`` set, which means
        ``_create_session`` already succeeded, which means ``_glue`` resolved —
        so the one narrowing error this could otherwise raise is unreachable,
        and every AWS call below is inside its own ``except Exception``.

        R7.1 and R7.2 are two requirements, not one, and this method treats
        them that way: the stop and the delete are attempted **independently**,
        so a failing stop cannot skip the delete. Both are best-effort by
        necessity — this runs in a ``finally`` that may be unwinding an
        exception, and raising here would replace the real failure with a
        teardown failure.

        Glue refuses ``DeleteSession`` on a session that is still
        ``PROVISIONING``, reporting ``IllegalSessionStateException``, so the
        delete is a bounded retry rather than a single call. When the retry
        budget runs out the session is logged as leaked and
        ``IdleTimeout: 1`` is what bounds its cost to ~$0.015 — the reason
        that constant is one minute rather than the 15-minute default.
        """
        try:
            self._glue.stop_session(Id=session_id)
            logger.info("Glue session stopped: %s", session_id)
        except Exception as exc:
            # Includes `IllegalSessionStateException` (already stopping, or
            # still provisioning) and `EntityNotFoundException` (already gone).
            # Neither is a reason to skip the delete below.
            logger.warning("Failed to stop Glue session %s: %s", session_id, exc)

        deadline = time.monotonic() + _RELEASE_DEADLINE_SECONDS
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            try:
                self._glue.delete_session(Id=session_id)
                logger.info("Glue session deleted: %s", session_id)
                return
            except Exception as exc:
                if _is_not_found(exc):
                    # Never created, or already fully released. Released is
                    # released.
                    logger.info("Glue session %s already gone", session_id)
                    return
                if not _is_illegal_state(exc):
                    logger.warning("Failed to delete Glue session %s: %s", session_id, exc)
                    return
                logger.info(
                    "Glue session %s not yet deletable (attempt %d) — retrying",
                    session_id,
                    attempts,
                )
                time.sleep(_RELEASE_POLL_SECONDS)

        logger.error(
            "LEAKED Glue session %s — not deleted within %ds. It bills until its %d-minute "
            "idle timeout expires (~$0.015). Delete it by hand if it is still listed.",
            session_id,
            _RELEASE_DEADLINE_SECONDS,
            _IDLE_TIMEOUT_MINUTES,
        )


def _error_code(exc: Exception) -> str:
    """The boto3 error code on *exc*, or ``""`` for anything else.

    Read off ``response`` rather than by ``isinstance(exc, ClientError)``:
    ``botocore.exceptions.ClientError`` is not importable without botocore, and
    Glue's modelled exceptions are generated per-client so there is no stable
    class to catch. The shape is stable; the classes are not.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    code = (response.get("Error") or {}).get("Code", "")
    return code if isinstance(code, str) else ""


def _is_illegal_state(exc: Exception) -> bool:
    """Whether *exc* says "not in a state where that is allowed, yet"."""
    return _error_code(exc) == "IllegalSessionStateException"


def _is_not_found(exc: Exception) -> bool:
    """Whether *exc* says the session does not exist."""
    return _error_code(exc) == "EntityNotFoundException"


#: Glue's error code for "this account already holds as many of these as it may
#: hold". The ONE ``CreateSession`` failure that says nothing whatsoever about
#: the transform being verified — see :func:`session_capacity_results`.
_ERROR_CODE_RESOURCE_LIMIT = "ResourceNumberLimitExceededException"


def _is_capacity_exhausted(exc: Exception) -> bool:
    """Whether *exc* says the account is at its session limit (R8.1).

    Read off the error CODE via :func:`_error_code` rather than by catching a
    modelled exception class, for the reason recorded there: Glue's exception
    classes are generated per-client, so there is no stable class to catch, and
    ``botocore`` may not be importable. The code is stable.
    """
    return _error_code(exc) == _ERROR_CODE_RESOURCE_LIMIT


# ── Frame construction: the pre-session refusal ────────────────────────
#
# Spec task 5.3. R7.3, and design §2's `_frame_refusal` paragraph plus C5.
#
# Both halves are the pandas path's own functions, bound here by name and called
# UNCHANGED — the same technique `_bound_stderr` and `_parse_results` use below,
# and for a stronger reason than tidiness. The refusal is a *judgement* about
# what a frame proves, argued out in `_frame_refusal`'s docstring
# (`code_interpreter_runner.py:320-350`); an equivalent reimplementation here
# would be a second judgement, free to drift from the first, and the two
# executors disagreeing about which schemas are verifiable at all would corrupt
# shadow mode at the root rather than at the margin. One function object is the
# only arrangement in which that cannot happen, and
# `test_frame_refusal_reuse.py` asserts it by identity.

#: Reused verbatim by import: returns ``(reason, field_count)`` when no honest
#: frame can be built from a schema, ``None`` when one can.
#:
#: Annotated explicitly for the same reason :data:`_parse_results` is — the
#: checker resolves the sibling module to ``Any``, so an unannotated alias makes
#: every call through it untyped, and the caller below then reports
#: ``no-any-return``. The annotation restores the signature the source declares;
#: it does not change the object, which is what the identity assertion in
#: ``test_frame_refusal_reuse.py`` pins.
_frame_refusal: Callable[[dict], tuple[str, int] | None] = CodeInterpreterTestRunner._frame_refusal

#: Reused verbatim by import: turns that pair into the harness
#: non-verification dict — the thirteen keys ``FRAME_REFUSAL`` declares in
#: ``tests/fixtures/run_test_result_shapes.py``, including the
#: ``not_verified_field_count`` / ``not_verified_frame_ceiling`` pair that
#: ``lambda_function.HARNESS_FIELD_COUNT_KEY`` / ``HARNESS_FRAME_CEILING_KEY``
#: render as text. Calling it rather than building the dict is what makes a
#: Spark-side refusal and a pandas-side refusal literally the same report.
_frame_truncation_results: Callable[[int, int, str], dict] = CodeInterpreterTestRunner._frame_truncation_results


def frame_refusal_results(source_schema: dict | None, *, sample_frame: bool = False) -> dict | None:
    """The harness non-verification for an unbuildable frame, or ``None`` (R7.3).

    ``None`` means the frame will carry one column per declared field, which is
    the only state in which the transform is worth executing. Anything else
    returns a complete Test_Result_Contract dict and **must stop the run before
    a session is created** — which :meth:`GlueSessionTestRunner.run_guarded_session`
    is the structural guarantee of.

    ``sample_frame`` is :attr:`FrameSource.is_sample`, and it retires **one arm**
    of the refusal — see the *conditional retirement* section below. It defaults
    to ``False``, so the caller that wants the retirement has to ask for it: the
    dangerous mistake here is retiring the refusal by accident, never keeping it
    by accident.

    **The hazard transfers to Spark, which is why this is reused rather than
    reconsidered.** ``_frame_refusal``'s docstring names the mechanism: the
    common transform shape here is ``withColumnRenamed``, the converter rewrites
    it to pandas ``.rename(columns=…)``, and ``.rename`` silently ignores names
    that are absent — so a frame missing columns the transform references does
    not raise, it produces a plausible-looking **low** ``schema_compliance``
    caused entirely by the harness. Spark's own ``withColumnRenamed`` is
    likewise a no-op on an absent column
    (`pyspark.sql.DataFrame.withColumnRenamed`: "if the schema doesn't contain
    the given column name, it is a no-op"), so the *same* false-low score is
    reachable under the new executor by the *same* route. A false failing score
    with evidence attached is harder to diagnose than a crash, and attributing
    it to the transform is exactly the dishonesty p32 exists to remove. Reasoned
    for Spark rather than measured on it (design §6) — the refusal's cost if the
    reasoning is wrong is a refused run, so the safe direction is the one taken.

    **The 512-column ceiling is imported, not restated** (R10.4, design C5).
    ``_MAX_TEST_FRAME_COLUMNS``' derivation is the DynamoDB item-size limit on
    the pipeline-runs state row — ``null_counts`` and ``dtypes`` carry one entry
    per transformed column at ~45 bytes each, so 512 columns is ~46 KB against a
    400 KB item limit (``code_interpreter_runner.py:105-127``). Real Spark
    changes **neither side** of that arithmetic: it writes the same state row
    through the same parser. Do NOT remove or raise it here; its own comment
    records that when ``null_counts``/``dtypes`` stop reaching the state row the
    ceiling "can be removed, not raised".

    **The conditional retirement (R9.4 / R9.5), and why it is conditional.**
    ``NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA`` exists because, with no declared
    schema, the only frame buildable is the pandas builder's ``{id, value}``
    fallback — "a defensive default" whose measurements "prove nothing about the
    pipeline's actual input" (``code_interpreter_runner.py:337-344``).
    ``LGR-b2f79ef-9`` ratified exactly that: the fallback stays in place as a
    defensive default and a run built on it "reports **not verified** … rather
    than a score", because any ``schema_compliance`` figure computed over it is a
    harness artifact.

    A Sample_Frame removes the premise rather than the consequence. Its columns
    come from the run's real source object, so the frame **is** the pipeline's
    input — the one thing the synthetic path cannot supply without a declared
    schema — and the artifact ``LGR-b2f79ef-9`` refuses to report as a score does
    not exist. So with ``sample_frame`` true this returns ``None`` for the
    no-source-schema arm and the run proceeds; with it false the refusal stands
    (R9.5), unchanged.

    Note the record's own revisit condition is a *different* route to the same
    place — "``schema_extract`` gains a guarantee that it always writes a source
    schema, at which point the fallback is dead code and can go". This is not
    that: the fallback stays, ``_frame_refusal`` is untouched, and the arm is
    retired only for the runs that read real columns. Nothing here makes the
    fallback dead.

    **The width arm is NOT retired, in either mode.** A source read as a sample
    is still read into the same ``null_counts``/``dtypes`` state row, so
    :data:`_MAX_TEST_FRAME_COLUMNS`' DynamoDB derivation applies to a
    Sample_Frame exactly as it does to a synthetic one (R10.4, design C5).
    ``sample_frame`` gates the reason, not the refusal.

    **Rejected: retire it unconditionally when the executor is Glue.** That is
    the shape a reader expects — "real Spark, so we can read the source" — and it
    is wrong on the majority of runs. A JDBC-sourced pipeline's
    ``source_data_uri`` is ``jdbc://pipeline/<id>``
    (``schema_extract/lambda_function.py:1735``) and this runner reads no JDBC
    source (R9.3, design C8, ``LGR-819980f-1``), so its frame is synthetic;
    ``frame_mode`` also defaults to synthetic and nothing sets it otherwise yet.
    Retiring the arm on the executor rather than on the frame would therefore
    report a full verification for a run measured entirely against ``{id,
    value}`` — reintroducing precisely the dishonesty ``LGR-b2f79ef-9`` removed,
    on the path that is currently the *only* path.
    """
    refusal = _frame_refusal(source_schema or {})
    if refusal is None:
        return None

    reason, field_count = refusal
    if sample_frame and reason == NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA:
        # R9.4 — the sample supplies the columns the schema would have declared,
        # so there is nothing left for this arm to be about. Deliberately NOT
        # extended to the width arm above: see the docstring.
        logger.info(
            "no source schema was declared, but the frame is read from the run's real source, "
            "so the columns a schema would have declared are present — not refusing (R9.4)"
        )
        return None

    logger.error(
        "Test frame cannot be built (%s): %d declared source field(s) against a ceiling of %d — "
        "refusing before creating a Glue session",
        reason,
        field_count,
        _MAX_TEST_FRAME_COLUMNS,
    )
    # `conversion_report` stays the `None` the shared builder sets. The pandas
    # path overwrites it here with a real report, because its refusal happens
    # AFTER a successful conversion (`code_interpreter_runner.py:304`); this
    # runner converts nothing, so `None` is the truthful value rather than a
    # missing one — the same reasoning the result-mapping section records for
    # every builder in this module.
    return _frame_truncation_results(field_count, _MAX_TEST_FRAME_COLUMNS, reason)


# ── Frame construction: the synthetic frame ────────────────────────────
#
# Spec task 5.1, design §4.4's `synthetic` mode. The Spark equivalent of
# `_build_test_data_code` (`code_interpreter_runner.py:496-533`), and equivalent
# in the strong sense the design asks for: same row count, same one-column-per-
# declared-field rule, same three type buckets with the same membership, same
# ~5% null rate — and the same null POSITIONS, which is the part that has to be
# engineered rather than inherited.
#
# **MECHANISM — why parity is load-bearing, not tidy.** Design §4.4 states it
# outright and R10.3 requires it: a shadow disagreement is attributable to the
# EXECUTOR only if the input was identical. Two independently-drawn 5% null
# masks over 100 rows disagree on ~10 cells per column by construction, so every
# `null_counts` difference shadow mode recorded would be sampling noise and the
# whole measurement — the evidence `LGR-4dd9b42-1`'s revisit condition asks for
# before the converter can be retired — would be worthless. The seed is not a
# nicety.
#
# **Where the nulls are drawn, and why not in Spark.** Host-side, in
# :func:`synthetic_null_rows`, and embedded in the generated code as literal row
# indices. Two reasons, both load-bearing:
#
#   * Spark's `rand(seed)` is seeded per PARTITION, so the same seed over a
#     different partitioning gives different positions. A frame built through it
#     would be reproducible only for a fixed executor count — which is exactly
#     the kind of hidden dependency the seed exists to remove.
#   * Computing the mask host-side makes parity CHECKABLE without a Spark
#     runtime. PySpark is deliberately not a dependency of this repository
#     (design §3 do-NOT 7, `LGR-4dd9b42-1`), so task 5.2's parity property
#     compares :func:`synthetic_null_rows` against the pandas builder's
#     algorithm directly rather than executing two frames.
#
# The generated code still builds the VALUES inside Spark, the way the pandas
# builder builds them inside the interpreter. That is a size decision with the
# same derivation as `_MAX_TEST_FRAME_COLUMNS` (`:105-127`): emitting 100 × 512
# literal cells would put megabytes on the wire, where a comprehension plus a
# ~5-index null list per column stays in the tens of kilobytes.

#: Fixed at 100 by `_build_test_data_code`'s `num_rows = 100`. The pandas
#: builder's own comment at `_MAX_TEST_FRAME_COLUMNS` records that the row count
#: is fixed "so width is the only variable"; this is the other side of that.
_SYNTHETIC_FRAME_ROWS = 100

#: `random.random() < 0.05` in the pandas builder — the "~5% nulls" design §4.4
#: asks for. A rate, not a count: the number of nulls in a column is itself
#: drawn, so two columns differ, which is what makes a per-column `null_counts`
#: comparison worth recording at all.
_SYNTHETIC_NULL_RATE = 0.05

#: The seed. Its VALUE is arbitrary; its FIXEDNESS is the requirement (R10.3).
#:
#: Changing it is not a no-op: it re-draws every null position, so shadow
#: measurements recorded before and after are no longer comparable frame-for-
#: frame. If it ever has to move, treat previously recorded Shadow_Comparisons as
#: a separate population rather than as more of the same one.
_SYNTHETIC_FRAME_SEED = 20260816

#: The name the generated frame-build code binds the DataFrame to, and the seam
#: task 6.1 builds on: its ``GENERATED:`` line reports on this variable, and the
#: transform statement reads it. Spelled once here so the two cannot drift.
#:
#: ``df`` and not ``df_source``, because the transform block the AI generates
#: reads ``df`` — the same name the pandas path's generated frame uses
#: (``code_interpreter_runner.py:527``), for the same reason.
FRAME_VARIABLE = "df"

# ── The three type buckets ─────────────────────────────────────────────
#
# Membership is COPIED from `_build_test_data_code:511-518`, not chosen. Where a
# name looks like it belongs in a different bucket than it is in, the pandas
# builder is why, and matching it is the requirement — a column the two
# executors type differently is a column whose values differ, and R10.3's whole
# point is that they must not.
#
# Case is NOT normalised, for the same reason: the pandas builder tests
# `dtype in ("integer", …)` against the raw declared value, so `"Integer"` lands
# in its string bucket. Lower-casing here would type it as a long on one executor
# and a string on the other. Every producer in this repo lower-cases before
# writing a schema (`shared/target_schema_resolution.py:178`,
# `schema_extract/lambda_function.py:549`,
# `schema_mapping/lambda_function.py:660`), so a case variant is not an observed
# input — it is an unknown, and reaches the refusal below.

#: → ``LongType``. Verbatim from the pandas builder's first branch.
_INT_DECLARED_TYPES = frozenset({"integer", "int", "long", "bigint"})

#: → ``DoubleType``. Verbatim from the pandas builder's second branch.
_FLOAT_DECLARED_TYPES = frozenset({"float", "double", "decimal", "number"})

#: → ``StringType``. The pandas builder has no such list — it has an `else`. This
#: is that `else` written out over the vocabulary this repository actually
#: produces, which is what turns "anything else is a string" into "anything
#: OUTSIDE THIS is unknown" (:func:`spark_type_for`).
#:
#: The vocabulary, by where it is produced:
#:
#:   * the normalised JSON-Schema types every `schema_extract` path emits and
#:     `create_infrastructure/lambda_function.py:135-143` enumerates as its
#:     canonical key set — `string`, `boolean`, `array`, `object`, `null`
#:   * the aliases `shared/target_schema_resolution.py:179-191` accepts on a
#:     `fields` list — `str`, `bool`, `date`, `datetime`, `timestamp`
#:   * the raw Glue/SQL names `schema_mapping/lambda_function.py:663-694`,
#:     `schema_extract/lambda_function.py:551-568` and
#:     `create_glue_pipeline/schema_evolution.py:204-213` name — `char`,
#:     `varchar`, `text`, `binary`, `varbinary`, `time`, `timestamptz`,
#:     `struct`, `map`
#:
#: **`smallint`, `tinyint`, `numeric` and `real` are here on purpose and it is
#: not a mistake.** They are numeric names, and this bucket produces
#: ``"val_{i}"`` strings for them. That is what the pandas builder does with
#: them — none of the four is in either numeric branch there — so putting them
#: anywhere else would break parity on a schema that declares one. The right
#: place to fix the typing is the pandas builder's buckets, which R3.3 and design
#: §3 do-NOT 1 put out of scope here; until then the two agree, and agreeing is
#: what R10.3 asks for.
_STRING_DECLARED_TYPES = frozenset(
    {
        "string",
        "str",
        "boolean",
        "bool",
        "array",
        "object",
        "null",
        "char",
        "varchar",
        "text",
        "binary",
        "varbinary",
        "date",
        "datetime",
        "time",
        "timestamp",
        "timestamptz",
        "struct",
        "map",
        "smallint",
        "tinyint",
        "numeric",
        "real",
    }
)

#: Spark type constructors, by bucket. Names rather than objects: `pyspark` is
#: not importable here (design §3 do-NOT 7) and does not need to be — these are
#: emitted into a code string that Spark imports for itself.
_SPARK_TYPE_LONG = "LongType"
_SPARK_TYPE_DOUBLE = "DoubleType"
_SPARK_TYPE_STRING = "StringType"

#: The per-row value expression for each Spark type, over the loop variable `i`.
#: Character-for-character the pandas builder's three expressions, so a column's
#: non-null values are equal across the two executors and not merely
#: similar — `i`, `round(i * 1.1, 2)` and `f"val_{i}"` (`:513-518`).
_VALUE_EXPRESSIONS: dict[str, str] = {
    _SPARK_TYPE_LONG: "i",
    _SPARK_TYPE_DOUBLE: "round(i * 1.1, 2)",
    _SPARK_TYPE_STRING: 'f"val_{i}"',
}

#: The pandas builder's fallback when a schema declares no fields at all
#: (`:507-508`), mirrored so the two frames match even on the path that is no
#: longer reached. It is not reached: :func:`frame_refusal_results` returns
#: `NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA` for an empty schema, and
#: :meth:`GlueSessionTestRunner.run_guarded_session` refuses on it before a
#: session starts (task 5.3), and the `sample`-mode dispatch does not change
#: that: an unavailable sample leaves the refusal armed (R9.5), and an available
#: one never reaches this builder at all. What keeps the fallback here is that
#: :func:`build_synthetic_frame_code` is also callable directly, by a test — so
#: the unguarded path still needs a
#: defined answer, and the honest one is the pandas builder's: "a defensive
#: default" whose measurements "prove nothing about the pipeline's actual input"
#: (`_frame_refusal`'s own comment, `:341-344`). Removing it would turn that into
#: an `IndexError` deep in the emitted code instead.
_EMPTY_SCHEMA_FALLBACK_FIELDS: dict[str, dict] = {
    "id": {"type": "string"},
    "value": {"type": "string"},
}


def spark_type_for(declared_type: Any) -> str:
    """Return the Spark type name for one declared field type.

    Exhaustive over the vocabulary this repository produces, with an explicit
    default that **refuses** rather than guessing. The three buckets are
    :data:`_INT_DECLARED_TYPES`, :data:`_FLOAT_DECLARED_TYPES` and
    :data:`_STRING_DECLARED_TYPES`; anything else raises
    :class:`FrameBuildError` naming the value it was given.

    **Why refusing beats falling through to ``StringType``.** The pandas builder
    can afford an `else`: pandas is dynamically typed, so a column it fills with
    ``"val_0"`` strings when the schema said something numeric produces a
    plausible-looking frame and a `dtypes` value a reviewer can read. Spark is
    not: the type goes into an explicit ``StructType`` and is *enforced* at
    ``createDataFrame``, and every operation the transform performs on that
    column — a comparison, a cast, an arithmetic expression — then behaves
    according to the wrong type. The failure would surface as a transform error
    or, worse, as a wrong data-quality number, attributed to the transform. A
    named refusal at frame build costs no session and says what is actually
    wrong.

    This is the same argument ``classify_conversion_refusal``
    (``code_interpreter_runner.py:186-199``) makes about defaulting: the default
    has to be the one that cannot be mistaken for a verification.

    A non-``str`` value raises too, rather than being coerced. The case that
    reaches this is a JSON-Schema union type (``{"type": ["string", "null"]}``),
    where there is no single right answer and choosing one silently is precisely
    the mis-typing above.
    """
    if not isinstance(declared_type, str):
        raise FrameBuildError(
            f"declared field type must be a string, got {type(declared_type).__name__} "
            f"({declared_type!r}) — a union or structured type has no single Spark type, "
            f"so no honest test frame can be built for it"
        )
    if declared_type in _INT_DECLARED_TYPES:
        return _SPARK_TYPE_LONG
    if declared_type in _FLOAT_DECLARED_TYPES:
        return _SPARK_TYPE_DOUBLE
    if declared_type in _STRING_DECLARED_TYPES:
        return _SPARK_TYPE_STRING
    raise FrameBuildError(
        f"declared field type {declared_type!r} is outside the known vocabulary, so the "
        f"column would be mis-typed rather than merely approximated. Add it to one of "
        f"the three buckets in glue_session_runner.py — and to the matching branch of "
        f"code_interpreter_runner._build_test_data_code, or the two executors' frames "
        f"stop agreeing (R10.3)"
    )


def synthetic_frame_fields(source_schema: dict | None) -> list[tuple[str, str]]:
    """Return ``[(column_name, spark_type_name), …]`` for the synthetic frame.

    The column list and its ORDER, which is half of what R10.3 pins. Derived
    from :func:`~code_interpreter_runner.declared_source_fields` — imported, not
    reimplemented, because that helper exists (its own docstring says so) "so
    the two cannot drift": a builder that read ``properties``/``fields``
    differently from the pandas builder would produce a differently-shaped frame
    from the same schema, and every column-level shadow disagreement after that
    would be an artifact of the reading rather than of the executor.

    Mirrors the pandas builder's two other schema behaviours as well: the
    ``{id, value}`` fallback for a schema declaring nothing
    (:data:`_EMPTY_SCHEMA_FALLBACK_FIELDS`), and the
    ``[:_MAX_TEST_FRAME_COLUMNS]`` slice — which is belt-and-braces on both
    paths, since a wider schema is refused before a session starts
    (:func:`frame_refusal_results`).

    Raises :class:`FrameBuildError` for a declared type outside the vocabulary,
    naming the COLUMN as well as the type — a schema with 400 fields needs to
    say which one.
    """
    fields = declared_source_fields(source_schema) or _EMPTY_SCHEMA_FALLBACK_FIELDS

    columns: list[tuple[str, str]] = []
    for name, meta in list(fields.items())[:_MAX_TEST_FRAME_COLUMNS]:
        # `_build_test_data_code:512` — a non-dict `meta` is treated as an
        # undeclared type, which defaults to `string`. Mirrored, not tidied.
        declared = meta.get("type", "string") if isinstance(meta, dict) else "string"
        try:
            columns.append((name, spark_type_for(declared)))
        except FrameBuildError as exc:
            raise FrameBuildError(f"column {name!r}: {exc}") from exc
    return columns


def synthetic_null_rows(
    column_names: Sequence[str],
    *,
    num_rows: int = _SYNTHETIC_FRAME_ROWS,
    seed: int = _SYNTHETIC_FRAME_SEED,
) -> dict[str, list[int]]:
    """Return the row indices to null out, per column — the seeded null mask.

    The other half of R10.3, and the reason this is a separate public function
    rather than an inline loop: it is the *whole* of the non-determinism, so
    pinning it pins the frame, and task 5.2's parity property can compare it
    against the pandas builder's mask without either executor running.

    **The draw order is the pandas builder's, exactly.** That builder does::

        for c in df.columns:
            mask = [random.random() < 0.05 for _ in range(num_rows)]

    — ``num_rows`` draws per column, columns in frame order, from one stream. So
    this walks *column_names* in order and takes ``num_rows`` draws each from one
    :class:`random.Random`. Reordering the two loops, or drawing per column from
    its own seeded stream, would still be deterministic and would still be
    wrong: the positions would no longer be the ones a seeded pandas builder
    produces, and R10.3 is about the two agreeing rather than about either being
    stable on its own.

    ``random.Random`` and not ``numpy`` or ``secrets``: the pandas builder uses
    the stdlib Mersenne Twister via the module-level ``random`` functions, and
    ``random.Random(seed)`` is the same generator with a private stream — so a
    later change that seeds the pandas side reproduces these positions rather
    than merely its own.
    """
    # Both suppressions are the same known-false finding under two tools: ruff
    # spells it S311, bandit B311. Neither is optional — ruff's `select` includes
    # "S" and does not ignore S311, so dropping the `noqa` reintroduces a lint
    # finding; bandit ignores `noqa` entirely, so without the `nosec` this line
    # sits in the security baseline as a finding every reader must re-dismiss.
    # The generator is *required* to be predictable: see this function's
    # docstring for why it is `random.Random` and not `secrets` or `numpy`.
    rng = random.Random(seed)  # noqa: S311 — seeded for parity, not secrecy (R10.3)  # nosec B311
    return {name: [row for row in range(num_rows) if rng.random() < _SYNTHETIC_NULL_RATE] for name in column_names}


def build_synthetic_frame_code(
    source_schema: dict | None,
    *,
    num_rows: int = _SYNTHETIC_FRAME_ROWS,
    seed: int = _SYNTHETIC_FRAME_SEED,
) -> str:
    """Return PySpark that binds :data:`FRAME_VARIABLE` to the synthetic frame.

    A **string of code**, not a DataFrame. This module runs where ``pyspark`` is
    not installed and must not be (design §3 do-NOT 7, `LGR-4dd9b42-1`); the
    code is submitted as the :data:`STAGE_FRAME_BUILD` statement and Spark
    imports what it needs on the session side. It is the same arrangement
    ``_build_test_data_code`` uses for pandas, which is why both are testable by
    reading what they emit.

    The emitted statement, in order:

    1. the ``pyspark.sql.types`` imports it uses and nothing else
    2. an explicit ``StructType``, one ``StructField`` per declared field, every
       field ``nullable=True`` — they have to be, ~5% of each column is null
    3. the seeded null row indices as literals (:func:`synthetic_null_rows`)
    4. one tuple per row, built by a comprehension over ``range(num_rows)``
    5. ``spark.createDataFrame(rows, schema)`` bound to :data:`FRAME_VARIABLE`

    ``spark`` is not created here because a Glue interactive session already
    provides it — the session's ``glueetl`` command bootstraps ``spark``, ``sc``
    and ``glueContext`` before the first ``RunStatement``, which is what design
    C10's measured 17 s first-Spark-action cost is the cost OF.

    **Stops at the bind, deliberately.** No ``GENERATED:`` print: the marker
    protocol is task 6.1's and design §3 do-NOT 8 forbids touching it from
    anywhere else. 6.1 appends its line to this statement and reports on
    :data:`FRAME_VARIABLE`.

    Raises :class:`FrameBuildError` — before returning any code, and therefore
    before any session is created — for a declared type outside the vocabulary.
    """
    columns = synthetic_frame_fields(source_schema)
    null_rows = synthetic_null_rows([name for name, _ in columns], num_rows=num_rows, seed=seed)

    # `json.dumps` for every emitted name, rather than the pandas builder's
    # bare f-string interpolation (`:513-518`). A column name containing a quote
    # or a backslash would otherwise emit code that does not parse, or parses as
    # a different name; JSON's escapes are valid Python string escapes, so this
    # is strictly safer and changes no name that was already safe. Task 6.2
    # generates brace-containing names against the marker parser for the same
    # class of reason.
    struct_fields = ",\n".join(
        f"    StructField({json.dumps(name)}, {spark_type}(), True)" for name, spark_type in columns
    )
    # One list entry per column, positionally aligned with `struct_fields` and
    # with the tuple expression below — the alignment is what lets the row
    # expression index by position instead of hashing a name per cell. The
    # trailing comma goes BEFORE the name comment and the join adds none of its
    # own: a `",\n".join` would put each comma after the comment that follows it,
    # where `#` swallows it, and adjacent list literals with no separator are
    # SUBSCRIPTS rather than a syntax error — so the emitted code would parse
    # and then fail at runtime with a `TypeError` naming neither this module nor
    # the column. Observed while building this; hence the comma placement and
    # hence `test_generated_frame_code_executes`, which runs the emitted code
    # instead of only parsing it.
    null_literals = "\n".join(f"    {null_rows[name]},  # {json.dumps(name)}" for name, _ in columns)
    row_values = ",\n".join(
        f"        (None if i in _nulls[{index}] else {_VALUE_EXPRESSIONS[spark_type]})"
        for index, (_, spark_type) in enumerate(columns)
    )

    return f"""\
from pyspark.sql.types import DoubleType, LongType, StringType, StructField, StructType
num_rows = {num_rows}
schema = StructType([
{struct_fields}
])
# Null row indices per column, drawn host-side from a fixed seed ({seed}) so that
# both verification executors see the same null positions (R10.3). Do not
# re-draw these in Spark: rand(seed) is seeded per partition.
_null_rows = [
{null_literals}
]
_nulls = [set(rows) for rows in _null_rows]
rows = [
    (
{row_values},
    )
    for i in range(num_rows)
]
{FRAME_VARIABLE} = spark.createDataFrame(rows, schema)
"""


# ── Frame construction: the S3 sample, and the scheme it needs ─────────
#
# Spec task 5.5, design §4.4's `sample` paragraph and C8. R9.1-R9.5.
#
# This is the arm that makes a run mean something without a declared schema, and
# it is also the arm with a standing invariant across it. Both halves in one
# place:
#
# **What it does.** `spark.read.<reader>(uri).limit(N)` for an `s3://` object.
# The columns come from the run's real input, which is the one thing the
# synthetic frame cannot supply from an absent schema — hence the conditional
# retirement in `frame_refusal_results` (R9.4).
#
# **What it does NOT do, and will not.** It reads NO JDBC source (R9.3). A JDBC
# read needs VPC configuration, a Glue connection and credentials;
# `LGR-819980f-1` (STANDING INVARIANT) makes credentials reachable only by
# Secrets Manager ARN, because plaintext user/password in Glue job arguments
# leaked a real target-database credential once, and `LGR-819980f-2` adds that
# `SECRET_ID` in `ConnectionProperties` is load-bearing while the
# `adi:secret_arn` tag is a read-path mirror only. Design C8's judgement is that
# handing a *verification* session any of that machinery widens the credential
# surface for a test, and the task 2.1 role is built to match: it has no Secrets
# Manager access at all, so a JDBC arm added here would not merely violate the
# invariant, it would fail with AccessDenied while doing so.
#
# A non-`s3://` URI is therefore an ORDINARY case: `schema_extract` writes
# `f"jdbc://pipeline/{pipeline_id}"` as its fallback
# (`schema_extract/lambda_function.py:1735`), so a JDBC-sourced pipeline's URI is
# non-`s3://` by construction. It falls back to `synthetic` and says so
# (R9.2) — it is not an error and must not be reported as one.

#: How many rows the sample is bounded to.
#:
#: Equal to :data:`_SYNTHETIC_FRAME_ROWS` by derivation rather than by
#: coincidence: ``input_records`` is one of the dimensions shadow mode compares,
#: and two frames of different heights would differ on it for a reason that has
#: nothing to do with either executor. Written as the other constant, not as
#: ``100``, so the two cannot drift apart in a later edit.
_SAMPLE_FRAME_ROWS = _SYNTHETIC_FRAME_ROWS

#: The scheme, spelled once. Compared as a literal PREFIX rather than parsed —
#: see :func:`is_s3_source_uri` for why that is deliberate and not lazy.
_S3_URI_PREFIX = "s3://"

#: Extension → canonical format. **`file_validation.FORMAT_MAP`'s content**
#: (`file_validation/lambda_function.py:55-69`), reproduced rather than imported
#: because `file_validation` is a different Lambda package: it is not in
#: `shared/`, so it is not on this Lambda's import path at runtime and importing
#: it would work in the test suite and fail in the container.
#:
#: Reproduced, so: **change both or neither.** The value of matching it is that
#: the format this runner reads a sample as is the format `file_validation`
#: recorded and the format the generated Glue job was templated for; a third
#: opinion here would verify the transform against a frame the job never sees.
#:
#: **p55 R5.1/R5.2 — "change both or neither" is now checked, and this table is
#: no longer a subset of what this runner can read.**
#: `tests/contracts/test_format_vocabulary_cross_lambda.py`'s mirror guard
#: asserts this mapping is `FORMAT_MAP` extension-for-extension in both
#: directions, so the instruction above fails a test rather than relying on a
#: reader noticing it. The `xlsx → "excel"` entry is the first member of the
#: **Conversion_Required_Set**: Spark reads no Excel workbook, so there is
#: deliberately NO `excel` entry in `_SAMPLE_READERS`, and
#: :func:`sample_read_format` declines a format this module cannot read rather
#: than resolving it. See that function for why the narrowing lives there and
#: not here.
_SOURCE_FORMAT_BY_EXTENSION: dict[str, str] = {
    "csv": "csv",
    "tsv": "csv",
    # p64 R3.1. Mirrors `FORMAT_MAP`'s new `txt → "csv"` entry — "change both or
    # neither", now checked by guard 6. No `_SAMPLE_READERS` entry is needed or
    # wanted: the `csv` reader carries `option("sep", {sep})` and
    # :func:`build_sample_frame_code` fills it from `delimiter_for_key(source_uri)`,
    # so a `.txt` sample is read with the pipe the generated Glue job will use.
    "txt": "csv",
    "json": "json",
    "jsonl": "json",
    "ndjson": "json",
    "parquet": "parquet",
    "pq": "parquet",
    "avro": "avro",
    "orc": "orc",
    # p55 R5.1. The Canonical_Format is `"excel"`, matching `FORMAT_MAP`'s value
    # exactly — a second spelling here would mean the sample was read as one
    # format while the generated Glue job was templated for another.
    "xlsx": "excel",
}

#: Compression suffixes to look through, from ``detect_format``'s own list
#: (`file_validation/lambda_function.py:70-77`): ``data.csv.gz`` is a csv.
_COMPRESSION_EXTENSIONS = frozenset({"gz", "bz2", "snappy", "zst"})

#: The reader expression per canonical format, over the URI literal.
#:
#: **Character-for-character the reader the generated Glue job uses** — the
#: `source_format` branch every one of the four S3-source templates carries
#: (`pyspark_templates/glue_job_s3_tables.py.j2:72-82` and its three siblings).
#: That is the whole point rather than a convenience: this executor exists to
#: verify the transform against the runtime the job will actually run on, and a
#: frame read with different options is a different frame. Two consequences are
#: inherited deliberately and are NOT bugs to fix here:
#:
#:   * ``inferSchema`` is **False** for csv, so every csv column arrives as a
#:     string. That is what the job does, so it is what the transform will face.
#:     "Improving" it here would verify against types the job never produces.
#:   * ``jsonl`` reads with ``multiLine`` True, because `FORMAT_MAP` collapses
#:     it onto ``json`` and the templates branch on the canonical name. It
#:     produces an odd frame — and produces the same odd frame in the real job.
#:
#: **p55 R4.1 amends the second bullet's other half.** ``tsv`` used to read
#: through the csv reader with no ``sep``, which made a TSV one column here for
#: the same reason it made it one column in the generated job. The templates now
#: pass ``.option("sep", …)``, so this map carries a second placeholder,
#: ``{sep}``, in the position the templates put
#: ``{{ source_delimiter | tojson }}``. :func:`build_sample_frame_code` fills it
#: from ``delimiter_for_key(source_uri)`` — the same total lookup
#: ``create_glue_pipeline``, ``create_code`` and ``schema_extract`` use, so all
#: four agree on what separates a source's fields. Entries without a ``{sep}``
#: are unaffected: ``str.format`` ignores a keyword no placeholder names.
#:
#: The right place to change either is the template plus `FORMAT_MAP`, together;
#: design §3 do-NOT 4's reasoning applies to the templates as it does to the
#: prompt.
_SAMPLE_READERS: dict[str, str] = {
    "csv": 'spark.read.option("header", True).option("inferSchema", False).option("sep", {sep}).csv({uri})',
    "json": 'spark.read.option("multiLine", True).json({uri})',
    "parquet": "spark.read.parquet({uri})",
    "avro": 'spark.read.format("avro").load({uri})',
    "orc": "spark.read.orc({uri})",
}


def is_s3_source_uri(source_uri: Any) -> bool:
    """Whether *source_uri* names an S3 object this runner may read (R9.1).

    **The gate on R9.3 and design C8**, so it is written to be read: ``True``
    here is the only route to a source read anywhere in this module, and every
    other answer falls back to a generated frame. ``LGR-819980f-1`` (**standing
    invariant**) is what that gate protects — a ``jdbc://`` URI must not merely
    be unhandled, it must be unreachable.

    Accepts ``Any`` rather than ``str`` on purpose. ``source_data_uri`` is read
    off a payload, so ``None`` and a non-string are both reachable inputs, and a
    predicate that raised ``AttributeError`` on them would turn an ordinary
    absent URI into an exception on the pre-flight path.

    **A literal ``s3://`` prefix, not a parsed scheme, and that is the
    decision.** ``urllib.parse.urlsplit`` looks like the right tool and is the
    wrong one twice over: it **lower-cases the scheme**, so ``S3://b/k`` would
    report ``s3``, and it needs no authority, so ``s3:/x`` would report ``s3``
    as well. Neither is a URI this repository produces — every producer writes
    ``s3://`` (``file_validation/lambda_function.py:633``) — so both are
    unknowns, and an unknown that Spark would then fail to open
    (``No FileSystem for scheme: S3``) is better answered by a generated frame
    than by a frame-build statement failure attributed to test data generation.
    Design §4.4 spells the condition ``s3://``, slashes included, for this
    reason.

    So, all of these are ``False``, each for a stated reason:

    ==================== ==========================================
    ``s3a://b/k``        a Hadoop scheme, not one anything here writes
    ``S3://b/k``         case is not normalised; see above
    ``s3:/b``            no authority, so no bucket
    ``s3://``            no bucket
    ``s3:///k``          no bucket
    ``jdbc://pipeline/x``  the ``schema_extract`` fallback (C8) — the ordinary case
    ``""`` / ``None``    absent
    ==================== ==========================================

    A key is **not** required: ``s3://bucket`` and ``s3://bucket/prefix/`` are
    real locations, and :func:`build_sample_frame_code`'s ``.limit`` bounds what
    is read from them. What such a URI usually lacks is a recognisable format,
    which is :func:`sample_read_format`'s answer rather than this one's — the two
    conditions are kept apart so that task 5.6's scheme property has a target
    that is *only* about the scheme.
    """
    if not isinstance(source_uri, str):
        return False
    if not source_uri.startswith(_S3_URI_PREFIX):
        return False
    bucket = source_uri[len(_S3_URI_PREFIX) :].split("/", 1)[0]
    # `strip()` because a whitespace bucket is not a bucket; S3 bucket names
    # cannot contain spaces, so this rejects nothing that was already valid.
    return bool(bucket.strip())


def sample_read_format(source_uri: str) -> str | None:
    """The canonical format to read *source_uri* as, or ``None`` if unknown.

    ``detect_format``'s algorithm (``file_validation/lambda_function.py:70-83``)
    over :data:`_SOURCE_FORMAT_BY_EXTENSION`, with one deliberate refinement: the
    extension is taken from the **last path segment** rather than from the whole
    key. ``detect_format`` scans the whole key, which for ``s3://b/my.dir/data``
    yields ``dir/data`` and therefore ``unknown`` — the same answer this gives,
    by a shorter route. The refinement can only ever agree with it or be more
    conservative, never less.

    **``None`` is a fallback, not an error, and that is a narrowing worth
    naming.** R9.2 keys the fallback on the *scheme*; this adds a second arm the
    requirements do not mention, so design Property 10 (task 5.6) is exactly
    true in the direction that matters — a read is attempted **only if** the
    scheme is ``s3`` — and narrower in the other: not every ``s3://`` URI is
    read. The reason is the folder-upload path, which is the observed instance
    rather than a hypothetical: ``file_validation`` writes
    ``original_s3_uri = source_s3_uri`` for a multi-file prefix
    (``file_validation/lambda_function.py:368``), and a prefix carries no
    extension.

    The alternative is what the templates do — branch to a json reader as the
    ``else`` — and it is **rejected here**. The templates can afford it because
    they are rendered with a ``source_format`` ``file_validation`` already
    classified, and an extension outside ``FORMAT_MAP`` never gets that far:
    ``LGR-60c4985-7`` records that it writes an ``unsupported`` audit row and
    raises. This function is given only a URI, so an unclassifiable one means
    "how to read this is not known". Reading it as json anyway would build a
    frame of Spark ``_corrupt_record`` rows, and the transform's failure against
    it would be reported at :data:`STAGE_FRAME_BUILD` — a harness limitation
    wearing a data-generation failure's clothes. A generated frame is the honest
    answer, and :attr:`FrameSource.unavailable_reason` is where it says so.

    **p55 R5.1 — a recognised extension is not the same fact as a readable one,
    and the second is what this function answers.** The mirror gained
    ``xlsx → "excel"`` so the two vocabularies stay in sync, and ``excel`` is a
    member of the Conversion_Required_Set: no Glue_Job_Template reads an Excel
    workbook, so :data:`_SAMPLE_READERS` deliberately has no entry for it and
    neither will it gain one. Without the second lookup below, an ``.xlsx`` URI
    would resolve to ``"excel"``, :meth:`GlueSessionTestRunner.resolve_frame_source`
    would return :data:`FRAME_MODE_SAMPLE`, and
    :func:`build_sample_frame_code` would raise :class:`FrameBuildError` — turning
    the documented fallback into a harness failure for a URI that should never
    have arrived in the first place.

    It should never arrive because ``schema_extract`` converts the workbook and
    routes the CONVERTED object's URI (p55 task 4.3, R13.3), so an Excel run's
    ``source_data_uri`` carries a ``.csv`` and resolves to ``csv`` here. This arm
    is what happens when that routing did not: a bounded sample of an object Spark
    cannot open is not available, so the run gets a generated frame and says a
    sample was requested and could not be had — the same answer, for the same
    reason, as an ``.xlsx`` gave before the vocabulary widened. The narrowing lives
    here rather than in the mirror because the mirror's job is to agree with
    ``FORMAT_MAP`` (R5.1) and this function's job is to select a reader.
    """
    basename = source_uri.rsplit("/", 1)[-1].lower()
    if "." not in basename:
        return None
    parts = basename.rsplit(".", 2)
    extension = parts[-1]
    if extension in _COMPRESSION_EXTENSIONS and len(parts) >= 3:
        extension = parts[-2]
    read_format = _SOURCE_FORMAT_BY_EXTENSION.get(extension)
    if read_format is not None and read_format not in _SAMPLE_READERS:
        # A Conversion_Required_Set member. Recognised, and unreadable by this
        # module — so "how to read this is not known" is still the true answer,
        # and it is the answer that keeps the fallback rather than raising.
        return None
    return read_format


def build_sample_frame_code(source_uri: str, read_format: str, *, num_rows: int = _SAMPLE_FRAME_ROWS) -> str:
    """Return PySpark that binds :data:`FRAME_VARIABLE` to a bounded sample (R9.1).

    A **string of code**, for the same reason :func:`build_synthetic_frame_code`
    returns one: ``pyspark`` is not installed here and must not be (design §3
    do-NOT 7, ``LGR-4dd9b42-1``). The statement is submitted as
    :data:`STAGE_FRAME_BUILD` and Spark resolves the reader on the session side.

    *read_format* is :func:`sample_read_format`'s output and selects the reader
    from :data:`_SAMPLE_READERS` — the generated Glue job's own reader for that
    format. Raises :class:`FrameBuildError` for a format outside that map, the
    same posture :func:`spark_type_for` takes: the default has to be one that
    cannot be mistaken for a verification.

    *source_uri* is emitted through ``json.dumps``, as every name in the
    synthetic builder is. A bucket or key containing a quote or a backslash
    would otherwise emit code that does not parse, or parses as a different
    object — and "a different object" is the worse of the two, since it would
    silently verify against something else.

    **This function does not check the scheme**, and callers must not read that
    as permission. :meth:`GlueSessionTestRunner.resolve_frame_source` is the one
    place that decides a sample is readable, and :func:`is_s3_source_uri` is the
    gate it uses (R9.3, C8, ``LGR-819980f-1``). Duplicating the check here would
    make two places responsible for an invariant, which is how one of them
    eventually stops being.

    **Stops at the bind, deliberately.** No ``GENERATED:`` print — the marker
    protocol is task 6.1's and design §3 do-NOT 8 forbids touching it from
    anywhere else.

    ``.limit`` is the bound and it is not uniform in cost: for parquet and orc it
    pushes down, while the csv and json readers still open the object to build a
    schema. What bounds *that* is :data:`_STATEMENT_DEADLINE_SECONDS`, after
    which the run becomes a :class:`GlueSessionError` and
    :func:`lifecycle_failure_results` — a harness failure that names itself,
    which is the correct outcome for "the source was too large to sample".
    """
    reader = _SAMPLE_READERS.get(read_format)
    if reader is None:
        raise FrameBuildError(
            f"no Spark reader is defined for format {read_format!r}; the known formats are "
            f"{sorted(_SAMPLE_READERS)}. Add it to _SAMPLE_READERS in glue_session_runner.py — "
            f"and to the matching branch of the pyspark_templates source read, or the sample "
            f"frame stops being the frame the generated job produces"
        )
    # p55 R4.1: `sep` is filled for every format, not only csv — `str.format`
    # ignores a keyword no placeholder names, so the four readers that carry no
    # `{sep}` are byte-identical to what they emitted before. `json.dumps`
    # rather than the raw character, matching the templates'
    # `{{ source_delimiter | tojson }}`: a tab must reach the generated code as
    # the two characters `\t` inside a string literal, never as a literal tab.
    # The two agree on `,` and on `\t` — Jinja's `tojson` differs from
    # `json.dumps` only on `<`, `>`, `&` and `'`, none of which is a delimiter
    # in `FORMAT_DELIMITERS`.
    return f"""\
# A bounded sample of the run's real source object (R9.1), read with the SAME
# reader the generated Glue job uses for this format — a frame read differently
# from the job's own read would verify a frame the job never sees.
{FRAME_VARIABLE} = (
    {reader.format(uri=json.dumps(source_uri), sep=json.dumps(delimiter_for_key(source_uri)))}
    .limit({num_rows})
)
"""


def build_frame_code(frame_source: FrameSource, source_schema: dict | None) -> str:
    """Return the frame-build statement for *frame_source* — the mode dispatch.

    One place the two arms of design §4.4 meet, so a caller cannot reach a
    sample read without having gone through
    :meth:`GlueSessionTestRunner.resolve_frame_source` and therefore through
    :func:`is_s3_source_uri` (R9.3).

    The dispatch is on :attr:`FrameSource.is_sample` and nothing else — not on
    the requested mode, and not on whether a URI happens to be present. A
    requested sample that fell back has ``is_sample`` false and gets the
    synthetic frame, which is exactly what R9.2 asks for.

    Raises :class:`FrameBuildError` — before any session exists — for a declared
    field type outside the vocabulary on the synthetic arm
    (:func:`spark_type_for`), and for a sample source that is somehow
    incomplete on the other. The second is unreachable:
    :meth:`FrameSource.__post_init__` refuses to construct it. It is checked
    anyway because the failure it would otherwise produce is the silent one —
    falling through to a synthetic frame while :attr:`FrameSource.is_sample`
    still reports true, which is the state that retires the no-source-schema
    refusal (R9.4) over generated data.
    """
    if not frame_source.is_sample:
        return build_synthetic_frame_code(source_schema)

    source_uri = frame_source.source_uri
    read_format = frame_source.read_format
    if source_uri is None or read_format is None:  # pragma: no cover — __post_init__ refuses it
        raise FrameBuildError(
            f"frame source claims mode {FRAME_MODE_SAMPLE!r} with source_uri={source_uri!r} and "
            f"read_format={read_format!r}; falling back to a synthetic frame here would retire "
            f"the absent-schema refusal over generated data (R9.4/R9.5)"
        )
    return build_sample_frame_code(source_uri, read_format)


# ── The transform's ambient namespace ──────────────────────────────────
#
# Spec `real-spark-verification`, defect 3. R1.4.
#
# **The defect this closes.** Run `run-2026-08-18-18-52-03-a8de6fca` (pipeline
# in account 777788889999, `us-west-2`) got past defect 2's `IndentationError`:
# the statement parsed and Spark evaluated it. It then failed with
#
#     Glue statement failed at stage 'transform execution'
#     Glue transform execution failed: NameError: name 'col' is not defined
#
# The transform block references `col`, `lit`, `initcap`, `regexp_replace` and
# friends as BARE names, and it is right to — inside the generated Glue job those
# are bound at MODULE scope by the import line every `pyspark_templates/*.j2`
# carries. This runner submits only the transform block, into a session whose
# namespace was built by the two statements before it: the frame build imports
# `pyspark.sql.types` for its own `StructType` (`build_synthetic_frame_code`) and
# the validation statement imports `pyspark.sql.functions` **as `_F`**
# (`build_validation_code`) — namespaced deliberately, so it binds no bare `col`,
# and running after the transform anyway. Nothing bound the job's ambient names,
# so the first argument the transform evaluated was undefined.
#
# **THIS IS NOT A TRANSLATION, and the next reader will ask.** R1.4 requires the
# Transform_Block to execute untranslated, and it still does: no construct is
# inspected, no name is rewritten, no line is added to or removed from the block,
# and the block's own statement is byte-identical to what
# `transform_statement` produced before this change. What is added is the
# *environment* the block was written against — reproducing the ambient
# namespace of the runtime the code will really run in is the opposite of
# rewriting the code. `dedent_for_statement`'s docstring makes the same
# distinction about columns; this is the same distinction about scope.
#
# **Why the aliases are reproduced exactly rather than flattened.** The templates
# import `count as spark_count` and `max as spark_max`, so bare `count` and bare
# `max` are NOT bound at module scope in the generated job. Emitting them here
# would hand the verification session a name the job does not have, and a
# transform using bare `count()` would then verify green and fail in production
# — the same class of false pass this section exists to prevent, pointing the
# other way. Emit what the templates bind and nothing else.
#
# **The union, and the one imprecision it buys.** Four templates import the base
# function list; `glue_job_jdbc_source.py.j2:52` adds `max as spark_max`. This
# emits the UNION, because the runner is not told which template the job will be
# rendered from — `run_test`'s arity is fixed at three by R9.6 and the target
# type is not among them. So a transform referencing `spark_max` verifies here
# and would fail in a job rendered from one of the other four. Stated rather than
# hidden: it is a *permissive* imprecision on one name, against a defect that
# currently fails EVERY run, and closing it needs the target type plumbed to the
# constructor. Revisit if a `spark_max` transform ever reaches a non-JDBC-source
# target.
#
# **Scoped to the two expression-vocabulary lines, on purpose.** The templates'
# other module-scope imports are job scaffolding, not transform vocabulary:
# `SparkContext`, `GlueContext` and `Job` construct the job, `DynamicFrame` is
# used only in the write partials AFTER the transform block
# (`_partials/_write_glue_catalog.py.j2:3`), and `sys`/`logging`/`json`/`boto3`/
# `datetime`/`jdbc_helpers` are not expression vocabulary. `from
# awsglue.transforms import *` is also a star import, so "every module-scope
# name" is not statically enumerable and a test claiming it would be false by
# construction. `tests/contracts/test_transform_namespace_matches_templates.py`
# fences the two lines that ARE reproduced.

#: The `pyspark.sql.functions` names the templates bind at module scope, in the
#: templates' own order, each spelled as it appears in the import list — so an
#: aliased name carries its `as` clause and binds only the alias.
#:
#: **Kept as data, not as one long emitted literal**, so the contract test can
#: compare bound NAMES rather than string-matching a line, and so a template
#: reordering its list does not read as a diff here.
TRANSFORM_FUNCTION_IMPORTS: tuple[str, ...] = (
    "col",
    "lit",
    "trim",
    "lower",
    "to_date",
    "to_timestamp",
    "when",
    "concat_ws",
    "count as spark_count",
    "regexp_replace",
    "upper",
    "initcap",
    "substring",
    "coalesce",
    "length",
    "split",
    "array_contains",
    "expr",
    # `glue_job_jdbc_source.py.j2:52` only — see the union note above.
    "max as spark_max",
)

#: The `pyspark.sql.types` names the templates bind at module scope. Identical
#: across all five templates.
#:
#: Included for the same reason as the functions and not as an afterthought: the
#: generated job's auto-cast block uses these, and a transform block is free to
#: write `.cast(DateType())` — which would fail with the identical `NameError` on
#: a different name. Overlaps `build_synthetic_frame_code`'s own types import on
#: three names (`StringType`, `LongType`, `DoubleType`) and that is harmless: a
#: repeated `from … import` is idempotent, and the two lists are different sets
#: for different reasons (that one needs `StructField`/`StructType`, which no
#: template imports).
TRANSFORM_TYPE_IMPORTS: tuple[str, ...] = (
    "StringType",
    "IntegerType",
    "LongType",
    "DoubleType",
    "BooleanType",
    "DateType",
    "TimestampType",
)


def transform_namespace_bound_names() -> frozenset[str]:
    """The names :func:`build_transform_namespace_code` actually binds.

    An ``X as Y`` entry binds ``Y`` and not ``X``, which is the whole reason this
    is a function rather than a set literal: reading the bound name off the
    ``as`` clause in one place means the contract test and the emitted statement
    cannot disagree about what an alias does.
    """
    return frozenset(
        entry.split(" as ")[-1].strip() for entry in (*TRANSFORM_FUNCTION_IMPORTS, *TRANSFORM_TYPE_IMPORTS)
    )


def build_transform_namespace_code() -> str:
    """Return the import lines that reproduce the generated job's namespace.

    A **string of code**, like every builder in this module and for the same
    reason: ``pyspark`` is not installed here and must not be (design §3 do-NOT
    7, ``LGR-4dd9b42-1``). So this is testable by reading what it emits.

    **Not an import in this module.** The names are bound in the Glue session, by
    text submitted to it. Importing ``pyspark.sql.functions`` here would fail at
    module import and take the whole Lambda with it.

    **Not read from the templates at run time either**, though they do ship to
    ``/opt/python/pyspark_templates`` (``compute-stack.ts:197``). Parsing Jinja
    on the verification path would make the transform statement's correctness
    depend on file I/O and a template parse inside the container, adding a
    failure mode to the path this exists to make work. The drift risk that buys
    is answered where ``LGR-28b4d75-5`` (**template / ratified**) says to answer
    it — in a contract test that parses the producer, because "a test written
    from the producer's output cannot detect the producer drifting".
    """
    return (
        f"from pyspark.sql.functions import {', '.join(TRANSFORM_FUNCTION_IMPORTS)}\n"
        f"from pyspark.sql.types import {', '.join(TRANSFORM_TYPE_IMPORTS)}\n"
    )


# ── The marker protocol: GENERATED and VALIDATION ──────────────────────
#
# Spec task 6.1, design §4.9 and C7's table. R1.3, R10.1, R10.2, R10.6.
#
# **This section EMITS the protocol; it does not define it.** `_parse_results`
# defines it, is imported verbatim above, and is not touched — design §3 do-NOT 8.
# That asymmetry is the whole reason this change is an executor swap rather than a
# contract change: both executors print the same two markers, one parser reads
# them, and so a shadow-mode disagreement is attributable to the EXECUTOR rather
# than to the parser. A second parser here, however faithful, would make every
# difference ambiguous between the two.
#
# **ACTION ACCOUNTING, because R10.6 is about cost and not about tidiness.**
# Spark is lazy where pandas is eager, so the count of *actions* is a real
# property of these statements and is stated rather than left to be inferred:
#
#   | statement                   | Spark actions | what pays for them            |
#   |-----------------------------|---------------|-------------------------------|
#   | frame build + `GENERATED:`  | 1             | `df.count()` in the marker    |
#   | transform                   | 0 or more     | the AI's code, not ours       |
#   | validation + `VALIDATION:`  | **1**         | one `select(...).collect()`   |
#
# One action for the whole validation payload is R10.6, and it is achieved by
# putting `record_count`, the distinct-row count and every per-column null count
# into ONE projection. `dtypes`, `missing_columns` and `schema_compliance` cost
# ZERO actions — they are derived from the schema and from a declared list, not
# from the data. Computing the four metrics with four separate aggregations would
# be four actions over the same frame, which on an uncached DataFrame means
# recomputing the transform four times.
#
# The frame build's one action is deliberate rather than incidental. C10 measured
# the first Spark action on a session at 17 s (executor warmup, paid once); paying
# it in :data:`STAGE_FRAME_BUILD` means that if warmup ever blows
# :data:`_STATEMENT_DEADLINE_SECONDS` the failure is attributed to the frame build
# instead of to the transform, which is the correct attribution. And a *sample*
# frame's row count is not knowable host-side — `.limit(N)` is a ceiling, and the
# object may hold fewer rows — so `record_count` has to be counted rather than
# assumed. `_parse_results` reads it as `input_records`.
#
# **WHERE THE VALUES COME FROM, and the one thing that must not be done here.**
# The arithmetic below is derived from the metric *definitions* in R10.2 and
# design §4.9 — completeness is the proportion of non-null cells, uniqueness the
# proportion of rows that are not exact duplicates of an earlier row,
# schema_compliance the proportion of declared target columns present — and from
# task 1.1's characterization of those sentences
# (`tests/unit/lambdas/test_code/test_dq_definitions_characterization.py`). It is
# NOT derived from what the pandas path currently prints, because that output is
# the approximation being replaced and deriving from it would pin the bug (R10.5).
#
# So one number MOVES, on purpose, and it is the re-baseline this section exists
# to deliver: pandas `isna()` is True for `None` **and** for a float `NaN`, while
# Spark's `isNull()` is False for `NaN`. A `NaN` is a present IEEE value, not an
# absent cell, so under R10.2's sentence it is not a null and completeness rises.
# Task 1.1 measured both sides on its `NULLS_INCLUDING_NAN` frame: the definition
# says `amount` has **1** null and completeness is **83.3**; the pandas path
# reports **2** and **75.0**. C7 predicts exactly that direction. `uniqueness` and
# `schema_compliance` move on no frame task 1.1 built.

#: The two marker names, spelled once. ``_parse_results`` scans for
#: ``f"{marker}:"`` followed by a brace-balanced JSON object
#: (``code_interpreter_runner.py:599-614``); these are the two markers it looks
#: for. Spelled here rather than imported because they are string *literals*
#: inside that function's body, not constants — so what actually guards the pair
#: against drifting is a round-trip through the real parser, which is task 6.2's
#: property and is also asserted directly in
#: ``test_validation_statement.py``.
_GENERATED_MARKER = "GENERATED"
_VALIDATION_MARKER = "VALIDATION"

#: The message the validation statement prints and raises with when the transform
#: bound no ``df_transformed``. **Character-for-character the pandas path's**
#: (``code_interpreter_runner.py:553-554``), so the two executors' reports read
#: identically and `_parse_results` puts the same text in ``error_details``.
#: A second spelling of a literal that lives inline in the other builder, so
#: ``test_validation_statement.py`` asserts the pandas builder still emits it.
_MISSING_TRANSFORM_FRAME_ERROR = "transform did not produce df_transformed"

#: The nine keys R10.1 pins — the key set the pandas path emits and the key set
#: ``_parse_results`` reads. Declared as data so the emitted payload and the
#: assertion about it cannot disagree, and so task 6.3's executor-independence
#: property has something to compare against.
#:
#: ``dtypes`` is in the set and its VALUES are deliberately not pinned anywhere.
#: R10.1 requires the key; C7 predicts every entry moves, because the two
#: vocabularies are disjoint (``object``/``int64``/``float64`` against
#: ``string``/``bigint``/``double``) — and task 1.1 records that the pandas side is
#: not even one vocabulary, since a string column reports ``object`` on pandas
#: 2.2.3 and ``str`` on 3.0.5 with nothing pinning which pandas the managed
#: interpreter runs. Emit the key; do not try to make the values agree. Shadow
#: mode (task 10.1) measures the pair actually in use rather than predicting it.
VALIDATION_PAYLOAD_KEYS: tuple[str, ...] = (
    "record_count",
    "null_counts",
    "dtypes",
    "duplicates",
    "schema_compliance",
    "completeness",
    "uniqueness",
    "overall_quality",
    "missing_columns",
)

#: Payload key → the expression in the emitted statement that supplies it.
#:
#: **Eight of the nine are the identity mapping and the ninth is not**, which is
#: the whole reason this is written out instead of generated from
#: :data:`VALIDATION_PAYLOAD_KEYS`: the pandas path emits
#: ``"missing_columns": missing`` (``code_interpreter_runner.py:574``), so a key
#: that names its own variable is a coincidence holding for eight keys and not a
#: rule. Generating the pair from the key emitted ``missing_columns`` as a
#: variable name, which parses cleanly and fails at run time with a ``NameError``
#: naming neither this module nor the payload — the same class of parse-clean
#: defect the synthetic frame builder's comma-placement note records, and caught
#: the same way, by executing the emitted code
#: (``test_validation_statement.py::test_emitted_statement_executes``).
_VALIDATION_PAYLOAD_EXPRESSIONS: dict[str, str] = {
    "record_count": "record_count",
    "null_counts": "null_counts",
    "dtypes": "dtypes",
    "duplicates": "duplicates",
    "schema_compliance": "schema_compliance",
    "completeness": "completeness",
    "uniqueness": "uniqueness",
    "overall_quality": "overall_quality",
    "missing_columns": "missing",
}


def declared_target_columns(target_schema: dict | None) -> list[str]:
    """Return the declared target column names, in declaration order.

    Mirrors ``_build_validation_code``'s one-line reading
    (``code_interpreter_runner.py:546``) — ``properties`` if the KEY is present,
    else ``fields``, else nothing — because ``schema_compliance`` is the one
    metric design §2 says does **not** move between executors, and it would move
    immediately if the two paths disagreed about which columns were declared.

    Mirrored rather than imported, and that is a cost this notes rather than
    hides: the reading is an inline expression inside the pandas builder, not a
    shared helper, and extracting it would modify a module R3.3 and design §3
    do-NOT 1 require to stay unmodified. So this is a second spelling, and
    ``test_validation_statement.py`` pins the two against each other over the
    shapes that distinguish them — ``properties``, ``fields``, both, neither, and
    the p33 3.14 shape where an EMPTY ``properties`` beats a populated ``fields``
    because the key is present.

    ``None`` is accepted where the pandas builder would raise, since a target
    schema is optional on this path and "nothing declared" is the honest reading
    of its absence — the same answer ``{}`` gives.
    """
    if not target_schema:
        return []
    declared = target_schema.get("properties", target_schema.get("fields", {}))
    if not isinstance(declared, dict):
        # A `fields` LIST is a shape `shared/target_schema_resolution.py` accepts
        # elsewhere; the pandas builder refuses it too, by a different route —
        # `AttributeError` on `list.keys` raised HOST-side inside
        # `_build_validation_code` (`code_interpreter_runner.py:546`), before any
        # code string is returned, not as a validation-stage failure inside the
        # generated code. Different exception and different stage, same
        # conclusion: both executors refuse a list-shaped `fields` before a
        # session exists. Pinned by
        # `test_validation_payload_keys_prop.py::test_the_pandas_builder_also_refuses`,
        # which asserts the raise on the builder CALL. Reading it as "nothing
        # declared" here would instead report a vacuous 100.0
        # `schema_compliance`, which is the one outcome R12.4 exists to prevent.
        raise FrameBuildError(
            f"declared target columns must be a mapping of column name to metadata, got "
            f"{type(declared).__name__} — a list-shaped `fields` would otherwise be read as "
            f"'nothing declared' and report a vacuous schema_compliance of 100.0"
        )
    return list(declared.keys())


def build_generated_marker_code(frame_variable: str = FRAME_VARIABLE) -> str:
    """Return the ``GENERATED:`` print line for a bound frame (R1.3).

    Reports on *frame_variable* — :data:`FRAME_VARIABLE`, the name both frame
    builders bind and the name the AI's transform reads. The two keys are the
    pandas path's (``code_interpreter_runner.py:532``): ``record_count``, which
    ``_parse_results`` reads as ``input_records``, and ``columns``, which it
    ignores today and which is emitted anyway because R1.3 asks for the marker
    *in the form the parser reads* rather than for a minimal subset of it.

    ``len(df)`` becomes ``df.count()`` — C7's fourth row. Value-equivalent, but an
    **action** rather than a property, and it is the frame-build statement's one
    action. See the section header for why paying the first-action warmup here is
    the right attribution, and why a sample frame's row count cannot be assumed.

    A separate function rather than a line inside either frame builder: both
    builders stop at the bind deliberately (their docstrings say so) so that the
    marker protocol lives in exactly one place. :func:`frame_build_statement` is
    where the two are joined.
    """
    return (
        "import json\n"
        f'print("{_GENERATED_MARKER}:" + json.dumps('
        f'{{"record_count": {frame_variable}.count(), "columns": list({frame_variable}.columns)}}))\n'
    )


def frame_build_statement(frame_source: FrameSource, source_schema: dict | None) -> str:
    """Return the full :data:`STAGE_FRAME_BUILD` statement: frame code + marker.

    The composition point the frame builders' docstrings promise — "6.1 appends
    its line to this statement and reports on ``FRAME_VARIABLE``".
    :func:`build_frame_code` stays a pure mode dispatch and keeps emitting no
    marker, so what each builder produces is still checkable on its own; this is
    the one thing that submits.

    Raises :class:`FrameBuildError` for everything :func:`build_frame_code` does,
    unchanged and from the same pre-session position: appending a print cannot
    fail, so this adds no failure mode of its own.

    **It also carries the transform's ambient namespace** (defect 3, R1.4) —
    :func:`build_transform_namespace_code`, between the frame code and the
    marker. Three placements were available and this is the one that costs
    nothing:

    * **Here**, folded into a statement that is already session setup, which is
      what this stage is. Chosen.
    * **A fourth statement.** Rejected, and not on taste: the shadow half's
      pre-flight budget ``SHADOW_GLUE_WORST_CASE_SECONDS``
      (``lambda_function.py:627``) is ``180 + 3 × 120 + 120`` — *three*
      statements — and the fence that guards it against drift
      (``test_shadow_orchestration.py:232``) hardcodes the same ``3 *``. So a
      fourth statement would raise the real worst case to 780 s while both the
      constant and its fence still said 660, silently. A failing test would have
      been fine; an invalidated budget that still passes is not.
    * **Prepended to the transform statement.** Rejected because
      ``transform_statement`` is asserted to be the dedented block and nothing
      else, line for line
      (``test_transform_statement_indentation.py::test_a_real_renders_block_keeps_every_statement_unchanged``).
      Keeping that property exactly true is worth more than the placement.

    Position *within* this statement is after the frame code and before the
    marker, so ``build_synthetic_frame_code``/``build_sample_frame_code`` remain
    this statement's leading text — which is what
    ``test_frame_refusal_precedes_session_prop.py:526`` and
    ``test_sample_frame_dispatch.py:617`` assert with ``startswith``, and those
    assertions keep their strength rather than being relaxed to ``in``.

    **What the chosen placement costs**, stated rather than discovered later: a
    failure in those import lines is reported at :data:`STAGE_FRAME_BUILD`, not
    at :data:`STAGE_TRANSFORM`. Accepted, because such a failure means a name in
    :data:`TRANSFORM_FUNCTION_IMPORTS` does not exist in the session's PySpark —
    which is a harness/environment fact about setting the session up, not a fact
    about the AI's transform, so the frame-build stage is arguably the *better*
    attribution. It is also the same reasoning the section header above already
    applies to the frame build's first Spark action. And it is narrow: the
    templates issue the identical imports in the real job, so any name that fails
    here would fail the generated job too.
    """
    return (
        build_frame_code(frame_source, source_schema) + build_transform_namespace_code() + build_generated_marker_code()
    )


def build_validation_code(target_schema: dict | None) -> str:
    """Return PySpark that validates the transform output and prints ``VALIDATION:``.

    The Spark counterpart of ``_build_validation_code``
    (``code_interpreter_runner.py:535-576``): the identical nine keys
    (:data:`VALIDATION_PAYLOAD_KEYS`, R10.1), computed per design C7's table, in
    **one** ``select`` so the payload costs one Spark action (R10.6).

    A **string of code**, like both frame builders, for the same reason:
    ``pyspark`` is not installed here and must not be (design §3 do-NOT 7,
    ``LGR-4dd9b42-1``). What it emits is therefore testable by reading it and by
    executing it against a stub, which is what
    ``tests/unit/lambdas/test_code/test_validation_statement.py`` does.

    The emitted statement, in order:

    1. ``_df = df_transformed`` as the ONLY binding, with the pandas path's
       ``NameError`` arm reproduced verbatim — see below
    2. the column list and its backtick-quoted forms
    3. ONE ``select`` carrying every aggregate the payload needs
    4. the metric arithmetic, from the R10.2 definitions
    5. the ``VALIDATION:`` print

    **The ``df_transformed`` arm is reproduced, not re-decided.** P28 R10.4: the
    raw ``df`` is always still in scope, so any fallback to it would validate the
    INPUT and call it the output — validation theater. The emitted code prints
    ``VALIDATION:{"error": …}`` and then raises with the same message, so
    ``_parse_results`` reports ``execution_successful`` false with the text in
    ``error_details``, and Glue reports ``Output.Status`` as ``error`` so
    :func:`statement_failure_results` sees a failed statement. Both halves matter
    and both are the pandas path's, down to the message string
    (:data:`_MISSING_TRANSFORM_FRAME_ERROR`).

    **The four expression substitutions, and which one changes a value.** Design
    C7's table:

    * ``_df[c].isna().sum()`` → ``count(when(col(c).isNull(), 1))``. **This one
      moves.** pandas counts a float ``NaN`` as null; Spark does not. A ``NaN`` is
      a present IEEE value, so under R10.2 it is not an absent cell and
      completeness rises. Measured on both sides by task 1.1.
    * ``_df.duplicated().sum()`` → ``count() - count_distinct(struct(*cols))``.
      ``count_distinct`` is the snake_case name PySpark has carried since 3.2;
      Glue 5.0 runs Spark **3.5.4-amzn-0** (C10, measured on the probe session),
      so it is available. A session on an older Glue version would need
      ``countDistinct`` instead — which is a reason to notice the version, not to
      write a fallback for a version this design does not create sessions on.
      Agrees on exact duplicates. The ``struct`` is load-bearing: SQL
      ``count(DISTINCT a, b)`` **drops** any row with a null argument, which would
      report a frame full of nulls as having one distinct row; a ``struct`` built
      by ``struct()`` is itself never null, so every row is counted. Spark
      normalises ``NaN`` in grouping keys, so a row carrying ``NaN`` is a
      duplicate of itself — which agrees with both the definition oracle and the
      recorded pandas observation on task 1.1's
      ``DUPLICATE_ROWS_CONTAINING_NAN``. That agreement is **reasoned from
      documented semantics, not measured**: no Spark runs in this repository.
    * ``str(_df[c].dtype)`` → ``dict(_df.dtypes)``. Every entry moves; no value is
      pinned. See :data:`VALIDATION_PAYLOAD_KEYS`.
    * ``len(_df)`` → ``count(lit(1))`` inside the same select. Value-equivalent,
      and free here because the select is being paid for anyway.

    **Zero declared target columns reproduces the vacuous 100.0, knowingly.**
    R10.2's sentence — "the proportion of declared target columns present" — does
    not decide a proportion over an empty set, and task 1.1 records that gap
    explicitly: its ``NO_DECLARED_TARGETS`` frame carries the ``100.0`` as an
    *observation* only, and its oracle raises rather than answering. The pandas
    path short-circuits to ``100.0`` (``:563``), and this emits the identical
    ``if expected else 100.0``. Reproducing it is deliberate, because R12.4's
    "report a score only where validation ran" gate lives in
    ``aggregate_check_results`` and not in this arithmetic — changing the number
    here would move a value the requirements do not decide, in a place that
    cannot see whether anything downstream will report it.
    """
    expected_str = json.dumps(declared_target_columns(target_schema))
    payload_entries = "\n".join(
        f'    "{key}": {_VALIDATION_PAYLOAD_EXPRESSIONS[key]},' for key in VALIDATION_PAYLOAD_KEYS
    )
    # No blank lines anywhere in the emitted statement, and no `def`: a Glue
    # `RunStatement` is executed as a cell, and a blank line inside an indented
    # block is the shape that terminates a block early in a REPL-style executor.
    # The pandas builder has none either.
    return f"""\
import json
from pyspark.sql import functions as _F
try:
    _df = df_transformed  # P28 R10.4: validate the TRANSFORMED frame — never raw df
except NameError:
    print("{_VALIDATION_MARKER}:" + json.dumps({{"error": "{_MISSING_TRANSFORM_FRAME_ERROR}"}}))
    raise RuntimeError("{_MISSING_TRANSFORM_FRAME_ERROR}") from None
_cols = list(_df.columns)
# Backtick-quote every column reference, doubling a backtick inside the name.
# `F.col` PARSES its argument, so an unquoted `a.b` resolves as struct-field
# access on `a` rather than as the column literally named `a.b`.
_quoted = ["`" + _name.replace("`", "``") + "`" for _name in _cols]
# R10.6 — ONE select, therefore ONE Spark action for the whole payload:
# [0] the row count, [1] the distinct-row count, [2:] one null count per column.
# The row identity is a struct because SQL `count(DISTINCT a, b)` drops any row
# with a null argument, while a struct built by `struct(...)` is never null.
_row_identity = _F.struct(*[_F.col(_ref) for _ref in _quoted]) if _quoted else _F.lit(1)
_aggregates = [_F.count(_F.lit(1)), _F.count_distinct(_row_identity)] + [
    _F.count(_F.when(_F.col(_ref).isNull(), _F.lit(1))) for _ref in _quoted
]
# Read POSITIONALLY, not by alias: an alias derived from a column name would need
# escaping of its own, and the projection order is already the contract.
_agg = _df.select(*_aggregates).collect()[0]
record_count = int(_agg[0])
# C7: Spark `isNull()` is False for a float NaN, where pandas `isna()` is True.
# A NaN is a present value, not an absent cell (R10.2), so this counts fewer
# nulls than the pandas path and completeness is correspondingly higher. That
# movement is the re-baseline, measured on both sides by spec task 1.1.
null_counts = {{_name: int(_agg[_index + 2]) for _index, _name in enumerate(_cols)}}
# Schema-derived: costs no action. Values are Spark's vocabulary and are
# deliberately not pinned to the pandas path's — C7 predicts every entry moves.
dtypes = dict(_df.dtypes)
duplicates = record_count - int(_agg[1])
expected = {expected_str}
missing = [c for c in expected if c not in _cols] if expected else []
completeness = round((1 - sum(null_counts.values()) / max(record_count * len(_cols), 1)) * 100, 1)
uniqueness = round((1 - duplicates / max(record_count, 1)) * 100, 1)
schema_compliance = round((1 - len(missing) / max(len(expected), 1)) * 100, 1) if expected else 100.0
overall_quality = round((completeness + uniqueness + schema_compliance) / 3, 1)
print("{_VALIDATION_MARKER}:" + json.dumps({{
{payload_entries}
}}))
"""


def _leading_whitespace(line: str) -> str:
    """The line's leading whitespace, as text rather than as a width.

    Text and not a count because the prefix is compared by
    :meth:`str.startswith` below, so a tab and four spaces must not be
    interchangeable: treating them as equal widths would strip a prefix a line
    does not actually carry and corrupt the statement.
    """
    return line[: len(line) - len(line.lstrip())]


def dedent_for_statement(code: str) -> str:
    """Strip the block's own leading indentation so it can stand at top level.

    A Glue statement is submitted into a **fresh top-level scope** with no
    enclosing block, so a leading indent is an ``IndentationError`` raised by
    the interpreter before Spark sees anything. The transform block that
    reaches :func:`transform_statement` is indented, because
    ``lambda_function.extract_transform_block`` (``:178``) returns
    ``code[start_idx:end_idx].rstrip()`` — a raw slice that keeps the four
    spaces the block carries inside the generated job, where every
    ``pyspark_templates/*.j2`` renders it as
    ``{{ transform_block | indent(4, first=False) }}`` one level inside
    ``main()``'s ``try:``.

    **This does not weaken R1.4's "executed as written".** Removing a common
    leading indent changes no statement, no identifier, no literal and no
    evaluation order — it re-anchors the block at the column the submission
    scope actually starts at. It is also the house treatment of this exact
    input rather than a new idea: the pandas proxy has dedented since it was
    written (``pyspark_to_pandas.py:1948``, ``textwrap.dedent(pyspark_code)``),
    and its docstring at ``:1927`` gives the same reason — "the real template
    output indents the transform block (4 or 8 spaces inside ``main()``), and
    the emitted pandas code must be valid top-level Python". That path silently
    absorbed the indentation for the whole life of the codebase, which is why
    the defect surfaced only when ``glue`` became the active executor.

    **Not ``textwrap.dedent`` alone, and the reason is a real input shape.**
    ``dedent`` strips the longest prefix *common to every non-blank line*, so a
    single line at a shallower column reduces that prefix to nothing and the
    block is returned still indented — a silent no-op that fails exactly as
    before. Three shapes reach that:

    * A **comment line at a shallower column**. Python's tokenizer emits no
      ``INDENT``/``DEDENT`` for a comment-only line, so ``# note`` at column 0
      inside an indented block is valid input, and Jinja's ``indent`` filter
      does not rescue it either — the filter indents rendered lines, not lines
      the model wrote at column 0 in a block the template then re-indents as a
      whole.
    * **The no-end-marker fallback.** ``extract_transform_block`` returns
      ``code[start_idx:]`` when the closing marker is absent, and the tail of
      every template carries ``except Exception as e:`` at column 0
      (``glue_job_s3_parquet.py.j2:120``). First line at 4, later lines at 0.
    * **Tabs against spaces.** ``dedent`` compares prefixes as text, so a block
      mixing them has no common prefix at all.

    So the anchor here is the **first significant line's** own indentation —
    significant meaning neither blank nor comment-only. In valid Python the
    first statement of a block sits at that block's base column and no later
    *statement* line can be shallower, so that prefix is the one to remove;
    anything shallower than it is a comment or a continuation line, and is
    left-stripped rather than left to defeat the whole operation. Relative
    depth is preserved for every line that carries the prefix, so nested
    ``if``/``for`` bodies inside the transform keep their structure.

    Two properties this deliberately keeps:

    * **A block already at column 0 is returned unchanged**, byte for byte —
      including the whole-file fallback ``extract_transform_block`` returns
      when the opening marker is absent, which is already valid top-level
      Python and must not be touched.
    * **Blank lines are emptied rather than preserved**, matching
      ``textwrap.dedent``'s own normalisation of whitespace-only lines, so a
      line of trailing spaces inside the block cannot read as an indent.

    Known and accepted: a multi-line string **literal** whose continuation
    lines carry the prefix is dedented with everything else, so its content
    changes. ``textwrap.dedent`` has the same behaviour and the pandas path has
    lived with it since it was written; a literal-aware dedent needs a
    tokenizer pass, and the generated transform's only multi-line literals are
    the ones the templates render at the block's own indent.
    """
    lines = code.split("\n")

    prefix: str | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        prefix = _leading_whitespace(line)
        break

    # No significant line — an empty block, or comments only. There is no
    # anchor to read, so fall back to `textwrap.dedent`, which on this input
    # normalises the blank lines and is otherwise a no-op. Returning `code`
    # untouched would leave an all-comment block indented; a comment at an
    # indent is not a syntax error, but the two paths should not disagree.
    if prefix is None:
        return textwrap.dedent(code)

    if not prefix:
        return code

    width = len(prefix)
    out: list[str] = []
    for line in lines:
        if not line.strip():
            # `textwrap.dedent`'s own normalisation of whitespace-only lines.
            out.append("")
        elif line.startswith(prefix):
            out.append(line[width:])
        else:
            # Shallower than the anchor, or indented with different whitespace:
            # a comment-only line or a continuation. Left-stripped rather than
            # left in place, because leaving it is what turns the whole dedent
            # into a no-op on an otherwise valid block.
            out.append(line.lstrip())
    return "\n".join(out)


def transform_statement(transform_block: str) -> tuple[str, str]:
    """Return the ``(stage, code)`` pair for the transform stage.

    :data:`STAGE_TRANSFORM` paired with the block itself, in the same
    ``[(stage_label, code), …]`` shape :func:`validation_statement` returns and
    :meth:`GlueSessionTestRunner.run_guarded_session` takes — one place the
    label and the code it carries are joined, so the transform cannot be
    submitted under the wrong stage and mis-attribute its failure.

    The block passes through :func:`dedent_for_statement` and through nothing
    else. That is the whole body of this function and the whole of the
    difference from submitting *transform_block* directly: no construct is
    inspected, no vocabulary is consulted, no line is rewritten. Read
    :func:`dedent_for_statement`'s docstring before changing this — in
    particular for why the normalisation is not a translation, and why the
    equivalent fix does **not** belong in
    ``lambda_function.extract_transform_block``, whose output also feeds the
    pandas converter (which dedents again) and the reviewer prompt.
    """
    return (STAGE_TRANSFORM, dedent_for_statement(transform_block))


def validation_statement(target_schema: dict | None) -> tuple[str, str]:
    """Return the ``(stage, code)`` pair for the validation stage.

    :data:`STAGE_VALIDATION` paired with :func:`build_validation_code`'s output,
    in the ``[(stage_label, code), …]`` shape
    :meth:`GlueSessionTestRunner.run_guarded_session` takes as
    *statements_after_frame*. One place the label and the code it carries are
    joined, so a caller cannot submit the validation code under the wrong stage
    and mis-attribute its failure.
    """
    return (STAGE_VALIDATION, build_validation_code(target_schema))


# ── Result mapping: Glue observations → the Test_Result_Contract ───────
#
# Spec task 3.3, and design §4.5's table verbatim. Every builder below returns
# the same nine keys `tests/fixtures/run_test_result_shapes.py` declares — the
# shape `CodeInterpreterTestRunner.run_test` returns today — so the report, the
# check aggregation and the gate need no new vocabulary for a Spark failure
# (R1.1, R1.2).
#
#   | Glue observation                       | builder                            |
#   |----------------------------------------|------------------------------------|
#   | `Output.Status == "ok"`, markers parsed | `success_results`                  |
#   | `Output.Status == "error"`              | `statement_failure_results`        |
#   | statement `CANCELLED` / session `ERROR` | `statement_failure_results`        |
#   | `CreateSession` fails (not capacity)    | `session_creation_failure_results` |
#   | session never usable / deadline         | `lifecycle_failure_results`        |
#   | `CreateSession` refused for capacity    | `session_capacity_results`          |
#   | no frame could be BUILT (raised)        | `frame_build_failure_results`      |
#
# p44 R2.1/R3.4 add two more, reachable only where the Frame_Source is a
# Sample_Frame — the frame-build statement was reading the pipeline's own object,
# so its failure is a fact about the harness's reach and not about the transform:
#
#   | sample frame build failed               | `sample_read_failure_results`      |
#   | sample frame build hit the deadline     | `sample_read_deadline_results`     |
#
# Without those two, both observations route to a builder that reports a FAILED
# TRANSFORM — `statement_failure_results` for the first and
# `lifecycle_failure_results` (no harness marker, no mention of the read) for the
# second. The routing that sends a sample run to them instead is `run_test`'s,
# and it is spec task 6.1; until it lands these two builders are unreachable from
# production, which is deliberate — the reason vocabulary lands as one sequence
# with its two labels (spec task 4) before anything routes to it.
#
# The four rows carrying `not_verified_by_harness` are the only ones that are NOT
# a verdict about the transform, which is why they are the only ones carrying that
# marker. `create_session_failure_results` owns the choice between the capacity
# row and the plain creation failure; nothing should call either of them directly.
# `results_from_outcomes` owns the choice between the first two rows.
#
# One row of design §4.5's table is NOT built here, deliberately:
#
#   | frame refusal                           | `frame_refusal_results` (task 5.3) |
#
# It lives in the frame-construction section instead, because it is produced by
# the pandas path's own `_frame_truncation_results` rather than assembled from
# `_base_results`, and because its position — before `CreateSession` — is the
# requirement (R7.3) and belongs beside the guard that enforces it.
#
# **`conversion_report` is `None` on every path here, and task 8.1 SETTLED that
# it stays `None`.** All five `_*_results` builders in
# `code_interpreter_runner.py` also return `None`; the pandas `run_test` then
# overwrites it on every path that reached a successful conversion (`:304`,
# `:316`). So the type the CONTRACT declares for that key is set by the CALL
# PATH, not by the builder — read the builders alone and three of the six shapes
# come out wrong. This runner converts nothing, so its `run_test` overwrites
# nothing: `None` is the value the key's own documented meaning ("None when
# conversion failed or never ran") already assigns to a path where no conversion
# ran. The decision, its rejected alternatives and what it costs task 8.2 are
# recorded on `GlueSessionTestRunner.run_test`. Builders keep returning `None`
# because that is now also the final answer, not because a caller will fix it.

#: R1.5. The only builder that can report ``execution_successful`` true is also
#: the only one that pins ``exit_code``, and it pins it to this — which is what
#: makes R5.7 ("no result in which ``execution_successful`` is true and
#: ``exit_code`` is non-zero") hold BY CONSTRUCTION rather than by review.
_SUCCESS_EXIT_CODE = 0

#: R5.2. Glue reports no exit code: a statement has an ``Output.Status``, not a
#: process. A failed statement is therefore GIVEN one, because the contract's
#: ``exit_code`` is an ``int`` on every path where code executed and the report
#: renders it. The value is design §4.5's and means only "non-zero" — nothing
#: downstream keys on which non-zero it is.
_FAILED_EXIT_CODE = 1

#: ``not_verified_reason`` for the THIRD harness reason (R8.1): the account was
#: already at its concurrent-session limit, so no session could be created.
#:
#: A new REASON, not a new field and not a new state. The check this produces is
#: ``not_verified_by_harness`` with ``passed: False`` + ``verified: False``,
#: exactly as the two width reasons are — the argument is written out at
#: ``lambda_function.py:508-527``, which records that ``LGR-b2f79ef-2`` refused a
#: third CHECK FIELD once already. So this constant is the whole of the new
#: vocabulary; ``is_not_verified_check`` (``lambda_function.py:215-223``) and the
#: verified-check partition (``:676``) are untouched by it.
#:
#: Spelled here AND in ``lambda_function.SESSION_CAPACITY_REASON``, deliberately:
#: every test that imports ``lambda_function`` stubs its sibling producer modules
#: with a bare ``MagicMock``, so an imported constant arrives there as an
#: auto-attribute mock and a comparison against it silently never matches. The
#: reasoning is recorded in full at ``lambda_function.py:240-253``. It is a second
#: spelling, so: **change both or neither**, and
#: ``tests/contracts/test_not_verified_reason_labels_cross_language.py`` is what
#: notices if they drift — it also requires a matching entry in the dashboard's
#: ``NOT_VERIFIED_LABELS``.
NOT_VERIFIED_REASON_SESSION_CAPACITY = "session_capacity_exhausted"

#: ``not_verified_reason`` for the FOURTH harness reason (R7.3, spec task 8.1):
#: the frame builder could not produce a frame at all, so no Test_Frame carrying
#: every declared source field exists. Raised as :class:`FrameBuildError`
#: host-side and mapped by :func:`frame_build_failure_results`.
#:
#: **Why it is not one of the two width reasons.** Both of those are rendered by
#: ``aggregate_check_results`` as "the source schema declares N field(s) against a
#: test-frame width ceiling of M" (``lambda_function.py:643-666``), and neither
#: sentence is true here: the schema is present and it is inside the ceiling —
#: what the harness cannot do is *type* one of its fields. Borrowing either
#: reason would put a false claim about the schema on the reviewer's screen while
#: removing a false claim about the transform, which is the objection
#: :func:`session_capacity_results` already records for its own omitted keys.
#:
#: Spelled here AND in ``lambda_function.FRAME_UNBUILDABLE_REASON`` for the reason
#: recorded at :data:`NOT_VERIFIED_REASON_SESSION_CAPACITY` and in full at
#: ``lambda_function.py:240-253``: every test that imports ``lambda_function``
#: stubs its sibling producers with a bare ``MagicMock``, so an imported constant
#: arrives as an auto-attribute mock and a comparison against it silently never
#: matches. **Change both or neither**, and note that
#: ``tests/contracts/test_not_verified_reason_labels_cross_language.py`` requires
#: a matching entry in ``lambda_function.NOT_VERIFIED_REASON_LABELS`` *and* in the
#: dashboard's ``NOT_VERIFIED_LABELS`` — a reason without both renders the bare
#: fallback "not verified" and loses the half of the sentence that says whose
#: fault it was.
NOT_VERIFIED_REASON_FRAME_UNBUILDABLE = "test_frame_unbuildable"

#: ``not_verified_reason`` for the FIFTH harness reason (p44 R2.1, R2.2, R2.3,
#: R3.4; spec task 3.1): the run's own source object could not be read, so the
#: transform was never executed. Reachable only where the Frame_Source is a
#: Sample_Frame — from a frame-build statement that FAILED
#: (:func:`sample_read_failure_results`) or from one that never reached a
#: terminal state inside :data:`_STATEMENT_DEADLINE_SECONDS`
#: (:func:`sample_read_deadline_results`).
#:
#: **Why a fifth reason and not a reuse of
#: :data:`NOT_VERIFIED_REASON_FRAME_UNBUILDABLE`.** They are different facts with
#: different next steps. That one means the harness could not represent a
#: declared type and **nothing was spent** — no session existed. This one means a
#: session was created, billed, and the object could not be opened: the action is
#: to check the object and the session's read access, and
#: ``FRAME_UNBUILDABLE_REASON``'s dashboard label ("could not build a test frame
#: from the declared source types") would be a false claim about the schema.
#: Reusing it would also collapse two rows of
#: ``test_no_proxy_non_verification_prop.HARNESS_KINDS`` into one and make that
#: file's distinct-reason assertion vacuous.
#:
#: **Not the stage label either.** :data:`STAGE_FRAME_BUILD` is
#: ``"test data generation"``, so ``statement_failure_results``' sentence shape
#: would read "test data generation failed" about a run that was reading the
#: pipeline's real object — the misattribution R2.1 exists to remove. Both
#: builders below name the source read and the object instead.
#:
#: A new REASON, not a new field and not a new state, exactly as the two above:
#: ``is_not_verified_check`` (``lambda_function.py:215-223``) and the
#: verified-check partition are untouched by it, because the ``verified: False``
#: discriminator already does that work. ``LGR-b2f79ef-2`` refused a third check
#: FIELD once already.
#:
#: Spelled here AND in ``lambda_function.SOURCE_READ_FAILED_REASON`` for the
#: reason recorded at :data:`NOT_VERIFIED_REASON_SESSION_CAPACITY` and in full at
#: ``lambda_function.py:240-253``: every test that imports ``lambda_function``
#: stubs its sibling producers with a bare ``MagicMock``, so an imported constant
#: arrives as an auto-attribute mock and a comparison against it silently never
#: matches. **Change both or neither**, and note that
#: ``tests/contracts/test_not_verified_reason_labels_cross_language.py`` requires
#: a matching entry in ``lambda_function.NOT_VERIFIED_REASON_LABELS`` *and* in the
#: dashboard's ``NOT_VERIFIED_LABELS`` — a reason without both renders the bare
#: fallback "not verified" and loses the half of the sentence that says whose
#: fault it was. Those three places are spec task 4's, and that contract test is
#: RED between this constant landing and task 4 landing, by construction — which
#: is why the plan orders task 3 before task 4 and treats the four places as one
#: sequence.
NOT_VERIFIED_REASON_SOURCE_READ_FAILED = "source_read_failed"

#: ``error_details`` for a runner whose Glue client could not be constructed.
#:
#: Deliberately NOT named ``*REASON*``: that naming convention is what
#: ``test_not_verified_reason_labels_cross_language.py`` scans this directory for,
#: and this string is an ``error_details`` sentence rather than a
#: ``not_verified_reason`` value. A ``REASON`` in the name would enlist it into
#: the reason vocabulary and demand a dashboard label for something no check ever
#: carries.
_CLIENT_UNAVAILABLE_DETAIL = "glue client not available"

#: The Python traceback header Glue does not send.
#:
#: **READ THIS BEFORE TOUCHING THE CLASSIFICATION BELOW.** Importing
#: ``_TRACEBACK_MARKER`` here is the *opposite* of the trap R5.5 names: the
#: marker is used to BUILD stderr text, never to decide whether a statement
#: failed. ``_has_traceback`` (``code_interpreter_runner.py:464-467``) searches
#: for this literal, and Glue's ``Traceback`` is a list of frame strings with no
#: header line — so a runner that classified by sniffing would find no marker in
#: a real Spark exception and would pass every one of them as a SUCCESS.
#: Classification comes from ``Output.Status`` alone
#: (:attr:`StatementOutcome.failed`); the header is synthesised into ``stderr``
#: afterwards so that a Spark failure and a Code Interpreter failure read
#: identically in a report.
#:
#: Derived from the imported marker rather than re-typed, for the reason
#: ``NOT_VERIFIED_REASON_SCALAR_LITERAL`` gives for aliasing rather than
#: re-spelling (``code_interpreter_runner.py:78-90``): two copies of one literal
#: are two things a rename can leave silently disagreeing.
_SYNTHESISED_TRACEBACK_HEADER = f"{_TRACEBACK_MARKER}):"

#: R5.4, reused VERBATIM by import — the same function object the pandas path
#: calls, bound to a module-level name rather than reimplemented. Head 2000 +
#: tail 1500, chosen there because the value "flows into reports and DynamoDB".
#: A Spark traceback is long and puts the exception at the END, which is exactly
#: the shape that bound was designed for, so bounding retains both the first and
#: the last characters of what Glue reported.
_bound_stderr = CodeInterpreterTestRunner._bound_stderr

#: ``_parse_results`` reused verbatim by import (design §4.1). It is declared as
#: an instance method but reads no attribute of ``self`` — every value it
#: returns is derived from the *output string* — so it is called here as the
#: plain function it is, with ``None`` standing in for the unused receiver. The
#: annotation is what makes that legal to the checker as well as at runtime.
#:
#: Copying its body instead is the thing that must not happen: reusing the one
#: parser is what keeps ``data_quality_results`` from drifting between the two
#: executors, and that non-drift is a *precondition* for shadow mode being able
#: to attribute a difference to the executor rather than to the parser. What
#: produces the markers it reads is task 6.1's; this is only the reader.
_parse_results: Callable[[Any, str], dict] = CodeInterpreterTestRunner._parse_results


def _base_results(**overrides: Any) -> dict:
    """The nine Test_Result_Contract keys, with *overrides* applied.

    One place the key set lives, so no builder below can omit a key a report or
    the gate reads. The defaults are the "nothing ran, nothing proven" values —
    the same ones ``_empty_results`` uses (``code_interpreter_runner.py:774``) —
    so a builder states only what it actually observed.
    """
    results: dict = {
        "execution_successful": False,
        "executed": False,
        "exit_code": None,
        "stderr": "",
        "input_records": 0,
        "output_records": 0,
        "data_quality_results": {},
        "error_details": None,
        "conversion_report": None,
    }
    results.update(overrides)
    return results


#: The longest a quoted literal may be once it has left a Verification_Session
#: (p44 R6.6). A NAMED CONSTANT rather than an inline ``64``, because the number
#: is a data-protection decision with a justification and two rejected
#: alternatives, and an inline literal carries none of them — nor a name a
#: property test can import and assert the bound against.
#:
#: **Why 64.** Every diagnostic shape that actually defeats a cast fits inside it
#: with room over: a thousands separator (``1,234.56``), a timestamp in a numeric
#: column (``2026-08-18T19:45:13Z``, 20 characters), a currency prefix
#: (``$1200``), exponent notation (``1.2e5``), a sentinel (``N/A``), padding
#: (``" 42 "``). The case the bound EXISTS for is the one where "the value" is not
#: a value: a CSV read with the wrong delimiter collapses an entire record into
#: one field, so an untruncated message reproduces a whole customer row into every
#: sink R6.11 enumerates — the report's execution-error block, ``test_report.md``,
#: ``test_results.json`` (which carries the report body twice), the
#: ``test_results`` DynamoDB item, ``checks[].detail``, the ``review_summary``
#: excerpt and this module's own ERROR log. 64 caps that at a recognisable
#: fragment.
#:
#: *Rejected:* redacting the literal entirely. ``For input string: ""`` with the
#: value gone does not tell a reviewer which of the six shapes above it was, and
#: destroying the diagnostic to protect a value that is often ``N/A`` is a bad
#: trade (R6.9). *Rejected:* accepting untruncated text, which is the status quo
#: and is what makes the delimiter-collapse case a full-row copy.
#:
#: **First of its kind here, so read this as a proposal rather than a
#: restatement.** ``decisions(concept="security")`` is empty and
#: ``concept="error-handling"`` holds one unrelated record, so this repository has
#: no ratified decision about data protection for customer rows (requirements
#: C10). p44 R6 is the proposal; this constant is its executable half.
_ERROR_LITERAL_MAX_CHARS = 64

#: What stands in for the removed tail, INSIDE the quotes. One character, so a
#: truncated literal's rendered content is exactly
#: :data:`_ERROR_LITERAL_MAX_CHARS` characters counting it — the bound holds on
#: what a reader sees between the quotes, not merely on the prefix underneath it.
_ERROR_LITERAL_ELISION = "…"


def _closing_quote_index(line: str, start: int) -> int:
    """Index of the ``"`` that closes a literal whose content begins at *start*.

    ``-1`` when the line carries none. A backslash escapes whatever follows it,
    so ``\\"`` does NOT close the literal — reading it as a terminator would
    split one literal into two and bound neither of them, and an escaped quote
    inside a literal is one of the shapes task 8.2's property generates. A
    ``\\\\`` pair escapes itself, after which a following ``"`` does close.
    """
    index = start
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index
        index += 1
    return -1


def _truncate_quoted_literals(text: str) -> str:
    """Bound every double-quoted literal in *text* (p44 R6.6, R6.8, R6.9).

    A Spark cast failure quotes the offending value — ``For input string:
    "1,234.56"`` — so once a run reads a Sample_Frame, that message carries a real
    customer cell out of the Verification_Session and into every sink R6.11
    enumerates. This bounds the quoted part to :data:`_ERROR_LITERAL_MAX_CHARS`
    characters and says how much it dropped::

        For input string: "1,234.56…" (+412 chars elided)

    **What it preserves, byte for byte: everything outside a quoted literal**
    (R6.9, do-NOT 16). ``NumberFormatException`` and any unquoted column
    identifier ARE the diagnostic — a truncation that lost them would defeat its
    own purpose and leave a reviewer with a bounded nothing.

    **What it states: the number of characters removed** (R6.8), following
    :data:`_bound_stderr`'s own elision convention
    (``code_interpreter_runner.py:475``). Without the count, a value exactly at
    the bound and a value four hundred characters past it render identically —
    and a value being enormous is itself the diagnostic, because that is the
    delimiter-collapse case the bound exists for.

    **Idempotent, and it has to be.** A truncated literal's content is exactly at
    the bound, so a second pass leaves it alone, and the ``(+N chars elided)``
    marker sits outside the quotes and contains no quote of its own. This helper
    sits on a path that renders into two sinks, so a non-idempotent truncation
    would double-elide.

    **Two scanning rules that are choices, with their reasons:**

    * *A literal never spans a newline.* Lines are scanned independently, so one
      stray quote in a multi-line Spark message cannot make the whole remainder
      "the literal" and get it cut to 64 characters — which would delete frames
      and exception types R6.9 requires survive.
    * *An unterminated quote is not a quoted literal, and is left alone.* Where
      such a value ends is unknowable, and guessing "to end of line" would delete
      diagnostic text that merely happens to follow a stray quote (do-NOT 16).
      Spark's own message templates pair their quotes and
      :func:`_closing_quote_index` handles escaped ones, so the shape arises
      mainly from text another layer already cut. Stated as a residual: what still
      bounds it is :data:`_bound_stderr`'s 3500 characters.

    Single quotes and backticks are deliberately NOT bounded. Spark quotes
    *identifiers* that way — ```nope``, ``'order_key'`` — and a column name is
    schema rather than data (resolved decision 3), while an apostrophe in ordinary
    prose would make any two of them look like a literal.

    Applied at TWO sites, one per channel, and never per sink (R6.10, do-NOT 15 —
    a sink added later would inherit nothing): :func:`exception_summary` for the
    ``ErrorName`` / ``ErrorValue`` channel, and :func:`synthesised_traceback`'s
    frame rendering for the ``Output.Traceback`` channel. The third channel,
    ``Output.Data.TextPlain`` stdout, is a **stated residual** rather than an
    oversight — the reason is recorded at
    :meth:`GlueSessionTestRunner._observe` (do-NOT 24).
    """
    # Not merely a fast path: it is what makes "returned unchanged when there is
    # no over-long literal" hold for the overwhelming majority of Spark messages
    # without depending on the scanner below being perfectly round-tripping.
    if '"' not in text:
        return text
    return "\n".join(_truncate_line_quoted_literals(line) for line in text.split("\n"))


def _truncate_line_quoted_literals(line: str) -> str:
    """:func:`_truncate_quoted_literals` over one line — see it for the reasons."""
    keep = max(0, _ERROR_LITERAL_MAX_CHARS - len(_ERROR_LITERAL_ELISION))
    parts: list[str] = []
    cursor = 0
    while True:
        opening = line.find('"', cursor)
        if opening < 0:
            parts.append(line[cursor:])
            return "".join(parts)

        closing = _closing_quote_index(line, opening + 1)
        if closing < 0:
            # Unterminated. Preserved whole, and nothing after it on this line can
            # be a literal either, so the rest is preserved with it.
            parts.append(line[cursor:])
            return "".join(parts)

        literal = line[opening + 1 : closing]
        parts.append(line[cursor : opening + 1])
        if len(literal) > _ERROR_LITERAL_MAX_CHARS:
            elided = len(literal) - keep
            parts.append(f'{literal[:keep]}{_ERROR_LITERAL_ELISION}" (+{elided} chars elided)')
        else:
            parts.append(f'{literal}"')
        cursor = closing + 1


def exception_summary(statement: Mapping[str, Any]) -> str:
    """``"{ErrorName}: {ErrorValue}"`` for a failed Glue statement (R5.3).

    Returns whichever of the two Glue reported when it reported only one, and
    ``""`` when it reported neither — a caller that needs a non-empty reason
    names the observed status instead, so nothing renders an empty detail.

    **Quoted literals in the composed text are bounded here** (p44 R6.6, R6.10).
    This is the ONE site for the ``ErrorName`` / ``ErrorValue`` channel, not one
    site per sink (do-NOT 15): this function is the module's only reader of
    ``Output.ErrorValue``, and both text-producing paths pass through it —
    :func:`statement_failure_results` takes the return as ``reason`` (into
    ``error_details`` and the ERROR log) and :func:`synthesised_traceback` appends
    it last, which becomes ``stderr``. So for this channel, here is genuinely
    "once, before every sink". See :func:`_truncate_quoted_literals` for what the
    truncation preserves and what it states.
    """
    output = statement.get("Output") or {}
    name = str(output.get("ErrorName") or "").strip()
    value = str(output.get("ErrorValue") or "").strip()
    summary = f"{name}: {value}" if name and value else name or value
    return _truncate_quoted_literals(summary)


def synthesised_traceback(statement: Mapping[str, Any]) -> str:
    """Render Glue's error fields as a Python traceback, header included.

    Glue's ``Traceback`` is a list of frame strings and carries no
    ``Traceback (most recent call last):`` line; this puts one back, followed by
    the frames and then ``ErrorName: ErrorValue`` — Python's own order, so the
    exception is LAST and survives :func:`_bound_stderr`'s tail (R5.4).

    Returns ``""`` when Glue reported neither frames nor an exception, rather
    than a lone header describing nothing.

    Unbounded on purpose: the bound belongs to the builder that writes the
    result, so the length-crossing property test (task 3.5) can generate against
    this and assert against the bounded value. ``_bound_stderr``'s head-and-tail
    limit and p44's per-literal truncation are different bounds on different
    things and neither replaces the other: one caps the whole message, the other
    caps one customer value inside it.

    **Quoted literals in the FRAMES are bounded here** (p44 R6.6, R6.10). This is
    the one site for the ``Output.Traceback`` channel, which reaches ``stderr``
    only and bypasses :func:`exception_summary` entirely — a Py4J-wrapped Java
    exception commonly embeds its message in frame text. See
    :func:`_truncate_quoted_literals`.
    """
    output = statement.get("Output") or {}
    frames = output.get("Traceback") or []
    # A single string rather than a list is not a shape Glue documents, but it
    # is the one that would otherwise be iterated character by character.
    if isinstance(frames, str):
        frames = [frames]

    # Empty frames are dropped rather than rendered as blank lines, so that a
    # `Traceback` of `[""]` is "Glue reported nothing" — the same as `[]` — and
    # not a header followed by whitespace. The blank test reads the frame BEFORE
    # truncation, which is the same decision either way: bounding a literal never
    # empties a frame, because everything outside the literal survives.
    rendered = [
        _truncate_quoted_literals(stripped)
        for stripped in (str(frame).strip("\n") for frame in frames)
        if stripped.strip()
    ]
    # NOT truncated again: `exception_summary` is the other channel's site and has
    # already bounded its own literals. Doing it twice would be harmless — the
    # helper is idempotent — but it would read as though one site were not enough.
    exception = exception_summary(statement)
    if not rendered and not exception:
        return ""

    lines = [_SYNTHESISED_TRACEBACK_HEADER, *rendered]
    if exception:
        lines.append(exception)
    return "\n".join(lines)


def success_results(outcomes: Sequence[StatementOutcome]) -> dict:
    """Map a run whose every stage reported ``ok`` onto the contract (R1.5).

    ``execution_successful`` true, ``executed`` true, ``exit_code`` zero. The
    record counts and every data-quality value come from
    :data:`_parse_results` reading the ``GENERATED:`` and ``VALIDATION:`` markers
    out of the stages' combined stdout, so they are computed by the same parser
    the pandas path uses.

    **This cannot manufacture a success.** ``_parse_results`` sets
    ``execution_successful`` true only where it found a ``VALIDATION:`` payload
    without an ``error`` key, and a run that failed emits no such payload — so a
    misrouted call returns a result that reports nothing was validated rather
    than one that claims a passing verification.

    ``stderr`` stays ``""``, which is a statement about Glue rather than an
    omission: a Glue statement's ``Output`` carries ``Status``, ``ErrorName``,
    ``ErrorValue``, ``Traceback`` and ``Data.TextPlain`` — there is no separate
    stderr stream, so the "benign stderr" the pandas path joins across its three
    steps has no counterpart here. Inventing text for the field would be worse
    than leaving it empty.
    """
    combined = "\n".join(outcome.stdout for outcome in outcomes if outcome.stdout)
    results = _parse_results(None, combined)
    # R5.7 by construction: the only builder that can report success is also the
    # only one that sets `exit_code`, and it sets it here. No other builder in
    # this module leaves `execution_successful` anything but the base False, so
    # the forbidden combination has nowhere to be produced.
    results["exit_code"] = _SUCCESS_EXIT_CODE
    # `_parse_results` returns eight of the nine contract keys — it predates the
    # conversion report, which `run_test` adds. Set it so every builder in this
    # module returns the same key set (R1.1), and see the section header above
    # for why the value is `None` and whose decision it is.
    results["conversion_report"] = None
    return results


def _failure_reason_and_stderr(outcome: StatementOutcome) -> tuple[str, str]:
    """What a failed statement REPORTED: ``(reason, unbounded stderr)``.

    Extracted from :func:`statement_failure_results` so that R2.3's
    classification rule lives in exactly one place: p44's
    :func:`sample_read_failure_results` needs the same *reason* for the same
    observation and must not re-derive it. Two callers, one branch — a second
    copy of this ``if`` is the second-inference-path defect, and the two copies
    would be free to disagree about what a ``CANCELLED`` statement means.

    **This does not decide that the statement failed** (R5.5, R2.3). The
    classification is :attr:`StatementOutcome.failed`, which reads ``state`` and
    ``Output.Status`` only; this reads the fields of a statement a caller has
    already classified. See :data:`_SYNTHESISED_TRACEBACK_HEADER` for why
    sniffing traceback text instead would pass every Spark exception as a
    success.

    ``stderr`` is returned unbounded, because the bound belongs to the builder
    that writes the result — :func:`synthesised_traceback` says the same about
    itself, and :func:`sample_read_failure_results` discards this half entirely.
    """
    if outcome.state == _STATEMENT_STATE_AVAILABLE:
        # R5.1-R5.3. The fallback names the status Glue reported, so a failure
        # with neither `ErrorName` nor `ErrorValue` still says what was seen.
        reason = exception_summary(outcome.statement) or f"Glue reported output status {outcome.output_status!r}"
        return reason, synthesised_traceback(outcome.statement)

    # R5.6 — a cancelled statement, or one whose session went to ERROR under it.
    # Glue reports no `Output` on either, so the state IS the evidence.
    reason = f"Glue statement reached state {outcome.state}"
    return reason, "\n".join(part for part in (reason, outcome.stdout) if part)


def statement_failure_results(outcome: StatementOutcome) -> dict:
    """Map a statement that RAN and failed (R5.1, R5.2, R5.3, R5.5, R5.6).

    ``executed`` true — the transform reached Spark, so this is an observation
    about the transform rather than about the harness — with a non-zero
    ``exit_code`` and ``execution_successful`` false.

    Two observations land here, and the difference is visible in what they say:

    * ``AVAILABLE`` with ``Output.Status`` other than ``ok``: Spark raised.
      ``error_details`` names the reported exception and its value (R5.3) and
      ``stderr`` carries the synthesised traceback, bounded (R5.4).
    * ``CANCELLED`` or ``ERROR``: the statement never produced an output.
      ``error_details`` and ``stderr`` both NAME THE REACHED STATE (R5.6), and
      ``stderr`` carries whatever the statement had printed before it stopped.

    **Neither branch reads traceback text to decide it failed** (R5.5) — the
    classification is :attr:`StatementOutcome.failed`, which reads ``state`` and
    ``Output.Status`` only. See :data:`_SYNTHESISED_TRACEBACK_HEADER` for the
    full argument and for why reusing ``_has_traceback`` here would pass every
    Spark exception as a success.

    The ``error_details`` layout is ``_failed_execution_results``'
    (``code_interpreter_runner.py:746-771``): ``"{stage} failed: {reason}"`` plus
    the stderr block, so a reviewer reads one sentence shape whichever executor
    ran. That function is deliberately NOT called to produce it: it logs
    "Code Interpreter %s failed", and labelling a Spark failure with the other
    executor's name is exactly the misattribution p32 exists to remove. The
    shape is held to the contract by test instead of by delegation.
    """
    # Both branches live in `_failure_reason_and_stderr`, which p44's
    # `sample_read_failure_results` shares rather than copying — the routing
    # itself is unchanged.
    reason, stderr = _failure_reason_and_stderr(outcome)

    bounded = _bound_stderr(stderr)
    detail = f"{outcome.stage} failed: {reason}"
    if bounded:
        detail += f"\nstderr:\n{bounded}"
    logger.error("Glue %s failed: %s", outcome.stage, reason)

    return _base_results(
        executed=True,
        exit_code=_FAILED_EXIT_CODE,
        stderr=bounded,
        error_details=detail,
    )


def not_executed_results(reason: str) -> dict:
    """The shape for "nothing ran": ``executed`` false, no ``exit_code``.

    A FAILURE, not a non-verification — deliberately, and this is the one place
    the distinction is easy to get wrong. An executor that could not be reached
    proved nothing about the transform *and named no reason a reviewer can act
    on*, which is why the pandas path's equivalent (``_empty_results``,
    ``code_interpreter_runner.py:774``) stays a failed check (p32 3.22). The
    single exception is account-level capacity exhaustion, which says something
    about the account rather than about this run and therefore becomes a harness
    non-verification with a named reason — R8.1, which
    :func:`session_capacity_results` now owns. Do not grow a
    ``not_verified_by_harness`` key here: this builder is the shape three other
    outcomes share, and a marker added at this depth would attach to all of them.
    """
    return _base_results(error_details=reason)


def session_creation_failure_results(exc: Exception) -> dict:
    """``CreateSession`` failed for a reason other than capacity (R8.5).

    ``execution_successful`` false and ``executed`` false, with the reported
    error in ``error_details``. boto3's message already names the error code, so
    ``str(exc)`` is the reported error rather than a paraphrase of it.

    **This builder is the else-branch and it stays the else-branch.**
    :func:`create_session_failure_results` is the only thing that should route a
    ``CreateSession`` failure, and it recognises capacity first (R8.1) — routing
    capacity through here would report an account limit as a failed verification,
    which is the attribution error R8 exists to prevent. Calling this directly
    with an unclassified exception reintroduces exactly that error.
    """
    return not_executed_results(f"Glue session creation failed: {exc}")


def session_capacity_results(exc: Exception) -> dict:
    """``CreateSession`` was refused for capacity: a HARNESS non-verification (R8.1).

    The one ``CreateSession`` failure that is not a failure. An account already
    holding its maximum number of interactive sessions says nothing about the
    transform — the report's job here is "nothing was verified, and it was not
    your code's fault", which is the same claim a frame-width refusal makes and
    is why it reuses that claim's marker.

    **A new reason, not a new field and not a new state.** The shape is
    :data:`~run_test_result_shapes.FRAME_REFUSAL`'s sibling: the nine base
    contract keys plus ``not_verified_by_harness`` and ``not_verified_reason``,
    and nothing else. ``aggregate_check_results`` therefore reports it through its
    existing ``not_verified_by_harness`` arm, with ``passed: False`` +
    ``verified: False`` (R8.2), which keeps it out of the verified-check partition
    (R8.3) with no new logic — the ``verified: False`` discriminator already does
    that. ``lambda_function.py:508-527`` states the rule and records that a third
    check field was refused once (``LGR-b2f79ef-2``).

    **``not_verified_field_count`` / ``not_verified_frame_ceiling`` are absent,
    and that absence is deliberate.** They are the frame refusal's contract with
    ``lambda_function.HARNESS_FIELD_COUNT_KEY`` / ``HARNESS_FRAME_CEILING_KEY``,
    which render them as text. There is no field count and no ceiling in a
    capacity refusal, so carrying them as zeroes would render the sentence "the
    source schema declares 0 field(s) against a test-frame width ceiling of 0" —
    a false claim about the schema, made while removing a false claim about the
    transform. The harness branch names capacity instead.

    **Adds no field a Step Functions Choice state reads (R8.4).** The Choice
    states over this step read ``$.test_code_result.test_result.status``,
    ``.regeneration_needed``, ``.regeneration_attempt``,
    ``.regeneration_max_attempts`` and ``.approval_required``
    (``orchestration-stack.ts:851-923``); both keys added here live on a check
    inside the check list, which no condition addresses. ``LGR-69ca78b-1``
    (**invariant**) is why that matters: the governance Choice states fail closed,
    so a key that no condition reads is inert rather than permissive.
    """
    return _base_results(
        error_details=(
            f"no Glue session could be created: the account is at its concurrent "
            f"interactive-session limit, so the transform was never executed ({exc})"
        ),
        not_verified_by_harness=True,
        not_verified_reason=NOT_VERIFIED_REASON_SESSION_CAPACITY,
    )


def create_session_failure_results(exc: Exception) -> dict:
    """Route a ``CreateSession`` failure: capacity → non-verification, else failure.

    The whole of R8's routing, in one place, so the ordering constraint holds by
    construction rather than by a caller remembering it. Capacity is recognised
    FIRST (R8.1); everything else is a failed verification carrying the reported
    error (R8.5).

    This exists because the two outcomes are one API call apart and mean opposite
    things. ``run_test`` (task 8.1) calls this, not either builder directly — an
    ``except`` clause that reached for :func:`session_creation_failure_results`
    would report an account-level limit as "your transform failed", and would do
    it silently, on the one path nobody can reproduce on demand.
    """
    if _is_capacity_exhausted(exc):
        logger.warning("Glue session creation refused for capacity — nothing was verified: %s", exc)
        return session_capacity_results(exc)
    return session_creation_failure_results(exc)


def lifecycle_failure_results(exc: Exception) -> dict:
    """The session never became usable, or a deadline elapsed (R5.6).

    Covers every :class:`GlueSessionError` the lifecycle raises for its own
    failures: a session that reached a state from which ``READY`` is
    unreachable, a session that never reached ``READY`` inside its deadline, a
    statement that never reached a terminal state inside its deadline, and an
    unavailable client. Each of those messages already **names the reached
    state** where there was one, which is what R5.6 asks of the session branch —
    so the requirement is met by the exception text rather than by re-deriving
    it here.

    This is the Glue analogue of the sixth ``run_test`` outcome, which the
    spec's list of five does not name: ``LIFECYCLE_EXCEPTION`` in
    ``tests/fixtures/run_test_result_shapes.py``, produced when any exception
    out of ``_run_lifecycle`` is caught and turned into ``_empty_results``. The
    Glue runner reaches the same shape by the same route, because task 3.1's
    ``finally`` releases the session and lets the exception surface to a caller
    that maps it — which is :meth:`GlueSessionTestRunner.run_test`.
    """
    return not_executed_results(str(exc))


def frame_build_failure_results(exc: FrameBuildError) -> dict:
    """A frame that could not be built at all: a HARNESS non-verification (R7.3).

    The mapping :class:`FrameBuildError`'s docstring defers to ``run_test``, and
    the destination it names: "a harness non-verification with no session
    created". Raised host-side by :func:`build_frame_code` — from a declared field
    type outside :func:`spark_type_for`'s vocabulary on the synthetic arm, or a
    :class:`FrameSource` with no reader on the sample arm — so nothing was spent
    and nothing about the transform was observed.

    **A non-verification and not a failure, and the direction of the mistake
    matters.** The harness could not represent one of the schema's declared
    types. That is a limitation of the frame builder, exactly as a width refusal
    is a limitation of the frame's size, so mapping it to
    :func:`not_executed_results` would report a harness limitation as a failed
    verification — which ``FrameBuildError``'s docstring warns against by name and
    which is the attribution error p32 exists to remove.

    **Shape: :func:`session_capacity_results`' sibling, not
    ``_frame_truncation_results``'.** The nine base contract keys plus
    ``not_verified_by_harness`` and ``not_verified_reason``, and nothing else.
    ``not_verified_field_count`` / ``not_verified_frame_ceiling`` are absent for
    the same reason capacity omits them: there is no width problem here, so
    carrying a count and a ceiling would render a sentence about the schema's
    width that is not what went wrong. The harness branch in
    ``aggregate_check_results`` names the unbuildable type instead
    (``lambda_function.py``'s :data:`~lambda_function.FRAME_UNBUILDABLE_REASON`
    arm).

    ``verified: False`` + ``passed: False`` follow from the existing
    ``not_verified_by_harness`` arm with no new logic, so
    ``is_not_verified_check`` (``lambda_function.py:215-223``) and the
    verified-check partition (``:676``) are untouched — a fourth REASON, not a
    third state and not a new field. ``LGR-b2f79ef-2`` refused a third check field
    once already.

    ``error_details`` carries the exception's own text, which names both the
    offending column and the type it declared — see :func:`spark_type_for` for why
    that is a refusal rather than a fall-through to ``StringType``.
    """
    logger.error("No test frame could be built — nothing was verified: %s", exc)
    return _base_results(
        error_details=f"test frame could not be built: {exc}",
        not_verified_by_harness=True,
        not_verified_reason=NOT_VERIFIED_REASON_FRAME_UNBUILDABLE,
    )


def sample_read_failure_results(outcome: StatementOutcome, frame_source: FrameSource) -> dict:
    """A Sample_Frame's source read FAILED: a HARNESS non-verification (R2.1, R2.2).

    The frame-build statement ran against the pipeline's own source object and
    Glue reported a failure — an ``AccessDenied`` on the object, a malformed
    file, a reader that could not open it. Nothing about the transform was
    observed, because the transform statement is never submitted after a failed
    stage (:meth:`run_session` stops at the first failure), so ``executed`` is
    **false** and there is no ``exit_code``.

    **The direction of the mistake this exists to prevent.** Route the same
    observation through :func:`statement_failure_results` — which is where
    :func:`results_from_outcomes` sends it today — and the result claims
    ``executed`` true with ``exit_code`` 1 and an ``error_details`` reading
    "test data generation failed: …", because :data:`STAGE_FRAME_BUILD` is
    labelled ``"test data generation"``. That is a reported transform failure for
    a transform that never ran, on a stage that was not generating anything. R2.1
    and R2.2 exist to remove exactly that sentence, so ``error_details`` here
    names **the source read and the object** and no stage label.

    **The reason comes from the shared classifier** (R2.3):
    :func:`_failure_reason_and_stderr` is the same branch
    :func:`statement_failure_results` uses, reading ``state`` and
    ``Output.Status`` through :attr:`StatementOutcome.failed`'s vocabulary and
    :func:`exception_summary`'s fields. Nothing here sniffs traceback text — see
    :data:`_SYNTHESISED_TRACEBACK_HEADER` for why a sniff would pass every Spark
    exception as a success.

    **Shape: :func:`session_capacity_results`' sibling.** The nine base contract
    keys plus ``not_verified_by_harness`` and ``not_verified_reason``, and nothing
    else — so ``aggregate_check_results`` reports it through its existing harness
    arm with ``passed: False`` + ``verified: False`` and no new logic, and
    ``conversion_report`` stays ``None`` as it is on every Glue result
    (``LGR-ad78946-2``, **template / ratified**).

    ``not_verified_field_count`` / ``not_verified_frame_ceiling`` are absent for
    the reason capacity omits them: there is no width problem here, so carrying a
    count and a ceiling would render "the source schema declares 0 field(s)
    against a test-frame width ceiling of 0" — a false claim about the schema,
    made while removing a false one about the transform.

    ``stderr`` is left at the base ``""``, as both harness siblings leave it. The
    synthesised traceback is a rendering of the transform's execution, and this
    run has none to render; the observed reason travels in ``error_details``,
    which is the one sentence a reviewer reads.

    **The session is already released.** :meth:`run_session`'s ``finally`` stops
    and deletes it on every exit path (R2.4), so this builder adds no release
    logic and must not.
    """
    reason, _stderr = _failure_reason_and_stderr(outcome)
    logger.error(
        "Glue could not read the run's source object — nothing was verified: %s (%s)",
        frame_source.source_uri,
        reason,
    )
    return _base_results(
        error_details=(f"the run's source object could not be read: {frame_source.source_uri} — {reason}"),
        not_verified_by_harness=True,
        not_verified_reason=NOT_VERIFIED_REASON_SOURCE_READ_FAILED,
    )


def sample_read_deadline_results(exc: GlueSessionError, frame_source: FrameSource) -> dict:
    """A Sample_Frame's source read never finished: a HARNESS non-verification (R3.4).

    The other door onto :data:`NOT_VERIFIED_REASON_SOURCE_READ_FAILED`. The
    frame-build statement did not reach a terminal state inside
    :data:`_STATEMENT_DEADLINE_SECONDS`, so ``_await_statement`` raised
    :class:`GlueSessionError` and there is no :class:`StatementOutcome` to read —
    which is why this takes the exception and why the frame decision has to be
    reachable from the runner instance on a raise path.

    **What this adds over :func:`lifecycle_failure_results`, which is where the
    same raise lands today.** That builder is :func:`not_executed_results`, so
    ``executed`` is already false — but it carries **no** harness marker and its
    ``error_details`` is the exception's own text, which names the statement
    deadline and not the source read. R3.4 asks for both: a reason naming the
    source read, and the session released. So this is the same shape with the
    marker and the object added, not a different verdict.

    Whether a bounded read of a real object completes inside the deadline is
    **unmeasured** — measuring it creates a billable session — which is why R3.4
    specifies an honest outcome for exceeding it rather than waiting on the
    measurement.

    Shape and omissions are :func:`sample_read_failure_results`', for the same
    reasons; ``conversion_report`` is ``None`` (``LGR-ad78946-2``). The session is
    released by :meth:`run_session`'s ``finally`` before this is ever called
    (R2.4), so nothing here releases anything.
    """
    logger.error(
        "Glue did not finish reading the run's source object within the statement deadline — "
        "nothing was verified: %s (%s)",
        frame_source.source_uri,
        exc,
    )
    return _base_results(
        error_details=(
            f"the run's source object could not be read within the statement deadline: "
            f"{frame_source.source_uri} — {exc}"
        ),
        not_verified_by_harness=True,
        not_verified_reason=NOT_VERIFIED_REASON_SOURCE_READ_FAILED,
    )


def sample_read_failure(outcomes: Sequence[StatementOutcome]) -> StatementOutcome | None:
    """The FRAME-BUILD outcome that failed, or ``None`` — the sample route's gate.

    ``run_test``'s gate for :func:`sample_read_failure_results` (R2.1, R4.1), and
    the reason the route is a *narrow* one rather than "a Sample_Frame run that
    failed". On a Sample_Frame run the frame-build statement is the source read,
    so a failure there is a failed read. A failure at the **transform** or
    **validation** stage of the same run is a genuine transform failure — the read
    succeeded, the transform ran on real rows and Spark raised — and it keeps its
    existing routing through :func:`results_from_outcomes` to
    :func:`statement_failure_results`, ``executed`` true and all. Testing the
    stage is what separates the two; testing only ``failed`` would relabel every
    transform failure on a sample run as a harness abstention, which is the
    opposite of the misattribution R2.1 exists to remove.

    **Only the last outcome is examined, and that is not an approximation.**
    :meth:`GlueSessionTestRunner._run_statements` breaks at the first failure, so
    a list whose last entry passed cannot contain an earlier entry that failed.
    :func:`results_from_outcomes` documents the same rule for the same reason;
    scanning the whole list here would be a second, weaker statement of it, free
    to disagree after an edit.

    The stage is matched by :data:`STAGE_FRAME_BUILD` rather than by position, so
    the gate does not quietly depend on the frame build being submitted first —
    which :meth:`GlueSessionTestRunner.run_guarded_session` guarantees today and
    is not this function's fact to assume.

    Says nothing about whether the frame WAS a sample: that is
    :attr:`FrameSource.is_sample`, which the caller tests, because this function
    is handed outcomes and has no frame decision to read.
    """
    if not outcomes:
        return None
    last = outcomes[-1]
    if last.stage == STAGE_FRAME_BUILD and last.failed:
        return last
    return None


def results_from_outcomes(outcomes: Sequence[StatementOutcome]) -> dict:
    """Map a completed run's stage outcomes onto the contract.

    The other half of ``run_test``'s composition: :meth:`~GlueSessionTestRunner.run_session`
    stops at the first failure, so the **last** outcome is the whole verdict —
    either it failed, and it is the stage that failed, or every stage reported
    ``ok``.

    Reading only the last one is what keeps this consistent with
    ``_run_statements``' own contract rather than re-deriving it: a list whose
    last entry passed cannot contain an earlier entry that failed, because the
    loop breaks. Scanning for *any* failure would be a second, weaker statement
    of the same rule, free to disagree with it after an edit.

    The empty case is unreachable through ``run_test`` —
    :meth:`~GlueSessionTestRunner.run_guarded_session` always submits the frame
    build first — and is mapped rather than asserted because the alternative is an
    ``IndexError`` from a result-mapping function, which is the least
    diagnosable failure available.
    """
    if not outcomes:
        return not_executed_results("no Glue statement was submitted, so nothing was verified")
    last = outcomes[-1]
    if last.failed:
        return statement_failure_results(last)
    return success_results(outcomes)
