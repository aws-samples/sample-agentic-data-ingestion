# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Requirement-derived cost tests, and the structural guards the fallback left behind.

Feature: p39-metrics-truth, task 6.3. Subjects: ``_get_estimated_monthly_cost``
and its two call sites in ``_build_comprehensive_metrics`` (task 6.1), plus the
absence of everything task 6.2 deleted from ``handlers/metrics.py``.

WHERE THE EXPECTATIONS COME FROM
--------------------------------
**Every expectation here is written from ``R2.3``–``R2.7``, and none of it from
what this function returned before p39.** The pre-p39 figure was a sum of
``GlueJob.EstimatedCostPerRun``, published once per Glue job *created* inside
``publish_glue_job_creation_metrics``'s ``if creation_success:`` branch
(``metrics_facade.py:645``; callers ``job_builders.py:761`` and ``:785``) — a
creation counter priced at a flat estimate, under a tile labelled with a
currency. Its trend was arithmetic on two windows overlapping by 29 of 30 days,
and its fallback lower-bounded ``started_at`` while filtering ``updated_at``
with ``ts >= start`` only. **None of those numbers appears below as a baseline**
(`LGR-28b4d75-5`, **template / ratified**: a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle). Where the old behaviour appears at all it is asserted *against* — the
overlap is what :class:`TestTheTwoWindowsAreComparable` forbids, and the
ignored lower bound is what :class:`TestBothBoundsReachTheQuery` forbids.

WHAT IS ASSERTED, CLAUSE BY CLAUSE
----------------------------------
=========  =====================================================================
``R2.3``   the current and previous cost reads are **adjacent, disjoint, equal
           in length, taken at the same period, and read the same metric name**
``R2.4``   a window with no datapoint yields ``None`` with an ``_absent`` token
           **and** a ``not_established`` cost trend; a *measured* ``0.0`` over
           datapoints that exist is a measurement and is none of those things
``R2.5``   both bounds reach the query — the window is the caller's, and
           neither bound is re-derived inside the function
``R2.6``   ``basis`` bounds equal the bounds **actually queried**, and ``basis``
           names the metric that was actually searched
``R2.7``   ``basis`` states the figure is a DPU-minutes estimate with the
           10-minute per-run minimum, and is **not measured spend**
``R8.2``   ``metrics.py`` enumerates no execution status, names no
           ``StatusIndex``, reads no executions table, and Scans nothing
=========  =====================================================================

``R2.7`` is not in task 6.3's own sentence and is asserted anyway: it is the
requirement the ``note`` key exists to satisfy, and a ``basis`` without it is a
dict of numbers with no warning attached. Nothing in this system measures spend
(requirements constraint 14) and ``R2.7`` requires that stated rather than
implied.

WHY THE STRUCTURAL GUARDS ARE AST WALKS AND NOT SUBSTRING SEARCHES
------------------------------------------------------------------
Task 6.2 removed the flat-rate fallback and, with it, the ``RUNNING``
enumeration that was this handler's only contact with p38's phantom rows. It
also left **prose naming what it removed**, which is what breaks the obvious
guard:

* ``metrics.py:290-298`` is a comment block that names ``StatusIndex``,
  ``RUNNING`` and the executions table, explaining why none of them is read any
  more and pointing at ``handlers/health.py:59-73`` as the remaining reader
  (out of p39's scope, requirements constraint 10);
* ``_get_estimated_monthly_cost``'s docstring at ``:1325`` names ``SUCCEEDED``,
  ``FAILED``, ``STOPPED`` and ``TIMEOUT`` — the terminal states the publisher
  fires on, which is ``R2.8``'s cadence statement.

A substring scan over the file source fails on both, so it "forbids documenting
the fence, and failed for exactly that reason" — `LGR-b46a5bb-4`'s own words
(**template / ratified**) about the ``threading.Thread`` scan, where the same
trap was met and answered the same way. Comments never reach the AST at all and
docstrings are excluded here by node identity, so prose is invisible to these
detectors *structurally* rather than by luck.
:func:`test_a_substring_scan_would_fail_on_the_modules_own_prose` demonstrates
the difference rather than asserting it, against the module actually on disk.
``test_metrics_polarity.py`` (task 2.3) and ``test_metrics_storage.py``
(task 4.2) solved the same problem for ``R3.2`` and ``R1.5``; the docstring and
binding detectors below are the same shape.

GUARDS WRITTEN TO SURVIVE THE CHANGES THIS SPEC HAS ALREADY SCHEDULED
---------------------------------------------------------------------
Nothing here depends on a CloudWatch query **id** or on the ``period``
argument. The stub reads the module's own ``MetricDataQueries`` and replies in
the protocol it finds — a returned reduction series is matched to its raw
``SEARCH`` by the id its ``Expression`` references, and the ``'Sum'`` /
``'SampleCount'`` statistic in that ``SEARCH`` decides which values it gets. So
``rc_total`` / ``rc_runs`` may be renamed, reordered, or given a different
period and these tests still speak to the function. The cost read is located by
the **metric name** it searches, for the same reason. Task 6.1 added the third
``period`` parameter because CloudWatch keeps 1-minute data for only 15 days
while the ``30d`` window's previous half reaches 60 days back; both the
two-argument and three-argument call forms are exercised.

WHAT THIS FILE DELIBERATELY DOES NOT DO
---------------------------------------
* **No Hypothesis.** Task 21.5 owns Property 6 — the generated-input form of
  the ``basis`` key set and the window comparability. What is here is example
  coverage of the requirement clauses plus the structural guards.
* **No assertion on the SEARCH expression's composition**, its dimension order,
  or the reduction's shape. ``R2.3`` asks that both windows read the *same*
  metric name; that they read it through one ``SEARCH`` per statistic is task
  6.1's choice.
* **No claim about ``handlers/health.py``**, which still enumerates statuses
  against ``IndexName="StatusIndex"`` and is out of p39's change set
  (requirements constraint 10). The guards below are scoped to ``metrics.py``.
* **No live AWS.**

MOCKING
-------
CloudWatch is outside the shared fixtures' scope (they cover DynamoDB / S3 /
SQS), so the module-level ``cloudwatch_client`` is patched with ``MagicMock`` —
the established pattern for these read paths (``test_query_not_scan.py``,
``test_metrics_storage.py`` and the task-1 characterization files). No
``mock_aws()`` is created here and no fixture-covered service is stubbed by
hand; ``state_table`` is patched because ``_get_rows_loaded`` and
``_get_stage_metrics`` Query its GSI on every payload build (P28 R17.1).
``AWS_REGION`` is deliberately **not** set — the unit layer pins it by
assignment in ``tests/unit/conftest.py`` (`LGR-3e45e65-1`, **template /
ratified**).

