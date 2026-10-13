# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p43 task 6.3 — Properties 10 and 13 over the gate's consumers and pass-throughs.

What this file owns
-------------------
Two properties from design §6, and nothing else:

* **Property 10** — no regeneration and no approval on an effective pass
  (R3.2, R3.3), over ``analyze_test_results_for_regeneration``
  (``lambda_function.py:1465``) and ``determine_code_approval_requirement``
  (``:1614``).
* **Property 13** — the Raw_Verdict and the parser's attribution default are
  passed through (R8.1, R8.2), over ``verdict_to_parsed_results``
  (``review_verdict.py:383``) and ``parse_review_verdict`` (``:156``).

Task 6.1's three amended parametrizations
(``test_review_gate_calibration.py:145``, ``:209``, ``:284``) are the point
examples of Property 10's ``minor``-on-a-raw-pass cell; this generalises them
over every route to an Effective_Verdict of ``pass`` and does not replace them.
The ``ai_review_verdict`` check arm of R3.1 stays with task 5.1's Property 2,
and the retained **count** in the detail is Property 7's subject (task 10.2) —
this file asserts nothing about the detail string, which does not carry the
count yet.

Why these two are one file
--------------------------
Both are the same kind of claim: **the floor changed rule 1 and nothing else.**
Property 10 says the three consumers agree with the gate rather than each
carrying their own reading of a severity, and Property 13 says the two values
p43 must not touch — the reviewer's own ``verdict`` token, and the parser's
conservative attribution default — still arrive unchanged. do-NOT 6 and
do-NOT 7 quantified.

Property 13's second arm carries the load-bearing half. The parser defaults an
absent or unrecognised attribution to ``("transform_block", "default")``
(``review_verdict.py:230-243``), and rule 1 deliberately does not match a
*defaulted* attribution — so a defaulted issue reaches rule 2, not rule 1
(``requirements.md`` do-NOT 7). Design §4.1 rejected "loudly" the alternative
implementation of this spec that would have applied the floor in the parser by
downgrading a ``minor`` finding's ``attribution_source`` to ``default``. That
alternative would pass every gate property in this suite and be visible only
here: it corrupts provenance three consumers display. Arm B is what makes it
fail.

How the two consumer functions are reached
------------------------------------------
Through the dynamic import this directory already uses
(``test_review_gate_calibration.py:64-95``, and identically in
``test_code_quality_gate_branches_prop.py:83-108``): stub the heavy /
AWS-touching modules into ``sys.modules`` with ``patch.dict``, pop any cached
``lambda_function``, import it, and call ``_mod.analyze_test_results_for_regeneration``.
The autouse ``_ensure_correct_module`` fixture re-binds ``sys.modules`` per test
because several Lambda directories expose a module of that name. Reused rather
than reinvented — a second way in would be a second thing to keep working.

**That import is also why codegraph is wrong about these two symbols, and the
figures below are recorded with the correction rather than the flag.**

Blast radius and test coverage, per ``codegraph explore``
--------------------------------------------------------
* ``analyze_test_results_for_regeneration`` — 1 caller (the handler);
  codegraph reports **no covering tests**. That is a **false negative**: seven
  test files call it (``test_review_gate_calibration.py``,
  ``test_honest_aggregation.py``, ``test_review_gate_honesty.py``,
  ``test_executed_check_counts.py``,
  ``test_not_verified_classification_prop.py``, and two under
  ``tests/integration/pipeline_flow/``), every one of them through the
  ``_mod.<name>`` attribute access above. No static edge expresses an attribute
  lookup on a module object bound at import time under ``patch.dict``.
* ``determine_code_approval_requirement`` — 1 caller; same false negative, nine
  calling test files.
* ``verdict_to_parsed_results`` — 11 callers in ``lambda_function.py``; four
  covering test files, correctly found. It is imported by name, so the edge is
  visible. **That contrast is the evidence for the diagnosis** rather than a
  guess about it: the two functions codegraph cannot see are exactly the two
  reached by attribute access, and the one it can see is the one imported
  normally.

So no characterization test is owed here. The distinction matters because the
rule is that modifying *untested* code needs characterization tests first, and
acting on the flag would have written a third copy of coverage that already
exists twice.

Fixtures come through the real parser
------------------------------------
Every verdict in this file is built by ``parse_review_verdict`` from a JSON
document, matching ``test_retained_findings_helpers_prop.py`` and
``test_review_verdict_severity_floor_prop.py``: ``attribution_source`` is then
whatever the parser decided, not what this file typed. That is half of rule 1's
antecedent, and it is exactly what Property 13 arm B is *about*, so a hand-set
``attribution_source`` would make both properties self-fulfilling.

