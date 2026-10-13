# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests: the mapping-compatibility filter, over arbitrary candidate sets.

Two universal properties the example tests in
``test_mapping_source_schema_compatibility.py`` can only sample:

**Property 1 — an incompatible mapping is never substituted.** For any set of
approved candidates and any non-empty run schema hash, the mapping
``_select_freshest_mapping`` returns is either the incumbent (unchanged, so the
function made no claim about it — the incumbent is validated downstream by
``code_assembly._validate_mapped_source_fields``) or one whose
``source_schema_hash`` equals the run's. There is no third outcome. This is the
property whose absence produced ``KeyError: 'email'`` on
``run-2026-08-15-19-15-27-e99dbcbe``.

**Property 2 — an empty run hash changes nothing.** For any candidate set, the
id selected with ``run_schema_hash=""`` is the id the pre-change recency-only
selection would have chosen. Constraint 2 as a universal statement: an
unavailable hash cannot start refusing candidates.

Tables are injected as plain stubs rather than moto resources: these properties
are about the selection expression, and a function-scoped moto fixture under
``@given`` is a hypothesis health-check error. ``test_mapping_freshness.py`` sets
the precedent (``test_lookup_error_keeps_current_mapping``).

**Validates: Requirements 6.1, 6.2, 6.3**
"""

import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_create_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "parameter_extractor": MagicMock(),
    "artifact_schema": MagicMock(),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("mapping_resolution", None)
    import mapping_resolution as _mr


RUN_HASH = "c76edff25e69575d9739faf1f5e64fbba093711c100076d9bfd4ca98050a0fe4"
_OTHER_HASHES = [
    "d9d3b7e1001dbfbb0ab0259f68fe36b62338458753807dd2f8e6fd296884f94f",
    "no-hash",
    "",
]

_IDS = st.sampled_from(["map-a", "map-b", "map-c", "map-d", "map-e"])
_TIMESTAMPS = st.sampled_from(
    [
        "",
        "2026-01-01T00:00:00",
        "2026-06-01T00:00:00",
        "2026-08-09T15:30:47.313475+00:00",
        "2026-08-15T19:08:20.979489+00:00",
        "2027-01-01T00:00:00",
    ]
)
# `None` means the record omits source_schema_hash entirely; "" means it carries
# an empty one. Both are the anomaly side of constraint 3.
_HASHES = st.one_of(st.just(RUN_HASH), st.sampled_from(_OTHER_HASHES), st.none())


@st.composite
def _candidates(draw):
    """A set of distinct candidate records, plus a subset that is dangling.

    Constrained to the shape ``store_mapping_separately`` writes and the state
    table indexes: distinct ids, a created_timestamp that may be absent, and a
    source_schema_hash that may be absent.
    """
    records = draw(
        st.dictionaries(
            keys=_IDS,
            values=st.tuples(_TIMESTAMPS, _HASHES),
            min_size=0,
            max_size=5,
        )
    )
    dangling = draw(st.sets(_IDS, max_size=2))
    return records, dangling


def _stub_tables(records, dangling, incumbent_id, incumbent_ts):
    """State-table and mappings-table stubs over ``records``.

    Every candidate id appears as an approved schema_mapping state row; ids in
    ``dangling`` additionally have no mappings-table record. The incumbent
    resolves to its own record so it is never itself dangling here.
    """
    all_ids = set(records) | set(dangling)
    state = MagicMock()
    state.query.return_value = {
        "Items": [{"mapping_id": mid, "step_name": "schema_mapping", "status": "success"} for mid in sorted(all_ids)]
    }

    def _get_item(Key):  # boto3's own keyword name — matched, not renamed
        mid = Key["mapping_id"]
        if mid == incumbent_id:
            return {"Item": {"mapping_id": mid, "created_timestamp": incumbent_ts, "source_schema_hash": RUN_HASH}}
        if mid in dangling or mid not in records:
            return {}
        ts, hsh = records[mid]
        item = {"mapping_id": mid}
        if ts:
            item["created_timestamp"] = ts
        if hsh is not None:
            item["source_schema_hash"] = hsh
        return {"Item": item}

    mappings = MagicMock()
    mappings.get_item.side_effect = _get_item
    return state, mappings


def _select(state, mappings, incumbent_id, incumbent_ts, run_hash):
    current_resp = {
        "Item": {"mapping_id": incumbent_id, "created_timestamp": incumbent_ts, "source_schema_hash": RUN_HASH}
    }
    return _mr._select_freshest_mapping(
        "pipe-prop",
        incumbent_id,
        current_resp,
        pipeline_runs_table=state,
        mappings_table=mappings,
        run_schema_hash=run_hash,
    )


# Feature: p42-mapping-schema-compatibility, Property 1: no incompatible substitution
@settings(max_examples=400, deadline=None)
@given(case=_candidates(), incumbent_ts=_TIMESTAMPS)
def test_a_substituted_mapping_always_matches_the_run_schema_hash(case, incumbent_ts):
    """The selected mapping is the incumbent, or its source_schema_hash equals
    the run's. Never a third thing.

    **Validates: Requirements 6.2, 6.3**
    """
    records, dangling = case
    incumbent_id = "map-incumbent"
    state, mappings = _stub_tables(records, dangling, incumbent_id, incumbent_ts)

    selected_id, selected_resp = _select(state, mappings, incumbent_id, incumbent_ts, RUN_HASH)

    if selected_id == incumbent_id:
        return  # the incumbent is not this function's to validate
    assert selected_resp["Item"].get("source_schema_hash") == RUN_HASH, (
        f"substituted {selected_id} built against "
        f"{selected_resp['Item'].get('source_schema_hash')!r}, run extracted {RUN_HASH!r}"
    )


# Feature: p42-mapping-schema-compatibility, Property 1b: substitution stays strictly newer
@settings(max_examples=400, deadline=None)
@given(case=_candidates(), incumbent_ts=_TIMESTAMPS)
def test_substitution_still_requires_strictly_newer(case, incumbent_ts):
    """The filter narrows eligibility; it does not relax the recency rule. A
    substituted mapping's created_timestamp is still strictly greater than the
    incumbent's.

    **Validates: Requirements 6.1, 6.3**
    """
    records, dangling = case
    incumbent_id = "map-incumbent"
    state, mappings = _stub_tables(records, dangling, incumbent_id, incumbent_ts)

    selected_id, selected_resp = _select(state, mappings, incumbent_id, incumbent_ts, RUN_HASH)

    if selected_id == incumbent_id:
        return
    assert selected_resp["Item"].get("created_timestamp", "") > incumbent_ts


# Feature: p42-mapping-schema-compatibility, Property 2: an empty run hash is inert
@settings(max_examples=400, deadline=None)
@given(case=_candidates(), incumbent_ts=_TIMESTAMPS)
def test_empty_run_hash_selects_exactly_what_recency_alone_would(case, incumbent_ts):
    """With no run hash the selection equals the recency-only answer, computed
    here independently of the function under test: eligible = every candidate
    with a resolvable record, winner = max on (created_timestamp, mapping_id),
    substituted only when strictly newer than the incumbent.

    **Validates: Requirements 6.1, 6.2, 6.3**
    """
    records, dangling = case
    incumbent_id = "map-incumbent"
    state, mappings = _stub_tables(records, dangling, incumbent_id, incumbent_ts)

    selected_id, _ = _select(state, mappings, incumbent_id, incumbent_ts, "")

    resolvable = [(ts, mid) for mid, (ts, _h) in records.items() if mid not in dangling]
    expected = incumbent_id
    if resolvable:
        best_ts, best_id = max(resolvable)
        if best_ts > incumbent_ts:
            expected = best_id

    assert selected_id == expected
