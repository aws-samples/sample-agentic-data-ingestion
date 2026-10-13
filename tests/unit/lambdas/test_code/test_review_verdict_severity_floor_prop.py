# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p43 task 5.3 — Properties 3, 4, 5 and 6 over the Review_Gate and the parser.

What this file owns
-------------------
The four gate/parser properties design §6 assigns to task 5.3, and nothing else:

* **Property 3** — a raw ``fail`` stays a ``fail`` at every severity (R1.4).
* **Property 4** — rules 2, 3 and 4 are untouched, checked against an
  independent reference implementation (R1.3, R1.7).
* **Property 5** — severity classification is exact equality, in two arms
  (R1.5, R2.2).
* **Property 6** — the parser accepts every non-empty token and raises on
  empty-or-whitespace (R2.1, R2.2).

Properties 1 and 2 live with task 5.1's generator in
``test_review_gate_calibration.py``; Property 12 lives in
``test_retained_findings_helpers_prop.py``; the eight named cells of the R7.5
matrix live in ``test_review_verdict_severity_matrix.py``. This file adds no
assertion about ``retained_nonblocking_findings`` or
``blocking_transform_block_findings`` — that is Property 12's ground, and
duplicating it here would mean two files disagreeing about which one owns the
helpers.

Symbols under test, blast radius and coverage
---------------------------------------------
``effective_verdict`` (``review_verdict.py:294``) — 51 callers across
``review_verdict.py`` and ``test_code/lambda_function.py`` (``:1152``, ``:1506``,
``:1675``, ``:405``); covered by nine test files, so codegraph raises no
zero-coverage flag and no characterization test is owed.
``parse_review_verdict`` (``:156``) — 59 callers in
``test_code/lambda_function.py``, covered by eight test files.

Both figures are from ``codegraph explore`` run *after* this file landed, so both
count this file's own calls and its own coverage; measured before it existed they
were 40 and 57. Recorded rather than assumed, and expected to keep rising as
tasks 6.3, 10.2, 11.2 and 12.2 add their files — a later reader finding a larger
number has not found a discrepancy.

Fixtures come through the real parser wherever the claim allows it
-----------------------------------------------------------------
Properties 3, 4 and 6 build every verdict with ``parse_review_verdict`` from a
JSON document, matching the technique in ``test_review_verdict_severity_matrix.py``
and ``test_retained_findings_helpers_prop.py``: ``attribution_source`` then comes
out ``"reviewer"`` because the parser put it there, not because this file typed
it. That matters because it is half of rule 1's antecedent — a hand-set
``attribution_source`` would make the antecedent an artifact of the test, and
every one of these properties would still pass while measuring rule 2.

**Property 5's arm A is the one deliberate exception, and it has to be.** Its
claim is that the *gate* compares by byte equality, and the parser normalises
severity at ``review_verdict.py:225`` — so a token that differs from ``minor``
only in case or padding can never reach the gate through the parser at all. Arm A
therefore hand-builds the canonical issue, and
:func:`test_the_hand_built_issue_shape_matches_what_the_parser_emits` pins that
hand-built shape against a real parsed issue so the exception cannot quietly
drift into a fixture the parser would never produce. Arm B is the same claim on
the parser's own path.

Non-vacuity guards
------------------
Every property here has an antecedent that a careless generator can leave
unsatisfied, at which point the property passes and tests nothing. Each is
guarded inside the property body (the drawn shape really is a
Reviewer_Transform_Block_Finding, or really carries none), and three named
example tests fix the reachability claims the ``if``-guarded assertions cannot
make on their own:

* :func:`test_property_3_reaches_both_documented_routes` — a raw ``fail`` fails
  through rule 1 at a Blocking_Severity and through rules 2/3 at a
  Non_Blocking_Severity, so Property 3 is not one route wearing two hats.
* :func:`test_property_4_reference_reaches_all_three_of_its_outcomes` — the
  reference implementation returns ``pass`` by rule 3 and by rule 4, and
  ``fail`` by rule 2, over shapes the generators draw. Without this, an
  always-``fail`` reference would agree with an always-``fail`` gate.
* :func:`test_the_hand_built_issue_shape_matches_what_the_parser_emits` — see
  above.

