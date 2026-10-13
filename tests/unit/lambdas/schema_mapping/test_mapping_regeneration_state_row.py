# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: a mapping regeneration does not rewrite the run it lands on.

``POST /api/rerun-mapping`` async-invokes this Lambda with ``force_remap`` and the
run_id of whichever run last extracted a schema. That run may be finished, or
waiting at the approval gate. Observed on pipeline ``newpipeoctnine`` on
2026-10-09:

* run ``run-2026-10-09-18-39-09-2af368a0`` was waiting at the gate; a
  regeneration rewrote its row from ``waiting_approval`` to ``success``; the
  human then rejected it, and the next run reused the rejected mapping;
* a regeneration on the rejected run ``run-2026-10-09-20-25-21-bb9c98a1`` put
  that FAILED run back to RUNNING.

Expectations come from those requirements, not from the writer's output: the
row keeps an undecided or refused status, the write is marked as an annotation
the executions row ignores, and a regeneration failure leaves the row alone.
The conditional put runs against moto through the shared
``mock_dynamodb_tables`` fixture.
"""

import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Lambda-layer-only dependencies, stubbed exactly as the sibling files here do.
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

# `lambda_function` is a name many Lambda dirs share; load this one by path order
# and restore the env_utils stub afterwards (LGR-c58f276-4).
sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _mod
finally:
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

from mapping_reuse import ANNOTATED_AT, REUSE_REVOKED_AT, REUSE_REVOKED_BY_APPROVAL, annotation_is_latest_write

_RUN_ID = "run-2026-10-09-20-25-21-bb9c98a1"
_PIPELINE = "newpipeoctnine-dff57b5f"
_OLD_MAPPING = "mapping-hash-3688d5e92445445c226cfa75271003e2"
_NEW_MAPPING = "mapping-hash-ef67fd7c23ae97a487b10b79587055f5"
_KEY = {"run_id": _RUN_ID, "step_name": "schema_mapping"}


def _mapping_result() -> dict:
    return {
        "mappings": [
            {"source_field": "first_name", "target_field": "first_name", "confidence": 0.95},
            {"source_field": "created_at", "target_field": "registration_date", "confidence": 0.9},
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "generated_by": "anthropic.claude-sonnet-5-5",
    }


def _store(table, mapping_id: str, *, regeneration: bool) -> None:
    with (
        patch.object(_mod, "table", table),
        patch.object(_mod, "create_approval_payload", return_value={"approval_type": "schema_mapping"}),
        patch.object(_mod, "create_output_data", return_value={"artifact_type": "schema_mapping"}),
        patch.object(_mod, "convert_floats_to_decimal", side_effect=lambda value: value),
    ):
        _mod.store_mapping_result(
            _RUN_ID,
            _PIPELINE,
            mapping_id,
            _mapping_result(),
            "src-hash",
            {"properties": {}},
            {"properties": {}},
            "tgt-hash",
            None,
            False,
            regeneration=regeneration,
        )


@pytest.fixture
def state_table(mock_dynamodb_tables):
    return mock_dynamodb_tables["agentic_data_ingestion_state"]


def _seed(table, **attrs) -> None:
    table.put_item(
        Item={
            **_KEY,
            "pipeline_id": _PIPELINE,
            "timestamp": "2026-10-09T18:43:02.531245+00:00",
            "mapping_count": Decimal(4),
            **attrs,
        }
    )


def _row(table) -> dict:
    return table.get_item(Key=_KEY)["Item"]


class TestTheStatusARegenerationWrites:
    def test_a_run_waiting_at_the_gate_stays_waiting(self, state_table):
        _seed(state_table, status="waiting_approval", mapping_id=_OLD_MAPPING)

        _store(state_table, _NEW_MAPPING, regeneration=True)

        row = _row(state_table)
        assert row["status"] == "waiting_approval"
        assert row["mapping_id"] == _NEW_MAPPING

    def test_refused_content_stays_refused(self, state_table):
        _seed(state_table, status="rejected", mapping_id=_OLD_MAPPING)

        _store(state_table, _OLD_MAPPING, regeneration=True)

        assert _row(state_table)["status"] == "rejected"

    def test_new_content_on_a_rejected_run_is_offered_to_the_next_run(self, state_table):
        """It still passes the gate when reused (LGR-5d68eeb-1)."""
        _seed(state_table, status="rejected", mapping_id=_OLD_MAPPING)

        _store(state_table, _NEW_MAPPING, regeneration=True)

        row = _row(state_table)
        assert row["status"] == "success"
        assert row["mapping_id"] == _NEW_MAPPING

    def test_a_run_without_a_mapping_row_gets_one(self, state_table):
        """A reuse run writes no mapping row, so the regeneration creates it."""
        _store(state_table, _NEW_MAPPING, regeneration=True)

        row = _row(state_table)
        assert row["status"] == "success"
        assert row["mapping_id"] == _NEW_MAPPING


class TestRevocationSurvivesTheSameContent:
    def test_the_same_revoked_content_stays_revoked(self, state_table):
        _seed(
            state_table,
            status="success",
            mapping_id=_OLD_MAPPING,
            **{REUSE_REVOKED_AT: "2026-10-09T20:31:31+00:00", REUSE_REVOKED_BY_APPROVAL: "approval-x"},
        )

        _store(state_table, _OLD_MAPPING, regeneration=True)

        row = _row(state_table)
        assert row[REUSE_REVOKED_AT] == "2026-10-09T20:31:31+00:00"
        assert row[REUSE_REVOKED_BY_APPROVAL] == "approval-x"

    def test_new_content_is_not_revoked(self, state_table):
        _seed(state_table, status="success", mapping_id=_OLD_MAPPING, **{REUSE_REVOKED_AT: "2026-10-09T20:31:31"})

        _store(state_table, _NEW_MAPPING, regeneration=True)

        assert REUSE_REVOKED_AT not in _row(state_table)


class TestTheExecutionsRowIgnoresTheWrite:
    def test_the_regenerated_row_is_an_annotation(self, state_table):
        _seed(state_table, status="rejected", mapping_id=_OLD_MAPPING, updated_at="2026-10-09T20:31:31+00:00")

        _store(state_table, _NEW_MAPPING, regeneration=True)

        row = _row(state_table)
        assert row[ANNOTATED_AT] == row["timestamp"]
        assert annotation_is_latest_write(row[ANNOTATED_AT], row["timestamp"], row.get("updated_at", ""))

    def test_the_pipeline_path_is_not_an_annotation(self, state_table):
        _store(state_table, _NEW_MAPPING, regeneration=False)

        row = _row(state_table)
        assert ANNOTATED_AT not in row
        assert row["status"] == "success"


class TestTheReadAndPutAreAPair:
    def test_a_conflicting_write_is_re_read_not_overwritten(self):
        """A decision landing between read and put is honoured on the retry."""
        table = MagicMock()
        table.get_item.side_effect = [
            {"Item": {**_KEY, "status": "success", "mapping_id": _OLD_MAPPING}},
            {"Item": {**_KEY, "status": "waiting_approval", "mapping_id": _OLD_MAPPING}},
        ]
        conflict = ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")
        table.put_item.side_effect = [conflict, None]

        _store(table, _NEW_MAPPING, regeneration=True)

        assert table.get_item.call_count == 2
        assert table.put_item.call_args.kwargs["Item"]["status"] == "waiting_approval"

    def test_a_row_that_keeps_changing_fails_the_regeneration(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {**_KEY, "status": "success", "mapping_id": _OLD_MAPPING}}
        table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem"
        )

        with pytest.raises(RuntimeError, match="kept changing"):
            _store(table, _NEW_MAPPING, regeneration=True)


class TestARegenerationFailureLeavesTheRowAlone:
    """The failure path's failed-status put would destroy the run's mapping row."""

    def _run_failing_handler(self, *, force_remap: bool) -> MagicMock:
        table = MagicMock()
        event = {"run_id": _RUN_ID, "pipeline_id": _PIPELINE, "source_schema_hash": "src-hash"}
        if force_remap:
            event["force_remap"] = True
        with (
            patch.object(_mod, "table", table),
            patch.object(_mod, "get_source_schema", side_effect=RuntimeError("bedrock unavailable")),
            patch.object(_mod, "publish_pipeline_state_change"),
            pytest.raises(_mod.StepFailure),
        ):
            _mod.lambda_handler(event, None)
        return table

    def test_a_failed_regeneration_writes_no_state_row(self):
        table = self._run_failing_handler(force_remap=True)

        table.put_item.assert_not_called()

    def test_a_failed_pipeline_step_still_writes_its_failed_row(self):
        table = self._run_failing_handler(force_remap=False)

        item = table.put_item.call_args.kwargs["Item"]
        assert item["status"] == "failed"
        assert item["run_id"] == _RUN_ID
