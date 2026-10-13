# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p43 task 10.2 — Properties 7 and 8 over the retained count and the retained detail.

What this file owns
-------------------
Two properties from design §6, and nothing else:

* **Property 7** — the retained count is the same number in all three places a
  reader can find it (R3.6, R4.1): the ``ai_review_verdict`` check detail built
  by ``aggregate_check_results`` (``lambda_function.py:1169-1186``), the report's
  ``Static Analysis Summary`` line and the report's ``Deployment Decision``
  clause, both built by ``generate_comprehensive_test_report`` (``:2307-2355``).
* **Property 8** — every retained finding's severity and description survive
  into that detail, and the pre-existing per-attribution counts substring is
  still there beside the new count (R3.5, R3.6).

Property 12 (``test_retained_findings_helpers_prop.py``) owns what
``retained_nonblocking_findings`` *means*; this file takes the helper's answer
as given and asks whether the three consumers report it faithfully. The report's
**wording** is task 11.3's, the log is task 12.2's, and the viewer is 13.2's —
none is asserted here.

Why the three-way comparison is the point
-----------------------------------------
The number is derived **once**, by one helper, and read by three surfaces. That
is the whole design (§4.4): "deriving it three times is the second-inference-path
defect this spec was asked to avoid." A property that only checked each surface
against a hand-computed expectation would pass just as happily if a future change
gave one surface its own derivation — so this asserts all three against
``len(retained_nonblocking_findings(...))`` **and** against each other, which is
what makes a divergence visible from whichever end a reader arrives at.

The counts are read out of the rendered text by pattern rather than by comparing
whole sentences, because Property 7's subject is the *number*. If a producer
rewords a clause, the presence assertions below fail loudly — which is the
correct outcome, and much better than a property that keeps passing because it
was pinned to copy nobody produces any more.

The detail is unconditional; the two report renderings are conditional
---------------------------------------------------------------------
**This is a deliberate asymmetry, not an oversight, and the two surfaces must
not be aligned.** Design §6's Property 7 says all three renderings "are absent
when that length is zero", while design §4.4's Consumer 1 renders the detail's
clause unconditionally, so a zero-retained run reads ``0 retained as
non-blocking``. Both cannot hold. The resolution taken here, and matching what
both producers actually do on disk:

* **The check detail states the number including zero.** R3.6 asks the detail to
  "state the number", which includes zero, and the clause's neighbours — the
  per-attribution counts ``0 transform_block, 2 template_scaffolding`` — are
  already unconditional, so a conditional third count would read as an
  inconsistency in one parenthesis.
* **The two report renderings are absent at zero.** A line in a document a human
  reads saying that zero findings were retained is noise.

