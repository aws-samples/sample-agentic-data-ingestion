# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 6.3 — the Non_Conforming_Column_Name forcing clause and the
Reusable_Rename_Manifest lookup that exempts it (R10.5, R10.8, R10.9).

**Functions under test, named by file because names in this repository are not
unique:** ``determine_approval_requirement`` and the new
``find_reusable_rename_manifest`` in
``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py``. The
same-named ``determine_approval_requirement`` that once lived in
``create_code/lambda_function.py`` was deleted by p34 design D2; the assertion in
``test_this_is_the_schema_mapping_module`` pins which module this file bound
anyway, because the same trap (risk R5.8) applies to ``create_approval_payload``
in the same file.

**The clause is ADDED here, not amended.** Before this task
``determine_approval_requirement`` had no Non_Conforming_Column_Name clause at
all — tasks 6.1 and 6.2 built the manifest's derivation and its approval payload
without one — so the forcing condition and its exemption arrive together. That
matters for how the exemption reads: it relaxes nothing that previously held, and
it is **not** the general claim ``LGR-5d68eeb-1`` rejected. That record refused to
inject ``approval_required: false`` on the mapping-reuse path as "a reduction in
human oversight … a decision for a human and not for an implementer", and its
``revisit_when`` names "the reuse-candidate lookup gains its own schema-identity
check" as the condition that changes the verdict. R10.8 **is** that check.

**Three conjuncts, each with a recorded failure behind it** (``LGR-5d68eeb-2``,
read in full):

* *approved for reuse* — ``mapping_reuse.approved_for_reuse_condition()``:
  ``status == "success"`` and no ``reuse_revoked_at``. Expressed as **is
  approved** and not as *is not rejected*, because an unapproved row has more than
  one resting status: a rejection sets ``rejected``, and a gate that never got a
  decision (a timeout) leaves ``waiting_approval``. A rejection at a later run
  revokes the earlier row with ``reuse_revoked_at`` instead of rewriting its
  status. That record's predecessor omitted the filter entirely, which made
  a human-*rejected* artifact reusable **and** simultaneously cost a well-behaved
  pipeline access to its last approved one.
* *Schema_Hash equality* (R10.8) — ``LGR-5d68eeb-2`` deliberately did not narrow
  by hash and says so in its own ``revisit_when``. ``LGR-a419843-1``'s live defect
  was recency-without-identity: "newest approved mapping for this pipeline"
  silently meant "newest mapping for whatever the source looked like when it was
  built".
* *pagination* — DynamoDB applies ``FilterExpression`` **after** the 1 MB page
  read, so an all-filtered page returns ``Items: []`` **with** a
  ``LastEvaluatedKey`` (``LGR-eee8ac8-3``, same GSI). Left unpaginated the busiest
  pipelines silently lose the fast path with no symptom.

**Inertness on an empty hash (R10.9) is asserted at BOTH seams**, exactly as
``LGR-a419843-1`` did it — at the unit seam on ``find_reusable_rename_manifest``
and ``determine_approval_requirement``, and through ``lambda_handler``'s wiring.
That record's rejected alternative is the reason: it refused "filtering when the
run hash is unavailable" because "a comparison that cannot be made must not
become a refusal". Here the same principle points at *requiring a human*, because
``store_mapping_separately`` writes ``source_schema_hash`` defaulting to the
**constant** ``"no-hash"`` — so an equality filter on it would answer "same
schema" for two schemas that share nothing. It is a value to be inert on, never a
missing key.

**do-NOT 14 is asserted, not merely respected**: ``test_every_other_forcing_clause
_still_fires_with_a_reusable_manifest`` drives four other clauses to ``True``
while a manifest covers the schema.

