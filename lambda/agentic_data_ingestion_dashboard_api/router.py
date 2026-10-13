#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Router for dashboard API endpoints - Resource-level routing pattern

This router implements a resource-based routing strategy that aligns with the
API Gateway resource-level catch-all pattern. Each top-level resource (pipelines,
files, approvals, etc.) is routed to its dedicated handler module.

Architecture:
    API Gateway: /api/{resource}/{proxy+}
    Router: Extracts resource and delegates to resource-specific router
    Handlers: Process requests within their domain

Example Flow:
    Request: GET /api/pipelines/my-pipeline/details
    → Router extracts: resource='pipelines', subpath='my-pipeline/details'
    → Delegates to: pipelines_router(event, subpath)
    → Handler processes and returns response
"""

from collections.abc import Callable
from typing import Any

from handlers.approvals import approve_request, get_approvals, get_pending_approvals, reject_request
from handlers.bedrock_models import get_bedrock_models
from handlers.common import create_response, logger
from handlers.errors import get_errors
from handlers.files import get_file_details, get_files
from handlers.health import get_health, get_notifications, get_updates, mark_notification_read
from handlers.metrics import get_comprehensive_metrics
from handlers.pipelines import (
    archive_pipeline,
    create_pipeline,
    edit_pipeline,
    get_pipeline_details,
    get_pipelines,
    rerun_mapping,
    restore_pipeline,
    save_mapping_hints,
    trigger_batch,
    update_mappings,
)
from handlers.schemas import (
    delete_schema,
    get_schema,
    get_schemas,
    set_display_label,
    update_field_context,
    upload_schema,
)
from handlers.stats import get_stats
from handlers.upload import presign_upload, trigger_pipeline


def pipelines_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route requests for /api/pipelines/* endpoints"""
    http_method = event.get("httpMethod", "GET")
    query_params = event.get("queryStringParameters") or {}

    # /api/pipelines (root)
    if not subpath:
        if http_method == "GET":
            # Extract pagination and filter parameters
            page = int(query_params.get("page", 1))
            page_size = int(query_params.get("page_size", 20))
            search = query_params.get("search", "")
            status_filter = query_params.get("status", "")
            return get_pipelines(page, page_size, search, status_filter)
        elif http_method == "POST":
            return create_pipeline(event.get("body") or "{}", event=event)

    # /api/pipelines/archived
    if subpath == "archived" and http_method == "GET":
        from handlers.pipelines import get_archived_pipelines

        return get_archived_pipelines()

    # /api/pipelines/provision (special case - same as create)
    if subpath == "provision" and http_method == "POST":
        return create_pipeline(event.get("body") or "{}", event=event)

    # /api/pipelines/{pipeline_id}/*
    parts = subpath.strip("/").split("/")
    pipeline_id = parts[0] if parts else ""
    action = parts[1] if len(parts) > 1 else ""

    if action == "edit" and http_method == "PUT":
        return edit_pipeline(pipeline_id, event.get("body") or "{}", event=event)
    elif action == "archive" and http_method == "PUT":
        return archive_pipeline(pipeline_id, event=event)
    elif action == "restore" and http_method == "PUT":
        return restore_pipeline(pipeline_id)
    elif action == "trigger-batch" and http_method == "POST":
        # POST /api/pipelines/{id}/trigger-batch
        event["pipeline_id"] = pipeline_id
        return trigger_batch(event)
    elif action == "preview" and http_method == "GET":
        from handlers.data_preview import get_latest_preview

        return get_latest_preview(pipeline_id, query_params)
    elif action == "runs" and len(parts) > 2:
        run_id = parts[2]
        run_action = parts[3] if len(parts) > 3 else ""
        if run_action == "lineage" and http_method == "GET":
            from handlers.lineage import get_run_lineage

            return get_run_lineage(pipeline_id, run_id)
        elif run_action == "self-healing" and http_method == "GET":
            from handlers.agents import get_self_healing_activity

            return get_self_healing_activity(pipeline_id, run_id)
        elif run_action == "quality-report" and http_method == "GET":
            from handlers.agents import get_quality_report

            return get_quality_report(pipeline_id, run_id)
        # There is deliberately no POST quality-report route. The on-demand data
        # quality check was disabled on 2026-10-11: it dispatched the agent with
        # no target table and the agent's role cannot run Athena, so its only
        # possible result was a failing report written over the inline Glue one.
        # The agent runtime and its dispatch grant stay deployed so it can be
        # finished later; see docs/AGENTS.md § 3.
    elif action == "costs" and http_method == "GET":
        from handlers.costs import get_pipeline_costs

        return get_pipeline_costs(pipeline_id)
    elif action == "audit" and http_method == "GET":
        from handlers.audit import get_pipeline_audit

        return get_pipeline_audit(pipeline_id, query_params)
    elif action == "run" and len(parts) == 2 and http_method == "POST":
        # POST /api/pipelines/{pipeline_id}/run — Run Now (P17 Req 5.8).
        # Dispatches to handlers.pipelines.run_now, which synchronously
        # invokes Pipeline_Trigger_Lambda with a manual-trigger payload.
        from handlers.pipelines import run_now

        return run_now(pipeline_id, event=event)
    elif action == "run" and len(parts) > 2 and http_method == "GET":
        # /api/pipeline/{pipeline_id}/run/{run_id}/artifacts or preview
        run_id = parts[2]
        run_action = parts[3] if len(parts) > 3 else ""
        if run_action == "artifacts":
            from handlers.pipelines import get_run_artifacts

            return get_run_artifacts(pipeline_id, run_id)
        elif run_action == "preview":
            from handlers.data_preview import get_data_preview

            return get_data_preview(pipeline_id, run_id, query_params)
        elif run_action == "neighbours":
            # GET /api/pipeline/{pipeline_id}/run/{run_id}/neighbours — p48 C10,
            # task 5.2. The run either side of this one in chronological order,
            # backing the run-detail Previous/Next buttons.
            #
            # Added to THIS route family rather than as a new top-level surface
            # because it is keyed by exactly the pair the family is already keyed by
            # (`pipeline_id`, `run_id`), like `artifacts` and `preview` above.
            #
            # No query parameters at all, and that is the point: the anchor is the
            # `run_id` in the path, and `handlers/pipelines_runs.get_run_neighbours`
            # mints the `ExclusiveStartKey` server-side from a keyed read. There is
            # no cursor to validate here because nothing client-supplied reaches the
            # seek — see that function's docstring for why `run_cursor` is not used.
            from handlers.pipelines import get_run_neighbours

            return get_run_neighbours(pipeline_id, run_id)
    elif action == "executions" and http_method == "GET":
        from handlers.pipelines import get_pipeline_executions

        # Get pagination parameters from query string
        page = int(query_params.get("page", 1))
        page_size = int(query_params.get("page_size", 10))
        # p48 C2 (task 3.2): the keyed-pagination token from a previous response's
        # ``pagination.next_cursor``. Passed through as an opaque string and NOT
        # parsed here — ``handlers/run_cursor.decode_cursor`` owns deciding what a
        # cursor is, next to the allowlist that makes the decision safe, so there is
        # exactly one place that validates it. An empty ``?cursor=`` reads as absent.
        #
        # ``page_size`` still has no ceiling here (noted at ``pipelines_runs.py``'s
        # batch-get comment and p48 O6). Pre-existing and deliberately not touched:
        # in scope only insofar as this change must not make it worse, and keyed
        # paging reads exactly one page whatever the number is.
        cursor = query_params.get("cursor") or None
        # p48 C5 (task 4.3): the derived-status filter. Read beside ``page_size`` and
        # ``cursor`` and passed through **unvalidated on purpose** — the handler owns
        # the accepted set (``pipelines_runs._ACCEPTED_STATUS_FILTERS``) next to the
        # comment explaining why ``QUEUED`` is refused and why the filter cannot be a
        # ``FilterExpression``, so there is exactly one place that decides what a
        # status is. Same division of labour as the cursor above.
        #
        # An empty ``?status=`` reads as absent, matching ``cursor``. The spelling
        # ``status`` is the one ``/api/pipelines`` already uses for its own filter
        # (``:64``), which is a *different* vocabulary over a different table — see the
        # handler's note on why the two answer an unrecognised value differently.
        status = query_params.get("status") or None
        return get_pipeline_executions(pipeline_id, page, page_size, cursor=cursor, status=status)
    elif action == "queued-files" and http_method == "GET":
        from handlers.pipelines import get_queued_files

        return get_queued_files(pipeline_id)
    elif action == "mapping-memory" and http_method == "GET":
        from handlers.mapping_memory import get_mapping_memory

        return get_mapping_memory(pipeline_id)
    elif action == "mapping-memory" and http_method == "DELETE" and len(parts) > 2:
        from handlers.mapping_memory import delete_mapping_memory_entry

        entry_id = parts[2]
        return delete_mapping_memory_entry(pipeline_id, entry_id)
    elif action == "mapping-hints" and http_method == "POST":
        return save_mapping_hints(pipeline_id, event.get("body") or "{}")
    elif action == "mappings" and http_method == "PUT":
        return update_mappings(pipeline_id, event.get("body") or "{}")
    elif action == "glue-job" and http_method == "GET":
        # Check if this is /glue-job/script
        if len(parts) > 2 and parts[2] == "script":
            from handlers.glue_jobs import get_glue_job_script

            version = query_params.get("version")
            return get_glue_job_script(pipeline_id, version)
        # Check if this is /glue-job/runs/{run_id}/logs
        elif len(parts) > 4 and parts[2] == "runs" and parts[4] == "logs":
            from handlers.glue_jobs import get_glue_job_logs

            run_id = parts[3]
            # Extract log query parameters from query string
            log_type = query_params.get("log_type", "output")
            filter_pattern = query_params.get("filter_pattern")
            start_time = int(query_params.get("start_time")) if query_params.get("start_time") else None
            end_time = int(query_params.get("end_time")) if query_params.get("end_time") else None
            limit = int(query_params.get("limit", 100))
            next_token = query_params.get("next_token")
            return get_glue_job_logs(
                pipeline_id, run_id, log_type, filter_pattern, start_time, end_time, limit, next_token
            )
        # Check if this is /glue-job/runs
        elif len(parts) > 2 and parts[2] == "runs":
            from handlers.glue_jobs import get_glue_job_runs

            # Extract pagination parameters from query string
            max_results = int(query_params.get("max_results", 25))
            next_token = query_params.get("next_token")
            return get_glue_job_runs(pipeline_id, max_results, next_token)
        else:
            # /glue-job (details)
            from handlers.glue_jobs import get_glue_job_details

            return get_glue_job_details(pipeline_id)
    elif action == "glue-job" and len(parts) > 2 and parts[2] == "run" and http_method == "POST":
        from handlers.glue_jobs import start_glue_job_run

        return start_glue_job_run(pipeline_id)
    elif action == "glue-job" and len(parts) > 2 and parts[2] == "rollback" and http_method == "POST":
        from handlers.glue_jobs import rollback_glue_job

        return rollback_glue_job(pipeline_id)
    elif action == "glue-job" and len(parts) > 2 and parts[2] == "versions" and http_method == "GET":
        from handlers.glue_jobs import get_job_version_history

        return get_job_version_history(pipeline_id)
    elif action == "glue-job" and len(parts) > 2 and parts[2] == "validate-update" and http_method == "GET":
        from handlers.glue_jobs import validate_job_update_allowed

        return validate_job_update_allowed(pipeline_id)
    elif not action and http_method == "GET":
        # Extract schema pagination parameters
        schema_page = int(query_params.get("schema_page", 1))
        schema_page_size = int(query_params.get("schema_page_size", 50))
        return get_pipeline_details(pipeline_id, schema_page, schema_page_size)
    elif not action and http_method == "PUT":
        return edit_pipeline(pipeline_id, event.get("body") or "{}", event=event)
    elif not action and http_method == "DELETE":
        from handlers.pipelines import delete_pipeline

        return delete_pipeline(pipeline_id)

    return create_response(404, {"error": f"Pipeline endpoint not found: {subpath}"})


