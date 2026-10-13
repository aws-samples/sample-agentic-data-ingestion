# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E: the route the `test_failure` gate names is the route that regenerates.

This is the deployed-environment half of P32 C9. The dashboard consequence hint
(`dashboard-react/src/components/ApprovalWorkflowSection.tsx`) now tells an
approver three things, and 2.4b requires all three to be TRUE of the deployed
system rather than of the source tree:

  1. Reject fails the run.
  2. Rejecting is a *precondition* of the remediation route — the retry endpoint
     answers 409 while the run is not FAILED
     (`dashboard_api/handlers/pipelines_runs.py:1204-1213`), and the
     `Retry from Step` control only renders on a FAILED run
     (`run-detail/RunDetailHeader.tsx:85-114`).
  3. Following the route named by `details.remediation_hint` generates the code
     again.

And 2.17's own deployed obligation: a failing run makes its automatic
regeneration attempts BEFORE any human sees this gate, so a deployed run should
show **two CreateCode attempts and one gate**, not one attempt and one gate.

Why the tests below are conditional rather than driving the failure themselves.
A `test_failure` gate exists only when `test_code` reports a failing test
(`orchestration-stack.ts` TestResultChoice → Default arm), which depends on what
Bedrock generates for the pipeline under test. There is no deployed lever that
forces a failing test without also faking the thing being verified, so these
tests observe a run that reached the gate and **skip explicitly** when none did.
A skip here says "not observed"; it must never be read as "verified".

Nothing in this file is mutating unless it reaches the reject/retry steps, which
require `AGENTIC_DATA_INGESTION_E2E_TRIGGER=true` via `requires_trigger`. The
whole layer is skipped unless `AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true`
(`tests/e2e/conftest.py::require_real_aws`), and `-m 'not e2e'` is the default in
`pyproject.toml`, so `make test-fast` and `make test-ci` never reach it.

Validates: Requirements 2.4b, 2.5, 2.6, 2.17
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest
from tests.e2e.conftest import poll_execution_by_input

logger = logging.getLogger(__name__)

#: The route the payload carries (`orchestration-stack.ts:585`, defaulted at
#: `approval_handler/lambda_function.py:276`) and the route the consequence hint
#: names. Asserted, not assumed: if the producer starts emitting a different
#: hint, the sentence on screen stops matching the payload and this fails.
EXPECTED_HINT = "retry-from-step:create_code"

#: The SFN state id the hint's step resolves to, and therefore the label the
#: `Retry from Step` selector shows ("Create Code").
EXPECTED_RETRY_STATE = "CreateCode"

#: How long to wait for a `test_failure` gate to appear on any pipeline before
#: recording the observation as not-made.
GATE_DISCOVERY_TIMEOUT = 60

