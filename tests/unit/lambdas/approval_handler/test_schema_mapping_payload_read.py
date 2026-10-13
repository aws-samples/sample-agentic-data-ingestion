# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ``schema_mapping`` approval payload is read from ``output_data.approval``.

# Feature: p50-approval-payload-and-actor-display, C1

WHAT THIS FILE IS
-----------------
The regression suite for p50 C1. It fails on the pre-C1 tree and passes after,
which is the opposite polarity to ``test_schema_mapping_decimal_fixture.py`` in
this directory — that file guards the *fixture* and passes on both sides.

WHAT WAS WRONG
--------------
``get_approval_data``'s ``schema_mapping`` branch read all nine keys off the
state row's TOP level. Measured live (the validation account / us-west-2, run
``run-2026-08-24-13-48-26-4fccdf03``): ``approval_data`` was **237 characters**
with **eight of nine keys literal ``null``**, only ``generated_by`` resolving. So
a human approved a mapping having been shown nothing about it — the identical
defect, with the identical consequence, that the ``code_generation`` branch
fourteen lines below had until p34 R3.6 corrected it. ``LGR-86698d4-1``
(**template / ratified**) predicted this twin by name and named the target path:
"the real ``Decimal``-converted payload sits at ``output_data.approval``".

SEVEN KEYS MOVE, TWO DO NOT — AND THAT SPLIT IS THE POINT
--------------------------------------------------------
``LGR-86698d4-1`` says "eight of nine keys from the row's top level". It is right
about the eight measured nulls and **wrong about the address for two of them**,
which is why this file asserts the split explicitly rather than moving all nine.

Verified against both producers, not inferred:

* ``create_approval_payload`` (``schema_mapping/lambda_function.py:1732-1780``)
  builds **seven** — ``source_schema``, ``target_schema``,
  ``proposed_mappings``, ``confidence_scores``, ``unmapped_fields``,
  ``mapping_summary``, ``generated_by`` — and ``store_mapping_result`` stores
  the payload whole at ``output_data["approval"]`` (``:1927``). Those seven move.
* ``evidence`` and ``questions`` are **not in that payload at all**. A different
  writer produces them: the complex-mapping agent's ``update_item`` at
  ``:2277-2291``, whose ``UpdateExpression`` is ``SET evidence = :ev,
  questions = :qs`` against ``Key={run_id, "schema_mapping"}`` — the row's TOP
  level. They were ``None`` on the observed run because that agent did not run,
  not because the read address was wrong. **Moving them would have broken a
  working read**, and ``test_approval_handler_evidence.py:113-155`` is the
  coverage that would have caught it.

``generated_by`` resolves in both places (``:1946`` writes it at the top level
beside the payload), so it is read payload-first, row-second — the same
two-source shape ``code_version_id`` uses at ``approval_handler:300``, for the
same stated reason: both sources are the same writer's own.

WHY THE ``Decimal`` CASE IS HERE AND NOT THEORETICAL
---------------------------------------------------
Design O2 / R1.6. The boundary conversion at ``lambda_function.py:551`` —
``convert_decimals_to_float`` immediately before ``json.dumps``, ratified by
``LGR-86698d4-1`` — had never had a populated ``schema_mapping`` payload to
convert, because the branch never surfaced one. C1 is the change that first
makes ``Decimal`` reachable there, and ``confidence_scores`` is the carrier. So
the seeded row comes from ``tests/fixtures/conftest.py``'s
``SCHEMA_MAPPING_DECIMAL_STATE_ROW``, whose every numeric is a real
``decimal.Decimal`` — a ``float`` fixture would serialize cleanly and pass while
the real path raised.

FIXTURES, REGION, MOCKING
-------------------------
Shared fixtures only — ``mock_dynamodb_tables``, ``mock_lambda_env`` and the p50
row fixtures from ``tests/fixtures/conftest.py``. No ``moto.mock_aws()`` call and
no boto3 client or resource mock (``tests/unit/test_no_duplicate_mocking_prop.py``),
and ``AWS_REGION`` is not set here — the unit layer pins it in
``tests/unit/conftest.py`` (``LGR-3e45e65-1``, template / ratified).

