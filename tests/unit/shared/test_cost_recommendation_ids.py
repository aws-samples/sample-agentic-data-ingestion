# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the shared cost-recommendation id derivation (p30a R1.4).

These ids are the only handle the dashboard has on one entry inside a
``step_name="cost_optimization"`` state row, and the agent's apply branch has to
resolve the same id back to the same entry. What matters is therefore not the
exact format but that derivation is deterministic, unique enough to address one
recommendation, and that a lookup miss is a miss rather than a wrong match.

**Validates: Requirements 1.4**
"""

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "shared"))

from cost_recommendation_ids import (
    derive_recommendation_id,
    derive_recommendation_ids,
    find_recommendation_by_id,
)


def _rec(**overrides):
    base = {
        "job_name": "agentic_data_ingestion_etl_1",
        "pipeline_id": "pipe-1",
        "type": "downsize",
    }
    base.update(overrides)
    return base


class TestDeriveRecommendationId:
    def test_uses_job_name_and_type(self):
        assert derive_recommendation_id(_rec(), 0) == "agentic_data_ingestion_etl_1:downsize"

    def test_falls_back_to_pipeline_id_when_no_job_name(self):
        assert derive_recommendation_id(_rec(job_name=""), 0) == "pipe-1:downsize"

    def test_falls_back_to_index_when_nothing_identifies_the_job(self):
        assert derive_recommendation_id({"type": "idle"}, 3) == "rec-3:idle"

    def test_explicit_id_wins(self):
        # So a future agent version can emit real ids without breaking either side.
        assert derive_recommendation_id(_rec(id="rec-abc123"), 0) == "rec-abc123"

    def test_is_deterministic(self):
        rec = _rec()
        assert derive_recommendation_id(rec, 0) == derive_recommendation_id(rec, 0)

    def test_same_job_different_types_get_different_ids(self):
        # A job can appear twice in one report (e.g. downsize + idle); Apply has
        # to be able to address them separately.
        downsize = derive_recommendation_id(_rec(type="downsize"), 0)
        idle = derive_recommendation_id(_rec(type="idle"), 1)
        assert downsize != idle

    def test_missing_type_still_yields_an_id(self):
        assert derive_recommendation_id({"job_name": "j1"}, 0) == "j1:rec"


class TestFindRecommendationById:
    def test_finds_the_one_matching_recommendation(self):
        recs = [
            _rec(job_name="job-a", type="downsize"),
            _rec(job_name="job-b", type="idle"),
            _rec(job_name="job-c", type="upsize"),
        ]
        found = find_recommendation_by_id(recs, "job-b:idle")
        assert found is recs[1]

    def test_unknown_id_returns_none(self):
        recs = [_rec(job_name="job-a", type="downsize")]
        assert find_recommendation_by_id(recs, "job-z:downsize") is None

    def test_empty_id_returns_none(self):
        assert find_recommendation_by_id([_rec()], "") is None

    def test_empty_list_returns_none(self):
        assert find_recommendation_by_id([], "job-a:downsize") is None

    def test_every_derived_id_resolves_back_to_its_own_recommendation(self):
        # The round-trip property the two call sites depend on.
        recs = [
            _rec(job_name="job-a", type="downsize"),
            _rec(job_name="job-a", type="idle"),
            {"type": "idle"},
            _rec(job_name="", pipeline_id="pipe-9", type="upsize"),
        ]
        for idx, rec_id in enumerate(derive_recommendation_ids(recs)):
            assert find_recommendation_by_id(recs, rec_id) is recs[idx]
