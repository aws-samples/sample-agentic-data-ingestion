# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p40 task 13.1 — EXECUTE the converter's output against real pandas (R1, R7).

Spec: `.kiro/specs/p40-pandas-proxy-literal-cast/` — design §6's last testing
row ("Execution — skipped here, real elsewhere") and §5's note N2. Requirements
R1 (the four converted shapes agree with Spark) and R7 (no emitted pandas writes
the string `"None"` where Spark produces a null).

════════════════════════════════════════════════════════════════════════
THIS FILE SKIPS IN THE REPO ENVIRONMENT, AND THE SKIP IS THE EXPECTED
OUTCOME. A GREEN LOCAL RUN IS EVIDENCE FOR THE TRANSLATION, NOT FOR THE
EXECUTION.
════════════════════════════════════════════════════════════════════════
pandas is **not** installed in `agentic_data_ingestion/.venv`, is **not** in
`pyproject.toml`, and p40 does **not** add it — the converter is checker logic,
not a pandas consumer, and installing pandas to satisfy a test would be a
dependency change wearing a test's clothes (design §3's do-NOTs). So
`pytest.importorskip` below skips the whole module here, every other p40 test
asserts the **emitted text** (which is where the defect actually lives), and
p40 task 14 checks that this file reports **skipped** rather than passed — a
pass in this environment means pandas got installed after all.

**What the assertions rest on, since they do not run here.** design §5's
F-table, measured in two throwaway venvs created OUTSIDE the repo under `/tmp`:
pandas **2.2.3** / numpy 2.5.2 / CPython 3.12, and pandas **3.0.5** / numpy
2.5.2 / CPython 3.14. The facts this file re-checks by execution wherever
pandas exists:

* `F7` — `pd.Series(None, index=df.index, dtype="string")` is `<NA>` in every
  row and `pd.Series("x", index=df.index, dtype="string")` is `'x'` in every
  row, dtype `string`, on both majors. That is Spark's answer for
  `lit(None).cast("string")` and `lit("x").cast("string")`.
* `F6`/`F10` — `astype(str)` over a column containing a null writes the literal
  string `'None'` into the null cell and reports **zero** nulls on pandas 2.2.3
  (preserving the null on 3.0.5), while `astype("string")` yields `<NA>` and
  counts it on **both**. The harness injects ~5% nulls into every generated
  column (`code_interpreter_runner._build_test_data_code:449`), so the
  pre-p40 spelling inflated `completeness` for every string-cast column on a
  pandas-2 interpreter. That is why the **column** path moved too, and it is
  the second half of this file.

**N1, carried not resolved.** Nothing in this repo pins the pandas version the
Code Interpreter actually runs — it is the AWS-managed `aws.codeinterpreter.v1`
sandbox (`code_interpreter_runner.py:338`), so its pandas is AWS's. The
nullable `"string"` dtype was chosen because it is correct on both majors
measured; this file is what would catch a third major disagreeing, in an
environment that has pandas.

════════════════════════════════════════════════════════════════════════
ORACLE RULE: THE EXECUTED CODE IS DERIVED FROM THE CONVERTER, NEVER
TRANSCRIBED.
════════════════════════════════════════════════════════════════════════
Every test below calls `PySpark2PandasConverter.convert(...)` and executes what
it returns. No emitted pandas is written down anywhere in this file, because a
transcribed snippet would make the test's own expectation its oracle and the
test could no longer detect the converter drifting — `LGR-28b4d75-5` (template
/ ratified): "a test written from the producer's output cannot detect the
producer drifting, because the output is its own oracle." Its sibling files
(`test_literal_receiver_cast.py` and friends) pin the emitted **text** on
purpose; this is the one p40 test that closes the loop from PySpark input to
executed pandas **behaviour**, so it asserts only over values, dtypes and null
counts.

`exec` is therefore unavoidable here, and it is the established technique in
this directory (`test_alias_conversion.py:90`, `test_to_date_regression.py:196`
— both exec converter output). It is scoped to one helper, over code this
process just generated from a literal source string in this file, with a
namespace holding nothing but the frame. No lint suppression is needed:
`pyproject.toml`'s `[tool.ruff.lint.per-file-ignores]` sets
`"tests/**" = ["S", "B011"]`, so flake8-bandit (S102 `exec-used` included) does
not apply under `tests/`; bandit itself scans only `lambda/`, `shared/` and
`mcp/`.

