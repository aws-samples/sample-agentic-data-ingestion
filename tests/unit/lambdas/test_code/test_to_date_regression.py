# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End-to-end regression for the review's concrete story (P28 task 9.5).

The original bug: a ``to_date`` format transform was (a) mangled by the
PySpark→pandas conversion — the self-reference ``withColumn`` regex missed
``df_transformed = df.withColumn(...)``, so the line was commented out and
the date format dropped; (b) the commented-out code "executed" cleanly
(nothing actually ran); (c) validation silently fell back to the RAW ``df``
— so a broken date transform sailed through as a passed test.

This regression proves each link now holds END-TO-END for the canonical
review shape ``to_date(col("order_date"), "dd-MM-yyyy")``:

1. conversion preserves the transform and its format (nothing commented out),
2. the converted code RUNS and produces ``df_transformed`` with genuinely
   day-first-parsed dates (the format string reached the call),
3. validation validates the TRANSFORMED output, and its metrics reflect it,
4. negative control: when the transform is dropped, the run fails honestly
   — never a passed run.

pandas is not installed in the test venv, so execution uses a purpose-built
pandas stand-in that records the ``format`` argument and genuinely parses
values with it (``datetime.strptime``). A dropped or altered format would
produce wrong dates or misses in the recorder — either breaks the test.

