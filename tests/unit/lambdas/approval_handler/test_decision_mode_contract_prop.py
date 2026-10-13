# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 4 — decision-mode error triage unchanged.

# Feature: p34a-code-approval-stranded, Property 4: Decision-mode error triage unchanged

For any decision-mode event, ``lambda_handler`` returns the same status code and
body as the original: ``409`` for ``ApprovalExpired``, ``410`` for
``TaskTokenNotFound``, ``200`` on success, ``400`` on ``ValueError``, ``500`` on
a transient send failure.

**Validates: Requirements 3.4, 3.5**

WHAT THIS FILE IS
-----------------
A **preservation** file (spec task 4), not an exploration file. Every assertion
below was derived by running the UNFIXED handler and recording what it did — the
observations are quoted verbatim in ``OBSERVED_*`` constants and in each test's
docstring — so the whole file passes today and must still pass at task 6.5.
Nothing here is an assumption about what the handler *ought* to return.

Task 6.2 narrows the blanket ``except Exception`` at ``lambda_function.py:803``
so a create-mode failure raises. That is a change to the *shared* trailing
handler, which is why decision mode needs a fence around it: the decision-mode
caller is the dashboard API and it consumes status codes (P28b R3.3/R3.5,
docstring at ``lambda_function.py:547-550``). Same handler, two callers, two
correct contracts.

WHAT IS NEW HERE, AND WHAT WAS ALREADY PINNED
---------------------------------------------
Three files already stand on this path. This one was scoped against them rather
than written from the task text, because most of the five status codes turned
out to be covered already:

* ``test_approval_send_ordering.py`` (P28b R3.1, read in full) already pins, at
  the ``lambda_handler`` level, **``409``** (``TaskTimedOut`` /
  ``TaskDoesNotExist`` / ``InvalidToken``, on approve and on reject),
  **``410``** (token record absent), **``200``** (approve, reject, and the
  no-token path) and **``500``** (``ThrottlingException`` /
  ``ServiceUnavailable`` out of a real send). Those are not re-pinned here. It
  also pins the Send_Before_Status ordering, which this bugfix must not touch
  (design §2) and which nothing below perturbs — no test in this file asserts
  anything about write order, and the two properties never reach the send at all.
* ``test_create_mode_raises.py`` (task 2) already pins the decision-mode mirror
  for **non-``ValueError``** exceptions out of ``process_approval_decision``:
  a dict with ``statusCode 500`` and body
  ``{"error": "InternalError", "message": str(exc)}``, never a raise. And it
  pins ``ValueError`` → ``400`` for the three places ``lambda_handler`` and
  ``validate_approval_request`` raise it themselves (missing field, invalid
  approval type, the neither-mode ``else``).
* ``tests/integration/approval_flow/test_approval_system.py:345-380`` asserts
  the three ``ValueError``s *raise out of* ``process_approval_decision``
  (``assertRaises``). It does not go through ``lambda_handler``, so it says
  nothing about the status code they become.

So the genuinely new ground, and all this file claims:

1. **The ``400`` arm of the decision-mode contract.** ``process_approval_decision``
   raises ``ValueError`` in three places of its own — approval not found,
   invalid decision value, already-processed — and the mapping of those to
   ``400 ValidationError`` at ``lambda_function.py:799-801`` was pinned nowhere.
   Design §6 names "``400`` on ``ValueError``" as part of Property 4; these are
   the decision-mode instances of it.
2. **Closure of the triage line.** ``lambda_function.py:788`` reads
   ``status_code = 409 if result["error"] == "ApprovalExpired" else 410``. The
   ordering file pins the two error strings production emits today; nobody pins
   what the ``else`` does with a *third* one. Property 4b covers the whole
   result space, so the answer is recorded rather than discovered later: any
   unrecognised error string is reported as ``410``.
3. **The response bodies.** The ordering file asserts status codes plus
   ``body["error"]``/``body["approval_id"]``. The ``200`` body shape
   (``{"message": "Approval <decision> successfully", "result": {...}}``), the
   ``410`` ``detail`` string, and the fact that an error result is echoed as
   ``json.dumps(result)`` *verbatim* were unpinned. Property 4 is about the
   status code **and body**, so they are pinned here.
4. **Totality over exception types.** Task 2 asserts decision mode returns a
   dict for arbitrary non-``ValueError`` exceptions. Property 4a extends the
   domain to include ``ValueError`` and asserts the *classification boundary*:
   ``400`` if and only if ``ValueError``, ``500`` otherwise, never a raise, for
   any exception type at all.

