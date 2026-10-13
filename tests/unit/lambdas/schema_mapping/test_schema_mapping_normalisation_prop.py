# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 3 — normalisation is idempotent and information-preserving.

p37 C9's `_normalise_mapping_structure`
(`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1062-1141`)
moves every `mappings` entry with no usable `source_field` into
`unmapped_target_fields`, so the Bedrock path produces the structure the
name-similarity fallback already produces (`:884-890`). That is how `phone`
became a mapping on pipeline `newtest-ab2a08a0` / run
`run-2026-08-09-15-38-19-5c5f5f8a`.

**Why both halves, and why the second one is the load-bearing one.**
Idempotence alone is satisfied by a normaliser that deletes every unsourced
entry outright — `f(f(x)) == f(x)` holds trivially once the entries are gone.
That failure mode *looks* like a fix: `mappings` no longer claims a field it
cannot source, `determine_approval_requirement`'s unmapped-target ratio drops,
and the run auto-approves with the field silently absent from both lists.
The information-preserving half is what forbids it: the union of `mappings`'
target-field names with `unmapped_target_fields`' field names must be the same
set before and after.

**Expectations derive from requirement 3.1 and design C9, not from running the
function.** C9 states the rule as "preserving order and dropping nothing" and
"applied to the fallback's output it is a no-op". The three test functions below
are those two sentences plus the two shapes C9 deliberately leaves in place.

**Non-vacuity, three ways, because a normaliser that did nothing at all would
otherwise pass every assertion here.**

