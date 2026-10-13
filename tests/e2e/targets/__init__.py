# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E tests for non-standard target types (Glue Catalog, S3 Parquet).

These tests provision throwaway pipelines with target types that do not
exist in the shared dev environment today, run them to SUCCEEDED, verify
rows in the real target store, and tear everything down.

The Glue Catalog test is skipped: that target type is no longer offered for
new pipelines, and ``POST /api/pipelines`` refuses it.

Phase T3 — Requirements 20, 21.
"""
