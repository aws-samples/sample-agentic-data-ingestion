# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 4 — a label failure degrades a label and never removes a schema.

# Feature: p36-schema-naming, Property 4: A label failure degrades a label and never removes a schema

For *any* generated world and *any* subset of its Annotated_Schemas whose sidecar
read fails, ``GET /api/schemas`` still returns a row for **every** schema, no
schema in the failing subset carries a ``displayLabel``, and the shortfall is
reported in ``sourcesFailed`` — **and** a schema that simply has no sidecar
produces no ``sourcesFailed`` entry at all.

**The fourth clause is the point of the property, not an addendum.** Requirement
1.10 turns on absence and unreadability being different facts: "reporting it as
'no label' alone would make an `AccessDenied` on the sidecar prefix
indistinguishable from a schema nobody has named" (`requirements.md:163-171`).
The inverse error is just as bad and is the one an over-eager implementation makes
— reporting every unnamed schema as a failure would turn "nobody named this" into
"I could not look", which is the defect 1.10 fixes wearing the other shoe. So
clauses 1–3 pin what a failure must produce and clause 4 pins what an *absence*
must not, and neither half is sufficient alone: an implementation reporting
nothing passes 4 and fails 3, one reporting everything passes 3 and fails 4.

**Three doors, all through task 1.2's one recorder.** `recorded_s3_calls`'
``fail_keys`` takes an error code per key, which is what makes all three
injectable without new machinery (design §6, "no new mocking machinery"):

- an **unreadable** sidecar — ``AccessDenied`` and friends — is clauses 1–3
  (`test_a_sidecar_read_failure_degrades_only_the_label`);
- an **absent** sidecar — ``NoSuchKey``, a real case, a sidecar deleted between
  the listing and the read — is clause 4
  (`test_an_absent_sidecar_is_silent_not_a_reported_failure`), alongside the
  schemas that never had a sidecar at all;
- a failed **prefix listing** is the second of Requirement 1.10's two scopes and
  needs ``fail_list_prefixes``, added to the shared recorder for this task
  (`test_a_failed_prefix_listing_reports_one_entry_and_no_labels`).

**Two implementation behaviours the assertions below are written against**, both
read end to end before this file was written rather than inferred:

1. ``NoSuchKey`` produces **no** entry. `_absent_sidecar` (`schemas.py:1016-1034`)
   classifies through ``NOT_FOUND_CLIENT_ERROR_CODES`` and
   `_resolve_display_labels` ``continue``s on it, so an injected 404 is silent by
   the same route a genuine one is. ``AccessDenied`` is not in that set and does
   produce an entry.
2. Failures are aggregated into **one** entry, not one per schema
   (`_record_label_shortfall`, `schemas.py:1050-1083`). So clause 3 asserts one
   entry naming the count, not N entries. The aggregation is load-bearing:
   `SchemaManagementPage.tsx:448-451` renders one ``<li>`` per entry, so 1000
   abandoned reads emitting 1000 entries would read as *the schemas page broke*.

**Scoped to the label entries rather than to `sourcesFailed` as a whole.** The
array is p35's and other sources contribute to it — the ``s3tables`` service model
is not always present in the unit environment, and that failure is legitimately
reported. Clause 3 and clause 4 are therefore claims about the entries this
feature adds, identified by `_label_entries` below.

Shared fixtures only: `label_world_env` composes `mock_s3_buckets` and
`mock_lambda_env` from `tests/fixtures/conftest.py`, there is no `moto.mock_aws()`
and no boto3 client mock in this file
(`tests/unit/test_no_duplicate_mocking_prop.py`), and it does **not** set
``AWS_REGION`` (`LGR-3e45e65-1`, **template / ratified**). Every address comes
from the handler's own constants through `sidecar_key` / `sidecar_prefix`
(`LGR-cad8032-1`, **template / ratified**).

