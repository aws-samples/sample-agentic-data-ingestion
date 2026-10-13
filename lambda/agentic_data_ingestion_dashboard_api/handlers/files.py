#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""File management handlers for dashboard API"""

from typing import Any

from dynamo_typing import item_str

from .common import create_response, handle_error, logger, state_table

#: State-row attributes ``get_file_details`` must never pass through to the
#: browser in a step's ``details``. A Step Functions task token lets its holder
#: report a result for the step it belongs to. run_pipeline no longer stores
#: one on the state row (it lives in the task-tokens table), but rows written
#: before that change still carry ``task_token``, and those rows are not
#: rewritten. Both spellings in use are listed, as ``retry_utils``'s
#: ``TASK_TOKEN_FIELDS`` does. A deny set rather than an allowlist because
#: ``details`` is deliberately the row's remaining attributes, and an
#: allowlist would silently drop any attribute a later writer adds.
_INTERNAL_STATE_ATTRIBUTES = frozenset({"task_token", "taskToken"})

#: Attributes ``get_file_details`` lifts to the top level of a step, so they are
#: not repeated inside ``details``.
_STEP_TOP_LEVEL_ATTRIBUTES = frozenset({"step_name", "status", "timestamp", "execution_id", "run_id", "error_message"})


def get_files(query_params: dict[str, str]) -> dict[str, Any]:
    """Get all file metadata from DynamoDB state table"""
    try:
        pipeline_key = query_params.get("pipeline_key")

        # Build scan parameters
        scan_kwargs: dict[str, Any]
        if pipeline_key:
            scan_kwargs = {
                "FilterExpression": "(pipeline_id = :pk OR original_pipeline_id = :pk) AND step_name = :step",
                "ExpressionAttributeValues": {":pk": pipeline_key, ":step": "file_validation"},
            }
        else:
            scan_kwargs = {
                "FilterExpression": "step_name = :step",
                "ExpressionAttributeValues": {":step": "file_validation"},
            }

        # Paginate through all results (DynamoDB scan returns max 1MB per call)
        files: list[dict[str, Any]] = []
        while True:
            response = state_table.scan(**scan_kwargs)

            for item in response["Items"]:
                file_info = {
                    "file_key": item_str(item, "original_s3_uri").split("/")[-1],
                    "pipeline_key": item_str(item, "original_pipeline_id") or item_str(item, "pipeline_id"),
                    "processed_at": item.get("timestamp", ""),
                    "status": item.get("status", "unknown"),
                    "file_type": item.get("file_type", "unknown"),
                    "original_s3_uri": item.get("original_s3_uri", ""),
                    "json_s3_uri": item.get("json_s3_uri", ""),
                }
                files.append(file_info)

            # Check if there are more pages
            if "LastEvaluatedKey" not in response:
                break
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        # Sort files by processed_at timestamp (most recent first)
        files.sort(key=lambda x: x.get("processed_at", ""), reverse=True)

        logger.info(f"Returning {len(files)} files")
        return create_response(200, files)

    except Exception as e:
        return handle_error(e, "Error fetching files")


def get_file_details(pipeline_key: str, file_key: str) -> dict[str, Any]:
    """Get detailed execution information for a specific file"""
    try:
        # Get all items for this pipeline and file (with pagination)
        scan_kwargs: dict[str, Any] = {
            "FilterExpression": "(pipeline_id = :pk OR original_pipeline_id = :pk)",
            "ExpressionAttributeValues": {":pk": pipeline_key},
        }

        items = []
        while True:
            response = state_table.scan(**scan_kwargs)
            items.extend(response["Items"])
            if "LastEvaluatedKey" not in response:
                break
            scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        file_steps: list[dict[str, Any]] = []

        for item in items:
            # Check if this item relates to our file
            original_s3_uri = item_str(item, "original_s3_uri")
            json_s3_uri = item_str(item, "json_s3_uri")

            item_file_key = ""
            if original_s3_uri:
                item_file_key = original_s3_uri.split("/")[-1]
            elif json_s3_uri:
                item_file_key = json_s3_uri.split("/")[-1]
                if item_file_key.endswith(".json") and not item_file_key.endswith("_reprocess.json"):
                    item_file_key = item_file_key.replace(".json", "")

            if item_file_key == file_key:
                step_info = {
                    "step_name": item.get("step_name", ""),
                    "status": item.get("status", ""),
                    "timestamp": item.get("timestamp", ""),
                    "execution_id": item.get("execution_id", ""),
                    "run_id": item.get("run_id", ""),
                    "error_message": item.get("error_message", ""),
                    "details": {
                        k: v
                        for k, v in item.items()
                        if k not in _STEP_TOP_LEVEL_ATTRIBUTES and k not in _INTERNAL_STATE_ATTRIBUTES
                    },
                }
                file_steps.append(step_info)

        # Sort by timestamp
        file_steps.sort(key=lambda x: x.get("timestamp", ""))

        if not file_steps:
            return create_response(404, {"error": "File not found"})

        return create_response(200, {"pipeline_key": pipeline_key, "file_key": file_key, "steps": file_steps})

    except Exception as e:
        return handle_error(e, "Error fetching file details")