**Validates: Requirements 10.7**
"""

import json
import os
import sys
import types
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from code_interpreter_runner import CodeInterpreterTestRunner
from pyspark_to_pandas import PySpark2PandasConverter

# ──────────────────────────────────────────────────────────────────────
# Link 1 — the canonical transform: the exact review shape
# (source ≠ target assignment + to_date with a day-first format).
# ──────────────────────────────────────────────────────────────────────

CANONICAL_TRANSFORM = 'df_transformed = df.withColumn("order_date", to_date(col("order_date"), "dd-MM-yyyy"))'

# The pandas expression the conversion must emit — format mapped to strftime.
EXPECTED_TO_DATETIME = 'pd.to_datetime(df["order_date"], format="%d-%m-%Y", errors="coerce").dt.date'

# Day-first source data: "05-01-2023" is 5 January — a dropped format
# (month-first or ISO guessing) would yield 1 May or a parse failure.
RAW_DATES = ["25-12-2023", "05-01-2023", "17-06-2024", "not-a-date"]

TARGET_SCHEMA = {
    "properties": {
        "order_id": {"type": "integer"},
        "order_date": {"type": "string"},
    }
}


# ──────────────────────────────────────────────────────────────────────
# Purpose-built pandas stand-in (pandas is not in the test venv).
# Enough surface to execute BOTH the converted transform and the
# generated validation code in-process, while recording the format
# argument that reaches pd.to_datetime.
# ──────────────────────────────────────────────────────────────────────


class _FakeSeries:
    """Minimal pandas.Series stand-in for the surfaces the generated code uses."""

    def __init__(self, values, name=None):
        self._values = list(values)
        self.name = name
        self.dtype = "object"

    def rename(self, name):
        """pandas ``Series.rename("y")`` — returns a NEW series named *name*.

        The converted ``.alias("y")`` shape from P28b R1a; like real pandas,
        the original series is untouched.
        """
        return _FakeSeries(self._values, name=name)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def isna(self):
        return _FakeSeries([v is None for v in self._values])

    def sum(self):
        return sum(1 for v in self._values if v is True)


class _FakeDataFrame:
    """Minimal pandas.DataFrame stand-in supporting the converted transform
    (copy + column assignment — the withColumn rewrite shape) and the
    generated validation code (len, columns, isna, duplicated)."""

    def __init__(self, data: dict):
        self._data = {k: list(v) for k, v in data.items()}

    @property
    def columns(self):
        return list(self._data)

    def copy(self):
        return _FakeDataFrame(self._data)

    def __len__(self):
        return len(next(iter(self._data.values()), []))

    def __getitem__(self, col):
        return _FakeSeries(self._data[col])

    def __setitem__(self, col, values):
        self._data[col] = list(values)

    def duplicated(self):
        rows = list(zip(*self._data.values(), strict=True))
        seen: set = set()
        flags = []
        for row in rows:
            flags.append(row in seen)
            seen.add(row)
        return _FakeSeries(flags)


class _DtAccessor:
    """The ``.dt`` accessor of a datetime series — ``.date`` is an attribute."""

    def __init__(self, values):
        self._values = values

    @property
    def date(self):
        return _FakeSeries([v.date() if isinstance(v, datetime) else None for v in self._values])


class _FakeDatetimeSeries:
    def __init__(self, values):
        self._values = values

    @property
    def dt(self):
        return _DtAccessor(self._values)


def _make_fake_pandas(recorded_formats: list) -> types.ModuleType:
    """A pandas module stand-in whose to_datetime records the format arg and
    genuinely parses with it — so only a surviving, correct format string
    produces the expected day-first dates."""
    module = types.ModuleType("pandas")

    def to_datetime(series, format=None, errors=None):
        recorded_formats.append(format)
        parsed = []
        for value in series:
            if value is None:
                parsed.append(None)
                continue
            try:
                if format is not None:
                    parsed.append(datetime.strptime(str(value), format))
                else:
                    # No format (i.e. the format was dropped): only ISO parses —
                    # day-first strings like "25-12-2023" fail, mirroring what
                    # losing the format does to dd-MM-yyyy data.
                    parsed.append(datetime.fromisoformat(str(value)))
            except ValueError:
                if errors == "coerce":
                    parsed.append(None)
                else:
                    raise
        return _FakeDatetimeSeries(parsed)

    module.to_datetime = to_datetime
    return module


def _execute_converted(code: str, namespace: dict, recorded_formats: list) -> None:
    """Execute the FULL converted code (including its import header) with the
    pandas/numpy stand-ins satisfying the imports."""
    fake_pandas = _make_fake_pandas(recorded_formats)
    fake_numpy = types.ModuleType("numpy")
    with patch.dict(sys.modules, {"pandas": fake_pandas, "numpy": fake_numpy}):
        exec(code, namespace)


def _run_validation_code(code: str, namespace: dict) -> tuple[dict | None, Exception | None, str | None]:
    """Execute generated validation code in *namespace*; capture the VALIDATION
    marker (via a print shim) and any raised exception."""
    printed: list[str] = []
    namespace["print"] = lambda *a: printed.append(" ".join(str(x) for x in a))
    error: Exception | None = None
    try:
        exec(code, namespace)
    except Exception as exc:
        error = exc
    marker = next((line for line in printed if line.startswith("VALIDATION:")), None)
    parsed = json.loads(marker[len("VALIDATION:") :]) if marker else None
    return parsed, error, marker


def _raw_df() -> _FakeDataFrame:
    return _FakeDataFrame({"order_id": [1, 2, 3, 4], "order_date": list(RAW_DATES)})


def _make_stream_response(text: str, stderr: str = "", exit_code: int = 0) -> dict:
    """Fake invoke_code_interpreter response with structuredContent."""
    return {
        "stream": [
            {
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": {
                        "stdout": text,
                        "stderr": stderr,
                        "exitCode": exit_code,
                        "executionTime": 0.05,
                    },
                }
            }
        ]
    }


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.start_code_interpreter_session.return_value = {"sessionId": "sess-regression"}
    client.stop_code_interpreter_session.return_value = {}
    return client


@pytest.fixture
def runner(mock_client):
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = mock_client
        r = CodeInterpreterTestRunner(region="us-west-2")
        r.client = mock_client
        return r


# ──────────────────────────────────────────────────────────────────────
# Link 2 — conversion preserves the transform and its format.
# ──────────────────────────────────────────────────────────────────────


class TestConversionPreservesToDateFormat:
    def test_emits_format_mapped_to_datetime_assignment(self):
        """The converted code contains the format-mapped pd.to_datetime and
        the df_transformed assignment (new-frame withColumn semantics)."""
        code = PySpark2PandasConverter.convert(CANONICAL_TRANSFORM)["code"]
        assert EXPECTED_TO_DATETIME in code
        assert 'df_transformed = df.copy(); df_transformed["order_date"] = ' in code

    def test_report_shows_to_date_and_with_column_converted(self):
        report = PySpark2PandasConverter.convert(CANONICAL_TRANSFORM)["conversion_report"]
        assert "to_date" in report["constructs_converted"]
        assert "withColumn" in report["constructs_converted"]
        # One meaningful line in, one out — nothing dropped.
        assert report["input_lines"] == 1
        assert report["output_lines"] == 1

    def test_nothing_commented_out_and_no_pyspark_residue(self):
        """The old failure shipped the transform commented out — assert no
        comment lines and no PySpark constructs remain."""
        code = PySpark2PandasConverter.convert(CANONICAL_TRANSFORM)["code"]
        assert "# UNCONVERTED" not in code
        assert not any(line.strip().startswith("#") for line in code.splitlines())
        assert "withColumn" not in code
        assert "col(" not in code


# ──────────────────────────────────────────────────────────────────────
# Link 3 — the converted code EXECUTES and transforms the dates.
# ──────────────────────────────────────────────────────────────────────


class TestConvertedCodeExecutes:
    def test_runs_and_produces_day_first_transformed_dates(self):
        """The full converted code (imports included) runs and df_transformed
        holds genuinely day-first-parsed dates: "05-01-2023" is 5 January —
        a dropped format would yield 1 May (month-first) or a parse miss."""
        code = PySpark2PandasConverter.convert(CANONICAL_TRANSFORM)["code"]
        namespace = {"df": _raw_df()}
        recorded: list = []

        _execute_converted(code, namespace, recorded)

        assert "df_transformed" in namespace
        transformed_dates = list(namespace["df_transformed"]["order_date"])
        assert transformed_dates[0] == date(2023, 12, 25)
        assert transformed_dates[1] == date(2023, 1, 5)  # day-first, NOT May 1
        assert transformed_dates[2] == date(2024, 6, 17)
        assert transformed_dates[3] is None  # unparseable → coerced, not crashed

    def test_format_string_reached_the_conversion_call(self):
        """The dd-MM-yyyy format survives all the way INTO the pd.to_datetime
        call — previously it was captured by the regex and silently dropped."""
        code = PySpark2PandasConverter.convert(CANONICAL_TRANSFORM)["code"]
        recorded: list = []

        _execute_converted(code, {"df": _raw_df()}, recorded)

        assert recorded == ["%d-%m-%Y"]

    def test_raw_df_stays_pristine(self):
        """withColumn returns a NEW frame — the copy keeps raw df untouched,
        which is what makes link 4's raw-vs-transformed distinction real."""
        code = PySpark2PandasConverter.convert(CANONICAL_TRANSFORM)["code"]
        namespace = {"df": _raw_df()}

        _execute_converted(code, namespace, [])

        assert list(namespace["df"]["order_date"]) == RAW_DATES


