# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: `_get_rows_loaded` (`handlers/metrics.py`).

The reader behind the `lines_ingested_24h` and `processing_rate` tiles. This file
pins what that read must keep doing:

1. **The bounded Query on `step_name-timestamp-index`** — `step_name` equality on
   the `load_metrics` partition plus a `timestamp BETWEEN` window, projecting
   `rows_written`, `write_confirmed` and the `#ts` alias, with no
   FilterExpression and never a Scan (P28 R17.1).
2. **The sum** — `rows_written` across every page, counted only where
   `write_confirmed is True`, with rows outside `[start, end]` excluded by the
   client-side `_parse_iso` re-check and an unusable `rows_written` (absent,
   `bool`, negative, non-numeric) skipped rather than raised.
3. **The three outcomes** — at least one row with a usable count is a
   measurement (a sum of zeros is a measured 0, token `load_metrics_rows`); a
   successful Query with no such row is `load_metrics_rows_absent`; a raising
   Query is `load_metrics_rows_incomplete`, logged at WARNING.

HISTORY, 2026-10-10. This file used to be `test_metrics_lines_ingested_char.py`
and characterized `_get_lines_ingested`, which summed the `record_count_estimate`
(file size ÷ a fixed per-format divisor) on successful `file_validation` rows.
That read was replaced, not adjusted: the estimate counted re-runs again,
credited runs that never reached Glue, and said nothing about a JDBC source. Its
status filter and `output_data` projection went with it.

WHERE THE EXPECTATIONS COME FROM
--------------------------------
From the stated requirement for the replacement, never from running the function:
`LGR-28b4d75-5` (template / ratified) — a test written from the producer's output
cannot detect the producer drifting. Every expected total below is arithmetic on
the rows the test itself builds.

The provenance tokens are written out literally here, once, because they are wire
values: the dashboard fixtures and the frontend read these exact strings. The
sibling `test_metrics_absence.py` takes the source name from the module constant
and spells only the suffixes; this file is where the constant's value is pinned.

MOCKING
-------
The module-level `state_table` resource is patched with a `MagicMock` — the
established pattern for these read paths (`test_query_not_scan.py`) and the only
way to observe the Query *shape*. No `mock_aws()` is created and no boto3 client
is replaced. The module `logger` is patched where the WARNING is asserted. No AWS
call is made.
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import AttributeBase

