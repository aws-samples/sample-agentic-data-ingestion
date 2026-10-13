# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 1 — an approval payload survives DynamoDB-native numeric types.

# Feature: p34a-code-approval-stranded, Property 1: Approval payload survives DynamoDB-native types

For any approval request whose ``get_approval_data`` result contains a
``Decimal`` at any depth, ``create_approval_request`` completes without raising,
writes the approval row, stores the task token when one is supplied, and
produces an ``approval_data`` string whose parsed form preserves every numeric
value.

**Validates: Requirements 2.1, 2.2, 2.5**

WHAT THIS FILE IS
-----------------
A **bug condition exploration** file (spec task 1). Every assertion here
encodes the **post-fix** expected behaviour, so on the code as it stands the
serialization cases FAIL — and that failure is the evidence the diagnosis in
``design.md`` §4.1 is right. The same file is re-run unchanged at task 6.3,
where it must pass.

THE MECHANISM, IN ONE PARAGRAPH
-------------------------------
DynamoDB has a single numeric type, ``N``. boto3's resource-level deserializer
returns **every** ``N`` attribute as ``decimal.Decimal``, regardless of whether
the writer supplied an ``int`` or a ``float``. ``json.dumps`` has no encoder for
``Decimal`` and no ``default=`` is passed at
``approval_handler/lambda_function.py:413``, so any numeric value
``get_approval_data`` surfaces from a state row raises
``TypeError: Object of type Decimal is not JSON serializable``. That raise lands
**before** ``store_task_token`` (``:428``) and before
``approvals_table.put_item`` (``:431``) — which is why the observed live run
stranded with no approval row and no stored token.

WHY THE PROPERTY IS SCOPED BEFORE IT IS WIDENED
-----------------------------------------------
The bug is deterministic, not distributional: one ``Decimal`` anywhere in the
payload is sufficient. So the file starts from the concrete observed failure
(``code_quality_metrics``, the four values from run
``run-2026-08-08-14-47-44-db94df01``) and only then widens to a Hypothesis
property over arbitrary ``Decimal``-bearing structures. The narrow cases give a
readable counterexample; the property proves the boundary is safe for shapes
nobody enumerated.

THREE SIGNALS, DELIBERATELY NOT CONFLATED
-----------------------------------------
===  ==================================  ===========================  =========================
#    claim                                pre-fix                      post-fix
===  ==================================  ===========================  =========================
1    the observed payload serializes,     FAILS (``TypeError``)        passes
     row written, token stored,
     metrics preserved
2    the failure is **atomic** — the      passes, via the "it raised"  passes, via the "it did
     crash lands before ``:428`` and      branch, with both tables     not raise" branch
     ``:431``, so nothing partial is      empty
     written
3    the ``test_failure`` fallback at     3a passes / 3b FAILS         both pass
     ``:293-298`` is exposed too          (see the finding below)
===  ==================================  ===========================  =========================

Case 2 is written as an **atomicity** claim rather than as a bare "both tables
are empty", because a bare emptiness assertion would invert at task 6.3 and the
task requires this file to pass there unchanged. The pre-fix signal is not lost:
on unfixed code the raising branch is the one that executes, and its assertion
is exactly "no row at ``:431``, no token at ``:428``".

THE ``test_failure`` FALLBACK — THE OPEN LEAD, SETTLED
-----------------------------------------------------
``design.md`` §4.1 predicts the ``test_failure`` branch is exposed "on the
fallback path only", on the grounds that ``test_code`` writes the attribute as
``convert_floats_to_decimal(test_result)``
(``test_code/lambda_function.py:1913``). Read end to end, that is right about
the *mechanism* and needs one refinement about *today*: the canonical
``test_result`` the producer builds at ``test_code/lambda_function.py:1759-1762``
is ``{status, checks, report_s3_uri}``, and every leaf of it is a ``str`` or a
``bool`` — the ``checks`` entries carry ``name``/``passed``/``detail`` and
optionally ``verified``/``not_verified_reason`` (``:471-566``), and the
schema-compliance *score* is interpolated into the ``detail`` **text** rather
than stored as a number (``:544-563``). So:

