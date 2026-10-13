# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Runtime entrypoint for the data quality agent.

Receives pipeline run completion events and performs automated data quality
checks on the target table using Athena: row count reconciliation, null
rate analysis, duplicate key detection, and data freshness validation.

Produces a structured quality report with pass/fail per check and root
cause classification.

Output is written to the state table and audit log so the dashboard
can display data quality reports.

P28 R11.3/R11.4 report-safety rules:

- Sample failing rows (raw data — potential PII) are NEVER embedded in the
  DynamoDB state item. They are written to
  ``s3://<PROCESSED_BUCKET>/dq-reports/<run_id>/samples.json`` and the item
  carries the S3 URI reference plus per-check ``sample_count``.
- The DynamoDB report item is size-bounded (< ~100KB, well under DynamoDB's
  400KB hard limit): an oversized report degrades to a slimmed/summary item
  with the full report preserved at
  ``s3://<PROCESSED_BUCKET>/dq-reports/<run_id>/report.json`` — a huge
  report can no longer lose the whole write.
- The row-count check is SKIPPED (status ``skipped``) when no expected
  count is provided — it no longer defaults expected=0, which failed every
  run that produced data.
"""

import json
import logging
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", force=True)
for name in list(logging.Logger.manager.loggerDict):
    logging.getLogger(name).propagate = True

sys.path.insert(0, "/app/shared")

from bedrock_agentcore.runtime import BedrockAgentCoreApp

# P28 R19.2: single shared float→Decimal converter (replaces the local
# ``_to_decimal`` copy). Resolves from /app/shared in the container and from
# the repo's shared/ dir in tests.
from decimal_utils import convert_floats_to_decimal

# P28 R12.1: Identifier_Allowlist validator — every identifier this agent
# f-strings into Athena SQL (table names, column names) is validated before
# query assembly. Validation failure raises, naming the identifier (R12.2).
from identifier_utils import validate_identifier

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()


# ---------------------------------------------------------------------------
# Logging filter — injects run_id into every log record
# ---------------------------------------------------------------------------


class _RunIdFilter(logging.Filter):
    """Injects run_id into every log record so container logs are searchable."""

    run_id = ""
    pipeline_id = ""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id  # type: ignore[attr-defined]
        record.pipeline_id = self.pipeline_id  # type: ignore[attr-defined]
        return True


_run_filter = _RunIdFilter()

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------

import boto3

_athena_client = boto3.client("athena")
_dynamodb = boto3.resource("dynamodb")
_s3_client = boto3.client("s3")


def _require_env(name: str, default: str | None = None) -> str:
    """Thin wrapper — import shared env_utils at call time."""
    from env_utils import require_env
    return require_env(name, default)



# ---------------------------------------------------------------------------
# Task 3.2 — Athena query execution and data quality checks
# ---------------------------------------------------------------------------

_ATHENA_POLL_INTERVAL = 2  # seconds
_ATHENA_MAX_POLL_ATTEMPTS = 60  # 2 minutes max


def run_athena_query(
    query: str, database: str, execution_parameters: list[str] | None = None
) -> list[dict[str, Any]]:
    """Execute an Athena query, wait for results, and return rows as dicts.

    Uses the ATHENA_RESULTS_BUCKET env var for query result storage.
    Returns a list of dicts where keys are column names.

    P28 R12.3: data VALUES are bound via ``execution_parameters`` (Athena
    ``ExecutionParameters`` with ``?`` placeholders in the query) instead of
    being f-string'd into the query text. Athena substitutes parameter values
    literally, so string values must arrive pre-quoted (``"'value'"``) —
    callers therefore validate the underlying value before quoting it.
    """
    results_bucket = _require_env("ATHENA_RESULTS_BUCKET", "agentic-data-ingestion-athena-results")
    output_location = f"s3://{results_bucket}/data-quality-results/"

    # Start query execution
    start_kwargs: dict[str, Any] = {
        "QueryString": query,
        "QueryExecutionContext": {"Database": database},
        "ResultConfiguration": {"OutputLocation": output_location},
    }
    if execution_parameters:
        start_kwargs["ExecutionParameters"] = execution_parameters
    response = _athena_client.start_query_execution(**start_kwargs)
    query_execution_id = response["QueryExecutionId"]

    logger.info("Athena query started: id=%s", query_execution_id)

    # Poll for completion
    for attempt in range(_ATHENA_MAX_POLL_ATTEMPTS):
        status_resp = _athena_client.get_query_execution(
            QueryExecutionId=query_execution_id
        )
        state = status_resp["QueryExecution"]["Status"]["State"]

        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status_resp["QueryExecution"]["Status"].get(
                "StateChangeReason", "Unknown"
            )
            logger.error("Athena query %s: %s — %s", state, query_execution_id, reason)
            raise RuntimeError(f"Athena query {state}: {reason}")

        time.sleep(_ATHENA_POLL_INTERVAL)
    else:
        raise RuntimeError(
            f"Athena query timed out after {_ATHENA_MAX_POLL_ATTEMPTS * _ATHENA_POLL_INTERVAL}s"
        )

    # Fetch results
    results_resp = _athena_client.get_query_results(
        QueryExecutionId=query_execution_id
    )
    rows = results_resp["ResultSet"]["Rows"]
    if not rows:
        return []

    # First row is the header
    headers = [col["VarCharValue"] for col in rows[0]["Data"]]
    result_rows: list[dict[str, Any]] = []
    for row in rows[1:]:
        values = [col.get("VarCharValue", "") for col in row["Data"]]
        result_rows.append(dict(zip(headers, values)))

    logger.info(
        "Athena query complete: id=%s rows=%d",
        query_execution_id,
        len(result_rows),
    )
    return result_rows


def _quoted_identifier(name: str, context: str) -> str:
    """Validate an identifier, then return it double-quoted for Athena SQL.

    Validation keeps the query safe; quoting keeps it correct. Athena's SELECT
    grammar reserves words such as ORDER, GROUP, END, FIRST, LAST, SKIP and
    VALUES, and those pass the allowlist, so an unquoted column called
    ``order`` is a parse error. In check_null_rates that one column fails the
    single all-columns query, and with it the check for every column.

    Quoting changes nothing for names that already worked: Athena resolves
    quoted identifiers case-insensitively, for columns and for catalog tables
    alike. No escaping is needed because the allowlist excludes the double
    quote, and validating here, not only at the caller, keeps that
    precondition next to the quotes that depend on it.
    """
    return f'"{validate_identifier(name, context)}"'


def check_row_count(
    database: str, table: str, expected_count: int | None
) -> dict[str, Any]:
    """Check that the target table row count matches the expected count.

    P28 R11.4: when no expectation is provided (``expected_count is None``)
    the check is SKIPPED — it must never default to expected=0, which failed
    every run that actually produced data. An explicit 0 remains a real
    expectation and is evaluated.

    Returns a check result dict with name, status, expected, and actual.
    """
    if expected_count is None:
        return {
            "name": "row_count",
            "status": "skipped",
            "message": "No expected row count provided — check skipped",
        }

    # P28 R12.1: identifier validated, then quoted, before entering the SQL text.
    q_table = _quoted_identifier(table, "Athena row-count query table")
    query = f"SELECT COUNT(*) AS row_count FROM {q_table}"  # noqa: S608  # nosec B608 — identifier validated above
    rows = run_athena_query(query, database)

    actual = int(rows[0]["row_count"]) if rows else 0
    status = "pass" if actual == expected_count else "fail"

    return {
        "name": "row_count",
        "status": status,
        "expected": expected_count,
        "actual": actual,
    }


def check_null_rates(
    database: str,
    table: str,
    columns: list[str],
    threshold: float = 0.05,
) -> list[dict[str, Any]]:
    """Check null rates per column against a threshold.

    Returns a list of check results, one per column, with status
    "pass", "warning", or "fail" based on the null rate vs threshold.
    """
    if not columns:
        return []

    # P28 R12.1: the table name AND every discovered column name (which
    # ultimately derive from user-influenced CSV headers) are validated
    # before entering the SQL text — a hostile header fails the check loudly
    # instead of becoming code. Each is quoted as it is validated.
    q_table = _quoted_identifier(table, "Athena null-rate query table")
    q_cols = {col: _quoted_identifier(col, "Athena null-rate query column") for col in columns}

    # Build a single query that computes null rate for all columns. The alias
    # stays unquoted: the null_rate_ prefix means it can never be a reserved
    # word, and Athena returns an alias spelled exactly as written, so the
    # row.get() below matches it whatever the column's case.
    null_exprs = ", ".join(
        f"CAST(SUM(CASE WHEN {q_cols[col]} IS NULL THEN 1 ELSE 0 END) AS DOUBLE) / "
        f"CAST(COUNT(*) AS DOUBLE) AS null_rate_{col}"
        for col in columns
    )
    count_expr = "COUNT(*) AS total_rows"
    query = f"SELECT {count_expr}, {null_exprs} FROM {q_table}"  # noqa: S608  # nosec B608 — validated above

    rows = run_athena_query(query, database)
    if not rows:
        return []

    row = rows[0]
    total_rows = int(row.get("total_rows", "0"))
    checks: list[dict[str, Any]] = []

    for col in columns:
        rate_str = row.get(f"null_rate_{col}", "0")
        rate = float(rate_str) if rate_str else 0.0

        if rate > threshold:
            status = "warning"
        else:
            status = "pass"

        check: dict[str, Any] = {
            "name": f"null_rate_{col}",
            "status": status,
            "rate": round(rate, 4),
            "threshold": threshold,
            "total_rows": total_rows,
        }

        # Fetch sample null rows if rate exceeds threshold
        if rate > threshold:
            # table + col validated at function entry (R12.1).
            sample_query = (
                f"SELECT * FROM {q_table} WHERE {q_cols[col]} IS NULL LIMIT 5"  # noqa: S608  # nosec B608
            )
            try:
                sample_rows = run_athena_query(sample_query, database)
                check["sample_nulls"] = [
                    json.dumps(r, default=str) for r in sample_rows
                ]
            except Exception as e:
                logger.warning("Failed to fetch null samples for %s: %s", col, e)
                check["sample_nulls"] = []

        checks.append(check)

    return checks



# ---------------------------------------------------------------------------
# Task 3.3 — Source-target reconciliation
# ---------------------------------------------------------------------------


def check_source_target_reconciliation(
    database: str,
    table: str,
    source_row_count: int | None,
) -> dict[str, Any]:
    """Compare output row count with input row count from file validation.

    This is a convenience wrapper around check_row_count that uses the
    source_row_count from the pipeline payload. ``None`` (no expectation in
    the payload) skips the check per R11.4.
    """
    return check_row_count(database, table, source_row_count)


# ---------------------------------------------------------------------------
# Task 3.4 — Duplicate detection
# ---------------------------------------------------------------------------


def check_duplicates(
    database: str,
    table: str,
    key_columns: list[str],
) -> dict[str, Any]:
    """Query for duplicate keys in the target table.

    Returns a check result with the number of duplicate key combinations
    and a sample of the duplicated values.
    """
    if not key_columns:
        return {
            "name": "duplicate_keys",
            "status": "pass",
            "duplicates": 0,
            "sample_duplicates": [],
            "message": "No primary key columns specified — skipping duplicate check",
        }

    # P28 R12.1: table + key columns (payload-supplied) validated, then
    # quoted, before entering the SQL text (see _quoted_identifier).
    q_table = _quoted_identifier(table, "Athena duplicate-check query table")
    key_cols_str = ", ".join(
        _quoted_identifier(key_col, "Athena duplicate-check key column") for key_col in key_columns
    )

    # Count duplicate key combinations
    # Identifiers validated above. The suppressions sit on the first f-string
    # line because that is the line ruff and Bandit report, not the opening paren.
    count_query = (
        f"SELECT COUNT(*) AS dup_count FROM ("  # noqa: S608  # nosec B608
        f"  SELECT {key_cols_str}, COUNT(*) AS cnt"
        f"  FROM {q_table}"
        f"  GROUP BY {key_cols_str}"
        f"  HAVING COUNT(*) > 1"
        f")"
    )
    count_rows = run_athena_query(count_query, database)
    dup_count = int(count_rows[0]["dup_count"]) if count_rows else 0

    status = "pass" if dup_count == 0 else "fail"

    result: dict[str, Any] = {
        "name": "duplicate_keys",
        "status": status,
        "duplicates": dup_count,
        "sample_duplicates": [],
    }

    # Fetch sample duplicates if any exist
    if dup_count > 0:
        # table + key columns validated at function entry (R12.1).
        sample_query = (
            f"SELECT {key_cols_str}, COUNT(*) AS cnt"  # noqa: S608  # nosec B608
            f" FROM {q_table}"
            f" GROUP BY {key_cols_str}"
            f" HAVING COUNT(*) > 1"
            f" ORDER BY cnt DESC"
            f" LIMIT 10"
        )
        try:
            sample_rows = run_athena_query(sample_query, database)
            result["sample_duplicates"] = sample_rows
        except Exception as e:
            logger.warning("Failed to fetch duplicate samples: %s", e)

    return result


# ---------------------------------------------------------------------------
# Task 3.5 — Data freshness check
# ---------------------------------------------------------------------------


def check_freshness(
    database: str,
    table: str,
    timestamp_column: str,
    max_age_hours: int = 24,
) -> dict[str, Any]:
    """Check that the latest record timestamp is within the expected age.

    Queries for the MAX of the timestamp column and compares it against
    the current time minus max_age_hours.
    """
    if not timestamp_column:
        return {
            "name": "freshness",
            "status": "pass",
            "message": "No timestamp column specified — skipping freshness check",
        }

    # P28 R12.1: identifiers validated, then quoted, before entering the SQL text.
    q_table = _quoted_identifier(table, "Athena freshness query table")
    q_ts = _quoted_identifier(timestamp_column, "Athena freshness timestamp column")
    query = (
        f"SELECT MAX({q_ts}) AS latest_record,"  # noqa: S608  # nosec B608 — identifiers validated above
        f" current_timestamp AS check_time"
        f" FROM {q_table}"
    )
    rows = run_athena_query(query, database)

    if not rows or not rows[0].get("latest_record"):
        return {
            "name": "freshness",
            "status": "fail",
            "latest_record": None,
            "message": "No records found or timestamp column is all NULL",
        }

    latest_record = rows[0]["latest_record"]
    check_time = rows[0].get("check_time", "")

    # Parse timestamps — Athena returns ISO-ish format
    from datetime import datetime, timedelta, timezone

    try:
        # Handle common Athena timestamp formats
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                latest_dt = datetime.strptime(latest_record, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            # Fallback: treat as string comparison
            return {
                "name": "freshness",
                "status": "warning",
                "latest_record": latest_record,
                "message": f"Could not parse timestamp format: {latest_record}",
            }

        now = datetime.now(timezone.utc)
        age_hours = (now - latest_dt).total_seconds() / 3600
        status = "pass" if age_hours <= max_age_hours else "fail"

        return {
            "name": "freshness",
            "status": status,
            "latest_record": latest_record,
            "max_age_hours": max_age_hours,
            "actual_age_hours": round(age_hours, 2),
        }

    except Exception as e:
        logger.warning("Freshness check timestamp parsing failed: %s", e)
        return {
            "name": "freshness",
            "status": "warning",
            "latest_record": latest_record,
            "message": f"Timestamp parsing error: {e}",
        }



# ---------------------------------------------------------------------------
# Task 3.6 — Structured report generation
# ---------------------------------------------------------------------------


def _classify_root_cause(checks: list[dict[str, Any]]) -> str | None:
    """Classify the root cause based on failing checks.

    Returns one of: "data_issue", "transform_issue", "infrastructure_issue",
    or None if all checks pass.
    """
    failing = [c for c in checks if c.get("status") in ("fail", "warning")]
    if not failing:
        return None

    failing_names = {c["name"] for c in failing}

    # Duplicate keys or row count mismatch suggest transform issues
    if "duplicate_keys" in failing_names or "row_count" in failing_names:
        return "transform_issue"

    # Freshness issues suggest infrastructure/scheduling problems
    if "freshness" in failing_names:
        return "infrastructure_issue"

    # Null rate warnings suggest upstream data quality problems
    if any(n.startswith("null_rate_") for n in failing_names):
        return "data_issue"

    return "data_issue"


def _generate_recommendations(checks: list[dict[str, Any]]) -> list[str]:
    """Generate human-readable recommendations based on failing checks."""
    recommendations: list[str] = []

    for check in checks:
        # "skipped" checks (e.g. row_count with no expectation, R11.4) carry
        # no finding — they must not produce a remediation recommendation.
        if check.get("status") in ("pass", "skipped"):
            continue

        name = check.get("name", "")

        if name == "row_count":
            expected = check.get("expected", 0)
            actual = check.get("actual", 0)
            diff = actual - expected
            if diff < 0:
                recommendations.append(
                    f"Row count mismatch: {actual} rows found vs {expected} expected "
                    f"({abs(diff)} rows missing). Check transform logic for filtered rows."
                )
            else:
                recommendations.append(
                    f"Row count mismatch: {actual} rows found vs {expected} expected "
                    f"({diff} extra rows). Check for duplicate generation in transform."
                )

        elif name == "duplicate_keys":
            dup_count = check.get("duplicates", 0)
            recommendations.append(
                f"Found {dup_count} duplicate key combinations. "
                "Review transform logic for missing deduplication step."
            )

        elif name == "freshness":
            age = check.get("actual_age_hours", 0)
            max_age = check.get("max_age_hours", 24)
            recommendations.append(
                f"Data freshness check failed: latest record is {age:.1f} hours old "
                f"(threshold: {max_age}h). Check pipeline scheduling and source data availability."
            )

        elif name.startswith("null_rate_"):
            col = name.replace("null_rate_", "")
            rate = check.get("rate", 0)
            threshold = check.get("threshold", 0.05)
            recommendations.append(
                f"Investigate null {col} values — {rate:.0%} null rate "
                f"exceeds {threshold:.0%} threshold"
            )

    return recommendations



def generate_report(
    pipeline_id: str,
    run_id: str,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Generate a structured quality report from individual check results.

    Computes overall_status (pass/warning/fail), root cause classification,
    and human-readable recommendations.
    """
    statuses = [c.get("status", "pass") for c in checks]

    if "fail" in statuses:
        overall_status = "fail"
    elif "warning" in statuses:
        overall_status = "warning"
    else:
        overall_status = "pass"

    root_cause = _classify_root_cause(checks)
    recommendations = _generate_recommendations(checks)

    return {
        "pipeline_id": pipeline_id,
        "run_id": run_id,
        "overall_status": overall_status,
        "checks": checks,
        "root_cause": root_cause,
        "recommendations": recommendations,
    }



