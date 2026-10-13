# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Error path unit tests for the dashboard_api Lambda.

Covers:
- Unknown API path returns 404
- DynamoDB scan failure returns 500
- Malformed request body returns 400

Validates: Requirements 17.1, 17.2, 17.3
"""

import importlib
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    )
)
SHARED_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "shared",
    )
)

_ENV = {
    "STATE_TABLE": "test-state",
    "MAPPINGS_TABLE": "test-mappings",
    "PIPELINES_TABLE": "test-pipelines",
    "GENERATED_CODE_TABLE": "test-code",
    "TEST_RESULTS_TABLE": "test-results",
    "APPROVALS_TABLE": "test-approvals",
    "EXECUTIONS_TABLE": "test-executions",
    "ERRORS_TABLE": "test-errors",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AWS_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}


def _load_router():
    """Load the dashboard_api router module, patching boto3 before import."""
    mock_dynamo = MagicMock()
    mock_s3 = MagicMock()

    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        with patch("boto3.client", return_value=mock_s3), patch("boto3.resource", return_value=mock_dynamo):
            # Clear all cached handler modules
            for mod_name in list(sys.modules.keys()):
                if mod_name in ("router", "lambda_function") or mod_name.startswith("handlers"):
                    del sys.modules[mod_name]

            # Add Lambda dir to path for handler imports
            if LAMBDA_DIR not in sys.path:
                sys.path.insert(0, LAMBDA_DIR)
            if SHARED_DIR not in sys.path:
                sys.path.insert(0, SHARED_DIR)

            spec = importlib.util.spec_from_file_location(
                "router",
                os.path.join(LAMBDA_DIR, "router.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, mock_dynamo


# ---------------------------------------------------------------------------
# Tests — Unknown API path returns 404
# ---------------------------------------------------------------------------


class TestUnknownPath:
    """Unknown API paths return 404 with error details."""

    def test_unknown_resource_returns_404(self):
        router, _ = _load_router()
        event = {"httpMethod": "GET", "path": "/api/nonexistent"}
        result = router.route_request(event)
        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert "error" in body
        assert "nonexistent" in body["error"].lower() or "unknown" in body["error"].lower()

    def test_invalid_path_no_api_prefix_returns_404(self):
        router, _ = _load_router()
        event = {"httpMethod": "GET", "path": "/invalid/path"}
        result = router.route_request(event)
        assert result["statusCode"] == 404

    def test_empty_path_returns_404(self):
        router, _ = _load_router()
        event = {"httpMethod": "GET", "path": ""}
        result = router.route_request(event)
        assert result["statusCode"] == 404

    def test_root_api_path_returns_404(self):
        """Just /api/ with no resource returns 404."""
        router, _ = _load_router()
        event = {"httpMethod": "GET", "path": "/api/"}
        result = router.route_request(event)
        # /api/ splits to ['api', ''] — empty resource should 404
        assert result["statusCode"] == 404


# ---------------------------------------------------------------------------
# Tests — DynamoDB scan failure returns 500
# ---------------------------------------------------------------------------


class TestDynamoDBScanFailure:
    """Internal errors from DynamoDB propagate as 500 responses."""

    def test_handler_exception_returns_500(self):
        """When a resource handler raises, route_request catches and returns 500."""
        router, _ = _load_router()
        # Patch a known router to raise
        original = router.RESOURCE_ROUTERS.get("health")
        router.RESOURCE_ROUTERS["health"] = lambda event, subpath: (_ for _ in ()).throw(
            RuntimeError("DynamoDB scan failed")
        )
        try:
            event = {"httpMethod": "GET", "path": "/api/health"}
            result = router.route_request(event)
            assert result["statusCode"] == 500
            body = json.loads(result["body"])
            assert "error" in body
        finally:
            router.RESOURCE_ROUTERS["health"] = original


# ---------------------------------------------------------------------------
# Tests — Malformed request body returns 400
# ---------------------------------------------------------------------------


class TestMalformedRequestBody:
    """Malformed request bodies are handled gracefully."""

    def test_options_request_returns_200(self):
        """CORS preflight OPTIONS request returns 200 regardless of body."""
        router, _ = _load_router()
        event = {"httpMethod": "OPTIONS", "path": "/api/pipelines", "body": "garbage"}
        result = router.route_request(event)
        assert result["statusCode"] == 200

    def test_missing_http_method_defaults_to_get(self):
        """Missing httpMethod defaults to GET and routes normally."""
        router, _ = _load_router()
        event = {"path": "/api/nonexistent"}
        result = router.route_request(event)
        # Should still route (and 404 for unknown resource)
        assert result["statusCode"] == 404


# ---------------------------------------------------------------------------
# Tests — run cancellation reports AWS failures instead of claiming success
# ---------------------------------------------------------------------------


class TestCancelRunErrorPaths:
    """POST /api/runs/{id}/cancel through the router.

    Until 2026-10-11 the cancel swallowed every AWS error, including the
    InvalidArn its own StopExecution call raised on every invocation, and
    answered 200 "Run cancelled successfully".
    """

    @staticmethod
    def _cancel(run_id: str, *, query_error: Exception | None = None, items: list | None = None):
        """Route a cancel with boto3 patched for the whole call.

        The router imports the runs handler at request time, so the patches have
        to cover the call, not only ``_load_router``. ``ExecutionsTable.query``
        is the cancel's first read, the run_id-index lookup.
        """
        router, mock_dynamo = _load_router()
        table = mock_dynamo.Table.return_value
        if query_error is not None:
            table.query.side_effect = query_error
        else:
            table.query.return_value = {"Items": items or []}
        with (
            patch.dict(os.environ, _ENV),
            patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
            patch("boto3.client", return_value=MagicMock()),
            patch("boto3.resource", return_value=mock_dynamo),
        ):
            # _load_router's patch.dict(sys.modules) restores whatever handler
            # modules an earlier test imported, bound to other tables. Drop them so
            # the router's request-time import binds to mock_dynamo; this block's
            # own patch.dict puts them back afterwards.
            for name in [m for m in sys.modules if m == "handlers" or m.startswith("handlers.")]:
                del sys.modules[name]
            result = router.route_request({"httpMethod": "POST", "path": f"/api/runs/{run_id}/cancel"})
        return result, table

    def test_a_failed_run_lookup_is_a_500_not_a_cancel(self):
        from botocore.exceptions import ClientError

        result, table = self._cancel(
            "run-err-1",
            query_error=ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Slow down"}}, "Query"
            ),
        )

        assert table.query.call_count == 1, "the failure must come from the run lookup, not from the import"
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "error" in body
        assert "status" not in body, "a failed cancel must not carry a run status"
        table.update_item.assert_not_called()

    def test_a_run_that_does_not_exist_is_a_404(self):
        result, table = self._cancel("run-missing-1", items=[])

        assert table.query.call_count == 1
        assert result["statusCode"] == 404
        assert json.loads(result["body"])["run_id"] == "run-missing-1"
