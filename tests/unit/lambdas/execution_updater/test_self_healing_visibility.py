# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A heal is visible in the pipeline run list (p30 task 2.5).

R14.3 requires a heal that ran to be indicated in the pipeline run list. The
run list reads the executions table, which is built from the state table's
DynamoDB stream by this Lambda — so this is where a heal becomes visible there.

Two things are covered, and the first is a regression guard rather than a new
feature:

1. **The self-healing outcome record must not drive the run's status.** The
   record keys on the ORIGINAL run's ``run_id`` and carries
   ``original_pipeline_id`` (which ``_process_record`` resolves in preference to
   ``pipeline_id``), so it lands on that run's execution row. It carries no
   ``status`` attribute at all, so before task 2.5 the step machinery derived
   ``RUNNING`` from it: a healed FAILED run showed up in the run list as
   "RUNNING → self_healing" with ``steps_status.self_healing = UNKNOWN``. The
   run list did change when a heal happened — it lied.

2. **The four heal states stay distinct.** ``rerun_status`` is copied through
   verbatim, never reduced to a boolean, because "a heal was attempted", "a
   re-run is in flight", "the re-run succeeded" and "the guard declined" are
   four different facts and three of them would be misreported by one "healed"
   flag.

Plus the run's provenance: ``trigger`` and ``original_run_id`` from the
``file_validation`` identity row, so a heal re-run is identifiable as one in
the list instead of looking like an ordinary run.

The notification half of R14.3 lives in
tests/unit/shared/test_self_healing_applied_notification.py (content) and
tests/unit/agents/self_healing_agent/test_heal_applied_notification.py
(wiring). The Partial_Write_Guard's own behaviour is task 6.5 and is not
touched here.

**Validates: Requirements 14.3**
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_execution_updater",
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

PIPELINE_ID = "pipe-001"
ORIGINAL_RUN_ID = "run-2026-08-02-abcd1234"
HEAL_RERUN_RUN_ID = "run-2026-08-02-efgh5678"


def _load_handler():
    """Load execution_updater with boto3 patched, isolated per call.

    The importlib load (rather than a plain import) follows LGR-b7d5490-1:
    every pipeline-Lambda unit test in this tree loads its module by path after
    popping ``lambda_function``, because the whole unit layer runs in one pytest
    process and ``lambda_function`` is a name 20+ Lambdas share.
    """
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


def _image(**attrs) -> dict:
    """Build a NEW_IMAGE from plain string attributes."""
    return {k: {"S": v} for k, v in attrs.items() if v is not None}


def _healing_record(
    *,
    rerun_status: str = "running",
    remediation: str = "Upgraded worker type from G.1X to G.2X",
    error_classification: str = "oom_error",
    linked_run_id: str | None = None,
    skip_reason: str | None = None,
    original_pipeline_id: str | None = PIPELINE_ID,
    event_name: str = "INSERT",
) -> dict:
    """A stream record shaped exactly like ``build_outcome_record``'s output.

    Transcribed from ``agentcore/self_healing_agent/main.py`` rather than
    generated from it: per LGR-28b4d75-5, a fixture derived from the producer
    cannot detect the producer drifting. Note the ABSENCE of a ``status``
    attribute — that absence is what used to derive RUNNING.
    """
    return {
        "eventID": f"evt-self_healing-{rerun_status}",
        "eventName": event_name,
        "dynamodb": {
            "NewImage": _image(
                run_id=ORIGINAL_RUN_ID,
                step_name="self_healing",
                agent_type="self_healing",
                healing_id="heal-0123456789ab",
                root_cause=error_classification,
                error_classification=error_classification,
                remediation_applied=remediation,
                original_run_id=ORIGINAL_RUN_ID,
                original_pipeline_id=original_pipeline_id,
                rerun_id=f"{ORIGINAL_RUN_ID}-heal",
                rerun_status=rerun_status,
                linked_run_id=linked_run_id,
                skip_reason=skip_reason,
                timestamp="2026-08-02T12:00:00+00:00",
            )
        },
    }


def _file_validation_record(
    *,
    run_id: str = HEAL_RERUN_RUN_ID,
    triggered_by: str | None = None,
    trigger: str | None = None,
    original_run_id: str | None = None,
    event_name: str = "INSERT",
) -> dict:
    return {
        "eventID": f"evt-file_validation-{run_id}",
        "eventName": event_name,
        "dynamodb": {
            "NewImage": _image(
                run_id=run_id,
                pipeline_id=PIPELINE_ID,
                step_name="file_validation",
                status="success",
                timestamp="2026-08-02T12:00:05+00:00",
                triggered_by=triggered_by,
                trigger=trigger,
                original_run_id=original_run_id,
            )
        },
    }


