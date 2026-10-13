# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test — the rows-loaded figure is a sum of confirmed writes, or absent.

Feature: p39-metrics-truth, task 21.3 (Property 4), re-stated 2026-10-10 for the
replacement reader. Subject: ``_get_rows_loaded`` (``handlers/metrics.py``).

# Feature: p39-metrics-truth, Property 4: The rows-loaded figure is a sum of confirmed writes, or absent

*For any* set of ``load_metrics`` rows, the reported figure equals the sum of
``rows_written`` over the in-window rows that carry a usable count **and** whose
``write_confirmed`` is ``True``, whenever at least one in-window row carries a
usable count — and is ``null`` otherwise. In the ``null`` case it is not equal to
the number of rows unless that number is itself zero.

**Validates: Requirements 4.9** (no substitution of a row count for an absent
measurement), applied to the 2026-10-10 source.

What changed on 2026-10-10
--------------------------

Until then this property was stated over ``_get_lines_ingested``, which summed
``record_count_estimate`` (file size ÷ a fixed per-format divisor) on successful
``file_validation`` rows. That reader was replaced rather than adjusted: the
tiles now count the rows the generated Glue job reports writing, on its
``(run_id, "load_metrics")`` state row. The property's shape survives the
replacement because the defect it guards against does: a count of *rows in the
window* returned where no measurement exists. What is new is the second gate —
``rows_written`` is rows *handed to* the writer, and only ``write_confirmed`` says
the write returned (p37 risk N1) — so an unconfirmed row is a measurement that
adds nothing, never a contribution.

Why the third clause is the whole point
---------------------------------------

A row-count fallback is observable only on inputs where the row count is non-zero
while no row carries a usable count — on every other input it can agree with the
correct answer. So that shape is generated deliberately: :func:`_row_sets` draws a
``coverage`` branch, making "no in-window row carries a usable ``rows_written``"
one of three explicit cases instead of the vanishing tail of a per-row coin flip,
and it lets out-of-window rows carry confirmed counts so that the sharpest input
of all — counts exist, none of them in the window — is reachable. Each branch
emits a Hypothesis ``event``, so ``--hypothesis-show-statistics`` shows the
discriminating case is reached rather than leaving that to be assumed.

The third sentence is asserted **ahead of** the second, for the reason spelled out
at :func:`test_rows_loaded_figure_is_a_sum_of_confirmed_writes_or_absent`.

Where the oracle comes from
---------------------------

**The expected sum is computed here from the generated rows, never read back out
of the function.** :class:`_RowSet` carries ``expected_sum`` and
``counts_present``, both accumulated while the rows are built, so the oracle is
arithmetic on the input — `LGR-28b4d75-5` (**template / ratified**).

What "a usable count" means, and why not ``total > 0``
-----------------------------------------------------

A row carries a usable count when ``rows_written`` is a non-negative whole number
— an ``int`` or the ``Decimal`` DynamoDB returns. ``0`` is such a value: the
empty-source run writes ``rows_written = 0, write_confirmed = false``, which is a
**measurement whose value is zero**. A row whose write was not confirmed is
likewise a measurement that adds zero. That is why the property is keyed on
*presence* rather than on a positive sum — keying off ``total > 0`` would send a
measured zero back down the absent arm, the collapse `LGR-4854b3d-1`
(**template / ratified**) forbids.

Rows that carry **no** usable count come in every shape the read can meet: no
``rows_written`` key, an explicit ``None``, a ``bool`` (DynamoDB stores those
separately and ``True`` is not one row), a negative number, a non-numeric string,
a map and a list. None of them contributes to the sum or establishes a
measurement, and each still counts as a row — which is what makes them able to
trip the third clause.

Deliberately out of scope
-------------------------

* **The Query shape** — partition, window, projection, no filter, no Scan. Pinned
  in ``test_metrics_rows_loaded_char.py`` and ``test_query_not_scan.py``. This
  property is about the **return value**.
* **The provenance token** and **the raising-query arm** — asserted in
  ``test_metrics_absence.py``, the characterization file, and at the payload in
  Property 1. A stub that raises produces no *set of rows*, so it is outside this
  property's quantifier.
