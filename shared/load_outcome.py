# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Load_Outcome — what reached the target on one run (p37 R1.2-R1.6).

This module answers one question about a run: **how much of what was read
reached the target, and is that knowable at all?** It is pure — no AWS clients,
no I/O — so the answer is a function of the two counts the generated Glue job
records for the run, plus the provenance of those two counts.

Why this exists at all
----------------------
A run reported ``SUCCEEDED`` — no ``TaskFailed`` or ``ExecutionFailed`` event
anywhere in its Step Functions history — while writing **zero rows** to its
Iceberg target, because a data quality rule quarantined every row. The
quarantine behaved correctly and the approval gate behaved as designed. The
defect was that nothing downstream noticed the load produced nothing: run
status is derived from Glue state, and until p37 R1.1 no state row recorded
rows written at all (``collect_job_metrics`` returns timing, worker, DPU, cost
and Glue version and no row counts — ``LGR-453f12d-2``, **template /
ratified**, gives "the generated job records rows written" as its own revisit
condition). This record is what makes a zero-row load visibly different from a
load that landed the data.

What it is NOT for
------------------
It is **not** the Quality_Verdict. :mod:`shared.quality_verdict` answers *did
anything gate the write, and what did it withhold* — a statement about the
**generated code**. This answers *what reached the target* — a statement about
the **run**. The two are siblings and stay separate: ``LGR-eee8ac8-6``
(**template / ratified**) is the record for that separation, and its
``revisit_when`` ("enforcement is actually implemented") is what licenses this
module to exist beside it rather than as a fifth branch inside it.

It also does **not** re-decide the run. See ``blocks_run`` below.

Counts only. The (source_type, target_type) pair is deliberately not consulted
-----------------------------------------------------------------------------
``LGR-eee8ac8-6`` (**template / ratified**) rejected re-deriving that pair in a
new module as something that "duplicates ``TEMPLATE_RULES`` in a module free to
drift and answers the full-AI fallback path wrongly". Its sibling therefore
keys on the rendered template; this module keys on **neither**, because the
counts are written from all five PySpark templates and the arithmetic is the
same whichever one produced them. Template-agnosticism is what makes p37 R1.11
true — the outcome exists for every combination the system generates code for —
without a second rules table to keep in step.

``blocks_run`` is always ``False``, and it is recorded rather than omitted
-------------------------------------------------------------------------
By the time any of this is knowable, ``job_completion`` has already called
``send_task_success`` (``job_completion/lambda_function.py:765-771``), so there
is no decision left to make: converting a ``no_rows_loaded`` verdict into a task
failure would mean sending a second terminal signal on a consumed token. That is
p37 R1.12 and do-NOT 4. The field is present in every arm because
``no_rows_loaded`` invites exactly the opposite reading — the same reason
``derive_quality_verdict`` states it explicitly for ``enforced``.

``empty_source`` is deliberately NOT a subcase of ``no_rows_loaded``
-------------------------------------------------------------------
An empty source file is a legitimate no-op, not a data-loss event.
``glue_job_jdbc_source.py.j2`` has a deliberate empty-source early exit whose
double-commit was already fixed once (``LGR-334667e-1``, **backfilled**).
Collapsing the two outcomes would make every empty scheduled JDBC poll a
warning, which is the cry-wolf failure p37 R1.2 exists to avoid from the other
side.

Total, and it never raises
--------------------------
Every input reaches exactly one of the five outcomes. Absent, wrongly typed,
negative and mutually inconsistent counts all reach ``not_established`` with
:data:`OUTCOME_KEY_MISSING` naming what was wrong; ``rows_not_written`` is never
negative. A provenance annotation must never be the reason a dashboard page
500s — the same contract :func:`shared.quality_verdict.count_dqdl_rules` states
for itself.

