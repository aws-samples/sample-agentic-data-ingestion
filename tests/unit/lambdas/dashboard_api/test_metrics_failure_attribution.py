# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Stage Metrics panel accounts for failures no stage row recorded (p59).

WHY THIS FILE EXISTS. The metrics page reported **26 Failed Executions** and a
**50.7% success rate** directly above a Stage Metrics panel showing roughly
**one** error across all six stages. The success rate was not wrong —
`_success_rate` divides `ExecutionsSucceeded` by all four terminal `AWS/States`
outcomes and is correct. The panel was, and structurally so: it queried the six
stage ``step_name`` partitions and counted rows whose ``status`` was in
`_ERROR_STATUSES`, and the comment above that read asserted the exclusion of
``pipeline_failure`` rows was a FEATURE ("so they can't skew stage counts").

`LGR-9e76249-6` (**template / ratified**) is what makes that a defect rather
than a preference: the RecordFailure sentinel ``(run_id, "pipeline_failure")``
is **the only carrier of a run's terminal FAILED status whenever the failing
step never got to write a row of its own** — a Lambda timeout, a
``States.Timeout`` parked at an approval gate, ``GlueJobFailed``. Every task
state Catches to RecordFailure for exactly those cases. So a run that failed at
a gate or in Glue incremented `ExecutionsFailed` and contributed nothing to any
stage's error count.

WHAT IS PINNED HERE

1. **The reconciliation.** A run that fails at an approval gate, with no stage
   error row anywhere, reaches the stage panel's error accounting. This is the
   test the defect would have failed.
2. **No double-count.** A run with BOTH a step row at ``status="error"`` AND a
   ``pipeline_failure`` row counts ONCE. Both writes happen — the step recorded
   its own failure and RecordFailure also fired — and counting both would
   inflate the stage. Deduped per ``run_id`` per stage.
3. **Unattributable failures are visible, not dropped.** Only six of the
   thirteen `Canonical_Step_Names` have a stage row on this page, so a run that
   died at ``check_code_approval``, ``test_code``, ``file_validation`` or
   ``create_glue_pipeline`` names a step no stage renders. Those are counted and
   named in ``unattributed_by_step``. Dropping them silently is the same defect
   one level down.
4. **Three-state discipline.** A failure-partition read that RAISED reports
   ``pipeline_failure_records_incomplete`` and publishes ``None``, never a dict
   of zeros — `LGR-4854b3d-1` (**template / ratified**) forbids collapsing a
   three-state value to one of its ends, and "no run failed outside a stage row"
   is the wrong end to collapse a failed read onto. A read that SUCCEEDED and
   found nothing is a measured zero, not an absence: there is deliberately no
   ``absent`` arm, the same reasoning that keeps `failed_pipelines` a plain
   number.
5. **The residual is signed.** ``failed_executions_without_a_record`` is
   published unclamped in both directions, because it is the field whose job is
   to admit the arithmetic does not close.
6. **The attribution map is DERIVED from `_STAGE_STEP_NAMES`,** not a second
   copy of it — including the legacy PascalCase spellings, which is why a
   ``failed_step`` of ``CodeGeneration`` lands on ``code_generation``.

Test approach mirrors the sibling characterization file: the module-level
``state_table`` and ``cloudwatch_client`` are patched with ``MagicMock`` stubs
that route rows BY PARTITION, the way DynamoDB would. No ``moto.mock_aws()`` and
no boto3 client mock is created here
(``tests/unit/test_no_duplicate_mocking_prop.py``) — these reads go through a
table resource the handler holds as a module global.

Validates: the Stage Metrics panel reconciling with `performance.failed_pipelines`
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import AttributeBase

