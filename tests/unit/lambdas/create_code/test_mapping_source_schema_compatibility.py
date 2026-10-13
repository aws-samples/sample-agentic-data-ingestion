# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: a substituted mapping must have been built against THIS run's schema.

``_select_freshest_mapping`` selected on recency alone. "Newest approved mapping
for this pipeline" silently means "newest mapping for whatever the source looked
like when it was built", so a mapping built against an older or a different
source schema could displace one built against the schema the run actually
extracted. Both hashes were already being written down — the run's
``schema_hash`` on its ``schema_extract`` state row, the mapping's
``source_schema_hash`` on its mappings-table record — and nothing compared them.

**The live failure this pins.** Account 777788889999 / us-west-2, pipeline
``latesttest-50e07536``, run ``run-2026-08-15-19-15-27-e99dbcbe``:

| | ``a5a00ebac4a903530e6a785af8295005`` | ``96ca07130bea2308215a46b321c5ee63`` |
|---|---|---|
| role | state-carried | substituted, and used |
| ``created_timestamp`` | ``2026-08-09T15:30:47`` | ``2026-08-15T19:08:20`` |
| ``source_schema_hash`` | ``c76edff2…`` | ``d9d3b7e1…`` |
| maps email as | ``email_address → email`` | ``email → email`` |

The run's own ``schema_extract`` row carried ``schema_hash = c76edff2…`` and an
``extracted_schema`` declaring ``email_address``. The newer mapping won on
recency for all three regeneration attempts (``regeneration_attempt`` 0, 1, 2),
the transform prompt was told ``email`` was a source column, the model wrote
``df_transformed["email"] = df_transformed["email"].str.lower()``, and the
pandas test frame — built from *this* run's schema — raised
``KeyError: 'email'``.

Companion to ``test_mapping_freshness.py``, which pins the recency contract
(P28 R6.1–R6.4) this filter narrows rather than replaces: every assertion in
that file still holds, because a run hash that is absent or empty leaves the
filter inert.

