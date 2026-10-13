# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A reused mapping's count never disagrees with the mappings it is shown beside.

# Feature: p54-reused-and-skipped-run-honesty, task 2.3

THE PROPERTY
------------
*For any* resolved origin payload, the mapping count the reuse payload carries
equals ``len(proposed_mappings)`` where that value is a list and is ``None``
otherwise. **The two are never both present and unequal** — there is no input for
which the approver is shown a mapping list of one length beside a confident count
of another, and none for which a number is stated over a list that is not a list.

WHAT THIS ADDS OVER THE TWO TESTS THAT ALREADY TOUCH ``mapping_count``
--------------------------------------------------------------------
Stated plainly, because a near-duplicate property is worse than no property: it
costs a run every checkpoint and buys a second spelling of an existing guard.

* ``test_mapping_count_three_state_prop.py`` (p50, Property 2 / CP2) is a
  generative sweep over the **non-reuse** arm — ``get_approval_data`` with the
  run's OWN ``schema_mapping`` row, over a ``MagicMock`` table, with no event. It
  exhausts the three-state derivation itself, and this file does **not** re-assert
  any of that: not the empty-list-is-zero half, not the degraded-container half,
  not the stored-count decoys.
* ``test_reused_mapping_payload_read.py``'s
  ``test_the_reuse_payload_count_agrees_with_the_mappings_it_carries`` (task 2.2)
  is the resolved reuse case at **one** length — the fixture's four mappings.

What neither can state is the **reuse arm's boundary law**, and that is this file's
whole content. ``_reused_mapping_payload`` does not simply return what
``_schema_mapping_payload`` gives it: there is a post-projection guard at
``lambda_function.py:481`` that re-routes a projection whose ``proposed_mappings``
came back ``None`` into the *unreadable* statement instead. So the reuse arm has
exactly **two** outcomes over the origin payload's whole shape space, and the count
agrees with the list on both — on the resolved one because it is ``len()`` of it,
on the unreadable one because both are ``None``.

That guard is what makes the resolved state's shape space strictly **narrower**
than the non-reuse state's, and the asymmetry is real and otherwise unasserted:
feed ``proposed_mappings: None`` to the non-reuse arm and you get a ten-key payload
carrying ``proposed_mappings: None`` beside ``mapping_count: None``; feed the same
origin row to the reuse arm and you get five keys and ``mapping_unreadable: True``.
One derivation, two arms, two different answers for one input — which is correct
(do-NOT 7: a named reference to something absent is not a mapping with no fields,
``LGR-b2f79ef-14``) and which no existing assertion pins generatively.

Two further dimensions come free from the same generator and are asserted rather
than given their own property, on the ``Property 8``-shaped reasoning design's
Notes give for folding R9.4 and R10.5 together — three properties over one
generator would be three spellings of one guard:

* the three keys that **append** on the resolved path (``reused_mapping``,
  ``reused_mapping_id``, ``reused_from_run_id``) are written *after* the count is
  computed (``:491-493``), and must not perturb the pair. The key-order tuples in
  ``tests/fixtures/conftest.py`` pin which keys are present, not the count's
  relationship to the list beside it.
* the count's **type** across every length. 2.2 asserts a plain ``int`` at length
  four; a ``Decimal`` leaking in at some other length would serialize as ``4.0``
  and inherit ``LGR-86698d4-1``'s accepted int-to-float widening for no reason.

WHY moto AND NOT A MagicMock, WHICH IS THE OPPOSITE OF p50's CHOICE NEXT DOOR
----------------------------------------------------------------------------
p50's sibling uses a ``MagicMock`` state table deliberately, and says so: its
adversarial half needs values DynamoDB would refuse to store, and a table that
cannot hold the input cannot test the guard against it.

This property is the reverse case. Its subject is not the derivation in isolation
but **what survives three chased reads** — mappings item, origin run id, origin
state row — so the list the count is compared against must be one boto3's resource
deserializer actually returned. Every shape this file generates was checked against
moto first and all of them round-trip faithfully, including ``[]``, ``{}``, ``''``
and ``False``, so nothing is lost by using the real table here.

FIXTURES, REGION, MOCKING
-------------------------
Shared fixtures only — ``mock_dynamodb_tables``, ``mock_lambda_env`` and the
p50/p54 row fixtures from ``tests/fixtures/conftest.py``. No ``moto.mock_aws()``
call and no boto3 client or resource mock
(``tests/unit/test_no_duplicate_mocking_prop.py``), and ``AWS_REGION`` is not set
here — the unit layer pins it in ``tests/unit/conftest.py`` (``LGR-3e45e65-1``,
template / ratified).

