# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for CodeInterpreterTestRunner (mocked boto3)."""

import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from code_interpreter_runner import (
    NOT_VERIFIED_REASON_NOT_YET,
    NOT_VERIFIED_REASON_PERMANENT,
    CodeInterpreterTestRunner,
    classify_conversion_refusal,
)

SOURCE_SCHEMA = {
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "amount": {"type": "float"},
    }
}

TARGET_SCHEMA = {
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "amount": {"type": "float"},
        "status": {"type": "string"},
    }
}

TRANSFORM_BLOCK = (
    'df_transformed = df.withColumn("name", lower(col("name")))\n'
    'df_transformed = df_transformed.withColumnRenamed("amount", "total")'
)


def _make_stream_response(text: str, stderr: str = "", exit_code: int = 0) -> dict:
    """Build a fake invoke_code_interpreter response with structuredContent."""
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


def _make_legacy_stream_response(text: str) -> dict:
    """Build a fake response using only legacy content[].text (no structuredContent)."""
    return {"stream": [{"result": {"content": [{"type": "text", "text": text}]}}]}


@pytest.fixture
def mock_client():
    """Return a mocked bedrock-agentcore client."""
    client = MagicMock()
    client.start_code_interpreter_session.return_value = {"sessionId": "sess-123"}
    client.stop_code_interpreter_session.return_value = {}
    return client


@pytest.fixture
def runner(mock_client):
    """Return a CodeInterpreterTestRunner with a mocked client."""
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = mock_client
        r = CodeInterpreterTestRunner(region="us-west-2")
        r.client = mock_client
        return r


class TestStartSession:
    def test_returns_session_id(self, runner, mock_client):
        sid = runner._start_session()
        assert sid == "sess-123"
        mock_client.start_code_interpreter_session.assert_called_once()

    def test_passes_correct_params(self, runner, mock_client):
        runner._start_session()
        call_kwargs = mock_client.start_code_interpreter_session.call_args[1]
        assert call_kwargs["codeInterpreterIdentifier"] == "aws.codeinterpreter.v1"
        assert call_kwargs["sessionTimeoutSeconds"] == 300


class TestInvoke:
    def test_extracts_text_from_structured_content(self, runner, mock_client):
        mock_client.invoke_code_interpreter.return_value = _make_stream_response("hello world")
        runner.session_id = "sess-123"
        result = runner._invoke("print('hello')")
        assert result["stdout"] == "hello world"
        assert result["failed"] is False
        assert result["exit_code"] == 0
        assert result["stderr"] == ""

    def test_falls_back_to_content_text(self, runner, mock_client):
        mock_client.invoke_code_interpreter.return_value = _make_legacy_stream_response("legacy output")
        runner.session_id = "sess-123"
        result = runner._invoke("print('legacy')")
        assert result["stdout"] == "legacy output"
        assert result["failed"] is False

    def test_handles_empty_stream(self, runner, mock_client):
        mock_client.invoke_code_interpreter.return_value = {"stream": []}
        runner.session_id = "sess-123"
        result = runner._invoke("pass")
        assert result["stdout"] == ""
        assert result["failed"] is False

    def test_handles_multiple_events(self, runner, mock_client):
        """structuredContent path returns on first event with structuredContent."""
        mock_client.invoke_code_interpreter.return_value = _make_stream_response("combined")
        runner.session_id = "sess-123"
        result = runner._invoke("code")
        assert "combined" in result["stdout"]

    # ── P28 R10.1: exit code / traceback classification ──────────────

    def test_nonzero_exit_code_marks_failed(self, runner, mock_client):
        mock_client.invoke_code_interpreter.return_value = _make_stream_response("", stderr="boom", exit_code=2)
        runner.session_id = "sess-123"
        result = runner._invoke("raise SystemExit(2)")
        assert result["failed"] is True
        assert result["exit_code"] == 2
        assert "exit code 2" in result["failure_reason"]

    def test_traceback_on_stderr_with_exit_zero_marks_failed(self, runner, mock_client):
        stderr = 'Traceback (most recent call last):\n  File "<stdin>", line 1\nValueError: bad'
        mock_client.invoke_code_interpreter.return_value = _make_stream_response(
            "partial output", stderr=stderr, exit_code=0
        )
        runner.session_id = "sess-123"
        result = runner._invoke("code")
        assert result["failed"] is True
        assert result["exit_code"] == 0
        assert "traceback" in result["failure_reason"].lower()
        assert "ValueError: bad" in result["stderr"]

    def test_benign_stderr_without_traceback_not_failed(self, runner, mock_client):
        """Deprecation warnings on stderr must NOT fail the run (R10.1)."""
        stderr = "DeprecationWarning: np.bool is deprecated, use bool instead"
        mock_client.invoke_code_interpreter.return_value = _make_stream_response(
            "GENERATED:{}", stderr=stderr, exit_code=0
        )
        runner.session_id = "sess-123"
        result = runner._invoke("code")
        assert result["failed"] is False
        assert result["stderr"] == stderr

    def test_legacy_text_with_traceback_marks_failed(self, runner, mock_client):
        """Legacy shape has no stderr stream — a traceback in the text fails."""
        text = 'Traceback (most recent call last):\n  File "x"\nKeyError: nope'
        mock_client.invoke_code_interpreter.return_value = _make_legacy_stream_response(text)
        runner.session_id = "sess-123"
        result = runner._invoke("code")
        assert result["failed"] is True


