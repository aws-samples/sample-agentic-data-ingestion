# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /api/cost-recommendations top-level response shape (p30a R1.3).

``get_cost_recommendations`` reads a ``step_name="cost_optimization"`` state row
in which the agent nests its report, and **lifts** ``recommendations`` and
``total_projected_savings_monthly`` to the TOP LEVEL of the HTTP response. That
lift is not decoration: returning the whole state row under ``recommendations``
makes the dashboard see an object where its type declares a list, so
``MetricsDashboardPage.tsx`` renders nothing at all and the Cost Recommendations
panel simply stays hidden — an empty panel, not an error. That is a failure mode
with no symptom, which is why it is worth a test rather than a comment.

It has already been got wrong once: commit ``c1462286e1`` ("Fix metrics
dashboard: cost recs …") both paginated the scan and introduced this shape, and
the handler's own comment records the reasoning. This file exists so that fix
cannot be undone silently.

**Pinned against the frontend type, not against the handler.** The expected
field sets below are PARSED at test time out of::

    dashboard-react/src/hooks/use-cost-recommendations.ts

(``CostRecommendationsResponse`` / ``CostRecommendation``), which is the type
``useCostRecommendations`` instantiates ``apiClient.get<…>`` with. Deriving the
expectations from the consumer is the whole point: a contract test written from
the producer's current output cannot detect the producer drifting, because the
producer's output *is* its own oracle. Parsed this way, adding a required field
to the TypeScript interface fails this test until the handler supplies it.

Two properties of ``id`` are deliberately pinned:

* Ids are **not persisted**. The stored row carries no ``id`` (see the fixture
  below — it omits one on purpose), and the handler synthesises one per
  recommendation at read time via ``shared/cost_recommendation_ids.py``. The
  dashboard uses that id as its React key AND posts it back on Apply, where
  ``cost_optimization_agent`` re-derives it from the same row. So ``id`` is
  asserted as a *derived, always-present* field, which is the property the
  Apply round-trip depends on.
* The derivation must agree with the shared module, not merely be non-empty.

The scan is exercised **paginated**, with the newest row on the second page.
``cost_optimization`` rows are a tiny fraction of the state table and DynamoDB's
``Limit`` caps items *evaluated*, not items *matched*, so the handler pages
through the filtered scan (precedent: ``LGR-dd80c6d-1``, which paginated the
previously unbounded dashboard-API scans). A single-page fixture would let a
regression to one capped scan pass.

Handler dispatch behaviour for the Apply endpoint is covered by
``tests/unit/lambdas/dashboard_api/test_agents_async_dispatch.py``; the id
derivation itself by ``tests/unit/shared/test_cost_recommendation_ids.py``. This
file pins only the **wire format** of the GET.

Validates: Requirements 1.3
"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — handlers/common.py resolves its table names at import time
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parents[3]
LAMBDA_DIR = REPO_DIR / "lambda" / "agentic_data_ingestion_dashboard_api"
SHARED_DIR = REPO_DIR / "shared"
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
# handlers/common.py calls require_env for each table at import time, before any
# fixture can run — mirrors test_metrics_response_contract.py. The real table
# handle is replaced by a mock in the fixtures below; nothing here touches AWS.
for _table_env, _table_name in (
    ("STATE_TABLE", "agentic_data_ingestion_state"),
    ("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"),
    ("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"),
    ("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code"),
    ("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results"),
    ("APPROVALS_TABLE", "agentic_data_ingestion_approvals"),
    ("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"),
    ("AUDIT_TABLE", "agentic_data_ingestion_audit_log"),
    ("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces"),
):
    os.environ.setdefault(_table_env, _table_name)

sys.modules.pop("lambda_function", None)

from handlers import agents as _agents

from cost_recommendation_ids import derive_recommendation_id

# The TypeScript reader below used to live in this file. It moved to `helpers.py`
# when the /costs contract test needed it too — one parser rather than two that
# drift (the copy here had no nullable-union branch). Behaviour is unchanged for
# this file's types.
from .helpers import json_types as _json_types
from .helpers import read_interface as _read_interface

#: The consumer this contract is pinned against. Its path is asserted rather
#: than assumed: if the hook is moved or renamed, this test must fail loudly
#: instead of quietly deriving an empty set of required fields and passing.
FRONTEND_TYPE_FILE = REPO_DIR / "dashboard-react" / "src" / "hooks" / "use-cost-recommendations.ts"
RESPONSE_TYPE = "CostRecommendationsResponse"
ITEM_TYPE = "CostRecommendation"


@pytest.fixture(scope="module")
def frontend_types() -> dict[str, dict[str, tuple[bool, str]]]:
    """The two frontend interfaces this endpoint's response must satisfy."""
    assert FRONTEND_TYPE_FILE.is_file(), (
        f"{FRONTEND_TYPE_FILE} is missing. This contract test derives its expectations from the "
        f"frontend {RESPONSE_TYPE} type; if the hook moved, repoint FRONTEND_TYPE_FILE rather than "
        f"deleting the assertion."
    )
    source = FRONTEND_TYPE_FILE.read_text(encoding="utf-8")
    types = {
        RESPONSE_TYPE: _read_interface(source, RESPONSE_TYPE, FRONTEND_TYPE_FILE),
        ITEM_TYPE: _read_interface(source, ITEM_TYPE, FRONTEND_TYPE_FILE),
    }
    # Guard against a vacuous pass: an empty or unrecognisable interface would
    # make every assertion below trivially true.
    assert types[RESPONSE_TYPE], f"{RESPONSE_TYPE} parsed as empty"
    assert types[ITEM_TYPE], f"{ITEM_TYPE} parsed as empty"
    return types


def _required(types: dict[str, dict[str, tuple[bool, str]]], name: str) -> set[str]:
    return {field for field, (required, _) in types[name].items() if required}


# ---------------------------------------------------------------------------
# Fixture data — shaped as the agent actually writes it
# ---------------------------------------------------------------------------
# Faithful to agentcore/cost_optimization_agent/main.py: the report's
# `recommendations` / `total_projected_savings_monthly` / `generated_at` are
# merged into the state item, floats are stored as Decimal, and NO `id` is
# written on any recommendation.
_OLDER_ROW: dict[str, Any] = {
    "run_id": "cost-run-2026-07-20",
    "step_name": "cost_optimization",
    "agent_type": "cost_optimization",
    "timestamp": "2026-07-20T06:00:00+00:00",
    "generated_at": "2026-07-20T06:00:00+00:00",
    "recommendations": [
        {
            "pipeline_id": "pipe-stale",
            "job_name": "agentic_data_ingestion_pipe_stale",
            "type": "downsize",
            "risk": "low",
            "projected_savings_monthly": Decimal("1.00"),
        }
    ],
    "total_projected_savings_monthly": Decimal("1.00"),
}

_LATEST_ROW: dict[str, Any] = {
    "run_id": "cost-run-2026-07-27",
    "step_name": "cost_optimization",
    "agent_type": "cost_optimization",
    "timestamp": "2026-07-27T06:00:00+00:00",
    "generated_at": "2026-07-27T06:00:00+00:00",
    "jobs_scanned": 12,
    "auto_applied_count": 0,
    "total_duration_ms": 41_233,
    "recommendations": [
        # over-provisioned (detect_over_provisioned)
        {
            "pipeline_id": "pipe-over",
            "job_name": "agentic_data_ingestion_pipe_over",
            "type": "downsize",
            "current": {"worker_type": "G.2X", "workers": 10},
            "recommended": {"worker_type": "G.1X", "workers": 5},
            "avg_dpu_utilization": Decimal("0.22"),
            "run_count": 8,
            "risk": "low",
            "auto_apply_eligible": True,
            "projected_savings_monthly": Decimal("184.50"),
        },
        # under-provisioned (detect_under_provisioned)
        {
            "pipeline_id": "pipe-under",
            "job_name": "agentic_data_ingestion_pipe_under",
            "type": "upsize",
            "current": {"worker_type": "G.1X", "workers": 2},
            "recommended": {"worker_type": "G.2X", "workers": 4},
            "reason": "oom_failures",
            "avg_execution_time_sec": Decimal("612.4"),
            "oom_failure_count": 3,
            "risk": "medium",
            "auto_apply_eligible": False,
            "projected_savings_monthly": Decimal("0"),
        },
        # idle (detect_idle_pipelines) — no current/recommended at all
        {
            "pipeline_id": "pipe-idle",
            "job_name": "agentic_data_ingestion_pipe_idle",
            "type": "idle",
            "last_run": "never",
            "idle_days": 61,
            "recommendation": "Archive pipeline — no runs in 61 days",
            "risk": "low",
            "auto_apply_eligible": False,
            "projected_savings_monthly": Decimal("57.25"),
        },
    ],
    "total_projected_savings_monthly": Decimal("241.75"),
}


def _paginated_scan(pages: list[list[dict[str, Any]]]) -> MagicMock:
    """A state table whose filtered scan spans several pages.

    ``LastEvaluatedKey`` is returned on every page but the last, so the
    handler's pagination loop is what finds the newest row. A fixture that
    answered in one page would keep passing if the loop were removed.

    Pages are deep-copied per call because the handler assigns ``rec["id"]``
    onto the recommendation dicts it got back from ``scan`` — it mutates its
    input, not a copy of it. Harmless in the Lambda (each invocation gets a
    fresh boto3 response) but it would leak ids into the module-level fixture
    rows here and make the "ids are not persisted" assertion vacuous.
    """
    table = MagicMock()
    responses = []
    for idx, page in enumerate(deepcopy(pages)):
        resp: dict[str, Any] = {"Items": page}
        if idx < len(pages) - 1:
            resp["LastEvaluatedKey"] = {"run_id": f"page-{idx}", "step_name": "cost_optimization"}
        responses.append(resp)
    table.scan.side_effect = responses
    return table


@pytest.fixture
def latest_on_second_page(mock_lambda_env, monkeypatch) -> MagicMock:
    """Newest ``cost_optimization`` row arrives on page 2 of the scan."""
    table = _paginated_scan([[_OLDER_ROW], [_LATEST_ROW]])
    monkeypatch.setattr(_agents, "state_table", table)
    return table


@pytest.fixture
def no_rows(mock_lambda_env, monkeypatch) -> MagicMock:
    """The state of a fresh environment: the agent has never run."""
    table = _paginated_scan([[]])
    monkeypatch.setattr(_agents, "state_table", table)
    return table


def _parse(response: dict) -> tuple[int, dict]:
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
class TestCostRecommendationsResponseContract:
    """GET /api/cost-recommendations must satisfy ``CostRecommendationsResponse``."""

    def test_scan_is_paginated(self, latest_on_second_page):
        """The handler must page; a single capped scan misses matching rows."""
        status, _ = _parse(_agents.get_cost_recommendations())
        assert status == 200
        assert latest_on_second_page.scan.call_count == 2, (
            "get_cost_recommendations stopped paginating the filtered scan. DynamoDB's Limit caps "
            "items EVALUATED, not items MATCHED, and cost_optimization rows are a tiny fraction of "
            "the state table (LGR-dd80c6d-1)."
        )

    def test_response_has_every_required_top_level_field(self, latest_on_second_page, frontend_types):
        status, body = _parse(_agents.get_cost_recommendations())
        assert status == 200
        missing = _required(frontend_types, RESPONSE_TYPE) - set(body)
        assert not missing, (
            f"Response is missing {sorted(missing)}, which the frontend type {RESPONSE_TYPE} "
            f"({FRONTEND_TYPE_FILE.name}) declares required. useCostRecommendations reads these at "
            f"the TOP LEVEL of the body."
        )

    def test_recommendations_is_a_top_level_list(self, latest_on_second_page, frontend_types):
        """The lift itself: a list at the top level, never nested one deeper."""
        _, body = _parse(_agents.get_cost_recommendations())
        assert isinstance(body.get("recommendations"), list), (
            f"body['recommendations'] is {type(body.get('recommendations')).__name__}, but the "
            f"frontend type {RESPONSE_TYPE}.recommendations is "
            f"{frontend_types[RESPONSE_TYPE]['recommendations'][1]}. MetricsDashboardPage reads "
            f"costData.recommendations?.length and maps over it, so anything but a top-level list "
            f"hides the Cost Recommendations panel with no error shown."
        )
        assert len(body["recommendations"]) == len(_LATEST_ROW["recommendations"]), (
            "The lift must carry every recommendation from the newest cost_optimization row."
        )

    def test_savings_total_is_a_top_level_number(self, latest_on_second_page):
        _, body = _parse(_agents.get_cost_recommendations())
        assert "total_projected_savings_monthly" in body, (
            f"total_projected_savings_monthly disappeared from the top level. "
            f"{RESPONSE_TYPE} declares it required and the panel header renders "
            f"costData.total_projected_savings_monthly.toFixed(2)."
        )
        total = body["total_projected_savings_monthly"]
        assert isinstance(total, (int, float)) and not isinstance(total, bool), (
            f"total_projected_savings_monthly is {type(total).__name__}; {RESPONSE_TYPE} declares "
            f"number, and .toFixed(2) is called on it."
        )
        assert total == pytest.approx(float(_LATEST_ROW["total_projected_savings_monthly"]))

    def test_top_level_field_types_match_the_frontend_type(self, latest_on_second_page, frontend_types):
        _, body = _parse(_agents.get_cost_recommendations())
        for field, (required, ts_type) in frontend_types[RESPONSE_TYPE].items():
            if not required and field not in body:
                continue
            expected = _json_types(ts_type)
            assert isinstance(body[field], expected) and not isinstance(body[field], bool), (
                f"{RESPONSE_TYPE}.{field} is declared {ts_type}, response carried {type(body[field]).__name__}"
            )

    def test_latest_row_wins(self, latest_on_second_page):
        """Newest row by timestamp, not merely the first page's row."""
        _, body = _parse(_agents.get_cost_recommendations())
        assert body["generated_at"] == _LATEST_ROW["generated_at"]

    def test_empty_state_still_satisfies_the_contract(self, no_rows, frontend_types):
        """Agent has never run: the shape must hold, with an empty list.

        This is the response the panel gets today (design §5 O1 — zero
        cost_optimization rows exist in dev), so it is the shape most likely to
        be relied upon, and the one most easily broken by an early return.
        """
        status, body = _parse(_agents.get_cost_recommendations())
        assert status == 200
        missing = _required(frontend_types, RESPONSE_TYPE) - set(body)
        assert not missing, f"Empty-state response is missing required {RESPONSE_TYPE} fields: {sorted(missing)}"
        assert body["recommendations"] == []
        assert body["total_projected_savings_monthly"] == 0


class TestRecommendationItemContract:
    """Each entry must satisfy the frontend ``CostRecommendation`` type."""

    def test_every_recommendation_has_all_required_fields(self, latest_on_second_page, frontend_types):
        _, body = _parse(_agents.get_cost_recommendations())
        required = _required(frontend_types, ITEM_TYPE)
        for idx, rec in enumerate(body["recommendations"]):
            missing = required - set(rec)
            assert not missing, (
                f"recommendations[{idx}] is missing {sorted(missing)}, which the frontend type "
                f"{ITEM_TYPE} ({FRONTEND_TYPE_FILE.name}) declares required."
            )

    def test_recommendation_field_types_match_the_frontend_type(self, latest_on_second_page, frontend_types):
        _, body = _parse(_agents.get_cost_recommendations())
        for idx, rec in enumerate(body["recommendations"]):
            for field, (required, ts_type) in frontend_types[ITEM_TYPE].items():
                if not required and field not in rec:
                    continue
                if isinstance(rec[field], bool) and ts_type != "boolean":
                    raise AssertionError(f"recommendations[{idx}].{field} is a bool, declared {ts_type}")
                assert isinstance(rec[field], _json_types(ts_type)), (
                    f"{ITEM_TYPE}.{field} is declared {ts_type}, recommendations[{idx}] carried "
                    f"{type(rec[field]).__name__}"
                )

    def test_nested_worker_shapes_match(self, latest_on_second_page):
        """``current`` / ``recommended`` carry the two keys the panel renders."""
        _, body = _parse(_agents.get_cost_recommendations())
        for idx, rec in enumerate(body["recommendations"]):
            for field in ("current", "recommended"):
                if field not in rec:
                    continue  # optional — the `idle` type omits both
                assert set(rec[field]) >= {"worker_type", "workers"}, (
                    f"recommendations[{idx}].{field} is missing worker_type/workers; "
                    f"{ITEM_TYPE}.{field} declares "
                    "{ worker_type: string; workers: number } and the panel renders "
                    "`${worker_type} × ${workers}`."
                )

    def test_id_is_derived_present_and_unique(self, latest_on_second_page):
        """``id`` is synthesised at read time — it is never stored.

        The fixture row deliberately carries no ``id`` on any recommendation,
        matching what the agent writes. The dashboard uses this value as its
        React key and posts it back to
        ``POST /cost-recommendations/{id}/apply``, where the agent re-derives
        it from the same row, so "always present and agreeing with the shared
        derivation" is the property the Apply round-trip rests on.
        """
        assert all("id" not in rec for rec in _LATEST_ROW["recommendations"]), (
            "Fixture drift: the stored cost_optimization row must NOT contain ids — the point of "
            "this test is that the handler derives them."
        )
        _, body = _parse(_agents.get_cost_recommendations())
        ids = [rec.get("id") for rec in body["recommendations"]]
        assert all(isinstance(rec_id, str) and rec_id for rec_id in ids), (
            f"Every recommendation must carry a non-empty derived id; {ITEM_TYPE}.id is required "
            f"and is used as the React key. Got {ids!r}."
        )
        assert len(set(ids)) == len(ids), f"Derived recommendation ids must be unique, got {ids!r}"

        expected = [derive_recommendation_id(rec, idx) for idx, rec in enumerate(_LATEST_ROW["recommendations"])]
        assert ids == expected, (
            "Derived ids diverged from shared/cost_recommendation_ids.py. The agent's apply branch "
            "resolves the id the dashboard sends back using that same module, so a private "
            "derivation here breaks Apply for ids the UI itself produced."
        )

    def test_type_and_risk_are_non_empty_strings(self, latest_on_second_page):
        """Structural check only — the ``type`` union is knowingly out of date.

        ``CostRecommendation.type`` declares ``'downsize' | 'idle' | 'upgrade'``,
        but ``cost_optimization_agent/main.py`` emits ``downsize``, ``idle`` and
        ``upsize`` — ``upsize`` is absent from the union and ``upgrade`` is
        never produced. The divergence is type-level only: the panel renders
        ``rec.type`` into a Badge and branches on ``=== 'idle'``, so an
        unlisted literal still renders. Asserting the declared union here would
        pin a contract neither side keeps, so this test pins what both sides do
        agree on. Fixing the union is a frontend change, out of scope for a
        contract test.
        """
        _, body = _parse(_agents.get_cost_recommendations())
        for idx, rec in enumerate(body["recommendations"]):
            for field in ("type", "risk"):
                assert isinstance(rec[field], str) and rec[field], (
                    f"recommendations[{idx}].{field} must be a non-empty string; the panel renders "
                    f"it into a Badge and filters on it."
                )
        assert {rec["risk"] for rec in body["recommendations"]} <= {"low", "medium", "high"}, (
            f"risk left the union {ITEM_TYPE}.risk declares; the panel maps it to a Badge colour with no fallback."
        )
