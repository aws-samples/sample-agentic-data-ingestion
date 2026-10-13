# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.exceptions import ClientError

# boto3-stubs are a dev dependency and are not on the Lambda's runtime path, so
# the Glue TypeDefs are imported for the checker only — the same shape
# ``create_glue_pipeline/job_builders.py:33-39`` uses for ``JobUpdateTypeDef``.
if TYPE_CHECKING:
    from mypy_boto3_glue.type_defs import ColumnTypeDef, TableInputTypeDef

# Add shared directory to path for imports
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))

# Initialize logger
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# Import shared env_utils via the Layer_Import_Fallback (fix path + import the
# canonical resolver — do not redefine it inline; P21 Req 5.3/5.4/5.7).
# decimal_utils: P28 R19.2 single shared float→Decimal converter (replaces the
# local ``convert_floats_to_decimals`` copy).
# glue_mirror_identifiers: p35 R5.7 — the ONE definition of the Glue mirror's
# (database, table) pair. This module is its defining caller; the Schema_Listing
# exclusion and job_completion's metadata_location writer read the same pair.
# LGR-acca05d-1's revisit_when is precisely "create_infrastructure changes
# either derivation", so do not re-inline it here.
# glue_table_input: p35 R7.3/R7.6 — the keys glue:UpdateTable accepts inside a
# TableInput. Shared with job_completion because BOTH Lambdas now
# read-modify-write the same mirror table, and two allowlists that disagree
# would produce two different tables from one read, silently (see that module's
# docstring). Do not re-inline it here either.
try:
    from decimal_utils import convert_floats_to_decimal
    from env_utils import get_aws_account_id, require_env
    from glue_mirror_identifiers import glue_mirror_identifiers
    from glue_table_input import glue_table_input_from_stored
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from decimal_utils import convert_floats_to_decimal
    from env_utils import get_aws_account_id, require_env
    from glue_mirror_identifiers import glue_mirror_identifiers
    from glue_table_input import glue_table_input_from_stored


from artifact_schema import create_output_data

# P28 R1.1: Step_Lambdas raise on failure instead of returning error payloads.
from sfn_task_token import step_task_token
from step_errors import StepFailure, raise_for_read_client_error, step_handler
from step_names import STEP_SEQUENCE, StepName

try:
    from metrics_facade import PipelineMetrics
except ImportError:
    PipelineMetrics = None

# Environment variables
PIPELINE_RUNS_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
PIPELINES_TABLE = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

# Initialize AWS clients
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
sts_client = boto3.client("sts")
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)
pipelines_table = dynamodb.Table(PIPELINES_TABLE)


def get_pipeline_metadata(pipeline_id: str) -> dict:
    """
    Get pipeline metadata including target_config.
    If target_config doesn't exist, derive it from target schema for backward compatibility.
    Properly deserializes DynamoDB types to Python types.

    Not-found returns {}. Infrastructure errors (throttles, access denied)
    raise instead of masquerading as missing metadata (P28 R17.3).
    """
    try:
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})
    except ClientError as e:
        raise_for_read_client_error(e, "create_infrastructure", operation="get_pipeline_metadata")
        logger.error(f"Pipelines table not found getting metadata for {pipeline_id}: {e}")
        return {}

    if "Item" not in response:
        return {}

    # DynamoDB resource automatically deserializes to Python types
    metadata = response["Item"]

    # If target_config doesn't exist, try to derive it from target schema (backward compatibility)
    if "target_config" not in metadata:
        logger.info("target_config not found, attempting to derive from target schema")

        target_schema_bucket = metadata.get("target_schema_bucket")
        target_schema_key = metadata.get("target_schema_s3_key")

        if target_schema_bucket and target_schema_key:
            try:
                # Read target schema from S3 — best-effort backward-compat
                # derivation: failure means downstream infra detection uses
                # whatever config exists rather than the derived hint.
                s3 = boto3.client("s3")
                schema_obj = s3.get_object(Bucket=target_schema_bucket, Key=target_schema_key)
                schema = json.loads(schema_obj["Body"].read())

                # Extract target_type from schema metadata
                target_type = schema.get("metadata", {}).get("target_type", "UNKNOWN")

                # Create minimal target_config
                metadata["target_config"] = {
                    "type": target_type,
                    "derived": True,  # Flag to indicate this was derived
                }

                logger.info(f"Derived target_config with type: {target_type}")

            except Exception as e:
                logger.warning(f"Failed to derive target_config from schema: {e}")

    return metadata


def _map_json_type_to_iceberg(json_type: str) -> str:
    """Map JSON schema types to Iceberg types."""
    type_mapping = {
        "string": "string",
        "integer": "int",
        "number": "double",
        "boolean": "boolean",
        "array": "list<string>",
        "object": "struct<>",
        "null": "string",
    }
    return type_mapping.get(json_type.lower(), "string")


# ---------------------------------------------------------------------------
# The Glue mirror's Columns and Parameters — p35 R7.1, R7.2, R7.7
# ---------------------------------------------------------------------------
#: The vocabulary ``target_schema["fields"][].type`` actually carries — a
#: JSON-Schema-ish one — mapped to the Glue/Hive column types a Data Quality
#: rule attaches to (``LGR-7c18e81-1``, **backfilled**: the registration exists
#: for DQ console integration, and an entry for a table with no columns
#: describes nothing).
#:
#: Deliberately **not** ``_map_json_type_to_iceberg`` above. That is a different
#: mapping for a different target: it is silent about a type it does not know,
#: and it emits ``list<string>`` / ``struct<>``, which are Iceberg spellings
#: rather than Glue ones. Design §5 risk 4: **no test and no live registration
#: in this account has ever passed a non-empty ``Columns`` for an Iceberg
#: ``EXTERNAL_TABLE``**, so the accepted type set is unverified. Hence map only
#: the four types the vocabulary produces, warn on anything else, and leave the
#: whole registration inside its existing non-blocking ``except`` so a rejection
#: degrades to today's behaviour instead of failing provisioning.
_GLUE_COLUMN_TYPES = {
    "string": "string",
    "integer": "int",
    "number": "double",
    "boolean": "boolean",
}

