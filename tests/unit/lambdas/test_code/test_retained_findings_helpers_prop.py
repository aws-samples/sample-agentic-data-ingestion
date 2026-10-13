# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p43 task 4.2 — Property 12 over the retained/blocking finding helper pair.

What this file owns
-------------------
One property, three facts, over ``retained_nonblocking_findings`` and
``blocking_transform_block_findings`` (``review_verdict.py:345``, ``:367``):

* the two helpers **share no issue**;
* a non-empty **blocking** list implies the Review_Gate returned ``fail``;
* a non-empty **retained** list implies it returned ``pass``.

Why this is not bookkeeping
---------------------------
Task 12.1's log is an ``if retained: WARNING`` / ``elif blocking: INFO`` chain
(design §4.6). That chain is only correct if the two lists are mutually
exclusive — if a verdict could produce both, the ``elif`` would silently drop
the blocking line on exactly the runs where both facts matter. This file is that
precondition, quantified.

It is also the executable form of the C8 point design §4.4 makes in words: **an
effective ``fail`` is not the same fact as a blocking finding.** Rule 2
(``review_verdict.py:296-305``) produces the first without the second — a raw
``fail`` carrying one unattributed issue is an effective fail whose blocking
list is *empty*. A log line keyed on ``effective_verdict(...) == VERDICT_FAIL``
would label every such run as a blocking finding. That third fact is asserted
here too, as :func:`test_a_rule_2_effective_fail_carries_an_empty_blocking_list`
and its named companion, because it is the case the naive keying gets wrong and
nothing else in the suite pins it.

Two generators, and why both
----------------------------
**Through the real parser** (:func:`_parsed_verdicts`). The reviewer-attributed
draws go through ``parse_review_verdict`` so ``attribution_source`` comes out
``reviewer`` because the parser put it there, not because the test hand-set it.
Rule 1's attribution antecedent is half of what these helpers read, so a
hand-set ``attribution_source`` would make the antecedent an artifact of the
test. Same technique as task 5.1's Property 2
(``test_review_gate_calibration.py:1030-1052``).

**Hand-built dicts** (:func:`_hand_built_verdicts`). The helpers are documented
to accept "a canonical verdict or a ``parsed_results`` dict", and
``aggregate_check_results`` calls the gate on ``parsed_results`` at
``lambda_function.py:1152`` — so the ``parsed_results`` shape is drawn too. The
hand-built arm also reaches three shapes the parser cannot emit, all of which
the helpers must survive: a dict with **neither** ``verdict`` nor ``issues``
(exactly ``test_comprehensive_report.py:151-153``), ``issues: None``, and an
issue with **no severity key** — which blocks, because
``None not in frozenset({"minor"})`` is True (design §4.1, the same fail-closed
polarity as ``LGR-69ca78b-1``).

The two arms disagree about one thing on purpose, and Property 12 holds either
way: ``"Minor"`` and ``"minor "`` are normalised to ``minor`` by the parser
(``review_verdict.py:203``) and so are *retained*, while the same spellings
hand-built onto a dict are never normalised and so *block*. Which of the two is
right is Property 5's subject (task 5.3), not this file's.

