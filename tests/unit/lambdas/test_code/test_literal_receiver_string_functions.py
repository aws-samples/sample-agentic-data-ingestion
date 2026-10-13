# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The literal-receiver shapes that never reach `_cast` — p40 task 3.2.

Spec: `.kiro/specs/p40-pandas-proxy-literal-cast/`. Requirements **R2** and
**R8** (`design.md` §1); defect clauses `bugfix.md` 1.4 and 1.5; expected
clauses 2.4, 2.5 and the class-level 2.9; oracle rule 2.15.

WHAT THESE FIVE SHAPES ARE. `_function_call` appends a pandas member that
exists only on a `Series` to whatever the receiver translated to, and for
`lit(<const>)` that receiver is the constant's verbatim source text
(`pyspark_to_pandas.py:532-535`). Three emission sites, all read:

  * `trim` / `lower` / `upper` — the string branch, `:537-545`, member appended
    at `:544` (`.str.strip()`, `.str.lower()`, `.str.upper()`);
  * `regexp_replace` — `:549-556`, `.str.replace` at `:555`;
  * `concat_ws` — `:557-565`, `.astype(str)` per part at `:561`.

None of the three goes anywhere near `_cast` (`:652-669`). That is the whole
reason this file exists, and it is asserted rather than asserted-in-prose: the
refusal test below first checks that its own input contains no `.cast(` at all,
so the claim "these shapes never reach `_cast`" is machine-checked next to the
outcome it justifies.

WHY THIS FILE IS THE ONE THAT DECIDES WHERE THE FENCE LIVES. `design.md`
Appendix B rejects **Arm A alone** — broadcasting the literal inside `_cast` —
on exactly these five shapes: Arm A cannot pass a single assertion here, because
none of them reaches `_cast` to be broadcast. So this file is not extra coverage
of the defect; it is the evidence for the ratified arm's shape (Arm C as the
general fence, at the member-application sites, with Arm A narrow inside
`_cast`). It is also the test that would catch a future "simplification" that
moves the fence back into `_cast` — that change passes
`test_literal_receiver_cast.py` and fails every test here.

EXPECTED RED WINDOW — EVERY ASSERTION IN THIS FILE IS RED UNTIL TASK 4.2.
At the time of writing, all five shapes CONVERT SUCCESSFULLY and emit pandas
that raises at runtime (`bugfix.md` 1.4, 1.5, both *reproduced*), so the failure
mode is `_refusal`'s "the converter converted this" fail, carrying the emitted
text — not an exception out of `convert()`. The red is the point (task 3.4
records the counterexamples). Task 4.2 lands `_require_series` at the six guard
sites and every assertion here goes green; unlike
`test_literal_receiver_cast.py`, nothing in this file waits on task 5.1, because
no shape here has a converted outcome to wait for. **A shape that already
refuses is a finding, not a pass** — it means the working tree moved and
`design.md` §4.2's ratified line needs re-deriving before task 4 proceeds.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT — `concat_ws`'s column parts.
`concat_ws` hardcodes `.astype(str)` on **every** part (`:561`), including
`col(...)` ones. That is a separate, real fidelity divergence from Spark and it
is OUT OF SCOPE (`design.md` §5, N3a): out of scope means its emission stays
byte-identical, because it pins `test_converter_equivalence_baseline.py:414`
(`concat_ws-two-cols`) and `test_pyspark_to_pandas.py:241`, both of which task
14 checks for an empty diff. So the `concat_ws` case below asserts only that the
**literal** part refuses, and there is no assertion anywhere in this file that
requires a column part's emitted text to change — nor a local copy of those two
pins, which would transcribe current output into a file whose oracle is Spark
semantics and would give task 14 a third pin it does not know to check. The
"a column receiver must still convert" side is held generatively by task 12.2's
orthogonality property and by the baseline.