NO FIXTURES
-----------
Nothing here needs one: ``review_verdict.py`` is pure stdlib (its module
docstring, ``:56-58``), so there is no AWS client, no moto mock and no Lambda
environment to arrange, and no fixture in ``tests/fixtures/conftest.py`` applies.
The root ``tests/conftest.py`` still applies ``@pytest.mark.unit`` by location.
"""

from __future__ import annotations

import json
import os
import string
import sys
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)

# Imported after the sys.path insert above, which is what makes it resolvable.
import review_verdict as _rv
from review_verdict import (
    ATTRIBUTION_SOURCE_DEFAULT,
    ATTRIBUTION_SOURCE_REVIEWER,
    ATTRIBUTION_TEMPLATE_SCAFFOLDING,
    ATTRIBUTION_TRANSFORM_BLOCK,
    NON_BLOCKING_SEVERITIES,
    VERDICT_FAIL,
    VERDICT_PASS,
    ReviewVerdictError,
    effective_verdict,
    parse_review_verdict,
)

# ---------------------------------------------------------------------------
# Property 4's reference implementation of rules 2, 3 and 4
# ---------------------------------------------------------------------------
#
# WRITTEN FROM THE DOCSTRING, NOT FROM THE FUNCTION BODY. ``LGR-28b4d75-5``
# (**template**) rejected transcribing a producer's own output into a test
# "because the output is its own oracle" — a test written that way cannot detect
# the producer drifting. The same argument applies one step over: a reference
# implementation copied from ``effective_verdict``'s body agrees with it by
# construction, including after a wrong edit.
#
# The three source texts this was written from, quoted in the clauses below:
#
#   * ``review_verdict.py:311-320`` — rules 2, 3 and 4 of ``effective_verdict``'s
#     docstring, which give the conditions and the outcomes;
#   * ``review_verdict.py:322-324`` — the docstring's closing sentence about a
#     ``fail`` verdict with no issues;
#   * ``review_verdict.py:38-45`` (the module docstring's ``attribution`` bullet)
#     plus ``requirements.md``'s Glossary, for what "explicitly" means: the
#     reviewer supplied a recognised attribution rather than the parser
#     defaulting it, recorded as ``attribution_source``.
#
# The structure below deliberately differs from the implementation's: rule 4 is
# an early return here and the no-issues case is its own clause, where the
# implementation folds both into a ``bool(issues) and all(...)`` expression. Two
# shapes that agree on every input is the evidence Property 4 is after; two
# copies of one shape would not be.


def _is_explicitly_scaffolding(issue: dict[str, Any]) -> bool:
    """ "Explicitly ``template_scaffolding``", per the docs cited above.

    Two conditions, because "explicitly" is doing work: the attribution names
    the template scaffolding, AND the reviewer is the one who said so. The
    parser's conservative default is ``("transform_block", "default")``, so an
    issue the reviewer left unattributed is not scaffolding by omission.
    """
    return (
        issue.get("attribution") == ATTRIBUTION_TEMPLATE_SCAFFOLDING
        and issue.get("attribution_source") == ATTRIBUTION_SOURCE_REVIEWER
    )


def _rules_2_3_4_reference(verdict: dict[str, Any]) -> str:
    """Rules 2, 3 and 4 alone — rule 1 deliberately absent.

    Read as: what would ``effective_verdict`` return if no
    Reviewer_Transform_Block_Finding at a Blocking_Severity existed? Property 4
    then asserts the real gate agrees with this on exactly the two families of
    verdict where rule 1 cannot fire.
    """
    # Rule 4 — "Raw verdict ``pass`` → **pass**" (:319-321). Written as the
    # docstring words it (a raw verdict that is not ``fail`` is a pass, since
    # the parser admits only the two tokens).
    if verdict.get("verdict") != VERDICT_FAIL:
        return VERDICT_PASS

    issues = verdict.get("issues") or []

    # ":322-324" — "A ``fail`` verdict with no issues ... stays **fail** here:
    # with no issues there is no exempting scaffolding attribution."
    if not issues:
        return VERDICT_FAIL

    # Rule 3 — "Raw verdict ``fail``, every issue explicitly
    # ``template_scaffolding`` → **pass**" (:316-318).
    if all(_is_explicitly_scaffolding(issue) for issue in issues):
        return VERDICT_PASS

    # Rule 2 — "Raw verdict ``fail`` and any issue NOT explicitly
    # scaffolding-attributed → **fail**" (:311-315). The complement of rule 3
    # on a raw fail, which is what makes these two clauses total.
    return VERDICT_FAIL


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Non-empty after the parser's ``strip()``, which is what
#: ``_validate_verdict_document`` requires of a description (``:222-223``).
_ISSUE_TEXT = (
    st.text(alphabet=string.ascii_letters + string.digits + " .,", min_size=1, max_size=30).map(str.strip).filter(bool)
)

#: The Non_Blocking_Severity tokens, read off the real frozenset rather than
#: restated, so nothing here drifts if the set ever grows a second tier.
_NON_BLOCKING_SEVERITY = st.sampled_from(sorted(NON_BLOCKING_SEVERITIES))

#: The five near-miss spellings task 5.3 names, kept as literals because they
#: are literals in the requirement. Two of them (``"minor "``, ``"Minor"``)
#: normalise INTO the set and so are non-blocking once parsed; three
#: (``"minorly"``, ``"min"``, ``"sminor"``) do not, and are the prefix/substring
#: attack — a gate testing ``startswith`` or ``in`` rather than set membership
#: would classify at least one of them as exempt.
_NEAR_MISSES = ("minor ", "Minor", "minorly", "min", "sminor")

#: The three near-misses no amount of normalisation rescues. Broken out because
#: they are the only near-misses that still block after the parser has had them.
_NON_NORMALISING_NEAR_MISSES = ("minorly", "min", "sminor")

#: Tokens outside the non-blocking set: the two recommended blocking severities
#: and two Unrecognised_Severity tokens that READ as trivial. ``nit`` and
#: ``info`` are here on purpose — under the complement encoding a token nobody
#: enumerated blocks, and that is the half of R1.2 a reader is most likely to
#: get wrong.
_BLOCKING_SEVERITY = st.sampled_from(("critical", "major", "nit", "info", "blocker", "error"))

#: Spellings the parser normalises INTO the non-blocking set. Built from the real
#: set members rather than hardcoded so this follows the vocabulary.
_NORMALISES_TO_NON_BLOCKING = st.builds(
    lambda token, upper_first, pad_left, pad_right: (
        (" " * pad_left) + (token[:1].upper() + token[1:] if upper_first else token) + (" " * pad_right)
    ),
    st.sampled_from(sorted(NON_BLOCKING_SEVERITIES)),
    st.booleans(),
    st.integers(min_value=0, max_value=2),
    st.integers(min_value=0, max_value=2),
)

#: The whole severity axis as a REVIEWER would spell it, spanning both sides of
#: the floor plus the near-misses and the casings the parser folds away.
_ANY_SEVERITY = st.one_of(
    _NON_BLOCKING_SEVERITY,
    _BLOCKING_SEVERITY,
    st.sampled_from(_NEAR_MISSES),
    _NORMALISES_TO_NON_BLOCKING,
)

#: Attribution inputs whose normalised value is NOT ``transform_block``, so a
#: parsed issue from one of these is never a Reviewer_Transform_Block_Finding:
#: ``None`` / ``""`` / garbage default to ``("transform_block", "default")``
#: (which rule 1 does not match), and the scaffolding spellings yield
#: ``("template_scaffolding", "reviewer")``.
_NON_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION = st.sampled_from(
    (None, "", "generated", "transform-block", "TRANSFORM BLOCK", "template_scaffolding", "Template_Scaffolding")
)

#: Attribution inputs the parser recognises as an explicit reviewer
#: transform-block attribution, in the casings ``:236-238`` folds.
_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION = st.sampled_from(
    ("transform_block", "TRANSFORM_BLOCK", " transform_block ", "Transform_Block")
)

#: Every attribution input, either kind.
_ANY_ATTRIBUTION = st.one_of(
    _NON_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION,
    _REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION,
)


def _issue_doc(description: str, severity: str, attribution: str | None) -> dict[str, Any]:
    """A raw reviewer issue as it appears in the model's JSON, pre-parse."""
    doc: dict[str, Any] = {"severity": severity, "description": description}
    if attribution is not None:
        doc["attribution"] = attribution
    return doc


