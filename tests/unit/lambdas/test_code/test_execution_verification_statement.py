# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: the absence of execution evidence, stated AT the success claim (p40 R6).

Covers p40 task 9.1's two pure helpers and the five statement sites they feed.

**The assertion that matters** is not that a refusal is labelled — it is that a
run which *did* execute is NOT labelled "never executed". A ``schema_compliance``
entry carrying ``verified: False`` / ``no_target_schema`` (p33 2.6) shares the
three-state ``verified`` flag with a proxy refusal, so a helper keyed on
:func:`is_not_verified_check` instead of on the check NAME would report such a
run as unexecuted — a NEW false claim made in the course of removing one, which
is what ``execution_was_verified``'s docstring is about. So the
``schema_compliance`` case below is built by the real producers
(``_parse_results`` → ``aggregate_check_results``) and asserts that the two
predicates genuinely DISAGREE on it: ``is_not_verified_check`` True,
``execution_was_verified`` True.

What this file closes that nothing pinned before: the **test-results table item**
and the **handler return `message`**. The other three sites had incidental
coverage — ``test_executed_check_counts.py:455`` pins ``test_result``'s exact key
set (amended in place by 9.1), ``test_artifact_schema.py`` /
``test_output_data_contract.py`` cover ``output_data`` (the latter's summary
assertion is ``>=`` and is over ``create_code``, so added summary keys are inert
for it), and the state row is covered by
``test_state_row_write_preserves_feedback.py``. Neither the table item's
``execution_verified`` nor the amended ``message`` was asserted anywhere.

Two do-NOTs are asserted rather than assumed, because R6 was allowed to state
the truth only by adding things that change no decision:

* ``validation_status``'s value set is unchanged — still only
  ``"validated" | "failed"`` at all three emitting sites, and it still says
  ``"validated"`` on a non-verified run (§4.7 do-NOT / N5). The boolean, the
  note, the report heading and the dashboard row are what carry the truth.
* nothing added feeds a gate (§4.7 Q2, decided "no" — p32 2.21 stands):
  ``approval_required``, ``quality_gate_requires_approval``,
  ``regeneration_needed`` and ``test_status`` answer identically on a
  non-verified run and on an executed one, asserted as a pair below.

Ledger notes, so a reader knows which claims are governed:

* ``LGR-69ca78b-1`` (**invariant**) — both governance Choice states fail closed.
  Adding keys inside ``test_result`` is inert because the only Choice over it
  reads ``.status`` (``orchestration-stack.ts:853-854``); the gate-parity test
  below is that inertness made checkable.
* ``LGR-60c4985-4`` (**template**) — ``output_data`` is fixed to five keys, which
  is why the note rides inside ``summary``. Asserted over
  ``create_output_data``'s own return; the handler's sixth ``"approval"`` key is
  on the ENVELOPE and is pre-existing (``lambda_function.py:2064``).
* ``LGR-b2f79ef-2`` (**template**) — a ``verified: False`` entry must never be
  the only check. The ``schema_compliance`` fixture below draws a full check list
  so the D2 fence is not exercised here; extending that fence's generator is task
  12.3's work, not this file's.
* ``LGR-99050e6-1`` (**template**) — colour is not the carrier; text is. Every
  assertion here is over a boolean and a sentence.

