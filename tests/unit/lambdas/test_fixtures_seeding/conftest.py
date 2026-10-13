# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Conftest for test-fixtures seeding Lambda unit tests.

The seeding Lambda lives at ``tests/fixtures/cdk/seeding_lambda/index.py`` and
is NOT installed into the project venv as a package — it is bundled into a
Lambda asset by CDK at synth time. To exercise its pure helpers from a test,
we splice the module's directory onto ``sys.path`` and import it as ``index``.

A second concern: ``index`` is a fairly generic module name and other tests
may end up importing a same-named module. The session-scoped fixture pops
``index`` from ``sys.modules`` after the test session completes so the
collection state stays clean for any other suites that run in the same
process.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Resolve to <repo>/agentic_data_ingestion/tests/fixtures/cdk/seeding_lambda
_SEEDING_LAMBDA_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "cdk" / "seeding_lambda"

if str(_SEEDING_LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(_SEEDING_LAMBDA_DIR))
