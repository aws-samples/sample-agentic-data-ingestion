# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Requirement-derived test — ``_get_agent_latency`` (``handlers/metrics.py:1283``).

Feature: p39-metrics-truth, task 15.2. Task 15.1 re-sourced the Avg Agent
Latency tile: it used to average the four dispatcher Lambdas' ``AWS/Lambda``
``Duration`` — a verified ~1 s dispatch ack under a heading about agent
performance — and now averages ``Pipeline.StageDuration`` across the four agent
stages in ``_AGENT_STAGE_NAMES`` (D1, ratified). **No existing test called
``_get_agent_latency`` at all** (established by grep over ``tests/``, because
``lambda/**`` tests reach their target through a ``sys.path``-injected alias the
symbol index cannot follow — requirements risk 6; design.md records the same).
This file is that coverage.

**Expectations come from the requirements, not from the function's output.**
``R7.4`` says the figure is sourced from the agent's own work time; ``R4.1``
says an unobtained measurement is reported as absent rather than as a value;
``R4.2`` says the absence names which of the three kinds it is. Every expected
number below is computed from the stub's input table by this file's own
:func:`_expected_average`, never by calling the function and copying the answer
— ``LGR-28b4d75-5`` (template / ratified): a test written from producer output
cannot detect producer drift, because the output is its own oracle.

**Four arms, because a test covering three leaves the fourth unpinned.**

1. datapoints on the agent stages ⇒ their average, with the bare source token;
2. the query returned and no stage has a datapoint ⇒ ``None`` + ``_absent``;
3. the query raised ⇒ ``None`` + ``_incomplete``, logged at WARNING.
   ``LGR-b2f79ef-8`` (template / ratified) is the fence: "nothing ran" and "we
   could not look" must not collapse into one answer, because an operator acts
   differently on each;
4. the reduction series missing from the response entirely ⇒ ``_absent``. The
   function's docstring states why this is not ``_incomplete``: CloudWatch
   returns the ids it was asked for, so an absent id says no more about the
   window than an empty ``Values`` list does.

**Partial data is arm 1's live reality and is pinned separately.** Two of the
four stages emit and two are silent. The average is over the terms that came
back; a four-way average with zeros for the silent stages would claim those
stages ran instantly. ``LGR-4854b3d-1`` (template / ratified), constraint 18:
a three-state value is never collapsed to its zero. Only all four being absent
reaches arm 2.

**Two assertions are structural rather than value-based.**

* *The emitted expression names no dispatcher source.* A future revert to
  ``AWS/Lambda`` ``Duration`` then fails a test rather than a screen — the same
  guard shape as task 2.3's ``test_metrics_polarity.py``. The forbidden tokens
  are partly derived (``_AGENT_FUNCTIONS``, which survives as
  ``_get_agent_error_rate``'s function list) and partly literal, because a
  literal is what is being forbidden.
* *The brace dimension set equals the set ``record_stage_execution``
  publishes.* Task 13.3 established this pattern in
  ``tests/unit/shared/test_metrics_facade_service_dimension.py`` for
  ``_stage_durations_cw``; ``_get_agent_latency`` is a sibling expression
  sharing that brace and needs the same protection. A CloudWatch SEARCH brace
  schema is an EXACT match on the dimension-NAME set, so naming three of the
  four matches nothing and looks exactly like an idle system. Both sides are
  read out of the code; neither is restated here.

**What a unit test cannot pin, stated rather than implied.** The cross-stage
totals are computed by CloudWatch's ``SUM`` over two SEARCH arrays (the ``Sum``
and ``SampleCount`` statistics), server-side. The stub below emulates that so
the arms are exercisable at all, which means the value assertions pin the
*consumer's* half: that it reads the reductions and not the raw series, that it
divides total duration by total invocations, and that neither the fixed stage
count nor an empty series enters the divisor. The query itself is pinned
structurally instead — that is what the two guards above are for.

**AMENDED 2026-10-10.** The reduction used to be ``AVG`` over a SEARCH of the
``Average`` statistic, and the stub used to emulate ``AVG`` as a mean over the
series *present* at each timestamp. Live CloudWatch does not behave that way: a
SEARCH also returns indexed series with no datapoint in the window, and ``AVG``
counts them as zeros. On a development deployment the tile read 2 s against a 25.5 s
invocation mean. The stub now returns those empty series too, and
``test_an_idle_pipeline_series_does_not_dilute_the_average`` encodes the
measurement.

Mocking: CloudWatch is not one of the services the shared fixtures cover
(DynamoDB / S3 / SQS), so the module-level ``cloudwatch_client`` is stubbed, as
in the sibling ``test_metrics_stage_durations_char.py``. No ``mock_aws()``, no
boto3 client mock for a fixture-covered service.

**Validates: Requirements 7.4, 4.1, 4.2**
"""

import os
import re
import sys
from datetime import UTC, datetime, timedelta
from statistics import fmean
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason the sibling
# test_metrics_stage_durations_char.py repeats it — under xdist --dist=loadfile
# any file in this directory may be the first one imported. ``shared`` is on the
# list because the producer half of the dimension-set comparison lives there.
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

#: ``R4.2``'s vocabulary, written as the requirement words it. The base token is
#: read off the module so a renamed source follows; the suffixes are the
#: requirement's own and are literals on purpose.
SOURCE = metrics_mod.SOURCE_STAGE_DURATION
ABSENT = f"{SOURCE}_absent"
INCOMPLETE = f"{SOURCE}_incomplete"


# ---------------------------------------------------------------------------
# Expression introspection
# ---------------------------------------------------------------------------

# SEARCH('{<namespace>,<dim>,...} <filters>', '<stat>', <period>)
_SEARCH_RE = re.compile(
    r"^\s*SEARCH\(\s*'\{(?P<schema>[^}]*)\}(?P<filters>[^']*)'\s*,\s*'(?P<stat>[^']*)'\s*,\s*(?P<period>\d+)\s*\)\s*$"
)

# SUM(<id>) — the reduction across a SEARCH array.
_REDUCTION_RE = re.compile(r"^\s*(?P<func>[A-Z]+)\(\s*(?P<arg>\w+)\s*\)\s*$")

# StageName=("A" OR "B") — the queried stage set, whatever its order.
_STAGE_FILTER_RE = re.compile(r"StageName=\(([^)]*)\)")


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

    @property
    def stage_names(self) -> set[str]:
        """The ``StageName`` values the filter asks for."""
        match = _STAGE_FILTER_RE.search(self.filters)
        assert match, f"no StageName filter in {self.filters!r}"
        return {part.strip().strip('"') for part in match.group(1).split(" OR ")}


class _FakeCloudWatch:
    """A CloudWatch stub that serves per-stage datapoints the way the live service does.

    * Each ``SEARCH`` returns one series per matching stage, **including an
      empty series for a stage with no datapoints** (live CloudWatch returns
      every indexed series for the brace, not only those with data in the
      window). ``idle_series`` adds further empty series, standing in for other
      pipelines that ran the stage once.
    * ``SUM(<array>)`` reduces element-wise to the sum of the values present at
      each timestamp, so an empty series contributes nothing.
    * Each datapoint is one invocation: the ``Sum`` statistic is the value and
      the ``SampleCount`` statistic is 1.

    ``stage_values`` is the table of what each stage published. Stages absent
    from it, or mapped to an empty list, are silent.
    """

    def __init__(
        self, stage_values: dict[str, list[float]], *, omit_reduction: bool = False, idle_series: int = 0
    ) -> None:
        self.stage_values = dict(stage_values)
        self.omit_reduction = omit_reduction
        self.idle_series = idle_series
        self.calls: list[dict] = []

    # -- the API surface the handler uses -----------------------------------

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        queries = kwargs["MetricDataQueries"]

        searches = {q["Id"]: _Search(q["Expression"]) for q in queries if _SEARCH_RE.match(q["Expression"])}
        assert {s.stat for s in searches.values()} == {"Sum", "SampleCount"}, (
            f"expected one Sum and one SampleCount SEARCH, got {[q['Expression'] for q in queries]}"
        )

        def arrays(search: _Search) -> list[list[float]]:
            per_stage = [
                [1.0 if search.stat == "SampleCount" else v for v in self.stage_values.get(name, [])]
                for name in search.stage_names
            ]
            return per_stage + [[] for _ in range(self.idle_series)]

        if self.omit_reduction:
            # What CloudWatch would return if the reduction ids vanished: the raw
            # arrays only. Doubles as proof the raw values cannot leak past them.
            return {
                "MetricDataResults": [
                    {"Id": raw_id, "Values": vals, "Timestamps": self._timestamps(len(vals))}
                    for raw_id, search in searches.items()
                    for vals in arrays(search)
                ]
            }

        results = []
        for query in queries:
            if query["Id"] in searches:
                continue
            match = _REDUCTION_RE.match(query["Expression"])
            assert match, f"unrecognised reduction expression: {query['Expression']!r}"
            assert match.group("func") == "SUM", f"the reductions must be SUM, got {query['Expression']!r}"
            assert match.group("arg") in searches, f"{query['Expression']!r} reduces no SEARCH array"
            values = self._sum(arrays(searches[match.group("arg")]))
            results.append({"Id": query["Id"], "Values": values, "Timestamps": self._timestamps(len(values))})
        return {"MetricDataResults": results}

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _sum(series: list[list[float]]) -> list[float]:
        width = max((len(vals) for vals in series), default=0)
        return [sum(vals[i] for vals in series if i < len(vals)) for i in range(width)]

    @staticmethod
    def _timestamps(count: int) -> list[datetime]:
        return [START + timedelta(seconds=PERIOD * i) for i in range(count)]


def _expected_average(stage_values: dict[str, list[float]]) -> float:
    """The figure ``R7.4`` asks for: the mean agent-stage duration per invocation.

    Derived from the input table, independently of the function under test:
    every datapoint is one invocation, so the expectation is the mean of all
    datapoints across the stages that reported. Silent stages contribute no term.
    """
    every = [value for vals in stage_values.values() for value in vals]
    assert every, "_expected_average is only meaningful when at least one stage reported"
    return fmean(every)


def _run(
    stage_values: dict[str, list[float]] | None = None,
    *,
    omit_reduction: bool = False,
    idle_series: int = 0,
    side_effect: Exception | None = None,
) -> tuple[metrics_mod.MetricOutcome, MagicMock | _FakeCloudWatch]:
    """Call ``_get_agent_latency`` against a stubbed CloudWatch client."""
    client: MagicMock | _FakeCloudWatch
    if side_effect is not None:
        client = MagicMock()
        client.get_metric_data.side_effect = side_effect
    else:
        client = _FakeCloudWatch(stage_values or {}, omit_reduction=omit_reduction, idle_series=idle_series)

    with patch.object(metrics_mod, "cloudwatch_client", client):
        outcome = metrics_mod._get_agent_latency(START, END, PERIOD)
    return outcome, client


def _emitted_expressions(client: _FakeCloudWatch) -> list[str]:
    assert len(client.calls) == 1, f"expected one get_metric_data call, got {len(client.calls)}"
    return [q["Expression"] for q in client.calls[0]["MetricDataQueries"]]


def _the_search(client: _FakeCloudWatch) -> _Search:
    for expression in _emitted_expressions(client):
        if _SEARCH_RE.match(expression):
            return _Search(expression)
    raise AssertionError(f"no SEARCH expression emitted: {_emitted_expressions(client)}")


# ═══════════════════════════════════════════════════════════════════════════
# Arm 1 — datapoints on the agent stages report their average (R7.4)
# ═══════════════════════════════════════════════════════════════════════════


def test_average_across_the_agent_stages_when_all_four_report():
    """All four agent stages emitting ⇒ the mean of the four, with the bare token.

    **Validates: Requirements 7.4, 4.2**
    """
    # One distinct value per stage, generated from the stage set so a change to
    # its size does not turn this into a length assertion.
    values = [1000.0 * (i + 1) for i in range(len(metrics_mod._AGENT_STAGE_NAMES))]
    table = {name: [value] for name, value in zip(metrics_mod._AGENT_STAGE_NAMES, values, strict=True)}
    expected = _expected_average(table)  # computed from the table, not from the function

    outcome, _client = _run(table)

    assert outcome.value == pytest.approx(expected)
    assert outcome.determined_from == SOURCE, (
        "a measurement carries the bare source token — the `_absent` / `_incomplete` suffixes "
        f"are for the two absence kinds (R4.2). Got {outcome.determined_from!r}."
    )


def test_average_over_the_window_is_a_mean_not_a_sum():
    """Several datapoints per stage ⇒ the mean over the window, not their total.

    **Validates: Requirements 7.4**
    """
    first, second = metrics_mod._AGENT_STAGE_NAMES[:2]
    table = {first: [1000.0, 3000.0], second: [2000.0, 2000.0]}
    expected = _expected_average(table)  # mean(1500, 2500) = 2000.0

    outcome, _client = _run(table)

    assert outcome.value == pytest.approx(expected)


def test_partial_data_averages_only_the_stages_that_reported():
    """Two of four emitting ⇒ the mean of the two, never a four-way average.

    This is the live shape: ``CodeGeneration`` and ``CodeTesting`` publish while
    ``CreateGluePipeline`` and ``CreateInfrastructure`` have not run since the
    producer fix deployed. A silent stage contributes no term — giving it a zero
    would claim it ran instantly, which ``LGR-4854b3d-1`` (template / ratified,
    constraint 18) forbids: a three-state value is never collapsed to its zero.

    The input values are the live-verified per-stage averages; the expectation is
    still computed from the table, not taken from the deployed figure.

    **Validates: Requirements 7.4, 4.1**
    """
    reporting, silent = metrics_mod._AGENT_STAGE_NAMES[:2], metrics_mod._AGENT_STAGE_NAMES[2:]
    table: dict[str, list[float]] = {reporting[0]: [5245.2], reporting[1]: [4667.3]}
    table.update({name: [] for name in silent})

    expected = _expected_average(table)
    zero_substituted = sum(vals[0] for vals in table.values() if vals) / len(metrics_mod._AGENT_STAGE_NAMES)

    outcome, _client = _run(table)

    assert outcome.value == pytest.approx(expected), (
        f"expected the mean of the {len(reporting)} reporting stages ({expected}); got {outcome.value}"
    )
    assert outcome.value != pytest.approx(zero_substituted), (
        f"{outcome.value} is the reporting stages' sum divided by all "
        f"{len(metrics_mod._AGENT_STAGE_NAMES)} stages — a zero term for every silent stage, which claims "
        f"{sorted(silent)} ran instantly. LGR-4854b3d-1: no zero-substitution."
    )
    assert outcome.determined_from == SOURCE, (
        "a partially-reporting window is a measurement, not an absence: only all four stages being "
        f"silent reaches the absent arm. Got {outcome.determined_from!r}."
    )


def test_an_idle_pipeline_series_does_not_dilute_the_average():
    """Empty series beside a reporting one leave its average unchanged.

    The measured 2026-10-10 case: six pipelines had a ``CodeGeneration`` series,
    one ran (3024.7, 3001.8), and ``AVG`` read ~502 ms. The weighted mean divides
    by invocations, which an empty series does not add.

    **Validates: Requirements 7.4, 4.1**
    """
    table: dict[str, list[float]] = {name: [] for name in metrics_mod._AGENT_STAGE_NAMES}
    table[metrics_mod._AGENT_STAGE_NAMES[0]] = [3024.7, 3001.8]

    outcome, _client = _run(table, idle_series=5)

    assert outcome.value == pytest.approx(_expected_average(table))
    assert outcome.value == pytest.approx(3013.25)


def test_one_stage_reporting_is_reported_as_itself():
    """A single emitting stage ⇒ its own average, undivided.

    **Validates: Requirements 7.4**
    """
    table: dict[str, list[float]] = {name: [] for name in metrics_mod._AGENT_STAGE_NAMES}
    table[metrics_mod._AGENT_STAGE_NAMES[0]] = [4321.0]

    outcome, _client = _run(table)

    assert outcome.value == pytest.approx(_expected_average(table))
    assert outcome.determined_from == SOURCE


# ═══════════════════════════════════════════════════════════════════════════
# Arms 2-4 — the three ways there is no number (R4.1, R4.2)
# ═══════════════════════════════════════════════════════════════════════════


def test_no_datapoints_on_any_agent_stage_reports_absent():
    """Every agent stage silent ⇒ ``None`` and an ``_absent`` token.

    ``R4.1``: no measurement was obtained, so the tile says nothing rather than
    standing the dispatcher's ~1 s figure in for it.

    **Validates: Requirements 4.1, 4.2**
    """
    outcome, _client = _run({name: [] for name in metrics_mod._AGENT_STAGE_NAMES})

    assert outcome.value is None, (
        f"an unmeasured window reports no value, not {outcome.value!r} — a zero would read as an "
        "agent that answered instantly (R4.1)."
    )
    assert outcome.determined_from == ABSENT


def test_missing_reduction_series_is_treated_as_absent():
    """The reduction id absent from the response ⇒ ``_absent``, not a raw value.

    CloudWatch returns the ids it was asked for, so a missing id says no more
    about the window than an empty ``Values`` list does — which is why this is
    ``_absent`` and not ``_incomplete``. The response still carries the raw
    SEARCH array, so this also pins that the raw series (fetched with
    ``ReturnData: False``) cannot leak past the reduction.

    **Validates: Requirements 4.1, 4.2**
    """
    table = {name: [9_999.0] for name in metrics_mod._AGENT_STAGE_NAMES}

    outcome, _client = _run(table, omit_reduction=True)

    assert outcome.value is None, f"raw SEARCH values leaked past the SUM reductions: {outcome.value!r}"
    assert outcome.determined_from == ABSENT


def test_raising_client_reports_incomplete_and_warns():
    """A failed query ⇒ ``None`` + ``_incomplete``, logged at WARNING.

    ``LGR-b2f79ef-8`` (template / ratified) is the fence this pins: "nothing
    ran" and "we could not look" are different facts and must not collapse into
    one answer. An operator acts differently on each, so the token has to say
    which — and the failure has to be visible in the log as well as in the
    payload.

    **Validates: Requirements 4.1, 4.2**
    """
    with patch.object(metrics_mod.logger, "warning") as mock_warn:
        outcome, _client = _run(side_effect=RuntimeError("cloudwatch is down"))

    assert outcome.value is None
    assert outcome.determined_from == INCOMPLETE, (
        "a query that could not be read is `_incomplete`; spelling it `_absent` would claim the "
        f"agent stages were silent on evidence that proves nothing was observed. Got {outcome.determined_from!r}."
    )
    assert mock_warn.call_count == 1, "a read that failed must be recorded, not only reported"
    assert "cloudwatch is down" in str(mock_warn.call_args)


# ═══════════════════════════════════════════════════════════════════════════
# Structural guards — the query, not the value
# ═══════════════════════════════════════════════════════════════════════════


def test_the_dispatcher_lambda_duration_is_never_read():
    """No emitted expression names the dispatcher source (``R7.4``).

    The tile used to average the four dispatcher Lambdas' ``AWS/Lambda``
    ``Duration``, which measures the async-invoke ack — ~1 s against a workload
    measured in minutes — and answers no question an operator has. A revert to
    it now fails here rather than being spotted on a screen. Same guard shape as
    task 2.3's ``test_metrics_polarity.py``.

    ``_AGENT_FUNCTIONS`` survives as ``_get_agent_error_rate``'s function list
    (D1 ratified keeping its ``AWS/Lambda`` source, since a dispatcher error IS
    a genuine agent-invocation error), so the function names are read from the
    module rather than restated — a change to that list keeps this guard honest.

    **Validates: Requirements 7.4**
    """
    outcome, client = _run({name: [100.0] for name in metrics_mod._AGENT_STAGE_NAMES})
    assert outcome.value is not None, "guarding a query that returned nothing would be near-vacuous"

    expressions = _emitted_expressions(client)
    forbidden = ["AWS/Lambda", 'MetricName="Duration"', "FunctionName", *metrics_mod._AGENT_FUNCTIONS]

    violations = [
        f"{token!r} appears in {expression!r}"
        for expression in expressions
        for token in forbidden
        if token in expression
    ]
    assert not violations, (
        "the agent-latency tile reads the agent's own work time, not the dispatcher's ack:\n  "
        + "\n  ".join(violations)
    )

    search = _the_search(client)
    assert search.namespace == "AgenticDataIngestion/Pipeline", (
        f"the agent's stage duration lives in the pipeline namespace; got {search.namespace!r}"
    )
    assert 'MetricName="Pipeline.StageDuration"' in search.filters
    assert search.stage_names == set(metrics_mod._AGENT_STAGE_NAMES), (
        "the queried stage set must be the agent stage set exactly — a dropped stage silently "
        f"narrows the average. Queried {sorted(search.stage_names)}, expected "
        f"{sorted(metrics_mod._AGENT_STAGE_NAMES)}."
    )


def _emitted_stage_duration_dimension_names(monkeypatch) -> set[str]:
    """The dimension NAMES the producer puts on ``Pipeline.StageDuration``.

    Read off the ``put_metric_data`` call ``record_stage_execution`` actually
    makes, not off the buffer: the buffer is an implementation detail and the
    API call is what CloudWatch indexes. The container path is forced because
    that is the path that publishes through the API at all — the EMF path writes
    a log record instead. ``metrics_facade`` resolves ``POWERTOOLS_ENABLED`` at
    import time (``metrics_facade.py:38-46``), so the flag is only reachable by
    dropping the module from the cache and importing again. Same helper as
    ``tests/unit/shared/test_metrics_facade_service_dimension.py``.
    """
    monkeypatch.setenv("POWERTOOLS_ENABLED", "false")
    sys.modules.pop("metrics_facade", None)
    try:
        import metrics_facade

        assert metrics_facade.POWERTOOLS_AVAILABLE is False, "this is the container path — Powertools must be off"

        mock_cw = MagicMock()
        metrics_facade.metrics_facade._cloudwatch = mock_cw
        metrics_facade.PipelineMetrics.record_stage_execution(
            stage_name=metrics_mod._AGENT_STAGE_NAMES[0],
            pipeline_id="pipe-agent-latency",
            status="completed",
            duration_ms=5245.2,
        )

        assert mock_cw.put_metric_data.call_count == 1, "the stage flush publishes in one call"
        published = {m["MetricName"]: m for m in mock_cw.put_metric_data.call_args.kwargs["MetricData"]}
        return {d["Name"] for d in published["Pipeline.StageDuration"]["Dimensions"]}
    finally:
        # Evict the reloaded module so no flag state leaks to another file.
        sys.modules.pop("metrics_facade", None)


def test_brace_dimension_set_equals_what_the_producer_publishes(monkeypatch):
    """The searched dimension-NAME set is the emitted one, exactly (``R7.4``).

    A CloudWatch SEARCH brace schema is an exact match on the dimension-name
    set, so a consumer naming three of the producer's four reads an empty series
    while the datapoints sit in the namespace — indistinguishable, on screen,
    from an idle system. That is the failure task 13.3 caught for
    ``_stage_durations_cw`` and pinned in
    ``tests/unit/shared/test_metrics_facade_service_dimension.py``;
    ``_get_agent_latency`` is a sibling expression sharing the same brace and
    carries the same exposure.

    Both sides are derived — the searched set out of the expression the function
    builds, the emitted set off the ``put_metric_data`` call the producer makes
    — so a change to either fails here instead of going quiet.

    **Validates: Requirements 7.4**
    """
    _outcome, client = _run({name: [100.0] for name in metrics_mod._AGENT_STAGE_NAMES})
    searched = set(_the_search(client).dimensions)
    emitted = _emitted_stage_duration_dimension_names(monkeypatch)

    assert searched == emitted, (
        f"_get_agent_latency searches dimension names {sorted(searched)} but "
        f"Pipeline.StageDuration is published with {sorted(emitted)}. A SEARCH brace schema is an "
        f"EXACT match on that set, so the read returns nothing and the tile reads as idle. "
        f"Missing from the search: {sorted(emitted - searched)}; searched but not emitted: "
        f"{sorted(searched - emitted)}."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