* **Case 3a** seeds that canonical shape verbatim. It does **not** crash on
  unfixed code — the branch is *latently* exposed, not actively broken today.
* **Case 3b** seeds the same attribute with a numeric leaf. It crashes on
  unfixed code, which is the exposure proof: the fallback read at ``:297``
  surfaces whatever is on the row, and anything numeric arrives as ``Decimal``.

Both are asserted at their post-fix expectation, so 3a is a passing guard and 3b
is the counterexample. This is what makes task 6.1's *boundary* conversion the
right shape rather than a patch on the one branch that happens to be failing.

FIXTURES, REGION, MOCKING
-------------------------
Shared fixtures only — ``mock_dynamodb_tables`` and ``mock_lambda_env`` from
``tests/fixtures/conftest.py``. There is no ``moto.mock_aws()`` call and no
boto3 client or resource mock in this file
(``tests/unit/test_no_duplicate_mocking_prop.py``), and it does **not** set
``AWS_REGION`` — the unit layer pins it in ``tests/unit/conftest.py``
(``LGR-3e45e65-1``, template / ratified). The region is *read*, through the same
``os.environ.get`` expression the handler's own resource construction relies on.

The handler module is loaded **inside** the moto context rather than at import
time, because a boto3 resource constructed before ``mock_aws()`` starts is not
intercepted. Its four table globals are then rebound to the fixture's own Table
objects, so nothing in this file depends on moto's start/stop internals.

``eventbridge_utils`` / ``metrics_facade`` / ``notification_service`` are the
Lambda-layer modules the handler imports; pass-through stubs are installed for
the duration of the load and then restored. This is the preamble
``test_state_row_echo_canonical.py`` established in this directory, and it is
load-bearing for the same reason it documents: a bare ``MagicMock`` for
``metrics_facade`` turns the ``@time_function`` decorator on
``create_approval_request`` into a call-swallowing mock, which would make every
assertion below meaningless. The guard at the end of ``_load_handler`` asserts
that did not happen.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
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
# The observed live failure — run-2026-08-08-14-47-44-db94df01
# ---------------------------------------------------------------------------
#: The run that stranded. Kept verbatim so the counterexample this file produces
#: is comparable with the CloudWatch record in `requirements.md`.
RUN_ID = "run-2026-08-08-14-47-44-db94df01"
PIPELINE_ID = "hellowrols-aabed79f"
EXECUTION_ARN = (
    "arn:aws:states:us-west-2:777788889999:execution:agentic-data-ingestion-pipeline-dev:"
    "file-customer_data_5_csv-20260808T144743-aa0da5731aec"
)
#: Real tokens are ~600-700 chars and `validate_approval_request` is careful not
#: to truncate them; length is irrelevant to this property, presence is not.
TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAA" + "x" * 40

#: The four values observed on the failing run, as they come back off the
#: `create_code` row: all `int` on write
#: (`create_code/lambda_function.py:261-264` builds them from `len()`/`.count()`),
#: all `Decimal` on read, because DynamoDB has one numeric type.
OBSERVED_CODE_QUALITY_METRICS: dict[str, Decimal] = {
    "lines_of_code": Decimal("335"),
    "imports_count": Decimal("18"),
    "functions_generated": Decimal("1"),
    "classes_generated": Decimal("0"),
}

#: The inline `test_result` `WaitForCodeApproval` passes
#: (`orchestration-stack.ts:639`; confirmed present in the deployed definition,
#: `requirements.md` "the deploy took correctly"). Included so the only
#: `Decimal` in the observed case is the one in `code_quality_metrics` — the
#: state-row fallback at `:274` is not reached and cannot muddy the signal.
INLINE_TEST_RESULT: dict[str, Any] = {
    "status": "passed",
    "checks": [{"name": "syntax_check", "passed": True, "detail": "compiles"}],
    "report_s3_uri": "s3://agentic-data-ingestion-test-outputs/run/test_report.md",
}

