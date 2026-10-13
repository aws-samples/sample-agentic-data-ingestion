# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK assertion tests for the VERIFICATION_EXECUTOR two-sided wiring (P42 R2.5, task 2.5).

THE FAILURE MODE THIS FILE EXISTS TO CATCH — read before editing.

`compute-stack.ts` sets `USE_AGENTCORE_RUNTIME: 'true'` on the code-testing
Lambda, so that handler only DISPATCHES to the AgentCore container
(`lambda_function.py:1443-1450`); the verification runner executes in the
container, not in the Lambda process. A `VERIFICATION_EXECUTOR` set only on the
Lambda is therefore INERT on the live path — it changes nothing — while a
one-sided test suite stays green. Design §4.2's MECHANISM note names this
exactly. The whole point of this file is that it fails when EITHER side is
missing, so both `test_*_lambda_*` and `test_*_container_*` below are
load-bearing; deleting one restores the failure mode.

Assertions are scoped to the specific env block on each side rather than to
file-wide presence. A bare `"VERIFICATION_EXECUTOR" in source` would pass if the
variable were moved onto some other Lambda, which is a different flavour of the
same inertness.

The deployed value is asserted, not just presence: a wrong value would silently
change the live verification path.

**AMENDED, NOT DELETED — the pinned value is now `glue`, and that is a
deliberate reversal.** This file originally pinned `pandas` on both sides, on
R2.3 (safe fall-through) and R11.2 (hold the default until the concurrent-session
ceiling is measured). The Glue interactive-session executor has since been made
the active verification path in the deployed environment, so the pin moved with
it. Per ``LGR-60c4985-6`` (**template / ratified**) this repo amends an assertion
that encoded a hold rather than deleting it, so the reversal stays legible as a
decision instead of reading as a regression; ``LGR-5906ec6-2`` (**template /
ratified**) is where the hold-as-assertion convention comes from.

Two things did NOT change and must not be confused with this:

* **R2.3 is untouched.** `pandas` is still the runtime fall-through for an
  unrecognised or empty value. That lives in `lambda_function.py`, is pinned by
  `tests/unit/lambdas/test_code/test_verification_executor_selection.py` and
  `test_executor_selection_totality_prop.py`, and no assertion there moved.
* **R11.2's precondition is still unmet.** R11.2 holds `pandas` WHILE the
  concurrency measurement of R11.1 is absent, and it remains absent — task 1.3's
  probe was written and deliberately never run because running it creates
  billable Glue sessions. So this is an accepted DEVIATION from R11.2, not a
  satisfaction of it. What makes the deviation tolerable: exhausting the ceiling
  produces a capacity refusal that the harness maps to the
  `session_capacity_exhausted` non-verification, so an affected run reports "not
  verified" rather than failing — an abstention, not an error.

The pin stays as a pin: it is still an exact-value assertion, so moving the
deployed executor again (back to `pandas`, or on to `shadow`) still has to edit
this file on purpose.

Why a dedicated file rather than extending `test_lambda_env_table_names.py`:
that module is scoped to table-name standardization in compute-stack.ts
(AC-14.3), reads only compute-stack.ts, and asserts nothing about
agentcore-stack.ts — which is the half the inert-flag mode lives in. A failure
there would also report as a table-name regression. `test_model_call_budget_table.py`
is the closer precedent: it pins one env var across the compute Lambda env AND
`agentContainerEnv`, which is the same two-sided shape, and this file follows it.

Pinning a CDK shape with a test rather than trusting it follows the same posture
as `LGR-fb2310b-1` / `LGR-b2f79ef-7` (cited by task 2.3) and the naming-form pin
in `test/foundation-iam.test.ts`. Note that a symbol graph cannot help here:
`VERIFICATION_EXECUTOR` is an object-literal key, not an indexed symbol, so
text-scoped assertions are the available instrument.