# ---------------------------------------------------------------------------
# Path / env setup
# ---------------------------------------------------------------------------
# `conftest.py` in this directory already puts `shared/` and the dashboard API
# Lambda package on `sys.path` at collection start, and it is deliberately
# repeated here for the same reason `test_query_not_scan.py` repeats it: under
# `--dist=loadfile` any file may be the first imported on a worker.
_DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(_DASHBOARD_API_DIR), os.path.abspath(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: The unit layer's region, read from the layer that owns the decision rather
#: than spelled again here. `tests/unit/conftest.py` assigns `AWS_REGION` and
#: `AWS_DEFAULT_REGION` for the whole unit layer (`LGR-3e45e65-1`, template /
#: ratified) and runs before this module is imported, so this is that value.
#: A `KeyError` here is a loud signal the layer pin has gone away, which is the
#: failure that record exists to prevent — not something to paper over with a
#: second default.
_UNIT_LAYER_REGION = os.environ["AWS_REGION"]


@pytest.fixture
def metrics_module(mock_lambda_env, monkeypatch):
    """`handlers.metrics`, imported with the env its import requires.

    `mock_lambda_env` is the shared fixture from `tests/fixtures/conftest.py`; it
    supplies the table names `handlers/common.py` `require_env`s at **import**
    time, which is why the module import below is inside a fixture rather than at
    module scope.

    The two region variables are then re-pinned to the unit layer's value.
    `mock_lambda_env` sets `us-east-1` for its own callers, but
    `handlers/common.py` freezes `AWS_REGION` into module-global boto3 resources
    at import, and whichever test imports the module first would decide that
    region for the whole session — exactly the collection-order dependence
    `LGR-3e45e65-1` removed from this layer.
    """
    monkeypatch.setenv("AWS_REGION", _UNIT_LAYER_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _UNIT_LAYER_REGION)

    from handlers import metrics as metrics_mod

    return metrics_mod


# ---------------------------------------------------------------------------
# The requirement's vocabulary
# ---------------------------------------------------------------------------
#: The three `determined_from` values the read may answer with.
MEASURED_TOKEN = "load_metrics_rows"
ABSENT_TOKEN = "load_metrics_rows_absent"
INCOMPLETE_TOKEN = "load_metrics_rows_incomplete"

START = datetime(2025, 3, 1, tzinfo=UTC)
END = datetime(2025, 3, 2, tzinfo=UTC)
IN_WINDOW = datetime(2025, 3, 1, 12, tzinfo=UTC)

#: Marks a key the row does not carry at all, as distinct from carrying `None`.
_OMIT = object()


def _row(rows_written: Any = _OMIT, *, confirmed: Any = True, ts: Any = IN_WINDOW) -> dict[str, Any]:
    """One `load_metrics` state row, shaped as the projection returns it.

    The Query projects `rows_written`, `write_confirmed` and the `timestamp` sort
    key, so those three are the whole row. Pass `_OMIT` for any of them to leave
    the key off; `ts` may be a `datetime` (rendered `isoformat()`, which is what
    the Glue template writes) or a raw string.
    """
    row: dict[str, Any] = {}
    if ts is not _OMIT:
        row["timestamp"] = ts.isoformat() if isinstance(ts, datetime) else ts
    if rows_written is not _OMIT:
        row["rows_written"] = rows_written
    if confirmed is not _OMIT:
        row["write_confirmed"] = confirmed
    return row


def _leaves(condition: Any) -> list[tuple[str, str, tuple]]:
    """Flatten a boto3 condition tree into `(operator, attribute, operands)` leaves.

    Same shape as `test_query_not_scan.py:_leaves`; a local copy rather than a
    shared helper, as that file's own copy is.
    """
    expr = condition.get_expression()
    op = expr["operator"]
    values = expr["values"]
    if op in ("AND", "OR"):
        out: list[tuple[str, str, tuple]] = []
        for sub in values:
            out.extend(_leaves(sub))
        return out
    name = values[0].name if isinstance(values[0], AttributeBase) else str(values[0])
    return [(op, name, tuple(values[1:]))]


def _call_rows_loaded(
    metrics_mod: Any,
    pages: list[dict[str, Any]] | None = None,
    *,
    side_effect: Exception | None = None,
) -> tuple[Any, MagicMock, MagicMock]:
    """Run `_get_rows_loaded(START, END)` against a stubbed state table.

    `pages` is the sequence of `table.query` responses, so a caller can hand over
    one page or several; `side_effect` makes the Query raise instead. Returns the
    outcome, the state-table stub (for the Query shape) and the patched module
    logger (for the WARNING).
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
    return outcome, mock_state, mock_logger


def _rows_loaded(metrics_mod: Any, *rows: dict[str, Any]) -> Any:
    """The outcome for one page holding ``rows``."""
    outcome, _, _ = _call_rows_loaded(metrics_mod, [{"Items": list(rows)}])
    return outcome


def assert_measured(outcome: Any, expected: int) -> None:
    """A measurement of ``expected`` rows, with the bare source token."""
    assert outcome.value == expected, f"expected {expected} rows loaded, got {outcome.value!r}"
    assert outcome.value is not None
    assert outcome.determined_from == MEASURED_TOKEN, outcome.determined_from


def assert_absent(outcome: Any) -> None:
    """An established nothing: no value, and the `_absent` token."""
    assert outcome.value is None, f"expected Not_Established, got {outcome.value!r}"
    assert outcome.determined_from == ABSENT_TOKEN, outcome.determined_from


# ═══════════════════════════════════════════════════════════════════════════
# 1. The bounded Query
# ═══════════════════════════════════════════════════════════════════════════


class TestRowsLoadedReadShape:
    """The read is one bounded Query on the step GSI's `load_metrics` partition."""

    def test_the_source_token_is_the_load_metrics_rows_name(self, metrics_module):
        """The constant behind every token below carries the wire value."""
        assert metrics_module.SOURCE_LOAD_METRICS_ROWS == MEASURED_TOKEN

    def test_reads_the_load_metrics_partition_bounded_to_the_window(self, metrics_module):
        """One Query on `step_name-timestamp-index`: `step_name` equals
        `load_metrics`, `timestamp` BETWEEN the window bounds as ISO strings.
        Never a Scan (P28 R17.1)."""
        _, mock_state, _ = _call_rows_loaded(metrics_module)

        assert mock_state.query.call_count == 1
        kwargs = mock_state.query.call_args.kwargs
        assert kwargs["IndexName"] == "step_name-timestamp-index"

        leaves = _leaves(kwargs["KeyConditionExpression"])
        assert ("=", "step_name", ("load_metrics",)) in leaves
        assert ("BETWEEN", "timestamp", (START.isoformat(), END.isoformat())) in leaves
        assert ("=", "step_name", ("file_validation",)) not in leaves

        mock_state.scan.assert_not_called()

    def test_projects_the_count_the_confirmation_and_the_timestamp(self, metrics_module):
        """`rows_written`, `write_confirmed` and `#ts`, with `#ts` aliasing
        `timestamp` (a DynamoDB reserved word). Nothing from `output_data`."""
        _, mock_state, _ = _call_rows_loaded(metrics_module)

        kwargs = mock_state.query.call_args.kwargs
        projected = {name.strip() for name in kwargs["ProjectionExpression"].split(",")}
        assert projected == {"rows_written", "write_confirmed", "#ts"}
        assert kwargs["ExpressionAttributeNames"] == {"#ts": "timestamp"}

    def test_applies_no_server_side_filter(self, metrics_module):
        """Every `load_metrics` row in the window is read: confirmation is decided
        client-side, so an unconfirmed row can still establish a measurement. The
        old read's `status == "success"` filter has no counterpart here."""
        _, mock_state, _ = _call_rows_loaded(metrics_module)

        assert "FilterExpression" not in mock_state.query.call_args.kwargs


# ═══════════════════════════════════════════════════════════════════════════
# 2. What is summed
# ═══════════════════════════════════════════════════════════════════════════


class TestRowsLoadedSummation:
    """The figure is `rows_written` summed over the rows whose write was confirmed."""

    def test_sums_confirmed_rows_written(self, metrics_module):
        """Three confirmed writes: the figure is their sum, not the row count.
        `Decimal` is the type DynamoDB returns for a number, so one row carries
        one."""
        outcome = _rows_loaded(metrics_module, _row(1200), _row(Decimal("300")), _row(7))

        assert_measured(outcome, 1200 + 300 + 7)

    def test_sum_spans_every_page_of_the_query(self, metrics_module):
        """`_query_state_by_step` paginates on `LastEvaluatedKey`; the sum covers
        every page, not just the first."""
        outcome, mock_state, _ = _call_rows_loaded(
            metrics_module,
            [
                {"Items": [_row(500)], "LastEvaluatedKey": {"run_id": "r-1", "step_name": "load_metrics"}},
                {"Items": [_row(250)]},
            ],
        )

        assert mock_state.query.call_count == 2
        assert mock_state.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {
            "run_id": "r-1",
            "step_name": "load_metrics",
        }
        assert_measured(outcome, 500 + 250)

    def test_an_empty_source_row_is_a_measured_zero(self, metrics_module):
        """The empty-source run writes `rows_written = 0, write_confirmed = false`.
        That is a run that loaded nothing, measured: the figure is 0, not absent."""
        outcome = _rows_loaded(metrics_module, _row(0, confirmed=False))

        assert_measured(outcome, 0)

    def test_several_zero_rows_still_sum_to_a_measured_zero(self, metrics_module):
        """A sum of zeros is a measured 0 — and not the row count (2)."""
        outcome = _rows_loaded(metrics_module, _row(0, confirmed=False), _row(0, confirmed=True))

        assert_measured(outcome, 0)

    def test_an_unconfirmed_non_zero_row_contributes_nothing_but_is_a_measurement(self, metrics_module):
        """`rows_written` without a confirmed write is rows handed to the writer,
        not rows loaded (p37 risk N1). It adds 0, yet the run was seen, so the
        figure is a measured 0 rather than absent."""
        outcome = _rows_loaded(metrics_module, _row(500, confirmed=False))

        assert_measured(outcome, 0)

    def test_an_unconfirmed_row_beside_a_confirmed_one_adds_nothing(self, metrics_module):
        outcome = _rows_loaded(metrics_module, _row(100, confirmed=True), _row(500, confirmed=False))

        assert_measured(outcome, 100)

    @pytest.mark.parametrize(
        "confirmed",
        [_OMIT, False, None, "true", Decimal("1")],
        ids=["absent", "false", "none", "string-true", "decimal-one"],
    )
    def test_only_a_boolean_true_confirms_the_write(self, metrics_module, confirmed):
        """The count is added only when `write_confirmed is True`. Any other value
        — missing, false, or merely truthy — leaves the row seen and adding 0."""
        outcome = _rows_loaded(metrics_module, _row(40), _row(900, confirmed=confirmed))

        assert_measured(outcome, 40)


class TestRowsLoadedUnusableCount:
    """A `rows_written` that is not a non-negative integer is not a count.

    Such a row is skipped entirely: it adds nothing, and it does not establish a
    measurement on its own.
    """

    _UNUSABLE = pytest.mark.parametrize(
        "rows_written",
        [_OMIT, None, True, False, -5, Decimal("-1"), "not-a-number", {"nested": "map"}, []],
        ids=["absent", "none", "bool-true", "bool-false", "negative", "negative-decimal", "string", "map", "list"],
    )

    @_UNUSABLE
    def test_an_unusable_count_beside_a_usable_one_is_skipped(self, metrics_module, rows_written):
        """The usable row still sums; the unusable one adds nothing and the call
        does not raise. A confirmed `True` would add 1 and a confirmed `-5` would
        subtract, so both are confirmed here to make a wrong acceptance visible."""
        outcome = _rows_loaded(metrics_module, _row(500), _row(rows_written, confirmed=True))

        assert_measured(outcome, 500)

    @_UNUSABLE
    def test_a_window_of_only_unusable_counts_is_absent(self, metrics_module, rows_written):
        """No row carries a usable count, so nothing was measured."""
        outcome = _rows_loaded(metrics_module, _row(rows_written), _row(rows_written, confirmed=False))

        assert_absent(outcome)


# ═══════════════════════════════════════════════════════════════════════════
# 3. The window
# ═══════════════════════════════════════════════════════════════════════════


class TestRowsLoadedWindow:
    """The client-side `_parse_iso` re-check is the authoritative window filter.

    `_query_state_by_step`'s docstring says so, for mixed-suffix timestamps that
    a string BETWEEN can admit. A row outside `[start, end]`, or with no
    parseable timestamp, neither adds to the sum nor establishes a measurement.
    """

    def test_rows_outside_the_window_contribute_nothing(self, metrics_module):
        outcome = _rows_loaded(
            metrics_module,
            _row(400),
            _row(999_999, ts=END + timedelta(seconds=1)),
            _row(888_888, ts=START - timedelta(seconds=1)),
            _row(777, ts="not-a-timestamp"),
            _row(666, ts=_OMIT),
        )

        assert_measured(outcome, 400)

    def test_both_bounds_are_inclusive(self, metrics_module):
        """`[start, end]`: a row stamped exactly at either bound is in the window."""
        outcome = _rows_loaded(metrics_module, _row(10, ts=START), _row(20, ts=END))

        assert_measured(outcome, 30)

    def test_a_z_suffixed_timestamp_is_read_as_utc(self, metrics_module):
        outcome = _rows_loaded(metrics_module, _row(15, ts="2025-03-01T06:00:00Z"))

        assert_measured(outcome, 15)

    def test_a_window_of_only_out_of_window_rows_is_absent(self, metrics_module):
        """Rows came back, counts exist, none is in the window: nothing measured."""
        outcome = _rows_loaded(
            metrics_module,
            _row(50, ts=END + timedelta(hours=1)),
            _row(0, confirmed=False, ts=START - timedelta(hours=1)),
        )

        assert_absent(outcome)


# ═══════════════════════════════════════════════════════════════════════════
# 4. The three outcomes
# ═══════════════════════════════════════════════════════════════════════════


class TestRowsLoadedOutcomes:
    """Measured, absent and incomplete are three different answers (`LGR-b2f79ef-8`)."""

    def test_no_rows_is_absent(self, metrics_module):
        """A successful Query over a window with no `load_metrics` row is an
        established nothing, not a zero."""
        outcome, _, mock_logger = _call_rows_loaded(metrics_module, [{"Items": []}])

        assert_absent(outcome)
        mock_logger.warning.assert_not_called()

    def test_a_raising_query_is_incomplete_and_logged_at_warning(self, metrics_module):
        """A failed read ("we could not look") is not an empty window ("we looked
        and there is nothing"). The figure is Not_Established, the token says the
        read failed, and the failure is logged at WARNING rather than raised."""
        outcome, _, mock_logger = _call_rows_loaded(metrics_module, side_effect=RuntimeError("DynamoDB unavailable"))

        assert outcome.value is None
        assert outcome.determined_from == INCOMPLETE_TOKEN
        mock_logger.warning.assert_called_once()
        message = str(mock_logger.warning.call_args.args[0])
        assert message.startswith("Rows-loaded query failed:"), message
        assert "DynamoDB unavailable" in message

    def test_a_query_failing_on_a_later_page_is_incomplete(self, metrics_module):
        """A partial read is not a measurement of the rows it did get."""
        outcome, _, mock_logger = _call_rows_loaded(
            metrics_module,
            [
                {"Items": [_row(500)], "LastEvaluatedKey": {"run_id": "r-1", "step_name": "load_metrics"}},
                RuntimeError("throttled on page 2"),
            ],
        )

        assert outcome.value is None
        assert outcome.determined_from == INCOMPLETE_TOKEN
        mock_logger.warning.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
