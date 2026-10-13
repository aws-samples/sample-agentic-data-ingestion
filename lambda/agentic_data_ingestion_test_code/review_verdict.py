# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Structured review verdict: schema, parser, and reparse prompt (P28 R13).

The code-review model is instructed (``test_analysis.j2`` /
``test_analysis_fallback.j2``) to return ONLY a JSON object:

.. code-block:: json

    {
        "verdict": "pass" | "fail",
        "issues": [
            {
                "severity": "critical" | "major" | "minor",
                "description": "<1-2 sentences>",
                "category": "<optional review category>"
            }
        ],
        "categories": {
            "Correctness": "pass" | "fail",
            "Schema Compliance": "pass" | "fail",
            "Error Handling": "pass" | "fail",
            "AWS Best Practices": "pass" | "fail",
            "Security": "pass" | "fail",
            "Performance": "pass" | "fail"
        }
    }

Schema contract (what :func:`parse_review_verdict` enforces):

* ``verdict`` — required; string; case-normalized to ``"pass"`` or ``"fail"``.
* ``issues`` — required; list (may be empty ONLY when the verdict is
  ``"pass"``). A ``fail`` verdict without issues is rejected: regeneration
  must be able to include the reviewer's issues in the new prompt (R13.3),
  so an issue-less failure carries no actionable signal and is treated as a
  malformed verdict. Each issue requires non-empty string ``severity``
  (lowercased; ``critical``/``major``/``minor`` recommended but any
  non-empty token accepted) and non-empty string ``description``.
  ``category`` is optional. Issue lists/descriptions are bounded
  (:data:`MAX_ISSUES`, :data:`MAX_DESCRIPTION_CHARS`) so the verdict stays
  DynamoDB-safe.
* ``attribution`` — ADDITIVE OPTIONAL per-issue field (P28b R1b,
  Issue_Attribution). Case-insensitively normalized to
  ``"transform_block"`` or ``"template_scaffolding"``; every canonical
  issue emerges carrying ``attribution`` plus its provenance
  ``attribution_source`` (``"reviewer"`` when the reviewer supplied a
  valid value, ``"default"`` otherwise). Absent or garbage attribution
  defaults conservatively to ``("transform_block", "default")`` — missing
  attribution can never weaken the gate (R1.5).
* ``categories`` — optional; mapping of category name → per-category
  verdict. Values accept ``pass``/``fail``/``passed``/``failed`` in any
  case; anything else is a schema violation (no silent coercion).

The P28 R13.2 point: the verdict is a machine-readable field, never a
substring scan of free text — "no syntax errors found" cannot match
anything here.

