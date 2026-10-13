# Agentic Data Ingestion Code Tester — Code Interpreter Architecture

## Overview

The Code Tester Lambda validates AI-generated PySpark transform blocks by executing them in a sandboxed AgentCore Code Interpreter session. It converts PySpark code to pandas, generates test data from the source schema, executes the transform, and validates the output against the target schema.

## Architecture

```
lambda_handler(event)
  │
  ├── 1. Static Analysis
  │     ├── Syntax check (ast.parse)
  │     ├── Import validation
  │     └── Security scan (no os/subprocess/eval)
  │
  ├── 2. Extract Transform Block
  │     └── Between # === AI-Generated Transform Block === markers
  │
  ├── 3. PySpark → Pandas Conversion
  │     └── PySpark2PandasConverter.convert(transform_block)
  │
  ├── 4. Code Interpreter Execution
  │     ├── Start session (bedrock-agentcore API)
  │     ├── Generate test data from source_schema (5 rows, 5% nulls)
  │     ├── Execute converted pandas transform
  │     ├── Validate output against target_schema
  │     └── Stop session (always, even on error)
  │
  └── 5. Generate Test Report
        ├── record_count, null_counts, schema_compliance
        ├── completeness, uniqueness, overall_quality
        └── test_status: passed/failed
```

## Key Components

### `pyspark_to_pandas.py` — PySpark2PandasConverter

Converts PySpark transform blocks to pandas equivalents.

`convert` is a **classmethod**. It dedents the block, parses it once with
`ast.parse`, and walks each statement's call chain. It returns
`{"code": <pandas code>, "conversion_report": {...}}`, where the code carries an
`import pandas as pd` / `import numpy as np` header and the report documents what
was converted: `{input_lines, output_lines, constructs_converted}`.

Conversion is **all-or-nothing**. A construct the translator has no rule for
raises `ConversionError(construct, line, line_number)` at that AST node, with a
message of the form `unconvertible PySpark construct '<construct>' on line <n>:
<line>` (P28 R10.2 — nothing is ever commented out, dropped or partially
translated; a transform the converter cannot express fails the conversion loudly
with the construct named). For a chain spread over several physical lines, `<n>`
is the line the construct's own name sits on, not the line the statement starts
on. A block that is not valid Python is a refusal like any other.

Handles:

