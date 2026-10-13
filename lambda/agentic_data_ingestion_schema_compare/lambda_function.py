# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from artifact_schema import create_output_data

# Typed accessors for DynamoDB item values (P22 Req 2)
from dynamo_typing import item_str
from env_utils import require_env

# One definition of "approved for reuse", shared with create_code and
# schema_mapping (LGR-5d68eeb-2).
from mapping_reuse import approved_for_reuse_condition
from step_errors import StepFailure, raise_for_read_client_error, step_handler
from step_names import STEP_SEQUENCE, StepName

try:
    from metrics_facade import PipelineMetrics
except ImportError:
    PipelineMetrics = None  # type: ignore[misc,assignment]  # optional dep; guarded by `is not None` at use sites

# Initialize logger
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# Environment variables
DYNAMODB_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
PIPELINES_TABLE = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")

# Initialize AWS clients
s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE)
pipelines_table = dynamodb.Table(PIPELINES_TABLE)


# P30 R8.3: page bound for the previous-schema GSI walk, mirroring
# `create_code/mapping_resolution.py::_MAPPING_LOOKUP_MAX_PAGES` (same table, same
# `PipelineIndex` GSI). `Limit` is NOT usable as the bound: DynamoDB applies it
# BEFORE `FilterExpression`, so a small `Limit` returns zero `schema_extract` rows
# even when they exist — that was the sibling's old `Limit=5` bug. Descending order
# from the current run's timestamp is what actually bounds the walk (see
# `get_previous_schema`); this cap only stops a pathological partition from
# becoming an unbounded read. Each run writes ~8 step rows and a page holds up to
# 1MB, so 10 pages covers thousands of runs per pipeline.
_PREVIOUS_SCHEMA_MAX_PAGES = 10


def get_previous_schema(original_pipeline_id, current_run_id):
    """Previous successful schema extraction for this pipeline, relative to THIS run.

    R8.3: the previous schema is the newest successful ``schema_extract`` row whose
    ``timestamp`` is strictly older than **the current run's own row** — never "the
    second most recent row in the partition".

    *Why the signature takes the run id.* The old implementation sorted the whole
    partition descending and returned ``items[1]``, which is the previous schema
    only when the current run's row is ``items[0]``. When another run's
    ``schema_extract`` row is newer — a concurrently started run, a retry, or clock
    skew — ``items[1]`` **is** the current run's own row, so the run compared its
    schema against itself, reported identical, and skipped the remap. Selecting
    "relative to this run" is impossible without knowing which run is current, so
    the run id is a required parameter. The current run's row is re-read here
    rather than passed in, which keeps the comparison anchor un-spoofable: the
    function cannot be handed a row belonging to a different run.

    *Bounding the walk.* ``timestamp`` is the ``PipelineIndex`` sort key, so the
    previous schema is by construction the nearest match *below* this run's
    timestamp. Querying descending from that upper bound lets the first surviving
    row win and stops the walk there — the read spans only the rows between this
    run and the immediately-previous schema, not the pipeline's whole history,
    which the old unbounded ascending query had to read in full before it could
    sort. Pages before that first match can still come back empty: DynamoDB
    applies ``FilterExpression`` AFTER the 1MB page read and the state table holds
    a row per step per run, so an empty page arrives with a ``LastEvaluatedKey``
    and the walk must resume on it. Treating that empty page as "no previous
    schema" was the second half of the R8.3 defect.

    Returns None when this is the pipeline's first schema.
    """
    try:
        # Anchor the comparison on this run's OWN schema_extract row.
        current_row = get_current_schema(current_run_id)
        current_timestamp = item_str(current_row, "timestamp") if current_row else ""
        if not current_timestamp:
            # No usable row for this run, so nothing is "previous" relative to it.
            # `lambda_handler` raises on a missing current schema before reaching
            # here; returning None routes any other caller to the first-run branch,
            # which requires approval before the target is written.
            logger.warning(
                f"No timestamped schema_extract row for run {current_run_id} — "
                "cannot select a previous schema by timestamp comparison"
            )
            return None

        last_key = None
        for _ in range(_PREVIOUS_SCHEMA_MAX_PAGES):
            # Use PipelineIndex GSI to query by pipeline_id directly (avoids full table scan)
            kwargs: dict[str, Any] = {
                "IndexName": "PipelineIndex",
                # R8.3: the timestamp comparison against the current run's own row
                # is server-side and part of the KEY condition, so the current
                # run's row can never be a candidate.
                "KeyConditionExpression": (
                    Key("pipeline_id").eq(original_pipeline_id) & Key("timestamp").lt(current_timestamp)
                ),
                "FilterExpression": Attr("step_name").eq("schema_extract") & Attr("status").eq("success"),
                # Newest-first, so the first surviving row IS the previous schema.
                "ScanIndexForward": False,
            }
            if last_key:
                kwargs["ExclusiveStartKey"] = last_key

            response = table.query(**kwargs)

            for item in response.get("Items", []):
                # Defence in depth for a duplicate timestamp: a run must never be
                # handed its own row as its own predecessor.
                if item_str(item, "run_id") != current_run_id:
                    return item

            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                # Partition genuinely exhausted: this is the pipeline's first schema.
                return None

        logger.warning(
            f"Previous-schema walk for pipeline {original_pipeline_id} hit the "
            f"{_PREVIOUS_SCHEMA_MAX_PAGES}-page cap without finding a schema_extract row"
        )
        return None

    except Exception as e:
        logger.error(f"Error getting previous schema: {e}")
        raise


