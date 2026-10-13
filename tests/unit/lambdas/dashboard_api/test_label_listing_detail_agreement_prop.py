# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 2 — the listing and the detail never disagree about a label.

# Feature: p36-schema-naming, Property 2: The listing and the detail never disagree about a label

For any generated world and any Uploaded_Schema in it, the ``displayLabel``
``GET /api/schemas`` reports for that schema and the ``displayLabel``
``GET /api/schemas/{id}`` reports are **equal, including agreeing on absence**.

**Expected RED until tasks 2.1 + 2.3.** The expectations come from Requirement
1.3, **not** from current output: `_collect_schema_rows`' uploaded block emits no
label at all (`schemas.py:566-604`) while `_schema_detail:1115-1117` is the one
read of the sidecar's `display_name` on the read path, so today the two surfaces
disagree about **every** labelled schema. A failure here is the defect, not a
broken test. No production code is touched by this file.

**Why this is not Property 1 restated, said here so a later reader does not
delete it as redundant.** Property 1 (task 1.3) judges one surface against the
requirements; this one judges two surfaces against **each other**. Two
independently written, independently *correct-looking* readers can still disagree
on the `""` case — one gating on truthiness and one on `is not None` both look
right in isolation — and that disagreement is invisible to a property that only
ever looks at the listing. Design §4.1 makes 1.3 true **by construction** by
routing both surfaces through one `_display_label_from_sidecar` whose gate is
truthy; this file is the check that the single shared gate is what actually
shipped. Design §"What the reflection removed" states the same thing from the
other direction: "Property 1 does not imply Property 2".

**The two cases that make this real, and they are why the file exists.**

- A sidecar holding ``""``. `_schema_detail`'s gate is truthy
  (`schemas.py:1115-1116`, `if display_label:`), so ``""`` is **already** absent
  from the detail response. A listing implemented with `is not None` would emit
  ``""`` where the detail emits nothing — two surfaces disagreeing about one
  schema, on exactly the legacy data the truthy gate exists for
  (`LGR-44d601e-1`, **template / ratified**, settled that clearing a label
  removes the key and that storing ``""`` was rejected — but an object written
  before that rule can hold it).
- A sidecar holding **whitespace only**. Truthy, so it must appear **verbatim on
  both** surfaces. That is Requirement 2.4's rule read from the storage side: the
  gate trims and the value does not.

Both are branches of `conftest.DISPLAY_LABELS`, and both also have a dedicated
example test below so a failure names the case rather than a shrunk world.

**The vacuity guard, and why it is not Property 1 leaking in.** "Both surfaces
report no label" satisfies equality trivially — including when the key this test
looks for is not the key either surface emits. So where the world says a label
must surface, the agreed value is additionally checked against the world's own
expectation (`LabelWorld.expected_labels`, written from Requirements 1.2 and 1.3
rather than imported from the code under test). Where no label is expected, only
equality is asserted: if some future change made **both** surfaces emit ``""``,
Requirement 1.3 would still hold and Property 1 is what would fail. Each property
keeps its own subject.

**The Display_Label wire key is derived, never transcribed** (Requirement 4.7,
`LGR-28b4d75-5`, **template / ratified**). It is read at test time from
`normalizeSchema` in `dashboard-react/src/hooks/use-schemas.ts` — the
**consumer's** single definition of the key it expects on the wire. Taking it
from the producer would make the producer its own oracle, which is the failure
mode that record is about and item 1's defect one layer up.

**Fixtures.** Everything comes from task 1.2's shared infrastructure in this
directory's `conftest.py`: `label_worlds()`, `seed_label_world()`,
`label_world_env` and the address helpers. The sidecar's address is taken from
`_context_sidecar_key`'s one definition through `conftest.sidecar_key`
(`LGR-cad8032-1`) and is not re-spelled here. There is no `moto.mock_aws()` and
no boto3 client mock in this file
(`tests/unit/test_no_duplicate_mocking_prop.py`), and it does **not** set
`AWS_REGION` (`LGR-3e45e65-1`, **template / ratified**).

