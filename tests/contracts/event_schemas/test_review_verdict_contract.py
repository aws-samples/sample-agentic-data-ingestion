# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract tests: structured review verdict schema (P28 R13.1, task 11.2).

The review model (prompted by ``test_analysis.j2`` / ``test_analysis_fallback.j2``)
and the test_code gate communicate through ONE schema::

    {
        "verdict": "pass" | "fail",
        "issues": [{"severity": str, "description": str, "category": str?}],
        "categories": {"<name>": "pass" | "fail"}
    }

``review_verdict.parse_review_verdict`` is the single parser: everything it
accepts satisfies the canonical shape below (the gate, the regeneration
trigger, and the state row all consume it), and every schema violation
raises — there is no fabricated-verdict path.

**Validates: Requirements 13.1, 13.2**
"""

import json
import os
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_test_code"),
)

from review_verdict import (
    ALLOWED_ATTRIBUTION_SOURCES,
    ALLOWED_ATTRIBUTIONS,
    ALLOWED_VERDICTS,
    MAX_DESCRIPTION_CHARS,
    MAX_ISSUES,
    REVIEW_CATEGORIES,
    ReviewVerdictError,
    effective_verdict,
    parse_review_verdict,
    verdict_to_parsed_results,
)

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared", "prompt_templates")


# ── Generators (valid documents per the schema) ──────────────────────────

_severities = st.sampled_from(["critical", "major", "minor"])
_descriptions = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=1, max_size=200
).filter(lambda s: s.strip())

# Attribution inputs (P28b R1b): the field is ADDITIVE OPTIONAL — absent,
# valid in any case, or garbage (wrong tokens, wrong types) must all parse.
_ABSENT = object()
_valid_attributions = st.sampled_from(
    ["transform_block", "template_scaffolding", "Transform_Block", "TEMPLATE_SCAFFOLDING", "Template_Scaffolding"]
)
_garbage_attributions = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.lists(st.sampled_from(["transform_block", "x"]), max_size=2),
    st.text(
        alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=30
    ).filter(lambda s: s.strip().lower() not in ALLOWED_ATTRIBUTIONS),
)

_issues = st.builds(
    lambda severity, description, category, attribution: {
        "severity": severity,
        "description": description,
        **({"category": category} if category else {}),
        **({} if attribution is _ABSENT else {"attribution": attribution}),
    },
    severity=_severities,
    description=_descriptions,
    category=st.one_of(st.none(), st.sampled_from(REVIEW_CATEGORIES)),
    attribution=st.one_of(st.just(_ABSENT), _valid_attributions, _garbage_attributions),
)

_categories = st.dictionaries(
    st.sampled_from(REVIEW_CATEGORIES),
    st.sampled_from(["pass", "fail", "PASS", "FAIL", "passed", "failed"]),
    max_size=len(REVIEW_CATEGORIES),
)


@st.composite
def _valid_documents(draw):
    verdict = draw(st.sampled_from(["pass", "fail", "PASS", "FAIL", "Pass", "Fail"]))
    min_issues = 1 if verdict.lower() == "fail" else 0
    issues = draw(st.lists(_issues, min_size=min_issues, max_size=5))
    doc = {"verdict": verdict, "issues": issues, "categories": draw(_categories)}
    return doc


# ── Contract: accepted documents always yield the canonical shape ─────────


@given(doc=_valid_documents())
def test_accepted_documents_yield_canonical_shape(doc):
    """**Validates: Requirements 13.1**

    For ANY schema-valid verdict document, the parsed output carries exactly
    the canonical keys with normalized values — the shape the gate, the
    regeneration trigger, and the state row consume.
    """
    parsed = parse_review_verdict(json.dumps(doc))

    assert set(parsed) == {"verdict", "issues", "categories"}
    assert parsed["verdict"] in ALLOWED_VERDICTS
    assert parsed["verdict"] == doc["verdict"].lower()
    assert len(parsed["issues"]) <= MAX_ISSUES
    for issue in parsed["issues"]:
        assert set(issue) <= {"severity", "description", "category", "attribution", "attribution_source"}
        # P28b R1b: the attribution pair is ALWAYS present on canonical issues.
        assert {"attribution", "attribution_source"} <= set(issue)
        assert issue["severity"] and issue["severity"] == issue["severity"].lower()
        assert issue["description"] and len(issue["description"]) <= MAX_DESCRIPTION_CHARS
    for name, value in parsed["categories"].items():
        assert name in REVIEW_CATEGORIES
        assert value in ALLOWED_VERDICTS
    # The R13.3 invariant: a fail verdict always carries issues for the
    # regeneration prompt.
    if parsed["verdict"] == "fail":
        assert parsed["issues"]


@given(doc=_valid_documents())
def test_every_parsed_issue_carries_attribution_with_conservative_default(doc):
    """**Validates: Requirements 1.5, 1.6** (P28b R1b)

    For ANY schema-valid document — attribution absent, valid in any case,
    or garbage — every parsed issue carries
    ``attribution ∈ {transform_block, template_scaffolding}`` AND
    ``attribution_source ∈ {reviewer, default}``. A valid reviewer value is
    normalized case-insensitively with source ``reviewer``; absent/invalid
    values take the conservative default ``(transform_block, default)``.
    """
    parsed = parse_review_verdict(json.dumps(doc))

    raw_issues = doc["issues"][:MAX_ISSUES]
    assert len(parsed["issues"]) == len(raw_issues)
    for raw_issue, issue in zip(raw_issues, parsed["issues"], strict=True):
        assert issue["attribution"] in ALLOWED_ATTRIBUTIONS
        assert issue["attribution_source"] in ALLOWED_ATTRIBUTION_SOURCES
        raw_attribution = raw_issue.get("attribution")
        if isinstance(raw_attribution, str) and raw_attribution.strip().lower() in ALLOWED_ATTRIBUTIONS:
            assert issue["attribution"] == raw_attribution.strip().lower()
            assert issue["attribution_source"] == "reviewer"
        else:
            assert issue["attribution"] == "transform_block"
            assert issue["attribution_source"] == "default"


@given(doc=_valid_documents())
def test_downstream_parsed_results_shape(doc):
    """**Validates: Requirements 13.1** — the adapter output is the stable
    shape downstream consumers read (aggregation, report, test_summary).

    P28b R1b: ``recommendation`` derives from the Issue_Attribution-calibrated
    ``effective_verdict`` (a scaffolding-only fail recommends APPROVE with its
    issues retained; an explicit reviewer transform-block finding recommends
    REJECT even on a raw pass) — no longer from the raw verdict.
    """
    canonical = parse_review_verdict(json.dumps(doc))
    parsed = verdict_to_parsed_results(canonical)

    assert set(parsed) == {"verdict", "issues", "test_categories", "recommendation"}
    # The raw verdict passes through unchanged; the recommendation is the
    # calibrated gate decision.
    assert parsed["verdict"] == canonical["verdict"]
    assert parsed["issues"] == canonical["issues"]
    assert parsed["recommendation"] == ("APPROVE" if effective_verdict(canonical) == "pass" else "REJECT")
    for info in parsed["test_categories"].values():
        assert set(info) == {"status", "passed"}
        assert info["status"] in {"PASSED", "FAILED"}
        assert info["passed"] == (info["status"] == "PASSED")


@given(doc=_valid_documents(), prefix=st.sampled_from(["", "Here is my review:\n", "```json\n"]))
def test_wrapping_tolerance_is_part_of_the_contract(doc, prefix):
    """Fenced or prose-wrapped JSON parses to the same verdict."""
    suffix = "\n```" if prefix.startswith("```") else ""
    parsed = parse_review_verdict(prefix + json.dumps(doc) + suffix)
    assert parsed["verdict"] == doc["verdict"].lower()


# ── Contract: violations raise, never fabricate ──────────────────────────


@pytest.mark.parametrize(
    "violation",
    [
        "no syntax errors found",  # free text is NOT a verdict (R13.2)
        json.dumps({"verdict": "approve", "issues": []}),  # non-enum verdict
        json.dumps({"verdict": "fail", "issues": []}),  # fail without issues
        json.dumps({"verdict": "pass"}),  # missing issues
        json.dumps({"verdict": "pass", "issues": [], "categories": {"Correctness": "mostly"}}),
    ],
)
def test_schema_violations_raise(violation):
    with pytest.raises(ReviewVerdictError):
        parse_review_verdict(violation)


# ── Contract: the prompts demand the schema the parser enforces ──────────


@pytest.mark.parametrize("template", ["test_analysis.j2", "test_analysis_fallback.j2"])
def test_prompt_templates_specify_the_json_verdict_contract(template):
    """The system prompts and the parser must stay in sync: both prompt
    templates demand the JSON object with verdict/issues/categories and the
    pass|fail vocabulary, and no longer ask for the old markdown report."""
    with open(os.path.join(_TEMPLATE_DIR, template), encoding="utf-8") as f:
        content = f.read()

    assert '"verdict"' in content
    assert '"issues"' in content
    assert '"categories"' in content
    assert "pass" in content and "fail" in content
    for category in REVIEW_CATEGORIES:
        assert category in content
    # P28b R1b: the issue schema carries the attribution field, the marker
    # rule names both attribution values, and the scaffolding exemption is
    # stated (scaffolding findings must not alone produce a fail verdict).
    assert '"attribution"' in content
    for attribution in sorted(ALLOWED_ATTRIBUTIONS):
        assert attribution in content
    assert "# === AI-Generated Transform Block ===" in content
    # The old free-text report format is gone from the prompt.
    assert "Overall Status:" not in content
    assert "Coverage:" not in content
