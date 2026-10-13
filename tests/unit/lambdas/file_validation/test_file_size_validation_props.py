# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for file size configuration (Task 8.2).

validate_file_size() was removed from the Lambda — file size is now checked
inline in lambda_handler via HEAD request.  These property tests validate
that the MAX_FILE_SIZE_BYTES constant is correctly set to the S3 limit (5 TB).
"""

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

# Add paths
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_file_validation"),
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Set local mode
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

logging.basicConfig(level=logging.WARNING)

# Import with mocked AWS clients
with patch.dict(os.environ, {"DYNAMODB_TABLE": "test-table"}):
    with patch("boto3.client", return_value=MagicMock()) as _mock_client:
        with patch("boto3.resource", return_value=MagicMock()):
            sys.modules.pop("lambda_function", None)
            import lambda_function

# The current threshold is 5 TB (S3 object size limit)
EXPECTED_MAX = 5 * 1024 * 1024 * 1024 * 1024


@given(st.integers(min_value=EXPECTED_MAX + 1, max_value=EXPECTED_MAX * 2))
@settings(max_examples=200)
def test_oversized_files_exceed_threshold(file_size: int):
    """Files larger than MAX_FILE_SIZE_BYTES are above the configured threshold."""
    assert file_size > lambda_function.MAX_FILE_SIZE_BYTES


@given(st.integers(min_value=0, max_value=EXPECTED_MAX))
@settings(max_examples=100)
def test_files_within_limit_are_under_threshold(file_size: int):
    """Files at or below MAX_FILE_SIZE_BYTES are within the configured threshold."""
    assert file_size <= lambda_function.MAX_FILE_SIZE_BYTES
