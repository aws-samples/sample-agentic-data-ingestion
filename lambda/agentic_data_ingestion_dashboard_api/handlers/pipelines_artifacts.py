#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generated-code / test artifact retrieval for the dashboard API.

Extracted verbatim from ``handlers/pipelines.py`` (P21 Task 7.2 — module
decomposition). Behaviour is unchanged; ``handlers/pipelines.py`` re-exports
the public names for router/test compatibility.
"""

import json
from typing import Any

from botocore.exceptions import ClientError

from dynamo_typing import item_int, item_str
from step_errors import NOT_FOUND_CLIENT_ERROR_CODES

from .common import create_response, handle_error, logger, s3_client


def get_code_by_version(code_version_id: str) -> dict[str, Any]:
    """Get generated code details by version ID"""
    try:
        from .common import generated_code_table, s3_client

        logger.info(f"Fetching code for version: {code_version_id}")

        # Query generated code table using composite key
        response = generated_code_table.get_item(Key={"PK": f"CODE#{code_version_id}", "SK": "VERSION"})

        if "Item" not in response:
            return create_response(404, {"error": f"Code version not found: {code_version_id}"})

        item = response["Item"]
        code_s3_uri = item_str(item, "code_s3_uri")

        # Fetch code content from S3
        code_content = ""
        if code_s3_uri:
            try:
                # Parse S3 URI
                bucket = code_s3_uri.replace("s3://", "").split("/")[0]
                key = "/".join(code_s3_uri.replace("s3://", "").split("/")[1:])

                # Get object from S3
                s3_response = s3_client.get_object(Bucket=bucket, Key=key)
                code_content = s3_response["Body"].read().decode("utf-8")
            except Exception as e:
                logger.error(f"Error fetching code from S3: {e!s}")
                code_content = f"Error loading code from S3: {e!s}"

        return create_response(
            200,
            {
                "code_version_id": code_version_id,
                "run_id": item.get("run_id", ""),
                "pipeline_id": item.get("pipeline_id", ""),
                "mapping_id": item.get("mapping_id", ""),
                "code_s3_uri": code_s3_uri,
                "code_content": code_content,
                "generation_timestamp": item.get("generation_timestamp", ""),
                "generation_status": item.get("generation_status", ""),
                "code_metadata": item.get("code_metadata", {}),
                "agent_session_id": item.get("agent_session_id", ""),
            },
        )

    except Exception as e:
        logger.error(f"Error getting code details: {e!s}")
        return handle_error(e, f"Error getting code for version {code_version_id}")


def get_tests_by_version(code_version_id: str) -> dict[str, Any]:
    """Get test results by code version ID"""
    try:
        from .common import test_results_table

        logger.info(f"Fetching test results for code version: {code_version_id}")

        # Query test results table using composite key
        response = test_results_table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"TEST#{code_version_id}"},
            ScanIndexForward=False,  # Most recent first
        )

        items = response.get("Items", [])

        if not items:
            return create_response(404, {"error": f"No test results found for code version: {code_version_id}"})

        # Format test results.
        #
        # This is an EXPLICIT key list, deliberately — it is the allowlist that
        # decides what a test-results row publishes, not a convenience. Widen it
        # only by a key with a reader, per ``LGR-69ca78b-5`` / ``LGR-f077f1b-1``
        # (both template / ratified): a field with no consumer is dead weight,
        # and the same reasoning removed ``approval_required`` from
        # ``get_pipeline_code_versions`` below. Do NOT convert it to a
        # forward-everything spread.
        #
        # Three attributes on the row are withheld on that basis, each verified
        # by search over ``dashboard-react/`` rather than assumed:
        #
        # * ``execution_verified`` — p40 R6's statement of whether the transform
        #   actually ran. ``test_code``'s producer comment predicted this list
        #   "gain[ing] it" as a follow-up, but the frontend has **no** reader:
        #   zero hits for ``execution_verified`` and ``verification_note``
        #   anywhere under ``dashboard-react/``. The three-state check rendering
        #   in ``approvals/ApprovalArtifact.tsx`` reads the PER-CHECK
        #   ``verified`` / ``not_verified_reason`` keys off the approval payload,
        #   which is a different surface fed by a different producer — it never
        #   reads this row. Forwarding it here would publish a field nothing
        #   renders, which is exactly what ``LGR-f077f1b-1`` rejected. It stays
        #   withheld until a consumer exists.
        # * ``test_report_s3_uri`` — no reader; the frontend fetches the report
        #   through ``test_output_s3_uri`` (``use-code.ts:36``).
        # * ``fixed_code_version_id`` — no reader.
        formatted_results = []
        for test in items:
            formatted_results.append(
                {
                    "test_execution_id": test.get("test_execution_id", ""),
                    "code_version_id": code_version_id,
                    "test_timestamp": test.get("test_timestamp", ""),
                    "test_status": test.get("test_status", ""),
                    "test_results": test.get("test_results", {}),
                    "test_output_s3_uri": test.get("test_output_s3_uri", ""),
                    "validation_status": test.get("validation_status", ""),
                    # Added because it HAS a reader: ``use-code.ts:50`` assigns
                    # ``error_details`` onto the ``TestResult`` it returns, and
                    # ``TestResultsViewer`` renders it in the Error Details block
                    # (``:346``, and ``:399`` in ``TestResultsDisplay``). Until
                    # this key was forwarded, that block was unreachable on every
                    # failing run — the producer writes
                    # ``f"Failed checks: {…}"`` whenever ``test_status`` is not
                    # ``passed`` (``test_code/lambda_function.py:2990-2993``), so
                    # the data existed and only the read path was missing.
                    #
                    # No default: the producer writes ``None`` explicitly on a
                    # passing run, so ``None`` is this attribute's own "no error"
                    # value and a ``""`` default would be a second spelling of
                    # it. Both serialise falsy and the frontend coalesces with
                    # ``?? undefined``, so the rendered outcome is identical —
                    # ``None`` is chosen because it preserves the producer's
                    # value verbatim rather than inventing one.
                    "error_details": test.get("error_details"),
                    "agent_session_id": test.get("agent_session_id", ""),
                    "retry_count": test.get("retry_count", 0),
                }
            )

        return create_response(
            200,
            {
                "code_version_id": code_version_id,
                "test_count": len(formatted_results),
                "latest_test": formatted_results[0] if formatted_results else None,
                "all_tests": formatted_results,
            },
        )

    except Exception as e:
        logger.error(f"Error getting test results: {e!s}")
        return handle_error(e, f"Error getting test results for code version {code_version_id}")


def fetch_s3_content(event: dict[str, Any]) -> dict[str, Any]:
    """Fetch content from S3"""
    try:
        from .common import s3_client

        query_params = event.get("queryStringParameters") or {}
        bucket = query_params.get("bucket")
        key = query_params.get("key")

        if not bucket or not key:
            return create_response(400, {"error": "Missing bucket or key parameter"})

        logger.info(f"Fetching S3 content: s3://{bucket}/{key}")

        # Fetch object from S3
        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            content = response["Body"].read().decode("utf-8")

            # Try to parse as JSON
            try:
                json_content = json.loads(content)
                return create_response(200, json_content)
            except json.JSONDecodeError:
                # Return as plain text if not JSON
                return create_response(200, {"content": content, "type": "text"})

        except s3_client.exceptions.NoSuchKey:
            # File doesn't exist - return empty result instead of error
            logger.warning(f"S3 object not found: s3://{bucket}/{key}")
            return create_response(
                200,
                {
                    "error": "not_found",
                    "message": "Test results not available. The test may not have completed or failed to write results.",
                    "location": f"s3://{bucket}/{key}",
                },
            )

    except Exception as e:
        logger.error(f"Error fetching S3 content: {e!s}")
        return handle_error(e, f"Error fetching S3 content from {bucket}/{key}")


def get_pipeline_code_versions(pipeline_id: str) -> dict[str, Any]:
    """Get all code versions for a specific pipeline"""
    try:
        from .common import generated_code_table

        # Query the generated_code table using the pipeline_id-index GSI
        response = generated_code_table.query(
            IndexName="pipeline_id-index",
            KeyConditionExpression="pipeline_id = :pid",
            ExpressionAttributeValues={":pid": pipeline_id},
            ScanIndexForward=False,  # Sort by generation_timestamp descending (newest first)
        )

        code_versions = []
        for item in response.get("Items", []):
            code_version = {
                "code_version_id": item.get("code_version_id"),
                "run_id": item.get("run_id"),
                "code_s3_uri": item.get("code_s3_uri"),
                "generation_timestamp": item.get("generation_timestamp"),
                "generation_status": item.get("generation_status"),
                "template_used": item.get("template_used"),
                "regeneration_attempt": item_int(item, "regeneration_attempt"),
                # p34 follow-up: ``approval_required`` REMOVED from this payload.
                #
                # It read ``item.get("approval_required", False)`` from the
                # generated-code table — the attribute ``create_code`` stopped
                # writing when p34 task 3.2 deleted
                # ``determine_approval_requirement`` and its six sites under
                # amended, ratified R3.4 (see ``create_code:804``, ``:848``).
                # The ``.get(…, False)`` default absorbed that deletion without
                # raising, so the field had become a silent mix of stale stored
                # values on pre-change rows and a hardcoded ``False`` on every
                # row written since. No test asserted it and no frontend
                # consumer read it (verified by search: zero hits for
                # ``code_versions`` under ``tests/``; zero for
                # ``approval_required`` under ``dashboard-react/src`` other than
                # pipeline-status string comparisons in ``formatting.ts`` and
                # ``PipelineListPage.tsx``, which are a different concern).
                #
                # Deliberately NOT re-sourced from ``test_code``'s state row.
                # ``LGR-f077f1b-1`` (template / ratified) rejected *renaming*
                # ``create_code``'s approval field for exactly this reason: a
                # field with no consumer is dead weight, which is the objection
                # requirement 3 — "one field, one meaning, one owner" — exists
                # to answer. Re-sourcing would add a per-code-version
                # ``pipeline_runs`` query to feed a reader that does not exist.
                #
                # The decision now lives on ``test_code``'s state row, which
                # publishes ``approval_required`` (the merged Approval_Policy
                # decision), ``quality_gate_requires_approval`` (the unmerged
                # quality verdict) and ``approval_decision_inputs`` (which of
                # the three inputs forced the gate). If per-code-version
                # approval state is wanted on this endpoint later, it should be
                # a deliberate feature sourced from there. The absence is
                # asserted by
                # ``tests/contracts/api_responses/test_pipeline_code_versions_contract.py``.
                "code_metadata": item.get("code_metadata", {}),
                "agent_reasoning": item.get("agent_reasoning", ""),
                "required_parameters": item.get("required_parameters", []),
                "optional_parameters": item.get("optional_parameters", []),
                "parameter_defaults": item.get("parameter_defaults", {}),
            }
            code_versions.append(code_version)

        logger.info(f"Found {len(code_versions)} code versions for pipeline {pipeline_id}")

        return create_response(
            200, {"pipeline_id": pipeline_id, "code_versions": code_versions, "total_count": len(code_versions)}
        )

    except Exception as e:
        return handle_error(e, f"Error fetching code versions for pipeline {pipeline_id}")


def _read_s3_object(s3_uri: str) -> str | None:
    """Read text content from an S3 URI (s3://bucket/key).

    Returns ``None`` only when the object genuinely does not exist (callers
    treat a missing body as absent content). Infrastructure errors
    (throttles, access denied) propagate to the endpoint's error handler so
    they surface as 5xx instead of masquerading as absent content
    (P28 R17.3).
    """
    parts = s3_uri.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in NOT_FOUND_CLIENT_ERROR_CODES:
            logger.warning(f"S3 object not found: {s3_uri}")
            return None
        logger.error(f"Error reading S3 object {s3_uri}: {e}")
        raise
    return response["Body"].read().decode("utf-8")
