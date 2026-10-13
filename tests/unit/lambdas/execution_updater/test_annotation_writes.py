# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: writes that are not run progress never move a run's executions row.

Observed on pipeline ``newpipeoctnine`` on 2026-10-09: the run
``run-2026-10-09-20-25-21-bb9c98a1`` was rejected at the mapping gate and its
executions row read FAILED. A minute later the user regenerated the mapping from
the Schema tab. The regeneration wrote ``(run_id, schema_mapping)`` with
``status="success"``, this handler derived RUNNING from it (``schema_mapping``
is not a final step), and the rejected run showed as running again.

Requirements these derive from:

* a regeneration or a reuse revocation (both stamp ``annotated_at``, see
  ``shared/mapping_reuse.py``) writes nothing to the executions row;
* an approval decision that lands after an annotation is still processed;
* a ``rejected`` step status ends the run: FAILED, not RUNNING.

The handler is loaded per test by path (``LGR-b7d5490-1``) with a ``MagicMock``
executions table, because the claim is about which writes the handler *emits*.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_execution_updater"
    )
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

_ENV = {
    "EXECUTIONS_TABLE": "test-executions",
    "PIPELINES_TABLE": "test-pipelines",
    "ERRORS_TABLE": "test-errors",
    "STATE_TABLE": "test-state",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

PIPELINE_ID = "newpipeoctnine-dff57b5f"
RUN_ID = "run-2026-10-09-20-25-21-bb9c98a1"


def _load_handler():
    mock_table = MagicMock()
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    with (
        patch.dict(os.environ, _ENV),
        patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
        patch("boto3.resource", return_value=mock_dynamo),
    ):
        sys.modules.pop("lambda_function", None)
        if SHARED_DIR not in sys.path:
            sys.path.insert(0, SHARED_DIR)
        spec = importlib.util.spec_from_file_location("lambda_function", os.path.join(LAMBDA_DIR, "lambda_function.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, mock_table


def _record(*, event_name: str = "MODIFY", step_name: str = "schema_mapping", **attrs: str) -> dict:
    image = {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "step_name": step_name, **attrs}
    return {
        "eventID": f"evt-{step_name}-{attrs.get('status', '')}",
        "eventName": event_name,
        "dynamodb": {"NewImage": {k: {"S": v} for k, v in image.items()}},
    }


def _run(record: dict):
    updater, table = _load_handler()
    updater.lambda_handler({"Records": [record]}, None)
    return updater, table


def _status_writes(table) -> list[str]:
    return [
        call.kwargs["ExpressionAttributeValues"][":status"]
        for call in table.update_item.call_args_list
        if ":status" in call.kwargs.get("ExpressionAttributeValues", {})
    ]


class TestARegenerationDoesNotMoveTheRun:
    def test_the_observed_regeneration_writes_nothing(self):
        """The 2026-10-09 record: an INSERT on a rejected reuse run, ``success``."""
        stamp = "2026-10-09T20:32:38.004210+00:00"
        _, table = _run(_record(event_name="INSERT", status="success", timestamp=stamp, annotated_at=stamp))

        table.update_item.assert_not_called()

    def test_a_regeneration_over_an_existing_row_writes_nothing(self):
        stamp = "2026-10-09T18:43:02.531245+00:00"
        _, table = _run(_record(status="waiting_approval", timestamp=stamp, annotated_at=stamp))

        table.update_item.assert_not_called()

    def test_a_follow_up_update_that_moves_no_stamp_is_still_ignored(self):
        """The complex-mapping agent and the SFN-result cache both update the row
        after a regeneration without touching ``timestamp`` or ``updated_at``."""
        stamp = "2026-10-09T20:32:38+00:00"
        _, table = _run(_record(status="success", timestamp=stamp, annotated_at=stamp, evidence="{}"))

        table.update_item.assert_not_called()


class TestARevocationDoesNotMoveTheRun:
    def test_a_revocation_on_an_earlier_succeeded_run_writes_nothing(self):
        _, table = _run(
            _record(
                status="success",
                timestamp="2026-10-09T18:43:02+00:00",
                reuse_revoked_at="2026-10-09T21:00:00+00:00",
                annotated_at="2026-10-09T21:00:00+00:00",
            )
        )

        table.update_item.assert_not_called()


class TestADecisionAfterAnAnnotationIsProcessed:
    def test_an_approval_after_a_regeneration_resumes_the_run(self):
        _, table = _run(
            _record(
                status="success",
                timestamp="2026-10-09T18:43:02+00:00",
                annotated_at="2026-10-09T18:43:02+00:00",
                updated_at="2026-10-09T18:50:00+00:00",
            )
        )

        assert _status_writes(table) == ["RUNNING"]

    def test_a_rejection_after_a_regeneration_fails_the_run(self):
        _, table = _run(
            _record(
                status="rejected",
                timestamp="2026-10-09T18:43:02+00:00",
                annotated_at="2026-10-09T18:43:02+00:00",
                updated_at="2026-10-09T20:16:42+00:00",
            )
        )

        assert _status_writes(table) == ["FAILED"]


class TestARejectionEndsTheRun:
    def test_rejected_derives_failed(self):
        updater, _ = _load_handler()

        assert updater._derive_execution_status("schema_mapping", "rejected") == "FAILED"
        assert updater._derive_execution_status("create_code", "REJECTED") == "FAILED"

    def test_the_step_keeps_its_own_rejected_label(self):
        """``steps_status`` still says what happened to the step."""
        _, table = _run(_record(status="rejected", timestamp="2026-10-09T20:31:31+00:00"))

        step_writes = [
            call.kwargs["ExpressionAttributeValues"][":step_status"]
            for call in table.update_item.call_args_list
            if ":step_status" in call.kwargs.get("ExpressionAttributeValues", {})
        ]
        assert step_writes == ["REJECTED"]


class TestOrdinaryStepRowsAreUnaffected:
    def test_a_pipeline_mapping_row_still_drives_the_run(self):
        _, table = _run(_record(event_name="INSERT", status="success", timestamp="2026-10-09T18:41:53+00:00"))

        assert _status_writes(table) == ["RUNNING"]

    def test_an_unparseable_annotation_falls_back_to_processing(self):
        _, table = _run(_record(status="success", timestamp="2026-10-09T18:41:53+00:00", annotated_at="garbage"))

        assert _status_writes(table) == ["RUNNING"]