This module is pure stdlib (json/re) so the AgentCore container and the
Lambda share it with zero extra dependencies (the Dockerfile copies the
whole Lambda directory).
"""

import json
import re
from typing import Any

# Verdict values (the ONLY gate vocabulary — R13.1).
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
ALLOWED_VERDICTS = frozenset({VERDICT_PASS, VERDICT_FAIL})

#: Recommended issue severities — documented, not enforced. Any non-empty token
#: is accepted so a novel severity cannot fail the step. PROMPT-FACING: this
#: tuple mirrors what the reviewer prompt asks for and NO gate decision reads it.
RECOMMENDED_SEVERITIES = ("critical", "major", "minor")

#: Severities that do NOT block the Review_Gate. LOAD-BEARING: rule 1 of
#: :func:`effective_verdict` reads this set, and Blocking_Severity is its
#: COMPLEMENT — an explicit reviewer transform-block finding blocks unless its
#: severity is listed here.
#:
#: Two different statements about a novel token, both true, and the reason this
#: comment is separate from the one above:
#:   * a novel token cannot fail the STEP — the parser accepts any non-empty
#:     string, so there is no reparse retry and no honest step failure for a
#:     cosmetic spelling;
#:   * a novel token DOES block the GATE — it is not in this set, so rule 1
#:     treats it as blocking.
#: The complement encoding is what makes that conservative by construction:
#: widening the reviewer's severity vocabulary can never silently weaken the
#: gate, because a token nobody listed here still blocks.
#:
#: Keep this a subset of RECOMMENDED_SEVERITIES: a token here the prompt never
#: asks for is unreachable, and a typo here is a dead calibration that fails
#: closed and silently.
NON_BLOCKING_SEVERITIES = frozenset({"minor"})

# Issue attribution (P28b R1b): which artifact region a finding concerns.
# ``transform_block`` — the AI-generated transform (the code the gate judges);
# ``template_scaffolding`` — pre-reviewed template boilerplate the AI didn't write.
ATTRIBUTION_TRANSFORM_BLOCK = "transform_block"
ATTRIBUTION_TEMPLATE_SCAFFOLDING = "template_scaffolding"
ALLOWED_ATTRIBUTIONS = frozenset({ATTRIBUTION_TRANSFORM_BLOCK, ATTRIBUTION_TEMPLATE_SCAFFOLDING})

#: Attribution provenance: ``reviewer`` — the reviewer supplied a valid
#: attribution; ``default`` — the conservative parser default was applied.
ATTRIBUTION_SOURCE_REVIEWER = "reviewer"
ATTRIBUTION_SOURCE_DEFAULT = "default"
ALLOWED_ATTRIBUTION_SOURCES = frozenset({ATTRIBUTION_SOURCE_REVIEWER, ATTRIBUTION_SOURCE_DEFAULT})

#: Review categories the prompt asks for (display data for the dashboard).
REVIEW_CATEGORIES = (
    "Correctness",
    "Schema Compliance",
    "Error Handling",
    "AWS Best Practices",
    "Security",
    "Performance",
)

#: Bounds keeping the parsed verdict DynamoDB/state-item safe.
MAX_ISSUES = 50
MAX_DESCRIPTION_CHARS = 2000

_CATEGORY_VALUE_MAP = {
    "pass": VERDICT_PASS,
    "passed": VERDICT_PASS,
    "fail": VERDICT_FAIL,
    "failed": VERDICT_FAIL,
}

# ```json ... ``` or ``` ... ``` fenced block (first one wins).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class ReviewVerdictError(Exception):
    """The review model output is not a schema-valid structured verdict."""


def _json_candidates(text: str) -> list[str]:
    """Candidate JSON substrings, most-specific first.

    1. content of the first markdown code fence (models love fences),
    2. the first ``{`` through the last ``}`` (JSON embedded in prose),
    3. the raw text as-is.
    """
    candidates = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    candidates.append(text.strip())
    return candidates


def parse_review_verdict(review_text: Any) -> dict[str, Any]:
    """Extract and schema-validate the structured review verdict.

    Args:
        review_text: Raw review-model output (ideally a bare JSON object;
            fenced or prose-wrapped JSON is tolerated).

    Returns:
        Canonical verdict dict::

            {
                "verdict": "pass" | "fail",
                "issues": [{"severity": str, "description": str,
                            "category": str?,
                            "attribution": "transform_block"
                                | "template_scaffolding",
                            "attribution_source": "reviewer" | "default"},
                           ...],
                "categories": {"<name>": "pass" | "fail", ...},
            }

    Raises:
        ReviewVerdictError: On any parse or schema violation. The caller
            (test_code handler) performs exactly ONE reparse retry with
            :func:`build_reparse_prompt`, then fails the step honestly.
    """
    if not isinstance(review_text, str) or not review_text.strip():
        raise ReviewVerdictError("review output is empty or not text")

    doc = None
    for candidate in _json_candidates(review_text):
        try:
            doc = json.loads(candidate)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if doc is None:
        raise ReviewVerdictError("no JSON object found in review output")

    return _validate_verdict_document(doc)


def _validate_verdict_document(doc: Any) -> dict[str, Any]:
    """Validate a decoded JSON document against the verdict schema."""
    if not isinstance(doc, dict):
        raise ReviewVerdictError(f"verdict document must be a JSON object, got {type(doc).__name__}")

    raw_verdict = doc.get("verdict")
    if not isinstance(raw_verdict, str):
        raise ReviewVerdictError("missing or non-string 'verdict' field")
    verdict = raw_verdict.strip().lower()
    if verdict not in ALLOWED_VERDICTS:
        raise ReviewVerdictError(f"'verdict' must be one of {sorted(ALLOWED_VERDICTS)}, got {raw_verdict!r}")

    raw_issues = doc.get("issues")
    if not isinstance(raw_issues, list):
        raise ReviewVerdictError("missing or non-list 'issues' field")

    issues: list[dict[str, str]] = []
    for idx, raw_issue in enumerate(raw_issues[:MAX_ISSUES]):
        if not isinstance(raw_issue, dict):
            raise ReviewVerdictError(f"issues[{idx}] must be an object")
        severity = raw_issue.get("severity")
        description = raw_issue.get("description")
        if not isinstance(severity, str) or not severity.strip():
            raise ReviewVerdictError(f"issues[{idx}].severity must be a non-empty string")
        if not isinstance(description, str) or not description.strip():
            raise ReviewVerdictError(f"issues[{idx}].description must be a non-empty string")
        issue: dict[str, str] = {
            "severity": severity.strip().lower(),
            "description": description.strip()[:MAX_DESCRIPTION_CHARS],
        }
        category = raw_issue.get("category")
        if isinstance(category, str) and category.strip():
            issue["category"] = category.strip()
        # Issue_Attribution (P28b R1b): normalize the additive optional
        # reviewer field case-insensitively; absent/garbage values default
        # conservatively to the transform block so missing attribution can
        # never weaken the gate (R1.5). ``attribution_source`` records the
        # provenance so the gate can distinguish an explicit reviewer
        # attribution from the default.
        raw_attribution = raw_issue.get("attribution")
        normalized_attribution = raw_attribution.strip().lower() if isinstance(raw_attribution, str) else ""
        if normalized_attribution in ALLOWED_ATTRIBUTIONS:
            issue["attribution"] = normalized_attribution
            issue["attribution_source"] = ATTRIBUTION_SOURCE_REVIEWER
        else:
            issue["attribution"] = ATTRIBUTION_TRANSFORM_BLOCK
            issue["attribution_source"] = ATTRIBUTION_SOURCE_DEFAULT
        issues.append(issue)

    if verdict == VERDICT_FAIL and not issues:
        # R13.3: regeneration includes the reviewer's issues — a fail verdict
        # carrying none is unactionable and treated as malformed.
        raise ReviewVerdictError("a 'fail' verdict must include at least one issue")

    raw_categories = doc.get("categories", {})
    if raw_categories is None:
        raw_categories = {}
    if not isinstance(raw_categories, dict):
        raise ReviewVerdictError("'categories' must be an object when present")
    categories: dict[str, str] = {}
    for name, value in raw_categories.items():
        if not isinstance(name, str) or not name.strip():
            raise ReviewVerdictError("category names must be non-empty strings")
        normalized = _CATEGORY_VALUE_MAP.get(value.strip().lower()) if isinstance(value, str) else None
        if normalized is None:
            raise ReviewVerdictError(f"categories[{name!r}] must be 'pass' or 'fail', got {value!r}")
        categories[name.strip()] = normalized

    return {"verdict": verdict, "issues": issues, "categories": categories}


def _is_reviewer_transform_block(issue: dict[str, Any]) -> bool:
    """A Reviewer_Transform_Block_Finding: the reviewer itself said transform_block.

    Both halves of rule 1's attribution antecedent, written down ONCE:
    ``attribution == "transform_block"`` AND ``attribution_source ==
    "reviewer"`` — i.e. the reviewer supplied a recognised attribution rather
    than the parser defaulting it (see the attribution default in
    :func:`_validate_verdict_document`, which yields
    ``("transform_block", "default")`` and is deliberately NOT matched here).

    Read by rule 1 of :func:`effective_verdict` and by the retained/blocking
    finding helpers; three copies of the condition would be three places for it
    to drift. Comparison is exact equality against the already-normalised token
    the parser emits, with no second normalisation layer (``LGR-ad78946-1``).

    NOT the predicate rules 2/3 use — those test
    :data:`ATTRIBUTION_TEMPLATE_SCAFFOLDING`, a different condition, and are
    untouched by it.
    """
    return (
        issue.get("attribution") == ATTRIBUTION_TRANSFORM_BLOCK
        and issue.get("attribution_source") == ATTRIBUTION_SOURCE_REVIEWER
    )


def effective_verdict(verdict: dict[str, Any]) -> str:
    """Gate decision calibrated by Issue_Attribution (P28b R1b).

    Pure function over a canonical verdict dict (the
    :func:`parse_review_verdict` output shape). Returns ``"pass"`` or
    ``"fail"`` per these rules, in order:

    1. Any Reviewer_Transform_Block_Finding (see
       :func:`_is_reviewer_transform_block`) whose severity is NOT in
       :data:`NON_BLOCKING_SEVERITIES` → **fail**, regardless of raw verdict
       (p43 R1.1/R1.2, calibrating P28b R1.4 rather than removing it). The
       severity floor is the COMPLEMENT of that set, so ``critical``,
       ``major``, an unrecognised token and a missing severity all block;
       only an exact match on a listed token is exempt (p43 R1.5). A
       ``minor`` finding is retained on the verdict and displayed but does
       not fire this rule (p43 R1.3/R1.6) — on a raw ``fail`` it still
       fails, through rules 2/3 below rather than here.
    2. Raw verdict ``fail`` and any issue NOT explicitly
       scaffolding-attributed → **fail** (R1.5 — the conservative default
       applies exactly where it prevents weakening: a fail verdict can only
       be exempted by explicit scaffolding attribution on every issue).
    3. Raw verdict ``fail``, every issue explicitly
       ``template_scaffolding`` → **pass** (R1.3 — THE calibration; the
       issues are retained on the verdict, not discarded).
    4. Raw verdict ``pass`` → **pass** (the conservative default may never
       strengthen a pass into a fail — only an explicit reviewer
       transform-block attribution can, via rule 1).

    A ``fail`` verdict with no issues is unreachable through the parser
    (the fail-needs-issues rule) but stays **fail** here: with no issues
    there is no exempting scaffolding attribution.
    """
    issues = verdict.get("issues") or []
    # Rule 1: an explicit reviewer transform-block finding at a Blocking_Severity
    # fails, regardless of raw verdict.
    for issue in issues:
        if _is_reviewer_transform_block(issue) and issue.get("severity") not in NON_BLOCKING_SEVERITIES:
            return VERDICT_FAIL
    # Rule 4: a raw pass stays a pass.
    if verdict.get("verdict") != VERDICT_FAIL:
        return VERDICT_PASS
    # Rules 2/3: a raw fail is exempted ONLY by explicit scaffolding
    # attribution on every issue.
    all_explicit_scaffolding = bool(issues) and all(
        issue.get("attribution") == ATTRIBUTION_TEMPLATE_SCAFFOLDING
        and issue.get("attribution_source") == ATTRIBUTION_SOURCE_REVIEWER
        for issue in issues
    )
    return VERDICT_PASS if all_explicit_scaffolding else VERDICT_FAIL


def retained_nonblocking_findings(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """The Reviewer_Transform_Block_Findings this verdict RETAINED as non-blocking.

    Empty unless :func:`effective_verdict` returned ``"pass"``: on a run the gate
    failed, nothing was retained — a `minor` transform-block finding on a raw
    `fail` contributed to the failure through rules 2/3, and reporting it as
    "did not block" would be false.

    Pure, non-mutating, and free of logging (R5.3, C12). Three consumers read it
    so that the check detail, the report and the log cannot disagree about a
    number: deriving it three times is the second-inference-path defect this spec
    was asked to avoid.
    """
    if effective_verdict(verdict) != VERDICT_PASS:
        return []
    return [
        issue
        for issue in (verdict.get("issues") or [])
        if _is_reviewer_transform_block(issue) and issue.get("severity") in NON_BLOCKING_SEVERITIES
    ]


def blocking_transform_block_findings(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """The complement: the findings rule 1 fails the gate on.

    Needs no Effective_Verdict guard — a non-empty result means rule 1 fired, so
    the gate returned ``"fail"`` by definition. Exists so §4.6's log can name
    the blocking case precisely without restating rule 1's condition at the log
    site; both halves of rule 1's antecedent are then written down exactly once,
    here and in :func:`retained_nonblocking_findings`.
    """
    return [
        issue
        for issue in (verdict.get("issues") or [])
        if _is_reviewer_transform_block(issue) and issue.get("severity") not in NON_BLOCKING_SEVERITIES
    ]


def verdict_to_parsed_results(verdict: dict[str, Any]) -> dict[str, Any]:
    """Adapt a canonical verdict to the handler's ``parsed_results`` shape.

    Downstream consumers (``aggregate_check_results``, the comprehensive
    report, ``test_summary``) read ``test_categories`` (per-category
    ``{status, passed}``) and ``recommendation``. Both now derive from the
    single structured verdict — there is no second parser (R13.1).

    P28b R1b: issues pass through carrying their Issue_Attribution, and
    ``recommendation`` derives from :func:`effective_verdict` (the
    attribution-calibrated gate decision) — a scaffolding-only fail
    recommends APPROVE with its issues retained; an explicit reviewer
    transform-block finding recommends REJECT even on a raw pass.
    """
    test_categories = {
        name: {"status": "PASSED" if value == VERDICT_PASS else "FAILED", "passed": value == VERDICT_PASS}
        for name, value in verdict["categories"].items()
    }
    return {
        "verdict": verdict["verdict"],
        "issues": verdict["issues"],
        "test_categories": test_categories,
        "recommendation": "APPROVE" if effective_verdict(verdict) == VERDICT_PASS else "REJECT",
    }


_SCHEMA_EXAMPLE = """{
  "verdict": "pass",
  "issues": [
    {
      "severity": "minor",
      "category": "Correctness",
      "description": "1-2 sentences describing the issue.",
      "attribution": "transform_block"
    }
  ],
  "categories": {
    "Correctness": "pass",
    "Schema Compliance": "pass",
    "Error Handling": "pass",
    "AWS Best Practices": "pass",
    "Security": "pass",
    "Performance": "pass"
  }
}"""


def build_reparse_prompt(previous_response: str, parse_error: str) -> str:
    """Prompt for the single reparse retry (re-ask the model — R13/design).

    Instructs the model to re-emit its OWN previous review as a valid JSON
    verdict. The previous response is included (bounded) so the retry
    preserves the original findings instead of re-reviewing from scratch.
    """
    return f"""Your previous code-review response could not be parsed as a structured verdict.
Parse error: {parse_error}

Your previous response was:
---
{previous_response[:6000]}
---

Re-emit the SAME review as ONLY one valid JSON object with EXACTLY this structure (no markdown fences, no prose before or after):
{_SCHEMA_EXAMPLE}

Rules:
- "verdict" must be "pass" or "fail".
- "issues" is a list of {{"severity": "critical"|"major"|"minor", "description": "...", "category": "...", "attribution": "transform_block"|"template_scaffolding"}} objects; it MUST be non-empty when the verdict is "fail".
- "attribution": code between the "# === AI-Generated Transform Block ===" markers is "transform_block"; everything else is pre-reviewed template scaffolding. Findings about scaffolding must carry "attribution": "template_scaffolding" and must NOT alone produce a "fail" verdict.
- Every "categories" value must be "pass" or "fail"."""
