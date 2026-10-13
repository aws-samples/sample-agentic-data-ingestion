# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p34-code-approval-gate-honors-config, Property 2: The Approval_Policy is exactly the disjunction of its three inputs
"""Property 2 — the Approval_Policy is exactly the disjunction of its three inputs.

*For any* combination of Code_Approval_Setting, Code_Quality_Gate outcome and
non-negative ``regeneration_attempt``, ``determine_merged_approval_requirement``
returns ``True`` **if and only if** at least one of (setting is ``True``, quality
gate requires approval, ``regeneration_attempt > 0``) holds, and the returned
input map agrees element-wise with those three normalised inputs.

**Validates: Requirements 1.1, 1.3, 2.1, 2.2, 2.3, 2.4, 3.5, 8.2**

Model-based, and why the model is a different shape from the implementation
--------------------------------------------------------------------------
The model is ``any(...)`` over a **list built in a different order** from the
implementation's dict, with each term derived from the requirement that states it
rather than from the code. That is small enough to be obviously correct, and
different enough to catch the two failure modes a hand-written truth table would
miss: an ordering error (an early ``return`` that lets one input decide before
another is consulted — exactly the shape p32 2.17 removed from
``determine_code_approval_requirement``, where ``regeneration_needed`` decided
ahead of the test status), and a short-circuit error (``or`` chains that skip
evaluating a later term, which matters here because the later term also has to
appear in the audit map).

The audit map is asserted element-wise for the same reason. R3.5 says the state
row records *which* of the three inputs required approval; a decision that is
right while its map is wrong produces an audit trail that misattributes the
reason, which is worse than no audit trail because it reads as authoritative.
Design §4.2 is why the two are returned together from one dict rather than by two
functions.

Why this file exists
--------------------
This is amended R8.2's second relocation target. It carries the
**disabled-with-regeneration escalation** of
``create_code.determine_approval_requirement`` — pinned today at
``tests/unit/lambdas/create_code/test_no_keyword_self_approval.py:136`` — which
design decision D2 deletes (``LGR-f077f1b-1``, **template / ratified**:
``test_code`` owns the code-generation Approval_Policy and ``create_code``'s
decision is deleted rather than renamed, because renaming a field nothing
consumes yields a renamed dead field). The escalation must be provable in its new
home before anything is deleted, which is why this task precedes the deletion.

Its sibling relocation target is Property 1
(``test_code_approval_setting_prop.py``), which carries the enabled / disabled /
absent / malformed branches; the read-error branch relocates onto the exception
classification examples at the configuration read.

Expectations come from the requirements, not from running anything
-----------------------------------------------------------------
There is no prior implementation of this policy to characterize — the defect this
spec fixes is that the merged decision does not exist, so the gate reads a field
that structurally cannot see the configuration. Each expectation names its
criterion:

* R2.1 — a quality gate requiring approval forces the gate irrespective of the
  setting. Generating the setting axis under a ``True`` quality gate is what
  proves "irrespective", rather than asserting it at one setting value.
* R2.2 — a setting of ``True`` forces the gate irrespective of the quality gate
  outcome, which together with R1.1 is the whole of the reported defect: a clean
  run on ``hello123`` was never reviewed although the record said to review it.
* R2.3 — ``regeneration_attempt > 0`` forces the gate irrespective of the
  setting. **The case that matters most** is the conjunction of all three
  criteria: setting explicitly disabled, quality gate ``False``, attempt count
  positive must still be ``True``. That is the escalation this file relocates,
  and it is the one combination where two of the three inputs say "no".
* R2.4 — the iff. Approval is not required only when none of the three requires
  it.
* R1.3 — that residual, stated as its own example: setting ``False``, quality
  ``False``, attempt ``0`` is the only shape that reaches
  ``CreateInfrastructure`` without an approval.
* R3.5 — the per-input map, element-wise.

What is deliberately NOT asserted here
--------------------------------------
Nothing about *how* the quality gate reached its answer.
``determine_code_approval_requirement`` (``:748-816``) is untouched by this task
and its three escalating branches are Property 3's subject
(``test_code_quality_gate_branches_prop.py``). This property takes that answer as
a given boolean, which is the point of the split: the merged policy must be
correct for every quality answer, including ones the quality gate cannot
currently produce.

Nothing about the wiring either. Task 2.4 owns the handler call, the R7.4 log
line and the state-row write; this file exercises a pure function.

On ``bool`` as ``int`` — what was decided and why
------------------------------------------------
``regeneration_attempt`` is generated over ``0`` and positive ints, and
**deliberately not** over ``True`` / ``False``, for two reasons that both had to
hold:

1. It is unreachable. The ASL guard ``CheckRegenerationAttempt``
   (``orchestration-stack.ts:774-782``) is ``And[IsPresent, IsNumeric, …]`` with
   an explicit apply-default state for the residual, and a JSON boolean does not
   satisfy ``IsNumeric``. A boolean cannot arrive at the handler's read
   (``:1323``) from the state machine.
2. It does not misbehave if it did arrive. ``bool`` is a subclass of ``int``, so
   ``True > 0`` is ``1 > 0`` — attempt 1, which is the arithmetically correct
   reading, and identical to what the model computes from the same value. There
   is no divergence to catch, so generating it would add examples that can only
   pass.

What a boolean input *could* corrupt is the audit map, if an implementation
stored the raw value instead of the comparison result. That is guarded directly:
``test_the_input_map_values_are_strict_booleans`` asserts every map value is a
real ``bool`` rather than a truthy stand-in, and
``test_a_boolean_regeneration_attempt_is_read_as_its_integer_value`` pins the
reading rather than leaving it to be rediscovered. Together those cover the
concern without pretending the input class is reachable.

Ledger provenance
-----------------
No record governs this gate; ``requirements.md`` §"Ledger provenance for this
area" is the record. ``LGR-f077f1b-1`` (**template / ratified**) records the
ownership decision this file's subject depends on, including both rejected
alternatives — the rename, and having ``create_code`` compute the policy for
``test_code`` to merge from ``$.create_code_result``.
``LGR-3e45e65-1`` (**template / ratified**) is why this file does not re-pin
``AWS_REGION``: ``tests/unit/conftest.py`` assigns it before any test module in
this directory is imported.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

# Account id is not pinned by tests/unit/conftest.py (only the region is —
# LGR-3e45e65-1), and env_utils is stubbed below in any case.
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy / AWS-touching imports so the module loads in isolation. Same set
# as the sibling files in this directory; the module-level cache pop is the
# house pattern for lambda_function cross-contamination (several Lambda dirs
# expose a module of that name).
_mods = {
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

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

#: The three keys R3.5's audit map carries, in the order design §4.3 lists them.
_INPUT_KEYS = ("pipeline_config", "quality_gate", "regeneration")


def _model_decision(config: bool, quality: bool, attempt: int) -> bool:
    """R2.4's disjunction, written from the requirement rather than from the code.

    Order is deliberately not the implementation's: the requirement names the
    setting first, and this list names it last, so a policy that let one input
    decide before another was consulted diverges here.
    """
    return any([attempt > 0, quality, config])


def _model_inputs(config: bool, quality: bool, attempt: int) -> dict[str, bool]:
    """R3.5's per-input map, built term by term from the criteria."""
    return {
        "pipeline_config": config,  # R2.2
        "quality_gate": quality,  # R2.1
        "regeneration": attempt > 0,  # R2.3
    }


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