def files_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route requests for /api/files/* endpoints"""
    query_params = event.get("queryStringParameters") or {}

    # /api/files (root)
    if not subpath:
        return get_files(query_params)

    # /api/files/{pipeline_key}/{file_key}/details
    parts = subpath.strip("/").split("/")
    if len(parts) >= 3 and parts[-1] == "details":
        pipeline_key = "/".join(parts[:-2])
        file_key = parts[-2]
        return get_file_details(pipeline_key, file_key)

    return create_response(404, {"error": f"File endpoint not found: {subpath}"})


def approvals_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route requests for /api/approvals/* endpoints.

    Ordered literal-paths-first, then the two-segment actions, then the per-id
    GET last. The per-id branch tests BOTH the segment count and the method, for
    the reason `schemas_router`'s docstring records from a live defect: its
    pre-p35 third branch tested only `http_method == "GET"` and therefore caught
    every GET under its resource, so `GET /api/schemas/{id}` returned the whole
    list. Do NOT write a path-agnostic or method-agnostic branch here.

    The per-id GET cannot shadow `approve` / `reject`: those are two segments and
    POST, this is one segment and GET, so the pairs are disjoint on BOTH
    coordinates. It cannot shadow `pending` or `cleanup` either, because both are
    matched on the whole `subpath` above it — and `"pending"` and `"cleanup"` are
    not ids the approval_handler can produce (it writes
    `{pipeline_id}_{approval_type}_{epoch}`), which is the same reasoning
    `schemas_router` uses for `GET /api/schemas/upload`.

    The `pending` branch above is deliberately left method-agnostic. Tightening
    it would change what a non-GET to that path answers today, which is a
    separate behaviour change from adding a route, and it is not needed for the
    disjointness argument: `pending` is matched and returned before `parts` is
    ever computed.

    Adding a route here REQUIRES amending the three router contract tests that
    enumerate this resource's routes — `LGR-ce8641e-1` (backfilled) records that
    those cover HTTP-method routing for all 21 resource keys, so an unamended
    addition is invisible to them:
    tests/contracts/api_responses/test_router_response_contract.py,
    test_router_mapping_prop.py and test_router_props.py.
    """
    http_method = event.get("httpMethod", "GET")
    query_params = event.get("queryStringParameters") or {}

    # /api/approvals (root)
    if not subpath:
        return get_approvals(query_params)

    # /api/approvals/pending
    if subpath == "pending":
        return get_pending_approvals()

    # /api/approvals/cleanup — DELETE orphaned ghost approvals
    if subpath == "cleanup" and http_method == "POST":
        from handlers.approvals import cleanup_ghost_approvals

        return cleanup_ghost_approvals()

    # /api/approvals/{approval_id}/*
    #
    # `split` WITHOUT an empty-segment filter, unlike `schemas_router`. Filtering
    # would widen the two POST branches below — `/api/approvals/x//approve` would
    # start routing to `approve_request` where today it 404s — and this change is
    # not permitted to alter the approve/reject flow. The per-id branch does not
    # need the filter: `.strip("/")` already handles a trailing slash.
    parts = subpath.strip("/").split("/")
    approval_id = parts[0] if parts else ""
    action = parts[1] if len(parts) > 1 else ""

    if action == "approve" and http_method == "POST":
        return approve_request(approval_id, event.get("body") or "{}", event=event)
    elif action == "reject" and http_method == "POST":
        return reject_request(approval_id, event.get("body") or "{}", event=event)

    # /api/approvals/{approval_id}                     GET  — one approval, with
    # its stored `approval_data` parsed into `details`. Last, and narrowest.
    if len(parts) == 1 and http_method == "GET":
        from handlers.approvals import get_approval

        return get_approval(parts[0])

    return create_response(404, {"error": f"Approval endpoint not found: {subpath}"})


def runs_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route requests for /api/runs/* endpoints"""
    http_method = event.get("httpMethod", "GET")

    # /api/runs/{run_id}/details
    parts = subpath.strip("/").split("/")
    run_id = parts[0] if parts else ""
    action = parts[1] if len(parts) > 1 else ""

    if action == "details" and http_method == "GET":
        from handlers.pipelines import get_run_details

        return get_run_details(run_id)

    if action == "cancel" and http_method == "POST":
        from handlers.pipelines import cancel_run

        return cancel_run(run_id, event=event)

    if action == "retry" and http_method == "POST":
        from handlers.pipelines import retry_run

        return retry_run(run_id, body=event.get("body") or "{}")

    # /api/runs/{run_id}/agent-trace — all step traces for a run
    # /api/runs/{run_id}/agent-trace/{step_name} — single step trace
    if action == "agent-trace" and http_method == "GET":
        from handlers.agent_traces import get_run_traces, get_step_trace

        step_name = parts[2] if len(parts) > 2 else ""
        if step_name:
            return get_step_trace(run_id, step_name)
        return get_run_traces(run_id)

    return create_response(404, {"error": f"Run endpoint not found: {subpath}"})


def schemas_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route requests for /api/schemas/* endpoints.

    Ordered most-specific-path-first, and EVERY branch tests the method. The
    pre-p35 third branch tested only `http_method == "GET"` and therefore
    caught every GET under /api/schemas/*, including the per-id case it was
    written for — which is why GET /api/schemas/{id} returned the whole list
    and the detail modal always described `[0]` (use-schemas.ts:38-45).
    Do NOT reintroduce a path-agnostic branch here.

    Branches 3-6 are mutually exclusive on `len(parts)` alone — 4, 2, 1, 1 —
    and the two one-segment branches are disjoint on method. The only pair
    sharing a length and a method would be 2 and 5 (`["upload"]` versus
    `[id]`), and those are disjoint on method (POST versus GET). That is why
    the ordering is safe structurally rather than by accident, and it is what
    `tests/unit/lambdas/dashboard_api/test_schemas_router_dispatch_prop.py`
    (Property 1) asserts over generated request shapes.

    Adding a route here REQUIRES amending the three router contract tests that
    enumerate ("/api/schemas", "GET") — LGR-ce8641e-1 (backfilled) records that
    those tests cover HTTP-method routing for all 21 resource keys, so an
    unamended addition is invisible to them:
    tests/contracts/api_responses/test_router_response_contract.py,
    test_router_mapping_prop.py and test_router_props.py.
    """
    http_method = event.get("httpMethod", "GET")
    parts = [p for p in subpath.strip("/").split("/") if p]

    # 1. /api/schemas                                    GET     — collection
    if not parts:
        if http_method == "GET":
            return get_schemas()
        # A POST to the collection is a method error, not a missing endpoint.
        return create_response(405, {"error": f"Method {http_method} not allowed on /api/schemas"})

    # 2. /api/schemas/upload                             POST    — literal, must precede {id}
    if parts == ["upload"] and http_method == "POST":
        return upload_schema(event)

    # 3. /api/schemas/{id}/fields/{name}/context          PUT     — R2.3
    if len(parts) == 4 and parts[1] == "fields" and parts[3] == "context" and http_method == "PUT":
        return update_field_context(parts[0], parts[2], event)

    # 4. /api/schemas/{id}/display-label                  PUT     — R4.9, D2's rename
    #    NOT PATCH /api/schemas/{id}: a PATCH on the schema resource says the
    #    schema is being modified, and D2's whole point (LGR-acca05d-2,
    #    LGR-cad8032-1) is that it is not — id, object key and bytes are frozen.
    if len(parts) == 2 and parts[1] == "display-label" and http_method == "PUT":
        return set_display_label(parts[0], event)

    # 5. /api/schemas/{id}                                GET     — R1.1
    #    GET /api/schemas/upload lands here and 404s out of get_schema("upload"),
    #    which is right rather than lucky: "upload" is not an id any source can
    #    produce, so R1.2's "404 naming the requested id" is the correct answer
    #    and no special case is needed.
    if len(parts) == 1 and http_method == "GET":
        return get_schema(parts[0])

    # 6. /api/schemas/{id}                                DELETE  — R3.1
    if len(parts) == 1 and http_method == "DELETE":
        return delete_schema(parts[0])

    return create_response(404, {"error": f"Schema endpoint not found: {subpath}"})


def notifications_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route /api/notifications/* requests."""
    http_method = event.get("httpMethod", "GET")
    query_params = event.get("queryStringParameters") or {}

    if not subpath and http_method == "GET":
        return get_notifications(query_params)

    parts = subpath.strip("/").split("/")
    notification_id = parts[0] if parts else ""
    action = parts[1] if len(parts) > 1 else ""

    if action == "read" and http_method == "PUT":
        return mark_notification_read(notification_id)

    return create_response(404, {"error": f"Notification endpoint not found: {subpath}"})


def cost_recommendations_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route requests for /api/cost-recommendations/* endpoints"""
    http_method = event.get("httpMethod", "GET")
    parts = subpath.strip("/").split("/") if subpath else [""]

    if not subpath and http_method == "GET":
        from handlers.agents import get_cost_recommendations

        return get_cost_recommendations()
    elif len(parts) >= 2 and parts[1] == "apply" and http_method == "POST":
        recommendation_id = parts[0]
        from handlers.agents import apply_cost_recommendation

        return apply_cost_recommendation(recommendation_id)

    return create_response(404, {"error": f"Cost recommendations endpoint not found: {subpath}"})


def chat_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route requests for /api/chat endpoint"""
    http_method = event.get("httpMethod", "GET")
    if http_method == "POST":
        from handlers.agents import handle_chat_message

        return handle_chat_message(event.get("body") or "{}")
    return create_response(405, {"error": "Method not allowed"})


# Resource handler registry - maps resource names to their router functions
def resources_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route requests for /api/resources/* endpoints"""
    from handlers.resources import (
        detect_schema,
        list_glue_databases,
        list_glue_tables,
        list_s3_tables_buckets,
        list_s3_tables_in_bucket,
    )

    parts = subpath.strip("/").split("/")

    # /api/resources/detect-schema
    if len(parts) >= 1 and parts[0] == "detect-schema":
        return detect_schema(event, None)

    # /api/resources/s3-tables/buckets
    if len(parts) >= 2 and parts[0] == "s3-tables" and parts[1] == "buckets":
        if len(parts) == 2:
            # List all S3 Tables buckets
            return list_s3_tables_buckets(event, None)
        elif len(parts) == 4 and parts[3] == "tables":
            # List tables in specific bucket
            # /api/resources/s3-tables/buckets/{bucket_name}/tables
            event["pathParameters"] = {"bucket_name": parts[2]}
            return list_s3_tables_in_bucket(event, None)

    # /api/resources/glue/databases
    elif len(parts) >= 2 and parts[0] == "glue" and parts[1] == "databases":
        if len(parts) == 2:
            # List all Glue databases
            return list_glue_databases(event, None)
        elif len(parts) == 4 and parts[3] == "tables":
            # List tables in specific database
            # /api/resources/glue/databases/{database_name}/tables
            event["pathParameters"] = {"database_name": parts[2]}
            return list_glue_tables(event, None)

    # /api/resources/rds/*
    elif len(parts) >= 2 and parts[0] == "rds":
        from handlers.rds_resources import (
            list_glue_jdbc_connections,
            list_rds_clusters,
            list_rds_instances,
            list_rds_secrets,
        )

        rds_subpath = parts[1]
        if rds_subpath == "instances":
            return list_rds_instances(event, None)
        elif rds_subpath == "clusters":
            return list_rds_clusters(event, None)
        elif rds_subpath == "glue-connections":
            return list_glue_jdbc_connections(event, None)
        elif rds_subpath == "secrets":
            return list_rds_secrets(event, None)
        else:
            return create_response(404, {"error": f"Unknown RDS resources endpoint: {'/'.join(parts)}"})

    # /api/resources/jdbc/detect-schema           — POST: start crawler discovery
    # /api/resources/jdbc/detect-schema/{name}    — GET: poll discovery status
    elif len(parts) >= 2 and parts[0] == "jdbc" and parts[1] == "detect-schema":
        from handlers.jdbc_resources import detect_schema_start, detect_schema_status

        http_method = event.get("httpMethod", "GET")
        if len(parts) == 2 and http_method == "POST":
            return detect_schema_start(event, None)
        elif len(parts) == 3 and http_method == "GET":
            # detect_schema_status reads event["pathParameters"]["crawler_name"];
            # set it here so the handler works when invoked through this router
            # (mirrors the s3-tables/buckets/{bucket_name}/tables pattern above).
            event["pathParameters"] = {"crawler_name": parts[2]}
            return detect_schema_status(event, None)
        else:
            return create_response(
                404,
                {"error": f"Unknown JDBC discovery endpoint: {'/'.join(parts)}"},
            )

    # /api/resources/jdbc/detect-target-schema           — POST: start target crawler (P19)
    # /api/resources/jdbc/detect-target-schema/{name}    — GET: poll target discovery
    elif len(parts) >= 2 and parts[0] == "jdbc" and parts[1] == "detect-target-schema":
        from handlers.jdbc_resources import (
            detect_target_schema_start,
            detect_target_schema_status,
        )

        http_method = event.get("httpMethod", "GET")
        if len(parts) == 2 and http_method == "POST":
            return detect_target_schema_start(event, None)
        elif len(parts) == 3 and http_method == "GET":
            # detect_target_schema_status reads
            # event["pathParameters"]["crawler_name"] — set it here so the
            # handler works when invoked through this router (mirrors the
            # source-side detect-schema pattern above).
            event["pathParameters"] = {"crawler_name": parts[2]}
            return detect_target_schema_status(event, None)
        else:
            return create_response(
                404,
                {"error": (f"Unknown JDBC target discovery endpoint: {'/'.join(parts)}")},
            )

    # /api/resources/vpc/list                          — GET: list VPCs (P20)
    # /api/resources/vpc/{vpc_id}/subnets              — GET: list subnets (P20)
    # /api/resources/vpc/{vpc_id}/security-groups      — GET: list SGs (P20)
    elif len(parts) >= 2 and parts[0] == "vpc":
        from handlers.vpc_resources import (
            list_security_groups,
            list_subnets,
            list_vpcs,
        )

        if parts == ["vpc", "list"]:
            return list_vpcs(event, None)
        if len(parts) == 3 and parts[2] == "subnets":
            # list_subnets reads event["pathParameters"]["vpc_id"] — set
            # it here so the handler works when invoked through this
            # router (mirrors the JDBC detect-schema pattern above).
            event["pathParameters"] = {"vpc_id": parts[1]}
            return list_subnets(event, None)
        if len(parts) == 3 and parts[2] == "security-groups":
            event["pathParameters"] = {"vpc_id": parts[1]}
            return list_security_groups(event, None)
        return create_response(
            404,
            {"error": f"Unknown VPC resources endpoint: {'/'.join(parts)}"},
        )

    # /api/resources/secrets/jdbc-candidates           — GET: filtered Secrets (P20)
    elif parts == ["secrets", "jdbc-candidates"]:
        from handlers.rds_resources import list_jdbc_candidate_secrets

        return list_jdbc_candidate_secrets(event, None)

    return create_response(404, {"error": f"Unknown resources endpoint: {subpath}"})


def metrics_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route requests for /api/metrics/* endpoints"""
    # /api/metrics/token-usage?period=7d
    if subpath == "token-usage":
        from handlers.agent_traces import get_token_usage_metrics

        query_params = event.get("queryStringParameters") or {}
        return get_token_usage_metrics(query_params)
    # /api/metrics/comprehensive
    if subpath == "comprehensive" or not subpath:
        return get_comprehensive_metrics(event)
    return create_response(404, {"error": f"Metrics endpoint not found: {subpath}"})


def upload_router(event: dict[str, Any], subpath: str) -> dict[str, Any]:
    """Route /api/upload/* — the single reachable presign surface (p64 R4.3).

    ONE route, ONE handler. Two dead keys used to sit beside this one —
    ``upload-url`` and ``upload-file``, both reaching ``generate_upload_url``,
    whose body contract was ``pipeline_key`` where the live caller
    (``FileUploadModal.tsx``) sends ``pipeline_id``. Worse, this key was a lambda
    whose ``else`` arm handed every request that was **not** exactly
    ``POST /api/upload/presign`` to that same handler, so ``GET
    /api/upload/presign`` — or ``POST /api/upload/anything-else`` — answered
    ``400 pipeline_key and file_name are required``: a complaint about a field the
    caller was never asked for, on a resource it had spelled correctly. Both keys
    and that handler are gone, and an unmatched request now gets a status that
    says what is actually wrong.

    EVERY branch tests the method, following ``schemas_router``'s rule and for its
    reason — its pre-p35 method-only branch caught every ``GET`` under its
    resource. ``LGR-78540f6-1`` (template / ratified) settled the same shape for
    the approvals per-id route: branches must be disjoint on BOTH segment and
    method rather than by ordering. Do NOT reintroduce a fall-through that hands
    an unmatched request to a handler with a different body contract; a 405 or a
    404 naming the subpath is the honest answer, and
    ``tests/unit/lambdas/dashboard_api/test_upload_router_dispatch.py`` asserts it
    rather than trusting it.

    Removing a route here REQUIRED amending the three router contract tests that
    enumerated ``upload-url`` / ``upload-file`` — ``LGR-ce8641e-1`` (backfilled)
    records those suites as covering HTTP-method routing for every resource key,
    so an unamended removal is invisible to them:
    ``tests/contracts/api_responses/test_router_response_contract.py``,
    ``test_router_mapping_prop.py`` and ``test_router_props.py``. All three now
    enumerate ``("/api/upload/presign", "POST")`` in its place.
    """
    http_method = event.get("httpMethod", "GET")
    parts = [p for p in subpath.strip("/").split("/") if p]

    # /api/upload/presign                                POST — the live route
    if parts == ["presign"]:
        if http_method != "POST":
            # A wrong method on a route that exists is a method error, not a
            # missing endpoint — and emphatically not a body-shape complaint.
            return create_response(405, {"error": f"Method {http_method} not allowed on /api/upload/presign"})
        return presign_upload(event)

    return create_response(404, {"error": f"Upload endpoint not found: {subpath}"})


RESOURCE_ROUTERS: dict[str, Callable] = {
    "pipelines": pipelines_router,
    "pipeline": pipelines_router,  # Alias for singular form
    "runs": runs_router,
    "files": files_router,
    "approvals": approvals_router,
    "resources": resources_router,  # NEW: AWS resources listing
    "metrics": metrics_router,  # Real CloudWatch metrics
    "errors": lambda event, subpath: get_errors(event),  # Error tracking
    "cost-recommendations": cost_recommendations_router,
    "chat": chat_router,
    # Simple resources that don't need sub-routing
    "stats": lambda event, subpath: get_stats(),
    "health": lambda event, subpath: get_health(),
    "notifications": notifications_router,
    "updates": lambda event, subpath: get_updates(event.get("queryStringParameters") or {}),
    "bedrock-models": lambda event, subpath: get_bedrock_models(),
    "schemas": lambda event, subpath: schemas_router(event, subpath),
    # p64 R4.3: one presign route, one presign handler. `upload-url` and
    # `upload-file` were deleted here — both reached a second handler with an
    # incompatible body contract, and neither appears in the deployed dashboard
    # bundle. An old caller now gets `route_request`'s 404 naming the resource,
    # which is a better answer than the 400 it used to get.
    "upload": upload_router,
    "rerun-mapping": lambda event, subpath: rerun_mapping(event),
    "trigger-pipeline": lambda event, subpath: trigger_pipeline(event),
    "code": lambda event, subpath: get_code_details(subpath.strip("/")),
    "tests": lambda event, subpath: get_test_results(subpath.strip("/")),
    "s3-content": lambda event, subpath: get_s3_content(event),
}


def get_code_details(code_version_id: str):
    """Get generated code details"""
    from handlers.pipelines import get_code_by_version

    return get_code_by_version(code_version_id)


def get_test_results(code_version_id: str):
    """Get test results"""
    from handlers.pipelines import get_tests_by_version

    return get_tests_by_version(code_version_id)


def get_s3_content(event: dict[str, Any]):
    """Get S3 content"""
    from handlers.pipelines import fetch_s3_content

    return fetch_s3_content(event)


def route_request(event: dict[str, Any]) -> dict[str, Any]:
    """Route API Gateway requests to appropriate resource handlers.

    This function implements resource-level routing that aligns with the
    API Gateway catch-all pattern. It extracts the resource name from the
    path and delegates to the appropriate resource router.

    Args:
        event: API Gateway event containing httpMethod, path, headers, body, etc.

    Returns:
        API Gateway response with statusCode, headers, and body

    Example:
        Path: /api/pipelines/my-pipeline/edit
        → resource='pipelines', subpath='my-pipeline/edit'
        → Calls: pipelines_router(event, 'my-pipeline/edit')
    """
    try:
        # Parse the request
        http_method = event.get("httpMethod", "GET")
        path = event.get("path", "")

        logger.info(f"Processing {http_method} request to {path}")

        # Handle CORS preflight requests
        if http_method == "OPTIONS":
            return create_response(200, {})

        # Extract resource and subpath from path
        # Example: /api/pipelines/my-pipeline/edit → resource='pipelines', subpath='my-pipeline/edit'
        parts = path.strip("/").split("/")

        # Validate path structure: must start with 'api'
        if len(parts) < 2 or parts[0] != "api":
            return create_response(404, {"error": "Invalid path - must start with /api/"})

        resource = parts[1]
        subpath = "/".join(parts[2:]) if len(parts) > 2 else ""

        # Look up the resource router
        router = RESOURCE_ROUTERS.get(resource)

        if not router:
            return create_response(
                404, {"error": f"Unknown resource: {resource}", "available_resources": list(RESOURCE_ROUTERS.keys())}
            )

        # Delegate to resource-specific router
        logger.info(f"Routing to {resource} handler with subpath: {subpath}")
        return router(event, subpath)

    except Exception as e:
        logger.error(f"Error routing request: {e!s}")
        return create_response(500, {"error": "Internal server error", "details": str(e)})
