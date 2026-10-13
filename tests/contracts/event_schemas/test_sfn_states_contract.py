# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: Synthesized state machine contains all required states and transitions.

Validates the type-safe CDK Step Functions constructs produce a state machine
that is functionally equivalent to the original JSON string definition.

Validates: AC-8.1, AC-8.2, AC-8.3, AC-8.4, AC-8.5, AC-8.6
"""

import os
import re

import pytest

ORCHESTRATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "orchestration-stack.ts"
)


def _read_source() -> str:
    with open(ORCHESTRATION_STACK_PATH) as f:
        return f.read()


# ── All 16 expected state names ──────────────────────────────────
EXPECTED_STATES = [
    "FileValidation",
    "SchemaExtract",
    "SchemaCompare",
    "CheckSchemaAction",
    "ReuseExistingMapping",
    "SchemaMapping",
    "CheckSchemaMappingApproval",
    "WaitForSchemaMappingApproval",
    "CreateCode",
    "TestCode",
    "TestResultChoice",
    "ForceTestReviewApproval",
    "CheckCodeApproval",
    "WaitForCodeApproval",
    "CreateGluePipeline",
    "CreateInfrastructure",
    "CreateQualityChecks",
    "CheckJobTimeoutConfig",
    "ApplyDefaultJobTimeout",
    # p32 D2 — the in-execution regeneration loop
    "CheckRegenerationAttempt",
    "ApplyDefaultRegenerationAttempt",
    "ComputeNextRegenerationAttempt",
    "IncrementRegenerationAttempt",
    "RunPipeline",
    "UpdateSuccessState",
    "RecordFailure",
]

# Lambda invoke states that should use tasks.LambdaInvoke.
# The four container-backed steps (CreateCode, TestCode, CreateGluePipeline,
# CreateInfrastructure) are NOT here anymore: P28 R19.11 converts them to
# waitForTaskToken CustomStates (see CONTAINER_TOKEN_STATES below).
LAMBDA_INVOKE_STATES = [
    "FileValidation",
    "SchemaExtract",
    "SchemaCompare",
    "SchemaMapping",
    "CreateQualityChecks",
]

# Container-backed steps built via the containerBackedTask helper
# (P28 R19.11): waitForTaskToken CustomStates so the proxy Lambda does not
# idle-bill for the container's full duration. Maps state name → the exact
# ResultPath the old payloadResponseOnly LambdaInvoke used (downstream states
# keep reading the same keys, e.g. TestResultChoice reads
# $.test_code_result.test_result.status).
CONTAINER_TOKEN_STATES = {
    "CreateCode": "$.create_code_result",
    "TestCode": "$.test_code_result",
    "CreateGluePipeline": "$.create_glue_pipeline_result",
    "CreateInfrastructure": "$.create_infrastructure_result",
}

# Choice states
CHOICE_STATES = [
    "CheckSchemaAction",
    "CheckSchemaMappingApproval",
    "TestResultChoice",
    "CheckCodeApproval",
    "CheckJobTimeoutConfig",
    "CheckRegenerationAttempt",
]

# waitForTaskToken states (3 approval gates + the Glue completion callback)
WAIT_FOR_TOKEN_STATES = [
    "WaitForSchemaMappingApproval",
    "WaitForCodeApproval",
    "ForceTestReviewApproval",
    "RunPipeline",
]


class TestNoRawJsonDefinition:
    """AC-8.6: The raw JSON string definition is deleted."""

    def test_no_json_stringify_state_machine(self):
        """No JSON.stringify({ ... StartAt: 'FileValidation' ... }) pattern."""
        source = _read_source()
        assert "JSON.stringify" not in source, "Raw JSON.stringify state machine definition should be removed"

    def test_no_definition_from_string(self):
        """No DefinitionBody.fromString() usage — should use fromChainable()."""
        source = _read_source()
        assert "fromString" not in source, "DefinitionBody.fromString() should be replaced with fromChainable()"

    def test_uses_from_chainable(self):
        """State machine uses DefinitionBody.fromChainable()."""
        source = _read_source()
        assert "fromChainable" in source


class TestAllStatesPresent:
    """AC-8.5: All required states exist in the CDK construct definitions."""

    @pytest.mark.parametrize("state_name", EXPECTED_STATES)
    def test_state_defined(self, state_name: str):
        """Each expected state is defined as a CDK construct."""
        source = _read_source()
        # CDK constructs use: new <Type>(this, 'StateName', ...). The four
        # container-backed steps go through the containerBackedTask helper,
        # which receives the state name as its first argument (P28 R19.11).
        pattern = rf"(this,\s*'{state_name}')|(containerBackedTask\(\s*'{state_name}')"
        assert re.search(pattern, source), f"State '{state_name}' must be defined as a CDK construct"


class TestLambdaInvokeStates:
    """AC-8.2: Lambda invocation steps use tasks.LambdaInvoke."""

    @pytest.mark.parametrize("state_name", LAMBDA_INVOKE_STATES)
    def test_uses_lambda_invoke(self, state_name: str):
        """Each Lambda step uses tasks.LambdaInvoke construct."""
        source = _read_source()
        pattern = rf"new tasks\.LambdaInvoke\(this,\s*'{state_name}'"
        assert re.search(pattern, source), f"State '{state_name}' must use tasks.LambdaInvoke"

    @pytest.mark.parametrize("state_name", LAMBDA_INVOKE_STATES)
    def test_has_result_path(self, state_name: str):
        """Each Lambda step has a resultPath configuration."""
        source = _read_source()
        # Find the construct block and check for resultPath
        idx = source.find(f"'{state_name}'")
        assert idx != -1
        block = source[idx : idx + 600]
        assert "resultPath" in block, f"State '{state_name}' must have a resultPath"

    @pytest.mark.parametrize("state_name", LAMBDA_INVOKE_STATES)
    def test_has_retry(self, state_name: str):
        """Each Lambda step has retry configuration."""
        source = _read_source()
        # After the construct, there should be a .addRetry() call
        pattern = rf"{state_name.lower().replace('', '')}.*\.addRetry"
        # Simpler: just check that addRetry is called on the variable
        # Variable names are camelCase versions of state names
        var_name = state_name[0].lower() + state_name[1:]
        assert f"{var_name}.addRetry" in source, f"State '{state_name}' must have .addRetry() configured"

    @pytest.mark.parametrize("state_name", LAMBDA_INVOKE_STATES)
    def test_has_catch(self, state_name: str):
        """Each Lambda step catches errors to UpdateFailureState."""
        source = _read_source()
        var_name = state_name[0].lower() + state_name[1:]
        assert f"{var_name}.addCatch" in source, f"State '{state_name}' must have .addCatch() configured"


class TestContainerTokenStates:
    """P28 R19.11: container-backed steps are waitForTaskToken CustomStates.

    The proxy Lambda dispatches the token in the container payload and
    returns immediately; the container closes the loop via
    SendTaskSuccess/SendTaskFailure (shared/sfn_task_token.py). These pins
    freeze the pieces that keep the conversion behavior-preserving.

    Validates: P28 Requirements 19.11
    """

    @pytest.mark.parametrize("state_name", sorted(CONTAINER_TOKEN_STATES))
    def test_built_via_container_backed_task_helper(self, state_name: str):
        """Each container step is built via the containerBackedTask helper."""
        source = _read_source()
        pattern = rf"containerBackedTask\(\s*'{state_name}'"
        assert re.search(pattern, source), f"State '{state_name}' must be built via containerBackedTask()"

    def test_helper_is_wait_for_task_token_custom_state(self):
        """The helper produces a CustomState with the waitForTaskToken resource."""
        source = _read_source()
        idx = source.find("const containerBackedTask = (")
        assert idx != -1, "containerBackedTask helper must exist"
        block = source[idx : idx + 900]
        assert "new sfn.CustomState(this, id" in block
        assert "arn:aws:states:::lambda:invoke.waitForTaskToken" in block

    def test_helper_passes_task_token_in_payload(self):
        """The token travels IN the dispatch payload ($$.Task.Token)."""
        source = _read_source()
        idx = source.find("const containerBackedTask = (")
        block = source[idx : idx + 900]
        assert "'task_token.$': '$$.Task.Token'" in block, (
            "containerBackedTask must pass the task token in the Lambda payload"
        )

    def test_helper_has_explicit_timeout(self):
        """waitForTaskToken states MUST carry an explicit TimeoutSeconds (the
        ASL default is ~1 year) — the backstop for a container that crashes
        after acking the dispatch."""
        source = _read_source()
        idx = source.find("const containerBackedTask = (")
        block = source[idx : idx + 900]
        assert "TimeoutSeconds: timeoutSeconds" in block
        # Work ceiling (Lambda timeout / container read-timeout) + buffer
        assert "const CONTAINER_STEP_TIMEOUT_SECONDS = 1200" in source
        assert "const CREATE_INFRA_TIMEOUT_SECONDS = 900" in source

    @pytest.mark.parametrize(("state_name", "result_path"), sorted(CONTAINER_TOKEN_STATES.items()))
    def test_result_path_preserved(self, state_name: str, result_path: str):
        """SendTaskSuccess output lands at the SAME ResultPath the old
        payloadResponseOnly LambdaInvoke used — downstream readers (e.g.
        TestResultChoice, RecordFailure PROGRESS_ORDER) are unchanged."""
        source = _read_source()
        match = re.search(rf"containerBackedTask\(\s*'{state_name}',(.*?)\);", source, re.DOTALL)
        assert match, f"containerBackedTask call for '{state_name}' not found"
        assert f"'{result_path}'" in match.group(1), (
            f"State '{state_name}' must keep ResultPath {result_path}"
        )

    def test_container_retry_keeps_transient_matching(self):
        """SFN Retry still fires on TransientStepFailure — the container sends
        the exception class name via SendTaskFailure error, and Lambda-side
        raises remain invocation errors, so ErrorEquals match both paths."""
        source = _read_source()
        idx = source.find("const CONTAINER_STEP_RETRY")
        assert idx != -1, "CONTAINER_STEP_RETRY must exist"
        block = source[idx : idx + 300]
        assert "'TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException'" in block

    def test_container_catch_routes_to_record_failure(self):
        """Catch → RecordFailure preserved on the converted states."""
        source = _read_source()
        idx = source.find("const CONTAINER_STEP_CATCH")
        assert idx != -1, "CONTAINER_STEP_CATCH must exist"
        block = source[idx : idx + 200]
        assert "ErrorEquals: ['States.ALL'], Next: 'RecordFailure', ResultPath: '$.error'" in block

    def test_create_infrastructure_uses_lower_timeout(self):
        """CreateInfrastructure (600s work ceiling) gets the lower timeout."""
        source = _read_source()
        match = re.search(r"containerBackedTask\(\s*'CreateInfrastructure',(.*?)\);", source, re.DOTALL)
        assert match
        assert "CREATE_INFRA_TIMEOUT_SECONDS" in match.group(1)


class TestChoiceStates:
    """AC-8.3: Choice states use sfn.Choice and sfn.Condition."""

    @pytest.mark.parametrize("state_name", CHOICE_STATES)
    def test_uses_sfn_choice(self, state_name: str):
        """Each choice state uses sfn.Choice construct."""
        source = _read_source()
        pattern = rf"new sfn\.Choice\(this,\s*'{state_name}'"
        assert re.search(pattern, source), f"State '{state_name}' must use sfn.Choice"

    def test_check_schema_action_uses_and_condition(self):
        """CheckSchemaAction uses sfn.Condition.and() for complex branching."""
        source = _read_source()
        assert "sfn.Condition.and(" in source
        assert "sfn.Condition.stringEquals('$.schema_compare_result.action', 'use_existing_pipeline')" in source

    def test_check_schema_action_checks_mapping_id_present(self):
        """CheckSchemaAction verifies existing_mapping_id is present."""
        source = _read_source()
        assert "sfn.Condition.isPresent('$.schema_compare_result.existing_mapping_id')" in source

    def test_check_schema_mapping_approval_checks_boolean(self):
        """CheckSchemaMappingApproval checks approval_required boolean.

        AMENDED — p34 R4.5, a deliberate reversal, not a regression
        (``LGR-5906ec6-2``, ``LGR-60c4985-6``, both template / ratified).

        The operand was ``true`` and the residual branch was the permissive one,
        so an absent or malformed flag routed PAST the human. The gate now fails
        CLOSED: the permissive arm fires only on an explicit boolean ``false``
        and every other shape falls to ``WaitForSchemaMappingApproval``.

        The subject this test was written to prove is unchanged and still
        asserted: **the gate reads a boolean on that path.** The ``isBoolean``
        guard is asserted alongside the operand because that is what makes
        "a boolean" true of the value's TYPE and not only of its value — which
        is the half the old ``booleanEquals``-only assertion could not see.

        This is a source-text check, which ``requirements.md`` risk 8 and R8.5
        both record as the weaker form. The synthesized-definition assertions
        for this gate live in ``deploy-cdk/test/orchestration-stack.test.ts``.

        Validates: Requirements 4.5, 8.4
        """
        source = _read_source()
        assert "sfn.Condition.isBoolean('$.schema_mapping_result.approval_required')" in source
        assert "sfn.Condition.booleanEquals('$.schema_mapping_result.approval_required', false)" in source

    def test_check_code_approval_checks_boolean(self):
        """CheckCodeApproval checks approval_required boolean.

        AMENDED — p34 R4.1-4.3, a deliberate reversal, not a regression
        (``LGR-5906ec6-2``, ``LGR-60c4985-6``, both template / ratified).

        Same inversion as its sibling above, and the reason this spec exists:
        ``CheckCodeApproval`` was the only Choice in ``orchestration-stack.ts``
        whose residual branch was the permissive outcome. It now fires the
        permissive arm only on an explicit boolean ``false``.

        The subject is unchanged and still asserted: **the gate reads a boolean
        on that path** — and, per R3.3, that path is
        ``$.test_code_result.approval_required``, the merged field, which is the
        one the state machine has always read.

        Source-text check; see the sibling's docstring for why that is the
        weaker form and where the definition-level assertions live.

        Validates: Requirements 4.1, 4.2, 4.3, 8.4
        """
        source = _read_source()
        assert "sfn.Condition.isBoolean('$.test_code_result.approval_required')" in source
        assert "sfn.Condition.booleanEquals('$.test_code_result.approval_required', false)" in source


class TestWaitForTaskTokenStates:
    """AC-8.4: Approval gates use sfn.CustomState with waitForTaskToken."""

    @pytest.mark.parametrize("state_name", WAIT_FOR_TOKEN_STATES)
    def test_uses_custom_state(self, state_name: str):
        """Each waitForTaskToken state uses sfn.CustomState."""
        source = _read_source()
        pattern = rf"new sfn\.CustomState\(this,\s*'{state_name}'"
        assert re.search(pattern, source), f"State '{state_name}' must use sfn.CustomState"

    def test_wait_for_task_token_resource(self):
        """waitForTaskToken states reference the correct ARN resource.

        5 literal occurrences: the 3 approval gates + RunPipeline, plus ONE in
        the containerBackedTask helper that stamps the four container-backed
        steps (P28 R19.11) — 8 token states in the synthesized ASL total.
        """
        source = _read_source()
        count = source.count("arn:aws:states:::lambda:invoke.waitForTaskToken")
        assert count == 5, f"Expected 5 waitForTaskToken resource literals, found {count}"

    def test_schema_mapping_approval_type(self):
        """WaitForSchemaMappingApproval passes approval_type: schema_mapping."""
        source = _read_source()
        assert "'approval_type': 'schema_mapping'" in source

    def test_code_approval_type(self):
        """WaitForCodeApproval passes approval_type: code_generation."""
        source = _read_source()
        assert "'approval_type': 'code_generation'" in source

    def test_test_failure_approval_type(self):
        """ForceTestReviewApproval passes approval_type: test_failure (P28 R4.2)."""
        source = _read_source()
        assert "'approval_type': 'test_failure'" in source

    def test_test_failure_approval_carries_report(self):
        """ForceTestReviewApproval carries the structured report + remediation hint."""
        source = _read_source()
        assert "'test_result.$': '$.test_code_result.test_result'" in source
        # Regeneration = p16 retry-from-step (create_code), not an SFN loop
        assert "'remediation_hint': 'retry-from-step:create_code'" in source

    def test_run_pipeline_has_task_token(self):
        """RunPipeline passes taskToken from context."""
        source = _read_source()
        assert "'taskToken.$': '$$.Task.Token'" in source


class TestTransitionChain:
    """AC-8.5: States are chained in the correct order."""

    def test_main_chain_starts_with_file_validation(self):
        """The definition chain starts with fileValidation."""
        source = _read_source()
        assert "const definition = fileValidation" in source

    def test_main_chain_includes_schema_extract(self):
        """fileValidation chains to schemaExtract."""
        source = _read_source()
        assert "fileValidation.next(schemaExtract)" in source

    def test_main_chain_includes_schema_compare(self):
        """schemaExtract chains to schemaCompare."""
        source = _read_source()
        assert "schemaExtract.next(schemaCompare)" in source

    def test_main_chain_includes_check_schema_action(self):
        """schemaCompare chains to checkSchemaAction."""
        source = _read_source()
        assert "schemaCompare.next(checkSchemaAction)" in source

    def test_reuse_existing_mapping_to_approval(self):
        """ReuseExistingMapping transitions to checkSchemaMappingApproval."""
        source = _read_source()
        assert "reuseExistingMapping.next(checkSchemaMappingApproval)" in source

    def test_schema_mapping_to_approval(self):
        """SchemaMapping transitions to checkSchemaMappingApproval."""
        source = _read_source()
        assert "schemaMapping.next(checkSchemaMappingApproval)" in source

    def test_wait_schema_approval_reaches_create_code_through_the_counter_guard(self):
        """WaitForSchemaMappingApproval reaches CreateCode via CheckRegenerationAttempt.

        p32 D2 amends this assertion the same way P28 R5.5's timeout guard
        amended ``createQualityChecks.next(runPipeline)``: the guard is
        inserted, and the old direct edge is asserted GONE so the insertion
        cannot be silently undone. CreateCode is still the next step on this
        route — the guard only makes ``$.regeneration_attempt`` resolvable
        before CreateCode's payload reads it.
        """
        source = _read_source()
        assert "waitForSchemaMappingApproval.next(checkRegenerationAttempt)" in source
        assert "waitForSchemaMappingApproval.next(createCode)" not in source
        # Both guard exits land on CreateCode
        assert "applyDefaultRegenerationAttempt.next(createCode)" in source
        guard = re.search(
            r"checkRegenerationAttempt\s*\.when\((.*?)\.otherwise\(applyDefaultRegenerationAttempt\)",
            source,
            re.DOTALL,
        )
        assert guard is not None, "CheckRegenerationAttempt's branching not found"
        assert "createCode" in guard.group(1), "the guard's pass-through branch must target CreateCode"

    def test_create_code_to_test_code(self):
        """CreateCode transitions to testCode."""
        source = _read_source()
        assert "createCode.next(testCode)" in source

    def test_test_code_to_test_result_choice(self):
        """TestCode transitions to testResultChoice (P28 R4.1) — never straight to approval."""
        source = _read_source()
        assert "testCode.next(testResultChoice)" in source
        assert "testCode.next(checkCodeApproval)" not in source

    def test_test_result_choice_branches_on_structured_status(self):
        """TestResultChoice: passed → checkCodeApproval, anything else → forced review."""
        source = _read_source()
        assert "sfn.Condition.stringEquals('$.test_code_result.test_result.status', 'passed')" in source
        assert "sfn.Condition.isPresent('$.test_code_result.test_result.status')" in source
        assert ".otherwise(forceTestReviewApproval)" in source

    def test_test_result_choice_regeneration_branch_reenters_create_code(self):
        """p32 D2: the middle branch loops back to CreateCode in this execution.

        The bound is re-evaluated on the edge, so a regression in test_code
        cannot produce an unbounded loop, and the loop-back is a plain Pass
        pair rather than a new execution or a new run id (``LGR-21a1b5b-2``,
        ``LGR-21a1b5b-4``).

        Validates: Requirements 2.6, 2.17
        """
        source = _read_source()
        assert "sfn.Condition.booleanEquals('$.test_code_result.regeneration_needed', true)" in source
        assert (
            "sfn.Condition.numberLessThanJsonPath(\n"
            "            '$.test_code_result.regeneration_attempt',\n"
            "            '$.test_code_result.regeneration_max_attempts',\n"
            "          )" in source
        )
        assert "computeNextRegenerationAttempt.next(incrementRegenerationAttempt)" in source
        assert "incrementRegenerationAttempt.next(createCode)" in source
        # In-execution only: nothing on the loop starts another execution
        assert "states:startExecution" not in source

    def test_regeneration_counter_reaches_both_agent_steps(self):
        """CreateCode and TestCode payloads carry the attempt counter (p32 2.16, 2.17)."""
        source = _read_source()
        assert source.count("'regeneration_attempt.$': REGENERATION_ATTEMPT_PATH") == 2
        assert "const REGENERATION_ATTEMPT_PATH = '$.regeneration_attempt'" in source

    def test_force_test_review_approve_anyway_resumes_at_check_code_approval(self):
        """Approve-anyway proceeds to the SAME next state as a passed test (P28 R4.2)."""
        source = _read_source()
        assert "forceTestReviewApproval.next(checkCodeApproval)" in source

    def test_wait_code_approval_to_create_infrastructure(self):
        """WaitForCodeApproval transitions to createInfrastructure."""
        source = _read_source()
        assert "waitForCodeApproval.next(createInfrastructure)" in source

    def test_create_infrastructure_to_create_glue_pipeline(self):
        """CreateInfrastructure transitions to createGluePipeline."""
        source = _read_source()
        assert "createInfrastructure.next(createGluePipeline)" in source

    def test_create_glue_pipeline_to_create_quality_checks(self):
        """CreateGluePipeline transitions to createQualityChecks."""
        source = _read_source()
        assert "createGluePipeline.next(createQualityChecks)" in source

    def test_create_quality_checks_to_timeout_guard(self):
        """CreateQualityChecks transitions to the timeout guard (P28 R5.5), never straight to RunPipeline."""
        source = _read_source()
        assert "createQualityChecks.next(checkJobTimeoutConfig)" in source
        assert "createQualityChecks.next(runPipeline)" not in source

    def test_timeout_guard_default_branch_reaches_run_pipeline(self):
        """ApplyDefaultJobTimeout (the absent-config branch) flows into RunPipeline."""
        source = _read_source()
        assert "applyDefaultJobTimeout.next(runPipeline)" in source

    def test_run_pipeline_to_release_lock(self):
        """RunPipeline transitions to ReleaseLockSuccess."""
        source = _read_source()
        assert "runPipeline.next(releaseLockSuccess)" in source

    def test_release_lock_to_update_success(self):
        """ReleaseLockSuccess transitions to UpdateSuccessState."""
        source = _read_source()
        assert "releaseLockSuccess.next(updateSuccessState)" in source


class TestPassStates:
    """Verify Pass states are correctly defined."""

    def test_reuse_existing_mapping_is_pass(self):
        """ReuseExistingMapping is a sfn.Pass state."""
        source = _read_source()
        assert "new sfn.Pass(this, 'ReuseExistingMapping'" in source

    def test_update_success_state_is_pass(self):
        """UpdateSuccessState is a sfn.Pass state."""
        source = _read_source()
        assert "new sfn.Pass(this, 'UpdateSuccessState'" in source

    def test_apply_default_job_timeout_is_pass(self):
        """ApplyDefaultJobTimeout is a sfn.Pass state (P28 R5.5)."""
        source = _read_source()
        assert "new sfn.Pass(this, 'ApplyDefaultJobTimeout'" in source

    @pytest.mark.parametrize(
        "state_name",
        ["ApplyDefaultRegenerationAttempt", "ComputeNextRegenerationAttempt", "IncrementRegenerationAttempt"],
    )
    def test_regeneration_counter_states_are_pass(self, state_name: str):
        """p32 D2's counter states are Pass states — no Lambda, no service call."""
        source = _read_source()
        assert f"new sfn.Pass(this, '{state_name}'" in source