# ── Which executions 2.17 can honestly be asserted against ────────────
#
# 2.17 is a claim about the definition D2 deploys. An execution that ran under
# the PREVIOUS definition cannot exhibit it, so asserting it there reports
# "unobserved" as "broken" — the mirror image of the dishonesty this file
# exists to prevent (module docstring).
#
# `CheckRegenerationAttempt` is the self-selecting marker. It is a Choice state
# introduced by D2 (`orchestration-stack.ts:504`; `git show
# HEAD:...orchestration-stack.ts` contains the name zero times), and every
# entry into `CreateCode` goes through it — the stack comment at :489-497 states
# that, and the loop-back edge is only reachable after a first pass through it.
# So a run that reached the gate under the post-D2 definition MUST have entered
# it, and a run under any earlier definition CANNOT have: the state did not
# exist. No clock is involved, so the filter cannot go stale on a redeploy.
#
# Rejected alternatives:
#
# * **A literal cutoff timestamp.** Correct for exactly one deployment. The
#   D2 edge went out 2026-08-05T12:04Z; the next deploy makes any constant
#   written here silently wrong, in the direction that admits pre-fix runs.
# * **`describe_state_machine()["creationDate"]`.** That is when the state
#   machine was created, not when its definition last changed. CDK replaces the
#   `definitionBody` of one long-lived `sfn.StateMachine`
#   (`orchestration-stack.ts:909-918`), so `creationDate` is the first-ever
#   deploy of the stack and admits every execution in the account. The
#   `describe_state_machine` response carries no "definition last updated"
#   field to use instead.
# * **`list_state_machine_versions` / `describe_execution()`'s
#   `stateMachineVersionArn`.** Both require a PUBLISHED state machine. The
#   deployed machine is a plain unversioned `sfn.StateMachine`
#   (`orchestration-stack.ts:909`, no version or alias construct), so the
#   version list is empty and executions carry no `stateMachineVersionArn` —
#   there is nothing to filter on.
# * **The previous "newest page only" filter.** "Recent" is not "post-deploy".
#   The one execution in the validation account that ever reached
#   `ForceTestReviewApproval` started 2026-08-04T12:28Z — a day BEFORE the
#   edge deployed — and it is on the newest page, so paging handed the test
#   precisely the subject that cannot satisfy the claim.
POST_D2_MARKER_STATE = "CheckRegenerationAttempt"

#: The gate whose single entry 2.17/3.21 is about.
FORCED_REVIEW_STATE = "ForceTestReviewApproval"

#: Cost bound on the history reads below — `get_execution_history` is one API
#: call per page per execution. This is a BUDGET, not a correctness filter:
#: correctness comes from :data:`POST_D2_MARKER_STATE`. The skip message
#: reports how many executions were actually examined so a skip cannot be
#: mistaken for "the whole account was checked".
MAX_EXECUTIONS_INSPECTED = 250


def _pending_test_failure_approval(boto3_session: Any) -> dict[str, Any] | None:
    """The most recent pending `test_failure` approval, or None.

    Read-only scan of the approvals table. `test_failure` gates are rare by
    design, so this looks across pipelines rather than provisioning one.
    """
    table = boto3_session.resource("dynamodb").Table("agentic_data_ingestion_approvals")
    deadline = time.time() + GATE_DISCOVERY_TIMEOUT
    while time.time() < deadline:
        items: list[dict[str, Any]] = []
        scan_kwargs: dict[str, Any] = {}
        while True:
            page = table.scan(**scan_kwargs)
            items.extend(page.get("Items", []))
            if not page.get("LastEvaluatedKey"):
                break
            scan_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

        pending = [
            item
            for item in items
            if item.get("approval_type") == "test_failure" and str(item.get("status", "")).lower() == "pending"
        ]
        if pending:
            pending.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
            return pending[0]
        time.sleep(5)
    return None


def _details(approval: dict[str, Any]) -> dict[str, Any]:
    """`approval.details` as the dashboard receives it.

    The handler stores the payload as a JSON string under `approval_data`
    (`approval_handler/lambda_function.py:373`) and the run-detail handler parses
    it back into `details` (`pipelines_runs.py:626-633`). The dashboard reads the
    parsed object, so the parse is part of what is being verified.
    """
    raw = approval.get("approval_data")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw if isinstance(raw, dict) else {}


