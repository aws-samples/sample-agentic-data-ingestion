# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /api/metrics/comprehensive response shape matches
the ComprehensiveMetrics TypeScript interface used by use-metrics.ts.

Validates: AC-9.1, AC-9.4

The frontend expects:
  ComprehensiveMetrics {
    performance: PerformanceMetrics;
    stages: Record<string, StageMetrics>;
    infrastructure: InfrastructureStatus;
  }
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)
# `handlers.metrics` imports `dynamo_typing` from the Lambda layer, so `shared/`
# has to be reachable too. Mirrors `test_metrics_response_contract.py`. Without
# it this file only ran as part of the layer, behind whichever sibling module
# had already added the path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

import pytest

from .helpers import declares_null, load_interfaces, required_fields

# ── The oracle these assertions are pinned against ─────────────────────────

#: The consumer whose interface the assertions below encode. Asserted rather
#: than assumed by `load_interfaces`: if the types move, this fails loudly
#: instead of quietly deriving an empty field set and passing.
FRONTEND_TYPE_FILE = Path(__file__).resolve().parents[3] / "dashboard-react" / "src" / "types.ts"
PERFORMANCE_TYPE = "PerformanceMetrics"
INFRASTRUCTURE_TYPE = "InfrastructureStatus"


@pytest.fixture(autouse=True)
def _mock_state_tables():
    """Keep these contract tests hermetic — never touch real DynamoDB.

    p39 task 6.2: the executions-table stub is gone with the subject it stubbed.
    `handlers.metrics` no longer imports `executions_table` — the flat-rate cost
    fallback was its only reader — so patching that attribute would now raise
    `AttributeError`. The state table stub stays: P28 R17.1's read paths still
    Query its GSI on every `get_comprehensive_metrics` call.
    """
    # `patch("handlers.metrics.…")` resolves the attribute, so the module must be
    # imported BEFORE the patch — not by the test body underneath it. Without
    # this the file passed only when some other module in the layer happened to
    # import it first: run alone it raised `AttributeError: module 'handlers' has
    # no attribute 'metrics'` at fixture setup, before any assertion.
    from handlers import metrics  # noqa: F401

    with patch("handlers.metrics.state_table") as ms:
        # P28 R17.1: metrics read paths now Query GSIs instead of scanning
        ms.query.return_value = {"Items": []}
        yield


# ── Expected shape validators ──────────────────────────────────────────

# p39 R4.4: `not_established` is a fourth member of the trend vocabulary, for a
# window whose previous side established no baseline. It is not a direction, and
# it is deliberately not folded into `stable` — "nothing to compare against" and
# "the comparison came out level" are different facts (LGR-4854b3d-1).
TREND_VALUES = {"up", "down", "stable", "not_established"}
HEALTH_VALUES = {"healthy", "warning", "critical", "unknown"}

EXPECTED_STAGES = [
    "schema_extract",
    "schema_mapping",
    "code_generation",
    "infrastructure",
    "quality_checks",
    "pipeline_run",
]


def _assert_performance(perf: dict):
    """Validate PerformanceMetrics shape."""
    # p39 R4.9: null when no `load_metrics` row in the window carries a usable
    # `rows_written` (the source since 2026-10-10; before that, the
    # `file_validation` `record_count_estimate`). This once held only because
    # the handler substituted the count of FILES for the estimate of RECORDS — a
    # different unit under a tile labelled "Records Ingested". Amended with its
    # reason rather than deleted.
    assert perf["lines_ingested_24h"] is None or isinstance(perf["lines_ingested_24h"], (int, float))
    # Inherits R4.9's nullability: the rate divides the rows-loaded figure by the
    # window's wall clock, and there is no rate without a numerator.
    assert perf["processing_rate"] is None or isinstance(perf["processing_rate"], (int, float))
    # p39 R4.6: null when no execution reached a terminal outcome. The previous
    # assertion held only because the handler reported 100.0 for a window in
    # which nothing ran — a perfect record claimed on no evidence. Amended with
    # its reason rather than deleted; `failed_pipelines` below stays a plain
    # number because absence there is a genuine zero.
    assert perf["overall_success_rate"] is None or isinstance(perf["overall_success_rate"], (int, float))
    assert isinstance(perf["failed_pipelines"], (int, float))
    # p39 R4.1 / D1: null when no agent stage published a `Pipeline.StageDuration`
    # in the window — as here, where CloudWatch is stubbed empty. This asserted a
    # number only because the read behind it returned 0.0 both for a window with
    # no agent invocations and for a raising query, so a measured zero and an
    # absence were indistinguishable and R4.1 forbids standing the dispatcher's
    # ~1 s `Duration` in for either. Amended with its reason rather than deleted,
    # and the union is kept rather than pinned to `is None` so a populated window
    # stays a handler concern and not also a test change.
    assert perf["agent_latency_ms"] is None or isinstance(perf["agent_latency_ms"], (int, float))
    # p39 R2.4: null when no Glue run reached a terminal state in the window —
    # as here, where CloudWatch is stubbed empty. This used to assert a number
    # only because the handler fell back to counting execution-table rows and
    # pricing them at a flat $0.50, from a different table over a different
    # window; a figure it could not source became a figure it invented. `basis`
    # below is null on the same condition: there is no annotation for a sum that
    # was never taken. Amended with its reason rather than deleted, and the
    # union is kept rather than pinned to `is None` so a populated window stays
    # a handler concern and not also a test change.
    assert perf["monthly_cost"] is None or isinstance(perf["monthly_cost"], (int, float))
    assert perf["monthly_cost_basis"] is None or isinstance(perf["monthly_cost_basis"], dict)
    # Removed (no real data source): mcp_fallback_rate, cost_savings, mcp_latency

    trends = perf["trends"]
    assert trends["ingestion"] in TREND_VALUES
    assert trends["success"] in TREND_VALUES
    assert trends["agent_latency"] in TREND_VALUES
    assert trends["cost"] in TREND_VALUES


