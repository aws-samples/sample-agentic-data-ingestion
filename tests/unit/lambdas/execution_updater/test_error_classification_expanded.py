# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for expanded error classification categories.

Verifies:
- Each new category (authentication, schema_mismatch, resource_quota,
  data_quality, network) matches its expected patterns.
- Existing categories (permission, timeout, data_format, code_error,
  infrastructure, throttling) still work unchanged.
- Precedence: new specific categories are evaluated before existing
  broader ones (authentication before permission, network before timeout).

Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7
"""

import importlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (same pattern as sibling test files)
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_execution_updater",
    )
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

_ENV = {
    "EXECUTIONS_TABLE": "test-executions",
    "PIPELINES_TABLE": "test-pipelines",
    "ERRORS_TABLE": "test-errors",
    "STATE_TABLE": "test-state",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}


def _load_module():
    """Import lambda_function with boto3 and powertools mocked out."""
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = MagicMock()

    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        with patch("boto3.resource", return_value=mock_dynamo):
            sys.modules.pop("lambda_function", None)
            if SHARED_DIR not in sys.path:
                sys.path.insert(0, SHARED_DIR)
            spec = importlib.util.spec_from_file_location(
                "lambda_function",
                os.path.join(LAMBDA_DIR, "lambda_function.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod


# Load once for the entire module — classify_error is a pure function.
_mod = _load_module()
classify_error = _mod.classify_error


# ---------------------------------------------------------------------------
# Tests — New category: authentication (Req 8.5)
# ---------------------------------------------------------------------------


class TestAuthenticationCategory:
    """classify_error returns 'authentication' for auth-related errors."""

    @pytest.mark.parametrize(
        "message",
        [
            "ExpiredToken: The security token included in the request is expired",
            "InvalidSignature: Signature verification failed",
            "SignatureDoesNotMatch: The request signature we calculated does not match",
            "TokenExpired: Token has expired and needs to be refreshed",
            "UnrecognizedClientException: The security token is not valid",
        ],
    )
    def test_authentication_patterns(self, message):
        assert classify_error(message) == "authentication"

    def test_authentication_case_insensitive(self):
        assert classify_error("expiredtoken error occurred") == "authentication"


# ---------------------------------------------------------------------------
# Tests — New category: schema_mismatch (Req 8.1)
# ---------------------------------------------------------------------------


class TestSchemaMismatchCategory:
    """classify_error returns 'schema_mismatch' for schema mismatch errors."""

    @pytest.mark.parametrize(
        "message",
        [
            "SchemaMismatch: source and target schemas differ",
            "field type conflict between columns",
            "incompatible schema detected for table",
            "column mismatch in target table",
        ],
    )
    def test_schema_mismatch_patterns(self, message):
        assert classify_error(message) == "schema_mismatch"

    def test_schema_mismatch_case_insensitive(self):
        assert classify_error("SCHEMAMISMATCH found") == "schema_mismatch"


# ---------------------------------------------------------------------------
# Tests — New category: resource_quota (Req 8.2)
# ---------------------------------------------------------------------------


class TestResourceQuotaCategory:
    """classify_error returns 'resource_quota' for quota/limit errors."""

    @pytest.mark.parametrize(
        "message",
        [
            "LimitExceeded: Rate exceeded for API calls",
            "ServiceQuotaExceeded: You have exceeded a service quota",
            "ProvisionedThroughputExceeded: throughput exceeded for table",
            "ItemCollectionSizeLimitExceeded: collection too large",
        ],
    )
    def test_resource_quota_patterns(self, message):
        assert classify_error(message) == "resource_quota"

    def test_resource_quota_case_insensitive(self):
        assert classify_error("limitexceeded on DynamoDB") == "resource_quota"


# ---------------------------------------------------------------------------
# Tests — New category: data_quality (Req 8.3)
# ---------------------------------------------------------------------------


class TestDataQualityCategory:
    """classify_error returns 'data_quality' for data quality errors."""

    @pytest.mark.parametrize(
        "message",
        [
            "QualityCheck failed for dataset",
            "quality rule violation detected",
            "data quality threshold not met",
            "constraint violation on column age",
        ],
    )
    def test_data_quality_patterns(self, message):
        assert classify_error(message) == "data_quality"

    def test_data_quality_case_insensitive(self):
        assert classify_error("QUALITYCHECK failure") == "data_quality"


# ---------------------------------------------------------------------------
# Tests — New category: network (Req 8.4)
# ---------------------------------------------------------------------------


class TestNetworkCategory:
    """classify_error returns 'network' for network-related errors."""

    @pytest.mark.parametrize(
        "message",
        [
            "ConnectionTimeout: Could not connect to endpoint",
            "ConnectTimeoutError: Connect timeout on endpoint URL",
            "DNSError: Failed to resolve hostname",
            "EndpointConnectionError: Could not connect to endpoint",
            "ConnectionRefused: Connection was refused by server",
        ],
    )
    def test_network_patterns(self, message):
        assert classify_error(message) == "network"

    def test_network_case_insensitive(self):
        assert classify_error("dnserror while resolving") == "network"


# ---------------------------------------------------------------------------
# Tests — Existing categories preserved (Req 8.6)
# ---------------------------------------------------------------------------


class TestExistingCategoriesPreserved:
    """Existing error categories still classify correctly after changes."""

    @pytest.mark.parametrize(
        "message, expected",
        [
            ("AccessDenied: User is not authorized", "permission"),
            ("Unauthorized access attempt", "permission"),
            ("Forbidden: insufficient permissions", "permission"),
            ("TimedOut waiting for response", "timeout"),
            ("JSONDecodeError: Expecting value", "data_format"),
            ("SyntaxError: invalid syntax in generated code", "code_error"),
            ("NoSuchBucket: The specified bucket does not exist", "infrastructure"),
            ("ThrottlingException: Rate exceeded", "throttling"),
        ],
    )
    def test_existing_category_still_works(self, message, expected):
        assert classify_error(message) == expected

    def test_unknown_error_returns_unknown(self):
        assert classify_error("Something completely unexpected happened") == "unknown"

    def test_empty_message_returns_unknown(self):
        assert classify_error("") == "unknown"


# ---------------------------------------------------------------------------
# Tests — Precedence: new categories before existing (Req 8.7)
# ---------------------------------------------------------------------------


class TestPrecedenceOrder:
    """New specific categories take precedence over broader existing ones."""

    def test_authentication_before_permission(self):
        """'Unauthorized' matches both authentication and permission patterns,
        but authentication is checked first only when auth-specific tokens appear.
        An ExpiredToken + Unauthorized message should classify as authentication."""
        msg = "ExpiredToken: Unauthorized - token has expired"
        assert classify_error(msg) == "authentication"

    def test_network_before_timeout(self):
        """ConnectionTimeout matches both network and timeout patterns,
        but network is checked first."""
        msg = "ConnectionTimeout: timed out connecting to endpoint"
        assert classify_error(msg) == "network"

    def test_pure_timeout_still_timeout(self):
        """A plain timeout message (no network keywords) still classifies as timeout."""
        msg = "TimeoutError: Lambda function timed out after 300 seconds"
        assert classify_error(msg) == "timeout"

    def test_pure_permission_still_permission(self):
        """A plain permission message (no auth keywords) still classifies as permission."""
        msg = "AccessDenied: User does not have permission"
        assert classify_error(msg) == "permission"

    def test_dict_order_matches_expected(self):
        """Verify the dict key order has new categories before existing ones."""
        keys = list(_mod._ERROR_PATTERNS.keys())
        # New categories should come first
        assert keys.index("authentication") < keys.index("permission")
        assert keys.index("network") < keys.index("timeout")
        assert keys.index("schema_mismatch") < keys.index("data_format")
        assert keys.index("resource_quota") < keys.index("throttling")
        assert keys.index("data_quality") < keys.index("data_format")