def _parse(raw_verdict: str, issue_docs: list[dict[str, Any]], categories: dict[str, str] | None = None):
    """Build a verdict document and run it through the REAL parser."""
    return parse_review_verdict(
        json.dumps({"verdict": raw_verdict, "issues": issue_docs, "categories": categories or {}})
    )


def _is_reviewer_transform_block_finding(issue: dict[str, Any]) -> bool:
    """Rule 1's attribution antecedent, spelled out at the test's own level.

    Deliberately not an import of ``review_verdict._is_reviewer_transform_block``
    — the module-private predicate is the implementation's, and reading it here
    would make the generators' guards agree with the gate by construction. This
    is the definition from ``requirements.md``'s Glossary
    (Reviewer_Transform_Block_Finding) written out.
    """
    return (
        issue.get("attribution") == ATTRIBUTION_TRANSFORM_BLOCK
        and issue.get("attribution_source") == ATTRIBUTION_SOURCE_REVIEWER
    )


@st.composite
def _raw_fail_with_a_reviewer_transform_block(draw) -> dict[str, Any]:
    """Property 3's space: a raw ``fail`` with >= 1 reviewer transform-block finding.

    The blocking finding's severity is drawn from the WHOLE axis — both sides of
    the floor, the near-misses and the normalising casings — because Property 3's
    claim is "of any severity whatsoever". Companions are drawn freely, including
    scaffolding ones, which is the shape that could exempt a raw fail through
    rule 3 if the transform-block finding were not there.
    """
    focus_severity = draw(_ANY_SEVERITY)
    focus = _issue_doc(draw(_ISSUE_TEXT), focus_severity, draw(_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION))
    companions = draw(
        st.lists(st.builds(_issue_doc, _ISSUE_TEXT, _ANY_SEVERITY, _ANY_ATTRIBUTION), max_size=4),
    )
    # Position matters to the claim ("at any position"), so the focus issue is
    # spliced in at a drawn index rather than always leading.
    index = draw(st.integers(min_value=0, max_value=len(companions)))
    docs = [*companions[:index], focus, *companions[index:]]
    return _parse(VERDICT_FAIL, docs)


@st.composite
def _verdicts_with_no_reviewer_transform_block(draw) -> dict[str, Any]:
    """Property 4 arm (a): every issue's attribution is anything BUT reviewer transform-block.

    Reaches both rule-3-exempt shapes (all issues explicitly scaffolding) and
    rule-2 shapes (at least one defaulted issue), and the empty issue list on a
    raw pass — so the reference's four documented outcomes are all in range.
    """
    raw_verdict = draw(st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))
    docs = draw(
        st.lists(
            st.builds(_issue_doc, _ISSUE_TEXT, _ANY_SEVERITY, _NON_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION),
            # A raw fail with no issues is rejected by the parser (:246-249).
            min_size=1 if raw_verdict == VERDICT_FAIL else 0,
            max_size=5,
        )
    )
    return _parse(raw_verdict, docs)


@st.composite
def _verdicts_whose_reviewer_findings_are_all_non_blocking(draw) -> dict[str, Any]:
    """Property 4 arm (b): reviewer transform-block findings exist, all non-blocking.

    The constraint is per-issue: an issue drawn with a reviewer transform-block
    attribution takes a severity that normalises INTO
    ``NON_BLOCKING_SEVERITIES``; every other issue takes any severity, because
    rule 1 cannot see it. At least one reviewer transform-block finding is forced,
    or the arm collapses into arm (a).
    """
    raw_verdict = draw(st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))
    focus = _issue_doc(
        draw(_ISSUE_TEXT),
        draw(_NORMALISES_TO_NON_BLOCKING),
        draw(_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION),
    )
    companions: list[dict[str, Any]] = []
    for attribution, blocking_severity, non_blocking_severity in draw(
        st.lists(
            st.tuples(_ANY_ATTRIBUTION, _ANY_SEVERITY, _NORMALISES_TO_NON_BLOCKING),
            max_size=4,
        )
    ):
        recognised_transform_block = isinstance(attribution, str) and attribution.strip().lower() == (
            ATTRIBUTION_TRANSFORM_BLOCK
        )
        severity = non_blocking_severity if recognised_transform_block else blocking_severity
        companions.append(_issue_doc(draw(_ISSUE_TEXT), severity, attribution))
    index = draw(st.integers(min_value=0, max_value=len(companions)))
    docs = [*companions[:index], focus, *companions[index:]]
    return _parse(raw_verdict, docs)


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


