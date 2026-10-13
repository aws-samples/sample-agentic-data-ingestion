# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``mapping_count`` is three-state: a number, ``0``, or not-established.

# Feature: p50-approval-payload-and-actor-display, Property 2 (CP2)

THE PROPERTY
------------
An unreadable payload never yields ``mapping_count == 0``, and an empty mapping
list never yields not-established. **The two are distinct for every input.**

That is design CP2 verbatim, and it is the whole point of C2. A mapping with zero
entries and a mapping that could not be read are different facts about a run
(R1.5), and an approver who is shown ``0`` for the second one has been told
something false — the same class of quiet wrongness as the eight nulls C1 fixed,
one field along.

WHY THE COUNT IS DERIVED AND NOT READ
-------------------------------------
The state row carries its own top-level ``mapping_count``: ``store_mapping_result``
writes it as ``len(mappings)`` at ``schema_mapping/lambda_function.py:1938``,
beside the payload. C2 declines to read it, and ``test_the_derived_count_ignores_a
_contradicting_stored_attribute`` below is the assertion that holds that line.

The reason is not that the stored number is wrong — at write time the two agree.
It is that ``proposed_mappings`` is **what the approver is shown**, so a count
derived from it cannot disagree with the list beside it, whereas a count read from
the row's top level is a second authority that survives an ``output_data`` this
branch cannot read and would then print a confident ``Mapping count: 4`` next to
an empty Mapping Preview. ``LGR-28b4d75-1`` and ``LGR-9e76249-2`` (both
**template / ratified**) reject a second authority for a derivable number on
exactly this ground: derive on the read path, never persist. do-NOT 8 forbids a
*new* stored field; C2 goes further and declines the existing one.

There is already a fixture in this repo where the two disagree —
``test_serialization_preservation_prop.py``'s minimal row stores ``mapping_count:
3`` against a payload holding **one** proposed mapping — so the divergence is
demonstrable rather than hypothetical.

THE THREE-STATE SHAPE, AND ITS GOVERNING RECORD
-----------------------------------------------
``LGR-4854b3d-1`` (**template / ratified**) settled this for ``field_count``: "a
positive number, ``0``, or null — and null is never collapsed to ``0``", because
"every hand-written ``?? 0`` collapses null into 'declares nothing'". C2 follows
it, including the representation: **not-established is ``None``**, which crosses
the wire as ``null``. The key is always present when the state row is, so the
payload's key set does not vary with the row's health — a degraded row and a
populated one both emit ten keys, which is what keeps
``SCHEMA_MAPPING_APPROVAL_KEYS`` a single pinned tuple.

``LGR-9a35b2d-2`` (**template / ratified**) measured what the collapse costs on
the sibling listing surface: ``mapping_count: 0`` reported for a pipeline whose
``schema_mapping`` row carried **7**. Different code path, same field name, same
failure — which is why ``isinstance(..., list)`` rather than a bare ``len()`` is
load-bearing here: ``len()`` accepts a ``str`` and a ``dict``, so a malformed
``proposed_mappings`` would otherwise be reported as a character or key count.

WHY MagicMock HERE AND moto NEXT DOOR
-------------------------------------
This file uses a ``MagicMock`` state table, following
``test_code_generation_payload_prop.py`` in this directory — the in-directory
precedent for a property over ``get_approval_data``'s pure derivation. Two
reasons, both specific to this property:

* the derivation is ``len()`` over a list, and nothing about it depends on boto3's
  deserializer. The ``Decimal`` half of the read *does*, and it is covered with
  real moto tables in ``test_schema_mapping_payload_read.py``.
* the adversarial half of CP2 needs values DynamoDB would refuse to store at all.
  A table that cannot hold the input cannot test the guard against it.

No ``mock_aws()`` call, so ``tests/unit/test_no_duplicate_mocking_prop.py`` is
satisfied; ``AWS_REGION`` is left to ``tests/unit/conftest.py`` (``LGR-3e45e65-1``,
template / ratified).

**Validates: Requirements 1.4, 1.5**
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("STATE_TABLE", "test-state")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

# ---------------------------------------------------------------------------
# Mock setup — patch boto3 before importing the handler module
# ---------------------------------------------------------------------------
mock_task_tokens_table = MagicMock()
mock_approvals_table = MagicMock()
mock_state_table = MagicMock()