1. `test_normalisation_moves_every_unsourced_entry_and_nothing_else` draws from
   a generator that *guarantees* at least one movable entry, so the moving path
   is exercised on every example rather than probabilistically, and asserts the
   moved entry's exact shape — `{field, default_value, reason}` with `reason ==
   UNSOURCED_MAPPING_REASON`.
2. The wide property asserts the count directly: for an input the rule says must
   shed entries, `len(result["mappings"])` must be exactly that many shorter,
   and for an input it says must not, the function must return **its argument
   object itself**. `return mapping_result` fails the first; a normaliser that
   rebuilt the dict unconditionally fails the second.
3. The predicate the assertions compare against (`_must_leave`) is written from
   C9's rule rather than by calling the production helpers, so a change to
   `_has_usable_source_field` or `_mapping_target_field_name` cannot drag the
   test's expectation along with it.

Two behaviours C9 documents are asserted rather than contradicted, because both
are deliberate:

- an entry naming **no** target field (a bare string, a dict with no
  `target_field`, a blank one) stays in `mappings` — moving it could only invent
  an empty name;
- an entry whose target field `unmapped_target_fields` **already** names is
  removed from `mappings` but **not appended a second time**, because a
  duplicate would inflate `len(unmapped_target_fields)`, a numerator in the 20%
  ratio rule (`:1304-1306`). The union is unchanged either way, which is why the
  information-preserving half is stated over sets and not over lengths.

Function untouched — this file adds coverage and changes no production code.

**Validates: Requirements 3.1**
"""

import copy
import os
import sys
from typing import Any
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1, template /
# ratified); these are no-ops kept for parity with the sibling files here.
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

# Sentinel for "the key is absent entirely", which is a different input from
# `source_field: None` and is what an unvalidated Bedrock response produces.
_ABSENT = object()

# Small pool so duplicate names arise across the two lists naturally rather than
# having to be constructed. `status` and `created_date` are in it on purpose:
# they are the two special arms of `get_default_value` (`:969-972`), which
# `_default_value_or_none` calls for every moved entry.
_FIELD_NAMES = ("phone", "email", "postal_code", "status", "created_date", "mobile")

# Every way a `source_field` can fail the str/strip test at `:1028`, plus the two
# ways it can pass. Whitespace padding is included on the passing side because
# `.strip()` is what decides.
_UNUSABLE_SOURCE_FIELDS = (_ABSENT, None, "", "   ", "\t", "\n ", 7, 0.5, True, ["src"], {}, {"name": "src"})
_USABLE_SOURCE_FIELDS = ("src_col", "  src_col  ", "a")


# --- The rule, restated from design C9 rather than read off the code ----------


def _target_names(mappings: Any) -> set[str]:
    """Target-field names a `mappings` list contributes to the union.

    A non-list contributes nothing; so does an element that names no target
    field. Blank is not a name — that is the same judgement `:1008` makes and
    the reason C9 leaves those entries in place instead of moving them.
    """
    if not isinstance(mappings, list):
        return set()
    names = set()
    for entry in mappings:
        if isinstance(entry, dict):
            target_field = entry.get("target_field")
            if isinstance(target_field, str) and target_field.strip():
                names.add(target_field.strip())
    return names


def _unmapped_names(entries: Any) -> set[str]:
    """Field names an `unmapped_target_fields` list contributes to the union.

    Both element shapes its two producers write: a bare string, and a dict with a
    `field` key (`:884-890` and the mapping editor's recompute at
    `pipelines_mappings.py:248-252`).
    """
    if not isinstance(entries, list):
        return set()
    names = set()
    for entry in entries:
        if isinstance(entry, str):
            if entry.strip():
                names.add(entry.strip())
        elif isinstance(entry, dict):
            field = entry.get("field")
            if isinstance(field, str) and field.strip():
                names.add(field.strip())
    return names


def _union(mapping_result: dict) -> set[str]:
    """The set requirement 3.1 says normalisation must not change."""
    return _target_names(mapping_result.get("mappings")) | _unmapped_names(mapping_result.get("unmapped_target_fields"))


def _must_leave(entry: Any) -> str:
    """C9's rule for one entry: the target field it must surrender, or `""`.

    Non-empty exactly when the entry names a target field and has no usable
    source field. Written from design C9's sentence rather than by calling
    `_mapping_target_field_name` / `_has_usable_source_field`, so that a change
    to either helper cannot drag the test's expectation along with it.
    """
    if not isinstance(entry, dict):
        return ""
    target_field = entry.get("target_field")
    target_field = target_field.strip() if isinstance(target_field, str) else ""
    if not target_field:
        return ""
    source_field = entry.get("source_field")
    if isinstance(source_field, str) and source_field.strip():
        return ""
    return target_field


def _leaving_indices(mapping_result: dict) -> list[int] | None:
    """Indices of the `mappings` entries C9's rule says must leave the list.

    `None` means the input is one the function must return exactly as it arrived
    — a `mappings` or `unmapped_target_fields` that is not a list (`:1107`).
    """
    mappings = mapping_result.get("mappings")
    raw_unmapped = mapping_result.get("unmapped_target_fields") or []
    if not isinstance(mappings, list) or not isinstance(raw_unmapped, list):
        return None
    return [index for index, entry in enumerate(mappings) if _must_leave(entry)]


def _newly_named(mapping_result: dict) -> list[str]:
    """The names appended to `unmapped_target_fields`, in `mappings` order.

    A surrendered name the list **already** carries is removed from `mappings`
    without being appended a second time, because a duplicate would inflate
    `len(unmapped_target_fields)` — a numerator in the 20% ratio rule.
    """
    already = _unmapped_names(mapping_result.get("unmapped_target_fields") or [])
    newly: list[str] = []
    for entry in mapping_result.get("mappings") or []:
        name = _must_leave(entry)
        if name and name not in already and name not in newly:
            newly.append(name)
    return newly


def _expected_kept(mapping_result: dict) -> list[Any]:
    """The `mappings` list after normalisation: the originals, in their order."""
    indices = set(_leaving_indices(mapping_result) or [])
    return [entry for index, entry in enumerate(mapping_result["mappings"]) if index not in indices]


# --- Generators ---------------------------------------------------------------


@st.composite
def _mapping_entry(draw: st.DrawFn) -> Any:
    """One `mappings` element, over the whole shape space model output can take.

    `mappings` is parsed model JSON (`:806-826`), so an element can be a bare
    string or a number as easily as a dict, and a dict's `target_field` can be
    absent, `None`, blank or non-string.
    """
    shape = draw(st.sampled_from(["dict", "dict", "dict", "dict", "bare_string", "non_dict"]))
    if shape == "bare_string":
        return draw(st.sampled_from([*_FIELD_NAMES, "", "   "]))
    if shape == "non_dict":
        return draw(st.sampled_from([None, 7, ["phone"], True]))

    entry: dict[str, Any] = {}
    target_field = draw(st.sampled_from([*_FIELD_NAMES, "  phone  ", "", "   ", None, 7, _ABSENT]))
    if target_field is not _ABSENT:
        entry["target_field"] = target_field
    source_field = draw(st.sampled_from([*_UNUSABLE_SOURCE_FIELDS, *_USABLE_SOURCE_FIELDS]))
    if source_field is not _ABSENT:
        entry["source_field"] = source_field
    # Fields carried alongside, so the assertion that a kept entry is untouched
    # has something to be untouched about.
    if draw(st.booleans()):
        entry["confidence"] = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
    if draw(st.booleans()):
        entry["transformation"] = "direct"
    return entry


@st.composite
def _unmapped_entry(draw: st.DrawFn) -> Any:
    """One `unmapped_target_fields` element, in both producers' shapes."""
    shape = draw(st.sampled_from(["bare_string", "dict", "dict", "degenerate"]))
    if shape == "bare_string":
        return draw(st.sampled_from([*_FIELD_NAMES, "  phone  ", "", "   "]))
    if shape == "degenerate":
        return draw(st.sampled_from([None, 7, [], {}]))
    entry: dict[str, Any] = {}
    field = draw(st.sampled_from([*_FIELD_NAMES, "  email  ", "", None, 7, _ABSENT]))
    if field is not _ABSENT:
        entry["field"] = field
    if draw(st.booleans()):
        entry["default_value"] = draw(st.sampled_from([None, "", 0, "active"]))
    if draw(st.booleans()):
        entry["reason"] = "No matching source field found"
    return entry


@st.composite
def _mapping_result(draw: st.DrawFn) -> dict:
    """A whole mapping result, including the shapes that must round-trip untouched.

    Both keys are drawn across list / non-list / `None`-under-a-present-key /
    absent, and the empty dict is in the space, because `:1105-1108` returns the
    argument unchanged for anything it cannot read and that arm needs to be
    exercised rather than assumed.
    """
    shape = draw(st.sampled_from(["normal", "normal", "normal", "normal", "degenerate"]))
    if shape == "degenerate":
        return draw(
            st.sampled_from(
                [
                    {},
                    {"mappings": None, "unmapped_target_fields": None},
                    {"mappings": "phone", "unmapped_target_fields": []},
                    {"mappings": {"target_field": "phone"}, "unmapped_target_fields": []},
                    {"mappings": 7},
                    {"unmapped_target_fields": ["phone"]},
                    {"mappings": [{"target_field": "phone"}], "unmapped_target_fields": "phone"},
                    {"mappings": [{"target_field": "phone"}], "unmapped_target_fields": {"field": "phone"}},
                    # Falsy non-list: `or []` at `:1106` reads it as empty, so
                    # this one IS normalisable and the entry moves.
                    {"mappings": [{"target_field": "phone"}], "unmapped_target_fields": ""},
                    {"mappings": [{"target_field": "phone"}], "unmapped_target_fields": None},
                ]
            )
        )

    result: dict[str, Any] = {
        "mappings": draw(st.lists(_mapping_entry(), max_size=6)),
        "unmapped_target_fields": draw(st.lists(_unmapped_entry(), max_size=4)),
    }
    if draw(st.booleans()):
        result["unmapped_source_fields"] = draw(st.lists(st.sampled_from(["a", "b"]), max_size=2))
    if draw(st.booleans()):
        result["generated_by"] = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    if draw(st.booleans()):
        result["mapping_summary"] = "generated"
    return result


@st.composite
def _mapping_result_with_a_movable_entry(draw: st.DrawFn) -> dict:
    """A mapping result guaranteed to contain at least one entry that must move.

    This is the non-vacuity generator: every example reaches the moving path, so
    a normaliser that returned its argument unchanged fails on every example
    rather than on a lucky one.
    """
    result = draw(_mapping_result())
    if not isinstance(result.get("mappings"), list):
        result = dict(result)
        result["mappings"] = []
    if not isinstance(result.get("unmapped_target_fields"), list):
        result = dict(result)
        result["unmapped_target_fields"] = []

    movable: dict[str, Any] = {"target_field": draw(st.sampled_from([*_FIELD_NAMES, "  phone  ", "tax_id"]))}
    unusable = draw(st.sampled_from(_UNUSABLE_SOURCE_FIELDS))
    if unusable is not _ABSENT:
        movable["source_field"] = unusable
    if draw(st.booleans()):
        movable["confidence"] = 0.31
    mappings = list(result["mappings"])
    mappings.insert(draw(st.integers(min_value=0, max_value=len(mappings))), movable)
    result = dict(result)
    result["mappings"] = mappings
    return result


def _target_schemas() -> st.SearchStrategy[Any]:
    """Target schemas including the shapes that make `get_default_value` raise.

    `properties` as a list and a field entry as a string both raise
    `AttributeError` on `get_default_value`'s `.get` chain (`:963-964`);
    `_default_value_or_none` (`:1031-1059`) is what keeps that from propagating
    into `generate_mapping_with_bedrock`'s outer `except Exception` and
    downgrading an honest model mapping to the substring fallback.
    """
    well_formed = {
        "type": "object",
        "properties": {name: {"type": "string"} for name in _FIELD_NAMES},
        "required": ["phone"],
    }
    return st.sampled_from(
        [
            None,
            well_formed,
            {"type": "object", "properties": {}},
            {"properties": list(_FIELD_NAMES)},
            {"properties": {name: "string" for name in _FIELD_NAMES}},
            {},
            "not a schema",
            7,
        ]
    )


# --- Property 3, half (i) and half (ii), over the whole shape space -----------


# Feature: p37-zero-row-success, Property 3: Normalisation is idempotent and information-preserving
@settings(max_examples=200, deadline=None)
@given(mapping_result=_mapping_result(), target_schema=_target_schemas())
def test_normalisation_is_idempotent_and_information_preserving(mapping_result, target_schema):
    """R3.1: `f(f(x)) == f(x)`, and the union of field names is unchanged by `f`.

    The count assertions at the end are what keep the two halves from being
    jointly satisfiable by a no-op: an input the rule says must shed entries
    must shed exactly that many, and an input it says must not must come back as
    the **same object**.
    """
    before = copy.deepcopy(mapping_result)
    indices = _leaving_indices(mapping_result)

    once = _mod._normalise_mapping_structure(mapping_result, target_schema)
    twice = _mod._normalise_mapping_structure(once, target_schema)

    # (i) Idempotence.
    assert twice == once

    # (ii) Information-preserving: not one field name enters or leaves the union.
    assert _union(once) == _union(before)

    # The function does not mutate its argument — the Bedrock path hands it a
    # dict it has already stamped `generated_by` and `_token_usage` onto (`:816-820`).
    assert mapping_result == before

    if indices is None or not indices:
        # Nothing to move, or nothing readable: the argument object itself.
        assert once is mapping_result
        return

    # Order-preserving, and nothing kept that should have gone: the surviving
    # `mappings` list is the originals minus exactly the surrendering entries.
    assert once["mappings"] == _expected_kept(before)
    assert len(once["mappings"]) == len(before["mappings"]) - len(indices)

    newly = _newly_named(before)
    appended = once["unmapped_target_fields"][len(before.get("unmapped_target_fields") or []) :]
    assert [entry["field"] for entry in appended] == newly, "appended in the order they appeared in `mappings`"
    for entry in appended:
        assert set(entry) == {"field", "default_value", "reason"}
        assert entry["reason"] == _mod.UNSOURCED_MAPPING_REASON
        if target_schema is None:
            assert entry["default_value"] is None
    # An already-named field is removed WITHOUT being appended, so the list
    # cannot be inflated past the number of genuinely new names.
    assert len(appended) == len(newly)


# --- Non-vacuity: the moving path, on every example --------------------------


# Feature: p37-zero-row-success, Property 3: Normalisation is idempotent and information-preserving
@settings(max_examples=200, deadline=None)
@given(mapping_result=_mapping_result_with_a_movable_entry(), target_schema=_target_schemas())
def test_normalisation_moves_every_unsourced_entry_and_nothing_else(mapping_result, target_schema):
    """R3.1: the entry leaves `mappings` and arrives in the other list, in shape.

    Every drawn input carries at least one unsourced entry naming a target
    field, so this is the assertion a normaliser that did nothing at all fails.
    """
    before = copy.deepcopy(mapping_result)
    indices = _leaving_indices(mapping_result)
    assert indices, "generator must produce a movable entry, or the property is vacuous"

    once = _mod._normalise_mapping_structure(mapping_result, target_schema)

    assert once is not mapping_result
    assert _union(once) == _union(before)
    assert len(once["mappings"]) == len(before["mappings"]) - len(indices)
    assert once["mappings"] == _expected_kept(before)

    # No entry left behind in `mappings` still claims a target field it cannot
    # source — the structural guarantee C9 exists to establish.
    assert _leaving_indices(once) == []

    appended = once["unmapped_target_fields"][len(before["unmapped_target_fields"]) :]
    assert [entry["field"] for entry in appended] == _newly_named(before)
    for entry in appended:
        assert set(entry) == {"field", "default_value", "reason"}
        assert entry["reason"] == _mod.UNSOURCED_MAPPING_REASON

    # Idempotent from here: a second pass has nothing left to move.
    assert _mod._normalise_mapping_structure(once, target_schema) is once


# --- Property 3's no-op half, on the path that already gets it right ---------


@st.composite
def _schema_pair(draw: st.DrawFn) -> tuple[dict, dict]:
    """Source and target schemas with a drawn overlap, for the fallback path.

    Names are drawn so `calculate_field_similarity` matches some target fields
    and leaves others unmatched, which is the case that exercises
    `generate_simple_mapping`'s `unmapped_target_fields` branch (`:884-890`).
    """
    target_fields = draw(st.lists(st.sampled_from(_FIELD_NAMES), min_size=1, max_size=5, unique=True))
    matched = draw(st.lists(st.sampled_from(target_fields), max_size=len(target_fields), unique=True))
    extra = draw(st.lists(st.sampled_from(["order_ref", "legacy_col"]), max_size=2, unique=True))
    source_fields = [*matched, *extra]
    source_schema = {"type": "object", "properties": {name: {"type": "string"} for name in source_fields}}
    target_schema = {"type": "object", "properties": {name: {"type": "string"} for name in target_fields}}
    return source_schema, target_schema


# Feature: p37-zero-row-success, Property 3: Normalisation is idempotent and information-preserving
@settings(max_examples=150, deadline=None)
@given(schemas=_schema_pair())
def test_normalisation_is_a_no_op_on_the_fallback_paths_output(schemas):
    """R3.1: applied to `generate_simple_mapping`'s output the normaliser is inert.

    C9's claim is that this is not a new rule — it is the fallback path's own
    rule, applied to both returns. The evidence is that the fallback's output is
    already a fixed point: the same object comes back, no entry carries
    `UNSOURCED_MAPPING_REASON`, and the union is untouched. Real output from the
    real function, not a hand-built fixture, so a future change to the fallback
    that put a source-less entry in `mappings` would fail here.
    """
    source_schema, target_schema = schemas
    fallback = _mod.generate_simple_mapping(source_schema, target_schema)

    renormalised = _mod._normalise_mapping_structure(fallback, target_schema)

    assert renormalised is fallback
    assert _union(renormalised) == _union(fallback)
    assert _leaving_indices(fallback) == [], "the fallback never puts a source-less entry in `mappings`"
    assert all(
        entry.get("reason") != _mod.UNSOURCED_MAPPING_REASON
        for entry in fallback["unmapped_target_fields"]
        if isinstance(entry, dict)
    )
    # Every target field is still accounted for exactly once across the two
    # lists — the union half stated against the schema the mapping came from.
    assert _union(fallback) == set(target_schema["properties"])
