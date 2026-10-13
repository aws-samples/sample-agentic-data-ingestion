# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test: metrics handler queries CloudWatch with correct namespaces,
metric names, and time periods.

Validates: AC-9.1, AC-9.2
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name


@pytest.fixture(autouse=True)
def _env_utils_stub(monkeypatch):
    """Install the env_utils stub for each test, and only for the test.

    The handler modules are imported inside the tests, so the stub must be in
    sys.modules then. Installed at module scope it leaked into every module
    collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    """
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)


os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Add handler package to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)


@pytest.fixture(autouse=True)
def _mock_state_tables():
    """Keep these unit tests hermetic — never touch real DynamoDB.

    p39 task 6.2: the executions-table stub is gone with the subject it stubbed.
    `handlers.metrics` no longer imports `executions_table` — the flat-rate cost
    fallback was its only reader — so patching that attribute would now raise
    `AttributeError`. Only the state table is left, and it is still needed:
    P28 R17.1's read paths (`_get_stage_metrics`, `_get_rows_loaded`) Query
    its GSI on every `get_comprehensive_metrics` call.
    """
    with patch("handlers.metrics.state_table") as ms:
        # P28 R17.1: metrics read paths now Query GSIs instead of scanning
        ms.query.return_value = {"Items": []}
        yield