``_reset`` per example is not boilerplate: it is 2.2's own counterexample.
``approval_env`` is function-scoped and Hypothesis runs every example inside one
invocation, so without it the two moto tables accumulate rows across examples and
a later example's "absent" is a previous example's leftover. 2.2's Property 2
falsified on ``mappings_item_absent`` and then failed to reproduce on replay for
exactly that reason.

ON THE MODULE-LOADING PREAMBLE — DUPLICATION, DECLARED
-----------------------------------------------------
``_load_handler`` / ``_metrics_stub`` / ``approval_env`` are the seventh copy of
this preamble in this directory. ``test_serialization_preservation_prop.py``
records the reasoning and files the extraction as follow-up work to be done as one
mechanical change with a full-suite run behind it; that has not happened, so this
file copies rather than extracts. The distinct module name is load-bearing — each
copy execs the handler under its own name so a sibling's rebound tables cannot
reach this one's.

**Validates: Requirements 1.1, 1.2**
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import os
import sys
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.fixtures.conftest import (
    SCHEMA_MAPPING_APPROVAL_KEYS_REUSED,
    SCHEMA_MAPPING_APPROVAL_KEYS_UNREADABLE,
    SCHEMA_MAPPING_DECIMAL_RUN_ID,
    SCHEMA_MAPPING_REUSE_RUN_ID,
    SCHEMA_MAPPING_REUSED_MAPPING_ID,
)

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
MAPPINGS_TABLE = "agentic_data_ingestion_schema_mappings"

#: The sentinel the whole property is about. Named so a failure message can say
#: which of the two answers appeared rather than only that something did.
NOT_ESTABLISHED = None


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
    """A ``metrics_facade`` whose ``time_function`` is a real pass-through."""
    stub = MagicMock()
    stub.time_function = lambda *_a, **_k: lambda f: f
    stub.record_error = MagicMock()
    stub.ApprovalMetrics = MagicMock()
    stub.PipelineMetrics = None
    return stub


def _load_handler():
    """Exec the handler module once, under a name unique to this file."""
    global _HANDLER
    if _HANDLER is not None:
        return _HANDLER

    stubs = {**_LAYER_STUBS, "metrics_facade": _metrics_stub()}
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            "approval_handler_reused_mapping_count_agreement_lf",
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

    for name in ("get_approval_data", "_reused_mapping_payload", "_schema_mapping_payload"):
        assert inspect.isfunction(getattr(module, name)), (
            f"{name} is not a plain function — a decorator was replaced by a mock, which would make "
            f"the assertions in this file vacuous"
        )
    _HANDLER = module
    return _HANDLER


@pytest.fixture
def approval_env(mock_lambda_env, mock_dynamodb_tables):
    """The handler bound to the shared fixture's moto tables."""
    handler = _load_handler()

    region = os.environ.get("AWS_REGION", "us-west-2")
    handler.dynamodb = boto3.resource("dynamodb", region_name=region)
    handler.approvals_table = mock_dynamodb_tables[APPROVALS_TABLE]
    handler.state_table = mock_dynamodb_tables[STATE_TABLE]
    handler.task_tokens_table = mock_dynamodb_tables[TASK_TOKENS_TABLE]
    handler.mappings_table = mock_dynamodb_tables[MAPPINGS_TABLE]

    return SimpleNamespace(
        handler=handler,
        state=mock_dynamodb_tables[STATE_TABLE],
        mappings=mock_dynamodb_tables[MAPPINGS_TABLE],
    )


def _reset(env) -> None:
    """Return the two moto tables to a clean slate between Hypothesis examples.

    2.2's counterexample, restated: ``approval_env`` is function-scoped and every
    example runs inside one invocation, so rows accumulate and a later example's
    "absent" becomes an earlier example's leftover. Its Property 2 falsified on
    ``mappings_item_absent`` and then failed to reproduce on replay for that
    reason, which is Hypothesis's "produces unreliable results" warning.
    """
    for table, key_names in ((env.state, ("run_id", "step_name")), (env.mappings, ("mapping_id",))):
        for item in table.scan().get("Items", []):
            table.delete_item(Key={name: item[name] for name in key_names})


