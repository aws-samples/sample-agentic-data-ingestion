# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p44-verify-against-real-source-data, Property 7: The frame provenance
# is inside the first 500 characters
"""Property 7 — the frame provenance is inside the ``review_summary`` excerpt.

Spec ``p44-verify-against-real-source-data`` task 13.1, design §3 C15, §4.5.

    *For any* frame outcome × *any* verdict branch, the report's frame claim
    appears within ``report[:500]`` — the slice ``review_summary`` takes.

**Validates: Requirements 5.7**

WHY THIS IS A PROPERTY AND NOT AN EXAMPLE
-----------------------------------------
C15's headroom is a **snapshot**. On the synthetic frame the executor caveat ends
at report offset 486, so an edit that lengthens ``overall_status`` or the caveat
by more than 14 characters pushes the caveat's tail out of the excerpt, and an
example over one branch would not see it: within one frame the claim's offset
moves by up to 77 characters across the five verdict branches, because
``overall_status`` is a different string in each arm.
The requirements' own risk list predicted this failure mode ("if the
Frame_Provenance sentence lands after the static-analysis section, the
``review_summary`` excerpt will not carry it") one section *later* than it
actually occurs — the budget is spent before the static-analysis section starts,
not inside it.

WHAT IS ASSERTED, AND THE TWO MEASUREMENTS THAT DECIDED ITS SHAPE
-----------------------------------------------------------------
**(1) The assertion is against the frame-claim PHRASE, never the whole caveat.**
On two cells — ``sample_unavailable`` × ``production_ready`` and
``sample_unavailable`` × ``legacy_no_parsed_results`` — the caveat itself ends at
521 (530 with the longer of the two reason ids) and so overflows, while its
frame-claim phrase sits at 294-350 and does not. Tasks 12.1 and 12.5 measured that
independently, and this file re-measured it (see :data:`MEASURED_SPANS`). A
property asserting "the caveat is inside ``[:500]``" would fail on those two cells
and would be wrong to: R5.7 asks for the Frame_Provenance *statement* inside the
excerpt, and what falls outside on those cells is 21 characters reading
``'l production Glue job'`` — the tail of "not the full production Glue job",
which is not the frame statement. Asserting the whole caveat asserts more than the
requirement does.

**(2) There are two carriers, and which one carries varies by cell.** C15 found
two placements already inside the budget — ``overall_status`` and the executor
caveat — and this property uses them *as* two placements:

* the **caveat's frame-claim phrase**, present on every Glue report; and
* the **``overall_status`` parenthetical** (``_GLUE_FRAME_STATUS_NOTES``), present
  only on the two verdict branches whose status line reads
  ``PASSED - STATIC ANALYSIS + REAL SPARK EXECUTION (…)`` — the PRODUCTION READY
  arm and the legacy no-``parsed_results`` arm, which reach the same string. On
  ``review_required``, ``approve_with_fixes`` and ``not_production_ready`` the
  status line carries no frame claim at all, so on those three the caveat's phrase
  is the *only* carrier.

So the property asserts **one of the two carriers is inside the excerpt**, and
names which on failure.

**On this tree carrier 1 carries on all sixteen cells, and that is a measurement
rather than a redundancy.** The two cells whose *caveat* overflows are the ones the
producer's own comment (``lambda_function.py:2216-2220``) says the status
parenthetical carries — but that comment is reasoning about the whole caveat, and
the frame-claim phrase inside it ends at 350 on those cells, 150 characters clear.
So the disjunction's second term is currently slack, and the number that would
spend it is in :data:`MEASURED_SPANS`: the phrase's tightest cell is the sample
PRODUCTION READY arm ending at 398. The disjunction is kept anyway, because it is
what R5.7 asks for — the status note ``real source sample, 100 rows`` is a frame
statement, so a future edit that pushed the phrase out while leaving the note in
would still satisfy the requirement on the two branches that have a note. What
guards the slack from being lost silently is
:func:`test_the_measured_offsets_are_what_c15_measured`, which pins the phrase's
span on every cell as an equality: a phrase that moved out of the excerpt fails
there with the offset named, before the property has to decide anything.

WHERE THE 500 COMES FROM — DERIVED, NOT TRANSCRIBED
---------------------------------------------------
:data:`EXCERPT_LIMIT` is parsed out of ``lambda_function.py`` at collection time:
the one dict entry whose key is ``"review_summary"`` and whose value is a slice of
``review_result``. ``LGR-28b4d75-5`` (**template / ratified**) is the posture — an
expectation read off the thing under test cannot detect it drifting — and here it
buys two things: an edit that narrows the excerpt to 400 tightens this property
automatically, and an edit that removes the site fails
:func:`test_the_excerpt_slice_site_is_found_exactly_once` loudly rather than
leaving R5.7 asserted against a number nothing reads any more.

**The spec's citation for that site has drifted.** ``design.md`` and ``tasks.md``
both cite ``lambda_function.py:3143``; the slice is at **:3845** on disk today
(``requirements.md`` cites ``:3025``, older still). The line number is recorded
here as an observation, not asserted — the AST scan is what locates it. Two
sibling slices of the same variable exist and are deliberately NOT this one:
``raw_response_head`` at ``:3342`` (500, a parse-failure diagnostic) and
``test_report_markdown`` at ``:3857`` (5000, the state row's copy). Keying on the
``review_summary`` key rather than on ``review_result[:500]`` is what keeps them
apart.

THE FRAME OUTCOMES, AND WHY THE READ-FAILURE OUTCOME IS ONE CELL AND NOT FIVE
-----------------------------------------------------------------------------
Four frame outcomes reach the Glue arm's frame dimension:

* ``synthetic`` — ``GLUE_SPARK_CAVEAT``, p42's bytes, the ``frame_provenance=None``
  default;
* ``sample`` — ``GLUE_SPARK_SAMPLE_CAVEAT``, the only arm permitted to claim
  verification against the pipeline's own data (R5.5);
* ``sample_unavailable`` — ``GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT``, whose rendered
  length varies with the recorded reason id;
* ``sample`` **plus a harness non-verification** —
  ``GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT``, task 12.5's fourth arm, selected by the
  frame *and* the run's ``executed`` flag rather than by a fourth ``frame`` value.

The fourth pairs with exactly **one** verdict branch, and that is a fact about the
verdict chain rather than a choice made here: a ``sample_read_failure_results``
carries ``execution_successful: False``, the proxy marker is absent (the Glue
runner has no producer for it — ``LGR-ad78946-2``), so the chain always lands on
``elif not execution_successful`` regardless of what ``parsed_results`` says.
:func:`test_the_read_failure_outcome_reaches_one_verdict_branch` pins that,
because this file's own cell enumeration depends on it.

WHAT THIS FILE DOES NOT DO
--------------------------
* **No session, mocked or otherwise** (do-NOT 1). The report builder is a pure
  function of its arguments and is called directly; the only producer calls are
  ``FrameSource.provenance()`` and the two ``sample_read_*_results`` builders,
  which are equally pure.
* **It asserts nothing about honesty.** Which wording each frame gets, and the
  negative universal that only a sample may claim real source data, are Property 6
  (task 12.2). This file is about *position* only — the two properties fail for
  different reasons and a reader should not have to guess which.
* **It edits nothing.** The ten caveat assertions in
  ``test_comprehensive_report.py`` and ``test_report_names_the_running_executor.py``
  stand unamended; ``test_the_overall_status_line_names_the_executor`` records the
  measured offsets in prose and explicitly leaves the assertion to this file.

RECORDS THAT GOVERN THE CODE UNDER TEST
---------------------------------------
* ``LGR-5c30733-3`` (**template / ratified**, **SUPERSEDED by p44**) — the
  executor-branched caveat. Its stated basis was "``frame_mode`` has no caller, so
  the frame is synthesised from the declared schema", which p44 removes. Three of
  its four claims survive and none of them is about position.
* ``LGR-ad78946-5`` (**template / ratified**) — a report is not the place to fail a
  run over a flag; it is why the builder ``.get``s the frame and falls to the
  synthetic arm instead of raising, which is what makes an unrecognised ``frame``
  value renderable at all.
* ``LGR-0fe3e5d-5`` (**template / ratified**) — the retained-findings clause is
  appended to ``deployment_decision`` *after* the branch chain has chosen its
  wording. It is drawn here because it is the one tail that varies independently of
  the verdict, and it sits far past the excerpt: it must move no offset.
* ``LGR-28b4d75-5`` (**template / ratified**) — why the excerpt bound is parsed out
  of the producer and why every transcribed phrase below carries an anti-vacuity
  guard.
* ``LGR-334667e-1`` — cited by ``tasks.md`` and by the builder's own docstring for
  the verdict-matrix fence. **It does not resolve in the ledger**; ``record()``
  returns nothing for it, as tasks 12.1 and 12.5 both found. Recorded here so the
  next reader does not spend the lookup again. No verdict branch is touched, so
  nothing here depends on it.
"""

