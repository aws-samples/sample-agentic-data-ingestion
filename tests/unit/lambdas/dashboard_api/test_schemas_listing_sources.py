# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization of ``Schema_Listing`` — three sources, empty, failed, field extraction.

Every expectation here is derived from ``requirements.md`` — **never** from
running ``get_schemas``. Current output contains all three defects this file
describes, so a test written from it would pin the defects instead of catching
them.

**Expected state when this file lands: RED**, per design §6's ordering table
(step 2) and task 3.2. Four distinct requirement disagreements, in the order the
implementation tasks close them:

1. **`targetType`** (R5.5) — every Glue row is hardcoded ``"Aurora Postgres"``
   (``schemas.py:49``) regardless of what the table is, including the S3 Tables
   Iceberg mirrors, which are not Postgres. Closed by task 4.3.
2. **The dedupe** (R5.3, R5.4, R5.7, R5.8) — a `glue_catalog` row that an
   enumerated `s3_table_bucket` row accounts for is still returned, so every S3
   Tables target appears twice. Closed by task 4.3.
3. **Empty versus failed** (R1.4, R5.3) — all seven ``except`` clauses in
   ``get_schemas`` swallow into ``logger.warning``/``logger.debug``
   (``:68-72``, ``:125-129``, ``:194-195``, ``:211-215``), and the response
   carries nothing but ``{"schemas": [...]}``, so a source that **failed** and a
   source that is **empty** are byte-identical outcomes. Closed by task 4.5.
4. **The per-field `description`** (R1.5) — the API emits exactly four keys per
   field, ``name``/``type``/``nullable``/``context``, and **never**
   ``description``; each source's *own* description is fed into ``context``
   instead (Glue ``Comment`` at ``:62``, uploaded ``description`` at ``:109``
   and ``:119``, Iceberg ``doc`` at ``:189``). That is R1.5's mechanism note
   verbatim: one description value under the wrong heading, next to a heading
   that is structurally always blank. Closed by task 4.5, together with (3) —
   which is why task 4.5 is the task that says "task 3.2 goes green here".

Per design §5 risk 9, that red window is the expected state and **not a
regression**: a reviewer who sees it and reverts has undone the only thing that
would have caught the defect.

Assertions that are **green today** and must stay green: all three sources are
enumerated and reachable; each source's field-extraction branch produces one
field per declared column/property/Iceberg field with its name and type;
``nullable`` is derived from ``required`` on the two sources that declare it;
and no row carries the legacy ``RDS_DATABASE`` alias.

**`fieldCount` is deliberately not asserted here.** DD4 / task 4.4 introduces
it (``fieldCount: null``, never ``0``) and owns its characterization in
``test_schemas_listing_pagination.py``; this file would otherwise stake a claim
on a key its own task does not land.

**The amendment this file predicted has now landed — task 4.4 / DD4.** The two
*lazy* field-extraction branches — the uploaded body read (``:88``) and the
Iceberg metadata read (``:173-175``) — are off the listing and on
``GET /api/schemas/{id}``. The two field-extraction tests below moved **with the
branch they characterize, keeping their names and their subject** rather than
being deleted (`LGR-5906ec6-2`, `LGR-60c4985-6`, both **template / ratified**: an
amendment states its reversal and keeps its test name so it reads as
deliberate). Each states the reversal in its own docstring. The Glue branch reads
its columns from the ``get_tables`` page, costs no read, and stays on the
listing — so ``test_glue_field_extraction_carries_every_declared_column`` did not
move.

**Both reds are now closed by task 4.5, and the second needed two changes rather
than one — recorded here because it was not obvious from the red.**
``test_each_source_emits_its_own_description_and_leaves_context_to_the_sidecar``
read the per-field ``description`` of **all four** sources off the listing. After
task 4.4 two of those rows carry no ``fields`` key at all, so it tripped on
``has no field 'email' — fields: []`` before it reached the R1.5 disagreement it
was written to state. It was **deliberately left untouched by task 4.4** — one of
that task's two permitted reds — and task 4.5 closed it on both axes: the
producer-side ``description`` / ``context`` split (which is what the **Glue** row,
still read off the listing, needed), and re-pointing the uploaded and S3 Tables
halves through ``get_schema(id)``, now the only surface that has their fields.
``_schema_detail`` already separated the two authorities, so those two rows also
assert that it passes the producer's ``description`` through rather than
re-deriving it. The amendment keeps the test name verbatim and states the reversal
in its own docstring (`LGR-5906ec6-2`, `LGR-60c4985-6`).

**Task 4.5's response shape, for
``test_empty_failed_and_populated_source_are_three_distinguishable_outcomes``.**
That test is deliberately shape-agnostic — it asserts only that the empty and the
failed bodies **differ** and that the failing one **names** the source — so it
does not pin the shape the implementation chose. For a reader: ``get_schemas``
now emits ``sourcesFailed`` beside ``schemas``, a list of
``{source, scope, error}`` entries, **always present and empty when every source
was read to the end**. The empty list is the affirmative half of the distinction;
the entries are the reported half.

