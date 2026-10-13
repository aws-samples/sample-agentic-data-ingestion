#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Test script for infrastructure target type detection logic.
Verifies that should_create_infrastructure() correctly determines when to create infrastructure.
"""

import os
import sys

# Add lambda function path to sys.path
lambda_path = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_infrastructure"
)
sys.path.insert(0, lambda_path)


def test_should_create_infrastructure():
    """Test the should_create_infrastructure function with different target configs"""

    # Import the function (this will work if the Lambda is properly structured)
    try:
        from lambda_function import should_create_infrastructure
    except ImportError as e:
        print(f"❌ Failed to import function: {e}")
        return False

    test_cases = [
        {
            "name": "NEW_S3_TABLE_BUCKET - should create",
            "target_config": {
                "type": "NEW_S3_TABLE_BUCKET",
                "s3_table_bucket": "agentic-data-ingestion-table-test-123456789012",
                "table_name": "test_table",
                "format": "iceberg",
            },
            "expected_create": True,
            "expected_reason_contains": "Creating new S3 Table Bucket",
        },
        {
            "name": "EXISTING_S3_TABLE_BUCKET - should skip",
            "target_config": {
                "type": "EXISTING_S3_TABLE_BUCKET",
                "s3_table_bucket": "existing-bucket",
                "table_name": "existing_table",
            },
            "expected_create": False,
            "expected_reason_contains": "existing S3 Table Bucket",
        },
        {
            "name": "GLUE_CATALOG - should skip",
            "target_config": {
                "type": "GLUE_CATALOG",
                "glue_database": "test_db",
                "glue_table": "test_table",
                "format": "parquet",
            },
            "expected_create": False,
            "expected_reason_contains": "Glue Catalog",
        },
        {
            "name": "JDBC - should skip",
            "target_config": {
                "type": "JDBC",
                "jdbc_connection_name": "aurora-connection",
                "database_name": "test_db",
                "table_name": "test_table",
            },
            "expected_create": False,
            "expected_reason_contains": "JDBC",
        },
        {
            "name": "Empty target_config - should skip",
            "target_config": {},
            "expected_create": False,
            "expected_reason_contains": "No target_config",
        },
        {
            "name": "None target_config - should skip",
            "target_config": None,
            "expected_create": False,
            "expected_reason_contains": "No target_config",
        },
        {
            "name": "Unknown type - should skip",
            "target_config": {"type": "UNKNOWN_TYPE"},
            "expected_create": False,
            "expected_reason_contains": "Unknown target type",
        },
    ]

    all_passed = True

    print("\n" + "=" * 80)
    print("Testing Infrastructure Target Type Detection Logic")
    print("=" * 80 + "\n")

    for i, test_case in enumerate(test_cases, 1):
        print(f"Test {i}: {test_case['name']}")
        print(f"  Target Config: {test_case['target_config']}")

        try:
            should_create, reason = should_create_infrastructure(test_case["target_config"])

            # Check if should_create matches expected
            if should_create != test_case["expected_create"]:
                print(f"  ❌ FAILED: Expected should_create={test_case['expected_create']}, got {should_create}")
                all_passed = False
            # Check if reason contains expected text
            elif test_case["expected_reason_contains"].lower() not in reason.lower():
                print(
                    f"  ❌ FAILED: Expected reason to contain '{test_case['expected_reason_contains']}', got '{reason}'"
                )
                all_passed = False
            else:
                print(f"  ✅ PASSED: should_create={should_create}, reason='{reason}'")
        except Exception as e:
            print(f"  ❌ FAILED with exception: {e}")
            all_passed = False

        print()

    print("=" * 80)
    if all_passed:
        print("✅ All tests PASSED!")
    else:
        print("❌ Some tests FAILED!")
    print("=" * 80 + "\n")

    return all_passed


if __name__ == "__main__":
    success = test_should_create_infrastructure()
    sys.exit(0 if success else 1)
