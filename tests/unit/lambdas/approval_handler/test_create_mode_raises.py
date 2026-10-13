# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 3 — a create-mode failure surfaces as a failure.

# Feature: p34a-code-approval-stranded, Property 3: A create-mode failure surfaces as a failure

For any create-mode event on which ``create_approval_request`` raises a
non-``ValueError`` exception, ``lambda_handler`` propagates the exception rather
than returning a dict, so the Step Functions ``.waitForTaskToken`` state fails
through its ``Catch`` on ``States.ALL`` instead of waiting out
``TimeoutSeconds``.

**Validates: Requirements 1.3, 1.4, 2.3**

WHAT THIS FILE IS
-----------------
A **bug condition exploration** file (spec task 2), and the more consequential
of the two defects p34a covers. Task 1's file pins a crash; this one pins the
crash being *reported as success*. Every assertion below encodes the
**post-fix** expectation, so on the code as it stands the create-mode cases
FAIL — and that failure is the evidence. The same file is re-run unchanged at
task 6.4, where it must pass.

THE MECHANISM, IN ONE PARAGRAPH
-------------------------------
``lambda_handler``'s trailing ``except Exception`` (``lambda_function.py:803-805``)
**returns** ``{"statusCode": 500, "body": ...}``. A Lambda that returns is a
successful invocation. On a ``.waitForTaskToken`` state, Step Functions
therefore records ``TaskSubmitted`` and begins waiting for a token that will
never arrive; the ``Catch`` on ``States.ALL`` fires only on an invocation
*failure*, so it never fires. The state waits ``TimeoutSeconds`` = 259200 and
then raises ``States.Timeout`` — three days after a fault that was detectable
in 49.5 ms. The observed payload, verbatim from the live ``TaskSubmitted``
event, is ``OBSERVED_TASK_SUBMITTED_PAYLOAD`` below.

THE LEDGER ALREADY RECORDS THIS FAILURE SHAPE
---------------------------------------------
``LGR-60c4985-3`` (**template / ratified**) excludes ``waitForTaskToken`` steps
from retry-resume caching for exactly this reason: a cached short-circuit
"would return before the token is ever sent and the ``waitForTaskToken`` state
would hang for its full ``TimeoutSeconds``". Identical shape — a healthy Lambda
return on a token-carrying state with no token sent. Only the trigger differs
(an exception handler rather than a resume cache) and the magnitude (72 h rather
than 20 min). That record is why this defect matters more than the ``Decimal``
one: the serialization fault is one bug, the silent return is the reason nobody
found out about it. ``LGR-69ca78b-2`` (**template**) closes the loop —
``TimeoutSeconds`` is "the worst-case stall", not a backstop, precisely because
``LGR-60c4985-3`` keeps these steps out of the resume cache.

The fix shape task 6.2 will use is also already in the ledger:
``LGR-78ce0b7-13`` / ``LGR-b2f79ef-13`` (**template**, the same decision carried
on two commits) both narrow a blanket ``except Exception`` so an
already-classified exception passes through instead of being re-labelled.

WHY CASE 2 IS AN OBSERVATION AND NOT A ``pytest.raises``
--------------------------------------------------------
Case 2 reaches the same defect through the *genuine* path — a real
``Decimal``-bearing state row, a real ``TypeError`` out of ``json.dumps`` at
``lambda_function.py:413`` — rather than a patched exception. The obvious
spelling, ``pytest.raises(TypeError)``, is wrong here, and the reason is worth
stating because it is not obvious. Enumerate the four worlds for that one
invocation:

====  ==================  ============================================
world  fixes applied       ``lambda_handler`` on the real Decimal path
====  ==================  ============================================
W0     none                returns ``{"statusCode": 500, ...}``
W1     6.1 only            returns ``{"statusCode": 200, ...}``
W2     6.2 only            **raises** ``TypeError``
W3     6.1 + 6.2           returns ``{"statusCode": 200, ...}``
====  ==================  ============================================

W1 and W3 are indistinguishable for this call: once the payload serializes
there is no failure left to report. So ``pytest.raises`` would be true in W2 and
**false in W3** — it would invert at task 6.4, which re-runs this file
unchanged. The claim that is false in W0 and true in W1/W2/W3 is the one this
property is actually about:

    **a create-mode failure must never be reported by returning.**