WHY THE CREATE-MODE HALF OF "ONLY CREATE MODE RAISES" IS NOT ASSERTED HERE
-------------------------------------------------------------------------
Task 4's brief phrases the property as "decision mode always returns a dict and
create mode is the only path that raises". Only the first half is version-stable.
The second half is **false on the code as it stands** — that is precisely the
defect Property 3 exists to record — so asserting it here would make this
preservation file fail today, which is the one thing a preservation file must not
do. It is already asserted, correctly, as an exploration test in
``test_create_mode_raises.py``. What is left for this file is the claim that
holds both before and after the fix and that keeps task 6.2 honest:

    **decision mode is never a raising path, whatever create mode does.**

Property 4a asserts exactly that, over every exception type it can be handed.

TWO PATHS DELIBERATELY NOT REACHED THROUGH THEIR REAL MECHANISM
--------------------------------------------------------------
``409`` and the transient ``500`` both require a Step Functions ``send_task_*``
call to fail, and there is no shared fixture for Step Functions — reaching them
for real means installing a client mock. Both are already pinned end-to-end in
``test_approval_send_ordering.py`` (``TestConsumedToken``,
``TestTransientSendFailure``), so this file reaches the ``lambda_handler`` half
of that contract — the result-dict → status-code mapping, which is the only part
task 6.2 could break — by patching ``process_approval_decision``'s *result*
instead. Both were still observed directly on unfixed code while scoping this
file, and both matched the ordering file's pins; the observations are recorded in
``OBSERVED_EXPIRED_BODY`` and ``OBSERVED_TRANSIENT_500_BODY`` below.

FIXTURES, REGION, MOCKING
-------------------------
Shared fixtures only — ``mock_dynamodb_tables`` and ``mock_lambda_env`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` call, no boto3 client or
resource mock (``tests/unit/test_no_duplicate_mocking_prop.py``), and no
Step Functions client mock anywhere in this file. ``AWS_REGION`` is **not** set
here — the unit layer pins it in ``tests/unit/conftest.py`` (``LGR-3e45e65-1``,
template / ratified); it is only *read*, through the same
``os.environ.get`` expression the handler's own resource construction uses.

ON THE MODULE-LOADING PREAMBLE — DUPLICATION, DECLARED
------------------------------------------------------
``_load_handler`` / ``_metrics_stub`` / ``approval_env`` below are the **fifth**
copy of this preamble in this directory (``test_state_row_echo_canonical.py``
established it; ``test_decimal_serialization_prop.py``,
``test_create_mode_raises.py`` and ``test_serialization_preservation_prop.py``
carried it). Copied deliberately, following the precedent tasks 2 and 3 both set
and stated:

* it **should** live in ``tests/unit/lambdas/approval_handler/conftest.py`` —
  that filename is one of the four ``test_naming_convention_prop.py`` allows
  under ``tests/unit/``, so there is somewhere for it to go;
* it is not extracted now because tasks 1, 2 and 3's files are this spec's
  evidence, and editing them mid-fix puts that evidence in doubt. A conftest in
  this directory would also execute for every file in it, including
  ``test_state_row_echo_canonical.py``, which documents at its own stub block
  that it is deliberately immune to stub pollution from siblings. Extraction is
  a job for after task 6.5, when nothing is mid-flight;
* one part of the copy is load-bearing rather than incidental: this file
  monkeypatches a module global (``process_approval_decision``), so it needs its
  **own** module instance. The distinct module name below is what keeps a patch
  here from leaking into a sibling's handler.