def _assert_stages(stages: dict):
    """Validate Record<string, StageMetrics> shape."""
    for key in EXPECTED_STAGES:
        assert key in stages, f"Missing stage key: {key}"
        stage = stages[key]
        assert isinstance(stage["success_count"], (int, float))
        assert isinstance(stage["error_count"], (int, float))
        # avg_time_ms is null when the stage records no duration (rendered "N/A")
        assert stage["avg_time_ms"] is None or isinstance(stage["avg_time_ms"], (int, float))


def _assert_infrastructure(infra: dict):
    """Validate InfrastructureStatus shape."""
    # agentcore
    ac = infra["agentcore"]
    # p39 R4.8: null when the agent error-rate query established no rate — a
    # raising query, or a window with no agent invocations. This used to assert a
    # number only because both cases returned an error rate of 0.0, which became
    # an availability of 100.0 and a status of `healthy`: a perfect record
    # claimed on the one event that proves nothing was observed. `status` then
    # reads `unknown`, already a member of HEALTH_VALUES below. Amended with its
    # reason rather than deleted.
    assert ac["availability"] is None or isinstance(ac["availability"], (int, float))
    # p39 R4.1 / D1: this MIRRORS `performance.agent_latency_ms` — one fact under
    # two names, read from the same rounded local — so it inherits that field's
    # Not_Established case and cannot stay a non-null number while the other
    # admits null. A JSON-schema widening does not relax a hand-written
    # `isinstance` in a test body, which is why this line is amended here and not
    # only in `schemas.py`. Amended with its reason rather than deleted.
    assert ac["response_time_ms"] is None or isinstance(ac["response_time_ms"], (int, float))
    assert ac["status"] in HEALTH_VALUES

    # dynamodb
    db = infra["dynamodb"]
    assert isinstance(db["read_capacity_per_hour"], (int, float))
    assert isinstance(db["write_capacity_per_hour"], (int, float))
    assert db["status"] in HEALTH_VALUES

    # s3
    s3 = infra["s3"]
    # p39 R1.3: null when the deployed buckets report no `BucketSizeBytes`
    # datapoint, or when `PROCESSED_BUCKET` gives the filter nothing to be
    # derived from — as here, where CloudWatch is stubbed empty. This used to
    # assert a number only because the handler returned 0 for a SEARCH that
    # matched no series at all: ~135 MB of real storage published as a measured
    # zero, from a bucket-name token no bucket name contains. `BucketSizeBytes`
    # is a daily metric, so "no series" and "measured empty" are genuinely
    # different facts (LGR-4854b3d-1). Amended with its reason rather than
    # deleted, and the union is kept rather than pinned to `is None` so a
    # populated window stays a handler concern and not also a test change.
    assert s3["storage_used_bytes"] is None or isinstance(s3["storage_used_bytes"], (int, float))
    # p39 R4.7: always null — S3 request metrics were never enabled on the
    # deployed buckets, so the `0` this used to assert was a hand-written
    # constant dressed as a measurement. Amended with its reason rather than
    # deleted; the union is kept rather than pinned to `is None` so enabling the
    # metrics later is a handler change and not also a test change.
    assert s3["requests_per_minute"] is None or isinstance(s3["requests_per_minute"], (int, float))
    assert s3["status"] in HEALTH_VALUES

    # lambda
    lam = infra["lambda"]
    assert isinstance(lam["invocations_per_minute"], (int, float))
    assert isinstance(lam["error_rate"], (int, float))
    assert lam["status"] in HEALTH_VALUES