The generator space, and where it is bounded
--------------------------------------------
Property 10 draws the three documented routes to an Effective_Verdict of
``pass`` — rule 4 with no reviewer transform-block finding, rule 4 with a
retained one (p43's new route), and rule 3's scaffolding-only exemption on a raw
``fail`` — over both sides of the severity floor, the casings the parser folds,
unrecognised attributions and absent ones.

Two inputs are deliberately **held** rather than drawn, because R3.2 and R3.3
are about the *verdict grounds* and each of the others has an escalating branch
of its own:

* ``test_status`` is held at ``"passed"`` — ``"failed"`` requires approval by
  its own branch (``lambda_function.py:1670-1672``) and adds its own
  regeneration trigger (``:1528-1529``), regardless of any verdict.
* ``regeneration_attempt`` is drawn strictly **in bounds**.
  ``max_attempts_reached`` is computed unconditionally (``:1538``), so an
  out-of-bounds attempt requires approval through the first branch (``:1663``)
  even when nothing triggered. :func:`test_an_out_of_bounds_attempt_requires_approval_for_its_own_reason`
  pins that boundary as an example instead of letting it falsify a property it
  is not about.

Check counts ARE drawn, held consistent with the status (``checks_passed ==
checks_run``), to show the two decisions do not read them on this path — they
reach only the log and summary strings.

Non-vacuity guards
------------------
Both properties have antecedents a careless generator can leave unsatisfied,
and both have a shape that would hold while measuring nothing. Named:

* :func:`test_property_10_reaches_all_three_effective_pass_routes` — all three
  routes are reachable, including the retained-finding one that only exists
  after this spec.
* :func:`test_property_10_is_not_vacuous_an_effective_fail_triggers_both` — the
  control. Both consumers DO escalate on an effective fail, so Property 10 is
  not two functions that never escalate at all.
* :func:`test_property_13_reaches_both_raw_verdicts` and
  :func:`test_the_raw_verdict_survives_a_gate_that_says_fail` — the second is
  the case where a pass-through could be conflated with a derivation: the gate
  returns ``fail`` on a raw ``pass``, and ``verdict`` must still read ``pass``
  while ``recommendation`` reads ``REJECT``.
* :func:`test_every_seed_attribution_value_defaults_conservatively` — the ten
  values arm B's generator is seeded from, as named examples.

NO FIXTURES
-----------
No fixture in ``tests/fixtures/conftest.py`` applies: there is no AWS client to
mock (the modules that would reach one are stubbed at import) and no Lambda
environment to arrange beyond the account id. The autouse fixture here is
module-identity bookkeeping, not test data. ``AWS_REGION`` is not re-pinned —
``tests/unit/conftest.py`` assigns it before any module in this directory is
imported (``LGR-3e45e65-1``, **template / ratified**).

Ledger provenance
-----------------
``why`` on ``review_verdict.py`` returns only the 634-file working-tree snapshot
with ``records: []`` — **no ratified record governs the gate or its parser**
(design §5, C10). ``why`` on ``lambda_function.py`` adds ``LGR-42dbdb1-1/-2``
(the error-path state row) and ``LGR-b2f79ef-2/-8`` (the check-state encoding),
none of which names these two consumers. That hole is task 15.1's to fill; it is
recorded here as a gap, not as an all-clear. The records that DO bear on this
file: ``LGR-28b4d75-5`` (**template**) is why nothing here is written from the
functions' current output — Property 13's expected values come from R8.1/R8.2
and the parser's documented default, not from running it; and
``LGR-60c4985-6`` (**template / ratified**) is the amend-don't-delete discipline
task 6.1 and 6.2 worked under, which this file generalises rather than restates.
"""

from __future__ import annotations

import json
import os
import string
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

# Account id is not pinned by tests/unit/conftest.py (only the region is —
# LGR-3e45e65-1), and env_utils is stubbed below in any case.
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Imported after the sys.path insert above, which is what makes it resolvable.
# review_verdict.py is pure stdlib (its module docstring, :56-58), so it needs
# none of the stubbing below.
from review_verdict import (
    ALLOWED_ATTRIBUTIONS,
    ATTRIBUTION_SOURCE_DEFAULT,
    ATTRIBUTION_SOURCE_REVIEWER,
    ATTRIBUTION_TEMPLATE_SCAFFOLDING,
    ATTRIBUTION_TRANSFORM_BLOCK,
    NON_BLOCKING_SEVERITIES,
    VERDICT_FAIL,
    VERDICT_PASS,
    effective_verdict,
    parse_review_verdict,
    verdict_to_parsed_results,
)

# Stub heavy / AWS-touching imports so the handler module loads in isolation.
# Same set as the sibling files in this directory; the module-level cache pop is
# the house pattern for lambda_function cross-contamination (several Lambda dirs
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


#: The verdict-grounds regeneration trigger, as the producer spells it
#: (``lambda_function.py:1508``). Property 10's "adds no verdict trigger" is
#: asserted against this exact string, which is also what task 6.1's amended
#: example inverts at ``test_review_gate_calibration.py:373``.
_VERDICT_TRIGGER = "review verdict: fail"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Sentinel: the ``attribution`` key is absent from the document entirely.
#: Distinct from a present ``None``, which is one of the ten seed values below —
#: both default, and the parser reaches them by different routes (``.get``
#: returning ``None`` versus a non-string value), so both are drawn.
_OMIT = object()

#: Non-empty after the parser's ``strip()``, which is what
#: ``_validate_verdict_document`` requires of a description (``:222-223``).
_ISSUE_TEXT = (
    st.text(alphabet=string.ascii_letters + string.digits + " .,", min_size=1, max_size=30).map(str.strip).filter(bool)
)

#: The Non_Blocking_Severity tokens, read off the real frozenset rather than
#: restated, so nothing here drifts if the set ever grows a second tier.
_NON_BLOCKING_SEVERITY = st.sampled_from(sorted(NON_BLOCKING_SEVERITIES))

#: Tokens outside the non-blocking set: the two recommended blocking severities
#: and four Unrecognised_Severity tokens, two of which (``nit``, ``info``) READ
#: as trivial. Under the complement encoding a token nobody enumerated blocks,
#: which is the half of R1.2 a reader is most likely to get wrong.
_BLOCKING_SEVERITY = st.sampled_from(("critical", "major", "nit", "info", "blocker", "error"))

#: Spellings the parser normalises INTO the non-blocking set (``:225``). Built
#: from the real set members rather than hardcoded so this follows the
#: vocabulary. These are the draws that would silently become blocking if the
#: parser stopped normalising.
_NORMALISES_TO_NON_BLOCKING = st.builds(
    lambda token, upper_first, pad_left, pad_right: (
        (" " * pad_left) + (token[:1].upper() + token[1:] if upper_first else token) + (" " * pad_right)
    ),
    st.sampled_from(sorted(NON_BLOCKING_SEVERITIES)),
    st.booleans(),
    st.integers(min_value=0, max_value=2),
    st.integers(min_value=0, max_value=2),
)

#: The whole severity axis as a REVIEWER would spell it, both sides of the floor.
_ANY_SEVERITY = st.one_of(_NON_BLOCKING_SEVERITY, _BLOCKING_SEVERITY, _NORMALISES_TO_NON_BLOCKING)

#: Attribution inputs the parser recognises as an explicit reviewer
#: transform-block attribution, in the casings ``:236-238`` folds.
_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION = st.sampled_from(
    ("transform_block", "TRANSFORM_BLOCK", " transform_block ", "Transform_Block")
)

#: Attribution inputs the parser recognises as an explicit reviewer scaffolding
#: attribution — rules 2/3's vocabulary, untouched by this spec (``:285-289``).
_SCAFFOLDING_ATTRIBUTION = st.sampled_from(
    ("template_scaffolding", "Template_Scaffolding", "TEMPLATE_SCAFFOLDING", "  template_scaffolding  ")
)

#: **Property 13 arm B's seed cases: the ten values parametrized at**
#: ``test_review_verdict_attribution.py:78-88`` **(that file's**
#: ``test_garbage_attribution_defaults_to_transform_block``), restated here so
#: arm B's space provably contains them. Eight are listed; the remaining two are
#: the mutable ``["template_scaffolding"]`` and ``{}``, constructed per draw in
#: :data:`_UNRECOGNISED_ATTRIBUTION` rather than shared across examples.
#:
#: They span the four ways a value fails to normalise: a plausible-but-wrong
#: token, an empty or whitespace string, the recognised token with a space
#: instead of an underscore, and four non-string types. ``True`` is there
#: because ``isinstance(True, int)`` and a truthiness test would take it for a
#: value.
_SEED_ATTRIBUTION_VALUES: tuple[Any, ...] = (
    "scaffolding",
    "boilerplate",
    "",
    "   ",
    "template scaffolding",
    None,
    42,
    True,
)

#: The full ten, for the named parametrized companion. Order follows the source
#: parametrization so the two lists can be read side by side.
_TEN_SEED_ATTRIBUTION_VALUES: tuple[Any, ...] = (*_SEED_ATTRIBUTION_VALUES, ["template_scaffolding"], {})


def _normalises_to_a_recognised_attribution(value: Any) -> bool:
    """The parser's own recognition test, written out at the test's level.

    Deliberately not an import of anything private: this is the condition at
    ``review_verdict.py:239-241`` stated from the module docstring's
    ``attribution`` bullet, so arm B's filter cannot agree with the parser by
    construction.
    """
    return isinstance(value, str) and value.strip().lower() in ALLOWED_ATTRIBUTIONS


#: Anything a reviewer's JSON could carry in ``attribution`` that does NOT
#: normalise to a recognised token: the eight scalar seeds, a fresh list and a
#: fresh dict per draw, plus generated JSON-serialisable values filtered by the
#: predicate above. Generated text is included so "unrecognised" is not just a
#: list someone thought of — the filter is what keeps ``transform_block`` and
#: its casings out.
_UNRECOGNISED_ATTRIBUTION = st.one_of(
    st.sampled_from(_SEED_ATTRIBUTION_VALUES),
    st.builds(lambda: ["template_scaffolding"]),
    st.builds(dict),
    st.one_of(
        st.text(max_size=20),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.booleans(),
        st.none(),
        st.lists(st.text(max_size=8), max_size=3),
    ).filter(lambda v: not _normalises_to_a_recognised_attribution(v)),
)

#: Every attribution input whose parsed result is NOT a
#: Reviewer_Transform_Block_Finding: the unrecognised values (which default to
#: ``("transform_block", "default")``, which rule 1 does not match), the
#: scaffolding spellings, and an omitted key.
_NON_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION = st.one_of(
    _UNRECOGNISED_ATTRIBUTION,
    _SCAFFOLDING_ATTRIBUTION,
    st.just(_OMIT),
)

#: Every attribution input, either kind — Property 13 arm A's space.
_ANY_ATTRIBUTION = st.one_of(_NON_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION, _REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION)


def _issue_doc(description: str, severity: str, attribution: Any = _OMIT) -> dict[str, Any]:
    """A raw reviewer issue as it appears in the model's JSON, pre-parse."""
    doc: dict[str, Any] = {"severity": severity, "description": description}
    if attribution is not _OMIT:
        doc["attribution"] = attribution
    return doc


def _parse(raw_verdict: str, issue_docs: list[dict[str, Any]], categories: dict[str, str] | None = None) -> dict:
    """Build a verdict document and run it through the REAL parser."""
    return parse_review_verdict(
        json.dumps({"verdict": raw_verdict, "issues": issue_docs, "categories": categories or {}})
    )


def _is_reviewer_transform_block_finding(issue: dict[str, Any]) -> bool:
    """Rule 1's attribution antecedent, spelled out at the test's own level.

    Not an import of ``review_verdict._is_reviewer_transform_block`` — the
    module-private predicate is the implementation's, and reading it here would
    make the generators' constraints agree with the gate by construction. This
    is ``requirements.md``'s Glossary definition of
    Reviewer_Transform_Block_Finding, written out.
    """
    return (
        issue.get("attribution") == ATTRIBUTION_TRANSFORM_BLOCK
        and issue.get("attribution_source") == ATTRIBUTION_SOURCE_REVIEWER
    )


@st.composite
def _effective_pass_verdicts(draw) -> tuple[str, dict[str, Any]]:
    """Property 10's space: every documented route to an Effective_Verdict of ``pass``.

    Returns ``(route, verdict)`` so the property can report which route a
    counterexample came from — the three fail for different reasons and the
    distinction is most of the diagnosis.

    * ``rule_4_plain`` — a raw ``pass`` with no Reviewer_Transform_Block_Finding
      at all. The pre-p43 effective pass.
    * ``rule_4_retained`` — a raw ``pass`` whose reviewer transform-block
      findings all carry a severity that normalises INTO
      ``NON_BLOCKING_SEVERITIES``. **This route only exists after this spec**,
      and it is the one R3.2/R3.3 are about.
    * ``rule_3_scaffolding_only`` — a raw ``fail`` every issue of which is
      explicitly ``template_scaffolding``. P28b R1b's calibration, which p43
      leaves alone; drawn so Property 10 cannot silently become a claim about
      raw-pass verdicts only.
    """
    route = draw(st.sampled_from(("rule_4_plain", "rule_4_retained", "rule_3_scaffolding_only")))

    if route == "rule_3_scaffolding_only":
        # A raw fail with no issues is rejected by the parser (:246-249), and
        # rule 3 needs every issue explicitly scaffolding-attributed. Severity
        # is drawn from the whole axis: rules 2/3 read attribution only, so a
        # `critical` scaffolding issue is still exempt.
        docs = draw(
            st.lists(
                st.builds(_issue_doc, _ISSUE_TEXT, _ANY_SEVERITY, _SCAFFOLDING_ATTRIBUTION),
                min_size=1,
                max_size=4,
            )
        )
        return route, _parse(VERDICT_FAIL, docs)

    if route == "rule_4_plain":
        docs = draw(
            st.lists(
                st.builds(_issue_doc, _ISSUE_TEXT, _ANY_SEVERITY, _NON_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION),
                max_size=4,
            )
        )
        return route, _parse(VERDICT_PASS, docs)

    # rule_4_retained: at least one reviewer transform-block finding, and the
    # constraint is per-issue — an issue drawn with a recognised transform-block
    # attribution takes a severity that normalises into the non-blocking set;
    # any other issue takes any severity, because rule 1 cannot see it.
    focus = _issue_doc(
        draw(_ISSUE_TEXT),
        draw(_NORMALISES_TO_NON_BLOCKING),
        draw(_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION),
    )
    companions: list[dict[str, Any]] = []
    for attribution, free_severity, non_blocking_severity in draw(
        st.lists(st.tuples(_ANY_ATTRIBUTION, _ANY_SEVERITY, _NORMALISES_TO_NON_BLOCKING), max_size=3)
    ):
        recognised_transform_block = _normalises_to_a_recognised_attribution(attribution) and (
            attribution.strip().lower() == ATTRIBUTION_TRANSFORM_BLOCK
        )
        severity = non_blocking_severity if recognised_transform_block else free_severity
        companions.append(_issue_doc(draw(_ISSUE_TEXT), severity, attribution))
    # Position matters — the gate loops over issues — so the focus issue is
    # spliced in at a drawn index rather than always leading.
    index = draw(st.integers(min_value=0, max_value=len(companions)))
    return route, _parse(VERDICT_PASS, [*companions[:index], focus, *companions[index:]])


@st.composite
def _any_parseable_verdict(draw) -> dict[str, Any]:
    """Property 13 arm A's space: anything the parser will emit.

    Unconstrained on the severity and attribution axes — arm A's claim is about
    the ``verdict`` token surviving the adapter, which must hold whatever the
    gate then decides. ``categories`` is drawn because the adapter reads it
    (``:397-400``) and a shared mutable default would hide an aliasing bug.
    """
    raw_verdict = draw(st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))
    docs = draw(
        st.lists(
            st.builds(_issue_doc, _ISSUE_TEXT, _ANY_SEVERITY, _ANY_ATTRIBUTION),
            # A raw fail with no issues is rejected by the parser (:246-249).
            min_size=1 if raw_verdict == VERDICT_FAIL else 0,
            max_size=4,
        )
    )
    categories = draw(
        st.dictionaries(
            st.sampled_from(("Correctness", "Security", "Performance")),
            st.sampled_from(("pass", "fail")),
            max_size=3,
        )
    )
    return _parse(raw_verdict, docs, categories)


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------