The ``metrics_facade`` stub keeps ``time_function`` a real pass-through for the
reason all four earlier files record: a bare ``MagicMock`` turns the
``@time_function`` decorator into a call-swallowing mock, and every assertion
about what the decorated function returned would hold vacuously. The guard at
the end of ``_load_handler`` asserts that did not happen.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from hypothesis import HealthCheck, Phase, example, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Paths — the handler and the shared layer
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
for _path in (LAMBDA_DIR, SHARED_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

APPROVALS_TABLE = "agentic_data_ingestion_approvals"
STATE_TABLE = "agentic_data_ingestion_state"
TASK_TOKENS_TABLE = "agentic_data_ingestion_task_tokens"
EXECUTIONS_TABLE = "agentic_data_ingestion_executions"

APPROVAL_ID = "hellowrols-aabed79f_code_generation_1754671698"
RUN_ID = "run-2026-08-08-14-47-44-db94df01"
PIPELINE_ID = "hellowrols-aabed79f"

# ---------------------------------------------------------------------------
# What the UNFIXED handler was observed to return — the preservation baseline
#
# Recorded by invoking `lambda_handler` on the unfixed module for each of the
# five paths Property 4 names. Everything below is a transcript, not a
# specification: `str.format` placeholders stand in only for the approval id and
# the decision, which vary with the input.
# ---------------------------------------------------------------------------

#: 200, `has_task_token` absent — the send block is skipped entirely, so this
#: path needs no Step Functions client. `{decision}` is "approved" or "rejected".
OBSERVED_200_MESSAGE = "Approval {decision} successfully"

#: The four keys `process_approval_decision` returns on success, echoed under
#: `result`. `processed_at` is a timestamp, so it is checked for presence only.
OBSERVED_200_RESULT_KEYS = frozenset({"approval_id", "decision", "processed_at", "processed_by"})

#: 400 — the three ValueErrors `process_approval_decision` raises itself, mapped
#: at `lambda_function.py:799-801`. Messages verbatim from the observation run.
OBSERVED_400_MESSAGES = {
    "not-found": f"Approval not found: {APPROVAL_ID}",
    "invalid-decision": "Invalid decision: maybe. Must be 'approved' or 'rejected'",
    "already-processed": "Approval already processed with status: approved",
}

#: 409 — observed while scoping this file, with a `TaskTimedOut` ClientError out
#: of `send_task_success`. Identical to what `test_approval_send_ordering.py`
#: `TestConsumedToken` already pins, which is why it is not re-pinned here.
OBSERVED_EXPIRED_BODY = {"error": "ApprovalExpired", "approval_id": APPROVAL_ID, "status": "expired"}

#: 410 — `has_task_token` set, token record absent (TTL-reaped). Reached for real
#: below: it returns before any send, so no Step Functions client is involved.
OBSERVED_TOKEN_NOT_FOUND_DETAIL = f"Task token not found or expired for approval {APPROVAL_ID}"

#: 500 — observed while scoping this file, with a `ThrottlingException` out of
#: `send_task_success`. The approval stays "pending" and the token record is
#: retained so the dashboard can retry (P28b R3.3). Already pinned in
#: `test_approval_send_ordering.py` `TestTransientSendFailure`.
OBSERVED_TRANSIENT_500_BODY = {
    "error": "InternalError",
    "message": "An error occurred (ThrottlingException) when calling the SendTaskSuccess operation: slow down",
}

#: Stand-in for the Lambda context object; `lambda_handler` never reads it.
LAMBDA_CONTEXT = SimpleNamespace(
    function_name="agentic_data_ingestion_approval_handler",
    aws_request_id="40ebe58e-624c-47ae-8fc2-c0dfee9f96e4",
    memory_limit_in_mb=512,
)


# ---------------------------------------------------------------------------
# Loading the handler
# ---------------------------------------------------------------------------
_HANDLER = None

_LAYER_STUBS = {
    "eventbridge_utils": MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
    "notification_service": MagicMock(
        send_approval_notification=MagicMock(return_value={"success": True}),
        send_approval_timeout_alert=MagicMock(return_value={"success": True}),
    ),
}


def _metrics_stub() -> MagicMock:
    """A ``metrics_facade`` whose ``time_function`` is a real pass-through.

    The handler decorates ``create_approval_request`` and
    ``process_approval_decision`` with ``@time_function(...)``. A bare
    ``MagicMock`` would replace both with mocks that swallow the call and return
    a mock, so no status code this file asserts would come from the real
    handler.
    """
    stub = MagicMock()
    stub.time_function = lambda *_a, **_k: lambda f: f
    stub.record_error = MagicMock()
    stub.ApprovalMetrics = MagicMock()
    stub.PipelineMetrics = None
    return stub


def _load_handler():
    """Exec the handler module once, under a name unique to this file.

    Deferred rather than done at import time because the module builds a boto3
    resource and three Table handles at module scope, and a resource constructed
    before ``mock_aws()`` starts is not intercepted by it. The fixture rebinds
    all four afterwards regardless, so the load context only has to be safe, not
    authoritative.

    The module name is distinct from every sibling's so the ``patch.object``
    calls below cannot reach another file's handler instance.
    """
    global _HANDLER
    if _HANDLER is not None:
        return _HANDLER

    stubs = {**_LAYER_STUBS, "metrics_facade": _metrics_stub()}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "approval_handler_decision_mode_contract_lf",
            os.path.join(LAMBDA_DIR, "lambda_function.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    for name in ("create_approval_request", "process_approval_decision", "lambda_handler"):
        assert inspect.isfunction(getattr(module, name)), (
            f"{name} is not a plain function — the @time_function decorator was replaced by a "
            f"mock, which would make every assertion in this file vacuous"
        )
    _HANDLER = module
    return _HANDLER


@pytest.fixture
def approval_env(mock_lambda_env, mock_dynamodb_tables):
    """The handler bound to the shared fixture's moto tables.

    ``mock_lambda_env`` is resolved first (declaration order), so the table-name
    environment variables the handler reads at call time — ``STATE_TABLE`` for
    the status echo, ``EXECUTIONS_TABLE`` for the denormalization — already name
    the fixture's tables by the time any of this runs.

    ``handler.stepfunctions`` is left exactly as the module built it. Every test
    in this file either returns before the send block or has
    ``process_approval_decision`` patched out, so the real client is never
    called — which is the point: no Step Functions mock is needed to pin
    ``lambda_handler``'s half of the contract.
    """
    handler = _load_handler()

    region = os.environ.get("AWS_REGION", "us-west-2")
    handler.dynamodb = boto3.resource("dynamodb", region_name=region)
    handler.approvals_table = mock_dynamodb_tables[APPROVALS_TABLE]
    handler.state_table = mock_dynamodb_tables[STATE_TABLE]
    handler.task_tokens_table = mock_dynamodb_tables[TASK_TOKENS_TABLE]

    return SimpleNamespace(
        handler=handler,
        approvals=mock_dynamodb_tables[APPROVALS_TABLE],
        state=mock_dynamodb_tables[STATE_TABLE],
        tokens=mock_dynamodb_tables[TASK_TOKENS_TABLE],
        executions=mock_dynamodb_tables[EXECUTIONS_TABLE],
    )


# ---------------------------------------------------------------------------
# Events and seeding
# ---------------------------------------------------------------------------
def _decision_mode_event(**overrides: Any) -> dict[str, Any]:
    """A well-formed decision-mode event — ``approval_id`` + ``decision``.

    Deliberately carries neither ``approval_type`` nor ``pipeline_id``, so the
    create-mode test at ``lambda_function.py:752`` cannot claim it.
    """
    event = {
        "approval_id": APPROVAL_ID,
        "decision": "approved",
        "comment": "lgtm",
        "user": "an-approver",
    }
    event.update(overrides)
    return event


def _seed_pending_approval(env, **overrides: Any) -> dict[str, Any]:
    """Write a pending approval record in the shape ``create_approval_request`` writes.

    Note ``has_task_token`` is absent by default: tokens live only in the
    dedicated task-tokens table and the approval record carries just the flag
    (P28b R3.1). With the flag absent the send block at
    ``lambda_function.py:576`` is skipped entirely, which is what lets the
    ``200`` and ``400`` cases below run without a Step Functions client.
    """
    item = {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "approval_type": "code_generation",
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "created_at": "2026-08-08T14:47:44+00:00",
        "approval_data": json.dumps({"code_version_id": "cv-20260808144744"}),
    }
    item.update(overrides)
    env.approvals.put_item(Item=item)
    return item


def _invoke(handler, event: dict[str, Any]) -> tuple[Exception | None, Any]:
    """Call ``lambda_handler`` and report which of the two outcomes happened.

    Returns ``(raised, returned)`` with exactly one of them set. Broad on
    purpose — the job is to OBSERVE, and "decision mode never raises" cannot be
    checked by a test that lets the raise escape.
    """
    try:
        return None, handler.lambda_handler(event, LAMBDA_CONTEXT)
    except Exception as exc:
        return exc, None


def _returned(raised: Exception | None, returned: Any) -> dict[str, Any]:
    """Assert decision mode returned a dict, and hand it back parsed-ready."""
    assert raised is None, (
        f"requirement 3.4: decision mode must never raise — its caller is the dashboard API, "
        f"which consumes status codes (P28b R3.3/R3.5, docstring at lambda_function.py:547-550). "
        f"Got {type(raised).__name__}: {raised}"
    )
    assert isinstance(returned, dict), f"expected a dict return, got {type(returned).__name__}: {returned!r}"
    return returned


# ---------------------------------------------------------------------------
# The 200 arm — success, with the body shape pinned
#
# `test_approval_send_ordering.py` pins `statusCode == 200` for approve, reject
# and the no-token path, plus the write ordering. What it does not pin is the
# BODY, and Property 4 is about the status code and the body. These two go
# through the real `process_approval_decision` against real moto tables.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_a_successful_decision_returns_200_with_the_observed_body(approval_env, decision):
    """``200`` and the body ``{"message": ..., "result": {...}}``, as observed.

    Observed on unfixed code, verbatim for ``approved``::

        {"message": "Approval approved successfully",
         "result": {"approval_id": "...", "decision": "approved",
                    "processed_at": "2026-08-08T19:10:53.145228+00:00",
                    "processed_by": "an-approver"}}

    The message interpolates ``result["decision"]``, not the event's — pinned
    with both decisions because those two are the same string today and a
    refactor could make them differ.

    The stored row is checked too, because a ``200`` body is only honest if the
    write it claims happened: this is the same "returned success must be genuine
    success" shape task 1 and task 2 use.

    **Validates: Requirements 3.4**
    """
    env = approval_env
    _seed_pending_approval(env)

    returned = _returned(*_invoke(env.handler, _decision_mode_event(decision=decision)))

    assert returned["statusCode"] == 200, f"expected 200 on a successful {decision} decision, got {returned!r}"
    body = json.loads(returned["body"])
    assert body["message"] == OBSERVED_200_MESSAGE.format(decision=decision), (
        f"the 200 message changed — observed {OBSERVED_200_MESSAGE.format(decision=decision)!r}, "
        f"got {body['message']!r}"
    )
    assert set(body["result"]) == OBSERVED_200_RESULT_KEYS, (
        f"the 200 result keys changed — observed {sorted(OBSERVED_200_RESULT_KEYS)}, got {sorted(body['result'])}"
    )
    assert body["result"]["approval_id"] == APPROVAL_ID
    assert body["result"]["decision"] == decision
    assert body["result"]["processed_by"] == "an-approver"

    row = env.approvals.get_item(Key={"approval_id": APPROVAL_ID})["Item"]
    assert row["status"] == decision, (
        f"lambda_handler returned 200 but the stored approval says {row['status']!r} — a returned "
        f"success must be a genuine success"
    )


# ---------------------------------------------------------------------------
# The 400 arm — ValueError raised INSIDE process_approval_decision
#
# The genuinely unpinned half of requirement 3.5. Task 2's file covers the three
# ValueErrors raised by `lambda_handler`/`validate_approval_request` themselves;
# `test_approval_system.py:345-380` covers these three as raises out of
# `process_approval_decision`, without going through `lambda_handler`. Nobody
# covered the mapping to 400.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "case_id, seed, event_overrides",
    [
        pytest.param("not-found", None, {}, id="approval-row-missing"),
        pytest.param("invalid-decision", {}, {"decision": "maybe"}, id="invalid-decision-value"),
        pytest.param("already-processed", {"status": "approved"}, {}, id="already-processed"),
    ],
)
def test_a_decision_mode_value_error_returns_400(approval_env, case_id, seed, event_overrides):
    """``ValueError`` from inside ``process_approval_decision`` → ``400``.

    The three cases are the three ``raise ValueError`` sites in
    ``process_approval_decision``: approval not found
    (``lambda_function.py:557``), a decision value outside
    ``{approved, rejected}`` (``:563``), and a record whose status is not
    ``pending`` (``:567``). All three reach ``lambda_handler``'s
    ``except ValueError`` at ``:799-801`` and come back as
    ``{"error": "ValidationError", "message": str(e)}`` with the message passed
    through unchanged — observed, and quoted in ``OBSERVED_400_MESSAGES``.

    Why it matters for task 6.2: these are *decision-mode* ``ValueError``s, and
    the clause that catches them sits directly above the blanket
    ``except Exception`` the fix narrows. A fix that reshapes the trailing
    handlers by mode has to leave this arm alone, and this is the only test that
    says so.

    The stored row is asserted untouched as well — a request turned away with
    ``400`` must not half-apply. For ``already-processed`` that means the
    pre-existing ``approved`` status survives rather than being rewritten.

    **Validates: Requirements 3.4, 3.5**
    """
    env = approval_env
    if seed is not None:
        _seed_pending_approval(env, **seed)

    returned = _returned(*_invoke(env.handler, _decision_mode_event(**event_overrides)))

    assert returned["statusCode"] == 400, (
        f"requirement 3.5: a decision-mode ValueError is reported as 400 — it is non-retryable and "
        f"is not part of the stranding mechanism. Got {returned!r}"
    )
    body = json.loads(returned["body"])
    assert body["error"] == "ValidationError"
    assert body["message"] == OBSERVED_400_MESSAGES[case_id], (
        f"the 400 message changed — observed {OBSERVED_400_MESSAGES[case_id]!r}, got {body['message']!r}"
    )

    row = env.approvals.get_item(Key={"approval_id": APPROVAL_ID}).get("Item")
    if seed is None:
        assert row is None, f"a 400 for a missing approval must not create one — found {row!r}"
    else:
        assert row["status"] == seed.get("status", "pending"), (
            f"a 400 must leave the approval record untouched — status is now {row['status']!r}"
        )
        assert "processed_at" not in row, f"a 400 must not record a decision — found processed_at in {row!r}"


# ---------------------------------------------------------------------------
# The 410 arm — reached for real, and checked at the level of stored state
#
# `test_approval_send_ordering.py::TestTokenRecordAbsent` pins the 410 and two
# body keys with MagicMock tables, asserting `update_item.assert_not_called()`.
# This case reaches the same path by genuinely not writing the token row, and
# asserts the *stored record* is untouched rather than that a mock went uncalled
# — which is the claim requirement 3.4 actually makes. It also pins the `detail`
# string, which nothing pinned before.
# ---------------------------------------------------------------------------
def test_a_missing_token_record_returns_410_and_leaves_the_approval_pending(approval_env):
    """``410 TaskTokenNotFound``, with the approval record genuinely untouched.

    ``has_task_token`` is set but no row exists in the task-tokens table — the
    TTL-reaped case. ``process_approval_decision`` returns the error dict at
    ``lambda_function.py:583-587`` before any send, so this path needs no
    Step Functions client at all, and ``lambda_handler``'s triage at ``:788``
    maps it to ``410`` (the ``else`` arm).

    Observed body, verbatim::

        {"error": "TaskTokenNotFound", "approval_id": "...",
         "detail": "Task token not found or expired for approval ..."}

    Note ``detail``, not ``message`` — an error-shaped result is echoed through
    ``json.dumps(result)`` exactly as ``process_approval_decision`` built it, so
    the key set differs from the ``409`` body's. Property 4b generalises that
    pass-through; this case pins the concrete string.

    **Validates: Requirements 3.4**
    """
    env = approval_env
    _seed_pending_approval(env, has_task_token=True)

    returned = _returned(*_invoke(env.handler, _decision_mode_event()))

    assert returned["statusCode"] == 410, f"expected 410 when the token record is absent, got {returned!r}"
    body = json.loads(returned["body"])
    assert body == {
        "error": "TaskTokenNotFound",
        "approval_id": APPROVAL_ID,
        "detail": OBSERVED_TOKEN_NOT_FOUND_DETAIL,
    }, f"the 410 body changed — got {body!r}"

    row = env.approvals.get_item(Key={"approval_id": APPROVAL_ID})["Item"]
    assert row["status"] == "pending", (
        f"a 410 leaves the approval untouched — the run may still be waiting, so nothing may claim "
        f"a decision. Status is now {row['status']!r}"
    )
    assert "expired_at" not in row and "processed_at" not in row, (
        f"a 410 must write nothing to the approval record — got {sorted(row)}"
    )


# ---------------------------------------------------------------------------
# Property 4a — decision mode is total over exception types
# ---------------------------------------------------------------------------
class ApprovalStoreUnavailable(Exception):
    """A caller-defined exception, standing for anything the ledger has not met."""


class RetryBudgetExhausted(RuntimeError):
    """A second custom class, one level down from a builtin."""


class PickyValueError(ValueError):
    """A ``ValueError`` subclass — the classification is by ``isinstance``, not by type.

    Included because ``lambda_handler``'s ``except ValueError`` catches
    subclasses, so a subclass must land on ``400`` and not ``500``. Nothing
    pinned that before: task 2's generator excludes ``ValueError`` entirely.
    """


#: Exception instances spanning what ``process_approval_decision`` can
#: realistically raise, plus three caller-defined classes.
#:
#: Unlike task 2's generator this one **includes** ``ValueError`` and a
#: ``ValueError`` subclass, because Property 4a is about the classification
#: boundary rather than about one side of it. Still excluded: non-``Exception``
#: ``BaseException``s (``KeyboardInterrupt``, ``SystemExit``) — the blanket
#: clause catches ``Exception``, so those propagate by design and are not part
#: of the contract the dashboard consumes.
_EXCEPTIONS = st.one_of(
    st.builds(RuntimeError, st.text(max_size=24)),
    st.builds(KeyError, st.text(max_size=24)),
    st.builds(TypeError, st.just("Object of type Decimal is not JSON serializable")),
    st.builds(AttributeError, st.text(max_size=24)),
    st.builds(ApprovalStoreUnavailable, st.text(max_size=24)),
    st.builds(RetryBudgetExhausted, st.text(max_size=24)),
    st.builds(BotoCoreError),
    st.builds(ValueError, st.text(max_size=24)),
    st.builds(PickyValueError, st.text(max_size=24)),
    st.builds(
        lambda code, op: ClientError({"Error": {"Code": code, "Message": f"{code} from {op}"}}, op),
        st.sampled_from(
            [
                "ProvisionedThroughputExceededException",
                "ThrottlingException",
                "TaskTimedOut",
                "InternalServerError",
                "ResourceNotFoundException",
            ]
        ),
        st.sampled_from(["SendTaskSuccess", "SendTaskFailure", "UpdateItem"]),
    ),
)

_PROPERTY_SETTINGS = settings(
    max_examples=100,
    # Each example patches `process_approval_decision` out, so no table is
    # touched and Hypothesis's 200 ms default deadline is measuring the wrong
    # thing: the property is over exception types, not latency.
    deadline=None,
    # One moto fixture serves every example by design. Neither property below
    # writes to a table — `process_approval_decision` never runs — so there is
    # no cross-example state to leak.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` excluded: it re-runs hundreds of further examples to
    # annotate a failure whose cause the assertion message already states.
    # Shrinking is kept — the minimal counterexample is the deliverable.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)


@example(exc=ValueError("Approval already processed with status: approved"))
@example(exc=PickyValueError("a ValueError subclass still means 400"))
@example(exc=TypeError("Object of type Decimal is not JSON serializable"))
@example(exc=ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "SendTaskSuccess"))
@given(exc=_EXCEPTIONS)
@_PROPERTY_SETTINGS
def test_decision_mode_classifies_every_exception_without_raising(exc, approval_env):
    """Property 4a: decision mode is a total function into ``400``/``500`` dicts.

    For **any** exception type out of ``process_approval_decision``,
    ``lambda_handler`` returns a dict and never raises, and the classification is
    exactly ``isinstance(exc, ValueError)``:

    * ``ValueError`` (and subclasses) → ``400``
      ``{"error": "ValidationError", "message": str(exc)}``
    * anything else → ``500`` ``{"error": "InternalError", "message": str(exc)}``

    This is the fence around task 6.2. The fix narrows the blanket
    ``except Exception`` at ``lambda_function.py:803`` so create mode raises;
    decision mode must come out the other side unchanged, because its caller is
    the dashboard API and a raise where it expects a status code is a broken
    caller, not a louder failure.

    Relation to the create-mode half: this is the version-stable direction of
    "create mode is the only path that raises". The create-mode half is false
    today by construction — it is the defect — and lives in
    ``test_create_mode_raises.py`` as an exploration test. See the module
    docstring.

    New relative to task 2: the ``ValueError`` arm and the boundary itself. Task
    2's mirror asserts the ``500`` half for non-``ValueError`` exceptions only.

    **Validates: Requirements 3.4, 3.5**
    """
    env = approval_env

    with patch.object(env.handler, "process_approval_decision", side_effect=exc):
        returned = _returned(*_invoke(env.handler, _decision_mode_event()))

    expected_status = 400 if isinstance(exc, ValueError) else 500
    expected_error = "ValidationError" if isinstance(exc, ValueError) else "InternalError"

    assert returned["statusCode"] == expected_status, (
        f"{type(exc).__name__} classified as {returned['statusCode']}, expected {expected_status} — "
        f"the boundary is isinstance(exc, ValueError), nothing else. Got {returned!r}"
    )
    body = json.loads(returned["body"])
    assert body == {"error": expected_error, "message": str(exc)}, (
        f"the {expected_status} body changed for {type(exc).__name__} — got {body!r}"
    )


# ---------------------------------------------------------------------------
# Property 4b — the result-dict → status-code mapping, over the whole space
# ---------------------------------------------------------------------------
#: Error strings `process_approval_decision` emits today, plus arbitrary others.
#:
#: The arbitrary ones are the point. `lambda_function.py:788` is
#: `status_code = 409 if result["error"] == "ApprovalExpired" else 410`, so every
#: error string that is not exactly "ApprovalExpired" is reported as 410 —
#: including one added later by someone who has not read this line. That is the
#: current behaviour, so this file records it rather than asserting a preference.
_ERROR_STRINGS = st.one_of(
    st.just("ApprovalExpired"),
    st.just("TaskTokenNotFound"),
    st.sampled_from(["ApprovalExpiredd", "approvalexpired", "APPROVALEXPIRED", "SomethingElse", ""]),
    st.text(max_size=24),
)

#: JSON-safe filler, so `json.dumps(result)` at `:789` cannot fail for a reason
#: unrelated to the property.
_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=24),
)