#: Attempt counts. ``0`` is the fresh-run value the ASL's apply-default state
#: supplies, and it is the only value R1.3's residual admits, so it is sampled
#: explicitly rather than left to the integer strategy's shrinking to reach.
#: The positive arm reaches well past the configured bound (default 2) — a
#: ``max_attempts_reached`` run carries a count at or above it, and the policy's
#: statement is about every non-negative count, not only in-bounds ones. The
#: upper bound is wide on purpose: a range of 8 would make the whole input space
#: 36 values, which Hypothesis exhausts and then stops early, reporting far fewer
#: than the configured example count.
def _regeneration_attempts() -> st.SearchStrategy[int]:
    return st.one_of(st.just(0), st.integers(min_value=1, max_value=10_000))


def _decisions() -> st.SearchStrategy[bool]:
    return st.booleans()


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


@settings(max_examples=400, deadline=None)
@given(config=_decisions(), quality=_decisions(), attempt=_regeneration_attempts())
def test_the_policy_is_exactly_the_disjunction_of_its_three_inputs(config, quality, attempt):
    """Property 2 over the whole input space — the iff, and the map element-wise.

    Validates: Requirements 1.1, 1.3, 2.1, 2.2, 2.3, 2.4, 3.5
    """
    event(f"config={config} quality={quality} regeneration={attempt > 0}")

    decision, inputs = _mod.determine_merged_approval_requirement(
        config_requires_approval=config,
        quality_requires_approval=quality,
        regeneration_attempt=attempt,
    )

    expected_decision = _model_decision(config, quality, attempt)
    expected_inputs = _model_inputs(config, quality, attempt)

    # `is` not `==`: a returned 0/1 satisfies `==` and then flows into the ASL
    # as a non-boolean, where R4.2's fail-closed guard routes it to a human —
    # a correct-looking decision arriving in a shape the gate rejects.
    assert decision is expected_decision, (
        f"config={config!r} quality={quality!r} attempt={attempt!r}: expected {expected_decision!r}, "
        f"got {decision!r} — R2.4, the disjunction of the three inputs"
    )

    assert set(inputs) == set(_INPUT_KEYS), (
        f"the audit map's keys are {sorted(inputs)}, expected {sorted(_INPUT_KEYS)} — R3.5 names three inputs"
    )
    for key in _INPUT_KEYS:
        assert inputs[key] is expected_inputs[key], (
            f"audit map disagrees on {key!r}: expected {expected_inputs[key]!r}, got {inputs[key]!r} — "
            "R3.5, the recorded reason must be the reason that decided"
        )

    # The decision and its own map cannot disagree: this is what design §4.2's
    # "computed once, together" buys, and it is the assertion that would fail if
    # a future change derived the two separately.
    assert decision is any(inputs.values()), (
        f"decision {decision!r} is not the disjunction of its own recorded inputs {inputs!r} — R3.5"
    )

    # R2.4's second half, as its own statement: not-required implies all three
    # said not-required.
    if decision is False:
        assert not any(inputs.values()), (
            f"approval not required while {[k for k, v in inputs.items() if v]} required it — R2.4"
        )