# Feature: p43-review-gate-severity-floor, Property 10: No regeneration or
# approval on an effective pass.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    route_and_verdict=_effective_pass_verdicts(),
    checks_run=st.integers(min_value=1, max_value=6),
    max_attempts=st.integers(min_value=1, max_value=4),
    attempt=st.integers(min_value=0, max_value=3),
)
def test_no_regeneration_or_approval_on_an_effective_pass(route_and_verdict, checks_run, max_attempts, attempt):
    """**Validates: Requirements 3.2, 3.3** — Property 10.

    For any verdict whose Effective_Verdict is ``pass`` — by any of the three
    documented routes, at any severity on either side of the floor, with any
    attribution including unrecognised and absent — with ``test_status`` held at
    ``"passed"``:

    * ``analyze_test_results_for_regeneration`` adds **no verdict trigger**
      (R3.2), so ``regeneration_needed`` is ``False``; and
    * ``determine_code_approval_requirement`` returns ``False`` (R3.3).

    Both are asserted, and the trigger is asserted by its own string rather than
    only through ``regeneration_needed``. That distinction is the point: R3.2
    says "add no regeneration trigger **on verdict grounds**", and
    ``regeneration_needed`` is also ``False`` for two reasons unrelated to the
    verdict — an out-of-bounds attempt, and a clean check status. Reading only
    the boolean would let a spurious verdict trigger hide behind a bound.

    R3.5's other half — that the finding is retained rather than discarded — is
    task 6.1's, at ``test_review_gate_calibration.py:373``; this file does not
    re-assert it.

    Expected from R3.2 and R3.3, not from what the consumers currently return:
    both requirements say "no ... on verdict grounds", and the two functions are
    checked against that wording. ``LGR-28b4d75-5`` (**template**) is why that
    matters — a test written from the producer's own output cannot detect the
    producer drifting.

    The two held inputs and the drawn-but-irrelevant counts are explained in the
    module docstring's "The generator space" section; the out-of-bounds boundary
    is :func:`test_an_out_of_bounds_attempt_requires_approval_for_its_own_reason`.
    """
    route, verdict = route_and_verdict
    event(f"route: {route}")

    # The antecedent, asserted rather than assumed: the generator claims an
    # effective pass, and if it is wrong this property would be testing the
    # escalating direction (which is Property 3's subject in
    # test_code_quality_gate_branches_prop.py) while looking like it passed.
    assert effective_verdict(verdict) == VERDICT_PASS, (
        f"route {route!r} produced a verdict whose Effective_Verdict is 'fail', so Property 10's "
        f"antecedent is unsatisfied and it is not testing R3.2/R3.3: {verdict!r}"
    )
    # Clamped strictly in bounds, so `max_attempts_reached` is False and cannot
    # supply the approval answer through :1663 for a reason unrelated to the
    # verdict. Clamped rather than filtered so no draw is discarded.
    regeneration_attempt = min(attempt, max_attempts - 1)

    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="passed",
        checks_run=checks_run,
        checks_passed=checks_run,
        review_verdict=verdict,
        regeneration_attempt=regeneration_attempt,
        max_attempts=max_attempts,
    )

    # The bound took the drawn value, so "in bounds" above is really in bounds.
    # R8.3 keeps the bound machinery itself out of scope — this is a guard on
    # this test's own premise, not a claim about the bound.
    assert analysis["max_attempts"] == max_attempts
    assert analysis["max_attempts_reached"] is False, (
        f"attempt {regeneration_attempt} against bound {max_attempts} should be in bounds; the "
        "approval answer below would otherwise come from the max-attempts branch."
    )

    issues_found = "\n".join(analysis["feedback_context"]["issues_found"])
    assert _VERDICT_TRIGGER not in issues_found, (
        f"an effective pass added the verdict-grounds regeneration trigger {_VERDICT_TRIGGER!r} "
        f"(R3.2 forbids it). Route {route!r}, verdict {verdict!r}. The most likely cause is a "
        "consumer reading the raw verdict, or reading severity itself, rather than consuming "
        "effective_verdict."
    )
    assert analysis["regeneration_needed"] is False, (
        f"an effective pass with a passing test status triggered regeneration (R3.2). Route "
        f"{route!r}, triggers {analysis['feedback_context']['issues_found']!r}."
    )

    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=checks_run,
        checks_passed=checks_run,
        review_verdict=verdict,
        regeneration_analysis=analysis,
    )
    assert required is False, (
        f"an effective pass required approval on verdict grounds (R3.3). Route {route!r}, verdict "
        f"{verdict!r}. test_status is 'passed' and the attempt is in bounds, so the verdict branch "
        "(lambda_function.py:1677) is the only one that could have fired."
    )


