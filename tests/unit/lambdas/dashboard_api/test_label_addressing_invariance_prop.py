# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 5 — adding Display_Labels perturbs no addressing field.

# Feature: p36-schema-naming, Property 5: Adding labels perturbs no addressing field

For *any* generated world, running the listing with every Context_Sidecar removed
and running it with the sidecars present produces rows whose ``schemaId`` and
``name`` are identical per schema, and whose only key difference is the
Display_Label key.

**Why this test exists, and why it is written before the producer changes.** The
failure it guards is silent and lands six steps from its cause. R4.11 and
``LGR-acca05d-2`` (**template / ratified**) forbid a Display_Label participating
in addressing. A Schema_Listing_Row whose ``schemaId`` became the label would put
that label into ``target_schema_name`` (``handlers/pipelines_crud.py:837``), where
``schema_mapping``'s by-name fallback interpolates it into six candidate object
keys (``schema_mapping/lambda_function.py:249-250`` → ``:296`` →
``shared/target_schema_resolution.py:407``) and raises ``Could not find uploaded
schema`` at ``:317-319``. There is **no other symptom**: no 4xx on the listing, no
warning on the page, nothing naming a label. A pipeline simply fails to resolve
its target schema, in a different Lambda, on a later request.

So this file's job is to be in place **before task 2.3 lands**, so that a
regression is *caught* rather than inferred from a pipeline failure with no
mention of a name.

**It is expected to pass today, and that is not a defect in the test.** Nothing
adds a Display_Label to a listing row yet (``_collect_schema_rows``' uploaded block
emits ``schemaId`` and ``name``, both the id, and no label — ``schemas.py:556-604``),
so today the two runs produce *identical* rows and the key difference is empty.
An empty difference is a subset of the permitted one, so the property holds
vacuously. The vacuity is made **visible** rather than hidden: every example
reports, through Hypothesis ``event()``, whether the labelled run produced a label
key at all and how many per-object sidecar reads it performed. Once task 2.3
lands, those counters become non-zero and the same assertions start doing work.

**The two worlds, and which of the two permitted constructions this file uses.**
The sidecars-removed run seeds a world with an **empty ``sidecars`` map** — the
first of the two options task 1.6 allows — rather than seeding and then deleting.
Two reasons: it is entirely task 1.2's fixture (``seed_label_world`` wipes the
bucket before writing, so no residue crosses between the runs and nothing here
re-spells the Context_Sidecar's address, ``LGR-cad8032-1``), and the removed run
therefore comes **first**, so no label an earlier call resolved could leak into
the baseline.

The cost of that choice, stated rather than hidden: each run writes the schema
bodies afresh, so ``lastModified`` — which the listing takes from the object's S3
``LastModified`` (``schemas.py:575``) — differs between the two runs. This
property therefore compares the two **addressing** fields R1.12 names, plus the
row's key *set*, and does not assert whole-row value identity. That is the
property as specified; a whole-row comparison would be asserting a fixture
artefact.

**The Display_Label key is derived, never transcribed** (Requirement 4.7,
``LGR-28b4d75-5``, **template / ratified**). It is read at test time out of the
frontend consumer that declares which wire spellings *are* the Display_Label —
``normalizeSchema``'s ``display_label:`` line in
``dashboard-react/src/hooks/use-schemas.ts`` — so a producer that starts emitting
some third spelling is a key difference this property **rejects** rather than
silently accepts. A test that literalled ``"displayLabel"`` in the permitted-key
set would be taking its oracle from the producer's output, which is item 1's own
defect one layer up.

Shared fixtures only: ``label_world_env`` composes ``mock_s3_buckets`` and
``mock_lambda_env`` from ``tests/fixtures/conftest.py``. There is no
``moto.mock_aws()`` call and no boto3 client mock in this file
(``tests/unit/test_no_duplicate_mocking_prop.py``), and it does **not** set
``AWS_REGION`` (``LGR-3e45e65-1``, **template / ratified**) — the seeding client
resolves the region through the same call-time expression the handler uses.

