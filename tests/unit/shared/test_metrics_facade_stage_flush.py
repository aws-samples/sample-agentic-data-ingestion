# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the ``flush()`` inside ``PipelineMetrics.record_stage_execution``
(p39 C5, R6.5/R6.6).

Why this exists. ``record_stage_execution`` buffers three metrics per call
(``metrics_facade.py:407-415``) and, where ``POWERTOOLS_ENABLED`` is unset,
``put_metric`` falls through to ``put_direct_metric``, which auto-flushes only at
``batch_size`` (20). Every AgentCore container is on that path — ``agentContainerEnv``
sets no ``POWERTOOLS_ENABLED`` — so the microVM was reclaimed with the buffer still
full: nothing published, nothing logged. That is why ``cloudwatch list-metrics``
showed no ``CodeGeneration`` ``Pipeline.StageDuration`` series at all.

Expectations here derive from R6.5/R6.6 — a stage that ran must publish, and a
failed emission must be recorded at WARNING — not from the pre-fix behaviour,
which was silence.

``flush()`` itself is unchanged and still delegates to ``flush_direct()`` with no
Powertools branch (``LGR-eee8ac8-7``, template / ratified); these tests assert the
call, never a second flush implementation.

CloudWatch is stubbed with ``MagicMock`` on ``_cloudwatch``, matching
``test_metrics_facade_flush.py`` and ``test_metrics_facade_props.py`` — the shared
fixtures in ``tests/fixtures/conftest.py`` cover DynamoDB, S3 and SQS only.
``AWS_REGION`` / ``AWS_DEFAULT_REGION`` come from ``tests/unit/conftest.py``
(``LGR-3e45e65-1``) and are deliberately not set here.

Validates: Requirements R6.5, R6.6
"""

import logging
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)


def _reload_metrics_facade(monkeypatch, enabled: str):
    """Force-reload ``metrics_facade`` with the given POWERTOOLS_ENABLED value.

    The module resolves the flag at import time (``metrics_facade.py:38-46``), so
    both paths are only reachable by dropping it from the module cache and
    importing again. Same helper as ``test_metrics_facade_flush.py:32-45``.
    """
    monkeypatch.setenv("POWERTOOLS_ENABLED", enabled)
    sys.modules.pop("metrics_facade", None)

    import metrics_facade

    return metrics_facade


@pytest.fixture(autouse=True)
def _drop_reloaded_module():
    """Evict the reloaded module afterwards so no flag state leaks to other files."""
    yield
    sys.modules.pop("metrics_facade", None)


STAGE = "CodeGeneration"
PIPELINE = "pipe-stage-flush"


def test_record_stage_execution_flushes_on_the_buffered_path(monkeypatch):
    """On POWERTOOLS_ENABLED=false the three buffered metrics are published now (R6.5).

    The assertion is on ``put_metric_data`` rather than on ``buffer_length``,
    because an emptied buffer with no API call is exactly the silent-drop the
    microVM reclaim produced.
    """
    mod = _reload_metrics_facade(monkeypatch, "false")
    assert mod.POWERTOOLS_AVAILABLE is False

    mock_cw = MagicMock()
    mod.metrics_facade._cloudwatch = mock_cw

    mod.PipelineMetrics.record_stage_execution(
        stage_name=STAGE,
        pipeline_id=PIPELINE,
        status="completed",
        duration_ms=1234.5,
    )

    assert mock_cw.put_metric_data.call_count == 1
    kwargs = mock_cw.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == "AgenticDataIngestion/Pipeline"

    published = {m["MetricName"]: m for m in kwargs["MetricData"]}
    assert set(published) == {
        "Pipeline.StageExecution",
        "Pipeline.StageDuration",
        "Pipeline.StageSuccessRate",
    }

    duration = published["Pipeline.StageDuration"]
    assert duration["Value"] == 1234.5
    assert duration["Unit"] == "Milliseconds"
    # AMENDED by p39 task 13.2, with its reason kept at the assertion.
    #
    # This asserted exactly `{StageName, PipelineId, Status}` — the shape task
    # 8.1's flush made reach CloudWatch, and the shape NO consumer can read.
    # `_stage_durations_cw` searches a four-name brace schema including
    # `service`, and a brace schema is an exact match on the dimension-NAME set,
    # so the three-name shape returned zero series for `CodeGeneration` against
    # 26 for `SchemaExtract` on a live probe. Task 13.2 adds `service` in
    # `put_metric`'s Powertools fallback so the container path emits what the
    # EMF path emits.
    #
    # The name-set equality against the consumer's own schema is asserted in
    # `test_metrics_facade_service_dimension.py` (task 13.3); this file keeps
    # asserting the three stage dimensions and their VALUES, which is what it
    # was written for.
    assert {d["Name"]: d["Value"] for d in duration["Dimensions"]} == {
        "StageName": STAGE,
        "PipelineId": PIPELINE,
        "Status": "completed",
        "service": "agentic_data_ingestion",
    }

    assert mod.metrics_facade.buffer_length == 0, "flush() should leave the buffer empty"


def test_record_stage_execution_makes_no_api_call_on_the_emf_path(monkeypatch, caplog):
    """On the EMF path the direct buffer is empty, so the flush is a no-op.

    ``_cloudwatch`` staying ``None`` proves ``_get_cloudwatch()`` was never
    reached, so no boto3 client was constructed either — the same evidence
    ``test_flush_with_empty_buffer_makes_no_api_call`` uses.

    The ERROR-log assertion is load-bearing: ``put_metric`` catches its own
    exceptions and logs at ERROR (``metrics_facade.py:175-176``), so a broken EMF
    emission would also leave the buffer empty and satisfy the no-API-call
    assertion for the wrong reason.
    """
    mod = _reload_metrics_facade(monkeypatch, "true")
    assert mod.POWERTOOLS_AVAILABLE is True

    with caplog.at_level(logging.WARNING, logger="metrics_facade"):
        mod.PipelineMetrics.record_stage_execution(
            stage_name=STAGE,
            pipeline_id=PIPELINE,
            status="completed",
            duration_ms=99.0,
        )

    assert mod.metrics_facade._cloudwatch is None
    assert mod.metrics_facade.buffer_length == 0
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_raising_flush_logs_at_warning_and_does_not_propagate(monkeypatch, caplog):
    """A failed flush is recorded, never raised (R6.5, requirements constraint 8).

    A stage-duration emission is telemetry; losing it must not fail the pipeline
    step. What is not acceptable is losing it with no trace, which is the shape
    the swallowing ``except`` this replaces had.
    """
    mod = _reload_metrics_facade(monkeypatch, "false")

    def _raising_flush():
        raise RuntimeError("put_metric_data refused")

    monkeypatch.setattr(mod.metrics_facade, "flush", _raising_flush)

    with caplog.at_level(logging.WARNING, logger="metrics_facade"):
        mod.PipelineMetrics.record_stage_execution(
            stage_name=STAGE,
            pipeline_id=PIPELINE,
            status="failed",
            duration_ms=7.0,
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert STAGE in message
    assert PIPELINE in message
    assert "failed" in message
    assert warnings[0].exc_info is not None, "the traceback is what makes a recurrence diagnosable"