def test_property_10_reaches_all_three_effective_pass_routes():
    """Property 10's generator reaches all three routes, and they are distinct.

    Three fixed verdicts, one per route, each shown to be an effective pass and
    each shown to be the route it claims. Without this the property could be one
    route drawn three ways — and the interesting one, ``rule_4_retained``, is the
    only route that did not exist before this spec, so its absence would make
    Property 10 a restatement of P28b behaviour.

    The retained route is identified by the parsed issue's own triple rather than
    by calling a helper: ``('transform_block', 'reviewer')`` at a severity inside
    ``NON_BLOCKING_SEVERITIES`` is exactly the shape rule 1 now exempts. The
    helpers' semantics are Property 12's ground
    (``test_retained_findings_helpers_prop.py``) and are not re-asserted here.
    """
    non_blocking_token = sorted(NON_BLOCKING_SEVERITIES)[0]

    rule_4_plain = _parse(VERDICT_PASS, [_issue_doc("A naming nit.", "critical")])
    assert effective_verdict(rule_4_plain) == VERDICT_PASS
    # Defaulted, not reviewer-attributed — so rule 1 cannot see it even at
    # `critical`, and this route reaches the gate through rule 4.
    assert rule_4_plain["issues"][0]["attribution_source"] == ATTRIBUTION_SOURCE_DEFAULT
    assert not _is_reviewer_transform_block_finding(rule_4_plain["issues"][0])

    rule_4_retained = _parse(
        VERDICT_PASS,
        [_issue_doc("Transform drops null rows.", non_blocking_token, "transform_block")],
    )
    assert effective_verdict(rule_4_retained) == VERDICT_PASS
    retained_issue = rule_4_retained["issues"][0]
    assert _is_reviewer_transform_block_finding(retained_issue), (
        "the parser did not record ('transform_block', 'reviewer') here, so this route is not the "
        f"retained one and Property 10's new cell is unreached: {retained_issue!r}"
    )
    assert retained_issue["severity"] in NON_BLOCKING_SEVERITIES

    rule_3_scaffolding_only = _parse(
        VERDICT_FAIL,
        [_issue_doc("Glue boilerplate re-creates the SparkSession.", "major", "template_scaffolding")],
    )
    assert effective_verdict(rule_3_scaffolding_only) == VERDICT_PASS
    assert rule_3_scaffolding_only["issues"][0]["attribution"] == ATTRIBUTION_TEMPLATE_SCAFFOLDING
    assert rule_3_scaffolding_only["issues"][0]["attribution_source"] == ATTRIBUTION_SOURCE_REVIEWER
    # The three routes really are three: two raw verdicts, and two different
    # attributions on the raw-pass side.
    assert rule_3_scaffolding_only["verdict"] == VERDICT_FAIL
    assert rule_4_plain["verdict"] == rule_4_retained["verdict"] == VERDICT_PASS