So case 2 observes the outcome and then branches: a raise is accepted as the
failure surfacing correctly, and a return is accepted only if it is a *genuine*
success — ``200``, with the approval row and the task token actually written.
A ``500``-shaped return is the counterexample. This is the same shape task 1
used for its atomicity case, and for the same reason.

One consequence to state plainly rather than paper over: under this formulation
task 6.2 alone turns this file green, and task 6.1 alone does not (cases 1 and 3
patch their exceptions in, so they are indifferent to the ``Decimal``). The two
defects stay pinned independently — task 1's file is the one that requires 6.1 —
but this file is not a joint gate on both, and no single-file formulation can be,
because W1 and W3 are the same observable behaviour.

WHAT THE PRESERVATION HALVES ARE FOR
------------------------------------
Requirement 3.5 (``ValueError`` → ``400``) and requirement 3.4 (decision mode
returns, never raises) are both easy to break with an over-broad fix — "make
create mode raise" spelled carelessly takes the validation path and the
dashboard path with it. Case 3's mirror half and case 4 live in this file rather
than a sibling so the fix and its fence are read together. They pass today and
must still pass at 6.4. Task 4 owns the full decision-mode status contract
(``200``/``400``/``409``/``410``/``500``); this file only pins the half that
touches the blanket ``except``.

