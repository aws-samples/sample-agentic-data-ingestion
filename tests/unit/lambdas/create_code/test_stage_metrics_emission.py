# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``create_code``'s two stage-duration emit sites (p39 C5, R6.5/R6.6).

What this file exists to pin, and why no existing file could.

**The emit was unreachable in the whole directory.** Fourteen of the 28 files in
``tests/unit/lambdas/create_code/`` stub the shared module as
``"metrics_facade": MagicMock(PipelineMetrics=None)`` — ``test_canonical_state_row``,
``test_duration_metrics``, ``test_failure_ids``, ``test_mapping_freshness``,
``test_mapping_resolution_regeneration_feedback``, ``test_no_ghost_approval_publication``,
``test_no_keyword_self_approval``, ``test_read_path_errors``,
``test_reasoning_field_mapping_count``, ``test_regeneration_prompt_differs``,
``test_rule_contradiction_check``, ``test_silent_fallback_removal``,
``test_target_schema_and_hash_reads``, ``test_target_schema_in_transform_prompt``.
``from metrics_facade import PipelineMetrics`` then binds ``None``, so the
``if PipelineMetrics is not None:`` guards at ``:1004`` and ``:1065`` are False and
**neither emit runs**. None of the fourteen asserts on log output either, so the
``None`` branch's WARNING is emitted on every one of their handler runs and observed
by nothing.

This file therefore stubs ``metrics_facade`` as a plain ``MagicMock()`` — whose
``PipelineMetrics`` attribute is an auto-created mock, **not** ``None`` — and each
test then substitutes the exact double it needs onto ``_mod.PipelineMetrics``. The
last test goes further and installs the **real** ``PipelineMetrics`` from
``shared/metrics_facade.py`` with a stubbed CloudWatch client, so the two halves of
C5's producer fix (the flush in ``record_stage_execution`` and the emit here) are
pinned as composing rather than as two separate claims.

**R6.6's split.** ``USE_AGENTCORE_RUNTIME`` is ``'true'`` on this Lambda
(``compute-stack.ts:400``), so the handler returns at ``:383-391`` through
``invoke_agentcore_runtime`` and never reaches the emit; the container's own
condition is the flag's *absence*, since ``agentContainerEnv`` does not set it.
Both halves are asserted, so C5's diagnosis is verified rather than assumed.

Log assertions go through ``caplog`` on the ``agentic_data_ingestion`` logger — the
Powertools ``Logger`` at ``:55`` wraps that stdlib logger and propagates.

Expectations derive from R6.5 (a failed emission is recorded at WARNING or above)
and R6.6 (the emit is on the path that performs the work), not from current output.
Never failing the step is ``LGR-9e76249-4``'s ratified stance for this very file:
the handler records its findings and does not fail on them.

