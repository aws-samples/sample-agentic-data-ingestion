# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test — a reported trend is the sign of the raw change.

Feature: p39-metrics-truth, task 2.2. Subject: ``_compute_trend``
(``handlers/metrics.py:878``).

# Feature: p39-metrics-truth, Property 2: A reported trend is the sign of the raw change, and absence of a baseline is its own answer

*For any* pair ``(current, previous)`` where ``previous`` is a measured number,
the reported direction is ``up`` when ``current > previous``, ``down`` when
``current < previous``, and ``stable`` when the relative change is within the
stability band — with no metric receiving a different answer from any other.
*For any* ``current`` where ``previous`` is unavailable, the reported direction
is ``not_established`` and never ``stable``. A measured ``previous`` of zero is
a baseline and never yields ``not_established``.

**Validates: Requirements 3.1, 3.3, 3.5, 4.4, 4.5, 9.5**

Where the expectations come from
--------------------------------

**Every expectation below is written from the requirements, and none of it from
the current output.** That is not a stylistic preference: before task 2.1
landed, `_compute_trend` took an `invert` flag that the `agent_latency` and
`cost` call sites passed, so a *rise* in cost was reported as ``down`` — and a
test written from that output would have pinned the defect. `LGR-28b4d75-5`
(**template / ratified**) states the general form: "a test written from the
producer's output cannot detect the producer drifting, because the output is
its own oracle." `R9.4` is the same rule for this spec.

So the oracle here is arithmetic on the generated pair:

* ``R3.1`` — the handler reports "the direction of the raw change between the
  current and previous window, applying **no** polarity transformation".
* ``R3.3`` — a cost figure exceeding the previous window's by more than the
  stability threshold is reported ``up``. Not ``down``, which is what the
  backend produced before task 2.1, and not "up for ingestion but down for
  cost", which is what an `invert` flag makes possible.
* ``R3.5`` — no parameter may let a caller invert a computed direction. The
  machine-checkable form of that is :func:`test_rise_beyond_the_band_reports_up`
  asserting the *same* answer for all four `TrendIndicators` fields: two of them
  (``agent_latency``, ``cost``) are ``lower-is-better`` in the frontend's
  `TREND_POLARITY` (`MetricsDashboardPage.tsx:42-47`) and are exactly the two
  that carried ``invert=True``. If polarity ever comes back to the backend,
  those two stop agreeing with the other two and the property fails.
* ``R9.5`` — the same assertion, read from the other side: a re-introduced
  backend inversion fails a test rather than a screen. Task 2.3's
  ``test_metrics_polarity.py`` covers the *signature* half structurally; this
  file covers the *behavioural* half, which is strictly stronger — an inversion
  hard-coded into the body carries no parameter for a signature check to see.
* ``R4.4`` — no baseline in the previous window ⇒ ``not_established``, never
  ``stable``.
* ``R4.5`` — both windows carrying a baseline and a relative change below the
  stability threshold ⇒ ``stable``. Together with `LGR-4854b3d-1` (**template /
  ratified**, a three-state value is never collapsed to its zero) this is why a
  *measured* zero baseline is a baseline: absence and a measurement of zero are
  different answers and must not share one.

The stability band is a parameter, not an oracle
------------------------------------------------

``R3.3`` says "by more than the stability threshold" and ``R4.5`` says "below
the stability threshold". **Neither names a value**, so no assertion here uses
one. :data:`_STABILITY_BAND` records the band the implementation currently uses
purely so the generators can stay clear of it by a wide margin — pairs built for
``up``/``down`` sit at a relative change of at least 0.5 (ten times the band)
and pairs built for ``stable`` at most 0.01 (a fifth of it). A future change to
the threshold anywhere in ``(0.01, 0.5)`` leaves every property below true, and
the boundary itself is deliberately untested because it is not a requirement.

The expected-red window, now closed
----------------------------------

Two clauses were **expected to fail until p39 task 3.2**, and both are green as
of it:

1. :func:`test_absent_baseline_reports_not_established` (``R4.4``) — before 3.2
   ``previous=None`` reached ``(current - previous)`` and raised ``TypeError``.
2. :func:`test_measured_zero_baseline_with_positive_current_reports_up`
   (``R4.5``) — before 3.2 ``previous == 0`` short-circuited to ``"stable"``,
   collapsing "no baseline" and "a baseline of zero" into one answer. That
   collapse is the fence `LGR-4854b3d-1` names.

Both carried ``@pytest.mark.xfail(strict=True)`` rather than a skip while the
window was open, for two reasons. A skip records nothing and reads as coverage;
an xfail records the red in the run's own output. And ``strict=True`` meant that
when 3.2 landed each xfail became an **XPASS**, which pytest reports as a
failure — so the closing task could not land quietly without deleting the
marker, and the markers' removal (task 3.2, alongside widening ``previous`` to
``float | None``) is the visible acknowledgement that both clauses went green.
The assertions themselves are unchanged; only the markers were removed.