def _make_cw_response(ids_and_values: dict) -> dict:
    """Build a minimal CloudWatch GetMetricData response."""
    return {
        "MetricDataResults": [{"Id": mid, "Values": vals, "Timestamps": []} for mid, vals in ids_and_values.items()]
    }


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_get_comprehensive_metrics_queries_all_namespaces(mock_cw, mock_cache):
    """get_comprehensive_metrics queries AWS/Lambda, AWS/States, AWS/DynamoDB,
    and AgenticDataIngestion/Pipeline namespaces."""
    # Cache miss
    mock_cache.get_item.return_value = {}

    # Return empty but valid responses for all get_metric_data calls
    mock_cw.get_metric_data.return_value = _make_cw_response({})

    from handlers.metrics import get_comprehensive_metrics

    event = {"queryStringParameters": {"period": "24h"}}
    result = get_comprehensive_metrics(event)
    body = json.loads(result["body"])

    assert result["statusCode"] == 200
    assert "performance" in body
    assert "stages" in body
    assert "infrastructure" in body
    assert body["period"] == "24h"

    # Verify CloudWatch was called (Lambda, SFN, DynamoDB, custom pipeline)
    assert mock_cw.get_metric_data.call_count >= 3

    # Check that the calls include the expected namespaces via SEARCH expressions
    # or MetricStat.Metric.Namespace
    all_calls = mock_cw.get_metric_data.call_args_list
    all_expressions = []
    all_namespaces = set()
    for c in all_calls:
        queries = c.kwargs.get("MetricDataQueries", []) or c[1].get("MetricDataQueries", [])
        for q in queries:
            expr = q.get("Expression", "")
            if expr:
                all_expressions.append(expr)
            stat = q.get("MetricStat", {})
            ns = stat.get("Metric", {}).get("Namespace", "")
            if ns:
                all_namespaces.add(ns)

    all_expr_text = " ".join(all_expressions)
    assert "AWS/Lambda" in all_expr_text
    assert "AWS/States" in all_expr_text
    assert "AWS/DynamoDB" in all_expr_text
    # Custom AgenticDataIngestion metrics are now queried via SEARCH expressions (consistent
    # with the other namespaces), not MetricStat, so check the expression text.
    assert "AgenticDataIngestion/Pipeline" in all_expr_text or "AgenticDataIngestion/Pipeline" in all_namespaces


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_lambda_metrics_returns_invocations_and_errors(mock_cw, mock_cache):
    """_get_lambda_metrics returns invocation counts, error rates, and duration."""
    mock_cw.get_metric_data.return_value = _make_cw_response(
        {
            "invocations_sum": [100, 200, 150],
            "errors_sum": [2, 3, 1],
            "duration_avg": [120.5, 130.2, 110.8],
        }
    )

    from handlers.metrics import _get_lambda_metrics

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)
    result = _get_lambda_metrics(start, end, 300)

    assert result["invocations"] == 450  # sum of [100, 200, 150]
    assert result["errors"] == 6  # sum of [2, 3, 1]
    assert result["avg_duration_ms"] > 0


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_sfn_metrics_returns_execution_counts(mock_cw, mock_cache):
    """_get_sfn_metrics returns started plus a count per terminal outcome.

    p39 R5.1 added ``aborted`` and ``timed_out`` alongside ``succeeded`` and
    ``failed``, because all four are terminal and the success-rate denominator
    covers every one of them.
    """
    mock_cw.get_metric_data.return_value = _make_cw_response(
        {
            "started_sum": [10, 15],
            "succeeded_sum": [9, 14],
            "failed_sum": [1, 1],
            "aborted_sum": [2, 3],
            "timed_out_sum": [0, 1],
            "duration_avg": [5000.0, 6000.0],
        }
    )

    from handlers.metrics import _get_sfn_metrics

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)
    result = _get_sfn_metrics(start, end, 300)

    assert result["started"] == 25
    assert result["succeeded"] == 23
    assert result["failed"] == 2
    assert result["aborted"] == 5
    assert result["timed_out"] == 1
    assert result["avg_duration_ms"] > 0
    # A successful read is not a failed one — `_success_rate` keys the
    # difference between "we looked and there is nothing" and "we could not
    # look" off this flag (R4.2).
    assert result["read_failed"] is False


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_sfn_metrics_queries_every_terminal_outcome(mock_cw, mock_cache):
    """Every metric in _TERMINAL_EXECUTION_METRICS gets a SEARCH query (R5.1, R5.5).

    Asserted against the mapping rather than against a hard-coded list of four,
    so adding a terminal outcome to the one named set is genuinely a one-line
    change and cannot leave the query set behind.
    """
    mock_cw.get_metric_data.return_value = _make_cw_response({})

    from handlers.metrics import _TERMINAL_EXECUTION_METRICS, _get_sfn_metrics

    _get_sfn_metrics(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC), 300)

    queries = mock_cw.get_metric_data.call_args.kwargs["MetricDataQueries"]
    expressions = " ".join(q.get("Expression", "") for q in queries)
    for metric_name in _TERMINAL_EXECUTION_METRICS.values():
        assert f'MetricName="{metric_name}"' in expressions, f"No SEARCH query for {metric_name}"
    # R5.2: Started is queried and reported, but it is not a terminal outcome.
    assert "ExecutionsStarted" not in _TERMINAL_EXECUTION_METRICS.values()


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_dynamodb_metrics_returns_capacity(mock_cw, mock_cache):
    """_get_dynamodb_metrics returns consumed read/write capacity."""
    mock_cw.get_metric_data.return_value = _make_cw_response(
        {
            "read_capacity": [45.5, 50.2],
            "write_capacity": [30.1, 35.8],
        }
    )

    from handlers.metrics import _get_dynamodb_metrics

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)
    result = _get_dynamodb_metrics(start, end, 300)

    assert result["read_capacity"] == pytest.approx(95.7, abs=0.1)
    assert result["write_capacity"] == pytest.approx(65.9, abs=0.1)


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_period_parameter_defaults_to_24h(mock_cw, mock_cache):
    """When no period is specified, defaults to 24h."""
    mock_cache.get_item.return_value = {}
    mock_cw.get_metric_data.return_value = _make_cw_response({})

    from handlers.metrics import get_comprehensive_metrics

    result = get_comprehensive_metrics({"queryStringParameters": None})
    body = json.loads(result["body"])

    assert body["period"] == "24h"


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_invalid_period_falls_back_to_24h(mock_cw, mock_cache):
    """Invalid period values fall back to 24h."""
    mock_cache.get_item.return_value = {}
    mock_cw.get_metric_data.return_value = _make_cw_response({})

    from handlers.metrics import get_comprehensive_metrics

    result = get_comprehensive_metrics({"queryStringParameters": {"period": "invalid"}})
    body = json.loads(result["body"])

    assert body["period"] == "24h"


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_period_7d_uses_3600_second_cw_period(mock_cw, mock_cache):
    """7d period uses 3600s CloudWatch data-point period."""
    mock_cache.get_item.return_value = {}
    mock_cw.get_metric_data.return_value = _make_cw_response({})

    from handlers.metrics import get_comprehensive_metrics

    result = get_comprehensive_metrics({"queryStringParameters": {"period": "7d"}})
    body = json.loads(result["body"])

    assert body["period"] == "7d"

    # Verify at least one call used Period=3600
    for c in mock_cw.get_metric_data.call_args_list:
        queries = c.kwargs.get("MetricDataQueries", []) or c[1].get("MetricDataQueries", [])
        for q in queries:
            stat = q.get("MetricStat", {})
            if stat.get("Period"):
                assert stat["Period"] == 3600


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_stages_section_has_expected_keys(mock_cw, mock_cache):
    """Response stages section contains all expected pipeline stage keys."""
    mock_cache.get_item.return_value = {}
    mock_cw.get_metric_data.return_value = _make_cw_response({})

    from handlers.metrics import get_comprehensive_metrics

    result = get_comprehensive_metrics({"queryStringParameters": {"period": "1h"}})
    body = json.loads(result["body"])

    expected_stages = [
        "schema_extract",
        "schema_mapping",
        "code_generation",
        "infrastructure",
        "quality_checks",
        "pipeline_run",
    ]
    for stage in expected_stages:
        assert stage in body["stages"], f"Missing stage: {stage}"
        assert "success_count" in body["stages"][stage]
        assert "error_count" in body["stages"][stage]
        assert "avg_time_ms" in body["stages"][stage]


