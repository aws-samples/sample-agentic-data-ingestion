# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Producer half of the cross-layer trend seam — `D2`, task 20.1.

Feature: p39-metrics-truth. Subject: ``get_comprehensive_metrics``'s published
``performance.trends.cost``, and the two fixtures the consumer half renders.

WHY THIS IS HALF A TEST, AND NOT A WHOLE ONE
--------------------------------------------
``tests/contracts/api_responses/`` is pytest and cannot render React;
``dashboard-react/src/pages/__tests__/`` is vitest and cannot import the Python
handler. **There is no process in which both halves run** (design `D2`), so the
seam is bridged by a fixture this file GENERATES and the consumer half CONSUMES:

* task 20.1 (here) — build the payload from the handler and write it to disk;
* task 20.2 — render it through the real ``useMetrics`` hook;
* task 20.3 — a freshness guard, so the file on disk cannot drift from what the
  handler would produce today.

Pretending one process could do both produces a test that mocks one side away,
which is exactly the gap `R9` exists to close. `trend-polarity.test.tsx:68-79`
calls ``vi.mock('../../hooks', …)`` and feeds ``TrendIndicators`` values straight
into the component, so **no API is involved and a flipped backend direction is
unobservable from the frontend**. Before task 2.1 the handler reported ``"down"``
on a cost rise — the ``invert`` flag two call sites passed — and neither existing
test could see it. That defect is what this file's expectation is written to
catch.

WHERE THE EXPECTED DIRECTION COMES FROM
---------------------------------------
`R3.3`, verbatim: "WHEN the cost figure for the current window exceeds the cost
figure for the previous window by more than the stability threshold, THE
Metrics_Handler SHALL report the cost trend as ``up``." The fall is the mirror of
`R3.1` ("the direction of the raw change … applying no polarity transformation")
and is what `R9.3` asks the consumer half to render.

**Nothing here was obtained by running the handler and writing down what came
back.** `LGR-28b4d75-5` (**template / ratified**) is the fence, in its own words:
a contract test written from the producer's output cannot detect the producer
drifting, "because the output is its own oracle". `R9.4` is the same rule.

So for cost, ``up`` is the correct BACKEND answer to a rise even though a cost
rise is bad news — the backend reports direction and the FRONTEND decides
whether up is good, keyed on the field name and stated in text as well as in
colour (`LGR-99050e6-1`, **template / ratified**). The two halves of that
sentence live on opposite sides of the seam, which is why the seam needs a test.

THE FIGURES, AND WHY THEY ARE NOT NEAR ANY THRESHOLD
----------------------------------------------------
The rise is ``0.147 → 0.588`` (one terminal Glue run against four), a 300%
relative change; the fall is the same pair reversed. `R3.3` says "more than the
stability threshold" without fixing its value, so the figures are chosen to be
unambiguous for **any** threshold below 300% rather than tuned to the 5% the
handler currently applies. A test that sat just outside the threshold would be
asserting the constant, not the requirement.

``0.147`` is the live per-run figure, and it is a constant for a reason worth
restating: `job_completion:113-131` applies a 10-minute DPU minimum, so every
observed run prices identically (requirements constraint 14). **Nothing in this
system measures spend**, which is what ``monthly_cost_basis.note`` says in the
payload written below.

WHAT MAKES THE FIXTURES USABLE FROM ANOTHER PROCESS
---------------------------------------------------
* **Complete.** They are whole ``ComprehensiveMetrics`` payloads taken from the
  handler's own response body, so every key the frontend reads is present. Task
  20.2 renders them through the real hook with only the network layer stubbed,
  where a missing key surfaces as an ``undefined`` access rather than as a clear
  failure — so each written file is validated against
  ``COMPREHENSIVE_METRICS_SCHEMA`` **here**, in the process that produced it.
* **Deterministic.** ``get_comprehensive_metrics`` anchors its window on
  ``datetime.now(UTC)`` and publishes it as ``generated_at`` and as the basis
  bounds, so the clock is frozen (see ``_FrozenDateTime``). Without that the file
  would differ on every run and task 20.3's freshness guard could never pass.
