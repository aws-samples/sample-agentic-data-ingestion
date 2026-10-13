# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test — every stage that ran reports a duration, every tile names its measurement.

Feature: p39-metrics-truth, task 21.6. Subjects: ``_get_stage_metrics``
(``handlers/metrics.py:1065``) for the first clause and
``get_comprehensive_metrics`` / ``_build_comprehensive_metrics`` for the second.

# Feature: p39-metrics-truth, Property 7: Every stage that ran reports a duration, and every tile names its measurement

*For any* subset of the six mapped stages holding at least one
``Pipeline.StageDuration`` datapoint, every stage **in** the subset reports a
numeric ``avg_time_ms`` and every stage **outside** it reports Not_Established —
except ``pipeline_run``, which additionally reports a number whenever its state
rows supply a span. Independently: the ``measurement`` map's key set equals the
performance tile key set.

**Validates: Requirements 6.3, 6.4, 7.6**

Two clauses, one property, because they are one statement about the payload: a
figure a reader can see is a figure whose provenance the payload names. Clause 1
is the figure (``R6.3``/``R6.4``), clause 2 is the naming (``R7.6``).

Where the expectations come from
--------------------------------

`LGR-28b4d75-5` (**template / ratified**) — a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle — and ``R9.4``, the same rule for this spec. So **neither clause is a
list transcribed from a run of the handler.** Both are relations:

* ``R6.3`` — "WHEN a stage has published at least one ``Pipeline.StageDuration``
  datapoint within the window, THE Stage_Metrics_Reader SHALL report a numeric
  ``avg_time_ms`` for that stage." Asserted as: numeric, and an *average of that
  stage's own datapoints* — bounded by the min and max this example generated
  for it, within rounding. The exact reduction (summed duration over summed
  sample count, rounded to 1dp) is task 1.3's characterization and is deliberately not
  re-pinned here; what this clause fixes is that a stage which emitted reports a
  number, and that the number came from its own series.
* ``R6.4`` — "WHEN a stage has published no ``Pipeline.StageDuration`` datapoint
  within the window, THE Stage_Metrics_Reader SHALL report Not_Established for
  that stage's ``avg_time_ms``." Asserted as ``None``, never ``0`` and never
  another stage's figure. ``STAGE_METRICS_SCHEMA``'s ``avg_time_ms`` was already
  ``["number", "null"]`` (``tests/contracts/api_responses/schemas.py:405``),
  which is why Not_Established fits the existing stage shape with no contract
  change.
* ``R7.6`` — "THE Metrics_Handler SHALL name in the payload the measurement
  backing each performance tile, so that a label and its source cannot drift
  independently." Asserted as set **equality** against a tile key set derived
  from the API contract (see below), so a tile with no named measurement and a
  named measurement for no tile both fail.

The ``pipeline_run`` carve-out is the precedence rule, not an exception
----------------------------------------------------------------------

``_CW_STAGE_NAMES`` maps ``pipeline_run`` to ``RunPipeline`` for ``R6.2``'s
completeness and **nothing emits it** — ``run_pipeline`` holds no
``PipelineMetrics`` reference and p39 adds none (``metrics.py:246-262``). Its
source is the state-row ``started_at``/``completed_at`` span, under the
precedence rule stated at ``metrics.py:1120-1150``: the CloudWatch value when
present, else the state-row derivation where a stage has one, else
Not_Established. So "outside the emitting subset ⇒ Not_Established" is true of
the five stages that have only a CloudWatch arm, and ``pipeline_run`` is
generated with and without a state-row span so both of its arms are exercised.

Where the two arms are both available, the assertion is that the CloudWatch
figure is what is reported — clause 1 of the precedence rule. The generated
bands below are what make that checkable: each stage's CloudWatch datapoints are
drawn from a band unique to that stage and the state-row spans from a band no
CloudWatch series can reach, so a figure identifies its own source and a
cross-wired stage cannot pass.

The dual-partition read is respected, not collapsed
--------------------------------------------------

``_STAGE_STEP_NAMES`` queries both the canonical snake_case and the legacy
PascalCase partition per stage (P28 R8.2, p39 constraint 7), pinned by
``test_metrics_stage_metrics_char.py``. The state stub here answers **per
partition**, the way DynamoDB would, and the generated ``pipeline_run`` spans are
split across *both* of its partitions — so nothing below asserts against a single
partition and a stub row cannot be counted ten times.

