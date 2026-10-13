# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E shared harness — gating, cleanup, provisioning, and verification utilities.

This package provides reusable infrastructure for e2e tests against the
deployed AgenticDataIngestion environment. Fixtures are registered in the parent
``tests/e2e/conftest.py`` so they're available to all e2e test modules.
"""
