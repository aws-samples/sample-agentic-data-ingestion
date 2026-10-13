# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test for shared/load_outcome.py (p37 C2, task 5.2).

# Feature: p37-zero-row-success, Property 1: The deriver is total and its outcomes partition the input space

For any pair ``(rows_read, rows_written)`` drawn over integers **including
negatives and ``None``**, :func:`derive_load_outcome` returns exactly one of the
five outcomes, never raises, and never reports a negative ``rows_not_written``.

**Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.6**

Why this is a property and not five examples
--------------------------------------------
The five named boundaries and the three ``determined_from`` values are unit
tests (task 5.3). This test covers the *space*: it is the assertion that no
input — absent, negative, inverted, wrongly typed, or absurdly large — escapes
the five arms or reaches one of them carrying an impossible count. Totality is
the claim ``pipelines_runs.py`` will rely on when it calls this on the read
path, where a raised exception is a 500 on a page that was only annotating a
run.

The three clauses, and what each would catch
--------------------------------------------
(a) **Exactly one of the five outcomes.** Membership is checked against a set
    built from the module's **own** constants — no outcome string is spelled out
    anywhere in this file — so a renamed constant fails here rather than passing
    against a stale literal. The set's length is asserted too: if two constants
    ever collapsed to the same string the outcomes would no longer partition
    anything, and "exactly one" would be silently satisfied by an alias.

(b) **Never raises.** The call below sits outside any ``pytest.raises``, so any
    exception from any input is a failure of this test. That is the whole
    mechanism; there is nothing to assert.

(c) **``rows_not_written`` is never negative.** The one field derived by
    arithmetic rather than by branch, and the one that a reordering of the
    inverted-counts guard would corrupt into a negative "rows withheld" count
    shown to an operator.

Ledger status, recorded as a gap rather than an all-clear
--------------------------------------------------------
``why`` on ``shared/load_outcome.py`` returns **nothing** — the file is new in
task 5.1 and has no history yet. ``why`` on its sibling
``shared/quality_verdict.py`` returns ``LGR-eee8ac8-1..7``, of which
``LGR-eee8ac8-6`` (**template / ratified**) is the on-point one: it ratifies the
separation this module depends on and its ``revisit_when`` names this spec.
``decisions(concept="testing", include_backfilled=True)`` returns nothing that
governs a pure deriver's test shape — task 5.1 established that and it still
holds. ``decisions(concept="error-handling")`` returns two records that are the
same decision under two commit hashes (``LGR-78ce0b7-13`` /
``LGR-b2f79ef-13``), and that record's own body states the concept is empty
despite being in the documented vocabulary: a **confirmed gap**, not a licence.
``LGR-78ce0b7-8`` (**template / ratified**) is the record behind the arm this
property leans on hardest — a missing fact and an empty one must not collapse
into one value.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

# Imported after the sys.path shim above, per this tree's convention for shared/
# modules (ruff's E402 is globally ignored in pyproject.toml for exactly this).
from load_outcome import (
    FROM_DQ_REPORT_ROW_LOWER_BOUND,
    FROM_LOAD_METRICS_ROW,
    FROM_LOAD_METRICS_ROW_ABSENT,
    FROM_LOAD_METRICS_ROW_INCOMPLETE,
    OUTCOME_COMPLETE_LOAD,
    OUTCOME_EMPTY_SOURCE,
    OUTCOME_NO_ROWS_LOADED,
    OUTCOME_NOT_ESTABLISHED,
    OUTCOME_PARTIAL_LOAD,
    derive_load_outcome,
)

# The partition, built from the module's own constants. Deliberately not five
# hand-typed strings: a constant renamed in load_outcome.py must fail clause (a)
# rather than pass against a literal this file froze.
FIVE_OUTCOMES = frozenset(
    {
        OUTCOME_COMPLETE_LOAD,
        OUTCOME_PARTIAL_LOAD,
        OUTCOME_NO_ROWS_LOADED,
        OUTCOME_EMPTY_SOURCE,
        OUTCOME_NOT_ESTABLISHED,
    }
)

