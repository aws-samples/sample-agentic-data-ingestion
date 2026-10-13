# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A Reuse_Run's schema-mapping approval carries the mapping it is gating.

# Feature: p54-reused-and-skipped-run-honesty, task 2.2

WHAT WAS WRONG
--------------
``get_approval_data``'s ``schema_mapping`` branch read the
``(run_id, "schema_mapping")`` state row and guarded the entire payload
construction on ``if "Item" in response:``. A Reuse_Run takes the
``ReuseExistingMapping`` ``sfn.Pass`` instead of the ``schema_mapping`` Lambda, and
a Pass writes nothing to DynamoDB — so the row does not exist, the guard is skipped,
and ``approval_data`` stayed ``{}``. The gate fired correctly on a human and showed
that human nothing.

This is p50 R1.2's defect one layer out. p50 corrected the *address* of the read;
it could not do anything about the *absence* of the row.

ROUTE A′ — THREE READS, ALL READS
--------------------------------
1. ``mappings_table.get_item(Key={"mapping_id": ...})`` — the shape
   ``dashboard_api/handlers/approvals.py`` already uses.
2. ``run_id`` off that item (``schema_mapping/lambda_function.py:1858-1859``, which
   sets it **conditionally**, so its absence is a real state).
3. the ORIGIN run's ``(run_id, "schema_mapping")`` row, whose
   ``output_data.approval`` is the producer's own bytes from the one
   ``create_approval_payload`` call.

Nothing here writes. Route B — having the Pass write a real state row — is rejected
on ``LGR-5d68eeb-2``'s eligibility ground (p54 do-NOT 9 / R8.9): a Pass-written row
at ``status: "success"`` would become the newest reuse candidate while the run is
still parked at the human gate.

WHY THE ORIGIN ROW AND NOT A REBUILD
-----------------------------------
``create_approval_payload`` is pure and would port, but it lives in the
``schema_mapping`` Lambda package and is unreachable from ``approval_handler``, so
"rebuild" means a second derivation of one shape in a second place —
``LGR-28b4d75-1``, ``LGR-28b4d75-5`` and ``LGR-9e76249-2`` (all template /
ratified) reject exactly that. So the resolved payload's schema keys are asserted
below to be **the origin payload's own values**, identity-compared against the
fixture rather than re-derived in the test.

THE THREE STATES, AND THE ONE THAT IS EASY TO GET WRONG
------------------------------------------------------
* **resolved** — the origin payload's ten keys, then three appended.
* **unreadable** — ``reused_mapping``, ``reused_mapping_id``,
  ``mapping_unreadable``, ``proposed_mappings: None`` and ``mapping_count: None``.
  **Never ``0``, never ``[]``, never ``{}``.** An unresolvable reused mapping is a
  *named reference to something absent* (``LGR-b2f79ef-14``), not a mapping with no
  fields — p54 do-NOT 7. ``LGR-b2f79ef-8`` is the same rule one Lambda over: "no
  pipeline record" and "no target schema" were made to stop collapsing into one
  value because a run "reported as unverifiable when the real problem is a missing
  record" is the prohibited outcome.
* **no reuse** — no row **and** no ``mapping_id``: ``{}``, unchanged. A different
  fact, and not p54's to change.

``mapping_id`` TRUTHINESS, NOT ``in``
------------------------------------
``schema_compare`` returns ``existing_mapping_id: None`` — not ``""`` — on the
no-candidate path (``LGR-5d68eeb-2``), and ``CheckSchemaMappingApproval``'s reuse
conjuncts are both satisfiable by a null, so the event can legitimately carry
``mapping_id: null``. A null id is the *no reuse* state, not an id to resolve. The
test at the bottom of the first section pins that.