Validates: Requirements R6, R9
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Region/account before any boto3 import (LGR-3e45e65-1 pins these in
# tests/unit/conftest.py; these setdefaults are consistent no-ops).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The runner and the converter are the REAL modules: every non-verification
# fixture below is produced by the shipped producers rather than transcribed, so
# a producer renaming a key fails these tests instead of passing them vacuously.
from code_interpreter_runner import (
    NOT_VERIFIED_REASON_FRAME_TRUNCATED,
    NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA,
    NOT_VERIFIED_REASON_NOT_YET,
    NOT_VERIFIED_REASON_PERMANENT,
    NOT_VERIFIED_REASON_SCALAR_LITERAL,
    CodeInterpreterTestRunner,
)
from pyspark_to_pandas import ConversionError

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


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# Production-shaped fixtures
# ---------------------------------------------------------------------------

_VALIDATED_OUTPUT = (
    'GENERATED:{"record_count": 3}\n'
    'VALIDATION:{"record_count": 3, "completeness": 100, "uniqueness": 100, '
    '"schema_compliance": 100, "overall_quality": 100, "null_counts": {}, '
    '"duplicates": 0, "missing_columns": []}'
)


def _runner() -> CodeInterpreterTestRunner:
    """A runner whose bedrock-agentcore client is a stub but non-None."""
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = MagicMock()
        runner = CodeInterpreterTestRunner(region="us-east-1")
    return runner


def _executed_with_validation_output() -> dict:
    """The shape a run that EXECUTED and produced ``VALIDATION:`` output returns.

    Built by the real ``_parse_results`` rather than hand-written, because the
    whole point of the ``schema_compliance`` case is that this run executed:
    ``data_quality_results`` carrying ``schema_compliance`` IS the evidence of
    execution (``aggregate_check_results``' gating comment), and a transcribed
    dict could drift from what the parser actually writes.
    """
    results = _runner()._parse_results(_VALIDATED_OUTPUT)
    assert results["execution_successful"] is True, "guard: fixture must represent a run that executed"
    assert "schema_compliance" in results["data_quality_results"], "guard: validation output must carry the score"
    return results


def _proxy_refusal(reason: str | None = NOT_VERIFIED_REASON_SCALAR_LITERAL) -> dict:
    """A pandas-proxy refusal, produced by ``_conversion_failure_results``."""
    return CodeInterpreterTestRunner._conversion_failure_results(
        ConversionError("cast", 'df = df.withColumn("s", F.lit(None).cast("string"))', 7, reason)
    )


def _harness_refusal(reason: str = NOT_VERIFIED_REASON_FRAME_TRUNCATED) -> dict:
    """A test-harness refusal, produced by ``_frame_truncation_results``."""
    return CodeInterpreterTestRunner._frame_truncation_results(600, 512, reason)


_PASS_VERDICT = {"verdict": "pass", "issues": [], "categories": {"Correctness": "pass", "Security": "pass"}}


def _parsed_results() -> dict:
    """``verdict_to_parsed_results`` output for a clean pass — the real shape."""
    return _mod.verdict_to_parsed_results(_mod.parse_review_verdict(json.dumps(_PASS_VERDICT)))


# ---------------------------------------------------------------------------
# execution_was_verified — the two execution-replacing names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("execution_results", "expected_name"),
    [
        (_proxy_refusal(), _mod.NOT_VERIFIED_CHECK_NAME),
        (_harness_refusal(), _mod.NOT_VERIFIED_BY_HARNESS_CHECK_NAME),
    ],
    ids=["proxy_refusal", "harness_refusal"],
)
def test_execution_was_verified_is_false_for_each_execution_replacing_check_name(execution_results, expected_name):
    """Either execution-replacing name in the list means no transform ran.

    Both names are appended from an ``elif`` arm that would otherwise have
    appended ``code_interpreter_execution``, so their presence is the absence of
    an execution — and the check list is produced here by
    ``aggregate_check_results`` over the real producer's output, not assembled by
    hand.
    """
    status, checks = _mod.aggregate_check_results(_parsed_results(), execution_results)

    names = [check["name"] for check in checks]
    assert expected_name in names, names
    assert "code_interpreter_execution" not in names, (
        f"guard: the refusal must REPLACE the execution check, not sit beside it — {names}"
    )
    assert _mod.execution_was_verified(checks) is False
    # p32 2.21 / Q2: a refusal alone still does not derive "failed".
    assert status == "passed", checks


def test_execution_was_verified_is_true_when_a_schema_compliance_entry_is_not_verified():
    """A ``verified: False`` ``schema_compliance`` entry belongs to a run that RAN.

    This is the false-claim case ``execution_was_verified``'s docstring exists
    for. The entry is emitted only when ``data_quality_results`` carries a parsed
    ``VALIDATION:`` payload, and only an execution produces one — so labelling
    this run "never executed" would replace one false claim with another.

    Non-vacuity is asserted three ways: the entry really carries p33's two-key
    encoding, the two predicates genuinely DISAGREE on it, and
    ``NO_TARGET_SCHEMA_REASON`` is absent from the label map (a mapped reason
    would mean the note could render for a run that executed).
    """
    checks = _mod.aggregate_check_results(_parsed_results(), _executed_with_validation_output(), target_schema=None)[1]

    entry = next(check for check in checks if check["name"] == _mod.SCHEMA_COMPLIANCE_CHECK_NAME)
    assert entry["verified"] is False
    assert entry["not_verified_reason"] == _mod.NO_TARGET_SCHEMA_REASON

    # The two predicates disagree, and that disagreement is the requirement.
    assert _mod.is_not_verified_check(entry) is True
    assert _mod.execution_was_verified(checks) is True
    assert _mod.execution_verification_note(checks) == ""

    # The run that carried it executed: the execution check is present and passed.
    execution_check = next(check for check in checks if check["name"] == "code_interpreter_execution")
    assert execution_check["passed"] is True
    # LGR-b2f79ef-2's caveat: a `verified: False` entry is never the only check.
    assert len(checks) > 1, checks

    assert _mod.NO_TARGET_SCHEMA_REASON not in _mod.NOT_VERIFIED_REASON_LABELS, (
        "no_target_schema is a verified: False reason for a run that EXECUTED — mapping it would "
        "let the note render for such a run"
    )