# ═══════════════════════════════════════════════════════════════════════════
# Not_Established at the payload — p39-metrics-truth task 3.6
# ═══════════════════════════════════════════════════════════════════════════
#
# WHY THESE WERE ADDED, AND WHAT WAS VACUOUS WITHOUT THEM
# -------------------------------------------------------
# Every test above drives `get_comprehensive_metrics` with `_make_cw_response({})`
# — a CloudWatch response carrying no series at all — against a state table whose
# `query` returns no items. **That is the fully-absent window**, the exact input
# p39 R4 is about. And every assertion above is a key-presence or a period check:
# `"performance" in body`, `body["period"] == "24h"`, `"avg_time_ms" in
# body["stages"][stage]`.
#
# Key presence is true of `{"overall_success_rate": 100.0}` and of
# `{"overall_success_rate": None}` alike. So before p39 this file passed while the
# handler reported a 100% success rate, a records figure equal to the count of
# files, an AgentCore availability of 100% and a health of `healthy` — all from a
# window in which nothing ran and one CloudWatch response that contained nothing.
# It would keep passing, unchanged, if any of those collapses were reintroduced
# tomorrow. That is what "passing vacuously" means here: the assertions cannot
# distinguish the requirement being met from the defect it replaced.
#
# The assertions below close that. Each one fails if a source's absence were
# re-collapsed into a value.
#
# WHERE THE EXPECTATIONS COME FROM
# --------------------------------
# From R4.2-R4.9 and R5.4, not from the current payload (R9.4, requirements
# constraint 12; `LGR-28b4d75-5`, template / ratified — "a test written from the
# producer's output cannot detect the producer drifting, because the output is its
# own oracle"). The pre-p39 output for this very input was `100.0`, a file count,
# and `healthy`; none of it appears below as a baseline.
#
# The R4.2 provenance suffixes (`_absent`, `_incomplete`) are written out
# literally, because the requirement fixes those words. Source *names* are not
# asserted: no requirement fixes them, so the assertions are on the suffix and on
# the token's presence.
#
# DELIBERATELY NOT ASSERTED HERE
# ------------------------------
# * **S3 storage bytes.** `_get_s3_storage_bytes` is task 4.1's, mid-change, and
#   its provenance token is 4.1's to establish. The `missing` assertions below are
#   scoped so they do not incidentally observe it — see
#   `test_missing_names_every_absent_performance_field`.
# * **The `agent_latency` and `cost` trends.** `_get_agent_latency` and
#   `_get_estimated_monthly_cost` still return a bare float (tasks 15.1 and 6.1),
#   so those two windows are measured zeros and their trends are honestly
#   `stable`. Asserting `not_established` there would assert a future.
# * **Per-function behaviour.** `test_metrics_absence.py` (same task) covers
#   `_compute_trend`, `_success_rate`, `_get_agent_error_rate` and
#   `_get_rows_loaded` against their own requirement clauses. What is here is
#   the *payload* half: that an absence survives assembly and is named — and,
#   for the rows-loaded pair, that a measurement survives it too
#   (`test_rows_loaded_and_rate_reach_the_payload_from_load_metrics_rows`).

#: R4.2's two suffixes for an unavailable source.
_ABSENT = "_absent"
_INCOMPLETE = "_incomplete"


