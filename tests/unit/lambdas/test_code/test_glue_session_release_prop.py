# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 7 — every created session is released.

Spec `real-spark-verification` task 3.2. **Mocked boto3 only. No real Glue
session is created anywhere in this file and nothing is deployed.**

    *For any* lifecycle step at which a failure is injected, a session that
    reached creation is both stopped and deleted before ``run_session``
    returns.

**Validates: Requirements 7.1, 7.2**

The design states the property over ``run_test``; ``run_test`` is task 8.1 and
does not exist yet, so it is quantified here over :meth:`run_session`, which is
the method that owns everything between ``CreateSession`` and
``DeleteSession``. When 8.1 lands, ``run_test`` calls into this same
``finally``, so the property transfers without restating.

What this generalises
---------------------
``test_glue_session_lifecycle.py`` (task 3.1) pins the release on six named
exit paths: success, a statement exception, a session that never reached READY,
a failing stop, a statement deadline, and a refused creation. Those stay — they
are the worked examples, and a named regression is easier to read than a
counterexample. This file quantifies the same claim over the **whole** cross
product of lifecycle step × failure kind, so a step added later (a fourth
statement stage, a new pre-flight call) is covered by construction rather than
by remembering to add an example.

Stated as a biconditional, not an implication
---------------------------------------------
The property is asserted as **released if and only if created**, because the
one-directional form would assert away the two cases where releasing is the
*wrong* behaviour:

* A **refused** ``CreateSession`` never reached creation. R7.1 and R7.2 are both
  conditioned on "WHEN a session reaches creation", so neither binds, and a
  test that demanded a delete here would demand a ``DeleteSession`` on an id
  that never existed.
* An **externally supplied** ``session_id`` (R7.5) was not created by this
  runner, so it must be neither stopped nor deleted. Releasing it would defeat
  the reuse seam design §4.3 opened it for — the caller owns that lifecycle.

Both fall out of the biconditional rather than needing an exemption, which is
why it is written that way.

What "deleted" is read as
-------------------------
A ``DeleteSession`` call was issued. The runner cannot make AWS honour one, and
it already handles refusal properly: Glue rejects a delete on a still-
``PROVISIONING`` session with ``IllegalSessionStateException``, so
``_release_session`` retries to a bounded deadline and then logs the session as
leaked (``glue_session_runner.py:508-570``, read in full). That path — generated
here as a ``client_error`` with that code, which never clears — issues many
calls and deletes nothing; ``IdleTimeout: 1`` is what bounds its cost, which is
the reason that constant is one minute rather than Glue's 15-minute default.
Asserting "the session no longer exists" would be asserting something about the
mock, not about the runner.

Ledger and structure notes
--------------------------
``why`` on ``glue_session_runner.py`` and on
``test_glue_session_lifecycle.py`` returns **no records**: both files are task
3.1's, untracked, so the ledger has nothing to say about them yet. That is a
gap, not an all-clear. The records that do govern this file are
``LGR-b46a5bb-1`` (standing invariant — the blocking poll loops these
injections drive are legitimate on the container path, reasoning recorded at
``_await_statement``) and ``LGR-3e45e65-1`` (the unit layer pins its region in
``tests/unit/conftest.py``, so no ``os.environ.setdefault`` dance is needed
here). ``LGR-b46a5bb-4`` is the precedent for the shape of this file — a
repository property test with no allowlist, because "an allowlist entry is a
door".

