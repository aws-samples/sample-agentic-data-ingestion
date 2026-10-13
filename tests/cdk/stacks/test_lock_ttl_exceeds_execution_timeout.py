# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pipeline lock outlives the longest execution the state machine allows.

A DynamoDB TTL deletes the row whether or not its execution is still running,
so the lock's TTL has to exceed the execution's own ceiling or a run parked at
an approval gate loses its exclusion mid-wait. Until 2026-10-11 the TTL was
24 h against a 72 h gate; the state machine also had no timeout at all, so the
"ceiling" was Step Functions' one-year default.

Both constants are parsed out of their own files rather than imported, so this
is a cross-language check of what is deployed, not of a Python view of it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STACK = ROOT / "deploy-cdk" / "lib" / "orchestration-stack.ts"
CONFIG = ROOT / "shared" / "config.py"

HOUR = 3600


def _ts_const(name: str) -> int:
    match = re.search(rf"export const {name} = (\d+);", STACK.read_text(encoding="utf-8"))
    assert match, f"{name} not found in orchestration-stack.ts"
    return int(match.group(1))


def _lock_ttl_seconds() -> int:
    match = re.search(r"^LOCK_TTL_SECONDS = ([0-9 *]+)$", CONFIG.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "LOCK_TTL_SECONDS not found in shared/config.py"
    return eval(match.group(1))


def test_the_state_machine_has_an_execution_timeout_and_it_is_applied():
    text = STACK.read_text(encoding="utf-8")
    assert "timeout: cdk.Duration.seconds(EXECUTION_TIMEOUT_SECONDS)" in text


def test_the_execution_timeout_covers_three_approval_gates_and_the_glue_job():
    approval = _ts_const("APPROVAL_TIMEOUT_SECONDS")
    execution = _ts_const("EXECUTION_TIMEOUT_SECONDS")
    glue_default = 6 * HOUR  # DEFAULT_MAX_JOB_DURATION_SECONDS
    # Schema-mapping gate, forced test review, code approval, then the Glue job,
    # plus the container steps and regeneration loop (budgeted at 2 h).
    worst_legitimate = 3 * approval + glue_default + 2 * HOUR
    assert execution >= worst_legitimate, (
        f"EXECUTION_TIMEOUT_SECONDS={execution} is below the longest legitimate run ({worst_legitimate})"
    )


def test_the_lock_ttl_exceeds_the_execution_timeout():
    assert _lock_ttl_seconds() > _ts_const("EXECUTION_TIMEOUT_SECONDS")