Tests use the shared moto DynamoDB fixtures so the approved-rows
FilterExpression is enforced by a real query engine.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Pre-import the boto3 condition classes BEFORE patch.dict(sys.modules) so the
# moto Table resource's isinstance checks recognize the Key/Attr objects the
# module under test builds (same reasoning as test_mapping_freshness.py).
import boto3.dynamodb.conditions  # noqa: F401  (import order is the point)

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
    "code_validator": MagicMock(
        extract_code_only=MagicMock(return_value="# code"),
        validate_generated_code=MagicMock(return_value={"is_valid": True, "errors": [], "summary": {}}),
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(load_prompt_template=MagicMock(return_value="mock prompt")),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "artifact_schema": MagicMock(create_output_data=MagicMock(return_value={})),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    sys.modules.pop("mapping_resolution", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


PIPELINE_ID = "pipe-compat"

# ── The live incident's real values (see module docstring) ──────────────
INCIDENT_PIPELINE_ID = "latesttest-50e07536"
INCIDENT_RUN_ID = "run-2026-08-15-19-15-27-e99dbcbe"

# The run's own schema_extract hash, and the mapping built against it.
RUN_SCHEMA_HASH = "c76edff25e69575d9739faf1f5e64fbba093711c100076d9bfd4ca98050a0fe4"
MATCHING_MAPPING_ID = "a5a00ebac4a903530e6a785af8295005"
MATCHING_CREATED = "2026-08-09T15:30:47.313475+00:00"
MATCHING_SOURCE_FIELDS = [
    "customer_id",
    "first_name",
    "last_name",
    "email_address",
    "phone_number",
    "signup_date",
    "account_status",
    "country",
]

# The strictly-newer mapping built against a DIFFERENT source schema.
OTHER_SCHEMA_HASH = "d9d3b7e1001dbfbb0ab0259f68fe36b62338458753807dd2f8e6fd296884f94f"
MISMATCHED_MAPPING_ID = "96ca07130bea2308215a46b321c5ee63"
MISMATCHED_CREATED = "2026-08-15T19:08:20.979489+00:00"
MISMATCHED_SOURCE_FIELDS = [
    "customer_id",
    "first_name",
    "last_name",
    "email",
    "phone",
    "registration_date",
    "status",
]

_SKIP_LOG = "Skipping approved mapping built against a different source schema"
_SUBSTITUTION_LOG = "Substituting newer approved mapping for state-carried mapping"


def _seed_state_row(state_table, *, run_id, mapping_id, ts, status="success", pipeline_id=PIPELINE_ID):
    """An approved schema_mapping state row visible in the PipelineIndex GSI."""
    state_table.put_item(
        Item={
            "run_id": run_id,
            "step_name": "schema_mapping",
            "pipeline_id": pipeline_id,
            "timestamp": ts,
            "status": status,
            "mapping_id": mapping_id,
        }
    )


_ABSENT = object()


def _seed_mapping(mappings_table, mapping_id, created_timestamp, source_schema_hash=_ABSENT, **extra):
    """A mappings-table record shaped like ``store_mapping_separately`` output.

    ``source_schema_hash`` defaults to a per-mapping sentinel so a test that does
    not care carries *some* hash (as the writer always does); pass ``None`` to
    omit the attribute entirely, which is the anomaly constraint 3 rules on.
    """
    item: dict = {
        "mapping_id": mapping_id,
        "mapping_result": "{}",
        "target_schema_hash": f"tgt-hash-{mapping_id}",
        **extra,
    }
    if created_timestamp is not None:
        item["created_timestamp"] = created_timestamp
    if source_schema_hash is _ABSENT:
        item["source_schema_hash"] = f"src-hash-{mapping_id}"
    elif source_schema_hash is not None:
        item["source_schema_hash"] = source_schema_hash
    mappings_table.put_item(Item=item)


@pytest.fixture
def tables(mock_dynamodb_tables):
    state = mock_dynamodb_tables["agentic_data_ingestion_state"]
    mappings = mock_dynamodb_tables["agentic_data_ingestion_schema_mappings"]
    with (
        patch.object(_mod, "pipeline_runs_table", state),
        patch.object(_mod, "mappings_table", mappings),
    ):
        yield state, mappings


def _logged(mock_logger, message):
    """The ``extra`` dicts of every ``logger.info`` call carrying ``message``."""
    return [c.kwargs["extra"] for c in mock_logger.info.call_args_list if c.args and c.args[0] == message]


# ── The named regression: the live run, with its real values ────────────


def test_run_2026_08_15_19_15_27_e99dbcbe_prefers_the_schema_matching_mapping(tables):
    """Reproduces pipeline ``latesttest-50e07536`` run
    ``run-2026-08-15-19-15-27-e99dbcbe`` with the values read live from account
    777788889999 / us-west-2.

    Two approved mappings, the newer one (``96ca0713…``) built against
    ``d9d3b7e1…`` and naming ``email``/``phone``/``registration_date``/
    ``status`` as source columns, the older one (``a5a00eba…``) built against
    the ``c76edff2…`` schema the run actually extracted and naming
    ``email_address``. Selection must return the older, schema-matching
    mapping: had it done so on 2026-08-15 the transform would have read
    ``df["email_address"]`` and the run would have succeeded instead of raising
    ``KeyError: 'email'``.
    """
    state, mappings = tables
    _seed_state_row(
        state,
        run_id="run-2026-08-09-15-30-27-cb31d2d3",
        mapping_id=MATCHING_MAPPING_ID,
        ts="2026-08-09T15:30:27",
        pipeline_id=INCIDENT_PIPELINE_ID,
    )
    _seed_state_row(
        state,
        run_id="run-2026-08-15-19-07-50-18238eb1",
        mapping_id=MISMATCHED_MAPPING_ID,
        ts="2026-08-15T19:07:50",
        pipeline_id=INCIDENT_PIPELINE_ID,
    )
    _seed_mapping(
        mappings,
        MATCHING_MAPPING_ID,
        MATCHING_CREATED,
        RUN_SCHEMA_HASH,
        source_schema_fields=MATCHING_SOURCE_FIELDS,
    )
    _seed_mapping(
        mappings,
        MISMATCHED_MAPPING_ID,
        MISMATCHED_CREATED,
        OTHER_SCHEMA_HASH,
        source_schema_fields=MISMATCHED_SOURCE_FIELDS,
    )

    # The run's state-carried mapping is the schema-matching one.
    current_resp = mappings.get_item(Key={"mapping_id": MATCHING_MAPPING_ID})
    selected_id, selected_resp = _mod._select_freshest_mapping(
        INCIDENT_PIPELINE_ID,
        MATCHING_MAPPING_ID,
        current_resp,
        RUN_SCHEMA_HASH,
    )

    assert selected_id == MATCHING_MAPPING_ID
    assert selected_resp["Item"]["source_schema_hash"] == RUN_SCHEMA_HASH
    assert "email_address" in selected_resp["Item"]["source_schema_fields"]


def test_run_2026_08_15_19_15_27_e99dbcbe_without_the_guard_the_mismatch_would_win(tables):
    """The same two records with NO run hash: the newer, schema-incompatible
    mapping is substituted. This is the pre-change behaviour, pinned so the
    regression above is demonstrably the guard's doing and not an artifact of
    the fixture ordering.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-old", mapping_id=MATCHING_MAPPING_ID, ts="2026-08-09T15:30:27")
    _seed_state_row(state, run_id="run-new", mapping_id=MISMATCHED_MAPPING_ID, ts="2026-08-15T19:07:50")
    _seed_mapping(mappings, MATCHING_MAPPING_ID, MATCHING_CREATED, RUN_SCHEMA_HASH)
    _seed_mapping(mappings, MISMATCHED_MAPPING_ID, MISMATCHED_CREATED, OTHER_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": MATCHING_MAPPING_ID})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, MATCHING_MAPPING_ID, current_resp, "")

    assert selected_id == MISMATCHED_MAPPING_ID


# ── Newer AND matching still substitutes (the filter narrows, not replaces) ──


def test_newer_and_matching_candidate_is_still_substituted(tables):
    """A strictly-newer approved mapping built against the SAME schema the run
    extracted is substituted exactly as before, and logged with both ids.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-rerun", mapping_id="map-rerun", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-rerun", "2026-01-02T00:00:00", RUN_SCHEMA_HASH)
    _seed_mapping(mappings, "map-stale", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-stale"})
    mock_logger = MagicMock()
    with patch.object(_mod.mapping_resolution, "logger", mock_logger):
        selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-stale", current_resp, RUN_SCHEMA_HASH)

    assert selected_id == "map-rerun"
    substitutions = _logged(mock_logger, _SUBSTITUTION_LOG)
    assert len(substitutions) == 1
    assert substitutions[0]["state_mapping_id"] == "map-stale"
    assert substitutions[0]["selected_mapping_id"] == "map-rerun"
    assert _logged(mock_logger, _SKIP_LOG) == []


# ── Newer AND mismatching is skipped, with both ids and both hashes ─────


def test_newer_mismatching_candidate_is_skipped_and_logged_with_both_hashes(tables):
    """The skip names both mapping ids and both hashes, so a run that keeps its
    incumbent says why in one log line.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-other", mapping_id="map-other-schema", ts="2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-other-schema", "2026-03-01T00:00:00", OTHER_SCHEMA_HASH)
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    mock_logger = MagicMock()
    with patch.object(_mod.mapping_resolution, "logger", mock_logger):
        selected_id, selected_resp = _mod._select_freshest_mapping(
            PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH
        )

    assert selected_id == "map-current"
    assert selected_resp is current_resp

    skips = _logged(mock_logger, _SKIP_LOG)
    assert len(skips) == 1
    assert skips[0]["state_mapping_id"] == "map-current"
    assert skips[0]["skipped_mapping_id"] == "map-other-schema"
    assert skips[0]["run_schema_hash"] == RUN_SCHEMA_HASH
    assert skips[0]["skipped_mapping_source_schema_hash"] == OTHER_SCHEMA_HASH
    assert _logged(mock_logger, _SUBSTITUTION_LOG) == []


def test_only_an_older_matching_candidate_survives_several_newer_mismatches(tables):
    """Three candidates newer than the incumbent, two built against other
    schemas: the one matching the run's schema wins even though it is the
    oldest of the three.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-a", mapping_id="map-wrong-newest", ts="2026-01-05T00:00:00")
    _seed_state_row(state, run_id="run-b", mapping_id="map-wrong-mid", ts="2026-01-04T00:00:00")
    _seed_state_row(state, run_id="run-c", mapping_id="map-right-oldest", ts="2026-01-03T00:00:00")

    _seed_mapping(mappings, "map-wrong-newest", "2026-06-01T00:00:00", OTHER_SCHEMA_HASH)
    _seed_mapping(mappings, "map-wrong-mid", "2026-05-01T00:00:00", "some-third-hash")
    _seed_mapping(mappings, "map-right-oldest", "2026-04-01T00:00:00", RUN_SCHEMA_HASH)
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH)

    assert selected_id == "map-right-oldest"


def test_no_candidate_matches_keeps_the_incumbent(tables):
    """Every approved candidate was built against a different schema: the
    state-carried mapping is returned untouched, same object.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-a", mapping_id="map-wrong-1", ts="2026-01-03T00:00:00")
    _seed_state_row(state, run_id="run-b", mapping_id="map-wrong-2", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-wrong-1", "2026-06-01T00:00:00", OTHER_SCHEMA_HASH)
    _seed_mapping(mappings, "map-wrong-2", "2026-05-01T00:00:00", "another-hash")
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, selected_resp = _mod._select_freshest_mapping(
        PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH
    )

    assert selected_id == "map-current"
    assert selected_resp is current_resp


# ── Constraint 2: a missing run hash must NOT start refusing ────────────


@pytest.mark.parametrize("absent_hash", ["", None])
def test_absent_run_schema_hash_leaves_selection_exactly_as_before(tables, absent_hash):
    """With no run hash the comparison is impossible, so the filter is inert and
    recency alone decides — including for a candidate the filter would have
    rejected. Turning an unavailable hash into a refusal is the most likely way
    to make this guard an outage.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-other", mapping_id="map-other-schema", ts="2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-other-schema", "2026-03-01T00:00:00", OTHER_SCHEMA_HASH)
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    mock_logger = MagicMock()
    with patch.object(_mod.mapping_resolution, "logger", mock_logger):
        if absent_hash is None:
            # The parameter's own default — the shape every existing caller and
            # every test in test_mapping_freshness.py uses.
            selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)
        else:
            selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp, absent_hash)

    assert selected_id == "map-other-schema"
    assert _logged(mock_logger, _SKIP_LOG) == []