Two shapes of unavailability, kept distinct on purpose
-----------------------------------------------------
* A **fact** that is absent or unusable names itself — ``["rows_read"]``,
  ``["rows_written"]``, or both. p37 R1.6 requires recording *which* fact was
  unavailable, and ``LGR-78ce0b7-8`` (**template / ratified**) settles the same
  question one layer down: a missing record and a record with nothing in it must
  not collapse into one value, because "a run reported as unverifiable when the
  real problem is a missing record" is the prohibited outcome.
* A **pair** that cannot describe a load — a negative count, or more rows
  written than read — reports :data:`MISSING_CONSISTENT_COUNTS`. Both counts
  arrived; what failed is the relationship between them, so naming one of them
  would be arbitrary.

``determined_from`` carries the provenance of the two counts, with the same
three-value shape ``derive_quality_verdict`` established
(``quality_verdict.py:103-105``): the row was read, the row was absent, or the
row was there and incomplete. A fourth value,
:data:`FROM_DQ_REPORT_ROW_LOWER_BOUND`, is described below. Anything other than
those two of the four that can establish anything yields ``not_established``:
provenance saying the counts did not come from the load-metrics row means they
are not established, whatever values arrived alongside it.

The fourth provenance, and the one outcome it can reach (p37 C5)
---------------------------------------------------------------
Runs that completed before p37 R1.1 shipped carry no ``load_metrics`` row and
will read ``not_established`` for ever. For one family of them the answer is
recoverable from a row that already exists: the DQ_Report_Row of an
inline-DQ ``s3_tables`` run carries ``rows_passed``, which **is** Rows_Written
(``glue_job_s3_tables.py.j2:307`` writes ``transformed_count`` under that name).

Rows_Read is **not** recoverable from that row. Both the transform filter and
the quarantine reduce the frame and neither pre-transform count is written, so
``R1.4`` and ``R1.5`` cannot be told apart on this path and
:data:`OUTCOME_COMPLETE_LOAD` is **unreachable** — enforced by a guard in the
function, not by a caller's promise.

One combination does settle: ``rows_passed == 0`` with ``rows_quarantined > 0``.
The quarantined rows came from the source, so Rows_Read > 0 **follows** and
``no_rows_loaded`` is established rather than guessed. Every other combination
under this provenance is ``not_established`` naming ``rows_read`` as the fact
that was unavailable. ``rows_read`` on the established arm carries the
quarantine count, which is a **lower bound** — the true figure can only be
higher — and the ``reason`` string says so out loud, because ``R1.6``'s honesty
requirement applies to this path most of all.

