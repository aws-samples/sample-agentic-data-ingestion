# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/config.py — the single config source (P28 R18.2).

Covers:
- Documented defaults for every constant (the 72 h advertised approval
  window, the 0.85 confidence gate set by the recorded p28b R4 decision —
  superseding the crystallized live 0.8 — and the 11-day trigger-lock TTL).
- The deliberate separation between the ADVERTISED approval window and the
  task-token TTL BACKSTOP: two different numbers on purpose, the backstop
  strictly the longer of the two.
- Env-override readers: same env var names the deployed Lambdas already
  use (DEFAULT_TIMEOUT_HOURS, COMPLEX_MAPPING_THRESHOLD), read at call
  time, honest ValueError on garbage — never a silent fallback.
- model_config's budget-item TTL, now owned by model_config outright rather
  than derived from the approval window.

AMENDED DELIBERATELY — p34 R6, not a regression.
    P28 R18.1 made ``APPROVAL_WAIT_DAYS = 7`` the one source for every
    approval-window-derived value, and this file pinned that with
    ``test_approval_wait_days_is_seven`` and
    ``test_default_timeout_hours_default_is_168``. p34 R6 shortened the
    ENFORCING side — ``APPROVAL_TIMEOUT_SECONDS`` on the three
    ``waitForTaskToken`` gates in ``deploy-cdk/lib/orchestration-stack.ts``
    — to 72 h without moving the Python side, so the system advertised a
    deadline 96 hours longer than the one it enforced.

    The reversal: the advertised window (``APPROVAL_WINDOW_HOURS``) now
    mirrors the gate at 72 h, and the task-token TTL backstop
    (``TASK_TOKEN_TTL_DAYS``) deliberately KEEPS 7 days, because a token
    must outlive the longest legitimate approval wait rather than race it.
    So one constant, one number, and one derivation became two constants
    that must NOT be the same number — which is why
    ``test_approval_wait_days_is_seven`` could not simply be retargeted and
    ``test_timeout_hours_derives_from_wait_days`` no longer describes a real
    relationship.

    Amending rather than deleting follows ``LGR-5906ec6-2`` and
    ``LGR-60c4985-6`` (both template / ratified): this repo encodes a
    deliberate position as a test assertion, so reversing the position means
    changing the test on purpose and saying so where the assertion lives.
    ``LGR-60c4985-3`` (template / ratified) is why the gate is the authority
    — ``waitForTaskToken`` steps are excluded from retry-resume caching, so
    ``TimeoutSeconds`` is the worst-case stall for those states, not a
    theoretical backstop.

    The surviving subject is unchanged in each case: every behavioral value
    has exactly one home, and consumers import it rather than restating it.
    What moved is which value that home holds, and the fact that the
    advertised deadline and the TTL backstop are now two homes instead of
    one. Only the P28-era claim that they are the *same* number is reversed.

    The cross-language half of the agreement — that the advertised window
    equals the CDK gate constant — is asserted in
    ``tests/contracts/test_approval_window_cross_language.py``, which reads
    ``APPROVAL_TIMEOUT_SECONDS`` out of the TypeScript source. This file
    deliberately does NOT restate 259200: that would be the second literal
    p34 R6.6 exists to prevent.

Validates: Requirements 18.1, 18.2 (P28); 6.1, 6.6 (p34)
"""

import os
import sys

import pytest

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from config import (
    APPROVAL_RECORD_RETENTION_DAYS,
    APPROVAL_WINDOW_HOURS,
    COMPLEX_MAPPING_THRESHOLD_DEFAULT,
    CONFIDENCE_AUTO_APPROVE_THRESHOLD,
    DEFAULT_TIMEOUT_HOURS_DEFAULT,
    FALLBACK_CONFIDENCE_CAP,
    LOCK_TTL_SECONDS,
    MAPPABILITY_HARD_FAIL_THRESHOLD,
    QUEUED_RECORD_TTL_SECONDS,
    TASK_TOKEN_TTL_DAYS,
    TASK_TOKEN_TTL_SECONDS,
    complex_mapping_threshold,
    default_timeout_hours,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate every test from ambient env overrides."""
    monkeypatch.delenv("DEFAULT_TIMEOUT_HOURS", raising=False)
    monkeypatch.delenv("COMPLEX_MAPPING_THRESHOLD", raising=False)


# ---------------------------------------------------------------------------
# Behavior-preserving default values (R18.2)
# ---------------------------------------------------------------------------


