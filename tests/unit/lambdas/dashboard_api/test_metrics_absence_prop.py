# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test — absence is reported, never substituted.

Feature: p39-metrics-truth, task 21.1. Subject: ``get_comprehensive_metrics`` /
``_build_comprehensive_metrics`` (``handlers/metrics.py``).

# Feature: p39-metrics-truth, Property 1: Absence is reported, never substituted

*For any* subset of the metric sources made unavailable — each one either read
successfully and holding nothing, or unreadable — the handler returns a **200**
payload carrying all three sections (``performance``, ``stages``,
``infrastructure``); every published fact whose source was unavailable is
``null`` and carries a ``<source>_absent`` / ``<source>_incomplete`` token in
``determined_from``; every published fact whose source was measured carries the
bare ``<source>`` token and a value that is a number (or, for the two facts that
are objects, a present object); and ``set(missing)`` equals **exactly** the set
of ``null`` field names.

**Validates: Requirements 1.2, 1.3, 4.1, 4.2, 4.3, 4.7, 4.10, 4.12**

Where the expectation comes from
--------------------------------

`LGR-28b4d75-5` (**template / ratified**) — "a contract test written from the
producer's output cannot detect the producer drifting, because the output is its
own oracle" — and `R9.4`, the same rule for this spec. So **nothing below is a
field list transcribed from a run of the handler.** The property is a *relation*
between three things the payload publishes, and each clause is read off a
requirement:

* ``R4.1`` — a metric for which no measurement was obtained reports
  Not_Established. The machine-checkable form is *the value is* ``null``, for
  every fact whose source this example made unavailable.
* ``R4.2`` — the Not_Established outcome carries a provenance field whose value
  is one of ``<source>``, ``<source>_absent`` or ``<source>_incomplete``. Read as
  a biconditional, which is what :func:`test_absence_is_reported_never_substituted`
  asserts: **a token is suffixed exactly when the value it annotates is** ``null``.
  Both directions matter and each catches a different defect — an unsuffixed
  token over a ``null`` publishes an absence with no reason attached, and a
  suffixed token over a number claims a fact was unavailable while publishing it.