# ---------------------------------------------------------------------------
# P28 R11.3 — Sample offload to S3 and bounded DynamoDB report items
# ---------------------------------------------------------------------------

# Target ceiling for the serialized state item. Deliberately well under
# DynamoDB's 400KB hard limit so a large report degrades gracefully instead
# of failing the whole put_item (which previously lost the entire report).
_MAX_ITEM_BYTES = 100_000

# Check fields that carry raw data rows (potential PII) — these are popped
# out of every check result and offloaded to S3, never written to DynamoDB.
_SAMPLE_FIELDS = ("sample_nulls", "sample_duplicates")

# Scalar per-check fields preserved when an oversized report is slimmed.
_CHECK_CORE_KEYS = (
    "name",
    "status",
    "expected",
    "actual",
    "rate",
    "threshold",
    "duplicates",
    "total_rows",
    "latest_record",
    "max_age_hours",
    "actual_age_hours",
    "message",
    "error",
    "sample_count",
    "samples_s3_uri",
)

_MAX_FREETEXT_CHARS = 500  # cap for message/error/recommendation strings
_MAX_RECOMMENDATIONS = 20
_MAX_SUMMARY_CHECKS = 200  # name+status entries kept in the summary degrade


def _serialized_size(obj: Any) -> int:
    """Approximate DynamoDB item size as UTF-8 JSON bytes."""
    return len(json.dumps(obj, default=str).encode("utf-8"))