* **``processing_rate``** — the same figure over the window's wall clock; an
  absence-propagation statement about the payload, covered by Property 1.
* **Non-integral and string-shaped numbers.** ``int(Decimal("3.7"))`` truncates
  and ``int("42")`` succeeds inside ``item_int``; the requirement does not speak
  to either, and the Glue template casts the count with ``int()`` before writing.
  Integral ``int`` and ``Decimal`` are generated, plus values that are not counts.

Mocking
-------

The module-level ``state_table`` is patched with a ``MagicMock`` whose ``query``
returns the generated pages — the established pattern for these read paths. No
``mock_aws()`` is created and no fixture-covered service is stubbed by hand. No
AWS call is made.

_Requirements: R4.9_
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason test_metrics_trend_prop.py
# and the characterization files repeat it — under xdist --dist=loadfile any
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
# bootstrap as test_metrics_trend_prop.py and test_metrics_absence.py). A
# Hypothesis @given test cannot take a function-scoped fixture, so the env is
# established at import rather than through mock_lambda_env. AWS_REGION is
# deliberately NOT set: the unit layer pins it by assignment in
# tests/unit/conftest.py (LGR-3e45e65-1, template / ratified), and
# handlers/common.py freezes the region into module-global boto3 resources at
# import.
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

# ═══════════════════════════════════════════════════════════════════════════
# The window
# ═══════════════════════════════════════════════════════════════════════════

START = datetime(2025, 5, 1, tzinfo=UTC)
END = datetime(2025, 5, 2, tzinfo=UTC)

#: In-window timestamps, strictly inside the bounds. The inclusive boundaries
#: themselves are the window filter's business, not this property's — they are
#: pinned in `test_metrics_rows_loaded_char.py`.
_IN_WINDOW_TIMES = st.integers(min_value=1, max_value=23 * 60).map(lambda m: START + timedelta(minutes=m))

#: Timestamps outside the window, on both sides. A row here is returned by the
#: Query but excluded by the client-side `_parse_iso` re-check, so it contributes
#: nothing to the sum while still being a row a row-count fallback would have
#: had an opinion about.
_OUT_OF_WINDOW_TIMES = st.one_of(
    st.integers(min_value=1, max_value=10_000).map(lambda m: START - timedelta(minutes=m)),
    st.integers(min_value=1, max_value=10_000).map(lambda m: END + timedelta(minutes=m)),
)

# ═══════════════════════════════════════════════════════════════════════════
# Counts, confirmations, and the shapes that carry no count
# ═══════════════════════════════════════════════════════════════════════════
#
# Magnitudes run from 0 to 1e9: zero because an empty-source run or a confirmed
# write of nothing is a measured zero, and 1e9 because it is far past anything a
# dev deployment's Glue runs write.

_COUNT_VALUES = st.integers(min_value=0, max_value=10**9)

#: The two number types a real read can hand over. DynamoDB returns `Decimal`
#: for a stored number; an `int` reaches the same code path through a directly
#: constructed item, and both must sum identically.
_COUNTS = st.one_of(
    _COUNT_VALUES.map(lambda n: (n, n)),
    _COUNT_VALUES.map(lambda n: (n, Decimal(n))),
)

#: Marks a key the row does not carry at all, as distinct from carrying `None`.
_OMIT = object()

#: `write_confirmed` values. Only a boolean `True` confirms the write; every other
#: value — the template's `False`, a missing key, or a truthy non-bool — leaves the
#: row a measurement that adds nothing.
_CONFIRMATIONS = st.sampled_from([True, False, _OMIT, "true"])

#: Every `rows_written` that is not a usable count.
_UNUSABLE_COUNTS = st.sampled_from(
    [
        _OMIT,
        None,
        True,
        False,
        -1,
        Decimal("-250"),
        "not-a-number",
        {"nested": "map"},
        [],
    ]
)