class TestConstantDefaults:
    def test_task_token_ttl_backstop_is_seven_days(self):
        """The task-token TTL backstop is 7 days.

        AMENDED per p34 R6 — see the module docstring. This test was named
        ``test_approval_wait_days_is_seven`` and asserted
        ``APPROVAL_WAIT_DAYS == 7``, the single constant that served as both
        the advertised window AND the TTL backstop.

        The number it asserts has not changed and the constant is still 7 —
        what changed is WHICH value it describes. ``APPROVAL_WAIT_DAYS`` is
        gone because the name no longer told the truth: the approval wait is
        72 h now. 7 days survives here, on the backstop, deliberately: a
        stored token must outlive the longest legitimate approval wait, and a
        TTL equal to the window would race it.
        """
        assert TASK_TOKEN_TTL_DAYS == 7

    def test_advertised_approval_window_is_72_hours(self):
        """The advertised window is 72 h — mirroring the gate that enforces it.

        AMENDED per p34 R6 — see the module docstring. This test was named
        ``test_default_timeout_hours_default_is_168`` and asserted
        ``DEFAULT_TIMEOUT_HOURS_DEFAULT == 168``, i.e. the 7-day window.
        Renamed rather than retargeted in place because the old name stated
        the number it was asserting — the same situation p34 task 10.3 hit
        with ``test_schema_mapping_gate_timeout_is_604800``, resolved the same
        way: rename, and record the old name here so the reversal stays
        greppable.

        The number the gate enforces lives in exactly one place, the CDK's
        ``APPROVAL_TIMEOUT_SECONDS``; the equality between the two is asserted
        in ``tests/contracts/test_approval_window_cross_language.py``. 72 is
        restated here only as the Python-side default's own value.
        """
        assert APPROVAL_WINDOW_HOURS == 72
        assert DEFAULT_TIMEOUT_HOURS_DEFAULT == 72

    def test_confidence_auto_approve_threshold_is_recorded_decision(self):
        """0.85 — the deliberate p28b R4 decision (safer gate, more human
        approvals), superseding the crystallized live 0.8."""
        assert CONFIDENCE_AUTO_APPROVE_THRESHOLD == 0.85

    def test_complex_mapping_threshold_default(self):
        assert COMPLEX_MAPPING_THRESHOLD_DEFAULT == 0.7

    def test_lock_ttl_is_eleven_days(self):
        # Above the state machine's 10-day EXECUTION_TIMEOUT_SECONDS, so a
        # lock can only expire after its execution has ended. The ordering
        # itself is pinned in tests/cdk/stacks/test_lock_ttl_exceeds_execution_timeout.py.
        assert LOCK_TTL_SECONDS == 11 * 24 * 60 * 60

    def test_queued_record_ttl_is_one_hour(self):
        assert QUEUED_RECORD_TTL_SECONDS == 3600

    def test_approval_record_retention_is_30_days(self):
        assert APPROVAL_RECORD_RETENTION_DAYS == 30

    def test_mappability_hard_fail_threshold_is_the_ruled_value(self):
        """p55 R14.6: 0.5, defined once, here.

        The value comes from the tester's proposal and the user's ruling on
        p55 Open Question 1 — it is a **product** decision, so a change here
        is a re-ruling and not a tuning. This is the ONE place a test states
        the number; the constant is the one place production code states it
        (R14.6), and no consumer exists yet — p55 task 3.1 is the consumer.

        This assertion is deliberately not the interesting one. What makes
        the constant load-bearing is its relationship to the approval clause
        it must nest strictly inside, and that is asserted against the
        clause's own source literal in
        ``test_config_mappability_nesting.py`` rather than against a second
        copy of 0.8 written down here.
        """
        assert MAPPABILITY_HARD_FAIL_THRESHOLD == 0.5

    def test_mappability_hard_fail_threshold_is_a_constant_not_an_accessor(self):
        """p55 W5-D3: a value, not a callable, and no env var reads it.

        The decision is that a threshold deciding whether a run is REFUSED
        must not be silently movable per environment — two accounts would
        otherwise refuse differently on identical data with no signal. An
        accessor added later in the shape of ``complex_mapping_threshold()``
        would satisfy every other assertion in this file, so the posture is
        asserted directly: the module attribute is a float, and no
        ``MAPPABILITY_HARD_FAIL_THRESHOLD`` environment variable is consulted
        anywhere in ``config.py``.
        """
        import config

        assert isinstance(config.MAPPABILITY_HARD_FAIL_THRESHOLD, float)
        assert not callable(config.MAPPABILITY_HARD_FAIL_THRESHOLD)
        assert not hasattr(config, "mappability_hard_fail_threshold")

        with open(config.__file__, encoding="utf-8") as fh:
            source = fh.read()
        assert 'os.environ.get("MAPPABILITY' not in source
        assert '_env_float("MAPPABILITY' not in source