def test_property_10_is_not_vacuous_an_effective_fail_triggers_both():
    """The control: both consumers DO escalate on an effective fail.

    Property 10 is a pair of negative claims, and two functions that never
    trigger and never require approval would satisfy it completely. So the same
    fixture with one severity flipped to a Blocking_Severity — the minimal
    change that makes rule 1 fire — is asserted to produce the verdict trigger
    and the approval requirement, with every other input identical.

    This mirrors the attribution-flipped control at
    ``test_review_gate_calibration.py:1002-1007`` and the severity flip Property
    2 uses; it is the same technique applied to the consumers rather than to the
    gate.
    """
    non_blocking_token = sorted(NON_BLOCKING_SEVERITIES)[0]
    description = "Transform drops null rows."

    def _run(severity: str) -> tuple[dict, bool]:
        verdict = _parse(VERDICT_PASS, [_issue_doc(description, severity, "transform_block")])
        analysis = _mod.analyze_test_results_for_regeneration(
            test_status="passed",
            checks_run=4,
            checks_passed=4,
            review_verdict=verdict,
            regeneration_attempt=0,
            max_attempts=2,
        )
        required = _mod.determine_code_approval_requirement(
            test_status="passed",
            checks_run=4,
            checks_passed=4,
            review_verdict=verdict,
            regeneration_analysis=analysis,
        )
        return analysis, required

    retained_analysis, retained_required = _run(non_blocking_token)
    assert _VERDICT_TRIGGER not in "\n".join(retained_analysis["feedback_context"]["issues_found"])
    assert retained_analysis["regeneration_needed"] is False
    assert retained_required is False

    blocking_analysis, blocking_required = _run("critical")
    assert _VERDICT_TRIGGER in "\n".join(blocking_analysis["feedback_context"]["issues_found"]), (
        "flipping the severity to 'critical' did not add the verdict trigger, so Property 10 holds "
        "over a space where nothing ever triggers and it is measuring nothing."
    )
    assert blocking_analysis["regeneration_needed"] is True
    assert blocking_required is True, (
        "flipping the severity to 'critical' did not require approval, so Property 10's False claim is vacuous."
    )