* ``R4.2``'s three-way split, again — ``_absent`` and ``_incomplete`` are
  different answers ("we looked and there is nothing" versus "we could not
  look"), so a source made *unreadable* may never report ``_absent``, and a
  source read successfully may never report ``_incomplete``. `LGR-b2f79ef-8`
  (**template / ratified**) is the fence: an operator acts differently on each.
* ``R4.3`` — ``missing`` names each fact that was unavailable. Combined with
  ``R4.2``'s biconditional this fixes ``missing`` **exactly**: it is the set of
  null field names, no more and no less. A ``missing`` that under-reports leaves
  a ``null`` on screen with nothing to say why; one that over-reports names a
  field the payload published a number for — a self-contradiction inside one
  record. Asserted as set equality in both directions.
* ``R4.3`` again — "each fact **once**". ``infrastructure.agentcore.response_time_ms``
  mirrors ``performance.agent_latency_ms``: one fact under two names, so its
  absence is named once and never twice.
* ``R4.7`` — the S3 requests-per-minute figure is *always* Not_Established,
  because S3 request metrics were never enabled on the deployed buckets. It is
  therefore a source with a fixed injected state rather than a generated one, and
  it appears in ``missing`` on every payload this property builds.
* ``R1.2`` / ``R1.3`` — the storage figure is a positive total when a bucket
  reports a datapoint and Not_Established when none does, never a byte count of
  zero. Here it is one of the generated sources and rides the general relation.
* ``R4.10`` / ``R4.12`` — a payload for **every** requested window, including a
  window in which every metric is Not_Established, and every Not_Established
  outcome is a normal return value rather than a raise. Asserted as: status 200,
  all three sections present, and no ``error`` key — the outer ``except`` in
  ``get_comprehensive_metrics`` returns through ``handle_error``, whose body
  carries ``error`` / ``details`` at status 500, so that arm is observable and is
  asserted **not** to have been reached, for all 3^6 source combinations.

Three facts about the subject that the property is written around
-----------------------------------------------------------------

Each of these is a deliberate part of the implementation, established by reading
it, and a property that ignored any of them would be red for a correct handler:

1. **``failed_pipelines`` is not three-state and is not in the widening.** An
   ``ExecutionsFailed`` sum with no datapoints means nothing failed, which is a
   measurement. It carries no ``determined_from`` token at all, so it is outside
   the per-fact relation; what is asserted about it is that it stays a number and
   is never named in ``missing``. Asserting it goes ``null`` would be asserting a
   defect.
2. **A successful States read of an empty window measures zero of each terminal
   outcome.** ``R5.4`` publishes the per-status counts so a reader can
   reconstruct the denominator, and zeros over a read that completed *are* that
   reconstruction — while ``R4.6`` gives the *rate* no denominator and therefore
   no value. So a source whose state is ``absent`` may legitimately publish a
   measured fact beside an absent one, and the clause for it is the ``R4.2``
   biconditional rather than "every field from this source is null". A source
   that was **unreadable** has no such split: nothing was observed, so every fact
   derived from it is ``null`` with ``_incomplete``, and that clause is asserted
   at full strength.
3. **``terminal_counts`` and ``monthly_cost_basis`` are objects.** The "available
   ⇒ a number" clause is therefore stated as "available ⇒ present, and a number
   unless it is an object", which is what keeps it from being a transcribed list
   of the four scalar tiles.

Mocking
-------

CloudWatch is **not** one of the services the shared fixtures cover (they cover
DynamoDB, S3 and SQS), so the module-level ``cloudwatch_client`` is replaced with
a ``MagicMock`` whose ``side_effect`` answers per read — the convention in
``test_metrics_stage_durations_char.py`` and ``test_metrics_cost.py``. The state
table and the cache table are patched the same way, as
``test_metrics_handler.py`` does. There is no ``moto.mock_aws()`` here and no
boto3 client mock for a fixture-covered service
(``tests/unit/test_no_duplicate_mocking_prop.py``). No AWS call is made, so 100
iterations cost approximately nothing.

``AWS_REGION`` is deliberately not set: the unit layer pins it by assignment in
``tests/unit/conftest.py`` (`LGR-3e45e65-1`, **template / ratified**).
``PROCESSED_BUCKET`` **is** set, per example, because ``R1.5``'s filter is
derived from it and an unset value short-circuits the storage read to
Not_Established before any query — which would silently contradict the injected
state for that source.
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason the sibling
# test_metrics_stage_durations_char.py repeats it — under xdist --dist=loadfile
# any file in this directory may be the first imported.
# ---------------------------------------------------------------------------
_DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(_DASHBOARD_API_DIR), os.path.abspath(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# handlers.common resolves nine table names through require_env at import time;
# the sanctioned development switch is AGENTIC_DATA_INGESTION_ENV=local (same
# bootstrap as test_query_not_scan.py and the task 1 characterization files).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

# ═══════════════════════════════════════════════════════════════════════════
# The vocabulary — R4.2's three answers, named once
# ═══════════════════════════════════════════════════════════════════════════

#: A source that was queried and yielded a measurement.
MEASURED = "measured"
#: A source that was queried successfully and holds nothing for the window.
ABSENT = "absent"
#: A source that could not be read at all.
INCOMPLETE = "incomplete"

#: R4.2's two suffixes, written out because the requirement fixes those words.
_ABSENT_SUFFIX = "_absent"
_INCOMPLETE_SUFFIX = "_incomplete"

#: Every source the handler reads, as the payload spells it. Imported rather than
#: re-spelled: a source name is an **identity**, not an oracle — the same
#: distinction ``conftest.py`` draws when it takes an S3 key from the handler's
#: own key function ("an address is not an oracle"). No requirement fixes these
#: strings, so nothing below asserts one; they are here so a token can be split
#: into its source and its suffix without guessing where the split falls.
_SOURCE_SFN = metrics_mod.SOURCE_SFN_EXECUTIONS
_SOURCE_COST = metrics_mod.SOURCE_GLUE_RUN_COST
_SOURCE_STAGE_DURATION = metrics_mod.SOURCE_STAGE_DURATION
_SOURCE_AGENT_ERRORS = metrics_mod.SOURCE_AGENT_LAMBDA_ERRORS
_SOURCE_S3_SIZE = metrics_mod.SOURCE_S3_BUCKET_SIZE
_SOURCE_S3_REQUESTS = metrics_mod.SOURCE_S3_REQUEST_METRICS
_SOURCE_ROWS_LOADED = metrics_mod.SOURCE_LOAD_METRICS_ROWS
_SOURCE_FAILURE_RECORDS = metrics_mod.SOURCE_FAILURE_RECORDS

#: Sources with all three states reachable from the stubs: a CloudWatch read that
#: returns datapoints, returns none, or raises; or a state-table Query that
#: returns rows carrying a usable `rows_written`, rows without one, or raises.
_THREE_STATE_SOURCES: tuple[str, ...] = (
    _SOURCE_SFN,
    _SOURCE_ROWS_LOADED,
    _SOURCE_STAGE_DURATION,
    _SOURCE_AGENT_ERRORS,
    _SOURCE_COST,
    _SOURCE_S3_SIZE,
)

#: p59 — sources with NO ``absent`` arm, so this property must not generate one
#: for them. The failure-record read is the case: an empty ``pipeline_failure``
#: partition is the measurement "no run failed outside a stage row", exactly as an
#: `ExecutionsFailed` sum with no datapoints is a genuine zero. Only "we could not
#: look" is an absence there, which is what makes the two states MEASURED and
#: INCOMPLETE and nothing else. Generating an ABSENT state for it would assert a
#: token the handler is right not to emit.
_TWO_STATE_SOURCES: tuple[str, ...] = (_SOURCE_FAILURE_RECORDS,)

_GENERATED_SOURCES: tuple[str, ...] = (*_THREE_STATE_SOURCES, *_TWO_STATE_SOURCES)

#: R4.7 — the S3 request-metrics source is unavailable on **every** payload,
#: because the metrics were never enabled on the deployed buckets. It is not a
#: generated state; it is a fixed one, and it is what makes at least one entry in
#: ``missing`` unconditional.
_FIXED_SOURCE_STATES: dict[str, str] = {_SOURCE_S3_REQUESTS: ABSENT}

_ALL_SOURCES: tuple[str, ...] = (*_GENERATED_SOURCES, *_FIXED_SOURCE_STATES)

#: The three facts the registry publishes under ``infrastructure`` rather than in
#: ``performance``, and where the payload puts each. **Not a transcription of a
#: run** — each entry is a field a requirement names by hand: ``R4.8`` (AgentCore
#: availability), ``R4.7`` (the S3 requests figure) and ``R1.3`` (the S3 storage
#: figure). It exists so the R4.2 biconditional can be checked against the value
#: actually published for those three, rather than against ``missing`` alone,
#: which would make the check circular.
_INFRASTRUCTURE_FACT_PATHS: dict[str, tuple[str, str]] = {
    "agentcore_availability": ("agentcore", "availability"),
    "requests_per_minute": ("s3", "requests_per_minute"),
    "storage_used_bytes": ("s3", "storage_used_bytes"),
}

#: The three sections ``R4.10`` requires on every payload.
_SECTIONS = ("performance", "stages", "infrastructure")

#: A bucket name in the deploy form ``<prefix>-<purpose>-<account>-<region>``
#: (``foundation-stack.ts``), so ``_deploy_bucket_search_tokens`` recovers a
#: filter and the storage read reaches CloudWatch. The account id is the
#: repo-wide placeholder, not a real one.
_DEPLOY_BUCKET = "adi-processed-files-123456789012-us-west-2"


# ═══════════════════════════════════════════════════════════════════════════
# The generated world
# ═══════════════════════════════════════════════════════════════════════════
#
# One example is: a state per source, a requested window, and the values the
# measured sources report. The measured values are generated as well as the
# states, so "available ⇒ a number" is not satisfied by one hard-coded figure —
# and so a measured **zero** (a run that cost nothing, a Glue run that loaded no
# rows, a window in which no agent errored) is reachable and must still
# read as a measurement rather than as an absence.


@st.composite
def _worlds(draw) -> dict[str, Any]:
    states = {source: draw(st.sampled_from((MEASURED, ABSENT, INCOMPLETE))) for source in _THREE_STATE_SOURCES}
    # p59 — see `_TWO_STATE_SOURCES`: no ABSENT arm exists for these, so none is
    # drawn. Keyed into the same map so every clause below reads one dict.
    states.update({source: draw(st.sampled_from((MEASURED, INCOMPLETE))) for source in _TWO_STATE_SOURCES})

    # Terminal execution counts, drawn per outcome from the one named set so a
    # terminal metric added for R5.5 is generated too. At least one is forced
    # positive: a denominator of zero is the ABSENT state for this source, not
    # its MEASURED state, and the two must stay distinguishable.
    outcome_keys = list(metrics_mod._TERMINAL_EXECUTION_METRICS)
    counts = draw(
        st.lists(st.integers(min_value=0, max_value=500), min_size=len(outcome_keys), max_size=len(outcome_keys))
    )
    bumped = draw(st.integers(min_value=0, max_value=len(outcome_keys) - 1))
    counts[bumped] = max(counts[bumped], 1)

    invocations = draw(st.integers(min_value=1, max_value=5000))

    return {
        "states": states,
        "period": draw(st.sampled_from(sorted(metrics_mod.PERIOD_MAP))),
        "terminal_counts": dict(zip(outcome_keys, counts, strict=True)),
        "started": draw(st.integers(min_value=0, max_value=5000)),
        # A non-empty reduction series is what makes the cost read MEASURED; the
        # values may sum to 0.0, which is a measured cost of nothing.
        "cost_values": draw(
            st.lists(
                st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
                min_size=1,
                max_size=4,
            )
        ),
        "cost_runs": draw(st.integers(min_value=1, max_value=500)),
        "duration_values": draw(
            st.lists(
                st.floats(min_value=0.0, max_value=600_000.0, allow_nan=False, allow_infinity=False),
                min_size=1,
                max_size=4,
            )
        ),
        "invocations": invocations,
        "errors": draw(st.integers(min_value=0, max_value=invocations)),
        "storage_bytes": draw(st.integers(min_value=0, max_value=10**13)),
        # Rows-loaded counts may include 0: a confirmed write of nothing, or an
        # empty-source run (rows_written 0, write_confirmed false), is a
        # measurement whose value is zero (R4.5's rule read from the load side),
        # and it must not fall back down the absent arm. Each count carries its
        # own `write_confirmed`, so a MEASURED window can be made entirely of
        # unconfirmed rows — still a measurement, of zero rows loaded.
        "rows_loaded": draw(
            st.lists(
                st.tuples(st.integers(min_value=0, max_value=10**7), st.booleans()),
                min_size=1,
                max_size=4,
            )
        ),
        # Both shapes of "we looked and there is nothing" for the state-table
        # read: no rows in the window at all, and load_metrics rows that carry no
        # usable `rows_written`.
        "empty_rows_shape": draw(st.sampled_from(("no_rows", "rows_without_count"))),
    }


# ═══════════════════════════════════════════════════════════════════════════
# The stubs
# ═══════════════════════════════════════════════════════════════════════════
#
# Each CloudWatch read this handler performs asks for its own query Ids, so the
# read a call belongs to is recoverable from the Ids and answered from the state
# injected for that read's source. Ids are identities, like the source names
# above; an unrecognised set is recorded and asserted on rather than raised,
# because several reads wrap their query in `except Exception` and would swallow
# an assertion thrown from in here.

#: Query-Id witness -> the read it identifies.
_READ_WITNESSES: tuple[tuple[str, str], ...] = (
    ("started_sum", "sfn"),
    ("invocations_sum", "lambda"),
    ("read_capacity", "dynamodb"),
    ("alat_sum", "agent_latency"),
    ("ai", "agent_errors"),
    ("rc_total", "cost"),
    ("s3sum", "s3_storage"),
)

#: read -> the source whose injected state answers it. ``lambda`` and
#: ``dynamodb`` feed the two ``infrastructure`` sections p39 does not widen, so
#: they have no source and always answer successfully.
_READ_SOURCES: dict[str, str] = {
    "sfn": _SOURCE_SFN,
    "agent_latency": _SOURCE_STAGE_DURATION,
    "stage_durations": _SOURCE_STAGE_DURATION,
    "agent_errors": _SOURCE_AGENT_ERRORS,
    "cost": _SOURCE_COST,
    "s3_storage": _SOURCE_S3_SIZE,
}


def _classify_read(ids: set[str]) -> str | None:
    """Which read a ``get_metric_data`` call is, or ``None`` if unrecognised."""
    for witness, read in _READ_WITNESSES:
        if witness in ids:
            return read
    if any(query_id.endswith("_dur_sum") for query_id in ids):
        return "stage_durations"
    return None


def _measured_values(read: str, world: dict[str, Any]) -> dict[str, list[float]]:
    """The reduction-series values a MEASURED read reports."""
    if read == "sfn":
        values: dict[str, list[float]] = {
            "started_sum": [float(world["started"])],
            "duration_avg": [5_000.0],
        }
        for key, count in world["terminal_counts"].items():
            values[f"{key}_sum"] = [float(count)]
        return values
    if read == "agent_latency":
        # One invocation per value: the reader divides the summed duration by
        # the summed sample count (`_weighted_stage_duration`).
        return {"alat_sum": list(world["duration_values"]), "alat_n": [1.0] * len(world["duration_values"])}
    if read == "stage_durations":
        ones = [1.0] * len(world["duration_values"])
        values = {}
        for stage in metrics_mod._CW_STAGE_NAMES:
            values[f"{stage}_dur_sum"] = list(world["duration_values"])
            values[f"{stage}_dur_n"] = list(ones)
        return values
    if read == "agent_errors":
        return {"ai": [float(world["invocations"])], "ae": [float(world["errors"])]}
    if read == "cost":
        return {"rc_total": list(world["cost_values"]), "rc_runs": [float(world["cost_runs"])]}
    if read == "s3_storage":
        return {"s3sum": [float(world["storage_bytes"])]}
    # `lambda` / `dynamodb`: not widened by p39, answered as measured zeros.
    return {}


def _cloudwatch_stub(world: dict[str, Any], unrecognised: list[list[str]]) -> MagicMock:
    """A CloudWatch client answering each read from its source's injected state."""

    def get_metric_data(**kwargs: Any) -> dict[str, Any]:
        queries = kwargs["MetricDataQueries"]
        ids = {query["Id"] for query in queries}
        read = _classify_read(ids)
        if read is None:
            unrecognised.append(sorted(ids))
            return {"MetricDataResults": []}

        source = _READ_SOURCES.get(read)
        state = world["states"][source] if source is not None else MEASURED
        if state == INCOMPLETE:
            raise RuntimeError(f"p39 Property 1 stub: the {read} read could not be performed")

        values = _measured_values(read, world) if state == MEASURED else {}
        # Only the reduction queries come back to the reader; the raw SEARCH
        # series are fetched with ReturnData=False and are never consumed.
        returned = [query["Id"] for query in queries if query.get("ReturnData", True) is not False]
        return {
            "MetricDataResults": [
                {"Id": query_id, "Values": list(values.get(query_id, [])), "Timestamps": []} for query_id in returned
            ]
        }

    client = MagicMock()
    client.get_metric_data.side_effect = get_metric_data
    return client


def _state_table_stub(world: dict[str, Any], row_timestamp: str) -> MagicMock:
    """A state table answering the rows-loaded and failure-record Queries.

    The three Queries this handler makes are told apart by their projection:
    ``_get_rows_loaded`` projects ``rows_written`` (on the ``load_metrics``
    partition — the 2026-10-10 replacement for the ``file_validation`` estimate
    read), `_attribute_failure_records` projects ``failed_step`` (p59), and
    `_get_stage_metrics`' own per-stage reads project the step/status/span
    attributes. The third has no source in the widening, so it always answers
    with no rows — the stage COUNTS are not what this property is about, only the
    provenance of the failure-record read that adjusts them.
    """
    rows_state = world["states"][_SOURCE_ROWS_LOADED]
    failure_state = world["states"][_SOURCE_FAILURE_RECORDS]

    def query(**kwargs: Any) -> dict[str, Any]:
        projection = kwargs.get("ProjectionExpression", "")
        if "failed_step" in projection:
            # p59. INCOMPLETE raises; MEASURED answers with one attributable
            # record, so the read is observable rather than trivially empty.
            if failure_state == INCOMPLETE:
                raise RuntimeError("p59 Property 1 stub: the failure-record Query could not be performed")
            return {
                "Items": [
                    {
                        "run_id": "run-p59-absence-prop",
                        "failed_step": "run_pipeline",
                        "timestamp": row_timestamp,
                    }
                ]
            }
        if "rows_written" not in projection:
            return {"Items": []}
        if rows_state == INCOMPLETE:
            raise RuntimeError("p39 Property 1 stub: the rows-loaded Query could not be performed")
        if rows_state == MEASURED:
            return {
                "Items": [
                    {
                        "step_name": "load_metrics",
                        "timestamp": row_timestamp,
                        "rows_written": written,
                        "write_confirmed": confirmed,
                    }
                    for written, confirmed in world["rows_loaded"]
                ]
            }
        if world["empty_rows_shape"] == "no_rows":
            return {"Items": []}
        # A load_metrics row in the window with no `rows_written` at all: the read
        # succeeded and established nothing, which is the same fact as no rows.
        return {
            "Items": [
                {
                    "step_name": "load_metrics",
                    "timestamp": row_timestamp,
                    "write_confirmed": True,
                }
            ]
        }

    table = MagicMock()
    table.query.side_effect = query
    return table


# ═══════════════════════════════════════════════════════════════════════════
# Reading the payload back
# ═══════════════════════════════════════════════════════════════════════════


def _split_token(token: str) -> tuple[str, str | None]:
    """A provenance token as ``(source, suffix)``, per R4.2's three values."""
    for source in _ALL_SOURCES:
        if token == source:
            return source, None
        if token == f"{source}{_ABSENT_SUFFIX}":
            return source, ABSENT
        if token == f"{source}{_INCOMPLETE_SUFFIX}":
            return source, INCOMPLETE
    raise AssertionError(
        f"R4.2: {token!r} is not '<source>', '<source>{_ABSENT_SUFFIX}' or "
        f"'<source>{_INCOMPLETE_SUFFIX}' for any source this handler reads ({sorted(_ALL_SOURCES)})"
    )


def _published_value(field: str, performance: dict[str, Any], infrastructure: dict[str, Any]) -> Any:
    """The value the payload publishes for a registered fact name."""
    if field in performance:
        return performance[field]
    assert field in _INFRASTRUCTURE_FACT_PATHS, (
        f"{field!r} carries a provenance token but is published nowhere this test knows to look — "
        f"neither a `performance` key nor one of the requirement-named infrastructure facts "
        f"({sorted(_INFRASTRUCTURE_FACT_PATHS)})"
    )
    section, leaf = _INFRASTRUCTURE_FACT_PATHS[field]
    return infrastructure[section][leaf]


# ═══════════════════════════════════════════════════════════════════════════
# The property
# ═══════════════════════════════════════════════════════════════════════════


@settings(max_examples=100, deadline=None)
@given(world=_worlds())
def test_absence_is_reported_never_substituted(world: dict[str, Any]) -> None:
    """For any subset of sources made unavailable, absence is named, never valued.

    **Validates: Requirements 1.2, 1.3, 4.1, 4.2, 4.3, 4.7, 4.10, 4.12**
    """
    injected: dict[str, str] = {**world["states"], **_FIXED_SOURCE_STATES}
    unrecognised: list[list[str]] = []
    # Fixed before the call, so it is inside the window the handler anchors from
    # `datetime.now(UTC)` a moment later — a timestamp computed inside the stub
    # would land after the window's end and be filtered out client-side.
    row_timestamp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    with (
        patch.dict(os.environ, {metrics_mod._DEPLOY_BUCKET_ENV: _DEPLOY_BUCKET}),
        patch.object(metrics_mod, "cloudwatch_client", _cloudwatch_stub(world, unrecognised)),
        patch.object(metrics_mod, "state_table", _state_table_stub(world, row_timestamp)),
        patch.object(metrics_mod, "_cache_table", MagicMock(get_item=MagicMock(return_value={}))),
    ):
        result = metrics_mod.get_comprehensive_metrics({"queryStringParameters": {"period": world["period"]}})

    assert not unrecognised, (
        f"the stub was asked for a CloudWatch read it does not model, so this example proved nothing: {unrecognised}"
    )

    # ── R4.10, R4.12: a payload for every window, absence never raising ──────
    body = json.loads(result["body"])
    assert result["statusCode"] == 200, (
        f"R4.10/R4.12: every window returns a payload, including one in which every source is unavailable — "
        f"got {result['statusCode']}: {result['body']}"
    )
    assert "error" not in body, (
        f"R4.12: an absent source reached the outer `except` in get_comprehensive_metrics and was reported as a "
        f"fault instead of returned as a value: {body}"
    )
    for section in _SECTIONS:
        assert section in body, f"R4.10: the payload is missing its {section!r} section: {sorted(body)}"
    assert body["period"] == world["period"]

    performance = body["performance"]
    infrastructure = body["infrastructure"]
    determined_from = performance["determined_from"]

    # ── Every source this example touched is visible in the provenance ───────
    # Without this, a source dropped from the registry would make every clause
    # below vacuously true for it.
    reported_sources = {_split_token(token)[0] for token in determined_from.values()}
    for source in _ALL_SOURCES:
        assert source in reported_sources, (
            f"R4.2/R4.3: no published fact names {source!r} as its source, so this example's "
            f"{injected[source]!r} state for it is unobservable in the payload"
        )

    # ── R4.1, R4.2: the token's suffix and the value agree, both ways ────────
    nulls_by_source: dict[str, int] = dict.fromkeys(_ALL_SOURCES, 0)
    for field, token in sorted(determined_from.items()):
        source, suffix = _split_token(token)
        value = _published_value(field, performance, infrastructure)
        state = injected[source]

        # R4.2 as a biconditional: suffixed exactly when absent.
        assert (value is None) == (suffix is not None), (
            f"R4.2: {field!r} publishes {value!r} under the token {token!r} — a suffixed token must annotate a "
            f"null and a bare source token must annotate a value"
        )

        if state == INCOMPLETE:
            # Nothing was observed, so nothing derived from it can be published
            # and the absence is an unknown rather than an established nothing.
            assert suffix == INCOMPLETE, (
                f"R4.2: the {source!r} read could not be performed, so {field!r} must carry "
                f"'{source}{_INCOMPLETE_SUFFIX}' — got {token!r}"
            )
            assert value is None, f"R4.1: {field!r} publishes {value!r} from a source that could not be read"
        elif state == MEASURED:
            assert suffix is None, (
                f"R4.2: {source!r} was read and yielded a measurement, so {field!r} must carry the bare source "
                f"token — got {token!r}, which reports an absence for a fact that was established"
            )
            assert value is not None
            if not isinstance(value, dict):
                assert isinstance(value, int | float) and not isinstance(value, bool), (
                    f"a measured fact is published as a number: {field!r} = {value!r}"
                )
        else:  # ABSENT — read successfully, holds nothing for this window
            assert suffix != INCOMPLETE, (
                f"R4.2: the {source!r} read completed, so an absence from it is '{source}{_ABSENT_SUFFIX}' and not "
                f"'{source}{_INCOMPLETE_SUFFIX}' — 'we looked and there is nothing' is not 'we could not look' "
                f"(got {field!r} = {token!r})"
            )

        if value is None:
            nulls_by_source[source] += 1

    # ── R4.1: an unavailable source is reported, not substituted for ─────────
    for source, state in sorted(injected.items()):
        if state != MEASURED:
            assert nulls_by_source[source] > 0, (
                f"R4.1: {source!r} was unavailable ({state}) and every fact derived from it is published as a "
                f"value — an absence was substituted for rather than reported"
            )

    # ── R4.3: set(missing) is exactly the set of null field names ────────────
    missing = performance["missing"]
    expected_missing = {field for field, value in performance.items() if value is None} | {
        field for field, token in determined_from.items() if _split_token(token)[1] is not None
    }
    assert set(missing) == expected_missing, (
        f"R4.3: `missing` must name exactly the unavailable facts. "
        f"Null but unnamed: {sorted(expected_missing - set(missing))}; "
        f"named but published with a value: {sorted(set(missing) - expected_missing)}"
    )
    assert len(missing) == len(set(missing)), f"R4.3: each unavailable fact is named once — got {missing}"
    assert missing == sorted(missing), f"`missing` is published sorted: {missing}"

    # ── R4.7: the S3 requests figure is unavailable on every payload ─────────
    assert infrastructure["s3"]["requests_per_minute"] is None, (
        "R4.7: S3 request metrics were never enabled on the deployed buckets, so this figure is never a number"
    )
    assert "requests_per_minute" in missing, "R4.7: an unavailable figure is named in `missing` on every payload"

    # ── R4.3: one fact under two names is named once ─────────────────────────
    # `infrastructure.agentcore.response_time_ms` mirrors
    # `performance.agent_latency_ms`; both are the agent stage-duration read.
    assert infrastructure["agentcore"]["response_time_ms"] == performance["agent_latency_ms"], (
        "the AgentCore response time mirrors the agent-latency tile — one fact under two names must publish one value"
    )
    for mirror_name in ("response_time_ms", "agentcore_response_time_ms"):
        assert mirror_name not in missing, (
            f"R4.3: the mirrored agent-duration fact is named once in `missing`, as the fact it is; "
            f"{mirror_name!r} is a second name for it"
        )

    # ── R4.8's short-circuit: health over an absent availability is unknown ──
    availability = infrastructure["agentcore"]["availability"]
    assert (infrastructure["agentcore"]["status"] == "unknown") == (availability is None), (
        f"R4.8: an unmeasured availability has no health to map — got status "
        f"{infrastructure['agentcore']['status']!r} for availability {availability!r}"
    )

    # ── `failed_pipelines` is not part of the widening ───────────────────────
    # An ExecutionsFailed sum with no datapoints is a genuine zero: nothing
    # failed. It carries no provenance token and is never named in `missing`;
    # its read's failure case is not lost, because its two siblings from the same
    # States read carry the token that says the read did not complete.
    failed = performance["failed_pipelines"]
    assert isinstance(failed, int | float) and not isinstance(failed, bool), (
        f"`failed_pipelines` stays a plain number — absence there is a genuine zero (got {failed!r})"
    )
    assert "failed_pipelines" not in missing, (
        "nothing failed is a measurement, not an absence — `failed_pipelines` is never named in `missing`"
    )
    assert "failed_pipelines" not in determined_from, (
        "`failed_pipelines` is deliberately outside the three-state widening and carries no provenance token"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