def get_current_schema(run_id):
    """Get the current schema extraction result from this run"""
    try:
        # Get step 2 (schema extraction) from this run — direct base table lookup
        response = table.get_item(Key={"run_id": run_id, "step_name": "schema_extract"})

        if "Item" in response:
            return response["Item"]
        return None

    except Exception as e:
        logger.error(f"Error getting current schema: {e}")
        raise


# ---------------------------------------------------------------------------
# P30 R8.1 / R8.2 — a change to a *declared* `type` value is a breaking change.
#
# `deep_compare` walks two JSON Schema documents produced by `schema_extract`.
# Its only `type_mismatch` branch is `type(obj1) != type(obj2)`, a *Python*
# type check that two JSON strings never trip — so changing a field's declared
# type from "integer" to "string" used to land as `value_changed` and left
# `is_compatible` true. A breaking source change adapted itself into the target
# table with no human decision (R8.1).
#
# Which `type` keys are declarations must be decided DURING the walk, never by
# parsing the joined `path` afterwards. `deep_compare` dot-joins path segments,
# so a field named `a.b` — a legal CSV header — emits `properties.a.b.type`,
# indistinguishable from a nested `a` containing `b`; and a field named
# `x.type` emits `properties.x.type.type`, which no string parse can resolve
# (p30 task 0.3, cases C and D). `path` itself is unchanged: it is for display.
#
# Positional predicate (p30 task 0.3). A *schema node* is:
#   - the root document;
#   - `P["properties"][k]` for ANY key `k`, where `P` is a schema node;
#   - `P["items"]`, where `P` is a schema node.
# The key `type` is a declaration iff its parent is a schema node AND its value
# is a string. Everything else is excluded structurally, with no dependence on
# field names — notably the jdbc path's root `required` array, whose *values
# are field names*, so a column reorder on a table with columns named
# "integer"/"string" stays `value_changed` rather than blocking the pipeline
# (0.3 case B). A field literally named `type` is handled correctly too: its
# own `type` key is a genuine declaration, while adding or removing the field
# stays classified on the field (0.3 case A) because only `value_changed`
# positions are reclassified here.
# ---------------------------------------------------------------------------

#: Roles threaded through `deep_compare`'s recursion to implement the predicate.
_ROLE_SCHEMA = "schema"  # this dict is a schema node
_ROLE_PROPERTIES = "properties"  # a `properties` map: every value is a schema node
_ROLE_DECLARED_TYPE = "declared_type"  # the value of a schema node's `type` key
_ROLE_OTHER = "other"  # anything else (`required` elements, unknown branches)

#: Declared-type transitions that are widenings rather than breaking changes:
#: they stay `value_changed` and leave `is_compatible` true.
#:
#: WIDENING DECISION (R8 does not settle this; p30 task 0.3 asked task 4.4 to
#: make the call explicitly rather than leave it implicit). Task 0.3 found one
#: way an *unchanged* pipeline can report a different declared type between two
#: runs of the same data: `infer_schema_from_records` samples 100 records and
#: resolves {integer, number} -> number, so identical data can read `integer`
#: on one run and `number` on the next. Blocking on that would make an
#: unchanged pipeline non-deterministic, and it cannot break the target — a
#: `number` column holds every integer. So it is allowed.
#:
#: This mirrors the repo's existing widening convention,
#: `create_glue_pipeline/schema_evolution.py::is_type_change_compatible`, which
#: allows int -> double/decimal (the SQL-vocabulary analogue of
#: integer -> number) and does not allow integer -> string.
#:
#: Everything else stays BLOCKING, deliberately — including the format-switch
#: divergences 0.3 flagged: `string` <-> `integer` on a .csv -> .parquet
#: re-upload of identical data, and `array`/`object` <-> `string` between the
#: parquet and jdbc readers. Those are deterministic given the input, they do
#: change the physical type the target column must hold, and R8's whole point is
#: that such a change gets a human decision. The consequence is an approval
#: prompt on a format switch, not a failed run.
_COMPATIBLE_TYPE_WIDENINGS: frozenset[tuple[str, str]] = frozenset(
    {
        ("integer", "number"),
    }
)


