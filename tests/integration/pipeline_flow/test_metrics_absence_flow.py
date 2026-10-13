# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Two end-to-end passes through ``get_comprehensive_metrics`` (p39 task 22.1).

Feature: p39-metrics-truth. Subject: ``handlers/metrics.py``'s public entry point,
called as the router calls it, over a real moto-backed DynamoDB state table.

**Validates: Requirements 4.10, 4.12**

* ``R4.10`` — a payload for **every** requested window, including one in which
  every metric is Not_Established.
* ``R4.12`` — every Not_Established outcome is a normal return value rather than
  a raise. Observed exactly as task 21.1 observes it: the outer ``except`` in
  ``get_comprehensive_metrics`` returns through ``handle_error``, whose body
  carries ``error`` / ``details`` at status 500 (``shared/response_utils.py:47-50``),
  so ``statusCode == 200`` **and** ``"error" not in body`` is the observable form
  of "that arm was never entered". That reading is reused here rather than
  reinvented.

TWO EXAMPLES, NOT A GENERATIVE SUITE, and that is the point of the task rather
than a shortcut. The input variation — every combination of source states — is
already covered against stubs by Property 1
(``tests/unit/lambdas/dashboard_api/test_metrics_absence_prop.py``, 3^6
combinations). What this file adds is the one thing that property cannot say: the
same two endpoints of that space also hold when the state table is a real
DynamoDB implementation reached through the real ``step_name-timestamp-index``
Query, and when the CloudWatch call is one a CloudWatch implementation accepts.
So: one pass at each end, no ``@given``, no generators.

What is moto here and what is stubbed
-------------------------------------

**DynamoDB — moto, through the shared fixture.** ``mock_dynamodb_tables``
(``tests/fixtures/conftest.py``) creates the real table and GSI schemas from
``data-stack.ts``, so ``_query_state_by_step``'s bounded Query on
``step_name-timestamp-index`` is executed by a DynamoDB implementation rather
than answered by a canned dict. No ``moto.mock_aws()`` and no ``boto3`` client
mock is created here for a service the shared fixtures cover — the rule in
``.kiro/steering/testing-standards.md``. (``tests/unit/test_no_duplicate_mocking_prop.py``
scans ``unit/`` and ``contracts/`` only, so it would not have caught a violation
in this layer; the rule is followed because it is the rule, not because a test
enforces it here.)

**CloudWatch, pass 1 — moto, unstubbed.** ``moto.mock_aws`` is active for the
whole test through that same fixture, so the module-level ``cloudwatch_client``
is intercepted and every ``get_metric_data`` call returns a **successful,
empty** response. That is precisely the "fully-absent CloudWatch" this pass
wants: a read that completed and established nothing. It is also self-verifying
— a call that escaped moto would fail against the dummy credentials the
integration conftest pins, and the handler would report ``_incomplete`` rather
than ``_absent``, which this test asserts against. So the absence of a
``_incomplete`` token below is the evidence that no real AWS call was made.

**CloudWatch, pass 2 — stubbed, because moto cannot serve it.** moto 5.2.2
accepts a ``SEARCH(...)`` expression without raising but never evaluates it:
``get_metric_data`` routes any non-``SELECT`` ``Expression`` to
``metric_data_expression_parser.parse_expression``, which returns the values of
a query whose ``Id`` equals the expression string verbatim and nothing else
(``moto/cloudwatch/metric_data_expression_parser.py``). ``SEARCH(...)`` matches
no id, and neither does ``SUM(started)``, so datapoints written with
``put_metric_data`` are invisible to every read in this module. There is no way
to make "all sources present" true through moto; it is stubbed instead, and this
comment is the statement of that rather than a silently weaker test.

The stub follows the convention the CloudWatch reads already have in this repo —
a ``MagicMock`` on ``cloudwatch_client`` whose ``side_effect`` answers per query
id, as in ``test_metrics_cost.py``, ``test_metrics_stage_durations_char.py`` and
Property 1. No integration test touched CloudWatch before this one, so there was
no third convention available to follow or to invent.

What "every field Not_Established" means in pass 1, precisely
------------------------------------------------------------

Facts established by earlier p39 tasks. Asserting the opposite of any of them
would look like a finding and would not be one:

