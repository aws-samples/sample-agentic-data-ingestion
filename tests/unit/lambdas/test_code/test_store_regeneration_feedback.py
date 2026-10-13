# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: ``store_regeneration_feedback``'s write (p32 T13).

This is the write half of the regeneration feedback loop
(``test_code/lambda_function.py:510``). design.md §5 (V3) records it as one of
only two genuinely uncovered symbols out of codegraph's seven flags: the sole
reference in the suite today is a comment plus an existence check —

    # store_regeneration_feedback writes via update_item on the runs table
    update_calls = runs_table.update_item.call_args_list
    assert update_calls, "expected regeneration feedback to be stored"
    (tests/unit/lambdas/test_code/test_review_gate_honesty.py:330-332)

— which passes for ANY ``update_item`` on that table, including one that wrote
the wrong attribute under the wrong key. Nothing asserts the write itself. This
file does.

**Where expectations come from.** `LGR-28b4d75-5` (template) forbids
transcribing expectations from the producer's output: "a test written from the
producer's output cannot detect the producer drifting, because the output is
its own oracle." The remedy that record applies is to derive the expectation
from the CONSUMER at test time, and that is what happens here — the key and the
attribute name are not transcribed from ``store_regeneration_feedback``'s
source, they are taken from the reader that has to find the data,
``create_code/mapping_resolution.get_regeneration_feedback``, and the central
test drives an actual write-then-read round-trip through a real DynamoDB
engine. If either side moves, the round-trip breaks.

The governing clause is bugfix.md 2.16:

    2.16 WHEN create_code runs with regeneration_attempt > 0 THEN it SHALL read
    the prior iteration's reviewer feedback and the resulting prompt SHALL
    differ from the original — a regeneration attempt SHALL NOT rebuild a
    byte-identical prompt.

A write that lands where the reader does not look satisfies no part of that,
and fails invisibly: ``get_regeneration_feedback`` returns {},
``_build_feedback_section`` returns "" (transform_generation.py:44-45), and the
regenerated prompt is byte-identical to the one that already failed.

**Where current behaviour differs from what 2.16/2.17 require.** One place, and
it is pinned rather than fixed here (T13 changes no production code):
``store_regeneration_feedback`` swallows every exception into a log line
(``:524-525``). Under 2.16 a lost write is not a cosmetic failure — it is the
byte-identical retry the clause forbids, arriving with no signal, and then
consuming one of the two attempts 2.17 bounds. ``test_write_failure_is_swallowed_today``
records that as CURRENT behaviour, not as desired behaviour, so that if task 16
makes the write loud the reversal is deliberate and visible in the diff rather
than silent (`LGR-5906ec6-2`: in this repo a hold is encoded as an assertion,
and enabling the held behaviour means changing a test on purpose).

