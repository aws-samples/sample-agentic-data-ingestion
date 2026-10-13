# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 4 (Defect 4): a second attempt is genuinely a second attempt.

*** THIS TEST IS EXPECTED TO FAIL ON UNFIXED CODE. THE FAILURE IS THE POINT. ***

p32 task 13 is a red-first task. This file is its deliverable. Do NOT "fix" it,
and do NOT change production code to make it pass — tasks 14, 15, 16 and 17 do
that. tasks.md's preamble: "Red-first tasks fail on purpose — 2, 5, 9 and 13 …
Whoever runs this list — or reviews a run of it — should read those four
failures as the deliverable."

It lives in ``tests/integration/``, which ``make test-fast`` does not run
(unit + contract only), so the fast gate stays green. The failure surfaces
under ``make test-ci`` or a direct pytest run of this directory.

design.md §6, Property 4:

    _For any_ regeneration or retry-from-step execution input whose
    ``regeneration_attempt`` is absent or 0, the fixed pipeline SHALL produce a
    run with ``regeneration_attempt > 0``, a ``create_code`` prompt differing
    from the original, resumption at ``retry_from_step``, and attempts bounded
    by ``regeneration_max_attempts``. The resumption clause is gated on V1.

bugfix.md's predicate:

    FUNCTION isBugCondition_D4(X)
      INPUT: X of type ExecutionInput
      RETURN X is a regeneration or retry-from-step execution
         AND (X.regeneration_attempt IS absent OR X.regeneration_attempt = 0)
    END FUNCTION

**Validates: Requirements 2.15, 2.16, 2.17, 2.18**

Why an integration test rather than a unit test
-----------------------------------------------
No single function owns this property. It spans the trigger contract
(``shared/rerun_contract.py``), the state machine that advances the run
(``deploy-cdk/lib/orchestration-stack.ts``), create_code's prompt build, and
test_code's regeneration decision. Following the precedent set by
``test_test_failure_gating.py:5-9``, the state machine is simulated at the
Lambda level — moto cannot execute a real state machine with Lambda
integrations — and every decision below is taken by the REAL production
function, never by a reimplementation in this file. Where a seam the fix needs
does not exist yet, the test says so in the assertion message rather than
inventing it.

Expectation sourcing
--------------------
`LGR-28b4d75-5` (template): expectations come from the requirements, not from
current output — "a test written from the producer's output cannot detect the
producer drifting, because the output is its own oracle." Every assertion below
is traceable to a 2.x clause. Current output here is *uniformly* wrong (the
counter is always 0), so a test written from it would assert the bug.

Counterexamples observed on unfixed code
----------------------------------------
Verified against the working tree at the time this test was written. Line
numbers are current; tasks 6, 7a and 7b shifted them from the ones quoted in
tasks.md.

1. **Nothing writes the counter, so it is always 0.**
   ``regeneration_attempt`` is READ at ``test_code/lambda_function.py:894``
   (``event.get("regeneration_attempt", 0)``) and
   ``create_code/lambda_function.py:358`` (identical), and threaded through
   both handlers' outputs (test_code ``:1312``, ``:1391``; create_code
   ``:634``, ``:654``, ``:707``, ``:753``, ``:767``, ``:807``). It occurs
   **0 times in ``deploy-cdk/lib/``** and **0 times in ``shared/``** — so it is
   absent from ``build_trigger_input``'s signature and from every execution
   input the contract can produce. ``event.get(..., 0)`` therefore returns 0 on
   every step of every run, forever.