FIXTURES, REGION, MOCKING
-------------------------
Shared fixtures only — ``mock_dynamodb_tables`` and ``mock_lambda_env`` from
``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` call and no boto3 client
or resource mock (``tests/unit/test_no_duplicate_mocking_prop.py``), and
``AWS_REGION`` is **not** set here — the unit layer pins it in
``tests/unit/conftest.py`` (``LGR-3e45e65-1``, template / ratified). It is only
*read*, through the same ``os.environ.get`` expression the handler's own
resource construction uses.

ON THE MODULE-LOADING PREAMBLE — DUPLICATION, DECLARED
------------------------------------------------------
``_load_handler`` / ``_metrics_stub`` / ``approval_env`` below are the third
copy of this preamble in this directory (``test_state_row_echo_canonical.py``
established it, ``test_decimal_serialization_prop.py`` carried it to moto). It
is copied rather than shared, deliberately and not silently:

* it **should** live in ``tests/unit/lambdas/approval_handler/conftest.py`` —
  that filename is one of the four the naming-convention property test allows
  under ``tests/unit/``, so there is somewhere for it to go;
* it is not moved *here* because this task's blast radius is "one new test
  file". A conftest in this directory executes for all eight files in it, and
  ``test_state_row_echo_canonical.py`` documents at its own stub block that it
  is deliberately immune to stub pollution from siblings — perturbing that from
  a conftest is not a change to make inside an exploration task;
* one part of the copy is load-bearing rather than incidental: this file
  monkeypatches module globals (``create_approval_request``,
  ``process_approval_decision``), so it wants its **own** module instance. The
  distinct module name below is what keeps a patch here from leaking into task
  1's file.

The ``metrics_facade`` stub keeps ``time_function`` a real pass-through for the
reason both earlier files record: a bare ``MagicMock`` turns the
``@time_function`` decorator into a call-swallowing mock, the raise this file
hunts for would never surface, and every assertion would be vacuous. The guard
at the end of ``_load_handler`` asserts that did not happen.
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

# ---------------------------------------------------------------------------
# The observed live stall — run-2026-08-08-14-47-44-db94df01
# ---------------------------------------------------------------------------
RUN_ID = "run-2026-08-08-14-47-44-db94df01"
PIPELINE_ID = "hellowrols-aabed79f"
EXECUTION_ARN = (
    "arn:aws:states:us-west-2:777788889999:execution:agentic-data-ingestion-pipeline-dev:"
    "file-customer_data_5_csv-20260808T144743-aa0da5731aec"
)
TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAA" + "x" * 40

#: The ``TaskSubmitted`` event output from the stranded execution, verbatim
#: (`requirements.md` 1.3). Kept here so the counterexample this file produces
#: is directly comparable with the Step Functions history: this is what a
#: 72-hour stall looks like at the moment it becomes inevitable.
OBSERVED_TASK_SUBMITTED_PAYLOAD = (
    '{"statusCode":500,"body":"{\\"error\\": \\"InternalError\\", '
    '\\"message\\": \\"Object of type Decimal is not JSON serializable\\"}"}'
)

#: The four values observed on the failing run, as they come back off the
#: ``create_code`` row: all ``int`` on write
#: (``create_code/lambda_function.py:261-264`` builds them from
#: ``len()``/``.count()``), all ``Decimal`` on read, because DynamoDB has one
#: numeric type and boto3's deserializer returns every ``N`` as ``Decimal``.
OBSERVED_CODE_QUALITY_METRICS: dict[str, Any] = {
    "lines_of_code": 335,
    "imports_count": 18,
    "functions_generated": 1,
    "classes_generated": 0,
}

#: The inline ``test_result`` ``WaitForCodeApproval`` passes. Present so the only
#: ``Decimal`` in case 2 is the one in ``code_quality_metrics`` — the state-row
#: fallback at ``:274`` is not reached and cannot muddy the signal.
INLINE_TEST_RESULT: dict[str, Any] = {
    "status": "passed",
    "checks": [{"name": "syntax_check", "passed": True, "detail": "compiles"}],
    "report_s3_uri": "s3://agentic-data-ingestion-test-outputs/run/test_report.md",
}

#: Stand-in for the Lambda context object. ``lambda_handler`` never reads it;
#: the request id is the one from the observed failure so a captured log line
#: from this test is greppable against the CloudWatch record.
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
    a mock, so no exception could ever reach ``lambda_handler`` and every
    assertion in this file would hold vacuously.
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
            "approval_handler_create_mode_raises_lf",
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
# Events
# ---------------------------------------------------------------------------
def _create_mode_event(**overrides: Any) -> dict[str, Any]:
    """A well-formed create-mode event — ``approval_type`` + ``pipeline_id``.

    ``lambda_handler`` tests for create mode first (``:752``), and
    ``validate_approval_request`` accepts this, so the only way to reach the
    trailing ``except Exception`` from here is through
    ``create_approval_request`` itself. That is the bug condition.
    """
    event = {
        "approval_type": "code_generation",
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "task_token": TASK_TOKEN,
        "execution_arn": EXECUTION_ARN,
    }
    event.update(overrides)
    return event


def _decision_mode_event(**overrides: Any) -> dict[str, Any]:
    """A well-formed decision-mode event — ``approval_id`` + ``decision``.

    Deliberately carries neither ``approval_type`` nor ``pipeline_id``, so the
    create-mode test at ``:752`` cannot claim it.
    """
    event = {
        "approval_id": f"{PIPELINE_ID}_code_generation_1754671698",
        "decision": "approved",
        "comment": "lgtm",
        "user": "an-approver",
    }
    event.update(overrides)
    return event


def _seed_create_code_row(env, code_quality_metrics: Any) -> None:
    """Write the ``create_code`` row at the address p34's R3.6 rewrite reads.

    ``create_code/lambda_function.py:839`` stores the whole approval payload at
    ``output_data.approval``; nothing is written at the row's top level. Reading
    that address correctly is what exposed the crash (``requirements.md`` "Bug
    Analysis"), so a row seeded at the old, never-written top level would
    surface nothing at all.

    The numerics are handed in as plain ``int``s — exactly what the producer
    writes — and come back off the row as ``Decimal``. The exposure is on the
    read side; nothing here has to fake it.
    """
    env.state.put_item(
        Item={
            "run_id": RUN_ID,
            "step_name": "create_code",
            "status": "completed",
            "output_data": {
                "code_version_id": "cv-20260808144744",
                "approval": {
                    "approval_type": "code_generation",
                    "code_version_id": "cv-20260808144744",
                    "generated_code_s3_uri": "s3://agentic-data-ingestion-generated-code/run/job.py",
                    "generated_code_preview": "import sys\nfrom pyspark.sql import functions as F\n",
                    "code_quality_metrics": code_quality_metrics,
                    "dqdl_rules": "Rules = [ ColumnCount > 0 ]",
                    "strands_agent_reasoning": "selected the s3 tables template",
                    "template_selection": {"template_used": "glue_job_s3_tables"},
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# Invocation — observe, do not classify
# ---------------------------------------------------------------------------
def _invoke(handler, event: dict[str, Any]) -> tuple[Exception | None, Any]:
    """Call ``lambda_handler`` and report which of the two outcomes happened.

    Returns ``(raised, returned)`` with exactly one of them set. Broad on
    purpose: the job is to OBSERVE what the handler does, not to classify it.
    Narrowing the ``except`` would encode a specific exception type as the
    expectation, which is the one thing an exploration test must not do.
    """
    try:
        return None, handler.lambda_handler(event, LAMBDA_CONTEXT)
    except Exception as exc:
        return exc, None


def _reported_by_returning(returned: Any) -> str:
    """A self-describing counterexample for a failure that was returned.

    The message quotes the returned dict verbatim and puts it next to the live
    ``TaskSubmitted`` payload, because the two being the same shape IS the
    finding: what this test provokes in 2 ms is what stranded a real execution
    for 72 hours.
    """
    return (
        f"Property 3 / requirement 2.3: lambda_handler did NOT raise — it RETURNED {returned!r}.\n"
        f"A Lambda that returns is a successful invocation, so the .waitForTaskToken state records "
        f"TaskSubmitted, the Catch on States.ALL never fires, and the execution waits out "
        f"TimeoutSeconds (259200s / 72h) before States.Timeout.\n"
        f"Same shape as the live stall's TaskSubmitted payload:\n  {OBSERVED_TASK_SUBMITTED_PAYLOAD}\n"
        f"LGR-60c4985-3 (template / ratified) already records this hazard: a return that skips the "
        f"token send makes the state 'hang for its full TimeoutSeconds'."
    )


# ---------------------------------------------------------------------------
# Case 1 — a patched, arbitrary non-ValueError exception
# ---------------------------------------------------------------------------
class ApprovalStoreUnavailable(Exception):
    """A caller-defined exception, standing for anything the ledger has not met.

    A custom class is included on purpose: the point of Property 3 is not that
    ``TypeError`` propagates, it is that the create-mode path has no exception
    it swallows.
    """


def test_create_mode_propagates_a_patched_exception(approval_env):
    """The minimal statement of the defect, with the trigger held constant.

    ``create_approval_request`` is patched to raise, so nothing about
    serialization, DynamoDB, or the ``Decimal`` is in play — the only thing
    under test is what ``lambda_handler`` does with an exception from create
    mode. Pre-fix it returns ``{"statusCode": 500, ...}``; the assertion message
    carries that dict verbatim.

    Identity (``raised is boom``) rather than type: a re-raise must not relabel
    the failure, because the Step Functions error name is what an operator sees
    first when the ``Catch`` finally fires.

    **Validates: Requirements 1.3, 1.4, 2.3**
    """
    env = approval_env
    boom = ApprovalStoreUnavailable("approvals table unreachable")

    with patch.object(env.handler, "create_approval_request", side_effect=boom):
        raised, returned = _invoke(env.handler, _create_mode_event())

    assert raised is not None, _reported_by_returning(returned)
    assert raised is boom, (
        f"the exception must propagate unchanged, not be re-wrapped — got "
        f"{type(raised).__name__}: {raised}, expected the original "
        f"{type(boom).__name__}: {boom}"
    )


# ---------------------------------------------------------------------------
# Case 2 — the real Decimal TypeError, through the genuine path
# ---------------------------------------------------------------------------
def test_create_mode_never_reports_the_real_decimal_failure_by_returning(approval_env):
    """The same defect reached without patching anything.

    A real ``create_code`` row, real numerics, the real ``TypeError`` out of
    ``json.dumps`` at ``lambda_function.py:413``. Pre-fix (W0) the handler
    returns ``{"statusCode": 500, ...}`` and this fails with that dict verbatim
    — which is the whole counterexample p34a exists to record.

    Written as an observation with two accepted outcomes rather than a
    ``pytest.raises``, because W1 (task 6.1 only) and W3 (both fixes) are
    indistinguishable for this call and a raises-assertion would invert at task
    6.4. See "WHY CASE 2 IS AN OBSERVATION" in the module docstring. The claim
    asserted here holds in W1, W2 and W3 and is false only in W0:

    * it raised → the failure surfaced as a failure. Then nothing may be
      half-written: ``json.dumps`` at ``:413`` runs fifteen lines before
      ``store_task_token`` (``:428``) and eighteen before ``put_item``
      (``:431``).
    * it returned → the return must be a *genuine* success: ``200``, with the
      approval row and the task token actually present. A ``500``-shaped return
      is the defect.

    **Validates: Requirements 1.3, 1.4, 2.3**
    """
    env = approval_env
    _seed_create_code_row(env, OBSERVED_CODE_QUALITY_METRICS)

    raised, returned = _invoke(env.handler, _create_mode_event(test_result=INLINE_TEST_RESULT))

    if raised is not None:
        assert env.approvals.scan(Select="COUNT")["Count"] == 0, (
            f"lambda_handler raised {type(raised).__name__}: {raised} — but an approval row exists, "
            f"so the failure did NOT land before put_item at lambda_function.py:431"
        )
        assert env.tokens.scan(Select="COUNT")["Count"] == 0, (
            f"lambda_handler raised {type(raised).__name__}: {raised} — but a task token was stored, "
            f"so the failure did NOT land before store_task_token at lambda_function.py:428"
        )
        return

    assert isinstance(returned, dict), f"expected a dict return, got {type(returned).__name__}: {returned!r}"
    assert returned.get("statusCode") == 200, _reported_by_returning(returned)

    # A returned 200 is only honest if the writes it claims actually happened.
    approval_id = json.loads(returned["body"])["approval_id"]
    row = env.approvals.get_item(Key={"approval_id": approval_id}).get("Item")
    assert row is not None, (
        f"lambda_handler returned 200 for {approval_id!r} but no approval row exists — "
        f"put_item at lambda_function.py:431 was never reached (requirement 2.2)"
    )
    token = env.tokens.get_item(Key={"approval_id": approval_id}).get("Item")
    assert token is not None and token["task_token"] == TASK_TOKEN, (
        f"lambda_handler returned 200 for {approval_id!r} but no task token was stored — "
        f"the execution would wait on a token that does not exist (requirement 2.2)"
    )
    metrics = json.loads(row["approval_data"])["code_quality_metrics"]
    for key, value in OBSERVED_CODE_QUALITY_METRICS.items():
        assert float(metrics[key]) == float(value), f"$.code_quality_metrics.{key}: {value} -> {metrics[key]}"


# ---------------------------------------------------------------------------
# Case 3 — the property, over arbitrary exception types
# ---------------------------------------------------------------------------
class RetryBudgetExhausted(RuntimeError):
    """A second custom class, one level down from a builtin."""


#: Exception instances spanning what ``create_approval_request`` can realistically
#: raise, plus two caller-defined classes.
#:
#: Two exclusions, both deliberate:
#:
#: * **``ValueError`` and its subclasses.** Property 3's domain is
#:   ``isBugCondition_silentStall`` — "raises a **non**-``ValueError``
#:   exception". ``ValueError`` → ``400`` is preserved behaviour (requirement
#:   3.5) and is pinned separately in case 4.
#: * **non-``Exception`` ``BaseException``s** (``KeyboardInterrupt``,
#:   ``SystemExit``). The blanket clause at ``:803`` catches ``Exception``, so
#:   those already propagate on unfixed code. Including them would put inputs
#:   in the generator that satisfy the property *before* the fix, blunting the
#:   pre-fix signal for no gain in coverage.
_EXCEPTIONS = st.one_of(
    st.builds(RuntimeError, st.text(max_size=24)),
    st.builds(KeyError, st.text(max_size=24)),
    st.builds(TypeError, st.just("Object of type Decimal is not JSON serializable")),
    st.builds(AttributeError, st.text(max_size=24)),
    st.builds(ApprovalStoreUnavailable, st.text(max_size=24)),
    st.builds(RetryBudgetExhausted, st.text(max_size=24)),
    st.builds(BotoCoreError),
    st.builds(
        lambda code, op: ClientError({"Error": {"Code": code, "Message": f"{code} from {op}"}}, op),
        st.sampled_from(
            [
                "ProvisionedThroughputExceededException",
                "ThrottlingException",
                "ValidationException",
                "InternalServerError",
                "ResourceNotFoundException",
            ]
        ),
        st.sampled_from(["PutItem", "UpdateItem", "GetItem"]),
    ),
)

_PROPERTY_SETTINGS = settings(
    max_examples=100,
    # Each example loads nothing and touches no table, but the moto fixture is
    # function-scoped and Hypothesis's 200 ms default is about the wrong thing
    # here: the property is over exception types, not latency.
    deadline=None,
    # One fixture serves every example by design. Neither half of this property
    # writes to a table, so there is no cross-example state to leak.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` excluded: it re-runs hundreds of further examples to
    # annotate a failure whose cause the assertion message already states.
    # Shrinking is kept — the minimal counterexample is the deliverable.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)


