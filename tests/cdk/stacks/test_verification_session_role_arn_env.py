# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK assertion tests for the VERIFICATION_SESSION_ROLE_ARN two-sided wiring (P42 R6.4).

THE FAILURE MODE THIS FILE EXISTS TO CATCH — read before editing.

This is the sibling of ``test_verification_executor_env.py`` and holds the second
variable to the same standard. Both are read by the code-testing handler, both
are inert unless published on BOTH sides, and the executor variable already had
this file's protection while this one had none — which is precisely how it came
to be declared in Python and published by no stack at all.

Two distinct failures are pinned here:

1. **One-sided publication.** ``compute-stack.ts`` sets
   ``USE_AGENTCORE_RUNTIME: 'true'`` on the code-testing Lambda, so that handler
   only DISPATCHES to the AgentCore container (``lambda_function.py:1443-1450``);
   the Glue_Session_Runner executes in the container. A value set only on the
   Lambda is therefore inert on the live path while every test still passes —
   design §4.2's MECHANISM note, and design.md:158 states the general rule: any
   environment variable the runner reads must be in ``agentContainerEnv`` as well
   as on the Lambda.

2. **A reconstructed ARN.** ``lambda_function.py:454-473`` records that building
   the ARN from account and region was rejected: it encodes a CDK physical name
   in Python and silently produces a valid-LOOKING ARN for a role that may not
   exist, converting a missing resource into an opaque ``AccessDenied`` at
   ``CreateSession``. So presence alone is not enough — the value must come from
   the Foundation construct. ``foundation-stack.ts:973-977`` makes the identical
   choice for the ``iam:PassRole`` grant, in a comment naming the same hazard
   ("Reference the role object rather than rebuilding its ARN as a string, so a
   rename ... cannot leave this grant pointing at a role that no longer exists").

**Consequence of the gap this file closes.** With the variable unpublished,
``resolve_effective_executor`` (``lambda_function.py:590-598``) downgrades a
requested ``glue`` to ``pandas`` and logs at ERROR, so neither ``glue`` nor
``shadow`` was selectable in a deployed environment however an operator
configured ``VERIFICATION_EXECUTOR``. The completed runner was unreachable.

**What this file deliberately does NOT assert.** WHAT value
``VERIFICATION_EXECUTOR`` holds — ``test_verification_executor_env.py`` owns that,
and duplicating it would give two files one claim. The cross-checks in
:class:`TestSelectableNotSelected` exist because they are the specific hazard of
*this* variable rather than of that one: wiring the role ARN must not move the
executor as a side-effect.

**That class was reworked when the executor was deliberately flipped.** It
originally held one test asserting the executor was literally ``pandas``, because
publishing the role ARN made ``glue`` **selectable** and R11.2 required it stay
**unselected**. The Glue executor has since been made the active verification
path on purpose, so a literal ``pandas`` assertion would now fail for the wrong
reason. Per ``LGR-60c4985-6`` it was amended rather than deleted: the hazard is
still pinned, now as "the intended value, sourced from the sibling, and identical
in both stacks", so an accidental one-sided change still fails while an
intentional recorded one does not. Note that R11.2's precondition remains unmet —
R11.1's concurrent-session measurement was deliberately never taken, because
task 1.3's probe creates billable sessions — so the deployed value is an accepted
deviation from R11.2, not a satisfaction of it.

**Why source-text assertions.** Same instrument, same reason as the sibling file:
``VERIFICATION_SESSION_ROLE_ARN`` is an object-literal key, not an indexed
symbol, so a symbol graph cannot see it. Pinning a CDK shape with a test rather
than trusting it follows ``LGR-fb2310b-1`` and ``LGR-b2f79ef-7`` (both template /
ratified), the latter explicitly because a regression here would surface as an
``AccessDenied`` that no test layer below CDK can observe.

Validates: Requirement 6.4
"""

import importlib.util
import os
import re

import pytest

_LIB_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib")
COMPUTE_STACK_PATH = os.path.join(_LIB_DIR, "compute-stack.ts")
AGENTCORE_STACK_PATH = os.path.join(_LIB_DIR, "agentcore-stack.ts")

ENV_VAR = "VERIFICATION_SESSION_ROLE_ARN"

# The Foundation stack property holding the role, and the expression the two
# stacks must use. Both stacks already receive `foundation` as a prop, so this is
# an object reference rather than a string import: compute-stack.ts:692
# (`GLUE_PIPELINE_ROLE_ARN`) and agentcore-stack.ts (`agentCoreRuntimeRole.roleArn`,
# 13 sites) are the in-repo precedents for a role ARN crossing Foundation → here.
FOUNDATION_ROLE_PROP = "verificationSessionRole"
EXPECTED_EXPRESSION = f"foundation.{FOUNDATION_ROLE_PROP}.roleArn"

EXECUTOR_ENV_VAR = "VERIFICATION_EXECUTOR"

# The expected executor value is SOURCED from the sibling module rather than
# re-spelled here.
#
# It used to be re-spelled: `EXECUTOR_PINNED_DEFAULT = "pandas"`. That was safe
# only while the value never moved. It has now moved once — `pandas` → `glue`,
# the deliberate R11.2 reversal recorded in the sibling — and a literal in two
# files is exactly how the next move produces a failure in this file that reads
# as a regression rather than as the reversal it is. Single-sourcing keeps this
# file's claim about the COUPLING (see TestSelectableNotSelected) while leaving
# the VALUE owned by `test_verification_executor_env.py`, which is the division
# this module's docstring already describes.
_SIBLING_PATH = os.path.join(os.path.dirname(__file__), "test_verification_executor_env.py")
_sibling_spec = importlib.util.spec_from_file_location("_verification_executor_env_pins", _SIBLING_PATH)
assert _sibling_spec and _sibling_spec.loader, f"could not load the executor pin module at {_SIBLING_PATH}"
_executor_pins = importlib.util.module_from_spec(_sibling_spec)
_sibling_spec.loader.exec_module(_executor_pins)

#: Whatever the sibling pins as the deployed executor. `glue` at the time of
#: writing; the point is that this file does not get an independent opinion.
EXECUTOR_EXPECTED = _executor_pins.DEPLOYED_VALUE
EXECUTOR_VOCABULARY = _executor_pins.ALLOWED_VALUES


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

    Scoped deliberately, as in the sibling file: the variable must be on the
    code-testing Lambda, not merely somewhere in compute-stack.ts.
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


def _assigned_expression(env_block: str, env_var: str = ENV_VAR) -> str | None:
    """The right-hand side assigned to `env_var` in an env block, or None.

    Unlike the sibling file's `_assigned_value`, this returns the raw expression
    rather than the contents of a quoted literal: the correct value here IS an
    expression (`foundation.verificationSessionRole.roleArn`), and a quoted
    literal is the failure this file is looking for.

    Comment lines are skipped so that a variable surviving only inside a `//`
    comment does not read as present.
    """
    for line in env_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        match = re.match(rf"{env_var}:\s*(.+?),?\s*$", stripped)
        if match:
            return match.group(1).rstrip(",").strip()
    return None


# ── Side 1: the code-testing Lambda (inline break-glass path) ───────


class TestComputeStackLambdaEnv:
    """The ARN reaches the code-testing Lambda's own environment."""

    def test_present_in_code_testing_lambda_env(self, compute_stack_content):
        env_block = _code_testing_env_block(compute_stack_content)
        assert _assigned_expression(env_block) is not None, (
            f"{ENV_VAR} missing from the CodeTesting Lambda environment in compute-stack.ts. "
            "This is the inline break-glass path (used when USE_AGENTCORE_RUNTIME is flipped off)."
        )

    def test_lambda_env_references_the_foundation_construct(self, compute_stack_content):
        env_block = _code_testing_env_block(compute_stack_content)
        expression = _assigned_expression(env_block)
        assert expression == EXPECTED_EXPRESSION, (
            f"{ENV_VAR} on the CodeTesting Lambda must be {EXPECTED_EXPRESSION!r} so CDK renders "
            f"the cross-stack reference, got {expression!r}. Rebuilding the ARN from account and "
            "region was rejected (lambda_function.py:454-473): it yields a valid-looking ARN for "
            "a role that may not exist, so a missing role becomes an opaque AccessDenied."
        )

    def test_not_on_some_other_lambda_only(self, compute_stack_content):
        """File-wide presence is not enough — it must be inside CodeTesting's block."""
        assert ENV_VAR in compute_stack_content, f"{ENV_VAR} absent from compute-stack.ts entirely"
        env_block = _code_testing_env_block(compute_stack_content)
        assert ENV_VAR in env_block, (
            f"{ENV_VAR} appears in compute-stack.ts but NOT in the CodeTesting Lambda's "
            "environment block — it has been attached to the wrong function"
        )


# ── Side 2: the agent container env (the live path) ─────────────────


class TestAgentCoreContainerEnv:
    """The ARN reaches the agent containers, where the runner actually executes."""

    def test_present_in_agent_container_env(self, agentcore_stack_content):
        env_block = _agent_container_env_block(agentcore_stack_content)
        assert _assigned_expression(env_block) is not None, (
            f"{ENV_VAR} missing from agentContainerEnv in agentcore-stack.ts. This is the copy "
            "the LIVE path reads (USE_AGENTCORE_RUNTIME: 'true' dispatches verification into the "
            "container) — without it resolve_effective_executor downgrades 'glue' to 'pandas' and "
            "logs at ERROR, so the Glue runner is unreachable in a deployed environment."
        )

    def test_container_env_references_the_foundation_construct(self, agentcore_stack_content):
        env_block = _agent_container_env_block(agentcore_stack_content)
        expression = _assigned_expression(env_block)
        assert expression == EXPECTED_EXPRESSION, (
            f"{ENV_VAR} in agentContainerEnv must be {EXPECTED_EXPRESSION!r}, got {expression!r}. "
            "Note that the bucket names in this same helper ARE interpolated from account and "
            "region; that asymmetry is deliberate and must not be 'made consistent' here — see "
            "lambda_function.py:454-473 and the foundation-stack.ts:973-977 PassRole comment."
        )


# ── The rejected alternative, pinned negatively ─────────────────────


class TestArnIsNotReconstructed:
    """No copy of the variable may be built from account and region.

    The positive assertions above already require the exact construct expression,
    so these are redundant on a green tree — deliberately. They fail with a
    message that names the rejected alternative, which is the information a
    future editor reaching for `arn:aws:iam::${account}:role/...` needs. Without
    them the failure reads only as an unexpected string.
    """

    @pytest.mark.parametrize(
        "label,path_key",
        [
            ("compute-stack.ts CodeTesting Lambda environment", "compute"),
            ("agentcore-stack.ts agentContainerEnv", "agentcore"),
        ],
    )
    def test_no_iam_arn_literal_assigned(
        self, label, path_key, compute_stack_content, agentcore_stack_content
    ):
        env_block = (
            _code_testing_env_block(compute_stack_content)
            if path_key == "compute"
            else _agent_container_env_block(agentcore_stack_content)
        )
        expression = _assigned_expression(env_block)
        assert expression is not None, f"{ENV_VAR} missing from {label}"
        assert "arn:aws:iam" not in expression, (
            f"{ENV_VAR} in {label} is assembled from an ARN literal ({expression!r}). "
            "Rejected: it encodes the CDK physical name "
            "AgenticDataIngestion-VerificationSession-Role outside CDK, and produces a "
            "well-formed ARN even when the role is absent or renamed — the failure then "
            "surfaces as AccessDenied at CreateSession rather than at synth or deploy."
        )

    @pytest.mark.parametrize(
        "label,path_key",
        [
            ("compute-stack.ts CodeTesting Lambda environment", "compute"),
            ("agentcore-stack.ts agentContainerEnv", "agentcore"),
        ],
    )
    def test_no_account_or_region_interpolation(
        self, label, path_key, compute_stack_content, agentcore_stack_content
    ):
        env_block = (
            _code_testing_env_block(compute_stack_content)
            if path_key == "compute"
            else _agent_container_env_block(agentcore_stack_content)
        )
        expression = _assigned_expression(env_block)
        assert expression is not None, f"{ENV_VAR} missing from {label}"
        for token in ("${account}", "${region}"):
            assert token not in expression, (
                f"{ENV_VAR} in {label} interpolates {token} ({expression!r}). The role must be "
                "reached through the Foundation construct so CDK owns the name."
            )


# ── Both sides together — the assertion the change is actually about ──


class TestBothSidesPresent:
    """One-sided publication is the failure mode; these are the two-sided check."""

    def test_variable_set_in_both_environments(self, compute_stack_content, agentcore_stack_content):
        """Fails if EITHER side is missing, naming which one.

        The state this change fixed was worse than one-sided: the variable was
        declared in Python (`VERIFICATION_SESSION_ROLE_ARN_ENV`) and published by
        neither stack, so the completed Glue runner could not be selected in any
        deployed environment.
        """
        lambda_value = _assigned_expression(_code_testing_env_block(compute_stack_content))
        container_value = _assigned_expression(_agent_container_env_block(agentcore_stack_content))

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
            "One-sided publication produces a value that is inert on the live path while "
            "every other test still passes (design §4.2 MECHANISM, design.md:158)."
        )

    def test_both_sides_agree_on_the_expression(self, compute_stack_content, agentcore_stack_content):
        """The two copies must not drift apart.

        Both present but disagreeing would mean the session role depended on
        which path served the run — and one of the two could be pointing at a
        role that does not exist.
        """
        lambda_value = _assigned_expression(_code_testing_env_block(compute_stack_content))
        container_value = _assigned_expression(_agent_container_env_block(agentcore_stack_content))

        assert lambda_value == container_value, (
            f"{ENV_VAR} disagrees between environments: "
            f"Lambda={lambda_value!r}, container={container_value!r}."
        )