@settings(max_examples=200, deadline=None)
@given(config=_decisions(), attempt=_regeneration_attempts())
def test_a_quality_gate_requiring_approval_forces_the_gate_irrespective_of_the_setting(config, attempt):
    """R2.1 — generating the setting axis is what proves "irrespective of".

    Code that failed its own tests cannot reach ``CreateInfrastructure``
    unreviewed on a pipeline whose routine approvals are switched off.

    Validates: Requirements 2.1
    """
    decision, inputs = _mod.determine_merged_approval_requirement(
        config_requires_approval=config,
        quality_requires_approval=True,
        regeneration_attempt=attempt,
    )
    assert decision is True, (
        f"the quality gate required approval and the policy returned {decision!r} with setting={config!r} — R2.1"
    )
    assert inputs["quality_gate"] is True, "the audit map does not record the quality gate as the reason — R3.5"


@settings(max_examples=200, deadline=None)
@given(quality=_decisions(), attempt=_regeneration_attempts())
def test_an_enabled_setting_forces_the_gate_irrespective_of_the_quality_gate(quality, attempt):
    """R2.2, and R1.1 — the reported defect, stated as a property.

    ``hello123`` carried ``code_generation_enabled = true`` and its clean run was
    never reviewed. Generating the quality axis is what proves the setting wins
    on a **passing** run, which is the case the live execution got wrong.

    Validates: Requirements 1.1, 2.2
    """
    decision, inputs = _mod.determine_merged_approval_requirement(
        config_requires_approval=True,
        quality_requires_approval=quality,
        regeneration_attempt=attempt,
    )
    assert decision is True, (
        f"the setting required approval and the policy returned {decision!r} with quality={quality!r} — R2.2/R1.1"
    )
    assert inputs["pipeline_config"] is True, "the audit map does not record the configuration as a reason — R3.5"


@settings(max_examples=200, deadline=None)
@given(config=_decisions(), quality=_decisions(), attempt=st.integers(min_value=1, max_value=10_000))
def test_a_regeneration_attempt_forces_the_gate_irrespective_of_the_other_two(config, quality, attempt):
    """R2.3 — the relocated escalation, with both other axes generated.

    This is the criterion that moves here from
    ``create_code.determine_approval_requirement`` (``create_code:331-334``),
    whose disabled-with-regeneration branch is pinned at
    ``test_no_keyword_self_approval.py:136`` until D2 deletes its home.
    Regenerated code is reviewer-flagged code.

    Validates: Requirements 2.3, 8.2
    """
    decision, inputs = _mod.determine_merged_approval_requirement(
        config_requires_approval=config,
        quality_requires_approval=quality,
        regeneration_attempt=attempt,
    )
    assert decision is True, (
        f"regeneration_attempt={attempt!r} did not force the gate with config={config!r} quality={quality!r} — R2.3"
    )
    assert inputs["regeneration"] is True, "the audit map does not record regeneration as a reason — R3.5"