**Fixtures.** Shared fixtures only: ``mock_s3_buckets`` from
``tests/fixtures/conftest.py``, which is also what provides the moto context the
Glue and S3 Tables seeding runs inside. There is no ``moto.mock_aws()`` call and
no boto3 client mock in this file (``tests/unit/test_no_duplicate_mocking_prop.py``,
`LGR-adceb72-1` / `LGR-aadd206-1`) — the seeding clients are real clients
talking to the shared fixture's moto backend, which is what lets "a source that
raises" be a genuine ``NoSuchBucket`` from the service rather than a stubbed
exception.

**This file does not set `AWS_REGION`** (`LGR-3e45e65-1`, **template /
ratified**), which matters here specifically because ``get_schemas`` reads it at
call time (``schemas.py:23``). Every seeding client instead **reads** the region
through that same expression (see ``_client``), so the seeding and the handler
cannot land in different regions — moto's Glue and S3 Tables backends are
region-scoped, and a mismatch presents as an empty source, which
``get_schemas``' swallowing ``except`` clauses make indistinguishable from a
requirement disagreement. That is `LGR-3e45e65-1`'s own failure mode one layer
down, and it is observed rather than theorised: it is what the first full-suite
run of this file did, while the file passed in isolation.

``mock_lambda_env`` is deliberately *not* used, for the same record's reason: it
assigns ``AWS_REGION=us-east-1`` by ``monkeypatch.setenv``, which would decide
this handler's region from a fixture rather than from the layer.
``TARGET_SCHEMAS_BUCKET`` is set per test with ``monkeypatch`` because the
empty / failed / populated scenarios differ in nothing else.

