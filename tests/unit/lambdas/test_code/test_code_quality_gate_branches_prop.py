# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p34-code-approval-gate-honors-config, Property 3: The quality gate's three escalating branches survive
"""Property 3 — the Code_Quality_Gate's three escalating branches survive.

*For any* ``(test_status, checks_run, checks_passed, review_verdict,
regeneration_analysis)``, ``determine_code_approval_requirement`` returns
``True`` whenever ``regeneration_analysis["max_attempts_reached"]`` is ``True``,
or ``test_status == "failed"``, or
``effective_verdict(review_verdict) == VERDICT_FAIL``.

**Validates: Requirements 2.5, 8.1**

Why this file exists
--------------------
p34 moves the approval decision into an Approval_Policy that merges the
pipeline's configuration with this function's answer (design §4.2). This
function itself is **not modified** by that change, and these assertions are
what make a later modification of it visible rather than inferred (R8.1).

The eleven existing direct call sites are eleven point examples of a
three-branch rule and none of them varies the verdict input systematically
(design §6). This property generalises them; it does not replace them — all
eleven, including ``test_honest_aggregation.py:189`` and ``:200``, keep passing
unmodified.

Expectations derive from **requirement 2.5**, not from running the current
code: "THE Code_Quality_Gate SHALL continue to report that approval is
required when ``regeneration_analysis.max_attempts_reached`` is ``true``, when
``test_status`` is ``failed``, or when the Effective_Verdict is ``fail``."

Deliberately one-directional
----------------------------
The ``False`` case is **not** asserted symmetrically. It is R3.1's subject (the
Quality_Verdict_Field keeps this function's unmerged meaning) and P28b R1b's
calibration (a scaffolding-only fail does not force the gate on verdict
grounds), and asserting it here would contradict design §4.7 row 2, which
amends ``test_review_gate_calibration.py:485-486`` to carry that subject on
``quality_gate_requires_approval``. See ``_no_assertion_on_the_false_case``
below for how the residual examples are accounted for instead of silently
passing.

Ledger provenance
-----------------
``why`` on ``test_code/lambda_function.py:692`` returns only ``LGR-8f3f9e1-1``
(**backfilled** — the p12a bare-except cleanup), whose body does not name this
function. No record governs this gate; ``requirements.md`` §"Ledger provenance
for this area" is the record. ``LGR-3e45e65-1`` (**template / ratified**) is why
this file does not re-pin ``AWS_REGION``: ``tests/unit/conftest.py`` assigns it
before any test module in this directory is imported.
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

# review_verdict is a real module on the same path — effective_verdict is read
# by the gate, not modified by p34, and is independently covered by
# test_review_verdict_attribution.py / test_review_verdict_contract.py.
from review_verdict import (
    ATTRIBUTION_SOURCE_DEFAULT,
    ATTRIBUTION_SOURCE_REVIEWER,
    ATTRIBUTION_TEMPLATE_SCAFFOLDING,
    ATTRIBUTION_TRANSFORM_BLOCK,
    NON_BLOCKING_SEVERITIES,
    VERDICT_FAIL,
    VERDICT_PASS,
    effective_verdict,
)

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
# Generators — constrained to the shapes the producers actually emit
# ---------------------------------------------------------------------------

#: ``aggregate_check_results`` emits "passed" / "failed" today. p32 2.21 left a
#: third status deliberately open, so a third value is generated as well: R2.5
#: names ``"failed"`` specifically, and a third status must not be read as one.
_TEST_STATUSES = ["passed", "failed", "not_verified"]

#: The severity vocabulary, kept **whole** by p43 (this file's R7.3 amendment).
#:
#: It feeds the rule-2/3/4 arms, where severity is irrelevant — those rules read
#: attribution only (``review_verdict.py:281-290``). Dropping ``minor`` globally
#: to fix the rule-1 arm would have lost a real shape: a defaulted-attribution
#: ``minor`` on a raw pass, which is a rule-4 case worth generating. Only the
#: rule-1 arm narrows, via :data:`_BLOCKING_SEVERITIES`.
_SEVERITIES = ["error", "major", "minor", "info"]

#: Blocking_Severity: the tokens that still fire rule 1 after p43's severity
#: floor, **derived** as the complement of the gate's own exemption set rather
#: than restated. Today exactly ``["error", "major", "info"]`` — ``error`` and
#: ``info`` are Unrecognised_Severity and block, which is the complement encoding
#: working as designed (p43 R1.5).
#:
#: **What narrowed and why** (``LGR-60c4985-6``, **template / ratified** —
#: amend, never delete, and keep the reversal legible). p43 R1.1/R1.2 put a
#: severity floor inside rule 1's condition (``review_verdict.py:294``), so a
#: reviewer ``transform_block`` finding at a Non_Blocking_Severity no longer
#: fails the gate. The rule-1 arm of :func:`_verdict` was drawing from
#: :data:`_SEVERITIES`, so it could construct that exact shape while claiming
#: ``effective=fail``, and the guard at the end of :func:`_verdict` — correctly —
#: refused it. **The property failed in its strategy, not in its assertion**
#: (p43 R7.3). Nothing was deleted: the drawn-away ``minor`` shape moved to the
#: rule-4 arm below, where it now generates an effective *pass*.
#:
#: **Derived rather than hardcoded, departing from design §4.7 row 10's literal
#: list.** Same value today, but a literal restates a snapshot of the gate's
#: vocabulary and this expresses the rule: design §4.3 chose the complement
#: encoding precisely so that widening the exemption set can never silently
#: strengthen or weaken the gate, and a derivation is that encoding rather than a
#: copy of its current output. A literal would re-break this guard the day
#: ``NON_BLOCKING_SEVERITIES`` grows a second tier — the same opaque failure
#: this amendment exists to fix. The usual objection to deriving, that a
#: vocabulary change then shrinks this generator's coverage silently, does not
#: apply: the set cannot change without failing
#: ``test_review_verdict_severity_vocabulary.py``'s exact-value pin on
#: ``sorted(NON_BLOCKING_SEVERITIES) == ["minor"]``, which is the one place that
#: change should be loud.
_BLOCKING_SEVERITIES = [s for s in _SEVERITIES if s not in NON_BLOCKING_SEVERITIES]

#: Non_Blocking_Severity, read off the real frozenset for the same
#: no-drift reason, following :data:`_NON_BLOCKING_SEVERITY` in
#: ``test_review_gate_calibration.py``. Feeds the rule-4 arm's retained shape.
_NON_BLOCKING_SEVERITIES = sorted(NON_BLOCKING_SEVERITIES)

# Both derived lists must be non-empty for `st.sampled_from` to be constructible.
# An exemption set that swallowed the whole vocabulary would otherwise surface as
# an opaque Hypothesis InvalidArgument from inside a draw.
assert _BLOCKING_SEVERITIES, "every token in _SEVERITIES is now exempt — the rule-1 arm cannot be built"
assert _NON_BLOCKING_SEVERITIES, "NON_BLOCKING_SEVERITIES is empty — the rule-4 retained shape cannot be built"

_CATEGORIES = ["Correctness", "Security", "Schema Compliance", "Performance"]


@st.composite
def _check_counts(draw) -> tuple[int, int]:
    """Executed-check counts, with ``passed <= run`` (P28 R10.6).

    The gate uses these only for its log line, but generating an impossible
    pair would put the property's inputs outside the producer's range.
    """
    checks_run = draw(st.integers(min_value=0, max_value=12))
    checks_passed = draw(st.integers(min_value=0, max_value=checks_run))
    return checks_run, checks_passed


@st.composite
def _issue(draw, *, attribution=None, attribution_source=None, severities=None) -> dict:
    """A canonical parsed issue (the ``parse_review_verdict`` output shape).

    ``severities`` narrows the severity draw for the one caller that needs it —
    the rule-1 arm, which must draw a Blocking_Severity for its claimed
    Effective_Verdict to be true (p43 R7.3). Defaults to the whole of
    :data:`_SEVERITIES`, so every other arm is unchanged.
    """
    issue = {
        "severity": draw(st.sampled_from(_SEVERITIES if severities is None else severities)),
        "description": draw(st.text(min_size=1, max_size=40)),
    }
    if attribution is not None:
        issue["attribution"] = attribution
    if attribution_source is not None:
        issue["attribution_source"] = attribution_source
    return issue


@st.composite
def _verdict(draw, *, effective: str | None = None) -> tuple[dict, str]:
    """A canonical verdict dict plus the Effective_Verdict it carries.

    The Effective_Verdict is fixed **by construction** rather than by asking
    ``effective_verdict`` what it thinks, so the ``fail`` half of Property 3's
    antecedent does not depend on the function that computes it:

    * fail, rule 1 — an explicit reviewer ``transform_block`` issue at a
      **Blocking_Severity**, at any raw verdict;
    * fail, rule 2 — raw ``fail`` with an issue that is not explicitly
      scaffolding-attributed;
    * pass, rule 3 — raw ``fail`` with every issue explicitly
      ``template_scaffolding`` (P28b R1b's calibration);
    * pass, rule 4 — raw ``pass`` with no *blocking* reviewer transform-block
      issue, which now includes one at a Non_Blocking_Severity.

    **p43 amendment (R1.3/R1.6/R7.3), under ``LGR-60c4985-6``.** Rule 1's arm
    reads "at any severity" no longer — see :data:`_BLOCKING_SEVERITIES` for what
    narrowed and why. The ``minor`` draws it gave up were not deleted: they moved
    to rule 4's arm, where a reviewer ``transform_block`` at a
    Non_Blocking_Severity is an effective **pass**. That is a route to rule 4
    this generator could not previously produce at all — the old rule-4 arm
    reached a pass only by *avoiding* reviewer transform-block attribution, so
    the "reviewer said transform_block, and it still passes" shape was outside
    the generated space in both directions.
    """
    if effective is None:
        effective = draw(st.sampled_from([VERDICT_PASS, VERDICT_FAIL]))

    categories = {name: draw(st.sampled_from(["pass", "fail"])) for name in draw(st.sets(st.sampled_from(_CATEGORIES)))}

    if effective == VERDICT_FAIL:
        rule = draw(st.sampled_from(["rule1", "rule2"]))
        if rule == "rule1":
            raw = draw(st.sampled_from([VERDICT_PASS, VERDICT_FAIL]))
            issues = draw(
                st.lists(
                    _issue(
                        attribution=ATTRIBUTION_TEMPLATE_SCAFFOLDING,
                        attribution_source=ATTRIBUTION_SOURCE_REVIEWER,
                    ),
                    max_size=2,
                )
            )
            # p43 R7.3: a Blocking_Severity, or this is not a rule-1 case at all.
            issues.append(
                draw(
                    _issue(
                        attribution=ATTRIBUTION_TRANSFORM_BLOCK,
                        attribution_source=ATTRIBUTION_SOURCE_REVIEWER,
                        severities=_BLOCKING_SEVERITIES,
                    )
                )
            )
        else:
            raw = VERDICT_FAIL
            # Not explicitly scaffolding-attributed: either the conservative
            # parser default, or no attribution fields at all.
            unattributed = draw(
                st.one_of(
                    _issue(
                        attribution=ATTRIBUTION_TRANSFORM_BLOCK,
                        attribution_source=ATTRIBUTION_SOURCE_DEFAULT,
                    ),
                    _issue(),
                )
            )
            issues = [unattributed]
    else:
        raw = draw(st.sampled_from([VERDICT_PASS, VERDICT_FAIL]))
        if raw == VERDICT_FAIL:
            # Rule 3: a fail is exempted only by explicit scaffolding
            # attribution on EVERY issue, and needs at least one issue.
            issues = draw(
                st.lists(
                    _issue(
                        attribution=ATTRIBUTION_TEMPLATE_SCAFFOLDING,
                        attribution_source=ATTRIBUTION_SOURCE_REVIEWER,
                    ),
                    min_size=1,
                    max_size=3,
                )
            )
        else:
            # Rule 4: a raw pass stays a pass, so long as no issue is a
            # *blocking* reviewer transform-block finding (which is rule 1).
            issues = draw(
                st.lists(
                    st.one_of(
                        _issue(
                            attribution=ATTRIBUTION_TEMPLATE_SCAFFOLDING,
                            attribution_source=ATTRIBUTION_SOURCE_REVIEWER,
                        ),
                        _issue(
                            attribution=ATTRIBUTION_TRANSFORM_BLOCK,
                            attribution_source=ATTRIBUTION_SOURCE_DEFAULT,
                        ),
                        # p43 R1.3/R1.6, additive: the retained shape. The
                        # reviewer DID attribute this to the transform block, so
                        # rule 1's attribution antecedent is satisfied and only
                        # its severity conjunct is not. Kept beside the three
                        # attribution-avoiding shapes so the one axis that
                        # separates a retained finding from a blocking one is
                        # visible here rather than inferred.
                        _issue(
                            attribution=ATTRIBUTION_TRANSFORM_BLOCK,
                            attribution_source=ATTRIBUTION_SOURCE_REVIEWER,
                            severities=_NON_BLOCKING_SEVERITIES,
                        ),
                        _issue(),
                    ),
                    max_size=3,
                )
            )

    verdict = {"verdict": raw, "issues": issues, "categories": categories}
    assert effective_verdict(verdict) == effective, (
        f"guard: generator claimed effective={effective} for {verdict!r} — the strategy has drifted "
        "from review_verdict's rules 1-4, so the property would be quantified over the wrong space"
    )
    return verdict, effective


@st.composite
def _regeneration_analysis(draw, *, max_attempts_reached: bool | None = None) -> dict:
    """The ``analyze_test_results_for_regeneration`` return shape (``:683-689``).

    ``max_attempts_reached`` is always present because that producer always
    emits it (``:686``); Property 3's antecedent reads it directly.
    """
    if max_attempts_reached is None:
        max_attempts_reached = draw(st.booleans())
    max_attempts = draw(st.integers(min_value=1, max_value=4))
    attempt = (
        draw(st.integers(min_value=max_attempts, max_value=max_attempts + 2))
        if max_attempts_reached
        else draw(st.integers(min_value=0, max_value=max_attempts - 1))
    )
    return {
        "regeneration_needed": draw(st.booleans()),
        "max_attempts_reached": max_attempts_reached,
        "regeneration_attempt": attempt,
        "max_attempts": max_attempts,
    }


@st.composite
def gate_inputs(draw, *, force_escalation: bool = False):
    """The gate's five arguments.

    ``force_escalation`` draws a non-empty subset of the three escalating
    conditions, so every example exercises the assertion rather than only the
    fraction that happens to escalate.
    """
    if force_escalation:
        which = draw(st.sets(st.sampled_from(["max_attempts", "failed", "verdict"]), min_size=1))
        max_attempts_reached = "max_attempts" in which
        test_status = "failed" if "failed" in which else draw(st.sampled_from(["passed", "not_verified"]))
        effective = VERDICT_FAIL if "verdict" in which else VERDICT_PASS
    else:
        max_attempts_reached = None
        test_status = draw(st.sampled_from(_TEST_STATUSES))
        effective = None

    checks_run, checks_passed = draw(_check_counts())
    verdict, effective_value = draw(_verdict(effective=effective))
    return {
        "test_status": test_status,
        "checks_run": checks_run,
        "checks_passed": checks_passed,
        "review_verdict": verdict,
        "regeneration_analysis": draw(_regeneration_analysis(max_attempts_reached=max_attempts_reached)),
        "_effective_verdict": effective_value,
    }


def _escalating_reasons(case: dict) -> list[str]:
    """R2.5's three reasons, evaluated on the generated inputs."""
    reasons = []
    if case["regeneration_analysis"]["max_attempts_reached"]:
        reasons.append("max_attempts_reached")
    if case["test_status"] == "failed":
        reasons.append("test_status == failed")
    if case["_effective_verdict"] == VERDICT_FAIL:
        reasons.append("effective_verdict == fail")
    return reasons


def _call(case: dict) -> bool:
    return _mod.determine_code_approval_requirement(
        test_status=case["test_status"],
        checks_run=case["checks_run"],
        checks_passed=case["checks_passed"],
        review_verdict=case["review_verdict"],
        regeneration_analysis=case["regeneration_analysis"],
    )


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(gate_inputs(force_escalation=True))
def test_any_escalating_condition_requires_approval(case):
    """Property 3, over inputs where at least one escalating reason applies.

    Validates: Requirements 2.5, 8.1
    """
    reasons = _escalating_reasons(case)
    assert reasons, "guard: force_escalation drew no escalating reason"
    event(f"escalating reasons: {sorted(reasons)}")

    assert _call(case) is True, (
        f"the quality gate did not require approval despite {reasons} "
        f"(test_status={case['test_status']!r}, "
        f"checks={case['checks_passed']}/{case['checks_run']}, "
        f"effective_verdict={case['_effective_verdict']!r}, "
        f"max_attempts_reached={case['regeneration_analysis']['max_attempts_reached']}) — R2.5"
    )


@settings(max_examples=200, deadline=None)
@given(gate_inputs())
def test_any_escalating_condition_requires_approval_over_the_whole_input_space(case):
    """Property 3, unrestricted — the antecedent is not forced.

    One-directional on purpose: when no escalating reason applies, this test
    asserts **nothing** about the return value. The ``False`` case belongs to
    R3.1 and P28b R1b (see the module docstring), so the residual examples are
    recorded as a Hypothesis event instead of being silently dropped.

    Validates: Requirements 2.5, 8.1
    """
    reasons = _escalating_reasons(case)
    if not reasons:
        event("no escalating reason — the False case is R3.1's subject, not asserted here")
        return

    event(f"escalating reasons: {sorted(reasons)}")
    assert _call(case) is True, (
        f"the quality gate did not require approval despite {reasons} "
        f"(test_status={case['test_status']!r}, "
        f"effective_verdict={case['_effective_verdict']!r}, "
        f"max_attempts_reached={case['regeneration_analysis']['max_attempts_reached']}) — R2.5"
    )
