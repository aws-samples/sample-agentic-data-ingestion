# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The handler tells the report which executor ran (p42 defect 2).

**This file exists because the pure builder was never the defect.**
``generate_comprehensive_test_report`` did exactly what it was asked; the seam was
that nobody passed it the executor, so it stated ``PANDAS_PROXY_CAVEAT``
unconditionally. A test that only called the builder — with or without the new
argument — would have passed on the broken code. The claim that has to be pinned
is *through the handler*: with ``VERIFICATION_EXECUTOR=glue`` deployed, the report
the handler STORES names the Spark run.

Observed on run ``run-2026-08-18-17-48-19-bd6f4deb`` (pipeline
``helloworld-f9a76ce9``, account 777788889999, ``us-west-2``). The stored
``output_data.summary.test_report_markdown`` on that row carries, in one report::

    > **Scope of this check:** the Code Interpreter runs a **pandas
    > approximation** of the transform over **generated sample data** …
    …
    transform execution failed: IndentationError: unexpected indent (<stdin>, line 2)

A pandas approximation does not raise ``IndentationError`` at ``<stdin>``. Both
halves of that report describe a Glue statement, and one of them said pandas.

Where the report is observable, and why the state row is the site asserted
-------------------------------------------------------------------------

The report is ``review_result``, and the handler stores it in three places: the
state row's ``output_data.summary.test_report_markdown`` (truncated to 5000
chars, ``lambda_function.py:3037``), the S3 ``test_results.json`` as
``review_result``, and the S3 ``test_report.md``. The state row is what the
dashboard reads, so it is the site asserted here; the S3 object is asserted too,
because ``use-code.ts`` reads ``review_result`` out of it and prefers it over
every DynamoDB value.

Ledger
------

* ``PANDAS_PROXY_CAVEAT``'s own revisit condition is the authority for the
  branch — "Do NOT delete this caveat without landing V5", V5 being a Glue
  interactive session. V5 is the ``real-spark-verification`` spec. The caveat is
  not deleted: ``test_the_pandas_path_still_states_the_pandas_caveat`` is what
  keeps it standing where it applies.
* ``LGR-ad78946-1`` (**template / ratified**, ``NOTE: BROAD``) — three named
  executor values by exact string comparison. This file takes the resolution as
  given and asserts what the report does with the result.
* ``LGR-ad78946-5`` (**template / ratified**) — a ``glue`` request with no role
  ARN resolves to ``pandas`` at ERROR. That is why the report reads the
  handler's RESOLVED value and not the environment:
  ``test_a_glue_request_with_no_role_arn_reports_pandas`` is that case, and it is
  the one an environment read inside the builder would get wrong.
* ``LGR-60c4985-4`` (**template / ratified**) — ``output_data`` is fixed to five
  keys, which is why the report rides inside ``summary`` rather than as a new
  top-level attribute. Nothing here adds a key.
* ``LGR-60c4985-6`` (**template / ratified**) — the practice the four caveat
  assertions below follow under p44: record the reversal at the assertion so a
  later reader finds the reason with the requirement. ``LGR-5108dd4-2`` applied
  the same rule to eleven assertions in this file and in
  ``test_comprehensive_report.py`` for the glue-first reversal; p44 task 12.3
  applies it to four here, and to six there, with **no assertion amended**.

p44 (R5.3, R9.6) — what these four caveat assertions pin now
------------------------------------------------------------

``GLUE_SPARK_CAVEAT`` is no longer "the Glue report's wording". It is the
**synthetic-frame** arm of ``_GLUE_FRAME_CAVEATS``
(``lambda_function.py:2240``), sitting beside ``GLUE_SPARK_SAMPLE_CAVEAT`` and
``GLUE_SPARK_SAMPLE_UNAVAILABLE_CAVEAT``. Its bytes are unchanged and it is what
a ``frame_provenance`` of ``None`` selects, so all four assertions below hold
untouched — that is p44's own evidence that rewriting the constant instead would
have forced ten assertion edits to express identical behaviour, each one a chance
to encode a guess as a pin. Nothing here narrowed: before p44 wired the
Frame_Mode caller, synthetic was the only reachable frame, so the
synthetic-frame report is what these tests always described. What changed is that
the frame is now a dimension a reader has to name.

