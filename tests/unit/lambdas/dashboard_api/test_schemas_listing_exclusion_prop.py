# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 2 — the Glue-mirror exclusion removes exactly the mirrors.

# Feature: p35-schema-surfaces, Property 2: Exclusion removes exactly the mirrors and never a real schema

For any set of `s3_table_bucket` rows and any set of `glue_catalog` rows,
`Schema_Listing`'s response contains **every** `s3_table_bucket` row, **every**
`glue_catalog` row whose `(database, table)` pair no `s3_table_bucket` row
derives, and **no** `glue_catalog` row whose pair some `s3_table_bucket` row does
derive — including when two `s3_table_bucket` rows derive the **same** pair.

**Two subjects, deliberately.** The property runs against
``_exclude_glue_mirrors`` — the post-pass itself, the real production function —
because that is what the property is *about*, and because 100+ examples over a
seeded S3 Tables + Glue backend would be a slow test of enumeration rather than
a sharp test of exclusion. Two example tests below then run **end to end through
``get_schemas``** against the shared moto fixture, so the wiring the property
cannot see is pinned too: that the identifiers are carried onto the rows at all,
that the post-pass is actually called, and that the internal keys never cross the
API boundary.

**The oracle is R5.7's two expressions, written out from ``requirements.md``, not
imported from the code under test.** ``_r57_mirror_pair`` below spells out
``agentic_data_ingestion_{bucket_name with '-' replaced by '_'}`` and
``{namespace}_{table_name}`` — criterion 5.7 verbatim, the same two expressions
``create_infrastructure/lambda_function.py:272`` and ``:273`` write. Calling
``shared/glue_mirror_identifiers`` as the oracle would make the test its own
subject's mirror; task 4.1's ``tests/unit/shared/test_glue_mirror_identifiers.py``
is where that derivation is tested on its own terms, hyphens and underscores
included.

**Namespace and table names contain underscores, and that is the point** — DD7's
axis 2, the reachable collision the requirements did not name.
``catalog_table = f"{namespace}_{table_name}"`` joins two values that both permit
``_``, so namespace ``sales`` + table ``orders_2026`` and namespace
``sales_orders`` + table ``2026`` derive the same ``sales_orders_2026``. Both are
nameable. Generating those collisions is the **only** way this property
distinguishes an idempotent ``set`` filter from a list-based one that could drop
a row twice — and it is why the generators draw both segments from a small pool
of shared tokens rather than from a wide random alphabet, which would essentially
never collide. DD7's axis 1, the database name, is **unreachable**: an S3 Tables
``TableBucketName`` cannot contain ``_`` (``[0-9a-z-]*``), so two buckets
differing only by ``-`` versus ``_`` cannot both exist. Bucket names are
generated with hyphens anyway, because the lossy ``replace('-','_')`` is what
makes the derivation the mirror's rather than the bucket's.

**The Glue side generates three kinds of database**, so the "never a real schema"
half is reachable rather than assumed: a mirror-shaped name derived from a
generated bucket, a per-pipeline ``agentic_data_ingestion_{pipeline_id}`` name
from ``create_glue_pipeline/iam_roles.py:673``'s **different** scheme, and an
unrelated name. The second is the one a database-name prefix test would wrongly
hide, so a prefix implementation fails this property rather than passing it.

**Uploaded rows are generated too.** "Never a real schema" is a claim about all
three sources, and the filter targets `glue_catalog` rows only — an
implementation that dropped an `uploaded` row on a name coincidence would be
caught here.

**This property was checked against the implementations the design rejects, not
only against the one it prescribes.** Over 400 examples of the generator below, a
database-name **prefix** test disagreed with the oracle on 313, a
**database-only** match on 203, a **list-based** non-idempotent filter on 162,
and a **reverse-direction** filter (excluding the S3 Tables row instead of the
mirror) on 187 — while the real ``_exclude_glue_mirrors`` disagreed on **0**. So
the property distinguishes the four wrong answers from the right one rather than
merely accepting the right one. That survey was a throwaway; the ``event()``
calls it motivated are permanent, because the generator's first version reached
the exclusion case in **0 of 200 examples** and passed anyway.

