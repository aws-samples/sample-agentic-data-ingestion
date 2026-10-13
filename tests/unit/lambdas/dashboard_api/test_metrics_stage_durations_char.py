# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization test — ``_stage_durations_cw`` (``handlers/metrics.py:571``).

Feature: p39-metrics-truth, task 1.3.

**This file pins today's behaviour, and only the part of it that must
survive the change set.** Two things:

1. **The SEARCH-across-``PipelineId`` shape.** ``Pipeline.StageDuration`` is
   published with a per-``PipelineId`` dimension set, so it cannot be
   aggregated by ``{StageName, Status}`` alone (see the module docstring at
   ``metrics.py:11-16``). The read therefore issues a ``SEARCH`` over the
   ``{AgenticDataIngestion/Pipeline,PipelineId,StageName,Status,service}``
   name-space filtered on ``StageName`` only — it never pins a ``PipelineId``
   value. A future change that narrows the expression to one pipeline would
   silently report one pipeline's durations as the fleet's.
2. **The invocation-weighted reduction.** Each stage issues two raw SEARCHes,
   one on the ``Sum`` statistic and one on ``SampleCount``, each reduced by
   ``SUM(...)``; the reported figure is the window's summed duration divided by
   its summed sample count (rounded to 1dp). The raw series are fetched with
   ``ReturnData: False`` and never read.

   **AMENDED 2026-10-10** from ``AVG(<stage>_dur_raw)`` over the ``Average``
   statistic. A SEARCH returns every series CloudWatch indexes for the brace,
   including series with no datapoint in the window, and ``AVG`` counts those
   as zeros: measured on a development deployment, Code Generation read 502 ms for two
   invocations of ~3,010 ms, because six pipelines had a series and one ran.
   ``TestAnEmptySeriesDoesNotDilute`` encodes that measurement.

**Written to survive p39 task 11.1**, which widens ``_CW_STAGE_NAMES``
(``metrics.py:96-99``) from two keys to six. Every assertion below is derived
from ``_CW_STAGE_NAMES`` at call time; nothing asserts the current two-key map
as a fixed set, and nothing names ``schema_extract`` / ``schema_mapping`` as
the only mapped stages.

Deliberately **not** pinned here:

* the contents of ``_CW_STAGE_NAMES`` (task 11.1 changes it — ``R6.2``);
* the precedence between the CloudWatch value and the state-row derivation,
  which lives in ``_get_stage_metrics`` and belongs to task 1.1's file;
* the ``None``-vs-Not_Established question. ``R6.4`` keeps ``avg_time_ms``
  absent-as-``None`` for a stage with no datapoint, and
  ``STAGE_METRICS_SCHEMA``'s ``avg_time_ms`` is already ``["number", "null"]``
  (``schemas.py:308``), so the ``None`` returns asserted below are the shape
  the stage payload keeps.

Mocking: CloudWatch is not one of the services the shared fixtures cover
(DynamoDB / S3 / SQS), so — as in the sibling ``test_query_not_scan.py`` — the
module-level ``cloudwatch_client`` is patched with a ``MagicMock``. No
``mock_aws()``, no boto3 client mock for a fixture-covered service.