* **Computable without writing.** :func:`build_seam_payload` returns the payload
  and touches no file; :func:`serialize_fixture` and :func:`write_fixture` are
  separate. Task 20.3's guard compares the built serialization against the one on
  disk and needs no restructuring here.

Every tile other than cost is Not_Established in these fixtures, and that is the
honest shape rather than a shortcut: the stub answers only the cost read, so the
cost assertions cannot depend on any other series, and `R4.10`/`R4.12` require a
200 payload for exactly such a window. The consumer half asserts a cost
direction and its colour; nothing else needs a value.

MOCKING
-------
CloudWatch is not one of the services the shared fixtures cover (DynamoDB / S3 /
SQS), so the module-level ``cloudwatch_client`` is replaced with a stub that
speaks the module's own ``MetricDataQueries`` protocol — the convention in
``tests/unit/lambdas/dashboard_api/test_metrics_stage_durations_char.py`` and
``test_metrics_cost.py``. No ``moto.mock_aws()`` is created and no
fixture-covered service is stubbed by hand. The response cache is stubbed to a
miss, and ``state_table`` is stubbed because ``_get_rows_loaded`` and
``_get_stage_metrics`` Query its GSI on every payload build (P28 R17.1).

Nothing here depends on a CloudWatch query **id**: the cost read is located by
the metric NAME it searches and its two series are told apart by the statistic
(``Sum`` / ``SampleCount``) in the ``SEARCH`` each reduction references. The
brace-form series ``_COST_METRIC_SERIES`` (``metrics.py:1451``) names exactly
``{PipelineId, JobName, WorkerType, Environment}`` — no ``service`` dimension,
deliberately (task 13.2; a fifth name empties the read) — and the stub matches
on the metric name inside it rather than re-spelling that dimension set.

**Validates: Requirements 9.1, 9.2, 9.3, 9.4**