# ──────────────────────────────────────────────────────────────────────
# Link 4 — validation validates the TRANSFORMED output.
# ──────────────────────────────────────────────────────────────────────


class TestValidationSeesTransformedOutput:
    def _transform_then_validate(self, runner):
        code = PySpark2PandasConverter.convert(CANONICAL_TRANSFORM)["code"]
        namespace = {"df": _raw_df()}
        _execute_converted(code, namespace, [])
        validation_code = runner._build_validation_code(TARGET_SCHEMA)
        return _run_validation_code(validation_code, namespace)

    def test_validation_metrics_reflect_transformed_dates(self, runner):
        """The VALIDATION metrics show df_transformed: the coerced bad date
        appears as ONE null in order_date — the raw df has zero nulls, so
        this is only observable on the transformed frame."""
        parsed, error, _ = self._transform_then_validate(runner)

        assert error is None
        assert parsed is not None and "error" not in parsed
        assert parsed["record_count"] == 4
        assert parsed["null_counts"] == {"order_id": 0, "order_date": 1}
        assert parsed["missing_columns"] == []
        assert parsed["schema_compliance"] == 100.0

    def test_parsed_results_carry_transformed_metrics_to_the_report(self, runner):
        """The runner's result parsing (what reaches the test report and the
        9.4 executed-check metrics) reflects the transformed frame."""
        _, _, marker = self._transform_then_validate(runner)

        results = runner._parse_results(marker)

        assert results["execution_successful"] is True
        assert results["output_records"] == 4
        assert results["data_quality_results"]["null_counts"] == {"order_id": 0, "order_date": 1}