**Validates: Requirements R2, R8**
"""

# ══════════════════════════════════════════════════════════════════════
# ORACLE RULE FOR THIS FILE, AND IT IS THE OPPOSITE OF THE ONE NEXT DOOR.
#
# Expectations here come from **Spark semantics**, by way of the outcome
# table `design.md` §4.2 ratified. **The current converter's output must NOT
# be consulted** — `bugfix.md` 2.15, `LGR-28b4d75-5` (template / ratified).
# The reason is specific to this defect class rather than general caution: in
# every shape below the bug is a *successful* conversion, so a test written
# from current output would pin the bug as the contract and pass forever. The
# only place current output appears in this file is inside `_refusal`'s
# FAILURE message, where it is evidence that the defect exists, never an
# expectation.
#
# THE OPPOSITE RULE LIVES IN THIS SAME DIRECTORY. The p40 task-2.1 pin block
# at `test_converter_equivalence_baseline.py:1067` onwards — the eight
# `_CAST_TARGETS` keys nothing pinned before this spec — is a *preservation*
# pin block over a `col(...)` receiver, and there **current converter output
# IS the oracle**, transcribed byte-exactly. Its own header says so. Getting
# the two rules the wrong way round is the single most likely way to produce
# a plausible-looking, worthless test file, which is why both sides state
# which rule they follow.
#
# Sibling files under the same Spark-derived rule as this one:
# `test_literal_receiver_cast.py` (p40 task 3.1) and
# `test_literal_receiver_composition.py` (p40 task 3.3).
# ══════════════════════════════════════════════════════════════════════

import os
import sys

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter


def _refusal(source: str) -> ConversionError:
    """Return the `ConversionError` *source* raises, or fail naming the emission.

    Written this way rather than as `pytest.raises` so the red run carries the
    counterexample. Until task 4.2 lands, every input below CONVERTS, and what
    task 3.4 has to record is the pandas that conversion emitted — the text that
    raises inside the Code Interpreter. `pytest.raises` would report only
    `DID NOT RAISE`, which is the one fact about this defect that was never in
    doubt.
    """
    try:
        result = PySpark2PandasConverter.convert(source)
    except ConversionError as exc:
        return exc
    pytest.fail(
        "expected a ConversionError (design.md §4.2: refuse). The converter CONVERTED "
        "this shape instead, emitting pandas that applies a Series-only member to a "
        f"scalar:\n  input:  {source}\n  emitted: {result['code']!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# The five shapes, each paired with the construct that OWNS the member —
# `design.md` §4.1: the construct named is the one applying the member, so a
# reviewer is pointed at the operation rather than at the literal. Naming
# `lit` or `withColumn` here would point at code that is not the problem.
#
# Written as the PySpark the generation prompt actually emits (a `withColumn`
# assignment), which is the form `bugfix.md` 1.4/1.5 reproduced.
# ──────────────────────────────────────────────────────────────────────

LITERAL_RECEIVER_SHAPES = [
    pytest.param('df_transformed = df.withColumn("a", trim(lit("x")))', "trim", id="trim-of-literal"),
    pytest.param('df_transformed = df.withColumn("a", lower(lit("x")))', "lower", id="lower-of-literal"),
    pytest.param('df_transformed = df.withColumn("a", upper(lit("x")))', "upper", id="upper-of-literal"),
    pytest.param(
        'df_transformed = df.withColumn("a", regexp_replace(lit("x"), "x", "y"))',
        "regexp_replace",
        id="regexp_replace-of-literal",
    ),
    pytest.param(
        'df_transformed = df.withColumn("a", concat_ws("-", lit("x"), col("b")))',
        "concat_ws",
        id="concat_ws-literal-part",
    ),
]

#: Two lines that convert on their own, used to put a refusing statement at
#: line 3 of a block. Taken from the same shapes the baseline already pins, so
#: a failure in this file can only be about the statement under test.
CONVERTIBLE_PREAMBLE = (
    'df_transformed = df.withColumn("name", trim(col("name")))\n'
    'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")\n'
)


class TestSeriesOnlyMemberOnALiteralRefuses:
    """`bugfix.md` 2.4, 2.5 via 2.1's disjunction, and 2.9 as the class.

    Each shape either emits pandas that executes and agrees with Spark, or
    refuses naming the construct and the line. None of these five has a
    conversion that agrees with Spark — a `.str` accessor and an `.astype` on a
    Python `str` raise (`design.md` §5, F9, *measured*) — so the disjunct left
    is the refusal.

    **Validates: Requirements R2, R8**
    """

    @pytest.mark.parametrize(("source", "construct"), LITERAL_RECEIVER_SHAPES)
    def test_refuses_rather_than_emitting_a_member_a_scalar_does_not_have(self, source: str, construct: str):
        """The shape refuses — and the input contains no `.cast(`, which is the
        machine-checked half of "these never reach `_cast`" (`design.md`
        Appendix B's rejected Arm A). A fence living inside `_cast` cannot
        change the outcome of this test, in either direction."""
        assert ".cast(" not in source, "this file's shapes must not route through _cast"
        _refusal(source)

    @pytest.mark.parametrize(("source", "construct"), LITERAL_RECEIVER_SHAPES)
    def test_refusal_names_the_construct_that_owns_the_member(self, source: str, construct: str):
        """`bugfix.md` 3.4: the refusal names the construct. `design.md` §4.1
        fixes which one — the construct applying the Series-only member, not the
        `lit` that produced the scalar and not the `withColumn` around it."""
        assert _refusal(source).construct == construct

    @pytest.mark.parametrize(("source", "construct"), LITERAL_RECEIVER_SHAPES)
    def test_refusal_carries_the_offending_line_and_its_number(self, source: str, construct: str):
        """`bugfix.md` 3.4: the offending line and the line number travel with
        the error, and both are populated. `code_interpreter_runner.py:84,:385`
        puts them in front of a human, so an unpopulated `line_number` is a
        refusal that reads as `on line None` — the failure mode
        `LGR-b2f79ef-12` gave `not_verified_by_harness` its own reason to
        avoid."""
        error = _refusal(source)
        assert error.line == source.strip()
        assert error.line_number == 1
        assert construct in str(error)
        assert "line 1" in str(error)

    @pytest.mark.parametrize(("source", "construct"), LITERAL_RECEIVER_SHAPES)
    def test_refusal_is_all_or_nothing_and_reports_the_offending_statement(self, source: str, construct: str):
        """`bugfix.md` 3.4/3.8: two convertible lines ahead of the shape do not
        rescue the block, and the number reported is the refusing statement's
        own position — not the first line, and not the last. A partial
        conversion would send the Code Interpreter pandas that is quietly
        missing a transform."""
        error = _refusal(CONVERTIBLE_PREAMBLE + source)
        assert error.construct == construct
        assert error.line_number == 3
        assert error.line == source.strip()