class TestStderrBounding:
    def test_short_stderr_unchanged(self, runner):
        assert runner._bound_stderr("short error") == "short error"

    def test_long_stderr_truncated_keeps_head_and_tail(self, runner):
        long_stderr = "H" * 3000 + "M" * 5000 + "T" * 3000
        bounded = runner._bound_stderr(long_stderr)
        assert len(bounded) < len(long_stderr)
        assert bounded.startswith("H" * 100)
        assert bounded.endswith("T" * 100)
        assert "chars truncated" in bounded

    def test_result_stderr_is_bounded(self, runner, mock_client):
        """stderr carried in the structured result is bounded (flows to DynamoDB)."""
        huge_stderr = "x" * 50_000 + "\nTraceback (most recent call last):\nValueError: end"
        mock_client.invoke_code_interpreter.return_value = _make_stream_response("", stderr=huge_stderr, exit_code=1)
        runner.session_id = "sess-123"
        result = runner._invoke("code")
        assert result["failed"] is True
        assert len(result["stderr"]) < 5000
        # Tail is preserved — the exception message survives truncation
        assert "ValueError: end" in result["stderr"]


class TestStopSession:
    def test_stops_active_session(self, runner, mock_client):
        runner.session_id = "sess-123"
        runner._stop_session()
        mock_client.stop_code_interpreter_session.assert_called_once()
        assert runner.session_id is None

    def test_noop_when_no_session(self, runner, mock_client):
        runner.session_id = None
        runner._stop_session()
        mock_client.stop_code_interpreter_session.assert_not_called()

    def test_handles_stop_error_gracefully(self, runner, mock_client):
        runner.session_id = "sess-123"
        mock_client.stop_code_interpreter_session.side_effect = Exception("timeout")
        runner._stop_session()  # Should not raise
        assert runner.session_id is None


class TestParseResults:
    def test_parses_generated_and_validation(self, runner):
        output = (
            'GENERATED:{"record_count": 100, "columns": ["id", "name"]}\n'
            'VALIDATION:{"record_count": 95, "null_counts": {"id": 0}, "dtypes": {"id": "int64"}, '
            '"duplicates": 2, "schema_compliance": 100.0, "completeness": 98.0, '
            '"uniqueness": 97.9, "overall_quality": 98.6, "missing_columns": []}'
        )
        result = runner._parse_results(output)
        assert result["execution_successful"] is True
        assert result["executed"] is True
        assert result["exit_code"] == 0
        assert result["input_records"] == 100
        assert result["output_records"] == 95
        assert result["data_quality_results"]["completeness_score"] == 98.0

    def test_handles_validation_error(self, runner):
        output = 'VALIDATION:{"error": "no dataframe in scope"}'
        result = runner._parse_results(output)
        assert result["execution_successful"] is False
        assert "no dataframe" in result["error_details"]

    def test_handles_no_markers(self, runner):
        result = runner._parse_results("some random output")
        assert result["execution_successful"] is False
        assert result["input_records"] == 0


_GEN_JSON = json.dumps({"record_count": 100, "columns": ["id", "name", "amount"]})
_VAL_JSON = json.dumps(
    {
        "record_count": 95,
        "null_counts": {},
        "dtypes": {},
        "duplicates": 0,
        "schema_compliance": 100.0,
        "completeness": 100.0,
        "uniqueness": 100.0,
        "overall_quality": 100.0,
        "missing_columns": [],
    }
)

_TRACEBACK_STDERR = (
    "Traceback (most recent call last):\n  File \"<stdin>\", line 3, in <module>\nKeyError: 'missing_column'"
)


class TestRunTest:
    def test_full_lifecycle(self, runner, mock_client):
        """Exit 0 + clean stderr on every step → success, executed=True."""
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("transform executed"),
            _make_stream_response(f"VALIDATION:{_VAL_JSON}"),
        ]

        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is True
        assert result["executed"] is True
        assert result["exit_code"] == 0
        assert result["stderr"] == ""
        assert result["input_records"] == 100
        assert result["output_records"] == 95
        # Session should be stopped
        mock_client.stop_code_interpreter_session.assert_called_once()

    def test_success_result_carries_conversion_report(self, runner, mock_client):
        """P28 R10.2: the conversion report flows into the result."""
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("transform executed"),
            _make_stream_response(f"VALIDATION:{_VAL_JSON}"),
        ]

        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        report = result["conversion_report"]
        assert report["input_lines"] == 2
        assert report["output_lines"] == 2
        assert "withColumn" in report["constructs_converted"]
        assert "withColumnRenamed" in report["constructs_converted"]

    def test_executed_transform_is_converted_pandas(self, runner, mock_client):
        """The code sent to the interpreter for step 2 is the CONVERTED
        transform — including the canonical first-transform shape
        (df_transformed = df.withColumn(...)) that the old self-reference
        regex silently commented out (P28 R10.3)."""
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("transform executed"),
            _make_stream_response(f"VALIDATION:{_VAL_JSON}"),
        ]

        runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        transform_code = mock_client.invoke_code_interpreter.call_args_list[1][1]["arguments"]["code"]
        assert 'df_transformed = df.copy(); df_transformed["name"] = df["name"].str.lower()' in transform_code
        assert "# UNCONVERTED" not in transform_code
        assert "withColumn" not in transform_code

    def test_stops_session_on_error(self, runner, mock_client):
        mock_client.invoke_code_interpreter.side_effect = Exception("boom")
        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is False
        assert result["executed"] is False
        assert "boom" in result["error_details"]
        mock_client.stop_code_interpreter_session.assert_called_once()

    # ── P28 R10.1: failure gating in the full lifecycle ───────────────

    def test_transform_nonzero_exit_fails_run_with_exit_code(self, runner, mock_client):
        """exit != 0 on the transform step → failure, exit_code carried."""
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("", stderr="SyntaxError: invalid syntax", exit_code=1),
        ]

        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is False
        assert result["executed"] is True
        assert result["exit_code"] == 1
        assert "SyntaxError" in result["stderr"]
        assert "transform execution failed" in result["error_details"]
        # Only 2 invocations — validation never ran after the failure
        assert mock_client.invoke_code_interpreter.call_count == 2
        mock_client.stop_code_interpreter_session.assert_called_once()

    def test_transform_traceback_with_exit_zero_fails_run(self, runner, mock_client):
        """Traceback on stderr with exit 0 → failure (no warn-and-continue)."""
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("some output", stderr=_TRACEBACK_STDERR, exit_code=0),
        ]

        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is False
        assert result["executed"] is True
        assert result["exit_code"] == 0
        assert "KeyError: 'missing_column'" in result["stderr"]
        assert "traceback" in result["error_details"].lower()
        assert mock_client.invoke_code_interpreter.call_count == 2

    def test_benign_stderr_does_not_fail_run(self, runner, mock_client):
        """Deprecation warning on stderr (no traceback) → still success."""
        warning = "FutureWarning: DataFrame.append is deprecated"
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("transform executed", stderr=warning),
            _make_stream_response(f"VALIDATION:{_VAL_JSON}"),
        ]

        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is True
        assert result["executed"] is True
        assert result["exit_code"] == 0
        # The benign stderr is surfaced for diagnostics, not swallowed
        assert warning in result["stderr"]

    def test_test_data_generation_failure_fails_run(self, runner, mock_client):
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response("", stderr=_TRACEBACK_STDERR, exit_code=1),
        ]

        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is False
        assert "test data generation failed" in result["error_details"]
        assert mock_client.invoke_code_interpreter.call_count == 1

    def test_validation_step_failure_fails_run(self, runner, mock_client):
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("transform executed"),
            _make_stream_response("", stderr=_TRACEBACK_STDERR, exit_code=0),
        ]

        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is False
        assert "output validation failed" in result["error_details"]

    def test_failure_result_keeps_caller_contract_keys(self, runner, mock_client):
        """aggregate_check_results consumes execution_successful/error_details/
        input_records/output_records — all present on the failure shape."""
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("", stderr=_TRACEBACK_STDERR, exit_code=1),
        ]

        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        for key in (
            "execution_successful",
            "error_details",
            "input_records",
            "output_records",
            "data_quality_results",
            "executed",
            "exit_code",
            "stderr",
            "conversion_report",
        ):
            assert key in result
        # Conversion succeeded before the execution failure — report present
        assert result["conversion_report"] is not None


