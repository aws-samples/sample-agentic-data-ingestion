# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Conftest for unit/lambdas/ — handles lambda_function module isolation.

Multiple test files import different Lambda handlers as ``lambda_function``
via sys.path manipulation. Without clearing the module cache between test
files, the first Lambda's module wins and subsequent test files get the
wrong module.

This conftest clears the ``lambda_function`` entry from ``sys.modules``
before each test module is collected, ensuring each test file gets a fresh
import of its target Lambda handler.
"""

import sys


def pytest_collectstart(collector):
    """Clear cached lambda_function and dashboard API modules before collecting each test module."""
    # Only clear for test file collectors (not directories)
    if hasattr(collector, "fspath") and str(collector.fspath).endswith(".py"):
        sys.modules.pop("lambda_function", None)
        # Clean up dashboard API package modules to prevent cross-contamination
        for mod_name in list(sys.modules.keys()):
            if "agentic_data_ingestion_dashboard_api" in mod_name:
                del sys.modules[mod_name]
            if mod_name in ("router", "handlers"):
                del sys.modules[mod_name]