_Requirements: R9.1, R9.2, R9.3, R9.4_
"""

import difflib
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import jsonschema
import pytest

from .schemas import COMPREHENSIVE_METRICS_SCHEMA

# ---------------------------------------------------------------------------
# Path and environment setup — the sibling contract test's bootstrap
# (test_metrics_response_contract.py:18-38). `handlers.common` resolves every
# table name through `require_env` at import time, and `boto3.client` needs a
# region before the module-level client is constructed.
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
for _path in (os.path.abspath(LAMBDA_DIR), os.path.abspath(SHARED_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
# Enumerated rather than left to `AGENTIC_DATA_INGESTION_ENV=local`'s defaults,
# and enumerated MORE fully than the sibling contract test: `common.py:42-43`
# resolves `AUDIT_TABLE` and `AGENT_TRACES_TABLE` too, and this file must import
# whether or not another module in the layer happened to be collected first.
os.environ.setdefault("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
os.environ.setdefault("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

sys.modules.pop("lambda_function", None)

from handlers import metrics as metrics_mod

# ═══════════════════════════════════════════════════════════════════════════
# Vocabulary
# ═══════════════════════════════════════════════════════════════════════════

#: The two directions the requirements fix, written out literally. Reading them
#: off the module would make the assertions circular — these are `R3.3`'s and
#: `R9.3`'s own words.
TREND_UP = "up"
TREND_DOWN = "down"

#: The metric `R2.1` moves the cost figure onto: the run-completion publisher's,
#: one datapoint per Glue run that reached a terminal state. Named literally for
#: the same reason.
COST_METRIC_NAME = "GlueJob.RunCostEstimate"

#: The window the fixtures describe. `24h` is the handler's default period and
#: the one the dashboard opens on. The instant is arbitrary but FIXED: it reaches
#: the payload as `generated_at` and as `monthly_cost_basis.window_start` /
#: `window_end`, so a live clock would make every write a diff and leave task
#: 20.3's freshness guard unable to pass.
PERIOD = "24h"
FROZEN_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=24)
CURRENT_START = FROZEN_NOW - WINDOW
PREVIOUS_START = CURRENT_START - WINDOW

#: One terminal Glue run at the live per-run figure, and four of them. The ratio
#: is 4:1 — a 300% change — so the direction follows from `R3.3` for any
#: stability threshold below 300%, rather than from the 5% the handler applies.
ONE_RUN_COST = [0.147]
ONE_RUN_SAMPLES = [1.0]
FOUR_RUN_COST = [0.147, 0.147, 0.147, 0.147]
FOUR_RUN_SAMPLES = [1.0, 1.0, 1.0, 1.0]

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "api_responses"

#: Keys the consumer half reads out of the payload. `determined_from`,
#: `missing`, `monthly_cost_basis`, `terminal_counts` and `measurement` are the
#: additive keys tasks 3.5 and 15.1 introduced; a fixture predating them would
#: render as `undefined` in the other process.
CONSUMED_PERFORMANCE_KEYS = frozenset(
    {
        "lines_ingested_24h",
        "processing_rate",
        "overall_success_rate",
        "failed_pipelines",
        "agent_latency_ms",
        "monthly_cost",
        "monthly_cost_basis",
        "terminal_counts",
        "measurement",
        "determined_from",
        "missing",
        "trends",
    }
)
CONSUMED_TREND_KEYS = frozenset({"ingestion", "success", "agent_latency", "cost"})


class SeamCase(NamedTuple):
    """One direction of the seam: what the two windows measured, and the answer.

    ``expected_trend`` is the requirement's answer, not the handler's. It is
    written here so the fixture name, the figures and the expectation are read
    from one place by both the assertions and the writer.
    """

    name: str
    fixture_name: str
    current: tuple[list[float], list[float]]
    previous: tuple[list[float], list[float]]
    expected_trend: str
    derivation: str

    @property
    def path(self) -> Path:
        return FIXTURE_DIR / self.fixture_name


RISE = SeamCase(
    name="rise",
    fixture_name="get_metrics.trend_up.json",
    current=(FOUR_RUN_COST, FOUR_RUN_SAMPLES),
    previous=(ONE_RUN_COST, ONE_RUN_SAMPLES),
    expected_trend=TREND_UP,
    derivation=(
        "R3.3: the current window's cost exceeds the previous window's by more than the stability "
        "threshold, so the handler reports `up`. The backend reports DIRECTION and applies no polarity "
        "transformation (R3.1, R3.5); that a cost rise is bad news is the frontend's to say "
        "(LGR-99050e6-1). Before task 2.1 this returned `down` — that was the defect (R9.4)."
    ),
)

FALL = SeamCase(
    name="fall",
    fixture_name="get_metrics.trend_down.json",
    current=(ONE_RUN_COST, ONE_RUN_SAMPLES),
    previous=(FOUR_RUN_COST, FOUR_RUN_SAMPLES),
    expected_trend=TREND_DOWN,
    derivation=(
        "R3.1: the trend is the direction of the raw change, so a fall reports `down`. R9.3 is what the "
        "consumer half then renders as a POSITIVE signal — the polarity, and only the polarity, is the "
        "frontend's."
    ),
)

CASES = (RISE, FALL)


# ═══════════════════════════════════════════════════════════════════════════
# A frozen clock, so the generated fixture is a function of the handler alone
# ═══════════════════════════════════════════════════════════════════════════


class _FrozenDateTime(datetime):
    """``datetime`` with ``now()`` pinned to :data:`FROZEN_NOW`.

    A subclass rather than a ``MagicMock``: ``handlers.metrics`` also calls
    ``datetime.fromisoformat`` (``_parse_iso``, ``metrics.py:1940``), and every
    ``isinstance`` check against ``datetime`` elsewhere must keep holding.
    """

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


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


def _is_cost_read(queries: list[dict[str, Any]]) -> bool:
    """True when ``queries`` search the run-cost metric.

    Located by metric NAME, so renaming `rc_raw` / `rc_total` — or reordering
    the queries — leaves this file working.
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
    ``SEARCH`` its ``Expression`` references, and the statistic in that ``SEARCH``
    decides what it gets: the ``Sum`` series carries the cost, the ``SampleCount``
    series the run count.
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
                "Timestamps": [FROZEN_NOW for _ in values],
                "Values": list(values),
                "StatusCode": "Complete",
            }
        )
    return {"MetricDataResults": results}