def _event(*records) -> dict:
    return {"Records": list(records)}


def _sole_update(mock_table) -> dict:
    """The kwargs of the one and only update_item call."""
    assert mock_table.update_item.call_count == 1, (
        f"expected exactly one update_item, got {mock_table.update_item.call_count}"
    )
    return mock_table.update_item.call_args.kwargs


# ---------------------------------------------------------------------------
# 1. The regression: an agent record must not drive the run's status
# ---------------------------------------------------------------------------


class TestHealDoesNotRewriteTheRunStatus:
    def test_healing_record_writes_no_status_and_no_current_step(self):
        """The defect this fixes: RUNNING derived from a record with no status."""
        updater, table = _load_handler()

        updater.lambda_handler(_event(_healing_record()), None)

        expr = _sole_update(table)["UpdateExpression"]
        assert "#status" not in expr
        assert "current_step" not in expr
        assert "steps_status" not in expr
        # And nothing anywhere in the call mentions the RUNNING it used to derive.
        assert "RUNNING" not in str(_sole_update(table))

    def test_healing_record_produces_one_update_not_the_two_step_updates(self):
        """A pipeline step costs two update_item calls; an agent record is not a step."""
        updater, table = _load_handler()
        updater.lambda_handler(_event(_healing_record()), None)
        assert table.update_item.call_count == 1

    def test_a_real_step_still_takes_the_step_path(self):
        """The routing is keyed on the agent step name only — steps are unaffected."""
        updater, table = _load_handler()

        updater.lambda_handler(
            _event(
                {
                    "eventID": "evt-run_pipeline-failed",
                    "eventName": "MODIFY",
                    "dynamodb": {
                        "NewImage": _image(
                            run_id=ORIGINAL_RUN_ID,
                            pipeline_id=PIPELINE_ID,
                            step_name="run_pipeline",
                            status="failed",
                        )
                    },
                }
            ),
            None,
        )

        # Scalars + steps_status map, exactly as before.
        assert table.update_item.call_count == 2
        assert table.put_item.called, "the failed step should still be captured in the errors table"
        first = table.update_item.call_args_list[0].kwargs
        assert first["ExpressionAttributeValues"][":status"] == "FAILED"

    def test_healing_record_does_not_capture_an_error(self):
        """The outcome record is not a failure — it must not land in the errors table."""
        updater, table = _load_handler()
        updater.lambda_handler(_event(_healing_record(rerun_status="failed")), None)
        assert not table.put_item.called

    def test_heal_guard_skip_record_is_dropped_for_want_of_a_pipeline_id(self):
        """The Heal_Guard skip record carries no pipeline id, so it never reaches the row.

        Stated as a test rather than left implicit: it is why
        ``skipped_heal_guard`` cannot appear in the run list, and a heal-
        triggered run is instead identifiable by its own trigger.
        """
        updater, table = _load_handler()
        updater.lambda_handler(_event(_healing_record(original_pipeline_id=None)), None)
        assert not table.update_item.called


# ---------------------------------------------------------------------------
# 2. The four states stay four states
# ---------------------------------------------------------------------------


class TestFourStatesStayDistinct:
    @pytest.mark.parametrize(
        "rerun_status",
        [
            "running",
            "success",
            "failed",
            "start_failed",
            "skipped_partial_write_guard",
            "skipped",
        ],
    )
    def test_rerun_status_is_copied_through_verbatim(self, rerun_status):
        updater, table = _load_handler()
        updater.lambda_handler(_event(_healing_record(rerun_status=rerun_status)), None)
        values = _sole_update(table)["ExpressionAttributeValues"]
        assert values[":sh_status"] == rerun_status

    def test_distinct_statuses_produce_distinct_written_values(self):
        """No two of the states collapse onto the same stored value."""
        written = []
        for status in ("running", "success", "failed", "start_failed", "skipped_partial_write_guard"):
            updater, table = _load_handler()
            updater.lambda_handler(_event(_healing_record(rerun_status=status)), None)
            written.append(_sole_update(table)["ExpressionAttributeValues"][":sh_status"])
        assert len(set(written)) == 5

    def test_a_record_with_no_rerun_status_is_reported_as_unknown(self):
        updater, table = _load_handler()
        record = _healing_record()
        del record["dynamodb"]["NewImage"]["rerun_status"]
        updater.lambda_handler(_event(record), None)
        assert _sole_update(table)["ExpressionAttributeValues"][":sh_status"] == "unknown"

    def test_the_remediation_and_root_cause_are_carried(self):
        updater, table = _load_handler()
        updater.lambda_handler(_event(_healing_record()), None)
        values = _sole_update(table)["ExpressionAttributeValues"]
        assert values[":sh_remediation"] == "Upgraded worker type from G.1X to G.2X"
        assert values[":sh_error_class"] == "oom_error"

    def test_the_guard_decline_carries_its_skip_reason(self):
        updater, table = _load_handler()
        updater.lambda_handler(
            _event(
                _healing_record(
                    rerun_status="skipped_partial_write_guard",
                    skip_reason="partial_write_guard",
                )
            ),
            None,
        )
        values = _sole_update(table)["ExpressionAttributeValues"]
        assert values[":sh_skip_reason"] == "partial_write_guard"