_table_map = {
    "test-task-tokens": mock_task_tokens_table,
    "test-approvals": mock_approvals_table,
    "test-state": mock_state_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client"):
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo

    sys.modules.pop("lambda_function", None)
    _spec = importlib.util.spec_from_file_location(
        "approval_handler_mapping_count_three_state_lf",
        os.path.join(LAMBDA_DIR, "lambda_function.py"),
    )
    handler = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(handler)

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

# Bound at module scope deliberately: Hypothesis runs many examples inside one
# test function, so per-example state lives in _install_row rather than in a
# function-scoped fixture. Nothing here writes, so there is no state to leak.
handler.state_table = mock_state_table

RUN_ID = "run-2026-08-24-13-48-26-4fccdf03"

#: The sentinel this whole file exists to protect. Named so the assertion
#: messages can say what is wrong rather than only that something is.
NOT_ESTABLISHED = None


# ---------------------------------------------------------------------------
# Generators — the producer's real mapping shape, and the shapes it never writes
# ---------------------------------------------------------------------------
def _mapping_entries(min_size: int = 0, max_size: int = 8):
    """``mapping_result["mappings"]`` as the producer emits it.

    Field names are drawn from a small pool rather than free text: the count is a
    function of list LENGTH alone, so entry content is irrelevant to the property
    and unconstrained text would only slow the run down. Confidences are real
    ``Decimal``s because that is what boto3 returns (R1.6's carrier).
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


#: Everything ``proposed_mappings`` can be that is NOT a list, plus the absent
#: case. Every one of these must read as not-established, and ``len()`` would
#: happily answer for three of them — the ``str``, the ``dict`` and the mapping
#: dict — which is precisely why the guard tests the type and not truthiness.
_NON_LIST_MAPPINGS = st.sampled_from(
    [
        None,
        "",
        "not-a-list",
        "[]",
        {},
        {"cust_id": "customer_id"},
        Decimal("0"),
        Decimal("4"),
        0,
        4,
        True,
        False,
        set(),
        ("cust_id", "customer_id"),
    ]
)

#: Shapes of ``output_data`` that make the payload unreachable. These are the
#: pre-C1 states, so every approval row written before this spec is one of them.
_DEGRADED_OUTPUT_DATA = st.sampled_from(
    [
        "__absent__",
        None,
        "not-a-dict",
        ["not", "a", "dict"],
        Decimal("7"),
        {},
        {"artifact_type": "schema_mapping"},
        {"artifact_type": "schema_mapping", "approval": None},
        {"artifact_type": "schema_mapping", "approval": "not-a-dict"},
        {"artifact_type": "schema_mapping", "approval": ["not", "a", "dict"]},
        {"artifact_type": "schema_mapping", "approval": Decimal("7")},
        {"artifact_type": "schema_mapping", "approval": {}},
    ]
)

#: Values the row's top-level ``mapping_count`` attribute can hold. Included so
#: the derivation can be shown to ignore it — including when it contradicts.
_STORED_COUNT_DECOYS = st.sampled_from([Decimal("0"), Decimal("1"), Decimal("3"), Decimal("99"), None, "four"])


def _install_row(row: dict[str, Any] | None) -> None:
    """Point the mocked state table at one ``(run_id, "schema_mapping")`` row."""
    mock_state_table.reset_mock(return_value=True, side_effect=True)
    mock_state_table.get_item.side_effect = lambda Key: {"Item": row} if row is not None else {}


def _row(proposed_mappings: Any, *, stored_count: Any = None, omit_mappings: bool = False) -> dict[str, Any]:
    """The producer's row shape, with ``proposed_mappings`` under the payload."""
    approval: dict[str, Any] = {"approval_type": "schema_mapping"}
    if not omit_mappings:
        approval["proposed_mappings"] = proposed_mappings

    row: dict[str, Any] = {
        "run_id": RUN_ID,
        "step_name": "schema_mapping",
        "status": "success",
        "generated_by": "us.anthropic.claude-sonnet-4-6",
        "output_data": {"artifact_type": "schema_mapping", "approval": approval},
    }
    if stored_count is not None:
        row["mapping_count"] = stored_count
    return row


def _count(row: dict[str, Any] | None) -> Any:
    _install_row(row)
    return handler.get_approval_data("schema_mapping", RUN_ID).get("mapping_count", "__key-absent__")


_PROPERTY_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    # One module-scope mock table serves every example. Neither half of this
    # property writes anything, and `_install_row` replaces the side effect at the
    # start of each example, so there is no cross-example state to leak.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ---------------------------------------------------------------------------
# CP2, first half — an unreadable payload is never 0
# ---------------------------------------------------------------------------
@example(proposed_mappings="not-a-list")
@example(proposed_mappings={})
@example(proposed_mappings=Decimal("4"))
@example(proposed_mappings=None)
@given(proposed_mappings=_NON_LIST_MAPPINGS)
@_PROPERTY_SETTINGS
def test_a_non_list_proposed_mappings_is_never_reported_as_zero(proposed_mappings):
    """do-NOT 7, and design's error-handling table row for this exact case.

    "``proposed_mappings`` present but not a list -> ``mapping_count`` is
    not-established, not ``0``". ``0`` is excluded explicitly as well as ``None``
    asserted, because ``0``, ``False`` and ``Decimal("0")`` all compare equal and
    an identity check alone would not say which of them slipped through.

    **Validates: Requirements 1.4, 1.5**
    """
    count = _count(_row(proposed_mappings))

    assert count is NOT_ESTABLISHED, (
        f"proposed_mappings={proposed_mappings!r} is not a list, so the count is not established; "
        f"got {count!r}. len() would have answered for a str or a dict — that is the bug the "
        f"isinstance guard prevents"
    )
    assert count != 0, f"do-NOT 7: an unreadable payload reported as 0 says 'zero mappings'; got {count!r}"


@example(output_data="__absent__")
@example(output_data=None)
@example(output_data={"artifact_type": "schema_mapping", "approval": {}})
@given(output_data=_DEGRADED_OUTPUT_DATA)
@_PROPERTY_SETTINGS
def test_a_degraded_container_is_never_reported_as_zero(output_data):
    """Every pre-C1 row is one of these shapes, and none of them means "zero".

    The two ``isinstance`` guards C1 copied from the ``code_generation`` branch
    coerce an unusable ``output_data`` or ``approval`` to ``{}``, which makes
    ``proposed_mappings`` read as absent. Absent is not empty.

    **Validates: Requirements 1.3, 1.4, 1.5**
    """
    row: dict[str, Any] = {
        "run_id": RUN_ID,
        "step_name": "schema_mapping",
        "generated_by": "us.anthropic.claude-sonnet-4-6",
        # A stored count is present throughout, so a `0` here could only come
        # from a collapse and never from the second authority.
        "mapping_count": Decimal("4"),
    }
    if output_data != "__absent__":
        row["output_data"] = output_data

    count = _count(row)

    assert count is NOT_ESTABLISHED, (
        f"output_data={output_data!r} makes the payload unreadable, so the count is not established; got {count!r}"
    )
    assert count != 0, f"do-NOT 7: got {count!r} for a row whose mappings could not be read at all"


def test_an_absent_proposed_mappings_key_is_not_established():
    """The payload is a dict but says nothing about mappings.

    Distinct from a ``None`` value: an older producer, or the error payload,
    simply omits the key. Same answer, asserted separately because a ``.get``
    default is the easy place for a ``0`` to appear.

    **Validates: Requirements 1.4, 1.5**
    """
    count = _count(_row(None, omit_mappings=True))

    assert count is NOT_ESTABLISHED, f"an omitted key is not established, got {count!r}"
    assert count != 0


# ---------------------------------------------------------------------------
# CP2, second half — a readable list is never not-established
# ---------------------------------------------------------------------------
@example(mappings=[])
@given(mappings=_mapping_entries())
@_PROPERTY_SETTINGS
def test_a_readable_list_yields_its_length_and_is_never_not_established(mappings):
    """``len(proposed_mappings)`` for every list, empty included.

    The empty list is the half of CP2 that is easy to lose: a truthiness test
    (``len(x) if x else None``) passes every other example in this property and
    turns ``[]`` into not-established, which is the mirror image of do-NOT 7 —
    refusing to state a zero the producer did state.

    **Validates: Requirements 1.4, 1.5**
    """
    count = _count(_row(mappings))

    assert count == len(mappings), f"the count is the list's length; got {count!r} for {len(mappings)} mappings"
    assert count is not NOT_ESTABLISHED, (
        f"a readable list always establishes a count, including the empty one; got {count!r}"
    )
    assert isinstance(count, int) and not isinstance(count, bool), (
        f"a derived count is a plain int so the boundary conversion at lambda_function.py:551 "
        f"leaves it alone and it serializes as 4 rather than 4.0; got {type(count).__name__}"
    )


def test_an_empty_list_is_zero_and_not_not_established():
    """The named case, spelled out because it is the one CP2 pairs against.

    ``[]`` is a producer statement — this run mapped nothing — and it must reach
    the approver as ``0``.

    **Validates: Requirements 1.4, 1.5**
    """
    count = _count(_row([]))

    assert count == 0
    assert count is not NOT_ESTABLISHED, "an empty mapping list is a stated zero, not an absence"


# ---------------------------------------------------------------------------
# CP2's conjunction — the two states are distinct for every input
# ---------------------------------------------------------------------------
@given(unreadable=_NON_LIST_MAPPINGS, degraded=_DEGRADED_OUTPUT_DATA)
@_PROPERTY_SETTINGS
def test_the_empty_and_the_unreadable_never_collapse_into_each_other(unreadable, degraded):
    """The property as design states it: distinct for every input.

    Asserting each half separately leaves one gap — a representation where both
    answers happen to be the same object. This closes it by computing the empty
    answer and both unreadable answers in one example and requiring the pair to
    differ, by value and by identity.

    **Validates: Requirements 1.4, 1.5**
    """
    empty_answer = _count(_row([]))

    degraded_row: dict[str, Any] = {"run_id": RUN_ID, "step_name": "schema_mapping"}
    if degraded != "__absent__":
        degraded_row["output_data"] = degraded

    for unreadable_answer in (_count(_row(unreadable)), _count(degraded_row)):
        assert empty_answer != unreadable_answer, (
            f"R1.5: zero mappings and an unreadable mapping are different facts, and both rendered as {empty_answer!r}"
        )
        assert (empty_answer is NOT_ESTABLISHED) is not (unreadable_answer is NOT_ESTABLISHED)


# ---------------------------------------------------------------------------
# The derive-vs-read decision, held by assertion
# ---------------------------------------------------------------------------
@example(stored_count=Decimal("99"), mappings=[])
@example(stored_count=Decimal("0"), mappings=[{"source_field": "cust_id", "target_field": "customer_id"}])
@given(stored_count=_STORED_COUNT_DECOYS, mappings=_mapping_entries(max_size=5))
@_PROPERTY_SETTINGS
def test_the_derived_count_ignores_a_contradicting_stored_attribute(stored_count, mappings):
    """C2 derives; it does not read the row's own ``mapping_count``.

    The row genuinely carries that attribute in production
    (``schema_mapping/lambda_function.py:1938``), so "we derive" is a claim that
    needs holding rather than asserting once. A contradicting stored value is not
    a shape any producer writes — it is here for the same reason the top-level
    decoys in ``test_schema_mapping_payload_read.py`` are: it is the only way to
    prove *which* source answered, rather than that something did.

    **Validates: Requirements 1.4**
    """
    count = _count(_row(mappings, stored_count=stored_count))

    assert count == len(mappings), (
        f"the row stores mapping_count={stored_count!r} and the payload holds {len(mappings)} "
        f"mappings; a {count!r} here means the branch consumed the second authority that "
        f"LGR-28b4d75-1 and LGR-9e76249-2 reject"
    )


def test_no_stored_mapping_count_field_is_written_by_this_derivation():
    """do-NOT 8, from the other direction: the derivation adds no writer.

    C2 is a read-path change. ``get_approval_data`` performs exactly one
    ``get_item`` for this branch and no write of any kind, so there is no second
    place a count could come to live.

    **Validates: Requirements 1.4**
    """
    _install_row(_row([{"source_field": "cust_id", "target_field": "customer_id"}]))

    handler.get_approval_data("schema_mapping", RUN_ID)

    assert mock_state_table.put_item.call_count == 0
    assert mock_state_table.update_item.call_count == 0
    assert mock_state_table.get_item.call_count == 1


# ---------------------------------------------------------------------------
# The wire shape task 4.2 receives
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("case", "proposed_mappings", "expected"),
    [
        ("populated", [{"source_field": "cust_id", "target_field": "customer_id"}], 1),
        ("stated zero", [], 0),
        ("not established", None, None),
    ],
)
def test_the_three_states_survive_the_json_boundary(case, proposed_mappings, expected):
    """What the dashboard actually receives, per state.

    ``create_approval_request`` stores the payload as a JSON string and
    ``get_run_details`` parses it back into ``details``, so a state that does not
    survive ``json.dumps`` never reaches the UI. ``None`` crosses as ``null`` and
    arrives as ``undefined``-adjacent ``null`` in TypeScript — which
    ``ApprovalWorkflowSection.tsx:161`` (``?? '—'``) already renders correctly and
    ``ApprovalsPage.tsx:39`` (``?? 0``) does not. Task 4.2 owns that second site;
    this test pins what it will be handed.

    **Validates: Requirements 1.4, 1.5**
    """
    payload = {"mapping_count": _count(_row(proposed_mappings))}

    round_tripped = json.loads(json.dumps(payload))
    received = round_tripped["mapping_count"]

    assert "mapping_count" in round_tripped, (
        f"{case}: the key is always present when the state row is, so the UI can tell "
        f"not-established from a payload that predates p50"
    )
    if expected is None:
        assert received is None, f"{case}: not-established must cross the wire as null, got {received!r}"
    else:
        assert received == expected, f"{case}: expected {expected!r} across the wire, got {received!r}"
        assert isinstance(received, int) and not isinstance(received, bool), (
            f"{case}: an established count arrives as a JSON integer, not {type(received).__name__}"
        )