Validates: Requirement 2.5
"""

import os
import re

import pytest

_LIB_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib")
COMPUTE_STACK_PATH = os.path.join(_LIB_DIR, "compute-stack.ts")
AGENTCORE_STACK_PATH = os.path.join(_LIB_DIR, "agentcore-stack.ts")

ENV_VAR = "VERIFICATION_EXECUTOR"

#: The value both stacks must deploy.
#:
#: WAS ``"pandas"`` (R2.3 fall-through + R11.2's hold). AMENDED to ``"glue"``
#: when the Glue interactive-session executor was made the active verification
#: path — a deliberate reversal of that hold, amended rather than deleted per
#: ``LGR-60c4985-6``. R11.1's concurrency measurement is still absent, so this is
#: an accepted deviation from R11.2 and not a satisfaction of it; see the module
#: docstring for what was weighed. R2.3's runtime fall-through to ``pandas`` is
#: unchanged and is pinned elsewhere.
DEPLOYED_VALUE = "glue"

#: The value R11.2 originally pinned, kept named so the assertions below can say
#: what moved and so a revert reads as a revert.
PRE_REVERSAL_VALUE = "pandas"

# R2.3 / design §4.2: the flag is three-valued. A boolean cannot express shadow.
ALLOWED_VALUES = frozenset({"pandas", "glue", "shadow"})


@pytest.fixture(scope="module")
def compute_stack_content():
    with open(COMPUTE_STACK_PATH) as f:
        return f.read()


@pytest.fixture(scope="module")
def agentcore_stack_content():
    with open(AGENTCORE_STACK_PATH) as f:
        return f.read()


def _code_testing_env_block(content: str) -> str:
    """The `environment: {...}` object of the CodeTesting Lambda only.

    Scoped deliberately: the variable must be on the code-testing Lambda, not
    merely somewhere in compute-stack.ts.
    """
    match = re.search(
        r"codeTestingLambda = new lambda\.Function\(this, 'CodeTesting',.*?environment:\s*\{(.*?)\n\s*\},",
        content,
        re.DOTALL,
    )
    assert match, "CodeTesting Lambda definition not found in compute-stack.ts"
    return match.group(1)


def _agent_container_env_block(content: str) -> str:
    """The body of the `agentContainerEnv` helper only.

    This is the env every agent runtime container receives, including
    TestCodeAgentRuntime — the live verification path.
    """
    match = re.search(
        r"const agentContainerEnv = \(serviceName: string\) => \(\{(.*?)\n\s*\}\);",
        content,
        re.DOTALL,
    )
    assert match, "agentContainerEnv helper not found in agentcore-stack.ts"
    return match.group(1)


def _assigned_value(env_block: str) -> str | None:
    """The literal assigned to VERIFICATION_EXECUTOR in an env block, or None.

    Ignores commented-out lines so that a variable which survives only inside a
    `//` comment does not read as present.
    """
    for line in env_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        match = re.match(rf"{ENV_VAR}:\s*'([^']*)'", stripped)
        if match:
            return match.group(1)
    return None


# ── Side 1: the code-testing Lambda (inline break-glass path) ───────


class TestComputeStackLambdaEnv:
    """The variable reaches the code-testing Lambda's own environment."""

    def test_present_in_code_testing_lambda_env(self, compute_stack_content):
        env_block = _code_testing_env_block(compute_stack_content)
        assert _assigned_value(env_block) is not None, (
            f"{ENV_VAR} missing from the CodeTesting Lambda environment in compute-stack.ts. "
            "This is the inline break-glass path (used when USE_AGENTCORE_RUNTIME is flipped off)."
        )

    def test_deployed_value_is_glue_in_lambda_env(self, compute_stack_content):
        """AMENDED, not deleted: was ``== "pandas"``, now ``== "glue"``.

        This assertion read ``_assigned_value(env_block) == "pandas"`` and cited
        R2.3 and R11.2. It was changed on purpose when the Glue interactive-session
        executor became the active verification path — ``LGR-60c4985-6``'s house
        pattern, so the reversal is legible here rather than looking like drift.
        R11.1's concurrency measurement is still absent, so this is an accepted
        deviation from R11.2. The assertion is still exact-value, so moving the
        executor again still has to come through this line.
        """
        value = _assigned_value(_code_testing_env_block(compute_stack_content))
        assert value == DEPLOYED_VALUE, (
            f"{ENV_VAR} on the CodeTesting Lambda must be '{DEPLOYED_VALUE}', got {value!r}. "
            "This is the inline break-glass copy; it must track the container copy. If you are "
            f"reverting to '{PRE_REVERSAL_VALUE}', change both stacks and this file together."
        )

    def test_not_on_some_other_lambda_only(self, compute_stack_content):
        """File-wide presence is not enough — it must be inside CodeTesting's block.

        Guards the case where the variable is moved to a neighbouring Lambda: the
        text would still appear in the file while the code-testing path lost it.
        """
        assert ENV_VAR in compute_stack_content, f"{ENV_VAR} absent from compute-stack.ts entirely"
        env_block = _code_testing_env_block(compute_stack_content)
        assert ENV_VAR in env_block, (
            f"{ENV_VAR} appears in compute-stack.ts but NOT in the CodeTesting Lambda's "
            "environment block — it has been attached to the wrong function"
        )


# ── Side 2: the agent container env (the live path) ─────────────────