**Validates: Requirements 1.3, 4.2**
"""

import json
import os
import re
from typing import Any

import pytest
from hypothesis import HealthCheck, Phase, event, given, settings

# `label_world_env` is deliberately **not** imported: it is a fixture this
# directory's `conftest.py` already registers, and importing a fixture function
# into a test module registers a second copy of it. Requesting it by name in a
# signature is how the four existing users of this shape reach their own.
from tests.unit.lambdas.dashboard_api.conftest import (
    LabelWorld,
    label_worlds,
    seed_label_world,
    sidecar_field_names,
    sidecar_key,
)

# ---------------------------------------------------------------------------
# The wire key, derived from the consumer that reads it
# ---------------------------------------------------------------------------
_USE_SCHEMAS_TS = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "dashboard-react",
    "src",
    "hooks",
    "use-schemas.ts",
)

#: `normalizeSchema`'s own line: `display_label: (raw.displayLabel as string) ?? …`
#: (`use-schemas.ts:56`). The camelCase name it reads **first** is the wire key
#: the API is expected to emit; `display_label` is the tolerated older spelling
#: and deliberately not what this test asserts on.
_WIRE_KEY_PATTERN = re.compile(r"display_label:\s*\(raw\.(\w+) as string\)")


def _display_label_wire_key() -> str:
    """The Display_Label key on the wire, read from the frontend at test time.

    Requirement 4.7. A `"displayLabel"` literal in the assertion below could not
    detect the producer drifting, because the producer's output would be its own
    oracle. Reading it from the consumer means a producer that renamed the key
    fails this test instead of silently handing the UI a shape its type does not
    declare and rendering nothing with no error.
    """
    with open(os.path.abspath(_USE_SCHEMAS_TS), encoding="utf-8") as handle:
        source = handle.read()
    match = _WIRE_KEY_PATTERN.search(source)
    assert match, (
        "could not read the Display_Label wire key from normalizeSchema in "
        f"{os.path.relpath(os.path.abspath(_USE_SCHEMAS_TS))}. Requirement 4.7 requires this "
        "test to derive the key rather than transcribe it, so a shape change there must be "
        "reflected here deliberately rather than papered over with a literal."
    )
    return match.group(1)


# ---------------------------------------------------------------------------
# Reading one label off each surface
# ---------------------------------------------------------------------------
class _Absent:
    """The absence of the key, as a value the equality can compare.

    Requirement 1.2 makes absence and `None` different answers, so absence cannot
    be represented by `None` here without collapsing the very distinction
    Requirement 1.3 is stated over.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "<no displayLabel key>"


ABSENT = _Absent()


def _label_of(document: dict[str, Any], wire_key: str) -> Any:
    """The Display_Label a response document reports, or `ABSENT`."""
    return document.get(wire_key, ABSENT)


def _listing_rows() -> list[dict[str, Any]]:
    """`GET /api/schemas`' rows, through the real handler."""
    from handlers.schemas import get_schemas

    response = get_schemas()
    assert response["statusCode"] == 200, f"expected 200 from get_schemas, got {response}"
    return json.loads(response["body"])["schemas"]


def _detail_of(schema_id: str) -> dict[str, Any]:
    """`GET /api/schemas/{id}`' bare schema object, through the real handler."""
    from handlers.schemas import get_schema

    response = get_schema(schema_id)
    assert response["statusCode"] == 200, (
        f"expected 200 from get_schema({schema_id!r}); the world seeded a body for it, so a "
        f"404 or 500 here is a harness fault rather than the property failing — got {response}"
    )
    return json.loads(response["body"])


def _row_for(rows: list[dict[str, Any]], schema_id: str) -> dict[str, Any]:
    row = next((r for r in rows if r.get("schemaId") == schema_id), None)
    assert row is not None, (
        f"the listing carries no row for {schema_id!r}, which the world seeded a "
        f"`shared/{schema_id}.json` body for. Requirement 1.3 is about two surfaces "
        f"agreeing; a missing row is Requirement 1.12's subject and a harness fault here. "
        f"ids present: {sorted(r.get('schemaId', '') for r in rows)}"
    )
    return row


