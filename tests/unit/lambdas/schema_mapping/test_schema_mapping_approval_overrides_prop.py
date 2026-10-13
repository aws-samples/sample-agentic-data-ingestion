# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 5 — the sibling gate's two overrides beat its own configuration.

`schema_mapping.determine_approval_requirement`
(`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:949-1037`)
checks fallback provenance (`:960-967`) and `always_approve_schema_changes`
(`:969-975`) **before** it reads `schema_mapping_enabled` (`:985`). A disabled
configuration therefore already loses to a quality or risk concern. p34
requirements 4.6 and 4.7 pin that ordering so the p34 change to the sibling
`CheckCodeApproval`/`CheckSchemaMappingApproval` gates cannot quietly invert it.

**Expectations derive from requirements 4.6 and 4.7, not from running the
function.** Generating the enabled/disabled axis of the
Schema_Mapping_Approval_Setting is what proves "irrespective of" — a fixture
that pins the setting to one value proves only that one value.

Two deliberate non-assertions, both from the p34 design:

1. **The trailing `except Exception: return True` (`:1033-1037`) is NOT pinned
   as intended behaviour.** It is the pattern `LGR-78ce0b7-13` (template /
   ratified, scope names this file) narrowed out of `get_target_schema` in this
   same module, for stringifying an already-classified `StepFailure` /
   `TransientStepFailure` into a bare `ValueError`. Both properties assert
   `logger.error` was never called, which is the only `logger.error` inside this
   function — so a `True` produced by the swallow fails the property instead of
   being recorded as the documented override.
2. **The confidence heuristics (`:1009-1027`) are not asserted**, and neither is
   the no-override outcome. Requirements open question 2 leaves the heuristics
   deliberately unchanged and records that they can still return `False` while
   the setting is `True`; asserting that would pin an open question as settled.
   Only the two documented overrides are asserted. What keeps the properties
   from being vacuous instead is the mock-call assertion on each: R4.6 must
   reach its `True` **without** reading the configuration at all, R4.7 must
   reach its `True` having read it exactly once. A function returning `True`
   unconditionally fails both. Both generators additionally build a mapping the
   heuristics would auto-approve, so no `True` here is attributable to them.

Function untouched — this file adds coverage and changes no production code.