def test_a_disabled_setting_with_a_clean_quality_gate_still_escalates_on_regeneration():
    """R2.3's hardest case — two of the three inputs say no, and it is still True.

    Setting explicitly ``False`` (the owner switched routine approvals off), the
    quality gate clean (nothing failed), and ``regeneration_attempt`` positive.
    This is the exact combination ``create_code:331-334`` decided today and the
    one an implementation that read the setting last, or short-circuited on it,
    would get wrong.

    Validates: Requirements 2.3, 8.2
    """
    decision, inputs = _mod.determine_merged_approval_requirement(
        config_requires_approval=False,
        quality_requires_approval=False,
        regeneration_attempt=1,
    )
    assert decision is True, "a regenerated run on a pipeline with approvals disabled must still be reviewed — R2.3"
    assert inputs == {"pipeline_config": False, "quality_gate": False, "regeneration": True}, (
        f"the audit map must name regeneration as the sole reason, got {inputs!r} — R3.5"
    )


def test_the_only_shape_that_skips_review_is_all_three_saying_no():
    """R1.3 and R2.4's residual — the one combination that reaches infrastructure.

    Setting explicitly ``false``, quality gate clean, attempt count ``0``. Every
    other shape routes to ``WaitForCodeApproval``.

    Validates: Requirements 1.3, 2.4
    """
    decision, inputs = _mod.determine_merged_approval_requirement(
        config_requires_approval=False,
        quality_requires_approval=False,
        regeneration_attempt=0,
    )
    assert decision is False, "the residual must not require approval — R1.3"
    assert inputs == {"pipeline_config": False, "quality_gate": False, "regeneration": False}, (
        f"expected all three inputs recorded as not requiring approval, got {inputs!r} — R3.5"
    )


@settings(max_examples=200, deadline=None)
@given(config=_decisions(), quality=_decisions(), attempt=_regeneration_attempts())
def test_the_input_map_values_are_strict_booleans(config, quality, attempt):
    """R3.5 — the map is JSON booleans, not truthy stand-ins.

    The map is written to the ``pipeline_runs`` state row and read by a human (or
    eventually by the run-detail step card, design §4.3). A ``Decimal(1)`` or a
    raw attempt count in the ``regeneration`` slot would render as something
    other than the boolean the audit claims to record, and would also be the
    signature of an implementation that stored the input instead of the
    comparison.

    Validates: Requirements 3.5
    """
    decision, inputs = _mod.determine_merged_approval_requirement(
        config_requires_approval=config,
        quality_requires_approval=quality,
        regeneration_attempt=attempt,
    )
    assert type(decision) is bool, f"the decision is {type(decision).__name__}, not bool — R3.5"
    for key, value in inputs.items():
        assert type(value) is bool, f"audit map value {key}={value!r} is {type(value).__name__}, not bool — R3.5"


def test_a_boolean_regeneration_attempt_is_read_as_its_integer_value():
    """The ``bool``-as-``int`` reading, pinned rather than left to be rediscovered.

    Unreachable from the state machine — ``CheckRegenerationAttempt``
    (``orchestration-stack.ts:774-782``) guards with
    ``And[IsPresent, IsNumeric, …]`` and a JSON boolean is not numeric — so this
    is not a supported input shape. It is pinned because ``bool`` subclasses
    ``int`` in Python, so the comparison silently succeeds, and the honest
    reading is the arithmetic one: ``True`` is attempt 1, ``False`` is attempt 0.
    The map still carries a strict boolean either way, which is the part that
    would otherwise corrupt R3.5's audit row.

    Validates: Requirements 2.3, 3.5
    """
    decision_true, inputs_true = _mod.determine_merged_approval_requirement(
        config_requires_approval=False,
        quality_requires_approval=False,
        regeneration_attempt=True,
    )
    assert decision_true is True, "True reads as attempt 1, which escalates — R2.3"
    assert inputs_true["regeneration"] is True
    assert type(inputs_true["regeneration"]) is bool

    decision_false, inputs_false = _mod.determine_merged_approval_requirement(
        config_requires_approval=False,
        quality_requires_approval=False,
        regeneration_attempt=False,
    )
    assert decision_false is False, "False reads as attempt 0, which does not escalate — R1.3"
    assert inputs_false["regeneration"] is False
    assert type(inputs_false["regeneration"]) is bool


def test_the_policy_is_keyword_only_so_the_three_inputs_cannot_be_transposed():
    """The signature, pinned: three same-typed arguments in a fixed order.

    Two of the three inputs are booleans with opposite meanings for the audit
    row, and a positional call site that transposed them would produce the right
    decision with the wrong recorded reason on exactly the runs where the reason
    matters. Keyword-only makes that call impossible rather than merely unlikely
    — design §4.2's signature, stated as an assertion so a later refactor cannot
    quietly relax it.

    Validates: Requirements 3.5
    """
    with pytest.raises(TypeError):
        _mod.determine_merged_approval_requirement(True, False, 0)