class TestRunPipelineTimeout:
    """P28 R5.5 (task 6.3): the RunPipeline timeout derives from pipeline config.

    Validates: Requirements 5.5
    """

    def test_run_pipeline_uses_timeout_seconds_path(self):
        """RunPipeline reads its timeout from $.pipeline_config.max_job_duration_seconds."""
        source = _read_source()
        assert "TimeoutSecondsPath: MAX_JOB_DURATION_PATH" in source
        assert "'$.pipeline_config.max_job_duration_seconds'" in source

    def test_no_hardcoded_3600_timeout(self):
        """The old hardcoded 1-hour RunPipeline timeout is gone."""
        source = _read_source()
        assert "TimeoutSeconds: 3600" not in source

    def test_default_ceiling_is_six_hours(self):
        """The in-machine default ceiling is the documented 21600s (6h)."""
        source = _read_source()
        assert "DEFAULT_MAX_JOB_DURATION_SECONDS = 21600" in source

    def test_choice_guards_presence_type_and_positivity(self):
        """CheckJobTimeoutConfig only routes config values that TimeoutSecondsPath can consume."""
        source = _read_source()
        assert "sfn.Condition.isPresent(MAX_JOB_DURATION_PATH)" in source
        assert "sfn.Condition.isNumeric(MAX_JOB_DURATION_PATH)" in source
        assert "sfn.Condition.numberGreaterThan(MAX_JOB_DURATION_PATH, 0)" in source