**Validates: Requirements 10.5, 10.8, 10.9**
"""

import inspect
import os
import sys
from unittest.mock import MagicMock, patch

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1); these are no-ops
# kept for parity with the sibling files in this directory.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Lambda-layer-only dependencies (the pattern in the sibling files here).
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

# sys.modules.pop + sys.path.insert: `lambda_function` is a name 23 Lambda dirs
# share, so it cross-contaminates between test files. Every patch below is
# `patch.object(_mod, …)` (`LGR-b397ee2-2`, `LGR-c58f276-4`), so nothing here
# depends on sys.modules still pointing at this module at call time.
sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _mod
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

#: A source column name that fails the Identifier_Allowlist (a space).
_NON_CONFORMING = "first name"

#: The pipeline and hash used throughout, so a copy-paste mismatch is visible.
_PIPELINE = "p55-task-6-3"
_RUN_HASH = "src-hash-6-3"


# --- Fixtures -----------------------------------------------------------------


def _high_confidence_mapping(first_name_column: str = _NON_CONFORMING) -> dict:
    """A mapping no OTHER clause forces a human on.

    Every source field mapped (so neither ratio fires), confidence well above
    ``CONFIDENCE_AUTO_APPROVE_THRESHOLD`` (0.85), no fallback provenance, and
    both target fields sourced.

    AMENDED for T-04, which added a clause requiring every entry to be
    corroborated deterministically: its ``source_field`` must be one of the
    source schema's own columns, and its names must agree after normalisation
    or be an operator mapping hint. Two consequences for this fixture, both
    fixture-side so that "no other clause forces a human" stays true:

    * ``first_name_column`` names the column the first entry reads. It defaults
      to the non-conforming ``first name`` exactly as before (which normalises
      to the same name as ``first_name``); a CONFORMING schema spells the column
      ``first_name``, and a model mapping that schema would read that column, so
      ``_first_name_column`` picks it for those calls.
    * ``cust_id → customer_id`` is a semantic match, which T-04 sends to a
      human. ``_pipeline_metadata`` therefore carries it as a mapping hint —
      the operator's corroboration — rather than renaming a column that a dozen
      assertions below name.
    """
    return {
        "mappings": [
            {"source_field": first_name_column, "target_field": "first_name", "confidence": 0.97},
            {"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.98},
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _first_name_column(source_column_names) -> str:
    """The column a model's ``first_name`` entry reads for this source schema (T-04)."""
    if source_column_names is not None and "first_name" in source_column_names:
        return "first_name"
    return _NON_CONFORMING


def _target_schema() -> dict:
    return {
        "type": "object",
        "properties": {"first_name": {"type": "string"}, "customer_id": {"type": "string"}},
        "required": ["customer_id"],
    }


def _source_schema(*column_names: str) -> dict:
    return {"type": "object", "properties": {name: {"type": "string"} for name in column_names}}


def _pipeline_metadata() -> dict:
    # `mapping_hints` AMENDED for T-04 — see `_high_confidence_mapping`.
    return {
        "pipeline_id": _PIPELINE,
        "approval_config": {"schema_mapping_enabled": True, "always_approve_schema_changes": True},
        "mapping_hints": {"cust_id": "customer_id"},
    }


def _manifest(*, hash_value: str = _RUN_HASH, manifest_id: str = "mapping-hash-abc") -> dict:
    return {
        "manifest_id": manifest_id,
        "source_schema_hash": hash_value,
        "approval_id": None,
        "renames": [{"original_name": _NON_CONFORMING, "target_name": "first_name"}],
    }


def _row(
    *,
    status: str = "success",
    approval_id: str | None = "approval-6-3",
    manifest: dict | None = None,
    row_hash: str = _RUN_HASH,
    timestamp: str = "2026-08-27T10:00:00+00:00",
) -> dict:
    """A `schema_mapping` state row shaped like `store_mapping_result` writes one."""
    item: dict = {
        "run_id": "run-old",
        "step_name": "schema_mapping",
        "status": status,
        "timestamp": timestamp,
        "mapping_id": "mapping-hash-abc",
        "source_schema_hash": row_hash,
        "pipeline_id": _PIPELINE,
        "output_data": {"approval": {"rename_manifest": _manifest() if manifest is None else manifest}},
    }
    if approval_id is not None:
        item["approval_id"] = approval_id
    return item


def _fake_table(pages: list[dict]) -> MagicMock:
    """A table whose `query` returns *pages* in order and records its kwargs."""
    table = MagicMock()
    table.query.side_effect = list(pages)
    return table