_HANDLER_ROOT = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (_HANDLER_ROOT, _SHARED_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

START = datetime(2025, 3, 1, tzinfo=UTC)
END = START + timedelta(days=1)
MID = START + timedelta(hours=6)
PERIOD = 300


# ═══════════════════════════════════════════════════════════════════════════
# Stubs
# ═══════════════════════════════════════════════════════════════════════════


def _queried_step_name(query_kwargs: dict[str, Any]) -> str | None:
    """The single ``step_name`` a Query was bounded to, or ``None``."""
    condition = query_kwargs.get("KeyConditionExpression")
    if condition is None:
        return None
    pending = [condition]
    while pending:
        expression = pending.pop().get_expression()
        operator = expression["operator"]
        values = expression["values"]
        if operator in ("AND", "OR"):
            pending.extend(values)
            continue
        name = values[0].name if isinstance(values[0], AttributeBase) else str(values[0])
        if operator == "=" and name == "step_name":
            return str(values[1])
    return None


def _partitioned_state_table(rows_by_partition: dict[str, list[dict]]) -> MagicMock:
    """A state table answering each Query with only its own partition's rows.

    A stub returning one flat list for every partition would hand the same rows
    to all eleven partition reads — including handing stage rows to the failure
    read, which would make every assertion below unable to fail for the right
    reason.
    """

    def query(**kwargs: Any) -> dict[str, Any]:
        return {"Items": list(rows_by_partition.get(_queried_step_name(kwargs) or "", []))}

    table = MagicMock()
    table.query.side_effect = query
    return table


def _empty_cloudwatch() -> MagicMock:
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    return client


def _step_row(run_id: str, step_name: str, status: str, timestamp: datetime = MID) -> dict:
    return {
        "run_id": run_id,
        "step_name": step_name,
        "status": status,
        "timestamp": timestamp.isoformat(),
    }


def _failure_row(run_id: str, failed_step: str, timestamp: datetime = MID) -> dict:
    """A RecordFailure sentinel row, in the shape that Lambda writes.

    Only the three attributes `_attribute_failure_records` projects are supplied
    — the handler reads ``run_id``, ``failed_step`` and ``timestamp`` and nothing
    else, and giving it more here would hide a projection that stopped covering
    what the attribution needs.
    """
    return {"run_id": run_id, "failed_step": failed_step, "timestamp": timestamp.isoformat()}


def _read(rows_by_partition: dict[str, list[dict]]):
    state = _partitioned_state_table(rows_by_partition)
    with (
        patch.object(metrics_mod, "state_table", state),
        patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
    ):
        return metrics_mod._get_stage_metrics(START, END, PERIOD)


# ═══════════════════════════════════════════════════════════════════════════
# 1. The reconciliation — the test the defect would have failed
# ═══════════════════════════════════════════════════════════════════════════


class TestFailuresNoStageRowRecorded:
    def test_a_gate_timeout_with_no_stage_error_row_reaches_the_error_accounting(self):
        """A run parked at an approval gate until ``States.Timeout``.

        The state table holds a SUCCESSFUL ``schema_mapping`` row for the run and
        nothing after it — the gate is a Choice state and writes no row
        (``shared/step_names.py``: the three ``check_*`` members "never write
        state rows"). RecordFailure's sentinel attributes the failure to
        ``check_schema_mapping_approval``.

        Before p59 this run was invisible to the whole panel. It is now in the
        error accounting: not in a stage's ``error_count``, because no stage on
        this page IS the gate, but named and counted in
        ``unattributed_by_step`` so a reader can see it.
        """
        read = _read(
            {
                "schema_mapping": [_step_row("run-gate", "schema_mapping", "success")],
                "pipeline_failure": [_failure_row("run-gate", "check_schema_mapping_approval")],
            }
        )

        assert sum(stage["error_count"] for stage in read.stages.values()) == 0, (
            "the gate is not one of the six stages, so no stage's error_count may claim it"
        )
        published = metrics_mod._published_attribution(read.attribution, failed_executions=1)
        assert published is not None
        assert published["failure_records"] == 1
        assert published["unattributed"] == 1
        assert published["unattributed_by_step"] == {"check_schema_mapping_approval": 1}
        assert published["failed_executions_without_a_record"] == 0, (
            "one failed execution and one failure record: the two halves reconcile exactly"
        )

    def test_a_glue_failure_with_no_stage_error_row_increments_that_stage(self):
        """``GlueJobFailed`` attributes to ``run_pipeline``, which IS a rendered
        stage — so this failure lands in ``pipeline_run.error_count``.

        `ERROR_NAME_TO_STEP` in RecordFailure maps ``GlueJobFailed`` ->
        ``run_pipeline`` (``record_failure/lambda_function.py:214``), and the run
        has no ``run_pipeline`` row of its own because the Glue job failed rather
        than the step.
        """
        read = _read({"pipeline_failure": [_failure_row("run-glue", "run_pipeline")]})

        assert read.stages["pipeline_run"]["error_count"] == 1
        assert read.stages["pipeline_run"]["success_count"] == 0
        published = metrics_mod._published_attribution(read.attribution, failed_executions=1)
        assert published is not None
        assert published["attributed_to_stage"] == 1
        assert published["counted_into_stage_errors"] == 1
        assert published["unattributed"] == 0

    def test_a_legacy_pascalcase_failed_step_lands_on_its_canonical_stage(self):
        """``failed_step`` in the legacy spelling attributes to the same stage.

        The attribution map is DERIVED from `_STAGE_STEP_NAMES` through
        `step_names.from_any`, so both partition spellings of a stage collapse
        onto one canonical entry. A second hand-written map is what this pins
        against.
        """
        read = _read({"pipeline_failure": [_failure_row("run-legacy", "CodeGeneration")]})
        assert read.stages["code_generation"]["error_count"] == 1

    def test_the_attribution_map_covers_exactly_the_stage_partitions(self):
        """Every `_STAGE_STEP_NAMES` partition is attributable, and nothing else
        was invented alongside them."""
        from step_names import from_any

        expected = {from_any(step).value for steps in metrics_mod._STAGE_STEP_NAMES.values() for step in steps}
        assert set(metrics_mod._CANONICAL_STEP_TO_STAGE) == expected
        for steps in metrics_mod._STAGE_STEP_NAMES.values():
            for step in steps:
                assert metrics_mod._stage_for_failed_step(step) is not None


# ═══════════════════════════════════════════════════════════════════════════
# 2. No double-count
# ═══════════════════════════════════════════════════════════════════════════


class TestDedupeByRunId:
    def test_a_run_with_both_row_kinds_counts_once(self):
        """A step that wrote its own ``status="error"`` row AND had RecordFailure
        fire for the same run contributes ONE error to that stage.

        Both writes really happen: the step's ``@step_handler`` raises after
        recording, the state machine Catches to RecordFailure, and the sentinel
        row is written for the same ``run_id``. Counting both would inflate the
        stage by exactly the runs that recorded their own failure properly.
        """
        read = _read(
            {
                "create_code": [_step_row("run-both", "create_code", "error")],
                "pipeline_failure": [_failure_row("run-both", "create_code")],
            }
        )

        assert read.stages["code_generation"]["error_count"] == 1
        published = metrics_mod._published_attribution(read.attribution, failed_executions=1)
        assert published is not None
        assert published["attributed_to_stage"] == 1
        assert published["already_counted_by_stage_row"] == 1
        assert published["counted_into_stage_errors"] == 0, (
            "the stage row already counted this run, so the sentinel adds nothing"
        )

    def test_dedupe_is_per_stage_not_global(self):
        """A stage row for one stage does not suppress a sentinel naming another.

        ``run-two-stages`` recorded its own ``schema_mapping`` error and a
        sentinel attributing to ``create_code``. Deduping on ``run_id`` alone
        would drop the second — the key is (stage, run_id).
        """
        read = _read(
            {
                "schema_mapping": [_step_row("run-two-stages", "schema_mapping", "error")],
                "pipeline_failure": [_failure_row("run-two-stages", "create_code")],
            }
        )
        assert read.stages["schema_mapping"]["error_count"] == 1
        assert read.stages["code_generation"]["error_count"] == 1

    def test_two_sentinel_rows_for_one_run_and_stage_count_once(self):
        """A retried run can leave two sentinel rows in the window. The first
        adds the run to the stage's set, so the second is deduped against it."""
        read = _read(
            {
                "pipeline_failure": [
                    _failure_row("run-retried", "run_pipeline", MID),
                    _failure_row("run-retried", "run_pipeline", MID + timedelta(hours=1)),
                ]
            }
        )
        assert read.stages["pipeline_run"]["error_count"] == 1
        assert read.attribution.already_counted == 1

    def test_a_sentinel_with_no_run_id_is_counted_rather_than_dropped(self):
        """It cannot be deduped, and dropping it would be the defect this whole
        change closes. RecordFailure always writes something for ``run_id``
        (falling back to the execution name, then ``"unknown"``), so a blank is
        the rare shape rather than the norm."""
        read = _read({"pipeline_failure": [{"failed_step": "run_pipeline", "timestamp": MID.isoformat()}]})
        assert read.stages["pipeline_run"]["error_count"] == 1
        assert read.attribution.counted == 1


# ═══════════════════════════════════════════════════════════════════════════
# 3. Unattributable failures are visible
# ═══════════════════════════════════════════════════════════════════════════


class TestUnattributableFailures:
    @pytest.mark.parametrize(
        "failed_step",
        ["file_validation", "schema_compare", "test_code", "check_code_approval", "create_glue_pipeline"],
    )
    def test_a_canonical_step_with_no_stage_row_is_named_not_dropped(self, failed_step: str):
        """Seven of the thirteen canonical steps have no stage on this page.

        Each is a real place a run dies, and each must be counted somewhere the
        reader can see. These are exactly the failures that made 26 and ~1
        irreconcilable.
        """
        read = _read({"pipeline_failure": [_failure_row("run-x", failed_step)]})

        assert sum(stage["error_count"] for stage in read.stages.values()) == 0
        assert read.attribution.records == 1
        assert read.attribution.unattributed_by_step == {failed_step: 1}

    def test_an_unparseable_failed_step_is_kept_under_its_raw_value(self):
        """`step_names.from_any` raises on an unknown name by contract. The row
        is still counted, keyed by what it actually said — a name nobody
        recognises is a finding, and silently discarding it hides one."""
        read = _read({"pipeline_failure": [_failure_row("run-y", "SomeFutureStep")]})
        assert read.attribution.unattributed_by_step == {"SomeFutureStep": 1}

    def test_a_missing_failed_step_is_kept_as_unknown(self):
        """``"unknown"`` is what RecordFailure itself writes when it cannot
        attribute (``record_failure/lambda_function.py:206``)."""
        read = _read({"pipeline_failure": [{"run_id": "run-z", "timestamp": MID.isoformat()}]})
        assert read.attribution.unattributed_by_step == {"unknown": 1}

    def test_the_breakdown_is_a_breakdown_and_not_just_a_total(self):
        """Per-step counts, because "5 timed out at the code approval gate" and
        "5 failed somewhere" are different operational facts."""
        read = _read(
            {
                "pipeline_failure": [
                    _failure_row("r1", "check_code_approval"),
                    _failure_row("r2", "check_code_approval"),
                    _failure_row("r3", "test_code"),
                ]
            }
        )
        assert read.attribution.unattributed == 3
        published = metrics_mod._published_attribution(read.attribution, failed_executions=3)
        assert published is not None
        assert published["unattributed_by_step"] == {"check_code_approval": 2, "test_code": 1}


# ═══════════════════════════════════════════════════════════════════════════
# 4. Three-state discipline (LGR-4854b3d-1)
# ═══════════════════════════════════════════════════════════════════════════


class TestThreeStateProvenance:
    def test_a_read_that_found_nothing_is_a_measured_zero(self):
        """An empty ``pipeline_failure`` partition IS the measurement "no run
        failed outside a stage row". There is deliberately no ``absent`` arm."""
        read = _read({})
        assert read.attribution.outcome.determined_from == metrics_mod.SOURCE_FAILURE_RECORDS
        assert read.attribution.outcome.value == 0
        published = metrics_mod._published_attribution(read.attribution, failed_executions=0)
        assert published is not None
        assert published["failure_records"] == 0

    def test_a_raising_failure_read_does_not_read_as_no_failures(self):
        """The stage partitions answer; only the failure partition raises.

        The stage counts stay as the step rows left them, and the published fact
        is ``None`` — not a dict of zeros, which would assert that nothing failed
        outside a stage row when in fact nobody looked.
        """
        rows = {"schema_extract": [_step_row("run-a", "schema_extract", "success")]}

        def query(**kwargs: Any) -> dict[str, Any]:
            step = _queried_step_name(kwargs)
            if step == metrics_mod._FAILURE_RECORD_STEP_NAME:
                raise RuntimeError("DynamoDB unavailable for the failure partition")
            return {"Items": list(rows.get(step or "", []))}

        state = MagicMock()
        state.query.side_effect = query
        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
            patch.object(metrics_mod.logger, "warning") as mock_warning,
        ):
            read = metrics_mod._get_stage_metrics(START, END, PERIOD)

        assert read.stages["schema_extract"]["success_count"] == 1, "the stage read succeeded and must be unaffected"
        assert read.attribution.outcome.value is None
        assert read.attribution.outcome.determined_from == f"{metrics_mod.SOURCE_FAILURE_RECORDS}_incomplete"
        assert metrics_mod._published_attribution(read.attribution, failed_executions=26) is None
        mock_warning.assert_called()

    def test_the_two_reads_fail_independently(self):
        """A raising STAGE read does not make the failure read report incomplete.

        The two are in separate ``try`` blocks precisely so a reader can tell
        which one did not complete.
        """

        def query(**kwargs: Any) -> dict[str, Any]:
            if _queried_step_name(kwargs) != metrics_mod._FAILURE_RECORD_STEP_NAME:
                raise RuntimeError("DynamoDB unavailable for the stage partitions")
            return {"Items": [_failure_row("run-only-sentinel", "run_pipeline")]}

        state = MagicMock()
        state.query.side_effect = query
        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
        ):
            read = metrics_mod._get_stage_metrics(START, END, PERIOD)

        assert read.attribution.outcome.value == 1
        assert read.stages["pipeline_run"]["error_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 5. The residual — where the halves cannot be made to add up
# ═══════════════════════════════════════════════════════════════════════════


class TestReconciliationResidual:
    def test_the_published_numbers_satisfy_the_two_partition_identities(self):
        """The arithmetic a reader checks the page with, asserted rather than
        asserted-about-in-prose.

        Every failure record lands in exactly one branch, so:

            failure_records     == attributed_to_stage + unattributed
            attributed_to_stage == counted_into_stage_errors + already_counted_by_stage_row
            failed_executions   == failure_records + failed_executions_without_a_record

        Mixed rows on purpose: two attributed to distinct stages, one already
        covered by its stage's own error row, two naming steps no stage renders,
        and a failed-execution total larger than all of them.
        """
        read = _read(
            {
                "create_code": [_step_row("r-dup", "create_code", "error")],
                "pipeline_failure": [
                    _failure_row("r-dup", "create_code"),
                    _failure_row("r-glue", "run_pipeline"),
                    _failure_row("r-infra", "create_infrastructure"),
                    _failure_row("r-gate", "check_code_approval"),
                    _failure_row("r-test", "test_code"),
                ],
            }
        )
        published = metrics_mod._published_attribution(read.attribution, failed_executions=9)
        assert published is not None

        assert published["failure_records"] == published["attributed_to_stage"] + published["unattributed"]
        assert (
            published["attributed_to_stage"]
            == published["counted_into_stage_errors"] + published["already_counted_by_stage_row"]
        )
        assert (
            published["failed_executions"]
            == published["failure_records"] + published["failed_executions_without_a_record"]
        )
        assert published["unattributed"] == sum(published["unattributed_by_step"].values())

        # And the concrete split, so a refactor that preserved the identities
        # while moving rows between branches still fails.
        assert published == {
            "failure_records": 5,
            "attributed_to_stage": 3,
            "counted_into_stage_errors": 2,
            "already_counted_by_stage_row": 1,
            "unattributed": 2,
            "unattributed_by_step": {"check_code_approval": 1, "test_code": 1},
            "rejected_at_approval": 0,
            "rejected_by_step": {},
            "failed_executions": 9,
            "failed_executions_without_a_record": 4,
        }
        # The two new stage errors landed on the two stages the records named,
        # and the deduped one did not double its stage.
        assert read.stages["code_generation"]["error_count"] == 1, "the deduped run did not double its stage"
        assert read.stages["pipeline_run"]["error_count"] == 1
        assert read.stages["infrastructure"]["error_count"] == 1

    def test_failed_executions_with_no_record_are_published_positive(self):
        """26 failed executions, 4 failure records: 22 unaccounted for.

        An execution can reach FAILED without RecordFailure writing anything —
        its own ``put_item`` is caught and logged so a RecordFailure crash never
        masks the original failure — and ``ExecutionsAborted`` /
        ``ExecutionsTimedOut`` never route through it at all.
        """
        read = _read(
            {
                "pipeline_failure": [
                    _failure_row("r1", "run_pipeline"),
                    _failure_row("r2", "check_code_approval"),
                    _failure_row("r3", "create_code"),
                    _failure_row("r4", "test_code"),
                ]
            }
        )
        published = metrics_mod._published_attribution(read.attribution, failed_executions=26)
        assert published is not None
        assert published["failure_records"] == 4
        assert published["failed_executions"] == 26
        assert published["failed_executions_without_a_record"] == 22

    def test_the_residual_is_signed_and_not_clamped(self):
        """More failure records than failed executions is a real shape.

        The two are read over the same wall clock from different systems, so a
        run on the boundary can be counted by one and not the other. Clamping to
        zero would be the collapse `LGR-4854b3d-1` forbids, in the one field
        whose whole job is to admit the arithmetic does not close.
        """
        read = _read({"pipeline_failure": [_failure_row("r1", "run_pipeline"), _failure_row("r2", "create_code")]})
        published = metrics_mod._published_attribution(read.attribution, failed_executions=1)
        assert published is not None
        assert published["failed_executions_without_a_record"] == -1

    def test_an_unread_states_metric_makes_the_residual_none_not_zero(self):
        """`failed_pipelines` is 0 when the AWS/States read failed, and a 0 there
        is the read's failure rather than a count. Passing it through would claim
        every failure record is unexplained."""
        read = _read({"pipeline_failure": [_failure_row("r1", "run_pipeline")]})
        published = metrics_mod._published_attribution(read.attribution, failed_executions=None)
        assert published is not None
        assert published["failed_executions"] is None
        assert published["failed_executions_without_a_record"] is None
        assert published["failure_records"] == 1, "the failure read still answered, and says so"


# ═══════════════════════════════════════════════════════════════════════════
# 6. The read stays bounded (P28 R17.1)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheReadStaysBounded:
    def test_the_failure_partition_is_a_keyed_windowed_query_never_a_scan(self):
        """``pipeline_failure`` is its own partition of the same GSI, so this is
        one more bounded Query and not a new access pattern."""
        state = _partitioned_state_table({})
        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
        ):
            metrics_mod._get_stage_metrics(START, END, PERIOD)

        state.scan.assert_not_called()
        failure_calls = [
            call.kwargs
            for call in state.query.call_args_list
            if _queried_step_name(call.kwargs) == metrics_mod._FAILURE_RECORD_STEP_NAME
        ]
        assert len(failure_calls) == 1, f"expected exactly one failure-partition Query, got {len(failure_calls)}"
        kwargs = failure_calls[0]
        assert kwargs["IndexName"] == metrics_mod._STEP_TIMESTAMP_INDEX
        names = kwargs["ExpressionAttributeNames"]
        projected = {names.get(part.strip(), part.strip()) for part in kwargs["ProjectionExpression"].split(",")}
        assert {"run_id", "failed_step", "timestamp"} <= projected, (
            f"the attribution reads run_id, failed_step and timestamp; projection is {projected}"
        )

    def test_rows_outside_the_window_are_re_checked_client_side(self):
        """BETWEEN on ISO strings is lexicographic, so the client-side
        ``_parse_iso`` check stays the authoritative filter — the same
        arrangement the stage reads use."""
        read = _read(
            {
                "pipeline_failure": [
                    _failure_row("r1", "run_pipeline", START - timedelta(minutes=1)),
                    _failure_row("r2", "run_pipeline", END + timedelta(minutes=1)),
                    _failure_row("r3", "run_pipeline", MID),
                ]
            }
        )
        assert read.attribution.records == 1

    @pytest.mark.parametrize("timestamp", ["", "not-a-timestamp", "2025-13-45T99:99:99"])
    def test_an_unparseable_timestamp_is_skipped_not_counted(self, timestamp: str):
        read = _read({"pipeline_failure": [{"run_id": "r1", "failed_step": "run_pipeline", "timestamp": timestamp}]})
        assert read.attribution.records == 0
        assert read.stages["pipeline_run"]["error_count"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. The published shape reaches the payload
# ═══════════════════════════════════════════════════════════════════════════


class TestThePayloadCarriesTheFact:
    def _payload(self, rows_by_partition: dict[str, list[dict]], now: datetime) -> dict:
        import json

        rows = {
            step: [{**row, "timestamp": (now - timedelta(minutes=1)).isoformat()} for row in rows]
            for step, rows in rows_by_partition.items()
        }
        state = _partitioned_state_table(rows)
        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
            patch.object(metrics_mod, "_cache_table", MagicMock(get_item=MagicMock(return_value={}))),
        ):
            result = metrics_mod.get_comprehensive_metrics({"queryStringParameters": {"period": "24h"}})
        assert result["statusCode"] == 200
        return json.loads(result["body"])

    def test_the_fact_is_published_with_its_provenance_and_stays_out_of_measurement(self):
        """Registered in the `_PublishedFact` registry, so `determined_from`
        derives from it. NOT in ``measurement``: that map's key set is the
        performance TILE key set exactly (Property 7 in
        ``test_metrics_stage_measurement_prop.py``), and this is a structured
        fact about the panel rather than a figure on a tile.
        """
        body = self._payload(
            {"pipeline_failure": [_failure_row("r1", "check_code_approval")]},
            datetime.now(UTC),
        )
        performance = body["performance"]

        assert performance["stage_failure_attribution"]["unattributed_by_step"] == {"check_code_approval": 1}
        assert performance["determined_from"]["stage_failure_attribution"] == metrics_mod.SOURCE_FAILURE_RECORDS
        assert "stage_failure_attribution" not in performance["measurement"]
        assert "stage_failure_attribution" not in performance["missing"], (
            "the read completed, so the fact is established and is not missing"
        )

    def test_an_unreadable_failure_partition_reaches_missing(self):
        """R4.3's derivation carries the new fact with no change to it: the value
        is ``None``, so ``missing`` names it and ``determined_from`` says why."""
        import json

        def query(**kwargs: Any) -> dict[str, Any]:
            if _queried_step_name(kwargs) == metrics_mod._FAILURE_RECORD_STEP_NAME:
                raise RuntimeError("DynamoDB unavailable for the failure partition")
            return {"Items": []}

        state = MagicMock()
        state.query.side_effect = query
        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
            patch.object(metrics_mod, "_cache_table", MagicMock(get_item=MagicMock(return_value={}))),
        ):
            body = json.loads(
                metrics_mod.get_comprehensive_metrics({"queryStringParameters": {"period": "24h"}})["body"]
            )

        performance = body["performance"]
        assert performance["stage_failure_attribution"] is None
        assert "stage_failure_attribution" in performance["missing"]
        assert (
            performance["determined_from"]["stage_failure_attribution"]
            == f"{metrics_mod.SOURCE_FAILURE_RECORDS}_incomplete"
        )

    def test_the_stage_key_set_and_field_set_are_unchanged(self):
        """No seventh pseudo-stage and no seventh field per stage. The
        unattributable failures are published as a fact ABOUT the panel because
        both key sets are pinned on both sides of the wire."""
        body = self._payload({"pipeline_failure": [_failure_row("r1", "test_code")]}, datetime.now(UTC))
        assert set(body["stages"]) == set(metrics_mod._STAGE_STEP_NAMES)
        for values in body["stages"].values():
            assert set(values) == {"success_count", "error_count", "avg_time_ms"}