class TestRecordFailure:
    """Verify the RecordFailure Catch path (P28 R1.3/R1.4)."""

    def test_failure_state_uses_lambda_invoke(self):
        """RecordFailure uses tasks.LambdaInvoke."""
        source = _read_source()
        assert "new tasks.LambdaInvoke(this, 'RecordFailure'" in source

    def test_failure_state_invokes_record_failure_lambda(self):
        """RecordFailure invokes recordFailureFn."""
        source = _read_source()
        # Find the RecordFailure block
        idx = source.find("new tasks.LambdaInvoke(this, 'RecordFailure'")
        assert idx != -1
        block = source[idx : idx + 300]
        assert "recordFailureFn" in block

    def test_retry_covers_transient_step_failures(self):
        """The shared Retry policy matches TransientStepFailure + Lambda service errors."""
        source = _read_source()
        assert "'TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException'" in source

    def test_custom_states_catch_all_to_record_failure(self):
        """The four waitForTaskToken CustomStates catch States.ALL → RecordFailure."""
        source = _read_source()
        count = source.count("Catch: [{ ErrorEquals: ['States.ALL'], Next: 'RecordFailure', ResultPath: '$.error' }]")
        assert count == 4, f"Expected 4 CustomState catches to RecordFailure, found {count}"

    def test_record_failure_lambda_uses_asset_code(self):
        """The record-failure Lambda comes from lambda/agentic_data_ingestion_record_failure (not inline)."""
        source = _read_source()
        assert "agentic_data_ingestion_record_failure" in source


class TestImports:
    """Verify correct CDK imports are used."""

    def test_imports_sfn_tasks(self):
        """File imports aws-stepfunctions-tasks."""
        source = _read_source()
        assert "import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks'" in source

    def test_imports_sfn(self):
        """File imports aws-stepfunctions."""
        source = _read_source()
        assert "import * as sfn from 'aws-cdk-lib/aws-stepfunctions'" in source