# Feature: p43-review-gate-severity-floor, Property 3: A raw fail stays a fail
# at every severity.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(verdict=_raw_fail_with_a_reviewer_transform_block())
def test_a_raw_fail_stays_a_fail_at_every_severity(verdict):
    """**Validates: Requirements 1.4** — Property 3.

    For any raw ``fail`` verdict carrying at least one
    Reviewer_Transform_Block_Finding of any severity whatsoever — either side of
    the floor, an Unrecognised_Severity, a near-miss spelling, at any position,
    with any companions including explicit scaffolding ones — the Review_Gate
    returns ``fail``.

    R1.4 is what stops the floor weakening a run the reviewer itself failed, and
    the floor makes it hold by two different routes depending on severity: rule 1
    at a Blocking_Severity, rules 2/3 at a Non_Blocking_Severity (design §4.1,
    "Where the minor-on-raw-fail case goes"). Both routes are inside this draw
    space, and :func:`test_property_3_reaches_both_documented_routes` shows they
    are both reachable — otherwise this property could be one route asserted
    twice.

    Expected from ``requirements.md``'s four-row table (rows 3 and 4 of "The
    floor is narrower than it first appears") and R1.4, not from what the gate
    currently returns.
    """
    # The antecedent is real, and the parser is what made it real: at least one
    # issue carries ('transform_block', 'reviewer') because the reviewer's own
    # attribution said so. Without this, a defaulted attribution would make every
    # draw a rule-2 verdict and the property would be about rule 2.
    reviewer_findings = [i for i in verdict["issues"] if _is_reviewer_transform_block_finding(i)]
    assert reviewer_findings, (
        "no issue in this draw is a Reviewer_Transform_Block_Finding, so Property 3's antecedent is "
        f"unsatisfied and it is not testing R1.4: {verdict!r}"
    )
    assert verdict["verdict"] == VERDICT_FAIL, (
        f"the parser rewrote the raw verdict to {verdict['verdict']!r}; this property's 'raw fail' "
        "antecedent no longer holds (R8.1 forbids rewriting it)."
    )

    assert effective_verdict(verdict) == VERDICT_FAIL, (
        "a raw 'fail' carrying a reviewer transform-block finding must stay 'fail' at EVERY severity "
        f"(R1.4). Severities present: {[i['severity'] for i in reviewer_findings]!r}. "
        f"NON_BLOCKING_SEVERITIES is {sorted(NON_BLOCKING_SEVERITIES)!r}. If this failed at a "
        "non-blocking severity, the floor was applied outside rule 1 — most likely by exempting the "
        "token from rules 2/3 as well, which weakens R1.4 while leaving the raw-pass cells green."
    )


def test_property_3_reaches_both_documented_routes():
    """Property 3's antecedent covers two routes, and both are reachable.

    Rule 1 at a Blocking_Severity, and rules 2/3 at a Non_Blocking_Severity. Both
    return ``fail`` on a raw ``fail``, so the property alone cannot tell whether
    its draw space reaches the second one at all — and the second one is the
    interesting half, because it is where the floor changed the route without
    changing the outcome (design §4.1).

    Route is read the way ``test_review_verdict_severity_matrix.py`` reads it:
    rule 1 is the only rule that ignores the raw verdict, so re-evaluating the
    same issue shape under a raw ``pass`` answers whether rule 1 fires.
    """
    blocking_token = "critical"
    non_blocking_token = sorted(NON_BLOCKING_SEVERITIES)[0]

    for token, rule_1_should_fire in ((blocking_token, True), (non_blocking_token, False)):
        docs = [_issue_doc("a finding in the generated transform", token, "transform_block")]
        on_raw_fail = _parse(VERDICT_FAIL, docs)
        on_raw_pass = _parse(VERDICT_PASS, docs)

        assert effective_verdict(on_raw_fail) == VERDICT_FAIL, f"a raw fail at severity {token!r} should fail (R1.4)."
        assert (effective_verdict(on_raw_pass) == VERDICT_FAIL) is rule_1_should_fire, (
            f"rule 1 should {'' if rule_1_should_fire else 'NOT '}fire at severity {token!r}; the same "
            "issue shape on a raw 'pass' says otherwise. Property 3's space would then cover only one "
            "route."
        )


# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------


