# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 3 — the truncated test frame (p33 Bug Condition 2).

For any source schema declaring more than 20 fields, the fixed code SHALL
either build a test frame carrying one column per declared field, or — when the
field count exceeds the frame-width ceiling — SHALL report the Code Interpreter
check as **not verified** with a harness reason naming the field count and the
ceiling, and SHALL emit no ``schema_compliance`` score. In no case SHALL a
truncated frame produce a **failed** ``code_interpreter_execution`` or a
**failed** ``schema_compliance``.

Bug condition (design §Second bug condition)::

    isFrameTruncationBugCondition(input) =
        count(sourceSchemaFields(input)) > 20
        AND transformReferences(input, someField NOT IN first20(fields))
        AND reportedCheck(input) is a FAILURE   // not a non-verification

**RED ON UNFIXED CODE — the failure is task 1's deliverable.**
``_build_test_data_code`` slices ``list(fields.items())[:20]``
(``code_interpreter_runner.py:322``), so a 25-field schema yields 20 columns
with nothing recording the loss. There is no ceiling constant, no pre-session
refusal, and no ``not_verified_by_harness`` shape for cases B and C to find.

Why this defect is only observable at the unit level today
---------------------------------------------------------
The handler's source-schema read returns ``{}``
(``test_code/lambda_function.py:976-985``), so ``fields`` is always the
two-element ``{id, value}`` fallback at ``:318-319`` and the cap never binds.
Driving it through the handler is impossible on unfixed code; that is part of
why the cap was never diagnosed. Task 4.3 is what makes it reachable, which is
why task 4.10 must land in the same change (design R3).

Why case A is an exploration case and requirement 3.14 is not
------------------------------------------------------------
Case A **fails** on unfixed code, which is the entry criterion for an
exploration case. Its narrow-schema counterpart — byte-identical frame code for
a schema of 20 or fewer fields (requirement 3.14) — **passes** on unfixed code
and therefore belongs to the preservation test (task 2). The two halves of D12
split along that line.

Ledger note: ``why`` on ``code_interpreter_runner.py`` and on ``:322``
specifically returns no record mentioning a field limit at all — five commits,
two with no records, three broad snapshots (``LGR-feb6a1d-1`` at :322,
``LGR-d2e9878-1`` the commit that introduced the builder, ``LGR-0176ccc-1``).
The 20 is an unjustified magic number, so changing it overrides no decision.

Validates: Requirements 1.8, 2.10, 2.11
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

import code_interpreter_runner as _runner_mod
from code_interpreter_runner import CodeInterpreterTestRunner

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


#: The current, unexplained cap. Named so the assertions read against the
#: defect rather than a bare literal.
_CURRENT_SLICE = 20

#: The three type branches at code_interpreter_runner.py:324-329.
_TYPES = ["integer", "bigint", "float", "double", "decimal", "number", "string", "timestamp"]


def _schema(field_names, *, shape: str = "properties", types=None) -> dict:
    types = types or ["string"] * len(field_names)
    fields = {name: {"type": t} for name, t in zip(field_names, types, strict=True)}
    return {shape: fields}


def _names(count: int, prefix: str = "col") -> list[str]:
    return [f"{prefix}_{i:04d}" for i in range(count)]


def _frame_column_names(frame_code: str) -> list[str]:
    """Column names the generated frame code declares, in declaration order."""
    names = []
    for line in frame_code.splitlines():
        stripped = line.strip()
        if stripped.startswith('"') and '": [' in stripped:
            names.append(stripped.split('"')[1])
    return names


def _make_client() -> MagicMock:
    client = MagicMock()
    client.start_code_interpreter_session.return_value = {"sessionId": "sess-1"}
    client.stop_code_interpreter_session.return_value = {}
    client.invoke_code_interpreter.return_value = {
        "stream": [
            {
                "result": {
                    "content": [{"type": "text", "text": "ok"}],
                    "structuredContent": {"stdout": "ok", "stderr": "", "exitCode": 0},
                }
            }
        ]
    }
    return client


def _runner_with(client) -> CodeInterpreterTestRunner:
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = client
        runner = CodeInterpreterTestRunner(region="us-east-1")
    assert runner.client is client, "guard: a None client would take the _empty_results path instead"
    return runner


#: Convertible on purpose. An unconvertible block would refuse BEFORE any
#: session too (`code_interpreter_runner.py:152-156`), which would make case
#: B's "session never started" assertion pass for the wrong reason.
_CONVERTIBLE_TRANSFORM = 'df_transformed = df.withColumnRenamed("col_0000", "out_0")'

