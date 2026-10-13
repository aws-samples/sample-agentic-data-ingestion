# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p43 task 5.2 — the R7.5 severity x raw-verdict matrix for the Review_Gate.

What this file owns
-------------------
Eight named cells, and nothing else: ``critical`` / ``major`` / ``minor`` / one
Unrecognised_Severity, crossed with a raw ``pass`` and a raw ``fail``, each
asserting the Effective_Verdict **and the rule that produced it**.

Why an explicit matrix when the properties cover the space more broadly
-----------------------------------------------------------------------
Properties 1-6 (``test_review_verdict_severity_floor_prop.py``,
``test_review_gate_calibration.py``) quantify over far more shapes than eight
cells do, and they are the stronger guarantee. This file is not competing with
them: **a named cell reads better in a failure report than a shrunk
counterexample.** ``[minor-raw_fail]`` in a pytest header says which of the four
severities on which of the two raw verdicts moved; a hypothesis falsifying
example says an issue list did. R7.5 names this coverage explicitly, so it is
not optional, and design §6's example table gives the reason in the same words.

Where each expectation came from
--------------------------------
**Not from running the gate and recording what it returned** — that oracle
cannot detect the gate drifting (``LGR-28b4d75-5``, **template**). Every cell is
read off the four-row table in ``requirements.md`` § "The floor is narrower than
it first appears", which gives the outcome, and design §4.1, which gives the
route:

===============  ============  =================  =========  ==========================================
severity         raw verdict   Effective_Verdict  route      authority
===============  ============  =================  =========  ==========================================
``critical``     ``pass``      ``fail``           rule 1     table row 2 (any critical/major); R1.1
``major``        ``pass``      ``fail``           rule 1     table row 2; R1.1
``minor``        ``pass``      ``pass``           rule 4     table row 1 (the one cell that INVERTS); R1.3/R1.6
unrecognised     ``pass``      ``fail``           rule 1     table row 2 (any unrecognised); R1.2
``critical``     ``fail``      ``fail``           rule 1     R1.1 + R1.4 (rule 1 ignores the raw verdict)
``major``        ``fail``      ``fail``           rule 1     R1.1 + R1.4
``minor``        ``fail``      ``fail``           rules 2/3  table row 3; R1.4 — **the asymmetry**
unrecognised     ``fail``      ``fail``           rule 1     R1.2 + R1.4
===============  ============  =================  =========  ==========================================

The asymmetry is the thing this file exists to hold still
--------------------------------------------------------
Seven cells fail and one passes, which makes the matrix look like it is testing
one bit. It is not. On a raw ``fail`` a ``minor`` finding **still fails, by a
different route**: rule 1 skips it (severity exempt), rule 4's guard is
``verdict.get("verdict") != VERDICT_FAIL`` and so does not apply, and it lands on
rules 2/3, which exempt a raw ``fail`` only when *every* issue is explicitly
``template_scaffolding`` — a transform-block finding is not, so the ``all()`` is
False and the gate returns ``fail`` (design §4.1, "Where the minor-on-raw-fail
case goes, read on disk"). Requirement 1.4 holds through that route, which is
why the assertions on that shape elsewhere survived p43 with only their
docstrings amended (design §4.7 rows 8, 12, A).

An outcome-only matrix would pass unchanged if someone "simplified" the floor
into rule 4's branch, or exempted ``minor`` from rules 2/3 as well — the second
would be a real weakening of R1.4 with seven of eight cells still green. So the
route is asserted, not just documented.

How the route is asserted without reading the gate's internals
--------------------------------------------------------------
By the one structural difference between the rules, taken from
``effective_verdict``'s own docstring (``review_verdict.py``, rules 1-4): **rule
1 fires regardless of the raw verdict; rules 2, 3 and 4 are all conditioned on
it.** So re-evaluating the *same issue list* under a raw ``pass`` answers "does
rule 1 fire?" — a raw ``pass`` that comes back ``fail`` can only have failed
through rule 1, because rule 4 returns ``pass`` for everything rule 1 did not
already catch, and rules 2/3 are unreachable on a raw ``pass``.

Rule 1's condition reads only the issue (attribution, provenance, severity), so
"rule 1 fires on this issue list" is the same fact under either raw verdict, and
the probe transfers. That makes this a metamorphic route check derived from the
documented rule order rather than from the function body — no ``inspect``, no
private helper, and nothing that breaks when the implementation is refactored
while keeping its documented semantics.

Fixtures come through the real parser, deliberately
---------------------------------------------------
Every cell is built by ``parse_review_verdict`` from a JSON document, following
the technique in ``test_review_verdict_attribution.py`` (task 5.1). A hand-built
dict would carry whatever ``attribution_source`` the test author typed; the
parser is what makes it ``"reviewer"`` (``:220-222``), and **that is a load-
bearing part of every cell**. A dict with ``attribution_source`` absent or
``"default"`` is not a Reviewer_Transform_Block_Finding at all: it fails rule 1's
attribution antecedent, reaches rule 2 instead, and the matrix would silently be
measuring rule 2's behaviour across severities — every cell would still be green
and the floor would be untested. The provenance is asserted per cell rather than
assumed, so that substitution cannot happen quietly.

NO FIXTURES
-----------
Nothing here needs one: one pure-stdlib module, no AWS client, no moto mock, no
Lambda environment, so no fixture from ``tests/fixtures/conftest.py`` applies.
The root ``tests/conftest.py`` still applies ``@pytest.mark.unit`` by location.

**Validates: Requirements 7.5**
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)