def _decide(
    *,
    source_column_names: list[str] | None = None,
    run_schema_hash: str = _RUN_HASH,
    mapping_result: dict | None = None,
    reusable: dict | None = None,
    lookup_patched: bool = True,
) -> tuple[bool, MagicMock | None]:
    """Call `determine_approval_requirement`, optionally stubbing the lookup."""
    if mapping_result is None:
        mapping_result = _high_confidence_mapping(_first_name_column(source_column_names))
    managers = [patch.object(_mod, "get_pipeline_metadata", return_value=_pipeline_metadata())]
    lookup = None
    if lookup_patched:
        lookup_manager = patch.object(_mod, "find_reusable_rename_manifest", return_value=reusable)
        managers.append(lookup_manager)

    entered = [manager.__enter__() for manager in managers]
    try:
        if lookup_patched:
            lookup = entered[1]
        decision = _mod.determine_approval_requirement(
            _PIPELINE,
            mapping_result,
            None,
            _target_schema(),
            source_column_names=source_column_names,
            run_schema_hash=run_schema_hash,
        )
    finally:
        for manager in reversed(managers):
            manager.__exit__(None, None, None)

    return decision, lookup


# --- Which module this is ------------------------------------------------------


def test_this_is_the_schema_mapping_module():
    """Risk R5.8's discriminator, asserted rather than assumed from a filename."""
    assert _mod.__file__.endswith("agentic_data_ingestion_schema_mapping/lambda_function.py"), _mod.__file__
    parameters = list(inspect.signature(_mod.determine_approval_requirement).parameters)
    # AMENDED by task 6.4, in place and with the reason at the assertion (the
    # convention `LGR-60c4985-6` / `LGR-9e76249-10` set: a deliberate change to an
    # assertion stays legible as a change and is never deleted). `reuse_provenance`
    # is 6.4's plumbing for R10.10 — the caller-owned sink the manifest-reuse branch
    # writes both identifiers into. It is additive and defaulted, exactly as this
    # task's own two parameters were, so nothing above changes meaning; the RETURN
    # type is still `bool`, which is what task 1.1's characterization pins.
    assert parameters == [
        "original_pipeline_id",
        "mapping_result",
        "schema_change_action",
        "target_schema",
        "source_column_names",
        "run_schema_hash",
        "reuse_provenance",
    ], parameters


# --- R10.5: the forcing clause and its exemption -------------------------------


def test_a_non_conforming_column_forces_a_human_when_nothing_covers_the_schema():
    """R10.5's forcing half. The mapping itself forces nothing — see the fixture."""
    baseline, _ = _decide(source_column_names=["cust_id", "first_name"])
    assert baseline is False, "the fixture already forces a human for another reason; this test is vacuous"

    decision, lookup = _decide(source_column_names=["cust_id", _NON_CONFORMING], reusable=None)

    assert decision is True
    lookup.assert_called_once_with(_PIPELINE, _RUN_HASH)


def test_an_approved_manifest_for_the_same_schema_lifts_the_clause():
    """R10.5's WHILE half: the clause fires only while no manifest covers the schema."""
    decision, lookup = _decide(
        source_column_names=["cust_id", _NON_CONFORMING],
        reusable={"manifest_id": "mapping-hash-abc", "approval_id": "approval-6-3", "renames": [{}]},
    )

    assert decision is False
    lookup.assert_called_once()


def test_the_clause_is_inert_for_a_caller_that_supplies_no_column_names():
    """The default keeps every existing caller — including task 1.1's — unchanged."""
    decision, lookup = _decide(source_column_names=None)

    assert decision is False
    lookup.assert_not_called()


def test_a_conforming_schema_never_reaches_the_lookup():
    """No non-conforming name means no query: the cheap check gates the read."""
    decision, lookup = _decide(source_column_names=["cust_id", "first_name", "total_2024"])

    assert decision is False
    lookup.assert_not_called()


def test_a_non_string_column_name_counts_as_non_conforming():
    """Fail-closed, and consistent with `derive_rename_manifest`, which raises on one.

    A non-`str` name must reach the clause as non-conforming rather than be
    silently skipped, so with nothing covering the schema it forces a human.
    """
    decision, lookup = _decide(source_column_names=[None], reusable=None)  # type: ignore[list-item]

    assert decision is True
    lookup.assert_called_once_with(_PIPELINE, _RUN_HASH)


