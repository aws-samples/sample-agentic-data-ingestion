# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 2 — Decimal-free payloads and the payload shapes around them survive the fix.

# Feature: p34a-code-approval-stranded, Property 2: Decimal-free payloads and decision-mode returns

For any input where the bug condition does NOT hold — an approval payload
containing no ``Decimal`` — the fixed ``create_approval_request`` produces the
same result as the original: byte-identical ``json.dumps`` output, the same
50000-character trim, and the same ``schema_mapping`` payload shape.

**Validates: Requirements 3.1, 3.2, 3.3**

Requirement 3.2's half of that — the ``schema_mapping`` payload shape — was
**deliberately reversed by p50 C1**, which corrected the branch's read. Case 4
below is amended in place with the reason at its assertions rather than deleted
(``LGR-60c4985-6``, ``LGR-9e76249-10``). Requirements 3.1 and 3.3 are untouched.

WHAT THIS FILE IS
-----------------
A **preservation** file (spec task 3), and the opposite kind of test to its two
siblings. ``test_decimal_serialization_prop.py`` and
``test_create_mode_raises.py`` encode post-fix behaviour and therefore FAIL
today; every assertion here describes behaviour that must NOT change, so this
file PASSES on the code as it stands and must still pass after tasks 6.1 and
6.2. Task 6.5 re-runs it unchanged.

Property 2 as stated in ``design.md`` §6 also covers the decision-mode status
contract (requirements 3.4, 3.5). That half belongs to **task 4** and is
deliberately absent here; ``test_create_mode_raises.py`` already pins the
``ValueError`` → ``400`` fence in all three places that ``ValueError`` is
raised, and the ``500``-on-transient-decision-failure return. One point worth
recording so nobody adds a contradicting assertion to this file later: the
``raise``-vs-``400`` line has been settled **by origin, not by type**. A
``ValueError`` out of ``validate_approval_request`` (a malformed event) still
returns ``400``; **any** exception propagating out of
``create_approval_request`` — a ``ValueError`` included, e.g. the
``json.JSONDecodeError`` ``get_pipeline_config`` could raise off a corrupt
``pipeline_config`` attribute — must be raised. So "a ``ValueError`` returns
``400``" is NOT a preservation claim, and is not asserted anywhere below.

OBSERVATION-FIRST — WHAT WAS ACTUALLY MEASURED
---------------------------------------------
Every number and every string below was read off the **unfixed** handler before
it was written down, not assumed. What the probe found:

* the serialization step is plain ``json.dumps`` with no keyword arguments —
  ``json.dumps(get_approval_data(...)) == row["approval_data"]`` exactly, so
  ``ensure_ascii`` escaping (``"caf\\u00e9"``), default separators
  (``", "`` / ``": "``) and insertion key order are all in the observed output;
* an ``int`` that never went through DynamoDB stays an ``int``: ``335`` →
  ``335``, not ``335.0``;
* floats survive verbatim, artifacts included — ``0.30000000000000004``,
  ``1e+22``, ``5e-324``, ``-0.0``;
* the trim: a 60218-character payload became **50014** characters —
  ``untrimmed[:50000] + "...(truncated)"`` exactly — with one warning,
  ``"Trimmed approval_data from 60218 to 50000 chars"``;
* the ``schema_mapping`` branch really did return **nine keys with exactly
  eight ``None``s**, measured against a row seeded the way the producer writes
  one, with only ``generated_by`` carrying a value. **p50 C1 reversed this on
  purpose** — see case 4 below, which now asserts the corrected shape and states
  why the reversal is not a regression. The observation stands as the record of
  what was measured on the unfixed handler.

Two of those were not what a reader of the fix would guess, so they are called
out rather than buried:

**The ``int`` is only safe because it never touched DynamoDB.** ``335`` staying
``335`` is a fact about a payload that came in on the Step Functions event. The
same field read off a state row arrives as ``Decimal("335")`` and, after task
6.1, serializes as ``335.0``. That widening is accepted (``design.md`` §4.1 — no
consumer reads those fields). Nothing here contradicts it: this file's
precondition is a ``Decimal``-free payload, and the assertion is that such a
payload is left alone.

**``5e-324`` serializes fine here and would be rejected on the write path.**
Task 1's generator comments record that
``boto3.dynamodb.types.DYNAMODB_CONTEXT`` raises ``decimal.Underflow`` for
``Decimal("2.2e-309")`` because DynamoDB's numeric range bottoms out near
1e-130. That wall is on the *write* to a state row. The payloads in this file
never cross it, which is the whole trick — see below.

THE FLOAT / DECIMAL ROUND-TRIP PROBLEM, AND HOW IT IS SOLVED
-----------------------------------------------------------
A ``float`` written into a state row does not come back a ``float``. It cannot
even be written: boto3's resource serializer rejects ``float`` outright and
requires ``Decimal``, and every ``N`` attribute is returned as ``Decimal``
regardless of what the writer supplied. So a payload *seeded through a state
row* is never ``Decimal``-free by the time ``get_approval_data`` returns it, and
a test that seeded floats there would be testing the bug condition, not its
complement — the precondition would be false and the property vacuous or
inverted.

