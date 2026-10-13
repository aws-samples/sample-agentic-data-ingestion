# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test — the success rate covers every terminal outcome.

Feature: p39-metrics-truth, task 21.2. Subjects: ``_success_rate``
(``handlers/metrics.py:825``), the read it consumes (``_get_sfn_metrics``,
``:925``) and the ``overall_success_rate`` / ``terminal_counts`` pair
``_build_comprehensive_metrics`` publishes (``:523``, ``:715``).

# Feature: p39-metrics-truth, Property 3: The success rate is computed over every terminal outcome and its denominator is reconstructible

*For any* window whose terminal execution counts sum to a positive number, the
reported rate is ``succeeded ÷ (the sum of every terminal outcome) × 100``.
*For any* window whose terminal counts sum to zero, the reported rate is absent
— never ``100.0``. *For any* window at all, recomputing the rate from the
per-status counts the payload publishes reproduces the rate the payload
publishes. And *for any* perturbation of ``started`` alone, the rate does not
move.

**Validates: Requirements 4.6, 5.1, 5.2, 5.3, 5.4**

Where the expectations come from
--------------------------------

**Every expectation below is arithmetic on the generated counts, and none of it
is produced by calling the subject.** `LGR-28b4d75-5` (**template / ratified**)
is the rule: a test written from the producer's output cannot detect the
producer drifting, because the output is its own oracle. That is not abstract
here — the pre-p39 denominator was ``succeeded + failed``, so on the verified
7-day window (12 / 14 / 10 / 0 against 36 started) it reported 36.4% where the
honest figure is 33.3%, and a test that had recorded 36.4% would have pinned the
defect. ``R9.4`` is the same rule for this spec.

So the oracle is ``R5.3``'s own sentence — "succeeded divided by the sum of all
four, expressed as a percentage" — evaluated on the counts the stub was told to
report. The **headline moving from 36.4% to 33.3% is the point of the change,
not a defect**, and ``R5.4``'s published counts are what make that arithmetic
checkable on screen rather than a number that changed for unstated reasons.

The denominator is derived, never re-spelled
--------------------------------------------

Nothing below names four statuses. ``handlers/metrics.py`` builds the SEARCH
queries **and** the denominator from one named mapping,
``_TERMINAL_EXECUTION_METRICS`` (``:308``), precisely so that ``R5.5`` — "where
the deployed state machine emits an additional terminal execution metric,
include that metric in the denominator" — is a one-line addition. This file
reads its denominator from that same mapping (:data:`DENOMINATOR_KEYS`) and its
CloudWatch stub answers **by metric name** rather than by query id, so a fifth
terminal outcome added to the mapping is generated, stubbed, summed and asserted
here automatically. If it is ever added to the query set without reaching the
denominator, or the reverse, this property fails rather than quietly covering
three of five.

Two names *are* written out literally, because a requirement fixes them and
reading them from the module would make the assertion circular:
``ExecutionsSucceeded`` is ``R5.3``'s numerator, and ``ExecutionsStarted`` is
``R5.2``'s exclusion.

Float tolerance, and why it cannot hide a wrong denominator
-----------------------------------------------------------

The rate is a quotient, so the comparisons use :func:`pytest.approx` rather than
``==``. The tolerance has to be loose enough to survive the last bit of a
division and tight enough that a rate computed over three statuses instead of
four still fails. Both hold with room to spare, and the margin is a ratio rather
than a hope:

* :func:`test_rate_is_succeeded_over_every_terminal_outcome` compares the
  **unrounded** rate at ``rel=`` :data:`_RELATIVE_TOLERANCE` ``=1e-9``. Write
  ``S`` for the numerator, ``T`` for the four-status total and ``x`` for the
  count of a status a wrong denominator dropped. The tolerance is
  ``1e-9 × 100·S/T``; the gap the drop opens is ``100·S·x / (T·(T−x))``; their
  ratio is ``1e9 · x/(T−x)``, which is at least ``1e9/T``. ``T`` is bounded by
  :data:`_COUNT_MAX` × the number of statuses — 20 000 today — so the smallest
  wrong-denominator gap is at least **50 000×** the tolerance for every input
  this file can generate, and a fifth status only raises the bound to 25 000
  (40 000×). Note the ratio is independent of ``S``: the tolerance and the gap
  shrink together, which is why a relative tolerance is the right shape here.
  One thing the ratio does not cover, and the generator does: a numerator of
  zero gives ``0.0`` under any denominator, so one branch draws **every** status
  strictly positive and the margin assertion skips the ``S = 0`` examples.
