#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Handler for schemas endpoints"""

import json
import os
import re
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, NamedTuple

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# `shared/` reaches this module through the Lambda layer, whose `/opt/python` is
# on `sys.path` automatically inside the Lambda runtime — the same unqualified
# form `handlers/agents.py:25` and `handlers/jdbc_resources.py:68` already use,
# so no explicit path manipulation belongs here.
#
# `glue_mirror_identifiers` is the ONE definition of an S3 Tables table's Glue
# mirror `(database, table)` pair: `create_infrastructure` is its defining
# caller and this exclusion is its second reader. `LGR-acca05d-1` (**template /
# ratified**) states the dedupe's `revisit_when` as precisely
# "`create_infrastructure` changes either derivation at `:272` or `:273`", which
# is only checkable while there is one derivation. Two definitions of one
# identifier is how a reader and a writer drift apart without either being
# wrong.
from glue_mirror_identifiers import glue_mirror_identifiers

# The one classification of "the object is not there" versus "the read failed",
# shared with the step Lambdas so it cannot drift. `handlers/pipelines_artifacts.py:15`
# is the precedent for importing it into this package. Used by the Context_Sidecar
# writer, where the distinction is destructive to get wrong: an unreadable
# existing sidecar treated as absent would be overwritten rather than merged into.
from step_errors import NOT_FOUND_CLIENT_ERROR_CODES

# `pipelines_table` is imported as a **module-level handle**, the same shape
# `handlers/pipelines_crud.py` uses, and D4's reference check reads it through
# this module's namespace at call time. That is deliberate rather than
# incidental: a `dynamodb.Table(...)` constructed inside the handler would bind
# to whatever region the call resolved, and the one place that matters is a test
# — `tests/fixtures/conftest.py` creates its tables in `us-east-1` while this
# module resolves `us-west-2`, and moto's DynamoDB backend is region-scoped, so
# an inline resource finds no table and raises `ResourceNotFoundException`.
# Reading a module attribute is what makes the table substitutable.
from .common import create_response, get_aws_account_id, logger, pipelines_table

# ---------------------------------------------------------------------------
# R5.5 — a target type that describes the schema's actual target
# ---------------------------------------------------------------------------
# One vocabulary, shared with the frontend's `TARGET_TYPE_LABELS`
# (`dashboard-react/src/pages/wizard/WizardStepReview.tsx:18`) so the listing
# and the wizard name the same thing the same way. Before this, every Glue row
# carried the hardcoded `"Aurora Postgres"` (`schemas.py:49`) regardless of what
# the table was — including the S3 Tables Iceberg mirrors, which are not
# Postgres.
_TARGET_TYPE_LABELS: dict[str, str] = {
    "s3_tables": "S3 Table Bucket",
    "glue_catalog": "Glue Catalog",
    "jdbc": "JDBC Database (RDS, Aurora, or self-managed)",
    "s3_parquet": "S3 Parquet",
    # An Uploaded_Schema is a target *schema*, not a target: nothing binds it to
    # one until a pipeline is created against it. "Target Schema" says that,
    # and it is what DD9's example response carries.
    "uploaded": "Target Schema",
}

#: `RDS_DATABASE` is a legacy alias consolidated into `jdbc` (P17 task 20.4) and
#: **normalised on read** — it is not a fifth target type and nothing here may
#: report it as one. Normalising in the map rather than at each call site is
#: what makes that structural instead of remembered.
_LEGACY_TARGET_TYPE_ALIASES: dict[str, str] = {"rds_database": "jdbc"}


def _target_type_label(target_type: str) -> str:
    """The display label for one canonical target-type token (R5.5).

    Case-folded and legacy-normalised, so `RDS_DATABASE`, `rds_database` and
    `jdbc` all resolve to the one `jdbc` label. An unrecognised token is
    returned unchanged rather than guessed at: a token the vocabulary does not
    know is still better information than a label invented for it.
    """
    token = (target_type or "").strip().lower()
    token = _LEGACY_TARGET_TYPE_ALIASES.get(token, token)
    return _TARGET_TYPE_LABELS.get(token, target_type)


def _glue_target_type(table: Mapping[str, Any]) -> str:
    """The canonical target-type token for one Glue Data Catalog table (R5.5).

    Derived from the table entry the `get_tables` page already returned, so this
    costs no read.

    A JDBC-crawled catalog table names the Glue connection it was crawled
    through (`handlers/jdbc_resources.py:379` creates exactly such a crawler
    with a `JdbcTargets` `ConnectionName`). That is the one Glue row for which a
    database engine really is the target — and where the old `"Aurora Postgres"`
    label was accidentally right for the wrong reason, since it was applied to
    every Glue row alike.

    **The connection-name key is read from the live table entry and is not
    verified against a live JDBC-crawled table in this phase.** That is
    deliberately non-load-bearing: if Glue names it something else, this falls
    back to `glue_catalog`, which is still an honest description of what the row
    is — a table in the Glue Data Catalog. What it can never do is report
    `"Aurora Postgres"` for an Iceberg mirror, or `RDS_DATABASE` for anything.
    """
    parameter_maps = (
        table.get("Parameters") or {},
        (table.get("StorageDescriptor") or {}).get("Parameters") or {},
    )
    for parameters in parameter_maps:
        for key, value in parameters.items():
            if key.lower() == "connectionname" and value:
                return "jdbc"
    return "glue_catalog"


#: Identifiers carried on a row for the R5.7 exclusion and for DD4's lazy field
#: resolution, and stripped before the response. Underscore-prefixed so a reader
#: can see at a glance that they are internal, and popped in one place so they
#: cannot leak across the API boundary.
#:
#: `_bucket_arn` and `_object_key` are DD4's additions. With field resolution
#: moved onto `GET /api/schemas/{id}`, the per-id endpoint has to be able to
#: reach the *one* row's source object — and neither address is recoverable from
#: the row's `schemaId`. `s3_table_{bucket}_{ns}_{table}` (`:199`) is the same
#: ambiguous underscore join as `glue_{db}_{table}` (`:47`) and cannot be
#: decomposed back into a triple, which is exactly why the exclusion is
#: forbidden from matching on it. Carrying the address the enumeration actually
#: read is also the `LGR-cad8032-1` move: one definition of a schema's address,
#: not a second derivation that can drift from the first.
_INTERNAL_ROW_KEYS = (
    "_bucket_name",
    "_namespace",
    "_table_name",
    "_glue_database",
    "_glue_table",
    "_bucket_arn",
    "_object_key",
)


# ---------------------------------------------------------------------------
# R1.4 / R1.6 / R5.3 — the narrowed `except` set, and per-source partial success
# ---------------------------------------------------------------------------
#: What a source failure **is**, and therefore the only thing this file's
#: per-source clauses catch (task 4.5, `LGR-78ce0b7-13`, **template /
#: ratified**).
#:
#: Every clause in `_collect_schema_rows` used to be `except Exception`, so a
#: broken Glue call and an empty catalog were byte-identical outcomes and a
#: `StepFailure` / `TransientStepFailure` raised on one of these paths would have
#: been re-labelled a `logger.warning` and dropped. `LGR-78ce0b7-13` narrowed a
#: clause of exactly that shape elsewhere for exactly that reason, and its
#: `revisit_when` is "nothing" — there is no downside to trade against.
#:
#: **What this deliberately does NOT catch, and why each is right:**
#:
#: - `StepFailure` / `TransientStepFailure` — a classified exception passes
#:   through rather than being re-classified. Structural rather than a re-raise
#:   guard: neither is a `ClientError` or a `BotoCoreError`, so the narrow tuple
#:   is itself the mechanism, and it cannot be defeated by someone forgetting to
#:   re-raise.
#: - `KeyError` on a response field this code requires (`database["Name"]`,
#:   `obj["Key"]`, `bucket_info["arn"]`) — a break in an AWS response contract,
#:   not a source being unreadable. It reaches `get_schemas`' outer clause and
#:   answers `500` naming it, which is the honest report; degrading it to a short
#:   list is what R1.4 exists to stop.
#: - `NameError` / `AttributeError` / `ImportError` — programming errors. A
#:   clause that hides those is how an `ImportError` on the Lambda layer presents
#:   as "you have no schemas" (the `--exclusively` failure the plan forbids).
#:
#: `ClientError` carries every permissions and service problem: `AccessDenied`,
#: `NoSuchBucket`, `EntityNotFoundException`, `ThrottlingException`.
#: `BotoCoreError` carries the client-side ones — `EndpointConnectionError`,
#: `NoCredentialsError`, `ParamValidationError`, and `OperationNotPageableError`
#: from a runtime botocore that predates a paginator config (see
#: `_paginate_continuation_token`).
_SOURCE_ERRORS: tuple[type[ClientError], type[BotoCoreError]] = (ClientError, BotoCoreError)

#: `_SOURCE_ERRORS` plus `ValueError`, for the two paths that also **parse** a
#: document: `_read_context_sidecar` and `_resolve_row_fields`. `ValueError`
#: covers `json.JSONDecodeError` and the classified "this JSON is not a schema"
#: raised by `_as_schema_document` / `_object_entries` — see `_as_schema_document`
#: for why converting that case at the point of detection is what lets these
#: clauses stay narrow instead of having to catch `TypeError` and
#: `AttributeError` as well.
#:
#: Spelled as its own constant rather than as `except (*_SOURCE_ERRORS,
#: ValueError)` because mypy rejects an unpacked variadic tuple in an `except`
#: clause ("Exception type must be derived from BaseException"), and a
#: `# type: ignore` on an exception clause is the last place worth silencing a
#: type checker.
_DOCUMENT_ERRORS: tuple[type[ClientError], type[BotoCoreError], type[ValueError]] = (
    ClientError,
    BotoCoreError,
    ValueError,
)


class _SourceFailure(NamedTuple):
    """One source, or one scope within a source, that could not be enumerated.

    `source` is the same token the rows carry — `glue_catalog`, `uploaded`,
    `s3_table_bucket` — so a consumer can line a failure up against the section
    of the list it is missing from, rather than having to map a private name onto
    a `source_type` filter.

    `scope` says **how much** of that source is missing: the whole source, or one
    Glue database / one S3 Tables bucket within it. Design §5 risk 10's mitigation
    is that one broken source degrades one section rather than the page, and a
    partially-enumerated source is the finer-grained form of the same claim — the
    Glue list is short by one database, not absent.
    """

    source: str
    scope: str
    error: str

    def as_response_entry(self) -> dict[str, str]:
        return {"source": self.source, "scope": self.scope, "error": self.error}


class _SchemaEnumeration(NamedTuple):
    """What `_collect_schema_rows` returns: the rows it got, and what it missed.

    Two values rather than one because R1.4 and R5.3 both turn on a distinction
    the rows alone cannot express. Zero uploaded rows because the prefix is empty
    and zero uploaded rows because `list_objects_v2` raised `AccessDenied` are the
    same list and different facts, and the pre-p35 response carried only the list
    — so the dashboard showed an empty picker either way and the user had no way
    to tell "you have no schemas" from "I could not look".

    `failures` empty is therefore an **affirmative** statement: every source was
    read to the end. That is why `get_schemas` emits the key even when it is
    empty, rather than omitting it — an absent key would be indistinguishable
    from a deploy that predates this.
    """

    rows: list[dict[str, Any]]
    failures: list[_SourceFailure]


def _record_source_failure(failures: list[_SourceFailure], source: str, scope: str, error: BaseException) -> None:
    """Log one source failure and carry it into the response (design §5 risk 10).

    The `logger.warning` is kept — it is what an operator greps for — and the
    entry is what the *user* gets, which is the half that was missing. Logging
    alone is what made a permissions problem invisible: the log line existed all
    along, on a surface nobody reading the dashboard can see.
    """
    logger.warning(f"Could not enumerate {source} ({scope}): {error}")
    failures.append(_SourceFailure(source=source, scope=scope, error=str(error)))


#: S3 Tables keeps a table's data in an AWS-managed bucket whose name ends in
#: `--table-s3` (the same shape the schema-mapping role's grant on
#: `arn:aws:s3:::*--table-s3/*` names in `foundation-stack.ts`). A Glue table
#: located there is a registration of S3 Tables storage, not a table of its own.
_TABLE_BUCKET_STORAGE_RE = re.compile(r"\As3://[^/]+--table-s3(?:/|\Z)")


def _is_table_bucket_storage(table: dict[str, Any]) -> bool:
    """True when a Glue table's location is S3 Tables managed storage."""
    location = (table.get("StorageDescriptor") or {}).get("Location") or ""
    return isinstance(location, str) and bool(_TABLE_BUCKET_STORAGE_RE.match(location))


def _jdbc_discovery_database() -> str:
    """The JDBC discovery database's name, from its one definition.

    Imported at call time, as `pipelines_crud.delete_pipeline` does, so loading
    this module does not load `jdbc_resources` and its Glue client.
    """
    from .jdbc_resources import DISCOVERY_DB

    return DISCOVERY_DB