**Expected result: green on the first run.** Tasks 6.1 and 6.2 have both
landed, so the expected-red window this task's entry names is closed. A red
here is a real finding about the read behind the test, not a test to adjust.

**Validates: Requirements 2.3, 2.4, 2.5, 2.6, 2.7, 8.2**

_Requirements: R2.3, R2.4, R2.5, R2.6, R2.7, R8.2_
"""

import ast
import os
import pathlib
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason test_metrics_storage.py
# and the task-1 characterization files repeat it — under xdist --dist=loadfile
# any file in this directory may be the first one imported on a worker.
# ---------------------------------------------------------------------------
_DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(_DASHBOARD_API_DIR), os.path.abspath(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# handlers.common resolves its table names through require_env at import time;
# the sanctioned development switch is AGENTIC_DATA_INGESTION_ENV=local (same
# bootstrap as test_query_not_scan.py).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

METRICS_PATH = pathlib.Path(os.path.abspath(metrics_mod.__file__))
METRICS_SOURCE = METRICS_PATH.read_text()


# ═══════════════════════════════════════════════════════════════════════════
# Vocabulary
# ═══════════════════════════════════════════════════════════════════════════

#: R4.2's two suffixes and R4.4's trend token, written out literally because the
#: requirements fix these words. The source *name* comes from the module's own
#: SOURCE_* constant, because no requirement fixes that.
ABSENT = "_absent"
INCOMPLETE = "_incomplete"
NOT_ESTABLISHED = "not_established"

#: The metric R2.1 moves the figure onto — the run-completion publisher's, not
#: the job-creation publisher's. Named literally: this is the requirement's own
#: subject, and reading it out of the module would make the assertion circular.
COST_METRIC_NAME = "GlueJob.RunCostEstimate"

#: The metric the pre-p39 read summed. Asserted **against**: R2.1 moves off it,
#: so its continued presence in the cost read would be the defect.
CREATION_TIME_METRIC_NAME = "GlueJob.EstimatedCostPerRun"

#: R2.7's own number — "including the 10-minute DPU minimum applied at
#: job_completion:113-131". The rate is not pinned here (no requirement fixes
#: 0.44; it is the producer's constant, reported and applied nowhere), only that
#: it is present and positive.
DPU_MINIMUM_MINUTES = 10

#: The closed execution-status set the deleted fallback enumerated on
#: StatusIndex, one bounded Query each. Written out because it is the shape
#: R8.2 forbids re-introducing, and because it is gone from the module — there
#: is nothing left to read it from. `RUNNING` is the member that mattered: the
#: seven phantom rows p38 owns sit permanently at that status, so this
#: enumeration counted each of them once per window for as long as they exist.
DELETED_EXECUTION_STATUSES = (
    "QUEUED",
    "RUNNING",
    "IN_PROGRESS",
    "WAITING_APPROVAL",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "UNKNOWN",
)

#: The executions-table GSI the fallback read (PK `status`, SK `started_at`).
STATUS_INDEX_NAME = "StatusIndex"

#: Every DynamoDB resource attribute `metrics.py` is permitted to call. Task 6.2
#: left exactly two: the state table's windowed GSI Queries (P28 R17.1) and the
#: response cache's get/put.
#:
#: AMENDED BY p47 TASK D3 — `cache_table` added, and the amendment answered
#: rather than absorbed. p47 R7.2 forbids a second caching mechanism, so
#: `handlers/stats.py` calls this module's `_get_cached_metrics` / `_cache_metrics`
#: and passes its own table handle through a new keyword-only `table` parameter.
#: Both helpers therefore resolve a local `cache_table` before use, and this guard
#: correctly noticed that the module's table-access surface changed.
#:
#: Adding the name alone would give up the "which binding" precision this set
#: exists for, so `test_metrics_binds_the_cache_table_local_from_nothing_else`
#: below pins what `cache_table` may be assigned from. The guard's substantive
#: claims are untouched and still separately asserted: no `executions_table`
#: import or binding, no status enumeration, and no `scan` on *any* receiver
#: including this one. Recorded per `LGR-60c4985-6` — an amended assertion stays
#: legible as a deliberate change.
PERMITTED_TABLE_RECEIVERS = frozenset({"state_table", "_cache_table", "cache_table"})

#: The only two things the `cache_table` local may be bound from: this module's
#: own `_cache_table`, or the `table` parameter the second caller passes.
PERMITTED_CACHE_TABLE_SOURCES = frozenset({"_cache_table", "table"})

#: Table operations that read or write. `scan` is deliberately in the list so
#: the detector *sees* it and the Scan guard can reject it by name, rather than
#: a Scan slipping past a permitted-operations allowlist.
TABLE_OPERATIONS = frozenset(
    {"query", "scan", "get_item", "put_item", "update_item", "delete_item", "batch_get_item", "batch_write_item"}
)

REMEDIATION_R82 = (
    "p39 R8.1/R8.2: metrics.py relies on p38 for phantom-row exclusion and introduces no filtering of "
    "its own. Task 6.2 deleted the flat-rate cost fallback, and with it the only executions-table read "
    "in this module — the StatusIndex enumeration over a closed status set including RUNNING, which "
    "counted each of p38's phantom rows once per window. Do not re-introduce an executions-table read, "
    "a status enumeration, or a Scan here (requirements constraint 6, P28 R17.1). handlers/health.py is "
    "the remaining StatusIndex reader and is out of p39's change set (constraint 10)."
)

#: Windows chosen to make the pre-p39 overlap visible if it ever returns. The
#: old function derived a trailing 30 days from whichever `end` it was given, so
#: on a 1-hour or 24-hour period the two windows overlapped almost completely
#: while on a 30-day period they overlapped not at all — the defect was
#: length-dependent, so adjacency is asserted across lengths.
END = datetime(2025, 4, 2, 12, 0, tzinfo=UTC)
WINDOW_LENGTHS = [
    pytest.param(timedelta(hours=1), 60, id="1h"),
    pytest.param(timedelta(hours=24), 300, id="24h"),
    pytest.param(timedelta(days=7), 3600, id="7d"),
    pytest.param(timedelta(days=30), 3600, id="30d"),
]


def absent_token() -> str:
    """``glue_run_cost_metric_absent`` — "we looked and there is nothing"."""
    return f"{metrics_mod.SOURCE_GLUE_RUN_COST}{ABSENT}"


def incomplete_token() -> str:
    """``glue_run_cost_metric_incomplete`` — "we could not look"."""
    return f"{metrics_mod.SOURCE_GLUE_RUN_COST}{INCOMPLETE}"


# ═══════════════════════════════════════════════════════════════════════════
# A CloudWatch stub that speaks the module's protocol rather than its ids
# ═══════════════════════════════════════════════════════════════════════════

_SEARCH_RE = re.compile(r"SEARCH\(\s*'(?P<series>(?:[^']|'')*)'\s*,\s*'(?P<stat>[^']*)'")


def _search_terms(queries: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Every ``SEARCH`` in ``queries`` as ``(query id, series, statistic)``."""
    terms: list[tuple[str, str, str]] = []
    for query in queries:
        match = _SEARCH_RE.search(query.get("Expression", "") or "")
        if match:
            terms.append((query["Id"], match.group("series"), match.group("stat")))
    return terms