# ---------------------------------------------------------------------------
# execution_verification_note — the label, the silence, and the two fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check_name", "reason"),
    [
        (_mod.NOT_VERIFIED_CHECK_NAME, NOT_VERIFIED_REASON_PERMANENT),
        (_mod.NOT_VERIFIED_CHECK_NAME, NOT_VERIFIED_REASON_NOT_YET),
        (_mod.NOT_VERIFIED_CHECK_NAME, NOT_VERIFIED_REASON_SCALAR_LITERAL),
        (_mod.NOT_VERIFIED_BY_HARNESS_CHECK_NAME, NOT_VERIFIED_REASON_FRAME_TRUNCATED),
        (_mod.NOT_VERIFIED_BY_HARNESS_CHECK_NAME, NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA),
    ],
)
def test_execution_verification_note_states_the_label_for_every_mapped_reason(check_name, reason):
    """Each of the five mapped reasons renders its own label in one sentence.

    The reasons are bound to the PRODUCER's constants rather than transcribed, so
    a producer renaming one fails here instead of silently falling through to the
    raw-reason branch.
    """
    checks = [
        {"name": "ai_review_verdict", "passed": True, "detail": "x"},
        {"name": check_name, "passed": False, "detail": "y", "verified": False, "not_verified_reason": reason},
    ]

    note = _mod.execution_verification_note(checks)

    assert note == f"Transform NOT executed — {_mod.NOT_VERIFIED_REASON_LABELS[reason]}."
    assert _mod.execution_was_verified(checks) is False


def test_execution_verification_note_is_empty_when_the_transform_executed():
    checks = _mod.aggregate_check_results(_parsed_results(), _executed_with_validation_output())[1]

    assert _mod.execution_verification_note(checks) == ""
    assert _mod.execution_was_verified(checks) is True


def test_execution_verification_note_falls_back_to_the_raw_reason_when_unmapped():
    """A sixth reason still says something true rather than nothing.

    R6 is about saying so, so an unmapped reason renders the raw string instead
    of falling silent.
    """
    checks = [
        {
            "name": _mod.NOT_VERIFIED_CHECK_NAME,
            "passed": False,
            "detail": "x",
            "verified": False,
            "not_verified_reason": "some_reason_a_later_producer_adds",
        }
    ]

    assert _mod.execution_verification_note(checks) == ("Transform NOT executed — some_reason_a_later_producer_adds.")


def test_execution_verification_note_says_reason_not_stated_when_the_reason_is_absent():
    """A refusal that names no reason is still reported as unexecuted."""
    checks = [{"name": _mod.NOT_VERIFIED_BY_HARNESS_CHECK_NAME, "passed": False, "detail": "x", "verified": False}]

    assert _mod.execution_verification_note(checks) == "Transform NOT executed — reason not stated."
    assert _mod.execution_was_verified(checks) is False


# ---------------------------------------------------------------------------
# The five statement sites, through the handler
# ---------------------------------------------------------------------------

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
}


def _fake_dynamodb() -> MagicMock:
    """A ``dynamodb`` stand-in whose pipeline record exists and names no schema.

    Requirement 2.6's legitimate state, and the state that keeps the
    ``schema_compliance`` non-verification reachable: a *missing* record would
    raise StepFailure (2.8) instead.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-1"}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def _state_rows(runs_table) -> list[dict]:
    """Reconstruct the state rows the handler wrote (a SET update, not a put)."""
    rows = []
    for call in runs_table.update_item.call_args_list:
        values = {name.lstrip(":"): value for name, value in call.kwargs["ExpressionAttributeValues"].items()}
        if "step_sequence" not in values:
            continue
        rows.append({**call.kwargs["Key"], **values})
    return rows


def _run_handler(execution_results: dict):
    """Drive ``lambda_handler`` with every AWS/agent dependency stubbed.

    Modelled on ``test_executed_check_counts.py::_run_handler``. moto cannot
    express what this needs — a Code Interpreter result to inject and a review
    agent to script — so the module-attribute patch harness is the pattern in
    this directory.

    Returns ``(response, state_row, results_table_item, test_output)`` — the four
    objects the five statement sites are observable in.
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    results_table = MagicMock()
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value=json.dumps(_PASS_VERDICT))
    agent.add_hook = MagicMock()

    runner = MagicMock()
    runner.run_test.return_value = execution_results

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", results_table),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        response = _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1"}, None)

    state_rows = [row for row in _state_rows(runs_table) if row.get("step_name") == "test_code"]
    assert len(state_rows) == 1, "expected exactly one test_code state row"

    results_items = [call.kwargs["Item"] for call in results_table.put_item.call_args_list]
    assert len(results_items) == 1, "expected exactly one test-results table item"

    test_outputs = [
        json.loads(call.kwargs["Body"].decode("utf-8"))
        for call in s3.put_object.call_args_list
        if call.kwargs["Key"].endswith("test_results.json")
    ]
    assert len(test_outputs) == 1, "expected exactly one test_results.json object"

    return response, state_rows[0], results_items[0], test_outputs[0]


