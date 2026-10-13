# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p40 task 3.3 — the COMPOSED literal-receiver shapes, and the datetime pair.

Spec: `.kiro/specs/p40-pandas-proxy-literal-cast/` — R1, R2, R8. Discharges
`bugfix.md` defect clauses **1.6** (a broken receiver carried into a
surrounding expression that converts) and **1.8** (`to_date` / `to_timestamp`
on a literal), against expected clauses **2.6** and **2.8**, and the outcome
table `design.md` §4.2 ratified.

ORACLE RULE FOR THIS FILE: **expectations come from SPARK SEMANTICS. The
current converter's output must NOT be consulted.**
`bugfix.md` 2.15 and `LGR-28b4d75-5` (template / ratified). Every shape below
converts *successfully* today and emits pandas that raises at runtime, so a
test transcribed from current output would pin the bug instead of catching it.
Each expectation here is either (a) `ConversionError` naming the construct and
the line — 2.1's safe disjunct, per §4.2's row for that shape — or (b) Arm A's
broadcast emission from `design.md` §4.3.

**THE OPPOSITE RULE LIVES NEXT DOOR, IN THIS SAME DIRECTORY.** The p40 task
2.1 pin block in `test_converter_equivalence_baseline.py` (search for
`p40 task 2.1 — preservation pins`, and `P40_UNPINNED_CAST_KEY_PINS`) is a set
of *preservation* pins over a `col(...)` receiver, where **current converter
output IS the oracle** and every expectation was transcribed from `convert()`
byte-exactly. Both rules are correct, for opposite jobs. Getting them the
wrong way round is the single most likely way to produce a plausible-looking,
worthless test file, which is why each side names its own rule.

EXPECTED RED WINDOW — TWO STAGES, AND THE RED IS THE POINT (tasks.md task 3).
This file is written BEFORE any production line moves and is *meant* to fail.
`make test-fast` passing at HEAD is not evidence for this fix: `bugfix.md`
1.14 established that no existing test fails on this bug.

* **Closes at task 4.2** (the fence — `_is_scalar_emission` / `_require_series`
  at the six member-application sites): every refusal assertion here, i.e.
  `lit(0).cast("int").alias("z")`, the same shape inside `select`, and
  `to_date(lit("2020-01-01"), fmt)`.