def _is_cost_read(queries: list[dict[str, Any]]) -> bool:
    """True when ``queries`` search the run-cost metric.

    Located by metric NAME rather than by query id, so a rename of ``rc_raw`` /
    ``rc_total`` leaves every test in this file working.
    """
    return any(COST_METRIC_NAME in series for _, series, _ in _search_terms(queries))


def _cost_reply(
    queries: list[dict[str, Any]],
    *,
    cost_values: list[float],
    run_values: list[float],
) -> dict[str, Any]:
    """A ``get_metric_data`` reply for a cost read, keyed on the module's own ids.

    Each returned (non-``ReturnData: False``) query is matched to the raw
    ``SEARCH`` its ``Expression`` references, and the statistic in that
    ``SEARCH`` decides what it gets: the ``'Sum'`` series carries the cost
    values, the ``'SampleCount'`` series the run counts. Nothing here depends on
    an id spelling or on the order the queries appear in.
    """
    stat_by_id = {query_id: stat for query_id, _, stat in _search_terms(queries)}
    results: list[dict[str, Any]] = []
    for query in queries:
        if not query.get("ReturnData", True):
            continue
        expression = query.get("Expression", "") or ""
        referenced = [raw_id for raw_id in stat_by_id if re.search(rf"\b{re.escape(raw_id)}\b", expression)]
        stat = stat_by_id[referenced[0]] if referenced else None
        values = cost_values if stat == "Sum" else run_values if stat == "SampleCount" else []
        results.append(
            {
                "Id": query["Id"],
                "Label": expression,
                "Timestamps": [END for _ in values],
                "Values": list(values),
                "StatusCode": "Complete",
            }
        )
    return {"MetricDataResults": results}


EMPTY_REPLY: dict[str, Any] = {"MetricDataResults": []}


class CostRead(NamedTuple):
    """One or more calls to the cost read, and what each of them asked for."""

    outcome: Any
    calls: list[dict[str, Any]]
    logger: MagicMock

    @property
    def call(self) -> dict[str, Any]:
        """The single ``get_metric_data`` call this read issued."""
        assert len(self.calls) == 1, f"expected exactly one get_metric_data call, got {len(self.calls)}"
        return self.calls[0]

    @property
    def queried_start(self) -> datetime:
        return self.call["StartTime"]

    @property
    def queried_end(self) -> datetime:
        return self.call["EndTime"]


def run_cost(
    start: datetime,
    end: datetime,
    *,
    period: int | None = None,
    cost_values: list[float] | None = None,
    run_values: list[float] | None = None,
    response: dict[str, Any] | None = None,
    side_effect: Exception | None = None,
) -> CostRead:
    """Call ``_get_estimated_monthly_cost`` over ``[start, end]``.

    ``period`` left as ``None`` exercises the two-argument call form, which task
    6.1 kept valid alongside the three-argument one the payload builder uses.
    """
    mock_cw = MagicMock()
    if side_effect is not None:
        mock_cw.get_metric_data.side_effect = side_effect
    elif response is not None:
        mock_cw.get_metric_data.return_value = response
    else:
        mock_cw.get_metric_data.side_effect = lambda **kwargs: _cost_reply(
            kwargs["MetricDataQueries"],
            cost_values=cost_values or [],
            run_values=run_values or [],
        )
    args = (start, end) if period is None else (start, end, period)
    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "logger") as mock_logger,
    ):
        outcome = metrics_mod._get_estimated_monthly_cost(*args)
    return CostRead(
        outcome=outcome,
        calls=[call.kwargs for call in mock_cw.get_metric_data.call_args_list],
        logger=mock_logger,
    )


class PayloadRun(NamedTuple):
    """One ``_build_comprehensive_metrics`` pass and the cost reads inside it."""

    payload: dict[str, Any]
    cost_calls: list[dict[str, Any]]

    @property
    def performance(self) -> dict[str, Any]:
        return self.payload["performance"]

    @property
    def cost_trend(self) -> str:
        return self.performance["trends"]["cost"]

    @property
    def windows(self) -> list[tuple[datetime, datetime]]:
        """The cost windows actually queried, earliest first."""
        return sorted((call["StartTime"], call["EndTime"]) for call in self.cost_calls)


