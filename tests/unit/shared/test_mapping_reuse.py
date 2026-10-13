# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: ``shared/mapping_reuse`` — the shared reuse predicate and its revocation.

The defect these pin, as the user reported it on pipeline ``newpipeoctnine``:

1. A mapping rejected at the gate was proposed again by the next run. The
   rejection never reached the state row, and the row read ``success``.
2. Regenerating a mapping on a finished run put that run back to RUNNING.

Expectations come from those two requirements: a refused mapping is never a
reuse candidate, and a write that is not progress of a run does not move it.
The predicate is exercised against moto's DynamoDB through the shared
``mock_dynamodb_tables`` fixture, so the filter semantics are DynamoDB's own
rather than a restatement of the condition tree.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

import mapping_reuse as mr

PIPELINE = "pipe-reuse"
OTHER_PIPELINE = "pipe-elsewhere"
REJECTED_MAPPING = "mapping-hash-rejected"
OTHER_MAPPING = "mapping-hash-other"
APPROVAL = "pipe-reuse_schema_mapping_1791577793"


def _row(run_id: str, *, mapping_id: str, status: str | None, pipeline_id: str = PIPELINE, **extra) -> dict:
    item = {
        "run_id": run_id,
        "step_name": "schema_mapping",
        "pipeline_id": pipeline_id,
        "timestamp": f"2026-10-09T20:{run_id[-2:]}:00+00:00",
        "mapping_id": mapping_id,
        **extra,
    }
    if status is not None:
        item["status"] = status
    return item


@pytest.fixture
def state_table(mock_dynamodb_tables):
    return mock_dynamodb_tables["agentic_data_ingestion_state"]


def _approved_run_ids(table, pipeline_id: str = PIPELINE) -> set[str]:
    response = table.query(
        IndexName="PipelineIndex",
        KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
        FilterExpression=Attr("step_name").eq("schema_mapping") & mr.approved_for_reuse_condition(),
    )
    return {item["run_id"] for item in response["Items"]}


# --- The shared predicate -------------------------------------------------------


class TestApprovedForReuse:
    def test_only_an_approved_unrevoked_row_qualifies(self, state_table):
        state_table.put_item(Item=_row("run-01", mapping_id=OTHER_MAPPING, status="success"))
        state_table.put_item(
            Item=_row("run-02", mapping_id=REJECTED_MAPPING, status="success", reuse_revoked_at="2026-10-09T21:00:00")
        )
        state_table.put_item(Item=_row("run-03", mapping_id=REJECTED_MAPPING, status="rejected"))
        state_table.put_item(Item=_row("run-04", mapping_id=REJECTED_MAPPING, status="waiting_approval"))
        state_table.put_item(Item=_row("run-05", mapping_id=REJECTED_MAPPING, status=None))

        assert _approved_run_ids(state_table) == {"run-01"}

    def test_a_row_with_no_status_never_qualifies(self, state_table):
        """LGR-5d68eeb-2: no permissive arm for an absent status."""
        state_table.put_item(Item=_row("run-06", mapping_id=OTHER_MAPPING, status=None))

        assert _approved_run_ids(state_table) == set()


# --- What a regeneration writes ---------------------------------------------------


class TestStatusAfterRegeneration:
    @pytest.mark.parametrize(
        ("existing", "expected"),
        [
            (None, "success"),
            ({"status": "waiting_approval", "mapping_id": OTHER_MAPPING}, "waiting_approval"),
            ({"status": "waiting_approval", "mapping_id": REJECTED_MAPPING}, "waiting_approval"),
            ({"status": "rejected", "mapping_id": REJECTED_MAPPING}, "rejected"),
            ({"status": "rejected", "mapping_id": OTHER_MAPPING}, "success"),
            ({"status": "success", "mapping_id": OTHER_MAPPING}, "success"),
            ({"status": "failed"}, "success"),
        ],
    )
    def test_the_status_written(self, existing, expected):
        assert mr.status_after_regeneration(existing, REJECTED_MAPPING) == expected

    def test_a_row_at_the_gate_is_never_presented_as_approved(self):
        """The run is still waiting for a human; ``success`` would read as approved."""
        assert mr.status_after_regeneration({"status": "waiting_approval"}, "anything") != "success"

    def test_refused_content_stays_refused(self):
        """``mapping_id`` is a content hash, so the same id is the same content."""
        assert mr.status_after_regeneration({"status": "rejected", "mapping_id": "m"}, "m") == "rejected"