# Imported after the sys.path insert above, which is what makes it resolvable.
from review_verdict import (
    NON_BLOCKING_SEVERITIES,
    effective_verdict,
    parse_review_verdict,
)

#: The Unrecognised_Severity cell's token. ``nit`` rather than ``blocker``
#: (which the sibling in ``test_review_verdict_attribution.py`` already uses) on
#: purpose: the complement encoding means a token that *reads* as trivial still
#: blocks, and that is the half of R1.2 a reader is most likely to get wrong.
#: A test whose unrecognised token is named "blocker" would be satisfied by an
#: implementation that special-cased the word.
_UNRECOGNISED_SEVERITY = "nit"

#: Route labels. Which rule produced the cell's Effective_Verdict, per
#: ``effective_verdict``'s docstring order 1 -> 4 -> 2/3.
_RULE_1 = "rule 1 (reviewer transform-block at a Blocking_Severity)"
_RULE_4 = "rule 4 (a raw pass stays a pass)"
_RULES_2_3 = "rules 2/3 (a raw fail is exempted only by all-explicit-scaffolding)"


def _reviewer_transform_block_doc(verdict: str, severity: str) -> str:
    """A reviewer verdict document with one explicitly-attributed transform-block issue.

    ``attribution`` is spelled in the document, not on a dict, so the parser sets
    ``attribution_source = "reviewer"`` itself. See the module docstring for why
    that cannot be short-circuited.
    """
    return json.dumps(
        {
            "verdict": verdict,
            "issues": [
                {
                    "severity": severity,
                    "description": f"a {severity} finding in the generated transform",
                    "attribution": "transform_block",
                }
            ],
            "categories": {},
        }
    )


def _rule_1_fires(severity: str) -> bool:
    """Does rule 1 fire on a lone reviewer transform-block issue at ``severity``?

    Answered by evaluating the same issue shape under a raw ``pass``. Per
    ``effective_verdict``'s documented rules, rule 1 is the only one that can
    return ``fail`` for a raw ``pass``: rules 2/3 are guarded on a raw ``fail``,
    and rule 4 returns ``pass`` for everything rule 1 did not catch. Rule 1's
    condition reads only the issue, so the answer transfers to the raw-``fail``
    cells — which is how those cells assert their route without inspecting the
    gate.
    """
    probe = parse_review_verdict(_reviewer_transform_block_doc("pass", severity))
    return effective_verdict(probe) == "fail"


# ---------------------------------------------------------------------------
# R7.5 — the eight cells
# ---------------------------------------------------------------------------

