# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p40 task 3.1 — the literal-receiver shapes that reach `_cast` (1.1, 1.2, 1.3, 1.7).

Spec: `.kiro/specs/p40-pandas-proxy-literal-cast/` — R1, R2, R8. Defect clauses
`bugfix.md` 1.1, 1.2, 1.3, 1.7; expected clauses 2.1, 2.2, 2.3, 2.7; the refusal
payload contract 3.4. The ratified outcome per shape is `design.md` §4.2's
table; the converted shapes' emission is §4.3; the measured pandas facts are
§5's F-table (`F1`-`F11`), whose probe script is preserved in
`verification-notes.md`.

════════════════════════════════════════════════════════════════════════
ORACLE RULE FOR THIS FILE: EXPECTATIONS COME FROM SPARK SEMANTICS.
THE CURRENT CONVERTER'S OUTPUT MUST NOT BE CONSULTED.
════════════════════════════════════════════════════════════════════════
`bugfix.md` 2.15, and `LGR-28b4d75-5` (template / ratified): "a test written
from the producer's output cannot detect the producer drifting, because the
output is its own oracle." That normally costs little. Here it is the whole
point, because in **every** shape below the bug is a *successful* conversion —
`convert()` returns, and the pandas it returns raises at runtime (`F9`) or
disagrees with Spark quietly (`F4`, `F6`). A test transcribed from today's
output would therefore pin the bug and look thorough doing it.

**The opposite rule lives next door, in this same directory.** The p40 task-2.1
pin block at the tail of `test_converter_equivalence_baseline.py`
(`P40_UNPINNED_CAST_KEY_PINS`, keyed by `P40_PIN_CAST_KEY`) is a set of
*preservation* pins over a `col(...)` receiver — a path that works — so there
**current converter output IS the oracle** and every expectation was
transcribed from it byte-exactly. Both rules now live in one directory. Each
side says which one it follows; getting them the wrong way round produces a
plausible-looking, worthless test file.

════════════════════════════════════════════════════════════════════════
EXPECTED RED WINDOW — TWO STAGES. BOTH REDS ARE ON PURPOSE.
════════════════════════════════════════════════════════════════════════
This file is written BEFORE any production line moves (p40 tasks 4 and 5), and
it is expected to FAIL against the converter as it stands. That failure is the
recorded evidence that the defect exists — p40 task 3.4 runs it red and
transcribes the counterexamples. Nothing here may be `xfail`ed or `skip`ped to
hide it, and neither a test nor the converter may be "fixed" to make it green
early.

