# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test layer conftest — auto-applies @pytest.mark.contract to all tests."""

import os

import pytest

# Dashboard API handlers/common.py calls require_env("AUDIT_TABLE") at import
# time. Set it here so contract tests can import handler modules without error.
os.environ.setdefault("AUDIT_TABLE", "agentic_data_ingestion_audit_log")

_THIS_DIR = os.path.dirname(__file__)


def pytest_collection_modifyitems(items):
    """Automatically add the 'contract' marker to every test in this directory."""
    for item in items:
        if os.path.commonpath([str(item.fspath), _THIS_DIR]) == _THIS_DIR:
            item.add_marker(pytest.mark.contract)
