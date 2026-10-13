# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Requirement 1.11 — a Display_Label is derived from the Context_Sidecar and from no other store.

p36-schema-naming task 2.8. **Validates: Requirements 1.11.**

**An example, not a property, and design §6 says why.** "No other store" has no
enumerable input space — there is nothing to quantify over, because the claim is
about which *stores* the code consults rather than about which inputs it handles.
Design §6's non-property table lists it as "negative assertion on recorded calls",
so wrapping it in `@given` would buy 100 identical runs of one negative.

**The decision this pins.** `LGR-acca05d-2` (**template / ratified**) put the
Display_Label in the Context_Sidecar and **rejected a new DynamoDB table or an
attribute on an existing one** as "a third authority for one artifact whose other
two facts already live in S3". Three further stores are unavailable rather than
merely unattractive, and that is what makes the negatives below checkable rather
than aspirational: `list_objects_v2` returns neither user metadata nor object
tags, and `upload_schema` sets neither ``Metadata=`` nor ``Tagging=``. So this
file is the guard on the label acquiring a **second** store later — the failure it
catches is not a wrong label, it is a correct label sourced twice.

**Four negatives and one positive, all over one recorded list.** Task 1.2 widened
`recorded_s3_calls` past ``("s3",)`` for exactly this test (`conftest.py`'s
``services`` docstring): the tagging and metadata halves are S3 operations but the
DynamoDB half is not, and recording both services is what keeps this to one
assertion over one list rather than a second piece of mocking machinery, which
design §6 rules out.

- **no DynamoDB call at all** — attributed through botocore's own DynamoDB service
  model rather than a transcribed list of operation names;
- **no ``GetObjectTagging``**;
- **no ``HeadObject``** — no read of an object's metadata for label purposes;
- **nothing else either**, as a closure over the whole recorded set. A fifth store
  nobody has thought of trips this without anyone amending the three above.

**Two decoys, because the negatives alone do not close the metadata half.** A
``GetObject`` response carries the object's user metadata *and* nothing forces a
separate ``HeadObject`` — so a label read out of the sidecar object's metadata
would leave the recorded call list identical to a correct implementation's. Both
the schema body and the Context_Sidecar are therefore re-put carrying a tag and a
user-metadata entry holding a value that must never appear in the response, and
the response is asserted not to contain it anywhere. That closes the hole the call
list cannot see, and it makes the claim positive as well as negative: the label
that arrives is the one in the sidecar **document**.

**The positive half, because every negative here is satisfied by doing nothing.**
A listing that resolved no labels at all would pass all four negatives. So the
labelled rows are asserted to carry their stored labels, and the recorded
``GetObject`` set is asserted to be **exactly** the Context_Sidecar keys of the
Annotated_Schemas — the only label-bearing read there is.

DynamoDB is **available** in this test (`mock_dynamodb_tables`), deliberately: an
absent DynamoDB call has to be a fact about the code rather than an artifact of an
unreachable backend.

Everything else comes from task 1.2's shared infrastructure in this directory's
`conftest.py` — `seed_label_world` materialises the world, `sidecar_key` and
`body_key` delegate to the handler's own address functions so this file is not a
second definition of either (`LGR-cad8032-1`, **template / ratified**), and the
expected row key is task 1.3's consumer-derived `display_label_row_key` rather
than a transcribed literal (Requirement 4.7, `LGR-28b4d75-5`, **template /
ratified**). There is no `moto.mock_aws()` and no boto3 client mock in this file
(`tests/unit/test_no_duplicate_mocking_prop.py`), and it does **not** set
`AWS_REGION` (`LGR-3e45e65-1`, **template / ratified**).
"""

import json
from typing import Any

from botocore.session import get_session
from tests.unit.lambdas.dashboard_api.conftest import (
    LabelWorld,
    body_key,
    named_key,
    read_keys,
    recorded_s3_calls,
    s3_client,
    seed_label_world,
    sidecar_field_names,
    sidecar_key,
)
from tests.unit.lambdas.dashboard_api.test_schemas_listing_label_prop import display_label_row_key

# ---------------------------------------------------------------------------
# Which store a recorded call belongs to — from botocore, never transcribed
# ---------------------------------------------------------------------------
# `recorded_s3_calls` records `(operation, params)` and not the service, so the
# attribution happens here. botocore's own service models are the authority for
# which operation names belong to which service, so a DynamoDB operation this
# file has never heard of is still caught. A hand-written set of DynamoDB
# operation names would be a second definition of the AWS API surface, and its
# failure mode is silent: the one operation left out is the one that ships.


def _service_operations(service: str) -> frozenset[str]:
    """Every operation name botocore's model for `service` declares."""
    return frozenset(get_session().get_service_model(service).operation_names)