@pytest.mark.e2e
@pytest.mark.slow
class TestGateNamesAWorkingRoute:
    """2.4b/2.5/2.6 — the sentence on screen is true of the deployed system."""

    def test_gate_payload_carries_the_route_the_screen_names(self, boto3_session):
        """Read-only: the hint the dashboard reads is present and is the route.

        This is the half that does not need to mutate anything, so it runs
        without `requires_trigger`. It is the guard against the two sides
        drifting: the consequence hint derives its sentence from
        `details.remediation_hint`, so a producer that stops emitting the key,
        or emits a route the screen cannot map to a control, changes what the
        approver is told.
        """
        approval = _pending_test_failure_approval(boto3_session)
        if approval is None:
            pytest.skip(
                "No pending test_failure gate in the deployed environment — the route claim was NOT "
                "observed. A test_failure gate needs a run whose generated code fails testing, which "
                "cannot be forced without faking the verdict being checked."
            )

        details = _details(approval)
        assert "remediation_hint" in details, (
            f"approval {approval.get('approval_id')} carries {sorted(details)} — the dashboard hint "
            f"derives its route sentence from details.remediation_hint (2.5), so an absent key means "
            f"the screen falls back to naming the route generically"
        )
        assert details["remediation_hint"] == EXPECTED_HINT, (
            f"the gate names {details['remediation_hint']!r}; the consequence hint's recognised form is "
            f"retry-from-step:<step> and its worked case is {EXPECTED_HINT!r}"
        )

    def test_named_route_is_a_step_the_retry_selector_offers(self, boto3_session):
        """The hint's step is a real SFN state, so "select Create Code" is findable.

        `getRetryStepOptions` (`dashboard-react/src/constants.ts`) builds the
        selector from `PIPELINE_STEP_ORDER`, whose ids must match the deployed
        state machine. Naming a step the deployed machine does not have would be
        an instruction an approver cannot follow.
        """
        approval = _pending_test_failure_approval(boto3_session)
        if approval is None:
            pytest.skip("No pending test_failure gate — the route's step was NOT observed.")

        hint = _details(approval).get("remediation_hint", "")
        assert hint.startswith("retry-from-step:"), f"unrecognised route form: {hint!r}"

        definition = json.loads(
            boto3_session.client("stepfunctions").describe_state_machine(
                stateMachineArn=(
                    f"arn:aws:states:{boto3_session.region_name}:"
                    f"{boto3_session.client('sts').get_caller_identity()['Account']}:"
                    f"stateMachine:agentic-data-ingestion-pipeline"
                )
            )["definition"]
        )
        assert EXPECTED_RETRY_STATE in definition["States"], (
            f"{EXPECTED_RETRY_STATE} is not a state in the deployed definition, so the hint names a "
            f"step the retry selector cannot offer"
        )

    def test_reject_then_the_named_route_regenerates_the_code(
        self,
        requires_trigger,
        boto3_session,
        rest_client,
        approval_driver,
        cost_budget,
        resource_names,
        e2e_ledger,
    ):
        """The full sentence, executed: reject → retry from the named step → new code.

        Deliberately asserts only what the hint claims. It does NOT assert that
        the retry resumes at `CreateCode`: today a retry re-enters
        `FileValidation` and the pre-seeded cached results are overwritten
        (design.md "V1 live finding"), and task 17 is building the resume. The
        hint says the code is generated again, which is what is checked here.
        """
        approval = _pending_test_failure_approval(boto3_session)
        if approval is None:
            pytest.skip("No pending test_failure gate — reject-then-retry was NOT executed against a deployed run.")

        approval_id = str(approval["approval_id"])
        pipeline_id = str(approval.get("pipeline_id") or approval.get("pipeline_key") or "")
        run_id = str(approval.get("run_id") or "")
        assert pipeline_id and run_id, f"approval {approval_id} carries no pipeline_id/run_id to retry"

        hint = _details(approval).get("remediation_hint", EXPECTED_HINT)
        retry_step = hint.split(":", 1)[1] if ":" in hint else EXPECTED_RETRY_STATE

        code_before = _latest_code_version(boto3_session, run_id)

        # ── 1. Reject: the run fails, which is the precondition of the route ──
        result = approval_driver.reject(approval_id, reason="p32 e2e — verify the named route")
        assert result.status == "rejected", f"reject returned {result.status!r}"

        status, execution_arn = poll_execution_by_input(
            boto3_session.client("stepfunctions"),
            resource_names["state_machine_arn"],
            pipeline_id=pipeline_id,
            started_after=0,
            timeout=cost_budget,
        )
        assert status in ("FAILED", "ABORTED"), (
            f"rejecting the gate left the execution {status} — the hint says Reject fails the run "
            f"(execution_arn={execution_arn})"
        )

        # ── 2. Follow the route the payload named ─────────────────────
        response = rest_client.post(f"/runs/{run_id}/retry", {"retry_from_step": retry_step})
        assert response.status_code == 200, (
            f"the route the gate names returned {response.status_code}: {response.body!r}. The hint tells "
            f"an approver to reject first precisely so this call is legal (409 while not FAILED)."
        )
        new_run_id = json.loads(response.body)["run_id"]
        logger.info("Retry from %s started run %s", retry_step, new_run_id)

        # ── 3. The code is generated again ────────────────────────────
        deadline = time.time() + cost_budget
        code_after = None
        while time.time() < deadline:
            code_after = _latest_code_version(boto3_session, new_run_id)
            if code_after and code_after != code_before:
                break
            time.sleep(15)

        assert code_after, (
            f"run {new_run_id} produced no create_code state row — the route did not regenerate the code (2.6)"
        )
        assert code_after != code_before, (
            f"run {new_run_id} carries the same code_version_id as {run_id} ({code_after}) — the route "
            f"did not generate the code again"
        )
        e2e_ledger.add_execution(new_run_id)