def _mirror_excluded_rows(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`_exclude_glue_mirrors`' filter half, with the carried identifiers intact.

    Split out for DD4 / task 4.4: `get_schema(id)` needs the surviving rows
    **and** their `_bucket_arn` / `_object_key`, because that is where it now
    resolves the field list from. `_exclude_glue_mirrors` remains the
    API-boundary form — filter **then** strip — so nothing that builds a
    response can forget the strip.

    See `_exclude_glue_mirrors` for the decision this implements.
    """
    mirrored = {
        glue_mirror_identifiers(
            row.get("_bucket_name", ""),
            row.get("_namespace", ""),
            row.get("_table_name", ""),
        )
        for row in schemas
        if row.get("source") == "s3_table_bucket"
    }

    kept = [
        row
        for row in schemas
        if row.get("source") != "glue_catalog"
        or (row.get("_glue_database", ""), row.get("_glue_table", "")) not in mirrored
    ]

    excluded = len(schemas) - len(kept)
    if excluded:
        logger.info(f"R5.4: excluded {excluded} Glue mirror row(s) an enumerated S3 Tables table accounts for")

    return kept


def _exclude_glue_mirrors(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop every `glue_catalog` row a concrete `s3_table_bucket` row accounts for.

    D1 / R5.3, R5.4, R5.7, R5.8 (`LGR-acca05d-1`, **template / ratified**).

    **A post-pass, not an inline filter.** `get_schemas` appends the Glue rows
    first (`:26-72`), then uploaded, then S3 Tables — so at Glue-loop time no
    `s3_table_bucket` row exists to match against and an inline exclusion is not
    merely awkward, it is impossible. Reordering the three source blocks to make
    one possible was rejected: the uploaded block's `bucket_name` local shadows
    the S3 Tables block's (both named `bucket_name`), so moving the blocks
    changes which value is live at the `except` handlers — a real bug inside a
    cosmetic refactor.

    **Direction is S3-Tables-row → Glue-row and never the reverse**, so a
    `glue_catalog` row is dropped only when a *concrete* enumerated S3 Tables
    table derives its pair. Nothing is inferred from a Glue name in isolation,
    which is what keeps a genuine per-pipeline `agentic_data_ingestion_{pipeline_id}`
    database safe — `create_glue_pipeline/iam_roles.py:673` writes those from a
    **different** scheme, so a prefix test would hide real Glue targets.

    **The match is on the carried identifiers, not on `name` and not on
    `schemaId`.** `name` is a display string and R4.8 makes displayed names a
    thing that can change, so an exclusion reading one is an exclusion a
    presentation change can silently disable. `schemaId` is
    `glue_{db}_{table}` (`:47`) — an underscore-joined composite of two values
    that themselves contain underscores, which cannot be decomposed back into a
    pair at all.

    **R5.8's idempotence is free here:** `mirrored` is a `set` and the filter is
    a membership test, so two `s3_table_bucket` rows deriving one pair exclude
    one Glue row once, and no `s3_table_bucket` row is ever a filter *target* —
    ambiguity can suppress a mirror but can never suppress a real schema.

    **The strip is the second half and it lives here, not in
    `_mirror_excluded_rows`.** Every path that builds a *response* goes through
    this function, so the internal keys cannot cross the API boundary by
    omission; the per-id path calls `_mirror_excluded_rows` directly because it
    needs them.
    """
    kept = _mirror_excluded_rows(schemas)

    for row in kept:
        for key in _INTERNAL_ROW_KEYS:
            row.pop(key, None)

    return kept


def _paginated_items(client: Any, operation: str, result_key: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
    """Yield every item of a boto3-paginated list operation (R6.5).

    One place where `get_paginator` is called, so the three enumerations that
    have a paginator — `glue:GetDatabases`, `glue:GetTables` and
    `s3:ListObjectsV2` — cannot be paginated inconsistently or forgotten one at
    a time. Each returned exactly one page before this
    (`LGR-dd80c6d-1`, **backfilled**, records the same defect class already
    fixed for the stats and approvals handlers).

    `result_key` is passed explicitly rather than read out of the paginator
    config: it is the key the caller then reads, so naming it at the call site is
    what makes a mismatch a visible error instead of a silently empty source.

    Unbounded, deliberately — and a caller depends on it
    -----------------------------------------------------------------------
    There is **no page cap, no wall-clock deadline and no concept of
    truncation** here. Every page is walked and everything is yielded, so a
    short result is always a short *source* and never a stopped enumeration.
    That is a decision, not an omission (p66 O5,
    `.kiro/specs/p66-glue-catalog-target-selection/design.md` §4.7): a bound
    needs a measured cost to justify it, and none has been measured for these
    enumerations.

    **What depends on it.** p66 R2.5 forbids the Glue Catalog Table_Picker's
    empty state claiming a database is empty when the enumeration was truncated
    or bounded. The picker is allowed to make that positive claim — "this
    database holds no tables" — precisely *because* a zero-length result from
    here cannot be a truncated one. Add a cap or a deadline and R2.5's premise
    becomes false **silently**: the picker keeps asserting a cause nothing
    checks any more. p66 R3.4 and R2.6 are the criteria that would then have to
    fire, and today they have no producer by construction.

    **The absence reads as a choice because the contrast is in this module.**
    `_resolve_display_labels` below *is* deadline-bounded, ratified as such by
    `LGR-14d6dd3-2`. The difference is what the cost scales with: that is a
    per-object fan-out, one `GetObject` per row, so its wall clock is a function
    of row count and unmeasured per-read latency. This is a page walk — one
    round trip per page, with the service choosing the page size.

    **Revisit when** either Glue enumeration is measured to breach the
    dashboard API's 25 s budget in a real account, or a bound is added for any
    other reason. At that point R3.4 and R2.6 become live and
    `LGR-9a35b2d-3`'s always-present `partial_counts` is the shape to copy — and
    the picker's positive empty-state claim has to be withdrawn in the same
    change, not after it.

    **Pinned by a test, not by this paragraph alone.**
    `tests/unit/lambdas/dashboard_api/test_paginated_items_unbounded_guard.py`
    reads this signature and this body and fails when a bound appears, so a
    future bound is a loud failure rather than a quiet invalidation.
    """
    for page in client.get_paginator(operation).paginate(**kwargs):
        yield from page.get(result_key, []) or []


def _paginate_continuation_token(
    list_call: Callable[..., Mapping[str, Any]], result_key: str, **kwargs: Any
) -> Iterator[dict[str, Any]]:
    """Yield every item of a `continuationToken`-paginated list call (R6.5).

    The S3 Tables list operations are the two enumerations in this file with no
    boto3 paginator *guaranteed* to be present, so they get a hand-rolled loop
    rather than `client.get_paginator(...)`.

    **Why a loop and not a paginator, stated because the reason is not the one
    DD4 gives.** DD4 says `list_table_buckets` / `list_tables` "have no
    paginator". That is **not true of the botocore in this repo's virtualenv**
    (1.43.33), which does ship both — verified by
    `botocore.session.get_session().get_paginator_model("s3tables")`. The loop is
    kept anyway, and the honest reason is version independence: the Lambda
    runtime supplies its own botocore, `get_paginator` raises
    `OperationNotPageableError` when the loaded version predates the S3 Tables
    paginator config, and the S3 Tables block in `_collect_schema_rows` sits
    inside a swallowing `except` — so on an older runtime the whole S3 Tables
    source would simply vanish from the listing with a `warning`, which is the
    failure mode R1.4 and R5.3 exist to remove. The token contract is in the
    service API itself and cannot go away underneath us.

    Terminates on a falsy `continuationToken`, which is what both operations
    return on the last page.
    """
    continuation_token: str | None = None
    while True:
        params = dict(kwargs)
        if continuation_token:
            params["continuationToken"] = continuation_token
        response = list_call(**params)
        yield from response.get(result_key, []) or []
        continuation_token = response.get("continuationToken")
        if not continuation_token:
            return


def _collect_schema_rows() -> _SchemaEnumeration:
    """Enumerate every schema all three sources hold, without resolving fields.

    DD4 / R6.5. Two changes from the pre-p35 shape, and they are separable:

    **1. Every enumeration is paginated.** The five list calls this file makes —
    `glue:GetDatabases`, `glue:GetTables` per database, `s3:ListObjectsV2`,
    `s3tables:ListTableBuckets` and `s3tables:ListTables` per bucket — each
    returned exactly one page before, so past each API's first page the listing
    **silently under-reported**. That is what makes R6.1's search unable to find
    a schema that exists: the search runs client-side over the options the
    picker was given (`filteringType="auto"`), so anything the listing dropped
    is unfindable. `LGR-dd80c6d-1` (**backfilled**) records unbounded
    enumeration in `dashboard_api` as a recognised defect class already fixed
    for the stats and approvals handlers; this is the same fix in the file that
    was skipped.

    (DD4 and task 4.4 both say "six unpaginated calls". Their own enumeration
    names **five**, and five is what this file contains. Recorded rather than
    silently corrected.)

    **2. No field list is resolved here.** The two per-row reads are gone: the
    `s3:GetObject` on each uploaded schema body and the `s3:GetObject` on each
    Iceberg metadata file (which also cost an `s3tables:GetTable` first). Field
    **lists** now belong to `GET /api/schemas/{id}` — see `_resolve_row_fields`
    — which is the only consumer that needs them.

    Pagination and per-row reads had to move **together**. Paginating while
    still resolving every field makes those two reads unbounded on a 25-second
    synchronous request, so the endpoint's failure mode becomes a **timeout** —
    the one outcome that makes every requirement in this spec unobservable at
    once. Capping the listing at N rows was the other rejected option: R6.1's
    search cannot find a schema outside the cap, which is Requirement 6's own
    defect reintroduced one layer down.

    **`fieldCount`, and why `null` is load-bearing.** A row carries a real
    `fieldCount` only where the source supplies one **free**: Glue, from the
    `get_tables` page's `StorageDescriptor.Columns`. Neither
    `s3tables:ListTables`/`GetTable` nor `s3:ListObjectsV2` returns a column
    count, so those rows carry **`fieldCount: None`** and **no `fields` key at
    all**. Collapsing that to `0` would break R5.1's zero-field guard in the
    worst direction — the picker would refuse **every** S3 Tables and uploaded
    schema as empty. It is the same "no fields" versus "not established"
    distinction R1.4 and R5.3 turn on.

    Raises rather than returning a response: `get_schemas` and `get_schema` each
    decide what an unbuildable listing means for them.

    **3. Task 4.5 — the five per-source `except` clauses are narrowed to
    `_SOURCE_ERRORS`, and every one of them records what it lost.** They were
    `except Exception` + `logger.warning`, which made a source that **failed** and
    a source that is **empty** byte-identical in the response (R1.4, R5.3). Each
    now catches only what a source failure actually is and appends a
    `_SourceFailure`, so the response states which sources are missing alongside
    the rows that succeeded — design §5 risk 10's mitigation, and the reason
    narrowing is shippable at all: the first consequence of narrowing is that
    permissions and service problems start being *reported*, which without partial
    success reads as "the schemas page broke". See `_SOURCE_ERRORS` for what is
    deliberately no longer caught.
    """
    logger.info("Fetching schemas from multiple sources")
    schemas: list[dict[str, Any]] = []
    failures: list[_SourceFailure] = []
    region = os.environ.get("AWS_REGION", "us-west-2")

    # Get schemas from Glue Data Catalog
    try:
        glue_client = boto3.client("glue", region_name=region)

        # R6.5 — paginated. `get_databases` returns at most 100 databases per
        # page server-side, so an account past 100 lost every database after
        # the first page before this.
        for database in _paginated_items(glue_client, "get_databases", "DatabaseList"):
            database_name = database["Name"]

            # The JDBC discovery database holds only crawler copies of SOURCE
            # tables, written by Detect Schema and by each JDBC pipeline's
            # runtime crawler. None is a target schema; listed, they were most
            # of the Glue rows. Matched by exact name, not by prefix, because a
            # per-pipeline database for a pipeline named "jdbc-discovery-…"
            # would share any prefix (`LGR-acca05d-1`).
            if database_name == _jdbc_discovery_database():
                continue

            try:
                # R6.5 — paginated per database, for the same reason.
                for table in _paginated_items(glue_client, "get_tables", "TableList", DatabaseName=database_name):
                    # A Glue registration of S3 Tables storage: the mirror
                    # `create_infrastructure` writes so the Data Quality console
                    # can describe the table. `_exclude_glue_mirrors` drops a
                    # mirror while its S3 Tables table is listed (`LGR-acca05d-1`);
                    # this drops the ones it cannot, whose table bucket was
                    # deleted. Such a row has no columns, so it was a "0 fields"
                    # entry nothing could use or delete through the UI. The test
                    # is the table's storage, which only S3 Tables manages, never
                    # its database name, so a per-pipeline database is untouched.
                    if _is_table_bucket_storage(table):
                        continue
                    # Get last modified time
                    last_modified = table.get("UpdateTime") or table.get("CreateTime")
                    if last_modified:
                        last_modified = (
                            last_modified.isoformat() if hasattr(last_modified, "isoformat") else str(last_modified)
                        )
                    else:
                        last_modified = datetime.now().isoformat()

                    # DD4 — the Glue field list is the one that stays on the
                    # listing: it is already in the `get_tables` page, so it
                    # costs no read. No `s3:GetObject` was ever made per Glue
                    # row, which is why this source keeps a real `fieldCount`.
                    fields = [
                        {
                            "name": column["Name"],
                            "type": column["Type"],
                            # R5.3 — **no `nullable` key.** Glue's
                            # `StorageDescriptor.Columns[]` carries no
                            # nullability at all, so this branch has no answer to
                            # give. It used to hardcode `True`, which reported
                            # "the source did not say" as "the source said
                            # optional" — the collapse `LGR-4854b3d-1` forbids
                            # inside a scope that names this file. Absence is this
                            # file's spelling for "not established"
                            # (`LGR-44d601e-1`, and `_schema_detail`'s
                            # `displayLabel`, which is "absent, not null" for the
                            # same reason); a `nullable: None` would be a third
                            # spelling of two facts. Do NOT reinstate either.
                            # R1.5/R1.6 — task 4.5's producer-side split. The
                            # column's `Comment` is the **schema's own** declared
                            # description, so it belongs under `description`. It
                            # fed `context` before, which is why the API emitted
                            # no field-level `description` at all,
                            # `normalizeField` resolved it to `undefined`
                            # (`use-schemas.ts:11`), and the Description column
                            # rendered `—` for every field of every schema — one
                            # real value under the wrong heading, beside a
                            # heading that was structurally always blank.
                            "description": column.get("Comment", ""),
                            # `context` is the Context_Sidecar's annotation and
                            # nothing else (R2.3). A Glue table is not an
                            # Uploaded_Schema, so it can never have one — empty
                            # here is a fact about the source, not a placeholder.
                            "context": "",
                        }
                        for column in table.get("StorageDescriptor", {}).get("Columns", [])
                    ]

                    schemas.append(
                        {
                            "schemaId": f"glue_{database_name}_{table['Name']}",
                            "name": f"{database_name}.{table['Name']}",
                            # R5.5 — derived from this table, not hardcoded.
                            "targetType": _target_type_label(_glue_target_type(table)),
                            "source": "glue_catalog",
                            "lastModified": last_modified,
                            "version": "1.0",
                            "fields": fields,
                            # DD4 — free from the page above, so this source is
                            # the one that can state a count rather than `null`.
                            "fieldCount": len(fields),
                            # R5.7 — the pair the exclusion matches on, carried
                            # rather than recomputed from `name` or `schemaId`
                            # (see `_exclude_glue_mirrors`). Stripped before the
                            # response.
                            "_glue_database": database_name,
                            "_glue_table": table["Name"],
                        }
                    )

            # Task 4.5 — narrowed, and it reports the scope it lost. One database
            # the caller cannot read leaves the Glue list short by that database's
            # tables; every other database still contributes.
            except _SOURCE_ERRORS as e:
                _record_source_failure(failures, "glue_catalog", f"database {database_name}", e)

    # Task 4.5 — narrowed. This one is the whole Glue source: `get_databases`
    # denied or unreachable means no Glue row can be produced at all, which is
    # exactly the case R1.4 refuses to let look like an empty catalog.
    except _SOURCE_ERRORS as e:
        _record_source_failure(failures, "glue_catalog", "all Glue databases", e)

    # Get uploaded JSON schemas from S3 target-schemas/shared/ folder
    #
    # Task 4.5 — `uploaded_bucket` is bound BEFORE the `try` so the narrowed
    # `except` can still name a bucket when the failure happened while *resolving*
    # one: `get_aws_account_id()` is itself an AWS call, so a `ClientError` there
    # would reach the handler with `bucket_name` unbound and turn a reported
    # source failure into a `NameError` inside its own error path.
    uploaded_bucket = os.environ.get("TARGET_SCHEMAS_BUCKET")
    try:
        s3_client = boto3.client("s3", region_name=region)
        account_id = get_aws_account_id()
        bucket_name = os.environ.get("TARGET_SCHEMAS_BUCKET", f"adi-target-schemas-{account_id}-{region}")
        uploaded_bucket = bucket_name

        # R6.5 — paginated. `list_objects_v2` returns at most 1000 keys per
        # page, so the 1001st uploaded schema was invisible to the picker and
        # therefore to R6.1's search.
        for obj in _paginated_items(s3_client, "list_objects_v2", "Contents", Bucket=bucket_name, Prefix="shared/"):
            if not obj["Key"].endswith(".json") or obj["Key"] == "shared/":
                continue

            schema_name = obj["Key"].replace("shared/", "").replace(".json", "")

            # DD4 — no `get_object` here. The body was read once per row purely
            # to count and list its fields; both now happen on
            # `GET /api/schemas/{id}` (`_uploaded_schema_fields`).
            #
            # A consequence worth stating: a `shared/*.json` object whose body
            # is unreadable or unparseable used to be dropped from the listing
            # by the per-row `except`. It is now listed, with
            # `fieldCount: None`. That is the honest outcome — the object exists
            # and R1.2 says an id a source *can* produce must not 404 — and it
            # is the same "not established" rather than "empty" claim
            # `fieldCount: None` makes everywhere else.
            schemas.append(
                {
                    "schemaId": schema_name,
                    "name": schema_name,
                    # R5.5 — an Uploaded_Schema is a target schema, not
                    # a target; same vocabulary, same label as before.
                    "targetType": _target_type_label("uploaded"),
                    "source": "uploaded",
                    "lastModified": obj["LastModified"].isoformat(),
                    "version": "1.0",
                    # DD4 — `list_objects_v2` returns key, size and
                    # LastModified. There is no column count in it, so this is
                    # `None` and never `0`: "not established", not "empty".
                    # There is deliberately no `fields` key either — an absent
                    # list cannot be mistaken for an empty one.
                    "fieldCount": None,
                    # DD4 — the key this enumeration actually read, carried so
                    # the per-id endpoint resolves fields from the same address
                    # rather than re-deriving one (`LGR-cad8032-1`). Stripped
                    # before the response.
                    "_object_key": obj["Key"],
                }
            )

    # Task 4.5 — narrowed. `bucket_name` is this block's local, the target-schemas
    # bucket; naming it in the scope is what tells an operator *which* bucket the
    # `AccessDenied` or `NoSuchBucket` was on, which is the one fact the old
    # warning had and the response did not.
    except _SOURCE_ERRORS as e:
        _record_source_failure(failures, "uploaded", f"bucket {uploaded_bucket or '<unresolved>'}", e)

    # Get S3 Table Bucket schemas using S3 Tables API
    try:
        s3tables_client = boto3.client("s3tables", region_name=region)

        # R6.5 — paginated through the `continuationToken` loop; see
        # `_paginate_continuation_token` for why this one is not a paginator.
        for bucket_info in _paginate_continuation_token(s3tables_client.list_table_buckets, "tableBuckets"):
            bucket_arn = bucket_info["arn"]
            bucket_name = bucket_info["name"]

            try:
                # R6.5 — paginated per table bucket, same loop.
                for table_info in _paginate_continuation_token(
                    s3tables_client.list_tables, "tables", tableBucketARN=bucket_arn
                ):
                    table_name = table_info["name"]
                    namespace_list = table_info.get("namespace", [])
                    namespace = ".".join(namespace_list)

                    # Convert datetime to string
                    last_modified = table_info.get("modifiedAt") or table_info.get("createdAt") or ""
                    if last_modified and hasattr(last_modified, "isoformat"):
                        last_modified = last_modified.isoformat()

                    # DD4 — neither `s3tables:GetTable` nor the Iceberg metadata
                    # `s3:GetObject` happens here any more. Those were two calls
                    # *per row*, and they were the entire reason a paginated
                    # listing would have timed out. `_s3_tables_schema_fields`
                    # makes them for the one row the detail endpoint is asked
                    # about.
                    schemas.append(
                        {
                            "schemaId": f"s3_table_{bucket_name}_{namespace}_{table_name}",
                            "name": f"{bucket_name}.{namespace}.{table_name}"
                            if namespace
                            else f"{bucket_name}.{table_name}",
                            # R5.5 — same vocabulary as the wizard's
                            # `s3_tables` label, and the same string as
                            # before: this row was already honest.
                            "targetType": _target_type_label("s3_tables"),
                            "source": "s3_table_bucket",
                            "lastModified": last_modified,
                            "version": "1.0",
                            # DD4 — `list_tables` returns name, namespace, arn
                            # and timestamps; `get_table` adds a metadata
                            # *location*, not a column count. So `None`, never
                            # `0`, and no `fields` key.
                            "fieldCount": None,
                            # R5.7 — the three values `create_infrastructure`
                            # derives the mirror's Glue pair from. Carried
                            # here, matched in `_exclude_glue_mirrors`,
                            # stripped before the response.
                            #
                            # `bucket_name` is THIS block's local — the S3
                            # Tables table-bucket name (`bucket_info["name"]`)
                            # — not the uploaded block's target-schemas
                            # bucket, which shares the name. That shadowing
                            # is why the source blocks must not be reordered.
                            #
                            # `namespace` is passed as read. S3 Tables
                            # supports single-level namespaces only
                            # (`create_infrastructure:160-161`), so the
                            # joined value equals the one element, and
                            # `NamespaceName` forbids hyphens — so it is
                            # already in the underscore form
                            # `create_infrastructure` wrote with. Re-running
                            # its normalisation here would make this reader
                            # disagree with that writer.
                            "_bucket_name": bucket_name,
                            "_namespace": namespace,
                            "_table_name": table_name,
                            # DD4 — the table bucket ARN `get_table` needs. It
                            # is NOT reconstructible from `schemaId`, and
                            # hand-assembling an ARN from account and region
                            # would be a second definition of an address the
                            # enumeration already has. Stripped before the
                            # response.
                            "_bucket_arn": bucket_arn,
                        }
                    )

            # Task 4.5 — narrowed. One table bucket that cannot be listed leaves
            # the S3 Tables list short by that bucket's tables. It also matters to
            # the dedupe: an `s3_table_bucket` row that was never enumerated
            # cannot exclude its Glue mirror, so this failure is *why* a duplicate
            # pair may still be visible, and the response now says so instead of
            # leaving R5.4 looking broken.
            except _SOURCE_ERRORS as table_error:
                _record_source_failure(failures, "s3_table_bucket", f"table bucket {bucket_name}", table_error)

    # Task 4.5 — narrowed. The whole S3 Tables source. On a Lambda runtime whose
    # botocore predates the `s3tables` service model this is where the
    # `UnknownServiceError` lands (a `BotoCoreError`), and before this it made the
    # entire source vanish with only a log line — see
    # `_paginate_continuation_token` for the same version hazard one layer down.
    except _SOURCE_ERRORS as e:
        _record_source_failure(failures, "s3_table_bucket", "all S3 Tables table buckets", e)

    return _SchemaEnumeration(rows=schemas, failures=failures)


def get_schemas() -> dict[str, Any]:
    """Get all schemas from multiple sources (Glue, S3, uploaded schemas).

    **Per-source partial success — task 4.5, design §5 risk 10.** The response
    carries two keys:

    - ``schemas`` — the rows every source that *could* be read contributed.
      Unchanged in shape, name and content, which is what keeps
      ``fetchSchemas`` (`use-schemas.ts:30-37`) working: it reads ``body.schemas``
      and nothing else.
    - ``sourcesFailed`` — one entry per source, or per scope within a source, that
      could not be enumerated, each naming the ``source`` token, the ``scope``, and
      the error.

    ``sourcesFailed`` is emitted **always, empty list included**. An empty list is
    the affirmative claim "every source was read to the end", and that is the claim
    R1.4 and R5.3 need: before this, zero uploaded rows because the prefix is empty
    and zero uploaded rows because ``list_objects_v2`` raised ``AccessDenied`` were
    the **identical** response, so an empty picker and a broken permission looked
    the same to the user. Omitting the key when nothing failed would put that
    ambiguity straight back, because an absent key is indistinguishable from a
    deploy that predates this one.

    **A non-empty ``sourcesFailed`` is still a ``200``, deliberately.** One broken
    source degrades one section, not the page — that is risk 10's whole mitigation.
    A ``500`` here would throw away every row the other two sources produced in
    order to report the one that failed, which is a worse answer than a short list
    plus the reason it is short.

    **A ``500`` is still reachable** and now means something narrower: the outer
    clause below catches what `_SOURCE_ERRORS` deliberately does not — a break in
    an AWS response contract, a programming error, a classified
    ``StepFailure`` / ``TransientStepFailure`` passing through
    (`LGR-78ce0b7-13`). Those are not "a source is unreadable" and must not be
    reported as one.
    """
    # p36 task 2.4 — the anchor the Display_Label pass's deadline is derived
    # from. Taken before the enumeration, not inside the label pass, so the
    # window available for label reads is what is left of R1.9's budget rather
    # than a fixed duration: a slow enumeration shrinks it. See
    # `_label_pass_deadline`.
    request_started = time.monotonic()

    try:
        enumeration = _collect_schema_rows()

        # D1 / R5.3, R5.4, R5.7, R5.8 — the mirror exclusion, after all three
        # sources are enumerated and before the response is built. It has to be
        # here: the Glue rows are appended first, so this is the earliest point
        # at which an `s3_table_bucket` row exists to match against.
        schemas = _exclude_glue_mirrors(enumeration.rows)

        # p36 item 1 / R1.1-R1.3 — the Display_Label post-pass. Here rather than
        # inside `_collect_schema_rows`: its cost is bounded independently of the
        # page count, which is what `LGR-4854b3d-1`'s fence is actually about.
        # See `_resolve_display_labels`. Before `failed` is built, so its R1.10
        # entries reach the response.
        _resolve_display_labels(schemas, enumeration.failures, started_at=request_started)

        failed = [failure.as_response_entry() for failure in enumeration.failures]
        if failed:
            logger.info(
                f"Found {len(schemas)} schemas total; "
                f"{len(failed)} source(s) could not be enumerated: {sorted({f['source'] for f in failed})}"
            )
        else:
            logger.info(f"Found {len(schemas)} schemas total")

        return create_response(200, {"schemas": schemas, "sourcesFailed": failed})

    except Exception as e:
        logger.error(f"Error fetching schemas: {e}")
        return create_response(500, {"error": "Failed to fetch schemas", "details": str(e)})


#: The Context_Sidecar's address, as its two halves rather than as one f-string.
#: p36 task 2.2 needs the **inverse** of `_context_sidecar_key` — a schema id
#: recovered from a listed key — and a second spelling of either half is exactly
#: the shape `LGR-cad8032-1` (**template / ratified**) exists to prevent: an
#: inverse can drift from the forward form without either side looking wrong, and
#: two definitions of one address silently disabled a feature once already. Both
#: directions read these two constants, so there is one definition to change.
_CONTEXT_SIDECAR_PREFIX = "schemas/uploaded/"
_CONTEXT_SIDECAR_SUFFIX = "-metadata.json"


def _context_sidecar_key(schema_id: str) -> str:
    """The Context_Sidecar's key for one schema id.

    ``schemas/uploaded/{schema_id}-metadata.json`` — the shape
    ``handlers/pipelines_crud.py:785`` already derives and ``:800`` already
    reads. Keyed on the schema **id**, which R4.10 freezes, so a rename cannot
    invalidate this address (D2 / ``LGR-acca05d-2``, ``LGR-cad8032-1``).

    `_annotated_schema_ids` is this function read backwards, over the same two
    constants.
    """
    return f"{_CONTEXT_SIDECAR_PREFIX}{schema_id}{_CONTEXT_SIDECAR_SUFFIX}"


def _read_context_sidecar(schema_id: str) -> dict[str, Any]:
    """Read one schema's Context_Sidecar, or ``{}`` when there is none.

    Absence is the normal case and is **not** an error: nothing writes this
    object until task 8.5 (D3, ``LGR-acca05d-3``), and R2.6 requires an absent
    sidecar to be treated as "no context recorded". Logged at ``debug`` for the
    same reason ``pipelines_crud.py:783-789`` logs its own miss below ``error``.

    **Task 4.5 narrowed this clause** from ``except Exception``. It catches
    ``_DOCUMENT_ERRORS`` — ``NoSuchKey`` is the expected case and ``AccessDenied``
    the diagnosable one — plus ``ValueError`` for a sidecar that is not valid JSON.
    Nothing else: a classified ``StepFailure`` / ``TransientStepFailure`` passes
    through (`LGR-78ce0b7-13`), and so does a programming error, which a clause
    this quiet — ``debug``, on a path taken on every detail request — would
    otherwise hide completely.
    """
    region = os.environ.get("AWS_REGION", "us-west-2")
    try:
        s3_client = boto3.client("s3", region_name=region)
        account_id = get_aws_account_id()
        bucket_name = os.environ.get("TARGET_SCHEMAS_BUCKET", f"adi-target-schemas-{account_id}-{region}")
        obj = s3_client.get_object(Bucket=bucket_name, Key=_context_sidecar_key(schema_id))
        sidecar = json.loads(obj["Body"].read())
    except _DOCUMENT_ERRORS as e:
        # Absence and unreadability are both "no context recorded" here. This is
        # a read on the presentation path: the schema itself is already in hand,
        # and R2.6 requires an absent sidecar not to be reported as an error.
        logger.debug(f"No context sidecar for schema {schema_id}: {e}")
        return {}
    return sidecar if isinstance(sidecar, dict) else {}


# ---------------------------------------------------------------------------
# p36 item 1 — the listing resolves Display_Labels in a post-pass
# ---------------------------------------------------------------------------
# R1.1, R1.2, R1.3, R1.8, R1.11, R1.12. Before this, `_schema_detail:1115-1117`
# was the ONLY read of a sidecar's `display_name` on the read path, and every
# surface except the detail modal is built from a Schema_Listing_Row — so every
# surface except the detail modal showed the schema id.
#
# This block sits beside `_context_sidecar_key` / `_read_context_sidecar` on
# purpose: it is the third reader of that one address, and the gate below is
# shared with the detail path rather than written a second time.


def _display_label_from_sidecar(sidecar: Mapping[str, Any]) -> str | None:
    """The Display_Label one Context_Sidecar document holds, or ``None`` (R1.3).

    **The one definition of what a sidecar's label is**, read by both surfaces
    that report one — `_schema_detail` and `_resolve_display_labels`. That is the
    mechanism for R1.3: the listing and the detail cannot disagree about a label
    because there is one function deciding it, not two gates that happen to
    match (`LGR-cad8032-1`, **template / ratified**).

    **The gate is truthy, and it must stay truthy rather than becoming
    ``is not None``.** `LGR-44d601e-1` (**template / ratified**) settled that
    clearing a label *removes* the key and that storing ``""`` was rejected — but
    a sidecar written by a path predating that rule can hold ``""``, and the
    detail path has always treated that as absent (`_schema_detail`'s
    ``if display_label:``). An ``is not None`` gate would emit ``""`` on the
    listing where the detail emits nothing, breaking R1.3 on exactly the legacy
    data the truthy gate exists for.

    A whitespace-only label is **truthy** and is returned verbatim: the gate
    trims, the value does not (R2.4, the same rule `upload_schema` stores by at
    ``:1623-1628``).

    ``_SIDECAR_DISPLAY_NAME`` is the write path's constant (``:2191``), resolved
    at call time — one spelling of the field for the writer and both readers.
    """
    label: str | None = sidecar.get(_SIDECAR_DISPLAY_NAME)
    return label if label else None


def _target_schemas_bucket() -> str:
    """The target-schemas bucket, the same expression `_read_context_sidecar` uses.

    ``TARGET_SCHEMAS_BUCKET`` with the account-and-region default, resolved at
    call time (`LGR-3e45e65-1`, **template / ratified**: nothing here caches a
    region). `get_aws_account_id` is memoized in a module global
    (`shared/env_utils.py:13-15`), so the default costs one STS call per warm
    container.

    `_collect_schema_rows` and `_read_context_sidecar` keep their own inline
    copies deliberately and are **not** routed through this: the enumeration
    binds `uploaded_bucket` before its `try` so a failure while *resolving* the
    bucket can still name one (`:551-556`), and ``schemas.py:2219-2232`` records
    that the sidecar reader's tolerances are deliberately not converged with
    anything else. This exists so the label post-pass does not add a fourth
    spelling of the same env read.
    """
    region = os.environ.get("AWS_REGION", "us-west-2")
    return os.environ.get("TARGET_SCHEMAS_BUCKET", f"adi-target-schemas-{get_aws_account_id()}-{region}")


def _annotated_schema_ids(s3_client: Any, bucket: str) -> set[str]:
    """Every Uploaded_Schema id that has a Context_Sidecar object (R1.8, R1.11).

    One ``list_objects_v2`` over ``Prefix="schemas/uploaded/"``, **through
    `_paginated_items`** so R6.5's single definition of pagination still holds
    here and `LGR-dd80c6d-1`'s (**backfilled**) unbounded-enumeration defect
    class is not reintroduced one prefix over: a bucket past 1000 sidecars would
    otherwise lose every label after the first page, silently and with no
    warning.

    The id is the **exact inverse of `_context_sidecar_key`** — the same two
    constants read backwards — and a key carrying neither half is skipped rather
    than guessed at (`LGR-cad8032-1`, **template / ratified**). The server-side
    ``Prefix`` already implies the first check; it is asserted anyway so the
    derivation is correct on its own terms rather than on the request's.

    **This call is what makes R1.8's second half true**: a schema with no sidecar
    object is never read, because it is never in this set. It is also why a label
    still needs a per-object read at all — ``list_objects_v2`` returns key, size
    and ``LastModified``, no user metadata and no tags, and `upload_schema` sets
    neither ``Metadata=`` nor ``Tagging=`` (R1.11).

    ``s3:ListBucket`` on this prefix is already granted to the dashboard API's
    role (verified in the p36 design phase), so no IAM change accompanies this.
    """
    annotated: set[str] = set()
    for obj in _paginated_items(
        s3_client, "list_objects_v2", "Contents", Bucket=bucket, Prefix=_CONTEXT_SIDECAR_PREFIX
    ):
        key = obj.get("Key", "")
        if not key.startswith(_CONTEXT_SIDECAR_PREFIX) or not key.endswith(_CONTEXT_SIDECAR_SUFFIX):
            continue
        schema_id = key[len(_CONTEXT_SIDECAR_PREFIX) : -len(_CONTEXT_SIDECAR_SUFFIX)]
        if schema_id:
            annotated.add(schema_id)
    return annotated


#: The label post-pass's own client timeouts, spelled as constants so the bound
#: they contribute to is re-derivable rather than inherited. Both are far below
#: the Lambda's own 25 s timeout (`compute-stack.ts:630`): a hung sidecar read
#: must never be the thing that ends the request. botocore's defaults are 60 s,
#: which is longer than the whole request — one of the three reasons this path
#: does not reuse `_read_context_sidecar` (see `_resolve_display_labels`).
_LABEL_READ_CONNECT_TIMEOUT_SECONDS = 2
_LABEL_READ_READ_TIMEOUT_SECONDS = 3
#: One retry, not botocore's default of three: a label is degradable (R1.10) and
#: retrying it three times spends the request's budget on the least important
#: field on the page.
_LABEL_READ_MAX_ATTEMPTS = 2

#: D2's fan-out width. 10 matches `pipelines_crud.py:357`, the higher of the two
#: in-tree `ThreadPoolExecutor` widths in **this same Lambda**
#: (`pipelines_runs.py:575` uses 5) — chosen to sit inside existing house
#: practice rather than to be optimal. S3 `GetObject` has no per-partition limit
#: to throttle against the way the DynamoDB queries those two sites fan out over
#: do.
_LABEL_READ_WORKERS = 10

#: R1.9's budget: the listing answers within 20 s. The Lambda's own timeout is
#: 25 s (`compute-stack.ts:630`, confirmed live in the design phase), and the API
#: behaviour on the CloudFront distribution is `CachePolicy.CACHING_DISABLED`
#: (`LGR-2d64f7a-1`, **backfilled**), so every request pays full cost. The 5 s
#: left in hand is what makes an overrun a **reportable** outcome (R1.10) rather
#: than a gateway timeout carrying no body.
_LISTING_TIME_BUDGET_SECONDS = 20.0

#: Held back out of that budget for everything after the label pass returns —
#: `create_response`, the JSON serialisation of up to 1000 rows, and margin.
#: A test that needs the deadline to bind shortens the budget or lengthens this,
#: rather than asserting a wall-clock duration.
_LABEL_PASS_RESERVE_SECONDS = 2.0

#: How many schema ids one aggregated R1.10 entry names. Bounded because the
#: entry is rendered as a single `<li>` (`SchemaManagementPage.tsx:448-451`) and
#: a shortfall of 1000 must read as one warning, not as a wall of ids.
_LABEL_FAILURE_SAMPLE_SIZE = 5


def _label_pass_deadline(started_at: float) -> float:
    """The monotonic instant the Display_Label pass must be finished by (R1.9).

    Derived **at call time** from the request's own budget rather than being a
    hard-coded duration. The deadline is anchored at the instant the request
    started, so the window actually available to the label pass is
    ``budget - reserve - elapsed`` and **shrinks when the enumeration ahead of it
    was slow**. The enumeration's own cost is unmeasured (design §5 N4), which is
    exactly why ``D`` is relative and not absolute.

    **Sizing, as an inequality so a later change can re-derive it rather than
    inherit a magic number** (design §4.2):

    - expected: ``ceil(N / W) x L``. At R1.9's shape with ``W = 10`` and an
      optimistic ``L = 25 ms`` that is ``ceil(100/10) x 25 ms = 250 ms`` — about
      **1.3 % of the 20 s budget**, which is why the deadline is not expected to
      bind.
    - guaranteed: ``min(ceil(N / W) x T_read_max, D)``. ``D`` is what makes it a
      guarantee rather than an estimate, and it is the reason R1.9 holds
      independently of ``L`` — a number this design cannot verify (design §5
      risk 3, N4).
    """
    return started_at + max(0.0, _LISTING_TIME_BUDGET_SECONDS - _LABEL_PASS_RESERVE_SECONDS)


def _absent_sidecar(error: BaseException) -> bool:
    """Whether one failed sidecar read means "there is no such object" (R1.10).

    **Absence is not a failure and must not be reported as one.** A schema
    nobody has named is the ordinary case — `_read_context_sidecar` has always
    treated it that way — and an R1.10 entry for it would make "nobody named
    this" indistinguishable from "I could not look", which is the inverse of the
    defect R1.10 fixes.

    Read through `NOT_FOUND_CLIENT_ERROR_CODES`, the one classification of "the
    object is not there" this package already imports, rather than a second
    ``NoSuchKey`` literal. Reachable even though `_annotated_schema_ids` listed
    the key first: a sidecar deleted between the listing and the read is a
    genuine absence.
    """
    if not isinstance(error, ClientError):
        return False
    return error.response.get("Error", {}).get("Code") in NOT_FOUND_CLIENT_ERROR_CODES


def _label_failure_reason(error: BaseException) -> str:
    """A short reason string for one unresolvable Display_Label.

    The error **code** where there is one, the exception class otherwise. A
    botocore message is a paragraph; several of them concatenated into one
    aggregated entry would be a paragraph per schema in a warning banner.
    """
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code")
        if code:
            return str(code)
    return type(error).__name__


def _record_label_shortfall(
    failures: list[_SourceFailure], bucket: str, unresolved: Mapping[str, str], attempted: int
) -> None:
    """Report every Display_Label that did not resolve as **one** entry (R1.10).

    **The aggregation is load-bearing, not tidiness.**
    `SchemaManagementPage.tsx:448-451` renders one ``<li>`` per `sourcesFailed`
    entry, so 1000 abandoned reads emitting 1000 entries would turn requirements
    risk 7's "this reads as *the schemas page broke*" from a perception risk into
    a certainty. One entry, naming the count and a bounded sample of ids.

    `_SourceFailure.scope` already means "how much of that source is missing", so
    a label sub-scope is the finer-grained form of an existing concept rather
    than a new one — which is why this reuses p35's shape and adds no type, no
    key and no frontend change.

    The ``error`` handed to `_record_source_failure` is **constructed** rather
    than caught, because this entry summarises many failures and no single one of
    them describes it. Going through `_record_source_failure` anyway keeps one
    writer for the log line and the response entry.
    """
    schema_ids = sorted(unresolved)
    sample = ", ".join(schema_ids[:_LABEL_FAILURE_SAMPLE_SIZE])
    if len(schema_ids) > _LABEL_FAILURE_SAMPLE_SIZE:
        sample += f", and {len(schema_ids) - _LABEL_FAILURE_SAMPLE_SIZE} more"
    reasons = ", ".join(sorted(set(unresolved.values())))
    summary = (
        f"{len(schema_ids)} of {attempted} Display_Label read(s) in {bucket} "
        f"did not resolve: {reasons} (schemas: {sample})"
    )
    _record_source_failure(
        failures, "uploaded", f"display labels for {len(schema_ids)} schema(s)", RuntimeError(summary)
    )


def _label_reader_client() -> Any:
    """The one S3 client the Display_Label post-pass reads every sidecar through.

    Constructed **once per pass** and shared, with explicit ``connect_timeout``,
    ``read_timeout`` and ``retries``. boto3 *clients* are safe to call from
    several threads (boto3 *resources* are not), which is what lets task 2.4 fan
    out over this one client rather than one client per read.
    """
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
        config=Config(
            connect_timeout=_LABEL_READ_CONNECT_TIMEOUT_SECONDS,
            read_timeout=_LABEL_READ_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": _LABEL_READ_MAX_ATTEMPTS},
        ),
    )


def _read_display_label(s3_client: Any, bucket: str, schema_id: str) -> str | None:
    """One schema's stored Display_Label, read from its Context_Sidecar.

    Raises rather than absorbing: the caller decides what an unreadable sidecar
    means, because "nobody named this" and "I could not look" are different facts
    and R1.10 is about reporting the second one.
    """
    obj = s3_client.get_object(Bucket=bucket, Key=_context_sidecar_key(schema_id))
    sidecar = json.loads(obj["Body"].read())
    return _display_label_from_sidecar(sidecar) if isinstance(sidecar, Mapping) else None


def _resolve_display_labels(
    rows: list[dict[str, Any]], failures: list[_SourceFailure], *, started_at: float | None = None
) -> None:
    """Set ``displayLabel`` on every row that has a stored one (R1.1, R1.2, R1.3).

    Mutates ``rows`` in place. Called from `get_schemas` immediately after
    `_exclude_glue_mirrors` and before the response is built.

    ``started_at`` is the monotonic instant the **request** began, which is what
    the deadline is derived from (`_label_pass_deadline`) — passing it rather than
    reading the clock here is what makes the window shrink when the enumeration
    ahead of this pass was slow. It defaults to "now" so a direct call still has a
    deadline rather than none.

    **A post-pass, not part of the enumeration, and that is the design rather
    than a way around a check.** `test_no_per_row_read_remains_in_the_listing`
    reads `_collect_schema_rows`' source and forbids a per-row read *there*; its
    actual subject (`LGR-4854b3d-1`, **template / ratified**, ``revisit_when`` =
    "the listing regains per-row field resolution") is DD4's cost **coupled to
    the page count** — resolve fields inside the enumeration and every row the
    pagination fix newly reveals adds guaranteed cost to a 25 s synchronous
    request. This pass is bounded independently of the row count, by the deadline
    below, so adding a row adds no guaranteed cost. If that ever stops being
    true, amend the fence deliberately instead of routing around it.

    **After the exclusion rather than before.** Only ``glue_catalog`` rows are
    ever dropped (`_mirror_excluded_rows`) and only ``uploaded`` rows carry
    labels, so the set needing a label is identical either way. After is chosen
    so no label is resolved for a row that will not ship, and so this pass sees
    exactly the rows the response carries. The internal-key strip is irrelevant
    to it: ``schemaId`` and ``source`` are the only two fields read and both
    survive (`_INTERNAL_ROW_KEYS`).

    **The key is set only where a label exists — absent, never ``None`` and never
    ``""``** (R1.2, `LGR-44d601e-1`). A client falling back on a missing key
    renders the id; one handed ``null`` renders nothing.

    **R1.12 by omission:** exactly one key is written, ``displayLabel``. Nothing
    here reads or assigns ``schemaId`` or ``name``. A label reaching ``schemaId``
    becomes ``target_schema_name`` and is interpolated into six candidate object
    keys before ``Could not find uploaded schema`` six steps later, in a
    different Lambda, with no other symptom.

    **Why this does not reuse `_read_context_sidecar`**, and the third reason is
    the load-bearing one: it constructs a fresh client on **every** call
    (``:801``) with no shared connection pool; it passes no timeout config, so a
    hung read is bounded only by botocore's 60 s default — longer than the whole
    request; and it returns ``{}`` for ``AccessDenied`` (``:810-815``), which is
    precisely the "could not check" R1.10 must report rather than absorb. The two
    readers are deliberately **not** converged in either direction
    (``schemas.py:2219-2232``): that reader's tolerance on a *write* path is what
    stops it silently deleting a user's ``field_contexts``.

    **D2 — the reads are fanned out under a wall-clock deadline** (design §4.2,
    R1.8, R1.9). Sequential cost is ``N x L``, and R1.8 permits
    ``N = |Uploaded|``: at 1000 annotated schemas and an optimistic 25 ms round
    trip that is ~25 s, past the Lambda's own timeout — DD4's failure mode
    returning through a different door. The deadline makes R1.9 hold **by
    construction and independently of ``L``**, which is the only property worth
    having here because ``L`` is unmeasured (design §5 risk 3, N4).

    **Why this is not `LGR-b46a5bb-1`** (**standing invariant**: no
    ``threading.Thread`` for post-response work in ``lambda/**``, enforced with
    **no allowlist** by `tests/unit/test_no_lambda_threads_prop.py`,
    `LGR-b46a5bb-4`, **template / ratified**). Three claims, each verified rather
    than inferred: (1) ``find_thread_uses`` resolves only bindings from
    ``import threading`` / ``from threading import Thread`` and matches
    ``ast.Attribute(attr="Thread")`` or a ``Name`` bound to ``Thread`` —
    ``ThreadPoolExecutor`` is structurally neither; (2) it is already live twice
    in **this same Lambda** and both sites pass the check today
    (`pipelines_crud.py:357`, `pipelines_runs.py:575`); (3) the invariant's
    subject is a thread doing **post-response** work something depends on, and
    nothing here consumes a read that arrives after the deadline — the response is
    complete and correct without it. The precedent is **code, not a ratified
    decision**: `why` on `pipelines_runs.py:575` returns only a broad backfilled
    snapshot, which is why D2 gets a `### Decision` block of its own.

    R1.10 reporting, two deliberately distinct scopes, both through p35's
    existing `_record_source_failure` / `_SourceFailure` with the ``source`` token
    ``"uploaded"`` — no new type, no new key, no frontend change:

    - the prefix listing failed, so **no** label is resolvable at all → **one**
      entry scoped ``display labels in {bucket}``;
    - individual reads failed or were abandoned at the deadline → **one
      aggregated** entry naming the count and a bounded sample of ids
      (`_record_label_shortfall`).

    ``NoSuchKey`` is **not** a failure and produces no entry (`_absent_sidecar`).
    """
    uploaded = {row["schemaId"]: row for row in rows if row.get("source") == "uploaded" and row.get("schemaId")}
    if not uploaded:
        return

    bucket = _target_schemas_bucket()
    s3_client = _label_reader_client()

    try:
        annotated = _annotated_schema_ids(s3_client, bucket)
    except _SOURCE_ERRORS as e:
        # Every row still ships, without a label — R1.10 degrades a label and
        # never a schema. One entry, because when the listing itself failed no
        # label is resolvable and there is no per-schema shortfall to enumerate:
        # this is the `7c9378d` shape (`LGR-7c9378d-1`, **template / ratified**),
        # correct code in front of a missing permission, and it has to be
        # *reported* rather than absorbed.
        _record_source_failure(failures, "uploaded", f"display labels in {bucket}", e)
        return

    pending = sorted(annotated & set(uploaded))
    if not pending:
        return

    deadline = _label_pass_deadline(started_at if started_at is not None else time.monotonic())

    # schema id -> why its label did not resolve. One aggregated entry at the end
    # rather than one per schema; see `_record_label_shortfall`.
    unresolved: dict[str, str] = {}

    # NOT a context manager, and this is the single most likely thing for a later
    # reader to "clean up". `Executor.__exit__` calls `shutdown(wait=True)`,
    # which blocks until every submitted read finishes — so a deadline followed
    # by a `with`-block exit would return *after* the very overrun the deadline
    # exists to prevent, making the deadline decorative. `try` / `finally` with
    # `shutdown(wait=False, cancel_futures=True)` is what actually abandons the
    # outstanding reads. `cancel_futures` needs Python 3.9+; the runtime is
    # PYTHON_3_12 (`compute-stack.ts:143`).
    executor = ThreadPoolExecutor(max_workers=_LABEL_READ_WORKERS)
    try:
        # Submitted in sorted id order so that which labels resolve first is
        # deterministic for a given world, which is the only determinism
        # available once a deadline can bind (design §5 N1).
        futures: dict[Future[str | None], str] = {
            executor.submit(_read_display_label, s3_client, bucket, schema_id): schema_id for schema_id in pending
        }
        # Consumed rather than `future.done()`-tested on timeout: a read can
        # complete in the instant between the deadline firing and the check, and
        # a label nothing consumed did not reach the response either way.
        outstanding = dict(futures)

        try:
            for future in as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
                schema_id = outstanding.pop(future)
                try:
                    label = future.result()
                except _DOCUMENT_ERRORS as e:
                    # Narrow, for `_SOURCE_ERRORS`' reasons plus `ValueError` for
                    # a sidecar that is not valid JSON. A classified StepFailure /
                    # TransientStepFailure is NOT caught here: it propagates out
                    # of `future.result()` to `get_schemas`' outer clause and
                    # becomes a 500 (`LGR-78ce0b7-13`, `LGR-b2f79ef-13`, both
                    # **template / ratified**) — it is not "a source is
                    # unreadable" and must not be reported as one.
                    if _absent_sidecar(e):
                        continue
                    logger.warning(f"Could not read the Display_Label of schema {schema_id}: {e}")
                    unresolved[schema_id] = _label_failure_reason(e)
                    continue
                if label is not None:
                    uploaded[schema_id]["displayLabel"] = label
        except TimeoutError:
            # `as_completed`'s own timeout. Spelled as the builtin because
            # `concurrent.futures.TimeoutError` has been an alias of it since
            # Python 3.11 and the runtime is 3.12 — one spelling, correct on
            # both. Every row is already in the response; what is lost is a
            # label, and R1.10 is what says so out loud.
            for abandoned in outstanding.values():
                unresolved.setdefault(abandoned, "abandoned at the Display_Label deadline")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if unresolved:
        _record_label_shortfall(failures, bucket, unresolved, len(pending))


def _as_schema_document(raw: Any, what: str) -> Mapping[str, Any]:
    """Assert that a parsed JSON document is an object, raising ``ValueError`` if not.

    **This exists so task 4.5's narrowing is real rather than nominal.** The two
    field extractors read documents a *user* uploaded and a *service* wrote, so
    "this JSON is not a schema" is a reachable, ordinary outcome — not a
    programming error. Without this guard it arrives as a ``TypeError``
    (``"fields" in 5``) or an ``AttributeError`` (``"email".get("name")``), which
    would force `_resolve_row_fields`' clause to catch those two classes as well —
    and a clause that catches ``AttributeError`` hides every genuine typo inside
    the code it wraps. Converting the case to a classified ``ValueError`` at the
    point it is detected is what lets the clause above stay narrow.

    The message names the document, so the ``warning`` `_resolve_row_fields` logs
    identifies which object is unreadable rather than only which schema asked.
    """
    if not isinstance(raw, Mapping):
        raise ValueError(f"{what} is a {type(raw).__name__}, not a JSON object")
    return raw


def _object_entries(raw: Any, what: str) -> list[Mapping[str, Any]]:
    """The JSON-object entries of a list inside a schema document.

    Same purpose as `_as_schema_document`: a value that is not a list at all is a
    classified ``ValueError``, and an entry that is not an object is **skipped
    with a ``warning`` naming its position** rather than raising, so one malformed
    entry costs its own field instead of the whole schema's field list. That is
    the tolerance the per-row read on the listing had — a body it could not read
    was simply dropped — so keeping it here means this task does not make the
    detail endpoint *less* tolerant than the code DD4 moved.
    """
    if not isinstance(raw, list):
        raise ValueError(f"{what} is a {type(raw).__name__}, not a list")
    entries: list[Mapping[str, Any]] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, Mapping):
            entries.append(entry)
        else:
            logger.warning(f"Skipping entry {index} of {what}: a {type(entry).__name__}, not a JSON object")
    return entries