_Requirements: R6.2, R6.3_
"""

import os
import re
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason test_query_not_scan.py
# repeats it — under xdist --dist=loadfile any file may be imported first.
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
# bootstrap as test_query_not_scan.py). AWS_REGION is deliberately NOT set here:
# the unit layer pins it by assignment in tests/unit/conftest.py
# (LGR-3e45e65-1, template / ratified).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

START = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2025, 1, 2, tzinfo=UTC)
PERIOD = 300


# ---------------------------------------------------------------------------
# Expression introspection
# ---------------------------------------------------------------------------

# SEARCH('{<namespace>,<dim>,...} <filters>', '<stat>', <period>)
_SEARCH_RE = re.compile(
    r"^\s*SEARCH\(\s*'\{(?P<schema>[^}]*)\}(?P<filters>[^']*)'\s*,\s*'(?P<stat>[^']*)'\s*,\s*(?P<period>\d+)\s*\)\s*$"
)


class _Search:
    """A parsed CloudWatch SEARCH expression."""

    def __init__(self, expression: str) -> None:
        match = _SEARCH_RE.match(expression)
        assert match, f"not a SEARCH expression: {expression!r}"
        parts = [p.strip() for p in match.group("schema").split(",")]
        self.namespace = parts[0]
        self.dimensions = parts[1:]
        self.filters = match.group("filters").strip()
        self.stat = match.group("stat")
        self.period = int(match.group("period"))


def _queries_by_id(mock_cw: MagicMock) -> dict[str, dict]:
    """Every MetricDataQuery from the single get_metric_data call, keyed by Id."""
    assert mock_cw.get_metric_data.call_count == 1, (
        f"expected one get_metric_data call, got {mock_cw.get_metric_data.call_count}"
    )
    queries = mock_cw.get_metric_data.call_args.kwargs["MetricDataQueries"]
    by_id = {q["Id"]: q for q in queries}
    assert len(by_id) == len(queries), f"duplicate query Ids: {[q['Id'] for q in queries]}"
    return by_id


def _run(response: dict | None = None, *, side_effect: Exception | None = None) -> tuple[dict, MagicMock]:
    """Call _stage_durations_cw against a stubbed CloudWatch client."""
    mock_cw = MagicMock()
    if side_effect is not None:
        mock_cw.get_metric_data.side_effect = side_effect
    else:
        mock_cw.get_metric_data.return_value = response if response is not None else {"MetricDataResults": []}

    with patch.object(metrics_mod, "cloudwatch_client", mock_cw):
        out = metrics_mod._stage_durations_cw(START, END, PERIOD)
    return out, mock_cw


def _results(*pairs: tuple[str, list[float]]) -> dict:
    """A minimal GetMetricData response for the given (Id, Values) pairs."""
    return {"MetricDataResults": [{"Id": mid, "Values": vals, "Timestamps": []} for mid, vals in pairs]}


# ═══════════════════════════════════════════════════════════════════════════
# The query shape: SEARCH across PipelineId, Sum and SampleCount, each SUMmed
# ═══════════════════════════════════════════════════════════════════════════

#: The four query ids per stage, in the order the reader builds them.
_ID_SUFFIXES = ("_dur_sum_raw", "_dur_sum", "_dur_n_raw", "_dur_n")


def _one(values: list[float]) -> list[float]:
    """A sample count of one invocation per value."""
    return [1.0] * len(values)


class TestStageDurationsQueryShape:
    """Four queries per mapped stage: two non-returning SEARCHes and their SUM reductions."""

    def test_four_queries_per_mapped_stage_derived_from_the_map(self):
        """Derived from the map, so widening it keeps this assertion true."""
        _, mock_cw = _run()
        by_id = _queries_by_id(mock_cw)

        expected_ids = {f"{fe}{suffix}" for fe in metrics_mod._CW_STAGE_NAMES for suffix in _ID_SUFFIXES}
        assert set(by_id) == expected_ids

    def test_raw_queries_search_across_pipelineid_for_the_stage(self):
        """Both raw queries SEARCH the AgenticDataIngestion/Pipeline name-space
        including the PipelineId dimension, filtered on the stage's StageName,
        one on Sum and one on SampleCount."""
        _, mock_cw = _run()
        by_id = _queries_by_id(mock_cw)
        for fe, cw_name in metrics_mod._CW_STAGE_NAMES.items():
            for suffix, statistic in (("_dur_sum_raw", "Sum"), ("_dur_n_raw", "SampleCount")):
                raw = by_id[f"{fe}{suffix}"]
                search = _Search(raw["Expression"])

                assert search.namespace == "AgenticDataIngestion/Pipeline"
                assert "PipelineId" in search.dimensions
                assert 'MetricName="Pipeline.StageDuration"' in search.filters
                assert f'StageName="{cw_name}"' in search.filters
                assert search.stat == statistic
                assert search.period == PERIOD
                # Fetched only to be reduced — its own values are never returned.
                assert raw["ReturnData"] is False

    def test_raw_queries_do_not_pin_a_pipelineid_value(self):
        """PipelineId appears as a searched dimension, never as an equality
        predicate. Narrowing to one pipeline would report one pipeline's
        durations as the fleet's."""
        _, mock_cw = _run()
        by_id = _queries_by_id(mock_cw)
        for fe in metrics_mod._CW_STAGE_NAMES:
            for suffix in ("_dur_sum_raw", "_dur_n_raw"):
                search = _Search(by_id[f"{fe}{suffix}"]["Expression"])
                assert "PipelineId=" not in search.filters.replace(" ", ""), (
                    f"{fe}: search must not pin a PipelineId: {search.filters!r}"
                )

    def test_reductions_are_sums_never_avg(self):
        """Each raw array is reduced by SUM at the caller's period. AVG is the
        defect: it divides by every series the SEARCH returns, empty ones too."""
        _, mock_cw = _run()
        by_id = _queries_by_id(mock_cw)

        for fe in metrics_mod._CW_STAGE_NAMES:
            for reduced, raw in ((f"{fe}_dur_sum", f"{fe}_dur_sum_raw"), (f"{fe}_dur_n", f"{fe}_dur_n_raw")):
                reduction = by_id[reduced]
                assert reduction["Expression"] == f"SUM({raw})"
                assert reduction["Period"] == PERIOD
                # No ReturnData=False: these are the series the reader consumes.
                assert reduction.get("ReturnData", True) is True
        assert not any("AVG(" in q["Expression"] for q in by_id.values())

    def test_window_bounds_pass_through_unchanged(self):
        """The caller's [start, end] are the CloudWatch window; the function
        anchors no window of its own."""
        _, mock_cw = _run()

        kwargs = mock_cw.get_metric_data.call_args.kwargs
        assert kwargs["StartTime"] == START
        assert kwargs["EndTime"] == END