moto rather than a ``MagicMock`` table is deliberate: the whole point of the
``Decimal`` case is that boto3's resource deserializer returns every ``N``
attribute as ``Decimal`` whatever the writer supplied, and a hand-built mock
return value simply asserts whatever the author typed.

ON THE MODULE-LOADING PREAMBLE — DUPLICATION, DECLARED
-----------------------------------------------------
``_load_handler`` / ``_metrics_stub`` / ``approval_env`` are the fifth copy of
this preamble in this directory. ``test_serialization_preservation_prop.py``
records the reasoning at length and files the extraction as follow-up work to be
done as one mechanical change with a full-suite run behind it; that has not
happened yet, so this file copies rather than extracts. The distinct module name
is load-bearing — each copy execs the handler under its own name so a sibling's
rebound tables cannot reach this one's.
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
from unittest.mock import MagicMock, patch

import boto3
import pytest
from tests.fixtures.conftest import (
    SCHEMA_MAPPING_APPROVAL_KEYS,
    SCHEMA_MAPPING_APPROVAL_KEYS_UNDER_OUTPUT_DATA,
    SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES,
    SCHEMA_MAPPING_DECIMAL_CONFIDENCE,
    SCHEMA_MAPPING_DECIMAL_PIPELINE_ID,
    SCHEMA_MAPPING_DECIMAL_RUN_ID,
    SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD,
    SCHEMA_MAPPING_DECIMAL_TARGET_FIELD,
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

RUN_ID = SCHEMA_MAPPING_DECIMAL_RUN_ID
PIPELINE_ID = SCHEMA_MAPPING_DECIMAL_PIPELINE_ID
TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAA" + "p50" * 14

#: The two of the nine whose correct address IS the row's top level.
TOP_LEVEL_KEYS = ("evidence", "questions")


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

    ``create_approval_request`` is decorated with ``@time_function(...)``. A bare
    ``MagicMock`` replaces it with a call-swallowing mock, so no approval row
    would be written and the boundary assertions below would hold vacuously.
    """
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
            "approval_handler_schema_mapping_payload_read_lf",
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
            f"mock, which would make the boundary assertions in this file vacuous"
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

    return SimpleNamespace(
        handler=handler,
        approvals=mock_dynamodb_tables[APPROVALS_TABLE],
        state=mock_dynamodb_tables[STATE_TABLE],
    )


def _seed(env, row: dict) -> None:
    env.state.put_item(Item=row)


def _read(env) -> dict[str, Any]:
    return env.handler.get_approval_data("schema_mapping", RUN_ID)


def _event() -> dict[str, Any]:
    return {
        "approval_type": "schema_mapping",
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "task_token": TASK_TOKEN,
    }


# ---------------------------------------------------------------------------
# R1.1 — the payload reaches the approver
# ---------------------------------------------------------------------------
def test_the_populated_payload_reaches_the_approver(approval_env, schema_mapping_decimal_state_row):
    """All nine keys resolve, and ``proposed_mappings`` carries the real mapping.

    The regression test named by design's testing table for R1.1, asserted
    against the observed run's real shape. Composed with the complex-mapping
    agent's two top-level attributes so all nine are exercised at once —
    ``evidence`` and ``questions`` are absent from the base row precisely because
    that agent did not run on the observed one.

    **Validates: Requirements 1.1, 1.2**
    """
    env = approval_env
    _seed(env, {**schema_mapping_decimal_state_row, **SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES})

    payload = _read(env)

    assert list(payload) == list(SCHEMA_MAPPING_APPROVAL_KEYS), (
        f"the key set or its order changed — the order is part of the serialized bytes.\n"
        f"expected: {list(SCHEMA_MAPPING_APPROVAL_KEYS)}\n     got: {list(payload)}"
    )
    nulls = sorted(key for key, value in payload.items() if value is None)
    assert nulls == [], (
        f"R1.1: eight of these nine were null before C1. Every one must now resolve; still null: {nulls}"
    )

    mappings = payload["proposed_mappings"]
    assert isinstance(mappings, list) and mappings, (
        f"proposed_mappings being null is the whole defect — got {mappings!r}"
    )
    documented = [
        m
        for m in mappings
        if m.get("source_field") == SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD
        and m.get("target_field") == SCHEMA_MAPPING_DECIMAL_TARGET_FIELD
    ]
    assert len(documented) == 1, (
        f"the mapping requirements.md records for this run "
        f"({SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD} -> {SCHEMA_MAPPING_DECIMAL_TARGET_FIELD}) "
        f"must survive the read; got {mappings!r}"
    )
    assert documented[0]["confidence"] == SCHEMA_MAPPING_DECIMAL_CONFIDENCE
    assert payload["confidence_scores"][SCHEMA_MAPPING_DECIMAL_TARGET_FIELD] == SCHEMA_MAPPING_DECIMAL_CONFIDENCE


# ---------------------------------------------------------------------------
# R1.2 — the read targets output_data.approval, not the top level
# ---------------------------------------------------------------------------
def test_the_seven_keys_are_read_from_output_data_and_not_the_top_level(approval_env, schema_mapping_decimal_state_row):
    """Decoys at the top level lose to the producer's payload.

    A row carrying a contradicting value at the top level for each of the seven
    is not a shape any producer writes; it is here because it is the only way to
    prove *which* address the branch read, rather than that it read something.
    ``generated_by`` is excluded — it legitimately resolves in both places and
    has its own test below.

    **Validates: Requirements 1.2**
    """
    env = approval_env
    decoys = {
        key: f"TOP-LEVEL-DECOY-{key}" for key in SCHEMA_MAPPING_APPROVAL_KEYS_UNDER_OUTPUT_DATA if key != "generated_by"
    }
    _seed(env, {**schema_mapping_decimal_state_row, **decoys})

    payload = _read(env)

    wrong = {key: value for key, value in payload.items() if isinstance(value, str) and value.startswith("TOP-LEVEL")}
    assert wrong == {}, (
        f"R1.2: these keys were read off the row's top level, where the producer writes nothing "
        f"(create_approval_payload -> output_data.approval, schema_mapping:1927): {wrong}"
    )
    assert payload["mapping_summary"] == schema_mapping_decimal_state_row["output_data"]["approval"]["mapping_summary"]


def test_evidence_and_questions_are_still_read_from_the_row_top_level(approval_env, schema_mapping_decimal_state_row):
    """The two keys C1 must NOT move, and the reason stated at the assertion.

    ``create_approval_payload`` (``schema_mapping:1732-1780``) does not build
    these; the complex-mapping agent's ``update_item`` (``:2277-2291``) SETs them
    at the row's top level. Reading them from ``output_data.approval`` would
    break a working read, so this test seeds them ONLY at the top level and
    additionally plants decoys inside the payload to prove the payload does not
    win for these two.

    **Validates: Requirements 1.2, 1.3**
    """
    env = approval_env
    row = schema_mapping_decimal_state_row
    row["output_data"]["approval"]["evidence"] = "PAYLOAD-DECOY-evidence"
    row["output_data"]["approval"]["questions"] = ["PAYLOAD-DECOY-questions"]
    _seed(env, {**row, **SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES})

    payload = _read(env)

    assert payload["evidence"] == SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES["evidence"], (
        "evidence's producer is the complex-mapping agent's top-level update_item "
        "(schema_mapping:2277-2291), not create_approval_payload — the top-level read is correct"
    )
    assert payload["questions"] == SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES["questions"]


def test_evidence_and_questions_stay_none_when_the_complex_agent_did_not_run(
    approval_env, schema_mapping_decimal_state_row
):
    """The observed run's own shape: seven populated, two ``None``.

    This is what the live row looked like for those two keys, and it must keep
    looking like that — they are null because the agent did not run, which is a
    true statement about the run rather than a read defect.

    **Validates: Requirements 1.1, 1.5**
    """
    env = approval_env
    _seed(env, schema_mapping_decimal_state_row)

    payload = _read(env)

    assert [key for key, value in payload.items() if value is None] == list(TOP_LEVEL_KEYS)
    assert payload["proposed_mappings"], "the seven payload keys must resolve regardless"


def test_generated_by_resolves_from_either_of_the_producers_two_writes(approval_env, schema_mapping_decimal_state_row):
    """Payload first, row second — the ``code_version_id`` shape at ``:300``.

    The producer writes ``generated_by`` inside the payload (``:1760``) *and* at
    the row's top level (``:1946``), so both are the same writer's own and either
    is a legitimate source. A row written before the payload carried it still
    resolves.

    **Validates: Requirements 1.1, 1.3**
    """
    env = approval_env
    row = schema_mapping_decimal_state_row

    # Both present: the payload wins, and the two agree in production anyway.
    _seed(env, {**row, "generated_by": "row-level-value"})
    assert _read(env)["generated_by"] == "us.anthropic.claude-sonnet-4-6"

    # Payload silent (an older row, or the error payload's ``""`` default):
    # the top level still answers.
    row_without_payload_provenance = {**row, "generated_by": "row-level-value"}
    row_without_payload_provenance["output_data"]["approval"].pop("generated_by")
    _seed(env, row_without_payload_provenance)
    assert _read(env)["generated_by"] == "row-level-value"


# ---------------------------------------------------------------------------
# R1.3 — degraded rows resolve to absent keys, never to an exception
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("absent", None),
        ("null", None),
        ("string", "not-a-dict"),
        ("list", ["not", "a", "dict"]),
        ("number", Decimal("7")),
    ],
    ids=["output_data absent", "output_data null", "output_data string", "output_data list", "output_data number"],
)
def test_a_degraded_output_data_yields_absent_keys_without_raising(approval_env, attribute, value):
    """Every pre-C1 row is in this state, so it must degrade rather than raise.

    ``get_approval_data`` swallows its own exceptions and returns ``{}``
    (``:405-407``), so "does not raise" is not enough on its own — a raise would
    show up as an empty payload rather than a stack trace. Both are asserted:
    the nine keys are present, and the seven payload-sourced ones are ``None``.

    **Validates: Requirements 1.3, 1.5**
    """
    env = approval_env
    row: dict[str, Any] = {
        "run_id": RUN_ID,
        "step_name": "schema_mapping",
        "status": "success",
        "generated_by": "us.anthropic.claude-sonnet-4-6",
    }
    if attribute != "absent":
        row["output_data"] = value
    _seed(env, row)

    payload = _read(env)

    assert list(payload) == list(SCHEMA_MAPPING_APPROVAL_KEYS), (
        f"a degraded row must still produce the full key set, not {{}} from the except at :405-407; got {payload!r}"
    )
    for key in SCHEMA_MAPPING_APPROVAL_KEYS_UNDER_OUTPUT_DATA:
        if key == "generated_by":
            continue
        assert payload[key] is None, f"{key} should read as absent on a degraded row, got {payload[key]!r}"
    assert payload["generated_by"] == "us.anthropic.claude-sonnet-4-6", (
        "the top-level second source is what keeps provenance readable on an old row"
    )
    # p50 C2 / do-NOT 7: the count is NOT ESTABLISHED here, and `0` would be a
    # false statement — this row's mappings were unreadable, not empty.
    assert payload["mapping_count"] is None, (
        f"an unreadable payload must yield not-established, never 0 (R1.5, do-NOT 7); got {payload['mapping_count']!r}"
    )


@pytest.mark.parametrize(
    "approval_value",
    [None, "not-a-dict", ["not", "a", "dict"], Decimal("7")],
    ids=["approval null", "approval string", "approval list", "approval number"],
)
def test_a_degraded_approval_subtree_yields_absent_keys_without_raising(approval_env, approval_value):
    """The second guard, same shape as the first — copied from ``:288-295``.

    **Validates: Requirements 1.3**
    """
    env = approval_env
    _seed(
        env,
        {
            "run_id": RUN_ID,
            "step_name": "schema_mapping",
            "output_data": {"artifact_type": "schema_mapping", "approval": approval_value},
        },
    )

    payload = _read(env)

    assert list(payload) == list(SCHEMA_MAPPING_APPROVAL_KEYS)
    assert payload["proposed_mappings"] is None
    assert payload["confidence_scores"] is None
    assert payload["mapping_count"] is None, (
        "p50 C2 / do-NOT 7: a degraded approval subtree makes the count not-established, not 0"
    )


def test_a_missing_row_still_yields_an_empty_payload(approval_env):
    """Unchanged behaviour: no ``Item`` means no keys at all, not nine nulls.

    Pinned because the ``code_generation`` branch this fix copies uses
    ``.get("Item", {})`` and therefore always emits its key set. This branch's
    ``if "Item" in response`` is deliberately retained — ``test_approval_system.py``
    ``test_get_approval_data_missing_data`` asserts the empty dict.

    **Validates: Requirements 1.3**
    """
    assert _read(approval_env) == {}


# ---------------------------------------------------------------------------
# R1.6 / CP1 — the Decimal boundary, now reachable for the first time
# ---------------------------------------------------------------------------
def test_the_populated_decimal_payload_serializes_at_the_boundary(approval_env, schema_mapping_decimal_state_row):
    """The whole payload round-trips through ``create_approval_request``.

    Design O2: the conversion at ``:551`` had never had a populated
    ``schema_mapping`` payload to convert, and C1 is what makes ``Decimal``
    reachable there. Every numeric in the seeded row is a real ``Decimal``, so a
    boundary that stopped converting would raise ``TypeError`` here rather than
    passing quietly.

    ``LGR-86698d4-1``'s accepted cost applies: an integral ``Decimal`` widens to
    a ``float`` (``Decimal("4")`` -> ``4.0``). Asserted as equality with the
    numeric value rather than as a type, because that widening is ratified.

    **Validates: Requirements 1.1, 1.6**
    """
    env = approval_env
    _seed(env, {**schema_mapping_decimal_state_row, **SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES})

    with (
        patch.object(env.handler, "send_approval_notifications"),
        patch.object(env.handler, "publish_approval_status_change"),
    ):
        approval_id = env.handler.create_approval_request(_event())

    stored = env.approvals.get_item(Key={"approval_id": approval_id}).get("Item")
    assert stored is not None, f"no approval row was written for {approval_id!r}"

    parsed = json.loads(str(stored["approval_data"]))

    assert parsed["confidence_scores"][SCHEMA_MAPPING_DECIMAL_TARGET_FIELD] == float(SCHEMA_MAPPING_DECIMAL_CONFIDENCE)
    assert len(parsed["proposed_mappings"]) == 4
    # p50 C2: the derived count survives the wire hop as an int, not 4.0. The row
    # also stores Decimal("4") at its top level; reading that would have widened
    # to a float here under LGR-86698d4-1's accepted cost, for no reason.
    assert parsed["mapping_count"] == 4
    assert isinstance(parsed["mapping_count"], int) and not isinstance(parsed["mapping_count"], bool), (
        f"a derived plain int must serialize as 4, not 4.0; got {parsed['mapping_count']!r}"
    )
    assert parsed["unmapped_fields"]["source"] == ["legacy_flag"]
    assert parsed["evidence"][SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD]["support"] == 0.87
    assert parsed["generated_by"] == "us.anthropic.claude-sonnet-4-6"

    # 237 characters was the measured size of the empty payload. A populated one
    # cannot be that small, and that number is the tell requirements.md names.
    assert len(str(stored["approval_data"])) > 237 * 4, (
        f"the stored payload is {len(str(stored['approval_data']))} characters — the null payload "
        f"measured 237, so a populated one being anywhere near that means the read is still wrong"
    )