def _uploaded_schema_fields(object_key: str) -> list[dict[str, Any]]:
    """The field list of one uploaded schema body — DD4's moved branch.

    This is `get_schemas`' former per-row `s3:GetObject` (`:88`) and both of the
    body shapes it handled: the custom ``{"fields": [...]}`` form and the JSON
    Schema ``{"properties": {...}}`` form. Nothing about the extraction changed;
    only **when** it runs. It used to run once per uploaded schema on every
    listing request; it now runs once, for the one schema
    ``GET /api/schemas/{id}`` was asked about.

    `object_key` is the key the enumeration read, carried on the row as
    `_object_key` rather than re-derived from the id — one definition of a
    schema's address (`LGR-cad8032-1`).
    """
    region = os.environ.get("AWS_REGION", "us-west-2")
    s3_client = boto3.client("s3", region_name=region)
    account_id = get_aws_account_id()
    bucket_name = os.environ.get("TARGET_SCHEMAS_BUCKET", f"adi-target-schemas-{account_id}-{region}")

    schema_content = _as_schema_document(
        json.loads(s3_client.get_object(Bucket=bucket_name, Key=object_key)["Body"].read()),
        f"uploaded schema body {object_key}",
    )

    fields: list[dict[str, Any]] = []
    if "fields" in schema_content:
        # Custom format with fields array
        for field_def in _object_entries(schema_content["fields"], f"`fields` of {object_key}"):
            fields.append(
                {
                    "name": field_def.get("name", ""),
                    "type": field_def.get("type", "string"),
                    "nullable": not field_def.get("required", False),
                    # R1.5/R1.6 — task 4.5's producer-side split. The body's own
                    # `description` is the schema's own description and belongs
                    # under `description`; it fed `context` before, which is why
                    # the Description column was blank for every field.
                    "description": field_def.get("description", ""),
                    # R2.3 — `context` is the Context_Sidecar's annotation and
                    # nothing else. `_schema_detail` is what merges the sidecar
                    # in, keyed by field name; empty here means this extractor
                    # makes no claim about a human annotation, which is the whole
                    # point of the two keys having two authorities.
                    "context": "",
                }
            )
    elif "properties" in schema_content:
        # JSON Schema format
        properties = _as_schema_document(schema_content["properties"], f"properties of {object_key}")
        required = schema_content.get("required") or []
        if not isinstance(required, list | tuple | set):
            logger.warning(f"Ignoring `required` of {object_key}: a {type(required).__name__}, not a list")
            required = []
        for field_name, field_def in properties.items():
            if not isinstance(field_def, Mapping):
                logger.warning(f"Skipping property {field_name!r} of {object_key}: not a JSON object")
                continue
            fields.append(
                {
                    "name": field_name,
                    "type": field_def.get("type", "string"),
                    "nullable": field_name not in required,
                    # R1.5/R1.6 — as above, the JSON Schema shape of the same
                    # producer-side split.
                    "description": field_def.get("description", ""),
                    "context": "",
                }
            )
    return fields