# ═══════════════════════════════════════════════════════════════════════════
# 8. Approval rejections are decisions, not failures (2026-10-10)
# ═══════════════════════════════════════════════════════════════════════════
#
# Expectations come from the rule the user agreed, not from handler output:
#
# * a `pipeline_failure` row whose `error_name` is `ApprovalRejected` is a
#   reviewer's decision. It is counted as `rejected_at_approval` and is in no
#   failure figure: not Failed Executions, not the Success Rate denominator,
#   not a stage error and not an unattributed failure;
# * an approval nobody answered (`States.Timeout`, `ApprovalTimeout`) is
#   still a failure;
# * when the failure partition cannot be read, nothing is taken out and the
#   rejection count is null rather than 0.


def _rejection_row(run_id: str, gate: str, timestamp: datetime = MID) -> dict:
    """A RecordFailure row for a run a reviewer rejected at ``gate``."""
    return {**_failure_row(run_id, gate, timestamp), "error_name": "ApprovalRejected"}


def _states_cloudwatch(succeeded: int, failed: int) -> MagicMock:
    """CloudWatch answering the AWS/States sums the success rate reads.

    Every ``get_metric_data`` call gets the same results, so both windows see
    the same counts. The other reads look for different ids and find nothing.
    """
    client = MagicMock()
    client.get_metric_data.return_value = {
        "MetricDataResults": [
            {"Id": "succeeded_sum", "Values": [float(succeeded)]},
            {"Id": "failed_sum", "Values": [float(failed)]},
            {"Id": "aborted_sum", "Values": [0.0]},
            {"Id": "timed_out_sum", "Values": [0.0]},
        ]
    }
    return client