# ── Constraint 3: a candidate with no source_schema_hash ────────────────


def test_candidate_with_absent_source_schema_hash_is_skipped(tables):
    """Conservative direction, decided: an absent ``source_schema_hash`` does
    not match, so the candidate is skipped rather than silently substituted.

    ``store_mapping_separately`` writes the attribute on every put, so an
    absent hash is an anomaly rather than a supported state — the stance
    ``LGR-b2f79ef-11`` (template / ratified) already took by making an absent
    hash a ``StepFailure`` instead of a fabricated UUID. Such a candidate could
    not be used even if selected: the handler's schema-identity read fails
    naming the attribute. Skipping it lets a compatible candidate win.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-nohash", mapping_id="map-no-hash-attr", ts="2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-no-hash-attr", "2026-06-01T00:00:00", None)
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    mock_logger = MagicMock()
    with patch.object(_mod.mapping_resolution, "logger", mock_logger):
        selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH)

    assert selected_id == "map-current"
    skips = _logged(mock_logger, _SKIP_LOG)
    assert len(skips) == 1
    assert skips[0]["skipped_mapping_id"] == "map-no-hash-attr"
    # The absent attribute is reported as empty, not invented.
    assert skips[0]["skipped_mapping_source_schema_hash"] == ""


def test_candidate_with_absent_source_schema_hash_still_wins_when_run_hash_is_absent(tables):
    """The other direction of constraint 3. The skip is a consequence of the
    filter, not a new rule about hash-less records: with no run hash to compare
    against, a hash-less candidate is selected on recency exactly as it was
    before this change. So the conservative choice narrows nothing for a
    pipeline whose ``schema_extract`` row carries no hash.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-nohash", mapping_id="map-no-hash-attr", ts="2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-no-hash-attr", "2026-06-01T00:00:00", None)
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp, "")

    assert selected_id == "map-no-hash-attr"