def _s3_tables_schema_fields(bucket_arn: str, namespace: str, table_name: str) -> list[dict[str, Any]]:
    """The field list of one S3 Tables table's current Iceberg schema — DD4's moved branch.

    This is `get_schemas`' former per-row `s3tables:GetTable` (`:159-161`) plus
    the `s3:GetObject` on the Iceberg metadata document (`:173-175`) — **two**
    calls per row, which is why a paginated listing that kept resolving fields
    would have timed out rather than merely got slower. The extraction is
    unchanged; it now runs for one table instead of every table.

    `LGR-2eaf8a3-1` (**backfilled**) fences the Iceberg read to the S3 Tables
    Iceberg REST ``loadTable`` path with SigV4. `requirements.md` keeps that "IN
    as a constraint, OUT as a fix", so this task moves the existing read rather
    than replacing it.
    """
    region = os.environ.get("AWS_REGION", "us-west-2")
    s3tables_client = boto3.client("s3tables", region_name=region)

    table_details = s3tables_client.get_table(
        tableBucketARN=bucket_arn, namespace=namespace or "default", name=table_name
    )
    metadata_location = table_details.get("metadataLocation", "")
    if not metadata_location or not metadata_location.startswith("s3://"):
        # R7.1's honest absence, read from the other side: a table provisioned
        # and never written has no metadata pointer, so it has no *established*
        # field list. Empty is the right answer here and it is not a failure.
        logger.info(f"No Iceberg metadata location for table {table_name}; it declares no fields yet")
        return []

    # Parse S3 URI
    s3_parts = metadata_location.replace("s3://", "").split("/", 1)
    metadata_bucket = s3_parts[0]
    metadata_key = s3_parts[1] if len(s3_parts) > 1 else ""

    # Read Iceberg metadata file
    s3_client = boto3.client("s3", region_name=region)
    metadata_content = _as_schema_document(
        json.loads(s3_client.get_object(Bucket=metadata_bucket, Key=metadata_key)["Body"].read()),
        f"Iceberg metadata {metadata_location}",
    )

    # Extract schema from Iceberg metadata
    current_schema = metadata_content.get("current-schema-id")
    fields: list[dict[str, Any]] = []
    for schema_def in _object_entries(
        metadata_content.get("schemas") or [], f"`schemas` of Iceberg metadata {metadata_location}"
    ):
        if schema_def.get("schema-id") == current_schema:
            for field_def in _object_entries(
                schema_def.get("fields") or [], f"`fields` of Iceberg schema {current_schema} of {table_name}"
            ):
                fields.append(
                    {
                        "name": field_def.get("name", ""),
                        "type": field_def.get("type", "string"),
                        "nullable": not field_def.get("required", False),
                        # R1.5/R1.6 — task 4.5's producer-side split. An Iceberg
                        # field's `doc` is the table's own description of that
                        # column, so it belongs under `description`. It fed
                        # `context` before.
                        "description": field_def.get("doc", ""),
                        # R2.3 — an S3 Tables table is not an Uploaded_Schema and
                        # can carry no Context_Sidecar, so it has no context at
                        # all rather than an unstated one.
                        "context": "",
                    }
                )
            break
    return fields