def _row(rows_written: Any, confirmed: Any, ts: datetime) -> dict[str, Any]:
    """One `load_metrics` state row, as the projection returns it.

    The Query projects `rows_written,write_confirmed,#ts`, so those three keys are
    the whole row; `_OMIT` leaves a key off.
    """
    row: dict[str, Any] = {"timestamp": ts.isoformat()}
    if rows_written is not _OMIT:
        row["rows_written"] = rows_written
    if confirmed is not _OMIT:
        row["write_confirmed"] = confirmed
    return row


# ═══════════════════════════════════════════════════════════════════════════
# The generated row set
# ═══════════════════════════════════════════════════════════════════════════


class _RowSet(NamedTuple):
    """A generated set of `load_metrics` rows and the truth about it.

    Every field but ``pages`` is accumulated while the rows are built, so the
    oracle is arithmetic on the input rather than a second reading of the code
    under test (`LGR-28b4d75-5`).

    ``pages``
        The sequence of ``table.query`` responses, so the set can arrive in one
        page or several.
    ``expected_sum``
        ``rows_written`` summed over the in-window rows with a usable count and
        ``write_confirmed is True``.
    ``counts_present``
        How many in-window rows carry a usable count, confirmed or not. Zero is
        the absent arm.
    ``confirmed_rows``
        How many of those were confirmed — so the statistics show unconfirmed-only
        measurements are reached.
    ``in_window_rows``
        The number of rows the window re-check admits.
    ``total_rows``
        Every row the Query returned, in-window or not — the looser reading of
        "the number of rows", asserted alongside the precise one so a fallback
        written against either loses.
    """

    pages: list[dict[str, Any]]
    expected_sum: int
    counts_present: int
    confirmed_rows: int
    in_window_rows: int
    total_rows: int


@st.composite
def _row_sets(draw) -> _RowSet:
    """A set of `load_metrics` rows, paged, with the truth about it.

    ``coverage`` is drawn from an explicit three-way choice rather than from a
    per-row coin flip, and that is the load-bearing part of this generator:

    * ``"none"`` — **no in-window row carries a usable count.** The
      discriminating case.
    * ``"all"`` — every in-window row carries one.
    * ``"mixed"`` — a per-row flip, which is what a real window looks like.

    Out-of-window rows **always** carry a usable, confirmed count, whatever
    ``coverage`` says. That is what makes ``"none"`` produce the sharpest input:
    rows exist, confirmed counts exist, and none is in the window, so the answer
    is absent while ``total_rows`` is non-zero.
    """
    coverage = draw(st.sampled_from(("none", "all", "mixed")))

    # Zero in-window rows is generated on purpose: it is the "unless that number
    # is itself zero" carve-out in the property's own sentence.
    n_in = draw(st.integers(min_value=0, max_value=6))
    n_out = draw(st.integers(min_value=0, max_value=2))

    rows: list[dict[str, Any]] = []
    expected_sum = 0
    counts_present = 0
    confirmed_rows = 0

    for _ in range(n_in):
        carries = coverage == "all" or (coverage == "mixed" and draw(st.booleans()))
        confirmed = draw(_CONFIRMATIONS)
        if carries:
            value, stored = draw(_COUNTS)
            counts_present += 1
            if confirmed is True:
                expected_sum += value
                confirmed_rows += 1
            rows.append(_row(stored, confirmed, draw(_IN_WINDOW_TIMES)))
        else:
            rows.append(_row(draw(_UNUSABLE_COUNTS), confirmed, draw(_IN_WINDOW_TIMES)))

    # Out-of-window rows are spliced in at drawn positions rather than appended,
    # so they land on either side of a page boundary. Where a row falls relative
    # to a page break is the only positional fact that matters: addition is
    # commutative.
    for _ in range(n_out):
        _value, stored = draw(_COUNTS)
        position = draw(st.integers(min_value=0, max_value=len(rows)))
        rows.insert(position, _row(stored, True, draw(_OUT_OF_WINDOW_TIMES)))

    n_pages = draw(st.integers(min_value=1, max_value=3))
    pages = _paginate(rows, n_pages)

    return _RowSet(
        pages=pages,
        expected_sum=expected_sum,
        counts_present=counts_present,
        confirmed_rows=confirmed_rows,
        in_window_rows=n_in,
        total_rows=n_in + n_out,
    )


