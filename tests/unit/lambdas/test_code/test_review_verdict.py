# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: structured review verdict parsing (P28 R13.1, task 11.2).

The review model must return a JSON verdict ``{verdict, issues, categories}``;
``parse_review_verdict`` schema-validates it and raises ``ReviewVerdictError``
on any violation (the handler performs exactly one reparse retry, then fails
honestly). ``verdict_to_parsed_results`` adapts the canonical verdict to the
``parsed_results`` shape downstream consumers read — the single parser, no
free-text extraction.

Validates: P28 Requirements 13.1, 13.2
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
    MAX_DESCRIPTION_CHARS,
    MAX_ISSUES,
    ReviewVerdictError,
    build_reparse_prompt,
    parse_review_verdict,
    verdict_to_parsed_results,
)


def _doc(verdict="pass", issues=None, categories=None, **extra) -> dict:
    doc = {"verdict": verdict, "issues": issues if issues is not None else [], "categories": categories or {}}
    doc.update(extra)
    return doc


# ---------------------------------------------------------------------------
# Valid documents
# ---------------------------------------------------------------------------


def test_parses_bare_json_pass_verdict():
    parsed = parse_review_verdict(json.dumps(_doc("pass", categories={"Correctness": "pass"})))
    assert parsed["verdict"] == "pass"
    assert parsed["issues"] == []
    assert parsed["categories"] == {"Correctness": "pass"}


def test_parses_fail_verdict_with_issues():
    doc = _doc(
        "fail",
        issues=[{"severity": "critical", "category": "Security", "description": "Hardcoded credentials."}],
        categories={"Security": "fail"},
    )
    parsed = parse_review_verdict(json.dumps(doc))
    assert parsed["verdict"] == "fail"
    assert parsed["issues"] == [
        {
            "severity": "critical",
            "category": "Security",
            "description": "Hardcoded credentials.",
            # P28b R1b additive fields: no reviewer attribution → conservative default.
            "attribution": "transform_block",
            "attribution_source": "default",
        }
    ]
    assert parsed["categories"] == {"Security": "fail"}


def test_parses_json_inside_markdown_fence():
    text = "```json\n" + json.dumps(_doc("pass")) + "\n```"
    assert parse_review_verdict(text)["verdict"] == "pass"


def test_parses_json_embedded_in_prose():
    text = "Here is my review:\n" + json.dumps(_doc("pass")) + "\nHope that helps!"
    assert parse_review_verdict(text)["verdict"] == "pass"


def test_normalizes_verdict_case_and_category_values():
    doc = {"verdict": "PASS", "issues": [], "categories": {"Correctness": "PASSED", "Security": "Failed"}}
    parsed = parse_review_verdict(json.dumps(doc))
    assert parsed["verdict"] == "pass"
    assert parsed["categories"] == {"Correctness": "pass", "Security": "fail"}


def test_missing_categories_key_is_tolerated():
    parsed = parse_review_verdict(json.dumps({"verdict": "pass", "issues": []}))
    assert parsed["categories"] == {}


def test_bounds_issue_count_and_description_length():
    issues = [{"severity": "minor", "description": "x" * (MAX_DESCRIPTION_CHARS + 500)} for _ in range(MAX_ISSUES + 10)]
    parsed = parse_review_verdict(json.dumps(_doc("fail", issues=issues)))
    assert len(parsed["issues"]) == MAX_ISSUES
    assert all(len(i["description"]) == MAX_DESCRIPTION_CHARS for i in parsed["issues"])


# ---------------------------------------------------------------------------
# Schema violations → ReviewVerdictError (never a fabricated verdict)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_input",
    [
        None,
        "",
        "   ",
        "Great code, no issues found, production ready!",  # prose, no JSON
        "no syntax errors found",  # the R13.2 poster child — NOT a verdict
        json.dumps(["pass"]),  # not an object
        json.dumps({"issues": []}),  # missing verdict
        json.dumps({"verdict": "maybe", "issues": []}),  # bad verdict value
        json.dumps({"verdict": "pass"}),  # missing issues
        json.dumps({"verdict": "pass", "issues": "none"}),  # non-list issues
        json.dumps({"verdict": "fail", "issues": []}),  # fail without issues
        json.dumps({"verdict": "fail", "issues": [{"severity": "major"}]}),  # issue missing description
        json.dumps({"verdict": "fail", "issues": [{"description": "x"}]}),  # issue missing severity
        json.dumps({"verdict": "fail", "issues": ["broken"]}),  # non-object issue
        json.dumps({"verdict": "pass", "issues": [], "categories": {"Correctness": "partial"}}),  # bad category value
        json.dumps({"verdict": "pass", "issues": [], "categories": "all good"}),  # non-object categories
    ],
)
def test_schema_violations_raise(bad_input):
    with pytest.raises(ReviewVerdictError):
        parse_review_verdict(bad_input)


# ---------------------------------------------------------------------------
# verdict_to_parsed_results adapter — the single downstream shape
# ---------------------------------------------------------------------------


def test_adapter_builds_test_categories_and_recommendation():
    parsed = verdict_to_parsed_results(
        parse_review_verdict(json.dumps(_doc("pass", categories={"Correctness": "pass", "Security": "fail"})))
    )
    assert parsed["verdict"] == "pass"
    assert parsed["test_categories"]["Correctness"] == {"status": "PASSED", "passed": True}
    assert parsed["test_categories"]["Security"] == {"status": "FAILED", "passed": False}
    assert parsed["recommendation"] == "APPROVE"


def test_adapter_fail_verdict_maps_to_reject_recommendation():
    doc = _doc("fail", issues=[{"severity": "major", "description": "bad"}])
    parsed = verdict_to_parsed_results(parse_review_verdict(json.dumps(doc)))
    assert parsed["recommendation"] == "REJECT"
    assert parsed["issues"] == [
        {
            "severity": "major",
            "description": "bad",
            # P28b R1b additive fields pass through the adapter unchanged.
            "attribution": "transform_block",
            "attribution_source": "default",
        }
    ]


# ---------------------------------------------------------------------------
# Reparse prompt (the single retry re-asks the model)
# ---------------------------------------------------------------------------


def test_reparse_prompt_carries_previous_response_and_error():
    prompt = build_reparse_prompt("NOT JSON AT ALL", "no JSON object found in review output")
    assert "NOT JSON AT ALL" in prompt
    assert "no JSON object found" in prompt
    assert '"verdict"' in prompt  # shows the required schema