# Feature: p43-review-gate-severity-floor, Property 4: Rules 2, 3 and 4 are
# untouched.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(verdict=_verdicts_with_no_reviewer_transform_block())
def test_rules_2_3_4_are_untouched_when_no_reviewer_transform_block_finding_exists(verdict):
    """**Validates: Requirements 1.3, 1.7** — Property 4, arm (a). Model-based.

    For any verdict carrying no Reviewer_Transform_Block_Finding, rule 1 cannot
    fire, so the Review_Gate must return exactly what rules 2, 3 and 4 alone
    return. The oracle is :func:`_rules_2_3_4_reference`, **written from
    ``effective_verdict``'s docstring at ``review_verdict.py:311-324``, not
    copied from its body** — ``LGR-28b4d75-5`` (**template**): a test written
    from the producer's own output cannot detect the producer drifting, because
    the output is its own oracle.

    R1.7 says rules 2, 3 and 4 are unchanged in both condition and outcome. That
    is a claim about the gate as a whole, not about the diff, which is why it is
    checked against an independent implementation rather than by reading the
    patch.
    """
    for issue in verdict["issues"]:
        assert not _is_reviewer_transform_block_finding(issue), (
            "this arm's generator produced a Reviewer_Transform_Block_Finding, so rule 1 CAN fire and "
            f"the arm's premise is void: {issue!r}"
        )

    assert effective_verdict(verdict) == _rules_2_3_4_reference(verdict), (
        "the gate disagreed with an independent implementation of rules 2/3/4 on a verdict where rule 1 "
        f"cannot fire, so one of those three rules moved (R1.7 forbids it): {verdict!r}"
    )


# Feature: p43-review-gate-severity-floor, Property 4: Rules 2, 3 and 4 are
# untouched.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(verdict=_verdicts_whose_reviewer_findings_are_all_non_blocking())
def test_rules_2_3_4_are_untouched_when_every_reviewer_finding_is_non_blocking(verdict):
    """**Validates: Requirements 1.3, 1.7** — Property 4, arm (b). Model-based.

    For any verdict whose Reviewer_Transform_Block_Findings all carry a
    Non_Blocking_Severity, rule 1 is exempt on every one of them, so the gate
    must again agree with rules 2/3/4 alone. This is R1.3 stated as an
    equality — "evaluate the remaining rules without failing on those findings"
    means exactly "return what the remaining rules return".

    The two arms are separate because they fail for different reasons. Arm (a)
    breaking means one of rules 2/3/4 changed. This one breaking means the floor
    leaked: either rule 1 is still firing on an exempt token, or the exemption
    reached further than rule 1 and started excusing a raw ``fail`` — the R1.4
    weakening Property 3 guards from the other side.
    """
    reviewer_findings = [i for i in verdict["issues"] if _is_reviewer_transform_block_finding(i)]
    assert reviewer_findings, (
        f"this arm must contain at least one Reviewer_Transform_Block_Finding or it duplicates arm (a): {verdict!r}"
    )
    for issue in reviewer_findings:
        assert issue["severity"] in NON_BLOCKING_SEVERITIES, (
            f"the generator let a Blocking_Severity {issue['severity']!r} through on a reviewer "
            "transform-block finding, so rule 1 legitimately fires and this arm's premise is void."
        )

    assert effective_verdict(verdict) == _rules_2_3_4_reference(verdict), (
        "the gate disagreed with an independent implementation of rules 2/3/4 on a verdict whose "
        "reviewer transform-block findings are all at a Non_Blocking_Severity (R1.3, R1.7): "
        f"{verdict!r}"
    )


def test_property_4_reference_reaches_all_three_of_its_outcomes():
    """The reference implementation is not a constant function.

    Property 4 asserts an equality, and an equality between two functions that
    both always return ``fail`` holds while saying nothing. So each documented
    route through :func:`_rules_2_3_4_reference` is exercised on a fixture the
    generators can draw, and the gate is checked to agree on each.

    Rule 4 (raw pass), rule 3 (raw fail, every issue explicitly scaffolding) and
    rule 2 (raw fail, at least one issue the reviewer left unattributed) — plus
    the docstring's no-issues sentence, which the parser cannot reach and so is
    checked on a hand-built dict.
    """
    rule_4 = _parse(VERDICT_PASS, [_issue_doc("a nit", "critical", "template_scaffolding")])
    assert _rules_2_3_4_reference(rule_4) == VERDICT_PASS
    assert effective_verdict(rule_4) == VERDICT_PASS

    rule_3 = _parse(
        VERDICT_FAIL,
        [
            _issue_doc("boilerplate import order", "major", "template_scaffolding"),
            _issue_doc("boilerplate logging call", "minor", "Template_Scaffolding"),
        ],
    )
    assert _rules_2_3_4_reference(rule_3) == VERDICT_PASS
    assert effective_verdict(rule_3) == VERDICT_PASS

    rule_2 = _parse(
        VERDICT_FAIL,
        [
            _issue_doc("boilerplate import order", "major", "template_scaffolding"),
            _issue_doc("something the reviewer did not attribute", "major", None),
        ],
    )
    assert _rules_2_3_4_reference(rule_2) == VERDICT_FAIL
    assert effective_verdict(rule_2) == VERDICT_FAIL
    # The unattributed issue defaulted rather than being read as scaffolding,
    # which is what routes it to rule 2 (do-NOT 7, untouched by this spec).
    assert rule_2["issues"][1]["attribution"] == ATTRIBUTION_TRANSFORM_BLOCK
    assert rule_2["issues"][1]["attribution_source"] == ATTRIBUTION_SOURCE_DEFAULT

    # ``:322-324``: unreachable through the parser, which rejects a fail with no
    # issues — so the reference and the gate are compared on a hand-built dict.
    no_issues = {"verdict": VERDICT_FAIL, "issues": []}
    assert _rules_2_3_4_reference(no_issues) == VERDICT_FAIL
    assert effective_verdict(no_issues) == VERDICT_FAIL


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------