class SeamBuild(NamedTuple):
    """A built payload and the cost reads that produced it."""

    payload: dict[str, Any]
    status: int
    cost_calls: list[dict[str, Any]]

    @property
    def cost_trend(self) -> str:
        return str(self.payload["performance"]["trends"]["cost"])

    @property
    def cost_windows(self) -> list[tuple[datetime, datetime]]:
        return sorted((call["StartTime"], call["EndTime"]) for call in self.cost_calls)


def build_seam_payload(case: SeamCase) -> SeamBuild:
    """Invoke ``get_comprehensive_metrics`` for ``case`` and return its body.

    Writes nothing. Task 20.3's freshness guard needs the payload the test WOULD
    write, computed independently of whether it writes, which is why this is a
    function and not a side effect of a test.

    The two cost windows are told apart by the bounds they ask for, never by call
    order: the current window is the one whose ``StartTime`` is
    :data:`CURRENT_START`. Every other read gets an empty reply, so no other
    series can influence the cost assertions.
    """
    cost_calls: list[dict[str, Any]] = []

    def _reply(**kwargs: Any) -> dict[str, Any]:
        queries = kwargs.get("MetricDataQueries", [])
        if not _is_cost_read(queries):
            return EMPTY_REPLY
        cost_calls.append(kwargs)
        values = case.current if kwargs.get("StartTime") == CURRENT_START else case.previous
        return _cost_reply(queries, cost_values=values[0], run_values=values[1])

    mock_cw = MagicMock()
    mock_cw.get_metric_data.side_effect = _reply
    mock_state = MagicMock()
    mock_state.query.return_value = {"Items": []}
    mock_state.get_item.return_value = {}

    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "state_table", mock_state),
        patch.object(metrics_mod, "_cache_table", mock_state),
        patch.object(metrics_mod, "datetime", _FrozenDateTime),
        patch.object(metrics_mod, "_get_cached_metrics", lambda period: None),
        patch.object(metrics_mod, "_cache_metrics", lambda period, metrics: None),
    ):
        response = metrics_mod.get_comprehensive_metrics({"queryStringParameters": {"period": PERIOD}})

    return SeamBuild(
        payload=json.loads(response["body"]),
        status=response["statusCode"],
        cost_calls=cost_calls,
    )


def serialize_fixture(payload: dict[str, Any]) -> str:
    """The on-disk form: sorted keys, two-space indent, trailing newline.

    Sorted so the serialization is a function of the payload's content and not of
    the handler's insertion order — the property task 20.3's guard compares on.

    ``ensure_ascii=False`` because the ``measurement`` strings carry em dashes and
    a division sign: escaping them to ``\\uXXXX`` is valid JSON and parses
    identically, but it makes the generated file unreadable next to the
    hand-written ``get_metrics.json``, which spells them literally.
    """
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


#: Every fixture write this process has performed, in order. Exists so the
#: freshness guard can ASSERT that its snapshot of the disk predates all of them
#: (see :data:`WRITES_BEFORE_SNAPSHOT`) rather than merely documenting it.
_WRITE_LOG: list[Path] = []


def write_fixture(case: SeamCase, payload: dict[str, Any]) -> Path:
    """Write ``payload`` to ``case.path`` and return the path."""
    case.path.parent.mkdir(parents=True, exist_ok=True)
    case.path.write_text(serialize_fixture(payload), encoding="utf-8")
    _WRITE_LOG.append(case.path)
    return case.path


@pytest.fixture(scope="module")
def written() -> dict[str, dict[str, Any]]:
    """Build and write both fixtures once, returning the payloads by case name.

    Module-scoped so no test depends on another having run first: every
    assertion below reads either this mapping or the file on disk.
    """
    payloads: dict[str, dict[str, Any]] = {}
    for case in CASES:
        build = build_seam_payload(case)
        write_fixture(case, build.payload)
        payloads[case.name] = build.payload
    return payloads


def _on_disk(case: SeamCase) -> dict[str, Any]:
    return dict(json.loads(case.path.read_text(encoding="utf-8")))


def _fixture_text(case: SeamCase) -> str | None:
    """The fixture's exact bytes as text, or ``None`` if it is not there.

    Absence is a different situation from drift and gets a different message, so
    it is a distinct return value rather than an exception to be caught later.
    """
    try:
        return case.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