@example(result={"error": "ApprovalExpired", "approval_id": APPROVAL_ID, "status": "expired"})
@example(
    result={
        "error": "TaskTokenNotFound",
        "approval_id": APPROVAL_ID,
        "detail": OBSERVED_TOKEN_NOT_FOUND_DETAIL,
    }
)
@example(result={"error": "ApprovalExpired"})
@example(result={"error": ""})
@given(
    result=st.fixed_dictionaries({"error": _ERROR_STRINGS}, optional={"approval_id": st.text(max_size=24)}).flatmap(
        lambda base: st.dictionaries(
            st.sampled_from(["status", "detail", "note", "retry_after"]), _JSON_SCALARS, max_size=3
        ).map(lambda extra: {**extra, **base})
    )
)
@_PROPERTY_SETTINGS
def test_an_error_shaped_result_is_409_only_for_approval_expired_and_410_otherwise(result, approval_env):
    """Property 4b: the triage at ``lambda_function.py:786-789``, over all of it.

    For any error-shaped result from ``process_approval_decision``,
    ``lambda_handler`` returns ``409`` if and only if
    ``result["error"] == "ApprovalExpired"``, ``410`` in every other case, and
    the body is ``json.dumps(result)`` — the result dict echoed through
    **verbatim**, keys and values unchanged.

    Two things this pins that nothing else does:

    * **Closure.** ``test_approval_send_ordering.py`` pins the two error strings
      production emits today (``TestConsumedToken`` → ``409``,
      ``TestTokenRecordAbsent`` → ``410``). The ``else`` arm is what a *third*
      error string would hit, and it is ``410`` — "Gone", for something that may
      not be gone at all. Recorded as current behaviour, not endorsed; if a
      third outcome is ever added, this test is where the cost shows up.
    * **Verbatim pass-through.** The body is not reshaped, so the key set varies
      with the outcome (``409`` carries ``status``, ``410`` carries ``detail``).
      A future "normalise the error body" change has to come through here.

    Reached by patching ``process_approval_decision``'s result rather than by
    failing a real ``send_task_*``, because there is no shared fixture for
    Step Functions and the real triage is already pinned end-to-end in the
    ordering file. What is under test here is ``lambda_handler``'s mapping — the
    only part of the ``409``/``410`` contract task 6.2 could break.

    **Validates: Requirements 3.4**
    """
    env = approval_env

    with patch.object(env.handler, "process_approval_decision", return_value=result):
        returned = _returned(*_invoke(env.handler, _decision_mode_event()))

    expected_status = 409 if result["error"] == "ApprovalExpired" else 410
    assert returned["statusCode"] == expected_status, (
        f"error={result['error']!r} mapped to {returned['statusCode']}, expected {expected_status} — "
        f"lambda_function.py:788 is `409 if error == 'ApprovalExpired' else 410`"
    )
    assert json.loads(returned["body"]) == result, (
        f"an error-shaped result must be echoed verbatim — sent {result!r}, got {returned['body']!r}"
    )