Clause 2's expected set is derived, and from an artifact that is not the subject
-------------------------------------------------------------------------------

``performance["measurement"]`` is built from the handler's one ``_PublishedFact``
registry (``metrics.py:721``). Deriving the expected key set from that registry
would compare the map with itself. Deriving it from a run of the handler would
make the payload its own oracle. So it is derived from the **API contract**
(``tests/contracts/api_responses/schemas.py``, ``PERFORMANCE_METRICS_SCHEMA``): a
performance tile is a declared ``performance`` property whose type union is
scalar-numeric (``number`` / ``integer``, optionally with ``null``). That
selects the six figures a tile renders and excludes, by their declared types
rather than by a list written here, the two objects (``terminal_counts``,
``monthly_cost_basis``), the three derived maps (``measurement``,
``determined_from``, ``trends``) and the array (``missing``).

Two consequences, both wanted. A tile added to the contract later is covered
automatically — if the handler does not name its measurement, this fails loudly
rather than passing over a hole. And ``storage_used_bytes`` /
``requests_per_minute`` are excluded because they are declared under
``infrastructure.s3`` and not in ``performance`` at all, which is task 18.1's
deliberate boundary: they carry provenance in ``determined_from`` but they are
not performance tiles.

**Clause 2 is a set equality, which is easy to write vacuously.** Two guards
against that: the payload is asserted to carry a **non-empty** ``measurement``
map with a non-empty string per entry, and the contract-derived tile set is
asserted non-empty before it is compared. A registry that stopped emitting
``measurement``, or a contract whose numeric properties were renamed away, then
fails rather than passing trivially.

Mocking
-------

CloudWatch is **not** one of the services the shared fixtures cover (they cover
DynamoDB, S3 and SQS), so the module-level ``cloudwatch_client`` is replaced with
a ``MagicMock`` — the convention in ``test_metrics_stage_durations_char.py`` and
``test_metrics_absence_prop.py``. The state table and the cache table are patched
the same way. **No live account is touched**, no ``moto.mock_aws()`` is created
and no boto3 client mock is made for a fixture-covered service
(``tests/unit/test_no_duplicate_mocking_prop.py``). No AWS call is made, so 100
iterations cost approximately nothing.