def _original_message(response: dict) -> str:
    """The pre-p40 sentence, rebuilt from the response's own honest counts.

    The note is APPENDED to this (``LGR-60c4985-6`` / ``LGR-b2f79ef-6``: amended
    in place, not replaced), so comparing against it is what makes "byte-identical
    original sentence" checkable rather than asserted in prose.
    """
    counts = response["test_results"]
    return (
        f"Test analysis completed. {counts['checks_passed']}/{counts['checks_run']} checks passed. Report stored in S3."
    )


@pytest.mark.parametrize(
    ("execution_results", "reason"),
    [
        (_proxy_refusal(), NOT_VERIFIED_REASON_SCALAR_LITERAL),
        (_harness_refusal(), NOT_VERIFIED_REASON_FRAME_TRUNCATED),
    ],
    ids=["proxy_refusal", "harness_refusal"],
)
def test_every_statement_site_carries_the_label_on_a_non_verified_run(execution_results, reason):
    """All five sites say the transform was not executed, in a boolean AND in text.

    Site by site: ``test_result`` (read by the response, the S3 ``test_output``,
    the approval payload and the state row), the test-results table item,
    ``output_data.summary``, the state row (carried, no new top-level attribute),
    and the handler ``message``.
    """
    response, state_row, results_item, test_output = _run_handler(execution_results)
    expected_note = f"Transform NOT executed — {_mod.NOT_VERIFIED_REASON_LABELS[reason]}."

    # Site 1 — `test_result`, and the three readers that get it for free. The
    # exact-set form mirrors `test_executed_check_counts.py:455`, which pins the
    # EXECUTED shape and names this shape as 9.2's subject: keeping both exact is
    # what makes "no key appears here that the spec did not put here" checkable,
    # and that is what carries the inertness claim (`B3` / `LGR-69ca78b-1`).
    for container in (
        response["test_result"],
        state_row["test_result"],
        test_output["test_result"],
        state_row["output_data"]["approval"]["test_result"],
    ):
        assert set(container) == {"status", "checks", "report_s3_uri", "execution_verified", "verification_note"}
        assert container["execution_verified"] is False
        assert container["verification_note"] == expected_note

    # Site 2 — the test-results table item, beside `validation_status`.
    assert results_item["execution_verified"] is False

    # Site 3 — inside `output_data.summary`, never as a sixth top-level key.
    summary = state_row["output_data"]["summary"]
    assert summary["execution_verified"] is False
    assert summary["verification_note"] == expected_note

    # Site 4 — the state row carries it via `test_result` and `output_data`, and
    # adds NO new top-level attribute (§4.7: the label is already on the row
    # twice, and a third copy would be a third thing to keep in agreement).
    assert "execution_verified" not in state_row
    assert "verification_note" not in state_row

    # Site 5 — the one-line summary can no longer read as a plain pass.
    assert response["message"] == f"{_original_message(response)} {expected_note}"


def test_every_statement_site_reports_verified_on_a_run_that_executed_despite_a_not_verified_check():
    """The false-claim case end to end: a ``verified: False`` entry, but it RAN.

    Same shape as the helper-level assertion, driven through the handler: a run
    with no target schema emits a ``schema_compliance`` entry carrying
    ``verified: False``, and every one of the five sites must still report the
    execution as verified and carry no note. A helper keyed on
    ``is_not_verified_check`` would label all five "never executed" here.
    """
    response, state_row, results_item, test_output = _run_handler(_executed_with_validation_output())

    checks = response["test_result"]["checks"]
    assert any(_mod.is_not_verified_check(check) for check in checks), (
        f"guard: this run must carry a verified: False entry for the test to mean anything — {checks}"
    )

    for container in (
        response["test_result"],
        state_row["test_result"],
        test_output["test_result"],
        state_row["output_data"]["approval"]["test_result"],
    ):
        assert container["execution_verified"] is True
        assert "verification_note" not in container

    assert results_item["execution_verified"] is True

    summary = state_row["output_data"]["summary"]
    assert summary["execution_verified"] is True
    assert "verification_note" not in summary

    # The original sentence, unamended.
    assert response["message"] == _original_message(response)
    assert "NOT executed" not in response["message"]