# ---------------------------------------------------------------------------
# Generators — the origin payload's ``proposed_mappings``, as DynamoDB can hold it
# ---------------------------------------------------------------------------
def _mapping_entries(min_size: int = 0, max_size: int = 8):
    """``proposed_mappings`` as ``create_approval_payload`` emits it.

    Entry content is drawn from small pools rather than free text: the count is a
    function of list LENGTH alone, so content is irrelevant to the property and
    unconstrained text would only slow the run down. Confidences are real
    ``Decimal``s because that is what the row is written through and what boto3
    hands back.
    """
    entry = st.fixed_dictionaries(
        {
            "source_field": st.sampled_from(["cust_id", "full_name", "email_addr", "signup_date"]),
            "target_field": st.sampled_from(["customer_id", "customer_name", "email", "registration_date"]),
            "transformation": st.sampled_from(["direct", "string conversion", "to_date(signup_date, 'MM/dd/yyyy')"]),
            "confidence": st.sampled_from([Decimal("0.91"), Decimal("0.94"), Decimal("0.96"), Decimal("0.99")]),
        }
    )
    return st.lists(entry, min_size=min_size, max_size=max_size)


#: Everything an origin payload's ``proposed_mappings`` can be that is NOT a list,
#: restricted to shapes DynamoDB actually stores and returns unchanged — every one
#: below was round-tripped through moto before being put here, because a value the
#: table silently refused would make its example vacuous rather than adversarial.
#:
#: ``"__absent__"`` is the key being omitted entirely, which is a different input
#: from a stored ``None`` even though the projection answers the same for both.
_NON_LIST_MAPPINGS = st.sampled_from(
    [
        "__absent__",
        None,
        "",
        "not-a-list",
        "[]",
        {},
        {"cust_id": "customer_id"},
        Decimal("0"),
        Decimal("4"),
        True,
        False,
    ]
)

#: Non-blank mapping ids. The count must not vary with the id that resolved it.
_MAPPING_IDS = st.one_of(
    st.just(SCHEMA_MAPPING_REUSED_MAPPING_ID),
    st.text(
        alphabet=st.characters(min_codepoint=48, max_codepoint=122, blacklist_categories=("Cs",)),
        min_size=1,
        max_size=40,
    ).filter(lambda s: s.strip() != ""),
)


def _seed_origin(env, origin_row: dict, mappings_item: dict, proposed_mappings: Any) -> None:
    """Seed the Reuse_Run trio with one origin ``proposed_mappings`` value.

    The Reuse_Run itself gets NO ``schema_mapping`` row — a Pass writes nothing,
    and that absence is the condition the reuse arm exists for.
    """
    row = copy.deepcopy(origin_row)
    approval = row["output_data"]["approval"]
    if proposed_mappings == "__absent__":
        approval.pop("proposed_mappings", None)
    else:
        approval["proposed_mappings"] = proposed_mappings

    env.state.put_item(Item=row)
    env.mappings.put_item(Item=mappings_item)


def _read(env, mapping_id: Any) -> dict[str, Any]:
    """``get_approval_data`` for the Reuse_Run, with 2.1's ``mapping_id`` on the event."""
    return env.handler.get_approval_data(
        "schema_mapping",
        SCHEMA_MAPPING_REUSE_RUN_ID,
        {
            "approval_type": "schema_mapping",
            "run_id": SCHEMA_MAPPING_REUSE_RUN_ID,
            "mapping_id": mapping_id,
        },
    )