Validates: Requirements R6.5, R6.6
"""

import contextlib
import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region before any boto3 imports (tests/unit/conftest.py pins the value —
# LGR-3e45e65-1; these setdefault calls are consistent no-ops under it).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_create_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _create_code_dir)
_shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
if _shared_dir not in sys.path:
    sys.path.insert(0, _shared_dir)

# Stub heavy dependencies. Mirrors test_read_path_errors.py EXCEPT for
# ``metrics_facade``: a bare ``MagicMock()`` leaves ``PipelineMetrics`` non-None so
# the emit guards are reachable. See the module docstring.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(
        extract_code_only=MagicMock(return_value="# code"),
        validate_generated_code=MagicMock(return_value={"is_valid": True, "errors": [], "summary": {}}),
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(
        load_prompt_template=MagicMock(return_value="mock prompt"),
    ),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "artifact_schema": MagicMock(
        create_output_data=MagicMock(return_value={}),
    ),
    "metrics_facade": MagicMock(),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


@pytest.fixture(autouse=True)
def _no_agentcore_proxy_flag(monkeypatch):
    """Default to the container's own condition: the proxy flag absent.

    The proxy-path test sets it explicitly; every other test needs the inline path
    that actually reaches the emit.
    """
    monkeypatch.delenv("USE_AGENTCORE_RUNTIME", raising=False)


PIPELINE_ID = "pipe-stage-metrics"
RUN_ID = "run-stage-metrics"
EVENT = {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-stage-metrics"}
LOGGER_NAME = "agentic_data_ingestion"


def _happy_path_patches():
    """Context managers for a successful handler run (shape from test_duration_metrics.py)."""
    return [
        patch.object(_mod, "publish_pipeline_state_change"),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(
            _mod,
            "get_pipeline_metadata",
            return_value={
                "target_config": {
                    "type": "s3_tables",
                    "bucket_name": "b",
                    "namespace_name": "n",
                    "table_name": "t",
                }
            },
        ),
        patch.object(
            _mod,
            "pipeline_runs_table",
            MagicMock(get_item=MagicMock(return_value={"Item": {"extracted_schema": '{"properties": {}}'}})),
        ),
        patch.object(
            _mod,
            "mappings_table",
            MagicMock(
                get_item=MagicMock(
                    return_value={
                        "Item": {
                            "mapping_id": "m1",
                            "source_schema_hash": "src-hash-m1",
                            "target_schema_hash": "tgt-hash-m1",
                        }
                    }
                )
            ),
        ),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(_mod, "generate_code", return_value="# code\ndf = spark.read.json('s3://b/k')\n"),
        patch.object(_mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
    ]


def _failure_path_patches(error: Exception):
    """Context managers for a handler run that fails inside the try block.

    The read raises, so control reaches the failure-path emit at ``:1065-1091``
    while the handler is already unwinding.
    """
    return [
        patch.object(_mod, "publish_pipeline_state_change"),
        patch.object(_mod, "get_pipeline_metadata", side_effect=error),
        patch.object(_mod, "pipeline_runs_table", MagicMock()),
    ]


def _run_handler(patches):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return _mod.lambda_handler(EVENT, None)


def _stage_metric_warnings(caplog):
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "CodeGeneration" in r.getMessage() and "stage" in r.getMessage().lower()
    ]


# ── The emit fires on the path that performs the work (R6.6) ─────────────────


def test_success_path_emit_fires_when_the_agentcore_proxy_flag_is_absent():
    """With no proxy flag the handler runs inline and records CodeGeneration (R6.1, R6.6)."""
    stage_metrics = MagicMock()

    with patch.object(_mod, "PipelineMetrics", stage_metrics):
        result = _run_handler(_happy_path_patches())

    assert result["statusCode"] == 200
    assert stage_metrics.record_stage_execution.call_count == 1
    kwargs = stage_metrics.record_stage_execution.call_args.kwargs
    assert kwargs["stage_name"] == "CodeGeneration"
    assert kwargs["pipeline_id"] == PIPELINE_ID
    assert kwargs["status"] == "completed"
    assert isinstance(kwargs["duration_ms"], float)
    assert kwargs["duration_ms"] >= 0


def test_agentcore_proxy_path_returns_before_the_emit(monkeypatch, caplog):
    """With the proxy flag set the handler early-returns and never reaches the emit (R6.6).

    This is the other half of C5's split: the emit is reached only on the
    container's condition, which is the flag's absence. Asserting both halves is
    what makes the diagnosis verified rather than assumed.
    """
    proxy_response = {"statusCode": 200, "run_id": RUN_ID, "next_step": "code_testing"}
    proxy = MagicMock(return_value=proxy_response)
    stage_metrics = MagicMock()

    monkeypatch.setenv("USE_AGENTCORE_RUNTIME", "true")
    monkeypatch.setenv("AGENTCORE_CREATE_CODE_ENDPOINT", "https://example.invalid/runtimes/abc/invocations")

    with (
        patch.dict("sys.modules", {"agentcore_proxy": MagicMock(invoke_agentcore_runtime=proxy)}),
        patch.object(_mod, "PipelineMetrics", stage_metrics),
        patch.object(_mod, "get_pipeline_metadata") as metadata_read,
        caplog.at_level(logging.WARNING, logger=LOGGER_NAME),
    ):
        result = _mod.lambda_handler(EVENT, None)

    assert result == proxy_response
    proxy.assert_called_once_with(
        endpoint="https://example.invalid/runtimes/abc/invocations",
        payload=EVENT,
        step_name="create_code",
    )
    assert stage_metrics.record_stage_execution.call_count == 0
    assert metadata_read.call_count == 0, "the handler must return before doing any work"
    assert _stage_metric_warnings(caplog) == [], "an unreached emit must not report a failure either"


def test_failure_path_emit_fires_with_the_failed_status():
    """A failing run records the stage as failed and still fails the step (R6.1, R6.6)."""
    stage_metrics = MagicMock()

    with patch.object(_mod, "PipelineMetrics", stage_metrics), pytest.raises(_mod.StepFailure):
        _run_handler(_failure_path_patches(RuntimeError("simulated read failure")))

    assert stage_metrics.record_stage_execution.call_count == 1
    kwargs = stage_metrics.record_stage_execution.call_args.kwargs
    assert kwargs["stage_name"] == "CodeGeneration"
    assert kwargs["pipeline_id"] == PIPELINE_ID
    assert kwargs["status"] == "failed"


# ── A failed emission is observable, and never changes the step's outcome ────


def test_success_path_raising_emit_logs_at_warning_and_still_returns_200(caplog):
    """A raising emit is recorded with its traceback; the run still succeeds (R6.5).

    Nothing in this file made ``record_stage_execution`` raise before, so the
    ``except`` arm at ``:1013-1027`` was unverified.
    """
    stage_metrics = MagicMock()
    stage_metrics.record_stage_execution.side_effect = RuntimeError("emit refused")

    with patch.object(_mod, "PipelineMetrics", stage_metrics), caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        result = _run_handler(_happy_path_patches())

    assert result["statusCode"] == 200
    warnings = _stage_metric_warnings(caplog)
    assert len(warnings) == 1
    assert "(success path)" in warnings[0].getMessage()
    assert PIPELINE_ID in warnings[0].getMessage()
    assert warnings[0].exc_info is not None


def test_failure_path_raising_emit_does_not_displace_the_real_failure(caplog):
    """On the failure path a metrics problem must not become the recorded error (R6.5).

    The handler is already unwinding the exception it will re-raise, so the
    assertion is on the surviving message as well as on the WARNING.
    """
    stage_metrics = MagicMock()
    stage_metrics.record_stage_execution.side_effect = RuntimeError("emit refused")

    with (
        patch.object(_mod, "PipelineMetrics", stage_metrics),
        caplog.at_level(logging.WARNING, logger=LOGGER_NAME),
        pytest.raises(_mod.StepFailure) as exc_info,
    ):
        _run_handler(_failure_path_patches(RuntimeError("simulated read failure")))

    assert "simulated read failure" in exc_info.value.message
    assert "emit refused" not in exc_info.value.message

    warnings = _stage_metric_warnings(caplog)
    assert len(warnings) == 1
    assert "(failure path)" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None


# ── The import-guard branch is audible (R6.6) ───────────────────────────────


def test_success_path_reports_unavailable_pipeline_metrics(caplog):
    """``PipelineMetrics is None`` says so at WARNING instead of silently skipping.

    Design §4 C5 ruled this cause out for the AgentCore container, but the branch
    stays reachable if the Lambda layer ever loses the module — and an unreachable
    emit is indistinguishable from a working one without this line.
    """
    with patch.object(_mod, "PipelineMetrics", None), caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        result = _run_handler(_happy_path_patches())

    assert result["statusCode"] == 200
    warnings = _stage_metric_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "PipelineMetrics is unavailable" in message
    assert "(success path)" in message
    assert PIPELINE_ID in message


def test_failure_path_reports_unavailable_pipeline_metrics(caplog):
    """Same signal on the failure path, which has its own guard at ``:1065``."""
    with (
        patch.object(_mod, "PipelineMetrics", None),
        caplog.at_level(logging.WARNING, logger=LOGGER_NAME),
        pytest.raises(_mod.StepFailure),
    ):
        _run_handler(_failure_path_patches(RuntimeError("simulated read failure")))

    warnings = _stage_metric_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "PipelineMetrics is unavailable" in message
    assert "(failure path)" in message


# ── The two halves of C5's producer fix compose ──────────────────────────────


def test_emit_reaches_put_metric_data_through_the_real_producer(monkeypatch):
    """With the real producer, one handler run publishes the stage duration.

    The emit alone proves nothing about C5: before the flush landed,
    ``record_stage_execution`` buffered three metrics on the ``POWERTOOLS_ENABLED``
    unset path and ``put_direct_metric`` auto-flushed only at ``batch_size`` (20),
    so the container was reclaimed with the buffer full. This is the one test where
    both halves run, so ``put_metric_data`` being called is the composed claim.

    CloudWatch is a ``MagicMock`` on ``_cloudwatch``, the established pattern in
    ``tests/unit/shared/test_metrics_facade_flush.py`` — no boto3 client is
    constructed and no AWS call is made.
    """
    monkeypatch.setenv("POWERTOOLS_ENABLED", "false")
    sys.modules.pop("metrics_facade", None)
    try:
        import metrics_facade as real_facade

        assert real_facade.POWERTOOLS_AVAILABLE is False
        mock_cw = MagicMock()
        real_facade.metrics_facade._cloudwatch = mock_cw

        with patch.object(_mod, "PipelineMetrics", real_facade.PipelineMetrics):
            result = _run_handler(_happy_path_patches())

        assert result["statusCode"] == 200
        assert mock_cw.put_metric_data.call_count == 1

        kwargs = mock_cw.put_metric_data.call_args.kwargs
        assert kwargs["Namespace"] == "AgenticDataIngestion/Pipeline"
        published = {m["MetricName"]: m for m in kwargs["MetricData"]}
        assert "Pipeline.StageDuration" in published
        # AMENDED by p39 task 13.2, with its reason kept at the assertion.
        #
        # This asserted exactly `{StageName, PipelineId, Status}` — the shape the
        # emit produced once task 8.1's flush made it reach CloudWatch, and a
        # shape NO consumer can read. `_stage_durations_cw`
        # (`dashboard_api/handlers/metrics.py:1108`) searches a four-name brace
        # schema including `service`, which Powertools stamps on the EMF path and
        # the direct fallback did not; a brace schema is an EXACT match on the
        # dimension-NAME set, so a live probe with the consumer's own expression
        # returned zero series for `CodeGeneration` against 26 for
        # `SchemaExtract`. Task 13.2 adds `service` in `put_metric`'s Powertools
        # fallback so this path emits what the EMF path emits.
        #
        # The equality against the consumer's own schema — rather than against a
        # restated list — is asserted in
        # `tests/unit/shared/test_metrics_facade_service_dimension.py` (task 13.3).
        assert {d["Name"]: d["Value"] for d in published["Pipeline.StageDuration"]["Dimensions"]} == {
            "StageName": "CodeGeneration",
            "PipelineId": PIPELINE_ID,
            "Status": "completed",
            "service": "agentic_data_ingestion",
        }
    finally:
        # The 14 files above bind ``PipelineMetrics`` at import time from their own
        # stub, so leaving a real module in the cache would be a cross-file effect.
        sys.modules.pop("metrics_facade", None)