So Property 7's absence clause is narrowed to the two report arms. The producer
records the same reasoning at its own site (``lambda_function.py:2320-2324``,
"deliberately UNLIKE the check detail … The two surfaces differ on purpose; do
not align them"), and :func:`test_the_detail_states_zero_where_the_report_says_nothing`
is the named example that pins the asymmetry so it cannot be tidied away by
someone reading only one of the two.

One consequence worth stating, because it is the one shape the detail arm does
not cover: on a verdict with **no issues at all** the detail takes its other
branch entirely — ``(no issues reported)``, with no count in it (``:1187``).
``retained`` is necessarily empty there, so nothing is being hidden, and the
property asserts that branch explicitly rather than skipping it.

How the three functions are reached
-----------------------------------
Through the dynamic import this directory already uses
(``test_review_gate_calibration.py:64-95``, identically in
``test_gate_consumer_passthrough_prop.py:172-200`` and
``test_comprehensive_report.py:32-57``): stub the heavy / AWS-touching modules
into ``sys.modules`` with ``patch.dict``, pop any cached ``lambda_function``,
import it, and call ``_mod.aggregate_check_results`` /
``_mod.generate_comprehensive_test_report``. The autouse
``_ensure_correct_module`` fixture re-binds ``sys.modules`` per test because
several Lambda directories expose a module of that name. Reused rather than
reinvented — a second way in would be a second thing to keep working.

**That import is also why codegraph is wrong about two of the three symbols, and
the figures below are recorded with the correction rather than the flag.**

Blast radius and test coverage, per ``codegraph explore``
--------------------------------------------------------
* ``retained_nonblocking_findings`` (``review_verdict.py:345``) — codegraph
  reports 7 callers in ``lambda_function.py``; on disk there are **two call
  sites** (``:1169`` in ``aggregate_check_results``, ``:2309`` in
  ``generate_comprehensive_test_report``) plus the ``from review_verdict import``
  binding at ``:107``, so read the 7 as reference edges rather than call sites.
  Covering tests found correctly (``test_retained_findings_helpers_prop.py``,
  ``test_review_verdict_purity_source.py``) — it is imported by name, so the edge
  is visible. **Task 12.1 will add the third call site**, which is the point of
  the helper and is why this file's three-way comparison is written to extend to
  a fourth surface without changing shape.
* ``aggregate_check_results`` (``lambda_function.py:1057``) — 1 caller (the
  handler); codegraph reports **no covering tests**. That is a **false
  negative**: 19 test files call it — 17 in this directory including this one,
  ``tests/contracts/api_responses/test_test_failure_approval_payload.py``, and
  ``tests/integration/pipeline_flow/test_test_failure_gating.py`` — every one
  through the ``_mod.<name>`` attribute access above. No static edge expresses an
  attribute lookup on a module object bound at import time under ``patch.dict``.
* ``generate_comprehensive_test_report`` (``lambda_function.py:1924``) — 1
  caller; same false negative, 3 calling test files
  (``test_comprehensive_report.py``, ``test_not_verified_classification_prop.py``,
  ``test_report_names_the_running_executor.py``). **Codegraph demonstrates its
  own blind spot here rather than leaving it to be diagnosed**: it also reports a
  *separate* symbol of the same name at ``test_comprehensive_report.py:56`` with
  **25 callers**, which is the module-level alias
  ``generate_comprehensive_test_report = _mod.generate_comprehensive_test_report``.
  It can see the alias and everything that calls it; what it cannot do is link
  the alias to the producer.

So no characterization test is owed for any of the three. The distinction matters
because the rule is that modifying *untested* code needs characterization tests
first, and acting on the flag would have written a third copy of coverage that
already exists.

Fixtures come through the real parser
------------------------------------
Every verdict here is built by ``parse_review_verdict`` from a JSON document and
then adapted by ``verdict_to_parsed_results``, matching
``test_retained_findings_helpers_prop.py`` and
``test_gate_consumer_passthrough_prop.py``: ``attribution_source`` is whatever
the parser decided, not what this file typed. That is half of rule 1's
antecedent and therefore half of the helper's, so a hand-set
``attribution_source`` would make the retained count agree with itself by
construction. ``parsed_results`` — not the canonical verdict — is what both
consumers are actually handed in the handler (``:1152``, and the report's own
parameter), so it is what is passed here; the property additionally checks the
canonical verdict yields the same number, which pins the adapter as
count-preserving.

The generator space, and where it is bounded
--------------------------------------------
Property 7 draws both raw verdicts, both sides of the severity floor, the
casings the parser folds into the non-blocking set, recognised scaffolding
attributions, unrecognised tokens and an omitted key — so an effective fail
(retained empty by the helper's guard), an effective pass with no retained
finding, and an effective pass with one to four of them are all reachable.

It also draws the two inputs that select the report's branch, because the
Deployment Decision clause is appended **after** the branch chain and the claim
is that it is true in every arm:

* ``execution_results`` over three shapes — a clean run, a genuine execution
  failure, and a pandas-proxy refusal (``not_verified_by_proxy``), which is the
  arm whose wording is furthest from the others.
* ``executor`` over all three values, so ``LGR-5c30733-3``'s executor-branched
  caveat is exercised on both sides.
* ``categories`` including the three Blocking_Categories, which reaches design
  §5 risk 2 — a retained finding coexisting with a blocking static failure, in a
  report whose status line says static analysis blocked.
  :func:`test_a_retained_finding_coexists_with_a_blocking_static_failure` is the
  named case.

Property 8 is bounded, per design §6's own note, to **at most five issues with
short descriptions** — the region where neither the ``issues[:5]`` summary slice
(``:1172``) nor the 1000-character truncation (``:1185``) applies. Both limits
are real and neither is a defect, so they are pinned as examples
(:func:`test_a_sixth_issue_is_outside_property_8s_bound`,
:func:`test_a_long_description_is_truncated_outside_property_8s_bound`) rather
than allowed to falsify a property that is not about them. The property asserts
``len(detail) < 1000`` on every draw, so it can never quietly start testing
truncated text if the prefix grows.

Non-vacuity guards
------------------
Both properties have antecedents a careless generator can leave unsatisfied, and
both have a shape that would hold while measuring nothing — Property 7 trivially
if the count were never non-zero, Property 8 trivially if no finding were ever
retained. Named:

* :func:`test_property_7_reaches_a_zero_and_a_non_zero_retained_count` — both
  sides of the absence clause are reachable, and at a non-zero count the two
  report renderings really do appear.
* :func:`test_property_7_is_not_vacuous_the_report_renderings_carry_the_number` —
  the control: the report's two counts move with the number of retained
  findings, so the arms are reading a rendered value rather than matching an
  absent one twice.
* :func:`test_a_retained_finding_coexists_with_a_blocking_static_failure` —
  design §5 risk 2, the branch where the two renderings are most likely to be
  suppressed by accident.
* :func:`test_property_8_reaches_a_verdict_with_two_retained_findings` — more
  than one retained finding is reachable, so "every retained finding" quantifies
  over something.
* :func:`test_the_detail_states_zero_where_the_report_says_nothing` — the
  asymmetry, as an example.

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
``0176ccc686`` with ``records: []`` — **no ratified record governs the gate, the
helper or its parser** (design §5, C10, re-confirmed for this task). ``why`` on
``lambda_function.py`` adds ``LGR-b2f79ef-2`` (the ``verified``/``passed`` check
encoding, which the ``ai_review_verdict`` entry sits beside untouched) and
``LGR-42dbdb1-1/-2`` (the error-path state row), neither of which names the
detail string or the report's summary block. ``decisions(scope=".../shared")``
carries nothing about this gate either. That hole is task 15.1's to fill; it is
recorded here as a gap, not as an all-clear. Two records do bear on this file:

* ``LGR-5c30733-3`` (**template / ratified**) is why the Deployment Decision arm
  draws the executor rather than fixing it — the sentence's execution caveat is
  executor-branched, and a clause appended after the chain must not disturb
  either arm.
* ``LGR-28b4d75-5`` (**template**) is why every expected value here is derived
  from R3.5/R3.6/R4.1 and from ``len(retained_nonblocking_findings(...))``, and
  never from running the producers and recording what came out: a test written
  from the producer's own output cannot detect the producer drifting.
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
    ALLOWED_ATTRIBUTIONS,
    ATTRIBUTION_TEMPLATE_SCAFFOLDING,
    ATTRIBUTION_TRANSFORM_BLOCK,
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
# Reading the three renderings back out
# ---------------------------------------------------------------------------

#: The check detail's retained count. R3.6 asks the detail to state the number
#: "as text distinct from the existing per-attribution counts", and the phrase
#: is what makes it distinct — so the phrase is part of what is asserted, not
#: incidental to locating a digit.
_DETAIL_COUNT_RE = re.compile(r"(\d+) retained as non-blocking")

#: The report's retained count, in both of its renderings. Two independent
#: discriminators are used together throughout: this pattern for the NUMBER, and
#: :data:`_REPORT_RETENTION_MARKER` for the CLAIM. A reworded clause then fails
#: the presence assertions loudly instead of slipping past the absence ones.
_REPORT_COUNT_RE = re.compile(r"(\d+) reviewer finding\(s\)")

#: The semantic content R4.1 requires of both report renderings — that the
#: findings did not block the gate. R4.2 is why it names the review gate
#: specifically rather than the report: a retained finding can appear in a report
#: whose status line says static analysis blocked, and the line must not read as
#: a contradiction of it. Asserting the copy itself is task 11.3's.
_REPORT_RETENTION_MARKER = "did not block the review gate"

#: A ``static_analysis_report`` argument carrying no digits and neither marker,
#: so the section scans below cannot pick anything out of text this file supplied.
_STATIC_ANALYSIS_REPORT = "static"


def _verdict_check(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """The one ``ai_review_verdict`` entry, or a failure naming what was found."""
    matches = [c for c in checks if c.get("name") == "ai_review_verdict"]
    assert len(matches) == 1, f"expected exactly one ai_review_verdict check, got {[c.get('name') for c in checks]}"
    return matches[0]


def _static_analysis_summary_block(report: str) -> str:
    """The ``### Static Analysis Summary`` block, up to the next heading.

    Sliced rather than regex-scanned over the whole report so the two report arms
    are read from the two places they are rendered, independently. A count that
    appeared in the Deployment Decision line but not the summary block — or the
    reverse — is a real divergence between two surfaces, and reading the whole
    document at once would hide it.
    """
    start = report.find("### Static Analysis Summary")
    if start == -1:
        return ""
    end = report.find("### Recommendations", start)
    return report[start:end] if end != -1 else report[start:]


def _deployment_decision_line(report: str) -> str:
    """The ``1. **Deployment Decision:** …`` line, whichever branch built it."""
    for line in report.splitlines():
        if line.startswith("1. **Deployment Decision:**"):
            return line
    return ""


def _sole_count(text: str, pattern: re.Pattern[str], where: str) -> int | None:
    """The single count ``pattern`` finds in ``text``; ``None`` when it finds none.

    Raises on more than one, because two counts in one rendering is the
    disagreement Property 7 exists to catch, arriving from an unexpected
    direction — a second clause rather than a wrong number.
    """
    found = pattern.findall(text)
    assert len(found) <= 1, f"{where} states {len(found)} retained counts, expected at most one: {found!r} in {text!r}"
    return int(found[0]) if found else None


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Sentinel: the ``attribution`` key is absent from the document entirely.
_OMIT = object()

#: Non-empty after the parser's ``strip()``, which is what
#: ``_validate_verdict_document`` requires of a description (``:222-223``).
#: Bounded at 30 characters so Property 8's draws stay clear of the detail's
#: 1000-character truncation; the property asserts that headroom per draw rather
#: than trusting this bound to hold as the prefix grows.
_ISSUE_TEXT = (
    st.text(alphabet=string.ascii_letters + string.digits + " .,", min_size=1, max_size=30).map(str.strip).filter(bool)
)

#: The Non_Blocking_Severity tokens, read off the real frozenset rather than
#: restated, so nothing here drifts if the set ever grows a second tier.
_NON_BLOCKING_SEVERITY = st.sampled_from(sorted(NON_BLOCKING_SEVERITIES))

#: Tokens outside the non-blocking set: the two recommended blocking severities
#: and four Unrecognised_Severity tokens, two of which (``nit``, ``info``) READ
#: as trivial. Under the complement encoding a token nobody enumerated blocks.
_BLOCKING_SEVERITY = st.sampled_from(("critical", "major", "nit", "info", "blocker", "error"))

#: Spellings the parser normalises INTO the non-blocking set (``:225``), built
#: from the real set members rather than hardcoded. These are the draws whose
#: findings would silently stop being retained if the parser stopped normalising
#: — and the count would then differ from zero in all three places at once,
#: which is why Property 7 cannot see it and Property 5 (task 5.3) owns it.
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
#: transform-block attribution, in the casings ``:236-238`` folds. Half of the
#: helper's antecedent.
_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION = st.sampled_from(
    ("transform_block", "TRANSFORM_BLOCK", " transform_block ", "Transform_Block")
)

#: Explicit reviewer scaffolding attribution — rules 2/3's vocabulary, untouched
#: by this spec. Present so the per-attribution counts substring Property 8
#: checks has a non-zero second term to carry.
_SCAFFOLDING_ATTRIBUTION = st.sampled_from(
    ("template_scaffolding", "Template_Scaffolding", "TEMPLATE_SCAFFOLDING", "  template_scaffolding  ")
)

#: Values that do NOT normalise to a recognised attribution, so the parser
#: defaults them to ``("transform_block", "default")`` — which the helper
#: deliberately does not match, while the per-attribution ``transform_block``
#: count DOES include them. That divergence is real and is exactly why Property
#: 8 asserts both numbers rather than assuming they move together.
_UNRECOGNISED_ATTRIBUTION = st.sampled_from(("generated", "boilerplate", "transform-block", "", "   "))

#: Every attribution input, either kind.
_ANY_ATTRIBUTION = st.one_of(
    _REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION,
    _SCAFFOLDING_ATTRIBUTION,
    _UNRECOGNISED_ATTRIBUTION,
    st.just(_OMIT),
)

#: The report's category axis. Blocking and advisory names are drawn together so
#: the branch chain's arms — blocking, advisory-only, all-pass — are all
#: reachable, and so a retained finding can coexist with a blocking static
#: failure (design §5 risk 2).
_BLOCKING_CATEGORIES = ("Correctness", "Security", "Schema Compliance")
_ADVISORY_CATEGORIES = ("Error Handling", "AWS Best Practices", "Performance")

_CATEGORIES = st.dictionaries(
    st.sampled_from(_BLOCKING_CATEGORIES + _ADVISORY_CATEGORIES),
    st.sampled_from((VERDICT_PASS, VERDICT_FAIL)),
    max_size=4,
)

#: The three ``execution_results`` shapes the report branches on. Fixed dicts
#: rather than drawn field-by-field: the report's arms turn on which shape it is,
#: not on the numbers inside, and the numbers are checked nowhere here.
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
#: The proxy REFUSED the transform, so nothing ran. The arm whose Deployment
#: Decision wording is furthest from the others, and the one that proves the
#: clause is appended after the chain rather than inside an arm.
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


def _normalises_to_a_recognised_attribution(value: Any) -> bool:
    """The parser's own recognition test, written out at the test's level.

    Deliberately not an import of anything private: this is the condition at
    ``review_verdict.py:239-241`` stated from the module docstring's
    ``attribution`` bullet, so a generator's constraint cannot agree with the
    parser by construction.
    """
    return isinstance(value, str) and value.strip().lower() in ALLOWED_ATTRIBUTIONS


@st.composite
def _verdicts(draw, max_issues: int = 5) -> tuple[dict[str, Any], dict[str, Any]]:
    """Draw ``(canonical_verdict, parsed_results)`` through the real parser and adapter.

    Both are returned because Property 7 asserts the count is the same on either
    side of the adapter: the consumers are handed ``parsed_results``, but the
    number they must agree with is the helper's answer about the verdict, and
    ``verdict_to_parsed_results`` copying ``issues`` through is what makes those
    the same question.

    A raw ``fail`` with no issues is rejected by the parser (``:247-250``), so the
    issue list is drawn non-empty on that branch. Roughly half the draws are
    steered towards containing at least one Reviewer_Transform_Block_Finding at a
    non-blocking severity on a raw ``pass``, because that is the only shape with a
    non-zero retained count and an unsteered generator reaches it rarely — the
    reachability test below is what checks the steering worked rather than
    assuming it.
    """
    raw_verdict = draw(st.sampled_from((VERDICT_PASS, VERDICT_FAIL)))
    docs = draw(
        st.lists(
            st.builds(_issue_doc, _ISSUE_TEXT, _ANY_SEVERITY, _ANY_ATTRIBUTION),
            min_size=1 if raw_verdict == VERDICT_FAIL else 0,
            max_size=max(0, max_issues - 1),
        )
    )
    if raw_verdict == VERDICT_PASS and draw(st.booleans()):
        focus = _issue_doc(
            draw(_ISSUE_TEXT),
            draw(_NORMALISES_TO_NON_BLOCKING),
            draw(_REVIEWER_TRANSFORM_BLOCK_ATTRIBUTION),
        )
        # Position matters — the gate and both consumers loop over issues — so
        # the focus issue is spliced in at a drawn index rather than appended.
        index = draw(st.integers(min_value=0, max_value=len(docs)))
        docs = [*docs[:index], focus, *docs[index:]]

    verdict = _parse(raw_verdict, docs, draw(_CATEGORIES))
    return verdict, verdict_to_parsed_results(verdict)


def _detail_for(parsed_results: dict[str, Any], execution_results: dict[str, Any] | None) -> str:
    """The ``ai_review_verdict`` check's detail, through the real aggregator."""
    _status, checks = _mod.aggregate_check_results(parsed_results, execution_results)
    return _verdict_check(checks)["detail"]


def _expected_attribution_counts_substring(issues: list[dict[str, Any]]) -> str:
    """The per-attribution counts substring, computed at the test's own level.

    This is the PRE-EXISTING clause p43 must leave byte-intact, so the expected
    value is derived from the two substrings already pinned in
    ``test_review_gate_calibration.py`` — ``"0 transform_block, 2
    template_scaffolding"`` and ``"1 transform_block, 0 template_scaffolding"`` —
    and not from p43's own requirements, which say nothing about how these two
    are counted.

    Note the counts key on ``attribution`` alone, with no ``attribution_source``
    test, so a defaulted issue adds to the ``transform_block`` term while never
    being retained. That divergence is the reason Property 8 asserts both
    numbers instead of one.
    """
    transform = sum(1 for i in issues if i.get("attribution") == ATTRIBUTION_TRANSFORM_BLOCK)
    scaffolding = sum(1 for i in issues if i.get("attribution") == ATTRIBUTION_TEMPLATE_SCAFFOLDING)
    return f"{transform} transform_block, {scaffolding} template_scaffolding"


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------


# Feature: p43-review-gate-severity-floor, Property 7: The retained count is the
# same number everywhere.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    verdicts=_verdicts(),
    execution_results=_EXECUTION_RESULTS,
    executor=_EXECUTOR,
)
def test_the_retained_count_is_the_same_number_everywhere(verdicts, execution_results, executor):
    """**Validates: Requirements 3.6, 4.1** — Property 7.

    For any verdict the parser can emit, the count in the ``ai_review_verdict``
    check detail, the count in the report's ``Static Analysis Summary`` line and
    the count in the report's ``Deployment Decision`` clause all equal
    ``len(retained_nonblocking_findings(verdict))`` — and the two **report**
    renderings are absent when that length is zero.

    **The absence clause covers the two report arms only; the check detail states
    the number including zero.** That asymmetry is deliberate and is argued in
    full in this module's docstring ("The detail is unconditional…"): R3.6 asks
    the detail to state the number, which includes zero, and its neighbouring
    per-attribution counts are already unconditional, whereas a report line
    saying zero findings were retained is noise in a document a human reads. The
    producer records the same reasoning at ``lambda_function.py:2320-2324``. Do
    not align the two surfaces; :func:`test_the_detail_states_zero_where_the_report_says_nothing`
    pins the difference as an example so it survives a reader who sees only one.

    The one shape the detail arm cannot cover is a verdict with no issues at all,
    which takes the ``(no issues reported)`` branch and carries no count. The
    retained list is necessarily empty there, and that branch is asserted
    explicitly below rather than skipped.

    All three are compared against the helper **and** against each other. Against
    the helper because that is where the number comes from; against each other
    because a future change giving one surface its own derivation would otherwise
    only be caught if it also got the arithmetic wrong. Design §4.4: deriving it
    three times is the second-inference-path defect this spec was asked to avoid.

    ``execution_results``, ``executor`` and ``categories`` are drawn because the
    Deployment Decision clause is appended after the branch chain has chosen its
    wording, and the claim is that the clause is true in **every** arm — including
    the two where a retained finding coexists with a blocking outcome (design §5
    risk 2). ``LGR-5c30733-3`` (**template / ratified**) is why the executor is
    drawn rather than fixed.
    """
    verdict, parsed_results = verdicts

    expected = len(retained_nonblocking_findings(parsed_results))
    # The adapter copies `issues` through, so the helper must give the same
    # answer on either side of it. Asserted rather than assumed: if the adapter
    # ever filtered or rewrote issues, every arm below would agree with each
    # other about a number that no longer describes the reviewer's verdict.
    assert expected == len(retained_nonblocking_findings(verdict)), (
        "verdict_to_parsed_results changed the retained count, so the number the consumers agree "
        f"about is not the number the verdict carries: {verdict!r}"
    )
    event(f"retained: {'0' if expected == 0 else '1+'}")
    event(f"effective verdict: {effective_verdict(parsed_results)}")

    detail = _detail_for(parsed_results, execution_results)
    report = _mod.generate_comprehensive_test_report(
        _STATIC_ANALYSIS_REPORT, execution_results, parsed_results, executor=executor
    )
    summary_block = _static_analysis_summary_block(report)
    decision_line = _deployment_decision_line(report)

    detail_count = _sole_count(detail, _DETAIL_COUNT_RE, "the ai_review_verdict detail")
    summary_count = _sole_count(summary_block, _REPORT_COUNT_RE, "the report's Static Analysis Summary block")
    decision_count = _sole_count(decision_line, _REPORT_COUNT_RE, "the report's Deployment Decision line")

    # Arm 1 — the check detail, unconditional wherever the detail states counts
    # at all. R3.6.
    if parsed_results.get("issues"):
        assert detail_count == expected, (
            f"the ai_review_verdict detail states {detail_count} retained findings where the helper "
            f"found {expected} (R3.6). Detail: {detail!r}"
        )
    else:
        # The no-issues branch (:1187) states no counts of any kind, and nothing
        # is hidden by that: no issues means nothing could have been retained.
        assert expected == 0, f"a verdict with no issues retained {expected} findings: {parsed_results!r}"
        assert detail_count is None, (
            f"the no-issues detail branch grew a retained count; it has no per-attribution counts "
            f"beside it for one to be distinct from (R3.6). Detail: {detail!r}"
        )

    # Arms 2 and 3 — the report, conditional. R4.1.
    if expected:
        assert summary_count == expected, (
            f"the report's Static Analysis Summary states {summary_count} retained findings where the "
            f"helper found {expected} (R4.1). Summary block: {summary_block!r}"
        )
        assert decision_count == expected, (
            f"the report's Deployment Decision states {decision_count} retained findings where the "
            f"helper found {expected} (R4.1). Decision line: {decision_line!r}"
        )
        # The claim, not just the number: R4.1 asks the report to say the
        # findings did not block the gate, and a bare count would not.
        assert _REPORT_RETENTION_MARKER in summary_block, (
            f"the Static Analysis Summary states a retained count without saying the findings did not "
            f"block the review gate (R4.1): {summary_block!r}"
        )
        assert _REPORT_RETENTION_MARKER in decision_line, (
            f"the Deployment Decision states a retained count without saying the findings did not "
            f"block the review gate (R4.1): {decision_line!r}"
        )
        # All three against each other, so a divergence is visible from whichever
        # surface a reader arrives at.
        assert detail_count == summary_count == decision_count, (
            f"three consumers of one helper disagree about one number: detail {detail_count}, "
            f"summary {summary_count}, decision {decision_count}"
        )
    else:
        assert summary_count is None and _REPORT_RETENTION_MARKER not in summary_block, (
            "nothing was retained, so the report's Static Analysis Summary must carry no retention "
            f"line at all: {summary_block!r}"
        )
        assert decision_count is None and _REPORT_RETENTION_MARKER not in decision_line, (
            "nothing was retained, so the report's Deployment Decision must carry no retention "
            f"clause at all: {decision_line!r}"
        )


def test_property_7_reaches_a_zero_and_a_non_zero_retained_count():
    """Property 7's generator reaches both sides of its absence clause.

    Two fixed verdicts, identical but for one severity token. Without this the
    property could hold with every draw retaining nothing: both report arms would
    then assert absence 300 times and the interesting half — that the two
    renderings appear and carry the right number — would never run.

    The retained verdict is identified by its own parsed triple rather than by
    the helper, so the reachability claim does not depend on the thing it is
    making reachable. ``('transform_block', 'reviewer')`` at a severity inside
    ``NON_BLOCKING_SEVERITIES`` is the shape rule 1 exempts.
    """
    non_blocking = sorted(NON_BLOCKING_SEVERITIES)[0]
    categories = {"Correctness": VERDICT_PASS}

    retained_verdict = _parse(VERDICT_PASS, [_issue_doc("A naming nit.", non_blocking, "transform_block")], categories)
    empty_verdict = _parse(VERDICT_PASS, [_issue_doc("A naming nit.", "critical", "transform_block")], categories)

    retained_parsed = verdict_to_parsed_results(retained_verdict)
    empty_parsed = verdict_to_parsed_results(empty_verdict)

    only_issue = retained_parsed["issues"][0]
    assert only_issue["attribution"] == ATTRIBUTION_TRANSFORM_BLOCK
    assert only_issue["attribution_source"] == "reviewer"
    assert only_issue["severity"] in NON_BLOCKING_SEVERITIES

    assert len(retained_nonblocking_findings(retained_parsed)) == 1
    # The blocking sibling fails the gate, so the helper's guard empties the list
    # for a second reason as well — both are why the count is zero there.
    assert len(retained_nonblocking_findings(empty_parsed)) == 0
    assert effective_verdict(empty_parsed) == VERDICT_FAIL

    retained_report = _mod.generate_comprehensive_test_report(_STATIC_ANALYSIS_REPORT, _EXEC_OK, retained_parsed)
    empty_report = _mod.generate_comprehensive_test_report(_STATIC_ANALYSIS_REPORT, _EXEC_OK, empty_parsed)

    assert "1 reviewer finding(s)" in _static_analysis_summary_block(retained_report)
    assert "1 reviewer finding(s)" in _deployment_decision_line(retained_report)
    assert _REPORT_RETENTION_MARKER not in empty_report

    assert "1 retained as non-blocking" in _detail_for(retained_parsed, _EXEC_OK)
    assert "0 retained as non-blocking" in _detail_for(empty_parsed, _EXEC_OK)


def test_property_7_is_not_vacuous_the_report_renderings_carry_the_number():
    """The report's two counts MOVE with the number of retained findings.

    The control. Property 7's report arms could be satisfied by a producer that
    happened to print the right digit once — or, at zero, by two absence
    assertions over a rendering that never exists at all. Here the same shape is
    rendered at one, two and three retained findings, and each count is read back
    out of both renderings.

    Expected values come from R4.1 ("naming the count") and from the length of
    the helper's own list, never from running the report and recording what it
    printed — ``LGR-28b4d75-5`` (**template**).
    """
    non_blocking = sorted(NON_BLOCKING_SEVERITIES)[0]
    for count in (1, 2, 3):
        docs = [_issue_doc(f"Finding number {n}.", non_blocking, "transform_block") for n in range(count)]
        parsed = verdict_to_parsed_results(_parse(VERDICT_PASS, docs, {"Correctness": VERDICT_PASS}))
        assert len(retained_nonblocking_findings(parsed)) == count

        report = _mod.generate_comprehensive_test_report(_STATIC_ANALYSIS_REPORT, _EXEC_OK, parsed)
        summary_block = _static_analysis_summary_block(report)
        decision_line = _deployment_decision_line(report)

        assert _sole_count(summary_block, _REPORT_COUNT_RE, "the summary block") == count
        assert _sole_count(decision_line, _REPORT_COUNT_RE, "the decision line") == count
        assert _sole_count(_detail_for(parsed, _EXEC_OK), _DETAIL_COUNT_RE, "the detail") == count


def test_a_retained_finding_coexists_with_a_blocking_static_failure():
    """Design §5 risk 2: both renderings survive the blocking branch.

    A retained finding on an effective ``pass`` whose ``categories`` carry a
    failing Blocking_Category. ``has_blocking`` is then True
    (``lambda_function.py:2178``) and the run reaches ``⚠️ REVIEW REQUIRED``, so a
    retained finding appears in a report whose status line says static analysis
    blocked.

    This is the branch where the two renderings are most likely to go missing by
    accident — the summary line is inside ``if parsed_results:`` and the clause is
    appended after a chain that took an early arm here. Both must still appear,
    with the same number, and the line must name **the review gate** rather than
    claiming these findings did not block anything at all (R4.2). The wording
    itself is task 11.3's; what is asserted here is that the number and the claim
    are present on this branch too.
    """
    non_blocking = sorted(NON_BLOCKING_SEVERITIES)[0]
    parsed = verdict_to_parsed_results(
        _parse(
            VERDICT_PASS,
            [_issue_doc("A naming nit.", non_blocking, "transform_block")],
            {"Correctness": VERDICT_FAIL},
        )
    )
    assert effective_verdict(parsed) == VERDICT_PASS
    assert len(retained_nonblocking_findings(parsed)) == 1

    report = _mod.generate_comprehensive_test_report(_STATIC_ANALYSIS_REPORT, _EXEC_OK, parsed)
    # The premise: this really is the blocking branch.
    assert "REVIEW REQUIRED" in report
    assert "Blocking Failures" in report

    summary_block = _static_analysis_summary_block(report)
    decision_line = _deployment_decision_line(report)
    assert _sole_count(summary_block, _REPORT_COUNT_RE, "the summary block") == 1
    assert _sole_count(decision_line, _REPORT_COUNT_RE, "the decision line") == 1
    assert _REPORT_RETENTION_MARKER in summary_block
    assert _REPORT_RETENTION_MARKER in decision_line


def test_the_detail_states_zero_where_the_report_says_nothing():
    """The asymmetry, as an example: detail unconditional, report conditional.

    One verdict carrying issues, none of them retained. The check detail reads
    ``0 retained as non-blocking``; the report carries no retention line and no
    retention clause.

    This is here because the two surfaces are one design decision apart and a
    later reader seeing only one of them would reasonably "fix" it. R3.6 asks the
    detail to state the number, which includes zero, and the count sits beside
    per-attribution counts that already state zero (``0 template_scaffolding``
    here) — so a conditional third count would read as an inconsistency inside
    one parenthesis. A report line saying zero findings were retained is just
    noise. Design §6's Property 7 says all three are absent at zero; **that
    clause is narrowed to the two report arms**, and this example is the record
    of why.
    """
    parsed = verdict_to_parsed_results(
        _parse(
            VERDICT_PASS,
            [_issue_doc("A real defect.", "critical", "transform_block")],
            {"Correctness": VERDICT_PASS},
        )
    )
    assert retained_nonblocking_findings(parsed) == []

    detail = _detail_for(parsed, _EXEC_OK)
    assert "0 retained as non-blocking" in detail
    # And the neighbour it must stay distinct from is unconditional in the same
    # way, which is the consistency argument for the detail's polarity.
    assert "1 transform_block, 0 template_scaffolding" in detail

    report = _mod.generate_comprehensive_test_report(_STATIC_ANALYSIS_REPORT, _EXEC_OK, parsed)
    assert _REPORT_COUNT_RE.findall(report) == []
    assert _REPORT_RETENTION_MARKER not in report


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------


# Feature: p43-review-gate-severity-floor, Property 8: Retained findings survive
# into the detail.
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(verdicts=_verdicts(max_issues=5), execution_results=_EXECUTION_RESULTS)
def test_retained_findings_survive_into_the_detail(verdicts, execution_results):
    """**Validates: Requirements 3.5, 3.6** — Property 8.

    For any verdict with at most five issues and short descriptions, every
    retained finding's severity and description appear in the ``ai_review_verdict``
    check detail, and the pre-existing per-attribution counts substring is still
    present.

    R3.5 is a retention claim: the finding is carried into the detail rather than
    discarded because it did not block. So the assertion is about the finding's own
    text, not about the count — a detail that said ``2 retained as non-blocking``
    and then listed nothing would satisfy R3.6 and fail R3.5, and that is the
    failure this property is shaped to catch.

    The severity asserted is the **parsed** issue's, not the token this file
    drew: the parser lowercases and strips (``:225``), so ``" Minor "`` is
    retained as ``minor`` and the detail renders ``minor``. Asserting the drawn
    spelling would fail for a reason that has nothing to do with retention.

    Bounded per design §6 to the region where the ``issues[:5]`` summary slice
    (``:1172``) and the 1000-character truncation (``:1185``) do not apply. Both
    limits are real and neither is a defect, so they are pinned as examples —
    :func:`test_a_sixth_issue_is_outside_property_8s_bound` and
    :func:`test_a_long_description_is_truncated_outside_property_8s_bound` —
    rather than allowed to falsify a property that is not about them. The
    headroom is asserted per draw below, so this cannot quietly start testing
    truncated text if the detail's prefix grows.
    """
    _verdict, parsed_results = verdicts
    issues = parsed_results.get("issues") or []
    assert len(issues) <= 5, "Property 8 is bounded to at most five issues; the generator broke its bound"

    retained = retained_nonblocking_findings(parsed_results)
    event(f"retained: {min(len(retained), 3)}")

    detail = _detail_for(parsed_results, execution_results)
    # The bound, asserted rather than assumed. At 1000 the detail was sliced and
    # every "appears in the detail" assertion below would be about text that may
    # simply have been cut off — a false failure, and a confusing one.
    assert len(detail) < 1000, (
        f"the detail reached the truncation ceiling at {len(detail)} characters, so this draw is "
        f"outside Property 8's bound and its assertions would be about truncated text: {detail!r}"
    )

    for finding in retained:
        severity = str(finding["severity"])
        description = str(finding["description"])
        assert severity in detail, (
            f"a retained finding's severity {severity!r} does not appear in the ai_review_verdict "
            f"detail, so the finding was not carried into it (R3.5). Detail: {detail!r}"
        )
        assert description in detail, (
            f"a retained finding's description {description!r} does not appear in the "
            f"ai_review_verdict detail (R3.5). A retained finding is reported, not discarded — "
            f"'did not block' must not be implemented as 'not shown'. Detail: {detail!r}"
        )
        # The rendered pairing, not just the two strings somewhere in the text:
        # the summary renders `[severity] description` per issue (:1172), and two
        # findings whose halves crossed over would satisfy the two assertions
        # above while misreporting both.
        assert f"[{severity}] {description}" in detail, (
            f"the retained finding's severity and description both appear but not as the rendered "
            f"pair '[{severity}] {description}' (R3.5). Detail: {detail!r}"
        )

    # The per-attribution counts live only on the issues branch (``:1178-1186``);
    # the no-issues branch states no counts of any kind (``:1187``), so there is
    # nothing there for p43's count to sit beside and nothing to preserve. Same
    # branch guard as Property 7's detail arm, for the same reason — and the
    # retained list is empty on that branch anyway, so the loop above made no
    # claim either.
    if issues:
        expected_counts = _expected_attribution_counts_substring(issues)
        assert expected_counts in detail, (
            f"the per-attribution counts substring {expected_counts!r} is no longer in the detail, so "
            f"p43's new count did not sit beside the existing ones as R3.6 requires — the two pinned "
            f"substrings in test_review_gate_calibration.py are the examples this generalises. "
            f"Detail: {detail!r}"
        )
        # R3.6's "distinct from" is positional as well as textual: the new count
        # must follow the per-attribution counts, which is what keeps those two
        # pinned substrings byte-intact and puts the number ahead of the free
        # text truncation eats first.
        assert detail.index(expected_counts) < detail.index("retained as non-blocking"), (
            f"the retained count precedes the per-attribution counts; R3.6 wants it as text distinct "
            f"from them, and the pinned substrings depend on the order. Detail: {detail!r}"
        )
    else:
        assert not retained, f"a verdict with no issues retained {len(retained)} findings: {parsed_results!r}"


def test_property_8_reaches_a_verdict_with_two_retained_findings():
    """Property 8 quantifies over something: two retained findings, both rendered.

    "Every retained finding" is vacuously true of an empty list, so without this
    the property could pass on 300 draws that retained nothing. Two findings also
    exercise the ``"; "`` join, which one cannot.

    Descriptions are deliberately distinct and non-overlapping so a producer that
    rendered one finding twice would fail rather than coincidentally satisfy both
    assertions.
    """
    non_blocking = sorted(NON_BLOCKING_SEVERITIES)[0]
    parsed = verdict_to_parsed_results(
        _parse(
            VERDICT_PASS,
            [
                _issue_doc("Rename the alias to something clearer.", non_blocking, "transform_block"),
                _issue_doc("Prefer an explicit cast here.", non_blocking, "TRANSFORM_BLOCK"),
            ],
            {"Correctness": VERDICT_PASS},
        )
    )
    retained = retained_nonblocking_findings(parsed)
    assert len(retained) == 2

    detail = _detail_for(parsed, _EXEC_OK)
    assert f"[{non_blocking}] Rename the alias to something clearer." in detail
    assert f"[{non_blocking}] Prefer an explicit cast here." in detail
    assert "2 retained as non-blocking" in detail
    assert "2 transform_block, 0 template_scaffolding" in detail


def test_a_sixth_issue_is_outside_property_8s_bound():
    """The ``issues[:5]`` slice is real: a sixth issue's text is not in the detail.

    Property 8 is bounded at five issues for this reason, and the bound is a
    property of the producer rather than of the test, so it is pinned here as an
    example instead of being left as a comment. The **count** is unaffected — it
    is ``len(retained)`` over every issue, not over the rendered five — which is
    exactly why the count sits ahead of the summary in the detail string.
    """
    non_blocking = sorted(NON_BLOCKING_SEVERITIES)[0]
    docs = [_issue_doc(f"Finding number {n}.", non_blocking, "transform_block") for n in range(6)]
    parsed = verdict_to_parsed_results(_parse(VERDICT_PASS, docs, {"Correctness": VERDICT_PASS}))
    assert len(retained_nonblocking_findings(parsed)) == 6

    detail = _detail_for(parsed, _EXEC_OK)
    assert "Finding number 0." in detail
    assert "Finding number 4." in detail
    assert "Finding number 5." not in detail, "the summary slice moved; Property 8's bound needs revisiting"
    # The count still describes all six, so it does not silently become a count
    # of what was printed.
    assert "6 retained as non-blocking" in detail


def test_a_long_description_is_truncated_outside_property_8s_bound():
    """The 1000-character ceiling is real, and the count survives it.

    Design §6 says truncation is "covered as an example, not contradicted by a
    property", and this is that example. Five findings with long descriptions
    push the detail past the ceiling, so the tail of the issue summary is cut —
    while the retained count, which sits ahead of the summary precisely for this
    reason (design §4.4, "the count sits ahead of the free-text summaries that
    truncation eats first"), is still readable.
    """
    non_blocking = sorted(NON_BLOCKING_SEVERITIES)[0]
    long_text = "This description is deliberately long so the detail exceeds its ceiling. " * 4
    docs = [_issue_doc(f"{n} {long_text}", non_blocking, "transform_block") for n in range(5)]
    parsed = verdict_to_parsed_results(_parse(VERDICT_PASS, docs, {"Correctness": VERDICT_PASS}))
    assert len(retained_nonblocking_findings(parsed)) == 5

    detail = _detail_for(parsed, _EXEC_OK)
    assert len(detail) == 1000, "the detail was not truncated, so this example no longer covers truncation"
    # What survives: the counts, both kinds, because they precede the free text.
    assert "5 retained as non-blocking" in detail
    assert "5 transform_block, 0 template_scaffolding" in detail
    # What does not: the last finding's text.
    assert f"4 {long_text}" not in detail


def test_a_defaulted_attribution_counts_but_is_not_retained():
    """The two numbers in the detail measure different things, on purpose.

    An issue whose ``attribution`` does not normalise to a recognised token is
    defaulted by the parser to ``("transform_block", "default")``, so it adds to
    the per-attribution ``transform_block`` count while never being retained —
    rule 1 does not match a defaulted attribution (``requirements.md`` do-NOT 7).

    Pinned as an example because it is the one case where a reader could
    reasonably expect the detail's two numbers to agree and be wrong, and because
    it is what makes Property 8's separate assertions on each of them necessary
    rather than redundant.
    """
    non_blocking = sorted(NON_BLOCKING_SEVERITIES)[0]
    assert not _normalises_to_a_recognised_attribution("generated")
    parsed = verdict_to_parsed_results(
        _parse(
            VERDICT_PASS,
            [
                _issue_doc("Reviewer said transform_block.", non_blocking, "transform_block"),
                _issue_doc("Reviewer said something else.", non_blocking, "generated"),
            ],
            {"Correctness": VERDICT_PASS},
        )
    )
    assert len(retained_nonblocking_findings(parsed)) == 1

    detail = _detail_for(parsed, _EXEC_OK)
    assert "2 transform_block, 0 template_scaffolding" in detail
    assert "1 retained as non-blocking" in detail
    # Both issues are still shown — the defaulted one is reported, it is just not
    # reported as retained.
    assert "Reviewer said something else." in detail