@example(exc=TypeError("Object of type Decimal is not JSON serializable"))
@example(exc=KeyError("code_quality_metrics"))
@example(exc=ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "PutItem"))
@given(exc=_EXCEPTIONS)
@_PROPERTY_SETTINGS
def test_create_mode_raises_for_any_non_value_error(exc, approval_env):
    """Property 3 over arbitrary exception types: create mode always raises.

    For any create-mode event on which ``create_approval_request`` raises a
    non-``ValueError`` exception, ``lambda_handler`` propagates it rather than
    returning a dict — so Step Functions fails the state through its ``Catch``
    on ``States.ALL`` instead of waiting out ``TimeoutSeconds``.

    The explicit examples are the three shapes with a name in the record:
    the observed ``Decimal`` ``TypeError``, a missing-key read, and a DynamoDB
    throttle (the one that is genuinely transient and most deserves a retry the
    silent return denies it).

    **Validates: Requirements 1.3, 1.4, 2.3**
    """
    env = approval_env
    assert not isinstance(exc, ValueError), (
        f"the generator produced an input outside the property's precondition — "
        f"isBugCondition_silentStall excludes ValueError, got {exc!r}"
    )

    with patch.object(env.handler, "create_approval_request", side_effect=exc):
        raised, returned = _invoke(env.handler, _create_mode_event())

    assert raised is not None, _reported_by_returning(returned)
    assert raised is exc, (
        f"the exception must propagate unchanged, not be re-wrapped — got "
        f"{type(raised).__name__}: {raised}, expected {type(exc).__name__}: {exc}"
    )


