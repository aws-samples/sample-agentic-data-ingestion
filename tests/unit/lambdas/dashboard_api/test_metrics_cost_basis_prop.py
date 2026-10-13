# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test — the cost figure and its basis agree, and the windows are comparable.

Feature: p39-metrics-truth, task 21.5. Subjects: ``_get_estimated_monthly_cost``
(``handlers/metrics.py:1525``) and its two call sites in
``_build_comprehensive_metrics`` (``:574-575``).

# Feature: p39-metrics-truth, Property 6: The cost figure and its stated basis agree, and the two comparison windows are comparable

*For any* window pair and run set, the cost figure carries a ``basis`` naming
the metric, both window bounds, the run count, the DPU rate and the DPU
minimum; the ``basis`` window bounds equal the bounds **actually queried**; and
the current and previous windows are adjacent and disjoint, queried from the
same metric name.

**Validates: Requirements 2.3, 2.5, 2.6, 2.7**

Why the "actually queried" clause is the whole point
---------------------------------------------------

A ``basis`` that declared bounds of its own would reproduce the original defect
while looking correct. Before task 6.1, ``_get_estimated_monthly_cost`` took
only an ``end`` and anchored its own trailing 30 days from it, so the two calls
the payload builder makes — one for the current window, one for the previous —
asked for windows overlapping by **29 of 30 days** on a 24-hour period, and the
cost trend was arithmetic on two overlapping integrals of one series. Task 6.1
made the caller pass both bounds.

So every bound assertion below is compared against the ``StartTime`` /
``EndTime`` the function **sent to CloudWatch**, captured off the stubbed
``get_metric_data`` call, never against ``basis``'s own statement and never
against the arguments the test passed in. Those three agree today; comparing
against the query is what keeps them so.

Where the expectations come from
--------------------------------

**From ``R2.3``, ``R2.5``, ``R2.6`` and ``R2.7``, and from nothing this code
returned before p39.** `LGR-28b4d75-5` (**template / ratified**): a test written
from the producer's output cannot detect the producer drifting, because the
output is its own oracle. ``R9.4`` is the same rule for this spec. Adjacency and
disjointness are read off ``R2.3`` — "compute the cost figure for the current
window and for the previous window from the same source, so that the two windows
are comparable" — and not off observing which bounds the handler happens to ask
for.

The pre-p39 behaviour appears here only as the thing asserted *against*: the
overlap is what :func:`test_the_two_cost_windows_are_adjacent_disjoint_and_one_metric`
forbids, and a self-anchored window is what
:func:`test_the_basis_bounds_are_the_bounds_actually_queried` forbids.

``R2.7`` is why ``note`` is asserted at all. Nothing in this system measures
spend (requirements constraint 14); the 10-minute DPU minimum is why every
observed run prices at a constant ``0.147`` and why the pipeline page's
``$0.44 / $0.15`` reconciles as ``3 x 0.147`` exactly. ``R2.7`` requires the
basis to **state** that, so a ``basis`` of numbers with no warning attached is
``R2.6`` satisfied in form and ``R2.7`` broken in fact.

The required key set is derived, not transcribed
------------------------------------------------

:data:`_DECLARED_BASIS_KEYS` is read out of the module's own AST — the keys of
the ``basis`` dict literal inside ``_get_estimated_monthly_cost`` — and
:data:`_ASSERTED_BASIS_KEYS` is the set this property actually judges. The two
are asserted equal, so **a key added to the basis cannot be silently
unasserted**: it appears in the declared set, fails that equality, and the
property says which key to cover. The runtime key set is compared against the
declared set as well, which is what catches a key added conditionally rather
than in the literal.

The DPU **rate** is not pinned to a value. No requirement fixes ``0.44`` — it is
the producer's constant, reported here and applied nowhere
(``job_completion:128-135`` applies it) — so what ``R2.6`` asks for is that the
rate be *named*, and the assertion is that it is present, numeric and positive.
The **minimum** is pinned, because ``R2.7`` names ten minutes itself.

What this file deliberately does not do
---------------------------------------

* **No assertion that the two windows are equal in length or read at the same
  period.** Both are true and both are task 6.3's example coverage
  (``test_metrics_cost.py``); Property 6's own sentence names adjacency,
  disjointness and one metric name, and this file implements that sentence.