* ``failed_pipelines`` stays a plain ``number`` and reads ``0``. An
  ``ExecutionsFailed`` sum with no datapoints is a genuine zero — nothing
  failed. It is not in ``missing`` and carries no ``determined_from`` token.
* ``requests_per_minute`` is absent on **every** payload (``R4.7``): S3 request
  metrics were never enabled on the deployed buckets.
* A **successful** States read of an empty window publishes ``terminal_counts``
  as measured zeros (``R5.4``) beside an **absent** rate (``R4.6``). So "every
  field is null" is not literally true even here. The relation that does hold,
  and the one asserted, is ``set(missing)`` == the set of null field names.
* ``agentcore.response_time_ms`` mirrors ``performance.agent_latency_ms`` — one
  fact under two names, named once in ``missing`` (``R4.3``).
* ``agentcore.status`` reads ``unknown`` when availability is null (``R4.8``).
* The ``dynamodb`` and ``lambda`` infrastructure sections are outside p39's
  widening and are not asserted about.
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DASHBOARD_API_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api"
_SHARED_DIR = _PROJECT_ROOT / "shared"
for _p in (str(_DASHBOARD_API_DIR), str(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# `handlers.common` resolves nine table names through `require_env` at import
# time. Both switches are set because either alone is order-dependent in a
# single-process layer run: `AGENTIC_DATA_INGESTION_ENV=local` only unlocks the
# defaults if `shared/env_utils` has not already been imported with a different
# value, and the explicit names only take effect if no earlier module in this
# directory set them first (`test_dashboard_api_integration.py` does, with its
# own placeholders). Which of the two wins is irrelevant: the state table this
# file exercises is the fixture's, bound onto the module below.
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
for _var, _default in (
    ("STATE_TABLE", "agentic_data_ingestion_state"),
    ("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"),
    ("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"),
    ("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code"),
    ("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results"),
    ("APPROVALS_TABLE", "agentic_data_ingestion_approvals"),
    ("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"),
    ("AUDIT_TABLE", "agentic_data_ingestion_audit_log"),
    ("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces"),
):
    os.environ.setdefault(_var, _default)

from handlers import metrics as metrics_mod

#: The canonical state table the shared fixture creates (``data-stack.ts`` name).
_STATE_TABLE_NAME = "agentic_data_ingestion_state"

#: A bucket name in the deploy form ``<prefix>-<purpose>-<account>-<region>``
#: (``foundation-stack.ts:80-83``), so ``_deploy_bucket_search_tokens`` recovers a
#: filter and the storage read actually reaches CloudWatch. Set in BOTH passes:
#: an unset `PROCESSED_BUCKET` short-circuits that read to Not_Established before
#: any query, which would make pass 1 assert less than it claims and pass 2
#: contradict its own name. The account id is the repo-wide placeholder.
_DEPLOY_BUCKET = "adi-processed-files-123456789012-us-west-2"

#: ``R4.2``'s two suffixes, spelled out because the requirement fixes those words.
_ABSENT_SUFFIX = "_absent"
_INCOMPLETE_SUFFIX = "_incomplete"

#: Every source the handler reads, imported rather than re-spelled — a source name
#: is an identity, not an oracle. Needed only to split a token into its source and
#: its suffix without guessing where the split falls.
_ALL_SOURCES: tuple[str, ...] = (
    metrics_mod.SOURCE_SFN_EXECUTIONS,
    metrics_mod.SOURCE_GLUE_RUN_COST,
    metrics_mod.SOURCE_STAGE_DURATION,
    metrics_mod.SOURCE_AGENT_LAMBDA_ERRORS,
    metrics_mod.SOURCE_S3_BUCKET_SIZE,
    metrics_mod.SOURCE_S3_REQUEST_METRICS,
    metrics_mod.SOURCE_LOAD_METRICS_ROWS,
    metrics_mod.SOURCE_FAILURE_RECORDS,
)

#: The three facts the registry publishes under ``infrastructure`` rather than in
#: ``performance``, and where the payload puts each — the same reading task 21.1
#: established. Each entry is a field a requirement names by hand: ``R4.8``,
#: ``R4.7``, ``R1.3``.
_INFRASTRUCTURE_FACT_PATHS: dict[str, tuple[str, str]] = {
    "agentcore_availability": ("agentcore", "availability"),
    "requests_per_minute": ("s3", "requests_per_minute"),
    "storage_used_bytes": ("s3", "storage_used_bytes"),
}

#: The three sections ``R4.10`` requires on every payload.
_SECTIONS = ("performance", "stages", "infrastructure")


# ═══════════════════════════════════════════════════════════════════════════
# Fixture — a real moto state table bound onto the handler
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def metrics_state_table(mock_dynamodb_tables, mock_lambda_env, monkeypatch):
    """The shared fixture's state table, bound onto the metrics module.

    Bound with ``patch.object`` rather than by lining the env up with the
    module's import-time resolution: this directory's ``conftest.py`` re-imports
    the dashboard API package per file, and an earlier file in the same process
    may have set the table names to its own placeholders, so the name the module
    resolved is not knowable from here. The object is the fixture's own
    moto-backed ``boto3`` Table either way.

    ``_cache_table`` is the same table by construction in the module
    (``metrics.py:52``) and is rebound to the same object, so the cache
    read/write round trip runs against DynamoDB too. Each test gets a fresh
    table, so no pass can be served the other's cached payload.
    """
    table = mock_dynamodb_tables[_STATE_TABLE_NAME]
    monkeypatch.setenv(metrics_mod._DEPLOY_BUCKET_ENV, _DEPLOY_BUCKET)
    with (
        patch.object(metrics_mod, "state_table", table),
        patch.object(metrics_mod, "_cache_table", table),
    ):
        yield table


# ═══════════════════════════════════════════════════════════════════════════
# Reading the payload back — task 21.1's reading, reused
# ═══════════════════════════════════════════════════════════════════════════


def _split_token(token: str) -> tuple[str, str | None]:
    """A provenance token as ``(source, suffix)``, per ``R4.2``'s three values."""
    for source in _ALL_SOURCES:
        if token == source:
            return source, None
        if token == f"{source}{_ABSENT_SUFFIX}":
            return source, _ABSENT_SUFFIX
        if token == f"{source}{_INCOMPLETE_SUFFIX}":
            return source, _INCOMPLETE_SUFFIX
    raise AssertionError(
        f"R4.2: {token!r} is not '<source>', '<source>{_ABSENT_SUFFIX}' or "
        f"'<source>{_INCOMPLETE_SUFFIX}' for any source this handler reads ({sorted(_ALL_SOURCES)})"
    )


def _published_value(field: str, performance: dict[str, Any], infrastructure: dict[str, Any]) -> Any:
    """The value the payload publishes for a registered fact name."""
    if field in performance:
        return performance[field]
    assert field in _INFRASTRUCTURE_FACT_PATHS, (
        f"{field!r} carries a provenance token but is published nowhere this test knows to look"
    )
    section, leaf = _INFRASTRUCTURE_FACT_PATHS[field]
    return infrastructure[section][leaf]


def _returned_payload(result: dict[str, Any]) -> dict[str, Any]:
    """The body of a 200 that did not go through the outer ``except`` (R4.10, R4.12)."""
    body: dict[str, Any] = json.loads(result["body"])
    assert result["statusCode"] == 200, (
        f"R4.10: every requested window returns a payload — got {result['statusCode']}: {result['body']}"
    )
    assert "error" not in body, (
        f"R4.12: an absent source reached the outer `except` in get_comprehensive_metrics and was reported as a "
        f"fault instead of returned as a value: {body}"
    )
    for section in _SECTIONS:
        assert section in body, f"R4.10: the payload is missing its {section!r} section: {sorted(body)}"
    return body


def _null_field_names(body: dict[str, Any]) -> set[str]:
    """Every field name the payload published as ``null``, across both sections."""
    performance = body["performance"]
    infrastructure = body["infrastructure"]
    names = {key for key, value in performance.items() if value is None}
    for field in body["performance"]["determined_from"]:
        if _published_value(field, performance, infrastructure) is None:
            names.add(field)
    return names


def _assert_missing_is_exactly_the_nulls(body: dict[str, Any]) -> None:
    """``R4.3``: ``set(missing)`` is exactly the set of null field names."""
    missing = body["performance"]["missing"]
    expected = _null_field_names(body)
    assert set(missing) == expected, (
        f"R4.3: `missing` must name exactly the unavailable facts. "
        f"Null but unnamed: {sorted(expected - set(missing))}; "
        f"named but published with a value: {sorted(set(missing) - expected)}"
    )
    assert len(missing) == len(set(missing)), f"R4.3: each unavailable fact is named once — got {missing}"


def _assert_the_mirrored_fact_is_one_fact(body: dict[str, Any]) -> None:
    """``R4.3``: ``agentcore.response_time_ms`` is ``agent_latency_ms`` under a second name."""
    performance = body["performance"]
    assert body["infrastructure"]["agentcore"]["response_time_ms"] == performance["agent_latency_ms"], (
        "the AgentCore response time mirrors the agent-latency tile — one fact under two names publishes one value"
    )
    for mirror_name in ("response_time_ms", "agentcore_response_time_ms"):
        assert mirror_name not in performance["missing"], (
            f"R4.3: the mirrored agent-duration fact is named once in `missing`; {mirror_name!r} is a second name"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Pass 1 — a fully-absent CloudWatch and an empty state table
# ═══════════════════════════════════════════════════════════════════════════


def test_absent_cloudwatch_and_empty_state_table_return_a_full_payload(metrics_state_table):
    """Nothing measured anywhere still returns 200, with the absence named (R4.10, R4.12).

    **Validates: Requirements 4.10, 4.12**

    No CloudWatch stub: moto answers every ``get_metric_data`` successfully and
    empty, and the state table is the fixture's, freshly created and never
    written to.
    """
    result = metrics_mod.get_comprehensive_metrics({"queryStringParameters": {"period": "24h"}})

    body = _returned_payload(result)
    assert body["period"] == "24h"
    performance = body["performance"]
    infrastructure = body["infrastructure"]
    determined_from = performance["determined_from"]

    # ── Every read completed, so every absence is `_absent` and none is
    # `_incomplete`. This is also what proves no call escaped moto: a real call
    # would fail on the conftest's dummy credentials and land in the raising arm.
    for field, token in sorted(determined_from.items()):
        source, suffix = _split_token(token)
        value = _published_value(field, performance, infrastructure)
        assert (value is None) == (suffix is not None), (
            f"R4.2: {field!r} publishes {value!r} under the token {token!r} — a suffixed token must annotate a "
            f"null and a bare source token must annotate a value"
        )
        assert suffix != _INCOMPLETE_SUFFIX, (
            f"every read in this pass completed and established nothing, so {field!r} must carry "
            f"'{source}{_ABSENT_SUFFIX}' — {token!r} says the read could not be performed at all"
        )

    # ── R4.3: `missing` is exactly the nulls ────────────────────────────────
    _assert_missing_is_exactly_the_nulls(body)
    assert performance["missing"], "a window in which nothing was measured names the unavailable facts"

    # ── R4.6: no terminal execution means no rate, and never 100.0 ──────────
    assert performance["overall_success_rate"] is None, (
        f"R4.6: a window with no terminal execution has no denominator — got {performance['overall_success_rate']!r}"
    )

    # ── R5.4: the States read SUCCEEDED, so the counts are measured zeros ───
    # Not null, and not in `missing`: zeros over a read that completed are the
    # arithmetic that shows the rate above is absent for want of runs rather
    # than for want of a read.
    assert performance["terminal_counts"] == dict.fromkeys(metrics_mod._TERMINAL_EXECUTION_METRICS, 0), (
        f"R5.4: a successful States read of an empty window measures zero of each terminal outcome — got "
        f"{performance['terminal_counts']!r}"
    )
    assert _split_token(determined_from["terminal_counts"])[1] is None, (
        "the terminal counts were measured, so they carry the bare source token"
    )

    # ── `failed_pipelines` is outside the widening ──────────────────────────
    failed = performance["failed_pipelines"]
    assert failed == 0 and isinstance(failed, int | float) and not isinstance(failed, bool), (
        f"an ExecutionsFailed sum with no datapoints is a genuine zero, published as a number (got {failed!r})"
    )
    assert "failed_pipelines" not in performance["missing"], (
        "nothing failed is a measurement, not an absence — `failed_pipelines` is never named in `missing`"
    )
    assert "failed_pipelines" not in determined_from, (
        "`failed_pipelines` is deliberately outside the three-state widening and carries no provenance token"
    )

    # ── R4.7 / R4.8 ─────────────────────────────────────────────────────────
    assert infrastructure["s3"]["requests_per_minute"] is None, (
        "R4.7: S3 request metrics were never enabled on the deployed buckets"
    )
    assert infrastructure["agentcore"]["availability"] is None
    assert infrastructure["agentcore"]["status"] == "unknown", (
        f"R4.8: an unmeasured availability has no health to map — got {infrastructure['agentcore']['status']!r}"
    )
    _assert_the_mirrored_fact_is_one_fact(body)


# ═══════════════════════════════════════════════════════════════════════════
# Pass 2 — every source present
# ═══════════════════════════════════════════════════════════════════════════

#: The reduction-series values a fully-reporting CloudWatch answers, per query id.
#: The four States counts are the verified 7-day window from `R5`'s reasoning
#: (started 36, succeeded 12, failed 14, aborted 10, timed out 0) and the agent
#: stage average is the figure task 15.1 verified live, so the numbers describe a
#: window that actually occurred rather than round placeholders.
#:
#: `duration_avg` is deliberately one entry: `_get_lambda_metrics` and
#: `_get_sfn_metrics` both name their reduction that, in separate calls.
_MEASURED_SERIES: dict[str, list[float]] = {
    "invocations_sum": [400.0],
    "errors_sum": [4.0],
    "duration_avg": [5_000.0],
    "started_sum": [36.0],
    "succeeded_sum": [12.0],
    "failed_sum": [14.0],
    "aborted_sum": [10.0],
    # A datapoint whose value is zero — measured, not absent.
    "timed_out_sum": [0.0],
    "read_capacity": [1_200.0],
    "write_capacity": [800.0],
    # Stage and agent durations are an invocation-weighted mean since b0a6212
    # (2026-10-10): summed `Sum` (`_sum`) over summed `SampleCount` (`_n`). One
    # invocation each, so each mean is the figure below.
    **{f"{stage}_dur_sum": [5_000.0] for stage in metrics_mod._CW_STAGE_NAMES},
    **{f"{stage}_dur_n": [1.0] for stage in metrics_mod._CW_STAGE_NAMES},
    "alat_sum": [4_956.3],
    "alat_n": [1.0],
    "ai": [36.0],
    "ae": [1.0],
    # Three terminal Glue runs at the 10-minute DPU minimum (0.147 each).
    "rc_total": [0.147, 0.147, 0.147],
    "rc_runs": [3.0],
    "s3sum": [141_557_760.0],
}

#: `rows_written` on the one confirmed `load_metrics` row, so the rows-loaded
#: source is measured rather than absent. (Until 2026-10-10 this pass wrote a
#: `file_validation` row carrying a `record_count_estimate` instead.)
_ROWS_WRITTEN_CONFIRMED = 100

#: `rows_written` on a second run's row whose write was NOT confirmed. Written
#: through real DynamoDB so the `write_confirmed is True` gate is exercised on a
#: deserialized boolean and the count on a deserialized `Decimal`; it must add
#: nothing to the figure.
_ROWS_WRITTEN_UNCONFIRMED = 40


def _measured_cloudwatch(unserved: list[str]) -> MagicMock:
    """A CloudWatch client answering every reduction query with a measurement.

    An id this stub has no series for is recorded rather than raised: several
    reads wrap their query in ``except Exception`` and would swallow an
    assertion thrown from in here, leaving the test asserting an absence it did
    not intend. The list is asserted empty in the test body instead.
    """

    def get_metric_data(**kwargs: Any) -> dict[str, Any]:
        # Only the reduction queries come back to the reader; the raw SEARCH
        # series are fetched with ReturnData=False and never consumed.
        returned = [q["Id"] for q in kwargs["MetricDataQueries"] if q.get("ReturnData", True) is not False]
        for query_id in returned:
            if query_id not in _MEASURED_SERIES:
                unserved.append(query_id)
        return {
            "MetricDataResults": [
                {"Id": query_id, "Values": list(_MEASURED_SERIES.get(query_id, [])), "Timestamps": []}
                for query_id in returned
            ]
        }

    client = MagicMock()
    client.get_metric_data.side_effect = get_metric_data
    return client


def test_every_source_present_leaves_only_the_disabled_s3_figure_missing(metrics_state_table):
    """With every source reporting, the payload is a 200 whose only absence is R4.7's.

    **Validates: Requirements 4.10, 4.12**
    """
    # Inside the requested 24h window. Fixed before the call so it precedes the
    # `datetime.now(UTC)` the handler anchors its window from. Shaped as the
    # Glue template's `_load_counts` partial writes the row (one per run, keyed
    # `(run_id, "load_metrics")`).
    stamp = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    for run_id, rows_written, confirmed in (
        ("run-p39-22-1-metrics-flow", _ROWS_WRITTEN_CONFIRMED, True),
        ("run-p39-22-1-metrics-flow-unconfirmed", _ROWS_WRITTEN_UNCONFIRMED, False),
    ):
        metrics_state_table.put_item(
            Item={
                "run_id": run_id,
                "step_name": "load_metrics",
                "pipeline_id": "pipeline-p39-22-1",
                "original_pipeline_id": "pipeline-p39-22-1",
                "rows_read": rows_written,
                "rows_written": rows_written,
                "write_confirmed": confirmed,
                "source": "glue_job",
                "timestamp": stamp,
            }
        )
    unserved: list[str] = []

    with patch.object(metrics_mod, "cloudwatch_client", _measured_cloudwatch(unserved)):
        result = metrics_mod.get_comprehensive_metrics({"queryStringParameters": {"period": "24h"}})

    assert not unserved, (
        f"the stub was asked for a reduction it does not model, so those sources were absent and this example "
        f"did not test what it claims: {sorted(set(unserved))}"
    )

    body = _returned_payload(result)
    performance = body["performance"]
    infrastructure = body["infrastructure"]

    # ── R4.7 is the only absence a fully-reporting deployment still has ─────
    assert performance["missing"] == ["requests_per_minute"], (
        f"every source reported except the S3 request metrics, which are never enabled (R4.7) — got "
        f"{performance['missing']}"
    )
    _assert_missing_is_exactly_the_nulls(body)

    # ── Every other fact carries a bare source token over a real value ──────
    for field, token in sorted(performance["determined_from"].items()):
        _source, suffix = _split_token(token)
        value = _published_value(field, performance, infrastructure)
        if field == "requests_per_minute":
            assert suffix == _ABSENT_SUFFIX and value is None
            continue
        assert suffix is None, (
            f"R4.2: {field!r}'s source reported, so it must carry the bare source token — got {token!r}"
        )
        assert value is not None, f"R4.1: {field!r} carries a measured token over a null"

    # ── R5.3 / R5.4: the rate reconciles with the published counts ──────────
    # Derived from what the payload published, not transcribed from a run.
    counts = performance["terminal_counts"]
    denominator = sum(counts.values())
    assert denominator > 0
    assert performance["overall_success_rate"] == round(counts["succeeded"] / denominator * 100, 1), (
        f"R5.3/R5.4: the rate must be reconstructable from the published counts — {performance['overall_success_rate']!r} "
        f"against {counts!r}"
    )
    assert performance["lines_ingested_24h"] == _ROWS_WRITTEN_CONFIRMED, (
        "the rows-loaded figure is rows_written summed over the load_metrics rows whose write was confirmed — "
        f"the unconfirmed run's {_ROWS_WRITTEN_UNCONFIRMED} must add nothing"
    )
    # Rows per wall-clock hour of the 24h window, rounded to 2 places.
    assert performance["processing_rate"] == round(_ROWS_WRITTEN_CONFIRMED / 24, 2)

    # ── R6.3: a stage with a datapoint reports a numeric average ────────────
    for stage, values in body["stages"].items():
        assert values["avg_time_ms"] is not None, f"{stage!r} published a duration and must report it"

    # ── R4.8: a measured availability maps to a health that is not `unknown` ─
    assert infrastructure["agentcore"]["availability"] is not None
    assert infrastructure["agentcore"]["status"] != "unknown"
    _assert_the_mirrored_fact_is_one_fact(body)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
