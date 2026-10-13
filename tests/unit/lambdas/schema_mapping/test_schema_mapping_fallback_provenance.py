# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: fallback mapping provenance and forced approval (P28 R3, Task 4.1).

When Bedrock mapping generation fails or returns unparseable JSON, the
``generate_simple_mapping`` fallback must:

- record honest provenance (``generated_by="fallback_substring_match"``,
  never the Bedrock model id),
- force manual approval regardless of confidence score, and
- cap confidence values strictly below the auto-approve threshold by
  construction (no more 0.8-meets-0.8).

The mapping approval payload must carry the provenance so approvers can
see when they are reviewing a fallback mapping.

**Validates: Requirements 3.1, 3.2, 3.5** (and the fallback half of 3.6)
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Add Lambda dir and shared dir to import path
_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub out Lambda-layer dependencies (mirrors test_step_failure_raise.py)
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

sys.modules.pop("lambda_function", None)
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

# --- Helpers ---


def _source_schema():
    return {"properties": {"id": {"type": "string"}, "email": {"type": "string"}}}


def _target_schema():
    """Target where 'user_id' gets a contains-match against 'id'.

    ``calculate_field_similarity("id", "user_id")`` returns exactly 0.8 — the
    boundary case: a would-be 0.8 fallback confidence under a 0.8 threshold.
    'email' is an exact match (would-be 1.0).
    """
    return {"properties": {"user_id": {"type": "string"}, "email": {"type": "string"}}}


def _fallback_mapping_result(confidence=0.95):
    """A fallback-marked mapping result with HIGH computed confidence."""
    return {
        "mappings": [
            {"target_field": "user_id", "source_field": "id", "transformation": "direct", "confidence": confidence},
            {
                "target_field": "email",
                "source_field": "email",
                "transformation": "direct",
                "confidence": confidence,
            },
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "fallback",
        "generated_by": _mod.FALLBACK_GENERATED_BY,
        "requires_manual_approval": True,
    }


def _ai_mapping_result(model_id="us.anthropic.claude-3-5-sonnet-20241022-v2:0"):
    """An AI-generated mapping result with high confidence and no unmapped fields."""
    return {
        "mappings": [
            {"target_field": "user_id", "source_field": "id", "transformation": "direct", "confidence": 0.95},
            {"target_field": "email", "source_field": "email", "transformation": "direct", "confidence": 0.98},
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "ai",
        "generated_by": model_id,
    }


def _approval_enabled_pipeline(schema_mapping_enabled=True):
    return {
        "pipeline_id": "test-pipeline",
        "approval_config": {
            "schema_mapping_enabled": schema_mapping_enabled,
            "always_approve_schema_changes": True,
        },
    }


# --- Fallback provenance and confidence cap (R3.1, R3.2) ---


def test_fallback_records_provenance_and_forces_manual_approval_flag():
    """generate_simple_mapping marks honest provenance and the forced-approval flag."""
    result = _mod.generate_simple_mapping(_source_schema(), _target_schema())

    assert result["generated_by"] == "fallback_substring_match"
    assert result["requires_manual_approval"] is True
    # Never the Bedrock model id
    assert result["generated_by"] != _mod.BEDROCK_MODEL_ID


def test_fallback_confidence_capped_below_threshold_by_construction():
    """Every fallback confidence is strictly below the auto-approve threshold.

    Boundary: 'id' → 'user_id' is a contains-match scoring exactly 0.8 (the
    threshold value); 'email' → 'email' is an exact match scoring 1.0. Both
    must be capped strictly below the 0.8 auto-approve threshold.
    """
    # The cap itself is below the threshold by construction
    assert _mod.FALLBACK_CONFIDENCE_CAP < _mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD

    result = _mod.generate_simple_mapping(_source_schema(), _target_schema())

    mappings = {m["target_field"]: m for m in result["mappings"]}
    # Both fields matched
    assert set(mappings) == {"user_id", "email"}
    for mapping in mappings.values():
        assert mapping["confidence"] <= _mod.FALLBACK_CONFIDENCE_CAP
        assert mapping["confidence"] < _mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD
    # The would-be 0.8 (== threshold) contains-match got capped
    assert mappings["user_id"]["confidence"] == _mod.FALLBACK_CONFIDENCE_CAP
    # The would-be 1.0 exact match got capped too
    assert mappings["email"]["confidence"] == _mod.FALLBACK_CONFIDENCE_CAP


def test_fallback_error_branch_also_records_provenance():
    """The generate_simple_mapping internal-error branch keeps honest provenance."""
    # A schema whose properties access blows up inside the try block
    broken_schema = MagicMock()
    broken_schema.get.side_effect = [Exception("boom"), {}, {}]

    result = _mod.generate_simple_mapping(broken_schema, {"properties": {}})

    assert result["generated_by"] == "fallback_substring_match"
    assert result["requires_manual_approval"] is True


# --- Bedrock failure paths fall back WITH provenance (R3.1) ---


def test_bedrock_call_failure_falls_back_with_provenance():
    """A Bedrock invocation error produces a fallback-marked mapping result."""
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.side_effect = Exception("bedrock unavailable")
        result = _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model")

    assert result["generated_by"] == "fallback_substring_match"
    assert result["requires_manual_approval"] is True


def test_unparseable_bedrock_response_falls_back_with_provenance():
    """A non-JSON Bedrock response produces a fallback-marked mapping result."""
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "sorry, no JSON here"}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }
        result = _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model")

    assert result["generated_by"] == "fallback_substring_match"
    assert result["requires_manual_approval"] is True


