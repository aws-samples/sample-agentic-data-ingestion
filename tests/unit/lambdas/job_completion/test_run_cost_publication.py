# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: job_completion publishes a run-cost estimate per Glue run (p39 R2.1, R2.2).

The call-site half of p39 task 5.3. The publisher itself — metric name,
dimensions, unit, and that it flushes — is covered in
``tests/unit/shared/test_metrics_facade_run_cost.py``. This file covers
``_publish_run_cost`` (``lambda_function.py:629-711``) and the call at ``:951``.

**Why this anchor.** The publish is the fifth writer on ``LGR-eee8ac8-4``'s
post-callback anchor (**template / ratified**), for that record's own reason: a
side effect that depends on the run having happened is committed at the
completion callback, not six steps before it. A cost estimate for a run that
never ran is a fabricated number, which is why nothing publishes this from
``run_pipeline`` at job-start time.

**Why it fires on every terminal state, where its three neighbours do not.**
``_commit_target_schema_version``, ``_record_processed_file`` and
``_populate_mirror_metadata_location`` all no-op on non-``SUCCEEDED`` because they
record *data* outcomes — a version that was written, a file that was ingested, a
metadata address that now exists — and a failed run establishes none of those.
This records *spend*: AWS bills the DPU-minutes of a run that FAILED, was STOPPED
or hit TIMEOUT exactly as it bills a successful one, so gating on ``SUCCEEDED``
would under-report the cost tile by the whole of every failed run. That is what
the four-state parametrization below pins.

**Why nothing here dedupes.** R2.2's once-per-run is the *existing* idempotency,
not a new mechanism: on a duplicate EventBridge delivery the record is still
found, but the first delivery consumed the task token, so Step Functions rejects
the send with a consumed-token code and the handler returns from
``_consumed_token_response`` (``:857`` / ``:873``) — structurally before the call
at ``:951``. The two-delivery test asserts that, so a future refactor that moved
the publish above the callback would fail a test rather than start
double-counting spend.

**Best-effort.** The Step Functions callback is the run's source of truth and has
already been sent by the time the publish runs, so a publish failure must never
change the returned status. ``LGR-9d1c0e4-1`` and ``LGR-8f3f9e1-1`` (both
**backfilled**) record a bare ``except`` in this same file dropping a metric
silently on every run, and ``LGR-9e76249-2`` (**template / ratified**) named a
fifth writer on this anchor as exactly that risk when it rejected
``job_completion`` for a different derivation — so "swallowed" is asserted
together with "logged at WARNING", never alone.

Expectations derive from R2.1 and R2.2. The harness follows
``test_target_schema_version_commit.py``, which covers the third writer on the
same anchor; CloudWatch and Step Functions are outside the shared fixtures'
scope, so ``MagicMock`` is the established pattern there.