#: The only two S3 operations the schemas listing performs, label pass included:
#: `ListObjectsV2` over `shared/` (the enumeration) and over `schemas/uploaded/`
#: (`_annotated_schema_ids`), plus one `GetObject` per Annotated_Schema's
#: Context_Sidecar. There is deliberately no per-row body read — DD4,
#: `schemas.py:565-583` — and no third operation of any kind.
_PERMITTED_OPERATIONS = frozenset({"ListObjectsV2", "GetObject"})

#: Read out of an S3 object's user metadata or its tags, either of which would be
#: a second store for the label. Neither is written by `upload_schema`
#: (Requirement 1.11 verified this by search), so a value that arrived from one of
#: them could only have been put there to be read.
_DECOY = "decoy-label-from-a-second-store"


def _get_schemas() -> dict[str, Any]:
    """Call the listing.

    Imported inside the function for the reason this directory's `conftest.py`
    gives: `handlers/common.py:29-37` calls `require_env` for nine table names at
    **import** time, so binding the handler at module scope would decide this
    file's import-time env before `mock_lambda_env` has run.
    """
    from handlers.schemas import get_schemas

    return get_schemas()


def _describe(calls: list[tuple[str, dict[str, Any]]]) -> str:
    """Every recorded call as `Operation(Key=…)`, for a readable failure."""
    return ", ".join(f"{operation}({named_key(params)})" for operation, params in calls) or "<no recorded calls>"


def _build_world() -> tuple[LabelWorld, dict[str, str]]:
    """The one world this example needs, and the labels it must surface.

    Four Uploaded_Schemas covering every case the claim has to hold across at
    once: two Annotated_Schemas carrying a Display_Label (one of them beside
    `field_contexts`, the sidecar's other tenant), one Annotated_Schema carrying
    **no** label — read, and yielding nothing — and one schema with no sidecar at
    all, which must be read from nowhere.

    The field names come from `sidecar_field_names()`, which reads the write
    path's own constants, so the world is seeded under the field the handler
    looks for rather than under a second spelling of it.
    """
    contexts_field, display_field = sidecar_field_names()
    labelled = "uploaded_schema_1700000000010"
    labelled_too = "customers-v3"
    annotated_unlabelled = "orders_2026"
    unannotated = "uploaded_schema_1700000000011"

    # Padding preserved: a Display_Label is stored verbatim and the gate, not the
    # value, is what trims (R2.4).
    expected = {labelled: "Customer Master", labelled_too: "  顧客マスタ "}

    world = LabelWorld(
        schema_ids=(labelled, labelled_too, annotated_unlabelled, unannotated),
        sidecars={
            labelled: {display_field: expected[labelled]},
            labelled_too: {
                display_field: expected[labelled_too],
                contexts_field: {"customer_id": "the customer's id"},
            },
            annotated_unlabelled: {contexts_field: {"order_id": "the order's id"}},
        },
    )
    return world, expected


def _plant_decoys(bucket: str, schema_id: str) -> None:
    """Re-put one schema's body and sidecar carrying a tag and metadata decoy.

    Same bytes, same keys — only an object tag and a user-metadata entry are
    added, both holding `_DECOY`. Runs **before** the recorder window opens, so
    these calls are never under assertion.

    This is what closes the metadata half of Requirement 1.11. A label read out
    of the sidecar object's user metadata needs no extra API call at all — user
    metadata comes back on the same `GetObject` the sidecar body arrives in — so
    the recorded call list would be byte-identical to a correct implementation's
    and only the *value* in the response can distinguish them.
    """
    s3 = s3_client()
    for key in (body_key(schema_id), sidecar_key(schema_id)):
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            Tagging=f"display_name={_DECOY}",
            Metadata={"display-name": _DECOY},
        )