import ast
import os
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# Region/account before any boto3 import. LGR-3e45e65-1 pins the region in
# tests/unit/conftest.py, so these setdefaults are consistent no-ops.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The REAL producer module, imported before the stub block: every provenance dict
# and every harness result below is built by the shipped code rather than
# transcribed, so a producer renaming a key or a frame value fails these tests
# instead of passing them vacuously.
from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
    STAGE_FRAME_BUILD,
    FrameSource,
    GlueSessionError,
    StatementOutcome,
    sample_read_deadline_results,
    sample_read_failure_results,
    sample_read_format,
)

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

GLUE = _mod.VERIFICATION_EXECUTOR_GLUE
build_report = _mod.generate_comprehensive_test_report

_LAMBDA_SOURCE = pathlib.Path(_TEST_CODE_DIR) / "lambda_function.py"


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Sibling files import the handler under their own stubs and rebind the name."""
    sys.modules["lambda_function"] = _mod
    yield


# ══════════════════════════════════════════════════════════════════════
# The excerpt bound, parsed out of the producer (LGR-28b4d75-5)
# ══════════════════════════════════════════════════════════════════════


def _review_summary_slice_sites() -> list[tuple[int, int]]:
    """Every ``{"review_summary": review_result[:N]}`` site, as ``(line, N)``.

    Keyed on the **dict key** rather than on the slice expression, because
    ``review_result`` is sliced three times in this module and the other two are
    not the excerpt R5.7 is about: ``raw_response_head`` (also 500) and
    ``test_report_markdown`` (5000).
    """
    tree = ast.parse(_LAMBDA_SOURCE.read_text(encoding="utf-8"))
    sites: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            if not (isinstance(key, ast.Constant) and key.value == "review_summary"):
                continue
            if (
                isinstance(value, ast.Subscript)
                and isinstance(value.value, ast.Name)
                and value.value.id == "review_result"
                and isinstance(value.slice, ast.Slice)
                and isinstance(value.slice.upper, ast.Constant)
                and isinstance(value.slice.upper.value, int)
            ):
                sites.append((value.lineno, value.slice.upper.value))
    return sites


_SLICE_SITES = _review_summary_slice_sites()

#: The number of characters ``code_quality_metrics.review_summary`` carries.
#: Derived, so narrowing the slice tightens this property automatically.
EXCERPT_LIMIT = _SLICE_SITES[0][1] if _SLICE_SITES else 0


def test_the_excerpt_slice_site_is_found_exactly_once():
    """Anti-vacuity for :data:`EXCERPT_LIMIT`, and the whole file rests on it.

    With no site found, ``EXCERPT_LIMIT`` is 0 and every slice below is empty, so
    the property would fail loudly rather than silently — but it would fail for
    the wrong reason and name the wrong thing. This test is the one that names the
    right thing: the handler stopped writing ``review_summary`` from a bounded
    slice of the report, so R5.7's premise is gone and the requirement needs
    re-reading, not the property re-tuning.

    The line number is asserted only as being the one the AST found, not as a
    constant: ``:3143`` in the spec, ``:3025`` in ``requirements.md`` and ``:3845``
    on disk today are three snapshots of one site, and pinning any of them would
    make this test fail on an unrelated insertion above it.
    """
    assert len(_SLICE_SITES) == 1, f"expected exactly one review_summary slice site, found {_SLICE_SITES}"
    line, bound = _SLICE_SITES[0]
    assert bound == 500, f"the excerpt bound moved from 500 to {bound} at line {line} — re-measure C15's table"
    assert bound == EXCERPT_LIMIT


# ══════════════════════════════════════════════════════════════════════
# The two carriers
# ══════════════════════════════════════════════════════════════════════

#: The four frame outcomes, as this file labels them. ``sample_read_failed`` is
#: not a ``frame`` value — ``provenance()`` emits three and task 5.2 pins that as
#: an equality — it is the ``sample`` frame plus a harness non-verification.
SYNTHETIC = "synthetic"
SAMPLE = "sample"
SAMPLE_UNAVAILABLE = "sample_unavailable"
SAMPLE_READ_FAILED = "sample_read_failed"

FRAME_OUTCOMES = (SYNTHETIC, SAMPLE, SAMPLE_UNAVAILABLE, SAMPLE_READ_FAILED)

#: Carrier 1 — the frame-claim phrase inside each arm's caveat.
#:
#: **Transcribed on purpose, and guarded.** These are the words that make the
#: caveat a Frame_Provenance statement, and no expression in the module isolates
#: them: they are substrings of constants that also carry the "not the full
#: production Glue job" tail, which is not part of the claim.
#: :func:`test_every_frame_claim_phrase_is_still_inside_its_shipped_caveat` is the
#: derivation guard — a reword of any caveat that drops its phrase fails there
#: with the constant named, instead of leaving this property asserting the
#: position of a string that no longer exists.
#:
#: ``sample`` and ``sample_read_failed`` share a phrase, and that is the shipped
#: wording rather than an economy here: both name the same intended frame — one
#: verified it, the other could not read it. Which of them may CLAIM verification
#: is R5.5's question and Property 6's assertion, not this file's.
FRAME_CLAIM_PHRASES = {
    SYNTHETIC: "synthetic frame built from the declared source schema",
    SAMPLE: "sample of this pipeline's own source object",
    SAMPLE_UNAVAILABLE: "a real-source sample was requested and could not be read",
    SAMPLE_READ_FAILED: "sample of this pipeline's own source object",
}

#: Which shipped constant each phrase must still be inside.
_CAVEAT_FOR_OUTCOME = {
    SYNTHETIC: ("GLUE_SPARK_CAVEAT", _mod.GLUE_SPARK_CAVEAT),
    SAMPLE: ("GLUE_SPARK_SAMPLE_CAVEAT", _mod.GLUE_SPARK_SAMPLE_CAVEAT),
    SAMPLE_UNAVAILABLE: ("GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT", _mod.GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT),
    SAMPLE_READ_FAILED: ("GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT", _mod.GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT),
}


def test_every_frame_claim_phrase_is_still_inside_its_shipped_caveat():
    """The derivation guard for carrier 1 (``LGR-28b4d75-5``).

    Each phrase is asserted to be a substring of the caveat the builder selects
    for that outcome. Without this, a reworded caveat would leave the property
    searching the report for a phrase that is no longer in it — which fails, but
    reports "the frame claim left the excerpt" for a change that never touched a
    position. The message here names the constant instead.
    """
    for outcome, phrase in FRAME_CLAIM_PHRASES.items():
        name, caveat = _CAVEAT_FOR_OUTCOME[outcome]
        assert phrase in caveat, (
            f"{outcome}: the frame-claim phrase {phrase!r} is no longer inside {name}. "
            f"Re-read the constant and update FRAME_CLAIM_PHRASES — this is a wording change, "
            f"not an excerpt-position failure."
        )


def _status_note_for(provenance: dict | None) -> str | None:
    """Carrier 2 — the rendered ``overall_status`` parenthetical, or ``None``.

    Derived straight from :data:`lambda_function._GLUE_FRAME_STATUS_NOTES` and
    formatted the way the builder formats it, so no transcription and no guard is
    needed here. ``None`` for a frame the mapping does not know, which is the
    unrecognised-value arm the builder answers with the synthetic note.
    """
    if not provenance:
        return _mod._GLUE_FRAME_STATUS_NOTES[_mod.FRAME_SYNTHETIC]
    note = _mod._GLUE_FRAME_STATUS_NOTES.get(provenance.get("frame"))
    if note is None:
        return None
    return note.format(
        row_bound=provenance.get("row_bound"),
        reason=provenance.get("unavailable_reason") or "reason not recorded",
    )


def _span(report: str, needle: str | None) -> tuple[int, int] | None:
    """``(start, end)`` of *needle* in *report*, or ``None`` when absent."""
    if not needle:
        return None
    start = report.find(needle)
    if start < 0:
        return None
    return (start, start + len(needle))


# ══════════════════════════════════════════════════════════════════════
# Cells — the frame outcome and the verdict branch
# ══════════════════════════════════════════════════════════════════════

#: A ``sourceBucket`` object URI of the shape ``file_validation`` writes.
SOURCE_URI = "s3://adi-source-files-123456789012-us-west-2/uploads/orders.csv"

#: The URI a JDBC-sourced pipeline carries (``schema_extract:747``), which is what
#: makes ``sample_unavailable`` the ordinary outcome for a whole class of
#: pipelines rather than an edge case (design §4.10, R4.4/R4.5).
JDBC_URI = "jdbc://postgres/public/orders"

#: The five verdict recipes, copied in shape from
#: ``test_comprehensive_report.py::_all_verdict_branches_on`` so the cells this
#: file measures are the cells that file's ten caveat assertions describe.
VERDICT_BRANCHES = (
    "production_ready",
    "approve_with_fixes",
    "review_required",
    "not_production_ready",
    "legacy_no_parsed_results",
)

_ALL_STATIC_PASS = {
    "Correctness": True,
    "Schema Compliance": True,
    "Error Handling": True,
    "AWS Best Practices": True,
    "Security": True,
    "Performance": True,
}

#: The two doors onto ``source_read_failed``: a failed frame-build statement
#: (R2.1) and an elapsed statement deadline (R3.4). Both reach one caveat, which
#: :func:`test_both_read_failure_doors_render_the_same_head` measures.
STATEMENT_DOOR = "statement"
DEADLINE_DOOR = "deadline"


def _parsed(categories: dict[str, bool], recommendation: str) -> dict:
    return {
        "test_categories": {
            name: {"status": "PASSED" if ok else "FAILED", "passed": ok} for name, ok in categories.items()
        },
        "recommendation": recommendation,
    }


def _sample_frame_source(uri: str = SOURCE_URI) -> FrameSource:
    read_format = sample_read_format(uri)
    assert read_format is not None, f"{uri} must resolve a reader for this fixture to be a sample"
    return FrameSource(
        mode=FRAME_MODE_SAMPLE, requested_mode=FRAME_MODE_SAMPLE, source_uri=uri, read_format=read_format
    )


def _provenance_for(outcome: str, *, unavailable_reason: str, row_bound: int | None) -> dict:
    """The run's frame provenance, built by ``FrameSource.provenance()``.

    *row_bound* is applied by substitution rather than by argument, because
    ``provenance()`` derives it from ``_SAMPLE_FRAME_ROWS`` and takes no
    parameter. Substituting is how this file asks "what would the status note
    measure if that constant changed" — the note reads
    ``real source sample, {row_bound} rows``, so the digit count is one of the two
    things that can grow carrier 2.
    """
    if outcome == SYNTHETIC:
        source = FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode=FRAME_MODE_SYNTHETIC)
    elif outcome == SAMPLE_UNAVAILABLE:
        source = FrameSource(
            mode=FRAME_MODE_SYNTHETIC,
            requested_mode=FRAME_MODE_SAMPLE,
            source_uri=JDBC_URI,
            unavailable_reason=unavailable_reason,
        )
    else:
        # `sample` and `sample_read_failed` share a provenance: the frame WAS a
        # Sample_Frame in both, and what separates them is the harness marker on
        # the result (task 12.5 — three frame values, not four).
        source = _sample_frame_source()
    provenance = source.provenance()
    if provenance["row_bound"] is not None and row_bound is not None:
        provenance = {**provenance, "row_bound": row_bound}
    return provenance


def _read_failure_results(door: str) -> dict:
    """A real harness non-verification for a failed source read.

    Built by the shipped producers — the statement door takes a failed
    frame-build :class:`StatementOutcome`, the deadline door takes the
    :class:`GlueSessionError` ``_await_statement`` raises — so the marker, the
    ``executed`` flag and ``error_details`` are whatever the module actually emits.
    Hand-building this dict would let the builder's suppression branch be asserted
    against a shape no producer creates.
    """
    frame_source = _sample_frame_source()
    if door == DEADLINE_DOOR:
        return sample_read_deadline_results(
            GlueSessionError("statement did not reach a terminal state within 120s"), frame_source
        )
    return sample_read_failure_results(
        StatementOutcome(
            stage=STAGE_FRAME_BUILD,
            state="ERROR",
            output_status="error",
            stdout="",
            statement={
                "Output": {
                    "Status": "error",
                    "ErrorName": "AccessDeniedException",
                    "ErrorValue": "An error occurred (403) when calling the HeadObject operation: Forbidden",
                    "Traceback": [],
                }
            },
        ),
        frame_source,
    )


def _execution_results(outcome: str, branch: str, *, records: int, door: str) -> dict:
    if outcome == SAMPLE_READ_FAILED:
        return _read_failure_results(door)
    if branch == "not_production_ready":
        return {"execution_successful": False, "error_details": "AnalysisException: cannot resolve 'order_total'"}
    return {
        "execution_successful": True,
        "input_records": records,
        "output_records": records,
        "data_quality_results": {"overall_quality_score": 98},
    }


def _parsed_results(branch: str, *, recommendation: str, retained: int) -> dict | None:
    """``parsed_results`` for a branch, with the tails that vary independently.

    *retained* adds reviewer findings at ``minor`` severity —
    ``LGR-0fe3e5d-5``'s clause, which appends to ``deployment_decision`` and adds a
    "Reported, not blocking" line to the static summary. Both sit far past the
    excerpt, and the property's job includes noticing if that stops being true.
    """
    if branch == "legacy_no_parsed_results":
        return None
    categories = dict(_ALL_STATIC_PASS)
    if branch == "approve_with_fixes":
        categories["Performance"] = False
    elif branch == "review_required":
        categories["Security"] = False
    parsed = _parsed(categories, recommendation)
    if retained:
        parsed["issues"] = [
            {
                "severity": "minor",
                "attribution": "transform_block",
                "attribution_source": "reviewer",
                "description": f"finding {index}",
            }
            for index in range(retained)
        ]
    return parsed


#: A token that appears nowhere in the report template, prefixed to every
#: static-analysis body so "the reviewer's output is outside the excerpt" can be
#: asserted on one distinctive string instead of on a drawn one — a draw of ``":"``
#: is in the report head for reasons that have nothing to do with this claim.
STATIC_BODY_TAINT = "P44-STATIC-BODY-TAINT-e2f1"


def render(
    outcome: str,
    branch: str,
    *,
    unavailable_reason: str = SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    row_bound: int | None = None,
    records: int = 100,
    recommendation: str = "APPROVE",
    retained: int = 0,
    static_body: str = "",
    door: str = STATEMENT_DOOR,
) -> tuple[str, dict]:
    """Render one cell. Returns ``(report, provenance)``."""
    provenance = _provenance_for(outcome, unavailable_reason=unavailable_reason, row_bound=row_bound)
    return (
        build_report(
            f"{STATIC_BODY_TAINT}\n{static_body}",
            _execution_results(outcome, branch, records=records, door=door),
            _parsed_results(branch, recommendation=recommendation, retained=retained),
            executor=GLUE,
            frame_provenance=provenance,
        ),
        provenance,
    )


def carriers(report: str, outcome: str, provenance: dict) -> dict[str, tuple[int, int] | None]:
    """The two carriers' spans, plus the whole caveat's, for diagnostics."""
    return {
        "claim": _span(report, FRAME_CLAIM_PHRASES[outcome]),
        "status": _span(report, _status_note_for(provenance)),
        "caveat": _span(report, _rendered_caveat(outcome, provenance)),
    }


def _rendered_caveat(outcome: str, provenance: dict) -> str:
    """The caveat the builder selects for this cell, formatted as it formats it.

    Reproduced from the shipped constants rather than extracted from the report,
    so the caveat span reported on failure is the span of the string the module
    chose — not of whatever happened to match.
    """
    fields = {
        "row_bound": provenance.get("row_bound"),
        "reason": provenance.get("unavailable_reason") or "reason not recorded",
    }
    _name, caveat = _CAVEAT_FOR_OUTCOME[outcome]
    return caveat.format(**fields)


# ══════════════════════════════════════════════════════════════════════
# Property 7
# ══════════════════════════════════════════════════════════════════════

_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])

#: The two reason ids the runner actually records, plus one longer hypothetical.
#:
#: **Draw both real ones**: they are 17 and 26 characters
#: (``source_uri_not_s3``, ``source_format_unrecognised``), and the longer one is
#: what pushes the ``sample_unavailable`` caveat from 521 to 530 on the two cells
#: where it already overflows. The third draw is the direction a future reason id
#: would grow, and it is safe to assert against because the frame-claim phrase
#: ends *before* the ``({reason})`` the id lands in.
UNAVAILABLE_REASONS = (
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
    "source_format_unrecognised_and_prefix_carried_no_readable_object",
)


@given(
    outcome=st.sampled_from(FRAME_OUTCOMES),
    branch=st.sampled_from(VERDICT_BRANCHES),
    unavailable_reason=st.sampled_from(UNAVAILABLE_REASONS),
    row_bound=st.sampled_from([None, 1, 100, 10_000, 1_000_000_000]),
    records=st.integers(min_value=0, max_value=10**9),
    recommendation=st.sampled_from(["APPROVE", "NEEDS_IMPROVEMENT", "REJECT"]),
    retained=st.integers(min_value=0, max_value=3),
    static_body=st.text(max_size=400),
    door=st.sampled_from([STATEMENT_DOOR, DEADLINE_DOOR]),
)
@_SETTINGS
def test_property_7_one_frame_carrier_is_inside_the_review_summary_excerpt(
    outcome, branch, unavailable_reason, row_bound, records, recommendation, retained, static_body, door
):
    """*For any* frame outcome × verdict branch, a frame carrier is in the excerpt.

    The assertion is the disjunction C15's two placements describe: the caveat's
    frame-claim phrase, or the ``overall_status`` parenthetical. Both spans, the
    whole caveat's span and the winning carrier's headroom are in the message, so
    a failure names *what* grew rather than only that something did.

    The axes past the two named in the property statement are there to be
    **irrelevant**, and asserting that is the point: the static-analysis body, the
    record counts, the reviewer recommendation and the retained-findings count all
    render *after* the excerpt boundary, so none of them may move a carrier. The
    static body is the load-bearing one — it is interpolated at
    ``# Original Static Analysis Report``, below the execution section, and a
    reordering that moved it above would take every frame claim out of the excerpt
    at once.
    """
    report, provenance = render(
        outcome,
        branch,
        unavailable_reason=unavailable_reason,
        row_bound=row_bound,
        records=records,
        recommendation=recommendation,
        retained=retained,
        static_body=static_body,
        door=door,
    )
    spans = carriers(report, outcome, provenance)
    excerpt = report[:EXCERPT_LIMIT]

    inside = {name: bool(span and span[1] <= EXCERPT_LIMIT) for name, span in spans.items()}
    event(f"{outcome} | {branch} | status carrier: {'present' if spans['status'] else 'absent'}")

    assert spans["claim"] is not None, (
        f"{outcome} × {branch}: the report carries no frame claim at all. "
        f"Expected {FRAME_CLAIM_PHRASES[outcome]!r} somewhere in it."
    )

    assert inside["claim"] or inside["status"], (
        f"{outcome} × {branch}: R5.7 is unmet — neither frame carrier is inside "
        f"report[:{EXCERPT_LIMIT}].\n"
        f"  frame-claim phrase : {spans['claim']} (overflows by {spans['claim'][1] - EXCERPT_LIMIT})\n"
        f"  overall_status note: {spans['status']}\n"
        f"  whole caveat       : {spans['caveat']}\n"
        f"  the excerpt ends: ...{excerpt[-60:]!r}\n"
        f"C15's headroom was a snapshot. Something ahead of the frame claim grew — "
        f"overall_status or the caveat's opening. Branch the frame claim into a string "
        f"that is already inside the excerpt; do NOT append a sentence (do-NOT 19)."
    )

    # The measured offsets, asserted alongside (task 13.1). The winning carrier's
    # headroom is the number a future editor spends, so it is reported at every
    # example rather than only on failure.
    carrier = "claim" if inside["claim"] else "status"
    headroom = EXCERPT_LIMIT - spans[carrier][1]
    assert headroom >= 0
    event(f"{outcome} | carrier {carrier} | headroom {headroom}")

    # The tail that must stay past the boundary. Stated as an assertion because
    # "it renders later in the template" is exactly the kind of claim that stops
    # being true when a section is reordered — and asserted on a distinctive taint
    # rather than on the drawn text, because a short draw ("::", "#") occurs in the
    # report head for reasons that have nothing to do with the static section.
    assert STATIC_BODY_TAINT in report, "the static-analysis body is no longer interpolated at all"
    assert STATIC_BODY_TAINT not in excerpt, (
        f"{outcome} × {branch}: the static-analysis body reached the excerpt. "
        f"The execution section no longer precedes it, so every frame claim's "
        f"position is now a function of the reviewer's output length."
    )


def test_the_excerpt_is_invariant_under_everything_that_renders_after_it():
    """Why the property's other seven axes are drawn: to be inert, measurably.

    The record counts, the reviewer recommendation, the retained-findings count and
    the static-analysis body all render below the scope block — the execution status
    line, the data-quality block, the static summary and ``deployment_decision`` are
    each past character 500 on every cell in :data:`MEASURED_SPANS`. So the excerpt
    must be byte-identical across them, and this is the cheap direct measurement of
    that; the property's ``static_body`` taint assertion is the same claim under a
    quantifier.

    ``REJECT`` is excluded deliberately rather than overlooked: it makes
    ``has_blocking`` true, which moves the run to a different verdict branch and so
    to a different ``overall_status`` — a real change to the head, not a tail.
    """
    for outcome in FRAME_OUTCOMES:
        quiet, _ = render(outcome, "production_ready", records=0, recommendation="APPROVE", retained=0)
        noisy, _ = render(
            outcome,
            "production_ready",
            records=1_000_000_000,
            recommendation="NEEDS_IMPROVEMENT",
            retained=3,
            static_body="x" * 400,
        )
        assert quiet[:EXCERPT_LIMIT] == noisy[:EXCERPT_LIMIT], (
            f"{outcome}: something that renders after the excerpt moved into it. "
            f"The frame claim's position is now a function of the reviewer's output."
        )
        # Anti-vacuity: the two reports must genuinely differ, past the boundary.
        assert quiet != noisy


def test_the_read_failure_outcome_reaches_one_verdict_branch():
    """12.5's "one cell, not five", and this file's enumeration depends on it.

    A ``sample_read_failure_results`` fixes ``execution_successful: False`` and
    carries no proxy marker, so the verdict chain reaches
    ``elif not execution_successful`` whatever ``parsed_results`` says. That is why
    :data:`MEASURED_SPANS` has one row for this outcome against five for each of
    the other three, and why the property's ``branch`` axis is inert for it.

    Asserted through the rendered head — the ``overall_status`` line — rather than
    by reading the builder's branch conditions, so it stays true of the report
    rather than of a reading of the code.
    """
    heads = set()
    for branch in VERDICT_BRANCHES:
        report, _provenance = render(SAMPLE_READ_FAILED, branch)
        heads.add(report.splitlines()[2])
    assert len(heads) == 1, f"a Sample_Read_Failure reached {len(heads)} status lines: {sorted(heads)}"
    assert "PASSED" not in next(iter(heads))


def test_both_read_failure_doors_render_the_same_head():
    """R2.1's door and R3.4's door reach one caveat, so one set of offsets.

    The two builders differ in ``error_details`` — one names the read, the other
    names the read and the deadline — and that text renders *below* the scope
    block, in the harness arm. So the frame claim's position is identical, and the
    table below needs one row rather than two.
    """
    statement_report, provenance = render(SAMPLE_READ_FAILED, "not_production_ready", door=STATEMENT_DOOR)
    deadline_report, _ = render(SAMPLE_READ_FAILED, "not_production_ready", door=DEADLINE_DOOR)

    assert carriers(statement_report, SAMPLE_READ_FAILED, provenance) == carriers(
        deadline_report, SAMPLE_READ_FAILED, provenance
    )
    assert statement_report[:EXCERPT_LIMIT] == deadline_report[:EXCERPT_LIMIT]


# ══════════════════════════════════════════════════════════════════════
# The measured table — C15's snapshot, in code
# ══════════════════════════════════════════════════════════════════════

#: ``(frame outcome, verdict branch) → (claim span, caveat span, status span)``,
#: measured on this tree with the SHORT unavailability reason id.
#:
#: **This is a characterization snapshot, not a requirement.** R5.7 is the
#: disjunction the property asserts; these numbers are the measurement C15 rests
#: on, and their whole value is that a failure here names *which* string grew and
#: by how much. A change that shortens the head is harmless and still fails this
#: test — re-measure, update the row, and check the property still passes. That is
#: the practice ``LGR-60c4985-6`` (**template / ratified**) asks for: record the
#: reversal at the assertion so a later reader finds the reason with the
#: requirement.
#:
#: Re-measured for task 13.1. The claim and caveat spans are identical to task
#: 12.5's table on all sixteen cells. ``status`` is ``None`` on the three verdict
#: branches whose ``overall_status`` carries no frame note, which is why carrier 1
#: is the only one available there.
#:
#: **One column is measured differently from 12.5's table, deliberately.** For
#: ``sample_unavailable`` 12.5 recorded the status carrier as ``158-196`` — the
#: frame-claim *sub-phrase* ``a sample was requested and unavailable`` inside the
#: note — where this file records ``135-196``, the **whole** note
#: ``synthetic input data — a sample was requested and unavailable``. Same end
#: offset, and the end is what the boundary tests; the start differs because
#: carrier 2 needs no transcription at all (it is
#: ``_GLUE_FRAME_STATUS_NOTES[frame]`` formatted), so measuring the whole note
#: asserts more for free. 135 is where every arm's parenthetical starts, which is
#: also what ``test_the_overall_status_line_names_the_executor`` records in prose
#: for the synthetic arm.
MEASURED_SPANS: dict[tuple[str, str], tuple[tuple[int, int], tuple[int, int], tuple[int, int] | None]] = {
    (SYNTHETIC, "production_ready"): ((331, 384), (253, 486), (135, 155)),
    (SYNTHETIC, "approve_with_fixes"): ((313, 366), (235, 468), None),
    (SYNTHETIC, "review_required"): ((311, 364), (233, 466), None),
    (SYNTHETIC, "not_production_ready"): ((295, 348), (217, 450), None),
    (SYNTHETIC, "legacy_no_parsed_results"): ((331, 384), (253, 486), (135, 155)),
    (SAMPLE, "production_ready"): ((355, 398), (261, 483), (135, 163)),
    (SAMPLE, "approve_with_fixes"): ((329, 372), (235, 457), None),
    (SAMPLE, "review_required"): ((327, 370), (233, 455), None),
    (SAMPLE, "not_production_ready"): ((311, 354), (217, 439), None),
    (SAMPLE, "legacy_no_parsed_results"): ((355, 398), (261, 483), (135, 163)),
    (SAMPLE_UNAVAILABLE, "production_ready"): ((294, 350), (294, 521), (135, 196)),
    (SAMPLE_UNAVAILABLE, "approve_with_fixes"): ((235, 291), (235, 462), None),
    (SAMPLE_UNAVAILABLE, "review_required"): ((233, 289), (233, 460), None),
    (SAMPLE_UNAVAILABLE, "not_production_ready"): ((217, 273), (217, 444), None),
    (SAMPLE_UNAVAILABLE, "legacy_no_parsed_results"): ((294, 350), (294, 521), (135, 196)),
    (SAMPLE_READ_FAILED, "not_production_ready"): ((257, 300), (217, 462), None),
}


@pytest.mark.parametrize("cell", sorted(MEASURED_SPANS), ids=lambda cell: f"{cell[0]}-{cell[1]}")
def test_the_measured_offsets_are_what_c15_measured(cell):
    """The offsets, asserted alongside the property (task 13.1).

    A failure here is the diagnostic the property cannot give on its own: the
    property says "R5.7 still holds", this says "the head is 12 characters longer
    than it was, in the caveat, on the sample arm". The two smallest numbers in
    this table are the ones a future editor spends first — the caveat ends at 486
    on the synthetic PRODUCTION READY cell (14 characters of slack) and at 483 on
    the sample one (17) — and the frame-claim phrase's own tightest cell is the
    sample PRODUCTION READY arm at 398, with 102.
    """
    outcome, branch = cell
    expected_claim, expected_caveat, expected_status = MEASURED_SPANS[cell]
    report, provenance = render(outcome, branch)
    spans = carriers(report, outcome, provenance)

    assert spans["claim"] == expected_claim, (
        f"{outcome} × {branch}: the frame claim moved from {expected_claim} to {spans['claim']} "
        f"({(spans['claim'] or (0, 0))[0] - expected_claim[0]:+d} characters ahead of it). "
        f"Headroom to the {EXCERPT_LIMIT}-character boundary is now "
        f"{EXCERPT_LIMIT - (spans['claim'] or (0, EXCERPT_LIMIT))[1]}."
    )
    assert spans["caveat"] == expected_caveat, (
        f"{outcome} × {branch}: the caveat moved from {expected_caveat} to {spans['caveat']}. "
        f"C15's 14-character headroom is measured on this number."
    )
    assert spans["status"] == expected_status, (
        f"{outcome} × {branch}: the overall_status frame note moved from {expected_status} "
        f"to {spans['status']} — carrier 2 is what R5.7 rests on for the two cells whose "
        f"caveat overflows."
    )


def test_the_longer_reason_id_moves_the_caveat_end_and_not_the_claim():
    """Why both reason ids are drawn, in the two cells where it matters.

    ``sample_unavailable``'s caveat interpolates the recorded reason id, so its
    rendered length varies: 521 with ``source_uri_not_s3`` (17 characters) and 530
    with ``source_format_unrecognised`` (26). Both overflow. The frame-claim phrase
    ends at 350 either way, because the id lands in a ``({reason})`` that comes
    *after* the phrase — which is the structural reason asserting the phrase rather
    than the caveat is not merely more lenient, it is measuring the right thing.
    """
    for branch, expected_short, expected_long in (
        ("production_ready", 521, 530),
        ("legacy_no_parsed_results", 521, 530),
    ):
        short_report, short_prov = render(
            SAMPLE_UNAVAILABLE, branch, unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
        )
        long_report, long_prov = render(
            SAMPLE_UNAVAILABLE, branch, unavailable_reason=SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT
        )
        short = carriers(short_report, SAMPLE_UNAVAILABLE, short_prov)
        long = carriers(long_report, SAMPLE_UNAVAILABLE, long_prov)

        assert short["caveat"][1] == expected_short
        assert long["caveat"][1] == expected_long
        assert short["caveat"][1] > EXCERPT_LIMIT and long["caveat"][1] > EXCERPT_LIMIT
        # The claim and the status note — the two carriers — do not move.
        assert short["claim"] == long["claim"] == (294, 350)
        assert short["status"] == long["status"] == (135, 196)
