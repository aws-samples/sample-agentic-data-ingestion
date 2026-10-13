# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""T-04, generated — one bad entry is enough to require a human.

The rule (the example tests in `test_approval_requires_corroboration.py` state it
in full): `schema_mapping.determine_approval_requirement` may auto-approve only
when EVERY `mappings` entry has (a) a confidence that is a real, finite number in
`[0, 1]` and (b) deterministic corroboration — a usable `source_field` that is one
of the source schema's columns and whose name agrees with its `target_field` after
normalisation, or whose exact pair is an operator `mapping_hints` entry.

Two properties, both derived from that rule rather than from running the function:

1. **Any set containing one bad entry requires a human, whatever the others say.**
   The other entries' confidences are drawn from the whole range a model can
   emit — in-range, huge, `NaN`, `±inf` — because "regardless of the others'
   confidence" is the claim, and the huge values are precisely the ones that
   used to lift an average past the threshold.
2. **That one entry is the reason.** The same corroborated, in-range,
   above-threshold neighbours auto-approve on their own and require a human the
   moment the bad entry is added. Without this half, property 1 would be
   satisfied by a function that always returns `True`.

Both assert `logger.error` was never reached, so a `True` from the trailing
`except Exception: return True` cannot pass as a verdict.
"""

import os
import sys
from unittest.mock import MagicMock, patch

from hypothesis import assume, given, settings
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

_THRESHOLD = _mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD

#: Identifier-allowlist-conforming names only, so the Non_Conforming_Column_Name
#: clause (which would issue a DynamoDB lookup) is never what this file reaches.
_IDENTIFIER = st.from_regex(r"[a-z][a-z0-9_]{0,11}", fullmatch=True)


def _normalised(name: str) -> str:
    """The rule's normalisation, restated from its wording rather than imported."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


# --- Generators ---------------------------------------------------------------


#: Every confidence a model's JSON can carry: in range, far out of range (the
#: values that lift an average), and the non-finite tokens `json.loads` accepts.
_ANY_CONFIDENCE = st.one_of(
    st.floats(min_value=0.0, max_value=1.0),
    st.floats(min_value=1.0, max_value=1e9),
    st.floats(min_value=-1e9, max_value=0.0),
    st.just(float("nan")),
    st.just(float("inf")),
    st.just(float("-inf")),
)

_INVALID_CONFIDENCE = st.one_of(
    st.floats(min_value=1.0, max_value=1e9, exclude_min=True),
    st.floats(min_value=-1e9, max_value=0.0, exclude_max=True),
    st.just(float("nan")),
    st.just(float("inf")),
    st.just(float("-inf")),
    st.booleans(),
    st.none(),
)


@st.composite
def _neighbours(draw, confidence: st.SearchStrategy) -> list[dict]:
    """1–8 exact-name entries on distinct columns `col_0 … col_n`."""
    count = draw(st.integers(min_value=1, max_value=8))
    return [
        {"source_field": f"col_{i}", "target_field": f"col_{i}", "confidence": draw(confidence)} for i in range(count)
    ]


@st.composite
def _bad_entry(draw) -> tuple[dict, str, list[str]]:
    """One entry the rule rejects, the kind of rejection, and the columns to supply.

    The columns returned are the ones the bad entry needs to be rejected for the
    stated reason ONLY: for every kind except `not_a_column` its own source is a
    column, so membership is not what rejects it.
    """
    kind = draw(st.sampled_from(["invalid_confidence", "names_disagree", "not_a_column", "unusable_source"]))

    if kind == "invalid_confidence":
        name = draw(_IDENTIFIER.filter(lambda n: not n.startswith("col_")))
        entry = {"source_field": name, "target_field": name}
        confidence = draw(_INVALID_CONFIDENCE)
        if confidence is not None:  # `None` stands for the key being absent
            entry["confidence"] = confidence
        return entry, kind, [name]

    if kind == "names_disagree":
        source = draw(_IDENTIFIER.filter(lambda n: not n.startswith("col_")))
        target = draw(_IDENTIFIER)
        assume(_normalised(source) != _normalised(target))
        return {"source_field": source, "target_field": target, "confidence": 0.99}, kind, [source]

    if kind == "not_a_column":
        name = draw(_IDENTIFIER.filter(lambda n: not n.startswith("col_")))
        return {"source_field": name, "target_field": name, "confidence": 0.99}, kind, []

    unusable = draw(st.sampled_from([None, "", "   ", 7, ["x"], {}]))
    return {"source_field": unusable, "target_field": "some_target", "confidence": 0.99}, kind, []