# ── Strategies ────────────────────────────────────────────────────────────
# Counts. Three branches rather than one, because the branch structure under
# test lives in a narrow neighbourhood of zero (0 vs > 0, written == read vs
# written < read vs written > read) while totality is a claim about the whole
# integer line. `st.integers()` alone would spend most of its budget far from
# every boundary; the bounded band raises the density where the arms are.
#
# `st.booleans()` is included on purpose. `bool` is a subclass of `int`, so an
# "integers" draw in Python legitimately contains True and False, and
# load_outcome._usable_count makes an explicit judgement about them — a flag in
# a numeric field is not a count of one row. Drawing them here is what makes
# that judgement load-bearing rather than incidental.
COUNTS = st.one_of(
    st.none(),
    st.integers(min_value=-4, max_value=4),
    st.integers(),
    st.booleans(),
)

# Provenance. The three documented values, plus junk — the empty string, a
# near-miss in the wrong case, arbitrary text, and a wrong type. Task 5.1 routes
# any non-`load_metrics_row` provenance to `not_established`, so junk is not an
# edge case here: it is a whole half of the input space, and it must reach an
# outcome rather than a `TypeError` on someone else's `__str__`.
#
# ``FROM_DQ_REPORT_ROW_LOWER_BOUND`` is drawn here too (p37 C5, task 12.1). It is
# a fourth documented value, so leaving it out would quietly shrink the space
# this property claims to cover — and it is drawn with the same unrestricted
# counts as the others, including the inverted and negative pairs its own guard
# has to survive. The truth table for that path is task 12.2's; totality over it
# is this test's.
PROVENANCE = st.one_of(
    st.sampled_from(
        (
            FROM_LOAD_METRICS_ROW,
            FROM_LOAD_METRICS_ROW_ABSENT,
            FROM_LOAD_METRICS_ROW_INCOMPLETE,
            FROM_DQ_REPORT_ROW_LOWER_BOUND,
        )
    ),
    st.just(""),
    st.just(FROM_LOAD_METRICS_ROW.upper()),
    st.text(max_size=12),
    st.none(),
    st.integers(),
)


@given(rows_read=COUNTS, rows_written=COUNTS, determined_from=PROVENANCE)
@settings(max_examples=300)
def test_deriver_is_total_and_outcomes_partition_the_input_space(
    rows_read: Any,
    rows_written: Any,
    determined_from: Any,
) -> None:
    """Property 1: The deriver is total and its outcomes partition the input space.

    # Feature: p37-zero-row-success, Property 1: The deriver is total and its outcomes partition the input space

    **Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.6**
    """
    # Clause (b): never raises. No pytest.raises anywhere near this call — any
    # exception, from any input above, fails the test here.
    result = derive_load_outcome(
        rows_read=rows_read,
        rows_written=rows_written,
        determined_from=determined_from,
    )

    # Clause (a): exactly one of the five, and five means five.
    assert len(FIVE_OUTCOMES) == 5, (
        "Two OUTCOME_* constants share a value, so the five outcomes no longer partition anything: "
        f"{sorted(FIVE_OUTCOMES)}"
    )
    matched = [outcome for outcome in FIVE_OUTCOMES if result["outcome"] == outcome]
    assert len(matched) == 1, (
        f"derive_load_outcome returned outcome={result['outcome']!r} for "
        f"rows_read={rows_read!r}, rows_written={rows_written!r}, "
        f"determined_from={determined_from!r} — matched {len(matched)} of the five "
        f"module constants, expected exactly 1"
    )

    # Clause (c): rows_not_written is never negative. A negative here would be
    # an operator being shown a negative count of withheld rows.
    assert result["rows_not_written"] >= 0, (
        f"derive_load_outcome reported rows_not_written={result['rows_not_written']!r} for "
        f"rows_read={rows_read!r}, rows_written={rows_written!r}, "
        f"determined_from={determined_from!r} — must never be negative (R1.4)"
    )