def test_literal_no_hash_sentinel_is_skipped_without_a_special_case(tables):
    """``store_mapping_separately`` substitutes the literal ``"no-hash"`` when
    its own value is falsy. It never equals a real hash, so plain inequality
    already refuses it — no separate branch, and none should be added.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-sentinel", mapping_id="map-sentinel", ts="2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-sentinel", "2026-06-01T00:00:00", "no-hash")
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH)

    assert selected_id == "map-current"


# ── Constraint 5: the dangling-incumbent interaction ────────────────────


def test_dangling_incumbent_still_rescued_by_a_matching_candidate(tables):
    """The docstring's rescue path, preserved: a state-carried mapping_id with
    no mappings-table record (incumbent timestamp sorts as ``""``) is still
    rescued — provided the rescuer was built against this run's schema.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-r", mapping_id="map-rescue", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-rescue", "2026-01-02T00:00:00", RUN_SCHEMA_HASH)
    # 'map-ghost' has no mappings-table record.

    current_resp = mappings.get_item(Key={"mapping_id": "map-ghost"})
    selected_id, selected_resp = _mod._select_freshest_mapping(PIPELINE_ID, "map-ghost", current_resp, RUN_SCHEMA_HASH)

    assert selected_id == "map-rescue"
    assert selected_resp["Item"]["mapping_id"] == "map-rescue"