**Validates: Requirements 1.4, 1.5, 5.3, 5.4, 5.5, 8.2**
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
# and require_env raises OSError when a name is unset outside local mode. The
# directory conftest puts shared/ and the Lambda package on sys.path; the table
# names have to exist before the import itself.
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(SHARED_DIR), os.path.abspath(LAMBDA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Table names only — AWS_REGION and AWS_DEFAULT_REGION are deliberately absent.
# setdefault, so an already-pinned value is never overridden.
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

from handlers.schemas import get_schema, get_schemas

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------
# The shared fixture's target-schemas bucket (tests/fixtures/conftest.py's
# S3_BUCKET_DEFINITIONS). get_schemas reads TARGET_SCHEMAS_BUCKET (schemas.py:78),
# which no shared fixture sets — mock_lambda_env sets TARGET_SCHEMA_BUCKET,
# singular, which this handler does not read.
TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"

# A bucket that does not exist, so the uploaded source's list_objects_v2 raises a
# real NoSuchBucket. This is how "a source that raises" is produced without a
# client mock.
ABSENT_BUCKET = "adi-target-schemas-absent-in-this-account"

# One S3 Tables table, and the Glue mirror create_infrastructure would have
# written for it: database agentic_data_ingestion_{bucket with - → _}
# (create_infrastructure/lambda_function.py:272) and table {namespace}_{table}
# (:273). Named after the live pair in requirements.md §Requirement 7's evidence
# table.
S3T_BUCKET = "testingbucket"
S3T_NAMESPACE = "testingbucketns"
S3T_TABLE = "testingbuckettn"
MIRROR_DB = f"agentic_data_ingestion_{S3T_BUCKET.replace('-', '_')}"
MIRROR_TABLE = f"{S3T_NAMESPACE}_{S3T_TABLE}"

# A genuine per-pipeline Glue database. create_glue_pipeline/iam_roles.py:673
# writes agentic_data_ingestion_{pipeline_id.lower().replace('-','_')} from a
# DIFFERENT scheme than create_infrastructure:272's bucket-derived name, so this
# row shares the prefix but no S3 Tables table maps onto it. R5.7's direction —
# S3-Tables-row → Glue-row — requires it to SURVIVE the exclusion, which is what
# distinguishes an identifier match from a prefix test.
PIPELINE_DB = "agentic_data_ingestion_hello123_c4827272"
PIPELINE_TABLE = "orders"

# A plain Glue table unrelated to this product, with columns (requirements.md
# §Requirement 5's live evidence: sagemaker_sample_db.churn carries 21 columns,
# which is how the extraction code at schemas.py:57-64 is known to work).
PLAIN_DB = "sagemaker_sample_db"
PLAIN_TABLE = "churn"
PLAIN_COLUMNS = [
    {"Name": "customer_id", "Type": "bigint", "Comment": "Unique customer key"},
    {"Name": "churned", "Type": "boolean", "Comment": "Whether the customer left"},
]

# Uploaded schemas — one per body shape get_schemas handles: the custom
# {"fields": [...]} form (:102-111) and the JSON Schema {"properties": {...}}
# form (:112-121).
UPLOADED_FIELDS_ID = "uploaded_schema_1785844048926"
UPLOADED_FIELDS_BODY = {
    "fields": [
        {"name": "email", "type": "string", "required": True, "description": "Primary contact address"},
        {"name": "nickname", "type": "string", "required": False, "description": "Optional display name"},
    ]
}
UPLOADED_PROPERTIES_ID = "customers_jsonschema"
UPLOADED_PROPERTIES_BODY = {
    "properties": {
        "customer_id": {"type": "integer", "description": "Unique customer key"},
        "notes": {"type": "string", "description": "Free-text notes"},
    },
    "required": ["customer_id"],
}

# The Iceberg current-schema the S3 Tables branch extracts from (:177-192).
ICEBERG_FIELDS = [
    {"id": 1, "name": "order_id", "type": "long", "required": True, "doc": "Order key"},
    {"id": 2, "name": "placed_at", "type": "timestamp", "required": False, "doc": "When the order was placed"},
]

# Id shapes, from requirements.md rather than from reading the producer:
# glue_{db}_{table} (§Requirement 3's scope note, schemas.py:47), the object
# basename for uploaded (§Glossary "Uploaded_Schema", :85/:92), and
# s3_table_{bucket}_{ns}_{table} (§Requirement 3's scope note, :199).
MIRROR_ROW_ID = f"glue_{MIRROR_DB}_{MIRROR_TABLE}"
PIPELINE_ROW_ID = f"glue_{PIPELINE_DB}_{PIPELINE_TABLE}"
PLAIN_ROW_ID = f"glue_{PLAIN_DB}_{PLAIN_TABLE}"
S3T_ROW_ID = f"s3_table_{S3T_BUCKET}_{S3T_NAMESPACE}_{S3T_TABLE}"


# ---------------------------------------------------------------------------
# Seeding — real clients against the shared fixture's moto backend
# ---------------------------------------------------------------------------
def _client(service: str):
    """A client for `service` in exactly the region ``get_schemas`` will use.

    The region is **read**, not stated: ``os.environ.get("AWS_REGION",
    "us-west-2")`` is `schemas.py:23` verbatim, so the seeding and the handler
    cannot disagree. `LGR-3e45e65-1` — this file assigns no region; it resolves
    the one `tests/unit/conftest.py` pinned, through the same expression the
    code under test uses.

    **Why this is not `boto3.client(service)`.** boto3's default session caches
    its region when the session is first created, which in a full-suite run is
    some earlier test's process-wide state rather than this file's. moto's Glue
    and S3 Tables backends are **region-scoped** (its S3 backend is not), so a
    seeding client one region away from the handler writes tables the handler
    then cannot see — and `get_schemas` swallows the resulting empty result into
    a `logger.warning` (`:68-72`, `:211-215`), so it looks exactly like a
    requirement disagreement instead of a harness fault. That is
    `LGR-3e45e65-1`'s own failure mode — an effective region decided by import
    order — reappearing one layer down, and it is why the resolution is shared
    with the handler rather than left to boto3.
    """
    return boto3.client(service, region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _ensure_bucket(name: str) -> None:
    """Ensure an S3 bucket exists, reading the LocationConstraint off the client.

    `create_table` already provisions the table's warehouse bucket, so this is
    normally a no-op; it exists so the seeding does not depend on that being
    true.
    """
    s3 = _client("s3")
    kwargs: dict = {"Bucket": name}
    region = s3.meta.region_name
    if region and region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou, s3.exceptions.BucketAlreadyExists):
        s3.create_bucket(**kwargs)


def _seed_glue_table(database: str, table: str, columns: list[dict] | None = None) -> None:
    """Register one Glue database + table, with or without columns."""
    glue = _client("glue")
    with contextlib.suppress(glue.exceptions.AlreadyExistsException):
        glue.create_database(DatabaseInput={"Name": database})
    glue.create_table(
        DatabaseName=database,
        TableInput={
            "Name": table,
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {"table_type": "ICEBERG", "classification": "iceberg"},
            "StorageDescriptor": {
                "Columns": columns or [],
                "Location": f"s3://{database}/{table}",
            },
        },
    )


def _seed_uploaded_schema(schema_id: str, body: dict, bucket: str = TARGET_SCHEMAS_BUCKET) -> None:
    """Write one uploaded schema body at `shared/{id}.json` (`LGR-cad8032-1`)."""
    _client("s3").put_object(
        Bucket=bucket,
        Key=f"shared/{schema_id}.json",
        Body=json.dumps(body),
        ContentType="application/json",
    )


def _seed_s3_table(bucket: str, namespace: str, table: str, iceberg_fields: list[dict] | None) -> None:
    """Create one S3 Tables Iceberg table, optionally with committed metadata.

    When `iceberg_fields` is given, an Iceberg metadata document is written into
    the table's own warehouse location and the table's `metadataLocation` is
    pointed at it — the state requirements.md §Requirement 7 found live, where
    the pointer appears only after the first commit.

    The metadata object is seeded where the **current** code looks for it:
    `s3:GetObject` on the warehouse bucket (`schemas.py:173-175`).
    `LGR-2eaf8a3-1` (**backfilled**) fences that read to the S3 Tables Iceberg
    REST `loadTable` path instead, and requirements.md keeps it "IN as a
    constraint, OUT as a fix" — so seeding it here characterizes the branch that
    exists and is **not** an endorsement of the read path.
    """
    s3tables = _client("s3tables")
    arn = s3tables.create_table_bucket(name=bucket)["arn"]
    s3tables.create_namespace(tableBucketARN=arn, namespace=[namespace])
    created = s3tables.create_table(tableBucketARN=arn, namespace=namespace, name=table, format="ICEBERG")
    if iceberg_fields is None:
        return

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
def _body(response: dict) -> dict:
    assert response["statusCode"] == 200, f"expected 200 from get_schemas, got {response}"
    return json.loads(response["body"])


def _rows(response: dict) -> list[dict]:
    return _body(response)["schemas"]


def _row(rows: list[dict], schema_id: str) -> dict | None:
    for row in rows:
        if row.get("schemaId") == schema_id:
            return row
    return None


def _ids(rows: list[dict]) -> list[str]:
    return [row.get("schemaId") for row in rows]


def _fields(rows: list[dict], schema_id: str) -> list[dict]:
    row = _row(rows, schema_id)
    assert row is not None, f"{schema_id} is not in the listing — ids present: {_ids(rows)}"
    return row.get("fields", [])


def _field(rows: list[dict], schema_id: str, field_name: str) -> dict:
    fields = _fields(rows, schema_id)
    for field in fields:
        if field.get("name") == field_name:
            return field
    raise AssertionError(f"{schema_id} has no field {field_name!r} — fields: {fields}")


# ---------------------------------------------------------------------------
# Reading the per-id response — where DD4 moved the two lazy branches
# ---------------------------------------------------------------------------
def _detail_fields(schema_id: str) -> list[dict]:
    """The field list ``GET /api/schemas/{id}`` resolves for one schema.

    DD4 / task 4.4: the uploaded body read and the Iceberg metadata read are no
    longer on the listing, so the two tests that characterize them read here
    instead. ``_row``/``_fields`` above still read the listing, which is where
    the Glue branch's columns remain.
    """
    response = get_schema(schema_id)
    assert response["statusCode"] == 200, f"expected 200 from get_schema({schema_id!r}), got {response}"
    return json.loads(response["body"])["fields"]


def _detail_field(schema_id: str, field_name: str) -> dict:
    fields = _detail_fields(schema_id)
    for field in fields:
        if field.get("name") == field_name:
            return field
    raise AssertionError(f"{schema_id} has no field {field_name!r} on GET /api/schemas/{{id}} — fields: {fields}")


# ---------------------------------------------------------------------------
# The populated fixture: all three sources, the mirror pair, and a non-mirror
# ---------------------------------------------------------------------------
@pytest.fixture
def all_three_sources(mock_s3_buckets, monkeypatch):
    """Seed all three of Schema_Listing's sources and return its response.

    `mock_s3_buckets` is the shared fixture (`tests/fixtures/conftest.py`); it
    creates the six project buckets and is what puts the moto backend in place
    for the Glue and S3 Tables seeding below.
    """
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    # Keeps get_aws_account_id() (schemas.py:77) off STS entirely.
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")

    _seed_glue_table(PLAIN_DB, PLAIN_TABLE, PLAIN_COLUMNS)
    _seed_glue_table(PIPELINE_DB, PIPELINE_TABLE)
    _seed_glue_table(MIRROR_DB, MIRROR_TABLE)  # "Columns": [] — the live state
    _seed_uploaded_schema(UPLOADED_FIELDS_ID, UPLOADED_FIELDS_BODY)
    _seed_uploaded_schema(UPLOADED_PROPERTIES_ID, UPLOADED_PROPERTIES_BODY)
    _seed_s3_table(S3T_BUCKET, S3T_NAMESPACE, S3T_TABLE, ICEBERG_FIELDS)

    return get_schemas()


# ---------------------------------------------------------------------------
# All three sources — harness sanity, and R8.2's coverage clause. GREEN today.
# ---------------------------------------------------------------------------
def test_all_three_sources_are_enumerated(all_three_sources):
    """Every source contributes rows, so every branch below is really reached.

    Green today. It is here so a red assertion elsewhere in this file cannot be
    mistaken for a harness that never reached the code: if this passes, all
    three source blocks ran (`schemas.py:26-72`, `:75-129`, `:132-215`).

    **Validates: Requirement 8.2**
    """
    rows = _rows(all_three_sources)
    sources = {row.get("source") for row in rows}
    assert {"glue_catalog", "uploaded", "s3_table_bucket"} <= sources, (
        f"not every source produced a row — sources present: {sorted(sources)}, ids: {_ids(rows)}"
    )
    for expected_id in (PLAIN_ROW_ID, UPLOADED_FIELDS_ID, UPLOADED_PROPERTIES_ID, S3T_ROW_ID):
        assert _row(rows, expected_id) is not None, f"{expected_id} missing — ids: {_ids(rows)}"


# ---------------------------------------------------------------------------
# R5.5 — the target label describes the schema's actual target
# ---------------------------------------------------------------------------
def test_glue_row_is_not_labelled_aurora_postgres(all_three_sources):
    """R5.5: a Glue row's `targetType` describes that table's actual target.

    ``"Aurora Postgres"`` is hardcoded on **every** Glue row (`schemas.py:49`),
    including the S3 Tables Iceberg mirrors, which are not Postgres. Asserted as
    a negative rather than against a specific replacement string, because R5.5
    does not name one and task 4.3 chooses it.

    **RED today.**

    **Validates: Requirement 5.5**
    """
    rows = _rows(all_three_sources)
    mislabelled = [
        (row.get("schemaId"), row.get("targetType"))
        for row in rows
        if row.get("source") == "glue_catalog" and row.get("targetType") == "Aurora Postgres"
    ]
    assert not mislabelled, (
        "R5.5: every glue_catalog row is labelled the hardcoded "
        f'"Aurora Postgres" (schemas.py:49) — {mislabelled}. Expected a label '
        "describing each table's actual target."
    )


def test_no_row_carries_the_legacy_rds_database_alias(all_three_sources):
    """R5.5: a `jdbc` target is not reported as `RDS_DATABASE`.

    ``RDS_DATABASE`` is a legacy alias consolidated into ``jdbc`` (P17 task 20.4)
    and normalised on read, so it must not appear as a target label. Green today
    — nothing emits it — and this pins that it stays true when task 4.3 replaces
    the hardcoded labels with derived ones.

    **Validates: Requirement 5.5**
    """
    rows = _rows(all_three_sources)
    legacy = [(row.get("schemaId"), row.get("targetType")) for row in rows if row.get("targetType") == "RDS_DATABASE"]
    assert not legacy, f"RDS_DATABASE is a legacy alias for jdbc and must not be a reported target type: {legacy}"


# ---------------------------------------------------------------------------
# R5.3 / R5.4 / R5.7 / R5.8 — the mirror is excluded, the real schema is not
# ---------------------------------------------------------------------------
def test_mirrored_glue_row_is_absent_while_the_s3_tables_row_survives(all_three_sources):
    """R5.4: the `s3_table_bucket` representation survives, the mirror does not.

    The seeded S3 Tables table derives exactly the Glue pair
    ``agentic_data_ingestion_testingbucket`` / ``testingbucketns_testingbuckettn``
    (`create_infrastructure/lambda_function.py:272`, `:273`), so that
    `glue_catalog` row is a mirror of a table already enumerated and R5.4 says
    to omit it.

    **RED today** — `get_schemas` sweeps the whole Glue catalog, so the mirror
    is returned alongside the real row and every S3 Tables target appears twice.

    **Validates: Requirements 5.3, 5.4, 5.7**
    """
    rows = _rows(all_three_sources)
    assert _row(rows, S3T_ROW_ID) is not None, f"the s3_table_bucket row must survive — ids: {_ids(rows)}"
    mirror = _row(rows, MIRROR_ROW_ID)
    assert mirror is None, (
        f"R5.4: the Glue mirror {MIRROR_DB}.{MIRROR_TABLE} is still in the listing "
        f"({mirror!r}) even though {S3T_ROW_ID} accounts for it — so the S3 Tables "
        "target is offered twice, once with real fields and once with none."
    )


def test_a_glue_row_no_s3_tables_table_maps_onto_is_kept(all_three_sources):
    """R5.7/R5.8: the exclusion matches identifiers, it does not test a prefix.

    ``agentic_data_ingestion_hello123_c4827272`` shares the mirror's prefix but
    comes from `create_glue_pipeline/iam_roles.py:673`'s pipeline-id-keyed
    scheme, and no S3 Tables table derives it. Excluding on a prefix would hide
    it; matching on the derived pair cannot. Green today and must stay green —
    it is the assertion that fails if task 4.3 reaches for a prefix test.

    **Validates: Requirements 5.7, 5.8**
    """
    rows = _rows(all_three_sources)
    assert _row(rows, PIPELINE_ROW_ID) is not None, (
        f"{PIPELINE_ROW_ID} is a genuine per-pipeline Glue table that no S3 Tables "
        f"table maps onto; it must not be excluded — ids: {_ids(rows)}"
    )
    assert _row(rows, PLAIN_ROW_ID) is not None, (
        f"{PLAIN_ROW_ID} is an unrelated Glue table and must survive — ids: {_ids(rows)}"
    )


def test_at_most_one_row_per_distinct_target_table(all_three_sources):
    """R5.3: at most one schema per distinct target table.

    Expressed on the target the two representations share — the S3 Tables
    ``(bucket, namespace, table)`` triple and the Glue pair derived from it —
    rather than on `schemaId`, which differs between the two by construction.

    **RED today**, for the same reason as the test above: two rows describe the
    one `testingbucket` table.

    **Validates: Requirement 5.3**
    """
    rows = _rows(all_three_sources)
    describing_the_same_table = [
        row.get("schemaId") for row in rows if row.get("schemaId") in (S3T_ROW_ID, MIRROR_ROW_ID)
    ]
    assert len(describing_the_same_table) == 1, (
        f"R5.3: {len(describing_the_same_table)} rows describe the single "
        f"{S3T_BUCKET}.{S3T_NAMESPACE}.{S3T_TABLE} table — {describing_the_same_table}. "
        "Expected exactly one, the s3_table_bucket representation (R5.4)."
    )


# ---------------------------------------------------------------------------
# R1.4 / R5.3 — empty, failed and populated are three distinguishable outcomes
# ---------------------------------------------------------------------------
def test_empty_failed_and_populated_source_are_three_distinguishable_outcomes(mock_s3_buckets, monkeypatch):
    """R1.4/R5.3: zero rows, a raising source and rows are three outcomes, not two.

    The uploaded source is driven through all three states by configuration
    alone — an existing bucket with no ``shared/`` objects, a bucket that does
    not exist (a real ``NoSuchBucket`` out of `list_objects_v2`), and the same
    bucket with a schema in it. Glue and S3 Tables are seeded throughout so the
    rest of the response is constant and only the uploaded source varies.

    **RED today.** All seven `except` clauses swallow into
    ``logger.warning``/``logger.debug`` (`:68-72`, `:125-129`, `:194-195`,
    `:211-215`) and the response body is only ``{"schemas": [...]}``, so the
    empty and the failed responses are **identical** — the user cannot tell an
    empty picker from a broken Glue call. Asserted shape-agnostically: the two
    bodies must differ, and the failing one must name the source that failed.
    Task 4.5's per-source partial success is what closes it; this test does not
    presuppose its key names.

    **Validates: Requirements 1.4, 5.3**
    """
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    _seed_glue_table(PLAIN_DB, PLAIN_TABLE, PLAIN_COLUMNS)
    _seed_s3_table(S3T_BUCKET, S3T_NAMESPACE, S3T_TABLE, ICEBERG_FIELDS)

    # 1. The source is reachable and holds nothing.
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    empty = _body(get_schemas())

    # 2. The source cannot be read at all — NoSuchBucket from the service.
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", ABSENT_BUCKET)
    failed = _body(get_schemas())

    # 3. The source is reachable and holds a schema.
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    _seed_uploaded_schema(UPLOADED_FIELDS_ID, UPLOADED_FIELDS_BODY)
    populated = _body(get_schemas())

    assert _row(populated["schemas"], UPLOADED_FIELDS_ID) is not None, (
        f"the populated case must carry the uploaded row — ids: {_ids(populated['schemas'])}"
    )
    assert _row(empty["schemas"], UPLOADED_FIELDS_ID) is None
    assert _row(failed["schemas"], UPLOADED_FIELDS_ID) is None

    # Both clauses are collected rather than asserted in sequence, so the red
    # reads as the whole disagreement instead of only its first half.
    problems: list[str] = []
    if empty == failed:
        problems.append(
            "the empty and the unreadable case produce the IDENTICAL response, so "
            '"checked and found nothing" cannot be told from "could not check". '
            f"Both are: {json.dumps(empty, sort_keys=True)}"
        )
    if "uploaded" not in json.dumps(failed):
        problems.append(
            "the response for the source that raised names no failing source — "
            f'expected the "uploaded" source to be identified somewhere in '
            f"{json.dumps(failed, sort_keys=True)}"
        )
    assert not problems, "R1.4/R5.3 — three outcomes, not two:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# The field-extraction branch of each source
# ---------------------------------------------------------------------------
def test_glue_field_extraction_carries_every_declared_column(all_three_sources):
    """One field per Glue `StorageDescriptor.Columns` entry, with its type.

    Green today (`schemas.py:57-64`). This branch reads the columns out of the
    `get_tables` page, so DD4 leaves it on the listing.

    **Validates: Requirement 8.2**
    """
    rows = _rows(all_three_sources)
    fields = _fields(rows, PLAIN_ROW_ID)
    assert [f.get("name") for f in fields] == [c["Name"] for c in PLAIN_COLUMNS], (
        f"expected one field per declared column, got {fields}"
    )
    assert _field(rows, PLAIN_ROW_ID, "customer_id")["type"] == "bigint"
    assert _field(rows, PLAIN_ROW_ID, "churned")["type"] == "boolean"


def test_uploaded_field_extraction_covers_both_body_shapes(all_three_sources):
    """Both uploaded body shapes extract fields, and `nullable` comes from `required`.

    The custom ``{"fields": [...]}`` form and the JSON Schema
    ``{"properties": {...}}`` form, both still handled, both still deriving
    ``nullable`` from ``required``.

    **Reversal — DD4 / task 4.4.** This test previously read the two field lists
    off the **listing**. It does not any more, because the listing no longer
    makes the ``s3:GetObject`` on each uploaded body (`schemas.py:88`) that
    produced them; ``GET /api/schemas/{id}`` does, for the one schema it is asked
    about (`_uploaded_schema_fields`). The name and the subject are unchanged —
    the extraction itself was not touched, only where it runs — per
    `LGR-5906ec6-2` and `LGR-60c4985-6` (both **template / ratified**): an
    amendment states its reversal and keeps its test name so it reads as
    deliberate rather than as a regression.

    ``all_three_sources`` is requested for its **seeding**, not its response: it
    is what puts the two uploaded bodies in the fixture bucket and pins
    ``TARGET_SCHEMAS_BUCKET`` for the duration of the test.

    **Validates: Requirement 8.2**
    """
    assert all_three_sources["statusCode"] == 200, "the seeding fixture's listing must succeed"

    assert [f.get("name") for f in _detail_fields(UPLOADED_FIELDS_ID)] == ["email", "nickname"]
    assert _detail_field(UPLOADED_FIELDS_ID, "email")["type"] == "string"
    assert _detail_field(UPLOADED_FIELDS_ID, "email")["nullable"] is False, "required: true means not nullable"
    assert _detail_field(UPLOADED_FIELDS_ID, "nickname")["nullable"] is True

    assert sorted(f.get("name") for f in _detail_fields(UPLOADED_PROPERTIES_ID)) == ["customer_id", "notes"]
    assert _detail_field(UPLOADED_PROPERTIES_ID, "customer_id")["type"] == "integer"
    assert _detail_field(UPLOADED_PROPERTIES_ID, "customer_id")["nullable"] is False, "listed in required"
    assert _detail_field(UPLOADED_PROPERTIES_ID, "notes")["nullable"] is True


def test_s3_tables_field_extraction_reads_the_iceberg_current_schema(all_three_sources):
    """The S3 Tables branch extracts the fields of the current Iceberg schema.

    Still the extraction of `schemas.py:177-192`, unchanged: one field per field
    of the Iceberg document's **current** schema, with `nullable` derived from
    `required`.

    **Reversal — DD4 / task 4.4.** This previously read off the **listing**. The
    listing no longer makes either of the two calls that produced this list — the
    `s3tables:GetTable` (`:159-161`) and the `s3:GetObject` on the Iceberg
    metadata document (`:173-175`) — so it reads through
    ``GET /api/schemas/{id}`` (`_s3_tables_schema_fields`), which makes them for
    one table instead of every table. Two calls *per row* is why a paginated
    listing that kept resolving fields would have timed out rather than merely
    got slower. Name and subject kept verbatim per `LGR-5906ec6-2` and
    `LGR-60c4985-6` (both **template / ratified**).

    **The mirror clause survives the move, on the listing, restated in
    `fieldCount`.** It is what makes the dedupe tests meaningful: the surviving
    `s3_table_bucket` row is the one that can produce real fields, against the
    mirror's `"Columns": []`. Now that task 4.3's exclusion has landed the mirror
    is absent outright, and `fieldCount` is where a Glue row states its column
    count — so the clause asserts on that rather than on a `fields` list the row
    would only carry if it were a Glue row.

    ``all_three_sources`` is requested for its **seeding** and for the listing
    half of the assertion below.

    **Validates: Requirement 8.2**
    """
    assert [f.get("name") for f in _detail_fields(S3T_ROW_ID)] == ["order_id", "placed_at"]
    assert _detail_field(S3T_ROW_ID, "order_id")["type"] == "long"
    assert _detail_field(S3T_ROW_ID, "order_id")["nullable"] is False, "required: true means not nullable"
    assert _detail_field(S3T_ROW_ID, "placed_at")["nullable"] is True

    rows = _rows(all_three_sources)
    mirror = _row(rows, MIRROR_ROW_ID)
    assert mirror is None or mirror.get("fieldCount") == 0, (
        f"the Glue mirror is expected to declare no columns, got fieldCount={mirror.get('fieldCount')!r}"
    )


def test_each_source_emits_its_own_description_and_leaves_context_to_the_sidecar(all_three_sources):
    """R1.5: every field carries a `description` the API actually emits.

    The API emitted four keys per field — ``name``, ``type``, ``nullable``,
    ``context`` — and no field-level ``description``, so ``normalizeField``
    resolved it to ``undefined`` (`use-schemas.ts:11`) and the Description column
    rendered ``—`` for every field of every schema. Each source's *own*
    description was fed into ``context`` instead: Glue ``Comment`` (`:62`),
    uploaded ``description`` (`:109`, `:119`), Iceberg ``doc`` (`:189`). That is
    one value under the wrong heading beside a heading that is structurally
    always blank.

    So, per source: ``description`` carries the source's declared description,
    and ``context`` carries **only** a Context_Sidecar annotation — of which
    there is none for any of these schemas, so it is empty.

    **Amendment — task 4.5, and it is the one this module's docstring predicted.**
    The test name and its subject are unchanged: four sources, each field's
    ``description`` from that source's own declaration, ``context`` left to the
    sidecar. What changed is **where two of the four rows are read from.** Task
    4.4 / DD4 moved the uploaded body read and the Iceberg metadata read off the
    listing, so ``uploaded`` and ``s3_table_bucket`` rows carry no ``fields`` key
    there at all — this test therefore tripped on ``has no field 'email' —
    fields: []`` *before* reaching the R1.5 disagreement it was written to state.
    Those two rows now read through ``GET /api/schemas/{id}``
    (``_detail_field``), which is the only surface that has their fields;
    ``PLAIN_ROW_ID`` still reads off the **listing**, because the Glue branch's
    columns come free in the ``get_tables`` page and never moved. Per
    `LGR-5906ec6-2` and `LGR-60c4985-6` (both **template / ratified**) the name is
    kept verbatim and the reversal is stated here, so it reads as deliberate
    rather than as a regression.

    That split of read surfaces is not incidental — it is what makes the
    assertion cover **both** halves of task 4.5's producer-side change. The Glue
    row exercises the split inside ``_collect_schema_rows``; the other three
    exercise it inside ``_uploaded_schema_fields`` and
    ``_s3_tables_schema_fields``, and they additionally prove ``_schema_detail``
    passes the producer's ``description`` through rather than re-deriving it.

    **Validates: Requirement 1.5**
    """
    rows = _rows(all_three_sources)
    # (schema id, field, declared description, origin, reads off the listing?)
    expected = [
        (PLAIN_ROW_ID, "customer_id", "Unique customer key", "Glue Columns[].Comment", True),
        (UPLOADED_FIELDS_ID, "email", "Primary contact address", "uploaded fields[].description", False),
        (UPLOADED_PROPERTIES_ID, "customer_id", "Unique customer key", "uploaded properties[].description", False),
        (S3T_ROW_ID, "order_id", "Order key", "Iceberg doc", False),
    ]

    problems: list[str] = []
    for schema_id, field_name, declared, origin, on_listing in expected:
        field = _field(rows, schema_id, field_name) if on_listing else _detail_field(schema_id, field_name)
        surface = "the listing" if on_listing else "GET /api/schemas/{id}"
        if field.get("description") != declared:
            problems.append(
                f"{schema_id}.{field_name} on {surface}: description is {field.get('description')!r}, "
                f"expected {declared!r} from {origin} (R1.5) — keys emitted: {sorted(field)}"
            )
        if field.get("context"):
            problems.append(
                f"{schema_id}.{field_name} on {surface}: context is {field.get('context')!r} but no "
                "Context_Sidecar exists for this schema; context carries only a human "
                "annotation (R1.5's mechanism note, R2.3)"
            )
    assert not problems, "R1.5 — description and context are two authorities:\n" + "\n".join(problems)


# ---------------------------------------------------------------------------
# The app's own Glue tables are not schemas. Two kinds are left out: the JDBC
# discovery database (crawler copies of SOURCE tables), and a Glue table on S3
# Tables managed storage that no listed S3 Tables table accounts for (a mirror
# whose table bucket was deleted). Expectations come from what each table is,
# and the two tripwires keep the per-pipeline database scheme listed, which
# `LGR-acca05d-1` protects from any name-prefix test.
# ---------------------------------------------------------------------------
def _seed_glue_table_at(database: str, table: str, location: str, columns: list[dict] | None = None) -> None:
    glue = _client("glue")
    with contextlib.suppress(glue.exceptions.AlreadyExistsException):
        glue.create_database(DatabaseInput={"Name": database})
    glue.create_table(
        DatabaseName=database,
        TableInput={
            "Name": table,
            "TableType": "EXTERNAL_TABLE",
            "StorageDescriptor": {"Columns": columns or [], "Location": location},
        },
    )


@pytest.fixture
def app_owned_glue_tables(mock_s3_buckets, monkeypatch):
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    from handlers.jdbc_resources import DISCOVERY_DB

    columns = [{"Name": "customer_id", "Type": "int"}, {"Name": "email", "Type": "string"}]
    # A crawler copy of a source table, in the discovery database.
    _seed_glue_table_at(
        DISCOVERY_DB, "agentic_data_ingestion_mynew233_503c3151_appdb_customers", "appdb.customers", columns
    )
    # A per-pipeline database for a pipeline named "jdbc-discovery-x": the
    # discovery name as a PREFIX, but a different database. Must stay.
    _seed_glue_table_at("agentic_data_ingestion_jdbc_discovery_x_1a2b3c4d", "orders", "s3://warehouse/orders", columns)
    # A mirror of a deleted table bucket: S3 Tables managed storage, no columns.
    _seed_glue_table_at(
        "agentic_data_ingestion_gone_bucket",
        "ns_customers",
        "s3://0a1b2c3d-4e5f-6a7b-8c9d0e1f2a3b--table-s3",
    )
    # Same database shape, ordinary storage: a real Glue table. Must stay.
    _seed_glue_table_at("agentic_data_ingestion_gone_bucket", "ns_orders", "s3://warehouse/ns_orders", columns)
    return DISCOVERY_DB, _rows(get_schemas())


def test_the_jdbc_discovery_database_is_not_listed(app_owned_glue_tables):
    discovery_db, rows = app_owned_glue_tables
    leaked = [r["schemaId"] for r in rows if r["schemaId"].startswith(f"glue_{discovery_db}_")]
    assert leaked == [], f"crawler copies of source tables were listed as schemas: {leaked}"


def test_a_database_that_only_shares_the_discovery_prefix_is_listed(app_owned_glue_tables):
    _, rows = app_owned_glue_tables
    assert _row(rows, "glue_agentic_data_ingestion_jdbc_discovery_x_1a2b3c4d_orders") is not None, _ids(rows)


def test_a_table_on_table_bucket_storage_with_no_s3_tables_row_is_not_listed(app_owned_glue_tables):
    _, rows = app_owned_glue_tables
    assert _row(rows, "glue_agentic_data_ingestion_gone_bucket_ns_customers") is None, _ids(rows)
    # The test is the storage, not the database: its sibling on ordinary
    # storage is a real table and stays.
    assert _row(rows, "glue_agentic_data_ingestion_gone_bucket_ns_orders") is not None, _ids(rows)
