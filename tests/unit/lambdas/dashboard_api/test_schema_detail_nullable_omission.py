# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``nullable`` is emitted only when a producer stated one — ``_schema_detail`` and the Glue branch.

Task 11.1 of p45. It observes the two Python edits tasks 8.1 and 9.1 landed, both
in ``handlers/schemas.py``:

- **The Glue branch of ``_collect_schema_rows``** (``:496-520``; the enclosing
  function is ``_collect_schema_rows`` at ``:401``, **not** ``get_schemas``, which
  is at ``:728``). ``"nullable": True`` is gone and no key replaces it, because
  ``StorageDescriptor.Columns[]`` carries no nullability at all.
- **``_schema_detail``** (``:1542``), whose per-field ``nullable`` moved from
  ``field.get("nullable", True)`` to a conditional key set at ``:1606-1608``, on
  the gate ``displayLabel`` already uses twenty lines below at ``:1625-1627``.

Three claims, one per group below: ``_schema_detail`` **omits** the key for a
producer that stated nothing; it **passes a stated one through unchanged**, both
``True`` and ``False``; and the Glue branch emits **no** ``nullable`` at all, on
the listing as well as on the per-id response.

**Where the expectations come from.** R5.2 (the status is sourced from the
Field_Nullable the API emits) and R5.3 (where the source did not state
nullability, Not_Established is a third state and must not be reported as
optional), plus the three producers that *do* state it, read as source:
``_uploaded_schema_fields``' two body shapes (``:1376`` — ``not
field_def.get("required", False)`` — and ``:1405`` — ``field_name not in
required``) and ``_s3_tables_schema_fields`` (``:1469``). **Never from running the
endpoint** — `LGR-28b4d75-5` (**template / ratified**): *"a test written from the
producer's output cannot detect the producer drifting, because the output is its
own oracle."*

**What this file deliberately does NOT re-assert.** ``nullable``'s *stated* cases
are already pinned six times, all reaching ``_schema_detail`` through
``get_schema``:
``test_schemas_listing_sources.py:686``, ``:687``, ``:691``, ``:692`` (both
uploaded body shapes) and ``:727``, ``:728`` (s3_tables), each asserting ``is
False`` / ``is True`` for a producer's ``required``-derived value. Those pin the
**producers' derivation** and are why task 9.1's change moved no test — every one
of them is a stated case, and the gate only changed the unstated one. This file's
subjects are therefore the **unstated** case, which was pinned nowhere, and the
**exact key sets**, which no test asserted either: the design's §4.9b claim that
``nullable`` is pinned "nowhere on either side" is right about the case that
matters and wrong as written.

The stated case still appears below, at the ``_schema_detail`` level and as a
**pass-through** claim rather than a derivation one: whatever boolean a row
carries reaches the response byte-identically. That is the regression the
conditional gate could have introduced and the six existing assertions do not
isolate, because each of them also exercises a producer.

**No ``nullable: None``, anywhere.** A ``None`` would be a third spelling of two
facts — do-NOT 17 and `LGR-44d601e-1` (**template / ratified**), which clears a
Display_Label by *removing* the key precisely because *"a stored `\"\"` would
have to be distinguished from absence at every reader."* Asserted both for a
producer that stated nothing and for a row that carries an explicit ``None``,
which must be dropped rather than forwarded.