def test_every_other_forcing_clause_still_fires_with_a_reusable_manifest():
    """do-NOT 14. The exemption lifts ONE clause and no other.

    Four independent clauses driven to `True` while a manifest covers the source
    schema: fallback provenance (P28 R3.1), a required target field with no
    source (p37 R3.3), low average confidence, and the unmapped-source ratio.
    """
    covered = {"manifest_id": "mapping-hash-abc", "approval_id": "approval-6-3", "renames": [{}]}
    columns = ["cust_id", _NON_CONFORMING]

    fallback = dict(_high_confidence_mapping(), generated_by=_mod.FALLBACK_GENERATED_BY)
    assert _decide(source_column_names=columns, reusable=covered, mapping_result=fallback)[0] is True

    unsourced_required = {
        "mappings": [{"source_field": _NON_CONFORMING, "target_field": "first_name", "confidence": 0.99}],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [{"target_field": "customer_id"}],
        "generated_by": "us.model",
    }
    assert _decide(source_column_names=columns, reusable=covered, mapping_result=unsourced_required)[0] is True

    low_confidence = dict(
        _high_confidence_mapping(),
        mappings=[{"source_field": _NON_CONFORMING, "target_field": "first_name", "confidence": 0.4}],
        unmapped_target_fields=[],
    )
    low_confidence["mappings"].append({"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.4})
    assert _decide(source_column_names=columns, reusable=covered, mapping_result=low_confidence)[0] is True

    ratio = dict(_high_confidence_mapping(), unmapped_source_fields=["a", "b", "c"])
    assert _decide(source_column_names=columns, reusable=covered, mapping_result=ratio)[0] is True


# --- R10.8: the three conjuncts ------------------------------------------------


def _conjuncts(condition) -> set[tuple[str, str, object]]:
    """Flatten a boto3 condition tree into (operator, attribute name, value)."""
    expression = condition.get_expression()
    operator = expression["operator"]
    values = expression["values"]
    if operator == "AND":
        collected: set[tuple[str, str, object]] = set()
        for value in values:
            collected |= _conjuncts(value)
        return collected
    return {(operator, getattr(values[0], "name", str(values[0])), values[1] if len(values) > 1 else None)}


def test_the_filter_carries_approved_status_hash_equality_and_the_step():
    """The conjuncts, read off the query the lookup actually issued.

    `status == "success"` is asserted as an equality on `success` rather than as
    an inequality on `rejected`, because that is the decision: a row still at the
    gate, or abandoned there by a timeout, rests at `waiting_approval`, and
    `!= rejected` would admit it.

    `reuse_revoked_at` absent is the fourth conjunct. A rejection at a later run
    that reused this row's mapping stamps it and leaves `status` alone, because the
    row is the earlier run's history (`shared/mapping_reuse.py`). Without the
    conjunct a refused manifest would still exempt the next run from a human.
    """
    table = _fake_table([{"Items": []}])
    with patch.object(_mod, "table", table):
        assert _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH) is None

    kwargs = table.query.call_args.kwargs
    assert kwargs["IndexName"] == "PipelineIndex"
    assert kwargs["ScanIndexForward"] is False
    assert _conjuncts(kwargs["FilterExpression"]) == {
        ("=", "step_name", "schema_mapping"),
        ("=", "status", "success"),
        ("attribute_not_exists", "reuse_revoked_at", None),
        ("=", "source_schema_hash", _RUN_HASH),
    }
    assert "Limit" not in kwargs, "Limit is applied BEFORE the filter and must not be the bound"


def test_a_rejected_row_is_not_a_candidate():
    """Both resting states of an unapproved row, through the filter's own semantics.

    A rejection rests the run's row at `rejected`; a row whose gate never got a
    decision (a timeout) rests at `waiting_approval`. Neither satisfies
    `status == "success"`, so neither reaches the client-side eligibility test.
    """
    for resting_status in ("rejected", "waiting_approval"):
        # The real query would have filtered these out server-side; the client
        # side is asserted to agree by never being handed them.
        table = _fake_table([{"Items": [_row(status=resting_status)]}])
        with patch.object(_mod, "table", table):
            _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH)
        assert ("=", "status", "success") in _conjuncts(table.query.call_args.kwargs["FilterExpression"])


