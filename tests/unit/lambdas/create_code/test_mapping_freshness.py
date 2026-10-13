# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_code mapping-freshness lookup (P28 R6).

The "newer mapping" lookup previously (a) compared ``created_at``/``timestamp``
attributes that mapping records never carry (the mappings table writes
``created_timestamp``), (b) broke unconditionally after the FIRST GSI item,
and (c) never checked approval status. The rewritten lookup
(``_select_freshest_mapping``):

- reads ``created_timestamp`` — the attribute that actually exists (R6.1)
- evaluates ALL approved schema_mapping GSI rows, paginated, selecting the
  newest deterministically (created_timestamp desc, mapping_id tiebreak) (R6.2)
- substitutes only a strictly-newer APPROVED mapping and logs the
  substitution with both ids (R6.3)

"Approved" here means the state-table schema_mapping row has
``status == "success"`` — the mappings-table records carry no status
attribute; the approvals flow flips waiting_approval → success (approved)
or rejected on the state row, and rows for mappings that never required
approval are written as success directly.

Tests use the shared moto DynamoDB fixtures so FilterExpression semantics
(the approved filter, step_name filter) are enforced by a real query engine,
not mocked away.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4**
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Pre-import boto3 condition classes BEFORE the patch.dict(sys.modules) block
# below: patch.dict evicts modules first imported inside it, and a re-imported
# boto3.dynamodb.conditions would carry NEW Key/Attr classes that the moto
# Table resource's condition-translation isinstance checks don't recognize
# (queries would then fail parameter validation with raw condition objects).
import boto3.dynamodb.conditions  # noqa: F401  (import order is the point)

# Add Lambda dir and shared dir to path
_create_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy dependencies
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
    "prompt_template_loader": MagicMock(
        load_prompt_template=MagicMock(return_value="mock prompt"),
    ),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "artifact_schema": MagicMock(
        create_output_data=MagicMock(return_value={}),
    ),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    sys.modules.pop("mapping_resolution", None)
    import lambda_function as _mod

    # The extracted module lambda_function delegates to (P28 task 15.2) —
    # imported inside the block so we get the SAME module object the
    # wrappers reference (needed to patch its logger below).
    import mapping_resolution as _mr


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


PIPELINE_ID = "pipe-fresh"


def _seed_state_row(state_table, *, run_id, mapping_id, status="success", ts, step_name="schema_mapping"):
    """Seed a state-table row that appears in the PipelineIndex GSI (needs `timestamp`)."""
    state_table.put_item(
        Item={
            "run_id": run_id,
            "step_name": step_name,
            "pipeline_id": PIPELINE_ID,
            "timestamp": ts,
            "status": status,
            "mapping_id": mapping_id,
        }
    )


def _seed_mapping(mappings_table, mapping_id, created_timestamp=None, **extra):
    """Seed a mappings-table record shaped like store_mapping_separately output.

    p33 task 4.6 harness amendment, NOT an assertion change. The two schema
    hashes are added here rather than at the call sites because this helper
    claims to mirror ``store_mapping_separately``, and that writer sets both on
    every put — substituting the literal ``"no-hash"`` when its own value is
    falsy (``schema_mapping/lambda_function.py:1166-1169``). The fixture was
    under-specified relative to the writer it mirrors, which only surfaced once
    requirement 2.9 made an absent hash a StepFailure instead of a fresh
    ``str(uuid.uuid4())``. Only the one handler-level test in this file
    (``test_handler_uses_newer_approved_mapping_end_to_end``) reaches that check;
    the ``_select_freshest_mapping`` tests select on ``created_timestamp`` and are
    indifferent to these attributes.
    """
    item = {
        "mapping_id": mapping_id,
        "mapping_result": "{}",
        "source_schema_hash": f"src-hash-{mapping_id}",
        "target_schema_hash": f"tgt-hash-{mapping_id}",
        **extra,
    }
    if created_timestamp is not None:
        item["created_timestamp"] = created_timestamp
    mappings_table.put_item(Item=item)


@pytest.fixture
def tables(mock_dynamodb_tables):
    """Patch the module's table globals to the moto tables."""
    state = mock_dynamodb_tables["agentic_data_ingestion_state"]
    mappings = mock_dynamodb_tables["agentic_data_ingestion_schema_mappings"]
    with (
        patch.object(_mod, "pipeline_runs_table", state),
        patch.object(_mod, "mappings_table", mappings),
    ):
        yield state, mappings