#: `test_code/lambda_function.py:1759-1762` verbatim, in shape: every leaf a
#: `str` or a `bool`. Read `:471-566` before adding a number here — the
#: schema-compliance score is text, deliberately.
CANONICAL_TEST_RESULT: dict[str, Any] = {
    "status": "failed",
    "checks": [
        {"name": "syntax_check", "passed": True, "detail": "compiles"},
        {"name": "transform_execution", "passed": False, "detail": "to_date raised"},
        {
            "name": "schema_compliance",
            "passed": False,
            "detail": "Schema compliance 60: 2 declared target column(s) missing from the transform output: a, b",
            "verified": False,
            "not_verified_reason": "no_target_schema",
        },
    ],
    "report_s3_uri": "s3://agentic-data-ingestion-test-outputs/run/test_report.md",
}


# ---------------------------------------------------------------------------
# Loading the handler inside the moto context
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

    The handler decorates ``create_approval_request`` with
    ``@time_function(...)``. A bare ``MagicMock`` would replace the function
    with a mock that swallows the call and returns a mock, so the raise this
    file is looking for would never surface and every assertion would be
    vacuous.
    """
    stub = MagicMock()
    stub.time_function = lambda *_a, **_k: lambda f: f
    stub.record_error = MagicMock()
    stub.ApprovalMetrics = MagicMock()
    stub.PipelineMetrics = None
    return stub


def _load_handler():
    """Exec the handler module once, inside whatever moto context is active.

    Deferred rather than done at import time because the module builds a boto3
    resource and three Table handles at module scope, and a resource constructed
    before ``mock_aws()`` starts is not intercepted by it. The fixture rebinds
    all four afterwards regardless, so the load context only has to be safe,
    not authoritative.
    """
    global _HANDLER
    if _HANDLER is not None:
        return _HANDLER

    stubs = {**_LAYER_STUBS, "metrics_facade": _metrics_stub()}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "approval_handler_decimal_serialization_lf",
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

    assert inspect.isfunction(module.create_approval_request), (
        "create_approval_request is not a plain function — the @time_function decorator was "
        "replaced by a mock, which would make every assertion in this file vacuous"
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
# Seeding — the producers' real addresses
# ---------------------------------------------------------------------------
def _seed_create_code_row(env, code_quality_metrics: Any) -> None:
    """Write the ``create_code`` row at the address p34's R3.6 rewrite reads.

    ``create_code/lambda_function.py:839`` stores the whole approval payload at
    ``output_data.approval``; nothing is written at the row's top level. Getting
    that address right is the point — reading it correctly is what exposed the
    crash (``requirements.md`` "Bug Analysis"), so a test seeding the old,
    never-written top level would surface nothing.
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


def _seed_test_code_row(env, test_result: Any) -> None:
    """Write the ``test_code`` row's ``test_result`` attribute.

    ``test_code/lambda_function.py:1913`` writes
    ``convert_floats_to_decimal(test_result)`` under the canonical step name, so
    the values handed in here stand for what that walk produces: numerics as
    ``Decimal``, everything else untouched.
    """
    env.state.put_item(
        Item={
            "run_id": RUN_ID,
            "step_name": "test_code",
            "status": "failed",
            "test_status": "failed",
            "test_result": test_result,
        }
    )


def _code_generation_event(*, task_token: str = TASK_TOKEN, inline_test_result: Any = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "approval_type": "code_generation",
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "task_token": task_token,
        "execution_arn": EXECUTION_ARN,
    }
    if inline_test_result is not None:
        event["test_result"] = inline_test_result
    return event


def _test_failure_event(*, task_token: str = TASK_TOKEN) -> dict[str, Any]:
    """A ``test_failure`` request with **no** inline ``test_result``.

    The absence is the whole point: it is what drives the fallback state read at
    ``lambda_function.py:293-298``.
    """
    return {
        "approval_type": "test_failure",
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "task_token": task_token,
        "execution_arn": EXECUTION_ARN,
    }


