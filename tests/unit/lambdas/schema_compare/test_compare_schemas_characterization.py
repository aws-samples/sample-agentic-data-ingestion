# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests: ``compare_schemas`` / ``deep_compare`` (p30 task 4.0).

These three symbols — ``compare_schemas``, its nested ``deep_compare``, and
``get_previous_schema`` (see ``test_previous_schema_selection.py``) — had **no
covering tests** before this file. p30 tasks 4.4 and 4.5 modify all of them, so
this is the safety net that must exist first (p30 design §5 O10).

**Two kinds of test live here, and the difference is the point.**

1. **Pinned** — behaviour that was already *correct* under p30 Requirement 8.
   These are the characterization value: task 4.4 must not break them, and
   several are guards against the specific ways 4.4's fix could go wrong.
2. **Written to the requirement** — behaviour required by **R8.1 / R8.2** that
   the code did **not** exhibit when this file was written. The output at the
   time *was* the defect (design §5 O10), so a test asserting it would have
   encoded the bug. These asserted the requirement instead, and carried
   ``xfail(strict=True)`` so that the moment task 4.4 landed they flipped from
   XFAIL to a hard suite failure (XPASS-under-strict) — 4.4's acceptance signal,
   forcing the stale marker to be deleted rather than lingering and hiding a
   later regression. **Task 4.4 has landed; the markers are deleted and the
   tests now pass as ordinary assertions.** (Repo precedent for the removal:
   ``tests/unit/test_module_sizes.py``.)

**The defect that was fixed (R8.1 / R8.2).** ``deep_compare``'s only
``type_mismatch`` branch was ``type(obj1) != type(obj2)`` — a *Python* type
check. Two JSON strings never trip it, so changing a field's declared ``type``
from ``"integer"`` to ``"string"`` was reported as ``value_changed`` and left
``is_compatible`` **true**: a breaking source change adapted itself into the
target table with no human decision. Task 4.4 added a positional predicate
threaded through the walk (see ``_child_role`` in the Lambda module).

**Why the schema fixtures below are literal dicts and not calls to the
extractor.** ``LGR-28b4d75-5`` (template-provenance, ratified) records that
shape drift between a producer and its consumer fails *silently*, and that a
test written from the producer's own output cannot detect it. The six emitting
paths of ``schema_extract`` were enumerated and their output captured verbatim
in p30 task 0.3; those literals are reproduced here as fixtures. If
``schema_extract`` changes shape, these tests keep asserting the shape
``compare_schemas`` was specified against, which is the only way the drift
becomes visible.

**Constraint C10 (``LGR-dd80c6d-1``, backfilled).** ``schema_compare`` /
``schema_mapping`` hashing must stay on ``hashlib.sha256``; the builtin
``hash()`` was replaced because it is randomized per process, which broke
cross-invocation drift detection. ``compare_schemas`` does no hashing, so the
one guard here asserts digest **stability across calls** rather than pinning a
literal digest (a pinned digest would be brittle, since the hash is taken over
``json.dumps(schema, sort_keys=True)`` and every shape difference changes it).

**Deliberately not asserted here**, because these were 4.4's own decisions and
it made them. They are covered in ``test_declared_type_mismatch.py`` instead, so
this file stays a characterization file:

- **Widening.** Task 0.3 found csv reads every column as ``string`` while json
  and parquet read the same data as ``integer``/``number``, and that sampling
  alone can widen ``integer`` → ``number``. Whether widening should stay
  compatible was 4.4's call and R8 does not decide it, so no test here fixes it.
  The cases below use R8.5's named example, ``integer`` → ``string``, which is
  unambiguously breaking either way.
- **Action routing.** R8.2's second half ("route to an action that requires
  human approval") is a ``lambda_handler`` concern and the action *name* was
  4.4's choice, so asserting a guessed string here would have been noise.

**Validates: p30 design §5 O10; R8.1, R8.2; R16.3**
"""

import copy
import importlib.util
import json
import os
import sys
import unittest.mock as mock

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_schema_compare",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_mods = {
    "metrics_facade": mock.MagicMock(PipelineMetrics=None),
}


def _load_module():
    """Load the schema_compare lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "schema_compare_lf_p30_characterization",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["schema_compare_lf_p30_characterization"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()


# ---------------------------------------------------------------------------
# The six emitted schema shapes, captured verbatim in p30 task 0.3 by calling
# each emitter directly. Literal fixtures on purpose — see LGR-28b4d75-5 in the
# module docstring.
# ---------------------------------------------------------------------------

# csv — extract_schema_from_csv → infer_schema_from_records. Flat, depth 1.
# Every column reads as `string`: both readers hand back strings.
CSV_SHAPE = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "score": {"type": "string"},
        "active": {"type": "string"},
    },
}