@example(result={"decision": "approved", "approval_id": APPROVAL_ID, "processed_by": "an-approver"})
@example(result={"decision": "rejected"})
@given(
    result=st.fixed_dictionaries(
        {"decision": st.sampled_from(["approved", "rejected"])},
        optional={
            "approval_id": st.text(max_size=24),
            "processed_by": st.text(max_size=24),
            "processed_at": st.text(max_size=32),
        },
    )
)
@_PROPERTY_SETTINGS
def test_a_non_error_result_is_200_with_the_result_echoed(result, approval_env):
    """Property 4b, the success arm: no ``"error"`` key means ``200``.

    For any result dict without an ``"error"`` key, ``lambda_handler`` returns
    ``200`` with body
    ``{"message": f"Approval {result['decision']} successfully", "result": result}``
    — the result echoed verbatim under ``result``, same as the error arm echoes
    it at the top level.

    Together with the case above this closes the mapping over
    ``process_approval_decision``'s whole return space: ``"error"`` present →
    ``409``/``410``, absent → ``200``. The concrete ``200`` through the real
    function is pinned further up; this is the shape claim, independent of what
    the real function happens to return today.

    **Validates: Requirements 3.4**
    """
    env = approval_env

    with patch.object(env.handler, "process_approval_decision", return_value=result):
        returned = _returned(*_invoke(env.handler, _decision_mode_event()))

    assert returned["statusCode"] == 200, f"a result with no 'error' key is a success — got {returned!r}"
    body = json.loads(returned["body"])
    assert body == {
        "message": OBSERVED_200_MESSAGE.format(decision=result["decision"]),
        "result": result,
    }, f"the 200 body changed — got {body!r}"