#: Arm A's token space: the recognised tokens, the five near-misses, the
#: normalising casings, and arbitrary text — so "any severity token" is not just
#: a list someone thought of.
_ARM_A_TOKEN = st.one_of(
    _NON_BLOCKING_SEVERITY,
    _BLOCKING_SEVERITY,
    st.sampled_from(_NEAR_MISSES),
    _NORMALISES_TO_NON_BLOCKING,
    st.text(max_size=12),
)


def _hand_built_reviewer_transform_block(severity: str) -> dict[str, Any]:
    """A canonical verdict whose one issue is a reviewer transform-block finding.

    Hand-built, and only here: arm A's subject is the GATE's comparison, and the
    parser normalises severity at ``:225``, so a token differing from a
    non-blocking member only in case or padding cannot reach the gate through the
    parser at all. The raw verdict is ``pass`` so that rule 1 is the only route
    to ``fail``, which is what makes "blocks" observable as a return value.

    The attribution pair is taken from the module's own constants and is pinned
    against a real parsed issue by
    :func:`test_the_hand_built_issue_shape_matches_what_the_parser_emits`.
    """
    return {
        "verdict": VERDICT_PASS,
        "issues": [
            {
                "severity": severity,
                "description": "a finding in the generated transform",
                "attribution": ATTRIBUTION_TRANSFORM_BLOCK,
                "attribution_source": ATTRIBUTION_SOURCE_REVIEWER,
            }
        ],
        "categories": {},
    }


def test_the_hand_built_issue_shape_matches_what_the_parser_emits():
    """Arm A's hand-built fixture is the shape the parser really produces.

    Arm A is the one place in this file that does not route through
    ``parse_review_verdict``, for the reason given in
    :func:`_hand_built_reviewer_transform_block`. This pins the exception: the
    two attribution keys it sets are compared against a genuinely parsed
    explicit transform-block issue, so the arm cannot drift into testing a shape
    the parser would never emit — at which point its ``fail``/``pass`` results
    would say nothing about production.
    """
    token = sorted(NON_BLOCKING_SEVERITIES)[0]
    parsed = _parse(VERDICT_PASS, [_issue_doc("a finding in the generated transform", token, "transform_block")])
    parsed_issue = parsed["issues"][0]
    hand_built_issue = _hand_built_reviewer_transform_block(token)["issues"][0]

    assert parsed_issue["attribution"] == hand_built_issue["attribution"]
    assert parsed_issue["attribution_source"] == hand_built_issue["attribution_source"]
    assert _is_reviewer_transform_block_finding(parsed_issue)
    assert _is_reviewer_transform_block_finding(hand_built_issue)


# Feature: p43-review-gate-severity-floor, Property 5: Severity classification
# is exact equality.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(token=_ARM_A_TOKEN)
@example(token="minor ")
@example(token="Minor")
@example(token="minorly")
@example(token="min")
@example(token="sminor")
@example(token="minor")
def test_the_gate_blocks_every_severity_token_that_is_not_byte_equal_to_a_member(token):
    """**Validates: Requirements 1.5, 2.2** — Property 5, arm A.

    For any severity token whatsoever, the Review_Gate blocks a
    Reviewer_Transform_Block_Finding carrying it **unless** the token is
    byte-equal to a member of ``NON_BLOCKING_SEVERITIES``. No case folding, no
    stripping, no prefix match, no substring match — R1.5 in as many words.

    The five named near-misses are pinned as explicit examples rather than left
    to the draw: ``"minor "`` and ``"Minor"`` catch a gate that folds case or
    strips (which would duplicate work the parser already does at ``:225`` — the
    second normalisation layer ``LGR-ad78946-1``, **template / ratified**,
    rejected, on the ground that normalising removes the only signal that the
    value is not the intended one), and ``"minorly"``, ``"min"``, ``"sminor"``
    catch a gate testing ``startswith`` or ``in`` against the vocabulary.

    Arm B is the same claim on the parser's path, where those first two spellings
    legitimately become ``minor``. Both arms are needed because they disagree
    about those two tokens on purpose: normalisation is the parser's job and only
    the parser's.
    """
    verdict = _hand_built_reviewer_transform_block(token)
    expected = VERDICT_PASS if token in NON_BLOCKING_SEVERITIES else VERDICT_FAIL

    assert effective_verdict(verdict) == expected, (
        f"severity token {token!r} should be {'exempt' if expected == VERDICT_PASS else 'blocking'} at "
        f"the gate, because {'it is' if expected == VERDICT_PASS else 'it is not'} byte-equal to a member "
        f"of NON_BLOCKING_SEVERITIES ({sorted(NON_BLOCKING_SEVERITIES)!r}). R1.5 requires exact equality "
        "against the token the parser produced, with no further case folding, stripping, prefix or "
        "substring matching."
    )