@pytest.fixture
def absent_window_body():
    """The payload for a window in which nothing was measured.

    Empty CloudWatch responses plus the autouse `_mock_state_tables` fixture's
    empty state-table Query — the same input every test above uses, read once
    here so the absence assertions all describe one payload.
    """
    with (
        patch("handlers.metrics._cache_table") as mock_cache,
        patch("handlers.metrics.cloudwatch_client") as mock_cw,
    ):
        mock_cache.get_item.return_value = {}
        mock_cw.get_metric_data.return_value = _make_cw_response({})

        from handlers.metrics import get_comprehensive_metrics

        result = get_comprehensive_metrics({"queryStringParameters": {"period": "24h"}})

    assert result["statusCode"] == 200, (
        "a window in which every source is absent must still return 200 (R4.10, R4.12) — "
        f"got {result['statusCode']}: {result['body']}"
    )
    return json.loads(result["body"])


def test_absent_sources_report_null_rather_than_a_value(absent_window_body):
    """R4.6, R4.7, R4.8, R4.9: an unmeasured metric is `null`, not its zero.

    Four sources, four collapses this replaces, named with the value each used to
    publish for this same input:

    * `overall_success_rate` — R4.6. Was `100.0`: a window in which nothing ran
      was indistinguishable from one in which everything succeeded.
    * `lines_ingested_24h` — R4.9. Was the count of `file_validation` rows, shown
      under a tile labelled "Records Ingested". Since 2026-10-10 the figure is
      rows loaded (`rows_written` on `load_metrics` rows); with no such row it is
      still `null`, never a row count.
    * `requests_per_minute` — R4.7. Was a hand-written `0`, a tile reporting a
      perfectly idle bucket for a bucket nobody is measuring.
    * `agentcore.availability` / `status` — R4.8. Was `100.0` and `healthy`.

    `processing_rate` is included because it is the rows-loaded figure divided by
    the window's wall clock: dividing "we do not know" by a number of hours does
    not produce a number of rows.
    """
    performance = absent_window_body["performance"]
    infrastructure = absent_window_body["infrastructure"]

    assert performance["overall_success_rate"] is None, (
        f"R4.6: zero terminal executions must report Not_Established, got {performance['overall_success_rate']!r}"
    )
    assert performance["lines_ingested_24h"] is None, (
        f"R4.9: no load_metrics row must report Not_Established, got {performance['lines_ingested_24h']!r}"
    )
    assert performance["processing_rate"] is None, (
        f"a rate over an absent numerator is not a rate, got {performance['processing_rate']!r}"
    )
    assert infrastructure["s3"]["requests_per_minute"] is None, (
        f"R4.7: S3 request metrics are not enabled, got {infrastructure['s3']['requests_per_minute']!r}"
    )
    assert infrastructure["agentcore"]["availability"] is None, (
        f"R4.8: an unmeasured error rate is an unmeasured availability, "
        f"got {infrastructure['agentcore']['availability']!r}"
    )
    assert infrastructure["agentcore"]["status"] == "unknown", (
        f"R4.8: health over an absent availability is 'unknown', never 'healthy' — "
        f"got {infrastructure['agentcore']['status']!r}"
    )


def test_determined_from_names_the_reason_for_each_absence(absent_window_body):
    """R4.2: every absent field carries a `<source>_absent` / `_incomplete` token.

    A `null` on its own says a measurement was not obtained; the token says
    whether we looked and found nothing or could not look. `LGR-b2f79ef-8`
    (template / ratified) is why those are two answers and not one.

    Asserted for each field the test above found `null`, so the payload cannot
    publish an absence with no reason attached.
    """
    performance = absent_window_body["performance"]
    determined_from = performance["determined_from"]

    for field in ("overall_success_rate", "processing_rate", "lines_ingested_24h"):
        assert field in determined_from, f"R4.2: {field} is null with no provenance token"

    # `requests_per_minute` and `agentcore_availability` are published under
    # `infrastructure`; the registry names them so their absence is reachable.
    for field in ("requests_per_minute", "agentcore_availability"):
        assert field in determined_from, f"R4.2: {field} is null with no provenance token"

    for field, token in determined_from.items():
        if performance.get(field) is None:
            assert token.endswith(_ABSENT) or token.endswith(_INCOMPLETE), (
                f"R4.2: {field} is null, so its token must end in '{_ABSENT}' or '{_INCOMPLETE}', got {token!r}"
            )