Layer 0 in ``shared/__init__._LAYER_MAP`` — no shared-module dependencies.
"""

from typing import Any

# ── The five outcomes (p37 R1.2-R1.6, design.md C2) ──────────────────────
#: Every row that was read reached the target (R1.5).
OUTCOME_COMPLETE_LOAD = "complete_load"
#: Some rows reached the target and some did not (R1.4).
OUTCOME_PARTIAL_LOAD = "partial_load"
#: The source had rows and none of them reached the target (R1.2).
OUTCOME_NO_ROWS_LOADED = "no_rows_loaded"
#: The source had no rows, so writing none is a legitimate no-op (R1.3).
OUTCOME_EMPTY_SOURCE = "empty_source"
#: At least one of the two counts is not available, so no claim is made (R1.6).
OUTCOME_NOT_ESTABLISHED = "not_established"

# ── ``determined_from`` values — provenance of the two counts ─────────────
#: Both counts were read off the run's ``(run_id, "load_metrics")`` state row.
FROM_LOAD_METRICS_ROW = "load_metrics_row"
#: No ``load_metrics`` row exists for the run — the job never wrote one, or the
#: run predates the counts being recorded at all.
FROM_LOAD_METRICS_ROW_ABSENT = "load_metrics_row_absent"
#: The row exists but does not carry both counts.
FROM_LOAD_METRICS_ROW_INCOMPLETE = "load_metrics_row_incomplete"
#: There is no ``load_metrics`` row, and the two counts were taken off the run's
#: DQ_Report_Row instead (p37 C5): ``rows_written`` is that row's ``rows_passed``
#: and ``rows_read`` is a **lower bound** derived from ``rows_quarantined``, not a
#: recorded count. Only :data:`OUTCOME_NO_ROWS_LOADED` is reachable under this
#: provenance — see the module docstring for why, and for the guard that enforces
#: it.
FROM_DQ_REPORT_ROW_LOWER_BOUND = "dq_report_row_lower_bound"

#: The provenances under which a count can establish anything. The two
#: ``load_metrics_row_*`` values below say the counts did **not** come off the
#: row, so they belong outside this set alongside unrecognised and wrongly-typed
#: input: provenance wins over content.
_ESTABLISHING_PROVENANCE = frozenset({FROM_LOAD_METRICS_ROW, FROM_DQ_REPORT_ROW_LOWER_BOUND})

# ── ``missing`` entries — which fact, or which relationship, failed ───────
#: ``rows_read`` was absent or not a usable count.
MISSING_ROWS_READ = "rows_read"
#: ``rows_written`` was absent or not a usable count.
MISSING_ROWS_WRITTEN = "rows_written"
#: Both counts arrived but cannot describe a load: one is negative, or more rows
#: were written than were read.
MISSING_CONSISTENT_COUNTS = "consistent_counts"

#: Name of the returned key that carries the ``missing`` list. Exported so a
#: caller can read it without spelling the string a second time.
OUTCOME_KEY_MISSING = "missing"


def _usable_count(value: Any) -> bool:
    """Return True when ``value`` can be read as a row count.

    ``bool`` is rejected even though it is a subclass of ``int``: ``True`` is not
    "one row", it is a flag that arrived in a numeric field, and silently reading
    it as 1 would invent a count. Floats, strings, ``Decimal`` and ``None`` are
    all rejected for the same reason — the fact is not available in a usable
    form, which p37 R1.6 requires be reported rather than guessed at.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _as_provenance(determined_from: Any) -> str:
    """Coerce ``determined_from`` to a string without raising.

    A non-string provenance is not :data:`FROM_LOAD_METRICS_ROW`, so it routes to
    ``not_established`` on its own; it is flattened to ``""`` here so the returned
    dict stays strings-and-ints and no arbitrary object's ``__str__`` runs inside
    this function.
    """
    return determined_from if isinstance(determined_from, str) else ""