_MATRIX = [
    # (severity, raw verdict, expected Effective_Verdict, expected route)
    pytest.param("critical", "pass", "fail", _RULE_1, id="critical-raw_pass"),
    pytest.param("major", "pass", "fail", _RULE_1, id="major-raw_pass"),
    pytest.param("minor", "pass", "pass", _RULE_4, id="minor-raw_pass"),
    pytest.param(_UNRECOGNISED_SEVERITY, "pass", "fail", _RULE_1, id="unrecognised-raw_pass"),
    pytest.param("critical", "fail", "fail", _RULE_1, id="critical-raw_fail"),
    pytest.param("major", "fail", "fail", _RULE_1, id="major-raw_fail"),
    pytest.param("minor", "fail", "fail", _RULES_2_3, id="minor-raw_fail"),
    pytest.param(_UNRECOGNISED_SEVERITY, "fail", "fail", _RULE_1, id="unrecognised-raw_fail"),
]


@pytest.mark.parametrize(("severity", "raw_verdict", "expected", "route"), _MATRIX)
def test_severity_and_raw_verdict_produce_the_expected_effective_verdict(severity, raw_verdict, expected, route):
    """R7.5: one cell of the severity x raw-verdict matrix, outcome and route.

    Expectations are read off ``requirements.md``'s four-row table and design
    §4.1's route analysis — see this module's docstring for the cell-by-cell
    provenance. They are **not** recorded from what the gate returns.

    The single inverting cell is ``minor`` on a raw ``pass``: rule 1 no longer
    fires and rule 4 returns ``pass`` (R1.3/R1.6). ``minor`` on a raw ``fail``
    still fails, and the route assertion is what pins that it fails through
    rules 2/3 rather than through a floor that quietly stopped applying (R1.4).
    """
    parsed = parse_review_verdict(_reviewer_transform_block_doc(raw_verdict, severity))

    # The fixture is a Reviewer_Transform_Block_Finding because the PARSER said
    # so. Asserted, not assumed: with `attribution_source == "default"` this cell
    # would exercise rule 2 and the floor would be untested (module docstring).
    issue = parsed["issues"][0]
    assert issue["attribution"] == "transform_block", (
        f"the parser did not record a transform_block attribution for the {severity!r} cell "
        f"(got {issue.get('attribution')!r}). Rule 1's attribution antecedent is unsatisfied, so this "
        "cell is no longer testing the severity floor."
    )
    assert issue["attribution_source"] == "reviewer", (
        f"the parser recorded attribution_source={issue.get('attribution_source')!r} for the "
        f"{severity!r} cell, not 'reviewer'. Only an EXPLICIT reviewer attribution reaches rule 1 "
        "(review_verdict.py's attribution default yields ('transform_block', 'default'), which reaches "
        "rule 2). Every cell in this matrix would still be green while the floor went untested."
    )
    assert issue["severity"] == severity, (
        f"the parser normalised severity {severity!r} to {issue['severity']!r}. The matrix's cells are "
        "keyed on the token the gate sees, so a normalisation change here invalidates the cell."
    )
    # Raw_Verdict is passed through, so 'raw pass' and 'raw fail' mean what the
    # cell says they mean (R8.1, pinned at test_review_gate_calibration.py:370-376).
    assert parsed["verdict"] == raw_verdict, (
        f"the parser rewrote the raw verdict {raw_verdict!r} to {parsed['verdict']!r}; this cell's "
        "'raw pass' / 'raw fail' axis no longer holds."
    )

    assert effective_verdict(parsed) == expected, (
        f"Effective_Verdict for a reviewer transform-block finding at severity {severity!r} on a raw "
        f"{raw_verdict!r} verdict should be {expected!r}, via {route}. Expected from requirements.md's "
        "four-row table ('The floor is narrower than it first appears') and design §4.1 — not from what "
        "the gate currently returns. If this cell moved, either the floor was recalibrated again or one "
        f"of rules 2/3/4 changed (R1.7 forbids the latter). NON_BLOCKING_SEVERITIES is "
        f"{sorted(NON_BLOCKING_SEVERITIES)!r}."
    )