``AWS_REGION`` is deliberately not set here: the unit layer pins it by assignment
in ``tests/unit/conftest.py`` (`LGR-3e45e65-1`, **template / ratified**).
``PROCESSED_BUCKET`` **is** set for the payload call, because ``R1.5``'s storage
filter is derived from it and an unset value short-circuits that read before any
query.
"""

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import AttributeBase
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

# handlers.common resolves its table names through require_env at import time;
# the sanctioned development switch is AGENTIC_DATA_INGESTION_ENV=local (same
# bootstrap as test_query_not_scan.py and the task 1 characterization files).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

# ═══════════════════════════════════════════════════════════════════════════
# The contract, loaded as data — clause 2's independent derivation
# ═══════════════════════════════════════════════════════════════════════════
#
# Loaded by path rather than imported as a package: it lives in another test
# layer, the layers run in separate pytest processes, and this file reads it as a
# DECLARATION rather than participating in the contract suite. The module is pure
# data with no imports of its own.

_CONTRACT_SCHEMAS_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "contracts", "api_responses", "schemas.py")
)


def _load_contract_schemas() -> Any:
    spec = importlib.util.spec_from_file_location("p39_task21_6_contract_schemas", _CONTRACT_SCHEMAS_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load the API contract at {_CONTRACT_SCHEMAS_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_type_union(declaration: Any) -> set[str]:
    """The JSON types a contract property declares, as a set."""
    declared = declaration.get("type", []) if isinstance(declaration, dict) else []
    return {declared} if isinstance(declared, str) else set(declared)


#: JSON types a scalar figure may declare. ``null`` is a member because five of
#: the six tiles are three-state (R4.1/R4.2) and one — ``failed_pipelines`` — is
#: not; both are tiles.
_SCALAR_FIGURE_TYPES = {"number", "integer", "null"}


def _contract_performance_tile_keys() -> set[str]:
    """Every ``performance`` property the contract declares as a scalar figure.

    A tile renders one number (or Not_Established). The derived and structured
    keys in the same section declare ``object`` or ``array`` types and drop out
    here by their declaration, not by being named.
    """
    schemas = _load_contract_schemas()
    properties = schemas.PERFORMANCE_METRICS_SCHEMA["properties"]
    tiles = {
        name
        for name, declaration in properties.items()
        if _declared_type_union(declaration) and _declared_type_union(declaration) <= _SCALAR_FIGURE_TYPES
    }
    assert tiles, (
        "no `performance` property in the API contract declares a scalar-numeric type, so clause 2 would compare "
        f"`measurement` against an empty set and pass vacuously — contract properties: {sorted(properties)}"
    )
    return tiles


#: Resolved once at import: the contract does not vary per example.
_PERFORMANCE_TILE_KEYS = _contract_performance_tile_keys()

# ═══════════════════════════════════════════════════════════════════════════
# The generated world — clause 1
# ═══════════════════════════════════════════════════════════════════════════
#
# Stage keys come from `_CW_STAGE_NAMES` rather than being spelled out: task
# 11.2 pins that key set to the six the page renders, so re-spelling it here
# would be a second copy of the same truth and would hide a stage added later.

#: Sorted for a stable band assignment; the map's own order is not load-bearing.
_STAGE_KEYS: tuple[str, ...] = tuple(sorted(metrics_mod._CW_STAGE_NAMES))

#: Each stage's CloudWatch datapoints are drawn from a band unique to that stage,
#: so a reported figure identifies the series it came from and a cross-wired
#: stage cannot pass. Bands are 10,000 ms wide and disjoint.
_BAND_WIDTH = 10_000.0

#: The state-row span band, above every CloudWatch band — so a `pipeline_run`
#: figure says which of its two arms answered.
_SPAN_MIN_MS = 1_000_000
_SPAN_MAX_MS = 2_000_000

#: Rounding slack: the reported figure is rounded to one decimal, so an average
#: may sit marginally outside the interval its inputs span.
_ROUNDING_SLACK = 0.05

START = datetime(2025, 6, 1, tzinfo=UTC)
END = datetime(2025, 6, 2, tzinfo=UTC)
MID = START + timedelta(hours=6)


def _stage_band(stage: str) -> tuple[float, float]:
    """The (low, high) datapoint band reserved for ``stage``."""
    index = _STAGE_KEYS.index(stage)
    low = index * _BAND_WIDTH + 1.0
    return low, low + _BAND_WIDTH - 2.0


@st.composite
def _stage_worlds(draw: Any) -> dict[str, Any]:
    """A subset of the mapped stages that emitted, their datapoints, and spans."""
    # Any subset, including the empty set (a window in which nothing ran — R6.4
    # for every stage at once) and the full set.
    emitting = draw(st.sets(st.sampled_from(_STAGE_KEYS)))

    datapoints: dict[str, list[float]] = {}
    for stage in sorted(emitting):
        low, high = _stage_band(stage)
        datapoints[stage] = draw(
            st.lists(
                st.floats(min_value=low, max_value=high, allow_nan=False, allow_infinity=False),
                min_size=1,
                max_size=4,
            )
        )

    # `pipeline_run`'s other arm. Empty for the R6.4 case; non-empty for the
    # carve-out. Split across BOTH step-name partitions (P28 R8.2) so the
    # dual-partition read is exercised rather than asserted against.
    spans_ms = draw(st.lists(st.integers(min_value=_SPAN_MIN_MS, max_value=_SPAN_MAX_MS), max_size=4))
    legacy_partition = [draw(st.booleans()) for _ in spans_ms]

    return {
        "emitting": emitting,
        "datapoints": datapoints,
        "spans_ms": spans_ms,
        "legacy_partition": legacy_partition,
        "period": draw(st.sampled_from(sorted(set(metrics_mod.CW_PERIOD.values())))),
    }


# ═══════════════════════════════════════════════════════════════════════════
# The generated world — clause 2
# ═══════════════════════════════════════════════════════════════════════════
#
# R7.6 is about the payload naming its measurements, and it must hold whether a
# tile carries a figure or Not_Established. So the payload world varies only
# what makes tiles present or absent: whether the CloudWatch reads answer with
# datapoints, answer empty, or raise; and the same three states for the state
# table read behind the rows-loaded figure (the `load_metrics` partition, which
# replaced the `file_validation` record estimate on 2026-10-10).

MEASURED = "measured"
ABSENT = "absent"
INCOMPLETE = "incomplete"

_READ_STATES = (MEASURED, ABSENT, INCOMPLETE)

#: A bucket name in the deploy form ``<prefix>-<purpose>-<account>-<region>``
#: (``foundation-stack.ts``), so the storage read reaches CloudWatch instead of
#: short-circuiting on an unset variable. The account id is the repo-wide
#: placeholder, not a real one.
_DEPLOY_BUCKET = "adi-processed-files-123456789012-us-west-2"


@st.composite
def _payload_worlds(draw: Any) -> dict[str, Any]:
    return {
        "cloudwatch_state": draw(st.sampled_from(_READ_STATES)),
        "state_table_state": draw(st.sampled_from(_READ_STATES)),
        # One value answers every reduction query: clause 2 is about key sets,
        # so what varies is whether a figure exists, not what it is. Zero is
        # reachable, which is a measurement of nothing rather than an absence.
        "value": draw(st.floats(min_value=0.0, max_value=100_000.0, allow_nan=False, allow_infinity=False)),
        "rows_written": draw(st.lists(st.integers(min_value=0, max_value=10**7), min_size=1, max_size=3)),
        "period": draw(st.sampled_from(sorted(metrics_mod.PERIOD_MAP))),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Stubs
# ═══════════════════════════════════════════════════════════════════════════


def _queried_step_name(query_kwargs: dict[str, Any]) -> str | None:
    """The single ``step_name`` a Query was bounded to, or ``None``."""
    condition = query_kwargs.get("KeyConditionExpression")
    if condition is None:
        return None
    pending = [condition]
    while pending:
        expression = pending.pop().get_expression()
        operator = expression["operator"]
        values = expression["values"]
        if operator in ("AND", "OR"):
            pending.extend(values)
            continue
        name = values[0].name if isinstance(values[0], AttributeBase) else str(values[0])
        if operator == "=" and name == "step_name":
            return str(values[1])
    return None


def _stage_cloudwatch_stub(world: dict[str, Any]) -> MagicMock:
    """CloudWatch answering ``<stage>_dur_sum`` / ``<stage>_dur_n`` for the stages that emitted.

    Each datapoint is one invocation, so the summed count is the datapoint count.
    """

    def values_for(query_id: str) -> list[float]:
        if query_id.endswith("_dur_sum"):
            return list(world["datapoints"].get(query_id.removesuffix("_dur_sum"), []))
        if query_id.endswith("_dur_n"):
            return [1.0] * len(world["datapoints"].get(query_id.removesuffix("_dur_n"), []))
        return []

    def get_metric_data(**kwargs: Any) -> dict[str, Any]:
        returned = [q["Id"] for q in kwargs["MetricDataQueries"] if q.get("ReturnData", True) is not False]
        return {
            "MetricDataResults": [
                {"Id": query_id, "Values": values_for(query_id), "Timestamps": []} for query_id in returned
            ]
        }

    client = MagicMock()
    client.get_metric_data.side_effect = get_metric_data
    return client


def _stage_state_table_stub(world: dict[str, Any]) -> MagicMock:
    """A state table answering each Query with only its own partition's rows.

    A stub returning one flat list for every partition would hand the same rows
    to all ten partition reads — the reason the sibling characterization file
    routes by partition too.
    """
    rows_by_partition: dict[str, list[dict[str, Any]]] = {}
    for span_ms, is_legacy in zip(world["spans_ms"], world["legacy_partition"], strict=True):
        partition = "RunPipeline" if is_legacy else "run_pipeline"
        completed = MID + timedelta(milliseconds=span_ms)
        rows_by_partition.setdefault(partition, []).append(
            {
                "step_name": partition,
                "status": "success",
                "timestamp": MID.isoformat(),
                "started_at": MID.isoformat(),
                "completed_at": completed.isoformat(),
            }
        )

    def query(**kwargs: Any) -> dict[str, Any]:
        step_name = _queried_step_name(kwargs)
        return {"Items": list(rows_by_partition.get(step_name or "", []))}

    table = MagicMock()
    table.query.side_effect = query
    return table


def _payload_cloudwatch_stub(world: dict[str, Any]) -> MagicMock:
    """Every CloudWatch read answered from one injected state."""

    def get_metric_data(**kwargs: Any) -> dict[str, Any]:
        if world["cloudwatch_state"] == INCOMPLETE:
            raise RuntimeError("p39 Property 7 stub: this CloudWatch read could not be performed")
        values = [] if world["cloudwatch_state"] == ABSENT else [world["value"]]
        returned = [q["Id"] for q in kwargs["MetricDataQueries"] if q.get("ReturnData", True) is not False]
        return {
            "MetricDataResults": [{"Id": query_id, "Values": list(values), "Timestamps": []} for query_id in returned]
        }

    client = MagicMock()
    client.get_metric_data.side_effect = get_metric_data
    return client


def _payload_state_table_stub(world: dict[str, Any], row_timestamp: str) -> MagicMock:
    """The rows-loaded Query answered from its injected state.

    The handler's state reads are told apart by their projection:
    ``_get_rows_loaded`` projects ``rows_written`` (on the ``load_metrics``
    partition), ``_get_stage_metrics`` projects the step/status/span attributes.
    The second is clause 1's subject and is answered empty here.
    """

    def query(**kwargs: Any) -> dict[str, Any]:
        if "rows_written" not in kwargs.get("ProjectionExpression", ""):
            return {"Items": []}
        if world["state_table_state"] == INCOMPLETE:
            raise RuntimeError("p39 Property 7 stub: the rows-loaded Query could not be performed")
        if world["state_table_state"] == ABSENT:
            return {"Items": []}
        return {
            "Items": [
                {
                    "step_name": "load_metrics",
                    "timestamp": row_timestamp,
                    "rows_written": written,
                    "write_confirmed": True,
                }
                for written in world["rows_written"]
            ]
        }

    table = MagicMock()
    table.query.side_effect = query
    return table


# ═══════════════════════════════════════════════════════════════════════════
# The property
# ═══════════════════════════════════════════════════════════════════════════


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@settings(max_examples=100, deadline=None)
@given(stage_world=_stage_worlds(), payload_world=_payload_worlds())
def test_stages_that_ran_report_durations_and_tiles_name_their_measurement(
    stage_world: dict[str, Any],
    payload_world: dict[str, Any],
) -> None:
    """A stage that emitted reports a number, one that did not reports absence,
    and every performance tile names what measures it.

    **Validates: Requirements 6.3, 6.4, 7.6**
    """
    # ═══════════════════════════════════════════════════════════════════════
    # Clause 1 — R6.3 / R6.4, against `_get_stage_metrics`
    # ═══════════════════════════════════════════════════════════════════════
    with (
        patch.object(metrics_mod, "cloudwatch_client", _stage_cloudwatch_stub(stage_world)),
        patch.object(metrics_mod, "state_table", _stage_state_table_stub(stage_world)),
    ):
        # p59: `_get_stage_metrics` returns the stage map AND the failure-record
        # reconciliation from one pass. This clause is about durations only.
        stages = metrics_mod._get_stage_metrics(START, END, stage_world["period"]).stages

    # Every mapped stage reports, whatever the reads returned. Without this the
    # per-stage clauses below could be satisfied by an absent key.
    assert set(stages) == set(metrics_mod._CW_STAGE_NAMES), (
        f"R6.2/R6.3: every mapped stage reports; got {sorted(stages)} for {sorted(metrics_mod._CW_STAGE_NAMES)}"
    )

    emitting: set[str] = stage_world["emitting"]
    spans_ms: list[int] = stage_world["spans_ms"]

    for stage in _STAGE_KEYS:
        reported = stages[stage]["avg_time_ms"]

        if stage in emitting:
            # R6.3 — a datapoint in the window is a number on the page, and it
            # is an average of THIS stage's datapoints.
            datapoints = stage_world["datapoints"][stage]
            assert _is_number(reported), (
                f"R6.3: {stage!r} published {len(datapoints)} Pipeline.StageDuration datapoint(s) in the window and "
                f"reports {reported!r} instead of a number"
            )
            assert min(datapoints) - _ROUNDING_SLACK <= reported <= max(datapoints) + _ROUNDING_SLACK, (
                f"R6.3: {stage!r} reports {reported!r}, outside the interval its own datapoints span "
                f"({min(datapoints)} .. {max(datapoints)}) — the figure is not an average of that stage's series"
            )
        elif stage == "pipeline_run" and spans_ms:
            # The carve-out, and it is the precedence rule's clause 2: nothing
            # emits `RunPipeline`, so a `pipeline_run` figure comes from the
            # state-row span. Bounded by the spans generated, which sit in a band
            # no CloudWatch series here can reach.
            assert _is_number(reported), (
                f"R6.3: {len(spans_ms)} run_pipeline state row(s) supply a span and pipeline_run reports "
                f"{reported!r} instead of a number"
            )
            assert min(spans_ms) - _ROUNDING_SLACK <= reported <= max(spans_ms) + _ROUNDING_SLACK, (
                f"R6.3: pipeline_run reports {reported!r}, outside the interval its state-row spans cover "
                f"({min(spans_ms)} .. {max(spans_ms)})"
            )
        else:
            # R6.4 — no datapoint from either source is Not_Established, never a
            # zero and never another stage's figure.
            assert reported is None, (
                f"R6.4: {stage!r} has no Pipeline.StageDuration datapoint in the window and no state-row span, so "
                f"its avg_time_ms is Not_Established — got {reported!r}"
            )

    # A stage outside the emitting set that reported nothing proves nothing if
    # NO stage emitted a value the reader could see. Assert the reads were
    # observable whenever the example made them so.
    if emitting:
        assert any(stages[stage]["avg_time_ms"] is not None for stage in emitting), (
            "the CloudWatch stub's datapoints never reached the reader, so this example's R6.3 clause was vacuous"
        )

    # ═══════════════════════════════════════════════════════════════════════
    # Clause 2 — R7.6, against the assembled payload
    # ═══════════════════════════════════════════════════════════════════════
    # Fixed before the call so the row lands inside the window the handler
    # anchors from `datetime.now(UTC)` a moment later.
    row_timestamp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    with (
        patch.dict(os.environ, {metrics_mod._DEPLOY_BUCKET_ENV: _DEPLOY_BUCKET}),
        patch.object(metrics_mod, "cloudwatch_client", _payload_cloudwatch_stub(payload_world)),
        patch.object(metrics_mod, "state_table", _payload_state_table_stub(payload_world, row_timestamp)),
        patch.object(metrics_mod, "_cache_table", MagicMock(get_item=MagicMock(return_value={}))),
    ):
        result = metrics_mod.get_comprehensive_metrics({"queryStringParameters": {"period": payload_world["period"]}})

    assert result["statusCode"] == 200, f"the payload call did not return: {result['body']}"
    performance = json.loads(result["body"])["performance"]
    measurement = performance["measurement"]

    # Non-vacuity, both halves: a map that stopped being emitted, or entries
    # with nothing in them, must fail rather than satisfy a set equality.
    assert measurement, "R7.6: the payload carries no `measurement` map, so the set equality below would hold vacuously"
    for name, text in sorted(measurement.items()):
        assert isinstance(text, str) and text.strip(), (
            f"R7.6: {name!r} names its measurement as {text!r} — a tile's source has to be stated to be read"
        )

    # R7.6 as set equality against the contract-derived tile set.
    assert set(measurement) == _PERFORMANCE_TILE_KEYS, (
        f"R7.6: `measurement`'s key set must equal the performance tile key set. "
        f"Tiles with no named measurement: {sorted(_PERFORMANCE_TILE_KEYS - set(measurement))}; "
        f"named measurements for no tile: {sorted(set(measurement) - _PERFORMANCE_TILE_KEYS)}"
    )

    # Every named tile is published in the same section it is named for, so the
    # map cannot name a field the payload does not carry.
    for name in sorted(measurement):
        assert name in performance, (
            f"R7.6: `measurement` names {name!r}, which `performance` does not publish: {sorted(performance)}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
