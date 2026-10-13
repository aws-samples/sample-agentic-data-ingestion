# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Code Interpreter runner for testing AI-generated transforms.

Starts a Code Interpreter session, generates test data, executes the
pandas-converted transform, validates the output, and tears down the
session — all via the ``bedrock-agentcore`` boto3 client.

P28 R10.1: a non-zero exit code or a Python traceback on stderr from any
execution step fails the test run — those signals are never downgraded to
warnings. The structured result carries ``{executed, exit_code, stderr}``
alongside the existing ``execution_successful``/``error_details`` contract.

P28 R10.2: PySpark→pandas conversion happens BEFORE anything runs. An
unconvertible construct raises ``ConversionError`` inside the converter and
is returned as an honest structured outcome naming the construct — the
transform is never executed with lines commented out. On success the
converter's ``conversion_report`` flows into the result for diagnostics.

p32 2.20: that outcome is a NON-VERIFICATION, not a failed execution. The
pandas converter exists so the transform can be executed as a *proxy* for
Spark; when the proxy cannot express the transform, nothing about the
transform was proven either way, so the result carries
``not_verified_by_proxy`` plus which of 2.23's two reasons applies. A
transform that ran and failed, and a Code Interpreter run that was missing
or unavailable, both stay failures (p32 3.22).