| PySpark Pattern | Pandas Equivalent |
|----------------|-------------------|
| `.withColumnRenamed("a", "b")` | `.rename(columns={"a": "b"})` |
| `col("x").cast(IntegerType())` | `pd.to_numeric(df["x"], errors="coerce").astype("Int64")` |
| `trim(col("x"))` | `df["x"].str.strip()` |
| `lower(col("x"))` | `df["x"].str.lower()` |
| `to_date(col("x"), "dd-MM-yyyy")` | `pd.to_datetime(df["x"], format="%d-%m-%Y", errors="coerce").dt.date` — the format string survives conversion |
| `to_timestamp(col("x"), fmt)` | `pd.to_datetime(df["x"], format=..., errors="coerce")` |
| `.filter(col("x").isNotNull())` | `df[df["x"].notna()]` (assignment form binds the mask to the statement's source frame) |
| `when(cond, val).otherwise(default)` | `np.where(cond, val, default)` |
| `regexp_replace(col("x"), p, r)` | `df["x"].str.replace(p, r, regex=True)` |
| `concat_ws(sep, ...)` | String concatenation with separator |
| `lit(value)` | Literal value assignment |
| `.drop("col")` | `df.drop(columns=["col"])` |
| `.select(cols)` | `df[cols]` |
| `df = df.withColumn("x", expr)` | `df["x"] = expr` |
| `df_transformed = df.withColumn("x", expr)` | `df_transformed = df.copy(); df_transformed["x"] = expr` — the canonical first-transform shape where source and target differ (P28 R10.3) |

Chained transforms (`df_transformed = df_transformed.withColumn(...)`) bind
inner column references to the current frame, so columns renamed by an
earlier line resolve correctly. Because the converter walks the call chain
rather than matching lines, a parenthesised multi-line chain of `withColumn`
calls — optionally closed by one `select` — converts to a single emitted
statement. What refuses is a chain that *mixes* frame operations (e.g.
`.withColumnRenamed(...).withColumn(...)`), which has no single-frame pandas
expression; it refuses at the first step that cannot compose, and splitting it
into one statement per line converts.

Constructs with no single-frame pandas equivalent (aggregations, joins, set
operations, window functions, UDFs) and functions the generation prompt offers
but the converter has no rewrite for (such as `initcap`) raise `ConversionError`
instead of shipping broken code. The full vocabulary is read out of the
converter's own frozensets in
[`pyspark_templates/README.md`](../../pyspark_templates/README.md#what-the-pandas-proxy-converts)
— that document is the canonical list, so it does not get restated here where it
could drift.

Downstream, a refusal is classified as *not verified by the pandas proxy* rather
than as a failed execution. See `run_test` below.

### `code_interpreter_runner.py` — CodeInterpreterTestRunner

Manages the Code Interpreter session lifecycle:

1. **`_start_session()`** — Creates a sandboxed session via `start_code_interpreter_session(codeInterpreterIdentifier='aws.codeinterpreter.v1', sessionTimeoutSeconds=300)`
2. **`_invoke(code)`** — Sends code to the session via `invoke_code_interpreter`, parses streaming response for text results
3. **`_build_test_data_code(source_schema)`** — Generates pandas DataFrame creation code from schema fields (5 rows, 5% null injection). Output marked with `GENERATED:{json}`
4. **`_build_validation_code(target_schema)`** — Generates validation code that computes record_count, null_counts, dtypes, duplicates, schema_compliance, completeness, uniqueness, overall_quality. Output marked with `VALIDATION:{json}`
5. **`run_test(transform_block, source_schema, target_schema)`** — Orchestrates the full lifecycle: convert → start → generate → execute → validate → stop. Its first argument is the **raw transform block**; it calls `PySpark2PandasConverter.convert` itself, BEFORE a session is started, so an unconvertible construct never costs a session. A `ConversionError` is returned as a structured **non-verification**, not a failure: `execution_successful=False`, `executed=False`, `exit_code=None`, `conversion_report=None`, plus `not_verified_by_proxy=True`, `not_verified_reason`, `not_verified_construct` and `not_verified_line_number`. The transform never ran, so nothing about it was proven either way. `classify_conversion_refusal` sets the reason: `permanently_inexpressible` (no single-frame pandas equivalent exists — nothing to fix in the generated PySpark) or `not_yet_expressible` (a defect in this converter). The handler emits that outcome as its own `not_verified_by_proxy` check in place of the `code_interpreter_execution` check, and partitions it out of the pass/fail derivation. On success the `conversion_report` flows into the result.
6. **`_parse_results(outputs)`** — Regex-parses `GENERATED:{json}` and `VALIDATION:{json}` markers from session output
7. **`_stop_session()`** — Always called in `finally` block to clean up the session

**Graceful fallback**: If Code Interpreter is unavailable (service error, permissions issue), returns empty results instead of crashing the pipeline.

### `lambda_function.py` — Lambda Handler

The handler orchestrates the full test flow:

1. Retrieve generated code from DynamoDB
2. Run static analysis (syntax, imports, security)
3. Extract the transform block between markers
4. Execute via Code Interpreter (PySpark→pandas conversion + sandboxed execution)
5. Generate comprehensive test report with real metrics
6. Store results in DynamoDB and S3

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `STATE_TABLE` | DynamoDB state table name |
| `CODE_TABLE` | Generated code table name |
| `TEST_RESULTS_TABLE` | Test results table name |
| `GENERATED_CODE_BUCKET` | S3 bucket for generated code |
| `POWERTOOLS_ENABLED` | `true` — enables structured logging and EMF metrics |
| `POWERTOOLS_SERVICE_NAME` | `agentic_data_ingestion` |
| `POWERTOOLS_LOG_LEVEL` | `INFO` |

### IAM Permissions

```
bedrock-agentcore:CreateCodeInterpreter
bedrock-agentcore:StartCodeInterpreterSession
bedrock-agentcore:InvokeCodeInterpreter
bedrock-agentcore:StopCodeInterpreterSession
bedrock-agentcore:GetCodeInterpreterSession
bedrock-agentcore:ListCodeInterpreters
bedrock-agentcore:GetCodeInterpreter
bedrock-agentcore:ListCodeInterpreterSessions
```

Resource: `arn:aws:bedrock-agentcore:${region}:${account}:code-interpreter/*`

## Test Report Output

```json
{
  "test_status": "passed",
  "record_count": 5,
  "null_counts": {"name": 0, "age": 1, "email": 0},
  "schema_compliance": 100.0,
  "completeness": 96.0,
  "uniqueness": 100.0,
  "overall_quality": 98.7,
  "execution_time_seconds": 12.3
}
```

## Cost

- Lambda execution: ~$0.002 (1024MB, ~15s)
- Code Interpreter session: ~$0.01 per session (300s max)
- Total per test: ~$0.012

## What Was Removed (Phase 8)

The following ~800 lines of MCP Interactive Session code were removed:
- `AgentCoreMCPClient` class
- `execute_code_with_interactive_session()` + 7 helper functions
- `call_mcp_tool_async()`, `handle_unavailable_tool()`, `parse_tool_response()`
- All `asyncio` usage in the execution path
- `TEST_MODE` environment variable handling

Replaced with ~200 lines: `PySpark2PandasConverter` + `CodeInterpreterTestRunner`.

## Tests

```bash
# Unit tests for PySpark-to-pandas conversion
python -m pytest tests/test_pyspark_to_pandas.py -v

# Unit tests for Code Interpreter runner (mocked boto3)
python -m pytest tests/test_code_interpreter_runner.py -v
```