def _write_json_to_s3(run_id: str, filename: str, payload: Any) -> str:
    """Write a JSON document under dq-reports/<run_id>/ and return its S3 URI.

    Bucket choice: PROCESSED_BUCKET (``adi-processed-files-<account>-<region>``)
    — it is already injected into the agent container env by the AgentCore
    stack and covered by the runtime role's ``adi-*`` S3 read/write grant
    (foundation-stack), so no new infrastructure or IAM is needed.
    """
    bucket = _require_env("PROCESSED_BUCKET", "adi-processed-files-local")
    key = f"dq-reports/{run_id}/{filename}"
    _s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, default=str).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{bucket}/{key}"


def _extract_samples(checks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pop raw sample rows out of check results (they must not reach DynamoDB).

    Mutates each check in place: sample fields are removed and replaced by a
    ``sample_count``. Returns ``{check_name: {field: rows}}`` for checks that
    carried non-empty samples, ready to be written to S3.
    """
    samples: dict[str, dict[str, Any]] = {}
    for check in checks:
        extracted: dict[str, Any] = {}
        for field in _SAMPLE_FIELDS:
            if field in check:
                rows = check.pop(field)
                if rows:
                    extracted[field] = rows
        if extracted:
            name = str(check.get("name", "unknown"))
            samples[name] = extracted
            check["sample_count"] = sum(len(rows) for rows in extracted.values())
    return samples


def _slim_check(check: dict[str, Any]) -> dict[str, Any]:
    """Reduce a check to its core scalar fields with bounded free text."""
    slim = {k: check[k] for k in _CHECK_CORE_KEYS if k in check}
    for key in ("message", "error"):
        value = slim.get(key)
        if isinstance(value, str) and len(value) > _MAX_FREETEXT_CHARS:
            slim[key] = value[:_MAX_FREETEXT_CHARS]
    return slim


def _bound_state_item(
    item: dict[str, Any],
    *,
    report_s3_uri: str | None = None,
    max_bytes: int = _MAX_ITEM_BYTES,
) -> dict[str, Any]:
    """Return a state item guaranteed to serialize under ``max_bytes``.

    Degrades stepwise — an oversized report must shrink to a summary that
    still writes successfully, never lose the whole report:

    1. As-is when already under the cap.
    2. Slimmed: checks reduced to core scalar fields, free text and the
       recommendations list capped.
    3. Summary: checks reduced to ``{name, status}`` (capped count) with
       ``checks_total`` recording the real number.
    4. Counts only: checks dropped entirely (S3 ref carries the full data).
    """
    if _serialized_size(item) <= max_bytes:
        return item

    bounded = dict(item)
    bounded["report_truncated"] = True
    if report_s3_uri:
        bounded["report_s3_uri"] = report_s3_uri
    bounded["checks"] = [_slim_check(c) for c in item.get("checks", [])]
    bounded["recommendations"] = [
        str(r)[:_MAX_FREETEXT_CHARS]
        for r in item.get("recommendations", [])[:_MAX_RECOMMENDATIONS]
    ]
    if _serialized_size(bounded) <= max_bytes:
        return bounded

    checks = bounded.get("checks", [])
    bounded["checks_total"] = len(checks)
    bounded["checks"] = [
        {"name": str(c.get("name", ""))[:255], "status": c.get("status", "")}
        for c in checks[:_MAX_SUMMARY_CHECKS]
    ]
    if _serialized_size(bounded) <= max_bytes:
        return bounded

    # Last resort: counts + S3 reference only.
    bounded.pop("checks", None)
    return bounded


# ---------------------------------------------------------------------------
# Entrypoint (Task 3.1)
# ---------------------------------------------------------------------------


@app.entrypoint
def handler(payload: dict) -> dict:
    """Data quality agent entrypoint.

    Payload shape::

        {
            "pipeline_id": "pipe-123",
            "run_id": "run-2025-...",
            "target_table_info": {
                "database": "agentic_data_ingestion_db",
                "table": "customers",
                "location": "s3://bucket/path/"
            },
            "source_row_count": 1500,
            "primary_key_columns": ["customer_id"]
        }
    """
    start_time = time.time()

    run_id: str = payload.get("run_id", f"run-{uuid.uuid4().hex[:12]}")
    pipeline_id: str = payload.get("pipeline_id", "unknown")
    target_info: dict = payload.get("target_table_info", {})
    # R11.4: no default — an absent expectation means the row-count check is
    # skipped, never evaluated against a fabricated expected=0.
    source_row_count: int | None = payload.get("source_row_count")
    primary_key_columns: list[str] = payload.get("primary_key_columns", [])

    database: str = target_info.get("database", "")
    table: str = target_info.get("table", "")

    # Configure logging with run context
    _run_filter.run_id = run_id
    _run_filter.pipeline_id = pipeline_id
    root = logging.getLogger()
    root.addFilter(_run_filter)
    for h in root.handlers:
        h.setFormatter(
            logging.Formatter(f"%(levelname)s [run_id={run_id}] %(name)s %(message)s")
        )

    logger.info(
        "Data quality agent invoked: pipeline_id=%s database=%s table=%s",
        pipeline_id,
        database,
        table,
    )

    # Set up trace plugin for observability
    from agent_trace_plugin import AgentTracePlugin

    trace_plugin = AgentTracePlugin(
        pipeline_id=pipeline_id,
        run_id=run_id,
        step_name="data_quality",
    )

    checks: list[dict[str, Any]] = []

    # ── 1. Row count / source-target reconciliation ─────────────
    try:
        row_check = check_source_target_reconciliation(
            database, table, source_row_count,
        )
        checks.append(row_check)
        logger.info("Row count check: %s", row_check["status"])
    except Exception as e:
        logger.error("Row count check failed: %s", e)
        checks.append({
            "name": "row_count",
            "status": "fail",
            "error": str(e),
        })

    # ── 2. Null rate checks ─────────────────────────────────────
    # Discover columns from the table metadata
    all_columns: list[str] = []
    try:
        # P28 R12.3: database/table appear here as data VALUES, not
        # identifiers — bound via Athena ExecutionParameters (`?`
        # placeholders) instead of f-string'd into the query text. Athena
        # substitutes parameter values literally (strings must arrive
        # pre-quoted), so both values are also validated against the
        # identifier allowlist before quoting — they cannot contain quotes.
        validate_identifier(database, "Athena information_schema query database value")
        validate_identifier(table, "Athena information_schema query table value")
        col_query = (
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = ? AND table_name = ?"
        )
        col_rows = run_athena_query(
            col_query,
            database,
            execution_parameters=[f"'{database}'", f"'{table}'"],
        )
        all_columns = [r["column_name"] for r in col_rows]

        if all_columns:
            null_checks = check_null_rates(database, table, all_columns)
            checks.extend(null_checks)
            logger.info("Null rate checks: %d columns", len(null_checks))
    except Exception as e:
        logger.error("Null rate check failed: %s", e)
        checks.append({
            "name": "null_rates",
            "status": "fail",
            "error": str(e),
        })

    # ── 3. Duplicate key detection ──────────────────────────────
    try:
        dup_check = check_duplicates(database, table, primary_key_columns)
        checks.append(dup_check)
        logger.info("Duplicate check: %s", dup_check["status"])
    except Exception as e:
        logger.error("Duplicate check failed: %s", e)
        checks.append({
            "name": "duplicate_keys",
            "status": "fail",
            "error": str(e),
        })

    # ── 4. Data freshness check ─────────────────────────────────
    # Use a timestamp column if available (look for common names)
    timestamp_column = ""
    if all_columns:
        ts_candidates = [
            "updated_at", "created_at", "timestamp", "last_modified",
            "event_time", "ingestion_time", "load_timestamp",
        ]
        for candidate in ts_candidates:
            if candidate in all_columns:
                timestamp_column = candidate
                break

    try:
        freshness_check = check_freshness(database, table, timestamp_column)
        checks.append(freshness_check)
        logger.info("Freshness check: %s", freshness_check["status"])
    except Exception as e:
        logger.error("Freshness check failed: %s", e)
        checks.append({
            "name": "freshness",
            "status": "fail",
            "error": str(e),
        })

    # ── 5. Offload sample rows to S3 (R11.3: no raw rows/PII in DynamoDB) ──
    samples_by_check = _extract_samples(checks)
    samples_s3_uri: str | None = None
    if samples_by_check:
        try:
            samples_s3_uri = _write_json_to_s3(run_id, "samples.json", samples_by_check)
            for check in checks:
                if check.get("sample_count"):
                    check["samples_s3_uri"] = samples_s3_uri
            logger.info("DQ sample rows written to %s", samples_s3_uri)
        except Exception as e:
            # Samples are auxiliary evidence — losing them must not lose the
            # report itself (and raw rows must still never reach DynamoDB).
            logger.error("Failed to write DQ samples to S3 — samples dropped: %s", e)

    # ── 6. Generate structured report ───────────────────────────
    report = generate_report(pipeline_id, run_id, checks)
    if samples_s3_uri:
        report["samples_s3_uri"] = samples_s3_uri
    total_duration_ms = round((time.time() - start_time) * 1000)
    report["total_duration_ms"] = total_duration_ms
    # ISO-8601: ``timestamp`` is the RANGE key of the agentic_data_ingestion_state
    # PipelineIndex GSI (typed S); an int is rejected with
    # "Type mismatch for Index Key timestamp Expected: S Actual: N".
    report["timestamp"] = datetime.now(UTC).isoformat()

    # ── 7. Store result in state table (size-bounded, R11.3) ────
    state_table = _dynamodb.Table(
        _require_env("STATE_TABLE", "agentic_data_ingestion_state")
    )
    state_item = {
        "run_id": run_id,
        "step_name": "data_quality",
        "agent_type": "data_quality",
        **report,
    }
    if _serialized_size(state_item) > _MAX_ITEM_BYTES:
        # Preserve the full report in S3, then degrade the item to a bounded
        # summary — a 400KB overflow must never lose the whole report.
        report_s3_uri: str | None = None
        try:
            report_s3_uri = _write_json_to_s3(run_id, "report.json", report)
            logger.info("Full DQ report written to %s", report_s3_uri)
        except Exception as e:
            logger.error("Failed to write full DQ report to S3: %s", e)
        state_item = _bound_state_item(state_item, report_s3_uri=report_s3_uri)
    # report carries float null-rates → convert floats to Decimal.
    state_table.put_item(Item=convert_floats_to_decimal(state_item))

    # Write audit trail
    from audit_utils import write_agent_audit_entry

    write_agent_audit_entry(
        agent_type="data_quality",
        action="quality_check",
        resource_id=pipeline_id,
        details={
            "overall_status": report["overall_status"],
            "checks_run": len(checks),
            "root_cause": report["root_cause"],
        },
        run_id=run_id,
        pipeline_id=pipeline_id,
    )

    # Flush trace data
    trace_plugin.flush()

    logger.info(
        "Data quality complete: overall=%s checks=%d total_ms=%d",
        report["overall_status"],
        len(checks),
        total_duration_ms,
    )

    return report


if __name__ == "__main__":
    app.run()
