# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK test layer conftest — auto-applies @pytest.mark.cdk to all tests."""

import os

import pytest

_THIS_DIR = os.path.dirname(__file__)


def pytest_collection_modifyitems(items):
    """Automatically add the 'cdk' marker to every test in this directory."""
    for item in items:
        if os.path.commonpath([str(item.fspath), _THIS_DIR]) == _THIS_DIR:
            item.add_marker(pytest.mark.cdk)