def _comprehensive(state: MagicMock, cloudwatch: MagicMock, **patches: Any) -> dict:
    import contextlib
    import json

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(metrics_mod, "state_table", state))
        stack.enter_context(patch.object(metrics_mod, "cloudwatch_client", cloudwatch))
        stack.enter_context(patch.object(metrics_mod, "_cache_table", MagicMock(get_item=MagicMock(return_value={}))))
        for name, value in patches.items():
            stack.enter_context(patch.object(metrics_mod, name, value))
        result = metrics_mod.get_comprehensive_metrics({"queryStringParameters": {"period": "7d"}})
    assert result["statusCode"] == 200
    return json.loads(result["body"])


def _recent(rows: list[dict]) -> list[dict]:
    """The rows re-stamped one minute ago, inside the current window only."""
    stamp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    return [{**row, "timestamp": stamp} for row in rows]


class TestApprovalRejectionsAreNotFailures:
    def test_a_rejection_is_set_aside_from_every_failure_field(self):
        read = _read(
            {
                "pipeline_failure": [
                    _rejection_row("r-rej", "check_schema_mapping_approval"),
                    {**_failure_row("r-timeout", "check_schema_mapping_approval"), "error_name": "States.Timeout"},
                    {**_failure_row("r-glue", "run_pipeline"), "error_name": "GlueJobFailed"},
                ]
            }
        )
        attribution = read.attribution
        assert attribution.rejected_by_step == {"check_schema_mapping_approval": 1}
        assert attribution.records == 2, "the timeout and the Glue failure are failures; the rejection is not"
        assert attribution.unattributed_by_step == {"check_schema_mapping_approval": 1}, "only the timeout"
        assert read.stages["pipeline_run"]["error_count"] == 1

    @pytest.mark.parametrize("error_name", ["States.Timeout", "ApprovalTimeout", "Unknown", ""])
    def test_an_approval_nobody_decided_stays_a_failure(self, error_name: str):
        row = {**_failure_row("r1", "check_code_approval"), "error_name": error_name}
        read = _read({"pipeline_failure": [row]})
        assert read.attribution.rejected == 0
        assert read.attribution.unattributed_by_step == {"check_code_approval": 1}

    def test_the_rejection_test_is_on_error_name_and_a_blank_gate_is_kept(self):
        """A rejection whose gate RecordFailure could not name is still a
        rejection, kept under ``unknown`` the way an unnamed failure is."""
        read = _read({"pipeline_failure": [_rejection_row("r1", "")]})
        assert read.attribution.rejected_by_step == {"unknown": 1}
        assert read.attribution.records == 0

    def test_rejections_are_published_beside_the_failure_split(self):
        read = _read(
            {
                "pipeline_failure": [
                    _rejection_row("r1", "check_schema_mapping_approval"),
                    _rejection_row("r2", "check_schema_mapping_approval"),
                    _rejection_row("r3", "check_code_approval"),
                    _failure_row("r4", "create_glue_pipeline"),
                ]
            }
        )
        published = metrics_mod._published_attribution(read.attribution, failed_executions=1)
        assert published is not None
        assert published["rejected_at_approval"] == 3
        assert published["rejected_by_step"] == {"check_code_approval": 1, "check_schema_mapping_approval": 2}
        assert published["failure_records"] == 1
        assert published["failed_executions_without_a_record"] == 0

    def test_the_failure_read_projects_error_name(self):
        """A GSI Query with a ProjectionExpression returns only the named
        attributes, so without ``error_name`` every rejection would read as a
        failure and nothing would ever be taken out."""
        state = _partitioned_state_table({})
        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
        ):
            metrics_mod._get_stage_metrics(START, END, PERIOD)
        (kwargs,) = [
            call.kwargs
            for call in state.query.call_args_list
            if _queried_step_name(call.kwargs) == metrics_mod._FAILURE_RECORD_STEP_NAME
        ]
        names = kwargs["ExpressionAttributeNames"]
        projected = {names.get(part.strip(), part.strip()) for part in kwargs["ProjectionExpression"].split(",")}
        assert "error_name" in projected

    def test_the_constant_matches_what_record_failure_stores(self):
        """The handler cannot import another Lambda's module, so the name is
        spelled twice. This pins the two spellings together."""
        from pathlib import Path

        record_failure = Path(_HANDLER_ROOT).parent / "agentic_data_ingestion_record_failure" / "lambda_function.py"
        source = record_failure.read_text(encoding="utf-8")
        assert f'APPROVAL_REJECTED_ERROR_NAME = "{metrics_mod._APPROVAL_REJECTED_ERROR_NAME}"' in source