class TestRevocationKeptByRegeneration:
    def test_the_same_revoked_content_keeps_its_revocation(self):
        existing = {
            "mapping_id": REJECTED_MAPPING,
            mr.REUSE_REVOKED_AT: "2026-10-09T21:00:00+00:00",
            mr.REUSE_REVOKED_BY_APPROVAL: APPROVAL,
        }

        assert mr.revocation_kept_by_regeneration(existing, REJECTED_MAPPING) == {
            mr.REUSE_REVOKED_AT: "2026-10-09T21:00:00+00:00",
            mr.REUSE_REVOKED_BY_APPROVAL: APPROVAL,
        }

    def test_new_content_starts_unrevoked(self):
        existing = {"mapping_id": REJECTED_MAPPING, mr.REUSE_REVOKED_AT: "2026-10-09T21:00:00+00:00"}

        assert mr.revocation_kept_by_regeneration(existing, OTHER_MAPPING) == {}

    @pytest.mark.parametrize("existing", [None, {"mapping_id": REJECTED_MAPPING, "status": "success"}])
    def test_nothing_to_carry(self, existing):
        assert mr.revocation_kept_by_regeneration(existing, REJECTED_MAPPING) == {}


# --- Which writes the executions row ignores ----------------------------------------


class TestAnnotationIsLatestWrite:
    def test_a_regeneration_put_is_its_own_latest_write(self):
        """The regeneration stamps ``annotated_at == timestamp``."""
        stamp = "2026-10-09T20:32:38.004210+00:00"
        assert mr.annotation_is_latest_write(stamp, stamp, "") is True

    def test_a_revocation_is_newer_than_the_row_it_annotates(self):
        assert (
            mr.annotation_is_latest_write(
                "2026-10-09T21:00:00+00:00", "2026-10-09T18:43:02+00:00", "2026-10-09T20:16:42+00:00"
            )
            is True
        )

    def test_a_decision_after_the_annotation_is_processed(self):
        """An approve or reject echo sets a newer ``updated_at``."""
        assert (
            mr.annotation_is_latest_write(
                "2026-10-09T20:32:38+00:00", "2026-10-09T20:32:38+00:00", "2026-10-09T20:40:00+00:00"
            )
            is False
        )

    def test_a_row_never_annotated_is_processed(self):
        assert mr.annotation_is_latest_write("", "2026-10-09T20:32:38+00:00", "") is False

    def test_an_unparseable_stamp_fails_toward_processing(self):
        assert mr.annotation_is_latest_write("not-a-time", "2026-10-09T20:32:38+00:00") is False
        assert mr.annotation_is_latest_write("2026-10-09T20:32:38+00:00", "not-a-time") is False

    def test_naive_and_aware_stamps_compare_by_instant(self):
        assert mr.annotation_is_latest_write("2026-10-09T20:32:38+00:00", "2026-10-09T20:32:37") is True
        assert mr.annotation_is_latest_write("2026-10-09T20:32:38", "2026-10-09T20:32:39+00:00") is False


# --- Revocation ---------------------------------------------------------------------