def derive_load_outcome(
    *,
    rows_read: int | None,
    rows_written: int | None,
    determined_from: str = FROM_LOAD_METRICS_ROW,
) -> dict[str, Any]:
    """Return the Load_Outcome record for one run (p37 R1.2-R1.6).

    Args:
        rows_read: Rows the generated job read from the source — ``source_count``
            in every PySpark template. ``None`` means the fact was unavailable.
        rows_written: Rows the generated job presented to the target writer —
            ``transformed_count`` in every template. ``None`` means the fact was
            unavailable. Note the wording: this is rows handed to the writer, not
            rows confirmed written, which is why the state row carries its own
            ``write_confirmed`` stamp (design.md risk N1).
        determined_from: Provenance of the two counts above; one of
            :data:`FROM_LOAD_METRICS_ROW`,
            :data:`FROM_LOAD_METRICS_ROW_ABSENT`,
            :data:`FROM_LOAD_METRICS_ROW_INCOMPLETE` or
            :data:`FROM_DQ_REPORT_ROW_LOWER_BOUND`. Anything else — including a
            value of the wrong type — yields ``not_established``. Under
            :data:`FROM_DQ_REPORT_ROW_LOWER_BOUND` only ``no_rows_loaded`` is
            reachable; ``rows_read`` is then a lower bound, not a count.

    Returns:
        A flat dict of ints, strings and one list of strings. Keys, in every arm:

        ``outcome``
            One of the five ``OUTCOME_*`` values.
        ``rows_read`` / ``rows_written``
            The two counts as established, or ``None`` for one that was not.
            They are **not** coerced to 0 there: 0 is a meaningful count in this
            vocabulary — it is what ``empty_source`` and ``no_rows_loaded`` are
            made of — so reporting it for a count that was never established
            would contradict ``missing`` inside the same record. Under
            :data:`FROM_DQ_REPORT_ROW_LOWER_BOUND` ``rows_read`` is a **lower
            bound** rather than a count; ``determined_from`` and ``reason`` both
            say so, and a reader that presents it as exact understates it.
        ``rows_not_written``
            ``rows_read - rows_written`` when both are established, else 0. Never
            negative (p37 R1.4, and Property 1's third clause).
        ``blocks_run``
            Always ``False``. See the module docstring: p37 R1.12 and do-NOT 4.
        ``missing``
            The facts or the relationship that failed, using the ``MISSING_*``
            values. Empty on the four established outcomes.
        ``determined_from``
            Echoed provenance.
        ``reason``
            One sentence a dashboard can show, stating what is claimed and what
            is not.

    This function is total and never raises. See the module docstring.
    """
    provenance = _as_provenance(determined_from)
    # ``None`` from here on means "not available in a usable form", which folds
    # absent, wrongly typed and boolean inputs into one state without inventing a
    # count for any of them.
    read = rows_read if _usable_count(rows_read) else None
    written = rows_written if _usable_count(rows_written) else None

    def _record(
        outcome: str,
        *,
        reason: str,
        missing: list[str] | None = None,
        read_count: int | None = None,
        written_count: int | None = None,
        not_written: int = 0,
    ) -> dict[str, Any]:
        # One assembly point for all five arms, so ``blocks_run`` cannot be
        # omitted from one of them by editing a branch — the same reason
        # ``derive_quality_verdict`` returns from a single site.
        return {
            "outcome": outcome,
            "rows_read": read_count,
            "rows_written": written_count,
            "rows_not_written": not_written,
            # Always False. A Load_Outcome describes a run; it never re-decides
            # one, and by the time it is knowable the task token is consumed
            # (job_completion/lambda_function.py:765-771). Recorded explicitly
            # because "no_rows_loaded" reads as if it stops something.
            "blocks_run": False,
            OUTCOME_KEY_MISSING: missing if missing is not None else [],
            "determined_from": provenance,
            "reason": reason,
        }

    # ── not_established, arm 5 (R1.6) ────────────────────────────────────
    absent = [name for name, value in ((MISSING_ROWS_READ, read), (MISSING_ROWS_WRITTEN, written)) if value is None]
    # p37 C5. Kept as one local so every arm below reads the same answer, and so
    # the guard before the four established arms is the single place this
    # provenance changes the outcome.
    lower_bound = provenance == FROM_DQ_REPORT_ROW_LOWER_BOUND
    row_label = "data quality report row" if lower_bound else "load-metrics row"

    if provenance not in _ESTABLISHING_PROVENANCE:
        return _record(
            OUTCOME_NOT_ESTABLISHED,
            # Provenance says these counts did not come from the load-metrics
            # row, so neither fact is established even if a number arrived.
            missing=absent or [MISSING_ROWS_READ, MISSING_ROWS_WRITTEN],
            reason=(
                "How many rows this run read and wrote was not established "
                f"({provenance or 'unrecognised provenance'}), so what reached the target "
                "cannot be stated. This is not a claim that nothing loaded."
            ),
        )

    if read is None or written is None:
        return _record(
            OUTCOME_NOT_ESTABLISHED,
            missing=absent,
            read_count=read,
            written_count=written,
            reason=(
                f"This run's {row_label} carries no usable {' and '.join(absent)}, so what "
                "reached the target cannot be stated. This is not a claim that nothing loaded."
            ),
        )

    # ── The lower-bound path reaches exactly one outcome (p37 C5) ────────
    # ``complete_load`` is unreachable from a DQ_Report_Row and this is the guard
    # that makes that structural rather than a promise the caller keeps. With
    # ``rows_read`` set to the quarantine count, ``rows_passed == rows_quarantined``
    # would otherwise satisfy R1.5's equality arm and produce a confident
    # ``complete_load`` out of two numbers that cannot support one.
    #
    # Deliberately BEFORE the inconsistent-counts guard below. On this path
    # ``written > read`` means "more rows passed the quality check than were
    # quarantined", which is an ordinary successful run rather than an impossible
    # pair — reporting it as counts that "cannot describe a load" would be a
    # false accusation. What is actually unavailable is Rows_Read, and this arm
    # says that instead.
    if lower_bound and not (written == 0 and read > 0):
        return _record(
            OUTCOME_NOT_ESTABLISHED,
            # ``rows_written`` did arrive — ``rows_passed`` IS Rows_Written. What
            # is unavailable is Rows_Read, and naming it is R1.6's requirement.
            missing=[MISSING_ROWS_READ],
            written_count=written,
            reason=(
                f"This run has no recorded row counts. Its data quality report shows {written} "
                f"row(s) passing the quality check and {read} quarantined, which puts a lower "
                "bound on how many rows were read but does not establish it, so what reached "
                "the target cannot be stated."
            ),
        )

    if read < 0 or written < 0 or written > read:
        return _record(
            OUTCOME_NOT_ESTABLISHED,
            missing=[MISSING_CONSISTENT_COUNTS],
            reason=(
                f"This run's recorded counts cannot describe a load (read {read}, "
                f"wrote {written}), so what reached the target cannot be stated."
            ),
        )

    # ── The four established outcomes ────────────────────────────────────
    # Both counts are usable, non-negative ints with ``written <= read``, so this
    # subtraction is never negative and the four arms below are exhaustive.
    rows_not_written = read - written

    if written == 0 and read > 0:
        # R1.2. Ordered before complete_load so the zero-and-zero case cannot
        # fall into it, and stated separately from empty_source on purpose.
        return _record(
            OUTCOME_NO_ROWS_LOADED,
            read_count=read,
            written_count=written,
            not_written=rows_not_written,
            reason=(
                (
                    f"This run wrote no rows to the target. At least {read} row(s) were read — "
                    f"that is a lower bound, taken from the {read} row(s) this run's data quality "
                    "report quarantined, not a recorded count, so the true number read may be "
                    "higher. Every row that was read was withheld."
                )
                if lower_bound
                else (
                    f"This run read {read} row(s) and wrote none to the target. Every row was "
                    "withheld — quarantined by a data quality rule, or dropped by the transform."
                )
            ),
        )

    if written == 0:
        # R1.3, reached only when ``read == 0`` as well. An empty source is a
        # legitimate no-op, not a data-loss event.
        return _record(
            OUTCOME_EMPTY_SOURCE,
            read_count=read,
            written_count=written,
            reason=(
                "This run's source had no rows, so writing none to the target is the correct "
                "outcome rather than a loss."
            ),
        )

    if written < read:
        # R1.4. The count is always carried; per design.md D1 it is displayed
        # rather than turned into a non-success status, and there is no threshold.
        return _record(
            OUTCOME_PARTIAL_LOAD,
            read_count=read,
            written_count=written,
            not_written=rows_not_written,
            reason=(
                f"This run read {read} row(s) and wrote {written} to the target; "
                f"{rows_not_written} row(s) were not written."
            ),
        )

    # R1.5 — ``written == read``, both greater than zero.
    return _record(
        OUTCOME_COMPLETE_LOAD,
        read_count=read,
        written_count=written,
        reason=(
            f"This run read {read} row(s) and wrote all of them to the target. Note that this "
            "counts rows presented to the target writer, not rows re-read from the target."
        ),
    )
