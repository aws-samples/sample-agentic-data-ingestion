# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""DD4 — the listing paginates every enumeration and resolves no field lists.

Task 4.4's own characterization. Two separable changes, and this file keeps them
separable:

1. **Every enumeration is paginated** (R6.5). Five list calls —
   ``glue:GetDatabases``, ``glue:GetTables`` per database, ``s3:ListObjectsV2``,
   ``s3tables:ListTableBuckets`` and ``s3tables:ListTables`` per bucket — each
   returned exactly one page, so past each API's first page the listing silently
   under-reported. That is what made R6.1's search unable to find a schema that
   exists: the search runs client-side over the options the picker was given, so
   anything the listing dropped is unfindable. ``LGR-dd80c6d-1``
   (**backfilled**) records unbounded enumeration in ``dashboard_api`` as a
   recognised defect class already fixed for the stats and approvals handlers.
2. **No field list is resolved on the listing.** The two per-row ``s3:GetObject``
   reads — the uploaded body (``schemas.py:88``) and the Iceberg metadata
   document (``:173-175``, which also cost an ``s3tables:GetTable`` first) — moved
   to ``GET /api/schemas/{id}``. They had to move *with* the pagination:
   paginating while still resolving every field makes those reads unbounded on a
   25-second synchronous request, so the failure mode becomes a **timeout**.

**``fieldCount: null`` is load-bearing and is asserted as ``null``, not as
falsy.** A row carries a real count only where the source supplies one free —
Glue, from the ``get_tables`` page. Neither ``s3tables:ListTables``/``GetTable``
nor ``s3:ListObjectsV2`` returns a column count, so those rows carry ``None``.
Collapsing that to ``0`` would break R5.1's zero-field guard in the worst
direction: the picker would refuse **every** S3 Tables and uploaded schema as
empty. It is the same "no fields" versus "not established" distinction R1.4 and
R5.3 turn on, which is why every assertion below distinguishes ``None`` from
``0`` explicitly rather than testing truthiness.

**How far the multi-page proof actually goes, stated rather than implied.** Only
one of the five enumerations can be driven past a first page against moto, and
this file drives it:

============================ ==================================================
Enumeration                  Multi-page proof available here
============================ ==================================================
``s3:ListObjectsV2``         **Yes, and exercised.** moto paginates at
                             ``MaxKeys`` (1000 default), so 1001 objects produce
                             a genuine second page — and the test asserts the
                             unpaginated call returns exactly 1000 with
                             ``IsTruncated``, so the fixture is *shown* to exceed
                             a page rather than assumed to.
``glue:GetDatabases``        **No.** moto implements no pagination for either:
``glue:GetTables``           ``moto/glue/responses.py`` returns the whole
                             ``DatabaseList``/``TableList`` and never a
                             ``NextToken``, and ``moto/glue/models.py``'s
                             ``PAGINATION_MODEL`` has no entry for them. There is
                             no fixture that can make a second page appear.
``s3tables:ListTableBuckets`` **No.** moto paginates both at a 1000 default
``s3tables:ListTables``      (``moto/s3tables/models.py``), and seeding 1001
                             table buckets exceeds a 120-second budget —
                             measured, not assumed.
============================ ==================================================

So the four that cannot be driven past a page are covered two other ways, and
neither is "merely written": the ``continuationToken`` loop is a **pure
function** and is exercised over three real pages directly
(``test_the_continuation_token_loop_yields_every_page_and_stops_on_a_falsy_token``),
and every call site is pinned structurally
(``test_no_enumeration_in_the_listing_is_unpaginated``) so a reverted paginator
fails a test rather than passing quietly. The structural test is the weakest of
the three and it is here because the alternative is no coverage at all for those
four call sites.

**Fixtures.** Shared fixtures only: ``mock_s3_buckets`` from
``tests/fixtures/conftest.py``, which is also what provides the moto context the
Glue and S3 Tables seeding runs inside. No ``moto.mock_aws()`` call and no boto3
client mock (``tests/unit/test_no_duplicate_mocking_prop.py``, ``LGR-adceb72-1``
/ ``LGR-aadd206-1``). This file does **not** set ``AWS_REGION``
(``LGR-3e45e65-1``), which matters because ``get_schemas`` reads it at call time;
every seeding client resolves the region through that same expression, because
moto's Glue and S3 Tables backends are region-scoped and a mismatch presents as
an empty source rather than as a harness fault.