class TestRevokeMappingReuse:
    def test_only_this_pipelines_approved_rows_holding_the_mapping_are_revoked(self, state_table):
        state_table.put_item(Item=_row("run-11", mapping_id=REJECTED_MAPPING, status="success"))
        state_table.put_item(Item=_row("run-12", mapping_id=REJECTED_MAPPING, status="rejected"))
        state_table.put_item(Item=_row("run-13", mapping_id=OTHER_MAPPING, status="success"))
        state_table.put_item(
            Item=_row("run-14", mapping_id=REJECTED_MAPPING, status="success", pipeline_id=OTHER_PIPELINE)
        )

        revoked = mr.revoke_mapping_reuse(
            state_table, PIPELINE, REJECTED_MAPPING, approval_id=APPROVAL, now="2026-10-09T21:00:00+00:00"
        )

        assert revoked == 1
        assert _approved_run_ids(state_table) == {"run-13"}
        assert _approved_run_ids(state_table, OTHER_PIPELINE) == {"run-14"}

    def test_the_revoked_row_keeps_its_status_and_records_who_revoked_it(self, state_table):
        """The row is the earlier run's history; only reuse is withdrawn."""
        state_table.put_item(Item=_row("run-21", mapping_id=REJECTED_MAPPING, status="success"))

        mr.revoke_mapping_reuse(
            state_table, PIPELINE, REJECTED_MAPPING, approval_id=APPROVAL, now="2026-10-09T21:00:00+00:00"
        )

        item = state_table.get_item(Key={"run_id": "run-21", "step_name": "schema_mapping"})["Item"]
        assert item["status"] == "success"
        assert item[mr.REUSE_REVOKED_AT] == "2026-10-09T21:00:00+00:00"
        assert item[mr.REUSE_REVOKED_BY_APPROVAL] == APPROVAL
        assert item[mr.ANNOTATED_AT] == "2026-10-09T21:00:00+00:00"

    def test_the_revocation_is_an_annotation_the_executions_row_ignores(self, state_table):
        state_table.put_item(Item=_row("run-31", mapping_id=REJECTED_MAPPING, status="success"))

        mr.revoke_mapping_reuse(state_table, PIPELINE, REJECTED_MAPPING, approval_id=APPROVAL)

        item = state_table.get_item(Key={"run_id": "run-31", "step_name": "schema_mapping"})["Item"]
        assert mr.annotation_is_latest_write(item[mr.ANNOTATED_AT], item["timestamp"], item.get("updated_at", ""))

    def test_nothing_to_revoke_is_not_an_error(self, state_table):
        assert mr.revoke_mapping_reuse(state_table, PIPELINE, REJECTED_MAPPING, approval_id=APPROVAL) == 0

    def test_content_replaced_since_the_query_is_left_alone(self):
        """A regeneration between query and update keeps its new content reusable."""
        table = MagicMock()
        table.query.return_value = {"Items": [{"run_id": "run-41"}]}
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "changed"}}, "UpdateItem"
        )

        assert mr.revoke_mapping_reuse(table, PIPELINE, REJECTED_MAPPING, approval_id=APPROVAL) == 0
        condition = table.update_item.call_args.kwargs["ConditionExpression"]
        assert condition == "attribute_exists(run_id) AND mapping_id = :mapping_id"
        assert table.update_item.call_args.kwargs["ExpressionAttributeValues"][":mapping_id"] == REJECTED_MAPPING

    def test_any_other_error_reaches_the_caller(self):
        table = MagicMock()
        table.query.return_value = {"Items": [{"run_id": "run-42"}]}
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "UpdateItem"
        )

        with pytest.raises(ClientError):
            mr.revoke_mapping_reuse(table, PIPELINE, REJECTED_MAPPING, approval_id=APPROVAL)

    def test_an_empty_filtered_page_does_not_end_the_walk(self):
        """FilterExpression applies after the page read (LGR-eee8ac8-3)."""
        table = MagicMock()
        table.query.side_effect = [
            {"Items": [], "LastEvaluatedKey": {"run_id": "page-1"}},
            {"Items": [{"run_id": "run-51"}]},
        ]

        assert mr.revoke_mapping_reuse(table, PIPELINE, REJECTED_MAPPING, approval_id=APPROVAL) == 1
        assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"run_id": "page-1"}

    def test_the_walk_is_bounded(self):
        table = MagicMock()
        table.query.return_value = {"Items": [], "LastEvaluatedKey": {"run_id": "more"}}

        assert mr.revoke_mapping_reuse(table, PIPELINE, REJECTED_MAPPING, approval_id=APPROVAL, max_pages=3) == 0
        assert table.query.call_count == 3