* :func:`test_published_counts_reproduce_the_published_rate` works against the
  payload, where the rate is published rounded to 1dp (``:617``). Rounding is a
  coarse grain — at large totals a dropped status can shift the rate by less
  than half the last digit — so that clause does not rely on the tolerance at
  all: it asserts, per generated example, that dropping any single status
  changes the **published** figure, and its generator is bounded so that this
  holds. The tolerance there is only absorbing the division (``abs=1e-9``).

Scope
-----

Deliberately **not** asserted here:

* ``failed_pipelines``. It is a separate ``ExecutionsFailed`` sum and stays a
  plain number — an absent datapoint there is a genuine zero, nothing failed —
  so it is not part of this property.
* the ``_incomplete`` arm. A raising States read is
  ``test_metrics_absence.py``'s subject (task 3.6) and does not bear on the
  denominator's composition.
* ``set(missing)`` against the null field names, which is Property 1 (task 21.1).

**Expected result: green on the first run.** Task 3.3 landed the two new SEARCH
queries, the named-set denominator and ``terminal_counts``. A red here is a real
finding about that code, not a property to adjust.

Mocking
-------

CloudWatch is not one of the services the shared fixtures cover (DynamoDB / S3 /
SQS), so the module-level ``cloudwatch_client`` is patched with a ``MagicMock``
— the established pattern for these read paths
(``test_metrics_stage_durations_char.py``, ``test_metrics_cost.py``,
``test_query_not_scan.py``). No ``mock_aws()`` is created here and no
fixture-covered service is stubbed by hand. ``state_table`` is patched because
``_get_rows_loaded`` and ``_get_stage_metrics`` Query its GSI on every
payload build (P28 R17.1). No AWS call is made. ``AWS_REGION`` is deliberately
**not** set — the unit layer pins it by assignment in ``tests/unit/conftest.py``
(`LGR-3e45e65-1`, **template / ratified**).