**Validates: Requirements 1.1, 1.3, 5.1, 5.6, 6.5**
"""

import contextlib
import inspect
import json
import os
import sys
from typing import Any

import boto3
import pytest

# ---------------------------------------------------------------------------
# Path and import-time env — must come before importing handlers.schemas
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(SHARED_DIR), os.path.abspath(LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Table names only — AWS_REGION and AWS_DEFAULT_REGION are deliberately absent.
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

from handlers.schemas import _collect_schema_rows, _paginate_continuation_token, get_schema, get_schemas

TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"

GLUE_DB = "sagemaker_sample_db"
GLUE_TABLE = "churn"
GLUE_COLUMNS = [
    {"Name": "customer_id", "Type": "bigint", "Comment": "Unique customer key"},
    {"Name": "churned", "Type": "boolean", "Comment": "Whether the customer left"},
]

#: A Glue table with no columns at all. Its `fieldCount` is a genuine `0` — the
#: source *did* supply a count and the count is zero — which is the other half of
#: the null-versus-zero distinction: `0` must stay reachable and must mean
#: something different from `None`.
EMPTY_GLUE_DB = "agentic_data_ingestion_emptydb"
EMPTY_GLUE_TABLE = "no_columns"

UPLOADED_ID = "uploaded_schema_1785844048926"
UPLOADED_BODY = {
    "fields": [
        {"name": "email", "type": "string", "required": True, "description": "Primary contact address"},
        {"name": "nickname", "type": "string", "required": False, "description": "Optional display name"},
    ]
}

#: An object under `shared/` whose body is not JSON. Before DD4 the listing read
#: every body and this row was silently dropped by the per-row `except`
#: (`schemas.py:119-120`). It is now listed, because no body is read — which is
#: what makes its presence a *proof* that the read is gone rather than a claim
#: about it.
UNREADABLE_ID = "not_really_json"

S3T_BUCKET = "testingbucket"
S3T_NAMESPACE = "testingbucketns"
S3T_TABLE = "testingbuckettn"
ICEBERG_FIELDS = [
    {"id": 1, "name": "order_id", "type": "long", "required": True, "doc": "Order key"},
    {"id": 2, "name": "placed_at", "type": "timestamp", "required": False, "doc": "When the order was placed"},
]

GLUE_ROW_ID = f"glue_{GLUE_DB}_{GLUE_TABLE}"
EMPTY_GLUE_ROW_ID = f"glue_{EMPTY_GLUE_DB}_{EMPTY_GLUE_TABLE}"
S3T_ROW_ID = f"s3_table_{S3T_BUCKET}_{S3T_NAMESPACE}_{S3T_TABLE}"


# ---------------------------------------------------------------------------
# Seeding — real clients against the shared fixture's moto backend
# ---------------------------------------------------------------------------
def _client(service: str):
    """A client in exactly the region ``get_schemas`` will use (``LGR-3e45e65-1``).

    ``os.environ.get("AWS_REGION", "us-west-2")`` is the handler's own
    expression, so seeding and handler cannot land in different regions. moto's
    Glue and S3 Tables backends are region-scoped and a mismatch presents as an
    empty source, which the listing's swallowing ``except`` clauses make
    indistinguishable from a requirement disagreement.
    """
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _ensure_bucket(name: str) -> None:
    s3 = _client("s3")
    kwargs: dict[str, Any] = {"Bucket": name}
    region = s3.meta.region_name
    if region and region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou, s3.exceptions.BucketAlreadyExists):
        s3.create_bucket(**kwargs)


def _seed_glue_table(database: str, table: str, columns: list[dict] | None = None) -> None:
    glue = _client("glue")
    with contextlib.suppress(glue.exceptions.AlreadyExistsException):
        glue.create_database(DatabaseInput={"Name": database})
    glue.create_table(
        DatabaseName=database,
        TableInput={
            "Name": table,
            "TableType": "EXTERNAL_TABLE",
            "StorageDescriptor": {"Columns": columns or [], "Location": f"s3://{database}/{table}"},
        },
    )


def _seed_uploaded_object(schema_id: str, body: str) -> None:
    """Write one object at ``shared/{id}.json`` (``LGR-cad8032-1``), body as given.

    Takes the body as a **string** so an unparseable one can be seeded, which is
    what proves the listing no longer reads it.
    """
    _client("s3").put_object(
        Bucket=TARGET_SCHEMAS_BUCKET, Key=f"shared/{schema_id}.json", Body=body, ContentType="application/json"
    )


def _seed_s3_table(bucket: str, namespace: str, table: str, iceberg_fields: list[dict]) -> None:
    """One S3 Tables Iceberg table with a committed metadata document.

    The metadata is seeded where the code looks for it — ``s3:GetObject`` on the
    warehouse bucket. ``LGR-2eaf8a3-1`` (**backfilled**) fences that read to the
    S3 Tables Iceberg REST ``loadTable`` path instead, and ``requirements.md``
    keeps it "IN as a constraint, OUT as a fix", so seeding it characterizes the
    branch that exists and is not an endorsement of the read path.
    """
    s3tables = _client("s3tables")
    arn = s3tables.create_table_bucket(name=bucket)["arn"]
    s3tables.create_namespace(tableBucketARN=arn, namespace=[namespace])
    created = s3tables.create_table(tableBucketARN=arn, namespace=namespace, name=table, format="ICEBERG")

    warehouse = s3tables.get_table(tableBucketARN=arn, namespace=namespace, name=table)["warehouseLocation"]
    metadata_bucket = warehouse.removeprefix("s3://")
    metadata_key = f"metadata/00000-{table}.metadata.json"
    _ensure_bucket(metadata_bucket)
    _client("s3").put_object(
        Bucket=metadata_bucket,
        Key=metadata_key,
        Body=json.dumps(
            {
                "format-version": 2,
                "current-schema-id": 0,
                "schemas": [{"schema-id": 0, "type": "struct", "fields": iceberg_fields}],
            }
        ),
    )
    s3tables.update_table_metadata_location(
        tableBucketARN=arn,
        namespace=namespace,
        name=table,
        versionToken=created["versionToken"],
        metadataLocation=f"{warehouse}/{metadata_key}",
    )


# ---------------------------------------------------------------------------
# Reading the response
# ---------------------------------------------------------------------------
def _rows(response: dict) -> list[dict]:
    assert response["statusCode"] == 200, f"expected 200 from get_schemas, got {response}"
    return json.loads(response["body"])["schemas"]


def _row(rows: list[dict], schema_id: str) -> dict:
    for row in rows:
        if row.get("schemaId") == schema_id:
            return row
    raise AssertionError(f"{schema_id} is not in the listing — ids: {[r.get('schemaId') for r in rows]}")


@pytest.fixture
def listing_env(mock_s3_buckets, monkeypatch):
    """The shared fixture's moto backend plus the two env vars the listing reads."""
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    # Keeps get_aws_account_id() off STS entirely.
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    return mock_s3_buckets