@example(exc=TypeError("Object of type Decimal is not JSON serializable"))
@example(exc=ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "SendTaskSuccess"))
@given(exc=_EXCEPTIONS)
@_PROPERTY_SETTINGS
def test_decision_mode_returns_a_dict_for_any_non_value_error(exc, approval_env):
    """The mirror half — decision mode always returns, and never raises.

    This is the fence, not the defect. Decision mode is invoked by the dashboard
    API, which consumes the status code (``409`` ``ApprovalExpired``, ``410``
    ``TaskTokenNotFound``, ``500`` transient send failure the caller should
    retry — P28b R3.3/R3.5, docstring at ``lambda_function.py:571-583``).
    Turning those into raises would break that caller, so "make create mode
    raise" must not be spelled in a way that takes decision mode with it. Same
    handler, two callers, two correct contracts.

    Passes today and must still pass at task 6.4. Task 4 owns the full
    ``200``/``400``/``409``/``410``/``500`` contract; this pins only the half
    that the blanket ``except`` at ``:803-805`` is responsible for.

    **Validates: Requirements 3.4, 3.5**
    """
    env = approval_env
    assert not isinstance(exc, ValueError), (
        f"the generator produced an input outside this property's domain — got {exc!r}"
    )

    with patch.object(env.handler, "process_approval_decision", side_effect=exc):
        raised, returned = _invoke(env.handler, _decision_mode_event())

    assert raised is None, (
        f"requirement 3.4: decision mode must never raise — its caller is the dashboard API, "
        f"which consumes status codes. Got {type(raised).__name__}: {raised}"
    )
    assert isinstance(returned, dict), f"expected a dict return, got {type(returned).__name__}: {returned!r}"
    assert returned["statusCode"] == 500, (
        f"requirement 3.4: a transient decision-mode failure is reported as 500 so the dashboard "
        f"retries. Got {returned!r}"
    )
    body = json.loads(returned["body"])
    assert body["error"] == "InternalError"
    assert body["message"] == str(exc)


