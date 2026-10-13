# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The retention log: which way the severity floor went, said out loud (p43 task 12.2).

Task 12.1 put two mutually exclusive log lines in the handler, immediately after
``parsed_results = verdict_to_parsed_results(review_verdict)``
(``lambda_function.py:2748-2763``) and beside the ``logger.info`` that already
names the raw verdict and the issue count. This file pins them **through the
handler**, which is the only place they exist: neither line is in
``review_verdict.py`` (R5.3) and neither is in ``aggregate_check_results``, so a
test calling a helper directly could not see either one.

What is asserted here
---------------------

* **R5.1** — a run whose gate retained findings logs the retention, naming the
  count and each retained severity.
* **R5.2** — the retained line and the blocking line are at **distinct levels**:
  WARNING for retained, INFO for blocking. Asserted as a two-point comparison
  (each run is checked for the presence of its own level *and* the absence of the
  other), following ``test_verification_executor_selection.py:242-254``
  (``test_the_role_downgrade_is_reported_at_error_not_warning``), which pins the
  ``LGR-ad78946-5`` distinction the same way: one record at the level claimed,
  and an empty list at the level not claimed. A one-sided assertion would pass
  against an implementation that logged both facts at one level, which is exactly
  what that record forbids — "logging both at one level makes them
  indistinguishable".
* **R7.4 / R5.3, the emission site** — the gate line is emitted from
  ``lambda_function.py`` and not from ``review_verdict.py``, so the
  observability work landed outside the Review_Gate.

Why the direction is WARNING = retained and INFO = blocking
-----------------------------------------------------------

``LGR-ad78946-5`` (**template / ratified**) requires only that the two be
distinct. The direction is design §4.6's choice, on which fact would otherwise
leave no trace: a blocking finding already announces itself four other ways (the
step fails, ``determine_code_approval_requirement`` logs it, the report reaches
its review-required banner, an approval is requested), whereas a retained
finding on a green run appears nowhere except inside the embedded reviewer JSON.
It is a deliberate departure from this module's usual WARNING = "outside the
vocabulary" reading, recorded at the log site. **Settled; these assertions are
not the place to relitigate it.**

Sibling coverage — deliberately NOT duplicated here
---------------------------------------------------

* **The substring/AST form of test 5.3** lives in
  ``test_review_verdict_purity_source.py``
  (``test_the_gate_and_the_retention_helpers_contain_no_logging``, task 4.3),
  which covers ``effective_verdict`` *and* both retention helpers, over the
  parsed AST rather than raw source text — because the raw-substring form is red
  today against correct code: ``retained_nonblocking_findings``' docstring says
  "free of logging". Restating a weaker, brittler copy of an assertion that
  already holds would add a second oracle that fails on a docstring edit, so
  this file asserts the complementary half instead: *where the log record was
  actually emitted from*, which an AST scan of ``review_verdict.py`` cannot see.
* ``test_retained_findings_helpers_prop.py`` — that the two helpers partition
  the findings and that the two branches are mutually exclusive (Property 12).
  This file takes that as given and asserts what the handler does with it.
* ``test_review_gate_calibration.py`` — the gate outcome, the check detail and
  the approval payload on the same runs.

**Validates: Requirements 5.1, 5.2, 5.3, 7.4**
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Region/account before any boto3 import.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub the heavy / AWS-touching imports so the module loads in isolation — the
# same dict every handler test in this directory builds.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
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

import review_verdict as _rv
from review_verdict import blocking_transform_block_findings, parse_review_verdict, retained_nonblocking_findings


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


#: Every gate line task 12.1 emits opens with this. Both branches are matched by
#: one prefix on purpose: the filter must be able to see a line the run was not
#: supposed to emit, which is the whole content of the R5.2 comparison. Filtering
#: each branch by its own wording would make the absence half vacuous.
_GATE_LINE_PREFIX = "Review gate "

#: One recognised blocking token and one unrecognised one. Both take rule 1's
#: blocking arm (p43 R1.1 and R1.2) and therefore the same log branch, so this is
#: the cheapest pair that shows the branch is keyed on the *floor* rather than on
#: an enumeration of the two recommended blocking tokens. The full severity
#: matrix is ``test_review_verdict_severity_matrix.py``'s job, not this file's.
_BLOCKING_SEVERITIES = ("critical", "blocker")

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
}

_EXEC_OK = {"execution_successful": True, "input_records": 10, "output_records": 10}