def test_an_out_of_bounds_attempt_requires_approval_for_its_own_reason():
    """The boundary Property 10 holds ``regeneration_attempt`` inside, and why.

    ``max_attempts_reached`` is computed unconditionally
    (``lambda_function.py:1538``), not only when something triggered, so an
    effective pass at or past the bound still reports it — and
    ``determine_code_approval_requirement``'s **first** branch (``:1663``)
    returns ``True`` on it, ahead of the verdict branch entirely.

    That is correct behaviour and is P28b R1.6's bound machinery, which R8.3
    keeps out of this spec's scope. It is pinned here as an example so the
    boundary is documented rather than discovered as a shrunk counterexample,
    and so the in-bounds draw in Property 10 reads as a deliberate constraint
    rather than a convenient one.

    Note what is NOT true here: no verdict trigger is added. Approval is
    required for the attempt-bound reason, and R3.2 still holds.
    """
    verdict = _parse(
        VERDICT_PASS,
        [_issue_doc("Transform drops null rows.", sorted(NON_BLOCKING_SEVERITIES)[0], "transform_block")],
    )
    assert effective_verdict(verdict) == VERDICT_PASS

    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=verdict,
        regeneration_attempt=2,
        max_attempts=2,
    )
    assert analysis["max_attempts_reached"] is True
    assert analysis["regeneration_needed"] is False
    # R3.2 holds regardless of the bound: the verdict added nothing.
    assert _VERDICT_TRIGGER not in "\n".join(analysis["feedback_context"]["issues_found"])

    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=verdict,
        regeneration_analysis=analysis,
    )
    assert required is True, (
        "an out-of-bounds attempt must still require approval (P28b R1.6, the first branch at "
        "lambda_function.py:1663). If this returned False the bound stopped being a fail-safe, "
        "which R8.3 says this spec leaves alone."
    )


# ---------------------------------------------------------------------------
# Property 13, arm A — the Raw_Verdict is passed through
# ---------------------------------------------------------------------------


# Feature: p43-review-gate-severity-floor, Property 13: The raw verdict and the
# attribution default are passed through.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(verdict=_any_parseable_verdict())
def test_the_adapter_passes_the_raw_verdict_through_unchanged(verdict):
    """**Validates: Requirements 8.1** — Property 13, arm A. do-NOT 6 quantified.

    For any canonical verdict, ``verdict_to_parsed_results(v)["verdict"] ==
    v["verdict"]``. The Recommendation_Deriver derives ``recommendation`` from
    the Effective_Verdict (``review_verdict.py:405``) and passes the reviewer's
    own token through beside it, unmodified — two facts on one dict, and R8.1
    says the second is not a function of the first.

    The severity floor is the reason this needs quantifying rather than
    assuming. It creates a new shape where the two values differ in the
    *opposite* direction from before p43: a raw ``fail`` whose only finding is a
    ``minor`` scaffolding-attributed one already recommended APPROVE, and now a
    raw ``pass`` carrying a blocking finding recommends REJECT. An
    implementation that "tidied up" by writing the effective verdict back onto
    ``verdict`` would look right on the recommendation and destroy what the
    reviewer said — which three consumers display, and which
    ``test_review_gate_calibration.py:370-376`` pins by example on one fixture.

    ``issues`` is asserted to pass through by identity as well: the adapter is
    documented to let issues "pass through carrying their Issue_Attribution"
    (``:391``), and a copy here would make the retention claims in tasks 6.1 and
    10.1 true of a different list than the one the gate read.
    """
    parsed = verdict_to_parsed_results(verdict)

    assert parsed["verdict"] == verdict["verdict"], (
        f"the adapter rewrote the Raw_Verdict from {verdict['verdict']!r} to {parsed['verdict']!r} "
        "(R8.1 forbids it; do-NOT 6). If this failed with the two verdicts differing exactly where "
        "the Effective_Verdict differs from the raw one, the adapter is writing the gate's answer "
        "back over the reviewer's."
    )
    assert parsed["verdict"] in {VERDICT_PASS, VERDICT_FAIL}
    # Identity, not equality: the issues are the same list object the gate read.
    assert parsed["issues"] is verdict["issues"]
    # The derived half, asserted so arm A cannot pass by the adapter deriving
    # nothing at all: the recommendation follows the EFFECTIVE verdict.
    expected_recommendation = "APPROVE" if effective_verdict(verdict) == VERDICT_PASS else "REJECT"
    assert parsed["recommendation"] == expected_recommendation


