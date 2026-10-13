# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Performance integration test conftest."""

import sys


def pytest_collectstart(collector):
    """Clear cached modules between test files to prevent cross-contamination."""
    if hasattr(collector, "fspath") and str(collector.fspath).endswith(".py"):
        sys.modules.pop("lambda_function", None)
        for mod_name in list(sys.modules.keys()):
            if "agentic_data_ingestion_dashboard_api" in mod_name:
                del sys.modules[mod_name]
            if mod_name in ("router", "handlers", "handlers.common", "handlers.pipelines"):
                del sys.modules[mod_name]