p33 2.11: the same posture covers the TEST FRAME. The frame carries one column
per field the source schema declares, up to ``_MAX_TEST_FRAME_COLUMNS``; a
schema wider than that, or a run with no source schema at all, is refused
before a session is started and returned as ``not_verified_by_harness``. A
frame the harness could not build must never present as a transform that
failed, and must never be scored for schema compliance.
"""

import json
import logging
import time

import boto3
from pyspark_to_pandas import (
    REFUSAL_REASON_SCALAR_LITERAL,
    ConversionError,
    PySpark2PandasConverter,
)

logger = logging.getLogger(__name__)

# P28 R10.1: marker that identifies a Python traceback on stderr. Some
# runtimes exit 0 after printing a traceback from a background/subprocess
# context, so this is checked independently of the exit code.
_TRACEBACK_MARKER = "Traceback (most recent call last"

# Bounds for stderr carried in the structured result — it flows into
# reports and DynamoDB items, so keep head + tail (Python puts the
# exception type/message at the END of a traceback).
_STDERR_HEAD_CHARS = 2000
_STDERR_TAIL_CHARS = 1500

# p32 2.20/2.23 (C4): a refusal to convert is a NON-VERIFICATION, not a failed
# execution — the transform never ran, so nothing about it was proven either
# way. The refusal itself is untouched (P28 R10.2, p32 3.7: never comment out,
# never silently drop, always name the construct); what changes is how the
# refusal is CLASSIFIED downstream (p32 V4).
#
# The two reasons mean different things to a reviewer and must stay
# distinguishable (2.23). Only the second is something to fix.
#: No single-frame pandas equivalent exists — permanent by design. These keep
#: refusing after the AST rewrite and are NOT a defect in this system.
NOT_VERIFIED_REASON_PERMANENT = "permanently_inexpressible"
#: The converter cannot YET express the construct (p32 1.7-1.9) — a defect in
#: this system, and the thing to fix.
NOT_VERIFIED_REASON_NOT_YET = "not_yet_expressible"

# p40 R4: the THIRD proxy reason, and the one shape of refusal the two above
# cannot describe. Those two are about a converter that REFUSED; this one is
# about a converter that would otherwise have LIED — the receiver-shape fence
# declining to apply a Series-only pandas member to a scalar emission
# (`pyspark_to_pandas._require_series`). Nothing in the generated PySpark is
# wrong, so it is neither "nothing to fix, ever" nor "a converter gap to close".
#
#: **An ALIAS, not a second spelling.** The converter owns the string
#: (`pyspark_to_pandas.py:93`) because the dependency runs converter → runner
#: (the import above), so the converter cannot read the two constants beside
#: this one without a cycle. Re-typing the literal here would give the reason two
#: sources of truth that a rename could leave silently disagreeing; binding the
#: name to the imported constant cannot. The local name follows this module's
#: `NOT_VERIFIED_REASON_*` convention, which is what every reader imports.
NOT_VERIFIED_REASON_SCALAR_LITERAL = REFUSAL_REASON_SCALAR_LITERAL

# p33 2.11/D12: the two reasons a non-verification is the TEST HARNESS's rather
# than the pandas proxy's. Kept distinct from the two reasons above because they
# mean something different to a reviewer: the proxy reasons say "the transform
# could not be expressed", these say "no frame could be built to run it
# against". `aggregate_check_results` reports them under a name of their own
# (C14) — reusing `not_verified_by_proxy` would render "construct 'unknown
# construct' on line None" and blame the converter for a width limit.
#: The source schema declares more fields than the frame can carry.
NOT_VERIFIED_REASON_FRAME_TRUNCATED = "test_frame_truncated"
#: No source schema was available, so the only frame buildable is the
#: `{id, value}` fallback — not the pipeline's input, so nothing measured
#: against it would mean anything.
NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA = "no_source_schema"

# p33 D12 / requirement 2.10: the frame carries one column per declared source
# field, bounded here. `num_rows = 100` is fixed in the generated frame code, so
# width is the only variable, and the bound comes from the DynamoDB write path
# rather than from the interpreter:
#
#   * `_build_validation_code` emits `null_counts` and `dtypes` with one entry
#     per transformed column, `_parse_results` puts both into
#     `data_quality_results`, and that lands on the pipeline-runs state row. At
#     ~45 bytes per column per map, 512 columns is ~46 KB across the two —
#     inside DynamoDB's 400 KB item limit with room to spare, and the reason a
#     ceiling exists at all rather than no bound.
#   * The interpreter cost is negligible by comparison: the data is built INSIDE
#     the interpreter, so only ~45-70 bytes of code per column crosses the wire
#     (~30 KB at 512 columns).
#
# Same reasoning as `_bound_stderr`, which bounds stderr because "it flows into
# reports and DynamoDB" — the in-file precedent for bounding a diagnostic
# artifact on its write path.
#
# This replaces an unexplained `[:20]` slice. Nothing justified the 20: `why` on
# this file returns no record mentioning a field limit, and the commit that
# introduced the builder (`LGR-d2e9878-1`, backfilled) describes everything about
# it except that. Revisit when a legitimate source schema exceeds 512 fields, or
# when `null_counts`/`dtypes` stop reaching the state row — at that point the
# ceiling's only justification disappears and it can be removed, not raised.
_MAX_TEST_FRAME_COLUMNS = 512


# p32 3.7's list (groupBy, agg, spark., UDFs) plus joins, set operations and
# window functions, which 2.23 puts in the same class. Names are as
# ConversionError reports them (the residual match with whitespace, dots and
# parens stripped).
#
# MEMBERSHIP CRITERION — read this before adding a name (p41 2.7).
#
# A name belongs here IFF **no single-frame elementwise pandas equivalent
# exists**: the operation cannot be written as a pandas expression over the one
# frame the proxy holds (``df_transformed``), producing each row's value from
# that row alone. Membership is a claim about what PANDAS can express.
#
# Membership does NOT mean "this converter has no rule for it yet". A construct
# pandas can express and this converter merely has not implemented belongs
# OUTSIDE the set, where :func:`classify_conversion_refusal`'s conservative
# `not_yet_expressible` default correctly calls it a defect in this system and
# something to fix.
#
# The criterion is written down because its absence cost a customer a false
# claim. `initcap` was a member — p32 3.7's list carried it — and fails the
# criterion: `.str.title()` is single-frame and elementwise. So the report said
# "there is nothing to fix here" about a construct that is UNIMPLEMENTED, not
# inexpressible (p41 D4, 1.9). p41 removed it. `initcap` still has no converter
# rule and still refuses; only the REASON changed. Do not read its removal as a
# vocabulary change, and do not re-add a name on the grounds that the converter
# lacks a rule for it — that is the default's job, not this set's.
#
# Each member against the criterion: changes the frame's shape (`groupBy`,
# `agg`); needs a second frame (`join`, `crossJoin`, `union`, `unionAll`,
# `unionByName`); reads rows other than the current one (`Window`, `over`,
# `partitionBy`, `row_number`, `rank`, `dense_rank`, `lag`, `lead`); has no
# static translation at all (`rdd`, `udf`); is a Spark entry point rather than a
# column expression (`spark`, `sc`).
_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS = frozenset(
    {
        "groupBy",
        "agg",
        "spark",
        "sc",
        "rdd",
        "udf",
        "join",
        "crossJoin",
        "union",
        "unionAll",
        "unionByName",
        "Window",
        "over",
        "partitionBy",
        "row_number",
        "rank",
        "dense_rank",
        "lag",
        "lead",
    }
)


def classify_conversion_refusal(construct: str) -> str:
    """Return which of p32 2.23's two reasons a refusal carries.

    Anything not on the permanent list defaults to *not yet expressible* — the
    reason that says "this is a defect in this system". Defaulting the other
    way would let a real converter gap read as "nothing to fix", which 2.23
    forbids: reclassifying refusals must not make the second kind
    indistinguishable from the first.
    """
    if construct in _PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS:
        return NOT_VERIFIED_REASON_PERMANENT
    return NOT_VERIFIED_REASON_NOT_YET


def declared_source_fields(source_schema: dict | None) -> dict:
    """Return the field map a test frame would be built from.

    Shared by :meth:`CodeInterpreterTestRunner._build_test_data_code` and the
    pre-session frame guard so the two cannot drift — a guard that counted
    fields differently from the builder could refuse a schema the builder
    handles, or admit one it truncates.

    ``properties`` wins whenever the KEY is present, even when its value is
    empty; only an absent ``properties`` falls through to ``fields``. That is the
    behaviour requirement 3.14 pins, so it is reproduced rather than tidied.
    """
    schema = source_schema or {}
    fields: dict = schema.get("properties", schema.get("fields", {}))
    return fields


class CodeInterpreterTestRunner:
    """Execute a transform block inside AgentCore Code Interpreter."""

    def __init__(self, region: str):
        try:
            self.client = boto3.client("bedrock-agentcore", region_name=region)
        except Exception as exc:
            logger.warning("bedrock-agentcore client unavailable: %s", exc)
            self.client = None
        self.session_id: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_test(
        self,
        transform_block: str,
        source_schema: dict,
        target_schema: dict,
    ) -> dict:
        """Run the full test lifecycle and return results.

        Returns a dict with keys: ``execution_successful``, ``output_records``,
        ``input_records``, ``data_quality_results``, ``error_details``, the
        structured execution fields ``executed`` (bool — did code actually
        run in the interpreter), ``exit_code`` and ``stderr`` (bounded), plus
        ``conversion_report`` (what the PySpark→pandas conversion handled;
        None when conversion failed or never ran).

        P28 R10.1: a non-zero exit code or a traceback on stderr from ANY of
        the three execution steps fails the run — no warn-and-continue.

        P28 R10.2: the transform is converted BEFORE a session is started.
        An unconvertible construct produces an honest structured outcome
        naming the construct (``executed=False`` — the transform never ran);
        nothing is commented out or silently dropped. p32 2.20: that outcome
        additionally carries ``not_verified_by_proxy``, ``not_verified_reason``,
        ``not_verified_construct`` and ``not_verified_line_number`` so it is
        recorded as a non-verification rather than an execution failure.

        p33 2.11 (D12): the test frame is checked BEFORE a session is started
        too. When no frame can be built that carries every field the source
        schema declares — the count exceeds ``_MAX_TEST_FRAME_COLUMNS``, or
        there is no source schema at all — the run returns
        ``_frame_truncation_results``: ``not_verified_by_harness`` with the
        field count and the ceiling, and no session spent.
        """
        if self.client is None:
            logger.warning("Code Interpreter unavailable — returning empty results")
            return self._empty_results("bedrock-agentcore client not available")

        try:
            conversion = PySpark2PandasConverter.convert(transform_block)
        except ConversionError as exc:
            logger.error("PySpark-to-pandas conversion failed: %s", exc)
            return self._conversion_failure_results(exc)

        # p33 2.11 / D12 — refuse an unbuildable frame here, beside the
        # conversion refusal above and for the same reason: an unconvertible
        # transform never costs a session, and neither should a frame that
        # cannot carry the columns the transform references. Refusing before
        # `_start_session` is also what makes C13 hold by elimination —
        # `_build_validation_code`'s `expected` list is never compared against an
        # incomplete frame, so every shortfall `schema_compliance` reports is
        # attributable to the transform.
        #
        # Ordered AFTER the conversion check on purpose: a transform the proxy
        # cannot express is not a frame problem, and the proxy refusal names the
        # construct, which is the more actionable of the two.
        refusal = self._frame_refusal(source_schema)
        if refusal is not None:
            reason, field_count = refusal
            logger.error(
                "Test frame cannot be built (%s): %d declared source field(s) against a "
                "ceiling of %d — refusing before starting a session",
                reason,
                field_count,
                _MAX_TEST_FRAME_COLUMNS,
            )
            results = self._frame_truncation_results(field_count, _MAX_TEST_FRAME_COLUMNS, reason)
            # Conversion DID run and succeed here, so its report is real
            # diagnostic output — the docstring's "None when conversion failed
            # or never ran" contract keeps holding either way.
            results["conversion_report"] = conversion["conversion_report"]
            return results

        try:
            results = self._run_lifecycle(conversion["code"], source_schema, target_schema)
        except Exception as exc:
            logger.error("Code Interpreter test failed: %s", exc, exc_info=True)
            results = self._empty_results(str(exc))
        finally:
            self._stop_session()

        # P28 R10.2: the conversion report flows into the result for diagnostics.
        results["conversion_report"] = conversion["conversion_report"]
        return results

    @staticmethod
    def _frame_refusal(source_schema: dict) -> tuple[str, int] | None:
        """Return ``(reason, field_count)`` when no honest frame can be built.

        ``None`` means the frame will carry one column per declared field, which
        is the only state in which the transform is worth executing (p33 2.10).

        Why this is a refusal and not a truncation. Once the real source schema
        reaches the frame, a schema wider than the frame produces a frame missing
        columns the transform references — and the symptom is NOT the
        ``KeyError`` it looks like it should be. The most common transform shape
        here is ``withColumnRenamed``, which the converter rewrites to pandas
        ``.rename(columns=...)`` (``pyspark_to_pandas.py:837``), and ``.rename``
        silently ignores names that are absent. So a wide-schema pipeline whose
        transform renames columns runs clean and reports a plausible-looking LOW
        ``schema_compliance`` caused entirely by the dropped columns: a false
        failing score with evidence attached, which is harder to diagnose than a
        crash and is why the refusal has to happen before the session rather than
        being handled after it.
        """
        fields = declared_source_fields(source_schema)
        if not fields:
            # The builder's `{id, value}` fallback would be the frame. It stays
            # in place as a defensive default, but a run measured against it
            # proves nothing about the pipeline's actual input, so it is reported
            # rather than scored (p33 2.11, D9 as amended by D12).
            return NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA, 0
        field_count = len(fields)
        if field_count > _MAX_TEST_FRAME_COLUMNS:
            return NOT_VERIFIED_REASON_FRAME_TRUNCATED, field_count
        return None

    def _run_lifecycle(self, pandas_code: str, source_schema: dict, target_schema: dict) -> dict:
        """Start a session, generate data, execute the transform, validate."""
        self.session_id = self._start_session()

        # Step 1: Generate test data
        gen_exec = self._invoke(self._build_test_data_code(source_schema))
        if gen_exec["failed"]:
            return self._failed_execution_results("test data generation", gen_exec)

        # Step 2: Execute the pandas-converted transform
        transform_exec = self._invoke(pandas_code)
        if transform_exec["failed"]:
            return self._failed_execution_results("transform execution", transform_exec)

        # Step 3: Validate output
        val_exec = self._invoke(self._build_validation_code(target_schema))
        if val_exec["failed"]:
            return self._failed_execution_results("output validation", val_exec)

        results = self._parse_results(gen_exec["stdout"] + "\n" + val_exec["stdout"])
        # All steps exited 0 with no traceback — surface any benign,
        # non-fatal stderr (e.g. deprecation warnings) for diagnostics.
        results["stderr"] = self._bound_stderr(
            "\n".join(step["stderr"] for step in (gen_exec, transform_exec, val_exec) if step["stderr"])
        )
        return results

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _start_session(self) -> str:
        """Start a Code Interpreter session and return the session ID."""
        resp = self.client.start_code_interpreter_session(
            codeInterpreterIdentifier="aws.codeinterpreter.v1",
            name=f"agentic-data-ingestion-test-{int(time.time())}",
            sessionTimeoutSeconds=300,
        )
        session_id = resp.get("sessionId", "")
        logger.info("Code Interpreter session started: %s", session_id)
        return session_id

    def _invoke(self, code: str) -> dict:
        """Send *code* to the session and return a structured execution result.

        Returns ``{"stdout", "stderr", "exit_code", "failed", "failure_reason"}``.

        P28 R10.1: ``failed`` is True when the code exited non-zero OR a
        Python traceback was detected on stderr (some runtimes exit 0 after a
        traceback printed from a background/subprocess context). Benign stderr
        output WITHOUT a traceback (e.g. deprecation warnings) does NOT fail
        the execution. These signals were previously logged as warnings and
        dropped — they are now carried in the result and gate the run.

        Uses ``structuredContent`` (preferred) for stdout/stderr/exitCode.
        Falls back to legacy ``content[].text`` iteration if absent; that
        shape has no stderr/exit-code separation, so the combined text is
        scanned for a traceback instead.
        """
        resp = self.client.invoke_code_interpreter(
            codeInterpreterIdentifier="aws.codeinterpreter.v1",
            sessionId=self.session_id,
            name="executeCode",
            arguments={"language": "python", "code": code},
        )

        stream = resp.get("stream", resp.get("body", []))
        for event in stream:
            result = event.get("result", {})
            structured = result.get("structuredContent", {})
            if structured:
                return self._execution_result(
                    stdout=structured.get("stdout", ""),
                    stderr=structured.get("stderr", "") or "",
                    exit_code=int(structured.get("exitCode", 0) or 0),
                )
            # Fallback: legacy content[].text iteration (no stderr/exitCode)
            text_parts: list[str] = []
            for content_item in result.get("content", []):
                if content_item.get("type") == "text":
                    text_parts.append(content_item.get("text", ""))
            if text_parts:
                text = "\n".join(text_parts)
                if self._has_traceback(text):
                    # No separate stderr stream in the legacy shape — treat
                    # the combined text as the stderr evidence.
                    return self._execution_result(stdout=text, stderr=text, exit_code=0)
                return self._execution_result(stdout=text, stderr="", exit_code=0)
        return self._execution_result(stdout="", stderr="", exit_code=0)

    def _execution_result(self, stdout: str, stderr: str, exit_code: int) -> dict:
        """Classify one execution: non-zero exit or traceback-on-stderr → failed."""
        failure_reason: str | None = None
        if exit_code != 0:
            failure_reason = f"exit code {exit_code}"
        elif self._has_traceback(stderr):
            failure_reason = "traceback on stderr (exit code 0)"

        bounded_stderr = self._bound_stderr(stderr)
        if failure_reason:
            logger.error("Code Interpreter execution failed (%s); stderr: %s", failure_reason, bounded_stderr)
        elif stderr:
            logger.info("Code Interpreter benign stderr (non-fatal): %s", bounded_stderr)

        return {
            "stdout": stdout,
            "stderr": bounded_stderr,
            "exit_code": exit_code,
            "failed": failure_reason is not None,
            "failure_reason": failure_reason,
        }

    @staticmethod
    def _has_traceback(stderr: str) -> bool:
        """Detect a Python traceback (checked on the RAW stream, pre-truncation)."""
        return _TRACEBACK_MARKER in stderr

    @staticmethod
    def _bound_stderr(stderr: str) -> str:
        """Bound stderr to head + tail — it flows into reports and DynamoDB."""
        max_len = _STDERR_HEAD_CHARS + _STDERR_TAIL_CHARS
        if len(stderr) <= max_len:
            return stderr
        omitted = len(stderr) - max_len
        return stderr[:_STDERR_HEAD_CHARS] + f"\n... [{omitted} chars truncated] ...\n" + stderr[-_STDERR_TAIL_CHARS:]

    def _stop_session(self) -> None:
        """Stop the session if one is active."""
        if not self.session_id or not self.client:
            return
        try:
            self.client.stop_code_interpreter_session(
                codeInterpreterIdentifier="aws.codeinterpreter.v1",
                sessionId=self.session_id,
            )
            logger.info("Code Interpreter session stopped: %s", self.session_id)
        except Exception as exc:
            logger.warning("Failed to stop session %s: %s", self.session_id, exc)
        finally:
            self.session_id = None

    # ------------------------------------------------------------------
    # Code builders
    # ------------------------------------------------------------------

    def _build_test_data_code(self, source_schema: dict) -> str:
        """Return Python code that creates a pandas DataFrame from *source_schema*.

        One column per field the schema declares, up to
        :data:`_MAX_TEST_FRAME_COLUMNS` (p33 2.10). The bound is belt-and-braces
        only: ``run_test`` refuses a wider schema before a session is started
        (``_frame_refusal``), so on the live path this slice never drops a
        column. Requirement 3.14 — a schema of 20 or fewer fields produces
        byte-identical code to the pre-D12 builder.
        """
        fields = declared_source_fields(source_schema)
        if not fields:
            fields = {"id": {"type": "string"}, "value": {"type": "string"}}

        col_defs: list[str] = []
        for name, meta in list(fields.items())[:_MAX_TEST_FRAME_COLUMNS]:
            dtype = meta.get("type", "string") if isinstance(meta, dict) else "string"
            if dtype in ("integer", "int", "long", "bigint"):
                col_defs.append(f'    "{name}": [i for i in range(num_rows)]')
            elif dtype in ("float", "double", "decimal", "number"):
                col_defs.append(f'    "{name}": [round(i * 1.1, 2) for i in range(num_rows)]')
            else:
                col_defs.append(f'    "{name}": [f"val_{{i}}" for i in range(num_rows)]')

        cols_block = ",\n".join(col_defs)
        return f"""\
