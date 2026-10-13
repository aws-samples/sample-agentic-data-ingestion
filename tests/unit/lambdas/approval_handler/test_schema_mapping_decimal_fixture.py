# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guards on the ``schema_mapping`` ``Decimal`` fixture — p50 task 1.2.

WHAT THIS FILE IS
-----------------
Not a test of ``get_approval_data``. It is a guard on the **fixture** that p50's
C1 needs, and it is deliberately written so it passes both before and after that
change: nothing here reads the handler.

The reason it exists is design O2. The boundary conversion at
``approval_handler/lambda_function.py:551`` — ``convert_decimals_to_float``
immediately before ``json.dumps``, ratified by ``LGR-86698d4-1`` — has never had
a populated ``schema_mapping`` payload to convert, because the branch at
``:245-263`` reads all nine keys off the state row's top level where the producer
writes almost nothing. C1 is the change that first makes ``Decimal`` reachable
there, and ``confidence_scores`` is the carrier.

That makes one specific mistake very cheap to make and very expensive to catch
later: writing the fixture with ``float`` values. A float fixture serializes
cleanly through ``json.dumps``, so the C1 regression test would pass while the
real path — which gets ``Decimal`` back from DynamoDB for every numeric
attribute, whatever the writer supplied — raises ``TypeError``. So the assertion
that carries the weight here is the **negative control**
(``test_the_raw_fixture_is_not_json_serializable``): if anybody downgrades those
values to floats, that test fails immediately and says why.

WHY THE NUMERICS ARE ALL ``Decimal`` EVEN WHERE THE PRODUCER WROTE AN ``int``
----------------------------------------------------------------------------
``store_mapping_result`` writes the payload as
``convert_floats_to_decimal(approval_payload)``
(``schema_mapping/lambda_function.py:1927``), which converts ``float`` and
leaves ``int`` alone — so ``mapped_fields`` goes in as an ``int``. It does not
come back as one. DynamoDB has a single numeric type ``N``, and boto3's
resource-level deserializer returns every ``N`` attribute as ``decimal.Decimal``
regardless. The fixture stands for what comes **back off the table**, so a plain
``int`` anywhere in it would be a shape the read path cannot actually produce.
``test_decimal_serialization_prop.py``'s ``OBSERVED_CODE_QUALITY_METRICS`` makes
the same choice for the same reason, from a measured live row.

``bool`` is the exception and is asserted separately: it subclasses ``int``, and
``shared/decimal_utils.py``'s docstring records that it is deliberately NOT
converted, so ``approval_required`` must stay a ``bool`` on both sides of the
boundary.

FIXTURES AND MOCKING
--------------------
Shared fixtures only, from ``tests/fixtures/conftest.py``. No ``moto.mock_aws()``
call and no boto3 client or resource mock — nothing here touches AWS
(``tests/unit/test_no_duplicate_mocking_prop.py``). ``AWS_REGION`` is not set
here; the unit layer pins it in ``tests/unit/conftest.py``
(``LGR-3e45e65-1``, template / ratified).
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from typing import Any

from tests.fixtures.conftest import (
    SCHEMA_MAPPING_APPROVAL_KEYS,
    SCHEMA_MAPPING_APPROVAL_KEYS_DERIVED,
    SCHEMA_MAPPING_APPROVAL_KEYS_UNDER_OUTPUT_DATA,
    SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES,
    SCHEMA_MAPPING_DECIMAL_CONFIDENCE,
    SCHEMA_MAPPING_DECIMAL_RUN_ID,
    SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD,
    SCHEMA_MAPPING_DECIMAL_STATE_ROW,
    SCHEMA_MAPPING_DECIMAL_TARGET_FIELD,
)

# The shared layer, on the convention `tests/unit/shared/test_decimal_utils.py`
# uses. `convert_decimals_to_float` is the converter `LGR-86698d4-1` mandates at
# the boundary; it is exercised here against this fixture specifically, not
# re-tested in general (that is `tests/unit/shared/test_decimal_utils.py`).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

from decimal_utils import convert_decimals_to_float


