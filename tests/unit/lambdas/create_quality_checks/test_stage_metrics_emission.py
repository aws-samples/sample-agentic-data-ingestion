# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_quality_checks' two stage-duration emit sites (p39 C5, R6.1).

`CreateQualityChecks` had no producer at all before task 10.1 — no
`PipelineMetrics` reference and no `metrics_facade` reference anywhere in
`lambda/agentic_data_ingestion_create_quality_checks/lambda_function.py` — so the
dashboard's Avg Time for that stage was Not_Established for want of an emitter,
not for want of runs. R6.1 is therefore satisfied by an addition, which is what
task 9's gate ratified.

What this file pins: the emit fires with `StageName="CreateQualityChecks"` on
**both** the success and the failure path, so a stage that ran and failed stays
distinguishable from a stage that never ran.

**Deliberately not stubbing `metrics_facade`.** Fourteen files under
`tests/unit/lambdas/create_code/` stub it as `MagicMock(PipelineMetrics=None)`;
`from metrics_facade import PipelineMetrics` then binds `None`, the
`if PipelineMetrics is not None:` guards are False, and **neither emit runs** —
the trap task 8.3 documented. The three sibling files here
(`test_quality_verdict_recording.py`, `test_read_path_errors.py`,
`test_step_failure_raise.py`) load the real module with `shared/` on `sys.path`,
so the real `PipelineMetrics` binds. `test_the_emit_guard_is_reachable` asserts
that directly, which is what keeps the two path tests below from being vacuous.

No AWS call is made. Two things stop one: `tests/unit/conftest.py`'s autouse
`no_cloudwatch_writes` guard, and — in the composed test at the end — a
`MagicMock` assigned to the facade's `_cloudwatch`, which is the established
pattern in `tests/unit/shared/test_metrics_facade_flush.py`.

Recording only. This emit evaluates no threshold and gates nothing, per
`LGR-eee8ac8-6` (template / ratified), the record that deleted this step's inert
`fail_on_error` / `max_error_threshold` keys rather than honour them. Nothing
here reintroduces one.

Validates: Requirements R6.1
"""

import importlib.util
import os
import sys
import unittest.mock as mock
from unittest.mock import MagicMock

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_quality_checks",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

# tests/unit/conftest.py pins the region by assignment (LGR-3e45e65-1), so these
# are consistent no-ops under it; kept for parity with the sibling files.
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


def _load_module():
    """Load the create_quality_checks lambda_function by path (sibling-test pattern).

    A file-location load under a unique module name, not
    `sys.modules["lambda_function"]`: the alias is shared across the whole
    `tests/unit/lambdas/` tree and colliding on it is what makes some combined
    selections fail.
    """
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_quality_checks_lf_p39_stage_metrics",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["create_quality_checks_lf_p39_stage_metrics"] = mod
    spec.loader.exec_module(mod)
    return mod


_lf = _load_module()

RUN_ID = "run-stage-metrics-qc"
PIPELINE_ID = "pipe-stage-metrics-qc"
STAGE_NAME = "CreateQualityChecks"

# The create_code state row shape this step reads its verdict facts from,
# transcribed from that writer rather than produced by importing it.
DQDL_TWO_RULES = 'Rules = [\n    RowCount > 0,\n    Completeness "customer_id" >= 0.95\n]'


def _runs_table() -> mock.MagicMock:
    """State-table mock answering the handler's cross-step read for create_code."""
    table = mock.MagicMock()

    def _get_item(Key):  # boto3 names the kwarg "Key"
        if Key["step_name"] == "create_code":
            return {
                "Item": {
                    "run_id": RUN_ID,
                    "step_name": "create_code",
                    "status": "success",
                    "output_data": {
                        "summary": {"template_used": "glue_job_s3_tables.py.j2", "target_type": "s3_tables"},
                        "approval": {"dqdl_rules": DQDL_TWO_RULES},
                    },
                }
            }
        return {}

    table.get_item.side_effect = _get_item
    return table


def _run_success_path(stage_metrics) -> dict:
    with (
        mock.patch.object(_lf, "pipeline_runs_table", _runs_table()),
        mock.patch.object(_lf, "PipelineMetrics", stage_metrics),
    ):
        return _lf.lambda_handler({"run_id": RUN_ID, "pipeline_id": PIPELINE_ID}, None)


def _run_failure_path(stage_metrics):
    """Drive the failure path the way the sibling P28 test does: no run_id.

    `ValueError("run_id and pipeline_id are required")` is raised on the first
    statement inside the `try`, which is precisely why task 10.1 anchored
    `_metrics_start` *before* the `try`.
    """
    with (
        mock.patch.object(_lf, "pipeline_runs_table", mock.MagicMock()),
        mock.patch.object(_lf, "PipelineMetrics", stage_metrics),
        pytest.raises(_lf.StepFailure),
    ):
        _lf.lambda_handler({"pipeline_id": PIPELINE_ID}, None)


# ── Non-vacuity: the emit is actually reachable in this directory ─────────────


