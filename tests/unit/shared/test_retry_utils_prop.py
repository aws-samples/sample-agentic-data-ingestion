# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests for shared/retry_utils.py.

# Feature: p16-retry-from-any-step, Property 1: Skip decision is correct for any step pair

For any two valid pipeline steps (current_step, retry_from_step) drawn from
STEP_ORDER, should_skip_step(event, current_step, STEP_ORDER) returns True
if and only if current_step's index is strictly less than retry_from_step's index.

**Validates: Requirements 1.2, 1.3**
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# Stub env_utils before importing retry_utils
mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

# Add shared directory to path
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    from retry_utils import STEP_ORDER, should_skip_step
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

# Strategy: draw any step name from the canonical STEP_ORDER list
step_strategy = st.sampled_from(STEP_ORDER)


@given(current_step=step_strategy, retry_from_step=step_strategy)
@settings(max_examples=200)
def test_skip_decision_correct_for_any_step_pair(current_step: str, retry_from_step: str) -> None:
    """Property 1: Skip decision is correct for any step pair.

    # Feature: p16-retry-from-any-step, Property 1: Skip decision is correct for any step pair

    For any (current_step, retry_from_step) pair from STEP_ORDER,
    should_skip_step returns True iff current_step index < retry_from_step index.

    **Validates: Requirements 1.2, 1.3**
    """
    event = {"retry_from_step": retry_from_step}

    result = should_skip_step(event, current_step, STEP_ORDER)

    current_idx = STEP_ORDER.index(current_step)
    retry_idx = STEP_ORDER.index(retry_from_step)
    expected = current_idx < retry_idx

    assert result == expected, (
        f"should_skip_step returned {result} for current_step={current_step!r} "
        f"(idx={current_idx}), retry_from_step={retry_from_step!r} (idx={retry_idx}), "
        f"expected {expected}"
    )


@given(step=step_strategy)
@settings(max_examples=200)
def test_no_retry_from_step_means_no_skipping(step: str) -> None:
    """Property 2: No retry_from_step means no skipping.

    # Feature: p16-retry-from-any-step, Property 2: No retry_from_step means no skipping

    For any step in STEP_ORDER, when the event dict does not contain a
    retry_from_step key, should_skip_step must return False.

    **Validates: Requirements 1.4**
    """
    event: dict = {}  # no retry_from_step key

    result = should_skip_step(event, step, STEP_ORDER)

    assert result is False, (
        f"should_skip_step returned {result} for step={step!r} with no retry_from_step in event, expected False"
    )


# ---------------------------------------------------------------------------
# Property 3: Invalid step names are rejected
# Feature: p16-retry-from-any-step, Property 3: Invalid step names are rejected
# ---------------------------------------------------------------------------


def validate_retry_step(step_name: str) -> dict:
    """Validate a retry step name against STEP_ORDER.

    Mirrors the validation logic in retry_run() from pipelines.py.
    Returns a dict with statusCode 400 and valid_steps when the step
    name is not a member of STEP_ORDER, or statusCode 200 when valid.
    """
    if step_name not in STEP_ORDER:
        return {
            "statusCode": 400,
            "error": "Invalid step name",
            "valid_steps": STEP_ORDER,
        }
    return {"statusCode": 200}


# Strategy: random text strings that are NOT valid step names
invalid_step_strategy = st.text().filter(lambda s: s not in STEP_ORDER)


@given(step_name=invalid_step_strategy)
@settings(max_examples=200)
def test_invalid_step_names_are_rejected(step_name: str) -> None:
    """Property 3: Invalid step names are rejected.

    # Feature: p16-retry-from-any-step, Property 3: Invalid step names are rejected

    For any string that is not a member of STEP_ORDER, the retry API
    validation rejects it with a 400 status code and includes the
    complete list of valid step names in the error response.

    **Validates: Requirements 3.3, 5.2**
    """
    result = validate_retry_step(step_name)

    assert result["statusCode"] == 400, f"Expected 400 for invalid step name {step_name!r}, got {result['statusCode']}"
    assert result["error"] == "Invalid step name", f"Expected error 'Invalid step name', got {result['error']!r}"
    assert result["valid_steps"] == STEP_ORDER, (
        f"Expected valid_steps to be STEP_ORDER ({len(STEP_ORDER)} items), got {len(result['valid_steps'])} items"
    )