Validates: p39 Requirements 2.1, 2.2
"""

import os
import sys
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

# Add Lambda dir and shared dir to path
_job_completion_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_job_completion")
)
sys.path.insert(0, _job_completion_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

sys.modules.pop("lambda_function", None)
import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


RUN_ID = "run-p39-cost-1"
PIPELINE_ID = "pipe-p39"
JOB_NAME = "agentic-data-ingestion-pipe-p39"
JOB_RUN_ID = "jr-p39"

# ExecutionTime 60s on 2 × G.1X is below the 10-minute DPU minimum
# ``collect_job_metrics`` applies (`lambda_function.py:139-143`), so the estimate
# is 2 DPU × 600 s ÷ 3600 × $0.44 = 0.147 — the constant every short run
# produces, and the reason p39 R2.7 requires the basis to be stated.
EXPECTED_COST = 0.147

TERMINAL_STATES = ["SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT"]


@pytest.fixture(autouse=True)
def _pinned_environment(monkeypatch):
    """Pin ENVIRONMENT so the published dimension is deterministic."""
    monkeypatch.setenv("ENVIRONMENT", "dev")


def _event(state="SUCCEEDED"):
    return {"detail": {"jobName": JOB_NAME, "jobRunId": JOB_RUN_ID, "state": state}}


def _token_record(pipeline_id=PIPELINE_ID):
    return {
        "run_id": RUN_ID,
        "step_name": "run_pipeline",
        "original_pipeline_id": pipeline_id,
        "glue_job_run_id": JOB_RUN_ID,
        "status": "RUNNING",
    }


def _task_tokens_table():
    """The run's Glue-callback token row — run_pipeline stores it in the
    task-tokens table, never on the state row."""
    table = MagicMock(name="task_tokens_table")
    table.get_item.return_value = {"Item": {"approval_id": f"glue_run#{RUN_ID}", "task_token": "token-p39"}}
    return table


def _glue_job_run(state="SUCCEEDED"):
    return {
        "JobRun": {
            "JobRunState": state,
            "ExecutionTime": 60,
            "NumberOfWorkers": 2,
            "WorkerType": "G.1X",
            "MaxCapacity": 2.0,
            "GlueVersion": "5.0",
            "ErrorMessage": "",
        }
    }


def _mocks(state="SUCCEEDED", pipeline_id=PIPELINE_ID):
    """Wire the handler's four AWS clients.

    ``get_item`` returns a normal (non-heal) ``file_validation`` row so the heal
    linker returns early, and nothing for any other step row, so the two other
    best-effort writers on this anchor are no-ops and cannot mask the assertions
    below.
    """
    rows = {"file_validation": {"run_id": RUN_ID, "step_name": "file_validation", "triggered_by": "s3_upload"}}

    runs_table = MagicMock()
    runs_table.query.return_value = {"Items": [_token_record(pipeline_id)]}
    runs_table.get_item.side_effect = lambda Key: (
        {"Item": rows[Key["step_name"]]} if rows.get(Key["step_name"]) is not None else {}
    )

    glue = MagicMock()
    glue.get_job_run.return_value = _glue_job_run(state)

    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {}

    return runs_table, glue, pipelines_table, MagicMock()


def _run_handler(runs_table, glue, pipelines_table, sfn, event, publisher=None, logger=None):
    """Invoke the handler with the run-cost publisher stubbed.

    ``publish_glue_run_cost`` is imported into this module's namespace at
    ``lambda_function.py:46``, so patching the module attribute intercepts the
    call the handler actually makes.
    """
    publisher = publisher if publisher is not None else MagicMock()
    patches = [
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "task_tokens_table", _task_tokens_table()),
        patch.object(_mod, "glue_client", glue),
        patch.object(_mod, "pipelines_table", pipelines_table),
        patch.object(_mod, "sfn_client", sfn),
        patch.object(_mod, "lambda_client", MagicMock()),
        patch.object(_mod, "publish_glue_run_cost", publisher),
    ]
    if logger is not None:
        # Patched only where a log assertion needs it: the module's logger is a
        # Powertools ``Logger``, whose records ``caplog`` does not see.
        patches.append(patch.object(_mod, "logger", logger))

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return _mod.lambda_handler(event, None), publisher


def _consumed_token_error(code="TaskTimedOut"):
    return ClientError({"Error": {"Code": code, "Message": "already consumed"}}, "SendTaskSuccess")


# ── Fires on every terminal state ─────────────────────────────────────


@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_every_terminal_state_publishes_the_run_cost(state):
    """A run that ended is billed, whatever way it ended.

    The deliberate divergence from the three writers above it on this anchor:
    they no-op on non-``SUCCEEDED`` because they record data outcomes, and this
    records spend. FAILED, STOPPED and TIMEOUT runs consumed DPU-minutes and AWS
    charged for them, so omitting them under-reports the cost tile.

    **Validates: Requirements 2.1, 2.2**
    """
    mocks = _mocks(state)
    result, publisher = _run_handler(*mocks, _event(state))

    assert result["statusCode"] == 200
    assert publisher.call_count == 1
    assert publisher.call_args.kwargs == {
        "pipeline_id": PIPELINE_ID,
        "job_name": JOB_NAME,
        "estimated_cost": EXPECTED_COST,
        "worker_type": "G.1X",
        "environment": "dev",
    }


def test_environment_dimension_comes_from_the_lambda_environment(monkeypatch):
    """The published Environment dimension is the deployment's own, not a constant.

    It is what keeps a dev run's spend out of a prod window.

    **Validates: Requirement 2.1**
    """
    monkeypatch.setenv("ENVIRONMENT", "prod")
    mocks = _mocks("SUCCEEDED")

    _, publisher = _run_handler(*mocks, _event("SUCCEEDED"))

    assert publisher.call_args.kwargs["environment"] == "prod"


def test_publish_happens_after_the_step_functions_callback():
    """The run's source of truth precedes the best-effort publish.

    This is what makes "a publish failure cannot change the outcome" structural
    rather than incidental — and what would fail if a refactor moved the publish
    above the callback, where a duplicate delivery would reach it and
    double-count.

    **Validates: Requirements 2.1, 2.2**
    """
    mocks = _mocks("SUCCEEDED")
    order: list[str] = []
    mocks[3].send_task_success.side_effect = lambda **kw: order.append("callback")
    publisher = MagicMock(side_effect=lambda **kw: order.append("publish"))

    _run_handler(*mocks, _event("SUCCEEDED"), publisher=publisher)

    assert order == ["callback", "publish"]


# ── A duplicate delivery never reaches it ─────────────────────────────


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED"])
@pytest.mark.parametrize("code", ["TaskTimedOut", "TaskDoesNotExist", "InvalidToken"])
def test_consumed_token_delivery_publishes_nothing(state, code):
    """A consumed-token callback returns before the publish, on both callback paths.

    R2.2 needs no new mechanism: the token is the existing dedupe. The handler
    returns from ``_consumed_token_response`` at ``:857`` / ``:873``, structurally
    before ``:951``.

    **Validates: Requirement 2.2**
    """
    mocks = _mocks(state)
    mocks[3].send_task_success.side_effect = _consumed_token_error(code)
    mocks[3].send_task_failure.side_effect = _consumed_token_error(code)

    result, publisher = _run_handler(*mocks, _event(state))

    assert result["statusCode"] == 200
    assert "already consumed" in result["message"]
    publisher.assert_not_called()


@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_two_deliveries_of_one_run_publish_exactly_once(state):
    """Exactly one datapoint across a first delivery and its duplicate.

    Modelled as the real sequence rather than as a code assertion: the first
    delivery consumes the token and publishes; the second finds the record, is
    rejected by Step Functions, and returns early. The same publisher instance
    spans both, so its call count is the run's total.

    **Validates: Requirement 2.2**
    """
    publisher = MagicMock()

    first = _mocks(state)
    result_1, _ = _run_handler(*first, _event(state), publisher=publisher)

    second = _mocks(state)
    second[3].send_task_success.side_effect = _consumed_token_error()
    second[3].send_task_failure.side_effect = _consumed_token_error()
    result_2, _ = _run_handler(*second, _event(state), publisher=publisher)

    assert result_1["statusCode"] == 200
    assert result_2["statusCode"] == 200
    assert publisher.call_count == 1, "a duplicate delivery must not add a second cost datapoint"


# ── A raising publisher does not change the returned status ───────────


@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_a_raising_publisher_leaves_the_status_unchanged(state):
    """A publish failure is swallowed; the run's reported outcome is untouched.

    The real publisher logs and never raises, so this drives the caller's own
    ``try`` / ``except`` at ``:698-711`` directly. 200 rather than the outer
    handler's 500 is the assertion that matters: reaching
    ``lambda_function.py:975`` would turn a bookkeeping failure into a reported
    invocation error, and EventBridge redelivery on top of it.

    **Validates: Requirement 2.1**
    """
    mocks = _mocks(state)
    publisher = MagicMock(side_effect=RuntimeError("cloudwatch unavailable"))
    logger = MagicMock()

    result, _ = _run_handler(*mocks, _event(state), publisher=publisher, logger=logger)

    assert result["statusCode"] == 200
    assert result["message"] == f"Job completion processed: {state}"
    warnings = [c for c in logger.warning.call_args_list if "run cost estimate" in str(c)]
    assert warnings, "a swallowed publish failure must still be logged at WARNING"


# ── The two early returns ─────────────────────────────────────────────


def test_a_run_with_no_pipeline_id_publishes_nothing():
    """An unattributable datapoint is not published.

    ``PipelineId`` is the dimension that makes a cost datapoint belong to a
    pipeline, and CloudWatch does not accept an empty dimension value. Same
    guard the three writers above use.

    **Validates: Requirement 2.1**
    """
    mocks = _mocks("SUCCEEDED", pipeline_id="")

    result, publisher = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    publisher.assert_not_called()


def test_an_unreadable_job_run_publishes_nothing_and_warns():
    """No cost estimate means no datapoint — never a fabricated 0.0.

    ``collect_job_metrics`` falls into its own ``except`` when the Glue job-run
    details cannot be read and returns an error shape carrying no
    ``actual_cost``. Publishing 0.0 there would assert the run was free, which is
    the fabricated number this anchor exists to prevent; the absence stays absent
    and the dashboard reports Not_Established for a window with no datapoint
    (R2.4).

    **Validates: Requirements 2.1, 2.4**
    """
    mocks = _mocks("SUCCEEDED")
    mocks[1].get_job_run.side_effect = RuntimeError("glue unavailable")
    logger = MagicMock()

    result, publisher = _run_handler(*mocks, _event("SUCCEEDED"), logger=logger)

    assert result["statusCode"] == 200
    publisher.assert_not_called()
    warnings = [c for c in logger.warning.call_args_list if "No cost estimate available" in str(c)]
    assert warnings, "an absent cost estimate must be reported, not silently skipped"