# Feature: p43-review-gate-severity-floor, Property 5: Severity classification
# is exact equality.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(spelling=_NORMALISES_TO_NON_BLOCKING)
@example(spelling="minor ")
@example(spelling="Minor")
@example(spelling=" MINOR ")
def test_a_case_variant_or_padded_non_blocking_token_is_non_blocking_after_the_parser(spelling):
    """**Validates: Requirements 1.5, 2.2** — Property 5, arm B.

    For any case-variant or whitespace-padded spelling of a Non_Blocking_Severity
    token, passing it through the **real parser** first yields a non-blocking
    classification: the parser lowercases and strips at ``review_verdict.py:225``,
    so the gate receives the canonical token and its byte-equality test succeeds.

    This is the half arm A cannot state and must not: R2.2 requires the parser to
    emit the token "lowercased and stripped", and R1.5 requires the gate to do
    nothing further. Asserting both here is what pins normalisation to exactly one
    layer — ``LGR-ad78946-1`` (**template / ratified**) rejected a second one.

    A raw ``pass`` verdict is used so the classification is visible in the return
    value: rule 1 is the only route from a raw pass to ``fail`` (rule 4 returns
    the pass, rules 2/3 are guarded on a raw fail), so ``pass`` here means rule 1
    found the token exempt.
    """
    verdict = _parse(VERDICT_PASS, [_issue_doc("a naming nit in the transform", spelling, "transform_block")])
    issue = verdict["issues"][0]

    # R2.2: the parser did the normalising, and it produced a token byte-equal to
    # a member. Asserted, not assumed — if the parser stopped normalising, the
    # gate assertion below would fail for a reason this message would not name.
    assert issue["severity"] == spelling.strip().lower(), (
        f"the parser emitted {issue['severity']!r} for input {spelling!r}; R2.2 requires the token "
        "lowercased and stripped."
    )
    assert issue["severity"] in NON_BLOCKING_SEVERITIES, (
        f"spelling {spelling!r} normalised to {issue['severity']!r}, which is not in "
        f"{sorted(NON_BLOCKING_SEVERITIES)!r} — this arm's generator should only produce spellings that "
        "normalise INTO the set."
    )
    # The antecedent: the parser, not this test, made it a reviewer finding.
    assert _is_reviewer_transform_block_finding(issue), (
        f"the parser did not record ('transform_block', 'reviewer') for this issue: {issue!r}. Rule 1's "
        "attribution antecedent is unsatisfied, so this draw exercises rule 2 and the floor is untested."
    )

    assert effective_verdict(verdict) == VERDICT_PASS, (
        f"a raw 'pass' whose only reviewer transform-block finding was spelled {spelling!r} — normalising "
        f"to {issue['severity']!r}, a Non_Blocking_Severity — must not fail the gate (R1.3/R1.6). It "
        "did, which means rule 1 fired on an exempt token."
    )


@pytest.mark.parametrize("token", _NON_NORMALISING_NEAR_MISSES)
def test_a_prefix_or_substring_near_miss_still_blocks_after_the_parser(token):
    """Property 5's prefix/substring half, on the parser's own path.

    ``minorly``, ``min`` and ``sminor`` survive normalisation unchanged — the
    parser lowercases and strips but does not truncate or fuzzy-match — so they
    stay outside ``NON_BLOCKING_SEVERITIES`` and block. Arm A already covers them
    on hand-built dicts; this is the same attack where a real reviewer could
    actually land it, by mistyping the token in its own JSON.

    Named examples rather than a third property arm: three fixed tokens, and a
    parametrized id reads better in a failure report than a shrunk counterexample.
    """
    verdict = _parse(VERDICT_PASS, [_issue_doc("a finding in the generated transform", token, "transform_block")])
    issue = verdict["issues"][0]

    assert issue["severity"] == token, (
        f"the parser rewrote {token!r} to {issue['severity']!r}; this test's premise is that these three "
        "tokens pass through normalisation unchanged."
    )
    assert issue["severity"] not in NON_BLOCKING_SEVERITIES
    assert effective_verdict(verdict) == VERDICT_FAIL, (
        f"severity {token!r} is not byte-equal to any member of "
        f"{sorted(NON_BLOCKING_SEVERITIES)!r}, so it is an Unrecognised_Severity and must block (R1.2, "
        "R2.3). A gate matching by prefix or substring would wrongly exempt it."
    )


def test_a_missing_severity_blocks():
    """The absent-token edge of Property 5's exact-equality claim.

    ``issue.get("severity")`` is ``None`` on a hand-built dict with no severity
    key, and ``None`` is not byte-equal to any member, so the finding blocks.
    Unreachable through the parser, which requires a non-empty string
    (``:220-221``) — but the gate is called directly on hand-built dicts
    elsewhere in the suite, and the polarity of the comparison buys the
    conservative answer with no extra branch (design §4.1, the same fail-closed
    posture as ``LGR-69ca78b-1``, **invariant**).
    """
    verdict = {
        "verdict": VERDICT_PASS,
        "issues": [
            {
                "description": "a finding with no severity at all",
                "attribution": ATTRIBUTION_TRANSFORM_BLOCK,
                "attribution_source": ATTRIBUTION_SOURCE_REVIEWER,
            }
        ],
        "categories": {},
    }
    assert effective_verdict(verdict) == VERDICT_FAIL, (
        "a reviewer transform-block finding with no severity key must block: the exemption is an exact "
        "match against NON_BLOCKING_SEVERITIES, and a missing severity matches nothing."
    )


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------

#: Any string non-empty after stripping. Full ``st.text()`` rather than an
#: ASCII alphabet, because C4's fence is about tokens nobody enumerated — an
#: alphabet chosen here would be another enumeration.
_NON_EMPTY_AFTER_STRIP = st.text(min_size=1, max_size=40).filter(lambda s: bool(s.strip()))

#: Empty or whitespace-only. Both the fixed spellings and generated runs of
#: whitespace, so the raise arm is not three hand-picked strings.
_EMPTY_OR_WHITESPACE = st.one_of(
    st.just(""),
    st.text(alphabet=" \t\n\r\v\f", max_size=6),
)


