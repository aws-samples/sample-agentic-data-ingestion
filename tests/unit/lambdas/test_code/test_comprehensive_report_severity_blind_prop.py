# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p43 task 11.2 — Property 9: the comprehensive report's tallies are severity-blind.

What this file owns
-------------------
One property from design §6, and nothing else:

* **Property 9** — for any verdict, rewriting every issue severity to any other
  token leaves the report's ``Blocking Failures`` and ``Advisory Failures`` lines
  byte-identical (R4.6).

It is **metamorphic**: nothing here computes what the tallies ought to be. It
builds one report, rewrites every severity in the same verdict document,
re-parses, builds a second report, and compares the two tally lines to each
other. That shape is chosen deliberately over an oracle — an expected-value test
would have to restate the tally derivation, and a test that restates the thing it
checks agrees with the producer by construction (``LGR-28b4d75-5``,
**template**).

Task 11.3 owns the report's retention **wording** and its branch examples; task
10.2's :mod:`test_review_verdict_detail_retention_prop` owns the retained
**count** across the three surfaces. Neither is asserted here.

Why R4.6 needs an executable form at all
----------------------------------------
R4.6 writes no code — task 11.1 changed no tally derivation, and this file
changes none either. What it pins is the answer the user gave to
``requirements.md`` Q2: **severity and the tally are different axes.**
``static_failed``, ``blocking_failures`` and ``advisory_failures`` all come from
one loop over the reviewer's own per-category map
(``lambda_function.py:2162-2174``, read end to end), and never from issue
severities. Deriving them from severities as well would create a second
inference path that can contradict the reviewer's ``categories`` map — a
reviewer reporting ``{"Performance": "pass"}` alongside a ``minor`` Performance
finding is not confused, it is saying the finding does not fail the category —
and there is no principled tiebreak when the two disagree. R4.6 turns "nobody
wired the two together" into a stated decision so that a future reader does not
tidy it into a bug, and this property is what makes that decision fail loudly if
somebody does.

**The rewrite, and what it is scoped against — the load-bearing design choice**
-------------------------------------------------------------------------------
Two shapes were available, and they differ in what the property can see:

(a) **Hold the classification fixed** — rewrite only among Blocking_Severity
    tokens, so the retained set never moves — and then compare the **whole
    report**.
(b) **Rewrite freely across the floor**, permitting the retention renderings to
    move, and compare **only the two tally lines**.

**This file takes (b) for the property, and keeps (a) as a named example.**

(b) is what design §6 states ("any other token"), and it is the only shape that
attacks the failure R4.6 exists to prevent. Every plausible way of wiring
severity into a tally crosses the floor: "a ``minor`` finding should downgrade
its category to advisory", "a ``critical`` finding should add to the blocking
tally". A rewrite confined to blocking↔blocking cannot see either of those,
because both sides of such a rewrite are on the same side of the floor. So
narrowing the antecedent to protect the retention lines would buy a stable
comparison at the price of the only draws that matter.

What (b) costs is that the two reports are **not** identical, and the property
must therefore name what is licensed to differ rather than compare documents.
Three things move with severity, all legitimately:

1. **The retention line and the Deployment Decision clause** — they render
   ``len(retained_nonblocking_findings(...))``, and rewriting a severity across
   the floor changes that number by design (R4.1).
2. **The ``Reviewer Recommendation`` line and the whole verdict chain** —
   ``verdict_to_parsed_results`` derives ``recommendation`` from
   ``effective_verdict`` (``review_verdict.py:414``), so a rewrite that crosses
   the floor flips ``APPROVE``/``REJECT``, which flips ``has_blocking``
   (``:2178``) and can therefore change the banner, ``overall_status``, the
   Deployment Decision arm and the next steps. This is correct: the *gate* moved,
   so the report's *verdict* moved with it. R4.6 is about the tallies, which did
   not.
3. Nothing else. The report renders no issue severity anywhere outside the
   retention line — verified by reading the builder end to end
   (``lambda_function.py:2023-2384``); the per-issue findings a reader sees come
   in through the ``static_analysis_report`` argument, which this file holds at a
   fixed string carrying no digits and no marker.

Point 3 is what makes (a) worth keeping as an example rather than dropping:
:func:`test_a_rewrite_among_blocking_tokens_leaves_the_whole_report_byte_identical`
asserts the stronger conclusion on the narrower antecedent, which is how the
"nothing else moves" half is pinned. It is an example and not a second property
because `NON_BLOCKING_SEVERITIES` currently has one member, so a
classification-preserving rewrite can only be the identity on the non-blocking
side; the day a second non-blocking tier is added, the retention line's severity
list starts moving under (a) too and the whole-report claim has to narrow. Better
to have that break in one named example than inside a property.

Two neighbours are asserted alongside the two tally lines, and one is not
------------------------------------------------------------------------
* ``Checks Passed: {static_passed}/{static_total}`` **is** asserted identical.
  It is the same loop's other output (``:2167-2168``), so a change that wired
  severity into ``static_failed`` would show up here first — and R4.6's own
  wording is about the derivation, not only about the two rendered lines.
* ``Reviewer Recommendation`` is **not** asserted identical, per point 2 above.
  It is the one line in the summary block that legitimately moves with severity,
  and asserting it would fail for a reason R4.6 does not prohibit.

How the report is reached
-------------------------
Through the dynamic import this directory already uses
(``test_review_gate_calibration.py:64-95``, identically in
``test_review_verdict_detail_retention_prop.py:198-226`` and
``test_gate_consumer_passthrough_prop.py:172-200``): stub the heavy /
AWS-touching modules into ``sys.modules`` with ``patch.dict``, pop any cached
``lambda_function``, import it, and call
``_mod.generate_comprehensive_test_report``. The autouse
``_ensure_correct_module`` fixture re-binds ``sys.modules`` per test because
several Lambda directories expose a module of that name. Reused rather than
reinvented — a second way in would be a second thing to keep working.

Blast radius and test coverage, per ``codegraph explore``
--------------------------------------------------------
* ``generate_comprehensive_test_report`` (``lambda_function.py:1924``) — **1
  caller** (the handler), and codegraph reports **no covering tests**. That flag
  is a **false negative**, and codegraph demonstrates its own blind spot on this
  very symbol: it also reports a *separate* symbol of the same name at
  ``test_comprehensive_report.py:56`` with **25 callers**, which is the
  module-level alias
  ``generate_comprehensive_test_report = _mod.generate_comprehensive_test_report``.
  It can see the alias and everything calling it; what it cannot do is link the
  alias to the producer, because no static edge expresses an attribute lookup on
  a module object bound at import time under ``patch.dict``. On disk the producer
  has at least four calling test files — ``test_comprehensive_report.py``,
  ``test_not_verified_classification_prop.py``,
  ``test_report_names_the_running_executor.py`` and
  ``test_review_verdict_detail_retention_prop.py``.

So **no characterization test is owed**, and none is written. The distinction
matters because the rule is that modifying untested code needs characterization
tests first; acting on the flag would have written a third copy of coverage that
already exists. This task modifies no production code in any case.

Fixtures come through the real parser
------------------------------------
Every verdict here is built by ``parse_review_verdict`` from a JSON document and
adapted by ``verdict_to_parsed_results``, matching the sibling property files.
That matters twice for this property. The rewrite is applied to the **document**
and re-parsed, so it is a rewrite as a reviewer would have written it — including
the casings and padding the parser folds (``review_verdict.py:225``) — rather
than a hand-edit of an already-canonical dict. And ``attribution_source`` is
whatever the parser decided, which is half of the antecedent of every rule the
floor touches; hand-setting it would let the two reports agree for a reason the
producer does not share.

The generator space, and where it is bounded
--------------------------------------------
* **Severities**, on both sides of the floor and on neither: the non-blocking
  tokens read off the real frozenset, the two recommended blocking tokens, four
  Unrecognised_Severity tokens (two of which — ``nit``, ``info`` — *read* as
  trivial while blocking under the complement encoding), and the casings and
  paddings the parser normalises into the non-blocking set.
* **Attributions**: explicit reviewer ``transform_block`` (the half of rule 1's
  antecedent that makes a severity matter at all), explicit
  ``template_scaffolding``, values that do not normalise to a recognised token,
  and an omitted key.
* **Categories**: drawn over the three Blocking_Categories and the three
  advisory ones, with half the draws steered to carry at least one failure of
  each kind so both tally lines are present rather than both absent. Whether
  they were present is reported per draw with ``event`` and pinned by
  :func:`test_property_9_reaches_a_report_carrying_both_tally_lines`.
* **``execution_results``** over three shapes — a clean run, a genuine execution
  failure, and a pandas-proxy refusal — and **``executor``** over all three
  values, because the summary block is rendered after a branch chain that these
  two select, and ``LGR-5c30733-3`` (**template / ratified**) branches the
  Deployment Decision sentence on the executor. The tally lines must be blind to
  severity in every arm, not only the one a fixed fixture happens to reach.

Bounded at five issues per verdict — enough for several findings on both sides of
the floor in one draw, and short of ``MAX_ISSUES``, which nothing here is about.
An empty-or-whitespace severity is outside the space by construction: the parser
rejects it (``review_verdict.py:213-214``), so it can never reach a report. The
gate's own polarity on a missing severity is Property 5's subject (task 5.3), not
this one's.

Non-vacuity guards
------------------
This property has two ways to hold while measuring nothing — if the tally lines
were never present, and if the rewrite never really changed anything. Both are
closed:

* The rewrite is **guaranteed to change a parsed severity** on every draw
  carrying at least one issue: the composite compares what the parser produced
  for both documents and perturbs one token where they came out equal
  (:func:`_perturb_first_severity`). The property then asserts the parsed severity
  lists really differ rather than trusting that step.
* :func:`test_property_9_reaches_a_report_carrying_both_tally_lines` — both
  lines are reachable and non-empty, so "byte-identical" is not two absences
  compared.
* :func:`test_a_rewrite_across_the_floor_moves_the_retention_line_but_not_the_tallies`
  — the control, and the licensed-difference statement made concrete: the same
  rewrite that leaves the tallies byte-identical flips the recommendation, the
  banner and the retention renderings. Without it, the property could be
  satisfied by a rewrite that reached nothing at all.
* :func:`test_a_minor_finding_neither_adds_to_nor_removes_from_the_advisory_tally`
  — R4.6's own wording, directed: a retained finding in a category the reviewer
  passed creates no advisory line, and a failing category produces one whatever
  the severities are.

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
Re-confirmed for this task, not inherited. ``why`` on
``review_verdict.py`` returns only the 634-file working-tree snapshot with
``records: []`` — **no ratified record governs the gate or its parser** (design
§5, C10). ``why`` on ``lambda_function.py:2270``, the tally derivation itself,
returns seven records from one 64-file commit (``LGR-60c4985-1`` … ``-7``), every
one of them carrying ``NOTE: BROAD`` and none naming the report's tallies, the
summary block or the severity axis. ``decisions(scope="…/shared")`` carries
nothing about this report either. The ledger is 141 records, **141 template / 0
backfilled**, so nothing is hidden behind the provenance filter. **So no record
governs R4.6's derivation** — that hole is task 15.1's to fill, and it is
recorded here as a gap rather than as an all-clear. Three records do bear on this
file:

* ``LGR-5c30733-3`` (**template / ratified**) is why ``executor`` is drawn rather
  than fixed: the Deployment Decision sentence's execution caveat is
  executor-branched, and the report's arms must be exercised on both sides.
* ``LGR-28b4d75-5`` (**template**) is why this is metamorphic and carries no
  expected tally values at all.
* ``LGR-60c4985-6`` (**template / ratified**) is the amend-don't-delete
  discipline the rest of this spec worked under. Nothing is amended here — this
  file is additive — which is noted so its absence is not read as an oversight.
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, event, given, settings
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
    NON_BLOCKING_SEVERITIES,
    VERDICT_FAIL,
    VERDICT_PASS,
    effective_verdict,
    parse_review_verdict,
    retained_nonblocking_findings,
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
    "artifact_schema": MagicMock(),
    "dynamo_typing": MagicMock(),
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
# Reading the tally lines back out
# ---------------------------------------------------------------------------

#: The two lines Property 9 is about, as the producer spells them
#: (``lambda_function.py:2316`` and ``:2318``). Matched by prefix rather than in
#: full, because the property compares the two reports' lines to EACH OTHER — the
#: prefix only has to locate the line, and what is compared is the whole line
#: byte-for-byte.
_BLOCKING_TALLY_PREFIX = "- **Blocking Failures:**"
_ADVISORY_TALLY_PREFIX = "- **Advisory Failures:**"

#: The same loop's other output (``:2167-2168``, rendered at ``:2312``). Asserted
#: identical too: R4.6 is about the derivation, and a change that wired severity
#: into ``static_failed`` would move this line as well as the two above.
_CHECKS_PASSED_PREFIX = "- **Checks Passed:**"

#: The line that legitimately MOVES with severity, and is therefore deliberately
#: NOT compared: ``verdict_to_parsed_results`` derives ``recommendation`` from
#: ``effective_verdict`` (``review_verdict.py:414``), so a rewrite across the
#: floor flips it. Named so the omission reads as a decision.
_RECOMMENDATION_PREFIX = "- **Reviewer Recommendation:**"

#: R4.1's retention rendering — the other licensed difference. Used only to prove
#: that the rewrite really reached across the floor (the control), never as
#: something the property requires to be stable. The wording itself is task
#: 11.3's.
_REPORT_RETENTION_MARKER = "did not block the review gate"
_REPORT_COUNT_RE = re.compile(r"(\d+) reviewer finding\(s\)")

#: The summary block's retention line (``lambda_function.py:2334``), the first of
#: R4.1's two renderings. Located, never compared for stability: it is one of the
#: three things a severity rewrite is licensed to move.
_RETENTION_PREFIX = "- **Reported, not blocking:**"

#: A ``static_analysis_report`` argument carrying no digits and none of the
#: markers above, so nothing this file supplies can be mistaken for something the
#: builder rendered.
_STATIC_ANALYSIS_REPORT = "static"


def _static_analysis_summary_block(report: str) -> str:
    """The ``### Static Analysis Summary`` block, up to the next heading.

    Sliced rather than scanned over the whole document so the tally lines are
    read from the one place they are rendered. Restated here rather than imported
    from ``test_review_verdict_detail_retention_prop.py``: test modules in this
    directory do not import each other, and a cross-test import would make one
    file's collection depend on another's module-level ``patch.dict`` block.
    """
    start = report.find("### Static Analysis Summary")
    if start == -1:
        return ""
    end = report.find("### Recommendations", start)
    return report[start:end] if end != -1 else report[start:]


def _summary_line(report: str, prefix: str) -> str | None:
    """The one summary-block line starting with ``prefix``, or ``None``.

    Raises on more than one. Two ``Blocking Failures`` lines in one report would
    be a second tally rendering — a divergence arriving from an unexpected
    direction, which a first-match lookup would hide.
    """
    matches = [line for line in _static_analysis_summary_block(report).splitlines() if line.startswith(prefix)]
    assert len(matches) <= 1, f"expected at most one {prefix!r} line, got {matches!r}"
    return matches[0] if matches else None


def _deployment_decision_line(report: str) -> str:
    """The ``1. **Deployment Decision:** …`` line, whichever branch built it."""
    for line in report.splitlines():
        if line.startswith("1. **Deployment Decision:**"):
            return line
    return ""


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Sentinel: the ``attribution`` key is absent from the document entirely.
_OMIT = object()

#: Non-empty after the parser's ``strip()``, which is what
#: ``_validate_verdict_document`` requires of a description (``:214-215``).
_ISSUE_TEXT = (
    st.text(alphabet=string.ascii_letters + string.digits + " .,", min_size=1, max_size=30).map(str.strip).filter(bool)
)

#: The Non_Blocking_Severity tokens, read off the real frozenset rather than
#: restated, so nothing here drifts if the set ever grows a second tier.
_NON_BLOCKING_SEVERITY = st.sampled_from(sorted(NON_BLOCKING_SEVERITIES))

#: Tokens outside the non-blocking set: the two recommended blocking severities
#: and four Unrecognised_Severity tokens, two of which (``nit``, ``info``) READ as
#: trivial. Under the complement encoding a token nobody enumerated blocks.
_BLOCKING_SEVERITY = st.sampled_from(("critical", "major", "nit", "info", "blocker", "error"))

#: Spellings the parser normalises INTO the non-blocking set (``:225``), built
#: from the real set members rather than hardcoded.
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
#: This is both the original-draw alphabet and the rewrite alphabet: "any other
#: token" in Property 9 means any token a reviewer could have emitted, which is
#: the same space, not a special one.
_ANY_SEVERITY = st.one_of(_NON_BLOCKING_SEVERITY, _BLOCKING_SEVERITY, _NORMALISES_TO_NON_BLOCKING)

#: Attribution inputs the parser recognises as an explicit reviewer
#: transform-block attribution, in the casings ``:236-238`` folds. Half of rule
#: 1's antecedent, and therefore the half that makes a severity matter at all.
_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION = st.sampled_from(
    ("transform_block", "TRANSFORM_BLOCK", " transform_block ", "Transform_Block")
)

#: Explicit reviewer scaffolding attribution — rules 2/3's vocabulary, untouched
#: by this spec.
_SCAFFOLDING_ATTRIBUTION = st.sampled_from(
    ("template_scaffolding", "Template_Scaffolding", "TEMPLATE_SCAFFOLDING", "  template_scaffolding  ")
)

#: Values that do NOT normalise to a recognised attribution, so the parser
#: defaults them to ``("transform_block", "default")`` — a shape rule 1 does not
#: match at any severity (do-NOT 7).
_UNRECOGNISED_ATTRIBUTION = st.sampled_from(("generated", "boilerplate", "transform-block", "", "   "))

_ANY_ATTRIBUTION = st.one_of(
    _REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION,
    _SCAFFOLDING_ATTRIBUTION,
    _UNRECOGNISED_ATTRIBUTION,
    st.just(_OMIT),
)

#: The report's category axis — the tallies' ONLY input (``:2162-2174``).
_BLOCKING_CATEGORIES = ("Correctness", "Security", "Schema Compliance")
_ADVISORY_CATEGORIES = ("Error Handling", "AWS Best Practices", "Performance")

_FREE_CATEGORIES = st.dictionaries(
    st.sampled_from(_BLOCKING_CATEGORIES + _ADVISORY_CATEGORIES),
    st.sampled_from((VERDICT_PASS, VERDICT_FAIL)),
    max_size=5,
)

#: Categories guaranteed to produce BOTH tally lines. Drawn half the time,
#: because an unsteered dictionary reaches "one blocking fail and one advisory
#: fail" rarely, and a property that compares two absent lines 300 times has
#: measured nothing. The reachability test below checks the steering worked
#: rather than assuming it.
_BOTH_TALLIES_CATEGORIES = st.builds(
    lambda blocking, advisory, extra_passes: {
        blocking: VERDICT_FAIL,
        advisory: VERDICT_FAIL,
        **{name: VERDICT_PASS for name in extra_passes},
    },
    st.sampled_from(_BLOCKING_CATEGORIES),
    st.sampled_from(_ADVISORY_CATEGORIES),
    st.lists(st.sampled_from(_BLOCKING_CATEGORIES + _ADVISORY_CATEGORIES), max_size=2),
)

_CATEGORIES = st.one_of(_FREE_CATEGORIES, _BOTH_TALLIES_CATEGORIES)

#: The three ``execution_results`` shapes the report branches on. Fixed dicts
#: rather than drawn field-by-field: the arms turn on which shape it is, and no
#: number inside them is read here.
_EXEC_OK: dict[str, Any] = {
    "execution_successful": True,
    "input_records": 100,
    "output_records": 100,
    "data_quality_results": {"overall_quality_score": 98},
}
_EXEC_FAILED: dict[str, Any] = {
    "execution_successful": False,
    "input_records": 100,
    "output_records": 0,
    "error_details": "AnalysisException: cannot resolve column",
}
#: The proxy REFUSED the transform, so nothing ran — the arm whose wording is
#: furthest from the others.
_EXEC_NOT_VERIFIED: dict[str, Any] = {
    "execution_successful": False,
    "not_verified_by_proxy": True,
    "not_verified_construct": "F.explode",
    "not_verified_line_number": 42,
    "not_verified_source_line": "df = df.select(F.explode('items'))",
    "not_verified_reason": "not_yet_expressible",
}

_EXECUTION_RESULTS = st.sampled_from((_EXEC_OK, _EXEC_FAILED, _EXEC_NOT_VERIFIED))

#: Every Verification_Executor, so ``LGR-5c30733-3``'s executor-branched caveat
#: is exercised on both sides of its own branch.
_EXECUTOR = st.sampled_from(
    (
        _mod.VERIFICATION_EXECUTOR_PANDAS,
        _mod.VERIFICATION_EXECUTOR_GLUE,
        _mod.VERIFICATION_EXECUTOR_SHADOW,
    )
)


def _issue_doc(description: str, severity: str, attribution: Any = _OMIT) -> dict[str, Any]:
    """A raw reviewer issue as it appears in the model's JSON, pre-parse."""
    doc: dict[str, Any] = {"severity": severity, "description": description}
    if attribution is not _OMIT:
        doc["attribution"] = attribution
    return doc


def _parse(raw_verdict: str, issue_docs: list[dict[str, Any]], categories: dict[str, str]) -> dict[str, Any]:
    """Build a verdict document and run it through the REAL parser."""
    return parse_review_verdict(json.dumps({"verdict": raw_verdict, "issues": issue_docs, "categories": categories}))


def _rewrite_severities(issue_docs: list[dict[str, Any]], replacements: list[str]) -> list[dict[str, Any]]:
    """The metamorphic transform: same documents, every severity replaced.

    Applied to the pre-parse **document**, not to a canonical verdict, so the
    rewrite is one a reviewer could have emitted and the parser's own
    normalisation runs over it (``:225``). Everything else about each issue —
    description, attribution, position in the list — is carried through
    unchanged, which is what makes the comparison downstream a statement about
    severity alone.
    """
    assert len(issue_docs) == len(replacements)
    return [{**doc, "severity": replacement} for doc, replacement in zip(issue_docs, replacements, strict=True)]


def _perturb_first_severity(issue_docs: list[dict[str, Any]], replacements: list[str]) -> list[str]:
    """Force one replacement to differ, by appending a character to the original.

    Used when a drawn replacement list would leave every **parsed** severity
    unchanged. Appending a character differs from the original both as written and
    after the parser's ``strip().lower()``, and the result is an
    Unrecognised_Severity — exactly the sort of token R4.6 requires the tallies to
    ignore, so the perturbation lands inside the property's subject rather than
    beside it.

    Comparing *written* tokens is not enough to decide when this is needed, which
    is the whole reason it is a separate step: ``" Minor "`` and ``"minor"`` are
    different documents and the same parsed severity, so a "did the strings
    differ" test passes while the two verdicts are identical. The caller therefore
    decides by parsing both and comparing what the parser produced — see
    :func:`_verdict_and_severity_rewrite`.
    """
    perturbed = list(replacements)
    perturbed[0] = f"{issue_docs[0]['severity']}x"
    return perturbed


@st.composite
def _verdict_and_severity_rewrite(draw) -> tuple[dict[str, Any], dict[str, Any]]:
    """Draw one verdict document and its severity-rewritten twin, both parsed.

    The pair is the property's input: two canonical verdicts identical in every
    respect except issue severities. A raw ``fail`` with no issues is rejected by
    the parser (``:247-250``), so the issue list is drawn non-empty on that
    branch.

    **Half the draws are steered to cross the floor**, in a drawn direction, by
    splicing in one Reviewer_Transform_Block_Finding whose replacement sits on the
    other side of ``NON_BLOCKING_SEVERITIES``. Unsteered, that shape needs a
    recognised reviewer attribution *and* a rewrite that happens to change the
    classification, and it was measured at ~8% of draws — enough to be reachable,
    not enough for the region where the report's verdict, recommendation and
    retention renderings all move to be more than incidental. Those are precisely
    the draws where a severity-derived tally would show up. The ``event`` tags on
    the property report the achieved rate per run rather than trusting this
    steering.

    Position is drawn rather than appended, because the gate and the report both
    loop over ``issues`` and a finding's index is part of the shape.
    """
    raw_verdict = draw(st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))
    docs = draw(
        st.lists(
            st.builds(_issue_doc, _ISSUE_TEXT, _ANY_SEVERITY, _ANY_ATTRIBUTION),
            min_size=1 if raw_verdict == VERDICT_FAIL else 0,
            max_size=4,
        )
    )
    categories = draw(_CATEGORIES)
    replacements = draw(st.lists(_ANY_SEVERITY, min_size=len(docs), max_size=len(docs)))

    if draw(st.booleans()):
        # Direction is drawn: retained-becomes-blocking, and blocking-becomes-retained.
        # The two are not symmetric downstream — one adds a retention rendering and
        # one removes it — so fixing either would leave half the region unvisited.
        if draw(st.booleans()):
            severity, replacement = draw(_NORMALISES_TO_NON_BLOCKING), draw(_BLOCKING_SEVERITY)
        else:
            severity, replacement = draw(_BLOCKING_SEVERITY), draw(_NON_BLOCKING_SEVERITY)
        focus = _issue_doc(draw(_ISSUE_TEXT), severity, draw(_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION))
        index = draw(st.integers(min_value=0, max_value=len(docs)))
        docs = [*docs[:index], focus, *docs[index:]]
        replacements = [*replacements[:index], replacement, *replacements[index:]]

    original = _parse(raw_verdict, docs, categories)
    rewritten = _parse(raw_verdict, _rewrite_severities(docs, replacements), categories)
    # Whether this draw is a real rewrite is decided by the PARSER, not by
    # comparing the tokens drawn: `" Minor "` and `"minor"` are different
    # documents and the same parsed severity (`review_verdict.py:225`), so a
    # string comparison here would call that pair a rewrite and the property would
    # then compare a report with itself. Where every parsed severity would be
    # unchanged, one replacement is perturbed and the document re-parsed.
    if docs and [i["severity"] for i in original["issues"]] == [i["severity"] for i in rewritten["issues"]]:
        rewritten = _parse(
            raw_verdict, _rewrite_severities(docs, _perturb_first_severity(docs, replacements)), categories
        )
    return original, rewritten


def _report(parsed_results: dict[str, Any], execution_results: dict[str, Any], executor: str) -> str:
    """The comprehensive report, through the real builder."""
    return _mod.generate_comprehensive_test_report(
        _STATIC_ANALYSIS_REPORT, execution_results, parsed_results, executor=executor
    )


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------


# Feature: p43-review-gate-severity-floor, Property 9: The report's tallies are
# severity-blind.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    verdicts=_verdict_and_severity_rewrite(),
    execution_results=_EXECUTION_RESULTS,
    executor=_EXECUTOR,
)
def test_the_reports_tallies_are_severity_blind(verdicts, execution_results, executor):
    """**Validates: Requirements 4.6** — Property 9.

    For any verdict, rewriting every issue severity to any other token leaves the
    report's ``Blocking Failures`` and ``Advisory Failures`` lines byte-identical.

    Metamorphic: no expected tally is computed anywhere here. Two reports are
    built from two verdicts differing only in severities, and the two lines are
    compared to each other. ``LGR-28b4d75-5`` (**template**) is why — a test that
    restated the derivation could not detect the derivation drifting.

    **The rewrite crosses the floor, and the assertion is scoped to the two tally
    lines for that reason.** The alternative — permuting only among blocking
    tokens so the retained set never moves, and then comparing whole reports — is
    kept as :func:`test_a_rewrite_among_blocking_tokens_leaves_the_whole_report_byte_identical`
    rather than used here, because every plausible way of wiring severity into a
    tally ("a ``minor`` downgrades its category to advisory") crosses the floor,
    and a rewrite that cannot cross it cannot see any of them. The module
    docstring argues this in full.

    So three things are licensed to differ and are not asserted: the retention
    line and its Deployment Decision clause (they render the retained count,
    which the floor moves by design, R4.1), the ``Reviewer Recommendation`` line,
    and the verdict chain that ``recommendation`` feeds through ``has_blocking``
    (``:2178``). That the gate's answer moves with severity is correct — R4.6 is
    about the tallies, which do not.

    ``Checks Passed`` **is** asserted identical: it is the same loop's other
    output, so wiring severity into ``static_failed`` would surface here too.

    ``execution_results`` and ``executor`` are drawn because the summary block is
    rendered after a branch chain these two select, and the claim is that the
    tallies are severity-blind in every arm. ``LGR-5c30733-3``
    (**template / ratified**) is why the executor is drawn rather than fixed.
    """
    original, rewritten = verdicts
    original_parsed = verdict_to_parsed_results(original)
    rewritten_parsed = verdict_to_parsed_results(rewritten)

    original_severities = [i["severity"] for i in original_parsed["issues"]]
    rewritten_severities = [i["severity"] for i in rewritten_parsed["issues"]]

    # ── Premises: this really is a severity-only rewrite ──────────────────
    # Asserted rather than trusted. If the transform changed a description, an
    # attribution or the category map, the comparison below would still pass
    # while no longer being about severity at all — and if the composite's
    # perturbation step ever stopped firing, the property would compare two
    # identical reports.
    if original_severities:
        assert original_severities != rewritten_severities, (
            "the generator produced a rewrite that changed no parsed severity, so this draw compares "
            f"a report with itself: {original_severities!r}"
        )
    assert [i["description"] for i in original_parsed["issues"]] == [
        i["description"] for i in rewritten_parsed["issues"]
    ], "the rewrite changed a description; it must touch severity only"
    assert [i["attribution"] for i in original_parsed["issues"]] == [
        i["attribution"] for i in rewritten_parsed["issues"]
    ], "the rewrite changed an attribution; it must touch severity only"
    assert [i["attribution_source"] for i in original_parsed["issues"]] == [
        i["attribution_source"] for i in rewritten_parsed["issues"]
    ], "the rewrite changed an attribution_source; it must touch severity only"
    # The tallies' only input (`:2164-2174`), unchanged across the rewrite. This
    # is the premise that makes the whole property meaningful: the two reports
    # are handed identical category maps, so any difference in the tally lines
    # could only have come from severity.
    assert original_parsed["test_categories"] == rewritten_parsed["test_categories"], (
        "the rewrite changed the per-category map, which is the tallies' only legitimate input"
    )

    report_before = _report(original_parsed, execution_results, executor)
    report_after = _report(rewritten_parsed, execution_results, executor)

    blocking_before = _summary_line(report_before, _BLOCKING_TALLY_PREFIX)
    blocking_after = _summary_line(report_after, _BLOCKING_TALLY_PREFIX)
    advisory_before = _summary_line(report_before, _ADVISORY_TALLY_PREFIX)
    advisory_after = _summary_line(report_after, _ADVISORY_TALLY_PREFIX)

    event(f"blocking tally line present: {blocking_before is not None}")
    event(f"advisory tally line present: {advisory_before is not None}")
    event(
        "rewrite moved the effective verdict: "
        f"{effective_verdict(original_parsed) != effective_verdict(rewritten_parsed)}"
    )
    event(
        "rewrite moved the retained count: "
        f"{len(retained_nonblocking_findings(original_parsed)) != len(retained_nonblocking_findings(rewritten_parsed))}"
    )

    # ── The property ──────────────────────────────────────────────────────
    assert blocking_before == blocking_after, (
        "rewriting issue severities changed the report's Blocking Failures line. R4.6: the blocking "
        "tally is derived from the reviewer's per-category `categories` map and never from issue "
        f"severities. {original_severities!r} -> {rewritten_severities!r}; "
        f"{blocking_before!r} -> {blocking_after!r}"
    )
    assert advisory_before == advisory_after, (
        "rewriting issue severities changed the report's Advisory Failures line. R4.6: a "
        "Non_Blocking_Severity neither adds to nor removes from the advisory tally. "
        f"{original_severities!r} -> {rewritten_severities!r}; {advisory_before!r} -> {advisory_after!r}"
    )
    # Whole-document occurrence counts, so a SECOND tally rendering added outside
    # the summary block cannot slip past a slice that only reads inside it.
    for prefix in (_BLOCKING_TALLY_PREFIX, _ADVISORY_TALLY_PREFIX):
        assert report_before.count(prefix) == report_after.count(prefix), (
            f"the number of {prefix!r} renderings in the report moved with severity, so some tally "
            "rendering outside the Static Analysis Summary block is reading the severity axis (R4.6)"
        )
    # The same loop's other output.
    assert _summary_line(report_before, _CHECKS_PASSED_PREFIX) == _summary_line(report_after, _CHECKS_PASSED_PREFIX), (
        "rewriting issue severities changed the report's Checks Passed count, so `static_failed` is "
        "reading the severity axis. R4.6 forbids deriving either tally from issue severities"
    )


def test_property_9_reaches_a_report_carrying_both_tally_lines():
    """Property 9's generator reaches a report where both tally lines exist.

    "Byte-identical" is trivially true of two absent lines, so without this the
    property could pass 300 times on reports carrying neither. One fixed verdict
    with a failing Blocking_Category and a failing advisory category; both lines
    are asserted present and non-empty, and both survive a severity rewrite that
    crosses the floor in both directions.

    The category names are asserted to land on the side the producer's own
    ``BLOCKING_CATEGORIES`` set puts them (``:2152``), so this test cannot quietly
    become a check that two identical lines exist.
    """
    docs = [
        _issue_doc("A naming nit.", sorted(NON_BLOCKING_SEVERITIES)[0], "transform_block"),
        _issue_doc("A real defect.", "critical", "transform_block"),
    ]
    categories = {"Correctness": VERDICT_FAIL, "Performance": VERDICT_FAIL, "Security": VERDICT_PASS}
    before = verdict_to_parsed_results(_parse(VERDICT_PASS, docs, categories))
    # The rewrite swaps the two severities, so both directions across the floor
    # are exercised in one comparison.
    after = verdict_to_parsed_results(
        _parse(VERDICT_PASS, _rewrite_severities(docs, ["critical", sorted(NON_BLOCKING_SEVERITIES)[0]]), categories)
    )

    report_before = _report(before, _EXEC_OK, _mod.VERIFICATION_EXECUTOR_PANDAS)
    report_after = _report(after, _EXEC_OK, _mod.VERIFICATION_EXECUTOR_PANDAS)

    blocking = _summary_line(report_before, _BLOCKING_TALLY_PREFIX)
    advisory = _summary_line(report_before, _ADVISORY_TALLY_PREFIX)
    assert blocking is not None and "Correctness" in blocking, (
        f"expected a Blocking Failures line naming Correctness, got {blocking!r}"
    )
    assert advisory is not None and "Performance" in advisory, (
        f"expected an Advisory Failures line naming Performance, got {advisory!r}"
    )

    assert blocking == _summary_line(report_after, _BLOCKING_TALLY_PREFIX)
    assert advisory == _summary_line(report_after, _ADVISORY_TALLY_PREFIX)


def test_the_tally_comparison_can_fail_when_the_tally_really_moves():
    """The assertion mechanism has teeth: a moved tally is detected.

    Property 9 asserts an **equality**, and an equality can hold because nothing
    is being read. Here the severities are held fixed and the *categories* are
    changed instead — the tallies' one legitimate input — and the same helpers
    the property uses report different lines.

    So the property's passing is evidence about the producer rather than about
    :func:`_summary_line`: if a future change derived either tally from severity,
    the comparison would move exactly as it moves here.
    """
    docs = [_issue_doc("A real defect.", "critical", "transform_block")]
    passing = verdict_to_parsed_results(_parse(VERDICT_PASS, docs, {"Correctness": VERDICT_PASS}))
    blocking = verdict_to_parsed_results(_parse(VERDICT_PASS, docs, {"Correctness": VERDICT_FAIL}))
    advisory = verdict_to_parsed_results(_parse(VERDICT_PASS, docs, {"Performance": VERDICT_FAIL}))

    reports = [_report(p, _EXEC_OK, _mod.VERIFICATION_EXECUTOR_PANDAS) for p in (passing, blocking, advisory)]
    blocking_lines = [_summary_line(r, _BLOCKING_TALLY_PREFIX) for r in reports]
    advisory_lines = [_summary_line(r, _ADVISORY_TALLY_PREFIX) for r in reports]

    assert blocking_lines[0] != blocking_lines[1], (
        f"the blocking tally did not move when a Blocking_Category failed: {blocking_lines!r}"
    )
    assert advisory_lines[0] != advisory_lines[2], (
        f"the advisory tally did not move when an advisory category failed: {advisory_lines!r}"
    )
    assert _summary_line(reports[0], _CHECKS_PASSED_PREFIX) != _summary_line(reports[1], _CHECKS_PASSED_PREFIX)


def test_a_rewrite_across_the_floor_moves_the_retention_line_but_not_the_tallies():
    """The control, and the licensed differences made concrete.

    One reviewer transform-block finding at a Non_Blocking_Severity on a raw
    ``pass``: retained, gate passes, report carries the retention line and the
    Deployment Decision clause. Rewrite that one severity to ``critical`` and the
    gate fails, ``recommendation`` flips to ``REJECT``, ``has_blocking`` becomes
    True and the report reaches a different verdict branch — while the two tally
    lines are byte-identical.

    This is why the property is scoped to the two lines rather than to the
    document: the report legitimately says something different about the *gate*,
    because the gate's answer moved. It also proves the rewrite reaches across the
    floor at all, which is the one thing Property 9's assertions cannot show —
    they would be equally satisfied by a rewrite that changed nothing.
    """
    non_blocking = sorted(NON_BLOCKING_SEVERITIES)[0]
    docs = [_issue_doc("A naming nit.", non_blocking, "transform_block")]
    categories = {"Correctness": VERDICT_PASS, "Performance": VERDICT_FAIL}

    retained_parsed = verdict_to_parsed_results(_parse(VERDICT_PASS, docs, categories))
    blocking_parsed = verdict_to_parsed_results(
        _parse(VERDICT_PASS, _rewrite_severities(docs, ["critical"]), categories)
    )

    # The premise: the rewrite really did cross the floor.
    assert effective_verdict(retained_parsed) == VERDICT_PASS
    assert effective_verdict(blocking_parsed) == VERDICT_FAIL
    assert len(retained_nonblocking_findings(retained_parsed)) == 1
    assert retained_nonblocking_findings(blocking_parsed) == []

    report_retained = _report(retained_parsed, _EXEC_OK, _mod.VERIFICATION_EXECUTOR_PANDAS)
    report_blocking = _report(blocking_parsed, _EXEC_OK, _mod.VERIFICATION_EXECUTOR_PANDAS)

    # What is licensed to move, and does. R4.1 has TWO retention renderings — the
    # summary line and the Deployment Decision clause — so the count appears twice
    # in the retained report and not at all in the blocking one. Each is read from
    # its own rendering rather than counted over the whole document, so "twice"
    # cannot be satisfied by one rendering stating it twice.
    assert _REPORT_RETENTION_MARKER in report_retained
    assert _REPORT_RETENTION_MARKER not in report_blocking
    assert _REPORT_COUNT_RE.findall(_summary_line(report_retained, _RETENTION_PREFIX) or "") == ["1"]
    assert _REPORT_COUNT_RE.findall(_deployment_decision_line(report_retained)) == ["1"]
    assert _REPORT_COUNT_RE.findall(report_blocking) == []
    assert _summary_line(report_retained, _RECOMMENDATION_PREFIX) != _summary_line(
        report_blocking, _RECOMMENDATION_PREFIX
    ), "the recommendation is derived from the gate, so a rewrite across the floor must move it"
    assert _deployment_decision_line(report_retained) != _deployment_decision_line(report_blocking)

    # What R4.6 requires to stay put, and does.
    assert _summary_line(report_retained, _ADVISORY_TALLY_PREFIX) == _summary_line(
        report_blocking, _ADVISORY_TALLY_PREFIX
    )
    assert _summary_line(report_retained, _BLOCKING_TALLY_PREFIX) == _summary_line(
        report_blocking, _BLOCKING_TALLY_PREFIX
    )
    # Advisory named, blocking absent: `Performance` failed and no
    # Blocking_Category did, so the finding's severity is the only thing that
    # could have added a blocking line — and did not, on either side.
    assert "Performance" in (_summary_line(report_retained, _ADVISORY_TALLY_PREFIX) or "")
    assert _summary_line(report_retained, _BLOCKING_TALLY_PREFIX) is None
    assert _summary_line(report_blocking, _BLOCKING_TALLY_PREFIX) is None


def test_a_rewrite_among_blocking_tokens_leaves_the_whole_report_byte_identical():
    """The stronger claim on the narrower antecedent: severity has no other path in.

    A classification-preserving rewrite — ``critical`` → ``error``, ``major`` →
    ``blocker``, all four Blocking_Severity tokens — moves neither the gate, the
    retained set, nor any rendered string: the two reports are **equal in full**.
    That is what pins "nothing else in the report reads severity", which Property
    9's two-line comparison cannot say on its own.

    Kept as an example rather than written as a second property because it is
    contingent on ``NON_BLOCKING_SEVERITIES`` having exactly one member. With a
    second non-blocking tier, a classification-preserving rewrite could move
    ``minor`` → the new token, which the retention line's severity list renders —
    and the whole-report claim would have to narrow. Better that it break here,
    in one named example next to its reason, than inside a property.
    """
    assert len(NON_BLOCKING_SEVERITIES) == 1, (
        "a second Non_Blocking_Severity exists, so a classification-preserving rewrite can now move "
        "the retention line's severity list and this example's whole-report claim needs narrowing"
    )
    categories = {"Correctness": VERDICT_FAIL, "AWS Best Practices": VERDICT_FAIL}
    docs = [
        _issue_doc("A real defect.", "critical", "transform_block"),
        _issue_doc("Scaffolding noise.", "major", "template_scaffolding"),
        _issue_doc("Unattributed finding.", "nit", "generated"),
    ]
    before = verdict_to_parsed_results(_parse(VERDICT_FAIL, docs, categories))
    after = verdict_to_parsed_results(
        _parse(VERDICT_FAIL, _rewrite_severities(docs, ["error", "blocker", "info"]), categories)
    )

    # The premise: every token moved, and none crossed the floor.
    assert [i["severity"] for i in before["issues"]] != [i["severity"] for i in after["issues"]]
    for parsed in (before, after):
        assert all(i["severity"] not in NON_BLOCKING_SEVERITIES for i in parsed["issues"])
        assert effective_verdict(parsed) == VERDICT_FAIL
        assert retained_nonblocking_findings(parsed) == []

    for execution_results in (_EXEC_OK, _EXEC_FAILED, _EXEC_NOT_VERIFIED):
        for executor in (
            _mod.VERIFICATION_EXECUTOR_PANDAS,
            _mod.VERIFICATION_EXECUTOR_GLUE,
            _mod.VERIFICATION_EXECUTOR_SHADOW,
        ):
            assert _report(before, execution_results, executor) == _report(after, execution_results, executor), (
                "a severity rewrite that changed no classification changed the report, so some "
                "rendering reads the severity token itself (R4.6)"
            )


def test_a_minor_finding_neither_adds_to_nor_removes_from_the_advisory_tally():
    """R4.6's own wording, directed at the two ways a tally could move.

    A reviewer transform-block finding in the ``Performance`` category, with the
    reviewer reporting ``{"Performance": "pass"}``. That pairing is the one
    ``requirements.md`` Q2 uses to explain the decision: the reviewer is not
    confused, it is saying the finding does not fail the category.

    * **Adds:** no advisory line appears at a non-blocking severity, and none
      appears at ``critical`` either — a finding of any severity in a passing
      category creates no tally entry.
    * **Removes:** with ``{"Performance": "fail"}`` the advisory line appears and
      names ``Performance`` at both severities — a ``minor`` finding does not
      excuse a category the reviewer failed.

    An example rather than a property arm because the four cells are fixed and
    named, and because a shrunk counterexample reads far worse in a failure report
    than "the advisory line moved when Performance passed".
    """
    non_blocking = sorted(NON_BLOCKING_SEVERITIES)[0]

    for category_verdict, expect_line in ((VERDICT_PASS, False), (VERDICT_FAIL, True)):
        lines = []
        for severity in (non_blocking, "critical"):
            parsed = verdict_to_parsed_results(
                _parse(
                    VERDICT_PASS,
                    [_issue_doc("Consider a broadcast join.", severity, "transform_block")],
                    {"Performance": category_verdict},
                )
            )
            line = _summary_line(_report(parsed, _EXEC_OK, _mod.VERIFICATION_EXECUTOR_PANDAS), _ADVISORY_TALLY_PREFIX)
            if expect_line:
                assert line is not None and "Performance" in line, (
                    f"Performance failed per the reviewer's categories, so the advisory tally must name "
                    f"it at severity {severity!r}; got {line!r} (R4.6)"
                )
            else:
                assert line is None, (
                    f"Performance passed per the reviewer's categories, so a {severity!r} finding must "
                    f"not create an advisory tally entry; got {line!r} (R4.6)"
                )
            lines.append(line)
        # And the two severities agree with each other, which is the tally being
        # blind rather than merely being right twice for different reasons.
        assert lines[0] == lines[1]
