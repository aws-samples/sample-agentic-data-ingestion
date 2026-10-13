#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Mapping memory management handlers for dashboard API.

Provides endpoints to list and delete mapping memory entries stored
in Bedrock AgentCore Memory via the MappingMemoryClient.
"""

from mapping_memory import MappingMemoryClient

from .common import create_response, handle_error


def get_mapping_memory(pipeline_id: str) -> dict:
    """Return all mapping memory entries for a pipeline.

    Calls MappingMemoryClient.get_entries() and returns the parsed records
    sorted by confidence descending.

    Returns:
        200 with list of mapping memory entries.
        500 on internal error.
    """
    try:
        client = MappingMemoryClient()
        if not client.enabled:
            return create_response(
                200,
                {
                    "pipeline_id": pipeline_id,
                    "entries": [],
                    "count": 0,
                    "memory_enabled": False,
                    "message": "Mapping memory is disabled. Set MAPPING_MEMORY_ID and MEMORY_ENABLED=true.",
                },
            )
        entries = client.get_entries(pipeline_id)
        return create_response(
            200,
            {
                "pipeline_id": pipeline_id,
                "entries": entries,
                "count": len(entries),
                "memory_enabled": True,
            },
        )
    except Exception as exc:
        return handle_error(exc, f"Failed to retrieve mapping memory for pipeline {pipeline_id}")


def delete_mapping_memory_entry(pipeline_id: str, entry_id: str) -> dict:
    """Delete a specific mapping memory entry.

    Calls MappingMemoryClient.delete_entry() with the record ID.

    Returns:
        200 on successful deletion.
        500 on internal error.
    """
    try:
        client = MappingMemoryClient()
        success = client.delete_entry(entry_id)
        if success:
            return create_response(
                200,
                {
                    "message": f"Entry {entry_id} deleted successfully",
                    "pipeline_id": pipeline_id,
                    "entry_id": entry_id,
                },
            )
        else:
            return create_response(
                500,
                {
                    "error": f"Failed to delete entry {entry_id}",
                    "pipeline_id": pipeline_id,
                },
            )
    except Exception as exc:
        return handle_error(exc, f"Failed to delete mapping memory entry {entry_id}")