# Feature: p43-review-gate-severity-floor, Property 6: The parser accepts every
# non-empty token.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(token=_NON_EMPTY_AFTER_STRIP, raw_verdict=st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))
@example(token="minor", raw_verdict=VERDICT_PASS)
@example(token="  Blocker  ", raw_verdict=VERDICT_PASS)
@example(token="\u00c9NORME", raw_verdict=VERDICT_FAIL)
@example(token="p1", raw_verdict=VERDICT_FAIL)
def test_the_parser_accepts_every_non_empty_severity_token(token, raw_verdict):
    """**Validates: Requirements 2.1, 2.2** — Property 6, accept arm.

    For any string non-empty after stripping, the Verdict_Parser emits a
    canonical issue carrying that token lowercased and stripped (R2.1, R2.2), on
    either raw verdict.

    This is C4's fence quantified: ``review_verdict.py:70-72`` states the intent
    in its own words — any non-empty token is accepted "so a novel severity
    cannot fail the step", because a raise there costs a reparse retry and then
    an honest step failure for a cosmetic spelling. do-NOT 5 forbids changing it.
    The severity floor makes an unvalidated field load-bearing at the *gate*
    (``requirements.md`` risk 3), and this property is what keeps that from
    leaking into the *step*: the two facts are separate, which is exactly what
    the comment block at ``:75-93`` was added to say unconfusably.

    "Cannot fail the step" is checked as two things, not one: the parse returns,
    and the gate then runs on the result without raising. A novel token that
    parsed and then broke the gate would satisfy a weaker reading of R2.1 while
    failing the run.
    """
    parsed = _parse(raw_verdict, [_issue_doc("a finding worth reporting", token, "transform_block")])
    issue = parsed["issues"][0]

    assert issue["severity"] == token.strip().lower(), (
        f"R2.2: the parser must carry severity {token!r} through lowercased and stripped; it emitted "
        f"{issue['severity']!r}."
    )
    assert issue["severity"], "R2.1: a token non-empty after stripping must not become an empty severity."
    # The gate accepts it too, so a novel token cannot fail the step by any route.
    assert effective_verdict(parsed) in {VERDICT_PASS, VERDICT_FAIL}


# Feature: p43-review-gate-severity-floor, Property 6: The parser accepts every
# non-empty token.
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(token=_EMPTY_OR_WHITESPACE, raw_verdict=st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))
@example(token="", raw_verdict=VERDICT_PASS)
@example(token="   ", raw_verdict=VERDICT_FAIL)
@example(token="\t\n", raw_verdict=VERDICT_PASS)
def test_the_parser_raises_on_an_empty_or_whitespace_severity_token(token, raw_verdict):
    """**Validates: Requirements 2.1, 2.2** — Property 6, raise arm.

    For any empty-or-whitespace severity the Verdict_Parser raises
    ``ReviewVerdictError``, naming the field. This is the boundary of the accept
    arm above and the reason "accepts everything" is not the same claim as
    "validates nothing": the schema still requires a severity to *be* something
    (module docstring, "non-empty string ``severity``"), it just does not
    require the something to be a token anybody enumerated.

    The distinction is load-bearing under the floor. A blank severity that parsed
    would reach the gate as ``""``, which is not in ``NON_BLOCKING_SEVERITIES``
    and so would block — the right answer, but arrived at silently, with the
    reviewer's malformed output never reported as malformed. The raise is what
    keeps the reparse retry (``:170-175``) reachable for a genuinely broken
    document.
    """
    with pytest.raises(ReviewVerdictError) as excinfo:
        _parse(raw_verdict, [_issue_doc("a finding worth reporting", token, "transform_block")])

    assert "severity" in str(excinfo.value), (
        f"the parser raised for an empty severity but did not name the field: {excinfo.value!r}. A reparse "
        "prompt built from this message has to be able to tell the model what was wrong."
    )


def test_the_severity_vocabulary_this_file_reasons_about_is_the_real_one():
    """The near-miss literals are near-misses of something that is actually in the set.

    Property 5's five named spellings are literals because the requirement names
    them as literals, so they cannot follow ``NON_BLOCKING_SEVERITIES`` if it
    changes. This is the tripwire: if ``minor`` ever leaves the set, or the set
    grows a member these five say nothing about, that is a signal to revisit the
    near-miss list rather than to let three of the strongest assertions in this
    file quietly become trivia.
    """
    assert "minor" in NON_BLOCKING_SEVERITIES, (
        "Property 5's near-miss spellings ('minor ', 'Minor', 'minorly', 'min', 'sminor') are all "
        f"near-misses of 'minor', which is no longer in NON_BLOCKING_SEVERITIES "
        f"({sorted(NON_BLOCKING_SEVERITIES)!r}). They no longer attack the prefix/substring half."
    )
    assert sorted(NON_BLOCKING_SEVERITIES) == ["minor"], (
        f"NON_BLOCKING_SEVERITIES is now {sorted(NON_BLOCKING_SEVERITIES)!r}. The near-miss list needs a "
        "spelling per member; the properties themselves already read the set and need no change."
    )
    # RECOMMENDED_SEVERITIES is prompt-facing documentation and no gate decision
    # reads it (:70-72) — asserted here only so the subset relation this file's
    # blocking-token pool assumes stays visible. Test 2.4b owns the claim.
    assert set(_rv.RECOMMENDED_SEVERITIES).issuperset(NON_BLOCKING_SEVERITIES)