# json — extract_schema_from_json → infer_schema_from_records. Flat, depth 1:
# a nested object emits {"type": "object"} with NO `properties`, an array emits
# {"type": "array"} with NO `items`. Never `null` (all-null reads as `string`).
JSON_SHAPE = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "score": {"type": "number"},
        "active": {"type": "boolean"},
        "nested": {"type": "object"},
        "tags": {"type": "array"},
        "nothing": {"type": "string"},
    },
}

# parquet — extract_schema_from_parquet → arrow_schema_to_json_schema. Flat,
# depth 1. No Arrow spellings leak: int8..uint64 → integer, float/decimal →
# number, timestamp/date/binary/dictionary → string.
PARQUET_SHAPE = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "score": {"type": "number"},
        "created": {"type": "string"},
        "amount": {"type": "number"},
        "tags": {"type": "array"},
        "nested": {"type": "object"},
        "day": {"type": "string"},
        "blob": {"type": "string"},
    },
}

# jdbc — handle_jdbc_schema_extract → _glue_columns_to_json_schema. The ONLY
# path that emits a root `required` array, whose *values are field names*.
# Four type tokens only: array<…>/struct<…> collapse to `string`.
JDBC_SHAPE = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "tags": {"type": "string"},
        "nested": {"type": "string"},
    },
    "required": ["id", "tags", "nested"],
}

# avro / orc / any unknown source_format — falls through to the JSON reader,
# which parses binary as text and yields an empty-but-valid schema. 4.4/4.5
# will read this as "every field removed", not as an extraction failure.
UNKNOWN_FORMAT_SHAPE = {"type": "object", "properties": {}}

# legacy (no source_format) — extract_json_schema. Recursive, unbounded depth,
# and the only path that emits the `null` type token.
LEGACY_SHAPE = {
    "type": "object",
    "properties": {
        "nested": {
            "type": "object",
            "properties": {"deeper": {"type": "object", "properties": {"b": {"type": "number"}}}},
        },
        "rows": {"type": "array", "items": {"type": "object", "properties": {"c": {"type": "boolean"}}}},
        "scalars": {"type": "array", "items": {"type": "integer"}},
        "nothing": {"type": "null"},
    },
}

# legacy with a JSON *list* at the root — no root `properties` at all.
LEGACY_ARRAY_ROOTED_SHAPE = {
    "type": "array",
    "items": {"type": "object", "properties": {"a": {"type": "integer"}}},
}

ALL_EMITTED_SHAPES = {
    "csv": CSV_SHAPE,
    "json": JSON_SHAPE,
    "parquet": PARQUET_SHAPE,
    "jdbc": JDBC_SHAPE,
    "unknown_format": UNKNOWN_FORMAT_SHAPE,
    "legacy": LEGACY_SHAPE,
    "legacy_array_rooted": LEGACY_ARRAY_ROOTED_SHAPE,
}


def _types_of(result: dict) -> list[str]:
    """Diff kinds present in a comparison result, in order."""
    return [d["type"] for d in result["differences"]]


# ===========================================================================
# PINNED — behaviour already correct under R8; task 4.4 must not break it
# ===========================================================================


@pytest.mark.parametrize("shape_name", sorted(ALL_EMITTED_SHAPES))
def test_identical_schema_reports_no_differences(shape_name):
    """A schema compared with itself is identical, compatible, and diff-free.

    Run across all six emitting paths (seven shapes, counting the legacy
    array-rooted variant) because ``deep_compare`` walks whatever nesting the
    emitter produced — flat depth-1 maps, an unbounded recursive tree, and an
    ``array``-rooted document with no root ``properties``.

    **Validates: p30 design §5 O10**
    """
    shape = ALL_EMITTED_SHAPES[shape_name]
    result = _lf.compare_schemas(shape, copy.deepcopy(shape))

    assert result["is_identical"] is True
    assert result["is_compatible"] is True
    assert result["differences"] == []
    assert result["summary"] == {
        "fields_added": 0,
        "fields_removed": 0,
        "type_mismatches": 0,
        "value_changes": 0,
    }


def test_field_added_is_classified_as_field_added_and_stays_compatible():
    """A new column is additive: ``field_added``, and NOT a breaking change.

    R8.2 makes only ``type_mismatch`` and ``field_removed`` incompatible, so an
    added field must leave ``is_compatible`` true.

    **Validates: p30 design §5 O10**
    """
    after = copy.deepcopy(CSV_SHAPE)
    after["properties"]["email"] = {"type": "string"}

    result = _lf.compare_schemas(CSV_SHAPE, after)

    assert result["is_identical"] is False
    assert result["is_compatible"] is True
    assert result["differences"] == [{"path": "properties.email", "type": "field_added", "field": "email"}]
    assert result["summary"]["fields_added"] == 1