* **No fifth dimension name.** ``_COST_METRIC_SERIES`` is a brace-form SEARCH on
  exactly ``{PipelineId, JobName, WorkerType, Environment}`` — no ``service``
  dimension, deliberately (task 13.2), because a SEARCH matches only series
  carrying precisely the dimensions named and a fifth would empty the read. What
  is asserted is that **both windows search the identical series string**, so a
  dimension added to one query and not the other fails; no name is spelled here.
* **No duplication of task 20.1's seam assertion.**
  ``test_metrics_trend_seam.py:574`` pins the two windows for its one frozen
  period; this generalises the same clause across every period in
  ``PERIOD_MAP`` and across arbitrary window ends. Neither file is amended by
  the other.
* **No live AWS**, and no assertion about the cost *total*'s arithmetic beyond
  its establishment — the sum is not what Property 6 is about.

Mocking
-------

CloudWatch is outside the shared fixtures' scope (they cover DynamoDB / S3 /
SQS), so the module-level ``cloudwatch_client`` is patched with ``MagicMock`` —
the established pattern for these read paths (``test_metrics_cost.py``,
``test_metrics_stage_durations_char.py``, ``test_query_not_scan.py``). No
``mock_aws()`` is created and no fixture-covered service is stubbed by hand;
``state_table`` is patched because ``_get_rows_loaded`` and
``_get_stage_metrics`` Query its GSI on every payload build (P28 R17.1).
``AWS_REGION`` is deliberately **not** set — the unit layer pins it by
assignment in ``tests/unit/conftest.py`` (`LGR-3e45e65-1`, **template /
ratified**).

The stub replies in the protocol it finds rather than by query id: a returned
reduction series is matched to the raw ``SEARCH`` its ``Expression`` references,
and the ``'Sum'`` / ``'SampleCount'`` statistic in that ``SEARCH`` decides which
values it gets. So ``rc_total`` / ``rc_runs`` may be renamed or reordered and
this file still speaks to the function.

**Expected result: green on the first run.** Every subject change (tasks 6.1,
6.2) has landed. A red here is a real finding about the read, not a property to
adjust.