def _decide(mappings: list[dict], *, source_column_names: list[str] | None, hints: dict | None) -> bool:
    pipeline: dict = {
        "pipeline_id": "t04-prop",
        "approval_config": {"schema_mapping_enabled": True, "always_approve_schema_changes": True},
    }
    if hints is not None:
        pipeline["mapping_hints"] = hints
    mapping_result = {
        "mappings": mappings,
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }
    with (
        patch.object(_mod, "get_pipeline_metadata", return_value=pipeline) as mock_get_meta,
        patch.object(_mod, "logger") as mock_logger,
    ):
        decision = _mod.determine_approval_requirement(
            "t04-prop", mapping_result, None, source_column_names=source_column_names
        )
        assert mock_get_meta.called, "an early return, not a clause"
        mock_logger.error.assert_not_called()
    return decision


def _columns_for(neighbours: list[dict], bad_columns: list[str], *, supply: bool) -> list[str] | None:
    if not supply:
        return None
    return [m["source_field"] for m in neighbours] + bad_columns


def _hints_without(bad: dict, drawn: dict) -> dict:
    """Operator hints that may name other pairs but never the bad entry's own."""
    source = bad.get("source_field")
    if isinstance(source, str):
        drawn = {k: v for k, v in drawn.items() if k != source}
    return drawn


_HINTS = st.dictionaries(_IDENTIFIER, _IDENTIFIER, max_size=4)


# --- Property 1 ---------------------------------------------------------------


@settings(max_examples=10, deadline=None)
@given(
    neighbours=_neighbours(_ANY_CONFIDENCE),
    bad=_bad_entry(),
    position=st.integers(min_value=0, max_value=8),
    supply_columns=st.booleans(),
    hints=_HINTS,
)
def test_one_bad_entry_requires_a_human_whatever_the_others_confidence(
    neighbours, bad, position, supply_columns, hints
):
    entry, kind, bad_columns = bad
    # `not_a_column` is only a rejection when columns are supplied; with `None`
    # the rule skips membership by design, so that kind forces column supply.
    supply = supply_columns or kind == "not_a_column"
    mappings = list(neighbours)
    mappings.insert(min(position, len(mappings)), entry)

    decision = _decide(
        mappings,
        source_column_names=_columns_for(neighbours, bad_columns, supply=supply),
        hints=_hints_without(entry, hints),
    )

    assert decision is True, f"{kind}: {entry}"


# --- Property 2 ---------------------------------------------------------------


@settings(max_examples=10, deadline=None)
@given(
    # `+ 0.01`: the mean of n values each exactly at the threshold can round to
    # one ulp below it, and this half must not depend on float rounding.
    neighbours=_neighbours(st.floats(min_value=_THRESHOLD + 0.01, max_value=1.0)),
    bad=_bad_entry(),
    supply_columns=st.booleans(),
)
def test_the_bad_entry_is_what_turns_an_auto_approve_into_a_human_decision(neighbours, bad, supply_columns):
    entry, kind, bad_columns = bad
    supply = supply_columns or kind == "not_a_column"
    columns = _columns_for(neighbours, bad_columns, supply=supply)

    assert _decide(neighbours, source_column_names=columns, hints=None) is False, (
        "corroborated, in-range, above-threshold neighbours must auto-approve on their own"
    )
    assert _decide([*neighbours, entry], source_column_names=columns, hints=None) is True, f"{kind}: {entry}"