#: What an unrecognised declared type becomes. A visible coarsening — never a
#: guess, never an empty ``Type`` Glue cannot store, and never a dropped column,
#: because a missing column is a lie about the table.
_GLUE_COLUMN_FALLBACK_TYPE = "string"


def _glue_column_type(field_name: str, declared_type: Any) -> str:
    """The Glue column type for one declared field type (p35 R7.7).

    An unrecognised type falls back to ``string`` and is **logged naming the
    field and the unmapped type**, so the coarsening is visible instead of
    silent. Design §5 risk 4 is why the warning exists rather than a comment:
    the type set is unverified, so the first live surprise should arrive as a log
    record naming the field that caused it.
    """
    mapped = _GLUE_COLUMN_TYPES.get(str(declared_type or "").strip().lower())
    if mapped:
        return mapped
    logger.warning(
        "Glue mirror column %s: no Glue column type is mapped for the declared type %r, "
        "storing it as %s. The column is described coarsely rather than dropped or left "
        "untyped (p35 R7.7).",
        field_name,
        declared_type,
        _GLUE_COLUMN_FALLBACK_TYPE,
    )
    return _GLUE_COLUMN_FALLBACK_TYPE


def _glue_mirror_columns(target_schema: dict) -> list["ColumnTypeDef"]:
    """The columns the target schema declares, in declaration order (p35 R7.7).

    Costs **no read**: ``target_schema["fields"]`` is already in hand, because
    ``lambda_handler`` refuses to proceed without it — it raises
    ``ValueError("target_schema with fields is required for S3 table bucket
    creation")`` — so it is non-empty on every path that reaches the
    registration.

    Do **not** read the Iceberg schema for these instead. ``LGR-2eaf8a3-1``
    (**backfilled**) fences that read to the S3 Tables Iceberg REST
    ``loadTable`` path with SigV4: a new signed client, a fifth IAM grant
    (``s3tables:GetTableData``), and no columns at all for a
    provisioned-and-never-run table — which is the state R7.1's omission exists
    to keep honest.
    """
    columns: list[ColumnTypeDef] = []
    for field in target_schema.get("fields") or []:
        if not isinstance(field, dict):
            logger.warning("Glue mirror column skipped: %r is not a field object", field)
            continue
        name = str(field.get("name") or "").strip()
        if not name:
            logger.warning("Glue mirror column skipped: field %r declares no name to address it by", field)
            continue
        columns.append({"Name": name, "Type": _glue_column_type(name, field.get("type"))})
    return columns


def _glue_mirror_parameters(resolved_metadata_location: str) -> dict[str, str]:
    """The mirror's ``Parameters``, with ``metadata_location`` **omitted** when
    there is none to name (p35 R7.1).

    Not cosmetic: ``LGR-78ce0b7-14`` (**template / ratified**) makes a named
    location a claim that the thing exists, and ``""`` is a name. A consumer
    that follows an empty pointer gets nothing and cannot tell whether the
    pointer was never attempted or genuinely is not there yet.

    **Do NOT add a wait for one here.** Phase 1 established the emptiness is
    structural rather than a race: ``create_table(..., format="ICEBERG")``
    passes no schema, so S3 Tables writes no initial Iceberg metadata and
    ``metadataLocation`` stays absent until the first commit — a table 22
    minutes old still had none. No wait inside this Lambda's 600-second budget
    could succeed, and that budget sits on the Step Functions critical path.
    """
    parameters = {"table_type": "ICEBERG", "classification": "iceberg"}
    if resolved_metadata_location:
        parameters["metadata_location"] = resolved_metadata_location
    return parameters