def test_property_13_reaches_both_raw_verdicts():
    """Arm A's space contains both tokens, and the adapter is not a constant.

    An adapter hardcoding ``"pass"`` would satisfy arm A on every raw-pass draw.
    Both tokens are therefore shown to survive, on the two fixtures the parser
    can produce for each.
    """
    raw_pass = _parse(VERDICT_PASS, [])
    assert verdict_to_parsed_results(raw_pass)["verdict"] == VERDICT_PASS

    raw_fail = _parse(VERDICT_FAIL, [_issue_doc("Unattributed finding.", "major")])
    assert verdict_to_parsed_results(raw_fail)["verdict"] == VERDICT_FAIL


def test_the_raw_verdict_survives_a_gate_that_says_fail():
    """The pass-through's hardest case: the gate and the reviewer disagree.

    A raw ``pass`` carrying a reviewer transform-block finding at a
    Blocking_Severity is an effective ``fail`` (rule 1). The recommendation is
    REJECT and the ``verdict`` key still reads ``pass`` — because those are two
    different facts, and R8.1 is the one that says so.

    The mirror case is asserted too: a raw ``fail`` whose only finding is
    retained-eligible scaffolding recommends APPROVE while ``verdict`` still
    reads ``fail``. Together they show the pass-through is not merely agreeing
    with the derivation in both directions by luck.
    """
    blocking_on_a_raw_pass = _parse(VERDICT_PASS, [_issue_doc("Wrong join key.", "critical", "transform_block")])
    parsed = verdict_to_parsed_results(blocking_on_a_raw_pass)
    assert effective_verdict(blocking_on_a_raw_pass) == VERDICT_FAIL
    assert parsed["recommendation"] == "REJECT"
    assert parsed["verdict"] == VERDICT_PASS, (
        "the gate said 'fail' and the adapter overwrote the reviewer's own 'pass' with it (R8.1, "
        "do-NOT 6). This is the exact confusion the retained-finding work makes more likely, not "
        "less: the two values now differ on more shapes than before."
    )

    scaffolding_on_a_raw_fail = _parse(
        VERDICT_FAIL, [_issue_doc("Boilerplate import order.", "minor", "template_scaffolding")]
    )
    parsed = verdict_to_parsed_results(scaffolding_on_a_raw_fail)
    assert effective_verdict(scaffolding_on_a_raw_fail) == VERDICT_PASS
    assert parsed["recommendation"] == "APPROVE"
    assert parsed["verdict"] == VERDICT_FAIL


# ---------------------------------------------------------------------------
# Property 13, arm B — the attribution default is passed through
# ---------------------------------------------------------------------------


# Feature: p43-review-gate-severity-floor, Property 13: The raw verdict and the
# attribution default are passed through.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    attribution=_UNRECOGNISED_ATTRIBUTION,
    severity=_ANY_SEVERITY,
    description=_ISSUE_TEXT,
    raw_verdict=st.sampled_from((VERDICT_PASS, VERDICT_FAIL)),
)
@example(attribution="", severity="minor", description="an issue", raw_verdict=VERDICT_PASS)
@example(attribution="   ", severity="critical", description="an issue", raw_verdict=VERDICT_PASS)
@example(attribution=None, severity="critical", description="an issue", raw_verdict=VERDICT_FAIL)
@example(attribution="template scaffolding", severity="minor", description="an issue", raw_verdict=VERDICT_FAIL)
@example(attribution="transform-block", severity="critical", description="an issue", raw_verdict=VERDICT_PASS)
def test_an_unrecognised_attribution_still_defaults_conservatively(attribution, severity, description, raw_verdict):
    """**Validates: Requirements 8.2** — Property 13, arm B. do-NOT 7 quantified.

    For any attribution value that is not a recognised token after
    normalisation — a plausible-but-wrong token, an empty or whitespace string,
    a hyphen for an underscore, a non-string type — the parsed issue carries
    ``("transform_block", "default")``. Exactly the default it applies today
    (``review_verdict.py:230-243``), at every severity and on both raw verdicts.

    **Why this is the arm that matters.** Design §4.1 rejected an alternative
    implementation of the severity floor that applied it *in the parser*, by
    downgrading a ``minor`` reviewer finding's ``attribution_source`` to
    ``default``. That alternative satisfies every gate property in this suite —
    the effective verdicts come out identical — and is visible only from here:
    it makes the calibration invisible at the gate and corrupts the provenance
    three consumers display. So this property asserts the default is applied
    where it always was and **nowhere else**.

    Two consequences are asserted beyond the pair itself, because the pair alone
    does not say what the gate then does with it:

    * the issue is **not** a Reviewer_Transform_Block_Finding, so rule 1 cannot
      fire — which is why the gate returns ``pass`` on a raw ``pass`` even at a
      Blocking_Severity;
    * on a raw ``fail`` it reaches **rule 2** and returns ``fail``, which is
      ``requirements.md`` do-NOT 7 in as many words: "It reaches rule 2, not
      rule 1, and this spec does not change rule 2."

    The severity is carried through normalised and untouched by the attribution
    default — the two fields are independent, and a parser that downgraded one
    on the strength of the other is the rejected design above.
    """
    parsed = _parse(raw_verdict, [_issue_doc(description, severity, attribution)])
    issue = parsed["issues"][0]

    assert (issue["attribution"], issue["attribution_source"]) == (
        ATTRIBUTION_TRANSFORM_BLOCK,
        ATTRIBUTION_SOURCE_DEFAULT,
    ), (
        f"attribution input {attribution!r} does not normalise to a recognised token, so R8.2 "
        f"requires the conservative default ('transform_block', 'default'); the parser emitted "
        f"{(issue['attribution'], issue['attribution_source'])!r}."
    )
    # R2.2 — severity is normalised and otherwise untouched. Asserted here
    # because the rejected parser-side floor would have changed the attribution
    # pair as a FUNCTION of this value; showing both survive independently is
    # what rules that implementation out.
    assert issue["severity"] == severity.strip().lower()

    # Rule 1's antecedent is unsatisfied — the reviewer never said
    # transform_block, the parser did.
    assert not _is_reviewer_transform_block_finding(issue)

    if raw_verdict == VERDICT_PASS:
        assert effective_verdict(parsed) == VERDICT_PASS, (
            f"a defaulted attribution at severity {issue['severity']!r} failed a raw 'pass'. Rule 1 "
            "requires reviewer provenance, so it must not fire here — the conservative default may "
            "never strengthen a pass (P28b rule 4, R1.6)."
        )
    else:
        assert effective_verdict(parsed) == VERDICT_FAIL, (
            "a defaulted attribution on a raw 'fail' must keep it failing through rule 2 — it is "
            "not explicitly scaffolding-attributed, so rule 3's exemption does not apply (do-NOT 7)."
        )


