# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: Router path-to-handler mapping.

Validates that every frontend-consumed URL path routes to the correct handler,
singular/plural path variants route to the same handler, unknown paths return
404, and HTTP method routing works correctly.

Task 7.3 — Requirements 23.1, 23.2, 23.3, 23.4
"""

import json
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

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

sys.modules.pop("lambda_function", None)

from router import RESOURCE_ROUTERS, route_request


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


# ===========================================================================
# Tests
# ===========================================================================


class TestResourceRouterRegistry:
    """All expected resource keys exist in RESOURCE_ROUTERS."""

    def test_all_frontend_resources_registered(self):
        expected = {
            "pipelines",
            "pipeline",
            "runs",
            "files",
            "approvals",
            "resources",
            "metrics",
            "errors",
            "stats",
            "health",
            "notifications",
            "updates",
            "bedrock-models",
            "schemas",
            # p64 task 1d.3 removed "upload-url" and "upload-file" from this set.
            # Amended in place rather than left passing, per LGR-60c4985-6: both
            # keys reached `generate_upload_url`, whose body contract was
            # `pipeline_key` where the live caller sends `pipeline_id`, and
            # neither appears in the deployed dashboard bundle. `expected` is
            # checked as a subset (`missing = expected - keys`), so leaving them
            # here would have failed rather than gone quiet — the reason is
            # recorded here so the next reader does not re-add them to "fix" it.
            "upload",
            "trigger-pipeline",
            "code",
            "tests",
            "s3-content",
        }
        missing = expected - set(RESOURCE_ROUTERS.keys())
        assert not missing, f"Missing resource routers: {missing}"

    def test_pipeline_singular_and_plural_share_router(self):
        """Both 'pipeline' and 'pipelines' map to the same router function."""
        assert RESOURCE_ROUTERS["pipeline"] is RESOURCE_ROUTERS["pipelines"]


class TestSingularPluralAliases:
    """Singular and plural path variants route to the same handler."""

    def test_pipeline_and_pipelines_list_route_same(self):
        """Both /api/pipeline and /api/pipelines route to pipelines_router (list)."""
        resp1 = route_request(_make_event("/api/pipelines"))
        resp2 = route_request(_make_event("/api/pipeline"))
        s1, _ = _parse(resp1)
        s2, _ = _parse(resp2)
        # Both should succeed (not 404) — they hit get_pipelines
        assert s1 != 404
        assert s2 != 404

    def test_exactly_one_upload_resource_key_is_registered(self):
        """Only 'upload' remains; the two aliases are gone (p64 R4.3).

        **Amended in place from ``test_upload_url_and_upload_file_are_aliases``,
        whose subject was inverted, not merely stale.** It asserted the two
        aliases were registered and called them aliases of each other. They were
        aliases of a *second handler* reading ``pipeline_key``, while the live
        route reads ``pipeline_id`` — so this assertion was pinning the very
        duplication R4.3 exists to remove. Restated as a deliberate reversal per
        ``LGR-60c4985-6`` / ``LGR-5906ec6-2`` (both template / ratified), with the
        reason at the assertion rather than in a changelog.
        """
        assert "upload" in RESOURCE_ROUTERS
        assert "upload-url" not in RESOURCE_ROUTERS
        assert "upload-file" not in RESOURCE_ROUTERS

    def test_metrics_root_and_comprehensive_alias(self):
        """Both /api/metrics and /api/metrics/comprehensive route successfully."""
        resp1 = route_request(_make_event("/api/metrics"))
        resp2 = route_request(_make_event("/api/metrics/comprehensive"))
        s1, _ = _parse(resp1)
        s2, _ = _parse(resp2)
        assert s1 != 404
        assert s2 != 404


class TestUnknownPaths:
    """Unknown resources return 404."""

    def test_unknown_resource_returns_404(self):
        resp = route_request(_make_event("/api/nonexistent"))
        status, body = _parse(resp)
        assert status == 404
        assert "error" in body
        assert "available_resources" in body

    def test_invalid_path_returns_404(self):
        resp = route_request(_make_event("/invalid"))
        status, body = _parse(resp)
        assert status == 404

    def test_unknown_pipeline_subpath_returns_404(self):
        resp = route_request(_make_event("/api/pipelines/my-pipe/nonexistent", method="DELETE"))
        status, body = _parse(resp)
        assert status == 404


class TestHTTPMethodRouting:
    """Correct HTTP methods route to correct handlers (not 404)."""

    def test_get_pipelines_routes(self):
        resp = route_request(_make_event("/api/pipelines", method="GET"))
        status, _ = _parse(resp)
        assert status != 404, "GET /api/pipelines should not return 404"

    def test_post_pipelines_routes(self):
        resp = route_request(_make_event("/api/pipelines", method="POST", body={"pipeline_name": "test"}))
        status, _ = _parse(resp)
        assert status != 404, "POST /api/pipelines should not return 404"

    def test_put_pipeline_edit_routes(self):
        resp = route_request(_make_event("/api/pipelines/my-pipe/edit", method="PUT", body={}))
        status, _ = _parse(resp)
        assert status != 404, "PUT /api/pipelines/{id}/edit should not return 404"

    def test_get_pipeline_executions_routes(self):
        resp = route_request(_make_event("/api/pipeline/test-pipe/executions", method="GET"))
        status, _ = _parse(resp)
        assert status != 404, "GET /api/pipeline/{id}/executions should not return 404"

    def test_get_pipeline_queued_files_routes(self):
        resp = route_request(_make_event("/api/pipeline/test-pipe/queued-files", method="GET"))
        status, _ = _parse(resp)
        assert status != 404, "GET /api/pipeline/{id}/queued-files should not return 404"

    def test_post_approval_approve_routes(self):
        """POST /api/approvals/{id}/approve routes to approve_request handler.

        The handler returns 404 when the approval_id doesn't exist in DynamoDB,
        which is correct handler behavior (not a routing 404). We verify the
        response body contains 'Approval not found' to confirm routing worked.
        """
        resp = route_request(_make_event("/api/approvals/appr-001/approve", method="POST", body={}))
        status, body = _parse(resp)
        # 404 from handler (approval not found) is expected — not a routing error
        if status == 404:
            assert "Approval not found" in body.get("error", ""), (
                "Got 404 but not from approval handler — routing may be broken"
            )

    def test_post_approval_reject_routes(self):
        """POST /api/approvals/{id}/reject routes to reject_request handler."""
        resp = route_request(_make_event("/api/approvals/appr-001/reject", method="POST", body={}))
        status, body = _parse(resp)
        if status == 404:
            assert "Approval not found" in body.get("error", ""), (
                "Got 404 but not from approval handler — routing may be broken"
            )

    def test_get_approvals_pending_routes(self):
        resp = route_request(_make_event("/api/approvals/pending", method="GET"))
        status, _ = _parse(resp)
        assert status != 404, "GET /api/approvals/pending should not return 404"


class TestCORSPreflight:
    """OPTIONS requests return 200 for CORS preflight."""

    def test_options_returns_200(self):
        resp = route_request(_make_event("/api/pipelines", method="OPTIONS"))
        assert resp["statusCode"] == 200


class TestFrontendConsumedPaths:
    """Every path the frontend calls routes successfully (not a routing 404)."""

    @pytest.mark.parametrize(
        "path,method",
        [
            ("/api/pipelines", "GET"),
            ("/api/pipeline/test-pipe/executions", "GET"),
            ("/api/pipeline/test-pipe/queued-files", "GET"),
            ("/api/approvals", "GET"),
            ("/api/approvals/pending", "GET"),
            # p56 — the per-id GET `approvals_router` gained. Added for the same
            # reason the four `schemas` pairs below were: LGR-ce8641e-1
            # (backfilled) records this suite as covering HTTP-method routing for
            # all 21 resource keys, so a route that is not enumerated here is
            # invisible to it. Like `GET /api/schemas/some-id`, this answers a
            # HANDLER 404 naming the id — which the predicate below already
            # tolerates and a routing 404 still fails.
            ("/api/approvals/some-approval-id", "GET"),
            ("/api/stats", "GET"),
            ("/api/health", "GET"),
            ("/api/metrics", "GET"),
            ("/api/metrics/comprehensive", "GET"),
            ("/api/errors", "GET"),
            ("/api/bedrock-models", "GET"),
            ("/api/schemas", "GET"),
            # p35 task 4.2 — the four routes schemas_router gained. Added here
            # because LGR-ce8641e-1 (backfilled) records this suite as covering
            # HTTP-method routing for all 21 resource keys, so a new route that
            # is not enumerated is invisible to it.
            ("/api/schemas/some-id", "GET"),
            ("/api/schemas/some-id", "DELETE"),
            ("/api/schemas/some-id/fields/email/context", "PUT"),
            ("/api/schemas/some-id/display-label", "PUT"),
            ("/api/notifications", "GET"),
            ("/api/updates", "GET"),
        ],
    )
    def test_frontend_path_routes_successfully(self, path, method):
        """Every enumerated path reaches a handler rather than a routing 404.

        **Amended by p35 task 4.2 — the subject is strengthened, not changed.**
        Four `(path, method)` pairs were added above: the per-id `GET` (R1.1),
        the per-id `DELETE` (R3.1), the field-context `PUT` (R2.3) and the
        display-label `PUT` (R4.9). Before p35, `schemas_router` had three
        branches and this list carried `("/api/schemas", "GET")` alone, so the
        subject — *every route this resource exposes is reachable through the
        router* — was satisfied by one route because there was one route.

        **The assertion moved with the addition, and this is the reversal
        stated** (`LGR-5906ec6-2`, `LGR-60c4985-6`, both template / ratified):
        it was `status != 404` and it is now "not a *routing* 404", the same
        distinction `test_router_mapping_prop.py` and `test_router_props.py`
        already drew before p35. It had to move because
        `GET /api/schemas/some-id` for an id no schema source can produce is
        required by R1.2 to answer `404` naming that id — a *handler* 404, and
        the correct answer. Asserting `status != 404` on it would have pinned
        the opposite of the requirement. A routing 404 is still a failure here:
        those carry `available_resources` or `Unknown resource`, and the
        `schemas_router` fall-through carries `Schema endpoint not found`, so
        all three are still caught.
        """
        resp = route_request(_make_event(path, method=method))
        status, body = _parse(resp)
        if status == 404:
            error = body.get("error", "")
            is_routing_404 = (
                "available_resources" in body or "Unknown resource" in error or "endpoint not found" in error
            )
            assert not is_routing_404, f"{method} {path} returned a routing 404: {body}"