import pandas as pd, numpy as np, json, random
num_rows = 100
data = {{
{cols_block}
}}
df = pd.DataFrame(data)
# Inject ~5% nulls
for c in df.columns:
    mask = [random.random() < 0.05 for _ in range(num_rows)]
    df.loc[mask, c] = None
print("GENERATED:" + json.dumps({{"record_count": len(df), "columns": list(df.columns)}}))
"""

    def _build_validation_code(self, target_schema: dict) -> str:
        """Return Python code that validates the TRANSFORMED DataFrame.

        P28 R10.4: ``_df = df_transformed`` is the ONLY binding. The raw
        ``df`` (test data created in step 1) is always in scope, so any
        fallback to it would silently validate the INPUT instead of the
        transform output — validation theater. When ``df_transformed`` is
        absent, the generated code prints an explicit VALIDATION error for
        the report and raises with the same message; the exit-code /
        traceback gating (R10.1) then fails the run honestly.
        """
        target_fields = list((target_schema.get("properties", target_schema.get("fields", {}))).keys())
        expected_str = json.dumps(target_fields)
        return f"""\
import json
try:
    _df = df_transformed  # P28 R10.4: validate the TRANSFORMED frame — never raw df
except NameError:
    print("VALIDATION:" + json.dumps({{"error": "transform did not produce df_transformed"}}))
    raise RuntimeError("transform did not produce df_transformed") from None