**Validates: Requirements 2.16, 2.17; V3**
"""

import importlib.util
import os
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

# Pre-import the condition classes before the patch.dict(sys.modules) block
# below (see test_mapping_freshness.py:38-45): a re-imported
# boto3.dynamodb.conditions carries Key/Attr classes moto's isinstance checks
# do not recognize.
import boto3.dynamodb.conditions  # noqa: F401  (import order is the point)

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, _SHARED_DIR)

# Stub heavy / AWS-touching imports so the module loads in isolation
# (identical set to test_review_gate_honesty.py).
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


def _load_module(alias: str, path: str):
    """Load a module by file path under a unique alias (no sys.modules clash)."""
    if alias in sys.modules:
        return sys.modules[alias]
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


# The CONSUMER of this write. Loaded under its own alias so this file does not
# import create_code's lambda_function (which would fight over the
# `lambda_function` sys.modules entry). `LGR-28b4d75-5`: the expectation comes
# from the reader, not from the writer's own output.
_reader = _load_module("p32_t13_mapping_resolution", os.path.join(_CREATE_CODE_DIR, "mapping_resolution.py"))
_step_names = _load_module("p32_t13_step_names", os.path.join(_SHARED_DIR, "step_names.py"))

RUN_ID = "run-2026-08-04-00-00-00-storefb"

# The feedback_context analyze_test_results_for_regeneration builds (:408-451).
FEEDBACK_CONTEXT = {
    "summary": "Code regeneration needed (attempt 1/2): 2 reviewer issue(s), 1 failed check(s)",
    "issues_found": ["executed checks failed: 1 out of 4", "[critical] to_date format string is wrong."],
    "reviewer_issues": [
        {"severity": "critical", "category": "Correctness", "description": "to_date format string is wrong."},
        {"severity": "major", "category": "Security", "description": "Credentials logged in plaintext."},
    ],
    "recommendations": ["Address every reviewer issue listed above and fix failing checks"],
}


@pytest.fixture
def state_table(mock_dynamodb_tables):
    """The real (moto) pipeline-runs/state table: PK run_id, SK step_name."""
    return mock_dynamodb_tables["agentic_data_ingestion_state"]


# ---------------------------------------------------------------------------
# The round-trip: the write lands where the reader looks (2.16)
# ---------------------------------------------------------------------------


def test_write_is_readable_by_create_codes_feedback_reader(state_table):
    """Write with test_code, read with create_code — the loop 2.16 needs.

    The only assertion that can actually detect the two halves drifting: the
    expectation is not "update_item was called with X", it is "the reader finds
    the reviewer's issues". Both sides run against a real DynamoDB engine, so
    the key schema, the attribute name and the map/list round-trip are all
    genuine.

    On a byte-identical-prompt regression this fails first and names the cause.

    **Validates: Requirements 2.16**
    """
    with patch.object(_mod, "pipeline_runs_table", state_table):
        _mod.store_regeneration_feedback(RUN_ID, FEEDBACK_CONTEXT)

    assert _reader.get_regeneration_feedback(RUN_ID, state_table) == FEEDBACK_CONTEXT


def test_write_lands_under_the_key_the_reader_reads(state_table):
    """The item key is (run_id, StepName.TEST_CODE) — derived from the reader.

    The expected key is captured by observing what the READER asks for, then
    asserted against what the writer produced. Transcribing
    ``{"run_id": ..., "step_name": "test_code"}`` from the writer's source
    would be the `LGR-28b4d75-5` mistake: it would keep passing if both sides
    moved to the wrong key together.

    **Validates: Requirements 2.16**
    """
    probe = MagicMock(return_value={})
    _reader.get_regeneration_feedback(RUN_ID, MagicMock(get_item=probe))
    key_the_reader_reads = probe.call_args.kwargs["Key"]

    table = MagicMock()
    with patch.object(_mod, "pipeline_runs_table", table):
        _mod.store_regeneration_feedback(RUN_ID, FEEDBACK_CONTEXT)

    assert table.update_item.call_args.kwargs["Key"] == key_the_reader_reads
    # …and, independently of the reader, that it is the canonical step name
    # rather than a literal that happens to match today.
    assert key_the_reader_reads == {"run_id": RUN_ID, "step_name": _step_names.StepName.TEST_CODE}


def test_write_targets_the_state_row_of_the_run_being_tested(state_table):
    """The run id is used verbatim — no derived or prefixed id.

    ``store_regeneration_feedback`` assigns ``test_step_id = run_id`` (:513).
    2.16 requires the PRIOR ITERATION'S feedback for THIS run, and create_code
    reads with the run id it was handed, so any transformation of the id here
    breaks the join.

    **Validates: Requirements 2.16**
    """
    with patch.object(_mod, "pipeline_runs_table", state_table):
        _mod.store_regeneration_feedback(RUN_ID, FEEDBACK_CONTEXT)

    stored = state_table.get_item(Key={"run_id": RUN_ID, "step_name": _step_names.StepName.TEST_CODE})
    assert "Item" in stored, "feedback must be written under the unmodified run id"


# ---------------------------------------------------------------------------
# The update_item call itself (2.16)
# ---------------------------------------------------------------------------


def test_update_expression_sets_feedback_and_timestamp_without_clobbering_the_row():
    """SET of exactly the two feedback attributes — not a put_item.

    ``update_item`` with a SET expression is load-bearing: the test_code state
    row already carries the step's own status/output, and the run cannot lose
    it just because feedback was attached. A ``put_item`` here would silently
    delete the rest of the row, and ``aggregate_check_results``' output would
    disappear from the run.

    **Validates: Requirements 2.16**
    """
    table = MagicMock()
    with patch.object(_mod, "pipeline_runs_table", table):
        _mod.store_regeneration_feedback(RUN_ID, FEEDBACK_CONTEXT)

    table.put_item.assert_not_called()
    table.update_item.assert_called_once()
    kwargs = table.update_item.call_args.kwargs

    expression = kwargs["UpdateExpression"]
    assert expression.strip().upper().startswith("SET")
    assert "regeneration_feedback = :feedback" in expression
    assert "feedback_timestamp = :timestamp" in expression
    assert "REMOVE" not in expression.upper()

    assert kwargs["ExpressionAttributeValues"][":feedback"] == FEEDBACK_CONTEXT


def test_reviewer_issues_are_written_verbatim_not_flattened():
    """The structured issues survive the write with their fields intact.

    ``_build_feedback_section`` (transform_generation.py:45-51) renders
    ``severity``, ``category`` and ``description`` per issue. Flattening the
    issues to strings, or dropping a field, degrades the regeneration prompt to
    something closer to the original — which is the outcome 2.16 forbids.

    **Validates: Requirements 2.16**
    """
    table = MagicMock()
    with patch.object(_mod, "pipeline_runs_table", table):
        _mod.store_regeneration_feedback(RUN_ID, FEEDBACK_CONTEXT)

    written = table.update_item.call_args.kwargs["ExpressionAttributeValues"][":feedback"]
    assert written["reviewer_issues"] == FEEDBACK_CONTEXT["reviewer_issues"]
    for issue in written["reviewer_issues"]:
        assert {"severity", "category", "description"} <= set(issue)


def test_feedback_timestamp_is_iso8601_utc():
    """The stored timestamp is timezone-aware ISO-8601 in UTC.

    2.17 bounds attempts, so "which iteration produced this feedback" has to be
    answerable from the row. A naive or local-time string makes ordering
    between attempts ambiguous.

    **Validates: Requirements 2.17**
    """
    table = MagicMock()
    before = datetime.now(UTC)
    with patch.object(_mod, "pipeline_runs_table", table):
        _mod.store_regeneration_feedback(RUN_ID, FEEDBACK_CONTEXT)
    after = datetime.now(UTC)

    raw = table.update_item.call_args.kwargs["ExpressionAttributeValues"][":timestamp"]
    assert isinstance(raw, str)
    parsed = datetime.fromisoformat(raw)
    assert parsed.tzinfo is not None, "timestamp must be timezone-aware"
    assert parsed.utcoffset().total_seconds() == 0, "timestamp must be UTC"
    assert before <= parsed <= after


# ---------------------------------------------------------------------------
# Successive attempts (2.17)
# ---------------------------------------------------------------------------


def test_second_attempt_feedback_replaces_the_first(state_table):
    """The row carries the LATEST iteration's feedback, not an accumulation.

    2.16 says "the prior iteration's reviewer feedback" — singular and most
    recent. Under 2.17's bound of ``DEFAULT_REGENERATION_MAX_ATTEMPTS`` the
    same (run_id, test_code) row is written once per attempt, so attempt 2 must
    read attempt 1's issues rather than attempt 1's plus a stale copy of the
    original.

    **Validates: Requirements 2.16, 2.17**
    """
    second = {
        "summary": "Code regeneration needed (attempt 2/2): 1 reviewer issue(s), 1 failed check(s)",
        "issues_found": ["executed checks failed: 1 out of 4"],
        "reviewer_issues": [
            {"severity": "critical", "category": "Schema Compliance", "description": "target column missing."}
        ],
        "recommendations": ["Address every reviewer issue listed above and fix failing checks"],
    }

    with patch.object(_mod, "pipeline_runs_table", state_table):
        _mod.store_regeneration_feedback(RUN_ID, FEEDBACK_CONTEXT)
        _mod.store_regeneration_feedback(RUN_ID, second)

    assert _reader.get_regeneration_feedback(RUN_ID, state_table) == second


def test_write_preserves_other_attributes_on_the_state_row(state_table):
    """Attaching feedback must not disturb the step's own recorded result.

    The SET-only expression asserted above, verified end to end against a real
    table: the test_code row's ``status`` and ``output_data`` survive.

    **Validates: Requirements 2.16**
    """
    state_table.put_item(
        Item={
            "run_id": RUN_ID,
            "step_name": _step_names.StepName.TEST_CODE,
            "status": "success",
            "output_data": {"test_status": "failed"},
        }
    )

    with patch.object(_mod, "pipeline_runs_table", state_table):
        _mod.store_regeneration_feedback(RUN_ID, FEEDBACK_CONTEXT)

    item = state_table.get_item(Key={"run_id": RUN_ID, "step_name": _step_names.StepName.TEST_CODE})["Item"]
    assert item["status"] == "success"
    assert item["output_data"] == {"test_status": "failed"}
    assert item["regeneration_feedback"] == FEEDBACK_CONTEXT


# ---------------------------------------------------------------------------
# CURRENT behaviour that 2.16 argues against — pinned, not endorsed
# ---------------------------------------------------------------------------


def test_write_failure_is_swallowed_today(state_table):
    """A failed write is logged and swallowed: no raise, no return value.

    CHARACTERIZATION OF CURRENT BEHAVIOUR, not an endorsement. ``:524-525``
    catches every exception. Under 2.16 a lost write is the byte-identical
    retry the clause forbids, delivered silently, and it still burns one of
    2.17's bounded attempts. Compare the READ side, which was deliberately
    narrowed the other way under P28 R17.3 — a throttle there raises
    ``TransientStepFailure`` precisely so a dropped feedback read cannot
    masquerade as "no feedback"
    (``mapping_resolution.get_regeneration_feedback``, and
    ``tests/unit/lambdas/create_code/test_read_path_errors.py:120-137``). The
    write half has no such narrowing.

    Task 16 may reverse this. `LGR-5906ec6-2` is the precedent for how this
    repo handles that: the hold is encoded as an assertion, so turning it
    around means editing this test on purpose rather than discovering later
    that nobody noticed.

    **Validates: Requirements 2.16**
    """
    table = MagicMock()
    table.update_item.side_effect = RuntimeError("simulated DynamoDB failure")

    with patch.object(_mod, "pipeline_runs_table", table):
        assert _mod.store_regeneration_feedback(RUN_ID, FEEDBACK_CONTEXT) is None

    # And the consequence: the reader finds nothing, so the next prompt is the
    # failed prompt again — with nothing raised anywhere to say so.
    assert _reader.get_regeneration_feedback(RUN_ID, state_table) == {}