class TestAgentCoreContainerEnv:
    """The variable reaches the agent containers, where the runner actually executes."""

    def test_present_in_agent_container_env(self, agentcore_stack_content):
        env_block = _agent_container_env_block(agentcore_stack_content)
        assert _assigned_value(env_block) is not None, (
            f"{ENV_VAR} missing from agentContainerEnv in agentcore-stack.ts. "
            "This is the copy the LIVE path reads (USE_AGENTCORE_RUNTIME: 'true' "
            "dispatches verification into the container) — without it the flag is inert."
        )

    def test_deployed_value_is_glue_in_container_env(self, agentcore_stack_content):
        """AMENDED, not deleted: was ``== "pandas"``, now ``== "glue"``.

        This assertion read ``_assigned_value(env_block) == "pandas"`` and cited
        R2.3 and R11.2. It was changed on purpose, per ``LGR-60c4985-6``, when the
        Glue interactive-session executor became the active verification path.

        This is the copy that MATTERS: ``USE_AGENTCORE_RUNTIME: 'true'`` means the
        runner executes in the container, so this side is what actually makes
        real Spark the verification path. Amending only the sibling assertion
        above would have recorded a reversal that never took effect.
        """
        value = _assigned_value(_agent_container_env_block(agentcore_stack_content))
        assert value == DEPLOYED_VALUE, (
            f"{ENV_VAR} in agentContainerEnv must be '{DEPLOYED_VALUE}', got {value!r}. This is "
            "the LIVE path's copy. If you are reverting to "
            f"'{PRE_REVERSAL_VALUE}', change both stacks and this file together."
        )

    def test_test_code_runtime_consumes_agent_container_env(self, agentcore_stack_content):
        """TestCodeAgentRuntime must source its env from `agentContainerEnv`.

        Without this, the inert-flag mode reappears one level up: the variable
        could sit in the helper while the runtime that actually runs verification
        was switched to an inline literal that omits it. TestCodeAgentRuntime is
        the live path — it is the runtime the code-testing Lambda dispatches to.
        """
        match = re.search(
            r"new cdk\.CfnResource\(this, 'TestCodeAgentRuntime',.*?EnvironmentVariables:\s*([^,\n]+)",
            agentcore_stack_content,
            re.DOTALL,
        )
        assert match, "TestCodeAgentRuntime definition not found in agentcore-stack.ts"
        env_source = match.group(1)
        assert "agentContainerEnv" in env_source, (
            "TestCodeAgentRuntime's EnvironmentVariables must come from agentContainerEnv "
            f"(got {env_source.strip()!r}) — otherwise the shared env, and with it "
            f"{ENV_VAR}, does not reach the runtime that performs verification"
        )


# ── Both sides together — the assertion the task is actually about ──


class TestBothSidesPresent:
    """One-sided wiring is the failure mode; these tests are the two-sided check."""

    def test_variable_set_in_both_environments(self, compute_stack_content, agentcore_stack_content):
        """Fails if EITHER side is missing, naming which one.

        This is the assertion design §4.2's MECHANISM note asks for. A variable
        set on the Lambda alone changes nothing on the live path; set in the
        container alone it is missing from the documented break-glass path.
        """
        lambda_value = _assigned_value(_code_testing_env_block(compute_stack_content))
        container_value = _assigned_value(_agent_container_env_block(agentcore_stack_content))

        missing = [
            name
            for name, value in (
                ("compute-stack.ts CodeTesting Lambda environment", lambda_value),
                ("agentcore-stack.ts agentContainerEnv", container_value),
            )
            if value is None
        ]
        assert not missing, (
            f"{ENV_VAR} must be set in BOTH environments; missing from: {missing}. "
            "One-sided wiring produces a flag that is inert on the live path while "
            "every other test still passes (design §4.2 MECHANISM)."
        )

    def test_both_sides_agree_on_the_value(self, compute_stack_content, agentcore_stack_content):
        """The two copies must not drift apart.

        Both present but disagreeing is a subtler version of the same bug: the
        executor would depend on which path served the run.
        """
        lambda_value = _assigned_value(_code_testing_env_block(compute_stack_content))
        container_value = _assigned_value(_agent_container_env_block(agentcore_stack_content))

        assert lambda_value == container_value, (
            f"{ENV_VAR} disagrees between environments: "
            f"Lambda={lambda_value!r}, container={container_value!r}. "
            "The selected executor would then depend on which path served the run."
        )

    def test_value_is_in_the_allowed_set(self, compute_stack_content, agentcore_stack_content):
        """Both copies hold one of {pandas, glue, shadow} (R2.3, design §4.2).

        UNCHANGED by the `pandas` → `glue` reversal, deliberately: `glue` was
        always a member of the vocabulary, so this assertion never encoded the
        hold and there was nothing here to amend. What it guards is a near-miss
        spelling reaching a stack — `"Glue"`, `"glue "` — which resolution refuses
        by design, silently falling back to the pandas proxy. That hazard got
        WORSE with the reversal, not better: before, a typo left the deployed
        behaviour where it already was; now a typo silently undoes the change.
        """
        for label, value in (
            ("compute-stack.ts", _assigned_value(_code_testing_env_block(compute_stack_content))),
            ("agentcore-stack.ts", _assigned_value(_agent_container_env_block(agentcore_stack_content))),
        ):
            assert value in ALLOWED_VALUES, (
                f"{ENV_VAR} in {label} is {value!r}, which is outside {sorted(ALLOWED_VALUES)}. "
                "An out-of-set value resolves to 'pandas' at runtime but should not be deployed — "
                "and now that 'glue' is the intended executor, that fall-back silently reverts it."
            )