# --- Fallback forces approval regardless of confidence (R3.1) ---


def test_fallback_forces_approval_even_with_high_computed_confidence():
    """A fallback mapping with 0.95 confidence still requires approval (belt)."""
    with patch.object(_mod, "get_pipeline_metadata") as mock_get_meta:
        mock_get_meta.return_value = _approval_enabled_pipeline()
        result = _mod.determine_approval_requirement("test-pipeline", _fallback_mapping_result(confidence=0.95))

    assert result is True
    # The fallback check fires before any pipeline-config lookup
    mock_get_meta.assert_not_called()


def test_fallback_forces_approval_even_when_pipeline_approval_disabled():
    """Fallback approval cannot be disabled by pipeline approval configuration."""
    with patch.object(_mod, "get_pipeline_metadata") as mock_get_meta:
        mock_get_meta.return_value = _approval_enabled_pipeline(schema_mapping_enabled=False)
        result = _mod.determine_approval_requirement("test-pipeline", _fallback_mapping_result(confidence=0.99))

    assert result is True


def test_generated_by_marker_alone_forces_approval():
    """generated_by=fallback forces approval even without the boolean flag."""
    mapping_result = _fallback_mapping_result(confidence=0.95)
    del mapping_result["requires_manual_approval"]

    with patch.object(_mod, "get_pipeline_metadata"):
        assert _mod.determine_approval_requirement("test-pipeline", mapping_result) is True


# --- AI path unaffected (R3.6 counterpart) ---


def test_ai_path_still_records_model_id():
    """A successful Bedrock mapping records the actual model id as provenance."""
    ai_body = {
        "mappings": [{"target_field": "email", "source_field": "email", "confidence": 0.97}],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "ai mapping",
    }
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": json.dumps(ai_body)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
        }
        result = _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model")

    assert result["generated_by"] == "us.test-model"
    assert not result.get("requires_manual_approval")


def test_a_reply_cut_off_at_the_token_ceiling_falls_back_and_says_why(caplog):
    """stopReason=max_tokens is an incomplete mapping even if what arrived parses.

    The reply below is valid JSON, which is exactly the case that must not be
    taken as a mapping: the model was stopped before it finished.
    """
    partial_body = {
        "mappings": [{"target_field": "email", "source_field": "email", "confidence": 0.97}],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "partial",
    }
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": json.dumps(partial_body)}]}},
            "stopReason": "max_tokens",
            "usage": {"inputTokens": 10, "outputTokens": 16000},
        }
        result = _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model")

    assert result["generated_by"] == "fallback_substring_match"
    assert result["requires_manual_approval"] is True
    assert any("stopReason=max_tokens" in r.getMessage() for r in caplog.records), (
        "the fallback log must name the token ceiling as the cause"
    )


