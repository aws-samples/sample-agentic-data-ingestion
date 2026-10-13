# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Root conftest.py — marker registration and shared configuration.

Registers all pytest markers used across the test hierarchy and sets
the default marker expression to exclude e2e tests from regular runs.

Shared fixtures from ``tests/fixtures/conftest.py`` are loaded via
``pytest_plugins`` so they are available to all test layers.
"""

import os

# ── jinja2 module-identity guard ──────────────────────────────────────
# Imported for its side effect: putting jinja2 (and its submodules) into
# sys.modules before ANY test module runs. Do not remove as an unused import.
#
# 118 test modules stub heavy dependencies with
# ``unittest.mock.patch.dict("sys.modules", {...})`` before importing a Lambda
# handler. On exit that helper does not merely undo the keys it overrode: it
# CLEARS sys.modules and re-applies the snapshot taken on entry, so every
# module first imported *inside* the block is evicted. ``lambda_function`` →
# ``code_assembly`` → ``import jinja2`` is inside those blocks, so when jinja2
# was not already loaded the process ends up holding TWO independent copies of
# the package: the one code_assembly is bound to, and the one every later
# ``import jinja2`` gets from sys.modules — including the import the compiled
# template itself performs (``from jinja2.runtime import ... missing``).
#
# Two copies means two distinct ``missing`` sentinels, and jinja2's generated
# code decides whether a variable is undefined with ``x is missing``. Across
# the copies that test is always False, so an undefined variable reaches
# filters as a raw ``_MissingType`` instead of as an ``Undefined``.
# ``_MissingType`` is truthy and is not an ``Undefined``, so
# ``default(x, y, true)`` returns the sentinel and ``{% if x %}`` is taken.
# Measured consequence before this line existed:
# ``tests/integration/pipeline_flow/test_code_generation_e2e.py`` rendered the
# checkpoint branch with ``checkpoint_bucket`` stringified as "missing"
# (``s3://missing/checkpoints/...``) — a script production never emits — and
# ``_partials/_load_counts.py.j2``'s ``indent()`` width became the sentinel,
# failing all 18 tests in that module with
# "can't multiply sequence by non-int of type '_MissingType'".
#
# Importing jinja2 here means it is in the entry snapshot of every one of those
# patch.dict blocks, so it survives their restore and stays a single object.
import jinja2  # noqa: F401
from hypothesis import settings as hypothesis_settings

# ── Hypothesis profiles ───────────────────────────────────────────────
# Hypothesis enforces a 200ms-per-example deadline by default. That deadline
# is a *performance* guard, not a correctness check: under machine load a
# single example can exceed it and fail an otherwise-passing run (a classic
# load-sensitive flake). We register deadline-relaxed profiles and load one by
# default so property tests stay deterministic on busy local/CI boxes.
#
# Only the deadline is relaxed. ``max_examples`` and every correctness setting
# come from each test's own ``@settings`` decorator (which takes precedence) or
# from Hypothesis defaults, so test rigor is unchanged. Re-registering a
# profile name simply overwrites it, so this stays idempotent across imports.
hypothesis_settings.register_profile("dev", deadline=None)
hypothesis_settings.register_profile("ci", deadline=None)
hypothesis_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


# Load shared fixtures from tests/fixtures/conftest.py so they are
# available to every test layer (unit, contract, integration, e2e).
pytest_plugins = ["tests.fixtures.conftest"]


def pytest_configure(config):
    """Register custom markers and set default marker expression."""
    config.addinivalue_line("markers", "unit: fast isolated unit tests")
    config.addinivalue_line("markers", "contract: API/event schema contract tests")
    config.addinivalue_line("markers", "integration: multi-Lambda flow tests with moto")
    config.addinivalue_line("markers", "e2e: end-to-end tests against real AWS")
    config.addinivalue_line("markers", "slow: tests exceeding time budget thresholds")
    config.addinivalue_line("markers", "cdk: CDK infrastructure assertion tests")
    config.addinivalue_line("markers", "cost_budget_override(seconds): override the default e2e cost budget timeout")

    # Exclude e2e tests by default unless the user explicitly passes -m
    markexpr = config.getoption("-m", default="")
    if not markexpr:
        config.option.markexpr = "not e2e"
