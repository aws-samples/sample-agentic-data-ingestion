# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The retry resume marker reaches the resumable steps (p32 2.18, 3.18).

``shared/retry_utils.resolve_step_resume`` can only decide anything if the
step's Lambda event carries ``retry_from_step``. Only ``FileValidation``
receives the whole execution state — every other state builds an explicit
``payload`` — so the marker is echoed through FileValidation's own result and
read from there by the steps that can act on it.

Two properties are pinned here, and each corresponds to a way this breaks:

1. **The marker path is guaranteed-present.** Payloads read it from
   ``$.file_validation_result.retry_from_step``, which FileValidation always
   returns (``""`` when the run is not a retry), rather than from
   ``$.retry_from_step``, which is absent on every non-retry run. An absent
   path in a ``Parameters`` block is a ``States.ParameterPathFailure`` — a
   ``States.Runtime`` error no ``Retry`` or ``Catch`` can intercept, which is
   the trap p32 task 15 hit and documented.

2. **Only the resumable steps carry it** (Decision D3). ``step_handler`` is
   the OUTERMOST decorator on the four container-backed steps
   (``@step_handler`` then ``@step_task_token``), so a served cached result
   would return before the task token is sent and the ``waitForTaskToken``
   state would stall for its full ``TimeoutSeconds``. Threading the marker
   into a token step would invite exactly that.

3.18 is pinned here too: the start state is unchanged, and the mechanism adds
no ASL state at all — so nothing new needed cataloguing in
``test_asl_state_names_pinned.py`` or ``test_sfn_states_contract.py``.

Validates: p32 Requirements 2.18, 3.18
"""

import os
import re

import pytest

ORCHESTRATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "orchestration-stack.ts"
)

#: The marker path every resumable step's payload must read.
RESUME_MARKER_PATH = "$.file_validation_result.retry_from_step"

#: The five tasks.LambdaInvoke steps. FileValidation is the carrier — it takes
#: the whole state and needs no payload entry; the other four read the marker.
RESUME_MARKER_CONSUMERS = ["SchemaExtract", "SchemaCompare", "SchemaMapping", "CreateQualityChecks"]

#: waitForTaskToken steps — deliberately excluded (Decision D3).
TOKEN_STEPS = ["CreateCode", "TestCode", "CreateGluePipeline", "CreateInfrastructure", "RunPipeline"]


@pytest.fixture(scope="module")
def source() -> str:
    with open(ORCHESTRATION_STACK_PATH) as f:
        return f.read()


def _payload_block(source: str, state_id: str) -> str:
    """The text from a state's construct to its resultPath — i.e. its payload."""
    start = re.search(rf"(?:this,\s*|containerBackedTask\(\s*)'{re.escape(state_id)}'", source)
    assert start, f"state {state_id!r} not found in orchestration-stack.ts"
    tail = source[start.end() : start.end() + 2000]
    end = re.search(r"resultPath|ResultPath|'\$\.\w+_result'", tail)
    return tail[: end.end()] if end else tail


class TestResumeMarkerReachesResumableSteps:
    def test_the_marker_path_is_declared_once(self, source):
        assert f"const RESUME_MARKER_PATH = '{RESUME_MARKER_PATH}';" in source, (
            "the resume marker path is not declared — payloads must not each spell it out"
        )

    @pytest.mark.parametrize("state_id", RESUME_MARKER_CONSUMERS)
    def test_resumable_step_payload_carries_the_marker(self, source, state_id):
        block = _payload_block(source, state_id)
        assert "'retry_from_step.$': RESUME_MARKER_PATH" in block, (
            f"2.18 UNMET — {state_id}'s payload does not carry retry_from_step, so its "
            f"handler cannot tell it is before a retry's resume point and will re-execute."
        )

    @pytest.mark.parametrize("state_id", TOKEN_STEPS)
    def test_token_step_payload_does_not_carry_the_marker(self, source, state_id):
        """Decision D3: a served result there would never close the callback."""
        block = _payload_block(source, state_id)
        assert "retry_from_step" not in block, (
            f"{state_id} is a waitForTaskToken state. step_handler is its outermost "
            f"decorator, so serving a cached result would return before the token is sent "
            f"and the state would stall for its full TimeoutSeconds."
        )

    def test_no_payload_reads_the_bare_execution_input_key(self, source):
        """The trap: `$.retry_from_step` is absent on every non-retry run."""
        assert "'$.retry_from_step'" not in source, (
            "a payload reads $.retry_from_step directly. That path is absent on a fresh "
            "run, and an unresolvable Parameters path is a States.Runtime error no Retry "
            "or Catch can intercept (p32 task 15's documented lesson)."
        )


class TestFreshRunsAreUnchanged:
    def test_the_pipeline_still_starts_at_file_validation(self, source):
        """3.18, and the reason the marker rides on FileValidation's result."""
        assert "const definition = fileValidation;" in source

    def test_the_mechanism_adds_no_asl_state(self, source):
        """No Choice/Pass guard was needed, so no catalogue needs extending.

        P16 built 13 Choice/Pass skip-state pairs and reverted them in the same
        commit (`LGR-2600abb-1`: "Revert orchestration-stack.ts to original
        linear chain (CDK skip states removed)"). This mechanism reaches the
        same goal with zero new states, which is why the ASL-state catalogues
        are untouched.
        """
        for forbidden in ("ChoiceSkip_", "Skip_FileValidation", "CheckRetryFromStep", "ApplyDefaultRetryFromStep"):
            assert forbidden not in source, f"unexpected resume-related state {forbidden!r}"