Scope note: the structural claims about these helpers — no ``logger``, no
execution-record key — belong to task 4.3
(``test_review_verdict_purity_source.py``), and the no-mutation claim is
Property 18's (``test_reviewer_path_invariance_prop.py:709-712``). This file
asserts behaviour only.
"""

from __future__ import annotations

import json
import os
import string
import sys
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)

# Imported after the sys.path insert above, which is what makes it resolvable.
# review_verdict.py is pure stdlib (its module docstring, :56-58), so no AWS or
# strands stubbing is needed here — unlike the handler-level gate test files.
import review_verdict as _rv
from review_verdict import (
    NON_BLOCKING_SEVERITIES,
    VERDICT_FAIL,
    VERDICT_PASS,
    blocking_transform_block_findings,
    effective_verdict,
    parse_review_verdict,
    retained_nonblocking_findings,
    verdict_to_parsed_results,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Non-empty after the parser's ``strip()``, which is what
#: ``_validate_verdict_document`` requires of a description
#: (``review_verdict.py:218-219``).
_ISSUE_TEXT = (
    st.text(alphabet=string.ascii_letters + string.digits + " .,", min_size=1, max_size=30).map(str.strip).filter(bool)
)

#: The Non_Blocking_Severity tokens, read off the real frozenset rather than
#: restated, so this cannot drift from the gate's own vocabulary if the set ever
#: grows a second tier.
_NON_BLOCKING_SEVERITY = st.sampled_from(sorted(NON_BLOCKING_SEVERITIES))

#: Tokens that block: the two recommended blocking severities, two
#: Unrecognised_Severity tokens, and three near-misses on ``minor``. The
#: near-misses are here because the helpers' severity test is membership in a
#: frozenset — a prefix or substring match would classify them as non-blocking
#: and break the disjointness this file asserts.
_BLOCKING_SEVERITY = st.sampled_from(("critical", "major", "blocker", "nit", "minorly", "min", "sminor"))

#: The whole severity axis, spanning both sides of the floor plus the two
#: spellings the parser normalises INTO the non-blocking set.
_ANY_SEVERITY = st.one_of(
    _NON_BLOCKING_SEVERITY,
    _BLOCKING_SEVERITY,
    st.sampled_from(("Minor", "minor ", " MINOR ")),
)

#: What a reviewer can put in the optional ``attribution`` field. ``None`` means
#: the key is omitted entirely. The recognised tokens (in any casing) yield
#: ``attribution_source == "reviewer"``; everything else defaults to
#: ``("transform_block", "default")`` — which rule 1 deliberately does NOT match
#: (``review_verdict.py:269-289``).
_ATTRIBUTION_INPUT = st.sampled_from(
    (
        None,
        "transform_block",
        "TRANSFORM_BLOCK",
        " transform_block ",
        "template_scaffolding",
        "Template_Scaffolding",
        "generated",
        "",
        "transform-block",
    )
)


def _issue_doc(description: str, severity: str, attribution: str | None) -> dict[str, Any]:
    """A raw reviewer issue as it appears in the model's JSON, pre-parse."""
    doc: dict[str, Any] = {"severity": severity, "description": description}
    if attribution is not None:
        doc["attribution"] = attribution
    return doc


@st.composite
def _parsed_verdicts(draw) -> dict[str, Any]:
    """Draw a canonical verdict through the REAL parser.

    The point of routing through ``parse_review_verdict`` rather than building
    the canonical shape directly: ``attribution_source`` is then whatever the
    parser decided, so rule 1's attribution antecedent — and therefore both
    helpers' first conjunct — is real rather than asserted into existence.

    A raw ``fail`` with no issues is rejected by the parser
    (``review_verdict.py:247-250``), so the issue list is drawn non-empty on
    that branch.
    """
    raw_verdict = draw(st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))
    issue_docs = draw(
        st.lists(
            st.builds(_issue_doc, _ISSUE_TEXT, _ANY_SEVERITY, _ATTRIBUTION_INPUT),
            min_size=1 if raw_verdict == VERDICT_FAIL else 0,
            max_size=5,
        )
    )
    categories = draw(
        st.dictionaries(
            st.sampled_from(("Correctness", "Security", "Performance")),
            st.sampled_from(("pass", "fail")),
            max_size=3,
        )
    )
    doc = {"verdict": raw_verdict, "issues": issue_docs, "categories": categories}
    verdict = parse_review_verdict(json.dumps(doc))
    # Half the draws go through the adapter, because that is the shape
    # ``aggregate_check_results`` hands the gate (``lambda_function.py:1152``).
    return verdict_to_parsed_results(verdict) if draw(st.booleans()) else verdict