# ---------------------------------------------------------------------------
# 3. The forward link waits for a run id that actually exists
# ---------------------------------------------------------------------------


class TestForwardLinkToTheHealRerun:
    def test_no_link_while_the_rerun_is_in_flight(self):
        """``rerun_id`` is ``{original}-heal`` — lineage, not a navigable run."""
        updater, table = _load_handler()
        updater.lambda_handler(_event(_healing_record(rerun_status="running")), None)
        kwargs = _sole_update(table)
        assert "self_healing_linked_run_id" not in kwargs["UpdateExpression"]
        assert f"{ORIGINAL_RUN_ID}-heal" not in str(kwargs["ExpressionAttributeValues"])

    def test_link_appears_once_job_completion_links_the_terminal_result(self):
        """The MODIFY carrying ``linked_run_id`` is what makes the link real."""
        updater, table = _load_handler()
        updater.lambda_handler(
            _event(
                _healing_record(
                    rerun_status="success",
                    linked_run_id=HEAL_RERUN_RUN_ID,
                    event_name="MODIFY",
                )
            ),
            None,
        )
        values = _sole_update(table)["ExpressionAttributeValues"]
        assert values[":sh_linked_run_id"] == HEAL_RERUN_RUN_ID
        assert values[":sh_status"] == "success"


# ---------------------------------------------------------------------------
# 4. No phantom rows, and a permanent failure is not retried forever
# ---------------------------------------------------------------------------


class TestWriteSafety:
    def test_the_update_is_conditional_on_the_row_existing(self):
        updater, table = _load_handler()
        updater.lambda_handler(_event(_healing_record()), None)
        kwargs = _sole_update(table)
        assert kwargs["ConditionExpression"] == "attribute_exists(run_id)"
        assert kwargs["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": ORIGINAL_RUN_ID}

    def test_a_missing_execution_row_is_logged_not_retried(self):
        updater, table = _load_handler()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, "UpdateItem"
        )
        with patch.object(updater, "logger") as log:
            result = updater.lambda_handler(_event(_healing_record()), None)
        assert result["statusCode"] == 200
        assert log.warning.called

    def test_a_transient_dynamodb_failure_still_fails_the_batch(self):
        """Anything but the condition check is worth a stream retry."""
        updater, table = _load_handler()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
            "UpdateItem",
        )
        with pytest.raises(ClientError):
            updater.lambda_handler(_event(_healing_record()), None)


# ---------------------------------------------------------------------------
# 5. Run provenance — a heal re-run is identifiable as one
# ---------------------------------------------------------------------------