def _verdict_json(raw_verdict: str, issues: list[dict]) -> str:
    """A reviewer response carrying ``issues``, built the way the model sends it.

    Every issue is explicitly ``transform_block``-attributed, which is what makes
    it a Reviewer_Transform_Block_Finding rather than a parser default
    (``review_verdict.py:215-222``) — rule 1's other conjunct. The categories all
    pass, so nothing except the issue severities can move the gate.
    """
    return json.dumps(
        {
            "verdict": raw_verdict,
            "issues": [{"category": "Correctness", "attribution": "transform_block", **issue} for issue in issues],
            "categories": {"Correctness": "pass", "Security": "pass"},
        }
    )


def _fake_dynamodb() -> MagicMock:
    """A ``dynamodb`` stand-in whose pipelines table returns a record with no schema.

    A record that EXISTS is required — a bare ``MagicMock`` answers ``False`` to
    ``"Item" in response``, which the handler reads as a missing pipeline record
    and fails the step (p33 requirement 2.8) before it ever reaches the log site.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-1"}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def _run_handler(review_text: str) -> None:
    """Drive ``lambda_handler`` with every AWS/agent dependency mocked.

    Nothing is returned: the observable under test is the log, which the caller
    holds through ``caplog``. The handler's own return value is asserted on by
    ``test_review_gate_calibration.py``.

    ``USE_AGENTCORE_RUNTIME=false`` keeps the run on the in-process branch and
    ``CodeInterpreterTestRunner`` is a ``MagicMock``, so no Glue interactive
    session is created here (requirements do-NOT 9 — they bill).
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value=review_text)
    agent.add_hook = MagicMock()

    runner = MagicMock()
    runner.run_test.return_value = _EXEC_OK

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1"}, None)


def _gate_lines(caplog) -> list:
    """The records task 12.1 emits, whichever branch produced them."""
    return [record for record in caplog.records if record.getMessage().startswith(_GATE_LINE_PREFIX)]


# ---------------------------------------------------------------------------
# 5.1 / 5.2 — the retained run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("severity", sorted(_rv.NON_BLOCKING_SEVERITIES))
def test_the_retained_run_names_the_retention_at_warning_and_no_gate_info_line(severity, caplog):
    """**Validates: Requirements 5.1, 5.2** — a retained finding is a WARNING
    naming the count and the severity, and the blocking line is absent.

    The run is the one this spec exists for: raw verdict ``pass``, one explicit
    reviewer transform-block finding at a Non_Blocking_Severity, so the gate
    passes and the finding is retained (p43 R1.3/R1.6). Before the floor this run
    failed, and the operator's only trace of the two findings was the embedded
    reviewer JSON — R5.1 is what replaces that silence.

    Parametrized over ``NON_BLOCKING_SEVERITIES`` rather than the literal
    ``"minor"`` so the constant stays the single source of truth (p43 R2.4): a
    second token added there is covered here without an edit, which is the
    difference the requirement's risk 4 was about.

    The absence half is asserted at INFO specifically, not "no other level": the
    handler logs plenty at INFO on this run (the raw verdict, the executor, the
    stage) and R5.2 is about the two GATE facts being distinguishable, so the
    filter is the gate-line prefix and the claim is that the blocking branch did
    not also fire.
    """
    with caplog.at_level("DEBUG"):
        _run_handler(_verdict_json("pass", [{"severity": severity, "description": "Nit in a comment."}]))

    gate_lines = _gate_lines(caplog)
    assert len(gate_lines) == 1, f"expected exactly one gate line, got {[r.getMessage() for r in gate_lines]}"

    retained_line = gate_lines[0]
    assert retained_line.levelname == "WARNING"
    message = retained_line.getMessage()
    assert "retained" in message
    assert "1" in message, f"the count is not in the record: {message!r}"
    assert severity in message, f"the retained severity is not in the record: {message!r}"

    assert [record for record in gate_lines if record.levelname == "INFO"] == [], (
        "the blocking branch fired on a run that blocked nothing — the two branches are supposed to be "
        "mutually exclusive by construction (design §4.6, Property 12)"
    )


def test_the_retained_count_is_the_number_of_findings_not_a_constant(caplog):
    """**Validates: Requirements 5.1** — the count in the record is derived.

    A second run of the test above with ``1`` swapped for ``2`` would pass
    against an implementation that hard-coded ``1``, and the count is half of
    what R5.1 asks the line to name. Two retained findings, so the number has to
    move.

    Both carry the same severity because ``NON_BLOCKING_SEVERITIES`` has one
    member today (requirements risk 4) — so this pins the count, and the
    parametrized test above pins the severity.
    """
    findings = [
        {"severity": "minor", "description": "Nit in a comment."},
        {"severity": "minor", "description": "Another nit."},
    ]
    verdict_json = _verdict_json("pass", findings)

    # Non-vacuity: the fixture really does retain BOTH findings, so a count of 2
    # is the fixture's own claim and not this test's guess about the gate.
    assert len(retained_nonblocking_findings(parse_review_verdict(verdict_json))) == 2

    with caplog.at_level("DEBUG"):
        _run_handler(verdict_json)

    gate_lines = _gate_lines(caplog)
    assert len(gate_lines) == 1
    assert gate_lines[0].levelname == "WARNING"
    assert "2" in gate_lines[0].getMessage()