# ---------------------------------------------------------------------------
# The oracle — numeric preservation, derived from the requirement not the fix
# ---------------------------------------------------------------------------
def assert_values_preserved(stored: Any, parsed: Any, path: str = "$") -> None:
    """Every leaf of ``stored`` survives into ``parsed`` with its value and kind.

    Deliberately converter-agnostic: it walks the structure that was *written to
    DynamoDB* and compares leaf by leaf, so it would hold for any correct fix
    and does not encode ``convert_decimals_to_float``'s particular choice of
    output type. Requirement 2.1 mandates that specific converter; this oracle
    checks the requirement the converter exists to satisfy.

    Two kind checks are not negotiable:

    * a numeric must stay **numeric** — a ``default=str`` encoder would keep the
      value legible but hand every consumer a string;
    * a ``bool`` must stay **bool**. ``bool`` subclasses ``int``, and
      ``shared/decimal_utils.py``'s docstring records that it is deliberately
      NOT converted, so ``True`` must serialize as ``true`` and never widen to
      ``1.0``.
    """
    # bool first: it subclasses int, so an isinstance chain that tests numbers
    # first would silently accept True == 1.
    if isinstance(stored, bool):
        assert parsed is stored, f"{path}: bool must stay bool, got {parsed!r} ({type(parsed).__name__})"
    elif isinstance(stored, Decimal):
        assert isinstance(parsed, int | float) and not isinstance(parsed, bool), (
            f"{path}: a DynamoDB numeric must serialize as a JSON number, got {parsed!r} ({type(parsed).__name__})"
        )
        assert float(parsed) == float(stored), f"{path}: numeric value changed, {stored} -> {parsed}"
    elif isinstance(stored, dict):
        assert isinstance(parsed, dict), f"{path}: expected an object, got {type(parsed).__name__}"
        assert set(parsed) == set(stored), (
            f"{path}: key set changed, missing={sorted(set(stored) - set(parsed))} "
            f"unexpected={sorted(set(parsed) - set(stored))}"
        )
        for key in stored:
            assert_values_preserved(stored[key], parsed[key], f"{path}.{key}")
    elif isinstance(stored, list):
        assert isinstance(parsed, list), f"{path}: expected an array, got {type(parsed).__name__}"
        assert len(parsed) == len(stored), f"{path}: length changed, {len(stored)} -> {len(parsed)}"
        for index, item in enumerate(stored):
            assert_values_preserved(item, parsed[index], f"{path}[{index}]")
    else:
        assert parsed == stored, f"{path}: value changed, {stored!r} -> {parsed!r}"


def _approval_row(env, approval_id: str) -> dict[str, Any]:
    item = env.approvals.get_item(Key={"approval_id": approval_id}).get("Item")
    assert item is not None, (
        f"requirement 2.2: no approval row was written for {approval_id!r} — "
        f"put_item at lambda_function.py:431 was never reached"
    )
    return item


def _stored_token(env, approval_id: str) -> str:
    item = env.tokens.get_item(Key={"approval_id": approval_id}).get("Item")
    assert item is not None, (
        f"requirement 2.2: no task token was stored for {approval_id!r} — "
        f"store_task_token at lambda_function.py:428 was never reached"
    )
    return str(item["task_token"])


def _parsed_approval_data(env, approval_id: str) -> dict[str, Any]:
    raw = _approval_row(env, approval_id)["approval_data"]
    assert not str(raw).endswith("...(truncated)"), (
        "the payload was trimmed at 50000 chars, so it cannot be parsed — keep the "
        "generated payloads small enough that the trim (requirement 3.3) is not in play here"
    )
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Case 1 — the observed live failure
# ---------------------------------------------------------------------------
def test_observed_code_quality_metrics_serialize_and_the_row_is_written(approval_env):
    """The four values from run ``run-2026-08-08-14-47-44-db94df01``, end to end.

    Pre-fix this raises ``TypeError: Object of type Decimal is not JSON
    serializable`` from ``json.dumps`` at ``lambda_function.py:413``. That raise
    IS the expected result of spec task 1.

    **Validates: Requirements 2.1, 2.2, 2.5**
    """
    env = approval_env
    _seed_create_code_row(env, OBSERVED_CODE_QUALITY_METRICS)

    approval_id = env.handler.create_approval_request(_code_generation_event(inline_test_result=INLINE_TEST_RESULT))

    assert _stored_token(env, approval_id) == TASK_TOKEN
    row = _approval_row(env, approval_id)
    assert row["approval_type"] == "code_generation"
    assert row["status"] == "pending"
    assert row["has_task_token"] is True

    metrics = _parsed_approval_data(env, approval_id)["code_quality_metrics"]
    assert_values_preserved(OBSERVED_CODE_QUALITY_METRICS, metrics, "$.code_quality_metrics")