def _assert_surfaces_agree(world: LabelWorld, wire_key: str) -> None:
    """The property itself, over one already-seeded world."""
    rows = _listing_rows()
    expected = world.expected_labels

    for schema_id in world.schema_ids:
        listing_label = _label_of(_row_for(rows, schema_id), wire_key)
        detail_label = _label_of(_detail_of(schema_id), wire_key)

        assert listing_label == detail_label, (
            f"the two surfaces disagree about {schema_id!r}:\n"
            f"  GET /api/schemas      -> {listing_label!r}\n"
            f"  GET /api/schemas/{{id}} -> {detail_label!r}\n"
            f"  sidecar stored: {world.sidecars.get(schema_id, '<none>')!r}\n"
            f"Requirement 1.3 says these are equal, absence included. Design §4.1 makes that "
            f"true by construction with one `_display_label_from_sidecar` whose gate is "
            f"**truthy**; a listing gating on `is not None` emits '' where the detail "
            f"(`schemas.py:1115-1116`) emits nothing."
        )

        # The vacuity guard — see the module docstring. Both surfaces agreeing on
        # `ABSENT` for a schema that *has* a stored label is agreement without
        # correctness, and is also what a wrong wire key would look like.
        if schema_id in expected:
            assert listing_label == expected[schema_id], (
                f"both surfaces agree about {schema_id!r} but on the wrong value: they report "
                f"{listing_label!r} where the stored Display_Label is {expected[schema_id]!r}. "
                f"Requirements 1.1 and 1.3 together: the agreed value must be the stored one, "
                f"verbatim and untrimmed."
            )


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------
@given(world=label_worlds(min_schemas=1, max_schemas=4))
@settings(
    max_examples=100,
    # Every example wipes and reseeds a moto bucket and then runs the real
    # `get_schemas` plus one real `get_schema` per schema. That routinely exceeds
    # Hypothesis's 200 ms default, and this property asserts agreement rather
    # than latency — Requirement 1.9's bound is task 2.7's, with a stub.
    deadline=None,
    # One fixture serves every example by design; `seed_label_world` wipes the
    # bucket at the start of each one, so nothing an earlier example wrote is
    # visible later.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` excluded: it re-runs hundreds of further examples to
    # annotate a failure whose cause the assertion message already states,
    # including the stored sidecar. Shrinking is kept.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_the_listing_and_the_detail_never_disagree_about_a_label(world, label_world_env):
    """Property 2, over one generated world.

    **Feature: p36-schema-naming, Property 2: The listing and the detail never
    disagree about a label**

    **Validates: Requirements 1.3, 4.2**
    """
    _, display_field = sidecar_field_names()
    stored = [document.get(display_field) for document in world.sidecars.values()]
    event(f"{len(world.schema_ids)} uploaded schema(s)")
    event(f"{len(world.sidecars)} annotated")
    if any(value == "" for value in stored):
        event("a sidecar holding '' — the case two independently written gates diverge on")
    if any(isinstance(value, str) and value != "" and value.strip() == "" for value in stored):
        event("a sidecar holding whitespace only — truthy, so verbatim on both surfaces")

    seed_label_world(world)
    _assert_surfaces_agree(world, _display_label_wire_key())


# ---------------------------------------------------------------------------
# The two mandated cases, as examples, so a failure names the case
# ---------------------------------------------------------------------------
def _world_with_label(schema_id: str, label: str) -> LabelWorld:
    """One Uploaded_Schema whose Context_Sidecar holds exactly `label`.

    The sidecar's field name comes from `sidecar_field_names()` — the handler's
    own `_SIDECAR_DISPLAY_NAME` (`schemas.py:2190-2191`) — for the same reason the
    address does: a second spelling of either would seed a world the handler reads
    as unannotated, and the case would pass while proving nothing
    (`LGR-cad8032-1`).
    """
    _, display_field = sidecar_field_names()
    return LabelWorld(schema_ids=(schema_id,), sidecars={schema_id: {display_field: label}})


def test_a_sidecar_holding_an_empty_string_is_absent_from_both_surfaces(label_world_env):
    """The `""` case, named: the gate is truthy on both surfaces or 1.3 breaks.

    `_schema_detail` already treats `""` as absent (`schemas.py:1115-1116`), so a
    listing written with `is not None` emits `""` where the detail emits nothing.
    This is the disagreement `LGR-44d601e-1`'s legacy data makes reachable and the
    reason design §4.1 requires the shared gate to stay **truthy**.

    Both halves are asserted here rather than only equality, because for this one
    value the correct agreed answer is known: absence on both surfaces.

    **Validates: Requirements 1.3, 4.2**
    """
    wire_key = _display_label_wire_key()
    world = seed_label_world(_world_with_label("uploaded_schema_1700000000000", ""))
    schema_id = world.schema_ids[0]

    _assert_surfaces_agree(world, wire_key)

    row = _row_for(_listing_rows(), schema_id)
    detail = _detail_of(schema_id)
    assert wire_key not in row, (
        f"a sidecar holding '' must not put {wire_key!r} on the listing row — the detail "
        f"omits it, so emitting it here is Requirement 1.3 broken on legacy data. row: {row}"
    )
    assert wire_key not in detail, (
        f"a sidecar holding '' must not put {wire_key!r} on the detail response; this is "
        f"`schemas.py:1115-1116`'s existing behaviour and the reason the listing's gate must "
        f"be truthy rather than `is not None`. detail: {detail}"
    )


@pytest.mark.parametrize(
    "label",
    [" ", "   ", "\t", "\n", " \t\n ", "\u00a0", "\u3000"],
    ids=["one-space", "spaces", "tab", "newline", "mixed", "nbsp", "ideographic"],
)
def test_a_whitespace_only_sidecar_label_appears_verbatim_on_both_surfaces(label, label_world_env):
    """The whitespace-only case, named: truthy, so carried verbatim by both.

    A whitespace-only stored label is truthy, so it is a stored Display_Label and
    both surfaces must report it **with its characters intact**. Requirement 2.4's
    rule read from the storage side: the gate trims and the value does not. A
    listing that gated on `.strip()` would omit the key where the detail emits the
    string, which is the same disagreement as the `""` case with the sides
    swapped.

    The non-breaking and ideographic spaces are included because Python's
    `str.strip()` treats them as whitespace while a hand-rolled `!= " "` check
    does not.

    **Validates: Requirements 1.3, 4.2**
    """
    wire_key = _display_label_wire_key()
    world = seed_label_world(_world_with_label("uploaded_schema_1700000000001", label))
    schema_id = world.schema_ids[0]

    _assert_surfaces_agree(world, wire_key)

    assert _label_of(_row_for(_listing_rows(), schema_id), wire_key) == label, (
        f"a whitespace-only stored label is truthy and must reach the listing verbatim: expected {label!r}"
    )
    assert _label_of(_detail_of(schema_id), wire_key) == label, (
        f"a whitespace-only stored label is truthy and must reach the detail verbatim: expected {label!r}"
    )


# ---------------------------------------------------------------------------
# The harness's own two claims
# ---------------------------------------------------------------------------
def test_the_wire_key_is_read_from_the_frontend_and_is_camel_case():
    """Requirement 4.7's derivation works and yields a plausible wire key.

    Without this, a regex that stopped matching would surface as every assertion
    above looking for a key nothing emits — which reads as the feature being
    broken rather than as the harness having drifted.
    """
    wire_key = _display_label_wire_key()
    assert wire_key and wire_key != "display_label", (
        f"normalizeSchema's first-choice key is {wire_key!r}; the wire key is expected to be "
        f"the camelCase spelling, with `display_label` as the tolerated older one"
    )


def test_the_sidecar_address_comes_from_the_handler(label_world_env):
    """The world is seeded at the address the handler reads, not a second one.

    A world seeded at a second spelling of the Context_Sidecar's key would be read
    as unannotated by both surfaces, and Property 2 would pass on every example
    while proving nothing (`LGR-cad8032-1`).
    """
    _, display_field = sidecar_field_names()
    schema_id = "uploaded_schema_1700000000002"
    seed_label_world(_world_with_label(schema_id, "Customer Master"))

    from handlers.schemas import _read_context_sidecar

    assert sidecar_key(schema_id).endswith("-metadata.json")
    assert _read_context_sidecar(schema_id).get(display_field) == "Customer Master", (
        "the handler's own reader must find the seeded sidecar; if it does not, every "
        "assertion in this file is comparing two absences"
    )
