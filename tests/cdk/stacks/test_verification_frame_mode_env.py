# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK assertion tests for the VERIFICATION_FRAME_MODE two-sided wiring (P44 R1.7, task 15.2).

THE FAILURE MODE THIS FILE EXISTS TO CATCH — read before editing.

This is the third file in the family, after ``test_verification_executor_env.py``
and ``test_verification_session_role_arn_env.py``, and it holds the frame-mode
variable to the same standard: published on BOTH sides, the intended value on
each, and the two sides in agreement.

``compute-stack.ts`` sets ``USE_AGENTCORE_RUNTIME: 'true'`` on the code-testing
Lambda, so that handler only DISPATCHES; the Glue_Session_Runner executes in the
AgentCore container. So the obvious copy is the inert one — ``LGR-87b8538-1``
(**template / ratified**) rejected the one-stack form in its own words: setting
only ``compute-stack.ts`` "yields an inert flag on the live path while every test
passes". AgentCore's ``agentContainerEnv`` copy is the live path; Compute's is
the inline break-glass route.

**Why the two-sided check is load-bearing HERE in a way it no longer is for the
executor.** ``LGR-5108dd4-1`` (**template / ratified**) made ``glue`` the
code-level fall-through in ``resolve_verification_executor``, so omitting the
AgentCore copy of ``VERIFICATION_EXECUTOR`` now leaves the container resolving to
``glue`` anyway — both stack comments say so, and ``compute-stack.ts`` states
outright that ``test_verification_executor_env.py`` "is no longer what catches a
one-sided set". The frame mode has no such rescue. An unset variable means
**synthetic**: that is the runner's constructor default
(``glue_session_runner.py:595``, ``frame_mode: str = FRAME_MODE_SYNTHETIC``) and
it is what design §4.1's caller produces, reading the environment with ``or
FRAME_MODE_SYNTHETIC_DEFAULT`` rather than raising. So a missing AgentCore copy
leaves every live Glue verification measuring the transform against 100 rows
generated from the declared schema — which is the exact defect p44 exists to fix,
restored silently, with every other test still green.

**The expected literal is imported, not re-spelled.** ``LGR-87b8538-1`` also
rejected re-spelling in its own words: "a literal in two files is how the next
move manufactures a misleading failure, so it is imported from the file that owns
it, with a separate test that the two stacks agree with each other". The file
that owns the frame-mode vocabulary is ``glue_session_runner.py`` — it defines
the pair at ``:205`` / ``:210`` and ``resolve_frame_source`` compares against
those two constants by exact equality, no ``.strip()`` and no ``.lower()``
(``LGR-ad78946-1``). Importing them makes the near-miss assertion below
definitional rather than a second opinion: the deployed literal is checked
against the very bytes the resolver will compare it to. The sibling ARN file
takes the same posture one step differently, importing the executor pin from
``test_verification_executor_env.py`` because the *executor's* deployed value has
no production constant to source from — it is a deployment choice, not a
vocabulary member.

*What that import costs, stated rather than discovered.* This is the only file in
``tests/cdk/`` that imports production Python, so it is the only one that can
fail for a reason unrelated to CDK — a syntax error mid-edit in the test_code
Lambda directory surfaces here as a collection error. Weighed against
re-spelling ``'sample'``, which is what the record forbids, the coupling is the
cheaper failure: it is loud, immediate, and names the real file. ``sys.path`` is
restored after the import so the rest of this layer runs with the path it had.

**Also asserted: membership in the allowed pair.** A near-miss spelling is NOT
normalised — ``'Sample'`` or ``'sample '`` takes ``resolve_frame_source``'s
unrecognised arm, builds a synthetic frame and logs a WARNING naming the value
(``LGR-ad78946-1``), so a typo here silently undoes the change and the only
symptom is one log line. That hazard is strictly worse for this variable than it
was for the executor under its old ``pandas`` pin, where a typo left deployed
behaviour where it already was.

**What this file deliberately does NOT assert**, mirroring the division the ARN
sibling documents:

* **that ``TestCodeAgentRuntime`` sources its env from ``agentContainerEnv``.**
  ``test_verification_executor_env.py`` owns that claim
  (``test_test_code_runtime_consumes_agent_container_env``) and it is not
  frame-mode-specific — duplicating it would give two files one claim.
* **what ``VERIFICATION_EXECUTOR`` holds.** Also that file's, and unchanged by
  this one. ``LGR-5108dd4-2`` (**template / ratified**) is the amend-don't-delete
  rule for those pins; nothing here moves or relaxes any of them. This is a new
  file, and it adds assertions only.