def run_payload(
    start: datetime,
    end: datetime,
    cw_period: int,
    *,
    current: tuple[list[float], list[float]] | None = None,
    previous: tuple[list[float], list[float]] | None = None,
) -> PayloadRun:
    """Build the whole payload, answering only the cost reads with data.

    Every other ``get_metric_data`` call gets an empty reply, so every other
    tile is Not_Established — which is R4.12's own case and keeps the cost
    assertions from depending on any of them. The cost reads are told apart by
    the bounds they ask for rather than by call order: the current window is the
    one whose ``StartTime`` is ``start``.
    """
    current_values = current if current is not None else ([], [])
    previous_values = previous if previous is not None else ([], [])
    cost_calls: list[dict[str, Any]] = []

    def _reply(**kwargs: Any) -> dict[str, Any]:
        queries = kwargs.get("MetricDataQueries", [])
        if not _is_cost_read(queries):
            return EMPTY_REPLY
        cost_calls.append(kwargs)
        values = current_values if kwargs.get("StartTime") == start else previous_values
        return _cost_reply(queries, cost_values=values[0], run_values=values[1])

    mock_cw = MagicMock()
    mock_cw.get_metric_data.side_effect = _reply
    mock_state = MagicMock()
    mock_state.query.return_value = {"Items": []}
    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "state_table", mock_state),
    ):
        payload = metrics_mod._build_comprehensive_metrics(start, end, cw_period, "test")
    return PayloadRun(payload=payload, cost_calls=cost_calls)