@pytest.fixture
def all_three_sources(listing_env):
    """All three sources seeded, including a column-less Glue table and a bad body."""
    _seed_glue_table(GLUE_DB, GLUE_TABLE, GLUE_COLUMNS)
    _seed_glue_table(EMPTY_GLUE_DB, EMPTY_GLUE_TABLE)
    _seed_uploaded_object(UPLOADED_ID, json.dumps(UPLOADED_BODY))
    _seed_uploaded_object(UNREADABLE_ID, "this is not json at all")
    _seed_s3_table(S3T_BUCKET, S3T_NAMESPACE, S3T_TABLE, ICEBERG_FIELDS)
    return get_schemas()


# ---------------------------------------------------------------------------
# fieldCount — a real count where it is free, null where it is not
# ---------------------------------------------------------------------------
def test_field_count_is_a_real_count_for_glue_and_null_for_the_two_lazy_sources(all_three_sources):
    """DD4: ``fieldCount`` where the source supplies it free, ``None`` where it does not.

    Glue's count comes out of the ``get_tables`` page, so it is exact and costs
    no read. S3 Tables and uploaded have no count available at enumeration time —
    ``list_tables``/``get_table`` return a metadata *location*, not a column
    count, and ``list_objects_v2`` returns key, size and ``LastModified`` — so
    those rows carry ``None``.

    **Validates: Requirements 5.6, 6.5**
    """
    rows = _rows(all_three_sources)

    assert _row(rows, GLUE_ROW_ID)["fieldCount"] == len(GLUE_COLUMNS)

    for schema_id in (UPLOADED_ID, S3T_ROW_ID):
        count = _row(rows, schema_id)["fieldCount"]
        assert count is None, (
            f"{schema_id} carries fieldCount={count!r}. Neither source supplies a "
            "column count at enumeration time, so the only honest value is null."
        )