def _resolve_row_fields(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Resolve one row's field list, reading only where DD4 moved a read.

    Three sources, two of which now cost a read here rather than one per row on
    the listing:

    - **Glue** — no read. Its columns came free in the `get_tables` page and are
      already on the row, which is why it is also the one source that carries a
      real `fieldCount`.
    - **uploaded** — one `s3:GetObject` on the body.
    - **s3_table_bucket** — one `s3tables:GetTable` plus one `s3:GetObject` on
      the Iceberg metadata.

    A read that fails degrades to an empty list plus a `warning` naming the
    schema, matching the tolerance the per-row reads had on the listing
    (`:88`'s and `:173`'s own `except`). That tolerance is kept deliberately: a
    body the old listing silently skipped must not start failing a request.

    **Task 4.5 narrowed this clause** from ``except Exception`` to
    ``_DOCUMENT_ERRORS``, the two things that can go wrong here: an AWS call
    (``_SOURCE_ERRORS``) and a document that is not readable as a schema
    (``ValueError``). The second is only a
    ``ValueError`` because `_as_schema_document` and `_object_entries` convert it
    into one at the point of detection — without them a non-object body arrives as
    a ``TypeError`` or an ``AttributeError``, and a clause catching those would
    hide every typo inside the two extractors it wraps.

    **Deliberately not narrowed to nothing.** This is the one place in this task
    where a failure is still swallowed rather than stated, because saying it in the
    response means adding a key to DD9's bare schema object — and DD9 fixes that
    shape against the frontend ``Schema`` interface (R8.7). So the honest note is
    that `get_schema` can still return an empty ``fields`` for a schema whose body
    could not be read, and R1.4's empty-state message on that surface cannot yet
    tell that from a schema that declares none. The listing-level distinction —
    which is the one R1.4, R5.3 and design §5 risk 10 are about — **is** stated,
    in ``sourcesFailed``.
    """
    source = row.get("source", "")
    schema_id = row.get("schemaId", "")

    if source == "glue_catalog":
        return list(row.get("fields", []))

    try:
        if source == "uploaded":
            return _uploaded_schema_fields(row.get("_object_key", ""))
        if source == "s3_table_bucket":
            return _s3_tables_schema_fields(
                row.get("_bucket_arn", ""), row.get("_namespace", ""), row.get("_table_name", "")
            )
    except _DOCUMENT_ERRORS as e:
        logger.warning(f"Could not resolve fields for schema {schema_id}: {e}")
        return []

    return list(row.get("fields", []))


def _schema_detail(row: dict[str, Any]) -> dict[str, Any]:
    """Build DD9's bare schema object from one Schema_Listing row.

    Two authorities, two keys, per R1.6 and R2.3:

    - ``description`` is the **schema's own** declared description — Glue
      ``Columns[].Comment``, an uploaded body's ``fields[].description`` /
      ``properties[].description``, or an Iceberg field's ``doc``.
    - ``context`` is the **Context_Sidecar's** annotation and nothing else.

    **Reversal — task 4.5 retired the interim fallback that used to live here.**
    Task 4.2 landed this function while every producer still fed each source's own
    description into ``context`` (``schemas.py:62``, ``:109``, ``:119``, ``:189``),
    so it read ``description`` and fell back to ``context`` when the key was
    absent. Task 4.5 moved the split onto the three producers, so ``description``
    is now always present and the fallback is not merely unnecessary — keeping it
    would make this the **second** definition of which key a description lives
    under, and a producer that regressed to feeding ``context`` would be silently
    corrected here rather than caught. That is the tolerant-consumer shape
    `LGR-28b4d75-5` (**template / ratified**) is about, and it is the same reason
    DD9 deletes ``fetchSchemaDetail``'s ``[0]`` unwrap instead of making it
    tolerant.

    ``displayLabel`` is **absent, not null**, when no label is stored: R4.8 says
    the schema id is what to display in that case, and a client that falls back
    on a missing key renders the id, while one handed ``null`` renders nothing.

    **R5.2/R5.3 — a field's ``nullable`` follows that same rule.** It used to be
    ``field.get("nullable", True)``, which answered *"the source declared it
    optional"* for a producer that had said nothing — and one producer says
    nothing by construction, the Glue branch (`:496`), because
    ``StorageDescriptor.Columns[]`` carries no nullability at all. A stated value
    still reaches the client unchanged, ``False`` as well as ``True``; an unstated
    one now emits **no key**, which is this file's established spelling for "not
    established" (`LGR-44d601e-1`, and `displayLabel` below for the same reason).
    Do NOT reinstate the default, and do NOT emit ``nullable: None`` instead — a
    ``null`` would be a third spelling of two facts.

    This says nothing about a schema whose ``fields`` list is empty. R5.7 and
    `_resolve_row_fields`' docstring above still hold: `get_schema` cannot tell an
    unreadable body from a schema that declares none, and per-field omission does
    not change that.
    """
    schema_id = row.get("schemaId", "")
    sidecar = _read_context_sidecar(schema_id) if row.get("source") == "uploaded" else {}
    field_contexts = sidecar.get("field_contexts") or {}

    fields = []
    for field in row.get("fields", []):
        field_name = field.get("name", "")
        detail_field: dict[str, Any] = {
            "name": field_name,
            "type": field.get("type", "string"),
            # The producer's key, read directly — see the reversal note above.
            "description": field.get("description", ""),
            # The sidecar's, and only the sidecar's (R2.3).
            "context": field_contexts.get(field_name, ""),
        }

        # R5.2/R5.3 — set only when the producer stated one, on the same gate
        # `displayLabel` uses below. The three producers that state it
        # (`:1376`, `:1405`, `:1469`) derive it from the body's `required`, so
        # their value passes through; the Glue branch states nothing and gets
        # nothing.
        nullable = field.get("nullable")
        if nullable is not None:
            detail_field["nullable"] = nullable

        fields.append(detail_field)

    detail: dict[str, Any] = {
        "schemaId": schema_id,
        "name": row.get("name", schema_id),
        "source": row.get("source", ""),
        "targetType": row.get("targetType", ""),
        "lastModified": row.get("lastModified", ""),
        "fields": fields,
    }

    # p36 task 2.1 — through the shared gate rather than a second inline read of
    # `display_name`. Behaviour is identical (the gate was, and stays, truthy);
    # what changes is that the listing now reads the *same* function, which is
    # what makes R1.3 true by construction (`LGR-cad8032-1`).
    display_label = _display_label_from_sidecar(sidecar)
    if display_label is not None:
        detail["displayLabel"] = display_label

    return detail


def get_schema(schema_id: str) -> dict[str, Any]:
    """Get one schema by id — ``GET /api/schemas/{schema_id}`` (R1.1, R1.2).

    Returns DD9's **bare schema object**, not ``{"schemas": [one]}``. The
    collection envelope is what the pre-p35 router returned for a per-id request
    (``router.py:329-332``), and it is why the detail modal always described
    element ``[0]`` of the listing (``use-schemas.ts:38-45``) — the row the user
    clicked was never in the response's shape at all.

    ``404`` names the requested id when no source can produce it (R1.2). A
    listing that could not be built is **not** a ``404``: "no schema has this id"
    and "the sources could not be read" are different claims, and the second one
    reports the listing's own failure instead of asserting the first.

    **Task 4.5 — the ``404`` is qualified when a source could not be enumerated.**
    Its body then carries ``sourcesFailed``, the same entries ``get_schemas``
    emits. The status stays ``404`` because R1.2 pins it and because the id
    genuinely is not among the schemas that could be produced; what changes is
    that the response no longer implies every source was checked. The ``200``
    deliberately carries **no** such key: DD9 fixes that shape against the
    frontend ``Schema`` interface (R8.7), and a schema that was found is complete
    regardless of which other source was unreadable.

    **DD4 — this is where the field list is resolved now.** The listing carries
    `fieldCount` and, for Glue, its free column list; the two per-row reads it
    used to make (the uploaded body, and `s3tables:GetTable` + the Iceberg
    metadata document) happen here, for this one schema. That is the whole point
    of the move: the cost is paid once when a user opens a row, not `N` times on
    a request nobody opened.

    It enumerates through `_mirror_excluded_rows` rather than through
    `get_schemas`, because the internal identifiers the field resolution needs —
    `_object_key`, `_bucket_arn`, `_namespace`, `_table_name` — are stripped on
    the way into a response, and neither address is recoverable from a
    `schemaId` (see `_INTERNAL_ROW_KEYS`). The mirror exclusion still runs, so a
    `glue_catalog` mirror an S3 Tables table accounts for is not addressable here
    either — the per-id surface and the listing agree on which schemas exist.
    """
    try:
        try:
            enumeration = _collect_schema_rows()
        except Exception as e:
            # The listing could not be built. Reported as the listing's own
            # failure, not as a 404 — "no schema has this id" is a claim this
            # code cannot support when it never finished looking (R1.4).
            logger.error(f"Error fetching schemas while resolving {schema_id}: {e}")
            return create_response(500, {"error": "Failed to fetch schemas", "details": str(e)})

        rows = _mirror_excluded_rows(enumeration.rows)

        row = next((r for r in rows if r.get("schemaId") == schema_id), None)
        if row is None:
            # Task 4.5 — the `404` is **qualified** when a source could not be
            # enumerated. "No schema has this id" is a claim about every source;
            # if one of them was unreadable, this code did not look there, and
            # R1.4 is precisely the rule that it must not assert what it did not
            # check. The status stays `404` — the requested id genuinely is not
            # among the schemas that could be produced, and R1.2 pins that
            # status — but the body now says which sources were skipped, so the
            # answer is legible instead of falsely complete.
            not_found: dict[str, Any] = {"error": f"Schema not found: {schema_id}"}
            failed = [failure.as_response_entry() for failure in enumeration.failures]
            if failed:
                not_found["sourcesFailed"] = failed
            return create_response(404, not_found)

        # A copy, so the resolved list is never written back onto a row another
        # caller might hold, and so `_schema_detail` sees no internal keys it
        # does not read.
        resolved = {key: value for key, value in row.items() if key not in _INTERNAL_ROW_KEYS}
        resolved["fields"] = _resolve_row_fields(row)

        return create_response(200, _schema_detail(resolved))

    except Exception as e:
        logger.error(f"Error fetching schema {schema_id}: {e}")
        return create_response(500, {"error": "Failed to fetch schema", "details": str(e)})


# ---------------------------------------------------------------------------
# R4.5 / R4.6 / R4.7 — one generated suffix, and an id that IS the basename
# ---------------------------------------------------------------------------
# The defect R4.5 names: the wizard sends `uploaded_schema_${Date.now()}`
# (`CreatePipelinePage.tsx:328`) and this handler unconditionally appended its own
# `%Y%m%d_%H%M%S`, producing `uploaded_schema_1785844048926_20260804_114729` —
# two independent timestamps, neither aware of the other, on every inline upload
# the wizard makes. That id is a live record, which is why the fixture carrying
# it stays as the **legacy** shape in
# `tests/unit/lambdas/dashboard_api/test_create_pipeline_target_schema_location.py`
# rather than being replaced.
#
# **What "exactly one" means here, stated because the requirement's wording
# admits two readings.** R4.5 says the Schema_Upload "SHALL append exactly one
# uniqueness suffix, so that a stored name carries no more than one generated
# component". The clause after the comma is the goal, and it is a property of the
# *stored name*, not of this function's arithmetic — so a submitted name that
# already carries a generated component must not gain a second one. The rule is
# therefore:
#
#   1. Strip every trailing generated component off the submitted name.
#   2. If the client had supplied exactly one, keep **its** value; if it had
#      supplied several (the pre-fix double-timestamp shape being handed back),
#      keep the **leftmost**, which is the one the client itself chose. If it
#      supplied none, generate one.
#   3. Compose `{base}_{suffix}` and probe. While the key is taken, replace the
#      suffix — never append a second one.
#
# Keeping the client's component rather than always minting a fresh one is what
# makes the id the wizard gets back **equal to the name it uploaded under**, which
# is the contract P29 R11.1 established when it made the wizard compute that name
# exactly once (`CreatePipelinePage.tsx:321-329`, pinned at
# `CreatePipelinePage.schema-upload.test.tsx:126-127`): the wizard's fallback id,
# used when a response omits `schemaId`, then names the object that was actually
# written instead of a different one. The pre-p35 handler broke that agreement on
# every upload and the fallback only ever worked by never being reached.
#
# It also normalises the pre-fix shape to its own head:
# `uploaded_schema_1785844048926_20260804_114729` re-submitted stores as
# `uploaded_schema_1785844048926` — the single-suffix sibling this spec's row-4
# amendment adds.

#: Anything a stored id may not carry, because the id **is** an address.
#:
#: R4.6 and `LGR-cad8032-1` (**template / ratified**) make the returned id
#: byte-identical to the basename of `shared/{id}.json`, so a name containing `/`
#: would write outside `shared/` **and** break that identity — the returned id and
#: the key's basename would disagree, which is the exact two-definitions-of-one-
#: address state that record exists to prevent.
#:
#: `.` is excluded deliberately, and not for safety. `_collect_schema_rows`
#: derives an uploaded schema's id back out of its key by stripping exactly
#: `shared/` and `.json` (`schemas.py:568`), so a name containing the substring
#: `.json` would round-trip to a *different* id than the one this handler
#: returned. Dropping `.` makes the derivation exactly invertible for every name
#: this writer can produce, which is what R4.6 asks for.
_UPLOAD_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

#: A cap on the base name. S3 keys are bounded at 1024 bytes and the id also has
#: to fit `schemas/uploaded/{id}-metadata.json`; 200 leaves room for both plus the
#: suffix, and no human-chosen filename reaches it. A user who wants a long name
#: has the Display_Label, which is stored verbatim and never addresses anything.
_UPLOAD_NAME_MAX_LENGTH = 200

#: One **generated** component, as either of the two shapes this system produces:
#:
#: - `\d{8}_\d{6}` — the `%Y%m%d_%H%M%S` stamp this handler used to append.
#: - `\d{9,}` — an epoch-milliseconds run, which is what `Date.now()` gives the
#:   wizard (13 digits today). The floor is 9 rather than 1 so that a *human*
#:   name ending in a short number — `orders_2024`, `events_v2_1` — is not
#:   mistaken for a machine stamp and left without a uniqueness component.
_GENERATED_SUFFIX = re.compile(r"_(?:\d{8}_\d{6}|\d{9,})$")

#: How many times a taken key is answered with a fresh suffix before giving up.
#: Each attempt widens the random tail by one digit, so the collision probability
#: falls off a cliff; exhausting ten means something other than a collision is
#: wrong, and a `500` naming it beats a silent second suffix.
_UNIQUENESS_ATTEMPTS = 10

#: The request keys carrying an optional Display_Label, on both shapes
#: `upload_schema` handles (R4.1, R8.5). `displayLabel` is the camelCase key the
#: sibling `PUT /api/schemas/{id}/display-label` already accepts
#: (`set_display_label`), so the upload and the rename spell it the same way;
#: `display_label` is accepted beside it because a `multipart/form-data` field is
#: as likely to be written snake_case by hand.
_DISPLAY_LABEL_KEYS = ("displayLabel", "display_label")


class _MultipartUpload(NamedTuple):
    """What a `multipart/form-data` upload carries: a name, a body, and a label.

    A NamedTuple rather than a widened tuple so the label is retrievable by name
    at the one call site and an unlabelled upload is `""` rather than a positional
    surprise.
    """

    schema_name: str
    content: str
    display_label: str


def _sanitised_schema_name(submitted: str) -> str:
    """The submitted name reduced to something `shared/{id}.json` can address.

    Every run of characters outside `[A-Za-z0-9_-]` collapses to a single `_`, and
    leading/trailing `_`/`-` are dropped so the composed id has no dangling
    separator. Returns `""` when nothing usable is left, which the caller answers
    `400`.

    **This is lossy on purpose and the loss is bounded.** Two submitted names can
    sanitise to one base — `a/b` and `a_b` both give `a_b` — which is why
    uniqueness is enforced by probing the key rather than assumed from the name.
    What it buys is R4.6: the id this handler returns is the basename of the key it
    wrote, for every input, with no exception for a name a caller composed out of
    a path or a unicode filename.
    """
    safe = _UPLOAD_NAME_UNSAFE.sub("_", submitted).strip("_-")
    return safe[:_UPLOAD_NAME_MAX_LENGTH].strip("_-")


def _split_generated_suffixes(name: str) -> tuple[str, list[str]]:
    """`(base, suffixes)` with every trailing generated component removed.

    `suffixes` is left-to-right, so `suffixes[0]` is the outermost — the one a
    client appended first. `("uploaded_schema_1785844048926_20260804_114729")`
    gives `("uploaded_schema", ["1785844048926", "20260804_114729"])`.
    """
    base = name
    suffixes: list[str] = []
    while (match := _GENERATED_SUFFIX.search(base)) is not None:
        suffixes.insert(0, match.group(0).removeprefix("_"))
        base = base[: match.start()]
    return base, suffixes


def _generated_suffix(attempt: int = 0) -> str:
    """One generated component: epoch milliseconds, widened on a collision.

    Milliseconds rather than `%Y%m%d_%H%M%S` for one reason, and it is the reason
    R4.7 needs: the second-resolution stamp collides for two uploads in the same
    second, and the pre-p35 handler had no collision check at all — so a second
    upload in the same second **overwrote** the first schema's object silently.
    Milliseconds narrow that window by a thousand; the `head_object` probe in
    `_unique_schema_id` is what closes it.

    On a retry a random tail is appended **inside the same digit run**, so the id
    still carries exactly one suffix — `_1785844048926` becomes `_178584404892607`,
    one generated component, not two. Widening by a digit per attempt keeps the
    first retry cheap and makes repeated collision arbitrarily unlikely.
    """
    suffix = str(int(time.time() * 1000))
    if attempt:
        width = attempt + 1
        suffix += f"{secrets.randbelow(10**width):0{width}d}"
    return suffix


def _object_exists(s3_client: Any, bucket: str, key: str) -> bool:
    """Whether an object is already at `key`.

    `head_object`, and a genuine not-found is the only outcome read as "free" —
    every other `ClientError` propagates, so an `AccessDenied` answers `500`
    naming itself rather than being read as an empty slot and overwriting whatever
    is there. Same classification, and the same reason, as
    `_apply_to_context_sidecar`'s body check.
    """
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise
    return True


def _unique_schema_id(s3_client: Any, bucket: str, submitted_name: str) -> str:
    """The id to store `submitted_name` under: one suffix, and no key collision.

    R4.5, R4.6, R4.7. See the block comment above for the three-step rule and why
    a client-supplied component is kept rather than replaced.

    Raises:
        ValueError: nothing addressable survives sanitisation.
        RuntimeError: `_UNIQUENESS_ATTEMPTS` distinct suffixes were all taken.
        ClientError / BotoCoreError: the existence probe itself failed.
    """
    base = _sanitised_schema_name(submitted_name)
    if not base:
        raise ValueError(
            f"Schema name {submitted_name!r} contains no characters a schema id can be composed "
            f"from (letters, digits, '_' and '-')"
        )

    base, supplied = _split_generated_suffixes(base)
    if not base:
        # The whole submitted name was a generated stamp — `1785844048926.json`.
        # There is no base to carry it, so the stamp becomes the base and this
        # upload gets a suffix of its own.
        base, supplied = "_".join(supplied), []

    suffix = supplied[0] if supplied else _generated_suffix()

    for attempt in range(_UNIQUENESS_ATTEMPTS):
        schema_id = f"{base}_{suffix}"
        if not _object_exists(s3_client, bucket, _schema_body_key(schema_id)):
            return schema_id
        # Taken. The suffix is REPLACED, never extended by a second component:
        # R4.5's whole point is that a stored name carries one generated
        # component, and an upload that had to retry is not a reason to carry two.
        logger.info(f"R4.7: schema id {schema_id} is taken; regenerating its uniqueness suffix")
        suffix = _generated_suffix(attempt + 1)

    raise RuntimeError(
        f"could not find a free schema id for base {base!r} in {_UNIQUENESS_ATTEMPTS} attempts — "
        f"refusing to overwrite an existing schema"
    )


def _multipart_field_name(headers: str) -> str:
    """The form field a multipart part belongs to, or `""`.

    The delimiter before `name=` is required so this cannot match the `name=`
    inside `filename=`, which is what a bare `name="?...` search does whenever a
    client emits `filename` first.
    """
    match = re.search(r'(?:^|[;\s])name="?([^";\r\n]+)"?', headers)
    return match.group(1) if match else ""


def _multipart_part_body(raw: str) -> str:
    """One part's body with the single line break the boundary needs removed.

    Exactly one trailing `\\r\\n` (or `\\n`), because that break belongs to the
    delimiter rather than to the value. Nothing else is stripped: R4.7 stores a
    Display_Label **verbatim**, so `"  padded label  "` keeps its padding and a
    label ending in `-` keeps its dash.
    """
    if raw.endswith("\r\n"):
        return raw[:-2]
    if raw.endswith("\n"):
        return raw[:-1]
    return raw


def _parse_multipart_body(body: str, content_type: str) -> _MultipartUpload:
    """The schema name, the file content and any Display_Label from a multipart body.

    R8.5 requires the Display_Label to be accepted on **both** shapes
    `upload_schema` handles, so this path reads a `displayLabel` (or
    `display_label`) form field beside the `file` part rather than the JSON path
    being the only one that can carry a label.

    The file part's own treatment is unchanged from the pre-p35 form —
    `.rstrip("\\r\\n").rstrip("-").rstrip("\\r\\n")` then `.strip()` — because it
    is a JSON document about to go through `json.loads`, where surrounding
    whitespace is meaningless. The label gets `_multipart_part_body`'s exact
    single-break removal instead, because there its whitespace is the value.

    Raises:
        ValueError: no boundary in the content type, or no `file` part.
    """
    boundary_match = re.search(r"boundary=(.+?)(?:;|$)", content_type)
    if not boundary_match:
        raise ValueError("Missing multipart boundary")

    boundary = boundary_match.group(1).strip()

    schema_name = ""
    file_content: str | None = None
    display_label = ""

    for part in body.split(f"--{boundary}"):
        header_body = part.split("\r\n\r\n", 1)
        if len(header_body) < 2:
            header_body = part.split("\n\n", 1)
        if len(header_body) < 2:
            continue

        headers_str, raw_body = header_body
        field_name = _multipart_field_name(headers_str)

        if field_name == "file":
            # Strip trailing boundary marker
            trimmed = raw_body.rstrip("\r\n").rstrip("-").rstrip("\r\n")
            fname_match = re.search(r'filename="?([^";\r\n]+)"?', headers_str)
            filename = fname_match.group(1) if fname_match else "uploaded_schema"
            # Remove extension for the schema name
            schema_name = filename.rsplit(".", 1)[0] if "." in filename else filename
            file_content = trimmed.strip()
        elif field_name in _DISPLAY_LABEL_KEYS:
            display_label = _multipart_part_body(raw_body)

    if file_content is None:
        raise ValueError("No file field found in multipart body")

    return _MultipartUpload(schema_name, file_content, display_label)


def _submitted_display_label(data: Mapping[str, Any]) -> str:
    """The optional Display_Label out of a JSON upload body (R4.1, R4.2).

    Absent, `null` and `""` are all "no label" — R4.2 makes "usable with no
    Display_Label at all" a first-class state, and task 8.5 already chose that an
    empty label is the *absence* of one rather than a stored `""`
    (`set_display_label`). A non-string is a `400` rather than being coerced,
    because `str(42)` would store a label the caller never wrote.

    Raises:
        ValueError: a label key is present but is not a string.
    """
    for key in _DISPLAY_LABEL_KEYS:
        if key not in data:
            continue
        value = data[key]
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{key!r} must be a string, got {type(value).__name__}")
        return value
    return ""


def upload_schema(event: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/schemas/upload`` — R4.1, R4.2, R4.5, R4.6, R4.7.

    Accepts both JSON body (``{name, content, displayLabel?}``) and
    multipart/form-data with a ``file`` field containing the JSON schema and an
    optional ``displayLabel`` field.

    **The id and the key are one decision, made once** (R4.6, `LGR-cad8032-1`,
    **template / ratified**): `_unique_schema_id` picks the id and
    `_schema_body_key` composes the key from it, so the returned `schemaId` is the
    basename of the object that was written, by construction rather than by two
    expressions agreeing. `shared/{id}.json` is the one convention that addresses
    both shapes this handler writes, on both the write and the read path — do not
    split them.

    **Order: body first, then the label.** The label lands in the Context_Sidecar
    through `_apply_to_context_sidecar`, which `head_object`s
    `shared/{id}.json` and raises `SchemaBodyAbsent` when nothing is there — so
    the body has to exist before the label write can pass. That ordering also
    gives the right answer when the label write fails: the schema is stored and
    usable (R4.2), so this returns `200` for the upload and reports the label
    separately rather than a `500` that would imply nothing was written.
    """
    import base64

    try:
        logger.info("Uploading new schema")

        content_type = ""
        for k, v in (event.get("headers") or {}).items():
            if k.lower() == "content-type":
                content_type = v
                break

        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")

        # ── Multipart upload ──────────────────────────────────────────
        if "multipart/form-data" in content_type:
            try:
                submitted_name, raw_content, display_label = _parse_multipart_body(body, content_type)
            except ValueError as exc:
                return create_response(400, {"error": str(exc)})

            try:
                schema_data = json.loads(raw_content)
            except json.JSONDecodeError as exc:
                return create_response(400, {"error": f"Invalid JSON in uploaded file: {exc!s}"})

        # ── JSON upload ───────────────────────────────────────────────
        else:
            data = json.loads(body) if isinstance(body, str) else body
            submitted_name = data.get("name", "")
            schema_content = data.get("content", "")

            if not submitted_name:
                return create_response(400, {"error": "Schema name is required"})
            if not schema_content:
                return create_response(400, {"error": "Schema content is required"})

            try:
                display_label = _submitted_display_label(data)
            except ValueError as exc:
                return create_response(400, {"error": str(exc)})

            if isinstance(schema_content, str):
                try:
                    schema_data = json.loads(schema_content)
                except json.JSONDecodeError as exc:
                    return create_response(400, {"error": f"Invalid JSON schema: {exc!s}"})
            else:
                schema_data = schema_content

        # ── Upload to S3 ─────────────────────────────────────────────
        region = os.environ.get("AWS_REGION", "us-west-2")
        s3_client = boto3.client("s3", region_name=region)
        account_id = get_aws_account_id()
        bucket_name = os.environ.get("TARGET_SCHEMAS_BUCKET", f"adi-target-schemas-{account_id}-{region}")

        try:
            schema_id = _unique_schema_id(s3_client, bucket_name, submitted_name)
        except ValueError as exc:
            return create_response(400, {"error": str(exc)})

        s3_key = _schema_body_key(schema_id)
        schema_json = json.dumps(schema_data, indent=2)
        s3_client.put_object(Bucket=bucket_name, Key=s3_key, Body=schema_json, ContentType="application/json")
        logger.info(f"Schema uploaded to target-schemas/shared: s3://{bucket_name}/{s3_key}")

        payload: dict[str, Any] = {
            "message": "Schema uploaded successfully",
            "schemaId": schema_id,
            "location": f"s3://{bucket_name}/{s3_key}",
        }

        # R4.1 / R4.12 — the label, verbatim, in the sidecar beside
        # `field_contexts`. Only when one was supplied: an upload with no label
        # must leave no `display_name` at all (R4.2, R4.8), and writing a sidecar
        # carrying nothing would create an object no listing surfaces.
        if display_label.strip():

            def _set_label(sidecar: dict[str, Any]) -> None:
                # Verbatim — no trimming and no uniqueness transformation (R4.7),
                # so two schemas may carry the same label and padding survives.
                sidecar[_SIDECAR_DISPLAY_NAME] = display_label

            try:
                _apply_to_context_sidecar(schema_id, _set_label)
                payload["displayLabel"] = display_label
            except Exception as exc:
                # The schema **is** stored, so a `500` here would be the wrong
                # claim in the other direction: the caller would retry an upload
                # that succeeded and get a second object. R4.2 says a schema with
                # no Display_Label is fully usable, so this degrades to exactly
                # that state and names the reason — the label is recoverable
                # through `PUT /api/schemas/{id}/display-label`.
                logger.error(f"Schema {schema_id} stored, but its Display_Label could not be written: {exc}")
                payload["displayLabelError"] = str(exc)

        return create_response(200, payload)

    except Exception as e:
        logger.error(f"Error uploading schema: {e}")
        return create_response(500, {"error": "Failed to upload schema", "details": str(e)})


# ---------------------------------------------------------------------------
# D4 / R3.7-R3.10 — the reference check, and what a delete is allowed to claim
# ---------------------------------------------------------------------------
#: The two lookups design §4.2 prescribes, spelled as the response reports them.
#:
#: These strings are the `checked` vocabulary, and `checked` is **not
#: diagnostics**. `LGR-acca05d-4` (**template / ratified**) delegates the
#: Query-versus-Scan choice to the design and requires the response semantics to
#: state which lookup ran, because "no referencing pipelines found" and "no
#: referencing pipelines exist" are the same distinction R1.4 and R5.3 turn on
#: everywhere else in this file. Naming the index and the scan is what discharges
#: that.
_LOOKUP_INDEX = "target_schema_name-index"
_LOOKUP_SCAN = "target_schema_s3_key-scan"

#: Wall-clock budget for the paginated Scan, in seconds.
#:
#: Bounded from **both** sides, which is why it is a named constant rather than a
#: literal at the call site:
#:
#: - **Upper bound: 25s.** That is the dashboard API Lambda's timeout
#:   (`deploy-cdk/lib/compute-stack.ts:629`, `cdk.Duration.seconds(25)`). A scan
#:   that overran it would be killed mid-request, so the caller would get an API
#:   Gateway `502`/`504` rather than an answer — and, decisively, **no `checked`
#:   at all**. Giving up at a page boundary with 15s still in hand is what makes
#:   the `503` a reportable outcome instead of a timeout.
#: - **Lower bound: long enough that a scan which *can* finish does.** Answering
#:   `503` to a request whose check would have completed is a false "could not
#:   check", and R3.1 says an unreferenced schema gets deleted.
#:
#: `LGR-dd80c6d-1` (**backfilled**) records unbounded enumeration in
#: `dashboard_api` as a recognised defect class already paginated for the stats
#: and approvals handlers. This adds a **bounded** scan to a table
#: `pipelines_crud.py:224` and `:1585` already scan on live dashboard paths, the
#: second without pagination at all — so the capability and its IAM exist and
#: this does not introduce the pattern.
_REFERENCE_CHECK_BUDGET_SECONDS = 10.0

#: DynamoDB error codes that mean "the lookup did not finish", as distinct from
#: "the lookup answered".
#:
#: Deliberately narrow. Every code here is **transient**, which is what makes the
#: `503`'s `retryable: true` a true statement rather than an invitation to retry
#: something that will never succeed. Anything else — `AccessDenied`,
#: `ValidationException`, `ResourceNotFoundException` — is left to propagate to
#: `delete_schema`'s outer clause and answer `500`. That is not a loss: a `500`
#: is equally not a claim that nothing references the schema, so no delete
#: happens either way, and reporting a permissions problem as a retryable `503`
#: would send the caller round a loop instead of naming the cause.
_LOOKUP_INTERRUPTED_ERROR_CODES = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "ThrottledException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "RequestThrottled",
        "InternalServerError",
        "ServiceUnavailable",
        "RequestTimeout",
    }
)