@st.composite
def _hand_built_verdicts(draw) -> dict[str, Any]:
    """Draw a hand-built dict, including shapes the parser cannot produce.

    Deliberately reaches: an issue with **no severity key** (blocks, by the
    fail-closed polarity of ``not in``), an issue with ``attribution_source``
    absent (does not block — the reviewer never said so), ``issues: None``,
    ``issues`` omitted, ``verdict`` omitted, and a dict carrying neither key.
    """
    shape = draw(st.sampled_from(("issues", "none", "omitted", "foreign")))
    if shape == "foreign":
        # Exactly ``test_comprehensive_report.py:151-153`` — no gate call
        # reaches it in production, and both helpers must return ``[]``.
        return {"test_categories": {}, "recommendation": "REJECT"}
    if shape == "none":
        return {"verdict": draw(st.sampled_from((VERDICT_PASS, VERDICT_FAIL))), "issues": None}
    if shape == "omitted":
        return {"verdict": draw(st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))}

    issues = draw(
        st.lists(
            st.fixed_dictionaries(
                {"description": _ISSUE_TEXT},
                optional={
                    "severity": _ANY_SEVERITY,
                    "attribution": st.sampled_from(("transform_block", "template_scaffolding", "generated")),
                    "attribution_source": st.sampled_from(("reviewer", "default", "somewhere_else")),
                },
            ),
            max_size=5,
        )
    )
    body: dict[str, Any] = {"issues": issues}
    if draw(st.booleans()):
        body["verdict"] = draw(st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))
    return body