# ── Contract test ──────────────────────────────────────────────────────


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_comprehensive_metrics_matches_typescript_interface(mock_cw, mock_cache):
    """Response from get_comprehensive_metrics matches ComprehensiveMetrics TS interface."""
    mock_cache.get_item.return_value = {}
    mock_cw.get_metric_data.return_value = {"MetricDataResults": []}

    from handlers.metrics import get_comprehensive_metrics

    result = get_comprehensive_metrics({"queryStringParameters": {"period": "24h"}})
    assert result["statusCode"] == 200

    body = json.loads(result["body"])

    # Top-level keys
    assert "performance" in body
    assert "stages" in body
    assert "infrastructure" in body

    _assert_performance(body["performance"])
    _assert_stages(body["stages"])
    _assert_infrastructure(body["infrastructure"])


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_response_is_valid_json_with_cors_headers(mock_cw, mock_cache):
    """Response has correct status code, JSON body, and CORS headers."""
    mock_cache.get_item.return_value = {}
    mock_cw.get_metric_data.return_value = {"MetricDataResults": []}

    from handlers.metrics import get_comprehensive_metrics

    result = get_comprehensive_metrics({"queryStringParameters": None})

    assert result["statusCode"] == 200
    assert result["headers"]["Content-Type"] == "application/json"
    assert result["headers"]["Access-Control-Allow-Origin"] == "*"

    # Body must be valid JSON
    body = json.loads(result["body"])
    assert isinstance(body, dict)


# ── Guard on the oracle, not on the handler ────────────────────────────────
#
# Every `is None or isinstance(...)` above is only meaningful while the frontend
# interface still declares that field required and nullable. Drop the `| null`
# and the handler's `null` becomes a type violation the consumer never sees; add
# a `?` and absence becomes legal, which is a different contract from
# present-and-null. Either would silently change what the assertions above mean.
#
# `LGR-99050e6-5` (template / ratified) is the pattern and the prohibition: it
# taught the frontend-type reader nullable unions and added exactly this guard,
# having rejected "narrow the frontend type to make the parser complaint go away"
# on the ground that the union is the true wire shape. `LGR-4854b3d-1` (template
# / ratified) is the same rule from the value side — a three-state value is never
# collapsed to one of its ends.

#: Performance fields the API can publish as `null`, each with the requirement
#: that made it three-state. A field here must be **required and nullable**.
NULLABLE_PERFORMANCE_FIELDS = {
    "lines_ingested_24h": "R4.9 — no `load_metrics` row in the window carries a usable `rows_written`",
    "processing_rate": "inherits R4.9 — no numerator, no rate",
    "overall_success_rate": "R4.6 — no execution reached a terminal outcome",
    "agent_latency_ms": "R4.1 / D1 — no agent stage published a duration",
    "monthly_cost": "R2.4 — no Glue run reached a terminal state",
    "monthly_cost_basis": "R2.6 — no basis for a sum that was never taken",
    "terminal_counts": "R5.4 — null, not four zeros, when the read did not complete",
    "stage_failure_attribution": (
        "p59 — null, not a dict of zeros, when the `pipeline_failure` partition could not be read; "
        "zeros there would read as 'no run failed outside a stage row'"
    ),
    "rejected_at_approval": (
        "2026-10-10 — null, not 0, when the `pipeline_failure` partition could not be read; "
        "0 there would read as 'nobody rejected anything'"
    ),
}

#: Performance fields that must stay **required and NOT nullable**, so the guard
#: fails a widening as well as a narrowing. `failed_pipelines` is the one the
#: task explicitly holds: an `ExecutionsFailed` sum with no datapoints is a
#: genuine zero. The three maps are always emitted, empty at worst.
NON_NULL_PERFORMANCE_FIELDS = ("failed_pipelines", "measurement", "determined_from", "missing", "trends")

#: `infrastructure.<service>.<field>` figures that are three-state for the same
#: reasons. `agentcore.response_time_ms` mirrors `performance.agent_latency_ms`.
NULLABLE_INFRASTRUCTURE_FIELDS = {
    "agentcore": {
        "availability": "R4.8 — the agent error-rate query established no rate",
        "response_time_ms": "R4.1 / D1 — mirrors performance.agent_latency_ms",
    },
    "s3": {
        "storage_used_bytes": "R1.3 — no `BucketSizeBytes` datapoint for the buckets",
        "requests_per_minute": "R4.7 — request metrics were never enabled",
    },
}


@pytest.fixture(scope="module")
def frontend_types() -> dict[str, dict[str, tuple[bool, str]]]:
    """The two interfaces the metrics response must satisfy."""
    return load_interfaces(FRONTEND_TYPE_FILE, PERFORMANCE_TYPE, INFRASTRUCTURE_TYPE)