class TestTheHeadlineFiguresTakeRejectionsOut:
    """The 7-day window the user reported: 4 succeeded and 4 failed in
    AWS/States, where 3 of the 4 failures were rejections at schema-mapping
    approval and 1 was a real failure at create_glue_pipeline."""

    ROWS = [
        _rejection_row("r1", "check_schema_mapping_approval"),
        _rejection_row("r2", "check_schema_mapping_approval"),
        _rejection_row("r3", "check_schema_mapping_approval"),
        {**_failure_row("r4", "create_glue_pipeline"), "error_name": "StepFailure"},
    ]

    def test_failed_executions_and_success_rate_exclude_rejections(self):
        body = _comprehensive(
            _partitioned_state_table({"pipeline_failure": _recent(self.ROWS)}),
            _states_cloudwatch(succeeded=4, failed=4),
        )
        performance = body["performance"]
        assert performance["failed_pipelines"] == 1
        assert performance["rejected_at_approval"] == 3
        assert performance["overall_success_rate"] == 80.0, "4 / (4 + 1)"
        assert performance["terminal_counts"] == {"succeeded": 4, "failed": 4, "aborted": 0, "timed_out": 0}, (
            "the raw AWS/States counts, so the rate stays reconstructible"
        )
        attribution = performance["stage_failure_attribution"]
        assert attribution["failed_executions"] == 1, "mirrors failed_pipelines"
        assert attribution["failure_records"] == 1
        assert attribution["failed_executions_without_a_record"] == 0
        assert attribution["rejected_by_step"] == {"check_schema_mapping_approval": 3}
        assert performance["determined_from"]["rejected_at_approval"] == metrics_mod.SOURCE_FAILURE_RECORDS
        assert "rejected_at_approval" in performance["measurement"]
        assert "rejected_at_approval" not in performance["missing"]

    def test_an_unreadable_failure_partition_takes_nothing_out(self):
        def query(**kwargs: Any) -> dict[str, Any]:
            if _queried_step_name(kwargs) == metrics_mod._FAILURE_RECORD_STEP_NAME:
                raise RuntimeError("DynamoDB unavailable for the failure partition")
            return {"Items": []}

        state = MagicMock()
        state.query.side_effect = query
        performance = _comprehensive(state, _states_cloudwatch(succeeded=4, failed=4))["performance"]
        assert performance["failed_pipelines"] == 4, "the plain ExecutionsFailed sum"
        assert performance["overall_success_rate"] == 50.0
        assert performance["rejected_at_approval"] is None, "not 0: nobody can say no one rejected anything"
        assert "rejected_at_approval" in performance["missing"]
        assert (
            performance["determined_from"]["rejected_at_approval"] == f"{metrics_mod.SOURCE_FAILURE_RECORDS}_incomplete"
        )

    def test_more_rejections_than_failures_never_goes_below_zero(self):
        """A rejected run on the window boundary can be in the records and not
        in the metric. The count is published as read; what is taken out is
        capped at the failed total."""
        performance = _comprehensive(
            _partitioned_state_table({"pipeline_failure": _recent(self.ROWS[:3])}),
            _states_cloudwatch(succeeded=4, failed=2),
        )["performance"]
        assert performance["rejected_at_approval"] == 3
        assert performance["failed_pipelines"] == 0
        assert performance["overall_success_rate"] == 100.0

    def test_the_success_trend_compares_both_windows_on_one_basis(self):
        """Both windows see 4 succeeded / 4 failed; only the current one has
        rejections. With both rejection reads answering, the current rate is
        80% against a previous 50%, so the trend is up. If the previous
        window's rejections cannot be read, both rates are compared unadjusted,
        50% against 50%, and the trend is stable — never 80% against 50%."""
        state = _partitioned_state_table({"pipeline_failure": _recent(self.ROWS)})
        both_read = _comprehensive(state, _states_cloudwatch(succeeded=4, failed=4))["performance"]
        assert both_read["trends"]["success"] == "up"

        unreadable = metrics_mod.MetricOutcome.incomplete(metrics_mod.SOURCE_FAILURE_RECORDS)
        prev_unread = _comprehensive(
            _partitioned_state_table({"pipeline_failure": _recent(self.ROWS)}),
            _states_cloudwatch(succeeded=4, failed=4),
            _count_approval_rejections=MagicMock(return_value=unreadable),
        )["performance"]
        assert prev_unread["overall_success_rate"] == 80.0, "the headline still uses the current window's count"
        assert prev_unread["trends"]["success"] == "stable"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