No mocks and no fabricated response: the property calls the production function
directly, and the two end-to-end tests seed the shared ``mock_s3_buckets``
fixture's moto backend through real clients. There is no ``moto.mock_aws()`` call
and no boto3 client mock in this file
(``tests/unit/test_no_duplicate_mocking_prop.py``), and it does **not** set
``AWS_REGION`` (`LGR-3e45e65-1`) — every seeding client resolves the region
through the same expression ``get_schemas`` uses (``schemas.py:23``), because
moto's Glue and S3 Tables backends are region-scoped and a mismatch would present
as an empty source rather than as a harness fault.

**Validates: Requirements 5.3, 5.4, 5.7, 5.8, 8.10**
"""

import contextlib
import copy
import json
import os
import sys
from typing import Any

import boto3
import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

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

from handlers.schemas import _INTERNAL_ROW_KEYS, _exclude_glue_mirrors, get_schemas

TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"


# ---------------------------------------------------------------------------
# The oracle — R5.7's two expressions, from requirements.md
# ---------------------------------------------------------------------------
def _r57_mirror_pair(bucket_name: str, namespace: str, table_name: str) -> tuple[str, str]:
    """Criterion 5.7's derivation, written out rather than imported.

    ``database`` is ``agentic_data_ingestion_{bucket_name with '-' replaced by
    '_'}`` and ``table`` is ``{namespace}_{table_name}`` — the pair
    ``create_infrastructure`` writes at ``:272`` and ``:273``. Spelled out here
    so the property's oracle is the requirement, not the helper the code under
    test calls.
    """
    return f"agentic_data_ingestion_{bucket_name.replace('-', '_')}", f"{namespace}_{table_name}"


def _oracle_kept_probes(rows: list[dict[str, Any]]) -> list[str]:
    """The rows R5.3/R5.4/R5.7/R5.8 say survive, in the order they arrived.

    A `glue_catalog` row goes only when a **concrete** enumerated
    `s3_table_bucket` row derives its pair — the direction is S3-Tables-row →
    Glue-row and never the reverse (R5.7). ``mirrored`` is a set, so two S3
    Tables rows deriving one pair exclude one Glue row **once** (R5.8), and no
    `s3_table_bucket` or `uploaded` row is ever a filter target.

    Identity is the ``probe`` key, not ``schemaId``. Two *distinct* S3 Tables
    triples that derive one pair also collapse to one ``schemaId`` —
    ``s3_table_{bucket}_{ns}_{table}`` is the same ambiguous underscore join as
    ``glue_{db}_{table}``, which is precisely why the design forbids matching on
    it — so ``schemaId`` cannot tell those two rows apart and cannot serve as a
    test identity. ``probe`` is unique per row and nothing in the code under test
    reads it.
    """
    mirrored = {
        _r57_mirror_pair(row["_bucket_name"], row["_namespace"], row["_table_name"])
        for row in rows
        if row["source"] == "s3_table_bucket"
    }
    return [
        row["probe"]
        for row in rows
        if row["source"] != "glue_catalog" or (row["_glue_database"], row["_glue_table"]) not in mirrored
    ]


# ---------------------------------------------------------------------------
# Row builders — the post-pass's input contract, not the producer's output
# ---------------------------------------------------------------------------
# `_exclude_glue_mirrors`' contract is `source` plus the carried underscore-keyed
# identifiers; the ids below exist only so the assertion can name which row moved.
# The two end-to-end tests are what check the real producer carries these keys.
def _s3_row(bucket: str, namespace: str, table: str) -> dict[str, Any]:
    return {
        "schemaId": f"s3_table_{bucket}_{namespace}_{table}",
        "source": "s3_table_bucket",
        "_bucket_name": bucket,
        "_namespace": namespace,
        "_table_name": table,
    }


def _glue_row(database: str, table: str) -> dict[str, Any]:
    return {
        "schemaId": f"glue_{database}_{table}",
        "source": "glue_catalog",
        "_glue_database": database,
        "_glue_table": table,
    }


def _uploaded_row(schema_id: str) -> dict[str, Any]:
    return {"schemaId": schema_id, "source": "uploaded"}


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------
# A small shared token pool for namespaces and table names, so DD7 axis 2's
# collision — namespace `sales` + table `orders_2026` against namespace
# `sales_orders` + table `2026` — is actually *reachable*. Drawing both segments
# from a wide alphabet would make the collision astronomically unlikely, and a
# property that never generates the case it is about cannot fail on it.
UNDERSCORE_TOKENS = ("sales", "orders", "2026", "a", "b", "ns")

# TableBucketName: min 3, max 63, pattern [0-9a-z-]* — no underscore, which is
# DD7 axis 1's unreachability. Hyphens are generated deliberately: the lossy
# `replace('-','_')` is what makes the derived database the mirror's name.
bucket_names = st.builds(
    "-".join,
    st.lists(st.sampled_from(("e2e", "s3tables", "hello123", "testingbucket", "newtestbn")), min_size=1, max_size=3),
)

# NamespaceName / TableName: pattern [0-9a-z_]* — underscores permitted, hyphens
# not. 1-3 tokens joined by "_", which is what makes the concatenation ambiguous.
underscore_names = st.builds(
    "_".join,
    st.lists(st.sampled_from(UNDERSCORE_TOKENS), min_size=1, max_size=3),
)

s3_triples = st.tuples(bucket_names, underscore_names, underscore_names)

# Glue databases that are NOT any mirror — the "never a real schema" half.
#   1. per-pipeline (create_glue_pipeline/iam_roles.py:673's DIFFERENT scheme):
#      shares the `agentic_data_ingestion_` prefix, must SURVIVE — this is the
#      prefix-test tripwire, and it is why matching on a prefix fails here
#   2. unrelated: must survive
per_pipeline_databases = st.builds(
    lambda name, suffix: f"agentic_data_ingestion_{name}_{suffix}",
    st.sampled_from(("hello123", "testing", "orders")),
    st.sampled_from(("c4827272", "39eddb17", "1a2b3c4d")),
)
unrelated_databases = st.sampled_from(("sagemaker_sample_db", "default", "jdbc_discovery"))
non_mirror_pairs = st.tuples(st.one_of(per_pipeline_databases, unrelated_databases), underscore_names)

uploaded_ids = st.sampled_from(("uploaded_schema_1785844048926", "customers_jsonschema", "agentic_data_ingestion_a_b"))


@st.composite
def colliding_triples(draw):
    """Two S3 Tables triples in one bucket that derive the **same** Glue pair.

    DD7's axis 2, constructed rather than hoped for. One token list is split at
    two **distinct** points into ``(namespace, table)``: ``sales`` +
    ``orders_2026`` and ``sales_orders`` + ``2026`` are different tables in
    different namespaces that both concatenate to ``sales_orders_2026``
    (``create_infrastructure:273``). Distinct split points make the two triples
    genuinely different rows, which is R5.8's case, and it is the only way to
    tell an idempotent ``set`` filter from a list-based one.
    """
    bucket = draw(bucket_names)
    # At least three tokens, so there are at least two distinct split points,
    # and `unique=True` takes two of them — which makes the two triples
    # genuinely distinct rather than identical-and-deduped. With `min_size=2` the
    # only split point is 1, so the pair collapsed and R5.8's case was reached in
    # 8% of examples instead of 50%.
    tokens = draw(st.lists(st.sampled_from(UNDERSCORE_TOKENS), min_size=3, max_size=5))
    splits = draw(st.lists(st.integers(min_value=1, max_value=len(tokens) - 1), min_size=2, max_size=2, unique=True))
    return [(bucket, "_".join(tokens[:split]), "_".join(tokens[split:])) for split in splits]


@st.composite
def listings(draw):
    """A whole enumerated listing: S3 Tables rows, Glue rows, uploaded rows.

    **The Glue rows are drawn partly *from* the S3 Tables rows**, through R5.7's
    own two expressions, because otherwise this property tests nothing. Measured:
    with the Glue database and the Glue table drawn independently, a Glue row
    that is *actually* a mirror of a generated S3 Tables table came up in **0 of
    200 examples** — both halves of the pair have to coincide, and the mirror
    table name is a 2-6 token join while an independently drawn one is 1-3. The
    property passed, and it passed by never exercising the exclusion at all.
    ``--hypothesis-show-statistics`` is what turned that up, and the events
    reported in ``_report_generated_cases`` are what keep it visible.

    So the Glue side is four kinds: exact mirrors of enumerated triples (must
    go), the same database with a different table and the same table under a
    different database (both near misses that must survive), and non-mirror
    databases including the per-pipeline scheme (must survive).

    Identical triples and identical Glue pairs are collapsed, because neither
    source can produce them — S3 Tables cannot hold two tables of one name in
    one namespace, and Glue cannot hold two tables of one name in one database.
    **Distinct** triples deriving the **same** pair are deliberately left in;
    they are R5.8's case.

    Rows are emitted Glue-first, then uploaded, then S3 Tables, which is the
    order ``get_schemas`` appends them (`:26-72`, `:75-129`, `:132-215`) and the
    reason the exclusion has to be a post-pass at all.
    """
    triples = draw(st.lists(s3_triples, min_size=0, max_size=4))
    if draw(st.booleans()):
        triples += draw(colliding_triples())
    triples = list(dict.fromkeys(triples))

    pairs = draw(st.lists(non_mirror_pairs, min_size=0, max_size=3))

    # The mirror and near-miss halves, derived from what was enumerated above.
    if triples:
        mirror_pairs = [_r57_mirror_pair(*triple) for triple in triples]
        pairs += draw(st.lists(st.sampled_from(mirror_pairs), min_size=0, max_size=3))
        # Right database, wrong table.
        pairs += draw(
            st.lists(
                st.tuples(st.sampled_from([database for database, _ in mirror_pairs]), underscore_names),
                min_size=0,
                max_size=2,
            )
        )
        # Right table, wrong database.
        pairs += draw(
            st.lists(
                st.tuples(
                    st.one_of(per_pipeline_databases, unrelated_databases),
                    st.sampled_from([table for _, table in mirror_pairs]),
                ),
                min_size=0,
                max_size=2,
            )
        )

    uploads = draw(st.lists(uploaded_ids, min_size=0, max_size=2))

    pairs = list(dict.fromkeys(pairs))
    uploads = list(dict.fromkeys(uploads))

    rows = (
        [_glue_row(database, table) for database, table in pairs]
        + [_uploaded_row(schema_id) for schema_id in uploads]
        + [_s3_row(bucket, namespace, table) for bucket, namespace, table in triples]
    )
    # A unique, readable identity per row — see `_oracle_kept_probes` for why
    # `schemaId` cannot be one. Not underscore-prefixed, so it is visibly not one
    # of the carried identifiers, and the code under test never reads it.
    for index, row in enumerate(rows):
        row["probe"] = f"{index}:{row['schemaId']}"
    return rows


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------
def _report_generated_cases(rows: list[dict[str, Any]]) -> None:
    """Record which of the cases this property is about the example reached."""
    derived: dict[tuple[str, str], int] = {}
    for row in rows:
        if row["source"] == "s3_table_bucket":
            pair = _r57_mirror_pair(row["_bucket_name"], row["_namespace"], row["_table_name"])
            derived[pair] = derived.get(pair, 0) + 1
            if "-" in row["_bucket_name"]:
                event("bucket name contains a hyphen (the lossy derivation)")
            if "_" in row["_namespace"] or "_" in row["_table_name"]:
                event("namespace or table name contains an underscore (DD7 axis 2)")

    if any(count > 1 for count in derived.values()):
        event("two distinct S3 Tables triples derive the SAME pair (R5.8)")

    kept = set(_oracle_kept_probes(rows))
    for row in rows:
        if row["source"] != "glue_catalog":
            continue
        if row["probe"] not in kept:
            event("a Glue mirror is excluded")
        elif row["_glue_database"].startswith("agentic_data_ingestion_"):
            event("a mirror-prefixed Glue database is KEPT (the prefix-test tripwire)")


@given(rows=listings())
@settings(max_examples=200)
def test_exclusion_removes_exactly_the_mirrors_and_never_a_real_schema(rows):
    """Property 2: exclusion removes exactly the mirrors and never a real schema.

    One equality carries all three clauses plus R5.8's idempotence: the post-pass
    is a filter, so the surviving ids in order are exactly the oracle's surviving
    ids in order. Every `s3_table_bucket` row survives, every `glue_catalog` row
    no S3 Tables row accounts for survives, every mirrored one goes, and a pair
    two S3 Tables rows both derive is excluded once rather than twice.

    **Validates: Requirements 5.3, 5.4, 5.7, 5.8, 8.10**
    """
    # Coverage is *reported*, not assumed: `pytest --hypothesis-show-statistics`
    # shows how often each case was actually reached. A property that never
    # generates a mirror to exclude, or never generates two triples deriving one
    # pair, passes without testing anything — and the second of those is DD7's
    # axis 2, the whole reason the generators use a shared token pool.
    _report_generated_cases(rows)

    expected = _oracle_kept_probes(rows)

    kept = _exclude_glue_mirrors(copy.deepcopy(rows))
    kept_probes = [row["probe"] for row in kept]

    assert kept_probes == expected, (
        "the exclusion did not remove exactly the mirrors.\n"
        f"  kept:     {kept_probes}\n"
        f"  expected: {expected}\n"
        f"  dropped by the code but kept by R5.7: {sorted(set(expected) - set(kept_probes))}\n"
        f"  kept by the code but mirrored per R5.7: {sorted(set(kept_probes) - set(expected))}"
    )

    # No `s3_table_bucket` and no `uploaded` row is ever a filter target (R5.8's
    # "can never suppress a real schema"), asserted separately so that half of
    # the property names itself when it breaks rather than hiding inside the
    # equality above.
    real = [row["probe"] for row in rows if row["source"] != "glue_catalog"]
    missing = [probe for probe in real if probe not in kept_probes]
    assert not missing, f"R5.8: the exclusion dropped rows that are not glue_catalog mirrors: {missing}"

    # The carried identifiers are internal and must not survive into a response.
    leaked = sorted({key for row in kept for key in _INTERNAL_ROW_KEYS if key in row})
    assert not leaked, f"internal identifier keys survived the post-pass: {leaked}"


def test_a_mirror_shaped_database_with_no_s3_tables_table_survives():
    """A Glue row is excluded only when a concrete S3 Tables row derives its pair.

    The pair is mirror-*shaped* — right database, right prefix — but no
    `s3_table_bucket` row is present at all, so nothing accounts for it and R5.7's
    direction requires it to survive. This is the property's counter-case stated
    as an example: an implementation that recognised mirrors by their name rather
    than matching them against an enumerated table would drop this row.

    **Validates: Requirements 5.7, 5.8**
    """
    rows = [_glue_row("agentic_data_ingestion_testingbucket", "testingbucketns_testingbuckettn")]
    kept = _exclude_glue_mirrors(copy.deepcopy(rows))
    assert [row["schemaId"] for row in kept] == [rows[0]["schemaId"]], (
        "no s3_table_bucket row is present, so nothing accounts for this Glue row and R5.7 keeps it"
    )


# ---------------------------------------------------------------------------
# End to end — the wiring the property cannot see
# ---------------------------------------------------------------------------
def _client(service: str):
    """A client in exactly the region ``get_schemas`` will use (`LGR-3e45e65-1`).

    ``os.environ.get("AWS_REGION", "us-west-2")`` is `schemas.py:23` verbatim, so
    the seeding and the handler cannot land in different regions. moto's Glue and
    S3 Tables backends are region-scoped, and a mismatch presents as an empty
    source, which ``get_schemas``' swallowing ``except`` clauses make
    indistinguishable from a requirement disagreement.
    """
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _seed_glue_table(database: str, table: str) -> None:
    glue = _client("glue")
    with contextlib.suppress(glue.exceptions.AlreadyExistsException):
        glue.create_database(DatabaseInput={"Name": database})
    glue.create_table(
        DatabaseName=database,
        TableInput={
            "Name": table,
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {"table_type": "ICEBERG"},
            "StorageDescriptor": {"Columns": [], "Location": f"s3://{database}/{table}"},
        },
    )


def _seed_s3_tables(bucket: str, tables: list[tuple[str, str]]) -> None:
    """Create one S3 Tables bucket holding one Iceberg table per (namespace, name)."""
    s3tables = _client("s3tables")
    arn = s3tables.create_table_bucket(name=bucket)["arn"]
    for namespace, table in tables:
        with contextlib.suppress(s3tables.exceptions.ConflictException):
            s3tables.create_namespace(tableBucketARN=arn, namespace=[namespace])
        s3tables.create_table(tableBucketARN=arn, namespace=namespace, name=table, format="ICEBERG")


@pytest.fixture
def listing_env(mock_s3_buckets, monkeypatch):
    """Shared-fixture moto backend plus the two env vars ``get_schemas`` reads."""
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    # Keeps get_aws_account_id() (schemas.py:77) off STS entirely.
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    return mock_s3_buckets


def _rows_from_listing() -> list[dict[str, Any]]:
    response = get_schemas()
    assert response["statusCode"] == 200, f"expected 200 from get_schemas, got {response}"
    return json.loads(response["body"])["schemas"]


def test_the_response_carries_no_internal_identifier_keys(listing_env):
    """R5.7's carried identifiers are stripped before the response.

    End to end, because this is the half the property cannot reach: the property
    is handed rows that already carry the keys, so it cannot tell whether
    ``get_schemas`` carries them, nor whether the strip runs on the real path.
    Any underscore-prefixed key crossing the API boundary would be an internal
    identifier the frontend's ``normalizeSchema`` never declared.

    **Validates: Requirement 5.7**
    """
    _seed_glue_table("sagemaker_sample_db", "churn")
    _seed_s3_tables("testingbucket", [("testingbucketns", "testingbuckettn")])

    rows = _rows_from_listing()
    assert rows, "the fixture seeded two sources; the listing must not be empty"

    internal = sorted({key for row in rows for key in row if key.startswith("_")})
    assert not internal, f"internal keys crossed the API boundary: {internal}"


def test_two_s3_tables_rows_deriving_one_pair_exclude_it_once_and_both_survive(listing_env):
    """DD7 axis 2, end to end: one mirror, two S3 Tables rows, nothing lost.

    Namespace ``sales`` + table ``orders_2026`` and namespace ``sales_orders`` +
    table ``2026`` both derive Glue table ``sales_orders_2026`` in the same
    mirror database — the reachable collision the requirements did not name.
    ``create_infrastructure`` would have registered both into that one Glue
    table, so there **is** one mirror and both rows legitimately account for it.

    R5.8: the mirror is excluded once, and **no** `s3_table_bucket` row is
    dropped. A list-based filter that removed one row per accounting S3 Tables
    row would try to drop the Glue row twice; a filter that resolved the
    ambiguity by dropping an S3 Tables row would lose a real schema.

    The two surviving rows are identified by ``name`` rather than ``schemaId``,
    because ``s3_table_{bucket}_{ns}_{table}`` collapses them onto one value
    (`:199`) for the same reason ``glue_{db}_{table}`` cannot be decomposed
    (`:47`) — the ambiguity is real and is one layer up from DD7's own.

    **Validates: Requirements 5.4, 5.7, 5.8**
    """
    bucket = "testingbucket"
    _seed_s3_tables(bucket, [("sales", "orders_2026"), ("sales_orders", "2026")])
    mirror_database, mirror_table = _r57_mirror_pair(bucket, "sales", "orders_2026")
    assert (mirror_database, mirror_table) == _r57_mirror_pair(bucket, "sales_orders", "2026"), (
        "the fixture's two tables must derive the SAME pair, or this test is not about the collision"
    )
    _seed_glue_table(mirror_database, mirror_table)
    # A second Glue table in the same mirror database that nothing accounts for —
    # so the exclusion is shown to be per-table, not per-database.
    _seed_glue_table(mirror_database, "unaccounted_table")

    rows = _rows_from_listing()
    ids = [row.get("schemaId") for row in rows]
    names = [row.get("name") for row in rows if row.get("source") == "s3_table_bucket"]

    assert sorted(names) == sorted([f"{bucket}.sales.orders_2026", f"{bucket}.sales_orders.2026"]), (
        f"both S3 Tables rows must survive; R5.8 forbids dropping a real schema — s3 rows: {names}"
    )
    assert f"glue_{mirror_database}_{mirror_table}" not in ids, (
        f"the single mirror both rows account for must be excluded — ids: {ids}"
    )
    assert f"glue_{mirror_database}_unaccounted_table" in ids, (
        f"a Glue table in the mirror database that no S3 Tables table derives must survive — ids: {ids}"
    )