@pytest.mark.e2e
@pytest.mark.slow
class TestTwoAttemptsOneGate:
    """2.17 — the automatic attempts happen before the human sees the gate."""

    def test_deployed_failing_run_shows_two_attempts_and_one_gate(self, boto3_session):
        """`CreateCode` entered twice, `ForceTestReviewApproval` entered once.

        The pre-P32 behaviour was one attempt and one gate: the counter was
        pinned at 0, so `regeneration_needed` was True on every failing run and
        the only consumer of that verdict was the approval gate (bugfix.md 1.16).
        With D2's loop-back edge the run should regenerate up to
        `DEFAULT_REGENERATION_MAX_ATTEMPTS` first and reach the gate once.

        The subject set is restricted to executions that ran under a definition
        carrying D2's loop-back edge, identified by
        :data:`POST_D2_MARKER_STATE` appearing in the execution's own history.
        See that constant for why the alternatives (a literal cutoff,
        `creationDate`, state-machine versions, "newest page only") were all
        rejected. The two assertions are unchanged; only the set of runs they
        are applied to is.
        """
        sfn = boto3_session.client("stepfunctions")
        account = boto3_session.client("sts").get_caller_identity()["Account"]
        state_machine_arn = (
            f"arn:aws:states:{boto3_session.region_name}:{account}:stateMachine:agentic-data-ingestion-pipeline"
        )

        examined, post_d2, gated = post_d2_gate_candidates(sfn, state_machine_arn)

        for execution_arn, entered in gated:
            assert entered.count(FORCED_REVIEW_STATE) == 1, (
                f"{execution_arn} reached the forced review gate "
                f"{entered.count(FORCED_REVIEW_STATE)} times — the gate is a single human "
                f"decision per run (3.21)"
            )
            assert entered.count("CreateCode") >= 2, (
                f"{execution_arn} entered CreateCode "
                f"{entered.count('CreateCode')} time(s) before the gate. 2.17 requires the "
                f"automatic attempts to happen first; one attempt and one gate is the pre-P32 "
                f"behaviour in which the counter never advanced (bugfix.md 1.16)."
            )

        if not gated:
            pytest.skip(
                f"Two-attempts-and-one-gate was NOT observed. Examined {len(examined)} retained "
                f"execution(s) (cap {MAX_EXECUTIONS_INSPECTED}); {len(post_d2)} ran under a definition "
                f"containing {POST_D2_MARKER_STATE}, and none of those reached {FORCED_REVIEW_STATE}. "
                f"Stated plainly: this test can ONLY skip until a post-deploy run's generated code "
                f"fails testing, and there is no deployed lever that forces that without faking the "
                f"verdict being verified — so a skip here is the expected steady state, not a pass."
            )