class TestRunProvenance:
    def test_heal_rerun_carries_its_trigger_and_source_run(self):
        updater, table = _load_handler()
        updater.lambda_handler(
            _event(
                _file_validation_record(
                    triggered_by="self_healing",
                    trigger="self_healing",
                    original_run_id=ORIGINAL_RUN_ID,
                )
            ),
            None,
        )
        first = table.update_item.call_args_list[0].kwargs
        assert first["ExpressionAttributeValues"][":triggered_by"] == "self_healing"
        assert first["ExpressionAttributeValues"][":original_run_id"] == ORIGINAL_RUN_ID

    def test_trigger_is_written_through_an_attribute_name_alias(self):
        """``trigger`` is a DynamoDB reserved word — a bare name is a ValidationException."""
        updater, table = _load_handler()
        updater.lambda_handler(_event(_file_validation_record(triggered_by="self_healing")), None)
        first = table.update_item.call_args_list[0].kwargs
        assert first["ExpressionAttributeNames"]["#trigger"] == "trigger"
        assert "#trigger = if_not_exists(#trigger" in first["UpdateExpression"]

    def test_a_producer_written_row_keeps_its_own_values(self):
        """if_not_exists — the dashboard retry path writes its own execution row first."""
        updater, table = _load_handler()
        updater.lambda_handler(
            _event(_file_validation_record(triggered_by="retry", original_run_id=ORIGINAL_RUN_ID)),
            None,
        )
        expr = table.update_item.call_args_list[0].kwargs["UpdateExpression"]
        assert "original_run_id = if_not_exists(original_run_id" in expr

    def test_the_legacy_trigger_key_is_accepted(self):
        """rerun_contract emits both keys; older rows may carry only ``trigger``."""
        updater, table = _load_handler()
        updater.lambda_handler(_event(_file_validation_record(trigger="scheduled")), None)
        first = table.update_item.call_args_list[0].kwargs
        assert first["ExpressionAttributeValues"][":triggered_by"] == "scheduled"

    def test_no_trigger_writes_no_trigger_attribute(self):
        updater, table = _load_handler()
        updater.lambda_handler(_event(_file_validation_record()), None)
        first = table.update_item.call_args_list[0].kwargs
        assert "#trigger" not in first["UpdateExpression"]
        assert "original_run_id" not in first["UpdateExpression"]

    def test_provenance_is_only_read_on_the_identity_row_insert(self):
        """A later MODIFY of file_validation must not re-derive provenance."""
        updater, table = _load_handler()
        updater.lambda_handler(
            _event(
                _file_validation_record(
                    triggered_by="self_healing",
                    original_run_id=ORIGINAL_RUN_ID,
                    event_name="MODIFY",
                )
            ),
            None,
        )
        first = table.update_item.call_args_list[0].kwargs
        assert "#trigger" not in first["UpdateExpression"]


# ---------------------------------------------------------------------------
# 6. p37 task 2.2 — the routing generalises without disturbing this precedent
# ---------------------------------------------------------------------------
#
# p37 C6 (task 2.1) generalised the ``self_healing`` routing above from one
# record type to an allowlist derived from ``shared/step_names.STEP_SEQUENCE``:
# ``_to_step_or_none`` is the allowlist and ``_drives_execution_row`` the
# routing predicate, checked in ``_process_record`` at ``:210`` — after the
# ``self_healing`` branch at ``:192`` and **before** the two derivations at
# ``:220-221``. ``LGR-453f12d-5`` (**template / ratified**) is the decision both
# rest on: "route non-step state rows away from the step machinery **before any
# status is derived**".
#
# This section asserts three things this file is the right home for, because
# each is about the precedent rather than about the classifier:
#
# 1. ``self_healing`` still takes its own branch. It has an outcome to apply, so
#    being classified as a non-Step_Row must not make it fall into the generic
#    early return — that would silently delete R14.3's visibility.
# 2. R4.2 in the same shape section 1 asserts it: a non-step row writes neither
#    ``#status`` nor ``current_step``.
# 3. ``pipeline_failure`` is admitted anyway, deliberately.
#
# The classifier itself, the ``load_metrics``/``data_quality`` caller cases and
# the non-raising guarantee are in ``test_execution_updater.py``. Both files use
# their own pre-existing loaders — this one's ``_load_handler`` above — so no new
# mocking pattern is introduced by task 2.2.
#
# **Validates: Requirements 4.1, 4.2, 4.3**

PIPELINE_FAILURE_SYNTHETIC_PIPELINE_ID = f"{ORIGINAL_RUN_ID}.99"


def _record_row_record(step_name: str, *, event_name: str = "INSERT", **attrs) -> dict:
    """A stream record for a non-step state row, with NO ``status`` attribute.

    The DQ_Report_Row carries ``overall_status``
    (``pyspark_templates/glue_job_s3_tables.py.j2:285``; the on-demand agent
    writes the same field at ``agentcore/data_quality_agent/main.py:582``), and
    C1's ``load_metrics`` row carries counts. Transcribed rather than generated,
    per LGR-28b4d75-5 — the same reason ``_healing_record`` above transcribes
    ``build_outcome_record``.

    ``pipeline_id`` is carried unless overridden: without it the row is dropped
    at ``:172-174`` for want of a pipeline, which would satisfy these assertions
    for the wrong reason. That is the "free dodge" p37 task 3.2 deliberately
    declines to rely on.
    """
    attrs.setdefault("pipeline_id", PIPELINE_ID)
    return {
        "eventID": f"evt-{step_name}",
        "eventName": event_name,
        "dynamodb": {"NewImage": _image(run_id=ORIGINAL_RUN_ID, step_name=step_name, **attrs)},
    }