# ──────────────────────────────────────────────────────────────────────
# Link 5 — negative control: the OLD failure now fails honestly.
# ──────────────────────────────────────────────────────────────────────


class TestDroppedTransformFailsHonestly:
    def test_validation_fails_when_df_transformed_absent(self, runner):
        """Simulate the old failure (transform dropped → df_transformed never
        produced, only raw df in scope): validation raises and reports the
        explicit error — it never silently validates the raw frame."""
        validation_code = runner._build_validation_code(TARGET_SCHEMA)
        namespace = {"df": _raw_df()}

        parsed, error, marker = _run_validation_code(validation_code, namespace)

        assert error is not None
        assert "transform did not produce df_transformed" in str(error)
        assert parsed == {"error": "transform did not produce df_transformed"}
        # The report side never shows a pass for that output.
        results = runner._parse_results(marker)
        assert results["execution_successful"] is False
        assert results["error_details"] == "transform did not produce df_transformed"

    def test_runner_returns_honest_failure_for_unconvertible_transform(self, runner, mock_client):
        """A transform block the converter cannot express (9.2's ConversionError
        at the runner level): honest structured failure naming the construct —
        nothing executed, nothing commented out, never a passed run."""
        block = (
            CANONICAL_TRANSFORM + '\ndf_transformed = df_transformed.groupBy("order_date").agg({"order_id": "count"})'
        )

        result = runner.run_test(block, TARGET_SCHEMA, TARGET_SCHEMA)

        assert result["execution_successful"] is False
        assert result["executed"] is False
        assert "groupBy" in result["error_details"]
        assert "line 2" in result["error_details"]
        mock_client.start_code_interpreter_session.assert_not_called()
        mock_client.invoke_code_interpreter.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Runner tie-in: the format-preserving converted code is exactly what
# the runner sends to the interpreter for the transform step.
# ──────────────────────────────────────────────────────────────────────


class TestRunnerSendsFormatPreservingCode:
    def test_step2_code_contains_mapped_format(self, runner, mock_client):
        gen_json = json.dumps({"record_count": 4, "columns": ["order_id", "order_date"]})
        val_json = json.dumps(
            {
                "record_count": 4,
                "null_counts": {"order_id": 0, "order_date": 1},
                "dtypes": {},
                "duplicates": 0,
                "schema_compliance": 100.0,
                "completeness": 87.5,
                "uniqueness": 100.0,
                "overall_quality": 95.8,
                "missing_columns": [],
            }
        )
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{gen_json}"),
            _make_stream_response("transform executed"),
            _make_stream_response(f"VALIDATION:{val_json}"),
        ]

        result = runner.run_test(CANONICAL_TRANSFORM, TARGET_SCHEMA, TARGET_SCHEMA)

        transform_code = mock_client.invoke_code_interpreter.call_args_list[1][1]["arguments"]["code"]
        assert EXPECTED_TO_DATETIME in transform_code
        assert "withColumn" not in transform_code
        assert result["execution_successful"] is True
        assert "to_date" in result["conversion_report"]["constructs_converted"]