_ALL_STATIC_PASS = {
    "verdict": "pass",
    "issues": [],
    "test_categories": {
        "Correctness": {"status": "PASSED", "passed": True},
        "Security": {"status": "PASSED", "passed": True},
    },
    "recommendation": "APPROVE",
}


def _frame_ceiling() -> int | None:
    """The frame-width ceiling D12 introduces, or None while it does not exist.

    Returned rather than asserted so the tests below can OBSERVE the unfixed
    behaviour at an over-width schema first. "The constant is missing" is a
    weaker report than "a 21-field schema started a session and executed the
    transform against a truncated frame".
    """
    ceiling = getattr(_runner_mod, "_MAX_TEST_FRAME_COLUMNS", None)
    return int(ceiling) if ceiling is not None else None


def _over_width(ceiling: int | None) -> int:
    """A field count that must not be silently truncated.

    With no ceiling in place, one field past the current ``[:20]`` slice is
    already over-width — it is the smallest input the defect binds on.
    """
    return (ceiling + 1) if ceiling is not None else (_CURRENT_SLICE + 1)


# ---------------------------------------------------------------------------
# Case A — every declared field becomes a column
# ---------------------------------------------------------------------------


def test_twenty_five_field_schema_declares_all_twenty_five_columns():
    """The task's named case: 25 fields in, 25 columns out.

    Validates: Requirements 1.8, 2.10
    """
    client = _make_client()
    runner = _runner_with(client)
    declared = _names(25)

    code = runner._build_test_data_code(_schema(declared))
    emitted = _frame_column_names(code)

    assert emitted == declared, (
        f"frame code declares {len(emitted)} columns for a 25-field source schema; "
        f"dropped {sorted(set(declared) - set(emitted))}. "
        f"code_interpreter_runner.py:322 slices list(fields.items())[:{_CURRENT_SLICE}]"
    )


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    width=st.integers(min_value=_CURRENT_SLICE + 1, max_value=64),
    shape=st.sampled_from(["properties", "fields"]),
    seed=st.integers(min_value=0, max_value=2**16),
)
def test_every_declared_field_becomes_a_column_above_the_old_cap(width, shape, seed):
    """Property 3, first limb: no field the schema declares is dropped.

    Quantified over both field shapes the builder handles
    (``properties`` / ``fields``) and all three type branches at
    ``code_interpreter_runner.py:324-329``.

    Validates: Requirements 1.8, 2.10
    """
    client = _make_client()
    runner = _runner_with(client)
    declared = _names(width)
    types = [_TYPES[(seed + i) % len(_TYPES)] for i in range(width)]

    code = runner._build_test_data_code(_schema(declared, shape=shape, types=types))
    emitted = _frame_column_names(code)

    assert len(emitted) == width, (
        f"{width}-field {shape} schema produced {len(emitted)} columns — "
        f"{width - len(emitted)} field(s) silently dropped, with nothing in the "
        "generated code or the result recording the loss"
    )
    assert emitted == declared, "column order or membership diverged from the declared fields"


# ---------------------------------------------------------------------------
# Case B — above the ceiling, refuse before spending a session
# ---------------------------------------------------------------------------