**Validates: Requirements 1.12**
"""

import json
import os
import re
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from tests.unit.lambdas.dashboard_api.conftest import (
    LabelWorld,
    label_worlds,
    read_keys,
    recorded_s3_calls,
    seed_label_world,
    sidecar_field_names,
)

# ---------------------------------------------------------------------------
# The permitted key difference — derived from the consumer, not the producer
# ---------------------------------------------------------------------------
#: The frontend module that declares which wire spellings mean "Display_Label".
#: `normalizeSchema` (`use-schemas.ts:52`) is the single place a listing row is
#: turned into a `Schema`, and its `display_label:` line is the one definition of
#: which raw keys carry the label.
_USE_SCHEMAS_TS = os.path.normpath(
    os.path.join(
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
)

#: The two fields R1.12 keeps: the schema id, and the source-derived name.
ADDRESSING_KEYS = ("schemaId", "name")


def consumer_display_label_keys() -> frozenset[str]:
    """Every wire key the frontend accepts as a Display_Label, read at test time.

    Parsed out of ``normalizeSchema``'s ``display_label:`` line rather than
    transcribed, per Requirement 4.7. Today that line is
    ``(raw.displayLabel as string) ?? (raw.display_label as string) ?? undefined``,
    so this returns ``{"displayLabel", "display_label"}`` — and if the producer
    grows a third spelling the consumer does not read, this property fails on the
    unexpected key instead of blessing it.

    Fails loudly rather than returning an empty set: an empty permitted set would
    make the property *stricter* by accident and a moved file would then look like
    a producer bug.
    """
    with open(_USE_SCHEMAS_TS, encoding="utf-8") as handle:
        source = handle.read()

    match = re.search(r"^\s*display_label:(?P<expr>.*)$", source, re.MULTILINE)
    assert match is not None, (
        f"no `display_label:` assignment found in {_USE_SCHEMAS_TS} — "
        f"normalizeSchema has moved or been renamed, and this property can no longer "
        f"derive the Display_Label wire key from the consumer (Requirement 4.7)"
    )

    keys = frozenset(re.findall(r"raw\.([A-Za-z_][A-Za-z0-9_]*)", match.group("expr")))
    assert keys, f"`display_label:` in {_USE_SCHEMAS_TS} names no `raw.<key>` spelling: {match.group('expr').strip()!r}"
    assert not keys.intersection(ADDRESSING_KEYS), (
        f"the derived Display_Label keys {sorted(keys)} include an addressing field "
        f"{sorted(set(keys).intersection(ADDRESSING_KEYS))} — the derivation has picked up the "
        f"wrong line and this property would permit a label landing in an address"
    )
    return keys


DISPLAY_LABEL_KEYS = consumer_display_label_keys()


# ---------------------------------------------------------------------------
# Running the listing
# ---------------------------------------------------------------------------
def uploaded_rows_by_id(expected_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Run ``GET /api/schemas`` and index its Uploaded_Schema rows by ``schemaId``.

    Imports the handler here rather than at module scope: ``handlers/common.py:29-37``
    calls ``require_env`` for nine table names at **import** time, so the import has
    to happen after ``mock_lambda_env`` — which is exactly the reason task 1.2's
    conftest keeps its own handler access lazy.

    Asserts the row set is precisely the world's Uploaded_Schemas before returning,
    because "identical per schema" is only a meaningful comparison once both runs
    are known to describe the same schemas.
    """
    from handlers.schemas import get_schemas

    response = get_schemas()
    assert response["statusCode"] == 200, f"expected 200 from get_schemas, got {response}"
    body = json.loads(response["body"])

    rows = {row["schemaId"]: row for row in body["schemas"] if row.get("source") == "uploaded"}
    assert set(rows) == set(expected_ids), (
        f"the listing's uploaded rows {sorted(rows)} are not the seeded world "
        f"{sorted(expected_ids)}; sourcesFailed={body.get('sourcesFailed')}"
    )
    return rows


