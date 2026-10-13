# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p44-verify-against-real-source-data, Property 6: The report's frame
# claim equals the run's frame, and only a sample claims real source data
"""Property 6 — the report's frame claim equals the run's frame.

Spec ``p44-verify-against-real-source-data`` task 12.2, design §4.5, §6.2.

    *For any* frame outcome × *any* verdict branch, the rendered report carries
    the caveat and status wording for that frame outcome **and no other**; a
    claim of verification against the pipeline's own source data appears **only**
    when the frame was a Sample_Frame; a synthetic run carries
    ``GLUE_SPARK_CAVEAT`` byte-for-byte; a requested-but-unavailable run names its
    recorded reason; and a Sample_Read_Failure run carries no execution status, no
    record count and no execution-error block.

**Validates: Requirements 2.5, 5.2, 5.3, 5.4, 5.5**

THE LOAD-BEARING ASSERTION IS A NEGATIVE UNIVERSAL
--------------------------------------------------
:func:`test_property_6_the_reports_frame_claim_equals_the_runs_frame` asserts four
things, and they are not of equal weight. The three positive ones — this outcome's
caveat is present, this outcome's status parenthetical is present where a
parenthetical renders at all, this outcome's reason is named — would each be
satisfied by a builder that emitted *every* arm's wording at once. The one that
would not is **R5.5**: an own-source claim appears only on a Sample_Frame run that
verified something.

That is the dishonesty p44 exists to prevent, stated as a universal over a
population rather than as an example. A run that measured a transform against 100
rows invented from the declared schema and told a human approver it had verified
behaviour on their data is the exact defect p42 spent a spec removing from the
executor dimension, and wiring ``frame_mode``'s caller reopens it on the frame
dimension. p42's version of it was found on a live run
(``run-2026-08-18-19-45-13-a158b7e5``, "100 -> 0 records", where the 100 was
``_SYNTHETIC_FRAME_ROWS``); this file is what makes the frame version fail in CI
instead.

The negative is asserted **only-if, never iff**, and that is a measurement rather
than a hedge. Two of the four own-source claims — the ``overall_status``
parenthetical and the ``deployment_decision`` clause — render on only two of the
five verdict branches (PRODUCTION READY and the legacy no-``parsed_results`` arm,
which reach the same strings). An ``iff`` would fail on the other three
``sample`` cells and would be wrong to: R5.5 forbids the claim off a sample, it
does not require every carrier on one. :func:`test_every_own_source_claim_is_reachable`
is the anti-vacuity guard that keeps the ``only-if`` from being satisfied by a
phrase that no longer exists anywhere.

THE FOURTH OUTCOME IS THE ONE WITH NO CLAIM AT ALL, AND IT IS WHY 12.5 EXISTS
-----------------------------------------------------------------------------
Four frame outcomes reach the Glue arm, and only three of them are ``frame``
values — ``provenance()`` emits exactly three and task 5.2's
``test_frame_provenance.py`` pins that as an equality:

* ``synthetic`` — ``GLUE_SPARK_CAVEAT``, p42's bytes, the
  ``frame_provenance=None`` default (R5.3);
* ``sample`` — ``GLUE_SPARK_SAMPLE_CAVEAT``, **the only arm permitted to claim
  verification against the pipeline's own data** (R5.2, R5.5);
* ``sample_unavailable`` — ``GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT``, which names
  the recorded reason (R5.4);
* ``sample`` **plus a harness non-verification** —
  ``GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT`` (R2.5, task 12.5), selected by the
  frame *and* the run's ``executed`` flag.

The fourth is the cell the frame alone gets wrong. ``frame_provenance["frame"]`` is
``"sample"`` whenever ``FrameSource.is_sample`` — **including on a run whose source
read failed and where nothing executed** — so a builder that selected on the frame
alone would emit "real Spark semantics over real source data" for a frame-build
statement that died on ``AccessDenied``. Task 12.1 found that by rendering a real
``sample_read_failure_results`` rather than by reasoning about it. So this file
draws the read-failure outcome as a first-class member of the population and holds
it to the same negative universal as the synthetic arm: it may claim nothing.

WHAT IS DERIVED AND WHAT IS TRANSCRIBED
---------------------------------------
``LGR-28b4d75-5`` (**template / ratified**) is the posture — an expectation read
off the thing under test cannot detect it drifting — so every expectation here is
derived from a shipped constant where one isolates the wording, and transcribed
with a named guard where none does:

* **derived** — the four caveats (``_GLUE_FRAME_CAVEATS`` plus 12.5's fourth arm),
  the status parentheticals (``_GLUE_FRAME_STATUS_NOTES``), the decision clauses
  (``_GLUE_FRAME_DECISION_CLAUSES``), the reason ids and the provenance dicts
  (``FrameSource.provenance()``), and both harness results (the two shipped
  ``sample_read_*_results`` builders);
* **transcribed, guarded** — the two ``scope_tail`` strings and the three
  execution-fact markers, which are f-string fragments inside the builder with no
  constant to read. :func:`test_every_own_source_claim_is_reachable` and
  :func:`test_every_execution_fact_marker_is_reachable` fail with the phrase named
  if a reword drops one, instead of letting a negative universal pass because its
  needle vanished.

Comparing the status parentheticals needs the enclosing brackets and that is not
cosmetic: ``_GLUE_FRAME_STATUS_NOTES[FRAME_SYNTHETIC]`` (``synthetic input data``)
is a **substring** of the ``sample_unavailable`` note
(``synthetic input data — a sample was requested and unavailable``), so a bare
substring test would report the synthetic wording present on every unavailable
cell and the "and no other" half of the property would be unassertable.
:func:`test_the_status_notes_are_distinguishable_only_with_their_brackets` records
that measurement.

THE PROPERTY WAS CHECKED AGAINST MUTATIONS, NOT ONLY AGAINST THE CURRENT TREE
-----------------------------------------------------------------------------
A property that passes on a correct implementation has shown nothing until it has
also failed on an incorrect one. Five source-level mutations were applied to a
copy of the builder while writing this file, and **every one was caught** by
:func:`test_property_6_the_reports_frame_claim_equals_the_runs_frame`:

* task 12.5's caveat condition reverted (``if frame_sample_unverified:`` → never) —
  the read-failure arm reclaims ``GLUE_SPARK_SAMPLE_CAVEAT``, which is the exact
  rendering 12.1 measured on a frame-build statement that died on ``AccessDenied``;
* task 12.5's other half reverted — the sample ``scope_tail`` returns on a failed
  read, so the sentence after the caveat claims those rows were verified;
* ``_GLUE_FRAME_CAVEATS[FRAME_SYNTHETIC]`` pointed at the sample caveat — a wholly
  synthetic run claiming real source data, p44's defect in its purest form;
* the builder re-deriving ``frame`` from ``VERIFICATION_FRAME_MODE`` instead of the
  argument — R5.1, caught by the ambient-value draw and by nothing else here;
* the recorded unavailability reason replaced by the placeholder — R5.4.

The first four are all caught by assertion (3), the negative universal. That is the
evidence for calling it load-bearing rather than merely asserting that it is.

WHAT THIS FILE DOES NOT DO
--------------------------
* **No session, mocked or otherwise** (do-NOT 1). The report builder is a pure
  function of its arguments and is called directly; the only producer calls —
  ``FrameSource.provenance()`` and the two ``sample_read_*_results`` builders — are
  equally pure.
* **It asserts nothing about position.** Whether the frame claim lands inside
  ``review_summary``'s 500-character excerpt is Property 7 (task 13.1,
  ``test_frame_provenance_excerpt_prop.py``). The two properties fail for different
  reasons and a reader should not have to guess which: this one fails when the
  report says the wrong thing, that one when it says the right thing too late.
* **It asserts no verdict.** ``LGR-334667e-1``'s fence — execution success never
  overrides a blocking static failure — is untouched by task 12; the frame changes
  what the report says about its evidence, never which verdict that evidence
  reaches. ``test_caveat_does_not_alter_which_verdict_is_reached`` and
  ``test_the_verdict_is_identical_under_both_executors`` in
  ``test_comprehensive_report.py`` own that, unamended.
* **It edits nothing.** The ten surviving caveat assertions (task 12.3) stand.

RECORDS THAT GOVERN THE CODE UNDER TEST
---------------------------------------
* ``LGR-5c30733-3`` (**template / ratified**, **SUPERSEDED by p44** on one of its
  four claims) — the executor-branched caveat. Its stated basis ended
  "``frame_mode`` has no caller, so the frame is synthesised from the declared
  schema", which p44 removes. **Its other three claims are what this file also
  pins:** branch on the *resolved* executor
  (:func:`test_no_other_executor_carries_a_frame_claim`); take the value from the
  handler rather than reading the environment in the builder (the property's
  ``env_frame_mode`` draw, R5.1); ``shadow`` maps to the pandas caveat (same test).
* ``LGR-ad78946-5`` (**template / ratified**) — fall back and log rather than raise
  on an unsatisfiable configuration; it is why the builder ``.get``s the frame and
  an unrecognised value renders the conservative synthetic arm instead of raising
  (:func:`test_an_unrecognised_frame_value_takes_the_synthetic_arm`).
* ``LGR-9e76249-2`` and ``LGR-28b4d75-1`` (**template / ratified**) — derive once
  and let both consumers read the one derivation; the two precedents R5.1's
  "passed, never re-derived" rests on in other domains.
* ``LGR-28b4d75-5`` (**template / ratified**) — why every transcribed phrase below
  carries a reachability guard.
* ``LGR-334667e-1`` — cited by ``tasks.md`` and by the builder's docstring for the
  verdict-matrix fence. **It does not resolve in the ledger**; ``record()`` returns
  nothing for it, as tasks 12.1, 12.5 and 13.1 each found. Recorded so the next
  reader does not spend the lookup again. No verdict branch is touched here.
"""