# ---------------------------------------------------------------------------
# Case 2 — where the failure lands, kept separate from Case 1
# ---------------------------------------------------------------------------
def test_a_serialization_failure_writes_nothing_at_all(approval_env):
    """The failure is atomic: either both writes happened, or neither did.

    This is the claim that localises the defect. ``json.dumps`` at ``:413`` runs
    **fifteen lines before** ``store_task_token`` (``:428``) and eighteen before
    ``approvals_table.put_item`` (``:431``), so a raise there leaves no token
    and no row — which is exactly the live symptom: an execution waiting on a
    token that was never stored, and a dashboard with no pending approval to
    show (requirement 1.2, 1.6).

    Written as an atomicity claim rather than a bare "both tables are empty" so
    that it holds on both sides of the fix; task 6.3 re-runs this file
    unchanged. On unfixed code it is the raising branch that executes, and that
    branch asserts the emptiness directly.

    **Validates: Requirements 1.2, 1.6, 2.2**
    """
    env = approval_env
    _seed_create_code_row(env, OBSERVED_CODE_QUALITY_METRICS)

    raised: Exception | None = None
    approval_id: str | None = None
    try:
        approval_id = env.handler.create_approval_request(_code_generation_event(inline_test_result=INLINE_TEST_RESULT))
    # Broad on purpose: the point is to OBSERVE what the handler does, not to
    # classify it. Narrowing to TypeError would encode the pre-fix behaviour as
    # the expectation, which is the one thing an exploration test must not do.
    except Exception as exc:
        raised = exc

    approvals_count = env.approvals.scan(Select="COUNT")["Count"]
    tokens_count = env.tokens.scan(Select="COUNT")["Count"]

    if raised is not None:
        # Unfixed code takes this branch. The message carries the verbatim
        # counterexample so the failure record in task 1 is self-describing.
        assert approvals_count == 0, (
            f"create_approval_request raised {type(raised).__name__}: {raised} — but "
            f"{approvals_count} approval row(s) exist, so the crash did NOT land before "
            f"put_item at lambda_function.py:431"
        )
        assert tokens_count == 0, (
            f"create_approval_request raised {type(raised).__name__}: {raised} — but "
            f"{tokens_count} task token(s) exist, so the crash did NOT land before "
            f"store_task_token at lambda_function.py:428"
        )
    else:
        assert approval_id is not None
        assert approvals_count == 1, f"expected exactly the one approval row, found {approvals_count}"
        assert tokens_count == 1, f"expected exactly the one stored token, found {tokens_count}"
        _approval_row(env, approval_id)
        assert _stored_token(env, approval_id) == TASK_TOKEN