def test_field_removed_is_classified_as_field_removed_and_breaks_compatibility():
    """A dropped column is breaking: ``field_removed`` and ``is_compatible`` false.

    **Validates: p30 design §5 O10**
    """
    after = copy.deepcopy(CSV_SHAPE)
    del after["properties"]["score"]

    result = _lf.compare_schemas(CSV_SHAPE, after)

    assert result["is_identical"] is False
    assert result["is_compatible"] is False
    assert result["differences"] == [{"path": "properties.score", "type": "field_removed", "field": "score"}]
    assert result["summary"]["fields_removed"] == 1


def test_jdbc_column_added_reports_one_field_added_and_not_the_required_growth():
    """Adding a jdbc column grows both ``properties`` and the ``required`` array,
    but only the ``properties`` entry is reported.

    ``deep_compare`` compares lists by their **first element only**
    (``obj1[0]`` vs ``obj2[0]``) and never compares length, so the longer
    ``required`` array contributes nothing. Pinned as a known limitation: task
    4.4 changes ``type`` classification, not list handling, so this count must
    stay at exactly one.

    **Validates: p30 design §5 O10**
    """
    after = copy.deepcopy(JDBC_SHAPE)
    after["properties"]["email"] = {"type": "string"}
    after["required"].append("email")

    result = _lf.compare_schemas(JDBC_SHAPE, after)

    assert result["differences"] == [{"path": "properties.email", "type": "field_added", "field": "email"}]
    assert result["is_compatible"] is True


def test_jdbc_required_reorder_is_a_value_change_and_never_a_type_mismatch():
    """A column **reorder** on a jdbc table must not read as a type change.

    The jdbc path's ``required`` array holds field *names* as its values
    (task 0.3, case B). On a table with columns literally named ``integer`` and
    ``string``, a reorder produces ``value_changed: "integer" → "string"`` at
    ``required[0]`` — which looks exactly like a type change to any rule that
    sniffs the value vocabulary.

    This is the false positive R8.1's fix can most easily introduce: a reorder
    is not a breaking change, so it must stay ``value_changed`` and
    ``is_compatible`` must stay true. Task 0.3's recommended predicate gets
    this right structurally, because ``required[N]`` is not reachable through
    ``properties``/``items`` and so is not a schema node.

    **Validates: p30 design §5 O10 (guards R8.1's fix)**
    """
    before = {
        "type": "object",
        "properties": {"integer": {"type": "integer"}, "string": {"type": "string"}},
        "required": ["integer", "string"],
    }
    after = {
        "type": "object",
        "properties": {"integer": {"type": "integer"}, "string": {"type": "string"}},
        "required": ["string", "integer"],
    }

    result = _lf.compare_schemas(before, after)

    assert result["differences"] == [
        {"path": "required[0]", "type": "value_changed", "old_value": "integer", "new_value": "string"}
    ]
    assert "type_mismatch" not in _types_of(result)
    assert result["is_compatible"] is True


def test_data_field_literally_named_type_is_classified_on_the_field_not_the_type():
    """A data column literally named ``type`` added to the source is a
    ``field_added`` at ``properties.type`` — not a type change.

    Task 0.3, case A. Task 4.4 must reclassify ``value_changed`` diffs only;
    ``field_added``/``field_removed`` on a field named ``type`` keep their
    current classification.

    **Validates: p30 design §5 O10 (guards R8.1's fix)**
    """
    before = {"type": "object", "properties": {"id": {"type": "integer"}}}
    after = {"type": "object", "properties": {"id": {"type": "integer"}, "type": {"type": "string"}}}

    result = _lf.compare_schemas(before, after)

    assert result["differences"] == [{"path": "properties.type", "type": "field_added", "field": "type"}]
    assert "type_mismatch" not in _types_of(result)
    assert result["is_compatible"] is True