def _child_role(role: str, key: str) -> str:
    """Role of ``parent[key]`` given the parent's role. See the predicate above."""
    if role == _ROLE_PROPERTIES:
        # Every value of a `properties` map is a schema node, whatever the field
        # is called — including a field literally named `type` or `items`.
        return _ROLE_SCHEMA
    if role == _ROLE_SCHEMA:
        if key == "properties":
            return _ROLE_PROPERTIES
        if key == "items":
            return _ROLE_SCHEMA
        if key == "type":
            return _ROLE_DECLARED_TYPE
    return _ROLE_OTHER


def _is_breaking_type_change(old_type: str, new_type: str) -> bool:
    """True when a change to a declared ``type`` must block (R8.1 / R8.2)."""
    if old_type == new_type:
        return False
    # `null` is the extractors' "no values observed" token rather than a type
    # claim: the legacy reader emits it for an all-null column where the
    # streaming reader emits `string` for the same input (task 0.3). A
    # transition involving it therefore carries no information about a type
    # change, and nulls are writable into any nullable target column.
    if "null" in (old_type, new_type):
        return False
    return (old_type, new_type) not in _COMPATIBLE_TYPE_WIDENINGS


def compare_schemas(schema1_str, schema2_str):
    """Compare two JSON schemas and return comparison results.

    A change to a field's declared ``type`` value is reported as
    ``type_mismatch`` (R8.1), which makes ``is_compatible`` false (R8.2).
    Widenings listed in :data:`_COMPATIBLE_TYPE_WIDENINGS` stay
    ``value_changed`` and remain compatible.
    """
    try:
        schema1 = json.loads(schema1_str) if isinstance(schema1_str, str) else schema1_str
        schema2 = json.loads(schema2_str) if isinstance(schema2_str, str) else schema2_str

        def deep_compare(obj1, obj2, path="", role=_ROLE_SCHEMA):
            differences = []

            if type(obj1) != type(obj2):
                differences.append(
                    {
                        "path": path,
                        "type": "type_mismatch",
                        "old_type": str(type(obj1).__name__),
                        "new_type": str(type(obj2).__name__),
                    }
                )
                return differences

            if isinstance(obj1, dict) and isinstance(obj2, dict):
                # Check for added fields
                for key in obj2:
                    if key not in obj1:
                        differences.append(
                            {"path": f"{path}.{key}" if path else key, "type": "field_added", "field": key}
                        )

                # Check for removed fields
                for key in obj1:
                    if key not in obj2:
                        differences.append(
                            {"path": f"{path}.{key}" if path else key, "type": "field_removed", "field": key}
                        )

                # Check for modified fields
                for key in obj1:
                    if key in obj2:
                        new_path = f"{path}.{key}" if path else key
                        differences.extend(deep_compare(obj1[key], obj2[key], new_path, _child_role(role, key)))

            elif isinstance(obj1, list) and isinstance(obj2, list):
                # For arrays, compare the item types if they exist. List
                # elements are never schema nodes: the only array a schema node
                # carries is the jdbc path's root `required`, whose values are
                # field NAMES (task 0.3 case B). A `type` spelled as a list
                # (`{"type": ["string", "null"]}`) also lands here, and is not a
                # single declared value, so the predicate excludes it.
                if len(obj1) > 0 and len(obj2) > 0:
                    differences.extend(deep_compare(obj1[0], obj2[0], f"{path}[0]", _ROLE_OTHER))

            elif obj1 != obj2:
                if (
                    role == _ROLE_DECLARED_TYPE
                    and isinstance(obj1, str)
                    and isinstance(obj2, str)
                    and _is_breaking_type_change(obj1, obj2)
                ):
                    # R8.1: the declared type changed and the change is not a
                    # widening. Same diff shape as the Python-type branch above
                    # so `type_mismatch` has one contract for consumers.
                    differences.append({"path": path, "type": "type_mismatch", "old_type": obj1, "new_type": obj2})
                else:
                    differences.append({"path": path, "type": "value_changed", "old_value": obj1, "new_value": obj2})

            return differences

        differences = deep_compare(schema1, schema2)

        return {
            "is_identical": len(differences) == 0,
            "is_compatible": len([d for d in differences if d["type"] in ["field_removed", "type_mismatch"]]) == 0,
            "differences": differences,
            "summary": {
                "fields_added": len([d for d in differences if d["type"] == "field_added"]),
                "fields_removed": len([d for d in differences if d["type"] == "field_removed"]),
                "type_mismatches": len([d for d in differences if d["type"] == "type_mismatch"]),
                "value_changes": len([d for d in differences if d["type"] == "value_changed"]),
            },
        }

    except Exception as e:
        logger.error(f"Error comparing schemas: {e}")
        raise