def test_a_manifest_recorded_against_a_different_schema_is_not_a_candidate():
    """R10.8's identity half, on the manifest's own hash rather than the row's.

    `LGR-a419843-1`'s defect was recency-without-identity, and this is the
    client-side half of the guard against reproducing it one domain over.
    """
    table = _fake_table([{"Items": [_row(manifest=_manifest(hash_value="a-different-schema"))]}])
    with patch.object(_mod, "table", table):
        assert _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH) is None


def test_a_row_without_an_approval_id_is_not_a_candidate():
    """R10.10's precondition. A manifest no approval can be named for cannot substitute.

    `LGR-c58f276-1` measured 438 audit rows carrying `actor == "unknown"`, 107 of
    them `approval_approved`; reusing a manifest with no attributable approval
    recreates that gap on purpose.
    """
    table = _fake_table([{"Items": [_row(approval_id=None)]}])
    with patch.object(_mod, "table", table):
        assert _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH) is None


def test_an_empty_renames_list_is_not_a_candidate():
    """A fully-conforming run's manifest covers nothing, so it exempts nothing."""
    table = _fake_table([{"Items": [_row(manifest=dict(_manifest(), renames=[]))]}])
    with patch.object(_mod, "table", table):
        assert _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH) is None


def test_a_row_carrying_no_manifest_is_not_a_candidate():
    """Every pre-p55 row is this shape, and none of them exempts anything."""
    row = _row()
    row["output_data"] = {"approval": {"approval_type": "schema_mapping"}}
    table = _fake_table([{"Items": [row]}])
    with patch.object(_mod, "table", table):
        assert _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH) is None


def test_the_lookup_returns_both_identifiers_task_6_4_records():
    """R10.10's two identifiers come back from the lookup rather than a second read."""
    table = _fake_table([{"Items": [_row()]}])
    with patch.object(_mod, "table", table):
        found = _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH)

    assert found is not None
    assert found["manifest_id"] == "mapping-hash-abc"
    assert found["approval_id"] == "approval-6-3"
    assert found["source_schema_hash"] == _RUN_HASH
    assert found["renames"] == [{"original_name": _NON_CONFORMING, "target_name": "first_name"}]


def test_the_newest_eligible_row_wins():
    """Newest-first, so the first eligible row on the page is the one reused."""
    newer = _row(manifest=_manifest(manifest_id="newer"), timestamp="2026-08-27T12:00:00+00:00")
    older = _row(manifest=_manifest(manifest_id="older"), timestamp="2026-08-20T12:00:00+00:00")
    table = _fake_table([{"Items": [older, newer]}])
    with patch.object(_mod, "table", table):
        found = _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH)

    assert found is not None
    assert found["manifest_id"] == "newer"


# --- R10.8: pagination ---------------------------------------------------------


def test_a_filtered_out_page_does_not_end_the_walk():
    """The pagination conjunct, and the reason it exists.

    DynamoDB applies `FilterExpression` AFTER the 1 MB page read, so a page
    holding only rows the filter rejects arrives as `Items: []` WITH a
    `LastEvaluatedKey`. Treating that as "no manifest" costs the fast path on
    exactly the busiest pipelines, silently — because the fall-through merely
    asks a human, which is what would have happened anyway.
    """
    table = _fake_table(
        [
            {"Items": [], "LastEvaluatedKey": {"run_id": "run-mid", "step_name": "schema_mapping"}},
            {"Items": [_row()]},
        ]
    )
    with patch.object(_mod, "table", table):
        found = _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH)

    assert found is not None, "an all-filtered first page ended the walk"
    assert table.query.call_count == 2
    assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {
        "run_id": "run-mid",
        "step_name": "schema_mapping",
    }
    assert "ExclusiveStartKey" not in table.query.call_args_list[0].kwargs


def test_an_exhausted_partition_stops_the_walk_without_reaching_the_bound():
    """No `LastEvaluatedKey` means the partition is genuinely finished."""
    table = _fake_table([{"Items": []}])
    with patch.object(_mod, "table", table):
        assert _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH) is None

    assert table.query.call_count == 1


def test_the_walk_stops_at_the_page_bound_and_requires_a_human():
    """The bound is a backstop on a pathological partition, and it fails closed."""
    page = {"Items": [], "LastEvaluatedKey": {"run_id": "r", "step_name": "schema_mapping"}}
    table = _fake_table([dict(page) for _ in range(_mod._RENAME_MANIFEST_MAX_PAGES + 2)])
    with patch.object(_mod, "table", table), patch.object(_mod, "logger") as logger:
        assert _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH) is None

    assert table.query.call_count == _mod._RENAME_MANIFEST_MAX_PAGES
    assert logger.warning.called, "hitting the page cap must be visible in the log"