def test_streaming_path_nested_object_contents_are_invisible_to_comparison():
    """On the streaming paths a nested object's *contents* cannot be compared.

    Task 0.3, case H. ``infer_schema_from_records`` and
    ``arrow_schema_to_json_schema`` do not recurse: a nested object emits a bare
    ``{"type": "object"}`` with no ``properties`` and an array emits
    ``{"type": "array"}`` with no ``items``. So two runs whose nested payloads
    differ emit **byte-identical** schemas and ``compare_schemas`` reports no
    differences.

    The blind spot lives in the *extractor*, not in ``compare_schemas`` — there
    is nothing in its input to compare. Pinned so 4.4 does not mistake this for
    a comparison bug and try to fix it here.

    **Validates: p30 design §5 O10**
    """
    # Both runs carried a `nested` object; run 2's nested payload gained a
    # field. This is what the streaming extractor emits for *either* payload.
    emitted_run_1 = {"type": "object", "properties": {"id": {"type": "integer"}, "nested": {"type": "object"}}}
    emitted_run_2 = {"type": "object", "properties": {"id": {"type": "integer"}, "nested": {"type": "object"}}}

    result = _lf.compare_schemas(emitted_run_1, emitted_run_2)

    assert result["differences"] == []
    assert result["is_identical"] is True


def test_legacy_nested_field_added_reports_the_full_properties_path():
    """The legacy path nests without bound; an added field deep inside reports a
    dot-joined path that walks every ``properties`` level.

    **Validates: p30 design §5 O10**
    """
    after = copy.deepcopy(LEGACY_SHAPE)
    after["properties"]["nested"]["properties"]["deeper"]["properties"]["z"] = {"type": "string"}

    result = _lf.compare_schemas(LEGACY_SHAPE, after)

    assert result["differences"] == [
        {
            "path": "properties.nested.properties.deeper.properties.z",
            "type": "field_added",
            "field": "z",
        }
    ]
    assert result["is_compatible"] is True


def test_legacy_array_item_field_added_reports_an_items_path():
    """A field added to an array's item schema reports a path through ``items``.

    The legacy path is the only emitter that produces ``items`` at all, so this
    is the only shape in which array *contents* are comparable.

    **Validates: p30 design §5 O10**
    """
    after = copy.deepcopy(LEGACY_SHAPE)
    after["properties"]["rows"]["items"]["properties"]["d"] = {"type": "string"}

    result = _lf.compare_schemas(LEGACY_SHAPE, after)

    assert result["differences"] == [
        {"path": "properties.rows.items.properties.d", "type": "field_added", "field": "d"}
    ]
    assert result["is_compatible"] is True


def test_object_rooted_to_array_rooted_schema_is_incompatible():
    """Switching from an object-rooted to an array-rooted document already
    blocks, via ``field_removed`` on the root ``properties`` key.

    Task 0.3, case E. Asserted on the *outcome* and the ``field_removed`` diff
    only: the root ``type`` change (``object`` → ``array``) is itself a declared
    type change, so task 4.4 will reclassify that diff from ``value_changed`` to
    ``type_mismatch``. Pinning the classification here would encode the defect;
    the verdict is what must not regress.

    **Validates: p30 design §5 O10**
    """
    result = _lf.compare_schemas(JSON_SHAPE, LEGACY_ARRAY_ROOTED_SHAPE)

    assert result["is_compatible"] is False
    assert {"path": "properties", "type": "field_removed", "field": "properties"} in result["differences"]
    assert {"path": "items", "type": "field_added", "field": "items"} in result["differences"]


def test_json_string_and_dict_inputs_produce_the_same_result():
    """Both arguments may arrive as JSON strings or as dicts.

    ``lambda_handler`` passes ``extracted_schema`` straight from the state
    table, where it is a JSON string; other callers may pass a dict. The two
    forms must agree.

    **Validates: p30 design §5 O10**
    """
    after = copy.deepcopy(CSV_SHAPE)
    after["properties"]["email"] = {"type": "string"}

    from_dicts = _lf.compare_schemas(CSV_SHAPE, after)
    from_strings = _lf.compare_schemas(json.dumps(CSV_SHAPE), json.dumps(after))

    assert from_dicts == from_strings


def test_target_schema_hash_is_stable_across_calls():
    """C10 fence: the schema digest must be deterministic across calls.

    ``LGR-dd80c6d-1`` replaced the builtin ``hash()`` with ``hashlib.sha256``
    because ``hash()`` is randomized per process, which broke cross-invocation
    drift detection and mapping reuse. Asserted as **stability**, not as a
    literal digest: the hash is taken over the serialized schema, so pinning a
    digest would break on any shape change (task 0.3) while saying nothing
    about the property that matters.

    **Validates: p30 constraint C10 (LGR-dd80c6d-1)**
    """
    content = json.dumps(CSV_SHAPE, sort_keys=True).encode("utf-8")
    s3 = mock.MagicMock()
    s3.get_object.side_effect = lambda **_kwargs: {"Body": mock.MagicMock(read=lambda: content)}

    with mock.patch.object(_lf, "s3_client", s3):
        _, first = _lf.get_target_schema_from_s3("bucket", "schemas/target.json")
        _, second = _lf.get_target_schema_from_s3("bucket", "schemas/target.json")

    assert first == second
    assert len(first) == 64  # sha256 hex digest