FIXTURES, REGION, MOCKING
-------------------------
Shared fixtures only — ``mock_dynamodb_tables``, ``mock_lambda_env`` and the p50/p54
row fixtures from ``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` call and no
boto3 client or resource mock (``tests/unit/test_no_duplicate_mocking_prop.py``), and
``AWS_REGION`` is not set here — the unit layer pins it in ``tests/unit/conftest.py``
(``LGR-3e45e65-1``, template / ratified).

moto rather than a ``MagicMock`` table is deliberate for the same reason p50's
sibling gives: boto3's resource deserializer returns every ``N`` attribute as
``Decimal`` whatever the writer supplied, and the origin payload is written through
``convert_floats_to_decimal``, so a hand-built mock return value would assert
whatever the author typed.

ON THE MODULE-LOADING PREAMBLE — DUPLICATION, DECLARED
-----------------------------------------------------
``_load_handler`` / ``_metrics_stub`` / ``approval_env`` are the sixth copy of this
preamble in this directory. ``test_serialization_preservation_prop.py`` records the
reasoning and files the extraction as follow-up work to be done as one mechanical
change with a full-suite run behind it; that has not happened, so this file copies
rather than extracts. The distinct module name is load-bearing — each copy execs the
handler under its own name so a sibling's rebound tables cannot reach this one's.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.fixtures.conftest import (
    SCHEMA_MAPPING_APPROVAL_KEYS,
    SCHEMA_MAPPING_APPROVAL_KEYS_REUSED,
    SCHEMA_MAPPING_APPROVAL_KEYS_UNREADABLE,
    SCHEMA_MAPPING_DECIMAL_PIPELINE_ID,
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
            "approval_handler_reused_mapping_payload_read_lf",
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


def _reuse_event(mapping_id: Any = SCHEMA_MAPPING_REUSED_MAPPING_ID) -> dict[str, Any]:
    """The approval event a Reuse_Run's gate produces, with 2.1's ``mapping_id``."""
    return {
        "approval_type": "schema_mapping",
        "pipeline_id": SCHEMA_MAPPING_DECIMAL_PIPELINE_ID,
        "run_id": SCHEMA_MAPPING_REUSE_RUN_ID,
        "mapping_id": mapping_id,
    }


def _read(env, event: dict[str, Any]) -> dict[str, Any]:
    """``get_approval_data`` for the Reuse_Run — which has NO ``schema_mapping`` row."""
    return env.handler.get_approval_data("schema_mapping", SCHEMA_MAPPING_REUSE_RUN_ID, event)


# ---------------------------------------------------------------------------
# The resolved state — R1.1, R1.2
# ---------------------------------------------------------------------------
def test_a_reuse_run_carries_the_origin_payloads_own_values(
    approval_env,
    schema_mapping_decimal_state_row,
    schema_mapping_reused_mappings_item,
):
    """The moto example: origin run, mappings row naming it, Reuse_Run with no row.

    The three schema keys are asserted to be **the origin payload's own values**,
    compared against the fixture's sub-tree rather than re-derived here. That is
    what "the producer's own bytes" means and it is why route A′ was chosen over a
    rebuild.

    **Validates: Requirements 1.1, 1.2**
    """
    env = approval_env
    env.state.put_item(Item=schema_mapping_decimal_state_row)
    env.mappings.put_item(Item=schema_mapping_reused_mappings_item)
    # Deliberately NO row for SCHEMA_MAPPING_REUSE_RUN_ID — a Pass writes nothing,
    # and that absence is the condition the reuse arm exists for.

    payload = _read(env, _reuse_event())

    assert payload != {}, (
        "a Reuse_Run's approval payload being empty IS the defect — the gate fired on a human and showed them nothing"
    )
    assert list(payload) == list(SCHEMA_MAPPING_APPROVAL_KEYS_REUSED), (
        f"the reuse payload's key set or its order changed — the order is part of the serialized "
        f"bytes, and the three reuse keys APPEND (do-NOT 6).\n"
        f"expected: {list(SCHEMA_MAPPING_APPROVAL_KEYS_REUSED)}\n     got: {list(payload)}"
    )

    origin_payload = schema_mapping_decimal_state_row["output_data"]["approval"]
    for key in ("source_schema", "target_schema", "proposed_mappings"):
        assert payload[key] == origin_payload[key], (
            f"{key} is not the origin payload's own value. The whole point of route A′ is that the "
            f"reuse payload is what the approver saw when the mapping was first proposed — a "
            f"difference here means something rebuilt it."
        )

    assert payload["reused_mapping"] is True
    assert payload["reused_mapping_id"] == SCHEMA_MAPPING_REUSED_MAPPING_ID
    assert payload["reused_from_run_id"] == SCHEMA_MAPPING_DECIMAL_RUN_ID, (
        "reused_from_run_id must name the run that PRODUCED the mapping, not the run being approved"
    )
    assert "mapping_unreadable" not in payload, "a resolved payload must not claim to be unreadable"


def test_the_reuse_payload_count_agrees_with_the_mappings_it_carries(
    approval_env,
    schema_mapping_decimal_state_row,
    schema_mapping_reused_mappings_item,
):
    """R1.2 by construction — the same expression over the same list.

    ``mapping_count`` is not a new key on the reuse path; it keeps the derivation
    p50 C2 ratified (``LGR-e59edd7-2``), so it cannot disagree with the
    ``proposed_mappings`` the payload carries.

    **Validates: Requirements 1.2**
    """
    env = approval_env
    env.state.put_item(Item=schema_mapping_decimal_state_row)
    env.mappings.put_item(Item=schema_mapping_reused_mappings_item)

    payload = _read(env, _reuse_event())

    assert payload["mapping_count"] == len(payload["proposed_mappings"])
    assert isinstance(payload["mapping_count"], int) and not isinstance(payload["mapping_count"], bool), (
        "a plain int, so the Decimal boundary conversion leaves it alone and it serializes as 4 "
        "rather than 4.0 (LGR-86698d4-1)"
    )


# ---------------------------------------------------------------------------
# The no-reuse state — unchanged, and NOT this spec's to change
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "event",
    [
        pytest.param({"approval_type": "schema_mapping", "run_id": SCHEMA_MAPPING_REUSE_RUN_ID}, id="absent"),
        pytest.param(_reuse_event(None), id="null"),
        pytest.param(_reuse_event(""), id="empty-string"),
    ],
)
def test_no_row_and_no_usable_mapping_id_is_still_an_empty_payload(approval_env, event):
    """The *no reuse* state keeps ``{}`` — a different fact from unreadable.

    The ``null`` case is not hypothetical. ``schema_compare`` returns
    ``existing_mapping_id: None`` on the no-candidate path (``LGR-5d68eeb-2``), and
    ``CheckSchemaMappingApproval``'s reuse conjuncts are both satisfiable by a null
    — ``IsPresent`` is true for one, ``not(stringEquals(null, ''))`` for the other —
    so the event can carry ``mapping_id: null``. Keying on ``"mapping_id" in event``
    rather than on truthiness would resolve that null as an id and report the run's
    mapping unreadable when in fact the run reused nothing.

    **Validates: Requirements 1.4**
    """
    payload = _read(approval_env, event)

    assert payload == {}, (
        "no state row AND no usable mapping_id is the *no reuse* state. It must not acquire the "
        "unreadable markers — that would claim a reuse that did not happen."
    )


def test_a_run_with_its_own_mapping_is_never_labelled_reused(
    approval_env,
    schema_mapping_decimal_state_row,
    schema_mapping_reused_mappings_item,
):
    """A run that generated its own mapping keeps exactly the ten keys.

    The reuse arm is an ``else``, so this is structural — but it is asserted because
    a reuse key leaking onto a non-reuse payload would tell the approver a run
    reused a mapping it in fact produced, and the ten-key equality is the guard.

    **Validates: Requirements 1.1**
    """
    env = approval_env
    env.state.put_item(Item=schema_mapping_decimal_state_row)
    env.mappings.put_item(Item=schema_mapping_reused_mappings_item)

    # The run's OWN row exists, and the event still carries a mapping_id.
    payload = env.handler.get_approval_data(
        "schema_mapping",
        SCHEMA_MAPPING_DECIMAL_RUN_ID,
        {**_reuse_event(), "run_id": SCHEMA_MAPPING_DECIMAL_RUN_ID},
    )

    assert list(payload) == list(SCHEMA_MAPPING_APPROVAL_KEYS), (
        f"a run with its own schema_mapping row must keep exactly the ten keys.\n"
        f"expected: {list(SCHEMA_MAPPING_APPROVAL_KEYS)}\n     got: {list(payload)}"
    )
    for key in ("reused_mapping", "reused_mapping_id", "reused_from_run_id", "mapping_unreadable"):
        assert key not in payload


# ---------------------------------------------------------------------------
# Property 2 — the unreadable state
# ---------------------------------------------------------------------------
#: The five resolution failures design's Error Handling enumerates, plus a sixth.
#:
#: The sixth — a read that RAISED — is generated here on purpose.
#: ``get_approval_data``'s own outer ``except Exception: return {}`` would otherwise
#: convert a transient DynamoDB failure into the *no reuse* payload, i.e. into the
#: false claim that this run did not reuse a mapping. That is the collapse do-NOT 7
#: forbids, so it belongs in this property rather than outside it.
_RESOLUTION_FAILURES = (
    "mappings_item_absent",
    "mappings_item_has_no_run_id",
    "origin_row_absent",
    "origin_output_data_not_a_dict",
    "origin_approval_not_a_dict",
    "a_read_raised",
)


def _reset(env) -> None:
    """Return the environment to a clean slate between Hypothesis examples.

    WHY THIS EXISTS — IT CAUGHT A REAL COUNTEREXAMPLE, IN THE TEST.
    ``approval_env`` is function-scoped and Hypothesis runs every example inside one
    invocation, so without this the two moto tables accumulate rows across examples
    and ``env.handler.mappings_table`` stays bound to whatever the previous example
    left there. The first run of this property falsified on
    ``failure='mappings_item_absent', mapping_id='0'`` and then did not falsify on
    replay — Hypothesis's own "produces unreliable results" warning — because an
    earlier example had already seeded a row under that id, so "absent" was not
    absent. The ``a_read_raised`` case leaks the other way: it swaps in a raising
    table handle that would make every later example unreadable for the wrong
    reason, which would have made the property pass vacuously.
    """
    env.handler.state_table = env.state
    env.handler.mappings_table = env.mappings
    for table, key_names in ((env.state, ("run_id", "step_name")), (env.mappings, ("mapping_id",))):
        for item in table.scan().get("Items", []):
            table.delete_item(Key={name: item[name] for name in key_names})


def _seed_failure(env, failure: str, origin_row: dict, mappings_item: dict) -> None:
    """Seed exactly one of the six resolution failures."""
    if failure == "mappings_item_absent":
        env.state.put_item(Item=origin_row)
        return  # no mappings row at all

    if failure == "mappings_item_has_no_run_id":
        env.state.put_item(Item=origin_row)
        env.mappings.put_item(Item={k: v for k, v in mappings_item.items() if k != "run_id"})
        return

    if failure == "origin_row_absent":
        env.mappings.put_item(Item=mappings_item)
        return  # the mapping names a run whose schema_mapping row is not there

    if failure == "origin_output_data_not_a_dict":
        env.state.put_item(Item={**origin_row, "output_data": "not-a-dict"})
        env.mappings.put_item(Item=mappings_item)
        return

    if failure == "origin_approval_not_a_dict":
        env.state.put_item(Item={**origin_row, "output_data": {**origin_row["output_data"], "approval": []}})
        env.mappings.put_item(Item=mappings_item)
        return

    if failure == "a_read_raised":
        env.state.put_item(Item=origin_row)
        env.mappings.put_item(Item=mappings_item)

        class _Raising:
            def get_item(self, **_kwargs):
                raise RuntimeError("ProvisionedThroughputExceededException")

        env.handler.mappings_table = _Raising()
        return

    raise AssertionError(f"unhandled failure mode: {failure}")


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    failure=st.sampled_from(_RESOLUTION_FAILURES),
    mapping_id=st.text(
        alphabet=st.characters(min_codepoint=48, max_codepoint=122, blacklist_categories=("Cs",)),
        min_size=1,
        max_size=40,
    ).filter(lambda s: s.strip() != ""),
)
def test_an_unresolvable_reused_mapping_is_never_a_mapping_with_no_fields(
    approval_env,
    schema_mapping_decimal_state_row,
    schema_mapping_reused_mappings_item,
    failure,
    mapping_id,
):
    """Property 2: An unresolvable reused mapping is never reported as a mapping with no fields.

    # Feature: p54-reused-and-skipped-run-honesty, Property 2: An unresolvable
    # reused mapping is never reported as a mapping with no fields

    *For any* of the six resolution failures and any non-blank ``mapping_id``: the
    payload states that the mapping could not be read, and ``mapping_count`` is
    ``None`` — **never ``0``**, never ``[]``, never ``{}``.

    An unresolvable reused mapping is a *named reference to something absent*
    (``LGR-b2f79ef-14``), not a mapping with no fields, and the difference between
    ``None`` and ``0`` is invisible to every example test in this suite. This is the
    only guard for that failure mode, which is why it is a non-optional deliverable
    of task 2.2 rather than one of the spec's optional property tasks.

    **Validates: Requirements 1.4**
    """
    env = approval_env
    _reset(env)
    mappings_item = {**schema_mapping_reused_mappings_item, "mapping_id": mapping_id}
    _seed_failure(env, failure, schema_mapping_decimal_state_row, mappings_item)

    payload = _read(env, _reuse_event(mapping_id))

    assert payload.get("mapping_unreadable") is True, (
        f"[{failure}] the approver must be TOLD the mapping could not be read. Got: {payload!r}"
    )
    assert payload.get("reused_mapping") is True, (
        f"[{failure}] the run did reuse a mapping — that fact survives the mapping being unreadable"
    )
    assert payload.get("reused_mapping_id") == mapping_id, (
        f"[{failure}] the unreadable payload must still NAME the mapping it could not read — that is "
        f"what makes it a named reference rather than an absence"
    )

    assert payload.get("mapping_count") is None, (
        f"[{failure}] mapping_count must stay None. Got {payload.get('mapping_count')!r} — a count of "
        f"0 reports an unreadable mapping as a mapping with no fields, which is do-NOT 7's collapse"
    )
    assert payload.get("mapping_count") != 0 and payload.get("mapping_count") is not False, (
        f"[{failure}] 0 and False are the two values that render as a count"
    )
    assert payload.get("proposed_mappings") is None, (
        f"[{failure}] proposed_mappings must be None, never [] — an empty list renders as a mapping "
        f"table with no rows, which is the same collapse at the display layer"
    )

    assert payload != {}, (
        f"[{failure}] {{}} is the *no reuse* state and means the run generated its own mapping or "
        f"reused none. Reporting it here would be a false statement about what happened."
    )
    assert list(payload) == list(SCHEMA_MAPPING_APPROVAL_KEYS_UNREADABLE), (
        f"[{failure}] the unreadable payload's key set or order changed.\n"
        f"expected: {list(SCHEMA_MAPPING_APPROVAL_KEYS_UNREADABLE)}\n     got: {list(payload)}"
    )
    assert "reused_from_run_id" not in payload, (
        f"[{failure}] there is no honest origin run id when resolution failed — omitting the key is how that is said"
    )


# ---------------------------------------------------------------------------
# The tuples did not drift
# ---------------------------------------------------------------------------
def test_the_reuse_key_tuples_extend_rather_than_replace():
    """do-NOT 6: appended, never inserted, and the base tuple is untouched.

    ``test_schema_mapping_decimal_fixture.py:283-289``'s tuple-agreement assertion
    guards the three p50 tuples. This is its p54 sibling: the reuse tuple must be
    the base tuple *followed by* the appended three, so no existing position moves
    and every assertion on ``SCHEMA_MAPPING_APPROVAL_KEYS`` keeps its meaning.

    **The base count below was AMENDED by p55 task 6.2, from 10 to 11, and this
    paragraph is the re-reading the old assertion message asked for.** R10.1 adds
    ``rename_manifest`` to the non-reuse payload, which is exactly the "a key was
    genuinely added to the non-reuse payload" case that message names. The three
    structural assertions here are unchanged and still hold: the reuse tuple still
    opens with the base tuple in order, the appended keys still shadow none of it,
    and the unreadable tuple's relationship to both is untouched. Only the arity
    moved, and it is re-stated rather than deleted — an exact count is what fails
    when a key is added silently, and ``LGR-e5e9edd-6``'s own reason for refusing
    to grow this tuple in p54 (three keys present on ONE path, so the only repair
    would have been a subset check p54 do-NOT 6 forbids) does not apply to a key
    the projection emits with ``.get`` on EVERY path.
    """
    assert SCHEMA_MAPPING_APPROVAL_KEYS_REUSED[: len(SCHEMA_MAPPING_APPROVAL_KEYS)] == SCHEMA_MAPPING_APPROVAL_KEYS, (
        "the reuse tuple must OPEN with the base keys in their existing order — the order is part of "
        "the serialized bytes, and an insertion would move a position p50 pinned"
    )
    assert len(SCHEMA_MAPPING_APPROVAL_KEYS) == 11, (
        "the base tuple grew. If a key was genuinely added to the non-reuse payload that is fine, "
        "but the reuse tuples and this assertion have to be re-read rather than adjusted."
    )
    assert set(SCHEMA_MAPPING_APPROVAL_KEYS).isdisjoint(
        set(SCHEMA_MAPPING_APPROVAL_KEYS_REUSED[len(SCHEMA_MAPPING_APPROVAL_KEYS) :])
    ), "an appended reuse key must not shadow one of the base keys"

    # The unreadable shape is deliberately NOT the base-with-nulls plus markers.
    assert set(SCHEMA_MAPPING_APPROVAL_KEYS_UNREADABLE) - set(SCHEMA_MAPPING_APPROVAL_KEYS_REUSED) == {
        "mapping_unreadable"
    }, "mapping_unreadable is the only key unique to the unreadable state"
    assert "reused_from_run_id" not in SCHEMA_MAPPING_APPROVAL_KEYS_UNREADABLE