Codegraph, before this file existed: ``run_session`` had 27 callers and
``GlueSessionTestRunner`` 6, every one of them in
``test_glue_session_lifecycle.py`` — no production call site, because executor
selection is task 9.1. So the blast radius of this file is nil in both
directions: it adds coverage and changes no behaviour. ``glue_session_runner``
imports nothing from ``shared/``, so there is no shared-layer blast radius
either.
"""

import os
import sys
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from glue_session_runner import (
    _SESSION_STATES_TERMINAL_BAD,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    STAGE_VALIDATION,
    GlueSessionTestRunner,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

#: The live path's three stages (design §4.1 — three statements so a failure is
#: attributable to a stage). Generated prefixes of this cover the empty case.
STATEMENTS = [
    (STAGE_FRAME_BUILD, "df = spark.createDataFrame(rows, schema)"),
    (STAGE_TRANSFORM, "df_transformed = df"),
    (STAGE_VALIDATION, "print('VALIDATION:{}')"),
]

EXTERNAL_SESSION_ID = "caller-owned-session"

# ── Lifecycle steps a failure can be injected at ──────────────────────
#
# One per AWS call `run_session` makes, plus the no-failure case. Enumerated so
# that the cross product below is checkable against the call sites in
# `glue_session_runner.py:293-343` rather than sampled ad hoc.
_STEP_NONE = "none"
_STEP_CREATE = "create_session"
_STEP_READY_POLL = "get_session"
_STEP_SUBMIT = "run_statement"
_STEP_POLL = "get_statement"
_STEP_STOP = "stop_session"
_STEP_DELETE = "delete_session"

#: Steps inside `_release_session`. A failure here is swallowed by design — the
#: method runs in a `finally` that may be unwinding a real exception, and
#: raising would replace that failure with a teardown failure.
_RELEASE_STEPS = frozenset({_STEP_STOP, _STEP_DELETE})

#: Steps that need a stage index, because they happen once per statement.
_PER_STATEMENT_STEPS = frozenset({_STEP_SUBMIT, _STEP_POLL})

# ── Failure kinds ─────────────────────────────────────────────────────
_KIND_NONE = "none"
#: A modelled boto3 error, carrying one of the codes below.
_KIND_CLIENT_ERROR = "client_error"
#: An arbitrary exception — not a `ClientError`, so `_error_code`'s
#: `getattr(exc, "response", None)` branch returns "" and no special-casing
#: applies.
_KIND_EXCEPTION = "exception"
#: The resource never reaches a terminal state; the step's own deadline is what
#: ends it. Only meaningful where there is a poll loop to stall.
_KIND_STALL = "stall"
#: The API reports, without raising, a state that ends the step.
_KIND_BAD_STATE = "bad_state"

_KINDS_FOR_STEP = {
    _STEP_NONE: (_KIND_NONE,),
    _STEP_CREATE: (_KIND_CLIENT_ERROR, _KIND_EXCEPTION),
    _STEP_READY_POLL: (_KIND_CLIENT_ERROR, _KIND_EXCEPTION, _KIND_STALL, _KIND_BAD_STATE),
    _STEP_SUBMIT: (_KIND_CLIENT_ERROR, _KIND_EXCEPTION),
    _STEP_POLL: (_KIND_CLIENT_ERROR, _KIND_EXCEPTION, _KIND_STALL, _KIND_BAD_STATE),
    _STEP_STOP: (_KIND_CLIENT_ERROR, _KIND_EXCEPTION),
    _STEP_DELETE: (_KIND_CLIENT_ERROR, _KIND_EXCEPTION),
}

#: Includes both codes `_release_session` branches on and several it does not,
#: so the retry loop's three outcomes — retry, stop retrying because the session
#: is gone, stop retrying because the error is unrelated — are all reachable.
#: `IllegalSessionStateException` on the delete step never clears, which is the
#: leaked-session path described in the module docstring.
_ERROR_CODES = (
    "IllegalSessionStateException",
    "EntityNotFoundException",
    "AccessDeniedException",
    "ResourceNumberLimitExceededException",
    "ValidationException",
    "ThrottlingException",
)

#: Statement outcomes that end a stage without raising. The first two carry no
#: `Output` at all (R5.6); the third is a Spark exception reported through
#: `Output.Status`, with a header-free Glue traceback (R5.5's trap).
_STATEMENT_FAILURE_SHAPES = ("CANCELLED", "ERROR", "error_output")

_GLUE_TRACEBACK = [
    '  File "<stdin>", line 1, in <module>\n',
    '  File "/opt/spark/python/pyspark/sql/dataframe.py", line 1, in withColumn\n',
]

#: Shortened for every example so a stalled loop costs ~10 mock calls rather
#: than 180s/5s of simulated polling. The production values are asserted
#: elsewhere; what is under test here is release, not the budgets themselves.
_TEST_DEADLINE = 10


@dataclass(frozen=True)
class Injection:
    """One failure, at one lifecycle step, in one lifecycle."""

    step: str
    kind: str
    #: The error code, bad session state, or statement failure shape the kind
    #: needs. Empty where the kind needs none.
    variant: str
    #: Which statement the injection applies to, for the per-statement steps.
    stage_index: int
    #: Statements handed to `run_session`. A prefix of `STATEMENTS`, so zero is
    #: reachable: a session created for no statements must still be released.
    statement_count: int
    #: Whether a session id is supplied by the caller (R7.5).
    external: bool

    def build_exception(self) -> Exception:
        if self.kind == _KIND_CLIENT_ERROR:
            return ClientError({"Error": {"Code": self.variant, "Message": self.variant}}, self.step)
        return RuntimeError(f"injected at {self.step}")

    def fires_at(self, step: str, stage_index: int = -1) -> bool:
        """Whether this injection applies to *step* on this call."""
        if self.step != step:
            return False
        if step in _PER_STATEMENT_STEPS:
            return self.stage_index == stage_index
        return True


@st.composite
def _injections(draw) -> Injection:
    """A lifecycle step to fail at, a way to fail, and the lifecycle around it.

    Composed rather than filtered: a per-statement step needs at least one
    statement to attach to, and each step admits a different set of failure
    kinds, so an independent draw would discard most of what it generated and
    trip ``filter_too_much``.
    """
    statement_count = draw(st.integers(min_value=0, max_value=len(STATEMENTS)))

    steps = sorted(_KINDS_FOR_STEP)
    if statement_count == 0:
        steps = [s for s in steps if s not in _PER_STATEMENT_STEPS]
    step = draw(st.sampled_from(steps))

    kind = draw(st.sampled_from(_KINDS_FOR_STEP[step]))

    if kind == _KIND_CLIENT_ERROR:
        variant = draw(st.sampled_from(_ERROR_CODES))
    elif kind == _KIND_BAD_STATE and step == _STEP_READY_POLL:
        # Sampled from the module's own frozenset, so a state added to it later
        # is generated here without this file being touched.
        variant = draw(st.sampled_from(sorted(_SESSION_STATES_TERMINAL_BAD)))
    elif kind == _KIND_BAD_STATE and step == _STEP_POLL:
        variant = draw(st.sampled_from(_STATEMENT_FAILURE_SHAPES))
    else:
        variant = ""

    stage_index = draw(st.integers(min_value=0, max_value=statement_count - 1)) if step in _PER_STATEMENT_STEPS else -1

    return Injection(
        step=step,
        kind=kind,
        variant=variant,
        stage_index=stage_index,
        statement_count=statement_count,
        external=draw(st.booleans()),
    )


def _ok_statement() -> dict:
    return {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": ""}}}


def _failing_statement(shape: str) -> dict:
    if shape == "error_output":
        return {
            "State": "AVAILABLE",
            "Output": {
                "Status": "error",
                "ErrorName": "AnalysisException",
                "ErrorValue": "cannot resolve 'nope'",
                "Traceback": _GLUE_TRACEBACK,
                "Data": {"TextPlain": ""},
            },
        }
    # CANCELLED / ERROR carry no Output to read (R5.6).
    return {"State": shape}


def _make_client(inj: Injection) -> tuple[MagicMock, dict]:
    """A mock Glue client that fails exactly where *inj* says.

    Returns the client and a mutable record of what actually happened. The
    record's ``created`` flag is set **after** ``CreateSession`` returns, which
    is the same condition ``run_session``'s ``created_session_id`` is set on —
    that is what makes "created" an observation rather than an inference.
    """
    client = MagicMock()
    state = {"created": False, "statement_index": -1}

    def create_session(**kwargs):
        if inj.fires_at(_STEP_CREATE):
            raise inj.build_exception()
        state["created"] = True
        return {"Session": {"Id": kwargs["Id"]}}

    def get_session(**_kwargs):
        if inj.fires_at(_STEP_READY_POLL):
            if inj.kind == _KIND_STALL:
                # PROVISIONING is the normal pre-READY state, so this stalls the
                # loop to its deadline rather than short-circuiting it.
                return {"Session": {"Status": "PROVISIONING"}}
            if inj.kind == _KIND_BAD_STATE:
                return {"Session": {"Status": inj.variant}}
            raise inj.build_exception()
        return {"Session": {"Status": "READY"}}

    def run_statement(**_kwargs):
        state["statement_index"] += 1
        if inj.fires_at(_STEP_SUBMIT, state["statement_index"]):
            raise inj.build_exception()
        return {"Id": state["statement_index"] + 1}

    def get_statement(**_kwargs):
        if inj.fires_at(_STEP_POLL, state["statement_index"]):
            if inj.kind == _KIND_STALL:
                return {"Statement": {"State": "RUNNING"}}
            if inj.kind == _KIND_BAD_STATE:
                return {"Statement": _failing_statement(inj.variant)}
            raise inj.build_exception()
        return {"Statement": _ok_statement()}

    def stop_session(**_kwargs):
        if inj.fires_at(_STEP_STOP):
            raise inj.build_exception()
        return {}

    def delete_session(**_kwargs):
        if inj.fires_at(_STEP_DELETE):
            raise inj.build_exception()
        return {}

    client.create_session.side_effect = create_session
    client.get_session.side_effect = get_session
    client.run_statement.side_effect = run_statement
    client.get_statement.side_effect = get_statement
    client.stop_session.side_effect = stop_session
    client.delete_session.side_effect = delete_session
    return client, state


def _clock(step: float = 1.0):
    """Patch ``time.monotonic`` with a clock that advances *step* per reading.

    A finite ``side_effect`` list cannot work here, and that is the whole reason
    this helper exists rather than a list: ``_release_session`` reads the clock
    too, and how many times depends on how the run failed — so a list sized for
    one injection starves another. A stepping clock is deterministic and
    unbounded, and gives every loop its own full budget from wherever the
    counter had reached.
    """
    counter = iter(range(0, 10_000_000))
    return patch("glue_session_runner.time.monotonic", side_effect=lambda: next(counter) * step)


def _expects_raise(inj: Injection) -> bool:
    """Whether *inj* makes ``run_session`` raise rather than return.

    Derived rather than observed, so an injection that silently stopped firing
    is caught by this test instead of quietly weakening it.
    """
    if inj.step in _RELEASE_STEPS:
        # Swallowed where it fires, never reached where the caller owns the
        # session. Either way, nothing escapes `_release_session`.
        return False
    if inj.step == _STEP_CREATE:
        # Never called when the caller owns the session, so the injection is
        # inert and the run completes.
        return not inj.external
    if inj.step == _STEP_POLL and inj.kind == _KIND_BAD_STATE:
        # A statement that ran and failed is an outcome, not an exception.
        return False
    return inj.step in {_STEP_READY_POLL, _STEP_SUBMIT, _STEP_POLL}


def _run(inj: Injection) -> tuple[MagicMock, dict, Exception | None, GlueSessionTestRunner]:
    """Drive one lifecycle under *inj*; return what was observed."""
    client, state = _make_client(inj)

    with patch("glue_session_runner.boto3.client", return_value=client):
        runner = GlueSessionTestRunner(
            REGION,
            role_arn=ROLE_ARN,
            session_id=EXTERNAL_SESSION_ID if inj.external else None,
        )
    assert runner.client is client, "guard: a None client refuses before creation and the property would be vacuous"

    raised: Exception | None = None
    with (
        patch("glue_session_runner.time.sleep"),
        _clock(),
        patch("glue_session_runner._SESSION_READY_DEADLINE_SECONDS", _TEST_DEADLINE),
        patch("glue_session_runner._STATEMENT_DEADLINE_SECONDS", _TEST_DEADLINE),
        patch("glue_session_runner._RELEASE_DEADLINE_SECONDS", _TEST_DEADLINE),
    ):
        try:
            runner.run_session(STATEMENTS[: inj.statement_count])
        # Broad on purpose: the injected failure is the point, and which type it
        # surfaces as is `_expects_raise`'s business rather than this line's.
        except Exception as exc:
            raised = exc

    return client, state, raised, runner


# ---------------------------------------------------------------------------
# Property 7 — every created session is released
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(inj=_injections())
def test_a_session_is_released_if_and_only_if_it_was_created(inj: Injection):
    """Property 7: released iff created, whatever failed and wherever.

    The biconditional is the whole statement. Its forward half is R7.1 and
    R7.2 — a created session is stopped and deleted before ``run_session``
    returns, on every exit path including one taken because an exception was
    raised. Its reverse half is what stops the test from demanding a
    ``DeleteSession`` on a session that was refused (R8.1's path) or on one the
    caller owns (R7.5).

    Validates: Requirements 7.1, 7.2
    """
    client, state, raised, runner = _run(inj)

    stopped = client.stop_session.call_count
    deleted = client.delete_session.call_count
    released = stopped > 0 and deleted > 0

    assert released is state["created"], (
        f"released={released} but created={state['created']} for {inj}: "
        f"StopSession × {stopped}, DeleteSession × {deleted}. "
        f"run_session {'raised ' + type(raised).__name__ if raised else 'returned'}"
    )

    if state["created"]:
        # R7.1 and R7.2 are two requirements, not one: the stop and the delete
        # are attempted independently, so neither can be skipped by the other
        # failing. Exactly one stop — `_release_session` does not retry it.
        assert stopped == 1, f"expected one StopSession for {inj}, got {stopped}"
        assert deleted >= 1, f"no DeleteSession issued for {inj} despite a created session"
        assert runner.session_id is None, (
            f"runner still holds session_id={runner.session_id!r} after releasing it ({inj})"
        )
    else:
        assert stopped == 0, f"stopped a session this runner did not create ({inj})"
        assert deleted == 0, f"deleted a session this runner did not create ({inj})"


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(inj=_injections())
def test_the_reuse_seam_is_neither_created_nor_released(inj: Injection):
    """R7.5: an externally supplied session is left entirely alone.

    The seam's point is that the caller owns the lifecycle, so this holds
    however the run failed — a session handed in and then torn down by the
    borrower would be worse than one never borrowed.

    Validates: Requirements 7.1, 7.2
    """
    client, _state, _raised, runner = _run(inj)

    if not inj.external:
        return

    assert runner.owns_session is False
    assert client.create_session.call_count == 0, f"created a session despite being handed one ({inj})"
    assert client.stop_session.call_count == 0, f"stopped the caller's session ({inj})"
    assert client.delete_session.call_count == 0, f"deleted the caller's session ({inj})"
    assert runner.session_id == EXTERNAL_SESSION_ID, (
        f"the caller's session id was clobbered: {runner.session_id!r} ({inj})"
    )


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(inj=_injections())
def test_a_teardown_failure_never_becomes_the_failure(inj: Injection):
    """Release is best-effort and silent; every other step's failure surfaces.

    The mechanism R7.1/R7.2 depend on: ``_release_session`` swallows its own
    errors so that a teardown failure cannot replace the exception being
    unwound, and cannot manufacture one where the run succeeded. Asserted
    alongside the property because it is what makes the ``finally`` safe to put
    an unconditional release in — and because deriving the expectation catches
    an injection that silently stopped firing, which would leave the property
    above passing vacuously.

    Validates: Requirements 7.1, 7.2
    """
    _client, _state, raised, _runner = _run(inj)

    if _expects_raise(inj):
        assert raised is not None, f"expected {inj} to raise, but run_session returned"
    else:
        assert raised is None, f"{inj} should not have raised, but got {type(raised).__name__}: {raised}"