@pytest.mark.parametrize("attribution", _TEN_SEED_ATTRIBUTION_VALUES)
def test_every_seed_attribution_value_defaults_conservatively(attribution):
    """Arm B's ten seed cases, as named examples.

    These are the values parametrized at
    ``test_review_verdict_attribution.py:78-88`` — that file's
    ``test_garbage_attribution_defaults_to_transform_block``, which asserts the
    same default for P28b R1.5. Restated here rather than imported: this file's
    subject is that p43 left the default alone, so the two assertions are meant
    to be independent and to fail independently.

    Kept as a parametrization beside the property because a parametrized id
    reads better in a failure report than a shrunk counterexample, and because
    ``["template_scaffolding"]`` and ``{}`` are the two seeds the property draws
    freshly per example rather than sampling.
    """
    assert not _normalises_to_a_recognised_attribution(attribution), (
        f"seed value {attribution!r} DOES normalise to a recognised attribution, so it is not a "
        "garbage case and arm B's premise does not apply to it."
    )
    parsed = _parse(VERDICT_FAIL, [_issue_doc("an issue", "minor", attribution)])
    issue = parsed["issues"][0]
    assert issue["attribution"] == ATTRIBUTION_TRANSFORM_BLOCK
    assert issue["attribution_source"] == ATTRIBUTION_SOURCE_DEFAULT


def test_the_seed_list_still_has_ten_values():
    """The tripwire on arm B's seeding claim.

    The module docstring and
    :func:`test_every_seed_attribution_value_defaults_conservatively` both state
    that arm B is seeded with the existing ten-value parametrization. If that
    list is trimmed here, the claim becomes false quietly — the property would
    still pass over its generated space while covering fewer of the shapes a
    real reviewer produces.
    """
    assert len(_TEN_SEED_ATTRIBUTION_VALUES) == 10, (
        f"the seed list carries {len(_TEN_SEED_ATTRIBUTION_VALUES)} values, not the ten taken from "
        "test_review_verdict_attribution.py:78-88."
    )
    assert _TEN_SEED_ATTRIBUTION_VALUES[:8] == _SEED_ATTRIBUTION_VALUES, (
        "the eight scalar seeds the property samples from must be the first eight of the ten; the "
        "remaining two are the mutable list and dict, built per draw."
    )


def test_an_explicit_reviewer_attribution_is_not_defaulted():
    """Arm B's complement: the default applies only where the reviewer said nothing usable.

    Arm B asserts a negative over a large space, and a parser that defaulted
    *everything* would satisfy it completely — while destroying rule 1's
    antecedent and silently disabling the whole gate calibration. The recognised
    spellings are therefore shown to survive with ``attribution_source ==
    "reviewer"``.
    """
    for spelling in ("transform_block", "TRANSFORM_BLOCK", " transform_block ", "Transform_Block"):
        issue = _parse(VERDICT_PASS, [_issue_doc("an issue", "minor", spelling)])["issues"][0]
        assert issue["attribution"] == ATTRIBUTION_TRANSFORM_BLOCK
        assert issue["attribution_source"] == ATTRIBUTION_SOURCE_REVIEWER, (
            f"spelling {spelling!r} was defaulted rather than read as an explicit reviewer "
            "attribution, which would make rule 1 unreachable and arm B vacuous."
        )

    for spelling in ("template_scaffolding", "Template_Scaffolding", "  template_scaffolding  "):
        issue = _parse(VERDICT_FAIL, [_issue_doc("an issue", "minor", spelling)])["issues"][0]
        assert issue["attribution"] == ATTRIBUTION_TEMPLATE_SCAFFOLDING
        assert issue["attribution_source"] == ATTRIBUTION_SOURCE_REVIEWER