def _numeric_leaves(obj: Any, path: str = "$") -> list[tuple[str, Any]]:
    """Every non-``bool`` numeric leaf in ``obj``, with the path that reached it.

    ``bool`` is excluded because it subclasses ``int`` and is a distinct claim —
    see ``test_booleans_stay_booleans_across_the_boundary``.
    """
    if isinstance(obj, bool):
        return []
    if isinstance(obj, Decimal | int | float):
        return [(path, obj)]
    if isinstance(obj, dict):
        found: list[tuple[str, Any]] = []
        for key, value in obj.items():
            found.extend(_numeric_leaves(value, f"{path}.{key}"))
        return found
    if isinstance(obj, list):
        found = []
        for index, item in enumerate(obj):
            found.extend(_numeric_leaves(item, f"{path}[{index}]"))
        return found
    return []


def _boolean_leaves(obj: Any, path: str = "$") -> list[tuple[str, bool]]:
    """Every ``bool`` leaf, with the path that reached it."""
    if isinstance(obj, bool):
        return [(path, obj)]
    if isinstance(obj, dict):
        found: list[tuple[str, bool]] = []
        for key, value in obj.items():
            found.extend(_boolean_leaves(value, f"{path}.{key}"))
        return found
    if isinstance(obj, list):
        found = []
        for index, item in enumerate(obj):
            found.extend(_boolean_leaves(item, f"{path}[{index}]"))
        return found
    return []


def test_every_numeric_leaf_is_a_real_decimal(schema_mapping_decimal_state_row):
    """No ``float`` and no bare ``int`` anywhere in the row.

    This is the shape a DynamoDB read actually produces, and the whole point of
    the fixture (design O2). A single ``float`` would make the C1 regression test
    pass against an input the read path cannot generate.
    """
    leaves = _numeric_leaves(schema_mapping_decimal_state_row)
    assert leaves, "the fixture carries no numerics at all, so it cannot exercise the Decimal boundary"

    wrong = [(path, value, type(value).__name__) for path, value in leaves if not isinstance(value, Decimal)]
    assert wrong == [], (
        "design O2: every numeric in this fixture must be a real decimal.Decimal, because boto3's "
        "resource deserializer returns every DynamoDB N attribute as Decimal regardless of what was "
        f"written. A float or int here would pass while the real path raises. Offenders: {wrong}"
    )


def test_the_raw_fixture_is_not_json_serializable(schema_mapping_decimal_state_row):
    """The negative control: ``json.dumps`` on the unconverted row raises.

    This is the assertion that fails the moment the fixture's ``Decimal``s are
    downgraded to floats — which is exactly the mistake design O2 warns about,
    since a float fixture is silently useless rather than loudly wrong.

    ``json.dumps`` has no encoder for ``Decimal`` and ``approval_handler`` passes
    no ``default=`` (``LGR-86698d4-1`` rejected one), so this raise is the
    condition the boundary converter exists to remove.
    """
    try:
        json.dumps(schema_mapping_decimal_state_row)
    except TypeError as exc:
        assert "Decimal" in str(exc), f"expected the Decimal encoder failure, got a different TypeError: {exc}"
    else:
        raise AssertionError(
            "design O2: json.dumps succeeded on the raw fixture, which means its numerics are no "
            "longer Decimal. A float fixture serializes cleanly and proves nothing about the path "
            "it stands in for — restore real decimal.Decimal values."
        )


def test_the_boundary_converter_makes_the_payload_serializable(schema_mapping_decimal_state_row):
    """The positive half: the shared converter, then ``json.dumps``, round-trips.

    Mirrors the boundary at ``approval_handler/lambda_function.py:551`` —
    ``json.dumps(convert_decimals_to_float(approval_data))`` — applied to the
    payload C1 will read. Every numeric survives as a JSON number with its value
    intact.
    """
    row = schema_mapping_decimal_state_row
    payload = row["output_data"]["approval"]

    serialized = json.dumps(convert_decimals_to_float(payload))
    parsed = json.loads(serialized)

    before = dict(_numeric_leaves(payload))
    after = dict(_numeric_leaves(parsed))
    assert set(before) == set(after), (
        f"the converter changed the payload's numeric shape: "
        f"lost={sorted(set(before) - set(after))} gained={sorted(set(after) - set(before))}"
    )
    for path, original in before.items():
        converted = after[path]
        assert isinstance(converted, int | float) and not isinstance(converted, bool), (
            f"{path}: a DynamoDB numeric must serialize as a JSON number, got {converted!r}"
        )
        assert float(converted) == float(original), f"{path}: value changed, {original} -> {converted}"