def test_field_count_is_null_and_never_zero_where_no_count_was_established(all_three_sources):
    """DD4: ``null`` and ``0`` are different claims, and ``0`` must stay reachable.

    This is the assertion the whole decision turns on. Collapsing ``null`` to
    ``0`` would break R5.1's zero-field guard in the worst possible direction —
    the picker refuses a schema that declares zero fields, so every S3 Tables and
    uploaded schema would be refused as empty. Written as "carries no number at
    all" rather than ``!= 0``, because ``bool`` is an ``int`` subclass and
    ``0 == False``, so both an equality test and a truthiness test would let one
    of the two wrong values through.

    The column-less Glue table is the other half: ``0`` is a real answer from a
    source that really did supply a count, and it must not have been turned into
    ``None`` to make the two cases uniform.

    **Validates: Requirements 1.4, 5.1, 5.3**
    """
    rows = _rows(all_three_sources)

    for schema_id in (UPLOADED_ID, S3T_ROW_ID):
        count = _row(rows, schema_id)["fieldCount"]
        assert not isinstance(count, int), (
            f"{schema_id} carries fieldCount={count!r}, which asserts the schema declares "
            f"{count} fields. It asserts nothing of the kind: no count was established. "
            "(isinstance, not != 0: bool subclasses int, so False would slip past ==.)"
        )

    empty_glue = _row(rows, EMPTY_GLUE_ROW_ID)["fieldCount"]
    assert empty_glue == 0 and empty_glue is not None, (
        f"a Glue table with no columns must report fieldCount 0, not {empty_glue!r} — "
        "the count was established and it is zero"
    )


# ---------------------------------------------------------------------------
# The two per-row reads are gone from the listing, and work on the per-id path
# ---------------------------------------------------------------------------
def test_the_listing_carries_no_field_list_for_the_two_lazy_sources(all_three_sources):
    """DD4: no ``fields`` key at all on an uploaded or S3 Tables row.

    Absent rather than empty, for the same reason ``fieldCount`` is ``null``
    rather than ``0``: an empty list is a claim that the schema declares no
    fields, and the listing is not in a position to make it. Glue keeps its list,
    because it was free.

    **Validates: Requirements 1.3, 6.5**
    """
    rows = _rows(all_three_sources)

    for schema_id in (UPLOADED_ID, S3T_ROW_ID):
        row = _row(rows, schema_id)
        assert "fields" not in row, (
            f"{schema_id} carries fields={row.get('fields')!r} on the listing. An empty "
            "list is indistinguishable from a schema that declares none (R1.4)."
        )

    assert [f["name"] for f in _row(rows, GLUE_ROW_ID)["fields"]] == [c["Name"] for c in GLUE_COLUMNS], (
        "the Glue field list came free in the get_tables page and stays on the listing"
    )


def test_an_unreadable_uploaded_body_is_listed_which_proves_no_body_is_read(all_three_sources):
    """DD4: the listing no longer reads each uploaded body — shown, not asserted.

    ``shared/not_really_json.json`` holds a body that is not JSON. Before this
    change the listing read every body and this row was dropped by the per-row
    ``except`` (``schemas.py:119-120``), so it could not appear. Its presence is
    therefore positive evidence that the ``s3:GetObject`` per uploaded schema is
    gone, which no assertion about the *absence* of a key could establish on its
    own.

    It is listed with ``fieldCount: None`` — the object exists, so R1.2 forbids
    pretending the id does not; no count is established, so ``None`` is the only
    honest value.

    **Validates: Requirements 1.2, 6.5**
    """
    row = _row(_rows(all_three_sources), UNREADABLE_ID)
    assert row["source"] == "uploaded"
    assert row["fieldCount"] is None
    assert "fields" not in row