2. **The feedback gate never fires, so a retry rebuilds a byte-identical
   prompt.** ``create_code/lambda_function.py:476`` reads
   ``get_regeneration_feedback(run_id) if regeneration_attempt > 0 else {}``.
   With the counter pinned at 0 the call never happens,
   ``_build_feedback_section({})`` returns ``""``
   (``transform_generation.py:44-45``), the ``feedback_section`` slot the prompt
   template reserves for exactly this purpose
   (``transform_generation.j2``, "P28 R13.3: on regeneration the reviewer's
   issues are injected here so the new prompt differs from the original")
   renders empty, and attempt 2's prompt is byte-for-byte attempt 1's.

3. **The bound is stuck open, and the verdict is routed to a human.**
   ``analyze_test_results_for_regeneration:430`` evaluates
   ``regeneration_triggers and regeneration_attempt < max_attempts`` — i.e.
   ``0 < 2`` — on **every** failing run, so ``regeneration_needed`` is
   permanently True, and ``max_attempts_reached`` (``:439``, ``0 >= 2``) is
   permanently False. ``determine_code_approval_requirement`` then converts
   that into "approval required" at ``:488-490`` ("Approval required due to
   regeneration needed"), which short-circuits ahead of its own ``test_status``
   branch at ``:495``. So the system concludes on every failure that the code
   should be regenerated, and asks a human instead.

   (Task 5 corrected tasks.md's line reference here: the forcing site is the
   ``regeneration_needed`` branch, not the ``test_status`` branch. Task 6 also
   narrowed the trigger to ``test_status == "failed"`` only, removing the
   ``checks_failed > 0`` disjunct — see ``:419-427``.)

4. **A retry-from-step execution does not resume.** Confirmed live in task 12
   (V1, design.md "V1 live finding"): the retry re-entered ``FileValidation``
   (step 1), the step actually ran, and its output **overwrote** the pre-seeded
   ``file_validation_result``, after which the next state's JSONPath could not
   be found. ``should_skip_step`` has 18 callers, all tests, and was verified
   not reached at runtime. The resumption clause is therefore real but is kept
   **skipped** here so task 17 can enable it separately.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.integration

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

# Pre-import the condition classes before any patch.dict(sys.modules) block
# (test_mapping_freshness.py:38-45).
import boto3.dynamodb.conditions  # noqa: F401  (import order is the point)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_ROOT = _PROJECT_ROOT / "lambda"
_SHARED_DIR = _PROJECT_ROOT / "shared"
_TEST_CODE_DIR = _LAMBDA_ROOT / "agentic_data_ingestion_test_code"
_CREATE_CODE_DIR = _LAMBDA_ROOT / "agentic_data_ingestion_create_code"
_CDK_LIB_DIR = _PROJECT_ROOT / "deploy-cdk" / "lib"

for _path in (str(_SHARED_DIR), str(_CREATE_CODE_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def _load_module(alias: str, path: Path):
    """Load a module by file path under a unique alias."""
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


# ── The real production units this property spans ───────────────────────────

_rerun_contract = _load_module("p32_t13_rerun_contract", _SHARED_DIR / "rerun_contract.py")
_prompt_loader = _load_module("p32_t13_prompt_template_loader", _SHARED_DIR / "prompt_template_loader.py")
_step_names = _load_module("p32_t13_step_names", _SHARED_DIR / "step_names.py")
# p32 task 17: the resume mechanism. Imported under their real module names —
# step_errors does a function-local `import retry_utils` at call time, so the
# two must resolve to the same module object the decorator will find.
import retry_utils as _retry_utils
import step_errors as _step_errors

# The five resumable steps, in pipeline order. These are exactly the
# `tasks.LambdaInvoke` states: Decision D3 excludes the waitForTaskToken steps
# because `step_handler` is their OUTERMOST decorator, so serving a cached
# result would return before the token is sent and the state would stall.
_RESUMABLE_ORDER = (
    _step_names.StepName.FILE_VALIDATION,
    _step_names.StepName.SCHEMA_EXTRACT,
    _step_names.StepName.SCHEMA_COMPARE,
    _step_names.StepName.SCHEMA_MAPPING,
    _step_names.StepName.CREATE_QUALITY_CHECKS,
)


def _resumable(step_variant: str) -> bool:
    """Is *step_variant* one of the five resumable steps?"""
    try:
        return _step_names.from_any(step_variant) in _RESUMABLE_ORDER
    except ValueError:
        return False


def _original_sfn_result(step, run_id: str) -> dict:
    """The value *step* returned to Step Functions on the ORIGINAL run.

    Deliberately the step's resultPath contract — the fields downstream ASL
    Payloads dereference — and NOT the five-key ``output_data`` artifact. That
    distinction is the whole reason the resume mechanism needs its own cache:
    ``$.file_validation_result.run_id`` is not a field ``output_data`` has.
    """
    return {
        "statusCode": 200,
        "run_id": run_id,
        "original_pipeline_id": PIPELINE["pipeline_id"],
        "source_format": "csv",
        "produced_by": f"original-{step}",
    }


def _reexecution_sfn_result(step, run_id: str) -> dict:
    """What the step would emit if it RE-RAN on the retry.

    Distinguishable from the original by `produced_by`, so a cached value that
    was silently overwritten is visible rather than merely suspected — the
    exact substitution V1 caught, where a 500 body landed at
    ``file_validation_result`` and the next state's JSONPath vanished.
    """
    return {
        "statusCode": 200,
        "run_id": run_id,
        "original_pipeline_id": PIPELINE["pipeline_id"],
        "source_format": "csv",
        "produced_by": f"reexecuted-{step}",
    }


# create_code's prompt builder and the feedback reader it is gated on.
_transform_generation = _load_module("p32_t13_transform_generation", _CREATE_CODE_DIR / "transform_generation.py")
_mapping_resolution = _load_module("p32_t13_mapping_resolution", _CREATE_CODE_DIR / "mapping_resolution.py")

# test_code's decision functions. Loaded with the heavy imports stubbed, the
# same way tests/unit/lambdas/test_code/* do it.
_test_code_stubs = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

if str(_TEST_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_CODE_DIR))
with (
    patch.dict("sys.modules", _test_code_stubs),
    patch("boto3.client", MagicMock()),
    patch("boto3.resource", MagicMock()),
):
    sys.modules.pop("p32_t13_test_code", None)
    _test_code = _load_module("p32_t13_test_code", _TEST_CODE_DIR / "lambda_function.py")


PIPELINE = {"pipeline_id": "customer-pipeline", "source_type": "S3_UPLOAD", "source_bucket": "adi-source"}

# The reviewer verdict a failing test run carries into the regeneration
# decision (the shape effective_verdict / _build_feedback_section consume).
FAILING_VERDICT = {
    "verdict": "fail",
    "issues": [
        {"severity": "critical", "category": "Correctness", "description": "to_date format string is wrong."},
        {"severity": "major", "category": "Schema Compliance", "description": "target column missing."},
    ],
}

MAPPINGS = [
    {
        "source_field": "cust_name",
        "source_type": "string",
        "target_field": "name",
        "target_type": "string",
        "transform": "trim",
    },
    {
        "source_field": "signup",
        "source_type": "string",
        "target_field": "signup_date",
        "target_type": "date",
        "transform": "to_date",
    },
]


# ── isBugCondition_D4 ───────────────────────────────────────────────────────


@st.composite
def bug_condition_execution_inputs(draw):
    """Generate X such that isBugCondition_D4(X).

    "A regeneration or retry-from-step execution" is exactly
    ``rerun_contract.RERUN_TRIGGERS`` — ``retry`` (the dashboard's
    retry-from-step) and ``self_healing`` (the agent's re-run after a failure).
    Design D1 chose those two as the flavors that may legally carry
    ``regeneration_attempt``, so generating outside that set would be
    generating inputs the contract is entitled to reject.

    The input is built by the REAL ``build_trigger_input`` — the production
    contract every producer uses — so no example can be a shape the pipeline
    could never see. The predicate's "absent OR 0" disjunction is drawn:
    absent is what the contract emits today; the explicit 0 models a producer
    that sets the key to its initial value.
    """
    triggered_by = draw(st.sampled_from(sorted(_rerun_contract.RERUN_TRIGGERS)))
    suffix = draw(st.text(alphabet="0123456789abcdef", min_size=8, max_size=8))
    run_id = f"run-2026-08-04-00-00-00-{suffix}"
    retry_step = draw(st.sampled_from(["create_code", "test_code", "schema_mapping"]))

    kwargs = {"triggered_by": triggered_by, "run_id": run_id, "original_run_id": run_id}
    if triggered_by == "retry":
        kwargs["retry_from_step"] = retry_step

    execution_input = _rerun_contract.build_trigger_input(PIPELINE, **kwargs)

    if draw(st.booleans()):
        # The "X.regeneration_attempt = 0" arm of the predicate.
        execution_input["regeneration_attempt"] = 0

    return execution_input


def is_bug_condition_d4(execution_input: dict) -> bool:
    """bugfix.md's predicate, transcribed."""
    is_rerun = execution_input.get("triggered_by") in _rerun_contract.RERUN_TRIGGERS
    attempt = execution_input.get("regeneration_attempt")
    return is_rerun and (attempt is None or attempt == 0)


# ── The pipeline, simulated at the Lambda level ─────────────────────────────


def _build_create_code_prompt(regeneration_feedback: dict) -> str:
    """The prompt create_code sends for the transform block.

    The real template render (``StrictUndefined``, `LGR-d3c81fc-1`) through the
    real feedback renderer, so the "prompt differs" clause is measured on the
    bytes the model would actually receive.
    """
    return _prompt_loader.load_prompt_template(
        "transform_generation.j2",
        mappings=MAPPINGS,
        feedback_section=_transform_generation._build_feedback_section(regeneration_feedback),
    )


def _advance_run(execution_input: dict, next_attempt: int) -> dict:
    """Produce the execution input for the next regeneration attempt.

    2.15: ``regeneration_attempt`` "SHALL be set by the producer that starts or
    advances the run, SHALL be part of the trigger-input contract in
    ``shared/rerun_contract.py``". So the next input is asked of the production
    contract. This is the seam the fix has to supply; it is not modelled here.
    """
    kwargs = {
        "triggered_by": execution_input["triggered_by"],
        "run_id": execution_input["run_id"],
        "original_run_id": execution_input.get("original_run_id"),
        "regeneration_attempt": next_attempt,
    }
    if execution_input["triggered_by"] == "retry":
        kwargs["retry_from_step"] = execution_input.get("retry_from_step")
    try:
        return _rerun_contract.build_trigger_input(PIPELINE, **kwargs)
    except TypeError as exc:
        raise AssertionError(
            "2.15 UNMET — the trigger contract cannot carry the counter, so no producer "
            "can advance a run: shared/rerun_contract.build_trigger_input rejected "
            f"regeneration_attempt ({exc}). `regeneration_attempt` occurs 0 times in "
            "shared/ and 0 times in deploy-cdk/lib/, while being read at "
            "test_code/lambda_function.py:894 and create_code/lambda_function.py:358 — "
            "so event.get('regeneration_attempt', 0) returns 0 on every step of every run."
        ) from exc


def _drive_regeneration_loop(execution_input: dict, feedback_table, *, hop_limit: int) -> dict:
    """Run failing test → regeneration → create_code, using real functions only.

    Each hop is one create_code + test_code pair. Every decision is taken by
    production code: ``analyze_test_results_for_regeneration`` decides whether
    to regenerate, ``store_regeneration_feedback`` persists the reviewer's
    issues, ``mapping_resolution.get_regeneration_feedback`` reads them back,
    and the gate condition is create_code's own
    (``lambda_function.py:476``: feedback is read only when the counter is
    above 0).

    ``hop_limit`` is deliberately larger than the bound so an unbounded loop is
    observable rather than hidden by the loop's own exit.
    """
    max_attempts = _test_code.get_regeneration_max_attempts()
    run_id = execution_input["run_id"]

    current = dict(execution_input)
    attempts_seen: list[int] = []
    prompts: list[str] = []
    analyses: list[dict] = []
    approval_required: list[bool] = []

    for _ in range(hop_limit):
        attempt = current.get("regeneration_attempt", 0)
        attempts_seen.append(attempt)

        # --- create_code -------------------------------------------------
        # The production gate at create_code/lambda_function.py:476.
        feedback = _mapping_resolution.get_regeneration_feedback(run_id, feedback_table) if attempt > 0 else {}
        prompts.append(_build_create_code_prompt(feedback))

        # --- test_code, on a failing run ---------------------------------
        # 4 checks executed, 3 passed: one genuine failure, so test_status is
        # "failed" — the trigger task 6 left in place (:427).
        analysis = _test_code.analyze_test_results_for_regeneration(
            test_status="failed",
            checks_run=4,
            checks_passed=3,
            review_verdict=FAILING_VERDICT,
            regeneration_attempt=attempt,
        )
        analyses.append(analysis)
        approval_required.append(
            _test_code.determine_code_approval_requirement(
                test_status="failed",
                checks_run=4,
                checks_passed=3,
                review_verdict=FAILING_VERDICT,
                regeneration_analysis=analysis,
            )
        )

        if not analysis["regeneration_needed"]:
            break

        with patch.object(_test_code, "pipeline_runs_table", feedback_table):
            _test_code.store_regeneration_feedback(run_id, analysis["feedback_context"])

        current = _advance_run(current, attempt + 1)

    return {
        "max_attempts": max_attempts,
        "attempts_seen": attempts_seen,
        "prompts": prompts,
        "analyses": analyses,
        "approval_required": approval_required,
    }


# ── Property 4 ──────────────────────────────────────────────────────────────


@settings(
    max_examples=20,
    # The moto tables are function-scoped, which Hypothesis flags because state
    # can leak between examples. Every example draws its own run id, and the
    # feedback row is keyed on it, so there is no cross-example visibility.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(execution_input=bug_condition_execution_inputs())
def test_a_second_attempt_is_genuinely_a_second_attempt(execution_input, mock_dynamodb_tables):
    """Property 4: for any bug-condition input, the run regenerates and is bounded.

    EXPECTED TO FAIL until tasks 14-16 land. Three of Property 4's four clauses
    are asserted here; the resumption clause is gated on V1 and lives in the
    skipped test below.

    **Validates: Requirements 2.15, 2.16, 2.17**
    """
    assert is_bug_condition_d4(execution_input), "generator must only produce bug-condition inputs"
    feedback_table = mock_dynamodb_tables["agentic_data_ingestion_state"]

    max_attempts = _test_code.get_regeneration_max_attempts()
    # One initial generation plus at most `max_attempts` regenerations, and two
    # spare hops so an unbounded loop shows up as a bound violation below
    # rather than as this loop's own exit condition.
    result = _drive_regeneration_loop(execution_input, feedback_table, hop_limit=max_attempts + 3)

    attempts_seen = result["attempts_seen"]
    prompts = result["prompts"]
    analyses = result["analyses"]

    # ── Clause 1 (2.15): the counter advances ────────────────────────────
    assert max(attempts_seen) > 0, (
        "2.15 UNMET — regeneration_attempt never rises above 0 across "
        f"{len(attempts_seen)} attempt(s) {attempts_seen}. Every read is "
        "event.get('regeneration_attempt', 0) against an input no producer writes."
    )

    # ── Clause 2 (2.16): the second prompt differs from the first ────────
    assert len(prompts) >= 2, (
        "2.17 UNMET — the run never reached a second create_code attempt, so there is "
        "no second prompt to compare. A regeneration_needed verdict did not cause a "
        "regeneration."
    )
    assert prompts[1] != prompts[0], (
        "2.16 UNMET — attempt 2 rebuilt a BYTE-IDENTICAL prompt. The gate at "
        "create_code/lambda_function.py:476 (`if regeneration_attempt > 0`) never fired, "
        "so get_regeneration_feedback was never called, _build_feedback_section({}) "
        "returned '', and transform_generation.j2's feedback_section slot rendered empty."
    )
    # The difference must be the reviewer's issues, not incidental drift — 2.16
    # requires the prior iteration's feedback to be what makes it differ.
    assert FAILING_VERDICT["issues"][0]["description"] in prompts[1], (
        "2.16 UNMET — the second prompt differs but does not carry the reviewer's issues, "
        "so nothing tells the model what to fix."
    )

    # ── Clause 4 (2.17): attempts are bounded ────────────────────────────
    regenerations = sum(1 for a in analyses if a["regeneration_needed"])
    assert regenerations <= max_attempts, (
        f"2.17 UNMET — {regenerations} regenerations requested with max_attempts="
        f"{max_attempts}. analyze_test_results_for_regeneration:430 evaluates "
        f"`regeneration_attempt < max_attempts` as {attempts_seen[0]} < {max_attempts} on "
        "every failing run, so regeneration_needed is permanently True."
    )
    assert analyses[-1]["max_attempts_reached"] is True, (
        "2.17 UNMET — the run stopped regenerating without max_attempts_reached ever "
        "becoming True (:439, `regeneration_attempt >= max_attempts`), so the bound was "
        "never the thing that stopped it."
    )
    # Only then may it fall through to a human, and it must.
    assert result["approval_required"][-1] is True, (
        "2.17 UNMET — the bounded-out run did not fall through to a human decision."
    )
    assert not any(a["regeneration_needed"] for a in analyses[:-1] if a["max_attempts_reached"]), (
        "2.17 UNMET — a run both reached the bound and was told to regenerate again."
    )


@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(execution_input=bug_condition_execution_inputs())
def test_a_retry_from_step_execution_resumes_at_that_step(execution_input, mock_dynamodb_tables, monkeypatch):
    """Property 4, resumption clause: the run resumes at ``retry_from_step``.

    2.18: steps that completed in the original run SHALL be skipped or served
    from their cached results, and SHALL NOT be silently re-executed while
    their cached results are discarded.

    Task 17 owns the mechanism and this assertion. The one thing V1 surfaced
    that the static reading did not: the cached ``{step}_result`` keys are not
    inert on a re-run, they are clobbered by the re-executed step, so whichever
    of skip-or-serve is chosen, the key must survive the step it belongs to.
    That is why this test asserts on BOTH halves — which steps ran, and what
    value reached each step's ``resultPath``.

    The pipeline is simulated at the Lambda level, as everywhere else in this
    file, but every decision is taken by production code:
    ``step_errors.step_handler`` wraps each step exactly as the real handlers
    are wrapped, and it consults ``retry_utils.resolve_step_resume``, which
    consults ``retry_utils.should_skip_step``. Nothing about the skip decision
    is reimplemented here.

    Scope of the mechanism (Decision D3): the five ``tasks.LambdaInvoke``
    steps are resumable. The ``waitForTaskToken`` steps are not — a served
    result would return before the task token is sent and the state would
    stall — so they are excluded here for the same reason production excludes
    them.

    **Validates: Requirements 2.18, 3.18**
    """
    # Task 13's stub used pytest.skip here, which is correct for a placeholder
    # and wrong for an assertion: inside @given it skips the WHOLE test as soon
    # as one drawn example is the self_healing flavor, so the property would
    # silently not run about half the time. `assume` rejects the example and
    # Hypothesis draws another, which is the same filter with teeth.
    assume(execution_input.get("triggered_by") == "retry")

    state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
    monkeypatch.setenv("STATE_TABLE", state_table.table_name)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    run_id = execution_input["run_id"]
    retry_from_step = execution_input["retry_from_step"]

    # Expectations come from the CANONICAL pipeline order in
    # shared/step_names.py — the requirements' own ordering — not from
    # should_skip_step, which is the code under test here (`LGR-28b4d75-5`: a
    # test built from the producer's output cannot detect the producer
    # drifting). A resumable step is expected to be served iff it sits
    # strictly before the resume point in that order; note that
    # create_quality_checks sits AFTER the four waitForTaskToken steps, so
    # resuming at create_code leaves it to run.
    canonical = list(_step_names.CANONICAL_ORDER)
    resume_position = canonical.index(_step_names.from_any(retry_from_step))
    expected_served = [step for step in _RESUMABLE_ORDER if canonical.index(step) < resume_position]
    expected_executed = [str(step) for step in _RESUMABLE_ORDER if canonical.index(step) >= resume_position]

    # ── The original run: every resumable step completed and cached what it
    #    returned to Step Functions. This is the state a real first run leaves
    #    behind now that step_handler caches on success.
    original_results = {step: _original_sfn_result(step, run_id) for step in _RESUMABLE_ORDER}
    for step, result in original_results.items():
        state_table.put_item(Item={"run_id": run_id, "step_name": str(step), "status": "success"})
        _retry_utils.cache_step_result(run_id, str(step), result)

    # ── The retry: drive each resumable step through the REAL decorator.
    executed: list[str] = []
    result_path: dict[str, dict] = {}

    for step in _RESUMABLE_ORDER:

        @_step_errors.step_handler(str(step))
        def handler(event, context, _step=step):
            executed.append(str(_step))
            # A re-executed step writes its OWN output at its resultPath. On
            # the live run task 12 observed, this is where the cached success
            # was destroyed by a 500 body.
            return _reexecution_sfn_result(_step, run_id)

        result_path[str(step)] = handler(
            {"run_id": run_id, "retry_from_step": retry_from_step},
            None,
        )

    # ── Clause: the run resumes AT retry_from_step ────────────────────────
    assert executed == expected_executed, (
        f"2.18 UNMET — retrying from {retry_from_step!r} should have resumed there. "
        f"Steps that ran: {executed}; expected {expected_executed}. Steps that should "
        f"have been served from cache: {[str(s) for s in expected_served]}."
    )

    # run.resumed_at_step = X.retry_from_step. Read as: the earliest step the
    # retry actually performed is the resume point, so nothing before it was
    # redone. When retry_from_step is itself resumable that is literal
    # equality; when it is a waitForTaskToken step (Decision D3 — CreateCode,
    # TestCode, …) the resume point is that step, which this simulation does
    # not drive, and the observable claim is that no resumable step before it
    # ran.
    if _resumable(retry_from_step):
        assert executed and executed[0] == str(_step_names.from_any(retry_from_step)), (
            f"2.18 UNMET — the run resumed at {executed[:1]}, not at {retry_from_step!r}."
        )
    else:
        assert not any(str(step) in executed for step in expected_served), (
            f"2.18 UNMET — retrying from {retry_from_step!r} re-executed a step that "
            f"completed before it: {[str(s) for s in expected_served if str(s) in executed]}."
        )

    # ── Clause: the cached results were NOT discarded ─────────────────────
    # The other half of 2.18, and the half V1 showed matters most: a skipped
    # step's {step}_result key must still hold the ORIGINAL run's value.
    for step in _RESUMABLE_ORDER:
        if str(step) in executed:
            continue
        landed = result_path[str(step)]
        original = original_results[step]
        for field, value in original.items():
            assert landed.get(field) == value, (
                f"2.18 UNMET — {step}'s cached {field!r} was discarded: its resultPath "
                f"carries {landed.get(field)!r}, not the original run's {value!r}. This is "
                f"the failure V1 observed live — the re-executed step's own output replaced "
                f"the cached shape the next state reads."
            )
        assert landed.get(_retry_utils.RESUMED_FROM_CACHE_KEY) is True, (
            f"2.18 UNMET — {step} was skipped without saying so. A silent skip is as "
            f"unauditable as a silent re-execution."
        )


def test_a_fresh_run_still_starts_at_file_validation(mock_dynamodb_tables, monkeypatch):
    """3.18: a fresh (non-retry) run is unchanged by the resume mechanism.

    Two halves, because the mechanism has two: the state machine still enters
    ``FileValidation`` first, and no step is skipped even when a warm cache
    for the same run id exists. The second half is the one that could regress
    silently — a resume decision that ignored the marker would still pass a
    StartAt assertion.

    **Validates: Requirements 3.18**
    """
    definition = (_CDK_LIB_DIR / "orchestration-stack.ts").read_text(encoding="utf-8")
    assert "const definition = fileValidation;" in definition, (
        "3.18 UNMET — the pipeline no longer starts at FileValidation."
    )

    state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
    monkeypatch.setenv("STATE_TABLE", state_table.table_name)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    run_id = "run-2026-08-04-00-00-00-freshrun"
    for step in _RESUMABLE_ORDER:
        state_table.put_item(Item={"run_id": run_id, "step_name": str(step), "status": "success"})
        _retry_utils.cache_step_result(run_id, str(step), _original_sfn_result(step, run_id))

    executed: list[str] = []
    for step in _RESUMABLE_ORDER:

        @_step_errors.step_handler(str(step))
        def handler(event, context, _step=step):
            executed.append(str(_step))
            return _reexecution_sfn_result(_step, run_id)

        # A fresh trigger input carries no retry_from_step at all.
        handler({"run_id": run_id}, None)

    assert executed == [str(s) for s in _RESUMABLE_ORDER], (
        f"3.18 UNMET — a fresh run skipped {set(str(s) for s in _RESUMABLE_ORDER) - set(executed)}. "
        f"Resume must be reachable only from a retry-from-step execution."
    )


# ── The producer that clause 1 needs (counterexample 1, documented) ─────────


def test_a_producer_writes_the_counter_into_the_execution_state():
    """Some producer must WRITE ``regeneration_attempt``, not just read it.

    EXPECTED TO FAIL. This is counterexample 1 stated directly, and it is the
    reason the property above cannot pass: the identifier is read in two
    handlers and written by nobody. Design D2 chose an in-execution Step
    Functions edge carrying the incremented counter in state, so
    ``deploy-cdk/lib/`` is where the writer belongs; 2.15 additionally requires
    the field in ``shared/rerun_contract.py``. Either one satisfies the "a
    producer exists" half, so both are checked and reported together.

    The grep form follows the repo's own precedent for pinning the absence or
    presence of a wiring string —
    ``test_honest_aggregation.py:336-340`` (P28 R4.4) is the same shape,
    inverted.

    **Validates: Requirements 2.15**
    """
    cdk_hits = sorted(
        path.relative_to(_PROJECT_ROOT).as_posix()
        for path in _CDK_LIB_DIR.rglob("*.ts")
        if "regeneration_attempt" in path.read_text(encoding="utf-8")
    )
    contract_source = (_SHARED_DIR / "rerun_contract.py").read_text(encoding="utf-8")
    in_contract = "regeneration_attempt" in contract_source

    assert cdk_hits or in_contract, (
        "2.15 UNMET — `regeneration_attempt` occurs 0 times in deploy-cdk/lib/ and 0 times "
        "in shared/rerun_contract.py, while being read at "
        "test_code/lambda_function.py:894 and create_code/lambda_function.py:358. "
        "No state machine payload and no trigger-contract field sets it, so "
        "event.get('regeneration_attempt', 0) is 0 on every step of every run and the "
        "loop in bugfix.md 1.14-1.16 is wired but dead."
    )