def post_d2_gate_candidates(
    sfn_client: Any,
    state_machine_arn: str,
    max_executions: int = MAX_EXECUTIONS_INSPECTED,
) -> tuple[list[str], list[str], list[tuple[str, list[str]]]]:
    """Partition retained executions into the ones 2.17 can be asserted against.

    Module-level and client-injected on purpose: this is the part of the test
    that got the SUBJECT wrong, so it is the part that needs to be exercisable
    without a deployed account. `tests/unit/test_e2e_post_d2_execution_filter.py`
    drives it with the live counterexample (a gated execution that predates the
    edge) and would have failed on the "newest page only" version.

    Returns:
        A ``(examined, post_d2, gated)`` triple. ``examined`` is every execution
        arn read, bounded by *max_executions*. ``post_d2`` is the subset whose
        history contains :data:`POST_D2_MARKER_STATE`, i.e. that ran under a
        definition carrying D2's loop-back edge. ``gated`` pairs each post-D2
        execution that reached :data:`FORCED_REVIEW_STATE` with its full list of
        entered state names, so the caller can count entries.
    """
    examined: list[str] = []
    for page in sfn_client.get_paginator("list_executions").paginate(
        stateMachineArn=state_machine_arn,
        PaginationConfig={"MaxItems": max_executions, "PageSize": 100},
    ):
        examined.extend(execution["executionArn"] for execution in page["executions"])

    post_d2: list[str] = []
    gated: list[tuple[str, list[str]]] = []
    for execution_arn in examined:
        entered = _entered_states(sfn_client, execution_arn)
        if POST_D2_MARKER_STATE not in entered:
            # Ran under a definition without D2's edge. 2.17 was not deployed
            # when this execution ran, so it is evidence of nothing either way.
            continue
        post_d2.append(execution_arn)
        if FORCED_REVIEW_STATE in entered:
            gated.append((execution_arn, entered))
    return examined, post_d2, gated


def _entered_states(sfn_client: Any, execution_arn: str) -> list[str]:
    """Every state name *execution_arn* entered, in order.

    The history is fully paginated. A single `get_execution_history` page caps
    at 1000 events, and a run that regenerates its code emits well over that —
    truncating it would undercount `CreateCode` entries and manufacture exactly
    the false failure this test was fixed to stop producing.
    """
    names: list[str] = []
    for page in sfn_client.get_paginator("get_execution_history").paginate(executionArn=execution_arn, maxResults=1000):
        for event in page["events"]:
            if event["type"].endswith("StateEntered"):
                names.append(event["stateEnteredEventDetails"]["name"])
    return names


def _latest_code_version(boto3_session: Any, run_id: str) -> str | None:
    """The `code_version_id` recorded by `create_code` for a run, if any.

    The state table is `agentic_data_ingestion_state` — the name `data-stack.ts:46`
    creates and every Lambda receives as `STATE_TABLE`
    (`agentcore-stack.ts:115`, `shared/retry_utils.py:_state_table`), and the
    name `tests/e2e/conftest.py` uses at `:83` (`resource_names["state_table"]`)
    and `:319`. This helper previously queried `agentic_data_ingestion_pipeline_state`,
    a table that exists nowhere in the repository or the deployment; the error
    never surfaced because its only caller sits behind a skip.
    """
    table = boto3_session.resource("dynamodb").Table("agentic_data_ingestion_state")
    from boto3.dynamodb.conditions import Key

    items = table.query(KeyConditionExpression=Key("run_id").eq(run_id)).get("Items", [])
    for item in items:
        if str(item.get("step_name", "")).lower() in ("create_code", "createcode"):
            output = item.get("output_data") or {}
            value = output.get("code_version_id") if isinstance(output, dict) else None
            if value:
                return str(value)
    return None