# ---------------------------------------------------------------------------
# Case 4 — ValueError still returns 400 (requirement 3.5)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "event, expected_fragment",
    [
        pytest.param(
            {"approval_type": "code_generation", "pipeline_id": PIPELINE_ID},
            "Missing required field: run_id",
            id="missing-required-field",
        ),
        pytest.param(
            {"approval_type": "not_an_approval_type", "pipeline_id": PIPELINE_ID, "run_id": RUN_ID},
            "Invalid approval type",
            id="invalid-approval-type",
        ),
        pytest.param(
            {"nothing": "that names either mode"},
            "Invalid event format",
            id="neither-mode",
        ),
    ],
)
def test_a_malformed_event_still_returns_400(approval_env, event, expected_fragment):
    """``ValueError`` → ``400`` survives, in all three places it is raised.

    Requirement 3.5: a malformed event is already correctly non-retryable and
    is not part of the stranding mechanism. This is the fence most easily broken
    by an over-broad reading of "make create mode raise" — the first two cases
    are create-mode events, so a fix that re-raises everything from the
    create-mode branch would turn a `400` into a Step Functions failure and
    make a typo look like an outage.

    The third case is not create mode at all: the ``else`` at ``:797-798``
    raises ``ValueError("Invalid event format...")``. It is here because a fix
    shaped as "wrap only the decision-mode branch in the 500 handler" has to
    decide what happens to that ``raise``, and the answer must still be ``400``.

    Passes today (``:799-801``) and must still pass at task 6.4.

    **Validates: Requirements 3.5**
    """
    env = approval_env

    raised, returned = _invoke(env.handler, event)

    assert raised is None, (
        f"requirement 3.5: a malformed event must be reported as 400, not raised — a validation "
        f"error is non-retryable and is not part of the stranding mechanism. Got "
        f"{type(raised).__name__}: {raised}"
    )
    assert returned["statusCode"] == 400, f"expected 400 for {event!r}, got {returned!r}"
    body = json.loads(returned["body"])
    assert body["error"] == "ValidationError"
    assert expected_fragment in body["message"], f"expected {expected_fragment!r} in {body['message']!r}"


