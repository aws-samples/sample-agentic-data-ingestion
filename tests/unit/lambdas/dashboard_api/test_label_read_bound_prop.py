# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 3 — the listing's per-object reads are bounded by the Annotated_Schemas.

# Feature: p36-schema-naming, Property 3: Reads are bounded by the Annotated_Schemas and never touch a schema outside them

For *any* number of Uploaded_Schemas and *any* annotated fraction of them, the
count of per-object sidecar reads ``GET /api/schemas`` performs is no greater than
the number of Annotated_Schemas, and no read targets the sidecar key of a schema
that has none.

**Three clauses, and the third one is why this file is worth having.** Clauses 1
and 2 are Requirement 1.8 read literally — a bound and an exclusion. Both of them
are satisfied by an implementation that performs **zero** reads, which is exactly
what the listing does today (`schemas.py:565-583`, read end to end: the uploaded
enumeration derives every field from the `list_objects_v2` page and carries the
verbatim fence **"DD4 — no `get_object` here."**). A file asserting only those two
clauses would go green on unfixed code and stay green if task 2.3's post-pass were
reverted, so it would prove nothing about the thing the bound is a bound *on*.

Clause 3 is the positive half: every Annotated_Schema's sidecar key **is** among
the read keys. With clause 1 it pins the read multiset exactly — no fewer than
every annotated sidecar (clause 3), no more than that count (clause 1), and
nothing outside the set (clause 2) — so neither doing nothing nor reading
everything passes.