# ---------------------------------------------------------------------------
# The do-NOTs: `validation_status`, `output_data`'s five keys, and the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "execution_results",
    [_proxy_refusal(), _harness_refusal(), _executed_with_validation_output()],
    ids=["proxy_refusal", "harness_refusal", "executed"],
)
def test_validation_status_value_set_is_unchanged_at_all_three_emitting_sites(execution_results):
    """``validation_status`` still says ``"validated"`` on a non-verified run (N5).

    Its value set is deliberately NOT widened: two readers pass it through
    (``dashboard_api/handlers/pipelines_artifacts.py:102`` and
    ``dashboard-react/src/hooks/use-code.ts:49``, which falls back to it for
    ``test_status``), so a third value would be a new shape for readers this spec
    was not asked to touch. This test is the fence on that decision — if a later
    change widens it, this fails and the reader question gets asked again.
    """
    response, state_row, results_item, _ = _run_handler(execution_results)

    for site, value in (
        ("handler return", response["validation_status"]),
        ("state row", state_row["validation_status"]),
        ("test-results item", results_item["validation_status"]),
    ):
        assert value in {"validated", "failed"}, f"{site} carries an unexpected validation_status: {value!r}"

    # All three agree, and on these fixtures the run passes — so a non-verified
    # run reads "validated" while `execution_verified` says otherwise. That is
    # the knowingly-unimproved reader N5 records.
    assert response["test_status"] == "passed"
    assert response["validation_status"] == "validated"
    assert state_row["validation_status"] == "validated"
    assert results_item["validation_status"] == "validated"


def test_create_output_data_returns_exactly_five_keys_and_the_approval_key_is_the_envelope():
    """``output_data`` is fixed to five keys (``B8`` / ``LGR-60c4985-4``).

    Which object is which matters: ``create_output_data``'s **return** is the five
    validated keys, and the note rides inside ``summary`` rather than becoming a
    sixth. The handler then assigns ``"approval"`` onto the ENVELOPE
    (``lambda_function.py:2064``) — that sixth key is **pre-existing at HEAD** and
    is not attributable to R6.
    """
    produced = _mod.create_output_data(
        artifact_type="test_results",
        primary_id="te-1",
        status="success",
        summary={"execution_verified": False, "verification_note": "Transform NOT executed — x."},
        s3_uris={"full_report_json": "s3://b/k"},
    )
    assert set(produced) == {"artifact_type", "primary_id", "status", "summary", "s3_uris"}

    _, state_row, _, _ = _run_handler(_proxy_refusal())
    output_data = state_row["output_data"]
    assert set(output_data) == {"artifact_type", "primary_id", "status", "summary", "s3_uris", "approval"}
    assert set(output_data) - {"approval"} == set(produced), (
        "create_output_data's return is five keys; only the pre-existing envelope key differs"
    )
    # R6's additions are inside `summary`, not beside it.
    assert "execution_verified" not in output_data
    assert "verification_note" not in output_data


def test_nothing_added_by_the_statement_sites_changes_a_gate_answer():
    """Q2 decided "no": stating the non-verification must not route to a human.

    p32 2.21 stands unreversed, so a non-verified run and an executed run whose
    static outcomes match answer every gate field identically. Asserted as a pair
    rather than as four literals, so the claim is "the label changed nothing"
    rather than "these happen to be False today".
    """
    gate_fields = ("test_status", "approval_required", "quality_gate_requires_approval", "regeneration_needed")

    refused, refused_row, _, _ = _run_handler(_proxy_refusal())
    executed, executed_row, _, _ = _run_handler(_executed_with_validation_output())

    assert {field: refused[field] for field in gate_fields} == {field: executed[field] for field in gate_fields}
    # And the destination the step names is unchanged too.
    assert refused["next_step"] == executed["next_step"]
    # The Choice's field is untouched — it reads `.status` and nothing else
    # (LGR-69ca78b-1 / B3, `orchestration-stack.ts:853-854`).
    assert refused["test_result"]["status"] == executed["test_result"]["status"] == "passed"
    assert refused_row["status"] == executed_row["status"] == "success"