record_count = len(_df)
null_counts = {{c: int(_df[c].isna().sum()) for c in _df.columns}}
dtypes = {{c: str(_df[c].dtype) for c in _df.columns}}
duplicates = int(_df.duplicated().sum())
expected = {expected_str}
missing = [c for c in expected if c not in _df.columns] if expected else []
completeness = round((1 - sum(null_counts.values()) / max(record_count * len(_df.columns), 1)) * 100, 1)
uniqueness = round((1 - duplicates / max(record_count, 1)) * 100, 1)
schema_compliance = round((1 - len(missing) / max(len(expected), 1)) * 100, 1) if expected else 100.0
overall_quality = round((completeness + uniqueness + schema_compliance) / 3, 1)
print("VALIDATION:" + json.dumps({{
    "record_count": record_count,
    "null_counts": null_counts,
    "dtypes": dtypes,
    "duplicates": duplicates,
    "schema_compliance": schema_compliance,
    "completeness": completeness,
    "uniqueness": uniqueness,
    "overall_quality": overall_quality,
    "missing_columns": missing,
}}))
"""

    # ------------------------------------------------------------------
    # Result parsing
    # ------------------------------------------------------------------

    def _parse_results(self, output: str) -> dict:
        """Extract GENERATED and VALIDATION markers from combined output.

        Only reached after every execution step exited 0 with no traceback,
        so ``executed=True`` and ``exit_code=0`` here.
        """
        results: dict = {
            "execution_successful": False,
            "executed": True,
            "exit_code": 0,
            "stderr": "",
            "input_records": 0,
            "output_records": 0,
            "data_quality_results": {},
            "error_details": None,
        }

        def _extract_json(marker: str, text: str) -> dict | None:
            """Find *marker*:{...} and parse the JSON, handling nested braces."""
            idx = text.find(f"{marker}:")
            if idx == -1:
                return None
            start = text.index("{", idx)
            depth, i = 0, start
            while i < len(text):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return json.loads(text[start : i + 1])
                i += 1
            return None

        gen = _extract_json("GENERATED", output)
        if gen:
            results["input_records"] = gen.get("record_count", 0)

        val = _extract_json("VALIDATION", output)
        if val:
            if "error" in val:
                results["error_details"] = val["error"]
                return results
            results["output_records"] = val.get("record_count", 0)
            results["data_quality_results"] = {
                "completeness_score": val.get("completeness", 0),
                "uniqueness_score": val.get("uniqueness", 0),
                "schema_compliance": val.get("schema_compliance", 0),
                "overall_quality_score": val.get("overall_quality", 0),
                "null_counts": val.get("null_counts", {}),
                "duplicates": val.get("duplicates", 0),
                "missing_columns": val.get("missing_columns", []),
            }
            results["execution_successful"] = True

        return results

    # ------------------------------------------------------------------
    # Failure shapes
    # ------------------------------------------------------------------

    @staticmethod
    def _conversion_failure_results(exc: ConversionError) -> dict:
        """Structured NON-VERIFICATION for a transform that could not be converted.

        P28 R10.2: an unconvertible PySpark construct is named, never commented
        out and never silently dropped. Nothing ran (``executed=False``, no exit
        code) — the converter refuses before a session is started.

        p32 V4 revises how that refusal is classified downstream, and only
        that. This shape used to be consumed as a failed
        ``code_interpreter_execution`` check, which said "the transform failed"
        about a transform that never executed and may be entirely correct
        (p32 1.19-1.20). It now carries ``not_verified_by_proxy`` so
        :func:`aggregate_check_results` records a non-verification instead
        (2.20), naming the construct, the line and which of 2.23's two reasons
        applies. ``execution_successful`` stays False: nothing was verified.

        A transform that RAN and failed (``_failed_execution_results``) and a
        Code Interpreter run that was missing or unavailable
        (``_empty_results``) do NOT carry this marker and stay failed checks —
        p32 3.22. Reclassifying a refusal must not reclassify a failure.

        p40 R4 adds a reason PRECEDENCE and nothing else on this path. A refusal
        that names its own reason is taken at its word; one that does not is
        classified from its construct exactly as before. The order matters
        because :func:`classify_conversion_refusal` keys on the CONSTRUCT NAME,
        and the receiver-shape fence reuses the same names a legitimate
        not-yet-expressible refusal uses — ``cast`` with an unrecognised target
        and ``cast`` applied to a literal are both refusals named ``cast`` — so
        the construct alone cannot tell the two apart. ``exc.reason`` is ``None``
        on every refusal that predates the fence (``ConversionError``'s
        parameter is optional and defaulted), which is what leaves the two p32
        reasons precisely where they were (p32 3.7, p40 3.7).
        """
        reason = exc.reason or classify_conversion_refusal(exc.construct)
        return {
            "execution_successful": False,
            "executed": False,
            "exit_code": None,
            "stderr": "",
            "input_records": 0,
            "output_records": 0,
            "data_quality_results": {},
            "error_details": f"PySpark-to-pandas conversion failed: {exc}",
            "conversion_report": None,
            # p32 2.20 — the proxy could not express the transform, so the
            # transform was NOT VERIFIED rather than failed.
            "not_verified_by_proxy": True,
            "not_verified_reason": reason,
            "not_verified_construct": exc.construct,
            "not_verified_line_number": exc.line_number,
            "not_verified_source_line": exc.line,
        }

    @staticmethod
    def _frame_truncation_results(field_count: int, ceiling: int, reason: str) -> dict:
        """Structured HARNESS non-verification for a frame that could not be built.

        p33 2.11 / D12. Sibling of :meth:`_conversion_failure_results`, and the
        same posture: nothing ran (``executed=False``, no exit code) because the
        run was refused before a session was started, so the transform was NOT
        VERIFIED rather than failed. What differs is whose limitation it was —
        the pandas proxy's there, the test harness's here — which is why the
        marker and the reasons are its own (C14). ``aggregate_check_results``
        reports it as a ``not_verified_by_harness`` check carrying
        ``verified: False``, in place of ``code_interpreter_execution``.

        ``data_quality_results`` is empty, and that is load-bearing rather than
        incidental: it is what suppresses the ``schema_compliance`` score (C13).
        A frame missing columns the transform references would score a shortfall
        the harness caused, reported as the transform's — the class of dishonesty
        p32 exists to remove. No branch decides this; there is simply no
        validation output to score.

        ``not_verified_field_count`` / ``not_verified_frame_ceiling`` are the
        contract with ``lambda_function.HARNESS_FIELD_COUNT_KEY`` /
        ``HARNESS_FRAME_CEILING_KEY``, which render them as text (C5): change
        both or neither.
        """
        if reason == NOT_VERIFIED_REASON_NO_SOURCE_SCHEMA:
            detail = "no source schema was available to build it from"
        else:
            detail = f"{field_count} declared source field(s) exceeds the {ceiling}-column ceiling"
        return {
            "execution_successful": False,
            "executed": False,
            "exit_code": None,
            "stderr": "",
            "input_records": 0,
            "output_records": 0,
            "data_quality_results": {},
            "error_details": f"test frame could not be built: {detail}",
            "conversion_report": None,
            # p33 2.11 — the harness could not build a frame, so the transform
            # was NOT VERIFIED rather than failed. A second reason for p32's
            # `verified: False` state, not a third state (C3).
            "not_verified_by_harness": True,
            "not_verified_reason": reason,
            "not_verified_field_count": field_count,
            "not_verified_frame_ceiling": ceiling,
        }

    @staticmethod
    def _failed_execution_results(stage: str, exec_result: dict) -> dict:
        """Structured failure for code that RAN but failed (P28 R10.1).

        ``executed=True`` distinguishes "the code ran and failed" from
        "we could not run the code at all" (``_empty_results``). The bounded
        stderr is included in ``error_details`` so reports show the traceback.
        The ``conversion_report`` placeholder is overwritten by ``run_test``
        once conversion has succeeded.
        """
        reason = exec_result.get("failure_reason") or "execution failed"
        stderr = exec_result.get("stderr", "")
        detail = f"{stage} failed: {reason}"
        if stderr:
            detail += f"\nstderr:\n{stderr}"
        logger.error("Code Interpreter %s failed: %s", stage, reason)
        return {
            "execution_successful": False,
            "executed": True,
            "exit_code": exec_result.get("exit_code"),
            "stderr": stderr,
            "input_records": 0,
            "output_records": 0,
            "data_quality_results": {},
            "error_details": detail,
            "conversion_report": None,
        }

    @staticmethod
    def _empty_results(reason: str) -> dict:
        """Safe results dict when the code could not be executed at all."""
        return {
            "execution_successful": False,
            "executed": False,
            "exit_code": None,
            "stderr": "",
            "input_records": 0,
            "output_records": 0,
            "data_quality_results": {},
            "error_details": reason,
            "conversion_report": None,
        }
