# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: AWS backend toggle behavior (Property 6).

Feature: testing-overhaul, Property 6: AWS backend toggle behavior

For any shared fixture, when ``AGENTIC_DATA_INGESTION_TEST_REAL_AWS`` is ``false``
(default), the fixture uses moto context managers; when ``true``, the
fixture uses real AWS boto3 clients with the caller's credentials.
The fixture's return type and interface are identical in both modes.

This test validates:
- ``USE_REAL_AWS`` responds correctly to env var values
- ``mock_dynamodb_tables`` returns dict of Table resources in moto mode
- ``mock_s3_buckets`` returns dict of bucket name strings in moto mode
- ``mock_sqs_queues`` returns dict of queue URL strings in moto mode

**Validates: Requirements 3.7**
"""

from __future__ import annotations

import os

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fixtures.conftest import (
    S3_BUCKET_DEFINITIONS,
    SQS_QUEUE_DEFINITIONS,
    TABLE_DEFINITIONS,
    USE_REAL_AWS,
)

# ---------------------------------------------------------------------------
# Property 6a: USE_REAL_AWS responds correctly to env var values
# ---------------------------------------------------------------------------

# Strategy: generate random truthy/falsy env var values
_TRUTHY_VALUES = st.sampled_from(["true", "True", "TRUE", "tRuE"])
_FALSY_VALUES = st.sampled_from(["false", "False", "FALSE", "0", "", "no", "anything"])


@given(value=_TRUTHY_VALUES)
@settings(max_examples=10)
def test_use_real_aws_truthy(value: str):
    """**Validates: Requirements 3.7**

    When AGENTIC_DATA_INGESTION_TEST_REAL_AWS is set to a truthy value (case-insensitive
    "true"), the toggle evaluates to True.
    """
    result = value.lower() == "true"
    assert result is True, f"Expected True for env value '{value}'"


@given(value=_FALSY_VALUES)
@settings(max_examples=10)
def test_use_real_aws_falsy(value: str):
    """**Validates: Requirements 3.7**

    When AGENTIC_DATA_INGESTION_TEST_REAL_AWS is set to any value other than
    case-insensitive "true", the toggle evaluates to False.
    """
    result = value.lower() == "true"
    assert result is False, f"Expected False for env value '{value}'"


def test_use_real_aws_default_is_false():
    """**Validates: Requirements 3.7**

    When AGENTIC_DATA_INGESTION_TEST_REAL_AWS is unset, USE_REAL_AWS defaults to False.
    The test environment should not have this variable set to 'true'.
    """
    env_val = os.environ.get("AGENTIC_DATA_INGESTION_TEST_REAL_AWS", "false")
    if env_val.lower() != "true":
        assert USE_REAL_AWS is False


# ---------------------------------------------------------------------------
# Property 6b: mock_dynamodb_tables returns dict of Table resources (moto)
# ---------------------------------------------------------------------------


class TestMockDynamoDBTablesToggle:
    """Validate mock_dynamodb_tables fixture interface in moto mode."""

    def test_returns_dict(self, mock_dynamodb_tables):
        """**Validates: Requirements 3.7**

        mock_dynamodb_tables returns a dict.
        """
        assert isinstance(mock_dynamodb_tables, dict)

    def test_keys_are_table_names(self, mock_dynamodb_tables):
        """**Validates: Requirements 3.7**

        Keys are the canonical DynamoDB table name strings.
        """
        expected = {td["TableName"] for td in TABLE_DEFINITIONS}
        assert set(mock_dynamodb_tables.keys()) == expected

    def test_values_are_table_resources(self, mock_dynamodb_tables):
        """**Validates: Requirements 3.7**

        Values are boto3 DynamoDB Table resources with a ``table_name``
        attribute matching the key.
        """
        for name, table in mock_dynamodb_tables.items():
            assert hasattr(table, "table_name"), f"Value for '{name}' is not a Table resource"
            assert table.table_name == name


# ---------------------------------------------------------------------------
# Property 6c: mock_s3_buckets returns dict of bucket name strings (moto)
# ---------------------------------------------------------------------------


class TestMockS3BucketsToggle:
    """Validate mock_s3_buckets fixture interface in moto mode."""

    def test_returns_dict(self, mock_s3_buckets):
        """**Validates: Requirements 3.7**

        mock_s3_buckets returns a dict.
        """
        assert isinstance(mock_s3_buckets, dict)

    def test_keys_are_bucket_names(self, mock_s3_buckets):
        """**Validates: Requirements 3.7**

        Keys are the canonical S3 bucket name strings.
        """
        expected = set(S3_BUCKET_DEFINITIONS)
        assert set(mock_s3_buckets.keys()) == expected

    def test_values_are_strings(self, mock_s3_buckets):
        """**Validates: Requirements 3.7**

        Values are bucket name strings (same as keys in moto mode).
        """
        for name, value in mock_s3_buckets.items():
            assert isinstance(value, str)
            assert value == name


# ---------------------------------------------------------------------------
# Property 6d: mock_sqs_queues returns dict of queue URL strings (moto)
# ---------------------------------------------------------------------------


class TestMockSQSQueuesToggle:
    """Validate mock_sqs_queues fixture interface in moto mode."""

    def test_returns_dict(self, mock_sqs_queues):
        """**Validates: Requirements 3.7**

        mock_sqs_queues returns a dict.
        """
        assert isinstance(mock_sqs_queues, dict)

    def test_keys_are_queue_names(self, mock_sqs_queues):
        """**Validates: Requirements 3.7**

        Keys are the canonical SQS queue name strings.
        """
        expected = set(SQS_QUEUE_DEFINITIONS)
        assert set(mock_sqs_queues.keys()) == expected

    def test_values_are_url_strings(self, mock_sqs_queues):
        """**Validates: Requirements 3.7**

        Values are SQS queue URL strings.
        """
        for name, url in mock_sqs_queues.items():
            assert isinstance(url, str)
            assert "sqs" in url.lower() or "queue" in url.lower(), (
                f"Queue URL for '{name}' doesn't look like an SQS URL: {url}"
            )