* **Stays RED until task 5.1** (Arm A — `_CAST_TARGETS`' two string keys and
  `_cast`'s literal broadcast): every converted / composing assertion, i.e.
  `lit(None).cast("string").alias("z")`, the same inside `select`, and
  `trim(lit(None).cast("string"))`.
* **GREEN from the start:** all of `TestToTimestampOnALiteralPassesThroughUnchanged`,
  and the two `_select_output_column` resolver checks. (The one test that
  straddles the pair, `TestTheDatetimePairDisagreesDeliberately`, is red until
  4.2 on its `to_date` leg only — which is why it is in a class of its own,
  so each of the other two has exactly one expected colour.)
  `to_timestamp` appends no member
  (`_to_datetime`'s ternary, `pyspark_to_pandas.py:603-613`, `.dt.date` only at
  `:613`) and `F5` measured the resulting `Timestamp` broadcasting into a
  datetime column in agreement with Spark — so it must keep passing through
  unchanged. **If the `to_timestamp` half is red, that is a finding, not a
  test to adjust.**

Do NOT mark anything here `xfail` or `skip`: task 3.4 runs this file red on
purpose and records the counterexamples as the evidence that the bug exists.

WHY BOTH HALVES OF 1.8 LIVE IN ONE FILE. `bugfix.md` 1.8 suspected
`to_date` and `to_timestamp` might behave differently; `F5` (design §5,
`verification-notes.md`) measured that they do — `.dt` on a `Timestamp` raises
on both pandas majors, and the member-free `to_timestamp` emission executes and
agrees with Spark. The mechanical rule of §4.1 gets both right with **no
special case**. Asserting the pair together is what stops a later
"consistency" change from quietly making `to_timestamp` refuse too, which
would be a regression dressed as tidying.

No pandas here, deliberately: these are assertions about the **emitted text**,
which is where this defect lives (design §5 N2). pandas is not installed in
`agentic_data_ingestion/.venv` and must not be — the execution half of the
fix-check property is task 13.1's `pytest.importorskip("pandas")` test, which
skips in this environment.

**Validates: Requirements R1, R2, R8**
"""

import os
import sys

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter, _select_output_column


def convert(code: str) -> dict:
    return PySpark2PandasConverter.convert(code)


# ──────────────────────────────────────────────────────────────────────
# Arm A's emission, spelled once (design.md §4.3).
#
#   pd.Series(<const>, index=<frame>.index, dtype="string")
#
# `<frame>` is the `frame` argument `_expr` already threads — the enclosing
# statement's SOURCE frame (3.1/3.2), which under `_with_column_chain`
# (`pyspark_to_pandas.py:791`) is the frame the target is a `.copy()` of
# (`:805`), so the broadcast index and the target's index are identical.
# The nullable `"string"` dtype rather than `str` is MEASURED, not preferred:
# `F6`/`F10` — `astype(str)` writes the literal string "None" into null cells
# and reports zero nulls on pandas 2.2.3, while `astype("string")` yields
# `<NA>` on both majors. Spark's `lit(None).cast("string")` column is null in
# every row, so `"string"` is the only spelling that agrees with Spark
# regardless of the interpreter's pandas version (R7).
# ──────────────────────────────────────────────────────────────────────

BROADCAST_NULL_STRING = 'pd.Series(None, index=df.index, dtype="string")'


# ──────────────────────────────────────────────────────────────────────
# 1.6 → 2.6, first half: a REFUSED inner shape refuses the OUTER expression.
# ──────────────────────────────────────────────────────────────────────

# `lit(0).cast("int")` is a numeric target on a literal, which §4.2 ratified as
# a REFUSAL: the `.astype`-suffixed half raises on both pandas majors (`F1`
# `AttributeError`, `F2` `TypeError: data type 'Int64' not understood`), and the
# suffix-free half executes but yields an `int64` column where Spark yields a
# double (`F4`). Composed under `.alias` and under `select`, the refusal must
# happen at the INNER construct, so the outer construct never sees a receiver
# that cannot carry the member it is about to append.
REFUSED_INNER_COMPOSITIONS = [
    pytest.param(
        'aliased = lit(0).cast("int").alias("z")',
        "alias",
        id="alias-over-refused-numeric-cast",
    ),
    pytest.param(
        'df_out = df.select(col("a"), lit(0).cast("int").alias("z"))',
        "select",
        id="select-over-refused-numeric-cast",
    ),
]


class TestRefusedInnerShapeRefusesTheWholeExpression:
    """2.6: a broken receiver is never carried into a surrounding expression
    that converts successfully.

    Today both of these convert and emit
    `pd.to_numeric(0, errors="coerce").astype("Int64").rename("z")` — the
    outer construct composing over a receiver that raises. That is 1.6, and it
    is why these assertions are red until task 4.2.

    **Validates: Requirements R2**
    """

    @pytest.mark.parametrize(("source", "outer"), REFUSED_INNER_COMPOSITIONS)
    def test_refuses_rather_than_composing_over_a_scalar(self, source: str, outer: str):
        with pytest.raises(ConversionError):
            convert(source)

    @pytest.mark.parametrize(("source", "outer"), REFUSED_INNER_COMPOSITIONS)
    def test_refusal_names_the_inner_construct_that_owns_the_member(self, source: str, outer: str):
        """The construct named is `cast` — the one that would have applied
        `.astype("Int64")` to a scalar — and NOT the outer `alias` / `select`.

        3.4 requires the refusal to name the construct; §4.1 requires it to be
        the construct that owns the member. Naming the outer construct would
        point a reviewer at code that is not the problem, and would also mean
        the guard fired too late — after the outer expression had already been
        built over a broken receiver.
        """
        with pytest.raises(ConversionError) as exc_info:
            convert(source)
        assert exc_info.value.construct == "cast"
        assert exc_info.value.construct != outer

    @pytest.mark.parametrize(("source", "outer"), REFUSED_INNER_COMPOSITIONS)
    def test_refusal_carries_the_offending_line_and_its_number(self, source: str, outer: str):
        """3.4: the construct, the offending source line and the line number
        all travel with the error — `code_interpreter_runner` puts them in
        front of a human."""
        with pytest.raises(ConversionError) as exc_info:
            convert(source)
        error = exc_info.value
        assert error.line == source
        assert error.line_number == 1
        assert "cast" in str(error)
        assert "line 1" in str(error)

    @pytest.mark.parametrize(("source", "outer"), REFUSED_INNER_COMPOSITIONS)
    def test_nothing_is_emitted_for_the_outer_expression(self, source: str, outer: str):
        """Refusal is all-or-nothing for the block (3.4): there is no partially
        converted code carrying the outer construct over a scalar receiver."""
        result = None
        with pytest.raises(ConversionError):
            result = convert(source)
        assert result is None

    def test_refusal_reports_the_line_of_the_composed_statement_not_the_first(self):
        """A convertible line before the composed one does not rescue the block,
        and the reported line number is the offending statement's own."""
        block = (
            'df_transformed = df.withColumn("name", trim(col("name")))\n'
            'df_out = df_transformed.select(col("name"), lit(0).cast("int").alias("z"))'
        )
        with pytest.raises(ConversionError) as exc_info:
            convert(block)
        assert exc_info.value.construct == "cast"
        assert exc_info.value.line_number == 2


# ──────────────────────────────────────────────────────────────────────
# 1.6 → 2.6, second half: a CONVERTED inner shape composes.
# ──────────────────────────────────────────────────────────────────────


class TestConvertedInnerShapeComposesThroughAlias:
    """§4.2's last-but-one row: Arm A's emission is a Series, so `.alias`
    composes over it exactly as it does over `col(...)` — `.rename("z")` is
    valid on a Series.

    Today this emits `None.astype(str).rename("z")` (1.6, *reproduced* in
    `bugfix.md`), which raises `AttributeError` before `.rename` is ever
    reached. Red until task 5.1.

    **Validates: Requirements R1**
    """

    ALIAS = 'aliased = lit(None).cast("string").alias("z")'
    WITH_COLUMN = 'df_transformed = df.withColumn("mobile_phone", lit(None).cast("string").alias("mobile_phone"))'

    def test_alias_emits_rename_over_the_broadcast_series(self):
        code = convert(self.ALIAS)["code"]
        assert f'aliased = {BROADCAST_NULL_STRING}.rename("z")' in code

    def test_alias_report_still_names_cast_lit_and_alias(self):
        """§4.3: `constructs_converted` is derived from the parsed statement by
        `_reported_constructs` (`:388-411`), so the converted shape still
        reports every construct the emitted code carries. This is the
        fix-check property's third clause, asserted rather than assumed."""
        report = convert(self.ALIAS)["conversion_report"]
        assert {"cast", "lit", "alias"} <= set(report["constructs_converted"])

    def test_broadcast_index_is_the_source_frame_inside_with_column(self):
        """§4.3: the broadcast index is the statement's SOURCE frame (`df`),
        which `_with_column_chain` copies into the target (`:805`), so the two
        indexes are identical. Indexing off the target instead would be a
        forward reference to a frame the assignment is still building."""
        code = convert(self.WITH_COLUMN)["code"]
        assert f'df_transformed["mobile_phone"] = {BROADCAST_NULL_STRING}.rename("mobile_phone")' in code

    def test_no_astype_str_survives_anywhere_in_the_emission(self):
        """The 1.1/1.6 signature, gone: `astype(str)` is what produced both the
        live `AttributeError` on a scalar and — on a column — the literal
        string "None" in null cells (`F6`/`F10`). Neither spelling may remain
        in the emitted code for these shapes."""
        for source in (self.ALIAS, self.WITH_COLUMN):
            code = convert(source)["code"]
            assert "astype(str)" not in code
            assert "None.astype" not in code


class TestConvertedInnerShapeComposesThroughSelect:
    """§4.2's last-but-one row, `select` half: the emission ends in
    `.rename("z")`, so `_SELECT_RENAME_SUFFIX` (`pyspark_to_pandas.py:160`) and
    `_select_output_column` (`:163-178`) resolve the output column name and
    `select` keeps NAMING the column instead of taking 3.6's refusal branch.

    3.6 is a fence this task's subject must not disturb: `select` still refuses
    when a name genuinely cannot be resolved (pinned by
    `test_select_output_column_refusal.py`, not repeated here). What is
    asserted here is only that Arm A's emission is *resolvable*, so the fence
    does not fire on a shape that now converts.

    **Validates: Requirements R1**
    """

    SELECT = 'df_out = df.select(col("a"), lit(None).cast("string").alias("z"))'

    def test_resolver_names_the_column_from_the_broadcast_emission(self):
        """The resolver operates on EMITTED text, so this is checkable without
        running `convert()` at all — and it is GREEN before task 5.1, which is
        the point: the mechanism that keeps `select` naming the column is
        already in place, so 5.1 only has to change what `_cast` emits."""
        assert _select_output_column(f'{BROADCAST_NULL_STRING}.rename("z")') == "z"

    def test_resolver_still_refuses_a_broadcast_with_no_alias(self):
        """The orthogonal half: the broadcast on its own carries no name, so an
        unaliased `select` argument stays unresolvable and 3.6's refusal keeps
        its meaning. Arm A does not widen what `select` accepts."""
        assert _select_output_column(BROADCAST_NULL_STRING) is None

    def test_select_builds_the_frame_by_name_over_the_broadcast(self):
        code = convert(self.SELECT)["code"]
        assert f'df_out = pd.DataFrame({{"a": df["a"], "z": {BROADCAST_NULL_STRING}.rename("z")}})' in code

    def test_select_does_not_refuse_and_reports_select_converted(self):
        report = convert(self.SELECT)["conversion_report"]
        assert {"cast", "lit", "alias", "select"} <= set(report["constructs_converted"])

    def test_select_emits_no_subscript_applied_to_the_broadcast(self):
        """3.6's failure mode, one level over: a `[[...]]` subscript applied to
        a Series rather than to a frame. The name resolves, so the frame is
        built by name and no such subscript is emitted."""
        assert "[[" not in convert(self.SELECT)["code"]


class TestConvertedInnerShapeComposesThroughStringFunction:
    """§4.2's last-but-one row, nested-string-function half: `trim` appends
    `.str.strip()` to whatever its argument emitted (`:537-545`), and Arm A's
    emission is a Series, so the receiver now carries the `.str` accessor.

    Today this emits `None.astype(str).str.strip()` (1.6, *reproduced*). Red
    until task 5.1. Note the asymmetry with `trim(lit("x"))`, which has no cast
    to convert and therefore REFUSES (1.4, task 3.2's file): what makes this
    shape convertible is that the cast turned the receiver into a Series first.

    **Validates: Requirements R1**
    """

    TRIM = 'clean = trim(lit(None).cast("string"))'

    def test_trim_emits_str_strip_over_the_broadcast_series(self):
        code = convert(self.TRIM)["code"]
        assert f"clean = {BROADCAST_NULL_STRING}.str.strip()" in code

    def test_trim_report_names_trim_cast_and_lit(self):
        report = convert(self.TRIM)["conversion_report"]
        assert {"trim", "cast", "lit"} <= set(report["constructs_converted"])

    def test_str_accessor_is_never_applied_to_a_bare_constant(self):
        """The whole defect class in one assertion for this shape: a
        Series-only member (`.str`) applied to a scalar emission."""
        code = convert(self.TRIM)["code"]
        assert "None.astype" not in code
        assert '"None".str' not in code


# ──────────────────────────────────────────────────────────────────────
# 1.8 → 2.8: the datetime pair. BOTH HALVES, ONE FILE, ON PURPOSE.
# ──────────────────────────────────────────────────────────────────────

ISO_LITERAL = '"2020-01-01"'
SPARK_FORMAT = '"yyyy-MM-dd"'
STRFTIME_FORMAT = "%Y-%m-%d"


class TestToDateOnALiteralRefuses:
    """§4.2's `to_date` row: `to_date` appends `.dt.date` (`:613`), and `.dt`
    on a scalar raises on both pandas majors — `F5` measured
    `AttributeError: 'Timestamp' object has no attribute 'dt'` for the parsed
    literal, and the same for the `None` / `NaT` case. So 2.1's safe disjunct
    is a refusal.

    Today this converts and emits
    `pd.to_datetime("2020-01-01", format="%Y-%m-%d", errors="coerce").dt.date`
    (1.8, *reproduced*). Red until task 4.2.

    **Validates: Requirements R2**
    """

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(f"d = to_date(lit({ISO_LITERAL}), {SPARK_FORMAT})", id="to_date-with-format"),
            pytest.param(f"d = to_date(lit({ISO_LITERAL}))", id="to_date-without-format"),
        ],
    )
    def test_refuses_naming_to_date_with_the_line_and_number(self, source: str):
        with pytest.raises(ConversionError) as exc_info:
            convert(source)
        error = exc_info.value
        assert error.construct == "to_date"
        assert error.line == source
        assert error.line_number == 1
        assert "to_date" in str(error)

    def test_refusal_holds_inside_with_column_too(self):
        source = f'df_transformed = df.withColumn("d", to_date(lit({ISO_LITERAL}), {SPARK_FORMAT}))'
        with pytest.raises(ConversionError) as exc_info:
            convert(source)
        assert exc_info.value.construct == "to_date"