def test_dangling_incumbent_with_only_mismatching_candidates_returns_the_dangling_id(tables):
    """The accepted behaviour change, pinned so it is a decision and not a
    surprise.

    Once candidates are hash-filtered a dangling incumbent may have no eligible
    rescuer left, and the function returns the dangling id — whereupon the
    handler raises a ``StepFailure`` naming the unresolvable mapping. That is
    the right trade: rescuing with a schema-incompatible mapping is precisely
    the failure this filter exists to prevent, and it would surface three steps
    later as a ``KeyError`` on a column name rather than here as a named
    lookup failure.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-r", mapping_id="map-rescue-wrong-schema", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-rescue-wrong-schema", "2026-01-02T00:00:00", OTHER_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-ghost"})
    selected_id, selected_resp = _mod._select_freshest_mapping(PIPELINE_ID, "map-ghost", current_resp, RUN_SCHEMA_HASH)

    assert selected_id == "map-ghost"
    assert "Item" not in selected_resp


# ── Pre-existing contracts hold under the filter ────────────────────────


def test_tiebreak_on_mapping_id_still_deterministic_among_matching_candidates(tables):
    """Two eligible candidates sharing a created_timestamp still tiebreak on
    mapping_id (lexicographically greatest), repeatably.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-a", mapping_id="map-tie-a", ts="2026-01-02T00:00:00")
    _seed_state_row(state, run_id="run-b", mapping_id="map-tie-b", ts="2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-tie-a", "2026-02-01T00:00:00", RUN_SCHEMA_HASH)
    _seed_mapping(mappings, "map-tie-b", "2026-02-01T00:00:00", RUN_SCHEMA_HASH)
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    winners = {
        _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH)[0] for _ in range(3)
    }

    assert winners == {"map-tie-b"}