_Requirements: R2.3, R2.5, R2.6, R2.7_
"""

import ast
import math
import os
import pathlib
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

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

# handlers.common resolves nine table names through require_env at import time;
# the sanctioned development switch is AGENTIC_DATA_INGESTION_ENV=local (same
# bootstrap as test_query_not_scan.py and the task-1 characterization files).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

METRICS_PATH = pathlib.Path(os.path.abspath(metrics_mod.__file__))
METRICS_SOURCE = METRICS_PATH.read_text()


# ═══════════════════════════════════════════════════════════════════════════
# Vocabulary — the requirements' own words
# ═══════════════════════════════════════════════════════════════════════════

#: The metric ``R2.1`` moves the figure onto: the run-completion publisher's,
#: not the job-creation publisher's. Spelled literally because it is the
#: requirement's own subject; reading it out of the module would make the
#: assertion circular.
COST_METRIC_NAME = "GlueJob.RunCostEstimate"

#: ``R2.7``'s own number — "including the 10-minute DPU minimum applied at
#: job_completion:113-131".
DPU_MINIMUM_MINUTES = 10

#: The six things ``R2.6`` and ``R2.7`` require the basis to name, as the keys
#: the payload carries them under. The *roles* are the requirements'; the key
#: spellings are the payload's, and the coverage guard below is what keeps the
#: two sets from drifting apart.
KEY_METRIC = "metric"
KEY_WINDOW_START = "window_start"
KEY_WINDOW_END = "window_end"
KEY_RUNS = "runs"
KEY_RATE = "rate_usd_per_dpu_hour"
KEY_DPU_MINIMUM = "dpu_minimum_minutes"
KEY_NOTE = "note"

#: Every key this property judges. Compared against the module's own declared
#: set below, so a new key is a red rather than an omission.
_ASSERTED_BASIS_KEYS = frozenset(
    {KEY_METRIC, KEY_WINDOW_START, KEY_WINDOW_END, KEY_RUNS, KEY_RATE, KEY_DPU_MINIMUM, KEY_NOTE}
)

#: The words ``R2.7`` requires the note to carry: that the figure is an
#: **estimate**, that it derives from **DPU**-minutes, that a per-run minimum of
#: **10** minutes is in it, and that it is **not measured** spend.
_NOTE_REQUIRED_SUBSTRINGS = ("estimate", "dpu", str(DPU_MINIMUM_MINUTES), "not measured")

_COST_READ_NAME = "_get_estimated_monthly_cost"
_BASIS_LOCAL = "basis"


def _declared_basis_keys() -> frozenset[str]:
    """The keys of the ``basis`` dict literal, read off the module's own AST.

    Derived rather than transcribed so that a key added to the basis shows up in
    :data:`_DECLARED_BASIS_KEYS`, fails the coverage guard, and has to be
    covered by this property before it can ship.

    A non-constant key (a computed name, or a ``**spread``) is refused rather
    than skipped: the guard's whole value is that the declared set is complete,
    and a key it could not read is a key nothing here asserts.
    """
    tree = ast.parse(METRICS_SOURCE, filename=str(METRICS_PATH))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == _COST_READ_NAME):
            continue
        for stmt in ast.walk(node):
            annotated = (
                isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == _BASIS_LOCAL
            )
            plain = isinstance(stmt, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == _BASIS_LOCAL for target in stmt.targets
            )
            if not (annotated or plain):
                continue
            value: ast.expr | None = stmt.value
            if isinstance(value, ast.Dict):
                keys: list[str] = []
                for key in value.keys:
                    if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                        raise AssertionError(
                            f"{_COST_READ_NAME}'s `{_BASIS_LOCAL}` literal has a key this guard cannot read "
                            f"({ast.dump(key) if key is not None else '**spread'}). R2.6 asks the basis to NAME "
                            f"what was summed; a key derived at run time cannot be asserted here, so spell it "
                            f"literally or extend this property."
                        )
                    keys.append(key.value)
                return frozenset(keys)
    raise AssertionError(
        f"no `{_BASIS_LOCAL}` dict literal found in {_COST_READ_NAME} — this property's key-set guard is "
        f"reading the wrong function, or the basis is now assembled somewhere R2.6 cannot be checked from"
    )


#: The basis key set the module declares. See :func:`_declared_basis_keys`.
_DECLARED_BASIS_KEYS = _declared_basis_keys()


# ═══════════════════════════════════════════════════════════════════════════
# A CloudWatch stub that speaks the module's protocol rather than its ids
# ═══════════════════════════════════════════════════════════════════════════

_SEARCH_RE = re.compile(r"SEARCH\(\s*'(?P<series>(?:[^']|'')*)'\s*,\s*'(?P<stat>[^']*)'")

EMPTY_REPLY: dict[str, Any] = {"MetricDataResults": []}


def _search_terms(queries: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Every ``SEARCH`` in ``queries`` as ``(query id, series, statistic)``."""
    terms: list[tuple[str, str, str]] = []
    for query in queries:
        match = _SEARCH_RE.search(query.get("Expression", "") or "")
        if match:
            terms.append((query["Id"], match.group("series"), match.group("stat")))
    return terms


def _searched_series(queries: list[dict[str, Any]]) -> frozenset[str]:
    """The set of series expressions ``queries`` searches."""
    return frozenset(series for _, series, _ in _search_terms(queries))


def _is_cost_read(queries: list[dict[str, Any]]) -> bool:
    """True when ``queries`` search the run-cost metric.

    Located by metric NAME rather than by query id, so a rename of ``rc_raw`` /
    ``rc_total`` leaves this file working.
    """
    return any(COST_METRIC_NAME in series for series in _searched_series(queries))