#: The fixtures as they were when this module was IMPORTED — which is during
#: collection, strictly before any test or fixture in the module can run.
#:
#: This is the whole mechanism of task 20.3's guard. The module-scoped ``written``
#: fixture rewrites both files, so a guard that read the live file could pass
#: merely because the same session had just regenerated it — it would guard
#: nothing, and its passing would depend on test order. Reading at import removes
#: the ordering question instead of arranging it favourably:
#: :data:`WRITES_BEFORE_SNAPSHOT` is asserted to be zero, so the snapshot
#: provably predates every write this process makes.
DISK_AT_IMPORT: dict[str, str | None] = {case.name: _fixture_text(case) for case in CASES}

#: Zero by construction: nothing above this line writes. Asserted, not assumed.
WRITES_BEFORE_SNAPSHOT = len(_WRITE_LOG)

#: What a reader should run when the guard goes red. Regenerating is a side
#: effect of this file's own ``written`` fixture, so the command is just the file.
REGENERATE_COMMAND = (
    "cd agentic_data_ingestion && .venv/bin/pytest tests/contracts/api_responses/test_metrics_trend_seam.py"
)

#: The vitest file on the other side of the seam, named in the failure messages
#: because a missing or stale fixture is only legible as a failure over there
#: once you know which suite consumes it (task 20.2).
CONSUMER_HALF = "dashboard-react/src/pages/__tests__/MetricsDashboardPage.seam.test.tsx"

_MAX_DIFF_LINES = 60