* **Closes at task 4.2** (the fence — `_require_series` at the six member
  application sites): every refusal class below —
  `TestBooleanCastOnLiteralRefuses` (1.2),
  `TestNumericCastOnLiteralRefuses` (1.7),
  `TestStringCastOnNonStringLiteralRefuses` (Arm A's boundary).
* **Closes at task 5.1** (Arm A — `_CAST_TARGETS`' two string keys and
  `_cast`'s literal broadcast): `TestStringCastOnLiteralConverts` (1.1, 1.3).

`TestConvertedShapeReportsItsConstructs` is expected GREEN throughout:
`constructs_converted` is derived from the *parsed statement* by
`_reported_constructs`, so it needs no new behaviour — 5.1 requires it asserted
rather than assumed, which is what it is doing here.

**A pass on any refusal or broadcast assertion before its closing task is a
FINDING, not a success.** Every one of these shapes was reproduced converting
successfully during the bugfix phase (`bugfix.md` 1.1-1.7, grade *reproduced*).
If one now refuses, the working tree has moved and `design.md` §4.2's line needs
re-deriving before task 4 proceeds.

pandas is NOT installed in this venv and is not added by this spec. Nothing here
executes pandas: every assertion is over the *emitted text*, which is where this
defect lives (`design.md` §5 N2). The execution half is p40 task 13.1, guarded
by `pytest.importorskip("pandas")`.

**Validates: Requirements R1, R2, R8**
"""

import os
import sys

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

#: `convert()` prepends this and it is not part of any statement's translation
#: (`pyspark_to_pandas.py:1031`). Stripped so an assertion reads as the one
#: emitted statement, and so a counterexample records that statement alone.
_PANDAS_HEADER = "import pandas as pd\nimport numpy as np\n\n"


def convert(code: str) -> dict:
    return PySpark2PandasConverter.convert(code)


def emitted(result: dict) -> str:
    """The emitted pandas with the two-line import header removed."""
    code = result["code"]
    assert code.startswith(_PANDAS_HEADER), f"public surface changed — header missing (B9):\n{code}"
    return code[len(_PANDAS_HEADER) :]


def expect_refusal(source: str) -> ConversionError:
    """Assert *source* refuses, and make a successful conversion legible.

    A bare `pytest.raises` would report only "DID NOT RAISE", and the emitted
    pandas is precisely what p40 task 3.4 has to record as the counterexample —
    so the emission is put in the failure message.
    """
    try:
        result = convert(source)
    except ConversionError as exc:
        return exc
    pytest.fail(
        "expected ConversionError (design.md §4.2 ratified REFUSE for this shape); "
        f"conversion SUCCEEDED instead and emitted:\n    {emitted(result)}"
    )


def cast_spec(key: str) -> str:
    """How a `_CAST_TARGETS` key is spelled inside `.cast(...)`.

    The table is keyed both by the type-constructor name and by the string
    literal spelling (`pyspark_to_pandas.py:277-294`), and `_cast` reads the two
    through different branches (`:655-660`), so both spellings are exercised.
    """
    return f"{key}()" if key.endswith("Type") else f'"{key}"'


# ══════════════════════════════════════════════════════════════════════
# 1.1 / 1.3 — the four CONVERTED shapes. Arm A.
#
# Spark's answer is unambiguous and pandas can express it identically on both
# majors measured: `lit(None).cast("string")` is null in every row, and
# `lit("x").cast("string")` is the literal in every row. `F7` measured
# `pd.Series(None, index=…, dtype="string")` as all-`<NA>` and
# `pd.Series("x", index=…, dtype="string")` as `'x'` in every row, on 2.2.3 and
# on 3.0.5 alike.
#
# `<frame>` in the emission is the frame `_expr` threads — the statement's
# SOURCE frame (3.1/3.2). Under `_with_column_chain` the target is a `.copy()`
# of that frame (`pyspark_to_pandas.py:805`), so the two indexes are identical.
# ══════════════════════════════════════════════════════════════════════

CONVERTED_STRING_CAST_SHAPES = [
    pytest.param(
        'df_transformed = df_transformed.withColumn("mobile_phone", lit(None).cast("string"))',
        'df_transformed["mobile_phone"] = pd.Series(None, index=df_transformed.index, dtype="string")',
        id="lit-None-cast-string",
    ),
    pytest.param(
        'df_transformed = df_transformed.withColumn("mobile_phone", lit(None).cast(StringType()))',
        'df_transformed["mobile_phone"] = pd.Series(None, index=df_transformed.index, dtype="string")',
        id="lit-None-cast-StringType",
    ),
    pytest.param(
        'df_transformed = df_transformed.withColumn("legacy_ref", lit("x").cast("string"))',
        'df_transformed["legacy_ref"] = pd.Series("x", index=df_transformed.index, dtype="string")',
        id="lit-str-cast-string",
    ),
    pytest.param(
        'df_transformed = df_transformed.withColumn("legacy_ref", lit("x").cast(StringType()))',
        'df_transformed["legacy_ref"] = pd.Series("x", index=df_transformed.index, dtype="string")',
        id="lit-str-cast-StringType",
    ),
]


class TestStringCastOnLiteralConverts:
    """1.1 / 1.3 → 2.1 / 2.3. The one family Arm A converts.

    RED until p40 task 5.1.

    **Validates: Requirements R1**
    """

    @pytest.mark.parametrize(("source", "expected"), CONVERTED_STRING_CAST_SHAPES)
    def test_emits_the_literal_broadcast_at_the_nullable_string_dtype(self, source: str, expected: str):
        """The broadcast emission of `design.md` §4.3, verbatim.

        Derived from Spark: the column is null in every row for `lit(None)` and
        the literal in every row for `lit("x")`. `pd.Series(<const>,
        index=<frame>.index, dtype="string")` is exactly that on both pandas
        majors measured (`F7`).
        """
        assert emitted(convert(source)) == expected

    @pytest.mark.parametrize(("source", "expected"), CONVERTED_STRING_CAST_SHAPES)
    def test_never_applies_a_series_only_member_to_the_constant(self, source: str, expected: str):
        """R7 / 2.10 — the loud failure must not be traded for a quiet one.

        `astype(str)` over a null yields the literal string `'None'` and reports
        ZERO nulls on pandas 2.2.3, while preserving `nan` on 3.0.5
        (`F6`/`F10`); the harness injects ~5% nulls into every generated column
        (`code_interpreter_runner._build_test_data_code:449`). So an emission
        carrying `.astype(str)` is a wrong answer about a nulls column, not a
        fix — and on a scalar receiver it is `AttributeError` outright (`F9`).
        """
        code = emitted(convert(source))
        assert ".astype(str)" not in code
        assert ".astype(" not in code

    def test_broadcast_indexes_the_statements_source_frame(self):
        """When the target and the source frame differ, the index is the
        SOURCE frame's — the frame `_expr` threads (3.1/3.2), which the
        `.copy()` at `pyspark_to_pandas.py:805` makes index-identical to the
        target."""
        source = 'df_transformed = df.withColumn("mobile_phone", lit(None).cast("string"))'
        assert emitted(convert(source)) == (
            "df_transformed = df.copy(); "
            'df_transformed["mobile_phone"] = pd.Series(None, index=df.index, dtype="string")'
        )


class TestConvertedShapeReportsItsConstructs:
    """`design.md` §4.3's last consequence: `constructs_converted` stays honest
    for the converted shape, and 5.1 requires that asserted rather than assumed.

    Expected GREEN throughout — `_reported_constructs`
    (`pyspark_to_pandas.py:388-411`) derives the set from the PARSED statement,
    so it needs no new behaviour. Its value is that a later change which drops
    `cast` or `lit` from the report — the third clause of the fix-check property
    — fails here.

    **Validates: Requirements R1**
    """

    @pytest.mark.parametrize(("source", "expected"), CONVERTED_STRING_CAST_SHAPES)
    def test_report_names_cast_and_lit(self, source: str, expected: str):
        report = convert(source)["conversion_report"]
        assert {"cast", "lit", "withColumn"} <= set(report["constructs_converted"])


# ══════════════════════════════════════════════════════════════════════
# 1.2 — boolean / BooleanType on a literal REFUSES.
#
# 2.2 asks for the class, not the string keys. `None.astype(bool)` raises today
# (`bugfix.md` 1.2, *reproduced*; `F9` for the mechanism), and while
# `pd.Series(None, dtype="boolean")` would express the null case, Spark's
# boolean cast of a NON-null literal could not be verified in this environment
# — so `design.md` §4.2 ratified refusal for the whole key pair. N3(b) is the
# neighbouring reason not to widen Arm A here: `astype(bool)` maps a null to
# `False` where Spark yields null, so the COLUMN path is itself known-divergent
# and is only characterization-pinned (task 2.1).
# ══════════════════════════════════════════════════════════════════════

BOOLEAN_CAST_ON_LITERAL_SHAPES = [
    pytest.param('df_transformed = df_transformed.withColumn("a", lit(None).cast("boolean"))', id="lit-None-boolean"),
    pytest.param(
        'df_transformed = df_transformed.withColumn("a", lit(None).cast(BooleanType()))', id="lit-None-BooleanType"
    ),
    pytest.param('df_transformed = df_transformed.withColumn("a", lit(True).cast("boolean"))', id="lit-True-boolean"),
    pytest.param(
        'df_transformed = df_transformed.withColumn("a", lit(True).cast(BooleanType()))', id="lit-True-BooleanType"
    ),
]


class TestBooleanCastOnLiteralRefuses:
    """1.2 → 2.2. RED until p40 task 4.2.

    **Validates: Requirements R2**
    """

    @pytest.mark.parametrize("source", BOOLEAN_CAST_ON_LITERAL_SHAPES)
    def test_refuses_rather_than_emitting_pandas_that_raises(self, source: str):
        expect_refusal(source)

    @pytest.mark.parametrize("source", BOOLEAN_CAST_ON_LITERAL_SHAPES)
    def test_refusal_names_cast_with_the_line_and_the_line_number(self, source: str):
        """3.4 — the refusal names the construct that OWNS the member, plus the
        offending line and its number. `cast` is that construct: it is the
        member application `code_interpreter_runner` puts in front of a human.
        """
        error = expect_refusal(source)
        assert error.construct == "cast"
        assert error.line == source.strip()
        assert error.line_number == 1
        assert "cast" in str(error)
        assert "line 1" in str(error)


# ══════════════════════════════════════════════════════════════════════
# 1.7 — every numeric cast target on a literal REFUSES.
#
# Measured grounds, both halves of the numeric branch:
#   * The `.astype`-suffixed half raises on BOTH pandas majors, with TWO
#     different exception types — `AttributeError: 'int' object has no
#     attribute 'astype'` for `lit(0)` (`F1`), `TypeError: data type 'Int64'
#     not understood` for `lit(None)` (`F2`), and `AttributeError` again for
#     the `float32` spelling (`F3`).
#   * The suffix-free half (`_CAST_NUMERIC_FLOAT = ("numeric", "")`,
#     `pyspark_to_pandas.py:276`) does NOT raise — it assigns, and disagrees
#     quietly: `pd.to_numeric(0, errors="coerce")` yields an `int64` column
#     where Spark produces a double (`F4`).
# Two halves behaving differently, and Spark's own dtype unverifiable here, is
# why §4.2 ratified one rule per receiver shape: refuse.
#
# Costs nothing that works today — no baseline case applies a cast to a
# literal; `cast-literal-integer` / `cast-literal-double` are COLUMN receivers
# whose target is merely spelled as a string (`design.md` §4.2).
# ══════════════════════════════════════════════════════════════════════

#: All twelve numeric keys of `_CAST_TARGETS`, in table order. The three
#: emission families are noted because they are three separate measured facts.
NUMERIC_CAST_TARGET_KEYS = [
    # `_CAST_NUMERIC_INT` → `.astype("Int64")` — raises (F1/F2)
    "int",
    "integer",
    "long",
    "bigint",
    "IntegerType",
    "LongType",
    "ShortType",
    # `_CAST_NUMERIC_FLOAT` → no suffix — executes, wrong dtype (F4)
    "double",
    "float",
    "decimal",
    "DoubleType",
    # `.astype("float32")` — raises (F3)
    "FloatType",
]

#: Both constants matter: they are what splits `F1` from `F2` in the suffixed
#: half and what splits the two `F4` outcomes in the suffix-free half.
NUMERIC_CAST_LITERAL_RECEIVERS = ["lit(0)", "lit(None)"]

NUMERIC_CAST_ON_LITERAL_SHAPES = [
    pytest.param(
        f'df_transformed = df_transformed.withColumn("amount", {receiver}.cast({cast_spec(key)}))',
        id=f"{receiver.replace('lit(', '').replace(')', '')}-{key}",
    )
    for key in NUMERIC_CAST_TARGET_KEYS
    for receiver in NUMERIC_CAST_LITERAL_RECEIVERS
]


class TestNumericCastOnLiteralRefuses:
    """1.7 → 2.7. RED until p40 task 4.2.

    **Validates: Requirements R2**
    """

    def test_every_numeric_cast_target_key_is_covered(self):
        """R8 asks for the class, so the parametrisation must not silently
        shrink. Twelve keys × two literal constants."""
        assert len(NUMERIC_CAST_TARGET_KEYS) == 12
        assert len(NUMERIC_CAST_ON_LITERAL_SHAPES) == 24

    @pytest.mark.parametrize("source", NUMERIC_CAST_ON_LITERAL_SHAPES)
    def test_refuses_rather_than_emitting_pandas_that_raises_or_lies(self, source: str):
        expect_refusal(source)

    @pytest.mark.parametrize("source", NUMERIC_CAST_ON_LITERAL_SHAPES)
    def test_refusal_names_cast_with_the_line_and_the_line_number(self, source: str):
        """3.4, again on the numeric branch: the refusal payload is the same
        whichever `_CAST_TARGETS` half the target falls in."""
        error = expect_refusal(source)
        assert error.construct == "cast"
        assert error.line == source.strip()
        assert error.line_number == 1

    def test_refusal_points_at_the_offending_statement_inside_a_block(self):
        """3.4's line number is the refusing statement's own 1-based position,
        not the block's first line — and 3.8's all-or-nothing still holds, so
        the two convertible lines around it do not rescue the block."""
        block = (
            'df_transformed = df.withColumn("name", trim(col("name")))\n'
            'df_transformed = df_transformed.withColumn("amount", lit(0).cast("int"))\n'
            'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")'
        )
        error = expect_refusal(block)
        assert error.construct == "cast"
        assert error.line_number == 2
        assert error.line == 'df_transformed = df_transformed.withColumn("amount", lit(0).cast("int"))'


# ══════════════════════════════════════════════════════════════════════
# Arm A's boundary — a STRING cast of a non-string, non-None literal REFUSES.
#
# Arm A covers `None` and `str` constants only. The measured reason is `F7`:
# `pd.Series(True, index=…, dtype="string")` yields `'True'`, where Spark
# spells a boolean `true`. `0` and `1.5` are refused on the same rule rather
# than on a per-constant-type carve-out — Spark's spelling of a non-string
# constant cast to string is not something this environment can verify, and
# `design.md` §4.2 ratified refusal as 2.1's safe disjunct.
#
# This is the pair of tests that would break a "just broadcast every literal"
# reading of Arm A.
# ══════════════════════════════════════════════════════════════════════

NON_STRING_LITERAL_STRING_CAST_SHAPES = [
    pytest.param('df_transformed = df_transformed.withColumn("a", lit(0).cast("string"))', id="lit-0-string"),
    pytest.param('df_transformed = df_transformed.withColumn("a", lit(0).cast(StringType()))', id="lit-0-StringType"),
    pytest.param('df_transformed = df_transformed.withColumn("a", lit(1.5).cast("string"))', id="lit-1.5-string"),
    pytest.param(
        'df_transformed = df_transformed.withColumn("a", lit(1.5).cast(StringType()))', id="lit-1.5-StringType"
    ),
    pytest.param('df_transformed = df_transformed.withColumn("a", lit(True).cast("string"))', id="lit-True-string"),
    pytest.param(
        'df_transformed = df_transformed.withColumn("a", lit(True).cast(StringType()))', id="lit-True-StringType"
    ),
]


class TestStringCastOnNonStringLiteralRefuses:
    """2.1's safe disjunct where Spark's spelling is unverified here.

    RED until p40 task 4.2.

    **Validates: Requirements R2**
    """

    @pytest.mark.parametrize("source", NON_STRING_LITERAL_STRING_CAST_SHAPES)
    def test_refuses_rather_than_guessing_sparks_spelling(self, source: str):
        expect_refusal(source)

    @pytest.mark.parametrize("source", NON_STRING_LITERAL_STRING_CAST_SHAPES)
    def test_refusal_names_cast_with_the_line_and_the_line_number(self, source: str):
        error = expect_refusal(source)
        assert error.construct == "cast"
        assert error.line == source.strip()
        assert error.line_number == 1


# ══════════════════════════════════════════════════════════════════════
# 3.2 — the asymmetry the bug condition turns on, asserted so the fix cannot
# close the defect by breaking the path that already works.
#
# A bare `lit(<constant>)` with NO cast emits the constant verbatim and must
# CONTINUE TO (*reproduced*, `bugfix.md` 3.2). No member is applied, so no
# guard fires (`design.md` §4.1, "what the fence does not catch, correctly").
#
# Expected GREEN throughout — including through 4.2 and 5.1, which is the
# point of asserting it in the same file as the refusals.
# ══════════════════════════════════════════════════════════════════════


class TestBareLiteralWithoutCastIsUntouched:
    """**Validates: Requirements R2**"""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            (
                'df_transformed = df_transformed.withColumn("a", lit(None))',
                'df_transformed["a"] = None',
            ),
            (
                'df_transformed = df_transformed.withColumn("a", lit("x"))',
                'df_transformed["a"] = "x"',
            ),
            (
                'df_transformed = df_transformed.withColumn("a", lit(0))',
                'df_transformed["a"] = 0',
            ),
        ],
    )
    def test_uncast_literal_emits_the_constant_verbatim(self, source: str, expected: str):
        assert emitted(convert(source)) == expected