def get_pipeline_metadata(original_pipeline_id: str) -> dict | None:
    """Get pipeline metadata including target schema information.

    Not-found returns None (the caller raises its clear error).
    Infrastructure errors (throttles, access denied) raise instead of
    masquerading as not-found (P28 R17.3).
    """
    try:
        response = pipelines_table.get_item(Key={"pipeline_id": original_pipeline_id})
    except ClientError as e:
        raise_for_read_client_error(e, "schema_compare", operation="get_pipeline_metadata")
        logger.error(f"Pipelines table not found getting metadata for {original_pipeline_id}: {e}")
        return None
    if "Item" in response:
        return response["Item"]
    return None


def get_target_schema_from_s3(bucket: str, key: str) -> tuple[dict | None, str | None]:
    """Get target schema from S3 and return schema + hash.

    A genuinely-missing object (NoSuchKey/NoSuchBucket) returns (None, None).
    Infrastructure errors (throttles, access denied) raise so an S3 throttle
    can never register as a false schema-version change (P28 R17.3).
    """
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read().decode("utf-8")
    except ClientError as e:
        raise_for_read_client_error(e, "schema_compare", operation="get_target_schema_from_s3")
        logger.error(f"Target schema object not found at s3://{bucket}/{key}: {e}")
        return None, None
    schema = json.loads(content)
    schema_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return schema, schema_hash


def check_target_schema_changed(pipeline_metadata: dict) -> tuple[bool, str | None, str | None]:
    """Check if target schema has changed since pipeline creation.

    **The bucket is resolved here, from the same record as the key.** This
    function used to take the bucket as a parameter and ``lambda_handler``
    supplied ``pipeline_metadata["generated_code_bucket"]`` — an attribute
    ``create_pipeline`` has never written (``pipelines_crud.py`` writes
    ``target_schema_bucket`` and ``target_schema_s3_key`` together, from the
    single ``s3://`` upload location, and nothing else writes a bucket onto a
    pipeline record). ``item_str`` returns ``""`` for the absent attribute, so
    the run reached ``s3_client.get_object(Bucket="", ...)`` and died in
    botocore parameter validation. The pair is now read where the pair is
    used, so the bucket and the key cannot be sourced from different
    attributes again — which is the only reason the divergence was possible.

    Returns ``(changed, stored_version, observed_version)``. ``observed_version``
    is the version this run actually read from S3 — it is reported whether or
    not the version changed, and whether or not a baseline exists yet, because
    it is the value the run's successful completion commits (R8.4, see below).

    **This function does not write (R8.4).** It used to be the caller's
    ``lambda_handler`` that advanced ``target_schema_version`` in the pipelines
    table the moment a change was detected — at step 3 of 9, with the Glue write
    still six steps away. If the run then failed, the pipelines table claimed a
    target-schema version that had never been mapped against or written to, so
    the *next* run compared against a version that did not reflect reality,
    found no change, and skipped the remap the failure should have left
    outstanding. The drift became invisible. The commit now happens in
    ``job_completion`` once the Glue job reports SUCCEEDED
    (``_commit_target_schema_version``); a failure leaves the stored version
    behind and the remap re-triggerable, which is R8.4's stated purpose.

    *Why the missing-baseline branch reads S3 anyway.* Nothing initialises
    ``target_schema_version``: ``create_pipeline`` persists
    ``target_schema_bucket`` + ``target_schema_s3_key`` and no version
    (``pipelines_crud.py:737-740``), and the old writer sat *inside* the
    changed-branch, which is unreachable without a stored version. So the
    version could never be established and target-schema drift could never
    fire at all. Returning the observed version here lets the first successful
    run establish the baseline. ``changed`` stays False in that case
    deliberately: with no baseline there is nothing to have changed *from*, and
    reporting True would route every pipeline's first run to ``remap_required``.

    Read errors propagate (P28 R17.3) — the old broad swallow returned
    "not changed" for a throttle, silently skipping the remap decision.
    """
    target_schema_key = pipeline_metadata.get("target_schema_s3_key")
    target_schema_bucket = pipeline_metadata.get("target_schema_bucket")
    stored_version = pipeline_metadata.get("target_schema_version")

    if not target_schema_key:
        # No target schema object at all — nothing to version.
        logger.warning("Pipeline missing target schema metadata")
        return False, None, None

    if not target_schema_bucket:
        # A key with no bucket is a half-written pipeline record, not an absent
        # target schema: `create_pipeline` writes both or neither. Failing here
        # names the missing attribute and the key it was meant to accompany;
        # returning "not changed" instead would make target-schema drift
        # silently undetectable for this pipeline, which is precisely the
        # invisibility R8.4 exists to prevent.
        raise ValueError(
            "Pipeline record has 'target_schema_s3_key' "
            f"({target_schema_key!r}) but no 'target_schema_bucket' — cannot "
            "read the target schema. The pipelines-table record is incomplete."
        )

    # Get current target schema version
    _, current_version = get_target_schema_from_s3(target_schema_bucket, target_schema_key)

    if not stored_version:
        # First run for this pipeline (or the first since R8.4): no baseline to
        # compare against. Report the observed version so a successful run can
        # commit it as the baseline; do not report a change.
        logger.info(f"No stored target schema version yet - observed {current_version}, baseline set on success")
        return False, None, current_version

    if current_version != stored_version:
        logger.info(f"Target schema changed: {stored_version} -> {current_version}")
        return True, stored_version, current_version

    return False, stored_version, current_version