def test_booleans_stay_booleans_across_the_boundary(schema_mapping_decimal_state_row):
    """``approval_required`` is a ``bool`` and must not widen to ``1`` or ``1.0``.

    ``bool`` subclasses ``int``, and ``shared/decimal_utils.py``'s docstring
    records that it is deliberately NOT converted. A fixture whose booleans had
    drifted into numerics would let a converter bug through unnoticed.
    """
    row = schema_mapping_decimal_state_row
    assert row["approval_required"] is True
    assert row["output_data"]["summary"]["approval_required"] is True
    assert row["output_data"]["approval"]["requires_review"] is True

    booleans = _boolean_leaves(row)
    assert booleans, "the fixture carries no booleans, so the bool-preservation claim is vacuous"

    serialized = json.dumps(convert_decimals_to_float(row["output_data"]["approval"]))
    assert '"requires_review": true' in serialized, f"requires_review did not serialize as a JSON bool: {serialized!r}"
    for widened in ('"requires_review": 1', '"requires_review": 1.0'):
        assert widened not in serialized, f"a bool was widened to a number: found {widened!r}"


def test_the_documented_live_mapping_is_present_at_its_observed_confidence(
    schema_mapping_decimal_state_row,
):
    """``signup_date`` → ``registration_date`` at ``0.96``, as measured.

    The one value in this fixture that came off the live record
    (``requirements.md`` Symptom 1, run ``run-2026-08-24-13-48-26-4fccdf03``).
    It is the regression target C1 asserts against, and ``confidence_scores`` is
    where R1.6 expects the ``Decimal`` to sit.
    """
    row = schema_mapping_decimal_state_row
    assert row["run_id"] == SCHEMA_MAPPING_DECIMAL_RUN_ID

    payload = row["output_data"]["approval"]
    mappings = payload["proposed_mappings"]
    assert mappings, "proposed_mappings must be non-null and non-empty — that is the whole defect"

    documented = [
        m
        for m in mappings
        if m["source_field"] == SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD
        and m["target_field"] == SCHEMA_MAPPING_DECIMAL_TARGET_FIELD
    ]
    assert len(documented) == 1, (
        f"expected exactly one {SCHEMA_MAPPING_DECIMAL_SOURCE_FIELD} -> "
        f"{SCHEMA_MAPPING_DECIMAL_TARGET_FIELD} mapping, found {len(documented)}"
    )
    assert documented[0]["confidence"] == SCHEMA_MAPPING_DECIMAL_CONFIDENCE
    assert isinstance(documented[0]["confidence"], Decimal)

    scores = payload["confidence_scores"]
    assert scores[SCHEMA_MAPPING_DECIMAL_TARGET_FIELD] == SCHEMA_MAPPING_DECIMAL_CONFIDENCE
    assert all(isinstance(value, Decimal) for value in scores.values()), (
        f"confidence_scores is R1.6's named Decimal carrier; every value must be a Decimal: {scores}"
    )
    assert set(scores) == {m["target_field"] for m in mappings}, (
        "the producer keys confidence_scores by target field, one per mapping "
        "(schema_mapping/lambda_function.py:1705-1707)"
    )