# ---------------------------------------------------------------------------
# Case 3 — the test_failure fallback at :293-298
# ---------------------------------------------------------------------------
def test_test_failure_fallback_survives_the_producers_current_shape(approval_env):
    """3a: the canonical ``test_result`` today carries no numeric leaf.

    ``test_code/lambda_function.py:1759-1762`` builds ``{status, checks,
    report_s3_uri}`` and every leaf is a ``str`` or a ``bool`` — the
    schema-compliance score is interpolated into ``detail`` as text (``:544-563``),
    not stored as a number. So this case passes on unfixed code, and that is a
    finding rather than a hole: the branch is *latently* exposed, which is why
    3b below is the one that carries the exposure proof.

    **Validates: Requirements 2.5**
    """
    env = approval_env
    _seed_test_code_row(env, CANONICAL_TEST_RESULT)

    approval_id = env.handler.create_approval_request(_test_failure_event())

    parsed = _parsed_approval_data(env, approval_id)
    # The fallback read at :297 is what supplied this — the event carried no
    # inline test_result, so an equal payload can only have come off the row.
    assert_values_preserved(CANONICAL_TEST_RESULT, parsed["test_result"], "$.test_result")
    assert parsed["test_status"] == "failed"
    assert parsed["failed_checks"] == ["transform_execution", "schema_compliance"]
    assert _stored_token(env, approval_id) == TASK_TOKEN


def test_test_failure_fallback_survives_a_numeric_bearing_test_result(approval_env):
    """3b: a numeric anywhere on that attribute reaches ``:413`` as a ``Decimal``.

    ``test_code`` writes the attribute as
    ``convert_floats_to_decimal(test_result)``
    (``test_code/lambda_function.py:1913``), and DynamoDB returns every ``N``
    attribute as ``Decimal`` regardless of what was written — so the exposure
    does not depend on the producer choosing a ``float`` over an ``int``. This
    settles the lead ``design.md`` §4.1 left open: the fallback path IS exposed,
    on the same mechanism as ``code_quality_metrics``, and pre-fix this raises.

    **Validates: Requirements 2.5**
    """
    env = approval_env
    numeric_test_result = {
        **CANONICAL_TEST_RESULT,
        "checks_run": Decimal("6"),
        "checks_failed": Decimal("2"),
        "duration_seconds": Decimal("12.5"),
        "checks": [
            {**CANONICAL_TEST_RESULT["checks"][0], "row_count": Decimal("335")},
            *CANONICAL_TEST_RESULT["checks"][1:],
        ],
    }
    _seed_test_code_row(env, numeric_test_result)

    approval_id = env.handler.create_approval_request(_test_failure_event())

    parsed = _parsed_approval_data(env, approval_id)
    assert_values_preserved(numeric_test_result, parsed["test_result"], "$.test_result")
    assert _stored_token(env, approval_id) == TASK_TOKEN


# ---------------------------------------------------------------------------
# Widening: the property over arbitrary Decimal-bearing payloads
# ---------------------------------------------------------------------------
#: Map keys. DynamoDB map keys are strings and `json.dumps` needs them to be,
#: so the alphabet is constrained rather than the failures being interesting.
_KEYS = st.text(alphabet="abcdefgHIJ_0123456789", min_size=1, max_size=8)

#: Numerics land in DynamoDB as `N` and come back as `Decimal` whatever the
#: writer supplied, so both an integral and a fractional generator produce
#: `Decimal` here.
#:
#: The fractional branch is `st.decimals(..., places=6)` rather than
#: `st.floats(...).map(Decimal)` on purpose, and the reason was measured rather
#: than assumed: a float range bound only at ±1e6 still draws **subnormals**,
#: and `Decimal("2.225073858507203E-309")` is rejected outright by
#: `boto3.dynamodb.types.DYNAMODB_CONTEXT` with `decimal.Underflow` — DynamoDB's
#: numeric range stops at a magnitude of 1e-130. That would have failed the
#: property in the *seed write*, for a reason with nothing to do with the fix.
#: `places=6` floors the nonzero magnitude at 1e-6, comfortably inside the
#: range, and keeps the significant-digit count far below DynamoDB's 38.
_DECIMALS = st.one_of(
    st.integers(min_value=-(10**9), max_value=10**9).map(Decimal),
    st.decimals(
        min_value=Decimal("-1000000"),
        max_value=Decimal("1000000"),
        places=6,
        allow_nan=False,
        allow_infinity=False,
    ),
)

