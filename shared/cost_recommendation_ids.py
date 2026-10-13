# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Stable identifiers for cost-optimization recommendations (p30a R1.4).

The cost optimization agent does not emit an id per recommendation. It writes a
list of recommendation dicts inside a single ``step_name="cost_optimization"``
state row, so a recommendation is only identifiable by something derived from
its own fields plus its position in that list.

Two separate processes have to agree on that derivation:

* ``handlers/agents.py::get_cost_recommendations`` synthesises the id the
  dashboard renders as a React key and sends back on Apply.
* ``agentcore/cost_optimization_agent/main.py``'s apply branch resolves that id
  back to exactly one recommendation in the same row, so Apply changes one job
  instead of rescanning the fleet.

They live in different deployment units — the dashboard API Lambda gets this
module through the shared layer, the agent gets it through ``COPY shared
/app/shared`` in its AgentCore image — so the derivation lives here once rather
than being written twice. If the two copies drifted, Apply would fail to find a
recommendation the UI had just displayed, and the only symptom would be a
"recommendation not found" error for an id the UI itself produced.

Ids are NOT persisted: the agent's stored row has no ``id`` field, and the
handler's synthesis mutates only its own in-memory copy of the response. That
is why both sides derive rather than look up.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def derive_recommendation_id(recommendation: Mapping[str, Any], index: int) -> str:
    """Return the stable id for one recommendation from a cost_optimization row.

    An explicit ``id`` on the recommendation wins, so a future agent version can
    start emitting real ids without breaking either side. Otherwise the id is
    ``"{job_name|pipeline_id|rec-{index}}:{type}"``.

    Args:
        recommendation: One entry from a cost_optimization row's
            ``recommendations`` list.
        index: Its position in that list. Only used for the last-resort base
            when the recommendation names neither a Glue job nor a pipeline;
            positional identity is stable because the stored list order is
            stable.

    Returns:
        The id string. Never empty.
    """
    existing = recommendation.get("id")
    if existing:
        return str(existing)

    base = recommendation.get("job_name") or recommendation.get("pipeline_id") or f"rec-{index}"
    rec_type = recommendation.get("type") or "rec"
    return f"{base}:{rec_type}"


def derive_recommendation_ids(recommendations: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the derived id for every recommendation, in stored order."""
    return [derive_recommendation_id(rec, idx) for idx, rec in enumerate(recommendations)]


def find_recommendation_by_id(
    recommendations: Sequence[Mapping[str, Any]],
    recommendation_id: str,
) -> Mapping[str, Any] | None:
    """Return the single recommendation whose derived id matches, else ``None``.

    Returning ``None`` rather than raising keeps the caller in control of how a
    miss is reported. A miss must never be treated as "apply everything" — that
    is the blast-radius failure p30a R1.4 exists to remove.
    """
    if not recommendation_id:
        return None

    for idx, rec in enumerate(recommendations):
        if derive_recommendation_id(rec, idx) == recommendation_id:
            return rec
    return None
