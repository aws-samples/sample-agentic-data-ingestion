# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests for ``_get_stage_metrics`` (``handlers/metrics.py:502``).

p39-metrics-truth task 1.1. **These pin today's behaviour, not a requirement.**
Every assertion here describes behaviour that must SURVIVE the p39 change set —
principally task 11.1, which widens ``_CW_STAGE_NAMES`` from two keys to six and
states the CloudWatch-then-state-row precedence rule. Nothing here asserts a
value p39 intends to change, so this file is expected green both before and
after that task. A red is a finding about the read behind it, not a test to
adjust.

What is pinned, and why each one is load-bearing
------------------------------------------------

1. **The dual-partition query set** (``_STAGE_STEP_NAMES``, ``metrics.py:87-94``).
   The four stages renamed in P28 R8.2 are read from BOTH their canonical
   snake_case partition and their legacy PascalCase partition, and rows from
   either land in the SAME frontend stage counter. ``LGR-9e76249-9``
   (**template / ratified**) is the standing reason this is not tidy-up-able:
   the legacy ``PascalCase`` / pre-rename ``step_name`` question is explicitly
   still open there ("legacy ``PascalCase`` and pre-rename keys that persist in
   live tables"), and its ``revisit_when`` is that question being settled —
   which p39 does not settle. Collapsing ``_STAGE_STEP_NAMES`` to one partition
   per stage silently undercounts pre-rename runs (p39 constraint 7).

   ``test_query_not_scan.py:102-160`` already asserts the Query SHAPE of these
   reads (P28 R17.1) and ``:162-176`` asserts the map's contents. Neither
   asserts a returned VALUE — codegraph flags ``_get_stage_metrics`` as having
   no covering tests for exactly that reason. The pin below is the value-level
   half: a legacy-partition row is COUNTED, under its canonical stage key.

2. **``_SUCCESS_STATUSES`` / ``_ERROR_STATUSES`` counting** (``:100-101``).
   Status is lower-cased before the membership test, the two sets are closed,
   and a status in neither set counts toward neither counter — it is not an
   error by default.

3. **The client-side window re-check** (``:534-536``). The GSI key condition
   already bounds the read (``_query_state_by_step:147-153``), and the handler
   re-checks each row's parsed timestamp anyway. ``_query_state_by_step``'s
   docstring names this as deliberate: "callers keep their client-side
   ``_parse_iso`` checks as the authoritative filter for mixed-suffix edge
   cases", because BETWEEN on ISO strings is lexicographic and a ``Z``-suffixed
   and an offset-aware spelling of the same instant do not sort alike. Bounds
   are inclusive on both ends.

Test approach: the module-level ``state_table`` and ``cloudwatch_client`` are
patched with ``MagicMock`` stubs, the pattern the sibling metrics tests use
(``test_query_not_scan.py:110-125``, ``test_metrics_handler.py:33-44``). No
``moto.mock_aws()`` and no boto3 client mock is created here
(``tests/unit/test_no_duplicate_mocking_prop.py``); there is nothing for the
shared DynamoDB/S3/SQS fixtures to do, because these reads go through a table
resource the handler holds as a module global.

The state stub routes rows BY PARTITION — it reads the ``step_name`` equality
out of the ``KeyConditionExpression`` and returns only the rows seeded for that
partition, the way DynamoDB would. A stub returning one flat list for every
query would hand every row to all ten partition reads and count each row ten
times, which would make the dual-partition pin unable to fail.

Validates: Requirements R6.3, R6.4
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import AttributeBase

# ---------------------------------------------------------------------------
# Path / env setup (same bootstrap as the sibling dashboard_api metrics tests)
# ---------------------------------------------------------------------------
DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(DASHBOARD_API_DIR), os.path.abspath(SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

START = datetime(2025, 3, 1, tzinfo=UTC)
END = datetime(2025, 3, 2, tzinfo=UTC)
MID = START + timedelta(hours=6)
PERIOD = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _condition_leaves(condition) -> list[tuple[str, str, tuple]]:
    """Flatten a boto3 condition tree into ``(operator, attribute, operands)``.

    Same shape as ``test_query_not_scan.py:_leaves``; kept local so this file
    stands alone, since that module is one of the files p39 task 6.2 edits.
    """
    expr = condition.get_expression()
    op = expr["operator"]
    values = expr["values"]
    if op in ("AND", "OR"):
        out: list[tuple[str, str, tuple]] = []
        for sub in values:
            out.extend(_condition_leaves(sub))
        return out
    name = values[0].name if isinstance(values[0], AttributeBase) else str(values[0])
    return [(op, name, tuple(values[1:]))]


def _queried_step_name(query_kwargs: dict) -> str:
    """The single ``step_name`` this Query was bounded to."""
    leaves = _condition_leaves(query_kwargs["KeyConditionExpression"])
    equalities = [operands[0] for op, name, operands in leaves if op == "=" and name == "step_name"]
    assert len(equalities) == 1, f"expected exactly one step_name equality, got {leaves}"
    return equalities[0]


def _partitioned_state_table(rows_by_partition: dict[str, list[dict]]) -> MagicMock:
    """A state-table stub that answers each Query with only its partition's rows.

    Partitions absent from ``rows_by_partition`` answer empty, which is what a
    real table does for a step name nothing has written.
    """
    table = MagicMock()

    def _query(**kwargs):
        return {"Items": list(rows_by_partition.get(_queried_step_name(kwargs), []))}

    table.query.side_effect = _query
    return table


def _empty_cloudwatch() -> MagicMock:
    """A CloudWatch stub with no datapoints for any stage-duration series."""
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": []}
    return client


def _cloudwatch_with(**stage_values: list[float]) -> MagicMock:
    """A CloudWatch stub returning ``Values`` for the named frontend stage keys.

    ``_stage_durations_cw`` reads two reduced series per stage, the summed
    duration ``<stage>_dur_sum`` and the summed sample count ``<stage>_dur_n``,
    and divides them. Each value here is one invocation.
    """
    results = []
    for stage, values in stage_values.items():
        results.append({"Id": f"{stage}_dur_sum", "Values": values})
        results.append({"Id": f"{stage}_dur_n", "Values": [1.0] * len(values)})
    client = MagicMock()
    client.get_metric_data.return_value = {"MetricDataResults": results}
    return client


def _row(step_name: str, status: str, timestamp: datetime | str, **extra) -> dict:
    ts = timestamp if isinstance(timestamp, str) else timestamp.isoformat()
    return {"step_name": step_name, "status": status, "timestamp": ts, **extra}


def _call_stage_metrics(rows_by_partition: dict[str, list[dict]], cloudwatch: MagicMock | None = None) -> dict:
    """The ``stages`` half of the read.

    p59 made `_get_stage_metrics` return `_StageMetricsRead` — the stage map AND
    the failure-record reconciliation, from one pass, because the dedupe that
    keeps the counts honest and the numbers that make them checkable are the same
    arithmetic. Every assertion in this file is about the stage map, so it is
    unwrapped here rather than at 20 call sites.
    ``test_metrics_failure_attribution.py`` owns the other half.
    """
    from handlers import metrics as metrics_mod

    state = _partitioned_state_table(rows_by_partition)
    with (
        patch.object(metrics_mod, "state_table", state),
        patch.object(metrics_mod, "cloudwatch_client", cloudwatch or _empty_cloudwatch()),
    ):
        return metrics_mod._get_stage_metrics(START, END, PERIOD).stages


# ═══════════════════════════════════════════════════════════════════════════
# 1. The dual-partition query set (P28 R8.2, p39 constraint 7)
# ═══════════════════════════════════════════════════════════════════════════


class TestDualPartitionReconciliation:
    """Both step-name partitions are read, and both feed one stage counter."""

    def test_every_partition_in_the_map_is_queried_once(self):
        """One bounded Query per PARTITION — ten stage partitions plus the
        failure-record partition, so eleven for six stages.

        The count matters as much as the set: a stage read twice would double
        every row it holds.

        AMENDED (p59). This asserted exactly the ten `_STAGE_STEP_NAMES`
        partitions, which was the same claim as the comment it mirrored — that
        `pipeline_failure` rows are "naturally outside the queried partitions".
        That exclusion was the defect, not a property to preserve:
        `LGR-9e76249-6` (**template / ratified**) records the sentinel as the ONLY
        carrier of a run's terminal FAILED status when the failing step never
        wrote a row, so a panel that never reads it cannot see most failures. The
        expectation is still derived from the handler's own constants rather than
        transcribed, so a stage added to the map is still covered here.
        """
        from handlers import metrics as metrics_mod

        state = _partitioned_state_table({})
        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
        ):
            metrics_mod._get_stage_metrics(START, END, PERIOD)

        queried = [_queried_step_name(c.kwargs) for c in state.query.call_args_list]
        expected = [step for steps in metrics_mod._STAGE_STEP_NAMES.values() for step in steps]
        expected.append(metrics_mod._FAILURE_RECORD_STEP_NAME)
        assert sorted(queried) == sorted(expected)
        state.scan.assert_not_called()

    def test_legacy_pascalcase_rows_count_under_the_canonical_stage_key(self):
        """A row in the legacy partition lands in the same counter as a
        canonical one, and the two sum rather than either winning.

        This is the assertion ``test_query_not_scan.py`` cannot make: it pins
        that both partitions are *queried*, not that both are *counted*.
        """
        stages = _call_stage_metrics(
            {
                "create_code": [_row("create_code", "success", MID)],
                "CodeGeneration": [
                    _row("CodeGeneration", "success", MID),
                    _row("CodeGeneration", "failed", MID),
                ],
                "create_infrastructure": [_row("create_infrastructure", "error", MID)],
                "CreateInfrastructure": [_row("CreateInfrastructure", "completed", MID)],
                "create_quality_checks": [_row("create_quality_checks", "completed", MID)],
                "CreateQualityChecks": [_row("CreateQualityChecks", "error", MID)],
                "run_pipeline": [_row("run_pipeline", "success", MID)],
                "RunPipeline": [_row("RunPipeline", "success", MID)],
            }
        )

        assert stages["code_generation"] == {"success_count": 2, "error_count": 1, "avg_time_ms": None}
        assert stages["infrastructure"] == {"success_count": 1, "error_count": 1, "avg_time_ms": None}
        assert stages["quality_checks"] == {"success_count": 1, "error_count": 1, "avg_time_ms": None}
        assert stages["pipeline_run"]["success_count"] == 2

    def test_the_two_always_snakecase_stages_have_exactly_one_partition(self):
        """``schema_extract`` / ``schema_mapping`` were never renamed, so they
        carry one partition each — and a PascalCase spelling of either is NOT
        read (``metrics.py:86``)."""
        from handlers import metrics as metrics_mod

        assert metrics_mod._STAGE_STEP_NAMES["schema_extract"] == ("schema_extract",)
        assert metrics_mod._STAGE_STEP_NAMES["schema_mapping"] == ("schema_mapping",)

        stages = _call_stage_metrics(
            {
                "schema_extract": [_row("schema_extract", "success", MID)],
                "SchemaExtract": [_row("SchemaExtract", "success", MID)],
            }
        )
        assert stages["schema_extract"]["success_count"] == 1

    def test_projection_covers_every_attribute_the_counting_reads(self):
        """The projected attribute set must name ``step_name``, ``status``,
        ``timestamp``, ``started_at``, ``completed_at`` and ``run_id``.

        Dropping one from the projection would not raise — ``item_str`` is
        total (``shared/dynamo_typing.py:39-48``) — it would silently zero the
        counts or the ``pipeline_run`` duration.

        AMENDED (p59) twice over. ``run_id`` joined the set: it is the failure
        records' dedupe key, and because a GSI Query with a ProjectionExpression
        returns only the attributes named — table keys are NOT added back — an
        unprojected ``run_id`` would make the dedupe never match and double-count
        every run whose stage recorded its own error. And the loop is now scoped to
        the STAGE partitions: `_FAILURE_RECORD_STEP_NAME`'s own Query projects
        ``run_id,failed_step,timestamp``, a different attribute set for a different
        question, and asserting this one against it would only force the failure
        read to over-project.
        """
        from handlers import metrics as metrics_mod

        state = _partitioned_state_table({})
        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
        ):
            metrics_mod._get_stage_metrics(START, END, PERIOD)

        stage_partitions = {step for steps in metrics_mod._STAGE_STEP_NAMES.values() for step in steps}
        checked = 0
        for call in state.query.call_args_list:
            kwargs = call.kwargs
            if _queried_step_name(kwargs) not in stage_partitions:
                continue
            checked += 1
            projected = {part.strip() for part in kwargs["ProjectionExpression"].split(",")}
            names = kwargs["ExpressionAttributeNames"]
            resolved = {names.get(part, part) for part in projected}
            assert {"run_id", "step_name", "status", "timestamp", "started_at", "completed_at"} <= resolved, (
                f"projection {kwargs['ProjectionExpression']!r} with names {names!r} "
                f"does not cover every attribute the counting reads"
            )
        assert checked == len(stage_partitions), (
            f"only {checked} of {len(stage_partitions)} stage partitions were Queried, so this assertion "
            f"passed on a subset — the scoping filter above is dropping reads it should be checking"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. _SUCCESS_STATUSES / _ERROR_STATUSES counting
# ═══════════════════════════════════════════════════════════════════════════


class TestStatusCounting:
    """The two closed status sets, case-folded, with no default bucket."""

    def test_both_status_sets_are_exactly_these_values(self):
        from handlers import metrics as metrics_mod

        assert sorted(metrics_mod._SUCCESS_STATUSES) == ["completed", "success"]
        assert sorted(metrics_mod._ERROR_STATUSES) == ["error", "failed"]

    @pytest.mark.parametrize("status", ["success", "completed", "SUCCESS", "Completed"])
    def test_success_statuses_count_as_success_case_insensitively(self, status):
        """``status`` is lower-cased before the membership test
        (``metrics.py:540``), so a row written in any case still counts."""
        stages = _call_stage_metrics({"schema_extract": [_row("schema_extract", status, MID)]})
        assert stages["schema_extract"] == {"success_count": 1, "error_count": 0, "avg_time_ms": None}

    @pytest.mark.parametrize("status", ["error", "failed", "ERROR", "Failed"])
    def test_error_statuses_count_as_error_case_insensitively(self, status):
        stages = _call_stage_metrics({"schema_mapping": [_row("schema_mapping", status, MID)]})
        assert stages["schema_mapping"] == {"success_count": 0, "error_count": 1, "avg_time_ms": None}

    @pytest.mark.parametrize("status", ["in_progress", "pending", "skipped", "unknown", "", "succeeded"])
    def test_a_status_in_neither_set_counts_toward_neither(self, status):
        """No default bucket: an in-flight or unrecognised status is not an
        error. ``succeeded`` is in the list on purpose — it is the *execution*
        vocabulary, not the *step* vocabulary, and it is not a success here.
        """
        stages = _call_stage_metrics({"schema_extract": [_row("schema_extract", status, MID)]})
        assert stages["schema_extract"] == {"success_count": 0, "error_count": 0, "avg_time_ms": None}

    def test_a_row_whose_step_name_is_not_in_the_map_is_ignored(self):
        """``pipeline_failure`` rows and anything else outside the map are
        skipped rather than counted against some stage (``metrics.py:537-539``).
        """
        stages = _call_stage_metrics({"schema_extract": [_row("pipeline_failure", "error", MID)]})
        assert stages["schema_extract"] == {"success_count": 0, "error_count": 0, "avg_time_ms": None}

    def test_every_stage_key_is_present_with_all_three_fields(self):
        """The returned map is keyed by ``_STAGE_STEP_NAMES`` and always carries
        the full ``StageMetrics`` shape, including for stages with no rows."""
        from handlers import metrics as metrics_mod

        stages = _call_stage_metrics({})
        assert set(stages) == set(metrics_mod._STAGE_STEP_NAMES)
        for values in stages.values():
            assert set(values) == {"success_count", "error_count", "avg_time_ms"}


# ═══════════════════════════════════════════════════════════════════════════
# 3. The client-side window re-check
# ═══════════════════════════════════════════════════════════════════════════


class TestClientSideWindowRecheck:
    """A row the Query returned is still re-checked against [start, end]."""

    def test_a_row_outside_the_window_is_not_counted(self):
        """The stub returns rows the key condition would not have — which is
        the point: the client-side check is what filters them
        (``_query_state_by_step``'s docstring, ``metrics.py:142-143``).
        """
        stages = _call_stage_metrics(
            {
                "schema_extract": [
                    _row("schema_extract", "success", START - timedelta(minutes=1)),
                    _row("schema_extract", "success", END + timedelta(minutes=1)),
                    _row("schema_extract", "success", MID),
                ]
            }
        )
        assert stages["schema_extract"]["success_count"] == 1

    def test_both_window_bounds_are_inclusive(self):
        """``ts < start or ts > end`` — a row exactly on either bound counts."""
        stages = _call_stage_metrics(
            {
                "schema_extract": [
                    _row("schema_extract", "success", START),
                    _row("schema_extract", "success", END),
                ]
            }
        )
        assert stages["schema_extract"]["success_count"] == 2

    @pytest.mark.parametrize("timestamp", ["", "not-a-timestamp", "2025-13-45T99:99:99"])
    def test_an_unparseable_timestamp_is_skipped_not_counted(self, timestamp):
        """``_parse_iso`` returns ``None`` (``metrics.py:859-875``) and the row
        is dropped rather than defaulting into the window."""
        stages = _call_stage_metrics({"schema_extract": [_row("schema_extract", "success", timestamp)]})
        assert stages["schema_extract"]["success_count"] == 0

    def test_a_missing_timestamp_attribute_is_skipped(self):
        stages = _call_stage_metrics({"schema_extract": [{"step_name": "schema_extract", "status": "success"}]})
        assert stages["schema_extract"]["success_count"] == 0

    @pytest.mark.parametrize(
        "spelling",
        [
            "2025-03-01T06:00:00",  # naive — assumed UTC
            "2025-03-01T06:00:00Z",  # Z suffix
            "2025-03-01T06:00:00+00:00",  # offset-aware
            "2025-03-01T01:00:00-05:00",  # same instant, other offset
        ],
    )
    def test_every_tolerated_timestamp_spelling_lands_inside_the_window(self, spelling):
        """All four spellings ``_parse_iso`` tolerates resolve to an instant
        inside [start, end] and count. This is the mixed-suffix case the
        client-side re-check exists for — lexicographic BETWEEN cannot compare
        an offset-aware spelling with a ``Z``-suffixed one.
        """
        stages = _call_stage_metrics({"schema_extract": [_row("schema_extract", "success", spelling)]})
        assert stages["schema_extract"]["success_count"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 4. Durations — the two sources, and absence reported as None (R6.3, R6.4)
# ═══════════════════════════════════════════════════════════════════════════


class TestStageDurations:
    """The CloudWatch arm, the ``pipeline_run`` state-row arm, and absence.

    Deliberately NOT pinned: that ``code_generation`` and friends are always
    ``None``. Task 11.1 maps all six stages to a CloudWatch ``StageName``, and
    a stage with a datapoint must then report it (``R6.3``). What is pinned is
    the R6.4 half — no datapoint anywhere means ``None``, never a zero.
    """

    def test_a_cloudwatch_average_lands_on_its_own_stage_key(self):
        stages = _call_stage_metrics(
            {},
            cloudwatch=_cloudwatch_with(schema_extract=[100.0, 200.0], schema_mapping=[50.0]),
        )
        assert stages["schema_extract"]["avg_time_ms"] == 150.0
        assert stages["schema_mapping"]["avg_time_ms"] == 50.0

    def test_no_datapoint_anywhere_reports_none_never_zero(self):
        """R6.4: absence is ``None``, which the frontend renders as N/A."""
        stages = _call_stage_metrics({})
        for stage, values in stages.items():
            assert values["avg_time_ms"] is None, f"{stage} reported {values['avg_time_ms']!r} for no datapoint"

    def test_pipeline_run_duration_is_the_mean_of_state_row_spans(self):
        """``pipeline_run`` is derived from ``started_at``/``completed_at`` on
        its own state rows, averaged and rounded to one decimal
        (``metrics.py:545-549`` and ``:554``). Task 11.1 keeps this as its source.
        """
        stages = _call_stage_metrics(
            {
                "run_pipeline": [
                    _row(
                        "run_pipeline",
                        "success",
                        MID,
                        started_at="2025-03-01T06:00:00Z",
                        completed_at="2025-03-01T06:00:02Z",
                    ),
                    _row(
                        "run_pipeline",
                        "success",
                        MID,
                        started_at="2025-03-01T06:10:00Z",
                        completed_at="2025-03-01T06:10:04Z",
                    ),
                ]
            }
        )
        assert stages["pipeline_run"]["avg_time_ms"] == 3000.0

    def test_a_span_ending_before_it_started_is_excluded(self):
        """The ``c_at >= s_at`` guard: a negative span is dropped, not
        averaged in as a negative duration. With no usable span left the stage
        reports ``None``."""
        stages = _call_stage_metrics(
            {
                "run_pipeline": [
                    _row(
                        "run_pipeline",
                        "success",
                        MID,
                        started_at="2025-03-01T06:00:05Z",
                        completed_at="2025-03-01T06:00:00Z",
                    )
                ]
            }
        )
        assert stages["pipeline_run"]["avg_time_ms"] is None

    def test_a_row_missing_either_bound_contributes_no_span(self):
        stages = _call_stage_metrics(
            {
                "run_pipeline": [
                    _row("run_pipeline", "success", MID, started_at="2025-03-01T06:00:00Z"),
                    _row("run_pipeline", "success", MID, completed_at="2025-03-01T06:00:02Z"),
                ]
            }
        )
        assert stages["pipeline_run"]["success_count"] == 2
        assert stages["pipeline_run"]["avg_time_ms"] is None


# ═══════════════════════════════════════════════════════════════════════════
# 5. Totality — a failing read degrades, it does not raise
# ═══════════════════════════════════════════════════════════════════════════


class TestFailingReadsDegrade:
    """``_get_stage_metrics`` returns the full stage map whatever the reads do."""

    def test_a_raising_state_query_yields_zero_counts_and_no_duration(self):
        """The ``except`` at ``metrics.py:550-551`` logs at WARNING and the
        function still returns every stage. R4.12's "absence is a return value,
        not an exception" already holds on this path.

        p59: this stub raises on EVERY Query, so both reads fail, and the
        attribution half reports `_incomplete` rather than a measured zero —
        asserted here so a future refactor cannot quietly make a failed read look
        like "no failures" (`LGR-4854b3d-1`, template / ratified).
        """
        from handlers import metrics as metrics_mod

        state = MagicMock()
        state.query.side_effect = RuntimeError("DynamoDB unavailable")

        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", _empty_cloudwatch()),
            patch.object(metrics_mod.logger, "warning") as mock_warning,
        ):
            read = metrics_mod._get_stage_metrics(START, END, PERIOD)

        assert set(read.stages) == set(metrics_mod._STAGE_STEP_NAMES)
        for values in read.stages.values():
            assert values == {"success_count": 0, "error_count": 0, "avg_time_ms": None}
        assert read.attribution.outcome.value is None
        assert read.attribution.outcome.determined_from == f"{metrics_mod.SOURCE_FAILURE_RECORDS}_incomplete"
        mock_warning.assert_called()

    def test_a_raising_cloudwatch_query_leaves_the_state_counts_intact(self):
        """The CloudWatch failure is contained in ``_stage_durations_cw``
        (``metrics.py:600-601``), so counts read from the state table survive
        it and durations report ``None``."""
        from handlers import metrics as metrics_mod

        cloudwatch = MagicMock()
        cloudwatch.get_metric_data.side_effect = RuntimeError("CloudWatch unavailable")
        state = _partitioned_state_table({"schema_extract": [_row("schema_extract", "success", MID)]})

        with (
            patch.object(metrics_mod, "state_table", state),
            patch.object(metrics_mod, "cloudwatch_client", cloudwatch),
        ):
            stages = metrics_mod._get_stage_metrics(START, END, PERIOD).stages

        assert stages["schema_extract"]["success_count"] == 1
        assert stages["schema_extract"]["avg_time_ms"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