class ReferenceCheckIncomplete(Exception):
    """The reference check could not be finished, so its result is unusable.

    Raised by `_find_referencing_pipelines` and **not caught there**. That is the
    whole point of the class: an empty result list means "no pipeline record
    references this schema", and an unfinished scan cannot support that claim, so
    the incompleteness has to travel to the code that decides the status rather
    than being flattened into a value that reads as an answer (design §4.2, the
    fourth row of its outcome table).

    `lookups_completed` carries the lookups that **did** run to the end, and it is
    the only thing the `503`'s `checked` may report. It is a smaller list than the
    lookups that were *attempted*, and that difference is the information.
    """

    def __init__(self, lookups_completed: list[str], cause: str) -> None:
        super().__init__(f"Reference check incomplete after {lookups_completed or 'no completed lookup'}: {cause}")
        self.lookups_completed = list(lookups_completed)
        self.cause = cause


def _schema_body_key(schema_id: str) -> str:
    """The key of an Uploaded_Schema's body object — `shared/{id}.json`.

    `LGR-cad8032-1`'s (**template / ratified**) one address convention: it leads
    `uploaded_schema_candidate_keys` (`shared/target_schema_resolution.py:407`),
    `upload_schema` writes there, and `_collect_schema_rows` derives an id back
    out of it by stripping exactly `shared/` and `.json`. R3.8's predicate — "a
    pipeline's `target_schema_s3_key` names the schema's object" — is equality
    against this value, so having one expression for it is what keeps the delete's
    reader in step with the upload's writer.
    """
    return f"shared/{schema_id}.json"