# ---------------------------------------------------------------------------
# The clause set — one definition, so the negative controls below judge the
# same oracle the property does
# ---------------------------------------------------------------------------
def assert_addressing_unperturbed(
    world: LabelWorld,
    baseline: dict[str, dict[str, Any]],
    labelled: dict[str, dict[str, Any]],
) -> set[str]:
    """Property 5's clauses over two runs of one world. Returns the keys added.

    Extracted so ``test_the_clause_set_rejects_a_label_that_reached_an_address``
    below can drive **this** function rather than a re-spelling of it. While the
    producer emits no label the property passes on empty differences, so the
    negative controls are what demonstrate the oracle bites — a green result today
    would otherwise be indistinguishable from a test that asserts nothing.
    """
    added_keys: set[str] = set()
    for schema_id in world.schema_ids:
        before, after = baseline[schema_id], labelled[schema_id]
        stored = world.expected_labels.get(schema_id)

        assert after["schemaId"] == before["schemaId"] == schema_id, (
            f"schemaId moved when the sidecars were present: {before['schemaId']!r} -> "
            f"{after['schemaId']!r} (the object key carries {schema_id!r}, and the stored "
            f"Display_Label is {stored!r}). R1.12: a label in schemaId becomes "
            f"target_schema_name and fails a pipeline six steps away with no other symptom"
        )
        assert after["name"] == before["name"], (
            f"the source-derived name moved when the sidecars were present: "
            f"{before['name']!r} -> {after['name']!r} for schema {schema_id!r} "
            f"(stored Display_Label {stored!r}). R1.12 keeps `name` source-derived"
        )

        removed = set(before) - set(after)
        assert not removed, (
            f"row for {schema_id!r} lost {sorted(removed)} when the sidecars were present; "
            f"resolving a Display_Label may only add the label key"
        )

        added = set(after) - set(before)
        assert added <= DISPLAY_LABEL_KEYS, (
            f"row for {schema_id!r} gained {sorted(added - DISPLAY_LABEL_KEYS)} when the "
            f"sidecars were present. The only key the sidecars may add is one the frontend "
            f"reads as the Display_Label ({sorted(DISPLAY_LABEL_KEYS)}, derived from "
            f"normalizeSchema); anything else is a key no consumer declared"
        )
        added_keys |= added
    return added_keys


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------
@given(world=label_worlds(min_schemas=1, max_schemas=5))
@settings(
    max_examples=100,
    # Each example wipes and reseeds a moto bucket twice and runs the listing
    # twice, which routinely exceeds Hypothesis's 200 ms default. The property
    # asserts field invariance, not latency.
    deadline=None,
    # One `label_world_env` serves every example by design; `seed_label_world`
    # wipes the bucket at the start of each run, so nothing an earlier example
    # wrote is visible to a later one.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_adding_labels_perturbs_no_addressing_field(world, label_world_env):
    """Property 5: adding labels perturbs no addressing field.

    Two runs of one world. First with every Context_Sidecar removed (an empty
    ``sidecars`` map), then with the sidecars present. Three clauses per schema:

    1. ``schemaId`` is unchanged and is still the id the object key carries.
    2. ``name`` is unchanged.
    3. No key disappears, and every key the labelled run *adds* is one the
       frontend recognises as the Display_Label — nothing else.

    Clauses 1 and 2 come first so a label that reached an address names itself in
    the failure, rather than being reported as an unexpected extra key.

    **Validates: Requirements 1.12**
    """
    seed_label_world(LabelWorld(schema_ids=world.schema_ids, sidecars={}))
    baseline = uploaded_rows_by_id(world.schema_ids)

    seed_label_world(world)
    with recorded_s3_calls() as calls:
        labelled = uploaded_rows_by_id(world.schema_ids)

    sidecar_reads = [key for key in read_keys(calls) if key.startswith("schemas/uploaded/")]
    added_keys = assert_addressing_unperturbed(world, baseline, labelled)

    # Visibility, not assertion. Until task 2.3 lands both counters are zero on
    # every example and this property holds vacuously — which is the expected
    # state, and is worth being able to see in the statistics rather than having
    # to infer from a green result.
    event(f"world has stored Display_Labels: {bool(world.expected_labels)}")
    event(f"labelled run added a Display_Label key: {bool(added_keys)}")
    event(f"labelled run performed a sidecar read: {bool(sidecar_reads)}")


# ---------------------------------------------------------------------------
# The derivation and the clause set are both load-bearing, so both are tested
# ---------------------------------------------------------------------------
def _labelled_world(schema_id: str, label: str) -> LabelWorld:
    """One world whose single schema carries ``label`` in its Context_Sidecar.

    The sidecar's field name comes from ``sidecar_field_names()`` rather than being
    spelled here (``LGR-cad8032-1`` — one definition), and the world is only ever
    used for the *expected* side of a comparison, so nothing is written to S3 from
    it.
    """
    _, display_name = sidecar_field_names()
    return LabelWorld(schema_ids=(schema_id,), sidecars={schema_id: {display_name: label}})


def test_the_display_label_key_derivation_reads_the_camelcase_wire_spelling():
    """The derived permitted-key set is the consumer's, and it is not empty.

    Requirement 4.7's whole point is that the permitted key comes from one
    definition. If this derivation silently returned an empty set or missed the
    camelCase spelling the producer will emit, Property 5 above would start
    rejecting the correct implementation — so the derivation gets its own check
    with the spelling written out **once**, here, where a mismatch reads as "the
    consumer changed" rather than as a property failure.
    """
    keys = consumer_display_label_keys()
    assert "displayLabel" in keys, (
        f"normalizeSchema no longer reads `raw.displayLabel`; derived {sorted(keys)}. "
        f"The listing's Display_Label wire key and the consumer have diverged"
    )
    assert not keys.intersection(ADDRESSING_KEYS)


@pytest.mark.parametrize(
    ("perturbation", "expected_message"),
    [
        ("schemaId", "schemaId moved"),
        ("name", "the source-derived name moved"),
        ("labelName", "gained"),
    ],
)
def test_the_clause_set_rejects_a_label_that_reached_an_address(perturbation, expected_message, label_world_env):
    """The oracle bites: three wrong post-passes, three failures.

    Property 5 passes vacuously until task 2.3 lands, so on its own a green result
    today does not distinguish "invariance holds" from "the comparison is empty".
    These three cases close that gap by driving the **same** clause set with rows
    a wrong implementation would produce:

    - ``schemaId`` becomes the Display_Label — the failure the docstring above
      traces to ``Could not find uploaded schema``.
    - ``name`` becomes the Display_Label — R1.12's other half. Harmless-looking,
      and ``PipelineFormModal`` hands a row straight into a pipeline write.
    - the label arrives under a key no consumer declared (``labelName``) — the
      shape Requirement 4.7 exists to catch, where the UI sees a key its type
      never declared and renders nothing with no error.

    The baseline rows are **real** listing rows from a real seeded world; only the
    second run's rows are perturbed, because a fabricated pair on both sides would
    be testing the perturbation rather than the clause.
    """
    schema_id = "uploaded_schema_1700000000000"
    seed_label_world(LabelWorld(schema_ids=(schema_id,), sidecars={}))
    baseline = uploaded_rows_by_id((schema_id,))

    labelled_world = _labelled_world(schema_id, "Customers")
    labelled = {row_id: dict(row) | {perturbation: "Customers"} for row_id, row in baseline.items()}

    assert assert_addressing_unperturbed(labelled_world, baseline, baseline) == set(), (
        "the unperturbed rows must satisfy the clause set, or this control proves nothing"
    )
    with pytest.raises(AssertionError, match=expected_message):
        assert_addressing_unperturbed(labelled_world, baseline, labelled)


def test_the_clause_set_permits_the_display_label_key_the_producer_will_add(label_world_env):
    """The oracle is not merely strict — it accepts the correct implementation.

    The mirror of the three controls above, and the reason they are not enough on
    their own: a clause set that rejected *every* difference would also fail once
    task 2.3 lands correctly, and would then look like a real finding.
    """
    schema_id = "uploaded_schema_1700000000001"
    seed_label_world(LabelWorld(schema_ids=(schema_id,), sidecars={}))
    baseline = uploaded_rows_by_id((schema_id,))

    labelled_world = _labelled_world(schema_id, "Customers")
    labelled = {
        row_id: dict(row) | {key: "Customers" for key in DISPLAY_LABEL_KEYS} for row_id, row in baseline.items()
    }

    assert assert_addressing_unperturbed(labelled_world, baseline, labelled) == set(DISPLAY_LABEL_KEYS)


@given(world=label_worlds(min_schemas=1, max_schemas=4))
@settings(max_examples=25, deadline=None)
def test_the_generated_world_reaches_both_sides_of_the_comparison(world):
    """The generator produces worlds with and without Context_Sidecars.

    Cheap guard on the input space rather than on the system: a generator that
    never produced an Annotated_Schema would make Property 5 compare a world with
    no sidecars against itself in **every** example, and it would pass forever —
    including after task 2.3 lands. ``label_worlds`` draws its annotated fraction
    from an explicit ``none``/``all``/``some`` choice for this reason; this test
    records which side each example landed on.
    """
    event(f"annotated fraction: {'none' if not world.annotated_ids else 'some or all'}")
    assert world.annotated_ids <= frozenset(world.schema_ids)
    assert frozenset(world.expected_labels) <= world.annotated_ids
    assert world.unlabelled_ids == frozenset(world.schema_ids) - frozenset(world.expected_labels)