**Validates: Requirements 1.10**
"""

import json
from typing import Any

from hypothesis import HealthCheck, Phase, event, example, given, settings
from hypothesis import strategies as st
from tests.unit.lambdas.dashboard_api.conftest import (
    LabelWorld,
    label_worlds,
    named_key,
    read_keys,
    recorded_s3_calls,
    seed_label_world,
    sidecar_key,
    sidecar_prefix,
)

# ---------------------------------------------------------------------------
# Reaching the handler
# ---------------------------------------------------------------------------
# Lazily, for `conftest.py`'s `_schemas_handler` reason: `handlers/common.py:29-37`
# `require_env`s nine table names at **import** time, so binding `get_schemas` at
# module scope would decide this file's import-time env before `mock_lambda_env`
# has run.


def _get_schemas() -> dict[str, Any]:
    from handlers.schemas import get_schemas

    return get_schemas()


def _label_failure_sample_size() -> int:
    """`_LABEL_FAILURE_SAMPLE_SIZE`, imported rather than transcribed.

    The bound on how many ids one aggregated entry names. Imported because the
    number is the *implementation's* choice of how much to show a user — R1.10
    requires the sample to be bounded, not to be five — so a test hard-coding 5
    would fail on a deliberate change to a value the requirement does not fix.
    """
    from handlers.schemas import _LABEL_FAILURE_SAMPLE_SIZE

    return _LABEL_FAILURE_SAMPLE_SIZE


def _body(response: dict[str, Any]) -> dict[str, Any]:
    assert response["statusCode"] == 200, (
        f"R1.10 degrades a label, so an unreadable sidecar must still be a 200 carrying every row; got {response}"
    )
    return json.loads(response["body"])


def _uploaded_rows(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["schemaId"]: row for row in body["schemas"] if row.get("source") == "uploaded"}


#: The two label scopes, as the prefixes the handler builds them from. Spelled
#: here because both are inline f-strings in `_record_label_shortfall` and
#: `_resolve_display_labels`' listing clause — there is no constant to import, so
#: what guards against drift is that the *count* and the *bucket* below are
#: derived from the world rather than transcribed, and that the two scopes are
#: asserted to be different from one another.
_READ_SHORTFALL_SCOPE_PREFIX = "display labels for "
_LISTING_SCOPE_PREFIX = "display labels in "


def _label_entries(body: dict[str, Any]) -> list[dict[str, str]]:
    """Every `sourcesFailed` entry this feature produced.

    Filtered rather than taking `sourcesFailed` whole: the array is p35's and
    other sources legitimately contribute to it in this environment, so a claim
    about label reporting has to be a claim about the label entries.
    """
    return [
        entry
        for entry in body["sourcesFailed"]
        if entry.get("source") == "uploaded"
        and entry.get("scope", "").startswith((_READ_SHORTFALL_SCOPE_PREFIX, _LISTING_SCOPE_PREFIX))
    ]


def _describe(calls: list[tuple[str, dict[str, Any]]]) -> str:
    return ", ".join(f"{operation}({named_key(params)})" for operation, params in calls) or "<no S3 calls>"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
#: Error codes that mean "I could not look". None is in
#: `NOT_FOUND_CLIENT_ERROR_CODES`, so each one must produce an entry.
#: `AccessDenied` is `_read_context_sidecar`'s absorbed case (`:810-815`) and the
#: reason the label pass does not reuse it; the other two are the transient shapes
#: a real bucket produces under load. The recorder raises above botocore's retry
#: handler, so a 5xx code is injected once and not retried away.
_UNREADABLE_CODES = st.sampled_from(["AccessDenied", "InternalError", "SlowDown"])

#: An id no generated id can collide with: `_SCHEMA_IDS` draws either
#: `uploaded_schema_{13 digits}` or free-form text of at most 24 characters, and
#: this is 27. Used to guarantee the "nobody named this" case exists in clause 4's
#: worlds without filtering worlds away.
_NEVER_ANNOTATED_ID = "schema_with_no_sidecar_at_all"


@st.composite
def _worlds_with_failing_reads(draw, codes=_UNREADABLE_CODES):
    """A world with at least one Annotated_Schema, and a failing subset of them.

    The subset is drawn **from the world's own annotated ids** rather than from
    the whole id set, because a schema with no sidecar has no read to fail — that
    is clause 4's subject, not clause 1's, and conflating them is what would make
    this property untestable in the direction that matters.
    """
    world = draw(label_worlds(min_schemas=1, max_schemas=8).filter(lambda w: bool(w.sidecars)))
    annotated = sorted(world.annotated_ids)
    failing = draw(st.lists(st.sampled_from(annotated), min_size=1, max_size=len(annotated), unique=True))
    return world, {schema_id: draw(codes) for schema_id in sorted(failing)}


@st.composite
def _worlds_with_absent_sidecars(draw):
    """A world with an absent-sidecar subset **and** a schema that never had one.

    Both halves of clause 4 in one example. The extra id is appended rather than
    filtered for: requiring the draw to produce a world with both an annotated and
    an unannotated schema rejects the ``none`` and ``all`` coverage branches
    outright, which is most of the generator.
    """
    world = draw(label_worlds(min_schemas=1, max_schemas=6).filter(lambda w: bool(w.sidecars)))
    schema_ids = list(world.schema_ids)
    if not (frozenset(schema_ids) - world.annotated_ids):
        schema_ids.append(_NEVER_ANNOTATED_ID)
    annotated = sorted(world.annotated_ids)
    deleted = draw(st.lists(st.sampled_from(annotated), min_size=1, max_size=len(annotated), unique=True))
    return LabelWorld(schema_ids=tuple(schema_ids), sidecars=world.sidecars), sorted(deleted)


_SETTINGS = settings(
    max_examples=100,
    # Every example wipes and reseeds a moto bucket and runs the whole listing
    # across three sources. This property asserts a response *shape*, never a
    # duration — the deadline is task 2.7's subject.
    deadline=None,
    # One fixture serves every example by design; `seed_label_world` wipes the
    # bucket at the start of each one.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` excluded, following `test_schema_rename_invariance_prop.py:756-765`
    # and `test_label_read_bound_prop.py`. Shrinking is kept.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)


# ---------------------------------------------------------------------------
# Clauses 1-3 — an unreadable sidecar
# ---------------------------------------------------------------------------
#: Seven Annotated_Schemas, every read denied. Pinned so the **bounded sample**
#: branch of `_record_label_shortfall` is exercised deterministically: at seven
#: failures the entry names five ids and says how many more, which is the branch
#: that stops one shortfall rendering as a wall of ids in a single `<li>`.
_SEVEN_DENIED = LabelWorld(
    schema_ids=tuple(f"denied_schema_{index}" for index in range(7)),
    sidecars={f"denied_schema_{index}": {"display_name": f"Denied {index}"} for index in range(7)},
)


@example(
    case=(_SEVEN_DENIED, dict.fromkeys(_SEVEN_DENIED.schema_ids, "AccessDenied")),
)
@given(case=_worlds_with_failing_reads())
@_SETTINGS
def test_a_sidecar_read_failure_degrades_only_the_label(case, label_world_env):
    """Property 4: a label failure degrades a label and never removes a schema.

    **Validates: Requirements 1.10**
    """
    world, failing = case
    bucket = label_world_env
    survivors = {schema_id: label for schema_id, label in world.expected_labels.items() if schema_id not in failing}
    event(f"failing reads: {len(failing)} of {len(world.annotated_ids)} annotated")
    event(f"labels expected to survive: {len(survivors)}")

    seed_label_world(world)
    with recorded_s3_calls(fail_keys={sidecar_key(schema_id): code for schema_id, code in failing.items()}) as calls:
        response = _get_schemas()

    body = _body(response)
    rows = _uploaded_rows(body)
    entries = _label_entries(body)

    # -- Clause 1: every schema still has a row --------------------------------
    # The whole of R1.10's first half. A label is a field on a row; losing the row
    # loses the schema from the picker, the filter and the detail link at once.
    missing = sorted(frozenset(world.schema_ids) - frozenset(rows))
    assert not missing, (
        f"R1.10: {missing} lost {'its' if len(missing) == 1 else 'their'} row entirely because a "
        f"Display_Label could not be read. A label failure degrades a label, never a schema.\n"
        f"  seeded: {sorted(world.schema_ids)}\n"
        f"  listed: {sorted(rows)}\n"
        f"  failed reads: {failing}\n"
        f"  calls: {_describe(calls)}"
    )

    # -- Clause 2: no schema whose read failed carries a label ------------------
    # Absent, not `None` and not `""` — R1.2's rule, which R1.10's degraded path
    # has to obey too. A client falling back on a missing key renders the id.
    mislabelled = {
        schema_id: rows[schema_id]["displayLabel"] for schema_id in failing if "displayLabel" in rows[schema_id]
    }
    assert not mislabelled, (
        f"R1.10: {sorted(mislabelled)} carried a Display_Label although the read of "
        f"{'its' if len(mislabelled) == 1 else 'their'} Context_Sidecar failed — the label is "
        f"unknown, so the key must be absent.\n"
        f"  labels emitted anyway: {mislabelled}\n"
        f"  failed reads: {failing}"
    )

    # -- Clause 3: the shortfall is reported, as ONE entry ----------------------
    # Non-empty is R1.10 literally. Exactly one is `_record_label_shortfall`'s
    # aggregation, and asserting the count here is what keeps a later "one entry
    # per schema" refactor from passing: `SchemaManagementPage.tsx:448-451`
    # renders one `<li>` per entry.
    assert entries, (
        f"R1.10: {len(failing)} Display_Label read(s) failed and nothing was reported — a schema "
        f"with no label and no explanation is indistinguishable from one nobody has named.\n"
        f"  failed reads: {failing}\n"
        f"  sourcesFailed: {body['sourcesFailed']}"
    )
    assert len(entries) == 1, (
        f"R1.10 aggregates: {len(failing)} failed read(s) must produce ONE entry, not one each. "
        f"Got {len(entries)}.\n  entries: {entries}"
    )

    entry = entries[0]
    assert entry["scope"] == f"{_READ_SHORTFALL_SCOPE_PREFIX}{len(failing)} schema(s)", (
        f"the aggregated entry must scope itself to the number of schemas short of a label "
        f"({len(failing)}); got {entry['scope']!r}"
    )

    # The reason codes and a bounded sample of ids, because an entry that names
    # neither is a warning a user cannot act on.
    for code in sorted(set(failing.values())):
        assert code in entry["error"], f"the entry does not name the reason {code!r}: {entry['error']!r}"
    named = [schema_id for schema_id in sorted(failing) if schema_id in entry["error"]]
    sample_size = _label_failure_sample_size()
    assert len(named) >= min(len(failing), sample_size), (
        f"the entry names {len(named)} of the {len(failing)} schemas short of a label, expected at "
        f"least {min(len(failing), sample_size)}: {entry['error']!r}"
    )
    if len(failing) > sample_size:
        assert "more" in entry["error"], (
            f"{len(failing)} failures exceed the sample size of {sample_size}, so the entry must say "
            f"how many it is not naming rather than listing them all: {entry['error']!r}"
        )

    # -- Positive half, so none of the above can pass vacuously ----------------
    # A pass that resolved nothing would satisfy clauses 1 and 2 for the wrong
    # reason. Every label whose read was NOT failed must still arrive: one
    # unreadable sidecar degrades one label.
    for schema_id, expected in survivors.items():
        assert rows[schema_id].get("displayLabel") == expected, (
            f"R1.10 degrades only what failed: {schema_id}'s sidecar was readable and holds "
            f"{expected!r}, so its label must still arrive. Row: {rows[schema_id]}\n"
            f"  failed reads: {failing}\n"
            f"  calls: {_describe(calls)}"
        )
    assert bucket in entry["error"] or bucket in entry["scope"], (
        f"the entry names neither the bucket nor a scope containing it, so an operator cannot tell "
        f"where to look: {entry}"
    )


# ---------------------------------------------------------------------------
# Clause 4 — an absent sidecar is silent
# ---------------------------------------------------------------------------
@given(case=_worlds_with_absent_sidecars())
@_SETTINGS
def test_an_absent_sidecar_is_silent_not_a_reported_failure(case, label_world_env):
    """Property 4, clause 4: absence produces no `sourcesFailed` entry.

    Two absences, one assertion: a schema that never had a Context_Sidecar and one
    whose sidecar has gone missing between the prefix listing and the read
    (injected ``NoSuchKey``, which `_absent_sidecar` classifies through
    ``NOT_FOUND_CLIENT_ERROR_CODES``). Neither is a failure. Reporting either
    would make "nobody named this" indistinguishable from "I could not look",
    which is the inverse of the defect R1.10 fixes.

    **Validates: Requirements 1.10**
    """
    world, deleted = case
    never_annotated = sorted(frozenset(world.schema_ids) - world.annotated_ids)
    survivors = {schema_id: label for schema_id, label in world.expected_labels.items() if schema_id not in deleted}
    event(f"sidecars vanished between listing and read: {len(deleted)}")
    event(f"schemas that never had a sidecar: {len(never_annotated)}")

    seed_label_world(world)
    with recorded_s3_calls(fail_keys=dict.fromkeys((sidecar_key(sid) for sid in deleted), "NoSuchKey")) as calls:
        response = _get_schemas()

    body = _body(response)
    rows = _uploaded_rows(body)

    missing = sorted(frozenset(world.schema_ids) - frozenset(rows))
    assert not missing, f"R1.10: {missing} lost a row over an absent Display_Label.\n  listed: {sorted(rows)}"

    # An absent sidecar means no label, on both routes into absence.
    for schema_id in [*deleted, *never_annotated]:
        assert "displayLabel" not in rows[schema_id], (
            f"{schema_id} has no Context_Sidecar, so no Display_Label can exist for it; the row "
            f"carries {rows[schema_id]['displayLabel']!r}"
        )

    # The clause. Silence, not a report.
    entries = _label_entries(body)
    assert not entries, (
        f"R1.10: absence is not a failure. {len(deleted)} sidecar(s) returned NoSuchKey and "
        f"{len(never_annotated)} schema(s) never had one, and the response reported it — which makes "
        f"'nobody named this' indistinguishable from 'I could not look'.\n"
        f"  entries: {entries}\n"
        f"  vanished: {deleted}\n"
        f"  never annotated: {never_annotated}"
    )

    # Not vacuous: the pass ran, read what it should have, and resolved the rest.
    reads = read_keys(calls)
    for schema_id in deleted:
        assert sidecar_key(schema_id) in reads, (
            f"harness fault, not a property failure: {schema_id}'s sidecar was never read, so the "
            f"injected NoSuchKey never fired and the silence above proves nothing.\n"
            f"  reads: {reads}"
        )
    for schema_id in never_annotated:
        assert sidecar_key(schema_id) not in reads, (
            f"R1.8: {schema_id} has no sidecar and must not be read at all.\n  reads: {reads}"
        )
    for schema_id, expected in survivors.items():
        assert rows[schema_id].get("displayLabel") == expected, (
            f"{schema_id}'s sidecar is readable and holds {expected!r}, so its label must arrive "
            f"even though other sidecars are gone. Row: {rows[schema_id]}"
        )


# ---------------------------------------------------------------------------
# R1.10's other scope — the prefix listing itself failed
# ---------------------------------------------------------------------------
#: Three schemas, two of them Annotated. Deterministic rather than generated: the
#: listing failing is one code path with no input space — no label is resolvable
#: whatever the world holds, which is exactly why it is a different entry.
_LISTING_WORLD = LabelWorld(
    schema_ids=("uploaded_schema_1700000000007", "orders-2026", "customers_v2"),
    sidecars={
        "uploaded_schema_1700000000007": {"display_name": "Customer Master"},
        "orders-2026": {"display_name": "Orders 2026", "field_contexts": {"order_id": "the order's id"}},
    },
)


def test_a_failed_prefix_listing_reports_one_entry_and_no_labels(label_world_env):
    """R1.10's first scope: no label at all, one entry, and a *different* scope.

    `_resolve_display_labels` returns early when `_annotated_schema_ids` raises,
    because when the listing itself failed nothing is resolvable and there is no
    per-schema shortfall to enumerate — the `7c9378d` shape (`LGR-7c9378d-1`,
    **template / ratified**): correct code in front of a missing permission, which
    has to be reported rather than absorbed.

    An example rather than a property. There is no input space: the outcome does
    not vary with the world, which is the claim.

    **Validates: Requirements 1.10**
    """
    bucket = label_world_env
    seed_label_world(_LISTING_WORLD)

    with recorded_s3_calls(fail_list_prefixes={sidecar_prefix(): "AccessDenied"}) as calls:
        response = _get_schemas()

    body = _body(response)
    rows = _uploaded_rows(body)

    missing = sorted(frozenset(_LISTING_WORLD.schema_ids) - frozenset(rows))
    assert not missing, (
        f"R1.10: {missing} lost a row because the sidecar prefix could not be listed. The body "
        f"enumeration under 'shared/' succeeded, so every schema is still known.\n"
        f"  calls: {_describe(calls)}"
    )

    labelled = {schema_id: row["displayLabel"] for schema_id, row in rows.items() if "displayLabel" in row}
    assert not labelled, (
        f"no Display_Label is resolvable when the sidecar prefix cannot be listed, yet {labelled} arrived"
    )

    # No speculative reads: the pass returned before submitting any.
    sidecar_reads = [key for key in read_keys(calls) if key.startswith(sidecar_prefix())]
    assert not sidecar_reads, (
        f"the pass read {sidecar_reads} after failing to list the prefix — with no listing there is "
        f"no Annotated_Schema set, so a read here would be a guess"
    )

    entries = _label_entries(body)
    assert len(entries) == 1, f"expected exactly one label entry for the failed listing; got {entries}"
    entry = entries[0]
    assert entry["source"] == "uploaded", f"the entry must carry the same source token the rows do: {entry}"
    assert entry["scope"] == f"{_LISTING_SCOPE_PREFIX}{bucket}", (
        f"a failed prefix listing is scoped to the bucket, not to a schema count; got {entry['scope']!r}"
    )
    # The two scopes are deliberately distinct: one says "I could not look at
    # all", the other "these N are short". A single scope for both would collapse
    # the distinction R1.10 is about.
    assert not entry["scope"].startswith(_READ_SHORTFALL_SCOPE_PREFIX), (
        f"a failed listing must not be reported as a per-schema shortfall: {entry['scope']!r}"
    )
    assert "AccessDenied" in entry["error"], f"the entry must name the reason: {entry['error']!r}"