**Why every run in this file lands on the synthetic arm, which is not obvious.**
``_run_handler``'s ``runner`` is a ``MagicMock``, so the handler's
``getattr(runner, "last_frame_source", None)`` (``lambda_function.py:3361``)
returns an auto-attribute mock rather than ``None``, and ``.provenance()`` yields
another mock. The builder's ``_GLUE_FRAME_CAVEATS.get(frame, GLUE_SPARK_CAVEAT)``
(``:2514``) finds no such key and falls to the synthetic arm. Measured rather than
reasoned about: ``frame_provenance=None``, an explicit ``{"frame": "synthetic",
…}`` and a mock provenance all render **byte-identical** reports. A handler-level
test that wants a non-synthetic frame must therefore set ``last_frame_source``
deliberately; the frame dimension's own property drives the builder directly with
an explicit provenance instead.

Sibling coverage — not duplicated here
--------------------------------------

* ``test_comprehensive_report.py`` — the builder's four claim sites, the verdict
  invariance across executors, ``shadow``, and the unrecognised value. This file
  asserts only that the handler reaches them with the right value.
* ``test_verification_executor_selection.py`` — how the value is resolved, and
  the runner dispatch.

Validates: p42 Requirements 2.1, 2.2, 2.3
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

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
    sys.modules["lambda_function"] = _mod
    yield


ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
}

_PASS_VERDICT = {
    "verdict": "pass",
    "issues": [],
    "categories": {
        "Correctness": "pass",
        "Schema Compliance": "pass",
        "Error Handling": "pass",
        "AWS Best Practices": "pass",
        "Security": "pass",
        "Performance": "pass",
    },
}

#: A clean successful execution — the PRODUCTION READY branch, which is where
#: three of the four wrong claims live.
_EXEC_OK = {
    "execution_successful": True,
    "executed": True,
    "exit_code": 0,
    "stderr": "",
    "error_details": None,
    "input_records": 100,
    "output_records": 100,
    "conversion_report": None,
    "data_quality_results": {
        "overall_quality_score": 98,
        "completeness_score": 100,
        "uniqueness_score": 100,
        "schema_compliance": 100,
        "duplicates": 0,
        "missing_columns": [],
    },
}


def _fake_dynamodb() -> MagicMock:
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-1"}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def _state_rows(runs_table) -> list[dict]:
    """Reconstruct the state rows the handler wrote (a SET update, not a put).

    Copied from ``test_execution_verification_statement.py:_state_rows`` — the
    handler writes through ``build_state_row_update``, so the row has to be
    rebuilt from the expression values rather than read off an ``Item``.
    """
    rows = []
    for call in runs_table.update_item.call_args_list:
        values = {name.lstrip(":"): value for name, value in call.kwargs["ExpressionAttributeValues"].items()}
        if "step_sequence" not in values:
            continue
        rows.append({**call.kwargs["Key"], **values})
    return rows