def test_the_per_id_endpoint_resolves_the_field_lists_the_listing_no_longer_does(all_three_sources):
    """DD4: both moved reads happen on ``GET /api/schemas/{id}`` and produce real fields.

    The pairing is the point: the same fixture that shows the listing carrying no
    field list shows the per-id endpoint carrying the full one, so the fields were
    **moved** rather than lost. The two field-extraction tests in
    ``test_schemas_listing_sources.py`` characterize the extraction itself under
    their original names; this asserts the routing of the work.

    **Validates: Requirements 1.1, 1.3**
    """
    assert all_three_sources["statusCode"] == 200

    uploaded = json.loads(get_schema(UPLOADED_ID)["body"])
    assert [f["name"] for f in uploaded["fields"]] == ["email", "nickname"]

    s3_tables = json.loads(get_schema(S3T_ROW_ID)["body"])
    assert [f["name"] for f in s3_tables["fields"]] == ["order_id", "placed_at"]

    glue = json.loads(get_schema(GLUE_ROW_ID)["body"])
    assert [f["name"] for f in glue["fields"]] == [c["Name"] for c in GLUE_COLUMNS], (
        "the Glue detail comes off the row it already had; no read was added for it"
    )


# ---------------------------------------------------------------------------
# R6.5 — pagination, exercised where a second page can be produced
# ---------------------------------------------------------------------------
def test_the_uploaded_source_enumerates_past_the_first_page_of_list_objects_v2(listing_env):
    """R6.5: 1001 uploaded schemas, and all 1001 are listed.

    The real multi-page proof. ``list_objects_v2`` returns at most 1000 keys per
    page, so the unpaginated call this replaces returned exactly 1000 and set
    ``IsTruncated`` — asserted below, so the fixture is *shown* to exceed one page
    rather than assumed to — and the 1001st schema was invisible to the picker and
    therefore unfindable by R6.1's search.

    1001 rows is also why the two per-row reads had to go with the pagination:
    each row would otherwise have cost an ``s3:GetObject`` on a synchronous
    25-second request.

    **Validates: Requirement 6.5**
    """
    page_size = 1000
    ids = [f"uploaded_schema_{index:05d}" for index in range(page_size + 1)]
    for schema_id in ids:
        _seed_uploaded_object(schema_id, "{}")

    # The fixture really does exceed one page — the state the old code saw.
    unpaginated = _client("s3").list_objects_v2(Bucket=TARGET_SCHEMAS_BUCKET, Prefix="shared/")
    assert len(unpaginated["Contents"]) == page_size and unpaginated["IsTruncated"], (
        "this fixture is meant to exceed one page of list_objects_v2; "
        f"got {len(unpaginated.get('Contents', []))} keys, IsTruncated={unpaginated.get('IsTruncated')}"
    )

    listed = {row["schemaId"] for row in _rows(get_schemas()) if row["source"] == "uploaded"}
    missing = [schema_id for schema_id in ids if schema_id not in listed]
    assert not missing, (
        f"{len(missing)} of {len(ids)} uploaded schemas are absent from the listing, so the "
        f"enumeration stopped at a page boundary — first missing: {missing[:3]}"
    )


def test_the_continuation_token_loop_yields_every_page_and_stops_on_a_falsy_token():
    """R6.5: the S3 Tables loop, exercised over three real pages.

    ``_paginate_continuation_token`` is a pure function over a list callable, so
    it can be driven past a page boundary directly — which matters, because moto
    paginates both S3 Tables list operations at a 1000 default and seeding 1001
    table buckets exceeds a 120-second budget (measured). This is the only
    multi-page exercise those two call sites can get, and it is the hand-rolled
    loop rather than boto3's, so it is also the one most worth having.

    Three things are pinned: every item of every page is yielded in order; the
    **first** call carries no ``continuationToken`` (passing one would be an
    invalid-token error against the real service); and a falsy token ends the
    loop rather than a missing key alone.

    The callable is a plain function, not a boto3 client mock — nothing here
    stubs an AWS client (``tests/unit/test_no_duplicate_mocking_prop.py``).

    **Validates: Requirement 6.5**
    """
    pages = [
        {"tableBuckets": [{"name": "a"}, {"name": "b"}], "continuationToken": "t1"},
        {"tableBuckets": [{"name": "c"}], "continuationToken": "t2"},
        # A falsy token, not an absent one: the last page of the real API returns
        # the key and the loop must stop on the value rather than on the key.
        {"tableBuckets": [{"name": "d"}], "continuationToken": ""},
    ]
    calls: list[dict] = []

    def list_call(**kwargs):
        calls.append(kwargs)
        return pages[len(calls) - 1]

    items = list(_paginate_continuation_token(list_call, "tableBuckets", tableBucketARN="arn:aws:s3tables:::bucket/x"))

    assert [item["name"] for item in items] == ["a", "b", "c", "d"]
    assert len(calls) == len(pages), f"expected one call per page, got {calls}"
    assert "continuationToken" not in calls[0], f"the first call must carry no token, got {calls[0]}"
    assert [call.get("continuationToken") for call in calls[1:]] == ["t1", "t2"]
    assert all(call["tableBucketARN"] == "arn:aws:s3tables:::bucket/x" for call in calls), (
        f"the caller's kwargs must be forwarded on every page, got {calls}"
    )