def test_tiebreak_ignores_a_mismatching_candidate_that_would_have_won_it(tables):
    """``map-tie-z`` shares the winning timestamp and sorts above both eligible
    candidates, but was built against another schema — so the tiebreak runs over
    the eligible set only.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-a", mapping_id="map-tie-a", ts="2026-01-02T00:00:00")
    _seed_state_row(state, run_id="run-b", mapping_id="map-tie-b", ts="2026-01-03T00:00:00")
    _seed_state_row(state, run_id="run-z", mapping_id="map-tie-z", ts="2026-01-04T00:00:00")
    _seed_mapping(mappings, "map-tie-a", "2026-02-01T00:00:00", RUN_SCHEMA_HASH)
    _seed_mapping(mappings, "map-tie-b", "2026-02-01T00:00:00", RUN_SCHEMA_HASH)
    _seed_mapping(mappings, "map-tie-z", "2026-02-01T00:00:00", OTHER_SCHEMA_HASH)
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH)

    assert selected_id == "map-tie-b"


def test_unapproved_filter_still_applies_alongside_the_hash_filter(tables):
    """The two filters compose: a candidate must be BOTH approved and built
    against this run's schema.
    """
    state, mappings = tables
    _seed_state_row(
        state, run_id="run-pend", mapping_id="map-pending-match", status="waiting_approval", ts="2026-01-04T00:00:00"
    )
    _seed_state_row(state, run_id="run-wrong", mapping_id="map-approved-mismatch", ts="2026-01-03T00:00:00")
    _seed_state_row(state, run_id="run-ok", mapping_id="map-approved-match", ts="2026-01-02T00:00:00")

    _seed_mapping(mappings, "map-pending-match", "2026-07-01T00:00:00", RUN_SCHEMA_HASH)  # newest, unapproved
    _seed_mapping(mappings, "map-approved-mismatch", "2026-06-01T00:00:00", OTHER_SCHEMA_HASH)
    _seed_mapping(mappings, "map-approved-match", "2026-05-01T00:00:00", RUN_SCHEMA_HASH)
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH)

    assert selected_id == "map-approved-match"


def test_lookup_error_keeps_the_incumbent_even_with_a_run_hash():
    """Best-effort is not weakened: the filter runs inside the same try block, so
    a query failure still keeps the state-carried mapping instead of raising. A
    raise here would degrade codegen for a lookup problem.
    """
    boom_table = MagicMock()
    boom_table.query.side_effect = RuntimeError("GSI unavailable")
    current_resp = {"Item": {"mapping_id": "map-current", "created_timestamp": "2026-01-01T00:00:00"}}

    with patch.object(_mod, "pipeline_runs_table", boom_table):
        selected_id, selected_resp = _mod._select_freshest_mapping(
            PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH
        )

    assert selected_id == "map-current"
    assert selected_resp is current_resp


def test_mappings_table_read_error_during_the_filter_keeps_the_incumbent(tables):
    """A candidate ``get_item`` that raises is caught by the same best-effort
    clause — the filter adds no new escaping exception.
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-a", mapping_id="map-cand", ts="2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    boom_mappings = MagicMock()
    boom_mappings.get_item.side_effect = RuntimeError("mappings table unavailable")
    current_resp = {"Item": {"mapping_id": "map-current", "created_timestamp": "2026-01-01T00:00:00"}}

    with patch.object(_mod, "mappings_table", boom_mappings):
        selected_id, selected_resp = _mod._select_freshest_mapping(
            PIPELINE_ID, "map-current", current_resp, RUN_SCHEMA_HASH
        )

    assert selected_id == "map-current"
    assert selected_resp is current_resp