def test_the_emit_guard_is_reachable():
    """`PipelineMetrics` resolved to the real class, so `is not None` is True.

    If a future edit stubbed `metrics_facade` at module scope here, both emits
    would become unreachable and every assertion below would pass while testing
    nothing. This is the assertion that fails instead.
    """
    assert _lf.PipelineMetrics is not None
    assert hasattr(_lf.PipelineMetrics, "record_stage_execution")


# ── R6.1: both paths emit, named for this stage ──────────────────────────────


def test_success_path_records_the_stage_with_its_own_name():
    stage_metrics = MagicMock()

    result = _run_success_path(stage_metrics)

    assert result["statusCode"] == 200
    assert stage_metrics.record_stage_execution.call_count == 1
    kwargs = stage_metrics.record_stage_execution.call_args.kwargs
    assert kwargs["stage_name"] == STAGE_NAME
    assert kwargs["pipeline_id"] == PIPELINE_ID
    assert kwargs["status"] == "completed"
    assert isinstance(kwargs["duration_ms"], float)
    assert kwargs["duration_ms"] >= 0


def test_failure_path_records_the_stage_as_failed():
    """A stage that ran and failed reports a duration too, and still fails the step."""
    stage_metrics = MagicMock()

    _run_failure_path(stage_metrics)

    assert stage_metrics.record_stage_execution.call_count == 1
    kwargs = stage_metrics.record_stage_execution.call_args.kwargs
    assert kwargs["stage_name"] == STAGE_NAME
    assert kwargs["pipeline_id"] == PIPELINE_ID
    assert kwargs["status"] == "failed"
    assert isinstance(kwargs["duration_ms"], float)
    assert kwargs["duration_ms"] >= 0


def test_each_run_emits_exactly_once_on_exactly_one_path():
    """The two guarded blocks are mutually exclusive — a successful run does not
    also report a failure, and neither path emits twice."""
    success_metrics = MagicMock()
    _run_success_path(success_metrics)
    assert [c.kwargs["status"] for c in success_metrics.record_stage_execution.call_args_list] == ["completed"]

    failure_metrics = MagicMock()
    _run_failure_path(failure_metrics)
    assert [c.kwargs["status"] for c in failure_metrics.record_stage_execution.call_args_list] == ["failed"]


# ── The emit composes with the real producer ─────────────────────────────────


def test_the_stage_name_reaches_put_metric_data_through_the_real_producer(monkeypatch):
    """One handler run publishes `Pipeline.StageDuration` for this stage.

    The emit alone does not establish R6.1: what the dashboard reads is a
    CloudWatch series dimensioned `StageName=CreateQualityChecks`, and between
    the emit and that series sit `put_metric`'s branch and the flush task 8.1
    added. This is the one test where both halves run.

    CloudWatch is a `MagicMock` on `_cloudwatch`, the established pattern in
    `tests/unit/shared/test_metrics_facade_flush.py` — no client is built and no
    AWS call is made. `POWERTOOLS_ENABLED=false` is the buffered path, i.e. the
    one that needs the flush; this Lambda runs with the flag `'true'` in
    production (`compute-stack.ts:887`), where the flush is a no-op and EMF
    publishes instead.
    """
    monkeypatch.setenv("POWERTOOLS_ENABLED", "false")
    sys.modules.pop("metrics_facade", None)
    try:
        import metrics_facade as real_facade

        assert real_facade.POWERTOOLS_AVAILABLE is False
        mock_cw = MagicMock()
        real_facade.metrics_facade._cloudwatch = mock_cw

        result = _run_success_path(real_facade.PipelineMetrics)

        assert result["statusCode"] == 200
        assert mock_cw.put_metric_data.call_count == 1
        kwargs = mock_cw.put_metric_data.call_args.kwargs
        assert kwargs["Namespace"] == "AgenticDataIngestion/Pipeline"
        published = {m["MetricName"]: m for m in kwargs["MetricData"]}
        assert "Pipeline.StageDuration" in published
        # AMENDED by p39 task 13.2, with its reason kept at the assertion.
        #
        # This asserted exactly `{StageName, PipelineId, Status}` — what the
        # buffered path published once task 8.1's flush made it reach CloudWatch,
        # and a shape no consumer can read. `_stage_durations_cw`
        # (`dashboard_api/handlers/metrics.py:1108`) searches a four-name brace
        # schema including `service`, which Powertools stamps on the EMF path and
        # the direct fallback did not, and a brace schema is an EXACT match on the
        # dimension-NAME set. Task 13.2 adds `service` in `put_metric`'s
        # Powertools fallback so both paths publish one shape.
        #
        # The equality against the consumer's own schema is asserted in
        # `tests/unit/shared/test_metrics_facade_service_dimension.py` (task 13.3).
        assert {d["Name"]: d["Value"] for d in published["Pipeline.StageDuration"]["Dimensions"]} == {
            "StageName": STAGE_NAME,
            "PipelineId": PIPELINE_ID,
            "Status": "completed",
            "service": "agentic_data_ingestion",
        }
    finally:
        # The sibling files bind `PipelineMetrics` at import time, so leaving a
        # reloaded module in the cache would be a cross-file effect.
        sys.modules.pop("metrics_facade", None)