# ---------------------------------------------------------------------------
# Case 5 — the BY-ORIGIN rule: a ValueError from INSIDE create_approval_request
#
# Appended at task 6.2, and the only edit made to this file after task 2 wrote
# it. It is here rather than in a sibling because it is the *other half* of case
# 4 directly above: case 4 pins the ValueErrors that must still be 400, this one
# pins the ValueErrors that must not be. Read together they are the whole rule.
#
# WHY THIS WAS NOT WRITTEN AT TASK 2. At the time tasks 2–4 were written the
# rule was undecided — design.md §4.2 says "let non-ValueError exceptions
# propagate", which is a rule about exception TYPE, and asserting anything about
# a ValueError out of create_approval_request would have been asserting a spec
# ambiguity rather than a decision. Task 2's generator excludes ValueError
# explicitly and says so. The rule is now decided, so the decision gets a test.
# ---------------------------------------------------------------------------
#: The ValueError shapes a failure inside ``create_approval_request`` can take.
#:
#: ``json.JSONDecodeError`` is included because it is the realistic *shape*: it
#: is a ``ValueError`` subclass raised by ``json.loads``, and this module reads a
#: JSON blob out of DynamoDB in ``get_pipeline_config``
#: (``lambda_function.py:165``). One honest caveat, checked rather than assumed:
#: that particular call site cannot reach ``lambda_handler`` today, because
#: ``get_pipeline_config`` swallows every exception of its own at
#: ``lambda_function.py:179`` and falls back to the hardcoded default. So the
#: shape is covered on the strength of being a ``ValueError`` subclass that this
#: code path can produce, not on a claim that it is reachable through that one
#: function as it stands. ``PickyValueError`` covers the general subclass case;
#: the bare ``ValueError`` covers the plain one.
_CREATE_MODE_VALUE_ERRORS = [
    pytest.param(ValueError("timeout_hours is not a number"), id="plain-value-error"),
    pytest.param(
        json.JSONDecodeError("Expecting value", "not json", 0),
        id="json-decode-error",
    ),
]


class PickyValueError(ValueError):
    """A ``ValueError`` subclass — ``except ValueError`` catches it by ``isinstance``.

    Present because the by-origin rule has to hold for subclasses too: the clause
    that used to swallow these is ``except ValueError``, which catches the whole
    subtree, so a rule stated only for the exact type would leak.
    """