#: Id prefix → the `source` token of the row that synthesises that shape.
#:
#: `_collect_schema_rows` builds `glue_{database}_{table}` (the `glue_catalog`
#: row) and `s3_table_{bucket}_{namespace}_{table}` (the `s3_table_bucket` row).
#: An Uploaded_Schema's id, by contrast, is the **basename of a real object** and
#: carries no synthesised prefix.
_CATALOG_ID_PREFIXES: tuple[tuple[str, str], ...] = (
    ("glue_", "glue_catalog"),
    ("s3_table_", "s3_table_bucket"),
)


def _catalog_derived_source(schema_id: str) -> str:
    """The catalog source a schema id was synthesised by, or `""` (R3.3).

    R3's scope note is what this implements: `glue_{db}_{table}`
    (`_collect_schema_rows`' Glue row) and `s3_table_{bucket}_{ns}_{table}` (its
    S3 Tables row) are **synthesised identifiers for catalog entries owned by
    `create_infrastructure` and by the S3 Tables service**. They name no
    deletable object, and deleting either through a dashboard list action would
    destroy a pipeline's target — so R3.3 answers `400` and says why.

    **A structural test rather than a lookup, and the bound is stated.** The
    alternative was to enumerate the listing and read the matched row's `source`,
    which is exact. It was not taken: it spends three services' worth of
    enumeration on every delete request to classify a string, and it makes the
    `400` depend on a listing that may itself have failed — so a Glue source that
    could not be read would turn R3.3's `400` into a delete. The prefix is the
    same expression the ids are built from, so it cannot drift from them without
    the build sites changing too.

    **What it costs:** an Uploaded_Schema a user managed to name `glue_x` or
    `s3_table_x` would be refused with `400` rather than deleted. That errs toward
    *not* deleting, which is the direction D4 (`LGR-acca05d-4`) exists to err in,
    and the schema stays fully usable — only this one endpoint declines it.
    """
    for prefix, source in _CATALOG_ID_PREFIXES:
        if schema_id.startswith(prefix):
            return source
    return ""


def _is_interrupted_lookup(error: ClientError) -> bool:
    """Whether a DynamoDB failure means "did not finish" rather than "answered"."""
    return str(error.response.get("Error", {}).get("Code", "")) in _LOOKUP_INTERRUPTED_ERROR_CODES


def _query_all_pages(completed: list[str], **kwargs: Any) -> list[dict[str, Any]]:
    """Every page of one Query on the pipelines table.

    Paginated rather than single-page for the reason `LGR-dd80c6d-1` records: a
    first-page-only read silently under-reports, and under-reporting *here* means
    missing a referencing pipeline, which is the one error D4 forbids. Projection
    on `target_schema_name-index` is `ALL` (`data-stack.ts:80-86`), so each item
    is the whole record and no follow-up `get_item` is needed.

    `completed` is what the raiser reports as having finished — for the Query it
    is empty, because the Query is the first lookup and nothing precedes it.
    """
    items: list[dict[str, Any]] = []
    page_kwargs = dict(kwargs)
    while True:
        try:
            page = pipelines_table.query(**page_kwargs)
        except ClientError as exc:
            if _is_interrupted_lookup(exc):
                raise ReferenceCheckIncomplete(completed, f"query interrupted: {exc}") from exc
            raise
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        page_kwargs["ExclusiveStartKey"] = last_key


def _scan_all_pages(completed: list[str], **kwargs: Any) -> list[dict[str, Any]]:
    """Every page of one filtered Scan of the pipelines table, under a budget.

    Raises `ReferenceCheckIncomplete` rather than returning a partial page. That
    is the difference the whole design turns on: a truncated scan returns a
    *shorter list*, and a shorter list is indistinguishable from "nothing
    references this schema" — so truncating silently converts "could not check"
    into the one claim that authorises the delete.

    **The clock is `time.monotonic`.** It is monotonic (a wall-clock adjustment
    mid-request cannot make the budget appear to run backwards) and it is the
    clock `TestTheSpecificationIsSatisfiable`'s probe in
    `test_schema_delete_reference_check_incomplete_prop.py` and that file's budget
    harness both drive. `time.perf_counter` is deliberately not used: Hypothesis
    measures with it, so a harness cannot advance it without breaking itself, and
    a budget measured on it would be unobservable to Property 5.

    The check is at the **page boundary**, after a page has been consumed and only
    when another page exists. So a scan that fits in one page is never refused for
    time, and a scan that would need a further page after the budget is spent is
    never started.
    """
    started = time.monotonic()
    items: list[dict[str, Any]] = []
    page_kwargs = dict(kwargs)
    pages = 0
    while True:
        try:
            page = pipelines_table.scan(**page_kwargs)
        except ClientError as exc:
            if _is_interrupted_lookup(exc):
                raise ReferenceCheckIncomplete(completed, f"scan interrupted after {pages} page(s): {exc}") from exc
            raise
        pages += 1
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        elapsed = time.monotonic() - started
        if elapsed > _REFERENCE_CHECK_BUDGET_SECONDS:
            raise ReferenceCheckIncomplete(
                completed,
                f"scan budget of {_REFERENCE_CHECK_BUDGET_SECONDS:g}s exhausted after {pages} page(s) "
                f"({elapsed:.1f}s elapsed) with more pages outstanding",
            )
        page_kwargs["ExclusiveStartKey"] = last_key