class TestConversionFailure:
    """P28 R10.2 — an unconvertible transform produces an honest structured
    failure naming the construct; nothing runs, nothing is commented out."""

    UNCONVERTIBLE_BLOCK = (
        'df_transformed = df.withColumn("name", lower(col("name")))\n'
        'df_transformed = df_transformed.groupBy("name").agg({"amount": "sum"})'
    )

    def test_conversion_error_returns_structured_failure(self, runner, mock_client):
        result = runner.run_test(self.UNCONVERTIBLE_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is False
        # The transform never executed — no session, no exit code
        assert result["executed"] is False
        assert result["exit_code"] is None
        assert result["conversion_report"] is None

    def test_conversion_error_names_construct_and_line(self, runner, mock_client):
        result = runner.run_test(self.UNCONVERTIBLE_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert "conversion failed" in result["error_details"]
        assert "groupBy" in result["error_details"]
        assert "line 2" in result["error_details"]

    def test_conversion_error_starts_no_session(self, runner, mock_client):
        """Conversion happens BEFORE the session — nothing is started or invoked."""
        runner.run_test(self.UNCONVERTIBLE_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        mock_client.start_code_interpreter_session.assert_not_called()
        mock_client.invoke_code_interpreter.assert_not_called()

    def test_conversion_failure_keeps_caller_contract_keys(self, runner, mock_client):
        result = runner.run_test(self.UNCONVERTIBLE_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        for key in (
            "execution_successful",
            "error_details",
            "input_records",
            "output_records",
            "data_quality_results",
            "executed",
            "exit_code",
            "stderr",
            "conversion_report",
        ):
            assert key in result


class TestRefusalIsANonVerification:
    """p32 2.20/2.23 (C4) — a refusal is a NON-VERIFICATION, not a failure.

    The proxy could not express the transform, so nothing about the transform
    was proven either way. The refusal itself is unchanged (P28 R10.2, p32
    3.7): the construct is named, nothing is commented out or dropped. What
    the outcome now carries is the downstream classification (p32 V4).

    Validates: Requirements 2.20, 2.23, 2.24; 3.22
    """

    # p32 2.23 reason (b) — a construct the converter cannot YET express, i.e.
    # a defect in the checker rather than something about the transform.
    #
    # This was p32 1.7 (two chained `.withColumn` on one statement) until task
    # T10's AST converter made that shape CONVERT, which is what 2.7 requires.
    # The assertions below are unchanged; only the input had to move to a shape
    # that still refuses — a chain that mixes frame operations, which is the
    # same defect class as `pyspark_templates/README.md`'s parenthesised fluent
    # worked example (1.12), collapsed onto one line so the refusal still
    # reports `withColumn` on line 1.
    NOT_YET_BLOCK = 'df_transformed = df.withColumnRenamed("old", "new").withColumn("a", trim(col("x"))).drop("tmp")'
    # p32 3.7 — no single-frame pandas equivalent; keeps refusing after the
    # AST rewrite, and is nothing to fix.
    PERMANENT_BLOCK = 'df_transformed = df.groupBy("state").agg(count("name"))'

    def test_refusal_is_marked_not_verified_by_proxy(self, runner, mock_client):
        result = runner.run_test(self.NOT_YET_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["not_verified_by_proxy"] is True
        # Nothing was verified, so the execution did not succeed either.
        assert result["execution_successful"] is False
        assert result["executed"] is False

    def test_refusal_names_the_construct_and_the_line(self, runner, mock_client):
        result = runner.run_test(self.NOT_YET_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["not_verified_construct"] == "withColumn"
        assert result["not_verified_line_number"] == 1
        assert "withColumn" in result["not_verified_source_line"]

    def test_a_converter_gap_reads_as_not_yet_expressible(self, runner, mock_client):
        """2.23 — the reason that IS something to fix."""
        result = runner.run_test(self.NOT_YET_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["not_verified_reason"] == NOT_VERIFIED_REASON_NOT_YET

    def test_an_inexpressible_construct_reads_as_permanent(self, runner, mock_client):
        """2.23 — the reason that is NOT something to fix."""
        result = runner.run_test(self.PERMANENT_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["not_verified_construct"] == "groupBy"
        assert result["not_verified_reason"] == NOT_VERIFIED_REASON_PERMANENT

    def test_an_unrecognised_construct_defaults_to_not_yet_expressible(self):
        """2.23 — defaulting the other way would let a real converter gap read
        as "nothing to fix", which is the confusion this clause forbids."""
        assert classify_conversion_refusal("someBrandNewMethod") == NOT_VERIFIED_REASON_NOT_YET

    def test_a_real_execution_failure_is_not_reclassified(self, runner, mock_client):
        """3.22 — the transform RAN and failed: still a failure, no marker."""
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("", stderr=_TRACEBACK_STDERR, exit_code=1),
        ]
        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)

        assert result["execution_successful"] is False
        assert result["executed"] is True
        assert "not_verified_by_proxy" not in result

    def test_a_missing_code_interpreter_run_is_not_reclassified(self):
        """3.22 — that path proved nothing AND named no reason."""
        with patch("code_interpreter_runner.boto3") as mock_boto:
            mock_boto.client.side_effect = Exception("no service")
            unavailable = CodeInterpreterTestRunner(region="us-west-2")
        result = unavailable.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)

        assert result["execution_successful"] is False
        assert "not_verified_by_proxy" not in result


class TestGracefulFallback:
    def test_unavailable_client_returns_empty(self):
        with patch("code_interpreter_runner.boto3") as mock_boto:
            mock_boto.client.side_effect = Exception("no service")
            runner = CodeInterpreterTestRunner(region="us-west-2")
        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is False
        assert "not available" in result["error_details"]
        # Nothing ran — executed=False with no exit code
        assert result["executed"] is False
        assert result["exit_code"] is None
        assert result["stderr"] == ""
        assert result["conversion_report"] is None


class TestBuildTestDataCode:
    def test_generates_code_with_schema_fields(self, runner):
        code = runner._build_test_data_code(SOURCE_SCHEMA)
        assert "import pandas" in code
        assert '"id"' in code
        assert '"name"' in code
        assert "GENERATED:" in code

    def test_handles_empty_schema(self, runner):
        code = runner._build_test_data_code({})
        assert "import pandas" in code
        assert "GENERATED:" in code


class _FakeSeries:
    """Minimal pandas.Series stand-in for the surface the validation code uses."""

    def __init__(self, values):
        self._values = list(values)
        self.dtype = "object"

    def isna(self):
        return _FakeSeries([v is None for v in self._values])

    def sum(self):
        return sum(1 for v in self._values if v is True)


class _FakeDataFrame:
    """Minimal pandas.DataFrame stand-in — enough to execute the generated
    validation code in-process without a pandas dependency."""

    def __init__(self, data: dict):
        self._data = {k: list(v) for k, v in data.items()}
        self.columns = list(self._data.keys())

    def __len__(self):
        return len(next(iter(self._data.values()), []))

    def __getitem__(self, col):
        return _FakeSeries(self._data[col])

    def duplicated(self):
        rows = list(zip(*self._data.values(), strict=True))
        seen: set = set()
        flags = []
        for row in rows:
            flags.append(row in seen)
            seen.add(row)
        return _FakeSeries(flags)


def _run_validation_code(code: str, scope_vars: dict) -> tuple[dict | None, Exception | None]:
    """Execute generated validation code in-process; capture the VALIDATION
    marker (via a print shim) and any raised exception."""
    printed: list[str] = []
    namespace = {"print": lambda *a: printed.append(" ".join(str(x) for x in a)), **scope_vars}
    error: Exception | None = None
    try:
        exec(code, namespace)
    except Exception as exc:
        error = exc
    marker = next((line for line in printed if line.startswith("VALIDATION:")), None)
    parsed = json.loads(marker[len("VALIDATION:") :]) if marker else None
    return parsed, error


class TestBuildValidationCode:
    def test_generates_validation_with_expected_cols(self, runner):
        code = runner._build_validation_code(TARGET_SCHEMA)
        assert "VALIDATION:" in code
        assert "record_count" in code
        assert "schema_compliance" in code

    # ── P28 R10.4: validate the TRANSFORMED dataframe, never raw df ────

    def test_binds_df_transformed_as_primary_target(self, runner):
        """_df = df_transformed is the binding; no `_df = df` primary
        binding (the raw test-data frame) remains anywhere."""
        code = runner._build_validation_code(TARGET_SCHEMA)
        assert "_df = df_transformed" in code
        assert re.search(r"_df\s*=\s*df\s*($|[^_.\w])", code, re.MULTILINE) is None

    def test_absent_df_transformed_raises_and_prints_explicit_error(self, runner):
        """Only raw `df` in scope (transform produced nothing) → the code
        prints the explicit VALIDATION error and raises — no silent
        fallback to validating the input frame."""
        code = runner._build_validation_code(TARGET_SCHEMA)
        raw_df = _FakeDataFrame({"id": [1, 2], "name": ["a", "b"], "amount": [1.0, 2.0]})

        parsed, error = _run_validation_code(code, {"json": json, "df": raw_df})

        assert error is not None, "validation must raise when df_transformed is absent"
        assert "transform did not produce df_transformed" in str(error)
        assert parsed == {"error": "transform did not produce df_transformed"}

    def test_present_df_transformed_validates_transformed_frame(self, runner):
        """df and df_transformed differ — the reported metrics must reflect
        df_transformed (row count, columns), proving the transformed frame
        is the thing validated."""
        code = runner._build_validation_code(TARGET_SCHEMA)
        raw_df = _FakeDataFrame({"raw_col": [1, 2, 3, 4, 5]})
        transformed = _FakeDataFrame(
            {
                "id": [1, 2],
                "name": ["a", "b"],
                "amount": [1.0, 2.0],
                "status": ["ok", "ok"],
            }
        )

        parsed, error = _run_validation_code(code, {"json": json, "df": raw_df, "df_transformed": transformed})

        assert error is None
        assert parsed is not None and "error" not in parsed
        assert parsed["record_count"] == 2  # df_transformed's 2 rows, not df's 5
        assert set(parsed["null_counts"]) == {"id", "name", "amount", "status"}
        assert parsed["missing_columns"] == []
        assert parsed["schema_compliance"] == 100.0


class TestValidationFailsRunWhenDfTransformedAbsent:
    """P28 R10.4 end-to-end through the runner: the NameError traceback from
    the validation step fails the run honestly (via R10.1 gating)."""

    _ABSENT_TRACEBACK = (
        "Traceback (most recent call last):\n"
        '  File "<stdin>", line 2, in <module>\n'
        "NameError: name 'df_transformed' is not defined\n"
        "RuntimeError: transform did not produce df_transformed"
    )

    def test_validation_step_nameerror_fails_run(self, runner, mock_client):
        mock_client.invoke_code_interpreter.side_effect = [
            _make_stream_response(f"GENERATED:{_GEN_JSON}"),
            _make_stream_response("transform executed"),
            _make_stream_response(
                'VALIDATION:{"error": "transform did not produce df_transformed"}',
                stderr=self._ABSENT_TRACEBACK,
                exit_code=1,
            ),
        ]

        result = runner.run_test(TRANSFORM_BLOCK, SOURCE_SCHEMA, TARGET_SCHEMA)
        assert result["execution_successful"] is False
        assert result["executed"] is True
        assert "output validation failed" in result["error_details"]
        # The explicit message survives into the failure details via stderr
        assert "transform did not produce df_transformed" in result["error_details"]

    def test_parse_results_surfaces_explicit_error(self, runner):
        output = 'VALIDATION:{"error": "transform did not produce df_transformed"}'
        result = runner._parse_results(output)
        assert result["execution_successful"] is False
        assert result["error_details"] == "transform did not produce df_transformed"


# Imported here rather than in the block at the top of the file: p33's spec
# documents line numbers in this file (`:559-570`, `:631`, `:677`), and adding a
# line above them would silently invalidate every one of those references.
import code_interpreter_runner


class TestFrameWidthRefusal:
    """p33 2.10/2.11 (D12): the frame is built whole, or the run is refused.

    Appended by task 4.10. The property-level coverage lives in
    ``test_test_frame_width_prop.py`` (every declared field becomes a column;
    an over-ceiling schema refuses before a session) and the downstream
    reporting in ``test_schema_compliance_emission.py``. What is left to this
    file, which owns the runner's shapes directly, is the pair those two do not
    reach: ``_frame_truncation_results`` for both reasons, and the
    ``no_source_schema`` refusal at the ``run_test`` level — the case the
    preservation test deliberately does NOT assert on the builder, because the
    builder's ``{id, value}`` fallback is unchanged and it is ``run_test`` that
    stops reaching it.
    """

    def test_the_ceiling_is_the_documented_512(self):
        """The constant, not a literal — the number is defensible (D12)."""
        assert code_interpreter_runner._MAX_TEST_FRAME_COLUMNS == 512

    def test_a_schema_exactly_at_the_ceiling_is_not_refused(self):
        """The boundary: 512 is buildable, 513 is not."""
        ceiling = code_interpreter_runner._MAX_TEST_FRAME_COLUMNS
        at_ceiling = {"properties": {f"c{i}": {"type": "string"} for i in range(ceiling)}}
        assert CodeInterpreterTestRunner._frame_refusal(at_ceiling) is None
        one_over = {"properties": {f"c{i}": {"type": "string"} for i in range(ceiling + 1)}}
        assert CodeInterpreterTestRunner._frame_refusal(one_over) == (
            code_interpreter_runner.NOT_VERIFIED_REASON_FRAME_TRUNCATED,
            ceiling + 1,
        )

    def test_a_wide_schema_below_the_ceiling_builds_every_column(self, runner):
        """The old cap was 20 — 100 columns must all be declared now."""
        declared = [f"c{i:03d}" for i in range(100)]
        code = runner._build_test_data_code({"properties": {n: {"type": "string"} for n in declared}})
        assert code.count('": [') == 100
        for name in declared:
            assert f'"{name}": [' in code

    @pytest.mark.parametrize(
        "source_schema",
        [{}, {"properties": {}}, {"fields": {}}, None],
        ids=["no key", "empty properties", "empty fields", "None"],
    )
    def test_an_absent_source_schema_refuses_rather_than_framing_id_value(self, source_schema):
        """Requirement 2.11's second reason.

        The builder still produces the ``{id, value}`` fallback for these inputs
        (pinned by ``test_handles_empty_schema`` and by the preservation test) —
        what changes is that ``run_test`` never gets there, because a two-column
        frame is not the pipeline's input and a transform exercised against it
        proves nothing.
        """
        assert CodeInterpreterTestRunner._frame_refusal(source_schema) == (
            code_interpreter_runner.NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA,
            0,
        )

    def test_no_source_schema_refuses_before_a_session_and_reports_the_reason(self, runner, mock_client):
        result = runner.run_test(TRANSFORM_BLOCK, {}, TARGET_SCHEMA)

        assert mock_client.start_code_interpreter_session.call_count == 0
        assert mock_client.invoke_code_interpreter.call_count == 0
        assert result["not_verified_by_harness"] is True
        assert result["not_verified_reason"] == "no_source_schema"
        assert result["executed"] is False
        assert result["execution_successful"] is False
        assert result["data_quality_results"] == {}
        # C14 — not the proxy's shape.
        assert "not_verified_by_proxy" not in result
        # Conversion ran and succeeded, so its report is real (run_test's
        # documented contract: None only when conversion failed or never ran).
        assert result["conversion_report"] is not None

    def test_an_unconvertible_transform_still_reports_the_proxy_refusal(self, runner, mock_client):
        """Ordering: the conversion refusal keeps precedence over the frame guard.

        A construct the proxy cannot express is not a frame problem, and the
        proxy refusal names the construct — the more actionable of the two. Both
        refuse before a session, so only the reported reason distinguishes them.
        """
        result = runner.run_test('df_transformed = df.groupBy("a").count()', {}, TARGET_SCHEMA)

        assert mock_client.start_code_interpreter_session.call_count == 0
        assert result.get("not_verified_by_proxy") is True
        assert "not_verified_by_harness" not in result

    def test_frame_truncation_results_carries_the_count_the_ceiling_and_no_quality(self):
        result = CodeInterpreterTestRunner._frame_truncation_results(
            640, 512, code_interpreter_runner.NOT_VERIFIED_REASON_FRAME_TRUNCATED
        )

        assert result["not_verified_by_harness"] is True
        assert result["not_verified_reason"] == "test_frame_truncated"
        assert result["not_verified_field_count"] == 640
        assert result["not_verified_frame_ceiling"] == 512
        assert result["executed"] is False
        assert result["exit_code"] is None
        assert result["execution_successful"] is False
        # C13 — the empty map is what suppresses the schema-compliance score.
        assert result["data_quality_results"] == {}
        assert "640" in result["error_details"] and "512" in result["error_details"]

    def test_frame_truncation_results_says_no_source_schema_in_words(self):
        result = CodeInterpreterTestRunner._frame_truncation_results(
            0, 512, code_interpreter_runner.NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA
        )

        assert result["not_verified_reason"] == "no_source_schema"
        assert result["not_verified_field_count"] == 0
        assert "no source schema" in result["error_details"]
        assert result["data_quality_results"] == {}


# ── p40 task 7.2 — the reason PRECEDENCE in _conversion_failure_results ──
#
# Appended at the END of the file, and these two imports are here rather than in
# the block at the top, for the same reason the `import code_interpreter_runner`
# above gives: p33's spec documents line numbers in this file (`:559-570`,
# `:631`, `:677`) and p40's own `design.md` cites `:627`, so a line added above
# any of them silently invalidates the reference.
from pyspark_to_pandas import (
    REFUSAL_REASON_SCALAR_LITERAL,
    ConversionError,
)


class TestConversionRefusalReasonPrecedence:
    """p40 R4 — a refusal that names its own reason is taken at its word.

    Task 7.1 made `_conversion_failure_results` read
    ``reason = exc.reason or classify_conversion_refusal(exc.construct)``
    (`code_interpreter_runner.py:650`) and changed nothing else on that path.
    This class is what makes that precedence VERIFIED BY THE SUITE rather than
    by inspection.

    WHAT THIS COVERS THAT NOTHING ELSE DID. `test_converter_refusal_reason.py`
    (task 4.4) stops at the converter: it asserts a fence refusal carries
    ``reason == scalar_literal_receiver`` and that a pre-p40 refusal carries
    ``reason is None``. What it never does is hand either exception to the
    runner, so nothing asserted which of the two inputs — the carried reason or
    the construct name — wins when they disagree. They DO disagree on every
    fence refusal: `cast`, `trim`, `lower`, `upper`, `regexp_replace`,
    `concat_ws`, `to_date` and `alias` are all names
    `classify_conversion_refusal` maps to `not_yet_expressible`, which is why
    the reason had to ride on the exception at all (`ConversionError`'s
    docstring; design §4.4). The `or` could be inverted, or dropped, and the
    only test that would fail before this class existed was a throwaway probe.

    `classify_conversion_refusal` is deliberately NOT exercised as a subject
    here — it is untouched by this spec (design §4.4) and its own property tests
    keep their meaning. It appears only as the ORACLE for the `reason is None`
    half: the assertion is that the fallback still returns exactly what that
    function returns, not that it returns some transcribed string.

    ORACLE RULE FOR THIS CLASS. Expectations come from `design.md` §4.4 and from
    `classify_conversion_refusal` itself for the fallback half — neither of the
    two rules that face each other in this directory. Nothing here is
    transcribed from current output (task 2.1's rule) and nothing here needs
    Spark semantics (tasks 3.1-3.3' rule); the subject is a classification, not
    a translation.

    EXPECTED-RED WINDOW: closed. Written after 7.1 landed, so every assertion is
    expected GREEN on its first run. A red here is a finding about 7.1.

    Validates: Requirements R4
    """

    #: A refusal the fence produces: valid Spark, and the construct that owns the
    #: member is `cast` — a name `classify_conversion_refusal` maps to
    #: `not_yet_expressible`, so the carried reason and the construct-keyed
    #: classification disagree. That disagreement is the whole subject.
    _FENCE_LINE = 'df_transformed = df.withColumn("mobile_phone", lit(0).cast("int"))'
    #: A refusal that predates the fence and is on the permanent list.
    _PERMANENT_LINE = 'df_transformed = df.groupBy("state").agg(count("name"))'
    #: A refusal that predates the fence and is NOT on the permanent list — the
    #: other side of the fallback, so `classify_conversion_refusal`'s two
    #: outcomes are both covered rather than only the default.
    _NOT_YET_LINE = 'df_transformed = df.withColumn("a", posexplode(col("b")))'

    def test_the_runner_reason_is_the_converter_constant_not_a_second_spelling(self):
        """One source of truth: an alias binding, never a re-typed literal.

        The converter owns the string because the dependency runs converter →
        runner (`code_interpreter_runner.py:39-43`), so identity is assertable —
        and a rename on the producing side that left a stale copy here would
        fail this, where a value comparison against a transcribed literal would
        not.
        """
        assert code_interpreter_runner.NOT_VERIFIED_REASON_SCALAR_LITERAL is REFUSAL_REASON_SCALAR_LITERAL
        assert code_interpreter_runner.NOT_VERIFIED_REASON_SCALAR_LITERAL == "scalar_literal_receiver"
        # The third reason must stay distinguishable from the two p32 reasons
        # (`bugfix.md` 2.13, 3.7) — those describe a converter that refused, this
        # one a converter that would otherwise have lied.
        assert code_interpreter_runner.NOT_VERIFIED_REASON_SCALAR_LITERAL not in (
            NOT_VERIFIED_REASON_PERMANENT,
            NOT_VERIFIED_REASON_NOT_YET,
        )

    def test_a_refusal_carrying_a_reason_classifies_as_scalar_literal_receiver(self):
        exc = ConversionError("cast", self._FENCE_LINE, 3, reason=REFUSAL_REASON_SCALAR_LITERAL)

        results = CodeInterpreterTestRunner._conversion_failure_results(exc)

        assert results["not_verified_reason"] == "scalar_literal_receiver"
        assert results["not_verified_reason"] == code_interpreter_runner.NOT_VERIFIED_REASON_SCALAR_LITERAL

    def test_a_carried_reason_wins_over_the_construct_keyed_classification(self):
        """The precedence itself — the assertion this task exists to add.

        `cast` is not on the permanent list, so the construct-keyed path would
        call this refusal a converter gap to close. It is not one: the generated
        PySpark is valid and there is nothing in it to fix. If the `or` were
        inverted, or the reason ignored, the result below would silently read
        `not_yet_expressible` and the third reason would never reach a reviewer.
        """
        exc = ConversionError("cast", self._FENCE_LINE, 1, reason=REFUSAL_REASON_SCALAR_LITERAL)

        results = CodeInterpreterTestRunner._conversion_failure_results(exc)

        assert classify_conversion_refusal("cast") == NOT_VERIFIED_REASON_NOT_YET
        assert results["not_verified_reason"] != classify_conversion_refusal(exc.construct)
        assert results["not_verified_reason"] == REFUSAL_REASON_SCALAR_LITERAL

    @pytest.mark.parametrize(
        ("construct", "line", "expected"),
        [
            ("groupBy", _PERMANENT_LINE, NOT_VERIFIED_REASON_PERMANENT),
            ("posexplode", _NOT_YET_LINE, NOT_VERIFIED_REASON_NOT_YET),
        ],
        ids=["permanently_inexpressible", "not_yet_expressible"],
    )
    def test_a_refusal_without_a_reason_classifies_exactly_as_it_does_today(self, construct, line, expected):
        """Both sides of the fallback, so p32's two reasons stay where they were.

        `exc.reason` is `None` on every refusal that predates the fence
        (`ConversionError`'s parameter is optional and defaulted), so this is the
        half of the precedence that must be a no-op. The expectation is taken
        FROM `classify_conversion_refusal` rather than transcribed, so a change
        to that function moves the test with it instead of pitting the two
        against each other — it is not this spec's subject (design §4.4).
        """
        exc = ConversionError(construct, line, 2)
        assert exc.reason is None

        results = CodeInterpreterTestRunner._conversion_failure_results(exc)

        assert results["not_verified_reason"] == classify_conversion_refusal(construct)
        assert results["not_verified_reason"] == expected

    def test_the_rest_of_the_refusal_contract_is_unchanged_for_the_new_reason(self):
        """7.1 changed the reason and nothing else on this path.

        Asserted on the new reason specifically: the third reason must land in
        the SAME shape as the two that predate it — no new state, no new marker
        (`bugfix.md` 2.13, design B1). The empty `data_quality_results` is
        load-bearing rather than incidental, as on the harness sibling: it is
        what suppresses the `schema_compliance` score for a run where nothing
        executed (`bugfix.md` 3.11, `LGR-b2f79ef-1`).
        """
        exc = ConversionError("trim", self._FENCE_LINE, 7, reason=REFUSAL_REASON_SCALAR_LITERAL)

        results = CodeInterpreterTestRunner._conversion_failure_results(exc)

        assert results["executed"] is False
        assert results["execution_successful"] is False
        assert results["exit_code"] is None
        assert results["stderr"] == ""
        assert results["input_records"] == 0
        assert results["output_records"] == 0
        assert results["data_quality_results"] == {}
        assert results["conversion_report"] is None
        # p32 2.20 — the proxy's non-verification, not the harness's (C14).
        assert results["not_verified_by_proxy"] is True
        assert "not_verified_by_harness" not in results
        # 3.4 — the construct, the offending line and its number all travel.
        assert results["not_verified_construct"] == "trim"
        assert results["not_verified_source_line"] == self._FENCE_LINE
        assert results["not_verified_line_number"] == 7
        # `str(exc)` reaches an operator through `error_details` unchanged.
        assert str(exc) in results["error_details"]

    def test_a_fence_refusal_reports_the_third_reason_through_run_test(self, runner, mock_client):
        """The same precedence through the real converter, end to end.

        The class above hands `_conversion_failure_results` an exception it built
        itself. This one lets the fence raise, so the assertion covers the whole
        producer → reader path — the converter naming its reason, `convert`'s
        `fail` closure forwarding it, and the runner preferring it. The sibling
        cases for the two p32 reasons live in
        `TestRefusalIsANonVerification` above; this is the third.
        """
        result = runner.run_test('df_transformed = df.withColumn("a", trim(lit("x")))', SOURCE_SCHEMA, TARGET_SCHEMA)

        assert result["not_verified_by_proxy"] is True
        assert result["not_verified_reason"] == code_interpreter_runner.NOT_VERIFIED_REASON_SCALAR_LITERAL
        assert result["not_verified_construct"] == "trim"
        # Refused before a session — nothing ran, so nothing was verified.
        assert result["executed"] is False
        mock_client.start_code_interpreter_session.assert_not_called()
        mock_client.invoke_code_interpreter.assert_not_called()


# ── p40 task 8.3 — the third reason's RESULTS SHAPE, not just its values ─────
#
# WHAT TASK 7.2 ALREADY COVERS, so this is not a second copy of it.
# `TestConversionRefusalReasonPrecedence` above asserts, for the new reason: the
# owner↔alias identity, the carried-reason precedence over
# `classify_conversion_refusal`, both sides of the `reason is None` fallback,
# every individual field of `_conversion_failure_results`, and one end-to-end
# `run_test` through the real fence.
#
# WHAT IT DOES NOT COVER, and what this class adds. Every one of those is a
# per-field assertion, so a key ADDED on the new reason's path — a
# `not_verified_scalar_shape` marker, say, or a third state smuggled in beside
# `verified: False` — would satisfy all of them. Design `B1` and `bugfix.md` 2.13
# say the third reason is a third REASON and not a third STATE; that claim is
# about the shape of the dict as a whole, so it needs a whole-dict assertion.
# `LGR-b2f79ef-12` (**template / ratified**) is the precedent being relied on —
# `not_verified_by_harness` got its own name and reason rather than a new field —
# and `LGR-b2f79ef-2` (**template / ratified**) is the one that refused a third
# check FIELD outright, once already.
#
# Held at the PRODUCER seam deliberately. What the reader does with these results
# (the check entry, the status derivation, the approval gate, the report prose) is
# asserted in `test_not_verified_classification_prop.py` and
# `test_comprehensive_report.py`; duplicating it here would give two places to
# fix when the reader changes.
#
# EXPECTED-RED WINDOW: closed. p40 tasks 8.1 and 8.2 have landed, so a red here
# is a finding about the producer, not a test to adjust.


class TestThirdReasonResultsShape:
    """p40 R4 — a third reason changes the reason and nothing else.

    Validates: p40 Requirements R4
    """

    #: One construct, one line, one line number — held IDENTICAL across the pair
    #: so the only difference between the two results dicts is the reason itself.
    #: `trim` is a name `classify_conversion_refusal` maps to
    #: `not_yet_expressible`, so the pair really does differ on the reason.
    _LINE = 'df_transformed = df.withColumn("mobile_phone", trim(lit("x")))'

    def test_the_third_reason_differs_from_a_p32_refusal_only_in_the_reason(self):
        """No new key, no dropped key, no changed value — one string moves.

        The `not_verified_reason` value is the ONLY permitted difference. Asserted
        by removing that key from both dicts and comparing what is left, which is
        stronger than any list of per-field assertions: it also fails on a key
        this test's author never thought to name.
        """
        without_reason = CodeInterpreterTestRunner._conversion_failure_results(ConversionError("trim", self._LINE, 4))
        with_reason = CodeInterpreterTestRunner._conversion_failure_results(
            ConversionError("trim", self._LINE, 4, reason=REFUSAL_REASON_SCALAR_LITERAL)
        )

        # Guard: the two really do carry different reasons, or the comparison
        # below would be an identity and would assert nothing.
        assert without_reason["not_verified_reason"] == NOT_VERIFIED_REASON_NOT_YET
        assert with_reason["not_verified_reason"] == REFUSAL_REASON_SCALAR_LITERAL

        assert with_reason.keys() == without_reason.keys(), (
            f"the third reason's results carry a different key set: "
            f"only-new={sorted(with_reason.keys() - without_reason.keys())}, "
            f"only-old={sorted(without_reason.keys() - with_reason.keys())} — that is a third STATE, which "
            "bugfix.md 2.13 and design B1 refuse (LGR-b2f79ef-2 refused a third check field once already)"
        )
        assert {k: v for k, v in with_reason.items() if k != "not_verified_reason"} == {
            k: v for k, v in without_reason.items() if k != "not_verified_reason"
        }, "the third reason changed something other than the reason on the refusal path"

    def test_all_three_reasons_produce_the_same_refusal_key_set(self):
        """The permanent reason too, so the parity is over all three.

        A different construct, so construct/line/line_number legitimately differ
        and only the KEY SET is comparable. Together with the test above — which
        compares values on a construct-identical pair — the two cover both halves
        of "same shape, different reason".
        """
        shapes = {
            NOT_VERIFIED_REASON_NOT_YET: ConversionError("trim", self._LINE, 4),
            NOT_VERIFIED_REASON_PERMANENT: ConversionError(
                "groupBy", 'df_transformed = df.groupBy("state").agg(count("name"))', 1
            ),
            REFUSAL_REASON_SCALAR_LITERAL: ConversionError("cast", self._LINE, 4, reason=REFUSAL_REASON_SCALAR_LITERAL),
        }

        key_sets = {}
        for expected_reason, exc in shapes.items():
            results = CodeInterpreterTestRunner._conversion_failure_results(exc)
            assert results["not_verified_reason"] == expected_reason, (
                f"{exc.construct!r} classified as {results['not_verified_reason']!r}, not {expected_reason!r} — "
                "this test is no longer comparing the three reasons it names"
            )
            # p32 2.20 / bugfix.md 3.11: the proxy's marker is present and the
            # harness's is not, on every reason. An empty `data_quality_results`
            # is what suppresses the schema-compliance score for a run where
            # nothing executed (`LGR-b2f79ef-1`).
            assert results["not_verified_by_proxy"] is True
            assert "not_verified_by_harness" not in results
            assert results["execution_successful"] is False
            assert results["executed"] is False
            assert results["data_quality_results"] == {}
            key_sets[expected_reason] = frozenset(results)

        assert len(set(key_sets.values())) == 1, (
            f"the three refusal reasons produce different key sets: "
            f"{ {r: sorted(k) for r, k in key_sets.items()} } — a reason must not carry a state"
        )