# ── Handler level: the hash reaches the filter from the schema_extract row ──


def test_handler_threads_the_run_schema_hash_from_the_schema_extract_row(tables):
    """End to end through ``lambda_handler``: the run's ``schema_extract`` row
    carries ``schema_hash``, a strictly-newer approved mapping was built against
    a different schema, and codegen runs on the state-carried mapping.

    This is the wiring the live failure lacked — the hash was on the row all
    along and the selection never received it.
    """
    state, mappings = tables
    run_id = "run-compat-e2e"
    state.put_item(
        Item={
            "run_id": run_id,
            "step_name": "schema_extract",
            "extracted_schema": '{"type": "object", "properties": {"email_address": {"type": "string"}}}',
            "schema_hash": RUN_SCHEMA_HASH,
        }
    )
    _seed_state_row(state, run_id="run-prev", mapping_id="map-newer-other-schema", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-newer-other-schema", "2026-06-01T00:00:00", OTHER_SCHEMA_HASH)
    _seed_mapping(mappings, "map-state", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    event = {"run_id": run_id, "pipeline_id": PIPELINE_ID, "mapping_id": "map-state"}
    captured: dict = {}

    def _capture_generate_code(pm, mapping_item, regeneration_feedback=None, **kwargs):
        captured["mapping_item"] = mapping_item
        return "# code\ndf = spark.read.json('s3://b/k')\n"

    with (
        patch.object(
            _mod,
            "get_pipeline_metadata",
            return_value={
                "target_config": {
                    "type": "s3_tables",
                    "bucket_name": "b",
                    "namespace_name": "n",
                    "table_name": "t",
                }
            },
        ),
        patch.object(_mod, "publish_pipeline_state_change"),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(_mod, "generate_code", side_effect=_capture_generate_code),
        patch.object(_mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
    ):
        result = _mod.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert captured["mapping_item"]["mapping_id"] == "map-state"
    assert captured["mapping_item"]["source_schema_hash"] == RUN_SCHEMA_HASH


def test_handler_without_a_schema_hash_on_the_row_still_substitutes_on_recency(tables):
    """A ``schema_extract`` row with no ``schema_hash`` (the shape
    ``test_mapping_freshness.py``'s end-to-end test seeds) leaves the filter
    inert at handler level too — constraint 2 verified through the real wiring,
    not just the unit seam.
    """
    state, mappings = tables
    run_id = "run-compat-nohash"
    state.put_item(
        Item={
            "run_id": run_id,
            "step_name": "schema_extract",
            "extracted_schema": '{"properties": {}}',
        }
    )
    _seed_state_row(state, run_id="run-prev", mapping_id="map-newer-other-schema", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-newer-other-schema", "2026-06-01T00:00:00", OTHER_SCHEMA_HASH)
    _seed_mapping(mappings, "map-state", "2026-01-01T00:00:00", RUN_SCHEMA_HASH)

    event = {"run_id": run_id, "pipeline_id": PIPELINE_ID, "mapping_id": "map-state"}
    captured: dict = {}

    def _capture_generate_code(pm, mapping_item, regeneration_feedback=None, **kwargs):
        captured["mapping_item"] = mapping_item
        return "# code\ndf = spark.read.json('s3://b/k')\n"

    with (
        patch.object(
            _mod,
            "get_pipeline_metadata",
            return_value={
                "target_config": {
                    "type": "s3_tables",
                    "bucket_name": "b",
                    "namespace_name": "n",
                    "table_name": "t",
                }
            },
        ),
        patch.object(_mod, "publish_pipeline_state_change"),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(_mod, "generate_code", side_effect=_capture_generate_code),
        patch.object(_mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
    ):
        result = _mod.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert captured["mapping_item"]["mapping_id"] == "map-newer-other-schema"