def test_missing_names_every_absent_performance_field(absent_window_body):
    """R4.3: `missing` is exactly the set of `null` field names, both directions.

    Two assertions, because either alone is satisfiable by a defect. A `missing`
    that under-reports leaves a `null` on screen with nothing to say why; a
    `missing` that over-reports names a field the payload published a number for
    — the self-contradiction inside one record that R4.3 exists to prevent.

    Scoped to `performance`'s own keys. Facts published elsewhere in the payload
    legitimately appear in `missing` without being `performance` keys
    (`agentcore_availability`, `requests_per_minute`, and — once task 4.1 lands —
    S3 storage), so the converse direction is asserted only over keys this
    section owns.
    """
    performance = absent_window_body["performance"]
    missing = performance["missing"]

    assert len(missing) == len(set(missing)), f"R4.3: `missing` contains duplicates: {missing}"

    null_performance_fields = {key for key, value in performance.items() if value is None}
    assert null_performance_fields <= set(missing), (
        f"R4.3: these fields are null but not named in `missing`: {sorted(null_performance_fields - set(missing))}"
    )

    for name in missing:
        if name in performance:
            assert performance[name] is None, (
                f"R4.3: `missing` names {name!r} while the payload publishes {performance[name]!r} for it"
            )

    # The four absences this window establishes are named, not merely derivable.
    for field in (
        "overall_success_rate",
        "lines_ingested_24h",
        "processing_rate",
        "agentcore_availability",
        "requests_per_minute",
    ):
        assert field in missing, f"R4.3: {field} is unavailable and must be named in `missing`"


def test_failed_pipelines_stays_a_number_and_is_never_missing(absent_window_body):
    """`failed_pipelines` is a plain number: absence there is a genuine zero.

    An `ExecutionsFailed` sum with no datapoints means nothing failed, which is a
    measurement. The read's failure case is not lost from the payload — its two
    siblings from the same States read, `overall_success_rate` and
    `terminal_counts`, carry the `_incomplete` token that says the read did not
    complete.

    Asserted so the `null`-everything sweep above cannot be satisfied by making
    every field nullable, which would trade one collapse for another.
    """
    performance = absent_window_body["performance"]

    assert isinstance(performance["failed_pipelines"], int | float), (
        f"`failed_pipelines` must stay a number, got {performance['failed_pipelines']!r}"
    )
    assert performance["failed_pipelines"] == 0
    assert "failed_pipelines" not in performance["missing"], (
        "nothing failed is a measurement, not an absence — `failed_pipelines` must not be named in `missing`"
    )


def test_terminal_counts_are_published_beside_the_rate(absent_window_body):
    """R5.4: the per-status counts the denominator was built from are on the payload.

    They are what makes the rate checkable arithmetic on screen rather than a
    number to take on trust — and on the verified window they are what makes the
    headline moving from 36.4% to 33.3% a corrected denominator rather than a new
    defect.

    The expected key set is read from `_TERMINAL_EXECUTION_METRICS`, so a terminal
    outcome added to that one named set (R5.5) appears here too and the published
    counts can never be a subset of the denominator's terms.

    This window is the case where the counts and the rate legitimately diverge:
    nothing ran, so there is no denominator and the *rate* is absent, while the
    four *counts* are an honest measurement of zero. That is exactly the
    arithmetic a reader needs to see the rate is absent for want of runs rather
    than for want of a read.
    """
    from handlers.metrics import _TERMINAL_EXECUTION_METRICS

    performance = absent_window_body["performance"]
    terminal_counts = performance["terminal_counts"]

    assert terminal_counts is not None, (
        "R5.4: a successful read of an empty window measured zero of each outcome; the counts are not absent"
    )
    assert set(terminal_counts) == set(_TERMINAL_EXECUTION_METRICS), (
        f"R5.4: published counts {sorted(terminal_counts)} must cover every terminal outcome "
        f"{sorted(_TERMINAL_EXECUTION_METRICS)}"
    )
    assert all(count == 0 for count in terminal_counts.values()), f"got {terminal_counts}"
    assert performance["overall_success_rate"] is None, (
        "R4.6: a denominator of zero has no rate, however many counts are published"
    )
    assert "terminal_counts" not in performance["missing"], (
        "a measured zero for each outcome is a measurement — `terminal_counts` is not missing here"
    )