The third measured-zero case was green on both sides of 3.2 and never carried a
marker: see :func:`test_measured_zero_baseline_with_zero_current_reports_stable`.

**Nothing in this file may now be red**, and there is no remaining expected-red
window. The rise, fall and stable clauses went green the moment task 2.1 deleted
the ``invert`` parameter.

Scope
-----

``_compute_trend`` is a pure function of two numbers — 4 call sites, all in
``_build_comprehensive_metrics:270-273``, and **zero** direct test coverage
before this file (requirements risk 6, re-confirmed by grep over ``tests/``).
So there is no AWS client to stub, no fixture to build and no mock anywhere
below. The payload-level statement — that the four ``trends`` entries are
assembled from this one function — belongs to Property 1 (task 21.1) and to
task 2.3's structural guard, not here.
"""

import os
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason the sibling
# test_metrics_stage_durations_char.py repeats it — under xdist --dist=loadfile
# any file in this directory may be the first imported.
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
# bootstrap as test_query_not_scan.py and the task 1 characterization files).
# AWS_REGION is deliberately NOT set here: the unit layer pins it by assignment
# in tests/unit/conftest.py (LGR-3e45e65-1, template / ratified).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

# ═══════════════════════════════════════════════════════════════════════════
# The vocabulary the requirements are written in
# ═══════════════════════════════════════════════════════════════════════════

#: The four directions ``R3.1``, ``R3.3``, ``R4.4`` and ``R4.5`` name between
#: them. Spelled here so a typo in an assertion is a NameError rather than a
#: silently-passing string comparison.
UP = "up"
DOWN = "down"
STABLE = "stable"
NOT_ESTABLISHED = "not_established"

#: The four ``TrendIndicators`` fields (``types.ts``), which are also the four
#: ``_compute_trend`` call sites (``metrics.py:270-273``). ``agent_latency`` and
#: ``cost`` are ``lower-is-better`` in the frontend's `TREND_POLARITY`
#: (`MetricsDashboardPage.tsx:42-47`) and were the two that passed
#: ``invert=True``; ``ingestion`` and ``success`` are ``higher-is-better``. The
#: property is that all four get the **same** answer for the same pair, because
#: polarity lives on the other side of the seam (`LGR-99050e6-1`, template /
#: ratified) and `R3.2` forbids a second table here.
TREND_FIELDS = ("ingestion", "success", "agent_latency", "cost")

#: The relative-change magnitude at which the implementation currently stops
#: calling a change ``stable``. **Not used as an oracle** — see the module
#: docstring. It exists so the two margins below can be stated relative to it.
_STABILITY_BAND = 0.05

#: Ten times the band. Pairs built for ``up`` / ``down`` sit at least this far
#: out, so the direction is unambiguous under any threshold below 0.5.
_OUTSIDE_BAND = 0.5

#: A fifth of the band. Pairs built for ``stable`` sit within this, so they are
#: inside the band under any threshold above 0.01.
_INSIDE_BAND = 0.01


# ═══════════════════════════════════════════════════════════════════════════
# Generators — constrained to the values these four metrics actually carry
# ═══════════════════════════════════════════════════════════════════════════
#
# The four call sites pass: ``lines_ingested`` (a summed integer record
# estimate, ~1.1e6 on the verified window), ``overall_success`` (a percentage,
# 0-100), ``agent_latency_ms`` (milliseconds, ~1e3) and ``monthly_cost`` (USD,
# ~1e0). All four are **non-negative**, and two of them are integers, so both
# types are generated on both sides of the pair. Nothing negative is generated:
# no call site can produce one, and a negative baseline would put a sign into
# ``abs(previous)`` that no requirement speaks to.
#
# Magnitudes span 1e-3 (a fraction of a cent) to 1e9 (well past any observed
# record count), which brackets every real value with room either side.
#
# Each strategy below establishes its band condition **by construction** rather
# than by ``assume``, so there is no filtering and no health-check pressure at
# 200 examples.

#: A measured, strictly-positive baseline. Positive rather than non-negative
#: because ``previous == 0`` is its own clause with its own two answers
#: (``R4.5``), generated separately below.
_POSITIVE_BASELINES = st.one_of(
    st.floats(min_value=1e-3, max_value=1e9, allow_nan=False, allow_infinity=False),
    st.integers(min_value=1, max_value=10**7),
)


@st.composite
def _rising_pairs(draw) -> tuple[float, float]:
    """``(current, previous)`` with ``current > previous`` and a large rise.

    Two branches so both an integer count and a fractional cost are exercised:
    a float baseline scaled by at least 1.5, and an integer baseline with an
    integer current of at least twice it. Relative change is ``>= 0.5`` in both.
    """
    if draw(st.booleans()):
        previous = draw(_POSITIVE_BASELINES)
        factor = draw(st.floats(min_value=1.0 + _OUTSIDE_BAND, max_value=1000.0, allow_nan=False, allow_infinity=False))
        return previous * factor, previous
    previous_int = draw(st.integers(min_value=1, max_value=10**7))
    current_int = draw(st.integers(min_value=2 * previous_int, max_value=2 * previous_int + 10**7))
    return current_int, previous_int


@st.composite
def _falling_pairs(draw) -> tuple[float, float]:
    """``(current, previous)`` with ``current < previous`` and a large fall.

    The float branch admits ``current == 0.0``: a metric dropping to nothing
    between windows is a real fall, and ``R3.1`` gives it the same answer as any
    other fall. Relative change is ``<= -0.5`` in both branches.
    """
    if draw(st.booleans()):
        previous = draw(_POSITIVE_BASELINES)
        factor = draw(st.floats(min_value=0.0, max_value=1.0 - _OUTSIDE_BAND, allow_nan=False, allow_infinity=False))
        return previous * factor, previous
    previous_int = draw(st.integers(min_value=2, max_value=10**7))
    current_int = draw(st.integers(min_value=0, max_value=previous_int // 2))
    return current_int, previous_int


@st.composite
def _stable_pairs(draw) -> tuple[float, float]:
    """``(current, previous)`` whose relative change is well inside the band.

    ``current == previous`` is reachable on both branches — a metric that did
    not move is the plainest ``stable`` there is. The integer branch starts at
    1000 so that a ±0.5% perturbation is representable in whole numbers at all.
    """
    if draw(st.booleans()):
        previous = draw(_POSITIVE_BASELINES)
        drift = draw(st.floats(min_value=-_INSIDE_BAND, max_value=_INSIDE_BAND, allow_nan=False, allow_infinity=False))
        return previous * (1.0 + drift), previous
    previous_int = draw(st.integers(min_value=1000, max_value=10**7))
    slack = previous_int // 200
    current_int = draw(st.integers(min_value=previous_int - slack, max_value=previous_int + slack))
    return current_int, previous_int


#: A current-window value for the clauses where ``previous`` is fixed. Includes
#: zero, because "nothing measured now" is a legitimate current value and is the
#: case that separates ``R4.4``'s absent baseline from ``R4.5``'s measured zero.
_CURRENT_VALUES = st.one_of(
    st.just(0),
    st.just(0.0),
    st.floats(min_value=1e-3, max_value=1e9, allow_nan=False, allow_infinity=False),
    st.integers(min_value=1, max_value=10**7),
)

#: A strictly-positive current value — the ``R4.5`` measured-zero clause that
#: must report ``up``.
_POSITIVE_CURRENT_VALUES = _POSITIVE_BASELINES


# ═══════════════════════════════════════════════════════════════════════════
# Green from task 2.1 onward — the raw-change clauses
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=200)
@given(_rising_pairs())
def test_rise_beyond_the_band_reports_up(pair: tuple[float, float]) -> None:
    """A rise beyond the band reports ``up``, identically for every metric.

    ``R3.1`` (direction of the raw change, no polarity transformation),
    ``R3.3`` (a cost figure above the previous window's reports ``up``),
    ``R3.5`` / ``R9.5`` (no inversion survives in the backend).

    The per-field loop is where ``R3.5`` becomes machine-checkable. Before task
    2.1 this function took an ``invert`` flag; ``agent_latency`` and ``cost``
    passed it, so those two answered ``down`` to the same pair the other two
    answered ``up`` to. Asserting a single shared answer for all four fields
    fails on any reintroduction of that split — including one hard-coded into
    the body, which task 2.3's signature guard could not see.
    """
    current, previous = pair
    assert current > previous, f"generator invariant: {current} must exceed {previous}"

    answers = {field: metrics_mod._compute_trend(current, previous) for field in TREND_FIELDS}

    assert set(answers.values()) == {UP}, (
        f"a rise from {previous} to {current} must report {UP!r} for every trend field — "
        f"polarity belongs to the frontend (R3.1/R3.5, LGR-99050e6-1), got {answers}"
    )


@settings(max_examples=200)
@given(_falling_pairs())
def test_fall_beyond_the_band_reports_down(pair: tuple[float, float]) -> None:
    """A fall beyond the band reports ``down``, identically for every metric.

    The mirror of the clause above: ``R3.1``'s "direction of the raw change" is
    a two-sided statement, and an inversion that flipped only one side would
    pass a rise-only test.
    """
    current, previous = pair
    assert current < previous, f"generator invariant: {current} must be below {previous}"

    answers = {field: metrics_mod._compute_trend(current, previous) for field in TREND_FIELDS}

    assert set(answers.values()) == {DOWN}, (
        f"a fall from {previous} to {current} must report {DOWN!r} for every trend field (R3.1/R3.5), got {answers}"
    )


@settings(max_examples=200)
@given(_stable_pairs())
def test_relative_change_inside_the_band_reports_stable(pair: tuple[float, float]) -> None:
    """Both windows measured and a small relative change reports ``stable``.

    ``R4.5``. This is the one direction ``stable`` legitimately answers, and it
    is why ``R4.4``'s absent baseline needs a *different* word: if absence also
    answered ``stable``, an operator could not tell a metric that held steady
    from one that was never measured.
    """
    current, previous = pair

    answers = {field: metrics_mod._compute_trend(current, previous) for field in TREND_FIELDS}

    assert set(answers.values()) == {STABLE}, (
        f"a relative change of {(current - previous) / abs(previous):+.4f} from {previous} to "
        f"{current} is inside the stability band and must report {STABLE!r} (R4.5), got {answers}"
    )


@settings(max_examples=200)
@given(_CURRENT_VALUES)
def test_a_measured_baseline_never_reports_not_established(current: float) -> None:
    """A measured baseline — zero included — never answers ``not_established``.

    The negative half of ``R4.4`` / ``R4.5``, and the half that is testable on
    both sides of task 3.2. ``not_established`` is reserved for *absence*; a
    number that was measured, whatever its value, has a baseline. Kept separate
    from the two measured-zero clauses below because this half is green now and
    stays green, while the answer for a positive current over a zero baseline
    does not.
    """
    answer = metrics_mod._compute_trend(current, 0.0)

    assert answer != NOT_ESTABLISHED, (
        f"previous=0.0 is a measured baseline, not an absent one — {NOT_ESTABLISHED!r} is "
        f"reserved for absence (R4.4/R4.5, LGR-4854b3d-1)"
    )


@settings(max_examples=200)
@given(st.sampled_from([0, 0.0]))
def test_measured_zero_baseline_with_zero_current_reports_stable(current: float) -> None:
    """A measured zero baseline and a current of zero reports ``stable``.

    ``R4.5``. The relative-change formula cannot be applied to a zero
    denominator, so this arm is an absolute rule rather than an arithmetic one:
    nothing then, nothing now, so nothing moved.

    **Green on both sides of task 3.2** — today ``previous == 0`` short-circuits
    to ``stable`` and reaches the right answer for the wrong reason; after 3.2 it
    is the ``current == 0`` arm of the absolute rule. So this clause carries no
    xfail marker, which is the point of separating it from the positive-current
    case: it shows exactly how much of the measured-zero behaviour is already
    correct.
    """
    assert metrics_mod._compute_trend(current, 0.0) == STABLE, (
        f"a measured baseline of zero with a current of zero must report {STABLE!r} — nothing moved (R4.5)"
    )


# ═══════════════════════════════════════════════════════════════════════════
# The absent-baseline clauses — red until p39 task 3.2, green from it
# ═══════════════════════════════════════════════════════════════════════════
# Both carried a strict xfail until 3.2 widened `previous` to `float | None`;
# 3.2 removed the markers, which is what makes the transition visible. The
# assertions are unchanged and are what R4.4 and R4.5 say. Do NOT weaken them.


@settings(max_examples=200)
@given(_CURRENT_VALUES)
def test_absent_baseline_reports_not_established(current: float) -> None:
    """No baseline in the previous window reports ``not_established``.

    ``R4.4``, and the reason `LGR-4854b3d-1` (**template / ratified**) is the
    fence: a three-state value is never collapsed to its zero. "We have no
    previous window to compare against" and "the comparison came out level" are
    different facts, and only one of them is ``stable``.
    """
    answer = metrics_mod._compute_trend(current, None)

    assert answer == NOT_ESTABLISHED, f"an absent baseline must report {NOT_ESTABLISHED!r} (R4.4), got {answer!r}"
    assert answer != STABLE, (
        "absence must not be reported as stability — an operator cannot tell an unmeasured "
        "metric from a steady one (R4.4)"
    )


@settings(max_examples=200)
@given(_POSITIVE_CURRENT_VALUES)
def test_measured_zero_baseline_with_positive_current_reports_up(current: float) -> None:
    """A measured zero baseline and a positive current reports ``up``.

    ``R4.5``. Nothing last window, something this window: that is a rise, and
    reporting it as ``stable`` hides the only interesting thing that happened.
    The relative-change formula cannot produce this answer — the denominator is
    zero — so this arm is an absolute rule, which is why it needs stating
    separately rather than falling out of the arithmetic.
    """
    answer = metrics_mod._compute_trend(current, 0.0)

    assert answer == UP, (
        f"a rise from a measured baseline of 0 to {current} must report {UP!r}, not "
        f"{answer!r} — a measured zero is a baseline (R4.5)"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