The way out is not to patch ``get_approval_data``. It is to use the branch that
**already** produces a ``Decimal``-free payload in production: ``test_failure``
with an **inline** ``test_result``. ``ForceTestReviewApproval`` passes that
payload on the Step Functions event, so it reaches ``get_approval_data``
(``lambda_function.py:293``) as parsed JSON — ``float``, ``int``, ``bool``,
``None``, ``str``, ``dict``, ``list``, and never ``Decimal``. ``design.md``
§4.1's branch table says exactly this about that path: "the inline-SFN primary
path is JSON from Step Functions, so floats, safe". The payload goes on to be
stored as a JSON **string**, so the floats never meet DynamoDB's serializer in
either direction.

That choice buys three things a patched ``get_approval_data`` would not:

1. the precondition is genuine *and checked* — ``_assert_decimal_free`` walks
   the payload ``get_approval_data`` actually returned, so the round-trip trap
   would be caught rather than assumed away;
2. ``float`` is reachable at all, including the subnormals and the repr
   artifacts that make byte-identity a real constraint;
3. no production function is stubbed, so the read path under test is the
   deployed one.

WHY ``get_approval_data`` IS AN HONEST ORACLE HERE
-------------------------------------------------
The reference string is ``json.dumps(get_approval_data(...))``, computed
**before** ``create_approval_request`` runs. That is not circular with respect
to the change under test: tasks 6.1 and 6.2 change the serialization boundary
and the exception path, and ``design.md`` §3 forbids changing what any
``get_approval_data`` branch reads ("Do NOT change what ``get_approval_data``'s
branches read"). So the payload is fixed across the fix and the only thing the
comparison can detect is a change at ``json.dumps`` — which is precisely what
an over-broad fix would be. A fix that normalised floats, reordered keys, or
switched to ``default=str`` fails this assertion; ``convert_decimals_to_float``
applied to a ``Decimal``-free payload does not, because it rebuilds ``dict``s by
comprehension (order preserved) and passes ``float``/``int``/``bool``/``None``/
``str`` through untouched (``shared/decimal_utils.py:60-80``, read in full, and
checked against the four plausible over-broad spellings: widening every ``int``,
widening ``bool`` with it, ``sort_keys=True``, and ``ensure_ascii=False`` are all
detected by the assertions below).

WHAT THIS FILE DELIBERATELY DOES NOT DUPLICATE
----------------------------------------------
Three sibling files already assert things about ``create_approval_request``'s
output, and each was read before this one was written:

* ``test_code_generation_payload_prop.py`` pins the ``code_generation``
  branch's ``test_result`` ``??``-chain and its precedence (p34 R3.6). This file
  asserts nothing about which paths a branch reads.
* ``test_advertised_window_precedence.py`` pins ``timeout_at`` resolution
  (``LGR-69ca78b-2``, **template**). ``design.md`` §5 notes the per-pipeline
  override is currently unreachable because ``get_pipeline_config`` never finds
  a ``pipeline_config`` attribute; that is a filed follow-up, **not** something
  to fix or to re-pin here. This file asserts nothing about ``timeout_at``.
* ``test_state_row_echo_canonical.py`` pins the canonical-step status echo (P28
  R8.3). This file asserts nothing about the echo.

One overlap is real and is a *sharpening*, not a duplicate:
``test_approval_handler_evidence.py:216-266`` (``test_large_evidence_triggers_trim``)
already exercises the trim, but asserts only the upper bound
``len(approval_data) <= 50000 + len("...(truncated)")``. That bound holds for a
fix that trimmed at 10 characters. Requirement 3.3 asks for the *behaviour*, so
the test below pins the exact truncation point, the exact suffix, the exact
resulting length, and the warning text — and adds the other half of the
boundary, that a payload under the limit is not trimmed and logs nothing.

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
``_load_handler`` / ``_metrics_stub`` / ``approval_env`` below are the **fourth**
copy of this preamble in this directory (``test_state_row_echo_canonical.py``
established it, ``test_decimal_serialization_prop.py`` carried it to moto,
``test_create_mode_raises.py`` declared the duplication and recommended a
conftest). It is copied again rather than extracted, and the reasoning has
changed since task 2 only in being more decisive:

* extracting it to ``tests/unit/lambdas/approval_handler/conftest.py`` would not
  actually remove a copy unless tasks 1 and 2's files were edited to consume it,
  and those files are complete — task 1's counterexamples and task 2's are the
  spec's evidence, and editing them mid-fix would put that evidence in doubt;
* moving the preamble into a conftest *for this file only* would add a fifth
  location while removing none, and a conftest in this directory executes for
  all ten test files in it — including ``test_state_row_echo_canonical.py``, which
  documents at its own stub block that it installs stubs by assignment
  specifically to be immune to sibling pollution. Perturbing that from a
  conftest is not a change to make while three of the directory's files are
  mid-flight;
* the extraction is still worth doing. It belongs **after** task 6.5 has
  confirmed all four property files green, as a single mechanical change with a
  full-suite run behind it. Recorded here so task 8 can file it.

The distinct module name is load-bearing for the same reason it is in task 2's
file: this file shadows ``logger.warning`` on the module object to observe the
trim log, and its own module instance keeps that from reaching a sibling's
handler.

The ``metrics_facade`` stub keeps ``time_function`` a real pass-through for the
reason all three earlier files record: a bare ``MagicMock`` turns the
``@time_function`` decorator into a call-swallowing mock, so
``create_approval_request`` would never run and every assertion below would hold
vacuously. The guard at the end of ``_load_handler`` asserts that did not
happen.
"""

from __future__ import annotations

import contextlib
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

RUN_ID = "run-2026-08-08-14-47-44-db94df01"
PIPELINE_ID = "hellowrols-aabed79f"
TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAA" + "x" * 40

#: The trim constants, read off ``lambda_function.py:419-423``. Named rather
#: than inlined because the trim assertions below are about the *exact*
#: boundary, and the existing upper-bound test in
#: ``test_approval_handler_evidence.py`` is what happens when they are not.
TRIM_LIMIT = 50000
TRIM_SUFFIX = "...(truncated)"
#: Measured on unfixed code: 50000 + len("...(truncated)").
TRIMMED_LENGTH = 50014


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

    The handler decorates ``create_approval_request`` with
    ``@time_function(...)``. A bare ``MagicMock`` would replace it with a mock
    that swallows the call and returns a mock, so no approval row would ever be
    written and every assertion in this file would pass vacuously.
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
    """
    global _HANDLER
    if _HANDLER is not None:
        return _HANDLER

    stubs = {**_LAYER_STUBS, "metrics_facade": _metrics_stub()}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "approval_handler_serialization_preservation_lf",
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

    for name in ("create_approval_request", "get_approval_data"):
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
# Events — the inline test_failure path, where a Decimal-free payload is real
# ---------------------------------------------------------------------------
def _inline_test_failure_event(test_result: dict[str, Any]) -> dict[str, Any]:
    """A ``test_failure`` request carrying its report **inline**.

    The inline payload is the primary source for this branch
    (``lambda_function.py:293``); the state-row fallback at ``:294-298`` is
    only reached when the event carries no ``test_result``. Keeping the payload
    inline is what makes it ``Decimal``-free: it arrives as parsed Step
    Functions JSON and is stored as a JSON string, so it never crosses boto3's
    numeric serializer in either direction.
    """
    return {
        "approval_type": "test_failure",
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "task_token": TASK_TOKEN,
        "test_result": test_result,
    }


def _schema_mapping_event() -> dict[str, Any]:
    return {
        "approval_type": "schema_mapping",
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "task_token": TASK_TOKEN,
    }


def _seed_schema_mapping_row(env) -> None:
    """Write the ``schema_mapping`` row the way the producer actually writes it.

    ``schema_mapping/lambda_function.py:1927-1946``: the real approval payload
    goes to ``output_data.approval`` as ``convert_floats_to_decimal(...)``
    (``:1927``), and of the nine keys the approval branch reads, the one the
    producer also writes at the row's **top level** is ``generated_by``
    (``:1946``).

    The ``Decimal``s under ``output_data.approval`` are deliberate and their role
    changed with p50 C1. Before it, the branch never read them, so this row was
    proof that the branch ignored the producer's payload. After it, the branch
    reads exactly them, so the same row is now what makes the boundary conversion
    at ``lambda_function.py:551`` observable. Kept minimal on purpose rather than
    swapped for ``tests/fixtures/conftest.py``'s
    ``schema_mapping_decimal_state_row``: a three-key payload keeps the
    byte-exact assertion in case 4 readable, and seeding ``generated_by`` at the
    top level **only** exercises the second source of that key's two-source read,
    which the full fixture (which writes it in both places) cannot.
    """
    env.state.put_item(
        Item={
            "run_id": RUN_ID,
            "step_name": "schema_mapping",
            "step_sequence": 4,
            "status": "success",
            "mapping_id": "map-20260808144744",
            "source_schema_hash": "sha256:aaaa",
            "target_schema_hash": "sha256:bbbb",
            "mapping_count": 3,
            "unmapped_source_count": 1,
            "unmapped_target_count": 0,
            "original_pipeline_id": PIPELINE_ID,
            "pipeline_id": PIPELINE_ID,
            "approval_required": True,
            # The one top-level key of the nine that the producer writes.
            "generated_by": "bedrock_agent",
            "output_data": {
                "artifact_type": "schema_mapping",
                "primary_id": "map-20260808144744",
                "status": "success",
                "approval": {
                    "proposed_mappings": [
                        {"source_field": "cust_id", "target_field": "customer_id", "confidence": Decimal("0.91")}
                    ],
                    "confidence_scores": {"cust_id": Decimal("0.91")},
                    "mapping_statistics": {"mapped_fields": Decimal("3"), "avg_confidence": Decimal("0.91")},
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# The precondition — checked, not assumed
# ---------------------------------------------------------------------------
def _contains_decimal(obj: Any) -> bool:
    """``isBugCondition_serialization``'s ``contains_decimal``, verbatim in intent."""
    if isinstance(obj, Decimal):
        return True
    if isinstance(obj, dict):
        return any(_contains_decimal(value) for value in obj.values())
    if isinstance(obj, list):
        return any(_contains_decimal(item) for item in obj)
    return False


def _assert_decimal_free(payload: Any) -> None:
    """Property 2's precondition, asserted against the payload that will be serialized.

    Checked on ``get_approval_data``'s **output** rather than on the generated
    input, because that is where the trap is: a value seeded into a state row
    comes back as ``Decimal`` however it was written, so a payload that looked
    ``Decimal``-free going in can be ``Decimal``-bearing by the time it reaches
    ``json.dumps``. If this ever fires, the test has drifted into Property 1's
    domain and its assertions mean the opposite of what they say.
    """
    assert not _contains_decimal(payload), (
        f"Property 2's precondition is violated — the payload reaching json.dumps contains a "
        f"Decimal, so this is a bug-condition input, not a preservation input: {payload!r}"
    )


def _reference_string(env, approval_type: str, event: dict[str, Any]) -> str:
    """What ``json.dumps`` produces for this event, computed before the write.

    Deliberately computed *before* ``create_approval_request`` runs: that call
    updates the state row for the echoing approval types, and although the
    attributes it writes (``status``, ``updated_at``, ``approval_id``) are none
    of the ones any branch reads, ordering the read first removes the question
    entirely.
    """
    payload = env.handler.get_approval_data(approval_type, RUN_ID, event)
    _assert_decimal_free(payload)
    return json.dumps(payload)


def _stored_approval_data(env, approval_id: str) -> str:
    item = env.approvals.get_item(Key={"approval_id": approval_id}).get("Item")
    assert item is not None, f"no approval row was written for {approval_id!r}"
    return str(item["approval_data"])


@contextlib.contextmanager
def _recorded_warnings(module):
    """Record ``logger.warning`` on one module, then call through.

    Requirement 3.3 is partly a statement about a **log line**, so the log line
    has to be observed. Powertools' ``Logger`` does not propagate, so ``caplog``
    sees nothing; this shadows the method as an instance attribute and restores
    whatever was there before — including restoring *absence*, since the
    original is resolved through delegation rather than living in ``__dict__``.
    Same shape as ``_recorded_error_logs`` in
    ``tests/unit/lambdas/dashboard_api/test_field_context_roundtrip_prop.py``.
    """
    recorded: list[str] = []
    original = module.logger.warning
    was_own = "warning" in module.logger.__dict__

    def _spy(message, *args, **kwargs):
        try:
            recorded.append(str(message) % args if args else str(message))
        except (TypeError, ValueError):
            recorded.append(f"{message} {args}")
        return original(message, *args, **kwargs)

    module.logger.warning = _spy
    try:
        yield recorded
    finally:
        if was_own:
            module.logger.warning = original
        else:
            module.logger.__dict__.pop("warning", None)


# ---------------------------------------------------------------------------
# Case 1 — the property: a Decimal-free payload serializes byte-identically
# ---------------------------------------------------------------------------
#: Map keys. JSON object keys are strings; the alphabet is constrained so the
#: failures this property finds are about serialization rather than about
#: exotic key text.
_KEYS = st.text(alphabet="abcdefgHIJ_0123456789", min_size=1, max_size=8)

#: The five JSON scalar kinds requirement 3.1 names, all reachable on the inline
#: path because nothing here is written to DynamoDB as a numeric attribute.
#:
#: ``st.floats`` is left at its full default range on purpose, subnormals
#: included. Task 1's generator had to floor the magnitude at 1e-6 because
#: ``boto3.dynamodb.types.DYNAMODB_CONTEXT`` raises ``decimal.Underflow`` below
#: roughly 1e-130 — but that wall is on the *write* to a state row, and this
#: payload never crosses it. ``5e-324`` was measured serializing cleanly here.
#: ``nan``/``inf`` are excluded: ``json.dumps`` emits the non-standard
#: ``NaN``/``Infinity`` for them, which Step Functions JSON cannot carry, so
#: they are outside the input space this property is about.
_LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=12),
)

#: Nested dicts and lists, and — because ``max_size=0`` is reachable — the empty
#: containers requirement 3.1 calls out.
_STRUCTURES = st.recursive(
    _LEAVES,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_KEYS, children, max_size=4),
    ),
    max_leaves=10,
)

#: The producer's own check shape (``test_code/lambda_function.py:471-566``), so
#: the payload the branch derives ``failed_checks`` and ``test_status`` from is
#: a realistic one rather than an arbitrary blob.
_CHECKS = st.lists(
    st.fixed_dictionaries(
        {
            "name": st.sampled_from(["syntax_check", "transform_execution", "schema_compliance"]),
            "passed": st.booleans(),
            "detail": st.text(max_size=24),
        }
    ),
    max_size=3,
)


@st.composite
def decimal_free_test_results(draw) -> dict[str, Any]:
    """An inline ``test_result`` guaranteed to be ``Decimal``-free.

    The three keys the branch reads — ``status``, ``checks``, ``report_s3_uri``
    — are always supplied and always win the merge, so an arbitrary draw can
    never turn ``checks`` into something the branch's list comprehension chokes
    on. That matters: ``get_approval_data`` swallows its own exceptions and
    returns ``{}``, which would leave this property comparing two empty strings
    and passing for the wrong reason. The non-vacuity guard in the test asserts
    the drawn structure actually reached the payload.
    """
    extras = draw(st.dictionaries(_KEYS, _STRUCTURES, max_size=4))
    return {
        **extras,
        "status": draw(st.sampled_from(["passed", "failed"])),
        "checks": draw(_CHECKS),
        "report_s3_uri": "s3://agentic-data-ingestion-test-outputs/run/test_report.md",
    }


@example(
    # The float artifacts measured on unfixed code, byte for byte. This is the
    # example that fails against a fix which normalises or re-formats numbers.
    test_result={
        "status": "failed",
        "checks": [],
        "report_s3_uri": "s3://b/k",
        "ratio": 0.30000000000000004,
        "big": 1e22,
        "subnormal": 5e-324,
        "negative_zero": -0.0,
    }
)
@example(
    # Empty containers, at the top level and nested.
    test_result={"status": "passed", "checks": [], "report_s3_uri": "s3://b/k", "empties": {"m": {}, "l": [[], {}]}}
)
@example(
    # ensure_ascii escaping and the string escapes, which are part of the
    # observed byte sequence and would change under `ensure_ascii=False`.
    test_result={
        "status": "passed",
        "checks": [],
        "report_s3_uri": "s3://b/k",
        "unicode": "caf\u00e9 \u4e2d\u6587 \U0001f600",
        "quoted": 'he said "hi"\n\t',
    }
)
@given(test_result=decimal_free_test_results())
@settings(
    max_examples=100,
    # Each example writes an approval row through moto, which routinely exceeds
    # Hypothesis's 200 ms default. The property is about bytes, not latency.
    deadline=None,
    # One fixture serves every example by design. `approval_id` is
    # `{pipeline_id}_{approval_type}_{int(time.time())}`, so examples inside the
    # same second reuse an id — harmless, because `put_item` replaces the whole
    # item and each example reads back the row it just wrote by that id.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` excluded: it re-runs hundreds of further examples to
    # annotate a failure whose cause the assertion message already states.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_a_decimal_free_payload_is_serialized_byte_identically(test_result, approval_env):
    """Property 2 over arbitrary ``Decimal``-free payload structures.

    For any inline ``test_result`` built from ``str``/``int``/``float``/``bool``/
    ``None``/nested ``dict``/``list``/empty containers, the stored
    ``approval_data`` is byte-for-byte ``json.dumps`` of the payload
    ``get_approval_data`` returned. Nothing may be normalised, re-ordered,
    re-escaped, or re-typed.

    This is the assertion that catches an over-broad fix. ``json.dumps`` with no
    keyword arguments is a very specific byte sequence: insertion key order,
    ``", "``/``": "`` separators, ``ensure_ascii`` escapes, ``repr``-shortest
    floats, and ``int`` printed without a decimal point.

    **Validates: Requirements 3.1**
    """
    env = approval_env
    event = _inline_test_failure_event(test_result)

    payload = env.handler.get_approval_data("test_failure", RUN_ID, event)
    _assert_decimal_free(payload)
    expected = json.dumps(payload)

    # Non-vacuity: the branch stores the very object the event carried
    # (`lambda_function.py:302`), so identity proves the drawn structure reached
    # the payload rather than `get_approval_data` having swallowed an error and
    # returned {}.
    assert payload["test_result"] is test_result, (
        f"the drawn payload did not reach approval_data — get_approval_data returned {payload!r}"
    )

    approval_id = env.handler.create_approval_request(event)
    stored = _stored_approval_data(env, approval_id)

    assert stored == expected, (
        "requirement 3.1: the serialized approval_data is not byte-identical to "
        "json.dumps(get_approval_data(...)).\n"
        f"expected: {expected!r}\n"
        f"     got: {stored!r}"
    )


def test_an_int_in_a_decimal_free_payload_is_not_widened_to_a_float(approval_env):
    """``335`` stays ``335`` — the most likely over-broad-fix failure.

    Task 6.1 converts ``Decimal`` to ``float`` at the boundary, and the accepted
    consequence is that a ``Decimal("335")`` read off a state row serializes as
    ``335.0`` (``design.md`` §4.1). This test draws the line: an ``int`` that
    never went through DynamoDB is not a ``Decimal`` and must not be widened. A
    fix spelled as "convert every number to float", or as
    ``json.dumps(..., default=float)`` combined with a walk over ``int``, passes
    Property 1 and fails here.

    Measured on unfixed code: ``"an_int": 335`` serializes as ``335``.

    **Validates: Requirements 3.1**
    """
    env = approval_env
    event = _inline_test_failure_event(
        {
            "status": "failed",
            "checks": [{"name": "syntax_check", "passed": False, "detail": "compiles"}],
            "report_s3_uri": "s3://b/k",
            "an_int": 335,
            "zero": 0,
            "negative": -18,
            "a_float": 1.5,
        }
    )
    expected = _reference_string(env, "test_failure", event)

    approval_id = env.handler.create_approval_request(event)
    stored = _stored_approval_data(env, approval_id)

    assert stored == expected
    for fragment in ('"an_int": 335,', '"zero": 0,', '"negative": -18,', '"a_float": 1.5'):
        assert fragment in stored, f"expected {fragment!r} in the serialized payload, got {stored!r}"
    for widened in ("335.0", "0.0", "-18.0"):
        assert widened not in stored, (
            f"requirement 3.1: an int in a Decimal-free payload was widened — found {widened!r} in {stored!r}"
        )


def test_a_bool_stays_a_json_bool_and_never_widens_to_a_number(approval_env):
    """``True`` serializes as ``true``, never as ``1`` and never as ``1.0``.

    Separate and explicit because ``bool`` subclasses ``int`` and
    ``shared/decimal_utils.py``'s docstring records that it is deliberately NOT
    converted — an ``isinstance`` chain that tests numbers before ``bool`` would
    silently widen it. Task 1's oracle already checks the *kind* of the parsed
    value; the concern here is the serialized **string**, which is what every
    dashboard consumer and every existing payload assertion actually reads.

    The ``1`` and ``0`` alongside are the discriminator: if a fix collapsed
    ``bool`` into the numeric branch, ``"flag"`` and ``"one"`` would serialize
    identically.

    **Validates: Requirements 3.1**
    """
    env = approval_env
    event = _inline_test_failure_event(
        {
            "status": "failed",
            # `passed` on a check is the bool the branch itself reads.
            "checks": [
                {"name": "syntax_check", "passed": True, "detail": "compiles"},
                {"name": "transform_execution", "passed": False, "detail": "to_date raised"},
            ],
            "report_s3_uri": "s3://b/k",
            "flag": True,
            "off": False,
            "one": 1,
            "nought": 0,
            "nested": [True, False, {"deep": True}],
        }
    )
    expected = _reference_string(env, "test_failure", event)

    approval_id = env.handler.create_approval_request(event)
    stored = _stored_approval_data(env, approval_id)

    assert stored == expected
    for fragment in ('"flag": true', '"off": false', '"passed": true', '"passed": false', '"nested": [true, false, '):
        assert fragment in stored, f"expected {fragment!r} in the serialized payload, got {stored!r}"
    for widened in ('"flag": 1', '"flag": 1.0', '"off": 0', '"off": 0.0', '"passed": 1', '"passed": 1.0'):
        assert widened not in stored, (
            f"requirement 3.1: a bool was widened to a number — found {widened!r} in {stored!r}. "
            f"bool subclasses int; shared/decimal_utils.py's docstring records that it is "
            f"deliberately NOT converted"
        )

    parsed = json.loads(stored)["test_result"]
    assert parsed["flag"] is True and parsed["off"] is False
    assert parsed["one"] == 1 and not isinstance(parsed["one"], bool)


# ---------------------------------------------------------------------------
# Case 3 — the 50000-character trim, both sides of the boundary
# ---------------------------------------------------------------------------
def test_an_oversized_payload_is_trimmed_at_exactly_50000_characters(approval_env):
    """The trim, pinned at the exact boundary rather than at an upper bound.

    Measured on unfixed code: a 60218-character payload was stored as **50014**
    characters — ``untrimmed[:50000] + "...(truncated)"`` exactly — and logged
    one warning, ``"Trimmed approval_data from 60218 to 50000 chars"``.

    ``lambda_function.py:419-423`` reads the string back out of
    ``approval_record["approval_data"]``, so it sees whatever ``json.dumps``
    produced. Task 6.1 changes that string's *content* for ``Decimal``-bearing
    payloads but not its type, and the trim is purely ``len()``-based — so for a
    ``Decimal``-free payload the whole computation is unchanged, which is what
    this asserts.

    ``test_approval_handler_evidence.py:216-266`` already exercises the trim but
    asserts only ``len(...) <= 50014``, a bound a fix trimming at 10 characters
    would satisfy. This is the sharpening, not a duplicate.

    **Validates: Requirements 3.3**
    """
    env = approval_env
    event = _inline_test_failure_event(
        {
            "status": "failed",
            "checks": [{"name": "schema_compliance", "passed": False, "detail": "x" * 100}],
            "report_s3_uri": "s3://b/k",
            "blob": "x" * 60000,
        }
    )
    untrimmed = _reference_string(env, "test_failure", event)
    assert len(untrimmed) > TRIM_LIMIT, "the payload must exceed the limit or this test proves nothing"

    with _recorded_warnings(env.handler) as warnings:
        approval_id = env.handler.create_approval_request(event)
    stored = _stored_approval_data(env, approval_id)

    assert stored == untrimmed[:TRIM_LIMIT] + TRIM_SUFFIX, (
        f"requirement 3.3: the trim is not the observed one. Expected the first {TRIM_LIMIT} "
        f"characters plus {TRIM_SUFFIX!r}; got a {len(stored)}-character string ending "
        f"{stored[-20:]!r}"
    )
    assert len(stored) == TRIMMED_LENGTH == TRIM_LIMIT + len(TRIM_SUFFIX)
    assert stored.endswith(TRIM_SUFFIX)

    assert warnings == [f"Trimmed approval_data from {len(untrimmed)} to {TRIM_LIMIT} chars"], (
        f"requirement 3.3: the trim warning changed. On unfixed code this call logs exactly one "
        f"warning, naming the pre-trim length and the limit. Got {warnings!r}"
    )


def test_a_payload_under_the_limit_is_neither_trimmed_nor_warned_about(approval_env):
    """The other side of the boundary: the trim does not fire early.

    Without this, a fix that trimmed everything would satisfy the assertions
    above only by accident of the oversized fixture. ``:420`` is a strict
    ``>``, so a payload at or below the limit is stored whole and logs nothing.

    **Validates: Requirements 3.3**
    """
    env = approval_env
    event = _inline_test_failure_event(
        {
            "status": "failed",
            "checks": [{"name": "schema_compliance", "passed": False, "detail": "y" * 100}],
            "report_s3_uri": "s3://b/k",
            "blob": "y" * 1000,
        }
    )
    untrimmed = _reference_string(env, "test_failure", event)
    assert len(untrimmed) <= TRIM_LIMIT

    with _recorded_warnings(env.handler) as warnings:
        approval_id = env.handler.create_approval_request(event)
    stored = _stored_approval_data(env, approval_id)

    assert stored == untrimmed
    assert TRIM_SUFFIX not in stored
    assert warnings == [], f"a payload under the limit must log no warning, got {warnings!r}"


# ---------------------------------------------------------------------------
# Case 4 — the schema_mapping payload shape (requirement 3.2, REVERSED by p50)
# ---------------------------------------------------------------------------
#: The eleven keys the ``schema_mapping`` branch emits, in the order it writes
#: them — the order is part of the serialized bytes. p50 C1 changed the addresses
#: nine of them are read from without changing the list; p50 C2 then APPENDED a
#: tenth, ``mapping_count``, which is derived from ``proposed_mappings`` and read
#: from no address at all; p55 R10.1 APPENDED an eleventh, ``rename_manifest``,
#: read from ``output_data.approval.rename_manifest``. Appended rather than
#: inserted so no existing position moves, and the list is EXTENDED rather than
#: the assertion below relaxed — order is pinned precisely because it is part of
#: the bytes.
#:
#: This is a **second transcription** of ``tests/fixtures/conftest.py``'s
#: ``SCHEMA_MAPPING_APPROVAL_KEYS``, and it is left as one rather than replaced by
#: an import: this module's whole subject is the serialized bytes, so a local list
#: that has to be updated by hand is the thing that fails when the producer's key
#: order changes under it. Two literals that must agree, each asserted against the
#: producer, is the shape this repository already chose for
#: ``_SOURCE_FORMAT_BY_EXTENSION``.
SCHEMA_MAPPING_KEYS = [
    "source_schema",
    "target_schema",
    "proposed_mappings",
    "confidence_scores",
    "unmapped_fields",
    "mapping_summary",
    "evidence",
    "questions",
    "generated_by",
    "mapping_count",
    "rename_manifest",
]


def test_the_schema_mapping_payload_now_carries_the_producers_payload(approval_env):
    """DELIBERATE REVERSAL — was ``test_the_schema_mapping_payload_shape_is_unchanged``.

    p34a requirement 3.2 held this branch's read gap **pinned rather than
    fixed**: nine keys, eight ``None``s, and the exact 237-character serialized
    string. Its own docstring said requirement 3.2 existed "so this bugfix cannot
    change it by accident, in either direction — not by correcting a read". p50
    C1 corrects the read **on purpose**, so the hold is over and the assertion
    that encoded it is amended here rather than deleted, per ``LGR-60c4985-6``
    and ``LGR-9e76249-10`` — this repo's convention for keeping a deliberate
    reversal legible instead of letting it read as a regression. The old name is
    recorded above rather than kept, because a test called ``_is_unchanged`` that
    asserts a change is worse than a renamed one; ``LGR-60c4985-6`` did the same
    thing when ``test_failing_run_reaches_approval_once_max_attempts_reached``
    replaced ``test_no_code_regeneration_string_remains_in_lambda_source``.

    What p50 verified about the old shape, and what it changes: seven of the nine
    keys are built by ``create_approval_payload``
    (``schema_mapping/lambda_function.py:1732-1780``) and stored whole at
    ``output_data.approval`` (``:1927``), so the branch now reads them there.
    ``evidence`` and ``questions`` are **not** in that payload — a different
    writer, the complex-mapping agent's ``update_item`` at ``:2277-2291``, SETs
    them at the row's top level — so those two keep their top-level read, and the
    row seeded here writes neither. That is why two ``None``s remain and eight
    did not become zero.

    **Property 2's ``Decimal``-free precondition no longer holds for this
    branch, by design.** ``_reference_string`` is deliberately not used below:
    the ``Decimal``s in ``output_data.approval`` used to be unreachable, which is
    exactly what ``design.md`` §4.1 meant by "one correct read away from crashing
    identically". They are reachable now, so the boundary conversion at
    ``lambda_function.py:551`` is doing real work here for the first time, and
    the assertion on the stored bytes is what proves it. The rest of this file —
    Property 2 proper, over the inline ``test_failure`` path — is untouched and
    still passes: no ``Decimal`` reaches ``json.dumps`` there.

    **Validates: Requirements 3.2 (as reversed by p50 R1.2, R1.6)**
    """
    env = approval_env
    _seed_schema_mapping_row(env)
    event = _schema_mapping_event()

    payload = env.handler.get_approval_data("schema_mapping", RUN_ID, event)

    assert list(payload) == SCHEMA_MAPPING_KEYS, (
        f"the schema_mapping payload's key set or order changed. p50 C1 moved seven addresses "
        f"and must not have touched the key list.\n"
        f"expected: {SCHEMA_MAPPING_KEYS}\n     got: {list(payload)}"
    )
    # This row is minimal by design (see ``_seed_schema_mapping_row``): its
    # payload carries only ``proposed_mappings`` and ``confidence_scores``, and
    # it writes neither ``evidence`` nor ``questions``. So six of the nine READ
    # keys are legitimately absent HERE — what changed is that two of them resolve
    # at all, where before p50 C1 the count was eight. The tenth key,
    # ``mapping_count``, is derived and is NOT in this list: ``proposed_mappings``
    # is a readable list here, so the count is established (1) rather than absent —
    # which is the R1.5 distinction the derivation exists to keep. The fully
    # populated case is ``test_schema_mapping_payload_read.py``, against the live
    # run's shape.
    #
    # AMENDED by p55 task 6.2, not loosened: ``rename_manifest`` joins the null
    # list, taking the count from six to seven. This row's payload does not carry
    # it — nothing written before p55 does — and the projection reads it with
    # ``.get``, so it arrives present-and-``None``. That is the state the frontend
    # reader has to tolerate (R10.1's key is absent from every pre-p55 mapping row
    # and from ``create_approval_payload``'s degraded arm), so having it appear
    # here, in the *minimal*-row test, is the assertion earning its keep rather
    # than churn.
    nulls = sorted(key for key, value in payload.items() if value is None)
    assert nulls == [
        "evidence",
        "mapping_summary",
        "questions",
        "rename_manifest",
        "source_schema",
        "target_schema",
        "unmapped_fields",
    ], (
        f"p50 R1.2: this row's payload seeds two of the eight output_data.approval keys and none of "
        f"the two top-level ones, so exactly seven read as absent. Eight was the pre-C1 count, six "
        f"the pre-p55 one. Got nulls={nulls}, payload={payload!r}"
    )
    assert payload["proposed_mappings"] == [
        {"source_field": "cust_id", "target_field": "customer_id", "confidence": Decimal("0.91")}
    ], (
        "the producer's own payload, read from output_data.approval — this is the value an "
        "approver was previously shown as null"
    )
    assert payload["generated_by"] == "bedrock_agent", (
        "generated_by resolves in both places (schema_mapping:1760 and :1946); this row writes it "
        "only at the top level, so the second source in the ``or`` chain is what answers here"
    )

    # p50 C2 — and this row is where the derive-vs-read decision is visible.
    # ``_seed_schema_mapping_row`` writes a top-level ``mapping_count`` of 3 while
    # its payload carries ONE proposed mapping, so the two authorities disagree by
    # construction here. The derived value describes the list an approver is
    # actually shown; the stored one would have said 3 (and, being a DynamoDB
    # number, would have arrived as ``3.0`` through the boundary conversion).
    assert payload["mapping_count"] == 1, (
        "mapping_count is len(proposed_mappings), NOT the row's top-level mapping_count — "
        "this row's stored value is 3 against one proposed mapping, so a 3 here means the "
        "branch consumed the second authority LGR-28b4d75-1 / LGR-9e76249-2 reject"
    )
    assert isinstance(payload["mapping_count"], int) and not isinstance(payload["mapping_count"], bool), (
        f"a derived count is a plain int, so the boundary conversion at :551 leaves it alone; "
        f"got {type(payload['mapping_count']).__name__}"
    )

    approval_id = env.handler.create_approval_request(event)
    stored = _stored_approval_data(env, approval_id)

    # AMENDED by p55 task 6.2, with the reason stated here rather than the literal
    # being relaxed to a substring or a parsed comparison: `"rename_manifest":
    # null` is appended to the bytes because R10.1's key is projected on every
    # path and this minimal row carries no manifest. The trailing position is the
    # point — an appended key cannot move any byte before it, which is what makes
    # this whole-string comparison worth keeping over an `in` test.
    assert stored == (
        '{"source_schema": null, "target_schema": null, "proposed_mappings": '
        '[{"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.91}], '
        '"confidence_scores": {"cust_id": 0.91}, "unmapped_fields": null, "mapping_summary": null, '
        '"evidence": null, "questions": null, "generated_by": "bedrock_agent", "mapping_count": 1, '
        '"rename_manifest": null}'
    ), (
        f"the serialized schema_mapping payload changed. Before p50 this was the 237-character "
        f"all-null string; the Decimals are floats here because of the boundary conversion at "
        f"lambda_function.py:551 (LGR-86698d4-1), while p50 C2's derived mapping_count is a plain "
        f"int and so serializes as 1 rather than 1.0, and p55 R10.1's rename_manifest is appended "
        f"as null on a row written before it existed. Got {stored!r}"
    )
    assert not _contains_decimal(json.loads(stored)), (
        "the stored string must parse back Decimal-free — that is the boundary conversion having run"
    )
