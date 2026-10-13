# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: Issue_Attribution parsing + effective_verdict gate rules (P28b R1b).

``parse_review_verdict`` normalizes the additive optional per-issue
``attribution`` field case-insensitively; every canonical issue emerges with
``attribution ∈ {transform_block, template_scaffolding}`` and
``attribution_source ∈ {reviewer, default}``; absent/garbage attribution
defaults conservatively to ``(transform_block, default)``.

``effective_verdict`` is the calibrated gate decision (rules 1-4):

1. explicit reviewer transform_block issue **at a Blocking_Severity** — any
   severity NOT in ``NON_BLOCKING_SEVERITIES`` — → fail, regardless of raw
   verdict;
2. raw fail with any issue NOT explicitly scaffolding-attributed → fail;
3. raw fail with every issue explicitly scaffolding-attributed → pass
   (issues retained, not discarded);
4. raw pass → pass (the conservative default may never strengthen a pass
   into a fail).

**Validates: Requirements 1.3, 1.4, 1.5, 1.6**

RECALIBRATED by spec ``p43-review-gate-severity-floor``
-------------------------------------------------------

Rule 1 above used to read "regardless of severity or raw verdict". p43 R1.1/R1.2
put a severity floor inside its condition, so a ``minor`` reviewer
transform-block finding is now **retained but not blocking** (p43 R1.3/R1.6) —
calibrating P28b R1.4 rather than removing it. The rule-1 assertions below are
**amended, never deleted**, each recording at its own site what reversed and what
route the outcome now arrives by, under ``LGR-60c4985-6``'s (**template /
ratified**) amend-don't-delete discipline and ``LGR-5906ec6-2``'s (**template /
ratified**) convention that a deliberate hold is encoded as a test assertion. The
hold being reversed is stated at ``test_review_gate_calibration.py:726-733`` — the
line range as it stood; that paragraph is itself now amended in place, under the
section header "Why the property is about the ATTRIBUTION and the severity FLOOR",
which is where to read the full reversal. Cited by range and by heading so the
pointer survives the file moving under it.
"""

import json
import os
import sys

import pytest

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)

from review_verdict import (
    MAX_ISSUES,
    ReviewVerdictError,
    effective_verdict,
    parse_review_verdict,
)


def _doc(verdict="pass", issues=None, categories=None) -> str:
    return json.dumps(
        {"verdict": verdict, "issues": issues if issues is not None else [], "categories": categories or {}}
    )


def _issue(description="an issue", severity="minor", **fields) -> dict:
    return {"severity": severity, "description": description, **fields}


# ---------------------------------------------------------------------------
# Parse-side attribution normalization (R1.5, R1.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["template_scaffolding", "Template_Scaffolding", "TEMPLATE_SCAFFOLDING", "  template_scaffolding  "]
)
def test_reviewer_scaffolding_attribution_normalized_case_insensitively(raw):
    parsed = parse_review_verdict(_doc("fail", issues=[_issue(attribution=raw)]))
    assert parsed["issues"][0]["attribution"] == "template_scaffolding"
    assert parsed["issues"][0]["attribution_source"] == "reviewer"


@pytest.mark.parametrize("raw", ["transform_block", "Transform_Block", "TRANSFORM_BLOCK", " transform_block "])
def test_reviewer_transform_block_attribution_normalized_case_insensitively(raw):
    parsed = parse_review_verdict(_doc("fail", issues=[_issue(attribution=raw)]))
    assert parsed["issues"][0]["attribution"] == "transform_block"
    assert parsed["issues"][0]["attribution_source"] == "reviewer"


def test_absent_attribution_defaults_to_transform_block():
    parsed = parse_review_verdict(_doc("fail", issues=[_issue()]))
    assert parsed["issues"][0]["attribution"] == "transform_block"
    assert parsed["issues"][0]["attribution_source"] == "default"


@pytest.mark.parametrize(
    "garbage",
    ["scaffolding", "boilerplate", "", "   ", "template scaffolding", None, 42, True, ["template_scaffolding"], {}],
)
def test_garbage_attribution_defaults_to_transform_block(garbage):
    """Any non-normalizable attribution value → the conservative default —
    a schema violation is NOT raised (the field is additive optional)."""
    parsed = parse_review_verdict(_doc("fail", issues=[_issue(attribution=garbage)]))
    assert parsed["issues"][0]["attribution"] == "transform_block"
    assert parsed["issues"][0]["attribution_source"] == "default"


def test_r13_machinery_untouched_by_attribution():
    """The fail-needs-issues rule and the issue bound survive: attribution is
    additive and changes no R13 schema rule (R1.6)."""
    with pytest.raises(ReviewVerdictError):
        parse_review_verdict(_doc("fail", issues=[]))

    many = [_issue(f"issue {i}", attribution="template_scaffolding") for i in range(MAX_ISSUES + 10)]
    parsed = parse_review_verdict(_doc("fail", issues=many))
    assert len(parsed["issues"]) == MAX_ISSUES
    assert all(
        i["attribution"] == "template_scaffolding" and i["attribution_source"] == "reviewer"
        for i in parsed["issues"]
    )


# ---------------------------------------------------------------------------
# effective_verdict — rule 1 (R1.4, as calibrated by p43 R1.1/R1.2): an explicit
# transform_block finding at a Blocking_Severity fails, regardless of raw verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["critical", "major", "minor"])
def test_rule1_explicit_transform_block_issue_fails_raw_fail_any_severity(severity):
    """A raw ``fail`` carrying an explicit transform-block finding stays ``fail``
    at every severity — including a Non_Blocking_Severity.

    **Docstring-only amendment: the outcome is unchanged, the ROUTE changed for
    one arm.** Before p43, all three arms failed through rule 1. Now the
    ``critical`` and ``major`` arms still fail through rule 1, while the ``minor``
    arm reaches rules 2/3 instead (``review_verdict.py:281-290``): the raw verdict
    is ``fail`` and the sole issue is not explicitly scaffolding-attributed, so
    the conservative default keeps it failing. p43 R1.4 is exactly this — the
    floor may not weaken a raw ``fail``.

    No assertion is touched, because none of them was wrong. Recorded here rather
    than left silent, under ``LGR-60c4985-6`` (**template / ratified**), whose
    discipline is that a reversal stays legible as a reversal at its own site; the
    hold reversed is stated at ``test_review_gate_calibration.py:726-733``.
    """
    parsed = parse_review_verdict(
        _doc("fail", issues=[_issue(severity=severity, attribution="transform_block")])
    )
    assert effective_verdict(parsed) == "fail"


def test_rule1_does_not_fail_a_raw_pass_at_minor_severity():
    """**INVERTED by p43 R1.3/R1.6.** A reviewer transform-block finding at a
    Non_Blocking_Severity is retained and displayed, and does NOT strengthen a raw
    ``pass`` into a ``fail``.

    What this assertion said before: ``== "fail"``, under the name
    ``test_rule1_explicit_transform_block_issue_fails_even_on_raw_pass``, on this
    same ``severity="minor"`` fixture. It encoded P28b R1.4's severity-blindness —
    rule 1 firing on any reviewer transform-block finding whatever its severity.
    p43 R1.1/R1.2 put a severity floor inside rule 1's condition, calibrating that
    rule rather than removing it, so this fixture now takes rule 4.

    The claim the old name stood for is not lost: rule 1 is still the ONLY path
    that strengthens a raw ``pass`` into a ``fail``, and the sibling below keeps
    asserting it — now qualified, because the path requires a Blocking_Severity.
    Amended and renamed rather than deleted, per ``LGR-60c4985-6`` (**template /
    ratified**), citing ``LGR-5906ec6-2`` (**template / ratified**) for the
    convention that a deliberate hold lives as a test assertion. The hold reversed
    is stated at ``test_review_gate_calibration.py:726-733``.
    """
    parsed = parse_review_verdict(
        _doc("pass", issues=[_issue(severity="minor", attribution="transform_block")])
    )
    assert effective_verdict(parsed) == "pass"
    # Retained, not discarded (p43 R1.3): the finding is still on the verdict,
    # still reviewer-attributed, and so still satisfies rule 1's ATTRIBUTION
    # antecedent — only the severity conjunct exempts it.
    assert parsed["issues"][0]["attribution"] == "transform_block"
    assert parsed["issues"][0]["attribution_source"] == "reviewer"


@pytest.mark.parametrize("severity", ["critical", "major", "blocker"])
def test_rule1_explicit_transform_block_issue_fails_a_raw_pass_at_blocking_severity(severity):
    """Rule 1 precedes rule 4: an explicit reviewer transform-block finding at a
    Blocking_Severity strengthens even a raw pass into a fail — still the ONLY
    strengthening path, now qualified by the severity floor (p43 R1.1/R1.2).

    The sibling that keeps the claim its neighbour above used to make, so no case
    is lost to the inversion. ``blocker`` is an Unrecognised_Severity and blocks
    because the floor is encoded as the COMPLEMENT of ``NON_BLOCKING_SEVERITIES``
    (p43 R1.5).
    """
    parsed = parse_review_verdict(_doc("pass", issues=[_issue(severity=severity, attribution="transform_block")]))
    assert effective_verdict(parsed) == "fail"


def test_rule1_explicit_transform_block_among_scaffolding_issues_fails():
    """A raw ``fail`` with a mixed issue list stays ``fail``.

    **Docstring-only amendment: outcome identical, route changed.** Both issues
    take ``_issue``'s default ``severity="minor"`` (``:48``), so under p43
    R1.1/R1.2 rule 1 no longer fires on the transform-block one. The verdict still
    fails, through rules 2/3: not every issue is explicitly
    scaffolding-attributed, so nothing exempts the raw ``fail``.

    A 14th amendment site the requirements' enumeration did not name, found by
    sweeping the file rather than by working the list. No assertion is touched;
    the route change is recorded here under ``LGR-60c4985-6`` (**template /
    ratified**), and the hold reversed is stated at
    ``test_review_gate_calibration.py:726-733``.
    """
    parsed = parse_review_verdict(
        _doc(
            "fail",
            issues=[
                _issue("scaffolding nit", attribution="template_scaffolding"),
                _issue("transform bug", attribution="transform_block"),
            ],
        )
    )
    assert effective_verdict(parsed) == "fail"


# ---------------------------------------------------------------------------
# effective_verdict — rule 2 (R1.5): raw fail + any issue not explicitly
# scaffolding-attributed → fail (the conservative default cannot weaken)
# ---------------------------------------------------------------------------


def test_rule2_raw_fail_with_unattributed_issue_stays_fail():
    parsed = parse_review_verdict(_doc("fail", issues=[_issue()]))
    assert effective_verdict(parsed) == "fail"


def test_rule2_raw_fail_with_mixed_scaffolding_and_unattributed_stays_fail():
    parsed = parse_review_verdict(
        _doc(
            "fail",
            issues=[
                _issue("scaffolding nit", attribution="template_scaffolding"),
                _issue("unattributed finding"),
            ],
        )
    )
    assert effective_verdict(parsed) == "fail"


def test_rule2_defaulted_scaffolding_value_does_not_exempt():
    """A scaffolding attribution NOT sourced from the reviewer must not
    exempt a fail verdict — exemption requires explicit reviewer provenance."""
    verdict = {
        "verdict": "fail",
        "issues": [
            {
                "severity": "minor",
                "description": "x",
                "attribution": "template_scaffolding",
                "attribution_source": "default",
            }
        ],
        "categories": {},
    }
    assert effective_verdict(verdict) == "fail"


def test_rule2_conservative_guard_raw_fail_without_issues_stays_fail():
    """Unreachable through the parser (fail-needs-issues), but the pure
    function stays conservative: no issues → no exempting attribution."""
    assert effective_verdict({"verdict": "fail", "issues": [], "categories": {}}) == "fail"


# ---------------------------------------------------------------------------
# effective_verdict — rule 3 (R1.3): raw fail, every issue explicitly
# scaffolding → pass, with the issues retained
# ---------------------------------------------------------------------------


def test_rule3_all_explicit_scaffolding_issues_pass_the_gate():
    parsed = parse_review_verdict(
        _doc(
            "fail",
            issues=[
                _issue("Glue boilerplate uses getResolvedOptions", attribution="template_scaffolding"),
                _issue("Spark session handling in template", attribution="Template_Scaffolding", severity="major"),
            ],
        )
    )
    assert effective_verdict(parsed) == "pass"
    # The calibration retains the issues — they are not discarded.
    assert len(parsed["issues"]) == 2
    assert all(i["attribution"] == "template_scaffolding" for i in parsed["issues"])


# ---------------------------------------------------------------------------
# effective_verdict — rule 4: raw pass → pass (the default never strengthens)
# ---------------------------------------------------------------------------


def test_rule4_raw_pass_with_no_issues_passes():
    parsed = parse_review_verdict(_doc("pass"))
    assert effective_verdict(parsed) == "pass"


def test_rule4_raw_pass_with_unattributed_minor_issue_stays_pass():
    """Mirrors the R13.4 regression pin: a pass verdict with an unattributed
    minor issue must NOT be strengthened into a fail by the default."""
    parsed = parse_review_verdict(
        _doc("pass", issues=[_issue("No syntax errors found; style is acceptable.")])
    )
    assert parsed["issues"][0]["attribution_source"] == "default"
    assert effective_verdict(parsed) == "pass"


def test_rule4_raw_pass_with_explicit_scaffolding_issue_stays_pass():
    parsed = parse_review_verdict(
        _doc("pass", issues=[_issue("scaffolding note", attribution="template_scaffolding")])
    )
    assert effective_verdict(parsed) == "pass"
