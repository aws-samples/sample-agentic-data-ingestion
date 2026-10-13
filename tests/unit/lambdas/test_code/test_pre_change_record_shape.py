# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test: a real pre-change Pipeline_Record gates on the next run (p34 R7.2).

This is the test that states the **behavioural change** requirement 7 warns
about. A pipeline whose record was written before p34 carries **no
``approval_config`` key at all** — nothing has ever written one for it. Under
requirement 1.2 the Code_Approval_Setting for such a record resolves to ``True``,
so the Approval_Policy's ``pipeline_config`` disjunct fires and a run that
previously went straight from ``TestCode`` to ``CreateInfrastructure`` now enters
``WaitForCodeApproval``.

The run exercised here is a **clean** one: the review passes, execution
succeeds, and ``quality_gate_requires_approval`` is ``False``. That is asserted
on the same invocation on purpose — it makes the reason for the gate
unambiguous. Approval is required because of the *configuration default*, not
because anything about the code is in doubt. The two fields differing on one run
is the whole of this spec (requirements 3.1 and 3.2).

**Requirement 7.1 — no migration, no per-pipeline backfill.** The resolution
happens on **read**, inside ``resolve_code_approval_setting``, from a record
nothing wrote an approval key into. This test asserts that directly: the
pipelines table sees a ``get_item`` and no write at all, and the fixture record
is asserted to carry no ``approval_config`` key so it cannot drift into a
config-carrying shape and quietly stop being a pre-change record.

**Fixture provenance.** The record shape is transcribed from
``test_honest_aggregation.py``'s ``_PIPELINE_RECORD_NO_TARGET_SCHEMA`` — the
richer of the two sites design §4.7 names, and the one that is a documented
module constant. ``test_review_gate_calibration.py``'s ``_fake_dynamodb``
returns the same shape minus ``pipeline_name``
(``{"Item": {"pipeline_id": "pipe-1"}}``); both are pre-change records for the
purposes of requirement 1.2, because neither carries ``approval_config``. Those
two fixtures are the mechanism behind six of design §4.7's rows — this file
states in one place what those six sites now imply.

**Validates: Requirements 1.2, 7.1, 7.2**
"""

import json
import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Region/account before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy / AWS-touching imports so the module loads in isolation.
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
# The pre-change record, transcribed — see the module docstring for provenance
# ---------------------------------------------------------------------------

#: A Pipeline_Record as it exists on a pipeline created before p34: it has a
#: ``pipeline_id``, a name, and **no ``approval_config`` key**. Transcribed from
#: ``test_honest_aggregation.py``'s ``_PIPELINE_RECORD_NO_TARGET_SCHEMA``. It
#: also carries no target schema, which is requirement 2.6's legitimate state
#: and keeps the check list here to the same shape the sibling files assert on.
_PRE_CHANGE_PIPELINE_RECORD = {"pipeline_id": "pipe-1", "pipeline_name": "pre-p34-pipeline"}

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {
        "code_version_id": "cv-1",
        "code_s3_uri": "s3://code-bucket/code.py",
    },
}

#: A clean review: the run this test describes is one that used to reach
#: ``CreateInfrastructure`` with no human in the loop.
_REVIEW_ALL_PASS = json.dumps(
    {
        "verdict": "pass",
        "issues": [],
        "categories": {"Correctness": "pass", "Security": "pass", "Performance": "pass"},
    }
)

_EXEC_OK = {"execution_successful": True, "input_records": 10, "output_records": 10}


class _RecordCollector(logging.Handler):
    """Collects the records the module's own logger actually emits.

    Attached to the underlying stdlib logger rather than replacing
    ``_mod.logger``, matching ``test_code_approval_setting_prop.py``: the
    Powertools ``Logger`` does not propagate to the root logger by default, so
    ``caplog`` would make an absent record indistinguishable from a suppressed
    one — and R7.4 is an assertion about a record existing.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def logs():
    stdlib_logger = logging.getLogger(_mod.logger.name)
    collector = _RecordCollector()
    original_level = stdlib_logger.level
    stdlib_logger.addHandler(collector)
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield collector
    finally:
        stdlib_logger.removeHandler(collector)
        stdlib_logger.setLevel(original_level)


def _state_rows(runs_table) -> list[dict]:
    """Reconstruct the state rows the handler wrote to the runs table.

    The row write is an ``update_item`` SET over the attributes this step owns,
    not a full-row ``put_item`` (see ``build_state_row_update``), so the row is
    the ``Key`` plus the SET values, identified by ``step_sequence`` — an
    attribute only the row write sets.
    """
    rows = []
    for call in runs_table.update_item.call_args_list:
        values = {name.lstrip(":"): value for name, value in call.kwargs["ExpressionAttributeValues"].items()}
        if "step_sequence" not in values:
            continue
        rows.append({**call.kwargs["Key"], **values})
    return rows