# ═══════════════════════════════════════════════════════════════════════════
# 1. The two windows are comparable (R2.3)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheTwoWindowsAreComparable:
    """R2.3: current and previous are computed "from the same source, so that the
    two windows are comparable".

    Comparability is asserted as four separate facts because a defect can break
    any one of them alone: two windows can be disjoint without being adjacent
    (a gap loses runs), adjacent without being equal in length (the sums are
    over different amounts of time), equal in length while read at different
    periods (different granularity, different retention), and all three while
    reading different metric names — which is precisely what the pre-p39 code
    did not do and what makes the trend arithmetic meaningful.
    """

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_the_cost_figure_is_read_exactly_twice(self, length, cw_period):
        """One read per window, and no third read anchoring a window of its own."""
        run = run_payload(END - length, END, cw_period)
        assert len(run.cost_calls) == 2, (
            f"R2.3 needs one cost read per window — the current one and the previous one — got "
            f"{len(run.cost_calls)}: {run.windows!r}"
        )

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_both_windows_read_the_same_metric_name(self, length, cw_period):
        """R2.3: "from the same source". Two windows over two metrics is not a trend."""
        run = run_payload(END - length, END, cw_period)
        searched = [{series for _, series, _ in _search_terms(call["MetricDataQueries"])} for call in run.cost_calls]
        assert all(any(COST_METRIC_NAME in series for series in call_series) for call_series in searched), (
            f"both cost windows must read {COST_METRIC_NAME} (R2.1, R2.3), got {searched!r}"
        )
        assert searched[0] == searched[1], (
            f"the two cost windows must read the SAME series, got {searched[0]!r} and {searched[1]!r}"
        )
        assert not any(CREATION_TIME_METRIC_NAME in series for cs in searched for series in cs), (
            f"{CREATION_TIME_METRIC_NAME} is published once per job CREATED, not once per run — R2.1 moves "
            f"the figure off it"
        )

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_the_two_windows_are_adjacent(self, length, cw_period):
        """The previous window ends exactly where the current one starts.

        A gap between them is not a "previous window" — runs falling in the gap
        are counted in neither, so a trend computed across it compares two
        samples of a series with a hole in it.
        """
        run = run_payload(END - length, END, cw_period)
        (prev_start, prev_end), (cur_start, cur_end) = run.windows
        assert prev_end == cur_start, (
            f"R2.3: the previous window must end where the current one begins — "
            f"previous [{prev_start}, {prev_end}], current [{cur_start}, {cur_end}]"
        )

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_the_two_windows_are_disjoint(self, length, cw_period):
        """Neither window's interior contains any part of the other's.

        This is the clause the pre-p39 code failed: the function derived a
        trailing 30 days from whichever ``end`` it was handed, so on a 24-hour
        period the two windows overlapped by 29 of 30 days and "previous" was
        the same sum minus a day.
        """
        run = run_payload(END - length, END, cw_period)
        (prev_start, prev_end), (cur_start, cur_end) = run.windows
        overlap = min(prev_end, cur_end) - max(prev_start, cur_start)
        assert overlap <= timedelta(0), (
            f"R2.3: the two cost windows must be disjoint — previous [{prev_start}, {prev_end}] and current "
            f"[{cur_start}, {cur_end}] overlap by {overlap}"
        )

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_the_two_windows_are_the_same_length(self, length, cw_period):
        """A sum over an hour and a sum over a month are not comparable figures."""
        run = run_payload(END - length, END, cw_period)
        (prev_start, prev_end), (cur_start, cur_end) = run.windows
        assert prev_end - prev_start == cur_end - cur_start == length, (
            f"R2.3: both cost windows must span the requested length {length} — previous "
            f"{prev_end - prev_start}, current {cur_end - cur_start}"
        )

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_both_windows_are_read_at_the_same_period(self, length, cw_period):
        """Same granularity on both sides, whatever that granularity is.

        The value is not pinned — task 6.1 chose it per window and may choose
        differently — only that the two windows agree, since CloudWatch's
        retention and rounding both follow from the period.
        """
        run = run_payload(END - length, END, cw_period)
        periods = [
            {query.get("Period") for query in call["MetricDataQueries"] if query.get("Period") is not None}
            for call in run.cost_calls
        ]
        # Non-empty first: two empty sets are equal, and a guard that compares
        # nothing to nothing passes for free.
        assert periods[0], f"the cost read must state a period, got {run.cost_calls[0]['MetricDataQueries']!r}"
        assert periods[0] == periods[1], (
            f"R2.3: both cost windows must be read at the same period, got {periods[0]!r} and {periods[1]!r}"
        )

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_the_current_window_is_the_one_the_caller_asked_for(self, length, cw_period):
        """The caller's bounds reach CloudWatch unchanged (R2.3, R2.5).

        Adjacency and equal length would both hold for two windows the function
        invented for itself. This is what says it invented neither.
        """
        start = END - length
        run = run_payload(start, END, cw_period)
        assert (start, END) in run.windows, (
            f"the current cost window must be the caller's [{start}, {END}], got {run.windows!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Both bounds reach the query (R2.5)
# ═══════════════════════════════════════════════════════════════════════════


class TestBothBoundsReachTheQuery:
    """R2.5: "apply both a lower bound and an upper bound to the timestamp filter".

    Asserted at the read itself rather than through the payload, because this is
    a statement about what the function does with its arguments. The pre-p39
    fallback lower-bounded ``started_at`` but filtered ``updated_at`` with
    ``ts >= start`` only, so a row updated after the window's end still counted.
    """

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_both_bounds_are_the_ones_passed_in(self, length, cw_period):
        start = END - length
        read = run_cost(start, END, period=cw_period, cost_values=[1.5], run_values=[3.0])
        assert read.queried_start == start, (
            f"R2.5: the lower bound must be the caller's {start}, got {read.queried_start}"
        )
        assert read.queried_end == END, f"R2.5: the upper bound must be the caller's {END}, got {read.queried_end}"

    def test_the_lower_bound_is_not_ignored(self):
        """Two calls sharing an ``end`` and differing in ``start`` query differently.

        The clause this catches is the one the old signature made structural: it
        took only ``end`` and derived its own trailing 30 days, so ``start`` had
        no effect it could have. A single-window assertion cannot see that — the
        derived window would coincidentally match for the 30-day case.
        """
        short = run_cost(END - timedelta(hours=1), END, cost_values=[1.0])
        long = run_cost(END - timedelta(days=30), END, cost_values=[1.0])
        assert short.queried_start != long.queried_start, (
            "R2.5: two windows with different lower bounds must issue different queries — a function that "
            "anchors its own window from `end` ignores `start` entirely"
        )
        assert short.queried_end == long.queried_end == END

    def test_the_window_is_not_re_anchored_to_thirty_days(self):
        """A 1-hour request queries 1 hour, not a trailing month."""
        start = END - timedelta(hours=1)
        read = run_cost(start, END, cost_values=[1.0])
        assert read.queried_end - read.queried_start == timedelta(hours=1), (
            f"R2.5: the queried window must be the requested one, got {read.queried_end - read.queried_start}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. `basis` bounds equal the bounds actually queried (R2.6)
# ═══════════════════════════════════════════════════════════════════════════


class TestBasisBoundsEqualTheBoundsQueried:
    """R2.6: ``basis`` names "what was summed and over what window".

    The bounds are compared against the ``StartTime`` / ``EndTime`` the function
    **sent to CloudWatch**, not against the arguments it was given. Those are
    the same today, and asserting the queried pair is what keeps them so: a
    ``basis`` re-derived from anything other than the bounds of the read it
    annotates would tell a reader they are looking at a window nobody queried.
    """

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_basis_bounds_are_the_queried_bounds(self, length, cw_period):
        start = END - length
        read = run_cost(start, END, period=cw_period, cost_values=[2.5, 1.0], run_values=[4.0])
        basis = read.outcome.basis
        assert basis is not None, "a measured figure must carry a basis (R2.6)"
        assert metrics_mod._parse_iso(basis["window_start"]) == read.queried_start, (
            f"R2.6: basis window_start {basis['window_start']!r} must be the bound actually queried "
            f"({read.queried_start})"
        )
        assert metrics_mod._parse_iso(basis["window_end"]) == read.queried_end, (
            f"R2.6: basis window_end {basis['window_end']!r} must be the bound actually queried ({read.queried_end})"
        )

    def test_basis_bounds_hold_on_the_two_argument_call_form(self):
        """Task 6.1 kept a two-argument call valid; the basis must not depend on the period."""
        start = END - timedelta(days=1)
        read = run_cost(start, END, cost_values=[7.0], run_values=[2.0])
        basis = read.outcome.basis
        assert metrics_mod._parse_iso(basis["window_start"]) == read.queried_start, (
            f"R2.6: basis window_start {basis['window_start']!r} must be the queried bound ({read.queried_start})"
        )
        assert metrics_mod._parse_iso(basis["window_end"]) == read.queried_end, (
            f"R2.6: basis window_end {basis['window_end']!r} must be the queried bound ({read.queried_end})"
        )

    def test_basis_names_the_metric_that_was_searched(self):
        """R2.6: "naming what was summed". The name must be the one queried."""
        read = run_cost(END - timedelta(days=1), END, cost_values=[3.0], run_values=[1.0])
        searched = {series for _, series, _ in _search_terms(read.call["MetricDataQueries"])}
        assert read.outcome.basis["metric"] == COST_METRIC_NAME, (
            f"R2.1/R2.6: the basis must name the run-completion metric {COST_METRIC_NAME!r}, got "
            f"{read.outcome.basis['metric']!r}"
        )
        assert any(read.outcome.basis["metric"] in series for series in searched), (
            f"basis names {read.outcome.basis['metric']!r} but the read searched {searched!r} (R2.6)"
        )

    def test_basis_reports_the_run_count_the_sum_is_over(self):
        """R2.6: "what was summed" includes how many runs it was summed over."""
        read = run_cost(END - timedelta(days=1), END, cost_values=[0.147, 0.294], run_values=[1.0, 2.0])
        assert read.outcome.basis["runs"] == 3, (
            f"basis.runs must be the number of runs in the window, got {read.outcome.basis['runs']!r}"
        )

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_the_published_basis_describes_the_current_window(self, length, cw_period):
        """In the payload, ``monthly_cost_basis`` is the *current* window's basis.

        Both windows produce a basis; only one is published. Naming the previous
        window's bounds beside the current window's figure would be R2.6
        satisfied in form and broken in fact.
        """
        start = END - length
        run = run_payload(start, END, cw_period, current=([5.0], [2.0]), previous=([4.0], [2.0]))
        basis = run.performance["monthly_cost_basis"]
        assert basis is not None, "a measured figure must publish its basis (R2.6)"
        assert metrics_mod._parse_iso(basis["window_start"]) == start, (
            f"R2.6: the published basis must describe the CURRENT window starting {start}, got "
            f"{basis['window_start']!r}"
        )
        assert metrics_mod._parse_iso(basis["window_end"]) == END, (
            f"R2.6: the published basis must describe the CURRENT window ending {END}, got {basis['window_end']!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. A window with no datapoint is Not_Established (R2.4)
# ═══════════════════════════════════════════════════════════════════════════


class TestNoDatapointIsNotEstablished:
    """R2.4: no datapoint for a window ⇒ Not_Established for the window **and**
    Not_Established for the cost trend.

    Both halves are asserted, and so is their boundary: a *measured* ``0.0`` over
    datapoints that exist is a measurement. Collapsing the two is what
    `LGR-4854b3d-1` (**template / ratified**) forbids — a three-state value is
    never reduced to its zero — and it is the reason the read parses the raw
    ``MetricDataResults`` instead of the helper that returns 0 for an empty
    series.
    """

    def test_an_empty_series_reports_absent_with_no_value_and_no_basis(self):
        read = run_cost(END - timedelta(days=1), END, cost_values=[], run_values=[])
        assert read.outcome.value is None, (
            f"R2.4: a window with no datapoint must report Not_Established, got {read.outcome.value!r}"
        )
        assert read.outcome.basis is None, (
            "there is no basis for a figure that was not established — a basis beside a null figure "
            "annotates a measurement that does not exist"
        )
        assert read.outcome.determined_from == absent_token(), (
            f"an established nothing must carry {absent_token()!r} (R4.2), got {read.outcome.determined_from!r}"
        )

    def test_a_response_with_no_series_at_all_reports_absent(self):
        """A successful call that returned nothing is still "we looked"."""
        read = run_cost(END - timedelta(days=1), END, response=EMPTY_REPLY)
        assert read.outcome.value is None
        assert read.outcome.determined_from == absent_token()

    def test_a_raising_client_reports_incomplete_and_not_absent(self):
        """ "We could not look" must not share a token with "there is nothing there".

        An operator acts differently on the two, which is the whole point of
        R4.2's three-valued vocabulary.
        """
        read = run_cost(END - timedelta(days=1), END, side_effect=RuntimeError("CloudWatch GetMetricData throttled"))
        assert read.outcome.value is None
        assert read.outcome.basis is None
        assert read.outcome.determined_from == incomplete_token(), (
            f"an unreadable cost source must carry {incomplete_token()!r} (R4.2), got {read.outcome.determined_from!r}"
        )
        assert read.logger.warning.called or read.logger.error.called or read.logger.exception.called, (
            "a failed cost read must be recorded at WARNING level or above"
        )

    @pytest.mark.parametrize(("length", "cw_period"), WINDOW_LENGTHS)
    def test_both_windows_absent_gives_a_null_figure_and_a_not_established_trend(self, length, cw_period):
        run = run_payload(END - length, END, cw_period)
        assert run.performance["monthly_cost"] is None, (
            f"R2.4: no terminal Glue run in the window means no figure, got {run.performance['monthly_cost']!r}"
        )
        assert run.performance["monthly_cost_basis"] is None
        assert run.cost_trend == NOT_ESTABLISHED, (
            f"R2.4: an absent cost window must report a {NOT_ESTABLISHED!r} trend, got {run.cost_trend!r}"
        )

    def test_an_absent_cost_names_itself_in_missing(self):
        """R4.3: every unavailable fact is named. The figure and its basis together."""
        run = run_payload(END - timedelta(days=1), END, 300)
        missing = set(run.performance["missing"])
        assert {"monthly_cost", "monthly_cost_basis"} <= missing, (
            f"an absent cost figure and its absent basis must both be named in `missing`, got {sorted(missing)!r}"
        )

    def test_an_absent_previous_window_alone_makes_the_trend_not_established(self):
        """The asymmetric arm: a measured current window with no baseline.

        This is R2.4's clause that a payload-wide absence cannot demonstrate —
        the figure is a number and the trend is still Not_Established, because
        there is nothing to compare it against. Reporting ``stable`` here would
        claim the cost held level.
        """
        run = run_payload(END - timedelta(days=1), END, 300, current=([0.441], [3.0]), previous=([], []))
        assert run.performance["monthly_cost"] == pytest.approx(0.44, abs=0.01)
        assert run.cost_trend == NOT_ESTABLISHED, f"R2.4: no baseline means no trend, got {run.cost_trend!r}"
        assert "monthly_cost" not in run.performance["missing"]

    def test_a_measured_zero_is_a_measurement_and_not_an_absence(self):
        """R2.4's boundary, and the collapse `LGR-4854b3d-1` forbids.

        Datapoints exist and sum to ``0.0``. That is a window in which runs were
        measured and cost nothing — a different fact from a window in which no
        run reached a terminal state — so it publishes a figure, carries a
        basis, is absent from ``missing``, and its trend is computed rather than
        Not_Established.
        """
        run = run_payload(END - timedelta(days=1), END, 300, current=([0.0], [1.0]), previous=([0.0], [1.0]))
        assert run.performance["monthly_cost"] == 0.0, (
            f"a sum of 0.0 over datapoints that exist is a measurement, got {run.performance['monthly_cost']!r}"
        )
        assert run.performance["monthly_cost_basis"] is not None
        assert "monthly_cost" not in run.performance["missing"]
        assert run.cost_trend != NOT_ESTABLISHED, (
            "two measured zeroes are a baseline and a comparison, not an absence (R4.5)"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. `basis` states that the figure is not measured spend (R2.7)
# ═══════════════════════════════════════════════════════════════════════════


class TestBasisStatesTheFigureIsAnEstimate:
    """R2.7: the ``basis`` states the figure is a DPU-minutes estimate including
    the 10-minute minimum, "so that a reader does not read the figure as
    measured spend".

    This is the requirement the ``note`` key exists to satisfy. A ``basis``
    carrying only bounds, a rate and a run count is a dict of numbers with no
    warning attached — and nothing in this system measures spend (requirements
    constraint 14: the 10-minute minimum is why every observed run's
    ``actual_cost`` is a constant 0.147). The wording is not pinned; what is
    asserted is that each thing R2.7 names is present and that the note negates
    measurement rather than merely omitting the claim.
    """

    @pytest.fixture
    def basis(self) -> dict[str, Any]:
        read = run_cost(END - timedelta(days=1), END, cost_values=[0.147], run_values=[1.0])
        assert read.outcome.basis is not None
        return read.outcome.basis

    def test_the_note_says_it_is_an_estimate(self, basis):
        assert re.search(r"estimat", basis["note"], re.IGNORECASE), (
            f"R2.7: the basis must state the figure is an estimate, got {basis['note']!r}"
        )

    def test_the_note_names_dpu_minutes_as_the_derivation(self, basis):
        assert re.search(r"\bDPU\b", basis["note"], re.IGNORECASE), (
            f"R2.7: the basis must state the figure is derived from Glue DPU-minutes, got {basis['note']!r}"
        )

    def test_the_note_states_the_per_run_minimum(self, basis):
        assert re.search(rf"{DPU_MINIMUM_MINUTES}[- ]?minute", basis["note"], re.IGNORECASE), (
            f"R2.7: the basis must state the {DPU_MINIMUM_MINUTES}-minute per-run minimum, got {basis['note']!r}"
        )

    def test_the_note_denies_that_this_is_measured_spend(self, basis):
        assert re.search(r"\b(not|never|no)\b[^.]*\bmeasured\b", basis["note"], re.IGNORECASE), (
            f"R2.7: the basis must state the figure is NOT measured spend — omitting the claim is not the "
            f"same as denying it, and a reader takes a currency tile for a bill. Got {basis['note']!r}"
        )

    def test_the_basis_reports_the_two_pricing_constants(self, basis):
        """R2.7 names the DPU minimum; the rate is what makes the note checkable.

        The minimum is pinned because the requirement fixes it. The rate is not:
        it is the producer's constant, reported here and applied nowhere, and a
        change to it should show in the payload rather than break a test.
        """
        assert basis["dpu_minimum_minutes"] == DPU_MINIMUM_MINUTES
        assert isinstance(basis["rate_usd_per_dpu_hour"], int | float)
        assert basis["rate_usd_per_dpu_hour"] > 0

    def test_the_note_is_published_in_the_payload(self):
        """R2.7 is only satisfied if the statement reaches the reader."""
        run = run_payload(END - timedelta(days=1), END, 300, current=([0.147], [1.0]), previous=([0.147], [1.0]))
        note = run.performance["monthly_cost_basis"]["note"]
        assert re.search(r"\b(not|never|no)\b[^.]*\bmeasured\b", note, re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════════════════
# Structural detectors — AST, so prose is invisible by construction
# ═══════════════════════════════════════════════════════════════════════════


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """Node ids of every bare string expression — docstrings and prose blocks.

    A docstring is a string *statement*, reachable as ``Expr(Constant(str))``.
    Collecting all of them, rather than only the first statement of a
    module/def/class, also excludes a free-standing string used as a section
    heading. Comments never reach the AST at all. Same helper shape as
    ``test_metrics_polarity.py``'s, for the same reason.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }


def code_string_constants(source: str) -> list[tuple[int, str]]:
    """Every string constant in ``source`` that is **code**, as ``(line, value)``.

    Docstrings and prose blocks are excluded by node identity; comments are not
    in the AST to begin with. This is the whole reason the guards below are an
    AST walk: ``metrics.py`` names ``RUNNING``, ``StatusIndex`` and the
    executions table in a comment explaining why it reads none of them, and
    names four terminal Glue states in a docstring stating R2.8's cadence.
    """
    tree = ast.parse(source)
    docstring_ids = _docstring_constant_ids(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids
    ]


def enumerated_execution_statuses(source: str) -> list[str]:
    """Execution-status tokens appearing as code strings, as ``line: value``.

    Matched on the **whole** stripped constant rather than as a substring, so
    the CloudWatch metric names this module legitimately carries
    (``ExecutionsFailed``, ``ExecutionsSucceeded``) and its lowercase payload
    keys (``failed``, ``succeeded``) are not confused with a DynamoDB status
    value. What is forbidden is the status vocabulary itself.
    """
    forbidden = set(DELETED_EXECUTION_STATUSES)
    return [f"{line}: {value!r}" for line, value in code_string_constants(source) if value.strip() in forbidden]


def named_status_index(source: str) -> list[str]:
    """Code strings naming the executions-table StatusIndex GSI."""
    return [f"{line}: {value!r}" for line, value in code_string_constants(source) if STATUS_INDEX_NAME in value]


def table_operations(source: str) -> list[tuple[int, str, str]]:
    """Every table read/write call, as ``(line, receiver name, operation)``.

    The receiver is the bare name the call is made on, so
    ``executions_table.query(...)`` and ``state_table.scan(...)`` are both
    visible. An attribute chain (``boto3.resource(...).Table(...).scan()``) is
    reported with the receiver spelled ``<expression>``, so it cannot pass as an
    unrecognised name.
    """
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in TABLE_OPERATIONS:
            continue
        value = node.func.value
        receiver = value.id if isinstance(value, ast.Name) else "<expression>"
        found.append((node.lineno, receiver, node.func.attr))
    return found


# ═══════════════════════════════════════════════════════════════════════════
# 6. The structural guards (R8.2, requirements constraint 6)
# ═══════════════════════════════════════════════════════════════════════════


class TestStructuralGuards:
    """What task 6.2 deleted stays deleted.

    The fallback's removal is what resolves R8.2: the ``RUNNING`` enumeration
    was this handler's only contact with p38's phantom rows, so p39 removed the
    exposure instead of adding a second filter. These guards make a
    re-introduction fail a test rather than quietly reinstate the coupling.
    """

    def test_metrics_enumerates_no_execution_status(self):
        """R8.2: no ``RUNNING`` — or any other status — enumeration in code."""
        violations = enumerated_execution_statuses(METRICS_SOURCE)
        assert violations == [], f"metrics.py enumerates execution statuses at {violations!r}. {REMEDIATION_R82}"

    def test_metrics_names_no_status_index(self):
        """The GSI the enumeration read. A name in code is a read waiting to happen."""
        violations = named_status_index(METRICS_SOURCE)
        assert violations == [], f"metrics.py names {STATUS_INDEX_NAME} at {violations!r}. {REMEDIATION_R82}"

    def test_metrics_does_not_import_the_executions_table(self):
        """The import went with its only reader (task 6.2).

        Asserted on the imported module object as well as on the source, because
        the four test files that used to ``patch`` this attribute would raise
        ``AttributeError`` the moment it came back under a stale stub.
        """
        assert not hasattr(metrics_mod, "executions_table"), (
            f"handlers.metrics must not bind `executions_table` — its only reader was the deleted flat-rate "
            f"cost fallback. {REMEDIATION_R82}"
        )
        imported = {
            alias.name
            for node in ast.walk(ast.parse(METRICS_SOURCE))
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "executions_table" not in imported, f"metrics.py imports executions_table. {REMEDIATION_R82}"

    def test_metrics_reads_only_the_state_table_and_the_response_cache(self):
        """Every table call is on one of the two permitted resources.

        Stronger than checking for the executions table by name: a table
        resolved under any other binding is caught too, and the permitted set is
        small enough to read.
        """
        receivers = {receiver for _, receiver, _ in table_operations(METRICS_SOURCE)}
        assert receivers <= PERMITTED_TABLE_RECEIVERS, (
            f"metrics.py calls table operations on {sorted(receivers - PERMITTED_TABLE_RECEIVERS)!r}; only "
            f"{sorted(PERMITTED_TABLE_RECEIVERS)!r} are permitted. {REMEDIATION_R82}"
        )

    def test_metrics_binds_the_cache_table_local_from_nothing_else(self):
        """The `cache_table` local resolves to `_cache_table` or the `table` parameter.

        This is the precision the p47 D3 allowlist entry would otherwise give up.
        The set above can no longer say *which* resource `cache_table` is, so this
        says it instead: every assignment to that name mentions nothing but the two
        permitted sources, so an `executions_table` (or anything else) cannot reach
        a table operation by way of the alias.

        Asserted on assignments rather than on behaviour because the whole point of
        this class is to catch a re-introduction in source, before it has a caller.
        """
        offenders: list[str] = []
        for node in ast.walk(ast.parse(METRICS_SOURCE)):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "cache_table" for t in node.targets):
                continue
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            if not names <= PERMITTED_CACHE_TABLE_SOURCES:
                offenders.append(f"{node.lineno}: {sorted(names - PERMITTED_CACHE_TABLE_SOURCES)}")

        assert offenders == [], (
            f"metrics.py binds `cache_table` from something other than "
            f"{sorted(PERMITTED_CACHE_TABLE_SOURCES)!r} at {offenders!r}. {REMEDIATION_R82}"
        )

    def test_metrics_scans_no_table(self):
        """P28 R17.1 / requirements constraint 6: bounded Queries, never a Scan."""
        scans = [
            f"{line}: {receiver}.{operation}"
            for line, receiver, operation in table_operations(METRICS_SOURCE)
            if operation == "scan"
        ]
        assert scans == [], (
            f"metrics.py Scans at {scans!r}. Metrics read paths use bounded Queries on the step_name GSI "
            f"(P28 R17.1, asserted behaviourally by test_query_not_scan.py). {REMEDIATION_R82}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Non-vacuity — the detectors flag what they claim to, and ignore prose
# ═══════════════════════════════════════════════════════════════════════════
# Without these, every guard above passes for free the moment a detector stops
# seeing anything. Each control is a synthetic source, so none of them can be
# satisfied by the module happening to be clean.


_REINTRODUCED_FALLBACK = '''
"""A docstring naming RUNNING and StatusIndex, which must stay invisible."""
# A comment naming RUNNING, StatusIndex and executions_table too.
_EXECUTION_STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED")
_EXEC_STATUS_INDEX = "StatusIndex"


def _legacy_cost_fallback(start, end):
    for exec_status in _EXECUTION_STATUSES:
        resp = executions_table.query(
            IndexName=_EXEC_STATUS_INDEX,
            KeyConditionExpression=Key("status").eq(exec_status),
        )
    return len(resp["Items"]) * 0.50
'''

_PROSE_ONLY = '''
"""RUNNING, StatusIndex and executions_table, all named in a docstring.

`SUCCEEDED`, `FAILED`, `STOPPED` and `TIMEOUT` too, as a cadence statement.
"""
# The StatusIndex enumeration over RUNNING was deleted; executions_table with it.
_COST_METRIC_NAME = "GlueJob.RunCostEstimate"
'''

_SCANNING_MODULE = """
def read_everything():
    return state_table.scan(FilterExpression="whatever")
"""


def test_the_status_enumeration_detector_flags_a_reintroduced_fallback():
    violations = enumerated_execution_statuses(_REINTRODUCED_FALLBACK)
    assert violations, "the status-enumeration detector must flag a re-introduced closed status set"
    assert any("RUNNING" in violation for violation in violations)


def test_the_status_index_detector_flags_a_reintroduced_gsi_constant():
    assert named_status_index(_REINTRODUCED_FALLBACK), "the StatusIndex detector must flag the GSI constant"


def test_the_table_receiver_detector_flags_an_executions_table_read():
    receivers = {receiver for _, receiver, _ in table_operations(_REINTRODUCED_FALLBACK)}
    assert "executions_table" in receivers
    assert not receivers <= PERMITTED_TABLE_RECEIVERS


def test_the_scan_detector_flags_a_scan():
    operations = {operation for _, _, operation in table_operations(_SCANNING_MODULE)}
    assert "scan" in operations, "the Scan detector must flag state_table.scan(...)"


def test_the_detectors_ignore_prose_naming_everything_they_forbid():
    """A module whose only mention of the forbidden shapes is prose is clean.

    This is the property that makes the guards compatible with documenting the
    fence — the failure mode `LGR-b46a5bb-4` records verbatim.
    """
    assert enumerated_execution_statuses(_PROSE_ONLY) == []
    assert named_status_index(_PROSE_ONLY) == []
    assert table_operations(_PROSE_ONLY) == []


def test_a_substring_scan_would_fail_on_the_modules_own_prose():
    """Demonstrated against the real file, not asserted about it.

    ``metrics.py`` contains the literal text ``RUNNING``, ``StatusIndex`` and
    ``executions table`` — all of it in the comment block at ``:290-298``
    explaining why none of them is read — and the four terminal Glue states in
    ``_get_estimated_monthly_cost``'s docstring. A substring guard goes red on
    every one of those. The AST detectors above see none of them, which is why
    they are AST detectors.
    """
    substring_hits = [token for token in ("RUNNING", STATUS_INDEX_NAME, "executions table") if token in METRICS_SOURCE]
    assert substring_hits, (
        "this control assumes metrics.py still documents what task 6.2 removed; if the prose is gone the "
        "control is meaningless, but the AST guards remain correct"
    )
    assert enumerated_execution_statuses(METRICS_SOURCE) == []
    assert named_status_index(METRICS_SOURCE) == []
