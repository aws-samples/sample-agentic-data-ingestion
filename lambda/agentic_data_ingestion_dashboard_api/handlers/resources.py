# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
API handlers for listing AWS resources (S3 Tables, Glue Catalog)
"""

import json
from datetime import datetime
from typing import Any

import boto3

# `create_response` is the shared HTTP-response builder, re-exported by
# `.common` from `shared/response_utils.py` (P21 Req 5.2). It applies the
# canonical Content-Type + CORS headers and Decimal-aware JSON encoding so
# these handlers no longer hand-roll `{"statusCode", "headers", "body"}` dicts.
from handlers.common import AWS_REGION, create_response, get_aws_account_id, logger

# `_paginated_items` is imported ACROSS modules and its leading underscore means
# **package-private within `handlers/`**, not private to `handlers/schemas.py`
# (R3.3, C12). Said explicitly because the name alone reads as the opposite, and
# because ruff will not say it: `pyproject.toml`'s `select` list carries no `PL*`
# rules, so `PLC2701` (private-member-import) is not enforced here — that is a
# licence, not the reason. The reason is that it is the ONE place `get_paginator`
# is called for `glue:GetDatabases` / `glue:GetTables` / `s3:ListObjectsV2`, so a
# second definition in this module is exactly the drift the helper exists to
# prevent. `handlers/schemas.py` constructs no client and reads no environment at
# module scope, so this import adds neither to the route surface.
from handlers.schemas import _paginated_items

# Initialize AWS clients
s3tables_client = boto3.client("s3tables")
glue_client = boto3.client("glue")


def _normalize_aws_datetime(value: Any) -> Any:
    """Normalize an AWS-API datetime to a frontend-friendly 'Z' suffixed string.

    AWS SDK returns timezone-aware ``datetime`` objects whose ``.isoformat()``
    produces a ``+00:00`` offset that the frontend cannot parse. Only datetimes
    are reformatted; strings and ``None`` are passed through unchanged.
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