@pytest.mark.parametrize(
    "exc",
    [*_CREATE_MODE_VALUE_ERRORS, pytest.param(PickyValueError("a subclass still raises"), id="value-error-subclass")],
)
def test_create_mode_raises_a_value_error_raised_inside_create_approval_request(approval_env, exc):
    """A ``ValueError`` out of ``create_approval_request`` RAISES — it is not a 400.

    **This is the by-origin decision, and this test is what distinguishes the two
    ValueError sources.** The line between "report the error by returning" and
    "fail the invocation" is drawn by **ORIGIN, not by TYPE**:

    * a ``ValueError`` from ``validate_approval_request`` — a malformed event —
      still returns ``400`` (requirement 3.5, pinned by case 4 above);
    * **any** exception out of ``create_approval_request``, a ``ValueError``
      included, must **raise** (requirement 2.3, "fails for any reason").

    Both of those are ``ValueError`` reaching the same ``except ValueError``
    clause, so no type-based rule can separate them. That is the whole argument:
    ``create_approval_request`` re-raises whatever it caught
    (``lambda_function.py:503-505``), so under design §4.2's literal "let
    non-``ValueError`` exceptions propagate" a ``ValueError`` originating *inside*
    it would still be caught, returned as ``400``, and the execution would strand
    for its full ``TimeoutSeconds`` — exactly the defect p34a exists to fix,
    surviving the fix. Requirement 2.3 says create mode raises "for any reason";
    requirement 3.5 scopes the ``400`` to a *malformed event*. Only the by-origin
    rule satisfies both.

    ``lambda_handler`` is called with a well-formed event, so
    ``validate_approval_request`` passes and the patched exception is
    unambiguously create-origin. Identity (``raised is exc``) rather than type,
    for the reason case 1 gives: a re-raise must not relabel the failure, because
    the Step Functions error name is what an operator sees first when the
    ``Catch`` on ``States.ALL`` finally fires.

    **Validates: Requirements 2.3, 3.5**
    """
    env = approval_env
    assert isinstance(exc, ValueError), (
        f"this case is specifically about the ValueError subtree — the non-ValueError side is "
        f"covered by cases 1 and 3. Got {exc!r}"
    )

    with patch.object(env.handler, "create_approval_request", side_effect=exc):
        raised, returned = _invoke(env.handler, _create_mode_event())

    assert raised is not None, (
        f"{_reported_by_returning(returned)}\n"
        f"BY ORIGIN, not by type: this {type(exc).__name__} came out of create_approval_request, "
        f"not from validate_approval_request, so it is a create-mode failure and must raise "
        f"(requirement 2.3) — not be returned as the 400 a malformed event gets (requirement 3.5)."
    )
    assert raised is exc, (
        f"the exception must propagate unchanged, not be re-wrapped — got "
        f"{type(raised).__name__}: {raised}, expected {type(exc).__name__}: {exc}"
    )


def test_the_two_value_error_sources_are_told_apart(approval_env):
    """The by-origin rule as one assertion: same type, same clause, two outcomes.

    A malformed create-mode event and a create-origin ``ValueError`` are both
    ``ValueError`` arriving at the same ``except ValueError``. This asserts they
    diverge — ``400`` for the first, a raise for the second — which is the claim
    a type-based rule cannot make. Kept separate from the parametrized case above
    so the contrast is one readable test rather than something a reader has to
    assemble from two files.

    **Validates: Requirements 2.3, 3.5**
    """
    env = approval_env

    # Origin 1: validate_approval_request, on an event missing `run_id`. 400.
    raised, returned = _invoke(env.handler, {"approval_type": "code_generation", "pipeline_id": PIPELINE_ID})
    assert raised is None, f"a malformed event must not raise — got {type(raised).__name__}: {raised}"
    assert returned["statusCode"] == 400, f"expected 400 for a malformed event, got {returned!r}"

    # Origin 2: create_approval_request, same exception type. Raises.
    boom = ValueError("Missing required field: run_id")  # deliberately the same message shape
    with patch.object(env.handler, "create_approval_request", side_effect=boom):
        raised, returned = _invoke(env.handler, _create_mode_event())
    assert raised is boom, (
        f"a ValueError out of create_approval_request must raise even though a ValueError out of "
        f"validate_approval_request returns 400 — the rule is by origin, not by type. "
        f"Got raised={raised!r}, returned={returned!r}"
    )