**The rest of the key set is asserted as a set, not as values** (R7.4 — this
change adds no response key, it stops fabricating one). ``description`` and
``context`` keep their two distinct authorities (C5, `LGR-acca05d-3`, **template
/ ratified**: *"a second authority for a field's description alongside the
schema's own … kept legible by putting them under distinct headings"*), and
``displayLabel`` keeps its absent-not-null behaviour — which is the same spelling
``nullable`` now follows, and the reason the two are asserted together.
``test_schema_detail_endpoint.py`` asserts those two facts by **value**; the
claims here are set equalities, so a *newly added* key fails here and nowhere
else.

**Out of scope, and deliberately untouched: the JDBC detect path.**
``jdbc_resources.py:660`` and the six wizard producers
(``resources.py:209``/``:252``/``:283``/``:327``, ``pipelines_crud.py:799``) still
emit a non-optional ``nullable``, and
``tests/contracts/api_responses/test_jdbc_target_detect_schema_contract.py:343-352``
pins that on purpose — *"Glue catalog drops JDBC NOT NULL — every column is
reported as nullable per the handler contract."* That is do-NOT 18's territory.
Nothing here extends to it and nothing here should be "harmonised" with it.

**Fixtures.** Shared fixtures only: ``mock_s3_buckets`` from
``tests/fixtures/conftest.py``, which is also what puts the moto backend in place
for the Glue seeding. No ``moto.mock_aws()`` call and no boto3 client mock
(``tests/unit/test_no_duplicate_mocking_prop.py``). This file does **not** set
``AWS_REGION`` (`LGR-3e45e65-1`, **template / ratified**); every seeding client
resolves the region through ``schemas.py:23``'s own expression (see ``_client``),
because moto's Glue backend is region-scoped and a mismatch presents as an empty
source rather than as a harness fault. ``mock_lambda_env`` is not used for the
same record's reason — it would assign the region from a fixture.

The ``_schema_detail`` group needs no AWS at all: the sidecar read at ``:1586`` is
gated on ``source == "uploaded"``, so rows sourced ``glue_catalog`` or
``s3_table_bucket`` exercise the gate as a pure function.

**Validates: Requirements 5.2, 5.3**
"""

import contextlib
import json
import os
import sys

import boto3
import pytest

# ---------------------------------------------------------------------------
# Path and import-time env — must come before importing handlers.schemas
# ---------------------------------------------------------------------------
# handlers/common.py resolves its table names with require_env() at import time,
# so the names have to exist before the import itself.
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(SHARED_DIR), os.path.abspath(LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Table names only — AWS_REGION and AWS_DEFAULT_REGION are deliberately absent
# (`LGR-3e45e65-1`). setdefault, so an already-pinned value is never overridden.
for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "ERRORS_TABLE": "agentic_data_ingestion_errors",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

from handlers.schemas import _schema_detail, get_schema, get_schemas

# ---------------------------------------------------------------------------
# The producers' field shapes, transcribed from the three producers as source
# ---------------------------------------------------------------------------
#: What a producer that states nothing emits per field — the Glue branch of
#: `_collect_schema_rows` (`schemas.py:490-517`): `name`, `type`, `description`,
#: `context`, and no `nullable`, because `StorageDescriptor.Columns[]` carries no
#: nullability at all (R5.3's named live instance).
UNSTATED_PRODUCER_KEYS = frozenset({"name", "type", "description", "context"})

#: What a producer that states one emits — `_uploaded_schema_fields`' two shapes
#: (`:1376`, `:1405`) and `_s3_tables_schema_fields` (`:1469`): the same four keys
#: plus `nullable`, derived from the body's own `required`.
STATED_PRODUCER_KEYS = UNSTATED_PRODUCER_KEYS | {"nullable"}

#: `_schema_detail`'s response object, minus the conditional `displayLabel`
#: (`:1612-1619`).
DETAIL_KEYS = frozenset({"schemaId", "name", "source", "targetType", "lastModified", "fields"})

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------
TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"

# A plain Glue table unrelated to this product, with columns. No
# `agentic_data_ingestion_` prefix, so `_exclude_glue_mirrors` cannot reach it and
# the row is guaranteed to survive to the response.
GLUE_DB = "p45_nullability_db"
GLUE_TABLE = "unstated_columns"
GLUE_COLUMNS = [
    {"Name": "customer_id", "Type": "bigint", "Comment": "Unique customer key"},
    {"Name": "churned", "Type": "boolean", "Comment": "Whether the customer left"},
]
GLUE_ROW_ID = f"glue_{GLUE_DB}_{GLUE_TABLE}"

# One uploaded schema in the custom `{"fields": [...]}` shape — a producer that
# DOES state nullability (`:1376`). Used here only for its **key set**; the
# `required`-to-`nullable` derivation itself is pinned at
# test_schemas_listing_sources.py:686-687 and is not re-asserted.
UPLOADED_ID = "uploaded_schema_1785844048926"
UPLOADED_BODY = {
    "fields": [
        {"name": "email", "type": "string", "required": True, "description": "Primary contact address"},
        {"name": "nickname", "type": "string", "required": False, "description": "Optional display name"},
    ]
}
FIELD_CONTEXT = "PII — hashed downstream"
DISPLAY_LABEL = "Customer master"


# ---------------------------------------------------------------------------
# Rows for the `_schema_detail` group — built from the shapes above, not from output
# ---------------------------------------------------------------------------
def _row(source: str, fields: list[dict]) -> dict:
    """One Schema_Listing row as `get_schema:1701-1702` hands it to `_schema_detail`.

    `source` is never ``"uploaded"`` in this group: the sidecar read at ``:1586``
    is gated on that value, so ``glue_catalog`` and ``s3_table_bucket`` rows make
    the gate a pure function of its argument and need no AWS.
    """
    return {
        "schemaId": f"{source}_row",
        "name": f"{source} row",
        "source": source,
        "targetType": "S3 Tables (Iceberg)",
        "lastModified": "2026-08-20T00:00:00",
        "fields": fields,
    }


def _unstated_field(name: str = "customer_id") -> dict:
    """A field as the Glue branch writes it (`:490-517`) — four keys, no `nullable`."""
    return {"name": name, "type": "bigint", "description": "Unique customer key", "context": ""}


def _stated_field(nullable: bool, name: str = "email") -> dict:
    """A field as the three stating producers write it (`:1376`, `:1405`, `:1469`)."""
    return {
        "name": name,
        "type": "string",
        "nullable": nullable,
        "description": "Primary contact address",
        "context": "",
    }


def _only_field(detail: dict) -> dict:
    fields = detail["fields"]
    assert len(fields) == 1, f"the fixture row declares one field; got {fields}"
    return fields[0]


# ---------------------------------------------------------------------------
# Seeding — real clients against the shared fixture's moto backend
# ---------------------------------------------------------------------------
def _client(service: str):
    """A client in exactly the region ``_collect_schema_rows`` will use (``schemas.py:23``).

    The region is **read** through the handler's own expression rather than
    stated, so seeding and handler cannot land in different regions
    (`LGR-3e45e65-1`). moto's Glue backend is region-scoped, and a mismatch
    presents as an empty source — indistinguishable from a real disagreement.
    """
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _seed_glue_table(database: str, table: str, columns: list[dict]) -> None:
    """Register one Glue database + table with columns."""
    glue = _client("glue")
    with contextlib.suppress(glue.exceptions.AlreadyExistsException):
        glue.create_database(DatabaseInput={"Name": database})
    glue.create_table(
        DatabaseName=database,
        TableInput={
            "Name": table,
            "TableType": "EXTERNAL_TABLE",
            "StorageDescriptor": {"Columns": columns, "Location": f"s3://{database}/{table}"},
        },
    )


def _seed_uploaded_schema(schema_id: str, body: dict) -> None:
    """Write one uploaded schema body at ``shared/{id}.json`` (`LGR-cad8032-1`)."""
    _client("s3").put_object(
        Bucket=TARGET_SCHEMAS_BUCKET,
        Key=f"shared/{schema_id}.json",
        Body=json.dumps(body),
        ContentType="application/json",
    )


def _seed_sidecar(schema_id: str, **contents) -> None:
    """Write the Context_Sidecar at ``schemas/uploaded/{id}-metadata.json`` (`LGR-acca05d-3`)."""
    _client("s3").put_object(
        Bucket=TARGET_SCHEMAS_BUCKET,
        Key=f"schemas/uploaded/{schema_id}-metadata.json",
        Body=json.dumps(contents),
        ContentType="application/json",
    )


@pytest.fixture
def two_sources(mock_s3_buckets, monkeypatch):
    """One Glue table (states nothing) and one uploaded schema (states one).

    ``mock_s3_buckets`` is the shared fixture; it creates the project buckets and
    is what puts the moto backend in place for the Glue seeding.
    """
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    # Keeps get_aws_account_id() (schemas.py:77) off STS entirely.
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    _seed_glue_table(GLUE_DB, GLUE_TABLE, GLUE_COLUMNS)
    _seed_uploaded_schema(UPLOADED_ID, UPLOADED_BODY)


def _detail(schema_id: str) -> dict:
    """The bare schema object ``GET /api/schemas/{id}`` returns (DD9)."""
    response = get_schema(schema_id)
    assert response["statusCode"] == 200, f"expected 200 from get_schema({schema_id!r}), got {response}"
    return json.loads(response["body"])


def _listing_row(schema_id: str) -> dict:
    response = get_schemas()
    assert response["statusCode"] == 200, f"expected 200 from get_schemas, got {response}"
    rows = json.loads(response["body"])["schemas"]
    for row in rows:
        if row.get("schemaId") == schema_id:
            return row
    raise AssertionError(f"{schema_id} is not in the listing — ids: {[r.get('schemaId') for r in rows]}")


def _nullable_nones(detail: dict) -> list[str]:
    """Field names whose ``nullable`` is present and ``None`` — do-NOT 17's failure."""
    return [f.get("name") for f in detail.get("fields", []) if "nullable" in f and f["nullable"] is None]


# ---------------------------------------------------------------------------
# Claim 1 — `_schema_detail` omits the key for a producer that stated nothing
# ---------------------------------------------------------------------------
def test_schema_detail_omits_nullable_when_the_producer_stated_none():
    """R5.3: no stated nullability means **no key**, not ``True`` and not ``None``.

    The gate is ``:1606-1608``. Before task 9.1 it was ``field.get("nullable",
    True)``, which answered *"the source declared it optional"* for a producer
    that had said nothing — C4's collapse, inside `LGR-4854b3d-1`'s own scope,
    which names this file. Absence is this file's established spelling for "not
    established", the same one ``displayLabel`` uses at ``:1625-1627``.

    Asserted as a **key-set equality** rather than as ``field["nullable"] is
    None``: the latter would pass if the key came back as ``None``, which is the
    third spelling do-NOT 17 forbids.

    **Validates: Requirements 5.2, 5.3**
    """
    detail = _schema_detail(_row("glue_catalog", [_unstated_field()]))
    field = _only_field(detail)

    assert "nullable" not in field, (
        "R5.3: a producer that stated no nullability must reach the client with no `nullable` key — "
        f"got {field.get('nullable')!r}. Do NOT reinstate the `.get(..., True)` default (`LGR-4854b3d-1`)."
    )
    assert set(field) == UNSTATED_PRODUCER_KEYS, (
        f"the four keys the Glue branch writes are what should arrive — got {sorted(field)}, "
        f"expected {sorted(UNSTATED_PRODUCER_KEYS)}"
    )


def test_schema_detail_drops_an_explicit_none_rather_than_forwarding_it():
    """do-NOT 17: ``None`` is never a value this endpoint emits for ``nullable``.

    A row carrying ``nullable: None`` is an upstream third spelling — no producer
    writes one today, and this asserts that if one ever did, ``_schema_detail``
    would not propagate it. `LGR-44d601e-1` (**template / ratified**) clears a
    stored value by **removing** the key for exactly this reason: *"a stored
    `""` would have to be distinguished from absence at every reader."*

    **Validates: Requirement 5.3**
    """
    detail = _schema_detail(_row("glue_catalog", [{**_unstated_field(), "nullable": None}]))

    assert _nullable_nones(detail) == [], (
        f"`nullable: None` reached the response for {_nullable_nones(detail)} — absence is the spelling "
        "for 'not established' and a `None` would be a third spelling of two facts (do-NOT 17)"
    )
    assert set(_only_field(detail)) == UNSTATED_PRODUCER_KEYS


# ---------------------------------------------------------------------------
# Claim 2 — a stated value passes through unchanged, both booleans
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stated", [True, False])
def test_schema_detail_passes_a_stated_nullable_through_unchanged(stated: bool):
    """R5.2: whatever boolean the producer stated is what reaches the client.

    The three producers that state it derive it from the body's own ``required``
    (``:1376``, ``:1405``, ``:1469``); this asserts ``_schema_detail`` neither
    re-derives nor defaults it. ``is`` rather than ``==``, so a truthy stand-in
    fails.

    **Not a duplicate of the six existing stated-case pins**
    (``test_schemas_listing_sources.py:686``, ``:687``, ``:691``, ``:692``,
    ``:727``, ``:728``). Those assert the *producers'* derivation through
    ``get_schema``; this asserts the *consumer's* pass-through in isolation,
    which is the regression the conditional gate at ``:1606-1608`` could have
    introduced and which none of the six can attribute, because each also
    exercises a producer.

    **Validates: Requirements 5.2, 5.3**
    """
    detail = _schema_detail(_row("s3_table_bucket", [_stated_field(stated)]))
    field = _only_field(detail)

    assert field["nullable"] is stated, (
        f"a stated `nullable` must pass through unchanged — stated {stated!r}, got {field.get('nullable')!r}"
    )
    assert set(field) == STATED_PRODUCER_KEYS, (
        f"a stating producer's five keys are what should arrive — got {sorted(field)}, "
        f"expected {sorted(STATED_PRODUCER_KEYS)}"
    )


# ---------------------------------------------------------------------------
# Claim 3 — the Glue branch of `_collect_schema_rows` emits no `nullable` at all
# ---------------------------------------------------------------------------
def test_collect_schema_rows_glue_branch_emits_no_nullable(two_sources):
    """R5.3: ``_collect_schema_rows``' Glue rows carry no ``nullable``, on the listing.

    The Glue branch (``:496-520``, inside ``_collect_schema_rows`` at ``:401``)
    used to hardcode ``"nullable": True`` for every column.
    ``StorageDescriptor.Columns[]`` carries no nullability, so the branch has no
    answer to give — and the row it appends also carries ``"fields": fields``
    (``:526``), so the omission is observable here as well as on the per-id
    response.

    Behaviour for every Python reader of the key is unchanged:
    ``create_glue_pipeline/job_builders.py:672`` and ``schema_evolution.py:92``,
    ``:130``, ``:131`` all read ``.get("nullable", True)``, so an absent key and
    ``True`` were already indistinguishable to them.

    **Validates: Requirements 5.2, 5.3**
    """
    fields = _listing_row(GLUE_ROW_ID).get("fields", [])

    assert [f.get("name") for f in fields] == [c["Name"] for c in GLUE_COLUMNS], (
        f"the fixture's two columns must both reach the listing — got {fields}"
    )
    stating = [f.get("name") for f in fields if "nullable" in f]
    assert stating == [], (
        f"the Glue branch states no nullability, so no column may carry the key — {stating} do. "
        "`StorageDescriptor.Columns[]` has no nullability to report (R5.3, `LGR-4854b3d-1`)."
    )


def test_glue_schema_detail_response_carries_no_nullable_key(two_sources):
    """R5.2/R5.3: the wire half of claim 1, through ``GET /api/schemas/{id}``.

    Glue is R5.3's named live instance and the one producer that says nothing by
    construction. ``_resolve_row_fields`` returns the row's own columns for this
    source without a read (``:1525-1526``), so what the Schema_Detail_View sees
    is the branch's output verbatim.

    **Validates: Requirements 5.2, 5.3**
    """
    detail = _detail(GLUE_ROW_ID)
    fields = detail["fields"]

    assert [f.get("name") for f in fields] == [c["Name"] for c in GLUE_COLUMNS]
    for field in fields:
        assert set(field) == UNSTATED_PRODUCER_KEYS, (
            f"{field.get('name')!r} on GET /api/schemas/{{id}}: got {sorted(field)}, "
            f"expected exactly {sorted(UNSTATED_PRODUCER_KEYS)} — no `nullable`, invented or otherwise"
        )
    assert _nullable_nones(detail) == [], f"`nullable: None` is never emitted (do-NOT 17) — {_nullable_nones(detail)}"


# ---------------------------------------------------------------------------
# R7.4 — the rest of the key set is unchanged; this removed a key, it added none
# ---------------------------------------------------------------------------
def test_stated_and_unstated_fields_differ_by_exactly_the_nullable_key(two_sources):
    """R7.4: the omission is the **only** difference between the two field shapes.

    One uploaded schema (a producer that states nullability, ``:1376``) and one
    Glue table (a producer that does not) through the same endpoint. Their
    per-field key sets must differ by exactly ``{"nullable"}`` — which is what
    makes "this adds no response key, it stops fabricating one" observable rather
    than asserted in prose.

    A newly added per-field key fails here, on both branches at once.

    **Validates: Requirements 5.2, 5.3**
    """
    unstated = {frozenset(f) for f in _detail(GLUE_ROW_ID)["fields"]}
    stated = {frozenset(f) for f in _detail(UPLOADED_ID)["fields"]}

    assert unstated == {UNSTATED_PRODUCER_KEYS}, f"Glue fields: got {[sorted(k) for k in unstated]}"
    assert stated == {STATED_PRODUCER_KEYS}, f"uploaded fields: got {[sorted(k) for k in stated]}"

    # The observed difference, not the constants' — so this fails if either branch
    # grows a key, rather than restating how the two constants were defined.
    difference = set().union(*stated) ^ set().union(*unstated)
    assert difference == {"nullable"}, (
        f"the two shapes must differ by `nullable` alone — they differ by {sorted(difference)}. A second "
        "conditional per-field key would mean this change added one rather than removing one (R7.4)."
    )


def test_the_detail_top_level_key_set_gains_nothing_and_display_label_stays_conditional(two_sources):
    """R7.4 / R4.8: six top-level keys, plus ``displayLabel`` only when one is stored.

    Asserted as a set equality, which ``test_schema_detail_endpoint.py`` does not
    do — it pins ``displayLabel``'s absence by value. The pairing is the point:
    ``displayLabel``'s "absent, not null" (``:1565-1567``, ``:1625-1627``) is the
    rule ``nullable`` now follows, so a reader who changes one can see the other
    is the same decision.

    **Validates: Requirements 5.2, 5.3**
    """
    assert set(_detail(UPLOADED_ID)) == DETAIL_KEYS, (
        f"with no stored label the response carries exactly {sorted(DETAIL_KEYS)} — got {sorted(_detail(UPLOADED_ID))}"
    )

    _seed_sidecar(UPLOADED_ID, display_name=DISPLAY_LABEL)
    labelled = _detail(UPLOADED_ID)
    assert set(labelled) == DETAIL_KEYS | {"displayLabel"}, (
        f"a stored label adds exactly one key — got {sorted(labelled)}"
    )
    assert labelled["displayLabel"] == DISPLAY_LABEL


def test_description_and_context_keep_their_two_authorities_alongside_the_omission(two_sources):
    """C5 / `LGR-acca05d-3`: two prose keys, two authorities, unaffected by task 9.1.

    ``description`` is the **schema's own** declared description — the uploaded
    body's ``fields[].description`` (``:1379``) or Glue's ``Columns[].Comment``
    (``:517``). ``context`` is the **Context_Sidecar's** annotation and nothing
    else, merged by field name at ``:1598``. `LGR-acca05d-3` (**template /
    ratified**) accepted the second authority on the condition that the two stay
    legible under distinct headings, and ``_schema_detail``'s docstring
    (``:1552-1563``) records that task 4.5 **deleted** the tolerant fallback
    between them.

    Here because task 9.1 edited the dict literal these two keys live in: the
    conditional ``nullable`` sits between ``context`` and ``fields.append``, so a
    mis-scoped edit could plausibly have moved either of them.

    **Validates: Requirements 5.2, 5.3**
    """
    _seed_sidecar(UPLOADED_ID, field_contexts={"email": FIELD_CONTEXT})
    fields = {f["name"]: f for f in _detail(UPLOADED_ID)["fields"]}

    assert fields["email"]["description"] == "Primary contact address", "the schema body's own description"
    assert fields["email"]["context"] == FIELD_CONTEXT, "the sidecar's annotation, and only the sidecar's"
    assert fields["nickname"]["description"] == "Optional display name"
    assert fields["nickname"]["context"] == "", (
        "a field with no sidecar entry has no context, and its description is still its own"
    )

    glue_field = {f["name"]: f for f in _detail(GLUE_ROW_ID)["fields"]}["customer_id"]
    assert glue_field["description"] == "Unique customer key", "Glue `Columns[].Comment` is the schema's own"
    assert glue_field["context"] == "", (
        "a Glue table is not an Uploaded_Schema, so it can carry no Context_Sidecar — empty here is a fact "
        "about the source, not a placeholder (`:512-515`)"
    )