class TestDerivations:
    def test_timeout_hours_default_is_the_advertised_window(self):
        """The env reader's fallback IS the advertised window — no second copy.

        AMENDED per p34 R6 — see the module docstring. This was
        ``test_timeout_hours_derives_from_wait_days``, asserting
        ``DEFAULT_TIMEOUT_HOURS_DEFAULT == APPROVAL_WAIT_DAYS * 24``. The
        surviving subject is the one that mattered: the default the approval
        handler uses is not a literal of its own, it is the window constant.
        Only the constant it derives from changed name and unit.
        """
        assert DEFAULT_TIMEOUT_HOURS_DEFAULT == APPROVAL_WINDOW_HOURS

    def test_task_token_ttl_derives_from_its_own_backstop_constant(self):
        """A stored token must outlive the longest legitimate approval wait.

        AMENDED per p34 R6 — see the module docstring. This was
        ``test_task_token_ttl_derives_from_wait_days``, asserting the TTL was
        ``APPROVAL_WAIT_DAYS * 24 * 3600`` — i.e. exactly the approval window.
        That derivation is deliberately gone: deriving the backstop from the
        window makes them the same instant, which is the race the docstring
        on ``TASK_TOKEN_TTL_DAYS`` describes. The TTL now has its own
        constant, and its relationship to the window is the strict inequality
        below, not equality.
        """
        assert TASK_TOKEN_TTL_SECONDS == TASK_TOKEN_TTL_DAYS * 24 * 3600

    def test_token_ttl_backstop_outlives_the_advertised_window(self):
        """p34 R6: the backstop is STRICTLY longer than the window it survives.

        The advertised deadline and the TTL backstop are two different numbers
        on purpose. Equal values would mean the token expires at the same
        instant the approval deadline passes, so a decision that arrived in
        time could still find no token to send.
        """
        assert TASK_TOKEN_TTL_SECONDS > APPROVAL_WINDOW_HOURS * 3600

    def test_fallback_cap_strictly_below_auto_approve_threshold(self):
        """R3.2: a fallback mapping can never auto-approve on confidence."""
        assert FALLBACK_CONFIDENCE_CAP < CONFIDENCE_AUTO_APPROVE_THRESHOLD


# ---------------------------------------------------------------------------
# Env-override readers (call-time; deployed env var names keep working)
# ---------------------------------------------------------------------------


class TestDefaultTimeoutHours:
    def test_default_without_env(self):
        """AMENDED per p34 R6: the default is 72 h, was 168 h."""
        assert default_timeout_hours() == 72

    def test_env_override_applies(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEOUT_HOURS", "12")
        assert default_timeout_hours() == 12

    def test_empty_env_falls_back_to_default(self, monkeypatch):
        """AMENDED per p34 R6: the fallback is 72 h, was 168 h."""
        monkeypatch.setenv("DEFAULT_TIMEOUT_HOURS", "  ")
        assert default_timeout_hours() == 72

    def test_garbage_env_raises_value_error(self, monkeypatch):
        """Honest failure over a silent fallback (R18.2 config policy)."""
        monkeypatch.setenv("DEFAULT_TIMEOUT_HOURS", "one-week")
        with pytest.raises(ValueError, match="DEFAULT_TIMEOUT_HOURS"):
            default_timeout_hours()


class TestComplexMappingThreshold:
    def test_default_without_env(self):
        assert complex_mapping_threshold() == 0.7

    def test_env_override_applies(self, monkeypatch):
        monkeypatch.setenv("COMPLEX_MAPPING_THRESHOLD", "0.55")
        assert complex_mapping_threshold() == 0.55

    def test_read_at_call_time(self, monkeypatch):
        """Per-invocation env read — matches the pre-R18 in-handler read."""
        monkeypatch.setenv("COMPLEX_MAPPING_THRESHOLD", "0.6")
        assert complex_mapping_threshold() == 0.6
        monkeypatch.setenv("COMPLEX_MAPPING_THRESHOLD", "0.65")
        assert complex_mapping_threshold() == 0.65

    def test_garbage_env_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("COMPLEX_MAPPING_THRESHOLD", "low")
        with pytest.raises(ValueError, match="COMPLEX_MAPPING_THRESHOLD"):
            complex_mapping_threshold()


# ---------------------------------------------------------------------------
# Cross-module single-sourcing (no divergent copies of the window)
# ---------------------------------------------------------------------------


class TestSingleSourcing:
    def test_model_config_budget_ttl_is_owned_by_model_config(self):
        """model_config owns its budget-item TTL outright — one home per value.

        AMENDED per p34 R6 — see the module docstring. This was
        ``test_model_config_budget_ttl_derives_from_approval_window``,
        asserting ``_BUDGET_ITEM_TTL_SECONDS == (APPROVAL_WAIT_DAYS + 1) * 24
        * 3600``. The derivation is deliberately gone, and the VALUE is
        deliberately unchanged at 8 days: shortening the advertised approval
        window would otherwise have dragged this housekeeping TTL from 8 days
        to 4 as a pure side effect. What a budget item must outlive is a
        *run*, which can be long for reasons unrelated to approvals.

        The surviving subject — R18.2's "exactly one home per value, never
        two" — is what is asserted: the TTL is derived from a named constant
        in the module that owns budgets, not from a literal and not from an
        unrelated module's window.
        """
        import model_config

        assert model_config.BUDGET_ITEM_TTL_DAYS == 8
        assert model_config._BUDGET_ITEM_TTL_SECONDS == model_config.BUDGET_ITEM_TTL_DAYS * 24 * 3600
        # The value is unchanged from the pre-p34 derivation (7 + 1 days).
        assert model_config._BUDGET_ITEM_TTL_SECONDS == 8 * 24 * 3600