def _find_referencing_pipelines(schema_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Pipelines that reference this uploaded schema, and which lookups ran.

    Returns `(referencing_pipelines, lookups_completed)`. The second element is
    **not diagnostics** — it is what the response uses to distinguish "checked and
    found nothing" from "could not check", the same distinction Requirements 1.4
    and 5.3 turn on elsewhere. A caller that cannot complete the scan MUST NOT
    delete (R3.10).

    Raises `ReferenceCheckIncomplete` when a lookup cannot be finished inside the
    request's budget. Deliberately **not caught here** and **not downgraded to an
    empty list**: an empty list means "no pipeline references this schema", and an
    unfinished scan cannot support that claim.

    **Query first, Scan only when the Query finds nothing** (design §4.2, DD2).

    **Rejected: Query-only.** Cheap, no scan, no budget, no `503` branch. It
    misses a record whose `target_schema_name` disagrees with its
    `target_schema_s3_key`, and that record is live — `pipelines_crud.py:822-830`
    names `testing-39eddb17`, carrying
    `target_schema_name=schema_1785844048926_20260804_114729` against
    `target_schema_s3_key=shared/uploaded_schema_1785844048926_20260804_114729.json`.
    R3.8's predicate is defined on the **key**, so the Query cannot see it, and the
    failure mode is exactly what D4 exists to prevent: the schema is deleted out
    from under a pipeline whose next run then *raises*
    `target_schema_object_missing` (`shared/target_schema_resolution.py:290-302`,
    `LGR-78ce0b7-14`, **template / ratified**). A `409`-capable endpoint that still
    orphans legacy pipelines is **worse** than one that admits it cannot check,
    because it looks safe.

    **Rejected: Scan-only, skipping the Query.** One code path, complete, no
    two-stage semantics. It pays a full-table read on every delete including the
    overwhelming majority the index answers in one page — and the index hit
    carries information the scan does not: a match on `target_schema_name` is a
    match on the attribute `schema_mapping`'s by-name fallback interpolates into
    candidate keys (R4.11, `schema_mapping/lambda_function.py:296`), so it is the
    one worth reporting first.

    **Rejected: add a `target_schema_s3_key` GSI**, which would make R3.8's
    predicate directly indexable and retire the scan. It is a table change with a
    backfill question for every existing record, a third `ProjectionType.ALL` GSI
    on this table — and it is `LGR-acca05d-4`'s own stated `revisit_when`, so it
    belongs in the spec that revisits the decision, not in the one implementing it.
    There is still no such index (`deploy-cdk/lib/data-stack.ts:80-86` declares
    exactly two GSIs), which is what makes this a real trade rather than a
    formality.

    **Revisit when** the pipelines table gains an index on
    `target_schema_s3_key`, per `LGR-acca05d-4`.
    """
    expected_key = _schema_body_key(schema_id)

    # 1. The index that carries the id. `create_pipeline` sets
    #    `target_schema_name` to the schema id on BOTH branches
    #    (`pipelines_crud.py:804` resolved, `:831` unresolvable, into the record
    #    at `:837`), and D2 keeps it that way (R4.11) — so for every record
    #    written by the current code this Query is complete on its own.
    #
    #    `eq`, not `begins_with`: a `begins_with` would match a record whose
    #    `target_schema_name` is `{id}_v2`, which is a different schema.
    hits = _query_all_pages(
        [],
        IndexName=_LOOKUP_INDEX,
        KeyConditionExpression=Key("target_schema_name").eq(schema_id),
    )
    if hits:
        # The scan is not run. It could only add more referencing pipelines, and
        # one is already enough to refuse — so paying a full-table read here
        # would buy nothing the answer depends on.
        return hits, [_LOOKUP_INDEX]

    # 2. Only now, and only because a delete is about to happen.
    #
    #    `eq`, not `contains`: R3.8's predicate is that the key **names** the
    #    schema's object. `contains` would refuse on
    #    `shared/uploaded_{id}.json`, which contains the id and names a different
    #    schema's object — and on the Context_Sidecar's
    #    `schemas/uploaded/{id}-metadata.json`, which is keyed on the id and is
    #    not the schema's object at all. A predicate that refuses whenever the id
    #    appears anywhere would make every delete impossible while looking
    #    correct on the referenced case.
    hits = _scan_all_pages(
        [_LOOKUP_INDEX],
        FilterExpression=Attr("target_schema_s3_key").eq(expected_key),
    )
    return hits, [_LOOKUP_INDEX, _LOOKUP_SCAN]


def _referencing_pipeline_entries(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The referencing pipelines, as the `409` names them (R3.7, R3.9).

    Id **and** name, because R3.9 requires the Schemas_Page to present them "so
    that the user can act on them rather than only being told no" — and a user
    acting on this list needs the id to find the pipeline and the name to
    recognise it.

    Only referencing pipelines appear. A refusal that named a pipeline which does
    not reference the schema would send the user to edit the wrong pipeline, which
    is worse than a bare refusal.
    """
    entries = [
        {
            "pipelineId": str(item.get("pipeline_id", "")),
            "pipelineName": str(item.get("pipeline_name") or item.get("pipeline_id", "")),
        }
        for item in items
    ]
    return sorted(entries, key=lambda entry: entry["pipelineId"])


def delete_schema(schema_id: str) -> dict[str, Any]:
    """``DELETE /api/schemas/{schema_id}`` — R3.1, R3.2, R3.3, R3.5, R3.7-R3.10.

    Design §4.2's four outcomes, each carrying `checked` so the response states
    which lookups ran (`LGR-acca05d-4`, **template / ratified**):

    | Outcome | Status | The claim being made |
    |---|---|---|
    | Query found referencing pipelines | `409`, no delete | these pipelines reference it; the scan was not run |
    | Query empty, Scan found some | `409`, no delete | a legacy split record references it — **the case Query-only would have got wrong, by deleting** |
    | Query empty, Scan completed empty | `200` after the delete | **no pipeline record references this schema.** An unqualified claim, which is what makes the delete safe |
    | A lookup could not complete | `503`, **no delete** | nothing. The request is refused because the question was not answered |

    The fourth row is the one carrying DD2's weight. A `200` on an unfinished
    check would be a silent claim this code cannot support — R1.4's failure class,
    and `LGR-78ce0b7-14`'s. A `409` would be worse: it would assert references
    that were never found. `503` says "ask again", is honestly `retryable` because
    every cause classified as incomplete is transient, and satisfies **R3.10 by
    construction** — the check precedes the delete on every path, so a refusal and
    a deletion can never both be observable for one request.

    Two further outcomes, neither of which reaches the reference check:

    - **R3.3** — a `glue_catalog` or `s3_table_bucket` id answers `400` stating
      the schema is a view of a catalog object. See `_catalog_derived_source`.
    - **R3.5** — an Uploaded_Schema whose object is already absent answers
      **success**, so a repeated delete is not reported as a failure. That falls
      out of `DeleteObject` being idempotent rather than out of a pre-check: S3
      answers `204` for a key that is not there, so no `head_object` is needed and
      none is made. It is ordered *after* the reference check on purpose — R3.10
      requires R3.7 to be evaluated before deleting, so a referenced schema whose
      object has already gone is still a `409` and not a quiet success.
    """
    try:
        # R3.3 — before any lookup. A catalog-derived id names no deletable
        # object, so there is nothing a reference check could inform.
        catalog_source = _catalog_derived_source(schema_id)
        if catalog_source:
            logger.info(f"Refusing delete of catalog-derived schema {schema_id} ({catalog_source})")
            return create_response(
                400,
                {
                    "error": (
                        f"Schema {schema_id} is a view of a catalog object ({catalog_source}) and is not "
                        f"deletable through this endpoint. It is owned by the catalog that created it — "
                        f"deleting it here would destroy a pipeline's target."
                    ),
                    "reason": "catalog_derived_schema",
                    "schemaId": schema_id,
                    "source": catalog_source,
                },
            )

        try:
            referencing, checked = _find_referencing_pipelines(schema_id)
        except ReferenceCheckIncomplete as incomplete:
            # NOT downgraded to an empty list. `checked` reports only the lookups
            # that finished, which is what tells the caller this is "could not
            # check" and not "checked and found nothing".
            logger.warning(f"Reference check for schema {schema_id} could not be completed: {incomplete.cause}")
            return create_response(
                503,
                {
                    "error": (
                        f"Could not establish whether any pipeline references schema {schema_id}, so the "
                        f"delete was not attempted. Please try again."
                    ),
                    "reason": "reference_check_incomplete",
                    "retryable": True,
                    "checked": incomplete.lookups_completed,
                    "schemaId": schema_id,
                    "details": incomplete.cause,
                },
            )

        if referencing:
            entries = _referencing_pipeline_entries(referencing)
            logger.info(
                f"Refusing delete of schema {schema_id}: referenced by {len(entries)} pipeline(s) (checked {checked})"
            )
            return create_response(
                409,
                {
                    "error": (
                        f"Schema {schema_id} is referenced by {len(entries)} pipeline(s) and was not deleted. "
                        f"Update or archive them first."
                    ),
                    "reason": "schema_in_use",
                    "schemaId": schema_id,
                    "checked": checked,
                    "referencingPipelines": entries,
                },
            )

        region = os.environ.get("AWS_REGION", "us-west-2")
        s3_client = boto3.client("s3", region_name=region)
        account_id = get_aws_account_id()
        bucket_name = os.environ.get("TARGET_SCHEMAS_BUCKET", f"adi-target-schemas-{account_id}-{region}")
        object_key = _schema_body_key(schema_id)

        # ── The delete ───────────────────────────────────────────────────────
        # DEPENDS ON UNIT A'S FOUNDATION DEPLOY. `s3:DeleteObject` on the
        # target-schemas bucket was added to `infrastructureRole` — the role this
        # Lambda runs as (`compute-stack.ts:628`) — in task 1.3
        # (`foundation-stack.ts:597`), and it was verified `implicitDeny` against
        # the live role before that. **Until the Foundation stack is deployed this
        # single call raises `AccessDenied` and the handler answers `500` on
        # exactly the schemas it has just decided it is willing to delete** — a
        # fixed handler in front of a missing permission, which is
        # `LGR-7c9378d-1`'s (**template / ratified**) recorded failure shape and
        # the reason Unit A gates this task at all. Confirm with
        # `iam:simulate-principal-policy` for `s3:DeleteObject` against
        # `adi-target-schemas-<account>-<region>/schemas/uploaded/x-metadata.json`
        # — against a real resource ARN, never a wildcard, which read as a deny
        # for every resource-scoped grant and made the first run of that check
        # useless.
        #
        # R3.5 lives on this line: `DeleteObject` succeeds for a key that is not
        # there, so an already-deleted schema answers success without a
        # pre-existence read.
        s3_client.delete_object(Bucket=bucket_name, Key=object_key)

        logger.info(f"Deleted schema {schema_id} at s3://{bucket_name}/{object_key} (checked {checked})")
        return create_response(
            200,
            {
                "message": f"Schema {schema_id} deleted",
                "schemaId": schema_id,
                # The unqualified claim, and what it rests on. R3.2 follows from
                # the object being gone: `_collect_schema_rows` enumerates
                # `shared/*.json`, so the listing stops returning it.
                "checked": checked,
                "referencingPipelines": [],
            },
        )

    except Exception as e:
        logger.error(f"Error deleting schema {schema_id}: {e}")
        return create_response(500, {"error": "Failed to delete schema", "details": str(e)})


# ---------------------------------------------------------------------------
# Unit C's write surface — the Context_Sidecar's ONE writer (task 8.5, design C3)
# ---------------------------------------------------------------------------
# `update_field_context` and `set_display_label` answered `501` from task 4.2
# until here. They write into the **same object**,
# `schemas/uploaded/{schema_id}-metadata.json`, through the same
# read-modify-write below — two keys in one sidecar, not two storage
# conventions:
#
#   {"field_contexts": {field_name: text},   # D3, LGR-acca05d-3, R2.3
#    "display_name": "<label>"}              # D2, LGR-acca05d-2, R4.12
#
# `field_contexts` is the shape `handlers/pipelines_crud.py:800` **already
# reads** — the reader existed and only the writer was missing, which is why
# every create-pipeline run logged a `NoSuchKey` structurally rather than as a
# race (`LGR-acca05d-3`). `display_name` joins it rather than getting a home of
# its own because D3 introduces a writer for that object anyway, so R4.12 adds a
# key and not a storage convention, a bucket prefix or an IAM surface — and it is
# keyed on the **frozen** id (R4.10), so the label's own address cannot go stale
# when the label changes.
#
# THREE THINGS NEITHER HANDLER DOES, each with a named reason:
#
# 1. **Neither writes the label into the schema body.** The body's bytes are the
#    schema's identity downstream — `sha256` over the raw bytes at
#    `shared/target_schema_resolution.py:310`, `schema_mapping:305` and
#    `schema_compare:384`, compared as `target_schema_version` to detect drift
#    (`LGR-eee8ac8-4`, **template / ratified**). A pure rename that touched one
#    byte would present as target-schema drift and trigger a remap and an
#    approval for a reason unrelated to the schema.
# 2. **Neither relocates anything.** No `copy_object`, no `delete_object`, and no
#    `put_object` whose key is not the sidecar (`LGR-cad8032-1`,
#    `LGR-78ce0b7-14`, `LGR-acca05d-2`). A pipeline's `target_schema_s3_key` is a
#    claim that an object exists, so moving the body turns every referencing
#    pipeline's target-schema read into a `target_schema_object_missing`
#    `StepFailure`. `tests/unit/lambdas/dashboard_api/test_schema_rename_invariance_prop.py`
#    (Property 3) asserts the **absence** of those calls, not merely the presence
#    of the right one.
# 3. **Neither touches DynamoDB.** A Display_Label never reaches
#    `target_schema_name` or any key derivation (R4.11): the only address either
#    handler composes is `_context_sidecar_key(schema_id)`, from the id. A label
#    in `target_schema_name` would make all six of
#    `uploaded_schema_candidate_keys`' probes miss and raise inside
#    `schema_mapping` six steps later with no other symptom.
#
# **An empty or whitespace-only value CLEARS, it is not refused.** R4 does not
# say which, and Property 3 deliberately does not generate one so as not to
# enforce an answer it was not given. The choice made here is *clear*, for three
# reasons. R4.2 makes "usable with no Display_Label at all" a first-class
# supported state and R4.8 defines what to show then (the schema id) — so
# refusing would make that state unreachable once a label had been set, with no
# way back short of editing S3 by hand. Storing `""` was rejected on
# `LGR-78ce0b7-14`'s rule that a stored name is a claim: an empty `display_name`
# is a label that names nothing, and every reader would then have to know that
# `""` means absent. `_schema_detail`'s `if display_label:` would in fact tolerate
# it — which is the point: a tolerant consumer is indistinguishable from the bug
# (`LGR-28b4d75-5`, **template / ratified**), so absence gets exactly one
# representation, the key not being there. The same rule applies to a field
# context, where `""` and absent are *already* indistinguishable to both readers
# (`_schema_detail` and `pipelines_crud.py:800` both `.get(field_name, "")`).
#
# **Emptiness is tested on the stripped value; the stored value is verbatim.**
# R4.7 stores a label with no uniqueness transformation and no trimming, so
# `"  padded label  "` is stored with its padding — Property 3 pins that case
# explicitly. `.strip()` appears only inside the "is this a clear?" test.

#: The two keys this writer owns, spelled once each.
_SIDECAR_FIELD_CONTEXTS = "field_contexts"
_SIDECAR_DISPLAY_NAME = "display_name"


class SchemaBodyAbsent(Exception):
    """No object backs the schema id, so there is nothing to annotate or rename.

    Raised by `_apply_to_context_sidecar` **before** it writes anything, and
    turned into a `404` naming the id by both handlers.

    The alternative was to write the sidecar for any well-shaped id. It was
    rejected on the rule this whole spec turns on: a `200` would be a claim that
    the schema now carries a label or a context, and for a mistyped id that claim
    is false and leaves an orphan `-metadata.json` behind that no listing will
    ever surface (`_collect_schema_rows` enumerates `shared/*.json`, so a sidecar
    with no body is invisible). Refusing costs one `head_object`.
    """


def _apply_to_context_sidecar(schema_id: str, apply_change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Read-modify-write one schema's Context_Sidecar, preserving every other key.

    Read-modify-write, and the read is load-bearing in both directions: a label
    write must not drop `field_contexts` and a context write must not drop
    `display_name`, because `put_object` **replaces** the object rather than
    merging into it. Property 3 clause 4 asserts exactly that — it seeds
    `{"field_contexts": {"email": "primary contact"}}` and requires it to still be
    there after a sequence of label writes.

    **Why this does not call `_read_context_sidecar`, which reads the same object
    at the same address.** That reader is the *presentation* path's, and it is
    deliberately tolerant: `_DOCUMENT_ERRORS` — including `AccessDenied` — comes
    back as `{}` at `debug`, because R2.6 requires an absent sidecar not to be
    reported as an error on a read. On a **write** path that tolerance is
    destructive: an unreadable existing sidecar returned as `{}` would be
    followed by a `put_object` carrying only the new key, silently deleting the
    `field_contexts` a user had entered. So only a genuine **not-found** is
    treated as "no sidecar yet"; everything else propagates and the handler
    answers `500` naming it. One address (`_context_sidecar_key`), two
    tolerances, and the difference is the point rather than a duplication —
    `LGR-cad8032-1` is about two definitions of an *address*, and there is still
    exactly one of those.

    Raises:
        SchemaBodyAbsent: `shared/{schema_id}.json` does not exist.
        ClientError / BotoCoreError: the sidecar could not be read or written.
        ValueError: the existing sidecar is not a JSON object, or its
            `field_contexts` is not one — refused rather than clobbered.
    """
    # Resolved inline, in the shape `delete_schema` and `upload_schema` already
    # use in this file. Deliberately not lifted into a helper: five call sites
    # above spell it the same way, and a helper only some of them used would be
    # the second definition of one thing, which is the state `LGR-cad8032-1`
    # exists to prevent. Converging all six belongs in a change that converts all
    # six.
    region = os.environ.get("AWS_REGION", "us-west-2")
    s3_client = boto3.client("s3", region_name=region)
    account_id = get_aws_account_id()
    bucket_name = os.environ.get("TARGET_SCHEMAS_BUCKET", f"adi-target-schemas-{account_id}-{region}")

    body_key = _schema_body_key(schema_id)
    sidecar_key = _context_sidecar_key(schema_id)

    # `head_object`, not `get_object`: existence is the whole question and the
    # body can be large. A read is not a forbidden call on this path — only a
    # relocation or a non-sidecar write is (Property 3's `_forbidden_writes`).
    try:
        s3_client.head_object(Bucket=bucket_name, Key=body_key)
    except ClientError as exc:
        if _is_not_found(exc):
            raise SchemaBodyAbsent(f"no object at s3://{bucket_name}/{body_key}") from exc
        raise

    sidecar: dict[str, Any] = {}
    try:
        existing = json.loads(s3_client.get_object(Bucket=bucket_name, Key=sidecar_key)["Body"].read())
    except ClientError as exc:
        if not _is_not_found(exc):
            raise
        existing = None
    if existing is not None:
        if not isinstance(existing, dict):
            raise ValueError(f"the Context_Sidecar at {sidecar_key} is a {type(existing).__name__}, not a JSON object")
        sidecar = dict(existing)

    apply_change(sidecar)

    s3_client.put_object(
        Bucket=bucket_name,
        Key=sidecar_key,
        Body=json.dumps(sidecar, indent=2),
        ContentType="application/json",
    )
    logger.info(f"Wrote Context_Sidecar s3://{bucket_name}/{sidecar_key} (keys: {sorted(sidecar)})")
    return sidecar


def _is_not_found(error: ClientError) -> bool:
    """Whether a `ClientError` means "the object is not there" rather than "the read failed".

    `NOT_FOUND_CLIENT_ERROR_CODES` is imported rather than re-listed so this
    classification cannot drift from the one the step Lambdas use — `head_object`
    answers `404` where `get_object` answers `NoSuchKey`, and both are in it.
    """
    return str(error.response.get("Error", {}).get("Code", "")) in NOT_FOUND_CLIENT_ERROR_CODES


def _sidecar_field_contexts(sidecar: dict[str, Any]) -> dict[str, Any]:
    """The sidecar's `field_contexts` map, created if absent, refused if malformed."""
    contexts = sidecar.setdefault(_SIDECAR_FIELD_CONTEXTS, {})
    if not isinstance(contexts, dict):
        raise ValueError(
            f"the Context_Sidecar's {_SIDECAR_FIELD_CONTEXTS} is a {type(contexts).__name__}, not an object"
        )
    return contexts


def _refuse_catalog_derived_context(schema_id: str, endpoint: str) -> dict[str, Any] | None:
    """R2.5's `400` for an id that is not an Uploaded_Schema, or `None` to proceed.

    A `glue_catalog` or `s3_table_bucket` id is a **synthesised** identifier for a
    catalog entry owned by `create_infrastructure` or by the S3 Tables service
    (`_catalog_derived_source`). It names no object under `shared/`, so it has no
    Context_Sidecar address — and `_schema_detail` reads the sidecar only for
    `source == "uploaded"`, so anything written against such an id would be
    stored and never displayed. R2.5 states this for the context `PUT`; the same
    `400` is returned for the label `PUT` by inference from the same fact rather
    than from a requirement, because a label nothing can render is the same
    unsupportable claim.
    """
    catalog_source = _catalog_derived_source(schema_id)
    if not catalog_source:
        return None
    logger.info(f"Refusing {endpoint} for catalog-derived schema {schema_id} ({catalog_source})")
    return create_response(
        400,
        {
            "error": (
                f"Schema {schema_id} is a view of a catalog object ({catalog_source}). Only uploaded "
                f"schemas carry editable context and display labels — a catalog entry's description is "
                f"owned by the catalog that created it."
            ),
            "reason": "not_an_uploaded_schema",
            "schemaId": schema_id,
            "source": catalog_source,
        },
    )


def _string_field(event: dict[str, Any], key: str) -> str:
    """One required string out of a JSON request body.

    Raises `ValueError` with a message fit to return, so both handlers share one
    `400` shape instead of two copies of the same four checks.
    """
    raw = event.get("body") or "{}"
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON body: {exc!s}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Request body must be a JSON object carrying {key!r}")
    if key not in payload:
        raise ValueError(f"Request body must carry {key!r}")
    value = payload[key]
    if value is None:
        # Explicit `null` is the clear, spelled the way a JSON client spells
        # "no value" — same outcome as `""`.
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key!r} must be a string, got {type(value).__name__}")
    return value


def update_field_context(schema_id: str, field_name: str, event: dict[str, Any]) -> dict[str, Any]:
    """``PUT /api/schemas/{id}/fields/{name}/context`` — R2.1, R2.3, R2.5, R2.6.

    Writes `{"field_contexts": {field_name: text}}` into the Context_Sidecar —
    the shape `handlers/pipelines_crud.py:800` **already reads**, so this closes
    D3 (`LGR-acca05d-3`) by adding the missing writer rather than by inventing a
    format. Request body is `{"context": "<text>"}`, the key
    `use-schemas.ts:121-123` already sends.

    Four answers:

    | Outcome | Status |
    |---|---|
    | the text was stored (or cleared) | `200`, echoing what is now stored |
    | the id is catalog-derived | `400` — R2.5, only uploaded schemas carry editable context |
    | no object backs the id | `404` naming the id |
    | the sidecar could not be read or written | `500` naming the failure — R2.1's "report the failure" |

    R2.1's other half — "leave the entered text recoverable" — is the frontend's
    (task 8.7): what this handler owes it is a status and a reason it can render,
    which is why the `500` carries `details` instead of a fixed message.
    """
    try:
        try:
            context = _string_field(event, "context")
        except ValueError as exc:
            return create_response(400, {"error": str(exc), "schemaId": schema_id, "fieldName": field_name})

        refusal = _refuse_catalog_derived_context(schema_id, "update_field_context")
        if refusal is not None:
            return refusal

        def _set_context(sidecar: dict[str, Any]) -> None:
            contexts = _sidecar_field_contexts(sidecar)
            if context.strip():
                # Verbatim — no trimming, so what the user typed is what a
                # pipeline created against this schema carries (R2.4).
                contexts[field_name] = context
            else:
                contexts.pop(field_name, None)

        try:
            sidecar = _apply_to_context_sidecar(schema_id, _set_context)
        except SchemaBodyAbsent:
            return create_response(
                404,
                {
                    "error": f"Schema not found: {schema_id}",
                    "schemaId": schema_id,
                    "fieldName": field_name,
                },
            )

        stored = sidecar.get(_SIDECAR_FIELD_CONTEXTS, {}).get(field_name, "")
        return create_response(
            200,
            {
                "message": f"Context stored for {schema_id}.{field_name}"
                if stored
                else f"Context cleared for {schema_id}.{field_name}",
                "schemaId": schema_id,
                "fieldName": field_name,
                "context": stored,
            },
        )

    except Exception as e:
        logger.error(f"Error writing field context for {schema_id}.{field_name}: {e}")
        return create_response(500, {"error": "Failed to store field context", "details": str(e)})


def set_display_label(schema_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """``PUT /api/schemas/{id}/display-label`` — R4.9, R4.10, R4.11, R4.12.

    Writes `display_name` into the **same** Context_Sidecar object, beside
    `field_contexts`. Request body is `{"displayLabel": "<label>"}` — camelCase,
    matching the `displayLabel` key DD9's per-id response carries and the
    `{"context": …}` precedent of the sibling context `PUT`. Nothing in the
    requirements names the request key; Property 3 named it first
    (`test_schema_rename_invariance_prop.py`), and task 8.7's `useSetDisplayLabel`
    must send the same one.

    **This is an attribute write and nothing else.** The schema's id, its backing
    object's key and that object's bytes are byte-identical afterwards (R4.10),
    no pipeline record is touched (R4.11), and the only S3 write is the sidecar.
    See the block comment above this section for each of those with its record.

    The label is stored **verbatim** — no uniqueness transformation and no
    trimming (R4.7), so two schemas may carry the same label and
    `"  padded label  "` keeps its padding. An empty, whitespace-only or `null`
    label **clears** the label rather than being refused; that choice, and why it
    is not a stored `""`, is argued in the block comment above.

    | Outcome | Status |
    |---|---|
    | the label was stored (or cleared) | `200` |
    | the id is catalog-derived | `400` — a label nothing renders is not stored |
    | no object backs the id | `404` naming the id |
    | the sidecar could not be read or written | `500` naming the failure |
    """
    try:
        try:
            label = _string_field(event, "displayLabel")
        except ValueError as exc:
            return create_response(400, {"error": str(exc), "schemaId": schema_id})

        refusal = _refuse_catalog_derived_context(schema_id, "set_display_label")
        if refusal is not None:
            return refusal

        def _set_label(sidecar: dict[str, Any]) -> None:
            if label.strip():
                sidecar[_SIDECAR_DISPLAY_NAME] = label
            else:
                sidecar.pop(_SIDECAR_DISPLAY_NAME, None)

        try:
            sidecar = _apply_to_context_sidecar(schema_id, _set_label)
        except SchemaBodyAbsent:
            return create_response(404, {"error": f"Schema not found: {schema_id}", "schemaId": schema_id})

        stored = sidecar.get(_SIDECAR_DISPLAY_NAME)
        if stored:
            return create_response(
                200,
                {
                    "message": f"Display label set for {schema_id}",
                    "schemaId": schema_id,
                    "displayLabel": stored,
                },
            )
        # `displayLabel` is **absent, not null**, exactly as in DD9's per-id
        # response: a client that falls back on a missing key renders the schema
        # id (R4.8), while one handed `null` renders nothing.
        return create_response(
            200,
            {
                "message": f"Display label cleared for {schema_id}",
                "schemaId": schema_id,
                "cleared": True,
            },
        )

    except Exception as e:
        logger.error(f"Error setting display label for {schema_id}: {e}")
        return create_response(500, {"error": "Failed to set display label", "details": str(e)})
