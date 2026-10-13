# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for env_utils (Task 6.3)."""

import importlib.util
import os
import sys
from unittest.mock import patch

import hypothesis.strategies as st
from hypothesis import given, settings

# Add shared directory to import path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

_ENV_UTILS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared", "env_utils.py")


def _load_private_env_utils():
    """Execute ``shared/env_utils.py`` into a module that is never put in ``sys.modules``.

    ``_LOCAL_MODE`` is read once, at import, so each example needs the module run
    again under its own environment. This used to ``importlib.reload`` the
    ``env_utils`` in ``sys.modules`` instead, which re-executed the copy every
    other module had already bound with ``from env_utils import require_env``,
    left it in production mode for the rest of the session, and reset its
    memoized account id. That stayed hidden while other test files left an
    ``env_utils`` MagicMock in ``sys.modules`` for this file to delete; once they
    stopped, ``test_retry_utils.py`` failed after this file in the same run. A
    private copy neither trusts nor mutates shared import state, the approach
    ``LGR-c58f276-4`` took for ``audit_utils``.
    """
    spec = importlib.util.spec_from_file_location("_env_utils_under_test", _ENV_UTILS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Strategy: valid environment variable names (alphanumeric + underscore, starting with letter)
env_var_names = st.from_regex(r"[A-Z][A-Z0-9_]{0,49}", fullmatch=True)


# Feature: agentic-data-ingestion-codebase-optimization, Property 7: Missing required env vars produce descriptive errors
@given(env_var_names)
@settings(max_examples=200)
def test_missing_env_var_raises_with_name_in_message(var_name: str):
    """
    When a required env var is unset and not in local mode,
    require_env() raises EnvironmentError with the variable name in the message.
    """
    # Ensure the var is NOT set and we are NOT in local mode
    env_overrides = {
        "AGENTIC_DATA_INGESTION_ENV": "production",  # Not local mode
    }
    # Remove the target var if it exists
    env_clean = {k: v for k, v in os.environ.items() if k != var_name}
    env_clean.update(env_overrides)

    with patch.dict(os.environ, env_clean, clear=True):
        # A fresh copy executed under this environment, so _LOCAL_MODE is False
        env_utils = _load_private_env_utils()

        try:
            env_utils.require_env(var_name)
            assert False, f"Expected EnvironmentError for missing var '{var_name}'"
        except OSError as e:
            # The variable name must appear in the error message
            assert var_name in str(e), f"Variable name '{var_name}' not found in error message: {e}"
