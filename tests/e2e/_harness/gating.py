# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E gating fixtures — trigger opt-in and cost budget.

These fixtures extend the existing ``require_real_aws`` session gate with:
- ``requires_trigger``: per-test gate that skips unless the triggering opt-in
  env var is set (``AGENTIC_DATA_INGESTION_E2E_TRIGGER=true``).
- ``cost_budget``: returns the per-test timeout ceiling used by polling
  helpers to bound execution time and avoid runaway cost.

Register both in ``tests/e2e/conftest.py`` so they're available suite-wide.
"""

import os

import pytest

# ── Defaults (overridable via env) ────────────────────────────────────
E2E_DEFAULT_TIMEOUT = int(os.environ.get("E2E_DEFAULT_TIMEOUT", "420"))
E2E_JDBC_TIMEOUT = int(os.environ.get("E2E_JDBC_TIMEOUT", "900"))


def _trigger_enabled() -> bool:
    """Check whether the triggering/mutating test opt-in is active."""
    return os.environ.get("AGENTIC_DATA_INGESTION_E2E_TRIGGER", "").lower() == "true"


@pytest.fixture
def requires_trigger():
    """Skip the test unless the e2e trigger opt-in env var is set.

    Mutating tests (upload → execution → approval → target write) must
    not run unless explicitly opted-in via AGENTIC_DATA_INGESTION_E2E_TRIGGER=true.
    This prevents accidental cost and side-effects in regular e2e runs.
    """
    if not _trigger_enabled():
        pytest.skip(
            "Triggering/mutating e2e tests are opt-in. Set AGENTIC_DATA_INGESTION_E2E_TRIGGER=true (see `make test-e2e-trigger`)."
        )


@pytest.fixture
def cost_budget(request) -> int:
    """Return the per-test timeout (seconds) for polling helpers.

    Default is E2E_DEFAULT_TIMEOUT (420s). Tests that need a longer budget
    (e.g. JDBC targets) can override via a ``cost_budget_override`` marker:

        @pytest.mark.cost_budget_override(900)
        def test_jdbc_target(..., cost_budget):
            ...

    Or set the E2E_JDBC_TIMEOUT env var for all JDBC tests.
    """
    marker = request.node.get_closest_marker("cost_budget_override")
    if marker and marker.args:
        return int(marker.args[0])
    return E2E_DEFAULT_TIMEOUT