**Clause 3 counts reads, not labels, and the generator is built to make the
difference visible.** An Annotated_Schema is an Uploaded_Schema for which a
Context_Sidecar object exists (requirements glossary, `requirements.md:76-78`) —
whether or not that sidecar holds a `display_name`. A sidecar carrying only
`field_contexts` still has to be **read** before anyone can know it holds no
label, so it is inside the bound and inside clause 3 while contributing no label
at all. `label_worlds()` draws that case deliberately
(`conftest.py`'s `if draw(st.booleans()): document[display_field] = …`), which is
what keeps this property from collapsing into Property 1 with extra steps.
Requirement 1.8's bound is on `annotated_ids`, never on `expected_labels`.

**The annotated fraction is varied including 0 and all, and both edges are pinned
as explicit examples rather than left to the draw.** 0 is the case where the
listing must perform **no** read whatever, and all is the bound's permitted
ceiling: Requirement 1.8 says "no greater than the number of Annotated_Schemas",
which permits ``|Annotated| == |Uploaded|``. That is the **sizing** case and it is
not in tension with Requirement 1.9's 100-of-1000 shape — 1.9 describes one
concrete world for a latency claim, not a cap on the annotated fraction.

**Per-object reads means every `GetObject` in the window, not only sidecar-shaped
keys.** A body read per row would satisfy a bound written narrowly against the
sidecar prefix while being precisely the DD4 regression the bound exists to
prevent (`schemas.py:428-441`: pagination and per-row reads had to move off this
path *together*, because the failure mode otherwise is "a **timeout** — the one
outcome that makes every requirement in this spec unobservable at once"). So
clause 1 counts them all.

**Expected RED until task 2.3.** Clause 3 is the clause that fails today, on any
example with at least one Annotated_Schema; clauses 1 and 2 pass vacuously because
the read list is empty. A reviewer seeing red here before 2.3 should read the
counterexample's clause number, not revert the fix.

Everything the world and the recorder need comes from task 1.2's shared
infrastructure in `conftest.py`: `label_worlds` / `seed_label_world` build and
materialise the bucket, `recorded_s3_calls` is a pass-through spy on
`BaseClient._make_api_call` (so it sees calls from a client the post-pass
constructs itself — design §4.2), `read_keys` returns a **list** because the bound
is on a count and a duplicate read must stay visible, and `sidecar_key` delegates
to the handler's own `_context_sidecar_key` so this file is not a second
definition of that address (`LGR-cad8032-1`, **template / ratified**). There is no
`moto.mock_aws()` and no boto3 client mock in this file
(`tests/unit/test_no_duplicate_mocking_prop.py`), and it does **not** set
`AWS_REGION` (`LGR-3e45e65-1`, **template / ratified**).

**Validates: Requirements 1.8, 4.3**
"""

import json
from typing import Any

from hypothesis import HealthCheck, Phase, event, example, given, settings
from tests.unit.lambdas.dashboard_api.conftest import (
    LabelWorld,
    label_worlds,
    named_key,
    read_keys,
    recorded_s3_calls,
    seed_label_world,
    sidecar_key,
)

# ---------------------------------------------------------------------------
# Reaching the handler
# ---------------------------------------------------------------------------
# Lazily, for the reason `conftest.py`'s `_schemas_handler` gives: `handlers/
# common.py:29-37` calls `require_env` for nine table names at **import** time, so
# binding `get_schemas` at module scope would decide this file's import-time env
# before `mock_lambda_env` has run. The `sys.path` bootstrap that makes the import
# resolve at all is the same conftest's, and it runs at collection start.


def _get_schemas() -> dict[str, Any]:
    from handlers.schemas import get_schemas

    return get_schemas()


def _rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    assert response["statusCode"] == 200, f"expected 200 from get_schemas, got {response}"
    return json.loads(response["body"])["schemas"]


def _describe(calls: list[tuple[str, dict[str, Any]]]) -> str:
    """Every recorded call as `Operation(Key=…)`, for a readable counterexample.

    `named_key` is task 1.2's helper and the reason this is worth printing at all:
    `GetObject(Key='schemas/uploaded/x-metadata.json')` says which schema tripped
    the property, `GetObject({...30 keys...})` does not.
    """
    return ", ".join(f"{operation}({named_key(params)})" for operation, params in calls) or "<no S3 calls>"


# ---------------------------------------------------------------------------
# The two fraction edges, pinned rather than drawn
# ---------------------------------------------------------------------------
#: No Annotated_Schemas at all — the listing must perform **no** per-object read.
#: Requirement 1.8's floor, and the one world in which clause 3 is vacuous and
#: clauses 1 and 2 carry the whole property.
_NO_SIDECARS = LabelWorld(schema_ids=("uploaded_schema_1700000000000", "customers_v2"), sidecars={})

#: Every Uploaded_Schema annotated — the bound's permitted ceiling
#: (`|Annotated| == |Uploaded|`). The second sidecar carries **only**
#: `field_contexts`: it is an Annotated_Schema that yields no Display_Label, so it
#: is inside clause 3 while being outside `expected_labels`. That asymmetry is the
#: distinction Requirement 1.8 is stated in terms of, and an implementation that
#: skipped it — reading only the schemas it expects a label from, which it cannot
#: know in advance — would fail clause 3 on exactly this example.
_ALL_ANNOTATED = LabelWorld(
    schema_ids=("uploaded_schema_1700000000001", "orders-2026"),
    sidecars={
        "uploaded_schema_1700000000001": {"display_name": "Customer Master"},
        "orders-2026": {"field_contexts": {"order_id": "the order's id"}},
    },
)


@example(world=_NO_SIDECARS)
@example(world=_ALL_ANNOTATED)
@given(world=label_worlds())
@settings(
    max_examples=100,
    # Every example wipes and reseeds a moto bucket and runs the whole listing
    # across three sources, which routinely exceeds Hypothesis's 200 ms default.
    # The property asserts a read **count**, never a duration — Requirement 1.9's
    # latency claim is an example with a slow stub (task 2.7), deliberately not
    # this.
    deadline=None,
    # One fixture serves every example by design; `seed_label_world` wipes the
    # bucket at the start of each one, so nothing an earlier example wrote is
    # visible to a later one.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` excluded, following `test_schema_rename_invariance_prop.py:756-765`.
    # This property is expected RED from here until task 2.3, and on every one of
    # those runs the explain phase would re-run hundreds of further examples to
    # annotate a failure whose message already names the clause and the missing
    # keys verbatim. Shrinking is kept: a minimal counterexample is the thing
    # worth having when this fails for a real reason after 2.3 lands.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_reads_are_bounded_by_the_annotated_schemas(world, label_world_env):
    """Property 3: reads are bounded by the Annotated_Schemas and never touch a schema outside them.

    **Validates: Requirements 1.8, 4.3**
    """
    annotated = sorted(world.annotated_ids)
    unannotated = sorted(frozenset(world.schema_ids) - world.annotated_ids)
    # Without these the generator's fraction distribution is invisible, and a
    # generator that reached one edge in 0 of 100 examples would pass while
    # testing half of what the property claims.
    event(f"annotated fraction: {'none' if not annotated else 'all' if not unannotated else 'some'}")
    event(f"annotated schemas that carry no display_name: {len(world.annotated_ids) - len(world.expected_labels)}")

    seed_label_world(world)
    with recorded_s3_calls() as calls:
        response = _get_schemas()

    # -- Harness precondition, not a clause -----------------------------------
    # A bound is vacuous if the listing enumerated nothing, so a world the
    # handler cannot see would let clauses 1 and 2 pass for the wrong reason.
    # This is a check on the fixture, not on the feature; the row's *content* is
    # Property 1's subject and is deliberately not asserted here.
    listed = {row["schemaId"] for row in _rows(response) if row.get("source") == "uploaded"}
    assert listed >= frozenset(world.schema_ids), (
        f"harness fault, not a property failure: the listing did not enumerate every seeded "
        f"schema, so any read bound below would hold vacuously.\n"
        f"  seeded:  {sorted(world.schema_ids)}\n"
        f"  listed:  {sorted(listed)}\n"
        f"  missing: {sorted(frozenset(world.schema_ids) - listed)}"
    )

    reads = read_keys(calls)

    # -- Clause 1: no more per-object reads than there are Annotated_Schemas ---
    # Every `GetObject` in the window, not only sidecar-shaped keys: a per-row
    # body read is the DD4 regression this bound exists to prevent, and it must
    # count against the bound rather than slip past a narrower definition.
    assert len(reads) <= len(world.annotated_ids), (
        f"Requirement 1.8: the listing performed {len(reads)} per-object read(s) for "
        f"{len(world.annotated_ids)} Annotated_Schema(s) — the count may not exceed the "
        f"number of Annotated_Schemas.\n"
        f"  annotated: {annotated}\n"
        f"  reads:     {reads}\n"
        f"  all calls: {_describe(calls)}"
    )

    # -- Clause 2: no read targets the sidecar of a schema that has none -------
    # Stated over the unannotated schemas' addresses specifically, because that
    # is Requirement 1.8's second half: a speculative read per enumerated row
    # would satisfy clause 1 only when every schema happens to be annotated, and
    # would fail here the moment one is not.
    forbidden = {sidecar_key(schema_id): schema_id for schema_id in unannotated}
    trespassed = sorted({key for key in reads if key in forbidden})
    assert not trespassed, (
        f"Requirement 1.8: the listing read the Context_Sidecar of "
        f"{[forbidden[key] for key in trespassed]}, which "
        f"{'has' if len(trespassed) == 1 else 'have'} none — an Uploaded_Schema that is not an "
        f"Annotated_Schema must not be read at all.\n"
        f"  keys read that should not have been: {trespassed}\n"
        f"  all calls: {_describe(calls)}"
    )

    # -- Clause 3: every Annotated_Schema's sidecar IS read --------------------
    # The half that stops the bound being satisfied by doing nothing. This is the
    # clause that is RED until task 2.3, and it is a claim about reads rather than
    # labels: a sidecar holding only `field_contexts` is still read, because
    # nothing can know it holds no `display_name` until it has been.
    expected = {sidecar_key(schema_id): schema_id for schema_id in annotated}
    unread = sorted(key for key in expected if key not in reads)
    assert not unread, (
        f"Requirement 1.8 bounds the read count, it does not permit zero: the sidecar of "
        f"{[expected[key] for key in unread]} was never read, so no Display_Label could have "
        f"been resolved for {'it' if len(unread) == 1 else 'them'}. The bound must be met by "
        f"reading each Annotated_Schema once, not by reading nothing.\n"
        f"  annotated ({len(annotated)}): {annotated}\n"
        f"  of which carry a display_name: {sorted(world.expected_labels)}\n"
        f"  sidecar keys never read: {unread}\n"
        f"  keys actually read: {reads}\n"
        f"  all calls: {_describe(calls)}"
    )