class TestToTimestampOnALiteralPassesThroughUnchanged:
    """§4.1, "what the fence does not catch, correctly": `to_timestamp` appends
    **no** member — `_to_datetime`'s ternary returns the bare
    `pd.to_datetime(...)` call and only the `to_date` arm adds `.dt.date`
    (`:603-613`, the member at `:613`). `F5` measured the result as a
    `Timestamp`, which broadcasts into a datetime column on assignment and
    agrees with Spark.

    **THIS HALF IS EXPECTED GREEN FROM THE START, and must STAY green.** It is
    here as a fence, not as a red: `bugfix.md` 1.8 suspected the two halves of
    the pair might differ, and they do. A later change that makes
    `to_timestamp` refuse "for consistency" with `to_date` would be a
    regression — the mechanical rule of §4.1 already gets both right with no
    special case, precisely because it fires on *a member being applied*, not
    on the construct's name. If this class is red, the working tree has moved
    and §4.2's row needs re-deriving before task 4 proceeds.

    **Validates: Requirements R2**
    """

    WITH_FORMAT = f"ts = to_timestamp(lit({ISO_LITERAL}), {SPARK_FORMAT})"
    WITHOUT_FORMAT = f"ts = to_timestamp(lit({ISO_LITERAL}))"

    def test_converts_with_the_format_mapped_and_no_member_appended(self):
        code = convert(self.WITH_FORMAT)["code"]
        assert f'ts = pd.to_datetime({ISO_LITERAL}, format="{STRFTIME_FORMAT}", errors="coerce")' in code

    def test_converts_without_a_format_and_no_member_appended(self):
        code = convert(self.WITHOUT_FORMAT)["code"]
        assert f'ts = pd.to_datetime({ISO_LITERAL}, errors="coerce")' in code

    @pytest.mark.parametrize("attribute", [".dt", ".astype", ".str", ".rename", ".notna", ".isna"])
    def test_no_series_only_member_is_appended(self, attribute: str):
        """Every member of `_SERIES_ONLY_MEMBERS` (§4.1). None of them is applied
        here, which is exactly why this shape needs no fence and no carve-out.

        The list was p40's four; ``.notna``/``.isna`` were appended when p42 gap 2
        added the null-predicate rule and widened the tuple. Transcribed rather
        than read from the module on purpose — this file's whole rule is that
        expectations do not come from the producer (`LGR-28b4d75-5`) — so it has
        to be kept in step by hand, which is what this sentence is for."""
        for source in (self.WITH_FORMAT, self.WITHOUT_FORMAT):
            assert attribute not in convert(source)["code"]

    def test_converts_inside_with_column(self):
        source = f'df_transformed = df.withColumn("ts", to_timestamp(lit({ISO_LITERAL}), {SPARK_FORMAT}))'
        code = convert(source)["code"]
        assert (
            f'df_transformed["ts"] = pd.to_datetime({ISO_LITERAL}, format="{STRFTIME_FORMAT}", errors="coerce")' in code
        )

    def test_report_names_to_timestamp_and_lit(self):
        report = convert(self.WITH_FORMAT)["conversion_report"]
        assert {"to_timestamp", "lit"} <= set(report["constructs_converted"])