def _assert_property_12(verdict: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """The three facts of Property 12, asserted over one verdict.

    Returns the two lists so a caller can add its own non-vacuity assertions.
    """
    retained = retained_nonblocking_findings(verdict)
    blocking = blocking_transform_block_findings(verdict)
    effective = effective_verdict(verdict)

    # Fact 1 — no shared issue. Identity, not equality: "share no issue" is a
    # claim about the same issue object landing in both lists, and two issues in
    # one verdict can legitimately be equal in value.
    retained_ids = {id(issue) for issue in retained}
    blocking_ids = {id(issue) for issue in blocking}
    assert not (retained_ids & blocking_ids), f"an issue is both retained and blocking: {verdict!r}"
    # The stronger value-level form also holds, and is what a reader of the log
    # would rely on: the severity conjunct is complementary, so no retained
    # issue can even *look* like a blocking one.
    assert all(r != b for r in retained for b in blocking), f"a retained issue equals a blocking issue: {verdict!r}"

    # Fact 2 — a non-empty blocking list means rule 1 fired.
    if blocking:
        assert effective == VERDICT_FAIL, f"blocking findings without an effective fail: {verdict!r}"

    # Fact 3 — a non-empty retained list means the gate passed.
    if retained:
        assert effective == VERDICT_PASS, f"retained findings without an effective pass: {verdict!r}"

    return retained, blocking


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------


# Feature: p43-review-gate-severity-floor, Property 12: Retained and blocking
# are complementary and exclusive.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(verdict=_parsed_verdicts())
def test_retained_and_blocking_are_complementary_and_exclusive(verdict):
    """**Validates: Requirements 5.1, 5.2** — Property 12, through the real parser.

    For any verdict the parser can emit — either raw verdict, any severity on
    either side of the floor, any attribution including unrecognised, absent and
    the casings the parser normalises — ``retained_nonblocking_findings`` and
    ``blocking_transform_block_findings`` share no issue, a non-empty blocking
    list implies the gate returned ``fail``, and a non-empty retained list
    implies it returned ``pass``.

    Design §4.6's ``if``/``elif`` retention log is correct only if these two are
    exclusive, which is why this is a precondition and not a description.

    The reviewer-attributed draws carry ``attribution_source == "reviewer"``
    because ``parse_review_verdict`` put it there, and that is checked below
    rather than assumed: if the parser stopped setting it, every draw would
    become defaulted, both lists would be empty everywhere, and the property
    would pass while testing nothing.
    """
    retained, blocking = _assert_property_12(verdict)

    # Non-vacuity: every issue in either list came out of the parser with
    # reviewer provenance, so rule 1's attribution antecedent was really met.
    for issue in retained + blocking:
        assert issue["attribution"] == _rv.ATTRIBUTION_TRANSFORM_BLOCK
        assert issue["attribution_source"] == _rv.ATTRIBUTION_SOURCE_REVIEWER


# Feature: p43-review-gate-severity-floor, Property 12: Retained and blocking
# are complementary and exclusive.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(verdict=_hand_built_verdicts())
def test_retained_and_blocking_are_exclusive_on_hand_built_dicts(verdict):
    """**Validates: Requirements 5.1, 5.2** — Property 12 off the parser's path.

    The same three facts over dicts assembled by hand, which is how the helpers
    are actually reached in two places: ``parsed_results`` at
    ``lambda_function.py:1152``, and the report's directly-constructed
    ``{"test_categories": {}, "recommendation": "REJECT"}``
    (``test_comprehensive_report.py:151-153``), which carries neither
    ``verdict`` nor ``issues``.

    Nothing here is normalised, so ``"Minor"`` blocks in this arm and is
    retained in the arm above. Both are Property 12-conformant; which one is
    correct behaviour is Property 5's question (task 5.3).
    """
    _assert_property_12(verdict)


# Feature: p43-review-gate-severity-floor, Property 12: Retained and blocking
# are complementary and exclusive.
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    unattributed=st.lists(st.tuples(_ISSUE_TEXT, _ANY_SEVERITY), min_size=1, max_size=3),
    scaffolding=st.lists(st.tuples(_ISSUE_TEXT, _ANY_SEVERITY), max_size=2),
)
def test_a_rule_2_effective_fail_carries_an_empty_blocking_list(unattributed, scaffolding):
    """**Validates: Requirements 5.1, 5.2** — the C8 fact, quantified.

    For any raw ``fail`` verdict carrying at least one issue the reviewer left
    unattributed, at any severity: the Review_Gate returns ``fail`` through
    rule 2, and the blocking list is **empty**. An effective ``fail`` is
    therefore not evidence of a blocking finding.

    This is the case design §4.4 says a log keyed on
    ``effective_verdict(...) == VERDICT_FAIL`` would mislabel, and it is why
    task 12.1's ``elif`` keys on the blocking list instead. Retained is empty
    too, by ``retained_nonblocking_findings``' own guard: on a failed run
    nothing was retained.
    """
    docs = [_issue_doc(text, severity, None) for text, severity in unattributed]
    docs += [_issue_doc(text, severity, "template_scaffolding") for text, severity in scaffolding]
    verdict = parse_review_verdict(json.dumps({"verdict": VERDICT_FAIL, "issues": docs, "categories": {}}))

    # The antecedent is real: the parser defaulted the unattributed issues, so
    # rule 1 cannot see them (``review_verdict.py:269-289``).
    defaulted = [i for i in verdict["issues"] if i["attribution_source"] == _rv.ATTRIBUTION_SOURCE_DEFAULT]
    assert len(defaulted) == len(unattributed)

    assert effective_verdict(verdict) == VERDICT_FAIL
    assert blocking_transform_block_findings(verdict) == []
    assert retained_nonblocking_findings(verdict) == []


def test_both_helpers_reach_a_non_empty_result():
    """Property 12's implications are not vacuous.

    Two fixed verdicts, one per side of the floor, showing that both lists are
    reachable at all. Without this, the two ``if``-guarded facts above could
    hold on an empty-everywhere space and say nothing.
    """
    retained_run = parse_review_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "issues": [{"severity": "minor", "description": "Naming nit.", "attribution": "transform_block"}],
                "categories": {},
            }
        )
    )
    assert len(retained_nonblocking_findings(retained_run)) == 1
    assert blocking_transform_block_findings(retained_run) == []
    assert effective_verdict(retained_run) == VERDICT_PASS

    blocking_run = parse_review_verdict(
        json.dumps(
            {
                "verdict": "pass",
                "issues": [
                    {"severity": "critical", "description": "Wrong join key.", "attribution": "transform_block"}
                ],
                "categories": {},
            }
        )
    )
    assert len(blocking_transform_block_findings(blocking_run)) == 1
    assert retained_nonblocking_findings(blocking_run) == []
    assert effective_verdict(blocking_run) == VERDICT_FAIL