def test_seven_of_the_nine_keys_sit_under_output_data_approval(schema_mapping_decimal_state_row):
    """Where C1 must point, and the two keys it must leave at the top level.

    ``create_approval_payload`` (``schema_mapping/lambda_function.py:1734-1780``)
    supplies seven of the nine keys the approval branch reads. ``evidence`` and
    ``questions`` are **not** in it: their producer is the complex-mapping
    agent's top-level ``update_item`` at ``:2277-2291``, so the existing
    top-level read is correct for those two and only wrong for the other seven.
    They were ``None`` on the observed run because that agent did not run.

    **The count in this test's NAME is p50's and is left alone deliberately.** p55
    R10.1 added an eighth key under ``output_data.approval`` —
    ``rename_manifest`` — so the arithmetic is now eight-of-eleven, and the name
    reads as stale. It stays because the name identifies **which decision** this
    test guards (p50's C1 address move, and the two keys C1 had to leave alone),
    not how many keys existed on the day it was written. The counts that must stay
    true are in the tuples, which is where they are asserted; renaming the test
    every time the payload grows would churn the one stable handle a reader has on
    it. The tuple-agreement assertion below is what actually fails when a key gains
    or loses an address.
    """
    payload = schema_mapping_decimal_state_row["output_data"]["approval"]

    missing = [key for key in SCHEMA_MAPPING_APPROVAL_KEYS_UNDER_OUTPUT_DATA if payload.get(key) is None]
    assert missing == [], f"output_data.approval must supply these: {missing}"

    for key in ("evidence", "questions"):
        assert key not in payload, (
            f"{key} is not part of create_approval_payload's output — its writer is the "
            f"complex-mapping agent's top-level update_item (schema_mapping/lambda_function.py:"
            f"2277-2291). Adding it here would hide that the top-level read is correct for it."
        )

    # p50 C2 — AMENDED, not loosened. This equality used to close over nine keys:
    # the seven under `output_data.approval` plus the two at the row's top level.
    # C2 added a tenth, `mapping_count`, which has NO address — it is derived from
    # `proposed_mappings` (`approval_handler/lambda_function.py`'s schema_mapping
    # branch), so it belongs in neither address-keyed tuple. The derived set is
    # named explicitly rather than the equality being weakened to a subset check,
    # because the whole value of this assertion is that it fails when a key gains
    # or loses an address without anyone saying so.
    assert set(SCHEMA_MAPPING_APPROVAL_KEYS_UNDER_OUTPUT_DATA) | {"evidence", "questions"} | set(
        SCHEMA_MAPPING_APPROVAL_KEYS_DERIVED
    ) == set(SCHEMA_MAPPING_APPROVAL_KEYS), "the key tuples have drifted apart"

    assert set(SCHEMA_MAPPING_APPROVAL_KEYS_DERIVED).isdisjoint(
        set(SCHEMA_MAPPING_APPROVAL_KEYS_UNDER_OUTPUT_DATA) | {"evidence", "questions"}
    ), "a derived key must not also be claimed as a read address — that is the second authority C2 declined"


def test_the_complex_agent_attributes_carry_decimals_at_the_top_level():
    """The ``evidence`` / ``questions`` companion constant is ``Decimal``-bearing too.

    Composed into the row when the two top-level reads are in scope. Held to the
    same standard as the row itself: its producer also writes through
    ``convert_floats_to_decimal`` (``:2283``, ``:2286``), so its numerics come
    back as ``Decimal``.
    """
    leaves = _numeric_leaves(SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES)
    assert leaves, "the complex-agent attributes carry no numerics, so they add nothing to the boundary test"
    wrong = [(path, type(value).__name__) for path, value in leaves if not isinstance(value, Decimal)]
    assert wrong == [], f"every numeric here must be a Decimal for the same reason as the row: {wrong}"

    composed = {**SCHEMA_MAPPING_DECIMAL_STATE_ROW, **SCHEMA_MAPPING_COMPLEX_AGENT_ATTRIBUTES}
    assert composed["evidence"] is not None
    assert composed["questions"]


def test_the_fixture_is_a_fresh_deep_copy_per_test(schema_mapping_decimal_state_row):
    """Mutating the fixture cannot leak into the module constant or another test.

    The row is nested four levels deep and tests will want to seed degraded
    shapes off it (``output_data`` absent, ``approval`` not a dict — the
    error-handling table in ``design.md``). A shallow copy would let one of those
    edits corrupt the populated case.
    """
    row = schema_mapping_decimal_state_row
    assert row is not SCHEMA_MAPPING_DECIMAL_STATE_ROW
    assert row["output_data"] is not SCHEMA_MAPPING_DECIMAL_STATE_ROW["output_data"]
    assert row["output_data"]["approval"] is not SCHEMA_MAPPING_DECIMAL_STATE_ROW["output_data"]["approval"]

    row["output_data"]["approval"]["confidence_scores"]["customer_id"] = Decimal("0.01")
    row["output_data"]["approval"]["proposed_mappings"].clear()

    assert SCHEMA_MAPPING_DECIMAL_STATE_ROW["output_data"]["approval"]["confidence_scores"]["customer_id"] == Decimal(
        "0.99"
    )
    assert len(SCHEMA_MAPPING_DECIMAL_STATE_ROW["output_data"]["approval"]["proposed_mappings"]) == 4


def test_the_payload_only_fixture_is_the_rows_own_subtree(
    schema_mapping_decimal_approval_payload, schema_mapping_decimal_state_row
):
    """The convenience fixture and the row's sub-tree are equal, not shared.

    Equal so a test can use either without a second source of truth about the
    payload; not shared so mutating one cannot affect the other.
    """
    payload = schema_mapping_decimal_approval_payload
    from_row = schema_mapping_decimal_state_row["output_data"]["approval"]

    assert payload == from_row
    assert payload is not from_row