def _run_handler(*, env: dict[str, str]) -> tuple[str, str]:
    """Drive ``lambda_handler`` and return the report as STORED, twice.

    ``(state_row_report, s3_report)`` — the state row's
    ``output_data.summary.test_report_markdown`` and ``test_results.json``'s
    ``review_result``. Both are returned because they are two different readers'
    copies of the same claim and the frontend prefers the S3 one.

    Both runners are patched to the same result, so the ONLY difference between a
    ``pandas`` invocation and a ``glue`` one here is the executor the handler
    resolves — which makes any difference in the report attributable to the
    resolution and to nothing else.
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
    runner.run_test.return_value = _EXEC_OK

    # The `glue` branch imports `GlueSessionTestRunner` function-locally, so the
    # patch has to be on the module it imports FROM. A stub module is registered
    # rather than the real one imported: no Glue client is constructed and no
    # session is created anywhere in this file.
    glue_module = MagicMock()
    glue_module.GlueSessionTestRunner = MagicMock(return_value=runner)

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", results_table),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("sys.modules", {"glue_session_runner": glue_module}),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false", **env}),
    ):
        _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1"}, None)

    rows = [row for row in _state_rows(runs_table) if row.get("step_name") == "test_code"]
    assert len(rows) == 1, "expected exactly one test_code state row"
    state_report = rows[0]["output_data"]["summary"]["test_report_markdown"]

    outputs = [
        json.loads(call.kwargs["Body"].decode("utf-8"))
        for call in s3.put_object.call_args_list
        if call.kwargs["Key"].endswith("test_results.json")
    ]
    assert len(outputs) == 1, "expected exactly one test_results.json object"

    return state_report, outputs[0]["review_result"]


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_a_glue_run_reports_the_spark_caveat_not_the_pandas_one():
    """The defect, at the seam it lived at.

    **Validates: Requirements 2.1, 2.2**

    Both stored copies, and both directions: the Spark sentence is present AND
    the pandas sentence is gone. Before this fix the second assertion is the one
    that fails, on both copies, which is exactly what the live row shows.

    **p44 R5.3 — assertions unchanged; this pins the SYNTHETIC-FRAME Glue
    wording.** This is the one site in this file where the frame dimension is
    actually consulted: ``ran_on_spark`` is true, so the builder reads the mock
    provenance and falls to ``GLUE_SPARK_CAVEAT`` by the mechanism the module
    docstring's p44 section measures. Read it as "a Glue run over a synthetic
    frame names Spark and does not name pandas", not as "every Glue run carries
    this string" — a sample run carries ``GLUE_SPARK_SAMPLE_CAVEAT`` and would
    fail the first assertion below, correctly.
    """
    for report in _run_handler(env={"VERIFICATION_EXECUTOR": "glue", "VERIFICATION_SESSION_ROLE_ARN": ROLE_ARN}):
        assert _mod.GLUE_SPARK_CAVEAT in report
        assert _mod.PANDAS_PROXY_CAVEAT not in report
        assert "Real Spark Execution Results (Glue interactive session)" in report
        assert "REAL SPARK EXECUTION" in report
        assert "pandas" not in report


def test_the_pandas_path_still_states_the_pandas_caveat():
    """The fence, still standing where it applies (p32 2.22).

    **Validates: Requirement 2.3**

    Asserted through the handler rather than only on the builder, so a future
    edit that made the Glue wording the default would fail here.

    **Assertions unchanged; the ROUTE to them changed.** ``env={}`` used to reach
    ``pandas`` through R2.3's fall-through. After the glue-first reversal the
    fall-through is ``glue``, so this configuration now resolves ``glue`` first
    and arrives at ``pandas`` through :func:`resolve_effective_executor`'s
    no-role-ARN downgrade instead (``LGR-ad78946-5``) — same observable report,
    different mechanism, and one ERROR line in the log where there used to be
    none. Stated because a reader would otherwise take this passing test as
    evidence that the fall-through is still ``pandas``. To exercise a genuine
    ``pandas`` request, pass ``VERIFICATION_EXECUTOR: "pandas"`` explicitly, which
    is what ``test_comprehensive_report.py`` does at the builder level.

    **p44 R5.3 / R9.6 — assertions unchanged, and for a structural reason.** The
    frame dimension is composed AFTER ``_EXECUTOR_CAVEATS`` and applies only on
    the ``glue`` arm (``lambda_function.py:2497-2516``); ``_EXECUTOR_CAVEATS`` was
    deliberately not re-keyed on ``(executor, frame)``. So a pandas report cannot
    carry frame wording of any kind — not the synthetic arm asserted absent below,
    and not either new sibling. A mock ``frame_provenance`` still reaches the
    builder on this path and is never consulted, because ``ran_on_spark`` is
    false. R9.6 is the criterion that keeps ``PANDAS_PROXY_CAVEAT`` reaching every
    pandas run unchanged, which is the first assertion below.
    """
    for report in _run_handler(env={}):
        assert _mod.PANDAS_PROXY_CAVEAT in report
        assert _mod.GLUE_SPARK_CAVEAT not in report
        assert "Code Interpreter Execution Results" in report


def test_a_glue_request_with_no_role_arn_reports_pandas():
    """``LGR-ad78946-5``: that configuration RESOLVES to pandas, so it reports pandas.

    **Validates: Requirement 2.3**

    This is the case that makes "pass the resolved value" load-bearing rather
    than stylistic. A report builder reading ``VERIFICATION_EXECUTOR`` from the
    environment would see ``glue`` and claim a Spark run, while the handler had
    already fallen back to the pandas proxy and run it — a false claim
    manufactured by the read itself.

    **p44 R5.3 / R9.6 — assertions unchanged, and the argument now covers two
    dimensions.** p44 gives the report a second passed-in fact, the
    Frame_Provenance, and it is passed for exactly this reason: a builder that read
    ``VERIFICATION_FRAME_MODE`` from the environment would claim a real-source
    sample on a run that fell back to a synthetic frame, the same false claim in
    the frame dimension that this test pins in the executor dimension. The
    assertion below is untouched because this configuration resolves ``pandas``,
    and a pandas report reaches no frame arm at all — see
    ``test_the_pandas_path_still_states_the_pandas_caveat``.
    """
    for report in _run_handler(env={"VERIFICATION_EXECUTOR": "glue", "VERIFICATION_SESSION_ROLE_ARN": ""}):
        assert _mod.PANDAS_PROXY_CAVEAT in report
        assert _mod.GLUE_SPARK_CAVEAT not in report


def test_shadow_reports_the_pandas_caveat_through_the_handler():
    """Under ``shadow`` the authoritative result is the pandas one (R4.1).

    The Glue half is non-authoritative and its result reaches only the
    ``shadow_verification`` row (``LGR-ad78946-3``), so the report — which
    describes ``execution_results`` — must name pandas.

    **p44 R5.3 / R9.6 — assertions unchanged, and this is one of
    ``LGR-5c30733-3``'s three surviving claims.** p44 supersedes that record —
    its stated basis was "``frame_mode`` has no caller" — but "``shadow`` maps to
    the pandas caveat" is carried forward untouched, and this test is where it is
    pinned through the handler. It is also why the frame dimension is composed
    onto the ``glue`` arm rather than re-keying ``_EXECUTOR_CAVEATS`` on
    ``(executor, frame)``: under ``shadow`` the authoritative result is the pandas
    one, which has no frame source at all, so those cells cannot exist. The Glue
    half's own frame is pinned synthetic separately (R1.6,
    ``_default_shadow_glue_runner``) and never reaches this report.
    """
    for report in _run_handler(env={"VERIFICATION_EXECUTOR": "shadow", "VERIFICATION_SESSION_ROLE_ARN": ROLE_ARN}):
        assert _mod.PANDAS_PROXY_CAVEAT in report
        assert _mod.GLUE_SPARK_CAVEAT not in report


def test_the_executor_does_not_change_the_stored_verdict():
    """The executor changes the report's claims about its evidence, not the gate.

    ``LGR-334667e-1``'s matrix does not take the executor as an input, and
    ``LGR-69ca78b-1`` (**invariant**) is why an accidental change here would
    matter: the governance Choice states read ``test_status``, so a verdict that
    moved with the flag would move the gate with it. Both runners return the
    same result in this harness, so the verdict must be identical.
    """
    glue_state, _ = _run_handler(env={"VERIFICATION_EXECUTOR": "glue", "VERIFICATION_SESSION_ROLE_ARN": ROLE_ARN})
    # `env={}` still lands on pandas, but via the no-role-ARN downgrade now that
    # the fall-through is `glue` — see
    # test_the_pandas_path_still_states_the_pandas_caveat.
    pandas_state, _ = _run_handler(env={})

    def _verdict_lines(report: str) -> list[str]:
        return [line for line in report.splitlines() if "PRODUCTION READY" in line or "**Next Steps:**" in line]

    assert _verdict_lines(glue_state) == _verdict_lines(pandas_state)