# ── R6.1 + R6.2: newest-approved selection across multiple GSI items ────


def test_newest_approved_selected_across_multiple_gsi_items(tables):
    """With 3+ approved schema_mapping rows carrying mixed created_timestamps,
    the NEWEST mapping wins — every GSI item is evaluated, not just the first.

    **Validates: Requirements 6.1, 6.2, 6.4**
    """
    state, mappings = tables
    # GSI returns newest-timestamp state row first (ScanIndexForward=False);
    # make the newest STATE row point at a mapping that is NOT the newest by
    # created_timestamp, so a first-item-only implementation picks wrongly.
    _seed_state_row(state, run_id="run-a", mapping_id="map-old", ts="2026-01-03T00:00:00")
    _seed_state_row(state, run_id="run-b", mapping_id="map-newest", ts="2026-01-02T00:00:00")
    _seed_state_row(state, run_id="run-c", mapping_id="map-mid", ts="2026-01-01T00:00:00")

    _seed_mapping(mappings, "map-old", "2026-01-01T00:00:00")
    _seed_mapping(mappings, "map-mid", "2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-newest", "2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-current", "2025-12-31T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, selected_resp = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    assert selected_id == "map-newest"
    assert selected_resp["Item"]["mapping_id"] == "map-newest"


def test_all_gsi_items_evaluated_when_first_candidate_is_oldest(tables):
    """The old code broke after the first GSI item; prove rows beyond the
    first are considered even when the first candidate resolves fine.

    **Validates: Requirements 6.2, 6.4**
    """
    state, mappings = tables
    # First GSI row (newest state timestamp) resolves to an OLDER mapping than
    # the current one — the old code would stop there and keep the current.
    _seed_state_row(state, run_id="run-1", mapping_id="map-older-than-current", ts="2026-02-09T00:00:00")
    _seed_state_row(state, run_id="run-2", mapping_id="map-newer-than-current", ts="2026-02-01T00:00:00")

    _seed_mapping(mappings, "map-older-than-current", "2026-01-01T00:00:00")
    _seed_mapping(mappings, "map-newer-than-current", "2026-03-01T00:00:00")
    _seed_mapping(mappings, "map-current", "2026-02-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    assert selected_id == "map-newer-than-current"


def test_schema_mapping_rows_beyond_newest_state_rows_are_found(tables):
    """Regression for the Limit-before-filter bug: DynamoDB applies Limit
    BEFORE FilterExpression, so the old Limit=5 could see zero schema_mapping
    rows when the 5 newest state rows were other steps. Bury the
    schema_mapping rows under many newer non-mapping step rows and verify the
    newer approved mapping is still found.

    **Validates: Requirements 6.2, 6.4**
    """
    state, mappings = tables
    # 10 non-schema_mapping rows with the newest timestamps
    for i in range(10):
        _seed_state_row(
            state,
            run_id=f"run-noise-{i}",
            mapping_id="unused",
            ts=f"2026-06-{10 + i:02d}T00:00:00",
            step_name="file_validation",
        )
    # The only schema_mapping row is older than all the noise
    _seed_state_row(state, run_id="run-m", mapping_id="map-new", ts="2026-01-01T00:00:00")

    _seed_mapping(mappings, "map-new", "2026-05-01T00:00:00")
    _seed_mapping(mappings, "map-current", "2026-04-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    assert selected_id == "map-new"


# ── R6.3: stale-state substitution + logging with both ids ──────────────


def test_stale_state_substitution_logs_both_ids(tables):
    """When state carries an older mapping_id and a newer approved mapping
    exists, the newer one is used and the substitution is logged with BOTH ids.

    **Validates: Requirements 6.1, 6.3, 6.4**
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-rerun", mapping_id="map-rerun", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-rerun", "2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-stale", "2026-01-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-stale"})
    mock_logger = MagicMock()
    # The substitution log is emitted by the extracted mapping_resolution
    # module (P28 R19.1), so patch its logger via the module object that
    # lambda_function imported (same sys.modules entry).
    with patch.object(_mod.mapping_resolution, "logger", mock_logger):
        selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-stale", current_resp)

    assert selected_id == "map-rerun"

    substitution_logs = [call for call in mock_logger.info.call_args_list if call.kwargs.get("extra")]
    assert len(substitution_logs) == 1
    extra = substitution_logs[0].kwargs["extra"]
    assert extra["state_mapping_id"] == "map-stale"
    assert extra["selected_mapping_id"] == "map-rerun"


def test_no_newer_mapping_keeps_state_id(tables):
    """When the state-carried mapping is already the newest approved one,
    it is kept — no substitution, no substitution log.

    **Validates: Requirements 6.3, 6.4**
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-cur", mapping_id="map-current", ts="2026-01-02T00:00:00")
    _seed_state_row(state, run_id="run-old", mapping_id="map-older", ts="2026-01-01T00:00:00")

    _seed_mapping(mappings, "map-current", "2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-older", "2026-01-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    mock_logger = MagicMock()
    with patch.object(_mod.mapping_resolution, "logger", mock_logger):
        selected_id, selected_resp = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    assert selected_id == "map-current"
    assert selected_resp is current_resp
    substitution_logs = [call for call in mock_logger.info.call_args_list if call.kwargs.get("extra")]
    assert substitution_logs == []


def test_no_candidates_at_all_keeps_state_id(tables):
    """An empty GSI result (no schema_mapping rows for the pipeline) keeps
    the state-carried mapping untouched.

    **Validates: Requirements 6.3, 6.4**
    """
    _state, mappings = tables
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, selected_resp = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    assert selected_id == "map-current"
    assert selected_resp is current_resp


# ── Approved filter: unapproved newer mappings are never selected ───────


@pytest.mark.parametrize("unapproved_status", ["waiting_approval", "rejected"])
def test_unapproved_newer_mapping_not_selected(tables, unapproved_status):
    """A NEWER mapping whose state row is waiting_approval or rejected is
    never substituted — only approved (status=success) rows qualify. The
    filter runs server-side in the query, enforced here by moto.

    **Validates: Requirements 6.2, 6.3, 6.4**
    """
    state, mappings = tables
    _seed_state_row(
        state, run_id="run-pend", mapping_id="map-pending", status=unapproved_status, ts="2026-01-03T00:00:00"
    )
    _seed_state_row(state, run_id="run-appr", mapping_id="map-approved", ts="2026-01-02T00:00:00")

    _seed_mapping(mappings, "map-pending", "2026-01-05T00:00:00")  # newest but unapproved
    _seed_mapping(mappings, "map-approved", "2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    # The unapproved newest is skipped; the newest APPROVED one wins.
    assert selected_id == "map-approved"


# ── R6.1: created_timestamp is the attribute compared ───────────────────


def test_legacy_attrs_created_at_and_timestamp_are_ignored(tables):
    """A candidate record carrying only the legacy ``created_at``/``timestamp``
    attributes (which store_mapping_separately never writes) has no
    created_timestamp — it sorts as oldest and can never displace a
    timestamped current mapping. This pins the comparison to the attribute
    that actually exists.

    **Validates: Requirements 6.1, 6.4**
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-legacy", mapping_id="map-legacy", ts="2026-01-03T00:00:00")
    # Legacy-shaped record: created_at/timestamp present, created_timestamp absent
    _seed_mapping(mappings, "map-legacy", None, created_at="2099-01-01T00:00:00", timestamp="2099-01-01T00:00:00")
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    assert selected_id == "map-current"


# ── Missing created_timestamp handled deterministically ─────────────────


def test_missing_created_timestamp_candidates_never_beat_timestamped_current(tables):
    """Candidates with no created_timestamp sort as empty string (oldest) and
    never win over a timestamped current mapping — repeatably.

    **Validates: Requirements 6.1, 6.4**
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-x", mapping_id="map-nots-1", ts="2026-01-02T00:00:00")
    _seed_state_row(state, run_id="run-y", mapping_id="map-nots-2", ts="2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-nots-1", None)
    _seed_mapping(mappings, "map-nots-2", None)
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    for _ in range(3):  # deterministic across repeated invocations
        selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)
        assert selected_id == "map-current"


def test_equal_timestamps_tiebreak_on_mapping_id_deterministically(tables):
    """Two candidates sharing a created_timestamp newer than the current one:
    selection tiebreaks on mapping_id (lexicographically greatest) so repeated
    runs pick the same winner.

    **Validates: Requirements 6.2, 6.4**
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-a", mapping_id="map-tie-a", ts="2026-01-02T00:00:00")
    _seed_state_row(state, run_id="run-b", mapping_id="map-tie-b", ts="2026-01-03T00:00:00")
    _seed_mapping(mappings, "map-tie-a", "2026-02-01T00:00:00")
    _seed_mapping(mappings, "map-tie-b", "2026-02-01T00:00:00")
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    winners = {_mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)[0] for _ in range(3)}

    assert winners == {"map-tie-b"}  # deterministic: greatest mapping_id


def test_candidate_equal_to_current_timestamp_not_substituted(tables):
    """Substitution requires STRICTLY newer — a candidate with the same
    created_timestamp as the current mapping is not substituted.

    **Validates: Requirements 6.3, 6.4**
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-e", mapping_id="map-equal", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-equal", "2026-01-01T00:00:00")
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    assert selected_id == "map-current"


def test_dangling_state_mapping_rescued_by_timestamped_approved_candidate(tables):
    """A state-carried mapping_id with NO mappings-table record (current
    timestamp sorts as empty) is rescued by any timestamped approved
    candidate — preserving the recovery path that lets codegen proceed
    instead of failing on the dangling id.

    **Validates: Requirements 6.1, 6.3, 6.4**
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-r", mapping_id="map-rescue", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-rescue", "2026-01-02T00:00:00")
    # 'map-ghost' has no mappings-table record

    current_resp = mappings.get_item(Key={"mapping_id": "map-ghost"})  # no Item
    selected_id, selected_resp = _mod._select_freshest_mapping(PIPELINE_ID, "map-ghost", current_resp)

    assert selected_id == "map-rescue"
    assert selected_resp["Item"]["mapping_id"] == "map-rescue"


def test_dangling_candidate_records_are_skipped(tables):
    """A state row whose mapping_id resolves to no mappings-table record is
    skipped; remaining candidates are still considered.

    **Validates: Requirements 6.2, 6.4**
    """
    state, mappings = tables
    _seed_state_row(state, run_id="run-d1", mapping_id="map-dangling", ts="2026-01-03T00:00:00")
    _seed_state_row(state, run_id="run-d2", mapping_id="map-real", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-real", "2026-02-01T00:00:00")
    _seed_mapping(mappings, "map-current", "2026-01-01T00:00:00")

    current_resp = mappings.get_item(Key={"mapping_id": "map-current"})
    selected_id, _ = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    assert selected_id == "map-real"


# ── Best-effort guarantee: lookup errors never fail codegen ──────────────


def test_lookup_error_keeps_current_mapping():
    """Any error in the freshness lookup keeps the state-carried mapping —
    the lookup must never fail the step.

    **Validates: Requirements 6.3**
    """
    boom_table = MagicMock()
    boom_table.query.side_effect = RuntimeError("GSI unavailable")
    current_resp = {"Item": {"mapping_id": "map-current", "created_timestamp": "2026-01-01T00:00:00"}}

    with patch.object(_mod, "pipeline_runs_table", boom_table):
        selected_id, selected_resp = _mod._select_freshest_mapping(PIPELINE_ID, "map-current", current_resp)

    assert selected_id == "map-current"
    assert selected_resp is current_resp


# ── Handler-level: substitution wired through lambda_handler ─────────────


def test_handler_uses_newer_approved_mapping_end_to_end(tables):
    """lambda_handler with a stale event mapping_id generates code from the
    NEWER approved mapping's record.

    **Validates: Requirements 6.3, 6.4**
    """
    state, mappings = tables
    run_id = "run-e2e"
    # schema_extract row the handler reads for the source schema
    state.put_item(
        Item={
            "run_id": run_id,
            "step_name": "schema_extract",
            "extracted_schema": '{"properties": {}}',
        }
    )
    _seed_state_row(state, run_id="run-prev", mapping_id="map-fresh", ts="2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-fresh", "2026-01-02T00:00:00")
    _seed_mapping(mappings, "map-stale", "2026-01-01T00:00:00")

    event = {"run_id": run_id, "pipeline_id": PIPELINE_ID, "mapping_id": "map-stale"}
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
    assert captured["mapping_item"]["mapping_id"] == "map-fresh"