# ── The hazard specific to publishing the ARN ───────────────────────


class TestSelectableNotSelected:
    """The role ARN makes `glue` reachable; the executor variable is what selects it.

    **REWORKED, NOT DELETED.** This class held one test asserting
    ``VERIFICATION_EXECUTOR == "'pandas'"`` in both stacks, guarding the hazard
    that publishing the role ARN accidentally flipped the executor as a
    side-effect. The executor has since been flipped ON PURPOSE (`pandas` →
    `glue`, the deliberate R11.2 reversal recorded in
    ``test_verification_executor_env.py``), so a literal `pandas` assertion here
    would now fail for the wrong reason and reads as a regression.

    Deleting it was rejected — ``LGR-60c4985-6`` (**template / ratified**) is
    explicit that an assertion encoding a hold is amended so the reversal stays
    legible, and ``LGR-5906ec6-2`` (**template / ratified**) is where the
    hold-as-assertion convention comes from. So the ORIGINAL HAZARD IS STILL
    PINNED, just stated in a form that survives an intentional move: the executor
    value must be the intended one and identical on both sides, sourced from the
    sibling's constant rather than re-spelled. An accidental change — to either
    stack, in either direction — still fails here, which is what the original
    test was for. What no longer fails is an intentional change made in both
    stacks and recorded in the sibling.
    """

    def test_the_role_arn_wiring_did_not_change_the_executor_by_accident(
        self, compute_stack_content, agentcore_stack_content
    ):
        """`VERIFICATION_EXECUTOR` is the intended value in both stacks.

        Cross-check rather than duplication, as before:
        ``test_verification_executor_env.py`` owns WHAT the value is, and this
        asserts that a change to ``VERIFICATION_SESSION_ROLE_ARN`` did not move it
        as a side-effect. The expected value is imported from that module, so the
        two files cannot disagree and a future move needs one edit, in the file
        that owns it.

        **Amended from** ``executor == "'pandas'"`` **on the** `pandas` → `glue`
        **reversal.** R11.2's precondition is still unmet — R11.1's
        concurrent-session measurement was deliberately never taken, because
        task 1.3's probe creates billable sessions — so the current value is an
        accepted deviation from R11.2 rather than a satisfaction of it.
        """
        for label, env_block in (
            ("compute-stack.ts CodeTesting Lambda", _code_testing_env_block(compute_stack_content)),
            ("agentcore-stack.ts agentContainerEnv", _agent_container_env_block(agentcore_stack_content)),
        ):
            executor = _assigned_expression(env_block, EXECUTOR_ENV_VAR)
            assert executor == f"'{EXECUTOR_EXPECTED}'", (
                f"{EXECUTOR_ENV_VAR} in {label} is {executor!r}, expected "
                f"'{EXECUTOR_EXPECTED}' (sourced from test_verification_executor_env.py). "
                f"Changing {ENV_VAR} must not move the executor as a side-effect. If you meant "
                "to change the executor, change BOTH stacks and amend "
                "test_verification_executor_env.py's pin, which is where that decision lives."
            )

    def test_the_two_stacks_do_not_disagree_about_the_executor(self, compute_stack_content, agentcore_stack_content):
        """A one-sided executor change is caught even if the pin is stale.

        Added with the rework above, because that test compares against an
        imported constant: if someone edits both stacks and the sibling pin but
        misses one stack, the constant still matches the stack they did edit. This
        asserts the two stacks agree with EACH OTHER, which needs no external
        reference and therefore cannot be defeated by updating the pin.

        A disagreement is the inert-flag mode in its subtlest form: the executor
        would depend on whether the run went through the container or the inline
        break-glass path.
        """
        lambda_executor = _assigned_expression(_code_testing_env_block(compute_stack_content), EXECUTOR_ENV_VAR)
        container = _agent_container_env_block(agentcore_stack_content)
        container_executor = _assigned_expression(container, EXECUTOR_ENV_VAR)
        assert lambda_executor == container_executor, (
            f"{EXECUTOR_ENV_VAR} disagrees between stacks: Lambda={lambda_executor!r}, "
            f"container={container_executor!r}. The container copy is the live path; the Lambda "
            "copy is the break-glass path. Whichever is wrong, one of them is not the executor "
            "anyone intended."
        )

    def test_the_executor_value_is_a_real_vocabulary_member(self, compute_stack_content, agentcore_stack_content):
        """Whatever the executor is, it must be selectable rather than a near-miss.

        Also added with the rework, and it matters MORE now than under the hold.
        Resolution refuses near-misses by design (`"Glue"`, `"glue "`) and falls
        back to the pandas proxy. Under the old `pandas` pin a typo left behaviour
        where it already was; now a typo silently reverts the reversal, and the
        only symptom is a WARNING in a log.
        """
        for label, env_block in (
            ("compute-stack.ts CodeTesting Lambda", _code_testing_env_block(compute_stack_content)),
            ("agentcore-stack.ts agentContainerEnv", _agent_container_env_block(agentcore_stack_content)),
        ):
            executor = _assigned_expression(env_block, EXECUTOR_ENV_VAR)
            assert executor is not None, f"{EXECUTOR_ENV_VAR} missing from {label}"
            unquoted = executor.strip("'")
            assert unquoted in EXECUTOR_VOCABULARY, (
                f"{EXECUTOR_ENV_VAR} in {label} is {executor!r}, outside "
                f"{sorted(EXECUTOR_VOCABULARY)}. Resolution would fall back to the pandas proxy "
                "and only log a WARNING, so the deployed executor would not be the intended one."
            )