* **deploy order.** Both stack comments carry C11 / R7.5 — the Foundation S3
  grant must land before this caller, or every single-file S3-upload
  verification abstains — but no test in any layer can observe the order two
  stacks were deployed in. It is a criterion, not an assertion, precisely
  because every test passes either way.

**Why source-text assertions, and the residual that leaves.**
``VERIFICATION_FRAME_MODE`` is an object-literal key, not an indexed symbol, so a
symbol graph cannot see it — confirmed rather than assumed: a codegraph
``explore`` for the variable and for ``agentContainerEnv`` returned unrelated
``ENVIRONMENT`` symbols from Python modules and nothing from either stack.
``LGR-1166391-1`` (**template / ratified**) records why text is the instrument
available in this layer at all: ``aws-cdk-lib`` is absent from
``pyproject.toml``, so ``tests/cdk/`` cannot synthesize a template, and every
file here asserts on ``.ts`` source. That record also names the residual — a
source scan can pass on input that still fails at deploy, which is how a
description assembled from three concatenated literals slipped through. The
residual is narrow here: the value is a single quoted literal in an object
literal, not an assembled string, and task 15.1 read the synthesized templates
once by hand (1× in Compute on ``CodeTesting794F2457``, 9× in AgentCore, one per
agent runtime from the shared helper — byte-symmetric with
``VERIFICATION_EXECUTOR``'s own split). Pinning a CDK shape with a test rather
than trusting it follows ``LGR-b2f79ef-7`` via that record: the failure would
otherwise surface only as a run that quietly verified nothing about real data.

Validates: Requirement 1.7
"""

import os
import re
import sys

import pytest

_LIB_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib")
COMPUTE_STACK_PATH = os.path.join(_LIB_DIR, "compute-stack.ts")
AGENTCORE_STACK_PATH = os.path.join(_LIB_DIR, "agentcore-stack.ts")

ENV_VAR = "VERIFICATION_FRAME_MODE"

# The frame-mode vocabulary is IMPORTED from the module that owns it rather than
# re-spelled here (`LGR-87b8538-1`; see the module docstring for the quote and
# for what the coupling costs). `glue_session_runner.py` defines both constants
# and `resolve_frame_source` compares against them by exact equality, so the
# assertions below check the deployed literal against the bytes the resolver will
# actually use.
#
# The path is inserted for the import and removed again: every other file in
# `tests/cdk/` reads `.ts` text and imports no production code, and leaving a
# Lambda directory at `sys.path[0]` for the rest of the session could shadow a
# top-level module name in a combined run.
_TEST_CODE_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_test_code"
)
sys.path.insert(0, _TEST_CODE_LAMBDA_DIR)
try:
    from glue_session_runner import FRAME_MODE_SAMPLE, FRAME_MODE_SYNTHETIC
finally:
    if _TEST_CODE_LAMBDA_DIR in sys.path:
        sys.path.remove(_TEST_CODE_LAMBDA_DIR)

#: The value both stacks must deploy — the pin, named locally so a future move is
#: one deliberate edit in one place, but spelled by `glue_session_runner`.
#:
#: `sample` is p44's whole point: the Glue verification measures the transform
#: against a bounded 100-row sample of the run's OWN source object instead of 100
#: rows generated from the declared schema. Moving this back to
#: FRAME_MODE_SYNTHETIC is a decision, not a tidy-up, and has to come through
#: this line and both stacks together.
DEPLOYED_VALUE = FRAME_MODE_SAMPLE

#: The two recognised modes (`glue_session_runner.py:205`, `:210`). Anything else
#: reaches `resolve_frame_source`'s unrecognised arm, which builds a synthetic
#: frame and logs a WARNING — so an out-of-pair value deploys as a silent revert.
ALLOWED_VALUES = frozenset({FRAME_MODE_SYNTHETIC, FRAME_MODE_SAMPLE})


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

    Scoped deliberately, as in both sibling files: the variable must be on the
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


def _assigned_value(env_block: str) -> str | None:
    """The literal assigned to VERIFICATION_FRAME_MODE in an env block, or None.

    Ignores commented-out lines so that a variable which survives only inside a
    `//` comment does not read as present — and both stacks carry a long comment
    block above this key that names the variable repeatedly, so this matters more
    here than in the sibling files.
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
            "This is the inline break-glass path (used when USE_AGENTCORE_RUNTIME is flipped "
            "off); unset, the runner falls back to a synthetic frame."
        )

    def test_deployed_value_in_lambda_env(self, compute_stack_content):
        value = _assigned_value(_code_testing_env_block(compute_stack_content))
        assert value == DEPLOYED_VALUE, (
            f"{ENV_VAR} on the CodeTesting Lambda must be '{DEPLOYED_VALUE}', got {value!r}. "
            "This is the break-glass copy; it must track the container copy. If you are moving "
            f"back to '{FRAME_MODE_SYNTHETIC}', change both stacks and DEPLOYED_VALUE here "
            "together — that is a decision about what verification measures, not a cleanup."
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
            f"{ENV_VAR} missing from agentContainerEnv in agentcore-stack.ts. This is the copy "
            "the LIVE path reads (USE_AGENTCORE_RUNTIME: 'true' dispatches verification into the "
            "container). Unlike VERIFICATION_EXECUTOR, there is no code-level rescue: unset "
            "resolves to a synthetic frame, so every live run silently goes back to measuring "
            "the transform against rows generated from the declared schema."
        )

    def test_deployed_value_in_container_env(self, agentcore_stack_content):
        """This is the copy that MATTERS.

        ``USE_AGENTCORE_RUNTIME: 'true'`` means the runner executes in the
        container, so this side is what actually makes the pipeline's own data
        the frame under test. Setting only the sibling above would record an
        intent that never took effect.
        """
        value = _assigned_value(_agent_container_env_block(agentcore_stack_content))
        assert value == DEPLOYED_VALUE, (
            f"{ENV_VAR} in agentContainerEnv must be '{DEPLOYED_VALUE}', got {value!r}. This is "
            f"the LIVE path's copy. If you are moving back to '{FRAME_MODE_SYNTHETIC}', change "
            "both stacks and DEPLOYED_VALUE here together."
        )