def test_the_display_label_comes_from_the_context_sidecar_and_no_other_store(mock_dynamodb_tables, label_world_env):
    """Requirement 1.11: the label is derived from the Context_Sidecar, and nothing else.

    `mock_dynamodb_tables` is requested **first** so its backends are live before
    `label_world_env` creates the buckets, matching the order
    `test_schema_rename_invariance_prop.py` already uses for the same pair. Its
    purpose here is negative: DynamoDB is reachable and seeded with the twelve
    real tables, so "no DynamoDB call" is a statement about the code rather than
    about an unavailable service.

    **Validates: Requirements 1.11**
    """
    bucket = label_world_env
    world, expected_labels = _build_world()
    seed_label_world(world)
    # On one labelled schema, so the response carrying the sidecar's value while
    # the decoy sits on both of that schema's objects is the discriminating case.
    labelled = sorted(expected_labels)[0]
    _plant_decoys(bucket, labelled)

    with recorded_s3_calls(services=("s3", "dynamodb")) as calls:
        response = _get_schemas()

    assert response["statusCode"] == 200, (
        f"Requirement 1.10 degrades a label, never the response — `GET /api/schemas` must be a "
        f"200 for a readable world. Got {response['statusCode']}: {response['body']}"
    )
    rows = {row["schemaId"]: row for row in json.loads(response["body"])["schemas"]}
    operations = {operation for operation, _ in calls}

    # -- The positive half, first, because every negative below is satisfied by --
    # -- doing nothing at all -------------------------------------------------
    label_key = display_label_row_key()
    for schema_id, label in expected_labels.items():
        assert rows[schema_id].get(label_key) == label, (
            f"Requirement 1.11 is a claim about *where* a label comes from, and it is vacuous if "
            f"no label arrives: row for {schema_id!r} carries {rows[schema_id].get(label_key)!r} "
            f"under {label_key!r}, expected the stored {label!r} verbatim.\n"
            f"  row: {rows[schema_id]}"
        )

    # The only label-bearing read there is: one `GetObject` per Annotated_Schema's
    # Context_Sidecar, and nothing else read at all. Sorted rather than compared
    # in order — the reads are fanned out (design §4.2), so which one enters
    # botocore first is not determined.
    reads = read_keys(calls)
    assert sorted(reads) == sorted(sidecar_key(schema_id) for schema_id in world.annotated_ids), (
        f"the only label-bearing read must be a `GetObject` on the Context_Sidecar's key, one per "
        f"Annotated_Schema.\n"
        f"  annotated:    {sorted(world.annotated_ids)}\n"
        f"  keys read:    {sorted(reads)}\n"
        f"  all calls:    {_describe(calls)}"
    )

    # -- Negative 1: no DynamoDB call at all ---------------------------------
    # `LGR-acca05d-2` rejected a DynamoDB table, and an attribute on an existing
    # one, as a third authority for one artifact. This is that rejection made
    # mechanical.
    dynamodb_operations = _service_operations("dynamodb")
    s3_operations = _service_operations("s3")
    assert not (dynamodb_operations & s3_operations), (
        f"harness fault, not a Requirement 1.11 failure: botocore now declares "
        f"{sorted(dynamodb_operations & s3_operations)} for both DynamoDB and S3, so an operation "
        f"name no longer identifies which store a recorded call reached. Record the service "
        f"alongside the operation before trusting the assertion below."
    )
    assert not (operations & dynamodb_operations), (
        f"Requirement 1.11: resolving Display_Labels made {sorted(operations & dynamodb_operations)} "
        f"against DynamoDB. `LGR-acca05d-2` (template / ratified) rejected a DynamoDB table, and an "
        f"attribute on an existing one, as a third authority for one artifact whose other two facts "
        f"already live in S3 — the label has one store and it is the Context_Sidecar.\n"
        f"  all calls: {_describe(calls)}"
    )

    # -- Negative 2: no object tags read -------------------------------------
    assert "GetObjectTagging" not in operations, (
        f"Requirement 1.11: the listing read object tags. `upload_schema` sets no `Tagging=`, so a "
        f"tag can hold no label — and reading one would make the tag a second store for a fact the "
        f"Context_Sidecar already holds.\n"
        f"  all calls: {_describe(calls)}"
    )

    # -- Negative 3: no object metadata read for label purposes --------------
    assert "HeadObject" not in operations, (
        f"Requirement 1.11: the listing read S3 object metadata. `upload_schema` sets no "
        f"`Metadata=`, so object metadata can hold no label, and a `HeadObject` per row is also the "
        f"per-object cost DD4 removed from this path.\n"
        f"  all calls: {_describe(calls)}"
    )

    # -- Negative 4: and nothing else either ---------------------------------
    # The closure. Negatives 1-3 name the three stores that were considered; this
    # one catches a fourth nobody has thought of, without anyone remembering to
    # extend the list above.
    assert operations <= _PERMITTED_OPERATIONS, (
        f"Requirement 1.11: resolving Display_Labels performed "
        f"{sorted(operations - _PERMITTED_OPERATIONS)}, which is neither the two prefix listings nor "
        f"the Context_Sidecar read. A label is derived from the Context_Sidecar and from no other "
        f"store; if a new store is genuinely wanted, `LGR-acca05d-2` is the decision to revisit "
        f"first.\n"
        f"  all calls: {_describe(calls)}"
    )

    # -- The decoys: the value came from the sidecar *document* --------------
    # Neither an object tag nor user metadata can reach the response, and this is
    # the only assertion that can say so — a metadata read costs no extra API
    # call, so the recorded list above cannot distinguish it.
    assert _DECOY not in response["body"], (
        f"Requirement 1.11: {_DECOY!r} reached the response. It exists only as an object tag and a "
        f"user-metadata entry on {labelled!r}'s body and Context_Sidecar, so a Display_Label is "
        f"being sourced from S3 object metadata or tags rather than from the Context_Sidecar "
        f"document.\n"
        f"  body: {response['body']}"
    )