def _cost_reply(
    queries: list[dict[str, Any]],
    *,
    cost_values: list[float],
    run_values: list[float],
) -> dict[str, Any]:
    """A ``get_metric_data`` reply for a cost read, keyed on the module's own ids.

    Each returned query is matched to the raw ``SEARCH`` its ``Expression``
    references, and the statistic in that ``SEARCH`` decides what it gets: the
    ``'Sum'`` series carries the cost values, the ``'SampleCount'`` series the
    run counts. Nothing depends on an id spelling or on query order.
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
                "Timestamps": [datetime(2025, 4, 2, 12, 0, tzinfo=UTC) for _ in values],
                "Values": list(values),
                "StatusCode": "Complete",
            }
        )
    return {"MetricDataResults": results}


class CostRead(NamedTuple):
    """One call to the cost read, and what it asked CloudWatch for."""

    outcome: Any
    calls: list[dict[str, Any]]

    @property
    def call(self) -> dict[str, Any]:
        assert len(self.calls) == 1, f"expected exactly one get_metric_data call, got {len(self.calls)}"
        return self.calls[0]

    @property
    def queried_start(self) -> datetime:
        """The lower bound that reached CloudWatch — not the one passed in."""
        return self.call["StartTime"]

    @property
    def queried_end(self) -> datetime:
        """The upper bound that reached CloudWatch — not the one passed in."""
        return self.call["EndTime"]

    @property
    def queried_series(self) -> frozenset[str]:
        return _searched_series(self.call["MetricDataQueries"])


def run_cost(
    window: "Window",
    *,
    cost_values: list[float],
    run_values: list[float],
    side_effect: Exception | None = None,
) -> CostRead:
    """Call ``_get_estimated_monthly_cost`` over ``window``, capturing the query."""
    mock_cw = MagicMock()
    if side_effect is not None:
        mock_cw.get_metric_data.side_effect = side_effect
    else:
        mock_cw.get_metric_data.side_effect = lambda **kwargs: _cost_reply(
            kwargs["MetricDataQueries"], cost_values=cost_values, run_values=run_values
        )
    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "logger", MagicMock()),
    ):
        outcome = metrics_mod._get_estimated_monthly_cost(window.start, window.end, window.cw_period)
    return CostRead(outcome=outcome, calls=[call.kwargs for call in mock_cw.get_metric_data.call_args_list])


class PayloadRun(NamedTuple):
    """One ``_build_comprehensive_metrics`` pass and the cost reads inside it."""

    payload: dict[str, Any]
    cost_calls: list[dict[str, Any]]

    @property
    def windows(self) -> list[tuple[datetime, datetime]]:
        """The cost windows actually queried, earliest first."""
        return sorted((call["StartTime"], call["EndTime"]) for call in self.cost_calls)


def run_payload(window: "Window", *, cost_values: list[float], run_values: list[float]) -> PayloadRun:
    """Build the whole payload, answering only the cost reads with data.

    Every other ``get_metric_data`` call gets an empty reply, so every other tile
    is Not_Established — ``R4.12``'s own case — and no other series can influence
    the window assertions. Both cost windows get the same values: which window
    holds which figure is Property 2's business, not this one's.
    """
    cost_calls: list[dict[str, Any]] = []

    def _reply(**kwargs: Any) -> dict[str, Any]:
        queries = kwargs.get("MetricDataQueries", [])
        if not _is_cost_read(queries):
            return EMPTY_REPLY
        cost_calls.append(kwargs)
        return _cost_reply(queries, cost_values=cost_values, run_values=run_values)

    mock_cw = MagicMock()
    mock_cw.get_metric_data.side_effect = _reply
    mock_state = MagicMock()
    mock_state.query.return_value = {"Items": []}
    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "state_table", mock_state),
    ):
        payload = metrics_mod._build_comprehensive_metrics(
            window.start, window.end, window.cw_period, window.period_label
        )
    return PayloadRun(payload=payload, cost_calls=cost_calls)


# ═══════════════════════════════════════════════════════════════════════════
# Generators — the windows this handler can actually be asked for
# ═══════════════════════════════════════════════════════════════════════════
#
# The window is not free-form: `get_comprehensive_metrics` accepts one of the
# five labels in `PERIOD_MAP` and derives `(start, end)` and the CloudWatch
# period from it (`metrics.py:449-452`). So the period LABEL is what is
# generated, and the length and period come from the module's own maps rather
# than from a second table here — a period added to `PERIOD_MAP` is generated
# without this file changing. The window END is generated freely across three
# years, because no bound assertion may depend on which instant was asked for.


class Window(NamedTuple):
    """One requested window: its label, its bounds and its CloudWatch period."""

    period_label: str
    start: datetime
    end: datetime
    cw_period: int


#: Every period the handler serves, read off its own map.
_PERIOD_LABELS = sorted(metrics_mod.PERIOD_MAP)


@st.composite
def _windows(draw) -> Window:
    """A window the handler can be asked for, ending at an arbitrary instant.

    Sub-minute and sub-second components are left in deliberately: CloudWatch
    rounds a requested bound down to its own granularity, and ``basis`` must
    still name the bound that was **sent**, so a bound carrying microseconds is
    the case where a re-derived or re-rounded annotation would diverge.
    """
    label = draw(st.sampled_from(_PERIOD_LABELS))
    end = draw(
        st.datetimes(
            min_value=datetime(2024, 1, 1, 0, 0),
            max_value=datetime(2026, 12, 31, 23, 59),
            timezones=st.just(UTC),
        )
    )
    return Window(
        period_label=label,
        start=end - metrics_mod.PERIOD_MAP[label],
        end=end,
        cw_period=metrics_mod.CW_PERIOD[label],
    )


#: Per-datapoint cost sums. One branch is the observed shape — the 10-minute DPU
#: minimum prices every real run at a constant 0.147, so a window's datapoints
#: are whole multiples of it (requirements constraint 14) — and the other is any
#: non-negative figure, because no requirement bounds the sum. Zero is included:
#: a *measured* 0.0 over datapoints that exist is a measurement, and the basis
#: must annotate it like any other.
_COST_DATAPOINTS = st.lists(
    st.one_of(
        st.integers(min_value=0, max_value=400).map(lambda runs: round(runs * 0.147, 3)),
        st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    ),
    min_size=1,
    max_size=4,
)

#: SampleCount datapoints — the publisher emits exactly one per terminal run, so
#: these are run counts. Whole numbers, because that is what a sample count is;
#: generated as ints so the expected `basis.runs` is exact integer arithmetic and
#: not a float sum this test would have to round the same way the code does.
_RUN_COUNTS = st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=4)


# ═══════════════════════════════════════════════════════════════════════════
# Property 6, clause 1 — the basis names what R2.6 and R2.7 require
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=200)
@given(_windows(), _COST_DATAPOINTS, _RUN_COUNTS)
def test_the_basis_names_the_metric_the_runs_and_the_pricing_constants(
    window: Window, cost_values: list[float], run_counts: list[int]
) -> None:
    """``R2.6`` / ``R2.7``: the basis names what was summed, and says what it is not.

    Six roles, one per assertion, plus the key-set guard that keeps a seventh
    from arriving unasserted:

    * the **metric** — and not merely a string, but the metric the read actually
      searched, so a basis naming a series nobody queried fails;
    * the **run count** the sum is over, which is the SampleCount reduction and
      not a re-query;
    * the **DPU rate**, present, numeric and positive — the value is the
      producer's constant and no requirement fixes it, so it is not pinned;
    * the **DPU minimum**, pinned at ten, because ``R2.7`` names ten itself;
    * the **note**, carrying ``R2.7``'s statement that the figure is a
      DPU-minutes estimate and not measured spend.

    The bounds are clause 2's, asserted separately against the query.
    """
    read = run_cost(window, cost_values=cost_values, run_values=[float(count) for count in run_counts])
    basis = read.outcome.basis

    assert read.outcome.value is not None, (
        f"datapoints exist for [{window.start}, {window.end}], so the figure is established — "
        f"got {read.outcome.value!r} with provenance {read.outcome.determined_from!r}"
    )
    assert basis is not None, "R2.6: an established cost figure must carry a basis"

    # The coverage guard. Both directions matter: a key added to the module is a
    # key nothing here judges, and a key removed leaves an assertion below
    # speaking to something that no longer exists.
    assert _DECLARED_BASIS_KEYS == _ASSERTED_BASIS_KEYS, (
        f"the basis declares {sorted(_DECLARED_BASIS_KEYS)} and Property 6 judges "
        f"{sorted(_ASSERTED_BASIS_KEYS)}. Unjudged: {sorted(_DECLARED_BASIS_KEYS - _ASSERTED_BASIS_KEYS)}; "
        f"judged but gone: {sorted(_ASSERTED_BASIS_KEYS - _DECLARED_BASIS_KEYS)}. R2.6 is a statement about "
        f"what the basis names, so a new key belongs in this property before it ships."
    )
    assert set(basis) == _DECLARED_BASIS_KEYS, (
        f"the basis returned {sorted(basis)} but the module declares {sorted(_DECLARED_BASIS_KEYS)} — a key "
        f"added or dropped at run time is a key R2.6's readers cannot rely on"
    )

    assert basis[KEY_METRIC] == COST_METRIC_NAME, (
        f"R2.1/R2.6: the basis must name the run-completion metric {COST_METRIC_NAME!r}, got {basis[KEY_METRIC]!r}"
    )
    assert any(basis[KEY_METRIC] in series for series in read.queried_series), (
        f"R2.6: the basis names {basis[KEY_METRIC]!r} but the read searched {sorted(read.queried_series)!r} — "
        f"a basis naming a series nobody queried tells a reader about a measurement that was not taken"
    )

    assert basis[KEY_RUNS] == sum(run_counts), (
        f"R2.6: basis.{KEY_RUNS} must be the number of runs the sum is over ({sum(run_counts)} samples), got "
        f"{basis[KEY_RUNS]!r}"
    )

    rate = basis[KEY_RATE]
    assert isinstance(rate, (int, float)) and not isinstance(rate, bool), (
        f"R2.6: basis.{KEY_RATE} must be a number a reader can multiply, got {rate!r}"
    )
    assert math.isfinite(rate) and rate > 0, f"R2.6: basis.{KEY_RATE} must be a positive DPU-hour rate, got {rate!r}"

    assert basis[KEY_DPU_MINIMUM] == DPU_MINIMUM_MINUTES, (
        f"R2.7: the basis must state the {DPU_MINIMUM_MINUTES}-minute per-run DPU minimum applied at "
        f"job_completion:113-131, got {basis[KEY_DPU_MINIMUM]!r}"
    )

    note = basis[KEY_NOTE]
    assert isinstance(note, str) and note.strip(), f"R2.7: the basis must carry a stated note, got {note!r}"
    lowered = note.lower()
    missing_words = [word for word in _NOTE_REQUIRED_SUBSTRINGS if word not in lowered]
    assert not missing_words, (
        f"R2.7: the note must state that the figure is an estimate derived from Glue DPU-minutes, including "
        f"the {DPU_MINIMUM_MINUTES}-minute minimum, and is not measured spend. Missing {missing_words!r} from "
        f"{note!r} — nothing in this system measures spend (constraint 14) and this is where a reader is told so"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Property 6, clause 2 — the bounds are the bounds actually queried
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=200)
@given(_windows(), _COST_DATAPOINTS, _RUN_COUNTS)
def test_the_basis_bounds_are_the_bounds_actually_queried(
    window: Window, cost_values: list[float], run_counts: list[int]
) -> None:
    """``R2.5`` / ``R2.6``: both bounds reach the query, and the basis names those.

    Compared against the ``StartTime`` / ``EndTime`` captured off the stubbed
    call, never against ``window`` — a ``basis`` that re-derived its bounds from
    anything other than the read it annotates would reproduce the pre-6.1
    overlap while reading as correct.

    The span assertion is the same clause from the other side: the queried
    window must be the requested one, which is what the old ``end``-only
    signature made structurally impossible.
    """
    read = run_cost(window, cost_values=cost_values, run_values=[float(count) for count in run_counts])
    basis = read.outcome.basis
    assert basis is not None, "R2.6: an established cost figure must carry a basis"

    assert metrics_mod._parse_iso(basis[KEY_WINDOW_START]) == read.queried_start, (
        f"R2.6: basis.{KEY_WINDOW_START} {basis[KEY_WINDOW_START]!r} must be the lower bound actually queried "
        f"({read.queried_start})"
    )
    assert metrics_mod._parse_iso(basis[KEY_WINDOW_END]) == read.queried_end, (
        f"R2.6: basis.{KEY_WINDOW_END} {basis[KEY_WINDOW_END]!r} must be the upper bound actually queried "
        f"({read.queried_end})"
    )
    assert read.queried_end - read.queried_start == metrics_mod.PERIOD_MAP[window.period_label], (
        f"R2.5: the queried window must be the caller's {window.period_label} window "
        f"({metrics_mod.PERIOD_MAP[window.period_label]}), got "
        f"{read.queried_end - read.queried_start} for [{read.queried_start}, {read.queried_end}]"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Property 6, clause 3 — the two windows are adjacent, disjoint, one metric
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=100)
@given(_windows(), _COST_DATAPOINTS, _RUN_COUNTS)
def test_the_two_cost_windows_are_adjacent_disjoint_and_one_metric(
    window: Window, cost_values: list[float], run_counts: list[int]
) -> None:
    """``R2.3``: current and previous are comparable, whatever the period.

    Read off the two ``get_metric_data`` calls the payload build issues, so the
    subject is the pair of windows **queried** and not a pair either window
    declares. Four facts, because a defect can break any one alone:

    * exactly two cost reads — a third would be a window anchored inside the
      read rather than passed to it;
    * the current window is the caller's, which is what makes the rest
      non-trivial: adjacency alone holds for two windows the function invented;
    * the previous window ends where the current begins (adjacent) and their
      interiors do not meet (disjoint) — the clause that failed by 29 of 30 days
      before task 6.1;
    * both windows search the identical series, so "from the same source" covers
      the dimension schema as well as the metric name. No dimension is named
      here: ``_COST_METRIC_SERIES`` matches only series carrying precisely the
      four the publisher emits, and a fifth would empty the read (task 13.2).
    """
    run = run_payload(window, cost_values=cost_values, run_values=[float(count) for count in run_counts])

    assert len(run.cost_calls) == 2, (
        f"R2.3 needs one cost read per window — the current one and the previous one — got "
        f"{len(run.cost_calls)} for the {window.period_label} window: {run.windows!r}"
    )

    (prev_start, prev_end), (cur_start, cur_end) = run.windows
    assert (cur_start, cur_end) == (window.start, window.end), (
        f"R2.3/R2.5: the current cost window must be the caller's [{window.start}, {window.end}], got "
        f"[{cur_start}, {cur_end}]"
    )
    assert prev_end == cur_start, (
        f"R2.3: the previous window must end where the current one begins — previous [{prev_start}, "
        f"{prev_end}], current [{cur_start}, {cur_end}]. A gap loses every run that fell in it"
    )
    overlap = min(prev_end, cur_end) - max(prev_start, cur_start)
    assert overlap <= timedelta(0), (
        f"R2.3: the two cost windows must be disjoint — previous [{prev_start}, {prev_end}] and current "
        f"[{cur_start}, {cur_end}] overlap by {overlap} on the {window.period_label} window, so the trend "
        f"would be arithmetic on two overlapping integrals of one series"
    )

    searched = [_searched_series(call["MetricDataQueries"]) for call in run.cost_calls]
    assert all(any(COST_METRIC_NAME in series for series in call_series) for call_series in searched), (
        f"R2.1/R2.3: both cost windows must read {COST_METRIC_NAME}, got {[sorted(s) for s in searched]!r}"
    )
    assert searched[0] == searched[1], (
        f"R2.3: the two windows must be read "
        f"from the SAME source — got {sorted(searched[0])!r} and {sorted(searched[1])!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Property 6, the agreement itself — a basis exactly when there is a figure
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=200)
@given(_windows(), st.sampled_from(("measured", "empty", "unreadable")), _COST_DATAPOINTS, _RUN_COUNTS)
def test_the_basis_is_present_exactly_when_the_figure_is(
    window: Window, kind: str, cost_values: list[float], run_counts: list[int]
) -> None:
    """ "The cost figure and its basis agree" — neither ever appears without the other.

    ``R2.6`` gives the basis to the figure it annotates, so an annotation beside
    a ``null`` describes a measurement that does not exist and a figure without
    one is unattributed. The three answers the read can give are all generated:
    datapoints (a figure and a basis), a successful query with none (``R2.4``:
    both absent), and a raising client (both absent).

    Provenance is asserted only as "the two agree" — which token belongs to
    which answer is ``R4.2``'s clause and task 6.3's example coverage.
    """
    values = cost_values if kind == "measured" else []
    read = run_cost(
        window,
        cost_values=values,
        run_values=[float(count) for count in run_counts],
        side_effect=RuntimeError("CloudWatch GetMetricData throttled") if kind == "unreadable" else None,
    )

    established = kind == "measured"
    assert (read.outcome.value is not None) is established, (
        f"a {kind} cost read over [{window.start}, {window.end}] must "
        f"{'establish a figure' if established else 'report Not_Established'}, got {read.outcome.value!r}"
    )
    assert (read.outcome.basis is None) == (read.outcome.value is None), (
        f"R2.6: the basis must be present exactly when the figure is — got value {read.outcome.value!r} with "
        f"basis {read.outcome.basis!r} on a {kind} read. There is no basis for a sum that was never taken, and "
        f"no unattributed figure"
    )