**Validates: Requirements R1, R7**
"""

import os
import sys

import pytest

# Skipped BEFORE anything else in this module runs, so the skip reason is the
# only thing a report shows. Worded to explain itself where it is read.
pd = pytest.importorskip(
    "pandas",
    reason=(
        "p40 task 13.1 executes the pandas the converter emits, and pandas is deliberately NOT a "
        "dependency of this repo (not in pyproject.toml, not in agentic_data_ingestion/.venv) — the "
        "converter is checker logic, not a pandas consumer. SKIPPED HERE IS THE EXPECTED OUTCOME: a "
        "green local p40 run is evidence for the translation, not for the execution. The evidence for "
        "these assertions is design.md §5's F-table (pandas 2.2.3 / 3.0.5, measured in throwaway venvs "
        "outside the repo). This file is for an environment that has pandas."
    ),
)

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import PySpark2PandasConverter  # noqa: E402  (sys.path pattern, see pyproject)

#: The three rows every assertion below is made over. Three is the smallest
#: width that distinguishes "the null cell stayed null" from "every cell is
#: null" on the column path — the middle row is the null.
_ROWS = 3


def _run(source: str, frame: "pd.DataFrame") -> "pd.DataFrame":
    """Convert *source*, execute the emitted pandas over *frame*, return the frame.

    The executed text comes from the converter on this call — nothing about the
    emission is written down in this file (`LGR-28b4d75-5`). `convert()` emits
    its own `import pandas as pd` / `import numpy as np` header, so the
    namespace carries only the frames the statement reads.
    """
    code = PySpark2PandasConverter.convert(source)["code"]
    namespace: dict = {"df_transformed": frame, "df": frame}
    exec(code, namespace)
    return namespace["df_transformed"]


def _literal_frame() -> "pd.DataFrame":
    """A 3-row frame the converted statement writes a new column into."""
    return pd.DataFrame({"src": ["a", "b", "c"]})


# ══════════════════════════════════════════════════════════════════════
# R1 — the four converted shapes, executed.
#
# Spark: `lit(None).cast("string")` is null in every row; `lit("x")
# .cast("string")` is the literal in every row. Arm A broadcasts the constant
# at the nullable `"string"` dtype, which `F7` measured as exactly that on both
# pandas majors.
# ══════════════════════════════════════════════════════════════════════

NULL_LITERAL_SOURCES = [
    pytest.param(
        'df_transformed = df_transformed.withColumn("mobile_phone", lit(None).cast("string"))',
        id="lit-None-cast-string",
    ),
    pytest.param(
        'df_transformed = df_transformed.withColumn("mobile_phone", lit(None).cast(StringType()))',
        id="lit-None-cast-StringType",
    ),
]

STRING_LITERAL_SOURCES = [
    pytest.param(
        'df_transformed = df_transformed.withColumn("legacy_ref", lit("x").cast("string"))',
        id="lit-str-cast-string",
    ),
    pytest.param(
        'df_transformed = df_transformed.withColumn("legacy_ref", lit("x").cast(StringType()))',
        id="lit-str-cast-StringType",
    ),
]


class TestNullLiteralCastExecutesToAnAllNullStringColumn:
    """1.1 → 2.1 / R1, executed rather than asserted over text.

    The live failure was `AttributeError: 'NoneType' object has no attribute
    'astype'` on exactly this input (`bugfix.md`'s introduction). These tests
    are that traceback inverted.

    **Validates: Requirements R1, R7**
    """

    @pytest.mark.parametrize("source", NULL_LITERAL_SOURCES)
    def test_column_is_na_in_every_row_at_the_nullable_string_dtype(self, source: str):
        result = _run(source, _literal_frame())["mobile_phone"]
        assert len(result) == _ROWS
        assert result.isna().all(), f"expected <NA> in every row, got {result.tolist()!r}"
        assert result.isna().sum() == _ROWS
        assert result.dtype == "string"

    @pytest.mark.parametrize("source", NULL_LITERAL_SOURCES)
    def test_no_cell_carries_the_string_None(self, source: str):
        """R7 / 2.10 — the loud failure must not be traded for a quiet one.

        `F6`: `astype(str)` writes the literal text `'None'` into a null cell on
        pandas 2.2.3 and reports zero nulls. A column spelling Spark's null as
        the three characters `None` is a wrong answer, not a fix.
        """
        result = _run(source, _literal_frame())["mobile_phone"]
        assert "None" not in [str(v) for v in result[result.notna()].tolist()]


class TestStringLiteralCastExecutesToTheLiteralInEveryRow:
    """1.3 → 2.3 / R1.

    **Validates: Requirements R1**
    """

    @pytest.mark.parametrize("source", STRING_LITERAL_SOURCES)
    def test_every_row_holds_the_literal_at_the_nullable_string_dtype(self, source: str):
        result = _run(source, _literal_frame())["legacy_ref"]
        assert len(result) == _ROWS
        assert result.isna().sum() == 0
        assert result.tolist() == ["x"] * _ROWS
        assert result.dtype == "string"


class TestBroadcastAlignsWithTheFramesIndex:
    """The broadcast indexes the statement's SOURCE frame (3.1/3.2), so a
    non-default index must not silently produce NaN by misalignment — which is
    the failure mode an `index=` argument exists to prevent and the one thing
    the emitted-text tests cannot see.

    **Validates: Requirements R1**
    """

    def test_a_non_default_index_still_yields_a_full_column(self):
        frame = pd.DataFrame({"src": ["a", "b", "c"]}, index=[10, 20, 30])
        result = _run(
            'df_transformed = df_transformed.withColumn("legacy_ref", lit("x").cast("string"))',
            frame,
        )["legacy_ref"]
        assert result.index.tolist() == [10, 20, 30]
        assert result.tolist() == ["x"] * _ROWS


# ══════════════════════════════════════════════════════════════════════
# R7 — the amended COLUMN path preserves nulls (`F6`/`F10`).
#
# This is the other half of task 13.1 and the half that was quietly wrong
# before p40: `col("x").cast("string")` used to emit `.astype(str)`, which on
# pandas 2.2.3 writes `'None'` into the null cell and reports `isna().sum() ==
# 0`. `.astype("string")` yields `<NA>` and counts it on both majors.
# ══════════════════════════════════════════════════════════════════════

COLUMN_CAST_SOURCES = [
    pytest.param(
        'df_transformed = df_transformed.withColumn("out", col("legacy_ref").cast("string"))',
        id="col-cast-string",
    ),
    pytest.param(
        'df_transformed = df_transformed.withColumn("out", col("legacy_ref").cast(StringType()))',
        id="col-cast-StringType",
    ),
]


def _column_frame() -> "pd.DataFrame":
    """3 rows, exactly one null — `F10`'s `["p", None, "q"]`."""
    return pd.DataFrame({"legacy_ref": ["p", None, "q"]})


class TestColumnStringCastPreservesNulls:
    """R7 over the column path. `F6`/`F10`.

    **Validates: Requirements R7**
    """

    @pytest.mark.parametrize("source", COLUMN_CAST_SOURCES)
    def test_the_null_cell_stays_null_and_is_counted(self, source: str):
        frame = _column_frame()
        expected_nulls = int(frame["legacy_ref"].isna().sum())
        result = _run(source, frame)["out"]
        assert result.isna().tolist() == [False, True, False]
        assert int(result.isna().sum()) == expected_nulls == 1
        assert result.dtype == "string"

    @pytest.mark.parametrize("source", COLUMN_CAST_SOURCES)
    def test_the_non_null_cells_survive_unchanged(self, source: str):
        result = _run(source, _column_frame())["out"]
        assert result.iloc[0] == "p"
        assert result.iloc[2] == "q"

    @pytest.mark.parametrize("source", COLUMN_CAST_SOURCES)
    def test_no_cell_carries_the_string_None(self, source: str):
        """The precise pandas-2.2.3 defect of the pre-p40 spelling: the null
        cell became the three characters `None` and the column reported zero
        nulls, inflating `completeness` for every string-cast column."""
        result = _run(source, _column_frame())["out"]
        assert "None" not in [str(v) for v in result[result.notna()].tolist()]