**Validates: Requirements 4.6, 4.7, 8.3**
"""

import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1); these are no-ops
# kept for parity with the sibling files in this directory.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Lambda-layer-only dependencies (the pattern in test_schema_mapping_fallback_provenance.py).
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
# share, so it cross-contaminates between test files (LGR-b7d5490-1, backfilled).
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

# Requirement 4.7's three actions, verbatim from `:970`.
_SCHEMA_CHANGE_ACTIONS = ("create_new_pipeline", "update_existing_pipeline", "remap_required")

# Actions that do NOT force approval, kept in the requirement-4.6 generator so
# the fallback override is shown to fire independently of the schema-change one.
_NON_FORCING_ACTIONS = (None, "use_existing_pipeline")


# --- Generators ---------------------------------------------------------------


@st.composite
def _auto_approvable_mapping(draw: st.DrawFn) -> dict:
    """A mapping the confidence heuristics (`:1009-1027`) would auto-approve.

    Every confidence is above `CONFIDENCE_AUTO_APPROVE_THRESHOLD` and both
    unmapped lists are empty, so `:1009`, `:1013`, `:1019` and `:1025` all fall
    through. That makes a `True` result attributable to an override and nothing
    else — the point of the property.

    AMENDED for T-04: each entry's source and target carry the same name
    (`field_i`), where they used to be `src_i` → `tgt_i`. T-04 added a clause
    that sends any entry no deterministic evidence corroborates to a human, and
    `src_i`/`tgt_i` share no name — so without this change the generator would
    no longer be "a mapping the heuristics would auto-approve", and a `True`
    here could have come from that clause instead of from the override.
    """
    field_count = draw(st.integers(min_value=1, max_value=6))
    confidences = draw(
        st.lists(
            st.floats(
                min_value=_mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD + 0.01,
                max_value=1.0,
                allow_nan=False,
                allow_infinity=False,
            ),
            min_size=field_count,
            max_size=field_count,
        )
    )
    return {
        "mappings": [
            {"source_field": f"field_{i}", "target_field": f"field_{i}", "confidence": c}
            for i, c in enumerate(confidences)
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
    }


@st.composite
def _pipeline_record(draw: st.DrawFn, always_approve_schema_changes: bool | None = None) -> dict:
    """A pipeline record generated across the Schema_Mapping_Approval_Setting axis.

    `schema_mapping_enabled` is drawn from {`True`, `False`, absent}. Absent is
    included because `:985` defaults it to `True`, so absent and `True` are the
    same setting reached two ways, and `False` is the case both requirements say
    must lose. `always_approve_schema_changes` is set only when a caller asks
    for it; `None` means the key is absent, which is `:973`'s default-`True`
    path.
    """
    approval_config: dict = {}
    enabled = draw(st.sampled_from([True, False, None]))
    if enabled is not None:
        approval_config["schema_mapping_enabled"] = enabled
    if always_approve_schema_changes is not None:
        approval_config["always_approve_schema_changes"] = always_approve_schema_changes
    return {"pipeline_id": draw(st.sampled_from(["pipe-1", "hello123-c4827272"])), "approval_config": approval_config}


def _records_across_always_approve() -> st.SearchStrategy[dict]:
    """Records spanning R4.7's "`always_approve_schema_changes` not explicitly false".

    `True` and absent, the two values `:973` treats as forcing. The explicit
    `False` case is out of scope — see the note on the R4.7 property.
    """
    return st.sampled_from([True, None]).flatmap(
        lambda always_approve: _pipeline_record(always_approve_schema_changes=always_approve)
    )


# --- Property 5, requirement 4.6: fallback provenance beats the setting -------


# Feature: p34-code-approval-gate-honors-config, Property 5: The sibling gate's two overrides beat its own configuration
@settings(max_examples=200, deadline=None)
@given(
    mapping_result=_auto_approvable_mapping(),
    pipeline_record=_pipeline_record(),
    provenance=st.sampled_from(["flag", "marker", "both"]),
    truthy_flag=st.sampled_from([True, 1, "yes", 0.5]),
    schema_change_action=st.sampled_from(_SCHEMA_CHANGE_ACTIONS + _NON_FORCING_ACTIONS),
)
def test_fallback_provenance_requires_approval_irrespective_of_the_setting(
    mapping_result, pipeline_record, provenance, truthy_flag, schema_change_action
):
    """R4.6: fallback provenance forces approval whatever the setting says.

    Provenance is either a truthy `requires_manual_approval`, the
    `generated_by == FALLBACK_GENERATED_BY` marker, or both — the three ways the
    disjunction at `:960-962` is satisfied. The truthy flag is drawn across
    types because `:960` tests truthiness rather than identity, and a mapping
    record round-tripped through DynamoDB does not guarantee a JSON boolean.
    """
    if provenance in ("flag", "both"):
        mapping_result["requires_manual_approval"] = truthy_flag
    if provenance in ("marker", "both"):
        mapping_result["generated_by"] = _mod.FALLBACK_GENERATED_BY

    with (
        patch.object(_mod, "get_pipeline_metadata") as mock_get_meta,
        patch.object(_mod, "logger") as mock_logger,
    ):
        mock_get_meta.return_value = pipeline_record
        result = _mod.determine_approval_requirement(
            pipeline_record["pipeline_id"], mapping_result, schema_change_action=schema_change_action
        )

    assert result is True
    # The override fires before any configuration lookup (`:960-967` precedes
    # `:985`), which is the structural form of "irrespective of the setting".
    mock_get_meta.assert_not_called()
    # NOT the trailing `except Exception: return True` (`:1033-1037`) — that
    # clause is the only `logger.error` in the function (LGR-78ce0b7-13).
    mock_logger.error.assert_not_called()


# --- Property 5, requirement 4.7: a schema change beats the setting ----------


# Feature: p34-code-approval-gate-honors-config, Property 5: The sibling gate's two overrides beat its own configuration
@settings(max_examples=200, deadline=None)
@given(
    mapping_result=_auto_approvable_mapping(),
    pipeline_record=_records_across_always_approve(),
    schema_change_action=st.sampled_from(_SCHEMA_CHANGE_ACTIONS),
)
def test_schema_change_requires_approval_irrespective_of_the_setting(
    mapping_result, pipeline_record, schema_change_action
):
    """R4.7: a genuine schema change forces approval whatever the setting says.

    `always_approve_schema_changes` is drawn from {`True`, absent} — the
    requirement's "not explicitly `false`". Its explicitly-`false` case is
    already pinned by `test_schema_mapping_approval.py:115`
    (`test_schema_change_respects_always_approve_false`) and is deliberately not
    re-asserted here: `:973`'s truthiness test is not what R4.7 governs.
    """
    with (
        patch.object(_mod, "get_pipeline_metadata") as mock_get_meta,
        patch.object(_mod, "logger") as mock_logger,
    ):
        mock_get_meta.return_value = pipeline_record
        result = _mod.determine_approval_requirement(
            pipeline_record["pipeline_id"], mapping_result, schema_change_action=schema_change_action
        )

    assert result is True
    # This override does read the record — the setting is generated and ignored
    # rather than unreached, so `schema_mapping_enabled=False` losing to it is
    # observed rather than implied.
    mock_get_meta.assert_called_once_with(pipeline_record["pipeline_id"])
    mock_logger.error.assert_not_called()


# ---------------------------------------------------------------------------
# p37 task 11.2 — C10's forcing clause over its shape axis (R3.3)
#
# INSERTION ONLY. Nothing above this line is amended. Both p34 properties call
# `determine_approval_requirement` with three arguments, so C10's optional
# fourth parameter defaults to `None`, its clause is inert, and
# `_auto_approvable_mapping` generates no unsourced target field anyway — every
# mapping it draws carries a `source_field` and both unmapped lists are empty.
# Property 5 is therefore untouched by C10 in both letter and effect.
#
# **These are NOT a new numbered property.** design.md §6 numbers three
# properties for p37 (tasks 5.2, 8.2, 11.4) and this is none of them. Task 11.2
# is a unit-test task; what follows is its unit case *generated* rather than
# enumerated, because the shape axis is a product — five unusable `source_field`
# forms × two list placements × two element shapes — and writing out ten
# `parametrize` rows for one assertion buys less than drawing them. The example
# at the observed run's exact arithmetic lives in
# `test_schema_mapping_approval.py`, where it can assert the literal 0.125 and
# 0.95 that make the case load-bearing.
#
# A THIRD deliberate non-assertion, in the spirit of the two above:
#
# 3. **`schema_mapping_enabled=False` is not generated here, and C10's clause
#    does not override it.** The enablement check returns `False` at `:1081-1083`
#    BEFORE the clause at `:1115-1120` — as it does before the confidence and
#    ratio rules too, because design C10 specifies the clause "placed with the
#    other quality clauses". So a disabled configuration still auto-approves a
#    required unsourced target field. Asserting that either way would settle a
#    question the design did not ask: R3.3 reads as an unconditional
#    requirement, while C10's stated placement makes it a quality clause. The
#    generator below draws only the enabled axis ({`True`, absent}) and the
#    divergence is recorded rather than resolved.
# ---------------------------------------------------------------------------


# Sentinel for "the key is absent entirely", which is a different input from
# `source_field: None` and is what an unvalidated Bedrock response produces.
_ABSENT = object()


@st.composite
def _mapping_with_one_unsourced_target_field(draw: st.DrawFn) -> tuple[dict, dict, str]:
    """A mapping the existing heuristics auto-approve, with one unsourced target field.

    Returns `(mapping_result, target_schema, field_name)`.

    At least four sourced fields, so the unmapped-target ratio stays at or below
    the existing `> 0.2` rule (1/5 = 20%) in the placement where the field lands
    in `unmapped_target_fields`. Every confidence is above
    `CONFIDENCE_AUTO_APPROVE_THRESHOLD`, and `unmapped_source_fields` is empty,
    so no pre-existing rule has anything to fire on. The test does not take that
    on trust — it calls the function once with `target_schema=None` and requires
    `False`, which *observes* the auto-approve for each drawn input.
    """
    sourced_count = draw(st.integers(min_value=4, max_value=12))
    confidences = draw(
        st.lists(
            st.floats(
                min_value=_mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD + 0.01,
                max_value=1.0,
                allow_nan=False,
                allow_infinity=False,
            ),
            min_size=sourced_count + 1,
            max_size=sourced_count + 1,
        )
    )
    field = draw(st.sampled_from(["phone", "mobile_number", "Postal_Code", "tax_id"]))
    sourced = [f"tgt_{i}" for i in range(sourced_count)]

    # AMENDED for T-04: each sourced entry's `source_field` is now its target's
    # own name, where it used to be `src_i`. T-04's corroboration clause sends
    # any entry whose names do not agree to a human, so `src_i` → `tgt_i` would
    # have made every sourced entry a reason for `True` and the no-schema call
    # below could never observe the auto-approve it exists to observe.
    mappings = [
        {"source_field": name, "target_field": name, "confidence": confidences[i]} for i, name in enumerate(sourced)
    ]

    # The two placements the single reader must handle: `mappings` without a
    # usable source (pre-C9 / pre-existing approved records) and
    # `unmapped_target_fields` (post-C9 / the name-similarity fallback).
    placement = draw(st.sampled_from(["in_mappings", "in_unmapped_target"]))
    unmapped_target: list = []
    if placement == "in_mappings":
        unusable = draw(st.sampled_from([_ABSENT, None, "", "   ", "\t", 7, ["src"], {}]))
        entry = {"target_field": field, "confidence": confidences[-1]}
        if unusable is not _ABSENT:
            entry["source_field"] = unusable
        mappings.append(entry)
    else:
        # Both element shapes the two producers write.
        unmapped_target = [
            draw(
                st.sampled_from(
                    [
                        field,
                        {"field": field, "default_value": None, "reason": "No matching source field"},
                        {"field": f"  {field}  "},
                    ]
                )
            )
        ]

    mapping_result = {
        "mappings": mappings,
        "unmapped_source_fields": [],
        "unmapped_target_fields": unmapped_target,
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }
    target_schema = {
        "type": "object",
        "properties": {name: {"type": "string"} for name in (*sourced, field)},
        "required": [field],
    }
    return mapping_result, target_schema, field


#: The `logger.info` text the C10 forcing clause emits when — and only when — it
#: is the clause that returns `True`. Read by the two tests below to attribute a
#: decision to C10 on the placement where a second clause (T-04) now also forces.
_C10_FORCING_LOG = "required target field(s) have no source"


def _placed_in_mappings(mapping_result: dict, field: str) -> bool:
    """Did the generator put the unsourced field inside `mappings` (pre-C9 shape)?"""
    return any(isinstance(m, dict) and m.get("target_field") == field for m in mapping_result["mappings"])


def _info_messages(mock_logger) -> list[str]:
    return [str(call.args[0]) for call in mock_logger.info.call_args_list if call.args]


@settings(max_examples=200, deadline=None)
@given(
    case=_mapping_with_one_unsourced_target_field(),
    always_approve=st.sampled_from([True, None]),
)
def test_required_unsourced_target_field_forces_approval_whatever_shape_it_arrives_in(case, always_approve):
    """R3.3: a required target field with no source forces a human, in every shape.

    The two calls together are the assertion. The first, with no target schema,
    must return `False` — that observes the pre-existing quality rules
    auto-approving this exact input, so the second call's `True` is attributable
    to the C10 clause and to nothing else. A function returning `True`
    unconditionally fails the first call.

    This is the generated form of the observed run's 12.5%-at-high-confidence
    case: `newtest-ab2a08a0` / `run-2026-08-09-15-38-19-5c5f5f8a`, 8 target
    fields with `phone` unsourced. It must stay green through task 11.3, because
    C9 moves the field between the two placements this generator draws and the
    answer has to be the same on both sides of that move.
    """
    mapping_result, target_schema, field = case
    approval_config: dict = {"schema_mapping_enabled": True}
    if always_approve is not None:
        approval_config["always_approve_schema_changes"] = always_approve
    pipeline_record = {"pipeline_id": "newtest-ab2a08a0", "approval_config": approval_config}

    with (
        patch.object(_mod, "get_pipeline_metadata") as mock_get_meta,
        patch.object(_mod, "logger") as mock_logger,
    ):
        mock_get_meta.return_value = pipeline_record
        without_schema = _mod.determine_approval_requirement("newtest-ab2a08a0", mapping_result, None)
        mock_logger.info.reset_mock()
        with_schema = _mod.determine_approval_requirement("newtest-ab2a08a0", mapping_result, None, target_schema)
        with_schema_messages = _info_messages(mock_logger)
        named = _mod.unsourced_target_field_names(mapping_result, target_schema)

    if _placed_in_mappings(mapping_result, field):
        # EXPECTED VALUE CHANGED for T-04 on this placement only. The unsourced
        # entry sits inside `mappings` with no usable `source_field`, so T-04's
        # corroboration clause now forces a human even with no target schema in
        # hand — the no-schema call can no longer observe an auto-approve here.
        # Attribution of the second call to C10 is kept instead by its log line:
        # C10 runs before T-04 and logs this text only when it is the clause that
        # returns, so a `True` from T-04 alone fails the assertion below.
        assert without_schema is True
    else:
        assert without_schema is False, "the pre-existing rules must auto-approve this input, or the case is vacuous"
    assert with_schema is True
    assert any(_C10_FORCING_LOG in message for message in with_schema_messages), with_schema_messages
    # R3.2's naming, from the same single reader the decision uses, so the two
    # cannot disagree about what "unsourced" means.
    assert named == [field.strip()]
    # NOT the trailing `except Exception: return True` (`:1147-1150`), the only
    # `logger.error` in the function (`LGR-78ce0b7-13`, template / ratified).
    mock_logger.error.assert_not_called()


@settings(max_examples=200, deadline=None)
@given(case=_mapping_with_one_unsourced_target_field())
def test_an_unsourced_target_field_outside_required_is_named_but_never_forces(case):
    """Risk 6: the naming covers every unsourced field; only the clause is gated.

    Same drawn inputs, `required` emptied. The field is still named — so if live
    uploaded schemas turn out never to populate `required`, the fallback is
    widening the clause against this same reader — and the decision is unchanged
    from the no-schema answer, which the sibling test pins at `False`.
    """
    mapping_result, target_schema, field = case
    schema_without_required = {k: v for k, v in target_schema.items() if k != "required"}
    pipeline_record = {"pipeline_id": "newtest-ab2a08a0", "approval_config": {"schema_mapping_enabled": True}}

    with (
        patch.object(_mod, "get_pipeline_metadata") as mock_get_meta,
        patch.object(_mod, "logger") as mock_logger,
    ):
        mock_get_meta.return_value = pipeline_record
        result = _mod.determine_approval_requirement("newtest-ab2a08a0", mapping_result, None, schema_without_required)
        messages = _info_messages(mock_logger)
        named = _mod.unsourced_target_field_names(mapping_result, schema_without_required)

    assert named == [field.strip()]
    # "Never forces" is a claim about C10, and it is asserted about C10 directly:
    # its forcing log line must be absent on every drawn input.
    assert not any(_C10_FORCING_LOG in message for message in messages), messages
    if _placed_in_mappings(mapping_result, field):
        # EXPECTED VALUE CHANGED for T-04 on this placement only: an entry inside
        # `mappings` with no usable `source_field` is uncorroborated, so T-04 —
        # not C10, per the assertion above — sends the run to a human.
        assert result is True
    else:
        assert result is False
    mock_logger.error.assert_not_called()
