# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Clear cached lambda_function and dashboard API modules between test files."""

import sys


def pytest_collectstart(collector):
    if hasattr(collector, "fspath") and str(collector.fspath).endswith(".py"):
        sys.modules.pop("lambda_function", None)
        # Clean up dashboard API package modules to prevent cross-contamination
        for mod_name in list(sys.modules.keys()):
            if "agentic_data_ingestion_dashboard_api" in mod_name:
                del sys.modules[mod_name]
            if mod_name in ("router", "handlers"):
                del sys.modules[mod_name]