_Requirements: R4.6, R5.1, R5.2, R5.3, R5.4
"""

import os
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason test_metrics_cost.py and
# the task-1 characterization files repeat it — under xdist --dist=loadfile any
# file in this directory may be the first one imported on a worker.
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
# bootstrap as test_query_not_scan.py and the task-1 characterization files).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

# ═══════════════════════════════════════════════════════════════════════════
# The denominator, read from the one named set that defines it
# ═══════════════════════════════════════════════════════════════════════════

#: Payload key -> AWS/States metric name, for every TERMINAL outcome. Read from
#: the module rather than re-spelled, because `R5.5` makes this mapping the
#: single place an outcome is added — see the module docstring.
TERMINAL_METRICS: dict[str, str] = metrics_mod._TERMINAL_EXECUTION_METRICS

#: The denominator's terms. Derived, so a fifth terminal outcome is covered by
#: every clause below the moment it joins the mapping.
DENOMINATOR_KEYS: tuple[str, ...] = tuple(TERMINAL_METRICS)

#: `R5.3`'s numerator, and `R5.2`'s exclusion. Written out literally: these two
#: metric names are the requirements' own subjects, and reading them out of the
#: module would make the assertions circular.
SUCCEEDED_METRIC_NAME = "ExecutionsSucceeded"
STARTED_METRIC_NAME = "ExecutionsStarted"

#: The mapping's key for `R5.3`'s numerator, located by metric name. A rename of
#: the payload key leaves every clause below working; a mapping that no longer
#: carries `ExecutionsSucceeded` at all fails loudly, at import.
_NUMERATOR_KEYS = [key for key, name in TERMINAL_METRICS.items() if name == SUCCEEDED_METRIC_NAME]
assert len(_NUMERATOR_KEYS) == 1, (
    f"R5.3 makes {SUCCEEDED_METRIC_NAME} the numerator, so exactly one key of "
    f"_TERMINAL_EXECUTION_METRICS must map to it — got {_NUMERATOR_KEYS!r} from {TERMINAL_METRICS!r}"
)
NUMERATOR_KEY = _NUMERATOR_KEYS[0]

#: The namespace the terminal counts are read from, used to tell the States read
#: apart from the payload's other CloudWatch reads.
STATES_NAMESPACE = "AWS/States"

#: `R4.6`'s forbidden answer for a window in which nothing reached a terminal
#: outcome. Before p39 this was the answer: a perfect record claimed on no
#: evidence, and indistinguishable from a window in which everything succeeded.
PERFECT_RATE = 100.0

# ═══════════════════════════════════════════════════════════════════════════
# Tolerances and bounds — see the module docstring for the margin argument
# ═══════════════════════════════════════════════════════════════════════════

#: Relative tolerance on the unrounded rate. Absorbs the last bit of a division;
#: at least 40 000x smaller than the smallest gap a three-status denominator
#: opens on any input this file generates — see the module docstring for the
#: ratio, which is independent of the numerator.
_RELATIVE_TOLERANCE = 1e-9

#: Absolute tolerance on the rate as published (rounded to 1dp at `:617`). The
#: rounding itself is asserted exactly; this only absorbs the division.
_PUBLISHED_TOLERANCE = 1e-9

#: Per-status ceiling for the unrounded clauses. Bounds the total, which is what
#: bounds the tolerance-to-gap ratio.
_COUNT_MAX = 5000

#: `started` is generated INDEPENDENTLY of the terminal counts, including values
#: far above and below their sum. That is not an artificial input: an execution
#: that began before the window can end inside it and vice versa, so the two
#: series genuinely do not have to reconcile. Independence is also what makes
#: the perturbation clause a real test of `R5.2` rather than of a bookkeeping
#: identity.
_STARTED_MAX = 100_000

# The window. One day, so the previous window is the day before it.
END = datetime(2025, 4, 2, 12, 0, tzinfo=UTC)
START = END - timedelta(days=1)
CW_PERIOD = 300
PERIOD_LABEL = "24h"

EMPTY_REPLY: dict[str, Any] = {"MetricDataResults": []}


# ═══════════════════════════════════════════════════════════════════════════
# A CloudWatch stub that answers by metric name, not by query id
# ═══════════════════════════════════════════════════════════════════════════
# Keyed on the metric names in `_TERMINAL_EXECUTION_METRICS` and on the SEARCH /
# reduction protocol, never on the id spellings (`succeeded_sum`, ...). So a
# terminal outcome added to the mapping is answered here with no change, and a
# rename of the ids leaves this file working.

_SEARCH_RE = re.compile(r"SEARCH\(\s*'(?P<series>(?:[^']|'')*)'\s*,\s*'(?P<stat>[^']*)'")
_METRIC_NAME_RE = re.compile(r'MetricName="(?P<name>[^"]+)"')


def _searched_series(queries: list[dict[str, Any]]) -> dict[str, str]:
    """``{query id: the series text of its SEARCH}`` for every raw SEARCH query."""
    series: dict[str, str] = {}
    for query in queries:
        match = _SEARCH_RE.search(query.get("Expression", "") or "")
        if match:
            series[query["Id"]] = match.group("series")
    return series


def _metric_name_by_query_id(queries: list[dict[str, Any]]) -> dict[str, str]:
    """``{query id: the MetricName its SEARCH filters on}``."""
    names: dict[str, str] = {}
    for query_id, series in _searched_series(queries).items():
        match = _METRIC_NAME_RE.search(series)
        if match:
            names[query_id] = match.group("name")
    return names


def _is_states_read(queries: list[dict[str, Any]]) -> bool:
    """True when ``queries`` search the AWS/States namespace.

    Located by namespace rather than by query id or call order, so this file
    does not depend on how many CloudWatch reads the payload builder makes or in
    what sequence.
    """
    return any(STATES_NAMESPACE in series for series in _searched_series(queries).values())


def _count(counts: dict[str, int | None], key: str) -> int:
    """The terminal count for ``key``. ``None`` means "no datapoints at all".

    Both spellings are an established zero and the arithmetic treats them the
    same; they are generated separately because CloudWatch reports them
    differently and a read that confused "no series" with a failed read would
    be the collapse `LGR-b2f79ef-8` rejected.
    """
    value = counts.get(key)
    return 0 if value is None else value


def _states_reply(
    queries: list[dict[str, Any]],
    *,
    counts: dict[str, int | None],
    started: int | None,
) -> dict[str, Any]:
    """A ``get_metric_data`` reply for the States read.

    Each returned (non-``ReturnData: False``) reduction query is matched to the
    raw ``SEARCH`` its expression references, and the ``MetricName`` in that
    SEARCH decides what it gets. A metric this stub was told nothing about — for
    instance ``ExecutionTime`` — comes back with no datapoints.
    """
    name_by_id = _metric_name_by_query_id(queries)
    value_by_metric: dict[str, int | None] = {TERMINAL_METRICS[key]: counts.get(key) for key in DENOMINATOR_KEYS}
    value_by_metric[STARTED_METRIC_NAME] = started

    results: list[dict[str, Any]] = []
    for query in queries:
        if not query.get("ReturnData", True):
            continue
        expression = query.get("Expression", "") or ""
        referenced = [
            query_id for query_id in name_by_id if re.search(rf"\b{re.escape(query_id)}\b", expression) is not None
        ]
        metric_name = name_by_id[referenced[0]] if len(referenced) == 1 else None
        value = value_by_metric.get(metric_name) if metric_name is not None else None
        values = [] if value is None else [value]
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


def read_sfn_metrics(counts: dict[str, int | None], started: int | None = 0) -> dict[str, Any]:
    """``_get_sfn_metrics`` over a stubbed States read reporting ``counts``.

    Goes through the real read rather than handing ``_success_rate`` a literal
    dict, so both halves of `R5.1` are under test at once: the queries the read
    issues and the denominator the calculator builds from them come from the
    same named mapping, and a mapping entry that reached only one of the two
    would show up here.
    """
    mock_cw = MagicMock()
    mock_cw.get_metric_data.side_effect = lambda **kwargs: _states_reply(
        kwargs["MetricDataQueries"], counts=counts, started=started
    )
    with patch.object(metrics_mod, "cloudwatch_client", mock_cw):
        return metrics_mod._get_sfn_metrics(START, END, CW_PERIOD)


def build_payload(counts: dict[str, int | None], started: int | None = 0) -> tuple[dict[str, Any], set[str]]:
    """The whole payload, with only the CURRENT window's States read answered.

    Returns the payload and the set of metric names the current States read
    asked for. Every other CloudWatch read — including the previous window's —
    gets an empty reply, so every other tile is Not_Established, which is
    `R4.12`'s own case and keeps these assertions independent of every other
    source.
    """
    requested: set[str] = set()

    def _reply(**kwargs: Any) -> dict[str, Any]:
        queries = kwargs.get("MetricDataQueries", [])
        if not _is_states_read(queries) or kwargs.get("StartTime") != START:
            return EMPTY_REPLY
        requested.update(_metric_name_by_query_id(queries).values())
        return _states_reply(queries, counts=counts, started=started)

    mock_cw = MagicMock()
    mock_cw.get_metric_data.side_effect = _reply
    mock_state = MagicMock()
    mock_state.query.return_value = {"Items": []}
    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "state_table", mock_state),
    ):
        payload = metrics_mod._build_comprehensive_metrics(START, END, CW_PERIOD, PERIOD_LABEL)
    return payload, requested


# ═══════════════════════════════════════════════════════════════════════════
# The oracle — R5.3's sentence, evaluated on the generated counts
# ═══════════════════════════════════════════════════════════════════════════


def expected_rate(counts: dict[str, int | None]) -> float | None:
    """``succeeded ÷ every terminal outcome × 100``, or ``None`` for an empty window.

    `R5.3` and `R4.6`, written out once. The denominator iterates
    :data:`DENOMINATOR_KEYS`, so this oracle covers a terminal outcome added to
    the named mapping without being touched. ``_success_rate`` is never called
    to produce an expectation here (`LGR-28b4d75-5`, `R9.4`).
    """
    total = sum(_count(counts, key) for key in DENOMINATOR_KEYS)
    if total <= 0:
        return None
    return _count(counts, NUMERATOR_KEY) / total * 100


def rate_over_a_subset(counts: dict[str, int | None], *, dropped: str) -> float | None:
    """The rate a denominator that omitted ``dropped`` would report.

    Used only to show that the tolerances below cannot hide a wrong denominator
    — never as an expectation. ``None`` is what a denominator of zero answers,
    which is trivially distinguishable from a number.
    """
    total = sum(_count(counts, key) for key in DENOMINATOR_KEYS if key != dropped)
    if total <= 0:
        return None
    return _count(counts, NUMERATOR_KEY) / total * 100


# ═══════════════════════════════════════════════════════════════════════════
# Generators — over the mapping's keys, so a fifth outcome is generated too
# ═══════════════════════════════════════════════════════════════════════════


@st.composite
def _counts_with_a_positive_total(draw: Any) -> dict[str, int | None]:
    """Terminal counts whose sum is positive.

    Two branches. The first draws **every** outcome strictly positive: that is
    the case the module docstring's margin argument needs, because a numerator
    or a fourth status of zero makes a three-status denominator arithmetically
    indistinguishable from a four-status one. The second is the general shape —
    any mix of absent statuses (``None``, no datapoints), measured zeros, and
    counts — with one drawn status forced positive so the total cannot be zero.
    """
    if draw(st.booleans()):
        return {key: draw(st.integers(min_value=1, max_value=_COUNT_MAX)) for key in DENOMINATOR_KEYS}
    counts: dict[str, int | None] = {
        key: draw(st.one_of(st.none(), st.integers(min_value=0, max_value=_COUNT_MAX))) for key in DENOMINATOR_KEYS
    }
    forced = draw(st.sampled_from(DENOMINATOR_KEYS))
    counts[forced] = max(_count(counts, forced), 1)
    return counts


@st.composite
def _counts_with_a_zero_total(draw: Any) -> dict[str, int | None]:
    """Terminal counts that sum to zero — `R4.6`'s window.

    Each status is either absent from the response or present with a datapoint
    of zero, in every combination. Both are an established nothing: the read
    succeeded, so `R4.6`'s answer is Not_Established either way and never
    ``100.0``.
    """
    return {key: draw(st.sampled_from([None, 0])) for key in DENOMINATOR_KEYS}


#: Counts bounded so that dropping any single status changes the rate by more
#: than half of the last PUBLISHED digit (the payload rounds to 1dp). The
#: numerator's floor and the other statuses' ceiling are what guarantee it: with
#: a numerator of at least 5 and every other status at most 12, the worst case is
#: a numerator of 5 against a total of 41, where dropping a status of count 1
#: shifts the rate by 0.30 percentage points — six times the 0.05 rounding
#: grain. A fifth terminal outcome widens the total to 53 and the worst-case
#: shift is still 0.18, so the bound survives `R5.5`.
#: :func:`test_published_counts_reproduce_the_published_rate` asserts the margin
#: per example rather than trusting this comment.
_PUBLISHED_NUMERATOR_MIN, _PUBLISHED_NUMERATOR_MAX = 5, 60
_PUBLISHED_OTHER_MIN, _PUBLISHED_OTHER_MAX = 1, 12


@st.composite
def _counts_the_published_rate_discriminates(draw: Any) -> dict[str, int]:
    """Every terminal outcome positive, bounded for 1dp resolution."""
    return {
        key: draw(
            st.integers(min_value=_PUBLISHED_NUMERATOR_MIN, max_value=_PUBLISHED_NUMERATOR_MAX)
            if key == NUMERATOR_KEY
            else st.integers(min_value=_PUBLISHED_OTHER_MIN, max_value=_PUBLISHED_OTHER_MAX)
        )
        for key in DENOMINATOR_KEYS
    }


_STARTED_VALUES = st.integers(min_value=0, max_value=_STARTED_MAX)


@st.composite
def _two_different_started_counts(draw: Any) -> tuple[int, int]:
    """Two ``started`` counts that differ, by construction rather than by filter."""
    first = draw(_STARTED_VALUES)
    delta = draw(st.integers(min_value=1, max_value=_STARTED_MAX))
    return first, first + delta


# ═══════════════════════════════════════════════════════════════════════════
# Property 3, clause 1 — the rate is succeeded over every terminal outcome
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=200)
@given(_counts_with_a_positive_total(), _STARTED_VALUES)
def test_rate_is_succeeded_over_every_terminal_outcome(counts: dict[str, int | None], started: int) -> None:
    """`R5.1`, `R5.3`: the denominator is the sum of every terminal outcome.

    The expectation is :func:`expected_rate`, arithmetic on the generated
    counts. Before p39 the denominator was ``succeeded + failed``, so the two
    outcomes `R5.1` adds were dropped — 28 percent of everything that ran on the
    verified window — and the reported rate was higher than the truth. That is
    what the second assertion pins from the other side: for every status whose
    count is positive, a denominator that omitted it would report a *different*
    number, by a margin far outside the tolerance.
    """
    outcome = metrics_mod._success_rate(read_sfn_metrics(counts, started))
    expected = expected_rate(counts)
    assert expected is not None, "generator invariant: the terminal counts must sum to a positive number"

    assert outcome.value is not None, (
        f"a positive denominator is a measured rate, not an absence — counts {counts!r} sum to "
        f"{sum(_count(counts, key) for key in DENOMINATOR_KEYS)}"
    )
    assert outcome.value == pytest.approx(expected, rel=_RELATIVE_TOLERANCE), (
        f"R5.3: the rate is {NUMERATOR_KEY} ÷ the sum of {list(DENOMINATOR_KEYS)} × 100 = {expected!r} "
        f"for counts {counts!r}, got {outcome.value!r}"
    )
    assert outcome.determined_from == metrics_mod.SOURCE_SFN_EXECUTIONS, (
        f"R4.2: a measured rate carries the bare source token, got {outcome.determined_from!r}"
    )

    # The tolerance's teeth, asserted rather than assumed: for every status that
    # actually contributed, a three-status denominator gives a different answer
    # by a margin the tolerance cannot absorb.
    #
    # Skipped when the numerator is zero, and that exclusion is the reason one
    # generator branch draws EVERY status positive: 0 ÷ anything is 0.0, so a
    # window in which nothing succeeded reports the same rate over three
    # statuses as over four and carries no discriminating power at all. The
    # example is still worth generating — a 0% success rate must be reported as
    # 0.0 and not as an absence, which the assertions above cover — it just
    # cannot be the example the margin rests on.
    tolerance = abs(expected) * _RELATIVE_TOLERANCE
    if _count(counts, NUMERATOR_KEY) > 0:
        for dropped in DENOMINATOR_KEYS:
            if _count(counts, dropped) == 0:
                continue  # a status with no executions cannot change the denominator
            narrower = rate_over_a_subset(counts, dropped=dropped)
            if narrower is None:
                continue  # dropping it empties the denominator — plainly a different answer
            assert abs(narrower - expected) > tolerance, (
                f"this assertion's own margin: dropping {dropped!r} (count "
                f"{_count(counts, dropped)}) must move the rate by more than {tolerance!r}, "
                f"else the tolerance could hide a denominator missing a terminal outcome — "
                f"{expected!r} vs {narrower!r} for {counts!r}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Property 3, clause 2 — nothing terminal is Not_Established, never 100.0
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=200)
@given(_counts_with_a_zero_total(), _STARTED_VALUES)
def test_no_terminal_outcome_reports_not_established(counts: dict[str, int | None], started: int) -> None:
    """`R4.6`: a window in which nothing reached a terminal outcome is absent.

    Not ``100.0``, which is what it answered before p39 — a perfect record on no
    evidence, indistinguishable from a window in which everything succeeded.
    `LGR-4854b3d-1` (**template / ratified**) is the fence: a three-state value
    is never collapsed to one of its ends.

    ``started`` is generated freely, including large values: executions that
    began in the window and had not finished are exactly the case that used to
    read as flawless.
    """
    outcome = metrics_mod._success_rate(read_sfn_metrics(counts, started))

    assert outcome.value is None, (
        f"R4.6: no terminal outcome in the window is Not_Established, got {outcome.value!r} for {counts!r} "
        f"with {started} started"
    )
    assert outcome.value != PERFECT_RATE, "R4.6: an empty window must never report a perfect success rate"
    assert outcome.determined_from == f"{metrics_mod.SOURCE_SFN_EXECUTIONS}_absent", (
        f"R4.2: the States read succeeded and holds nothing, which is the `_absent` token — "
        f"got {outcome.determined_from!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Property 3, clause 3 — the published denominator is reconstructible
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=200)
@given(_counts_the_published_rate_discriminates(), _STARTED_VALUES)
def test_published_counts_reproduce_the_published_rate(counts: dict[str, int], started: int) -> None:
    """`R5.4`: recomputing from the published per-status counts reproduces the rate.

    This is what makes the headline moving from 36.4% to 33.3% checkable
    arithmetic on screen — ``12 / (12 + 14 + 10 + 0)`` — rather than a number
    that changed for unstated reasons. Three facts, because a defect can break
    any one alone: the published counts must cover **every** term of the
    denominator (a subset is not reconstructible), each must be the count that
    was read, and the rate must be the one they produce.

    The third assertion is this clause's own margin. The published rate is
    rounded to 1dp, a grain coarse enough to hide a small error, so rather than
    trusting a tolerance the test asserts per example that dropping any single
    status changes the **published** figure.
    """
    payload, _ = build_payload(counts, started)
    performance = payload["performance"]
    published_counts = performance["terminal_counts"]

    assert published_counts is not None, (
        "R5.4: a successful States read publishes the per-status counts the denominator was built from"
    )
    assert set(published_counts) == set(DENOMINATOR_KEYS), (
        f"R5.4: the published counts must cover every term of the denominator "
        f"({list(DENOMINATOR_KEYS)}), else the rate cannot be reconstructed from them — got "
        f"{sorted(published_counts)}"
    )
    assert published_counts == counts, (
        f"the published counts must be the counts that were read, else the reconstruction reproduces "
        f"a different window — expected {counts!r}, got {published_counts!r}"
    )

    reconstructed = _count(published_counts, NUMERATOR_KEY) / sum(published_counts.values()) * 100
    assert performance["overall_success_rate"] == pytest.approx(round(reconstructed, 1), abs=_PUBLISHED_TOLERANCE), (
        f"R5.4: the published rate must be reproducible from the published counts — "
        f"{published_counts!r} give {round(reconstructed, 1)!r}, payload says "
        f"{performance['overall_success_rate']!r}"
    )

    for dropped in DENOMINATOR_KEYS:
        narrower = rate_over_a_subset(counts, dropped=dropped)
        assert narrower is None or round(narrower, 1) != performance["overall_success_rate"], (
            f"this assertion's own margin: at 1dp, dropping {dropped!r} must change the published "
            f"figure, else a denominator missing a terminal outcome would round to the same number — "
            f"{counts!r} publishes {performance['overall_success_rate']!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Property 3, clause 4 — `started` is not a terminal outcome
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=200)
@given(_counts_the_published_rate_discriminates(), _two_different_started_counts())
def test_perturbing_started_alone_never_changes_the_rate(counts: dict[str, int], started_pair: tuple[int, int]) -> None:
    """`R5.2`: ``ExecutionsStarted`` is excluded from the denominator.

    Two payload builds over identical terminal counts and different ``started``
    counts. A started execution may still be running, so it is not an outcome at
    all; a denominator that included it would make the rate a function of how
    many runs happened to be in flight when the window closed.

    The read is asserted to have actually **asked** for ``ExecutionsStarted``,
    so the perturbation is one the handler saw. Without that, a handler that
    never queried the metric would pass this clause vacuously — and `started` is
    published in its own right, so dropping the query is a real possibility this
    clause should not bless.
    """
    first_started, second_started = started_pair
    assert STARTED_METRIC_NAME not in TERMINAL_METRICS.values(), (
        f"R5.2: {STARTED_METRIC_NAME} is not a terminal outcome and must not be a member of "
        f"_TERMINAL_EXECUTION_METRICS — got {TERMINAL_METRICS!r}"
    )

    first_payload, first_requested = build_payload(counts, first_started)
    second_payload, second_requested = build_payload(counts, second_started)

    assert STARTED_METRIC_NAME in first_requested & second_requested, (
        f"the States read must ask for {STARTED_METRIC_NAME} for this clause to mean anything — "
        f"asked for {sorted(first_requested)}"
    )

    first_rate = first_payload["performance"]["overall_success_rate"]
    second_rate = second_payload["performance"]["overall_success_rate"]

    assert first_rate is not None, "generator invariant: every terminal count is positive, so the rate is measured"
    assert first_rate == second_rate, (
        f"R5.2: {STARTED_METRIC_NAME} is not in the denominator, so moving it from {first_started} to "
        f"{second_started} over identical terminal counts {counts!r} must not move the rate — "
        f"{first_rate!r} became {second_rate!r}"
    )
    assert first_payload["performance"]["terminal_counts"] == second_payload["performance"]["terminal_counts"], (
        f"R5.4: the published counts are the terminal ones, so they must not move with {STARTED_METRIC_NAME} "
        f"either — {first_payload['performance']['terminal_counts']!r} became "
        f"{second_payload['performance']['terminal_counts']!r}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