def _paginate(rows: list[dict[str, Any]], n_pages: int) -> list[dict[str, Any]]:
    """Split `rows` into at most `n_pages` `table.query` responses.

    Every page but the last carries a `LastEvaluatedKey`, which is what
    `_query_state_by_step` loops on. An empty row set yields a single empty page —
    what a Query over a window with no `load_metrics` row actually returns.
    """
    size = max(1, (len(rows) + n_pages - 1) // n_pages)
    chunks = [rows[i : i + size] for i in range(0, len(rows), size)] or [[]]
    pages: list[dict[str, Any]] = [{"Items": chunk} for chunk in chunks]
    for index, page in enumerate(pages[:-1]):
        page["LastEvaluatedKey"] = {"run_id": f"run-{index}", "step_name": "load_metrics"}
    return pages


def _rows_loaded_figure(pages: list[dict[str, Any]]) -> Any:
    """The figure `_get_rows_loaded(START, END)` reports for `pages`.

    The provenance half of the return is discarded on purpose: the token is
    asserted in `test_metrics_absence.py`, the characterization file and Property
    1, and this file states one property.
    """
    mock_state = MagicMock()
    mock_state.query.side_effect = list(pages)
    with patch.object(metrics_mod, "state_table", mock_state):
        figure, _provenance = metrics_mod._get_rows_loaded(START, END)
    return figure


# ═══════════════════════════════════════════════════════════════════════════
# Property 4
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=200)
@given(_row_sets())
def test_rows_loaded_figure_is_a_sum_of_confirmed_writes_or_absent(row_set: _RowSet) -> None:
    """The figure sums the confirmed counts present, or is absent — never a row count.

    All three clauses, with the third asserted **before** the second. With the
    figure at ``None``, ``None != n`` holds for every non-zero ``n``, so clause 3
    is logically implied by clause 2 and would never fail on its own. What makes
    it load-bearing is the generation and the ordering: the generation puts the
    function in front of inputs where rows exist and no usable count does — the
    only shape on which a row-count fallback is observable — and the ordering
    means that when such a fallback returns the row count, the assertion that
    fires names the unit error rather than a generic "expected None".
    """
    figure = _rows_loaded_figure(row_set.pages)

    if row_set.counts_present:
        if row_set.confirmed_rows == 0:
            event("counts present, none confirmed — a measured zero")
        else:
            event("counts present, some confirmed — sum clause")
        assert figure == row_set.expected_sum, (
            f"the rows-loaded figure must be rows_written summed over the {row_set.confirmed_rows} "
            f"confirmed row(s) among {row_set.counts_present} in-window row(s) with a usable count — "
            f"expected {row_set.expected_sum}, got {figure!r}"
        )
        return

    if row_set.in_window_rows:
        event("no usable count, rows present — the discriminating case")
    else:
        event("no usable count, no in-window row — the zero carve-out")

    # Clause 3. Both readings of "the number of rows" are asserted: the rows the
    # window admits, and every row the Query handed back, so a fallback written
    # against either loses. Each is guarded by its own non-zero test, which is the
    # "unless that number is itself zero" carve-out.
    if row_set.in_window_rows != 0:
        assert figure != row_set.in_window_rows, (
            f"the count of ROWS IN THE WINDOW ({row_set.in_window_rows}) was substituted for a count of "
            f"rows loaded — a fallback to a different unit is not a fallback (R4.9)"
        )
    if row_set.total_rows != 0:
        assert figure != row_set.total_rows, (
            f"the count of rows the Query returned ({row_set.total_rows}) was substituted for a count "
            f"of rows loaded (R4.9)"
        )

    # Clause 2. Reached on every absent input, including the zero carve-out: an
    # empty window is an established nothing, and a zero there would be the same
    # substitution at its degenerate end.
    assert figure is None, (
        f"no in-window load_metrics row carries a usable rows_written, so the figure must be "
        f"Not_Established — got {figure!r} from {row_set.in_window_rows} in-window row(s) (R4.9)"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