def test_trends_report_not_established_for_an_absent_metric(absent_window_body):
    """R4.4 / R4.1 at the payload: no baseline, no direction.

    Both windows of this request are unmeasured for ingestion and for success, so
    there is no change to take the direction of. `stable` would say the metric
    held level, which is a measurement of two windows neither of which was
    measured.

    Only the two trends whose sources can answer `None` today are asserted;
    `agent_latency` and `cost` still read a bare float and are honestly `stable`
    for a measured zero pair (tasks 15.1 and 6.1).
    """
    trends = absent_window_body["performance"]["trends"]

    for field in ("ingestion", "success"):
        assert trends[field] == "not_established", (
            f"R4.4: {field} has no baseline and no current value, so its trend is 'not_established', "
            f"got {trends[field]!r}"
        )
        assert trends[field] != "stable", (
            f"R4.4: absence must not be reported as stability — an operator cannot tell an unmeasured "
            f"metric from a steady one (got {field}={trends[field]!r})"
        )


def _key_condition_bounds(kwargs: dict) -> tuple[str | None, tuple[str, str] | None]:
    """The `step_name` partition and `timestamp` BETWEEN bounds of one Query call."""
    from boto3.dynamodb.conditions import AttributeBase

    step: str | None = None
    bounds: tuple[str, str] | None = None
    pending = [kwargs["KeyConditionExpression"]]
    while pending:
        expr = pending.pop().get_expression()
        if expr["operator"] in ("AND", "OR"):
            pending.extend(expr["values"])
            continue
        attribute, *operands = expr["values"]
        name = attribute.name if isinstance(attribute, AttributeBase) else str(attribute)
        if expr["operator"] == "=" and name == "step_name":
            step = operands[0]
        elif expr["operator"] == "BETWEEN" and name == "timestamp":
            bounds = (operands[0], operands[1])
    return step, bounds


@pytest.mark.parametrize("period", ["1h", "7d"])
def test_rows_loaded_and_rate_reach_the_payload_from_load_metrics_rows(period):
    """2026-10-10: the two tiles publish rows the Glue jobs confirmed writing.

    The state table answers the `load_metrics` partition with four rows inside the
    requested window, served only where the Query's own BETWEEN bounds admit them
    (as DynamoDB would). By the requirement:

    * the figure is `rows_written` summed where `write_confirmed` is true —
      1200 + 300 = 1500; the unconfirmed 5000 and the empty-source 0 add nothing;
    * `processing_rate` is that figure ÷ the window's wall-clock hours, at least 1,
      rounded to 2 places — computed here from `PERIOD_MAP`, not read back;
    * both carry the bare `load_metrics_rows` token and neither is `missing`.

    `1h` exercises the `max(hours, 1)` floor's boundary and `7d` a real division.
    """
    from decimal import Decimal

    from handlers.metrics import PERIOD_MAP, get_comprehensive_metrics

    stamp = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    load_metrics_rows = [
        {"timestamp": stamp, "rows_written": 1200, "write_confirmed": True},
        {"timestamp": stamp, "rows_written": Decimal("300"), "write_confirmed": True},
        {"timestamp": stamp, "rows_written": 5000, "write_confirmed": False},
        {"timestamp": stamp, "rows_written": 0, "write_confirmed": False},
    ]
    expected_rows = 1200 + 300
    window_hours = PERIOD_MAP[period].total_seconds() / 3600
    expected_rate = round(expected_rows / max(window_hours, 1), 2)

    def query(**kwargs):
        step, bounds = _key_condition_bounds(kwargs)
        if step != "load_metrics" or bounds is None:
            return {"Items": []}
        low, high = bounds
        return {"Items": [row for row in load_metrics_rows if low <= row["timestamp"] <= high]}

    with (
        patch("handlers.metrics.state_table") as mock_state,
        patch("handlers.metrics._cache_table") as mock_cache,
        patch("handlers.metrics.cloudwatch_client") as mock_cw,
    ):
        mock_state.query.side_effect = query
        mock_cache.get_item.return_value = {}
        mock_cw.get_metric_data.return_value = _make_cw_response({})

        result = get_comprehensive_metrics({"queryStringParameters": {"period": period}})

    assert result["statusCode"] == 200, result["body"]
    performance = json.loads(result["body"])["performance"]

    assert performance["lines_ingested_24h"] == expected_rows
    assert performance["processing_rate"] == expected_rate
    for field in ("lines_ingested_24h", "processing_rate"):
        assert performance["determined_from"][field] == "load_metrics_rows", performance["determined_from"][field]
        assert field not in performance["missing"], f"{field} was measured and must not be named in `missing`"
    mock_state.scan.assert_not_called()
