# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Requirement-derived tests — absence is a value with a name.

Feature: p39-metrics-truth, task 3.6. Subjects: ``_compute_trend``,
``_success_rate``, ``_get_agent_error_rate`` and ``_get_rows_loaded`` in
``handlers/metrics.py``. (``_get_rows_loaded`` replaced ``_get_lines_ingested``
on 2026-10-10; section 4 explains the change.)

WHERE THE EXPECTATIONS COME FROM
--------------------------------
**Every expectation in this file is written from ``R4.4``-``R4.9`` and
``R5.1``-``R5.4``, and none of it from what these four functions returned before
p39.** That is the whole reason the plan schedules requirement-derived tests here
rather than characterization tests (`design.md` §6, `R9.4`, requirements
constraint 12). `LGR-28b4d75-5` (**template / ratified**) states the general
form: "a test written from the producer's output cannot detect the producer
drifting, because the output is its own oracle."

It mattered most for the records tile, **whose old output was the defect**.
Its first reader, ``_get_lines_ingested``, used to end
``return total if total > 0 else files``, so with no ``record_count_estimate``
in any row it returned the count of *files* under a tile labelled "Records
Ingested". A test written from that output would have asserted a file count as
the records figure and locked the substitution in. Its replacement,
``_get_rows_loaded``, is held to the same clause.
The three other functions carry the same shape of defect, each in its own
spelling — the pre-p39 outputs were `stable` for an absent baseline (``R4.4``),
`100.0` for a window in which nothing ran (``R4.6``), and `0.0` for a CloudWatch
query that raised (``R4.8``, which the caller then published as an availability
of 100% and a health of `healthy`). Not one of those is asserted here as a
baseline to preserve.

WHAT IS ASSERTED, CLAUSE BY CLAUSE
----------------------------------
=========  =====================================================================
``R4.4``   an absent baseline reports ``not_established``, never ``stable``
``R4.5``   a **measured** baseline of zero is a baseline: ``up`` for a positive
           current, ``stable`` for a current of zero — and never
           ``not_established``
``R4.6``   a window with zero terminal executions reports Not_Established, never
           ``100.0``
``R4.7``   asserted at the payload, in `test_metrics_handler.py`
``R4.8``   a failed agent error-rate query reports Not_Established; its
           availability / health consequence is asserted at the payload, in
           `test_metrics_handler.py`
``R4.9``   no usable ``rows_written`` on any ``load_metrics`` row reports
           Not_Established, and specifically not the number of rows
``R5.1``   ``ExecutionsAborted`` and ``ExecutionsTimedOut`` are in the
           denominator
``R5.2``   ``ExecutionsStarted`` is not
``R5.3``   the rate is succeeded ÷ the sum of all four terminal outcomes
``R5.4``   the denominator is reconstructible from the published per-status
           counts
=========  =====================================================================

The R4.2 provenance vocabulary is asserted by *shape* rather than by value: a
token must end in ``_absent`` or ``_incomplete`` — the two words ``R4.2``
spells — and must name its source. The source *names* come from the module's
own ``SOURCE_*`` constants, because no requirement fixes them; the suffixes are
written out literally here, because ``R4.2`` does fix those. The two are kept
apart per `LGR-b2f79ef-8` (**template / ratified**): "no record" and "record
with nothing in it" are different facts and must not collapse into one value.

WHAT THIS FILE DELIBERATELY DOES NOT DO
---------------------------------------
* **No Hypothesis.** Tasks 21.1-21.6 own the property tests, and Property 2's
  test (`test_metrics_trend_prop.py`, task 2.2) already states the general
  ``_compute_trend`` law over generated pairs. What is here is example-based
  coverage of the requirement *clauses* — the specific boundaries ``R4.4`` and
  ``R4.5`` name, plus the two arms of ``R4.6`` and ``R4.9`` that no property test
  exercises yet.
* **No assertion about S3 storage bytes.** ``_get_s3_storage_bytes`` is task
  4.1's, mid-change, and its provenance token is 4.1's to establish.
