# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 7 — a code_generation approval payload carries the structured test result.

# Feature: p34-code-approval-gate-honors-config, Property 7: A code_generation approval payload carries the structured test result

For any ``test_code`` state row carrying a ``test_result`` object, and for any
inline ``test_result`` on the approval-request event,
``get_approval_data("code_generation", ...)`` returns a payload whose
``test_result.checks`` is the producer's checks list, with the inline event
taking precedence over the state row.

The ``??``-chain precedence mirrors the ``test_failure`` branch
(``approval_handler/lambda_function.py:283-291``) — p32 2.2's requirement that a
payload carrying the older key keeps working. ``WaitForCodeApproval`` now passes
``test_result`` inline (``orchestration-stack.ts:639``, p34 task 6.1), so the
state-row read is the fallback rather than the primary source.

Expectations derive from requirement 3.6 and the producers' real attribute sets
(``test_code/lambda_function.py:1579-1583`` for the ``test_result`` shape,
``:1722`` for the state-row attribute; ``create_code/lambda_function.py:839`` for
``output_data.approval``) — NOT from current output, which was seven ``null``s.

**Validates: Requirements 3.6**
"""

import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("STATE_TABLE", "test-state")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

# ---------------------------------------------------------------------------
# Mock setup — patch boto3 before importing the handler module
# ---------------------------------------------------------------------------
mock_task_tokens_table = MagicMock()
mock_approvals_table = MagicMock()
mock_state_table = MagicMock()

_table_map = {
    "test-task-tokens": mock_task_tokens_table,
    "test-approvals": mock_approvals_table,
    "test-state": mock_state_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client"):
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    import importlib.util

    sys.modules.pop("lambda_function", None)
    _spec = importlib.util.spec_from_file_location(
        "approval_handler_code_generation_payload_lf",
        os.path.join(LAMBDA_DIR, "lambda_function.py"),
    )
    handler = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(handler)

from hypothesis import given, settings
from hypothesis import strategies as st

# Bound at module scope deliberately: Hypothesis runs many examples inside one
# test function, so per-example state lives in _install_rows rather than in a
# function-scoped fixture.
handler.state_table = mock_state_table

RUN_ID = "run-2026-01-01-00-00-00-ut000007"


# ---------------------------------------------------------------------------
# Generators — constrained to the producers' real shapes
# ---------------------------------------------------------------------------
_CHECK_NAMES = st.sampled_from(
    [
        "syntax_check",
        "code_interpreter_execution",
        "transform_execution",
        "output_validation",
        "schema_compliance",
        "not_verified_by_harness",
    ]
)


def _checks(min_size: int = 0):
    """The producer's checks list — test_code/lambda_function.py builds entries
    carrying name/passed/detail, and schema_compliance additionally carries
    verified/not_verified_reason (:515-538)."""
    entry = st.fixed_dictionaries(
        {
            "name": _CHECK_NAMES,
            "passed": st.booleans(),
            "detail": st.text(max_size=40),
        },
        optional={
            "verified": st.booleans(),
            "not_verified_reason": st.sampled_from(["no_target_schema", "not_yet_expressible"]),
        },
    )
    return st.lists(entry, min_size=min_size, max_size=5)


def _test_result(min_checks: int = 0):
    """test_code/lambda_function.py:1579-1583 — {status, checks, report_s3_uri}."""
    return st.fixed_dictionaries(
        {
            "status": st.sampled_from(["passed", "failed"]),
            "checks": _checks(min_checks),
            "report_s3_uri": st.just("s3://test-outputs/run/test_report.md"),
        }
    )


def _create_code_row():
    """create_code/lambda_function.py:839 — the payload lives at
    output_data.approval, never at the row's top level."""
    return st.builds(
        lambda code_version_id, s3_uri: {
            "run_id": RUN_ID,
            "step_name": "create_code",
            "output_data": {
                "code_version_id": code_version_id,
                "approval": {
                    "approval_type": "code_generation",
                    "code_version_id": code_version_id,
                    "generated_code_s3_uri": s3_uri,
                    "generated_code_preview": "import sys\n",
                    "code_quality_metrics": {"lines_of_code": 42},
                    "dqdl_rules": "",
                    "strands_agent_reasoning": "selected template",
                    "template_selection": {"template_used": "glue_job_s3_tables"},
                },
            },
        },
        code_version_id=st.text(min_size=1, max_size=12).filter(lambda s: s.strip() != ""),
        s3_uri=st.just("s3://generated-code/run/job.py"),
    )


def _install_rows(code_row: dict, test_row_result: dict) -> list[dict]:
    """Route get_item by step_name so the assertion does not depend on call order.

    Returns the list the handler's reads are recorded into.
    """
    reads: list[dict] = []

    def _get_item(Key):  # boto3's own keyword name, kept so the call sites match
        reads.append(dict(Key))
        step_name = str(Key["step_name"])
        if step_name == "create_code":
            return {"Item": code_row}
        if step_name == "test_code":
            return {"Item": {"run_id": RUN_ID, "step_name": "test_code", "test_result": test_row_result}}
        return {}

    mock_state_table.reset_mock(return_value=True, side_effect=True)
    mock_state_table.get_item.side_effect = _get_item
    return reads


@settings(max_examples=200, deadline=None)
@given(
    code_row=_create_code_row(),
    row_result=_test_result(min_checks=1),
    inline_result=_test_result(min_checks=1),
    inline_present=st.booleans(),
)
def test_code_generation_payload_carries_the_producers_checks_list(code_row, row_result, inline_result, inline_present):
    """The payload's test_result.checks is the producer's list; inline wins."""
    reads = _install_rows(code_row, row_result)

    event = {"approval_type": "code_generation", "run_id": RUN_ID}
    if inline_present:
        event["test_result"] = inline_result

    data = handler.get_approval_data("code_generation", RUN_ID, event)

    expected = inline_result if inline_present else row_result
    assert data["test_result"] == expected
    assert data["test_result"]["checks"] == expected["checks"]
    assert data["test_status"] == expected["status"]
    assert data["report_s3_uri"] == expected["report_s3_uri"]

    # Precedence, observed rather than asserted about: an inline payload means
    # the test_code row is never read.
    read_steps = [str(key["step_name"]) for key in reads]
    if inline_present:
        assert read_steps == ["create_code"]
    else:
        assert read_steps == ["create_code", "test_code"]


@settings(max_examples=200, deadline=None)
@given(code_row=_create_code_row(), row_result=_test_result(min_checks=1))
def test_inline_non_dict_falls_back_to_the_state_row(code_row, row_result):
    """A non-dict inline value is not a test result — the ??-chain continues.

    Mirrors the test_failure branch's ``isinstance`` guard, so a malformed
    inline payload degrades to the producer's row rather than to null.
    """
    reads = _install_rows(code_row, row_result)

    for inline in (None, "", 0, [], "failed"):
        reads.clear()
        data = handler.get_approval_data(
            "code_generation", RUN_ID, {"approval_type": "code_generation", "test_result": inline}
        )
        assert data["test_result"] == row_result
        assert data["test_result"]["checks"] == row_result["checks"]
        assert [str(key["step_name"]) for key in reads] == ["create_code", "test_code"]