# ═══════════════════════════════════════════════════════════════════════════
# The reduction is what gets read back
# ═══════════════════════════════════════════════════════════════════════════


class TestStageDurationsReduction:
    """The reported figure is summed duration over summed sample count, rounded to 1dp."""

    def _first_stage(self) -> str:
        assert metrics_mod._CW_STAGE_NAMES, "_CW_STAGE_NAMES must map at least one stage"
        return next(iter(metrics_mod._CW_STAGE_NAMES))

    def test_reported_value_is_total_duration_over_total_invocations(self):
        fe = self._first_stage()
        durations = [100.0, 200.0, 300.5]
        out, _ = _run(_results((f"{fe}_dur_sum", durations), (f"{fe}_dur_n", _one(durations))))

        assert out[fe] == pytest.approx(200.2)  # round(600.5 / 3, 1)

    def test_a_busy_bucket_weighs_by_its_invocations(self):
        """Two invocations in one bucket and one in another weigh 2:1, not 1:1.

        A per-bucket mean would give (150 + 600) / 2 = 375.
        """
        fe = self._first_stage()
        out, _ = _run(_results((f"{fe}_dur_sum", [300.0, 600.0]), (f"{fe}_dur_n", [2.0, 1.0])))

        assert out[fe] == pytest.approx(300.0)  # 900 / 3

    def test_single_datapoint_is_reported_as_itself(self):
        fe = self._first_stage()
        out, _ = _run(_results((f"{fe}_dur_sum", [1234.56]), (f"{fe}_dur_n", [1.0])))

        assert out[fe] == pytest.approx(1234.6)

    def test_raw_search_series_values_are_not_read(self):
        """Raw ids in the response contribute nothing: only the two SUM
        reductions are consumed."""
        fe = self._first_stage()
        out, _ = _run(
            _results(
                (f"{fe}_dur_sum_raw", [9_999.0, 9_999.0]),
                (f"{fe}_dur_n_raw", [9_999.0]),
                (f"{fe}_dur_sum", [10.0, 20.0]),
                (f"{fe}_dur_n", [1.0, 1.0]),
            )
        )

        assert out[fe] == pytest.approx(15.0)

    def test_every_mapped_stage_is_a_key_in_the_result(self):
        """The result's key set is exactly _CW_STAGE_NAMES' key set, whatever
        CloudWatch returned — so a stage with no series still reports, as None."""
        out, _ = _run()

        assert set(out) == set(metrics_mod._CW_STAGE_NAMES)

    def test_absent_series_reports_none(self):
        """No series for a mapped stage → None, not 0.0."""
        out, _ = _run()

        for fe in metrics_mod._CW_STAGE_NAMES:
            assert out[fe] is None

    def test_zero_invocations_report_none(self):
        """Returned series with no datapoints, or a zero count → None, not 0.0."""
        fe = self._first_stage()
        out, _ = _run(_results((f"{fe}_dur_sum", []), (f"{fe}_dur_n", [])))
        assert out[fe] is None

        out, _ = _run(_results((f"{fe}_dur_sum", [0.0]), (f"{fe}_dur_n", [0.0])))
        assert out[fe] is None

    def test_one_stage_reporting_leaves_the_others_none(self):
        """Stages are independent: a datapoint for one never fills in another."""
        if len(metrics_mod._CW_STAGE_NAMES) < 2:
            pytest.skip("needs at least two mapped stages")
        fe, *others = list(metrics_mod._CW_STAGE_NAMES)
        out, _ = _run(_results((f"{fe}_dur_sum", [42.0]), (f"{fe}_dur_n", [1.0])))

        assert out[fe] == pytest.approx(42.0)
        for other in others:
            assert out[other] is None

    def test_cloudwatch_failure_reports_none_for_every_stage_and_warns(self):
        """A raising client is absorbed: every mapped stage reports None, the
        failure is logged at WARNING, and nothing propagates to the caller."""
        with patch.object(metrics_mod.logger, "warning") as mock_warn:
            out, _ = _run(side_effect=RuntimeError("cloudwatch is down"))

        assert set(out) == set(metrics_mod._CW_STAGE_NAMES)
        for fe in metrics_mod._CW_STAGE_NAMES:
            assert out[fe] is None
        assert mock_warn.call_count == 1
        assert "cloudwatch is down" in str(mock_warn.call_args)