class TestTheAllowlistGeneralisesThisRoutingWithoutReplacingIt:
    def test_self_healing_still_takes_its_own_branch(self):
        """It is not a Step_Row, but it must not take the generic early return.

        ``_drives_execution_row("self_healing")`` is ``False``, so if the
        predicate were checked *before* the dedicated branch the outcome record
        would be dropped and R14.3's run-list visibility would vanish silently.
        Section 1's tests would still pass — they only assert what is *not*
        written — so this is the assertion that makes the ordering visible.
        """
        updater, table = _load_handler()

        assert updater._drives_execution_row("self_healing") is False

        updater.lambda_handler(_event(_healing_record(rerun_status="success")), None)

        assert _sole_update(table)["ExpressionAttributeValues"][":sh_status"] == "success"

    def test_a_dq_report_row_writes_neither_status_nor_current_step(self):
        """R4.2, in section 1's shape.

        ``_derive_execution_status("data_quality", "")`` returns ``"RUNNING"``
        and the first update sets ``#status`` and ``current_step``
        unconditionally, so before C6 a DQ report on a finished run could flip
        it back to ``RUNNING -> data_quality`` — the same defect
        ``LGR-453f12d-5`` describes as showing "permanently, inflating the
        running count".
        """
        updater, table = _load_handler()

        updater.lambda_handler(
            _event(
                _record_row_record(
                    "data_quality",
                    overall_status="fail",
                    source="glue_inline_dq",
                    quarantine_path="s3://adi-quarantine/pipe-001/",
                )
            ),
            None,
        )

        assert table.update_item.call_count == 0
        assert not table.put_item.called

    def test_a_load_metrics_row_writes_neither_status_nor_current_step(self):
        """C1's new row, on the same stream shard as the run's step rows (shared
        ``run_id`` partition key). Already routed away, which is why C6 lands
        before C1 rather than after it."""
        updater, table = _load_handler()

        updater.lambda_handler(
            _event(_record_row_record("load_metrics", rows_read="5", rows_written="0", write_confirmed="false")),
            None,
        )

        assert table.update_item.call_count == 0

    def test_the_pipeline_failure_row_still_drives_status_and_its_steps_status_entry(self):
        """A documented exception to R4.3's literal text — required, not accidental.

        ``record_failure`` writes ``(run_id, "pipeline_failure")`` with
        ``status="error"`` and ``original_pipeline_id`` set to the real pipeline
        (``record_failure/lambda_function.py:432-447``), and it is the only
        carrier of a run's terminal FAILED status whenever the failing step never
        wrote a row of its own — ``States.Timeout`` at an approval gate,
        ``Lambda.ServiceException``, ``GlueJobFailed``. Routing it away would
        leave those runs reading RUNNING forever and would stop ``_capture_error``
        writing them to the errors table: the same inflated-running-count defect
        ``LGR-453f12d-5`` (**template / ratified**) fixed for ``self_healing``,
        reintroduced by the fix for it. Do not "correct" this to match R4.3's
        literal reading.
        """
        updater, table = _load_handler()

        updater.lambda_handler(
            _event(
                _record_row_record(
                    "pipeline_failure",
                    pipeline_id=PIPELINE_FAILURE_SYNTHETIC_PIPELINE_ID,
                    original_pipeline_id=PIPELINE_ID,
                    status="error",
                    error_message="States.Timeout",
                    failed_step="check_code_approval",
                )
            ),
            None,
        )

        assert updater._drives_execution_row("pipeline_failure") is True
        assert table.update_item.call_count == 2
        first = table.update_item.call_args_list[0].kwargs
        assert first["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": ORIGINAL_RUN_ID}
        assert first["ExpressionAttributeValues"][":status"] == "FAILED"
        second = table.update_item.call_args_list[1].kwargs
        assert second["ExpressionAttributeNames"]["#step_name"] == "pipeline_failure"
        assert second["ExpressionAttributeValues"][":step_status"] == "FAILED"
        assert table.put_item.called, "a recorded failure still reaches the errors table"