# ---------------------------------------------------------------------------
# The assertion the whole property reduces to
# ---------------------------------------------------------------------------
def _assert_count_agrees(payload: dict[str, Any], *, context: str) -> None:
    """The count and the list it is shown beside are never both present and unequal.

    Three ways they could disagree, and all three are excluded here rather than
    inferred from one another:

    1. a stated number over a list of a different length — the count the Mapping
       Preview contradicts;
    2. a stated number over something that is not a list at all — what a bare
       ``len()`` would produce for a ``str`` or a ``dict`` (``LGR-9a35b2d-2``
       measured that failure's cost on the sibling listing surface);
    3. not-established over a readable list — the mirror image, refusing to state
       a count the producer did state, which is what a truthiness test rather than
       an ``isinstance`` test would do to ``[]``.
    """
    proposed = payload.get("proposed_mappings", "__key-absent__")
    count = payload.get("mapping_count", "__key-absent__")

    assert proposed != "__key-absent__", (
        f"[{context}] the reuse payload must always declare proposed_mappings — both states carry the "
        f"key, present-and-None on the unreadable one, so the display reader's null branch is reached "
        f"by a value rather than by an absence. Got: {payload!r}"
    )
    assert count != "__key-absent__", (
        f"[{context}] the reuse payload must always declare mapping_count for the same reason. Got: {payload!r}"
    )

    if isinstance(proposed, list):
        assert count == len(proposed), (
            f"[{context}] the count disagrees with the mappings beside it: mapping_count={count!r} "
            f"against {len(proposed)} proposed mappings. The approver would read a confident number "
            f"over a list that contradicts it — the second-authority failure LGR-28b4d75-1 and "
            f"LGR-9e76249-2 reject by requiring the count be derived from what is displayed"
        )
        assert count is not NOT_ESTABLISHED, (
            f"[{context}] a readable list always establishes a count, the empty one included — "
            f"refusing to state a zero the producer stated is the mirror image of do-NOT 7"
        )
        assert isinstance(count, int) and not isinstance(count, bool), (
            f"[{context}] a derived count is a plain int, so the serialization boundary leaves it "
            f"alone and it crosses the wire as 4 rather than 4.0 (LGR-86698d4-1); got "
            f"{type(count).__name__} {count!r}"
        )
    else:
        assert count is NOT_ESTABLISHED, (
            f"[{context}] proposed_mappings={proposed!r} is not a list, so no count is established; "
            f"got {count!r}. A number here means len() answered over a str, a dict or a bool and its "
            f"character or key count was presented to the approver as a mapping count"
        )
        assert count != 0 and count is not False, (
            f"[{context}] 0, False and Decimal('0') all compare equal, so the identity check above is "
            f"not enough on its own to say WHICH of them slipped through; got {count!r}"
        )


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------
@example(proposed_mappings=[], mapping_id=SCHEMA_MAPPING_REUSED_MAPPING_ID)
@example(proposed_mappings="__absent__", mapping_id=SCHEMA_MAPPING_REUSED_MAPPING_ID)
@example(proposed_mappings=None, mapping_id=SCHEMA_MAPPING_REUSED_MAPPING_ID)
@example(proposed_mappings="not-a-list", mapping_id=SCHEMA_MAPPING_REUSED_MAPPING_ID)
@example(proposed_mappings=Decimal("4"), mapping_id=SCHEMA_MAPPING_REUSED_MAPPING_ID)
@example(proposed_mappings=False, mapping_id=SCHEMA_MAPPING_REUSED_MAPPING_ID)
@given(
    proposed_mappings=st.one_of(_mapping_entries(), _NON_LIST_MAPPINGS),
    mapping_id=_MAPPING_IDS,
)
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_a_reused_mappings_count_never_disagrees_with_the_mappings_beside_it(
    approval_env,
    schema_mapping_decimal_state_row,
    schema_mapping_reused_mappings_item,
    proposed_mappings,
    mapping_id,
):
    """Property 1: A reused mapping's count never disagrees with the mappings it is shown beside.

    # Feature: p54-reused-and-skipped-run-honesty, Property 1: A reused mapping's
    # count never disagrees with the mappings it is shown beside

    *For any* resolved origin payload — every list length the producer can emit,
    and every non-list shape DynamoDB can hold in that position — the count the
    reuse payload carries equals ``len(proposed_mappings)`` where that value is a
    list and is ``None`` otherwise. The two are never both present and unequal.

    The reuse arm reaches this through **three chased reads**, so the list the
    count is compared against is one boto3's deserializer returned rather than one
    the test handed to a mock. That is the difference between this and the
    non-reuse sweep next door, and it is why the whole trio is seeded per example.

    **Validates: Requirements 1.1, 1.2**
    """
    env = approval_env
    _reset(env)
    mappings_item = {**schema_mapping_reused_mappings_item, "mapping_id": mapping_id}
    _seed_origin(env, schema_mapping_decimal_state_row, mappings_item, proposed_mappings)

    payload = _read(env, mapping_id)

    _assert_count_agrees(payload, context=f"origin proposed_mappings={proposed_mappings!r}")