def store_comparison_result(
    run_id,
    original_pipeline_id,
    comparison_result,
    current_schema_hash,
    previous_schema_hash=None,
    execution_arn=None,
    target_schema_changed=False,
    target_schema_old_version=None,
    target_schema_new_version=None,
):
    """Store schema comparison result in DynamoDB using new run_id + sequence structure"""
    try:
        timestamp = datetime.now(UTC).isoformat()

        # Use run_id as PK directly
        pipeline_id = original_pipeline_id  # For PipelineIndex GSI

        item = {
            "run_id": run_id,  # Partition key
            "step_sequence": 3,  # Step number in pipeline
            "step_name": "schema_compare",
            "status": "success",
            "timestamp": timestamp,
            "source_schema_hash": current_schema_hash,
            "comparison_result": "schema_changed" if not comparison_result["is_identical"] else "schema_unchanged",
            "schema_differences": json.dumps(comparison_result.get("differences", [])),
            "original_pipeline_id": original_pipeline_id,  # Groups all runs for this pipeline
            "pipeline_id": pipeline_id,  # For PipelineIndex GSI
            # NEW: Target schema change tracking
            "target_schema_changed": target_schema_changed,
            "change_trigger": "target_schema"
            if target_schema_changed
            else ("source_schema" if not comparison_result["is_identical"] else "none"),
        }

        if previous_schema_hash:
            item["previous_schema_hash"] = previous_schema_hash

        if target_schema_old_version:
            item["target_schema_old_version"] = target_schema_old_version

        if target_schema_new_version:
            item["target_schema_new_version"] = target_schema_new_version

        if execution_arn:
            item["execution_id"] = execution_arn

        # Standardized output_data for unified artifact storage
        differences = comparison_result.get("differences", [])
        comp_result_str = "schema_changed" if not comparison_result["is_identical"] else "schema_unchanged"
        item["output_data"] = create_output_data(
            artifact_type="schema_comparison",
            primary_id=run_id,
            status="success",
            summary={
                "comparison_result": comp_result_str,
                "target_schema_changed": target_schema_changed,
                "differences_count": len(differences),
            },
            s3_uris={},
        )

        table.put_item(Item=item)
        logger.info(f"Schema comparison result stored - run_id: {run_id}, pipeline_id: {pipeline_id}")

    except Exception as e:
        logger.error(f"Error storing comparison result: {e}")
        raise