# ===========================================================================
# WRITTEN TO THE REQUIREMENT — xfail until task 4.4 landed, now passing
# ===========================================================================


def test_declared_type_change_is_classified_as_type_mismatch():
    """R8.1: ``integer`` → ``string`` on a declared ``type`` is a ``type_mismatch``.

    R8.5 names this exact case. Today's output is
    ``{"path": "properties.id.type", "type": "value_changed",
    "old_value": "integer", "new_value": "string"}`` — the defect.

    **Validates: Requirement 8.1**
    """
    after = copy.deepcopy(JSON_SHAPE)
    after["properties"]["id"]["type"] = "string"

    result = _lf.compare_schemas(JSON_SHAPE, after)

    assert _types_of(result) == ["type_mismatch"]
    assert result["differences"][0]["path"] == "properties.id.type"
    assert result["summary"]["type_mismatches"] == 1


def test_declared_type_change_makes_the_schema_incompatible():
    """R8.2: a ``type_mismatch`` must make ``is_compatible`` false.

    Split from the classification assertion above because this is the half with
    the operational consequence: today ``is_compatible`` is **true** for
    ``integer`` → ``string``, so a breaking source change is adapted into the
    target table with no human decision.

    **Validates: Requirement 8.2**
    """
    after = copy.deepcopy(JSON_SHAPE)
    after["properties"]["id"]["type"] = "string"

    result = _lf.compare_schemas(JSON_SHAPE, after)

    assert result["is_compatible"] is False


def test_declared_type_change_nested_in_the_legacy_shape_is_a_type_mismatch():
    """R8.1 must hold at depth, not only at ``properties.{field}.type``.

    The legacy emitter nests without bound, so the fix cannot assume a
    two-segment path shape.

    **Validates: Requirement 8.1**
    """
    after = copy.deepcopy(LEGACY_SHAPE)
    after["properties"]["nested"]["properties"]["deeper"]["properties"]["b"]["type"] = "string"

    result = _lf.compare_schemas(LEGACY_SHAPE, after)

    assert _types_of(result) == ["type_mismatch"]
    assert result["is_compatible"] is False


def test_declared_type_change_under_array_items_is_a_type_mismatch():
    """R8.1 must hold for a type declared under ``items``, not just ``properties``.

    ``{"type": "array", "items": {"type": "integer"}}`` → ``items.type ==
    "string"`` narrows the element type of every row in the array.

    **Validates: Requirement 8.1**
    """
    after = copy.deepcopy(LEGACY_SHAPE)
    after["properties"]["scalars"]["items"]["type"] = "string"

    result = _lf.compare_schemas(LEGACY_SHAPE, after)

    assert _types_of(result) == ["type_mismatch"]
    assert result["is_compatible"] is False


def test_declared_type_change_on_a_field_name_containing_a_dot_is_a_type_mismatch():
    """R8.1 must hold for a field named ``a.b`` — a legal CSV header.

    Task 0.3, case C. ``deep_compare`` builds ``path`` by dot-joining, so this
    change emits ``path == "properties.a.b.type"``, indistinguishable from a
    nested ``a`` containing ``b``. Any implementation that reclassifies by
    parsing the joined path string will **miss this real type change**. The
    positional predicate task 0.3 recommends — decide during the walk, never by
    parsing ``d["path"]`` — handles it.

    **Validates: Requirement 8.1**
    """
    before = {"type": "object", "properties": {"a.b": {"type": "integer"}}}
    after = {"type": "object", "properties": {"a.b": {"type": "string"}}}

    result = _lf.compare_schemas(before, after)

    assert _types_of(result) == ["type_mismatch"]
    assert result["is_compatible"] is False


def test_declared_type_change_on_a_field_named_x_dot_type_is_a_type_mismatch():
    """R8.1 must hold for a field named ``x.type``.

    Task 0.3, case D — the ambiguous twin of case C. The emitted path is
    ``properties.x.type.type``, which a path-string parse cannot resolve. The
    walk knows it stepped through ``properties`` into one map entry, so the
    final ``type`` is unambiguously the declaration.

    **Validates: Requirement 8.1**
    """
    before = {"type": "object", "properties": {"x.type": {"type": "integer"}}}
    after = {"type": "object", "properties": {"x.type": {"type": "string"}}}

    result = _lf.compare_schemas(before, after)

    assert _types_of(result) == ["type_mismatch"]
    assert result["is_compatible"] is False