def test_over_ceiling_schema_refuses_before_starting_a_session():
    """Requirement 2.11 — a frame that cannot carry every field is not executed.

    The refusal is reported as a harness non-verification naming the field
    count and the ceiling, NOT as a failed ``code_interpreter_execution``
    (C14: it must not reuse ``not_verified_by_proxy`` either — that shape names
    a construct and a line number a width refusal does not have).

    Validates: Requirement 2.11
    """
    ceiling = _frame_ceiling()
    width = _over_width(ceiling)
    client = _make_client()
    runner = _runner_with(client)

    results = runner.run_test(_CONVERTIBLE_TRANSFORM, _schema(_names(width)), _schema(["out_0"]))

    # Refused before the session — an unbuildable frame costs no session, the
    # same posture as the conversion refusal at :152-156.
    assert client.start_code_interpreter_session.call_count == 0, (
        f"a {width}-field source schema (frame-width ceiling: {ceiling}) started a Code "
        "Interpreter session and executed the transform against a frame truncated to "
        f"{_CURRENT_SLICE} columns by code_interpreter_runner.py:322"
    )
    assert client.invoke_code_interpreter.call_count == 0, "code was executed against an incomplete frame"

    assert ceiling is not None, (
        "code_interpreter_runner has no _MAX_TEST_FRAME_COLUMNS ceiling to name in the "
        "non-verification detail (requirement 2.11 requires the field count AND the ceiling)"
    )

    assert results.get("not_verified_by_harness") is True, (
        f"result carries no not_verified_by_harness marker: {sorted(results)}"
    )
    assert results.get("not_verified_reason") == "test_frame_truncated", (
        f"expected reason 'test_frame_truncated', got {results.get('not_verified_reason')!r}"
    )
    assert results.get("executed") is False
    assert results.get("execution_successful") is False
    # C14 — a width refusal is not a proxy refusal.
    assert not results.get("not_verified_by_proxy"), (
        "a frame-width refusal reused not_verified_by_proxy, which would misattribute the cause to the converter (C14)"
    )

    test_status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, results)
    names = [c["name"] for c in checks]

    harness = [c for c in checks if c["name"] == "not_verified_by_harness"]
    assert len(harness) == 1, f"expected one not_verified_by_harness check, got {names}"
    assert harness[0].get("verified") is False, f"harness non-verification carries no verified:False: {harness[0]}"
    assert harness[0].get("not_verified_reason") == "test_frame_truncated"

    # Stated as text — the field count and the ceiling, not colour alone (C5).
    detail = str(harness[0].get("detail", ""))
    assert str(width) in detail, f"detail does not name the field count {width}: {detail!r}"
    assert str(ceiling) in detail, f"detail does not name the ceiling {ceiling}: {detail!r}"

    # It replaces code_interpreter_execution rather than joining it, and it is
    # not evidence of failure (C4 partitioning).
    assert "code_interpreter_execution" not in names, (
        "a harness refusal was recorded as a code_interpreter_execution check — a "
        f"transform that never ran reported as an execution outcome: {names}"
    )
    assert test_status != "failed", (
        f"test_status is {test_status!r}; a frame the harness could not build must not "
        "fail a transform that was never executed"
    )


# ---------------------------------------------------------------------------
# Case C — the C13 fence, asserted as a contrast pair so it is not vacuous
# ---------------------------------------------------------------------------


def test_incomplete_frame_emits_no_schema_compliance_but_a_complete_one_does():
    """C13 — a shortfall the harness caused can never present as a failure.

    Deliberately a CONTRAST PAIR. "No ``schema_compliance`` entry for an
    incomplete frame" is true today only because no ``schema_compliance`` entry
    is ever emitted for anything, so asserting it alone would pass vacuously on
    unfixed code and prove nothing. Pairing it with the complete-frame case —
    which MUST emit the entry — makes the fence meaningful in both directions
    and red today for the defect: the complete-frame half finds no entry.

    Validates: Requirements 2.11, 3.13
    """
    width = _over_width(_frame_ceiling())

    # ── incomplete frame: over the width the harness can carry ──
    over_client = _make_client()
    over_results = _runner_with(over_client).run_test(
        _CONVERTIBLE_TRANSFORM, _schema(_names(width)), _schema(["out_0"])
    )
    _, over_checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, over_results)
    over_names = [c["name"] for c in over_checks]
    assert "schema_compliance" not in over_names, (
        "an incomplete test frame was scored for schema compliance — the shortfall "
        f"would be the harness's fault, reported as the transform's (C13): {over_names}"
    )

    # ── complete frame: a run that DID produce validation output ──
    # The shape _parse_results builds for a successful validation step.
    complete_results = {
        "execution_successful": True,
        "executed": True,
        "exit_code": 0,
        "stderr": "",
        "input_records": 100,
        "output_records": 100,
        "data_quality_results": {
            "completeness_score": 95.0,
            "uniqueness_score": 100.0,
            "schema_compliance": 100.0,
            "overall_quality_score": 98.3,
            "null_counts": {"out_0": 5},
            "duplicates": 0,
            "missing_columns": [],
        },
        "error_details": None,
        "conversion_report": None,
    }
    _, complete_checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, complete_results)
    complete_names = [c["name"] for c in complete_checks]
    assert "schema_compliance" in complete_names, (
        "a run over a complete frame emitted no schema_compliance entry, so the C13 "
        "fence is vacuous — there is no scoring for it to suppress. Emitted: "
        f"{complete_names}"
    )
    scored = next(c for c in complete_checks if c["name"] == "schema_compliance")
    assert scored["passed"] is True, f"a 100.0 score with no missing columns must pass: {scored}"
    assert "100" in json.dumps(scored), f"the score is not stated as text in the entry: {scored}"