def test_an_empty_first_page_with_no_token_terminates():
    """R6.5: the loop terminates on an empty result rather than spinning.

    The degenerate case — an account with no table buckets — and it is worth one
    assertion because a ``while True`` loop that reads the token from the wrong
    key would not terminate here, and a non-terminating enumeration on a
    25-second Lambda is the timeout DD4 exists to avoid.

    **Validates: Requirement 6.5**
    """
    calls: list[dict] = []

    def list_call(**kwargs):
        calls.append(kwargs)
        return {}

    assert list(_paginate_continuation_token(list_call, "tableBuckets")) == []
    assert len(calls) == 1


def test_no_enumeration_in_the_listing_is_unpaginated():
    """R6.5: all five list calls go through one of the two pagination helpers.

    Structural, and it is here because four of the five call sites have no
    reachable multi-page fixture — moto implements no pagination at all for
    ``glue:GetDatabases`` / ``glue:GetTables``, and paginates the two S3 Tables
    operations at a 1000 default that cannot be seeded inside a sane budget (see
    the module docstring's table). Without this, reverting a paginator on any of
    those four would pass every other test in this file.

    It is deliberately the weakest of the three pagination assertions and it does
    not stand alone: the ``continuationToken`` loop is exercised over three pages
    above, and ``s3:ListObjectsV2`` is exercised over a real second page.

    **Validates: Requirement 6.5**
    """
    source = inspect.getsource(_collect_schema_rows)

    # The three that have a boto3 paginator go through `_paginated_items`.
    for operation, result_key in (
        ("get_databases", "DatabaseList"),
        ("get_tables", "TableList"),
        ("list_objects_v2", "Contents"),
    ):
        assert f'"{operation}", "{result_key}"' in source, (
            f"{operation} must be enumerated through _paginated_items with result key "
            f"{result_key!r} — R6.5, LGR-dd80c6d-1"
        )

    # The two S3 Tables operations go through the continuation-token loop.
    for operation, result_key in (
        ("list_table_buckets", "tableBuckets"),
        ("list_tables", "tables"),
    ):
        assert f'{operation}, "{result_key}"' in source, (
            f"{operation} must be enumerated through _paginate_continuation_token with result key {result_key!r} — R6.5"
        )

    # And none of the five is called directly any more. A direct call is what the
    # pre-p35 code did, and it is a one-line revert away.
    for unpaginated in (
        "glue_client.get_databases(",
        "glue_client.get_tables(",
        "s3_client.list_objects_v2(",
        "s3tables_client.list_table_buckets(",
        "s3tables_client.list_tables(",
    ):
        assert unpaginated not in source, (
            f"{unpaginated} is an unpaginated enumeration — it returns one page and "
            "silently under-reports past it (R6.5)"
        )


def test_no_per_row_read_remains_in_the_listing():
    """DD4: neither per-row read is reachable from the enumeration any more.

    The counterpart to the assertion above, and the one that keeps the rejected
    option rejected: "paginate and keep resolving every field" is exactly what
    reintroducing either of these calls would be, and it turns the endpoint's
    failure mode into a timeout on an unbounded row count.

    ``s3tables:GetTable`` is included because the Iceberg read cost that call
    first, so it is two calls per row rather than one.

    **Validates: Requirements 1.3, 6.5**
    """
    source = inspect.getsource(_collect_schema_rows)

    for per_row_read in ("get_object(", "get_table("):
        assert per_row_read not in source, (
            f"{per_row_read} in the enumeration is a per-row read. Paginating while "
            "resolving every field makes it unbounded on a 25s synchronous request "
            "(DD4's rejected option); it belongs on GET /api/schemas/{id}."
        )