# ---------------------------------------------------------------------------
# The boundary the reuse arm adds, and the reason it has exactly two outcomes
# ---------------------------------------------------------------------------
@example(proposed_mappings=[], mapping_id=SCHEMA_MAPPING_REUSED_MAPPING_ID)
@example(proposed_mappings="__absent__", mapping_id=SCHEMA_MAPPING_REUSED_MAPPING_ID)
@example(proposed_mappings=None, mapping_id=SCHEMA_MAPPING_REUSED_MAPPING_ID)
@given(
    proposed_mappings=st.one_of(_mapping_entries(), _NON_LIST_MAPPINGS),
    mapping_id=_MAPPING_IDS,
)
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_the_reuse_arm_has_exactly_two_outcomes_and_the_count_agrees_on_both(
    approval_env,
    schema_mapping_decimal_state_row,
    schema_mapping_reused_mappings_item,
    proposed_mappings,
    mapping_id,
):
    """Property 1, the half that is specific to the reuse arm rather than to the derivation.

    # Feature: p54-reused-and-skipped-run-honesty, Property 1: A reused mapping's
    # count never disagrees with the mappings it is shown beside

    ``_reused_mapping_payload`` does not return whatever ``_schema_mapping_payload``
    hands it. The guard at ``lambda_function.py:481`` re-routes a projection whose
    ``proposed_mappings`` came back ``None`` into the *unreadable* statement, so
    over the origin payload's whole shape space the arm produces exactly **two**
    key sets — the resolved thirteen or the unreadable five — and never a third.

    That guard is why a **resolved** payload can never carry
    ``proposed_mappings: None``, which is the resolved state's defining narrowing
    and the one thing that separates this arm from the non-reuse one: fed the same
    origin row, the non-reuse arm answers with a ten-key payload carrying
    ``proposed_mappings: None`` beside ``mapping_count: None``, and the reuse arm
    answers ``mapping_unreadable: True``. Both are correct — a *named reference to
    something absent* is not a mapping with no fields (``LGR-b2f79ef-14``, do-NOT
    7) — and the difference is not asserted anywhere else.

    The three keys that append on the resolved path are written after the count is
    computed (``:491-493``), so they are checked here for not having perturbed the
    pair the previous test established.

    **Validates: Requirements 1.1, 1.2**
    """
    env = approval_env
    _reset(env)
    mappings_item = {**schema_mapping_reused_mappings_item, "mapping_id": mapping_id}
    _seed_origin(env, schema_mapping_decimal_state_row, mappings_item, proposed_mappings)

    payload = _read(env, mapping_id)
    keys = list(payload)
    context = f"origin proposed_mappings={proposed_mappings!r}"

    assert keys in (list(SCHEMA_MAPPING_APPROVAL_KEYS_REUSED), list(SCHEMA_MAPPING_APPROVAL_KEYS_UNREADABLE)), (
        f"[{context}] the reuse arm produced a third key set. It has two outcomes and the count law "
        f"is asserted over both; a third shape is one nothing has reasoned about.\n"
        f"resolved:   {list(SCHEMA_MAPPING_APPROVAL_KEYS_REUSED)}\n"
        f"unreadable: {list(SCHEMA_MAPPING_APPROVAL_KEYS_UNREADABLE)}\n"
        f"       got: {keys}"
    )

    # Whichever outcome it is, the law from the previous test still holds.
    _assert_count_agrees(payload, context=context)

    assert payload["reused_mapping"] is True, (
        f"[{context}] the run reused a mapping, and that fact survives both outcomes"
    )
    assert payload["reused_mapping_id"] == mapping_id, (
        f"[{context}] both outcomes NAME the mapping — that is what makes the unreadable one a named "
        f"reference rather than an absence"
    )

    resolved = keys == list(SCHEMA_MAPPING_APPROVAL_KEYS_REUSED)

    if resolved:
        assert payload["proposed_mappings"] is not None, (
            f"[{context}] a RESOLVED payload can never carry proposed_mappings: None — the guard at "
            f"lambda_function.py:481 exists precisely to send that case to the unreadable statement, "
            f"because a ten-key payload of nulls IS a mapping with no fields (do-NOT 7)"
        )
        assert "mapping_unreadable" not in payload, f"[{context}] a resolved payload must not claim to be unreadable"
        assert payload["reused_from_run_id"] == SCHEMA_MAPPING_DECIMAL_RUN_ID, (
            f"[{context}] reused_from_run_id names the run that PRODUCED the mapping, and the appended "
            f"keys are written after the count — an id from elsewhere would mean the count and the "
            f"list came from different rows"
        )
    else:
        assert payload["mapping_unreadable"] is True, (
            f"[{context}] the unreadable outcome must SAY so — the approver being told the mapping "
            f"could not be read is the whole of R1.4"
        )
        assert payload["proposed_mappings"] is NOT_ESTABLISHED, (
            f"[{context}] never [] — an empty list renders as a mapping table with no rows, which is "
            f"do-NOT 7's collapse at the display layer"
        )
        assert payload["mapping_count"] is NOT_ESTABLISHED
        assert "reused_from_run_id" not in payload, (
            f"[{context}] there is no honest origin run id when resolution failed"
        )