def test_a_reasoning_block_before_the_answer_keeps_the_ai_mapping():
    """A reply that leads with a reasoning block is still an AI mapping.

    Claude Opus 5.5 answers `[reasoningContent, text]`. Reading
    `content[0]["text"]` raised KeyError on that shape, and the outer `except`
    degraded the mapping to `fallback_substring_match` (run
    run-2026-10-09-18-39-09-2af368a0).
    """
    ai_body = {
        "mappings": [{"target_field": "email", "source_field": "email", "confidence": 0.97}],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "ai mapping",
    }
    reasoning = {"reasoningContent": {"reasoningText": {"text": "email maps to email.", "signature": "sig"}}}
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [reasoning, {"text": json.dumps(ai_body)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
        }
        result = _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model")

    assert result["generated_by"] == "us.test-model"
    assert not result.get("requires_manual_approval")


def test_ai_path_still_auto_approves_above_threshold():
    """A high-confidence AI mapping with no unmapped fields needs no approval."""
    pipeline = _approval_enabled_pipeline()
    # AMENDED for T-04. `_ai_mapping_result` maps `id → user_id`, a SEMANTIC
    # match: the names do not agree after normalisation, and T-04 now sends an
    # uncorroborated pairing to a human however confident the model is. The
    # fixture is shared with the payload and store tests below, so it is left
    # as it is and the pairing is corroborated the way an operator would do it
    # in production — a mapping hint naming exactly that pair. What this test
    # exists to show is unchanged: the fallback rule does not reach a genuine
    # AI-generated mapping.
    pipeline["mapping_hints"] = {"id": "user_id"}
    with patch.object(_mod, "get_pipeline_metadata") as mock_get_meta:
        mock_get_meta.return_value = pipeline
        result = _mod.determine_approval_requirement("test-pipeline", _ai_mapping_result())

    assert result is False


# --- Approval payload carries provenance (R3.5) ---


def test_approval_payload_carries_fallback_provenance_and_requires_review():
    """The approval payload exposes generated_by and forces requires_review."""
    payload = _mod.create_approval_payload(
        _source_schema(), _target_schema(), _fallback_mapping_result(0.95), "h1", "h2"
    )

    assert payload["generated_by"] == "fallback_substring_match"
    # High confidence + zero unmapped fields would normally skip review;
    # the fallback marker forces it.
    assert payload["requires_review"] is True


def test_approval_payload_carries_model_id_for_ai_mappings():
    """The approval payload records the model id for AI-generated mappings."""
    model_id = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
    payload = _mod.create_approval_payload(_source_schema(), _target_schema(), _ai_mapping_result(model_id), "h1", "h2")

    assert payload["generated_by"] == model_id
    assert payload["requires_review"] is False


# --- Stored mapping record honesty (R3.1) ---


def test_store_mapping_separately_records_fallback_not_model_id():
    """The mappings-table record never claims the Bedrock model id for fallbacks."""
    with patch.object(_mod, "mappings_table") as mock_table:
        mock_table.get_item.return_value = {}
        _mod.store_mapping_separately(
            "mapping-1", _fallback_mapping_result(), "h1", "h2", run_id="run-1", ai_model_used="us.test-model"
        )

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["generated_by"] == "fallback_substring_match"
    assert item["ai_model_used"] == "fallback_substring_match"
    assert item["ai_model_used"] != "us.test-model"


def test_store_mapping_separately_keeps_model_id_for_ai_mappings():
    """The mappings-table record keeps the real model id for AI mappings."""
    model_id = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
    with patch.object(_mod, "mappings_table") as mock_table:
        mock_table.get_item.return_value = {}
        _mod.store_mapping_separately(
            "mapping-2", _ai_mapping_result(model_id), "h1", "h2", run_id="run-1", ai_model_used=model_id
        )

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["generated_by"] == model_id
    assert item["ai_model_used"] == model_id


# ---------------------------------------------------------------------------
# p37 task 11.5 — unit tests for C9: `_normalise_mapping_structure` (R3.1)
#
# Appended rather than interleaved, because task 11.5 is **insertions only**:
# nothing above this line is amended. `pytest` and `copy` are imported here for
# the same reason (`E402` is ignored repo-wide for the Lambda `sys.path`
# pattern, and this module already imports `lambda_function` after executable
# statements).
#
# Why these tests live in *this* file rather than a new one: C9 is the change
# that decides which of two `generated_by` values a run reports. The guard
# inside `_default_value_or_none` exists so that an unusable **target schema**
# cannot raise on the Bedrock success path, be caught at
# `generate_mapping_with_bedrock:840-843`, and silently downgrade an honest
# model mapping into the substring fallback — which is exactly the provenance
# rule (P28 R3.1) the rest of this module fences. `LGR-78ce0b7-13` /
# `LGR-b2f79ef-13` (**template / ratified**, the same decision under two commit
# hashes) is why that guard names `AttributeError`/`TypeError` rather than
# `Exception`: a classified `StepFailure` must never be re-labelled here.
# ---------------------------------------------------------------------------

import copy

import pytest

#: Sentinel for "the ``source_field`` key is absent altogether", which is the
#: shape the observed run's `phone` entry actually had.
_ABSENT = object()

#: Every form of `source_field` that `_has_usable_source_field` rejects
#: (`lambda_function.py:1011-1029` — `isinstance(x, str) and x.strip()`).
_UNUSABLE_SOURCE_FIELDS = [
    pytest.param(_ABSENT, id="key-absent"),
    pytest.param(None, id="none"),
    pytest.param("", id="empty-string"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param(42, id="non-string-int"),
    pytest.param(["mobile"], id="non-string-list"),
]


def _observed_source_schema():
    """The observed run's source CSV — `mobile`, and no `phone`.

    Pipeline `newtest-ab2a08a0`, run `run-2026-08-09-15-38-19-5c5f5f8a`.
    """
    return {
        "properties": {
            name: {"type": "string"}
            for name in ("cust_id", "fname", "lname", "email", "mobile", "created_date", "state", "premium_member")
        }
    }


def _observed_target_schema():
    """The observed run's target schema — eight fields, one of them `phone`.

    `phone` is `{"type": "string"}`, so `get_default_value` answers `""` for it
    (`lambda_function.py:961-974`): no `status` and no `date` in the name, so it
    falls through to the type default.
    """
    return {
        "properties": {
            "cust_id": {"type": "string"},
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
            "email": {"type": "string"},
            "phone": {"type": "string"},
            "created_date": {"type": "string"},
            "state": {"type": "string"},
            "premium_member": {"type": "boolean"},
        }
    }


def _unsourced_bedrock_result(source_field=_ABSENT, *, unmapped_target=None, generated_by="us.test-model"):
    """A Bedrock-shaped mapping result: seven sourced entries plus `phone`.

    `phone` sits in the middle of `mappings` so a move has an order to preserve.
    """
    phone = {"target_field": "phone", "transformation": "direct", "confidence": 0.4}
    if source_field is not _ABSENT:
        phone["source_field"] = source_field

    mappings = [
        {"target_field": "cust_id", "source_field": "cust_id", "transformation": "direct", "confidence": 0.99},
        {"target_field": "first_name", "source_field": "fname", "transformation": "direct", "confidence": 0.93},
        {"target_field": "last_name", "source_field": "lname", "transformation": "direct", "confidence": 0.93},
        {"target_field": "email", "source_field": "email", "transformation": "direct", "confidence": 0.99},
        phone,
        {
            "target_field": "created_date",
            "source_field": "created_date",
            "transformation": "direct",
            "confidence": 0.97,
        },
        {"target_field": "state", "source_field": "state", "transformation": "direct", "confidence": 0.95},
        {
            "target_field": "premium_member",
            "source_field": "premium_member",
            "transformation": "direct",
            "confidence": 0.9,
        },
    ]
    return {
        "mappings": mappings,
        "unmapped_source_fields": ["mobile"],
        "unmapped_target_fields": [] if unmapped_target is None else unmapped_target,
        "mapping_summary": "eight target fields",
        "generated_by": generated_by,
    }


def _names(entries, key):
    """Target-field names carried by a list, tolerating both element shapes.

    A deliberately independent oracle: the union assertions must not be
    computed with the production name extractors they are checking.
    """
    out = []
    for entry in entries:
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            value = entry.get(key, "")
            name = value.strip() if isinstance(value, str) else ""
        else:
            name = ""
        if name:
            out.append(name)
    return out


def _target_name_union(result):
    return set(_names(result["mappings"], "target_field")) | set(_names(result["unmapped_target_fields"], "field"))


# --- R3.1: an unsourced entry ends up in unmapped_target_fields ---


@pytest.mark.parametrize("source_field", _UNUSABLE_SOURCE_FIELDS)
def test_unsourced_entry_moves_to_unmapped_target_fields(source_field):
    """Every unusable `source_field` form moves the entry out of `mappings`.

    `_has_usable_source_field` tests `isinstance(x, str) and x.strip()`, not
    truthiness on an arbitrary object, because a mapping record is parsed JSON.
    All six forms below are therefore unsourced and all six must move.
    """
    result = _mod._normalise_mapping_structure(_unsourced_bedrock_result(source_field), _observed_target_schema())

    assert "phone" not in _names(result["mappings"], "target_field")
    assert len(result["mappings"]) == 7

    moved = [e for e in result["unmapped_target_fields"] if e["field"] == "phone"]
    assert len(moved) == 1
    assert moved[0] == {"field": "phone", "default_value": "", "reason": _mod.UNSOURCED_MAPPING_REASON}
    assert set(moved[0]) == {"field", "default_value", "reason"}


@pytest.mark.parametrize("source_field", ["mobile", "  mobile  "])
def test_usable_source_field_is_left_in_mappings(source_field):
    """A `source_field` that survives `.strip()` is usable, so nothing moves.

    The contrast case for the parametrisation above, and the identity form of
    the no-op: with nothing to move, the function returns its argument object.
    """
    mapping_result = _unsourced_bedrock_result(source_field)
    result = _mod._normalise_mapping_structure(mapping_result, _observed_target_schema())

    assert result is mapping_result
    assert "phone" in _names(result["mappings"], "target_field")
    assert result["unmapped_target_fields"] == []


def test_bedrock_success_moves_unsourced_entry_and_keeps_model_provenance():
    """End to end: the observed run's shape, through `generate_mapping_with_bedrock`.

    The model returns eight `mappings` entries, one of them naming no source.
    After normalisation `mappings` holds 7 and `unmapped_target_fields` names
    `phone` — and `generated_by` is still the model id, because normalising is
    not falling back.
    """
    body = _unsourced_bedrock_result()
    del body["generated_by"]  # the model does not send it; the handler stamps it

    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": json.dumps(body)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
        }
        result = _mod.generate_mapping_with_bedrock(
            _observed_source_schema(), _observed_target_schema(), "", "us.test-model"
        )

    assert len(result["mappings"]) == 7
    assert "phone" not in _names(result["mappings"], "target_field")
    assert {"field": "phone", "default_value": "", "reason": _mod.UNSOURCED_MAPPING_REASON} in result[
        "unmapped_target_fields"
    ]
    assert result["generated_by"] == "us.test-model"
    assert result["generated_by"] != _mod.FALLBACK_GENERATED_BY


def test_unsourced_reason_is_distinct_from_the_fallback_reason():
    """The two reasons record different findings and must not be one string.

    `generate_simple_mapping` writes "No matching source field found" when
    name-similarity matching looked and found nothing. `UNSOURCED_MAPPING_REASON`
    records that a generated mapping claimed the field without naming a source.
    The fallback's literal is read out of a real fallback run rather than
    restated here, so this stays true if that string moves.
    """
    fallback = _mod.generate_simple_mapping(_source_schema(), {"properties": {"phone": {"type": "string"}}})

    fallback_reasons = {e["reason"] for e in fallback["unmapped_target_fields"]}
    assert fallback_reasons  # the fallback found no source for `phone`
    assert _mod.UNSOURCED_MAPPING_REASON not in fallback_reasons


# --- R3.1: no field name is lost from the union ---


def test_no_target_field_name_is_lost_from_the_union():
    """The union of both lists' target-field names is unchanged by normalising.

    This is the half that stops a normaliser from silently dropping a field
    name — the failure mode that would look like a fix.
    """
    mapping_result = _unsourced_bedrock_result(
        unmapped_target=["nickname", {"field": "loyalty_tier", "default_value": None, "reason": "absent in source"}]
    )
    before = _target_name_union(mapping_result)

    result = _mod._normalise_mapping_structure(mapping_result, _observed_target_schema())

    assert _target_name_union(result) == before
    assert "phone" in _target_name_union(result)


def test_entry_naming_no_target_field_stays_in_mappings():
    """An entry that names no target field is deliberately left where it is.

    A bare string, a dict with no `target_field` and a blank `target_field` all
    contribute no name to either list, so moving one could only invent an empty
    name. Nothing moves, so the argument object comes back.
    """
    mapping_result = {
        "mappings": ["phone", {"source_field": "mobile"}, {"target_field": "   ", "source_field": None}],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "generated_by": "us.test-model",
    }
    result = _mod._normalise_mapping_structure(mapping_result, _observed_target_schema())

    assert result is mapping_result
    assert len(result["mappings"]) == 3
    assert result["unmapped_target_fields"] == []


def test_already_named_target_field_is_removed_but_not_appended_twice():
    """A name `unmapped_target_fields` already carries is not duplicated.

    A duplicate would inflate `len(unmapped_target_fields)`, which is a
    numerator in `determine_approval_requirement`'s 20% ratio rule — so a
    "harmless" double-append would move an approval decision.
    """
    pre_existing = {"field": "phone", "default_value": "unknown", "reason": "operator note"}
    mapping_result = _unsourced_bedrock_result(unmapped_target=[pre_existing])

    result = _mod._normalise_mapping_structure(mapping_result, _observed_target_schema())

    assert "phone" not in _names(result["mappings"], "target_field")
    assert len(result["mappings"]) == 7
    # Exactly one `phone` entry, and it is the pre-existing one — its reason is
    # not overwritten with UNSOURCED_MAPPING_REASON.
    assert result["unmapped_target_fields"] == [pre_existing]


def test_already_named_target_field_as_a_bare_string_is_not_appended_twice():
    """The same non-move when the existing entry is a bare string.

    `unmapped_target_fields` has two producers that do not agree on the element
    shape, so the dedup has to read both.
    """
    mapping_result = _unsourced_bedrock_result(unmapped_target=["phone"])

    result = _mod._normalise_mapping_structure(mapping_result, _observed_target_schema())

    assert "phone" not in _names(result["mappings"], "target_field")
    assert result["unmapped_target_fields"] == ["phone"]


# --- R3.1: applied to a generate_simple_mapping result it is a no-op ---


def test_normalisation_of_a_matched_fallback_result_is_the_identity():
    """Re-normalising the fallback's output returns the same object.

    `generate_simple_mapping` never puts a source-less entry in `mappings`, so
    there is nothing to move and the function returns its argument. Identity is
    the stronger claim than equality and is what the implementation actually
    guarantees.
    """
    fallback = _mod.generate_simple_mapping(_source_schema(), _target_schema())

    assert _mod._normalise_mapping_structure(fallback, _target_schema()) is fallback
    # Also with the target schema omitted — the default-value derivation is the
    # only thing that reads it, and on a no-op it is never reached.
    assert _mod._normalise_mapping_structure(fallback) is fallback


def test_normalisation_of_a_fallback_result_with_unmapped_targets_is_the_identity():
    """The no-op holds on the non-trivial fallback shape too.

    Here the fallback found no source for `phone` and already routed it to
    `unmapped_target_fields` with its own reason — the structure C9 exists to
    make the Bedrock path produce. Re-normalising must change nothing, and must
    not re-stamp that entry's reason.
    """
    target = {"properties": {"email": {"type": "string"}, "phone": {"type": "string"}}}
    fallback = _mod.generate_simple_mapping(_source_schema(), target)

    assert _names(fallback["unmapped_target_fields"], "field") == ["phone"]
    assert _mod._normalise_mapping_structure(fallback, target) is fallback


# --- Provenance: normalising never changes who generated the mapping ---


def test_generated_by_is_unchanged_by_normalisation():
    """Moving an entry does not touch `generated_by`, on either path.

    Normalising is a structural change, not a fallback. It also adds no
    `requires_manual_approval` flag — forcing a human on an unsourced *required*
    field is C10's job, and C9 must not quietly do half of it.
    """
    ai = _mod._normalise_mapping_structure(_unsourced_bedrock_result(), _observed_target_schema())
    assert ai["generated_by"] == "us.test-model"
    assert ai["generated_by"] != _mod.FALLBACK_GENERATED_BY
    assert "requires_manual_approval" not in ai

    fallback_shaped = _unsourced_bedrock_result(generated_by=_mod.FALLBACK_GENERATED_BY)
    fallback_shaped["requires_manual_approval"] = True
    normalised = _mod._normalise_mapping_structure(fallback_shaped, _observed_target_schema())
    assert normalised["generated_by"] == _mod.FALLBACK_GENERATED_BY
    assert normalised["requires_manual_approval"] is True


@pytest.mark.parametrize(
    "broken_target_schema",
    [
        pytest.param({"properties": ["phone", "email"]}, id="properties-is-a-list"),
        pytest.param({"properties": {"phone": "string"}}, id="field-entry-is-a-string"),
    ],
)
def test_unusable_target_schema_does_not_downgrade_bedrock_provenance(broken_target_schema):
    """An unusable target schema degrades `default_value`, never `generated_by`.

    A target schema is an arbitrary uploaded JSON document, so `properties` can
    be a list and a field's entry can be a string — either makes
    `get_default_value` raise `AttributeError` on its `.get` chain. Without
    `_default_value_or_none`'s guard that raise would land in
    `generate_mapping_with_bedrock`'s outer `except Exception` and return the
    substring fallback with `generated_by` re-reported as
    `fallback_substring_match` — an honest model mapping reported as a fallback,
    which P28 R3.1's provenance rule forbids.

    The guard names `AttributeError`/`TypeError` rather than `Exception` so a
    classified `StepFailure` cannot be re-labelled here — do-NOT 9,
    `LGR-78ce0b7-13` / `LGR-b2f79ef-13` (**template / ratified**).
    """
    body = _unsourced_bedrock_result()
    del body["generated_by"]

    with patch.object(_mod, "bedrock_runtime") as mock_bedrock, patch.object(_mod, "logger") as mock_logger:
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": json.dumps(body)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
        }
        result = _mod.generate_mapping_with_bedrock(
            _observed_source_schema(), broken_target_schema, "", "us.test-model"
        )

    # Provenance stays honest.
    assert result["generated_by"] == "us.test-model"
    assert result["generated_by"] != _mod.FALLBACK_GENERATED_BY
    assert not result.get("requires_manual_approval")
    # The structural move still happened; only the default value degraded.
    assert {"field": "phone", "default_value": None, "reason": _mod.UNSOURCED_MAPPING_REASON} in result[
        "unmapped_target_fields"
    ]
    # And it said so rather than degrading silently.
    assert any("phone" in str(call) for call in mock_logger.warning.call_args_list)