def test_a_lookup_failure_requires_a_human_rather_than_granting_an_exemption():
    """Best-effort, and its failure direction is towards the gate.

    A throttle or an access-denied on this read must cost a human decision, never
    grant one — the opposite polarity to this module's read helpers, deliberately.
    """
    table = MagicMock()
    table.query.side_effect = RuntimeError("throttled")
    with patch.object(_mod, "table", table), patch.object(_mod, "logger") as logger:
        assert _mod.find_reusable_rename_manifest(_PIPELINE, _RUN_HASH) is None
    assert logger.warning.called

    with patch.object(_mod, "table", table):
        decision, _ = _decide(source_column_names=[_NON_CONFORMING], lookup_patched=False)
    assert decision is True


# --- R10.9: inert on an undeterminable hash, at the unit seam -------------------


def test_an_undeterminable_hash_makes_no_query_and_finds_nothing():
    """R10.9 at the unit seam, over every sentinel that reaches this comparison.

    `""` is what `store_mapping_result` writes when the hash is unavailable;
    `"no-hash"` is `store_mapping_separately`'s default and the manifest's, and it
    is the dangerous one — a CONSTANT that every undeterminable run shares, so an
    equality filter on it would answer "same schema" for schemas sharing nothing.
    """
    for sentinel in ("", "no-hash", None):
        table = MagicMock()
        with patch.object(_mod, "table", table):
            assert _mod.find_reusable_rename_manifest(_PIPELINE, sentinel) is None  # type: ignore[arg-type]
        table.query.assert_not_called()


def test_an_undeterminable_hash_leaves_the_clause_forcing_a_human():
    """The inertness points at the human, which is the pre-existing default."""
    table = MagicMock()
    with patch.object(_mod, "table", table):
        decision, _ = _decide(source_column_names=[_NON_CONFORMING], run_schema_hash="no-hash", lookup_patched=False)

    assert decision is True
    table.query.assert_not_called()


# --- Handler wiring ------------------------------------------------------------

_PATCH_TARGETS = (
    "get_source_schema",
    "get_pipeline_metadata",
    "get_target_schema",
    "normalize_schema_format",
    "get_ai_model_for_pipeline",
    "generate_mapping_id",
    "enforce_source_field_mappability",
    "store_mapping_separately",
    "store_mapping_result",
    "publish_pipeline_state_change",
    "write_agent_audit_entry",
    "AgentTracePlugin",
    "read_source_samples",
)


class _nested:
    """Enter a mapping of named context managers, exit them in reverse.

    The same helper shape the sibling handler-driving files in this directory use.
    """

    def __init__(self, managers: dict):
        self._managers = managers

    def __enter__(self) -> dict:
        self._entered: list = []
        values = {}
        for name, manager in self._managers.items():
            values[name] = manager.__enter__()
            self._entered.append(manager)
        return values

    def __exit__(self, *exc_info):
        for manager in reversed(self._entered):
            manager.__exit__(*exc_info)
        return False