* **No assertion about the deleted arms' old values.** Not "it used to return the
  file count, now it does not" — only what the requirement says it must return.

MOCKING
-------
CloudWatch is outside the shared fixtures' scope (they cover DynamoDB / S3 /
SQS), so the module-level ``cloudwatch_client`` and ``state_table`` are patched
with ``MagicMock`` — the established pattern for these read paths
(`test_query_not_scan.py`, and the three task-1 characterization files). No
``mock_aws()`` is created here and no fixture-covered service is stubbed by
hand. ``_success_rate`` and ``_compute_trend`` are pure functions of their
arguments and need neither.

**Expected result: green on the first run.** Tasks 3.2-3.5 have all landed, so
the expected-red window this task's entry names is closed. A red here is a real
finding about the read behind the test, not a test to adjust.

_Requirements: R4.4, R4.5, R4.6, R4.7, R4.8, R4.9, R5.1, R5.2, R5.3, R5.4_
"""

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason test_query_not_scan.py and
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
# bootstrap as test_query_not_scan.py). AWS_REGION is deliberately NOT set here:
# the unit layer pins it by assignment in tests/unit/conftest.py
# (LGR-3e45e65-1, template / ratified), and handlers/common.py freezes the
# region into module-global boto3 resources at import.
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

# ═══════════════════════════════════════════════════════════════════════════
# The vocabulary the requirements are written in
# ═══════════════════════════════════════════════════════════════════════════

#: The four trend directions R4.4 and R4.5 name between them. Spelled out so a
#: typo is a NameError rather than a silently-passing string comparison.
UP = "up"
STABLE = "stable"
NOT_ESTABLISHED = "not_established"

#: R4.2's two suffixes, written out literally because the requirement fixes
#: these words: "<source>", "<source>_absent", "<source>_incomplete". The source
#: names themselves are taken from the module's SOURCE_* constants below,
#: because no requirement fixes those.
ABSENT = "_absent"
INCOMPLETE = "_incomplete"

START = datetime(2025, 4, 1, tzinfo=UTC)
END = datetime(2025, 4, 2, tzinfo=UTC)
PERIOD = 300


def assert_absent(outcome: Any, source: str) -> None:
    """The outcome is an established nothing from ``source`` (R4.2).

    Value ``None`` — never a zero, never the source's other end — and a token
    naming the source with R4.2's ``_absent`` suffix. Asserted together because
    either alone is satisfiable by a defect: a ``None`` with an unsuffixed token
    says a measurement was obtained, and an ``_absent`` token beside a number
    contradicts itself inside one record.
    """
    assert outcome.value is None, f"expected Not_Established from {source}, got value {outcome.value!r}"
    assert outcome.determined_from == f"{source}{ABSENT}", (
        f"an established nothing from {source} must carry '{source}{ABSENT}' (R4.2), got {outcome.determined_from!r}"
    )


def assert_incomplete(outcome: Any, source: str) -> None:
    """The outcome is an unreadable ``source`` — "we could not look" (R4.2).

    Kept apart from :func:`assert_absent` per `LGR-b2f79ef-8` (template /
    ratified): an operator acts differently on "there is nothing there" than on
    "the read failed", so the two must not share a token.
    """
    assert outcome.value is None, f"expected Not_Established from {source}, got value {outcome.value!r}"
    assert outcome.determined_from == f"{source}{INCOMPLETE}", (
        f"an unreadable {source} must carry '{source}{INCOMPLETE}' (R4.2), got {outcome.determined_from!r}"
    )


def assert_measured(outcome: Any, source: str) -> None:
    """The outcome is a real measurement from ``source``, zero included (R4.2).

    The token is the bare source name: no suffix, because nothing is missing.
    """
    assert outcome.value is not None, f"expected a measurement from {source}, got Not_Established"
    assert outcome.determined_from == source, (
        f"a measurement from {source} must carry the bare source token (R4.2), got {outcome.determined_from!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. `_compute_trend` — an absent baseline is not a stable one (R4.4, R4.5)
# ═══════════════════════════════════════════════════════════════════════════


class TestTrendAbsentBaseline:
    """R4.4 and R4.5: the two things a `previous` of "nothing" can mean.

    ``previous is None`` — the previous window established no baseline — and
    ``previous == 0.0`` — the previous window measured zero — are different
    facts. Before p39 both reached ``"stable"``, which is the collapse
    `LGR-4854b3d-1` (template / ratified) forbids: a three-state value is never
    collapsed to one of its ends, and the collapsing expression lives in exactly
    one place.

    `test_metrics_trend_prop.py` (task 2.2) states the same two clauses as a
    property over generated pairs. These are the named boundaries, asserted as
    examples, so a reader of the requirement can find the requirement's own case
    in a test.
    """

    def test_absent_baseline_reports_not_established(self):
        """R4.4: no baseline in the previous window reports ``not_established``."""
        assert metrics_mod._compute_trend(1_101_165, None) == NOT_ESTABLISHED

    def test_absent_baseline_is_never_reported_as_stable(self):
        """R4.4, stated negatively — and this is the half that was the defect.

        "There is nothing to compare against" and "the comparison came out
        level" are different facts. An operator who cannot tell them apart reads
        an unmeasured metric as a steady one.
        """
        assert metrics_mod._compute_trend(1_101_165, None) != STABLE

    def test_measured_zero_baseline_with_a_positive_current_reports_up(self):
        """R4.5: nothing last window, something this window, is a rise.

        The relative-change formula cannot produce this answer — its denominator
        is zero — so R4.5 states it as an absolute rule rather than leaving it
        to the arithmetic.
        """
        assert metrics_mod._compute_trend(42.0, 0.0) == UP

    def test_measured_zero_baseline_with_a_zero_current_reports_stable(self):
        """R4.5: nothing either window has not moved."""
        assert metrics_mod._compute_trend(0.0, 0.0) == STABLE

    def test_a_measured_zero_baseline_is_never_not_established(self):
        """R4.5: ``not_established`` is reserved for absence.

        A baseline that was measured has a value, and zero is a value.
        """
        assert metrics_mod._compute_trend(42.0, 0.0) != NOT_ESTABLISHED
        assert metrics_mod._compute_trend(0.0, 0.0) != NOT_ESTABLISHED


class TestReportedTrendAbsentCurrent:
    """R4.1 / R4.4's mirror image, at the guard all four trends route through.

    ``_reported_trend`` is the single null test in front of ``_compute_trend``
    (`metrics.py:1400`), and it answers the case R4.4 does not name: an absent
    **current** window. There is no change to take the direction *of*, so
    ``not_established`` is the only honest answer — R4.1's "report
    Not_Established for any metric for which no measurement was obtained",
    reached for the mirror-image reason.

    Asserted here rather than left to the payload because it is where the four
    trend entries' absent case is decided once instead of four times, and a
    fifth hand-written conditional at a call site would make this pass while
    breaking the property it exists to hold.
    """

    def test_absent_current_reports_not_established(self):
        """An unmeasured current window has no direction to report (R4.1, R4.4)."""
        assert metrics_mod._reported_trend(None, 950_000.0) == NOT_ESTABLISHED

    def test_both_windows_absent_reports_not_established(self):
        """Neither window measured is still Not_Established, never ``stable``."""
        answer = metrics_mod._reported_trend(None, None)
        assert answer == NOT_ESTABLISHED
        assert answer != STABLE

    def test_a_measured_current_over_a_measured_baseline_still_computes(self):
        """The guard is a guard, not a replacement: a measured pair still gets a
        direction. Without this, a guard that returned ``not_established``
        unconditionally would satisfy every clause above."""
        assert metrics_mod._reported_trend(200.0, 100.0) == UP


# ═══════════════════════════════════════════════════════════════════════════
# 2. `_success_rate` — every terminal outcome, or nothing (R4.6, R5.1-R5.4)
# ═══════════════════════════════════════════════════════════════════════════


def _sfn(**counts: Any) -> dict[str, Any]:
    """An `_get_sfn_metrics` result carrying the given counts.

    Defaults are a successful read of a window in which nothing ran, so each
    test below names only the counts its clause is about. ``started`` defaults
    to the sum of the terminal counts the caller passed, because a state machine
    cannot have finished more executions than it started — a fixture that
    violated that would make R5.2's clause vacuous.
    """
    metrics: dict[str, Any] = {"read_failed": False, "avg_duration_ms": 0}
    for key in metrics_mod._TERMINAL_EXECUTION_METRICS:
        metrics[key] = counts.pop(key, 0)
    metrics["started"] = counts.pop("started", sum(metrics[k] for k in metrics_mod._TERMINAL_EXECUTION_METRICS))
    metrics.update(counts)
    return metrics


class TestSuccessRateDenominator:
    """R5.1-R5.4: succeeded over every terminal outcome, and nothing else."""

    def test_rate_is_succeeded_over_all_four_terminal_outcomes(self):
        """R5.3: succeeded ÷ (succeeded + failed + aborted + timed_out) × 100.

        The counts are the verified 7-day window's — 12 / 14 / 10 / 0 — and the
        expectation is R5.3's arithmetic over them, computed here rather than
        copied from a screen. 12 / 36 is 33.33%, and the pre-p39 denominator of
        succeeded + failed gave 46.15% for the same window.
        """
        outcome = metrics_mod._success_rate(_sfn(succeeded=12, failed=14, aborted=10, timed_out=0))

        assert_measured(outcome, metrics_mod.SOURCE_SFN_EXECUTIONS)
        assert outcome.value == pytest.approx(12 / 36 * 100)

    def test_aborted_executions_are_in_the_denominator(self):
        """R5.1: an aborted run did not succeed, so it lowers the rate.

        8 succeeded and 2 aborted is 80%, not 100%. Under the pre-p39
        denominator the aborted pair vanished and the tile claimed a perfect
        record — 28 percent of the verified window's executions were aborted.
        """
        outcome = metrics_mod._success_rate(_sfn(succeeded=8, aborted=2))

        assert outcome.value == pytest.approx(80.0)

    def test_timed_out_executions_are_in_the_denominator(self):
        """R5.1: a timed-out run is unambiguously a pipeline failure.

        Asserted separately from ``aborted`` because a denominator that added
        only one of the two would pass a combined test.
        """
        outcome = metrics_mod._success_rate(_sfn(succeeded=8, timed_out=2))

        assert outcome.value == pytest.approx(80.0)

    def test_started_executions_are_excluded_from_the_denominator(self):
        """R5.2: a started execution may still be running, so it is not an outcome.

        Perturbing ``started`` alone must not move the rate. The two fixtures
        differ only in ``started`` — 36 versus 4 against the same four terminal
        counts.
        """
        few_started = metrics_mod._success_rate(_sfn(succeeded=1, failed=1, aborted=1, timed_out=1, started=4))
        many_started = metrics_mod._success_rate(_sfn(succeeded=1, failed=1, aborted=1, timed_out=1, started=36))

        assert few_started.value == pytest.approx(25.0)
        assert many_started.value == few_started.value

    def test_denominator_is_reconstructible_from_the_published_counts(self):
        """R5.4: a reader can recompute the rate from the per-status counts.

        The denominator is derived from `_TERMINAL_EXECUTION_METRICS` here, not
        from a hard-coded four, so this states the *relation* R5.4 asks for
        rather than a fixed arithmetic. The payload half — that those counts are
        published beside the rate — is asserted in `test_metrics_handler.py`.
        """
        counts = {"succeeded": 7, "failed": 3, "aborted": 2, "timed_out": 1}
        assert set(counts) == set(metrics_mod._TERMINAL_EXECUTION_METRICS), (
            "this test's counts must cover every terminal outcome the handler queries; "
            f"got {sorted(counts)} against {sorted(metrics_mod._TERMINAL_EXECUTION_METRICS)}"
        )

        outcome = metrics_mod._success_rate(_sfn(**counts))

        reconstructed = counts["succeeded"] / sum(counts[k] for k in metrics_mod._TERMINAL_EXECUTION_METRICS) * 100
        assert outcome.value == pytest.approx(reconstructed)


class TestSuccessRateAbsence:
    """R4.6: a window with no terminal executions has no rate to report."""

    def test_zero_terminal_executions_reports_not_established(self):
        """R4.6: Not_Established, never ``100.0``.

        Before p39 an empty window answered ``100.0``, so a window in which
        nothing ran was indistinguishable from one in which everything
        succeeded — a perfect record claimed on no evidence.
        """
        outcome = metrics_mod._success_rate(_sfn())

        assert_absent(outcome, metrics_mod.SOURCE_SFN_EXECUTIONS)
        assert outcome.value != 100.0

    def test_only_started_executions_still_reports_not_established(self):
        """R4.6 with R5.2: executions in flight are not terminal outcomes.

        Nothing has finished, so there is no rate — and ``started`` cannot
        stand in as a denominator.
        """
        outcome = metrics_mod._success_rate(_sfn(started=17))

        assert_absent(outcome, metrics_mod.SOURCE_SFN_EXECUTIONS)

    def test_a_failed_read_is_incomplete_not_absent(self):
        """R4.2: "we could not look" is not "we looked and there is nothing".

        `_get_sfn_metrics`'s ``except`` arm returns zeros for every count, which
        are arithmetically identical to an empty window. Without the
        ``read_failed`` flag a CloudWatch failure would be published as an
        established nothing — the collapse `LGR-b2f79ef-8` (template /
        ratified) rejected.
        """
        outcome = metrics_mod._success_rate(_sfn(read_failed=True))

        assert_incomplete(outcome, metrics_mod.SOURCE_SFN_EXECUTIONS)

    def test_a_measured_zero_rate_is_a_measurement_not_an_absence(self):
        """R4.6's other side: nothing succeeded is a rate of 0, not Not_Established.

        A window with terminal outcomes has a denominator, so the rate is
        established even when its value is zero. This is the assertion that
        stops "denominator is zero" being implemented as "rate is zero".
        """
        outcome = metrics_mod._success_rate(_sfn(failed=5, aborted=1))

        assert_measured(outcome, metrics_mod.SOURCE_SFN_EXECUTIONS)
        assert outcome.value == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════════════
# 3. `_get_agent_error_rate` — a failed query is not a healthy one (R4.8)
# ═══════════════════════════════════════════════════════════════════════════


def _cw_response(**ids: list[float]) -> dict[str, Any]:
    """A minimal GetMetricData response for the given (Id, Values) pairs."""
    return {"MetricDataResults": [{"Id": mid, "Values": vals, "Timestamps": []} for mid, vals in ids.items()]}


def _run_agent_error_rate(
    response: dict[str, Any] | None = None,
    *,
    side_effect: Exception | None = None,
) -> tuple[Any, MagicMock]:
    """Call `_get_agent_error_rate` against a stubbed CloudWatch client.

    Returns the outcome and the patched module ``logger``, so a caller can
    assert both the value and whether the failure was recorded.
    """
    mock_cw = MagicMock()
    if side_effect is not None:
        mock_cw.get_metric_data.side_effect = side_effect
    else:
        mock_cw.get_metric_data.return_value = response if response is not None else _cw_response()

    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "logger") as mock_logger,
    ):
        outcome = metrics_mod._get_agent_error_rate(START, END, PERIOD)
    return outcome, mock_logger


class TestAgentErrorRateAbsence:
    """R4.8: neither an empty window nor a failed query is a 0% error rate."""

    def test_a_raising_query_reports_not_established(self):
        """R4.8 verbatim: a failed query must not report a perfect system.

        Before p39 this returned ``0.0``, which the caller turned into an
        availability of ``100.0`` and a health of ``healthy`` — the strongest
        possible claim, made from the one event that proves nothing was
        observed. The availability and health consequence is asserted at the
        payload, in `test_metrics_handler.py`.
        """
        outcome, _ = _run_agent_error_rate(side_effect=RuntimeError("CloudWatch unavailable"))

        assert_incomplete(outcome, metrics_mod.SOURCE_AGENT_LAMBDA_ERRORS)
        assert outcome.value != 0.0

    def test_a_raising_query_is_recorded_at_warning(self):
        """An `_incomplete` token with no log is a silent failure.

        `design.md` §"Error Handling": each `_get_*` helper keeps its
        ``except Exception`` + ``logger.warning`` and returns rather than
        raising. Asserted so the token and the log cannot come apart.
        """
        _, mock_logger = _run_agent_error_rate(side_effect=RuntimeError("CloudWatch unavailable"))

        assert mock_logger.warning.called or mock_logger.error.called or mock_logger.exception.called, (
            "a failed agent error-rate query must be recorded at WARNING or above"
        )

    def test_zero_invocations_reports_not_established(self):
        """R4.8 with R4.1: no invocations means there is no rate.

        ``err / inv`` is undefined at ``inv == 0``. Reporting ``0.0`` asserts a
        flawless window in which nothing ran — identical in kind to
        `_success_rate`'s pre-p39 ``100.0``.
        """
        outcome, _ = _run_agent_error_rate(_cw_response(ai=[0.0], ae=[0.0]))

        assert_absent(outcome, metrics_mod.SOURCE_AGENT_LAMBDA_ERRORS)
        assert outcome.value != 0.0

    def test_an_empty_response_reports_not_established(self):
        """A response with no series at all is an empty window, not a rate of zero."""
        outcome, _ = _run_agent_error_rate(_cw_response())

        assert_absent(outcome, metrics_mod.SOURCE_AGENT_LAMBDA_ERRORS)

    def test_absence_and_unreadability_carry_different_tokens(self):
        """R4.2 / `LGR-b2f79ef-8`: the two absences are distinguishable.

        Both report Not_Established, and an operator acts differently on each —
        an idle window needs no action, a failed read means the tile is blind.
        """
        empty, _ = _run_agent_error_rate(_cw_response(ai=[0.0], ae=[0.0]))
        raising, _ = _run_agent_error_rate(side_effect=RuntimeError("boom"))

        assert empty.determined_from != raising.determined_from
        assert empty.determined_from.endswith(ABSENT)
        assert raising.determined_from.endswith(INCOMPLETE)


class TestAgentErrorRateMeasured:
    """The measured arm, so the absent arms above cannot be satisfied by a
    function that returns Not_Established unconditionally."""

    def test_invocations_and_errors_give_the_percentage(self):
        """errors ÷ invocations × 100, from a window that has both."""
        outcome, _ = _run_agent_error_rate(_cw_response(ai=[40.0], ae=[2.0]))

        assert_measured(outcome, metrics_mod.SOURCE_AGENT_LAMBDA_ERRORS)
        assert outcome.value == pytest.approx(5.0)

    def test_invocations_with_no_errors_is_a_measured_zero(self):
        """A window that ran cleanly has an error rate, and it is zero.

        This is the case that must stay a number: R4.8 replaces the *absent*
        zeros, not this one.
        """
        outcome, _ = _run_agent_error_rate(_cw_response(ai=[40.0], ae=[0.0]))

        assert_measured(outcome, metrics_mod.SOURCE_AGENT_LAMBDA_ERRORS)
        assert outcome.value == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════════════
# 4. `_get_rows_loaded` — a sum of confirmed writes, or absent (R4.9)
# ═══════════════════════════════════════════════════════════════════════════
#
# 2026-10-10: the `lines_ingested_24h` / `processing_rate` tiles moved from
# `_get_lines_ingested` (the `record_count_estimate` on successful
# `file_validation` rows) to `_get_rows_loaded` (`rows_written` on the Glue
# job's `load_metrics` rows, counted where `write_confirmed` is true). R4.9's
# sentence carries over unchanged in substance: when no row in the window
# carries a count, report Not_Established rather than substituting the number
# of rows. The old reader's defect — `return total if total > 0 else files` —
# is the substitution every absent case below is written against.


def _row(
    rows_written: Any = None,
    *,
    confirmed: bool = True,
    ts: datetime = datetime(2025, 4, 1, 12, tzinfo=UTC),
) -> dict[str, Any]:
    """One `load_metrics` state row, as the Query's projection returns it.

    ``rows_written=None`` produces a row with **no** ``rows_written`` at all — a
    row from which no count can be read, which is exactly the input R4.9 is
    about. ``confirmed`` is the row's ``write_confirmed``.
    """
    row: dict[str, Any] = {"timestamp": ts.isoformat(), "write_confirmed": confirmed}
    if rows_written is not None:
        row["rows_written"] = rows_written
    return row


def _run_rows_loaded(
    pages: list[dict[str, Any]] | None = None,
    *,
    side_effect: Exception | None = None,
) -> tuple[Any, MagicMock]:
    """Call `_get_rows_loaded` against a stubbed state table.

    Returns the outcome and the patched module ``logger``.
    """
    mock_state = MagicMock()
    if side_effect is not None:
        mock_state.query.side_effect = side_effect
    else:
        mock_state.query.side_effect = list(pages if pages is not None else [{"Items": []}])

    with (
        patch.object(metrics_mod, "state_table", mock_state),
        patch.object(metrics_mod, "logger") as mock_logger,
    ):
        outcome = metrics_mod._get_rows_loaded(START, END)
    return outcome, mock_logger


class TestRowsLoadedAbsence:
    """R4.9: no count in any row reports Not_Established, not a row count."""

    def test_rows_without_a_count_report_not_established(self):
        """R4.9's sentence, against its own input.

        Three confirmed `load_metrics` rows, none carrying a ``rows_written``.
        The figure is Not_Established.
        """
        outcome, _ = _run_rows_loaded([{"Items": [_row(), _row(), _row()]}])

        assert_absent(outcome, metrics_mod.SOURCE_LOAD_METRICS_ROWS)

    def test_the_number_of_rows_is_never_substituted_for_the_count(self):
        """R4.9, stated as the substitution it forbids.

        The row count is chosen to be an unmistakable value (3), so a
        row-count fallback fails here on the value rather than only on the
        token. A count of state rows is not a count of rows loaded.
        """
        rows = [_row() for _ in range(3)]
        outcome, _ = _run_rows_loaded([{"Items": rows}])

        assert outcome.value is None, (
            f"expected Not_Established for {len(rows)} rows carrying no count (R4.9), got {outcome.value!r}"
        )
        assert outcome.value != len(rows), "the count of STATE ROWS was substituted for the rows loaded (R4.9)"

    def test_an_empty_window_reports_not_established(self):
        """No rows at all is an established nothing, not a zero.

        Distinct from the case above only in the input; the answer is the same,
        which is what makes "a usable count was seen" rather than the row count
        the thing the absent arm keys off.
        """
        outcome, _ = _run_rows_loaded([{"Items": []}])

        assert_absent(outcome, metrics_mod.SOURCE_LOAD_METRICS_ROWS)

    def test_a_raising_query_is_incomplete_not_absent(self):
        """R4.2: a read that failed is not a window that was empty.

        The figure is Not_Established either way, and the token says which — an
        operator waits out an idle window and investigates a failed read.
        """
        outcome, _ = _run_rows_loaded(side_effect=RuntimeError("DynamoDB unavailable"))

        assert_incomplete(outcome, metrics_mod.SOURCE_LOAD_METRICS_ROWS)

    def test_a_raising_query_is_recorded_at_warning(self):
        """`design.md` §"Error Handling": the failure is logged, not raised."""
        _, mock_logger = _run_rows_loaded(side_effect=RuntimeError("DynamoDB unavailable"))

        assert mock_logger.warning.called or mock_logger.error.called or mock_logger.exception.called, (
            "a failed rows-loaded read must be recorded at WARNING or above"
        )


class TestRowsLoadedMeasuredZero:
    """R4.9 with R4.5: a count of zero is a measurement.

    The empty-source run writes ``rows_written = 0`` with ``write_confirmed =
    false``, and a run whose write was not confirmed adds nothing. Both are
    measurements whose value is zero and are reported as ``0`` — the same
    treatment R4.5 gives a measured zero baseline. Keying the absent arm off
    ``total > 0`` instead of "a count was seen" would send them back down the
    absent arm and re-create the collapse `LGR-4854b3d-1` forbids, in a new
    spelling.
    """

    def test_an_empty_source_row_is_reported_as_zero(self):
        """One run that read nothing, and we know it. The figure is 0, not absent."""
        outcome, _ = _run_rows_loaded([{"Items": [_row(0, confirmed=False)]}])

        assert_measured(outcome, metrics_mod.SOURCE_LOAD_METRICS_ROWS)
        assert outcome.value == 0

    def test_an_unconfirmed_write_is_a_measured_zero(self):
        """500 rows handed to the writer, write not confirmed: seen, adds 0."""
        outcome, _ = _run_rows_loaded([{"Items": [_row(500, confirmed=False)]}])

        assert_measured(outcome, metrics_mod.SOURCE_LOAD_METRICS_ROWS)
        assert outcome.value == 0

    def test_several_zero_rows_still_sum_to_a_measured_zero(self):
        """Two zero rows sum to zero, and zero rows would too — but only one of
        those is a measurement. The row count is 2 and the figure is 0, so a
        row-count substitution is visible here as well."""
        outcome, _ = _run_rows_loaded([{"Items": [_row(0, confirmed=False), _row(0)]}])

        assert_measured(outcome, metrics_mod.SOURCE_LOAD_METRICS_ROWS)
        assert outcome.value == 0


class TestRowsLoadedMeasuredSum:
    """The measured arm: the figure is the sum of the confirmed counts present.

    `test_metrics_rows_loaded_char.py` characterizes the read shape, the window
    and the summation across pages. What is added here is the *mixed* case R4.9
    turns on — some rows carrying a count and some not — because that is where a
    row count and a sum of counts give different answers.
    """

    def test_confirmed_counts_sum_to_their_total(self):
        """The confirmed counts are summed, `Decimal` included (DynamoDB's
        number type)."""
        outcome, _ = _run_rows_loaded([{"Items": [_row(1200), _row(Decimal("300"))]}])

        assert_measured(outcome, metrics_mod.SOURCE_LOAD_METRICS_ROWS)
        assert outcome.value == 1500

    def test_rows_without_a_count_do_not_contribute_a_count(self):
        """A mixed window sums the counts present and adds nothing for the rest.
        Four rows, one count of 500: the figure is 500 — neither 4 (the rows)
        nor 503 (a row count folded into the sum)."""
        outcome, _ = _run_rows_loaded([{"Items": [_row(500), _row(), _row(), _row()]}])

        assert_measured(outcome, metrics_mod.SOURCE_LOAD_METRICS_ROWS)
        assert outcome.value == 500

    def test_an_unconfirmed_count_is_not_added(self):
        """100 confirmed and 500 unconfirmed: the figure is 100, not 600."""
        outcome, _ = _run_rows_loaded([{"Items": [_row(100), _row(500, confirmed=False)]}])

        assert_measured(outcome, metrics_mod.SOURCE_LOAD_METRICS_ROWS)
        assert outcome.value == 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