def test_non_dict_target_schema_degrades_without_warning_or_raise():
    """`None` (and any non-dict) target schema answers `None` with no warning.

    There is nothing anomalous to report — the caller simply did not supply a
    schema — so the warning is reserved for a schema that *is* a dict and still
    cannot be read.
    """
    with patch.object(_mod, "logger") as mock_logger:
        result = _mod._normalise_mapping_structure(_unsourced_bedrock_result(), None)

    assert {"field": "phone", "default_value": None, "reason": _mod.UNSOURCED_MAPPING_REASON} in result[
        "unmapped_target_fields"
    ]
    mock_logger.warning.assert_not_called()


# --- The function does not mutate its argument ---


def test_normalisation_does_not_mutate_its_argument():
    """The caller's dict — and both of its lists — come back untouched.

    `generate_mapping_with_bedrock` stamps `generated_by`, `_token_usage`,
    `_prompt_text` and `_response_text` onto the parsed body before normalising,
    so an in-place edit here would rewrite a record other code still holds.
    """
    mapping_result = _unsourced_bedrock_result(unmapped_target=["nickname"])
    before = copy.deepcopy(mapping_result)

    result = _mod._normalise_mapping_structure(mapping_result, _observed_target_schema())

    assert mapping_result == before
    assert result is not mapping_result
    assert len(mapping_result["mappings"]) == 8
    assert mapping_result["unmapped_target_fields"] == ["nickname"]