@pytest.mark.parametrize(("severity", "raw_verdict", "expected", "route"), _MATRIX)
def test_severity_and_raw_verdict_reach_that_verdict_by_the_expected_route(severity, raw_verdict, expected, route):
    """R7.5: the cell's outcome arrives by the rule the requirements say it does.

    The asymmetry R7.5's coverage is really about: on a raw ``fail``, ``minor``
    still returns ``fail`` — but through rules 2/3, not through rule 1. Outcome
    alone cannot see the difference, so seven of eight cells would stay green if
    ``minor`` were also exempted from rules 2/3, which would weaken R1.4.

    Route is derived from the one structural difference in
    ``effective_verdict``'s documented rules: rule 1 ignores the raw verdict,
    rules 2/3/4 do not. Re-evaluating the same issue shape under a raw ``pass``
    therefore answers whether rule 1 fires — see ``_rule_1_fires``. No private
    helper is called and no source is inspected, so a refactor that preserves the
    documented semantics does not disturb this.
    """
    rule_1_fires = _rule_1_fires(severity)

    if route == _RULE_1:
        assert rule_1_fires, (
            f"severity {severity!r} should be a Blocking_Severity, so rule 1 should fire on it "
            f"regardless of the raw verdict (R1.1/R1.2) — but the same issue shape on a raw 'pass' "
            "came back 'pass', which means rule 1 did not fire. Blocking_Severity is the COMPLEMENT of "
            f"NON_BLOCKING_SEVERITIES ({sorted(NON_BLOCKING_SEVERITIES)!r}), so an unrecognised token "
            "blocks too; if this cell moved, something was added to that set."
        )
        return

    assert not rule_1_fires, (
        f"severity {severity!r} is in NON_BLOCKING_SEVERITIES, so rule 1 must NOT fire on it "
        f"(R1.3) — but the same issue shape on a raw 'pass' came back 'fail'. This cell's documented "
        f"route is {route}."
    )

    if route == _RULE_4:
        # Rule 1 exempted it and the raw verdict is a pass, so rule 4 returns it
        # unchanged. Rules 2/3 are unreachable here — their guard is a raw fail.
        assert raw_verdict == "pass" and expected == "pass", (
            f"the matrix labels this cell {route} but it is ({raw_verdict!r} -> {expected!r}); rule 4 "
            "only ever returns 'pass', and only for a raw pass. The cell's route label is wrong."
        )
        return

    # _RULES_2_3: the asymmetry. Rule 1 skipped it (asserted above) and rule 4's
    # guard excludes a raw fail, so the fail arrives through rules 2/3 — a
    # transform-block finding is not explicit scaffolding, so nothing exempts the
    # raw fail (design §4.1). R1.4 holds by this route, not by the floor.
    assert route == _RULES_2_3, f"unknown route label {route!r}"
    assert raw_verdict == "fail" and expected == "fail", (
        f"the matrix labels this cell {route} but it is ({raw_verdict!r} -> {expected!r}); rules 2/3 "
        "are reachable only on a raw fail. The cell's route label is wrong."
    )
    # Non-vacuity for the route claim: rules 2/3 exempt a raw fail only when
    # EVERY issue is explicitly template_scaffolding. Re-attributing this same
    # non-blocking severity as scaffolding must therefore flip the outcome to
    # 'pass' — which shows the fail really did come from the scaffolding test and
    # not from some other unconditional path.
    scaffolding_only = parse_review_verdict(
        json.dumps(
            {
                "verdict": raw_verdict,
                "issues": [
                    {
                        "severity": severity,
                        "description": "the same finding, attributed to the template instead",
                        "attribution": "template_scaffolding",
                    }
                ],
                "categories": {},
            }
        )
    )
    assert effective_verdict(scaffolding_only) == "pass", (
        f"a raw {raw_verdict!r} whose only issue is at severity {severity!r} AND explicitly attributed "
        "to template_scaffolding should pass by rule 3 (P28b R1.3, untouched by p43 per R1.7). It did "
        "not, so the transform-block cell above may be failing through some path other than the "
        "all-explicit-scaffolding test, and its route label is unverified."
    )
