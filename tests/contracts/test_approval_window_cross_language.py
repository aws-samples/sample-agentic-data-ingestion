# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the advertised approval window equals the CDK gate window.

Two artifacts have to agree on one number and neither can import the other:

- ``deploy-cdk/lib/orchestration-stack.ts`` exports
  ``APPROVAL_TIMEOUT_SECONDS``, the ``TimeoutSeconds`` on all three
  ``waitForTaskToken`` approval gates. This is the number that ENFORCES the
  window — when it elapses, ``Catch[States.ALL]`` routes to ``RecordFailure``
  and the execution is abandoned.
- ``shared/config.py`` defines ``APPROVAL_WINDOW_HOURS``, the number the
  system ADVERTISES: ``timeout_at`` on the approval record, the "Timeout: N
  hours" line the approver reads, and the deadline the timeout sweeper acts
  on.

When they disagree, the system promises an approver more time than it will
give them. That is exactly what happened between p34 R6 (which shortened the
gate to 72 h) and this test: ``config.py`` still advertised 168 h, so an
approver told they had 7 days could act on day 4 and find the run already
failed.

The gate is the authority and Python moves to meet it, never the reverse —
``LGR-60c4985-3`` (template / ratified) excludes ``waitForTaskToken`` steps
from retry-resume caching, which makes ``TimeoutSeconds`` the worst-case
stall for those states rather than a theoretical backstop.

Why this file lives at ``tests/contracts/`` root rather than under
``api_responses/`` or ``event_schemas/``: the subject is neither a response
body nor an event payload, it is a constant shared across a language
boundary. The root ``conftest.py`` applies ``@pytest.mark.contract`` to
anything under ``tests/contracts/``, so it still runs in the contracts layer
of ``make test-fast``.

Why parse the TypeScript instead of restating the number: a second literal
that must agree is the drift this exists to prevent — p34 R6.6's whole point
was "expressed once". The technique is this repo's existing one, from
``tests/integration/pipeline_flow/test_test_failure_gating.py::_choice_passed_literal``
and ``tests/cdk/stacks/test_asl_state_names_pinned.py``: read the literal out
of the CDK source with a regex and assert against it, so changing one side
without the other fails here.

Validates: p34 Requirements 6.1, 6.6 (the window is expressed once);
P28 Requirement 18.1 (one source per behavioral value), as amended.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATION_STACK = _PROJECT_ROOT / "deploy-cdk" / "lib" / "orchestration-stack.ts"

sys.path.insert(0, os.path.join(str(_PROJECT_ROOT), "shared"))

from config import (
    APPROVAL_WINDOW_HOURS,
    TASK_TOKEN_TTL_SECONDS,
    default_timeout_hours,
)


def _cdk_approval_timeout_seconds() -> int:
    """The gate's TimeoutSeconds, read out of the CDK source.

    Matches the exported constant declaration, not a use site, so a stray
    reference elsewhere in the file cannot satisfy it.
    """
    source = _ORCHESTRATION_STACK.read_text(encoding="utf-8")
    match = re.search(
        r"export\s+const\s+APPROVAL_TIMEOUT_SECONDS\s*(?::\s*number\s*)?=\s*(\d+)\s*;",
        source,
    )
    assert match is not None, (
        "APPROVAL_TIMEOUT_SECONDS declaration not found in "
        "deploy-cdk/lib/orchestration-stack.ts — the approval gate window "
        "moved or was renamed, and shared/config.APPROVAL_WINDOW_HOURS can no "
        "longer be checked against it."
    )
    return int(match.group(1))


class TestAdvertisedWindowMirrorsTheGate:
    """The window Python advertises is the window the CDK gate enforces."""

    def test_advertised_window_equals_cdk_gate_timeout(self):
        """config.APPROVAL_WINDOW_HOURS == APPROVAL_TIMEOUT_SECONDS."""
        gate_seconds = _cdk_approval_timeout_seconds()
        assert gate_seconds == APPROVAL_WINDOW_HOURS * 3600, (
            f"shared/config.py advertises a {APPROVAL_WINDOW_HOURS}h approval window "
            f"but the CDK gate abandons the execution after {gate_seconds}s "
            f"({gate_seconds / 3600:g}h). The gate is the authority: move "
            f"APPROVAL_WINDOW_HOURS to match it, not the other way round."
        )

    def test_default_timeout_hours_reader_reports_the_gate_window(self, monkeypatch):
        """The env-backed reader defaults to the gate window, not just the constant.

        ``create_approval_request`` computes ``timeout_at`` from
        ``default_timeout_hours()``, so the reader — not only the raw constant
        — is what an approver's deadline is derived from.
        """
        monkeypatch.delenv("DEFAULT_TIMEOUT_HOURS", raising=False)
        assert _cdk_approval_timeout_seconds() == default_timeout_hours() * 3600

    def test_token_ttl_outlives_the_gate_window(self):
        """The TTL backstop is strictly longer than the window it must survive.

        Deliberate separation (p34 R6): the advertised deadline and the
        task-token TTL are two different numbers. A TTL equal to the window
        would race it, and a token reaped a moment early turns a decision
        that arrived in time into a callback that can never be delivered.
        """
        assert _cdk_approval_timeout_seconds() < TASK_TOKEN_TTL_SECONDS


@pytest.mark.contract
def test_cdk_source_is_readable():
    """Guard: a moved/renamed CDK file must fail loudly, not skip silently."""
    assert _ORCHESTRATION_STACK.is_file(), f"CDK source not found at {_ORCHESTRATION_STACK}"