def _run_handler(
    *,
    row_hash: str | None = _RUN_HASH,
    event_hash: str = _RUN_HASH,
    source_columns: tuple[str, ...] = ("cust_id", _NON_CONFORMING),
    stub_decision: bool = True,
    query_pages: list[dict] | None = None,
) -> dict:
    """One `lambda_handler` run, returning the mocks so the caller can interrogate them."""
    extract_item: dict = {
        "run_id": "run-6-3",
        "step_name": "schema_extract",
        "original_pipeline_id": _PIPELINE,
    }
    if row_hash is not None:
        extract_item["schema_hash"] = row_hash

    table = MagicMock()
    table.get_item.return_value = {"Item": extract_item}
    table.query.side_effect = list(query_pages) if query_pages else [{"Items": []}]

    managers = {name: patch.object(_mod, name) for name in _PATCH_TARGETS}
    managers["table"] = patch.object(_mod, "table", table)
    managers["memory_client"] = patch.object(_mod, "memory_client", None)
    managers["generate_mapping_with_bedrock"] = patch.object(
        _mod, "generate_mapping_with_bedrock", return_value=_high_confidence_mapping(_first_name_column(source_columns))
    )
    if stub_decision:
        managers["determine_approval_requirement"] = patch.object(_mod, "determine_approval_requirement")

    with _nested(managers) as mocks:
        mocks["get_source_schema"].return_value = _source_schema(*source_columns)
        mocks["get_pipeline_metadata"].return_value = _pipeline_metadata()
        mocks["get_target_schema"].return_value = (_target_schema(), "tgt-hash")
        mocks["normalize_schema_format"].side_effect = lambda schema: schema
        mocks["get_ai_model_for_pipeline"].return_value = "us.test-model"
        mocks["generate_mapping_id"].return_value = "mapping-hash-abc"
        mocks["read_source_samples"].return_value = {}
        mocks["AgentTracePlugin"].return_value = MagicMock()
        if stub_decision:
            mocks["determine_approval_requirement"].return_value = False

        event = {
            "run_id": "run-6-3",
            "pipeline_id": _PIPELINE,
            "source_schema_hash": event_hash,
            "additional_context": "",
        }
        mocks["_result"] = _mod.lambda_handler(event, None)
        mocks["_table"] = table

    return mocks


def _approval_required(mocks: dict) -> bool:
    """The `approval_required` value the handler handed `store_mapping_result`.

    Read positionally and with the arity asserted, so a signature change fails
    here loudly instead of turning every wired assertion below into a comparison
    against `None`.
    """
    args, kwargs = mocks["store_mapping_result"].call_args
    if "approval_required" in kwargs:
        return kwargs["approval_required"]
    assert len(args) == 10, f"store_mapping_result's arity changed; approval_required is not args[9]: {args}"
    return args[9]


def test_the_handler_passes_the_column_names_and_the_rows_own_hash():
    """R10.8 names the `schema_extract` state row as the Schema_Hash authority."""
    mocks = _run_handler(row_hash="row-hash-wins", event_hash="event-hash")

    kwargs = mocks["determine_approval_requirement"].call_args.kwargs
    assert kwargs["run_schema_hash"] == "row-hash-wins"
    assert kwargs["source_column_names"] == ["cust_id", _NON_CONFORMING]


def test_the_handler_falls_back_to_the_event_hash_when_the_row_carries_none():
    """The two are the same value on the normal path; the fallback covers a rerun."""
    mocks = _run_handler(row_hash=None, event_hash="event-hash")

    assert mocks["determine_approval_requirement"].call_args.kwargs["run_schema_hash"] == "event-hash"


def test_through_the_handler_an_undeterminable_hash_requires_a_human():
    """R10.9 through the wiring, with the real clause and the real lookup.

    Neither the row nor the event carries a hash, so the value reaching the
    filter is `""` — inert, no query issued, and the run is routed to a human.
    """
    mocks = _run_handler(row_hash=None, event_hash="", stub_decision=False)

    assert _approval_required(mocks) is True
    mocks["_table"].query.assert_not_called()


def test_through_the_handler_a_reusable_manifest_lifts_the_clause():
    """The whole path: row hash → filter → eligible row → no human for this clause."""
    mocks = _run_handler(
        row_hash=_RUN_HASH, event_hash=_RUN_HASH, stub_decision=False, query_pages=[{"Items": [_row()]}]
    )

    assert _approval_required(mocks) is False
    assert mocks["_table"].query.called


def test_through_the_handler_a_non_conforming_column_forces_a_human_with_no_candidate():
    """The complement of the test above, so neither is vacuous."""
    mocks = _run_handler(row_hash=_RUN_HASH, event_hash=_RUN_HASH, stub_decision=False, query_pages=[{"Items": []}])

    assert _approval_required(mocks) is True


def test_through_the_handler_a_conforming_schema_issues_no_reuse_query():
    """The cheap check gates the read on the wired path too."""
    mocks = _run_handler(source_columns=("cust_id", "first_name"), stub_decision=False)

    mocks["_table"].query.assert_not_called()
    assert _approval_required(mocks) is False


def test_confidence_threshold_is_the_shared_constant_not_a_local_literal():
    """Guards the fixture's own premise: 0.97/0.98 must clear the real threshold."""
    assert float(_mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD) == 0.85