def _inline_object_fields(declared: str, label: str) -> dict[str, str]:
    """Field -> declared type for a one-line inline object literal.

    ``InfrastructureStatus`` declares each service as an inline literal
    (``agentcore: { availability: number | null; ... }``), so ``declares_null``
    cannot be pointed at the service's own type — it would split the whole
    literal on ``|`` and find no bare ``null`` member. Reading the literal's
    members lets the same guard apply one level down.

    Raises rather than returning an empty map, for `helpers.py`'s stated reason:
    a guard that checked nothing would pass vacuously.
    """
    literal = declared.strip()
    assert literal.startswith("{") and literal.endswith("}"), (
        f"{label} is declared {declared!r}, not an inline object literal. If it became a named "
        f"interface, point this guard at that interface instead of loosening it."
    )
    fields = {}
    for member in literal[1:-1].split(";"):
        member = member.strip()
        if not member:
            continue
        name, separator, member_type = member.partition(":")
        assert separator, f"Unparsed member in {label}: {member!r}"
        fields[name.strip()] = member_type.strip()
    assert fields, f"{label} parsed as empty — every assertion built on it would pass vacuously"
    return fields


@pytest.mark.parametrize(("field", "reason"), sorted(NULLABLE_PERFORMANCE_FIELDS.items()))
def test_performance_interface_still_declares_field_required_and_nullable(frontend_types, field, reason):
    """`PerformanceMetrics.<field>: … | null`, and not optional."""
    interface = frontend_types[PERFORMANCE_TYPE]
    assert field in interface, f"{PERFORMANCE_TYPE} no longer declares {field} ({reason})"
    required, ts_type = interface[field]
    assert required, (
        f"{PERFORMANCE_TYPE}.{field} became optional (`?:`). The API always emits the key — {reason} "
        f"makes it null, never absent — and a consumer told absence is possible narrows differently."
    )
    assert declares_null(ts_type), (
        f"{PERFORMANCE_TYPE}.{field} is declared {ts_type!r}, which does not admit null. The API "
        f"publishes null when {reason}, so this type is now wrong — do not resolve that by narrowing."
    )


@pytest.mark.parametrize("field", NON_NULL_PERFORMANCE_FIELDS)
def test_performance_interface_keeps_non_nullable_fields_non_nullable(frontend_types, field):
    """The other half of the guard: these must NOT gain a null arm.

    A null here would be the defect this spec removes, in reverse — a genuine
    zero or an empty map reported as "nothing was established".
    """
    interface = frontend_types[PERFORMANCE_TYPE]
    assert field in interface, f"{PERFORMANCE_TYPE} no longer declares {field}"
    required, ts_type = interface[field]
    assert required, f"{PERFORMANCE_TYPE}.{field} became optional (`?:`); the API always emits it."
    assert not declares_null(ts_type), (
        f"{PERFORMANCE_TYPE}.{field} is declared {ts_type!r}, which admits null. Absence there is a "
        f"genuine zero (or an empty map), not an unestablished value — see the handler's fact registry."
    )


@pytest.mark.parametrize("service", sorted(NULLABLE_INFRASTRUCTURE_FIELDS))
def test_infrastructure_interface_still_declares_its_nullable_figures(frontend_types, service):
    """`InfrastructureStatus.<service>` keeps the null arms its reads produce."""
    required, declared = frontend_types[INFRASTRUCTURE_TYPE][service]
    assert required, f"{INFRASTRUCTURE_TYPE}.{service} became optional (`?:`); the API always emits it."
    members = _inline_object_fields(declared, f"{INFRASTRUCTURE_TYPE}.{service}")
    for field, reason in NULLABLE_INFRASTRUCTURE_FIELDS[service].items():
        assert field in members, f"{INFRASTRUCTURE_TYPE}.{service} no longer declares {field} ({reason})"
        assert declares_null(members[field]), (
            f"{INFRASTRUCTURE_TYPE}.{service}.{field} is declared {members[field]!r}, which does not "
            f"admit null. The API publishes null when {reason} — do not resolve that by narrowing."
        )


def test_performance_interface_declares_no_field_the_response_omits(frontend_types):
    """Every required field of the interface is present in a real response.

    The widening added five keys to `PerformanceMetrics`. This is what makes the
    addition symmetric: a key declared required and never emitted arrives as
    `undefined`, passes a `!== null` narrowing, and throws on the access after it.
    """
    with (
        patch("handlers.metrics._cache_table") as mock_cache,
        patch("handlers.metrics.cloudwatch_client") as mock_cw,
    ):
        mock_cache.get_item.return_value = {}
        mock_cw.get_metric_data.return_value = {"MetricDataResults": []}

        from handlers.metrics import get_comprehensive_metrics

        body = json.loads(get_comprehensive_metrics({"queryStringParameters": {"period": "24h"}})["body"])

    missing = required_fields(frontend_types[PERFORMANCE_TYPE]) - set(body["performance"])
    assert not missing, f"performance omits {sorted(missing)}, declared required by {PERFORMANCE_TYPE}"
