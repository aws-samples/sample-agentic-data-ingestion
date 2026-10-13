# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: router path-to-handler mapping (Property 36).

Feature: testing-overhaul, Property 36: Router path-to-handler mapping

For any (URL path, HTTP method) pair consumed by the frontend, the router
maps to the correct handler. Unknown paths return 404.

Uses hypothesis to generate random unknown resource names and verify they
all return 404. Uses a parametrized list of known frontend paths and
verifies they don't return 404.

Validates: Requirements 23.1, 23.2, 23.3, 23.4
"""

import json
import os
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Environment setup — must happen before handler imports
# ---------------------------------------------------------------------------
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
os.environ.setdefault("ERRORS_TABLE", "agentic_data_ingestion_errors")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

sys.modules.pop("lambda_function", None)

from router import RESOURCE_ROUTERS, route_request

# ---------------------------------------------------------------------------
# Known resource names registered in the router
# ---------------------------------------------------------------------------
KNOWN_RESOURCES = set(RESOURCE_ROUTERS.keys())

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(path, method="GET", body=None, query_params=None):
    """Build a minimal API Gateway event."""
    return {
        "path": path,
        "httpMethod": method,
        "headers": {"Content-Type": "application/json"},
        "queryStringParameters": query_params or {},
        "body": json.dumps(body) if body else None,
    }


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate resource names that are NOT in the known set
_unknown_resource_st = st.from_regex(r"[a-z]{3,15}", fullmatch=True).filter(lambda r: r not in KNOWN_RESOURCES)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestUnknownPathsReturn404:
    """Property 36 — unknown paths always return 404."""

    @given(resource=_unknown_resource_st)
    @settings(max_examples=50)
    def test_unknown_resource_returns_404(self, resource):
        """**Validates: Requirements 23.3**

        For any resource name not in RESOURCE_ROUTERS, route_request returns 404.
        """
        resp = route_request(_make_event(f"/api/{resource}"))
        status, body = _parse(resp)
        assert status == 404, f"Expected 404 for unknown resource '{resource}', got {status}"
        assert "error" in body

    @given(resource=_unknown_resource_st, suffix=st.from_regex(r"[a-z0-9\-]{1,10}", fullmatch=True))
    @settings(max_examples=50)
    def test_unknown_resource_with_subpath_returns_404(self, resource, suffix):
        """**Validates: Requirements 23.3**

        For any unknown resource with an arbitrary subpath, route_request returns 404.
        """
        resp = route_request(_make_event(f"/api/{resource}/{suffix}"))
        status, body = _parse(resp)
        assert status == 404, f"Expected 404 for unknown path '/api/{resource}/{suffix}', got {status}"


class TestKnownFrontendPathsDoNotReturn404:
    """Property 36 — known frontend paths never return 404 from the router."""

    # These are the paths the frontend actually calls. The handler may return
    # a non-200 status (e.g. 500 if DynamoDB is not mocked), but the router
    # itself should NOT return 404 for these paths.
    FRONTEND_PATHS = [
        ("/api/pipelines", "GET"),
        ("/api/pipeline/test-pipe/executions", "GET"),
        ("/api/pipeline/test-pipe/queued-files", "GET"),
        ("/api/approvals", "GET"),
        ("/api/approvals/pending", "GET"),
        # p56 — the per-id GET `approvals_router` gained. Added because
        # LGR-ce8641e-1 (backfilled) records this suite as covering HTTP-method
        # routing for all 21 resource keys, so an unenumerated route is invisible
        # to it. Answers a handler 404 naming the id, which the predicate below
        # already distinguishes from a routing 404.
        ("/api/approvals/some-approval-id", "GET"),
        ("/api/stats", "GET"),
        ("/api/health", "GET"),
        ("/api/metrics", "GET"),
        ("/api/metrics/comprehensive", "GET"),
        ("/api/errors", "GET"),
        ("/api/bedrock-models", "GET"),
        ("/api/schemas", "GET"),
        # p35 task 4.2 — the four routes schemas_router gained. Added because
        # LGR-ce8641e-1 (backfilled) records this suite as covering HTTP-method
        # routing for all 21 resource keys, so an unenumerated route is
        # invisible to it.
        ("/api/schemas/some-id", "GET"),
        ("/api/schemas/some-id", "DELETE"),
        ("/api/schemas/some-id/fields/email/context", "PUT"),
        ("/api/schemas/some-id/display-label", "PUT"),
        ("/api/notifications", "GET"),
        ("/api/updates", "GET"),
        # p64 task 1d.3 — the live presign route, which this list had never
        # enumerated: `upload` was represented here only by the two dead aliases
        # asserted below, and those are now gone. Added for the reason the two
        # notes above give — an unenumerated route is invisible to this suite, and
        # after the collapse this is the ONLY route under `upload`. Answers a 500
        # here (no body on the generated event), which this test accepts by
        # design; what it pins is that the router reaches a handler at all.
        ("/api/upload/presign", "POST"),
    ]

    @pytest.mark.parametrize("path,method", FRONTEND_PATHS)
    def test_frontend_path_does_not_return_routing_404(self, path, method):
        """**Validates: Requirements 23.1, 23.2**

        For any (URL path, HTTP method) pair consumed by the frontend,
        the router maps to a handler (not a 404 routing error).

        **Amended by p35 task 4.2 — the subject is strengthened, not changed.**
        Four pairs were added to ``FRONTEND_PATHS``: the per-id ``GET`` (R1.1),
        the per-id ``DELETE`` (R3.1), the field-context ``PUT`` (R2.3) and the
        display-label ``PUT`` (R4.9). Before p35 this list carried
        ``("/api/schemas", "GET")`` alone, because ``schemas_router`` had three
        branches. The routing-404 predicate gained one clause — a resource
        router's own ``"<Resource> endpoint not found"`` fall-through is a
        routing failure too, and without it a route removed from
        ``schemas_router`` would land there and read as an acceptable handler
        404, which is the invisibility these additions exist to remove. A
        genuine handler 404 still passes, which is what lets
        ``GET /api/schemas/some-id`` answer R1.2's ``404`` naming the requested
        id. Stated as a deliberate reversal per ``LGR-5906ec6-2`` /
        ``LGR-60c4985-6`` (both template / ratified).
        """
        resp = route_request(_make_event(path, method=method))
        status, body = _parse(resp)
        # A 404 from the *router* includes 'Unknown resource', 'available_resources',
        # or a resource router's own '<Resource> endpoint not found' fall-through.
        # A 404 from a *handler* (e.g. "Approval not found", or R1.2's "Schema not
        # found: {id}") is acceptable.
        if status == 404:
            error = body.get("error", "")
            is_routing_404 = (
                "available_resources" in body or "Unknown resource" in error or "endpoint not found" in error
            )
            assert not is_routing_404, f"{method} {path} returned a routing 404: {body}"


class TestSingularPluralAliases:
    """Property 36 — singular/plural path variants route to the same handler."""

    def test_pipeline_and_pipelines_share_router(self):
        """**Validates: Requirements 23.2**"""
        assert RESOURCE_ROUTERS["pipeline"] is RESOURCE_ROUTERS["pipelines"]

    def test_exactly_one_upload_resource_key_is_registered(self):
        """**Validates: Requirements 23.2**

        **Amended in place from ``test_upload_url_and_upload_file_both_registered``
        by p64 task 1d.3 — a deliberate reversal, not a stale expectation.** That
        test asserted the two aliases were registered. They were aliases of a
        *second* presign handler, ``generate_upload_url``, which read
        ``pipeline_key`` where the live caller sends ``pipeline_id``, and they
        appear nowhere in the deployed dashboard bundle. So the old subject pinned
        the duplication R4.3 exists to remove. Restated per ``LGR-60c4985-6`` /
        ``LGR-5906ec6-2`` (both template / ratified), with the reason at the
        assertion. An old caller of either alias now gets ``route_request``'s 404
        naming the resource, which is why removing them costs no diagnosability.
        """
        assert "upload" in RESOURCE_ROUTERS
        assert "upload-url" not in RESOURCE_ROUTERS
        assert "upload-file" not in RESOURCE_ROUTERS


class TestOptionsPreflightNever404:
    """Property 36 — OPTIONS requests never return 404."""

    @given(resource=st.sampled_from(sorted(KNOWN_RESOURCES)))
    @settings(max_examples=50)
    def test_options_returns_200_for_known_resources(self, resource):
        """**Validates: Requirements 23.4**

        For any known resource, OPTIONS returns 200 (CORS preflight).
        """
        resp = route_request(_make_event(f"/api/{resource}", method="OPTIONS"))
        assert resp["statusCode"] == 200
