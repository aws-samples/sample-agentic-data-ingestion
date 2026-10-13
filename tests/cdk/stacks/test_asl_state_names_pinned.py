# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""ASL state ids are pinned to shared/step_names.py (P28 R8.2, task 14.2).

The Step Functions state ids stay PascalCase — that surface is owned by
``shared/step_names.SFN_STATE_NAMES`` (``to_sfn_state_name``). This test
reads deploy-cdk/lib/orchestration-stack.ts (the CDK source that defines
every state either directly as ``new <Type>(this, '<StateId>', …)`` or via
the ``containerBackedTask('<StateId>', …)`` factory introduced by task 15.5
(R19.11) — the factory passes the id through to
``new sfn.CustomState(this, id, …)`` unchanged) and asserts:

- every SFN_STATE_NAMES value appears as a construct/state id, and
- every canonical step's id round-trips through from_any,

so the ASL definition and the shared step-name module cannot silently
drift apart (a renamed state or enum member fails here first).

Validates: P28 Requirements 8.2
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")))

from step_names import SFN_STATE_NAMES, StepName, from_any, to_sfn_state_name

ORCHESTRATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "orchestration-stack.ts"
)


@pytest.fixture(scope="module")
def orchestration_source() -> str:
    with open(ORCHESTRATION_STACK_PATH) as f:
        return f.read()


class TestEverySharedStateNameExistsInAsl:
    """Each SFN_STATE_NAMES value is a state id in orchestration-stack.ts."""

    @pytest.mark.parametrize(
        "step,state_id",
        list(SFN_STATE_NAMES.items()),
        ids=[str(step) for step in SFN_STATE_NAMES],
    )
    def test_state_id_defined_as_construct(self, orchestration_source, step, state_id):
        """CDK constructs carry the state id, directly or via the task-15.5 factory.

        Direct:  new <Type>(this, '<StateId>', …)
        Factory: containerBackedTask('<StateId>', …) → new sfn.CustomState(this, id, …)
        """
        escaped = re.escape(state_id)
        pattern = rf"(?:this,\s*|containerBackedTask\(\s*)'{escaped}'"
        assert re.search(pattern, orchestration_source), (
            f"SFN state id {state_id!r} (canonical step {step!r}) is not defined "
            f"in orchestration-stack.ts — shared/step_names.py and the ASL have drifted"
        )


class TestSharedModuleSelfConsistency:
    """The mapping itself stays total and round-trippable."""

    def test_every_canonical_step_has_a_state_id(self):
        assert set(SFN_STATE_NAMES.keys()) == set(StepName)

    @pytest.mark.parametrize("step", list(StepName), ids=[s.value for s in StepName])
    def test_state_id_round_trips_to_canonical(self, step):
        assert from_any(to_sfn_state_name(step)) is step


class TestNoForeignTaskStateIds:
    """Guard the reverse direction for the Lambda-task states: every
    tasks.LambdaInvoke / task-step CustomState id that IS a pipeline step
    parses via from_any. New task states that write state rows must be added
    to shared/step_names.py (non-step machinery states — RecordFailure,
    lock handling, timeout guard, approval waits — are exempt)."""

    # States that are deliberately NOT pipeline steps (machinery).
    _NON_STEP_STATE_IDS = {
        "RecordFailure",
        "ReleaseLockSuccess",
        "ReleaseLockFailure",
        "ReuseExistingMapping",
        "WaitForSchemaMappingApproval",
        "WaitForCodeApproval",
        "ForceTestReviewApproval",
        "TestResultChoice",
        "CheckJobTimeoutConfig",
        "ApplyDefaultJobTimeout",
        # p32 D2 — the in-execution regeneration counter. These shape execution
        # state and nothing else: they invoke no Lambda and write no state row,
        # so they are machinery, not pipeline steps, and deliberately do NOT
        # appear in shared/step_names.py. Registering them here is what keeps
        # the reverse guard below honest (an unregistered Choice state fails
        # `from_any`) — see `LGR-21a1b5b-2`: an attempt is not a run.
        "CheckRegenerationAttempt",
        "ApplyDefaultRegenerationAttempt",
        "ComputeNextRegenerationAttempt",
        "IncrementRegenerationAttempt",
        "UpdateSuccessState",
        "PipelineFailed",
    }

    def test_every_lambda_task_state_is_canonical_or_cataloged_machinery(self, orchestration_source):
        state_ids = re.findall(
            r"new (?:tasks\.LambdaInvoke|sfn\.CustomState|sfn\.Choice)\(this,\s*'([A-Za-z]+)'",
            orchestration_source,
        )
        # Task-15.5 factory states: containerBackedTask('<StateId>', …) wraps
        # new sfn.CustomState(this, id, …) — same id surface, different call shape.
        state_ids += re.findall(r"containerBackedTask\(\s*'([A-Za-z]+)'", orchestration_source)
        assert state_ids, "no state constructs found — extraction regex is stale"
        unknown = []
        for state_id in state_ids:
            if state_id in self._NON_STEP_STATE_IDS:
                continue
            try:
                from_any(state_id)
            except ValueError:
                unknown.append(state_id)
        assert not unknown, (
            f"ASL state ids {unknown!r} are neither cataloged machinery states nor "
            f"parseable pipeline steps — extend shared/step_names.py (task 14.2 pin)"
        )
