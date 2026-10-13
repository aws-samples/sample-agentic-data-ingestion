# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E test: Approval gate TimeoutSeconds config check.

Asserts that the ``WaitForSchemaMappingApproval`` state in the deployed
state machine carries the approval window, ``TimeoutSeconds=259200``
(72 hours). The subject this test was written to prove is unchanged: the
approval timeout is out-of-scope for live execution, so it is verified by
reading the deployed configuration and **never** by waiting for it to fire.

This test is read-only (no ``requires_trigger``).

AMENDED DELIBERATELY — p34 R6.1/R6.2/R6.6, not a regression.
    The expected value was ``604800`` (7 days) and the test was named
    ``test_schema_mapping_gate_timeout_is_604800``. p34 R6 shortens the
    approval window to 72 hours and expresses it once, as the exported
    ``APPROVAL_TIMEOUT_SECONDS`` constant in
    ``deploy-cdk/lib/orchestration-stack.ts``, applied to all three
    ``waitForTaskToken`` approval gates (``WaitForSchemaMappingApproval``,
    ``ForceTestReviewApproval``, ``WaitForCodeApproval``). Amending rather
    than deleting follows ``LGR-5906ec6-2`` and ``LGR-60c4985-6`` (both
    template / ratified): this repo encodes a deliberate position as a test
    assertion, so reversing the position means changing the test on purpose
    and saying so where the assertion lives.

    Why the window mattered enough to shorten: ``LGR-60c4985-3`` (template /
    ratified) excludes ``waitForTaskToken`` steps from retry-resume caching,
    which makes ``TimeoutSeconds`` the worst-case stall for these states
    rather than a theoretical backstop. R6 shortens the stall; it does not
    remove the exclusion.

    The test name changed too, because the old name stated the number it was
    asserting. The number now lives in one place (the CDK constant), so the
    name states the subject instead.

    This assertion reads the **deployed** definition, so it fails until the
    Orchestration stack carrying the new constant is deployed. It is
    ``@pytest.mark.e2e`` — excluded from ``make test-fast`` / ``make
    test-ci`` — and requires real AWS credentials to run at all.

Requirements: 14.1, 14.2 (p28); 6.1, 6.2 (p34)
"""

from __future__ import annotations

import json
import logging

import pytest

logger = logging.getLogger(__name__)

# The deployed state machine's schema-mapping wait state
WAIT_STATE_NAME = "WaitForSchemaMappingApproval"

# p34 R6.6: the single source of truth is APPROVAL_TIMEOUT_SECONDS in
# deploy-cdk/lib/orchestration-stack.ts. This test reads the DEPLOYED
# definition, so it cannot import that constant — the value is mirrored here
# and the two are kept in step by the CDK suite, which asserts the constant
# itself at all three approval gates
# (deploy-cdk/test/orchestration-stack.test.ts, "approval window" block).
EXPECTED_TIMEOUT_SECONDS = 259200  # 72 hours


@pytest.mark.e2e
class TestApprovalTimeoutConfig:
    """Config-check: the approval gate timeout is the 72h approval window."""

    def test_schema_mapping_gate_timeout_is_the_approval_window(
        self,
        boto3_session,
        resource_names,
    ):
        """Assert WaitForSchemaMappingApproval carries the approval window.

        Req 14.1: The approval timeout is documented as out-of-scope for live
                  execution.
        Req 14.2: Where timeout behavior is exercised, the test simulates
                  rather than waiting — here we simply assert the config value.
                  Nothing in this test waits for a timeout to fire, and
                  nothing in it may be changed to.

        p34 R6.1/R6.2: the window is at most 259200s and is the same value on
        all three approval gates. This test covers the schema-mapping gate
        against the deployed definition; the sibling gates are covered
        alongside it in the CDK suite, against the synthesized definition.

        AMENDED per p34 R6 — see the module docstring. The expected value was
        604800 (7 days) and the test was named
        ``test_schema_mapping_gate_timeout_is_604800``. The TC-9.4 "12-hour
        timeout" from the original test plan referred to a *test budget*
        consideration, never to the deployed gate timeout; that distinction is
        unchanged and is why this test reads configuration rather than waiting.
        """
        sfn_client = boto3_session.client("stepfunctions")
        state_machine_arn = resource_names["state_machine_arn"]

        # Retrieve the state machine definition
        response = sfn_client.describe_state_machine(stateMachineArn=state_machine_arn)
        definition = json.loads(response["definition"])

        # Navigate the ASL to find the WaitForSchemaMappingApproval state.
        # The state may be nested within a Parallel/Map state or at the top
        # level — we search recursively.
        wait_state = _find_state(definition, WAIT_STATE_NAME)

        assert wait_state is not None, (
            f"State '{WAIT_STATE_NAME}' not found in state machine definition. "
            f"Available top-level states: {list(definition.get('States', {}).keys())}"
        )

        # The waitForTaskToken pattern uses TimeoutSeconds (or
        # TimeoutSecondsPath) on the Task state.
        timeout = wait_state.get("TimeoutSeconds") or wait_state.get("HeartbeatSeconds")
        if timeout is None:
            # Check TimeoutSecondsPath (dynamic, less common)
            timeout_path = wait_state.get("TimeoutSecondsPath")
            pytest.skip(
                f"State '{WAIT_STATE_NAME}' uses TimeoutSecondsPath={timeout_path} "
                f"(dynamic timeout) — cannot assert a static value."
            )

        assert timeout == EXPECTED_TIMEOUT_SECONDS, (
            f"Expected {WAIT_STATE_NAME}.TimeoutSeconds={EXPECTED_TIMEOUT_SECONDS} "
            f"(72 hours, p34 R6.1), but found {timeout}. A value of 604800 means "
            f"the Orchestration stack carrying APPROVAL_TIMEOUT_SECONDS has not "
            f"been deployed yet."
        )
        logger.info(
            "Approval gate timeout config verified: %s.TimeoutSeconds=%d",
            WAIT_STATE_NAME,
            timeout,
        )


def _find_state(definition: dict, state_name: str) -> dict | None:
    """Recursively search ASL definition for a state by name.

    Handles top-level states, parallel branches, and map iterator states.
    """
    states = definition.get("States", {})

    # Direct match at this level
    if state_name in states:
        return states[state_name]

    # Recurse into Parallel branches and Map iterators
    for _name, state in states.items():
        state_type = state.get("Type", "")

        if state_type == "Parallel":
            for branch in state.get("Branches", []):
                found = _find_state(branch, state_name)
                if found:
                    return found

        elif state_type == "Map":
            iterator = state.get("Iterator") or state.get("ItemProcessor", {})
            if iterator:
                found = _find_state(iterator, state_name)
                if found:
                    return found

    return None