class TestAnEmptySeriesDoesNotDilute:
    """The measured 2026-10-10 defect, reproduced through CloudWatch's semantics.

    A SEARCH returns every indexed series for the brace. For Code Generation on
    a development deployment that was six, one per pipeline that ever ran the stage, and one
    had values (3024.7, 3001.8) in the 24h window. ``AVG`` over that array
    returned 504.1 and 500.3 (each value over six). The stub below answers the
    reader's queries the way CloudWatch does: SUM over an array is the sum of
    the present values, so empty series contribute nothing.
    """

    @staticmethod
    def _cloudwatch(series_values: list[list[float]]) -> MagicMock:
        def get_metric_data(**kwargs):
            results = []
            for query in kwargs["MetricDataQueries"]:
                if query.get("ReturnData", True) is False:
                    continue
                count = query["Id"].endswith("_dur_n")
                per_bucket = [
                    sum((1.0 if count else values[i]) for values in series_values if i < len(values))
                    for i in range(max((len(v) for v in series_values), default=0))
                ]
                results.append({"Id": query["Id"], "Values": per_bucket, "Timestamps": []})
            return {"MetricDataResults": results}

        client = MagicMock()
        client.get_metric_data.side_effect = get_metric_data
        return client

    def test_five_idle_pipelines_do_not_shrink_the_one_that_ran(self):
        series = [[3024.7, 3001.8], [], [], [], [], []]
        with patch.object(metrics_mod, "cloudwatch_client", self._cloudwatch(series)):
            out = metrics_mod._stage_durations_cw(START, END, PERIOD)

        for fe in metrics_mod._CW_STAGE_NAMES:
            assert out[fe] == pytest.approx(3013.2, abs=0.1), (
                f"{fe}: {out[fe]} — the six-series AVG gave ~502; the invocation mean is ~3013"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