# ── Both sides together — the assertion the task is actually about ──


class TestBothSidesPresent:
    """One-sided publication is the failure mode; these are the two-sided check."""

    def test_variable_set_in_both_environments(self, compute_stack_content, agentcore_stack_content):
        """Fails if EITHER side is missing, naming which one.

        A variable set on the Lambda alone changes nothing on the live path; set
        in the container alone it is missing from the documented break-glass
        path. `LGR-87b8538-1` rejected the one-stack form for exactly this.
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
            "One-sided publication produces a flag that is inert on the live path while every "
            "other test still passes (design §4.1's MECHANISM note, R1.7)."
        )

    def test_both_sides_agree_on_the_value(self, compute_stack_content, agentcore_stack_content):
        """The two copies must not drift apart.

        Both present but disagreeing is the subtler version of the same bug: the
        frame a run was measured against would depend on which path served it,
        and the report's per-run frame claim would be the only place the
        difference showed.

        This assertion needs no external reference, which is why it exists
        alongside the exact-value pins above: if someone edits one stack and
        `DEPLOYED_VALUE` together and misses the other stack, the pin still
        matches the stack they did edit. `LGR-87b8538-1` asks for this test by
        name — "with a separate test that the two stacks agree with each other".
        """
        lambda_value = _assigned_value(_code_testing_env_block(compute_stack_content))
        container_value = _assigned_value(_agent_container_env_block(agentcore_stack_content))

        assert lambda_value == container_value, (
            f"{ENV_VAR} disagrees between environments: "
            f"Lambda={lambda_value!r}, container={container_value!r}. "
            "The frame a run was verified against would then depend on which path served it."
        )


# ── The near-miss hazard, which is specific to this variable ────────


class TestValueIsInTheAllowedPair:
    """Whatever is deployed must be a real vocabulary member, not a near-miss."""

    def test_value_is_in_the_allowed_pair(self, compute_stack_content, agentcore_stack_content):
        """Both copies hold one of {synthetic, sample} (R1.4, `LGR-ad78946-1`).

        Kept separate from the exact-value pins because it survives an
        intentional move between the two modes and fails only on a value that is
        neither. The hazard it guards is stated in both stack comments: a
        near-miss is NOT normalised, so `'Sample'` or `'sample '` takes
        `resolve_frame_source`'s unrecognised arm, builds a synthetic frame and
        logs a WARNING naming the value. Deployed, that reverts p44 while
        reporting nothing — the only symptom is one log line nobody is watching.

        The pair is imported from `glue_session_runner`, so this compares the
        deployed literal against the exact strings the resolver will compare it
        to rather than against a second opinion about what they are.
        """
        for label, value in (
            ("compute-stack.ts", _assigned_value(_code_testing_env_block(compute_stack_content))),
            ("agentcore-stack.ts", _assigned_value(_agent_container_env_block(agentcore_stack_content))),
        ):
            assert value in ALLOWED_VALUES, (
                f"{ENV_VAR} in {label} is {value!r}, which is outside {sorted(ALLOWED_VALUES)}. "
                f"An out-of-pair value resolves to '{FRAME_MODE_SYNTHETIC}' at runtime with only "
                "a WARNING, so deploying it silently undoes the change this variable exists to "
                "make."
            )