import contextlib
import os
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
PANDAS = _mod.VERIFICATION_EXECUTOR_PANDAS
SHADOW = _mod.VERIFICATION_EXECUTOR_SHADOW
build_report = _mod.generate_comprehensive_test_report


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Sibling files import the handler under their own stubs and rebind the name."""
    sys.modules["lambda_function"] = _mod
    yield


# ══════════════════════════════════════════════════════════════════════
# The four frame outcomes, and the caveat each one must carry
# ══════════════════════════════════════════════════════════════════════

#: Labels used by this file. ``sample_read_failed`` is deliberately NOT a
#: ``frame`` value: ``provenance()`` emits three and task 5.2 pins that as an
#: equality. It is the ``sample`` frame plus a harness non-verification.
SYNTHETIC = "synthetic"
SAMPLE = "sample"
SAMPLE_UNAVAILABLE = "sample_unavailable"
SAMPLE_READ_FAILED = "sample_read_failed"

FRAME_OUTCOMES = (SYNTHETIC, SAMPLE, SAMPLE_UNAVAILABLE, SAMPLE_READ_FAILED)

#: ``outcome -> (constant name, unformatted caveat)``, read off the module.
#:
#: The first three are :data:`lambda_function._GLUE_FRAME_CAVEATS`' own values,
#: keyed by this file's labels rather than re-listed, so a mapping edit reaches
#: here. The fourth is 12.5's arm, which is deliberately **not** a fourth key in
#: that mapping — it is not a fourth frame — so it is named directly.
_CAVEAT_FOR_OUTCOME = {
    SYNTHETIC: ("GLUE_SPARK_CAVEAT", _mod._GLUE_FRAME_CAVEATS[_mod.FRAME_SYNTHETIC]),
    SAMPLE: ("GLUE_SPARK_SAMPLE_CAVEAT", _mod._GLUE_FRAME_CAVEATS[_mod.FRAME_SAMPLE]),
    SAMPLE_UNAVAILABLE: (
        "GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT",
        _mod._GLUE_FRAME_CAVEATS[_mod.FRAME_SAMPLE_UNAVAILABLE],
    ),
    SAMPLE_READ_FAILED: ("GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT", _mod.GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT),
}

#: The ``overall_status`` parenthetical each frame carries, unformatted.
#: ``sample_read_failed`` maps to ``None``: it cannot reach the verdict branch that
#: renders one, which :func:`test_the_read_failure_outcome_carries_no_frame_status_note`
#: measures rather than assumes.
_STATUS_NOTE_FOR_OUTCOME = {
    SYNTHETIC: _mod._GLUE_FRAME_STATUS_NOTES[_mod.FRAME_SYNTHETIC],
    SAMPLE: _mod._GLUE_FRAME_STATUS_NOTES[_mod.FRAME_SAMPLE],
    SAMPLE_UNAVAILABLE: _mod._GLUE_FRAME_STATUS_NOTES[_mod.FRAME_SAMPLE_UNAVAILABLE],
    SAMPLE_READ_FAILED: None,
}

#: The PRODUCTION READY ``deployment_decision`` clause each frame carries.
_DECISION_CLAUSE_FOR_OUTCOME = {
    SYNTHETIC: _mod._GLUE_FRAME_DECISION_CLAUSES[_mod.FRAME_SYNTHETIC],
    SAMPLE: _mod._GLUE_FRAME_DECISION_CLAUSES[_mod.FRAME_SAMPLE],
    SAMPLE_UNAVAILABLE: _mod._GLUE_FRAME_DECISION_CLAUSES[_mod.FRAME_SAMPLE_UNAVAILABLE],
    SAMPLE_READ_FAILED: None,
}


# ══════════════════════════════════════════════════════════════════════
# The own-source claims — R5.5's population, and the negative universal
# ══════════════════════════════════════════════════════════════════════

#: Every string in the report that CLAIMS verification against the pipeline's own
#: source data. R5.5 permits these on a Sample_Frame run that verified something
#: and nowhere else, so this dict is the whole content of the negative universal
#: and each entry earns its place by naming real data as *verified*, not merely by
#: mentioning a sample.
#:
#: Three are derived from the shipped constants; the fourth is the sample
#: ``scope_tail``, an f-string fragment in the builder with no constant to read, so
#: it is transcribed and guarded by
#: :func:`test_every_own_source_claim_is_reachable`.
#:
#: ``sample_scope_tail`` is the first line of the two-line tail only. The second
#: line opens with the blockquote continuation ``"> "`` that keeps the non-sample
#: rendering byte-identical to p42's, and including it would make this needle a
#: hostage to the report's line wrapping rather than to its claim.
OWN_SOURCE_CLAIMS = {
    "sample_caveat_tail": "real Spark semantics over real source data",
    "status_parenthetical": _mod._GLUE_FRAME_STATUS_NOTES[_mod.FRAME_SAMPLE].split("{")[0],
    "decision_clause_tail": _mod._GLUE_FRAME_DECISION_CLAUSES[_mod.FRAME_SAMPLE].split("—")[0].split("**,")[-1].strip(),
    "sample_scope_tail": "and behaviour on those sampled rows of the pipeline's own source object",
}

#: The scope sentence's tail, which is a frame claim too (R5.5). Exactly one of
#: these two renders on every Glue report, and the non-sample one is *true* on the
#: read-failure path rather than merely safe — the pipeline's real source data was
#: indeed not verified by that run, because the read of it is what failed.
#: Transcribed with the same guard as above.
NON_SAMPLE_SCOPE_TAIL = "and behaviour on the pipeline's real source data is"

#: What a run that executed reports, and a Sample_Read_Failure must not (R2.5).
#: Transcribed — they are f-string fragments — and guarded by
#: :func:`test_every_execution_fact_marker_is_reachable`.
EXECUTION_FACT_MARKERS = {
    "execution_status": "**Execution Status:**",
    "records_processed": "**Records Processed:**",
    "execution_error_block": "### ❌ Execution Error Details",
    "data_quality_block": "### Data Quality Assessment",
}


# ══════════════════════════════════════════════════════════════════════
# Cells — the frame outcome and the verdict branch
# ══════════════════════════════════════════════════════════════════════

#: A ``sourceBucket`` object URI of the shape ``file_validation`` writes.
SOURCE_URI = "s3://adi-source-files-123456789012-us-west-2/uploads/orders.csv"

#: The URI a JDBC-sourced pipeline carries (``schema_extract:747``), which is what
#: makes ``sample_unavailable`` the ordinary outcome for a whole class of pipelines
#: rather than an edge case (R4.4, R4.5).
JDBC_URI = "jdbc://postgres/public/orders"

#: The five verdict recipes, in the shape ``test_comprehensive_report.py`` and the
#: Property 7 sibling both use, so all three files measure the same cells.
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

#: The two doors onto ``source_read_failed``: a failed frame-build statement (R2.1)
#: and an elapsed statement deadline (R3.4). Both must reach the same claim.
STATEMENT_DOOR = "statement"
DEADLINE_DOOR = "deadline"

#: The reason ids the runner records, plus one longer hypothetical — drawn because
#: R5.4 requires the report to name the *recorded* reason, so a builder that
#: hardcoded one id would pass on a single-value axis.
UNAVAILABLE_REASONS = (
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
    "source_format_unrecognised_and_prefix_carried_no_readable_object",
)


def _sample_frame_source(uri: str = SOURCE_URI) -> FrameSource:
    read_format = sample_read_format(uri)
    assert read_format is not None, f"{uri} must resolve a reader for this fixture to be a sample"
    return FrameSource(
        mode=FRAME_MODE_SAMPLE, requested_mode=FRAME_MODE_SAMPLE, source_uri=uri, read_format=read_format
    )


def _provenance_for(outcome: str, *, unavailable_reason: str) -> dict:
    """The run's frame provenance, built by ``FrameSource.provenance()``.

    ``sample`` and ``sample_read_failed`` share a provenance dict: the frame WAS a
    Sample_Frame in both, and what separates them is the harness marker on the
    result. That sharing is the whole reason 12.5's arm cannot be selected by the
    frame alone, so it is reproduced here rather than papered over.
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
        source = _sample_frame_source()
    return source.provenance()