def _fixture_diff(case: SeamCase, on_disk: str, from_handler: str) -> str:
    """A unified diff of disk against the handler, truncated to stay readable."""
    lines = list(
        difflib.unified_diff(
            on_disk.splitlines(keepends=True),
            from_handler.splitlines(keepends=True),
            fromfile=f"{case.fixture_name} (on disk)",
            tofile=f"{case.fixture_name} (what the handler produces now)",
            n=2,
        )
    )
    if len(lines) > _MAX_DIFF_LINES:
        omitted = len(lines) - _MAX_DIFF_LINES
        lines = [*lines[:_MAX_DIFF_LINES], f"... {omitted} further diff lines omitted\n"]
    return "".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 1. The direction the handler publishes (R3.3, R3.1)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheHandlerReportsTheRawDirection:
    """`R3.3` for the rise, `R3.1` for the fall. Neither derived from output."""

    def test_a_cost_rise_is_reported_as_up(self):
        build = build_seam_payload(RISE)

        assert build.status == 200, "R4.10: every requested window returns a payload"
        assert build.cost_trend == TREND_UP, (
            f"expected trends.cost == {TREND_UP!r}, got {build.cost_trend!r}. {RISE.derivation}"
        )

    def test_a_cost_fall_is_reported_as_down(self):
        build = build_seam_payload(FALL)

        assert build.status == 200
        assert build.cost_trend == TREND_DOWN, (
            f"expected trends.cost == {TREND_DOWN!r}, got {build.cost_trend!r}. {FALL.derivation}"
        )

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_the_figure_the_trend_is_about_is_published_and_measured(self, case):
        """A direction with no figure beside it is not a trend a reader can check.

        `R2.4`'s boundary in the other direction: these windows HAVE datapoints,
        so the cost is a measurement carrying a basis, and neither it nor the
        basis appears in ``missing``.
        """
        build = build_seam_payload(case)
        performance = build.payload["performance"]

        assert performance["monthly_cost"] is not None, (
            "both seam windows measure a terminal Glue run, so the figure is established (R2.4)"
        )
        assert performance["monthly_cost_basis"] is not None, "a measured figure carries its basis (R2.6)"
        assert "monthly_cost" not in performance["missing"]
        assert performance["determined_from"]["monthly_cost"] == metrics_mod.SOURCE_GLUE_RUN_COST, (
            "a measured cost carries the source token itself, not an `_absent` or `_incomplete` one (R4.2)"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. The stub actually fed the read it claims to (non-vacuity)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheTwoWindowsWereFedAsIntended:
    """Without this, a stub that answered nothing would leave the direction
    ``not_established`` and every assertion above trivially wrong for a reason
    nobody could see."""

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_the_cost_figure_is_read_once_per_window(self, case):
        build = build_seam_payload(case)

        assert len(build.cost_calls) == 2, (
            f"the seam needs a current and a previous cost window; got {len(build.cost_calls)}: {build.cost_windows!r}"
        )
        assert build.cost_windows == [(PREVIOUS_START, CURRENT_START), (CURRENT_START, FROZEN_NOW)], (
            f"R2.3: the two windows must be the caller's, adjacent and disjoint; got {build.cost_windows!r}"
        )

    def test_the_rise_case_really_rises(self):
        """The premise of `R3.3`'s antecedent, asserted rather than assumed."""
        assert sum(RISE.current[0]) > sum(RISE.previous[0])
        ratio = sum(RISE.current[0]) / sum(RISE.previous[0])
        assert ratio == pytest.approx(4.0), (
            f"the rise is 4:1 so it clears any stability threshold below 300%; got {ratio}"
        )

    def test_the_fall_case_really_falls(self):
        assert sum(FALL.current[0]) < sum(FALL.previous[0])

    def test_the_two_cases_are_one_pair_of_figures_in_two_orders(self):
        """So the only difference between the fixtures is which window is larger."""
        assert RISE.current == FALL.previous
        assert RISE.previous == FALL.current


# ═══════════════════════════════════════════════════════════════════════════
# 3. The seam can see the defect it was written for
# ═══════════════════════════════════════════════════════════════════════════


def test_a_reinstated_backend_inversion_flips_the_generated_payload():
    """The control that makes this file worth having.

    Task 2.1 removed ``_compute_trend``'s ``invert`` parameter, which two call
    sites passed — so a cost rise was reported ``down`` and then re-inverted on
    display, wrong in both label and colour. Here that inversion is reinstated
    **in the test process only**, by wrapping the module's own
    ``_compute_trend``, and the generated payload flips with it.

    That is the property the whole seam rests on: an inversion re-entering the
    handler changes the fixture this file writes, so task 20.3's freshness guard
    goes red and task 20.2's render assertion goes red with it. A seam test that
    could not detect the defect it was written for would be worse than none.

    Nothing in ``handlers/metrics.py`` is modified — the patch is undone when the
    context manager exits.
    """
    honest = build_seam_payload(RISE)
    assert honest.cost_trend == TREND_UP

    raw_compute_trend = metrics_mod._compute_trend
    _inverted = {"up": "down", "down": "up"}

    def _inverting_compute_trend(current: float, previous: float | None) -> str:
        direction = raw_compute_trend(current, previous)
        return _inverted.get(direction, direction)

    with patch.object(metrics_mod, "_compute_trend", _inverting_compute_trend):
        flipped = build_seam_payload(RISE)

    assert flipped.cost_trend == TREND_DOWN, (
        "a re-introduced backend inversion must change the payload this file generates — otherwise the "
        "fixture, the freshness guard and the render assertion are all blind to it (R9.4, LGR-28b4d75-5)"
    )
    assert build_seam_payload(RISE).cost_trend == TREND_UP, "the patch must not outlive its context manager"


# ═══════════════════════════════════════════════════════════════════════════
# 4. The written fixtures (R9.1, R9.2, R9.3)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheGeneratedFixtures:
    """What crosses the process boundary, checked in the process that made it.

    Task 20.2 renders these through the real ``useMetrics`` hook with only the
    network layer stubbed, so an invalid or incomplete payload surfaces there as
    an ``undefined`` access rather than as a legible failure. Every check that
    can be made here is made here.
    """

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_the_fixture_is_written_where_the_consumer_half_looks(self, case, written):
        assert case.path.is_file(), f"the consumer half loads {case.path}"
        assert _on_disk(case) == written[case.name], (
            "the file on disk must be the payload this test built — the consumer half is only a test of "
            "the handler for as long as that holds (LGR-28b4d75-5)"
        )

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_the_fixture_validates_against_the_contract_schema(self, case, written):
        jsonschema.validate(_on_disk(case), COMPREHENSIVE_METRICS_SCHEMA)

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_the_fixture_carries_the_direction_its_name_claims(self, case, written):
        payload = _on_disk(case)

        assert payload["performance"]["trends"]["cost"] == case.expected_trend, case.derivation

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_the_fixture_carries_every_key_the_consumer_half_reads(self, case, written):
        payload = _on_disk(case)

        assert {"performance", "stages", "infrastructure", "period", "generated_at"} <= set(payload)
        performance = payload["performance"]
        assert set(performance) >= CONSUMED_PERFORMANCE_KEYS, (
            f"missing performance keys: {sorted(CONSUMED_PERFORMANCE_KEYS - set(performance))}"
        )
        assert set(performance["trends"]) == CONSUMED_TREND_KEYS
        assert set(payload["stages"]) == set(metrics_mod._CW_STAGE_NAMES), (
            "R6.2: the payload's stage key set is the frontend's six stage keys"
        )
        for stage, values in payload["stages"].items():
            assert {"success_count", "error_count", "avg_time_ms"} <= set(values), stage
        assert {"agentcore", "dynamodb", "s3", "lambda"} <= set(payload["infrastructure"])

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_the_fixture_is_a_deterministic_function_of_the_handler(self, case, written):
        """Two builds serialize identically, which is what task 20.3 compares.

        The clock is the only nondeterminism the payload has — ``generated_at``
        and the basis bounds — and it is frozen. If this goes red, the freshness
        guard cannot be written, so it is asserted here rather than discovered
        there.
        """
        first = serialize_fixture(build_seam_payload(case).payload)
        second = serialize_fixture(build_seam_payload(case).payload)

        assert first == second
        assert first == case.path.read_text(encoding="utf-8")

    def test_the_two_fixtures_differ_in_the_cost_direction_and_the_cost_figure_only(self, written):
        """Same window, same stubbed sources, opposite cost histories.

        So the consumer half's two assertions differ in exactly the fact `R9.2`
        and `R9.3` are about, and a difference anywhere else would mean the two
        renders were not comparable.
        """
        rise, fall = _on_disk(RISE), _on_disk(FALL)

        assert rise["generated_at"] == fall["generated_at"]
        assert rise["period"] == fall["period"] == PERIOD
        assert rise["stages"] == fall["stages"]
        assert rise["infrastructure"] == fall["infrastructure"]
        assert rise["performance"]["trends"]["cost"] == TREND_UP
        assert fall["performance"]["trends"]["cost"] == TREND_DOWN
        assert rise["performance"]["monthly_cost"] > fall["performance"]["monthly_cost"]
        differing_trends = {
            key
            for key in CONSUMED_TREND_KEYS
            if rise["performance"]["trends"][key] != fall["performance"]["trends"][key]
        }
        assert differing_trends == {"cost"}, (
            f"only the cost trend may differ between the two fixtures: {differing_trends}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. The freshness guard — task 20.3 (R9.1, R9.4)
# ═══════════════════════════════════════════════════════════════════════════


class TestTheFixtureOnDiskIsWhatTheHandlerProducesToday:
    """The load-bearing half of the seam: disk must equal the handler's output.

    Without this, the consumer half degrades into a test of a hand-written file.
    A re-introduced backend inversion — the defect
    ``test_a_reinstated_backend_inversion_flips_the_generated_payload`` above
    demonstrates is visible in the built payload — would flip the handler while
    the fixture kept the old value, and task 20.2's render assertion would keep
    passing against stale content. `LGR-28b4d75-5` (**template / ratified**) is
    the general form: a contract test whose expectation comes from the producer's
    own output cannot detect the producer drifting, "because the output is its own
    oracle". A generated fixture is exactly that output, so the generation has to
    be re-run and compared rather than trusted.

    **Why these tests take no ``written`` fixture.** ``written`` rewrites both
    files. A guard that compared against the live file after that would pass
    because the session had just regenerated it, and whether it caught anything
    would depend on test order. So the comparison is against
    :data:`DISK_AT_IMPORT`, read during collection, and
    ``test_the_snapshot_predates_every_write_this_process_makes`` asserts the
    ordering property instead of leaving it to convention.

    **The comparison is exact text, deliberately.** Not parsed dicts, and nothing
    excluded — not even ``generated_at``, which is why the clock is frozen
    (:data:`FROZEN_NOW`). Sorted keys make the serialization a function of content
    rather than of the handler's insertion order (:func:`serialize_fixture`), so
    the only thing exact comparison adds over a semantic one is that the file a
    human reads is the file the handler wrote. If exact comparison could not be
    made to pass, that would be a determinism finding about the handler, not a
    reason to loosen the assertion.
    """

    def test_the_snapshot_predates_every_write_this_process_makes(self):
        """The guard's independence from write ordering, as an assertion.

        Module import happens during collection, before any test or fixture body
        runs, so the count of writes performed when :data:`DISK_AT_IMPORT` was
        taken is zero. Stating it here means a future refactor that moved the
        snapshot below a write — or built it lazily from the live file — fails
        loudly instead of silently making the guard vacuous.
        """
        assert WRITES_BEFORE_SNAPSHOT == 0, (
            f"DISK_AT_IMPORT was captured after {WRITES_BEFORE_SNAPSHOT} fixture write(s), so it may be "
            "this session's own output rather than the file as committed. The freshness guard is vacuous "
            "in that state: move the snapshot back to module import time."
        )
        assert set(DISK_AT_IMPORT) == {case.name for case in CASES}, (
            "every seam case needs a pre-write snapshot, or its fixture is unguarded"
        )

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_the_fixture_exists_on_disk(self, case):
        """Absence and drift are different situations, so they fail differently.

        A missing fixture is not a handler change — it is a file the consumer half
        cannot load at all, which surfaces over there as a module-resolution error
        rather than as a failed expectation.
        """
        assert DISK_AT_IMPORT[case.name] is not None, (
            f"{case.path} is missing.\n"
            f"The consumer half ({CONSUMER_HALF}) imports this file to render; without it that suite "
            f"fails at load time with no statement about the trend at all.\n"
            f"Generate it:\n"
            f"    {REGENERATE_COMMAND}\n"
            f"then commit it — it is an input to a vitest process that cannot build it itself (design D2)."
        )

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_the_fixture_on_disk_is_byte_for_byte_what_the_handler_would_write(self, case):
        """The guard. Build the payload, serialize it, compare to the pre-write text."""
        on_disk = DISK_AT_IMPORT[case.name]
        if on_disk is None:
            pytest.skip(f"{case.fixture_name} is missing; test_the_fixture_exists_on_disk reports that")

        from_handler = serialize_fixture(build_seam_payload(case).payload)

        assert on_disk == from_handler, (
            f"{case.fixture_name} on disk is not what the handler produces today.\n\n"
            f"{_fixture_diff(case, on_disk, from_handler)}\n"
            f"Either the handler changed INTENTIONALLY — regenerate, then read the diff and confirm "
            f"every changed field is what the requirements now say:\n"
            f"    {REGENERATE_COMMAND}\n"
            f"    git diff -- tests/fixtures/api_responses/{case.fixture_name}\n"
            f"or it changed by ACCIDENT, and the diff above IS the finding. A flipped `trends.cost` "
            f"here is precisely the defect this seam exists to catch (R9.4, LGR-28b4d75-5): "
            f"{case.derivation}\n"
            f"Note: the run that reports this failure also rewrites the file, as a side effect of the "
            f"tests that consume the `written` fixture — so a second run goes green whether or not the "
            f"change was intended. Judge it from the git diff, never from the second run."
        )

    @pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
    def test_a_drifted_fixture_is_actually_detected(self, case):
        """Non-vacuity of the comparison, without touching the file on disk.

        The guard compares two strings; this asserts the comparison it performs
        rejects a fixture that differs by one character. A guard whose failure
        path was never exercised is a guard nobody has checked.
        """
        from_handler = serialize_fixture(build_seam_payload(case).payload)
        drifted = from_handler.replace(f'"cost": "{case.expected_trend}"', '"cost": "stable"')

        assert drifted != from_handler, (
            f"the drift this test injects must be a real change to the text; `trends.cost` is expected "
            f"to appear as {case.expected_trend!r} in the serialized fixture"
        )
        assert _fixture_diff(case, drifted, from_handler), "a drifted fixture must produce a readable diff"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
