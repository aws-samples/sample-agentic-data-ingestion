#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Schema-mapping operations for the dashboard API.

Extracted verbatim from ``handlers/pipelines.py`` (P21 Task 7.2 — module
decomposition). Behaviour is unchanged; ``handlers/pipelines.py`` re-exports
the public names for router/test compatibility.
"""

import json
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key

from .common import (
    AWS_REGION,
    create_response,
    logger,
    mappings_table,
    pipelines_table,
    state_table,
)


def rerun_mapping(event: dict[str, Any]) -> dict[str, Any]:
    """POST /api/rerun-mapping — trigger fresh AI mapping with optional context.

    Parses pipeline_key, context, and model_id from the POST body.
    Persists context as mapping_context on the pipeline record, queries
    the state table for the latest schema_extract run, deletes stale
    mapping records, and invokes the schema mapping Lambda synchronously.
    """
    try:
        body = json.loads(event.get("body", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        return create_response(400, {"error": "Invalid JSON in request body"})

    pipeline_key = body.get("pipeline_key")
    context = body.get("context", "")
    model_id = body.get("model_id", "")

    # --- 1.1: Validate inputs ---
    if not pipeline_key:
        return create_response(400, {"error": "pipeline_key is required"})

    if len(context) > 2000:
        return create_response(400, {"error": "context exceeds 2000 character limit"})

    # --- 1.3: Fetch pipeline record and read stored mapping_context ---
    try:
        pipeline_resp = pipelines_table.get_item(Key={"pipeline_id": pipeline_key})
        pipeline_item = pipeline_resp.get("Item", {})
    except Exception as e:
        logger.error(f"Error fetching pipeline {pipeline_key}: {e}")
        return create_response(500, {"error": "Failed to fetch pipeline record", "details": str(e)})

    stored_context = pipeline_item.get("mapping_context", "")

    # Persist or remove mapping_context on the pipeline record
    try:
        if context:
            pipelines_table.update_item(
                Key={"pipeline_id": pipeline_key},
                UpdateExpression="SET mapping_context = :mc",
                ExpressionAttributeValues={":mc": context},
            )
        else:
            pipelines_table.update_item(
                Key={"pipeline_id": pipeline_key},
                UpdateExpression="REMOVE mapping_context",
            )
    except Exception as e:
        logger.warning(f"Failed to persist mapping_context for {pipeline_key}: {e}")

    # The merged context sent to the Lambda is simply what the user provided.
    # The stored mapping_context was already persisted/removed above.
    # If the user cleared the context, we send empty — no merging with stale stored values.
    merged_context = context

    # --- 1.2: Query state table for latest schema_extract run ---
    try:
        state_resp = state_table.query(
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq(pipeline_key),
            FilterExpression=Attr("step_name").eq("schema_extract"),
            ScanIndexForward=False,
            Limit=10,
        )
        runs = state_resp.get("Items", [])
    except Exception as e:
        logger.error(f"Error querying state table for {pipeline_key}: {e}")
        return create_response(500, {"error": "Failed to query state table", "details": str(e)})

    if not runs:
        return create_response(404, {"error": f"No source schema run found for pipeline: {pipeline_key}"})

    latest_run = runs[0]
    run_id = latest_run.get("run_id")
    schema_hash = latest_run.get("schema_hash", "")

    # --- 1.5: Delete stale mapping records for this schema hash ---
    # NOTE: Stale mapping cleanup is NOT done here because the Lambda is invoked
    # asynchronously. Deleting mappings before the Lambda finishes creates a race
    # condition where the new mapping gets deleted by a subsequent rerun click.
    # The Lambda creates a new mapping with a unique ID; old ones are harmless.

    # --- 1.4: Invoke schema mapping Lambda asynchronously ---
    # The mapping Lambda takes ~30s which exceeds the dashboard API's 25s timeout,
    # so we invoke asynchronously and return immediately.
    lambda_payload: dict[str, Any] = {
        "run_id": run_id,
        "source_schema_hash": schema_hash,
        "additional_context": merged_context,
        "pipeline_id": pipeline_key,
        "force_remap": True,
    }
    if model_id:
        lambda_payload["bedrock_model_id"] = model_id

    try:
        lambda_client = boto3.client("lambda", region_name=AWS_REGION)
        lambda_client.invoke(
            FunctionName="agentic_data_ingestion_schema_mapping",
            InvocationType="Event",
            Payload=json.dumps(lambda_payload),
        )
        return create_response(
            200,
            {
                "success": True,
                "message": "Mapping regeneration started",
                "pipeline_id": pipeline_key,
                "run_id": run_id,
            },
        )
    except Exception as e:
        logger.error(f"Schema mapping Lambda invocation failed: {e}")
        return create_response(500, {"error": "Schema mapping invocation failed", "details": str(e)})


def save_mapping_hints(pipeline_id: str, body: str) -> dict[str, Any]:
    """POST /api/pipelines/{id}/mapping-hints — persist explicit field mapping hints.

    Hints are a dict of source→target field overrides (e.g. {"fname": "first_name"}).
    They are stored as the ``mapping_hints`` attribute on the pipeline record and
    included in the AI prompt by the schema mapping Lambda.
    """
    try:
        data = json.loads(body) if body else {}
    except (json.JSONDecodeError, TypeError):
        return create_response(400, {"error": "Invalid JSON in request body"})

    hints = data.get("hints")
    if hints is None:
        return create_response(400, {"error": "hints is required"})

    if not isinstance(hints, dict):
        return create_response(400, {"error": "hints must be a JSON object"})

    try:
        if hints:
            pipelines_table.update_item(
                Key={"pipeline_id": pipeline_id},
                UpdateExpression="SET mapping_hints = :mh",
                ExpressionAttributeValues={":mh": hints},
            )
        else:
            pipelines_table.update_item(
                Key={"pipeline_id": pipeline_id},
                UpdateExpression="REMOVE mapping_hints",
            )
        return create_response(200, {"success": True, "message": "Mapping hints saved successfully"})
    except Exception as e:
        logger.error(f"Error saving mapping hints for {pipeline_id}: {e}")
        return create_response(500, {"error": "Failed to save mapping hints", "details": str(e)})


def update_mappings(pipeline_id: str, body: str) -> dict[str, Any]:
    """PUT /api/pipelines/{id}/mappings — directly update the mapping_result in the mappings table.

    Accepts a JSON body with:
      - mappings: list of {source_field, target_field, confidence?, hint?} objects
      - mapping_id: the mapping_id to update (from pipeline detail response)

    If mapping_id is not provided, looks it up from the pipeline's latest state.
    """
    try:
        data = json.loads(body) if body else {}
    except (json.JSONDecodeError, TypeError):
        return create_response(400, {"error": "Invalid JSON in request body"})

    mappings = data.get("mappings")
    if mappings is None or not isinstance(mappings, list):
        return create_response(400, {"error": "mappings must be a JSON array"})

    mapping_id = data.get("mapping_id", "")

    # If no mapping_id provided, look it up from the pipeline's latest schema_mapping state
    if not mapping_id:
        try:
            result = state_table.query(
                IndexName="PipelineIndex",
                KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
                FilterExpression=Attr("step_name").eq("schema_mapping") & Attr("mapping_id").exists(),
                ScanIndexForward=False,
                Limit=10,
            )
            for item in result.get("Items", []):
                mid = item.get("mapping_id", "")
                if mid:
                    mapping_id = mid
                    break
        except Exception as e:
            logger.error(f"Error looking up mapping_id for {pipeline_id}: {e}")

    if not mapping_id:
        return create_response(404, {"error": "No mapping found for this pipeline"})

    try:
        # Fetch existing mapping_result to merge
        existing_response = mappings_table.get_item(Key={"mapping_id": mapping_id})
        if "Item" not in existing_response:
            return create_response(404, {"error": f"Mapping {mapping_id} not found"})

        existing_item = existing_response["Item"]
        existing_result = json.loads(existing_item.get("mapping_result", "{}"))

        # Replace the mappings array in the result
        existing_result["mappings"] = mappings

        # Recalculate unmapped fields
        mapped_sources = {m["source_field"] for m in mappings if m.get("source_field")}
        mapped_targets = {m["target_field"] for m in mappings if m.get("target_field")}

        # Preserve existing unmapped lists but remove newly mapped fields
        old_unmapped_source = existing_result.get("unmapped_source_fields", [])
        old_unmapped_target = existing_result.get("unmapped_target_fields", [])

        if old_unmapped_source:
            existing_result["unmapped_source_fields"] = [
                f
                for f in old_unmapped_source
                if (f if isinstance(f, str) else f.get("field", "")) not in mapped_sources
            ]

        if old_unmapped_target:
            existing_result["unmapped_target_fields"] = [
                f
                for f in old_unmapped_target
                if (f if isinstance(f, str) else f.get("field", "")) not in mapped_targets
            ]

        # Update the mapping record
        mappings_table.update_item(
            Key={"mapping_id": mapping_id},
            UpdateExpression="SET mapping_result = :mr, confidence_score = :cs",
            ExpressionAttributeValues={
                ":mr": json.dumps(existing_result),
                ":cs": Decimal(str(sum(m.get("confidence", 0.7) for m in mappings) / max(len(mappings), 1))),
            },
        )

        logger.info(f"Updated mappings for pipeline {pipeline_id}, mapping_id={mapping_id}, count={len(mappings)}")
        return create_response(200, {"success": True, "message": f"Mappings updated ({len(mappings)} fields)"})

    except Exception as e:
        logger.error(f"Error updating mappings for {pipeline_id}: {e}")
        return create_response(500, {"error": "Failed to update mappings", "details": str(e)})