def _run_handler(review_text: str, execution_results: dict):
    """Run ``lambda_handler`` over the pre-change record.

    Returns ``(response, state_row, pipelines_table)``. The pipelines table mock
    is returned because requirement 7.1 is an assertion about what was *not*
    written to it.
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    # Only `get_pipeline_metadata` resolves a table through `dynamodb.Table` at
    # call time (`test_code/lambda_function.py:159`); the other three are
    # module-level and patched directly. So this mock IS the pipelines table.
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": dict(_PRE_CHANGE_PIPELINE_RECORD)}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table

    agent = MagicMock(return_value=review_text)
    agent.add_hook = MagicMock()

    runner = MagicMock()
    runner.run_test.return_value = execution_results

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", dynamodb),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        response = _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1"}, None)

    testing_items = [i for i in _state_rows(runs_table) if i.get("step_name") == "test_code"]
    assert len(testing_items) == 1, "expected exactly one test_code state item"
    return response, testing_items[0], pipelines_table


# ---------------------------------------------------------------------------
# R1.2 — the record shape itself, and what it resolves to
# ---------------------------------------------------------------------------


def test_pre_change_record_carries_no_approval_config_key():
    """**Validates: Requirements 1.2** — the fixture is a pre-change record.

    Asserted rather than assumed. If a future edit adds ``approval_config`` to
    this constant to make some other assertion convenient, the record stops
    being the pre-change shape R7.2 is about and this file stops testing what
    its name says.
    """
    assert "approval_config" not in _PRE_CHANGE_PIPELINE_RECORD


def test_pre_change_record_resolves_the_setting_from_the_default():
    """**Validates: Requirements 1.2** — absent key → ``True``, source ``default``.

    The same record dict the handler test below is seeded with, so the two
    cannot drift apart. The *source* matters as much as the value: it is what
    distinguishes "the owner asked for review" from "nobody has ever said, so
    the gate fails closed".
    """
    setting, source = _mod.resolve_code_approval_setting(_PRE_CHANGE_PIPELINE_RECORD)

    assert setting is True
    assert source == _mod._SETTING_FROM_DEFAULT


# ---------------------------------------------------------------------------
# R7.2 — the behavioural change, end to end through the handler
# ---------------------------------------------------------------------------


def test_handler_clean_run_on_pre_change_record_now_requires_approval(logs):
    """**Validates: Requirements 1.2, 7.1, 7.2** — a clean run now gates.

    This run passes everything: ``test_status`` is ``passed`` and the
    Code_Quality_Gate wants nothing. Before p34 the field the state machine
    reads was that gate's return, so the run went to ``CreateInfrastructure``
    unreviewed. Now the merged field carries the configuration too, and the
    configuration for a record with no ``approval_config`` resolves to ``True``
    (R1.2) — so the run enters ``WaitForCodeApproval``.

    The two approval fields differing on one invocation is what makes the reason
    unambiguous: ``quality_gate_requires_approval is False`` says the code is
    not under suspicion, ``approval_required is True`` says the pipeline's
    configuration asked for the review.
    """
    response, state_row, _ = _run_handler(_REVIEW_ALL_PASS, _EXEC_OK)

    # The run is clean — this is the case that used to skip the gate entirely.
    assert response["test_status"] == "passed"
    assert response["test_result"]["status"] == "passed"
    assert state_row["status"] == "success"

    # R3.1: the quality gate, unmerged, wants nothing. No quality concern here.
    assert response["quality_gate_requires_approval"] is False
    assert state_row["quality_gate_requires_approval"] is False

    # R7.2: the merged field the Code_Approval_Gate reads is True anyway.
    assert response["approval_required"] is True
    assert state_row["approval_required"] is True

    # R7.2: and the destination the step names is the approval state, not
    # pipeline creation. This is the line that used to read
    # "create_glue_pipeline" for this exact input.
    assert response["next_step"] == "approval_check"

    # R3.5: the recorded reason names the configuration, and only the
    # configuration — so the change is attributable rather than merely visible.
    assert state_row["approval_decision_inputs"] == {
        "pipeline_config": True,
        "quality_gate": False,
        "regeneration": False,
    }

    # R7.4: the source is logged on the invocation, naming the default rather
    # than the record — the operator-readable half of R1.2.
    source_lines = [r.getMessage() for r in logs.records if "Code_Approval_Setting for pipeline" in r.getMessage()]
    assert len(source_lines) == 1
    assert f"source: {_mod._SETTING_FROM_DEFAULT}" in source_lines[0]
    assert "evaluated as True" in source_lines[0]


def test_handler_reads_the_pipeline_record_and_writes_no_approval_key(logs):
    """**Validates: Requirements 7.1** — resolution on read, no migration.

    R7.1 says the Approval_Policy applies to every existing Pipeline_Record with
    no data migration and no per-pipeline backfill. The evidence is that the
    step *reads* the record and never writes to the pipelines table at all: the
    ``True`` above is produced by ``resolve_code_approval_setting`` at read
    time, not by an ``approval_config`` someone stamped onto the record.

    A backfill would show up here as a write on this mock. There is none, and
    there is nothing to add for this to keep working on the next pipeline.
    """
    _, _, pipelines_table = _run_handler(_REVIEW_ALL_PASS, _EXEC_OK)

    # The read happened — the setting came from somewhere real.
    assert pipelines_table.get_item.call_count >= 1

    # And nothing was written back. No migration, no backfill, no stamped key.
    pipelines_table.put_item.assert_not_called()
    pipelines_table.update_item.assert_not_called()
    pipelines_table.batch_writer.assert_not_called()