def create_s3_table_bucket(pipeline_id: str, target_schema: dict) -> dict[str, Any]:
    """
    Create S3 Table Bucket, namespace, and table using boto3 s3tables client.
    Idempotent: checks existence before creating, handles ConflictException.

    Args:
        pipeline_id: Pipeline identifier
        target_schema: Target schema with fields

    Returns:
        {
            'bucket_name': str,
            'table_name': str,
            'table_bucket_arn': str,
            'table_arn': str,
            'namespace': str,
            'warehouse_location': str,
            'metadata_location': str,
            'status': 'created' | 'already_exists',
        }
    """
    try:
        # Read user-specified names from target_config
        pipeline_metadata = get_pipeline_metadata(pipeline_id)
        target_config = pipeline_metadata.get("target_config", {})

        # Use user-specified values or fallback to defaults
        bucket_name = target_config.get(
            "bucket_name", f"agentic-data-ingestion-table-{pipeline_id.lower()}-{get_aws_account_id()}"
        )
        namespace_name = target_config.get("namespace_name", pipeline_id.lower().replace("-", "_"))
        table_name = target_config.get("table_name", target_schema.get("name", "default_table"))

        # S3 Tables only supports single-level namespaces, ensure underscores
        namespace = namespace_name.replace("-", "_")

        logger.info("Creating S3 Tables infrastructure via boto3 API")
        logger.info(f"  Bucket: {bucket_name}, Namespace: {namespace}, Table: {table_name}")

        # Initialize S3 Tables client
        s3tables_client = boto3.client("s3tables", region_name=AWS_REGION)

        # Step 1: Check/Create S3 Tables bucket
        table_bucket_arn = None
        bucket_exists = False
        try:
            list_response = s3tables_client.list_table_buckets()
            for bucket in list_response.get("tableBuckets", []):
                if bucket["name"] == bucket_name:
                    table_bucket_arn = bucket["arn"]
                    bucket_exists = True
                    logger.info(f"S3 Tables bucket '{bucket_name}' already exists with ARN: {table_bucket_arn}")
                    break
        except Exception as e:
            logger.warning(f"Error listing table buckets: {e}")

        if not bucket_exists:
            try:
                create_response = s3tables_client.create_table_bucket(name=bucket_name)
                table_bucket_arn = create_response["arn"]
                logger.info(f"Created S3 Tables bucket: {bucket_name} with ARN: {table_bucket_arn}")
            except s3tables_client.exceptions.ConflictException:
                logger.info(f"S3 Tables bucket {bucket_name} already exists (race condition)")
                # Get existing bucket ARN
                list_response = s3tables_client.list_table_buckets()
                for bucket in list_response.get("tableBuckets", []):
                    if bucket["name"] == bucket_name:
                        table_bucket_arn = bucket["arn"]
                        break
                if not table_bucket_arn:
                    raise ValueError(f"Could not find ARN for existing bucket {bucket_name}")

        # Step 2: Check/Create namespace
        namespace_exists = False
        try:
            list_namespaces_response = s3tables_client.list_namespaces(tableBucketARN=table_bucket_arn)
            for ns in list_namespaces_response.get("namespaces", []):
                # Namespace is returned as array, compare first element
                if ns.get("namespace", []) == [namespace]:
                    namespace_exists = True
                    logger.info(f"Namespace '{namespace}' already exists")
                    break
        except Exception as e:
            logger.warning(f"Error listing namespaces: {e}")

        if not namespace_exists:
            try:
                s3tables_client.create_namespace(
                    tableBucketARN=table_bucket_arn,
                    namespace=[namespace],  # API expects array with single element
                )
                logger.info(f"Created namespace: {namespace}")
            except s3tables_client.exceptions.ConflictException:
                logger.info(f"Namespace {namespace} already exists (race condition)")

        # Step 3: Check/Create table
        table_arn = None
        table_exists = False
        try:
            list_tables_response = s3tables_client.list_tables(tableBucketARN=table_bucket_arn, namespace=namespace)
            for table in list_tables_response.get("tables", []):
                if table["name"] == table_name:
                    table_arn = table.get("tableARN", "")
                    table_exists = True
                    logger.info(f"Table '{table_name}' already exists with ARN: {table_arn}")
                    break
        except Exception as e:
            logger.warning(f"Error listing tables: {e}")

        if not table_exists:
            try:
                create_table_response = s3tables_client.create_table(
                    tableBucketARN=table_bucket_arn, namespace=namespace, name=table_name, format="ICEBERG"
                )
                table_arn = create_table_response.get("tableARN", "")
                logger.info(f"Created table: {table_name} with ARN: {table_arn}")
            except s3tables_client.exceptions.ConflictException:
                logger.info(f"Table {table_name} already exists (race condition)")
                # Get existing table ARN
                list_tables_response = s3tables_client.list_tables(tableBucketARN=table_bucket_arn, namespace=namespace)
                for table in list_tables_response.get("tables", []):
                    if table["name"] == table_name:
                        table_arn = table.get("tableARN", "")
                        break
                if not table_arn:
                    raise ValueError(f"Could not find ARN for existing table {table_name}")

        # Step 4: Get table metadata location
        metadata_location = ""
        warehouse_location = ""
        try:
            table_details = s3tables_client.get_table(
                tableBucketARN=table_bucket_arn, namespace=namespace, name=table_name
            )
            metadata_location = table_details.get("metadataLocation", "")
            warehouse_location = table_details.get("warehouseLocation", "")
            logger.info(f"Retrieved table metadata - warehouse: {warehouse_location}")
        except Exception as e:
            logger.warning(f"Could not get table metadata location: {e}")

        all_existed = bucket_exists and namespace_exists and table_exists

        # Register S3 Table in Glue Data Catalog for Data Quality console integration
        try:
            glue_client = boto3.client("glue", region_name=AWS_REGION)
            # p35 R5.7: same pair as before, derived in one place now
            # (shared/glue_mirror_identifiers.py). ``namespace`` is already
            # underscore-normalised at :161 — the helper does not repeat that.
            catalog_db, catalog_table = glue_mirror_identifiers(bucket_name, namespace, table_name)

            # p35 R7.7: the columns the table declares, computed once so an
            # unmapped type warns once per run rather than once per branch
            # attempted.
            mirror_columns = _glue_mirror_columns(target_schema)
            fallback_location = f"s3://{bucket_name}/{namespace}/{table_name}"

            # Create database if it doesn't exist
            try:
                glue_client.create_database(
                    DatabaseInput={
                        "Name": catalog_db,
                        "Description": f"AgenticDataIngestion S3 Tables catalog for {bucket_name}",
                    }
                )
                logger.info(f"Created Glue Catalog database: {catalog_db}")
            except glue_client.exceptions.AlreadyExistsException:
                pass

            # ``Parameters`` as actually written, or None if nothing was. R7.2's
            # omission notice is emitted from this below, once, so it reports
            # what the registration *did* rather than what a branch intended.
            written_parameters: dict[str, str] | None = None

            # Register the Iceberg table in the Glue Catalog
            try:
                created_parameters = _glue_mirror_parameters(metadata_location)
                glue_client.create_table(
                    DatabaseName=catalog_db,
                    TableInput={
                        "Name": catalog_table,
                        "Description": f"S3 Tables Iceberg table: {namespace}.{table_name}",
                        "TableType": "EXTERNAL_TABLE",
                        "Parameters": created_parameters,
                        "StorageDescriptor": {
                            "Location": warehouse_location or fallback_location,
                            "InputFormat": "org.apache.iceberg.mr.hive.HiveIcebergInputFormat",
                            "OutputFormat": "org.apache.iceberg.mr.hive.HiveIcebergOutputFormat",
                            "SerdeInfo": {"SerializationLibrary": "org.apache.iceberg.mr.hive.HiveIcebergSerDe"},
                            "Columns": mirror_columns,
                        },
                    },
                )
                written_parameters = created_parameters
                logger.info(f"Registered S3 Table in Glue Catalog: {catalog_db}.{catalog_table}")
            except glue_client.exceptions.AlreadyExistsException:
                # p35 R7.6 — read-modify-write, and the read is load-bearing.
                #
                # Glue ``UpdateTable`` **replaces** the stored table with the
                # ``TableInput`` given, so a write composed from local variables
                # destroys whatever it does not mention. This branch runs once
                # per pipeline run (``should_create_infrastructure`` returns
                # ``(True, …)`` for S3 Tables unconditionally), and at that
                # moment ``s3tables:GetTable`` still reports no
                # ``metadataLocation`` for an unchanged table. So rebuilding
                # ``Parameters`` from locals would erase the pointer
                # ``job_completion`` wrote on the last SUCCEEDED run (R7.3) —
                # turning R7.1's honest omission into R7.6's violation on the
                # very next run. ``glue:GetTable`` is therefore not redundant
                # with ``glue:UpdateTable``, and it is already allowed for this
                # role (``foundation-stack.ts:803-809``), verified.
                try:
                    stored_table = glue_client.get_table(DatabaseName=catalog_db, Name=catalog_table).get("Table") or {}
                    stored_parameters = stored_table.get("Parameters") or {}

                    # Whatever else the mirror carries survives; the
                    # ``metadata_location`` decision is this writer's alone, so
                    # it is dropped first and then re-decided — otherwise a
                    # stored ``""`` from before this change would be carried
                    # forward and R7.1 would not land on an existing mirror.
                    refreshed_parameters = {
                        key: value for key, value in stored_parameters.items() if key != "metadata_location"
                    }
                    refreshed_parameters.update(
                        _glue_mirror_parameters(metadata_location or stored_parameters.get("metadata_location", ""))
                    )

                    storage_descriptor = dict(stored_table.get("StorageDescriptor") or {})
                    storage_descriptor.update(
                        {
                            "Location": warehouse_location or storage_descriptor.get("Location") or fallback_location,
                            "InputFormat": "org.apache.iceberg.mr.hive.HiveIcebergInputFormat",
                            "OutputFormat": "org.apache.iceberg.mr.hive.HiveIcebergOutputFormat",
                            "SerdeInfo": {
                                "SerializationLibrary": "org.apache.iceberg.mr.hive.HiveIcebergSerDe",
                            },
                        }
                    )
                    # R7.7 on the refresh as well as the create. An empty
                    # declared set never clobbers what was read: writing ``[]``
                    # over real columns is the same defect as preserving ``[]``.
                    if mirror_columns:
                        storage_descriptor["Columns"] = mirror_columns
                    else:
                        storage_descriptor.setdefault("Columns", [])

                    table_input = glue_table_input_from_stored(stored_table)
                    table_input["Name"] = catalog_table
                    table_input.setdefault("TableType", "EXTERNAL_TABLE")
                    table_input.setdefault("Description", f"S3 Tables Iceberg table: {namespace}.{table_name}")
                    table_input["Parameters"] = refreshed_parameters
                    table_input["StorageDescriptor"] = storage_descriptor

                    glue_client.update_table(
                        DatabaseName=catalog_db,
                        TableInput=cast("TableInputTypeDef", table_input),
                    )
                    written_parameters = refreshed_parameters
                except Exception as exc:
                    # Non-fatal: refreshing the existing catalog entry's metadata is
                    # best-effort; the table already exists so we keep the prior
                    # registration and continue (P21 Req 6.2/6.5).
                    logger.warning(
                        f"Failed to update existing Glue Catalog table {catalog_db}.{catalog_table}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                logger.info(f"Glue Catalog table already exists: {catalog_db}.{catalog_table}")

            if written_parameters is not None and "metadata_location" not in written_parameters:
                # p35 R7.2: an absent pointer must be distinguishable from an
                # unattempted one. Without this, a mirror with no
                # ``metadata_location`` looks identical whether the registration
                # decided honestly that there was nothing to point at, never ran
                # the read, or hit the non-blocking ``except`` below and gave up
                # — which is what the whole registration does today under the
                # Lambda execution path, where ``glue:CreateTable`` is
                # ``implicitDeny`` (design §5 risk 5).
                logger.info(
                    "Glue mirror %s.%s registered with no metadata_location: the Iceberg table has "
                    "no committed metadata yet, so the registration makes no claim about one (p35 "
                    "R7.1). job_completion populates it on the first SUCCEEDED run (R7.3).",
                    catalog_db,
                    catalog_table,
                )
        except Exception as _catalog_err:
            logger.warning(f"Failed to register S3 Table in Glue Catalog (non-blocking): {_catalog_err}")

        logger.info("S3 Tables infrastructure ready:")
        logger.info(f"   Bucket: {bucket_name} ({'existing' if bucket_exists else 'created'})")
        logger.info(f"   Namespace: {namespace} ({'existing' if namespace_exists else 'created'})")
        logger.info(f"   Table: {table_name} ({'existing' if table_exists else 'created'})")

        return {
            "bucket_name": bucket_name,
            "table_name": table_name,
            "table_bucket_arn": table_bucket_arn,
            "table_arn": table_arn,
            "namespace": namespace,
            "warehouse_location": warehouse_location,
            "metadata_location": metadata_location,
            "status": "already_exists" if all_existed else "created",
        }

    except Exception as e:
        logger.error(f"Error creating S3 table bucket: {e}")
        return {"status": "error", "error_message": str(e)}


def create_s3_table_infrastructure(pipeline_id: str, target_schema: dict) -> dict[str, Any]:
    """
    Create S3 table bucket infrastructure using direct boto3 API calls.
    """
    try:
        logger.info(f"Creating S3 table infrastructure for pipeline: {pipeline_id}")

        # Create S3 table bucket with Iceberg support via direct boto3 API
        table_bucket_result = create_s3_table_bucket(pipeline_id, target_schema)

        if table_bucket_result.get("status") in ("created", "already_exists"):
            data_location = f"s3://{table_bucket_result['bucket_name']}/data/{table_bucket_result['table_name']}/"

            infrastructure_status = {
                "s3_table_bucket": {
                    "bucket_name": table_bucket_result["bucket_name"],
                    "table_name": table_bucket_result["table_name"],
                    "table_bucket_arn": table_bucket_result.get("table_bucket_arn"),
                    "table_arn": table_bucket_result.get("table_arn"),
                    "namespace": table_bucket_result.get("namespace", ""),
                    "status": table_bucket_result["status"],
                    "table_format": "iceberg",
                    "data_location": data_location,
                    "metadata_location": table_bucket_result.get("metadata_location", ""),
                    "warehouse_location": table_bucket_result.get("warehouse_location", ""),
                    "purpose": "table-storage",
                },
                "overall_status": "success",
                "resources_created": ["S3TableBucket", "Namespace", "IcebergTable"],
            }

            logger.info(f"Successfully created S3 table infrastructure: {table_bucket_result['bucket_name']}")
            return infrastructure_status
        else:
            return {
                "overall_status": "error",
                "error_message": table_bucket_result.get("error_message", "Unknown error creating S3 table bucket"),
            }

    except Exception as e:
        logger.error(f"Error creating S3 table infrastructure: {e}")
        return {
            "overall_status": "error",
            "error_message": str(e),
        }


def determine_infrastructure_approval_requirement(pipeline_id: str, infrastructure_status: dict) -> bool:
    """Whether infrastructure creation needs human review — a **producer-less seam**.

    p64 R10.3, taking the second arm and following ``LGR-5d68eeb-3``'s treatment
    of ``CheckJobTimeoutConfig``: the gate keeps its reader, gains no producer,
    and the posture is documented here and pinned by test rather than wired up.
    So ``approval_config.infrastructure_approval_enabled`` (``:614``) is absent on
    every pipeline this repo can create, this function returns ``False`` for every
    execution, and the ``:616`` early return is the only branch taken in practice.

    **Nothing writes the key.** Enumerated rather than assumed — the three paths
    that write an ``approval_config`` onto a pipeline record are:

    * ``pipelines_crud._default_approval_config`` — seeds four keys
      (``schema_mapping_enabled``, ``schema_mapping_confidence_threshold``,
      ``code_generation_enabled``, ``always_approve_schema_changes``). Not this one.
    * ``pipelines_crud._submitted_approval_config`` — persists a submitted map
      **verbatim** (p64 task 2a.1), so it writes only what a client sends.
    * ``approval_config/lambda_function.update_pipeline_approval_config:65`` —
      requires ``schema_mapping_enabled`` / ``code_generation_enabled`` /
      ``first_run_enabled`` and writes verbatim. Not this one either.

    The wizard's ``buildApprovalConfigRequest`` emits exactly the same four keys
    as the seeded map — ``tests/contracts/test_approval_config_casing_cross_language.py``
    asserts that set equality — so no UI path sets it.

    **There is also nowhere for the answer to go, which is the stronger half.**
    The two governance Choices in ``orchestration-stack.ts`` read
    ``$.schema_mapping_result.approval_required`` (``:947-949``) and
    ``$.test_code_result.approval_required`` (``:1076-1078``). There is **no
    Choice on ``$.create_infrastructure_result.approval_required``** — this step's
    result path (``:788``) is written and never branched on. So a producer alone
    would not route anything to a human; it would only change this Lambda's own
    control flow.

    **What that change would be, and why it is a regression rather than a
    feature.** ``:937`` is ``if overall_status != "success" and not
    approval_required: raise`` — a deliberate fail-loud installed because the
    handler used to fall through to ``statusCode=200`` with an empty
    ``s3_table_bucket``, so a real failure surfaced two steps later as a
    misleading "Glue job name not found" at ``RunPipeline``. A truthy
    ``infrastructure_approval_enabled`` inverts exactly that guard: it makes
    ``approval_required`` ``True`` on error, skips the raise, and returns 200 for a
    failed provisioning — with no Choice downstream to catch it. So "give it a
    producer" is a **product decision about where infrastructure review happens**,
    not wiring, which is why it is not taken here.

    **Residual, stated rather than papered over: the seam is reachable by
    hand-craft, not unreachable by construction.** Because
    ``_submitted_approval_config`` persists verbatim and
    ``_validate_approval_config`` deliberately does not filter unrecognised keys
    (``LGR-c88d326-4`` — deployment drift is measured here, so a newer client must
    stay compatible with an older Lambda), a hand-written
    ``POST /pipelines/provision`` carrying ``{"infrastructure_approval_enabled":
    true}`` **is** persisted today and does suppress the ``:937`` raise for that
    pipeline. Adding the key to that validator's gate-key tuple was considered and
    **rejected**: the value that does the damage is a real boolean ``true``, which
    any boolean check accepts, so validation would refuse only the harmless
    spellings while leaving the actual hazard open and implying the seam had been
    closed.

    **Do NOT conflate the stores. Four unrelated things share this vocabulary.**
    The map read at ``:613`` is the ``approval_config`` **MAP on the pipelines
    table**. It is not any of: (1) ``$.pipeline_config`` — a path in Step Functions
    **execution state**, read by ``CheckJobTimeoutConfig``, written only by
    ``ApplyDefaultJobTimeout``; (2) the JSON-**string attribute on a DynamoDB state
    row** read by ``approval_handler.get_pipeline_config``, itself producer-less;
    (3) the **map on the pipeline record** read for ``source_bucket`` by
    ``shared/rerun_contract.py:166``. The governance Choices cannot see this map at
    all. ``LGR-5d68eeb-3`` was written because that shared spelling is the trap.

    Revisit when a producer for ``infrastructure_approval_enabled`` is proposed —
    the prerequisite is the product question above (where infrastructure review
    happens, and what reviews a failed provisioning), plus a Choice that consumes
    ``$.create_infrastructure_result.approval_required``. Landing the key without
    both re-opens the swallowed-failure defect that ``:937`` exists to prevent.
    """
    try:
        # Get pipeline metadata to check approval settings
        pipeline_metadata = get_pipeline_metadata(pipeline_id)

        # Check if infrastructure approval is enabled for this pipeline
        approval_config = pipeline_metadata.get("approval_config", {})
        infrastructure_approval_enabled = approval_config.get("infrastructure_approval_enabled", False)

        if not infrastructure_approval_enabled:
            logger.info(f"Infrastructure approval disabled for pipeline {pipeline_id}")
            return False

        # Check infrastructure status
        overall_status = infrastructure_status.get("overall_status", "error")
        if overall_status == "error":
            logger.info("Infrastructure approval required due to errors")
            return True

        # For S3 table buckets, approval is generally not required unless explicitly configured
        logger.info("No infrastructure approval required - S3 table bucket deployment")
        return False

    except Exception as e:
        logger.error(f"Error determining infrastructure approval requirement: {e}")
        # Default to not requiring approval for S3 table buckets
        return False


def create_infrastructure_approval_payload(infrastructure_status: dict, pipeline_id: str) -> dict:
    """Create approval payload for infrastructure creation - simplified for S3 table buckets"""
    try:
        s3_table_bucket = infrastructure_status.get("s3_table_bucket", {})

        approval_payload = {
            "approval_type": "infrastructure",
            "infrastructure_status": infrastructure_status,
            "deployment_summary": {
                "resource_type": "S3 Table Bucket",
                "bucket_name": s3_table_bucket.get("bucket_name", ""),
                "table_name": s3_table_bucket.get("table_name", ""),
                "table_format": s3_table_bucket.get("table_format", "iceberg"),
            },
            "risk_assessment": {"cost_risk": "low", "complexity_risk": "low", "data_risk": "medium"},
            "requires_review": infrastructure_status.get("overall_status") != "success",
        }

        return approval_payload

    except Exception as e:
        logger.error(f"Error creating infrastructure approval payload: {e}")
        return {"approval_type": "infrastructure", "error": f"Failed to create approval payload: {e!s}"}


def check_s3_tables_bucket_exists(bucket_name: str) -> bool:
    """
    Check if S3 Tables bucket already exists.

    Args:
        bucket_name: Name of the S3 Tables bucket

    Returns:
        True if bucket exists, False otherwise
    """
    try:
        s3tables_client = boto3.client("s3tables")

        # List all table buckets and check if our bucket exists
        response = s3tables_client.list_table_buckets()

        for bucket in response.get("tableBuckets", []):
            if bucket.get("name") == bucket_name:
                logger.info(f"S3 Tables bucket '{bucket_name}' already exists")
                return True

        logger.info(f"S3 Tables bucket '{bucket_name}' does not exist")
        return False

    except Exception as e:
        logger.warning(f"Error checking S3 Tables bucket existence: {e}")
        return False


def should_create_infrastructure(target_config: dict[str, Any]) -> tuple:
    """
    Determine if infrastructure creation is needed based on target configuration.

    Args:
        target_config: Target configuration from pipelines table

    Returns:
        tuple: (should_create: bool, reason: str)
    """
    if not target_config:
        return False, "No target_config found"

    target_type = target_config.get("type", "").upper()

    # Infrastructure creation is needed for NEW S3 Table Buckets
    # Support both S3_TABLES and NEW_S3_TABLE_BUCKET for compatibility
    if target_type in ["NEW_S3_TABLE_BUCKET", "S3_TABLES"]:
        # Get bucket_name from top level or parameters
        bucket_name = target_config.get("bucket_name") or target_config.get("parameters", {}).get("table_bucket_name")
        if not bucket_name:
            logger.warning("No bucket_name or table_bucket_name specified in target_config")
            return False, "No bucket_name specified in target_config"

        # Always run infrastructure creation for S3 Tables
        # The creation functions are idempotent and will check/create bucket, namespace, and table
        logger.info(f"Infrastructure creation required: NEW_S3_TABLE_BUCKET '{bucket_name}'")
        logger.info("Will check and create bucket, namespace, and table as needed (idempotent)")
        return True, f"Ensuring S3 Table Bucket infrastructure exists: '{bucket_name}'"

    # Skip for existing S3 Table Buckets
    if target_type == "EXISTING_S3_TABLE_BUCKET":
        logger.info("Infrastructure creation skipped: EXISTING_S3_TABLE_BUCKET")
        return False, "Using existing S3 Table Bucket"

    # Skip for Glue Catalog tables (already exist)
    #
    # GLUE_CATALOG is a **map-to-existing-table** target, and this skip is the
    # decision, not an omission. Making this branch create the table was
    # considered and DECLINED on four grounds:
    #
    #   1. A catalog table is normally registered by whatever owns the data. This
    #      pipeline is a consumer of that registration, not its owner.
    #   2. Auto-creating one would force us to choose bucket, file format,
    #      partitioning and SerDe on the owner's behalf — four decisions with no
    #      defensible default, each of which silently diverges from what the
    #      owner's other readers expect.
    #   3. The per-pipeline Glue role is scoped to
    #      ``database/agentic_data_ingestion_*``
    #      (``create_glue_pipeline/iam_roles.py:255-262``), so anything we created
    #      would have to live under that prefix — which defeats the whole point of
    #      this target type, "write into my existing analytics database".
    #   4. It would overlap S3_TABLES and S3_PARQUET, which already create their
    #      destinations. A user who wants a destination created has two target
    #      types that do it.
    #
    # The cost of the decision is that GLUE_CATALOG has three preconditions no
    # other target type has, and they used to surface only as a mid-run Glue
    # AccessDenied. They are now checked up front, before a pipeline is created,
    # by the ``glue_catalog`` arm of
    # ``dashboard_api/handlers/resources.py::detect_schema`` — see the
    # ``GLUE_CATALOG target pre-flight`` block comment there for all three and for
    # the wizard copy that states them in the same words.
    if target_type == "GLUE_CATALOG":
        logger.info("Infrastructure creation skipped: GLUE_CATALOG")
        return False, "Using existing Glue Catalog table"

    # Skip for JDBC databases (already exist)
    if target_type == "JDBC":
        logger.info("Infrastructure creation skipped: JDBC")
        return False, "Using existing JDBC database connection"

    # Default: skip if type is unknown
    logger.warning(f"Unknown target type: {target_type}, skipping infrastructure creation")
    return False, f"Unknown target type: {target_type}"


@step_handler("create_infrastructure")
@step_task_token("create_infrastructure")
def lambda_handler(event, context):
    """
    Lambda function to provision S3 Tables infrastructure.
    Conditionally creates infrastructure based on target type.

    Args:
        event: Lambda event containing infrastructure requirements and configuration parameters
        context: Lambda context object

    Returns:
        dict: Created AWS resources and connection details, or skip status
    """
    # ── AgentCore Runtime proxy path ────────────────────────────────
    if os.environ.get("USE_AGENTCORE_RUNTIME") == "true":
        from agentcore_proxy import invoke_agentcore_runtime

        return invoke_agentcore_runtime(
            endpoint=os.environ["AGENTCORE_CREATE_INFRA_ENDPOINT"],
            payload=event,
            step_name="create_infrastructure",
        )

    try:
        import time as _time

        _metrics_start = _time.time()

        logger.info(f"Infrastructure creation event: {json.dumps(event)}")

        # Extract required data from event
        run_id = event.get("run_id")
        pipeline_id = event.get("pipeline_id")
        target_schema = event.get("target_schema", {})

        if not run_id or not pipeline_id:
            raise ValueError("run_id and pipeline_id are required")

        logger.append_keys(run_id=run_id, pipeline_id=pipeline_id, step_name=StepName.CREATE_INFRASTRUCTURE)

        # Read target_config from pipelines table
        logger.info(f"Reading target_config from pipelines table for pipeline: {pipeline_id}")
        pipeline_metadata = get_pipeline_metadata(pipeline_id)
        target_config = pipeline_metadata.get("target_config", {})

        # Fallback to event target_config if not in DynamoDB (for testing)
        if not target_config and event.get("target_config"):
            logger.info("Using target_config from event (testing mode)")
            target_config = event.get("target_config", {})

        # Load target schema from multiple sources if not provided in event
        if not target_schema or (
            not target_schema.get("fields") and not target_schema.get("properties") and not target_schema.get("items")
        ):
            # Try 1: Load from S3 (pipeline metadata has bucket/key)
            target_schema_bucket = pipeline_metadata.get("target_schema_bucket")
            target_schema_key = pipeline_metadata.get("target_schema_s3_key")

            if target_schema_bucket and target_schema_key:
                try:
                    logger.info(f"Loading target schema from s3://{target_schema_bucket}/{target_schema_key}")
                    schema_obj = s3_client.get_object(Bucket=target_schema_bucket, Key=target_schema_key)
                    target_schema = json.loads(schema_obj["Body"].read())
                    logger.info("Loaded target schema from S3")
                except Exception as e:
                    logger.error(f"Failed to load target schema from S3: {e}")

            # Try 2: Read from state table (schema_extract step stores it)
            if not target_schema or (
                not target_schema.get("fields")
                and not target_schema.get("properties")
                and not target_schema.get("items")
            ):
                try:
                    schema_response = pipeline_runs_table.get_item(
                        Key={"run_id": run_id, "step_name": "schema_extract"}
                    )
                    if "Item" in schema_response and schema_response["Item"].get("extracted_schema"):
                        target_schema = json.loads(schema_response["Item"]["extracted_schema"])
                        logger.info("Loaded target schema from state table (schema_extract step)")
                except Exception as e:
                    logger.warning(f"Could not load schema from state table: {e}")

        # Normalize JSON Schema format: extract properties from nested structure
        if not target_schema.get("fields"):
            props = target_schema.get("properties", {})
            if not props and target_schema.get("items", {}).get("properties"):
                props = target_schema["items"]["properties"]
            if props:
                target_schema["fields"] = [
                    {"name": name, "type": info.get("type", "string")} for name, info in props.items()
                ]
                logger.info(f"Normalized schema: {len(target_schema['fields'])} fields from JSON Schema properties")

        # Determine if infrastructure creation is needed
        should_create, reason = should_create_infrastructure(target_config)

        # Generate timestamp
        timestamp = datetime.now(UTC).isoformat()

        # If infrastructure creation is not needed, return early with skip status
        if not should_create:
            logger.info(f"Skipping infrastructure creation: {reason}")

            # Store skip status in pipeline runs table
            standardized_output = create_output_data(
                artifact_type="infrastructure",
                primary_id=run_id,
                status="skipped",
                summary={
                    "overall_status": "skipped",
                    "reason": reason,
                    "target_type": target_config.get("type", "unknown"),
                },
                s3_uris={},
            )
            # Keep legacy fields for backward compatibility
            standardized_output["infrastructure_status"] = {
                "overall_status": "skipped",
                "reason": reason,
            }

            pipeline_runs_table.put_item(
                Item={
                    "run_id": run_id,  # Partition key
                    "step_name": StepName.CREATE_INFRASTRUCTURE,
                    "status": "skipped",
                    "step_sequence": STEP_SEQUENCE[StepName.CREATE_INFRASTRUCTURE],
                    "timestamp": timestamp,
                    "pipeline_id": pipeline_id,  # For PipelineIndex GSI
                    "original_pipeline_id": pipeline_id,
                    "skip_reason": reason,
                    "target_type": target_config.get("type", "unknown"),
                    "output_data": standardized_output,
                }
            )

            # No stage execution is published for a skip. It used to record
            # `CreateInfrastructure` with status "completed" and the ~0.1 s it
            # took to decide, so the Metrics Dashboard's Infrastructure card
            # showed an average time beside zero successes and zero errors (the
            # state row says "skipped", which counts as neither), and the Avg
            # Agent Stage Duration tile averaged those near-zero skips in with
            # real agent work. Nothing was provisioned, so there is no stage
            # duration to report: the card reads N/A, which is what happened.

            return {
                "statusCode": 200,
                "run_id": run_id,
                "pipeline_id": pipeline_id,
                "infrastructure_status": {"overall_status": "skipped", "reason": reason},
                "status": "skipped",
                "skip_reason": reason,
                "target_config": target_config,
                "approval_required": False,
                "next_step": "create_glue_pipeline",
            }

        # Validate that we have a target schema for S3 table bucket creation
        if not target_schema or not target_schema.get("fields"):
            raise ValueError("target_schema with fields is required for S3 table bucket creation")

        # Create S3 table infrastructure
        logger.info(f"Creating S3 table infrastructure for pipeline: {pipeline_id}")
        infrastructure_status = create_s3_table_infrastructure(pipeline_id, target_schema)

        # Generate timestamps
        timestamp = datetime.now(UTC).isoformat()

        # Determine if infrastructure approval is required
        approval_required = determine_infrastructure_approval_requirement(pipeline_id, infrastructure_status)

        # Create approval payload
        approval_payload = create_infrastructure_approval_payload(infrastructure_status, pipeline_id)

        # Store infrastructure creation result in pipeline runs table
        overall_status = infrastructure_status.get("overall_status", "error")
        s3_table_bucket = infrastructure_status.get("s3_table_bucket", {})

        # Fail loudly when infrastructure creation errored and no human-approval
        # gate will review it. Previously the handler fell through to the
        # statusCode=200 return below with an empty s3_table_bucket, so the failure
        # was swallowed and surfaced two steps later as a misleading "Glue job name
        # not found" at RunPipeline. Raising routes the Step Functions execution to
        # the CreateInfrastructure Catch with the real cause (e.g. the S3 Tables
        # CreateTableBucket account-quota error).
        if overall_status != "success" and not approval_required:
            raise RuntimeError(
                f"Infrastructure creation failed for pipeline {pipeline_id}: "
                f"{infrastructure_status.get('error_message', 'Unknown error creating S3 Tables infrastructure')}"
            )

        standardized_output = create_output_data(
            artifact_type="infrastructure",
            primary_id=run_id,
            status="success" if overall_status == "success" else "failed",
            summary={
                "overall_status": overall_status,
                "resources_created": infrastructure_status.get("resources_created", []),
                "bucket_name": s3_table_bucket.get("bucket_name", ""),
                "table_name": s3_table_bucket.get("table_name", ""),
                "namespace": s3_table_bucket.get("namespace", ""),
                "table_format": s3_table_bucket.get("table_format", ""),
            },
            s3_uris={},
        )
        # Keep legacy fields for backward compatibility
        standardized_output["infrastructure_status"] = infrastructure_status
        standardized_output["s3_table_bucket"] = s3_table_bucket
        # Store approval fields as a native dict inside output_data (not a JSON string)
        standardized_output["approval"] = approval_payload

        item_data = {
            "run_id": run_id,  # Partition key
            "step_name": StepName.CREATE_INFRASTRUCTURE,
            "status": "success",
            "step_sequence": STEP_SEQUENCE[StepName.CREATE_INFRASTRUCTURE],
            "timestamp": timestamp,
            "pipeline_id": pipeline_id,  # For PipelineIndex GSI
            "original_pipeline_id": pipeline_id,
            "approval_required": approval_required,
            "output_data": standardized_output,
        }

        # Convert floats to Decimals before storing in DynamoDB
        pipeline_runs_table.put_item(Item=convert_floats_to_decimal(item_data))
        logger.info(f"Infrastructure creation state stored in {PIPELINE_RUNS_TABLE}")

        # Return output with S3 table bucket details for subsequent steps
        s3_table_bucket = infrastructure_status.get("s3_table_bucket", {})

        # Publish stage metrics
        if PipelineMetrics:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="CreateInfrastructure",
                    pipeline_id=pipeline_id or "unknown",
                    status="completed",
                    duration_ms=_duration_ms,
                )
            except Exception as exc:
                # Non-fatal: stage metrics are best-effort telemetry; a CloudWatch
                # publish failure must not fail a successful infrastructure run (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish CreateInfrastructure (completed) stage metrics for "
                    f"pipeline_id={pipeline_id} run_id={run_id}: {type(exc).__name__}: {exc}"
                )

        return {
            "statusCode": 200,
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "infrastructure_status": infrastructure_status,
            "s3_table_bucket": s3_table_bucket,
            "table_location": s3_table_bucket.get("data_location", ""),
            "bucket_name": s3_table_bucket.get("bucket_name", ""),
            "table_name": s3_table_bucket.get("table_name", ""),
            "table_bucket_arn": s3_table_bucket.get("table_bucket_arn", ""),
            "table_arn": s3_table_bucket.get("table_arn", ""),
            "namespace": s3_table_bucket.get("namespace", ""),
            "metadata_location": s3_table_bucket.get("metadata_location", ""),
            "warehouse_location": s3_table_bucket.get("warehouse_location", ""),
            "approval_required": approval_required,
            "next_step": "schema_mapping",
        }

    except Exception as e:
        error_message = f"Error in infrastructure creation: {e!s}"
        logger.error(error_message)

        if PipelineMetrics:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="CreateInfrastructure",
                    pipeline_id=event.get("pipeline_id", "unknown"),
                    status="failed",
                    duration_ms=_duration_ms,
                )
            except Exception as exc:
                # Non-fatal: failure-path stage metrics are best-effort telemetry; the
                # original error below is still recorded and returned (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish CreateInfrastructure (failed) stage metrics for "
                    f"pipeline_id={event.get('pipeline_id', 'unknown')} "
                    f"run_id={event.get('run_id', 'unknown')}: {type(exc).__name__}: {exc}"
                )

        # Write error to pipeline runs table
        try:
            run_id = event.get("run_id", "unknown")
            pipeline_runs_table.put_item(
                Item={
                    "run_id": run_id,  # Partition key
                    "step_name": StepName.CREATE_INFRASTRUCTURE,
                    "status": "failed",
                    "step_sequence": STEP_SEQUENCE[StepName.CREATE_INFRASTRUCTURE],
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pipeline_id": event.get("pipeline_id", "unknown"),  # For PipelineIndex GSI
                    "original_pipeline_id": event.get("pipeline_id", "unknown"),
                    "error_message": error_message,
                    "error_type": "InfrastructureError",
                }
            )
        except Exception as db_error:
            logger.error(f"Failed to write error to DynamoDB: {db_error!s}")

        # P28 R1.1: raise StepFailure so the failure propagates to the Step Functions
        # CreateInfrastructure task's Catch. SFN uses payloadResponseOnly and ignores a
        # returned statusCode, so a 500 return body would be treated as success and
        # swallow the failure.
        raise StepFailure(
            "create_infrastructure",
            error_message,
            details={
                "run_id": event.get("run_id", "unknown"),
                "pipeline_id": event.get("pipeline_id", "unknown"),
            },
        ) from e