# ---------------------------------------------------------------------------
# 5.1 / 5.2 — the blocking run, the other point of the comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("severity", _BLOCKING_SEVERITIES)
def test_the_blocking_run_names_the_finding_at_info_and_no_retention_warning(severity, caplog):
    """**Validates: Requirements 5.2** — the blocking fact is INFO, and the
    retention WARNING is absent.

    The second point of the level comparison. The fixture is a raw ``pass``
    carrying an explicit reviewer transform-block finding at a Blocking_Severity,
    which is the shape that makes **rule 1** fire — deliberately, not any run
    whose effective verdict is ``fail``. Task 12.1's ``elif`` keys on the
    blocking-finding LIST, so a rule-2 failure (a raw ``fail`` with one
    unattributed issue) is an effective fail with an EMPTY blocking list and logs
    no gate line at all. A fixture built from ``effective_verdict(...) == "fail"``
    would therefore be able to pass while the branch was keyed on the wrong
    thing.

    The absence assertion is over WARNING among the gate lines, not over the
    whole log: the handler warns elsewhere for its own reasons, and the claim
    R5.2 makes is about these two records.
    """
    verdict_json = _verdict_json("pass", [{"severity": severity, "description": "The transform drops rows."}])

    # Non-vacuity guard: rule 1 is what fires here. ``blocking_transform_block_findings``
    # is non-empty only when rule 1 fired, so this distinguishes the intended run
    # from a rule-2 effective fail that would log nothing.
    assert len(blocking_transform_block_findings(parse_review_verdict(verdict_json))) == 1

    with caplog.at_level("DEBUG"):
        _run_handler(verdict_json)

    gate_lines = _gate_lines(caplog)
    assert len(gate_lines) == 1, f"expected exactly one gate line, got {[r.getMessage() for r in gate_lines]}"

    blocking_line = gate_lines[0]
    assert blocking_line.levelname == "INFO"
    message = blocking_line.getMessage()
    assert "blocking" in message
    assert "1" in message, f"the count is not in the record: {message!r}"
    assert severity in message, f"the blocking severity is not in the record: {message!r}"

    assert [record for record in gate_lines if record.levelname == "WARNING"] == [], (
        "a blocking run announced a retention — the retained list is empty unless the effective verdict is "
        "pass (review_verdict.py:361), so this would mean the log is reading something else"
    )


# ---------------------------------------------------------------------------
# 5.3 / 7.4 — the observability work landed outside the gate
# ---------------------------------------------------------------------------


def test_the_gate_line_is_emitted_from_the_handler_not_from_the_gate_module(caplog):
    """**Validates: Requirements 5.3, 7.4** — the record comes from
    ``lambda_function.py``, never from ``review_verdict.py``.

    R5.3 keeps the Review_Gate free of logging calls so R7.4's purity claim
    survives this spec's observability work, and ``review_verdict.py`` is copied
    verbatim into the AgentCore container, where a logging import would arrive as
    a second thing to configure (``review_verdict.py:56-58``).

    ``test_review_verdict_purity_source.py`` asserts the source-level half of
    that — no logging identifier anywhere in the gate or either helper, over the
    AST — and this asserts the runtime half it cannot see: that the record which
    *does* exist was emitted from the handler module. A logging call added to
    ``review_verdict.py`` and paired with the handler line being deleted would
    keep every other assertion in this file green; the emitting file is what
    tells them apart.

    Read off ``LogRecord.pathname``, which is the caller's file because
    Powertools' ``Logger`` forwards ``stacklevel=2`` to the stdlib logger — so
    the record is attributed to the ``logger.warning`` call site rather than to
    Powertools' own module.
    """
    with caplog.at_level("DEBUG"):
        _run_handler(_verdict_json("pass", [{"severity": "minor", "description": "Nit in a comment."}]))

    gate_lines = _gate_lines(caplog)
    assert len(gate_lines) == 1

    emitting_file = os.path.basename(gate_lines[0].pathname)
    assert emitting_file == "lambda_function.py", (
        f"the gate line was emitted from {emitting_file!r}. R5.3: the retention log belongs at the handler; "
        "the Review_Gate and its helpers stay free of logging calls."
    )