def _read_failure_results(door: str) -> dict:
    """A real harness non-verification for a failed source read.

    Built by the shipped producers, so the marker, the ``executed`` flag and
    ``error_details`` are whatever the module actually emits. Hand-building this
    dict would let R2.5's suppression be asserted against a shape no producer
    creates — which is how the defect 12.5 fixed survived p42.
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


def _parsed_results(branch: str, *, recommendation: str) -> dict | None:
    if branch == "legacy_no_parsed_results":
        return None
    categories = dict(_ALL_STATIC_PASS)
    if branch == "approve_with_fixes":
        categories["Performance"] = False
    elif branch == "review_required":
        categories["Security"] = False
    return {
        "test_categories": {
            name: {"status": "PASSED" if ok else "FAILED", "passed": ok} for name, ok in categories.items()
        },
        "recommendation": recommendation,
    }


@contextlib.contextmanager
def _ambient_frame_mode(value: str | None):
    """Set ``VERIFICATION_FRAME_MODE`` for the duration, then restore it exactly.

    R5.1's contradicting-environment draw needs the ambient value to be *wrong*
    while the argument is right. Restoring rather than deleting matters because
    hypothesis runs hundreds of examples in one process and a leaked value would
    make a later example's "unset" case silently a "set" case.
    """
    name = _mod.VERIFICATION_FRAME_MODE_ENV
    previous = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def render(
    outcome: str,
    branch: str,
    *,
    unavailable_reason: str = SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    records: int = 100,
    recommendation: str = "APPROVE",
    door: str = STATEMENT_DOOR,
    executor: str = GLUE,
    provenance: dict | None = None,
    ambient_frame_mode: str | None = None,
) -> tuple[str, dict | None]:
    """Render one cell. Returns ``(report, provenance)``.

    *provenance* overrides the outcome's own dict, for the malformed-input draws;
    *ambient_frame_mode* is the value ``VERIFICATION_FRAME_MODE`` carries while the
    builder runs, which must reach nothing (R5.1).
    """
    if provenance is None:
        provenance = _provenance_for(outcome, unavailable_reason=unavailable_reason)
    with _ambient_frame_mode(ambient_frame_mode):
        report = build_report(
            "STATIC ANALYSIS BODY",
            _execution_results(outcome, branch, records=records, door=door),
            _parsed_results(branch, recommendation=recommendation),
            executor=executor,
            frame_provenance=provenance,
        )
    return report, provenance


def _format_fields(provenance: dict | None) -> dict:
    """The substitution map the builder builds, reproduced from the provenance."""
    if not provenance:
        return {"row_bound": None, "reason": "reason not recorded"}
    return {
        "row_bound": provenance.get("row_bound"),
        "reason": provenance.get("unavailable_reason") or "reason not recorded",
    }


def rendered_caveat(outcome: str, provenance: dict | None) -> str:
    """The caveat the builder selects for this cell, formatted as it formats it."""
    _name, caveat = _CAVEAT_FOR_OUTCOME[outcome]
    return caveat.format(**_format_fields(provenance))


def rendered_status_parenthetical(outcome: str, provenance: dict | None) -> str | None:
    """This outcome's ``overall_status`` parenthetical, brackets included.

    **The brackets are load-bearing, not decoration.** The synthetic note is a
    prefix of the ``sample_unavailable`` note, so a bare substring test would find
    the synthetic wording on every unavailable cell and the property's "and no
    other" clause could not be asserted at all. See
    :func:`test_the_status_notes_are_distinguishable_only_with_their_brackets`.
    """
    note = _STATUS_NOTE_FOR_OUTCOME[outcome]
    if note is None:
        return None
    return f"({note.format(**_format_fields(provenance))})"


def rendered_decision_clause(outcome: str, provenance: dict | None) -> str | None:
    clause = _DECISION_CLAUSE_FOR_OUTCOME[outcome]
    if clause is None:
        return None
    return clause.format(**_format_fields(provenance))


# ══════════════════════════════════════════════════════════════════════
# Derivation guards for the transcribed needles (LGR-28b4d75-5)
# ══════════════════════════════════════════════════════════════════════


def test_every_own_source_claim_is_reachable():
    """Anti-vacuity for the negative universal, and the whole property rests on it.

    A phrase that appears in **no** report satisfies "appears only on a sample"
    trivially, so R5.5 would be asserted by four needles that had quietly stopped
    existing. This test requires each claim to render on at least one ``sample``
    cell, and names the claim on failure so a reword is diagnosed as a wording
    change rather than as a dishonesty.

    Two cells are enough to cover all four: the PRODUCTION READY arm renders the
    status parenthetical and the decision clause, and every branch renders the
    caveat and the scope tail.
    """
    reached: set[str] = set()
    for branch in VERDICT_BRANCHES:
        report, _provenance = render(SAMPLE, branch)
        reached |= {name for name, phrase in OWN_SOURCE_CLAIMS.items() if phrase in report}

    missing = sorted(set(OWN_SOURCE_CLAIMS) - reached)
    assert not missing, (
        f"these own-source claims render nowhere on a sample run: {missing}. "
        f"Re-read the constants and update OWN_SOURCE_CLAIMS — this is a wording change, "
        f"not an R5.5 violation, and leaving it makes the negative universal vacuous."
    )

    assert NON_SAMPLE_SCOPE_TAIL not in render(SAMPLE, "production_ready")[0], (
        f"the non-sample scope tail {NON_SAMPLE_SCOPE_TAIL!r} rendered on a sample run, so it no "
        f"longer distinguishes the two tails and the scope-tail assertion below is inert."
    )
    assert NON_SAMPLE_SCOPE_TAIL in render(SYNTHETIC, "production_ready")[0], (
        f"the non-sample scope tail {NON_SAMPLE_SCOPE_TAIL!r} renders nowhere. Re-read the builder's "
        f"scope_tail else-branch."
    )


def test_every_execution_fact_marker_is_reachable():
    """Anti-vacuity for R2.5's three suppressed facts.

    Same argument as above in the other direction: "the read-failure report carries
    no execution status" is satisfied for free by a marker string that nothing
    emits. Each marker is required to render on a cell where the transform DID run,
    so the suppression assertion is measuring an absence that means something.
    """
    executed_report, _ = render(SYNTHETIC, "production_ready")
    failed_report, _ = render(SYNTHETIC, "not_production_ready")

    for name, marker in EXECUTION_FACT_MARKERS.items():
        assert marker in executed_report or marker in failed_report, (
            f"the execution-fact marker {name}={marker!r} renders on no executed run. "
            f"Re-read the builder's else-branch and update EXECUTION_FACT_MARKERS — without this, "
            f"R2.5's suppression assertion passes vacuously."
        )


def test_the_status_notes_are_distinguishable_only_with_their_brackets():
    """Why :func:`rendered_status_parenthetical` compares ``(note)`` and not ``note``.

    ``synthetic input data`` is a prefix of
    ``synthetic input data — a sample was requested and unavailable``, so the two
    frames' parentheticals are not distinguishable by substring. The closing bracket
    is what separates them, and ``overall_status`` supplies one. Recorded as an
    assertion because a future edit that made the notes disjoint would make the
    brackets look like decoration a reader could drop.
    """
    synthetic = _STATUS_NOTE_FOR_OUTCOME[SYNTHETIC]
    unavailable = _STATUS_NOTE_FOR_OUTCOME[SAMPLE_UNAVAILABLE]

    assert synthetic in unavailable, (
        "the synthetic status note is no longer a prefix of the sample_unavailable one. "
        "The brackets in rendered_status_parenthetical are now belt-and-braces rather than "
        "load-bearing — harmless, but update this docstring rather than deleting them."
    )
    assert f"({synthetic})" not in f"({unavailable})"


# ══════════════════════════════════════════════════════════════════════
# Property 6
# ══════════════════════════════════════════════════════════════════════

_SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@given(
    outcome=st.sampled_from(FRAME_OUTCOMES),
    branch=st.sampled_from(VERDICT_BRANCHES),
    unavailable_reason=st.sampled_from(UNAVAILABLE_REASONS),
    records=st.integers(min_value=0, max_value=10**9),
    recommendation=st.sampled_from(["APPROVE", "NEEDS_IMPROVEMENT", "REJECT"]),
    door=st.sampled_from([STATEMENT_DOOR, DEADLINE_DOOR]),
    ambient_frame_mode=st.sampled_from([None, FRAME_MODE_SYNTHETIC, FRAME_MODE_SAMPLE, "", "SAMPLE", "not-a-mode"]),
)
@_SETTINGS
def test_property_6_the_reports_frame_claim_equals_the_runs_frame(
    outcome, branch, unavailable_reason, records, recommendation, door, ambient_frame_mode
):
    """*For any* frame outcome × verdict branch, the report's claim is that frame's.

    Five assertions, in the order the property statement makes them:

    1. **this outcome's caveat is present** (R5.2, R5.3, R5.4, and R2.5 for the
       fourth arm);
    2. **no other outcome's caveat is present**, and no pandas wording reaches a
       Glue run — the "and no other" clause, which is what stops the builder from
       satisfying (1) by emitting every arm;
    3. **an own-source claim appears only on a verifying sample** — R5.5, the
       negative universal, and the one that catches the dishonesty;
    4. **the scope sentence's tail matches the same frame**, because the sentence
       after the caveat is a frame claim too and a report whose caveat and next
       sentence disagreed would be the p42 defect in miniature;
    5. **a Sample_Read_Failure carries no execution status, no record count and no
       execution-error block** (R2.5), while still naming what the harness could
       not do — "not verified" must not be implemented as "not reported".

    ``ambient_frame_mode`` is R5.1's contradicting-environment draw. Every example
    renders twice — once with ``VERIFICATION_FRAME_MODE`` set to the drawn value,
    once with it absent — and requires the two to be **byte-identical**. A builder
    that read the environment would pass every other assertion here on the cell
    where the ambient value happened to agree with the argument, which is most of
    them; this is what makes the disagreement visible. ``LGR-5c30733-3``'s own
    reasoning, one level down: an environment read would claim a frame the run did
    not use, exactly as it would have claimed a Spark run that did not happen.

    ``records`` and ``recommendation`` are drawn to be **irrelevant** to the frame
    claim: a report whose frame wording moved with the row count or the reviewer's
    verdict would be deriving it from the wrong thing.
    """
    report, provenance = render(
        outcome,
        branch,
        unavailable_reason=unavailable_reason,
        records=records,
        recommendation=recommendation,
        door=door,
        ambient_frame_mode=ambient_frame_mode,
    )
    event(f"{outcome} | {branch} | door {door}")

    # ── R5.1 — the report follows the argument, not the ambient value ──
    neutral, _ = render(
        outcome,
        branch,
        unavailable_reason=unavailable_reason,
        records=records,
        recommendation=recommendation,
        door=door,
        ambient_frame_mode=None,
    )
    assert report == neutral, (
        f"{outcome} × {branch}: VERIFICATION_FRAME_MODE={ambient_frame_mode!r} changed the report. "
        f"R5.1 requires the frame to be PASSED from where the decision was made, never re-derived "
        f"in the builder — an environment read claims a frame the run did not use. "
        f"See `LGR-5c30733-3`, whose whole argument is this one level up."
    )

    # ── (1) this outcome's caveat is present ──
    caveat_name, _unformatted = _CAVEAT_FOR_OUTCOME[outcome]
    expected_caveat = rendered_caveat(outcome, provenance)
    assert expected_caveat in report, (
        f"{outcome} × {branch}: the report does not carry {caveat_name}.\n"
        f"  expected: {expected_caveat!r}\n"
        f"  scope line: {report.splitlines()[4] if len(report.splitlines()) > 4 else report[:200]!r}"
    )

    # ── (2) and no other outcome's caveat, and no pandas wording ──
    for other in FRAME_OUTCOMES:
        if other == outcome:
            continue
        other_name, _ = _CAVEAT_FOR_OUTCOME[other]
        other_caveat = rendered_caveat(other, provenance)
        assert other_caveat not in report, (
            f"{outcome} × {branch}: the report carries {other_name} as well as {caveat_name}. "
            f"One run has one frame; two caveats means the report is describing two."
        )
        other_parenthetical = rendered_status_parenthetical(other, provenance)
        assert other_parenthetical is None or other_parenthetical not in report, (
            f"{outcome} × {branch}: overall_status carries {other}'s frame parenthetical "
            f"{other_parenthetical!r}. The status line and the caveat must name one frame."
        )
        other_clause = rendered_decision_clause(other, provenance)
        assert other_clause is None or other_clause not in report, (
            f"{outcome} × {branch}: deployment_decision carries {other}'s clause {other_clause!r}."
        )

    assert _mod.PANDAS_PROXY_CAVEAT not in report, (
        f"{outcome} × {branch}: the pandas caveat reached a Glue run. `LGR-5c30733-3`'s surviving "
        f"claim is that the caveat branches on the RESOLVED executor; naming pandas here attributes "
        f"the verdict to a run that did not produce it."
    )

    # The status parenthetical and the decision clause render on only two of the
    # five branches, so their presence is conditional — but when either renders it
    # must be THIS frame's, which assertion (2) has already established for the
    # other three frames. What is left is that the one present is not a stale
    # default: on the two branches that render it, it is this outcome's.
    expected_parenthetical = rendered_status_parenthetical(outcome, provenance)
    if expected_parenthetical is not None and "REAL SPARK EXECUTION" in report:
        assert expected_parenthetical in report, (
            f"{outcome} × {branch}: overall_status reads REAL SPARK EXECUTION but carries no frame "
            f"parenthetical for this frame. Expected {expected_parenthetical!r}."
        )

    # ── (3) R5.5 — the negative universal ──
    # `sample` is the only frame that may claim its own source data, and only when
    # the harness actually verified something. A read failure carries the sample
    # FRAME and must claim nothing: that cell is 12.5's whole reason for existing.
    may_claim_own_source = outcome == SAMPLE
    present_claims = {name for name, phrase in OWN_SOURCE_CLAIMS.items() if phrase in report}
    if not may_claim_own_source:
        assert not present_claims, (
            f"{outcome} × {branch}: the report claims verification against the pipeline's own "
            f"source data on a run that had none. Offending claims: {sorted(present_claims)}.\n"
            f"  R5.5 permits an own-source claim ONLY on a Sample_Frame run that verified "
            f"something. This is the dishonesty p44 exists to prevent: a run measured against "
            f"invented rows — or against a sample it could not read — telling a human approver "
            f"it verified their data."
        )
    else:
        # Anti-vacuity on the positive side: the caveat's own claim is on every
        # sample cell, whatever the verdict branch does.
        assert "sample_caveat_tail" in present_claims, (
            f"{outcome} × {branch}: a verifying sample run carries no own-source claim at all "
            f"(present: {sorted(present_claims)}). R5.2 requires the report to state that the "
            f"transform ran over a bounded sample of the pipeline's own source object."
        )

    # ── (4) the scope sentence's tail names the same frame as the caveat ──
    tail_is_sample = OWN_SOURCE_CLAIMS["sample_scope_tail"] in report
    assert tail_is_sample == may_claim_own_source, (
        f"{outcome} × {branch}: the scope sentence's tail disagrees with the caveat about the "
        f"frame — sample tail present: {tail_is_sample}, expected: {may_claim_own_source}. "
        f"The sentence after the caveat is a frame claim too, and a report that stated real "
        f"source data was verified in one and unverified in the next is the p42 defect in "
        f"miniature."
    )
    assert (NON_SAMPLE_SCOPE_TAIL in report) != tail_is_sample, (
        f"{outcome} × {branch}: the two scope tails are not mutually exclusive. Exactly one "
        f"renders on every Glue report."
    )

    # ── (5) R5.4 — a requested-but-unavailable run names its recorded reason ──
    if outcome == SAMPLE_UNAVAILABLE:
        assert unavailable_reason in report, (
            f"{outcome} × {branch}: the report does not name the recorded unavailability reason "
            f"{unavailable_reason!r}. R5.4 requires the reason, not just the fact — it is what "
            f"tells a reader whether to go and look at the source object."
        )
        assert "reason not recorded" not in report, (
            f"{outcome} × {branch}: the builder rendered its missing-reason placeholder over a "
            f"provenance that carried {unavailable_reason!r}."
        )

    # ── (5) R2.5 — a Sample_Read_Failure states nothing about an execution ──
    if outcome == SAMPLE_READ_FAILED:
        leaked = {name for name, marker in EXECUTION_FACT_MARKERS.items() if marker in report}
        assert not leaked, (
            f"{outcome} × {branch}: the report states execution facts for a transform that was "
            f"never submitted. Offending sections: {sorted(leaked)}.\n"
            f"  R2.5 forbids an execution status, a record count and an execution-error block on "
            f"a Sample_Read_Failure. The frame-build statement failed, so nothing ran."
        )
        assert f"{records} → " not in report, (
            f"{outcome} × {branch}: a record count reached the report for a run with no execution."
        )
        # "not verified" must not be implemented as "not reported" (p32 2.20): the
        # non-verification itself, its reason and the object are all named.
        assert _mod.SOURCE_READ_FAILED_REASON in report, (
            f"{outcome} × {branch}: the report suppresses the execution facts but does not name "
            f"what the harness could not do. R2.5 asks for a Harness_Non_Verification NAMING the "
            f"source read, and p32 2.20's rule applies: not verified is not the same as not reported."
        )
        assert SOURCE_URI in report, f"{outcome} × {branch}: the report does not name the object whose read failed."


# ══════════════════════════════════════════════════════════════════════
# The cells the property's quantifiers cannot reach
# ══════════════════════════════════════════════════════════════════════


def test_the_synthetic_arm_is_the_default_and_carries_p42s_bytes():
    """R5.3 — ``frame_provenance=None`` renders ``GLUE_SPARK_CAVEAT`` byte-for-byte.

    This is the compatibility claim design §4.5 rests on, and it is why the ten
    surviving caveat assertions (task 12.3) needed no amendment: the default
    selects p42's exact bytes, so every one of the ~47 direct call sites that
    passes no provenance keeps its current meaning. Asserted on the constant, not
    on a transcription — a reword of ``GLUE_SPARK_CAVEAT`` is a do-NOT 18
    violation and fails here rather than silently redefining what those ten
    assertions pin.
    """
    for branch in VERDICT_BRANCHES:
        omitted, _ = render(SYNTHETIC, branch, provenance=None)
        assert _mod.GLUE_SPARK_CAVEAT in omitted, (
            f"{branch}: frame_provenance=None no longer selects GLUE_SPARK_CAVEAT. Every direct "
            f"call site that passes no provenance has silently changed meaning."
        )
        explicit, _ = render(SYNTHETIC, branch)
        assert omitted == explicit, (
            f"{branch}: an explicit synthetic provenance renders differently from omitting it. "
            f"The default must BE the synthetic arm, not merely resemble it."
        )

    # And the omitted-provenance report makes no own-source claim, which is the
    # same negative universal the property asserts, on the one input it cannot draw
    # (the property always passes a dict).
    omitted, _ = render(SYNTHETIC, "production_ready", provenance=None)
    assert not {name for name, phrase in OWN_SOURCE_CLAIMS.items() if phrase in omitted}


@pytest.mark.parametrize("executor", [PANDAS, SHADOW, "gibberish-executor"])
def test_no_other_executor_carries_a_frame_claim(executor):
    """The frame dimension exists only on the ``glue`` arm (design §4.5).

    A sample provenance is passed on every one of these, which is malformed input
    — ``CodeInterpreterTestRunner`` has no frame source, so the handler passes
    ``None`` there — and malformed input is exactly what must not produce an
    own-source claim. Two mappings composed rather than one two-dimensional table
    is what makes this hold: the frame is read after the executor, on that arm
    only.

    ``shadow`` maps to the pandas caveat and that is ``LGR-5c30733-3``'s surviving
    claim rather than a fallback: under shadow the authoritative result is the
    pandas one, and R1.6 additionally pins shadow's Glue half to a synthetic frame.
    The unrecognised label takes the pandas arm too — understate what ran, never
    claim Spark.
    """
    report, _provenance = render(SAMPLE, "production_ready", executor=executor)

    present = {name for name, phrase in OWN_SOURCE_CLAIMS.items() if phrase in report}
    assert not present, (
        f"executor={executor!r} carried own-source claims {sorted(present)} from a frame "
        f"provenance it has no business reading. The frame dimension applies on the glue arm only."
    )
    assert _mod.PANDAS_PROXY_CAVEAT in report, (
        f"executor={executor!r} no longer carries PANDAS_PROXY_CAVEAT (R9.6, do-NOT 6)."
    )
    for outcome in FRAME_OUTCOMES:
        _name, caveat = _CAVEAT_FOR_OUTCOME[outcome]
        assert caveat.format(row_bound=100, reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE) not in report


@pytest.mark.parametrize("bogus", ["SAMPLE", "Sample", "", "sample_frame", "synthetic ", None, 0])
def test_an_unrecognised_frame_value_takes_the_synthetic_arm(bogus):
    """``LGR-ad78946-5`` — the conservative arm, and it does not raise.

    A report is not the place to fail a run over a flag, so the builder ``.get``s
    the frame and falls to synthetic. The direction is the point: an unrecognised
    value must **understate** what was verified, never claim the pipeline's own
    data. ``"SAMPLE"`` and ``"Sample"`` are the near-misses a case change would
    produce, and ``"synthetic "`` the one a stray space would. ``None`` and ``0``
    are drawn to show the tolerance is not string-only.

    **An UNHASHABLE frame value does raise, and that is recorded rather than
    asserted.** ``frame_provenance={"frame": []}`` reaches
    ``_GLUE_FRAME_CAVEATS.get(frame, …)`` at ``lambda_function.py:2623`` and raises
    ``TypeError: cannot use 'list' as a dict key`` — measured while writing task
    12.2, not reasoned about. Read strictly, that is ``LGR-ad78946-5``'s own
    prohibition: a report failing a run over a flag. It is **not** asserted here,
    and the reason is that the record's subject is an *unsatisfiable
    configuration* — a mode string nobody recognises — whereas an unhashable value
    is a malformed dict no producer creates: ``FrameSource.provenance()`` sets
    ``frame`` from one of three module-level string literals
    (``glue_session_runner.py:472-486``), and the only other route into this
    parameter is a DynamoDB ``summary`` round-trip, which returns a string.
    Asserting a fix here would put a requirement on the builder that no
    requirement in p44 makes. Left as an observation for whoever hardens the
    seam; do not "fix" it inside a test task.
    """
    provenance = {**_provenance_for(SAMPLE, unavailable_reason=SAMPLE_UNAVAILABLE_NOT_S3_SOURCE), "frame": bogus}
    report, _ = render(SAMPLE, "production_ready", provenance=provenance)

    assert _mod.GLUE_SPARK_CAVEAT in report, (
        f"frame={bogus!r} did not fall back to the synthetic caveat. `LGR-ad78946-5` is the record: "
        f"fall back and log rather than raise, and the fallback must be the conservative arm."
    )
    present = {name for name, phrase in OWN_SOURCE_CLAIMS.items() if phrase in report}
    assert not present, (
        f"frame={bogus!r} produced own-source claims {sorted(present)}. An unrecognised frame value "
        f"must understate what was verified — R5.5 permits the claim on a Sample_Frame only, and "
        f"this is not one."
    )


def test_the_read_failure_outcome_carries_no_frame_status_note():
    """Why :data:`_STATUS_NOTE_FOR_OUTCOME` maps the fourth outcome to ``None``.

    A ``sample_read_failure_results`` fixes ``execution_successful: False`` and
    carries no proxy marker, so the verdict chain reaches
    ``elif not execution_successful`` whatever ``parsed_results`` says — the
    PRODUCTION READY arm, which is the only one that renders a frame parenthetical
    or a decision clause, is unreachable. That is measured here rather than assumed,
    because the mapping's ``None`` is what lets the property skip an assertion.

    It also means the ``sample`` frame's parenthetical — an own-source claim —
    cannot leak onto this path through the status line, which the property asserts
    from the other direction.
    """
    heads = set()
    for branch in VERDICT_BRANCHES:
        report, provenance = render(SAMPLE_READ_FAILED, branch)
        heads.add(report.splitlines()[2])
        for outcome in FRAME_OUTCOMES:
            parenthetical = rendered_status_parenthetical(outcome, provenance)
            assert parenthetical is None or parenthetical not in report, (
                f"{branch}: a frame parenthetical {parenthetical!r} rendered on a run that never executed."
            )

    assert len(heads) == 1, f"a Sample_Read_Failure reached {len(heads)} status lines: {sorted(heads)}"
    assert "PASSED" not in next(iter(heads))


def test_both_read_failure_doors_make_the_same_claim():
    """R2.1's door and R3.4's door reach one caveat, so one claim.

    The two builders differ in ``error_details`` — one names the read, the other
    names the read and the deadline — and that text renders below the scope block.
    So the frame claim is identical, and neither door may be the one that slips an
    own-source claim through.
    """
    statement_report, _ = render(SAMPLE_READ_FAILED, "not_production_ready", door=STATEMENT_DOOR)
    deadline_report, _ = render(SAMPLE_READ_FAILED, "not_production_ready", door=DEADLINE_DOOR)

    assert _mod.GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT.format(row_bound=100) in statement_report
    assert _mod.GLUE_SPARK_SAMPLE_READ_FAILED_CAVEAT.format(row_bound=100) in deadline_report
    for report in (statement_report, deadline_report):
        assert not {name for name, phrase in OWN_SOURCE_CLAIMS.items() if phrase in report}
        assert not {name for name, marker in EXECUTION_FACT_MARKERS.items() if marker in report}
    # The two reports must genuinely differ, below the claim — otherwise this test
    # is comparing one rendering with itself.
    assert statement_report != deadline_report