@step_handler("schema_compare")
def lambda_handler(event, context):
    """
    Lambda function to compare current schema against previously stored schema.

    Args:
        event: Step Functions event containing pipeline information
        context: Lambda context object

    Returns:
        dict: Schema comparison results and next action decision
    """
    try:
        import time as _time

        _metrics_start = _time.time()

        logger.info(f"Schema comparison event: {json.dumps(event)}")

        # Get run_id and execution info from event
        run_id = event.get("run_id")
        execution_arn = event.get("execution_arn")
        if not run_id:
            raise ValueError("Run ID not provided in event")

        logger.append_keys(run_id=run_id, pipeline_id=event.get("original_pipeline_id", ""), step_name="SchemaCompare")

        # Get current schema (just extracted)
        current_schema_data = get_current_schema(run_id)
        if not current_schema_data:
            raise ValueError(f"No current schema found for run_id: {run_id}")

        current_schema = current_schema_data["extracted_schema"]
        current_schema_hash = current_schema_data["schema_hash"]
        original_pipeline_id = current_schema_data["original_pipeline_id"]

        # NEW: Get pipeline metadata and check target schema
        pipeline_metadata = get_pipeline_metadata(original_pipeline_id)
        if not pipeline_metadata:
            raise ValueError(f"Pipeline metadata not found for: {original_pipeline_id}")

        # The target-schema bucket is resolved inside `check_target_schema_changed`
        # from the same pipeline record as the key — see its docstring for why it
        # is no longer passed in from here.
        target_schema_changed, target_old_version, target_new_version = check_target_schema_changed(pipeline_metadata)

        # Get previous schema for comparison, relative to THIS run's own row (R8.3)
        previous_schema_data = get_previous_schema(original_pipeline_id, run_id)

        if not previous_schema_data:
            # This is the first file for this pipeline - no comparison needed
            logger.info(f"First schema for pipeline {original_pipeline_id} - no comparison needed")

            # NEW: Check if target schema changed even on first run
            action = "create_new_pipeline"
            if target_schema_changed:
                action = "remap_required"
                logger.info("Target schema changed on first run - remapping required")

            comparison_result: dict[str, Any] = {
                "is_identical": False,
                "is_compatible": True,
                "differences": [],
                "summary": {"first_schema": True, "target_schema_changed": target_schema_changed},
                "action": action,
            }

            store_comparison_result(
                run_id,
                original_pipeline_id,
                comparison_result,
                current_schema_hash,
                execution_arn=execution_arn,
                target_schema_changed=target_schema_changed,
                target_schema_old_version=target_old_version,
                target_schema_new_version=target_new_version,
            )

            # Publish stage metrics
            if PipelineMetrics is not None:
                try:
                    _duration_ms = (_time.time() - _metrics_start) * 1000
                    PipelineMetrics.record_stage_execution(
                        stage_name="SchemaCompare",
                        pipeline_id=original_pipeline_id or "unknown",
                        status="completed",
                        duration_ms=_duration_ms,
                    )
                except Exception:
                    pass  # Non-fatal

            return {
                "statusCode": 200,
                "run_id": run_id,
                "original_pipeline_id": original_pipeline_id,
                "schema_hash": current_schema_hash,
                "action": action,
                "comparison_result": comparison_result,
                "target_schema_changed": target_schema_changed,
                "message": f"First schema detected - {action}",
            }

        # Compare schemas
        previous_schema = previous_schema_data["extracted_schema"]
        previous_schema_hash = previous_schema_data["schema_hash"]

        logger.info(f"Comparing current schema hash {current_schema_hash} with previous {previous_schema_hash}")

        # NEW: Check if target schema changed - this triggers remapping regardless of source schema
        if target_schema_changed:
            logger.info("Target schema changed - remapping required regardless of source schema changes")

            # Still compare source schemas for audit trail
            if current_schema_hash == previous_schema_hash:
                comparison_result = {
                    "is_identical": True,
                    "is_compatible": True,
                    "differences": [],
                    "summary": {"identical_schemas": True, "target_schema_changed": True},
                    "action": "remap_required",
                }
            else:
                comparison_result = compare_schemas(previous_schema, current_schema)
                comparison_result["action"] = "remap_required"
                comparison_result["summary"]["target_schema_changed"] = True

            # R8.4: the pipelines-table `target_schema_version` is NOT advanced
            # here. The observed version is recorded on this run's
            # schema_compare state row instead (`target_schema_new_version`, via
            # `store_comparison_result`), and `job_completion` commits it to the
            # pipelines table once the Glue job reports SUCCEEDED. Advancing it
            # at this point told the next run the drift had been handled while
            # the run that was supposed to handle it could still fail six steps
            # later. See `check_target_schema_changed`'s docstring.

        elif current_schema_hash == previous_schema_hash:
            # Schemas are identical - use existing pipeline
            comparison_result = {
                "is_identical": True,
                "is_compatible": True,
                "differences": [],
                "summary": {"identical_schemas": True, "target_schema_changed": False},
                "action": "use_existing_pipeline",
            }

            # Look up the most recent APPROVED mapping_id for this pipeline so we can
            # skip remapping.
            #
            # p47 C2: `status == "success"` is load-bearing, not defensive. Without it
            # this query selected the newest `schema_mapping` row carrying a
            # `mapping_id` whatever its approval status, so a mapping a human had
            # REJECTED was a valid reuse candidate — and the schema-mapping approval
            # gate (p47 R4 Arm B, invariant `LGR-69ca78b-1`) was the only thing left
            # standing between it and use. Worse in one respect: because the newest
            # row won regardless, a pipeline whose latest mapping was rejected lost
            # access to its last approved one.
            #
            # The predicate is not invented here. It is `approved_for_reuse_condition()`
            # from `shared/mapping_reuse.py`, which the codegen path
            # (`create_code/mapping_resolution.py::_query_approved_schema_mapping_rows`)
            # and the rename-manifest lookup also call, so the three cannot drift.
            # Semantics: schema_mapping writes `status="success"`; approval_handler
            # flips it to `waiting_approval` when a manual approval is required; the
            # dashboard decision then sets `success` (approved) or `rejected`. A
            # rejection at a LATER run, which has no mapping row of its own when it
            # reused one, stamps `reuse_revoked_at` on the rows holding the rejected
            # mapping instead of rewriting their status. So the predicate is
            # `status == "success"` and no `reuse_revoked_at`: approved-or-approval-
            # not-required and not since refused, "which is exactly the set of
            # mappings safe to substitute".
            #
            # `.eq("success")` rather than anything admitting an absent status: no
            # writer produces a candidate row without `status`. The sole writer of a
            # row carrying a `mapping_id` always sets it
            # (`schema_mapping/lambda_function.py::store_mapping_result`), that step's
            # failure row carries no `mapping_id` so `mapping_id.exists()` already
            # excludes it, and the mutators use `SET` under `attribute_exists(run_id)`
            # — never `REMOVE`. A `| not_exists()` clause would be a second, divergent
            # definition of "approved", pointing the wrong way on a governance path.
            #
            # Narrowing this filter cannot refuse a run, which is why it does not meet
            # the outage shape `LGR-a419843-1` warns about ("a comparison that cannot
            # be made must not become a refusal"). An empty candidate set falls into
            # the `create_new_pipeline` / `force_new_mapping` branch immediately below:
            # the run computes a fresh mapping and takes the normal gate. The cost is
            # a Bedrock call, not availability.
            # The walk is paginated for the reason `LGR-eee8ac8-3` records about this
            # file's other GSI query: DynamoDB applies `FilterExpression` AFTER the 1MB
            # page read, so a page holding only rows the filter rejects comes back
            # `Items: []` WITH a `LastEvaluatedKey`. This partition holds ~9 step rows
            # per run and the filter now matches at most one of them, so an
            # all-rejected page is ordinary rather than pathological — and the status
            # filter above makes it likelier still, since a run rejected several times
            # in a row contributes only non-matching `schema_mapping` rows. Treating an
            # empty first page as "no approved mapping" would silently drop the
            # optimization on exactly the busiest pipelines. Bound and rationale mirror
            # `_PREVIOUS_SCHEMA_MAX_PAGES` and the sibling's
            # `_MAPPING_LOOKUP_MAX_PAGES`; `Limit` is deliberately not the bound,
            # because it applies BEFORE the filter.
            existing_mapping_id = None
            try:
                mapping_items: list[Any] = []
                mapping_last_key = None
                for _ in range(_PREVIOUS_SCHEMA_MAX_PAGES):
                    mapping_query: dict[str, Any] = {
                        "IndexName": "PipelineIndex",
                        "KeyConditionExpression": Key("pipeline_id").eq(original_pipeline_id),
                        "FilterExpression": (
                            Attr("step_name").eq("schema_mapping")
                            & Attr("mapping_id").exists()
                            & approved_for_reuse_condition()
                        ),
                        "ScanIndexForward": False,
                    }
                    if mapping_last_key:
                        mapping_query["ExclusiveStartKey"] = mapping_last_key
                    mapping_response = table.query(**mapping_query)
                    mapping_items.extend(mapping_response.get("Items", []))
                    mapping_last_key = mapping_response.get("LastEvaluatedKey")
                    # Newest-first, so the first page carrying a match already holds the
                    # winner; only an empty page needs the walk resumed.
                    if mapping_items or not mapping_last_key:
                        break
                if mapping_items:
                    mapping_items.sort(key=lambda x: item_str(x, "timestamp"), reverse=True)
                    existing_mapping_id = item_str(mapping_items[0], "mapping_id") or None
                    logger.info(f"Found existing approved mapping_id for reuse: {existing_mapping_id}")
            except Exception as e:
                logger.warning(f"Could not look up existing mapping_id: {e}")

            comparison_result["existing_mapping_id"] = existing_mapping_id

            # If no APPROVED mapping exists for this pipeline, we can't reuse — force
            # create. This is the safe degradation the status filter above relies on:
            # the run computes a new mapping and takes the normal approval gate.
            if not existing_mapping_id:
                logger.info(
                    f"No approved mapping found for pipeline {original_pipeline_id} despite identical schema — forcing create_new_pipeline"
                )
                comparison_result["action"] = "create_new_pipeline"
                comparison_result["summary"]["force_new_mapping"] = True
            else:
                logger.info(f"Schemas are identical for pipeline {original_pipeline_id}")
        else:
            # Perform detailed comparison
            comparison_result = compare_schemas(previous_schema, current_schema)
            comparison_result["summary"]["target_schema_changed"] = False

            # Determine action based on compatibility
            if comparison_result["is_compatible"]:
                # Schema is compatible - can use existing pipeline with minor updates
                comparison_result["action"] = "update_existing_pipeline"
                logger.info(f"Schema is compatible for pipeline {original_pipeline_id} - updating existing pipeline")
            else:
                # P30 R8.2: an incompatible schema — `field_removed` or, since
                # task 4.4, a declared `type_mismatch` — routes to
                # `create_new_pipeline`, which `schema_mapping`'s
                # `determine_approval_requirement` treats as a forced approval,
                # so nothing is written to the target until a human decides.
                # This is the existing `field_removed` route, reused rather than
                # duplicated: do not add a parallel action for type mismatches.
                comparison_result["action"] = "create_new_pipeline"
                logger.info(
                    f"Schema has breaking changes for pipeline {original_pipeline_id} "
                    f"({comparison_result['summary'].get('type_mismatches', 0)} type mismatch(es), "
                    f"{comparison_result['summary'].get('fields_removed', 0)} field(s) removed) "
                    "- creating new pipeline, which requires human approval before the target is written"
                )

        # Store comparison result
        store_comparison_result(
            run_id,
            original_pipeline_id,
            comparison_result,
            current_schema_hash,
            previous_schema_hash,
            execution_arn,
            target_schema_changed,
            target_old_version,
            target_new_version,
        )

        # Publish stage metrics
        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="SchemaCompare",
                    pipeline_id=original_pipeline_id or "unknown",
                    status="completed",
                    duration_ms=_duration_ms,
                )
            except Exception:
                pass  # Non-fatal

        return {
            "statusCode": 200,
            "run_id": run_id,
            "original_pipeline_id": original_pipeline_id,
            "schema_hash": current_schema_hash,
            "previous_schema_hash": previous_schema_hash,
            "action": comparison_result["action"],
            "comparison_result": comparison_result,
            "existing_mapping_id": comparison_result.get("existing_mapping_id", ""),
            "target_schema_changed": target_schema_changed,
            "message": f"Schema comparison completed - action: {comparison_result['action']}",
        }

    except Exception as e:
        logger.error(f"Error in schema comparison: {e!s}")

        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="SchemaCompare",
                    pipeline_id=original_pipeline_id if "original_pipeline_id" in dir() else "unknown",
                    status="failed",
                    duration_ms=_duration_ms,
                )
            except Exception:
                pass

        # P28 R8.5 (task 14.2): reliably write the failed state record with
        # the table's full key. The old item lacked the run_id partition key,
        # so the write was a 100% ValidationException silently swallowed by
        # the except — the failure record never landed. The record now
        # mirrors the sibling step Lambdas' failure rows: (run_id, canonical
        # step_name), status "failed", step_sequence from the shared table.
        try:
            _failed_run_id = event.get("run_id", "unknown")
            _failed_pipeline_id = (
                original_pipeline_id
                if "original_pipeline_id" in locals()
                else event.get("original_pipeline_id", "unknown")
            )
            table.put_item(
                Item={
                    "run_id": _failed_run_id,  # Partition key
                    "step_name": StepName.SCHEMA_COMPARE,
                    "status": "failed",
                    "step_sequence": STEP_SEQUENCE[StepName.SCHEMA_COMPARE],
                    "timestamp": datetime.now(UTC).isoformat(),
                    "pipeline_id": _failed_pipeline_id,  # For PipelineIndex GSI
                    "original_pipeline_id": _failed_pipeline_id,
                    "error_message": str(e),
                }
            )
        except Exception as write_err:
            # Never mask the original failure with a state-write error.
            logger.warning(f"Failed to write error state to DynamoDB: {write_err}")

        # P28 R1.1: raise so the Step Functions task fails and Retry/Catch fire
        # (an error payload return would be treated as task success).
        raise StepFailure(
            "schema_compare",
            f"Schema comparison failed: {e!s}",
            details={"run_id": event.get("run_id", "unknown")},
        ) from e