#: `bool` is in here deliberately and is the one leaf kind with a named hazard:
#: it subclasses `int`, and `shared/decimal_utils.py`'s docstring records that
#: it is NOT converted. A fix that widened `True` to `1.0` would pass a
#: numerics-only property and fail this one.
_LEAVES = st.one_of(
    _DECIMALS,
    st.booleans(),
    st.none(),
    st.text(max_size=12),
)

_STRUCTURES = st.recursive(
    _LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_KEYS, children, max_size=4),
    ),
    max_leaves=10,
)


def _contains_decimal(obj: Any) -> bool:
    """``isBugCondition_serialization``'s ``contains_decimal``, verbatim in intent."""
    if isinstance(obj, Decimal):
        return True
    if isinstance(obj, dict):
        return any(_contains_decimal(value) for value in obj.values())
    if isinstance(obj, list):
        return any(_contains_decimal(item) for item in obj)
    return False


@st.composite
def decimal_bearing_payloads(draw) -> dict[str, Any]:
    """A ``code_quality_metrics`` value guaranteed to satisfy the bug condition.

    The draw is allowed to place ``Decimal``s at any depth — that is the part of
    the input space the narrow cases above cannot reach. When it happens not to
    draw one, a guaranteed numeric is added alongside rather than the example
    being discarded, so the property never spends draws on inputs outside its
    own precondition.
    """
    payload = draw(st.dictionaries(_KEYS, _STRUCTURES, min_size=1, max_size=4))
    if not _contains_decimal(payload):
        payload = {"nested": payload, "lines_of_code": draw(_DECIMALS)}
    return payload


def _wipe(env) -> None:
    """Clear the two tables the assertions read.

    One moto backend serves every Hypothesis example, and ``approval_id`` is
    ``{pipeline_id}_{approval_type}_{int(time.time())}`` — so two examples in
    the same second would otherwise share an id and a row written by the first
    could satisfy the second. Runs before each example, in the harness.
    """
    for table, key_name in ((env.approvals, "approval_id"), (env.tokens, "approval_id")):
        for item in table.scan(ProjectionExpression=key_name).get("Items", []):
            table.delete_item(Key={key_name: item[key_name]})


@example(payload=OBSERVED_CODE_QUALITY_METRICS)
@example(payload={"depth": {"1": {"2": [{"3": Decimal("0.5")}]}}})
@example(payload={"flag": True, "count": Decimal("0"), "missing": None, "note": ""})
@example(payload={"empties": {"map": {}, "list": []}, "lines_of_code": Decimal("335")})
@given(payload=decimal_bearing_payloads())
@settings(
    max_examples=100,
    # Each example writes a state row and runs create_approval_request against
    # moto, which routinely exceeds Hypothesis's 200ms default. The property is
    # about serialization, not latency.
    deadline=None,
    # One fixture serves every example by design; `_wipe` runs at the start of
    # each one, so nothing an earlier example wrote is visible later.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` excluded: it re-runs hundreds of further examples to
    # annotate a failure whose cause the assertion message already states.
    # Shrinking is kept — the minimal counterexample is the deliverable.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_any_decimal_bearing_payload_serializes_and_round_trips(payload, approval_env):
    """Property 1 over arbitrary ``Decimal``-bearing payload structures.

    For any ``code_quality_metrics`` value containing a ``Decimal`` at any
    depth: ``create_approval_request`` does not raise, the approval row is
    written, the task token is stored, and every value in the parsed
    ``approval_data`` survives with its numeric value and its kind.

    **Validates: Requirements 2.1, 2.2, 2.5**
    """
    env = approval_env
    assert _contains_decimal(payload), (
        f"the generator produced an input outside the property's precondition — "
        f"isBugCondition_serialization is false for {payload!r}"
    )

    _wipe(env)
    _seed_create_code_row(env, payload)

    approval_id = env.handler.create_approval_request(_code_generation_event(inline_test_result=INLINE_TEST_RESULT))

    assert _stored_token(env, approval_id) == TASK_TOKEN
    parsed = _parsed_approval_data(env, approval_id)
    assert_values_preserved(payload, parsed["code_quality_metrics"], "$.code_quality_metrics")