class TestTheDatetimePairDisagreesDeliberately:
    """1.8's pair, asserted together in one test — the reason task 3.3 requires
    both halves in one file.

    Kept OUT of the two classes above so each of those has one expected colour:
    `TestToDateOnALiteralRefuses` is red until task 4.2,
    `TestToTimestampOnALiteralPassesThroughUnchanged` is green throughout. This
    test straddles them, so it is red until **4.2** on its `to_date` leg only.

    **Validates: Requirements R2**
    """

    def test_same_literal_same_format_opposite_outcomes(self):
        """Same literal, same format, same construct family — `to_date` refuses
        and `to_timestamp` does not, because only `to_date` appends a member
        (`:613`). A change that unifies them in either direction breaks this
        test: refusing `to_timestamp` "for consistency" would refuse a shape
        `F5` measured as correct, and converting `to_date` would re-emit `.dt`
        on a scalar."""
        with pytest.raises(ConversionError) as exc_info:
            convert(f"d = to_date(lit({ISO_LITERAL}), {SPARK_FORMAT})")
        assert exc_info.value.construct == "to_date"

        timestamp_code = convert(f"ts = to_timestamp(lit({ISO_LITERAL}), {SPARK_FORMAT})")["code"]
        assert f'ts = pd.to_datetime({ISO_LITERAL}, format="{STRFTIME_FORMAT}", errors="coerce")' in timestamp_code
        assert ".dt" not in timestamp_code