def list_s3_tables_buckets(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    List all S3 Tables buckets in the account.

    GET /api/resources/s3-tables/buckets
    """
    try:
        logger.info("Listing S3 Tables buckets")

        response = s3tables_client.list_table_buckets()

        buckets = []
        for bucket in response.get("tableBuckets", []):
            buckets.append(
                {
                    "name": bucket.get("name"),
                    "arn": bucket.get("arn"),
                    "createdAt": _normalize_aws_datetime(bucket.get("createdAt")),
                }
            )

        logger.info(f"Found {len(buckets)} S3 Tables buckets")

        return create_response(200, {"buckets": buckets, "count": len(buckets)})

    except Exception as e:
        logger.error(f"Error listing S3 Tables buckets: {e}", exc_info=True)
        return create_response(500, {"error": "Failed to list S3 Tables buckets", "message": str(e)})


def list_s3_tables_in_bucket(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    List all tables in a specific S3 Tables bucket.

    GET /api/resources/s3-tables/buckets/{bucket_name}/tables
    """
    try:
        # Extract bucket name from path parameters
        bucket_name = event.get("pathParameters", {}).get("bucket_name")

        if not bucket_name:
            return create_response(400, {"error": "bucket_name is required"})

        logger.info(f"Listing tables in S3 Tables bucket: {bucket_name}")

        # First, get the bucket ARN
        buckets_response = s3tables_client.list_table_buckets()
        bucket_arn = None

        for bucket in buckets_response.get("tableBuckets", []):
            if bucket.get("name") == bucket_name:
                bucket_arn = bucket.get("arn")
                break

        if not bucket_arn:
            return create_response(404, {"error": f"S3 Tables bucket '{bucket_name}' not found"})

        # List tables in the bucket
        response = s3tables_client.list_tables(tableBucketARN=bucket_arn)

        tables = []
        for table in response.get("tables", []):
            tables.append(
                {
                    "name": table.get("name"),
                    "namespace": table.get("namespace", [None])[0]
                    if isinstance(table.get("namespace"), list)
                    else table.get("namespace", ""),
                    "arn": table.get("arn"),
                    "type": table.get("type"),
                    "format": table.get("format"),
                    "createdAt": _normalize_aws_datetime(table.get("createdAt")),
                }
            )

        logger.info(f"Found {len(tables)} tables in bucket {bucket_name}")

        return create_response(
            200,
            {"bucket_name": bucket_name, "bucket_arn": bucket_arn, "tables": tables, "count": len(tables)},
        )

    except Exception as e:
        logger.error(f"Error listing tables in bucket: {e}", exc_info=True)
        return create_response(500, {"error": "Failed to list tables", "message": str(e)})


def list_glue_databases(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    List all Glue databases in the account.

    GET /api/resources/glue/databases
    """
    try:
        logger.info("Listing Glue databases")

        databases = []
        # R3.1 — paginated. `get_databases` pages server-side, so a single call
        # returned at most one page's worth and every database past it was
        # silently absent from the picker. `glue_client` is passed as an argument
        # rather than closed over, so the module-level handle is still read
        # through this module's namespace at call time and stays substitutable.
        for db in _paginated_items(glue_client, "get_databases", "DatabaseList"):
            databases.append(
                {
                    "name": db.get("Name"),
                    "description": db.get("Description", ""),
                    "locationUri": db.get("LocationUri", ""),
                    "createTime": db.get("CreateTime", "").isoformat() if db.get("CreateTime") else None,
                }
            )

        logger.info(f"Found {len(databases)} Glue databases")

        return create_response(200, {"databases": databases, "count": len(databases)})

    except Exception as e:
        logger.error(f"Error listing Glue databases: {e}", exc_info=True)
        return create_response(500, {"error": "Failed to list Glue databases", "message": str(e)})


def list_glue_tables(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    List all tables in a specific Glue database.

    GET /api/resources/glue/databases/{database_name}/tables
    """
    try:
        # Extract database name from path parameters
        database_name = event.get("pathParameters", {}).get("database_name")

        if not database_name:
            return create_response(400, {"error": "database_name is required"})

        logger.info(f"Listing tables in Glue database: {database_name}")

        tables = []
        # R3.2 — paginated per database, for the same reason as `get_databases`
        # above. `DatabaseName` reaches the paginator through `**kwargs`.
        for table in _paginated_items(glue_client, "get_tables", "TableList", DatabaseName=database_name):
            tables.append(
                {
                    "name": table.get("Name"),
                    "databaseName": table.get("DatabaseName"),
                    "description": table.get("Description", ""),
                    "storageDescriptor": {
                        "location": table.get("StorageDescriptor", {}).get("Location", ""),
                        "inputFormat": table.get("StorageDescriptor", {}).get("InputFormat", ""),
                        "outputFormat": table.get("StorageDescriptor", {}).get("OutputFormat", ""),
                    },
                    "createTime": table.get("CreateTime", "").isoformat() if table.get("CreateTime") else None,
                    "updateTime": table.get("UpdateTime", "").isoformat() if table.get("UpdateTime") else None,
                }
            )

        logger.info(f"Found {len(tables)} tables in database {database_name}")

        return create_response(200, {"database_name": database_name, "tables": tables, "count": len(tables)})

    except Exception as e:
        logger.error(f"Error listing Glue tables: {e}", exc_info=True)
        return create_response(500, {"error": "Failed to list Glue tables", "message": str(e)})


def _parse_iceberg_schema(table_response: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse Iceberg schema by reading the metadata JSON file from S3.

    The S3 Tables GetTable API returns a metadataLocation (S3 URI) pointing
    to the Iceberg metadata JSON. We fetch that file and extract the current
    schema's field definitions.
    """
    metadata_location = table_response.get("metadataLocation", "")
    if not metadata_location:
        format_info = table_response.get("format", "")
        return [
            {
                "name": "_placeholder",
                "type": "string",
                "nullable": True,
                "description": f"No metadata location found (format: {format_info})",
            }
        ]

    try:
        # Parse S3 URI: s3://bucket/key or s3a://bucket/key
        s3_client = boto3.client("s3")
        uri = metadata_location.replace("s3a://", "s3://").replace("s3://", "")
        parts = uri.split("/", 1)
        if len(parts) < 2:
            raise ValueError(f"Invalid metadata location: {metadata_location}")
        bucket = parts[0]
        key = parts[1]

        response = s3_client.get_object(Bucket=bucket, Key=key)
        metadata = json.loads(response["Body"].read().decode("utf-8"))

        # Extract schema fields from Iceberg metadata
        fields = []

        # Format version 2: schemas array with current-schema-id
        if "schemas" in metadata and isinstance(metadata["schemas"], list):
            current_schema_id = metadata.get("current-schema-id", 0)
            current_schema = None
            for s in metadata["schemas"]:
                if s.get("schema-id") == current_schema_id:
                    current_schema = s
                    break
            if not current_schema and metadata["schemas"]:
                current_schema = metadata["schemas"][-1]  # fallback to last
            if current_schema:
                fields = current_schema.get("fields", [])

        # Format version 1: single schema object
        elif "schema" in metadata and isinstance(metadata["schema"], dict):
            fields = metadata["schema"].get("fields", [])

        if not fields:
            return [
                {
                    "name": "_no_fields",
                    "type": "string",
                    "nullable": True,
                    "description": "Iceberg metadata found but no fields in schema",
                }
            ]

        # Map Iceberg field types to simpler types
        schema = []
        for field in fields:
            iceberg_type = field.get("type", "string")
            # Iceberg types can be complex (e.g., {"type": "list", "element": "string"})
            if isinstance(iceberg_type, dict):
                iceberg_type = iceberg_type.get("type", "string")

            schema.append(
                {
                    "name": field.get("name", ""),
                    "type": str(iceberg_type),
                    "nullable": not field.get("required", False),
                    "description": field.get("doc", ""),
                }
            )

        return schema

    except Exception as e:
        logger.warning(f"Failed to read Iceberg metadata from {metadata_location}: {e}")
        format_info = table_response.get("format", "")
        return [
            {
                "name": "_error",
                "type": "string",
                "nullable": True,
                "description": f"Failed to read Iceberg metadata: {str(e)[:200]}",
            }
        ]


# ---------------------------------------------------------------------------
# GLUE_CATALOG target pre-flight
# ---------------------------------------------------------------------------
#
# ``GLUE_CATALOG`` is the one target type that does NOT create its destination.
# ``create_infrastructure`` refuses explicitly
# (``create_infrastructure/lambda_function.py:801-802``), while S3 Tables and
# S3 Parquet both create theirs on first write — so a user who reasonably assumes
# symmetry gets an opaque Glue failure deep inside a run instead.
#
# Three preconditions govern a GLUE_CATALOG target, and until now all three were
# documented only in the e2e test's setup comment
# (``tests/e2e/targets/test_glue_catalog_target.py:60-76``):
#
#   1. The table must already exist, with a schema and an S3 location.
#   2. The database name must start with ``agentic_data_ingestion_`` — the
#      per-pipeline Glue role grants Glue actions only on
#      ``database/agentic_data_ingestion_*`` and
#      ``table/agentic_data_ingestion_*/*``
#      (``create_glue_pipeline/iam_roles.py:255-262``), so any other name is
#      denied at run time on GetTable/UpdateTable.
#   3. The table's S3 ``Location`` must be in a bucket the pipeline role can
#      write (``iam_roles.py:220-233``) **and** must not be the source bucket the
#      pipeline watches for uploads — writes there re-trigger the pipeline on its
#      own output. See ``_WRITABLE_BUCKET_STEMS`` for why IAM alone is not the
#      test.
#
# ``glue:GetTable`` answers all three: the prefix is a pure string check made
# BEFORE the call (so a wrong prefix costs no round trip), and
# ``StorageDescriptor`` already carries ``Location``, so precondition 3 needs no
# extra API call.
#
# **The wording here is load-bearing.** The Glue Catalog wizard tile
# (``dashboard-react/src/pages/wizard/WizardStepTarget.tsx``) and subform help
# text (``TargetForms.tsx``) state the same three rules in the same words, so a
# user who reads the tile and then hits one of these refusals recognises it as
# the same rule rather than a second one. Change one and change the others.
#
# ``reason`` is emitted alongside ``error`` so a consumer can branch without
# matching on prose, following the JDBC crawler classifier
# (``handlers/jdbc_resources.py`` ``_crawler_failure_response``, commit
# ``b367259``) and, before it, p35's schema-delete classifier
# (``dashboard-react/src/utils/schema-delete-failure.ts``).
#
# ``handle_error`` is deliberately NOT touched: it returns 500 unconditionally by
# design (``shared/response_utils.py``) and has 66 other call sites. This
# classifies locally, exactly as the JDBC path does.

#: Glue database prefix the per-pipeline Glue role is scoped to.
_GLUE_DATABASE_PREFIX = "agentic_data_ingestion_"

_REASON_GLUE_DATABASE_PREFIX = "glue_catalog_database_prefix"
_REASON_GLUE_TABLE_NOT_FOUND = "glue_catalog_table_not_found"
_REASON_GLUE_LOCATION_NOT_WRITABLE = "glue_catalog_location_not_writable"
_REASON_GLUE_LOCATION_SELF_TRIGGERING = "glue_catalog_location_self_triggering"
_REASON_GLUE_ACCESS_DENIED = "glue_catalog_access_denied"
_REASON_GLUE_UNEXPECTED = "glue_catalog_error"

#: Bucket-name prefixes the pipeline role can write for a GLUE_CATALOG target,
#: read off the wildcard ARNs in ``iam_roles.py:222-232``.
_WRITABLE_BUCKET_PREFIXES = ("adi-generated-code-", "adi-pipeline-artifacts-")

#: Bucket-name stems the same statement names with an account/region suffix.
#: ``adi-processed-files-*`` is the one an operator should normally pick — it is
#: what the e2e suite uses — but ``adi-dependencies-*`` is genuinely writable
#: too, so refusing it would be a false negative.
#:
#: **``adi-source-files`` is deliberately absent, and this is the second entry in
#: the IAM grant that must not be copied out of it.** ``iam_roles.py:227`` really
#: does grant ``s3:PutObject`` on
#: ``adi-source-files-{account}-{region}/*``, so a reader comparing this tuple
#: against that statement will find the omission and want to close it. Do not:
#: the source bucket is the pipeline's **own upload trigger source**, and IAM
#: permission is not the only constraint on it.
#:
#: ``foundation-stack.ts:76`` sets ``eventBridgeEnabled: true`` on that bucket,
#: and ``S3UploadTriggerRule`` (``orchestration-stack.ts:1574-1576``) matches
#: ``detail.object.key`` with ``{'anything-but': {'suffix': '/'}}`` — i.e. every
#: key that is not a folder marker. So a table whose ``StorageDescriptor.Location``
#: is under this bucket has every data file it writes match the upload trigger,
#: starting pipeline executions that write more files that match again. The
#: pipeline re-triggers on its own output.
#:
#: Refused by name with its own reason rather than by omission, because the
#: generic "role cannot write here" refusal would be a false statement about this
#: bucket — see ``_SELF_TRIGGERING_BUCKET_STEM``.
_WRITABLE_BUCKET_STEMS = ("adi-dependencies", "adi-processed-files")

#: The bucket that is IAM-writable but whose writes re-trigger the pipeline.
#: Split out from ``_WRITABLE_BUCKET_STEMS`` rather than merely dropped so the
#: exclusion carries its reason at the point of exclusion, and so the refusal can
#: say what is actually wrong. Same suffix convention as the stems above.
_SELF_TRIGGERING_BUCKET_STEM = "adi-source-files"


def _aws_error_code(exc: BaseException) -> str:
    """Return the AWS error code carried by ``exc``, or ``""``.

    A local copy of ``jdbc_resources._aws_error_code`` rather than an import.
    Importing that module executes its top level, which does three things this
    module must not inherit: it imports ``jdbc_helpers`` (present only in the
    Strands Lambda layer at ``/opt/python``), it constructs three boto3 clients
    (``glue``, ``ec2``, ``rds``), and it computes ``DISCOVERY_DB`` from the
    environment. Pulling all of that in so that two pure functions could be
    shared would make every resources.py route — and every resources.py unit
    test — depend on the JDBC discovery module's environment. Promoting the pair
    into a shared module was the other option and is not worth a new module for
    twelve lines; if a third caller appears, that is the point to reconsider.

    Read off ``exc.response`` rather than matched against
    ``glue_client.exceptions.*`` for the reason the original documents: those
    typed classes are attributes of a *live* client, so a caller holding a
    stubbed client has nothing usable to compare against, and ``except
    <MagicMock>`` raises ``TypeError`` instead of classifying anything. Every
    unit test of this handler patches ``handlers.resources.glue_client`` with a
    ``MagicMock``, so code-based dispatch is the only shape that works here.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""


def _glue_database_prefix_refusal(database_name: str, table_name: str) -> dict[str, Any] | None:
    """Refuse a Glue database the pipeline role could never read.

    Runs BEFORE ``glue:GetTable``: it is a pure string comparison, so a wrong
    prefix should not spend an AWS round trip. Returns ``None`` to proceed.
    """
    if database_name.startswith(_GLUE_DATABASE_PREFIX):
        return None

    logger.info(
        "Glue Catalog pre-flight refused %s.%s: database name does not start with %s",
        database_name,
        table_name,
        _GLUE_DATABASE_PREFIX,
    )
    return create_response(
        400,
        {
            "error": (
                f"The Glue database name must start with '{_GLUE_DATABASE_PREFIX}', and "
                f"'{database_name}' does not. The pipeline's Glue role grants access only to "
                f"databases and tables under that prefix, so a pipeline targeting "
                f"'{database_name}.{table_name}' would be denied at run time when it reads or "
                f"updates the table. Register the table in a database named "
                f"'{_GLUE_DATABASE_PREFIX}<something>' and select it again."
            ),
            "reason": _REASON_GLUE_DATABASE_PREFIX,
        },
    )


def _glue_get_table_failure(exc: BaseException, *, database_name: str, table_name: str) -> dict[str, Any]:
    """Classify a ``glue:GetTable`` failure for a GLUE_CATALOG target.

    Three named outcomes and a default, and the status code answers one question
    — *can the caller fix this by changing the request?* — because that is what a
    4xx claims:

    * ``EntityNotFoundException`` → **400**. Both entities in play came from the
      request, and "no such table" is exactly the precondition this pre-flight
      exists to surface. The message says the pipeline does not create catalog
      tables, because assuming it does is the reason a user is here.
    * ``AccessDeniedException`` → **500**, deliberately, not 403 and not 400.
      What lacks the grant is the *dashboard API's own* execution role, which no
      request change can fix — a deployment matter, not a user error. A 403 would
      claim something false about the end user, who is authorized (the Cognito
      authorizer already passed) and would sit confusingly close to the client's
      401 session-expiry path. This is the same split, for the same reason, as
      ``jdbc_resources._crawler_failure_response``.
    * ``InvalidInputException`` / ``ValidationException`` → **400**. Glue
      rejected the identifiers themselves.
    * anything else → **500**, unchanged in spirit from the outer handler.

    Before this function existed the ``get_table`` call was unguarded, so an
    absent table reached the handler's outer ``except Exception`` and became an
    opaque 500 — the same class of failure, one layer earlier than the Glue
    run-time AccessDenied, and just as undiagnosable.
    """
    code = _aws_error_code(exc)
    detail = str(exc)

    if code == "EntityNotFoundException":
        logger.info(
            "Glue Catalog pre-flight refused %s.%s: Glue has no such database or table",
            database_name,
            table_name,
        )
        return create_response(
            400,
            {
                "error": (
                    f"Glue has no table '{database_name}.{table_name}' in {AWS_REGION}. The "
                    f"pipeline does not create catalog tables — a Glue Catalog target maps to a "
                    f"table that already exists, so it must be registered in the Glue Data "
                    f"Catalog first, with a schema and an S3 location. Note that both the "
                    f"database and the table are looked up in {AWS_REGION}: one created in "
                    f"another region is not visible here even when the name is right."
                ),
                "reason": _REASON_GLUE_TABLE_NOT_FOUND,
                "details": detail,
            },
        )

    if code == "AccessDeniedException":
        logger.error(
            "Glue Catalog pre-flight could not read %s.%s — access denied to the dashboard API role: %s",
            database_name,
            table_name,
            exc,
        )
        return create_response(
            500,
            {
                "error": (
                    f"This deployment is not allowed to read '{database_name}.{table_name}' from "
                    f"the Glue Data Catalog. The refused grant is glue:GetTable on the dashboard "
                    f"API's own execution role, not anything about your account or your request — "
                    f"redeploy AgenticDataIngestion-Compute-<env> and retry. Nothing in the "
                    f"wizard can fix this."
                ),
                "reason": _REASON_GLUE_ACCESS_DENIED,
                "details": detail,
            },
        )

    if code in ("InvalidInputException", "ValidationException"):
        logger.info(
            "Glue Catalog pre-flight refused %s.%s: Glue rejected the identifiers: %s",
            database_name,
            table_name,
            exc,
        )
        return create_response(
            400,
            {
                "error": (
                    f"Glue rejected '{database_name}.{table_name}' as an invalid table "
                    f"reference. The AWS message below names the field it objected to."
                ),
                "reason": _REASON_GLUE_UNEXPECTED,
                "details": detail,
            },
        )

    logger.error(
        "Glue Catalog pre-flight failed for %s.%s (code=%s): %s",
        database_name,
        table_name,
        code or "<none>",
        exc,
        exc_info=True,
    )
    return create_response(
        500,
        {
            "error": f"Failed to read Glue table '{database_name}.{table_name}'.",
            "reason": _REASON_GLUE_UNEXPECTED,
            "details": detail,
        },
    )


def _bucket_from_s3_uri(location: str) -> str:
    """Return the bucket name from an ``s3://``/``s3a://`` URI, or ``""``.

    ``""`` means "could not read this", which callers must treat as *unknown*
    rather than as a failed check.
    """
    if not isinstance(location, str):
        return ""
    uri = location.strip()
    for scheme in ("s3://", "s3a://", "s3n://"):
        if uri.lower().startswith(scheme):
            return uri[len(scheme) :].split("/", 1)[0]
    return ""


def _bucket_is_pipeline_writable(bucket: str, account_id: str) -> bool:
    """True when the per-pipeline Glue role can write ``bucket`` *and* should.

    Derived from the GLUE_CATALOG arm of ``iam_roles.py:220-233``, not from the
    e2e test's single example, minus **two** buckets that grant names and this
    predicate rejects. Both omissions are cases of IAM permitting a write that
    does not work, so neither can be recovered by reading the policy:

    * ``agentic-data-ingestion-table-{pipeline_id}`` — never created for a
      GLUE_CATALOG target, so a table pointed at it fails on the write even
      though the policy permits it. Its ``pipeline_id`` does not exist at wizard
      time either.
    * ``adi-source-files-{account}-{region}`` — writes there re-trigger the
      pipeline on its own output. Handled ahead of this predicate by
      ``_glue_location_refusal`` so the refusal can name the real reason; see
      ``_WRITABLE_BUCKET_STEMS``.

    The set is **permissive-minus-harmful**, not prescriptive: it accepts
    everything the grant permits except writes that are known to break, and does
    not narrow further to the one bucket an operator would normally choose. The
    predicate answers a mechanical question — can the role write here, and will
    writing here break the pipeline — and both halves are facts about the
    deployment. Narrowing to ``adi-processed-files-*`` alone would additionally
    encode a preference, and this refusal is terminal in the wizard with no
    override, so a false refusal blocks a user outright while a false accept
    yields a table in an unusual location that still works.
    """
    if bucket.startswith(_WRITABLE_BUCKET_PREFIXES):
        return True
    return bucket in {f"{stem}-{account_id}-{AWS_REGION}" for stem in _WRITABLE_BUCKET_STEMS}


def _glue_location_refusal(table: dict[str, Any], *, database_name: str, table_name: str) -> dict[str, Any] | None:
    """Refuse a table whose S3 location the pipeline role cannot write.

    **A check that could not run must not become a new way to fail.** This is the
    posture ``jdbc_resources._connection_s3_preflight`` documents across its five
    verdicts, only two of which refuse, and it applies identically here. Three
    outcomes:

    * ``Location`` is under the source bucket → refuse, 400, with its own reason.
      Checked BEFORE the writable set because that set's refusal would be a false
      statement about this bucket: the role can write it (``iam_roles.py:227``);
      what is wrong is that doing so re-triggers the pipeline.
    * ``Location`` reads as a bucket outside the writable set → refuse, 400. A
      definite answer, and actionable.
    * ``Location`` is absent, empty, or not a parseable S3 URI, or the account ID
      cannot be resolved → **proceed**, and log that the check did not run. A
      table with no ``Location`` is unusual but it is not this endpoint's
      business to invent a refusal from a field it could not read, and
      ``get_aws_account_id`` reaching STS is a dependency that can fail on its
      own.
    * the bucket is in the writable set → proceed.

    Returns ``None`` to proceed, a 400 response to refuse.
    """
    storage = table.get("StorageDescriptor")
    location = storage.get("Location", "") if isinstance(storage, dict) else ""
    bucket = _bucket_from_s3_uri(location)

    if not bucket:
        logger.info(
            "Glue Catalog pre-flight: %s.%s has no readable StorageDescriptor.Location "
            "(value=%r) — writable-bucket check did not run, proceeding",
            database_name,
            table_name,
            location,
        )
        return None

    try:
        account_id = get_aws_account_id()
    # Deliberately broad: this resolves an account ID, possibly via STS. Failing
    # to learn it means the check cannot run, which must not fail the request.
    except Exception as exc:
        logger.warning(
            "Glue Catalog pre-flight: could not resolve the AWS account ID (%s) — "
            "writable-bucket check did not run for %s.%s, proceeding",
            exc,
            database_name,
            table_name,
        )
        return None

    if bucket == f"{_SELF_TRIGGERING_BUCKET_STEM}-{account_id}-{AWS_REGION}":
        logger.info(
            "Glue Catalog pre-flight refused %s.%s: location bucket %s is the pipeline's own "
            "upload trigger source, so table writes would re-trigger the pipeline",
            database_name,
            table_name,
            bucket,
        )
        return create_response(
            400,
            {
                "error": (
                    f"The table '{database_name}.{table_name}' stores its data at '{location}', "
                    f"which is in '{bucket}' — the bucket this pipeline watches for uploads. "
                    f"Writing table data there would re-trigger the pipeline on its own output: "
                    f"every object written under that bucket raises an upload event, which starts "
                    f"another execution, which writes more objects. The pipeline role can write "
                    f"this bucket, so the refusal is not about permissions — it is about the loop. "
                    f"Point the table at adi-processed-files-{account_id}-{AWS_REGION} instead."
                ),
                "reason": _REASON_GLUE_LOCATION_SELF_TRIGGERING,
            },
        )

    if _bucket_is_pipeline_writable(bucket, account_id):
        return None

    logger.info(
        "Glue Catalog pre-flight refused %s.%s: location bucket %s is not pipeline-writable",
        database_name,
        table_name,
        bucket,
    )
    return create_response(
        400,
        {
            "error": (
                f"The table '{database_name}.{table_name}' stores its data at '{location}', and "
                f"the pipeline role cannot write to bucket '{bucket}'. The table's S3 location "
                f"must be in a bucket the pipeline can write: normally "
                f"adi-processed-files-{account_id}-{AWS_REGION} (also accepted: "
                f"adi-dependencies-{account_id}-{AWS_REGION} and any "
                f"adi-generated-code-* / adi-pipeline-artifacts-* bucket). Two buckets the "
                f"role's policy names are still refused: the per-pipeline "
                f"agentic-data-ingestion-table-<pipeline_id> bucket, which is never created for "
                f"a Glue Catalog target, and adi-source-files-{account_id}-{AWS_REGION}, which "
                f"is the pipeline's own upload trigger source."
            ),
            "reason": _REASON_GLUE_LOCATION_NOT_WRITABLE,
        },
    )


def detect_schema(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Detect schema from a target destination (Glue Catalog or S3 Tables).

    GET /api/resources/detect-schema?target_type=...&target_config=...

    For ``glue_catalog`` this doubles as the target's pre-flight: the single
    ``glue:GetTable`` it already made answers all three of the target type's
    preconditions, so each now surfaces as an actionable 400 naming which one
    failed instead of as a mid-run Glue ``AccessDenied``. See the block comment
    above for the preconditions and for why the copy is duplicated into the
    wizard verbatim.
    """
    try:
        query_params = event.get("queryStringParameters") or {}
        target_type = query_params.get("target_type", "")
        target_config_raw = query_params.get("target_config", "{}")

        try:
            target_config = json.loads(target_config_raw)
        except (json.JSONDecodeError, TypeError):
            return create_response(400, {"error": "Invalid target_config JSON"})

        logger.info(f"Detecting schema for target_type={target_type}")

        if target_type == "glue_catalog":
            database_name = target_config.get("database_name", "")
            table_name = target_config.get("table_name", "")

            if not database_name or not table_name:
                return create_response(400, {"error": "database_name and table_name are required for glue_catalog"})

            # Precondition 2, checked first: a pure string comparison should not
            # spend a Glue round trip to reach the same conclusion.
            prefix_refusal = _glue_database_prefix_refusal(database_name, table_name)
            if prefix_refusal is not None:
                return prefix_refusal

            # Precondition 1. This call used to be unguarded, so an absent table
            # became an opaque 500 from the outer handler.
            try:
                response = glue_client.get_table(
                    DatabaseName=database_name,
                    Name=table_name,
                )
            # Broad by intent: classified by AWS error code in the helper, which
            # is the only shape that works against a stubbed client.
            except Exception as exc:
                return _glue_get_table_failure(exc, database_name=database_name, table_name=table_name)

            table = response.get("Table", {})

            # Precondition 3, from the StorageDescriptor already in hand — no
            # extra API call. Proceeds when it cannot read Location.
            location_refusal = _glue_location_refusal(table, database_name=database_name, table_name=table_name)
            if location_refusal is not None:
                return location_refusal

            columns = table.get("StorageDescriptor", {}).get("Columns", [])
            partition_keys = table.get("PartitionKeys", [])

            schema = []
            for c in columns + partition_keys:
                schema.append(
                    {
                        "name": c.get("Name", ""),
                        "type": c.get("Type", "string"),
                        "nullable": True,
                        "description": c.get("Comment", ""),
                    }
                )

            return create_response(200, {"schema": schema})

        elif target_type == "s3_tables":
            table_bucket_arn = target_config.get("table_bucket_arn", "")
            namespace = target_config.get("namespace", "")
            table_name = target_config.get("table_name", "")

            if not table_bucket_arn or not namespace or not table_name:
                return create_response(
                    400,
                    {"error": "table_bucket_arn, namespace, and table_name are required for s3_tables"},
                )

            table_response = s3tables_client.get_table(
                tableBucketARN=table_bucket_arn,
                namespace=namespace,
                name=table_name,
            )
            schema = _parse_iceberg_schema(table_response)

            return create_response(200, {"schema": schema})

        else:
            return create_response(400, {"error": f"Schema detection not supported for target type: {target_type}"})

    except Exception as e:
        logger.error(f"Error detecting schema: {e}", exc_info=True)
        return create_response(500, {"error": "Failed to detect schema", "message": str(e)})
